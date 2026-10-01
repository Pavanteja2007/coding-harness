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
  ...) and is NOT reimplemented here. Run state and conversation events
  are projected from the append-only trace journal. Captured backend text
  is retained only as bounded diagnostic detail and can never create a
  status, usage, verification outcome, or completion card.
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
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

import cli.a11y as _a11y
import cli.background as _bg
import cli.commands as _commands
import cli.design as _design
import cli.fileview as _fv
import cli.fuzzy as _fz
import cli.interactive as _iv
import cli.notify as _notify
import cli.streamview as _sv
import cli.toggles as _toggles
import cli.tracelog as _tl
import cli.ui as ui
from cli import runview as _rv
from cli.theme import ColorDepth, resolve_theme, theme_names
from cli.tui_components import (
    CommandPaletteFrame,
    ContextPanel,
    EmptyState,
    ErrorState,
    EventFeed,
    HeaderModel,
    LoadingState,
    ModalFrame,
    PlanRail,
    ResultCard,
    ShellFooter,
    ShellHeader,
    StreamPaint,
    fit_header,
    rail_content_width,
    rail_rows,
    resolve_shell_layout,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPOSER_PLACEHOLDER = "Ask, change, run, or debug this repo"
_CSS_THEME_PREFIX = ""
#: The layout authority is `cli/design.py`. The old
#: `_SIDEBAR_BREAKPOINT = 100` that lived here was DEAD (nothing read it) and
#: is gone; the real sidebar policy — the tri-state mode, the 120-column
#: `auto` breakpoint, the 42-column width, and the content-width formula —
#: is declared once in `cli.design` and reached through
#: `cli.tui_components.resolve_shell_layout`. Nothing in this file may
#: declare a layout constant; the gate is
#: `tests/test_design_layout.py::test_no_layout_constant_lives_outside_design`.
_TRANSCRIPT_MAX_LINES = 1200
_EVENT_POLL_S = 0.05
_PENDING_TOOL_AFTER_S = 0.75
_PROMPT_PATCH_LOCK = threading.RLock()
_PROMPT_PATCH_STACK: List[Tuple[Any, Any]] = []

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "|/\\"


class _UIMetrics:
    """Bounded in-process measurements for TUI interaction quality."""

    _NAMES = (
        # `input_ack_ms` is PER KEYSTROKE: the delay between a key
        # arriving and its character being in the composer. That is the
        # number the 100 ms gate is about.
        "input_ack_ms",
        # `submit_echo_ms` is the echo-before-persist path, which is a
        # different and larger budget. It was previously recorded under
        # `input_ack_ms`, which made the keystroke gate unfalsifiable
        # (a p95 over a handful of submitted lines).
        "submit_echo_ms",
        "event_to_ui_ms",
        "command_response_ms",
        "modal_open_ms",
        "resize_recovery_ms",
        "ui_thread_stall_ms",
        "live_diff_ms",
    )

    def __init__(self) -> None:
        self._started_wall = time.perf_counter()
        self._started_cpu = time.process_time()
        self._samples: Dict[str, List[float]] = {name: [] for name in self._NAMES}
        self._ui_stall_count = 0

    @staticmethod
    def _percentile(values: List[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)

    def observe(
        self,
        name: str,
        started: Optional[float] = None,
        value: Optional[float] = None,
    ) -> float:
        """Record one duration in milliseconds and return the value."""
        if name not in self._samples:
            self._samples[name] = []
        if value is None:
            value = (time.perf_counter() - float(started or time.perf_counter())) * 1000.0
        sample = max(0.0, round(float(value), 3))
        values = self._samples[name]
        values.append(sample)
        if len(values) > 512:
            del values[:-512]
        if name == "ui_thread_stall_ms" and sample > 500.0:
            self._ui_stall_count += 1
        return sample

    def observe_event(self, timestamp: Optional[float]) -> Optional[float]:
        """Record event-to-UI latency when a journal timestamp is usable."""
        if timestamp is None:
            return None
        try:
            latency = (time.time() - float(timestamp)) * 1000.0
        except (TypeError, ValueError):
            return None
        if latency < 0.0 or latency > 5000.0:
            return None
        return self.observe("event_to_ui_ms", value=latency)

    def snapshot(self) -> Dict[str, Any]:
        """Return JSON-friendly p50/p95/max metrics and process impact."""
        wall = max(0.0, time.perf_counter() - self._started_wall)
        cpu = max(0.0, time.process_time() - self._started_cpu)
        result: Dict[str, Any] = {
            "ui_thread_stalls_over_500ms": self._ui_stall_count,
            "wall_seconds": round(wall, 4),
            "cpu_seconds": round(cpu, 4),
            "cpu_percent": round((cpu / wall) * 100.0, 3) if wall else 0.0,
        }
        try:
            import tracemalloc

            result["python_memory_bytes"] = int(
                tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
            )
        except Exception:
            result["python_memory_bytes"] = 0
        for name in self._NAMES:
            values = self._samples.get(name, [])
            result[name] = {
                "samples": len(values),
                "p50": self._percentile(values, 0.5),
                "p95": self._percentile(values, 0.95),
                "max": round(max(values), 3) if values else None,
            }
        return result


def _truthy(value: Any) -> bool:
    """Interpret common terminal boolean spellings without raising."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "reduced"}


def _motion_enabled(
    state: Optional[Dict[str, Any]] = None, file_config: Optional[Dict[str, Any]] = None
) -> bool:
    """Return whether decorative terminal motion is enabled."""
    state = state or {}
    file_config = file_config or {}
    for value in (
        state.get("reduced_motion"),
        file_config.get("reduced_motion"),
        file_config.get("terminal_reduced_motion"),
        os.environ.get("VEX_REDUCED_MOTION"),
        os.environ.get("REDUCED_MOTION"),
        os.environ.get("NO_MOTION"),
    ):
        if value is not None and str(value) != "":
            return not _truthy(value)
    return True


def _term_is_dumb() -> bool:
    """Return whether the terminal explicitly requests dumb rendering."""
    return os.environ.get("TERM", "").strip().lower() == "dumb"


#: Thinking uses ui.THINK_FRAMES (distinct ORBIT glyph, shared with the
#: rich REPL's LiveMonitor) and the shared ui.JOKES table — one source.


def _spinner_frames(reduced_motion: bool = False) -> str:
    """Return encoding-safe spinner frames or a static reduced-motion mark."""
    if reduced_motion:
        return "*"
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
        # Vex's own artifact root. `git ls-files` lists tracked files only,
        # so a repo that ever committed a `logs/` tree would otherwise
        # surface every journal/state file as a phantom palette entry. The
        # walk fallback already skipped this; both paths must agree.
        "logs",
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
    """Build the Rich-role to Textual-markup map from active tokens."""
    out: Dict[str, str] = {}
    try:
        for name, style in ui.current_rich_theme().styles.items():
            if name.startswith("vex."):
                mk = _style_to_markup(style)
                if mk:
                    out[name] = mk
    except Exception:
        pass
    return out


_ROLE_MAP = _build_role_map()


def _refresh_role_map() -> None:
    """Refresh role conversion after a theme selection."""
    global _ROLE_MAP
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


def _vex_textual_theme(tokens: Optional[Any] = None) -> Any:
    """Build a Textual theme from resolved semantic terminal tokens."""
    from textual.theme import Theme

    active = tokens or ui.active_tokens()
    variables = ui.textual_theme_variables(active)
    variables.update(
        {
            "input-cursor-background": active["accent_text"],
            "input-cursor-foreground": active["text_primary"],
            "input-selection-background": active["selection_bg"] + "66",
            "input-selection-foreground": active["selection_fg"],
            "screen-selection-background": active["selection_bg"] + "7F",
            "screen-selection-foreground": active["selection_fg"],
            "block-cursor-background": active["accent_text"],
            "block-cursor-foreground": active["text_primary"],
            "block-cursor-blurred-background": active["accent_primary"] + "4C",
            "block-cursor-blurred-foreground": active["text_primary"],
            "border": active["accent_text"],
            "border-blurred": active["border_subtle"],
            "link-background-hover": active["accent_primary"],
            "footer-key-foreground": active["accent_text"],
            "text-disabled": active["text_disabled"],
            "foreground-disabled": active["text_disabled"],
            "scrollbar": active["border_subtle"],
            "scrollbar-hover": active["accent_text"],
            "scrollbar-active": active["accent_text"],
            "scrollbar-background": active["bg_base"],
            "scrollbar-background-hover": active["bg_panel_hover"],
            "scrollbar-background-active": active["bg_base"],
            "scrollbar-corner-color": active["bg_base"],
        }
    )
    return Theme(
        name="vex",
        primary=active["accent_text"],
        secondary=active["accent_text"],
        accent=active["accent_text"],
        foreground=active["text_primary"],
        background=active["bg_base"],
        surface=active["bg_panel"],
        panel=active["bg_panel"],
        warning=active["warning"],
        error=active["error"],
        success=active["success"],
        dark=True,
        variables=variables,
    )


def _m(text: str) -> str:
    """Map Vex roles to Textual styles after removing terminal escapes.

    A role the active theme cannot express (common under `NO_COLOR` /
    `TERM=dumb`, where the Rich theme carries no color and most roles
    resolve to a plain style) becomes Textual's `none` style. Passing the
    raw `[vex.*]` tag through is NOT safe: Textual does not parse it as a
    style, so the following `[/]` is an orphan and the whole app dies with
    `MarkupError: auto closing tag ('[/]') has nothing to close`."""
    text = ui.strip_ansi(text)
    if "vex." not in text:
        return text
    return _ROLE_RE.sub(
        lambda mo: f"[{mo.group(1)}{_ROLE_MAP.get(mo.group(2), 'none')}]",
        text,
    )


_CSS_THEME_PREFIX = "\n".join(
    f"    ${name}: {value};" for name, value in ui.textual_theme_variables().items()
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

FEED_LABELS = {
    "reason": "thinking",
    "tool": "tool",
    "diff": "change",
    "verify": "verification",
    "lifecycle": "lifecycle",
    "info": "info",
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


def _active_feed_styles() -> Dict[str, str]:
    """Return feed styles from the currently selected semantic tokens."""
    tokens = ui.active_tokens()
    return {
        "reason": f"italic {tokens['text_secondary']}",
        "tool": f"bold {tokens['accent_text']}",
        "diff": f"bold {tokens['accent_text']}",
        "verify": tokens["text_secondary"],
        "lifecycle": tokens["text_secondary"],
        "info": tokens["text_secondary"],
    }


def feed_style(entry: "_tl.FeedEntry") -> str:
    """The line style for one feed entry. Verify lines are outcome-aware."""
    tokens = ui.active_tokens()
    if entry.category == "verify":
        s = entry.summary.lower()
        if any(m in s for m in VERIFY_PASS_MARKS):
            return tokens["success"]
        if any(m in s for m in VERIFY_FAIL_MARKS):
            return tokens["error"]
    return _active_feed_styles().get(entry.category, tokens["text_secondary"])


def feed_line(entry: "_tl.FeedEntry") -> str:
    """A feed entry as one display markup line: glyph + index + summary,
    styled by category. The reasoning/action distinction (Task E) lives
    in the returned style string (italic dim for reason, bold accent for
    tool), so it is consistent everywhere the feed renders."""
    glyph = FEED_GLYPHS.get(entry.category, "*")
    style = feed_style(entry)
    label = FEED_LABELS.get(entry.category, "info")
    return (
        f"[{style}]{glyph} {label}[/][vex.muted] {entry.index:>2}[/] "
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
                lines[-1].append(ui.strip_ansi(part), style=seg.style)
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
            try:
                con._buffer.clear()
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


class _PromptScreen(ModalFrame[str]):
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
        border: round $vex-accent;
    }
    #prompt-title {
        color: $vex-accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #prompt-body {
        height: auto;
        max-height: 16;
        margin-bottom: 1;
    }
    #prompt-input {
        border: round $vex-accent;
    }
    #prompt-hint {
        color: $vex-secondary; /* text-secondary (design token), not a random grey */
        margin-top: 1;
    }
    """

    def __init__(self, question: str, body_lines: Optional[List[Text]] = None) -> None:
        super().__init__()
        self._question = question
        self._body = body_lines or []
        self._dismissed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(escape(self._question), id="prompt-title")
            yield RichLog(id="prompt-body", markup=False, wrap=False)
            yield Input(placeholder="answer", id="prompt-input")
            yield Static("enter submit · tab focus · esc cancel", id="prompt-hint")

    def on_mount(self) -> None:
        super().on_mount()
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


class _OnboardScreen(ModalFrame[Any]):
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
        border: round $vex-accent;
    }
    #onboard-title {
        color: $vex-accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #onboard-hint {
        color: $vex-secondary;
        margin-top: 1;
    }
    #onboard-error {
        color: $vex-accent;
        margin-top: 1;
    }
    #onboard-input {
        border: round $vex-accent;
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
        super().on_mount()
        self.call_after_refresh(self._schedule_render)

    def _dismiss_once(self, result: Any) -> None:
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
        import asyncio

        from textual.css.query import NoMatches
        from textual.widget import MountError

        from cli import onboard as _ob

        title = None
        body = None
        err = None
        for _ in range(100):
            try:
                candidate_title = self.query_one("#onboard-title", Static)
                candidate_body = self.query_one("#onboard-body", Vertical)
                candidate_error = self.query_one("#onboard-error", Static)
                if candidate_title.is_attached and candidate_body.is_attached and candidate_error.is_attached:
                    title = candidate_title
                    body = candidate_body
                    err = candidate_error
                    break
            except NoMatches:
                pass
            await asyncio.sleep(0.01)
        if title is None or body is None or err is None:
            return
        err.update(_m(f"[vex.error]{escape(self._error)}[/]") if self._error else "")

        async def mount_child(child: Any) -> bool:
            for _ in range(100):
                try:
                    await body.mount(child)
                    return True
                except MountError:
                    await asyncio.sleep(0.01)
            return False

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
            if not await mount_child(opts):
                return
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
            if not await mount_child(inp):
                return
            inp.focus()
        elif self._step == "testing":
            title.update(_m("[vex.accent]Testing the endpoint...[/]"))
            if not await mount_child(
                Static("one tiny live call; nothing is saved until it passes")
            ):
                return

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
        if val.startswith("/"):
            self._dismiss_once(val)
            return
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


def _honest_session_label(row: Any) -> str:
    """The honest verdict label for one session/index row.

    Delegates to `cli.runview.honest_row_status`, which fails CLOSED: a
    bare `completed` / `success` word with no clean verifier evidence is
    `unverified`, never `verified`. Every TUI surface that shows a run's
    outcome must route through this — the lifecycle `status` field
    collapses `completed_verified` and `completed_unverified` into one
    word, which is the R2-G45 defect.
    """
    try:
        from cli.runview import honest_row_status

        return honest_row_status(row)
    except Exception:
        return "unknown"


def _verdict_style(verdict: Any) -> str:
    """The Textual style for an honest verdict label.

    `SUCCESS` is reachable for exactly one value. Every other outcome —
    including an unverified completion — gets a distinct, non-success
    style, so no renderer can paint verified-looking chrome onto
    unverified work.
    """
    value = str(verdict or "").strip().lower()
    if value == "verified":
        return ui.SUCCESS
    if value in ("unverified", "pending"):
        return ui.WARNING
    if value == "failed":
        return ui.ERROR
    return ui.TEXT_SECONDARY


class _RunState:
    """Mutable snapshot of one live run, derived from trace events only.

    Same event-kind contract as _iv.LiveMonitor (harness trace.jsonl is
    the public observability surface; fields used are documented ones).
    This round adds the structured companions: `todo` (cli.runview's
    TodoModel — the harness's own plan/step decomposition) and `mode`
    (the work mode, from task_start's mode field or the TUI's dispatch).
    """

    __slots__ = (
        # streaming lane (VEX-CEILING-10). These three are assigned in
        # __init__ below; without them in __slots__ the assignment raises
        # AttributeError and EVERY _RunState construction fails, which
        # takes the whole TUI down.
        "_phases",
        "_stream",
        "calls",
        "cancel_requested",
        "cost",
        "events",
        "feed",
        "last_event_timestamp",
        "last_rendered_stream",
        "mode",
        "pending_label",
        "pending_since",
        "phase",
        "projection",
        "started_at",
        "stream_text",
        "streaming",
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
        self.thinking = False
        self.cancel_requested = False
        self.pending_label = ""
        self.pending_since: Optional[float] = None
        self.stream_text = ""
        self.streaming = False
        self.last_event_timestamp: Optional[float] = None
        self.feed = _tl.FeedBuilder(task_id)
        self.todo = _rv.TodoModel()  # live checklist (todo/status round)
        self.projection = _rv.RunProjection(task_id, mode=mode)
        # VEX-CEILING-10: the frame path folds journal deltas through the
        # coalescer (bounded text, one repaint per window) and derives its
        # label from the phase projector, which distinguishes waiting for
        # a first token from thinking from streaming from a running tool.
        # Both are pure projections over the journal; neither does I/O.
        self._stream = _sv.StreamCoalescer(
            window_ms=int(os.environ.get("VEX_STREAM_WINDOW_MS") or 60)
        )
        self._phases = _sv.PhaseProjector()
        self.last_rendered_stream: str = ""

    def consume(self, obj: Dict[str, Any]) -> bool:
        """Fold one legacy or normalized journal event into the state."""
        before_revision = self.projection.cursor_warning_revision
        changed = False
        try:
            changed = self.projection.consume(obj)
            accepted = self.projection.last_accepted_events
            for event in accepted:
                try:
                    self.todo.consume(event)
                except Exception:
                    pass
                kind, data, timestamp, _identity = _rv.event_parts(event)
                if timestamp is not None:
                    self.last_event_timestamp = timestamp
                if kind in ("run_started", "run_start", "task_start", "project_start") and data.get("mode"):
                    reported_mode = str(data.get("mode") or "")
                    self.mode = {
                        "agent": "agent_task",
                        "agent_task": "agent_task",
                        # The kernel's DAILY strategy is this shell's daily
                        # coding path, not a project build. Mapping it to
                        # "build" made a one-file coding run render with the
                        # same mode label as a multi-session project, which
                        # is exactly the distinction the live view exists to
                        # make. `project_start` is what selects a build.
                        "daily": "agent_task",
                        "verified_fix": "fix",
                        "plan": "plan",
                        "question": "question",
                        "research": "research",
                        "connector": "connector",
                        "build": "build",
                    }.get(reported_mode, self.mode)
                elif kind == "project_start":
                    self.mode = "build"
                if kind == "run_started":
                    kind = "task_start"
                elif kind == "run_finished":
                    kind = "task_end"
                elif kind == "model_completed":
                    kind = "model_response"
                elif kind == "verification":
                    kind = "verify"
                if kind == "model_request":
                    self.thinking = True
                    self.streaming = True
                    self.stream_text = ""
                    self._stream.reset()
                elif kind in ("model_delta", "response_delta", "text_delta"):
                    delta = data.get("delta") or data.get("text") or data.get("content") or ""
                    self._stream.push_delta(str(delta))
                    self.streaming = True
                    self.thinking = True
                    self.phase = "streaming response"
                    changed = True
                elif kind == "model_response":
                    self.thinking = False
                    self.streaming = False
                if kind in ("tool_call", "tool_started"):
                    action = self.projection.current_action or "tool pending"
                    command = str(data.get("command") or "").strip()
                    if command:
                        action = f"{action} · {command[:80]}"
                    self.pending_label = action
                    self.pending_since = time.monotonic()
                elif kind in ("tool_result", "tool_completed", "tool_error"):
                    self.pending_label = ""
                    self.pending_since = None
                if kind in ("cancellation_requested", "cancel_requested"):
                    self.cancel_requested = True
                    self.phase = "cancel requested"
                label = _iv._EVENT_LABELS.get(kind)
                if label:
                    for field in _iv._LABEL_FIELDS.get(kind, []):
                        if data.get(field) is not None:
                            label = label.format(**{field: data[field]})
                    self.phase = re.sub(r"\{[a-z]+\}", "", label)
                elif kind in {"phase_changed", "phase_change", "state_change"}:
                    self.phase = str(data.get("phase") or data.get("state") or self.phase)
        except Exception:
            return False
        try:
            self._phases.consume(kind, data)
        except Exception:
            pass
        snapshot = self.projection.snapshot()
        self.events = int(snapshot.get("events") or 0)
        self.calls = int(snapshot.get("model_calls") or 0)
        self.tokens = int(snapshot.get("tokens") or 0)
        self.cost = float(snapshot.get("cost_usd") or 0.0)
        if self.projection.cursor_warning_revision != before_revision:
            changed = True
        return changed

    def poll_frame(self) -> Optional[str]:
        """The next frame payload, or ``None`` for a no-op repaint.

        THE frame-path entry point (VEX-CEILING-10). It performs no I/O, no
        subprocess, and no network: it reads the coalescer, which already
        holds everything the journal tail thread folded in. Its cost is
        bounded by the number of frames, not by the number of stream
        events, which is what keeps a 200k-token answer as cheap to render
        as a 20-token one.
        """
        try:
            payload = self._stream.poll()
        except Exception:
            return None
        if payload is not None:
            self.stream_text = payload
        return payload

    def phase_state(self) -> "_sv.PhaseState":
        """The phase-typed state, including the slow-path wording."""
        try:
            return self._phases.state()
        except Exception:
            return _sv.PhaseState(phase=_sv.RunPhase.IDLE)

    def stream_receipt(self) -> Dict[str, Any]:
        """The measured frame-cost receipt for this run's live text."""
        try:
            return self._stream.frame_cost_receipt()
        except Exception:
            return {}

    def note_cancel_requested(self) -> None:
        """Record a cancellation in BOTH lanes (journal + control bypass)."""
        self.cancel_requested = True
        try:
            self._phases.cancel()
        except Exception:
            pass
        self._stream.push_control("cancel requested", "warn")

    def reset_stream(self) -> None:
        """Reset journal-derived state after trace rotation or reconnect."""
        self.events = 0
        self.calls = 0
        self.tokens = 0
        self.cost = 0.0
        self.thinking = False
        self.cancel_requested = False
        self.pending_label = ""
        self.pending_since = None
        self.stream_text = ""
        self.streaming = False
        self.last_event_timestamp = None
        self.feed.reset()
        self.todo = _rv.TodoModel()
        self.projection.reset_stream()
        self.phase = "reconnecting to event journal"
        try:
            self._stream.reset()
            self._phases.reset()
        except Exception:
            pass

    def line(self, frame: int = -1, reduced_motion: bool = False) -> str:
        """Render the journal-derived loading state through the shared component."""
        # Drain the coalescer HERE, not only in ``poll_frame``. ``line`` is the
        # render entry point, and a caller that renders without polling first
        # used to get a state whose phase said "streaming response" while
        # ``stream_text`` was still empty - a self-contradicting frame. A
        # second poll inside one window is a cheap no-op, so the TUI's
        # poll-then-line order still produces exactly one frame per window.
        self.poll_frame()
        elapsed = int(time.monotonic() - self.started_at)
        snapshot = self.projection.snapshot()
        cost_text = ui.fmt_cost(self.cost) if snapshot.get("cost_known") else "unknown cost"
        if self.thinking:
            frames = "" if reduced_motion else ui.thinking_frames()
            spinner = frames[frame % len(frames)] if frame >= 0 and frames else ""
            joke = "" if reduced_motion else (ui.joke_at(frame // 36) if frame >= 0 else ui.JOKES[0])
        else:
            frames = _spinner_frames(reduced_motion)
            spinner = frames[frame % len(frames)] if frame >= 0 and frames else ""
            joke = ""
        phase = self.phase
        if self.cancel_requested:
            phase = "cancel requested"
        elif self.pending_label:
            pending_age = (
                time.monotonic() - self.pending_since
                if self.pending_since is not None
                else 0.0
            )
            pending_state = (
                "tool still running" if pending_age >= _PENDING_TOOL_AFTER_S else "tool pending"
            )
            phase = f"{pending_state} · {self.pending_label} · /cancel"
        if self.streaming and "stream" not in phase.lower():
            phase = f"streaming response · {phase}"
        # VEX-CEILING-10: when a model call is in flight the phase-typed
        # state wins, because it is the only view that can tell a slow
        # endpoint (waiting for first token) from a wedged one. The
        # journal's own label is kept as the detail so no event meaning is
        # lost.
        typed = self.phase_state()
        if typed.phase in (
            _sv.RunPhase.AWAITING_FIRST_TOKEN,
            _sv.RunPhase.THINKING,
            _sv.RunPhase.STREAMING,
        ) and not self.cancel_requested:
            phase = typed.label()
            if self.phase and self.phase not in phase:
                phase = f"{phase} · {self.phase}"
        action = ""
        if self.mode not in ("fix",) and not self.todo.steps:
            action = self.projection.current_action or ""
            if action and action != phase:
                phase = f"{phase} · {action}"
        return LoadingState(
            task_id=self.task_id,
            phase=phase,
            mode=self.mode,
            events=self.events,
            cost_text=cost_text,
            elapsed_s=elapsed,
            thinking=self.thinking,
            spinner=spinner,
            joke=joke,
        ).render()


def _tail_trace(
    task_id: str,
    log_root: Path,
    run: _RunState,
    stop: threading.Event,
    on_event: Callable[[_RunState, List[_tl.FeedEntry], Optional[float]], None],
    poll_s: float = _EVENT_POLL_S,
    replay_existing: bool = False,
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
    try:
        import inspect

        callback_parameters = inspect.signature(on_event).parameters
        callback_accepts_timestamp = len(callback_parameters) >= 3 or any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in callback_parameters.values()
        )
    except (TypeError, ValueError):
        callback_accepts_timestamp = True
    while not stop.is_set() and not trace.exists():
        stop.wait(0.05)
    pos = 0
    carry = b""
    awaiting_replacement = False
    ignored_size = 0

    def _drain(emit: bool = True, flush: bool = False) -> bool:
        nonlocal pos, carry, awaiting_replacement, ignored_size
        if not trace.exists():
            return False
        rotated = False
        try:
            size = trace.stat().st_size
            if awaiting_replacement:
                if size <= ignored_size:
                    return False
                awaiting_replacement = False
                pos = 0
                carry = b""
            elif size < pos:
                run.reset_stream()
                run.projection.mark_reconnect("trace journal replaced; replaying")
                pos = size
                carry = b""
                awaiting_replacement = True
                ignored_size = size
                rotated = True
            with trace.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except OSError:
            return False
        if rotated:
            chunk = b""
        if chunk:
            carry += chunk
        lines = carry.split(b"\n")
        carry = lines.pop() if lines else b""
        if flush and carry:
            lines.append(carry)
            carry = b""
        consumed = False
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line.decode("utf-8", errors="replace"))
            except ValueError:
                run.projection.mark_reconnect("malformed journal row skipped")
                consumed = True
                continue
            changed = run.consume(obj)
            entries = run.feed.consume(obj)
            if changed or entries:
                consumed = True
                if emit:
                    _kind, _data, timestamp, _identity = _rv.event_parts(obj)
                    if callback_accepts_timestamp:
                        on_event(run, entries, timestamp)
                    else:
                        on_event(run, entries)
        return consumed

    if replay_existing:
        _drain(emit=False)
    while not stop.is_set():
        _drain()
        stop.wait(poll_s)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _drain(flush=True):
            break
        stop.wait(0.05)


_TranscriptLog = EventFeed


def _todo_marks() -> Dict[str, tuple]:
    """Return todo-state glyphs and colors from active semantic tokens."""
    tokens = ui.active_tokens()
    return {
        _rv.PENDING: ("○", tokens["text_secondary"]),
        _rv.ACTIVE: ("▸", tokens["accent_text"]),
        _rv.DONE: ("✔", tokens["success"]),
        _rv.SKIPPED: ("↷", tokens["text_secondary"]),
        _rv.FAILED: ("✘", tokens["error"]),
    }


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# THE RAIL'S SINGLE VOCABULARY (round 2 layout audit)
#
# The rail renders the live fact set TWICE: the projection block is
# `runview.status_lines` and the meters block below it restates mode, state,
# time, calls, tokens, and cost. `status_lines` is deliberately NOT edited
# here — the REPL and `/status` read it, and the round-2 audit rendered both
# spellings of one fact in one column (`verify PASS` directly above
# `verify verified`, `elapsed —` above `time 0s`).
#
# So the de-duplication happens on the rail's side, by the row's LABEL: a
# declared, small vocabulary rather than a second copy of the facts. A row
# whose label cannot be read is KEPT — a filter that silently drops a fact it
# failed to parse is the very defect this exists to remove.
# ---------------------------------------------------------------------------

#: Projection rows whose fact the rail states in its OWN block.
_RAIL_ROW_LABELS_OWNED_ELSEWHERE = frozenset(
    {"elapsed", "usage", "checkpoints", "diagnostics"}
)


def _row_label(line: str) -> str:
    """The label a rail fact row starts with, or `""` when there is none.

    The rows arrive as Textual markup — `[dim]usage[/] [#F5F5F5]unknown calls[/]`
    — so the tags are stripped before the label is read. An ESCAPED bracket
    (``\\[``) is left alone: `runview.status_lines` escapes its values, so a
    literal `[` in a path or a message survives the strip and a regex that ate
    it would shorten a label into something that matches nothing.
    """
    plain = _RAIL_MARKUP_TAG.sub(" ", ui.strip_ansi(str(line))).strip()
    if not plain:
        return ""
    for part in plain.split(" "):
        token = part.strip()
        if token and not token.startswith("[") and "]" not in token:
            return token.split("·")[0].strip()
    return ""


_RAIL_MARKUP_TAG = re.compile(r"(?<!\\)\[[^\]]*\]")


def _drop_rail_duplicate_rows(lines: List[str]) -> List[str]:
    """Remove the projection rows the rail's own blocks already state."""
    return [
        line
        for line in lines
        if _row_label(line) not in _RAIL_ROW_LABELS_OWNED_ELSEWHERE
    ]


class VexApp(App):
    """The persistent vex session shell (full-screen textual app)."""

    TITLE = "vex"
    SUB_TITLE = "the AI harness that fixes bugs"

    CSS = f"""{_CSS_THEME_PREFIX}
    Screen {{
        layout: vertical;
        background: $vex-background;
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
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        /* Never wrap. The header is `height: 1`, so a brand that wrapped
           put its second row outside the frame: the rendered row ended with
           a bare `· mode` and the value on the row nobody can see. The
           budget in `tui_components.fit_header` is the primary mechanism;
           this is the one that makes an arithmetic slip a visible ellipsis
           instead of a silent half-fact. */
        text-wrap: nowrap;
    }}
    #vex-task,
    #vex-status {{
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    #vex-task {{
        width: auto;
        max-width: 24;
        color: $vex-accent;
        margin-left: 1;
    }}
    /* The status chip is separated from the task chip. Rendered-frame audit
       (round 2) read `task audit-task-1234running` at EVERY width: the two
       most load-bearing facts in the shell were one 26-character token with
       nothing marking where one ended. A separator is the whole fix, and it
       is a layout fact, so it lives here rather than in the value. */
    #vex-status {{
        width: auto;
        min-width: 5;
        color: $vex-accent;
        margin-left: 1;
        content-align: right middle;
    }}
    /* Middle: transcript (1fr) + the live sidebar (todo + status
       panel). The sidebar fills the previously-empty right rail with
       data the run already tracks; it collapses when nothing runs. */
    #vex-mid {{
        height: 1fr;
    }}
    #vex-body {{
        width: 1fr;
        min-width: 0;
        padding: 0 1;
        border-top: solid $vex-border;
        scrollbar-size: 1 1;
    }}
    #vex-side {{
        width: 0;
        min-width: 0;
        padding: 0 1;
        background: $vex-panel;
        border-right: solid $vex-border;
        display: none;
    }}
    #vex-side-header {{
        color: $vex-accent;
        text-style: bold;
    }}
    #vex-todo {{
        height: auto;
        margin-bottom: 1;
    }}
    #vex-plan-checkpoints {{
        height: auto;
        margin-bottom: 1;
    }}
    #vex-side-label {{
        color: $vex-secondary;
        text-style: bold;
    }}
    #vex-side-status {{
        height: auto;
    }}
    #vex-context {{
        width: 0;
        min-width: 0;
        padding: 0 1;
        background: $vex-panel;
        border-left: solid $vex-border;
        display: none;
    }}
    #vex-context-header {{
        color: $vex-accent;
        text-style: bold;
        margin-bottom: 1;
    }}
    /* NO `max-height` on the rail regions any more. A constant cap is what
       silently cut the tail: the plan rail's cost/error/unreadable-event
       rows were off screen at every height where the rail appeared, and the
       context rail's whole EVIDENCE + USAGE block sat below the rail's
       bottom edge at 120x36. The cap is now the row BUDGET the renderers
       measure — see `tui_components.fit_region_blocks`. */
    #vex-context-files,
    #vex-context-relevant,
    #vex-context-sources,
    #vex-context-diagnostics,
    #vex-context-usage,
    #vex-context-legend {{
        height: auto;
        margin-bottom: 1;
    }}
    /* The LAST block of each rail carries no trailing margin. Every region
       above has `margin-bottom: 1` and the renderer reserves one separator row
       per block that FOLLOWS, so a margin on the final one is a row the rail
       spends on nothing — which is how the state-code legend lost its last
       row to the bottom edge at 120x36. This is declared last so it is the
       declaration that wins. */
    #vex-side-status,
    #vex-context-legend {{
        margin-bottom: 0;
    }}
    /* Run-line: the ONLY live widget during a run — updated in place.
       Sits on a raised panel surface with a left activity edge in the
       logo's crimson (active = accent, per the design system). */
    #vex-runline {{
        height: 1;
        padding: 0 1;
        background: $vex-panel;
        border-left: outer $vex-accent;
        display: none;
    }}
    /* Stream paint (R2-17): the bounded live-text peephole, directly
       under the one-line run line. A SEPARATE widget on purpose — the
       run line stays `height: 1`, so live text can never make it
       multi-line, and the transcript's scrollback is untouched. */
    #vex-stream {{
        height: auto;
        max-height: 4;
        padding: 0 2;
        background: $vex-panel;
        border-left: outer $vex-streaming;
        color: $vex-secondary;
        display: none;
        overflow: hidden;
    }}
    /* Input box pinned directly under the transcript (spacing
       tightened: the transcript's own border is the only divider —
       no empty band between the two). */
    #vex-inputwrap {{
        height: 3;
        padding: 0 1;
    }}
    #vex-input {{
        border: round $vex-border;
        background: $vex-panel;
    }}
    #vex-input:focus {{
        border: round $vex-accent;
        /* textual's Input:focus default is `background-tint: $foreground
           5%` — a non-token lighter blend (probe-verified).
           Focus is an INTERACTIVE state, so it uses the token for
           elevated/interactive surfaces: bg-panel-hover, exactly. */
        background: $vex-panel-hover;
        background-tint: $vex-panel-hover 0%;
    }}
    /* Focus-visible, for every focusable surface including the
       transcript. textual's default is `text-style: bold`, which on a
       black-on-black transcript is nearly invisible: the transcript has
       no border to tint, so a keyboard user tabbing into it got bold
       text and no other cue that focus had moved. The accent left edge
       is the same cue the run line uses for "active", so "focused" and
       "running" read as the same visual language rather than two. */
    #vex-body:focus {{
        border-left: outer $vex-focus;
    }}
    RichLog:focus {{
        text-style: none;
    }}
    Input:focus, OptionList:focus {{
        text-style: bold;
    }}
    OptionList:focus {{
        border: round $vex-focus;
    }}
    /* THE ANNOUNCEMENT REGION (Terminal 06 accessibility).
       A status chip updated IN PLACE emits no new bytes, so it is
       invisible to a screen reader reading the terminal buffer and
       indistinguishable from a static word on a dumb terminal. This
       region exists so every state transition is also written as
       NEW, PLAIN, UNCOLOURED output that a screen reader reads and a
       `TERM=dumb` console shows verbatim. It is `height: 1` and
       `display: none` when empty so an idle shell carries no blank
       band, and it sits directly above the composer where a run's
       outcome is looked for. */
    #vex-announce {{
        height: 1;
        padding: 0 1;
        color: $vex-text;
        background: $vex-panel;
        border-left: outer $vex-focus;
        display: none;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    /* Hint bar: keyboard shortcuts, OpenCode-style. */
    #vex-hints {{
        height: 1;
        padding: 0 1;
        background: $vex-panel;
        color: $vex-secondary;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    /* Statusline: the live run-context facts, each with the key that
       reaches it, joined by the middle dot and dropped by priority as the
       terminal narrows. It is `display: none` whenever it has nothing TRUE
       to say: "a hint must be true" is why the count is the caller's
       decision and why `design.fit_statusline` renders nothing at all when
       every section is empty. `height: auto` so a statusline that says
       nothing costs zero rows, not one blank one. */
    #vex-statusline {{
        height: auto;
        padding: 0 1;
        color: $vex-secondary;
        display: none;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    /* The sidebar's own sections. Each is `display: none` until the
       anti-clutter rule allows it to render, so an idle sidebar costs no
       row and no margin — an empty `height: auto` Static still costs a row
       and a margin, and two of those spent three of a 19-row rail. The
       margin IS the spacing unit (see `cli/design.py::SPACING_UNIT`), and
       the compact density sets it to zero imperatively, which is what
       measurably buys the rail its rows back. */
    #vex-sidebar-session,
    #vex-sidebar-context,
    #vex-sidebar-mcp,
    #vex-sidebar-lsp,
    #vex-sidebar-todo,
    #vex-sidebar-files,
    #vex-sidebar-startup {{
        height: auto;
        margin-bottom: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    #vex-sidebar-footer {{
        height: auto;
        margin-bottom: 0;
        color: $vex-secondary;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    """

    BINDINGS: ClassVar[List[Binding]] = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", show=True, priority=True),
        Binding("ctrl+q", "quit_app", "quit", show=True),
        Binding("ctrl+y", "copy_selection", "copy selection", show=False, priority=True),
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
        # VEX-CEILING-10 steering while a run is live. The composer stays
        # usable during a run; these two bindings are the difference
        # between "steer now" and "steer later", and they are the reason a
        # steering message never needs the run to be cancelled first.
        #   ctrl+g -> deliver at the NEXT SAFE BOUNDARY (the loop's own
        #             checkpoint decides when; in-flight work is preserved)
        #   ctrl+b -> QUEUE without interrupting (delivered at the next
        #             boundary after any already-queued steering)
        Binding("ctrl+g", "steer_boundary", "steer", show=False, priority=True),
        Binding("ctrl+b", "steer_queue", "queue steering", show=False, priority=True),
        # The sidebar's tri-state mode (auto / show / hide), advancing on
        # each press. The key and the `/sidebar` command are declared in
        # `cli.toggles` (Prompt 04's registry) — this binding is the MOUNT
        # of that declaration, not a second key. The action is named
        # `toggle_sidebar` rather than `action_toggle_sidebar` on purpose:
        # `toggles.toggle_mounts` classifies a bound key by the `toggle_`
        # prefix, so `action_toggle_sidebar` reads as a `conflict` (a key
        # bound to something that is not a toggle action) when it is in fact
        # the one mounted toggle. Textual resolves the action to
        # `action_toggle_sidebar`, which is the method below.
        Binding("ctrl+5", "toggle_sidebar", "sidebar", show=False, priority=True),
        Binding("ctrl+o", "cycle_density", "density", show=False, priority=True),
        # Cancel affordance, reachable by key at every terminal width
        # including 62 columns where the run-line label is too narrow to
        # show the hint.
        Binding("ctrl+x", "cancel_or_quit", "cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        repo: Path,
        log_root: Path,
        state: Optional[Dict[str, Any]] = None,
        file_config: Optional[Dict[str, Any]] = None,
        version: str = "",
        onboard_prompt: bool = False,
        theme: Optional[str] = None,
        theme_overrides: Optional[Dict[str, Any]] = None,
        theme_depth: Optional[ColorDepth | str] = None,
        root_scoped: bool = False,
    ) -> None:
        super().__init__()
        # True when the caller chose this artifact root explicitly. Session
        # browsers then read ONLY this root instead of the machine-wide
        # cross-repo index.
        self._root_scoped = bool(root_scoped)
        # Keep a reference to the caller's dict so a logout/config refresh can
        # purge a stale api_key from it too, not just from our own copy.
        self._supplied_file_config: Optional[Dict[str, Any]] = (
            file_config if isinstance(file_config, dict) else None
        )
        self.file_config: Dict[str, Any] = dict(file_config or {})
        self._tokens_before = ui.active_tokens()
        self._tokens = resolve_theme(
            name=theme or self.file_config.get("theme"),
            overrides=(
                theme_overrides
                if theme_overrides is not None
                else self.file_config.get("theme_overrides")
            ),
            depth=theme_depth,
            config=self.file_config,
            is_tty=True,
        )
        try:
            self.register_theme(_vex_textual_theme(self._tokens))
            self.theme = "vex"
        except Exception:
            pass
        self.repo = repo
        self.log_root = Path(log_root)
        if self._tokens.depth is ColorDepth.NONE:
            self.ansi_color = False
        self.state: Dict[str, Any] = state or {
            "model": None,
            "provider": None,
            "plan_preview": None,
            "quiet": False,
            "mode": "auto",
            "feed": True,
            "repo": str(repo),
            "file_config": None,
        }
        self.state.setdefault("mode", "auto")
        self.state.setdefault("feed", True)
        if not isinstance(self.state.get("approval_policy"), _commands.ApprovalPolicy):
            self.state["approval_policy"] = _commands.ApprovalPolicy()
        self.version = version
        self._motion_enabled = bool(
            self._tokens.motion and _motion_enabled(state, self.file_config)
        )
        self.state["reduced_motion"] = not self._motion_enabled
        self.ui_metrics = _UIMetrics()
        # Terminal 06: the announcement gate. A 125 Hz repaint loop would
        # otherwise speak hundreds of times per run; this speaks once per
        # TRANSITION, keyed by the caller (the phase string, the approval
        # effect, the terminal status). Deliberately not time-based: a
        # phase the user watched come and go is news again if it returns.
        self._announcements = _a11y.AnnouncementGate()
        # The task whose OUTCOME has been announced. Set by
        # `_announce_outcome`; read by `_announce_phase` so a live repaint
        # can never speak over a finished run's verdict. Cleared when a
        # new run starts.
        self._settled_outcome_task: str = ""
        self._last_rendered_event_timestamp: Optional[float] = None
        self._pending_run_text: Optional[str] = None
        self._diff_generation = 0
        self._last_resize_state: Optional[Dict[str, Any]] = None
        # First-run onboarding: run_tui passes True only on a real TTY;
        # on_mount then offers the modal wizard when no usable model is
        # set. An explicit flag (not an isatty probe at mount — textual
        # swaps sys.stdout under Pilot, so a mount-time probe fires in
        # headless test drives) keeps direct VexApp(...) construction
        # modal-free.
        self._onboard_prompt = bool(onboard_prompt)
        self._onboard_push_started = False
        # A corrupt conversation found at startup, offered for quarantine
        # once the first refresh lands (never auto-recovered).
        self._corrupt_session_id: Optional[str] = None
        self._startup_recovery_asked = False
        self.last: Dict[str, Any] = {}  # last run info (task_id/diff/status)
        self.last_command: Dict[str, Any] = {}
        self._status: str = _STATUS_IDLE
        self._run: Optional[_RunState] = None
        self._run_stop: Optional[threading.Event] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_ident: Optional[int] = None
        self._exit_code: int = 0
        self._shutting_down = False
        self._spinner_frame = 0
        self._cap = _CapturedConsole()
        self._diagnostic_lines: List[Text] = []
        self._completion_rendered: set = set()
        # VEX-CEILING-10: the run currently left alive by /detach (empty
        # when nothing is detached) and the steering the user queued with
        # ctrl+b, which is delivered at the next safe boundary in order.
        self._detached_task_id: str = ""
        self._queued_steering: List[str] = []
        self._connector_thread: Optional[threading.Thread] = None
        self._connector_cancel: Optional[threading.Event] = None
        self._context_thread: Optional[threading.Thread] = None
        # /diagnostics consults a language server, which starts a subprocess
        # and speaks JSON-RPC. That is worker-thread work: inline it blocks
        # the one interaction the prompt gates at 250 ms.
        self._diagnostics_thread: Optional[threading.Thread] = None
        self._memory_thread: Optional[threading.Thread] = None
        # -- layout authority (cli/design.py) -----------------------------
        # The sidebar MODE, the collapsed sections, and the density are
        # persisted user layout preferences, not run configuration: a value
        # in `harness/config.DEFAULTS` is merged into every task and every
        # eval arm, and "which panes does this person want open" is not a
        # fact about a run. The sidebar mode additionally reads
        # `cli.toggles` (Prompt 04's registry), which owns the key that
        # advances it and its own persisted store; the collapsed sections and
        # the density have no toggle row, so they live here.
        self._prefs: Any = _design.load_preferences(log_root)
        self._toggles: Any = self._resolve_toggles()
        self._sidebar_mode: str = self._effective_sidebar_mode()
        self._density: str = self._prefs.density
        self._statusline_sections: Dict[str, str] = {}
        self._sidebar_allocation: Dict[str, int] = {}
        self._layout = resolve_shell_layout(
            80, 24, sidebar=self._sidebar_mode, density=self._density
        )
        self._narrow_layout = True
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
        self._file_projection_cache: Optional[Tuple[str, int, bool, Dict[str, Any]]] = None
        # Round 2 layout receipts. These are values, not logs: the header fit
        # and each rail's block allocation are the DECISIONS the layout made,
        # kept so a test (or `/doctor`) can assert what the shell chose rather
        # than infer it from a screenshot.
        self._header_fit: Any = None
        self._rail_allocation: Dict[str, int] = {}
        self._context_allocation: Dict[str, int] = {}
        # The rail's own measured height, carried so the sidebar's sections
        # can be budgeted from what the rail's blocks left rather than from
        # the viewport (a section that does not fit is not published, so a
        # sidebar can never push a row off the rail's bottom edge).
        self._rail_rows: int = 0
        # True between a Resize event and the frame after it: the composer is
        # behind, so a rail's region is the previous viewport's and must not
        # be used as a row budget. See `_rail_measurement`.
        self._layout_dirty = False
        # approval requests already prompted (one prompt per request)
        self._approval_handled: set = set()
        # agent-loop require-mode latch (Allow always for this run) +
        # pending approved-plan guidance for the next agent run
        self._pending_agent_guidance: Optional[str] = None
        # prompt-body lines declared by the backend for the next modal
        # (set via the _PROMPT_BODY hook, consumed by _prompt_modal)
        self._pending_prompt_body: Optional[List[Text]] = None
        # True when the last `_prompt_modal` ended on its deadline rather than
        # on an answer. `None` cannot distinguish "the user declined" from
        # "nobody answered", and an approval gate that reports those the same
        # way is claiming a decision nobody made.
        self._prompt_timed_out: bool = False
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

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Return the current bounded TUI interaction and resource metrics."""
        return self.ui_metrics.snapshot()

    @property
    def ui_metrics(self) -> _UIMetrics:
        """The metrics sink the instrumented widgets record into."""
        return self._ui_metrics

    @ui_metrics.setter
    def ui_metrics(self, metrics: _UIMetrics) -> None:
        """Install a metrics sink and RE-POINT the instrumented widgets.

        A bare attribute assignment left `_MeasuredInput` recording into
        the previous sink, so a caller that reset the metrics between
        phases (the eval probe does exactly that) got an empty
        `input_ack_ms` and a silent zero-sample "pass" on the 100 ms gate.
        The sink is therefore a property, and replacing it is a real
        re-binding rather than a field write nobody else can see.
        """
        self._ui_metrics = metrics
        try:
            composer = self.query_one("#vex-input", _MeasuredInput)
        except Exception:
            return
        try:
            composer.bind(metrics)
        except Exception:
            pass

    # -- layout authority (cli/design.py) ---------------------------------
    #
    # Nothing below DECLARES a layout number. Breakpoints, widths, the
    # gutter, the chrome arithmetic, the type scale, the spacing unit, the
    # densities, the anti-clutter threshold, the statusline's sections and
    # its hint limit, and the sidebar's sections all live in `cli/design.py`
    # and are reached through `resolve_shell_layout`, which is a projection
    # of that one authority. The gate that keeps it true is
    # `tests/test_design_layout.py::test_no_layout_constant_lives_outside_design`.

    _SIDEBAR_TOGGLE: ClassVar[str] = "sidebar"

    def _resolve_toggles(self) -> Any:
        """Resolve Prompt 04's toggle registry for this shell. Never raises.

        A missing toggle store must not stop the shell from mounting: the
        layout falls back to `cli.design`'s own default mode, and the status
        line simply has no toggle-derived facts. "The store was unreadable"
        and "the user chose hide" must not read the same.
        """
        try:
            return _toggles.load_settings(
                repo_path=self.repo, session_id=(self.state or {}).get("session_id")
            )
        except Exception:
            return None

    def _effective_sidebar_mode(self) -> str:
        """The sidebar mode this shell is actually in, and why.

        An EXPLICIT toggle choice wins outright, in the toggle module's own
        vocabulary (`auto` / `shown` / `hidden`, translated by
        `design.SIDEBAR_MODE_ALIASES`). "Explicit" means the registry
        reports a SOURCE other than its own default — a value the user
        saved, not the value the registry ships with. The UNSET case falls
        back to `design.DEFAULT_SIDEBAR_MODE`, and that constant documents
        the reason and names the test that pins it.

        Reading the registry default as if the user had chosen it is the
        exact defect this split exists to avoid: a mode the user never
        picked must not quietly take a region away.
        """
        value = None
        try:
            source = str(self._toggles.source(self._SIDEBAR_TOGGLE) or "default")
            if source not in ("", "default", "unknown"):
                value = self._toggles.get(self._SIDEBAR_TOGGLE, None)
        except Exception:
            value = None
        if value is None:
            try:
                value = self._prefs.sidebar_mode
            except Exception:
                value = _design.DEFAULT_SIDEBAR_MODE
        return _design.normalize_sidebar_mode(value)

    def _save_prefs(self) -> None:
        """Persist the collapsed sections and the density. Never raises."""
        try:
            _design.save_preferences(self.log_root, self._prefs)
        except Exception:
            pass

    def set_density(self, density: str) -> str:
        """Set the shell's density and return the resolved name.

        Density is a real row count, not a label: `comfortable` spends a
        three-row composer and a one-row gap between rail blocks, `compact`
        spends a two-row composer and no gap, so a six-block rail gets its
        rows back. Rejects an unrecognised name rather than guessing, and
        says which name it ended on.
        """
        resolved = _design.DENSITY_PROFILES.get(
            str(density or "").strip().lower(), _design.DENSITY_PROFILES["comfortable"]
        )
        self._density = resolved.name
        self._prefs = self._prefs.with_density(resolved.name)
        self._save_prefs()
        self._apply_responsive_layout(self._layout.width, self._layout.height)
        self._render_header()
        self._rerender_rails()
        return self._density

    def cycle_density(self) -> str:
        """Switch to the other density and return the new name."""
        return self.set_density("compact" if self._density != "compact" else "comfortable")

    def toggle_section(self, key: str) -> bool:
        """Collapse or expand one sidebar section; return the new state.

        Only a section the anti-clutter rule RENDERS can be collapsed, so
        this returns False for a section with nothing in it — a triangle on
        a section that is not there is a control for nothing.
        """
        name = str(key or "")
        if name not in _design.SIDEBAR_SECTIONS:
            return False
        self._prefs = self._prefs.toggled(name)
        self._save_prefs()
        self._render_sidebar_sections(self._run)
        return self._prefs.is_collapsed(name)

    def sidebar_sections(self) -> Tuple[Any, ...]:
        """The sidebar's declared sections, in reading order."""
        return _design.SIDEBAR_SECTIONS

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield ShellHeader(id="vex-header")
        with Horizontal(id="vex-mid"):
            yield EventFeed(
                id="vex-body",
                markup=True,
                wrap=True,
                highlight=False,
                auto_scroll=True,
                max_lines=_TRANSCRIPT_MAX_LINES,
                min_width=0,
            )
            yield PlanRail(id="vex-side")
            yield ContextPanel(id="vex-context")
        yield Static("", id="vex-runline")
        yield Static("", id="vex-announce")
        yield StreamPaint(id="vex-stream")
        # The statusline: the live run-context facts, each with the key that
        # reaches it, dropped by priority as the terminal narrows. It is
        # `display: none` whenever it has nothing TRUE to say, which is the
        # whole point — a `0 queued` is a claim, and there is nothing queued.
        yield Static("", id="vex-statusline")
        with Vertical(id="vex-inputwrap"):
            yield _MeasuredInput(
                placeholder=_commands.argument_hint(""),
                id="vex-input",
                metrics=self.ui_metrics,
            )
        yield ShellFooter("", id="vex-hints")

    def on_mount(self) -> None:
        ui.set_active_tokens(self._tokens)
        _refresh_role_map()
        try:
            self._cap.set_width(self.size.width)
        except Exception:
            pass
        self._apply_responsive_layout(self.size.width or 80, self.size.height or 24)
        self._render_header()
        self._set_hints()
        # Warm the palette's repo-file cache while the shell is idle so the
        # first ctrl+p is a pure in-memory open (never a git subprocess on
        # the UI thread).
        self.call_later(self._prewarm_palette_files)
        first_launch = _iv._is_first_launch(self.log_root)
        self._print_splash(first_launch=first_launch)
        if first_launch and self._first_run_note:
            self.transcript(self._first_run_note)
        # Persistent conversation + memory-first: load (or create) the
        # session state file, then surface repo-scoped decisions +
        # structural index state automatically (best-effort muted lines;
        # a prior compacted summary is shown, not hidden).
        try:
            from cli.session import (
                load_latest_session as _load_conversation,
            )
            from cli.session import (
                session_memory_brief as _memory_brief,
            )
            from cli.session import (
                startup_recovery_candidate as _recovery_candidate,
            )

            # Startup recovery for a corrupt conversation: surface it and
            # offer the quarantine. The bytes are always kept; declining
            # simply continues without that conversation.
            _candidate = _recovery_candidate(self.log_root, self.repo)
            if _candidate:
                # UI thread, so `_transcript_ui` — not `_safe_call`, whose
                # `call_from_thread` raises here and is swallowed. This
                # notice is a data-loss warning; silently dropping it is
                # how a corrupt session used to disappear without a trace.
                self._transcript_ui(
                    f"[vex.warn]session {escape(str(_candidate['session_id']))} is "
                    f"unreadable[/] [vex.muted]({escape(str(_candidate['error'])[:160])})[/]"
                )
                self._transcript_ui(
                    "[vex.muted]recovering quarantines a copy of the file and "
                    "starts a clean conversation; the original is never deleted. "
                    f"Type [vex.accent]/recover {escape(str(_candidate['session_id']))} "
                    "--fresh[/] to do it now[/]"
                )
                self._corrupt_session_id = str(_candidate["session_id"])
                if self._onboard_prompt:
                    self.call_after_refresh(self._prompt_startup_recovery)
            self.conversation = _load_conversation(self.log_root, self.repo)
            self.state["conversation"] = self.conversation
            if first_launch and self._onboard_prompt:
                def load_memory() -> None:
                    try:
                        mem_lines = _memory_brief(self.repo, self.log_root)
                        if mem_lines:
                            self._safe_call(
                                self.transcript,
                                f"[vex.muted]context[/] [{ui.TEXT_PRIMARY}]"
                                f"{escape(str(mem_lines[0])[:180])}[/]",
                            )
                    except Exception:
                        pass

                self._memory_thread = threading.Thread(
                    target=load_memory,
                    name="vex-memory-receipt",
                    daemon=True,
                )
                self._memory_thread.start()
            _summary = str((self.conversation or {}).get("summary") or "").strip()
            if first_launch and _summary:
                self.transcript(
                    f"[vex.muted]session context: {escape(_summary[:200])}[/]"
                )
            try:
                from cli.session import (
                    build_session_context,
                    format_session_context_status,
                )

                _context_status = format_session_context_status(
                    build_session_context(
                        self.conversation,
                        repo=self.repo,
                        task={"issue_text": "session context"},
                        token_budget=int(self.file_config.get("session_context_tokens", 12000)),
                    )
                )
                if first_launch and _context_status:
                    self.transcript(
                        f"[vex.muted]context: {escape(str(_context_status)[:220])}[/]"
                    )
            except Exception:
                pass
        except Exception:
            self.conversation = None
        self.query_one("#vex-input", Input).focus()
        self.set_interval(1.0, self._tick_spinner)
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
            if self._onboard_prompt and not self._onboard_push_started:
                from cli import onboard as _ob

                if _ob.needs_onboarding():
                    self._onboard_push_started = True
                    self.push_screen(_OnboardScreen(), self._onboard_done)
        except Exception:
            pass
        # R2-17 (item 5): the first-run orientation. The splash is the
        # brand; this is the ORIENTATION — what to type, what the product
        # promises about verification, and where the evidence lands.
        # Three beats, because a first-run screen that is itself a wall
        # fails exactly the way the old `/help` did.
        if first_launch:
            self._print_first_run()
        else:
            # R2-17 (item 3): the resume briefing. One honest screen on
            # start — what happened, what it cost, what is UNVERIFIED, and
            # what happens next — re-derived from the previous run's own
            # journal. Skipped on a genuine first launch, where there is
            # nothing to brief and a briefing of zeros would be a
            # fabricated status.
            self._print_resume_briefing()

    def _print_first_run(self) -> None:
        """Render the first-run orientation into the transcript. Never raises."""
        try:
            for line in _iv.render_first_run(
                self.repo,
                self.log_root,
                model=ui.strip_ansi(
                    _iv._session_model_label(self.state, self.file_config)
                ),
            ):
                self._transcript_ui(line)
        except Exception:
            pass

    def _print_resume_briefing(self) -> None:
        """Render the resume briefing into the transcript. Never raises."""
        try:
            for line in _iv.render_resume_briefing(
                self.log_root, repo=self.repo
            ):
                self._transcript_ui(line)
        except Exception:
            pass

    def _prompt_startup_recovery(self) -> None:
        """Offer to quarantine a corrupt conversation found at startup.

        Runs after the first refresh so the modal has a mounted frame. Esc
        or "n" leaves the file exactly where it is; "y" quarantines a copy
        and reloads a clean conversation. Both paths are recoverable and
        neither deletes anything.
        """
        if self._startup_recovery_asked or not self._corrupt_session_id:
            return
        self._startup_recovery_asked = True
        session_id = self._corrupt_session_id
        try:
            self.push_screen(
                _ConfirmScreen(
                    f"session {session_id} is unreadable - quarantine it?",
                    [
                        Text(
                            "Recovery renames the snapshot and its journal to "
                            f"{session_id}.corrupt-<timestamp> and starts a clean "
                            "conversation. The original bytes are kept.",
                        )
                    ],
                    default="n",
                ),
                lambda answer: self._finish_startup_recovery(session_id, answer),
            )
        except Exception:
            self._startup_recovery_asked = False

    def _finish_startup_recovery(self, session_id: str, answer: Any) -> None:
        """Apply the startup-recovery answer (quarantine, or leave it)."""
        if str(answer or "").strip().lower() not in ("y", "yes"):
            self.transcript(
                "[vex.muted]left "
                f"{escape(session_id)} untouched - `/recover {escape(session_id)} "
                "--fresh` quarantines it any time[/]"
            )
            return
        try:
            from cli.session import load_or_create, recover_corrupt_session

            report = recover_corrupt_session(
                self.log_root, session_id, self.repo, strategy="fresh"
            )
            self.conversation = load_or_create(
                self.log_root, self.repo, session_id, strict=False
            )
            self.state["conversation"] = self.conversation
            self.transcript(
                "[vex.ok]recovered[/] [vex.muted]quarantined to "
                f"{escape(str(report.get('quarantine_path') or ''))}[/]"
            )
        except Exception as exc:
            self.transcript(f"[vex.error]recovery failed:[/] {escape(str(exc))}")
        self._corrupt_session_id = None

    def _onboard_done(self, saved: Any) -> None:
        """Handle a saved, skipped, or forwarded onboarding result."""
        try:
            if isinstance(saved, str) and saved.strip().startswith("/"):
                self._handle_line(saved.strip())
                return
            if saved:
                _iv._reload_file_config(
                    self.state, self.file_config, start=self.repo
                )
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
            self._pending_prompt_body = [
                Text(ui.strip_ansi(line)) for line in body_lines
            ][:25]
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
        if self._connector_cancel is not None:
            self._connector_cancel.set()
        self._context_thread = None
        self._memory_thread = None
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._interrupt_worker()
        if ui.active_tokens() == self._tokens:
            ui.set_active_tokens(self._tokens_before)

    # -- rendering helpers (all updates go through these, in place) ------

    def _render_header(self, status_text: str = "") -> None:
        """Render the persistent header while keeping the active task discoverable.

        The status word is part of the header's BUDGET, not decoration beside
        it: the task and status chips are reserved columns, and a header that
        budgets itself while ignoring the status is how the two chips end up
        welded into one run-on token. The fit is therefore resolved once,
        here, from the word that is actually about to be published.
        """
        model = ui.strip_ansi(_iv._session_model_label(self.state, self.file_config))
        status = ui.strip_ansi(status_text or self._status or _STATUS_IDLE)
        header = HeaderModel(
            version=self.version,
            model=model,
            repo=self.repo,
            mode=str(self.state.get("mode") or "auto"),
            task_id=self._active_task_id(),
            layout=self._layout,
            status=status,
        )
        fit = fit_header(header, status)
        self._header_fit = fit
        try:
            self.query_one("#vex-brand", Static).update(_m(header.brand_markup(status)))
            self.query_one("#vex-task", Static).update(_m(header.task_markup(status)))
        except Exception:
            pass
        self._set_status(status)

    def _set_status(self, text: str) -> None:
        self._status = text
        style = (
            "vex.warn"
            if "wait" in text.lower() or "approval" in text.lower()
            else "vex.ok"
            if text.lower() in ("success", "completed")
            else "vex.running"
            if text.lower() == "running"
            else "vex.muted"
        )
        try:
            status_widget = self.query_one("#vex-status", Static)
            status_widget.update(_m(f"[{style}]{ui.strip_ansi(text)}[/]"))
            try:
                status_widget.tooltip = f"Status: {ui.strip_ansi(text)}"
            except Exception:
                pass
        except Exception:
            pass

    # -- announcements (Terminal 06: screen readers / dumb terminals) ----
    #
    # A `#vex-status` chip is re-rendered IN PLACE, which writes no new
    # bytes to the terminal: a screen reader reading the buffer sees
    # nothing, and a `TERM=dumb` console sees a static word. So every
    # meaningful transition is ALSO written as a plain, uncoloured line
    # of NEW output through `_announce`, and mirrored into the transcript
    # for the transitions a user must not be able to miss.
    #
    # Two rules make this usable rather than noise:
    #   1. `AnnouncementGate` speaks once per TRANSITION, not per repaint.
    #   2. The sentence is plain text by construction — no glyph, no
    #      color, no markup — so the same string is correct on a
    #      truecolor TTY, under `NO_COLOR`, and under `TERM=dumb`.

    def _announce(self, key: Any, text: Any, *, transcript: bool = False) -> str:
        """Publish one state change and return the sentence, or "".

        `key` is the transition identity (the phase string, the approval
        effect signature, the terminal status). `transcript=True` also
        writes the sentence into the scrollback, which is what makes a
        screen reader read it: new terminal output, not an in-place
        repaint. Only transitions the user must not miss use it, or the
        transcript becomes a second status bar.
        """
        sentence = self._announcements.offer(key, text)
        if not sentence:
            return ""
        try:
            widget = self.query_one("#vex-announce", Static)
            widget.update(Text(sentence, style=ui.TEXT_PRIMARY))
            widget.styles.display = "block"
        except Exception:
            pass
        if transcript:
            self.transcript(sentence)
        return sentence

    def _clear_announcement(self) -> None:
        """Hide the announcement region and forget the gate.

        Called when a run ends: leaving the last sentence pinned forever
        makes a stale outcome look current, which is the same defect class
        as a run line that never returns to idle.
        """
        self._announcements.reset()
        try:
            widget = self.query_one("#vex-announce", Static)
            widget.update(Text(""))
            widget.styles.display = "none"
        except Exception:
            pass

    def announcement_text(self) -> str:
        """Return the currently announced sentence (for tests and probes)."""
        try:
            return str(self.query_one("#vex-announce", Static).visual).strip()
        except Exception:
            return ""

    def focus_order(self, *, modal: bool = False) -> Dict[str, Any]:
        """Return the keyboard-navigation report for the live surface.

        The declared order lives in `cli.a11y` (the product, not the
        test), and this resolves it against what is actually mounted, so
        "every interactive element is keyboard reachable" is a value the
        shell can report rather than a claim in a docstring.
        """
        try:
            mounted = [
                str(getattr(widget, "id", "") or "")
                for widget in self.screen.query("*")
            ]
        except Exception:
            mounted = []
        return dict(_a11y.focus_order_report(mounted, modal=modal))

    def _set_hints(self, text: Optional[str] = None) -> None:
        try:
            footer = self.query_one("#vex-hints", ShellFooter)
        except Exception:
            return
        try:
            if text is None:
                active = self._run is not None or bool(
                    self._worker_thread is not None and self._worker_thread.is_alive()
                )
                footer.set_context(
                    self._layout,
                    active=active,
                    waiting=self._status == _STATUS_WAITING,
                )
            else:
                footer.update(text)
        except Exception:
            return

    def _apply_responsive_layout(self, width: int, height: int = 24) -> None:
        """Apply the layout authority's decision to the mounted shell.

        Every number comes from `cli/design.py` through
        `resolve_shell_layout`; the only thing decided here is HOW the
        decision is applied to mounted widgets. The statusline's own row
        count is an INPUT to the resolution rather than a consequence of it,
        because a statusline that says nothing must not cost a row.
        """
        layout = resolve_shell_layout(
            width,
            height,
            sidebar=self._sidebar_mode,
            density=self._density,
            statusline_rows=1 if self._statusline_sections else 0,
        )
        self._layout = layout
        self._narrow_layout = not layout.plan_rail_visible
        gap = _design.rail_block_gap(self._density)
        try:
            side = self.query_one("#vex-side", PlanRail)
            side.styles.width = layout.plan_rail_width
            side.styles.min_width = layout.plan_rail_width
            side.styles.padding = 0 if not layout.plan_rail_visible else (0, 1)
            for selector in (
                "#vex-side-header",
                "#vex-todo",
                "#vex-plan-checkpoints",
                "#vex-side-label",
                "#vex-side-status",
            ):
                self.query_one(selector, Static).styles.display = (
                    "block" if layout.plan_rail_visible else "none"
                )
            # The rail's own block gaps follow the density, which is what
            # makes compact a measurable number of rows rather than a label.
            for selector in ("#vex-todo", "#vex-plan-checkpoints"):
                self.query_one(selector, Static).styles.margin_bottom = gap
            for key in _design.SIDEBAR_SECTIONS:
                self.query_one(f"#vex-sidebar-{key}", Static).styles.margin_bottom = gap
        except Exception:
            pass
        try:
            context = self.query_one("#vex-context", ContextPanel)
            context.styles.width = layout.context_rail_width
            context.styles.min_width = layout.context_rail_width
            context.styles.padding = 0 if not layout.context_rail_visible else (0, 1)
            for selector in (
                "#vex-context-header",
                "#vex-context-usage",
                "#vex-context-files",
                "#vex-context-diagnostics",
                "#vex-context-relevant",
                "#vex-context-sources",
                "#vex-context-legend",
            ):
                self.query_one(selector, Static).styles.display = (
                    "block" if layout.context_rail_visible else "none"
                )
            # The context rail's block gaps follow the density too, and the
            # LAST block carries none — six blocks would otherwise spend five
            # rows on nothing at compact.
            for selector in (
                "#vex-context-usage",
                "#vex-context-files",
                "#vex-context-diagnostics",
                "#vex-context-relevant",
                "#vex-context-sources",
            ):
                self.query_one(selector, Static).styles.margin_bottom = gap
            self.query_one("#vex-context-legend", Static).styles.margin_bottom = 0
        except Exception:
            pass
        try:
            self.query_one("#vex-inputwrap", Vertical).styles.height = layout.composer_height
        except Exception:
            pass
        self._render_statusline()
        self._set_hints()
        try:
            self._cap.set_width(layout.width)
        except Exception:
            pass

    def _capture_resize_state(self) -> Dict[str, Any]:
        """Capture focus and composer values across every mounted screen."""
        screens = tuple(self.screen_stack)
        inputs: List[Tuple[Any, str, str, int]] = []
        for screen in screens:
            try:
                for widget in screen.query(Input):
                    widget_id = str(getattr(widget, "id", "") or "")
                    if not widget_id:
                        continue
                    cursor = int(getattr(widget, "cursor_position", 0) or 0)
                    inputs.append((screen, widget_id, str(widget.value or ""), cursor))
            except Exception:
                continue
        return {
            "screens": screens,
            "inputs": inputs,
            "focused": self.focused,
            "task_id": self._active_task_id(),
        }

    def _restore_resize_state(self, state: Dict[str, Any]) -> None:
        """Restore captured focus and text after a viewport change."""
        for screen, widget_id, value, cursor in state.get("inputs", []):
            if screen not in self.screen_stack:
                continue
            try:
                widget = next(
                    item
                    for item in screen.query(Input)
                    if str(getattr(item, "id", "") or "") == widget_id
                )
                current_value = str(widget.value or "")
                if current_value and current_value != value:
                    continue
                widget.value = value
                try:
                    widget.cursor_position = min(cursor, len(value))
                except Exception:
                    pass
            except Exception:
                continue
        focused = state.get("focused")
        try:
            if focused is not None and focused.is_attached:
                focused.focus()
        except Exception:
            pass

    def _resize_from_modal(self, width: int, height: int) -> None:
        """Apply the shell's responsive policy when the active modal resizes.

        The rails are only re-rendered when the resolved LAYOUT actually
        changed. A modal fits itself on mount (twice, historically) and
        again on every resize, and re-projecting the plan rail and the
        context rail through the journal each time is duplicated work on
        the UI thread for a pixel-identical result. Terminal 06 measured
        the fit path; the guard is the difference between one rail
        re-render per modal and several.
        """
        layout = resolve_shell_layout(width, height)
        changed = layout != self._layout
        self._apply_responsive_layout(width, height)
        if not changed:
            return
        self._render_header()
        if self._run is not None:
            self._render_side(self._run)
            self._render_context(self._run)

    def on_resize(self, event: events.Resize) -> None:
        """Re-layout active and modal states without remounting persistent controls."""
        started = time.perf_counter()
        state = self._capture_resize_state()
        self._last_resize_state = state
        self._apply_responsive_layout(
            getattr(event.size, "width", 0) or 80,
            getattr(event.size, "height", 0) or 24,
        )
        self._render_header()
        # The rails are budgeted against their own MEASURED height, and this
        # handler runs before the compositor has applied the new one, so they
        # are repainted after the layout settles. The header does not measure
        # anything, so it is repainted here.
        self._layout_dirty = True
        self.call_after_refresh(self._settle_layout)
        self.call_after_refresh(self._restore_resize_state, state)
        self.ui_metrics.observe("resize_recovery_ms", started)


    def _render_run(self, run: Optional[_RunState] = None) -> None:
        """Repaint the run-line widget IN PLACE (Task B core).

        Also repaints the STREAM PAINT (`#vex-stream`) from the same
        coalescer frame, which is where streamed model text finally gets
        a home. The paint is a SEPARATE widget precisely so the run
        line's `height: 1` contract survives: the live text never makes
        the run line multi-line, and never scrolls the transcript.
        """
        started = time.perf_counter()
        run = run or self._run
        try:
            rl = self.query_one("#vex-runline", Static)
            if run is None:
                if self._pending_run_text:
                    rl.update(_m(self._pending_run_text))
                    rl.styles.display = "block"
                else:
                    self._hide_runline()
                self._paint_stream("")
                return
            rl.update(_m(run.line(self._spinner_frame, reduced_motion=not self._motion_enabled)))
            rl.styles.display = "block"
            # `line()` already drained the coalescer, so this reads the
            # settled frame rather than consuming a second one. A caller
            # that rendered the run line without it is covered by `line`.
            self._paint_stream(run.stream_text if run.streaming else "")
            self._announce_phase(run)
            if (
                run.last_event_timestamp is not None
                and run.last_event_timestamp != self._last_rendered_event_timestamp
            ):
                self.ui_metrics.observe_event(run.last_event_timestamp)
                self._last_rendered_event_timestamp = run.last_event_timestamp
        except Exception:
            pass
        finally:
            self.ui_metrics.observe("ui_thread_stall_ms", started)

    def _announce_phase(self, run: _RunState) -> str:
        """Announce the live phase, and keep it to one announcement per change.

        Two distinct states get different sentences on purpose, because on
        screen they are easy to confuse and to a reader they would be
        identical: a **running tool** names the tool and the elapsed
        seconds (so "slow" is distinguishable from "wedged"), and
        everything else names the phase. The key is the pending label or
        the phase, so a long `model: thinking` stretch speaks once rather
        than on every 125 ms repaint.

        The guard at the top is the one that matters: once a run's
        OUTCOME has been announced, no live phase may speak again. The
        repaint timer keeps firing after the card is drawn, and a
        "Tool running: ..." line landing on top of "Task finished: ... not
        verified" is the exact way a finished run reads as still running.
        The outcome is the last thing said about a run.
        """
        try:
            settled = str(getattr(self, "_settled_outcome_task", "") or "")
            if settled and settled == str(getattr(run, "task_id", "") or ""):
                return ""
            if run.cancel_requested:
                return self._announce(
                    "cancel", _a11y.announce_cancel_requested(), transcript=True
                )
            pending = str(getattr(run, "pending_label", "") or "")
            if pending:
                elapsed: Optional[int] = None
                since = getattr(run, "pending_since", None)
                if since is not None:
                    elapsed = max(0, int(time.monotonic() - float(since)))
                # The pending label already reads as a sentence
                # ("running a command · pytest -q"); splitting it keeps
                # the elapsed seconds attached to the right half instead
                # of repeating the whole phrase as a "tool name".
                action, command = _a11y.split_action(pending)
                return self._announce(
                    ("pending", pending),
                    _a11y.announce_tool_pending(action, command, elapsed_s=elapsed),
                )
            phase = str(getattr(run, "phase", "") or "working")
            return self._announce(
                ("phase", phase), _a11y.announce_phase(phase, events=run.events)
            )
        except Exception:
            return ""

    def _paint_stream(self, text: str) -> None:

        """Push untrusted stream text into the bounded paint widget.

        The widget's `update_stream` returns `rich.text.Text` and never a
        markup string, so a model reply containing `[`, `[/]`, or
        `[vex.*]` renders literally instead of closing a tag. Every
        failure is swallowed here on purpose: a paint problem must never
        take a live run down.

        The widget reference is CACHED on first use. `_render_run` runs on
        a 0.125s timer plus once per journal event, and a `query_one` per
        repaint is a real cost on that path for no benefit — the mount is
        the only moment the id resolves. A stale or missing reference just
        clears the cache and lets the next repaint retry.
        """
        try:
            widget = getattr(self, "_stream_widget", None)
            if widget is None or not widget.is_attached:
                widget = self.query_one("#vex-stream", StreamPaint)
                self._stream_widget = widget
            widget.update_stream(text)
        except Exception:
            try:
                self._stream_widget = None
            except Exception:
                pass

    def _hide_runline(self) -> None:
        self._pending_run_text = None
        try:
            self.query_one("#vex-runline", Static).styles.display = "none"
        except Exception:
            pass
        self._paint_stream("")

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

    def _agent_projection_text(self, run: _RunState) -> List[str]:
        """Render the live agent fact set without a fix-loop checklist."""
        snapshot = run.projection.snapshot()
        lines = _rv.status_lines(snapshot, mode=run.mode, live=run.projection.status == "running")
        return lines[:14]

    def _render_side(self, run: Optional[_RunState] = None) -> None:
        """Repaint the plan/checkpoint rail from the current journal snapshot.

        The rail is given its rows as a MEASURED budget: the block that
        carries mode/state/verify/cost/error is admitted first, the plan list
        is the block that gives way, and anything that does not fit is
        bounded with `+N more` rather than clipped by the compositor.
        """
        run = run or self._run
        try:
            side = self.query_one("#vex-side", PlanRail)
            if run is None:
                # Idle: the column collapses (see `_teardown_side` for the
                # test that pins it and the handoff that would change it),
                # so publishing the sections now would be work nobody sees.
                side.styles.display = "none"
                self._render_statusline()
                return
            self._apply_responsive_layout(self._layout.width, self._layout.height)
            side.styles.display = "block" if self._layout.plan_rail_visible else "none"
            heading = _m(
                f"[vex.accent]PLAN[/] [vex.muted]{ui.DOT}[/] "
                f"[{ui.TEXT_PRIMARY}]{escape(run.task_id)}[/]"
            )
            todo_lines: List[str] = []
            if run.todo.steps:
                done, total = run.todo.progress()
                todo_lines.append(
                    f"[{ui.TEXT_SECONDARY}]todo[/] [{ui.TEXT_PRIMARY}]{done}/{total}[/]"
                )
                enc_ok = ui._enc_ok("✔")
                for step in run.todo.steps[:8]:
                    state = run.todo.state_of(step)
                    mark, color = _todo_marks().get(
                        state,
                        ("○", ui.active_tokens()["text_secondary"]),
                    )
                    if not enc_ok:
                        mark = {"✔": "v", "✘": "x", "▸": ">", "↷": "-", "○": "o"}.get(
                            mark, mark
                        )
                    description = step.description or f"step {step.sid}"
                    clipped = (
                        description
                        if len(description) <= 30
                        else description[:29] + "…"
                    )
                    state_label = {
                        _rv.PENDING: "pending",
                        _rv.ACTIVE: "active",
                        _rv.DONE: "done",
                        _rv.SKIPPED: "skipped",
                        _rv.FAILED: "failed",
                    }.get(state, state)
                    todo_lines.append(
                        f"[{color}]{mark} {state_label}[/] [{color}]{escape(clipped)}[/]"
                    )

            elif run.mode not in ("fix",):
                todo_lines.append("[vex.accent]agent[/]")
                todo_lines.extend(self._agent_projection_text(run))
                todo_lines = _drop_rail_duplicate_rows(todo_lines)
            snapshot = run.projection.snapshot()
            checkpoints = list(snapshot.get("checkpoints") or [])
            checkpoint_lines = [
                f"[{ui.TEXT_SECONDARY}]checkpoints[/] "
                f"[{ui.TEXT_PRIMARY}]{len(checkpoints)}[/]"
            ]
            for checkpoint in checkpoints[:3]:
                sequence = checkpoint.get("last_event_sequence", "?")
                token = str(checkpoint.get("resume_token") or "available")
                checkpoint_lines.append(
                    f"[vex.muted]· {escape(str(sequence))} {escape(token[:18])}[/]"
                )
            elapsed = _rv.fmt_elapsed(time.monotonic() - run.started_at)
            machine = self._machine_state or "unknown"
            calls = str(run.calls) if snapshot.get("model_calls_known") else "unknown"
            token_count = f"{run.tokens:,}" if snapshot.get("tokens_known") else "unknown"
            cost = ui.fmt_cost(run.cost) if snapshot.get("cost_known") else "unknown"
            status_lines = [
                f"[vex.muted]mode [/] [vex.accent2]{escape(run.mode)}[/]",
                f"[vex.muted]state [/] [vex.running]{escape(machine)}[/]",
                f"[vex.muted]time [/] [{ui.TEXT_PRIMARY}]{elapsed}[/]",
                f"[vex.muted]calls [/] [{ui.TEXT_PRIMARY}]{calls}[/]",
                f"[vex.muted]tokens [/] [{ui.TEXT_PRIMARY}]{token_count}[/]",
                f"[vex.muted]cost [/] [vex.accent2]{cost}[/]",
            ]
            # `action`, `files`, and `verify` are DELIBERATELY not repeated
            # here. The plan/agent projection above already states all three
            # in the projection's own vocabulary, and the round-2 audit
            # rendered both spellings in one column — `verify PASS` directly
            # above `verify verified`, `files src/parser.py` above
            # `files 1`. Two words for one fact is how a reader ends up
            # unsure which one the product means, so this block is the run's
            # METERS and its exceptions and the projection above it is the
            # narrative. The projection is the authority: it is
            # `runview.status_lines`, and it cannot report a verification
            # state the journal does not carry.
            try:
                from cli.commands import mode_spec

                profile = mode_spec(run.mode)
                if profile is not None:
                    status_lines.append(
                        f"[vex.muted]tools [/] [{ui.TEXT_PRIMARY}]{escape(', '.join(profile.visible_tools[:5]))}[/]"
                    )
            except Exception:
                pass
            if run.projection.approval != "not required":
                approval_state = str(snapshot.get("approval") or "unknown")
                approval_line = (
                    f"[vex.muted]approval [/] [vex.warn]{escape(approval_state)}[/]"
                    if approval_state == "waiting"
                    else f"[vex.muted]approval [/] [{ui.TEXT_PRIMARY}]{escape(approval_state)}[/]"
                )
                note = str(snapshot.get("approval_note") or "").strip()
                if note and approval_state == "waiting":
                    # A pending approval must say WHAT is waiting. The policy
                    # engine's own reason is the only explanation on offer,
                    # and dropping it is how a waiting run reads as refused.
                    approval_line += f"[vex.muted] - {escape(note[:60])}[/]"
                status_lines.append(approval_line)
            unmapped_kinds = list(snapshot.get("unmapped_kinds") or [])
            if unmapped_kinds:
                # Explicit unknown state: the journal carries events this
                # build cannot render. Naming them is better than reporting a
                # clean-looking run we only partially understood.
                unreadable = "{0} unreadable event(s): {1}".format(
                    snapshot.get("unmapped_events"),
                    ", ".join(str(item) for item in unmapped_kinds)[:44],
                )
                status_lines.append(
                    f"[vex.muted]journal [/] [vex.warn]{escape(unreadable)}[/]"
                )
            if run.projection.last_error:
                status_lines.append(
                    f"[vex.muted]error [/] [vex.error]{escape(run.projection.last_error[:80])}[/]"
                )
            # The heading, the `status` label, and the two blocks' margins are
            # the rail's own fixed rows, so the budget the blocks get is what
            # is left of the rail after them. Measuring the rail's real region
            # (and falling back to the shell's chrome arithmetic) is what makes
            # the allocation a measurement rather than a guess at a viewport
            # that has not rendered yet. A COLLAPSED rail is published whole:
            # nothing is on screen to be squeezed, and a consumer that reads
            # the rail while it is hidden — the palettes, the headless
            # projection, the tests — sees the same facts a visible rail does.
            rows, content_width = self._rail_measurement("#vex-side")
            gap = _design.rail_block_gap(self._density)
            self._rail_allocation = side.update_content(
                heading,
                _m("\n".join(todo_lines)),
                _m("\n".join(checkpoint_lines)),
                _m("\n".join(status_lines)),
                rows=None if not self._layout.plan_rail_visible else max(0, rows - 2),
                content_width=content_width,
                # Real ENTRY counts, not line counts: the plan block is the
                # run's projection rows, the checkpoints block is the
                # checkpoints themselves (zero of them is a `checkpoints 0`
                # heading and no entries at all), and the status block is
                # its meters and exceptions.
                entry_counts={
                    "plan": len(todo_lines),
                    "checkpoints": len(checkpoints),
                    "status": len(status_lines),
                },
                gap=gap,
            )
            self._rail_rows = rows
        except Exception:
            pass
        # The sidebar's own sections are published AFTER the rail's three
        # blocks, so a section can never move `#vex-side-status` and its
        # rendered-row receipt by a single row.
        self._render_sidebar_sections(run)
        self._render_statusline()

    # -- the sidebar's own sections, and the statusline -------------------
    #
    # Both are governed by `cli/design.py`: the section list, the
    # anti-clutter threshold, the collapse keys, the statusline's priority
    # order and its explicit hint limit. Neither renderer decides what is
    # TRUE — it reports what the shell already knows, and a fact it cannot
    # establish is left out rather than printed as `unknown`.

    #: The sidebar section headings, in the product's own words. Declared
    #: here because the WORDS are the product's, while the section LIST and
    #: the collapse keys are the layout authority's.
    _SIDEBAR_TITLES: ClassVar[Dict[str, str]] = {
        "session": "SESSION",
        "context": "CONTEXT",
        "mcp": "MCP",
        "lsp": "LSP",
        "todo": "TODO",
        "files": "MODIFIED FILES",
        "startup": "GETTING STARTED",
    }

    def _sidebar_facts(self, run: Optional[_RunState]) -> Dict[str, List[str]]:
        """The sidebar's facts, per section, as PLAIN strings.

        Plain on purpose: these strings cross into Textual's markup parser
        from data this shell did not author (a model, a repository, a file
        path), and the parser eats a `[` in a name. Every value is escaped
        at the point it becomes markup, in `_render_sidebar_sections`.

        A fact that is not established is OMITTED rather than printed as
        `unknown`: an omitted fact makes its section fall under the
        anti-clutter rule, which is the honest way for a thin section to
        disappear.
        """
        facts: Dict[str, List[str]] = {key: [] for key in _design.SIDEBAR_SECTIONS}
        snapshot = run.projection.snapshot() if run is not None else {}

        # -- session: what this conversation is, in three words or none
        title = ""
        try:
            turns = (self.conversation or {}).get("turns") or []
            for turn in turns:
                if str(turn.get("role") or "") in ("user", "human"):
                    title = str(turn.get("text") or turn.get("content") or "").strip()
                    if title:
                        break
        except Exception:
            title = ""
        if not title:
            try:
                summary = str((self.conversation or {}).get("summary") or "").strip()
                title = summary
            except Exception:
                title = ""
        if title:
            facts["session"].append(title.splitlines()[0][:60])
        task_id = str(run.task_id) if run is not None else self._active_task_id()
        if task_id:
            facts["session"].append(task_id)
        if run is not None:
            facts["session"].append(str(run.mode))

        # -- context: tokens, % of window used, $ spent (all three or none)
        if run is not None:
            if snapshot.get("tokens_known") and run.tokens:
                facts["context"].append(f"{run.tokens:,} tokens")
            window = self._context_window_tokens()
            if window and snapshot.get("tokens_known") and run.tokens:
                facts["context"].append(f"{min(100, int(run.tokens * 100 / window))}% of window")
            if snapshot.get("cost_known"):
                facts["context"].append(ui.fmt_cost(run.cost))

        # -- MCP: the servers themselves, with the counts on the heading
        for row in self._mcp_rows():
            name = str(row.get("label") or row.get("server") or "").strip()
            if not name:
                continue
            state = str(row.get("status") or row.get("state") or "").strip().lower()
            facts["mcp"].append(f"{name} {state}".strip()[:60])

        # -- LSP: the state receipt, not a claim of cleanliness
        lsp = self._lsp_facts()
        if lsp:
            facts["lsp"].extend(lsp)

        # -- todo: only when there is work left, and only when the plan
        #    block above has not already stated it (one fact, one place)
        if run is not None and run.todo.steps:
            done, total = run.todo.progress()
            if not self._plan_block_states_todo() and (total and done < total):
                facts["todo"].append(f"{done}/{total} done")
                for step in run.todo.steps[:6]:
                    mark = _todo_marks().get(run.todo.state_of(step), ("", ""))[0]
                    description = step.description or f"step {step.sid}"
                    facts["todo"].append(f"{mark} {description}".strip()[:60])

        # -- modified files: only when the context rail is not already saying it
        if run is not None and not self._layout.context_rail_visible:
            for item in self._file_projection(run).get("file_changes") or []:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                summary = str(item.get("summary") or "").strip()
                facts["files"].append(f"{path} {summary}".strip()[:60])

        # -- the getting-started card: ONLY when nothing is connected.
        # The command named is `/connect`, which is the credential store
        # Prompt 02 built (`cli/auth.py`); naming `/login` here would point
        # at a flow that tests first and saves nothing on failure.
        if self._needs_provider():
            facts["startup"] = [
                "no provider is connected",
                "/connect  add a provider",
                "vex login  (or /model to pick one)",
            ]
        return facts

    def _context_window_tokens(self) -> int:
        """The model's context window in tokens, or 0 when it is unknown.

        `0` is the honest answer when the window is not resolvable: a
        percentage of an unknown window is a fabricated number, so the
        "N% of window" line is omitted rather than guessed.
        """
        try:
            window = int((self.state or {}).get("context_window") or 0)
            if window > 0:
                return window
        except Exception:
            pass
        return 0

    def _needs_provider(self) -> bool:
        """Whether the shell is certain that no provider is connected.

        Certain, not "not connected": if the check cannot run at all, the
        answer is False, because a Getting-started card shown because we
        could not look is exactly the clutter this round removes.
        """
        try:
            from cli import onboard as _ob

            return bool(_ob.needs_onboarding())
        except Exception:
            return False

    def _mcp_rows(self) -> List[Dict[str, Any]]:
        """The connector registry rows for this repo. Never raises.

        Config-derived only: the registry is read from the merged settings
        and the plugin manifests, which is a filesystem read and not a
        server spawn, so a repaint never starts a subprocess.
        """
        try:
            return [
                row
                for row in _iv.mcp_server_table(self.file_config, repo_path=self.repo)
                if isinstance(row, Mapping)
            ]
        except Exception:
            return []

    def _lsp_facts(self) -> List[str]:
        """The language-server receipt as up to three lines, or none.

        The receipt is `fileview.lsp_state_report`, which distinguishes a
        server that is not configured, one that cannot start, and one that
        was never attempted. A state we cannot establish contributes no
        entries, so a section of one fact falls under the anti-clutter rule
        and disappears rather than claiming a clean workspace.
        """
        try:
            report = _fv.lsp_state_report(self.repo)
        except Exception:
            return []
        if not isinstance(report, Mapping):
            return []
        state = str(report.get("state") or "").strip().lower()
        if state in ("", "unavailable", "unreadable_config"):
            return []
        out = [f"lsp {state}"]
        count = report.get("diagnostics")
        if isinstance(count, int) and count > 0:
            out.append(f"{count} diagnostics")
        source = str(report.get("server") or report.get("source") or "").strip()
        if source:
            out.append(source[:60])
        return out

    def _plan_block_states_todo(self) -> bool:
        """Whether the rail's plan block already published the todo steps.

        One fact in one place. The sidebar's `todo` section is skipped when
        the block above it is already showing the same steps, because two
        spellings of one fact is how a reader ends up unsure which one the
        product means.
        """
        return bool(self._rail_allocation.get("plan"))

    def _render_sidebar_sections(self, run: Optional[_RunState] = None) -> None:
        """Publish the sidebar's sections under the anti-clutter rule.

        Every string is escaped on the way into markup: a model name, a file
        path, a directory name, and a server label are all data this shell
        did not author, and a render failure must never delete a message.
        """
        run = run or self._run
        try:
            side = self.query_one("#vex-side", PlanRail)
        except Exception:
            return
        if run is None and not self._layout.plan_rail_visible:
            return
        try:
            facts = self._sidebar_facts(run)
            sections = []
            for key in _design.SIDEBAR_SECTIONS:
                entries = tuple(facts.get(key) or ())
                sections.append(
                    _design.SidebarSection(
                        key=key,
                        title=self._SIDEBAR_TITLES.get(key, key.upper()),
                        entries=entries,
                        collapsed=self._prefs.is_collapsed(key),
                    )
                )
            width = rail_content_width(self._layout.plan_rail_width or _design.SIDEBAR_WIDTH)
            gap = _design.rail_block_gap(self._density)
            # The sections are budgeted from what the rail's own blocks LEFT,
            # after the header, the `status` label, the two block gaps and the
            # footer. Sections that do not fit are simply not published, so a
            # sidebar can never push a row off the rail's bottom edge — the
            # defect `tests/test_cli_tui_layout.py::test_no_rail_region_is_cut_
            # without_saying_so` exists to catch, applied to the new widgets
            # rather than only to the old ones.
            footer = _design.sidebar_footer(self.repo, self.version)
            footer_rows = len(footer.lines) if footer.rendered else 0
            block_rows = sum(int(value) for value in (self._rail_allocation or {}).values())
            rail_total = int(self._rail_rows or 0)
            budget = max(
                0,
                rail_total - 2 - block_rows - 2 * gap - footer_rows,
            )
            self._sidebar_allocation = side.update_sections(
                sections,
                rows=budget,
                content_width=width,
                gap=gap,
            )
            side.update_footer(footer, content_width=width)
        except Exception:
            return

    def _statusline_facts(self) -> Dict[str, str]:
        """The statusline's live facts, as PLAIN text, keyed by section.

        Every value is either a true statement or absent. `0 queued` is a
        claim about the queue, and an absent entry is the honest form of it:
        "if there is nothing queued, show nothing".
        """
        out: Dict[str, str] = {}
        by_key = {item.key: item for item in _design.STATUSLINE_SECTIONS}

        def say(key: str, count: Any, detail: str = "") -> None:
            item = by_key.get(key)
            text = item.render(count, detail) if item is not None else ""
            if text:
                out[key] = text

        say("queue", len(self._queue))
        if self._run is not None:
            try:
                say("subagents", len(self._run.projection.subagents))
            except Exception:
                pass
        if self._detached_task_id:
            say("background", 1, self._detached_task_id[:24])
        # A setting is only worth a statusline row when it is NOT the
        # default: `density compact` and `sidebar auto` report a deliberate
        # choice, and reporting the default every second is noise.
        if self._density != _design.DEFAULT_DENSITY:
            out["density"] = f"density {self._density} ({by_key['density'].keybind})"
        if self._sidebar_mode != _design.DEFAULT_SIDEBAR_MODE:
            out["sidebar"] = f"sidebar {self._sidebar_mode} ({by_key['sidebar'].keybind})"
        return out

    def _render_statusline(self) -> None:
        """Fit the statusline into the viewport, or hide it.

        Hidden whenever it has nothing true to say, which is why
        `_apply_responsive_layout` takes its row count as an INPUT: a
        statusline with no facts must cost zero rows, not one blank one.
        """
        try:
            node = self.query_one("#vex-statusline", Static)
        except Exception:
            return
        try:
            facts = self._statusline_facts()
            self._statusline_sections = facts
            if not facts:
                node.update("")
                node.styles.display = "none"
                return
            fit = _design.fit_statusline(
                facts, max(0, int(self._layout.width or 0) - 2)
            )
            if not fit.text:
                node.update("")
                node.styles.display = "none"
                return
            node.update(_m(escape(fit.text)))
            node.styles.display = "block"
        except Exception:
            return

    def action_toggle_sidebar(self) -> None:
        """Advance the sidebar's tri-state mode: auto -> show -> hide.

        The key is declared in `cli.toggles` (Prompt 04's registry) and this
        is its mount. The next mode is computed from the mode THIS SHELL is
        in, not from the registry's own ordering, and the value is written
        with `set` rather than `flip`.

        That is deliberate, and it is a recorded defect in
        `cli/toggles.py` rather than a preference:
        `ToggleSettings.flip` indexes `TRISTATE_VALUES`
        (`auto`/`shown`/`hidden`) while `ToggleSpec.coerce` canonicalises to
        `auto`/`show`/`hide`, so after one flip the stored value is not in
        the list being indexed and the advance stalls at "always" forever.
        Measured: `flip` returned `(True, '')` for the first press and
        `(False, 'sidebar is already always')` for every press after it.
        `set` accepts every spelling and stores the canonical one, so the
        shell's cycle is real today and the registry's own `flip` is the
        thing that needs the one-line fix. Filed in `cli/AGENTS.md` under
        "Handoff to the toggles owner".
        """
        order = _design.design_mode_order()
        current = self._sidebar_mode
        index = order.index(current) if current in order else 0
        nxt = order[(index + 1) % len(order)]
        note = ""
        try:
            _ok, note = self._toggles.set(self._SIDEBAR_TOGGLE, nxt, persist=True)
        except Exception:
            note = "the toggle store is unavailable"
        # The mode is read BACK from the registry rather than assumed from
        # the return value: `ToggleSettings.set(..., persist=True)` returns
        # the FLUSH's receipt, so a session with no session id reports
        # "the change is not persisted" while the value it holds has already
        # moved. Trusting the boolean would leave the shell in the old mode
        # with a store that says otherwise — which is exactly the state the
        # repo-toggle defect in this module's docstring is about.
        try:
            value = self._toggles.get(self._SIDEBAR_TOGGLE, None)
        except Exception:
            value = None
        resolved = _design.normalize_sidebar_mode(value) if value is not None else nxt
        if resolved == current:
            resolved = nxt
        self._sidebar_mode = resolved
        try:
            self._prefs = self._prefs.with_sidebar_mode(self._sidebar_mode)
            self._save_prefs()
        except Exception:
            pass
        self._apply_responsive_layout(self._layout.width, self._layout.height)
        self._render_header()
        self._rerender_rails()
        sentence = f"sidebar: {self._sidebar_mode}"
        if note:
            sentence += f" ({note})"
        self._announce("layout", sentence)

    def action_cycle_density(self) -> None:
        """Switch between the comfortable and compact densities."""
        name = self.cycle_density()
        self._announce("layout", f"density: {name}")

    def _rail_measurement(self, selector: str) -> Tuple[int, int]:
        """Rows and content columns a rail actually has, right now.

        The mounted region is the authority; `tui_components.rail_rows` and
        `rail_content_width` are the pre-mount fallback so a caller that runs
        before the first layout still allocates sensibly instead of asking
        for rows the composer needs.

        The region is only trusted once the compositor has settled.
        `on_resize` runs BEFORE the new layout is applied, so a measurement
        taken there is the PREVIOUS size's: budgeting against it is how the
        context rail published 30 rows of content into a 19-row rail, and
        how a 120x26 rail ended two rows past its own bottom edge. A width
        check is not enough — a height-only resize leaves the width
        untouched and the stale region passes it — so the app raises
        `_layout_dirty` for the window in which the compositor is known to be
        behind, and the arithmetic is used instead. The arithmetic
        UNDER-promises (it assumes the run line and the announcement band are
        both showing), so the fallback can only ever leave a row unused.
        """
        expected = (
            self._layout.plan_rail_width
            if selector == "#vex-side"
            else self._layout.context_rail_width
        )
        fallback = rail_rows(self._layout.height)
        if self._layout_dirty:
            width = expected or 0
            return (fallback, rail_content_width(width) if width else 0)
        try:
            rail = self.query_one(selector)
            region = rail.region
            settled = (
                region.height > 0
                and (expected <= 0 or int(region.width or 0) == expected)
                and region.y + region.height <= self._layout.height
            )
        except Exception:
            settled = False
            region = None
        if not settled or region is None:
            width = expected or 0
            return (fallback, rail_content_width(width) if width else 0)
        return (int(region.height), rail_content_width(int(region.width or 0)))

    def _settle_layout(self) -> None:
        """Mark the layout settled and repaint the rails against it.

        The rails budget their content against their own measured height, and
        `on_resize` fires before the compositor has applied the new one, so
        the budget is taken a frame later. This is the callback that does it,
        and it is the only place `_layout_dirty` is cleared.
        """
        self._layout_dirty = False
        self._rerender_rails()

    def _rerender_rails(self) -> None:
        """Repaint both rails, the sidebar's sections, and the statusline.

        Called after a resize, because the rails budget their content against
        their own measured height and that height is only true once the new
        layout has been applied. The statusline is repainted with no run in
        flight because most of what it carries — the detached background, a
        non-default density, a non-default sidebar mode — is true exactly
        when nothing is running.
        """
        if self._run is not None:
            self._render_side(self._run)
            self._render_context(self._run)
        else:
            self._render_statusline()

    def _file_projection(
        self, run: Optional[_RunState] = None, *, include_git: bool = False
    ) -> Dict[str, Any]:
        """Return the current journal/workspace file projection for this task."""
        run = run or self._run
        task_id = run.task_id if run is not None else self._active_task_id()
        if not task_id:
            return {}
        event_count = int(run.projection.events) if run is not None else -1
        cache_key = (str(task_id), event_count, bool(include_git))
        if self._file_projection_cache is not None and self._file_projection_cache[:3] == cache_key:
            return self._file_projection_cache[3]
        try:
            task_dir = _iv._safe_task_dir(str(task_id), self.log_root)
            snapshot = run.projection.snapshot() if run is not None else None
            projection = _fv.build_file_projection(
                task_dir,
                self.repo,
                snapshot=snapshot,
                include_git=include_git,
                max_files=80,
                max_diff_lines=30,
            )
            if run is not None:
                context = run.projection.context
                sources = list(projection.get("sources") or [])
                for key in ("sources", "source_references", "citations", "files"):
                    value = context.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, Mapping):
                                path = str(item.get("path") or item.get("file") or "")
                                if path and not any(str(source.get("path")) == path for source in sources if isinstance(source, Mapping)):
                                    sources.append({"path": path, "source": key})
                projection["sources"] = sources
            self._file_projection_cache = (str(task_id), event_count, bool(include_git), projection)
            return projection
        except Exception:
            return {}

    def _render_context(self, run: Optional[_RunState] = None) -> None:
        """Repaint the files/diagnostics context rail from journal-derived facts."""
        run = run or self._run
        try:
            panel = self.query_one("#vex-context", ContextPanel)
            if run is None:
                panel.styles.display = "none"
                return
            data = run.projection.snapshot()
            files = self._file_projection(run)
            if files:
                data["file_changes"] = files.get("file_changes") or data.get("file_changes", [])
                data["changed_files"] = files.get("changed_files") or data.get("changed_files", [])
                data["relevant_files"] = files.get("relevant_files") or data.get("relevant_files", [])
                data["sources"] = files.get("sources") or data.get("sources", [])
            # The header and its one-row margin are the rail's own two fixed
            # rows, so the blocks share the rest, and EVIDENCE + USAGE is the
            # block that is guaranteed its share first. It used to be last in
            # the rail and therefore the first thing off screen — at 120x36
            # the rail rendered no verification state and no cost at all.
            rows, content_width = self._rail_measurement("#vex-context")
            self._context_allocation = panel.update_snapshot(
                data,
                rows=max(0, rows - 2),
                content_width=content_width,
                gap=_design.rail_block_gap(self._density),
            )
            panel.styles.display = "block" if self._layout.context_rail_visible else "none"
        except Exception:
            pass

    def _refresh_machine_state(self, task_id: str) -> None:
        """Re-read the state-machine audit trail (best-effort, UI-safe)."""
        try:
            self._machine_state = _rv.read_machine_state(self.log_root / task_id)
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        """Refresh elapsed status while advancing animation only when enabled."""
        if self._run is not None and not self._shutting_down:
            if self._motion_enabled:
                self._spinner_frame += 1
            self._render_run()
            self._render_side()
            self._render_context()

    def transcript(self, text: Any = "") -> None:
        """Append sanitized text or a Rich renderable to the transcript."""
        if isinstance(text, str):
            text = _m(ui.strip_ansi(text))
        elif isinstance(text, Text):
            clean = ui.strip_ansi(text.plain)
            text = (
                Text(clean, style=text.style, spans=text.spans)
                if clean == text.plain
                else Text(clean, style=text.style)
            )
        # A render failure must never make a line VANISH. `_m()` only rewrites
        # `vex.*` roles, so any un-escaped `[` reaching Textual's markup parser
        # is a live hazard, and one bad line used to take the whole message with
        # it -- which is how a typed slash command could produce no visible
        # response at all. Fall back to escaped plain text, which always
        # renders. This docstring promised that fallback before it existed.
        try:
            self.query_one("#vex-body", RichLog).write(text)
        except Exception:
            try:
                fallback = text if not isinstance(text, str) else Text(escape(text))
                self.query_one("#vex-body", RichLog).write(fallback)
            except Exception:
                pass

    def _store_diagnostic(self, lines: List[Text]) -> None:
        """Keep captured backend output out of the main completion view."""
        cleaned: List[Text] = []
        for line in lines or []:
            try:
                if isinstance(line, Text):
                    cleaned.append(Text(ui.strip_ansi(line.plain), style=line.style))
                else:
                    cleaned.append(Text(ui.strip_ansi(str(line))))
            except Exception:
                continue
        self._diagnostic_lines = cleaned[-80:]

    def _print_splash(self, first_launch: bool = True) -> None:
        """Render the full first-launch hero or a quiet later-launch state."""
        model = ui.strip_ansi(_iv._session_model_label(self.state, self.file_config))
        state = EmptyState(
            first_launch=first_launch,
            version=self.version,
            repo=self.repo,
            log_root=self.log_root,
            model=model,
            width=self.size.width or 80,
        )
        for line in state.lines():
            self.transcript(line)

    # -- worker-thread safe helpers ---------------------------------------

    def _thread_log(self, text: Any) -> None:
        """Worker-thread safe transcript append."""
        self._safe_call(self.transcript, text)

    def _safe_call(self, fn: Callable, *args) -> None:
        """call_from_thread that tolerates a shut-down app (worker threads
        are daemons; quitting during a run must not traceback)."""
        try:
            self.call_from_thread(fn, *args)
        except BaseException:
            pass

    def _transcript_ui(self, line: str) -> None:
        """Write ONE line to the transcript FROM THE UI THREAD.

        The counterpart to `_safe_call`, and the reason it exists:
        `_safe_call` goes through textual's `call_from_thread`, which
        REQUIRES being called from a different thread and raises when it
        is not. `on_mount` and the composer handlers run ON the UI thread,
        so every `_safe_call(self.transcript, ...)` on those paths raised
        and was swallowed — the startup notices, the resume briefing, and
        `/help` all rendered NOTHING while the code looked correct.

        A per-line write (not one multi-line string) is deliberate: a
        newline inside one `transcript` call lands as a single logical
        line, so a grouped help index would be clipped to one row.
        Never raises: a paint problem must not take a session down.
        """
        try:
            for piece in str(line or "").split("\n"):
                self.transcript(piece)
        except Exception:
            pass

    # -- input handling (Task C: every command from the REPL is ported) --

    def on_input_changed(self, event: Input.Changed) -> None:
        """Teach the composer the selected command's argument shape.

        The keystroke latency is NOT measured here: `Input.Changed` is
        posted by the widget and dispatched by the app's message pump, so
        a delta taken against it includes whatever frame the pump was
        already in the middle of. `_MeasuredInput` records it where the
        character actually lands.
        """
        if str(getattr(event.input, "id", "") or "") == "vex-input":
            event.input.placeholder = _commands.argument_hint(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Echo input visibly before persistence or dispatch work.

        The submit measurement is recorded under `submit_echo_ms` rather
        than `input_ack_ms`: it measures the echo-before-persist path,
        which is a different and larger budget than a keystroke. Keeping
        both under one name made the stricter gate unfalsifiable.
        """
        started = time.perf_counter()
        line = event.value.strip()
        event.input.value = ""
        self._hist_idx = None
        if not line:
            return
        self.transcript(
            f"[vex.accent]vex[/][vex.muted] {ui.GLYPHS['prompt']}[/] {escape(line)}"
        )
        self.ui_metrics.observe("submit_echo_ms", started)
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_history, save_session

                append_history(self.conversation, line)
                save_session(self.log_root, self.conversation)
            except Exception:
                pass
        command_started = time.perf_counter()
        self._handle_line(line)
        self.ui_metrics.observe("command_response_ms", command_started)

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
                    self.transcript(
                        ErrorState(
                            type(exc).__name__,
                            "edit the request or retry",
                        ).render()
                    )
                return
            self._steer_live(line)
            return

        # -- slash commands (incl. custom commands) -------------------
        if low.startswith("/"):
            try:
                self._slash_command(line, low)
            except Exception as exc:
                # Name the message, not just the class: the REPL reports both
                # (cli/interactive.py), and a bare "MarkupError" told a user
                # nothing about which command failed or why.
                self.transcript(
                    f"[vex.error]{escape(type(exc).__name__)}: "
                    f"{escape(str(exc))}[/]"
                )
            return

        # -- bare session commands --------------------------------------
        if low in ("exit", "quit", "q"):
            self.transcript("[vex.muted]bye[/]")
            self.exit()
            return
        if low in ("help", "?"):
            for help_line in _iv.render_help(line[4:].strip()).split("\n"):
                self._transcript_ui(help_line)
            return
        if low.startswith("repo ") or (
            _iv._looks_like_path(low) and not low.startswith(("fix", "in"))
        ):
            path = line[5:].strip() if low.startswith("repo ") else line
            cand = Path(path).expanduser().resolve()
            if cand.is_dir():
                self.repo = cand
                self.state["repo"] = str(cand)
                self._files_cache = None
                self._file_projection_cache = None
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

        # -- a new prompt COMMITS the staged revert (AGT-09) ---------
        # Deliberately placed AFTER the bare session commands, so `repo`,
        # `model`, `help` and `exit` never silently revert code, and BEFORE
        # the agent dispatch, so a real request reverts first and then runs
        # against the tree the user actually meant.
        if self._commit_staged_undo("a new prompt arrived"):
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
                from cli.session import expand_symbol_mentions

                expanded, inserted = expand_symbol_mentions(line, self.repo)
                if inserted:
                    self.transcript(
                        f"[vex.muted]attached symbols: {escape(', '.join(inserted))}[/]"
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
        selected_mode = str(self.state.get("mode") or "auto").lower()
        if selected_mode not in ("", "auto"):
            self._start_run(line, mode=selected_mode)
            return
        self._start_run(line, mode=work.kind)

    def _active_task_id(self) -> Optional[str]:
        """Resolve the live task before the last completed task."""
        if self._run is not None and self._run.task_id:
            return self._run.task_id
        live = _iv._live_run()
        if live and live.get("task_id"):
            return str(live["task_id"])
        task_id = self.last.get("task_id")
        return str(task_id) if task_id else None

    def _active_snapshot(self) -> Dict[str, Any]:
        """Return the journal-backed projection for the active task."""
        task_id = self._active_task_id()
        if not task_id:
            return {}
        if self._run is not None and self._run.task_id == task_id:
            return self._run.projection.snapshot()
        task_dir = _iv._safe_task_dir(task_id, self.log_root)
        if task_dir is None:
            return {}
        try:
            return _rv.read_live_projection(task_dir)
        except Exception:
            return {}

    def _is_in_flight(self) -> bool:
        """Return whether a run worker or journal projection is active."""
        if bool(
            self._run is not None
            or self._pending_run_text
            or (self._worker_thread is not None and self._worker_thread.is_alive())
            or _iv._live_run() is not None
        ):
            return True
        state = _commands.normalize_terminal_state(self._active_snapshot())
        return state in {"running", "waiting_for_approval", "resumed"}

    def _command_context(
        self, in_flight: Optional[bool] = None
    ) -> _commands.CommandContext:
        """Build the shared command context from journal and approval state."""
        task_id = self._active_task_id()
        snapshot = self._active_snapshot()
        pending = self._status == _STATUS_WAITING
        if task_id and not pending:
            pending = (
                _iv._pending_approval_request(self.log_root, task_id) is not None
            )
        active = self._is_in_flight() if in_flight is None else bool(in_flight)
        return _commands.surface_command_context(
            "tui",
            in_flight=active,
            snapshot=snapshot,
            task_id=str(task_id or ""),
            pending_approval=pending,
        )

    def _terminal_state(self) -> str:
        """Return the normalized active terminal state."""
        context = self._command_context()
        snapshot = self._active_snapshot()
        return _commands.normalize_terminal_state(
            snapshot,
            in_flight=context.in_flight,
            waiting_for_approval=context.waiting_for_approval,
            has_task=context.has_task,
        )

    def _active_task_dir(self) -> Optional[Path]:
        task_id = self._active_task_id()
        if not task_id:
            return None
        return _iv._safe_task_dir(task_id, self.log_root)

    def _render_diff_value(self, value: Any) -> None:
        try:
            lines = list(ui.diff_render_lines(value))
            if len(lines) > 240:
                self.transcript(
                    f"[vex.muted]diff summary:[/] {len(lines)} lines; "
                    "showing the first 240 · /diff detail for the paged view · "
                    "/copy-diff for the full patch[/]"
                )
                lines = lines[:240]
            for line in lines:
                self.transcript(line)
        except Exception:
            text = str(value or "")
            lines = text.splitlines()
            if len(lines) > 240:
                self.transcript(
                    f"[vex.muted]diff summary:[/] {len(lines)} lines; "
                    "showing the first 240 · /diff detail for the paged view[/]"
                )
                lines = lines[:240]
            for line in lines:
                self.transcript(escape(ui.strip_ansi(line)))

    #: Set by :meth:`_review_surface` so the ``/diff`` dispatch can tell
    #: "review claimed this line" from "review declined; the historical
    #: engine owns it". An attribute rather than a return value because the
    #: surface is reached from four branches of the same dispatcher and a
    #: return value threaded through all four is a second thing to forget.
    _review_handled: bool = False

    def _review_surface(self, argument: str, *, in_flight: bool) -> bool:
        """Ask `cli.review` to answer this `/diff` line, and render its receipt.

        Returns True when review claimed the line. Every failure mode here
        DEGRADES to "not claimed" rather than raising or printing a
        traceback: a review backend that cannot be reached must not take a
        working ``/diff`` down with it, and the honest answer to "I could not
        read the review" is the historical diff, not an error.

        The result shape is `cli.fileview.undo_command`'s, so this method
        renders an undo receipt with the protocol the shell already has.
        """
        self._review_handled = False
        task_id = self._active_task_id()
        if not task_id:
            return False
        try:
            task_dir = _iv._safe_task_dir(task_id, self.log_root)
        except Exception:
            task_dir = None
        if task_dir is None:
            return False
        try:
            from cli import review as _review
        except Exception:
            return False

        verb, argument = self._split_review_verb(argument)
        if verb:
            result = _review.review_command(
                task_dir,
                self.repo,
                argument,
                verb=verb,
                in_flight=bool(in_flight),
                config=dict(self._settings() or {}),
                width=self._layout.width,
            )
            if not result.get("handled"):
                return False
            self._review_handled = True
            self._render_review_receipt(result)
            return True

        # No verb: this is the SHOW surface, and it is a MODAL rather than a
        # transcript dump. A review of 200 files scrolled past is a review
        # nobody read, and the per-file roster is collapsed by default
        # precisely so the modal is a page rather than a wall.
        try:
            document = _review.build_review(
                task_dir,
                self.repo,
                config=dict(self._settings() or {}),
                width=self._layout.width,
            )
        except Exception:
            return False
        expanded = [document.path()] if str(argument or "").strip() else []
        rendered = _review.render_review(
            document, width=self._layout.width, expanded=expanded
        )
        lines = _review.review_lines(rendered, expanded=expanded)
        if not lines:
            self.transcript(
                "[vex.muted]nothing to review:[/] this run changed no file it is "
                "evidenced to have touched"
            )
            self._review_handled = True
            return True
        self._review_handled = True
        self.push_screen(
            _ReviewScreen(
                lines,
                title=(
                    f"review · {document.task_id or task_id} · "
                    f"{rendered.verdict} · {len(rendered.files)} file(s)"
                ),
            )
        )
        return True

    def _split_review_verb(self, argument: str) -> Tuple[str, str]:
        """``"reject src/a.py#2"`` -> ``("reject", "src/a.py#2")``.

        The verb is matched case-insensitively against
        `cli.review.DIFF_REVIEW_VERBS` rather than a restated literal, so a
        word added to the review module's own vocabulary reaches the TUI with
        no edit here - the same one-authority rule the command registry
        follows for the same four words.
        """
        text = str(argument or "").strip()
        if not text:
            return "", ""
        try:
            from cli import review as _review

            known = tuple(_review.diff_review_verbs())
        except Exception:
            return "", ""
        first, _, rest = text.partition(" ")
        if first.lower() in known:
            return first.lower(), rest.strip()
        return "", text

    def _render_review_receipt(self, result: Mapping[str, Any]) -> None:
        """Render a `cli.review` action receipt into the transcript.

        The lines are PLAIN and already sanitised by `cli.review`; they are
        escaped again on the way out because a transcript line crosses into
        Textual's markup parser and a repository path may contain `[`.
        Escaping an already-plain line is idempotent, and a second escape is
        cheaper than a second disclosure.
        """
        for line in list(result.get("lines") or [])[:80]:
            try:
                self.transcript(escape(ui.sanitize_text(str(line))))
            except Exception:
                continue
        if not result.get("ok"):
            self._mark_handler_result("failed", 1)
        else:
            self._mark_handler_result("ok", 0)

    def _open_diff_detail(self) -> None:
        """Open a paged, scrollable, selectable detail view for the diff.

        The WHOLE diff is paged, not the first 400 lines. A diff a user
        cannot scroll to the end of is a diff they will believe they read.
        """
        value = self._current_diff()
        if not value:
            self.transcript("[vex.muted]no diff from the last run[/]")
            return
        lines = [Text(line) for line in str(value).splitlines()]
        if not lines:
            self.transcript("[vex.muted]the diff is empty[/]")
            return
        screen = _TraceDetailScreen("diff detail", lines)
        self.push_screen(screen)

    def _current_diff(self) -> str:
        task_id = self._active_task_id()
        if not task_id:
            return ""
        task_dir = _iv._safe_task_dir(task_id, self.log_root)
        if task_dir is None:
            return ""
        try:
            if task_id.startswith("agent-"):
                from harness.agent_loop import agent_diff

                start = _iv._first_event(task_dir / "trace.jsonl", "task_start")
                data = (start or {}).get("data") or {}
                repo = str(data.get("repo_path") or self.repo)
                return agent_diff(task_id, self.log_root, repo) or ""
            live_lines = _tl.live_diff(
                task_dir / "pristine", task_dir / "work", max_lines=80
            )
            if live_lines:
                return "\n".join(str(line) for line, _kind in live_lines)
        except Exception:
            pass
        return str(self.last.get("diff") or "")

    def _start_mcp(self, label: str = "") -> None:
        """Discover or inspect connectors away from the UI thread."""
        if self._connector_thread is not None and self._connector_thread.is_alive():
            self.transcript("[vex.muted]connector request already running — /cancel to stop it[/]")
            return
        cancel = threading.Event()
        self._connector_cancel = cancel
        self.transcript(
            f"[vex.muted]scanning MCP connectors{' for ' + escape(label) if label else ''}…[/]"
        )

        def work() -> None:
            try:
                from cli import connectors
                from memory.mcp_client import list_mcp_tools

                rows = _iv.mcp_server_table(self.file_config, repo_path=self.repo)
                if cancel.is_set():
                    return
                if not label:
                    self._safe_call(self._mcp_list_done, rows, "")
                    return
                discovered = connectors.discover_mcp_servers(str(self.repo))
                command = discovered.get(label, {}).get("command")
                if not command:
                    self._safe_call(self._mcp_list_done, rows, label)
                    return
                if cancel.is_set():
                    return
                result = list_mcp_tools(command)
                if cancel.is_set():
                    return
                self._safe_call(self._mcp_tools_done, label, result, rows)
            except Exception as exc:
                self._safe_call(self._mcp_failed, type(exc).__name__, str(exc))

        thread = threading.Thread(target=work, daemon=True)
        self._connector_thread = thread
        thread.start()

    def _mcp_list_done(self, rows: List[Dict[str, str]], requested: str) -> None:
        self._connector_cancel = None
        if requested:
            self.transcript(f"[vex.error]unknown MCP server: {escape(requested)}[/]")
            return
        if not rows:
            self.transcript("[vex.muted]no MCP servers/connectors configured[/]")
            return
        self.transcript("[vex.accent]mcp connectors[/] [vex.muted](/mcp <label> lists tools)[/]")
        for row in rows:
            try:
                from cli.connectors import mask_command

                command = mask_command(str(row.get("command") or ""))
            except Exception:
                command = "***"
            self.transcript(
                f"  [vex.accent]{escape(str(row.get('label') or '?'))}[/] "
                f"[vex.muted]({escape(str(row.get('source') or '?'))})[/] "
                f"[vex.muted]{escape(command[:100])}[/]"
            )

    def _mcp_tools_done(
        self,
        label: str,
        result: Dict[str, Any],
        rows: List[Dict[str, str]],
    ) -> None:
        self._connector_cancel = None
        if not result.get("ok"):
            self.transcript(
                f"[vex.error]{escape(label)} unavailable[/] "
                f"[vex.muted]{escape(str(result.get('error') or 'unknown error'))}[/]"
            )
            return
        tools = result.get("tools") or []
        self.transcript(
            f"[vex.accent]{escape(label)}[/] [vex.muted]({len(tools)} tools)[/]"
        )
        for tool in tools[:50]:
            if isinstance(tool, dict):
                name = escape(str(tool.get("name") or "?"))
                desc = escape(str(tool.get("description") or "")[:100])
            else:
                name, desc = escape(str(tool)), ""
            self.transcript(f"  [vex.accent]{name}[/] [vex.muted]{desc}[/]")

    def _mcp_failed(self, kind: str, detail: str) -> None:
        self._connector_cancel = None
        self.transcript(
            f"[vex.error]connector request failed: {escape(kind)}[/] "
            f"[vex.muted]{escape(str(detail)[:180])}[/]"
        )

    def _copy_payload(self, prefer_diff: bool = False) -> Tuple[str, str]:
        """Return selectable text, then the current diff, then the last answer."""
        if not prefer_diff:
            try:
                selected = self.screen.get_selected_text()
            except Exception:
                selected = None
            if selected and selected.strip():
                return str(selected), "selection"
        diff = self._current_diff()
        if diff:
            return str(diff), "diff"
        answer = str(self.last.get("answer") or "")
        if answer:
            return answer, "code"
        return "", ""

    def _copy_payload_to_clipboard(self, payload: str, kind: str) -> None:
        """Copy a bounded text payload and announce the result accessibly."""
        if not payload:
            self.transcript("[vex.muted]nothing selected to copy[/]")
            return
        try:
            from cli.session import copy_text_to_clipboard

            copied = copy_text_to_clipboard(payload)
        except Exception:
            copied = False
        if copied:
            self.transcript(f"[vex.ok]{kind} copied to the clipboard[/]")
        else:
            self.transcript(
                f"[vex.warn]clipboard unavailable[/] [vex.muted]— {kind} remains selectable[/]"
            )

    def action_copy_selection(self) -> None:
        """Copy the current terminal selection or the latest code/diff."""
        payload, kind = self._copy_payload()
        self._copy_payload_to_clipboard(payload, kind or "text")

    # -- slash commands (all built-ins + custom-command dispatch) -------

    def _purge_supplied_file_config(self, refreshed: Dict[str, Any]) -> None:
        """Purge a stale api_key from the caller's config dict after a refresh.

        VexApp keeps its own copy, so without this a caller (notably the REPL
        and `run_tui`) can keep serving a key that logout just removed. Never
        raises: a stale secret must not turn a refresh into a crash.
        """
        target = self._supplied_file_config
        if target is None or target is self.file_config:
            return
        try:
            target.clear()
            target.update(refreshed)
        except Exception:
            pass

    def _slash_command(self, line: str, low: str, in_flight: bool = False) -> None:
        """Resolve, execute, and record one slash command on the TUI surface."""
        raw = str(line or "").strip()
        before_snapshot = self._active_snapshot()
        state_before = self._terminal_state()
        context = self._command_context(
            in_flight=bool(in_flight or self._is_in_flight())
        )
        resolution = _commands.resolve_command_line(raw, context)
        # A project/global custom command is a real command that is not in
        # `COMMAND_SPECS`, so an unregistered name is a refusal only when
        # no template backs it (R2-18: the same rule on all three
        # surfaces, or the TUI would refuse a command its own dispatcher
        # runs two hundred lines below).
        if resolution.status != "ok" and not (
            resolution.spec is None
            and resolution.status in {"unknown", "not_command"}
            and _commands.is_custom_command_line(raw, self.state.get("repo"))
        ):
            if resolution.status == "queued" and resolution.spec is not None:
                canonical = f"{resolution.spec.name} {resolution.args}".rstrip()
                self._queue.append(canonical)
                self.transcript(
                    f"[vex.muted]queued (will run when the current task "
                    f"finishes — {len(self._queue)} waiting)[/]"
                )
            elif resolution.spec is None:
                # R2-18: an UNKNOWN command is a refusal here exactly as it
                # is in the REPL and in the headless adapter. This branch
                # used to be gated on `resolution.spec is not None`, so a
                # name the registry does not describe fell through to the
                # dispatcher, came back as "no handler ran", and was
                # recorded `ok`/0 — the TUI accepted a command no registry
                # row describes, and the palette/help that a user reads to
                # learn the vocabulary could not account for it.
                self.transcript(
                    f"[vex.error]"
                    f"{escape(_commands.unknown_command_line(resolution))}[/]"
                )
                self.transcript(
                    f"[vex.muted]{escape(_commands.command_recovery_hint(None))}[/]"
                )
            else:
                self.transcript(
                    f"[vex.warn]{escape(resolution.spec.name)} unavailable: "
                    f"{escape(resolution.message)}[/]"
                )
                self.transcript(
                    f"[vex.muted]{escape(_commands.command_usage(resolution.spec))}[/]"
                )
                self.transcript(
                    f"[vex.muted]{escape(_commands.command_recovery_hint(resolution.spec))}[/]"
                )
            self.last_command = _commands.command_record(
                command=resolution.command,
                args=resolution.args,
                surface="tui",
                spec=resolution.spec,
                status=resolution.status,
                exit_code=resolution.exit_code,
                state_before=state_before,
                state_after=state_before,
                message=resolution.message,
                recovery=resolution.recovery,
                task_id=self._active_task_id() or "",
                verification_state=str(
                    before_snapshot.get("verification_state") or "not_run"
                ),
                run_status=str(before_snapshot.get("status") or ""),
                evidence=before_snapshot.get("verification_evidence"),
            ).to_dict()
            return
        canonical = raw
        if resolution.spec is not None:
            canonical = f"{resolution.spec.name} {resolution.args}".rstrip()
        try:
            self._last_handler_result = None
            self._slash_command_impl(
                canonical, canonical.lower(), in_flight=context.in_flight
            )
        except BaseException as exc:
            # R2-18: `commands.command_failure` is the one exception
            # policy. It used to be a hard-coded `error`/1 here and in the
            # REPL while the headless adapter classified the same
            # exception, so a Ctrl+C was a task failure in two shells and
            # an interruption in a script, and a dead Docker daemon was a
            # "retry the run" in two shells and a "fix the machine" in a
            # script.
            self.last_command = _commands.command_record(
                command=resolution.command,
                args=resolution.args,
                surface="tui",
                spec=resolution.spec,
                state_before=state_before,
                state_after=self._terminal_state(),
                recovery=resolution.recovery,
                task_id=self._active_task_id() or "",
                verification_state=str(
                    before_snapshot.get("verification_state") or "not_run"
                ),
                run_status=str(before_snapshot.get("status") or ""),
                evidence=before_snapshot.get("verification_evidence"),
                exc=exc,
            ).to_dict()
            raise
        after_snapshot = self._active_snapshot()
        handler_result = getattr(self, "_last_handler_result", None) or {}
        delegated_surface = ""
        if not handler_result:
            delegated = self.state.get("last_command")
            if (
                isinstance(delegated, dict)
                and delegated.get("command") == resolution.command
            ):
                handler_result = {
                    "status": delegated.get("status"),
                    "exit_code": delegated.get("exit_code"),
                }
                delegated_surface = str(delegated.get("surface") or "")
        # R2-18: a DELEGATED command (`/settings`, `/plugins`, `/theme`
        # run the REPL handler) used to be copied into the TUI's own record
        # verbatim, so `surface` read "repl" on a TUI session and the TUI
        # published a record it did not author. The status and exit code
        # the delegated handler reported are kept — that is the shared
        # decision — but the envelope is this surface's.
        self.last_command = _commands.command_record(
            command=resolution.command,
            args=resolution.args,
            surface="tui",
            spec=resolution.spec,
            status=str(handler_result.get("status") or "ok"),
            exit_code=int(handler_result.get("exit_code") or 0),
            state_before=state_before,
            state_after=self._terminal_state(),
            task_id=self._active_task_id() or "",
            verification_state=str(
                after_snapshot.get("verification_state")
                or before_snapshot.get("verification_state")
                or "not_run"
            ),
            run_status=str(after_snapshot.get("status") or ""),
            evidence=after_snapshot.get("verification_evidence")
            or before_snapshot.get("verification_evidence"),
        ).to_dict()
        if delegated_surface and delegated_surface != "tui":
            # The record is this surface's; the shared DECISION came from
            # the delegated handler. Both facts are kept so a reader can
            # see which handler decided.
            self.last_command["delegated_from"] = delegated_surface
        self._set_hints()

    def _delegate_shared_command(self, line: str) -> None:
        """Run one REPL-owned command and replay its bounded Rich output.

        R2-18: the delegated handler may install process-wide state that
        this app owns. `/theme` calls ``ui.set_theme`` inside the REPL
        handler, so after a `/theme reset` (whose argument is not a theme
        NAME, so the TUI's own ``_apply_theme`` never runs) the mounted
        app's tokens were stale, the ``on_unmount`` restore guard
        ``ui.active_tokens() == self._tokens`` no longer held, and the
        pre-mount theme was never restored. The delegation now RE-ADOPTS
        whatever it installed, so this app is once again the owner of the
        process tokens for the whole time it is mounted.
        """
        tokens_before = ui.active_tokens()
        _handled, lines = self._cap.capture(
            _iv._slash_command,
            line,
            line.lower(),
            dict(self.last),
            self.log_root,
            self.state,
        )
        if ui.active_tokens() != tokens_before:
            self._tokens = ui.active_tokens()
        self._store_diagnostic(lines)
        for line_item in lines:
            self.transcript(line_item)
        if isinstance(self.state.get("last_command"), dict):
            self.last_command = dict(self.state["last_command"])

    def _apply_theme(self, name: str) -> None:
        """Install one validated terminal theme in the mounted TUI."""
        selected = resolve_theme(
            name=name,
            overrides=self.file_config.get("theme_overrides"),
            config=self.file_config,
            depth=self._tokens.depth,
            is_tty=True,
        )
        self._tokens = ui.set_active_tokens(selected)
        self.state["theme"] = name
        _refresh_role_map()
        try:
            self.register_theme(_vex_textual_theme(self._tokens))
            self.theme = "vex"
        except Exception:
            pass
        self._render_header()

    def _mark_handler_result(self, status: str, exit_code: int = 0) -> None:
        """Record one TUI command handler's semantic outcome."""
        self._last_handler_result = {"status": str(status), "exit_code": int(exit_code)}

    def _slash_command_impl(
        self, line: str, low: str, in_flight: bool = False
    ) -> None:
        """Execute one preflight-approved TUI command or custom template."""
        parts = line.split()
        cmd = low.split()[0] if low.split() else ""

        if cmd in ("/help",):
            # R2-17: searchable, and the SAME index the REPL renders, so
            # help cannot differ between the two shells. Written through
            # `_transcript_ui` because this handler runs ON the UI thread.
            for help_line in _iv.render_help(
                " ".join(parts[1:]).strip()
            ).split("\n"):
                self._transcript_ui(help_line)
            return

        if cmd in ("/detach",):
            self.action_detach()
            return

        if cmd in ("/attach",):
            self.action_attach()
            return

        if cmd in ("/watch",):
            rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            target = rest or (self._live_task_id() or "")
            if not target:
                self.transcript(
                    "[vex.muted]usage: /watch <task-id>[/] [vex.muted]"
                    "(or `vex watch <task-id>` from another terminal)[/]"
                )
                return
            self.transcript(
                f"[vex.accent]watching[/] [vex.muted]{escape(target)} — "
                f"in another terminal: [vex.accent]vex watch {escape(target)}[/][/]"
            )
            return

        if cmd in ("/mode",):
            rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self.transcript("[vex.muted]select a mode: plan · build · explore · review · debug · ask[/]")
            if rest:
                _iv._mode_command(self.state, rest, say=self.transcript)
                self._render_header()
            return

        if cmd in ("/status",):
            target = self._active_task_id()
            if not target:
                self.transcript("[vex.muted]no run in this session yet[/]")
                return
            self._render_task_status(target)
            return

        if cmd in ("/files",):
            query = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self._open_files_screen(query)
            return

        if cmd in ("/open",):
            rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            try:
                from cli import fileview

                path, lineno = fileview.split_open_target(rest)
                result = fileview.launch_editor_detached(path, lineno)
                if result.returncode == 0:
                    self.transcript(f"[vex.ok]opened {escape(str(path))}[/]")
                else:
                    self.transcript(f"[vex.error]editor exited {result.returncode}[/]")
                    if result.stderr:
                        self.transcript(f"[vex.muted]{escape(result.stderr[:300])}[/]")
            except Exception as exc:
                self.transcript(f"[vex.error]cannot open {escape(rest)!r}:[/] {escape(str(exc))}")
            return

        if cmd in ("/repo",):
            rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            if not rest:
                self.transcript("[vex.muted]usage: /repo <path>[/]")
                return
            try:
                from cli import fileview

                changed = fileview.reload_repo_settings(Path(rest), self.state)
                self.state["repo"] = str(changed["repo"])
                self._files_cache = None
                self.transcript("[vex.ok]repo switched[/]")
                if changed["changed"]:
                    self.transcript("[vex.accent]effective settings changed:[/]")
                    for row in changed["effective_diff"]:
                        self.transcript(
                            f"  [vex.muted]{escape(str(row['key']))}[/] "
                            f"old=[vex.muted]{escape(str(row['before']))}[/] "
                            f"new=[vex.accent]{escape(str(row['after']))}[/]"
                        )
                self.transcript(f"[vex.muted]log root: {escape(str(changed['log_root']))}[/]")
            except Exception as exc:
                self.transcript(f"[vex.error]cannot switch repo:[/] {escape(str(exc))}")
            return

        if cmd in ("/doctor",):
            tokens = line.split()
            json_mode = "--json" in tokens[1:]
            try:
                from cli import doctor

                record = doctor.run_doctor(
                    repo_path=str(self.repo),
                    log_root=str(self.log_root),
                )
                if json_mode:
                    self.transcript(doctor.render_doctor_json(record))
                else:
                    self.transcript(doctor.render_doctor_human(record))
            except Exception as exc:
                self.transcript(f"[vex.error]doctor failed:[/] {escape(str(exc))}")
            return

        if cmd in ("/checkpoints",):
            self._open_checkpoints_screen(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            return

        if cmd in ("/context",):
            self._open_context_screen(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            return

        if cmd in ("/relevant",):
            self._open_relevant_screen(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            return

        if cmd in ("/diagnostics",):
            self._open_diagnostics_screen()
            return

        if cmd in ("/export", "/share"):
            if in_flight:
                self.transcript("[vex.warn]wait for the run to finish before exporting[/]")
                return
            rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            try:
                destination = _iv.export_session(
                    self.log_root,
                    self.state,
                    self._active_task_id(),
                    Path(rest) if rest else None,
                    share=cmd == "/share",
                )
                self.transcript(
                    f"[vex.ok]{'shared' if cmd == '/share' else 'exported'}[/] "
                    f"[vex.muted]{escape(str(destination))}[/]"
                )
            except Exception as exc:
                self.transcript(f"[vex.error]export failed:[/] {escape(str(exc))}")
            return

        if cmd in ("/diff",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            low_rest = rest.lower()
            if low_rest == "detail":
                self._open_diff_detail()
                return
            if (
                low_rest == "undo"
                or low_rest.startswith("undo ")
                or low_rest in ("undo all", "all")
            ):
                if in_flight or (self._worker_thread and self._worker_thread.is_alive()):
                    self.transcript("[vex.warn]undo is unavailable while a run is active[/]")
                    return
                arg = (
                    rest[len("undo") :].strip()
                    if low_rest.startswith("undo")
                    else rest.strip()
                )
                if self._handle_staged_undo(arg, in_flight=False):
                    return
                task_id = self._active_task_id()
                info = dict(self.last)
                if task_id:
                    info["task_id"] = task_id
                outcome = self._render_undo(
                    _iv.undo_result(info, self.log_root, str(self.repo), arg)
                )
                if outcome != "done":
                    self._mark_handler_result("failed", 1)
                return
            if rest and rest.lower() not in {"all", "undo"}:
                projection = self._file_projection(self._run, include_git=True)
                target = _fv.parse_diff_target(rest)
                requested = str(target.get("path") or "")
                hunk_number = target.get("hunk")
                line_number = target.get("line")
                if not requested:
                    self.transcript(
                        f"[vex.muted]no diff recorded for {escape(rest)}[/] "
                        "[vex.muted](use /diff <file>, <file>#<hunk>, or <file>:<line>)[/]"
                    )
                    return
                if hunk_number is not None and line_number is None:
                    selected = _fv.open_diff_file(projection, requested, hunk_number)
                    if selected is None:
                        self.transcript(f"[vex.muted]no diff recorded for {escape(rest)}[/]")
                        return
                    self.push_screen(_DiffFileScreen(selected, hunk_number))
                    return
                if hunk_number is not None:
                    # `file#hunk:line` names BOTH. Resolving only the hunk
                    # leaves `selected_line` absent, so the cursor seats on
                    # the FIRST change of that hunk instead of the addressed
                    # line — the line argument would be silently ignored.
                    # The target's OWN hunk is also narrowed to the named
                    # one, so a `#2:5` address shows hunk 2 and not hunk 1
                    # as well.
                    selected = _fv.open_diff_line(projection, requested, line_number)
                    if selected is None:
                        self.transcript(f"[vex.muted]no diff recorded for {escape(rest)}[/]")
                        return
                    selected["hunks"] = [
                        hunk
                        for hunk in (selected.get("hunks") or [])
                        if isinstance(hunk, Mapping) and int(hunk.get("index", 0)) == hunk_number
                    ]
                    self.push_screen(_DiffFileScreen(selected, hunk_number, line_number))
                    return
                selected = _fv.open_diff_line(projection, requested, line_number)
                if selected:
                    self.push_screen(_DiffFileScreen(selected, None, line_number))
                else:
                    self.transcript(f"[vex.muted]no diff recorded for {escape(rest)}[/]")
                return
            if not rest:
                projection = self._file_projection(self._run, include_git=True)
                summary = (projection.get("diff") or {}).get("summary") or {}
                if summary.get("large") and projection.get("changed_files"):
                    self.push_screen(_DiffBrowserScreen(projection["diff"]["files"]))
                    return
            # THE MOUNT. `cli.review` is asked FIRST, and it can decline:
            # `review_command` returns `handled=False` for every verb it does
            # not own, which is what keeps `/diff undo` and a bare `/diff all`
            # on the historical engine below, byte-identically. Asking
            # review first and falling through is the only order that
            # preserves that - the historical branches above are already
            # reached by the time we get here, so anything review claims is
            # a verb the historical engine never had.
            self._review_surface(rest, in_flight=in_flight)
            if self._review_handled:
                return
            live = self._current_diff()
            if live:
                self.last["diff"] = live
                self._render_diff_value(live)
            else:
                self.transcript("[vex.muted]no diff from the last run[/]")
            return

        if cmd in ("/sessions",):
            self._open_sessions_screen(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            return

        if cmd in ("/fork",):
            self._fork_command(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
                in_flight,
            )
            return

        if cmd in ("/import",):
            self._import_command(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
                in_flight,
            )
            return

        if cmd in ("/recover",):
            self._recover_command(
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else "",
                in_flight,
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
            values = line.split(None, 1)[1].strip().split() if len(line.split(None, 1)) > 1 else []
            scope_words = {"once", "session", "path", "command", "y", "s", "p", "c"}
            if values and values[0].lower() in scope_words:
                tid = self._active_task_id()
                scope = values[0]
            else:
                tid = values[0] if values else self._active_task_id()
                scope = values[1] if len(values) > 1 else "once"
            if not tid:
                self.transcript(
                    "[vex.muted]no task to decide on — run with "
                    "--approval or approve from a benchmark[/]"
                )
                return
            decided = self._decide_pending(
                tid,
                approve=(cmd == "/approve"),
                scope=scope,
                policy=self.state["approval_policy"],
            )
            if decided is None:
                self.transcript(f"[vex.muted]no pending approval request for {tid}[/]")
                self._mark_handler_result("failed", 1)
            return

        if cmd in ("/cancel",):
            if in_flight:
                self.transcript(
                    "[vex.warn]cancel requested — sending Ctrl+C semantics "
                    "to the running task (checkpoints kept)[/]"
                )
                self._interrupt_worker()
                return
            if self._connector_thread is not None and self._connector_thread.is_alive():
                if self._connector_cancel is not None:
                    self._connector_cancel.set()
                self.transcript("[vex.warn]connector request cancelled[/]")
                return
            self.transcript("[vex.muted]nothing running[/]")
            self._mark_handler_result("failed", 1)
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
                if in_flight or (self._worker_thread and self._worker_thread.is_alive()):
                    self.transcript("[vex.warn]model changes wait until the run finishes[/]")
                    return
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

        if cmd in ("/effort", "/thinking"):
            from cli.commands import apply_effort, effort_receipt, render_effort

            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            receipt = effort_receipt(self.state, self.file_config, rest)
            # Changing effort mid-run is ALLOWED and safe: it changes how hard
            # the model thinks from the next model call, never whether a result
            # is verified. Refusing it here (as `/model` does) would make the
            # one control a person reaches for when a run is struggling the one
            # control they cannot reach.
            applied = apply_effort(self.state, receipt)
            for rendered in render_effort(receipt):
                role = (
                    "vex.warn" if rendered.startswith("effort unchanged") else "vex.muted"
                )
                self.transcript(f"[{role}]{escape(rendered)}[/]")
            if applied:
                self._render_header()
                self.transcript(
                    f"[vex.ok]effort {escape(str(receipt.get('level') or ''))}"
                    f"{ui.GLYPHS['arrow']} applies from the next model call[/]"
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
                    selected = str(self.state.get("mode") or "auto").lower()
                    self._start_run(
                        filled,
                        mode=selected if selected not in ("", "auto") else "fix",
                    )
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
            payload, kind = self._copy_payload(prefer_diff=cmd == "/copy-diff")
            self._copy_payload_to_clipboard(payload, kind or "text")
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
                logged_out, lines = self._cap.capture(
                    _iv._do_logout,
                    state=self.state,
                    file_config=self.file_config,
                    repo=self.repo,
                )
            except Exception as exc:
                self.transcript(f"[vex.error]logout failed: {type(exc).__name__}[/]")
                return
            for line in lines:
                self.transcript(line)
            if not logged_out:
                self.transcript(
                    "[vex.warn]logout incomplete — the effective model may be unchanged[/]"
                )
                self._render_header()
                return
            self._store_diagnostic(lines)
            try:
                refreshed = _iv._reload_file_config(
                    self.state, self.file_config, start=self.repo
                )
                self.file_config.clear()
                self.file_config.update(refreshed)
                self._purge_supplied_file_config(refreshed)
            except Exception:
                pass
            self._render_header()
            self.transcript("[vex.muted]effective model refreshed[/]")
            return

        if cmd in ("/mcp",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if len(rest.split()) > 1:
                self.transcript("[vex.muted]usage: /mcp [label][/]")
                return
            self._start_mcp(rest)
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
            _iv._render_cost(
                self.last,
                self.log_root,
                say=self.transcript,
                task_id=(
                    self._active_task_id()
                    if self._run is not None or _iv._live_run() is not None
                    else None
                ),
            )
            return

        if cmd in ("/undo",):
            rest = (
                line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            )
            if self._handle_staged_undo(rest, in_flight=bool(in_flight)):
                return
            task_id = self._active_task_id()
            info = dict(self.last)
            if task_id:
                info["task_id"] = task_id
            outcome = self._render_undo(
                _iv.undo_result(info, self.log_root, str(self.repo), rest)
            )
            if outcome != "done":
                self._mark_handler_result("failed", 1)
            return

        if cmd in ("/redo",):
            if in_flight or (self._worker_thread and self._worker_thread.is_alive()):
                self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
                return
            task_id = self._active_task_id()
            info = dict(self.last)
            if task_id:
                info["task_id"] = task_id
            result = _iv.redo_result(info, self.log_root, str(self.repo))
            if result.get("outcome") != "done":
                self._mark_handler_result("failed", 1)
            if result.get("outcome") == "done":
                self.transcript(f"[vex.ok]redid {len(result.get('files') or [])} file(s)[/]")
                if result.get("diff"):
                    self.last["diff"] = result["diff"]
                    self._render_diff_value(result["diff"])
                else:
                    self.last["diff"] = None
            elif result.get("outcome") == "not_agent":
                self.transcript("[vex.muted]redo is for agent sessions[/]")
            elif result.get("outcome") == "nothing":
                self.transcript("[vex.muted]nothing to redo[/]")
            else:
                self.transcript(
                    f"[vex.error]redo failed:[/] [vex.muted]{escape(str(result.get('error') or 'unknown error'))}[/]"
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

        if cmd in ("/build",):
            request = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self._start_run(request, mode="build")
            return

        if cmd in ("/ask",):
            request = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self._start_run(request, mode="ask")
            return

        if cmd in ("/settings", "/plugins", "/theme"):
            self._delegate_shared_command(line)
            if cmd == "/settings":
                refreshed = _iv._reload_file_config(
                    self.state, self.file_config, start=self.repo
                )
                self.file_config.clear()
                self.file_config.update(refreshed)
                self._purge_supplied_file_config(refreshed)
                self._render_header()
            elif cmd == "/theme":
                requested = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
                if requested in theme_names():
                    self._apply_theme(requested)
            return

        if cmd in ("/quit", "/exit"):
            if in_flight:
                self.transcript("[vex.warn]/cancel the active run before /quit[/]")
                return
            self._exit_code = 0
            self.transcript("[vex.muted]bye[/]")
            self.exit()
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
            selected = str(self.state.get("mode") or "auto").lower()
            self._start_run(
                filled,
                mode=selected if selected not in ("", "auto") else "agent_task",
            )
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

    def _decide_pending(
        self,
        task_id: str,
        approve: bool,
        scope: str = "once",
        policy: Optional[_commands.ApprovalPolicy] = None,
    ) -> Optional[bool]:
        """Decide a pending request with the shared scope policy."""
        try:
            from runtime import approval as approval_mod
        except ImportError:
            self.transcript("[vex.error]runtime.approval unavailable[/]")
            return None
        if _iv._safe_task_dir(str(task_id), self.log_root) is None:
            self.transcript(
                f"[vex.error]invalid task id: {escape(str(task_id))!r}[/]"
            )
            return None
        gate = self.log_root / f"{task_id}.runtime" / "approval"
        req = approval_mod.pending_request(str(gate))
        if req is None:
            return None
        request_task_id = str(req.get("task_id") or "")
        if request_task_id and request_task_id != str(task_id):
            self.transcript("[vex.error]approval request identity mismatch[/]")
            return None
        normalized_scope = _commands.normalize_approval_scope(scope)
        if approve and normalized_scope != "once" and policy is not None:
            policy.record(_commands.approval_request_view(req), normalized_scope)
        approval_mod.decide(str(gate), approve=approve)
        self._approval_handled.add(task_id)
        suffix = f" ({normalized_scope})" if approve else ""
        self.transcript(
            f"[vex.{'ok' if approve else 'error'}]"
            f"{'approved' if approve else 'rejected'}[/] — the worker "
            f"continues {'with' if approve else 'without'} the fix{suffix}"
        )
        return approve

    def _render_task_status(self, task_id: str) -> None:
        """Render live or completed task facts from the same trace view."""
        task_dir = _iv._safe_task_dir(task_id, self.log_root)
        if task_dir is None:
            self.transcript(
                f"[vex.error]invalid task id: {escape(str(task_id))}[/]"
            )
            return
        mode = str(self._dispatched_mode or self.last.get("mode") or "agent_task")
        if self._run is not None and self._run.task_id == task_id:
            snapshot = self._run.projection.snapshot()
        else:
            snapshot = _rv.read_live_projection(task_dir, mode=mode)
        for row in _rv.status_lines(
            snapshot,
            mode=str(
                snapshot.get("mode")
                or self._dispatched_mode
                or self.last.get("mode")
                or "agent_task"
            ),
            live=self._run is not None and self._run.task_id == task_id,
        ):
            self.transcript(row)

    def _render_undo(self, res: Dict[str, Any]) -> str:
        """Render an undo_result dict into the transcript and return its outcome."""
        try:
            outcome = str((res or {}).get("outcome") or "failed")
            if outcome == "not_agent":
                self.transcript(
                    "[vex.muted]undo is for agent sessions (this run never "
                    "touched the live repo)[/]"
                )
                return outcome
            if outcome == "nothing":
                self.transcript("[vex.muted]nothing to undo[/]")
                return outcome
            if outcome == "conflict":
                self.transcript(
                    f"[vex.warn]undo conflict[/] [vex.muted]{escape(str((res or {}).get('error') or 'workspace changed after the recorded operation'))}[/]"
                )
                return outcome
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
                            self.transcript(escape(ui.strip_ansi(dl)))
                else:
                    self.last["diff"] = None
                return outcome
            self.transcript(
                f"[vex.error]undo failed:[/] [vex.muted]{escape(ui.strip_ansi((res or {}).get('error') or '?'))}[/]"
            )
        except Exception:
            return "failed"
        return outcome

    # -- AGT-09: staged undo surface -----------------------------------
    #
    # `/undo` STAGES. A second `/undo` WIDENS the range. A new prompt (or
    # `/undo commit`) COMMITS it. Nothing pops.
    #
    # A per-file argument still routes to the historical per-file revert, so a
    # user who types `/undo cli/tui.py` gets exactly what they got before; the
    # staged model is what the bare command and the new sub-verbs speak.

    #: Words that mean "the granularity", and the one that means "commit".
    _UNDO_VERBS = ("commit", "apply", "discard", "cancel", "force", "plan")

    def _undo_session_id(self) -> str:
        conversation = self.conversation
        if isinstance(conversation, dict):
            value = str(conversation.get("session_id") or conversation.get("id") or "")
            if value:
                return value
        return ""

    def _render_staged_undo(self, payload: Dict[str, Any]) -> None:
        """Render one staged-undo payload as escaped, markup-free lines."""
        try:
            record = payload if isinstance(payload, Mapping) else {}
            if "status" in record or "restored" in record:
                lines = _fv.render_undo_receipt(record)
            else:
                lines = [
                    str(record.get("detail") or record.get("reason") or record or "")
                ]
            for line in lines:
                text = str(line or "")
                if not text:
                    continue
                self.transcript(f"[vex.muted]{escape(text)}[/]")
        except Exception:
            # A render failure must never delete the message it was rendering.
            self.transcript("[vex.muted](undo output could not be rendered)[/]")

    def _handle_staged_undo(self, rest: str, *, in_flight: bool) -> bool:
        """Run the SHARED staged-undo dispatcher and render it.

        Returns True when it owned the line. Returning False hands the line to
        the historical per-file revert, which is what keeps ``/undo <file>``
        behaving exactly as it always has.
        """
        try:
            result = _fv.undo_command(
                self.repo,
                self.log_root,
                rest,
                session_id=self._undo_session_id(),
                in_flight=bool(in_flight)
                or bool(self._worker_thread is not None and self._worker_thread.is_alive()),
            )
        except Exception as exc:
            self.transcript(
                f"[vex.error]undo failed:[/] [vex.muted]{escape(f'{type(exc).__name__}: {exc}')}[/]"
            )
            return True
        if not result.get("handled"):
            return False
        kind = str(result.get("kind") or "")
        if kind in {"stage", "widen", "scope"} and result.get("ok"):
            head = "[vex.ok]staged revert[/]"
        elif result.get("ok"):
            head = "[vex.ok]undo[/]"
        else:
            head = "[vex.warn]undo[/]"
        try:
            lines = [str(item) for item in (result.get("lines") or [])]
        except Exception:
            lines = []
        for index, line in enumerate(lines):
            text = str(line or "")
            if not text:
                continue
            prefix = head if index == 0 else "[vex.muted]"
            self.transcript(f"{prefix} [vex.muted]{escape(text)}[/]")
        if not result.get("ok"):
            self._mark_handler_result("failed", 1)
        return True

    def _undo_session_id(self) -> str:
        conversation = self.conversation
        if isinstance(conversation, dict):
            value = str(
                conversation.get("session_id") or conversation.get("id") or ""
            )
            if value:
                return value
        return ""

    def _commit_staged_undo(self, reason: str) -> bool:
        """Commit a staged CODE-ONLY revert because the user asked for more work.

        Returns True when a revert was applied, so the caller can stop and let
        the request proceed against the tree the user actually meant.
        """
        try:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False
            result = _fv.commit_staged_undo_for_prompt(
                self.repo,
                self.log_root,
                session_id=self._undo_session_id(),
                in_flight=False,
            )
            if not result.get("committed"):
                return False
            for line in result.get("lines") or []:
                self.transcript(f"[vex.muted]{escape(str(line))}[/]")
            if not bool((result.get("payload") or {}).get("ok")):
                self.transcript(
                    "[vex.muted]the staged revert is still pending - "
                    "/undo plan shows what is left[/]"
                )
            return True
        except Exception:
            return False

    def _render_review(self) -> None:
        """Render the active diff and any recorded rationale as one view."""
        diff = self._current_diff()
        if diff:
            self.transcript("[vex.muted]diff:[/]")
            self._render_diff_value(diff)
        else:
            self.transcript("[vex.muted]no diff from the last run[/]")
        tid = self._active_task_id()
        body = _iv.last_rationale_text(self.log_root, tid) if tid else None
        if body:
            self.transcript("")
            self.transcript("[vex.muted]rationale:[/]")
            self.transcript(Markdown(ui.strip_ansi(body)))
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
        task_dir = _iv._safe_task_dir(tid, self.log_root)
        if task_dir is None:
            return None
        run = _RunState(
            tid,
            mode=str(
                self._dispatched_mode
                or self.last.get("mode")
                or "fix"
            ),
        )
        trace_file = task_dir / "trace.jsonl"
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

    def _open_files_screen(self, query: str = "") -> None:
        """Open the searchable file-tree and symbol browser."""
        if query.startswith("@"):
            query = query[1:].strip()
        task_id = self._active_task_id()
        try:
            changed = []
            if task_id:
                task_dir = _iv._safe_task_dir(task_id, self.log_root)
                if task_dir is not None:
                    changed = _rv.read_live_projection(task_dir).get("changed_files") or []
            rows = _fv.file_picker_rows(
                self.repo,
                query,
                changed_files=changed,
                include_symbols=True,
                limit=140,
            )
        except Exception:
            rows = []
        if not rows:
            self.transcript("[vex.muted]no repository files or symbols match[/]")
            return

        def _chosen(row: Optional[Dict[str, Any]]) -> None:
            if not isinstance(row, Mapping) or row.get("kind") == "directory":
                return
            path = str(row.get("path") or row.get("file") or "")
            if not path:
                return
            mention = str(row.get("qualified") or path)
            try:
                target = self.query_one("#vex-input", Input)
                current = target.value.strip()
                target.value = f"{current} @{mention}".strip()
                target.focus()
            except Exception:
                self.transcript(f"[vex.accent2]@{escape(mention)}[/]")

        self.push_screen(_FilesScreen(rows, query), _chosen)

    def _open_checkpoints_screen(self, query: str = "") -> None:
        """Open the checkpoint timeline with compare and restore actions."""
        task_id = self._active_task_id()
        records = _iv._checkpoint_lines(self.log_root, task_id, self.repo)
        if not records:
            self.transcript("[vex.muted]no checkpoints recorded[/]")
            return
        self.push_screen(_CheckpointsScreen(records, query), self._checkpoint_chosen)

    def _checkpoint_chosen(self, record: Optional[Dict[str, Any]]) -> None:
        """Open checkpoint compare and explicit restore choices."""
        if not isinstance(record, Mapping):
            return
        checkpoint_id = str(record.get("checkpoint_id") or record.get("resume_token") or "")
        if not checkpoint_id:
            self.transcript("[vex.muted]checkpoint has no durable id[/]")
            return
        try:
            review = _fv.compare_checkpoint(
                self.repo, self.log_root, checkpoint_id
            )
        except Exception as exc:
            self.transcript(f"[vex.error]checkpoint review failed: {escape(type(exc).__name__)}[/]")
            return
        body: List[Text] = [
            Text(f"checkpoint {checkpoint_id}", style=f"bold {ui.ACCENT_TEXT}"),
            Text(str(review.get("status") or "unknown"), style=ui.TEXT_SECONDARY),
        ]
        for value in review.get("files", [])[:80]:
            if isinstance(value, Mapping):
                body.append(
                    Text(
                        f"{value.get('path', '?')} · {value.get('status', '?')}",
                        style=ui.TEXT_PRIMARY,
                    )
                )
        self.push_screen(
            _CheckpointActionsScreen(
                checkpoint_id,
                body,
                self._checkpoint_restore,
            )
        )

    def _checkpoint_restore(self, checkpoint_id: str, include_conversation: bool) -> None:
        """Restore only after an explicit action selection and report conflicts."""
        try:
            result = _fv.restore_checkpoint(
                self.repo,
                self.log_root,
                checkpoint_id,
                include_conversation=include_conversation,
            )
        except Exception as exc:
            self.transcript(f"[vex.error]checkpoint restore failed: {escape(type(exc).__name__)}[/]")
            return
        if result.get("ok"):
            self.transcript(
                f"[vex.ok]restored {escape(checkpoint_id)}[/] "
                f"[vex.muted]({len(result.get('restored_files') or [])} file(s))[/]"
            )
        else:
            self.transcript(
                f"[vex.warn]restore refused[/] [vex.muted]{escape(str(result.get('status') or 'conflict'))}[/]"
            )
            for conflict in result.get("conflicts", [])[:8]:
                self.transcript(f"  [vex.muted]{escape(str(conflict))}[/]")

    def _open_context_screen(self, query: str = "") -> None:
        """Compile cited context off the UI thread and open its source panel."""
        if self._context_thread is not None and self._context_thread.is_alive():
            self.transcript("[vex.muted]context request already running[/]")
            return
        task_id = self._active_task_id()
        task_dir = self._iv_task_dir(task_id) if task_id else None
        issue = str(self.last.get("issue") or self.last.get("answer") or "")
        if task_dir is not None:
            try:
                issue = str(_rv.read_live_projection(task_dir).get("issue") or issue)
            except Exception:
                pass
        config = dict(self.file_config or {})
        selected = [item for item in str(query or "").split() if item]

        def work() -> None:
            try:
                data = _fv.context_snapshot(
                    self.repo,
                    issue,
                    task_id=task_id,
                    selected_files=selected,
                    config=config,
                )
                self._safe_call(self._context_done, data)
            except Exception as exc:
                self._safe_call(self._context_failed, type(exc).__name__, str(exc))

        self._context_thread = threading.Thread(target=work, daemon=True)
        self._context_thread.start()

    def _iv_task_dir(self, task_id: str) -> Optional[Path]:
        return _iv._safe_task_dir(str(task_id), self.log_root)

    def _context_done(self, data: Dict[str, Any]) -> None:
        self._context_thread = None
        self.push_screen(_ContextSourcesScreen(data))

    def _context_failed(self, kind: str, detail: str) -> None:
        self._context_thread = None
        self.transcript(f"[vex.error]context failed: {escape(kind)}[/] [vex.muted]{escape(detail[:160])}[/]")

    def _open_diagnostics_screen(self) -> None:
        """Open the bounded diagnostics panel with line-level links.

        The language server is consulted on a WORKER thread. It starts a
        subprocess and speaks JSON-RPC, so doing it on the UI thread is a
        multi-hundred-millisecond stall on the one interaction the prompt
        gates at 250 ms — and the previous implementation did it inline
        behind an `except Exception: pass`, which meant a language server
        that raised a `TypeError` produced no diagnostics and no
        explanation at all.
        """
        if self._diagnostics_thread is not None and self._diagnostics_thread.is_alive():
            self.transcript("[vex.muted]diagnostics request already running[/]")
            return

        log_root = self.log_root
        repo = self.repo
        task_id = self._active_task_id()

        def work() -> None:
            try:
                values = _iv._diagnostic_lines(log_root, repo, task_id)
            except Exception as exc:
                self._safe_call(self._diagnostics_done, [], {
                    "available": False,
                    "state": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                return
            try:
                receipt = _fv.lsp_state_report(repo)
            except Exception as exc:
                receipt = {
                    "available": False,
                    "state": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            self._safe_call(self._diagnostics_done, values, receipt)

        self._diagnostics_thread = threading.Thread(target=work, daemon=True)
        self._diagnostics_thread.start()

    def _diagnostics_done(
        self, values: List[Dict[str, Any]], receipt: Dict[str, Any]
    ) -> None:
        self._diagnostics_thread = None
        if not values:
            # Three different facts, three different sentences. "no
            # diagnostics available" made an absent check indistinguishable
            # from a clean workspace.
            self.transcript(
                "[vex.muted]no diagnostics recorded[/] "
                f"[vex.muted]{escape(_a11y.lsp_state_sentence(receipt))}[/]"
            )
            return
        # `_announce` is (key, text) — the key is the transition identity.
        # Calling it with the sentence ALONE raised TypeError inside the
        # worker-thread callback, so the raise happened before push_screen
        # and the panel never opened. It looked fine whenever the list was
        # EMPTY, because that path returns before the announce: a live
        # surface that only worked when it had nothing to show, found by
        # driving the app rather than reading it.
        self._announce("diagnostic", _a11y.describe_diagnostic(values[0]))
        # The callback is the LINK. Without it the panel is a read-only list
        # and the prompt's "must link directly to the affected file/line" is
        # unmet: choosing a diagnostic closed the modal and did nothing. The
        # receipt rides along so the panel can also say whether the live
        # language-server check ran, instead of only announcing it when the
        # list happens to be empty.
        self.push_screen(_DiagnosticsScreen(values, receipt), self._diagnostic_chosen)

    def _open_relevant_screen(self, query: str = "") -> None:
        """Open the ranked, reasoned relevant-files browser.

        The projection falls back to the WORKSPACE when no run is active:
        a task-scoped projection needs a task directory, so a fresh session
        previously answered "no relevant files" on a working tree with forty
        modified files — which is a statement about the surface, not about
        relevance.
        """
        projection = _fv.relevant_projection(
            self.log_root, self.repo, self._active_task_id(), include_git=True
        )
        rows = _fv.relevant_file_rows(projection, query, limit=140)
        if not rows:
            self.transcript(
                "[vex.muted]no relevant files — nothing is changed, staged, "
                "or cited for this run[/]"
            )
            return

        def _chosen(row: Optional[Dict[str, Any]]) -> None:
            if not isinstance(row, Mapping):
                return
            path = str(row.get("path") or "")
            if not path:
                return
            try:
                target = self.query_one("#vex-input", Input)
                current = target.value.strip()
                target.value = f"{current} @{path}".strip()
                target.focus()
            except Exception:
                self.transcript(f"[vex.accent2]@{escape(path)}[/]")

        self.push_screen(_RelevantFilesScreen(rows, query), _chosen)

    def _diagnostic_chosen(self, value: Optional[Dict[str, Any]]) -> None:
        """Open a diagnostic's linked file range in a read-only detail modal."""
        if not isinstance(value, Mapping):
            return
        path = str(value.get("path") or value.get("file") or "")
        line = int(value.get("line") or 1)
        if not path:
            self.transcript("[vex.muted]diagnostic has no file link[/]")
            return
        try:
            target = self.query_one("#vex-input", Input)
            current = target.value.strip()
            target.value = f"{current} @{path}:{line}".strip()
            target.focus()
        except Exception:
            self.transcript(f"[vex.accent2]@{escape(path)}:{line}[/]")

    def _expand_feed_entry(self, entry: "_tl.FeedEntry") -> None:
        """Push the read-only detail modal for one feed entry (raw
        command + output / the model's whole reply / the event JSON).

        The WHOLE detail is paged. The previous `[:400]` slice meant a
        failing test's 800th assertion line simply did not exist for the
        user, with nothing on screen saying so.
        """
        title = f"trace {entry.index} — {entry.detail_title or entry.category}"
        body: List[Text] = [
            Text(entry.summary, style=f"bold {ui.ACCENT_TEXT}"),
            Text(""),
        ]
        detail = (entry.detail or "").strip() or "(no detail recorded)"
        body.extend(Text(line) for line in detail.splitlines())
        self.push_screen(_TraceDetailScreen(title, body))

    def _open_sessions_screen(self, query: str = "") -> None:
        """/sessions [query] — the searchable, filterable session browser.

        Cross-repo by default: the global per-repo index is the read model,
        so the browser lists every repository's sessions from any CWD
        without re-reading a task trace. A `repo:` token narrows it. Falls
        back to the local log root when the index has nothing.
        """
        rows: List[Dict[str, Any]] = []
        try:
            from cli.session import index_records

            # `root_scoped` is the isolation contract: a caller that chose
            # this log root (a configured log_root, a headless run, a test)
            # must not be shown the machine's other repositories' sessions.
            rows = [
                _iv.normalize_index_row(row)
                for row in index_records(
                    limit=500,
                    log_root=self.log_root if self._root_scoped else None,
                )
                if _iv._index_matches(row, query)
            ]
        except Exception:
            rows = []
        if rows:
            self.push_screen(_SessionsScreen(rows, query), self._sessions_chosen)
            return
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

    def _fork_command(self, rest: str, in_flight: bool) -> None:
        """/fork [turn-id] — fork this conversation with a new session id."""
        if in_flight:
            self.transcript("[vex.warn]wait for the run to finish before forking[/]")
            return
        if not isinstance(self.conversation, dict):
            self.transcript("[vex.muted]no conversation to fork yet[/]")
            return
        token, _flags = _iv._session_arg(rest)
        try:
            from cli.session import fork_session, save_session

            fork = fork_session(
                self.log_root,
                str(self.conversation.get("session_id") or ""),
                self.repo,
                at_turn_id=token or None,
            )
            self.conversation = fork
            self.state["conversation"] = fork
            save_session(self.log_root, fork)
        except Exception as exc:
            self.transcript(f"[vex.error]fork failed:[/] {escape(str(exc))}")
            return
        self.transcript(
            f"[vex.ok]forked[/] [vex.muted]{escape(str(fork.get('session_id')))} "
            f"· {len(fork.get('turns') or [])} turns copied · diverges from "
            f"{escape(str(fork.get('parent_session_id') or 'the parent'))}[/]"
        )
        self.transcript(
            "[vex.muted]the parent conversation is untouched[/]"
        )

    def _import_command(self, rest: str, in_flight: bool) -> None:
        """/import <export.json> [--overwrite] — adopt a session export."""
        if in_flight:
            self.transcript("[vex.warn]wait for the run to finish before importing[/]")
            return
        source, flags = _iv._session_arg(rest)
        if not source:
            self.transcript("[vex.muted]usage: /import <export.json> [--overwrite][/]")
            return
        path = Path(source)
        if not path.is_file():
            self.transcript(f"[vex.error]no such export:[/] {escape(str(path))}")
            return
        try:
            from cli.session import import_session, save_session

            imported = import_session(
                str(path),
                self.log_root,
                self.repo,
                overwrite="--overwrite" in flags,
            )
            self.conversation = imported
            self.state["conversation"] = imported
            save_session(self.log_root, imported)
        except Exception as exc:
            self.transcript(f"[vex.error]import failed:[/] {escape(str(exc))}")
            return
        self.transcript(
            f"[vex.ok]imported[/] [vex.muted]{escape(str(imported.get('session_id')))} "
            f"· {len(imported.get('turns') or [])} turns · "
            f"{int(imported.get('event_count') or 0)} journal rows[/]"
        )

    def _recover_command(self, rest: str, in_flight: bool) -> None:
        """/recover [session-id] [--fresh|--backup] — report or quarantine."""
        if in_flight:
            self.transcript("[vex.warn]wait for the run to finish before recovering[/]")
            return
        token, flags = _iv._session_arg(rest)
        strategy = "report"
        if "--backup" in flags:
            strategy = "backup"
        elif "--fresh" in flags:
            strategy = "fresh"
        try:
            from cli.session import (
                inspect_session,
                list_conversations,
                load_or_create,
                recover_corrupt_session,
                resolve_session_token,
                startup_recovery_candidate,
            )

            session_id = ""
            if token:
                resolved = resolve_session_token(self.log_root, token, self.repo)
                if resolved.get("status") == "ok":
                    session_id = str(resolved["session_id"])
                else:
                    self.transcript(
                        f"[vex.error]{resolved.get('status')} session id:[/] "
                        f"{escape(str(token))}"
                    )
                    return
            else:
                # No id: prefer the newest UNREADABLE conversation (the case
                # recovery exists for), then the newest healthy one.
                candidate = startup_recovery_candidate(self.log_root, self.repo)
                records = list_conversations(self.log_root, self.repo)
                if candidate is None and not records:
                    self.transcript(
                        "[vex.muted]no conversation recorded for this repo[/]"
                    )
                    return
                session_id = str(
                    (candidate or {}).get("session_id") or records[0]["session_id"]
                )
            report = inspect_session(self.log_root, session_id, self.repo)
            self.transcript(
                f"[vex.accent]{escape(session_id)}[/] [vex.muted]"
                f"{escape(str(report.get('status')))} · "
                f"{report.get('turn_count', 0)} turns · "
                f"{report.get('event_count', 0)} journal rows[/]"
            )
            if report.get("status") == "ok":
                self.transcript("[vex.muted]nothing to recover[/]")
                return
            self.transcript(
                f"[vex.warn]{escape(str(report.get('error') or 'unreadable'))}[/]"
            )
            if strategy == "report":
                self.transcript(
                    "[vex.muted]nothing was changed — `/recover "
                    f"{escape(session_id)} --fresh` quarantines the file "
                    "(never deletes it)[/]"
                )
                return
            result = recover_corrupt_session(
                self.log_root, session_id, self.repo, strategy=strategy
            )
            if str(result.get("action") or "") == "quarantined_and_recreated":
                self.conversation = load_or_create(
                    self.log_root, self.repo, session_id, strict=False
                )
                self.state["conversation"] = self.conversation
                self.transcript(
                    "[vex.ok]recovered[/] [vex.muted]quarantined to "
                    f"{escape(str(result.get('quarantine_path') or ''))}[/]"
                )
            else:
                self.transcript(
                    f"[vex.ok]restored[/] [vex.muted]{escape(str(result.get('action')))}[/]"
                )
        except Exception as exc:
            self.transcript(f"[vex.error]recover failed:[/] {escape(str(exc))}")

    def _sessions_chosen(self, selection: Any) -> None:
        """Resume only a resumable selection; inspect terminal sessions."""
        if isinstance(selection, dict):
            task_id = str(selection.get("task_id") or "")
            resumable = bool(selection.get("resumable"))
            status = str(selection.get("status") or "")
        else:
            task_id = str(selection or "")
            resumable = False
            status = _iv.session_status(self.log_root, task_id) if task_id else "blocked"
        if not task_id:
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self.transcript("[vex.warn]a run is in flight — wait or /cancel[/]")
            return
        if not resumable and status not in ("running", "resumable"):
            self.transcript(
                f"[vex.muted]inspecting {escape(task_id)} — {escape(status)}; "
                "use /diff or /review for its result[/]"
            )
            self.last["task_id"] = task_id
            self._render_task_status(task_id)
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
        if arg == "diagnostic":
            body = list(self._diagnostic_lines) or [Text("(no captured diagnostics)")]
            self.push_screen(_TraceDetailScreen("backend diagnostics", body))
            return
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
        line = str(line or "").strip()
        if not line:
            return
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

    # -- steering while a run is live (VEX-CEILING-10) ---------------------

    def _composer_text(self) -> str:
        """Read the composer without disturbing focus or the cursor."""
        for widget in self.query("#vex-input"):
            value = getattr(widget, "value", "")
            if value:
                return str(value)
        return ""

    def _take_composer_text(self) -> str:
        """Read and clear the composer so the next keystroke starts clean."""
        for widget in self.query("#vex-input"):
            value = str(getattr(widget, "value", "") or "")
            if value:
                try:
                    widget.value = ""
                except Exception:
                    pass
                return value
        return ""

    def action_steer_boundary(self) -> None:
        """Deliver the composer's text as steering at the next safe boundary.

        The run is NOT interrupted: the loop's own boundary check decides
        when the instruction becomes visible to the model, and in-flight
        work plus its result are preserved. A `/cancel` is the tool for
        stopping work; this is the tool for redirecting it.
        """
        text = self._take_composer_text()
        if not text.strip():
            self.transcript(
                "[vex.muted]nothing to steer — type an instruction first[/]"
            )
            return
        self._steer_live(text)
        self._safe_call(self._set_hints)

    def action_steer_queue(self) -> None:
        """Queue the composer's text WITHOUT interrupting the run.

        Queued steering is delivered at the next safe boundary after any
        already-queued instruction, so a burst of corrections arrives in
        order instead of racing. This is the "queue without interrupting"
        half of the steering contract.
        """
        text = self._take_composer_text()
        if not text.strip():
            self.transcript("[vex.muted]nothing to queue — type an instruction first[/]")
            return
        pending = _iv.steer_live_run(
            text,
            self._live_task_id() or "",
            Path(self.log_root),
            source="tui",
            say=self.transcript,
            queue_only=True,
        )
        self._queued_steering.append(text)
        self.transcript(
            f"[vex.accent]queued[/] [vex.muted](position "
            f"{len(self._queued_steering)}; delivered at the next safe boundary)[/]"
        )
        del pending
        self._safe_call(self._set_hints)

    def _live_task_id(self) -> Optional[str]:
        """The live task id, or None when no run is registered."""
        try:
            live = _iv._live_run()
        except Exception:
            live = None
        if live and live.get("task_id"):
            return str(live["task_id"])
        run = getattr(self, "_run", None)
        return str(run.task_id) if run is not None else None

    def action_detach(self) -> None:
        """Leave the run alive and stop projecting it (`/detach`).

        The run's worker thread is NOT cancelled: this writes the
        background control record, hides the run-line, and lets the user
        leave. `vex watch` follows the same run from the journal, and
        `/attach` rebinds with a full replay.
        """
        task_id = self._live_task_id()
        if not task_id:
            self.transcript("[vex.warn]no live run to detach[/]")
            return
        mode = str(self._dispatched_mode or (self._run.mode if self._run else "fix"))
        path = _bg.detach(self.log_root, task_id, mode=mode, note="tui /detach")
        if path is None:
            self.transcript(
                f"[vex.error]could not detach {escape(task_id)} "
                "(control record not writable) — the run continues in this window[/]"
            )
            return
        # Stop the projection without touching the run: the tailer stops,
        # the run-line hides, and the worker keeps going.
        self._detached_task_id = str(task_id)
        if self._run_stop is not None:
            self._run_stop.set()
            self._run_stop = None
        self._run = None
        self.state["active_task_id"] = None
        self._safe_call(self._set_status, _STATUS_IDLE)
        self._safe_call(self._hide_runline)
        self._safe_call(self._teardown_side, 0.0)
        self._safe_call(self._set_hints)
        self._dispatched_mode = None
        self.transcript(
            f"[vex.accent]detached[/] [vex.muted]{escape(task_id)} is still running — "
            f"follow with [vex.accent]vex watch {escape(task_id)}[/], "
            f"rebind with [vex.accent]/attach[/][/]"
        )

    def action_attach(self) -> None:
        """Rebind the projection to a detached run, with a full replay.

        The replay is a real fold of the run's own journal, so the
        attached view's event count equals the journal's: no gap, and no
        reliance on state that only existed in the dead process.
        """
        task_id = (self.last.get("task_id") if isinstance(self.last, dict) else "") or ""
        if not task_id:
            listed = _bg.list_detached(self.log_root, limit=1)
            task_id = listed[0].task_id if listed else ""
        if not task_id:
            self.transcript(
                "[vex.warn]no detached run to attach — "
                "pass one with [vex.accent]/attach <task-id>[/][/]"
            )
            return
        receipt = _bg.attach(self.log_root, str(task_id))
        if not receipt.get("attached"):
            self.transcript(
                f"[vex.error]cannot attach {escape(str(task_id))}: "
                f"{escape(str(receipt.get('reason') or 'unknown reason'))}[/]"
            )
            return
        # An attach rebinds an ALIVE run; a finished one is reported, not
        # re-run, and the detach marker is cleared so a later finish
        # notifies normally.
        if receipt.get("phase") in ("done", "failed"):
            self._detached_task_id = ""
        self.transcript(
            f"[vex.accent]attached[/] [vex.muted]{escape(str(task_id))} — "
            f"replayed {receipt.get('events', 0)} events · "
            f"phase {escape(str(receipt.get('phase') or 'unknown'))}"
            + (f" · status {escape(str(receipt.get('status')))}" if receipt.get("status") else "")
            + "[/]"
        )
        detail = str(receipt.get("live_text") or "").strip()
        if detail:
            self.transcript(f"[vex.muted]{escape(detail[-600:])}[/]")
        self._safe_call(self._set_hints)

    def _show_pending_run(self, verb: str) -> None:
        """Show a stable skeleton until the journal publishes a task id."""
        self._pending_run_text = f"* {verb} · waiting for task journal · /cancel"
        self._render_run()
        # A new run starts a new announcement history: the gate is reset
        # here rather than at finish, so a run that never publishes a
        # task id (a refusal, an immediate error) still announces, and so
        # two consecutive runs cannot suppress each other's first phase.
        self._announcements.reset()
        self._settled_outcome_task = ""
        self._announce("queued", _a11y.announce_queued())

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
            "plan": "planning in",
            "explore": "exploring in",
            "review": "reviewing in",
            "debug": "debugging in",
            "ask": "answering about",
            "question": "answering about",
            "research": "researching",
            "agent_task": "working in",
            "agent": "working in",
        }.get(mode, "working in")
        if mode in ("agent", "agent_task"):
            mode = "agent_task"
        self._show_pending_run(verb)
        repo_part = (
            f" [vex.accent]{Path(self.repo).name}[/]" if mode != "research" else ""
        )
        self.transcript(
            f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]{verb}[/]{repo_part}"
        )
        explicit_mode = str(self.state.get("mode") or "auto").lower() not in ("", "auto")
        target = {
            "fix": self._fix_worker,
            "build": (
                (lambda value, _mode=mode: self._product_mode_worker(value, _mode))
                if explicit_mode
                else self._build_worker
            ),
            "plan": lambda value, _mode=mode: self._product_mode_worker(value, _mode),
            "explore": lambda value, _mode=mode: self._product_mode_worker(value, _mode),
            "review": lambda value, _mode=mode: self._product_mode_worker(value, _mode),
            "debug": lambda value, _mode=mode: self._product_mode_worker(value, _mode),
            "ask": lambda value, _mode=mode: self._product_mode_worker(value, _mode),
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
            selected = str(self.state.get("mode") or "auto").lower()
            self._start_run(
                request,
                mode=selected if selected not in ("", "auto") else "agent_task",
            )
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
            selected = str(self.state.get("mode") or "auto").lower()
            self._start_run(
                request,
                mode=selected if selected not in ("", "auto") else "agent_task",
            )

    def _agent_plan_chosen(self, request: str, answer: Optional[str]) -> None:
        """Callback for the agent plan confirm modal."""
        try:
            ans = (answer or "y").strip().lower()
        except Exception:
            ans = "y"
        if ans in ("", "y", "yes", "run"):
            self._pending_agent_guidance = request
            self.transcript("[vex.ok]approved — starting the agent[/]")
            selected = str(self.state.get("mode") or "auto").lower()
            self._start_run(
                request,
                mode=selected if selected not in ("", "auto") else "agent_task",
            )
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
        selected = str(self.state.get("mode") or "auto").lower()
        self._start_run(
            request,
            mode=selected if selected not in ("", "auto") else "agent_task",
        )

    def _start_resume(self, task_id: str) -> None:
        """Resume a previous run by id (REPL's _resume_task, threaded).

        Records the /resume line as a user turn first, so the resumed
        run keeps talking in the same conversation (not a restart)."""
        if _iv._safe_task_dir(task_id, self.log_root) is None:
            self.transcript(
                f"[vex.error]invalid task id: {escape(str(task_id))!r} "
                "(expected a single contained path segment)[/]"
            )
            self._mark_handler_result("failed", 1)
            return
        session_state = _iv.session_status(self.log_root, task_id)
        if session_state not in ("running", "resumable"):
            self.transcript(
                f"[vex.warn]cannot resume {escape(str(task_id))}: "
                f"session is {escape(session_state)}[/]"
            )
            self._mark_handler_result("failed", 1)
            return
        if isinstance(self.conversation, dict):
            try:
                from cli.session import append_turn, save_session

                append_turn(self.conversation, "user", f"/resume {task_id}")
                save_session(self.log_root, self.conversation)
            except Exception:
                pass
        start = _iv._first_event(
            _iv._safe_task_dir(task_id, self.log_root) / "trace.jsonl", "task_start"
        )
        _start_kind, start_data, _start_ts, _start_identity = _rv.event_parts(start or {})
        run_spec = start_data.get("run_spec") if isinstance(start_data, dict) else None
        metadata = run_spec.get("metadata") if isinstance(run_spec, dict) else None
        reported_mode = str(start_data.get("mode") or "")
        if not reported_mode and isinstance(metadata, dict):
            reported_mode = str(metadata.get("mode") or metadata.get("agent_mode") or "")
        mode_aliases = {
            "agent": "agent_task",
            "agent_task": "agent_task",
            "planning": "plan",
            "daily": "build",
            "question": "question",
            "research": "research",
            "connector": "connector",
            "mcp": "connector",
        }
        self._dispatched_mode = mode_aliases.get(reported_mode, "fix")
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
            self._store_diagnostic(self._cap.last_lines)
            self._thread_log(
                "[vex.warn]interrupted — containers cleaned, checkpoints "
                "kept ([vex.accent]vex --continue[/][vex.warn] resumes)[/]"
            )
            self._finish_run()
            return
        except Exception as exc:  # explain, never a traceback
            self._store_diagnostic(self._cap.last_lines)
            self._thread_log(
                f"[vex.error]run failed: {escape(type(exc).__name__)}[/] "
                "[vex.muted](details available with /trace)[/]"
            )
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
            self._store_diagnostic(lines)
            if isinstance(result, dict):
                self._safe_call(self._note_result, result)
            else:
                log("[vex.warn]run returned no structured result[/]")
        except KeyboardInterrupt:
            self._store_diagnostic(self._cap.last_lines)
            log("[vex.warn]interrupted[/]")
        except Exception as exc:  # explain, never a traceback
            self._store_diagnostic(self._cap.last_lines)
            log(
                f"[vex.error]run failed: {escape(type(exc).__name__)}[/] "
                "[vex.muted](details available with /trace)[/]"
            )
        finally:
            self._finish_run()

    def _product_mode_worker(self, request: str, mode: str) -> None:
        """Worker for an explicitly selected Plan/Build/Explore/Review/Debug/Ask mode."""

        def call():
            context = _iv._build_agent_session_context(
                request, self.repo, self.state, self.log_root, self.last
            )
            self.state["session_context"] = context
            return _iv._run_one_mode(
                request,
                self.repo,
                self.state,
                self.log_root,
                self.file_config or None,
                mode=mode,
                session_context=context,
                approve_fn=self._agent_approve_kernel,
            )

        self._mode_worker(mode, call, request)

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

            guidance = self._pending_agent_guidance
            self._pending_agent_guidance = None
            context = _iv._build_agent_session_context(
                request, self.repo, self.state, self.log_root, self.last
            )
            self.state["session_context"] = context
            fn = _iv._run_one_agent
            try:
                params = _inspect.signature(fn).parameters
                accepts_kw = (
                    "approve_fn_override" in params
                    or "plan_guidance" in params
                    or "session_context" in params
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
                    session_context=context,
                )
            # Legacy fake without the new kwargs (test scaffolding):
            # same run, default approval + no plan guidance.
            return fn(
                request, self.repo, self.state, self.log_root, self.file_config or None
            )

        self._mode_worker("agent_task", call, request)

    def _agent_approve_kernel(self, call: Any, decision: Any) -> Tuple[bool, str]:
        """Open the scoped approval modal for a typed kernel tool call."""
        try:
            tool = str(getattr(call, "tool", "") or "tool")
            arguments = dict(getattr(call, "arguments", {}) or {})
            effect = str(getattr(decision, "exact_effect", "") or "")
            request = _commands.approval_request_view(
                {
                    "request_id": str(getattr(decision, "request_id", "") or ""),
                    "fingerprint": str(getattr(decision, "fingerprint", "") or ""),
                    "task_id": self._active_task_id()
                    or (self._run.task_id if self._run else ""),
                    "repo_path": str(getattr(decision, "repo_path", "") or self.repo),
                    "paths": getattr(decision, "paths", None) or arguments.get("path"),
                    "command": arguments.get("command")
                    or str(getattr(decision, "command", "") or ""),
                    "server": arguments.get("server")
                    or str(getattr(decision, "server", "") or ""),
                    "side_effect": str(getattr(decision, "side_effect", "") or tool),
                    "summary": effect,
                }
            )
            policy = self.state["approval_policy"]
            matching = policy.matching(request)
            kernel_scopes = {
                "once": "once",
                "session": "session_path",
                "path": "session_path",
                "command": "session_command_prefix",
            }
            if matching is not None:
                return True, kernel_scopes.get(matching.scope, "once")
            body = [
                Text(
                    ui.strip_ansi(
                        f"{tool}: {json.dumps(arguments, ensure_ascii=False)[:220]}"
                    )
                )
            ]
            if effect:
                body.append(
                    Text(ui.strip_ansi(effect)[:400], style=ui.TEXT_SECONDARY)
                )
            self._pending_prompt_body = body
            answer = self._prompt_modal(
                f"allow {tool.upper()}? [y=once s=session p=path c=command n=reject] ",
                timeout_s=request.timeout_s,
            )
            if answer is None and self._prompt_timed_out:
                self._thread_log(
                    f"[vex.warn]no answer for {escape(tool.upper())} before its "
                    "deadline — the call was NOT approved[/]"
                )
                return False, "once"
            approved, scope = _commands.approval_from_answer(answer, scoped=True)
            if approved:
                policy.record(request, scope)
                self._thread_log(f"[vex.ok]approval scope: {escape(scope)}[/]")
                return True, kernel_scopes.get(scope, "once")
            self._thread_log(f"[vex.warn]rejected {escape(tool.upper())} — skipped[/]")
            return False, "once"
        except Exception:
            return False, "once"

    def _agent_approve_fn(self, tool: str, args: Dict[str, Any], preview: str) -> bool:
        """Apply the shared exact-effect approval policy to a legacy agent tool call."""
        try:
            arguments = dict(args or {})
            side_effect = (
                "process"
                if tool in {"bash", "shell", "process"}
                else "network"
                if tool in {"fetch", "webfetch"}
                else "external"
                if tool in {"mcp", "mcp_call"}
                else "workspace_write"
            )
            path_value = arguments.get("path") or arguments.get("file")
            paths = path_value if isinstance(path_value, (list, tuple)) else [path_value]
            view = _commands.approval_request_view(
                {
                    "request_id": f"agent-{time.time_ns()}",
                    "task_id": (
                        self._active_task_id()
                        or (self._run.task_id if self._run is not None else "")
                    ),
                    "repo_path": str(self.repo),
                    "paths": [str(item) for item in paths if item],
                    "command": str(arguments.get("command") or ""),
                    "server": str(arguments.get("server") or ""),
                    "side_effect": side_effect,
                    "summary": str(preview or ""),
                }
            )
            policy = self.state.setdefault("approval_policy", _commands.ApprovalPolicy())
            matching = policy.matching(view)
            if matching is not None:
                self._thread_log(
                    f"[vex.ok]approval reused: {escape(matching.scope)} scope[/]"
                )
                return True
            try:
                ui.bell("vex approval needed")
            except Exception:
                pass
            body = [Text(ui.strip_ansi(view.effect_summary()))]
            for line in ui.strip_ansi(preview or "").splitlines()[:14]:
                body.append(Text(line[:200]))
            self._pending_prompt_body = body[:25]
            answer = self._prompt_modal(
                f"allow {tool.upper()}? [y=once s=session p=path c=command n=reject] ",
                timeout_s=view.timeout_s,
            )
            if answer is None:
                if self._prompt_timed_out:
                    self._thread_log(
                        f"[vex.warn]no answer for {escape(tool.upper())} before "
                        "its deadline — the call was NOT approved[/]"
                    )
                return False
            approved, scope = _commands.approval_from_answer(answer, scoped=True)
            if approved:
                policy.record(view, scope)
                self._thread_log(f"[vex.ok]approval scope: {escape(scope)}[/]")
                return True
            self._thread_log(f"[vex.warn]rejected {tool.upper()} — skipped[/]")
            return False
        except Exception:
            return False

    def _resume_worker(self, task_id: str) -> None:
        """Worker body for /resume (REPL's _resume_task, captured)."""
        if _iv._safe_task_dir(task_id, self.log_root) is None:
            self._thread_log(
                f"[vex.error]invalid task id: {escape(str(task_id))!r} "
                "(expected a single contained path segment)[/]"
            )
            return
        self._worker_ident = threading.get_ident()
        log = self._thread_log
        try:
            with self._prompt_patches():
                result, lines = self._cap.capture(
                    _iv._resume_task, task_id, self.log_root, self.state
                )
            self._store_diagnostic(lines)
            if isinstance(result, dict):
                self._safe_call(self._note_result, result)
        except KeyboardInterrupt:
            self._store_diagnostic(self._cap.last_lines)
            log(
                "[vex.warn]interrupted — resumable via [vex.accent]vex --continue[/]"
            )
        except Exception as exc:
            self._store_diagnostic(self._cap.last_lines)
            log(
                f"[vex.error]resume of {escape(task_id)} failed: {escape(type(exc).__name__)}[/] "
                "[vex.muted](details available with /trace)[/]"
            )
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
            self._store_diagnostic(lines or self._cap.last_lines)
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
                app._store_diagnostic([Text(text)])

            with _PROMPT_PATCH_LOCK:
                previous_input = builtins.input
                previous_print = builtins.print
                _PROMPT_PATCH_STACK.append((_patched_input, _patched_print))
                builtins.input = _patched_input  # type: ignore[assignment]
                builtins.print = _patched_print  # type: ignore[assignment]
            try:
                yield
            finally:
                with _PROMPT_PATCH_LOCK:
                    _PROMPT_PATCH_STACK.pop()
                    if _PROMPT_PATCH_STACK:
                        restore_input, restore_print = _PROMPT_PATCH_STACK[-1]
                    else:
                        restore_input, restore_print = previous_input, previous_print
                    builtins.input = restore_input  # type: ignore[assignment]
                    builtins.print = restore_print  # type: ignore[assignment]

        return _ctx()

    def _prompt_modal(
        self, prompt: str, *, timeout_s: Optional[float] = None
    ) -> Optional[str]:
        """Blocking input() on a backend thread -> modal on the UI thread.

        The modal's body shows what the backend just printed (plan
        steps / the diff) — snapshotted from the live capture buffer as
        styled Text. y/n confirm prompts get the confirm screen (empty
        = default, Esc = 'n'); free-text prompts get Esc-cancel (the
        backend then sees EOFError, exactly like a closed stdin).
        Returns None only when the app is shutting down, the push
        failed (the backend sees EOFError — safe, run-gating prompts
        default to the safe answer), or the deadline passed.

        A deadline is HONORED, not merely applied to the wait. `None` alone
        cannot tell a caller "the user said no" from "nobody answered", so
        `self._prompt_timed_out` records which one it was, the pushed screen
        is popped, and the transcript says so. Leaving the screen up would
        leave a zombie modal: the user answers a question whose run already
        gave up, and the next prompt stacks on top of it.
        """
        if self._shutting_down:
            return None
        modal_started = time.perf_counter()
        done = threading.Event()
        answer: Dict[str, Any] = {"value": None}
        self._prompt_timed_out = False
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
                    # A modal is a blocking screen the user must find, so
                    # the effect AND the keys are spoken, in the
                    # transcript (new output) as well as the region. A
                    # "waiting for approval" chip names neither what is
                    # being asked nor how to answer it.
                    self._announce(
                        ("approval", str(prompt)[:200]),
                        _a11y.announce_approval(prompt, (body[0] if body else "")),
                        transcript=True,
                    )
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
                self.ui_metrics.observe("modal_open_ms", modal_started)
            except Exception:
                self.ui_metrics.observe("modal_open_ms", modal_started)
                done.set()  # app dying: cancel the prompt

        try:
            self.call_from_thread(_push)
        except Exception:
            return None
        wait_seconds = 86400.0
        if timeout_s is not None:
            try:
                wait_seconds = max(0.1, float(timeout_s))
            except (TypeError, ValueError):
                pass
        if not done.wait(timeout=wait_seconds):
            self._prompt_timed_out = True
            self._safe_call(self._dismiss_prompt_screen)
            self._thread_log(
                f"[vex.warn]no answer within {wait_seconds:g}s — the request "
                "was NOT approved[/]"
            )
        if is_confirm:
            self._safe_call(self._set_status, _STATUS_RUNNING)
        return answer["value"]

    def _dismiss_prompt_screen(self) -> None:
        """Pop a prompt modal that its own deadline already answered.

        Runs on the UI thread via `_safe_call`. A no-op when no modal is on
        the stack, so a late deadline cannot pop a DIFFERENT screen the user
        opened in the meantime.
        """
        try:
            screen = self.screen
        except Exception:
            return
        if isinstance(screen, _PromptScreen):
            try:
                self.pop_screen()
            except Exception:
                pass

    def _note_result(self, info: Dict[str, Any]) -> None:
        """Keep presentation-only result fields; completion state remains journal-backed."""
        for key in (
            "status",
            "kernel_status",
            "verification",
            "verification_state",
            "cost_usd",
            "model_calls",
            "files_touched",
        ):
            self.last.pop(key, None)
        for key in ("task_id", "log_root", "mode", "diff"):
            if key in info:
                self.last[key] = info[key]
        if info.get("answer"):
            self.last["answer"] = ui.strip_ansi(str(info["answer"]))
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
        task_id = run.task_id if run is not None else self.last.get("task_id")
        mode = run.mode if run is not None else str(
            self._dispatched_mode or self.last.get("mode") or "fix"
        )
        self._run = None
        self.state["active_task_id"] = None
        self._status = _STATUS_IDLE
        self._safe_call(self._set_status, _STATUS_IDLE)
        self._safe_call(self._hide_runline)
        if task_id is not None:
            # final sidebar snapshot (all steps resolved), then the
            # completion card (Task C) — re-derived from the records
            self._safe_call(self._refresh_machine_state, task_id)
            self._safe_call(self._render_side, run)
            self._safe_call(self._render_context, run)
            self._safe_call(self._render_card, task_id, mode)
            # The outcome is the one announcement a user must never be
            # able to miss, so the region is NOT left showing a stale
            # phase afterwards. `_render_card` wrote the sentence; this
            # drops the gate so the NEXT run announces from scratch.
            self._safe_call(self._announcements.reset)
            self._safe_call(self._teardown_side, 2.0)
        # VEX-CEILING-10: notify completion AND failure, from the ONE seam
        # every mode's worker passes through. The REPL's own notify_done
        # defers to the TUI hook being set, so there is never a double
        # notification. A failed run escalates (repeated bell + distinct
        # desktop toast) — a silent failure is the defect this closes.
        # A run that was DETACHED is not notified here: its terminal
        # result belongs to whoever watches or attaches, and a detached
        # run's worker may outlive this process.
        try:
            if task_id is not None and str(task_id) != str(
                getattr(self, "_detached_task_id", "") or ""
            ):
                status = str(run.projection.status) if run is not None else ""
                if not status:
                    status = (
                        str(self.last.get("status") or "")
                        if isinstance(self.last, dict)
                        else ""
                    )
                _notify.notify_run(
                    status or "failed",
                    label=f"{mode} run",
                    detail=str(task_id or ""),
                )
        except Exception:
            pass
        self._safe_call(self._set_hints)
        self._dispatched_mode = None
        self._safe_call(self._after_run)

    def _result_facts(self, task_id: str, mode: str) -> Dict[str, Any]:
        """Merge returned result fields with facts re-read from the trace."""
        try:
            facts = _rv.read_run_facts(self.log_root / task_id)
        except Exception:
            facts = {"task_id": task_id}
        info = self.last if self.last.get("task_id") == task_id else {}
        if not facts.get("answer") and info.get("answer"):
            facts["answer"] = ui.strip_ansi(str(info["answer"]))
        if not facts.get("task_id"):
            facts["task_id"] = task_id
        if isinstance(facts.get("model_calls"), list):
            facts["model_calls"] = len(facts["model_calls"])
        if isinstance(facts.get("verification"), dict):
            facts.setdefault("latest_verification", facts["verification"])
        if facts.get("status") is None:
            facts["status"] = "unknown"
        if not facts.get("mode"):
            facts["mode"] = mode
        return facts

    def _render_card(self, task_id: str, mode: str = "fix") -> None:
        """Render exactly one verifier-gated completion result for a run,
        plus the recovery card when the run did not succeed.

        R2-17 (item 6): the recovery affordance belongs in the surface
        where the failure happened, which for the TUI is the transcript
        directly under the completion card — not a `/doctor` invocation
        the user has to remember to type. It is drawn from the run's own
        journal via `cli.runview.failure_lines`, so the card and the
        completion card can never describe different runs.
        """
        if task_id in self._completion_rendered:
            return
        self._completion_rendered.add(task_id)
        facts = self._result_facts(task_id, mode)
        self.transcript(ResultCard(task_id=task_id, mode=mode, facts=facts).render())
        self._render_recovery(task_id, facts)
        self._announce_outcome(task_id, facts)

    def _announce_outcome(self, task_id: str, facts: Mapping[str, Any]) -> str:
        """Announce a finished run in words, reusing the card's own verdict.

        The verdict is RE-DERIVED through the same `_rv` reduction the
        `ResultCard` uses rather than read from a raw status field. That
        is the whole point: an announcement is a surface like any other,
        and the defect this repository keeps fixing surface-by-surface is
        an unverified run reading as verified. Routing both through one
        reduction makes them structurally unable to disagree — and a
        screen-reader user is not the only person who cannot see the
        green chip.
        """
        data = dict(facts or {})
        raw_evidence = data.get("verification_evidence")
        if isinstance(raw_evidence, Mapping):
            evidence: List[Any] = [raw_evidence]
        else:
            evidence = list(raw_evidence or [])
        if not evidence and data.get("target_passed") is not None:
            evidence = [data]
        status = _rv.effective_terminal_status(str(data.get("status") or "unknown"), evidence)
        verified = _rv.status_is_verified(status)
        # A zero cost is announced as nothing, not as "$0.000000": a
        # six-decimal zero in a SPOKEN sentence is noise, and R2-17 already
        # established that a cost figure nobody measured must never read
        # as a measured one. A recorded zero is real, but it tells a
        # waiting user nothing they did not already assume.
        cost = ""
        if data.get("cost_usd") is not None:
            try:
                spent = float(data["cost_usd"])
            except (TypeError, ValueError):
                spent = 0.0
            if spent > 0:
                cost = ui.fmt_cost(spent)
        sentence = _a11y.announce_finished(
            status,
            verified=verified,
            files=list(data.get("files") or data.get("changed_files") or []),
            elapsed_s=data.get("elapsed_s"),
            cost=cost,
            calls=int(data["model_calls"]) if data.get("model_calls") else None,
        )
        if not verified and not _rv.status_is_completed(status):
            sentence = _a11y.announce_failure(
                data.get("last_error") or status,
                next_steps=("/doctor", "/trace"),
            )
        # `transcript=True` because the outcome is the one transition a
        # user must never be able to miss: it writes a NEW line, so a
        # screen reader reads it rather than re-reading a static chip.
        # The task is recorded as settled so the live repaint path cannot
        # speak over it — see `_announce_phase`.
        self._settled_outcome_task = str(task_id)
        return self._announce(("finished", str(task_id)), sentence, transcript=True)

    def _render_recovery(
        self, task_id: str, facts: Optional[Dict[str, Any]] = None
    ) -> None:
        """Draw the failure/recovery card for a run that did not succeed.

        Shown for a failed or cancelled run, and for an unverified one
        whose own note names a problem — an unverified run is not a
        failure and must not be given a "what failed" card it did not
        earn. Never raises: a card that cannot be drawn is silence, not a
        crash on the completion path.
        """
        try:
            from cli.runview import failure_lines, run_verdict

            data = dict(facts or self._result_facts(task_id, "fix"))
            status = str(data.get("display_status") or data.get("status") or "")
            verdict = run_verdict(
                status, evidence=data.get("verification_evidence")
            )
            if verdict not in ("failed", "cancelled"):
                return
            excerpt = str(data.get("reason") or data.get("last_error") or "")
            if not excerpt:
                excerpt = _iv._last_run_error(self.log_root, task_id)
            for line in failure_lines(
                excerpt, log_root=self.log_root, task_id=str(task_id)
            ):
                self.transcript(line)
        except Exception:
            pass


    def _teardown_side(self, delay_s: float = 0.0) -> None:
        """Collapse the sidebar once the run's final snapshot has been
        shown (textual timers handle the delay; set_timer takes no
        call args, so the delayed pass closes over nothing).

        The collapse is UNCHANGED from before this round, and the reason is
        named rather than assumed:
        `tests/test_cli_tui.py::TestTodoSidebar::test_sidebar_collapses_after_run`
        pins `#vex-side` to `display: none` two seconds after a run ends. A
        PERSISTENT sidebar — the opencode behaviour, where the column stays
        up carrying the session, the connectors, the language server, and
        the Getting-started card — is therefore **not** delivered by this
        round. The sections, their collapse triangles, their persistence and
        the anti-clutter rule all exist and all render; they render inside
        the rail's existing lifetime. The one-line change that delivers a
        persistent sidebar is dropping the two `display = "none"` lines
        below, and it requires retargeting that assertion. Filed in
        `cli/AGENTS.md` under "Handoff to <tui owner>".
        """
        if self._run is not None:
            return  # a NEW run started meanwhile - keep it live
        try:
            if delay_s <= 0:
                self.query_one("#vex-side", PlanRail).styles.display = "none"
                self.query_one("#vex-context", ContextPanel).styles.display = "none"
            else:
                self.set_timer(delay_s, lambda: self._teardown_side())
        except Exception:
            pass


    def _after_run(self) -> None:
        """UI thread: post-run bookkeeping + queue drain."""
        self._set_status(_STATUS_IDLE)
        self._render_header()
        if not self._queue:
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self.set_timer(0.05, self._after_run)
            return
        nxt = self._queue.pop(0)
        self.transcript(
            f"[vex.muted]{ui.GLYPHS['arrow']}[/] [vex.muted]next queued[/]"
        )
        self._handle_line(nxt)

    # -- live run-line driving (Task B) -----------------------------------

    def _on_task_start_hook(self, task_id: str) -> None:
        """Marshal task start and register the active task for commands."""
        self._safe_call(self.begin_live_run, task_id)

    def begin_live_run(self, task_id: str) -> _RunState:
        """Spin the run state + tail + approval-watch threads for a live
        run; the run-line widget becomes visible and is updated in place
        per trace event, and the sidebar (todo + status panel) opens.
        Must run on the UI thread (called via the _ON_TASK_START hook).
        The trace's own task_start event corrects the mode for modes the
        harness labels itself (question/research/build)."""
        run = _RunState(task_id, mode=self._dispatched_mode or "fix")
        self._pending_run_text = None
        self._last_rendered_event_timestamp = None
        self._completion_rendered.discard(task_id)
        self._approval_handled.discard(task_id)
        self._machine_state = None
        self._run = run
        self.state["active_task_id"] = task_id
        # The task id is the one handle a user needs to cancel, watch, or
        # resume, and the skeleton could not name it. Announced the moment
        # it exists, in the transcript too: "a task is starting" without
        # the id is not actionable, and a person who cannot see the header
        # needs the id spelled out somewhere in the scrollback.
        self._announce(
            ("started", str(task_id)),
            _a11y.announce_run_started(task_id, self._dispatched_mode or run.mode),
            transcript=True,
        )
        stop = threading.Event()
        self._run_stop = stop
        replay_existing = False
        try:
            existing_trace = self.log_root / task_id / "trace.jsonl"
            if existing_trace.is_file() and existing_trace.stat().st_size:
                replay_existing = any(
                    "result" in line or "task_end" in line
                    for line in existing_trace.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-80:]
                )
        except OSError:
            replay_existing = False
        tail = threading.Thread(
            target=_tail_trace,
            args=(task_id, self.log_root, run, stop, self._on_trace_event),
            kwargs={"replay_existing": replay_existing},
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
        self._render_context(run)
        self._set_hints()
        self._set_status(_STATUS_RUNNING)
        self._render_header(_STATUS_RUNNING)
        return run

    def _on_trace_event(
        self,
        run: _RunState,
        entries: List[_tl.FeedEntry],
        event_timestamp: Optional[float] = None,
    ) -> None:
        """Marshal one journal event to the UI thread without batching state."""
        if self._run is not run:
            return
        self._safe_call(self._project_trace_event, run, entries, event_timestamp)

    def _project_trace_event(
        self,
        run: _RunState,
        entries: List[_tl.FeedEntry],
        event_timestamp: Optional[float] = None,
    ) -> None:
        """Render one journal event and measure the UI-thread work it causes."""
        started = time.perf_counter()
        try:
            if self._run is not run:
                return
            kinds = {entry.category for entry in entries}
            if kinds & {"lifecycle", "verify", "diff"} or run.todo.steps:
                self._refresh_machine_state(run.task_id)
            self._render_run(run)
            self._render_side(run)
            self._render_context(run)
            if self.state.get("feed", True):
                self._render_feed_entries(run, entries)
        finally:
            self.ui_metrics.observe("ui_thread_stall_ms", started)

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
        """Render journal feed lines and schedule bounded diff work off-thread."""
        task_dir = self.log_root / run.task_id
        saw_edit = False
        for entry in entries:
            self.transcript(feed_line(entry))
            if entry.category == "diff":
                saw_edit = True
        if saw_edit:
            self._queue_live_diff(task_dir)

    def _compute_live_diff(self, task_dir: Path) -> List[Any]:
        """Compute the current journal-backed diff without touching widgets."""
        try:
            lines = list(
                _tl.live_diff(
                    task_dir / "pristine", task_dir / "work", max_lines=14
                )
                or []
            )
            if not lines and task_dir.name.startswith("agent-"):
                from harness.agent_loop import agent_diff

                start = _iv._first_event(task_dir / "trace.jsonl", "task_start")
                data = (start or {}).get("data") or {}
                repo = str(data.get("repo_path") or self.repo)
                diff = agent_diff(task_dir.name, self.log_root, repo) or ""
                lines = [(line, "ctx") for line in diff.splitlines()[:14]]
            return lines
        except Exception:
            return []

    def _render_live_diff_lines(self, task_dir: Path, lines: List[Any]) -> None:
        """Render a bounded diff preview returned by a worker."""
        if not lines:
            return
        label = (
            f"diff (pristine {ui.GLYPHS['arrow']} work so far)"
            if (task_dir / "work").is_dir()
            else "diff (live changes)"
        )
        self.transcript(f"[vex.muted]{label}[/]")
        self._render_diff_value(lines)
        self.transcript("[vex.muted]end diff[/]")

    def _queue_live_diff(self, task_dir: Path) -> None:
        """Compute a live diff away from the UI thread and render its result."""
        self._diff_generation += 1
        generation = self._diff_generation

        def work() -> None:
            started = time.perf_counter()
            lines = self._compute_live_diff(task_dir)
            self.ui_metrics.observe("live_diff_ms", started)
            if generation != self._diff_generation:
                return
            self._safe_call(self._render_live_diff_lines, task_dir, lines)

        threading.Thread(target=work, daemon=True).start()

    def _render_live_diff(self, task_dir: Path) -> None:
        """Render a small diff preview synchronously for direct callers."""
        self._render_live_diff_lines(task_dir, self._compute_live_diff(task_dir))

    def _approval_watch(self, task_id: str, stop: threading.Event) -> None:
        """While a task is live, surface a parked approval request as the
        confirm modal (with the diff) — the TUI-native equivalent of the
        REPL's watch_for_approvals, scoped to THIS session's task. One
        prompt per request; /approve //reject stay available as manual
        paths (first decider wins, the other finds nothing pending).

        The gate's OWN review log is the authority on whether a request is
        still live: `request.json` survives a timed-out gate, so a surface
        that only polls for the file will happily re-prompt a run that
        already gave up. Prompting again for a dead gate asks the user to
        decide something that can no longer be decided.
        """
        gate = self.log_root / f"{task_id}.runtime" / "approval"
        try:
            from runtime import approval as approval_mod
        except ImportError:
            return
        seen_requests: set[str] = set()
        while not stop.is_set() and not self._shutting_down:
            if task_id in self._approval_handled:
                return
            try:
                req = approval_mod.pending_request(str(gate))
            except Exception:
                req = None
            if req is not None:
                if str(req.get("task_id") or "") not in {"", str(task_id)}:
                    stop.wait(0.25)
                    continue
                settled = _commands.approval_gate_outcome(gate)
                if settled["decision"] in {"approved", "rejected", "timeout"}:
                    # The gate already resolved this request (possibly in
                    # another surface, or by its own deadline).
                    if settled["decision"] == "timeout":
                        self._thread_log(
                            "[vex.warn]the approval request expired before a "
                            "decision arrived — the diff was NOT applied[/]"
                        )
                    self._approval_handled.add(task_id)
                    return
                view = _commands.approval_request_view(req)
                request_key = str(
                    view.request_id
                    or view.fingerprint
                    or f"{view.side_effect}:{view.effect_summary()}"
                )
                if request_key in seen_requests:
                    stop.wait(0.25)
                    continue
                seen_requests.add(request_key)
                self._approval_handled.add(task_id)
                body = [Text(view.effect_summary(), style=ui.TEXT_SECONDARY)]
                body += _diff_body(view.diff or "")
                if view.issue_text:
                    body.insert(0, Text(f"issue: {view.issue_text[:300]}"))
                answer = self._prompt_modal(
                    "approve this fix? [y/N] ", timeout_s=view.timeout_s
                )
                if answer is None:
                    self._approval_handled.discard(task_id)
                    if self._prompt_timed_out:
                        self._thread_log(
                            "[vex.warn]no answer before the approval deadline — "
                            "the diff was NOT applied[/]"
                        )
                    return
                approve = answer.strip().lower() in ("y", "yes")
                try:
                    approval_mod.decide(str(gate), approve=approve)
                except Exception:
                    return
                # Report the DECISION, not the run's outcome: the gate applies
                # a diff only if the run's own verification passes, and this
                # surface cannot observe that.
                self._thread_log(
                    f"[vex.{'ok' if approve else 'error'}]"
                    f"{'approved' if approve else 'rejected'}[/] — "
                    + (
                        "recorded for the gate; the diff is applied only if the "
                        "run's verification passes"
                        if approve
                        else "the gate will not apply the diff"
                    )
                )
                self._approval_handled.discard(task_id)
                stop.wait(0.1)
                continue
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

    _PALETTE_COMMANDS: ClassVar[List[Tuple[str, str, bool]]] = [
        (spec.name, spec.summary, spec.palette_behavior == "run")
        for spec in _commands.COMMAND_SPECS
    ]

    def action_command_palette(self) -> None:
        """ctrl+p — the fuzzy command palette (Task A of the polish round).

        The modal opens on the fast set (built-ins, custom commands,
        recent sessions, plus repo files when the idle prewarm already
        cached them). A cold file scan runs on a worker thread and merges
        itself in through `add_files`, so opening the palette never blocks
        the UI thread on a large repository."""
        started = time.perf_counter()
        warm = self._palette_files_ready()
        screen = _PaletteScreen(self._palette_entries(include_files=warm))
        screen._files_pending = not warm
        self.push_screen(screen, self._palette_chosen)
        self.ui_metrics.observe("modal_open_ms", started)
        if not warm:
            self._load_palette_files(screen)

    def _palette_files_ready(self) -> bool:
        """True when the idle prewarm already cached this repo's files."""
        cache = self._files_cache
        return cache is not None and cache[0] == str(self.repo)

    def _prewarm_palette_files(self) -> None:
        """Populate the palette file cache while the shell is idle.

        `scan_repo_files` shells out to git; doing it at mount time keeps
        the first ctrl+p free of I/O instead of spending the modal budget
        on a subprocess the user never asked for."""

        def _work() -> None:
            try:
                self._palette_files()
            except Exception:
                pass

        try:
            threading.Thread(
                target=_work, name="vex-palette-prewarm", daemon=True
            ).start()
        except Exception:
            pass

    def _load_palette_files(self, screen: "_PaletteScreen") -> None:
        """Scan repo files off the UI thread and hand them to the palette."""

        def _work() -> None:
            try:
                files = self._palette_files()
            except Exception:
                files = []
            try:
                self.call_from_thread(screen.add_files, files)
            except Exception:
                pass

        try:
            threading.Thread(
                target=_work, name="vex-palette-files", daemon=True
            ).start()
        except Exception:
            pass

    def _palette_entries(self, include_files: bool = True) -> List[Dict[str, Any]]:
        """Everything the palette can search: built-ins + custom
        commands, recent sessions, and the repo's files. Each entry is
        {kind, label, hint, value, run}; the hint carries the extra
        search surface (a session's status/repo/date/issue; a file's
        role), and `label` stays the short primary token."""
        entries: List[Dict[str, Any]] = list(
            _commands.command_palette_entries(self._command_context())
        )
        try:
            for name in _commands.command_names(self.state.get("repo")):
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
                # R2-17: the honest verdict label, NOT the lifecycle
                # status word. The lifecycle word collapses
                # completed_verified and completed_unverified into
                # `completed`, so a palette hint used to advertise an
                # unverified run with a word identical to a verified
                # one. `_iv.list_sessions` already derived this from the
                # run's own journal; this is the render half.
                bits = [_honest_session_label(s)]
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
                        "value": tid,
                        "run": True,
                        "session": {
                            "task_id": tid,
                            "status": _honest_session_label(s),
                            "resumable": bool(s.get("resumable")),
                        },
                    }
                )
        except Exception:
            pass
        for rel in self._palette_files() if include_files else []:
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
        if entry.get("kind") == "session":
            self._sessions_chosen(entry.get("session") or entry.get("value"))
            return
        cmd = str(entry.get("value") or "")
        if not cmd:
            return
        if entry.get("disabled"):
            self.transcript(
                f"[vex.warn]{escape(cmd)} unavailable: "
                f"{escape(str(entry.get('disabled_reason') or 'current state'))}[/]"
            )
            recovery = entry.get("failure_recovery") or []
            if recovery:
                self.transcript(
                    f"[vex.muted]next: {escape(' · '.join(str(item) for item in recovery))}[/]"
                )
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
        if self._run is not None:
            self._run.cancel_requested = True
            self._render_run()
        elif self._pending_run_text:
            self._pending_run_text = "* cancel requested · waiting for worker · /cancel"
            self._render_run()
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
            self._thread_log("[vex.warn]run interrupted — checkpoints kept[/]")
            kn(ident, KeyboardInterrupt)
        except Exception:
            pass


def _diff_body(diff: str) -> List[Text]:
    """A request's diff as syntax-highlighted rich Text lines for the
    approval modal body (Task B of the polish round — the same
    language-aware renderer as the inline preview and /diff; falls back
    to the flat role colors if the highlighter ever fails)."""
    safe = ui.strip_ansi(diff or "")
    try:
        return ui.diff_render_lines(safe)
    except Exception:
        out: List[Text] = []
        for line in safe.splitlines():
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


class _SearchListScreen(ModalFrame[Any]):
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
        border: round $vex-accent;
    }
    #sls-title {
        color: $vex-accent;
        text-style: bold;
    }
    #sls-input {
        border: round $vex-accent;
        margin-bottom: 1;
    }
    #sls-list {
        height: 1fr;
        background: $surface;
    }
    #sls-hint {
        color: $vex-secondary; /* text-secondary (design token) */
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
                "type to filter · tab focus · ↑/↓ scroll · enter open · esc close", id="sls-hint"
            )

    def on_mount(self) -> None:
        super().on_mount()
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
    out.append(f" {FEED_LABELS.get(entry.category, 'info')} ", style=style)
    out.append(f"{entry.index:>2} ", style=ui.TEXT_SECONDARY)
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
            # The whole detail, PAGED. The old `[:400]` slice made the
            # lines past 400 unreachable with nothing saying they existed.
            body.extend(Text(ln) for ln in detail.splitlines())
            try:
                self.app.push_screen(_TraceDetailScreen(title, body))
            except Exception:
                pass


class _FilesScreen(_SearchListScreen):
    """Searchable repository file-tree and symbol browser used by `/files`."""

    def __init__(self, rows: List[Any], query: str = "") -> None:
        super().__init__(
            f"repository files — {len(rows)} shown",
            "filter paths or symbols · enter attach",
        )
        self._rows = list(rows)
        self._initial = query

    def on_mount(self) -> None:
        super().on_mount()
        if self._initial:
            self.query_one("#sls-input", Input).value = self._initial
            self._refilter(self._initial)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        for value in self._rows:
            row = value if isinstance(value, Mapping) else {"path": str(value), "kind": "file"}
            label = str(row.get("qualified") or row.get("path") or row.get("label") or "")
            if needle and needle not in label.lower() and needle not in str(row.get("kind") or "").lower():
                continue
            style = ui.TEXT_SECONDARY if row.get("kind") == "directory" else ui.TEXT_PRIMARY
            prompt = Text()
            prompt.append(str(row.get("label") or label), style=style)
            if row.get("qualified") and row.get("qualified") != row.get("path"):
                prompt.append(f"  {row.get('qualified')}:{row.get('line', 0)}", style=ui.TEXT_SECONDARY)
            result.append((prompt, dict(row)))
        return result


class _CheckpointsScreen(_SearchListScreen):
    """Searchable checkpoint receipt browser used by `/checkpoints`."""

    def __init__(self, records: List[Dict[str, Any]], query: str = "") -> None:
        super().__init__(
            f"checkpoints — {len(records)} recorded",
            "filter id · sequence · file · enter inspect",
        )
        self._records = list(records)
        self._initial = query

    def on_mount(self) -> None:
        super().on_mount()
        if self._initial:
            self.query_one("#sls-input", Input).value = self._initial
            self._refilter(self._initial)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        for record in self._records:
            sequence = str(record.get("last_event_sequence", "?"))
            token = str(record.get("resume_token", "") or "—")
            identifier = str(record.get("checkpoint_id") or "—")
            files = record.get("captured_paths") or record.get("agent_owned_changes") or []
            if isinstance(files, list):
                file_text = ", ".join(str(item) for item in files)
            else:
                file_text = ""
            text = f"{identifier} · seq {sequence} · {token} · {file_text}"
            if needle and needle not in text.lower():
                continue
            prompt = Text(text[:160], style=ui.TEXT_PRIMARY)
            result.append((prompt, record))
        return result

    def _choose(self) -> None:
        if not self._payloads:
            self.dismiss(None)
            return
        try:
            index = self.query_one("#sls-list", OptionList).highlighted
            record = self._payloads[0 if index is None else min(index, len(self._payloads) - 1)]
        except Exception:
            record = self._payloads[0]
        self.dismiss(record)


def _fv_attribution_text(record: Mapping[str, Any]) -> str:
    """Render the five file-change attributions as one worded fragment.

    The prompt's contract is that a file change identifies who changed it,
    why, whether it is verified, whether it can be undone, and which
    checkpoint contains it. The diff browser used to print the ACTOR and
    nothing else, so a reader of `/diff` on a large change saw a path and a
    name and had to guess the other four. This is the one producer, so the
    browser, the file screen, and the REPL cannot disagree.

    `verified` is read from the RECORD's fail-closed `verified` flag, never
    re-derived from a status word here: a renderer that recomputes the
    verdict is a renderer that can disagree with the projection.
    """
    checkpoints = record.get("checkpoint_ids") or []
    where = str(checkpoints[0]) if checkpoints else "no checkpoint"
    if len(checkpoints) > 1:
        where = f"{checkpoints[0]} +{len(checkpoints) - 1}"
    return (
        f"by {record.get('actor', 'unknown')}"
        f" · {record.get('reason', 'reason not recorded')}"
        f" · {record.get('verification_state', 'not_run')}"
        f" · undo {'yes' if record.get('undoable') else 'no'}"
        f" · {where}"
    )


class _RelevantFilesScreen(_SearchListScreen):
    """Ranked, reasoned relevant-files browser used by `/relevant`.

    The rows carry a REASON, not just a path, because a list of files with
    no stated basis is a worse `/files` — a reader cannot act on "this file
    matters" and has no way to check it. Each row resolves its
    `cli.fileview.ATTRIBUTION_FIELDS` and the roles that put it here.
    """

    def __init__(self, rows: List[Dict[str, Any]], query: str = "") -> None:
        super().__init__(
            f"relevant files — {len(rows)} ranked",
            "filter path or reason · enter attach",
        )
        self._rows = [dict(row) for row in rows if isinstance(row, Mapping)]
        self._initial = query

    def on_mount(self) -> None:
        super().on_mount()
        if self._initial:
            self.query_one("#sls-input", Input).value = self._initial
            self._refilter(self._initial)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        for row in self._rows:
            text = f"{row.get('path', '')} · {row.get('reason', '')}"
            if needle and needle not in text.lower():
                continue
            prompt = Text()
            prompt.append(f"{int(row.get('rank') or 0):>2} ", style=ui.TEXT_SECONDARY)
            prompt.append(str(row.get("path") or "?"), style=ui.TEXT_PRIMARY)
            reason = str(row.get("reason") or "")
            if reason:
                prompt.append(f"  {reason}", style=ui.TEXT_SECONDARY)
            result.append((prompt, row))
        return result


class _DiagnosticsScreen(_SearchListScreen):
    """Searchable journal/lsp diagnostics panel used by `/diagnostics`.

    Rows are rendered through `cli.a11y.diagnostic_row`, the ONE producer of
    the shape, so the TUI, the REPL, and any future surface cannot encode a
    diagnostic two ways. The `!! error [lsp] path:line:col` silhouette is
    deliberately not a sentence: model output arrives in the transcript as
    prose, and a tool's findings must not read like something the model said.
    """

    def __init__(self, values: List[Dict[str, Any]], lsp_state: Optional[Mapping[str, Any]] = None) -> None:
        state = dict(lsp_state or {})
        super().__init__(
            f"diagnostics — {len(values)} issue(s)",
            "filter path · severity · provenance · message",
        )
        self._values = list(values)
        self._lsp_state = state

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        for item in sorted(
            self._values,
            key=lambda row: _a11y.diagnostic_severity_rank(row.get("severity")),
        ):
            text = _a11y.diagnostic_row(item)
            if needle and needle not in text.lower():
                continue
            style = ui.ERROR if str(item.get("severity") or "").lower() in {"error", "fatal"} else ui.WARNING
            result.append((Text(text[:200], style=style), item))
        return result



class _DiffBrowserScreen(_SearchListScreen):
    """Summary-first browser for large staged and unstaged diffs."""

    def __init__(self, files: List[Mapping[str, Any]]) -> None:
        super().__init__(
            f"diff browser — {len(files)} file(s)",
            "filter path · stage · actor · enter open",
        )
        self._files = [dict(value) for value in files if isinstance(value, Mapping)]

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        for record in self._files:
            stage = "S" if record.get("staged") else "U"
            attribution = _fv_attribution_text(record)
            text = f"[{stage}] {record.get('path', '?')} · {record.get('summary') or record.get('status', '')} · {attribution}"
            if needle and needle not in text.lower():
                continue
            prompt = Text(text[:220], style=ui.TEXT_PRIMARY)
            result.append((prompt, record))
        return result

    def _choose(self) -> None:
        if not self._payloads:
            self.dismiss(None)
            return
        try:
            index = self.query_one("#sls-list", OptionList).highlighted
            record = self._payloads[0 if index is None else min(index, len(self._payloads) - 1)]
        except Exception:
            record = self._payloads[0]
        self.app.push_screen(_DiffFileScreen(record))


class _DiffFileScreen(ModalFrame[None]):
    """Read-only file diff detail with hunk AND line-level navigation.

    Hunk granularity was the gap. `DiffHunk.line_numbers` already carried
    real per-line numbers and this screen already rendered them, but the only
    way to ADDRESS one was `#<hunk>` — so a 400-line hunk gave a reader no
    way to reach its 39th line from a diagnostic, a trace entry, or a
    teammate's message. Three additions close that:

    * a `line` target (from `/diff <file>:<line>`), scrolled to and marked;
    * `n` / `p` stepping through the ADDED and REMOVED lines only, which is
      what a reviewer actually wants to walk, rather than through context
      padding;
    * the address of the current line, so the reader can hand it to someone
      else or paste it into `/diff` without counting.

    A line that is not in the diff says so (`outside_diff`) rather than
    silently landing on a neighbour.
    """

    CSS = """
    _DiffFileScreen {
        align: center middle;
    }
    #diff-file-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 82%;
        padding: 1 2;
        background: $surface;
        border: round $vex-accent;
    }
    #diff-file-title {
        color: $vex-accent;
        text-style: bold;
    }
    #diff-file-body {
        height: auto;
        max-height: 28;
    }
    #diff-file-cursor {
        color: $vex-accent;
        text-style: bold;
    }
    #diff-file-hint {
        color: $vex-secondary;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        record: Mapping[str, Any],
        hunk: Optional[int] = None,
        line: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._record = dict(record)
        self._hunk = hunk
        self._line = line
        self._cursor = 0
        self._steps: List[Dict[str, Any]] = _fv.changed_line_index(self._record)

    def compose(self) -> ComposeResult:
        path = str(self._record.get("path") or "diff")
        title = f"{path} · {self._record.get('status') or 'changed'}"
        if self._record.get("large") or self._record.get("truncated"):
            title += " · bounded view"
        with Vertical(id="diff-file-box"):
            yield Static(escape(title), id="diff-file-title")
            yield RichLog(id="diff-file-body", markup=False, wrap=False)
            yield Static("", id="diff-file-cursor")
            yield Static(
                "scroll · esc close · n/p next/prev change · "
                "/diff <file>:<line> jumps to a line",
                id="diff-file-hint",
            )

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#diff-file-body", RichLog)
        record = self._record
        summary = Text(_fv_attribution_text(record))
        body.write(summary)
        selected = record.get("selected_hunk")
        for hunk in record.get("hunks") or []:
            if not isinstance(hunk, Mapping):
                continue
            if self._hunk is not None and int(hunk.get("index", 0)) != self._hunk:
                continue
            body.write(Text(ui.sanitize_text(str(hunk.get("header") or "")), style=ui.TEXT_SECONDARY))
            for number, line in zip(
                hunk.get("line_numbers") or [],
                hunk.get("lines") or [],
                strict=False,
            ):
                prefix = f"{int(number):>4} " if number else "     "
                # Defence in depth. `cli.fileview._parse_hunks` already
                # sanitises every line at the parse boundary, so this is a
                # second net rather than the only one: a `Text` has no markup
                # parser, so `markup=False` protects rich syntax and nothing
                # else, and a modal that rendered an unredacted diff line is
                # the recorded VEX-TERM-UX-09 credential disclosure.
                line = ui.sanitize_text(str(line))
                text = Text(f"{prefix}{line}")
                if line.startswith("+"):
                    text.stylize(ui.SUCCESS, len(prefix), len(text.plain))
                elif line.startswith("-"):
                    text.stylize(ui.ERROR, len(prefix), len(text.plain))
                body.write(text)
        if record.get("truncated"):
            body.write(Text("… diff truncated; address a narrower hunk or line", style=ui.TEXT_SECONDARY))
        if selected:
            body.write(Text("selected hunk", style=ui.ACCENT_TEXT))
        self._seat_at_target()
        self._paint_cursor()

    def _seat_at_target(self) -> None:
        """Start the step cursor on the addressed line, or the first change."""
        if not self._steps:
            return
        wanted = self._record.get("selected_line")
        seat = 0
        if wanted:
            for index, step in enumerate(self._steps):
                if int(step.get("line") or 0) == int(wanted):
                    seat = index
                    break
        self._cursor = min(seat, len(self._steps) - 1)

    def _paint_cursor(self) -> None:
        try:
            widget = self.query_one("#diff-file-cursor", Static)
        except Exception:
            return
        path = str(self._record.get("path") or "")
        if not self._steps:
            kind = str(self._record.get("selected_line_kind") or "")
            target = self._record.get("selected_line")
            if target:
                widget.update(
                    escape(
                        f"line {target} is {kind.replace('_', ' ')}; "
                        f"no added/removed line to step to"
                    )
                )
            else:
                widget.update(escape("no added or removed line in this file"))
            return
        step = self._steps[self._cursor]
        line = int(step.get("line") or 0)
        widget.update(
            escape(
                f"change {self._cursor + 1} of {len(self._steps)} · "
                f"{step.get('side')} line {line} · /diff {path}:{line}"
            )
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return
        if event.key in ("n", "j") and self._steps:
            event.stop()
            event.prevent_default()
            self._cursor = min(self._cursor + 1, len(self._steps) - 1)
            self._paint_cursor()
            self._scroll_to_change()
            return
        if event.key in ("p", "k") and self._steps:
            event.stop()
            event.prevent_default()
            self._cursor = max(self._cursor - 1, 0)
            self._paint_cursor()
            self._scroll_to_change()
            return
        actions = {
            "up": "scroll_up",
            "k": "scroll_up",
            "down": "scroll_down",
            "j": "scroll_down",
            "pageup": "scroll_page_up",
            "pagedown": "scroll_page_down",
            "home": "scroll_home",
            "end": "scroll_end",
        }
        action = actions.get(event.key)
        if action:
            event.stop()
            event.prevent_default()
            body = self.query_one("#diff-file-body", RichLog)
            method = getattr(body, action, None)
            if callable(method):
                method(animate=False)

    def _scroll_to_change(self) -> None:
        """Scroll the diff body to the line the cursor currently names.

        A cursor that moves on screen but not in the body is a cursor the
        reader has to hunt for, which is worse than no cursor. The body is
        written in a known order (one summary line, then a header per hunk,
        then its lines), so the target row is located by counting and the
        offset is bounds-checked against the widget rather than trusted.
        """
        if not self._steps:
            return
        step = self._steps[self._cursor]
        try:
            body = self.query_one("#diff-file-body", RichLog)
        except Exception:
            return
        row = 1  # row 0 is the attribution summary
        for hunk in self._record.get("hunks") or []:
            if not isinstance(hunk, Mapping):
                continue
            if self._hunk is not None and int(hunk.get("index", 0)) != self._hunk:
                continue
            row += 1  # the hunk header
            lines = list(hunk.get("lines") or [])
            numbers = list(hunk.get("line_numbers") or [])
            for offset in range(len(lines)):
                number = numbers[offset] if offset < len(numbers) else None
                if (
                    int(hunk.get("index", 0)) == int(step.get("hunk") or 0)
                    and offset == int(step.get("offset") or 0)
                    and number is not None
                    and int(number) == int(step.get("line") or 0)
                ):
                    target = max(0, row + offset - 2)
                    if 0 <= target < len(body.lines):
                        body.scroll_to(y=target, animate=False)
                    return
                row += 1


class _ReviewScreen(ModalFrame[None]):
    """The hash-verified review surface `cli.review` renders.

    THIS IS THE MOUNT. `cli/review.py` is 2,900+ lines of correct,
    concurrent-edit-safe review code that nothing in the product called:
    its only production reference for twelve rounds was
    `cli/commands.py::_diff_verbs`, which reads its verb vocabulary and
    nothing else. `/diff` rendered the HISTORICAL diff - a picture of the
    run's output with no verdicts, no evidence, and no way to put anything
    back.

    The screen is deliberately THIN. Every fact on it is a projection of
    `cli.review.build_review`, and the actions are `cli.review.review_command`
    - the same function both shells call, so a TUI reviewer and a REPL
    reviewer cannot disagree about what was decided. The screen owns layout
    and keys; it owns no decision vocabulary of its own.

    **The lines arrive SANITISED.** `cli/review.py::_bound` is the single
    function every rendered review line passes through and it routes through
    `cli.ui.sanitize_text`, so a diff of a file the run wrote cannot put a
    credential or a raw escape byte on this screen. The widget id is
    `cli.review.REVIEW_WIDGET_ID` and it is declared beside the payload
    rather than restated here, so the two cannot drift.
    """

    CSS = """
    _ReviewScreen {
        align: center middle;
    }
    #review-box {
        width: 92%;
        max-width: 130;
        height: auto;
        max-height: 86%;
        padding: 1 2;
        background: $surface;
        border: round $vex-accent;
    }
    #review-title {
        color: $vex-accent;
        text-style: bold;
    }
    #review-body {
        height: auto;
        max-height: 34;
    }
    #review-hint {
        color: $vex-muted;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "dismiss(None)", "close", show=True),
    ]

    def __init__(self, lines: List[str], *, title: str = "review") -> None:
        super().__init__()
        self._lines = list(lines or [])
        self._title = str(title or "review")

    def compose(self) -> ComposeResult:
        with Vertical(id="review-box"):
            yield Static(escape(self._title), id="review-title")
            # markup=False: `cli.review` already sanitised every one of these
            # lines, and a second markup pass over an already-escaped line
            # would double the backslashes. This protects nothing the
            # sanitiser did not, and the sanitiser is the part that matters.
            yield RichLog(id="review-body", markup=False, wrap=False)
            yield Static(
                "accept <file>#<hunk>  ·  reject <file>#<hunk>  ·  "
                "revert <file>  ·  esc close",
                id="review-hint",
            )

    def on_mount(self) -> None:
        super().on_mount()
        try:
            body = self.query_one("#review-body", RichLog)
        except Exception:
            return
        if not self._lines:
            body.write(Text("(this run changed no file it is evidenced to have touched)"))
            return
        for line in self._lines:
            # `ui.sanitize_text` a SECOND time here, on purpose. The review
            # module sanitises at its own boundary; this is the same
            # belt-and-braces net `_DiffFileScreen` uses, and it is free of
            # risk because `sanitize_text` is pinned idempotent. A second
            # net on a diff-rendering path is the cheapest gate in the repo.
            body.write(Text(ui.sanitize_text(str(line))))
        try:
            body.scroll_home(animate=False)
        except Exception:
            pass


class _ContextSourcesScreen(_SearchListScreen):
    """Searchable cited source panel for repository, skills, and memory."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__(
            f"context sources — {len(data.get('sources') or [])} cited",
            "filter source · path · section · enter inspect",
        )
        self._data = dict(data)

    def rows(self, query: str) -> List[Tuple[Any, Any]]:
        needle = str(query or "").strip().lower()
        result: List[Tuple[Any, Any]] = []
        sections = [
            {
                "kind": "section",
                "name": str(section.get("name") or "section"),
                "reason": str(section.get("reason") or ""),
                "text": str(section.get("text") or ""),
            }
            for section in self._data.get("sections") or []
            if isinstance(section, Mapping)
        ]
        sources = list(self._data.get("sources") or [])
        citations = [
            {
                "source": str(value.get("source") or "citation"),
                "path": str(value.get("path") or ""),
                "line": value.get("line"),
                "reason": str(value.get("role") or "cited source"),
            }
            for value in self._data.get("citations") or []
            if isinstance(value, Mapping)
        ]
        values = sections + [item for item in sources if isinstance(item, Mapping)] + citations
        for item in values:
            label = str(item.get("name") or item.get("source") or item.get("path") or "source")
            path = str(item.get("path") or "")
            reason = str(item.get("reason") or item.get("role") or "")
            text = " · ".join(value for value in (label, path, reason) if value)
            if needle and needle not in text.lower():
                continue
            prompt = Text(text[:180], style=ui.TEXT_PRIMARY)
            result.append((prompt, dict(item)))
        return result

    def _choose(self) -> None:
        if not self._payloads:
            self.dismiss(None)
            return
        try:
            index = self.query_one("#sls-list", OptionList).highlighted
            value = self._payloads[0 if index is None else min(index, len(self._payloads) - 1)]
        except Exception:
            value = self._payloads[0]
        body = [
            Text(json.dumps(value, ensure_ascii=False, indent=2, default=str)),
        ]
        self.app.push_screen(_TraceDetailScreen("context source", body))


class _CheckpointActionsScreen(ModalFrame[None]):
    """Explicit files-only, files-plus-conversation checkpoint restore gate."""

    CSS = """
    _CheckpointActionsScreen {
        align: center middle;
    }
    #checkpoint-action-box {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $vex-accent;
    }
    #checkpoint-action-title {
        color: $vex-accent;
        text-style: bold;
    }
    #checkpoint-action-body {
        height: auto;
        max-height: 20;
    }
    #checkpoint-action-input {
        border: round $vex-accent;
    }
    #checkpoint-action-hint {
        color: $vex-secondary;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        checkpoint_id: str,
        body: List[Text],
        on_action: Callable[[str, bool], None],
    ) -> None:
        super().__init__()
        self._checkpoint_id = checkpoint_id
        self._body = body
        self._on_action = on_action

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-action-box"):
            yield Static(escape(f"checkpoint {self._checkpoint_id}"), id="checkpoint-action-title")
            yield RichLog(id="checkpoint-action-body", markup=False, wrap=False)
            yield Input(placeholder="files | conversation | all | cancel", id="checkpoint-action-input")
            yield Static("enter executes · esc cancels · restore is conflict-checked", id="checkpoint-action-hint")

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#checkpoint-action-body", RichLog)
        for line in self._body:
            body.write(line)
        self.query_one("#checkpoint-action-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().lower()
        self.dismiss(None)
        if value in {"conversation", "all", "conversation+files", "files+conversation"}:
            self._on_action(self._checkpoint_id, True)
        elif value == "files":
            self._on_action(self._checkpoint_id, False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)


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
            # R2-17: the honest verdict label + its style. The raw
            # `status` field collapses completed_verified and
            # completed_unverified into `completed`, and the old style
            # test compared against the literal "success" — a word this
            # field never contains — so every row rendered in the same
            # grey and an unverified run was indistinguishable from a
            # verified one. `ui.SUCCESS` is now reachable, and only ever
            # with clean verifier evidence.
            status = _honest_session_label(s)
            style = _verdict_style(status)
            repo = str(s.get("repo") or "")
            issue = str(s.get("issue") or "").replace("\n", " ").strip()
            prompt = Text()
            prompt.append(
                f"{mark} ",
                style=ui.ACCENT_TEXT if s.get("resumable") else ui.TEXT_SECONDARY,
            )

            prompt.append(f"{tid}  ", style=ui.TEXT_PRIMARY)
            prompt.append(f"{status:<12}", style=style)
            prompt.append(
                f"  {when}  {Path(repo).name or repo}", style=ui.TEXT_SECONDARY
            )
            if issue:
                prompt.append(f"  {issue[:48]}", style=ui.TEXT_SECONDARY)
            out.append(
                (
                    prompt,
                    {
                        "task_id": tid,
                        "status": status,
                        "resumable": bool(s.get("resumable")),
                    },
                )
            )
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


class _MeasuredInput(Input):
    """The composer, instrumented for per-keystroke acknowledgement.

    `Input.Changed` is the wrong clock for the 100 ms gate: it is posted
    by the widget and dispatched by the app's message pump, so the delta
    from the key event to the handler includes whatever frame the pump
    happened to be in the middle of. Measured that way, a 100x30 repaint
    on a loaded host reported 350 ms p95 for input that the widget had
    processed in microseconds — a number about the compositor wearing the
    name of a number about typing.

    This measures where the work actually happens: the key event's
    monotonic stamp to the `insert_text_at_cursor` call that puts the
    character in the buffer. That is the acknowledgement a user
    experiences, and it is the app's own cost rather than the framework's
    scheduling. The metrics sink is injected (never a module global), so
    two apps in one process cannot contaminate each other.
    """

    def __init__(self, *args: Any, metrics: Optional["_UIMetrics"] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._metrics = metrics

    def bind(self, metrics: Optional["_UIMetrics"]) -> None:
        """Point this widget at a metrics sink (or `None` to stop measuring)."""
        self._metrics = metrics

    def _on_key(self, event: events.Key) -> Any:
        """Stamp the key, then let the widget do its normal work."""
        if self._metrics is not None and not (event.is_printable or event.key == "space"):
            self._metrics.observe("input_ack_ms", started=time.perf_counter())
        return super()._on_key(event)

    def insert_text_at_cursor(self, text: str) -> None:
        """Insert `text` and record the acknowledgement latency."""
        started = time.perf_counter() if self._metrics is not None else 0.0
        super().insert_text_at_cursor(text)
        if self._metrics is not None:
            self._metrics.observe(
                "input_ack_ms", value=(time.perf_counter() - started) * 1000.0
            )


class _TraceDetailScreen(ModalFrame[None]):
    """One feed entry's / diff's FULL detail — paged, scrollable, esc to
    close. The DEFAULT view stays one line per action; this is the
    on-demand expansion, mirroring Claude Code's collapsed tool calls.

    **This screen used to slice the body to 400 lines and stop** — a
    silent truncation. A user reading a 5,000-line test failure saw 400
    lines, was told nothing, and had no way to reach the other 4,600. It
    is now a real pager over the WHOLE body: `n`/`p` move a page, `g`/`G`
    jump to the first/last, and the label always states the exact range
    and the total, so the omitted remainder is named rather than implied
    away (`cli.a11y.Page` is the single pager contract every long-output
    surface shares).
    """

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
        border: round $vex-accent;
    }
    #trace-title {
        color: $vex-accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #trace-body {
        height: auto;
        max-height: 24;
        margin-bottom: 1;
    }
    #trace-page {
        color: $vex-text;
        margin-bottom: 0;
    }
    #trace-hint {
        color: $vex-secondary; /* text-secondary (design token), not a random grey */
    }
    """

    def __init__(
        self,
        title: str,
        body_lines: List[Text],
        *,
        total: Optional[int] = None,
        fetch: Optional[Callable[[Any], List[Text]]] = None,
        page_size: int = _a11y.DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__()
        self._title = title
        self._body = list(body_lines or [])
        self._total = len(self._body) if total is None else max(0, int(total))
        self._fetch = fetch
        # NOT `_size`: `Widget._size` is textual's own compositor-assigned
        # layout field, and shadowing it with a page size makes the screen
        # report an int for its own region — `AttributeError: 'int' object
        # has no attribute 'region'` the moment a style refresh touches
        # it. That is a production crash, not a test artifact.
        self._page_size = max(1, int(page_size or 1))
        self._page = _a11y.Page(self._total, 0, self._page_size)

    # -- pager state -------------------------------------------------------

    def page(self) -> Any:
        """Return the current `cli.a11y.Page` (public for probes/tests)."""
        return self._page

    def _lines_for(self, page: Any) -> List[Text]:
        """Return one page of body lines, tolerating a failing fetcher."""
        if self._fetch is not None:
            try:
                return list(self._fetch(page))
            except Exception:
                pass
        return list(page.window(self._body))

    def _show(self, page: Any) -> None:
        """Render one page into the body and refresh the label + hint."""
        self._page = page
        try:
            body = self.query_one("#trace-body", RichLog)
            body.clear()
            for line in self._lines_for(page):
                body.write(line)
            self.query_one("#trace-page", Static).update(
                Text(page.label(), style=ui.TEXT_PRIMARY)
            )
            # The KEY SET is static; the page LABEL above carries the
            # state. Listing the keys conditionally produced a hint that
            # repeated itself ("n next page · esc close · n next · …"),
            # which is worse than a fixed, learnable set.
            self.query_one("#trace-hint", Static).update(
                "n next · p previous · g first · G last · esc close"
                if page.pages > 1
                else "esc close"
            )
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-box"):
            yield Static(escape(self._title), id="trace-title")
            yield RichLog(
                id="trace-body",
                markup=False,
                wrap=False,
                # A backstop only: the pager bounds each page to
                # `page_size`, so this cap is never the reason content
                # stops appearing.
                max_lines=400,
                min_width=0,
            )
            yield Static("", id="trace-page")
            yield Static("esc close", id="trace-hint")

    def on_mount(self) -> None:
        super().on_mount()
        self._show(self._page)

    def action_next_page(self) -> None:
        """Advance one page. Never wraps past the end."""
        self._show(self._page.advance())

    def action_previous_page(self) -> None:
        """Go back one page. Never wraps past the start."""
        self._show(self._page.rewind())

    def action_first_page(self) -> None:
        """Jump to the first page."""
        self._show(_a11y.Page(self._total, 0, self._page_size))

    def action_last_page(self) -> None:
        """Jump to the final page."""
        self._show(_a11y.Page(self._total, self._page.last_index, self._page_size))

    #: Key -> action. Declared as a table so the pager's contract is one
    #: readable object rather than a chain of `if key == ...`, and so a
    #: test can assert the full set.
    PAGER_KEYS: ClassVar[Dict[str, str]] = {
        "n": "next_page",
        "j": "next_page",
        "right": "next_page",
        "pagedown": "next_page",
        "space": "next_page",
        "p": "previous_page",
        "k": "previous_page",
        "left": "previous_page",
        "pageup": "previous_page",
        "g": "first_page",
        "home": "first_page",
        "G": "last_page",
        "end": "last_page",
    }

    def on_key(self, event: events.Key) -> None:
        """Page the body, or close. Never raises, never swallows a key."""
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            try:
                self.dismiss(None)
            except Exception:
                pass
            return
        action = self.PAGER_KEYS.get(event.key)
        if not action:
            return
        try:
            getattr(self, f"action_{action}")()
        except Exception:
            return
        event.stop()
        event.prevent_default()


class _PaletteScreen(CommandPaletteFrame):
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
        min-width: 20;
        max-width: 92%;
        height: 20;
        min-height: 8;
        max-height: 80%;
        padding: 1 1;
        background: $surface;
        border: round $vex-accent;
    }
    #palette-input {
        border: round $vex-accent;
        margin-bottom: 1;
    }
    #palette-list {
        height: auto;
        max-height: 16;
        background: $surface;
    }
    #palette-hint {
        color: $vex-secondary; /* text-secondary (design token), not a random grey */
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
        self._files_pending = False

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(
                placeholder="fuzzy search commands · sessions · files",
                id="palette-input",
            )
            yield OptionList(id="palette-list")
            yield Static("tab focus · ↑/↓ select · enter run · esc close", id="palette-hint")

    def on_mount(self) -> None:
        super().on_mount()
        try:
            app_size = getattr(self.app, "size", None)
            width = max(20, min(78, int(getattr(app_size, "width", 0) or 100) - 4))
            height = max(8, min(22, int(getattr(app_size, "height", 0) or 36) * 4 // 5))
            box = self.query_one("#palette-box")
            box.styles.width = width
            box.styles.max_width = width
            box.styles.height = height
        except Exception:
            pass
        self._refilter("")
        self.query_one("#palette-input", Input).focus()

    def add_files(self, files: List[str]) -> None:
        """Merge deferred repo-file entries into a live palette.

        The file scan runs off the UI thread (a `git ls-files` on a large
        repo is far slower than the 250 ms modal budget), so the modal
        opens on commands + sessions and the files arrive a moment
        later. Re-filters with the current query so a half-loaded
        palette still ranks correctly."""
        self._files_pending = False
        if not files or not self.is_attached:
            return
        self._entries.extend(
            {
                "kind": "file",
                "label": rel,
                "hint": "file · insert path",
                "value": rel,
                "run": False,
            }
            for rel in files
        )
        try:
            self._refilter(self.query_one("#palette-input", Input).value)
        except Exception:
            self._refilter("")

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
        tokens = ui.active_tokens()
        kind_styles = {
            "command": tokens["text_primary"],
            "session": tokens["accent_text"],
            "file": tokens["text_secondary"],
        }
        for i, e in enumerate(self._visible):
            style = kind_styles.get(e.get("kind", "command"), tokens["text_primary"])
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
    import sys

    if os.environ.get("VEX_TUI", "").lower() in ("0", "false", "no"):
        return False
    if ui.is_dumb_terminal():
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
    # Safe default artifact location: the harness-owned home keyed by repo,
    # outside the user's repository. A forced in-repo root is gitignored and
    # reported through the session head, exactly as in the rich REPL.
    artifact = _iv._resolve_session_artifact_root(repo, file_config)
    root_scoped = log_root is not None or str(artifact.get("source")) in (
        "flag",
        "config",
    )
    log_root = Path(log_root) if log_root else Path(artifact["log_root"])
    for warning in artifact["warnings"]:
        ui.err_console().print(f"[vex.warn]{escape(str(warning))}[/]")
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
        root_scoped=root_scoped,
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
