"""Lint / static-analysis gate (Round 8, Task C).

Purpose: catch syntax and name-resolution errors in the agent's edits in
MILLISECONDS (host-side AST pass over the changed files) instead of
paying for a full sandboxed pytest verify cycle to discover the same
thing. Wired into the loop controller so a lint failure short-circuits
to a fix attempt (the step gets the classified lint errors as feedback)
before burning a verify cycle.

What it checks (stdlib-only — NO new dependencies, works offline and
in every CI cell):
1. syntax          — compile() per changed .py file (same class the
                      editor gate already runs, reported here with line
                      numbers as a LintFinding).
2. undefined_name  — a conservative single-file scope resolver that
                      flags names USED at module level but never defined
                      anywhere in the file (the import-time NameError
                      class: `_DAYS_PER_MONTHS` vs `_DAYS_PER_MONTH`,
                      a deleted import, a renamed module constant).

Why module-level-only for undefined names: local control flow
(conditional assignment, late imports, decorators, comprehension
scoping) makes single-file FUNCTION-body analysis false-positive-prone,
and the cost model is asymmetric — a missed real error costs one verify
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

Config keys (project convention — no hardcoded knobs):
  lint_gate  (bool, default True) — enables the pre-verify gate
  lint_names (bool, default True) — enables the undefined-name pass
                (syntax is always checked; a repo whose style defeats
                the name pass can pin just that half off)

Failure semantics: lint does NOT replace verify — it only short-circuits
the verify call when it KNOWS the code is broken; it never gates
success. Verifier-gated completion stays absolute (spec item 17).
"""

import ast
import builtins
from pathlib import Path
from typing import List, NamedTuple, Set, Tuple

__all__ = ["LintFinding", "lint_file", "lint_changed", "render_findings"]


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
# definition collection (over-approximation — see module docstring)
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
        # function params (incl. nested/lambda) — suppress uses of them
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
    """Load-name sets for the module's top-level statements — grouped
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


def check_syntax(src: str, rel: str) -> List[LintFinding]:
    """Syntax check; at most one finding per file. Language-aware:
    .py -> compile(); .js/.jsx/.mjs/.cjs -> tree-sitter JS grammar;
    .ts/.tsx -> tree-sitter TS grammar. Assumes rel is the repo-relative
    path used in messages. JS/TS parsing needs the tree-sitter grammar
    packages on the HOST (same ones the code graph uses); without them
    the check yields NO findings (false-negative biased — the sandbox
    verify still catches real syntax errors, and the gate must never
    block a verifiably-correct fix over host tooling)."""
    js_exts = (".js", ".jsx", ".mjs", ".cjs")
    ts_exts = (".ts", ".tsx")
    if rel.endswith(js_exts):
        ok, msg = _ts_syntax_ok(src.encode("utf-8", errors="replace"), "js")
        if not ok:
            return [LintFinding(rel, 0, "syntax", f"syntax error: {msg}")]
        return []
    if rel.endswith(ts_exts):
        ok, msg = _ts_syntax_ok(src.encode("utf-8", errors="replace"), "ts")
        if not ok:
            return [LintFinding(rel, 0, "syntax", f"syntax error: {msg}")]
        return []
    try:
        compile(src, rel, "exec")
    except SyntaxError as e:
        at = f" at line {e.lineno}" if e.lineno else ""
        return [LintFinding(rel, e.lineno or 0, "syntax", f"syntax error{at}: {e.msg}")]
    return []


def _ts_syntax_ok(source: bytes, which: str) -> Tuple[bool, str]:
    """Parse JS/TS source with tree-sitter; (ok, error). which is 'js' or
    'ts'. Grammar missing on host -> (True, '') (documented bias)."""
    try:
        from tree_sitter import Language, Parser

        if which == "js":
            import tree_sitter_javascript as pkg

            lang = Language(pkg.language())
        else:
            import tree_sitter_typescript as pkg

            lang = Language(pkg.language_typescript())
        parser = Parser(lang)
    except Exception:
        return True, ""
    tree = parser.parse(source)
    if not tree.root_node.has_error:
        return True, ""
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        if n.type == "ERROR" or n.is_missing:
            return False, f"parse error at line {n.start_point[0] + 1}"
        stack.extend(c for c in n.children if c is not None)
    return False, "parse error"


def check_undefined_names(src: str, rel: str) -> List[LintFinding]:
    """Names used at module level but defined nowhere in the file or
    builtins. Assumes src parses (run check_syntax first). False-negative
    biased — see module docstring for the suppression rules."""
    tree = ast.parse(src)
    defs = _Defs()
    defs.visit(tree)
    if defs.star_import:
        return []
    findings: List[LintFinding] = []
    seen: Set[str] = set()
    for stmt, loads in zip(tree.body, _module_level_loads(tree)):
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
                    f"undefined name '{name}' at module level — not defined "
                    f"or imported anywhere in this file",
                )
            )
    return findings


def lint_file(src: str, rel: str, check_names: bool = True) -> List[LintFinding]:
    """Lint one file's source; syntax first (language-aware — see
    check_syntax), names only if it parses. Assumes rel is the
    repo-relative path for messages; the undefined-NAME pass stays
    Python-only (JS/TS name resolution needs runtime semantics the
    single-file pass can't approximate safely — documented in
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
    """Lint the agent's CHANGED files (vs pristine) — the fast pre-verify
    gate. Assumes changed is editor.changed_files() output (repo-relative
    posix paths) and work_dir is the working copy root. Never raises: an
    unreadable file is reported as a finding, not an exception."""
    findings: List[LintFinding] = []
    for rel in changed:
        try:
            p = Path(work_dir, rel)
            if not p.is_file():
                continue  # deleted file — nothing to lint
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
    short-circuit message). Assumes findings came from lint_changed."""
    if not findings:
        return ""
    lines = ["LINT FAILED — fix these before re-submitting:"]
    for f in findings:
        lines.append(f"  {f.file}:{f.line}: [{f.kind}] {f.message}")
    lines.append(
        "These were found by a fast static check — fix them first; "
        "the full test suite has not run yet."
    )
    return "\n".join(lines)
