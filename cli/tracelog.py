"""Live agent-trace feed — the readable one-line-per-action view of a run.

This module is a PURE mapping layer over data that already exists on disk
(Task E: it tails `logs/{task_id}/trace.jsonl` + `state.json`-adjacent
files; it NEVER writes its own log — the harness trace stays the single
source of truth and can never drift out of sync with what the UI shows).

Two consumers share it:
- the full-screen TUI (cli/tui.py): renders feed lines live while a run
  is in flight (Tasks A/B/C of the live-trace round — reasoning summary,
  tool-call feed, inline diffs);
- tests + future surfaces (dashboards, `neo --continue` recap): the same
  FeedBuilder runs headless over any finished or in-progress trace file.

Design rules, mirroring Claude Code's visible-thinking texture:
- DEFAULT VIEW IS COMPACT: one readable line per action ("Reading
  src/utils.py", "Running: pytest tests/test_x.py") — the raw
  prompt/response/tool output rides along on every entry (detail=)
  so any line can be expanded for full fidelity without a second read
  of the trace file (Task D);
- reasoning summaries are derived from the model_response CONTENT for
  step calls (what the model actually said/did, truncated), not from
  the raw prompt — one line, not a wall;
- diffs are computed live against the SAME pristine/work copies the
  harness itself diffs on completion (logs/{task_id}/pristine vs
  logs/{task_id}/work) — identical output shape, same event that
  fires it, zero duplicate storage.

Every public function is total: malformed events, missing files, and
binary contents degrade to honest labels ("(unreadable …)"), never
raise — a UI layer must not take a run down.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cli.runview import (
    EventCursor,
    effective_terminal_status,
    event_parts,
    status_is_verified,
)
from cli.ui import strip_ansi

# ---------------------------------------------------------------------------
# Entry model — one actionable step in the feed
# ---------------------------------------------------------------------------


class FeedEntry:
    """One feed line + its expandable detail (Task D shape).

    summary is the one-liner shown by default (already display-safe);
    detail_lines is the FULL raw record for expansion (the tool output,
    the model's reply, the diff hunk context) — plain text, no markup:
    the renderer styles it. category routes the glyph/color in the UI:
    "reason" (planning/step commentary), "tool" (bash command), "diff"
    (file edit), "verify" (test/verify outcome), "lifecycle" (attempt/
    task boundaries), "info" (everything else).
    """

    __slots__ = (
        "call_id",
        "category",
        "detail",
        "detail_title",
        "index",
        "run_id",
        "sequence",
        "session_id",
        "summary",
        "turn_id",
    )

    def __init__(
        self,
        summary: str,
        category: str = "info",
        detail: str = "",
        detail_title: str = "",
        index: int = 0,
        call_id: str = "",
        sequence: int = 0,
        session_id: str = "",
        run_id: str = "",
        turn_id: str = "",
    ) -> None:
        self.summary = strip_ansi(summary)
        self.category = category
        self.detail = strip_ansi(detail)
        self.detail_title = strip_ansi(detail_title)
        self.index = index
        self.call_id = str(call_id or "")
        self.sequence = int(sequence or 0)
        self.session_id = str(session_id or "")
        self.run_id = str(run_id or "")
        self.turn_id = str(turn_id or "")

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"FeedEntry({self.category!r}, {self.summary!r})"


# Cap for detail text kept per entry (matches the harness's own
# raw_output[-3000:] convention; the trace file remains the full record).
_MAX_DETAIL = 4000

# Cap for a command shown in a summary line — longer commands (heredoc
# rewrites) are truncated with an explicit marker.
_MAX_CMD_SUMMARY = 160

# How many diff context lines surround changes in the inline preview
# (the harness's own diff is context-less via unified_diff default n=3;
# small is the point of "small, syntax-highlighted").
_DIFF_CONTEXT = 2


def _clip(text: str, cap: int) -> str:
    """Clip display text to cap chars with an honest truncation marker."""
    text = strip_ansi(text or "")
    if len(text) <= cap:
        return text
    return text[:cap] + "…[truncated]"


def _evidence_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed", "ok"}
    return False


# ---------------------------------------------------------------------------
# Bash command classification — "Reading src/utils.py" texture (Task B)
# ---------------------------------------------------------------------------

_READ_LABELS = {
    "cat": "Reading",
    "head": "Reading",
    "tail": "Reading",
    "sed": "Reading",
    "grep": "Searching",
    "rg": "Searching",
    "find": "Finding",
    "ls": "Listing",
    "dir": "Listing",
    "wc": "Counting",
    "file": "Inspecting",
    "stat": "Inspecting",
    "pwd": "Checking",
    "which": "Checking",
    "where": "Checking",
    "env": "Checking",
}


def classify_command(command: str) -> Tuple[str, str]:
    """Classify a bash command into (verb-class, human summary).

    verb-class is one of "read", "search", "test", "edit", "write",
    "inspect" (generic diagnostic), or "run" (anything else — the honest
    default for arbitrary bash). The summary is a display-safe one-liner
    (no markup — the renderer styles it).

    Assumes command is a single bash command line (possibly multi-line,
    e.g. a heredoc rewrite) as recorded in the tool_call trace event.
    Multi-line write commands are summarized by their first line with a
    "N lines" marker — the full text rides in the entry's detail.
    """
    cmd = (command or "").strip()
    if not cmd:
        return "run", "(empty command)"

    lines = [ln for ln in cmd.splitlines() if ln.strip()]
    first = lines[0] if lines else cmd

    # Multi-line heredoc/script shapes: summarize by shape, keep the
    # full body in detail.
    if len(lines) > 1:
        if re.search(r"<<\s*['\"]?\w+", first):
            target = _heredoc_target(first)
            if target:
                return "write", f"Rewriting {target} ({len(lines)} lines)"
        return "run", f"Running script ({len(lines)} lines): {_clip(first, 80)}"

    low = first.lower()
    parts = first.split()
    if not parts:
        return "run", _clip(first, _MAX_CMD_SUMMARY)

    # Agent-loop verbs (harness.agent_loop renders tool calls shell-ish
    # for this exact classifier; the fix loop never emits these).
    if low.startswith("read "):
        return "read", f"Reading {parts[1] if len(parts) > 1 else '(here)'}"
    if low.startswith("glob "):
        return "read", f"Listing {parts[1] if len(parts) > 1 else '(here)'}"
    if low.startswith("grep ") or low.startswith("rg "):
        return "read", _search_summary(first)
    if low.startswith("edit "):
        return "edit", f"Editing {parts[1] if len(parts) > 1 else '(file)'}"
    if low.startswith("write "):
        return "write", f"Writing {parts[1] if len(parts) > 1 else '(file)'}"
    if low.startswith("memory "):
        return "read", "Recalling past decisions"
    if low.startswith("fetch "):
        return "read", f"Fetching {parts[1] if len(parts) > 1 else '(page)'}"
    if low.startswith("mcp "):
        return "run", f"Calling MCP {parts[1] if len(parts) > 1 else '(tool)'}"
    if low == "verify":
        return "test", "Running declared tests"
    if low == "done" or low.startswith("done "):
        return "run", "Wrapping up"

    # pytest / test invocations (checked before the generic python
    # branch — `python -m pytest` must land here, not "Running …").
    if (
        low.startswith("pytest")
        or low.startswith("python -m pytest")
        or (low.startswith("python ") and " -m pytest" in low)
    ):
        targets = _test_targets(first)
        return "test", f"Running: pytest {targets}" if targets else "Running: pytest"

    # pydoc: a read of installed docs.
    if low.startswith("python -m pydoc"):
        return "read", f"Reading docs: {' '.join(parts[4:6])}" if len(
            parts
        ) > 4 else "Reading docs"

    # python -c / python scripts: inspect vs edit by what they touch.
    if parts[0] == "python":
        if " -c " in first:
            target = _python_c_target(first)
            if _python_c_looks_like_edit(first):
                if target:
                    return "edit", f"Editing {target}"
                return "edit", f"Editing via python: {_clip(first, _MAX_CMD_SUMMARY)}"
            if target:
                return "inspect", f"Inspecting {target}"
            return "inspect", f"Inspecting via python: {_clip(first, _MAX_CMD_SUMMARY)}"
        script = _script_target(first)
        if script:
            return "run", f"Running {script}"
        return "run", _clip(first, _MAX_CMD_SUMMARY)

    verb = parts[0]

    # Network fetches (read-only).
    if verb in ("curl", "wget"):
        return "read", f"Fetching {_clip(' '.join(parts[1:3]), 90)}"

    if verb in _READ_LABELS or verb == "git":
        # git status/diff/log/show are reads of repo state; other git
        # subcommands stay honest "run".
        if verb == "git":
            sub = parts[1] if len(parts) > 1 else ""
            if sub in ("status", "diff", "log", "show", "blame", "ls-files"):
                target = _clip(" ".join(parts[2:4]).strip(), 60)
                return "read", f"git {sub}" + (f" {target}" if target else "")
            return "run", _clip(first, _MAX_CMD_SUMMARY)
        if verb in ("grep", "rg"):
            return "read", _search_summary(first)
        if verb == "sed":
            target = _last_path_arg(parts)
            return "read", f"Reading {target}" if target else "Reading via sed"
        target = " ".join(p for p in parts[1:5] if not p.startswith("-")).strip()
        target = _clip(target, 100)
        label = _READ_LABELS[verb]
        return "read", f"{label} {target}" if target else f"{label} (here)"
    if verb == "touch":
        return "write", f"Creating {_clip(' '.join(parts[1:3]), 80)}"
    if verb == "mkdir":
        return "write", f"Creating dir {_clip(' '.join(parts[1:3]), 80)}"
    if verb in ("mv", "cp", "rm", "rmdir", "chmod"):
        return "edit", f"{verb} {_clip(' '.join(parts[1:4]), 100)}"
    if verb in ("echo", "printf") and (">" in first or ">>" in first):
        target = _redirect_target(first)
        return "write", f"Writing {target}" if target else "Writing via echo"
    if verb == "cd":
        return "run", f"cd {_clip(' '.join(parts[1:]), 60)}"

    return "run", _clip(first, _MAX_CMD_SUMMARY)


def _script_target(first: str) -> Optional[str]:
    """The script path a `python <script>` line names, if any."""
    m = re.match(r"^python\s+(?:-[a-zA-Z]+\s+)*([A-Za-z0-9_.\-/]+\.py)\b", first)
    return m.group(1) if m else None


def _heredoc_target(first: str) -> Optional[str]:
    """The file a `cat > file <<EOF` style line writes, if any."""
    m = re.search(r">\s*([A-Za-z0-9_.\-/]+)", first)
    if m:
        return m.group(1)
    m = re.search(r"\|\s*tee\s+([A-Za-z0-9_.\-/]+)", first)
    return m.group(1) if m else None


def _redirect_target(first: str) -> Optional[str]:
    """The file an echo/printf redirect writes, if any."""
    m = re.search(r">>?\s*([A-Za-z0-9_.\-/]+)", first)
    return m.group(1) if m else None


def _test_targets(first: str) -> str:
    """The test paths/node-ids a pytest invocation names (compact)."""
    args = [
        a
        for a in first.split()[1:]
        if not a.startswith("-") and a not in ("pytest", "-m")
    ]
    args = [a for a in args if a not in ("python",)]  # from `python -m pytest`
    args = [a for a in args if "." in a or "/" in a or a.startswith("test")]
    if not args:
        return ""
    return _clip(" ".join(args[:2]), 80) + (" …" if len(args) > 2 else "")


def _search_summary(first: str) -> str:
    """A grep/rg line as "Searching for \\"pat\\" in <file(s)>"."""
    # strip the verb and flags
    rest = first.split(None, 1)[1] if " " in first else ""
    for tok in rest.split():
        if tok.startswith("-"):
            rest = rest.replace(tok, "", 1)
    rest = " ".join(rest.split())
    quoted = re.match(r'^((?:["\']).*?(?:["\']))\s+(.*)$', rest)
    if quoted:
        pattern, files = quoted.group(1), quoted.group(2)
    else:
        bits = rest.split(None, 1)
        pattern, files = (bits[0], bits[1] if len(bits) > 1 else "")
    files = _clip(files.strip(), 90)
    if files:
        return f"Searching for {pattern} in {files}"
    return f"Searching for {pattern}"


def _last_path_arg(parts: List[str]) -> str:
    """The last path-shaped argument of a command (sed/awk's file)."""
    for tok in reversed(parts[1:]):
        if not tok.startswith("-") and ("/" in tok or "." in tok):
            return tok
    return ""


def _python_c_looks_like_edit(first: str) -> bool:
    """Heuristic: does a `python -c` line write files? (write_text /
    open(..., 'w') / replace patterns)."""
    return bool(
        re.search(r"write_text|write_bytes|open\([^)]*['\"][wa]['\"]", first)
        or (".replace(" in first and "write_text" in first)
    )


def _python_c_target(first: str) -> str:
    """The file path a `python -c` line names (Path('x') / open('x'))."""
    m = re.search(r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)", first) or re.search(
        r"open\(\s*['\"]([^'\"]+)['\"]", first
    )
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Reasoning summaries (Task A) — one line from a model response
# ---------------------------------------------------------------------------

_STEP_RE = re.compile(r"^step-(\d+)$")
_PLAN_STEP_RE = re.compile(r'"?description"?\s*:\s*"([^"]{3,})"')


def summarize_reply(step: str, content: str) -> Tuple[str, str]:
    """A readable one-line summary of what the model is doing at `step`.

    step is the trace's step label ("plan", "step-1", "self-critique",
    "agent-tests-1", …); content is the model response text. Returns
    (headline, summary):
    - headline names the phase in user language ("Planning the fix",
      "Step 2: <first sentence of its commentary>");
    - summary is the one-liner (a clipped fragment of the reply's own
      prose or its plan/action shape — the Claude-Code "visible
      thinking" texture).

    Total on any input: falls back to the step label itself.
    """
    text = (content or "").strip()
    m = _STEP_RE.match(step or "")
    if step == "plan" or step == "plan-retry":
        return "Planning the fix", _plan_summary(text)
    if step == "self-critique":
        return "Reviewing the diff", _clip(_first_prose(text), 100) or "reviewing"
    if m:
        n = m.group(1)
        return f"Step {n}", _clip(_first_prose(text), 110)
    if step and step.startswith("agent-tests"):
        return "Writing edge-case tests", _clip(
            _first_prose(text), 100
        ) or "drafting tests"
    if step == "answer" or step == "research" or step == "research-final":
        return "Thinking", _clip(_first_prose(text), 110) or "thinking"
    if step.startswith("agent-"):
        return "Agent turn", _clip(_first_prose(text), 110)
    # Unknown label: still show something honest.
    return step or "model", _clip(_first_prose(text), 110) or "thinking"


def _first_prose(text: str) -> str:
    """The first meaningful prose fragment of a reply.

    Skips code fences/commands (those are the ACTION, summarized by the
    tool_call line that follows) and JSON blobs; keeps a clipped plain
    sentence. A reply that is only a command returns "" (the tool feed
    covers it — no duplicated line).
    """
    for line in text.splitlines():
        ln = line.strip()
        if not ln or ln.startswith(("```", "#", "$", "COMMAND", "CMD:", "RUN:")):
            continue
        if ln.upper() == "SUBMIT" or ln.upper().startswith("SUBMIT"):
            continue
        # Skip pure command lines (they get their own tool_call entry).
        if _looks_like_command(ln):
            continue
        # Strip a leading markdown bullet/bold for a cleaner phrase.
        ln = re.sub(r"^[\-\*\d.\s]+", "", ln).strip()
        ln = ln.strip("*_`")
        if len(ln) >= 3:
            return ln
    return ""


def _looks_like_command(ln: str) -> bool:
    """A cheap command-shape test for prose extraction (not a shell)."""
    first = ln.split()[0] if ln.split() else ""
    if first in _READ_LABELS or first in ("python", "pytest", "touch", "echo"):
        return True
    return bool(re.match(r"^[A-Za-z0-9_./-]+(\.py|\.sh|\.txt)\b", ln))


def _looks_like_agent_tool_call(content: str) -> bool:
    """Whether a model reply is the agent loop's one-tool-call protocol."""
    text = (content or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            obj = None
        if isinstance(obj, dict) and obj.get("tool"):
            return True
    return bool(
        re.match(
            r"^(?:READ|GLOB|GREP|BASH|EDIT|WRITE|MEMORY|FETCH|MCP|VERIFY|DONE)\b",
            text,
            re.I,
        )
    )


def _plan_summary(text: str) -> str:
    """One line describing the plan the planner returned.

    Pulls the first step description out of the plan JSON (the reply is
    the raw planner output); degrades to the reply's first prose line.
    """
    m = _PLAN_STEP_RE.search(text or "")
    if m:
        frag = _clip(m.group(1).rstrip("."), 90)
        return f"sub-steps: {frag} …"
    return _clip(_first_prose(text), 110) or "drafting sub-steps"


# ---------------------------------------------------------------------------
# Live diff (Task C) — computed from pristine/ vs work/, same as the
# harness's own final diff (Task E: no second copy of patch data)
# ---------------------------------------------------------------------------


def live_diff(
    pristine_dir: Path, work_dir: Path, max_lines: int = 30
) -> Optional[List[Tuple[str, str]]]:
    """Unified diff lines of (text, kind) between pristine and work.

    kind is one of "meta" (+++/---), "hunk" (@@), "add" (+), "del" (-),
    "ctx" (context). Reads ONLY the two on-disk trees the harness itself
    diffs at completion — same inputs, same difflib shape, so the inline
    preview is exactly the eventual fix diff, minus later edits. A file
    that is binary/unreadable yields a "binary" marker line instead of a
    crash. Returns None when neither dir exists (nothing to diff yet);
    [] when the trees are identical.

    Assumes both dirs are inside logs/{task_id}/ (pristine snapshot +
    agent working copy). The diff deliberately skips junk dirs exactly
    like editor.changed_files (the harness's own convention), so
    __pycache__/caches never fake an edit.
    """
    pristine, work = Path(pristine_dir), Path(work_dir)
    if not pristine.exists() and not work.exists():
        return None
    if not pristine.exists() or not work.exists():
        # One side missing: the run is mid-setup; nothing reliable yet.
        return None

    def scan(root: Path) -> Dict[str, Path]:
        skip = {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".egg-info",
            ".git",
            ".hypothesis",
            ".cache",
            "_agent_generated",
        }
        out: Dict[str, Path] = {}
        for p in root.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(root).as_posix()
            if any(part in skip for part in rel.split("/")):
                continue
            if p.name.startswith(".coverage"):
                continue
            out[rel] = p
        return out

    p_files, w_files = scan(pristine), scan(work)
    out: List[Tuple[str, str]] = []
    for rel in sorted(set(p_files) | set(w_files)):
        p_path, w_path = p_files.get(rel), w_files.get(rel)
        try:
            p_text = (
                p_path.read_text(encoding="utf-8", errors="strict")
                if p_path is not None
                else ""
            )
            w_text = (
                w_path.read_text(encoding="utf-8", errors="strict")
                if w_path is not None
                else ""
            )
        except (UnicodeDecodeError, OSError):
            out.append((f"binary/unreadable change: {rel}", "binary"))
            continue
        # Binary heuristic (same as the harness's editor.unified_diff):
        # a NUL byte in either side is binary — report, never crash.
        if "\x00" in p_text or "\x00" in w_text:
            out.append((f"binary change: {rel}", "binary"))
            continue
        if p_text == w_text:
            continue
        d = difflib.unified_diff(
            p_text.splitlines(keepends=True),
            w_text.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=_DIFF_CONTEXT,
        )
        for emitted, line in enumerate(d):
            if line.endswith("\n"):
                line = line[:-1]
            if line.startswith(("+++", "---")):
                kind = "meta"
            elif line.startswith("@@"):
                kind = "hunk"
            elif line.startswith("+"):
                kind = "add"
            elif line.startswith("-"):
                kind = "del"
            else:
                kind = "ctx"
            out.append((line, kind))
            if emitted + 1 >= max_lines:
                out.append(("…[more changes truncated]", "trunc"))
                break
    return out


# ---------------------------------------------------------------------------
# The feed builder — fold trace events into feed entries (Tasks A+B+C)
# ---------------------------------------------------------------------------


class FeedBuilder:
    """Fold trace.jsonl events into FeedEntry lines, in arrival order.

    Pure event->entry mapping: holds only what one event needs (the last
    plan's step descriptions, for step labels), writes nothing. Used by
    the TUI's tail thread (each parsed event -> entries) and by tests;
    `entries` is the full feed for a run so far.

    Contract: assumes events are dicts as written by harness.trace
    ({"ts", "kind", "data"}); a malformed one is skipped, never raised.
    The event-kind set is the public schema documented in INTERFACES.md
    (kinds the CLI already consumes via LiveMonitor's label table).
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.entries: List[FeedEntry] = []
        self._step_descs: Dict[str, str] = {}
        self._cursor = EventCursor(task_id)
        self._tool_entries: Dict[str, FeedEntry] = {}
        self._completion_entry: Optional[FeedEntry] = None
        self._completion_status = ""
        self._last_warning_revision = 0
        self._latest_verification: Optional[Dict[str, Any]] = None
        self._unknown_kinds: Dict[str, int] = {}

    # -- core API ----------------------------------------------------------

    def consume(self, event: Dict[str, Any]) -> List[FeedEntry]:
        """Validate, order, and map one journal event without duplication."""
        before_revision = self._cursor.warning_revision
        before = len(self.entries)
        accepted = self._cursor.ingest(event)
        for row in accepted:
            self._consume_unchecked(row)
        if not isinstance(event, dict):
            entry = FeedEntry(
                f"(feed skipped a malformed event: {type(event).__name__})",
                "info",
            )
            entry.index = len(self.entries)
            self.entries.append(entry)
        if not accepted and self._cursor.warning_revision != before_revision:
            warning = (
                self._cursor.warnings[-1]
                if self._cursor.warnings
                else "event stream warning"
            )
            entry = FeedEntry(f"event stream: {warning}", "info", detail=warning)
            entry.index = len(self.entries)
            self.entries.append(entry)
        return list(self.entries[before:])

    def _on_model_delta(self, data: Any) -> List[FeedEntry]:
        """A streaming token window is the LIVE TEXT, not a feed row.

        The feed is the narrative of what the agent DID -- one line per real
        action. A streaming delta arrives per coalesced window, so feeding it
        here produced one transcript line per window and, because no
        `_on_model_delta` handler existed, every one of them rendered as
        "unknown event: model_delta". That is the literal text a user saw
        scrolling past during a run: noise carrying no information.

        The text belongs to the stream surface, which coalesces and paints it
        in one place. So the correct feed behaviour is to contribute NOTHING
        and stay silent. `_RunState.consume` (cli/tui.py) handles the same
        kinds for the live view; this keeps the two from double-rendering.
        """
        return []

    # The two spellings the TUI already treats as stream deltas.
    _on_response_delta = _on_model_delta
    _on_text_delta = _on_model_delta

    def _unknown_entry(self, kind: str, data: Any) -> FeedEntry:
        """One entry per UNKNOWN KIND, not one per unknown event.

        Reporting every unrecognised row is how a single new event kind turned
        into hundreds of identical "unknown event: X" lines -- a token stream
        made the feed unusable. But a genuinely new kind must still be
        visible, or a producer that is not wired to the feed looks identical to
        one that emits nothing. So: report the first occurrence, then count.
        """
        label = kind or "incomplete row"
        self._unknown_kinds[label] = self._unknown_kinds.get(label, 0) + 1
        seen = self._unknown_kinds[label]
        if seen > 1:
            # Already reported above; do not repeat the same line per event.
            return FeedEntry(
                f"({label} still unclassified: {seen} events so far)",
                "info",
            )
        # Wording is pinned by tests/test_cli_tracelog.py: an unrecognised kind
        # must be EXPLICIT, and "unknown event" is that contract's phrase.
        return FeedEntry(
            f"unknown event: {label}",
            "info",
            detail=json.dumps(data, ensure_ascii=False, default=str),
            detail_title="unknown event",
        )

    def _consume_unchecked(self, event: Dict[str, Any]) -> None:
        """Map one already-validated event and append its visual entries."""
        kind, data, _timestamp, identity = event_parts(event)
        aliases = {
            "run_started": "task_start",
            "run_finished": "task_end",
            "model_completed": "model_response",
            "tool_completed": "tool_result",
            "verification": "verify",
            "lsp_diagnostics": "diagnostics",
            "phase_changed": "phase_change",
            "phase_change": "phase_change",
        }
        kind = aliases.get(kind, kind)
        handler = getattr(self, f"_on_{kind}", None)
        try:
            if handler is not None and callable(handler):
                produced = list(handler(data) or [])
            elif kind == "tool_call":
                produced = self._on_tool_call(data)
            elif kind == "model_response":
                produced = self._on_model_response(data)
            else:
                produced = [self._unknown_entry(kind, data)]
            for entry in produced:
                entry.sequence = int(identity.get("sequence") or 0)
                entry.session_id = str(identity.get("session_id") or "")
                entry.run_id = str(identity.get("run_id") or "")
                entry.turn_id = str(identity.get("turn_id") or "")
                entry.index = len(self.entries)
                self.entries.append(entry)
        except Exception as exc:
            entry = FeedEntry(
                f"(feed skipped a malformed event: {type(exc).__name__})",
                "info",
            )
            entry.index = len(self.entries)
            self.entries.append(entry)

    def lines(self) -> List[str]:
        """All summaries so far, oldest first (headless recap use)."""
        return [e.summary for e in self.entries]

    def reset(self) -> None:
        """Reset the view after the backing journal is replaced."""
        self.entries = []
        self._step_descs = {}
        self._cursor = EventCursor(self.task_id)
        self._tool_entries = {}
        self._completion_entry = None
        self._completion_status = ""
        self._last_warning_revision = 0
        self._latest_verification: Optional[Dict[str, Any]] = None

    # -- event handlers ----------------------------------------------------

    def _on_task_start(self, data: Dict[str, Any]) -> List[FeedEntry]:
        issue = str(data.get("issue_text") or data.get("request") or "")
        run_spec = data.get("run_spec")
        if not issue and isinstance(run_spec, dict):
            issue = str(run_spec.get("request") or "")
        frag = _clip(issue.splitlines()[0] if issue else "", 90)
        summary = "task start" + (f" — {frag}" if frag else "")
        return [
            FeedEntry(
                summary,
                "lifecycle",
                detail=_clip(
                    json.dumps(data, ensure_ascii=False, default=str), _MAX_DETAIL
                ),
                detail_title="run_started",
            )
        ]

    def _on_baseline_verify(self, data: Dict[str, Any]) -> List[FeedEntry]:
        ok = _evidence_true(data.get("target_passed_on_pristine"))
        label = (
            "baseline: target already passes (no fix needed)"
            if ok
            else ("verifying baseline — reproducing the bug on a pristine copy")
        )
        return [
            FeedEntry(
                label,
                "verify",
                detail=_clip(str(data.get("raw") or ""), _MAX_DETAIL),
                detail_title="baseline verify output",
            )
        ]

    def _on_retrieval(self, data: Dict[str, Any]) -> List[FeedEntry]:
        files = data.get("files") or []
        strategy = data.get("strategy") or ""
        frag = ", ".join(str(f) for f in files[:3])
        if len(files) > 3:
            frag += f" (+{len(files) - 3} more)"
        summary = (
            "gathering context"
            + (f" — {frag}" if frag else "")
            + (f" [{strategy}]" if strategy else "")
        )
        return [
            FeedEntry(
                summary,
                "reason",
                detail="\n".join(str(f) for f in files),
                detail_title="retrieval — ranked files",
            )
        ]

    def _on_decision_memory(self, data: Dict[str, Any]) -> List[FeedEntry]:
        matched = int(data.get("matched") or 0)
        if matched:
            return [
                FeedEntry(
                    f"recalling {matched} past decision(s) from memory",
                    "reason",
                    detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                    detail_title="decision memory",
                )
            ]
        return []

    def _on_skills(self, data: Dict[str, Any]) -> List[FeedEntry]:
        matched = data.get("matched") or []
        if matched:
            names = ", ".join(str(m) for m in matched[:3])
            return [
                FeedEntry(
                    f"applying skill: {names}",
                    "reason",
                    detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                    detail_title="skills scan",
                )
            ]
        return []

    def _on_plan(self, data: Dict[str, Any]) -> List[FeedEntry]:
        plan = data.get("plan") or []
        self._step_descs = {}
        lines: List[str] = []
        for st in plan:
            sid = str(st.get("id", "?"))
            desc = str(st.get("description") or "")
            self._step_descs[sid] = desc
            lines.append(f"{sid}. {desc}")
        summary = f"planned {len(plan)} sub-step(s)"
        if plan:
            first_desc = str(plan[0].get("description") or "")
            if first_desc:
                summary += f" — first: {_clip(first_desc, 70)}"
        return [
            FeedEntry(
                summary,
                "reason",
                detail="\n".join(lines),
                detail_title="plan",
            )
        ]

    def _on_plan_parse_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [
            FeedEntry(
                "plan wasn't valid JSON — re-asking the planner",
                "reason",
                detail=_clip(str(data.get("raw") or ""), _MAX_DETAIL),
                detail_title="unparseable planner reply",
            )
        ]

    def _on_attempt_start(self, data: Dict[str, Any]) -> List[FeedEntry]:
        n = data.get("attempt", "?")
        if int(n if str(n).isdigit() else 1) <= 1:
            return [FeedEntry("attempt 1 — starting edits", "lifecycle")]
        return [
            FeedEntry(
                f"attempt {n} — retrying with feedback from the failure",
                "lifecycle",
                detail="the previous attempt failed verification; the working "
                "copy was rolled back and the failure output was fed back",
                detail_title=f"attempt {n}",
            )
        ]

    def _on_attempt_resume(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("resuming the interrupted attempt", "lifecycle")]

    def _on_step_end(self, data: Dict[str, Any]) -> List[FeedEntry]:
        ok = _evidence_true(data.get("ok"))
        sid = data.get("step_id", "?")
        desc = str(data.get("description") or "") or self._step_descs.get(str(sid), "")
        frag = _clip(desc, 70)
        note = str(data.get("note") or "")
        if ok:
            summary = f"step {sid} done" + (f" — {frag}" if frag else "")
        else:
            reason = _clip(note.splitlines()[0] if note else "", 80)
            summary = f"step {sid} ended without a clean pass" + (
                f" — {reason}" if reason else ""
            )
        return [
            FeedEntry(
                summary,
                "lifecycle" if ok else "verify",
                detail=_clip(note, _MAX_DETAIL),
                detail_title=f"step {sid} result",
            )
        ]

    def _on_step_skipped_resume(self, data: Dict[str, Any]) -> List[FeedEntry]:
        step = str(data.get("step") or "")
        frag = _clip(step.split(". ", 1)[-1], 70)
        return [
            FeedEntry(
                "skipping a step already completed before the interrupt"
                + (f" — {frag}" if frag else ""),
                "lifecycle",
            )
        ]

    def _on_model_request(self, data: Dict[str, Any]) -> List[FeedEntry]:
        # The request itself is raw prompt — no line (Task D: prompt text
        # is expandable detail on the RESPONSE entry, not a wall).
        return []

    def _on_model_response(self, data: Dict[str, Any]) -> List[FeedEntry]:
        step = str(data.get("step") or data.get("turn_id") or "")
        content = strip_ansi(
            str(data.get("content") or data.get("text") or data.get("message") or "")
        )
        tool_calls = data.get("tool_calls")
        if step.startswith("agent-") and _looks_like_agent_tool_call(content):
            return []
        if isinstance(tool_calls, list) and tool_calls and not content:
            return []
        headline, frag = summarize_reply(step, content)
        if step.startswith("step-") and not frag:
            return []
        detail = _clip(
            content or json.dumps(tool_calls or data, ensure_ascii=False, default=str),
            _MAX_DETAIL,
        )
        return [
            FeedEntry(
                f"{headline} — {frag}".strip(),
                "reason",
                detail=detail,
                detail_title=f"model reply ({step or 'response'})",
            )
        ]

    def _on_tool_call(self, data: Dict[str, Any]) -> List[FeedEntry]:
        command = str(data.get("command") or "")
        if not command:
            arguments = data.get("arguments")
            if not isinstance(arguments, dict):
                arguments = data.get("args")
            if not isinstance(arguments, dict):
                arguments = {}
            tool = str(data.get("tool") or data.get("name") or "").lower()
            if tool:
                target = (
                    arguments.get("path")
                    or arguments.get("pattern")
                    or arguments.get("query")
                    or arguments.get("url")
                    or arguments.get("command")
                    or ""
                )
                command = f"{tool.upper()} {target}".strip()
        cls, summary = classify_command(command)
        category = {
            "read": "tool",
            "search": "tool",
            "test": "verify",
            "edit": "diff",
            "write": "diff",
            "inspect": "tool",
            "run": "tool",
        }.get(cls, "tool")
        batch = bool(data.get("batch"))
        prefix = "batch: " if batch else ""
        call_id = str(data.get("call_id") or data.get("id") or "")
        entry = FeedEntry(
            f"{prefix}{summary}",
            category,
            detail=command,
            detail_title="command",
            call_id=call_id,
        )
        if call_id:
            self._tool_entries[call_id] = entry
        return [entry]

    def _on_tool_result(self, data: Dict[str, Any]) -> List[FeedEntry]:
        output = strip_ansi(str(data.get("output") or ""))
        ok = data.get("ok")
        call_id = str(data.get("call_id") or data.get("id") or "")
        target = self._tool_entries.get(call_id) if call_id else None
        if target is None and not call_id:
            for ent in reversed(self.entries):
                if ent.detail_title == "command":
                    target = ent
                    break
        if target is not None:
            target.detail = strip_ansi(
                f"$ {target.detail}\n{output or 'tool returned no output'}"
            )
            if ok is False:
                target.category = "verify"
                target.summary = f"{target.summary} — failed"
        if not output and ok is not False:
            return []
        if ok is False:
            return [
                FeedEntry(
                    f"tool failed: {_clip(output or 'no error detail', 100)}",
                    "verify",
                    detail=_clip(output, _MAX_DETAIL),
                    detail_title="tool result",
                    call_id=call_id,
                )
            ]
        return []

    def _on_tool_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        kind = str(data.get("kind") or "error")
        detail = str(
            data.get("detail") or data.get("error") or data.get("reason") or ""
        )
        return [
            FeedEntry(
                f"tool error ({kind}): {_clip(detail, 80)}",
                "verify",
                detail=_clip(f"{kind}: {detail}", _MAX_DETAIL),
                detail_title="tool error",
            )
        ]

    def _on_strategy_selected(self, data: Dict[str, Any]) -> List[FeedEntry]:
        strategy = str(data.get("strategy") or data.get("name") or "selected")
        return [FeedEntry(f"strategy: {strategy}", "lifecycle")]

    def _on_turn_started(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry(f"turn {data.get('turn', '?')} started", "lifecycle")]

    def _on_model_recovery(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("model retrying after an invalid response", "reason")]

    def _on_tool_recovery(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or "tool call needs recovery")
        return [FeedEntry(f"tool recovery: {_clip(reason, 100)}", "reason")]

    def _on_phase_change(self, data: Dict[str, Any]) -> List[FeedEntry]:
        phase = str(
            data.get("phase") or data.get("state") or data.get("to_state") or "changed"
        )
        return [FeedEntry(f"phase: {phase}", "lifecycle", detail_title="phase change")]

    def _on_reasoning_summary(self, data: Dict[str, Any]) -> List[FeedEntry]:
        summary = str(
            data.get("summary") or data.get("text") or data.get("content") or ""
        )
        return [FeedEntry(_clip(summary, 120) or "reasoning summary", "reason")]

    def _on_file_read(self, data: Dict[str, Any]) -> List[FeedEntry]:
        path = str(data.get("path") or data.get("file") or "a file")
        return [FeedEntry(f"Reading {path}", "tool", detail_title="file read")]

    def _on_file_search(self, data: Dict[str, Any]) -> List[FeedEntry]:
        query = str(data.get("query") or data.get("pattern") or "repository")
        return [FeedEntry(f"Searching {query}", "tool", detail_title="file search")]

    def _on_command_output(self, data: Dict[str, Any]) -> List[FeedEntry]:
        ok = data.get("ok")
        output = _clip(str(data.get("output") or data.get("text") or ""), 120)
        label = (
            "command output"
            if ok is not False
            else f"command failed: {output or 'no detail'}"
        )
        return [
            FeedEntry(
                label,
                "tool" if ok is not False else "verify",
                detail_title="process output",
            )
        ]

    def _on_process_output(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return self._on_command_output(data)

    def _on_subagent_started(self, data: Dict[str, Any]) -> List[FeedEntry]:
        name = str(
            data.get("name") or data.get("role") or data.get("child_id") or "subagent"
        )
        return [FeedEntry(f"subagent started: {name}", "lifecycle")]

    def _on_subagent_finished(self, data: Dict[str, Any]) -> List[FeedEntry]:
        name = str(
            data.get("name") or data.get("role") or data.get("child_id") or "subagent"
        )
        label = (
            "finished"
            if not data.get("error")
            else f"failed: {_clip(str(data.get('error')), 80)}"
        )
        return [
            FeedEntry(
                f"subagent {name} {label}",
                "verify" if data.get("error") else "lifecycle",
            )
        ]

    def _on_retry(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or data.get("attempt") or "retry")
        return [FeedEntry(f"retrying: {_clip(reason, 100)}", "lifecycle")]

    def _on_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        detail = str(
            data.get("error")
            or data.get("reason")
            or data.get("detail")
            or "unknown error"
        )
        return [FeedEntry(f"error: {_clip(detail, 120)}", "verify")]

    def _on_context_warning(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [
            FeedEntry(
                f"context warning: {_clip(str(data.get('warning') or ''), 100)}",
                "lifecycle",
            )
        ]

    def _on_checkpoint_warning(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [
            FeedEntry(
                f"checkpoint warning: {_clip(str(data.get('reason') or ''), 100)}",
                "verify",
            )
        ]

    def _on_approval_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("approval prompt failed safely", "verify")]

    def _on_run_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [
            FeedEntry(
                f"run error: {_clip(str(data.get('error') or ''), 120)}", "verify"
            )
        ]

    def _on_context_built(self, data: Dict[str, Any]) -> List[FeedEntry]:
        size = data.get("estimated_tokens", data.get("tokens", "?"))
        return [FeedEntry(f"context assembled ({size} tokens)", "reason")]

    def _on_context(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return self._on_context_built(data)

    def _on_session_context(self, data: Dict[str, Any]) -> List[FeedEntry]:
        delivered = data.get("delivered")
        chars = data.get("chars", "?")
        label = (
            "session context delivered"
            if delivered is not False
            else "session context empty"
        )
        return [FeedEntry(f"{label} ({chars} chars)", "reason")]

    def _on_permission_decision(self, data: Dict[str, Any]) -> List[FeedEntry]:
        action = str(data.get("action") or data.get("decision") or "ask")
        scope = str(data.get("scope") or "once")
        return [FeedEntry(f"permission {action} ({scope})", "lifecycle")]

    def _on_approval_denied(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or data.get("error") or "approval denied")
        return [FeedEntry(f"approval denied: {_clip(reason, 90)}", "verify")]

    def _on_input_requested(self, data: Dict[str, Any]) -> List[FeedEntry]:
        question = str(data.get("question") or data.get("prompt") or "input requested")
        return [FeedEntry(f"input requested: {_clip(question, 90)}", "lifecycle")]

    def _on_checkpoint_saved(self, data: Dict[str, Any]) -> List[FeedEntry]:
        checkpoint = data.get("checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = data
        sequence = checkpoint.get("last_event_sequence", "?")
        changes = checkpoint.get("agent_owned_changes") or []
        return [
            FeedEntry(
                f"checkpoint saved at event {sequence} ({len(changes)} file(s))",
                "lifecycle",
                detail=_clip(
                    json.dumps(checkpoint, ensure_ascii=False, default=str), _MAX_DETAIL
                ),
                detail_title="checkpoint",
            )
        ]

    def _on_diagnostics(self, data: Dict[str, Any]) -> List[FeedEntry]:
        items = data.get("items") or data.get("diagnostics") or []
        count = len(items) if isinstance(items, list) else 1
        return [FeedEntry(f"diagnostics: {count} issue(s)", "verify")]

    def _on_cancellation_requested(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("cancellation requested", "lifecycle")]

    def _on_verify(self, data: Dict[str, Any]) -> List[FeedEntry]:
        target = _evidence_true(
            data.get("target_passed", data.get("target_test_passed"))
        )
        regression = _evidence_true(data.get("regression_passed"))
        flaky = _evidence_true(data.get("flaky"))
        self._latest_verification = {
            "target_passed": target,
            "regression_passed": regression,
            "flaky": flaky,
        }
        sid = data.get("step_id", "?")
        if target and regression and not flaky:
            label = f"checkpoint passed (step {sid}) — target + suite green"
            category = "verify"
        elif target and not regression:
            label = f"checkpoint (step {sid}): target passed but the suite regressed"
            category = "verify"
        elif flaky:
            label = f"checkpoint (step {sid}): the target test is FLAKY"
            category = "verify"
        else:
            label = f"checkpoint (step {sid}): target still failing"
            category = "verify"
        return [
            FeedEntry(
                label,
                category,
                detail=_clip(str(data.get("raw") or ""), _MAX_DETAIL),
                detail_title="verify output",
            )
        ]

    def _on_final_verify(self, data: Dict[str, Any]) -> List[FeedEntry]:
        target = _evidence_true(data.get("target_passed"))
        regression = _evidence_true(data.get("regression_passed"))
        flaky = _evidence_true(data.get("flaky"))
        self._latest_verification = {
            "target_passed": target,
            "regression_passed": regression,
            "flaky": flaky,
        }
        if target and regression and not flaky:
            label = "final verification — target + full suite PASS"
        elif target and not regression:
            label = "final verification — target passed, suite regressed"
        else:
            label = "final verification — target still failing"
        return [
            FeedEntry(
                label,
                "verify",
                detail=_clip(str(data.get("raw") or ""), _MAX_DETAIL),
                detail_title="final verify output",
            )
        ]

    def _on_recall(self, data: Dict[str, Any]) -> List[FeedEntry]:
        q = str(data.get("query") or "")
        matched = int(data.get("matched") or 0)
        return [
            FeedEntry(
                f"recalling compacted context ({matched} match"
                f"{'es' if matched != 1 else ''}) — {q}",
                "reason",
                detail=f"query: {q}",
                detail_title="RECALL",
            )
        ]

    def _on_batch_call(self, data: Dict[str, Any]) -> List[FeedEntry]:
        cmds = data.get("commands") or []
        return [
            FeedEntry(
                f"batching {len(cmds)} read-only commands",
                "tool",
                detail="\n".join(str(c) for c in cmds),
                detail_title="batch",
            )
        ]

    def _on_batch_rejected(self, data: Dict[str, Any]) -> List[FeedEntry]:
        bad = str(data.get("entry") or "")
        return [
            FeedEntry(
                f"batch rejected (entry not read-only): {_clip(bad, 70)}",
                "verify",
                detail=bad,
                detail_title="rejected batch entry",
            )
        ]

    def _on_docs_lookup(self, data: Dict[str, Any]) -> List[FeedEntry]:
        q = str(data.get("query") or "")
        ok = _evidence_true(data.get("ok"))
        source = str(data.get("source") or "")
        label = f"looking up docs: {q}"
        if not ok:
            label = f"docs lookup missed: {q}"
        return [
            FeedEntry(
                label,
                "reason",
                detail=f"query: {q}\nsource: {source}\nok: {ok}",
                detail_title="DOCS lookup",
            )
        ]

    def _on_web_fetch(self, data: Dict[str, Any]) -> List[FeedEntry]:
        url = str(data.get("url") or "")
        ok = _evidence_true(data.get("ok"))
        chars = data.get("chars", 0)
        label = f"fetched {url}" if ok else f"fetch failed: {url}"
        return [
            FeedEntry(
                label + f" ({chars} chars)" if ok and chars else label,
                "reason",
                detail=f"url: {url}\nok: {ok}\nchars: {chars}",
                detail_title="FETCH",
            )
        ]

    def _on_coordination(self, data: Dict[str, Any]) -> List[FeedEntry]:
        if data.get("detected"):
            files = data.get("dependent_files") or []
            return [
                FeedEntry(
                    "coordinated multi-file change detected — call sites "
                    f"must change together ({len(files)} dependent file(s))",
                    "reason",
                    detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                    detail_title="coordination detection",
                )
            ]
        return []

    def _on_coordination_gate_rejected(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [
            FeedEntry(
                "coordinated change incomplete — some group members "
                "didn't change; rolling back the group",
                "verify",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="coordination gate",
            )
        ]

    def _on_coordination_rollback(self, data: Dict[str, Any]) -> List[FeedEntry]:
        restored = data.get("restored")
        n = len(restored) if isinstance(restored, list) else 1
        return [FeedEntry(f"rolled back {n} file(s) as one unit", "lifecycle")]

    def _on_lint_failed(self, data: Dict[str, Any]) -> List[FeedEntry]:
        findings = data.get("findings") or []
        first = findings[0] if findings else {}
        msg = str(first.get("message") or "")
        return [
            FeedEntry(
                f"static check failed: {_clip(msg, 80) or 'see detail'}",
                "verify",
                detail=_clip(json.dumps(findings, ensure_ascii=False), _MAX_DETAIL),
                detail_title="lint findings",
            )
        ]

    def _on_final_edit_validation_failed(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or "")
        return [
            FeedEntry(
                f"edit policy violation: {_clip(reason, 90)}",
                "verify",
                detail=reason,
                detail_title="edit validation",
            )
        ]

    def _on_agent_tests_skip(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or "")
        return [
            FeedEntry(
                f"edge-case test gate skipped — {reason}",
                "lifecycle",
                detail=reason,
                detail_title="agent-tests gate",
            )
        ]

    def _on_self_critique_reject(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or "")
        return [
            FeedEntry(
                "self-critique rejected the diff as not addressing the "
                f"issue: {_clip(reason, 80)}",
                "verify",
                detail=_clip(reason, _MAX_DETAIL),
                detail_title="self-critique",
            )
        ]

    def _on_self_critique_failed(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("self-critique call failed — approving as-is", "lifecycle")]

    def _on_git_output(self, data: Dict[str, Any]) -> List[FeedEntry]:
        branch = str(data.get("branch") or "")
        return [
            FeedEntry(
                "committed the fix"
                + (f" on {branch}" if branch else "")
                + " (git-native output)",
                "lifecycle",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="git output",
            )
        ]

    def _on_git_output_failed(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("git output failed (task result unaffected)", "lifecycle")]

    def _on_edit_applied(self, data: Dict[str, Any]) -> List[FeedEntry]:
        path = strip_ansi(str(data.get("path") or ""))
        if path:
            for ent in reversed(self.entries):
                if ent.category == "diff" and (
                    f" {path}" in ent.summary or f" {path} " in ent.summary
                ):
                    ent.detail = _clip(
                        f"{ent.detail}\n{json.dumps(data, ensure_ascii=False)}",
                        _MAX_DETAIL,
                    )
                    return []
        return [
            FeedEntry(
                f"edited {path}" if path else "edited a file",
                "diff",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="edit",
            )
        ]

    def _on_approval_required(self, data: Dict[str, Any]) -> List[FeedEntry]:
        tool = str(data.get("tool") or "")
        return [FeedEntry(f"approval needed for {tool}", "lifecycle")]

    def _on_approval_decided(self, data: Dict[str, Any]) -> List[FeedEntry]:
        tool = str(data.get("tool") or "")
        ok = _evidence_true(data.get("approved"))
        return [FeedEntry(f"{'approved' if ok else 'rejected'}: {tool}", "lifecycle")]

    def _on_rationale(self, data: Dict[str, Any]) -> List[FeedEntry]:
        para = str(data.get("paragraph") or "")
        return [
            FeedEntry(
                "writing the rationale",
                "lifecycle",
                detail=_clip(para, _MAX_DETAIL),
                detail_title="rationale",
            )
        ]

    def _on_rationale_failed(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("rationale write failed (result unaffected)", "lifecycle")]

    def _on_attempt_end(self, data: Dict[str, Any]) -> List[FeedEntry]:
        return [FeedEntry("attempt ended", "lifecycle")]

    def _on_stop(self, data: Dict[str, Any]) -> List[FeedEntry]:
        reason = str(data.get("reason") or "stopping")
        return [FeedEntry(f"stopped — {reason}", "lifecycle")]

    def _emit_completion(
        self,
        summary: str,
        status: str,
        data: Dict[str, Any],
        title: str,
    ) -> List[FeedEntry]:
        if self._completion_entry is not None:
            detail = _clip(
                json.dumps(data, ensure_ascii=False, default=str), _MAX_DETAIL
            )
            if detail and detail not in self._completion_entry.detail:
                self._completion_entry.detail = _clip(
                    f"{self._completion_entry.detail}\n{detail}", _MAX_DETAIL
                )
            if status_is_verified(status) and not status_is_verified(
                self._completion_status
            ):
                self._completion_entry.summary = summary
                self._completion_entry.category = "verify"
                self._completion_entry.detail_title = title
                self._completion_status = status
            return []
        entry = FeedEntry(
            summary,
            "verify" if status_is_verified(status) else "lifecycle",
            detail=_clip(
                json.dumps(data, ensure_ascii=False, default=str), _MAX_DETAIL
            ),
            detail_title=title,
        )
        self._completion_entry = entry
        self._completion_status = status
        return [entry]

    def _on_task_end(self, data: Dict[str, Any]) -> List[FeedEntry]:
        nested = data.get("result")
        if isinstance(nested, dict):
            data = {**nested, **{k: v for k, v in data.items() if k != "result"}}
        evidence = data.get("verification_evidence") or data.get("verification")
        if isinstance(evidence, dict):
            evidence = [evidence]
        if not evidence and self._latest_verification is not None:
            evidence = [self._latest_verification]
        status = effective_terminal_status(
            str(data.get("status") or "unknown"), evidence
        )
        return self._emit_completion(
            f"task {status}",
            status,
            data,
            "task_end",
        )

    def _on_result(self, data: Dict[str, Any]) -> List[FeedEntry]:
        nested = data.get("result")
        if isinstance(nested, dict):
            data = {**nested, **{k: v for k, v in data.items() if k != "result"}}
        evidence = data.get("verification_evidence") or data.get("verification")
        if isinstance(evidence, dict):
            evidence = [evidence]
        if not evidence and self._latest_verification is not None:
            evidence = [self._latest_verification]
        status = effective_terminal_status(
            str(data.get("status") or "unknown"), evidence
        )
        attempts = data.get("attempts", data.get("attempt", "?"))
        try:
            cost = float(data.get("cost_usd", data.get("cost", 0.0)) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return self._emit_completion(
            f"result: {status} (attempt {attempts}, ${cost:.4f})",
            status,
            data,
            "result",
        )
