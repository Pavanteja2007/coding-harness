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
via the structured classifier too).

VEX-CEILING-07 (`Recovery, steering, and doom-loop prevention`) turned the
classifier into a RECOVERY POLICY, because classifying an error and then
feeding the model the same prose is presentation, not recovery. The gap
matrix entries this closes:

  G13  error classification changed presentation, not behavior
  G15  one transient model error became FATAL
  G16  tool output was truncated without a shape that preserves the answer
  +    repeated identical commands had no loop protection

The second half of this module is that policy engine:

  - `classify_model_failure` / `ModelRecovery` — a provider failure is
    classified (retryable vs terminal) and retried with BOUNDED BACKOFF.
    429 / 5xx / connection / timeout are retried; auth and bad-request are
    terminal and reported as such. A transient provider failure is never
    converted into a task-ending FATAL. Every attempt emits `model_recovery`.
  - `RecoveryPolicy` — one per task. It maps an error KIND to a specific
    ACTION that changes what the loop does next (narrow the command and
    raise the bounded timeout; attach a directory listing; forbid a path;
    return the exact current file with line numbers; restate a tool schema
    with one example; bounded model fallback; preserve verification evidence
    and replan; stop and ask on a doom loop). It also keeps the forbidden
    path set, the escalated timeout, the repeated-command guard, and the
    mean turns-to-recovery per error kind.
  - `shape_tool_output` — head+tail truncation with an EXPLICIT omission
    marker, and a pytest-shaped bias toward the failure tail so a truncated
    failing test run still carries its assertion.

Every function here is defensive by contract: recovery machinery must never
be the reason a run dies. Read-only evidence builders return an honest
"could not read ..." line instead of raising.

VEX-CEILING-R2-05 (`Baseline failure set and environment triage`) adds the
ENVIRONMENT-versus-CODE question, which the classifier above could not ask.
A missing interpreter, an unresolvable import of a DECLARED dependency, an
unreachable network, an absent Docker daemon, and a permission error on the
repository itself are faults in the MACHINE. Classified as ordinary tool
errors they were handed back to the model as "try another path" advice, so a
run could burn its whole budget editing a working tree that was never broken.
The five classes are now rows in the SAME `POLICY` table (there is no second
policy to drift), `is_repairable_by_edit()` is False for every one of them, and
`RecoveryPolicy.on_tool_error` returns `stop=True` for them — the honest next
action after "the machine is wrong" is to stop and report, not to try harder.
A harness POLICY refusal is explicitly excluded from the permission class, and
an import error is only upgraded to an environment fault when the repository
DECLARES the missing module; an undeclared import stays the code defect it is.
See `classify_environment`, `environment_from_result`, `on_environment_failure`
and `render_environment_report`.
"""

import collections
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from harness.redaction import redact_text_for_journal

__all__ = [
    "ENVIRONMENT_INSTRUCTION",
    "ENVIRONMENT_KINDS",
    "KIND_COMMAND_NOT_FOUND",
    "KIND_COMMAND_REJECTED",
    "KIND_ENV_DOCKER_UNAVAILABLE",
    "KIND_ENV_MISSING_DEPENDENCY",
    "KIND_ENV_MISSING_INTERPRETER",
    "KIND_ENV_REPO_PERMISSION",
    "KIND_ENV_UNREACHABLE_NETWORK",
    "KIND_FILE_NOT_FOUND",
    "KIND_INTERNAL_ERROR",
    "KIND_LOOP_DETECTED",
    "KIND_MALFORMED_PATCH",
    "KIND_MALFORMED_TOOL_CALL",
    "KIND_MODEL_UNAVAILABLE",
    "KIND_PERMISSION_DENIED",
    "KIND_TIMEOUT",
    "KIND_VERIFICATION_FAILED",
    "NON_RETRYABLE_CLASSES",
    "OMISSION_MARKER",
    "OMISSION_RE",
    "POLICY",
    "RETRYABLE_CLASSES",
    "RETRY_CLASSES",
    "RETRY_CLASS_MODEL_ERROR",
    "RETRY_CLASS_POLICY_REFUSAL",
    "RETRY_CLASS_TASK_ERROR",
    "RETRY_CLASS_TERMINAL",
    "RETRY_CLASS_TRANSIENT",
    "ModelFailure",
    "ModelRecovery",
    "RecoveryAction",
    "RecoveryPolicy",
    "Reflection",
    "ReflectionBudget",
    "ToolError",
    "backoff_s",
    "classify",
    "classify_environment",
    "classify_exception",
    "classify_model_failure",
    "command_paths",
    "environment_action",
    "environment_from_result",
    "error_kind_of",
    "is_environment_kind",
    "is_pytest_shaped",
    "is_repairable_by_edit",
    "narrower_command",
    "on_environment_failure",
    "recovery_policy_from_config",
    "reflection_budget_from_config",
    "render_environment_report",
    "render_error",
    "render_reflection",
    "render_schema_recovery",
    "retry_class_of",
    "retry_class_of_model_failure",
    "shape_tool_output",
]


#: ``ToolError`` and ``ModelFailure`` were ``typing.NamedTuple`` subclasses
#: until the redaction boundary below, and Python < 3.13 refuses to let a
#: ``NamedTuple`` body overwrite ``__new__`` (the generated one is what builds
#: the tuple). They are therefore ``collections.namedtuple`` bases with an
#: explicit ``__new__``. Nothing observable changes: both are still tuple
#: subclasses, so indexing, unpacking, equality, ``_replace``, ``_asdict`` and
#: ``isinstance`` all behave exactly as before, and the field names and order
#: are unchanged. Only ``cls.__annotations__`` disappears, which nothing in
#: the tree reads (verified by grep before the change).
def _named_base(name: str, fields: str, defaults: tuple):
    return collections.namedtuple(name, fields, defaults=defaults)


_ToolErrorBase = _named_base("ToolErrorBase", "kind detail hint", ("", None))
_ModelFailureBase = _named_base(
    "ModelFailureBase",
    "kind detail retryable status_code backoff_s terminal",
    (None, 0.0, False),
)


class ToolError(_ToolErrorBase):
    __slots__ = ()

    """One classified tool failure.

    kind: stable class id (see module docstring) - safe to key off.
    detail: the human/model-readable one-liner ("file not found: X").
    hint: optional suggested next action for the model.

    **Redaction boundary (decision: redact AT THE BOUNDARY, here).** `detail`
    is assembled from the failing command, from `str(exc)`, and from regex
    groups lifted out of raw `stdout`/`stderr` - so a shell command with an
    inline credential, or a traceback that echoes an env value, lands in it
    verbatim today. This is the ONE place a `ToolError` comes into existence
    (~30 construction sites in this module and more in `harness/tools.py`), so
    redacting in `__new__` covers every present and future caller instead of
    relying on thirty call sites each remembering.

    Overriding `__new__` rather than adding a factory is deliberate: a factory
    is a convention, and a convention is exactly what the "one authority per
    concern" rule in the doctrine warns about. The value is non-negotiable at
    construction, so a caller cannot reach an unredacted detail by forgetting.

    Consequences a caller should know:
      * Redaction is a no-op on any value `shared.security` does not consider
        secret-shaped, so every existing assertion on an exact `detail` string
        still passes byte-for-byte.
      * `kind` is NOT redacted: the retry policy keys off it and it is drawn
        from a closed vocabulary, never from the failure text.
      * The boundary is fail-closed. If the redactor raises or answers
        `None`, the detail is REPLACED with `(detail withheld: ...)`, never
        passed through raw.
    """

    def __new__(
        cls, kind: str = "", detail: str = "", hint: Optional[str] = None
    ) -> "ToolError":
        return tuple.__new__(
            cls,
            (
                str(kind),
                redact_text_for_journal(detail, where="tool_errors.ToolError.detail"),
                hint,
            ),
        )


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
                "command timed out after exceeding the command_timeout_s budget",
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


# ===========================================================================
# VEX-CEILING-07 — output shaping (G16)
# ===========================================================================

# The ONE omission marker every truncation path in the harness uses. It is
# explicit by contract: a truncated tool result must always say how much was
# dropped, so a model can never mistake a cap for the whole truth.
OMISSION_MARKER = "\n[... {n} chars omitted ...]\n"
OMISSION_RE = re.compile(r"\[\.\.\. (\d+) chars omitted \.\.\.\]")

# Default head/tail split for non-pytest output. Preserved from
# harness.tools.truncate so existing consumers see identical shapes.
DEFAULT_HEAD_RATIO = 0.5
# pytest-shaped output carries the ANSWER at the end (the last failure's
# traceback and the short test summary). Spending half the budget on the
# collection preamble throws the assertion away, so this shape gets a small
# head and a large tail.
PYTEST_HEAD_RATIO = 0.15

_PYTEST_MARKERS = (
    "= FAILURES =",
    "= ERRORS =",
    "= short test summary info =",
    "=== FAILURES ===",
    "--- FAIL:",
    "+++ ERROR",
    "Traceback (most recent call last)",
    "_ test_",
    "assert",
)


_PYTEST_HEAD_CHARS = 4000
_PYTEST_TAIL_CHARS = 4000


def is_pytest_shaped(text: str) -> bool:
    """True when `text` looks like pytest output, so the failure tail wins.

    Deliberately a cheap substring vote on strong, unambiguous markers rather
    than a parser: a false positive only shifts which end of the output is
    preserved, and a false negative costs the last failure block.

    BOTH ends are sampled, and that is load-bearing rather than tidy. A long
    pytest run opens with a collection log that contains no pytest marker at
    all — `collecting...`, then thousands of `collected item N` lines — so a
    head-only check classifies the very output this exists to handle as
    unrecognised and silently falls back to the even split. The failure block
    and the summary are at the END, which is exactly where the tail sample
    looks. Assumes `text` is a str; never raises.
    """
    try:
        if len(text) <= _PYTEST_HEAD_CHARS:
            sample = text
        else:
            sample = text[:_PYTEST_HEAD_CHARS] + "\n" + text[-_PYTEST_TAIL_CHARS:]
        votes = sum(1 for marker in _PYTEST_MARKERS if marker in sample)
        if " in " in sample and ("passed" in sample or "failed" in sample):
            votes += 1
        return votes >= 2
    except Exception:  # pragma: no cover — defensive
        return False


def shape_tool_output(
    text: str,
    limit: int,
    *,
    head_ratio: Optional[float] = None,
) -> str:
    """Head+tail truncation with an explicit omission marker.

    Guarantees, in order of importance:
      1. text shorter than `limit` is returned byte-identical;
      2. a truncated result ALWAYS contains the explicit omission marker and
         the true omitted count — a cap is never silent;
      3. the TAIL is always preserved, so for a failing test run the
         assertion survives (pytest-shaped output additionally biases the
         split toward the tail via PYTEST_HEAD_RATIO);
      4. never raises; a non-str input degrades to str().

    `head_ratio` overrides the shape policy (0.0 = tail only, 0.5 = the
    historical even split). Callers that must keep a caller-chosen shape
    (harness.tools.truncate, which is test-pinned) pass it explicitly.
    """
    try:
        body = text if isinstance(text, str) else str(text)
        cap = int(limit)
        if cap <= 0:
            return ""
        if len(body) <= cap:
            return body
        ratio = (
            float(head_ratio)
            if head_ratio is not None
            else (PYTEST_HEAD_RATIO if is_pytest_shaped(body) else DEFAULT_HEAD_RATIO)
        )
        ratio = min(max(ratio, 0.0), 0.9)
        head = max(int(cap * ratio), 0)
        tail = max(cap - head, 1)
        cut = len(body) - head - tail
        if cut <= 0:  # cap larger than the text — nothing was actually cut
            return body
        return body[:head] + OMISSION_MARKER.format(n=cut) + body[-tail:]
    except Exception:  # pragma: no cover — defensive
        return ""


# ===========================================================================
# VEX-CEILING-07 — command narrowing (the `timeout` policy's first half)
# ===========================================================================

_PYTEST_FAMILY = re.compile(r"(^|\s)(pytest)(\s|$)|-m\s+pytest(\s|$)")


def _has_flag(tokens: Sequence[str], *flags: str) -> bool:
    """True when any token is one of `flags` OR starts with `flag=` .

    The prefix form matters: `--maxfail=2` already bounds the run exactly as
    `-x` does, so a command carrying it must not ALSO get `-x` (the two
    conflict), and a command carrying `-k expr` is already narrowed.
    """
    for token in tokens:
        if token in flags:
            return True
        for flag in flags:
            if token.startswith(flag + "="):
                return True
    return False


def _has_short_flag(tokens: Sequence[str], letter: str) -> bool:
    """True when POSIX short flag `letter` is present, including BUNDLED.

    `grep -rn pattern .` is recursive, and a matcher that only looks for the
    bare token `-r` would classify the most common recursive grep as
    non-recursive and then "narrow" it on a false premise.
    """
    for token in tokens:
        if not token.startswith("-") or token.startswith("--"):
            continue
        if letter in token[1:]:
            return True
    return False


def narrower_command(command: str, *, max_depth: int = 2) -> Optional[str]:
    """Return a strictly SMALLER version of `command`, or None.

    The `timeout` half of the timeout policy: a command that blew its budget
    must not be re-run unchanged. Narrowing is deliberately conservative —
    it only rewrites commands whose scope it can bound with certainty, so a
    returned value is always a genuine subset of the original work:

      * pytest family  -> add `-x` (stop at the first failure) and
                         `--tb=short` (a fraction of the traceback volume),
                         which is exactly the information a repair turn needs.
                         `-v`/`--verbose` is treated as an explicit request for
                         full output and is left alone; `-q` is NOT, because
                         quietness and traceback length are independent.
      * `find`         -> add `-maxdepth N` when absent.
      * `grep`/`rg`    -> add a single-file-type include filter.

    Never raises. Returns None when no safe narrowing exists, and the caller
    then falls back to the "split it into smaller commands" guidance rather
    than inventing a rewrite.
    """
    try:
        raw = (command or "").strip()
        if not raw:
            return None
        tokens = shlex.split(raw, posix=(os.name != "nt"))
        if not tokens:
            return None
        joined = " ".join(tokens)

        if _PYTEST_FAMILY.search(joined):
            additions: List[str] = []
            if not _has_flag(tokens, "-x", "--exitfirst", "--maxfail", "-k"):
                additions.append("-x")
            if not _has_flag(tokens, "--tb", "-v", "-vv", "--verbose"):
                additions.append("--tb=short")
            if not additions:
                return None
            return _join_command(raw, tokens, additions)

        head = tokens[0]
        base = os.path.basename(head)
        if base == "find" and not _has_flag(tokens, "-maxdepth", "-mindepth"):
            return _join_command(raw, tokens, ["-maxdepth", str(int(max_depth))])
        if (
            base == "grep"
            and _has_short_flag(tokens, "r")
            and not any(t.startswith("--include=") or t == "--include" for t in tokens)
        ):
            return _join_command(raw, tokens, ["--include=*.py"])
        if base == "rg" and not any(
            t.startswith("-g") or t == "--glob" for t in tokens
        ):
            return _join_command(raw, tokens, ["-g", "*.py"])
        return None
    except Exception:  # pragma: no cover — defensive
        return None


def _join_command(
    original: str, tokens: Sequence[str], additions: Sequence[str]
) -> str:
    """Append tokens to the command, preferring the original shell text.

    Falls back to a shell-quoted reconstruction when the original cannot be
    tokenized back into the same argv (Windows `&&`/redirect shapes), so the
    caller always receives a runnable command rather than a mangled one.
    """
    tail = ""
    try:
        marker = " ".join(tokens)
        idx = original.rfind(marker)
        if idx >= 0:
            tail = original[idx + len(marker) :]
    except Exception:  # pragma: no cover — defensive
        tail = ""
    extra = " ".join(shlex.quote(item) for item in additions)
    if tail and tail[0] in " \t":
        body = original[: idx + len(marker)] if tail else original
        return f"{body.strip()} {extra}{tail}"
    if not tail:
        return f"{original.strip()} {extra}"
    # The argv did not appear verbatim; reconstruct instead of corrupting.
    rebuilt = " ".join(shlex.quote(item) for item in list(tokens) + list(additions))
    return f"{rebuilt} {tail.strip()}"


# ===========================================================================
# VEX-CEILING-07 — model failure tolerance (G15)
# ===========================================================================

KIND_MODEL_RATE_LIMITED = "model_rate_limited"
KIND_MODEL_UNAVAILABLE = "model_unavailable"
KIND_MODEL_TIMEOUT = "model_timeout"
KIND_MODEL_AUTH = "model_auth"
KIND_MODEL_BAD_REQUEST = "model_bad_request"
KIND_MODEL_INTERNAL = "model_internal"

_RETRYABLE_MODEL_KINDS = frozenset(
    {KIND_MODEL_RATE_LIMITED, KIND_MODEL_UNAVAILABLE, KIND_MODEL_TIMEOUT}
)
_TERMINAL_MODEL_KINDS = frozenset(
    {KIND_MODEL_AUTH, KIND_MODEL_BAD_REQUEST, KIND_MODEL_INTERNAL}
)

_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")

# Ordered most-specific-first: the first hit wins. Provider vocabulary only;
# a harness-internal TypeError must NOT be mistaken for a provider outage.
_STATUS_RULES: Tuple[Tuple[str, int, "re.Pattern[str]"], ...] = (
    (
        KIND_MODEL_RATE_LIMITED,
        429,
        re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I),
    ),
    (
        KIND_MODEL_AUTH,
        401,
        re.compile(
            r"\b401\b|invalid api key|incorrect api key|authentication|unauthorized",
            re.I,
        ),
    ),
    (
        KIND_MODEL_AUTH,
        403,
        re.compile(r"\b403\b|permission denied.*(api key|token)|forbidden", re.I),
    ),
    (
        KIND_MODEL_BAD_REQUEST,
        400,
        re.compile(r"\b400\b|invalid_request_error|bad request", re.I),
    ),
    (
        KIND_MODEL_BAD_REQUEST,
        404,
        re.compile(r"\b404\b|model .*(not found|does not exist)|unknown model", re.I),
    ),
    (KIND_MODEL_TIMEOUT, 0, re.compile(r"timed? ?out|timeout|deadline exceeded", re.I)),
    (KIND_MODEL_UNAVAILABLE, 502, re.compile(r"\b502\b|bad gateway", re.I)),
    (
        KIND_MODEL_UNAVAILABLE,
        503,
        re.compile(r"\b503\b|service unavailable|overloaded|capacity", re.I),
    ),
    (KIND_MODEL_UNAVAILABLE, 504, re.compile(r"\b504\b|gateway timeout", re.I)),
    (KIND_MODEL_UNAVAILABLE, 500, re.compile(r"\b500\b|internal server error", re.I)),
    (
        KIND_MODEL_UNAVAILABLE,
        0,
        re.compile(
            r"connection|connecterror|unreachable|dns|ssl|proxy|server disconnected|"
            r"apierror|service unavailable|eof occurred|remote end closed",
            re.I,
        ),
    ),
)

# Exception CLASS names that mark a provider-side failure even without a
# status code. Matched case-insensitively against the class name.
_PROVIDER_CLASS_HINTS = (
    "ratelimit",
    "authentication",
    "permissiondenied",
    "badrequest",
    "notfound",
    "timeout",
    "apiconnection",
    "serviceunavailable",
    "internalserver",
    "apierror",
    "contentpolicy",
    "contextwindow",
    "serviceunavailable",
    "overloaded",
)
_PROVIDER_CLASS_RE = re.compile("|".join(_PROVIDER_CLASS_HINTS), re.I)


class ModelFailure(_ModelFailureBase):
    __slots__ = ()

    """One classified model/provider failure.

    kind: a stable class id — the retry policy keys off this, never off prose.
    detail: one bounded, human-readable line (never a credential).
    retryable: True when a bounded retry is the correct response. 429, 5xx,
      connection failures and timeouts are retryable; auth, bad-request and
      harness-internal errors are not.
    status_code: the provider status when one was available, else None.
    backoff_s: the bounded delay before the next attempt.
    terminal: True when retrying cannot help (the inverse of retryable, kept
      explicit so callers can branch on intent rather than on a negation).

    **Redaction boundary (decision: redact AT THE BOUNDARY, here).** The
    "never a credential" line in this docstring was, until this round, an
    *unimplemented claim*: `classify_model_failure` builds `detail` from
    `f"{type(exc).__name__}: {exc}"`, and a provider exception very often
    quotes the request it was given — including an `Authorization` header or a
    query-string key. The claim now has an enforcement point. Overriding
    `__new__` covers every construction site in the tree rather than the two in
    this function, and the boundary is fail-closed: an unredactable detail is
    REPLACED with `(detail withheld: ...)`.

    `kind`, `retryable`, `status_code`, `backoff_s` and `terminal` are NOT
    redacted — they are policy values drawn from closed vocabularies, and the
    retry ladder reads them on every provider fault, so they must stay exactly
    what the classifier decided.
    """

    def __new__(
        cls,
        kind: str = "",
        detail: str = "",
        retryable: bool = False,
        status_code: Optional[int] = None,
        backoff_s: float = 0.0,
        terminal: bool = False,
    ) -> "ModelFailure":
        return tuple.__new__(
            cls,
            (
                str(kind),
                redact_text_for_journal(
                    detail, where="tool_errors.ModelFailure.detail"
                ),
                bool(retryable),
                status_code,
                float(backoff_s or 0.0),
                bool(terminal),
            ),
        )


def _status_from_exception(exc: BaseException) -> Optional[int]:
    """Best-effort provider status code, or None. Never raises."""
    for holder in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "code", "status"):
            value = getattr(holder, attr, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and 100 <= value <= 599:
                return value
            if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
                return int(value)
    return None


def backoff_s(
    attempt: int,
    *,
    base_s: float = 0.5,
    cap_s: float = 8.0,
    multiplier: float = 2.0,
) -> float:
    """Bounded exponential backoff for attempt N (1-based).

    Bounded on both ends: the first retry waits `base_s`, the delay doubles
    per attempt, and it NEVER exceeds `cap_s`. A provider outage therefore
    costs a bounded, predictable amount of wall clock instead of an
    unbounded stall. Deterministic (no jitter) so a test can assert it.
    """
    try:
        n = max(1, int(attempt))
        delay = float(base_s) * (float(multiplier) ** (n - 1))
        return round(min(delay, float(cap_s)), 3)
    except Exception:  # pragma: no cover — defensive
        return 0.0


def classify_model_failure(
    exc: BaseException,
    *,
    attempt: int = 1,
    base_backoff_s: float = 0.5,
    cap_backoff_s: float = 8.0,
) -> ModelFailure:
    """Classify a model/provider failure into a retry decision. Never raises.

    Assumes `exc` is the exception raised by a single model call. The split
    that matters: a PROVIDER-side failure (429, 5xx, connection, timeout) is
    retryable, and a TERMINAL failure (auth, bad request) is not retried but
    is also reported as a distinct kind so the run can say WHY it stopped
    instead of collapsing everything into "the model failed". A non-provider
    exception (a harness TypeError) is `model_internal` and terminal —
    retrying a coding bug three times is noise, not recovery.
    """
    try:
        text = f"{type(exc).__name__}: {exc}".strip()
        cls_name = type(exc).__name__
        status = _status_from_exception(exc)
        is_provider = bool(
            status is not None
            or _PROVIDER_CLASS_RE.search(cls_name)
            or re.search(
                r"litellm|openai|anthropic|httpx|aiohttp|requests", cls_name, re.I
            )
        )
        if not is_provider:
            # A HARNESS exception is never a provider outage, however
            # provider-flavoured its message is. Retrying our own bug three
            # times is noise, and labelling it "model_timeout" would blame the
            # provider for a coding error on this side of the boundary.
            return ModelFailure(
                kind=KIND_MODEL_INTERNAL,
                detail=f"{cls_name}: {str(exc)[:200]}" if str(exc) else cls_name,
                retryable=False,
                status_code=status,
                terminal=True,
            )
        kind: Optional[str] = None
        matched_status: Optional[int] = status
        for candidate, code, pattern in _STATUS_RULES:
            if code and status is not None and status == code:
                kind = candidate
                break
            if pattern.search(text):
                kind = candidate
                matched_status = code or status
                break
        if kind is None:
            # An unrecognised PROVIDER failure is assumed transient: a bounded
            # retry is strictly better than killing a healthy run, and the
            # bounded retry budget is what makes that assumption safe.
            kind = KIND_MODEL_UNAVAILABLE
            matched_status = status
        retryable = kind in _RETRYABLE_MODEL_KINDS
        detail = f"{cls_name}: {str(exc)[:200]}" if str(exc) else cls_name
        return ModelFailure(
            kind=kind,
            detail=detail,
            retryable=retryable,
            status_code=matched_status,
            backoff_s=backoff_s(attempt, base_s=base_backoff_s, cap_s=cap_backoff_s)
            if retryable
            else 0.0,
            terminal=not retryable,
        )
    except Exception:  # pragma: no cover — defensive
        return ModelFailure(
            kind=KIND_MODEL_INTERNAL,
            detail="model failure could not be classified",
            retryable=False,
            terminal=True,
        )


class ModelRecovery:
    """Bounded retry for transient model/provider failures.

    One instance per task. `call(fn)` invokes `fn` (a zero-arg model call)
    up to `max_attempts` times, retrying ONLY the retryable classes with the
    bounded backoff above, and emitting a `model_recovery` event for every
    decision (retry or give-up) through the injected `trace.log`-shaped sink.

    The G15 contract: a transient provider failure is NEVER converted into a
    task-ending FATAL, and a terminal one (auth, bad request) is re-raised
    immediately with its kind available to the caller so the run can report
    the real reason. The observable counters are `attempts`, `recoveries`,
    and `last_failure`; the event stream is the durable record.
    """

    def __init__(
        self,
        *,
        trace: Any = None,
        max_attempts: int = 3,
        base_backoff_s: float = 0.5,
        cap_backoff_s: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
        label: str = "",
    ) -> None:
        self.trace = trace
        self.max_attempts = max(1, int(max_attempts))
        self.base_backoff_s = max(0.0, float(base_backoff_s))
        self.cap_backoff_s = max(self.base_backoff_s, float(cap_backoff_s))
        self._sleep = sleep
        self.label = str(label or "")
        self.attempts = 0
        self.recoveries = 0
        self.last_failure: Optional[ModelFailure] = None
        self.failures: List[ModelFailure] = []

    def _emit(self, payload: Dict[str, Any]) -> None:
        if self.trace is None:
            return
        try:
            self.trace.log("model_recovery", dict(payload))
        except Exception:  # pragma: no cover — observability must not kill a run
            pass

    def call(self, fn: Callable[[], Any], *, step: str = "") -> Any:
        """Run `fn`, retrying bounded-transient failures. Re-raises the last
        exception when the failure is terminal or the retry budget is spent.

        Assumes `fn` performs exactly one model call and is safe to repeat
        (model calls are read-only with respect to the workspace). Never
        swallows a non-provider exception silently: it is re-raised with
        `last_failure.kind == "model_internal"` recorded.
        """
        attempt = 0
        while True:
            attempt += 1
            self.attempts += 1
            try:
                return fn()
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                failure = classify_model_failure(
                    exc,
                    attempt=attempt,
                    base_backoff_s=self.base_backoff_s,
                    cap_backoff_s=self.cap_backoff_s,
                )
                self.last_failure = failure
                self.failures.append(failure)
                budget_left = attempt < self.max_attempts
                will_retry = failure.retryable and budget_left
                self._emit(
                    {
                        "label": self.label,
                        "step": step,
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "kind": failure.kind,
                        "detail": failure.detail,
                        "status_code": failure.status_code,
                        "retryable": failure.retryable,
                        "terminal": failure.terminal,
                        "backoff_s": failure.backoff_s,
                        "action": "retry" if will_retry else "give_up",
                    }
                )
                if not will_retry:
                    raise
                self.recoveries += 1
                if failure.backoff_s > 0:
                    try:
                        self._sleep(failure.backoff_s)
                    except Exception:  # pragma: no cover — defensive
                        pass

    def report(self) -> Dict[str, Any]:
        """Machine-readable recovery counters for the run's trace/summary."""
        return {
            "attempts": self.attempts,
            "recoveries": self.recoveries,
            "max_attempts": self.max_attempts,
            "by_kind": _tally(f.kind for f in self.failures),
            "last_kind": self.last_failure.kind if self.last_failure else None,
        }


# ===========================================================================
# VEX-CEILING-07 — the per-kind recovery policy (G13)
# ===========================================================================

KIND_TIMEOUT = "timeout"
KIND_MALFORMED_TOOL_CALL = "malformed_tool_call"
KIND_FILE_NOT_FOUND = "file_not_found"
KIND_PERMISSION_DENIED = "permission_denied"
KIND_MALFORMED_PATCH = "malformed_patch"
KIND_MODEL_UNAVAILABLE = KIND_MODEL_UNAVAILABLE
KIND_VERIFICATION_FAILED = "verification_failed"
KIND_LOOP_DETECTED = "loop_detected"
KIND_COMMAND_NOT_FOUND = "command_not_found"
KIND_COMMAND_REJECTED = "command_rejected"
KIND_INTERNAL_ERROR = "internal_error"

# VEX-CEILING-R2-05 (appended below): environment-vs-code kinds. They live in
# THIS table rather than in a second one, because a second table is a second
# policy and the two would drift. What makes them different is not a different
# table but a different ANSWER: `is_repairable_by_edit()` is False for every one
# of them, so no loop can "fix" a broken machine by editing the repository.
KIND_ENV_MISSING_INTERPRETER = "env_missing_interpreter"
KIND_ENV_MISSING_DEPENDENCY = "env_missing_dependency"
KIND_ENV_UNREACHABLE_NETWORK = "env_unreachable_network"
KIND_ENV_DOCKER_UNAVAILABLE = "env_docker_unavailable"
KIND_ENV_REPO_PERMISSION = "env_repo_permission"

#: The closed, stable environment-class vocabulary. A caller may key off these
#: strings; the ORDER is the reporting order (most specific class first) and is
#: part of the contract so a report reads the same way every time.
ENVIRONMENT_KINDS: Tuple[str, ...] = (
    KIND_ENV_DOCKER_UNAVAILABLE,
    KIND_ENV_MISSING_INTERPRETER,
    KIND_ENV_UNREACHABLE_NETWORK,
    KIND_ENV_MISSING_DEPENDENCY,
    KIND_ENV_REPO_PERMISSION,
)

#: The ONE instruction every environment class shares: the fault is not in the
#: repository, so editing the repository cannot repair it. Kept as a constant
#: rather than five near-copies so the two can never disagree.
ENVIRONMENT_INSTRUCTION = (
    "This is an ENVIRONMENT fault, not a defect in the code. Editing the "
    "repository cannot repair it and further edits only damage a working "
    "tree. Stop, report the classification, and let the operator fix the "
    "machine."
)

# kind -> (action slug, one-line instruction). This table IS the policy: the
# action is what the loop DOES, the instruction is what the model is told.
POLICY: Dict[str, Tuple[str, str]] = {
    KIND_TIMEOUT: (
        "narrow_and_extend",
        "NARROW the command (fewer files, first failure only, bounded depth) "
        "and re-run the smaller slice; the command budget was raised.",
    ),
    KIND_MALFORMED_TOOL_CALL: (
        "restate_schema",
        "The tool call did not match its schema. Re-read the schema below "
        "and emit ONE call in exactly that shape.",
    ),
    KIND_FILE_NOT_FOUND: (
        "attach_listing",
        "The path does not exist. The real directory listing is attached — "
        "use a path from it.",
    ),
    KIND_PERMISSION_DENIED: (
        "forbid_path",
        "That path is refused by the harness/OS. Do NOT retry it; choose a "
        "different location and say so if none works.",
    ),
    KIND_MALFORMED_PATCH: (
        "attach_numbered_file",
        "The patch did not apply. The EXACT current file is attached with "
        "line numbers — regenerate the patch against it.",
    ),
    KIND_MODEL_UNAVAILABLE: (
        "bounded_fallback",
        "The model provider is unavailable. The run continues on the "
        "fallback tier; do not abandon the task.",
    ),
    KIND_VERIFICATION_FAILED: (
        "preserve_and_replan",
        "Verification failed. The evidence is preserved verbatim below; "
        "re-plan against THIS evidence instead of guessing.",
    ),
    KIND_LOOP_DETECTED: (
        "stop_and_ask",
        "The same call was repeated and produced the same result. STOP "
        "repeating it: change the approach or ask the user.",
    ),
    KIND_COMMAND_NOT_FOUND: (
        "avoid_binary",
        "That binary is not in the sandbox image. Do not call it again; "
        "find the installed equivalent.",
    ),
    KIND_COMMAND_REJECTED: (
        "avoid_shape",
        "That command shape is refused by the harness safety guard. Do not "
        "retry it; use a different approach.",
    ),
    KIND_INTERNAL_ERROR: (
        "inspect_output",
        "Inspect the output above for the real cause before retrying.",
    ),
    # -- VEX-CEILING-R2-05: the five environment classes. Each action slug is
    # distinct (pinned by test_every_policy_kind_has_a_distinct_action) and each
    # one ends the turn instead of proposing another edit.
    KIND_ENV_MISSING_INTERPRETER: (
        "report_missing_interpreter",
        "The interpreter this repository needs is not installed or not on "
        "PATH. " + ENVIRONMENT_INSTRUCTION,
    ),
    KIND_ENV_MISSING_DEPENDENCY: (
        "report_missing_dependency",
        "A dependency the repository DECLARES cannot be imported/installed. "
        + ENVIRONMENT_INSTRUCTION,
    ),
    KIND_ENV_UNREACHABLE_NETWORK: (
        "report_network_unreachable",
        "The network is unreachable (or a registry refused the connection). "
        + ENVIRONMENT_INSTRUCTION,
    ),
    KIND_ENV_DOCKER_UNAVAILABLE: (
        "report_docker_unavailable",
        "The Docker daemon is not reachable, so the sandbox cannot run at "
        "all. " + ENVIRONMENT_INSTRUCTION,
    ),
    KIND_ENV_REPO_PERMISSION: (
        "report_repo_permission",
        "The repository itself is not readable/writable for this user. "
        + ENVIRONMENT_INSTRUCTION,
    ),
}

# Path-shaped kinds: the offending path is what the policy acts on.
_PATH_KINDS = frozenset(
    {KIND_FILE_NOT_FOUND, KIND_PERMISSION_DENIED, KIND_MALFORMED_PATCH}
)


class RecoveryAction(NamedTuple):
    """What the loop must DO next for one error kind.

    action: the policy slug from POLICY.
    next_command: a strictly narrower command for the `timeout` policy, else
      None (the caller must not invent one).
    timeout_s: the escalated, bounded per-command budget for the next command.
    forbidden: paths/binary names the loop must stop proposing.
    evidence: bounded, read-only context attached for the model (a directory
      listing, the numbered current file, a restated schema).
    stop: True when the loop must end the turn/ask rather than continue.
    replan: True when the evidence demands a new plan (verification_failed).
    """

    kind: str
    action: str
    instruction: str = ""
    evidence: str = ""
    next_command: Optional[str] = None
    timeout_s: Optional[int] = None
    forbidden: Tuple[str, ...] = ()
    stop: bool = False
    replan: bool = False
    attempts: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "instruction": self.instruction,
            "evidence_chars": len(self.evidence or ""),
            "next_command": self.next_command,
            "timeout_s": self.timeout_s,
            "forbidden": list(self.forbidden),
            "stop": self.stop,
            "replan": self.replan,
            "attempts": self.attempts,
        }


def _tally(values: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/\\]+")
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9_]{1,8}$")
_SKIP_TOKENS = frozenset({"", ".", "..", "-", "--", "/", "\\", "~", "*", "-c", "-m"})

# A `malformed_patch` recovery must return the file the patch APPLIES TO, not
# the patch file. These are the token shapes that are the patch itself (or a
# shell pseudo-path) rather than a target, so the policy skips them.
_PATCH_FILE_RE = re.compile(r"\.(patch|diff|rej|orig)$", re.I)
_PSEUDO_PATH_RE = re.compile(r"^(/dev/|/proc/|/tmp/|\\\\)")

# Loop protection is opt-out for read-only commands, matching the established
# `loop_guard_read_only` semantics of the typed tool catalog. Repeating a MUTATING
# or BUILDING command is the classic doom loop; repeating a read is often a model
# re-checking after an edit, and the existing loop contracts depend on that being
# allowed. Deliberately duplicated rather than imported: harness.tools imports this
# module, so importing back would be a cycle. `python -c` is NOT read-only here
# for the same reason the batch allowlist excludes it — it executes arbitrary code.
_READ_ONLY_PAT = re.compile(
    r"^\s*(?:cat|head|tail|less|more|ls|dir|find|grep|rg|wc|file|stat|pwd|"
    r"which|where|type|printenv|env|diff|git\s+status|git\s+diff|git\s+log|"
    r"git\s+show|git\s+blame|git\s+ls-files|python\s+-m\s+pydoc)\b",
    re.I,
)
_MUTATING_SHAPE_RE = re.compile(
    r"(?:^|\s)(?:rm|mv|cp|sed\s+-i|chmod|chown|mkdir|"
    r"touch|install|ln|truncate|dd|tee|patch|git\s+commit|"
    r"git\s+checkout|git\s+apply)\b",
    re.I,
)


# Shell COMPOSITION is disqualifying on its own, for the same reason the batch
# allowlist rejects it: `cat a.py > b.py` starts with a read verb and ends in
# a write, and no amount of verb-sniffing can see the second half. It is
# checked outside quoted spans so `grep "a|b" f` is not falsely disqualified.
_COMPOSITION_RE = re.compile(r"[>|;&`]|\$\(")
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def is_read_only_command(command: str) -> bool:
    """True when a command only READS, for the loop guard's exemption.

    Conservative in BOTH directions, because a wrong exemption silently lets a
    doom loop through and a wrong refusal kills a legitimate turn:
      * a known read verb; AND
      * no recognised mutating shape; AND
      * no shell composition at all outside quotes -- a redirect, pipe, `&&`,
        `;`, `&`, backtick or `$(` all mean the command is more than a read.

    `python -c` is NOT read-only for the same reason the batch allowlist
    excludes it: it executes arbitrary code, so its writes cannot be seen by
    inspection. A full `pytest` run is not read-only either -- it writes caches
    and runs arbitrary test code, and repeating one is the doom loop this
    policy exists to stop.
    """
    try:
        text = (command or "").strip()
        if not text:
            return False
        if _MUTATING_SHAPE_RE.search(text):
            return False
        if _COMPOSITION_RE.search(_QUOTED_SPAN_RE.sub("", text)):
            return False
        return bool(_READ_ONLY_PAT.match(text))
    except Exception:  # pragma: no cover - defensive
        return False


def command_paths(command: str) -> List[str]:
    """Path-looking tokens of a shell command, de-duplicated, order-preserved.

    Used to decide whether a command touches ONLY paths the policy has already
    forbidden. Heuristic by design (a shell command has no static argument
    grammar); it never raises and an empty result means "cannot tell", which
    the policy treats as permission to run.
    """
    try:
        out: List[str] = []
        for raw in _PATH_TOKEN_RE.findall(command or ""):
            token = raw.strip().strip("\"'")
            if token in _SKIP_TOKENS or token.startswith("-"):
                continue
            if "/" not in token and not _EXTENSION_RE.search(token):
                continue
            if token not in out:
                out.append(token)
        return out
    except Exception:  # pragma: no cover — defensive
        return []


def _path_from_error(err: "ToolError", command: str) -> Optional[str]:
    """The path a path-shaped error is about, best effort."""
    detail = getattr(err, "detail", "") or ""
    match = re.search(r"path ([^\s]+)", detail) or re.search(
        r"file not found: ([^\s]+)", detail
    )
    if match:
        return match.group(1).rstrip(".,;")
    candidates = command_paths(command or "")
    return candidates[0] if candidates else None


def _patch_target(
    err: Any,
    command: str,
    files_touched: Optional[Sequence[str]],
    readable: Callable[[str], bool],
) -> Optional[str]:
    """The file a failed patch applies TO.

    Resolution order, each step a genuine improvement over the last:
      1. a path named in the error detail that is a real file;
      2. a path in the command that is a real file and is NOT the patch
         itself (`.patch`/`.diff`/`.rej`) and not a shell pseudo-path — this
         is the fix for `patch -p1 < fix.patch`, whose first path token is
         the PATCH, not the file being patched;
      3. the most recently file the session itself wrote;
      4. any remaining candidate, so the honest "could not read" evidence is
         still produced rather than silence.
    """
    try:
        detail = str(getattr(err, "detail", "") or "")
        named = re.search(r"(?:file|path|target)\s+([^\s:]+)", detail)
        if named and readable(named.group(1)):
            return named.group(1)
        candidates = [
            token
            for token in command_paths(command or "")
            if not _PATCH_FILE_RE.search(token) and not _PSEUDO_PATH_RE.match(token)
        ]
        for token in candidates:
            if readable(token):
                return token
        if files_touched:
            return list(files_touched)[-1]
        return candidates[0] if candidates else None
    except Exception:  # pragma: no cover — defensive
        return None


class RecoveryPolicy:
    """Stateful, per-task error-kind -> recovery policy.

    Owns everything that must SURVIVE across the turns of one task, because
    the whole point is that the loop's NEXT action differs from what it would
    have done:

      * `timeout`   -> a strictly narrower command AND a larger bounded
                       per-command budget (both capped);
      * `file_not_found`   -> the real directory listing of the parent dir;
      * `permission_denied`-> the path is FORBIDDEN and a command whose every
                       path token is forbidden is refused before it runs;
      * `malformed_patch`  -> the exact current file with line numbers;
      * `malformed_tool_call` -> the schema restated plus ONE example;
      * `model_unavailable` -> bounded fallback tier, never a dead run;
      * `verification_failed` -> the verifier evidence preserved verbatim and
                       an explicit replan signal;
      * a repeated identical command -> loop protection that stops the turn.

    Plus the measured statistic the prompt asks for: `stats()` reports mean
    TURNS-TO-RECOVERY per error kind (turns elapsed between the error and the
    next turn that produced no error of that kind).

    Every method is defensive: recovery machinery must never be the reason a
    run dies. Read-only evidence access is bounded and returns an honest
    "could not read" line rather than raising.
    """

    def __init__(
        self,
        *,
        root: Optional[str] = None,
        base_timeout_s: int = 120,
        max_timeout_s: int = 600,
        timeout_backoff: float = 1.5,
        max_repeat_command: int = 2,
        loop_guard_read_only: bool = False,
        directory_listing_limit: int = 40,
        numbered_file_lines: int = 160,
        numbered_file_max_chars: int = 6000,
        fallback_model: Optional[str] = None,
    ) -> None:
        self.root = str(root) if root else None
        self.base_timeout_s = max(1, int(base_timeout_s))
        self.max_timeout_s = max(self.base_timeout_s, int(max_timeout_s))
        self.timeout_backoff = max(1.0, float(timeout_backoff))
        self.max_repeat_command = max(1, int(max_repeat_command))
        self.loop_guard_read_only = bool(loop_guard_read_only)
        self.directory_listing_limit = max(1, int(directory_listing_limit))
        self.numbered_file_lines = max(1, int(numbered_file_lines))
        self.numbered_file_max_chars = max(200, int(numbered_file_max_chars))
        self.fallback_model = str(fallback_model) if fallback_model else None
        self._forbidden: List[str] = []
        self._timeout_s = self.base_timeout_s
        self._turn = 0
        self._occurrences: Dict[str, int] = {}
        self._recoveries: Dict[str, List[int]] = {}
        self._pending: Dict[str, int] = {}
        self._recent_commands: Dict[str, int] = {}
        self._blocked_command: Optional[str] = None
        self.actions: List[Dict[str, Any]] = []

    # -- turn bookkeeping ------------------------------------------------

    def set_turn(self, turn: int) -> None:
        """Declare the current turn index, closing the recovery window of any
        error kind seen on an EARLIER turn. Called once per loop iteration so
        `mean_turns_to_recovery` is a real measurement, not a guess."""
        try:
            turn = int(turn)
        except (TypeError, ValueError):
            return
        if turn <= self._turn:
            return
        for kind in list(self._pending):
            self._recoveries.setdefault(kind, []).append(turn - self._pending.pop(kind))
        self._turn = turn

    @property
    def turn(self) -> int:
        return self._turn

    # -- timeout escalation ---------------------------------------------

    def next_timeout_s(self) -> int:
        """The per-command budget for the NEXT command (escalated, bounded)."""
        return int(self._timeout_s)

    def _escalate_timeout(self) -> int:
        grown = int(self._timeout_s * self.timeout_backoff)
        self._timeout_s = min(max(grown, self._timeout_s + 1), self.max_timeout_s)
        return int(self._timeout_s)

    # -- forbidden paths -------------------------------------------------

    def forbid(self, target: Optional[str]) -> Optional[str]:
        """Add a path (or a binary name) to the stop-retrying set."""
        cleaned = str(target or "").strip().strip("\"'")
        if not cleaned:
            return None
        if cleaned not in self._forbidden:
            self._forbidden.append(cleaned)
        return cleaned

    @property
    def forbidden(self) -> Tuple[str, ...]:
        return tuple(self._forbidden)

    def rejects(self, command: str) -> Optional[str]:
        """Reason this command must NOT run, or None.

        A command is refused only when EVERY path token it names is already
        forbidden: the policy stops retrying THE path, it does not stop the
        agent from working around it. A command with no recognizable path
        token returns None (cannot tell — permission to run, fail-open on the
        SAFE side only because running an ordinary read is not a hazard).
        """
        try:
            paths = command_paths(command or "")
            if not paths or not self._forbidden:
                return None
            blocked = [p for p in paths if self._match_forbidden(p)]
            if blocked and len(blocked) == len(paths):
                return "every path in this command is already refused: " + ", ".join(
                    sorted(blocked)
                )
            return None
        except Exception:  # pragma: no cover — defensive
            return None

    def _match_forbidden(self, path: str) -> bool:
        try:
            norm = path.replace("\\", "/").lstrip("./")
            for entry in self._forbidden:
                other = entry.replace("\\", "/").lstrip("./")
                if not other:
                    continue
                if (
                    norm == other
                    or norm.endswith("/" + other)
                    or other.endswith("/" + norm)
                ):
                    return True
            return False
        except Exception:  # pragma: no cover — defensive
            return False

    # -- loop protection -------------------------------------------------

    def note_command(self, command: str) -> Optional[RecoveryAction]:
        """Record a command about to run; trip loop protection on a repeat.

        A command is "the same" when its normalized text is identical
        (whitespace-collapsed). After `max_repeat_command` observations of the
        same command the action is returned with `stop=True` and the caller
        ends the turn instead of burning it again. The offending command is
        remembered so the refused call is never dispatched.

        READ-ONLY commands are exempt by default (`loop_guard_read_only=False`),
        matching the typed tool catalog's own `loop_guard_read_only` contract.
        That exemption is load-bearing, not a nicety: a model re-reading a file
        after its own edit is a legitimate pattern the existing loop contracts
        depend on, and a guard that stops it converts working sessions into
        failures. A repeated MUTATING or BUILDING command is the real doom
        loop and is never exempt.
        """
        try:
            norm = " ".join((command or "").split())
            if not norm:
                return None
            if not self.loop_guard_read_only and is_read_only_command(norm):
                return None
            self._recent_commands[norm] = self._recent_commands.get(norm, 0) + 1
            count = self._recent_commands[norm]
            if count <= self.max_repeat_command:
                return None
            self._blocked_command = norm
            self._record(KIND_LOOP_DETECTED)
            action, instruction = POLICY[KIND_LOOP_DETECTED]
            evidence = (
                f"REPEATED CALL #{count}: this exact command has now been "
                f"issued {count} times in this task and returned the same "
                f"result each time. Repeating it cannot produce new evidence."
            )
            return RecoveryAction(
                kind=KIND_LOOP_DETECTED,
                action=action,
                instruction=instruction,
                evidence=evidence,
                forbidden=self.forbidden,
                stop=True,
                attempts=count,
            )
        except Exception:  # pragma: no cover — defensive
            return None

    def release_blocked_command(self) -> Optional[str]:
        """Clear and return the command loop protection refused, if any."""
        blocked, self._blocked_command = self._blocked_command, None
        return blocked

    # -- evidence builders (read-only, bounded, never raise) -------------

    def _resolve(self, path: str) -> Optional[Path]:
        if not path:
            return None
        try:
            candidate = Path(path)
            if not candidate.is_absolute() and self.root:
                candidate = Path(self.root) / path
            return candidate
        except Exception:  # pragma: no cover — defensive
            return None

    def _is_readable(self, path: str) -> bool:
        """True when `path` names a real, readable file. Never raises.

        Used to choose between candidate patch targets: attaching the numbered
        contents of a file that does not exist would be evidence invented for
        the model's benefit, which is exactly what the verifier exists to stop.
        """
        try:
            target = self._resolve(path)
            return bool(target is not None and target.is_file())
        except Exception:  # pragma: no cover — defensive
            return False

    def directory_listing(self, path: str) -> str:
        """A bounded listing of the directory a missing path was looked for in.

        This is the `file_not_found` recovery: the model asked for something
        that does not exist, so show it what DOES exist instead of letting it
        guess. Returns an explicit, honest line when the directory itself
        cannot be read — never a raise, never a fabricated listing.
        """
        try:
            target = self._resolve(path)
            if target is None:
                return "(no directory could be identified for this path)"
            parent = target if target.is_dir() else target.parent
            try:
                entries = sorted(parent.iterdir(), key=lambda p: (p.is_file(), p.name))
            except OSError as exc:
                return f"(could not list {parent}: {exc.__class__.__name__}: {exc})"
            if not entries:
                return f"{parent} is empty."
            lines = [f"Contents of {parent.name or parent}:"]
            for entry in entries[: self.directory_listing_limit]:
                lines.append(f"  {entry.name}{'/' if entry.is_dir() else ''}")
            remaining = len(entries) - self.directory_listing_limit
            if remaining > 0:
                lines.append(f"  ... and {remaining} more entries")
            return "\n".join(lines)
        except Exception as exc:  # pragma: no cover — defensive
            return f"(directory listing unavailable: {exc})"

    def numbered_file(self, path: str) -> str:
        """The EXACT current file with line numbers, bounded.

        This is the `malformed_patch` recovery: a hunk that will not apply is
        a stale-context problem, and the only real fix is to see the current
        bytes. Returns an honest refusal when the file cannot be read rather
        than a guess.
        """
        try:
            target = self._resolve(path)
            if target is None:
                return "(no target file could be identified from the command)"
            try:
                raw = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return (
                    f"(could not read {target}: {exc.__class__.__name__}: {exc}; "
                    "re-read the file before re-applying the patch)"
                )
            lines = raw.splitlines()
            shown = lines[: self.numbered_file_lines]
            width = len(str(len(shown)))
            body = "\n".join(
                f"{str(index).rjust(width)} | {line}"
                for index, line in enumerate(shown, 1)
            )
            if len(lines) > len(shown):
                body += f"\n... and {len(lines) - len(shown)} more lines"
            if len(body) > self.numbered_file_max_chars:
                keep = self.numbered_file_max_chars
                body = (
                    body[: keep // 2]
                    + OMISSION_MARKER.format(n=len(body) - keep)
                    + body[-(keep - keep // 2) :]
                )
            return f"Current contents of {target} ({len(lines)} lines):\n{body}"
        except Exception as exc:  # pragma: no cover — defensive
            return f"(numbered file unavailable: {exc})"

    # -- the policy ------------------------------------------------------

    def on_tool_error(
        self,
        err: Any,
        *,
        command: str = "",
        files_touched: Optional[Sequence[str]] = None,
        tool_schema: Optional[str] = None,
        tool_example: Optional[str] = None,
    ) -> RecoveryAction:
        """Map one tool failure to the ACTION the loop must take.

        Assumes `err` is a ToolError (or any object with `.kind`/`.detail`).
        `command` is the command that produced it; `files_touched` is the
        session's own write history, used to identify a patch target when the
        command does not name one. This is the single decision point for
        requirement 1 of the ceiling prompt, and every kind in POLICY has a
        real behavioral effect, not just new prose.
        """
        kind = str(getattr(err, "kind", "") or KIND_INTERNAL_ERROR)
        action, instruction = POLICY.get(kind, POLICY[KIND_INTERNAL_ERROR])
        self._record(kind)
        evidence = ""
        next_command: Optional[str] = None
        timeout_s: Optional[int] = None
        forbidden: Tuple[str, ...] = ()
        stop = False

        try:
            if kind == KIND_TIMEOUT:
                next_command = narrower_command(command)
                timeout_s = self._escalate_timeout()
                evidence = (
                    f"Per-command budget is now {timeout_s}s (capped at "
                    f"{self.max_timeout_s}s). Suggested narrower command: "
                    f"{next_command}"
                    if next_command
                    else (
                        f"Per-command budget is now {timeout_s}s (capped at "
                        f"{self.max_timeout_s}s). This command has no safe "
                        "automatic narrowing — run a strictly smaller slice."
                    )
                )
            elif kind == KIND_FILE_NOT_FOUND:
                target = _path_from_error(err, command)
                evidence = self.directory_listing(
                    target or (command_paths(command) or [""])[0]
                )
                if target:
                    evidence = f"Requested path: {target}\n{evidence}"
            elif kind == KIND_PERMISSION_DENIED:
                target = _path_from_error(err, command)
                added = self.forbid(target) or self.forbid(
                    (command_paths(command) or [None])[0]
                )
                forbidden = (added,) if added else ()
                evidence = (
                    f"{added} is now on the harness do-not-retry list. Do not "
                    "issue another command whose only paths are refused ones."
                    if added
                    else "This refusal is not path-specific, but do not retry the same command."
                )
            elif kind == KIND_MALFORMED_PATCH:
                target = _patch_target(err, command, files_touched, self._is_readable)
                evidence = self.numbered_file(target or "")
                if target:
                    evidence = f"Patch target: {target}\n{evidence}"
            elif kind == KIND_MALFORMED_TOOL_CALL:
                evidence = render_schema_recovery(
                    str(getattr(err, "detail", "") or ""),
                    schema=tool_schema,
                    example=tool_example,
                )
            elif kind == KIND_MODEL_UNAVAILABLE:
                evidence = (
                    f"Fallback model tier: {self.fallback_model}"
                    if self.fallback_model
                    else "Continuing on the next configured provider/model tier."
                )
            elif kind == KIND_COMMAND_NOT_FOUND:
                binary = _first_token_of(command) or str(getattr(err, "detail", ""))
                added = self.forbid(binary)
                forbidden = (added,) if added else ()
                evidence = f"{added} is not installed in the sandbox image."
            elif kind == KIND_COMMAND_REJECTED:
                evidence = (
                    "The harness deny-guard refused this command shape before "
                    "it ran. Repeating it verbatim is a no-op."
                )
            elif is_environment_kind(kind):
                # R2-05: the loop must STOP here. `stop=True` is the whole
                # point — an environment fault has no repair the model can
                # make, so the only correct next action is to end the turn and
                # let the run's owner report it. Nothing is forbidden and
                # nothing is narrowed, because "try a different path" is
                # advice that cannot be true for a machine that is missing.
                stop = True
                evidence = (
                    f"ENVIRONMENT FAULT [{kind}]: {getattr(err, 'detail', '') or ''}\n"
                    f"repairable by editing this repository: NO"
                )
            else:
                evidence = str(getattr(err, "detail", "") or "")[:500]
        except Exception as exc:  # pragma: no cover — defensive
            evidence = f"(recovery evidence unavailable: {exc})"

        result = RecoveryAction(
            kind=kind,
            action=action,
            instruction=instruction,
            evidence=evidence,
            next_command=next_command,
            timeout_s=timeout_s,
            forbidden=forbidden or self.forbidden,
            stop=stop,
        )
        self.actions.append(result.to_dict())
        return result

    def on_malformed_tool_call(
        self,
        detail: str,
        *,
        tool_schema: Optional[str] = None,
        tool_example: Optional[str] = None,
    ) -> RecoveryAction:
        """The `malformed_tool_call` policy, for the call sites that see a
        malformed call as an exception rather than a classified result."""
        return self.on_tool_error(
            ToolError(KIND_MALFORMED_TOOL_CALL, detail),
            tool_schema=tool_schema,
            tool_example=tool_example,
        )

    def on_verification_failure(
        self, verification: Any = None, *, attempt: int = 1, evidence: str = ""
    ) -> RecoveryAction:
        """The `verification_failed` policy: PRESERVE the evidence, REPLAN.

        Preserving means: the verifier's structured feedback and the shaped
        tail of its raw output are carried into the action verbatim (bounded),
        so the next plan is built against the evidence that actually failed
        rather than a re-derivation of it. `replan=True` is the explicit
        signal the loop uses to re-plan instead of retrying the same change.
        """
        self._record(KIND_VERIFICATION_FAILED)
        kept = self._verification_evidence(verification) or str(evidence or "")[:4000]
        action, instruction = POLICY[KIND_VERIFICATION_FAILED]
        result = RecoveryAction(
            kind=KIND_VERIFICATION_FAILED,
            action=action,
            instruction=instruction,
            evidence=kept,
            forbidden=self.forbidden,
            replan=True,
            attempts=int(attempt),
        )
        self.actions.append(result.to_dict())
        return result

    def _verification_evidence(self, verification: Any) -> str:
        try:
            if verification is None:
                return ""
            parts: List[str] = []
            structured = getattr(verification, "structured_feedback", None) or []
            for item in structured[:5]:
                if isinstance(item, dict):
                    parts.append(
                        "- "
                        + "; ".join(
                            f"{key}={value}"
                            for key, value in list(item.items())[:6]
                            if key
                            in {"kind", "file", "line", "test", "message", "detail"}
                        )
                    )
            raw = str(getattr(verification, "raw_output", "") or "")
            if raw:
                parts.append(shape_tool_output(raw, 2000, head_ratio=PYTEST_HEAD_RATIO))
            return "\n".join(parts)
        except Exception:  # pragma: no cover — defensive
            return ""

    # -- statistics ------------------------------------------------------

    def _record(self, kind: str) -> None:
        self._occurrences[kind] = self._occurrences.get(kind, 0) + 1
        self._pending.setdefault(kind, self._turn)

    def stats(self) -> Dict[str, Any]:
        """Recovery statistics, including MEAN TURNS-TO-RECOVERY per kind.

        An error still pending at report time is NOT counted as recovered —
        an unrecovered error must never be reported as a fast recovery.
        """
        per_kind: Dict[str, Dict[str, Any]] = {}
        total_turns = 0
        for kind, count in sorted(self._occurrences.items()):
            waits = list(self._recoveries.get(kind, []))
            mean = round(sum(waits) / len(waits), 3) if waits else None
            per_kind[kind] = {
                "occurrences": count,
                "recoveries": len(waits),
                "pending": kind in self._pending,
                "mean_turns_to_recovery": mean,
            }
            total_turns += sum(waits)
        total_recoveries = sum(len(v) for v in self._recoveries.values())
        return {
            "by_kind": per_kind,
            "total_occurrences": sum(self._occurrences.values()),
            "total_recoveries": total_recoveries,
            "mean_turns_to_recovery": (
                round(total_turns / total_recoveries, 3) if total_recoveries else None
            ),
            "forbidden_paths": list(self._forbidden),
            "final_timeout_s": int(self._timeout_s),
            "actions": len(self.actions),
        }

    # -- model-facing rendering -----------------------------------------

    def feedback(self, action: RecoveryAction) -> str:
        """Render the action as the model-facing recovery message.

        Structured as a recovery block (kind, what the harness will now do,
        the evidence, the instruction) so the model can act on it in one
        read instead of parsing prose dialects.
        """
        try:
            lines = [f"RECOVERY [{action.kind}] -> {action.action}"]
            if action.timeout_s:
                lines.append(f"Next command budget: {action.timeout_s}s")
            if action.next_command:
                lines.append(f"Run this instead: {action.next_command}")
            if action.forbidden:
                lines.append("Do not retry: " + ", ".join(action.forbidden[:8]))
            if action.evidence:
                lines.append(action.evidence)
            if action.instruction:
                lines.append(action.instruction)
            return "\n".join(lines)
        except Exception:  # pragma: no cover — defensive
            return ""


def render_schema_recovery(
    detail: str,
    *,
    schema: Optional[str] = None,
    example: Optional[str] = None,
) -> str:
    """The `malformed_tool_call` policy payload: the schema RESTATED plus ONE
    example call. A single worked example is what actually fixes a malformed
    call; restating the error alone does not."""
    parts: List[str] = []
    if detail:
        parts.append(f"Rejected: {str(detail)[:400]}")
    if schema:
        parts.append(f"Required schema:\n{str(schema)[:2000]}")
    if example:
        parts.append(f"Example of a valid call:\n{str(example)[:800]}")
    return "\n".join(parts)


def recovery_policy_from_config(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    root: Optional[str] = None,
) -> RecoveryPolicy:
    """Build a RecoveryPolicy from `Task.config` (config-driven per project
    convention: no recovery constant is hardcoded in a loop).

    Recognized keys (all optional, all additive):
      `command_timeout_s` (the base budget), `recovery_max_timeout_s`,
      `recovery_timeout_backoff`, `recovery_max_repeat_command`,
      `recovery_listing_limit`, `recovery_numbered_file_lines`,
      `recovery_numbered_file_chars`, `recovery_fallback_model`.
    """
    conf = dict(cfg or {})
    return RecoveryPolicy(
        root=root,
        base_timeout_s=int(conf.get("command_timeout_s", 120) or 120),
        max_timeout_s=int(conf.get("recovery_max_timeout_s", 600) or 600),
        timeout_backoff=float(conf.get("recovery_timeout_backoff", 1.5) or 1.5),
        max_repeat_command=int(conf.get("recovery_max_repeat_command", 2) or 2),
        loop_guard_read_only=bool(
            conf.get(
                "recovery_loop_guard_read_only",
                conf.get("loop_guard_read_only", False),
            )
        ),
        directory_listing_limit=int(conf.get("recovery_listing_limit", 40) or 40),
        numbered_file_lines=int(conf.get("recovery_numbered_file_lines", 160) or 160),
        numbered_file_max_chars=int(
            conf.get("recovery_numbered_file_chars", 6000) or 6000
        ),
        fallback_model=conf.get("recovery_fallback_model")
        or conf.get("fallback_model"),
    )


# ===========================================================================
# VEX-CEILING-R2-05 — environment-vs-code triage (appended section)
# ===========================================================================
#
# A missing interpreter, an unresolvable import of a DECLARED dependency, an
# unreachable network, an absent Docker daemon, and a permission error on the
# repository itself are all the same thing to an agent loop: the failure is in
# the MACHINE, not in the code. Before this section the loop classified each of
# them as an ordinary tool error and handed the model an action — narrow the
# command, pick another path, retry — so a run could burn its entire budget
# "repairing" a machine by editing a working tree until the evidence for what
# was there before was gone.
#
# The rules this section implements, in order of importance:
#
#   1. ONE table. The five environment classes are rows in the SAME `POLICY`
#      table as every other kind, so there is no second policy to drift. What
#      distinguishes them is `is_repairable_by_edit()` returning False.
#   2. A NAMED, tested, closed vocabulary (`ENVIRONMENT_KINDS`), not prose.
#   3. DELEGATE where the existing classifier already covers the class. An
#      unresolvable import is already `import_error`; this section only
#      UPGRADES it to an environment class when the module is one the
#      repository DECLARES. An undeclared import stays a code defect, because
#      it is one.
#   4. A harness POLICY refusal is never an environment fault. `classify()`
#      maps the deny guard's PermissionError to `command_rejected`; without an
#      explicit exclusion, the shared "Permission denied" wording would drag
#      every safety refusal into `env_repo_permission`.
#
# No completion status and no verifier is named anywhere in this section, by
# design: environment triage decides what the loop does NEXT, never what counts
# as success (pinned by test_the_verifier_contract_is_untouched_by_recovery).

# Interpreters a repository's own test command is allowed to need. A missing
# one of these is a missing interpreter; a missing one of anything else is an
# ordinary `command_not_found` and the caller may legitimately use the
# installed equivalent.
_INTERPRETER_NAMES: Tuple[str, ...] = (
    "python",
    "python3",
    "python3.10",
    "python3.11",
    "python3.12",
    "node",
    "npm",
    "npx",
    "pytest",
    "pip",
    "pip3",
    "poetry",
    "uv",
    "go",
    "cargo",
    "java",
    "dotnet",
    "ruby",
    "bundle",
)

# Markers that prove the loop is talking to a PACKAGE REGISTRY or the wider
# network, rather than to a local machine fault. Checked before the
# declared-dependency rule so an install that failed *because the network was
# down* is attributed to the network — the root cause, not its symptom.
_NETWORK_RE = re.compile(
    r"(?:Temporary failure in name resolution"
    r"|Name or service not known"
    r"|nodename nor servname provided"
    r"|No address associated with hostname"
    r"|Network is unreachable"
    r"|No route to host"
    r"|Failed to establish a new connection"
    r"|Max retries exceeded with url"
    r"|Connection refused"
    r"|Connection reset by peer"
    r"|Connection aborted"
    r"|Could not resolve host"
    r"|Temporary failure in name lookup"
    r"|getaddrinfo"
    r"|EAI_AGAIN"
    r"|ENETUNREACH"
    r"|EHOSTUNREACH"
    r"|ETIMEDOUT"
    r"|Errno 110"
    r"|Errno 101"
    r"|SSLError"
    r"|ProxyError"
    r"|CERTIFICATE_VERIFY_FAILED)",
    re.IGNORECASE,
)

# An absent daemon is the one class where the harness KNOWS rather than infers:
# the sandbox layer raises by name instead of falling back, so the class name is
# the evidence. Matched by name, never imported, so this module keeps its
# zero-boundary-dependency property.
_DOCKER_UNAVAILABLE_RE = re.compile(
    r"(?:SandboxUnavailableError"
    r"|Cannot connect to the Docker daemon"
    r"|docker:\s*Cannot connect"
    r"|error during connect"
    r"|docker(?:'|\")? (?:daemon )?is not running"
    r"|Docker daemon"
    r"|error while trying to connect to the Docker Engine)",
    re.IGNORECASE,
)

# A declared dependency that could not be INSTALLED (as opposed to imported).
# Named separately from the import rule because the remediation is identical
# and the evidence is a different line of the same broken environment.
_DEPENDENCY_RESOLUTION_RE = re.compile(
    r"(?:No matching distribution found"
    r"|Could not find a version that satisfies the requirement"
    r"|ERROR: Could not find a version"
    r"|No such package"
    r"|PackageNotFoundError"
    r"|error: could not find `[^`]+` in crates\.io index)",
    re.IGNORECASE,
)

# Read-only filesystems and OS-level refusals. Matched only for a path that is
# inside the repository, because a read-only `/usr` is not this run's problem.
_REPO_PERMISSION_RE = re.compile(
    r"(?:Read-only file system"
    r"|\[Errno 30\]"
    r"|\[Errno 13\]"
    r"|\[Errno 1\] Operation not permitted"
    r"|EACCES"
    r"|EPERM"
    r"|EROFS)",
    re.IGNORECASE,
)

# An explicit exclusion list. Every string here marks a HARNESS POLICY refusal
# whose wording overlaps the OS vocabulary; treating one as an environment
# fault would report "fix the machine" for a refusal the harness made on
# purpose, which is worse than a missed classification.
_HARNESS_GUARD_RE = re.compile(
    r"(?:harness safety guard"
    r"|harness deny-guard"
    r"|deny-guard"
    r"|harness will not allow"
    r"|refused by the harness"
    r"|protected path"
    r"|safety guard rejected)",
    re.IGNORECASE,
)


def is_environment_kind(kind: str) -> bool:
    """True when `kind` is one of the five environment classes.

    Assumes `kind` is a stable classifier string. This is the predicate a
    loop asks before it proposes ANOTHER edit: an environment class is not
    repairable by editing the repository, so the honest next action is to stop.
    """
    return str(kind or "") in ENVIRONMENT_KINDS


def is_repairable_by_edit(kind: str) -> bool:
    """True when a fix for `kind` could plausibly live in the repository.

    The inverse of :func:`is_environment_kind` for the kinds this module
    knows, and True for anything unrecognised: an unknown kind must not be
    treated as an environment fault, because ending a run on a guess is worse
    than continuing one.
    """
    return not is_environment_kind(kind)


def environment_action(kind: str) -> Optional[str]:
    """The POLICY action slug for an environment `kind`, or None if it is not one.

    Assumes `kind` came from :func:`classify_environment` or
    :func:`is_environment_kind`. Returns the slug the loop would take so a
    receipt can name the response that was selected, not just the class.
    """
    if not is_environment_kind(kind):
        return None
    return POLICY.get(kind, ("", ""))[0] or None


def _module_root(name: str) -> str:
    """Top-level import name for a possibly-dotted module path.

    ``pkg.sub.mod`` -> ``pkg``. An empty or unparseable value returns "" so it
    can never accidentally match a declared dependency.
    """
    try:
        text = str(name or "").strip().strip(".").replace("\\", "/")
        if not text:
            return ""
        return text.split("/", 1)[0].split("::", 1)[0].split(".", 1)[0]
    except Exception:  # pragma: no cover — defensive
        return ""


def _normalize_dependencies(declared: Any) -> frozenset:
    """Return the set of top-level import names a repository DECLARES.

    Accepts anything iterable of strings plus a few common spellings, because
    the caller (a manifest reader) is the only thing that knows the format.
    Distribution names are lower-cased and split on `-`/`_`/`.` so
    ``zope.interface``, ``zope_interface`` and ``Zope-Interface`` all collapse
    onto one key — the import name and the distribution name are not the same
    string and a naive comparison would miss every renamed distribution.
    """
    out = set()
    try:
        source = declared if declared is not None else ()
        if isinstance(source, str):
            source = re.split(r"[,\s]+", source)
        for item in source:
            if isinstance(item, (list, tuple, set)):
                for sub in item:
                    out.update(_normalize_dependencies([sub]))
                continue
            raw = str(item or "").strip()
            if not raw:
                continue
            head = re.split(r"[<>=!~;\[\s]", raw, maxsplit=1)[0]
            for part in re.split(r"[-_.]+", head):
                token = part.strip().lower()
                if token and token != head.strip().lower():
                    out.add(token)
            token = _module_root(head).lower()
            if token:
                out.add(token)
    except Exception:  # pragma: no cover — defensive
        return frozenset()
    return frozenset(out)


def _missing_interpreter(text: str, command: str) -> Optional[str]:
    """Return the interpreter name a "not found" shape names, or None.

    Two shapes are accepted, because two runners produce them: the shell's
    ``python3: command not found`` and pip's ``No such file or directory:
    'python3'``. Anything whose missing binary is NOT a known interpreter
    returns None so `command_not_found` keeps owning it.
    """
    try:
        missing = ""
        match = _NOT_FOUND_CMD_RE.search(text or "")
        if match:
            missing = next((g for g in match.groups() if g), "") or ""
        if not missing:
            found = re.search(
                r"No such file or directory:\s*['\"]([^'\"]+)['\"]", text or ""
            )
            if found:
                missing = found.group(1)
        if not missing and command:
            head = _first_token_of(command).strip("'\"")
            if re.search(r"(?:not found|command not found)", text or "", re.IGNORECASE):
                missing = head
        if not missing:
            return None
        base = os.path.basename(str(missing).strip().strip("'\""))
        if base.lower() in _INTERPRETER_NAMES:
            return base
        return None
    except Exception:  # pragma: no cover — defensive
        return None


def _network_failure(text: str) -> bool:
    """True when the capture proves an unreachable network."""
    return bool(text) and bool(_NETWORK_RE.search(text))


def _docker_unavailable(text: str, exc: Optional[BaseException]) -> bool:
    """True when the evidence names an absent/unreachable Docker daemon.

    Checks the exception's CLASS NAME as well as the text, because the sandbox
    layer reports an absent daemon by raising a named error whose message is
    host-specific ("the system cannot find the file specified" on Windows).
    """
    try:
        if exc is not None and type(exc).__name__ == "SandboxUnavailableError":
            return True
        return bool(text) and bool(_DOCKER_UNAVAILABLE_RE.search(text))
    except Exception:  # pragma: no cover — defensive
        return False


def _import_module(text: str) -> Optional[str]:
    """Return the module name an import error names, via the shared pattern.

    Delegates to the SAME `_IMPORT_RE` the base `classify()` uses, which is
    the point: there is one import-error shape in this module, not two.
    """
    return _m(_IMPORT_RE, text or "")


def _dependency_is_declared(module: Optional[str], declared: frozenset) -> bool:
    """True when the missing module is one the repository DECLARES.

    An undeclared import that fails is a CODE defect (the repository imports
    something it never asked for) and must stay a repairable finding, so this
    returns False for it and the caller reports nothing.
    """
    if not module or not declared:
        return False
    root = _module_root(module).lower()
    if not root:
        return False
    return root in declared


def classify_environment(
    exc: Optional[BaseException] = None,
    *,
    output: str = "",
    exit_code: Optional[int] = None,
    timed_out: bool = False,
    command: str = "",
    declared_dependencies: Any = None,
    repo_path: Optional[str] = None,
) -> Optional[ToolError]:
    """Classify a failure as ENVIRONMENT (machine) or not (code). Never raises.

    Returns a :class:`ToolError` whose ``kind`` is one of
    :data:`ENVIRONMENT_KINDS`, or ``None`` when the failure is not
    environment-class — which is the honest answer for the overwhelming
    majority of failures, and the reason this cannot be a boolean.

    Assumes ``output`` is the combined stdout+stderr of the run, ``exc`` an
    exception raised before or around it (either may be None), and
    ``declared_dependencies`` the repository's declared distribution names as
    any iterable of strings. An EMPTY ``declared_dependencies`` is not a
    wildcard: without the manifest evidence this function cannot claim a
    dependency is declared, so an import error stays an ordinary code-class
    `import_error` and is reported by :func:`classify` instead. That is
    deliberate — guessing "environment" from a bare ModuleNotFoundError is how
    a genuine code bug gets excused.

    Precedence is root-cause-first and each step is a superset of the symptom
    below it: docker (the sandbox cannot run at all) -> interpreter (nothing
    can run) -> network (the cause of most install failures) -> declared
    dependency -> repository permission. A harness policy refusal is excluded
    FIRST, so "permission denied on a protected path" is never mistaken for a
    read-only repository.
    """
    try:
        text = f"{output or ''}"
        if exc is not None:
            text = f"{text}\n{exc}"
        if _HARNESS_GUARD_RE.search(text):
            # A refusal the harness made deliberately is policy, not a machine.
            return None
        if _docker_unavailable(text, exc):
            return ToolError(
                KIND_ENV_DOCKER_UNAVAILABLE,
                "the Docker daemon is not reachable, so no sandboxed command "
                "can run at all",
                hint="start/restart Docker Desktop (or the daemon) and re-run; "
                "the repository is not the problem.",
            )
        missing_interpreter = _missing_interpreter(text, command)
        if missing_interpreter:
            return ToolError(
                KIND_ENV_MISSING_INTERPRETER,
                f"the interpreter this repository needs is missing: "
                f"{missing_interpreter}",
                hint=f"install/provide {missing_interpreter} in the image the "
                "sandbox runs, or point the test command at the interpreter "
                "that is present.",
            )
        if _network_failure(text):
            return ToolError(
                KIND_ENV_UNREACHABLE_NETWORK,
                "the network is unreachable, so a required fetch/install or "
                "test connection cannot complete",
                hint="restore network egress (or allow the registry host) and "
                "re-run; retrying the same command cannot succeed offline.",
            )
        declared = _normalize_dependencies(declared_dependencies)
        if _DEPENDENCY_RESOLUTION_RE.search(text):
            return ToolError(
                KIND_ENV_MISSING_DEPENDENCY,
                "a declared dependency could not be resolved by the installer",
                hint="check the version constraints in the manifest and the "
                "index that serves them; the repository code is not the fault.",
            )
        module = _import_module(text)
        if module and _dependency_is_declared(module, declared):
            return ToolError(
                KIND_ENV_MISSING_DEPENDENCY,
                f"import error: no module named '{module}' - and "
                f"'{_module_root(module)}' IS a declared dependency, so this "
                f"is an uninstalled/broken environment, not a code defect",
                hint="install the declared dependency into the test image and "
                "re-run; do NOT edit the repository to work around a "
                "dependency the project already declares.",
            )
        if _REPO_PERMISSION_RE.search(text) and repo_path:
            # A read-only /usr is not this run's problem; the path must name
            # the repository for this to be a repository-permission fault.
            normalized_repo = str(repo_path).replace("\\", "/").rstrip("/")
            if any(
                normalized_repo in line.replace("\\", "/") for line in text.splitlines()
            ):
                return ToolError(
                    KIND_ENV_REPO_PERMISSION,
                    "the repository itself is not readable/writable for this user",
                    hint="fix the filesystem permissions or the mount for the "
                    "repository; editing files inside it is impossible.",
                )
        return None
    except Exception:  # classification must never crash a run
        return None


def environment_from_result(
    result: Any,
    *,
    command: str = "",
    declared_dependencies: Any = None,
    repo_path: Optional[str] = None,
) -> Optional[ToolError]:
    """:func:`classify_environment` for one ``ExecutionResult``-shaped object.

    Assumes ``result`` carries ``exit_code``/``stdout``/``stderr``/
    ``timed_out`` (duck typed, so a test double works). A SUCCESS is never an
    environment fault and returns None without looking at the text, so a
    passing command that happens to print "Network is unreachable" cannot
    manufacture a fault.
    """
    try:
        if result is None:
            return None
        exit_code = getattr(result, "exit_code", None)
        timed_out = bool(getattr(result, "timed_out", False))
        if (exit_code in (0, None) and not timed_out) and not _HARNESS_GUARD_RE.search(
            f"{getattr(result, 'stdout', '') or ''}{getattr(result, 'stderr', '') or ''}"
        ):
            return None
        return classify_environment(
            output=f"{getattr(result, 'stdout', '') or ''}\n"
            f"{getattr(result, 'stderr', '') or ''}",
            exit_code=exit_code,
            timed_out=timed_out,
            command=command,
            declared_dependencies=declared_dependencies,
            repo_path=repo_path,
        )
    except Exception:  # pragma: no cover — defensive
        return None


def on_environment_failure(
    policy: "RecoveryPolicy",
    err: Any,
    *,
    command: str = "",
) -> RecoveryAction:
    """The environment-class recovery action: STOP, and say it is the machine.

    A thin, explicit entry point over :meth:`RecoveryPolicy.on_tool_error` for
    the call sites that hold a classified environment error and want the
    non-repairable answer without restating the mapping. Assumes ``policy`` is
    a :class:`RecoveryPolicy` and ``err`` is (or duck-types) a ToolError whose
    kind is one of :data:`ENVIRONMENT_KINDS`; a non-environment kind is
    delegated unchanged rather than being mislabelled.

    The returned action has ``stop=True`` and ``next_command=None``: there is
    no narrower command that fixes a missing daemon, and inventing one is how a
    loop talks itself into a repair loop.
    """
    try:
        kind = str(getattr(err, "kind", "") or "")
        if not is_environment_kind(kind):
            return policy.on_tool_error(err, command=command)
        action = policy.on_tool_error(err, command=command)
        if not action.stop or action.next_command is not None:
            action = action._replace(stop=True, next_command=None)
        return action
    except Exception:  # pragma: no cover — defensive
        return RecoveryAction(
            kind=KIND_INTERNAL_ERROR,
            action="inspect_output",
            instruction="inspect the output above for the real cause.",
            stop=True,
        )


def render_environment_report(
    err: Any,
    *,
    declared_dependencies: Any = None,
) -> str:
    """Render an environment classification as the operator-facing report.

    Assumes ``err`` is the ToolError :func:`classify_environment` returned.
    The shape is deliberately fixed — class, why it is not the code, what would
    repair it, and what must NOT be done — because this text is the terminal
    state of a run and the next reader should not have to infer the argument.
    """
    try:
        kind = str(getattr(err, "kind", "") or KIND_INTERNAL_ERROR)
        detail = str(getattr(err, "detail", "") or "")
        hint = str(getattr(err, "hint", "") or "")
        declared = sorted(_normalize_dependencies(declared_dependencies))
        lines = [
            f"ENVIRONMENT FAILURE [{kind}]",
            f"cause: {detail}",
            "repairable by editing the repository: NO",
        ]
        if hint:
            lines.append(f"operator action: {hint}")
        if declared:
            shown = ", ".join(declared[:12])
            more = len(declared) - 12
            lines.append(
                f"declared dependencies considered: {shown}"
                + (f" (+{more} more)" if more > 0 else "")
            )
        lines.append(
            "The run stopped instead of editing the repository: an environment "
            "fault has no repository-side fix."
        )
        return "\n".join(lines)
    except Exception:  # pragma: no cover — defensive
        return "ENVIRONMENT FAILURE [unclassified]: triage unavailable"


# ===========================================================================
# AGT-02 — the bounded reflection loop
# ===========================================================================
#
# A failure that does not become the NEXT turn's input is a failure the model
# has to rediscover. This section is the policy for that: one capped loop, an
# honest RETRY CLASS per failure, and a journal row per reflection so a run's
# struggle is reconstructable from its trace alone.
#
# Three rules, in order of importance:
#
#   1. ONE classification. The retry class is DERIVED from the vocabulary this
#      module already defines — the `POLICY` rows and the model-failure kinds
#      `classify_model_failure` produces. There is no second error table here,
#      because a second table is a second policy and the two would drift. What
#      this section adds is the AXIS (may another attempt help?), not a new set
#      of failure names.
#   2. A REFUSAL IS NOT A RETRYABLE FAILURE. A harness/OS refusal and a
#      non-repairable environment fault do not spend the reflection budget, and
#      the rendered reflection for one never offers to "try again" — otherwise
#      a refusal becomes an invitation and a run can talk itself into hammering
#      a refused call. `render_reflection` is where that is enforced.
#   3. THE CAP IS REPORTED. `ReflectionBudget.report()` names both caps, both
#      counters, and which cap bound the last decision, so "the run stopped"
#      is never a mystery.

#: A provider fault (429/5xx/connection/timeout). Another attempt can succeed.
RETRY_CLASS_TRANSIENT = "transient_provider"
#: The model produced something unusable (a malformed tool call). Worth one
#: bounded reflection: the model, not the machine, can fix it.
RETRY_CLASS_MODEL_ERROR = "model_error"
#: The work itself failed in a way a different approach can fix: a test, a
#: lint finding, a tool error, a patch that would not apply.
RETRY_CLASS_TASK_ERROR = "task_error"
#: The harness or the OS refused ON PURPOSE (a deny-guard shape, a protected
#: path). Repeating the request is a no-op, so it must not spend the budget.
RETRY_CLASS_POLICY_REFUSAL = "policy_refusal"
#: Nothing to retry: a terminal model failure, an environment fault, or a
#: doom-loop stop. The run reports the reason instead of trying again.
RETRY_CLASS_TERMINAL = "terminal"

#: The closed retry-class vocabulary. Every input is an EXISTING classified
#: kind; the output is one of these five. Pinned by
#: `test_every_existing_kind_maps_to_exactly_one_retry_class`.
RETRY_CLASSES: Tuple[str, ...] = (
    RETRY_CLASS_TRANSIENT,
    RETRY_CLASS_MODEL_ERROR,
    RETRY_CLASS_TASK_ERROR,
    RETRY_CLASS_POLICY_REFUSAL,
    RETRY_CLASS_TERMINAL,
)

#: The classes a reflection may SPEND budget on. Everything else is reported
#: and free, which is what stops a refused call from eating a run's budget.
RETRYABLE_CLASSES = frozenset(
    {RETRY_CLASS_TRANSIENT, RETRY_CLASS_MODEL_ERROR, RETRY_CLASS_TASK_ERROR}
)
NON_RETRYABLE_CLASSES = frozenset({RETRY_CLASS_POLICY_REFUSAL, RETRY_CLASS_TERMINAL})

# The provider classes are NOT re-listed here: these are the same two
# frozensets `classify_model_failure` already keys its retry decision off, so
# "transient" and "terminal" cannot disagree between the two functions.
_TRANSIENT_MODEL_KINDS: frozenset = _RETRYABLE_MODEL_KINDS
_TERMINAL_MODEL_KINDS: frozenset = _TERMINAL_MODEL_KINDS

#: POLICY action slugs whose instruction is already "do not do this again"
#: ("Do not retry it", "Repeating it verbatim is a no-op"). A refusal is read
#: off the ONE table's own action rather than a second hand-written list, so a
#: new refusal row is classified correctly the moment it is added.
_REFUSAL_ACTIONS = frozenset({"avoid_shape", "forbid_path"})

#: Evidence longer than this is head+tail shaped with the shared omission
#: marker, never silently cut. Bounded per configuration.
_REFLECTION_EVIDENCE_FLOOR = 200


def retry_class_of(kind: Any) -> str:
    """The reflection class for an EXISTING classified kind.

    Assumes ``kind`` is a kind string this module already produces (a `POLICY`
    key or a model-failure kind) or any object carrying one in ``.kind``.
    Returns a member of :data:`RETRY_CLASSES`; never raises.

    Two deliberate choices:

    * **An unknown kind is `task_error`, not a refusal.** A refusal means "do
      not spend budget on this", so guessing one would silently disable
      recovery for a failure nobody has classified yet. An unknown kind is
      reflected on, which is the recoverable direction.
    * **A refusal is read off the `POLICY` action**, so a harness refusal and
      a non-repairable environment fault cannot be laundered into "try again"
      by wording alone.
    """
    try:
        text = str(getattr(kind, "kind", kind) or "").strip()
        if not text:
            return RETRY_CLASS_TASK_ERROR
        if text in _TRANSIENT_MODEL_KINDS:
            return RETRY_CLASS_TRANSIENT
        if text in _TERMINAL_MODEL_KINDS:
            return RETRY_CLASS_TERMINAL
        if text == KIND_MALFORMED_TOOL_CALL:
            return RETRY_CLASS_MODEL_ERROR
        if is_environment_kind(text):
            # R2-05: not repairable by another attempt either.
            return RETRY_CLASS_TERMINAL
        if text == KIND_LOOP_DETECTED:
            return RETRY_CLASS_TERMINAL
        row = POLICY.get(text)
        if row and str(row[0]) in _REFUSAL_ACTIONS:
            return RETRY_CLASS_POLICY_REFUSAL
        return RETRY_CLASS_TASK_ERROR
    except Exception:  # pragma: no cover — defensive
        return RETRY_CLASS_TASK_ERROR


def retry_class_of_model_failure(failure: Any) -> str:
    """The reflection class for a `ModelFailure` (or a bare kind string).

    Assumes ``failure`` is what :func:`classify_model_failure` returned. The
    provider's own ``retryable`` verdict is the authority when it is present,
    so the two functions cannot disagree about a 429; otherwise the kind is
    classified by :func:`retry_class_of`.
    """
    try:
        if getattr(failure, "retryable", None) is True:
            return RETRY_CLASS_TRANSIENT
        return retry_class_of(failure)
    except Exception:  # pragma: no cover — defensive
        return RETRY_CLASS_TERMINAL


class Reflection(NamedTuple):
    """One failure the loop has decided what to do about.

    index: 1-based position in the run's reflection journal (EVERY failure
      reaches the journal, so a reader can see what was considered).
    step_index: 1-based position within the current step.
    charged_index: the same counter counting only budget-spending
      reflections; `None` when this failure spent nothing.
    kind: the existing classified kind, verbatim.
    retry_class: one of :data:`RETRY_CLASSES`.
    step_id / signature: what "the same failure" means here — the step being
      worked on, and the call that failed.
    evidence: the bounded failure text, so the reflection carries the failure
      VERBATIM rather than a summary of it.
    retryable: whether another attempt can help at all.
    charged: whether this reflection spent budget.
    allowed: whether the loop may hand the failure back to the model.
    exhausted: the cap bound this decision (`per_step` / `per_run`).
    reason: why it was refused, or why it was free.
    """

    index: int
    step_index: int
    charged_index: Optional[int]
    kind: str
    retry_class: str
    step_id: str
    signature: str
    evidence: str
    retryable: bool
    charged: bool
    allowed: bool
    exhausted: str
    reason: str
    step_cap: int
    run_cap: int
    step_used: int
    run_used: int

    def to_dict(self) -> Dict[str, Any]:
        """The journal / receipt projection (bounded, JSON-safe)."""
        return {
            "index": self.index,
            "step_index": self.step_index,
            "charged_index": self.charged_index,
            "kind": self.kind,
            "retry_class": self.retry_class,
            "step_id": self.step_id,
            "signature": self.signature,
            "retryable": self.retryable,
            "charged": self.charged,
            "allowed": self.allowed,
            "exhausted": self.exhausted,
            "reason": self.reason,
            "step_cap": self.step_cap,
            "run_cap": self.run_cap,
            "step_used": self.step_used,
            "run_used": self.run_used,
            "evidence_chars": len(self.evidence or ""),
            "evidence": self.evidence,
        }


class ReflectionBudget:
    """The bounded reflection budget for one run.

    Two caps, both configurable, both reported:

    * ``per_step`` — reflections spent since the last productive turn (a
      successful call ends the step). This is the "three failures then a
      fourth is refused" cap.
    * ``per_run`` — reflections spent for the whole run, so a long session
      cannot reflect forever.

    Accounting rules, all test-pinned:

    * A **retryable** class charges the budget and is allowed while both caps
      have room.
    * A **non-retryable** class (harness refusal, environment fault, terminal
      model failure, doom-loop stop) is journalled and is **free**: it does not
      consume either counter, so a refused call can never be used to exhaust a
      run's recovery budget.
    * When a cap is spent the reflection is still journalled, with
      ``allowed=False`` and the cap that bound it, so "the loop stopped" is
      reconstructable.

    Every method is defensive by contract: recovery machinery must never be
    the reason a run dies, so a failure inside the budget degrades to a
    free, non-blocking reflection rather than raising.
    """

    def __init__(
        self,
        *,
        per_step: int = 3,
        per_run: int = 12,
        max_evidence_chars: int = 4000,
        trace: Any = None,
        label: str = "",
    ) -> None:
        self.per_step = max(1, int(per_step))
        self.per_run = max(1, int(per_run))
        self.max_evidence_chars = max(
            _REFLECTION_EVIDENCE_FLOOR, int(max_evidence_chars)
        )
        self.trace = trace
        self.label = str(label or "")
        self.reflections: List[Reflection] = []
        self._step_id = ""
        self._step_used = 0
        self._run_used = 0

    # -- step bookkeeping ------------------------------------------------

    def begin_step(self, step_id: str) -> None:
        """Declare the step being worked on; a CHANGE resets its counter."""
        try:
            sid = str(step_id or "run")
        except Exception:  # pragma: no cover — defensive
            sid = "run"
        if sid != self._step_id:
            self._step_id = sid
            self._step_used = 0

    def note_success(self, step_id: Optional[str] = None) -> None:
        """A productive turn: the current failure streak is over."""
        if step_id is not None:
            self.begin_step(step_id)
        self._step_used = 0

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def run_used(self) -> int:
        return self._run_used

    @property
    def last(self) -> Optional[Reflection]:
        """The most recent journalled reflection, or None."""
        return self.reflections[-1] if self.reflections else None

    # -- the decision ----------------------------------------------------

    def note(
        self,
        kind: Any,
        *,
        evidence: str = "",
        signature: str = "",
        step_id: str = "",
        retry_class: Optional[str] = None,
    ) -> Reflection:
        """Record ONE failure and decide whether the loop may reflect on it.

        Assumes ``kind`` is an already-classified kind (or ``ModelFailure``),
        ``evidence`` is the failure text to hand back VERBATIM, and
        ``signature`` identifies the call that failed so "the same failure"
        means something. ``retry_class`` overrides the derived class for a
        failure this module has no kind for (an approval refusal, say) — the
        override is still checked against the closed vocabulary, so it cannot
        invent a class.
        """
        try:
            text = str(getattr(kind, "kind", kind) or "").strip()
            cls = str(retry_class or retry_class_of(kind))
            if cls not in RETRY_CLASSES:
                cls = RETRY_CLASS_TASK_ERROR
            self.begin_step(step_id or self._step_id or "run")
            sig = " ".join(str(signature or text).split())[:200]
            body = self._shape(str(evidence or ""))
            retryable = cls in RETRYABLE_CLASSES

            charged = False
            allowed = False
            exhausted = ""
            charged_index: Optional[int] = None
            if not retryable:
                reason = (
                    f"not retryable ({cls}): another attempt cannot change this "
                    "answer, so the reflection budget is not spent"
                )
            elif self._step_used >= self.per_step:
                exhausted = "per_step"
                reason = (
                    f"per-step reflection cap reached "
                    f"({self._step_used}/{self.per_step})"
                )
            elif self._run_used >= self.per_run:
                exhausted = "per_run"
                reason = (
                    f"per-run reflection cap reached ({self._run_used}/{self.per_run})"
                )
            else:
                charged = True
                allowed = True
                charged_index = self._run_used + 1
                self._step_used += 1
                self._run_used += 1
                reason = f"reflection {charged_index} of the run budget"

            reflection = Reflection(
                index=len(self.reflections) + 1,
                step_index=self._step_used if charged else self._step_used + 1,
                charged_index=charged_index,
                kind=text,
                retry_class=cls,
                step_id=self._step_id,
                signature=sig,
                evidence=body,
                retryable=retryable,
                charged=charged,
                allowed=allowed,
                exhausted=exhausted,
                reason=reason,
                step_cap=self.per_step,
                run_cap=self.per_run,
                step_used=self._step_used,
                run_used=self._run_used,
            )
            self.reflections.append(reflection)
            self._emit(reflection)
            return reflection
        except Exception as exc:  # pragma: no cover — defensive
            return self._degrade(exc)

    def _shape(self, body: str) -> str:
        """Head+tail bound the evidence with the SHARED omission marker."""
        cap = self.max_evidence_chars
        if len(body) <= cap:
            return body
        head = cap // 2
        return (
            body[:head]
            + OMISSION_MARKER.format(n=len(body) - cap)
            + body[-(cap - head) :]
        )

    def _degrade(self, exc: BaseException) -> Reflection:
        """A budget that could not decide must not stop the run."""
        reflection = Reflection(
            index=len(self.reflections) + 1,
            step_index=0,
            charged_index=None,
            kind=str(getattr(exc, "__class__", type(exc)).__name__),
            retry_class=RETRY_CLASS_TASK_ERROR,
            step_id=self._step_id or "run",
            signature="",
            evidence=f"(reflection budget unavailable: {exc})",
            retryable=True,
            charged=False,
            allowed=True,
            exhausted="",
            reason="reflection budget degraded; no budget was spent",
            step_cap=self.per_step,
            run_cap=self.per_run,
            step_used=self._step_used,
            run_used=self._run_used,
        )
        self.reflections.append(reflection)
        self._emit(reflection)
        return reflection

    def _emit(self, reflection: Reflection) -> None:
        if self.trace is None:
            return
        try:
            self.trace.log(
                "reflection",
                {"label": self.label, **reflection.to_dict()},
            )
        except Exception:  # pragma: no cover — observability must not kill a run
            pass

    # -- reporting -------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """Both caps, both counters, and the per-class tally.

        ``exhausted`` is the honest answer to "why did the loop stop", and
        ``last`` carries the failure that stopped it.
        """
        try:
            by_class: Dict[str, int] = {}
            charged = 0
            free = 0
            for item in self.reflections:
                key = item.retry_class
                by_class[key] = by_class.get(key, 0) + 1
                if item.charged:
                    charged += 1
                else:
                    free += 1
            return {
                "per_step": self.per_step,
                "per_run": self.per_run,
                "step_used": self._step_used,
                "run_used": self._run_used,
                "reflections": len(self.reflections),
                "charged": charged,
                "free": free,
                "by_class": by_class,
                "by_kind": _tally(item.kind for item in self.reflections),
                "steps": sorted({item.step_id for item in self.reflections}),
                "step_cap_reached": self._step_used >= self.per_step,
                "run_cap_reached": self._run_used >= self.per_run,
                "exhausted": (
                    "per_step"
                    if self._step_used >= self.per_step
                    else ("per_run" if self._run_used >= self.per_run else "")
                ),
                "last": self.last.to_dict() if self.last is not None else None,
            }
        except Exception:  # pragma: no cover — defensive
            return {}


def render_reflection(reflection: Any, *, header: str = "") -> str:
    """Render one :class:`Reflection` as the model-facing NEXT USER MESSAGE.

    The failure itself is embedded VERBATIM (bounded by the budget's evidence
    cap), because a summary of a failure is a different failure.

    Three shapes, and the difference between them is the point of the
    section:

    * a retryable failure the budget still affords — the failure, its evidence,
      and the instruction to act differently;
    * a NON-RETRYABLE failure — the evidence, and an explicit statement that
      the decision STANDS. It never says "try again" and never offers to
      rephrase the refused call, which is how a refusal would be laundered
      into compliance;
    * a failure the cap refused — the last failure, verbatim, and an
      instruction to stop retrying and report.
    """
    try:
        if not isinstance(reflection, Reflection):
            return str(header or "")
        kind = reflection.kind or "failure"
        cls = reflection.retry_class
        evidence = reflection.evidence or "(no evidence captured)"

        if not reflection.retryable:
            return "\n".join(
                [
                    f"## REFUSED [{kind} · {cls}] — not retryable, no budget spent",
                    "The harness already decided this one. Repeating the same",
                    "request in any other wording is a no-op, so do not spend",
                    "another attempt on it. The evidence, verbatim:",
                    "",
                    evidence,
                    "",
                    "Take a different approach, or state plainly that the task",
                    "cannot proceed and why.",
                ]
            )

        if not reflection.allowed:
            where = reflection.exhausted or "run"
            return "\n".join(
                [
                    f"## REFLECTION BUDGET EXHAUSTED ({where}): {kind} · {cls}",
                    f"Cap: {reflection.step_used}/{reflection.step_cap} for this",
                    f"step, {reflection.run_used}/{reflection.run_cap} for the run.",
                    f"Refused because: {reflection.reason}.",
                    "",
                    "The last failure, verbatim:",
                    "",
                    evidence,
                    "",
                    "Do not retry this failure. Report what is blocked and why,",
                    "or finish the remaining work a different way.",
                ]
            )

        lines = [
            f"## REFLECTION {reflection.charged_index} "
            f"({reflection.step_used}/{reflection.step_cap} this step, "
            f"{reflection.run_used}/{reflection.run_cap} this run) — {kind} · {cls}",
            "The previous attempt failed. This failure is your next input,",
            "verbatim — act on THIS, not on a guess about it:",
            "",
            evidence,
        ]
        if header:
            lines.extend(["", str(header)])
        lines.extend(
            [
                "",
                "Change your approach. Continue with exactly ONE tool call, or",
                "DONE with an honest summary of what you could not do.",
            ]
        )
        return "\n".join(lines)
    except Exception:  # pragma: no cover — defensive
        return ""


def reflection_budget_from_config(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    trace: Any = None,
    label: str = "",
) -> ReflectionBudget:
    """Build a :class:`ReflectionBudget` from ``Task.config``.

    Recognized keys (all optional, all read by presence so an unusable value
    degrades to the documented default rather than crashing a run):
    ``reflection_max_per_step``, ``reflection_max_per_run`` and
    ``reflection_evidence_max_chars``.
    """
    conf = dict(cfg or {})

    def _int(key: str, fallback: int) -> int:
        try:
            return int(conf.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return ReflectionBudget(
        per_step=_int("reflection_max_per_step", 3),
        per_run=_int("reflection_max_per_run", 12),
        max_evidence_chars=_int("reflection_evidence_max_chars", 4000),
        trace=trace,
        label=label,
    )
