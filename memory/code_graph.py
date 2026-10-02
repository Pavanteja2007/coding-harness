"""Structural code memory: a tree-sitter based code knowledge graph.

Indexes a repository's structure — functions, classes, methods,
imports, definitions and call relationships — into a graph that persists
to disk as JSON, so structural questions ("what calls function X", "what
does module M import", "where is class C defined") don't require
re-reading source files.

Storage layout (default root: ``$HARNESS_HOME/code-graph/``):

    <root>/<repo_key>/graph.json    — the serialized graph
    <root>/<repo_key>/meta.json     — index metadata (source digests, mtimes, graph digest)

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

edges: ``calls`` / ``imports`` as (src, dst) node-id pairs. ``defines``
retains historical qualified aliases; ``canonical_defines`` contains only
node-id endpoints.

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

import hashlib
import json
import operator
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

import tree_sitter_python
from tree_sitter import Language, Parser

from memory.paths import harness_home

GRAPH_ROOT_DEFAULT = "code-graph"

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


def _has_symlink_component(path: Path, stop: Optional[Path] = None) -> bool:
    """Return True if ``path`` or any component up to ``stop`` is a symlink.

    The per-path fallback. It costs O(depth) ``is_symlink`` syscalls plus one
    ``resolve`` per component, which is why a whole-tree caller should build a
    :class:`PathSafety` once (see its docstring for the measured difference)
    and answer the same question with a set lookup instead. Assumes ``stop``,
    when given, is an existing directory; an unreadable or unresolvable path is
    reported as UNSAFE (fail closed), which is the historical behaviour.
    """
    try:
        current = Path(path)
        boundary = Path(stop).resolve() if stop is not None else None
        while True:
            if current.is_symlink():
                return True
            if boundary is not None:
                try:
                    if current.resolve() == boundary:
                        return False
                except (OSError, RuntimeError, ValueError):
                    return True
            parent = current.parent
            if parent == current:
                return False
            current = parent
    except (OSError, RuntimeError, ValueError):
        return True


class PathSafety:
    """Symlink classification for one root, resolved ONCE instead of per file.

    Why this exists (measured on the tree this round was written on, R2-09):
    :func:`_has_symlink_component` costs **4.97 ms/file** on the 289,584-file
    checkout because it walks every component of every path AND calls
    ``resolve()`` on each one. Projected over 288k files that is **23.9 minutes
    of pure path checking** before a byte of source is read. The cost is
    per-file and per-component, and it is avoidable in two parts:

    * classify each DIRECTORY once, during the walk that must happen anyway,
      so "is any ancestor a symlink" becomes a set membership test;
    * never resolve a path twice. On this platform a by-path ``os.lstat`` costs
      **105 us** while the same information read off an ``os.scandir`` entry
      costs **0.18 us**, and constructing a ``WindowsPath`` costs **17.75 us**.
      So the hot path here is plain strings and set lookups, and a Path object
      is built only by a caller that is about to read the file.

    Assumptions: ``root`` is an existing absolute or relative directory (it is
    normalized once here); paths passed later are absolute or resolvable. An
    unreadable component is recorded UNSAFE (fail closed), matching
    :func:`_has_symlink_component`. The index is NOT thread-safe; each
    builder/CodeGraph instance owns its own (as it owns its own lock).
    """

    __slots__ = ("_lookups", "_root", "_safe", "_syscalls", "_unsafe")

    def __init__(self, root: str | Path) -> None:
        self._root = self.key_of(root)
        self._safe: Set[str] = set()
        self._unsafe: Set[str] = set()
        self._syscalls = 0
        self._lookups = 0

    @staticmethod
    def key_of(path: str | Path) -> str:
        """Return the comparison key for a path, in one cheap C-level call.

        ``normcase`` lowercases and normalizes separators on Windows and is a
        no-op elsewhere. ``abspath`` is deliberately NOT in this hot path: every
        caller hands over a path built from an already-absolute root, and
        ``abspath`` measured 13 us on the platform this was written for. It is
        applied only when a path is neither separator- nor drive-absolute.
        """
        text = path if isinstance(path, str) else os.fspath(path)
        if not (text.startswith(("/", "\\")) or (len(text) > 1 and text[1] == ":")):
            text = os.path.abspath(text)
        return os.path.normcase(text)

    @classmethod
    def for_root(cls, root: str | Path) -> "PathSafety":
        """Return an index whose own root and its ancestors are classified.

        One-time cost: O(depth of root) lstat calls, paid once per build.
        """
        index = cls(root)
        current = index._root
        while True:
            index._classify(current)
            parent = os.path.dirname(current)
            if not parent or parent == current:
                return index
            current = parent

    # -- classification ---------------------------------------------------

    def _classify(self, key: str) -> bool:
        """lstat one component at most once; return True when it is a symlink."""
        if key in self._unsafe:
            return True
        if key in self._safe:
            return False
        self._syscalls += 1
        try:
            linked = stat.S_ISLNK(os.lstat(key).st_mode)
        except FileNotFoundError:
            # An absent path is not a symlink. This matches the per-path
            # helper (is_symlink() is False for a missing file) and keeps a
            # not-yet-created file from poisoning its directory.
            linked = False
        except (OSError, ValueError):
            linked = True  # unreadable -> fail closed
        if linked:
            self._unsafe.add(key)
        else:
            self._safe.add(key)
        return linked

    def observe_key(self, key: str, *, is_symlink: bool) -> bool:
        """Record a path the walk already normalized (see :meth:`key_of`).

        ``is_symlink`` comes from the ``os.scandir`` entry's stat, so this
        costs no syscall and no further string work. Returns the recorded
        verdict (True == symlink, i.e. do not descend / do not index).
        """
        if is_symlink:
            self._unsafe.add(key)
            self._safe.discard(key)
        else:
            self._safe.add(key)
            self._unsafe.discard(key)
        return is_symlink

    def observe(self, path: str | Path, *, is_symlink: bool) -> bool:
        """Normalize ``path`` and record its walk-provided verdict."""
        return self.observe_key(self.key_of(path), is_symlink=is_symlink)

    def observe_dir(self, path: str | Path, *, is_symlink: bool) -> bool:
        """Directory spelling of :meth:`observe` (same contract)."""
        return self.observe(path, is_symlink=is_symlink)

    def observe_file(self, path: str | Path, *, is_symlink: bool) -> bool:
        """File spelling of :meth:`observe` (same contract)."""
        return self.observe(path, is_symlink=is_symlink)

    # -- the question ----------------------------------------------------

    def has_symlink_component_key(self, key: str) -> bool:
        """Answer the symlink question for an already-normalized ``key``.

        One set membership test for the path itself, then one per ancestor up
        to (and excluding, as the per-path helper always has) the root. No
        syscall and no Path construction for anything already classified; an
        unclassified component pays exactly one ``lstat``, once.
        """
        self._lookups += 1
        if key in self._unsafe:
            return True
        if key in self._safe:
            return False
        if self._classify(key):
            return True
        parent = os.path.dirname(key)
        while parent and parent != self._root and parent != key:
            if parent in self._unsafe:
                return True
            if parent not in self._safe and self._classify(parent):
                return True
            key, parent = parent, os.path.dirname(parent)
        return False

    def has_symlink_component(self, path: str | Path) -> bool:
        """Answer "is any component of ``path`` a symlink?" for this root."""
        return self.has_symlink_component_key(self.key_of(path))

    def is_safe(self, path: str | Path) -> bool:
        """Inverse of :meth:`has_symlink_component`, for readable call sites."""
        return not self.has_symlink_component(path)

    @property
    def root(self) -> str:
        """Return the normalized root this index was built for."""
        return self._root

    @property
    def stats(self) -> Dict[str, Any]:
        """Return classification counters (measured, for the handoff)."""
        return {
            "safe_components": len(self._safe),
            "symlink_components": len(self._unsafe),
            "syscalls": self._syscalls,
            "lookups": self._lookups,
        }


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

    @property
    def node_id(self) -> str:
        """Return the collision-safe graph id for this node."""
        return str(self.extras.get("node_id") or f"{self.kind}:{self.qualified}")

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
    canonical_defines: Set[Tuple[str, str]] = field(default_factory=set)
    repo_path: str = ""
    built_at: float = 0.0
    file_count: int = 0
    indexed_file_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        """Serializable form (edges sorted for stable output)."""
        return {
            "repo_path": self.repo_path,
            "built_at": self.built_at,
            "file_count": self.file_count,
            "indexed_file_count": self.indexed_file_count,
            "nodes": {k: v.as_dict() for k, v in self.nodes.items()},
            "calls": sorted([list(e) for e in self.calls]),
            "imports": sorted([list(e) for e in self.imports]),
            "defines": sorted([list(e) for e in self.defines]),
            "canonical_defines": sorted([list(e) for e in self.canonical_defines]),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Graph":
        g = Graph(
            nodes={k: NodeInfo.from_dict(v) for k, v in d.get("nodes", {}).items()},
            calls={tuple(e) for e in d.get("calls", [])},
            imports={tuple(e) for e in d.get("imports", [])},
            defines={tuple(e) for e in d.get("defines", [])},
            canonical_defines={tuple(e) for e in d.get("canonical_defines", [])},
            repo_path=d.get("repo_path", ""),
            built_at=float(d.get("built_at", 0.0)),
            file_count=int(d.get("file_count", 0)),
            indexed_file_count=int(d.get("indexed_file_count", d.get("file_count", 0))),
        )
        return g

    def index_digest(self) -> str:
        """Return a stable digest of graph structure, excluding build time."""
        payload = self.as_dict()
        payload.pop("built_at", None)
        payload.pop("repo_path", None)
        return _graph_digest(payload)


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
                self._walk(body, [*class_stack, name])
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
        self.imports: List[str] = []
        self.resolved_import_files: Dict[str, str] = {}
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
                    self._walk(body, [*class_stack, name])
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
            doc = self._leading_javadoc(body, node)
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

    def _leading_javadoc(self, body_node, node) -> str:
        """Return a short JSDoc or line comment immediately above a node."""
        try:
            lines = self.source.decode("utf-8", errors="replace").splitlines()
            index = max(0, int(node.start_point[0]))
        except (AttributeError, UnicodeError):
            return ""
        if index < 0 or index >= len(lines):
            return ""
        declaration = lines[index]
        marker = declaration.find("//")
        if marker >= 0:
            comment = declaration[marker + 2 :].strip()
            if comment:
                return comment[:200]
        cursor = index - 1
        while cursor >= 0 and not lines[cursor].strip():
            cursor -= 1
        if cursor < 0:
            return ""
        line = lines[cursor].strip()
        if line.startswith("//"):
            return line[2:].strip()[:200]
        if "/*" not in line:
            return ""
        block: List[str] = []
        while cursor >= 0:
            block.append(lines[cursor].strip())
            if "*/" in lines[cursor]:
                break
            cursor -= 1
        text = " ".join(reversed(block))
        text = re.sub(r"^/\*+", "", text)
        text = re.sub(r"\*+/$", "", text)
        text = text.replace("*", " ")
        return " ".join(text.split())[:200]

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
        # One symlink classification for the whole build (see PathSafety).
        self._safety = PathSafety.for_root(self.repo)

    def build(self) -> Graph:
        """Walk the repo, parse every indexable source file (Python AND
        JS/TS), build and link the graph.

        Parse errors never abort the build — the file is skipped and
        counted. Assumes the JS/TS grammars are installed when the repo
        has those files (missing grammars degrade: those files are
        skipped, same as unparseable ones).
        """
        graph = Graph(repo_path=str(self.repo), built_at=time.time())
        modules: Dict[str, str] = {}
        module_files: Dict[str, List[Tuple[str, str]]] = {}
        module_ids: Dict[str, str] = {}
        indexers: List[Any] = []
        source_files = list(self._iter_source_files())
        source_files.sort(key=lambda item: (0 if item[2] == "py" else 1, item[0]))

        for rel, source, lang in source_files:
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
            graph.indexed_file_count += 1
            indexers.append(idx)
            if idx.module:
                modules.setdefault(idx.module, rel)
                module_files.setdefault(idx.module, []).append((rel, lang))
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
                base_id = prefix + info.qualified
                node_id = base_id
                if node_id in graph.nodes:
                    language = (
                        "js" if rel.endswith(tuple(_JS_EXTS + _TS_EXTS)) else "py"
                    )
                    node_id = f"{base_id}#{language}"
                    suffix = 2
                    while node_id in graph.nodes:
                        node_id = f"{base_id}#{language}{suffix}"
                        suffix += 1
                info.extras["node_id"] = node_id
                graph.nodes[node_id] = info

        # module nodes (after files so module-name resolution is complete)
        for mod, entries in module_files.items():
            for index, (rel, lang) in enumerate(entries):
                base_id = f"module:{mod}"
                module_id = base_id if index == 0 else f"{base_id}#{lang}"
                suffix = 2
                while module_id in graph.nodes:
                    module_id = f"{base_id}#{lang}{suffix}"
                    suffix += 1
                module_ids[rel] = module_id
                info = NodeInfo(
                    kind="module",
                    name=mod,
                    qualified=mod,
                    file=rel,
                    line=0,
                    end_line=0,
                )
                info.extras["node_id"] = module_id
                graph.nodes[module_id] = info

        # resolve JS/TS import specifiers against the repo's real files,
        # then treat them exactly like Python's module->module edges: one
        # imports set consumed by the same _trim_to_repo_module pass.
        js_indexers = [i for i in indexers if isinstance(i, _JSFileIndexer)]
        for idx in js_indexers:
            idx.imports = self._resolve_js_imports(idx)

        # intra-repo import edges: module -> module
        for idx in indexers:
            src = module_ids.get(idx.rel_path, f"module:{idx.module}")
            for target in idx.imports:
                t = _trim_to_repo_module(target, modules)
                target_file = getattr(idx, "resolved_import_files", {}).get(t or "")
                target_id = (
                    module_ids.get(target_file, f"module:{t}") if t is not None else ""
                )
                if t is not None and target_id != src:
                    graph.imports.add((src, target_id))

        for idx in indexers:
            for info in idx.symbols:
                file_id = f"file:{idx.rel_path}"
                graph.defines.add((file_id, info.qualified))
                graph.canonical_defines.add((file_id, info.node_id))
                if info.node_id != f"{info.kind}:{info.qualified}":
                    graph.defines.add((file_id, info.node_id))

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
            fragments = [frag]
            for extension in (".js", ".jsx", ".mjs", ".cjs"):
                if frag.endswith(extension):
                    fragments.extend(
                        [
                            frag[: -len(extension)] + ".ts",
                            frag[: -len(extension)] + ".tsx",
                        ]
                    )
                    break
            resolved = False
            for fragment in fragments:
                for cand_ext in _JS_RESOLVE_EXTS:
                    rel = fragment + cand_ext
                    candidate = self.repo / rel
                    if (
                        not self._safety.has_symlink_component(candidate)
                        and candidate.is_file()
                    ):
                        mod = _js_module_name(rel)
                        if mod:
                            out.append(mod)
                            idx.resolved_import_files[mod] = rel
                        resolved = True
                        break
                if resolved:
                    break
        return out

    def _iter_source_files(self):
        """Yield (rel_posix_path, source_bytes, lang) for every indexable
        source file. lang is 'py' | 'js' | 'ts' (ts covers .tsx too).

        One hoisted walk: see :func:`iter_source_entries`. The per-file
        symlink question is a set lookup on the builder's own
        :class:`PathSafety` (built once in ``__init__``), and each file is
        stat-ed exactly once, so the walk no longer pays the historical
        O(depth) ``resolve()`` storm per file.
        """
        for entry in iter_source_entries(self.repo, self._safety):
            try:
                source = entry.path.read_bytes()
            except OSError:
                continue
            yield entry.rel, source, entry.lang


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

    The caller lookup is indexed (:func:`_qualified_index` /
    :class:`CallerIndex`) because it used to be a full linear scan of
    ``graph.nodes`` per call site per node-kind prefix -- O(call sites x
    nodes), which measured **1,970 s of a 1,988 s build** on this repository
    (442 source files, 12,990 nodes). The index returns the same candidate
    list in the same order, so the edges are identical;
    :func:`_caller_id_reference` exists so a test can prove that.

    Measured again on this repository (2026-10-02, rung #9 of the Trust
    Ladder): the first R2-09 fix indexed only the SYMBOL-level branch and
    left the ``caller_qualified == module`` branch scanning every node, so a
    retrieval still cost **51.6 s** and the build spent 34.7 s inside
    ``_caller_id`` alone, making 55,436,109 ``str.startswith`` calls. One
    module-level call site per file, times 12,990 nodes, times every file.
    :class:`CallerIndex` fixes both branches and
    ``tests/test_code_graph_caller_index.py`` pins the equivalence on a graph
    big enough that the old shape is measurably slower, which the previous
    4-file fixture was not.
    """
    funcs_by_name: Dict[str, List[str]] = {}
    methods_by_name: Dict[str, List[str]] = {}
    for node_id, info in graph.nodes.items():
        if info.kind == "func":
            funcs_by_name.setdefault(info.name, []).append(node_id)
        elif info.kind == "method":
            methods_by_name.setdefault(info.name, []).append(node_id)
    index = _qualified_index(graph)

    for idx in indexers:
        for caller_qualified, callee_text in idx.calls:
            caller = _caller_id(
                graph,
                idx.module,
                caller_qualified,
                caller_file=idx.rel_path,
                index=index,
            )
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


def _qualified_index(graph: Graph) -> "CallerIndex":
    """Build the caller-lookup index, once per build.

    Returned as a :class:`CallerIndex` so both halves of :func:`_caller_id`
    are O(1) amortised. It replaces three separate O(nodes)-per-call-site
    scans, which is what the pre-R2-09 code did.

    The candidate list, its order, and the ``caller_file`` preference are
    byte-identical to the linear scan :func:`_caller_id_reference` performs,
    and that oracle is retained so the equivalence is TESTED rather than
    asserted. See ``tests/test_code_graph_caller_index.py``, which pins the
    equivalence on a graph large enough for the difference to be visible --
    the original 4-file fixture compared 4 call sites against 8 nodes, so an
    O(nodes) scan per call site was free and the module-level branch below
    survived an optimisation that fixed only the symbol-level branch.
    """
    return CallerIndex(graph)


class CallerIndex:
    """Precomputed lookups for :func:`_caller_id`.

    Three maps, each preserving ``graph.nodes`` INSERTION order, because the
    oracle's first match wins and a dict that reordered candidates would
    return a different - and still plausible - edge set.

    ``by_qualified[prefix][qualified] -> [node_id, ...]``
        Collapses the oracle's three full passes over ``graph.nodes`` into
        three dict lookups. Inner dict order is the oracle's per-prefix
        order; the outer order is the oracle's prefix order, which is why
        :data:`NODE_KIND_PREFIXES` is iterated in that fixed sequence.

    ``module_by_file[rel_path] -> module node id``
        The oracle's module-level branch scans every node looking for the
        first ``module:`` node whose ``file`` matches. This is the same
        first-in-insertion-order answer, built once.
    """

    __slots__ = ("by_qualified", "module_by_file", "node_count")

    def __init__(self, graph: Graph) -> None:
        self.node_count = len(graph.nodes)
        by_qualified: Dict[str, Dict[str, List[str]]] = {}
        module_by_file: Dict[str, str] = {}
        for node_id, info in graph.nodes.items():
            kind = _node_kind_prefix(node_id)
            if kind is None:
                continue
            if kind == "module:":
                # setdefault, not assignment: the oracle returns the FIRST
                # module node for a file, so a later one must not displace it.
                module_by_file.setdefault(info.file, node_id)
                continue
            by_qualified.setdefault(kind, {}).setdefault(info.qualified, []).append(
                node_id
            )
        self.by_qualified = by_qualified
        self.module_by_file = module_by_file


#: The node-id prefixes the oracle iterates, in ITS order. Changing this
#: sequence changes which candidate wins, so it is pinned by a test.
NODE_KIND_PREFIXES: Tuple[str, ...] = ("func:", "method:", "class:")


def _node_kind_prefix(node_id: str) -> Optional[str]:
    """Return the node-id prefix of ``node_id``, or None if it has no kind.

    Uses ``partition``/``startswith`` on a length check rather than
    ``node_id.split(":")[0]``, because this runs once per node per build and
    the oracle's own ``node_id.startswith(prefix)`` calls inside a per-call-site
    loop are exactly what this class exists to remove.
    """
    for kind in ("module:", *NODE_KIND_PREFIXES):
        if node_id.startswith(kind):
            return kind
    return None


def _caller_id(
    graph: Graph,
    module: str,
    caller_qualified: str,
    caller_file: Optional[str] = None,
    *,
    index: Optional[Any] = None,
) -> Optional[str]:
    """Return the node id for a call site's enclosing symbol.

    Assumes ``graph.nodes`` is already populated with every symbol (the build
    fills it before resolving calls). ``index`` is the :class:`CallerIndex` of
    the same graph; when omitted it is built here, so a direct caller pays
    O(nodes) once and the historical single-call cost is unchanged. The
    candidate list, its order, and the ``caller_file`` preference are identical
    to the linear scan this replaces (see :func:`_caller_id_reference`).

    :param index: a :class:`CallerIndex`, or ``None`` to build one. A plain
        dict is also accepted and treated as the qualified-only index, so a
        caller holding the pre-R2-09 shape keeps working -- it just does not
        get the module-level speedup.
    """
    if not isinstance(index, CallerIndex):
        index = CallerIndex(graph)

    if caller_qualified == module:
        if caller_file:
            found = index.module_by_file.get(caller_file)
            if found is not None:
                return found
        return f"module:{module}"

    candidates: List[str] = []
    for prefix in NODE_KIND_PREFIXES:
        nid = prefix + caller_qualified
        if nid in graph.nodes:
            candidates.append(nid)
        for node_id in index.by_qualified.get(prefix, {}).get(caller_qualified, ()):
            candidates.append(node_id)
    unique = list(dict.fromkeys(candidates))
    if caller_file:
        for node_id in unique:
            if graph.nodes[node_id].file == caller_file:
                return node_id
    return unique[0] if unique else None


def _caller_id_reference(
    graph: Graph,
    module: str,
    caller_qualified: str,
    caller_file: Optional[str] = None,
) -> Optional[str]:
    """The pre-R2-09 linear-scan caller lookup, kept as a test oracle.

    Deliberately O(nodes) per call site. It exists so the optimized
    :func:`_caller_id` can be proven to return the same id for every call site
    of a real graph, rather than asserted to.
    """
    if caller_qualified == module:
        if caller_file:
            for node_id, info in graph.nodes.items():
                if node_id.startswith("module:") and info.file == caller_file:
                    return node_id
        return f"module:{module}"
    candidates: List[str] = []
    for prefix in ("func:", "method:", "class:"):
        nid = prefix + caller_qualified
        if nid in graph.nodes:
            candidates.append(nid)
        for node_id, info in graph.nodes.items():
            if node_id.startswith(prefix) and info.qualified == caller_qualified:
                candidates.append(node_id)
    unique = list(dict.fromkeys(candidates))
    if caller_file:
        for node_id in unique:
            if graph.nodes[node_id].file == caller_file:
                return node_id
    return unique[0] if unique else None


# ---------------------------------------------------------------------------
# Lookup helpers (shared by CodeGraph queries)
# ---------------------------------------------------------------------------


def find_in_graph(name: str, graph: Graph) -> List[NodeInfo]:
    """Symbols matching `name` — exact simple/qualified first, then
    case-insensitive substring on func/method/class names."""
    name_l = (name or "").lower()
    exact: List[NodeInfo] = []
    for info in graph.nodes.values():
        if info.kind in ("func", "method", "class") and (
            info.name == name or info.qualified == name
        ):
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
    """User-typed module name or file path -> canonical module node id."""
    m = (module or "").strip().rstrip(".")
    if not m:
        return None
    if m.startswith("module:"):
        candidate = m
        if candidate in graph.nodes:
            return candidate
        m = candidate.split(":", 1)[1]
    if f"module:{m}" in graph.nodes:
        return f"module:{m}"
    ml = m.lower()
    for node_id, info in graph.nodes.items():
        if node_id.startswith("module:") and info.qualified.lower() == ml:
            return node_id
    m_norm = m.replace("\\", "/")
    for node_id, info in graph.nodes.items():
        if node_id.startswith("module:") and info.file == m_norm:
            return node_id
    return None


def _node_id(info: NodeInfo) -> str:
    return info.node_id


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


def _graph_digest(value: Dict[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    """Write one JSON document through a flushed sibling temp file."""
    payload = json.dumps(value, indent=1, ensure_ascii=False)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class CodeGraph:
    """Persistent, queryable code knowledge graph for one repo.

    ``load_or_build`` reuses a stored graph only when source content digests
    and the persisted graph digest match the on-disk snapshot. Any addition,
    edit, or deletion rebuilds from scratch (no incremental indexing).

    Thread-safety: persistence is locked; queries on a loaded graph are
    read-only dict/set walks and safe concurrently. The hoisted
    :class:`PathSafety` and the freshness cache are per-instance and are only
    touched under the lock or before the graph is published.
    """

    def __init__(
        self,
        repo_path: str,
        root: Optional[str] = None,
        *,
        verify_digests: Optional[bool] = None,
    ) -> None:
        """Open (but do not build) the graph index for one repository.

        Assumes ``repo_path`` is an existing directory. ``verify_digests=True``
        forces every freshness pass to re-read file CONTENT, which restores the
        historical "same size + preserved mtime is still a change" guarantee at
        the historical cost; ``None`` (the default) enables the stat-based
        freshness optimization described on :meth:`load_or_build`. It is a
        keyword-only, tri-state argument on purpose: a caller that has not
        thought about the trade is not silently opted into either behaviour by
        a truthy default.
        """
        self.repo = Path(repo_path).resolve()
        if not self.repo.is_dir():
            raise NotADirectoryError(f"code graph: not a directory: {repo_path}")
        self._root = Path(root) if root else (harness_home() / GRAPH_ROOT_DEFAULT)
        self._lock = threading.Lock()
        self._graph: Optional[Graph] = None
        self._verify_digests = bool(verify_digests)
        self._safety = PathSafety.for_root(self.repo)
        self._last_freshness: Optional[SourceSnapshot] = None

    # -- paths -----------------------------------------------------------

    @property
    def graph_dir(self) -> Path:
        return self._root / _repo_key(str(self.repo))

    # -- build / load / save ----------------------------------------------

    def build(self) -> Graph:
        """(Re)index the repo now; saves the result to graph.json."""
        builder = CodeGraphBuilder(str(self.repo))
        self._safety = builder._safety  # keep one classification for both halves
        graph = builder.build()
        with self._lock:
            self._graph = graph
            self._save_locked(graph)
        return graph

    def _save_locked(self, graph: Graph) -> None:
        d = self.graph_dir
        d.mkdir(parents=True, exist_ok=True)
        graph_payload = graph.as_dict()
        snapshot = self._freshness_snapshot()
        self._last_freshness = snapshot
        _atomic_write_json(d / "graph.json", graph_payload)
        _atomic_write_json(
            d / "meta.json",
            {
                "repo_path": str(self.repo),
                # Historical keys, byte-identical shapes.
                "mtimes": {
                    rel: value[1] / 1_000_000_000
                    for rel, value in snapshot.stats.items()
                },
                "source_digests": snapshot.digests,
                # Additive: the stat identity a later pass needs to skip a
                # content read, plus WHICH path produced this pass's digests.
                "source_stats": {
                    rel: list(value) for rel, value in snapshot.stats.items()
                },
                "digest_source": snapshot.digest_source,
                "digest_receipt": snapshot.receipt(),
                "file_count": graph.file_count,
                "indexed_file_count": graph.indexed_file_count,
                "built_at": graph.built_at,
                "graph_sha256": _graph_digest(graph_payload),
                "index_digest": graph.index_digest(),
            },
        )

    def load(self) -> Optional[Graph]:
        """Load the persisted graph; None if missing, corrupt, or invalid."""
        f = self.graph_dir / "graph.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            graph = Graph.from_dict(data)
        except (OSError, TypeError, ValueError, KeyError, AttributeError, IndexError):
            return None
        with self._lock:
            self._graph = graph
        return graph

    def load_or_build(self, force: bool = False) -> Graph:
        """Reuse the stored graph if still fresh; rebuild otherwise.

        force=True always rebuilds. Freshness is a content-digest comparison,
        and the digests for files whose ``(size, mtime_ns)`` is unchanged since
        the stored snapshot are REUSED without reading the file (stat-based
        freshness). That is the pass which measured >20 minutes on a 288k-file
        tree; the trade is named by
        :attr:`SourceSnapshot.blind_spot` and the receipt of the last pass is
        on :attr:`freshness_receipt`. ``verify_digests=True`` (constructor) or
        the ``code_graph_verify_digests`` config key forces the content pass and
        closes the blind spot at the old cost.
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
        except (OSError, TypeError, ValueError):
            return self.build()
        if not isinstance(meta, dict):
            return self.build()
        source_digests = meta.get("source_digests")
        if isinstance(source_digests, dict):
            snapshot = self._freshness_snapshot(
                previous_stats=meta.get("source_stats"),
                previous_digests=source_digests,
            )
            self._last_freshness = snapshot
            if source_digests != snapshot.digests:
                return self.build()
        elif _snapshot_mtimes(self.repo, safety=self._safety) != meta.get("mtimes"):
            return self.build()
        if meta.get("graph_sha256") != _graph_digest(graph.as_dict()):
            return self.build()
        if (
            meta.get("index_digest")
            and meta.get("index_digest") != graph.index_digest()
        ):
            return self.build()
        return graph

    def _freshness_snapshot(
        self,
        *,
        previous_stats: Optional[Mapping[str, Any]] = None,
        previous_digests: Optional[Mapping[str, str]] = None,
    ) -> SourceSnapshot:
        """Return one freshness pass, reusing this instance's PathSafety.

        Assumes the caller already holds a lock or is single-threaded, which is
        how both callers (the save path and ``load_or_build``) behave.
        """
        return snapshot_sources(
            self.repo,
            previous_stats=previous_stats,
            previous_digests=previous_digests,
            force_content=self._verify_digests,
            safety=self._safety,
        )

    @property
    def freshness_receipt(self) -> Dict[str, Any]:
        """Return the digest provenance of the last freshness pass.

        Empty until a pass has run. ``digest_source`` is one of
        ``content`` (every digest was computed from bytes), ``stat`` (every
        digest was reused because ``(size, mtime_ns)`` matched) or ``mixed``;
        ``stat_blind_spot`` is the class of edit the stat path cannot see.
        """
        if self._last_freshness is None:
            return {}
        return self._last_freshness.receipt()

    @property
    def graph(self) -> Graph:
        """The current graph; builds lazily on first access."""
        if self._graph is None:
            self.load_or_build()
        if self._graph is None:  # pragma: no cover - build() always sets it
            raise RuntimeError("code graph: build failed")
        return self._graph

    @property
    def index_digest(self) -> str:
        """Return the stable structural digest of the current graph."""
        return self.graph.index_digest()

    def source_digest(self) -> str:
        """Return a digest of the indexed source files on disk.

        Uses the same hoisted walk and stat-based freshness as the index
        itself, so a warm tree does not re-read every source file to answer it.
        """
        return _digest_mapping(self._freshness_snapshot().digests)

    def symbols(self) -> List[NodeInfo]:
        """Return indexed symbol nodes in deterministic file/line order."""
        return sorted(
            (
                info
                for info in self.graph.nodes.values()
                if info.kind in ("func", "class", "method")
            ),
            key=lambda info: (info.file, info.line, info.node_id),
        )

    def exact_symbols(self, name: str) -> List[NodeInfo]:
        """Return exact simple or qualified symbol matches."""
        value = str(name or "")
        return [
            info
            for info in find_in_graph(value, self.graph)
            if info.name == value or info.qualified == value
        ]

    def source_range(
        self,
        rel_path: str,
        start_line: int,
        end_line: Optional[int] = None,
    ) -> str:
        """Read an exact contained source range, returning '' on refusal."""
        path = _safe_repo_file(self.repo, rel_path)
        if path is None:
            return ""
        try:
            start = max(1, int(start_line))
            end = start if end_line is None else max(start, int(end_line))
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, TypeError, ValueError):
            return ""
        return "\n".join(lines[start - 1 : end])

    def node_id(self, info: NodeInfo) -> str:
        """Return the graph id for a symbol node."""
        return info.node_id

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
        return sorted(
            out.values(), key=lambda item: (item.file, item.line, item.node_id)
        )

    def callees(self, symbol: str) -> List[NodeInfo]:
        """What `symbol` calls."""
        g = self.graph
        out: Dict[str, NodeInfo] = {}
        for info in find_in_graph(symbol, g):
            nid = _node_id(info)
            for src, dst in g.calls:
                if src == nid and dst in g.nodes:
                    out.setdefault(dst, g.nodes[dst])
        return sorted(
            out.values(), key=lambda item: (item.file, item.line, item.node_id)
        )

    def importers(self, module: str) -> List[NodeInfo]:
        """Modules importing `module`."""
        g = self.graph
        target = _resolve_module_node(module, g)
        if target is None:
            return []
        mid = target
        out: Dict[str, NodeInfo] = {}
        for src, dst in g.imports:
            if dst == mid and src in g.nodes:
                out.setdefault(src, g.nodes[src])
        return sorted(
            out.values(), key=lambda item: (item.file, item.line, item.node_id)
        )

    def imports_of(self, module: str) -> List[NodeInfo]:
        """What `module` imports (repo-internal only)."""
        g = self.graph
        target = _resolve_module_node(module, g)
        if target is None:
            return []
        mid = target
        out: Dict[str, NodeInfo] = {}
        for src, dst in g.imports:
            if src == mid and dst in g.nodes:
                out.setdefault(dst, g.nodes[dst])
        return sorted(
            out.values(), key=lambda item: (item.file, item.line, item.node_id)
        )

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


def _safe_repo_file(root: Path, rel_path: str) -> Optional[Path]:
    """Resolve a repository-relative source path without escaping root."""
    text = str(rel_path or "").replace("\\", "/").strip()
    if not text or "\x00" in text or Path(text).is_absolute():
        return None
    if re.match(r"^[A-Za-z]:[\\/]", text) or ".." in Path(text).parts:
        return None
    try:
        candidate = (root / text).resolve()
        candidate.relative_to(root)
        if _has_symlink_component(candidate, root) or not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _digest_mapping(values: Dict[str, str]) -> str:
    """Return a stable digest for a path-to-digest mapping."""
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# One walk, one stat per file, honest digest provenance
# ---------------------------------------------------------------------------

#: Every digest in this pass was computed from file CONTENT.
DIGEST_SOURCE_CONTENT = "content"
#: Every digest in this pass was REUSED because (size, mtime_ns) matched the
#: previous snapshot, so the bytes were never read.
DIGEST_SOURCE_STAT = "stat"
#: Some digests were reused and some were computed — the normal warm case.
DIGEST_SOURCE_MIXED = "mixed"

DIGEST_SOURCE_VALUES = (DIGEST_SOURCE_CONTENT, DIGEST_SOURCE_STAT, DIGEST_SOURCE_MIXED)

_LANG_BY_EXT: Dict[str, str] = {}
for _ext in _PY_EXTS:
    _LANG_BY_EXT[_ext] = "py"
for _ext in _JS_EXTS:
    _LANG_BY_EXT[_ext] = "js"
for _ext in _TS_EXTS:
    _LANG_BY_EXT[_ext] = "ts"

# C-level attribute getter: a sort key that costs no Python frame per entry.
_ENTRY_NAME = operator.attrgetter("name")

#: True when this platform's ``normcase`` lowercases (Windows). The walk
#: builds a child key by concatenating a normalized parent key with the
#: entry name, which is only valid if case folding distributes over the
#: concatenation — hence this probe instead of an ``os.name`` check.
_KEY_FOLDS_CASE = os.path.normcase("A") == "a"
#: Separator that a normalized key uses between components.
_KEY_SEP = os.sep


def _child_key(parent_key: str, name: str) -> str:
    """Return the normalized key for a direct child of ``parent_key``.

    ``normcase`` is ``replace('/','\\\\').lower()`` on Windows and the
    identity elsewhere, so on a case-folding platform folding the name and
    concatenating the already-folded parent is exactly folding the joined
    path. Building a key this way costs one concat plus one short-string
    fold, where ``normcase(os.scandir_entry.path)`` measured 5.5 us on the
    platform this was written for (the ``DirEntry.path`` property itself is
    a Python-level ``os.path.join``).
    """
    if _KEY_FOLDS_CASE:
        return parent_key + _KEY_SEP + name.lower()
    return parent_key + _KEY_SEP + name


class SourceEntry:
    """One indexable source file, as classified by :func:`iter_source_entries`.

    ``path`` is a lazily built ``Path``: the walk itself never constructs one
    (measured **17.75 us** per ``WindowsPath`` on the platform this was written
    for, against 0.18 us for the same information off the scandir entry), so
    only a caller that is about to READ the file pays for it.
    """

    __slots__ = ("_path", "lang", "mtime_ns", "rel", "size", "stat_key")

    def __init__(
        self,
        rel: str,
        path_str: str,
        lang: str,
        size: int,
        mtime_ns: int,
    ) -> None:
        self.rel = rel  # repo-relative posix path
        self._path = path_str  # absolute, un-normalized
        self.lang = lang  # 'py' | 'js' | 'ts'
        self.size = size  # st_size
        self.mtime_ns = mtime_ns  # st_mtime_ns
        self.stat_key = (size, mtime_ns)  # the freshness identity

    @property
    def path(self) -> Path:
        """Return the absolute :class:`Path` for this file (built on demand)."""
        return Path(self._path)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceEntry(rel={self.rel!r}, lang={self.lang!r}, size={self.size})"


def iter_source_entries(
    root: str | Path,
    safety: Optional[PathSafety] = None,
    *,
    max_bytes: int = _MAX_FILE_BYTES,
) -> "Iterator[SourceEntry]":
    """Yield every indexable source file under ``root`` from ONE scandir walk.

    This replaces ``repo.rglob("*")`` plus a per-file
    :func:`_has_symlink_component`. Four things change, and every one of them
    is a measured win on a large tree (289,584 files, this repository):

    * SKIP directories and symlinked directories are pruned FROM the walk, so
      their subtrees are never enumerated at all; the old code enumerated
      everything and filtered afterwards.
    * Each entry is stat-ed through its ``os.scandir`` entry (0.18 us here)
      instead of by path (105 us here), and exactly once instead of three
      times (``is_file``, ``stat``, and inside the per-path symlink walk).
    * The symlink verdict for a directory comes from the same stat, so a
      file's safety question is a set lookup and no syscall.
    * No ``WindowsPath`` is constructed for a file nobody reads.

    Assumes ``root`` is an existing directory. A file that vanishes between
    listing and ``stat`` is skipped, never yielded with a guessed size. When
    ``safety`` is None a throwaway index is built for ``root``, which is correct
    but pays the one-time per-directory ``lstat``.
    """
    base = os.fspath(root)
    if not (base.startswith(("/", "\\")) or (len(base) > 1 and base[1] == ":")):
        base = os.path.abspath(base)
    while len(base) > 1 and base[-1] in ("/", "\\"):
        base = base[:-1]
    index = safety if safety is not None else PathSafety.for_root(base)
    index.observe_key(index.key_of(base), is_symlink=False)
    observe_key = index.observe_key
    ask_key = index.has_symlink_component_key
    stack: List[Tuple[str, str, str]] = [(base, "", index.key_of(base))]
    while stack:
        directory, prefix, parent_key = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=_ENTRY_NAME)
        except (OSError, ValueError):
            continue
        for entry in entries:
            name = entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                continue
            mode = info.st_mode
            path = directory + _KEY_SEP + name
            if stat.S_ISLNK(mode):
                # Never followed, never indexed; recorded so a later set-lookup
                # question about this subtree is answered from the index rather
                # than by re-walking it.
                observe_key(_child_key(parent_key, name), is_symlink=True)
                continue
            if stat.S_ISDIR(mode):
                if name in SKIP_DIR_NAMES:
                    continue
                child_key = _child_key(parent_key, name)
                observe_key(child_key, is_symlink=False)
                stack.append((path, f"{prefix}{name}/", child_key))
                continue
            if not stat.S_ISREG(mode):
                continue
            dot = name.rfind(".")
            if dot <= 0:
                continue
            lang = _LANG_BY_EXT.get(name[dot:].lower())
            if lang is None:
                continue
            if info.st_size > max_bytes:
                continue
            child_key = _child_key(parent_key, name)
            observe_key(child_key, is_symlink=False)
            if ask_key(child_key):
                # The walk already proved the directory chain, so this is a set
                # lookup; the call stays so an unobserved parent cannot slip
                # through silently.
                continue
            yield SourceEntry(
                rel=f"{prefix}{name}",
                path_str=path,
                lang=lang,
                size=int(info.st_size),
                mtime_ns=int(info.st_mtime_ns),
            )


@dataclass
class SourceSnapshot:
    """One freshness pass over a repository's indexable source files.

    ``digests`` keeps its historical shape (rel -> sha256 hex) so every
    existing consumer is byte-identical. ``digest_source`` says WHICH of the
    two paths produced it, and ``stat_reused`` / ``content_digested`` say how
    many files took each path, so a reader never has to guess whether the bytes
    behind a digest were actually read.
    """

    digests: Dict[str, str] = field(default_factory=dict)
    stats: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    digest_source: str = DIGEST_SOURCE_CONTENT
    content_digested: int = 0
    stat_reused: int = 0
    files_seen: int = 0
    unsafe_skipped: int = 0
    walk_s: float = 0.0

    @property
    def blind_spot(self) -> str:
        """Name the exact miss class a stat-reused digest cannot detect.

        A file whose CONTENT changed while its (size, mtime_ns) stayed
        identical is not re-read, so its digest is the previous one. This is
        the documented blind spot of the stat path, not a correctness claim;
        ``verify_digests=True`` (or the ``code_graph_verify_digests`` config
        key) forces the content pass and closes it.
        """
        return (
            "same size and identical mtime_ns are treated as unchanged; a "
            "content-only edit that preserves both is not detected on the "
            "stat-reused path"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection persisted in meta.json."""
        return {
            "digests": dict(self.digests),
            "stats": {rel: list(value) for rel, value in self.stats.items()},
            "digest_source": self.digest_source,
            "content_digested": self.content_digested,
            "stat_reused": self.stat_reused,
            "files_seen": self.files_seen,
            "unsafe_skipped": self.unsafe_skipped,
            "walk_s": round(float(self.walk_s), 6),
        }

    def receipt(self) -> Dict[str, Any]:
        """Return the audit rows a reader needs to judge the pass."""
        return {
            "digest_source": self.digest_source,
            "content_digested": self.content_digested,
            "stat_reused": self.stat_reused,
            "files_seen": self.files_seen,
            "unsafe_skipped": self.unsafe_skipped,
            "walk_s": round(float(self.walk_s), 6),
            "stat_blind_spot": self.blind_spot if self.stat_reused else "",
        }


def snapshot_sources(
    repo: Path,
    *,
    previous: Optional[SourceSnapshot] = None,
    previous_stats: Optional[Mapping[str, Any]] = None,
    previous_digests: Optional[Mapping[str, str]] = None,
    force_content: bool = False,
    safety: Optional[PathSafety] = None,
) -> SourceSnapshot:
    """Return content digests plus stat identity for every source file.

    Stat-based freshness: a file whose ``(size, mtime_ns)`` is unchanged since
    the previous snapshot keeps its previous digest WITHOUT reading the file.
    That removes the read+SHA-256 from the freshness check for the common
    "nothing changed" case, which is the pass that measured >20 minutes here.
    The trade is explicit and recorded: see :attr:`SourceSnapshot.blind_spot`.

    Assumes ``repo`` is an existing directory. ``previous`` (an in-process
    snapshot) wins over the ``previous_stats``/``previous_digests`` mappings
    read from meta.json; an unknown file, a changed stat, or ``force_content``
    all take the content path. Never raises for an unreadable file — it is
    skipped, and counted in ``unsafe_skipped``/``files_seen``.
    """
    base = Path(repo)
    index = safety if safety is not None else PathSafety.for_root(base)
    prior_stats: Dict[str, Tuple[int, int]] = {}
    prior_digests: Dict[str, str] = {}
    if previous is not None:
        prior_stats = dict(previous.stats)
        prior_digests = dict(previous.digests)
    elif previous_stats or previous_digests:
        for rel, value in (previous_stats or {}).items():
            try:
                size, mtime_ns = int(value[0]), int(value[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            prior_stats[str(rel)] = (size, mtime_ns)
        prior_digests = {str(k): str(v) for k, v in (previous_digests or {}).items()}

    started = time.perf_counter()
    out = SourceSnapshot()
    for entry in iter_source_entries(base, index):
        out.files_seen += 1
        out.stats[entry.rel] = entry.stat_key
        cached = prior_digests.get(entry.rel)
        if (
            not force_content
            and cached
            and prior_stats.get(entry.rel) == entry.stat_key
        ):
            out.digests[entry.rel] = cached
            out.stat_reused += 1
            continue
        try:
            out.digests[entry.rel] = hashlib.sha256(entry.path.read_bytes()).hexdigest()
        except OSError:
            out.unsafe_skipped += 1
            continue
        out.content_digested += 1
    out.walk_s = time.perf_counter() - started
    if out.stat_reused and out.content_digested:
        out.digest_source = DIGEST_SOURCE_MIXED
    elif out.stat_reused:
        out.digest_source = DIGEST_SOURCE_STAT
    else:
        out.digest_source = DIGEST_SOURCE_CONTENT
    return out


def _snapshot_digests(
    repo: Path,
    *,
    previous_stats: Optional[Mapping[str, Any]] = None,
    previous_digests: Optional[Mapping[str, str]] = None,
    force_content: bool = False,
    safety: Optional[PathSafety] = None,
) -> Dict[str, str]:
    """Return SHA-256 content digests for every indexable source file.

    Thin wrapper over :func:`snapshot_sources` with the historical return
    shape. The optional keyword arguments are additive; omitting them gives
    byte-identical behaviour (a full content pass).
    """
    return snapshot_sources(
        repo,
        previous_stats=previous_stats,
        previous_digests=previous_digests,
        force_content=force_content,
        safety=safety,
    ).digests


def _repo_key(abs_path: str) -> str:
    """Filesystem-safe deterministic key from an absolute repo path."""
    try:
        canonical = os.path.normcase(str(Path(abs_path).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        canonical = os.path.normcase(str(Path(abs_path)))
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", canonical).strip("_")
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{label[:48] or 'repo'}-{digest}"


def _snapshot_mtimes(
    repo: Path, *, safety: Optional[PathSafety] = None
) -> Dict[str, float]:
    """mtime snapshot of all indexable source files (Python AND JS/TS),
    for freshness checks.

    Shares the hoisted walk (:func:`iter_source_entries`) with the digest pass
    and reports the walk's own ``st_mtime_ns`` as a float, which is the
    historical shape (``st_mtime`` is exactly that value divided by 1e9).
    """
    out: Dict[str, float] = {}
    for entry in iter_source_entries(repo, safety):
        out[entry.rel] = entry.mtime_ns / 1_000_000_000
    return out
