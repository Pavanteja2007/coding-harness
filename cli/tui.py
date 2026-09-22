"""Vex full-screen TUI — a genuine persistent textual App replacing the
rich-print REPL (2026-09-13; cli/AGENTS.md "Real full-screen TUI" round).

    +-----------------------------------------------------------+
    | ◆ vex 0.1.0 · model <m> · <repo> · running        (header)|  <- Task A
    +-------------------------------------+---------------------+
    |  transcript (conversation, run      | ▶ todo              |  <- Task A
    |  results, diffs, rationale)          |   ✔ step 1 …        |    (live
    |  ...          (scroll, rich markup)  |   ▸ step 2 …        |     todo,
    |                                      | ─ status            |     status
    |                                      |   fix · editing      |     panel:
    |                                      |   3m 12s · $0.0031   |     round)
    +-------------------------------------+---------------------+
    | ⠋ planning sub-steps · 12 events · $0.0031 · 34s  (runline)|
    +-----------------------------------------------------------+
    | vex › describe what's wrong                        (input)|
    +-----------------------------------------------------------+
    | /help · ctrl+p palette · /sessions search · /feed history · shift+↑↓ scrollback |  <- hint bar
    +-----------------------------------------------------------+

Architecture (why this shape):

- The REPL's backend logic lives in cli/interactive.py (_run_one_fix,
  _execute_task, _resume_task, session index, plan preview, LiveMonitor
  ...) and is NOT reimplemented here — the TUI renders THAT backend.
  Where the backend prints through cli.ui's shared rich console,
  _CapturedConsole.capture records the themed segments and replays them
  into the transcript as styled rich Text (probe-verified: spans keep
  their resolved styles, e.g. `bold #e8114a`). Textual's markup parser
  doesn't know the `vex.*` theme roles, so every markup string is run
  through _m(), which rewrites `[vex.accent]` -> `[#E8114A bold]` etc.
  from ui.VEX_THEME — one source of truth for the palette.
- Task B: a run blocks its WORKER thread (never the UI). cli.interactive
  fires the _ON_TASK_START hook the moment the task id exists; the TUI
  spins a trace-tail thread that folds trace.jsonl events into a
  _RunState, and the UI repaints the SAME run-line widget per event —
  nothing scrolls, nothing re-prints. A UI timer advances the spinner
  frame between events. The backend's own LiveMonitor spinner is
  silenced via state["quiet"] (the run-line IS the live status); its
  event/cost accounting still runs.
- LIVE TRACE FEED (the live-trace round, cli.tracelog): the SAME tail
  thread feeds each event through a FeedBuilder (pure event->entry
  mapping over the harness's public trace schema — Task E: the trace
  file stays the single source, the feed is a view, never a second
  logging path) and the produced entries render into the transcript
  the MOMENT they happen: readable one-liners for reasoning/model
  replies (Task A), tool actions ("Reading src/utils.py", "Running:
  pytest tests/x.py" — Task B), and after any edit-shaped action the
  real inline diff of pristine/ vs work/ (Task C — the same trees the
  harness diffs at completion, computed live, small and colored).
  Detail is attached per entry (command + its output, whole model
  reply) and expandable on demand via /trace <n> in a modal (Task D —
  collapsed by default, exactly Claude-Code-style). /quiet toggles
  state["feed"] (NOT state["quiet"] — the worker sets quiet to silence
  the backend's spinner; the feed gate is the user's own).
- LIVE TODO + STATUS PANEL + COMPLETION CARD (this round, cli.runview):
  the same tail thread also folds each event into a TodoModel (the
  harness's OWN plan/step_end decomposition — checkmarks appear the
  moment a step completes, never a parallel tracking system) and the
  sidebar polls transitions.jsonl for the state-machine phase; the
  sidebar shows mode · state · elapsed · cost, the data the run-line
  already tracks. When a run finishes, a completion CARD (runview.
  read_run_facts over the run's own records) replaces the backend's
  raw result scroll with one polished summary — numbers re-derived
  from the trace at render time, so the card can never drift from the
  real record (the runview discipline: read-only, writes nothing).
- MODE ROUTING (this round): the TUI now dispatches the SAME four work
  modes as the REPL (harness.router.route_kind) — fix, question, build,
  research — instead of funneling every non-convo line into a fix. The
  old cli.intent gate stays for convo/ambiguous (its original job);
  work-kind decisions come from the harness classifier, identical to
  the rich REPL.
- Backend blocking prompts (plan preview's "run this plan? [Y/n]") and
  the approval gate become MODAL screens: _prompt_patches swaps
  input()/print() for the worker thread only; input() pushes a modal
  via the UI thread and blocks on an Event. The modal's body shows the
  lines the backend just printed (the plan / the diff) — snapshotted
  from the live capture buffer as styled Text.
- /cancel and Ctrl+C: the REPL raised SIGINT at its MAIN thread (its
  loop was the blocker). In the TUI the run lives in a worker thread,
  so cli.interactive's _CANCEL_RUN hook (set by the app) injects an
  async KeyboardInterrupt there instead (ctypes
  PyThreadState_SetAsyncExc — probe-verified; run_task's
  except-KeyboardInterrupt path then stops containers, keeps
  checkpoints, records the resumable session).
- Approvals during a run are surfaced proactively: while a task is
  live, an approval watcher polls the file gate and raises the SAME
  confirm modal (with the diff) when a request parks — /approve and
  /reject keep working as manual commands too.

Threading model: textual's event loop (UI thread) | one worker thread
per run | trace-tail thread | approval watcher thread. Cross-thread UI
access goes through call_from_thread (wrapped in _safe_call, which
tolerates shutdown).

Windows note: textual needs ANSI; Windows Terminal / VT-enabled conhost
have it (textual's Windows driver enables VT processing itself). If
textual can't run (no TTY, dumb console, ImportError), cli.main falls
back to the rich REPL — never a crash. VEX_TUI=0 forces the fallback.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

import cli.fuzzy as _fz
import cli.interactive as _iv
import cli.tracelog as _tl
import cli.ui as ui
from cli import runview as _rv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HINTS = (
    "/help commands · ctrl+p palette · ctrl+r history · @path attach file "
    "· /sessions search · /feed trace history · ctrl+c cancel/quit · ctrl+q quit"
)

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "|/\\"

#: Thinking uses ui.THINK_FRAMES (distinct ORBIT glyph, shared with the
#: rich REPL's LiveMonitor) and the shared ui.JOKES table — one source.


def _spinner_frames() -> str:
    """Braille frames when the console encoding allows, else ASCII
    (cp1252 discipline — same rule as ui.SPINNER)."""
    return _SPINNER_FRAMES if ui._enc_ok("\u280b") else _SPINNER_ASCII


_STATUS_IDLE = "idle"
_STATUS_RUNNING = "running"
_STATUS_WAITING = "waiting for approval"

#: Directories the palette's file scan skips — dependency/build/cache
#: noise that no one fuzzy-searches for, so the useful repo files stay
#: inside the cap even in a huge checkout.
_FILE_SKIP = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        "dist",
        "build",
        ".eggs",
        "site-packages",
        ".next",
        ".nuxt",
        "target",
    }
)


def scan_repo_files(repo: Any, cap: int = 4000) -> List[str]:
    """Relative POSIX paths of the repo's files, for the palette's
    fuzzy file search (Task A). Prefers `git ls-files` (fast, honors
    .gitignore) and falls back to a bounded, cache-skipping walk for
    non-git repos. Capped at `cap` files so opening the palette on a
    huge tree stays instant. Never raises — an unreadable repo yields an
    empty list.
    """
    import os
    import subprocess

    root = Path(str(repo or ""))
    if not root.is_dir():
        return []
    files: List[str] = []
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            timeout=3.0,
        )
        if proc.returncode == 0 and proc.stdout:
            raw = proc.stdout.decode("utf-8", errors="replace").split("\0")

            # git can resolve an ANCESTOR repo when `root` isn't itself
            # one (it would then list the wrong tree) — trust a git path
            # only when the file actually exists under `root`. Skip-list
            # applies either way (node_modules is never a palette hit).
            def _real(f: str) -> bool:
                if any(part in _FILE_SKIP for part in f.split("/")):
                    return False
                try:
                    return (root / f).is_file()
                except OSError:
                    return False

            files = sorted(f for f in raw if f and _real(f))[:cap]
            if files:
                return files
    except Exception:
        pass
    # fallback walk: skip hidden + cache dirs, cap the total
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _FILE_SKIP and not d.startswith(".") and d != "logs"
            ]
            for name in filenames:
                try:
                    rel = Path(dirpath, name).relative_to(root).as_posix()
                except ValueError:
                    continue
                files.append(rel)
                if len(files) >= cap:
                    return sorted(files)
    except OSError:
        pass
    return sorted(files)


# ---------------------------------------------------------------------------
# Markup role mapping — textual doesn't know the vex.* rich theme roles;
# rewrite them to concrete colors from ui.VEX_THEME (single source).
# ---------------------------------------------------------------------------

_ROLE_RE = re.compile(r"\[(/?)(vex\.[a-z0-9.]+)\]")


def _style_to_markup(style: Any) -> str:
    """A rich Style -> a textual-markup style string ("bold #e8114a")."""
    parts: List[str] = []
    if getattr(style, "bold", False):
        parts.append("bold")
    if getattr(style, "italic", False):
        parts.append("italic")
    color = getattr(style, "color", None)
    if color is not None:
        try:
            parts.append("#" + color.get_truecolor().hex.lstrip("#"))
        except Exception:
            pass
    return " ".join(parts)


def _build_role_map() -> Dict[str, str]:
    """role name -> concrete markup style, from ui.VEX_THEME."""
    out: Dict[str, str] = {}
    try:
        for name, style in ui.VEX_THEME.styles.items():
            if name.startswith("vex."):
                mk = _style_to_markup(style)
                if mk:
                    out[name] = mk
    except Exception:
        pass
    return out


_ROLE_MAP = _build_role_map()

# ---------------------------------------------------------------------------
# Textual chrome theme — pin the framework's OWN colors to the design
# tokens. Textual's default theme injects non-token chrome: a BLUE focus
# border/cursor/selection (#0178D4/#368AE9), $surface (#1E1E1E) modal
# backgrounds, and a plain-white input cursor (probe-verified against
# the exported SVG). Theme(variables=...) is textual's sanctioned hook
# for overriding exactly these names (design.py: colors.update(
# self.variables)) — one registration, every widget pinned.
# ---------------------------------------------------------------------------


def _vex_textual_theme() -> Any:
    """Build the textual Theme from the shared ui tokens (VEX_DESIGN
    SYSTEM.md). Registered per-app in VexApp.__init__ as "vex"."""
    from textual.theme import Theme

    return Theme(
        name="vex",
        primary=ui.ACCENT_TEXT,
        secondary=ui.ACCENT_TEXT,
        accent=ui.ACCENT_TEXT,
        foreground=ui.TEXT_PRIMARY,
        background=ui.BG_BASE,
        surface=ui.BG_PANEL,
        panel=ui.BG_PANEL,
        warning=ui.WARNING,
        error=ui.ERROR,
        success=ui.SUCCESS,
        dark=True,
        variables={
            # the active fills: the input cursor + text selection are
            # ACTIVITY — the logo's crimson, never textual's blue
            "input-cursor-background": ui.ACCENT_TEXT,
            "input-cursor-foreground": ui.TEXT_PRIMARY,
            "input-selection-background": ui.ACCENT_TEXT + "66",
            "input-selection-foreground": ui.TEXT_PRIMARY,
            "screen-selection-background": ui.ACCENT_TEXT + "7F",
            "screen-selection-foreground": ui.TEXT_PRIMARY,
            "block-cursor-background": ui.ACCENT_TEXT,
            "block-cursor-foreground": ui.TEXT_PRIMARY,
            "block-cursor-blurred-background": ui.ACCENT_PRIMARY + "4C",
            "block-cursor-blurred-foreground": ui.TEXT_PRIMARY,
            # focus/blur borders + link chrome
            "border": ui.ACCENT_TEXT,
            "border-blurred": ui.BORDER_SUBTLE,
            "link-background-hover": ui.ACCENT_PRIMARY,
            "footer-key-foreground": ui.ACCENT_TEXT,
            # placeholders + hints render as text-secondary
            "text-disabled": ui.TEXT_SECONDARY,
            "foreground-disabled": ui.TEXT_SECONDARY,
            # SCROLLBARS (Task B): textual's default theme ships a pure
            # BLACK scrollbar track (#000000) and a primary-derived red
            # thumb — both non-token colors that leaked into the rendered
            # SVG (probe-verified: 24+ black cells down the transcript
            # edge). Pinned to tokens: track = bg-base, thumb =
            # border-subtle, hover/active = accent crimson.
            "scrollbar": ui.BORDER_SUBTLE,
            "scrollbar-hover": ui.ACCENT_TEXT,
            "scrollbar-active": ui.ACCENT_TEXT,
            "scrollbar-background": ui.BG_BASE,
            "scrollbar-background-hover": ui.BG_PANEL_HOVER,
            "scrollbar-background-active": ui.BG_BASE,
            "scrollbar-corner-color": ui.BG_BASE,
        },
    )


def _m(text: str) -> str:
    """Map [vex.role] markup tags to concrete textual styles (unknown
    roles are left alone — textual renders them as no-ops, never an
    error). Safe on any string."""
    if "vex." not in text:
        return text
    return _ROLE_RE.sub(
        lambda mo: f"[{mo.group(1)}{_ROLE_MAP.get(mo.group(2), mo.group(2))}]",
        text,
    )


# ---------------------------------------------------------------------------
# Feed-line rendering (shared by the live transcript, /trace and the
# scrollable /feed browser — one renderer, one style table; visual
# distinction round Task E lives HERE: reasoning is italic+dim, actions
# are upright+accent, so every surface that shows the feed agrees).
# ---------------------------------------------------------------------------

FEED_GLYPHS: Dict[str, str] = {
    "reason": ui.GLYPHS["bullet"],
    "tool": ">",
    "diff": ui.GLYPHS["arrow"],
    "verify": ui.GLYPHS["ok"],
    "lifecycle": ui.GLYPHS["ember"],
    "info": "*",
}

FEED_STYLES: Dict[str, str] = {
    # reasoning = italic + dimmed secondary (the agent THINKING, quiet);
    # tool/diff = upright bold accent (the agent DOING — scannable at a
    # glance); verify is outcome-aware (see feed_line), lifecycle muted.
    "reason": f"italic {ui.TEXT_SECONDARY}",
    "tool": f"bold {ui.ACCENT_TEXT}",
    "diff": f"bold {ui.ACCENT_TEXT}",
    "verify": ui.TEXT_SECONDARY,
    "lifecycle": ui.TEXT_SECONDARY,
    "info": ui.TEXT_SECONDARY,
}

VERIFY_PASS_MARKS = (
    "checkpoint passed",
    "final verification — target + full suite pass",
    "task success",
    "result: success",
)
VERIFY_FAIL_MARKS = (
    "still failing",
    "regressed",
    "flaky",
    "tool error",
    "static check failed",
    "edit policy violation",
    "self-critique rejected",
    "rejected (entry not read-only)",
)


def feed_style(entry: "_tl.FeedEntry") -> str:
    """The line style for one feed entry. Verify lines are
    OUTCOME-aware (success green / error red — never green by default);
    everything else takes the category style."""
    if entry.category == "verify":
        s = entry.summary.lower()
        if any(m in s for m in VERIFY_PASS_MARKS):
            return ui.SUCCESS
        if any(m in s for m in VERIFY_FAIL_MARKS):
            return ui.ERROR
    return FEED_STYLES.get(entry.category, ui.TEXT_SECONDARY)


def feed_line(entry: "_tl.FeedEntry") -> str:
    """A feed entry as one display markup line: glyph + index + summary,
    styled by category. The reasoning/action distinction (Task E) lives
    in the returned style string (italic dim for reason, bold accent for
    tool), so it is consistent everywhere the feed renders."""
    glyph = FEED_GLYPHS.get(entry.category, "*")
    style = feed_style(entry)
    return (
        f"[{style}]{glyph}[/][vex.muted] {entry.index:>2}[/] "
        f"[{style}]{escape(entry.summary)}[/]"
    )


# ---------------------------------------------------------------------------
# Segment reassembly — recorded console output -> styled Text lines
# ---------------------------------------------------------------------------


def _styled_lines_from_segments(segments: List[Any]) -> List[Text]:
    """Recorded rich segments -> per-line Text objects with spans kept.

    Newlines inside segments split lines; trailing console padding is
    stripped; leading/trailing blank lines are dropped. Control
    segments (cursor moves) are skipped.
    """
    lines: List[Text] = [Text()]
    for seg in segments:
        if seg.control is not None:
            continue
        parts = seg.text.split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append(Text())
            if part:
                lines[-1].append(part, style=seg.style)
    # rich's Text.rstrip() mutates in place and returns None (unlike
    # str) — strip the old way, per line, inside try (never raises).
    out_lines: List[Text] = []
    for ln in lines:
        try:
            ln.rstrip()
        except Exception:
            pass
        out_lines.append(ln)
    lines = out_lines
    while lines and not lines[0].plain:
        lines.pop(0)
    while lines and not lines[-1].plain:
        lines.pop()
    return lines


class _CapturedConsole:
    """Record cli.ui's shared console output for the transcript.

    capture() swaps the shared rich console's file to a buffer, turns
    recording on, runs the backend call, reassembles the recorded
    segments into styled Text lines, and restores everything (even on
    exception — the partial output is kept in self.last_lines). The
    err_console is covered by patching ui.err_console to return the
    captured console (it builds a fresh stderr console per call, so
    there is no instance to swap).

    The file swap is process-global, but the TUI runs backend calls
    only from its worker thread; textual renders through its own driver
    (never ui.console) — so the blast radius is exactly the worker's
    own prints.
    """

    def __init__(self, width: int = 100) -> None:
        self._width = width
        self.last_lines: List[Text] = []

    def set_width(self, width: int) -> None:
        self._width = max(width or 100, 40)

    def capture(self, fn: Callable, *args, **kwargs) -> Tuple[Any, List[Text]]:
        """Run fn with the shared console captured; return (result,
        styled lines). fn's exception is re-raised AFTER reassembly.

        Rich 14's begin_capture() buffers SEGMENTS in con._buffer (the
        documented record buffer is plain-text-only); draining _buffer
        preserves the resolved styles (probe-verified: 'green3',
        '#d9645c' survive).

        FILE-STATE DISCIPLINE (a real leak, found live): rich's
        `con.file` GETTER resolves DYNAMICALLY (None _file -> sys.
        stdout at each access) — during a textual run that is
        textual's _PrintCapture redirect. Snapshotting the GETTER and
        restoring by assignment froze that app-lifetime object as the
        console's PERMANENT file; every later print in the process
        vanished (the cross-suite contamination bug). Save/restore the
        EXPLICIT _file/_width state instead: None stays None, so the
        console goes back to following sys.stdout after the app exits.
        """
        import io

        con = ui.console()
        old_file = getattr(con, "_file", None)  # explicit state only
        old_width = getattr(con, "_width", None)
        old_err = ui.err_console
        buf = io.StringIO()
        exc: Optional[BaseException] = None
        result: Any = None
        lines: List[Text] = []
        try:
            con._file = buf  # type: ignore[attr-defined]
            ui.err_console = lambda: con  # type: ignore[assignment]
            try:
                con._width = self._width  # type: ignore[attr-defined]
            except Exception:
                pass
            con.begin_capture()
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:  # re-raised after reassembly
                exc = e
        finally:
            try:
                lines = _styled_lines_from_segments(list(con._buffer))
            except Exception:
                lines = [Text(ln) for ln in buf.getvalue().splitlines()]
            try:
                con.end_capture()
            except Exception:
                pass  # capture state; the file/width restore below is
                # what actually matters for the next non-TUI print
            con._file = old_file  # type: ignore[attr-defined]
            con._width = old_width  # type: ignore[attr-defined]
            ui.err_console = old_err  # type: ignore[assignment]
            self.last_lines = lines
        if exc is not None:
            raise exc
        return result, lines

    def current_lines(self) -> List[Text]:
        """Snapshot of the CURRENT in-flight capture (live prompt bodies):
        the lines printed so far by the run, still sitting in the
        console's segment buffer (capture holds them until end)."""
        try:
            con = ui.console()
            live = _styled_lines_from_segments(list(getattr(con, "_buffer", [])))
            if live:
                return live
            return list(self.last_lines)
        except Exception:
            return list(self.last_lines)


# ---------------------------------------------------------------------------
# Prompt screens — the backend's input()/print() become modals
# ---------------------------------------------------------------------------


class _PromptScreen(ModalScreen[str]):
    """A blocking prompt as a modal: question + body (styled Text lines
    rendered in a RichLog), an input line, Enter submits, Esc cancels
    (dismisses None -> the worker sees EOFError)."""

    CSS = """
    _PromptScreen {
        align: center middle;
    }
    #prompt-box {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round #E8114A;
    }
    #prompt-title {
        color: #E8114A;
        text-style: bold;
        margin-bottom: 1;
    }
    #prompt-body {
        height: auto;
        max-height: 16;
        margin-bottom: 1;
    }
    #prompt-input {
        border: round #E8114A;
    }
    #prompt-hint {
        color: #8A8A8A; /* text-secondary (design token), not a random grey */
        margin-top: 1;
    }
    """

    def __init__(self, question: str, body_lines: Optional[List[Text]] = None) -> None:
        super().__init__()
        self._question = question
        self._body = body_lines or []

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(escape(self._question), id="prompt-title")
            yield RichLog(id="prompt-body", markup=False, wrap=False)
            yield Input(placeholder="answer", id="prompt-input")
            yield Static("enter submit · esc cancel", id="prompt-hint")

    def on_mount(self) -> None:
        self._dismissed = False
        body = self.query_one("#prompt-body", RichLog)
        for ln in self._body:
            body.write(ln)
        self.query_one("#prompt-input", Input).focus()

    def _dismiss_once(self, result: Optional[str]) -> None:
        """Dismiss guarded against double-firing (an enter can deliver
        both Input.Submitted and a key event; a second pop crashes the
        screen stack — seen live in the Pilot drives)."""
        if self._dismissed:
            return
        self._dismissed = True
        try:
            self.dismiss(result)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._dismiss_once(event.value)

    def on_key(self, events_key: events.Key) -> None:
        if events_key.key == "escape":
            events_key.stop()  # don't double-fire into app-level bindings
            events_key.prevent_default()
            self._on_escape()

    def _on_escape(self) -> None:
        """Esc default: cancel the prompt (subclasses override for 'n')."""
        self._dismiss_once(None)


class _ConfirmScreen(_PromptScreen):
    """y/n confirm variant (plan preview, approval gate). Empty answer
    submits the default; Esc counts as 'n' (the safe choice for a
    run-gating decision)."""

    def __init__(
        self,
        question: str,
        body_lines: Optional[List[Text]] = None,
        default: str = "y",
    ) -> None:
        super().__init__(question, body_lines)
        self._default = default

    def on_input_submitted(self, event: Input.Submitted) -> None:
        ans = event.value.strip().lower()
        self._dismiss_once(ans if ans else self._default)

    def _on_escape(self) -> None:
        """Esc on a run-gating confirm = the safe answer ('n')."""
        self._dismiss_once("n")


def _confirm_default(prompt: str) -> str:
    """The default answer for a y/n prompt: plan preview defaults to
    yes, the approval gate to no (matching the REPL's prompts)."""
    return "n" if "approve this fix" in prompt.lower() else "y"


class _OnboardScreen(ModalScreen[Optional[bool]]):
    """First-run model onboarding as a modal (the TUI half of the
    no-model wizard; the REPL half is cli.onboard.run_repl_wizard).

    One screen, stepped: pick (official vs router presets) ->
    base_url (prefilled, editable) -> model (suggestions + free text)
    -> api_key (masked) -> live TEST (one tiny litellm call; a
    failure shows the error and returns to the key step — bad creds
    are never saved) -> save to GLOBAL settings (model follows the
    screen's tier, default global). Esc at any step skips (dismisses
    None — offline mode; `vex login` re-runs). Dismisses True when
    credentials were saved.
    """

    CSS = """
    _OnboardScreen {
        align: center middle;
    }
    #onboard-box {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        background: $surface;
        border: round #E8114A;
    }
    #onboard-title {
        color: #E8114A;
        text-style: bold;
        margin-bottom: 1;
    }
    #onboard-hint {
        color: #8A8A8A;
        margin-top: 1;
    }
    #onboard-error {
        color: #E8114A;
        margin-top: 1;
    }
    #onboard-input {
        border: round #E8114A;
    }
    """

    def __init__(self, model_tier: str = "global") -> None:
        super().__init__()
        self._tier = model_tier if model_tier in ("global", "project") else "global"
        self._step = "pick"
        self._pid = ""
        self._base: Optional[str] = None
        self._model = ""
        self._key = ""
        self._error = ""
        self._dismissed = False
        self._testing = False
        self._render_task = None

    def compose(self) -> ComposeResult:
        with Vertical(id="onboard-box"):
            yield Static("", id="onboard-title")
            yield Vertical(id="onboard-body")
            yield Static("", id="onboard-error")
            yield Static("esc skip (offline mode)", id="onboard-hint")

    def on_mount(self) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # keep a reference: an un-referenced task can be GC'd
            # mid-paint (RUF006).
            self._render_task = loop.create_task(self._render_step())
        # no running loop (shouldn't happen under textual): the body
        # stays empty rather than crashing the session.

    def _dismiss_once(self, result: Optional[bool]) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        try:
            self.dismiss(result)
        except Exception:
            pass

    def on_key(self, events_key: events.Key) -> None:
        if events_key.key == "escape" and not self._testing:
            events_key.stop()
            events_key.prevent_default()
            self._dismiss_once(None)

    # -- steps ---------------------------------------------------------

    def _preset(self) -> Dict[str, Any]:
        from cli import onboard as _ob

        return dict(_ob.PRESETS.get(self._pid, _ob.PRESETS["custom"]))

    async def _render_step(self) -> None:
        from cli import onboard as _ob

        title = self.query_one("#onboard-title", Static)
        body = self.query_one("#onboard-body", Vertical)
        err = self.query_one("#onboard-error", Static)
        err.update(_m(f"[vex.error]{escape(self._error)}[/]") if self._error else "")
        # rebuild the body per step (one screen, stepped — simpler
        # than a screen stack, and Esc always means skip)
        try:
            await body.remove_children()
        except Exception:
            pass
        if self._step == "pick":
            title.update(_m("[vex.accent]Configure a model (once)[/]"))
            opts = OptionList(id="onboard-pick")
            for pid in _ob.PRESET_ORDER:
                p = _ob.PRESETS[pid]
                hint = p.get("key_hint") or "no key needed"
                opts.add_option(Option(f"{p['label']} — {hint}", id=pid))
            await body.mount(opts)
            try:
                opts.highlighted = 0  # Enter works without arrow keys
            except Exception:
                pass
            opts.focus()
        elif self._step in ("base", "model", "key"):
            p = self._preset()
            if self._step == "base":
                if p["kind"] == "official":
                    title.update(
                        _m(
                            "[vex.accent]base_url[/] [vex.muted](empty = litellm default)[/]"
                        )
                    )
                    pre = self._base or ""
                else:
                    title.update(_m("[vex.accent]base_url[/] [vex.muted](editable)[/]"))
                    pre = (
                        self._base
                        if self._base is not None
                        else str(p.get("base_url") or "")
                    )
                inp = Input(value=pre, id="onboard-input")
            elif self._step == "model":
                sugg = list(p.get("models") or [])
                title.update(
                    _m(
                        "[vex.accent]model[/]"
                        + (
                            f" [vex.muted](suggestions: {escape(', '.join(sugg))})[/]"
                            if sugg
                            else " [vex.muted](free text)[/]"
                        )
                    )
                )
                inp = Input(
                    value=self._model or (sugg[0] if sugg else ""), id="onboard-input"
                )
            else:
                hint = p.get("key_hint") or "api_key"
                title.update(
                    _m(f"[vex.accent]api_key[/] [vex.muted]({escape(hint)})[/]")
                )
                inp = Input(value="", password=True, id="onboard-input")
            await body.mount(inp)
            inp.focus()
        elif self._step == "testing":
            title.update(_m("[vex.accent]Testing the endpoint...[/]"))
            await body.mount(
                Static("one tiny live call; nothing is saved until it passes")
            )

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if self._step != "pick":
            return
        event.stop()
        pid = str(getattr(event.option, "id", "") or "")
        from cli import onboard as _ob

        if pid not in _ob.PRESETS:
            pid = "custom"
        self._pid = pid
        p = _ob.PRESETS[pid]
        self._base = None if p["kind"] == "official" else str(p.get("base_url") or "")
        self._model = ""
        self._key = ""
        self._error = ""
        self._step = "base"
        await self._render_step()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._step not in ("base", "model", "key"):
            return
        event.stop()
        from cli import onboard as _ob

        val = event.value.strip()
        if self._step == "base":
            p = self._preset()
            if p["kind"] == "official":
                self._base = val or None
            else:
                if self._pid == "custom" and not val:
                    self._error = "Custom needs a base_url."
                    await self._render_step()
                    return
                self._base = val or None
            self._error = ""
            self._step = "model"
            await self._render_step()
        elif self._step == "model":
            if not val:
                self._error = "A model name is required."
                await self._render_step()
                return
            self._model = val
            self._error = ""
            self._step = "key"
            await self._render_step()
        else:
            p = self._preset()
            no_key_ok = bool(p.get("no_key")) or _ob._is_local_base(self._base)
            if not val and not no_key_ok:
                self._error = "api_key is required for this endpoint."
                await self._render_step()
                return
            self._key = val
            self._error = ""
            self._step = "testing"
            await self._render_step()
            self._run_test()

    def _run_test(self) -> None:
        """One tiny live litellm call off the UI thread (a blocking
        endpoint must never freeze the shell); the result returns via
        call_from_thread."""
        import threading

        from cli import onboard as _ob

        self._testing = True
        provider = str(self._preset().get("provider") or "openai")
        model, key, base = self._model, self._key, self._base

        def _work() -> None:
            ok, err = _ob.test_credentials(provider, model, key, base)
            try:
                self.app.call_from_thread(self._on_test_done, ok, err)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def _schedule_render(self) -> None:
        """Re-render from a sync context (the test verdict arrives via
        call_from_thread): schedule the async step painter on the app's
        running loop. Falls back to a direct paint-less state update
        (never raises — a UI nicety must not take down the verdict)."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            # keep a reference: an un-referenced task can be GC'd
            # mid-paint (RUF006).
            self._render_task = loop.create_task(self._render_step())
        except Exception:
            pass

    def _on_test_done(self, ok: bool, err: str) -> None:
        """Test verdict back on the UI thread: save (dismiss True) or
        show the error and return to the key step (nothing saved)."""
        self._testing = False
        if ok:
            from cli import onboard as _ob

            try:
                _ob.save_credentials(
                    str(self._preset().get("provider") or "openai"),
                    self._model,
                    self._key,
                    self._base,
                    self._tier,
                )
            except (ValueError, OSError) as exc:
                self._error = f"Could not save settings: {exc}"
                self._step = "key"
                self._schedule_render()
                return
            self._dismiss_once(True)
            return
        self._error = f"Test failed: {err} — fix the key and press enter."
        self._step = "key"
        self._schedule_render()


# ---------------------------------------------------------------------------
# Trace tailing (Task B) — trace events -> in-place run-line updates
# ---------------------------------------------------------------------------


class _RunState:
    """Mutable snapshot of one live run, derived from trace events only.

    Same event-kind contract as _iv.LiveMonitor (harness trace.jsonl is
    the public observability surface; fields used are documented ones).
    This round adds the structured companions: `todo` (cli.runview's
    TodoModel — the harness's own plan/step decomposition) and `mode`
    (the work mode, from task_start's mode field or the TUI's dispatch).
    """

    __slots__ = (
        "calls",
        "cost",
        "events",
        "feed",
        "mode",
        "phase",
        "started_at",
        "task_id",
        "thinking",
        "todo",
        "tokens",
    )

    def __init__(self, task_id: str, mode: str = "fix") -> None:
        self.task_id = task_id
        self.mode = mode
        self.phase = "starting"
        self.events = 0
        self.calls = 0
        self.tokens = 0
        self.cost = 0.0
        self.started_at = time.monotonic()
        self.thinking = False  # True between model_request and its response
        self.feed = _tl.FeedBuilder(task_id)  # live one-liner feed (Tasks A-D)
        self.todo = _rv.TodoModel()  # live checklist (todo/status round)

    def consume(self, obj: Dict[str, Any]) -> None:
        """Fold one trace event into the state (never raises)."""
        kind = obj.get("kind")
        data = obj.get("data") or {}
        if not kind:
            return
        self.events += 1
        try:
            self.todo.consume(obj)
        except Exception:
            pass
        if kind == "model_request":
            self.thinking = True
        elif kind == "model_response":
            self.thinking = False
        if kind == "model_response":
            usage = data.get("usage") or {}
            self.calls += 1
            self.tokens += int(usage.get("tokens") or 0)
            self.cost += float(usage.get("cost") or 0.0)
        if kind == "task_start" and data.get("mode"):
            self.mode = str(data["mode"])
        label = _iv._EVENT_LABELS.get(kind)
        if label:
            for field in _iv._LABEL_FIELDS.get(kind, []):
                if data.get(field) is not None:
                    label = label.format(**{field: data[field]})
            # events missing the context field leave {placeholder}s —
            # strip them (a phase label reads as a phrase, not a fmt)
            self.phase = re.sub(r"\{[a-z]+\}", "", label)

    def line(self, frame: int = -1) -> str:
        """Run-line text. Thinking phases get the orbit glyph + a
        rotating tech joke (Qwen-Code-style flavor) in the rose glow
        tone (accent-glow — the lighter end of the logo's gradient,
        reserved for the thinking moment); every other phase gets the
        linear spinner in the logo's crimson — the two states read
        differently at a glance. Both render: glyph phase · [joke ·]
        N events · $cost · elapsed."""
        elapsed = int(time.monotonic() - self.started_at)
        if self.thinking:
            frames = ui.THINK_FRAMES
            spin = frames[frame % len(frames)] if frame >= 0 else ""
            spin_txt = f"[vex.glow]{spin}[/] " if spin else ""
            joke = ui.joke_at(frame // 36) if frame >= 0 else ui.JOKES[0]
            return (
                f"{spin_txt}[vex.glow]{self.phase}[/] "
                f"[vex.muted]{ui.DOT}[/] [i {ui.TEXT_SECONDARY}]{escape(joke)}[/] "
                f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{self.events} events[/] "
                f"[vex.muted]{ui.DOT}[/] [vex.accent2]{ui.fmt_cost(self.cost)}[/] "
                f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{elapsed}s[/]"
            )
        frames = _spinner_frames()
        spin = frames[frame % len(frames)] if frame >= 0 else ""
        spin_txt = f"[vex.running]{spin}[/] " if spin else ""
        return (
            f"{spin_txt}[vex.running]{self.phase}[/] "
            f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{self.events} events[/] "
            f"[vex.muted]{ui.DOT}[/] [vex.accent2]{ui.fmt_cost(self.cost)}[/] "
            f"[vex.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{elapsed}s[/]"
        )


def _tail_trace(
    task_id: str,
    log_root: Path,
    run: _RunState,
    stop: threading.Event,
    on_event: Callable[[_RunState, List[_tl.FeedEntry]], None],
    poll_s: float = 0.25,
) -> None:
    """Tail logs/{task_id}/trace.jsonl, folding events into `run` (run-line
    state + feed entries + todo checklist) and invoking on_event(run,
    new_entries) after each batch that changed anything (on_event
    marshals to the UI thread; new_entries are the feed lines to render
    live — Tasks A/B; batches with no feed entries still repaint for
    the todo's ACTIVE marker and the phase label). Survives
    missing/rotating files — same tolerance as LiveMonitor (the harness
    archives then recreates the trace).

    FINAL DRAIN: after `stop` is set, reads the file once more so the
    last events (the run's closing step_end/result) land in the state
    before the UI renders its final snapshot — otherwise the todo
    could show a stale mid-run checklist next to a finished card."""
    trace = Path(log_root) / task_id / "trace.jsonl"
    while not stop.is_set() and not trace.exists():
        time.sleep(0.1)
    pos = 0

    def _drain() -> bool:
        """Read new bytes once; fold + fire. True when anything landed."""
        nonlocal pos
        if not trace.exists():
            return False
        try:
            with trace.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
        except OSError:
            return False  # rotation mid-read
        if not chunk:
            return False
        pos += len(chunk)
        new_entries: List[_tl.FeedEntry] = []
        consumed = False
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            run.consume(obj)
            consumed = True
            new_entries.extend(run.feed.consume(obj))
        if consumed:
            on_event(run, new_entries)
        return consumed

    while not stop.is_set():
        _drain()
        stop.wait(poll_s)
    # final drain: the closing events must reach the final snapshot
    deadline = time.monotonic() + 2.0
    while _drain() and time.monotonic() < deadline:
        time.sleep(0.05)


class _TranscriptLog(RichLog):
    """The transcript, with real scrollback (Task C): the auto-follow
    that keeps the live feed pinned to the bottom PAUSES the moment the
    user scrolls away from the bottom (mouse wheel, page keys, any
    scroll path — the hook is textual's single low-level `_scroll_to`,
    which every scroll funnels through) and RESUMES when they return to
    it. Without this, reading the feed's history mid-run was impossible:
    every new entry yanked the view back down.

    Deliberately not key-bound: the input box owns the keyboard, so the
    scroll signal has to come from the scrolling itself. A programmatic
    scroll (RichLog's own auto-follow calls scroll_end -> _scroll_to with
    y == max) keeps following; a USER scroll up sets auto_scroll False,
    a user scroll back to the bottom re-enables it."""

    def _scroll_to(
        self,
        x: "float | None" = None,
        y: "float | None" = None,
        *,
        animate: bool = True,
        **kwargs,
    ) -> bool:
        was_following = self.auto_scroll
        moved = super()._scroll_to(x, y, animate=animate, **kwargs)
        try:
            if y is not None:
                at_bottom = y >= self.max_scroll_y - 1
                if was_following and at_bottom:
                    # THIS is RichLog's own auto-follow write; keep going
                    self.auto_scroll = True
                else:
                    # a user scroll: follow iff they ended at the bottom
                    self.auto_scroll = at_bottom
        except Exception:
            pass
        return moved


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


class VexApp(App):
    """The persistent vex session shell (full-screen textual app)."""

    TITLE = "vex"
    SUB_TITLE = "the AI harness that fixes bugs"

    CSS = f"""
    Screen {{
        layout: vertical;
        background: {ui.BG_BASE};
    }}
    /* Header: the compact header (version/model/repo/status) — the
       splash-vs-header design; the wordmark splash renders inside the
       transcript on first launch instead of stealing the layout. */
    #vex-header {{
        height: 1;
        padding: 0 1;
    }}
    #vex-brand {{
        width: 1fr;
    }}
    #vex-status {{
        width: auto;
        color: {ui.ACCENT_TEXT};
    }}
    /* Middle: transcript (1fr) + the live sidebar (todo + status
       panel). The sidebar fills the previously-empty right rail with
       data the run already tracks; it collapses when nothing runs. */
    #vex-mid {{
        height: 1fr;
    }}
    #vex-body {{
        width: 1fr;
        padding: 0 1;
        border-top: solid {ui.BORDER_SUBTLE};
        /* scrollbar colors come from the pinned theme variables (see
           _vex_textual_theme) — the old two-value shorthand left the
           track at textual's default BLACK (Task B). */
        scrollbar-size: 1 1;
    }}
    #vex-side {{
        width: 34;
        min-width: 26;
        padding: 0 1;
        background: {ui.BG_PANEL};
        border-left: solid {ui.BORDER_SUBTLE};
        display: none;
    }}
    #vex-side-header {{
        color: {ui.ACCENT_TEXT};
        text-style: bold;
    }}
    #vex-todo {{
        height: auto;
        max-height: 14;
        margin-bottom: 1;
    }}
    #vex-side-label {{
        color: {ui.TEXT_SECONDARY};
        text-style: bold;
    }}
    #vex-side-status {{
        height: auto;
    }}
    /* Run-line: the ONLY live widget during a run — updated in place.
       Sits on a raised panel surface with a left activity edge in the
       logo's crimson (active = accent, per the design system). */
    #vex-runline {{
        height: 1;
        padding: 0 1;
        background: {ui.BG_PANEL};
        border-left: outer {ui.ACCENT_TEXT};
        display: none;
    }}
    /* Input box pinned directly under the transcript (spacing
       tightened: the transcript's own border is the only divider —
       no empty band between the two). */
    #vex-inputwrap {{
        height: 3;
        padding: 0 1;
    }}
    #vex-input {{
        border: round {ui.BORDER_SUBTLE};
        background: {ui.BG_PANEL};
    }}
    #vex-input:focus {{
        border: round {ui.ACCENT_TEXT};
        /* textual's Input:focus default is `background-tint: $foreground
           5%` — a non-token lighter blend (probe-verified).
           Focus is an INTERACTIVE state, so it uses the token for
           elevated/interactive surfaces: bg-panel-hover, exactly. */
        background: {ui.BG_PANEL_HOVER};
        background-tint: {ui.BG_PANEL_HOVER} 0%;
    }}
    /* Hint bar: keyboard shortcuts, OpenCode-style. */
    #vex-hints {{
        height: 1;
        padding: 0 1;
        background: {ui.BG_PANEL};
        color: {ui.TEXT_SECONDARY};
    }}
    """

    BINDINGS: ClassVar[List[Binding]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", show=True, priority=True),
        Binding("ctrl+q", "quit_app", "quit", show=True),
        # NOTE: no app-level escape binding — Esc belongs to modal
        # screens (cancel prompt); binding it globally made the modal
        # dismiss race with action_clear_input (ScreenStackError).
        # Clearing the input line: ctrl+u (Input's built-in) or select-all+type.
        Binding("ctrl+p", "command_palette", "commands", show=False),
        # Ctrl+R: searchable input-history (the persistent conversation's
        # raw lines — type to filter, enter recalls one into the input).
        Binding("ctrl+r", "input_history", "history", show=False),
        # Ctrl+Space: complete the @path mention under the cursor from
        # the repo's file list (scan_repo_files, same source as the
        # palette's file entries).
        Binding("ctrl+space", "complete_mention", "complete @path", show=False),
        # Transcript scrollback (Task C): shift+arrows move through the
        # history while the input keeps focus (priority=True beats the
        # focused widget); shift+end re-pins to the live tail.
        Binding(
            "shift+pageup",
            "scroll_history(-1)",
            "scrollback",
            show=False,
            priority=True,
        ),
        Binding("shift+pagedown", "scroll_history(1)", show=False, priority=True),
        Binding("shift+up", "scroll_history(-1)", show=False, priority=True),
        Binding("shift+down", "scroll_history(1)", show=False, priority=True),
        Binding("shift+end", "scroll_history_end", show=False, priority=True),
    ]

    def __init__(
        self,
        repo: Path,
        log_root: Path,
        state: Optional[Dict[str, Any]] = None,
        file_config: Optional[Dict[str, Any]] = None,
        version: str = "",
        onboard_prompt: bool = False,
    ) -> None:
        super().__init__()
        # Pin textual's chrome (cursor/selection/focus borders/modal
        # surfaces) to the design tokens BEFORE the CSS applies — the
        # default theme ships a blue cursor/selection (#0178D4-class,
        # probe-verified) that would otherwise leak through every
        # widget the stylesheet doesn't explicitly style. register_theme
        # + assignment is textual's supported path (App.theme setter).
        try:
            self.register_theme(_vex_textual_theme())
            self.theme = "vex"
        except Exception:
            pass  # a theme failure must never stop the shell
        self.repo = repo
        self.log_root = Path(log_root)
        self.state: Dict[str, Any] = state or {
            "model": None,
            "provider": None,
            "plan_preview": None,
            "quiet": False,
            "feed": True,
            "repo": str(repo),
            "file_config": None,
        }
        self.file_config: Dict[str, Any] = file_config or {}
        self.version = version
        # First-run onboarding: run_tui passes True only on a real TTY;
        # on_mount then offers the modal wizard when no usable model is
        # set. An explicit flag (not an isatty probe at mount — textual
        # swaps sys.stdout under Pilot, so a mount-time probe fires in
        # headless test drives) keeps direct VexApp(...) construction
        # modal-free.
        self._onboard_prompt = bool(onboard_prompt)
        self.last: Dict[str, Any] = {}  # last run info (task_id/diff/status)
        self._status: str = _STATUS_IDLE
        self._run: Optional[_RunState] = None
        self._run_stop: Optional[threading.Event] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_ident: Optional[int] = None
        self._exit_code: int = 0
        self._shutting_down = False
        self._spinner_frame = 0
        self._cap = _CapturedConsole()
        # the mode the TUI dispatched for the run in flight (sidebar +
        # completion card labeling; corrected by the trace's own
        # task_start mode field for modes the harness labels itself)
        self._dispatched_mode: Optional[str] = None
        # cached state-machine phase (transitions.jsonl, re-read on
        # lifecycle events — runview.read_machine_state)
        self._machine_state: Optional[str] = None
        # the run's trace-tail thread (joined at finish for the final
        # drain — the closing events must reach the final snapshot)
        self._tail_thread: Optional[threading.Thread] = None
        # lines submitted while a run is in flight (drained post-run)
        self._queue: List[str] = []
        self._first_run_note: Optional[str] = None
        # palette file cache: (repo_str, [relpath, ...]) — rescans only
        # when the session's repo changes (a big tree should cost once).
        self._files_cache: Optional[Tuple[str, List[str]]] = None
        # approval requests already prompted (one prompt per request)
        self._approval_handled: set = set()
        # agent-loop require-mode latch (Allow always for this run) +
        # pending approved-plan guidance for the next agent run
        self._agent_allow_always: bool = False
        self._pending_agent_guidance: Optional[str] = None
        # prompt-body lines declared by the backend for the next modal
        # (set via the _PROMPT_BODY hook, consumed by _prompt_modal)
        self._pending_prompt_body: Optional[List[Text]] = None
        # The bound hook methods, stored ONCE so teardown can compare by
        # identity (`self._on_prompt_body is self._on_prompt_body` is
        # False for bound methods — each attribute access builds a new
        # wrapper; without stored refs the owner-tagged unmount would
        # never clear the hooks).
        self._hook_task_start = self._on_task_start_hook
        self._hook_cancel = self._interrupt_worker
        self._hook_prompt_body = self._on_prompt_body
        # Persistent conversation (opencode-style session): the
        # multi-turn transcript + input history + compacted summary,
        # loaded on mount (needs log_root/repo — set above).
        self.conversation: Optional[Dict[str, Any]] = None
        self._hist_idx: Optional[int] = None

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="vex-header"):
            yield Static("", id="vex-brand")
            yield Static("", id="vex-status")
        with Horizontal(id="vex-mid"):
            yield _TranscriptLog(
                id="vex-body",
                markup=True,
                wrap=False,
                highlight=False,
                auto_scroll=True,
            )
            with Vertical(id="vex-side"):
                yield Static("", id="vex-side-header")
                yield Static("", id="vex-todo")
                yield Static("", id="vex-side-label")
                yield Static("", id="vex-side-status")
        yield Static("", id="vex-runline")
        with Vertical(id="vex-inputwrap"):
            yield Input(placeholder="describe what's wrong, or /help", id="vex-input")
        yield Static("", id="vex-hints")

    def on_mount(self) -> None:
        try:
            self._cap.set_width(self.size.width)
        except Exception:
            pass
        self._render_header()
        self._set_hints(_HINTS)
        # Every session opens with the VEX wordmark in the transcript
        # (the brand mark IS the shell's face); first launch adds the
        # full info block, later sessions keep it lean.
        self._print_splash(first_launch=_iv._is_first_launch(self.log_root))
        if self._first_run_note:
            self.transcript(self._first_run_note)
        # Persistent conversation + memory-first: load (or create) the
        # session state file, then surface repo-scoped decisions +
        # structural index state automatically (best-effort muted lines;
        # a prior compacted summary is shown, not hidden).
        try:
            from cli.session import (
                load_or_create as _load_conversation,
            )
            from cli.session import (
                session_memory_brief as _memory_brief,
            )

            self.conversation = _load_conversation(self.log_root, self.repo)
            try:
                for mem_line in _memory_brief(self.repo, self.log_root):
                    self.transcript(f"[vex.muted]{escape(mem_line)}[/]")
            except Exception:
                pass
            _summary = str((self.conversation or {}).get("summary") or "").strip()
            if _summary:
                self.transcript(
                    f"[vex.muted]session context: {escape(_summary[:200])}[/]"
                )
        except Exception:
            self.conversation = None
        self.query_one("#vex-input", Input).focus()
        # Spinner heartbeat: repaint the run-line ~8x/s while a run is
        # live (advances the frame; the tail thread repaints on events).
        self.set_interval(0.125, self._tick_spinner)
        # Register the cli.interactive hooks (see module docstring): the
        # run-line starts when the task id exists; cancel targets the
        # worker instead of SIGINT-at-main (the main thread is textual's).
        _iv._ON_TASK_START = self._hook_task_start
        _iv._CANCEL_RUN = self._hook_cancel
        _iv._PROMPT_BODY = self._hook_prompt_body
        # First-run onboarding: no usable model/auth anywhere in the
        # chain -> the modal wizard, ONCE at session start (skippable
        # via Esc, VEX_NO_ONBOARD=1). Only when run_tui opted in (real
        # TTY) — direct construction (tests, embeds) stays modal-free.
        try:
            if self._onboard_prompt:
                from cli import onboard as _ob

                if _ob.needs_onboarding():
                    self.push_screen(_OnboardScreen(), self._onboard_done)
        except Exception:
            pass

    def _onboard_done(self, saved: Optional[bool]) -> None:
        """Onboarding modal closed: on save, reload the chain + repaint
        the header (the new model shows immediately); on skip, one
        muted line (offline mode stays usable). Never raises."""
        try:
            if saved:
                from cli.vexconfig import merged_settings

                self.file_config = merged_settings()
                self._render_header()
                self.transcript(
                    "[vex.ok]model configured[/] [vex.muted](`vex login` to change)[/]"
                )
            else:
                self.transcript(
                    "[vex.muted]no model configured (offline mode) — "
                    "`vex login` any time[/]"
                )
        except Exception:
            pass

    def _on_prompt_body(self, prompt: str, body_lines: List[str]) -> None:
        """The _PROMPT_BODY hook: remember the context lines the backend
        rendered for the prompt that follows (plan steps / diff), so the
        modal body shows them (the console capture can't see prints made
        from helper threads — probe-verified; this hook is the contract).

        Guarded: while THIS app is mounted, only the current run's
        backend should fire it; ignore anything else (never raises)."""
        try:
            self._pending_prompt_body = [Text(ln) for ln in body_lines][:25]
        except Exception:
            pass

    def on_unmount(self) -> None:
        """Teardown: drop THIS app's hooks, stop the tail/approval
        threads, cancel an in-flight run (daemon threads die with the
        process).

        OWNER-TAGGED teardown: unmount only clears the hooks this app
        installed. Textual can run an old app's unmount AFTER the next
        app mounted (async shutdown); a blanket None would strip the
        new app's hooks mid-session (seen live: the plan-preview modal
        lost its body lines ~1 in 8 full-suite runs)."""
        self._shutting_down = True
        if _iv._ON_TASK_START is self._hook_task_start:
            _iv._ON_TASK_START = None
        if _iv._CANCEL_RUN is self._hook_cancel:
            _iv._CANCEL_RUN = None
        if _iv._PROMPT_BODY is self._hook_prompt_body:
            _iv._PROMPT_BODY = None
        if self._run_stop is not None:
            self._run_stop.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._interrupt_worker()

    # -- rendering helpers (all updates go through these, in place) ------

    def _render_header(self, status_text: str = "") -> None:
        """The compact header: ◆ vex <ver> · model <m> · <repo> [· status]."""
        model = _iv._session_model_label(self.state, self.file_config)
        ember = ui.GLYPHS["ember"]
        bits = f"[vex.accent]{ember} vex[/]"
        if self.version:
            bits += f" [vex.muted]{self.version}[/]"
        bits += f" [vex.muted]{ui.DOT}[/] [vex.muted]model[/] [vex.accent2]{model}[/]"
        bits += f" [vex.muted]{ui.DOT}[/] [vex.muted]{Path(self.repo).name}[/]"
        self.query_one("#vex-brand", Static).update(_m(bits))
        self._set_status(status_text or self._status or _STATUS_IDLE)

    def _set_status(self, text: str) -> None:
        self._status = text
        try:
            self.query_one("#vex-status", Static).update(_m(f"[vex.running]{text}[/]"))
        except Exception:
            pass  # not mounted yet (early call from a worker)

    def _set_hints(self, text: str) -> None:
        self.query_one("#vex-hints", Static).update(text)

    def _render_run(self, run: Optional[_RunState] = None) -> None:
        """Repaint the run-line widget IN PLACE (Task B core)."""
        run = run or self._run
        if run is None:
            self._hide_runline()
            return
        rl = self.query_one("#vex-runline", Static)
        rl.update(_m(run.line(self._spinner_frame)))
        rl.styles.display = "block"

    def _hide_runline(self) -> None:
        try:
            self.query_one("#vex-runline", Static).styles.display = "none"
        except Exception:
            pass

    # -- sidebar: live todo + status panel (todo/status round) ---------

    _TODO_MARKS: ClassVar[Dict[str, tuple]] = {
        # pending renders as muted (text-secondary); ACTIVE is the
        # logo's crimson (active = accent, per the design system);
        # done/skipped are honestly distinct: done = success green,
        # skipped = muted (skipping is not passing); failed = error red.
        _rv.PENDING: ("○", ui.TEXT_SECONDARY),
        _rv.ACTIVE: ("▸", ui.ACCENT_TEXT),
        _rv.DONE: ("✔", ui.SUCCESS),
        _rv.SKIPPED: ("↷", ui.TEXT_SECONDARY),
        _rv.FAILED: ("✘", ui.ERROR),
    }

    def _render_side(self, run: Optional[_RunState] = None) -> None:
        """Repaint the sidebar: the todo checklist + the status rows.

        The todo folds the harness's OWN plan/step events (run.todo);
        the machine state comes from transitions.jsonl (polled on the
        same tail callback, cached per tick — a file read every render
        would be wasteful and the phase changes only on transitions).
        Must run on the UI thread.
        """
        run = run or self._run
        try:
            if run is None:
                self.query_one("#vex-side", Vertical).styles.display = "none"
                return
            self.query_one("#vex-side", Vertical).styles.display = "block"
            head = self.query_one("#vex-side-header", Static)
            head.update(
                _m(
                    f"[vex.accent]{ui.GLYPHS['ember']}[/] "
                    f"[{ui.TEXT_PRIMARY}]{run.task_id}[/]"
                )
            )
            # -- todo checklist (Task A) --------------------------------
            todo_widget = self.query_one("#vex-todo", Static)
            lines: List[str] = []
            done, total = run.todo.progress()
            lines.append(
                f"[{ui.TEXT_SECONDARY}]todo[/] [{ui.TEXT_PRIMARY}]{done}/{total}[/]"
                if run.todo.steps
                else ""
            )
            enc_ok = ui._enc_ok("✔")
            for st in run.todo.steps[:14]:
                mark, color = self._TODO_MARKS.get(
                    run.todo.state_of(st), ("○", ui.TEXT_SECONDARY)
                )
                if not enc_ok:
                    mark = {"✔": "v", "✘": "x", "▸": ">", "↷": "-", "○": "o"}.get(
                        mark, mark
                    )
                desc = st.description or f"step {st.sid}"
                clip = desc if len(desc) <= 30 else desc[:29] + "…"
                lines.append(f"[{color}]{mark}[/] [{color}]{clip}[/]")
            todo_widget.update(_m("\n".join(lines)))
            # -- status panel (Task B) ----------------------------------
            self.query_one("#vex-side-label", Static).update("status")
            sw = self.query_one("#vex-side-status", Static)
            elapsed = _rv.fmt_elapsed(time.monotonic() - run.started_at)
            machine = self._machine_state or "—"
            rows = [
                f"[vex.muted]mode [/] [vex.accent2]{run.mode}[/]",
                f"[vex.muted]state[/] [vex.running]{machine}[/]",
                f"[vex.muted]time [/] [{ui.TEXT_PRIMARY}]{elapsed}[/]",
                f"[vex.muted]cost [/] [vex.accent2]{ui.fmt_cost(run.cost)}[/]",
            ]
            sw.update(_m("\n".join(rows)))
        except Exception:
            pass  # pre-mount call or shutdown; never kill a run

    def _refresh_machine_state(self, task_id: str) -> None:
        """Re-read the state-machine audit trail (best-effort, UI-safe)."""
        try:
            self._machine_state = _rv.read_machine_state(self.log_root / task_id)
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        """UI timer: advance the spinner frame + sidebar clock while a
        run is live (the sidebar's elapsed/cost tick between events)."""
        if self._run is not None and not self._shutting_down:
            self._spinner_frame += 1
            self._render_run()
            self._render_side()

    def transcript(self, text: Any = "") -> None:
        """Append to the transcript (str markup / rich Text / any
        renderable). Strings go through _m so vex.* roles keep their
        colors; anything failing renders as escaped plain text."""
        if isinstance(text, str):
            text = _m(text)
        try:
            self.query_one("#vex-body", RichLog).write(text)
        except Exception:
            pass  # shutting down; a daemon thread's last words stay lost

    def _print_splash(self, first_launch: bool = True) -> None:
        """Session-open splash rendered into the transcript: the
        gradient wordmark EVERY session (the brand mark is the shell's
        face). First launch renders the HERO LOCKUP — the wordmark on
        the left, the repo/logs/model/version key-value block vertically
        centered beside it (a manual two-column row assembly, not a stack
        of printed lines) — plus the tagline; later sessions keep the
        wordmark + tagline + one lean hint row."""
        rows = ui.wordmark_lines()
        if ui._enc_ok("╔"):
            logo_rows = [ui.gradient_text(row) for row in rows]
        else:
            logo_rows = [Text(row, style=f"bold {ui.ACCENT_TEXT}") for row in rows]
        logo = Text()
        for i, row in enumerate(logo_rows):
            if i:
                logo.append_text(Text("\n"))
            logo.append_text(row)
        if not first_launch:
            self.transcript(logo)
            self.transcript(
                Text("  the AI harness that fixes bugs ", style=ui.TEXT_SECONDARY)
                + Text(ui.DOT, style=ui.ACCENT_TEXT)
                + Text(" verified, not vibed", style=ui.TEXT_PRIMARY)
            )
            # lean re-open: wordmark + tagline + one hint row
            self.transcript(
                f"[vex.muted]logs[/] [{ui.TEXT_PRIMARY}]"
                f"{self.log_root.resolve()}[/] "
                f"[vex.muted]{ui.DOT} plain language starts a fix[/]"
            )
            return
        # the info block: aligned key-value rows, vertically centered
        # against the 6-row wordmark. Rows are joined MANUALLY (one Text
        # per visual row: logo row + gap + key + value) — rich's Columns
        # auto-stacks its items once the pair exceeds the console width,
        # which reproduces exactly the stacked look Task C replaces; and
        # Table.rows are opaque (no public cell copy). Long values are
        # left-truncated ("...tail") to the remaining width, so the
        # distinctive tail (the repo dir name) stays visible and the hero
        # never overflows narrow terminals.
        model = _iv._session_model_label(self.state, self.file_config)
        try:
            app_w = self.size.width or 80
        except Exception:
            app_w = 80
        gap = "  "
        key_w = 5  # "model" is the widest key; keys right-align to it
        budget = max(app_w - 26 - len(gap) - key_w - 1 - 4, 20)

        def _fit(s: object) -> str:
            text = str(s)
            return text if len(text) <= budget else "..." + text[-(budget - 3) :]

        info_vals: List[Text] = [
            Text(_fit(self.repo), style=f"bold {ui.TEXT_PRIMARY}"),
            Text(_fit(self.log_root.resolve()), style=ui.TEXT_PRIMARY),
            Text(_fit(model), style=ui.ACCENT_TEXT),
        ]
        info_keys = ["repo", "logs", "model"]
        if self.version:
            info_keys.append("vex")
            info_vals.append(Text(_fit(self.version), style=ui.TEXT_PRIMARY))
        # vertical centering: the shorter column starts lower so both
        # sides share a midpoint
        pad_top = max((len(logo_rows) - len(info_keys)) // 2, 0)
        for i, logo_row in enumerate(logo_rows):
            line = Text()
            line.append_text(logo_row)
            line.append(gap)
            j = i - pad_top
            if 0 <= j < len(info_keys):
                line.append(Text(info_keys[j].rjust(key_w), style=ui.TEXT_SECONDARY))
                line.append(" ")
                line.append_text(info_vals[j])
            self.transcript(line)
        self.transcript("")
        self.transcript(
            Text("  the AI harness that fixes bugs ", style=ui.TEXT_SECONDARY)
            + Text(ui.DOT, style=ui.ACCENT_TEXT)
            + Text(" verified, not vibed", style=ui.TEXT_PRIMARY)
        )

    # -- worker-thread safe helpers ---------------------------------------

    def _thread_log(self, text: Any) -> None:
        """Worker-thread safe transcript append."""
        self._safe_call(self.transcript, text)

    def _safe_call(self, fn: Callable, *args) -> None:
        """call_from_thread that tolerates a shut-down app (worker threads
        are daemons; quitting during a run must not traceback)."""
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    # -- input handling (Task C: every command from the REPL is ported) --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        self._hist_idx = None
        if not line:
            return
        # Persistent conversation: every submitted line joins the
        # session history (Ctrl+R searches it; Up/Down browses it).
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_history, save_session

                append_history(self.conversation, line)
                save_session(self.log_root, self.conversation)
            except Exception:
                pass
        # Echo what the user typed (the transcript IS the conversation):
        # the prompt symbol is the logo's crimson — accent-primary's
        # text weight, per the design system's prompt/highlight role.
        self.transcript(
            f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] {escape(line)}"
        )
        self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        """Dispatch one submitted line — same branch order as the REPL."""
        low = line.lower()

        # A run in flight: slash commands still work (cancel/approve/
        # status...); other lines STEER the live task (steering round,
        # Task A — the TUI's whole reason for staying responsive: plain
        # text typed mid-run becomes an instruction the loop consumes
        # at its next safe checkpoint, never a silently queued task).
        if self._worker_thread is not None and self._worker_thread.is_alive():
            if low.startswith("/"):
                try:
                    self._slash_command(line, low, in_flight=True)
                except Exception as exc:
                    self.transcript(f"[vex.error]{escape(type(exc).__name__)}[/]")
                return
            self._steer_live(line)
            return

        # -- slash commands (incl. custom commands) -------------------
        if low.startswith("/"):
            try:
                self._slash_command(line, low)
            except Exception as exc:
                self.transcript(f"[vex.error]{escape(type(exc).__name__)}[/]")
            return

        # -- bare session commands --------------------------------------
        if low in ("exit", "quit", "q"):
            self.transcript("[vex.muted]bye[/]")
            self.exit()
            return
        if low in ("help", "?"):
            self.transcript(_iv._HELP)
            return
        if low.startswith("repo ") or (
            _iv._looks_like_path(low) and not low.startswith(("fix", "in"))
        ):
            path = line[5:].strip() if low.startswith("repo ") else line
            cand = Path(path).expanduser().resolve()
            if cand.is_dir():
                self.repo = cand
                self.state["repo"] = str(cand)
                self.transcript(f"[vex.ok]repo {ui.GLYPHS['arrow']} {cand}[/]")
                self._render_header()
            else:
                self.transcript(f"[vex.error]not a directory: {cand}[/]")
            return
        if low.startswith("model "):
            self.state["model"] = line[6:].strip()
            self.transcript(
                f"[vex.ok]model pinned {ui.GLYPHS['arrow']} "
                f"{escape(self.state['model'])}[/]"
            )
            self._render_header()
            return

        # -- agent dispatch (the SAME three-way classifier the rich
        # REPL uses): chit_chat answers inline and never launches;
        # question runs the read-only worker; agent_task (fix/build/
        # refactor/run/debug) runs the ONE agent loop on the live repo.
        # @path mentions are expanded first (attached file content is
        # conversation context, and the expanded text is what runs).
        if "@" in line:
            try:
                from cli.session import expand_at_mentions

                expanded, inserted = expand_at_mentions(
                    line, self.repo, self._palette_files()
                )
                if inserted:
                    self.transcript(
                        f"[vex.muted]attached: {escape(', '.join(inserted))}[/]"
                    )
                    line = expanded
                    low = line.lower()
            except Exception:
                pass
        from harness.agent_loop import classify_agent_input

        work = classify_agent_input(
            line,
            {
                **(self.file_config or {}),
                "model": self.state.get("model")
                or (self.file_config or {}).get("model"),
                "provider": self.state.get("provider")
                or (self.file_config or {}).get("provider"),
            },
        )
        if work.kind == "chit_chat":
            from cli.intent import clarify_reply

            reply = work.reply or clarify_reply()
            self.transcript(f"[vex.muted]{escape(reply)}[/]")
            if isinstance(self.conversation, dict):
                try:
                    from cli.session import append_turn, save_session

                    append_turn(self.conversation, "user", line)
                    append_turn(self.conversation, "assistant", reply)
                    save_session(self.log_root, self.conversation)
                except Exception:
                    pass
            return
        self._start_run(line, mode=work.kind)

    # -- slash commands (all built-ins + custom-command dispatch) -------

    def _slash_command(self, line: str, low: str, in_flight: bool = False) -> None:
        """The /command dispatcher (Task C port of _iv._slash_command,
        rendering into the transcript instead of stdout). `in_flight`
        gates the commands allowed while a run is live (approve/reject/
        cancel/status are exactly the ones that make sense then)."""
        parts = line.split()
        cmd = low.split()[0]

        if cmd in ("/help",):
            self.transcript(_iv._HELP)
            return

        if cmd in ("/status",):
            target = self.last.get("task_id")
            if not target:
                self.transcript("[vex.muted]no run in this session yet[/]")
                return
            self._render_task_status(target)
            return

        if cmd in ("/diff",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            low_rest = rest.lower()
            if (
                low_rest == "undo"
                or low_rest.startswith("undo ")
                or low_rest in ("undo all", "all")
            ):
                arg = (
                    rest[len("undo") :].strip()
                    if low_rest.startswith("undo")
                    else rest.strip()
                )
                self._render_undo(
                    _iv.undo_result(self.last, self.log_root, str(self.repo), arg)
                )
                return
            if self.last.get("diff"):
                try:
                    for text in ui.diff_render_lines(str(self.last["diff"])):
                        self.transcript(text)
                except Exception:
                    for dl in str(self.last["diff"]).splitlines():
                        self.transcript(escape(dl))
            else:
                # Agent runs with no recorded diff yet: recompute live
                # (pristine reference vs the repo) so /diff works
                # post-session — REPL parity.
                tid = self.last.get("task_id")
                live = ""
                if tid and str(tid).startswith("agent-"):
                    try:
                        from harness.agent_loop import agent_diff as _adiff

                        live = _adiff(str(tid), self.log_root, str(self.repo)) or ""
                    except Exception:
                        live = ""
                if live:
                    self.last["diff"] = live
                    try:
                        for text in ui.diff_render_lines(live):
                            self.transcript(text)
                    except Exception:
                        for dl in live.splitlines():
                            self.transcript(escape(dl))
                else:
                    self.transcript("[vex.muted]no diff from the last run[/]")
            return

        if cmd in ("/sessions",):
            self._open_sessions_screen(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            return

        if cmd in ("/resume",):
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            if len(parts) < 2:
                recent = _iv.most_recent_resumable(self.log_root)
                if recent is None or not recent.get("task_id"):
                    self.transcript(
                        "[vex.muted]usage: /resume <task_id> (see /sessions)[/]"
                    )
                    return
                self.transcript(
                    f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] "
                    f"/resume {escape(str(recent['task_id']))}"
                )
                self._start_resume(str(recent["task_id"]))
                return
            self._start_resume(parts[1])
            return

        if cmd in ("/approve", "/reject"):
            tid = parts[1] if len(parts) > 1 else self.last.get("task_id")
            if not tid:
                self.transcript(
                    "[vex.muted]no task to decide on — run with "
                    "--approval or approve from a benchmark[/]"
                )
                return
            decided = self._decide_pending(tid, approve=(cmd == "/approve"))
            if decided is None:
                self.transcript(f"[vex.muted]no pending approval request for {tid}[/]")
            return

        if cmd in ("/cancel",):
            if in_flight:
                self.transcript(
                    "[vex.warn]cancel requested — sending Ctrl+C semantics "
                    "to the running task (checkpoints kept)[/]"
                )
                self._interrupt_worker()
                return
            self.transcript("[vex.muted]nothing running[/]")
            return

        if cmd in ("/steer",):
            body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            if not body.strip():
                self.transcript(
                    "[vex.muted]usage: /steer <instruction> (plain text "
                    "while a run is live does the same; 'replan: …' "
                    "replaces the plan; 'abort' stops cleanly)[/]"
                )
                return
            if in_flight:
                self._steer_live(body)
            else:
                self.transcript(
                    "[vex.muted]nothing running — plain text starts a fix; "
                    "/steer only matters mid-run[/]"
                )
            return

        if cmd in ("/quiet",):
            self.state["quiet"] = not self.state.get("quiet", False)
            self.state["feed"] = not self.state.get("feed", True)
            feed_state = "on" if self.state["feed"] else "off"
            self.transcript(
                f"[vex.ok]verbosity: {'quiet' if self.state['quiet'] else 'normal'}"
                f"[/] [vex.muted](live feed {feed_state})[/]"
            )
            return

        if cmd in ("/model",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                self.state["model"] = rest
                self._render_header()
                self.transcript(
                    f"[vex.ok]model pinned {ui.GLYPHS['arrow']} {escape(rest)}[/]"
                )
                return
            from cli.onboard import format_model_display

            self.transcript(
                f"[vex.muted]{escape(format_model_display(self.state, self.file_config))}[/]"
            )
            return

        if cmd in ("/trace",):
            self._trace_command(parts[1] if len(parts) > 1 else None)
            return

        if cmd in ("/feed",):
            q = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self._feed_command(q)
            return

        if cmd in ("/plan",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                if in_flight:
                    self._queue.append(line)
                    self.transcript(
                        f"[vex.muted]queued (will run when the current task "
                        f"finishes — {len(self._queue)} waiting)[/]"
                    )
                    return
                # Agent-shaped text gets the lightweight agent preview
                # (steps/files modal, approve -> run, edit -> steer);
                # anything else keeps the fix-loop step preview.
                try:
                    from harness.agent_loop import classify_agent_input as _cl

                    _kind = _cl(rest, {}).kind
                except Exception:
                    _kind = "fix"
                if _kind == "agent_task":
                    self._agent_plan_preview(rest)
                    return
                # Plan mode: force step preview (approve before edits —
                # the backend's preview watcher becomes a modal) for
                # this run, then dispatch like a normal line.
                self.state["plan_preview"] = True
                self.transcript(
                    "[vex.accent]plan mode[/] [vex.muted]— previewing steps "
                    "before edits (approve/reject)[/]"
                )
                self._handle_line(rest)
                return
            self.state["plan_preview"] = not self.state.get("plan_preview")
            self.transcript(
                f"[vex.ok]plan preview: "
                f"{'on' if self.state['plan_preview'] else 'off'}[/] "
                "[vex.muted](next run previews before edits)[/]"
            )
            return

        if cmd in ("/review",):
            # A project/plugin "review.md" custom command keeps working:
            # "/review <args>" with a resolvable template runs it; a
            # bare "/review" renders the builtin diff + rationale view.
            from cli import commands as commands_mod

            rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            if rest.strip():
                template = commands_mod.load_command(
                    "review", repo_path=self.state.get("repo")
                )
                if template is not None:
                    if in_flight:
                        self._queue.append(line)
                        self.transcript(
                            f"[vex.muted]queued (will run when the current task "
                            f"finishes — {len(self._queue)} waiting)[/]"
                        )
                        return
                    filled = commands_mod.fill_template(template, rest)
                    self.transcript("[vex.accent]/review — running as a fix request[/]")
                    self.transcript(f"[vex.muted]instruction:[/]\n{escape(filled)}")
                    self._start_run(filled, mode="fix")
                    return
            self._render_review()
            return

        if cmd in ("/compact",):
            if not isinstance(self.conversation, dict):
                self.transcript("[vex.muted]no conversation to compact yet[/]")
                return
            try:
                from cli.session import compact_session, save_session

                summary = compact_session(self.conversation, self.log_root)
                save_session(self.log_root, self.conversation)
            except Exception:
                summary = ""
            if summary:
                self.transcript(
                    "[vex.ok]compacted[/] [vex.muted]— older turns summarized "
                    "(recall-backed), recent turns kept[/]"
                )
                self.transcript(f"[vex.muted]{escape(summary[:400])}[/]")
            else:
                self.transcript("[vex.muted]nothing to compact yet[/]")
            return

        if cmd in ("/copy-diff", "/copy"):
            diff = self.last.get("diff")
            if not diff:
                self.transcript("[vex.muted]no diff from the last run[/]")
                return
            try:
                from cli.session import copy_text_to_clipboard

                ok = copy_text_to_clipboard(str(diff))
            except Exception:
                ok = False
            if ok:
                self.transcript("[vex.ok]diff copied to the clipboard[/]")
            else:
                self.transcript(
                    "[vex.warn]clipboard unavailable[/] [vex.muted]— "
                    "see /diff for the full text[/]"
                )
            return

        if cmd in ("/history",):
            hist: List[str] = []
            try:
                if isinstance(self.conversation, dict):
                    hist = [str(h) for h in (self.conversation.get("history") or [])]
            except Exception:
                hist = []
            q = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            # the /sessions filter grammar, reused (same as the REPL):
            # each history line is scored as a session record's issue text.
            shown = [h for h in hist if _iv.history_matches(h, q)][-20:]
            if not shown:
                self.transcript(
                    f"[vex.muted]no history matches {escape(q)}[/]"
                    if q
                    else "[vex.muted]no input history yet[/]"
                )
                return
            self.transcript(
                "[vex.accent]input history[/] [vex.muted](most recent last)[/]"
            )
            for h in shown:
                self.transcript(f"  [vex.muted]{escape(h[:120])}[/]")
            return

        if cmd in ("/init",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                self.transcript(
                    "[vex.muted]usage: /init (scaffolds .vex/ in the session repo)[/]"
                )
                return
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            _iv._do_init(self.repo, say=self.transcript)
            return

        if cmd in ("/login",):
            rest = (
                line.split(None, 1)[1].strip().lower()
                if len(line.split(None, 1)) > 1
                else ""
            )
            if rest and rest not in ("global", "project"):
                self.transcript("[vex.muted]usage: /login [global|project][/]")
                return
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            # the TUI half of the wizard (no new auth code): the stepped
            # modal; _onboard_done reloads the chain + repaints.
            try:
                self.push_screen(
                    _OnboardScreen(model_tier=rest or "global"), self._onboard_done
                )
            except Exception:
                self.transcript("[vex.muted](login unavailable)[/]")
            return

        if cmd in ("/logout",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                self.transcript(
                    "[vex.muted]usage: /logout (removes the stored api_key)[/]"
                )
                return
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            # the onboarding module's own command (no new auth code);
            # its console output is captured into the transcript.
            try:
                _, lines = self._cap.capture(_iv._do_logout)
            except Exception as exc:
                self.transcript(f"[vex.error]logout failed: {type(exc).__name__}[/]")
                return
            for ln in lines:
                self.transcript(ln)
            return

        if cmd in ("/mcp",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if len(rest.split()) > 1:
                self.transcript("[vex.muted]usage: /mcp [label][/]")
                return
            if rest and in_flight:
                self.transcript(
                    "[vex.muted]tool listing waits for idle "
                    "(the server spawn blocks) — bare /mcp lists now[/]"
                )
                return
            try:
                cfg = dict(self.file_config or {})
                for k in ("model", "provider"):
                    if self.state.get(k):
                        cfg[k] = self.state[k]
            except Exception:
                cfg = None
            _iv._render_mcp(cfg, say=self.transcript, label=rest)
            return

        if cmd in ("/skills",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            _iv._render_skills(self.repo, say=self.transcript, filt=rest)
            return

        if cmd in ("/cost",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                self.transcript("[vex.muted]usage: /cost (last run + session total)[/]")
                return
            _iv._render_cost(self.last, self.log_root, say=self.transcript)
            return

        if cmd in ("/undo",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            self._render_undo(
                _iv.undo_result(self.last, self.log_root, str(self.repo), rest)
            )
            return

        if cmd in ("/clear",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if rest:
                self.transcript(
                    "[vex.muted]usage: /clear (starts a fresh conversation)[/]"
                )
                return
            if in_flight:
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            try:
                shim = {"conversation": self.conversation}
                new_id = _iv._do_clear(
                    self.log_root, self.repo, shim, say=self.transcript
                )
                if new_id:
                    self.conversation = shim["conversation"]
                    try:
                        self.query_one("#vex-body", RichLog).clear()
                    except Exception:
                        pass
                    self.transcript(
                        "[vex.ok]cleared[/] [vex.muted]— fresh conversation "
                        f"{escape(str(new_id))} (old kept)[/]"
                    )
            except Exception:
                self.transcript("[vex.muted](clear failed)[/]")
            return

        if in_flight:
            # custom commands during a run: queue like plain lines
            self._queue.append(line)
            self.transcript(
                f"[vex.muted]queued (will run when the current task "
                f"finishes — {len(self._queue)} waiting)[/]"
            )
            return

        # -- custom commands (cli/commands.py) -------------------------
        from cli import commands as commands_mod

        name = cmd.lstrip("/")
        template = commands_mod.load_command(name, repo_path=self.state.get("repo"))
        if template is not None:
            arguments = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            filled = commands_mod.fill_template(template, arguments)
            self.transcript(
                f"[vex.accent]/{escape(name)} — running as an agent task[/]"
            )
            if arguments:
                self.transcript(f"[vex.muted]arguments: {escape(arguments)}[/]")
            self.transcript(f"[vex.muted]instruction:[/]\n{escape(filled)}")
            self._start_run(filled)
            return

        available = commands_mod.command_names(self.state.get("repo"))
        hint = ""
        if available:
            hint = " — custom commands available: " + ", ".join(
                f"/{n}" for n in available
            )
        self.transcript(
            f"[vex.error]unknown command: {escape(line.split()[0])}[/] "
            f"[vex.muted]— try /help{escape(hint)}[/]"
        )

    def _decide_pending(self, task_id: str, approve: bool) -> Optional[bool]:
        """/approve //reject: the approval file protocol (the REPL's
        _decide_pending), transcript-rendered."""
        try:
            from runtime import approval as approval_mod
        except ImportError:
            self.transcript("[vex.error]runtime.approval unavailable[/]")
            return None
        gate = self.log_root / f"{task_id}.runtime" / "approval"
        req = approval_mod.pending_request(str(gate))
        if req is None:
            return None
        approval_mod.decide(str(gate), approve=approve)
        self._approval_handled.add(task_id)
        self.transcript(
            f"[vex.{'ok' if approve else 'error'}]"
            f"{'approved' if approve else 'rejected'}[/] — the worker "
            f"continues {'with' if approve else 'without'} the fix"
        )
        return approve

    def _render_task_status(self, task_id: str) -> None:
        """/status: structured state from logs/{task_id}/state.json,
        captured from the flag command's rich printer into the transcript."""
        import argparse as _ap

        from cli.main import cmd_status

        ns = _ap.Namespace(
            task_id=task_id,
            log_root=str(self.last.get("log_root") or self.log_root),
        )
        try:
            _, lines = self._cap.capture(cmd_status, ns)
        except Exception as exc:
            self.transcript(f"[vex.error]status failed: {type(exc).__name__}[/]")
            return
        for ln in lines:
            self.transcript(ln)

    def _render_undo(self, res: Dict[str, Any]) -> None:
        """Render an undo_result dict into the transcript (the TUI idiom:
        diff_render_lines for the refreshed diff). Updates self.last on
        done. Never raises."""
        try:
            outcome = (res or {}).get("outcome")
            if outcome == "not_agent":
                self.transcript(
                    "[vex.muted]undo is for agent sessions (this run never "
                    "touched the live repo)[/]"
                )
                return
            if outcome == "nothing":
                self.transcript("[vex.muted]nothing to undo[/]")
                return
            if outcome == "done":
                files = (res or {}).get("files") or []
                self.transcript(f"[vex.ok]undone {len(files)} file(s)[/]")
                diff = (res or {}).get("diff")
                if diff:
                    self.last["diff"] = diff
                    try:
                        for text in ui.diff_render_lines(str(diff)):
                            self.transcript(text)
                    except Exception:
                        for dl in str(diff).splitlines():
                            self.transcript(escape(dl))
                else:
                    self.last["diff"] = None
                return
            self.transcript(
                f"[vex.error]undo failed:[/] [vex.muted]{escape(str((res or {}).get('error', '?')))}[/]"
            )
        except Exception:
            pass

    def _render_review(self) -> None:
        """/review: the last run's diff + its rationale together in the
        transcript (the diff syntax-highlighted, the rationale as plain
        lines — never raises; absent pieces render one honest line)."""
        diff = self.last.get("diff")
        if diff:
            self.transcript("[vex.muted]diff:[/]")
            try:
                for text in ui.diff_render_lines(str(diff)):
                    self.transcript(text)
            except Exception:
                for dl in str(diff).splitlines()[:60]:
                    self.transcript(escape(dl))
        else:
            self.transcript("[vex.muted]no diff from the last run[/]")
        tid = self.last.get("task_id")
        body = _iv.last_rationale_text(self.log_root, tid) if tid else None
        if body:
            self.transcript("")
            self.transcript("[vex.muted]rationale:[/]")
            for ln in body.splitlines()[:40]:
                self.transcript(escape(ln))
        else:
            self.transcript("[vex.muted]no rationale recorded for the last run[/]")

    # -- /trace + /feed: the feed index, detail browser, and scrollable
    # searchable history (feed round Task D; scrollable-history round
    # Task C) -----------------------------------------------------------

    def _collect_feed_run(self) -> Optional[_RunState]:
        """The feed source for /trace and /feed: the live run's builder
        while a run is in flight, else a fresh run rebuilt from the LAST
        run's own trace.jsonl (Task E no-drift: the same data,
        re-derived — no copy kept). None when there's no run to read."""
        if self._run is not None:
            return self._run
        tid = self.last.get("task_id")
        if not tid:
            return None
        run = _RunState(tid)
        trace_file = self.log_root / tid / "trace.jsonl"
        if trace_file.is_file():
            try:
                for line in trace_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    run.consume(obj)
                    run.feed.consume(obj)
            except OSError:
                pass
        return run

    def _feed_command(self, query: str = "") -> None:
        """/feed [query] — the whole trace feed in a scrollable,
        searchable browser (Task C): every action line the run produced,
        reasoning styled apart from actions, type to filter, arrows or
        mouse to scroll, enter expands one entry."""
        run = self._collect_feed_run()
        if run is None:
            self.transcript("[vex.muted]no run in this session yet[/]")
            return
        entries = list(run.feed.entries)
        if not entries:
            self.transcript("[vex.muted]no feed entries yet[/]")
            return
        self.push_screen(_FeedBrowserScreen(entries, query))

    def _expand_feed_entry(self, entry: "_tl.FeedEntry") -> None:
        """Push the read-only detail modal for one feed entry (raw
        command + output / the model's whole reply / the event JSON)."""
        title = f"trace {entry.index} — {entry.detail_title or entry.category}"
        body: List[Text] = [
            Text(entry.summary, style=f"bold {ui.ACCENT_TEXT}"),
            Text(""),
        ]
        detail = (entry.detail or "").strip() or "(no detail recorded)"
        for ln in detail.splitlines()[:400]:
            body.append(Text(ln))
        self.push_screen(_TraceDetailScreen(title, body))

    def _open_sessions_screen(self, query: str = "") -> None:
        """/sessions [query] — the searchable, filterable session
        browser (Task C): status / repo / date / resumable tokens plus
        free text, over the full recorded history — not a flat list."""
        try:
            sessions = _iv.search_sessions(self.log_root, "", limit=300)
        except Exception:
            sessions = []
        if not sessions:
            self.transcript(
                f"[vex.muted]no recorded sessions under {self.log_root.resolve()}[/]"
            )
            return
        self.push_screen(_SessionsScreen(sessions, query), self._sessions_chosen)

    def _sessions_chosen(self, task_id: Optional[str]) -> None:
        """The sessions browser returns a task_id — resume that session
        (the explicit choose IS the confirmation; a filtered search that
        lands on one resumable run is exactly what /resume is for)."""
        if not task_id:
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
            return
        self.transcript(
            f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] "
            f"/resume {escape(task_id)}"
        )
        self._start_resume(task_id)

    def _trace_command(self, arg: Optional[str]) -> None:
        """/trace — the run's feed: no arg lists the one-liners (with
        their indexes), /trace N expands entry N's full detail (the raw
        command + output / the model's whole reply / the event's JSON)
        in a modal. Works live (the feed grows per event) and after the
        run (the builder keeps everything)."""
        if arg is not None:
            try:
                n = int(arg)
            except ValueError:
                self.transcript(
                    "[vex.error]usage: /trace [n][/][vex.muted] — n is an entry "
                    "number from the listing[/]"
                )
                return
        else:
            n = None
        run = self._collect_feed_run()
        if run is None:
            self.transcript("[vex.muted]no run in this session yet[/]")
            return
        entries = run.feed.entries
        if not entries:
            self.transcript("[vex.muted]no feed entries yet[/]")
            return
        if arg is None:
            self.transcript(
                f"[vex.accent]trace feed[/] [vex.muted]({len(entries)} entries — "
                "/trace <n> expands one · /feed scrolls + searches the whole "
                "session)[/]"
            )
            for ent in entries[-60:]:  # cap the listing; expand for more
                self.transcript(feed_line(ent))
            return
        match = next((e for e in entries if e.index == n), None)
        if match is None:
            self.transcript(
                f"[vex.error]no feed entry {n}[/] [vex.muted](0–{entries[-1].index})[/]"
            )
            return
        self._expand_feed_entry(match)

    # -- runs (worker thread + trace tail + in-place run-line) -----------

    def _steer_live(self, line: str) -> None:
        """Steer the live run with a plain-text line (steering round).

        Same contract as the REPL's reader-side handling (cli.interactive.
        steer_live_run does the parse/intent/live-gate work): conversational
        input is answered inline (never injected), everything else becomes
        a steering event in logs/{task_id}/steering.jsonl — the journal
        the loop's SteeringBuffer polls at its safe checkpoints. The
        live task id comes from _live_run() — the registration the
        backend's _execute_task maintains around every run — falling
        back to the run-line's task id (the TUI may see the line a beat
        before the backend registers, e.g. during the acceptance-test
        stage of a build; steer_live_run's own trace gate then answers
        honestly that the loop is not live yet).
        """
        live = _iv._live_run()
        task_id = (
            live["task_id"] if live else (self._run.task_id if self._run else None)
        )
        log_root = live["log_root"] if live else self.log_root
        if not task_id:
            # No live task at all (worker between runs): queue like
            # before — better one queued task than a lost instruction.
            self._queue.append(line)
            self.transcript(
                f"[vex.muted]queued (the run is still starting — "
                f"{len(self._queue)} waiting)[/]"
            )
            return
        from cli.intent import classify

        intent = classify(line)
        if intent.kind == "convo":
            self.transcript(f"[vex.muted]{escape(intent.reply)}[/]")
            return
        _iv.steer_live_run(
            line,
            task_id,
            Path(log_root),
            source="tui",
            say=self.transcript,
        )

    def _start_run(self, issue: str, mode: str = "agent_task") -> None:
        """Start one run in a worker thread (Task B: the UI never
        blocks; the run-line updates in place from the trace tail).
        The session dispatches TWO modes — question (read-only) and
        agent_task (the ONE live-repo loop); the fix/build/research
        workers stay as legacy entries so older callers/tests addressing
        them directly keep working."""
        self._dispatched_mode = mode
        verb = {
            "fix": "fixing in",
            "build": "building in",
            "question": "answering about",
            "research": "researching",
            "agent_task": "working in",
            "agent": "working in",
        }.get(mode, "working in")
        if mode in ("agent", "agent_task"):
            mode = "agent_task"
        repo_part = (
            f" [vex.accent]{Path(self.repo).name}[/]" if mode != "research" else ""
        )
        self.transcript(
            f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]{verb}[/]{repo_part}"
        )
        target = {
            "fix": self._fix_worker,
            "build": self._build_worker,
            "question": self._question_worker,
            "research": self._research_worker,
            "agent_task": self._agent_worker,
        }.get(mode, self._agent_worker)
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_turn, save_session

                append_turn(self.conversation, "user", issue)
                save_session(self.log_root, self.conversation)
            except Exception:
                pass
        self._worker_thread = threading.Thread(
            target=target, args=(issue,), daemon=True
        )
        self._worker_thread.start()

    def _agent_plan_preview(self, request: str) -> None:
        """Lightweight agent preview: steps/files transcript + confirm modal.

        Approve runs the agent with the plan as steering guidance (not a
        fixed contract); "edit" opens a steering prompt whose text steers
        the run; reject cancels. Never raises.
        """
        try:
            from harness.agent_loop import render_agent_plan

            plan = render_agent_plan(request, str(self.repo), {})
        except Exception:
            self._start_run(request, mode="agent_task")
            return
        steps = plan.get("steps", []) or []
        files = plan.get("files", []) or []
        self.transcript(
            "[vex.accent]agent plan[/] [vex.muted](approve → run, edit → steer)[/]"
        )
        for i, s in enumerate(steps, start=1):
            self.transcript(f"[vex.muted]{i}.[/] {escape(str(s))}")
        if files:
            self.transcript(f"[vex.muted]files: {escape(', '.join(files[:6]))}[/]")
        body = [Text(str(s)[:160]) for s in steps[:12]]
        try:
            self.push_screen(
                _ConfirmScreen("run this plan? [Y/n] (e=edit) ", body, "y"),
                lambda ans, _req=request: self._agent_plan_chosen(_req, ans),
            )
        except Exception:
            self._start_run(request, mode="agent_task")

    def _agent_plan_chosen(self, request: str, answer: Optional[str]) -> None:
        """Callback for the agent plan confirm modal."""
        try:
            ans = (answer or "y").strip().lower()
        except Exception:
            ans = "y"
        if ans in ("", "y", "yes", "run"):
            self._pending_agent_guidance = request
            self.transcript("[vex.ok]approved — starting the agent[/]")
            self._start_run(request, mode="agent_task")
            return
        if ans in ("e", "edit"):
            try:
                self.push_screen(
                    _PromptScreen("steer the plan (one line, empty cancels): ", []),
                    lambda edit, _req=request: self._agent_plan_edited(_req, edit),
                )
            except Exception:
                self.transcript("[vex.warn]cancelled — nothing ran[/]")
            return
        self.transcript("[vex.warn]cancelled — nothing ran[/]")

    def _agent_plan_edited(self, request: str, edit: Optional[str]) -> None:
        """Callback for the agent plan steering prompt."""
        try:
            text = (edit or "").strip()
        except Exception:
            text = ""
        if not text:
            self.transcript("[vex.warn]cancelled — nothing ran[/]")
            return
        guidance = request + "\n\nUser-steered plan: " + text
        self._pending_agent_guidance = guidance
        self.transcript("[vex.ok]steered — starting the agent[/]")
        self._start_run(request, mode="agent_task")

    def _start_resume(self, task_id: str) -> None:
        """Resume a previous run by id (REPL's _resume_task, threaded).

        Records the /resume line as a user turn first, so the resumed
        run keeps talking in the same conversation (not a restart)."""
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_turn, save_session

                append_turn(self.conversation, "user", f"/resume {task_id}")
                save_session(self.log_root, self.conversation)
            except Exception:
                pass
        self._dispatched_mode = "fix"
        self._worker_thread = threading.Thread(
            target=self._resume_worker, args=(task_id,), daemon=True
        )
        self._worker_thread.start()

    def _run_worker(self, issue: str) -> None:
        """Worker thread body: run one fix; every UI touch marshals via
        call_from_thread. Exceptions become transcript lines, never
        tracebacks (Task D discipline: explain, don't dump)."""
        self._worker_ident = threading.get_ident()
        try:
            info = self._execute_fix(issue)
        except KeyboardInterrupt:
            for ln in self._cap.last_lines:
                self._thread_log(ln)
            self._thread_log(
                "[vex.warn]interrupted — containers cleaned, checkpoints "
                "kept ([vex.accent]vex --continue[/][vex.warn] resumes)[/]"
            )
            self._finish_run()
            return
        except Exception as exc:  # explain, never a traceback
            from cli.errors import explain_exception

            try:
                _, lines = self._cap.capture(
                    explain_exception, exc, save_traceback=True
                )
            except Exception:
                lines = [Text(f"error: {type(exc).__name__}")]
            for ln in lines:
                self._thread_log(ln)
            self._finish_run()
            return
        if info is not None:
            self._safe_call(self._note_result, info)
        self._finish_run()

    # alias: the fix worker IS the generic worker (fix stays the
    # default dispatch target; the name reads better at the call site)
    _fix_worker = _run_worker

    def _mode_worker(self, mode: str, run_one: Callable[[], Any], issue: str) -> None:
        """Shared worker body for the non-fix modes (question/research/
        build): capture the backend's rich output into the transcript,
        fold the result into the session state, explain exceptions.
        `run_one` is a zero-arg closure — each mode's backend has its
        own signature (research takes repo last), so the caller binds
        it; the backends handle their own monitoring/registration and
        the TUI attaches the sidebar via the _ON_TASK_START hook they
        fire."""
        self._worker_ident = threading.get_ident()
        log = self._thread_log
        try:
            with self._prompt_patches():
                result, lines = self._cap.capture(run_one)
            for ln in lines:
                log(ln)
            if isinstance(result, dict):
                self._safe_call(self._note_result, result)
        except KeyboardInterrupt:
            for ln in self._cap.last_lines:
                log(ln)
            log("[vex.warn]interrupted[/]")
        except Exception as exc:  # explain, never a traceback
            from cli.errors import explain_exception

            try:
                _, lines = self._cap.capture(
                    explain_exception, exc, save_traceback=True
                )
            except Exception:
                lines = [Text(f"error: {type(exc).__name__}")]
            for ln in lines:
                log(ln)
        finally:
            self._finish_run()

    def _question_worker(self, question: str) -> None:
        """Worker body for question mode (read-only Q&A)."""

        def call():
            return _iv._run_one_question(
                question, self.repo, self.state, self.log_root, self.file_config or None
            )

        self._mode_worker("question", call, question)

    def _research_worker(self, question: str) -> None:
        """Worker body for research mode (FETCH-assisted investigation)."""

        def call():
            # research's backend takes (question, state, log_root,
            # file_config, repo) — repo LAST and optional
            return _iv._run_one_research(
                question, self.state, self.log_root, self.file_config or None, self.repo
            )

        self._mode_worker("research", call, question)

    def _build_worker(self, request: str) -> None:
        """Worker body for build mode (test-authored completion)."""

        def call():
            return _iv._run_one_build(
                request, self.repo, self.state, self.log_root, self.file_config or None
            )

        self._mode_worker("build", call, request)

    def _agent_worker(self, request: str) -> None:
        """Worker body for agent tasks (the ONE live-repo loop).

        Require-mode tools raise the SAME confirm modal pattern as the
        plan preview / fix approval (diff + command body, Allow once /
        Allow always / Reject); the REPL prompts per call instead.
        """

        def call():
            import inspect as _inspect

            self._agent_allow_always = False
            guidance = self._pending_agent_guidance
            self._pending_agent_guidance = None
            fn = _iv._run_one_agent
            try:
                params = _inspect.signature(fn).parameters
                accepts_kw = (
                    "approve_fn_override" in params
                    or "plan_guidance" in params
                    or any(
                        p.kind == _inspect.Parameter.VAR_KEYWORD
                        for p in params.values()
                    )
                )
            except (TypeError, ValueError):
                # Mocks/partials without an inspectable signature accept
                # anything — pass the full call.
                accepts_kw = True
            if accepts_kw:
                return fn(
                    request,
                    self.repo,
                    self.state,
                    self.log_root,
                    self.file_config or None,
                    approve_fn_override=self._agent_approve_fn,
                    plan_guidance=guidance,
                )
            # Legacy fake without the new kwargs (test scaffolding):
            # same run, default approval + no plan guidance.
            return fn(
                request, self.repo, self.state, self.log_root, self.file_config or None
            )

        self._mode_worker("agent_task", call, request)

    def _agent_approve_fn(self, tool: str, args: Dict[str, Any], preview: str) -> bool:
        """approve_fn for require-mode agent tools (TUI modal).

        Mirrors _ConfirmScreen (plan preview / fix approval): the modal
        body shows the diff/command, the prompt offers Allow once (y) /
        Allow always (a) / Reject (n, the safe default on Esc). An
        "always" answer latches for the rest of this run. VEX_NOTIFY is
        respected via ui.bell. Never raises (a broken prompt refuses).
        """
        try:
            if self._agent_allow_always:
                return True
            try:
                ui.bell("vex approval needed")
            except Exception:
                pass
            try:
                from harness.agent_loop import _approval_preview as _ap

                full_preview = _ap(str(self.repo), tool, args or {})
            except Exception:
                full_preview = preview or ""
            body = [
                Text(
                    f"{tool.upper()}: {(args or {}).get('path') or (args or {}).get('command') or (args or {}).get('url') or ''}"[
                        :160
                    ]
                )
            ]
            for ln in (full_preview or "").splitlines()[:14]:
                body.append(Text(ln[:200]))
            self._pending_prompt_body = body[:25]
            answer = self._prompt_modal(
                f"allow this {tool.upper()}? [y/n/a] (y=once, a=always, n=reject) "
            )
            if answer is None:
                return False
            ans = str(answer).strip().lower()
            if ans in ("a", "always"):
                self._agent_allow_always = True
                self._thread_log("[vex.ok]allowed always for this run[/]")
                return True
            if ans in ("y", "yes"):
                return True
            self._thread_log(f"[vex.warn]rejected {tool.upper()} — skipped[/]")
            return False
        except Exception:
            return False

    def _resume_worker(self, task_id: str) -> None:
        """Worker body for /resume (REPL's _resume_task, captured)."""
        self._worker_ident = threading.get_ident()
        log = self._thread_log
        try:
            with self._prompt_patches():
                result, lines = self._cap.capture(
                    _iv._resume_task, task_id, self.log_root, self.state
                )
            for ln in lines:
                log(ln)
            if isinstance(result, dict):
                self._safe_call(self._note_result, result)
        except KeyboardInterrupt:
            for ln in self._cap.last_lines:
                log(ln)
            log(
                "[vex.warn]interrupted — resumable via [vex.accent]vex --continue[/][/]"
            )
        except Exception as exc:
            from cli.errors import explain_exception

            log(f"[vex.error]resume of {escape(task_id)} failed:[/]")
            try:
                _, lines = self._cap.capture(explain_exception, exc)
            except Exception:
                lines = [Text(f"error: {type(exc).__name__}")]
            for ln in lines:
                log(ln)
        finally:
            self._finish_run()

    def _execute_fix(self, issue: str) -> Optional[Dict[str, Any]]:
        """Run _run_one_fix under prompt patches; capture its rich output
        for the transcript (replayed when the run finishes) and hook the
        task's trace into the run-line (via the _ON_TASK_START hook the
        backend fires when the task id exists).

        The backend's own live spinner is suppressed by state["quiet"]
        (the TUI run-line IS the live status); its result summary, diff
        and rationale land in the transcript via capture.
        """
        original_quiet = self.state.get("quiet")
        self.state["quiet"] = True
        lines: List[Text] = []
        result: Any = None
        try:
            with self._prompt_patches():
                result, lines = self._cap.capture(
                    _iv._run_one_fix,
                    issue,
                    self.repo,
                    self.state,
                    self.log_root,
                    self.file_config or None,
                )
        finally:
            self.state["quiet"] = original_quiet
            # on KeyboardInterrupt the capture re-raises AFTER reassembly,
            # so lines still holds the partial output — but a failure
            # before capture started leaves it empty (never unbound).
            for ln in lines or self._cap.last_lines:
                self._thread_log(ln)
        return result or None

    def _prompt_patches(self):
        """Context manager: the backend's input()/print() calls (plan
        preview, approval flow) become modal screens / transcript lines.

        Patched GLOBALLY for the duration of the backend call: the
        backend spawns its OWN helper threads (the plan-preview watcher)
        that also call input()/print() — ident-gating would miss them
        and a stray prompt would print over the textual render. During
        a TUI run nothing else legitimately reads stdin (textual owns
        the keyboard through its driver, not builtins.input), so the
        patch is scoped by TIME, not thread: enter on the worker before
        the run, restore in the worker's finally. Re-entrant safe (a
        nested patch would restore to the patched pair — but the TUI
        never nests these contexts; one run at a time by design)."""
        import builtins
        import contextlib

        app = self
        real_input = builtins.input
        real_print = builtins.print

        @contextlib.contextmanager
        def _ctx():
            def _patched_input(prompt: str = "") -> str:
                if app._shutting_down:
                    # a leaked patch (worker outliving the app) must
                    # never loop into a dead modal: behave like closed
                    # stdin, which the backend treats as cancel.
                    raise EOFError("app shutting down")
                answer = app._prompt_modal(str(prompt))
                if answer is None:
                    raise EOFError("cancelled")
                return answer

            def _patched_print(*args, sep=" ", end="\n", **kw):
                if app._shutting_down:
                    return
                text = sep.join(str(a) for a in args)
                app._thread_log(text)

            builtins.input = _patched_input  # type: ignore[assignment]
            builtins.print = _patched_print  # type: ignore[assignment]
            try:
                yield
            finally:
                builtins.input = real_input  # type: ignore[assignment]
                builtins.print = real_print  # type: ignore[assignment]

        return _ctx()

    def _prompt_modal(self, prompt: str) -> Optional[str]:
        """Blocking input() on a backend thread -> modal on the UI thread.

        The modal's body shows what the backend just printed (plan
        steps / the diff) — snapshotted from the live capture buffer as
        styled Text. y/n confirm prompts get the confirm screen (empty
        = default, Esc = 'n'); free-text prompts get Esc-cancel (the
        backend then sees EOFError, exactly like a closed stdin).
        Returns None only when the app is shutting down or the push
        failed (the backend sees EOFError — safe, run-gating prompts
        default to the safe answer).
        """
        if self._shutting_down:
            return None
        done = threading.Event()
        answer: Dict[str, Any] = {"value": None}
        is_confirm = "y/n" in prompt.lower().replace(" ", "")
        # the modal body: lines the backend declared via _PROMPT_BODY
        # (plan steps / diff), else whatever the live capture holds.
        body = getattr(self, "_pending_prompt_body", None) or []
        self._pending_prompt_body = None

        def _push() -> None:
            def _done(result: Optional[str]) -> None:
                answer["value"] = result
                done.set()

            try:
                if is_confirm:
                    self._set_status(_STATUS_WAITING)
                    self.push_screen(
                        _ConfirmScreen(
                            prompt,
                            list(body),
                            _confirm_default(prompt),
                        ),
                        _done,
                    )
                else:
                    self.push_screen(
                        _PromptScreen(prompt, list(body)),
                        _done,
                    )
            except Exception:
                done.set()  # app dying: cancel the prompt

        try:
            self.call_from_thread(_push)
        except Exception:
            return None
        done.wait(timeout=86400.0)
        if is_confirm:
            self._safe_call(self._set_status, _STATUS_RUNNING)
        return answer["value"]

    def _note_result(self, info: Dict[str, Any]) -> None:
        """Fold a finished run's summary into the session state (the
        transcript summary itself comes from the captured backend output).

        Also records the assistant turn in the persistent conversation
        (memory-first ingestion itself happens in record_session, which
        the backends call — this is the transcript side of that)."""
        self.last.update(info)
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_turn, save_session

                append_turn(
                    self.conversation,
                    "assistant",
                    str(info.get("answer") or info.get("status") or ""),
                    task_id=info.get("task_id"),
                )
                save_session(self.log_root, self.conversation)
            except Exception:
                pass

    def _finish_run(self) -> None:
        """Worker-thread end-of-run: stop the tail (its final drain
        catches the closing events), hide the run-line, keep the
        sidebar a beat for the final todo snapshot, render the
        completion card, restore the header, and drain the queue (all
        on the UI thread)."""
        if self._run_stop is not None:
            self._run_stop.set()
            self._run_stop = None
        tail = getattr(self, "_tail_thread", None)
        if tail is not None and tail.is_alive():
            tail.join(timeout=3.0)  # the final drain is bounded (~2s)
        run = self._run
        task_id = run.task_id if run is not None else None
        mode = run.mode if run is not None else "fix"
        self._run = None
        self._dispatched_mode = None  # the label was consumed at start
        self._safe_call(self._hide_runline)
        if task_id is not None:
            # final sidebar snapshot (all steps resolved), then the
            # completion card (Task C) — re-derived from the records
            self._safe_call(self._refresh_machine_state, task_id)
            self._safe_call(self._render_side, run)
            self._safe_call(self._render_card, task_id, mode)
            self._safe_call(self._teardown_side, 2.0)
        # Task F (polish round): the completion bell, once per run, from
        # the ONE seam every mode's worker passes through. The REPL's
        # own notify_done defers to the TUI hook being set, so there is
        # never a double ring.
        try:
            ui.bell(f"vex {mode} finished")
        except Exception:
            pass
        self._safe_call(self._after_run)

    def _render_card(self, task_id: str, mode: str = "fix") -> None:
        """The completion summary card (Task C): one polished block at
        the end of the transcript pulling together what the backend
        otherwise scatters across separate prints — status, attempts,
        model calls, time, cost, files changed, the verification
        verdict, and the git-native branch/commit. Every number is
        re-derived from the run's own records at render time (runview
        .read_run_facts — trace.jsonl + state.json), so the card can
        never drift from what actually happened."""
        try:
            facts = _rv.read_run_facts(self.log_root / task_id)
        except Exception:
            return  # a view must never take the session down
        rows = _rv.card_lines(facts, mode=mode)
        top = "┌" if ui._enc_ok("┌") else "+"
        bot = "└" if ui._enc_ok("└") else "+"
        side = "│" if ui._enc_ok("│") else "|"
        self.transcript("")
        self.transcript(
            f"[vex.muted]{top}{'─' if ui._enc_ok('─') else '-'} summary {task_id}[/]"
        )
        for row in rows:
            self.transcript(f"[vex.muted]{side}[/] {row}")
        self.transcript(
            f"[vex.muted]{bot}{'─' if ui._enc_ok('─') else '-'}{'─' if ui._enc_ok('─') else '-'}[/]"
        )

    def _teardown_side(self, delay_s: float = 0.0) -> None:
        """Collapse the sidebar once the run's final snapshot has been
        shown (textual timers handle the delay; set_timer takes no
        call args, so the delayed pass closes over nothing)."""
        if self._run is not None:
            return  # a NEW run started meanwhile — keep it live
        try:
            if delay_s <= 0:
                self.query_one("#vex-side", Vertical).styles.display = "none"
            else:
                self.set_timer(delay_s, lambda: self._teardown_side())
        except Exception:
            pass

    def _after_run(self) -> None:
        """UI thread: post-run bookkeeping + queue drain."""
        self._set_status(_STATUS_IDLE)
        if self._queue and not (self._worker_thread and self._worker_thread.is_alive()):
            nxt = self._queue.pop(0)
            self.transcript(
                f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]next queued[/]"
            )
            self._handle_line(nxt)

    # -- live run-line driving (Task B) -----------------------------------

    def _on_task_start_hook(self, task_id: str) -> None:
        """The _ON_TASK_START hook (called on the worker thread by
        cli.interactive._execute_task/_run_one_build): marshal the
        run-line start onto the UI thread."""
        self._safe_call(self.begin_live_run, task_id)

    def begin_live_run(self, task_id: str) -> _RunState:
        """Spin the run state + tail + approval-watch threads for a live
        run; the run-line widget becomes visible and is updated in place
        per trace event, and the sidebar (todo + status panel) opens.
        Must run on the UI thread (called via the _ON_TASK_START hook).
        The trace's own task_start event corrects the mode for modes the
        harness labels itself (question/research/build)."""
        run = _RunState(task_id, mode=self._dispatched_mode or "fix")
        self._machine_state = None
        self._run = run
        stop = threading.Event()
        self._run_stop = stop
        tail = threading.Thread(
            target=_tail_trace,
            args=(task_id, self.log_root, run, stop, self._on_trace_event),
            daemon=True,
        )
        self._tail_thread = tail
        tail.start()
        threading.Thread(
            target=self._approval_watch,
            args=(task_id, stop),
            daemon=True,
        ).start()
        self._refresh_machine_state(task_id)
        self._render_run(run)
        self._render_side(run)
        self._set_status(_STATUS_RUNNING)
        return run

    def _on_trace_event(self, run: _RunState, entries: List[_tl.FeedEntry]) -> None:
        """Tail-thread callback -> marshalled onto the UI thread; repaints
        the SAME run-line widget (never appends a new line) and renders
        the batch's feed entries live (Tasks A/B: the moment each event
        happens, not batched at the end). This round also refreshes the
        sidebar todo (the checkmark lands WITH the event) and, on
        lifecycle-ish events, re-reads the state-machine trail.

        Gated on the FEED toggle, not state["quiet"]: the worker sets
        quiet=True for every run (it silences the BACKEND's spinner —
        the TUI run-line replaces it), so quiet must never silence the
        feed; state["feed"] is the user's own /quiet toggle."""
        kinds = {e.category for e in entries}
        if kinds & {"lifecycle", "verify", "diff"} or run.todo.steps:
            # state first, THEN render: the batch's repaint must show
            # the transition that just landed, not the previous one
            self._safe_call(self._refresh_machine_state, run.task_id)
        self._safe_call(self._render_run, run)
        self._safe_call(self._render_side, run)
        if not self.state.get("feed", True):
            return  # user-silenced feed; the run-line stays
        self._safe_call(self._render_feed_entries, run, entries)

    # -- live feed rendering (Tasks A/B/C/D) -------------------------------

    # -- feed rendering delegates to the module-level shared renderers
    # (FEED_GLYPHS / FEED_STYLES / feed_style / feed_line) so the live
    # transcript, /trace and the scrollable /feed browser all agree.

    @staticmethod
    def _feed_style(entry: "_tl.FeedEntry") -> str:
        return feed_style(entry)

    def _render_feed_entries(
        self, run: _RunState, entries: List[_tl.FeedEntry]
    ) -> None:
        """Render feed lines live into the transcript (Task B) and, right
        after an edit-shaped entry, the inline diff of pristine vs work
        (Task C — small, colored, computed from the SAME trees the
        harness diffs at completion)."""
        task_dir = self.log_root / run.task_id
        saw_edit = False
        for ent in entries:
            self.transcript(feed_line(ent))
            if ent.category == "diff":
                saw_edit = True
        if saw_edit:
            self._render_live_diff(task_dir)

    def _render_live_diff(self, task_dir: Path) -> None:
        """Inline diff preview (Task C of the feed round; syntax-
        highlighted per Task B of the polish round): pristine vs work
        right NOW, the same comparison the harness's final diff runs —
        rendered as small LANGUAGE-AWARE colored hunk lines under the
        feed (pygments tokens over the +/-/@@ roles, via the shared
        `ui.diff_render_lines`), capped. Nothing to diff (early run,
        identical trees) renders nothing."""
        try:
            lines = _tl.live_diff(
                task_dir / "pristine", task_dir / "work", max_lines=14
            )
        except Exception:
            return
        if not lines:
            return
        # a header so the inline diff reads as the edit's effect
        # (cp1252-safe box corners via the encoding probe)
        top = "┌" if ui._enc_ok("┌") else "+"
        bot = "└" if ui._enc_ok("└") else "+"
        self.transcript(f"[vex.muted]{top} diff (pristine → work so far)[/]")
        try:
            rendered = ui.diff_render_lines(lines)
        except Exception:
            rendered = [Text(t) for t, _k in lines]
        for text in rendered:
            self.transcript(text)
        self.transcript(f"[vex.muted]{bot} end diff[/]")

    def _approval_watch(self, task_id: str, stop: threading.Event) -> None:
        """While a task is live, surface a parked approval request as the
        confirm modal (with the diff) — the TUI-native equivalent of the
        REPL's watch_for_approvals, scoped to THIS session's task. One
        prompt per request; /approve //reject stay available as manual
        paths (first decider wins, the other finds nothing pending)."""
        gate = self.log_root / f"{task_id}.runtime" / "approval"
        try:
            from runtime import approval as approval_mod
        except ImportError:
            return
        while not stop.is_set() and not self._shutting_down:
            if task_id in self._approval_handled:
                return
            try:
                req = approval_mod.pending_request(str(gate))
            except Exception:
                req = None
            if req is not None:
                self._approval_handled.add(task_id)
                body = _diff_body(req.get("diff") or "")
                if req.get("issue_text"):
                    body.insert(0, Text(f"issue: {req['issue_text'][:300]}"))
                answer = self._prompt_modal("approve this fix? [y/N] ")
                if answer is None:
                    return  # app shutting down; /approve stays available
                approve = answer.strip().lower() in ("y", "yes")
                try:
                    approval_mod.decide(str(gate), approve=approve)
                except Exception:
                    return
                self._thread_log(
                    f"[vex.{'ok' if approve else 'error'}]"
                    f"{'approved' if approve else 'rejected'}[/] — the run "
                    f"continues {'with' if approve else 'without'} the fix"
                )
                return
            stop.wait(1.0)

    # -- keyboard actions ---------------------------------------------------

    def action_cancel_or_quit(self) -> None:
        """Ctrl+C: cancel the in-flight run (checkpoints kept) if any,
        else quit (same semantics as the REPL's Ctrl+C)."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self.transcript(
                "[vex.warn]cancel requested — sending Ctrl+C semantics "
                "to the running task (checkpoints kept)[/]"
            )
            self._interrupt_worker()
            return
        self.exit()

    def action_quit_app(self) -> None:
        self.exit()

    def action_input_history(self) -> None:
        """Ctrl+R: searchable input-history browser over the persistent
        conversation's raw lines (newest first); choosing one recalls it
        into the input box. Empty history renders one honest line."""
        hist: List[str] = []
        try:
            if isinstance(self.conversation, dict):
                hist = [str(h) for h in (self.conversation.get("history") or [])]
        except Exception:
            hist = []
        if not hist:
            self.transcript("[vex.muted]no input history yet[/]")
            return
        self.push_screen(_HistoryScreen(list(reversed(hist))), self._history_chosen)

    def _history_chosen(self, line_text: Optional[str]) -> None:
        """History callback: recall the chosen line into the input."""
        if not line_text:
            return
        try:
            inp = self.query_one("#vex-input", Input)
            inp.value = str(line_text)
            inp.focus()
        except Exception:
            pass

    def action_complete_mention(self) -> None:
        """Ctrl+Space: complete the @path fragment under the cursor from
        the repo's file list (fuzzy, same matcher as the palette). Only
        acts when an @fragment precedes the cursor; otherwise a no-op
        (never steals the key from another widget's use)."""
        try:
            inp = self.query_one("#vex-input", Input)
        except Exception:
            return
        try:
            value = inp.value or ""
            cursor = getattr(inp, "cursor_position", len(value)) or 0
        except Exception:
            return
        try:
            head = value[: max(0, min(cursor, len(value)))]
            match = re.search(r"@([A-Za-z0-9_./\\-]*)$", head)
            if match is None:
                return
            frag = match.group(1)
            if not frag:
                return
            files = self._palette_files()
            ranked = _fz.filter_and_rank(
                [{"label": f, "hint": "file"} for f in files],
                frag,
                lambda e: str(e.get("label") or ""),
                limit=1,
            )
            if not ranked:
                return
            best = str(ranked[0].get("label") or "")
            if not best:
                return
            start = len(head) - len(frag)
            new_value = value[:start] + best + value[len(head) :]
            inp.value = new_value
            try:
                inp.cursor_position = start + len(best)
            except Exception:
                pass
        except Exception:
            pass

    def action_scroll_history(self, direction: int = -1) -> None:
        """shift+pageup/pagedown (+ arrows): move the transcript viewport
        through the history. Scrolling away from the bottom pauses the
        live auto-follow (see _TranscriptLog); scrolling back to the
        bottom resumes it."""
        try:
            log = self.query_one("#vex-body", RichLog)
            if direction < 0:
                log.scroll_page_up(animate=False)
            else:
                log.scroll_page_down(animate=False)
        except Exception:
            pass

    def action_scroll_history_end(self) -> None:
        """shift+end: jump to the live tail and resume auto-follow."""
        try:
            log = self.query_one("#vex-body", RichLog)
            log.auto_scroll = True
            log.scroll_end(animate=False)
        except Exception:
            pass

    def on_key(self, event: events.Key) -> None:
        """Up/Down with the MAIN input focused browses the persistent
        session's input history (newest-first on first Up); the browse
        cursor resets on every submit. Only the #vex-input line is
        covered — modal filter boxes and the palette keep their own
        keys (checked by widget id). Never raises."""
        try:
            if event.key not in ("up", "down"):
                return
            focused = self.focused
            if focused is None or getattr(focused, "id", "") != "vex-input":
                return
            hist: List[str] = []
            if isinstance(self.conversation, dict):
                hist = [str(h) for h in (self.conversation.get("history") or [])]
            if not hist:
                return
            idx = self._hist_idx
            if idx is None:
                idx = len(hist) - 1 if event.key == "up" else 0
            else:
                idx += -1 if event.key == "up" else 1
                idx = max(0, min(len(hist) - 1, idx))
            self._hist_idx = idx
            inp = self.query_one("#vex-input", Input)
            inp.value = hist[idx]
            try:
                inp.cursor_position = len(hist[idx])
            except Exception:
                pass
            event.stop()
            event.prevent_default()
        except Exception:
            pass

    def action_clear_input(self) -> None:
        self.query_one("#vex-input", Input).value = ""

    #: Built-in palette commands: (label, description, run-on-choose?).
    #: run=True fires the command immediately (no argument needed);
    #: run=False prefills the input so the user can finish the line
    #: (an id for /resume, text for /steer).
    _PALETTE_COMMANDS: ClassVar[List[Tuple[str, str, bool]]] = [
        ("/help", "what you can say", True),
        ("/status", "last task's structured state", True),
        ("/diff", "re-render the last fix's diff (syntax-highlighted)", True),
        ("/review", "last diff + rationale together", True),
        ("/plan", "preview steps before edits (plan mode)", False),
        ("/compact", "compact the conversation (recall-backed summary)", True),
        ("/copy-diff", "copy the last diff to the clipboard", True),
        ("/sessions", "search previous sessions (filterable)", True),
        ("/feed", "scrollable, searchable trace feed", True),
        ("/trace", "trace feed index (/trace <n> expands one)", False),
        ("/resume", "continue an interrupted task", False),
        ("/steer", "steer the running task", False),
        ("/approve", "approve a pending request", True),
        ("/reject", "reject a pending request", True),
        ("/cancel", "stop the current run (checkpoints kept)", True),
        ("/quiet", "toggle live feed + spinner", True),
        ("/model", "show the effective model (+ source tier)", True),
        ("/init", "scaffold .vex/ in the session repo", True),
        ("/login", "configure a model (wizard)", True),
        ("/logout", "remove the stored api_key", True),
        ("/mcp", "list configured MCP servers (+ tools)", True),
        ("/skills", "list discovered skills + origins", True),
        ("/cost", "session spend from trace usage-sum", True),
        ("/undo", "alias of /diff undo (agent sessions)", False),
        ("/clear", "fresh conversation (old kept)", True),
    ]

    def action_command_palette(self) -> None:
        """ctrl+p — the fuzzy command palette (Task A of the polish
        round). Commands, custom commands, recent sessions and repo
        files in ONE ranked, filter-as-you-type search box — the VS
        Code feel, not a static menu."""
        self.push_screen(_PaletteScreen(self._palette_entries()), self._palette_chosen)

    def _palette_entries(self) -> List[Dict[str, Any]]:
        """Everything the palette can search: built-ins + custom
        commands, recent sessions, and the repo's files. Each entry is
        {kind, label, hint, value, run}; the hint carries the extra
        search surface (a session's status/repo/date/issue; a file's
        role), and `label` stays the short primary token."""
        entries: List[Dict[str, Any]] = []
        for label, hint, run in self._PALETTE_COMMANDS:
            entries.append(
                {
                    "kind": "command",
                    "label": label,
                    "hint": hint,
                    "value": label,
                    "run": run,
                }
            )
        try:
            from cli import commands as commands_mod

            for name in commands_mod.command_names(self.state.get("repo")):
                entries.append(
                    {
                        "kind": "command",
                        "label": f"/{name}",
                        "hint": "custom command",
                        "value": f"/{name}",
                        "run": False,
                    }
                )
        except Exception:
            pass
        try:
            for s in _iv.list_sessions(self.log_root, limit=60):
                tid = str(s.get("task_id") or "")
                if not tid:
                    continue
                bits = [str(s.get("status") or "?")]
                if s.get("resumable"):
                    bits.append("resumable")
                repo = str(s.get("repo") or "")
                if repo:
                    bits.append(Path(repo).name or repo)
                ts = s.get("ts")
                try:
                    bits.append(
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
                    )
                except (TypeError, ValueError):
                    pass
                issue = str(s.get("issue") or "").replace("\n", " ").strip()
                if issue:
                    bits.append(issue[:48])
                entries.append(
                    {
                        "kind": "session",
                        "label": tid,
                        "hint": f"session · {' · '.join(bits)}",
                        "value": f"/resume {tid}",
                        "run": True,
                    }
                )
        except Exception:
            pass
        for rel in self._palette_files():
            entries.append(
                {
                    "kind": "file",
                    "label": rel,
                    "hint": "file · insert path",
                    "value": rel,
                    "run": False,
                }
            )
        return entries

    def _palette_files(self) -> List[str]:
        """Repo files for the palette (cached per repo — see
        `scan_repo_files`). A failure to read the repo yields []."""
        repo = str(self.repo)
        if self._files_cache is not None and self._files_cache[0] == repo:
            return self._files_cache[1]
        try:
            files = scan_repo_files(self.repo)
        except Exception:
            files = []
        self._files_cache = (repo, files)
        return files

    def _palette_chosen(self, entry: Optional[Dict[str, Any]]) -> None:
        """Palette callback: a chosen FILE inserts its path (you finish
        the sentence); a chosen COMMAND runs it when it takes no
        argument, otherwise prefills it — including session entries,
        whose value is the exact `/resume <task_id>` line."""
        if not entry:
            return
        inp = self.query_one("#vex-input", Input)
        if entry.get("kind") == "file":
            inp.value = f"{entry.get('value', '')} "
            inp.focus()
            return
        cmd = str(entry.get("value") or "")
        if not cmd:
            return
        if entry.get("run"):
            self.transcript(
                f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] {escape(cmd)}"
            )
            self._handle_line(cmd)
        else:
            inp.value = cmd + " "
            inp.focus()

    def _interrupt_worker(self) -> None:
        """Deliver Ctrl+C semantics to the in-flight run (also the
        _CANCEL_RUN hook — see cli.interactive._interrupt_main).

        The REPL raised SIGINT at its MAIN thread (its loop was the
        blocker). In the TUI the run blocks a WORKER thread, so an async
        KeyboardInterrupt is injected there (ctypes
        PyThreadState_SetAsyncExc — probe-verified; the exception fires
        at the worker's next bytecode boundary, which run_task's
        except-KeyboardInterrupt path handles: containers stop,
        checkpoints stay, the task is recorded resumable).

        IDENT-SAFETY: Python REUSES thread idents. Injecting at a bare
        ident can hit an UNRELATED recycled thread (seen live: a test's
        dead-worker ident got reused by a later watcher thread — the KI
        killed it silently and its modal never appeared). The thread
        object is checked alive with a MATCHING ident right before the
        injection, closing the recycle window to near-zero.
        Best-effort: on failure the run is left to finish normally (the
        user was told what was attempted).
        """
        wt = self._worker_thread
        if wt is None or not wt.is_alive():
            return
        ident = wt.ident
        if ident is None or ident != self._worker_ident:
            return
        import ctypes

        try:
            kn = ctypes.pythonapi.PyThreadState_SetAsyncExc
            kn.argtypes = [ctypes.c_long, ctypes.py_object]
            kn.restype = ctypes.c_int
            kn(ident, KeyboardInterrupt)
        except Exception:
            pass


def _diff_body(diff: str) -> List[Text]:
    """A request's diff as syntax-highlighted rich Text lines for the
    approval modal body (Task B of the polish round — the same
    language-aware renderer as the inline preview and /diff; falls back
    to the flat role colors if the highlighter ever fails)."""
    try:
        return ui.diff_render_lines(diff or "")
    except Exception:
        out: List[Text] = []
        for line in (diff or "").splitlines():
            if line.startswith(("+++", "---")):
                out.append(Text(line, style=ui.ACCENT_GLOW))
            elif line.startswith("@@"):
                out.append(Text(line, style=ui.TEXT_SECONDARY))
            elif line.startswith("+"):
                out.append(Text(line, style=ui.SUCCESS))
            elif line.startswith("-"):
                out.append(Text(line, style=ui.ERROR))
            else:
                out.append(Text(line))
        return out


# ---------------------------------------------------------------------------
# Searchable list browser — the shared chrome for /sessions and /feed
# (interaction-polish round, Task C: history you can scroll AND search)
# ---------------------------------------------------------------------------


class _SearchListScreen(ModalScreen[Any]):
    """A searchable, scrollable list modal: filter input on top, a real
    scrollable OptionList under it (mouse wheel + arrows), enter picks
    the highlighted row, esc closes. Subclasses just implement
    `rows(query)` returning (Option-prompt-Text, payload) pairs —
    filtering happens there so /sessions can use its token grammar and
    /feed its substring/fuzzy search without this class caring.

    Total by contract: a rows() implementation that raises keeps the
    previous list (never an empty screen mid-session)."""

    CSS = """
    _SearchListScreen {
        align: center middle;
    }
    #sls-box {
        width: 90%;
        max-width: 120;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: round #E8114A;
    }
    #sls-title {
        color: #E8114A;
        text-style: bold;
    }
    #sls-input {
        border: round #E8114A;
        margin-bottom: 1;
    }
    #sls-list {
        height: 1fr;
        background: $surface;
    }
    #sls-hint {
        color: #8A8A8A; /* text-secondary (design token) */
        margin-top: 1;
    }
    """

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._payloads: List[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="sls-box"):
            yield Static(escape(self._title), id="sls-title")
            yield Input(placeholder=self._placeholder, id="sls-input")
            yield OptionList(id="sls-list")
            yield Static(
                "type to filter · ↑/↓ scroll · enter open · esc close", id="sls-hint"
            )

    def on_mount(self) -> None:
        self._refilter("")
        self.query_one("#sls-input", Input).focus()

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        """(prompt, payload) pairs for the current filter. Subclass."""
        raise NotImplementedError

    def _refilter(self, query: str) -> None:
        try:
            pairs = self.rows(query)
        except Exception:
            return
        lst = self.query_one("#sls-list", OptionList)
        lst.clear_options()
        self._payloads = [p for _prompt, p in pairs]
        lst.add_options([Option(pr) for pr, _p in pairs])
        try:
            if self._payloads:
                lst.highlighted = 0
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""
        self._choose()

    def _choose(self) -> None:
        if not self._payloads:
            self.dismiss(None)
            return
        try:
            idx = self.query_one("#sls-list", OptionList).highlighted
        except Exception:
            idx = 0
        if idx is None:
            idx = 0
        self.dismiss(self._payloads[min(idx, len(self._payloads) - 1)])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return
        if event.key in ("up", "down", "pageup", "pagedown", "home", "end"):
            event.stop()
            event.prevent_default()
            lst = self.query_one("#sls-list", OptionList)
            action = {
                "up": "action_cursor_up",
                "pageup": "action_page_up",
                "home": "action_first",
                "down": "action_cursor_down",
                "pagedown": "action_page_down",
                "end": "action_last",
            }.get(event.key)
            if action and hasattr(lst, action):
                getattr(lst, action)()


def feed_line_text(entry: "_tl.FeedEntry") -> Text:
    """The feed entry as a styled rich Text (the OptionList prompt
    form of `feed_line` — same reasoning/action distinction, Task E,
    but for widgets that take a Visual instead of markup)."""
    glyph = FEED_GLYPHS.get(entry.category, "*")
    style = feed_style(entry)
    out = Text()
    out.append(glyph, style=style)
    out.append(f" {entry.index:>2} ", style=ui.TEXT_SECONDARY)
    out.append(entry.summary, style=style)
    return out


class _FeedBrowserScreen(_SearchListScreen):
    """/feed — the full trace feed of the current/last run: scrollable
    back through every action, filter as you type (plain substring, plus
    the category words like 'tool' or 'reason'), enter expands the
    selected entry in the trace-detail modal. Task C's 'scrollable and
    searchable history' for the feed; read-only over the same data the
    live transcript showed (Task E discipline — no second copy)."""

    def __init__(self, entries: List["_tl.FeedEntry"], query: str = "") -> None:
        super().__init__(
            f"trace feed — {len(entries)} entries",
            "filter summaries / categories (e.g. tool, verify, diff…)",
        )
        self._entries = list(entries)
        self._initial = query

    def on_mount(self) -> None:
        super().on_mount()
        if self._initial:
            self.query_one("#sls-input", Input).value = self._initial
            self._refilter(self._initial)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        q = (query or "").strip().lower()
        out: List[Tuple[Any, Any]] = []
        for ent in self._entries:
            if q:
                hay = f"{ent.summary} {ent.category} {ent.detail}".lower()
                if q not in hay:
                    continue
            out.append((feed_line_text(ent), ent))
        return out

    def _choose(self) -> None:
        entry = None
        if self._payloads:
            try:
                idx = self.query_one("#sls-list", OptionList).highlighted
            except Exception:
                idx = 0
            entry = self._payloads[
                0 if idx is None else min(idx, len(self._payloads) - 1)
            ]
        if isinstance(entry, _tl.FeedEntry):
            # expand WITHOUT closing: the browser is the history, the
            # detail modal is the drill-down on top of it
            title = f"trace {entry.index} — {entry.detail_title or entry.category}"
            body: List[Text] = [
                Text(entry.summary, style=f"bold {ui.ACCENT_TEXT}"),
                Text(""),
            ]
            detail = (entry.detail or "").strip() or "(no detail recorded)"
            for ln in detail.splitlines()[:400]:
                body.append(Text(ln))
            try:
                self.app.push_screen(_TraceDetailScreen(title, body))
            except Exception:
                pass


class _SessionsScreen(_SearchListScreen):
    """/sessions — the searchable session browser (Task C): filter by
    status / repo / date / resumable via the token grammar in
    cli.interactive.session_matches ('status:failed repo:auth
    since:2026-09-01 resumable bug'), free text over id/issue/repo.
    Enter dismisses with the chosen task_id → the app resumes it."""

    def __init__(self, sessions: List[Dict[str, Any]], query: str = "") -> None:
        super().__init__(
            f"sessions — {len(sessions)} recorded",
            "status:failed · repo:name · since:2026-09-01 · resumable · free text",
        )
        self._sessions = list(sessions)
        self._initial = query

    def on_mount(self) -> None:
        super().on_mount()
        if self._initial:
            self.query_one("#sls-input", Input).value = self._initial
            self._refilter(self._initial)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        out: List[Tuple[Any, Any]] = []
        for s in _iv.filter_sessions(self._sessions, query):
            mark = "R" if s.get("resumable") else " "
            tid = str(s.get("task_id") or "?")
            try:
                when = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(float(s.get("ts") or 0))
                )
            except (TypeError, ValueError):
                when = "?"
            status = str(s.get("status") or "?")
            repo = str(s.get("repo") or "")
            issue = str(s.get("issue") or "").replace("\n", " ").strip()
            prompt = Text()
            prompt.append(
                f"{mark} ",
                style=ui.ACCENT_TEXT if s.get("resumable") else ui.TEXT_SECONDARY,
            )
            prompt.append(f"{tid}  ", style=ui.TEXT_PRIMARY)
            prompt.append(
                status, style=ui.SUCCESS if status == "success" else ui.TEXT_SECONDARY
            )
            prompt.append(
                f"  {when}  {Path(repo).name or repo}", style=ui.TEXT_SECONDARY
            )
            if issue:
                prompt.append(f"  {issue[:48]}", style=ui.TEXT_SECONDARY)
            out.append((prompt, tid))
        return out


class _HistoryScreen(_SearchListScreen):
    """Ctrl+R — the persistent conversation's input history: newest
    first, substring-filtered as you type, enter recalls the line into
    the input box (the callback receives the raw text)."""

    def __init__(self, lines: List[str]) -> None:
        super().__init__(
            f"input history — {len(lines)} lines",
            "type to filter history · enter recalls",
        )
        self._lines = [str(ln) for ln in lines]

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        q = (query or "").strip().lower()
        out: List[Tuple[Any, Any]] = []
        for ln in self._lines:
            if q and q not in ln.lower():
                continue
            prompt = Text(ln[:100], style=ui.TEXT_PRIMARY)
            out.append((prompt, ln))
        return out


class _TraceDetailScreen(ModalScreen[None]):
    """Task D: one feed entry's full detail (raw command + output /
    the model's whole reply / the event payload) — scrollable, esc to
    close. The DEFAULT view stays one line per action; this is the
    on-demand expansion, mirroring Claude Code's collapsed tool calls."""

    CSS = """
    _TraceDetailScreen {
        align: center middle;
    }
    #trace-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round #E8114A;
    }
    #trace-title {
        color: #E8114A;
        text-style: bold;
        margin-bottom: 1;
    }
    #trace-body {
        height: auto;
        max-height: 24;
        margin-bottom: 1;
    }
    #trace-hint {
        color: #8A8A8A; /* text-secondary (design token), not a random grey */
    }
    """

    def __init__(self, title: str, body_lines: List[Text]) -> None:
        super().__init__()
        self._title = title
        self._body = body_lines

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-box"):
            yield Static(escape(self._title), id="trace-title")
            yield RichLog(id="trace-body", markup=False, wrap=False)
            yield Static("scroll for more · esc close", id="trace-hint")

    def on_mount(self) -> None:
        body = self.query_one("#trace-body", RichLog)
        for ln in self._body:
            body.write(ln)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            try:
                self.dismiss(None)
            except Exception:
                pass


class _PaletteScreen(ModalScreen[Optional[Dict[str, Any]]]):
    """ctrl+p — a genuinely fuzzy command palette (Task A). One search
    box over commands + custom commands + recent sessions + repo files;
    typing re-ranks with `cli.fuzzy` subsequence scoring (so "iffil"
    finds cli/ui.py, "suc" finds a success session), results scroll in a
    real `OptionList` (up/down, enter), and the kind is color-tagged so
    you see what you're choosing. It is a search, not a menu: thousands
    of repo files and dozens of sessions rank down to a usable list."""

    CSS = """
    _PaletteScreen {
        align: center middle;
    }
    #palette-box {
        width: 78;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        padding: 1 1;
        background: $surface;
        border: round #E8114A;
    }
    #palette-input {
        border: round #E8114A;
        margin-bottom: 1;
    }
    #palette-list {
        height: auto;
        max-height: 16;
        background: $surface;
    }
    #palette-hint {
        color: #8A8A8A; /* text-secondary (design token), not a random grey */
        margin-top: 1;
    }
    """

    _KIND_STYLE: ClassVar[Dict[str, str]] = {
        "command": ui.TEXT_PRIMARY,
        "session": ui.ACCENT_TEXT,
        "file": ui.TEXT_SECONDARY,
    }
    _MAX_RESULTS = 200

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._entries = entries
        self._visible: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(
                placeholder="fuzzy search commands · sessions · files",
                id="palette-input",
            )
            yield OptionList(id="palette-list")
            yield Static("↑/↓ select · enter run · esc close", id="palette-hint")

    def on_mount(self) -> None:
        self._refilter("")
        self.query_one("#palette-input", Input).focus()

    def _rank(self, query: str) -> List[Dict[str, Any]]:
        """Fuzzy-ranked entries for `query`. Commands are boosted to the
        top of a tie (the palette is a COMMAND palette first); empty
        query returns the curated order, commands-then-sessions-files
        (the entry order) capped for the initial view."""
        if not query.strip():
            return self._entries[: self._MAX_RESULTS]
        return _fz.filter_and_rank(
            self._entries,
            query,
            lambda e: f"{e['label']} {e.get('hint', '')}",
            limit=self._MAX_RESULTS,
        )

    def _refilter(self, query: str) -> None:
        self._visible = self._rank(query)
        lst = self.query_one("#palette-list", OptionList)
        lst.clear_options()
        opts: List[Option] = []
        for i, e in enumerate(self._visible):
            style = self._KIND_STYLE.get(e.get("kind", "command"), ui.TEXT_PRIMARY)
            prompt = Text()
            prompt.append(f"{e['label']}  ", style=style)
            hint = e.get("hint", "")
            if hint:
                prompt.append(hint[:60], style=ui.TEXT_SECONDARY)
            # positional id: labels can legitimately repeat (a file
            # named like a command), and a duplicate Option id raises
            opts.append(Option(prompt, id=str(i)))
        lst.add_options(opts)
        if opts:
            try:
                lst.highlighted = 0
            except Exception:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""  # don't let the empty box re-fire
        self._choose()

    def _choose(self) -> None:
        if not self._visible:
            self.dismiss(None)
            return
        lst = self.query_one("#palette-list", OptionList)
        idx = lst.highlighted if lst.highlighted is not None else 0
        self.dismiss(self._visible[idx])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return
        if event.key in ("up", "down", "pageup", "pagedown", "home", "end"):
            # the OptionList owns the cursor even while the input box
            # holds focus — type AND browse at once (the VS Code feel)
            event.stop()
            event.prevent_default()
            lst = self.query_one("#palette-list", OptionList)
            action = {
                "up": "action_cursor_up",
                "pageup": "action_page_up",
                "home": "action_first",
                "down": "action_cursor_down",
                "pagedown": "action_page_down",
                "end": "action_last",
            }.get(event.key)
            if action and hasattr(lst, action):
                getattr(lst, action)()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def can_run_tui() -> bool:
    """True when the full-screen TUI should launch: textual importable,
    stdout a TTY (textual needs the terminal), not force-disabled via
    VEX_TUI=0. Never raises — False means 'use the rich REPL fallback'."""
    import os
    import sys

    if os.environ.get("VEX_TUI", "").lower() in ("0", "false", "no"):
        return False
    try:
        import textual  # noqa: F401
    except Exception:
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def run_tui(
    repo: Optional[Path] = None,
    log_root: Optional[Path] = None,
    file_config: Optional[Dict[str, Any]] = None,
    version: str = "",
) -> int:
    """Entry point for `vex` (no args, TTY): launch the full-screen app.

    Assumes a TTY with ANSI (cli.main checks before dispatching here).
    Returns a process exit code (0/130).
    """
    from cli.vexconfig import ensure_first_run, maybe_scaffold_repo, merged_settings

    repo = repo or _iv._detect_repo()
    log_root = Path(log_root) if log_root else Path("logs")
    created, gp = ensure_first_run()
    file_config = dict(file_config or merged_settings())
    state: Dict[str, Any] = {
        "model": None,
        "provider": None,
        "plan_preview": None,
        "quiet": False,
        "feed": True,  # live trace feed on by default (this round)
        "repo": str(repo),
        "file_config": file_config,
    }
    if file_config.get("log_verbosity") == "quiet":
        state["quiet"] = True
        state["feed"] = False
    if file_config.get("plan_preview") is not None:
        state["plan_preview"] = bool(file_config["plan_preview"])

    # Plugins round: extend the session's BATCH read-only allowlist.
    try:
        from cli.plugins import apply_tool_extensions

        apply_tool_extensions()
    except Exception:
        pass

    # First-`vex`-in-a-repo: scaffold <repo>/.vex/ when inside a git
    # repo (never overwrites, never outside a repo, never raises).
    scaffold = maybe_scaffold_repo()
    scaffold_note = ""
    if scaffold and scaffold.get("created"):
        scaffold_note = (
            "[vex.ok]repo setup:[/] [vex.muted]created "
            + ", ".join(f".vex/{c}" for c in scaffold["created"])
            + " — `vex config list` shows the chain[/]"
        )
    app = VexApp(
        repo=repo,
        log_root=log_root,
        state=state,
        file_config=file_config,
        version=version,
        # Real terminal session (can_run_tui gated): offer the modal
        # wizard when no model is set; direct construction stays quiet.
        onboard_prompt=True,
    )
    if created:
        app._first_run_note = (
            f"[vex.ok]first run:[/] [vex.muted]created global settings at "
            f"{gp} — `vex config list` to see what's set[/]"
        )
    if scaffold_note:
        app._first_run_note = (
            ((app._first_run_note or "") + "\n" + scaffold_note)
            if getattr(app, "_first_run_note", None)
            else scaffold_note
        )
    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    return getattr(app, "_exit_code", 0) or 0
