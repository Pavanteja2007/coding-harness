"""The ONE directory-walk skip authority for `execution/`, and a budgeted walker.

## Why this module exists

`phases/DOCTRINE.md` §8 closes with the defect this module exists to make
impossible to repeat: *"`_SKIP_DIRS` drift: one retrieval path opened 124,774
directories in 144.6 s where another opened 155 in 0.42 s. **The same walk.
345x.**"*

Three separate skip tables existed for the same job at the start of P1/W1:

| authority | entries | dirs opened on this repo | scandir seconds |
|---|---:|---:|---:|
| `memory/code_graph.py::SKIP_DIR_NAMES` | 20 | **171** | **0.017 s** |
| `harness/retrieval.py::_SKIP_DIRS` | 14 | **145,552** | 54.75 s |
| `execution/test_selection.py::_SKIP_DIRS` | 14 | **145,552** | 43.70 s |

Measured 2026-10-02 on this tree (Python 3.10.11, win32), by wrapping
`os.scandir` at the `os` module level and counting directory opens. **851x on
directory opens** for the same walk over the same tree. The second cost of
having three tables is not the 851x at all - it is that a fourth gets written,
and this repo has already paid for that once.

**Directory-op counts are the number to compare, not seconds.** Seconds inside
one process are confounded by page-cache warming (the same bare walk measured
14.1 s cold and 0.4 s warm), so they are reported as supporting evidence and the
op count carries the claim. The brief's own acceptance bar is written in
entries - "no walk on the per-call verification path exceeds 1s or 1000
entries" - so entries is the unit the bar is in.

## Where the 145,552 directories are

Not a mystery and not a pathological filesystem: `logs/`. It is the harness's
own run-artifact tree, one subdirectory per run, each holding a `pristine/` and
a `work/` snapshot copy of the repository under repair. The three skipping
authorities differ on exactly two names, and those two names are the whole
difference: this set has `logs`, `site`, `env` and `docs`; the other two do
not. A repository that commits a real `logs/` directory of its own is
correctly copied by `execution/snapshot.py` - that module deliberately does NOT
consult this table (see `tests/test_ceiling_r2_10_snapshot_disk.py::test_the_ignore_matcher_is_git_and_not_a_second_implementation`,
which forbids a skip table in `snapshot.py`). **This authority is for
CODE walks only.** A copy that silently skipped real committed content would be
a data-loss bug; a code walk that spends 43 s in `logs/` is only slow.

## The rule, stated once

> A directory whose name is in :data:`SKIP_DIR_NAMES`, or which begins with a
> dot, is never a place this module looks for source.

Both halves are in the rule because the fast measured authority uses both. The
dot rule is a parameter (:func:`iter_source_entries`'s ``skip_dot_dirs``) and is
REPORTED in the receipt, so a caller that turns it off cannot have its choice
silently erased by a reader who assumed the shipped default.

## Why the walker is here and not in each caller

`os.walk` materialises a subtree's entries before the caller can prune it, so a
caller that prunes *after* the walk has already paid for the subtree.
:func:`iter_source_entries` is an explicit stack over `os.scandir` that decides
on a directory name BEFORE descending, so a pruned subtree is never
enumerated at all. It also carries a **deadline and a file cap**, so a walk that
is cut short reports `not_searched` instead of presenting a partial scan as a
whole repository - the honesty rule from `phases/DOCTRINE.md` §1, applied to a
filesystem walk rather than to a receipt.

Every result carries :class:`WalkResult`, whose ``complete`` property is the
answer to "did this walk see everything?". Nothing in this module reports a
bounded scan as an unbounded one.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

__all__ = [
    "SKIP_DIR_NAMES",
    "SOURCE_SUFFIXES",
    "TRUNCATION_BUDGET",
    "TRUNCATION_CAP",
    "TRUNCATION_COMPLETE",
    "TRUNCATION_ERROR",
    "WalkResult",
    "iter_source_entries",
    "missing_from",
    "skip_dir",
    "walk_python_files",
]

#: THE skip authority for `execution/` code walks.
#:
#: Populated 2026-10-02 from the union of the three tables that existed, and
#: then RE-CONVERGED the same day: T1 widened ``harness/retrieval._SKIP_DIRS``
#: from 14 to 45 entries mid-session (adding exactly the generated trees this
#: repository is full of - ``Temp``, ``graphify-out``, ``probe_logs``, ``out``,
#: ``target``, ``vendor``), and this table was re-read to include them rather
#: than to race them. That is the whole point of one authority: two modules
#: reaching the same answer by adopting each other's fix.
#:
#: Every name is grouped by why it is here, so a reader can audit the reasoning
#: and not just the list. The union is the safe direction: every entry was
#: already skipped by at least one table, so adopting it can only remove
#: directories an existing walk already removed.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        # -- version control and its working metadata ------------------------
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "_darcs",
        ".neo",  # this project's own run state, inside the repo it repairs
        # -- python environments, caches and build residue ------------------
        ".venv",
        "venv",
        "virtualenv",
        "env",
        ".env",
        ".eggs",
        "site-packages",
        "dist-info",
        "__pycache__",
        ".mypy_cache",
        ".mypy_cache.d",
        ".pytest_cache",
        ".ruff_cache",
        ".nox",
        ".tox",
        # -- javascript / frontend toolchains --------------------------------
        "node_modules",
        "bower_components",
        ".next",
        ".nuxt",
        ".parcel-cache",
        # -- other ecosystems' build output ----------------------------------
        "target",  # rust / java
        "out",
        "gradle",
        ".gradle",
        ".terraform",
        ".vs",
        "vendor",
        # -- editors and agent scratch --------------------------------------
        ".idea",
        ".vscode",
        ".harness",
        ".opencode",
        ".qwen",
        ".playwright-mcp",
        ".shots",
        # -- build and documentation OUTPUT, never source --------------------
        "build",
        "dist",
        "docs",
        "site",
        "tmp",
        "Temp",
        # -- THE 851x. `logs/` is the harness's own run-artifact tree: one
        # subdirectory per run, each holding a `pristine/` and a `work/` COPY
        # of the repository. Walking it to find source walks a copy of the
        # answer. Before this table, `execution/test_selection.py` opened
        # 149,559 directories on this repository and took 495.3 s to build an
        # import graph in which 39,599 "modules" were discovered and only 569
        # were real source files.
        "logs",
        "probe_logs",
        "graphify-out",
    }
)

#: Suffixes :func:`walk_python_files` collects. Named, not inferred, so a
#: caller's receipt says which extension set produced its numbers.
SOURCE_SUFFIXES: Tuple[str, ...] = (".py",)

#: Closed truncation vocabulary. ``TRUNCATION_COMPLETE`` is the ONLY value that
#: may be presented as a whole repository; the other three are refusals to
#: pretend, which is the same discipline ``execution/snapshot.py`` applies to
#: its own ``plan_incomplete`` reason.
TRUNCATION_COMPLETE = "complete"
TRUNCATION_BUDGET = "budget"
TRUNCATION_CAP = "cap"
TRUNCATION_ERROR = "error"
TRUNCATION_VALUES: Tuple[str, ...] = (
    TRUNCATION_COMPLETE,
    TRUNCATION_BUDGET,
    TRUNCATION_CAP,
    TRUNCATION_ERROR,
)

#: The acceptance bar from the P1/W1 brief, quoted into the code so the number
#: and the threshold cannot drift apart. A per-call verification walk that
#: exceeds either of these is a defect to remove or cap, not a cost to absorb.
WALK_ENTRY_BAR = 1_000
WALK_SECONDS_BAR = 1.0


@dataclass
class WalkResult:
    """What one walk did, and - the part that matters - what it did not do.

    ``files`` is the answer. ``dirs_opened`` and ``seconds`` are the cost.
    ``not_searched`` plus ``truncation`` are the honesty, and they are what
    stops a bounded walk from reading as a whole repository.
    """

    root: str
    files: List[Path] = field(default_factory=list)
    dirs_opened: int = 0
    files_seen: int = 0
    not_searched: int = 0
    seconds: float = 0.0
    truncation: str = TRUNCATION_COMPLETE
    skip_dot_dirs: bool = True
    error: str = ""

    @property
    def complete(self) -> bool:
        """Return whether the walk saw the whole tree.

        ``False`` means this is a PARTIAL view, whatever ``files`` contains. A
        caller that reports a selection derived from it as a complete one is
        making a claim the receipt contradicts.
        """
        return self.truncation == TRUNCATION_COMPLETE

    @property
    def within_bar(self) -> bool:
        """Return whether this walk met the P1/W1 per-call bar."""
        return self.dirs_opened <= WALK_ENTRY_BAR and self.seconds <= WALK_SECONDS_BAR

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "root": self.root,
            "files": len(self.files),
            "dirs_opened": int(self.dirs_opened),
            "files_seen": int(self.files_seen),
            "not_searched": int(self.not_searched),
            "seconds": round(float(self.seconds), 4),
            "truncation": self.truncation,
            "complete": self.complete,
            "skip_dot_dirs": bool(self.skip_dot_dirs),
            "within_bar": self.within_bar,
            "bar": {"entries": WALK_ENTRY_BAR, "seconds": WALK_SECONDS_BAR},
            "error": self.error,
        }


def skip_dir(name: str, *, skip_dot_dirs: bool = True) -> bool:
    """Return whether a directory with this name is never a place to look.

    ``skip_dot_dirs`` covers the general case the named table cannot: there are
    unbounded ``.foo`` directories and none of them is source. It is a
    parameter rather than a fixed half of the rule so a caller that needs a dot
    tree can say so, and so the choice is reportable in the receipt.
    """
    text = str(name or "")
    if not text:
        return True
    if text in SKIP_DIR_NAMES:
        return True
    return bool(skip_dot_dirs and text.startswith("."))


def missing_from(skip: Any) -> Tuple[str, ...]:
    """Return the names in ``skip`` that this authority does NOT contain.

    This is the drift detector. Give it a foreign module's table and it answers
    "what would that walk open that this one would not", which is the question a
    third skip-list needs asked of it. Used by the pin in this module's test
    suite; public because a future owner of another module's walk needs the same
    answer about the same table.
    """
    return tuple(
        sorted(str(name) for name in (skip or ()) if str(name) not in SKIP_DIR_NAMES)
    )


def iter_source_entries(
    root: str,
    *,
    suffixes: Optional[Sequence[str]] = None,
    deadline_s: Optional[float] = None,
    max_files: int = 0,
    skip_dot_dirs: bool = True,
) -> WalkResult:
    """Walk ``root`` for source files, pruning skipped subtrees BEFORE descent.

    Returns a :class:`WalkResult`, never a bare list, because a caller that only
    receives the file list cannot tell a complete walk from a bounded one - and
    that difference is the whole honesty problem this module closes.

    The deadline is absolute ``time.monotonic()`` and is checked once per
    directory plus every 64 entries, so the overshoot is bounded by one
    directory's size rather than by the tree. ``max_files`` (0 = unbounded)
    bounds the collected set and reports what it did not collect. An unreadable
    directory is SKIPPED and counted in ``not_searched`` - never silently
    dropped, because "we could not read it" and "there was nothing there" are
    different answers.

    Assumes ``root`` is an existing directory; anything else is a typed error
    value on the result, never an exception, because this runs on a
    user-supplied path.
    """
    result = WalkResult(root=str(root or ""), skip_dot_dirs=bool(skip_dot_dirs))
    started = time.perf_counter()
    text_root = str(root or "").strip()
    if not text_root:
        result.truncation = TRUNCATION_ERROR
        result.error = "no root was supplied"
        result.seconds = time.perf_counter() - started
        return result
    try:
        base = Path(text_root).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        result.truncation = TRUNCATION_ERROR
        result.error = f"the root could not be resolved: {type(exc).__name__}"
        result.seconds = time.perf_counter() - started
        return result
    if not base.is_dir():
        result.truncation = TRUNCATION_ERROR
        result.error = "the root is not a directory"
        result.seconds = time.perf_counter() - started
        return result

    wanted = tuple(suffixes) if suffixes else ()
    cap = max(0, int(max_files or 0))
    stack: List[str] = [str(base)]
    entries: List[os.DirEntry] = []
    unreadable = 0

    while stack:
        if deadline_s is not None and time.monotonic() >= deadline_s:
            result.truncation = TRUNCATION_BUDGET
            result.not_searched = len(stack) + unreadable
            break
        directory = stack.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = list(scanner)
        except (OSError, ValueError):
            unreadable += 1
            continue
        result.dirs_opened += 1
        for position, entry in enumerate(entries):
            if cap and len(result.files) >= cap:
                result.truncation = TRUNCATION_CAP
                result.not_searched = (
                    len(stack) + (len(entries) - position) + unreadable
                )
                break
            if (
                deadline_s is not None
                and not (position & 63)
                and time.monotonic() >= deadline_s
            ):
                result.truncation = TRUNCATION_BUDGET
                result.not_searched = (
                    len(stack) + (len(entries) - position) + unreadable
                )
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            if is_dir:
                # The decision is made on the NAME, before the subtree is
                # touched. This is the line the 851x is about.
                if skip_dir(entry.name, skip_dot_dirs=skip_dot_dirs):
                    continue
                stack.append(entry.path)
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                unreadable += 1
                continue
            result.files_seen += 1
            if wanted and not entry.name.endswith(wanted):
                continue
            result.files.append(Path(entry.path))
        if result.truncation != TRUNCATION_COMPLETE:
            break

    result.not_searched += unreadable if result.truncation == TRUNCATION_COMPLETE else 0
    result.seconds = time.perf_counter() - started
    return result


def walk_python_files(
    root: str,
    *,
    deadline_s: Optional[float] = None,
    max_files: int = 0,
    skip_dot_dirs: bool = True,
) -> WalkResult:
    """Walk ``root`` for ``.py`` files. The shape :mod:`execution.test_selection` uses."""
    return iter_source_entries(
        root,
        suffixes=SOURCE_SUFFIXES,
        deadline_s=deadline_s,
        max_files=max_files,
        skip_dot_dirs=skip_dot_dirs,
    )


def iter_python_paths(root: str) -> Iterator[Path]:
    """Yield ``.py`` paths under ``root`` with no bound, for callers that build their own.

    Kept separate from :func:`iter_source_entries` so the bounded form stays the
    one a receipt can be built from. A caller reaching for THIS is opting out of
    the receipt and should say why in a comment.
    """
    for candidate in walk_python_files(root).files:
        yield candidate
