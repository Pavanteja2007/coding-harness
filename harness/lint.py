"""Lint / static-analysis gate (Round 8, Task C).

Purpose: catch syntax and name-resolution errors in the agent's edits in
MILLISECONDS (host-side AST pass over the changed files) instead of
paying for a full sandboxed pytest verify cycle to discover the same
thing. Wired into the loop controller so a lint failure short-circuits
to a fix attempt (the step gets the classified lint errors as feedback)
before burning a verify cycle.

What it checks (stdlib-only â€” NO new dependencies, works offline and
in every CI cell):
1. syntax          â€” compile() per changed .py file (same class the
                      editor gate already runs, reported here with line
                      numbers as a LintFinding).
2. undefined_name  â€” a conservative single-file scope resolver that
                      flags names USED at module level but never defined
                      anywhere in the file (the import-time NameError
                      class: `_DAYS_PER_MONTHS` vs `_DAYS_PER_MONTH`,
                      a deleted import, a renamed module constant).

Why module-level-only for undefined names: local control flow
(conditional assignment, late imports, decorators, comprehension
scoping) makes single-file FUNCTION-body analysis false-positive-prone,
and the cost model is asymmetric â€” a missed real error costs one verify
cycle (which still catches it, fail-safe); a false positive would block
a VERIFIABLY CORRECT fix (unsafe). The classic agent-edit failure
shapes (typo'd/referenced-then-deleted top-level symbol) live exactly
at module level, where an import-time NameError crashes the whole
module anyway.

Suppression rules (all false-negative biased): builtins; every name
defined anywhere in the file (assignments incl. tuple/star targets,
import bindings, def/class names at ANY nesting, comprehension
targets, global/nonlocal declarations, walrus targets, except-as);
star imports (can't know what they bind) disable the pass for the file;
`__all__` members count as defined (re-export style).

Config keys (project convention â€” no hardcoded knobs):
  lint_gate  (bool, default True) â€” enables the pre-verify gate
  lint_names (bool, default True) â€” enables the undefined-name pass
                (syntax is always checked; a repo whose style defeats
                the name pass can pin just that half off)

Failure semantics: lint does NOT replace verify â€” it only short-circuits
the verify call when it KNOWS the code is broken; it never gates
success. Verifier-gated completion stays absolute (spec item 17).

--- AGT-03: the IN-EDIT check ---------------------------------------------

`check_source_for_edit` is a different surface from `lint_changed` and is
NOT a second lint. It answers one question about ONE candidate string that
is still in memory, before anything reaches the disk:

  is this content, as it is about to be written, syntactically broken?

Three properties separate it from the loop gate above, and each exists
because the loop gate cannot do this job:

1. **It is TRI-STATE.** `passed` / `failed` / `unchecked`. A file with no
   checker for its language is `unchecked` with a reason â€” never `passed`.
   A silent skip reads as a pass, and a reader who cannot tell "checked and
   clean" from "never looked at" will draw the wrong conclusion about how
   much was verified.
2. **It is PRE-COMMIT.** The candidate bytes are checked in memory, so a
   failing edit is DISCARDED rather than written and then rolled back. The
   loop gate is unchanged and still runs as the backstop; this is the fast
   local guard in front of it, not a replacement for it.
3. **It carries CONTEXT.** The failure ships Â±3 lines around the offending
   line, which is the form a model can actually self-correct from; a bare
   "syntax error" with a line number is not.

Why the undefined-name pass is OFF by default here (`check_names=False`)
even though the loop gate runs it: that pass is documented above as
false-NEGATIVE biased and false-POSITIVE prone for anything involving local
control flow, and a false positive in the LOOP gate costs one wasted
attempt, while a false positive HERE refuses a mutation outright. The loop
gate still runs the pass a moment later on the same file. A caller that
wants the stricter in-edit behaviour passes `check_names=True`
(`edit_inline_lint_names`).
"""

import ast
import builtins
from pathlib import Path
from typing import List, NamedTuple, Set, Tuple

from harness.redaction import redact_text_for_journal

__all__ = [
    "CHECK_DISABLED",
    "CHECK_FAILED",
    "CHECK_PASSED",
    "CHECK_STATUSES",
    "CHECK_UNCHECKED",
    "DEFAULT_CONTEXT_LINES",
    "LANGUAGE_BY_SUFFIX",
    "EditCheck",
    "LintFinding",
    "check_source_for_edit",
    "checker_unavailable_reason",
    "language_of",
    "lint_changed",
    "lint_file",
    "render_check",
    "render_findings",
]


class LintFinding(NamedTuple):
    """One lint problem: file, 1-based line, stable class id, message.

    kind is one of: "syntax" | "undefined_name"."""

    file: str
    line: int
    kind: str
    message: str


_BUILTINS: Set[str] = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__debug__",
    "__annotations__",
    "__dict__",
    "__path__",
    "__module__",
    "__qualname__",
    "__cached__",
    "__all__",
    "__version__",
    "__author__",
    "__getattr__",
}


# ---------------------------------------------------------------------------
# definition collection (over-approximation â€” see module docstring)
# ---------------------------------------------------------------------------


class _Defs(ast.NodeVisitor):
    """Collect every name the file could bind, at any nesting."""

    def __init__(self) -> None:
        self.defined: Set[str] = set()
        self.star_import: bool = False

    # -- binding statements ------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.defined.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.star_import = True
            else:
                self.defined.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # every Store/Bind context counts as a definition, wherever it
        # occurs (targets, walrus, comprehension targets are all Store)
        if not isinstance(node.ctx, ast.Load):
            self.defined.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        # function params (incl. nested/lambda) â€” suppress uses of them
        self.defined.add(node.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.defined.add(node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.defined.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.defined.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.defined.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.defined.add(node.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # __all__ entries count as defined (re-export style)
        for t in node.targets:
            if (
                isinstance(t, ast.Name)
                and t.id == "__all__"
                and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        self.defined.add(elt.value)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# use collection (module-level statements only)
# ---------------------------------------------------------------------------


def _loads(node: ast.AST) -> Set[str]:
    """All names referenced (Load context) anywhere inside `node`."""
    out: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            out.add(sub.id)
    return out


def _module_level_loads(tree: ast.Module) -> List[Set[str]]:
    """Load-name sets for the module's top-level statements â€” grouped
    per statement so a finding can cite the statement's line; nested
    function/class bodies are EXCLUDED (their locals are covered by the
    over-approximated definition set; only the module-level import-time
    execution path is checked)."""
    groups: List[Set[str]] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # decorator/default expressions evaluate at import time
            exprs: List[ast.AST] = list(stmt.decorator_list)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                exprs += list(stmt.args.defaults)
                exprs += [d for d in stmt.args.kw_defaults if d is not None]
            else:
                exprs += list(stmt.bases) + [kw.value for kw in stmt.keywords]
                exprs += list(stmt.decorator_list)
            names: Set[str] = set()
            for e in exprs:
                names |= _loads(e)
            groups.append(names)
            continue
        groups.append(_loads(stmt))
    return groups


#: Suffix -> language token. The ONE place the in-edit check decides which
#: parser (if any) applies to a path; `check_syntax`'s historical suffix
#: tests and this table are the same fact, so the table owns it.
LANGUAGE_BY_SUFFIX: Tuple[Tuple[str, str], ...] = (
    (".py", "python"),
    (".pyi", "python"),
    (".js", "javascript"),
    (".jsx", "javascript"),
    (".mjs", "javascript"),
    (".cjs", "javascript"),
    (".ts", "typescript"),
    (".tsx", "typescript"),
)


def language_of(rel: str) -> str:
    """Return the language token for `rel`, or '' when none applies.

    Assumes `rel` is a path string; a missing/blank path is '' rather than
    an exception, because a caller passes whatever the model supplied."""
    text = str(rel or "").replace("\\", "/").lower()
    if not text:
        return ""
    for suffix, language in LANGUAGE_BY_SUFFIX:
        if text.endswith(suffix):
            return language
    return ""


def check_syntax(src: str, rel: str) -> List[LintFinding]:
    """Syntax check; at most one finding per file. Language-aware:
    .py -> compile(); .js/.jsx/.mjs/.cjs -> tree-sitter JS grammar;
    .ts/.tsx -> tree-sitter TS grammar. Assumes rel is the repo-relative
    path used in messages. JS/TS parsing needs the tree-sitter grammar
    packages on the HOST (same ones the code graph uses); without them
    the check yields NO findings (false-negative biased â€” the sandbox
    verify still catches real syntax errors, and the gate must never
    block a verifiably-correct fix over host tooling)."""
    language = language_of(rel)
    if language in ("javascript", "typescript"):
        ok, msg = _ts_syntax_ok(
            src.encode("utf-8", errors="replace"),
            "js" if language == "javascript" else "ts",
        )
        if not ok:
            return [LintFinding(rel, 0, "syntax", f"syntax error: {msg}")]
        return []
    if language:
        try:
            compile(src, rel, "exec")
        except SyntaxError as e:
            at = f" at line {e.lineno}" if e.lineno else ""
            return [
                LintFinding(rel, e.lineno or 0, "syntax", f"syntax error{at}: {e.msg}")
            ]
        return []
    # An unrecognised extension: this function's historical contract is that
    # it ignores what it cannot check. `check_source_for_edit` is the surface
    # that reports `unchecked` instead, and the two must not be merged.
    return []


def _ts_parser(which: str) -> Tuple[object, str]:
    """Build a tree-sitter parser; (parser, reason). reason == '' on success.

    Split out of `_ts_syntax_ok` for AGT-03 so "the grammar is not installed
    on this host" is a value the caller can REPORT rather than a silent
    (True, ''). The loop gate keeps its documented false-negative bias; the
    in-edit check does not."""
    try:
        from tree_sitter import Language, Parser

        if which == "js":
            import tree_sitter_javascript as pkg

            lang = Language(pkg.language())
        else:
            import tree_sitter_typescript as pkg

            lang = Language(pkg.language_typescript())
        return Parser(lang), ""
    except Exception as exc:
        return None, (
            f"the {which} tree-sitter grammar is not available on this host "
            f"({type(exc).__name__}: {exc})"
        )


def _ts_syntax_status(source: bytes, which: str) -> Tuple[str, int, str]:
    """Parse JS/TS source; (status, line, message).

    status is one of "ok" | "error" | "unavailable". The third value is the
    whole point of the split: it is the difference between "this parses" and
    "this could not be parsed here", which the loop gate collapses (correctly,
    for its cost model) and the in-edit gate must not."""
    parser, reason = _ts_parser(which)
    if parser is None:
        return "unavailable", 0, reason
    tree = parser.parse(source)  # type: ignore[attr-defined]
    if not tree.root_node.has_error:
        return "ok", 0, ""
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        if n.type == "ERROR" or n.is_missing:
            return "error", n.start_point[0] + 1, "parse error"
        stack.extend(c for c in n.children if c is not None)
    return "error", 0, "parse error"


def _ts_syntax_ok(source: bytes, which: str) -> Tuple[bool, str]:
    """Parse JS/TS source with tree-sitter; (ok, error). which is 'js' or
    'ts'. Grammar missing on host -> (True, '') (documented bias)."""
    status, _line, message = _ts_syntax_status(source, which)
    if status == "unavailable":
        return True, ""
    return status == "ok", message


def check_undefined_names(src: str, rel: str) -> List[LintFinding]:
    """Names used at module level but defined nowhere in the file or
    builtins. Assumes src parses (run check_syntax first). False-negative
    biased â€” see module docstring for the suppression rules."""
    tree = ast.parse(src)
    defs = _Defs()
    defs.visit(tree)
    if defs.star_import:
        return []
    findings: List[LintFinding] = []
    seen: Set[str] = set()
    for stmt, loads in zip(tree.body, _module_level_loads(tree), strict=False):
        undefined = loads - defs.defined - _BUILTINS
        for name in sorted(undefined):
            if name in seen:
                continue
            seen.add(name)
            findings.append(
                LintFinding(
                    rel,
                    stmt.lineno,
                    "undefined_name",
                    f"undefined name '{name}' at module level â€” not defined "
                    f"or imported anywhere in this file",
                )
            )
    return findings


def lint_file(src: str, rel: str, check_names: bool = True) -> List[LintFinding]:
    """Lint one file's source; syntax first (language-aware â€” see
    check_syntax), names only if it parses. Assumes rel is the
    repo-relative path for messages; the undefined-NAME pass stays
    Python-only (JS/TS name resolution needs runtime semantics the
    single-file pass can't approximate safely â€” documented in
    harness/AGENTS.md)."""
    findings = check_syntax(src, rel)
    if findings:
        return findings
    if not check_names or not rel.endswith(".py"):
        return findings
    try:
        findings.extend(check_undefined_names(src, rel))
    except Exception as exc:  # never let lint crash a step
        findings.append(LintFinding(rel, 0, "syntax", f"lint name pass failed: {exc}"))
    return findings


def lint_changed(
    work_dir: str,
    changed: List[str],
    check_names: bool = True,
    max_file_bytes: int = 200_000,
) -> List[LintFinding]:
    """Lint the agent's CHANGED files (vs pristine) â€” the fast pre-verify
    gate. Assumes changed is editor.changed_files() output (repo-relative
    posix paths) and work_dir is the working copy root. Never raises: an
    unreadable file is reported as a finding, not an exception."""
    findings: List[LintFinding] = []
    for rel in changed:
        try:
            p = Path(work_dir, rel)
            if not p.is_file():
                continue  # deleted file â€” nothing to lint
            if p.stat().st_size > max_file_bytes:
                continue  # same size cap the context injector uses
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(
                LintFinding(rel, 0, "syntax", f"unreadable during lint: {e}")
            )
            continue
        findings.extend(lint_file(src, rel, check_names=check_names))
    return findings


def render_findings(findings: List[LintFinding]) -> str:
    """Model-facing one-block rendering (rides the step feedback and the
    short-circuit message). Assumes findings came from lint_changed.

    **Redaction boundary (decision: redact AT THE BOUNDARY, here).** A
    `LintFinding.message` quotes the offending source token, so a finding in a
    `.env` or a credentials module carries the secret into every consumer of
    this string: the step feedback the model reads, the `lint_failed` journal
    row, and the retry feedback in `harness/core.py`. Those are three
    different surfaces with three different lifetimes, so the value is
    redacted once here rather than three times by three callers that can
    disagree. `file` and `line` are a path and a number and are not redacted,
    which is what keeps the finding locatable.

    The authority is `shared.security`, reached through
    `harness.redaction.redact_text_for_journal`, and the boundary is
    fail-closed: a value that cannot be redacted is REPLACED with
    ``(detail withheld: ...)``, never passed through raw.
    """
    if not findings:
        return ""
    lines = ["LINT FAILED â€” fix these before re-submitting:"]
    for f in findings:
        lines.append(
            f"  {f.file}:{f.line}: [{f.kind}] "
            f"{redact_text_for_journal(f.message, where='lint.render_findings')}"
        )
    lines.append(
        "These were found by a fast static check â€” fix them first; "
        "the full test suite has not run yet."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AGT-03: the in-edit check (tri-state, pre-commit, with context)
# ---------------------------------------------------------------------------

#: The content parses. This is the ONLY status that means "looked at it".
CHECK_PASSED = "passed"
#: The content does not parse. The only status a mutating caller may refuse on.
CHECK_FAILED = "failed"
#: No checker applies (unsupported language, missing grammar, unreadable
#: bytes). Never presented as a pass â€” the reason travels with it.
CHECK_UNCHECKED = "unchecked"
#: A caller turned the in-edit check off. Distinct from `unchecked` because
#: "nobody asked for a check" and "a check was wanted and could not run" are
#: different facts about how much was verified.
CHECK_DISABLED = "disabled"

#: The closed status vocabulary, so a consumer can reject an unknown value
#: instead of defaulting it to a pass.
CHECK_STATUSES: Tuple[str, ...] = (
    CHECK_PASSED,
    CHECK_FAILED,
    CHECK_UNCHECKED,
    CHECK_DISABLED,
)

#: How many lines of context either side of the offending line ride a
#: failure. The brief's Â±3, and a value rather than a constant in a caller.
DEFAULT_CONTEXT_LINES = 3

#: Hard cap on the rendered context block, so a pathological one-line file
#: cannot turn a refusal into a multi-megabyte tool result.
MAX_CONTEXT_CHARS = 1200


class EditCheck(NamedTuple):
    """The result of checking ONE candidate string, before it is written.

    `status` is one of `CHECK_STATUSES`. `checked` is True only for
    `passed`/`failed` â€” the distinction the whole surface exists to keep
    honest. `reason` is populated for `unchecked`/`disabled` (why no check
    happened) and is empty for a real verdict. `context` is the Â±
    `context_lines` block around `line`, empty when there is no line to
    centre on. `ok` is True for everything except `failed`: a caller that
    can only act on a real failure uses it; a caller that renders a receipt
    must read `status` and must not collapse `unchecked` into `passed`.
    """

    status: str
    file: str
    line: int = 0
    kind: str = ""
    message: str = ""
    context: str = ""
    reason: str = ""

    @property
    def checked(self) -> bool:
        """True only when a checker actually ran and returned a verdict."""
        return self.status in (CHECK_PASSED, CHECK_FAILED)

    @property
    def ok(self) -> bool:
        """True unless the content is known to be broken."""
        return self.status != CHECK_FAILED

    def to_dict(self) -> dict:
        """Return the JSON-safe projection used in receipts and trace rows."""
        return {
            "status": self.status,
            "checked": self.checked,
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "message": self.message,
            "context": self.context,
            "reason": self.reason,
        }


def checker_unavailable_reason(language: str) -> str:
    """Return why `language` cannot be checked on this host, or '' if it can.

    Python is always checkable: `compile()` needs no external package.
    JavaScript/TypeScript need the tree-sitter grammar packages that the
    code graph also uses, and a host without them gets an honest reason
    instead of a silent pass."""
    if language == "python":
        return ""
    if language in ("javascript", "typescript"):
        _parser, reason = _ts_parser("js" if language == "javascript" else "ts")
        return reason
    return f"no in-edit checker is registered for {language or 'this file'}"


def _context_block(src: str, line: int, span: int) -> str:
    """Render Â±`span` lines around 1-based `line`, with real line numbers.

    Returns '' when there is no usable line, rather than inventing a
    context that points at the wrong place. Long lines are truncated from
    the RIGHT (the left edge is what a reader needs to recognise the line)
    and the whole block is capped at `MAX_CONTEXT_CHARS`."""
    if line <= 0:
        return ""
    lines = src.splitlines()
    if not lines:
        return ""
    index = min(max(line, 1), len(lines)) - 1
    start = max(0, index - span)
    end = min(len(lines), index + span + 1)
    out: List[str] = []
    for offset in range(start, end):
        number = offset + 1
        text = lines[offset]
        if len(text) > 200:
            text = text[:200] + "..."
        marker = ">" if offset == index else " "
        out.append(f"{marker} {number:>5} | {text}")
    block = "\n".join(out)
    if len(block) > MAX_CONTEXT_CHARS:
        block = block[:MAX_CONTEXT_CHARS] + "\n[... context truncated ...]"
    return block


def check_source_for_edit(
    src: str,
    rel: str,
    *,
    check_names: bool = False,
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> EditCheck:
    """Check ONE candidate string's syntax, before it is committed to disk.

    Assumes `src` is the exact post-edit content (the bytes the write is
    about to make visible) and `rel` is the repo-relative path it will be
    written to â€” the path selects the language, so a caller that checks the
    wrong path gets a check of the wrong language. Never raises: a checker
    that crashes is reported as a failure, because a crash is not a pass.

    `check_names` additionally runs the module-level undefined-name pass
    and is OFF by default here; see the module docstring for why (a false
    positive refuses a mutation outright, while the loop gate â€” which runs
    the same pass moments later â€” only costs a wasted attempt).

    The status vocabulary is `CHECK_STATUSES`. `unchecked` is a real
    outcome, not a soft failure: a `.md` file, a `.txt` file, a JavaScript
    file on a host without the grammar, and bytes that do not decode are
    all `unchecked` with a reason, and none of them is `passed`.
    """
    path = str(rel or "").replace("\\", "/")
    # Coerce here as well as in the editor: this function's contract is that
    # it never raises, and a caller that passes a config value straight
    # through must not be able to make it raise with a bad span.
    try:
        span = max(0, int(context_lines))
    except (TypeError, ValueError):
        span = DEFAULT_CONTEXT_LINES
    language = language_of(path)
    if not language:
        suffix = path.rsplit("/", 1)[-1]
        dot = suffix.rfind(".")
        ext = suffix[dot:] if dot > 0 else "(no extension)"
        return EditCheck(
            CHECK_UNCHECKED,
            path,
            kind="unsupported",
            reason=f"no in-edit syntax checker for {ext} files; the content "
            "was NOT checked",
        )
    if not isinstance(src, str):  # defensive: a caller handed non-text
        return EditCheck(
            CHECK_UNCHECKED,
            path,
            kind="unreadable",
            reason=f"content is {type(src).__name__}, not text; NOT checked",
        )
    if "\x00" in src:
        return EditCheck(
            CHECK_FAILED,
            path,
            line=0,
            kind="syntax",
            message="the new content contains a NUL byte, so it cannot be "
            "parsed as source",
            reason="a NUL byte is never valid in a source file",
        )

    if language == "python":
        try:
            compile(src, path, "exec")
        except SyntaxError as exc:
            line = exc.lineno or 0
            return EditCheck(
                CHECK_FAILED,
                path,
                line=line,
                kind="syntax",
                message=f"syntax error at line {line}: {exc.msg}"
                if line
                else f"syntax error: {exc.msg}",
                context=_context_block(src, line, span),
            )
        except (ValueError, MemoryError, RecursionError) as exc:
            return EditCheck(
                CHECK_FAILED,
                path,
                kind="syntax",
                message=f"the new content could not be compiled: "
                f"{type(exc).__name__}: {exc}",
                reason="compile() refused the content for a reason other "
                "than a syntax error",
            )
        if check_names:
            try:
                findings = check_undefined_names(src, path)
            except Exception as exc:  # a crashing pass is not a pass
                return EditCheck(
                    CHECK_FAILED,
                    path,
                    kind="lint_error",
                    message=f"the lint name pass failed: {exc}",
                    reason="an internal check error, treated as a failure",
                )
            if findings:
                first = findings[0]
                return EditCheck(
                    CHECK_FAILED,
                    path,
                    line=first.line,
                    kind=first.kind,
                    message=f"{first.message} (line {first.line})",
                    context=_context_block(src, first.line, span),
                )
    else:
        which = "js" if language == "javascript" else "ts"
        status, line, message = _ts_syntax_status(
            src.encode("utf-8", errors="replace"), which
        )
        if status == "unavailable":
            return EditCheck(
                CHECK_UNCHECKED,
                path,
                kind="no_checker",
                reason=message + "; the content was NOT checked",
            )
        if status == "error":
            return EditCheck(
                CHECK_FAILED,
                path,
                line=line,
                kind="syntax",
                message=f"syntax error at line {line}: {message}"
                if line
                else f"syntax error: {message}",
                context=_context_block(src, line, span),
            )

    return EditCheck(CHECK_PASSED, path, kind="syntax")


def render_check(check: EditCheck) -> str:
    """Model-facing rendering of an in-edit check.

    Assumes `check` came from `check_source_for_edit`. A `passed` check
    renders as the empty string (it is the ordinary case and must not add
    noise to every successful edit); a `failed` check renders the message
    plus its A,A�3-line context, which is the form a model can self-correct
    from; an `unchecked` or `disabled` check renders the reason, because a
    silent skip is indistinguishable from a pass to whoever reads the
    result.

    **Redaction boundary (decision: redact AT THE BOUNDARY, here).** This is
    the ONE renderer of an in-edit check, and its three consumers are the tool
    result, the `EditOutcome.check_context` receipt and the journal row. A
    failed syntax check on a `.env`-shaped file quotes the offending source, so
    the value is redacted once here instead of at three surfaces. The
    structured `EditCheck` itself is NOT redacted, and
    `EditOutcome.to_dict` redacts the same two fields independently — defence
    in depth, because `check.context` reaches the receipt as a field rather
    than through this string, and a field with no renderer is a field with no
    boundary.
    """
    if check.status == CHECK_PASSED:
        return ""
    if check.status == CHECK_DISABLED:
        return (
            f"[in-edit lint: {check.status} A��,��?? "
            f"{redact_text_for_journal(check.reason, where='lint.render_check.reason')}]"
        )
    if check.status == CHECK_UNCHECKED:
        return (
            f"[in-edit lint: {check.status} A��,��?? "
            f"{redact_text_for_journal(check.reason, where='lint.render_check.reason')}]"
        )
    parts = [
        (
            f"{check.file}:{check.line}: [{check.kind}] "
            f"{redact_text_for_journal(check.message, where='lint.render_check.message')}"
        ).rstrip(": ")
    ]
    if check.context:
        parts.append(
            redact_text_for_journal(check.context, where="lint.render_check.context")
        )
    return "\n".join(parts)
