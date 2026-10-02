"""The ONE skip-set authority for repository walks inside ``harness/``.

Why this module exists
----------------------
``phases/DOCTRINE.md`` §8 records the defect this file closes:

    ``_SKIP_DIRS`` drift: one retrieval path opened 124,774 directories in
    144.6 s where another opened 155 in 0.42 s. **The same walk. 345x.**

There were **three** ``_SKIP_DIRS`` literals inside ``harness/``
(``retrieval.py``, ``agent_loop.py``, ``scan_mode.py``) plus a fourth in
``memory/code_graph.py`` and more outside ``harness/`` entirely. A walk's cost
is decided entirely by this one set, so a set that lives next to the walk it
governs is a set that drifts — the module that happens to forget one entry pays
the whole bill, and nothing in the tree notices.

The set is therefore **not local to any walk**. It lives here, every walk
imports it, and :func:`skip_dirs_for` is the only way to obtain a variant.

What changed semantically (stated, not buried)
----------------------------------------------
Unifying on the union **does** change which files some walks can find: a walk
that previously descended into ``logs/``, ``site-packages/``, ``Temp/`` or
``graphify-out/`` no longer does. That is the intended direction and it is not
purely a performance decision — every one of those trees is either another
tool's output, a third-party vendored package, or a scratch directory, and a
code agent that greps its own run logs will report its own history back to
itself as though it were source. ``harness/scan_mode.py`` already documented
this reasoning ("cloned fixture repos under logs/ are third-party code, not
the project's"); this module makes it the one rule.

A caller that genuinely needs a different set must say so through
:func:`skip_dirs_for`, which is *auditable* (it appears in a trace receipt) —
which is the whole difference between this and a private literal.

Public surface
--------------
- :data:`SKIP_DIRS` — the authority. Every walk uses this by default.
- :data:`SKIP_FILE_PAT` / :data:`LARGE_FILE_BYTES` — the per-file sibling rules.
- :func:`skip_dirs_for` / :func:`skip_file_reasons` — the two audited accessors.
- :func:`skip_dirs_report` — provenance, for a trace row or a handoff.
- :func:`ripgrep_glob_args` — the same set expressed for ``rg --glob``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Iterable, Optional, Set, Tuple

__all__ = [
    "LARGE_FILE_BYTES",
    "SKIP_DIRS",
    "SKIP_FILE_PAT",
    "SOURCE_OF_TRUTH",
    "is_skipped_dir",
    "ripgrep_glob_args",
    "skip_dirs_for",
    "skip_dirs_report",
    "skip_file_reasons",
]

#: The authority. Provenance of every entry is in :func:`skip_dirs_report`.
_BASE: Tuple[str, ...] = (
    # -- version control + editor state -----------------------------------
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    ".idea",
    ".vscode",
    ".vs",
    # -- python environments and caches ------------------------------------
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
    "*.egg-info",  # see _EGG_INFO below - a glob, resolved explicitly
    "site-packages",
    "dist-info",
    # -- other-language build output --------------------------------------
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
    # -- tooling / audit output --------------------------------------------
    "probe_logs",
    ".opencode",
    ".qwen",
    ".playwright-mcp",
    ".shots",
    ".mypy_cache.d",
    "Temp",
    "tmp",
)

#: Entries that are globs rather than literal directory names. Kept apart so
#: the set-membership test stays a plain ``in`` (the hot path in every walk).
_GLOBS: Tuple[str, ...] = ("*.egg-info",)

#: Patterns applied to a FILE name. Same rule for every walk in ``harness/``.
SKIP_FILE_PAT = re.compile(
    r"(\.log$|\.pyc$|\.pyo$|\.lock$|package-lock\.json$|poetry\.lock$|\.pyd$|\.so$)"
)

#: Bytes. Larger a file is not opened by a discovery walk. The editor enforces
#: the same ceiling, so a file too big to edit is too big to search.
LARGE_FILE_BYTES = 200_000

#: Where this set came from, for the trace receipt and for the handoff.
SOURCE_OF_TRUTH = "harness/skipset.py"

SKIP_DIRS: FrozenSet[str] = frozenset(name for name in _BASE if name not in _GLOBS)
SKIP_FILE_GLOBS: FrozenSet[str] = frozenset(_GLOBS)


def is_skipped_dir(name: str) -> bool:
    """Whether a single directory name is pruned by the authority.

    The hot path of every walk, so it is a set lookup first and a glob only for
    the handful of glob entries.
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
    does not. Either way the caller must pass ``reason`` — it is recorded in
    :func:`skip_dirs_report`, so a walk that prunes differently is *visible* in
    the trace rather than being a second private literal that drifts.

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


def skip_file_reasons(name: str, size_bytes: Optional[int] = None) -> Tuple[str, ...]:
    """Return why a FILE is not a search candidate (empty tuple when it is).

    Split out so the per-file rules have one owner too, and so a receipt can
    name a reason rather than reporting a bare ``False``.
    """
    reasons: list[str] = []
    if SKIP_FILE_PAT.search(name):
        reasons.append("file_pattern")
    if size_bytes is not None and int(size_bytes) > LARGE_FILE_BYTES:
        reasons.append("too_large")
    return tuple(reasons)


def skip_dirs_report() -> Dict[str, Any]:
    """Provenance for :data:`SKIP_DIRS`, for a trace row or a handoff.

    Publishes the count and the authority's own identity so a reader can tell
    *which* set a run used without re-deriving it from a walk.
    """
    return {
        "authority": SOURCE_OF_TRUTH,
        "dir_count": len(SKIP_DIRS),
        "glob_count": len(SKIP_FILE_GLOBS),
        "dirs": sorted(SKIP_DIRS),
        "globs": sorted(SKIP_FILE_GLOBS),
        "file_pattern": SKIP_FILE_PAT.pattern,
        "large_file_bytes": LARGE_FILE_BYTES,
    }


def ripgrep_glob_args() -> Tuple[str, ...]:
    """The same set expressed as ``rg --glob`` arguments.

    The Python fallback and ripgrep must prune **identically** or a result is
    only incidentally reproducible across the two engines. ``rg`` already
    ignores VCS files via its own defaults; we pass ours explicitly rather than
    inheriting a version-dependent default set, because an engine that silently
    prunes MORE than the fallback is the same drift this module exists to kill.
    """
    args: list[str] = []
    for name in sorted(SKIP_DIRS) + sorted(SKIP_FILE_GLOBS):
        # The trailing ``/**`` is load-bearing: ``!**/name/`` does not exclude
        # the directory's CONTENTS in ripgrep, so it would prune less than the
        # Python fallback and reintroduce the very drift this module removes.
        args.extend(["--glob", f"!**/{name}/**"])
    return tuple(args)
