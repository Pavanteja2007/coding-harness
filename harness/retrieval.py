"""Context retrieval for the harness — two layers, one API.

Layer 1 (dumb grep, Phase 1): extract terms from the issue text, score
files by term hits in path + content. Always available; works on any
text-shaped repo.

Layer 2 (structural, Phase 2 — this module's headline upgrade): when the
repo is Python and Terminal 4's memory.code_graph is importable, the
issue is located through the repo's ACTUAL structure — imports and call
edges, not keyword proximity:

  1. ANCHOR: the target test (config["target_test"], a pytest node id)
     names the test that encodes the bug. Even when the issue text never
     mentions the relevant function/class, the TEST file does: its
     imports lead to the module under test, its calls lead to the
     buggy symbol.
  2. SYMBOL MATCH: issue terms are matched against the indexed symbol
     table (names, qualified names, docstrings) — "average is wrong"
     finds compute_monthly_average without a single grep hit.
  3. NEIGHBORHOOD: matched/anchored symbols are expanded along call
     edges (callers + callees) and import edges (the module's importers
     — for a bug, the test that fails IS an importer), so the fix
     surface and its direct dependents are surfaced together.

The two layers are merged into one ranking; structural hits outrank
plain-content hits because they carry the repo's real dependency
structure. Layer 2 is best-effort everywhere: any failure degrades to
Layer 1 with a note in the returned dict (never an exception — retrieval
must not kill a task run).

Bounded search (AGT-04): :func:`search_repo` is the ONE search a tool should
call. Its cap ERRORS rather than truncating — above ``SEARCH_MATCH_CAP`` matches
it returns "too many matches - narrow your query" with the count and a small
sample — and there is deliberately no page two: the paging-shaped argument names
are the closed set :data:`PAGING_ARGUMENTS`, audited against the tool catalog by
:func:`paging_affordances`. Measured rationale: offering paging scores WORSE than
offering no search at all, because a model pages exhaustively until the cap
stops it, and the narrowing the error was meant to teach never happens.

Spec items 1 (repo understanding) and 21 (structural code graph); the
graph itself is Terminal 4's memory.code_graph (tree-sitter), consumed
via harness.deps.get_code_graph_factory — one structural index for the
whole project instead of a harness-private duplicate.

Cheaper context (VEX-CEILING-09): retrieval results and the tree-sitter
graph are cached BY CONTENT DIGEST, never by path or mtime. A cached
retrieval is re-validated against the current digest of exactly the files
it cited before it is reused, so a cache hit can never serve a ranking
built from a source file that has since changed; a mismatch recomputes and
is reported as ``refreshed`` rather than served. Every result therefore
carries a ``cache_status`` receipt (``hit`` | ``miss`` | ``refreshed``)
plus the digests it was validated against.
"""

import fnmatch
import hashlib
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from . import search_engine, skipset

# Extensions considered "code" when ranking hits.
_CODE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp"}

# Files never offered as context (build artifacts, env, logs, binaries).
# These three names used to be a private literal in this module, and that
# WAS the 345x: the set was missing `logs`, which is the harness's own
# run-artifact tree, so a search walked a COPY of the repository once per
# historical run. Measured on this tree with this module's own walk, one
# pass each, no other change:
#
#   this module's old literal    147,241 directories   71.64 s
#   the same literal + `logs`        271 directories    0.08 s
#
# ONE missing name was 99.7% of the cost. The set is now
# :mod:`harness.skipset`, shared by every walk in `harness/`, because a
# walk's entire cost is decided by this one set, and a set that lives next
# to the walk it governs is a set that drifts. These three names stay so
# the in-module read sites and any external reader keep working unchanged.
_SKIP_DIRS = skipset.SKIP_DIRS
_SKIP_FILE_PAT = skipset.SKIP_FILE_PAT
_LARGE_FILE = skipset.LARGE_FILE_BYTES

# Bound stat-mode predicates, aliased once: the walk below calls them per
# entry, and a module-level attribute lookup is a measurable saving when the
# walk visits hundreds of thousands of entries.
_IS_DIR = stat.S_ISDIR
_IS_REG = stat.S_ISREG
_IS_LINK = stat.S_ISLNK

# Words too generic to be worth grepping for.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "when",
    "with",
    "without",
    "not",
    "no",
    "yes",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "should",
    "would",
    "could",
    "can",
    "will",
    "must",
    "may",
    "might",
    "shall",
    "from",
    "by",
    "as",
    "so",
    "than",
    "too",
    "very",
    "just",
    "about",
    "into",
    "over",
    "after",
    "before",
    "while",
    "during",
    "between",
    "under",
    "above",
    "out",
    "off",
    "up",
    "down",
    "again",
    "further",
    "once",
    "here",
    "there",
    "all",
    "any",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "only",
    "own",
    "same",
    "s",
    "t",
    "don",
    "now",
    "bug",
    "issue",
    "error",
    "fix",
    "please",
    "fails",
    "failed",
    "fail",
    "test",
    "tests",
    "testing",
    "returns",
    "return",
    "raise",
    "raises",
    "expected",
    "actual",
    "function",
    "method",
    "class",
    "module",
    "file",
    "line",
    "code",
    "python",
    "python3",
    "version",
    "traceback",
    "exception",
    "stack",
    "trace",
}


def _path_safety_for(root: Path):
    """Return a hoisted symlink-classification index for ``root``, or None.

    R2-09: the per-file symlink question used to be an O(depth) ``is_symlink``
    walk plus a ``resolve()`` for EVERY candidate file, measured at 4.97 ms per
    file on a 289,584-file checkout (105 us for one by-path ``os.lstat`` there,
    against 0.18 us for the same information off an ``os.scandir`` entry). The
    index is built once per walk and answers the same question by set lookup.

    Returns ``None`` when ``memory.code_graph`` cannot be imported (an optional
    dependency of this module, per ``harness.deps``); the caller then falls
    back to the per-file check, which is slower but exactly as correct.
    """
    try:
        from memory.code_graph import PathSafety
    except Exception:
        return None
    try:
        return PathSafety.for_root(root)
    except Exception:
        return None


def _walk_code_files(repo_path: str, *, safety: Any = None) -> List[Path]:
    """All candidate files in the repo, excluding unsafe paths and huge files.

    One ``os.scandir`` walk, pruning ``_SKIP_DIRS`` and symlinked directories
    FROM the walk so their subtrees are never enumerated. Each entry is stat-ed
    once through its dirent (never by path), and its safety is answered by the
    hoisted ``safety`` index (see :func:`_path_safety_for`) instead of a
    per-component ``is_symlink`` walk.

    The candidate set is deliberately broad — any text-shaped file, not only
    code — because an issue is as often about a config, a fixture, or a doc.
    Assumes ``repo_path`` is an existing directory. Never raises: an
    unreadable directory is skipped.

    This walk is the retrieval path's dominant cost on a large checkout, and it
    is dominated by DIRECTORY opens: measured on this repository (289,584
    files), the same walk with the code-graph skip set opens 155 directories in
    0.42 s, while this module's ``_SKIP_DIRS`` leaves 124,774 directories and
    takes 144.6 s — 1.16 ms per ``os.scandir`` open on this host. Widening the
    skip set is therefore the single largest retrieval win available and is
    deliberately NOT done here (it changes which files can be retrieved, which
    is a retrieval-quality decision, not a performance one); it is filed as a
    cross-terminal request. :func:`walk_code_files_budgeted` is the way to
    bound it without changing the set.
    """
    return walk_code_files_budgeted(repo_path, safety=safety)[0]


def walk_code_files_budgeted(
    repo_path: str,
    *,
    safety: Any = None,
    deadline_s: Optional[float] = None,
    max_files: int = 0,
    start: Optional[str] = None,
) -> Tuple[List[Path], int]:
    """Return ``(candidates, not_searched)`` for the code-file walk.

    ``deadline_s`` is an absolute ``time.monotonic()`` value and ``max_files``
    bounds the collected set (``0`` = unbounded). When either cuts the walk
    short, the second return value is the number of entries the walk did not
    look at, so the caller can say so instead of presenting a partial scan as a
    whole repository. With both omitted this is byte-identical to the
    historical candidate set, which is why :func:`_walk_code_files` is a thin
    wrapper over it.

    ``start`` narrows where the walk BEGINS (a ``path=``-scoped search) while
    the returned paths stay relative to ``repo_path`` and containment is still
    judged against it. It is the reason this walk is the ONE walk: the search
    path used to carry a second, un-hoisted ``os.scandir`` implementation whose
    per-file cost was 4.97 ms, and a second walk for the same job is a second
    answer to a question the tree already answers once.

    The deadline is checked per directory and every 64 files, so the overshoot
    is bounded by one directory's entries rather than by the tree. Never
    raises.
    """
    if not str(repo_path or "").strip():
        return [], 0
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return [], 0
    if not root.is_dir():
        return [], 0
    index = safety if safety is not None else _path_safety_for(root)
    out: List[Path] = []
    root_str = str(root)
    prefix_len = len(root_str) + 1
    cap = max(0, int(max_files or 0))
    walk_from = root_str
    if str(start or "").strip():
        try:
            candidate = Path(str(start)).resolve()
        except (OSError, RuntimeError, ValueError):
            return [], 0
        # A `start` outside the repository, or one that is a file rather than a
        # directory, is a refusal rather than a silent walk of the whole repo:
        # a search scoped to `../elsewhere` must not quietly answer about `.`.
        # The containment test appends the separator on purpose - a bare
        # `startswith` would accept a sibling directory whose name merely
        # begins with the root's, which is the same bug as `C:\repo2`.
        candidate_str = str(candidate)
        if not (
            candidate_str == root_str or candidate_str.startswith(root_str + os.sep)
        ):
            return [], 0
        if candidate == root:
            walk_from = root_str
        elif candidate.is_file():
            walk_from = str(candidate.parent)
        elif candidate.is_dir():
            walk_from = str(candidate)
        else:
            return [], 0
    stack: List[str] = [walk_from]
    visited = 0
    while stack:
        if deadline_s is not None and time.monotonic() >= deadline_s:
            return out, len(stack) + visited
        directory = stack.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = list(scanner)
        except (OSError, ValueError):
            continue
        for position, entry in enumerate(entries):
            if cap and len(out) >= cap:
                return out, len(stack) + (len(entries) - position) + visited
            if (
                deadline_s is not None
                and not (position & 63)
                and time.monotonic() >= deadline_s
            ):
                return out, len(stack) + (len(entries) - position) + visited
            name = entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                continue
            mode = info.st_mode
            path = entry.path
            if _IS_LINK(mode):
                if index is not None:
                    index.observe(path, is_symlink=True)
                continue
            if _IS_DIR(mode):
                if name in _SKIP_DIRS:
                    continue
                if index is not None:
                    index.observe(path, is_symlink=False)
                stack.append(path)
                continue
            if not _IS_REG(mode):
                continue
            if _SKIP_FILE_PAT.search(name):
                continue
            if info.st_size > _LARGE_FILE:
                continue
            if index is not None:
                if index.has_symlink_component(path):
                    continue
            else:
                # No hoisted index available: the per-file check, unchanged.
                try:
                    if (
                        path.startswith(root_str)
                        and _safe_source_path(
                            root_str, path[prefix_len:].replace("\\", "/")
                        )
                        is None
                    ):
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue
            out.append(Path(path))
            visited += 1
    return out, 0


def extract_terms(issue_text: str, extra_stop: Optional[set] = None) -> List[str]:
    """Extract likely file/symbol names from issue text.

    Assumes issue_text is a human-written bug report. Pulls (a) explicit
    file-ish tokens (contain a '/' or a '.' or snake/camel-case code
    identifiers), (b) code-extension file names, and (c) any non-stopword
    word of length >= 3 (short symbols like "pop" matter a lot). Returns a
    de-duplicated, order-preserving list.
    """
    stop = _STOPWORDS | (extra_stop or set())
    tokens: List[str] = []
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_./\-]*", issue_text or "")
    for tok in raw:
        low = tok.lower().strip("./-")
        if not low or low in stop or len(low) < 3:
            continue
        if tok in tokens:
            continue
        tokens.append(tok)
    return tokens


def _score_candidate_files(
    files: Sequence[Path],
    root: Path,
    terms: List[str],
    limit: int,
    *,
    deadline_s: Optional[float] = None,
) -> List[str]:
    """Score pre-collected candidate files by term overlap; return top ``limit``.

    Split out of :func:`rank_files` so a caller that already walked the tree
    (the budgeted path) does not pay for a second walk. Assumes ``files`` came
    from :func:`walk_code_files_budgeted` under the same ``root``. Stops at
    ``deadline_s`` and returns the ranking over the files it scored.
    """
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    scored: List[tuple] = []
    for position, f in enumerate(files):
        if (
            deadline_s is not None
            and not (position & 15)
            and time.monotonic() >= deadline_s
        ):
            break
        rel = f.relative_to(root).as_posix()
        rel_l = rel.lower()
        score = 0
        for tl in lows:
            if tl in rel_l or tl in f.stem.lower():
                score += 2
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            text = ""
        for tl in lows:
            c = text.count(tl)
            if c:
                score += min(c, 5)
        if score > 0:
            bonus = 1 if f.suffix in _CODE_EXTS else 0
            scored.append((score + bonus, -len(rel), rel))
    scored.sort(reverse=True)
    return [rel for _, _, rel in scored[:limit]]


def rank_files(
    repo_path: str,
    terms: List[str],
    limit: int = 4,
    *,
    deadline_s: Optional[float] = None,
) -> List[str]:
    """Return up to `limit` repo-relative file paths ranked by term overlap.

    Assumes `terms` came from extract_terms. Dumb grep-style scoring:
    - +2 per term appearing in the file's path (path or stem)
    - +min(count, 5) per term found in the file's CONTENT (the actual
      "grep for symbols mentioned in the issue" behavior)
    Ties broken by code-extension bonus then shorter path. Returns
    repo-relative posix paths.

    ``deadline_s`` (keyword-only, R2-09) bounds the scan: this function reads
    the FULL TEXT of every candidate file, which is why the grep layer is the
    retrieval path's dominant cost on a large checkout (measured: 144.6 s of
    directory opens plus a full-text read per candidate on a 289,584-file
    checkout). With no deadline the ranking is the historical whole-repository
    one.
    """
    if not str(repo_path or "").strip():
        return []
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    files, _unsearched = walk_code_files_budgeted(repo_path, deadline_s=deadline_s)
    return _score_candidate_files(files, root, terms, limit, deadline_s=deadline_s)


def search_file_for_lines(repo_path: str, rel_path: str, terms: List[str]) -> List[str]:
    """Grep one repo-relative file for matching lines without path escape."""
    try:
        root = Path(repo_path).resolve()
        relative = Path(str(rel_path).replace("\\", "/"))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or re.match(r"^[A-Za-z]:[\\/]", str(rel_path))
        ):
            return []
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return []
        path = current.resolve()
        path.relative_to(root)
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError, ValueError):
        return []
    hits = []
    lows = [t.lower() for t in terms if t]
    for i, line in enumerate(text.splitlines(), start=1):
        lline = line.lower()
        if any(t in lline for t in lows):
            hits.append(f"{rel_path}:{i}: {line.rstrip()}")
            if len(hits) >= 20:
                break
    return hits


# ---------------------------------------------------------------------------
# Bounded search (AGT-04): the cap is an ERROR, and there is no page two
# ---------------------------------------------------------------------------
#
# One grep must not be able to spend a whole context window, and it must not be
# able to be paged through. Both are deliberate:
#
# * **The cap errors.** Above ``SEARCH_MATCH_CAP`` matches the search returns
#   "too many matches - narrow your query", the true (or lower-bound) count,
#   and a small sample. Returning 5 000 matching lines costs 5 000 tokens and
#   teaches the model nothing it could not have narrowed for.
# * **There is deliberately no paging affordance.** Offering ``next``/``prev``
#   measures WORSE than offering no search at all, because a model pages
#   exhaustively: it walks the result set until the cap stops it, and the
#   narrowing the error was supposed to teach never happens. The names that
#   would constitute such an affordance are a CLOSED set
#   (:data:`PAGING_ARGUMENTS`) checked against this module's own signatures and
#   against the tool catalog, so one cannot reappear by accident.

#: Default ceiling on the matches ONE search may return. Above it the search
#: errors instead of returning a list.
SEARCH_MATCH_CAP = 50

#: Default number of example lines an over-cap search includes as a sample.
SEARCH_SAMPLE_CAP = 10

#: Default ceiling on the FILES one search may open, so a pathological pattern
#: cannot turn a single tool call into a whole-repository read.
SEARCH_FILE_SCAN_CAP = 5000

#: The model-facing refusal. Narrowing the query is the only way forward; the
#: exact wording is the point of the mechanism and is pinned by tests.
SEARCH_TOO_MANY_MESSAGE = "too many matches - narrow your query"

#: Error slug for an over-cap search. Stable, so callers match on it instead of
#: parsing prose.
ERROR_TOO_MANY_MATCHES = "too_many_matches"

#: Error slug for a paging argument. See :data:`PAGING_ARGUMENTS`.
ERROR_PAGING_REFUSED = "paging_refused"

#: Error slug for a pattern that is not a usable regular expression.
ERROR_BAD_PATTERN = "bad_pattern"

#: Error slug for a path that escapes the repository or does not exist.
ERROR_BAD_PATH = "bad_path"

#: The closed set of argument names that would constitute an iterative paging
#: affordance. Deliberately generous, and deliberately EXCLUDING range bounds
#: that are not paging: ``start_line``/``end_line`` select a line window of one
#: file (``git_blame``), which is a narrowing, not a page turn. A false positive
#: here costs one honest refusal; a false negative costs the whole context.
PAGING_ARGUMENTS: frozenset = frozenset(
    {
        "after",
        "after_cursor",
        "before",
        "continuation",
        "cursor",
        "from_index",
        "next",
        "next_cursor",
        "next_page",
        "next_page_token",
        "next_token",
        "offset",
        "page",
        "page_index",
        "page_number",
        "page_size",
        "page_token",
        "prev",
        "prev_page",
        "prev_page_token",
        "previous",
        "previous_page",
        "prev_token",
        "start",
        "start_at",
        "start_index",
        "start_offset",
        "token",
    }
)


def paging_arguments_in(names: object) -> Tuple[str, ...]:
    """Return every paging-shaped argument name in ``names``, sorted.

    Accepts any iterable of names or a mapping (its keys are used). This is the
    single definition of "a paging affordance", used by :func:`search_repo` to
    refuse one, by :func:`paging_affordances` to audit a catalog, and by the
    test suite to prove no such affordance exists.
    """
    if isinstance(names, Mapping):
        candidates = list(names.keys())
    elif isinstance(names, str):
        candidates = [names]
    else:
        try:
            candidates = list(names or ())
        except TypeError:
            candidates = []
    return tuple(
        sorted(
            {str(name) for name in candidates if str(name).lower() in PAGING_ARGUMENTS}
        )
    )


def paging_affordances(specs: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Audit tool specs (default: the canonical catalog) for paging arguments.

    Returns ``{"clean": bool, "offenders": [...], "checked": int}``. An entry is
    an offender when it DECLARES a paging argument - the affordance has to be
    offered to be used, so its absence from the schema is the property that
    matters. This is a real runtime guard: the daily strategy refuses to build
    a request whose catalog grew a page-two, rather than trusting that nobody
    adds one.
    """
    try:
        from harness.tools import typed_tool_specs as _specs
    except Exception:  # pragma: no cover - defensive
        _specs = None
    selected = (
        tuple(specs) if specs is not None else (_specs() if _specs is not None else ())
    )
    offenders: List[Dict[str, Any]] = []
    for spec in selected:
        declared = list(getattr(spec, "optional", ()) or ()) + list(
            getattr(spec, "required", ()) or ()
        )
        found = paging_arguments_in(declared)
        if found:
            offenders.append(
                {"tool": str(getattr(spec, "name", "")), "arguments": list(found)}
            )
    return {
        "clean": not offenders,
        "offenders": offenders,
        "checked": len(selected),
    }


@dataclass
class SearchOutcome:
    """One bounded search's answer, including the refusal that bounds it.

    ``ok`` is False for an over-cap search (:data:`ERROR_TOO_MANY_MATCHES`), a
    paging argument (:data:`ERROR_PAGING_REFUSED`), an unusable pattern
    (:data:`ERROR_BAD_PATTERN`) and an unusable path (:data:`ERROR_BAD_PATH`).
    An over-cap search still carries ``total``, ``file_count`` and ``sample`` -
    the model is told how big the answer was and shown a few lines, then told to
    narrow. It is never handed the set.

    ``total_is_lower_bound`` is True when the scan stopped at ``cap + 1``
    matches, so ``total`` is a floor and not an exact count. Likewise
    ``files_not_searched_known`` distinguishes "the file walk finished" from
    "the walk hit ``max_files`` and the remainder was never measured" - reading
    an absent count as 0 is the exact failure this module's own receipts exist
    to prevent.
    """

    ok: bool = True
    pattern: str = ""
    error: str = ""
    message: str = ""
    matches: List[str] = field(default_factory=list)
    sample: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    refused_arguments: List[str] = field(default_factory=list)
    total: int = 0
    total_is_lower_bound: bool = False
    file_count: int = 0
    cap: int = SEARCH_MATCH_CAP
    files_scanned: int = 0
    files_not_searched: Optional[int] = None
    files_not_searched_known: bool = True

    # --- the search's COST and its ENGINE (T1.W1.4 / T1.W1.1) --------------
    #
    # Every one of these is populated on EVERY path, including the refusals.
    # A receipt that omits its cost because the search was refused is a receipt
    # whose reader cannot tell a cheap refusal from an unmeasured one.
    engine: str = search_engine.ENGINE_FALLBACK
    engine_source: str = search_engine.SOURCE_UNAVAILABLE
    engine_reason: str = ""
    duration_s: float = 0.0
    #: The bound that stopped this search, named. ``""`` means the result is
    #: COMPLETE — the only value that may be presented as a whole answer.
    truncated_by: str = ""
    max_results_applied: Optional[int] = None

    @property
    def over_cap(self) -> bool:
        """Whether the cap refused this search (as opposed to it succeeding)."""
        return self.error == ERROR_TOO_MANY_MATCHES

    @property
    def truncated(self) -> bool:
        """Whether any bound cut this result short.

        Deliberately NOT ``not ok``: an over-cap search is a refusal *and* a
        truncated result, and a reader asking "can I treat this as the whole
        answer" wants the second question answered independently of the first.
        """
        return bool(self.truncated_by) or self.over_cap

    @property
    def complete(self) -> bool:
        """Whether this result may be presented as a whole answer."""
        return not self.truncated

    def cost(self) -> Dict[str, Any]:
        """The per-search cost receipt T4's live rail and Trust Ladder #9 read.

        Engine, files scanned, matches returned, duration, and the truncation
        status WITH THE BOUND NAMED. A truncated result that does not name its
        bound is a partial list that reads as complete, which is the specific
        dishonesty this receipt exists to prevent.
        """
        return {
            "engine": self.engine,
            "engine_source": self.engine_source,
            "files_scanned": int(self.files_scanned),
            "matches_returned": int(self.total),
            "sample_lines": len(self.sample),
            "duration_s": round(float(self.duration_s), 4),
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "complete": self.complete,
            "files_not_searched": self.files_not_searched,
            "files_not_searched_known": bool(self.files_not_searched_known),
            "cap": int(self.cap),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return the serializable receipt for a trace row."""
        row = {
            "ok": bool(self.ok),
            "pattern": self.pattern,
            "error": self.error,
            "message": self.message,
            "matches": list(self.matches),
            "sample": list(self.sample),
            "files": list(self.files),
            "refused_arguments": list(self.refused_arguments),
            "total": int(self.total),
            "total_is_lower_bound": bool(self.total_is_lower_bound),
            "file_count": int(self.file_count),
            "cap": int(self.cap),
            "files_scanned": int(self.files_scanned),
            "files_not_searched": self.files_not_searched,
            "files_not_searched_known": bool(self.files_not_searched_known),
        }
        # Flattened, not nested, so a consumer reading a trace row can find
        # the cost without knowing the shape of `cost()`. Additive only: no
        # existing key moved, renamed, or changed meaning.
        row.update(self.cost())
        row["max_results_applied"] = self.max_results_applied
        return row

    def render(self) -> str:
        """Return the model-facing text for this outcome.

        The over-cap rendering is the important one and is deliberately
        unmissable: the count, the sample, and the instruction to narrow, and
        an explicit statement that there is no page two (so a model does not go
        looking for one).

        A search that a bound cut short but that was still under the match cap
        says so too, in one line, naming the bound. A partial list that reads
        as a whole answer is the failure this line prevents.
        """
        if self.error == ERROR_TOO_MANY_MATCHES:
            count = (
                f"{self.total}+ matches"
                if self.total_is_lower_bound
                else f"{self.total} matches"
            )
            lines = [
                f"TOOL ERROR [{ERROR_TOO_MANY_MATCHES}]: {SEARCH_TOO_MANY_MESSAGE}.",
                f"{count} for {self.pattern!r} across {self.file_count} file(s); "
                f"showing {len(self.sample)} of them.",
            ]
            lines.extend(self.sample)
            lines.append(
                "Narrow the query: pass a `path`, a `glob`, or a more specific "
                "pattern. There is no paging through the result set."
            )
            return "\n".join(lines)
        if self.error:
            return f"TOOL ERROR [{self.error}]: {self.message}"
        body = "\n".join(self.matches) if self.matches else "(no matches)"
        if self.truncated_by:
            body = f"{body}\n[TRUNCATED: {self.truncated_by}] this is a partial list."
        return body


def _search_root(
    repo_path: str, relative: Optional[str]
) -> Tuple[Optional[Path], Path, str]:
    """Resolve a search root inside the repository, or explain why it is refused."""
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, Path(str(repo_path or ".")), ERROR_BAD_PATH
    if not root.is_dir():
        return None, root, ERROR_BAD_PATH
    if not relative:
        return root, root, ""
    candidate = _safe_source_path(str(root), str(relative))
    if candidate is None:
        # Either it escapes the repository, follows a symlink, or does not
        # exist. All three are one refusal: a search root is either a real file
        # we can read or nothing.
        return None, root, ERROR_BAD_PATH
    return (candidate.parent if candidate.is_file() else candidate), root, ""


def search_repo(
    repo_path: str,
    pattern: str,
    *,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    max_matches: int = SEARCH_MATCH_CAP,
    sample: int = SEARCH_SAMPLE_CAP,
    max_files: int = SEARCH_FILE_SCAN_CAP,
    max_results: Optional[int] = None,
    arguments: Optional[Mapping[str, Any]] = None,
) -> SearchOutcome:
    """Search the repository and either return a bounded set or refuse.

    Returns a :class:`SearchOutcome`. The search is bounded on three axes, all
    of which matter:

    * **matches** - the scan stops at ``max_matches + 1``, which is all it
      needs to know that the cap was exceeded. ``total`` is then a lower bound
      and says so.
    * **files** - the walk stops at ``max_files``, so a pathological pattern
      over a 300k-file checkout cannot become a repository read.
    * **bytes** - the rendered sample is a fixed number of short lines, so an
      over-cap search is a few hundred characters rather than a context event.

    ``max_results`` narrows a SUCCESSFUL result set further. It is clamped to
    ``max_matches`` and it cannot raise the error threshold, because a caller
    must not be able to buy its way past the cap by asking for more.

    ``arguments`` is the RAW argument mapping a model sent, when there is one.
    Any paging-shaped key in it is refused (:data:`ERROR_PAGING_REFUSED`) rather
    than honoured - the affordance is meant to be absent, and this is what makes
    its absence hold for a programmatic caller as well as for the catalog.
    Never raises.

    **The engine is part of the answer.** This prefers ripgrep
    (:func:`harness.search_engine.resolve_ripgrep`) and falls back to the Python
    walker, and every returned :class:`SearchOutcome` carries which one ran and
    why - in :meth:`SearchOutcome.cost`, in :meth:`SearchOutcome.to_dict`, and in
    the model-facing :meth:`SearchOutcome.render`. A silent fallback would make
    "fast because ripgrep ran" indistinguishable from "fast because there was
    nothing to search", which is the question a reader of a slow search needs
    answered most.
    """
    resolution = search_engine.resolve_ripgrep()
    return _search_repo_bounded(
        repo_path,
        pattern,
        path=path,
        glob=glob,
        max_matches=max_matches,
        sample=sample,
        max_files=max_files,
        max_results=max_results,
        arguments=arguments,
        engine=resolution,
    )


def _search_repo_bounded(
    repo_path: str,
    pattern: str,
    *,
    path: Optional[str],
    glob: Optional[str],
    max_matches: int,
    sample: int,
    max_files: int,
    max_results: Optional[int],
    arguments: Optional[Mapping[str, Any]],
    engine: Any,
) -> SearchOutcome:
    """The bounds and the refusals; the engine only chooses who reads the files.

    Split out from :func:`search_repo` so that the refusals (paging, a bad
    pattern, a bad path) are decided ONCE and identically on both engines. A
    refusal that only the Python path enforced would be a capability that
    vanished the moment someone installed ripgrep.
    """
    try:
        cap = max(1, int(max_matches or SEARCH_MATCH_CAP))
    except (TypeError, ValueError):
        cap = SEARCH_MATCH_CAP
    try:
        sample_cap = max(1, min(int(sample or SEARCH_SAMPLE_CAP), cap))
    except (TypeError, ValueError):
        sample_cap = SEARCH_SAMPLE_CAP
    try:
        file_cap = max(1, int(max_files or SEARCH_FILE_SCAN_CAP))
    except (TypeError, ValueError):
        file_cap = SEARCH_FILE_SCAN_CAP

    text_pattern = str(pattern or "")
    paging = paging_arguments_in(arguments)
    if paging:
        return SearchOutcome(
            ok=False,
            pattern=text_pattern,
            error=ERROR_PAGING_REFUSED,
            message=(
                f"paging is not an available search affordance: "
                f"{', '.join(paging)}. Narrow the query instead."
            ),
            refused_arguments=list(paging),
            cap=cap,
            engine=engine.engine,
            engine_source=engine.source,
            engine_reason=engine.reason,
        )
    try:
        matcher = re.compile(text_pattern, re.IGNORECASE)
    except (re.error, TypeError, ValueError) as exc:
        return SearchOutcome(
            ok=False,
            pattern=text_pattern,
            error=ERROR_BAD_PATTERN,
            message=f"{text_pattern!r} is not a usable regular expression: {exc}",
            cap=cap,
            engine=engine.engine,
            engine_source=engine.source,
            engine_reason=engine.reason,
        )

    start, root, refusal = _search_root(repo_path, path)
    if refusal or start is None:
        return SearchOutcome(
            ok=False,
            pattern=text_pattern,
            error=ERROR_BAD_PATH,
            message=f"search path is not a readable location inside the repository: {path!r}",
            cap=cap,
            engine=engine.engine,
            engine_source=engine.source,
            engine_reason=engine.reason,
        )

    file_filter = str(glob).strip() if glob else ""
    collected: List[str] = []  # capped at cap + 1
    files_seen: List[str] = []
    scanned = 0
    duration = 0.0
    truncated_walk = False
    used_engine = engine.engine
    engine_source = engine.source
    engine_reason = engine.reason
    truncated_by = ""

    # NOTE: ``matcher`` is already compiled and already proved usable by the
    # ERROR_BAD_PATTERN refusal above, so this block must NOT compile it again
    # - a second compile is a second place for the two to disagree.
    if engine.available:
        found = search_engine.ripgrep_search(
            engine,
            start,
            root,
            text_pattern,
            file_glob=file_filter,
            cap=cap,
        )
        duration = found.duration_s
        if found.ok:
            collected = list(found.lines)
            files_seen = list(found.files)
            scanned = found.files_scanned
            used_engine = engine.engine
            engine_source = engine.source
            engine_reason = engine.reason
        else:
            # A resolver said the engine was available and the engine then
            # failed. Falling back silently is exactly the degradation this
            # module forbids, so the reason is carried into the receipt and the
            # fallback is announced in the model-facing render.
            engine_reason = (
                f"{engine.engine} was resolved but the search failed "
                f"({found.error}: {found.reason}); the Python walker answered "
                f"instead. {engine.reason}"
            )
            used_engine = search_engine.ENGINE_FALLBACK
            engine_source = search_engine.SOURCE_UNAVAILABLE
            _t0 = time.perf_counter()
            collected, files_seen, scanned, truncated_walk = _python_scan(
                start,
                root,
                matcher,
                text_pattern=text_pattern,
                cap=cap,
                file_cap=file_cap,
                file_filter=file_filter,
            )
            duration = time.perf_counter() - _t0

    if not engine.available:
        _t0 = time.perf_counter()
        collected, files_seen, scanned, truncated_walk = _python_scan(
            start,
            root,
            matcher,
            text_pattern=text_pattern,
            cap=cap,
            file_cap=file_cap,
            file_filter=file_filter,
        )
        duration = time.perf_counter() - _t0

    over_cap = len(collected) > cap
    if not over_cap and truncated_walk:
        truncated_by = "files"
    if over_cap:
        sample_lines = collected[:sample_cap]
        distinct = list(
            dict.fromkeys(line.split(":", 1)[0] for line in collected[:cap])
        )
        return SearchOutcome(
            ok=False,
            pattern=text_pattern,
            error=ERROR_TOO_MANY_MATCHES,
            message=SEARCH_TOO_MANY_MESSAGE,
            sample=sample_lines,
            files=distinct,
            total=cap + 1,
            total_is_lower_bound=True,
            file_count=len(files_seen),
            cap=cap,
            files_scanned=scanned,
            files_not_searched=None if truncated_walk else 0,
            files_not_searched_known=not truncated_walk,
            engine=used_engine,
            engine_source=engine_source,
            engine_reason=engine_reason,
            duration_s=duration,
            truncated_by="matches",
        )

    distinct_files = list(dict.fromkeys(line.split(":", 1)[0] for line in collected))
    if max_results is not None:
        try:
            keep = max(0, min(int(max_results), cap))
        except (TypeError, ValueError):
            keep = len(collected)
        if keep < len(collected):
            collected = collected[:keep]
            truncated_by = "max_results"
            distinct_files = list(
                dict.fromkeys(line.split(":", 1)[0] for line in collected)
            )
    return SearchOutcome(
        ok=True,
        pattern=text_pattern,
        matches=collected,
        files=distinct_files,
        total=len(collected),
        file_count=len(files_seen),
        cap=cap,
        files_scanned=scanned,
        files_not_searched=None if truncated_walk else 0,
        files_not_searched_known=not truncated_walk,
        engine=used_engine,
        engine_source=engine_source,
        engine_reason=engine_reason,
        duration_s=duration,
        truncated_by=truncated_by,
        max_results_applied=(None if max_results is None else len(collected)),
    )


_REGEX_META = frozenset("\\^$.|?*+()[]{}")
#: Escapes whose escaped character is LITERAL punctuation. `\d`, `\w`, `\1` and
#: friends are not literal, and treating them as such would build a required
#: substring that a real match need not contain.
_LITERAL_ESCAPES = frozenset(".^$*+?()[]{}|/-# \t")


def _skip_character_class(text: str, start: int) -> int:
    """Return the index just past the ``[...]`` class beginning at ``start``.

    The whole class is consumed as ONE unit. Consuming only the brackets would
    leave the class's own characters looking like a literal run, and
    ``[a-z]+`` would then require the substring ``"a-z"`` - which a match of a
    single character from that class does not contain. That is not a missed
    optimisation; it is a dropped match, i.e. a wrong answer.
    """
    index = start + 1
    if index < len(text) and text[index] == "^":  # negated class
        index += 1
    if index < len(text) and text[index] == "]":  # a literal `]` first
        index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "]":
            return index + 1
        index += 1
    return len(text)


def _skip_quantifier(text: str, start: int) -> Tuple[int, int]:
    """Return ``(end_index, minimum_count)`` for a quantifier at ``start``.

    A quantifier's body is NOT matched text: ``a{2,3}`` matches ``aa`` or
    ``aaa``, so requiring the substring ``"2,3"`` would drop every real match.
    The brute-force soundness check in this module's suite found exactly that.

    ``minimum_count`` is what decides whether the quantified character is
    OPTIONAL: ``b*`` and ``b{0,3}`` can both match zero occurrences, so their
    target cannot be part of a required run, while ``b+`` and ``b{2,3}`` cannot.

    A brace that does NOT parse as a quantifier returns ``(start, 0)``: Python's
    ``re`` treats ``a{b}`` as the literal text ``a{b}``, so its characters ARE
    required. Returning that unchanged is what keeps the prefilter sound in
    both directions.
    """
    end = text.find("}", start + 1)
    if end == -1:
        return start, 0
    body = text[start + 1 : end]
    if not body:
        return start, 0
    low, _, high = body.partition(",")
    if not low.isdigit():
        return start, 0
    if high and not high.isdigit():
        return start, 0
    return end + 1, int(low)


def _skip_group(text: str, start: int) -> int:
    """Return the index just past the ``(...)`` group beginning at ``start``.

    Only the grouping constructs that contribute NOTHING to the matched text
    are treated as skippable. A plain capturing group is NOT skipped: its
    contents must appear in a match, so treating it as a literal run is sound
    and would produce a longer (better) filter. The forms handled here are
    ``(?:...)``, ``(?=...)``, ``(?!...)``, ``(?<=...)``, ``(?<!...)`` and
    ``(?P<name>...)`` - none of which emit characters of their own.
    """
    if start + 1 >= len(text) or text[start + 1] != "?":
        return start  # not a special group; caller decides
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _skip_character_class(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _has_top_level_alternation(text: str) -> bool:
    """Whether ``text`` contains a ``|`` that is not inside ``[...]`` or ``(...)``.

    A top-level alternation means DIFFERENT BRANCHES MAY MATCH DIFFERENT TEXT,
    so no substring is required by every match: ``(a|b)|a?b1`` is matched by the
    bare string ``a``, and requiring ``"b1"`` would drop it. The brute-force
    soundness check in this module's suite found exactly that.

    A ``|`` nested inside a group or a class is harmless - the whole group is a
    required unit - so only depth-zero alternation disqualifies the pattern.
    A pattern with one costs the optimisation, never correctness.
    """
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _skip_character_class(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "|" and depth == 0:
            return True
        index += 1
    return False


def required_literal(pattern: str) -> str:
    """Return a substring every match of ``pattern`` must contain, or ``""``.

    This is the prefilter ripgrep runs internally, and it is what makes a text
    scan over a 20-million-character tree cheap in Python: a plain ``in`` test
    is a C-level scan, and skipping the regex for the 99.9% of lines that
    cannot possibly match is the difference between a scan that is regex-bound
    and one that is not.

    **Soundness is the property that matters, and unsoundness here is a wrong
    answer rather than a slow one.** A regex match must contain every literal
    run in the pattern, so requiring one of them can only drop lines that could
    not have matched. Every construct whose contents are NOT guaranteed to
    appear in the matched text therefore terminates a run rather than
    contributing to one:

    | construct | why it cannot contribute |
    |---|---|
    | ``[a-z]`` | a class matches ONE character from a set, not the set's text |
    | ``(?:x)`` | a non-capturing group emits nothing |
    | ``(?=x)`` ``(?!x)`` | a lookahead emits nothing |
    | ``(?:x)`` after ``(`` | ditto, and ``(`` itself emits nothing |
    | ``.`` | matches any character but no fixed text |
    | ``^`` ``$`` | anchors, and a match may be a single character |
    | ``\\d`` ``\\w`` ``\\1`` | a class, a backreference, not a literal |

    ``(`` alone DOES end a run (a capture emits nothing) but a capturing group's
    CONTENTS are required by a match, so ``(foo)bar`` legitimately yields
    ``"bar"`` and ``bar(foo)`` yields ``"foo"`` - both sound.

    Returns ``""`` when no usable literal exists, in which case the caller runs
    the regex on every line, exactly as before.
    """
    text = str(pattern or "")
    if not text:
        return ""
    if _has_top_level_alternation(text):
        return ""
    best = ""
    current: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 < len(text) and text[index + 1] in _LITERAL_ESCAPES:
                current.append(text[index + 1])
                index += 2
                continue
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index += 2 if index + 1 < len(text) else 1
            continue
        if char == "[":
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index = _skip_character_class(text, index)
            continue
        if char == "(":
            consumed = _skip_group(text, index)
            if consumed > index:
                if len(current) > len(best):
                    best = "".join(current)
                current = []
                index = consumed
                continue
            # A capturing group: `(`, `)` and any group-prefix consume nothing,
            # but the CONTENTS are required by a match, so the run continues
            # through them and they are simply not appended.
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index += 1
            continue
        if char == "{":
            consumed, minimum = _skip_quantifier(text, index)
            if minimum == 0 and current:
                # The target may match ZERO times, so it is not required.
                current.pop()
            if len(current) > len(best):
                best = "".join(current)
            current = []
            if consumed > index:
                # Either way the run must end at the quantifier: `xb*` is
                # matched by the bare string `x`, so a run that spanned the `b`
                # would require a `b` that need not be there.
                index = consumed
                continue
            # Not a quantifier, and what `re` then does with the braces is not
            # worth modelling: `a{b}c` is literal text, but `a{,3}b` is
            # something else entirely (the brute-force check in this module's
            # suite found a real match for `b` alone). The whole brace group is
            # therefore consumed and contributes NOTHING - contributing its
            # characters is unsound for the second shape, and skipping only the
            # opening brace is unsound too, because then the body (`,3`) reads
            # as a literal run. The filter is merely shorter, never wrong.
            closing = text.find("}", index + 1)
            index = closing + 1 if closing != -1 else index + 1
            continue
        if char in ("*", "?"):
            # The quantified character is OPTIONAL: zero occurrences satisfy
            # it, so it must not be part of a required run. Drop it, then end
            # the run - what follows starts a fresh, independently-required
            # unit (`a*b` requires `b`; `xb*` requires nothing at all).
            if current:
                current.pop()
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index += 1
            continue
        if char == "+":
            # At least one occurrence, so the quantified character IS required;
            # the run simply ends here.
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index += 1
            continue
        if char in _REGEX_META or char == "^":
            if len(current) > len(best):
                best = "".join(current)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if len(current) > len(best):
        best = "".join(current)
    # One character is not a filter, it is overhead: `in` on a 1-char needle
    # plus the regex costs more than the regex alone on a line-dense file.
    return best if len(best) >= 2 else ""


def _python_scan(
    start: Path,
    root: Path,
    matcher: Any,
    *,
    text_pattern: str,
    cap: int,
    file_cap: int,
    file_filter: str,
) -> Tuple[List[str], List[str], int, bool]:
    """The Python fallback walk. Returns ``(lines, files, scanned, truncated)``.

    Uses :func:`walk_code_files_budgeted` - THE walk - rather than a second
    ``os.scandir`` implementation, and it deliberately does **not** re-check
    each candidate with :func:`_safe_source_path`. That check is an O(depth)
    ``is_symlink`` walk plus a ``resolve()`` per file, measured at 4.97 ms per
    file on a 289,584-file checkout, and the walk has ALREADY answered it for
    every candidate it returned (through the hoisted ``PathSafety`` index, or
    through this same per-file check when the optional memory layer is
    unavailable). Asking the same question twice is how a 2.5 ms/file scan
    happens inside a 0.4 ms/file walk.

    The walk returns absolute paths already pruned to the skip set, already
    size-filtered, and already symlink-screened; this function only turns them
    into the ``path:line: text`` shape the caller renders.
    """
    collected: List[str] = []
    files_seen: List[str] = []
    scanned = 0
    # Two bounds, because they bound different things. ``file_cap`` is how many
    # files may be OPENED; the candidate list is enumerated at a multiple of it
    # so a narrow ``glob`` over a huge tree still finds its files instead of
    # being cut off at the first few non-matching paths.
    try:
        # ``root`` is the REPOSITORY root and ``start`` is the (possibly
        # narrowed) directory the search begins in - the shape `_search_root`
        # returns, deliberately, so containment is judged against the repository
        # and not against the subdirectory a caller happened to name.
        start_resolved = start.resolve()
        repo_resolved = root.resolve()
        candidates, not_searched = walk_code_files_budgeted(
            str(repo_resolved),
            max_files=max(file_cap * 4, file_cap),
            start=str(start_resolved),
        )
    except (OSError, ValueError, RuntimeError):
        return [], [], 0, True
    base = str(repo_resolved)
    stopped_early = False
    # The prefilter is derived ONCE per search, and it is a NECESSARY condition
    # for a match, so `needle not in line -> skip` cannot drop a real match.
    # A search whose pattern has no usable literal runs the regex on every line,
    # which is the historical behaviour.
    # The prefilter is derived ONCE per search, and it is a NECESSARY condition
    # for a match, so a miss can only drop text that could not have matched.
    #
    # It is compiled with the SAME `re.IGNORECASE` machinery as the real
    # matcher, deliberately, rather than as `needle in body.lower()`. Two
    # reasons, and the second is the important one:
    #   1. `body.lower()` allocates a second copy of every file - 20 MB of
    #      garbage on this repository, for a test that can run in one pass.
    #   2. `str.lower()` and `re.IGNORECASE` do NOT agree on Unicode. The
    #      Kelvin sign, U+212A, lowercases to `k`; U+0130 lowercases to two
    #      characters. A prefilter using one and a matcher using the other can
    #      disagree, and when they do the prefilter silently DROPS A REAL
    #      MATCH. Deriving both from one engine removes the whole class rather
    #      than the instances we happened to think of.
    needle = required_literal(text_pattern)
    prefilter = re.compile(re.escape(needle), re.IGNORECASE) if needle else None
    for position, candidate in enumerate(candidates):
        if scanned >= file_cap:
            stopped_early = position < len(candidates)
            break
        text = str(candidate)
        try:
            relative = Path(text).relative_to(Path(base)).as_posix()
        except (OSError, ValueError, RuntimeError):
            continue
        if file_filter and not (
            fnmatch.fnmatch(relative, file_filter)
            or fnmatch.fnmatch(Path(relative).name, file_filter)
        ):
            continue
        try:
            body = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        files_seen.append(relative)
        # Tested ONCE PER FILE, not per line. Per line it would be
        # O(lines x filesize): the first version of this did exactly that and
        # turned a 1.2 s scan into a 26 s one, which is the kind of
        # "optimisation" that has to be measured rather than reasoned about.
        if prefilter is not None and not prefilter.search(body):
            continue
        for number, line in enumerate(body.splitlines(), 1):
            if not matcher.search(line):
                continue
            collected.append(f"{relative}:{number}: {line[:200]}")
            if len(collected) > cap:
                break
        if len(collected) > cap:
            break
    return collected, files_seen, scanned, stopped_early or bool(not_searched)


def _iter_bounded_files(
    root: Path, repo_root: Path, *, limit: int
) -> Tuple[List[str], bool]:
    """List repo-relative text candidates under ``root``.

    Returns ``(paths, exhausted)``. ``exhausted`` is False when the walk hit
    ``limit`` before finishing, which is the caller's signal that the remainder
    was never enumerated — a different fact from "there was no remainder", and
    one it must not report as zero.

    **This is a thin adapter over :func:`walk_code_files_budgeted`, and it used
    to be a second ``os.scandir`` implementation.** That is the whole 345x story
    in miniature: the search path carried its own walk, so it never picked up
    the hoisted ``PathSafety`` index (measured at 4.97 ms/file) that the
    retrieval path had already been given, and a walk whose cost is decided
    entirely by the skip set is exactly the thing that must not be written
    twice. One walk, one skip set, one safety index.
    """
    ceiling = max(1, int(limit or 1))
    try:
        candidates, not_searched = walk_code_files_budgeted(
            str(repo_root), max_files=ceiling, start=str(root)
        )
    except (OSError, ValueError, RuntimeError):
        return [], True
    out: List[str] = []
    base = str(repo_root)
    for candidate in candidates:
        text = str(candidate)
        if text == base:
            continue
        try:
            out.append(Path(text).relative_to(Path(base)).as_posix())
        except (OSError, ValueError, RuntimeError):
            continue
    return out, not_searched == 0


def _subwords(name: str) -> Set[str]:
    """Split a snake_case/camelCase identifier into lowercase words.

    ``compute_monthly_average`` -> {compute, monthly, average}; ``HTTPServer``
    -> {http, server}. Used so an issue saying "monthly average is wrong"
    matches the symbol compute_monthly_average even though the full
    identifier never appears in the issue text.
    """
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", split)
    return {w.lower() for w in re.split(r"[_\s]+", split) if len(w) >= 3}


# ---------------------------------------------------------------------------
# Structural layer (Phase 2)
# ---------------------------------------------------------------------------


def _module_id_for(t_file: str) -> str:
    """Graph module node id for a target-test file path (language-aware).

    Python: tests/test_x.py -> module:tests.test_x (the dotted module
    name, matching memory.code_graph's Python indexer). JS/TS: the
    extension-stripped path dotted (src/util.test.js -> module:src.util
    .test), matching the JS indexer's path-based identity. Assumes
    t_file is a normalized repo-relative posix path.
    """
    parts = t_file.split("/")
    js_exts = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    elif parts[-1].endswith(js_exts):
        for ext in js_exts:
            if parts[-1].endswith(ext) and parts[-1] != ext:
                parts[-1] = parts[-1][: -len(ext)]
                break
    return "module:" + ".".join(p for p in parts if p)


def _structural_scores(
    repo_path: str,
    terms: List[str],
    target_test: Optional[str],
    index_root: Optional[Path],
    *,
    deadline_s: Optional[float] = None,
    graph_receipt: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort structural ranking via memory.code_graph.

    Returns {"file_scores": {rel: score}, "symbol_notes": [str], "graph":
    Graph} or None when the graph layer is unavailable/unusable (caller
    degrades to grep-only). NEVER raises.

    R2-09: the index is resolved through :func:`load_code_graph`, which uses the
    code graph's OWN persistent per-repository root when ``index_root`` is
    None. This function used to build into a fresh ``tempfile`` directory on
    every call, so every retrieval re-parsed the whole repository and threw the
    index away — measured at 33-45 s per retrieval on a 289,584-file checkout
    even when a valid index already existed on disk. The "the default root lives
    inside the repo" comment that justified the temp dir was STALE: the default
    is ``memory.paths.harness_home()/code-graph``, outside the repository, and
    the never-mutate guarantee is unchanged (it is harness-owned state).
    """
    try:
        from harness.deps import get_code_graph_factory

        factory = get_code_graph_factory()
    except Exception:
        factory = None
    if factory is None:
        return None
    try:
        graph = load_code_graph(repo_path, index_root, receipt=graph_receipt)
    except Exception:
        return None
    if graph is None:
        return None

    nodes = getattr(graph, "nodes", {}) or {}
    calls = getattr(graph, "calls", set()) or set()
    imports = getattr(graph, "imports", set()) or set()
    file_scores: Dict[str, float] = {}
    notes: List[str] = []

    # -- helpers ----------------------------------------------------------
    def bump(rel: str, pts: float, why: str) -> None:
        if rel:
            file_scores[rel] = file_scores.get(rel, 0.0) + pts

    def seed_files_of(node_id: str) -> List[str]:
        info = nodes.get(node_id)
        return [info.file] if info is not None else []

    # -- 1. ANCHOR: target test -> the symbols it exercises --------------
    anchor_ids: Set[str] = set()
    if target_test:
        t_file = (
            target_test.split("::")[0].split(" - ")[0].strip("/").replace("\\", "/")
        )
        # normalize the usual target-id forms per language:
        # pytest: tests/test_x.py or ./tests/test_x.py; also accept a bare
        # test name (assume under tests/). JS/TS (vitest/jest ids carry
        # '<file> - <name>' or '<file>::<name>'): any .js/.ts-family file
        # is used as-is.
        if "/" not in t_file and not t_file.endswith(
            (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
        ):
            t_file = f"tests/test_{t_file}.py"
        file_node = f"file:{t_file}"
        # module identity is language-specific: dotted for Python, the
        # path-based dotted name for JS/TS (both produce module:<name>
        # nodes — see memory.code_graph._js_module_name)
        t_mod = _module_id_for(t_file)
        if file_node in nodes:
            anchor_ids.add(file_node)
        if t_mod and t_mod in nodes:
            anchor_ids.add(t_mod)
        if not anchor_ids:
            # find a file whose rel path ends with the node-id's file part
            suffix = t_file.split("/")[-1]
            for nid, info in nodes.items():
                if nid.startswith("file:") and info.file.endswith(suffix):
                    anchor_ids.add(nid)
                    break
        if anchor_ids:
            for nid in anchor_ids:
                for rel in seed_files_of(nid):
                    bump(rel, 3.0, "target-test anchor")
            notes.append(f"anchored on target test {target_test}")

    # expand anchors: what the test module imports + calls
    expanded: Set[str] = set(anchor_ids)

    def expand(ids: Set[str], hops: int) -> None:
        """One BFS hop along import/call edges from the given node ids."""
        for _ in range(hops):
            frontier: Set[str] = set()
            for src, dst in imports:
                if src in ids and dst not in ids:
                    frontier.add(dst)
                if dst in ids and src not in ids:
                    frontier.add(src)
            for src, dst in calls:
                if src in ids and dst not in ids:
                    frontier.add(dst)
                if dst in ids and src not in ids:
                    frontier.add(src)
            ids |= frontier
            if not frontier:
                return

    if anchor_ids:
        # imports first (test -> module under test), then calls out of the
        # test module (test -> buggy symbol)
        expand(expanded, 2)

    # test-module-only guard: expansion from the TEST should reach the code
    # under test but must not flood the whole repo; only files reachable
    # within these hops score.
    for nid in expanded:
        for rel in seed_files_of(nid):
            if nodes.get(nid) is not None and nodes[nid].kind != "file":
                # symbol/module nodes carry their defining file; file nodes
                # score via the anchor bump above
                bump(rel, 1.5, "structurally adjacent (import/call edge)")

    # -- 2. SYMBOL MATCH: terms vs the symbol table ----------------------
    # Subword matching: "average" matches compute_monthly_average because
    # identifiers decompose into words. This is what lets the harness find
    # the right file when the issue names a CONCEPT, not the identifier.
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    term_words: Set[str] = set()
    for tl in lows:
        term_words |= _subwords(tl)
    matched: Set[str] = set()
    for nid, info in nodes.items():
        if info.kind not in ("func", "method", "class"):
            continue
        hay_name = info.name.lower()
        hay_qual = info.qualified.lower()
        hay_doc = (info.docstring or "").lower()
        sym_words = _subwords(info.name) | _subwords(info.qualified.split(".")[-1])
        for tl in lows:
            if tl == hay_name or tl in hay_qual or (len(tl) >= 4 and tl in hay_doc):
                matched.add(nid)
                break
        else:
            if sym_words and term_words and (sym_words & term_words):
                matched.add(nid)
    for nid in matched:
        for rel in seed_files_of(nid):
            bump(rel, 4.0, "symbol-name match")

    # -- 3. NEIGHBORHOOD: call-graph expansion of matched symbols --------
    if matched:
        expanded_m: Set[str] = set(matched)
        # one hop is deliberate: the buggy symbol + its direct callers
        # (often the failing test) + direct callees (often the true defect
        # when the matched name is a wrapper)
        for src, dst in calls:
            if src in matched and dst not in matched:
                expanded_m.add(dst)
            if dst in matched and src not in matched:
                expanded_m.add(src)
        for nid in expanded_m:
            for rel in seed_files_of(nid):
                if nid in matched:
                    continue  # already scored at full weight
                bump(rel, 1.0, "call-graph neighbor of matched symbol")
        notes.append(f"{len(matched)} symbol(s) matched issue terms")

    return {"file_scores": file_scores, "symbol_notes": notes, "graph": graph}


# ---------------------------------------------------------------------------
# Content-digest retrieval cache (VEX-CEILING-09)
# ---------------------------------------------------------------------------
#
# The cache is keyed by the REQUEST (repo identity + terms + caps + target
# test + index identity), and every entry stores the content digest of exactly
# the files it cited. A hit therefore requires re-hashing only those files
# (a few dozen KB), not the repository, and it can never serve a ranking built
# from bytes that have since changed. The graph parse behind retrieval is
# cached separately, by index digest, in `load_code_graph`.

CACHE_HIT = "hit"
CACHE_MISS = "miss"
CACHE_REFRESHED = "refreshed"
CACHE_DISABLED = "disabled"

_CONTEXT_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CONTEXT_CACHE_MAX = 32
_CONTEXT_CACHE_LOCK = threading.RLock()
_CONTEXT_CACHE_STATS = {"hits": 0, "misses": 0, "refreshed": 0, "stores": 0}


def _context_cache_key(
    repo_path: str,
    terms: Sequence[str],
    *,
    max_files: int,
    max_grep_lines: int,
    target_test: Optional[str],
    index_root: Optional[Path],
    include_citations: bool,
) -> str:
    """Return the request key for one retrieval (cheap: no file is read)."""
    try:
        identity = str(Path(repo_path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        identity = str(repo_path or "")
    return json_digest(
        {
            "repo": identity,
            "terms": list(terms),
            "max_files": int(max_files),
            "max_grep_lines": int(max_grep_lines),
            "target_test": str(target_test or ""),
            "index": _index_artifact_digest(index_root),
            "citations": bool(include_citations),
        }
    )


def _context_cache_get(key: str) -> Optional[Dict[str, Any]]:
    """Return a cached retrieval entry, or None. Never revalidates."""
    with _CONTEXT_CACHE_LOCK:
        entry = _CONTEXT_CACHE.get(key)
        if entry is not None:
            _CONTEXT_CACHE.move_to_end(key)
        return dict(entry) if entry is not None else None


def _context_cache_put(key: str, entry: Dict[str, Any]) -> None:
    """Store one retrieval entry, evicting the least recently used."""
    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE[key] = dict(entry)
        _CONTEXT_CACHE.move_to_end(key)
        while len(_CONTEXT_CACHE) > _CONTEXT_CACHE_MAX:
            _CONTEXT_CACHE.popitem(last=False)
        _CONTEXT_CACHE_STATS["stores"] += 1


def context_cache_stats() -> Dict[str, Any]:
    """Return cache counters plus the live hit rate (evidence, not a claim)."""
    with _CONTEXT_CACHE_LOCK:
        stats = dict(_CONTEXT_CACHE_STATS)
        stats["entries"] = len(_CONTEXT_CACHE)
        decided = stats["hits"] + stats["misses"] + stats["refreshed"]
        stats["hit_rate"] = round(stats["hits"] / decided, 4) if decided else 0.0
        return stats


def clear_context_cache() -> None:
    """Drop every cached retrieval and reset the counters."""
    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE.clear()
        for key in _CONTEXT_CACHE_STATS:
            _CONTEXT_CACHE_STATS[key] = 0


def _emit_retrieval_trace(
    trace_hook: Optional[Callable[[str, Dict[str, Any]], None]],
    result: Mapping[str, Any],
    cache_key: str,
    cache_status: str,
    entry: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit the retrieval receipt, including the cache status. Never raises.

    R2-09: the payload also carries the completeness label
    (``truncated``/``truncation``/``not_searched``/``not_searched_files``) and
    the per-stage wall clock, so a bounded retrieval is visible in the trace
    even when the caller only renders the historical four-key result.
    """
    if trace_hook is None:
        return
    payload: Dict[str, Any] = {
        "strategy": result.get("strategy", ""),
        "files": list(result.get("files") or []),
        "citations": result.get("citations", []),
        "source_digest": result.get("source_digest", "")
        or (entry or {}).get("source_digest", ""),
        "index_digest": result.get("index_digest", ""),
        "cache_status": cache_status,
        "cache_key": cache_key,
    }
    if receipt is not None:
        payload.update(
            {
                "truncated": bool(receipt.get("truncated")),
                "truncation": str(receipt.get("truncation", "")),
                "not_searched": int(receipt.get("not_searched") or 0),
                "not_searched_files": list(receipt.get("not_searched_files") or []),
                "budget_s": float(receipt.get("budget_s") or 0.0),
                "elapsed_s": float(receipt.get("elapsed_s") or 0.0),
                "stages": dict(receipt.get("stages") or {}),
                "error": str(receipt.get("error", "")),
            }
        )
    try:
        trace_hook("retrieval_context", payload)
    except Exception:
        pass


def _note_cache_outcome(status: str) -> None:
    """Record one retrieval's cache outcome in the counters."""
    with _CONTEXT_CACHE_LOCK:
        if status == CACHE_HIT:
            _CONTEXT_CACHE_STATS["hits"] += 1
        elif status == CACHE_REFRESHED:
            _CONTEXT_CACHE_STATS["refreshed"] += 1
        else:
            _CONTEXT_CACHE_STATS["misses"] += 1


#: ``truncated`` values in a retrieval receipt. ``complete`` is the only one
#: that may be reported as a whole answer.
TRUNCATION_COMPLETE = "complete"
TRUNCATION_BUDGET = "budget"
TRUNCATION_FRONTIER = "frontier"
TRUNCATION_SPARSE = "sparse"
TRUNCATION_ERROR = "error"


@dataclass
class RetrievalOutcome:
    """One retrieval call's answer plus its honest completeness label.

    ``result`` is the historical four-key mapping
    (``terms``/``files``/``greps``/``strategy``) and is byte-compatible with
    :func:`retrieve_context`. ``receipt`` is the additive audit surface:
    ``truncated`` (bool), ``truncation`` (one of the ``TRUNCATION_*`` values),
    ``not_searched`` (int), ``not_searched_files`` (list), ``budget_s``,
    ``elapsed_s``, ``stages`` (per-stage wall clock), ``cache_status``, and
    ``error``.

    A caller that renders only ``result`` is rendering a PARTIAL answer unless
    ``receipt["truncated"]`` is False, which is why both are returned together
    rather than merged.
    """

    result: Dict[str, Any] = field(default_factory=dict)
    receipt: Dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """Whether this answer is partial, per the receipt."""
        return bool(self.receipt.get("truncated"))

    def to_dict(self) -> Dict[str, Any]:
        """Return the result mapping with the receipt folded in, for a caller
        that wants one document (the four historical keys keep their names and
        values; ``truncated`` and ``receipt`` are added)."""
        out = dict(self.result)
        out["truncated"] = self.truncated
        out["receipt"] = dict(self.receipt)
        return out

    @classmethod
    def from_result(cls, result: Dict[str, Any]) -> "RetrievalOutcome":
        """Wrap a plain four-key result with an empty, complete receipt."""
        return cls(result=dict(result), receipt={"truncated": False})


def retrieve_context(
    repo_path: str,
    issue_text: str,
    max_files: int = 4,
    max_grep_lines: int = 20,
    target_test: Optional[str] = None,
    index_root: Optional[Path] = None,
    include_citations: bool = False,
    trace_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    citations: bool = False,
    cache: bool = True,
    *,
    budget_s: Optional[float] = None,
) -> dict:
    """Two-layer retrieval: structural (imports/call graph) + grep fallback.

    Assumes repo_path exists and issue_text is the bug report; target_test
    (a pytest node id "file.py::test_name" for Python repos, or a
    "<file> - <name>" / "<file>::<name>" id for JS/TS vitest/jest repos —
    from task.config, when known) is the strongest
    anchor: the test that encodes the bug leads to the code that has it,
    even when the issue text never names the relevant symbol. Returns
    {"terms", "files", "greps", "strategy"} — files are repo-relative
    posix paths ranked best-first; strategy records which layer(s) ran
    (for the trace + tests). Never raises: structural failure degrades to
    grep-only with strategy="grep (structural layer unavailable)".

    ``cache`` (default on) reuses a previous result for the SAME request when
    the current content digest of every file that result cited still matches.
    That validation is the whole point: a hit is only served when the bytes
    behind it are unchanged, so the cache can never hand back a ranking
    derived from an edited file.

    The receipt is NOT added to the returned mapping — the historical four-key
    shape is a published contract. It rides the ``retrieval_context`` trace
    payload (``cache_status`` / ``cache_key`` / ``truncated`` /
    ``not_searched``) and the process-level counters in
    :func:`context_cache_stats`, where ``cache_status`` is ``hit`` (served
    from cache), ``miss`` (nothing cached), ``refreshed`` (cached but a cited
    file changed, so it was recomputed) or ``disabled``.

    ``budget_s`` (keyword-only, R2-09) bounds the WHOLE call. When it is
    exhausted the best-ranked results computed so far are returned and the
    receipt says ``truncated: true`` with what was not searched. Use
    :func:`retrieve_context_budgeted` to read the receipt directly.
    """
    return retrieve_context_budgeted(
        repo_path,
        issue_text,
        max_files=max_files,
        max_grep_lines=max_grep_lines,
        target_test=target_test,
        index_root=index_root,
        include_citations=include_citations,
        trace_hook=trace_hook,
        citations=citations,
        cache=cache,
        budget_s=budget_s,
    ).result


def retrieve_context_budgeted(
    repo_path: str,
    issue_text: str,
    max_files: int = 4,
    max_grep_lines: int = 20,
    target_test: Optional[str] = None,
    index_root: Optional[Path] = None,
    include_citations: bool = False,
    trace_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    citations: bool = False,
    cache: bool = True,
    *,
    budget_s: Optional[float] = None,
) -> RetrievalOutcome:
    """Budgeted :func:`retrieve_context` that returns its completeness label.

    The budget is a wall-clock deadline checked BETWEEN stages (terms, cache
    revalidation, structural scores, grep scores, per-file greps), plus a
    deadline INSIDE the two stages that scan the whole repository
    (``_structural_scores``'s index load and ``_grep_scores``' full-text
    scan). An exhausted budget stops the remaining stages, keeps every result
    already computed, sets ``truncated`` with the name of the stage that was
    not run, and returns.

    Assumes the same inputs as :func:`retrieve_context`. Never raises: a stage
    that raises is recorded in ``receipt["error"]`` and the call still returns
    whatever ranking it has. The one stage that cannot be interrupted is a
    cold index BUILD (``CodeGraph.load_or_build``), because a half-built index
    is a wrong index; its measured cost is reported as
    ``receipt["stages"]["structural"]`` and the structural layer degrades to
    grep-only rather than blocking the caller.
    """
    started = time.monotonic()
    budget: Optional[float] = None
    if budget_s is not None:
        try:
            budget = max(0.0, float(budget_s))
        except (TypeError, ValueError):
            budget = None
    deadline = (started + budget) if budget is not None else None
    receipt: Dict[str, Any] = {
        "truncated": False,
        "truncation": TRUNCATION_COMPLETE,
        "not_searched": 0,
        "not_searched_known": True,
        "not_searched_files": [],
        "budget_s": budget if budget is not None else 0.0,
        "budget_exhausted": False,
        "elapsed_s": 0.0,
        "stages": {},
        "cache_status": CACHE_DISABLED if not cache else "",
        "cache_key": "",
        "error": "",
    }
    stages = receipt["stages"]

    def remaining() -> float:
        if deadline is None:
            return float("inf")
        return max(0.0, deadline - time.monotonic())

    def exhausted() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    terms = extract_terms(issue_text)
    if citations:
        include_citations = True
    try:
        max_files = max(1, min(20, int(max_files)))
    except (TypeError, ValueError):
        max_files = 4
    try:
        max_grep_lines = max(1, min(100, int(max_grep_lines)))
    except (TypeError, ValueError):
        max_grep_lines = 20

    cache_key = _context_cache_key(
        repo_path,
        terms,
        max_files=max_files,
        max_grep_lines=max_grep_lines,
        target_test=target_test,
        index_root=index_root,
        include_citations=include_citations,
    )
    receipt["cache_key"] = cache_key
    if cache:
        cached = _context_cache_get(cache_key)
        if cached is not None:
            stage_started = time.monotonic()
            current = source_digest(repo_path, cached.get("cited_files"))
            stages["cache_revalidate"] = round(time.monotonic() - stage_started, 6)
            if current and current == cached.get("source_digest"):
                result = cached["result"]
                receipt["cache_status"] = CACHE_HIT
                _note_cache_outcome(CACHE_HIT)
                receipt["elapsed_s"] = round(time.monotonic() - started, 6)
                _emit_retrieval_trace(
                    trace_hook, result, cache_key, CACHE_HIT, cached, receipt
                )
                return RetrievalOutcome(result=result, receipt=receipt)

    structural: Optional[Dict[str, Any]] = None
    stage_started = time.monotonic()
    if exhausted():
        receipt["truncated"] = True
        receipt["truncation"] = TRUNCATION_BUDGET
    else:
        try:
            if target_test or terms:
                structural = _structural_scores(
                    repo_path,
                    terms,
                    target_test,
                    index_root,
                    deadline_s=deadline,
                )
        except Exception as exc:  # never raise into a task run
            structural = None
            receipt["error"] = f"structural: {type(exc).__name__}: {exc}"
    stages["structural"] = round(time.monotonic() - stage_started, 6)

    files: List[str]
    strategy: str
    if exhausted():
        # No ranking at all yet: report an empty, labelled answer rather than
        # inventing one, and say the structural stage was skipped. The
        # candidate count is deliberately NOT measured here — counting it means
        # another full walk of the tree, which is exactly the cost the budget
        # just refused to pay. `not_searched_known: False` says the number is
        # absent rather than reading 0 as "nothing was left".
        files = []
        strategy = "budget exhausted before ranking"
        receipt["truncated"] = True
        receipt["truncation"] = TRUNCATION_BUDGET
        receipt["budget_exhausted"] = True
        receipt["not_searched"] = 0
        receipt["not_searched_known"] = False
    elif structural is not None and structural["file_scores"]:
        # merge: structural score + grep score (grep alone ranks poorly on
        # repos where the issue text names nothing)
        stage_started = time.monotonic()
        try:
            grep_scores, grep_unread = _grep_scores(
                repo_path, terms, deadline_s=deadline
            )
        except Exception as exc:  # never raise into a task run
            grep_scores, grep_unread = {}, 0
            receipt["error"] = f"grep: {type(exc).__name__}: {exc}"
        stages["grep"] = round(time.monotonic() - stage_started, 6)
        merged: Dict[str, float] = {}
        for rel, s in structural["file_scores"].items():
            merged[rel] = s * 2.0  # structural carries the repo's real edges
        for rel, s in grep_scores.items():
            merged[rel] = merged.get(rel, 0.0) + s
        ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        files = [rel for rel, _ in ranked[:max_files]]
        strategy = "structural+grep"
        if structural["symbol_notes"]:
            strategy += f" ({'; '.join(structural['symbol_notes'])})"
        if grep_unread:
            receipt["truncated"] = True
            receipt["truncation"] = TRUNCATION_BUDGET
            receipt["budget_exhausted"] = True
            receipt["not_searched_known"] = True
            receipt["not_searched"] = max(receipt["not_searched"], grep_unread)
    else:
        stage_started = time.monotonic()
        try:
            candidates, walk_unsearched = walk_code_files_budgeted(
                repo_path, deadline_s=deadline
            )
            files = _score_candidate_files(
                candidates,
                Path(repo_path),
                terms,
                max_files,
                deadline_s=deadline,
            )
        except Exception as exc:  # never raise into a task run
            files = []
            walk_unsearched = 0
            receipt["error"] = f"rank_files: {type(exc).__name__}: {exc}"
        stages["grep"] = round(time.monotonic() - stage_started, 6)
        strategy = (
            "grep" if structural is None else "grep (structural layer found nothing)"
        )
        if walk_unsearched:
            receipt["truncated"] = True
            receipt["truncation"] = TRUNCATION_BUDGET
            receipt["budget_exhausted"] = True
            receipt["not_searched_known"] = True
            receipt["not_searched"] = max(receipt["not_searched"], walk_unsearched)

    greps: Dict[str, List[Any]] = {}
    stage_started = time.monotonic()
    skipped: List[str] = []
    for f in files:
        if exhausted():
            skipped.append(f)
            continue
        try:
            greps[f] = search_file_for_lines(repo_path, f, terms)[:max_grep_lines]
        except Exception as exc:  # never raise into a task run
            greps[f] = []
            receipt["error"] = receipt["error"] or f"grep_lines: {type(exc).__name__}"
    stages["grep_lines"] = round(time.monotonic() - stage_started, 6)
    if skipped:
        # The RANKING is kept. Those files are ranked on real structural and
        # lexical evidence; only their line-level excerpts are missing, and the
        # receipt says which. Dropping the ranked files here would discard the
        # best-ranked results — the opposite of what a budget is for.
        receipt["truncated"] = True
        receipt["truncation"] = TRUNCATION_BUDGET
        receipt["budget_exhausted"] = True
        receipt["not_searched_known"] = True
        receipt["not_searched"] = max(receipt["not_searched"], len(skipped))
        receipt["not_searched_files"] = list(skipped[:20])
        receipt["greps_missing_for"] = list(skipped[:20])

    if skipped or receipt["not_searched"]:
        strategy = f"{strategy} [truncated: {receipt['truncation']}]"
    result: Dict[str, Any] = {
        "terms": terms,
        "files": files,
        "greps": greps,
        "strategy": strategy,
    }
    digest = source_digest(repo_path, files)
    if include_citations:
        result["citations"] = context_citations(result, repo_path)
        result["source_digest"] = digest
        result["index_digest"] = _index_artifact_digest(index_root)
    if cache:
        had_entry = cache_key in _CONTEXT_CACHE
        status = CACHE_REFRESHED if had_entry else CACHE_MISS
        receipt["cache_status"] = status
        _note_cache_outcome(status)
        # A truncated ranking is never cached: a later call would serve a
        # partial answer as a complete one.
        if not receipt["truncated"]:
            _context_cache_put(
                cache_key,
                {
                    "result": {key: value for key, value in result.items()},
                    "cited_files": list(files),
                    "source_digest": digest,
                },
            )
        _emit_retrieval_trace(trace_hook, result, cache_key, status, None, receipt)
    else:
        receipt["cache_status"] = CACHE_DISABLED
        _emit_retrieval_trace(
            trace_hook, result, cache_key, CACHE_DISABLED, None, receipt
        )
    receipt["elapsed_s"] = round(time.monotonic() - started, 6)
    if deadline is not None and time.monotonic() >= deadline:
        receipt["budget_exhausted"] = True
        if not receipt["truncated"]:
            receipt["truncated"] = True
            receipt["truncation"] = TRUNCATION_BUDGET
    if receipt["error"] and not receipt["truncated"]:
        receipt["truncation"] = TRUNCATION_ERROR
    return RetrievalOutcome(result=result, receipt=receipt)


def _grep_scores(
    repo_path: str,
    terms: List[str],
    *,
    deadline_s: Optional[float] = None,
) -> Tuple[Dict[str, float], int]:
    """Score every candidate file by term overlap (the rank_files scoring,
    returned as a dict instead of a truncated list).

    ``deadline_s`` is an absolute ``time.monotonic()`` value. This stage reads
    the FULL TEXT of every candidate file, so on a large repository it is the
    most expensive part of retrieval by far; when the deadline passes the scan
    stops and the second return value says how many files were never read.
    The partial score map is still returned — a ranking over the files that
    were searched beats no answer, and the caller is told it is partial.
    Never raises.
    """
    files, walk_unsearched = walk_code_files_budgeted(repo_path, deadline_s=deadline_s)
    lows = [t.lower().strip("./-") for t in terms if len(t.strip("./-")) >= 3]
    out: Dict[str, float] = {}
    unread = walk_unsearched
    for position, f in enumerate(files):
        if (
            deadline_s is not None
            and not (position & 15)
            and time.monotonic() >= deadline_s
        ):
            unread = max(unread, len(files) - position)
            break
        rel = f.relative_to(repo_path).as_posix()
        rel_l = rel.lower()
        score = 0.0
        for tl in lows:
            if tl in rel_l or tl in f.stem.lower():
                score += 2
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            text = ""
        for tl in lows:
            c = text.count(tl)
            if c:
                score += min(c, 5)
        if score > 0:
            if f.suffix in _CODE_EXTS:
                score += 1
            out[rel] = score
    return out, unread


def _count_repo_files(repo_path: str) -> int:
    """Count repository files without following the code-retrieval filters."""
    root = Path(repo_path)
    if not root.is_dir():
        return 0
    count = 0
    try:
        for _dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            count += len(filenames)
    except OSError:
        return count
    return count


def size_context_budget(
    issue_text: str,
    repo_path: str,
    files_touched: Optional[List[str]] = None,
    base_files: int = 4,
    base_lines: int = 60,
    repo_file_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a bounded context budget derived from task and repository size.

    The result contains max_files, max_lines, and budget_signals. A short
    issue in a small repository gets the minimum useful context; a long
    issue or large repository gets the upper bound. files_touched keeps
    already-active files in the budget instead of shrinking it. A caller
    may provide repo_file_count to avoid walking the repository twice.
    """
    issue_chars = len(issue_text or "")
    touched_count = len(files_touched or [])
    if repo_file_count is None:
        repo_files = _count_repo_files(repo_path)
    else:
        try:
            repo_files = max(0, int(repo_file_count))
        except (TypeError, ValueError):
            repo_files = 0
    if issue_chars >= 1000 or repo_files >= 400:
        adjustment = 1.0
    elif issue_chars <= 80 and repo_files <= 20 and touched_count <= 1:
        adjustment = -0.5
    else:
        adjustment = 0.0
    try:
        base_files = max(1, int(base_files))
    except (TypeError, ValueError):
        base_files = 4
    try:
        base_lines = max(1, int(base_lines))
    except (TypeError, ValueError):
        base_lines = 60
    max_files = max(2, min(8, round(base_files * (1.0 + adjustment))))
    max_lines = max(30, min(120, round(base_lines * (1.0 + adjustment))))
    return {
        "max_files": max_files,
        "max_lines": max_lines,
        "budget_signals": {
            "issue_chars": issue_chars,
            "repo_files": repo_files,
            "files_touched": touched_count,
            "adjustment": adjustment,
        },
    }


def _normalized_rel(value: str) -> str:
    """Normalize a candidate path without allowing traversal components."""
    text = str(value or "").replace("\\", "/").strip()
    if (
        not text
        or Path(text).is_absolute()
        or "\x00" in text
        or re.match(r"^[A-Za-z]:[\\/]", text)
    ):
        return ""
    parts: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def rerank_files(
    repo_path: str,
    candidates: List[str],
    step: Dict[str, Any],
    limit: Optional[int] = None,
) -> List[str]:
    """Reorder retrieval candidates for one plan step without dropping any.

    Step file hints receive the strongest signal, followed by path and
    content overlap. Missing or unreadable candidates remain in their
    original order at the tail, so a relevance pass cannot erase the
    retrieval safety net.
    """
    original = [str(c).replace("\\", "/") for c in (candidates or [])]
    step = step if isinstance(step, dict) else {}
    hint_values = [str(v).replace("\\", "/") for v in (step.get("files_hint") or [])]
    hints = {_normalized_rel(v) for v in hint_values if _normalized_rel(v)}
    terms = extract_terms(
        " ".join(str(step.get(key) or "") for key in ("description", "checkpoint"))
    )
    root = Path(repo_path)
    scored: List[Tuple[float, int, str]] = []
    for index, candidate in enumerate(original):
        norm = _normalized_rel(candidate) or candidate
        score = 0.0
        if norm in hints:
            score += 1000.0
        else:
            for hint in hints:
                if Path(norm).name == Path(hint).name:
                    score += 100.0
        low_path = norm.lower()
        for term in terms:
            if term.lower() in low_path:
                score += 10.0
        try:
            path = (root / norm).resolve()
            path.relative_to(root.resolve())
            if path.is_file() and path.stat().st_size <= _LARGE_FILE:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for term in terms:
                    count = text.count(term.lower())
                    if count:
                        score += min(count, 5) * 3.0
        except (OSError, ValueError, RuntimeError):
            pass
        scored.append((score, index, norm))
    scored.sort(key=lambda item: (-item[0], item[1]))
    result = [item[2] for item in scored]
    if limit is not None:
        try:
            return result[: max(0, int(limit))]
        except (TypeError, ValueError):
            return result
    return result


def _strict_relative(value: str) -> str:
    """Normalize a repository path while refusing traversal and drives."""
    text = str(value or "").replace("\\", "/").strip()
    if (
        not text
        or "\x00" in text
        or Path(text).is_absolute()
        or re.match(r"^[A-Za-z]:[\\/]", text)
    ):
        return ""
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return ""
    return "/".join(parts)


def _safe_source_path(repo_path: str, rel_path: str) -> Optional[Path]:
    """Resolve a contained source file without following symlink components."""
    relative = _strict_relative(rel_path)
    if not relative:
        return None
    try:
        root = Path(repo_path).resolve()
        current = root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                return None
        candidate = current.resolve()
        candidate.relative_to(root)
        if not candidate.is_file() or candidate.stat().st_size > _LARGE_FILE:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def file_digest(path: str | Path) -> str:
    """Return a SHA-256 digest for one readable file, or a stable miss marker."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, TypeError, ValueError):
        return "missing"


def _safe_digest_path(root: Path, relative: str) -> Optional[Path]:
    """Resolve a source path for digesting without retrieval size limits."""
    normalized = _strict_relative(relative)
    if not normalized:
        return None
    try:
        current = root
        for part in normalized.split("/"):
            current = current / part
            if current.is_symlink():
                return None
        resolved = current.resolve()
        resolved.relative_to(root)
        if not resolved.is_file() or resolved.stat().st_size > 2_000_000:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def source_digest(repo_path: str, paths: Optional[Iterable[str]] = None) -> str:
    """Return a content digest for selected or all candidate repository files."""
    if not str(repo_path or "").strip():
        return "unavailable"
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return "unavailable"
    if not root.is_dir():
        return "unavailable"
    if paths is None:
        relatives: List[str] = []
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            for filename in filenames:
                candidate = Path(directory) / filename
                try:
                    if candidate.stat().st_size <= 2_000_000:
                        relatives.append(candidate.relative_to(root).as_posix())
                except OSError:
                    continue
    else:
        relatives = []
        for value in paths:
            normalized = _strict_relative(str(value))
            if normalized and normalized not in relatives:
                relatives.append(normalized)
    payload: List[Tuple[str, str]] = []
    for relative in sorted(relatives):
        path = _safe_digest_path(root, relative)
        payload.append((relative, file_digest(path) if path else "missing"))
    encoded = json_digest(payload)
    return encoded


def json_digest(value: Any) -> str:
    """Return a deterministic SHA-256 digest for JSON-compatible data."""
    import json

    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


def _index_artifact_digest(index_root: Optional[Path]) -> str:
    """Digest graph and metadata artifacts under an index root."""
    if index_root is None:
        return "no-index"
    root = Path(index_root)
    artifacts: List[Tuple[str, str]] = []
    try:
        for path in sorted(root.rglob("*")):
            if path.name not in ("graph.json", "meta.json") or not path.is_file():
                continue
            artifacts.append((path.relative_to(root).as_posix(), file_digest(path)))
    except (OSError, RuntimeError, ValueError):
        return "unreadable-index"
    return json_digest(artifacts) if artifacts else "empty-index"


def index_digest(index_root: Optional[Path]) -> str:
    """Return a digest for a persisted structural index, if present."""
    return _index_artifact_digest(index_root)


def repository_digest(
    repo_path: str,
    index_root: Optional[Path] = None,
    files: Optional[Iterable[str]] = None,
) -> str:
    """Return a cache digest combining repository content and index artifacts."""
    return json_digest(
        {
            "repo": str(Path(repo_path).resolve()),
            "source": source_digest(repo_path, files),
            "index": _index_artifact_digest(index_root),
        }
    )


def context_cache_key(
    repo_path: str,
    request: Any,
    index_root: Optional[Path] = None,
    files: Optional[Iterable[str]] = None,
) -> str:
    """Return a stable cache key for a request and its current source digest."""
    return json_digest(
        {
            "request": request,
            "source": source_digest(repo_path, files),
            "index": _index_artifact_digest(index_root),
        }
    )


def make_citation(
    source: str,
    path: str = "",
    line: int = 0,
    end_line: Optional[int] = None,
    digest: str = "",
    role: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a stable source citation record for context and trace receipts."""
    start = max(0, int(line or 0))
    end = start if end_line is None else max(start, int(end_line))
    identity = json_digest(
        {
            "source": str(source),
            "path": str(path).replace("\\", "/"),
            "line": start,
            "end_line": end,
            "digest": str(digest),
            "role": str(role),
            "metadata": dict(metadata or {}),
        }
    )[:20]
    record: Dict[str, Any] = {
        "id": f"ctx:{source}:{identity}",
        "source": str(source),
        "path": str(path).replace("\\", "/"),
        "line": start,
        "end_line": end,
        "digest": str(digest),
    }
    if role:
        record["role"] = str(role)
    if metadata:
        record["metadata"] = dict(metadata)
    return record


def load_code_graph(
    repo_path: str,
    index_root: Optional[Path] = None,
    *,
    receipt: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Load or build the Neo code graph, reusing the persisted index.

    With an explicit ``index_root`` the caller's location is used verbatim.
    Without one, the code graph's OWN persistent per-repository root is used
    (memory.code_graph's default, outside the repository), which is what makes
    the tree-sitter parse a CONTENT-DIGEST cache rather than a per-call
    rebuild: ``load_or_build`` reuses the stored index when every indexed
    file's SHA-256 still matches and re-parses otherwise.

    The previous behavior built into a throwaway temp directory on every call,
    which forced a full reparse per retrieval and defeated the digest cache
    entirely. Nothing about the never-mutate guarantee changes: the index root
    is harness-owned and lives outside the repository.

    ``receipt`` (keyword-only, R2-09) receives the index's own provenance when
    supplied: ``digest_source`` (``content``/``stat``/``mixed``), how many
    digests were computed versus reused, the per-pass wall clock, and the
    recorded blind spot. That is what lets a reader of a ranking know whether
    the index it came from was content-verified or stat-reused. The mapping is
    cleared first, so a failed load reports an empty receipt rather than a stale
    one. Never raises.
    """
    if not str(repo_path or "").strip():
        return None
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not root.is_dir():
        return None
    try:
        from harness.deps import get_code_graph_factory

        factory = get_code_graph_factory()
    except Exception:
        factory = None
    if factory is None:
        return None
    try:
        if index_root is None:
            code_graph = factory(str(root))
        else:
            code_graph = factory(str(root), root=str(index_root))
        graph = code_graph.load_or_build()
    except Exception:
        if receipt is not None:
            receipt.clear()
            receipt["index_error"] = "code graph load failed"
        return None
    if receipt is not None:
        receipt.clear()
        try:
            freshness = dict(getattr(code_graph, "freshness_receipt", {}) or {})
        except Exception:
            freshness = {}
        for key in (
            "digest_source",
            "content_digested",
            "stat_reused",
            "files_seen",
            "walk_s",
            "stat_blind_spot",
        ):
            if key in freshness:
                receipt[key] = freshness[key]
    return graph


def _node_id(info: Any) -> str:
    """Read a graph node id across old and collision-safe node records."""
    return str(getattr(info, "node_id", "") or getattr(info, "qualified", ""))


def _symbol_records(graph: Any) -> Dict[str, Any]:
    """Return symbol nodes keyed by both graph id and legacy qualified id."""
    records: Dict[str, Any] = {}
    for node_id, info in (getattr(graph, "nodes", {}) or {}).items():
        if getattr(info, "kind", "") not in ("func", "class", "method"):
            continue
        actual = _node_id(info)
        records[actual] = info
        records.setdefault(str(node_id), info)
        records.setdefault(f"{info.kind}:{info.qualified}", info)
    return records


def _pagerank(
    node_ids: Sequence[str],
    edges: Mapping[str, Mapping[str, float]],
    iterations: int = 30,
    damping: float = 0.85,
    *,
    deadline_s: Optional[float] = None,
) -> Dict[str, float]:
    """Compute deterministic weighted PageRank over a small symbol graph.

    Two performance properties, both measured (R2-09):

    * **O(V + E) per iteration instead of O(V^2).** The historical body
      recomputed ``sum(outgoing.values())`` for every (target, source) pair,
      which is quadratic in the vertex count. On a synthetic graph of the size
      this repository actually has it measured 3.90 s at 400 vertices, 17.75 s
      at 800, 69.16 s at 1,600 and 298.17 s at 3,200 — a clean quadratic. The
      out-totals are now computed once per pass; the arithmetic per edge is
      unchanged, so the ranks are the same numbers.
    * **A deadline.** When ``deadline_s`` (an absolute ``time.monotonic()``
      value) passes, the iteration stops and the CURRENT normalized ranks are
      returned. That is a truncated answer, not a converged one, so the caller
      must publish it as truncated (see :func:`sparse_pagerank`).

    Assumes ``node_ids`` is the complete vertex set of ``edges``' subgraph and
    that ``edges`` maps a node to its outgoing neighbours. Never raises.
    """
    count = len(node_ids)
    if not count:
        return {}
    rank = {node_id: 1.0 / count for node_id in node_ids}
    damping = min(0.99, max(0.0, float(damping)))
    out_totals = {
        node_id: sum((edges.get(node_id) or {}).values()) for node_id in node_ids
    }
    # Reverse adjacency, so one iteration touches every edge once.
    incoming_edges: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for source in node_ids:
        outgoing = edges.get(source) or {}
        total = out_totals.get(source, 0.0)
        if not total:
            continue
        for target, weight in outgoing.items():
            if target in rank:
                incoming_edges[target].append((source, weight / total))
    teleport = (1.0 - damping) / count
    for _ in range(max(1, int(iterations))):
        if deadline_s is not None and time.monotonic() >= deadline_s:
            break
        dangling = sum(
            rank[node_id] for node_id in node_ids if not out_totals.get(node_id)
        )
        updated: Dict[str, float] = {}
        for node_id in node_ids:
            incoming = 0.0
            for source, share in incoming_edges.get(node_id, ()):  # type: ignore[arg-type]
                incoming += rank[source] * share
            updated[node_id] = teleport + damping * (incoming + dangling / count)
        total_rank = sum(updated.values()) or 1.0
        rank = {node_id: value / total_rank for node_id, value in updated.items()}
    return rank


def sparse_pagerank(
    node_ids: Sequence[str],
    edges: Mapping[str, Mapping[str, float]],
    seeds: Sequence[str],
    *,
    frontier: int = 0,
    iterations: int = 30,
    damping: float = 0.85,
    deadline_s: Optional[float] = None,
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Rank only the candidate subgraph reachable from ``seeds``.

    Seed-and-restrict: a query does not need the whole graph ranked, it needs
    the neighbourhood of what it already knows is relevant. This walks outward
    from the seeds over call edges, so the iteration cost is proportional to
    the candidate subgraph rather than to the repository.

    ``frontier`` bounds that subgraph (``0``/``None`` = unbounded, i.e. the
    historical whole-graph behaviour). ``deadline_s`` bounds the iteration.

    Returns ``(ranks, receipt)``. The receipt ALWAYS says which path ran, so a
    reader can tell a converged full-graph answer from a bounded one:
    ``mode`` is ``"sparse"`` or ``"full"``, ``restricted`` is True when the
    candidate set is smaller than the vertex set, ``truncated`` is True when
    the frontier or the deadline cut the work short, and
    ``not_searched`` carries the counts of what was not ranked. Never raises.
    """
    vertices = [node_id for node_id in node_ids]
    universe = set(vertices)
    wanted: Set[str] = set()
    for seed in seeds:
        if seed in universe:
            wanted.add(seed)
    receipt: Dict[str, Any] = {
        "mode": "full",
        "restricted": False,
        "truncated": False,
        "frontier": 0,
        "iterations": 0,
        "iterations_bounded_by": "",
        "vertices_total": len(vertices),
        "vertices_ranked": 0,
        "seeds": len(wanted),
        "seeds_dropped": len([s for s in seeds if s not in universe]),
        "not_searched": 0,
        "not_searched_files": [],
        "elapsed_s": 0.0,
    }
    if not vertices:
        return {}, receipt
    limit = 0
    try:
        limit = max(0, int(frontier or 0))
    except (TypeError, ValueError):
        limit = 0

    if limit:
        # Breadth-first over call edges, nearest seeds first, deterministic in
        # (node id) order so the same query always ranks the same way.
        adjacency: Dict[str, Set[str]] = defaultdict(set)
        for source, outgoing in (edges or {}).items():
            if source not in universe:
                continue
            for target in outgoing or {}:
                if target in universe:
                    adjacency[source].add(target)
        ordered_seeds = sorted(wanted)
        frontier_set: Set[str] = set()
        queue: List[str] = list(ordered_seeds)
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            if node in frontier_set:
                continue
            frontier_set.add(node)
            if len(frontier_set) >= limit:
                break
            for target in sorted(adjacency.get(node, ())):
                if target not in frontier_set:
                    queue.append(target)
        wanted = frontier_set

    if not wanted:
        # A query that matched nothing: rank nothing, say so.
        receipt["mode"] = "sparse"
        receipt["not_searched"] = len(vertices)
        return {}, receipt

    candidate = sorted(wanted)
    started = time.monotonic()
    ranks = _pagerank(
        candidate,
        edges,
        iterations=iterations,
        damping=damping,
        deadline_s=deadline_s,
    )
    receipt["elapsed_s"] = round(time.monotonic() - started, 6)
    receipt["vertices_ranked"] = len(candidate)
    receipt["iterations"] = min(30, max(1, int(iterations)))
    if limit:
        receipt["frontier"] = limit
    # `mode` and `restricted` describe the RESULT, not the argument: a bounded
    # walk that happened to reach every vertex produced a whole-graph answer,
    # and an unbounded walk whose seeds reach only part of the graph produced a
    # partial one. Reporting the latter is the point: a query no longer pays
    # for a whole-graph iteration, so a reader must be told the ranking is
    # restricted rather than shown as a complete centrality ordering.
    excluded = len(vertices) - len(candidate)
    receipt["restricted"] = excluded > 0
    receipt["truncated"] = excluded > 0
    receipt["mode"] = "full" if excluded == 0 else "sparse"
    if excluded > 0:
        receipt["not_searched"] = excluded
        files = sorted(
            {
                str(node_id).split(":", 1)[-1]
                for node_id in vertices
                if node_id not in wanted
            }
        )
        receipt["not_searched_files"] = files[:20]
    return ranks, receipt


def _relevance_score(
    info: Any,
    terms: Sequence[str],
    selected_files: Set[str],
    changed_files: Set[str],
    target_test: str,
) -> float:
    """Score lexical, path, selected-file, and changed-symbol relevance."""
    name = str(getattr(info, "name", "")).lower()
    qualified = str(getattr(info, "qualified", "")).lower()
    doc = str(getattr(info, "docstring", "")).lower()
    file = str(getattr(info, "file", "")).replace("\\", "/")
    words = _subwords(name) | _subwords(qualified)
    score = 0.0
    for term in terms:
        low = term.lower().strip("./-")
        if not low:
            continue
        if low == name or low in qualified:
            score += 5.0
        elif low in doc:
            score += 2.0
        elif low in file:
            score += 1.5
        elif words and _subwords(low) & words:
            score += 2.5
    if file in selected_files:
        score += 4.0
    if file in changed_files:
        score += 8.0
    if target_test and file and file in target_test.replace("\\", "/"):
        score += 3.0
    return score


def rank_symbols(
    repo_path: str,
    terms: Optional[Sequence[str]] = None,
    target_test: Optional[str] = None,
    selected_files: Optional[Sequence[str]] = None,
    changed_files: Optional[Sequence[str]] = None,
    changed_symbols: Optional[Sequence[str]] = None,
    limit: int = 30,
    index_root: Optional[Path] = None,
    iterations: int = 30,
    damping: float = 0.85,
    *,
    frontier: int = 0,
    budget_s: Optional[float] = None,
    receipt: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return graph-ranked symbols with PageRank and explicit relevance signals.

    The ranking is structural and lexical only. It does not use embeddings or
    semantic similarity, and a missing graph returns an empty list rather than
    raising into a task run.

    ``frontier`` (0 = whole graph, the historical behaviour) restricts the
    PageRank to the candidate subgraph reachable from the query's seeds, and
    ``budget_s`` bounds the whole call. When either one cuts the work short the
    returned rows are a partial answer; pass a ``receipt`` dict to receive
    ``{"truncated", "not_searched", ...}`` so a caller can say so instead of
    presenting a bounded ranking as a complete one.
    """
    ranked, published = rank_symbols_with_receipt(
        repo_path,
        terms=terms,
        target_test=target_test,
        selected_files=selected_files,
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        limit=limit,
        index_root=index_root,
        iterations=iterations,
        damping=damping,
        frontier=frontier,
        budget_s=budget_s,
    )
    if receipt is not None:
        receipt.clear()
        receipt.update(published)
    return ranked


def rank_symbols_with_receipt(
    repo_path: str,
    terms: Optional[Sequence[str]] = None,
    target_test: Optional[str] = None,
    selected_files: Optional[Sequence[str]] = None,
    changed_files: Optional[Sequence[str]] = None,
    changed_symbols: Optional[Sequence[str]] = None,
    limit: int = 30,
    index_root: Optional[Path] = None,
    iterations: int = 30,
    damping: float = 0.85,
    *,
    frontier: int = 0,
    budget_s: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rank symbols and return ``(rows, receipt)`` — the truncation-aware form.

    The receipt is always populated, including on the paths that return
    nothing, because "no rows" and "no rows because the budget ran out" are
    different answers. Keys: ``truncated`` (bool), ``not_searched`` (int),
    ``not_searched_files`` (list), ``mode`` (``full`` | ``sparse``),
    ``restricted`` (bool), ``frontier``, ``vertices_total``,
    ``vertices_ranked``, ``seeds``, ``elapsed_s``, ``budget_s``,
    ``budget_exhausted`` (bool), ``source_digest``, ``digest_source``
    (``content`` | ``stat``), and ``error`` when a stage failed.

    Never raises for a missing/broken index, an unreadable repository, or an
    exhausted budget: it degrades and records why.
    """
    started = time.monotonic()
    budget = None
    if budget_s is not None:
        try:
            budget = max(0.0, float(budget_s))
        except (TypeError, ValueError):
            budget = None
    receipt: Dict[str, Any] = {
        "truncated": False,
        "truncation": TRUNCATION_COMPLETE,
        "not_searched": 0,
        "not_searched_files": [],
        "mode": "full",
        "restricted": False,
        "frontier": 0,
        "vertices_total": 0,
        "vertices_ranked": 0,
        "seeds": 0,
        "elapsed_s": 0.0,
        "budget_s": budget if budget is not None else 0.0,
        "budget_exhausted": False,
        "digest_source": "",
        "error": "",
    }
    deadline = (started + budget) if budget is not None else None

    graph = load_code_graph(repo_path, index_root, receipt=receipt)
    if graph is None:
        receipt["elapsed_s"] = round(time.monotonic() - started, 6)
        receipt["error"] = receipt.get("index_error") or "code graph unavailable"
        return [], receipt
    records = _symbol_records(graph)
    symbol_ids = sorted({_node_id(info) for info in records.values()})
    receipt["vertices_total"] = len(symbol_ids)
    if not symbol_ids:
        receipt["elapsed_s"] = round(time.monotonic() - started, 6)
        return [], receipt
    edges: Dict[str, Dict[str, float]] = defaultdict(dict)
    for source, target in getattr(graph, "calls", set()) or set():
        source_info = records.get(source)
        target_info = records.get(target)
        if source_info is None or target_info is None:
            continue
        source_id = _node_id(source_info)
        target_id = _node_id(target_info)
        if source_id == target_id:
            continue
        edges[source_id][target_id] = edges[source_id].get(target_id, 0.0) + 1.0
    file_symbols: Dict[str, List[str]] = defaultdict(list)
    for node_id in symbol_ids:
        file_symbols[str(records[node_id].file).replace("\\", "/")].append(node_id)
    for source, target in getattr(graph, "imports", set()) or set():
        source_info = (getattr(graph, "nodes", {}) or {}).get(source)
        target_info = (getattr(graph, "nodes", {}) or {}).get(target)
        if source_info is None or target_info is None:
            continue
        source_file = str(source_info.file).replace("\\", "/")
        target_file = str(target_info.file).replace("\\", "/")
        for source_id in file_symbols.get(source_file, [])[:25]:
            for target_id in file_symbols.get(target_file, [])[:25]:
                if source_id != target_id:
                    edges[source_id][target_id] = (
                        edges[source_id].get(target_id, 0.0) + 0.15
                    )
    selected = {
        _strict_relative(str(value))
        for value in (selected_files or [])
        if _strict_relative(str(value))
    }
    changed = {
        _strict_relative(str(value))
        for value in (changed_files or [])
        if _strict_relative(str(value))
    }
    term_values = [str(value) for value in (terms or []) if str(value).strip()]
    changed_names = {str(value).lower() for value in (changed_symbols or [])}

    # Seed the candidate subgraph from the query's own evidence: the terms,
    # the target test, and the files the caller already selected or changed.
    seeded: List[Tuple[float, str]] = []
    for node_id in symbol_ids:
        info = records[node_id]
        score = _relevance_score(
            info, term_values, selected, changed, target_test or ""
        )
        if (
            str(info.qualified).lower() in changed_names
            or str(info.name).lower() in changed_names
        ):
            score += 12.0
        if score > 0.0:
            seeded.append((score, node_id))
    seeded.sort(key=lambda item: (-item[0], item[1]))
    seeds = [node_id for _score, node_id in seeded]
    receipt["seeds"] = len(seeds)
    if not seeds:
        # Nothing matched lexically or structurally. Ranking the whole graph
        # anyway is how a query with no anchors costs a full iteration for a
        # result nobody asked for; rank the seed set only.
        seeds = symbol_ids

    pagerank, ranking_receipt = sparse_pagerank(
        symbol_ids,
        edges,
        seeds,
        frontier=frontier,
        iterations=iterations,
        damping=damping,
        deadline_s=deadline,
    )
    receipt.update(
        {
            "mode": ranking_receipt.get("mode", "full"),
            "restricted": bool(ranking_receipt.get("restricted")),
            # The frontier's own truncation is the ranking's truncation: a
            # bounded candidate set means the rows are a partial answer.
            "truncated": bool(ranking_receipt.get("truncated")),
            "frontier": int(ranking_receipt.get("frontier") or 0),
            "vertices_ranked": int(ranking_receipt.get("vertices_ranked") or 0),
            "not_searched": int(ranking_receipt.get("not_searched") or 0),
            "not_searched_files": list(ranking_receipt.get("not_searched_files") or []),
        }
    )
    if deadline is not None and time.monotonic() >= deadline:
        receipt["truncated"] = True
        receipt["truncation"] = TRUNCATION_BUDGET
        receipt["budget_exhausted"] = True
    elif receipt["truncated"] and not receipt.get("truncation"):
        receipt["truncation"] = (
            TRUNCATION_FRONTIER if receipt.get("frontier") else TRUNCATION_SPARSE
        )
    normalized = max(pagerank.values(), default=1.0) or 1.0
    ranked: List[Tuple[float, str, Dict[str, Any]]] = []
    # Only the ranked candidate set is scored; a restricted ranking must not
    # silently fall back to reporting the whole graph.
    for node_id in sorted(pagerank):
        info = records[node_id]
        relevance = _relevance_score(
            info, term_values, selected, changed, target_test or ""
        )
        if (
            str(info.qualified).lower() in changed_names
            or str(info.name).lower() in changed_names
        ):
            relevance += 12.0
        outgoing = edges.get(node_id, {})
        incoming = sum(1.0 for source in pagerank if node_id in edges.get(source, {}))
        centrality = pagerank.get(node_id, 0.0) / normalized
        score = centrality * 4.0 + relevance + math.log1p(len(outgoing)) * 0.35
        file = str(info.file).replace("\\", "/")
        safe_path = _safe_source_path(repo_path, file)
        record = {
            "id": node_id,
            "node_id": node_id,
            "name": str(info.name),
            "qualified": str(info.qualified),
            "kind": str(info.kind),
            "file": file,
            "line": int(getattr(info, "line", 0) or 0),
            "end_line": int(getattr(info, "end_line", 0) or 0),
            "score": round(float(score), 8),
            "pagerank": round(float(centrality), 8),
            "relevance": round(float(relevance), 8),
            "in_degree": int(incoming),
            "out_degree": len(outgoing),
            "citation": make_citation(
                "symbol",
                file,
                int(getattr(info, "line", 0) or 0),
                int(getattr(info, "end_line", 0) or 0) or None,
                file_digest(safe_path) if safe_path else "",
                "repository_map",
                {"qualified": str(info.qualified)},
            ),
        }
        ranked.append((-score, file, record))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]["line"], item[2]["id"]))
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        count = 30
    rows = [item[2] for item in ranked[:count]]
    receipt["elapsed_s"] = round(time.monotonic() - started, 6)
    # The requested top-k is NOT a truncation: the caller asked for `count`
    # rows and got them. `truncated` is reserved for work that was cut short
    # by a bound (frontier, deadline), so it stays meaningful.
    receipt["ranked_total"] = len(ranked)
    receipt["returned"] = len(rows)
    receipt["top_k"] = count
    return rows, receipt


def rank_repository_map(
    repo_path: str,
    issue_text: str = "",
    target_test: Optional[str] = None,
    selected_files: Optional[Sequence[str]] = None,
    changed_files: Optional[Sequence[str]] = None,
    changed_symbols: Optional[Sequence[str]] = None,
    limit: int = 30,
    index_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a serializable Aider-style repository map from graph evidence."""
    terms = extract_terms(issue_text)
    symbols = rank_symbols(
        repo_path,
        terms=terms,
        target_test=target_test,
        selected_files=selected_files,
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        limit=limit,
        index_root=index_root,
    )
    file_scores: Dict[str, float] = defaultdict(float)
    for record in symbols:
        file_scores[record["file"]] += float(record["score"])
    files = [
        {"path": path, "score": round(float(score), 8)}
        for path, score in sorted(
            file_scores.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return {
        "strategy": "pagerank+lexical",
        "terms": terms,
        "symbols": symbols,
        "files": files,
        "index_digest": _index_artifact_digest(index_root),
        "source_digest": source_digest(repo_path),
    }


def _source_record(
    repo_path: str,
    rel_path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    source: str = "file",
    role: str = "selected_file",
    include_source: bool = True,
) -> Optional[Dict[str, Any]]:
    """Read one contained source range and attach a stable citation."""
    relative = _strict_relative(rel_path)
    path = _safe_source_path(repo_path, relative) if relative else None
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        if lines and start > len(lines):
            return None
        end = start if end_line is None else max(start, int(end_line))
        end = min(end, len(lines)) if lines else start
        selected = "\n".join(lines[start - 1 : end])
    except (OSError, TypeError, ValueError):
        return None
    digest = file_digest(path)
    citation = make_citation(
        source,
        relative,
        start,
        end,
        digest,
        role,
    )
    record: Dict[str, Any] = {
        "file": relative,
        "path": relative,
        "line": start,
        "end_line": end,
        "text": selected if include_source else "",
        "source": selected if include_source else "",
        "digest": digest,
        "citation": citation,
    }
    return record


def retrieve_range(
    repo_path: str,
    rel_path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    include_source: bool = True,
) -> List[Dict[str, Any]]:
    """Retrieve one exact, contained file range with a citation record."""
    record = _source_record(
        repo_path,
        rel_path,
        start_line=start_line,
        end_line=end_line,
        source="range",
        role="selected_file",
        include_source=include_source,
    )
    return [record] if record else []


def retrieve_symbol(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    context_lines: int = 0,
    include_source: bool = True,
    index_root: Optional[Path] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Retrieve exact indexed symbol ranges, optionally with surrounding lines."""
    graph = load_code_graph(repo_path, index_root)
    if graph is None:
        return []
    try:
        from memory.code_graph import find_in_graph

        matches = [
            info
            for info in find_in_graph(str(symbol), graph)
            if (
                str(getattr(info, "kind", "")) in ("func", "class", "method")
                and (
                    str(getattr(info, "name", "")) == str(symbol)
                    or str(getattr(info, "qualified", "")) == str(symbol)
                )
            )
        ]
    except Exception:
        matches = []
    if target_file:
        normalized = _strict_relative(target_file)
        matches = [
            info for info in matches if str(info.file).replace("\\", "/") == normalized
        ]
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = 20
    if cap == 0:
        return []
    out: List[Dict[str, Any]] = []
    for info in sorted(matches, key=lambda item: (item.file, item.line, item.node_id))[
        :cap
    ]:
        line = int(
            start_line
            if start_line is not None
            else max(1, info.line - max(0, int(context_lines)))
        )
        finish = int(
            end_line
            if end_line is not None
            else info.end_line + max(0, int(context_lines))
        )
        record = _source_record(
            repo_path,
            str(info.file),
            line,
            finish,
            source="symbol",
            role="selected_symbol",
            include_source=include_source,
        )
        if record is None:
            continue
        record.update(
            {
                "id": info.node_id,
                "node_id": info.node_id,
                "name": info.name,
                "qualified": info.qualified,
                "kind": info.kind,
                "symbol_line": int(info.line),
                "symbol_end_line": int(info.end_line),
            }
        )
        out.append(record)
    return out


def retrieve_exact_symbol(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    include_source: bool = True,
    index_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return exact symbol source ranges for a simple or qualified name."""
    return retrieve_symbol(
        repo_path,
        symbol,
        target_file=target_file,
        start_line=start_line,
        end_line=end_line,
        include_source=include_source,
        index_root=index_root,
    )


def retrieve_symbol_range(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    include_source: bool = True,
    index_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Compatibility alias for exact symbol-range retrieval."""
    return retrieve_exact_symbol(
        repo_path,
        symbol,
        target_file=target_file,
        start_line=start_line,
        end_line=end_line,
        include_source=include_source,
        index_root=index_root,
    )


def get_symbol_context(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    include_source: bool = True,
    index_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return the first exact symbol record, or an honest empty record."""
    records = retrieve_exact_symbol(
        repo_path,
        symbol,
        target_file=target_file,
        include_source=include_source,
        index_root=index_root,
    )
    return records[0] if records else {}


def changed_symbol_context(
    repo_path: str,
    changed_files: Optional[Sequence[str]] = None,
    changed_symbols: Optional[Sequence[str]] = None,
    target_test: Optional[str] = None,
    index_root: Optional[Path] = None,
    include_source: bool = True,
    limit: int = 30,
) -> Dict[str, Any]:
    """Return changed symbols with direct callers, callees, and import dependents."""
    graph = load_code_graph(repo_path, index_root)
    if graph is None:
        return {
            "changed": [],
            "callers": [],
            "callees": [],
            "importers": [],
            "citations": [],
            "index_digest": "unavailable",
        }
    records = _symbol_records(graph)
    changed_paths = {
        _strict_relative(str(value))
        for value in (changed_files or [])
        if _strict_relative(str(value))
    }
    names = {str(value).lower() for value in (changed_symbols or [])}
    roots: Dict[str, Any] = {}
    for _key, info in records.items():
        file = str(info.file).replace("\\", "/")
        if (
            file in changed_paths
            or str(info.qualified).lower() in names
            or str(info.name).lower() in names
        ):
            roots[_node_id(info)] = info
    if target_test:
        target_path = (
            str(target_test).split("::", 1)[0].split(" - ", 1)[0].replace("\\", "/")
        )
        for _key, info in records.items():
            if str(info.file).replace("\\", "/") == _strict_relative(target_path):
                roots.setdefault(_node_id(info), info)

    calls = getattr(graph, "calls", set()) or set()
    imports = getattr(graph, "imports", set()) or set()
    callers: Dict[str, Any] = {}
    callees: Dict[str, Any] = {}
    importers: Dict[str, Any] = {}
    for root_id in roots:
        for source, target in calls:
            if target == root_id and source in records:
                callers[_node_id(records[source])] = records[source]
            if source == root_id and target in records:
                callees[_node_id(records[target])] = records[target]
    root_files = {str(info.file) for info in roots.values()}
    root_modules = {
        node_id
        for node_id, info in (getattr(graph, "nodes", {}) or {}).items()
        if getattr(info, "kind", "") == "module" and str(info.file) in root_files
    }
    for source, target in imports:
        if target in root_modules and source in (getattr(graph, "nodes", {}) or {}):
            info = graph.nodes[source]
            importers[_node_id(info)] = info

    def info_record(info: Any) -> Dict[str, Any]:
        file = str(info.file).replace("\\", "/")
        citation = make_citation(
            "symbol",
            file,
            int(getattr(info, "line", 0) or 0),
            int(getattr(info, "end_line", 0) or 0) or None,
            file_digest(_safe_source_path(repo_path, file))
            if _safe_source_path(repo_path, file)
            else "",
            "dependency_context",
            {"qualified": str(info.qualified)},
        )
        record = {
            "id": _node_id(info),
            "node_id": _node_id(info),
            "name": str(info.name),
            "qualified": str(info.qualified),
            "kind": str(info.kind),
            "file": file,
            "line": int(getattr(info, "line", 0) or 0),
            "end_line": int(getattr(info, "end_line", 0) or 0),
            "citation": citation,
        }
        if include_source:
            source_record = _source_record(
                repo_path,
                file,
                int(getattr(info, "line", 0) or 0),
                int(getattr(info, "end_line", 0) or 0) or None,
                source="symbol",
                role="dependency_context",
            )
            if source_record:
                record["text"] = source_record["text"]
        return record

    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 30

    def ordered(values: Mapping[str, Any]) -> List[Dict[str, Any]]:
        return [
            info_record(info)
            for info in sorted(
                values.values(),
                key=lambda item: (
                    str(item.file),
                    int(getattr(item, "line", 0) or 0),
                    _node_id(item),
                ),
            )
        ]

    changed_records = ordered(roots)[:cap]
    caller_records = ordered(callers)[:cap]
    callee_records = ordered(callees)[:cap]
    importer_records = ordered(importers)[:cap]
    citations = [
        record["citation"]
        for record in (
            *changed_records,
            *caller_records,
            *callee_records,
            *importer_records,
        )
    ]
    return {
        "changed": changed_records,
        "callers": caller_records,
        "callees": callee_records,
        "importers": importer_records,
        "citations": citations,
        "index_digest": _index_artifact_digest(index_root),
    }


def blast_radius(
    repo_path: str,
    changed_files: Optional[Sequence[str]] = None,
    changed_symbols: Optional[Sequence[str]] = None,
    index_root: Optional[Path] = None,
    limit: int = 30,
) -> Dict[str, Any]:
    """Return direct structural dependency context for changed code."""
    return changed_symbol_context(
        repo_path,
        changed_files=changed_files,
        changed_symbols=changed_symbols,
        index_root=index_root,
        limit=limit,
    )


def pagerank_symbols(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Compatibility alias for weighted PageRank symbol ranking."""
    return rank_symbols(*args, **kwargs)


def weighted_symbol_ranking(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Compatibility alias for weighted symbol ranking."""
    return rank_symbols(*args, **kwargs)


def repository_map(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Compatibility alias for the serializable repository map."""
    return rank_repository_map(*args, **kwargs)


def _resolve_symbol_nodes(
    graph: Any, symbol: str, target_file: Optional[str] = None
) -> List[Any]:
    """Return every graph node that defines ``symbol`` under any of its names.

    A symbol may be a bare name, a dotted qualified name, or ambiguous across
    several definitions. All matches are returned (bounded) so a caller can
    report the ambiguity instead of silently picking one.
    """
    if graph is None:
        return []
    needle = str(symbol or "").strip()
    if not needle:
        return []
    target = _strict_relative(target_file) if target_file else ""
    found: Dict[str, Any] = {}
    for info in (getattr(graph, "nodes", {}) or {}).values():
        if getattr(info, "kind", "") not in ("func", "class", "method"):
            continue
        if needle not in (
            str(getattr(info, "name", "")),
            str(getattr(info, "qualified", "")),
        ):
            continue
        file = str(getattr(info, "file", "")).replace("\\", "/")
        if target and file != target:
            continue
        found[_node_id(info)] = info
    return sorted(
        found.values(),
        key=lambda item: (
            str(getattr(item, "file", "")),
            int(getattr(item, "line", 0) or 0),
            _node_id(item),
        ),
    )


def _definition_records(
    repo_path: str, nodes: Sequence[Any], include_source: bool = True
) -> List[Dict[str, Any]]:
    """Render definition nodes as cited, bounded source records."""
    records: List[Dict[str, Any]] = []
    for info in nodes:
        file = str(getattr(info, "file", "")).replace("\\", "/")
        start = int(getattr(info, "line", 0) or 0)
        end = int(getattr(info, "end_line", 0) or 0) or None
        record = _source_record(
            repo_path,
            file,
            start_line=start or 1,
            end_line=end,
            source="symbol",
            role="symbol_definition",
            include_source=include_source,
        )
        if record is None:
            continue
        record.update(
            {
                "id": _node_id(info),
                "node_id": _node_id(info),
                "name": str(getattr(info, "name", "")),
                "qualified": str(getattr(info, "qualified", "")),
                "kind": str(getattr(info, "kind", "")),
                "file": file,
            }
        )
        records.append(record)
    return records


def find_definitions(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    *,
    include_source: bool = True,
    index_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return every indexed definition of ``symbol`` plus an ambiguity flag.

    Assumes a repository that :func:`load_code_graph` can open. An unavailable
    index returns ``available: False`` and an empty list, which a caller must
    report honestly rather than as "the symbol does not exist".
    """
    graph = load_code_graph(repo_path, index_root)
    if graph is None:
        return {
            "available": False,
            "definitions": [],
            "ambiguous": False,
            "index_digest": "unavailable",
        }
    nodes = _resolve_symbol_nodes(graph, symbol, target_file)
    records = _definition_records(repo_path, nodes, include_source=include_source)
    return {
        "available": True,
        "definitions": records,
        "ambiguous": len(records) > 1,
        "index_digest": _index_artifact_digest(index_root),
    }


def find_references(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    *,
    include_source: bool = True,
    index_root: Optional[Path] = None,
    limit: int = 60,
) -> Dict[str, Any]:
    """Return the call sites and importers that depend on ``symbol``.

    A reference is a call edge pointing at one of the symbol's definitions, or
    an import edge pointing at a module that defines it. Because resolution is
    name-based, this is an over-approximation: two same-named symbols in
    different modules share one reference set. The result reports
    ``resolution: "name_based"`` so a consumer never mistakes it for precise
    type resolution.
    """
    graph = load_code_graph(repo_path, index_root)
    empty: Dict[str, Any] = {
        "available": False,
        "definitions": [],
        "references": [],
        "definition_count": 0,
        "index_digest": "unavailable",
        "resolution": "unavailable",
    }
    if graph is None:
        return empty
    nodes = _resolve_symbol_nodes(graph, symbol, target_file)
    if not nodes:
        return {
            **empty,
            "available": True,
            "index_digest": _index_artifact_digest(index_root),
        }
    node_ids = {_node_id(info) for info in nodes}
    records = _symbol_records(graph)
    alias_ids = {key for key, info in records.items() if _node_id(info) in node_ids}
    reference_nodes: Dict[str, Any] = {}
    for source, target in getattr(graph, "calls", set()) or set():
        if (target in node_ids or target in alias_ids) and source in records:
            reference_nodes[_node_id(records[source])] = records[source]
    defining_files = {str(getattr(info, "file", "")) for info in nodes}
    defining_modules = {
        _node_id(info)
        for info in (getattr(graph, "nodes", {}) or {}).values()
        if getattr(info, "kind", "") == "module"
        and str(getattr(info, "file", "")) in defining_files
    }
    for source, target in getattr(graph, "imports", set()) or set():
        if target in defining_modules and source in (getattr(graph, "nodes", {}) or {}):
            info = graph.nodes[source]
            reference_nodes[_node_id(info)] = info
    ordered = sorted(
        reference_nodes.values(),
        key=lambda item: (
            str(getattr(item, "file", "")),
            int(getattr(item, "line", 0) or 0),
            _node_id(item),
        ),
    )[: max(1, int(limit or 60))]
    rendered: List[Dict[str, Any]] = []
    for info in ordered:
        file = str(getattr(info, "file", "")).replace("\\", "/")
        line = int(getattr(info, "line", 0) or 0)
        end = int(getattr(info, "end_line", 0) or 0) or None
        path = _safe_source_path(repo_path, file)
        digest = file_digest(path) if path else ""
        rendered.append(
            {
                "id": _node_id(info),
                "node_id": _node_id(info),
                "name": str(getattr(info, "name", "")),
                "qualified": str(getattr(info, "qualified", "")),
                "kind": str(getattr(info, "kind", "")),
                "relation": "reference",
                "file": file,
                "path": file,
                "line": line,
                "end_line": end or 0,
                "digest": digest,
                "citation": make_citation(
                    "symbol_reference",
                    file,
                    line,
                    end,
                    digest,
                    "symbol_reference",
                    {"qualified": str(getattr(info, "qualified", ""))},
                ),
            }
        )
        if include_source:
            body = _source_record(
                repo_path,
                file,
                start_line=line or 1,
                end_line=end,
                source="symbol_reference",
                role="symbol_reference",
            )
            if body:
                rendered[-1]["text"] = body["text"]
    return {
        "available": True,
        "definitions": _definition_records(
            repo_path, nodes, include_source=include_source
        ),
        "references": rendered,
        "definition_count": len(nodes),
        "index_digest": _index_artifact_digest(index_root),
        "resolution": "name_based",
    }


def read_symbol_records(
    repo_path: str,
    symbol: str,
    target_file: Optional[str] = None,
    *,
    index_root: Optional[Path] = None,
    limit: int = 400,
) -> List[Dict[str, Any]]:
    """Return the source of one symbol, bounded to ``limit`` lines.

    Assumes a warm index. ``limit`` is applied per definition, so a symbol
    with several definitions still returns each of them head-bounded rather
    than truncating the whole result to nothing.
    """
    found = find_definitions(
        repo_path, symbol, target_file, include_source=True, index_root=index_root
    )
    records: List[Dict[str, Any]] = []
    cap = max(1, int(limit or 400))
    for record in found["definitions"]:
        body = str(record.get("text") or "")
        lines = body.splitlines()
        if len(lines) > cap:
            record = dict(record)
            record["text"] = (
                "\n".join(lines[:cap]) + f"\n...[{len(lines) - cap} more lines]"
            )
            record["truncated"] = True
        records.append(record)
    return records


def read_symbol_by_search(
    repo_path: str,
    symbol: str,
    *,
    index_root: Optional[Path] = None,
    per_file: int = 2,
) -> List[Dict[str, Any]]:
    """Find a symbol's source by bounded text search when the index misses.

    This is the honest fallback for a cold or unavailable index: it looks for
    a real definition line (``def name(`` / ``class name(`` / a JS declaration)
    and returns the enclosing block's head. It is a text heuristic, so the
    record is labelled ``source="text_search"`` and a caller can tell the
    difference between an indexed definition and a searched one.
    """
    root = Path(repo_path or ".").expanduser()
    needle = str(symbol or "").strip()
    if not needle or not root.is_dir():
        return []
    escaped = re.escape(needle)
    definition = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{escaped}\b|"
        rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b|"
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\b",
        re.MULTILINE,
    )
    records: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(records) >= max(1, int(per_file or 2)) * 4:
            break
        if not path.is_file() or path.suffix.lower() not in _CODE_EXTS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > _LARGE_FILE:
                continue
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        lines = source.splitlines()
        hits = [index for index, line in enumerate(lines) if definition.search(line)]
        if not hits:
            continue
        for index in hits[: max(1, int(per_file or 2))]:
            start = index + 1
            end = min(len(lines), start + 120)
            record = _source_record(
                repo_path,
                relative,
                start_line=start,
                end_line=end,
                source="text_search",
                role="symbol_definition",
            )
            if record is not None:
                record["name"] = needle
                record["qualified"] = needle
                record["kind"] = "definition"
                records.append(record)
    return records


def blast_radius_for(
    repo_path: str,
    changed_symbols: Optional[Sequence[str]] = None,
    changed_files: Optional[Sequence[str]] = None,
    *,
    depth: int = 1,
    limit: int = 25,
    index_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return the files and symbols a change can break, walked to ``depth``.

    Depth 1 is the direct dependents (callers, callees, importers). Deeper
    walks add transitive callers, which is what a shared-signature change
    actually needs. Resolution stays name-based, so the answer is a list of
    files to CHECK; the ``resolution`` field says so in the payload rather than
    only in prose.
    """
    graph = load_code_graph(repo_path, index_root)
    if graph is None:
        return {
            "available": False,
            "symbols": [],
            "files": [],
            "depth": max(1, int(depth or 1)),
            "index_digest": "unavailable",
            "resolution": "unavailable",
        }
    records = _symbol_records(graph)
    frontier: Dict[str, Any] = {}
    for info in records.values():
        file = str(getattr(info, "file", "")).replace("\\", "/")
        for value in changed_files or []:
            if file == _strict_relative(str(value)):
                frontier[_node_id(info)] = info
                break
        if frontier.get(_node_id(info)) is info:
            continue
        for value in changed_symbols or []:
            needle = str(value)
            if needle and needle in (
                str(getattr(info, "name", "")),
                str(getattr(info, "qualified", "")),
            ):
                frontier[_node_id(info)] = info
                break
    cap = max(1, int(limit or 25))
    steps = max(1, int(depth or 1))
    collected: Dict[str, Dict[str, Any]] = {}
    seen = set(frontier)
    for _step in range(steps):
        dependents: Dict[str, Any] = {}
        for source, target in getattr(graph, "calls", set()) or set():
            if target in seen and source in records and source not in collected:
                info = records[source]
                dependents[_node_id(info)] = info
                collected[_node_id(info)] = {
                    "id": _node_id(info),
                    "name": str(getattr(info, "name", "")),
                    "qualified": str(getattr(info, "qualified", "")),
                    "kind": str(getattr(info, "kind", "")),
                    "relation": "caller",
                    "file": str(getattr(info, "file", "")).replace("\\", "/"),
                    "line": int(getattr(info, "line", 0) or 0),
                    "end_line": int(getattr(info, "end_line", 0) or 0),
                }
            if source in seen and target in records and target not in collected:
                info = records[target]
                dependents[_node_id(info)] = info
                collected[_node_id(info)] = {
                    "id": _node_id(info),
                    "name": str(getattr(info, "name", "")),
                    "qualified": str(getattr(info, "qualified", "")),
                    "kind": str(getattr(info, "kind", "")),
                    "relation": "callee",
                    "file": str(getattr(info, "file", "")).replace("\\", "/"),
                    "line": int(getattr(info, "line", 0) or 0),
                    "end_line": int(getattr(info, "end_line", 0) or 0),
                }
        frontier = dependents
        seen |= set(dependents)
        if not frontier:
            break
    modules = {
        _node_id(info)
        for info in (getattr(graph, "nodes", {}) or {}).values()
        if getattr(info, "kind", "") == "module"
        and str(getattr(info, "file", "")).replace("\\", "/")
        in {
            str(getattr(item, "file", "")).replace("\\", "/")
            for item in frontier.values()
        }
    }
    for source, target in getattr(graph, "imports", set()) or set():
        if target in modules and source in (getattr(graph, "nodes", {}) or {}):
            info = graph.nodes[source]
            if _node_id(info) in collected:
                continue
            collected[_node_id(info)] = {
                "id": _node_id(info),
                "name": str(getattr(info, "name", "")),
                "qualified": str(getattr(info, "qualified", "")),
                "kind": str(getattr(info, "kind", "")),
                "relation": "importer",
                "file": str(getattr(info, "file", "")).replace("\\", "/"),
                "line": int(getattr(info, "line", 0) or 0),
                "end_line": int(getattr(info, "end_line", 0) or 0),
            }
    ordered = sorted(
        collected.values(),
        key=lambda item: (
            str(item.get("file")),
            int(item.get("line") or 0),
            str(item.get("id")),
        ),
    )[:cap]
    for entry in ordered:
        path = _safe_source_path(repo_path, str(entry.get("file")))
        digest = file_digest(path) if path else ""
        entry["citation"] = make_citation(
            "blast_radius",
            str(entry.get("file")),
            int(entry.get("line") or 0),
            int(entry.get("end_line") or 0) or None,
            digest,
            "blast_radius",
            {"relation": entry.get("relation")},
        )
    files: Dict[str, Dict[str, Any]] = {}
    for entry in ordered:
        key = str(entry.get("file"))
        bucket = files.setdefault(key, {"file": key, "relations": [], "symbols": []})
        relation = str(entry.get("relation"))
        if relation not in bucket["relations"]:
            bucket["relations"].append(relation)
        bucket["symbols"].append(str(entry.get("qualified") or entry.get("name")))
    return {
        "available": True,
        "symbols": ordered,
        "files": sorted(files.values(), key=lambda item: str(item.get("file"))),
        "depth": steps,
        "index_digest": _index_artifact_digest(index_root),
        "resolution": "name_based",
    }


def _lexical_scores(query_terms: Sequence[str], text: str) -> float:
    """Score text by identifier-aware term overlap (no embeddings)."""
    if not text:
        return 0.0
    lowered = text.casefold()
    words = set(_IDENTIFIER_PATTERN.findall(lowered))
    subwords: Set[str] = set()
    for token in words:
        subwords.update(_subwords(token))
        if "_" in token:
            subwords.update(part for part in token.split("_") if part)
    score = 0.0
    for term in query_terms:
        needle = str(term).casefold()
        if not needle:
            continue
        if needle in lowered:
            score += 2.0
        if needle in words or needle in subwords:
            score += 1.5
        elif any(needle in part for part in subwords if len(part) > 3):
            score += 0.5
    return score


_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _hybrid_scores(query_terms: Sequence[str], text: str) -> float:
    """Score text as lexical score plus a deterministic character-ngram term.

    This stands in for an embedding arm WITHOUT calling a model or a network:
    a bounded character 3-gram overlap is the cheap, offline, deterministic
    stand-in for the "similar but not identical wording" cases lexical scoring
    misses. It exists so the hybrid arm can be MEASURED against the lexical arm
    on the same queries; it is an ablation probe, never the production path.
    """
    base = _lexical_scores(query_terms, text)
    if not text:
        return base
    grams_query: Set[str] = set()
    for term in query_terms:
        padded = f" {str(term).casefold()} "
        grams_query.update(
            padded[index : index + 3] for index in range(len(padded) - 2)
        )
    if not grams_query:
        return base
    lowered = f" {text.casefold()} "
    grams_text = {
        lowered[index : index + 3] for index in range(max(0, len(lowered) - 2))
    }
    if not grams_text:
        return base
    overlap = len(grams_query & grams_text) / len(grams_query)
    return base + 2.0 * overlap


def retrieval_ablation(
    repo_path: str,
    queries: Sequence[Any],
    expected: Optional[Mapping[str, Any]] = None,
    *,
    index_root: Optional[Path] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Measure lexical vs hybrid recall over a labeled query set.

    The production retrieval path is lexical and structural — subword,
    camelCase, and identifier decomposition over the tree-sitter index. An
    embedding arm is therefore an ABLATION: this function scores the same
    queries with the lexical scorer and with a hybrid scorer that adds a
    character-ngram similarity term, and reports recall@k for both.

    ``queries`` may be plain strings (recall is then reported as
    ``unlabeled``/``None``, because a query with no ground truth has no recall)
    or ``{"query": ..., "expect": [symbol or path, ...]}`` mappings, which is
    the labeled form the ceiling target is measured on.

    Assumes a repository :func:`load_code_graph` can open. An unavailable index
    reports ``available: False`` rather than a fabricated 0.0.
    """
    graph = load_code_graph(repo_path, index_root)
    catalog: List[Dict[str, str]] = []
    if graph is not None:
        for info in (getattr(graph, "nodes", {}) or {}).values():
            kind = str(getattr(info, "kind", ""))
            if kind not in ("func", "class", "method", "module"):
                continue
            file = str(getattr(info, "file", "")).replace("\\", "/")
            catalog.append(
                {
                    "id": _node_id(info),
                    "name": str(getattr(info, "name", "")),
                    "qualified": str(getattr(info, "qualified", "")),
                    "kind": kind,
                    "file": file,
                    "text": " ".join(
                        (
                            str(getattr(info, "name", "")),
                            str(getattr(info, "qualified", "")),
                            file,
                            str(getattr(info, "docstring", "") or ""),
                        )
                    ),
                }
            )
    report: Dict[str, Any] = {
        "available": graph is not None,
        "production_arm": "lexical_structural",
        "ablation_arm": "hybrid_ngram",
        "index_digest": _index_artifact_digest(index_root)
        if graph is not None
        else "unavailable",
        "candidate_count": len(catalog),
        "queries": [],
        "lexical_recall_at_k": None,
        "hybrid_recall_at_k": None,
        "labeled_queries": 0,
        "unlabeled_queries": 0,
        "note": (
            "recall is only computed for labeled queries; an unlabeled query "
            "reports its ranking so a human can label it later"
        ),
    }
    if graph is None:
        return report
    cap = max(1, int(limit or 10))
    lexical_hits = 0
    hybrid_hits = 0
    labeled = 0
    for raw in queries or []:
        if isinstance(raw, Mapping):
            query = str(raw.get("query") or "")
            expect = [str(item) for item in (raw.get("expect") or []) if str(item)]
        else:
            query = str(raw)
            expect = []
        terms = [term for term in extract_terms(query) if term] or [query]
        lexical = sorted(
            catalog,
            key=lambda item: (
                -_lexical_scores(terms, item["text"]),
                item["file"],
                item["id"],
            ),
        )[:cap]
        hybrid = sorted(
            catalog,
            key=lambda item: (
                -_hybrid_scores(terms, item["text"]),
                item["file"],
                item["id"],
            ),
        )[:cap]
        lexical_top = [item["id"] for item in lexical]
        hybrid_top = [item["id"] for item in hybrid]
        matched_lexical = _expected_hits(expect, lexical, catalog)
        matched_hybrid = _expected_hits(expect, hybrid, catalog)
        if expect:
            labeled += 1
            lexical_hits += 1 if matched_lexical else 0
            hybrid_hits += 1 if matched_hybrid else 0
        else:
            report["unlabeled_queries"] += 1
        report["queries"].append(
            {
                "query": query,
                "expect": expect,
                "lexical_top": lexical_top,
                "hybrid_top": hybrid_top,
                "lexical_hit": bool(matched_lexical),
                "hybrid_hit": bool(matched_hybrid),
                "agreement": sum(1 for item in lexical_top if item in hybrid_top)
                / max(1, len(lexical_top)),
            }
        )
    report["labeled_queries"] = labeled
    if labeled:
        report["lexical_recall_at_k"] = lexical_hits / labeled
        report["hybrid_recall_at_k"] = hybrid_hits / labeled
        report["hybrid_minus_lexical"] = (
            report["hybrid_recall_at_k"] - report["lexical_recall_at_k"]
        )
    return report


def _label_chain(value: str) -> Set[str]:
    """Expand a qualified label into every dotted/colon suffix of itself.

    ``pkg.core.Widget.render`` and ``Widget.render`` name the same symbol, and a
    labeled sample written by a human will use the short form. Matching only
    exact ids would report a recall miss for a query that actually ranked the
    right symbol first, which is exactly the kind of false negative that makes
    an ablation report untrustworthy.
    """
    text = str(value or "").strip()
    if not text:
        return set()
    parts = [part for part in re.split(r"[.:/]", text) if part]
    chain = {text.casefold(), parts[-1].casefold() if parts else ""}
    for index in range(1, len(parts)):
        chain.add(".".join(parts[index:]).casefold())
    return {item for item in chain if item}


def _expected_hits(
    expect: Sequence[str],
    ranked: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
) -> List[str]:
    """Return which expected labels the ranked list actually surfaced."""
    if not expect:
        return []
    by_id = {str(item["id"]): item for item in catalog}
    wanted: Set[str] = set()
    for label in expect:
        wanted |= _label_chain(str(label))
    hits: List[str] = []
    for entry in ranked:
        item = by_id.get(str(entry["id"]), {})
        labels: Set[str] = set()
        for key in ("id", "name", "qualified", "file"):
            labels |= _label_chain(str(item.get(key, "")))
        if labels & wanted:
            hits.append(str(entry["id"]))
    return hits


def context_citations(
    result: Mapping[str, Any], repo_path: str
) -> List[Dict[str, Any]]:
    """Build citations for a legacy retrieval result without changing its shape."""
    citations: List[Dict[str, Any]] = []
    for relative in result.get("files", []) if isinstance(result, Mapping) else []:
        path = _safe_source_path(repo_path, str(relative))
        citations.append(
            make_citation(
                "retrieval",
                str(relative),
                0,
                None,
                file_digest(path) if path else "",
                "retrieval",
            )
        )
    for relative, lines in (
        (result.get("greps", {}) or {}).items() if isinstance(result, Mapping) else []
    ):
        first = 0
        if lines:
            match = re.match(r".*:(\d+):", str(lines[0]))
            if match:
                first = int(match.group(1))
        path = _safe_source_path(repo_path, str(relative))
        citations.append(
            make_citation(
                "retrieval",
                str(relative),
                first,
                None,
                file_digest(path) if path else "",
                "retrieval",
            )
        )
    return citations
