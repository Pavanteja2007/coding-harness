"""Typed production tool catalog plus the legacy BashSession adapter.

``TypedToolRuntime`` validates every canonical tool call before delegating to
``execution.workspace.SafeToolBackend``. ``BashSession`` remains the
compatibility adapter used by the verified-fix loop and batch protocol.
"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness import editor
from harness.deps import get_execute_sandboxed
from harness.tool_errors import (
    ToolError,
    classify,
    classify_exception,
    render_error,
    shape_tool_output,
)
from shared.types import ExecutionResult

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
_BATCH_SIDE_EFFECT = re.compile(
    r"(?:^|\s)(?:--output(?:=|\s)|--pre(?:=|\s)|--run(?:=|\s)|"
    r"--in-place(?:\s|$)|--backup(?:\s|$)|--delete(?:\s|$)|"
    r"-delete(?:\s|$)|-exec(?:dir)?(?:\s|$)|-ok(?:dir)?(?:\s|$)|"
    r"-fprint(?:f)?(?:\s|$)|-fls(?:\s|$))",
    re.IGNORECASE,
)


def _batch_entry_is_safe(entry: str) -> bool:
    """Return whether one BATCH command is observation-only."""
    if not entry or _BATCH_FORBIDDEN.search(entry) or _BATCH_SIDE_EFFECT.search(entry):
        return False
    parts = entry.strip().split()
    if not parts:
        return False
    command = parts[0].lower()
    if command == "find":
        return not any(
            token.lower().startswith(("-delete", "-exec", "-ok", "-fprint", "-fls"))
            for token in parts[1:]
        )
    if command == "env":
        return not any(
            token and not token.startswith("-") and "=" not in token
            for token in parts[1:]
        )
    return True


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
        if not pat.match(c) or not _batch_entry_is_safe(c):
            return c
    return None


class TypedToolValidationError(ValueError):
    """Raised when a typed production tool call violates its schema."""


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral schema and effect metadata for one production tool.

    ``side_effect_class`` is the EFFECT vocabulary (``read_only``,
    ``workspace_write``, ``process``, ``network``, ``mcp``, ``memory``,
    ``control``). ``read_only`` is the CONCURRENCY declaration, and it is a
    separate field on purpose: the dispatcher asks "may this run while another
    is running?", which is a different question from "what does this change?".

    It may be declared explicitly, which is what the catalog does for every
    read-only entry, or left ``None`` to derive from
    ``side_effect_class == "read_only"``. Either way it resolves to a concrete
    bool in ``__post_init__``, so a consumer never has to know which was used.
    Declaring it on the spec is what lets the dispatcher work from the catalog
    rather than from a hardcoded list of tool names that would drift.
    """

    name: str
    side_effect_class: str
    required: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()
    types: Mapping[str, type | Tuple[type, ...]] = None
    requires_approval: bool = False
    aliases: Tuple[str, ...] = ()
    read_only: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "").strip().lower())
        object.__setattr__(
            self, "side_effect_class", str(self.side_effect_class or "read_only")
        )
        object.__setattr__(self, "required", tuple(self.required or ()))
        object.__setattr__(self, "optional", tuple(self.optional or ()))
        object.__setattr__(self, "types", dict(self.types or {}))
        object.__setattr__(self, "requires_approval", bool(self.requires_approval))
        object.__setattr__(
            self,
            "aliases",
            tuple(str(item).strip().lower() for item in self.aliases or ()),
        )
        object.__setattr__(
            self,
            "read_only",
            bool(self.side_effect_class == "read_only")
            if self.read_only is None
            else bool(self.read_only),
        )

    def to_schema(self) -> Dict[str, Any]:
        """Return a provider-neutral JSON schema for this tool."""
        properties: Dict[str, Dict[str, Any]] = {}
        for field in (*self.required, *self.optional):
            expected = self.types.get(field, str)
            values = expected if isinstance(expected, tuple) else (expected,)
            if bool in values:
                kind = "boolean"
            elif int in values:
                kind = "integer"
            elif float in values:
                kind = "number"
            elif dict in values:
                kind = "object"
            elif list in values:
                kind = "array"
            else:
                kind = "string"
            properties[field] = {"type": kind}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.name} ({self.side_effect_class})",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


def _tool(
    name: str,
    effect: str,
    *,
    required: Sequence[str] = (),
    optional: Sequence[str] = (),
    types: Optional[Mapping[str, type | Tuple[type, ...]]] = None,
    approval: bool = False,
    aliases: Sequence[str] = (),
    read_only: Optional[bool] = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        side_effect_class=effect,
        required=tuple(required),
        optional=tuple(optional),
        types=types,
        requires_approval=approval,
        aliases=tuple(aliases),
        read_only=read_only,
    )


_TYPED_TOOL_SPECS: Tuple[ToolSpec, ...] = (
    _tool("read", "read_only", required=("path",), types={"path": str}, read_only=True),
    _tool(
        "glob",
        "read_only",
        read_only=True,
        required=("pattern",),
        optional=("path", "max_results"),
        types={"pattern": str, "path": str, "max_results": int},
    ),
    _tool(
        "grep",
        "read_only",
        read_only=True,
        required=("pattern",),
        optional=("path", "glob", "max_results"),
        types={"pattern": str, "path": str, "glob": str, "max_results": int},
    ),
    _tool(
        "list",
        "read_only",
        read_only=True,
        optional=("path", "max_entries"),
        types={"path": str, "max_entries": int},
    ),
    _tool(
        "image",
        "read_only",
        read_only=True,
        required=("path",),
        optional=("include_data",),
        types={"path": str, "include_data": bool},
    ),
    _tool(
        "apply_patch",
        "workspace_write",
        required=("patch", "expected_revisions"),
        optional=("allow_delete", "allow_preexisting_change"),
        types={
            "patch": str,
            "expected_revisions": dict,
            "allow_delete": bool,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "edit",
        "workspace_write",
        required=("path", "old_string", "new_string", "expected_revision"),
        optional=("hunk_id", "allow_preexisting_change"),
        types={
            "path": str,
            "old_string": str,
            "new_string": str,
            "expected_revision": (str, dict),
            "hunk_id": str,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "write",
        "workspace_write",
        required=("path", "content"),
        optional=(
            "expected_revision",
            "overwrite",
            "hunk_id",
            "allow_preexisting_change",
        ),
        types={
            "path": str,
            "content": str,
            "expected_revision": (str, dict),
            "overwrite": bool,
            "hunk_id": str,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "rename",
        "workspace_write",
        required=("source_path", "destination_path", "expected_revision"),
        optional=("hunk_id", "allow_preexisting_change"),
        types={
            "source_path": str,
            "destination_path": str,
            "expected_revision": (str, dict),
            "hunk_id": str,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "delete",
        "workspace_write",
        required=("path", "expected_revision"),
        optional=("hunk_id", "allow_preexisting_change"),
        types={
            "path": str,
            "expected_revision": (str, dict),
            "hunk_id": str,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "undo",
        "workspace_write",
        optional=(
            "operation_id",
            "hunk_id",
            "steps",
            "targets",
            "allow_preexisting_change",
        ),
        types={
            "operation_id": str,
            "hunk_id": str,
            "steps": (int, str),
            "targets": list,
            "allow_preexisting_change": bool,
        },
        approval=True,
    ),
    _tool(
        "git_status",
        "read_only",
        optional=("max_chars",),
        types={"max_chars": int},
        read_only=True,
    ),
    _tool(
        "git_diff",
        "read_only",
        read_only=True,
        optional=("path", "revision", "max_chars"),
        types={"path": str, "revision": str, "max_chars": int},
    ),
    _tool(
        "git_log",
        "read_only",
        read_only=True,
        optional=("path", "revision", "max_count", "max_chars"),
        types={"path": str, "revision": str, "max_count": int, "max_chars": int},
    ),
    _tool(
        "git_show",
        "read_only",
        read_only=True,
        optional=("path", "revision", "max_chars"),
        types={"path": str, "revision": str, "max_chars": int},
    ),
    _tool(
        "git_blame",
        "read_only",
        read_only=True,
        required=("path",),
        optional=("start_line", "end_line", "max_chars"),
        types={"path": str, "start_line": int, "end_line": int, "max_chars": int},
    ),
    _tool(
        "git_branch",
        "read_only",
        read_only=True,
        optional=("branch", "max_chars"),
        types={"branch": str, "max_chars": int},
    ),
    _tool(
        "git_worktree",
        "read_only",
        read_only=True,
        optional=("max_chars",),
        types={"max_chars": int},
    ),
    _tool(
        "shell",
        "process",
        required=("command",),
        optional=("timeout_s", "cwd", "env", "allow_network"),
        types={
            "command": str,
            "timeout_s": int,
            "cwd": str,
            "env": dict,
            "allow_network": bool,
        },
        approval=True,
        aliases=("bash",),
    ),
    _tool(
        "process",
        "process",
        optional=(
            "command",
            "background",
            "pty",
            "cwd",
            "env",
            "timeout_s",
            "process_id",
        ),
        types={
            "command": str,
            "background": bool,
            "pty": bool,
            "cwd": str,
            "env": dict,
            "timeout_s": int,
            "process_id": (str, int),
        },
        approval=True,
    ),
    _tool(
        "process_read_output",
        "process",
        required=("process_id",),
        optional=("stdout_offset", "stderr_offset", "max_chars"),
        types={
            "process_id": (str, int),
            "stdout_offset": int,
            "stderr_offset": int,
            "max_chars": int,
        },
    ),
    _tool(
        "process_write_stdin",
        "process",
        required=("process_id", "data"),
        optional=("append_newline",),
        types={"process_id": (str, int), "data": str, "append_newline": bool},
        approval=True,
    ),
    _tool(
        "process_kill",
        "process",
        required=("process_id",),
        optional=("force",),
        types={"process_id": (str, int), "force": bool},
        approval=True,
    ),
    _tool(
        "test",
        "process",
        # ``command`` and ``test_command`` are both accepted: the kernel's
        # verifier-gated handler reads ``command`` while the production
        # ``execution.verify`` boundary documents ``test_command``. One
        # catalog must serve both without a second, divergent tool list.
        optional=(
            "target_test",
            "command",
            "test_command",
            "timeout_s",
            "verify_timeout_s",
        ),
        types={
            "target_test": str,
            "command": str,
            "test_command": str,
            "timeout_s": int,
            "verify_timeout_s": int,
        },
        approval=True,
    ),
    # ``verify`` is the verifier-gated completion probe (it consults the
    # run's CompletionPolicy instead of running an arbitrary command) and
    # ``cancel`` is the terminal control signal. Both were historically
    # kernel-only entries; they now live in the ONE catalog so the kernel
    # has no private tool list of its own.
    _tool(
        "verify",
        "process",
        optional=("target_test", "command", "test_command"),
        types={
            "target_test": str,
            "command": str,
            "test_command": str,
        },
    ),
    _tool(
        "lint",
        "process",
        optional=("command", "timeout_s"),
        types={"command": str, "timeout_s": int},
        approval=True,
    ),
    _tool(
        "typecheck",
        "process",
        optional=("command", "timeout_s"),
        types={"command": str, "timeout_s": int},
        approval=True,
    ),
    _tool(
        "build",
        "process",
        optional=("command", "timeout_s"),
        types={"command": str, "timeout_s": int},
        approval=True,
    ),
    _tool(
        "web_fetch",
        "network",
        required=("url",),
        optional=("timeout_s", "max_bytes", "max_chars", "max_redirects"),
        types={
            "url": str,
            "timeout_s": int,
            "max_bytes": int,
            "max_chars": int,
            "max_redirects": int,
        },
        approval=True,
        aliases=("fetch",),
    ),
    _tool(
        "web_search",
        "network",
        required=("query",),
        optional=("limit", "timeout_s"),
        types={"query": str, "limit": int, "timeout_s": int},
        approval=True,
        aliases=("search",),
    ),
    _tool(
        "mcp",
        "mcp",
        required=("server", "name"),
        optional=("args", "env"),
        types={"server": str, "name": str, "args": dict, "env": dict},
        approval=True,
        aliases=("mcp_call",),
    ),
    _tool(
        "memory",
        "memory",
        optional=("action", "query", "text", "category", "limit", "task_id"),
        types={
            "action": str,
            "query": str,
            "text": str,
            "category": str,
            "limit": int,
            "task_id": str,
        },
    ),
    # Symbol-level reads. ``read``/``grep`` answer "which file"; these answer
    # "where is this definition, who calls it, and what would a change to it
    # touch", which is what a model otherwise re-derives by re-reading whole
    # files. All four are read-only and resolve through the tree-sitter index
    # built by memory.code_graph, so a symbol outside the session's initial
    # file window is reachable in one call.
    _tool(
        "read_symbol",
        "read_only",
        read_only=True,
        required=("symbol",),
        optional=("path", "max_lines"),
        types={"symbol": str, "path": str, "max_lines": int},
    ),
    _tool(
        "find_definition",
        "read_only",
        read_only=True,
        required=("symbol",),
        optional=("path",),
        types={"symbol": str, "path": str},
    ),
    _tool(
        "find_references",
        "read_only",
        read_only=True,
        required=("symbol",),
        optional=("path", "max_results"),
        types={"symbol": str, "path": str, "max_results": int},
    ),
    _tool(
        "blast_radius",
        "read_only",
        read_only=True,
        required=("symbol",),
        optional=("paths", "depth", "max_files"),
        types={"symbol": str, "paths": list, "depth": int, "max_files": int},
    ),
    # Durable memory capture. Permission-gated (``approval=True``) because a
    # stored row outlives the session and is read back by later ones, so
    # writing one is an operator-visible effect, not a read. The handler
    # additionally routes every write through the untrusted-content gate: a
    # row claiming system authority is quarantined and a secret is redacted
    # before anything reaches the store.
    _tool(
        "memory_record",
        "memory",
        required=("text",),
        optional=("category", "verified", "source"),
        types={"text": str, "category": str, "verified": bool, "source": str},
        approval=True,
    ),
    # AST codemods. A codemod is a PLAN first: the tool always returns the
    # complete change set plus a completeness receipt (files considered, sites
    # changed, and the NAMED set of sites it could not resolve), and only then
    # applies it -- through the ordinary ``edit`` path, so the ordinary digest
    # precondition, unique-match guard, protected-path policy and undo journal
    # all apply unchanged. Two entries rather than one ``codemod`` tool with a
    # discriminator, because a rename and a signature change are two different
    # intents and a model picks the wrong one less often when it can say which.
    # ``workspace_write`` + ``approval=True`` because the effect is a
    # multi-file mutation a human should be able to see before it lands.
    _tool(
        "rename_symbol",
        "workspace_write",
        required=("symbol", "new_name"),
        optional=("path", "apply", "allow_incomplete"),
        types={
            "symbol": str,
            "new_name": str,
            "path": str,
            "apply": bool,
            "allow_incomplete": bool,
        },
        approval=True,
    ),
    _tool(
        "update_signature",
        "workspace_write",
        required=("symbol",),
        optional=(
            "path",
            "added",
            "removed",
            "renamed",
            "retyped",
            "apply",
            "allow_incomplete",
        ),
        types={
            "symbol": str,
            "path": str,
            "added": list,
            "removed": list,
            "renamed": dict,
            "retyped": dict,
            "apply": bool,
            "allow_incomplete": bool,
        },
        approval=True,
    ),
    _tool(
        "ask",
        "control",
        required=("question",),
        optional=("choices",),
        types={"question": str, "choices": list},
        # ``question`` is the same control tool under the production /
        # execution-backend vocabulary (``policy_for_tool`` already maps
        # ask->question). Aliasing instead of duplicating keeps ONE entry
        # while both vocabularies resolve.
        aliases=("question",),
    ),
    _tool(
        "todo",
        "control",
        required=("items",),
        optional=("replace",),
        types={"items": list, "replace": bool},
    ),
    _tool(
        "plan",
        "control",
        required=("steps",),
        optional=("notes",),
        types={"steps": list, "notes": str},
    ),
    _tool(
        "task",
        "control",
        required=("description",),
        optional=("task_id", "status", "agent", "files", "symbols", "depends_on"),
        types={
            "description": str,
            "task_id": str,
            "status": str,
            "agent": str,
            "files": list,
            "symbols": list,
            "depends_on": list,
        },
    ),
    _tool(
        "finish",
        "control",
        optional=("answer", "checks"),
        types={"answer": str, "checks": list},
        aliases=("done",),
    ),
    _tool(
        "cancel",
        "control",
        optional=("reason",),
        types={"reason": str},
        aliases=("abort",),
    ),
)

_TYPED_TOOL_INDEX: Dict[str, ToolSpec] = {}
for _spec_value in _TYPED_TOOL_SPECS:
    _TYPED_TOOL_INDEX[_spec_value.name] = _spec_value
    for _alias in _spec_value.aliases:
        _TYPED_TOOL_INDEX[_alias] = _spec_value


def typed_tool_specs() -> Tuple[ToolSpec, ...]:
    """Return the complete production typed tool catalog."""
    return _TYPED_TOOL_SPECS


def typed_tool_spec(name: str) -> Optional[ToolSpec]:
    """Return one canonical or aliased production tool specification."""
    return _TYPED_TOOL_INDEX.get(str(name or "").strip().lower())


def canonical_tool_names() -> Tuple[str, ...]:
    """Return every canonical catalog name in catalog order."""
    return tuple(spec.name for spec in _TYPED_TOOL_SPECS)


def canonical_tool_aliases() -> Dict[str, str]:
    """Return a mapping of every accepted alias to its canonical name."""
    return {alias: spec.name for spec in _TYPED_TOOL_SPECS for alias in spec.aliases}


def _catalog_shape(spec: ToolSpec) -> Dict[str, Any]:
    """Return the comparable identity of one catalog entry.

    ``read_only`` is part of the identity: it is the concurrency declaration
    the dispatcher schedules on, so a catalog that agrees about names and
    schemas but disagrees about what may run in parallel is a DIVERGENT
    catalog, not an identical one.
    """
    return {
        "name": spec.name,
        "side_effect_class": spec.side_effect_class,
        "read_only": bool(
            getattr(spec, "read_only", spec.side_effect_class == "read_only")
        ),
        "required": list(spec.required),
        "optional": list(spec.optional),
        "types": {
            key: sorted(_type_names(value)) for key, value in sorted(spec.types.items())
        },
        "aliases": sorted(spec.aliases),
    }


def _type_names(value: Any) -> Tuple[str, ...]:
    """Return stable type names for one declared argument type."""
    values = value if isinstance(value, tuple) else (value,)
    return tuple(sorted(getattr(item, "__name__", str(item)) for item in values))


def catalog_fingerprint(specs: Optional[Sequence[ToolSpec]] = None) -> str:
    """Return a stable SHA-256 fingerprint of a catalog's names and schemas.

    Two catalogs with the same fingerprint expose identical tool names,
    required/optional arguments, argument types, aliases, and effect
    classes. This is the machine-checkable form of "one catalog".
    """
    selected = tuple(specs) if specs is not None else _TYPED_TOOL_SPECS
    payload = json.dumps(
        [_catalog_shape(spec) for spec in selected],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def catalog_concurrency_report(
    specs: Optional[Sequence[ToolSpec]] = None,
) -> Dict[str, Any]:
    """Return the catalog's concurrency classes, derived from the specs.

    The dispatcher must never hold a list of "the tools that are safe to run in
    parallel"; it asks the spec. This report is the readable form of that
    answer — which entries declare ``read_only`` and which are sequential — so
    a reviewer can see the concurrency policy without reading the dispatcher,
    and a test can assert the declaration is present on every entry rather than
    inherited by accident.
    """
    selected = tuple(specs) if specs is not None else _TYPED_TOOL_SPECS
    concurrent: List[str] = []
    sequential: List[str] = []
    for spec in selected:
        read_only = bool(
            getattr(spec, "read_only", spec.side_effect_class == "read_only")
        )
        (concurrent if read_only else sequential).append(spec.name)
    return {
        "checked": len(selected),
        "concurrent": concurrent,
        "sequential": sequential,
        "concurrent_count": len(concurrent),
        "sequential_count": len(sequential),
    }


def catalog_parity_report(specs: Sequence[ToolSpec]) -> Dict[str, Any]:
    """Compare a derived catalog against the canonical production catalog.

    Assumes ``specs`` are objects exposing the ``ToolSpec`` surface (the
    kernel's own spec type is accepted). Returns a machine-readable report
    describing every missing, extra, or divergent entry; an empty
    ``differences`` list means the two catalogs are identical.
    """
    derived = [_catalog_shape(spec) for spec in specs]
    canonical = [_catalog_shape(spec) for spec in _TYPED_TOOL_SPECS]
    derived_by_name = {item["name"]: item for item in derived}
    canonical_by_name = {item["name"]: item for item in canonical}
    missing = sorted(set(canonical_by_name) - set(derived_by_name))
    extra = sorted(set(derived_by_name) - set(canonical_by_name))
    divergent = sorted(
        name
        for name in set(canonical_by_name) & set(derived_by_name)
        if canonical_by_name[name] != derived_by_name[name]
    )
    differences = [
        {
            "kind": kind,
            "tool": name,
            "expected": canonical_by_name.get(name),
            "actual": derived_by_name.get(name),
        }
        for kind, names in (
            ("missing", missing),
            ("extra", extra),
            ("divergent", divergent),
        )
        for name in names
    ]
    return {
        "canonical_fingerprint": catalog_fingerprint(),
        "derived_fingerprint": catalog_fingerprint(specs),
        "canonical_names": list(canonical_by_name),
        "derived_names": list(derived_by_name),
        "missing": missing,
        "extra": extra,
        "divergent": divergent,
        "differences": differences,
        "identical": not differences,
    }


def typed_tool_schemas() -> List[Dict[str, Any]]:
    """Return provider-neutral schemas for every canonical production tool."""
    return [spec.to_schema() for spec in _TYPED_TOOL_SPECS]


def validate_typed_arguments(
    name: str, arguments: Optional[Mapping[str, Any]] = None
) -> Tuple[str, Dict[str, Any]]:
    """Validate and normalize one production tool call before policy dispatch."""
    spec = typed_tool_spec(name)
    if spec is None:
        raise TypedToolValidationError(f"unknown tool: {name}")
    values = dict(arguments or {})
    allowed = set(spec.required) | set(spec.optional) | {"call_id"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise TypedToolValidationError(
            f"{spec.name} received unknown arguments: {', '.join(unknown)}"
        )
    missing = sorted(set(spec.required) - set(values))
    if missing:
        raise TypedToolValidationError(
            f"{spec.name} missing required arguments: {', '.join(missing)}"
        )
    for field, expected in spec.types.items():
        if field not in values:
            continue
        allowed_types = expected if isinstance(expected, tuple) else (expected,)
        if type(values[field]) not in allowed_types:
            raise TypedToolValidationError(
                f"{spec.name}.{field} has invalid type {type(values[field]).__name__}"
            )
    for field in ("path", "target", "source_path", "destination_path", "file"):
        if field not in values:
            continue
        text = str(values[field]).replace("\\", "/")
        if (
            text.startswith("/")
            or re.match(r"^[A-Za-z]:", text)
            or ".." in Path(text).parts
        ):
            raise TypedToolValidationError(
                f"{spec.name}.{field} must be repository-relative"
            )
    return spec.name, values


#: Every canonical tool that CHANGES the workspace. The in-edit check screens
#: exactly these: a read-only call has no candidate content, and a mutation the
#: gate cannot see is the failure mode this exists to close. The two codemod
#: tools are here because every edit they apply is an exact old_string /
#: new_string pair, i.e. the same shape `edit` takes.
MUTATING_TOOLS: Tuple[str, ...] = (
    "edit",
    "write",
    "apply_patch",
    "rename",
    "delete",
    "undo",
    "rename_symbol",
    "update_signature",
)

#: Gate receipt status for "there is no candidate content to check here" - a
#: read-only call, a delete/undo, or arguments that do not name a path. It is
#: NOT `unchecked` (nothing was wanted of a checker) and it is NOT `passed`.
GATE_NOT_APPLICABLE = "not_applicable"

_GATE_STATUSES_DOC = (
    "the lint statuses plus GATE_NOT_APPLICABLE; read it from "
    "gate_statuses() rather than re-deriving it"
)


def gate_statuses() -> Tuple[str, ...]:
    """Every status a `precommit_check_call` receipt can carry.

    The lint module owns its own four (passed / failed / unchecked /
    disabled) and this module adds one (`not_applicable`), so a consumer
    enumerates the vocabulary from here instead of hardcoding a fifth copy."""
    from harness.lint import CHECK_STATUSES

    return (*CHECK_STATUSES, GATE_NOT_APPLICABLE)


def _gate_receipt(tool: str, path: str = "") -> Dict[str, Any]:
    """Return a fresh, JSON-safe gate receipt for one mutation."""
    return {
        "tool": tool,
        "path": path,
        "status": GATE_NOT_APPLICABLE,
        "refused": False,
        "checked": False,
        "line": 0,
        "message": "",
        "context": "",
        "reason": "",
    }


def _read_source(root: Optional[str], rel: str) -> Tuple[Optional[str], str]:
    """Read a repo file as STRICT utf-8 text; (text, reason_when_none).

    Strict on purpose. A pre-flight check that read a latin-1 file with
    ``errors="replace"`` would be checking mojibake, and a refusal based on
    mojibake would be a false positive against a file that is fine. "I could
    not read it as UTF-8" is an honest `not_applicable`; guessing is not
    available here, for the same reason `harness.editor` refuses an
    undetermined encoding rather than rewriting it."""
    if not root:
        return None, "no repository root was supplied to the gate"
    rel = str(rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None, f"{rel!r} is not a repository-relative path"
    target = Path(root, rel)
    try:
        if not target.is_file():
            return None, f"{rel} does not exist yet, so there is no old content"
        return target.read_text(encoding="utf-8"), ""
    except UnicodeDecodeError:
        return None, f"{rel} is not valid UTF-8, so it was not decoded for checking"
    except OSError as exc:
        return None, f"{rel} could not be read for checking ({exc})"


def _exact_edit_candidate(
    root: Optional[str], values: Mapping[str, Any]
) -> List[Tuple[str, Optional[str], str]]:
    """Candidate post-image of an exact-block edit (edit + both codemods)."""
    rel = str(values.get("path") or "")
    old = values.get("old_string")
    new = values.get("new_string")
    if not rel or old is None or new is None:
        return [(rel, None, "the arguments name no path/old_string/new_string")]
    src, why = _read_source(root, rel)
    if src is None:
        return [(rel, None, why)]
    old_text = str(old)
    count = src.count(old_text)
    if count == 0:
        return [
            (
                rel,
                None,
                f"old_string is not present in {rel}; the edit path's own "
                "no_match refusal owns that case",
            )
        ]
    if count > 1:
        return [
            (
                rel,
                None,
                f"old_string matches {count} places in {rel}; an ambiguous "
                "edit is refused by the edit path, with the candidates listed",
            )
        ]
    return [(rel, src.replace(old_text, str(new), 1), "")]


def _write_candidate(
    root: Optional[str], values: Mapping[str, Any]
) -> List[Tuple[str, Optional[str], str]]:
    """Candidate post-image of a `write` - the content the model supplied."""
    rel = str(values.get("path") or "")
    content = values.get("content")
    if not rel or content is None:
        return [(rel, None, "write requires path and content")]
    target = Path(str(root), rel) if root else None
    if target is not None and target.is_file() and values.get("overwrite") is False:
        return [
            (
                rel,
                None,
                f"{rel} exists and the call did not ask to overwrite it, so "
                "the write is refused before any content is committed",
            )
        ]
    return [(rel, str(content), "")]


def _parse_unified_patch(patch: str) -> List[Tuple[str, int, List[str]]]:
    """Parse a unified diff into (rel, old_start, replacement_lines) triples.

    Deliberately narrow: it understands only the hunks it can apply EXACTLY,
    and raises `ValueError` on anything it cannot reconstruct, because a
    half-applied patch used as a syntax check is worse than no check - it
    would judge a file that was never going to exist."""
    out: List[Tuple[str, int, List[str]]] = []
    rel: Optional[str] = None
    lines = patch.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            if target == "/dev/null":
                rel = None
            else:
                rel = target
        elif line.startswith("@@"):
            if rel is None:
                raise ValueError("a hunk appeared before any target file")
            header = line.split("@@")[1].strip()
            old_part = header.split(" ")[0]
            if not old_part.startswith("-"):
                raise ValueError(f"unreadable hunk header: {line!r}")
            start_text = old_part[1:].split(",")[0].strip()
            if not start_text.isdigit():
                raise ValueError(f"unreadable hunk header: {line!r}")
            body: List[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("@@"):
                text = lines[index]
                if text.startswith("--- ") or text.startswith("+++ "):
                    break
                if text.startswith(("diff ", "index ", "new file", "deleted ")):
                    break
                body.append(text)
                index += 1
            out.append((rel, int(start_text), body))
            continue
        index += 1
    if not out:
        raise ValueError("the patch contains no hunks")
    return out


def _apply_hunks(src: str, start: int, body: List[str]) -> str:
    """Apply one hunk's body to `src`; the hunk must match exactly."""
    source = src.split("\n")
    cursor = max(0, start - 1)
    rebuilt: List[str] = list(source)
    consumed = 0
    produced: List[str] = []
    for text in body:
        if text.startswith("\\"):  # "\ No newline at end of file"
            continue
        tag, value = (text[:1], text[1:]) if text else (" ", "")
        if tag in (" ", "-"):
            if cursor + consumed >= len(rebuilt) or rebuilt[cursor + consumed] != value:
                raise ValueError("hunk context does not match the file")
            consumed += 1
        if tag in (" ", "+"):
            produced.append(value)
    out = rebuilt[:cursor] + produced + rebuilt[cursor + consumed :]
    return "\n".join(out)


def _patch_candidates(
    root: Optional[str], values: Mapping[str, Any]
) -> List[Tuple[str, Optional[str], str]]:
    """Candidate post-images of an `apply_patch`, one per touched file."""
    patch = values.get("patch")
    if not patch:
        return [("", None, "apply_patch requires a patch")]
    try:
        hunks = _parse_unified_patch(str(patch))
    except ValueError as exc:
        return [("", None, f"the patch could not be reconstructed: {exc}")]
    by_file: Dict[str, List[Tuple[int, List[str]]]] = {}
    for rel, start, body in hunks:
        by_file.setdefault(rel, []).append((start, body))
    out: List[Tuple[str, Optional[str], str]] = []
    for rel, file_hunks in by_file.items():
        src, why = _read_source(root, rel)
        if src is None:
            out.append((rel, None, why))
            continue
        candidate = src
        try:
            for start, body in sorted(file_hunks, key=lambda item: -item[0]):
                candidate = _apply_hunks(candidate, start, body)
        except ValueError as exc:
            out.append(
                (rel, None, f"the patch could not be applied for checking: {exc}")
            )
            continue
        out.append((rel, candidate, ""))
    return out


def _rename_candidates(
    root: Optional[str], values: Mapping[str, Any]
) -> List[Tuple[str, Optional[str], str]]:
    """Candidate for a `rename`: the same bytes, under the NEW path.

    A rename changes no content, so what the in-edit check can still say is
    useful: the DESTINATION's extension selects the language, so a file that
    arrived broken - an earlier edit that slipped past a disabled in-edit
    check, a shell redirect, a mutation from a tool that never went through
    the editor - is caught here under the name it will be read by. The loop
    gate remains the backstop for a tree this never sees."""
    rel = str(values.get("destination_path") or "")
    source_rel = str(values.get("source_path") or "")
    if not source_rel:
        return [(rel, None, "rename requires source_path")]
    src, why = _read_source(root, source_rel)
    if src is None:
        return [(rel, None, f"{source_rel}: {why}")]
    return [(rel, src, "")]


def precommit_check_call(
    root: Optional[str],
    tool: str,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Screen ONE catalog mutation's candidate content before it is written.

    AGT-03. Assumes `tool` is a canonical or aliased catalog name and
    `arguments` is the validated argument mapping. `root` is the repository
    the mutation is relative to; `None` is tolerated and degrades every
    candidate to `not_applicable` with a reason, never to a pass.

    This is `precommit_candidates` (one receipt per touched file) FOLDED into
    a single verdict, so a multi-file `apply_patch` cannot hide a broken file
    behind a clean one. The fold is fail-closed: any `failed` candidate makes
    the whole receipt `failed`, and when nothing failed the receipt reports
    the WEAKEST status any candidate had, because "some of it was checked" is
    not the same claim as "all of it was checked".

      passed        every candidate was checked and parses
      failed        at least one candidate does not parse; `refused` is True
                    and the caller must not dispatch the call
      unchecked     a checker exists for the language but could not run here
      disabled      the caller turned the in-edit check off
      not_applicable there is no candidate content to check (a read-only
                    call, a delete/undo, a write the backend will refuse, or
                    arguments this gate cannot reconstruct)

    A caller must not collapse `unchecked` / `disabled` / `not_applicable`
    into `passed`: a silent skip is indistinguishable from a clean check, and
    that is the specific dishonesty this receipt exists to prevent. Every
    per-file receipt is carried under `candidates`, so a reader can see WHAT
    was and was not covered instead of taking the aggregate on trust.
    """
    spec = typed_tool_spec(tool)
    name = spec.name if spec is not None else str(tool or "").strip().lower()
    receipt = _gate_receipt(name)
    if name not in MUTATING_TOOLS:
        receipt["reason"] = f"{name or tool!r} does not change the workspace"
        return receipt
    per_file = precommit_candidates(root, name, dict(arguments or {}), config=config)
    if not per_file:
        receipt["reason"] = (
            f"{name} produces no candidate content to check; the loop gate "
            "remains the backstop"
        )
        return receipt
    receipt["candidates"] = per_file
    failure = next((row for row in per_file if row["refused"]), None)
    if failure is not None:
        receipt.update(
            {
                "path": failure["path"],
                "status": failure["status"],
                "refused": True,
                "checked": True,
                "line": failure["line"],
                "message": failure["message"],
                "context": failure["context"],
                "reason": (
                    f"{failure['path']} does not parse; nothing was written, so "
                    "NO file in this mutation was modified"
                ),
            }
        )
        return receipt
    # Nothing failed: report the weakest status, so a partially-covered
    # mutation never reads as a clean one.
    for status in ("unchecked", "disabled", "not_applicable"):
        weak = next((row for row in per_file if row["status"] == status), None)
        if weak is not None:
            receipt.update(
                {
                    "path": weak["path"],
                    "status": status,
                    "checked": False,
                    "reason": weak["reason"],
                }
            )
            return receipt
    receipt.update(
        {
            "path": per_file[0]["path"],
            "status": "passed",
            "checked": True,
        }
    )
    return receipt


def precommit_candidates(
    root: Optional[str],
    tool: str,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """One receipt per candidate file of a possibly-multi-file mutation.

    The single-file form is `precommit_check_call`; this is the same logic
    without the "exactly one" restriction, for a caller that owns a
    multi-file mutation (`apply_patch`) and wants every touched file screened
    rather than a refusal-to-look."""
    from harness import lint as lint_mod

    spec = typed_tool_spec(tool)
    name = spec.name if spec is not None else str(tool or "").strip().lower()
    values = dict(arguments or {})
    if name in ("edit", "rename_symbol", "update_signature"):
        candidates = _exact_edit_candidate(root, values)
    elif name == "write":
        candidates = _write_candidate(root, values)
    elif name == "apply_patch":
        candidates = _patch_candidates(root, values)
    elif name == "rename":
        candidates = _rename_candidates(root, values)
    else:
        return []
    out: List[Dict[str, Any]] = []
    for rel, candidate, reason in candidates:
        receipt = _gate_receipt(name, rel)
        if candidate is None:
            receipt["reason"] = reason
            out.append(receipt)
            continue
        payload = editor.precommit_check(candidate, rel, config=config).to_dict()
        receipt.update(
            {
                "status": payload["status"],
                "refused": payload["status"] == lint_mod.CHECK_FAILED,
                "checked": payload["checked"],
                "line": payload["line"],
                "message": payload["message"],
                "context": payload["context"],
                "reason": payload["reason"],
                "path": payload["file"] or rel,
            }
        )
        out.append(receipt)
    return out


def _precommit_refusal(tool: str, receipt: Mapping[str, Any]) -> Any:
    """Render a gate refusal as the backend's own result type.

    The gate refuses a mutation, so the caller must receive the same envelope
    the backend would have returned — a value, never an exception, and never a
    partially-applied write. `execution.workspace.ToolResult` is imported
    lazily: `harness.tools` is importable without `execution`, and a caller
    that has a backend necessarily has the module."""
    message = (
        f"the edit to {receipt.get('path') or tool} was refused before it was "
        f"written: {receipt.get('message') or 'it would not parse'}. "
        "NOTHING was written - the file on disk is unchanged. Fix the content "
        "and retry."
    )
    if receipt.get("context"):
        message += "\n" + str(receipt["context"])
    try:
        from execution.workspace import ToolResult
    except Exception:  # pragma: no cover - execution is absent
        return {
            "ok": False,
            "tool": tool,
            "effect": "workspace_write",
            "backend": "precommit_lint",
            "value": dict(receipt),
            "error": message,
        }
    return ToolResult(
        False,
        tool,
        "workspace_write",
        "precommit_lint",
        value=dict(receipt),
        error=message,
    )


def backend_repo_root(backend: Any) -> Optional[str]:
    """Best-effort repository root for a backend object; None when unknown.

    The gate needs a root to read the PRE-IMAGE of an edit (the candidate
    content of a `write` is the argument itself, so a `write` still works
    without one). `SafeToolBackend` exposes `workspace.root`; a caller that
    wraps something else may expose `root` directly. Returning None is honest
    and degrades to `not_applicable` with a reason - the alternative, guessing
    a root, would have the gate checking a file the mutation does not target.
    """
    for holder, attribute in ((backend, "root"), (backend, "workspace")):
        value = getattr(holder, attribute, None)
        if value is None:
            continue
        if attribute == "root":
            return str(value)
        nested = getattr(value, "root", None)
        if nested is not None:
            return str(nested)
    return None


class TypedToolRuntime:
    """Authoritative schema-validating facade over ``SafeToolBackend``."""

    def __init__(
        self, backend: Any, *, config: Optional[Mapping[str, Any]] = None
    ) -> None:
        self.backend = backend
        self.config = config

    def execute(
        self,
        tool: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        sandboxed: bool = False,
    ) -> Any:
        """Validate one catalog call and execute it through the safe backend.

        AGT-03: a MUTATING call is screened by `precommit_check_call` first,
        and a refusal is returned as a value rather than raised — the backend
        is never reached, so a broken mutation is refused BEFORE anything is
        written rather than written and then undone. A non-mutating call skips
        the gate entirely, so this adds no cost to the read path."""
        name, values = validate_typed_arguments(tool, arguments)
        gate = precommit_check_call(
            backend_repo_root(self.backend), name, values, config=self.config
        )
        if gate.get("refused"):
            return _precommit_refusal(name, gate)
        return self.backend.execute(name, values, sandboxed=sandboxed)

    call = execute

    def schemas(self) -> List[Dict[str, Any]]:
        """Return the complete production tool schema list."""
        return typed_tool_schemas()


# --- R2-06: the edit path's shared refusal vocabulary -------------------------
# `harness.editor` is the one module that owns the exact-block edit semantics
# (ambiguity is a refusal, bytes are preserved, a failed post-edit check rolls
# back, writes are atomic), and it is also where the three slugs the kernel
# already speaks were defined. They are RE-EXPORTED here rather than
# re-declared so a `harness.tools` consumer and a `harness.editor` consumer
# cannot drift into two dialects; `tests/test_ceiling_r2_06_editing.py` pins
# them equal to the literals in `harness/agent_kernel/tools.py`, which is the
# third copy and cannot be edited from this file.
EDIT_ERROR_NO_MATCH = editor.ERROR_NO_MATCH
EDIT_ERROR_AMBIGUOUS_MATCH = editor.ERROR_AMBIGUOUS_MATCH
EDIT_ERROR_STALE_READ = editor.ERROR_STALE_READ
EDIT_ERROR_UNDETERMINED_ENCODING = editor.ERROR_UNDETERMINED_ENCODING
EDIT_ERROR_POST_CHECK_FAILED = editor.ERROR_POST_CHECK_FAILED
EDIT_ERROR_EDIT_REFUSED = editor.ERROR_EDIT_REFUSED

#: Schema validation refused the call before it could reach the filesystem. The
#: same word the kernel uses for its own validation errors, kept separate from
#: the editor's literals so this module never has to import the kernel (which
#: imports this module).
EDIT_ERROR_VALIDATION = "validation_error"

#: Marker handed to the catalog validator in place of a digest when the
#: precondition is met by the session rather than by a model-supplied argument.
#: It is deliberately not a hex digest: nothing can mistake it for one, and it
#: is dropped before the value is used - the real precondition is
#: `EditSession.was_read`, enforced by `harness.editor`.
_SESSION_BOUND_REVISION = "session-bound"

#: Every refusal slug the edit path can return.
EDIT_ERROR_KINDS = (*editor.EDIT_ERROR_KINDS, EDIT_ERROR_VALIDATION)


def edit_refusal_vocabulary() -> Dict[str, str]:
    """Return the edit path's refusal slug -> meaning map.

    Assumes nothing. Exists so a consumer (a prompt, a test, a dashboard) can
    enumerate the vocabulary from ONE place instead of hardcoding the words
    `ambiguous_match` / `no_match` / `stale_read` a third time.
    """
    return {
        EDIT_ERROR_AMBIGUOUS_MATCH: "old_string matched more than one place; the "
        "edit was refused and the candidates are listed",
        EDIT_ERROR_NO_MATCH: "old_string was not found verbatim in the file",
        EDIT_ERROR_STALE_READ: "the file was never read in this session, or its "
        "recorded digest no longer matches",
        EDIT_ERROR_UNDETERMINED_ENCODING: "the file's encoding could not be "
        "determined, so the edit was refused rather than guessed",
        EDIT_ERROR_POST_CHECK_FAILED: "the post-edit validation gate failed; the "
        "file was rolled back to its pre-edit bytes",
        EDIT_ERROR_EDIT_REFUSED: "the path or the request itself is not editable",
    }


def apply_catalog_edit(
    root: str,
    arguments: Mapping[str, Any],
    *,
    session: Optional["editor.EditSession"] = None,
    config: Optional[Mapping[str, Any]] = None,
    trace: Optional[Any] = None,
) -> Dict[str, Any]:
    """Apply one catalog ``edit`` call through the editor's edit primitive.

    Assumes `root` is the repository the edit is relative to and `arguments` is
    a mapping in the canonical ``edit`` shape. The catalog requires
    ``expected_revision`` on every mutation (Terminal 02's fail-closed schema
    contract, unchanged here); a model cannot know a digest it never read, so
    when the caller omits it this function BINDS the digest the supplied
    `session` already recorded for that path - the same mechanism
    ``strategy._bind_harness_arguments`` uses on the kernel path. When the
    session has no recorded revision nothing is bound and the editor's own
    ``stale_read`` gate refuses, which is the honest outcome rather than a
    fabricated precondition.

    Returns the JSON-safe `EditOutcome` receipt with an extra ``error_kind`` of
    ``validation_error`` when schema validation fails, which keeps the refusal
    vocabulary single-valued for a caller that has to render one field.
    """
    values_in = dict(arguments or {})
    if not values_in.get("expected_revision"):
        bound = session.revision(str(values_in.get("path") or "")) if session else None
        values_in["expected_revision"] = bound or _SESSION_BOUND_REVISION
    try:
        _, values = validate_typed_arguments("edit", values_in)
    except TypedToolValidationError as exc:
        return {
            "ok": False,
            "path": str((arguments or {}).get("path") or ""),
            "error_kind": EDIT_ERROR_VALIDATION,
            "message": str(exc),
            "rolled_back": False,
        }
    outcome = editor.apply_text_edit(
        root,
        str(values.get("path") or ""),
        str(values.get("old_string") or ""),
        str(values.get("new_string") or ""),
        session=session,
        config=config,
        trace=trace,
    )
    return outcome.to_dict()


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

    VEX-CEILING-07: an optional `cancellation_token` makes a running command
    interruptible. `cancel()` signals the token, which the real sandbox polls
    every 50ms (killing the container) and the local execution handle acts on
    by killing the process tree — so a hard abort terminates a long-running
    child in seconds instead of after the whole `timeout_s`. The token is
    passed through ONLY when the resolved sandbox accepts it, so the
    historical three-positional-arg sandbox and every test double keep
    working unchanged.
    """

    def __init__(
        self,
        repo_path: str,
        timeout_s: int,
        max_output_chars: int,
        cancellation_token: Any = None,
    ) -> None:
        self.repo_path = str(repo_path)
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self.cancellation_token = cancellation_token
        self.cwd: Optional[str] = None  # repo-relative, None = root
        self.commands: List[Dict[str, object]] = []
        self.files_touched: set = set()
        self._cancelled = False
        # VEX-CEILING-07: the STRUCTURED classification of the most recent
        # failing command, so the loop can select a recovery ACTION (a kind to
        # a policy) rather than re-parsing the rendered prose. None after a
        # successful command.
        self.last_error: Optional[ToolError] = None

    # -- cancellation ----------------------------------------------------

    def cancel(self) -> None:
        """Interrupt any in-flight command and refuse the next one.

        Idempotent and cheap: it only signals the token, so a steering abort
        can call it from a watcher thread while the main thread is blocked in
        the sandbox. Never raises — a failed cancel must not mask the abort.
        """
        self._cancelled = True
        token = self.cancellation_token
        if token is None:
            return
        try:
            for name in ("cancel", "set"):
                method = getattr(token, name, None)
                if callable(method):
                    method()
                    return
        except Exception:  # pragma: no cover — defensive
            pass

    @property
    def cancelled(self) -> bool:
        if self._cancelled:
            return True
        token = self.cancellation_token
        checker = getattr(token, "is_cancelled", None) if token is not None else None
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # pragma: no cover — defensive
                return False
        return False

    def set_timeout(self, timeout_s: int) -> None:
        """Raise the per-command budget for subsequent commands.

        Used by the `timeout` recovery policy, which escalates a bounded
        timeout instead of re-running a command that failed on the budget.
        Never lowers the budget: a recovery may only widen the allowance.
        """
        try:
            candidate = int(timeout_s)
        except (TypeError, ValueError):
            return
        if candidate > int(self.timeout_s):
            self.timeout_s = candidate

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
        self.last_error = None
        if result.timed_out or result.exit_code != 0:
            err = classify(
                result.exit_code,
                result.stdout or "",
                result.stderr or "",
                result.timed_out,
                command=command,
            )
            self.last_error = err
            parts.append(render_error(err))
        parts.append(
            f"exit={result.exit_code}" + (" TIMEOUT" if result.timed_out else "")
        )
        if result.stdout:
            parts.append(
                "stdout:\n" + shape_tool_output(result.stdout, self.max_output_chars)
            )
        if result.stderr:
            parts.append(
                "stderr:\n" + shape_tool_output(result.stderr, self.max_output_chars)
            )
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
        if self.cancelled:
            raise ToolExecutionError(
                "cancelled", "the session was cancelled before this command ran"
            )
        full = self._compose(command)
        sandbox = get_execute_sandboxed()
        try:
            result: ExecutionResult = self._invoke(sandbox, full)
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
                "stdout": shape_tool_output(result.stdout or "", self.max_output_chars),
                "stderr": shape_tool_output(result.stderr or "", self.max_output_chars),
                "timed_out": result.timed_out,
            }
        )
        return self._map_result(result, command)

    def _invoke(self, sandbox: Any, full: str) -> Any:
        """Call the sandbox with the cancellation token when it accepts one.

        The Boundary-1 contract is three positional args. Forwarding the token
        is what makes an in-flight command interruptible, but a legacy sandbox
        or test double that does not accept it must keep working — so the
        token is only forwarded after inspecting the signature, never by
        catching a TypeError from a call that may have had side effects.
        """
        token = self.cancellation_token
        if token is None:
            return sandbox(self.repo_path, full, self.timeout_s)
        try:
            import inspect

            params = inspect.signature(sandbox).parameters
        except (TypeError, ValueError):  # pragma: no cover — builtins/C callables
            params = {}
        if "cancellation_token" in params or "cancel_event" in params:
            keyword = (
                "cancellation_token"
                if "cancellation_token" in params
                else "cancel_event"
            )
            return sandbox(self.repo_path, full, self.timeout_s, **{keyword: token})
        if any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        ):
            return sandbox(
                self.repo_path, full, self.timeout_s, cancellation_token=token
            )
        return sandbox(self.repo_path, full, self.timeout_s)


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


@dataclass
class ToolBatchStep:
    """One call's outcome, as the batch reports it.

    `ok` and `output` are the two things every dispatcher already has; the
    `detail` mapping is where a caller puts whatever else it wants auditable
    (a tool name, an exit code, a preserved partial result) without the batch
    having to know about any of it.

    **Redaction boundary decision: redact AT THE JOURNAL, not here.** `output`
    is raw command output with two consumers and opposite trust requirements:
    the model needs it verbatim (redacting first would change what the agent
    can read, and `cap_tool_output` is deliberately a storage bound rather
    than a redaction), and every person-facing surface needs the authority
    because the value leaves the process. The two are separated by the
    boundary, so the redaction lives where the value leaves the process:
    `harness/agent_kernel/events.py::RunEventJournal.append` and
    `harness/trace.py::TraceLogger.log`, both through the one fail-closed
    `harness.redaction.redact_for_journal`. NOT covered here, deliberately: a
    caller that reads `step.output` and prints it without journalling — that is
    a presentation decision and belongs to `cli/ui.py` (T4).
    """

    ok: bool
    output: str = ""
    detail: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        # `field(default_factory=dict)` is not usable here: `field` is a
        # loop variable twice in this module, and shadowing an import for one
        # dataclass is not worth it.
        if self.detail is None:
            self.detail = {}


@dataclass
class ToolBatch:
    """The receipt for one turn's tool batch: what ran, where the seams were.

    `seams` is the number of safe boundaries the batch reached — one after
    every completed call, including the last, so a turn that runs N calls
    offers N delivery points rather than one. `not_executed` names the calls
    a seam declined to let run, which is the difference between "the model
    never asked for it" and "the user cancelled it before it happened".
    """

    calls: Tuple[Any, ...] = ()
    results: Tuple[ToolBatchStep, ...] = ()
    seams: int = 0
    stopped: bool = False
    not_executed: Tuple[Any, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": len(self.calls),
            "executed": len(self.results),
            "seams": self.seams,
            "stopped": self.stopped,
            "not_executed": len(self.not_executed),
            "ok": sum(1 for r in self.results if r.ok),
        }


def run_tool_batch(
    calls: Sequence[Any],
    dispatch: Callable[[Any], ToolBatchStep],
    *,
    seam: Optional[Callable[[ToolBatch], bool]] = None,
) -> ToolBatch:
    """Run one turn's tool calls, exposing the SAFE SEAM between them.

    This is the transport-agnostic half of "queued messages arrive at the
    tool-batch boundary". The rule it exists to make structural rather than
    a convention somebody has to remember:

      * `dispatch(call)` is invoked EXACTLY ONCE per call and is never
        re-entered, split, or interrupted. A mutating call therefore runs to
        completion exactly as it would have with no steering at all.
      * `seam` is called only BETWEEN calls — after `dispatch` has returned,
        before the next one starts. That is the only place a queued message
        can be delivered, and it is the reason delivery can never split a
        mutating call. The seam fires after the LAST call too, which is what
        makes a message typed during the final tool of a turn arrive in that
        turn rather than in the next one.
      * `seam` returning False stops the batch BEFORE the next call and
        records the remaining calls in `not_executed`. Returning True (or
        returning nothing) continues. A seam that raises is treated as
        False: a delivery that failed must not let the rest of the batch run
        as if the correction had arrived.

    `calls` may be empty, in which case the batch reports zero seams and the
    caller is responsible for its own delivery — there is no boundary to
    deliver at, and inventing one would be a lie about where the call ran.
    """
    items = list(calls)
    results: List[ToolBatchStep] = []
    not_executed: List[Any] = []
    seams = 0
    stopped = False
    for index, call in enumerate(items):
        step = dispatch(call)
        if not isinstance(step, ToolBatchStep):
            # A dispatcher that returns a bare pair is a real shape in this
            # repository; wrap it rather than making every caller allocate.
            ok, output = step  # type: ignore[misc]
            step = ToolBatchStep(ok=bool(ok), output=str(output))
        results.append(step)
        if seam is None:
            continue
        seams += 1
        batch = ToolBatch(
            calls=tuple(items),
            results=tuple(results),
            seams=seams,
            stopped=False,
            not_executed=(),
        )
        try:
            proceed = seam(batch)
        except Exception:
            # A seam that raises must not be read as "carry on": the
            # correction it was delivering did not land, and running the
            # remaining mutations after it silently is the failure mode this
            # primitive exists to prevent.
            stopped = True
            not_executed = list(items[index + 1 :])
            break
        if proceed is False:
            stopped = True
            not_executed = list(items[index + 1 :])
            break
    return ToolBatch(
        calls=tuple(items),
        results=tuple(results),
        seams=seams,
        stopped=stopped,
        not_executed=tuple(not_executed),
    )


def is_observation_command(command: str) -> bool:
    """True if a command only reads (used by core to weight step progress)."""
    return bool(_OBSERVE_PAT.match(command.strip()))
