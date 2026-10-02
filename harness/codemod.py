"""AST codemod plans: rename a symbol or change a signature, as a REVIEWABLE plan.

**A codemod here is a plan, not an action.** :func:`rename_symbol` and
:func:`update_signature` return a :class:`CodemodPlan` â€” a complete list of
exact, per-line text replacements plus a :class:`CodemodReceipt`. Nothing in
this module writes a file. Applying a plan goes through :func:`apply_plan`,
which dispatches every single replacement through the run's ordinary
``SafeToolBackend.execute("edit", ...)`` call, so it inherits the ordinary
digest precondition, the ordinary unique-match guard, the ordinary
protected-path policy, and the ordinary undo journal. A codemod that opened
files itself would bypass all of that, and would be forbidden.

**Why the two existing extractions, and no second parser.** Which symbol is
meant, and which module it belongs to, comes from the tree-sitter structural
index in ``memory.code_graph`` (this project's one index). Which lines and
columns a symbol occupies comes from stdlib ``ast``, which is the same parser
``runtime.symbols._python_symbols`` and ``harness.lint`` already use for
Python. ``runtime.symbols`` is the declared language surface: its suffix sets
are what decides that Python is reliable and JS/TS is not. No third grammar,
no regular-expression "parser", and no duplicate index is introduced.

**Completeness is the product.** A rename that silently missed a dynamic call
site is *worse* than no rename, because the code now looks renamed. So every
plan carries the set of sites it could not resolve, each NAMED with a path, a
line, a kind, and a human reason. Unresolved sites are in two classes:

``blocking``
    The rename cannot honestly claim to be complete: an identifier occurrence
    with no resolvable role, an attribute access whose receiver could not be
    tied to the target module, a file that would not parse, a definition that
    is ambiguous, a source span that is not uniquely addressable, or a call
    whose ``*args``/``**kwargs`` splats hide which argument occupies a
    removed parameter's position. :func:`apply_plan` refuses such a plan
    unless the caller explicitly passes ``allow_incomplete=True``.

``advisory``
    A real textual reference the plan deliberately does not rewrite, because
    rewriting it is a human's editorial decision: a comment, or a string /
    docstring. These are still NAMED in the receipt â€” they are simply not
    grounds for refusing.

**Languages without reliable extraction refuse.** Only Python (``.py`` /
``.pyi``) has identifier-level extraction here, so it is the only language in
:data:`RELIABLE_LANGUAGES`. JavaScript/TypeScript are recognised and refused
by name, with the reason that the available extraction there is a
conservative top-level declaration scan which cannot enumerate call sites,
imports, or string references. A refusal happens BEFORE any site is
collected, so an unsupported-language codemod cannot partially apply.
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "BLOCKING_UNRESOLVED_KINDS",
    "LANGUAGE_JAVASCRIPT",
    "LANGUAGE_PYTHON",
    "LANGUAGE_TYPESCRIPT",
    "LANGUAGE_UNKNOWN",
    "OPERATIONS",
    "OP_RENAME",
    "OP_SIGNATURE",
    "RELIABLE_LANGUAGES",
    "SCHEMA_VERSION",
    "SITE_ASSIGNMENT",
    "SITE_ATTRIBUTE",
    "SITE_CALL",
    "SITE_DEFINITION",
    "SITE_IMPORT",
    "SITE_KEYWORD",
    "SITE_KINDS",
    "SITE_PARAMETER",
    "SITE_REFERENCE",
    "SITE_SIGNATURE",
    "UNRESOLVED_AMBIGUOUS_DEFINITION",
    "UNRESOLVED_AMBIGUOUS_SPAN",
    "UNRESOLVED_ATTRIBUTE_RECEIVER",
    "UNRESOLVED_COMMENT",
    "UNRESOLVED_DYNAMIC_LOOKUP",
    "UNRESOLVED_KINDS",
    "UNRESOLVED_OTHER_REFERENCE",
    "UNRESOLVED_OTHER_SYMBOL",
    "UNRESOLVED_SPLAT_ARGUMENTS",
    "UNRESOLVED_STRING_REFERENCE",
    "UNRESOLVED_UNPARSED_FILE",
    "UNRESOLVED_UNSUPPORTED_LANGUAGE",
    "CodemodApplyResult",
    "CodemodConfig",
    "CodemodOutcome",
    "CodemodPlan",
    "CodemodReceipt",
    "CodemodSite",
    "PlannedEdit",
    "UnresolvedSite",
    "apply_plan",
    "config_from",
    "language_of",
    "language_support",
    "plan_codemod",
    "rename_symbol",
    "render_plan",
    "render_receipt",
    "run_codemod",
    "update_signature",
]

SCHEMA_VERSION = 1

OP_RENAME = "rename_symbol"
OP_SIGNATURE = "update_signature"
OPERATIONS: Tuple[str, ...] = (OP_RENAME, OP_SIGNATURE)

# ---------------------------------------------------------------------------
# languages
# ---------------------------------------------------------------------------

LANGUAGE_PYTHON = "python"
LANGUAGE_JAVASCRIPT = "javascript"
LANGUAGE_TYPESCRIPT = "typescript"
LANGUAGE_UNKNOWN = "unknown"

#: The only languages this module can resolve identifier-level sites for. A
#: codemod in anything else refuses with a reason instead of half-applying.
RELIABLE_LANGUAGES: Tuple[str, ...] = (LANGUAGE_PYTHON,)

_PY_SUFFIXES = (".py", ".pyi")
_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")

#: Directory names never walked by the candidate scan. Deliberately mirrors
#: the memory module's structural-index skip list rather than importing it:
#: ``memory`` is a separate module's internals, and a hard dependency on a
#: private constant would make a rename's scope depend on that module's
#: refactors. It also supplies the language suffix sets above, which is the
#: documented reason ``runtime.symbols`` treats Python as precise.
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "env",
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        "node_modules",
        ".harness",
        "logs",
        "build",
        "dist",
        "docs",
        "site",
    }
)

#: Files larger than this are not candidates; they are recorded as unresolved
#: rather than silently skipped.
MAX_FILE_BYTES = 1_000_000

# ---------------------------------------------------------------------------
# site kinds
# ---------------------------------------------------------------------------

SITE_DEFINITION = "definition"
SITE_CALL = "call"
SITE_REFERENCE = "reference"
SITE_ASSIGNMENT = "assignment_target"
SITE_ATTRIBUTE = "attribute_reference"
SITE_IMPORT = "import"
SITE_KEYWORD = "keyword_argument"
SITE_PARAMETER = "parameter"
SITE_SIGNATURE = "signature"

SITE_KINDS: Tuple[str, ...] = (
    SITE_DEFINITION,
    SITE_CALL,
    SITE_REFERENCE,
    SITE_ASSIGNMENT,
    SITE_ATTRIBUTE,
    SITE_IMPORT,
    SITE_KEYWORD,
    SITE_PARAMETER,
    SITE_SIGNATURE,
)

# ---------------------------------------------------------------------------
# unresolved kinds
# ---------------------------------------------------------------------------

UNRESOLVED_OTHER_REFERENCE = "other_reference"
UNRESOLVED_ATTRIBUTE_RECEIVER = "attribute_receiver_unknown"
UNRESOLVED_DYNAMIC_LOOKUP = "dynamic_symbol_lookup"
UNRESOLVED_OTHER_SYMBOL = "other_symbol_with_same_name"
UNRESOLVED_COMMENT = "comment_reference"
UNRESOLVED_STRING_REFERENCE = "string_reference"
UNRESOLVED_UNPARSED_FILE = "unparsed_file"
UNRESOLVED_AMBIGUOUS_SPAN = "ambiguous_span"
UNRESOLVED_AMBIGUOUS_DEFINITION = "ambiguous_definition"
UNRESOLVED_SPLAT_ARGUMENTS = "splat_arguments"
UNRESOLVED_UNSUPPORTED_LANGUAGE = "unsupported_language"

#: Unresolved kinds that make a plan refuse to apply. Every kind is NAMED in
#: the receipt; these are the ones that mean "this change might be incomplete".
#: A same-named symbol in a DIFFERENT module is deliberately NOT here: it is
#: provably a different symbol, so naming it is information rather than a
#: blocker. A bare reference in a file that never imported the target IS here,
#: because a re-export or a namespace injection could make it the same symbol.
BLOCKING_UNRESOLVED_KINDS: Tuple[str, ...] = (
    UNRESOLVED_OTHER_REFERENCE,
    UNRESOLVED_ATTRIBUTE_RECEIVER,
    UNRESOLVED_DYNAMIC_LOOKUP,
    UNRESOLVED_UNPARSED_FILE,
    UNRESOLVED_AMBIGUOUS_SPAN,
    UNRESOLVED_AMBIGUOUS_DEFINITION,
    UNRESOLVED_SPLAT_ARGUMENTS,
    UNRESOLVED_UNSUPPORTED_LANGUAGE,
)

UNRESOLVED_KINDS: Tuple[str, ...] = tuple(
    sorted(
        set(BLOCKING_UNRESOLVED_KINDS)
        | {UNRESOLVED_OTHER_SYMBOL, UNRESOLVED_COMMENT, UNRESOLVED_STRING_REFERENCE}
    )
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _word_re(name: str) -> "re.Pattern[str]":
    """Return a cached word-boundary pattern for one identifier."""
    pattern = _WORD_RE_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        _WORD_RE_CACHE[name] = pattern
    return pattern


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _module_name(rel: str) -> str:
    """Return the dotted module name for a repo-relative path.

    ``harness/core.py`` -> ``harness.core``; ``harness/__init__.py`` ->
    ``harness``. The same convention ``memory.code_graph`` documents; kept
    local so a codemod's scope does not depend on another module's private
    helper.
    """
    parts = str(rel or "").replace("\\", "/").split("/")
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(part for part in parts if part)


def _dotted(node: ast.AST) -> str:
    """Return the dotted text of a pure ``Name``/``Attribute`` chain, else ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodemodConfig:
    """Bounded knobs for one codemod plan.

    Every field is read from ``Task.config`` by :func:`config_from` and has a
    bounded internal default. **Nothing here is added to
    ``harness/config.py`` ``DEFAULTS`` by this module** — a default in
    ``DEFAULTS`` is merged into every task and every eval arm, so one would
    silently switch every run. ``harness/AGENTS.md`` carries the request to
    publish the discoverability entries.
    """

    scan_max_files: int = 2_000
    max_index_files: int = 300
    max_sites: int = 2_000
    edit_context_lines: int = 2
    include_paths: Tuple[str, ...] = ()
    follow_string_references: bool = False

    def clamped(self) -> Tuple["CodemodConfig", List[str]]:
        """Return this config with out-of-range values clamped, plus the notes.

        A clamp is always reported, never silently applied: a caller that
        asked for a million files learns that it got a bound instead.
        """
        notes: List[str] = []
        scan = max(1, min(int(self.scan_max_files), 200_000))
        index = max(1, min(int(self.max_index_files), 50_000))
        sites = max(1, min(int(self.max_sites), 200_000))
        context = max(0, min(int(self.edit_context_lines), 20))
        if scan != int(self.scan_max_files):
            notes.append(f"codemod_scan_max_files={self.scan_max_files} -> {scan}")
        if index != int(self.max_index_files):
            notes.append(f"codemod_max_index_files={self.max_index_files} -> {index}")
        if sites != int(self.max_sites):
            notes.append(f"codemod_max_sites={self.max_sites} -> {sites}")
        if context != int(self.edit_context_lines):
            notes.append(
                f"codemod_edit_context_lines={self.edit_context_lines} -> {context}"
            )
        return (
            CodemodConfig(
                scan_max_files=scan,
                max_index_files=index,
                max_sites=sites,
                edit_context_lines=context,
                include_paths=tuple(self.include_paths or ()),
                follow_string_references=bool(self.follow_string_references),
            ),
            notes,
        )
        return (
            CodemodConfig(
                scan_max_files=scan,
                max_sites=sites,
                edit_context_lines=context,
                include_paths=tuple(self.include_paths or ()),
                follow_string_references=bool(self.follow_string_references),
            ),
            notes,
        )


def config_from(
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[CodemodConfig, List[str]]:
    """Read codemod knobs from a ``Task.config`` mapping.

    Assumes ``config`` is the merged task config; an absent key means the
    module default. Returns ``(config, notes)`` where ``notes`` records every
    clamp or unusable value, so a misconfiguration is diagnosable rather than
    invisible. A non-integer bound raises ``TypeError`` rather than being
    coerced: a knob that silently became a different number is a knob nobody
    can audit.
    """
    raw = dict(config or {})

    def _int(key: str, default: int) -> int:
        if key not in raw:
            return default
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{key} must be an integer; {value!r} is not usable")
        return int(value)

    includes = raw.get("codemod_include_paths") or ()
    if isinstance(includes, str):
        includes = (includes,)
    return CodemodConfig(
        scan_max_files=_int("codemod_scan_max_files", 2_000),
        max_index_files=_int("codemod_max_index_files", 300),
        max_sites=_int("codemod_max_sites", 2_000),
        edit_context_lines=_int("codemod_edit_context_lines", 2),
        include_paths=tuple(str(item) for item in includes),
        follow_string_references=bool(
            raw.get("codemod_follow_string_references", False)
        ),
    ).clamped()


# ---------------------------------------------------------------------------
# value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodemodSite:
    """One resolved, addressable site a codemod will rewrite.

    ``scope`` is the AST symbol that encloses the site, taken from the EXISTING
    extraction in ``runtime.symbols`` rather than from a second walk here. It
    is what makes a receipt claim-accurate: a caller can see that a site is
    inside ``Registry.render`` rather than at module level, which is the same
    granularity the parallel-edit claim system reports.
    """

    path: str
    line: int
    column: int
    kind: str
    new_text: str
    detail: str = ""
    scope: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible site record."""
        return {
            "path": self.path,
            "line": int(self.line),
            "column": int(self.column),
            "kind": self.kind,
            "new_text": self.new_text,
            "detail": self.detail,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class UnresolvedSite:
    """One site the codemod could NOT resolve, named rather than skipped."""

    path: str
    line: int
    kind: str
    detail: str
    excerpt: str = ""

    @property
    def blocking(self) -> bool:
        """Return whether this unresolved site prevents a complete rename."""
        return self.kind in BLOCKING_UNRESOLVED_KINDS

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible unresolved-site record."""
        return {
            "path": self.path,
            "line": int(self.line),
            "kind": self.kind,
            "detail": self.detail,
            "excerpt": self.excerpt,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class PlannedEdit:
    """One exact text replacement, addressed for the ordinary edit path.

    ``old_string`` is a whole source line (widened by up to
    ``codemod_edit_context_lines`` neighbouring lines when one line is not
    uniquely addressable) with every span on that line already substituted
    into ``new_string``. It is therefore directly usable as the
    ``old_string``/``new_string`` pair of an ordinary ``edit`` call, and the
    ordinary unique-match guard applies to it unchanged.
    """

    path: str
    line: int
    kind: str
    old_string: str
    new_string: str
    expected_sha256: str
    site_lines: Tuple[int, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible edit record (never the file contents)."""
        return {
            "path": self.path,
            "line": int(self.line),
            "kind": self.kind,
            "expected_sha256": self.expected_sha256,
            "site_lines": list(self.site_lines),
            "old_chars": len(self.old_string),
            "new_chars": len(self.new_string),
        }


@dataclass(frozen=True)
class CodemodReceipt:
    """The completeness receipt every codemod carries.

    ``files_considered`` is the honest denominator: it counts every file the
    scan looked at, not only the files that happened to change.
    ``unresolved`` is the named set of sites the codemod could not resolve.
    """

    operation: str
    symbol: str
    language: str
    resolution: str
    index_source: str
    files_considered: int = 0
    files_changed: int = 0
    sites_found: int = 0
    sites_planned: int = 0
    sites_changed: int = 0
    unresolved: Tuple[UnresolvedSite, ...] = ()
    notes: Tuple[str, ...] = ()
    refused: bool = False
    reason: str = ""

    @property
    def blocking_unresolved(self) -> Tuple[UnresolvedSite, ...]:
        """Return the unresolved sites that make this rename incomplete."""
        return tuple(item for item in self.unresolved if item.blocking)

    @property
    def advisory_unresolved(self) -> Tuple[UnresolvedSite, ...]:
        """Return the named-but-non-blocking unresolved sites."""
        return tuple(item for item in self.unresolved if not item.blocking)

    @property
    def complete(self) -> bool:
        """Return whether the plan may claim to be a complete change set."""
        return not self.refused and not self.blocking_unresolved

    def with_applied(self, files_changed: int, sites_changed: int) -> "CodemodReceipt":
        """Return this receipt with the measured apply counts filled in."""
        return CodemodReceipt(
            operation=self.operation,
            symbol=self.symbol,
            language=self.language,
            resolution=self.resolution,
            index_source=self.index_source,
            files_considered=self.files_considered,
            files_changed=int(files_changed),
            sites_found=self.sites_found,
            sites_planned=self.sites_planned,
            sites_changed=int(sites_changed),
            unresolved=self.unresolved,
            notes=self.notes,
            refused=self.refused,
            reason=self.reason,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        blocking = self.blocking_unresolved
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "symbol": self.symbol,
            "language": self.language,
            "resolution": self.resolution,
            "index_source": self.index_source,
            "files_considered": int(self.files_considered),
            "files_changed": int(self.files_changed),
            "sites_found": int(self.sites_found),
            "sites_planned": int(self.sites_planned),
            "sites_changed": int(self.sites_changed),
            "complete": self.complete,
            "unresolved_count": len(self.unresolved),
            "blocking_unresolved_count": len(blocking),
            "unresolved": [item.to_dict() for item in self.unresolved],
            "notes": list(self.notes),
            "refused": self.refused,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CodemodPlan:
    """A complete, reviewable change set plus its receipt. Never applies."""

    operation: str
    repo_path: str
    symbol: str
    new_name: str
    language: str
    edits: Tuple[PlannedEdit, ...] = ()
    sites: Tuple[CodemodSite, ...] = ()
    receipt: CodemodReceipt = field(
        default_factory=lambda: CodemodReceipt("", "", "", "", "")
    )
    definition: str = ""
    signature: str = ""
    detail: str = ""

    @property
    def refused(self) -> bool:
        """Return whether the codemod refused outright and changed nothing."""
        return self.receipt.refused

    @property
    def complete(self) -> bool:
        """Return whether the plan may be applied without an override."""
        return self.receipt.complete

    def files(self) -> Tuple[str, ...]:
        """Return the repo-relative paths this plan would change."""
        return tuple(dict.fromkeys(edit.path for edit in self.edits))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible plan, without any file contents."""
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "repo_path": self.repo_path,
            "symbol": self.symbol,
            "new_name": self.new_name,
            "language": self.language,
            "definition": self.definition,
            "signature": self.signature,
            "detail": self.detail,
            "refused": self.refused,
            "complete": self.complete,
            "edits": [edit.to_dict() for edit in self.edits],
            "sites": [site.to_dict() for site in self.sites],
            "receipt": self.receipt.to_dict(),
        }

    def with_applied(self, applied: "CodemodApplyResult") -> "CodemodPlan":
        """Return this plan with the MEASURED apply counts folded into the receipt.

        A plan on its own has changed nothing, so its ``sites changed`` is
        zero. Folding the apply result in is what makes the receipt state what
        actually happened rather than what was proposed.
        """
        return CodemodPlan(
            operation=self.operation,
            repo_path=self.repo_path,
            symbol=self.symbol,
            new_name=self.new_name,
            language=self.language,
            edits=self.edits,
            sites=self.sites,
            receipt=self.receipt.with_applied(
                applied.files_changed, applied.sites_changed
            ),
            definition=self.definition,
            signature=self.signature,
            detail=self.detail,
        )


@dataclass(frozen=True)
class CodemodApplyResult:
    """What the ordinary edit path did with a plan."""

    ok: bool
    files_changed: int
    sites_changed: int
    applied: Tuple[Dict[str, Any], ...] = ()
    undo_ids: Tuple[str, ...] = ()
    rolled_back: bool = False
    rollback_failures: Tuple[Dict[str, str], ...] = ()
    error: str = ""
    error_kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible apply result."""
        return {
            "ok": self.ok,
            "files_changed": int(self.files_changed),
            "sites_changed": int(self.sites_changed),
            "applied": [dict(item) for item in self.applied],
            "undo_ids": list(self.undo_ids),
            "rolled_back": self.rolled_back,
            "rollback_failures": [dict(item) for item in self.rollback_failures],
            "error": self.error,
            "error_kind": self.error_kind,
        }


@dataclass(frozen=True)
class CodemodOutcome:
    """A plan plus its apply result â€” the complete story of one codemod."""

    plan: CodemodPlan
    applied: Optional[CodemodApplyResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible outcome."""
        return {
            "plan": self.plan.to_dict(),
            "apply": self.applied.to_dict() if self.applied is not None else None,
        }


# ---------------------------------------------------------------------------
# language support
# ---------------------------------------------------------------------------


def language_of(path: str) -> str:
    """Return the language id for a repo-relative path.

    Assumes ``path`` is a path string; an unrecognised extension (or none)
    yields :data:`LANGUAGE_UNKNOWN`, which is never a reliable language.
    """
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _PY_SUFFIXES:
        return LANGUAGE_PYTHON
    if suffix in _JS_SUFFIXES:
        return LANGUAGE_JAVASCRIPT
    if suffix in _TS_SUFFIXES:
        return LANGUAGE_TYPESCRIPT
    return LANGUAGE_UNKNOWN


def language_support(path: str) -> str:
    """Return ``""`` when a path's language is reliably extractable, else why not.

    Assumes ``path`` is a repository-relative path whose suffix selects the
    language; it does not read the file, so a path that does not exist is
    answered from its extension alone. A non-empty return is the refusal
    reason. The reason always names the language and the missing extraction, so
    a caller can tell "I do not support this" apart from "I found nothing".
    """
    language = language_of(path)
    if language in RELIABLE_LANGUAGES:
        return ""
    conservative = (
        "the available extraction (runtime.symbols) is a conservative "
        "top-level declaration scan that cannot enumerate call sites, "
        "imports, or string references, so a change could not be shown to be "
        "complete"
    )
    if language == LANGUAGE_JAVASCRIPT:
        return f"javascript has no reliable site extraction here: {conservative}"
    if language == LANGUAGE_TYPESCRIPT:
        return f"typescript has no reliable site extraction here: {conservative}"
    if language == LANGUAGE_UNKNOWN:
        return (
            "unrecognised file type: this module resolves identifier-level "
            "sites for Python (.py/.pyi) only, because that is the only "
            "extraction in this repository that enumerates definitions, call "
            "sites, imports, and references precisely"
        )
    return f"{language} is not a supported codemod language"


# ---------------------------------------------------------------------------
# the structural index (memory.code_graph)
# ---------------------------------------------------------------------------


def _load_index(repo_path: str) -> Tuple[Any, str]:
    """Return ``(index, source)`` for the repository's structural index.

    ``index`` is a loaded ``memory.code_graph.CodeGraph`` or ``None``;
    ``source`` is ``"code_graph"`` or a reason it is unavailable. The index is
    imported lazily so a missing or broken memory layer degrades to an honest
    refusal rather than an import error at module load â€” the same
    key-presence seam shape the rest of the tree uses.
    """
    try:
        from memory.code_graph import CodeGraph
    except Exception as exc:
        return None, f"code_graph_unavailable: {exc}"
    try:
        index = CodeGraph(repo_path)
        index.load_or_build()
        return index, "code_graph"
    except Exception as exc:
        return None, f"code_graph_unavailable: {exc}"


def _index_file_module(index: Any, rel_path: str) -> str:
    """Return the dotted module name the index assigns to a file, or ``""``."""
    try:
        nodes = list(index.graph.nodes.values())
    except Exception:
        return ""
    for node in nodes:
        if (
            getattr(node, "kind", "") == "module"
            and getattr(node, "file", "") == rel_path
        ):
            return str(getattr(node, "qualified", ""))
    return ""


def _resolve_definition(
    index: Any, symbol: str, path: Optional[str]
) -> Tuple[List[Any], str]:
    """Return the index's exact definition nodes for ``symbol``.

    Assumes ``index`` is a loaded ``memory.code_graph.CodeGraph``. Returns
    ``(nodes, resolution)``; an empty list with resolution
    ``"not_found_in_path"`` or ``"not_found"`` means the symbol is not
    resolvable, which is an honest "not found", not a guess.
    """
    try:
        nodes = list(index.exact_symbols(symbol))
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"index_query_failed: {exc}"
    if path:
        wanted = str(path).replace("\\", "/")
        scoped = [node for node in nodes if getattr(node, "file", "") == wanted]
        if scoped:
            return scoped, "path_scoped"
        return [], "not_found_in_path"
    distinct = {getattr(node, "qualified", "") for node in nodes}
    if len(distinct) <= 1:
        return nodes, "unique_definition"
    return nodes, "ambiguous_definition"


# ---------------------------------------------------------------------------
# file enumeration and reading
# ---------------------------------------------------------------------------


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Return whether any component of ``path`` under ``root`` is a symlink."""
    try:
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                return True
    except (OSError, ValueError):
        return True
    return False


def _iter_source_files(root: Path, include: Sequence[str]) -> List[str]:
    """Return the repo-relative Python source files a scan may examine.

    Assumes ``root`` is an existing directory. ``include``, when non-empty,
    restricts the walk to those repo-relative prefixes. Symlinked components
    are refused â€” a rename must not resolve through a link it cannot see
    through â€” and skip-listed directories are never descended. The walk is a
    full enumeration on purpose: a file that failed to parse is exactly where
    a missed call site would hide, so the completeness denominator has to
    include files the structural index skipped.
    """
    wanted = [str(item).replace("\\", "/").strip("/") for item in include or ()]
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        )
        base = Path(dirpath)
        for filename in sorted(filenames):
            if Path(filename).suffix.lower() not in _PY_SUFFIXES:
                continue
            full = base / filename
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - os.walk stays under root
                continue
            if wanted and not any(
                rel == prefix or rel.startswith(prefix + "/") for prefix in wanted
            ):
                continue
            if _has_symlink_component(full, root):
                continue
            found.append(rel)
    return found


def _read_source(root: Path, rel: str) -> Tuple[Optional[str], Optional[str], str]:
    """Return ``(text, sha256, error)`` for one repo-relative source file.

    ``sha256`` is the plain SHA-256 of the file bytes, which is exactly what
    ``execution.workspace.FileRevision.sha256`` carries, so it is a valid
    ``expected_revision`` for the ordinary edit path.
    """
    full = root / rel
    try:
        data = full.read_bytes()
    except OSError as exc:
        return None, None, f"unreadable: {exc}"
    if len(data) > MAX_FILE_BYTES:
        return None, None, f"too large to scan ({len(data)} bytes)"
    if b"\x00" in data:
        return None, None, "binary file refused"
    try:
        return data.decode("utf-8"), _sha256(data), ""
    except UnicodeDecodeError as exc:
        return None, None, f"not valid UTF-8: {exc}"


# ---------------------------------------------------------------------------
# span plumbing
# ---------------------------------------------------------------------------


def _char_column(line: str, byte_offset: int) -> int:
    """Convert an ``ast`` UTF-8 byte column into a character column."""
    prefix = line.encode("utf-8")[:byte_offset]
    try:
        return len(prefix.decode("utf-8"))
    except UnicodeDecodeError:  # pragma: no cover - defensive
        return len(prefix)


def _name_col_in_line(line: str, start_col: int, name: str) -> int:
    """Return the character column of ``name`` on a declaration line, or ``-1``.

    Assumes ``start_col`` is the node's own column, so a parameter list or a
    return annotation that happens to contain the same word cannot be mistaken
    for the declaration's name. Returns ``-1`` rather than a guess.
    """
    if not name:
        return -1
    index = max(0, start_col)
    while True:
        found = line.find(name, index)
        if found < 0:
            return -1
        before = line[found - 1] if found > 0 else ""
        after = line[found + len(name)] if found + len(name) < len(line) else ""
        if not (before.isalnum() or before == "_") and not (
            after.isalnum() or after == "_"
        ):
            return found
        index = found + 1


@dataclass(frozen=True)
class _Span:
    """An internal exact source span, in 1-based lines and 0-based columns."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int
    new_text: str
    kind: str
    detail: str = ""

    @property
    def key(self) -> Tuple[int, int]:
        return (self.start_line, self.start_col)


def _line_offsets(text: str) -> List[Tuple[int, int, int]]:
    """Return ``(start, content_end, end)`` char offsets for each 1-based line.

    ``content_end`` excludes the line terminator and ``end`` includes it, so a
    caller can slice a whole-line range out of the ORIGINAL text and keep the
    file's own terminators. That matters: a repository checked out on Windows
    has ``\\r\\n`` line endings, and an edit block re-joined with ``"\\n"``
    would not occur in the file at all — every span would look ambiguous and
    the plan would refuse to do ordinary work.
    """
    offsets: List[Tuple[int, int, int]] = []
    position = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        offsets.append((position, position + len(stripped), position + len(line)))
        position += len(line)
    return offsets


def _offset_of(text: str, line_index: int, column: int) -> int:
    """Return the character offset of ``column`` on 0-based ``line_index``.

    Assumes ``text`` is a block of whole lines. The column is clamped to the
    target line's content length so a column taken from a line that carries a
    terminator can never address the terminator itself.
    """
    if line_index < 0:
        return 0
    offset = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        if index == line_index:
            return offset + min(max(0, column), len(line.rstrip("\r\n")))
        offset += len(line)
    return offset


def _apply_spans(text: str, spans: Sequence[_Span], offset: int) -> str:
    """Return ``text`` with every span's text replaced.

    Assumes ``spans`` are sorted by start column and non-overlapping, and
    ``offset`` is the 0-based index within the FILE of ``text``'s first line
    (so a file line number L is at 0-based index ``L - 1 - offset``).
    """
    out: List[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda item: (item.start_line, item.start_col)):
        start = _offset_of(text, span.start_line - 1 - offset, span.start_col)
        end = _offset_of(text, span.end_line - 1 - offset, span.end_col)
        if start < cursor or end < start:
            continue
        out.append(text[cursor:start])
        out.append(span.new_text)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _line_terminator(text: str) -> str:
    """Return the dominant line terminator of ``text`` (``"\\n"`` by default)."""
    return "\r\n" if "\r\n" in text else "\n"


def _excerpt(text: str, lineno: int) -> str:
    lines = text.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:200]
    return ""


def _scope_index(text: str, path: str) -> Dict[int, str]:
    """Return ``line -> enclosing AST symbol name`` for one file.

    This is the EXISTING ``runtime.symbols`` extraction, used for exactly what
    it is good at: mapping a line to the Python symbol that owns it, with real
    ``def``/``class``/method line ranges. It is not reimplemented here, and it
    is not asked to resolve identifiers - that is the role-aware ``ast`` pass
    above, which needs the parse tree this module already holds.

    A failure inside the import is non-blocking: the scope is a reporting
    convenience, and a plan whose sites are correctly located is still correct
    without it. Returns an empty mapping in that case, and never raises.
    """
    try:
        from runtime.symbols import enclosing_symbol, symbols_in_source
    except Exception:  # pragma: no cover - the runtime layer is a hard dep
        return {}
    try:
        spans = symbols_in_source(text, path)
    except Exception:  # pragma: no cover - symbols_in_source never raises
        return {}
    scopes: Dict[int, str] = {}
    for span in spans:
        for line in range(int(span.start_line), int(span.end_line) + 1):
            scopes.setdefault(line, "")
    for line in sorted(scopes):
        try:
            scopes[line] = enclosing_symbol(spans, line)
        except Exception:  # pragma: no cover - defensive
            scopes[line] = ""
    return scopes


# ---------------------------------------------------------------------------
# the per-file rename scanner
# ---------------------------------------------------------------------------


@dataclass
class _FileScan:
    """Everything one file contributed to a plan."""

    rel: str
    text: str
    sha: str
    spans: List[_Span] = field(default_factory=list)
    unresolved: List[UnresolvedSite] = field(default_factory=list)
    module_bindings: Dict[str, str] = field(default_factory=dict)
    parsed: bool = True

    def lines(self) -> List[str]:
        return self.text.splitlines()


class _RenameScanner:
    """Collect every identifier occurrence of the target symbol, precisely.

    Assumes ``old`` is the resolved definition's name, ``role`` says how this
    file relates to it, and ``lines`` are the file's source lines. An
    occurrence is a SITE only when it provably refers to the target:
    a definition or bare reference in the defining module, a bare name the
    file imported from the target module, an attribute whose receiver resolves
    to the target module, or an import statement that imports the target
    module. Everything else is NAMED as unresolved — including a same-named
    definition in an unrelated module and a bare reference in a file that never
    imported the target, which a name-based over-approximation would have
    rewritten and thereby corrupted.

    The traversal is a single explicit stack carrying each node's parent, so
    every node is visited exactly once. A bare ``ast.NodeVisitor`` cannot
    answer "is this ``Name`` the callee of a ``Call``", and a visitor that both
    recursed and was driven by ``ast.walk`` would visit everything twice and
    duplicate every unresolved record.
    """

    def __init__(
        self,
        old: str,
        new: str,
        rel: str,
        lines: Sequence[str],
        role: _FileRole,
        unresolved: List[UnresolvedSite],
    ) -> None:
        self.old = old
        self.new = new
        self.rel = rel
        self.lines = list(lines)
        self.role = role
        self.unresolved = unresolved
        self.spans: List[_Span] = []
        #: Every ``(line, column)`` this scanner already accounted for, whether
        #: it resolved the occurrence into a span or NAMED it as unresolved.
        #: The completeness pass needs this: without it, an occurrence that was
        #: deliberately named would be reported a second time as an unexplained
        #: miss, and the receipt would double-count every finding.
        self.classified: Set[Tuple[int, int]] = set()

    # -- recording helpers ------------------------------------------------

    def line(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def add(self, node: Any, kind: str, detail: str) -> None:
        """Record a span covering a node's own identifier token."""
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = _char_column(self.line(lineno), int(getattr(node, "col_offset", 0) or 0))
        self.classified.add((lineno, col))
        self.spans.append(
            _Span(lineno, col, lineno, col + len(self.old), self.new, kind, detail)
        )

    def add_definition(self, node: Any, kind: str, detail: str) -> None:
        """Record a span covering a definition's NAME token.

        A ``FunctionDef``/``ClassDef`` node's ``col_offset`` points at the
        ``def``/``async def``/``class`` keyword, not at the name, so it cannot
        be used directly: recording it renames the keyword. The name token is
        located on the declaration line instead, from the node's own column
        onwards, and a name that cannot be located is NAMED rather than
        guessed at.
        """
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = _name_col_in_line(
            self.line(lineno), int(getattr(node, "col_offset", 0) or 0), self.old
        )
        if col < 0:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_REFERENCE,
                f"{detail}: the name token could not be located on its "
                "declaration line, so it was not changed",
            )
            return
        self.classified.add((lineno, col))
        self.add_at(lineno, col, kind, detail)

    def add_at(self, lineno: int, col: int, kind: str, detail: str) -> None:
        self.classified.add((int(lineno), int(col)))
        self.spans.append(
            _Span(lineno, col, lineno, col + len(self.old), self.new, kind, detail)
        )

    def unresolved_at(
        self, lineno: int, kind: str, detail: str, col: Optional[int] = None
    ) -> None:
        if col is not None and col >= 0:
            self.classified.add((int(lineno), int(col)))
        self.unresolved.append(
            UnresolvedSite(
                self.rel,
                int(lineno),
                kind,
                detail,
                self.line(int(lineno)).strip()[:200],
            )
        )

    # -- dispatch ---------------------------------------------------------

    def scan(self, tree: ast.Module) -> List[_Span]:
        """Return every resolved span in ``tree``."""
        stack: List[Tuple[ast.AST, Optional[ast.AST]]] = [(tree, None)]
        while stack:
            node, parent = stack.pop()
            self._record(node, parent)
            for child in reversed(list(ast.iter_child_nodes(node))):
                stack.append((child, node))
        return _dedupe_spans(self.spans)

    def _record(self, node: ast.AST, parent: Optional[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == self.old:
                self._record_definition(node, "definition name")
        elif isinstance(node, ast.ClassDef):
            if node.name == self.old:
                self._record_definition(node, "class name")
        elif isinstance(node, ast.arg):
            if node.arg == self.old:
                self._record_parameter(node)
        elif isinstance(node, ast.Name):
            self._record_name(node, parent)
        elif isinstance(node, ast.Attribute):
            self._record_attribute(node)
        elif isinstance(node, ast.Import):
            self._record_import(node)
        elif isinstance(node, ast.ImportFrom):
            self._record_import_from(node)
        elif isinstance(node, ast.keyword):
            self._record_keyword(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            self._record_scope(node, type(node).__name__.lower())
        elif isinstance(node, ast.ExceptHandler):
            self._record_except(node)

    # -- recorders --------------------------------------------------------

    def _record_definition(self, node: Any, detail: str) -> None:
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = _name_col_in_line(
            self.line(lineno), int(getattr(node, "col_offset", 0) or 0), self.old
        )
        if not self.role.is_defining_file:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_SYMBOL,
                f"a DIFFERENT module defines a symbol also named {self.old!r} "
                f"({self.role.own_module}); it is provably not the renamed "
                "symbol, and it was left alone",
                col,
            )
            return
        self.add_definition(node, SITE_DEFINITION, detail)

    def _record_parameter(self, node: Any) -> None:
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = _char_column(self.line(lineno), int(getattr(node, "col_offset", 0) or 0))
        if not self.role.is_defining_file:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_SYMBOL,
                f"a parameter named {self.old!r} in another module; provably not "
                "the renamed symbol, and left alone",
                col,
            )
            return
        self.classified.add((lineno, col))
        self.add_at(lineno, col, SITE_PARAMETER, "parameter name")

    def _record_name(self, node: ast.Name, parent: Optional[ast.AST]) -> None:
        if node.id != self.old:
            return
        if isinstance(parent, ast.Attribute):
            return  # the Attribute node owns this occurrence
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = _char_column(self.line(lineno), int(getattr(node, "col_offset", 0) or 0))
        if not self.role.bare_name_is_target(self.old):
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_REFERENCE,
                f"a bare reference to {self.old!r} in a file that does not "
                f"import it from the defining module '{self.role.target_module}'; "
                "it may be the same symbol through a re-export or a namespace "
                "injection, so it was neither renamed nor proven unrelated",
                col,
            )
            return
        self.classified.add((lineno, col))
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.spans.append(
                _Span(
                    lineno,
                    col,
                    lineno,
                    col + len(self.old),
                    self.new,
                    SITE_ASSIGNMENT,
                    "rebinding of the same name",
                )
            )
        elif isinstance(parent, ast.Call) and parent.func is node:
            self.spans.append(
                _Span(
                    lineno,
                    col,
                    lineno,
                    col + len(self.old),
                    self.new,
                    SITE_CALL,
                    "call site",
                )
            )
        else:
            self.spans.append(
                _Span(
                    lineno,
                    col,
                    lineno,
                    col + len(self.old),
                    self.new,
                    SITE_REFERENCE,
                    "identifier reference",
                )
            )

    def _record_attribute(self, node: ast.Attribute) -> None:
        if node.attr != self.old:
            return
        end_line = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
        end_col = _char_column(
            self.line(end_line), int(getattr(node, "end_col_offset", 0) or 0)
        )
        start = end_col - len(self.old)
        receiver = _dotted(node.value)
        if self.role.receiver_is_target(receiver) and start >= 0:
            self.classified.add((end_line, start))
            self.spans.append(
                _Span(
                    end_line,
                    start,
                    end_line,
                    end_col,
                    self.new,
                    SITE_ATTRIBUTE,
                    f"attribute of module {self.role.resolve_receiver(receiver)}",
                )
            )
            return
        self.unresolved_at(
            node.lineno,
            UNRESOLVED_ATTRIBUTE_RECEIVER,
            f"attribute '.{self.old}' on {receiver or '<expression>'}: the "
            f"receiver does not name module '{self.role.target_module or '?'}', "
            "so this reference was neither renamed nor proven unrelated",
            start,
        )

    def _record_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                if alias.name == self.old:
                    self.unresolved_at(
                        node.lineno,
                        UNRESOLVED_OTHER_REFERENCE,
                        f"`import {alias.name} as {alias.asname}`: a package "
                        "renamed at its last path component cannot be rewritten "
                        "by a member rename",
                    )
                continue
            if alias.name == self.old or alias.name.endswith("." + self.old):
                parent_module = (
                    alias.name.rsplit(".", 1)[0] if "." in alias.name else ""
                )
                if parent_module != self.role.target_module:
                    self.unresolved_at(
                        node.lineno,
                        UNRESOLVED_OTHER_SYMBOL,
                        f"`import {alias.name}` names module '{parent_module}', "
                        f"which is not the defining module "
                        f"'{self.role.target_module}'; provably a different "
                        "module and left alone",
                        self.line(int(node.lineno)).find(self.old),
                    )
                    continue
                self._locate(node.lineno, SITE_IMPORT, "imported module")

    def _record_import_from(self, node: ast.ImportFrom) -> None:
        module = _from_import_module(node, self.role.own_module)
        for alias in node.names:
            if alias.name != self.old:
                continue
            if module != self.role.target_module:
                self.unresolved_at(
                    node.lineno,
                    UNRESOLVED_OTHER_SYMBOL,
                    f"`from {node.module or '.' * node.level} import {self.old}` "
                    f"imports it from '{module or '(relative)'}', not the defining "
                    f"module '{self.role.target_module}'; provably a different "
                    "module and left alone",
                    self.line(int(node.lineno)).find(self.old),
                )
                continue
            detail = (
                f"imported member renamed; the local alias '{alias.asname}' is "
                "left alone"
                if alias.asname and alias.asname != self.old
                else "imported member"
            )
            self._locate(node.lineno, SITE_IMPORT, detail)

    def _locate(self, lineno: int, kind: str, detail: str) -> None:
        head = self.line(lineno).find(self.old)
        if head < 0:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_REFERENCE,
                f"could not locate {self.old!r} in its import statement",
            )
            return
        self.add_at(lineno, head, kind, detail)

    def _record_keyword(self, node: ast.keyword) -> None:
        if node.arg != self.old:
            return
        lineno = int(getattr(node, "lineno", 0) or 0)
        head = self.line(lineno).find(self.old + "=")
        if head < 0:
            head = self.line(lineno).find(self.old)
        if not self.role.is_defining_file and self.old not in self.role.imported_locals:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_SYMBOL,
                f"a keyword argument {self.old}= in a file that does not import "
                f"it from the defining module '{self.role.target_module}'; "
                "provably a different function and left alone",
                head,
            )
            return
        if head < 0:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_REFERENCE,
                f"keyword argument {self.old}= could not be located in its source line",
            )
            return
        self.add_at(lineno, head, SITE_KEYWORD, "keyword argument")

    def _record_scope(self, node: Any, detail: str) -> None:
        if self.old not in (node.names or ()):
            return
        lineno = int(node.lineno)
        head = self.line(lineno).find(f'"{self.old}"')
        if head < 0:
            head = self.line(lineno).find(f"'{self.old}'")
        col = head + 1 if head >= 0 else -1
        if not self.role.is_defining_file:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_SYMBOL,
                f"a {detail} of {self.old!r} in another module; provably not the "
                "renamed symbol and left alone",
                col,
            )
            return
        if head < 0:
            self.unresolved_at(
                lineno, UNRESOLVED_OTHER_REFERENCE, f"{detail}: not located"
            )
            return
        self.add_at(lineno, col, SITE_REFERENCE, detail)

    def _record_except(self, node: ast.ExceptHandler) -> None:
        if node.name != self.old:
            return
        lineno = int(node.lineno)
        head = self.line(lineno).find("as " + self.old)
        col = head + 3 if head >= 0 else -1
        if not self.role.is_defining_file:
            self.unresolved_at(
                lineno,
                UNRESOLVED_OTHER_SYMBOL,
                f"an exception alias {self.old!r} in another module; provably not "
                "the renamed symbol and left alone",
                col,
            )
            return
        if head < 0:
            self.unresolved_at(
                lineno, UNRESOLVED_OTHER_REFERENCE, "exception alias: not located"
            )
            return
        self.add_at(lineno, col, SITE_ASSIGNMENT, "exception alias")


def _dedupe_spans(spans: Sequence[_Span]) -> List[_Span]:
    """Return spans sorted by position with duplicates removed."""
    seen: Set[Tuple[int, int]] = set()
    unique: List[_Span] = []
    for span in sorted(
        spans, key=lambda item: (item.start_line, item.start_col, item.end_col)
    ):
        if span.key in seen:
            continue
        seen.add(span.key)
        unique.append(span)
    return unique


def _module_bindings(node: ast.Module, current: str) -> Dict[str, str]:
    """Return ``local name -> absolute dotted module`` for a file's imports.

    Assumes ``node`` is a parsed module and ``current`` is the file's own
    dotted module name. Relative ``from . import x`` forms are resolved
    against ``current`` using the same dot convention the structural index
    documents. Deliberately local: this is an import convention, not a parser,
    and importing another module's private helper for it would make a rename's
    scope depend on that module's internals.
    """
    bindings: Dict[str, str] = {}
    for child in getattr(node, "body", ()) or ():
        if isinstance(child, ast.Import):
            for alias in child.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    bindings[head] = head
        elif isinstance(child, ast.ImportFrom):
            module = _from_import_module(child, current)
            for alias in child.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                bindings[local] = f"{module}.{alias.name}" if module else alias.name
    return bindings


def _from_import_module(node: Any, current: str) -> str:
    """Return the absolute dotted module of one ``from ... import`` statement.

    Assumes ``node`` is an ``ast.ImportFrom`` and ``current`` is the
    importing file's own dotted module name. Relative forms are resolved
    against the current package with the standard dot convention.
    """
    if not node.level:
        return str(node.module or "")
    base = current.rsplit(".", 1)[0] if "." in current else current
    package = base.split(".")
    trimmed = max(0, len(package) - (node.level - 1))
    prefix = ".".join(package[:trimmed])
    return ".".join(part for part in (prefix, node.module or "") if part)


def _accessible_modules(node: ast.Module, current: str) -> Set[str]:
    """Return every dotted module path a file can name.

    Assumes ``node`` is a parsed module. ``import a.b.c`` makes ``a``, ``a.b``
    and ``a.b.c`` nameable; ``from a.b import c`` makes ``a.b`` nameable as
    well as the member. This is what lets ``a.b.handler`` be told apart from
    ``x.handler`` on an unrelated object — the distinction a name-based
    over-approximation cannot make, and the reason a rename of a common name
    does not silently rewrite an unrelated same-named attribute.
    """
    accessible: Set[str] = set()
    for child in getattr(node, "body", ()) or ():
        if isinstance(child, ast.Import):
            for alias in child.names:
                if alias.asname:
                    accessible.add(alias.name)
                    continue
                parts = alias.name.split(".")
                for index in range(1, len(parts) + 1):
                    accessible.add(".".join(parts[:index]))
        elif isinstance(child, ast.ImportFrom):
            module = _from_import_module(child, current)
            if module:
                accessible.add(module)
            for alias in child.names:
                if alias.name == "*":
                    continue
                accessible.add(f"{module}.{alias.name}" if module else alias.name)
    return accessible


@dataclass
class _FileRole:
    """How one file relates to the symbol a codemod is changing.

    Assumes ``tree`` is a parsed module of ``rel`` and ``target_module`` /
    ``target_symbol`` are the resolved definition's module and name. This is
    what makes a Python codemod precise rather than name-based: a same-named
    definition in an unrelated module, or a bare reference in a file that never
    imported the target, is NOT the target, and is reported as such instead of
    being rewritten.
    """

    rel: str
    text: str
    sha: str
    tree: ast.Module
    is_defining_file: bool
    own_module: str
    bindings: Dict[str, str]
    accessible: Set[str]
    imported_locals: Set[str]
    star_from_target: bool
    target_module: str
    target_symbol: str

    def lines(self) -> List[str]:
        return self.text.splitlines()

    def resolve_receiver(self, receiver: str) -> str:
        """Return the dotted module a receiver expression names, or ``""``."""
        if not receiver:
            return ""
        if receiver in self.accessible:
            return receiver
        head, _, tail = receiver.partition(".")
        bound = self.bindings.get(head)
        if bound is None and not tail:
            bound = self.bindings.get(receiver)
        if bound:
            return f"{bound}.{tail}" if tail else bound
        return ""

    def receiver_is_target(self, receiver: str) -> bool:
        """Return whether a receiver expression names the target module."""
        if not self.target_module or not receiver:
            return False
        return self.resolve_receiver(receiver) == self.target_module

    def bare_name_is_target(self, local: str) -> bool:
        """Return whether a bare local name refers to the target symbol."""
        if self.is_defining_file:
            return True
        return local in self.imported_locals


def _build_file_role(
    rel: str,
    text: str,
    sha: str,
    tree: ast.Module,
    target_module: str,
    target_symbol: str,
    defining_path: str,
) -> _FileRole:
    """Return the :class:`_FileRole` for one file, given the resolved target.

    Assumes ``rel`` is repo-relative, ``text`` is its exact source, and
    ``tree`` parsed from it.
    """
    own_module = _module_name(rel)
    imported_locals: Set[str] = set()
    star_from_target = False
    for child in getattr(tree, "body", ()) or ():
        if not isinstance(child, ast.ImportFrom):
            continue
        module = _from_import_module(child, own_module)
        if module != target_module:
            continue
        for alias in child.names:
            if alias.name == "*":
                star_from_target = True
                continue
            if alias.name == target_symbol:
                imported_locals.add(alias.asname or alias.name)
    return _FileRole(
        rel=rel,
        text=text,
        sha=sha,
        tree=tree,
        is_defining_file=rel == defining_path,
        own_module=own_module,
        bindings=_module_bindings(tree, own_module),
        accessible=_accessible_modules(tree, own_module),
        imported_locals=imported_locals,
        star_from_target=star_from_target,
        target_module=target_module,
        target_symbol=target_symbol,
    )


def _completeness_pass(
    scan: _FileScan,
    old: str,
    classified: Set[Tuple[int, int]],
) -> None:
    """Record every textual reference to ``old`` nothing else accounted for.

    Assumes ``scan.text`` is the file's source and ``classified`` is every
    ``(line, column)`` the role-aware scan already resolved or NAMED. Comments
    and string/docstring mentions are ADVISORY (named, not blocking, not
    rewritten); a string literal that IS the symbol's name is a BLOCKING
    dynamic lookup; any other unaccounted identifier is BLOCKING, because a
    rename that leaves it behind is the failure this module exists to prevent.
    This pass is what makes a ``globals()`` lookup, a registry key, or an
    f-string interpolation visible instead of silent.
    """
    word = _word_re(old)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(scan.text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        scan.unresolved.append(
            UnresolvedSite(
                scan.rel,
                0,
                UNRESOLVED_UNPARSED_FILE,
                f"could not tokenize for the completeness pass: {exc}",
            )
        )
        return
    for token in tokens:
        if token.type == tokenize.NAME and token.string == old:
            if (token.start[0], token.start[1]) not in classified:
                scan.unresolved.append(
                    UnresolvedSite(
                        scan.rel,
                        token.start[0],
                        UNRESOLVED_OTHER_REFERENCE,
                        "identifier occurrence with no resolvable role "
                        "(dynamic dispatch, pattern match, comprehension "
                        "target, or an expression form this module does not "
                        "model); not renamed",
                        _excerpt(scan.text, token.start[0]),
                    )
                )
        elif token.type == tokenize.COMMENT and word.search(token.string):
            scan.unresolved.append(
                UnresolvedSite(
                    scan.rel,
                    token.start[0],
                    UNRESOLVED_COMMENT,
                    "the name appears in a comment; a comment is a human "
                    "editorial decision, so it is named rather than rewritten",
                    _excerpt(scan.text, token.start[0]),
                )
            )
        elif token.type == tokenize.STRING and word.search(token.string):
            dynamic = _dynamic_string_reference(token.string, old)
            if dynamic is not None:
                scan.unresolved.append(
                    UnresolvedSite(
                        scan.rel,
                        token.start[0],
                        UNRESOLVED_DYNAMIC_LOOKUP,
                        f"{dynamic}; a rename that missed it would leave code "
                        "that looks renamed and is not",
                        _excerpt(scan.text, token.start[0]),
                    )
                )
            else:
                scan.unresolved.append(
                    UnresolvedSite(
                        scan.rel,
                        token.start[0],
                        UNRESOLVED_STRING_REFERENCE,
                        "the name appears inside a string or docstring; renamed "
                        "prose is a human editorial decision, so it is named "
                        "rather than rewritten",
                        _excerpt(scan.text, token.start[0]),
                    )
                )


def _dynamic_string_reference(token_text: str, name: str) -> Optional[str]:
    """Return why a string literal is a DYNAMIC symbol reference, or ``None``.

    Assumes ``token_text`` is one ``tokenize.STRING`` token's text. A literal
    whose value IS the symbol's name (or a dotted path ending in it) is a
    dynamic lookup — ``globals()["handler"]``, a registry keyed by name, an
    ``importlib.import_module`` argument — and a rename that missed it breaks
    code while leaving it looking renamed. Prose that merely mentions the word
    is not a lookup and returns ``None``, so a docstring stays advisory.
    """
    try:
        value = ast.literal_eval(token_text)
    except Exception:
        return _fstring_dynamic_reference(token_text, name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == name or text.endswith("." + name):
        return f"the string literal {text!r} IS the symbol's name, so it is a dynamic lookup"
    return None


def _fstring_dynamic_reference(token_text: str, name: str) -> Optional[str]:
    """Return why an f-string interpolates the symbol, or ``None``."""
    for match in re.finditer(r"\{([^{}]*)\}", token_text):
        expression = match.group(1).strip()
        if expression == name or expression.endswith("." + name):
            return (
                f"the f-string interpolates {expression!r}, which is the "
                "symbol's name, so it is a dynamic lookup"
            )
    return None


def _scan_file_for_rename(
    role: _FileRole,
    old: str,
    new: str,
    follow_strings: bool = False,
) -> _FileScan:
    """Scan one file for every site and unresolved reference of the target.

    Assumes ``role`` carries the file's exact source and its relationship to
    the resolved target, and ``old`` is the target's name. An unparseable
    file is refused upstream (it never gets a role), so this always returns a
    parsed scan; the caller names unparseable files itself.
    """
    scan = _FileScan(rel=role.rel, text=role.text, sha=role.sha)
    scan.module_bindings = dict(role.bindings)
    scanner = _RenameScanner(
        old=old,
        new=new,
        rel=role.rel,
        lines=scan.lines(),
        role=role,
        unresolved=scan.unresolved,
    )
    scan.spans = scanner.scan(role.tree)
    if role.star_from_target:
        scan.unresolved.append(
            UnresolvedSite(
                role.rel,
                0,
                UNRESOLVED_OTHER_REFERENCE,
                "this file does `from <target module> import *`, so which names "
                "it binds cannot be determined; occurrences in it were neither "
                "renamed nor proven unrelated",
            )
        )
    if not follow_strings:
        _completeness_pass(scan, old, set(scanner.classified))
    return scan


def _build_edits(
    scan: _FileScan, spans: Sequence[_Span], context_lines: int
) -> Tuple[List[PlannedEdit], List[UnresolvedSite]]:
    """Turn resolved spans into exact per-line text replacements.

    Assumes ``scan.text`` is the file's source. One edit is produced per
    distinct starting line, carrying every span on that line, so applying the
    edits in order never invalidates a later ``old_string``. When a single
    line is not uniquely addressable the span is widened by up to
    ``context_lines`` neighbouring lines and the widened span is verified by
    reconstruction; if it is still ambiguous the site is NAMED as
    ``UNRESOLVED_AMBIGUOUS_SPAN`` rather than guessed at.

    Every candidate block is sliced out of the ORIGINAL text, so it carries the
    file's own line terminators. A block re-joined with ``"\\n"`` would not
    occur in a CRLF file at all, which would make every span look ambiguous.
    """
    lines = scan.lines()
    bounds = _line_offsets(scan.text)
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []
    by_line: Dict[int, List[_Span]] = {}
    for span in spans:
        by_line.setdefault(span.start_line, []).append(span)
    for lineno in sorted(by_line):
        group = sorted(by_line[lineno], key=lambda item: item.start_col)
        if any(span.end_line != lineno for span in group):
            unresolved.append(
                UnresolvedSite(
                    scan.rel,
                    lineno,
                    UNRESOLVED_OTHER_REFERENCE,
                    "a resolved site spans more than one line and cannot be "
                    "addressed by a single exact replacement",
                    _excerpt(scan.text, lineno),
                )
            )
            continue
        chosen: Optional[Tuple[int, str, str]] = None
        reconstructed_without_change = False
        for extra in range(0, max(0, context_lines) + 1):
            first = max(1, lineno - extra)
            last = min(len(lines), lineno + extra)
            start = bounds[first - 1][0]
            stop = bounds[last - 1][2]
            old_block = scan.text[start:stop]
            if scan.text.count(old_block) != 1:
                continue
            new_block = _apply_spans(old_block, group, offset=first - 1)
            if new_block == old_block:
                reconstructed_without_change = True
                break
            chosen = (lineno, old_block, new_block)
            break
        if chosen is None and reconstructed_without_change:
            unresolved.append(
                UnresolvedSite(
                    scan.rel,
                    lineno,
                    UNRESOLVED_OTHER_REFERENCE,
                    "reconstructing the exact replacement produced no change, so "
                    "the site was not changed rather than written as a no-op",
                    _excerpt(scan.text, lineno),
                )
            )
            continue
        if chosen is None:
            unresolved.append(
                UnresolvedSite(
                    scan.rel,
                    lineno,
                    UNRESOLVED_AMBIGUOUS_SPAN,
                    "the resolved site could not be widened into a uniquely "
                    f"addressable source span within {context_lines} line(s) of "
                    "context, so the ordinary unique-match guard would have "
                    "refused it; not changed",
                    _excerpt(scan.text, lineno),
                )
            )
            continue
        line_no, old_block, new_block = chosen
        edits.append(
            PlannedEdit(
                path=scan.rel,
                line=line_no,
                kind=group[0].kind,
                old_string=old_block,
                new_string=new_block,
                expected_sha256=scan.sha,
                site_lines=tuple(span.start_line for span in group),
            )
        )
    return edits, unresolved


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def _refuse(
    operation: str,
    repo_path: str,
    symbol: str,
    reason: str,
    *,
    language: str = LANGUAGE_UNKNOWN,
    unresolved: Sequence[UnresolvedSite] = (),
    notes: Sequence[str] = (),
    index_source: str = "",
) -> CodemodPlan:
    """Return a refusing plan: no edits, and the reason named in the receipt."""
    receipt = CodemodReceipt(
        operation=operation,
        symbol=symbol,
        language=language,
        resolution="none",
        index_source=index_source,
        files_considered=0,
        unresolved=tuple(unresolved),
        notes=tuple(notes),
        refused=True,
        reason=reason,
    )
    return CodemodPlan(
        operation=operation,
        repo_path=str(repo_path),
        symbol=symbol,
        new_name="",
        language=language,
        receipt=receipt,
    )


# ---------------------------------------------------------------------------
# rename_symbol
# ---------------------------------------------------------------------------


def _scope_bound_refusal(
    operation: str,
    repo_path: str,
    symbol: str,
    candidates: Sequence[str],
    settings: CodemodConfig,
    notes: Sequence[str],
) -> Optional[CodemodPlan]:
    """Return a refusal when the repository is larger than the declared scope.

    Assumes ``candidates`` is the FULL list of source files a scan would
    examine. This runs BEFORE the structural index is touched, and that
    ordering is the point: building the tree-sitter index for a large
    repository costs minutes, so a bound that is checked afterwards is no bound
    at all. The refusal names the count and the config key, so a caller knows
    exactly what to change.
    """
    if len(candidates) <= settings.max_index_files:
        return None
    return _refuse(
        operation,
        repo_path,
        symbol,
        f"the repository has {len(candidates)} source file(s), above the "
        f"bounded index scope of {settings.max_index_files}. Refusing rather "
        "than starting an unbounded index build. Narrow the scope with "
        "`codemod_include_paths`, or raise `codemod_max_index_files` if this "
        "is the repository you meant. No file was changed.",
        notes=(*tuple(notes), f"candidate_files={len(candidates)}"),
    )


def rename_symbol(
    repo_path: str,
    old_name: str,
    new_name: str,
    *,
    path: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    index: Any = None,
) -> CodemodPlan:
    """Plan a rename of one symbol across a repository. Changes nothing.

    Assumes ``repo_path`` is an existing repository directory, ``old_name``
    and ``new_name`` are valid Python identifiers, and ``old_name !=
    new_name``. ``path``, when given, is the repo-relative file holding the
    definition and disambiguates a name defined in more than one place;
    without it an ambiguous definition is a refusal that names the candidates,
    because renaming "one of these three" is not a plan.

    Uses the tree-sitter structural index (``memory.code_graph``) to resolve
    the definition and its module, and stdlib ``ast`` â€” the parser
    ``runtime.symbols`` and ``harness.lint`` already use â€” to address every
    site exactly. Returns a :class:`CodemodPlan`; call :func:`apply_plan` to
    change anything, through the ordinary edit path.
    """
    repo = Path(str(repo_path or ""))
    if not repo.is_dir():
        return _refuse(
            OP_RENAME,
            str(repo_path),
            str(old_name),
            f"repository not found: {repo_path}",
        )
    old = str(old_name or "").strip()
    new = str(new_name or "").strip()
    if not _IDENTIFIER_RE.match(old) or not _IDENTIFIER_RE.match(new):
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            "rename requires two valid Python identifiers; received "
            f"{old_name!r} -> {new_name!r}",
        )
    if old == new:
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            "the new name is the old name; nothing to do",
        )
    try:
        settings, notes = config_from(config)
    except TypeError as exc:
        return _refuse(
            OP_RENAME, str(repo_path), old, f"codemod configuration unusable: {exc}"
        )

    candidates = _iter_source_files(repo, settings.include_paths)
    bounded = _scope_bound_refusal(
        OP_RENAME, str(repo_path), old, candidates, settings, notes
    )
    if bounded is not None:
        return bounded

    loaded, index_source = (
        (index, "injected") if index is not None else _load_index(str(repo))
    )
    if loaded is None:
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            "the structural symbol index could not be read, so the definition "
            f"of the symbol is unknown: {index_source}. No file was changed.",
            index_source=index_source,
            notes=notes,
        )

    definitions, resolution = _resolve_definition(loaded, old, path)
    if resolution == "not_found_in_path":
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            f"no definition of {old!r} in {path}; the index has it elsewhere or "
            "nowhere. No file was changed.",
            index_source=index_source,
            notes=notes,
        )
    if not definitions:
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            f"the structural index has no definition of {old!r}. No file was "
            "changed. This is not proof the symbol is unused: the index skips "
            "files that fail to parse, and this module's own scan names those.",
            index_source=index_source,
            notes=notes,
        )
    if resolution == "ambiguous_definition":
        candidates = sorted({f"{node.file}:{node.qualified}" for node in definitions})
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            f"{old!r} is defined in {len(candidates)} places; pass `path` to say "
            "which one. Candidates: " + ", ".join(candidates[:8]),
            unresolved=[
                UnresolvedSite(
                    item.split(":", 1)[0], 0, UNRESOLVED_AMBIGUOUS_DEFINITION, item
                )
                for item in candidates
            ],
            index_source=index_source,
            notes=notes,
        )

    definition = definitions[0]
    definition_path = str(getattr(definition, "file", "") or "")
    language = language_of(definition_path)
    support = language_support(definition_path)
    if support:
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            f"refusing to rename {old!r} in {definition_path}: {support}. No "
            "file was changed.",
            language=language,
            unresolved=[
                UnresolvedSite(
                    definition_path, 0, UNRESOLVED_UNSUPPORTED_LANGUAGE, support
                )
            ],
            index_source=index_source,
            notes=notes,
        )

    target_module = _index_file_module(loaded, definition_path) or _module_name(
        definition_path
    )

    word = _word_re(old)
    relevant: List[str] = []
    for rel in candidates:
        if rel == definition_path:
            relevant.append(rel)
            continue
        text, _sha, _err = _read_source(repo, rel)
        if text and word.search(text):
            relevant.append(rel)

    sites: List[CodemodSite] = []
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []
    for rel in sorted(dict.fromkeys(relevant)):
        text, sha, err = _read_source(repo, rel)
        if text is None or sha is None:
            unresolved.append(
                UnresolvedSite(
                    rel,
                    0,
                    UNRESOLVED_UNPARSED_FILE,
                    f"file could not be read for scanning: {err}; no site in it "
                    "was changed",
                )
            )
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError) as exc:
            unresolved.append(
                UnresolvedSite(
                    rel,
                    0,
                    UNRESOLVED_UNPARSED_FILE,
                    f"file did not parse, so no site in it was changed: {exc}",
                )
            )
            continue
        role = _build_file_role(
            rel, text, sha, tree, target_module, old, definition_path
        )
        scan = _scan_file_for_rename(
            role=role,
            old=old,
            new=new,
            follow_strings=settings.follow_string_references,
        )
        scopes = _scope_index(text, rel)
        for span in scan.spans:
            sites.append(
                CodemodSite(
                    rel,
                    span.start_line,
                    span.start_col,
                    span.kind,
                    span.new_text,
                    span.detail,
                    scopes.get(span.start_line, ""),
                )
            )
        file_edits, file_unresolved = _build_edits(
            scan, scan.spans, settings.edit_context_lines
        )
        edits.extend(file_edits)
        unresolved.extend(scan.unresolved)
        unresolved.extend(file_unresolved)

    if len(sites) > settings.max_sites:
        return _refuse(
            OP_RENAME,
            str(repo_path),
            old,
            f"the rename resolves to {len(sites)} sites, above the bounded "
            f"maximum of {settings.max_sites}. Refusing rather than clipping.",
            language=language,
            unresolved=unresolved,
            index_source=index_source,
            notes=notes,
        )

    receipt = CodemodReceipt(
        operation=OP_RENAME,
        symbol=old,
        language=language,
        resolution="ast_identifier_level",
        index_source=index_source,
        files_considered=len(candidates),
        files_changed=len({edit.path for edit in edits}),
        sites_found=len(sites),
        sites_planned=len(sites),
        unresolved=tuple(
            sorted(unresolved, key=lambda item: (item.path, item.line, item.kind))
        ),
        notes=(
            *tuple(notes),
            f"definition_resolution={resolution}",
            f"definition={definition_path}:{getattr(definition, 'qualified', '')}",
            f"module={target_module}",
        ),
    )
    return CodemodPlan(
        operation=OP_RENAME,
        repo_path=str(repo),
        symbol=old,
        new_name=new,
        language=language,
        edits=tuple(edits),
        sites=tuple(sites),
        receipt=receipt,
        definition=f"{definition_path}:{getattr(definition, 'qualified', '')}",
        detail=f"rename {old} -> {new}",
    )


# ---------------------------------------------------------------------------
# update_signature
# ---------------------------------------------------------------------------


def _iter_signature_args(node: Any) -> List[ast.arg]:
    """Return every declared parameter of a function, in declaration order."""
    args = node.args
    collected: List[ast.arg] = []
    collected.extend(getattr(args, "posonlyargs", []) or [])
    collected.extend(args.args)
    if args.vararg is not None:
        collected.append(args.vararg)
    collected.extend(args.kwonlyargs)
    if args.kwarg is not None:
        collected.append(args.kwarg)
    return collected


def _definition_nodes(tree: ast.Module, symbol: str) -> List[Tuple[ast.AST, str]]:
    """Return ``(node, kind)`` for every definition of ``symbol`` in a file."""
    found: List[Tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ):
            found.append((node, "function"))
        elif isinstance(node, ast.ClassDef) and node.name == symbol:
            found.append((node, "class"))
    found.sort(key=lambda item: int(getattr(item[0], "lineno", 0) or 0))
    return found


def _split_top_level(inner: str) -> List[str]:
    """Split parameter or argument text on top-level commas only."""
    pieces: List[str] = []
    depth = 0
    current: List[str] = []
    for char in inner:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)
    if "".join(current).strip():
        pieces.append("".join(current))
    return [piece for piece in pieces if piece.strip()]


def _parameter_base(piece: str) -> str:
    head = piece.strip().split("=")[0].split(":")[0]
    return head.strip().lstrip("*").strip()


def _rewrite_parameter_head(piece: str, new_name: str) -> str:
    """Return ``piece`` with its parameter name replaced, keeping stars and annotation."""
    stripped = piece.strip()
    lead = piece[: len(piece) - len(piece.lstrip())]
    stars = ""
    rest = stripped
    while rest.startswith("*"):
        stars += "*"
        rest = rest[1:]
    head, separator, tail = rest.partition(":")
    if separator:
        return f"{lead}{stars}{new_name}:{tail}"
    default_sep, _, default_tail = head.partition("=")
    if default_sep:
        return f"{lead}{stars}{new_name}={default_tail}"
    return f"{lead}{stars}{new_name}"


def _rewrite_annotation(piece: str, annotation: str) -> str:
    """Return ``piece`` with its annotation replaced, keeping name and default."""
    stripped = piece.strip()
    lead = piece[: len(piece) - len(piece.lstrip())]
    head, separator, tail = stripped.partition(":")
    if not separator:
        return f"{lead}{head}: {annotation}"
    default_marker = ""
    depth = 0
    for index, char in enumerate(tail):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "=" and depth == 0:
            default_marker = tail[index:]
            break
    return f"{lead}{head}: {annotation}{default_marker}"


_BARE_WORD_DEFAULT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYTHON_LITERALS = frozenset({"None", "True", "False"})


def _render_added_parameter(spec: str) -> str:
    """Return the source text for one added parameter specification.

    Assumes ``spec`` is ``"name"``, ``"name:annotation"``, ``"name=default"``
    or ``"name:annotation=default"``. Added parameters are APPENDED, after
    the last declared parameter: a positional insertion point is a design
    decision the caller owns, and appending is the only choice that cannot
    silently re-order existing arguments.

    A default that is a bare word and is not a Python literal is rendered as a
    STRING, so ``"currency:str=usd"`` means ``currency: str = "usd"`` rather
    than the unquoted, undefined name ``usd``. Anything that is not a bare
    word is treated as raw Python source, so ``"count:int=0"``,
    ``"items:list=None"`` and ``"fn=os.path.join"`` all mean what they say.
    """
    text = str(spec or "").strip()
    name, _, rest = text.partition(":")
    if not rest:
        return text
    annotation, separator, default = rest.partition("=")
    if not separator:
        return f"{name}: {annotation}"
    rendered = default.strip()
    if (
        rendered
        and _BARE_WORD_DEFAULT_RE.match(rendered)
        and rendered not in _PYTHON_LITERALS
    ):
        rendered = f'"{rendered}"'
    return f"{name}: {annotation} = {rendered}"


def _positional_indices(function: Any, remove_set: Set[str]) -> Dict[str, int]:
    """Return ``parameter name -> positional index`` for a function signature.

    Assumes ``remove_set`` names declared parameters. Keyword-only parameters
    and ``*args``/``**kwargs`` are excluded, because they do not occupy a
    positional slot a call could be using.
    """
    args = function.args
    ordered: List[ast.arg] = []
    ordered.extend(getattr(args, "posonlyargs", []) or [])
    ordered.extend(args.args)
    return {
        arg.arg: index for index, arg in enumerate(ordered) if arg.arg in remove_set
    }


def update_signature(
    repo_path: str,
    symbol: str,
    *,
    path: Optional[str] = None,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    renamed: Optional[Mapping[str, str]] = None,
    retyped: Optional[Mapping[str, str]] = None,
    config: Optional[Mapping[str, Any]] = None,
    index: Any = None,
) -> CodemodPlan:
    """Plan a signature change for one function. Changes nothing.

    Assumes ``repo_path`` is an existing repository directory, ``symbol`` is
    a function or method name, and the declaration lists are sequences of
    names. ``added`` entries are ``"name"``, ``"name:annotation"``, or
    ``"name=default"`` (both may be combined as
    ``"name:annotation=default"``) and are APPENDED after the last declared
    parameter. ``removed``, ``renamed`` and ``retyped`` are keyed by the
    CURRENT parameter name.

    Positional call sites are rewritten by mapping the resolved callee's
    declared parameters onto the call's positional arguments; keyword
    arguments are matched by name. A call that uses ``*args``/``**kwargs``
    splats is NAMED as ``UNRESOLVED_SPLAT_ARGUMENTS`` instead of being
    guessed at, because a wrong argument removal is a silent behaviour
    change. A call spanning several source lines is NAMED too: rewriting it
    would require re-formatting code this module does not own.

    Uses the tree-sitter structural index (``memory.code_graph``) for the
    definition and stdlib ``ast`` â€” the parser ``runtime.symbols`` and
    ``harness.lint`` already use â€” for every site. Returns a
    :class:`CodemodPlan`; call :func:`apply_plan` to change anything.
    """
    repo = Path(str(repo_path or ""))
    name = str(symbol or "").strip()
    if not repo.is_dir():
        return _refuse(
            OP_SIGNATURE, str(repo_path), name, f"repository not found: {repo_path}"
        )
    if not _IDENTIFIER_RE.match(name):
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"update_signature requires a valid Python identifier; received {symbol!r}",
        )
    try:
        settings, notes = config_from(config)
    except TypeError as exc:
        return _refuse(
            OP_SIGNATURE, str(repo_path), name, f"codemod configuration unusable: {exc}"
        )

    candidates = _iter_source_files(repo, settings.include_paths)
    bounded = _scope_bound_refusal(
        OP_SIGNATURE, str(repo_path), name, candidates, settings, notes
    )
    if bounded is not None:
        return bounded

    loaded, index_source = (
        (index, "injected") if index is not None else _load_index(str(repo))
    )
    if loaded is None:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            "the structural symbol index could not be read, so the definition "
            f"of the function is unknown: {index_source}. No file was changed.",
            index_source=index_source,
            notes=notes,
        )
    definitions, resolution = _resolve_definition(loaded, name, path)
    if resolution == "not_found_in_path":
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"no definition of {name!r} in {path}. No file was changed.",
            index_source=index_source,
            notes=notes,
        )
    if not definitions:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"the structural index has no definition of {name!r}. No file was changed.",
            index_source=index_source,
            notes=notes,
        )
    if resolution == "ambiguous_definition":
        candidates = sorted({f"{node.file}:{node.qualified}" for node in definitions})
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"{name!r} is defined in {len(candidates)} places; pass `path` to say "
            "which one. Candidates: " + ", ".join(candidates[:8]),
            unresolved=[
                UnresolvedSite(
                    item.split(":", 1)[0], 0, UNRESOLVED_AMBIGUOUS_DEFINITION, item
                )
                for item in candidates
            ],
            index_source=index_source,
            notes=notes,
        )

    definition = definitions[0]
    definition_path = str(getattr(definition, "file", "") or "")
    language = language_of(definition_path)
    support = language_support(definition_path)
    if support:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"refusing to change the signature of {name!r} in {definition_path}: "
            f"{support}. No file was changed.",
            language=language,
            unresolved=[
                UnresolvedSite(
                    definition_path, 0, UNRESOLVED_UNSUPPORTED_LANGUAGE, support
                )
            ],
            index_source=index_source,
            notes=notes,
        )
    target_module = _index_file_module(loaded, definition_path) or _module_name(
        definition_path
    )

    add_list = [str(item) for item in (added or ())]
    remove_set = {str(item) for item in (removed or ())}
    rename_map = {str(key): str(value) for key, value in dict(renamed or {}).items()}
    retype_map = {str(key): str(value) for key, value in dict(retyped or {}).items()}

    definition_text, definition_sha, err = _read_source(repo, definition_path)
    if definition_text is None or definition_sha is None:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"the definition file could not be read: {err}. No file was changed.",
            language=language,
            index_source=index_source,
            notes=notes,
        )
    try:
        tree = ast.parse(definition_text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"the definition file did not parse: {exc}. No file was changed.",
            language=language,
            unresolved=[
                UnresolvedSite(definition_path, 0, UNRESOLVED_UNPARSED_FILE, str(exc))
            ],
            index_source=index_source,
            notes=notes,
        )
    found = _definition_nodes(tree, name)
    if not found:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"{name!r} is indexed at {definition_path} but no longer parses to a "
            "definition there (stale index). No file was changed.",
            language=language,
            unresolved=[
                UnresolvedSite(
                    definition_path,
                    0,
                    UNRESOLVED_UNPARSED_FILE,
                    "stale structural index: the definition is absent from the "
                    "current file",
                )
            ],
            index_source=index_source,
            notes=notes,
        )
    function = found[0][0]
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"{name!r} at {definition_path} is a class, not a function; this "
            "operation changes function signatures only. No file was changed.",
            language=language,
            index_source=index_source,
            notes=notes,
        )
    declared = [arg.arg for arg in _iter_signature_args(function)]
    unknown = sorted((remove_set | set(rename_map) | set(retype_map)) - set(declared))
    if unknown:
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            f"{name!r} at {definition_path} declares no parameter(s) "
            f"{', '.join(unknown)}; declared parameters are "
            f"{', '.join(declared) or '(none)'}. No file was changed.",
            language=language,
            index_source=index_source,
            notes=notes,
        )
    if not (add_list or remove_set or rename_map or retype_map):
        return _refuse(
            OP_SIGNATURE,
            str(repo_path),
            name,
            "update_signature was given nothing to change (no added, removed, "
            "renamed, or retyped parameters). No file was changed.",
            language=language,
            index_source=index_source,
            notes=notes,
        )

    return _plan_signature_edits(
        repo=repo,
        name=name,
        definition_path=definition_path,
        target_module=target_module,
        function=function,
        definition_text=definition_text,
        definition_sha=definition_sha,
        add_list=add_list,
        remove_set=remove_set,
        rename_map=rename_map,
        retype_map=retype_map,
        settings=settings,
        candidates=candidates,
        index_source=index_source,
        notes=notes,
    )


def _plan_signature_edits(
    *,
    repo: Path,
    name: str,
    definition_path: str,
    target_module: str,
    function: Any,
    definition_text: str,
    definition_sha: str,
    add_list: Sequence[str],
    remove_set: Set[str],
    rename_map: Mapping[str, str],
    retype_map: Mapping[str, str],
    settings: CodemodConfig,
    candidates: Sequence[str],
    index_source: str,
    notes: Sequence[str],
) -> CodemodPlan:
    """Build the declaration-header and call-site edits for one signature change.

    Assumes ``function`` is a parsed ``FunctionDef`` from ``definition_text``.
    The header is rebuilt from the source between the ``def``/``async def``
    line and the first body statement, and the call sites are mapped
    positionally against the resolved callee's declared parameters.
    """
    lines = definition_text.splitlines()
    bounds = _line_offsets(definition_text)
    terminator = _line_terminator(definition_text)
    header_start = int(function.lineno)
    if function.decorator_list:
        header_start = min(
            int(getattr(item, "lineno", header_start) or header_start)
            for item in function.decorator_list
        )
    body_first = int(function.body[0].lineno)
    header_end = max(header_start, body_first - 1)
    if header_end > len(lines):
        header_end = len(lines)
    # Sliced out of the ORIGINAL text so the header carries the file's own
    # line terminators; a header re-joined with "\n" would not occur in a CRLF
    # file, and the ordinary unique-match guard would refuse every edit.
    header = definition_text[bounds[header_start - 1][0] : bounds[header_end - 1][2]]
    if "(" not in header or ")" not in header:
        return _refuse(
            OP_SIGNATURE,
            str(repo),
            name,
            f"the declaration of {name!r} at {definition_path} has no "
            "unambiguous parameter list to rewrite. No file was changed.",
            language=LANGUAGE_PYTHON,
            index_source=index_source,
            notes=notes,
        )
    open_index = header.index("(")
    close_index = header.rindex(")")
    prefix = header[: open_index + 1]
    suffix = header[close_index:]
    inner = header[open_index + 1 : close_index]

    raw_pieces = _split_top_level(inner)
    multi_line = terminator in inner
    param_indent = ""
    paren_indent = ""
    if multi_line:
        for raw_line in inner.splitlines()[1:]:
            body_text = raw_line.strip()
            if body_text:
                param_indent = raw_line[: len(raw_line) - len(body_text)]
                break
        head_lines = header.splitlines()
        head_line = head_lines[0] if head_lines else ""
        paren_indent = head_line[: len(head_line) - len(head_line.lstrip())]

    rebuilt: List[str] = []
    for piece in raw_pieces:
        base = _parameter_base(piece)
        if base in remove_set:
            continue
        text = piece.strip()
        if base in rename_map:
            text = _rewrite_parameter_head(text, rename_map[base])
        if base in retype_map:
            text = _rewrite_annotation(text, retype_map[base])
        rebuilt.append(text)
    for spec in add_list:
        rebuilt.append(_render_added_parameter(spec))
    if multi_line:
        separator = "," + (terminator + param_indent if param_indent else terminator)
        new_inner = separator.join(rebuilt)
        if inner.startswith(terminator):
            new_inner = terminator + param_indent + new_inner
        if inner.endswith(terminator):
            new_inner += terminator + paren_indent
    else:
        new_inner = ", ".join(rebuilt)
    new_header = prefix + new_inner + suffix
    if new_header == header:
        return _refuse(
            OP_SIGNATURE,
            str(repo),
            name,
            f"the requested signature change for {name!r} produces the same "
            "declaration. No file was changed.",
            language=LANGUAGE_PYTHON,
            index_source=index_source,
            notes=notes,
        )
    # A codemod must never leave a file that does not parse. The most common
    # way to get there is appending a parameter with no default after one that
    # has a default, which is a SyntaxError rather than a runtime error — so it
    # is caught HERE, before any edit is planned, and reported as the design
    # decision it is: where an added parameter goes is the caller's choice.
    try:
        compile(
            new_header + terminator + "    pass", f"<{definition_path}:{name}>", "exec"
        )
    except SyntaxError as exc:
        return _refuse(
            OP_SIGNATURE,
            str(repo),
            name,
            f"the requested declaration for {name!r} is not valid Python "
            f"({exc.msg} at {exc.lineno}:{exc.offset}). An added parameter with "
            "no default cannot follow a defaulted one, and this module appends "
            "added parameters rather than guessing an insertion point. Give the "
            "added parameter a default, or reorder the declaration yourself. No "
            "file was changed.",
            language=LANGUAGE_PYTHON,
            index_source=index_source,
            notes=notes,
        )

    sites: List[CodemodSite] = []
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []

    keyword_sites, keyword_edits, keyword_unresolved = _plan_parameter_renames(
        repo=repo,
        old_name=name,
        target_module=target_module,
        defining_path=definition_path,
        rename_map=rename_map,
        candidates=candidates,
        settings=settings,
    )
    sites.extend(keyword_sites)
    edits.extend(keyword_edits)
    unresolved.extend(keyword_unresolved)

    call_sites, call_edits, call_unresolved = _plan_call_argument_removals(
        repo=repo,
        old_name=name,
        target_module=target_module,
        defining_path=definition_path,
        function=function,
        removed_indices=_positional_indices(function, remove_set),
        remove_set=remove_set,
        candidates=candidates,
        settings=settings,
    )
    sites.extend(call_sites)
    edits.extend(call_edits)
    unresolved.extend(call_unresolved)

    # The declaration header is appended LAST on purpose. Rebuilding a
    # multi-line parameter list changes the file's line count, so every other
    # edit in the same file must already be applied while the original line
    # numbering still holds.
    if definition_text.count(header) == 1:
        edits.append(
            PlannedEdit(
                path=definition_path,
                line=header_start,
                kind=SITE_SIGNATURE,
                old_string=header,
                new_string=new_header,
                expected_sha256=definition_sha,
                site_lines=(header_start,),
            )
        )
        sites.append(
            CodemodSite(
                definition_path,
                header_start,
                open_index + 1,
                SITE_SIGNATURE,
                ", ".join(rebuilt),
                "declaration parameter list",
                name,
            )
        )
    else:
        unresolved.append(
            UnresolvedSite(
                definition_path,
                header_start,
                UNRESOLVED_AMBIGUOUS_SPAN,
                "the declaration header is not uniquely addressable, so the "
                "ordinary unique-match guard would have refused it; not changed",
            )
        )

    body_sites, body_edits, body_unresolved = _plan_body_parameter_references(
        name=name,
        definition_path=definition_path,
        definition_text=definition_text,
        definition_sha=definition_sha,
        function=function,
        rename_map=rename_map,
        remove_set=remove_set,
        settings=settings,
    )
    sites.extend(body_sites)
    edits.extend(body_edits)
    unresolved.extend(body_unresolved)

    if len(sites) > settings.max_sites:
        return _refuse(
            OP_SIGNATURE,
            str(repo),
            name,
            f"the signature change resolves to {len(sites)} sites, above the "
            f"bounded maximum of {settings.max_sites}. Refusing rather than clipping.",
            language=LANGUAGE_PYTHON,
            unresolved=unresolved,
            index_source=index_source,
            notes=notes,
        )

    ordered = tuple(
        sorted(unresolved, key=lambda item: (item.path, item.line, item.kind))
    )
    receipt = CodemodReceipt(
        operation=OP_SIGNATURE,
        symbol=name,
        language=LANGUAGE_PYTHON,
        resolution="ast_identifier_level",
        index_source=index_source,
        files_considered=len(candidates),
        files_changed=len({edit.path for edit in edits}),
        sites_found=len(sites),
        sites_planned=len(sites),
        unresolved=ordered,
        notes=(
            *tuple(notes),
            f"definition={definition_path}:{name}",
            f"added={list(add_list)}",
            f"removed={sorted(remove_set)}",
            f"renamed={dict(rename_map)}",
            f"retyped={dict(retype_map)}",
        ),
    )
    return CodemodPlan(
        operation=OP_SIGNATURE,
        repo_path=str(repo),
        symbol=name,
        new_name=name,
        language=LANGUAGE_PYTHON,
        edits=tuple(edits),
        sites=tuple(sites),
        receipt=receipt,
        definition=f"{definition_path}:{name}",
        signature=header,
        detail=(
            f"signature change for {name}: added={list(add_list)} "
            f"removed={sorted(remove_set)} renamed={dict(rename_map)} "
            f"retyped={dict(retype_map)}"
        ),
    )


def _body_bound_names(function: Any) -> Set[str]:
    """Return every name the function's BODY rebinds somewhere inside it.

    Assumes ``function`` is a parsed ``FunctionDef``. Only the body statements
    are walked, so the function's own parameters are not counted. A name that
    appears here is shadowed at least once, which means a reference to it
    cannot be resolved to the parameter without real scope analysis — so this
    module names it instead of rewriting it.
    """
    bound: Set[str] = set()
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Lambda):
                bound.update(arg.arg for arg in _iter_signature_args(node))
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bound.update(node.names)
            elif isinstance(node, ast.alias) and node.asname:
                bound.add(node.asname)
    return bound


def _plan_body_parameter_references(
    *,
    name: str,
    definition_path: str,
    definition_text: str,
    definition_sha: str,
    function: Any,
    rename_map: Mapping[str, str],
    remove_set: Set[str],
    settings: CodemodConfig,
) -> Tuple[List[CodemodSite], List[PlannedEdit], List[UnresolvedSite]]:
    """Plan the body's own uses of the parameters being renamed or removed.

    Assumes ``name`` is the declared function's name and ``function`` is its
    parsed declaration in ``definition_text``.

    A **renamed** parameter's reads inside the body are renamed, because
    leaving them would be a guaranteed ``NameError`` the moment the change
    lands. A **removed** parameter's reads are NAMED as blocking unresolved
    instead: there is no mechanical rewrite that is correct, because whether
    the argument should be dropped, defaulted, or replaced is a semantic
    decision. A name the body shadows anywhere is named rather than rewritten
    for both cases, because a single shadowing rebind means the remaining
    references cannot be resolved without scope analysis this module does not
    perform.
    """
    sites: List[CodemodSite] = []
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []
    if not rename_map and not remove_set:
        return sites, edits, unresolved
    lines = definition_text.splitlines()
    shadowed = _body_bound_names(function)
    spans: List[_Span] = []
    interesting = {*rename_map, *remove_set}

    for statement in function.body:
        for node in ast.walk(statement):
            lineno = int(getattr(node, "lineno", 0) or 0)
            if isinstance(node, ast.Name) and node.id in interesting:
                if node.id in shadowed:
                    unresolved.append(
                        UnresolvedSite(
                            definition_path,
                            lineno,
                            UNRESOLVED_OTHER_REFERENCE,
                            f"the body rebinds {node.id!r} somewhere inside the "
                            "function, so this reference cannot be resolved to "
                            "the parameter without scope analysis; not changed",
                            _excerpt(definition_text, lineno),
                        )
                    )
                    continue
                if node.id in remove_set:
                    unresolved.append(
                        UnresolvedSite(
                            definition_path,
                            lineno,
                            UNRESOLVED_OTHER_REFERENCE,
                            f"the body still reads parameter {node.id!r}, which this "
                            "change removes. There is no correct mechanical "
                            "rewrite: whether the argument should be dropped, "
                            "defaulted, or replaced is a semantic decision. Not "
                            "changed.",
                            _excerpt(definition_text, lineno),
                        )
                    )
                    continue
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    unresolved.append(
                        UnresolvedSite(
                            definition_path,
                            lineno,
                            UNRESOLVED_OTHER_REFERENCE,
                            f"the body assigns to parameter {node.id!r} in place; "
                            "rebinding semantics are a human decision, so it was "
                            "not renamed",
                            _excerpt(definition_text, lineno),
                        )
                    )
                    continue
                col = _char_column(
                    lines[lineno - 1] if lineno <= len(lines) else "",
                    int(getattr(node, "col_offset", 0) or 0),
                )
                spans.append(
                    _Span(
                        lineno,
                        col,
                        lineno,
                        col + len(node.id),
                        rename_map[node.id],
                        SITE_REFERENCE,
                        "body reference to a renamed parameter",
                    )
                )
                sites.append(
                    CodemodSite(
                        definition_path,
                        lineno,
                        col,
                        SITE_REFERENCE,
                        rename_map[node.id],
                        "body reference to a renamed parameter",
                        name,
                    )
                )
            elif isinstance(node, ast.keyword) and (node.arg or "") in rename_map:
                head = (lines[lineno - 1] if lineno <= len(lines) else "").find(
                    f"{node.arg}="
                )
                if head < 0:
                    unresolved.append(
                        UnresolvedSite(
                            definition_path,
                            lineno,
                            UNRESOLVED_OTHER_REFERENCE,
                            f"keyword argument {node.arg}= inside the body could "
                            "not be located in its source line; not renamed",
                            _excerpt(definition_text, lineno),
                        )
                    )
                    continue
                spans.append(
                    _Span(
                        lineno,
                        head,
                        lineno,
                        head + len(str(node.arg)),
                        rename_map[str(node.arg)],
                        SITE_KEYWORD,
                        "body keyword argument of a renamed parameter",
                    )
                )
                sites.append(
                    CodemodSite(
                        definition_path,
                        lineno,
                        head,
                        SITE_KEYWORD,
                        rename_map[str(node.arg)],
                        "body keyword",
                        name,
                    )
                )
    if not spans:
        return sites, edits, unresolved
    scan = _FileScan(rel=definition_path, text=definition_text, sha=definition_sha)
    file_edits, edit_unresolved = _build_edits(scan, spans, settings.edit_context_lines)
    edits.extend(file_edits)
    unresolved.extend(edit_unresolved)
    return sites, edits, unresolved


def _resolved_calls(role: _FileRole, symbol: str) -> List[ast.Call]:
    """Return the calls in a file that provably call the target function.

    Assumes ``role`` says how the file relates to the target definition and
    ``symbol`` is the target's name. A call qualifies when it is a bare name
    the file bound to the target (the defining module's own namespace, or an
    explicit import) or an attribute whose receiver resolves to the target
    module. A same-named call in a file that never imported the target is NOT
    returned: rewriting it would corrupt an unrelated function, which is the
    name-based over-approximation this module deliberately does not make.
    """
    found: List[ast.Call] = []
    for node in ast.walk(role.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if role.bare_name_is_target(func.id) and func.id == symbol:
                found.append(node)
            continue
        if isinstance(func, ast.Attribute) and func.attr == symbol:
            receiver = _dotted(func.value)
            if role.receiver_is_target(receiver):
                found.append(node)
    return found


def _plan_parameter_renames(
    *,
    repo: Path,
    old_name: str,
    target_module: str,
    defining_path: str,
    rename_map: Mapping[str, str],
    candidates: Sequence[str],
    settings: CodemodConfig,
) -> Tuple[List[CodemodSite], List[PlannedEdit], List[UnresolvedSite]]:
    """Plan the keyword-argument rewrites for renamed parameters.

    Assumes ``rename_map`` maps current parameter names to new ones. Only
    keyword arguments are rewritten (``f(old=1)`` -> ``f(new=1)``); a
    POSITIONAL call is unaffected by a parameter rename, which is the entire
    point of renaming rather than reordering. A file that never imported the
    target is skipped entirely rather than scanned on the strength of a
    matching name.
    """
    sites: List[CodemodSite] = []
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []
    if not rename_map:
        return sites, edits, unresolved
    for rel in candidates:
        text, sha, _err = _read_source(repo, rel)
        if text is None or sha is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            continue
        role = _build_file_role(
            rel, text, sha, tree, target_module, old_name, defining_path
        )
        if (
            not role.is_defining_file
            and old_name not in role.imported_locals
            and target_module not in role.accessible
        ):
            continue
        lines = text.splitlines()
        scopes = _scope_index(text, rel)
        spans: List[_Span] = []
        for call in _resolved_calls(role, old_name):
            for keyword in call.keywords:
                new_name = rename_map.get(keyword.arg or "")
                if not new_name:
                    continue
                lineno = int(keyword.value.lineno)
                head = lines[lineno - 1].find(f"{keyword.arg}=")
                if head < 0:
                    unresolved.append(
                        UnresolvedSite(
                            rel,
                            lineno,
                            UNRESOLVED_OTHER_REFERENCE,
                            f"keyword argument {keyword.arg}= could not be located "
                            "in its source line; not renamed",
                            lines[lineno - 1].strip()[:200],
                        )
                    )
                    continue
                spans.append(
                    _Span(
                        lineno,
                        head,
                        lineno,
                        head + len(keyword.arg),
                        new_name,
                        SITE_KEYWORD,
                        "renamed keyword argument",
                    )
                )
                sites.append(
                    CodemodSite(
                        rel,
                        lineno,
                        head,
                        SITE_KEYWORD,
                        new_name,
                        "keyword",
                        scopes.get(lineno, ""),
                    )
                )
        if not spans:
            continue
        scan = _FileScan(rel=rel, text=text, sha=sha)
        file_edits, edit_unresolved = _build_edits(
            scan, spans, settings.edit_context_lines
        )
        edits.extend(file_edits)
        unresolved.extend(edit_unresolved)
    return sites, edits, unresolved


def _plan_call_argument_removals(
    *,
    repo: Path,
    old_name: str,
    target_module: str,
    defining_path: str,
    function: Any,
    removed_indices: Mapping[str, int],
    remove_set: Set[str],
    candidates: Sequence[str],
    settings: CodemodConfig,
) -> Tuple[List[CodemodSite], List[PlannedEdit], List[UnresolvedSite]]:
    """Plan the removal of positional arguments at removed parameter positions.

    Assumes ``removed_indices`` maps a removed parameter to its positional
    index. A call using ``*args`` or ``**kwargs`` is NAMED as
    ``UNRESOLVED_SPLAT_ARGUMENTS`` and left alone, because the mapping from
    declared parameters to runtime arguments is unknown at that call. A file
    that never imported the target is skipped entirely, so a same-named
    function elsewhere keeps its arguments.
    """
    sites: List[CodemodSite] = []
    edits: List[PlannedEdit] = []
    unresolved: List[UnresolvedSite] = []
    if not remove_set:
        return sites, edits, unresolved
    targets = set(removed_indices.values())
    for rel in candidates:
        text, sha, _err = _read_source(repo, rel)
        if text is None or sha is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            continue
        role = _build_file_role(
            rel, text, sha, tree, target_module, old_name, defining_path
        )
        if (
            not role.is_defining_file
            and old_name not in role.imported_locals
            and target_module not in role.accessible
        ):
            continue
        lines = text.splitlines()
        scopes = _scope_index(text, rel)
        spans: List[_Span] = []
        for call in _resolved_calls(role, old_name):
            if any(
                isinstance(item, ast.Starred)
                for item in list(call.args) + list(call.keywords)
            ):
                unresolved.append(
                    UnresolvedSite(
                        rel,
                        int(call.lineno),
                        UNRESOLVED_SPLAT_ARGUMENTS,
                        f"call to {old_name}() uses a *args/**kwargs splat, so the "
                        "argument at each declared parameter position is not "
                        "statically known; the call was not rewritten",
                        _excerpt(text, int(call.lineno)),
                    )
                )
                continue
            new_line, changed = _rewrite_call_line(lines, call, targets, remove_set)
            if not changed:
                unresolved.append(
                    UnresolvedSite(
                        rel,
                        int(call.lineno),
                        UNRESOLVED_OTHER_REFERENCE,
                        f"call to {old_name}() spans more than one source line, so "
                        "rewriting it would reformat code this module does not "
                        "own; the call was left unchanged",
                        _excerpt(text, int(call.lineno)),
                    )
                )
                continue
            old_line = lines[int(call.lineno) - 1]
            spans.append(
                _Span(
                    int(call.lineno),
                    0,
                    int(call.lineno),
                    len(old_line),
                    new_line,
                    SITE_CALL,
                    f"call to {old_name}",
                )
            )
            sites.append(
                CodemodSite(
                    rel,
                    int(call.lineno),
                    0,
                    SITE_CALL,
                    new_line,
                    f"call to {old_name}",
                    scopes.get(int(call.lineno), ""),
                )
            )
        if not spans:
            continue
        scan = _FileScan(rel=rel, text=text, sha=sha)
        file_edits, edit_unresolved = _build_edits(
            scan, spans, settings.edit_context_lines
        )
        edits.extend(file_edits)
        unresolved.extend(edit_unresolved)
    return sites, edits, unresolved


def _rewrite_call_line(
    lines: Sequence[str],
    node: ast.Call,
    drop_positional: Set[int],
    drop_keywords: Set[str],
) -> Tuple[str, bool]:
    """Return the rewritten source line for a call with arguments removed.

    Assumes the whole call expression is on ``node.lineno``. A call that
    spans several lines returns ``(line, False)`` so the caller names it
    instead of guessing.
    """
    start = int(node.lineno)
    end = int(getattr(node, "end_lineno", start) or start)
    if start != end:
        return lines[start - 1], False
    line = lines[start - 1]
    open_paren = line.rfind("(", 0, int(getattr(node.func, "col_offset", 0) or 0) + 1)
    if open_paren < 0:
        open_paren = line.find("(")
    close_paren = line.rfind(")")
    if close_paren < open_paren:
        return line, False
    inner = line[open_paren + 1 : close_paren]
    kept: List[str] = []
    positional = 0
    changed = False
    for piece in _split_top_level(inner):
        stripped = piece.strip()
        keyword_name = None
        if "=" in stripped and not stripped.startswith("*"):
            head, _, _tail = stripped.partition("=")
            if head.strip().isidentifier():
                keyword_name = head.strip()
        if keyword_name is not None:
            if keyword_name in drop_keywords:
                changed = True
                continue
            kept.append(piece)
            continue
        if positional in drop_positional:
            changed = True
            positional += 1
            continue
        kept.append(piece)
        positional += 1
    if not changed:
        return line, False
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    joined = ", ".join(piece.strip() for piece in kept if piece.strip())
    new_line = line[: open_paren + 1] + lead + joined + trail + line[close_paren:]
    return new_line, True


# ---------------------------------------------------------------------------
# application through the ordinary edit path
# ---------------------------------------------------------------------------


def apply_plan(
    plan: CodemodPlan,
    backend: Any,
    *,
    allow_incomplete: bool = False,
) -> CodemodApplyResult:
    """Apply every edit of ``plan`` through ``backend``, or none of them.

    Assumes ``backend`` exposes the ordinary typed-tool surface, i.e.
    ``execute(tool, arguments) -> ToolResult`` with ``ok``, ``value``,
    ``operation_id`` and ``error``, as
    ``execution.workspace.SafeToolBackend`` does. Every replacement is sent as
    an ordinary ``edit`` carrying the ``expected_revision`` captured when the
    plan was built, so the ordinary stale-read guard refuses a file that
    changed underneath the plan, and the ordinary unique-match guard refuses a
    span that is no longer unique. The revision is advanced from each applied
    edit's own ``post_hash``, so a second edit to the same file is a fresh,
    correctly bound precondition rather than a fabricated one.

    The apply is ALL-OR-NOTHING: if any single edit is refused, every
    already-applied operation is undone in reverse through the same backend's
    ``undo`` tool, and the refusal is returned. A partially applied rename is
    exactly the state this module exists to avoid.

    A plan whose receipt carries blocking unresolved sites is refused unless
    ``allow_incomplete=True``; the refusal names them. An already-refused plan
    is always refused.
    """
    if plan.refused:
        return CodemodApplyResult(
            ok=False,
            files_changed=0,
            sites_changed=0,
            error=f"codemod refused: {plan.receipt.reason}",
            error_kind="refused",
        )
    blocking = plan.receipt.blocking_unresolved
    if blocking and not allow_incomplete:
        return CodemodApplyResult(
            ok=False,
            files_changed=0,
            sites_changed=0,
            error=(
                f"codemod is incomplete: {len(blocking)} site(s) could not be "
                "resolved and are named in the receipt. Nothing was changed. "
                f"First named site: {blocking[0].path}:{blocking[0].line} "
                f"[{blocking[0].kind}]"
            ),
            error_kind="incomplete_receipt",
        )
    if backend is None:
        return CodemodApplyResult(
            ok=False,
            files_changed=0,
            sites_changed=0,
            error="no execution backend is bound, so a codemod cannot go through "
            "the ordinary edit path. Nothing was changed.",
            error_kind="no_runtime",
        )
    if not plan.edits:
        return CodemodApplyResult(
            ok=False,
            files_changed=0,
            sites_changed=0,
            error="the plan resolved no editable site. Nothing was changed.",
            error_kind="no_match",
        )

    revisions: Dict[str, str] = {}
    applied: List[Dict[str, Any]] = []
    undo_ids: List[str] = []
    for edit in plan.edits:
        expected = revisions.get(edit.path, edit.expected_sha256)
        result = backend.execute(
            "edit",
            {
                "path": edit.path,
                "old_string": edit.old_string,
                "new_string": edit.new_string,
                "expected_revision": expected,
            },
        )
        if not bool(getattr(result, "ok", False)):
            failures, rolled_back = _undo_all(backend, undo_ids)
            return CodemodApplyResult(
                ok=False,
                files_changed=0,
                sites_changed=0,
                applied=tuple(applied),
                undo_ids=tuple(undo_ids),
                rolled_back=rolled_back,
                rollback_failures=tuple(failures),
                error=(
                    f"edit refused at {edit.path}:{edit.line} ({edit.kind}): "
                    f"{getattr(result, 'error', 'unknown error')}. The codemod is "
                    "all-or-nothing, so every applied edit was undone."
                ),
                error_kind="edit_refused",
            )
        value = getattr(result, "value", None)
        post_hash = (
            str(value.get("post_hash") or "") if isinstance(value, Mapping) else ""
        )
        operation_id = str(getattr(result, "operation_id", "") or "")
        if post_hash:
            revisions[edit.path] = post_hash
        if operation_id:
            undo_ids.append(operation_id)
        applied.append(
            {
                "path": edit.path,
                "line": edit.line,
                "kind": edit.kind,
                "operation_id": operation_id,
                "post_hash": post_hash,
            }
        )
    return CodemodApplyResult(
        ok=True,
        files_changed=len({item["path"] for item in applied}),
        sites_changed=len(applied),
        applied=tuple(applied),
        undo_ids=tuple(undo_ids),
        rolled_back=False,
    )


def _undo_all(
    backend: Any, undo_ids: Sequence[str]
) -> Tuple[List[Dict[str, str]], bool]:
    """Undo applied operations in reverse through ``backend``.

    Assumes ``backend`` accepts the ordinary ``undo`` tool with an
    ``operation_id``. Returns ``(failures, rolled_back)``; a failure is
    reported rather than swallowed, because an un-undone edit is real residue
    a reader must see.
    """
    failures: List[Dict[str, str]] = []
    rolled = False
    for operation_id in reversed(list(undo_ids)):
        result = backend.execute("undo", {"operation_id": operation_id})
        if bool(getattr(result, "ok", False)):
            rolled = True
            continue
        failures.append(
            {
                "operation_id": operation_id,
                "error": str(getattr(result, "error", "unknown undo error")),
            }
        )
    return failures, rolled


def _open_backend(repo_path: str, approve: Any = None) -> Tuple[Any, Any]:
    """Return ``(backend, closer)`` for the ordinary safe edit path.

    Assumes ``execution.workspace.open_workspace`` is importable. ``approve``
    is the backend's own approval hook; when it is omitted the backend keeps
    its default behaviour, which REFUSES an ``edit`` that nobody approved. That
    default is deliberate: a standalone codemod must not be the one caller that
    can mutate a repository without the ordinary approval decision. A failure
    to import or construct is returned as a one-element tuple carrying the
    reason, so a caller reports an honest "no backend" rather than falling back
    to writing files.
    """
    try:
        from execution.workspace import SafeToolBackend, open_workspace
    except Exception as exc:
        return ((f"the ordinary edit path is unavailable: {exc}"), None)
    try:
        workspace = open_workspace(repo_path)
    except Exception as exc:
        return ((f"a workspace could not be opened for {repo_path}: {exc}"), None)

    def _close() -> None:
        try:
            workspace.close()
        except Exception:  # pragma: no cover - close is best effort
            pass

    kwargs: Dict[str, Any] = {}
    if callable(approve):
        kwargs["approve"] = approve
    return SafeToolBackend(workspace, **kwargs), _close


def plan_codemod(
    operation: str,
    repo_path: str,
    symbol: str,
    new_name: str = "",
    *,
    path: str = "",
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    renamed: Optional[Mapping[str, str]] = None,
    retyped: Optional[Mapping[str, str]] = None,
    config: Optional[Mapping[str, Any]] = None,
    index: Any = None,
) -> CodemodPlan:
    """Dispatch to :func:`rename_symbol` or :func:`update_signature`.

    Assumes ``operation`` is one of :data:`OPERATIONS`. An unknown operation
    is a refusal naming the operations that exist, never a default.
    """
    name = str(operation or "").strip().lower()
    if name == OP_RENAME:
        return rename_symbol(
            repo_path, symbol, new_name, path=path or None, config=config, index=index
        )
    if name == OP_SIGNATURE:
        return update_signature(
            repo_path,
            symbol,
            path=path or None,
            added=added,
            removed=removed,
            renamed=renamed,
            retyped=retyped,
            config=config,
            index=index,
        )
    return _refuse(
        name or "(none)",
        str(repo_path),
        str(symbol),
        f"unknown codemod operation {operation!r}; this module implements "
        + ", ".join(OPERATIONS),
    )


def run_codemod(
    operation: str,
    repo_path: str,
    symbol: str,
    new_name: str = "",
    *,
    path: str = "",
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    renamed: Optional[Mapping[str, str]] = None,
    retyped: Optional[Mapping[str, str]] = None,
    config: Optional[Mapping[str, Any]] = None,
    backend: Any = None,
    apply: bool = True,
    allow_incomplete: bool = False,
    approve: Any = None,
    index: Any = None,
) -> CodemodOutcome:
    """Plan a codemod and, when ``apply`` is true, apply it. The one-call path.

    Assumes ``operation`` is one of :data:`OPERATIONS` and ``repo_path`` is an
    existing repository directory. When ``backend`` is omitted and ``apply``
    is true, a workspace is opened for ``repo_path``, used, and closed, so a
    caller outside the kernel still goes through the same ordinary edit path
    (and the same digest and unique-match guards) rather than writing files.
    ``approve`` is that backend's own approval hook and defaults to ``None``,
    which leaves the backend refusing an unapproved ``edit`` — so a standalone
    codemod cannot become the one caller that mutates a repository without the
    ordinary approval decision. ``apply=False`` returns the plan and receipt
    without opening anything.
    """
    plan = plan_codemod(
        operation,
        repo_path,
        symbol,
        new_name=new_name,
        path=path,
        added=added,
        removed=removed,
        renamed=renamed,
        retyped=retyped,
        config=config,
        index=index,
    )
    if not apply or plan.refused:
        return CodemodOutcome(plan=plan, applied=None)
    if backend is not None:
        return CodemodOutcome(
            plan=plan,
            applied=apply_plan(plan, backend, allow_incomplete=allow_incomplete),
        )
    opened = _open_backend(repo_path, approve)
    if len(opened) == 1:
        reason, _closer = opened[0]
        return CodemodOutcome(
            plan=plan,
            applied=CodemodApplyResult(
                ok=False,
                files_changed=0,
                sites_changed=0,
                error=str(reason),
                error_kind="no_runtime",
            ),
        )
    backend_value, closer = opened
    try:
        return CodemodOutcome(
            plan=plan,
            applied=apply_plan(plan, backend_value, allow_incomplete=allow_incomplete),
        )
    finally:
        closer()


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_receipt(receipt: CodemodReceipt) -> str:
    """Render a completeness receipt as a bounded, human-readable block.

    Assumes a ``CodemodReceipt``. The UNRESOLVED section is never omitted for
    brevity: an empty list renders as an explicit "none", so "no unresolved
    sites" stays distinguishable from "the list was dropped".
    """
    lines = [
        f"codemod receipt ({receipt.operation} {receipt.symbol!r})",
        f"  language: {receipt.language}   resolution: {receipt.resolution}"
        f"   index: {receipt.index_source}",
        f"  files considered: {receipt.files_considered}"
        f"   files changed: {receipt.files_changed}",
        f"  sites found: {receipt.sites_found}"
        f"   sites planned: {receipt.sites_planned}"
        f"   sites changed: {receipt.sites_changed}",
        f"  complete: {receipt.complete}",
    ]
    if receipt.refused:
        lines.append(f"  REFUSED: {receipt.reason}")
    blocking = receipt.blocking_unresolved
    advisory = receipt.advisory_unresolved
    lines.append(f"  unresolved blocking: {len(blocking)}   advisory: {len(advisory)}")
    for item in blocking[:40]:
        lines.append(f"    ! {item.path}:{item.line} [{item.kind}] {item.detail}")
    if len(blocking) > 40:
        lines.append(
            f"    ! ... {len(blocking) - 40} more blocking site(s) in the receipt"
        )
    for item in advisory[:20]:
        lines.append(f"    ? {item.path}:{item.line} [{item.kind}] {item.detail}")
    if len(advisory) > 20:
        lines.append(
            f"    ? ... {len(advisory) - 20} more advisory site(s) in the receipt"
        )
    if not blocking and not advisory:
        lines.append("    (no unresolved sites)")
    for note in receipt.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def render_plan(plan: CodemodPlan) -> str:
    """Render a plan and its receipt for a model or a log.

    Assumes a :class:`CodemodPlan`. The change set is shown as one line per
    file with its per-kind site counts, plus every unresolved site, so a
    reader can review the whole change set without re-deriving it.
    """
    header = [
        f"# codemod {plan.operation}: {plan.detail or plan.symbol}",
        f"definition: {plan.definition or '(unresolved)'}",
    ]
    if plan.signature:
        header.append(f"declaration: {plan.signature}")
    by_file: Dict[str, List[CodemodSite]] = {}
    for site in plan.sites:
        by_file.setdefault(site.path, []).append(site)
    header.append("change set:")
    if not by_file:
        header.append("  (no file would change)")
    for path in sorted(by_file):
        kinds: Dict[str, int] = {}
        for site in by_file[path]:
            kinds[site.kind] = kinds.get(site.kind, 0) + 1
        detail = ", ".join(f"{count}x {kind}" for kind, count in sorted(kinds.items()))
        header.append(f"  {path}: {len(by_file[path])} site(s) [{detail}]")
    header.append(render_receipt(plan.receipt))
    return "\n".join(header)
