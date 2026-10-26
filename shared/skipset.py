"""THE ONE skip-set authority for repository walks. This is ``shared/``.

Why this module exists, and why it is HERE rather than in ``harness/``
--------------------------------------------------------------------
``phases/DOCTRINE.md`` §8 records the defect this closes:

    ``_SKIP_DIRS`` drift: one retrieval path opened 124,774 directories in
    144.6 s where another opened 155 in 0.42 s. **The same walk. 345x.**

A walk's cost is decided entirely by its skip set, so a set that lives next to
the walk it governs is a set that drifts: the module that happens to forget one
entry pays the whole bill and nothing in the tree notices.

Phase 1 Wave 1 closed this **inside** ``harness/`` -- ``harness/skipset.py`` is
the authority there and three modules bind the same object. That was the right
fix and it is kept. It is not the whole fix, because a sweep of the tree found
the SAME table in two more places:

======================================  ======  ==========================
module                                  owner   its own table?
======================================  ======  ==========================
``harness/skipset.py``                   T1      yes -- ``SKIP_DIRS``
``execution/walk_scope.py``              T2      yes -- ``SKIP_DIR_NAMES``
``memory/code_graph.py``                 T4      yes -- ``SKIP_DIR_NAMES``
======================================  ======  ==========================

Three tables that agree *today* and nothing that keeps them agreeing is the
original defect with better branding. ``DOCTRINE.md`` §6: **one authority per
concern**; when two modules both compute something, one of them is wrong.

Why ``shared/`` and not one of the three
----------------------------------------
Because ``harness/`` cannot be imported by ``memory/`` without inverting the
layering (``memory`` is the layer ``harness`` retrieval reads), and
``execution/`` sits beside both. ``shared/`` is the only package in this
repository that all three may import, which makes it the only place an authority
can live that is not a copy.

The adoption, and what is deliberately NOT done here
----------------------------------------------------
``harness/skipset.py`` is **not** edited by this module's author: ``harness/``
belongs to T1. So the shape is:

* this module OWNS the set and its provenance;
* ``harness/skipset.py`` re-exports it (T1's one-line change, filed);
* ``memory/code_graph.py`` imports it directly (T4 owns ``memory/``, so this is
  applied here in this round);
* ``execution/walk_scope.py`` imports it directly (T2 owns ``execution/`` --
  filed, NOT applied).

Until the last two land, :func:`skip_dir_tables` reports the real count, and
``tests/test_skip_authority.py`` fails on it rather than accepting three tables
because two of them have the right values. **A shared authority that only one
module adopted is the original defect with better branding**, so the check
counts adopters, not values.

What changed semantically (stated, not buried)
----------------------------------------------
Unifying on the union means a walk that previously descended into ``logs/``,
``site-packages/``, ``Temp/`` or ``graphify-out/`` no longer does. That is not
only a performance decision: every one of those trees is another tool's output,
a third-party vendored package, or a scratch directory, and a code agent that
greps its own run logs will report its own history back to itself as source.
``harness/scan_mode.py`` already documented that reasoning; this makes it the
one rule.

Public surface
--------------
- :data:`SKIP_DIRS` -- the authority.
- :data:`SKIP_FILE_PAT` / :data:`LARGE_FILE_BYTES` -- the per-file siblings.
- :func:`skip_dirs_for` -- the ONLY way to obtain a variant, and it requires a
  reason so a differently-pruning walk is *visible* rather than private.
- :func:`skip_dirs_report` -- provenance, for a trace row or a handoff.
- :func:`ripgrep_glob_args` -- the same set expressed for ``rg --glob``.
- :func:`skip_dir_tables` -- how many independent tables exist and which
  modules still declare their own.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "LARGE_FILE_BYTES",
    "SKIP_DIRS",
    "SKIP_FILE_GLOBS",
    "SKIP_FILE_PAT",
    "SOURCE_OF_TRUTH",
    "is_skipped_dir",
    "ripgrep_glob_args",
    "skip_dir_tables",
    "skip_dirs_for",
    "skip_dirs_report",
    "skip_file_reasons",
]

#: Where this set came from, for the trace receipt and the handoff. Named as a
#: module path so a reader is told WHICH file is the authority without having
#: to infer it from which import they happened to see.
SOURCE_OF_TRUTH = "shared/skipset.py"

_BASE: Tuple[str, ...] = (
    # -- version control + editor state -------------------------------------
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    ".idea",
    ".vscode",
    ".vs",
    # -- python environments and caches --------------------------------------
    ".venv",
    "venv",
    "env",
    ".env",
    "virtualenv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".eggs",
    "*.egg-info",  # a glob, resolved explicitly - see _GLOBS below
    "site-packages",
    "dist-info",
    # -- other-language build output ----------------------------------------
    "node_modules",
    "bower_components",
    "vendor",
    "target",  # rust/java build dir
    "build",
    "dist",
    "out",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".gradle",
    ".terraform",
    # -- harness-owned state: ours, never a scan target --------------------
    ".harness",
    ".neo",
    "logs",
    "graphify-out",
    # -- tooling / audit output ----------------------------------------------
    "probe_logs",
    ".opencode",
    ".qwen",
    ".playwright-mcp",
    ".shots",
    # T1's harness-side authority carries this one and the transcription into
    # this file dropped it. That is the drift this whole module exists to stop,
    # and it happened on the FIRST commit -- so the entry is here and the
    # divergence is recorded rather than papered over.
    ".mypy_cache.d",
    "Temp",
    "tmp",
)

#: Entries that are globs rather than literal directory names. Kept apart so the
#: set-membership test stays a plain ``in`` -- it is the hot path of every walk.
_GLOBS: Tuple[str, ...] = ("*.egg-info",)

#: Patterns applied to a FILE name. Same rule for every walk.
SKIP_FILE_PAT = re.compile(
    r"(\.log$|\.pyc$|\.pyo$|\.lock$|package-lock\.json$|poetry\.lock$|\.pyd$|\.so$)"
)

#: Bytes. A larger file is not opened by a discovery walk. The editor enforces
#: the same ceiling, so a file too big to edit is too big to search.
LARGE_FILE_BYTES = 200_000

SKIP_DIRS: FrozenSet[str] = frozenset(name for name in _BASE if name not in _GLOBS)
SKIP_FILE_GLOBS: FrozenSet[str] = frozenset(_GLOBS)


def is_skipped_dir(name: str) -> bool:
    """Whether one directory name is pruned by the authority.

    Set lookup first, and a glob only for the handful of glob entries, because
    this is the hot path of every walk.
    """
    if name in SKIP_DIRS:
        return True
    return any(name.endswith(pattern.lstrip("*")) for pattern in SKIP_FILE_GLOBS)


def skip_dirs_for(
    *,
    drop: Optional[Iterable[str]] = None,
    add: Optional[Iterable[str]] = None,
    reason: str = "",
) -> FrozenSet[str]:
    """Return a variant of :data:`SKIP_DIRS`, for a walk that must differ.

    ``drop`` re-enables a directory the authority prunes; ``add`` prunes one it
    does not. Either way the caller must pass ``reason``, which is recorded in
    :func:`skip_dirs_report`, so a walk that prunes differently is *visible* in
    its receipt rather than being a second private literal that drifts.

    Prefer the default. Every ``drop`` is a retrieval-quality decision and every
    ``add`` is a performance decision; neither is free.
    """
    if not drop and not add:
        return SKIP_DIRS
    out: Set[str] = set(SKIP_DIRS)
    for name in drop or ():
        out.discard(str(name))
    for name in add or ():
        out.add(str(name))
    if not str(reason).strip():
        raise ValueError(
            "skip_dirs_for requires a non-empty reason: a walk that prunes "
            "differently from the authority must say why in its receipt."
        )
    return frozenset(out)


def skip_file_reasons(
    name: str, size_bytes: Optional[int] = None
) -> Tuple[str, ...]:
    """Return why a FILE is not a search candidate (empty tuple when it is).

    Split out so the per-file rules have one owner too, and so a receipt can
    name a reason instead of reporting a bare ``False``.
    """
    reasons: List[str] = []
    if SKIP_FILE_PAT.search(name):
        reasons.append("file_pattern")
    if size_bytes is not None and int(size_bytes) > LARGE_FILE_BYTES:
        reasons.append("too_large")
    return tuple(reasons)


def skip_dirs_report() -> Dict[str, Any]:
    """Provenance for :data:`SKIP_DIRS`, for a trace row or a handoff.

    Publishes the authority's own identity so a reader can tell which set a run
    used without re-deriving it from a walk, plus the ADOPTION state: how many
    modules still declare a table of their own.
    """
    tables = skip_dir_tables()
    return {
        "authority": SOURCE_OF_TRUTH,
        "dir_count": len(SKIP_DIRS),
        "glob_count": len(SKIP_FILE_GLOBS),
        "dirs": sorted(SKIP_DIRS),
        "globs": sorted(SKIP_FILE_GLOBS),
        "file_pattern": SKIP_FILE_PAT.pattern,
        "large_file_bytes": LARGE_FILE_BYTES,
        "independent_tables": tables["independent_table_count"],
        "modules_with_their_own_table": tables["own_tables"],
        "adopters": tables["adopters"],
    }


def ripgrep_glob_args() -> Tuple[str, ...]:
    """The same set expressed as ``rg --glob`` arguments.

    The Python fallback and ripgrep must prune **identically** or a result is
    only incidentally reproducible across the two engines. ``rg`` already
    ignores VCS files through its own defaults; ours are passed explicitly
    rather than inheriting a version-dependent default set, because an engine
    that silently prunes MORE than the fallback is the same drift this module
    exists to kill.

    The trailing ``/**`` is load-bearing: ``!**/name/`` does not exclude the
    directory's CONTENTS in ripgrep, so it would prune less than the fallback.
    """
    args: List[str] = []
    for name in sorted(SKIP_DIRS) + sorted(SKIP_FILE_GLOBS):
        args.extend(["--glob", f"!**/{name}/**"])
    return tuple(args)


# --------------------------------------------------------------------------
# THE ADOPTION AUDIT - how many tables exist, and who has not adopted
# --------------------------------------------------------------------------

#: Every module that is known to carry, or ought to carry, a skip set. The
#: audit is a fixed list rather than a filesystem glob on purpose: a glob would
#: silently absorb a new module and quietly change what the check proves.
CANDIDATE_MODULES: Tuple[Tuple[str, str], ...] = (
    ("shared/skipset.py", "SKIP_DIRS"),
    ("harness/skipset.py", "SKIP_DIRS"),
    ("harness/retrieval.py", "_SKIP_DIRS"),
    ("harness/agent_loop.py", "_SKIP_DIRS"),
    ("harness/scan_mode.py", "_SKIP_DIRS"),
    ("execution/walk_scope.py", "SKIP_DIR_NAMES"),
    ("memory/code_graph.py", "SKIP_DIR_NAMES"),
    ("runtime/difficulty.py", "_SKIP_DIRS"),
    ("runtime/subagents.py", "_SKIP_DIRS"),
    ("runtime/analyze_history.py", "_SKIP_DIRS"),
    ("cli/fileview.py", "_SKIP_DIRS"),
    ("dashboard/collect.py", "_SKIP_DIRS"),
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _classify(path: Path, attr: str) -> Tuple[str, Optional[List[str]]]:
    """Classify one module's skip-set attribute.

    Returns ``(kind, names)`` where ``kind`` is:

    * ``"table"`` -- an independent literal table, and ``names`` holds it;
    * ``"adopter"`` -- bound to another object (``= skipset.SKIP_DIRS``);
    * ``"absent"`` -- no such attribute at module scope.

    The three are kept apart because conflating them is the bug. An adopter read
    as a table is a table with no entries, and a table with no entries looks
    like a defect rather than an adoption -- so an audit that cannot tell them
    apart reports a healthy tree as broken and a broken tree as healthy.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ("absent", None)
    for node in tree.body:
        targets: Sequence[ast.AST] = ()
        value: Optional[ast.AST] = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is None:
            continue
        if not any(isinstance(t, ast.Name) and t.id == attr for t in targets):
            continue
        if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            names = _str_members(value)
            return ("table", names) if names is not None else ("derived_table", None)
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in ("frozenset", "set", "tuple", "list")
            and value.args
        ):
            # `frozenset({...})` is a table written through a constructor,
            # and `frozenset(genexp)` is a table built from local data.
            inner = _str_members(value.args[0])
            if inner is not None:
                return ("table", inner)
                # `set(skip_dirs_for(...))` is a WRAPPER around the authority,
                # not a second table. Detected by name because the argument is
                # an expression this audit deliberately does not evaluate --
                # counting it as a table would put the number back up for no
                # reason, and an audit whose count drifts when nothing changed
                # is an audit people stop reading.
                try:
                    snippet = ast.dump(value)
                except Exception:  # pragma: no cover - ast.dump is total
                    snippet = ""
                if "skip_dirs_for" in snippet or "SKIP_DIRS" in snippet:
                    return ("adopter", None)
                return ("derived_table", None)
        # A comprehension builds its own table from local data. The members are
        # not statically knowable, so it is counted as a TABLE and not as an
        # adoption -- misreading a comprehension as a reference would make the
        # one module that genuinely owns a table look like a module that merely
        # points at one.
        if isinstance(value, (ast.SetComp, ast.ListComp, ast.GeneratorExp)):
            return ("derived_table", None)
        return ("adopter", None)  # e.g. `= skipset.SKIP_DIRS`
    return ("absent", None)


def _str_members(node: ast.AST) -> Optional[List[str]]:
    """The string members of a literal collection node, or ``None``.

    ``None`` means "not a collection of plain strings". It is deliberately NOT
    an empty list: an empty list is a table with no members, which is a
    different and much more suspicious claim.
    """
    if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return None
    out: List[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(
            element.value, str
        ):
            return None
        out.append(element.value)
    return sorted(out)


def skip_dir_tables(
    root: Optional[Path] = None, *, authority: FrozenSet[str] = SKIP_DIRS
) -> Dict[str, Any]:
    """Audit how many independent skip-set tables this repository declares.

    Counts TABLES, not values. Several tables that happen to hold the same names
    today will diverge the first time one of them gains an entry, and nothing in
    the tree notices -- that is precisely the drift ``DOCTRINE.md`` §8 measured
    at 345x. Two tables that DISAGREE today are worse: one of them is already
    walking a directory tree the other prunes.
    """
    base = Path(root or _REPO_ROOT)
    own: Dict[str, List[str]] = {}
    derived: List[str] = []
    adopters: List[str] = []
    absent: List[str] = []
    for rel, attr in CANDIDATE_MODULES:
        path = base / rel
        if not path.is_file():
            absent.append(rel)
            continue
        kind, names = _classify(path, attr)
        if kind == "table" and names is not None:
            own[rel] = names
        elif kind == "derived_table":
            derived.append(rel)
        elif kind == "adopter":
            adopters.append(rel)
        else:
            absent.append(rel)
    # The authority's own comprehension-built table is not a SECOND authority.
    authority_rel = SOURCE_OF_TRUTH
    derived = sorted(rel for rel in derived if rel != authority_rel)
    missing = sorted(
        rel
        for rel, names in own.items()
        if rel != authority_rel and not set(names) >= set(authority)
    )
    extra = sorted(
        rel
        for rel, names in own.items()
        if rel != authority_rel and set(names) - set(authority)
    )
    return {
        "authority": SOURCE_OF_TRUTH,
        "authority_dir_count": len(authority),
        # The authority itself counts as one; every OTHER table is a second
        # authority, whether it is a literal or built by a comprehension.
        "independent_table_count": len(own) + len(derived) + (0 if authority_rel in own else 1),
        "own_tables": {rel: len(names) for rel, names in sorted(own.items())},
        "derived_tables": derived,
        "adopters": sorted(adopters),
        "modules_without_a_skip_set": sorted(absent),
        # A table that omits a name the authority carries is not merely a
        # second table, it is the SPECIFIC defect: that walk opens a directory
        # tree the authority says to prune.
        "tables_missing_authority_entries": missing,
        # A table carrying a name the authority does NOT have is the opposite
        # error, and an equally real one: it prunes something the authority
        # considers source.
        "tables_with_extra_entries": extra,
        "single_authority": not derived and len(own) <= 1,
    }
