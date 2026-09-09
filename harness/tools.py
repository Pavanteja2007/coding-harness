"""Bash-only tool interface (mini-swe-agent style — spec item 2).

Decision: BASH-ONLY action space, one shell command per model turn, with
capped output fed back into context. Rationale: simplest interface that
works with any model (no structured tool-calling needed), proven by
mini-swe-agent (>74% SWE-bench Verified with a ~100-line harness). The
agent signals "step done" by outputting SUBMIT (completion is still
verifier-gated in core.py — SUBMIT is only a step exit, not success).

This module defines the BashSession: stateful (cwd), with a run() that
executes commands in the working repo copy via the sandbox boundary
(execute_sandboxed), truncates output, and tracks the actions taken.
"""
import re
from pathlib import Path
from typing import Dict, List, Optional

from shared.types import ExecutionResult
from harness.deps import get_execute_sandboxed

SUBMIT = "SUBMIT"

# RECALL <terms> — the on-demand reinjection signal (spec item 13, reversible
# compaction): in place of a bash command, a step session asks the harness to
# pull older, compacted-away detail (tool outputs, verify tails, earlier
# step records) back out of trace.jsonl and re-inject it into THIS session's
# context. Parsed before _extract_command so its line is never run as a shell
# command.
_RECALL_PAT = re.compile(r"^\s*RECALL\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)

# Commands that only observe (no repo mutation) — used to decide whether a
# step session's commands justify re-checking protected paths / files touched.
_OBSERVE_PAT = re.compile(
    r"^\s*(cat|head|tail|ls|dir|find|grep|rg|sed -n|awk|wc|file|stat|git status|"
    r"git diff|git log|git show|git blame|--version|cd|pwd|"
    r"which|where|echo)\b",
    re.IGNORECASE,
)

# Cheap guard against catastrophic commands inside a step session (the local
# stub runs unsandboxed; Terminal 2's Docker sandbox makes this redundant).
# NOTE: patterns must end before a non-word char (e.g. '/' or '$') — \b
# never matches after '/' so 'rm -rf /' would slip through.
_DENY_PAT = re.compile(
    r"^\s*(rm\s+(-[a-zA-Z]+\s+)*-rf?\s+(/|~|\$HOME|C:\\)|"
    r"mkfs|shutdown|reboot|"
    r"(curl|wget)\s+\S+\s*\|\s*(ba)?sh|"
    r":\(\)\s*\{.*\}\s*;\s*:)",
    re.IGNORECASE,
)


def truncate(text: str, limit: int) -> str:
    """Cap output length, keeping head and tail with a marker in between."""
    if len(text) <= limit:
        return text
    head = max(limit // 2, 1)
    tail = max(limit - head, 1)
    cut = len(text) - head - tail
    return (
        text[:head]
        + f"\n[... {cut} chars omitted ...]\n"
        + text[-tail:]
    )


def is_submit(text: str) -> bool:
    """True iff the model's message is (close to) a bare SUBMIT signal.
    Assumes any whitespace/case variants of SUBMIT alone on the line."""
    return text.strip().upper() == SUBMIT or text.strip() == SUBMIT


def parse_recall(text: str) -> Optional[str]:
    """Return the query of a RECALL request, or None if the message isn't one.

    Accepts `RECALL <terms>` (case-insensitive, terms may span lines when
    the message continues on the next line). A RECALL is a control signal to
    the HARNESS, not a bash command — run_step checks this before extracting
    a command (on both the raw reply and its fence-stripped form), so the
    terms are never executed in the sandbox.
    """
    m = _RECALL_PAT.match((text or "").strip())
    return m.group(1) if m else None


class BashSession:
    """One step-scoped bash session against the working repo copy.

    Assumes commands run via the sandbox boundary with the repo copy's root
    as initial cwd; `cd` persists across run() calls within this session
    because the sandbox stub runs each command in its own subprocess (cwd is
    tracked here, not by a shell). Tracks every command for the trace and
    exposes files_touched for the state file.

    Note on cwd persistence: execute_sandboxed's contract takes repo_path —
    so the session always passes the session cwd via `cd <cwd> && <cmd>`
    composition (never relying on a shell process persisting). This works
    with both the stub and Terminal 2's real sandbox.
    """

    def __init__(self, repo_path: str, timeout_s: int, max_output_chars: int) -> None:
        self.repo_path = str(repo_path)
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self.cwd: Optional[str] = None  # repo-relative, None = root
        self.commands: List[Dict[str, object]] = []
        self.files_touched: set = set()

    # -- internal helpers ------------------------------------------------

    def _compose(self, command: str) -> str:
        """Prefix the session-relative cd so cwd persists without a stateful
        shell process. Uses bash `cd X && cmd` (cmd.exe works too)."""
        if self.cwd:
            return f'cd "{self.cwd}" && {command}'
        return command

    def _map_result(self, result: ExecutionResult) -> str:
        """Shape an ExecutionResult into the string fed back to the model."""
        parts = []
        parts.append(f"exit={result.exit_code}" + (" TIMEOUT" if result.timed_out else ""))
        if result.stdout:
            parts.append("stdout:\n" + truncate(result.stdout, self.max_output_chars))
        if result.stderr:
            parts.append("stderr:\n" + truncate(result.stderr, self.max_output_chars))
        if not result.stdout and not result.stderr:
            parts.append("(no output)")
        return "\n".join(parts)

    def _track_cd(self, command: str) -> None:
        """Parse `cd X` prefix (from _compose'd commands) or standalone cd to
        update session cwd. Minimal parser: only understands the forms the
        session itself composes plus a bare `cd <dir>`."""
        m = re.match(r'^\s*cd\s+"([^"]+)"(?:\s*&&|$)', command)
        if m:
            self.cwd = m.group(1)
            return
        m = re.match(r"^\s*cd\s+(\S+)(?:\s*&&|$)", command)
        if m:
            self.cwd = m.group(1)
            return
        m = re.match(r'^\s*cd\s*$|^cd\s+\.\.$', command or "")
        if command and command.strip() in ("cd", "cd .."):
            if command.strip() == "cd .." and self.cwd:
                # one level up, naive posix handling
                parent = "/".join(self.cwd.rstrip("/").split("/")[:-1])
                self.cwd = parent or None
            else:
                self.cwd = None

    def _update_touched_files(self, command: str) -> None:
        """Best-effort update of files_touched from write-looking commands."""
        m = re.findall(r'(?:^|\s|>)((?:[\w.\-/]+/)?[\w.\-]+\.(?:py|txt|md|json|toml|cfg|ini|yml|yaml))\b', command)
        for cand in m:
            cand = cand.strip('>"\' ')
            if cand and cand not in (".", ".."):
                self.files_touched.add(cand)

    # -- public API ------------------------------------------------------

    def run(self, command: str) -> str:
        """Run one command; returns the model-facing output string.

        Assumes command is a single shell command line from the model.
        Raises PermissionError if the command matches the deny pattern.
        """
        if _DENY_PAT.search(command or ""):
            raise PermissionError(f"command denied by harness safety pattern: {command!r}")
        full = self._compose(command)
        sandbox = get_execute_sandboxed()
        result: ExecutionResult = sandbox(self.repo_path, full, self.timeout_s)
        self._track_cd(full)
        self._update_touched_files(command)
        self.commands.append(
            {
                "command": command,
                "exit_code": result.exit_code,
                "stdout": truncate(result.stdout or "", self.max_output_chars),
                "stderr": truncate(result.stderr or "", self.max_output_chars),
                "timed_out": result.timed_out,
            }
        )
        return self._map_result(result)


def is_observation_command(command: str) -> bool:
    """True if a command only reads (used by core to weight step progress)."""
    return bool(_OBSERVE_PAT.match(command.strip()))
