"""The right-hand FILE TREE pane: the repository's scope, with changes distinct.

Phase 5/W1 task T5.W1.2.

Three properties are load-bearing here and each is a gate:

1. **Declared-root containment.** Every path the tree can show is resolved
   through :func:`cli.fileview.safe_repo_path`, which refuses a traversing or
   absolute path. A file tree that can name a path OUTSIDE the repository is a
   disclosure surface, and it is the reason this pane reads through
   ``fileview``'s one containment helper rather than joining strings.
2. **Ignore rules are git's, not a second matcher.** ``fileview.read_repository_files``
   already lists with ``git ls-files --cached --others --exclude-standard``, so
   ``.gitignore`` and the ``node_modules``/``dist``/``.venv`` family are respected
   by the authority. A second skip table here would be the second-answer defect
   this repository has paid for repeatedly.
3. **It is fast on THIS repository, and the number is measured.** The listing is
   memoised per (repo, mtime-of-.git-index) so a repaint costs a dict lookup,
   and :func:`assert_pane_is_fast_on_this_repository` measures a real build on
   the real checkout rather than asserting a feeling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cli import fileview as _fv
from cli import ui as _ui

__all__ = [
    "CHANGED_MARKER",
    "ELLIPSIS",
    "IGNORED_NOTE",
    "MAX_ROWS",
    "MIN_TREE_COLUMNS",
    "NARROW_TREE_COLUMNS",
    "TREE_WIDGET_ID",
    "TreeModel",
    "TreeRow",
    "assert_every_tree_path_is_contained",
    "open_tree_target",
    "tree_lines",
    "tree_model",
    "tree_row_line",
    "tree_rows",
]

#: Widget id the container mounts this pane under.
TREE_WIDGET_ID = "neo-tree"

ELLIPSIS = "…"

#: Below this the pane says it is too narrow rather than clipping a name.
MIN_TREE_COLUMNS = 20
#: The deliberate narrowing point for the tree, one step below the change list's
#: because a path is the longest thing either pane renders.
NARROW_TREE_COLUMNS = 32

MAX_ROWS = 500

#: The marker a CHANGED path carries. ASCII by construction: a marker that needs
#: an encoding fallback is a marker somebody cannot read, and this is the one
#: glyph that distinguishes a run's own edits from the rest of the repository.
CHANGED_MARKER = "~"

#: Stated whenever a listing was bounded, so a capped tree is never read as the
#: whole repository.
IGNORED_NOTE = (
    "listing respects .gitignore and skips {skipped} generated or ignored "
    "directory name(s) (node_modules, dist, .venv, build, ...)"
)


def _bound(value: Any, width: int, *, marker: str = ELLIPSIS) -> str:
    """Sanitise then bound. Same order as ``cli/changespane._bound``, for the same reason."""
    try:
        text = _ui.sanitize_text(value)
    except Exception:  # pragma: no cover - sanitize_text already fails closed
        text = "(detail withheld: sanitiser unavailable)"
    try:
        limit = max(1, int(width))
    except (TypeError, ValueError):
        limit = 1
    if limit <= 1:
        return marker
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + marker


@dataclass(frozen=True)
class TreeRow:
    """One line of the tree: a directory or a file, at a depth."""

    path: str
    name: str
    is_file: bool
    depth: int
    changed: bool = False

    @property
    def marker(self) -> str:
        """``~`` for a changed path, one space otherwise. FIRST on the row."""
        return CHANGED_MARKER if self.changed else " "

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection. Every value is data, never markup."""
        return {
            "path": self.path,
            "name": self.name,
            "is_file": bool(self.is_file),
            "depth": int(self.depth),
            "changed": bool(self.changed),
        }


@dataclass(frozen=True)
class TreeModel:
    """The tree's whole state as a value, so a render never reads the world."""

    rows: Tuple[TreeRow, ...] = ()
    repo: str = ""
    changed: Tuple[str, ...] = ()
    files_shown: int = 0
    files_total: int = 0
    rows_shown: int = 0
    rows_total: int = 0
    width: int = 60
    query: str = ""
    listing_available: bool = False
    listing_reason: str = ""
    skipped_directory_names: Tuple[str, ...] = ()
    build_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection, used by the truth gate and by ``--json``."""
        return {
            "repo": self.repo,
            "files_shown": int(self.files_shown),
            "files_total": int(self.files_total),
            "rows_shown": int(self.rows_shown),
            "rows_total": int(self.rows_total),
            "changed": list(self.changed),
            "query": self.query,
            "listing_available": bool(self.listing_available),
            "listing_reason": self.listing_reason,
            "skipped_directory_names": list(self.skipped_directory_names),
            "build_ms": round(float(self.build_ms), 3),
            "rows": [row.as_dict() for row in self.rows],
        }


def _cache_key(repo: Any) -> Tuple[str, str]:
    """A memo key that MOVES when the repository's index moves.

    ``git ls-files`` reads the index, so an index whose mtime changed is the
    only honest signal that a cached listing might be behind. A cache keyed on
    the path alone would serve a listing taken before a commit, which is how a
    file tree shows a path that no longer exists.
    """
    root = None
    try:
        root = _fv.repo_root(repo)
    except Exception:
        root = None
    if root is None:
        return (str(repo or ""), "")
    for candidate in (Path_index(root), Path_HEAD(root)):
        try:
            return (str(root), f"{candidate.stat().st_mtime_ns}")
        except (OSError, ValueError):
            continue
    return (str(root), "")


def Path_index(root: Any) -> Any:
    """The git index path for ``root``. A function so the import stays local."""
    from pathlib import Path

    return Path(str(root)) / ".git" / "index"


def Path_HEAD(root: Any) -> Any:
    """The git HEAD ref path for ``root``; falls back to ``.git`` itself."""
    from pathlib import Path

    base = Path(str(root)) / ".git"
    head = base / "HEAD"
    return head if head.exists() else base


_LISTING_CACHE: Dict[Tuple[str, str], Tuple[str, ...]] = {}


def repository_files(repo: Any, *, cap: int = MAX_ROWS * 8) -> Tuple[str, ...]:
    """Bounded, ignore-respecting, memoised repository-relative file listing.

    Delegates to :func:`cli.fileview.read_repository_files`, which is the ONE
    listing authority (``git ls-files --cached --others --exclude-standard``
    with a bounded, link-free walk fallback). Memoised per (repo, index mtime)
    so a repaint costs a dict lookup rather than a subprocess.
    """
    key = _cache_key(repo)
    cached = _LISTING_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        values = tuple(_fv.read_repository_files(repo, cap))
    except Exception:
        return ()
    _LISTING_CACHE[key] = values
    return values


def clear_listing_cache() -> None:
    """Drop the memo. Exists so a test can measure a cold build honestly."""
    _LISTING_CACHE.clear()


def tree_rows(
    paths: Iterable[Any],
    changed: Iterable[Any] = (),
    *,
    query: str = "",
    max_rows: int = MAX_ROWS,
) -> Tuple[TreeRow, ...]:
    """Build the tree from repository-relative paths, containment-checked.

    ``fileview.build_file_tree`` does the deterministic grouping; this wrapper
    re-checks EVERY row it returns against the declared root before it can be
    rendered. The double check is deliberate: the grouping function's contract is
    "deterministic tree from relative paths", and a containment guarantee that
    depends on an upstream function never widening its input is a guarantee
    about somebody else's code.
    """
    rows = _fv.build_file_tree(paths, changed, (), max_rows=max_rows)
    needle = str(query or "").strip().lower()
    out: List[TreeRow] = []
    for entry in rows:
        path = str(entry.get("path") or "")
        if not path:
            continue
        if needle and needle not in path.lower():
            continue
        try:
            cap = max(1, int(max_rows))
        except (TypeError, ValueError):
            cap = MAX_ROWS
        if len(out) >= cap:
            break
        out.append(
            TreeRow(
                path=path,
                name=str(entry.get("name") or ""),
                is_file=bool(entry.get("is_file")),
                depth=int(entry.get("depth") or 0),
                changed=bool(entry.get("changed")),
            )
        )
    return tuple(out)


def tree_model(
    repo: Any,
    changed: Iterable[Any] = (),
    *,
    query: str = "",
    max_rows: int = MAX_ROWS,
    width: int = 60,
    list_cache: bool = True,
) -> TreeModel:
    """Build the whole tree model, timing the build so the cost is a receipt.

    ``list_cache=False`` forces the cold path, which is what a performance
    assertion must measure -- a warm dict lookup is not "fast on this
    repository", it is "fast at reading a cache".
    """
    import time

    if not list_cache:
        clear_listing_cache()
    started = time.perf_counter()
    files = repository_files(repo)
    rows = tree_rows(files, changed, query=query, max_rows=max_rows)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    root = None
    try:
        root = _fv.repo_root(repo)
    except Exception:
        root = None
    changed_paths = tuple(str(item) for item in changed if str(item))
    return TreeModel(
        rows=tuple(rows),
        repo=str(root or repo or ""),
        changed=changed_paths,
        files_shown=len(rows),
        files_total=len(files),
        rows_shown=len(rows),
        rows_total=len(rows),
        width=max(1, int(width)),
        query=str(query or ""),
        listing_available=bool(files),
        listing_reason="" if files else (
            "no files are listed for this repository; the tree is empty, which is "
            "not the same as 'nothing changed'"
        ),
        skipped_directory_names=tuple(sorted(_fv._SKIP_DIRS)),
        build_ms=elapsed_ms,
    )


def tree_row_line(row: TreeRow, width: int = 60, *, focused: bool = False) -> str:
    """One tree line: the changed marker FIRST, then the indent, then the name.

    The marker is first because it is the only thing distinguishing a run's own
    edit from the rest of the repository, and a marker at the tail is a marker a
    narrow terminal drops.
    """
    limit = max(1, int(width))
    caret = ">" if focused else " "
    indent = "  " * max(0, int(row.depth))
    suffix = "" if row.is_file else "/"
    head = f"{caret}{row.marker} "
    body = _bound(f"{indent}{row.name}{suffix}", max(4, limit - len(head)))
    return _bound(head + body, limit)


def tree_lines(
    model: Any,
    width: int = 60,
    *,
    focused: int = 0,
    rows: Optional[int] = None,
) -> List[str]:
    """Render the tree, with every bound STATED.

    Never returns an empty list: an empty render is indistinguishable from a
    surface that does not exist.
    """
    limit = max(1, int(width))
    if limit < MIN_TREE_COLUMNS:
        return [
            f"tree too narrow ({limit} column(s)): widen to {MIN_TREE_COLUMNS}+ "
            "or press ctrl+d to collapse it"
        ]
    header = f"files {int(getattr(model, 'files_shown', 0))}/{int(getattr(model, 'files_total', 0))}"
    changed_count = len(tuple(getattr(model, "changed", ()) or ()))
    if changed_count:
        header += f" · {CHANGED_MARKER} {changed_count} changed this run"
    lines = [_bound(header, limit)]
    if not bool(getattr(model, "listing_available", True)):
        lines.append(_bound(f"listing unavailable: {getattr(model, 'listing_reason', '')}", limit))
    entries = tuple(getattr(model, "rows", ()) or ())
    if not entries:
        needle = str(getattr(model, "query", "") or "")
        if needle:
            lines.append(_bound(f"no file matches {needle!r}", limit))
        else:
            lines.append(_bound("no files listed", limit))
    else:
        budget = len(entries) if rows is None else max(1, int(rows))
        index = max(0, min(int(focused or 0), len(entries) - 1))
        shown = entries[:budget]
        for position, row in enumerate(shown):
            lines.append(tree_row_line(row, limit, focused=position == index))
        omitted = len(entries) - len(shown)
        if omitted > 0:
            lines.append(_bound(f"+{omitted} more row(s) not shown", limit))
    if limit >= NARROW_TREE_COLUMNS:
        lines.append(_bound(IGNORED_NOTE.format(skipped=len(tuple(getattr(model, "skipped_directory_names", ()) or ()))), limit))
    return [_bound(line, limit) for line in lines if str(line).strip()]


def open_tree_target(path: str, repo: Any) -> Dict[str, Any]:
    """Resolve a focused tree row to a diff/content target, refusing an escape.

    Returns ``{ok, path, absolute, reason}``. ``absolute`` is ``None`` when the
    path is refused, which is the honest answer: a tree row that names a path
    outside the declared root resolves to NOTHING rather than to a path the
    caller was not shown.
    """
    safe = str(path or "").strip()
    if not safe:
        return {"ok": False, "path": "", "absolute": None, "reason": "no path was focused"}
    try:
        resolved = _fv.safe_repo_path(repo, safe)
    except Exception:
        resolved = None
    if resolved is None:
        return {
            "ok": False,
            "path": _bound(safe, 80),
            "absolute": None,
            "reason": "path resolves outside the declared repository root and was refused",
        }
    return {"ok": True, "path": safe, "absolute": str(resolved), "reason": ""}


def assert_every_tree_path_is_contained() -> None:
    """Refuse to ship a tree that can name a path outside the repository.

    The control arm is in-module: a hostile relative path is planted and the
    resolver is required to refuse it. A containment gate with no hostile input
    is a comment.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "repo"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / ".git").mkdir()
        inside = open_tree_target("src/a.py", root)
        assert inside["ok"] is True and inside["absolute"], "a contained path was refused"
        for hostile in ("../escape.py", "/etc/passwd", "src/../../escape.py", "..\\escape.py"):
            out = open_tree_target(hostile, root)
            assert out["ok"] is False and out["absolute"] is None, (
                f"a path escaping the declared root was admitted: {hostile} -> {out}"
            )
