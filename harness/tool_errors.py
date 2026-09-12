"""Structured tool-call error classification (Round 8, Task A).

Problem being fixed: a failing or malformed tool call used to feed the
model a raw exit code + stderr noise (or, worse, a raised exception
ending the step). The model then had to parse shell stderr dialects
(gnu vs busybox, python tracebacks, pytest headers) to figure out what
to DO next.

This module classifies a tool failure into a short, actionable error
type — "file not found: X", "permission denied on protected path",
"malformed patch: hunk doesn't apply at line N" — plus a one-line
action hint, so the model gets a clear signal to act on instead of
noise to parse.

Contract:
- classify(...) NEVER raises; it degrades to kind="internal_error"
  with the original text preserved in `detail`.
- All classes are stable strings; the model-facing format is
  `TOOL ERROR [<kind>]: <detail>` (+ optional `\nSuggested fix: <hint>`),
  rendered by `render_error()`.

Error classes (all covered by regression tests in
tests/test_tool_errors.py):
  file_not_found        — cat/ls/edit of a nonexistent path
  permission_denied    — OS/protected-path permission failure
  command_not_found    — the binary itself doesn't exist
  malformed_patch      — patch/apply says the hunk doesn't apply
  syntax_error         — python SyntaxError in a -c/edit path
  import_error         — ModuleNotFoundError / ImportError
  undefined_name       — NameError from a bad edit
  timeout              — exit 124 / timed_out
  command_rejected     — harness deny-pattern guard (PermissionError)
  internal_error       — unclassifiable; original preserved

Wired in at two points: harness.tools.BashSession._map_result (every
nonzero/timeout command result gets classified before reaching the
model) and harness.core.run_step (PermissionError from the deny guard,
via the structured classifier too). A classified error never changes
step semantics — it is presentation + steering for the model, and the
full raw output still rides the trace (tool_result keeps the raw
string; the classified kind is logged alongside as `error_kind`).
"""

import re
from typing import Dict, NamedTuple, Optional

__all__ = ["ToolError", "classify", "render_error", "classify_exception"]


class ToolError(NamedTuple):
    """One classified tool failure.

    kind: stable class id (see module docstring) — safe to key off.
    detail: the human/model-readable one-liner ("file not found: X").
    hint: optional suggested next action for the model.
    """

    kind: str
    detail: str
    hint: Optional[str] = None


# -- classifier plumbing --------------------------------------------------

_FNF_RE = re.compile(
    r"(?:[Nn]o such file or directory|[Cc]annot find (?:the )?(?:path|file)"
    r"|[Ff]ile (?:or directory )?not found|not found: [^\s]+"
    r"|\[Errno 2\])"
)
_FNF_PATH_RE = re.compile(
    r"(?:[Nn]o such file or directory|[Ff]ile (?:or directory )?not found|"
    r"[Cc]annot find (?:the )?(?:path|file))\s*[:'`\"]?\s*"
    r"['\"`]([^'\"`]+)['\"`]"
)
_PERM_RE = re.compile(
    r"(?:[Pp]ermission denied|[Pp]ermissions?\s+\d+"
    r"|[Aa]ccess (?:is )?denied|EACCES|EPERM|Operation not permitted)",
    re.IGNORECASE,
)
_PERM_PATH_RE = re.compile(
    r"(?:[Pp]ermission denied|EACCES|EPERM)[^\n:]*[:\s]+['\"`]?([^\s'\"`]+)",
)
_PERM_PATH2_RE = re.compile(
    r"denied(?: on| for)?\s+(?:the\s+)?(?:protected\s+)?(?:path\s+)?['\"`]?([^\s'\":]+)",
    re.IGNORECASE,
)
_NOT_FOUND_CMD_RE = re.compile(
    r"^(?:(\S+):\s+(?:command not found|not found)|"
    r"(\S+):(?:\d+):(?:\d+)?:\s*(?:command not found|not found)|"
    r"'(\S+)' is not recognized as an (?:internal or external|executable)"
    r"|(\S+): command not found)",
    re.IGNORECASE | re.MULTILINE,
)
# pytest/cpython styles: "E ModuleNotFoundError: No module named 'x'"
_IMPORT_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[^\n]*?['\"]([^'\"]+)['\"]",
)
_PATCH_RE = re.compile(
    r"(?:[Hh]unk #?\d+ failed|malformed patch|corrupt patch|"
    r"(?:\d+) out of \d+ hunks? FAILED|doesn'?t apply|does not apply|"
    r"Rejected (?:patch|hunk)|misformed patch|failed to apply)",
)
# two shapes: "Hunk #1 FAILED at line 42" (line = group 2) and a bare
# "at line 42" elsewhere in the output (patch/git apply dialects)
_PATCH_LINE_RE = re.compile(r"[Hh]unk #?\d+[^\n]*?at line (\d+)")
_PATCH_LINE2_RE = re.compile(r"at line (\d+)")
# python traceback shape: the 'line N' sits on a PRIOR line
# ('File "x.py", line 3') with SyntaxError on the following line
_SYNTAX_FILE_LINE_RE = re.compile(r'[Ff]ile ["\'][^"\']+["\'], line (\d+)')
_SYNTAX_RE = re.compile(r"(?:SyntaxError|IndentationError|TabError)")
_SYNTAX_LINE_RE = re.compile(
    r"(?:SyntaxError|IndentationError|TabError).*?line (\d+)",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"NameError[^\n]*?'([^']+)'",
)
_TIMEOUT_RE = re.compile(
    r"^\s*(?:Command timed out|TIMED? ?OUT|exit 124)", re.IGNORECASE
)
_ARGPARSE_RE = re.compile(
    r"error: (?:unrecognized arguments?|invalid choice|the following arguments are required)",
    re.IGNORECASE,
)
_USAGE_ERR_RE = re.compile(
    r"^(?:usage:|Usage:)\s*(\S+)",
    re.IGNORECASE | re.MULTILINE,
)


def _m(re_pat: "re.Pattern", text: str) -> Optional[str]:
    """First group of a pattern match, or None."""
    m = re_pat.search(text)
    return m.group(1) if m else None


def _first_token_of(command: str) -> str:
    """Best-effort first word of a shell command, for hints."""
    toks = (command or "").strip().split()
    return toks[0] if toks else ""


def classify(
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    command: str = "",
) -> ToolError:
    """Classify one tool-call failure into a ToolError (never raises).

    Assumes exit_code/stdout/stderr come from an ExecutionResult. A
    SUCCESS (exit 0, no timeout) is not an error — returns kind
    "ok" (render_error treats it as a no-op so callers can call
    classify unconditionally). Precedence is deliberately:
    timeout > command_not_found > file_not_found > malformed_patch >
    syntax_error > import_error > undefined_name > permission_denied >
    argument_error > internal_error — the most ACTIONABLE class wins
    when multiple signals appear (a python -c that hits both a
    NameError and a nonzero exit is an undefined name, not noise).
    """
    text = f"{stdout or ''}\n{stderr or ''}"
    try:
        if timed_out or exit_code == 124:
            return ToolError(
                "timeout",
                f"command timed out after exceeding the command_timeout_s budget",
                hint="narrow the operation (fewer files, tighter pattern) "
                "or split it into smaller commands.",
            )
        if exit_code == 0:
            return ToolError("ok", "")

        cmd = _NOT_FOUND_CMD_RE.search(text)
        if cmd:
            missing = next(g for g in cmd.groups() if g) or _first_token_of(command)
            return ToolError(
                "command_not_found",
                f"command not found: {missing}",
                hint="check the binary name / PATH (or use `which` to "
                "locate it); avoid tools absent from the sandbox image.",
            )

        m = _FNF_RE.search(text)
        if m:
            path = _m(_FNF_PATH_RE, text) or _arg_token(command, text)
            return ToolError(
                "file_not_found",
                f"file not found: {path}" if path else "file not found",
                hint="check the path (pwd/ls) — the file may be "
                "elsewhere, missing, or already moved.",
            )

        m = _PATCH_RE.search(text)
        if m:
            line = _m(_PATCH_LINE_RE, text)
            line2 = _m(_PATCH_LINE2_RE, text)
            at = f" at line {line or line2}" if (line or line2) else ""
            return ToolError(
                "malformed_patch",
                f"malformed patch: hunk doesn't apply{at}",
                hint="the file doesn't match the patch context — re-read "
                "the file and regenerate the patch against its current "
                "content.",
            )

        m = _SYNTAX_RE.search(text)
        if m:
            line = _m(_SYNTAX_LINE_RE, text) or _m(_SYNTAX_FILE_LINE_RE, text)
            at = f" at line {line}" if line else ""
            return ToolError(
                "syntax_error",
                f"syntax error{at} (python could not compile the edited file)",
                hint="re-read the file around the reported line and fix "
                "the indentation/brackets; validate with "
                "`python -m py_compile <file>`.",
            )

        m = _IMPORT_RE.search(text)
        if m:
            mod = m.group(1)
            return ToolError(
                "import_error",
                f"import error: no module named '{mod}'",
                hint="the dependency isn't installed in the sandbox — "
                "avoid importing it, or use the DOCS lookup for its "
                "actual API before relying on it.",
            )

        m = _NAME_RE.search(text)
        if m:
            return ToolError(
                "undefined_name",
                f"undefined name: '{m.group(1)}'",
                hint="the referenced name isn't defined (typo or missing "
                "import) — check the definition site.",
            )

        m = _PERM_RE.search(text)
        if m:
            path = _m(_PERM_PATH_RE, text) or _m(_PERM_PATH2_RE, text)
            return ToolError(
                "permission_denied",
                f"permission denied on path {path}" if path else "permission denied",
                hint="if this is a protected path, the harness will not "
                "allow modifying it; work around it instead.",
            )

        if _ARGPARSE_RE.search(text) or _USAGE_ERR_RE.search(text):
            tool = _m(_USAGE_ERR_RE, text) or _first_token_of(command)
            return ToolError(
                "argument_error",
                f"bad arguments to {tool}",
                hint="re-check the tool's flag/argument spelling; `--help` "
                "lists the accepted form.",
            )

        return ToolError(
            "internal_error",
            (stderr or stdout or "").strip().splitlines()[0][:200]
            if (stderr or stdout or "").strip()
            else f"command failed with exit code {exit_code}",
            hint="inspect the full output above for the failure cause.",
        )
    except Exception as exc:  # classification must never crash a step
        return ToolError("internal_error", f"error classifier failed: {exc}")


def _arg_token(command: str, text: str) -> Optional[str]:
    """Extract the path token most likely the missing one: the last
    non-flag argument of the command, else the token after 'not found'."""
    toks = [t for t in (command or "").split() if not t.startswith("-")]
    if len(toks) > 1:
        return toks[-1]
    m = re.search(r"['\"`]?([^\s'\"`]+)['\"`]?\s*$", text.strip())
    return m.group(1) if m else None


def classify_exception(exc: BaseException, command: str = "") -> ToolError:
    """Classify an exception raised BEFORE the sandbox ran (harness-side
    guards), never raises. Currently covers the deny-pattern
    PermissionError from harness.tools; other exceptions map to
    internal_error with the original message preserved."""
    try:
        if isinstance(exc, PermissionError):
            return ToolError(
                "command_rejected",
                f"permission denied on protected path: harness safety "
                f"guard rejected the command ({command or exc})",
                hint="this command shape is not allowed by the harness "
                "(destructive pattern); use a different approach.",
            )
        return ToolError(
            "internal_error",
            f"{type(exc).__name__}: {exc}",
            hint="unexpected harness-side error; retry with a simpler command.",
        )
    except Exception:  # pragma: no cover — defensive
        return ToolError("internal_error", "unclassifiable exception")


def render_error(err: "ToolError") -> str:
    """Render a ToolError as the model-facing string. kind='ok' renders
    to '' (classify() on a successful result is a no-op)."""
    if err.kind == "ok":
        return ""
    out = f"TOOL ERROR [{err.kind}]: {err.detail}"
    if err.hint:
        out += f"\nSuggested fix: {err.hint}"
    return out


def error_kind_of(result_dict: Dict[str, object]) -> Optional[str]:
    """Helper for the trace: the classified kind of a recorded command
    (commands store {command, exit_code, stdout, stderr, timed_out}), or
    None when it wasn't a failure. Assumes the dict came from
    BashSession.commands entries."""
    try:
        if int(result_dict.get("exit_code", 0)) == 0 and not result_dict.get(
            "timed_out"
        ):
            return None
        err = classify(
            int(result_dict.get("exit_code", 0)),
            str(result_dict.get("stdout", "")),
            str(result_dict.get("stderr", "")),
            bool(result_dict.get("timed_out")),
            str(result_dict.get("command", "")),
        )
        return err.kind
    except Exception:  # pragma: no cover — defensive
        return "internal_error"
