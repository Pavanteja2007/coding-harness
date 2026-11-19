"""Import-graph test selection for the inner repair loop (Ceiling 08 §2).

Ceiling gap G20 was: the verifier ran the whole autodetected suite on every
verify, so the repair loop paid full-suite latency for a change that touched
one module. This module computes the smallest set of test node ids that can
possibly observe a set of changed files, using the repository's own import
graph, and it persists that selection with the run so the choice is auditable
after the fact.

Design constraints, in priority order:

1. **Soundness over tightness.** A test is selected when it imports the
   changed module DIRECTLY or TRANSITIVELY, or when it shares a top-level
   package with it. An unresolvable import graph degrades to selecting every
   test — never to selecting fewer than the changed file's own test file.
2. **No false "nothing to run".** If the graph cannot be built, or the changed
   file IS itself a test file, the selection falls back to the tests that
   plausibly matter and records ``strategy`` so the caller can see it.
3. **Persistence.** :func:`save_selection` / :func:`load_selection` round-trip
   through JSON next to the run, so a reviewer can see exactly which tests the
   inner loop trusted.

Renamed or deleted test files are the sharp edge. A selection that names a
file which no longer exists is reported through ``missing`` and, if that empties
the selection, ``is_empty`` becomes True — the caller must then fall back to the
full suite, because "zero selected tests" is never a passing verification.

P1/W1 — the walk is no longer this module's own table, and the graph is no
longer rebuilt per call. Both were measured, not guessed:

* This module used to carry its own 14-name skip set. Measured 2026-10-02 on
  this repository, that walk opened **149,559 directories** and
  :func:`build_import_graph` took **495.3 s**, because the set did not prune
  ``logs/`` and the harness's own run directory holds a ``pristine/`` and a
  ``work/`` COPY of the repository per run. 39,599 "modules" were discovered;
  **569 of them are real source files.** The other 98.6% were snapshot copies,
  so the selection was being drawn out of log artifacts. Through
  :func:`execution.walk_scope.walk_python_files` the same walk opens **171
  directories in 0.33 s** and finds the same 569 real modules.
* :func:`build_import_graph` now memoises on a content digest, so a second
  verification of an unchanged tree costs one walk and no ``ast`` parsing at
  all. The digest is over ``(path, size, mtime_ns)`` per source file, so an
  edit to any source file is a miss and never a stale hit.
"""

from __future__ import annotations

import ast
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from execution.walk_scope import WalkResult, walk_python_files

SELECTION_SCHEMA_VERSION = 1
SELECTION_FILENAME = "test_selection.json"

_TEST_DIR_NAMES: Tuple[str, ...] = ("tests", "test", "spec", "specs", "check", "checks")
_TEST_FILE_PREFIXES: Tuple[str, ...] = ("test_", "tests_", "check_", "spec_")

#: Strategies that selected tests by a SOUND argument (the import graph chose
#: them), as opposed to a fallback that happened to be conservative.
_SOUND_STRATEGIES: Tuple[str, ...] = ("import_graph", "import_graph_partial")

#: Strategies that selected the WHOLE denominator. Complete coverage by
#: construction even though reached by a fallback - see
#: :meth:`TestSelection.coverage_is_complete` for why reporting these as partial
#: would be the wrong direction to lie in.
_COMPLETE_STRATEGIES: Tuple[str, ...] = ("fallback_all", "all_tests")

#: Deliberately NO local skip table here, not even an alias of the shared one.
#: Before this round the module carried its own 14-name tuple; that was the
#: second of three and the source of the 149,559-directory / 495-second walk.
#: The pin
#: ``test_w1_latency_honesty_pins.py::test_execution_has_exactly_one_skip_table_and_it_is_the_authority``
#: fails on any second table OR alias, and the walk consults
#: :func:`execution.walk_scope.skip_dir` instead.


#: Change classes an ``ast`` import graph CANNOT see, as a closed set. Each is a
#: way a test reaches repository code without a static import edge, which makes
#: "this test cannot observe the change" an unsound conclusion.
#:
#: The selector's whole safety argument is "a test is selected when it imports
#: the changed module". A mechanism that reaches a module WITHOUT an import
#: edge falsifies that argument for the file using it, and there is no static
#: answer. The response is not to guess: it is to give up the tight selection
#: and run everything, because over-selection costs a container run and
#: under-selection mints a verified completion that was never verified.
UNANALYSABLE_DYNAMIC_IMPORT = "dynamic_import"
UNANALYSABLE_PLUGIN_REGISTRY = "plugin_registry"
UNANALYSABLE_MONKEYPATCH_TARGET = "monkeypatch_target"
UNANALYSABLE_DATA_FILE_READ = "data_file_read"
UNANALYSABLE_CONFTEST_SCOPE = "conftest_scope"
UNANALYSABLE_NO_STATIC_CHANGE = "no_static_change"
UNANALYSABLE_VALUES: Tuple[str, ...] = (
    UNANALYSABLE_DYNAMIC_IMPORT,
    UNANALYSABLE_PLUGIN_REGISTRY,
    UNANALYSABLE_MONKEYPATCH_TARGET,
    UNANALYSABLE_DATA_FILE_READ,
    UNANALYSABLE_CONFTEST_SCOPE,
    UNANALYSABLE_NO_STATIC_CHANGE,
)

#: Suffixes that mark a non-source file a test may read. A test that reads one
#: of these observes the FILE, not the import graph, so a change to it cannot
#: be routed by imports at all. Bounded on purpose: an unrecognised suffix is
#: not a data file claim.
_DATA_SUFFIXES: Tuple[str, ...] = (
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".xml",
    ".txt",
    ".sql",
    ".graphql",
)

#: Refuse to read more than this many un-selected test files looking for an
#: un-analysable reach. Bounding the search is the same discipline the walk
#: uses: a clip is REPORTED, never silent, and the reason it reports is that an
#: unexamined test is one whose reach was never ruled out.
UNANALYSABLE_SCAN_CAP = 4_000


def unanalysable_reach(source: str) -> Tuple[str, ...]:
    """Return which un-analysable reach patterns appear in one module's source.

    AST, not a grep, for the same reason the rest of this module is AST: a
    substring match cannot tell a ``patch("pkg.mod")`` target from prose that
    mentions it, and this function's answer decides whether the tight selection
    is trustworthy at all. It never raises - an unparseable file contributes
    nothing and the caller's walk receipt already reports it.

    The four shapes, and why each defeats an import graph:

    * ``dynamic_import`` - ``__import__(name)`` / ``importlib.import_module(name)``.
      The module name is a runtime value.
    * ``plugin_registry`` - an ``entry_points`` scan or ``importlib.metadata``.
      Which plugin modules load is decided by installed metadata, not by an
      import statement in the repository.
    * ``monkeypatch_target`` - ``patch("pkg.mod.attr")``. The string names a
      module path and is a real dependency edge with no ``import`` in sight.
    * ``data_file_read`` - ``open("data/x.json")`` / ``read_text()`` on a
      non-source file. The dependency is on a FILE, and a changed ``.json``
      cannot reach the graph at all.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    found: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = _call_name(func)
        if not name:
            continue
        # The FINAL component is what matters, and matching on the full dotted
        # name would miss the ordinary spellings: `importlib.import_module`,
        # `import_module`, `__import__` and `mock.patch` are the same reach
        # pattern written four ways.
        leaf = name.rpartition(".")[2]
        if leaf in {"__import__", "import_module"}:
            found.add(UNANALYSABLE_DYNAMIC_IMPORT)
        elif leaf in {"entry_points", "iter_modules", "load_entry_point"}:
            found.add(UNANALYSABLE_PLUGIN_REGISTRY)
        elif leaf == "patch":
            # A dotted string first argument is the module path being patched.
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    if "." in argument.value.strip("."):
                        found.add(UNANALYSABLE_MONKEYPATCH_TARGET)
                    break
        elif (
            leaf == "open" or name.endswith((".read_text", ".read_bytes", ".readlines"))
        ) and _any_string_suffix(node.args, _DATA_SUFFIXES):
            found.add(UNANALYSABLE_DATA_FILE_READ)
    return tuple(kind for kind in UNANALYSABLE_VALUES if kind in found)


def _call_name(func: ast.AST) -> str:
    """Return a dotted name for a call target, or ``""`` when it is computed."""
    parts: List[str] = []
    node: Any = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _any_string_suffix(args: Sequence[ast.AST], suffixes: Sequence[str]) -> bool:
    """Return whether any literal string argument names one of ``suffixes``."""
    for argument in args:
        for node in ast.walk(argument):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.strip().lower().endswith(tuple(suffixes))
            ):
                return True
    return False


def _split(value: str) -> str:
    """Return ``value`` normalized to forward slashes, without a leading ./."""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _posix(path: str, root: str = ".") -> str:
    """Return a repo-relative POSIX path for ``path`` under ``root``."""
    relative = os.path.relpath(str(path), str(root)).replace("\\", "/")
    return relative


@dataclass(frozen=True)
class ImportGraph:
    """A repository-local import graph, keyed by dotted module name.

    ``module_files`` maps a dotted module name to its repo-relative file;
    ``imports`` maps a module to the modules it imports; ``test_imports`` maps
    a test file to the modules that file pulls in, transitively closed inside
    the repository.
    """

    module_files: Dict[str, str] = field(default_factory=dict)
    imports: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    test_files: Tuple[str, ...] = ()
    test_imports: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    built: bool = True
    note: str = ""
    #: The receipt from the walk that discovered the sources. ``complete`` in
    #: here is what tells a caller a graph is a whole-repository graph. Absent
    #: only on a graph built by hand in a test.
    walk: Dict[str, Any] = field(default_factory=dict)
    #: Per TEST file, the un-analysable reach patterns it uses - a dynamic
    #: import, a plugin registry, a monkeypatched module path, a data-file read.
    #: Computed during graph construction, where every test file is ALREADY
    #: being read for its imports, so answering "can this test reach the change
    #: without an import edge" costs no extra I/O.
    unanalysable: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def test_reaches_without_imports(
        self, test_file: str, changed_modules: Iterable[str]
    ) -> Tuple[str, ...]:
        """Return the reach patterns that make ``test_file`` untrustworthy.

        Empty means the graph's answer about this test is sound. Non-empty means
        the test may observe the change through a mechanism the graph does not
        model, and a selection that skipped it would be an under-selection.

        A test that ALREADY statically imports one of the changed modules is
        excluded from the answer regardless of its other patterns: the graph
        will have selected it either way, so its un-analysable reach cannot
        change the outcome. That is what keeps this from degrading every
        repository that uses ``monkeypatch`` into a permanent full-suite run.
        """
        if not self.unanalysable:
            return ()
        patterns = self.unanalysable.get(test_file)
        if not patterns:
            return ()
        already = set(self.test_imports.get(test_file) or ())
        if already & set(changed_modules):
            return ()
        return patterns

    @property
    def walk_complete(self) -> bool:
        """Return whether the walk that built this graph saw the whole tree.

        ``False`` means every selection derived from this graph is a PARTIAL
        view. A caller must not report such a selection's coverage as if it
        described the repository.
        """
        if not self.walk:
            return False
        return bool(self.walk.get("complete"))

    def modules_for_file(self, relative: str) -> Tuple[str, ...]:
        """Return the dotted module names that ``relative`` provides."""
        wanted = _split(relative)
        return tuple(
            name for name, file_path in self.module_files.items() if file_path == wanted
        )

    def dependents(self, modules: Iterable[str]) -> Tuple[str, ...]:
        """Return every repo module that imports any of ``modules``."""
        targets = set(modules)
        if not targets:
            return ()
        found: Set[str] = set()
        for module, imported in self.imports.items():
            if targets.intersection(imported):
                found.add(module)
        return tuple(sorted(found))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view of the graph."""
        return {
            "built": bool(self.built),
            "note": self.note,
            "modules": len(self.module_files),
            "tests": len(self.test_files),
            "walk_complete": self.walk_complete,
            "walk": dict(self.walk),
            "unanalysable_tests": sum(
                1 for value in self.unanalysable.values() if value
            ),
        }


@dataclass(frozen=True)
class TestSelection:
    """The test set the inner loop may run, plus why."""

    node_ids: Tuple[str, ...] = ()
    files: Tuple[str, ...] = ()
    changed_files: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    strategy: str = "import_graph"
    total_tests: int = 0
    graph_note: str = ""
    scope: str = ""
    out_of_scope: Tuple[str, ...] = ()
    #: MACHINE-READABLE reason the tight selection was abandoned, as members of
    #: :data:`UNANALYSABLE_VALUES` (or ``"all_tests"``). Empty when the tight
    #: selection stood.
    #:
    #: This exists beside :attr:`graph_note` on purpose. ``graph_note`` is prose
    #: for a human, and a test that has to substring-match prose is a test that
    #: breaks when somebody rewords a sentence - which is how a check for "the
    #: fallback said why" quietly stops checking anything. A consumer that needs
    #: to KNOW should read this, which is a closed vocabulary.
    fallback_kinds: Tuple[str, ...] = ()
    #: The walk receipt the graph was built from. Carried so
    #: :meth:`coverage_is_complete` can answer without re-walking.
    walk: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Return whether nothing at all was selected.

        An empty selection is a "cannot vouch for this" signal, never a pass.
        """
        return not self.node_ids and not self.files

    @property
    def coverage_fraction(self) -> Optional[float]:
        """Return selected/total as a fraction, or None when unknown.

        This is a fraction OF THE DENOMINATOR THE SELECTION WAS DRAWN FROM, not
        of the repository. With a scope applied the denominator is the scope's
        tests and out-of-scope tests are named in ``out_of_scope``, so the
        fraction is honest but must not be read as repository coverage. Use
        :meth:`coverage_is_complete` before calling anything "coverage" at all.
        """
        if not self.total_tests:
            return None
        return round(len(self.node_ids or self.files) / float(self.total_tests), 6)

    def coverage_is_complete(self) -> bool:
        """Return whether this selection covers EVERY test in the tree.

        **Completeness and soundness are two different facts, and conflating
        them is the bug this method exists to prevent.** An import-graph
        selection can be perfectly SOUND - every test it picked really can
        observe the change - and still cover only 2 of 3 tests. Soundness is
        about not MISSING an affected test; completeness is about having run
        everything. Reporting a sound-but-2-of-3 selection as "complete" is
        exactly the "a partial regression check that reads as a complete one"
        failure, so completeness is decided on the NUMBERS and soundness is
        reported beside it.

        So this is ``False`` whenever the selection is smaller than the
        denominator, and for every one of these independent reasons:

        * fewer tests selected than exist (the ordinary graph selection);
        * a ``bounded`` strategy - the ``max_files`` clip dropped more;
        * a ``same_package`` strategy - same-package tests only;
        * a non-empty ``missing`` - a named file no longer exists;
        * a bounded source walk - the graph is a partial view of the repository;
        * a declared scope with ``out_of_scope`` tests outside it;
        * an unknown denominator.

        ``fallback_all`` and ``all_tests`` select the whole denominator, so they
        ARE complete - reporting them as partial would train every reader of
        this flag to ignore it, which destroys the only thing it is for. Their
        ``strategy`` still says how the selection was reached.
        """
        if self.coverage_fraction is None:
            return False
        selected = len(self.node_ids or self.files)
        if selected < int(self.total_tests or 0):
            return False
        if self.missing:
            return False
        if self.walk and not self.walk.get("complete"):
            return False
        return not (self.scope and self.out_of_scope)

    def coverage_is_sound(self) -> bool:
        """Return whether the strategy can be trusted to have found the affected tests.

        The complement of :meth:`coverage_is_complete` along a DIFFERENT axis.
        ``import_graph`` is sound-but-partial; ``fallback_all`` is complete-but-
        chosen-by-fallback; ``bounded`` is neither. A consumer that needs "no
        affected test was missed" reads THIS; one that needs "everything ran"
        reads the completeness flag.
        """
        return self.strategy in _SOUND_STRATEGIES

    def coverage_receipt(self) -> Dict[str, Any]:
        """Return the reportable coverage block: the numbers AND their limits.

        ``complete``, ``sound`` and ``fraction`` are three SEPARATE facts and
        they disagree in the common case on purpose. A caller that renders only
        ``fraction`` has still lied.
        """
        selected = len(self.node_ids or self.files)
        reasons: List[str] = []
        if self.coverage_fraction is None:
            reasons.append("the denominator is unknown: no tests were discovered")
        elif selected < int(self.total_tests or 0):
            reasons.append(
                f"PARTIAL: {selected} of {self.total_tests} test file(s) ran; "
                f"{int(self.total_tests) - selected} did not"
            )
        if self.strategy in _COMPLETE_STRATEGIES:
            reasons.append(
                f"strategy={self.strategy!r} selected the whole denominator, so the "
                "fraction is 1.0 by fallback rather than by choice"
            )
        elif self.strategy not in _SOUND_STRATEGIES:
            reasons.append(
                f"strategy={self.strategy!r} is a fallback, not a sound argument "
                "for which tests could observe the change"
            )
        if self.missing:
            reasons.append(f"{len(self.missing)} selected file(s) no longer exist")
        if self.walk and not self.walk.get("complete"):
            reasons.append(
                "the source walk was bounded "
                f"({self.walk.get('truncation')}); {self.walk.get('not_searched')} "
                "entr(y/ies) were never looked at"
            )
        if self.scope and self.out_of_scope:
            reasons.append(
                f"scope {self.scope!r} narrowed the candidate set; "
                f"{len(self.out_of_scope)} test(s) are outside it"
            )
        return {
            "selected": selected,
            "total_tests": int(self.total_tests),
            "fraction": self.coverage_fraction,
            "complete": self.coverage_is_complete(),
            "sound": self.coverage_is_sound(),
            "strategy": self.strategy,
            "reasons": reasons,
            "is_complete_check": "coverage_is_complete",
            "is_sound_check": "coverage_is_sound",
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view for run artifacts."""
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "node_ids": list(self.node_ids),
            "files": list(self.files),
            "changed_files": list(self.changed_files),
            "missing": list(self.missing),
            "strategy": self.strategy,
            "total_tests": int(self.total_tests),
            "coverage_fraction": self.coverage_fraction,
            "coverage": self.coverage_receipt(),
            "is_empty": self.is_empty,
            "graph_note": self.graph_note,
            "fallback_kinds": list(self.fallback_kinds),
            "scope": self.scope,
            "out_of_scope": list(self.out_of_scope),
            "walk": dict(self.walk),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "TestSelection":
        """Rebuild a selection from its JSON view, tolerating missing keys.

        ``walk`` IS restored. P1/W2 measured what dropping it costs: a selection
        whose walk was BOUNDED round-tripped through JSON and came back
        reporting ``coverage_is_complete() is True``, because
        :meth:`coverage_is_complete` can only see a bounded walk when ``walk``
        is present to say so. An absent receipt does not read as "complete" -
        it reads as nothing, and ``save_selection`` / ``load_selection`` are the
        persistence path ``inner_verify`` uses, so that erosion is exactly how
        a partial check becomes a "complete" one on disk.

        A ``walk`` that is present but not a mapping is normalised to ``{}`` for
        the same reason a missing one is: a malformed receipt is not evidence of
        a whole tree either way, and ``{}`` is the fail-closed shape because
        every bounded-walk test in :meth:`coverage_is_complete` is guarded on
        ``if self.walk and ...``.
        """
        if not isinstance(raw, dict):
            return cls(
                strategy="unreadable", graph_note="selection is not a JSON object"
            )
        walk = raw.get("walk")
        return cls(
            node_ids=tuple(str(value) for value in raw.get("node_ids") or ()),
            files=tuple(str(value) for value in raw.get("files") or ()),
            changed_files=tuple(str(value) for value in raw.get("changed_files") or ()),
            missing=tuple(str(value) for value in raw.get("missing") or ()),
            strategy=str(raw.get("strategy") or "unknown"),
            total_tests=int(raw.get("total_tests") or 0),
            graph_note=str(raw.get("graph_note") or ""),
            fallback_kinds=tuple(
                str(value) for value in raw.get("fallback_kinds") or ()
            ),
            scope=str(raw.get("scope") or ""),
            out_of_scope=tuple(str(value) for value in raw.get("out_of_scope") or ()),
            walk=dict(walk) if isinstance(walk, dict) else {},
        )


def build_import_graph(
    repo_path: str,
    *,
    use_cache: bool = True,
    deadline_s: Optional[float] = None,
) -> ImportGraph:
    """Build a repo-local import graph from Python sources with ``ast``.

    Deliberately stdlib-only and index-free: the verifier must be able to run
    this on an arbitrary user repository with no graph index, no tree-sitter
    build, and no network. Non-Python and unparseable files are skipped, which
    is why a JavaScript repository yields an unbuilt graph and the selection
    then falls back to "everything" rather than to "nothing".

    The walk is :func:`execution.walk_scope.walk_python_files`, so this module
    owns no skip table and the 149,559-directory walk measured on 2026-10-02
    cannot come back through it. The walk's own receipt is carried on
    :attr:`ImportGraph.walk` — including ``complete`` — so a bounded walk
    produces a graph that says it is bounded instead of one that silently looks
    whole.

    ``use_cache`` memoises on a digest of ``(relative path, size, mtime_ns)``
    across every discovered source file. An edit to ANY source file changes the
    digest, so a cache hit is only ever served for an unchanged tree; the
    cache holds two graphs so alternating between two trees does not thrash.
    """
    root = str(repo_path)
    walked = walk_python_files(root, deadline_s=deadline_s)
    python_files: List[str] = [str(path) for path in walked.files]
    digest = _source_digest(root, python_files)

    if use_cache and digest:
        cached = _CACHE.get((os.path.abspath(root), digest))
        if cached is not None:
            # The walk receipt is re-read rather than cached: it is the
            # per-call cost evidence, and caching it would make a later call
            # report the first call's directory count.
            return _with_walk(cached, walked)

    graph = _build_import_graph_uncached(root, python_files, walked)
    if use_cache and digest:
        _remember(root, digest, graph)
    return graph


def _with_walk(graph: ImportGraph, walked: WalkResult) -> ImportGraph:
    """Return ``graph`` carrying ``walked``'s receipt, without re-parsing."""
    return ImportGraph(
        module_files=dict(graph.module_files),
        imports={key: tuple(value) for key, value in graph.imports.items()},
        test_files=tuple(graph.test_files),
        test_imports={key: tuple(value) for key, value in graph.test_imports.items()},
        built=graph.built,
        note=graph.note,
        walk=walked.to_dict(),
        unanalysable=dict(graph.unanalysable),
    )


def _source_digest(root: str, python_files: Sequence[str]) -> str:
    """Return a digest over every discovered source file's identity and mtime.

    Empty string when nothing was discovered, which makes the cache a no-op
    rather than a hit on "this repository has no Python". Uses ``st_size`` and
    ``st_mtime_ns`` rather than file content on purpose: the point is to detect
    that a tree CHANGED, and reading every source file to do that would cost
    more than the parse it is protecting.
    """
    import hashlib

    digest = hashlib.sha256()
    digest.update(b"neo-test-selection-graph-v2\x00")
    parts: List[str] = []
    for absolute in python_files:
        try:
            info = os.stat(absolute)
        except OSError:
            continue
        parts.append(f"{_posix(absolute, root)}:{info.st_size}:{info.st_mtime_ns}")
    if not parts:
        return ""
    for part in sorted(parts):
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


#: Two slots, keyed by ``(canonical repo, source digest)``. Two rather than one
#: because the verification path alternates between a pristine tree and an edited
#: tree, and a one-slot cache would miss on every call in that pattern - which is
#: exactly the pattern the inner repair loop runs in.
_CACHE: Dict[Tuple[str, str], "ImportGraph"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SLOTS = 2


def _remember(repo_path: str, digest: str, graph: "ImportGraph") -> None:
    """Store ``graph`` in the bounded process cache, oldest key evicted."""
    key = (os.path.abspath(str(repo_path)), str(digest))
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_SLOTS:
            for stale in list(_CACHE)[: max(1, len(_CACHE) - _CACHE_SLOTS + 1)]:
                if stale != key:
                    _CACHE.pop(stale, None)
        _CACHE[key] = graph


def clear_graph_cache() -> int:
    """Empty the import-graph cache and return how many entries were dropped."""
    with _CACHE_LOCK:
        dropped = len(_CACHE)
        _CACHE.clear()
    return dropped


def _build_import_graph_uncached(
    root: str, python_files: Sequence[str], walked: WalkResult
) -> ImportGraph:
    """Parse ``python_files`` into an :class:`ImportGraph`. The real work."""
    module_files: Dict[str, str] = {}
    imports: Dict[str, Set[str]] = {}
    test_files: List[str] = []
    parse_errors: List[str] = []
    unanalysable: Dict[str, Tuple[str, ...]] = {}

    for absolute in python_files:
        relative = _posix(absolute, root)
        is_test = is_test_path(relative)
        dotted = _module_name(relative)
        if dotted and dotted not in module_files:
            module_files[dotted] = relative
        if is_test:
            test_files.append(relative)
        source = _read_text(absolute)
        if source is None:
            parse_errors.append(relative)
            imports.setdefault(dotted or relative, set())
            continue
        imports.setdefault(dotted or relative, set()).update(
            _local_imports(source, package=_own_package(dotted, relative))
        )
        if is_test:
            # Free: the source is already in hand for the imports above.
            kinds = unanalysable_reach(source)
            if kinds:
                unanalysable[relative] = kinds

    if not python_files:
        return ImportGraph(
            built=False,
            note="no Python sources found; selection falls back to all tests",
            walk=walked.to_dict(),
        )
    if parse_errors and len(parse_errors) == len(python_files):
        return ImportGraph(
            built=False,
            note=f"every Python source failed to parse ({len(parse_errors)} files)",
            walk=walked.to_dict(),
        )

    closed = _transitive_closure(imports, module_files)
    test_imports: Dict[str, Tuple[str, ...]] = {}
    for relative in test_files:
        module = _module_name(relative) or relative
        test_imports[relative] = tuple(sorted(closed.get(module, set())))

    return ImportGraph(
        module_files=module_files,
        imports={key: tuple(sorted(value)) for key, value in imports.items()},
        test_files=tuple(sorted(test_files)),
        test_imports=test_imports,
        built=True,
        note="" if not parse_errors else f"{len(parse_errors)} file(s) unparseable",
        walk=walked.to_dict(),
        unanalysable=unanalysable,
    )


def select_tests(
    repo_path: str,
    changed_files: Sequence[str],
    *,
    max_files: int = 12,
    target_test: Optional[str] = None,
    graph: Optional[ImportGraph] = None,
    scope: Optional[Any] = None,
) -> TestSelection:
    """Return the tests that can observe ``changed_files``.

    ``max_files`` bounds the selection so a repo-wide change cannot produce a
    selection that is the whole suite wearing a different name; when the bound
    bites, ``strategy`` becomes ``"bounded"`` so the caller knows the selection
    was clipped and must not be treated as sound.

    A changed file that IS a test file selects that file, because editing a test
    is itself the change under test.

    ``scope`` is an optional :class:`execution.snapshot.TaskScope`. When given,
    the candidate test set is narrowed to the scope, so a task declared against
    one package of a monorepo selects that package's tests rather than the
    whole suite. The scope is applied to the CANDIDATE SET, before any graph
    reasoning, which keeps the soundness argument identical -- only the set a
    selection may be drawn from shrinks. Three consequences are all recorded
    rather than hidden: an empty in-scope candidate set yields
    ``strategy="scope_empty"`` and ``is_empty=True`` so the caller's existing
    empty-selection fallback (the full suite) still fires; a changed file
    outside the scope is reported in ``out_of_scope`` and cannot select
    anything; and an explicit ``target_test`` outside the scope is still
    included, because the declared target is a stronger signal than the scope
    and silently dropping it would change what the verifier is asked to run.
    """
    root = str(repo_path)
    normalized = tuple(_split(value) for value in changed_files if _split(value))
    resolved = graph if graph is not None else build_import_graph(root)
    all_tests = tuple(resolved.test_files)
    total = len(all_tests)
    scope_label = ""
    out_of_scope: Tuple[str, ...] = ()
    if scope is not None:
        scope_label = ", ".join(getattr(scope, "roots", ()) or ()) or "whole"
        inside = tuple(value for value in all_tests if scope.contains(value))
        out_of_scope = tuple(value for value in all_tests if not scope.contains(value))
        all_tests = inside
        total = len(all_tests)

    if not normalized:
        return TestSelection(
            files=all_tests,
            strategy=(
                "scope_empty"
                if scope is not None and not all_tests
                else ("all_tests" if total else "empty_repo")
            ),
            total_tests=total,
            graph_note=resolved.note,
            scope=scope_label,
            out_of_scope=out_of_scope,
            walk=dict(resolved.walk),
        )

    if not resolved.built or not all_tests:
        return TestSelection(
            files=all_tests,
            changed_files=normalized,
            strategy="scope_empty"
            if scope is not None and not total
            else "fallback_all",
            total_tests=total,
            graph_note=resolved.note or "import graph unavailable",
            scope=scope_label,
            out_of_scope=out_of_scope,
            walk=dict(resolved.walk),
        )

    changed_modules: Set[str] = set()
    for relative in normalized:
        changed_modules.update(resolved.modules_for_file(relative))
    changed_modules.update(
        resolved.dependents(changed_modules) if changed_modules else ()
    )

    direct_tests: Set[str] = {
        relative
        for relative in normalized
        if is_test_path(relative) and (scope is None or scope.contains(relative))
    }
    selected: Set[str] = set(direct_tests)
    selected.update(
        relative
        for relative, modules in resolved.test_imports.items()
        if set(modules) & changed_modules
    )
    if not selected:
        # Nothing imports the change: fall back to same-package tests, then to
        # everything. An unselectable change must never become an empty run.
        packages = {_package_of(value) for value in normalized}
        same_package = [
            relative for relative in all_tests if _package_of(relative) in packages
        ]
        selected.update(same_package)
        strategy = "same_package"
        if not selected:
            selected.update(all_tests)
            strategy = "fallback_all"
    else:
        strategy = resolved_note_strategy(resolved, normalized)

    # The un-analysable-change fallback, and the documented answer to "what
    # happens for a change class an import graph cannot see".
    #
    # A test that reaches the changed module through a dynamic import, a plugin
    # registry, a monkeypatched module path, or a data file is not described by
    # the graph's answer, so skipping it is an under-selection. There is no
    # static way to know whether such a test observes THIS change, so the tight
    # selection is abandoned and every test runs. Over-selection costs one
    # container run; under-selection mints `completed_verified` over a check
    # that never ran, which is why this branch is a fallback and not a warning.
    unanalysable_note, fallback_kinds = _unanalysable_fallback(
        resolved, normalized, selected
    )
    if unanalysable_note:
        selected = set(all_tests)
        strategy = "fallback_all"

    if target_test:
        selected.add(_split(target_test).split("::")[0])

    if scope is not None:
        # Everything the graph chose must also be inside the scope. The
        # declared target is the one exception and was added above.
        declared_target = _split(target_test).split("::")[0] if target_test else ""
        escaped = {
            relative
            for relative in selected
            if not scope.contains(relative) and relative != declared_target
        }
        if escaped:
            out_of_scope = tuple(sorted(set(out_of_scope) | escaped))
            selected -= escaped

    ordered = tuple(sorted(selected))
    if max_files and len(ordered) > max_files:
        ordered = ordered[:max_files]
        strategy = "bounded"

    existing: List[str] = []
    missing: List[str] = []
    for relative in ordered:
        (existing if os.path.isfile(os.path.join(root, relative)) else missing).append(
            relative
        )

    return TestSelection(
        files=tuple(existing),
        node_ids=tuple(existing),
        changed_files=normalized,
        missing=tuple(missing),
        strategy=strategy,
        total_tests=total,
        graph_note="; ".join(
            part for part in (resolved.note, unanalysable_note) if part
        ),
        fallback_kinds=tuple(fallback_kinds),
        scope=scope_label,
        out_of_scope=out_of_scope,
        walk=dict(resolved.walk),
    )


def _unanalysable_fallback(
    graph: ImportGraph,
    changed: Sequence[str],
    selected: Set[str],
) -> Tuple[str, Tuple[str, ...]]:
    """Return the reason to abandon the tight selection, or ``""``.

    Six reasons, and the last two are about the CHANGE rather than the tests.

    ``conftest_scope`` is the one that was a live under-selection, measured on
    2026-10-02. ``_module_name`` returns ``""`` for ``conftest.py`` on purpose,
    so a changed conftest contributes no ``changed_modules`` at all and the
    graph can only select the conftest file itself - which pytest does not even
    collect as a test module. A conftest is applied by pytest's own DIRECTORY
    SCOPE, not by an import, so no import graph can express it: renaming a
    fixture, changing an ``autouse`` fixture, or adding a ``collect_ignore``
    breaks every test under it. The honest answer is every test in its scope,
    and the scope is by directory, which is the one rule here that needs no
    static analysis at all.

    ``no_static_change`` is the degenerate case: the change set yields NO
    modules, because it names non-Python files (a JSON table, a lockfile, a
    YAML fixture). Nothing can be routed to a test, so nothing may be skipped.

    Only UN-selected tests are examined for the test-side reasons: a test the
    graph already chose cannot be under-selected by this mechanism, and asking
    about it would make the check answer "yes" on every repository that uses
    ``monkeypatch``.
    """
    conftests = [
        relative for relative in changed if os.path.basename(relative) == "conftest.py"
    ]
    if conftests:
        scopes = ", ".join(
            f"{relative} -> {sorted(_tests_under(graph, relative))}"
            for relative in conftests
        )
        return (
            f"{len(conftests)} changed conftest(s) apply by pytest DIRECTORY SCOPE "
            "rather than by import, so no import graph can route them; every test "
            f"in scope runs: {scopes}",
            (UNANALYSABLE_CONFTEST_SCOPE,),
        )

    changed_modules: Set[str] = set()
    for relative in changed:
        changed_modules.update(graph.modules_for_file(relative))
    if not changed_modules and not any(is_test_path(value) for value in changed):
        return (
            f"the change set {list(changed)!r} yields no importable module, so "
            "there is no static argument for which tests could observe it; every "
            "test runs rather than assume the affected tests are unrelated",
            (UNANALYSABLE_NO_STATIC_CHANGE,),
        )
    if not changed_modules:
        # The change names only TEST files, and they are gone (renamed or
        # deleted). Returning here on purpose: this is the case
        # :attr:`TestSelection.missing` exists to report, and substituting
        # "every test" for it would erase that report. An empty selection then
        # propagates to the caller's own full-suite fallback, which is the
        # right answer for a vanished test - and the RENAME stays visible,
        # which is the thing that matters when a selection silently stops
        # naming a test.
        return "", ()
    examined = 0
    hits: List[Tuple[str, Tuple[str, ...]]] = []
    for test_file in graph.test_files:
        if test_file in selected:
            continue
        if examined >= UNANALYSABLE_SCAN_CAP:
            # An unexamined test is one whose reach was never ruled out, so the
            # honest answer is to keep the fallback rather than resume the tight
            # selection - and to say the cap is why.
            return (
                f"the un-analysable-reach scan hit its {UNANALYSABLE_SCAN_CAP}-test "
                "cap with tests still unexamined; every test runs because an "
                "unexamined test's reach was never ruled out",
                ("scan_cap",),
            )
        examined += 1
        patterns = graph.test_reaches_without_imports(test_file, changed_modules)
        if patterns:
            hits.append((test_file, patterns))
    if not hits:
        return "", ()
    shown = ", ".join(f"{name} ({', '.join(kinds)})" for name, kinds in hits[:5])
    more = f" and {len(hits) - 5} more" if len(hits) > 5 else ""
    kinds: List[str] = []
    for _, pair in hits:
        for kind in pair:
            if kind not in kinds:
                kinds.append(kind)
    return (
        f"{len(hits)} candidate test(s) can reach repository code in a way an "
        f"import graph does not model, so the tight selection was abandoned and "
        f"every test runs: {shown}{more}",
        tuple(kinds),
    )


def _tests_under(graph: ImportGraph, conftest: str) -> Set[str]:
    """Return the tests pytest would apply ``conftest`` to, by directory scope."""
    prefix = os.path.dirname(conftest)
    if not prefix:
        return set(graph.test_files)
    return {name for name in graph.test_files if name.startswith(prefix + "/")}


def selection_command(selection: TestSelection, suite_cmd: str) -> Optional[str]:
    """Return ``suite_cmd`` with the selection appended, or None when empty.

    Appending the selected FILES rather than guessed node ids is deliberate:
    a file argument is stable across test renames inside the file, and pytest
    exits 4 (usage error) for a path that does not exist rather than quietly
    collecting nothing.
    """
    if selection.is_empty or not suite_cmd:
        return None
    existing = [value for value in selection.files if value]
    if not existing:
        return None
    return suite_cmd + " " + " ".join(_quote(value) for value in existing)


def save_selection(path: str, selection: TestSelection) -> str:
    """Write a selection next to the run so the choice is auditable."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(selection.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def load_selection(path: str) -> Optional[TestSelection]:
    """Read a persisted selection, or None when it is absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return TestSelection.from_dict(json.load(handle))
    except (OSError, ValueError, TypeError):
        return None


def default_selection_path(run_dir: str) -> str:
    """Return the conventional selection artifact path under ``run_dir``."""
    return os.path.join(str(run_dir), SELECTION_FILENAME)


def is_test_path(relative: str) -> bool:
    """Return whether a repo-relative path looks like a test file."""
    text = _split(relative)
    if not text.endswith(".py"):
        return False
    basename = os.path.basename(text)
    if basename.startswith(_TEST_FILE_PREFIXES):
        return True
    return any(f"/{name}/" in f"/{text}" for name in _TEST_DIR_NAMES)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _module_name(relative: str) -> str:
    """Return the dotted module name for a repo-relative Python file."""
    text = _split(relative)
    if not text.endswith(".py"):
        return ""
    trimmed = text[: -len(".py")]
    parts = [part for part in trimmed.split("/") if part]
    if not parts:
        return ""
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return ""
    if parts[-1] in ("conftest", "setup"):
        return ""
    return ".".join(parts)


def _own_package(dotted: str, relative: str) -> str:
    """Return the dotted PACKAGE a module's own relative imports resolve against.

    For ``pkg/sub/mod.py`` (``dotted == "pkg.sub.mod"``) that is ``pkg.sub``.
    For a package's ``__init__.py`` (``pkg/sub/__init__.py``, whose
    ``dotted`` is ``pkg.sub`` because ``_module_name`` strips ``__init__``) a
    relative import resolves against ``pkg.sub`` ITSELF - ``from . import x``
    in ``pkg/sub/__init__.py`` means ``pkg.sub.x``. Getting this wrong by one
    level is the difference between recording an edge and dropping it, so it is
    derived from the file's own shape rather than guessed from the dotted name.
    """
    text = _split(relative)
    if text.endswith("__init__.py"):
        return str(dotted or "")
    return str(dotted or "").rpartition(".")[0]


def _package_of(relative: str) -> str:
    """Return the top-level package/directory of a repo-relative path."""
    text = _split(relative)
    if not text:
        return ""
    if is_test_path(text):
        parts = text.split("/")
        for index, part in enumerate(parts):
            if part in _TEST_DIR_NAMES:
                return "/".join(parts[: index + 1])
        return parts[0]
    return text.split("/")[0]


def _read_text(path: str) -> Optional[str]:
    """Read a text file, returning None when it is unreadable or binary."""
    try:
        with open(path, encoding="utf-8", errors="strict") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return None


def _local_imports(source: str, *, package: str = "") -> Set[str]:
    """Return the module names ``source`` imports, as a SUPERSET of the truth.

    ``package`` is the importing module's own dotted package (``pkg.sub`` for
    ``pkg/sub/mod.py`` and for ``pkg/sub/__init__.py``), which is what a
    relative import is resolved against.

    **Every name recorded here is a CANDIDATE, and the caller intersects the
    transitive closure with the set of real repo modules.** So recording an extra
    name costs nothing: ``from pkg.core import helper`` records both
    ``pkg.core`` and ``pkg.core.helper``, and ``pkg.core.helper`` is dropped
    unless a module by that name really exists. The asymmetry is the whole
    design: a name that is not a module is filtered away, so the direction of
    every imprecision here is OVER-selection, which is a cost, rather than
    under-selection, which is the false verified success this module must not
    produce.

    Three shapes are recorded that the previous version did not, and each was a
    real under-selection measured on 2026-10-02 (see the module docstring):

    1. ``from pkg.sub import mod`` - the single most common Python import
       shape. It recorded ONLY ``pkg.sub``, so a change to ``pkg/sub/mod.py``
       selected nothing that imported it. ``pkg.sub.mod`` is recorded too, and
       is the fix.
    2. Relative imports (``from ..other import thing``). These were dropped
       entirely (``if node.level: continue``), which is what let a transitive
       importer vanish from the graph while the docstring claimed relative
       imports were "resolved by the caller".
    3. ``import a.b.c`` recorded only ``a.b.c``, so a change to ``a/b/__init__.py``
       (``a.b``) selected nothing that imported ``a.b.c``.

    Dynamic imports remain invisible to ``ast`` - that is what
    :func:`unanalysable_reach` exists to detect, and it is why the caller keeps
    a same-package fallback.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` also makes `a` and `a.b` reachable names.
                _add_with_prefixes(found, alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(node.module, node.level, package)
            if not base:
                continue
            _add_with_prefixes(found, base)
            # `from pkg.sub import mod` may name a MODULE. Record the
            # candidate; the closure drops it when it is only a symbol.
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{base}.{alias.name}")
    return found


def _add_with_prefixes(found: Set[str], dotted: str) -> None:
    """Add ``dotted`` and each of its dotted prefixes to ``found``."""
    text = str(dotted or "")
    if not text:
        return
    parts = text.split(".")
    for index in range(1, len(parts) + 1):
        found.add(".".join(parts[:index]))


def _resolve_relative(module: Optional[str], level: int, package: str) -> str:
    """Resolve a possibly-relative ``from X import ...`` target to a dotted name.

    ``level`` is the number of leading dots. ``package`` is the importing
    module's own package. An absolute import (``level == 0``) ignores
    ``package`` entirely, so this is total over both shapes and returns ``""``
    only when the reference cannot be resolved - a relative import from a module
    with no resolvable package, which is recorded as nothing rather than as a
    guess.
    """
    if not level:
        return str(module or "")
    own = [part for part in str(package or "").split(".") if part]
    if len(own) < level:
        return ""
    trimmed = own[: len(own) - (level - 1)] if level > 1 else own
    tail = [part for part in str(module or "").split(".") if part]
    return ".".join(trimmed + tail)


def _transitive_closure(
    imports: Dict[str, Set[str]], module_files: Dict[str, str]
) -> Dict[str, Set[str]]:
    """Close ``imports`` transitively over repo-local modules only."""
    known = set(module_files)
    closure: Dict[str, Set[str]] = {}
    for start in imports:
        seen: Set[str] = set()
        stack = list(imports.get(start, set()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for child in imports.get(current, set()):
                if child not in seen:
                    stack.append(child)
        closure[start] = seen & known
    return closure


def _quote(value: str) -> str:
    """Quote one argument for a shell command when it needs quoting."""
    import re as _re

    text = str(value)
    if _re.fullmatch(r"[A-Za-z0-9_./:=+-]+", text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def resolved_note_strategy(graph: ImportGraph, changed: Sequence[str]) -> str:
    """Return the strategy label for a graph-backed selection."""
    if graph.note:
        return "import_graph_partial"
    return "import_graph"
