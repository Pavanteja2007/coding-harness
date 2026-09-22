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
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

from shared.types import ExecutionResult
from harness.deps import get_execute_sandboxed
from harness.tool_errors import ToolError, classify, classify_exception, render_error

SUBMIT = "SUBMIT"

# RECALL <terms> — the on-demand reinjection signal (spec item 13, reversible
# compaction): in place of a bash command, a step session asks the harness to
# pull older, compacted-away detail (tool outputs, verify tails, earlier
# step records) back out of trace.jsonl and re-inject it into THIS session's
# context. Parsed before _extract_command so its line is never run as a shell
# command.
_RECALL_PAT = re.compile(r"^\s*RECALL\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)

# DOCS <query> — the documentation/API lookup signal (Round 8, Task D):
# in place of a bash command, a step session asks the harness to look up
# library/API documentation (shared cache -> interpreter docs -> opt-in
# PyPI) and re-inject it into the live session. Same control-signal
# contract as RECALL: never executed as shell.
_DOCS_PAT = re.compile(r"^\s*DOCS?\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)

# BATCH <cmd1> ;;; <cmd2> ;;; ... — the batched read-only execution signal
# (Round 8, Task B): run several INDEPENDENT read-only commands together
# in one turn instead of one per turn serially. Deliberately narrow:
# every command must match the strict read-only allowlist below (a typo'd
# or write-shaped entry rejects the WHOLE batch — no partial semantics to
# reason about), no composition operators, no redirects. Order-
# independent by contract; results are labeled and returned together.
_BATCH_SEP = ";;;"
_BATCH_PAT = re.compile(r"^\s*BATCH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)

# Strict read-only verb allowlist for BATCH entries. This is MUCH tighter
# than _OBSERVE_PAT (which merely weights step progress): batch entries
# run concurrently, so anything with side effects (even `cd`-persisting
# state, `sort`'s --write, `tee`) is excluded. No composition chars
# allowed anywhere in the entry (pipes/redirects/&&/;; all rejected —
# which is why the separator itself is the non-shell ';;;').
# NOTE: `python -c` is deliberately NOT here despite being a common read
# idiom — it executes arbitrary code (writes included) and cannot be
# verified read-only by inspection. `python -m pydoc` IS here: pydoc
# only renders documentation.
_BATCH_READONLY_PAT = re.compile(
    r"^\s*(?:cat|head|tail|ls|dir|find|grep|rg|wc|file|stat|pwd|"
    r"which|where|type|printenv|env|git\s+status|git\s+diff|git\s+log|"
    r"git\s+show|git\s+blame|git\s+ls-files|python\s+-m\s+pydoc)\b.*$",
    re.IGNORECASE,
)
# Any composition/metacharacter in a batch entry -> reject the entry (and
# thus the batch). Note ;;; is the separator and is already split out.
# Chars that could smuggle a second command or a redirect.
_BATCH_FORBIDDEN = re.compile(r"[<>|&;`]|\$\(|\n")

# Plugin-extended BATCH verbs (Plugins round, Task C): a plugin's TOOL
# VERBS manifest entries extend the read-only allowlist so installed
# plugins can teach the loop a new READ-ONLY diagnostic command (e.g.
# "ruff check", "mypy --version"). Extensions are WORD-anchored verb
# prefixes merged into the pattern at validate time; the forbidden-
# composition guard above still applies to extended entries verbatim —
# a plugin can widen WHICH commands batch, never HOW commands compose.
_extra_batch_verbs: List[str] = []


# First tokens that can NEVER join the BATCH allowlist, no matter what
# a plugin manifest says (defense-in-depth on top of operator trust: the
# BATCH allowlist is the READ-ONLY contract, and these verbs write,
# execute arbitrary code, or destroy by inspection of the verb alone).
_DENY_VERB_TOKENS = frozenset(
    {
        "rm",
        "mv",
        "cp",
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
        "kill",
        "pkill",
        "chmod",
        "chown",
        "tee",
        "sed",
        "awk",
        "curl",
        "wget",
        "python",
        "python3",
        "pip",
        "pip3",
        "sh",
        "bash",
        "zsh",
        "dash",
        "eval",
        "exec",
        "source",
        "docker",
        "git",
        "make",
        "npm",
        "touch",
        "mkdir",
        "rmdir",
        "truncate",
        "shred",
        "sync",
        "sql",
        "sqlite",
        "sqlite3",
        "sudo",
        "su",
        "doas",
        "powershell",
        "pwsh",
        "xargs",
        "yes",
        "tar",
        "unzip",
        "gzip",
        "gunzip",
    }
)


def extend_batch_verbs(verbs: Sequence[str]) -> None:
    """Add plugin-provided read-only verbs to the BATCH allowlist.

    Assumes each verb is a bare command word or a short argv prefix
    ("ruff check", "mypy --version") built only from letters, digits,
    and the separators space/underscore/dash/dot. Anything containing
    shell metacharacters, other special characters, or whose FIRST
    token is a known write/destructive/arbitrary-execution command
    (see _DENY_VERB_TOKENS) is silently ignored — fail-safe: a
    malformed or hostile plugin manifest entry can never widen the
    read-only guard. Idempotent per verb.
    """
    for v in verbs or []:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        # plain words + inner spaces only (multi-word argv prefixes OK)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\- ]*", v):
            continue
        if v.split()[0].lower() in _DENY_VERB_TOKENS:
            continue
        if v not in _extra_batch_verbs:
            _extra_batch_verbs.append(v)


def _batch_readonly_pattern() -> "re.Pattern":
    """The BATCH allowlist pattern including any plugin verb extensions.

    Rebuilds the alternation with plugin verbs appended INSIDE the
    non-capturing group (the base pattern's shape is
    ^\\s*(?:...verbs...)\\b.*$ — the merge re-opens that group rather
    than appending after its end, so an extended verb anchors exactly
    like a built-in one).
    """
    if not _extra_batch_verbs:
        return _BATCH_READONLY_PAT
    extra = "|".join(
        re.escape(v) + r"\s*" if " " in v else re.escape(v) for v in _extra_batch_verbs
    )
    base = _BATCH_READONLY_PAT.pattern
    # base ends with r")\b.*$" — splice the extra verbs in before that
    tail = r")\b.*$"
    if not base.endswith(tail):
        return _BATCH_READONLY_PAT  # unexpected shape: fail safe, no merge
    return re.compile(base[: -len(tail)] + "|" + extra + tail, re.IGNORECASE)


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
# Round 6 hardening (adversarial cases): the flag cluster now matches any
# order of r/f among mixed flags ('rm -fr', 'rm -rf'), and pipes into a
# shell cover bash as well as sh ('curl ... | bash', 'wget ... | sh').
_DENY_PAT = re.compile(
    r"^\s*(rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*-?[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|C:\\)|"
    r"rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|C:\\)|"
    r"mkfs|shutdown|reboot|"
    r"(curl|wget)\s+[^|]*\|\s*(ba|z|da)?sh|"
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
    return text[:head] + f"\n[... {cut} chars omitted ...]\n" + text[-tail:]


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


def parse_docs(text: str) -> Optional[str]:
    """Return the query of a DOCS request, or None if the message isn't
    one. Same control-signal contract as parse_recall — a DOCS is parsed
    before command extraction so its terms are never run as shell."""
    m = _DOCS_PAT.match((text or "").strip())
    return m.group(1) if m else None


def parse_batch(text: str) -> Optional[List[str]]:
    """Return the command list of a BATCH request, or None if the
    message isn't one.

    Accepts `BATCH <cmd> ;;; <cmd> ;;; ...` (case-insensitive; the
    separator is the non-shell ';;;' so a shell parser can't mistake
    entries for one command chain). A BATCH is a control signal to the
    HARNESS: the entries are executed by run_batch (below) under a
    STRICT read-only allowlist — never as one composed shell line. Any
    entry that fails the allowlist rejects the whole batch (the caller
    feeds that back to the model); no partial execution.
    """
    m = _BATCH_PAT.match((text or "").strip())
    if not m:
        return None
    raw = m.group(1)
    cmds = [c.strip() for c in raw.split(_BATCH_SEP)]
    cmds = [c for c in cmds if c]
    if not cmds:
        return None
    return cmds


def validate_batch(cmds: Sequence[str]) -> Optional[str]:
    """Check every BATCH entry against the strict read-only allowlist.

    Returns None when all entries are read-only and composition-free;
    otherwise the first offending entry (for the model-facing rejection
    message — the whole batch is rejected, never partially run)."""
    pat = _batch_readonly_pattern()
    for c in cmds or []:
        if not pat.match(c) or _BATCH_FORBIDDEN.search(c):
            return c
    return None


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

    def _map_result(self, result: ExecutionResult, command: str = "") -> str:
        """Shape an ExecutionResult into the string fed back to the model.

        Round 8 (Task A): a FAILING result is now classified first —
        the model receives `TOOL ERROR [<kind>]: <detail>` + a suggested
        fix instead of raw exit/stderr noise to parse. The raw text is
        still present (capped) for diagnosis; a successful run renders
        exactly as before (existing consumers of "exit=0" strings are
        unaffected).
        """
        parts = []
        if result.timed_out or result.exit_code != 0:
            err = classify(
                result.exit_code,
                result.stdout or "",
                result.stderr or "",
                result.timed_out,
                command=command,
            )
            parts.append(render_error(err))
        parts.append(
            f"exit={result.exit_code}" + (" TIMEOUT" if result.timed_out else "")
        )
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
        m = re.match(r"^\s*cd\s*$|^cd\s+\.\.$", command or "")
        if command and command.strip() in ("cd", "cd .."):
            if command.strip() == "cd .." and self.cwd:
                # one level up, naive posix handling
                parent = "/".join(self.cwd.rstrip("/").split("/")[:-1])
                self.cwd = parent or None
            else:
                self.cwd = None

    def _update_touched_files(self, command: str) -> None:
        """Best-effort update of files_touched from write-looking commands."""
        m = re.findall(
            r"(?:^|\s|>)((?:[\w.\-/]+/)?[\w.\-]+\.(?:py|txt|md|json|toml|cfg|ini|yml|yaml))\b",
            command,
        )
        for cand in m:
            cand = cand.strip(">\"' ")
            if cand and cand not in (".", ".."):
                self.files_touched.add(cand)

    # -- public API ------------------------------------------------------

    def run(self, command: str) -> str:
        """Run one command; returns the model-facing output string.

        Assumes command is a single shell command line from the model.
        Raises PermissionError if the command matches the deny pattern.
        A sandbox-layer exception other than PermissionError /
        SandboxUnavailableError is classified (Round 8, Task A) and
        re-raised as ToolExecutionError — the loop controller turns it
        into structured feedback. SandboxUnavailableError keeps its own
        class (fail-loud is the harness contract) and is never swallowed.
        """
        if _DENY_PAT.search(command or ""):
            raise PermissionError(
                f"command denied by harness safety pattern: {command!r}"
            )
        full = self._compose(command)
        sandbox = get_execute_sandboxed()
        try:
            result: ExecutionResult = sandbox(self.repo_path, full, self.timeout_s)
        except PermissionError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "SandboxUnavailableError":
                raise
            err = classify_exception(exc, command)
            raise ToolExecutionError(err.kind, err.detail, command) from exc
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
        return self._map_result(result, command)


class ToolExecutionError(RuntimeError):
    """A harness-side tool failure (not a command failure) that has been
    classified (Round 8, Task A): kind is a stable tool_errors class.
    Lets the loop controller feed structured feedback instead of a raw
    traceback while still surfacing the failure loudly."""

    def __init__(self, kind: str, detail: str, command: str = "") -> None:
        super().__init__(f"[{kind}] {detail}")
        self.kind = kind
        self.detail = detail
        self.command = command


def run_batch(
    repo_path: str,
    commands: Sequence[str],
    timeout_s: int,
    max_output_chars: int,
    max_workers: int = 4,
) -> Tuple[List[Dict[str, object]], str]:
    """Execute a validated BATCH (read-only, order-independent commands)
    concurrently in one turn — Round 8, Task B.

    Assumes the caller (core.run_step) ALREADY validated the entries via
    validate_batch (this function re-checks defensively and returns a
    rejection string instead of executing when an entry fails). Each
    entry runs through the SAME BashSession path as a normal command
    (deny guard, output capping) — via a THROWAWAY session per entry,
    because the entries are order-independent by contract: no shared cwd
    state may couple them. Returns (records, rendered) where records
    mirror BashSession.commands entries (for the trace) and rendered is
    the single model-facing message with every labeled result. Never
    mutates any persistent session state.
    """
    bad = validate_batch(commands)
    if bad is not None:
        records: List[Dict[str, object]] = []
        return records, (
            f"BATCH REJECTED: entry {bad!r} is not a simple read-only "
            "command (composition operators and side effects are not "
            "allowed in batches). Re-issue as individual commands, or "
            "BATCH with only simple read-only entries."
        )

    def _one(cmd: str) -> Dict[str, object]:
        s = BashSession(repo_path, timeout_s, max_output_chars)
        try:
            out = s.run(cmd)
            rec = (
                dict(s.commands[-1])
                if s.commands
                else {
                    "command": cmd,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "no result recorded",
                    "timed_out": False,
                }
            )
            rec["output"] = out
        except PermissionError as exc:
            rec = {
                "command": cmd,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
                "output": f"COMMAND REJECTED: {exc}",
            }
        except ToolExecutionError as exc:
            rec = {
                "command": cmd,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
                "output": f"TOOL ERROR [{exc.kind}]: {exc.detail}",
            }
        return rec

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        records = list(pool.map(_one, list(commands)))

    lines = [f"BATCH results ({len(records)} commands, executed concurrently):"]
    for i, rec in enumerate(records, start=1):
        lines.append(f"--- [{i}] {rec['command']} ---")
        lines.append(str(rec["output"]))
    lines.append(
        "End of BATCH results. Continue with exactly ONE bash command "
        "(or another BATCH), or SUBMIT if this step is done."
    )
    return records, "\n".join(lines)


def is_observation_command(command: str) -> bool:
    """True if a command only reads (used by core to weight step progress)."""
    return bool(_OBSERVE_PAT.match(command.strip()))
