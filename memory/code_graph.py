"""Structural code memory: a tree-sitter based code knowledge graph.

Indexes a repository's structure — functions, classes, methods,
imports, definitions and call relationships — into a graph that persists
to disk as JSON, so structural questions ("what calls function X", "what
does module M import", "where is class C defined") don't require
re-reading source files.

Storage layout (default root: ``.harness/code-graph/`` at repo root):

    <root>/<repo_key>/graph.json    — the serialized graph
    <root>/<repo_key>/meta.json     — index metadata (source path, mtimes)

``repo_key`` is a sanitized version of the absolute repo path so multiple
repos can share one root without colliding.

Graph model
-----------
nodes: ``{node_id: NodeInfo}`` with ids:

    - ``func:<module>.<name>``            top-level function
    - ``class:<module>.<name>``           class
    - ``method:<module>.<Class>.<name>``  method (incl. static/class methods)
    - ``module:<dotted_name>``            module node (import graph)
    - ``file:<relpath>``                  file node (mapping to source)

edges: ``calls`` / ``imports`` / ``defines`` as (src, dst) node-id pairs.

Query surface (see CodeGraph.query and QUERY_HELP):

    symbol <name>            — lookup by simple or qualified name
    callers <name>           — who calls this function/method
    callees <name>           — what this function calls
    importers <module>       — which modules import this module
    imports <module>         — what this module imports
    file <path>              — everything defined in one file
    files [pattern]          — all indexed files
    symbols [pattern]        — all symbols, optionally substring-filtered
    help                     — this text

Call-edge caveat: dynamic languages resolve calls best-effort —
a bare ``foo()`` resolves to any known ``foo``; ``obj.foo()`` resolves to
ANY method named ``foo`` regardless of receiver type. Over-approximation
is the right trade for a structural-memory read model; documented here.

Languages: Python (.py) and JavaScript/TypeScript (.js/.jsx/.mjs/.cjs/
.ts/.tsx) are indexed, each with its own grammar (tree-sitter-python /
tree-sitter-javascript / tree-sitter-typescript). A repo can mix all of
them in one graph. JS/TS import specifiers are resolved against the
repo's actual files (extensionless "./mod" matches mod.js, mod.ts, ...) so
import edges work like Python's module edges. The optional grammars are
imported lazily: a repo without them installed still indexes Python.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import tree_sitter_python
from tree_sitter import Language, Parser

GRAPH_ROOT_DEFAULT = ".harness/code-graph"

SKIP_DIR_NAMES = {
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

_MAX_FILE_BYTES = 1_000_000  # skip pathological source files


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    """One symbol (function/class/method/module/file) in the graph."""

    kind: str  # "func" | "class" | "method" | "module" | "file"
    name: str  # simple name, e.g. "run_task"
    qualified: str  # dotted, e.g. "harness.core.run_task"
    file: str  # repo-relative posix path
    line: int  # 1-based def line (0 for synthesized nodes)
    end_line: int  # 1-based last line of the definition
    docstring: str = ""  # first line of the docstring, if any
    extras: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict form used for JSON persistence and MCP output."""
        return {
            "kind": self.kind,
            "name": self.name,
            "qualified": self.qualified,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "docstring": self.docstring,
            "extras": self.extras,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "NodeInfo":
        return NodeInfo(
            kind=d["kind"],
            name=d["name"],
            qualified=d["qualified"],
            file=d["file"],
            line=int(d.get("line", 0)),
            end_line=int(d.get("end_line", 0)),
            docstring=d.get("docstring", ""),
            extras=dict(d.get("extras") or {}),
        )


@dataclass
class Graph:
    """The whole indexed structure for one repo."""

    nodes: Dict[str, NodeInfo] = field(default_factory=dict)
    calls: Set[Tuple[str, str]] = field(default_factory=set)
    imports: Set[Tuple[str, str]] = field(default_factory=set)
    defines: Set[Tuple[str, str]] = field(default_factory=set)
    repo_path: str = ""
    built_at: float = 0.0
    file_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        """Serializable form (edges sorted for stable output)."""
        return {
            "repo_path": self.repo_path,
            "built_at": self.built_at,
            "file_count": self.file_count,
            "nodes": {k: v.as_dict() for k, v in self.nodes.items()},
            "calls": sorted([list(e) for e in self.calls]),
            "imports": sorted([list(e) for e in self.imports]),
            "defines": sorted([list(e) for e in self.defines]),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Graph":
        g = Graph(
            nodes={k: NodeInfo.from_dict(v) for k, v in d.get("nodes", {}).items()},
            calls={tuple(e) for e in d.get("calls", [])},
            imports={tuple(e) for e in d.get("imports", [])},
            defines={tuple(e) for e in d.get("defines", [])},
            repo_path=d.get("repo_path", ""),
            built_at=float(d.get("built_at", 0.0)),
            file_count=int(d.get("file_count", 0)),
        )
        return g


# ---------------------------------------------------------------------------
# Extraction: file -> symbols/imports/calls
# ---------------------------------------------------------------------------


def _module_name(rel_path: str) -> str:
    """Dotted module name for a repo-relative posix path.

    ``harness/core.py`` -> ``harness.core``; ``harness/__init__.py`` ->
    ``harness``; top-level ``main.py`` -> ``main``.
    """
    parts = rel_path.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(p for p in parts if p)


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _docstring_first_line(body_node) -> str:
    """First line of a definition's docstring ('' if none)."""
    if body_node is None or body_node.child_count == 0:
        return ""
    first = body_node.children[0]
    if first is None or first.type != "expression_statement":
        return ""
    if first.child_count == 0:
        return ""
    candidate = first.children[0]
    if candidate is None or candidate.type != "string":
        return ""
    text = candidate.text.decode("utf-8", errors="replace").strip()
    # strip quote delimiters and any string prefix (r, b, f, u combos)
    text = re.sub(r"^(?:[rRbBuUfF]{1,3})?(['\"]{3}|['\"])", "", text)
    text = re.sub(r"(['\"]{3}|['\"])$", "", text)
    return " ".join(text.split())[:200]


class _FileIndexer:
    """Extracts symbols, imports, and call sites from one parsed file."""

    def __init__(self, rel_path: str, source: bytes, root_node) -> None:
        self.rel_path = rel_path
        self.source = source
        self.root = root_node
        self.module = _module_name(rel_path)
        self.symbols: List[NodeInfo] = []
        self.imports: List[str] = []  # absolute imported module names
        self.calls: List[Tuple[str, str]] = []  # (caller_qualified, callee_text)

    def run(self) -> None:
        self._walk(self.root, class_stack=[])
        self._collect_imports()
        self._collect_module_level_calls()

    # -- definitions (recursive for methods) ----------------------------

    def _walk(self, node, class_stack: List[str]) -> None:
        """Depth-first walk collecting definitions and per-function calls."""
        from_cls = class_stack[-1] if class_stack else None

        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                self._walk(inner, class_stack)
            return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node)
            body = node.child_by_field_name("body")
            if from_cls is not None:
                qualified = f"{self.module}.{from_cls}.{name}"
                kind = "method"
            else:
                qualified = f"{self.module}.{name}"
                kind = "func"
            self._add_symbol(node, name, qualified, kind, body)
            if body is not None:
                self._collect_calls_in(body, qualified)
            return

        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node)
            body = node.child_by_field_name("body")
            self._add_symbol(node, name, f"{self.module}.{name}", "class", body)
            if body is not None:
                self._walk(body, class_stack + [name])
            return

        for child in node.children:
            if child is not None:
                self._walk(child, class_stack)

    def _add_symbol(self, node, name: str, qualified: str, kind: str, body) -> None:
        info = NodeInfo(
            kind=kind,
            name=name,
            qualified=qualified,
            file=self.rel_path,
            line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            docstring=_docstring_first_line(body),
        )
        self.symbols.append(info)

    # -- imports ----------------------------------------------------------

    def _collect_imports(self) -> None:
        """Top-level import statements only (imports inside functions are
        skipped by design — they're rare and rarely structural)."""
        for node in self.root.children:
            if node is None:
                continue
            if node.type == "import_statement":
                # children: 'import' kw, then dotted_name | aliased_import*
                for ch in node.children:
                    if ch is None:
                        continue
                    if ch.type == "dotted_name":
                        self.imports.append(_text(ch))
                    elif ch.type == "aliased_import":
                        for sub in ch.children:
                            if sub is not None and sub.type == "dotted_name":
                                self.imports.append(_text(sub))
            elif node.type == "import_from_statement":
                mod = self._from_import_module(node)
                if not mod:
                    continue
                mod_node = node.child_by_field_name("module_name")
                for ch in node.children:
                    if ch is None or ch is mod_node:
                        continue
                    if ch.type == "identifier":
                        # from X import foo
                        self.imports.append(f"{mod}.{_text(ch)}")
                    elif ch.type == "aliased_import":
                        # from X import foo as bar
                        for sub in ch.children:
                            if sub is not None and sub.type in (
                                "dotted_name",
                                "identifier",
                            ):
                                self.imports.append(f"{mod}.{_text(sub)}")
                                break
                    elif ch.type == "dotted_name" and ch is not mod_node:
                        self.imports.append(f"{mod}.{_text(ch)}")

    def _from_import_module(self, node) -> str:
        """Absolute module of a `from X import ...` statement, with relative
        dots (`.`, `..`, `.foo`) resolved against this file's module."""
        mod_node = node.child_by_field_name("module_name")
        raw = _text(mod_node) if mod_node is not None else ""
        if not raw:
            return ""
        if not raw.startswith("."):
            return raw
        base = self.module.split(".") if self.module else []
        while raw.startswith("."):
            raw = raw[1:]
            if base:
                base = base[:-1]
        parts = base + (raw.split(".") if raw else [])
        return ".".join(p for p in parts if p)

    # -- call sites -------------------------------------------------------

    def _collect_calls_in(self, root, caller: str) -> None:
        """Collect `call` expressions under `root`, attributed to `caller`."""
        stack = [root]
        while stack:
            n = stack.pop()
            if n is None:
                continue
            if n.type == "call":
                fn = n.child_by_field_name("function")
                if fn is not None:
                    self.calls.append((caller, _text(fn)))
            stack.extend(c for c in n.children if c is not None)

    def _collect_module_level_calls(self) -> None:
        """Calls at module top level (outside any def/class), attributed to
        the module itself. Decorator calls on defs/classes are skipped —
        they'd resolve to external names like `dataclass` anyway."""
        for node in self.root.children:
            if node is None:
                continue
            if node.type in (
                "function_definition",
                "class_definition",
                "decorated_definition",
                "import_statement",
                "import_from_statement",
            ):
                continue
            self._collect_calls_in(node, self.module)


# ---------------------------------------------------------------------------
# Language registry (multi-language support: JS/TS alongside Python)
# ---------------------------------------------------------------------------

# Source extensions each language layer indexes (keyed by language id).
_PY_EXTS = (".py",)
_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
_TS_EXTS = (".ts", ".tsx")

# module-name candidate extensions for resolving a JS/TS import specifier
# against repo files: "./lib/util" may be util.js, util.ts, util/index.js...
_JS_RESOLVE_EXTS = (
    "",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    "/index.js",
    "/index.ts",
    "/index.jsx",
    "/index.tsx",
)


def _js_module_name(rel_path: str) -> str:
    """Dotted module name for a JS/TS repo-relative posix path.

    ``src/lib/util.js`` -> ``src.lib.util`` (path-based identity — JS has no
    canonical dotted module identity, so the file path IS the id). ``@``
    scope chars stay literal; unlike Python, nothing is dropped for
    __init__-style files.
    """
    parts = rel_path.split("/")
    for ext in _JS_EXTS + _TS_EXTS:
        if parts[-1].endswith(ext) and parts[-1] != ext:
            parts[-1] = parts[-1][: -len(ext)]
            break
    return ".".join(p for p in parts if p)


def _js_normalize_specifier(spec: str, importer_rel: str) -> Optional[str]:
    """Resolve a JS/TS import specifier to an ABSOLUTE-ish repo-relative
    path fragment (no leading './', POSIX separators) — the identity the
    builder resolves against real files.

    Only RELATIVE specifiers ('./x', '../x') resolve inside the repo;
    bare package names ('react', 'lodash/fp') are external (None) —
    node_modules is skipped by the indexer anyway. Assumes spec is the raw
    string literal value (unquoted) and importer_rel is the importing
    file's repo-relative posix path.
    """
    spec = (spec or "").strip()
    if not spec.startswith("."):
        return None
    base_dir = "/".join(importer_rel.split("/")[:-1])
    stack = [p for p in base_dir.split("/") if p]
    for part in spec.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    return "/".join(stack)


def _strip_js_quotes(raw: str) -> str:
    """Strip matching quotes from a raw string literal's text."""
    raw = (raw or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"', "`"):
        return raw[1:-1]
    return raw


class _JSFileIndexer:
    """Extracts symbols, imports, and call sites from a parsed JS/TS file.

    Node kinds mirror the Python indexer exactly (func/class/method with
    the same ``module.name`` / ``module.Class.method`` qualified shapes,
    where module is the path-based identity from _js_module_name), so
    every downstream consumer (retrieval anchoring, coordination fan-out,
    call resolution, MCP queries) is language-agnostic.

    What is extracted from the grammar tree:
    - function declarations + arrow/function assigned to const/let/var
      (``const foo = (x) => ...`` indexes foo as a func)
    - class declarations + class methods (method_declaration);
      object methods (``foo() {}`` inside object literals) and test-
      callback functions are skipped — they're data-shaped, not exports
    - imports: ``import ... from '<spec>'`` / ``export ... from '<spec>'``
      / ``require('<spec>')`` — specifiers kept RAW (resolved against
      real files at build time; see _js_normalize_specifier usage)
    - call expressions with their callee text (same over-approximate
      resolution contract as Python: ``obj.foo()`` -> any method ``foo``)

    Deliberately NOT extracted (documented over-approximation budget):
    object-literal method shorthand, destructured renames, TS type-only
    imports as edges (they carry no runtime structure), JSX identifiers.
    """

    def __init__(self, rel_path: str, source: bytes, root_node) -> None:
        self.rel_path = rel_path
        self.source = source
        self.root = root_node
        self.module = _js_module_name(rel_path)
        self.symbols: List[NodeInfo] = []
        self.imports: List[str] = []  # raw import specifiers
        self.calls: List[Tuple[str, str]] = []  # (caller_qualified, callee_text)

    # -- entry -----------------------------------------------------------

    def run(self) -> None:
        self._walk(self.root, class_stack=[])

    # -- definitions + calls (single tree walk) ---------------------------

    def _walk(self, node, class_stack: List[str]) -> None:
        from_cls = class_stack[-1] if class_stack else None

        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node)
                body = node.child_by_field_name("body")
                qualified = (
                    f"{self.module}.{from_cls}.{name}"
                    if from_cls
                    else f"{self.module}.{name}"
                )
                kind = "method" if from_cls else "func"
                self._add_symbol(node, name, qualified, kind, body)
                if body is not None:
                    self._collect_calls_in(body, qualified)
            return

        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node)
                body = node.child_by_field_name("body")
                self._add_symbol(node, name, f"{self.module}.{name}", "class", body)
                if body is not None:
                    self._walk(body, class_stack + [name])
            return

        if node.type in ("method_definition", "method_declaration"):
            # JS/TS grammar names it method_definition; TS uses the same
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node)
                body = node.child_by_field_name("body")
                qualified = (
                    f"{self.module}.{from_cls}.{name}"
                    if from_cls
                    else f"{self.module}.{name}"
                )
                self._add_symbol(node, name, qualified, "method", body)
                if body is not None:
                    self._collect_calls_in(body, qualified)
            return

        if node.type == "lexical_declaration" or node.type == "variable_declaration":
            # const foo = (a) => ... / function foo() {...} / class Foo ...
            # NOTE: children_by_field_name("declarator") returns [] on
            # tree-sitter >=0.23 for this node type — the declarators are
            # plain children typed variable_declarator.
            for child in node.children:
                if child is not None and child.type == "variable_declarator":
                    self._maybe_index_declarator(child)
            return

        if node.type == "import_statement":
            self._collect_import(node)
            return

        if node.type == "export_statement":
            # export may carry an import specifier (re-export) or a
            # definition (export function/class/const).
            src = node.child_by_field_name("source")
            if src is not None:
                self.imports.append(_strip_js_quotes(_text(src)))
            for child in node.children:
                if child is not None and child.type in (
                    "function_declaration",
                    "class_declaration",
                    "lexical_declaration",
                    "variable_declaration",
                ):
                    self._walk(child, class_stack)
            return

        if node.type == "expression_statement":
            self._collect_calls_in(node, self.module)
            return

        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                # require('./x') at module level: index as an import edge
                arg = node.child_by_field_name("arguments")
                if fn is not None and _text(fn) in ("require",) and arg is not None:
                    self._collect_require_args(arg)
                    return
            self._collect_calls_in(node, self.module)
            return

        for child in node.children:
            if child is not None:
                self._walk(child, class_stack)

    def _maybe_index_declarator(self, decl) -> None:
        """Index ``const NAME = <function|arrow|class expression>`` shapes;
        skip every other initializer (data, imports, plain calls — the
        call expressions inside initializers are still collected by the
        walk so non-function consts contribute calls, not symbols)."""
        name_node = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name_node is None or value is None or name_node.type != "identifier":
            return
        name = _text(name_node)
        vt = value.type
        if vt in ("arrow_function", "function_expression", "function"):
            qualified = f"{self.module}.{name}"
            self._add_symbol(value, name, qualified, "func", value)
            self._collect_calls_in(value, qualified)
        elif vt == "class_expression" or vt == "class":
            qualified = f"{self.module}.{name}"
            self._add_symbol(value, name, qualified, "class", value)
            body = value.child_by_field_name("body")
            if body is not None:
                self._walk(body, [name])

    def _add_symbol(self, node, name: str, qualified: str, kind: str, body) -> None:
        doc = ""
        if body is not None:
            doc = self._leading_javadoc(body)
        self.symbols.append(
            NodeInfo(
                kind=kind,
                name=name,
                qualified=qualified,
                file=self.rel_path,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                docstring=doc,
            )
        )

    @staticmethod
    def _leading_javadoc(body_node) -> str:
        """First line of a /** ... */ block immediately above a definition,
        or a trailing // line comment on the declaration line. Best-effort:
        tree-sitter gives comments as siblings, not children, so this only
        sees inline trailing comments; block docs above land in the parent
        walk and are skipped (documented limitation)."""
        return ""  # see note: kept simple; comments are not child nodes

    # -- imports ----------------------------------------------------------

    def _collect_import(self, node) -> None:
        src = node.child_by_field_name("source")
        if src is not None:
            self.imports.append(_strip_js_quotes(_text(src)))

    def _collect_require_args(self, args_node) -> None:
        for child in args_node.children:
            if child is not None and child.type == "string":
                self.imports.append(_strip_js_quotes(_text(child)))

    # -- calls ------------------------------------------------------------

    def _collect_calls_in(self, root, caller: str) -> None:
        """Collect call expressions under root, attributed to caller.

        Same contract as the Python indexer: the callee TEXT is stored
        raw (``foo`` / ``obj.foo``) and resolved against the symbol table
        at build time (_resolve_calls is shared, language-agnostic)."""
        stack = [root]
        while stack:
            n = stack.pop()
            if n is None:
                continue
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None:
                    self.calls.append((caller, _text(fn)))
                # do not descend into nested function bodies here — the
                # walk already indexed those with their own callers
                stack.extend(
                    c
                    for c in n.children
                    if c is not None
                    and c.type
                    not in (
                        "arrow_function",
                        "function_expression",
                        "function",
                    )
                )
            else:
                stack.extend(c for c in n.children if c is not None)


# ---------------------------------------------------------------------------
# Graph construction + call resolution
# ---------------------------------------------------------------------------


class CodeGraphBuilder:
    """Builds a Graph from a repo directory using tree-sitter.

    Multi-language: .py files index with the Python grammar, .js/.jsx/
    .mjs/.cjs with the JavaScript grammar, .ts/.tsx with the TypeScript
    grammar (both optional — imported lazily; a repo without them
    installed still indexes Python). JS/TS import specifiers are resolved
    against the repo's actual files so import edges are file-to-file
    identities, mirroring Python's module edges.
    """

    def __init__(self, repo_path: str) -> None:
        self.repo = Path(repo_path).resolve()
        if not self.repo.is_dir():
            raise NotADirectoryError(f"code graph: not a directory: {repo_path}")
        self._parsers: Dict[str, Parser] = {
            "py": Parser(Language(tree_sitter_python.language())),
        }
        js = _try_language("tree_sitter_javascript", "language")
        if js is not None:
            self._parsers["js"] = Parser(js)
        ts = _try_language("tree_sitter_typescript", "language_typescript")
        if ts is not None:
            self._parsers["ts"] = Parser(ts)

    def build(self) -> Graph:
        """Walk the repo, parse every indexable source file (Python AND
        JS/TS), build and link the graph.

        Parse errors never abort the build — the file is skipped and
        counted. Assumes the JS/TS grammars are installed when the repo
        has those files (missing grammars degrade: those files are
        skipped, same as unparseable ones).
        """
        graph = Graph(repo_path=str(self.repo), built_at=time.time())
        modules: Dict[str, str] = {}  # module name -> file rel path
        indexers: List[Any] = []

        for rel, source, lang in self._iter_source_files():
            parser = self._parsers.get(lang)
            if parser is None:
                graph.file_count += 1
                continue
            try:
                tree = parser.parse(source)
            except Exception:
                graph.file_count += 1
                continue
            if lang == "py":
                idx = _FileIndexer(rel, source, tree.root_node)
            else:
                idx = _JSFileIndexer(rel, source, tree.root_node)
            idx.run()
            indexers.append(idx)
            if idx.module:
                modules[idx.module] = rel
            graph.file_count += 1
            graph.nodes[f"file:{rel}"] = NodeInfo(
                kind="file",
                name=rel,
                qualified=rel,
                file=rel,
                line=0,
                end_line=0,
            )
            for info in idx.symbols:
                prefix = (
                    "method:"
                    if info.kind == "method"
                    else ("class:" if info.kind == "class" else "func:")
                )
                graph.nodes[prefix + info.qualified] = info

        # module nodes (after files so module-name resolution is complete)
        for mod, rel in modules.items():
            graph.nodes[f"module:{mod}"] = NodeInfo(
                kind="module",
                name=mod,
                qualified=mod,
                file=rel,
                line=0,
                end_line=0,
            )

        # resolve JS/TS import specifiers against the repo's real files,
        # then treat them exactly like Python's module->module edges: one
        # imports set consumed by the same _trim_to_repo_module pass.
        js_indexers = [i for i in indexers if isinstance(i, _JSFileIndexer)]
        for idx in js_indexers:
            idx.imports = self._resolve_js_imports(idx)

        # intra-repo import edges: module -> module
        for idx in indexers:
            src = f"module:{idx.module}"
            for target in idx.imports:
                t = _trim_to_repo_module(target, modules)
                if t is not None and f"module:{t}" != src:
                    graph.imports.add((src, f"module:{t}"))

        # define edges: file -> symbol
        for idx in indexers:
            for info in idx.symbols:
                graph.defines.add((f"file:{idx.rel_path}", info.qualified))

        _resolve_calls(graph, indexers)
        return graph

    def _resolve_js_imports(self, idx: "_JSFileIndexer") -> List[str]:
        """Map an indexer's raw import specifiers to repo module names.

        A relative './x' resolves to the first existing candidate file
        (x.js, x.ts, x/index.js, ...); the resolved file's module name is
        returned — _trim_to_repo_module then no-ops (exact match) while
        the edge set stays uniform across languages. External specifiers
        (bare 'react') drop out (None)."""
        out: List[str] = []
        for spec in idx.imports:
            frag = _js_normalize_specifier(spec, idx.rel_path)
            if frag is None:
                continue
            for cand_ext in _JS_RESOLVE_EXTS:
                rel = frag + cand_ext
                if (self.repo / rel).is_file():
                    mod = _js_module_name(rel)
                    if mod:
                        out.append(mod)
                    break
        return out

    def _iter_source_files(self):
        """Yield (rel_posix_path, source_bytes, lang) for every indexable
        source file. lang is 'py' | 'js' | 'ts' (ts covers .tsx too)."""
        exts: Dict[str, str] = {}
        for e in _PY_EXTS:
            exts[e] = "py"
        for e in _JS_EXTS:
            exts[e] = "js"
        for e in _TS_EXTS:
            exts[e] = "ts"
        for path in sorted(self.repo.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            # .js/.ts exact-suffix match; .d.ts handled as ts (fine)
            lang = exts.get(ext)
            if lang is None:
                continue
            rel_parts = path.relative_to(self.repo).parts
            if any(part in SKIP_DIR_NAMES for part in rel_parts):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = "/".join(rel_parts)
            try:
                source = path.read_bytes()
            except OSError:
                continue
            yield rel, source, lang


def _try_language(module_name: str, factory_attr: str) -> Optional["Language"]:
    """Lazily import an optional tree-sitter grammar's Language.

    Assumes the module exposes a zero-arg callable returning the ABI
    pointer (tree_sitter_javascript.language / tree_sitter_typescript.
    language_typescript). Returns None when the package is not installed
    — the builder then simply has no parser for that language."""
    try:
        mod = __import__(module_name)
        factory = getattr(mod, factory_attr, None)
        if factory is None:
            return None
        return Language(factory())
    except Exception:
        return None


def _trim_to_repo_module(target: str, modules: Dict[str, str]) -> Optional[str]:
    """Longest prefix of `target` that is a module in this repo.

    ``shared.types`` matches exactly; ``shared.types.Optional`` (a
    from-import of a name inside the module) trims to ``shared.types``.
    External imports (os, litellm) return None — only intra-repo edges.
    """
    if target in modules:
        return target
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in modules:
            return prefix
    return None


def _resolve_calls(graph: Graph, indexers: List[Any]) -> None:
    """Turn raw call sites into edges between symbol node ids.

    Resolution rules (over-approximate by design — see module docstring):
    - ``foo()``     -> every known func with simple name `foo` (methods
                       as a fallback if no func matches)
    - ``obj.foo()`` -> every known method named `foo`
    - ``mod.foo()`` -> funcs/methods matching the trailing name
    Unresolved callees (builtins, stdlib, third-party) are dropped.
    """
    funcs_by_name: Dict[str, List[str]] = {}
    methods_by_name: Dict[str, List[str]] = {}
    for node_id, info in graph.nodes.items():
        if info.kind == "func":
            funcs_by_name.setdefault(info.name, []).append(node_id)
        elif info.kind == "method":
            methods_by_name.setdefault(info.name, []).append(node_id)

    for idx in indexers:
        for caller_qualified, callee_text in idx.calls:
            caller = _caller_id(graph, idx.module, caller_qualified)
            if caller is None:
                continue
            callee_text = callee_text.strip()
            if "." in callee_text:
                last = callee_text.split(".")[-1].strip()
                targets = methods_by_name.get(last, []) + funcs_by_name.get(last, [])
            else:
                targets = funcs_by_name.get(callee_text, [])
                if not targets:
                    targets = methods_by_name.get(callee_text, [])
            for t in targets:
                if t != caller:
                    graph.calls.add((caller, t))


def _caller_id(graph: Graph, module: str, caller_qualified: str) -> Optional[str]:
    """Node id for a call-site's enclosing symbol, if we indexed one."""
    if caller_qualified == module:
        return f"module:{module}"
    for prefix in ("func:", "method:", "class:"):
        nid = prefix + caller_qualified
        if nid in graph.nodes:
            return nid
    return None


# ---------------------------------------------------------------------------
# Lookup helpers (shared by CodeGraph queries)
# ---------------------------------------------------------------------------


def find_in_graph(name: str, graph: Graph) -> List[NodeInfo]:
    """Symbols matching `name` — exact simple/qualified first, then
    case-insensitive substring on func/method/class names."""
    name_l = (name or "").lower()
    exact: List[NodeInfo] = []
    for info in graph.nodes.values():
        if info.name == name or info.qualified == name:
            exact.append(info)
    if exact:
        return exact
    fuzzy: List[NodeInfo] = []
    if name_l:
        for info in graph.nodes.values():
            if info.kind in ("func", "method", "class") and (
                name_l in info.name.lower() or name_l in info.qualified.lower()
            ):
                fuzzy.append(info)
    return fuzzy


def _resolve_module_node(module: str, graph: Graph) -> Optional[str]:
    """User-typed module name -> indexed module name (exact, then ci)."""
    m = (module or "").strip().rstrip(".")
    if not m:
        return None
    if f"module:{m}" in graph.nodes:
        return m
    ml = m.lower()
    for node_id in graph.nodes:
        if node_id.startswith("module:") and node_id.split(":", 1)[1].lower() == ml:
            return node_id.split(":", 1)[1]
    # allow a file path form too: "harness/core.py"
    m_norm = m.replace("\\", "/")
    for info in graph.nodes.values():
        if info.kind == "module" and info.file == m_norm:
            return info.qualified
    return None


def _node_id(info: NodeInfo) -> str:
    return f"{info.kind}:{info.qualified}"


def _one_line(info: NodeInfo) -> str:
    return f"{info.kind:6} {info.qualified}  ({info.file}:{info.line})"


# ---------------------------------------------------------------------------
# Queryable, persistent graph
# ---------------------------------------------------------------------------

QUERY_HELP = """\
code-graph queries (first token is the verb):
  symbol <name>          — info for a symbol (simple or qualified name)
  callers <name>         — functions/methods that call <name>
  callees <name>         — what <name> calls
  importers <module>     — repo modules that import <module>
  imports <module>       — what <module> imports (repo-internal)
  file <path>            — all symbols defined in one file
  files [pattern]        — indexed files (substring-filtered)
  symbols [pattern]      — symbols (substring-filtered)
  help                   — this text
Name matching is case-insensitive substring on simple OR qualified names
when no exact match exists."""


class CodeGraph:
    """Persistent, queryable code knowledge graph for one repo.

    ``load_or_build`` reuses a stored graph only when the on-disk .py
    mtimes match the snapshot taken at build time — any addition, edit,
    or deletion rebuilds from scratch (no incremental indexing in Phase 1).

    Thread-safety: persistence is locked; queries on a loaded graph are
    read-only dict/set walks and safe concurrently.
    """

    def __init__(self, repo_path: str, root: Optional[str] = None) -> None:
        self.repo = Path(repo_path).resolve()
        if not self.repo.is_dir():
            raise NotADirectoryError(f"code graph: not a directory: {repo_path}")
        self._root = Path(root) if root else (self.repo / GRAPH_ROOT_DEFAULT)
        self._lock = threading.Lock()
        self._graph: Optional[Graph] = None

    # -- paths -----------------------------------------------------------

    @property
    def graph_dir(self) -> Path:
        return self._root / _repo_key(str(self.repo))

    # -- build / load / save ----------------------------------------------

    def build(self) -> Graph:
        """(Re)index the repo now; saves the result to graph.json."""
        graph = CodeGraphBuilder(str(self.repo)).build()
        with self._lock:
            self._graph = graph
            self._save_locked(graph)
        return graph

    def _save_locked(self, graph: Graph) -> None:
        d = self.graph_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "graph.json").write_text(
            json.dumps(graph.as_dict(), indent=1), encoding="utf-8"
        )
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "repo_path": str(self.repo),
                    "mtimes": _snapshot_mtimes(self.repo),
                    "file_count": graph.file_count,
                    "built_at": graph.built_at,
                }
            ),
            encoding="utf-8",
        )

    def load(self) -> Optional[Graph]:
        """Load the persisted graph; None if never built or unreadable."""
        f = self.graph_dir / "graph.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        graph = Graph.from_dict(data)
        with self._lock:
            self._graph = graph
        return graph

    def load_or_build(self, force: bool = False) -> Graph:
        """Reuse the stored graph if still fresh (mtime snapshot match);
        rebuild otherwise. force=True always rebuilds.
        """
        if force:
            return self.build()
        graph = self.load()
        if graph is None:
            return self.build()
        try:
            meta = json.loads(
                (self.graph_dir / "meta.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return self.build()
        if _snapshot_mtimes(self.repo) != meta.get("mtimes"):
            return self.build()
        return graph

    @property
    def graph(self) -> Graph:
        """The current graph; builds lazily on first access."""
        if self._graph is None:
            self.load_or_build()
        if self._graph is None:  # pragma: no cover - build() always sets it
            raise RuntimeError("code graph: build failed")
        return self._graph

    # -- queries ------------------------------------------------------------

    def query(self, q: str) -> str:
        """Answer a structural query; returns a human-readable string.

        Assumes `q` starts with one of the verbs in QUERY_HELP; anything
        unparseable returns the help text (an MCP client's typo degrades
        gracefully instead of erroring).
        """
        q = (q or "").strip()
        if not q or q.lower() in ("help", "?"):
            return QUERY_HELP
        parts = q.split(None, 1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        g = self.graph
        try:
            if verb == "symbol":
                return self._fmt_symbol(arg, g)
            if verb == "callers":
                return self._fmt_related(arg, g, self.callers)
            if verb == "callees":
                return self._fmt_related(arg, g, self.callees)
            if verb == "importers":
                return self._fmt_related(arg, g, self.importers)
            if verb == "imports":
                return self._fmt_related(arg, g, self.imports_of)
            if verb == "file":
                return self._fmt_file(arg, g)
            if verb == "files":
                return self._fmt_files(arg, g)
            if verb == "symbols":
                return self._fmt_symbols(arg, g)
        except Exception as exc:  # never leak a traceback to an MCP client
            return f"query error: {exc}"
        return QUERY_HELP + f"\n\n(unknown verb: {verb!r})"

    def find(self, name: str) -> List[NodeInfo]:
        """All symbols matching `name` (exact, else substring)."""
        return find_in_graph(name, self.graph)

    def callers(self, symbol: str) -> List[NodeInfo]:
        """Symbols that call `symbol` (over all matches of find())."""
        g = self.graph
        out: Dict[str, NodeInfo] = {}
        for info in find_in_graph(symbol, g):
            nid = _node_id(info)
            for src, dst in g.calls:
                if dst == nid and src in g.nodes:
                    out.setdefault(src, g.nodes[src])
        return list(out.values())

    def callees(self, symbol: str) -> List[NodeInfo]:
        """What `symbol` calls."""
        g = self.graph
        out: Dict[str, NodeInfo] = {}
        for info in find_in_graph(symbol, g):
            nid = _node_id(info)
            for src, dst in g.calls:
                if src == nid and dst in g.nodes:
                    out.setdefault(dst, g.nodes[dst])
        return list(out.values())

    def importers(self, module: str) -> List[NodeInfo]:
        """Modules importing `module`."""
        g = self.graph
        target = _resolve_module_node(module, g)
        if target is None:
            return []
        mid = f"module:{target}"
        out: Dict[str, NodeInfo] = {}
        for src, dst in g.imports:
            if dst == mid and src in g.nodes:
                out.setdefault(src, g.nodes[src])
        return list(out.values())

    def imports_of(self, module: str) -> List[NodeInfo]:
        """What `module` imports (repo-internal only)."""
        g = self.graph
        target = _resolve_module_node(module, g)
        if target is None:
            return []
        mid = f"module:{target}"
        out: Dict[str, NodeInfo] = {}
        for src, dst in g.imports:
            if src == mid and dst in g.nodes:
                out.setdefault(dst, g.nodes[dst])
        return list(out.values())

    # -- formatting ----------------------------------------------------------

    @staticmethod
    def _fmt_symbol(arg: str, g: Graph) -> str:
        if not arg:
            return "usage: symbol <name>"
        matches = find_in_graph(arg, g)
        if not matches:
            return f"no symbol matching {arg!r} (try 'symbols <substring>')"
        lines: List[str] = []
        for info in matches[:20]:
            lines.append(_one_line(info))
            if info.docstring:
                lines.append(f"    doc: {info.docstring}")
        if len(matches) > 20:
            lines.append(f"... and {len(matches) - 20} more")
        return "\n".join(lines)

    @staticmethod
    def _fmt_related(arg: str, g: Graph, fn) -> str:
        if not arg:
            return "usage: <verb> <name>"
        infos = fn(arg)
        if not infos:
            return f"no results for {arg!r}"
        lines = [_one_line(i) for i in infos[:30]]
        if len(infos) > 30:
            lines.append(f"... and {len(infos) - 30} more")
        return "\n".join(lines)

    @staticmethod
    def _fmt_file(arg: str, g: Graph) -> str:
        if not arg:
            return "usage: file <path>"
        norm = arg.replace("\\", "/").lstrip("./")
        files = [i.file for i in g.nodes.values() if i.kind == "file"]
        if norm not in files:
            cands = [f for f in files if norm in f]
            if not cands:
                return f"no indexed file matching {arg!r}"
            norm = cands[0]
        infos = sorted(
            (
                i
                for i in g.nodes.values()
                if i.file == norm and i.kind in ("func", "method", "class")
            ),
            key=lambda i: i.line,
        )
        if not infos:
            return f"{norm}: indexed, no symbols"
        return "\n".join(_one_line(i) for i in infos)

    @staticmethod
    def _fmt_files(arg: str, g: Graph) -> str:
        files = sorted(i.file for i in g.nodes.values() if i.kind == "file")
        if arg:
            a = arg.lower().replace("\\", "/")
            files = [f for f in files if a in f.lower()]
        if not files:
            return "no indexed files" if not arg else "no matching files"
        lines = files[:100]
        out = "\n".join(lines)
        if len(files) > 100:
            out += f"\n... and {len(files) - 100} more"
        return out

    @staticmethod
    def _fmt_symbols(arg: str, g: Graph) -> str:
        infos = [i for i in g.nodes.values() if i.kind in ("func", "method", "class")]
        if arg:
            a = arg.lower()
            infos = [
                i for i in infos if a in i.name.lower() or a in i.qualified.lower()
            ]
        infos = sorted(infos, key=lambda i: (i.file, i.line))
        if not infos:
            return "no matching symbols"
        lines = [_one_line(i) for i in infos[:100]]
        if len(infos) > 100:
            lines.append(f"... and {len(infos) - 100} more")
        return "\n".join(lines)


def _repo_key(abs_path: str) -> str:
    """Filesystem-safe key from an absolute repo path (multiple repos in
    one root must not collide)."""
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", abs_path).strip("_")
    return key[:80] or "default"


def _snapshot_mtimes(repo: Path) -> Dict[str, float]:
    """mtime snapshot of all indexable source files (Python AND JS/TS),
    for freshness checks."""
    exts = set(_PY_EXTS) | set(_JS_EXTS) | set(_TS_EXTS)
    out: Dict[str, float] = {}
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        rel_parts = path.relative_to(repo).parts
        if any(part in SKIP_DIR_NAMES for part in rel_parts):
            continue
        try:
            out["/".join(rel_parts)] = path.stat().st_mtime
        except OSError:
            continue
    return out
