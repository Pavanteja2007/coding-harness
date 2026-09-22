"""Live agent-trace feed — the readable one-line-per-action view of a run.

This module is a PURE mapping layer over data that already exists on disk
(Task E: it tails `logs/{task_id}/trace.jsonl` + `state.json`-adjacent
files; it NEVER writes its own log — the harness trace stays the single
source of truth and can never drift out of sync with what the UI shows).

Two consumers share it:
- the full-screen TUI (cli/tui.py): renders feed lines live while a run
  is in flight (Tasks A/B/C of the live-trace round — reasoning summary,
  tool-call feed, inline diffs);
- tests + future surfaces (dashboards, `vex --continue` recap): the same
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

    __slots__ = ("category", "detail", "detail_title", "index", "summary")

    def __init__(
        self,
        summary: str,
        category: str = "info",
        detail: str = "",
        detail_title: str = "",
        index: int = 0,
    ) -> None:
        self.summary = summary
        self.category = category
        self.detail = detail
        self.detail_title = detail_title
        self.index = index

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
    """Clip a string to cap chars with an honest truncation marker."""
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap] + "…[truncated]"


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
        self._step_descs: Dict[str, str] = {}  # "1" -> "fix the divisor"

    # -- core API ----------------------------------------------------------

    def consume(self, event: Dict[str, Any]) -> List[FeedEntry]:
        """Map one trace event to 0..n feed entries (never raises).

        Returns the entries it produced (the caller renders them live;
        they are also appended to self.entries for later inspection).
        """
        try:
            kind = str(event.get("kind") or "")
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}
            produced: List[FeedEntry] = []
            handler = getattr(self, f"_on_{kind}", None)
            if handler is not None and callable(handler):
                produced = list(handler(data) or [])
            elif kind == "tool_call":
                produced = self._on_tool_call(data)
            elif kind == "model_response":
                produced = self._on_model_response(data)
            for e in produced:
                e.index = len(self.entries)
                self.entries.append(e)
            return produced
        except Exception as exc:  # never kill a run over a feed line
            ent = FeedEntry(
                f"(feed skipped a malformed event: {type(exc).__name__})",
                "info",
            )
            self.entries.append(ent)
            return [ent]

    def lines(self) -> List[str]:
        """All summaries so far, oldest first (headless recap use)."""
        return [e.summary for e in self.entries]

    # -- event handlers ----------------------------------------------------

    def _on_task_start(self, data: Dict[str, Any]) -> List[FeedEntry]:
        issue = str(data.get("issue_text") or "")
        frag = _clip(issue.splitlines()[0] if issue else "", 90)
        summary = "task start" + (f" — {frag}" if frag else "")
        return [
            FeedEntry(
                summary,
                "lifecycle",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="task_start",
            )
        ]

    def _on_baseline_verify(self, data: Dict[str, Any]) -> List[FeedEntry]:
        ok = bool(data.get("target_passed_on_pristine"))
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
        ok = bool(data.get("ok"))
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
        step = str(data.get("step") or "")
        content = str(data.get("content") or "")
        headline, frag = summarize_reply(step, content)
        # A pure-command step reply is followed immediately by its
        # tool_call line (the ACTION) — no duplicate "thinking" line;
        # the in-flight moment is already covered by the live run-line's
        # "model: thinking (step N)" phase label. Only PROSE between
        # actions becomes a reasoning line (the visible-thinking
        # texture). Planner/critique/test-writing responses always get
        # their phase line.
        if step.startswith("step-") and not frag:
            return []
        detail = _clip(content, _MAX_DETAIL)
        return [
            FeedEntry(
                f"{headline} — {frag}".strip(),
                "reason",
                detail=detail,
                detail_title=f"model reply ({step})",
            )
        ]

    def _on_tool_call(self, data: Dict[str, Any]) -> List[FeedEntry]:
        command = str(data.get("command") or "")
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
        return [
            FeedEntry(
                f"{prefix}{summary}",
                category,
                detail=command,
                detail_title="command",
            )
        ]

    def _on_tool_result(self, data: Dict[str, Any]) -> List[FeedEntry]:
        # The result doesn't get its own line (Task D: output is
        # expandable detail, not a wall) — it attaches to the matching
        # command entry so expanding that line shows command + output,
        # exactly like Claude Code's collapsed tool calls.
        output = str(data.get("output") or "")
        if not output:
            return []
        for ent in reversed(self.entries):
            if ent.detail_title == "command":
                ent.detail = f"$ {ent.detail}\n{output}"
                break
        return []

    def _on_tool_error(self, data: Dict[str, Any]) -> List[FeedEntry]:
        kind = str(data.get("kind") or "error")
        detail = str(data.get("detail") or "")
        return [
            FeedEntry(
                f"tool error ({kind}): {_clip(detail, 80)}",
                "verify",
                detail=_clip(f"{kind}: {detail}", _MAX_DETAIL),
                detail_title="tool error",
            )
        ]

    def _on_verify(self, data: Dict[str, Any]) -> List[FeedEntry]:
        target = bool(data.get("target_passed"))
        regression = bool(data.get("regression_passed"))
        flaky = bool(data.get("flaky"))
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
        target = bool(data.get("target_passed"))
        regression = bool(data.get("regression_passed"))
        flaky = bool(data.get("flaky"))
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
        ok = bool(data.get("ok"))
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
        ok = bool(data.get("ok"))
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
        path = str(data.get("path") or "")
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
        ok = bool(data.get("approved"))
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

    def _on_task_end(self, data: Dict[str, Any]) -> List[FeedEntry]:
        status = str(data.get("status") or "?")
        return [
            FeedEntry(
                f"task {status}",
                "verify" if status == "success" else "lifecycle",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="task_end",
            )
        ]

    def _on_result(self, data: Dict[str, Any]) -> List[FeedEntry]:
        status = str(data.get("status") or "?")
        attempts = data.get("attempts", "?")
        cost = data.get("cost_usd", 0)
        return [
            FeedEntry(
                f"result: {status} (attempt {attempts}, ${float(cost or 0):.4f})",
                "verify" if status == "success" else "lifecycle",
                detail=_clip(json.dumps(data, ensure_ascii=False), _MAX_DETAIL),
                detail_title="result",
            )
        ]
