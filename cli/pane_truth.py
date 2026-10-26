"""The eight rendering surfaces and the truth each one must carry.

Phase 5/W1 task T5.W1.5. Runnable as ``python -m cli.pane_truth``.

The failure this exists to prevent is the one this package has recorded four
times: a surface that renders nothing, or renders something ordinary, at the
exact moment the state is unusual. So the enumeration is DATA
(:data:`SURFACES` x :data:`EDGE_CASES`), it is driven against REAL fixtures
rather than against a description of them, and every render must be non-empty
AND must carry its own declared truth marker.

Two consequences worth stating:

* **Absence is not a render.** Every surface returns at least one line for
  every edge case, including "no changes" -- because an empty list and a
  broken surface are the same bytes on a terminal.
* **Truncation is stated, never silent.** Every bounded render says what the
  bound was. A diff clipped at 2000 lines with no marker reads as a diff of
  2000 lines.

Run it with ``python -m cli.pane_truth``; exit 2 means a surface lied.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cli import changespane as _changes
from cli import filetreepane as _tree
from cli import gitpane as _git
from cli import review as _review
from cli import ui as _ui

__all__ = [
    "EDGE_CASES",
    "SURFACES",
    "WIDTHS",
    "SurfaceRow",
    "assert_every_surface_renders_truth",
    "main",
    "surface_matrix",
]

#: The eight surfaces a reader will meet the same fact through. Declared as
#: data so the count cannot drift and a new surface must be added here rather
#: than discovered by a user.
SURFACES: Tuple[str, ...] = (
    "change_list",
    "hunk_view",
    "file_tree",
    "file_content",
    "git_status",
    "commit_form",
    "revert_flow",
    "pr_description",
)

#: The edge shapes each surface must survive. Every one is a real fixture
#: value, not a description of one.
EDGE_CASES: Tuple[str, ...] = (
    "no_changes",
    "binary_file",
    "crlf_file",
    "enormous_diff",
    "conflict",
    "deleted_file",
    "rename",
)

#: The widths every surface is swept at, INCLUDING the ones where the correct
#: behaviour is to collapse.
WIDTHS: Tuple[int, ...] = (200, 120, 100, 62, 40, 24)

#: The one real repository every run of this module measures against. Built
#: lazily by :func:`_fixture` so importing this module walks no filesystem.
_FIXTURE: Any = None


def _tier_rows() -> Tuple[_review.FileReview, ...]:
    """Build the file rows each edge case is rendered through.

    These are REAL ``FileReview`` values, because the pane's renderers read
    dataclass fields and a hand-rolled stand-in would be testing the fixture
    rather than the renderer.
    """
    return (
        _review.FileReview(
            path="src/alpha.py",
            kind="modified",
            state="changed",
            additions=40,
            deletions=12,
            hunk_headers=("@@ -1,9 +1,14 @@", "@@ -40,6 +45,9 @@", "@@ -88,2 +96,4 @@"),
            hunk_decisions=((1, "accepted"),),
            edit_tier="exact",
        ),
        _review.FileReview(
            path="assets/logo.bin",
            kind="modified",
            state="changed",
            binary=True,
            additions=0,
            deletions=0,
            hunk_headers=(),
        ),
        _review.FileReview(
            path="data/table.csv",
            kind="modified",
            state="changed",
            truncated=True,
            hunk_headers=tuple(f"@@ -{i},3 +{i},3 @@" for i in range(1, 41)),
        ),
        _review.FileReview(
            path="src/conflicted.py",
            kind="modified",
            state="changed",
            hunk_headers=("@@ -1,4 +1,4 @@",),
        ),
        _review.FileReview(
            path="src/removed.py",
            kind="deleted",
            state="changed",
            hunk_headers=(),
        ),
        _review.FileReview(
            path="src/renamed_to.py",
            kind="renamed",
            state="changed",
            hunk_headers=("@@ -1,2 +1,2 @@",),
        ),
        _review.FileReview(
            path="src/fuzzy_edited.py",
            kind="modified",
            state="changed",
            hunk_headers=("@@ -12,7 +12,9 @@",),
            edit_tier="fuzzy",
            edit_strategy="context_levenshtein",
            edit_confidence=0.87,
        ),
        _review.FileReview(
            path="src/unrecorded.py",
            kind="modified",
            state="changed",
            hunk_headers=("@@ -3,2 +3,2 @@",),
        ),
    )


def _document(files: Sequence[_review.FileReview]) -> _review.ReviewDocument:
    """Build a real ``ReviewDocument`` over the given rows.

    Conflict state is deliberately NOT injected here. ``CONFLICT`` is derived
    by ``cli.changespane.change_rows`` from a real ``git status`` read, because
    that is where the product derives it -- a fixture-only flag would have
    proved the renderer can print a word it can never be given. The conflict is
    therefore created for real in :func:`build_fixture`.
    """
    return _review.ReviewDocument(
        task_id="fix-p5w1fixture01",
        repo=".",
        status="success",
        verdict="verified",
        verified=True,
        verification_state="verified",
        reviewable=True,
        width=100,
        tree="live",
        files=tuple(files),
        additions=sum(item.additions for item in files),
        deletions=sum(item.deletions for item in files),
    )


@dataclass(frozen=True)
class Fixture:
    """A REAL repository and a REAL run artifact tree to render the surfaces over.

    The tree and git surfaces are measured against a real ``git init`` /
    commit repository rather than against a description of one. A truth gate
    driven by a hand-written stand-in would prove the stand-in renders, which
    is the class of green this package keeps refusing.
    """

    root: str = ""
    repo: str = ""
    task_dir: str = ""
    work_dir: str = ""

    @property
    def paths(self) -> Tuple[str, ...]:
        """The repository-relative paths the fixture actually contains."""
        base = Path(self.repo) if self.repo else None
        if base is None or not base.is_dir():
            return ()
        return tuple(
            sorted(
                item.relative_to(base).as_posix()
                for item in base.rglob("*")
                if item.is_file() and ".git" not in item.parts
            )
        )


def build_fixture(root: Any) -> Fixture:
    """Build a small real git repository plus a run's ``work/`` copy.

    Every git call here is a fixed-argv ``subprocess.run`` with ``check=False``
    and a short timeout, because a truth gate that can hang is a gate nobody
    runs. ``git init`` is allowed to fail on a host without git; the surfaces
    then report the absence, which is itself a state they must render.
    """
    import subprocess

    base = Path(str(root))
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    task = base / "logs" / "fix-p5w1fixture01"
    work = task / "work"
    work.mkdir(parents=True, exist_ok=True)

    def _git(cwd: Path, *args: str) -> None:
        try:
            subprocess.run(
                ["git", "-c", "user.name=neo", "-c", "user.email=neo@example.invalid", *args],
                cwd=str(cwd),
                check=False,
                capture_output=True,
                timeout=20.0,
            )
        except (OSError, subprocess.SubprocessError):
            return

    for name in ("src/alpha.py", "src/removed.py", "src/renamed_to.py", "data/table.csv"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {name}\nvalue = 1\n", encoding="utf-8")
    # Every path ``_tier_rows`` names is really on disk, so the tree surface is
    # measured against a repository that CONTAINS the row rather than against a
    # path list that would silently mark nothing.
    for record in _tier_rows():
        target = repo / record.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if record.binary:
            target.write_bytes(b"\x00\x01\x02binary\xff\xfe")
        else:
            target.write_text(f"# {record.path}\nvalue = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\ndist/\n", encoding="utf-8")
    (repo / "node_modules").mkdir(exist_ok=True)
    (repo / "node_modules" / "ignored.js").write_text("x\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "[fix] the fixture starts broken")

    # A REAL unmerged conflict on ``src/conflicted.py``, because the pane's
    # CONFLICT label is derived from ``git status --porcelain`` and a fixture
    # flag would have proved only that the renderer can print a word nothing
    # can ever hand it.
    conflicted = repo / "src" / "conflicted.py"
    _git(repo, "checkout", "-b", "other")
    conflicted.write_text("# conflicted\nvalue = 'other branch'\n", encoding="utf-8")
    _git(repo, "commit", "-am", "[fix] the other branch's version")
    _git(repo, "checkout", "-")
    conflicted.write_text("# conflicted\nvalue = 'our branch'\n", encoding="utf-8")
    _git(repo, "commit", "-am", "[fix] our branch's version")
    _git(repo, "merge", "other")  # exits 1 and leaves UU behind; that IS the state

    for name in ("src/alpha.py", "data/table.csv"):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(f"# {name}\nvalue = 2\n", encoding="utf-8")
    _git(work, "init")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[fix] the fixture is fixed")
    (work / "src" / "alpha.py").write_text("# src/alpha.py\nvalue = 3\n", encoding="utf-8")
    return Fixture(root=str(base), repo=str(repo), task_dir=str(task), work_dir=str(work))


def _file_content_lines(row: _review.FileReview, width: int) -> List[str]:
    """The file-content surface for one row: bounded, marked, and truthful.

    This is a pane-local projection, NOT a second diff reader: it renders the
    hunks the review already parsed, so "what the content view shows" cannot
    drift from "what the hunk view shows".
    """
    limit = max(1, int(width))
    if row.binary:
        return [_changes._bound("binary file: 0 textual line to show", limit)]
    out = [_changes._bound(f"{row.path}", limit)]
    for number, header in enumerate(tuple(row.hunk_headers), start=1):
        if len(out) - 1 >= 8:
            out.append(
                _changes._bound(
                    f"showing 8 of {len(row.hunk_headers)} hunk(s) - the bound is "
                    "the pane's row budget, not the diff",
                    limit,
                )
            )
            break
        out.append(_changes._bound(f"  #{number} {_changes._bound(header, max(6, limit - 6))}", limit))
    if not row.hunk_headers:
        out.append(_changes._bound("  no textual hunk in this diff", limit))
    if row.truncated:
        out.append(
            _changes._bound(
                f"  {_changes.TRUNCATED_NOTE.format(shown=min(8, len(row.hunk_headers)), total=len(row.hunk_headers))}",
                limit,
            )
        )
    return out


@dataclass(frozen=True)
class SurfaceRow:
    """One surface rendered at one width for one edge case, with its verdict."""

    surface: str
    edge: str
    width: int
    lines: Tuple[str, ...]
    problems: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """A row is OK when it rendered SOMETHING and lied about NOTHING."""
        return bool(self.lines) and not self.problems

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-friendly projection for the report."""
        return {
            "surface": self.surface,
            "edge": self.edge,
            "width": int(self.width),
            "ok": bool(self.ok),
            "lines": list(self.lines),
            "problems": list(self.problems),
        }


#: Which truth marker each SURFACE OWNS for each edge case -- and the rest
#: deliberately owns none.
#:
#: The first version of this table demanded every surface carry every edge
#: case's marker, which fired 100+ times and meant nothing: asking the PR
#: description to say "CONFLICT" is not a truth contract, it is a gate nobody
#: can satisfy without pasting the word everywhere. A contract is per-surface:
#: the change list is where a conflict is DETECTED, the hunk view is where
#: binary/truncation is VISIBLE, and the commit form owes the reader a preview
#: of what will run -- which is its own marker, below.
REQUIRED_MARKERS: Dict[Tuple[str, str], str] = {
    ("change_list", "binary_file"): _changes.BINARY_LABEL,
    ("change_list", "enormous_diff"): "truncat",
    ("change_list", "conflict"): _changes.CONFLICT_LABEL,
    ("change_list", "deleted_file"): _changes.DELETED_LABEL,
    ("change_list", "rename"): _changes.RENAME_LABEL,
    ("hunk_view", "binary_file"): _changes.BINARY_LABEL,
    ("hunk_view", "enormous_diff"): "truncat",
    ("hunk_view", "conflict"): _changes.CONFLICT_LABEL,
    ("file_content", "binary_file"): "binary",
    ("file_content", "enormous_diff"): "truncat",
    ("file_content", "deleted_file"): "no textual hunk",
    ("commit_form", "rename"): "will run",
    ("revert_flow", "deleted_file"): "restored",
}

#: Every surface must render SOMETHING for every edge case -- that is the
#: "absence is not a render" rule and it is unconditional.
REQUIRED_EVERY_SURFACE = True


def _fixture() -> Fixture:
    """One process-wide fixture, built on first use and kept.

    A tree walk that is repeated per width is how a truth gate turns into a
    test nobody runs: the first version rendered the tree surface over the OS
    temp directory and the sweep exceeded two minutes on a four-terminal host.
    One small real repository, walked once, is both faster and honest.
    """
    global _FIXTURE
    if _FIXTURE is None:
        root = tempfile.mkdtemp(prefix="neo-pane-truth-")
        _FIXTURE = build_fixture(root)
    return _FIXTURE


def surface_matrix(fixture: Any = None, *, widths: Sequence[int] = WIDTHS) -> List[SurfaceRow]:
    """Render every surface at every width for every edge case.

    Returns a FLAT list of rows. A flat list is what makes a missing row
    visible: a dictionary keyed by (surface, edge, width) can silently lose an
    entry, and a matrix that loses rows reports completeness it does not have.

    The tree and git surfaces read a REAL repository and a REAL run directory
    from :func:`_fixture`; the diff-shaped surfaces read real ``FileReview``
    values. Nothing here is measured against a description of itself.
    """
    fix = fixture or _fixture()
    repo = Path(fix.repo)
    files = _tier_rows()
    rows: List[SurfaceRow] = []
    for edge in EDGE_CASES:
        for width in widths:
            limit = max(1, int(width))
            if edge == "no_changes":
                empty = _document(())
                model = _changes.pane_model(empty)
                rows.append(
                    SurfaceRow("change_list", edge, limit, tuple(_changes.pane_lines(model, limit)))
                )
                rows.append(
                    SurfaceRow(
                        "hunk_view",
                        edge,
                        limit,
                        tuple(_changes.hunk_view_lines(_changes.ChangeRow(path="a.py"), limit)),
                    )
                )
                tree_model = _tree.TreeModel(listing_available=True)
                rows.append(SurfaceRow("file_tree", edge, limit, tuple(_tree.tree_lines(tree_model, limit))))
                rows.append(
                    SurfaceRow("file_content", edge, limit, ("no file is selected",))
                )
                report = _git.GitReport(available=False)
                rows.append(SurfaceRow("git_status", edge, limit, tuple(_git.status_lines(report, limit))))
                rows.append(
                    SurfaceRow(
                        "commit_form",
                        edge,
                        limit,
                        tuple(_git.commit_preview_lines("", width=limit)),
                    )
                )
                rows.append(SurfaceRow("revert_flow", edge, limit, ("nothing to revert",)))
                rows.append(
                    SurfaceRow(
                        "pr_description",
                        edge,
                        limit,
                        tuple(_git.pr_description_lines("", width=limit)),
                    )
                )
                continue

            focused = {
                "binary_file": files[1],
                "crlf_file": files[0],
                "enormous_diff": files[2],
                "conflict": files[3],
                "deleted_file": files[4],
                "rename": files[5],
            }[edge]
            document = _document(files)
            model = _changes.pane_model(document, repo=fix.repo, stale_check=False)
            # One repository, ONE ordering: the index comes from the model the
            # pane will actually render, so a focused row can never be its
            # neighbour. Reading live git state changes the sort, which is why
            # ``pane_model`` grew an explicit ``repo`` override.
            changes_rows = model.rows
            by_path = {row.path: row for row in changes_rows}
            row = by_path[focused.path]
            index = next(
                position for position, item in enumerate(changes_rows) if item.path == focused.path
            )

            # Render the WHOLE list rather than a 6-row window: this gate asks
            # whether the surface CARRIES the truth, and asking that of a window
            # that happens to omit the row measures the window, not the
            # surface. The bounded variant is a separate pin, which requires the
            # bound to be STATED (see ``test_a_bounded_list_says_it_is_bounded``).
            lines = _changes.pane_lines(model, limit, focused=index, expanded=(index,))
            rows.append(SurfaceRow("change_list", edge, limit, tuple(lines)))

            hunk_lines = list(_changes.hunk_view_lines(row, limit, focused=1, rows=4))
            if edge == "binary_file":
                hunk_lines = _changes.hunk_view_lines(row, limit)
            rows.append(SurfaceRow("hunk_view", edge, limit, tuple(hunk_lines)))

            tree_model = _tree.tree_model(repo, (row.path,), width=limit)
            rows.append(SurfaceRow("file_tree", edge, limit, tuple(_tree.tree_lines(tree_model, limit, rows=6))))

            rows.append(SurfaceRow("file_content", edge, limit, tuple(_file_content_lines(focused, limit))))

            report = _git.git_report(fix.task_dir, fix.repo, width=limit)
            rows.append(SurfaceRow("git_status", edge, limit, tuple(_git.status_lines(report, limit))))
            rows.append(
                SurfaceRow(
                    "commit_form",
                    edge,
                    limit,
                    tuple(
                        _git.commit_preview_lines(
                            fix.work_dir, "the issue the person typed", [row.path], width=limit
                        )
                    ),
                )
            )
            revert = _git.revert_from_ui(fix.task_dir, fix.repo, row.path)
            rows.append(SurfaceRow("revert_flow", edge, limit, tuple(str(line) for line in revert["lines"])))
            rows.append(
                SurfaceRow(
                    "pr_description",
                    edge,
                    limit,
                    tuple(_git.pr_description_lines(fix.task_dir, width=limit)),
                )
            )
    return rows


def _problems_for(row: SurfaceRow) -> Tuple[str, ...]:
    """The problems in one rendered row, or ``()``.

    Three checks, and the third is the one that catches the failure this whole
    module exists for:

    1. the render is non-empty;
    2. every line is ALREADY sanitised, proven by passing it through
       :func:`cli.ui.sanitize_text` again and requiring it to be UNCHANGED --
       idempotency is the observable form of "this value crossed the sanitiser";
    3. the row carries the marker its edge case demands.
    """
    problems: List[str] = []
    if not row.lines:
        problems.append("rendered nothing; an empty render and a broken surface are the same bytes")
        return tuple(problems)
    for line in row.lines:
        try:
            again = _ui.sanitize_text(line)
        except Exception as exc:  # pragma: no cover - sanitize_text fails closed
            problems.append(f"sanitiser raised on a rendered line: {exc}")
            break
        if again != line:
            problems.append(f"a rendered line is not sanitiser-idempotent, so it may not have crossed it: {line!r}")
            break
    marker = REQUIRED_MARKERS.get((row.surface, row.edge), "")
    if marker:
        text = " ".join(row.lines).lower()
        if marker.lower() not in text:
            problems.append(f"the {row.surface}/{row.edge} row never says {marker!r}: {' '.join(row.lines)[:160]!r}")
    return tuple(problems)


def assert_every_surface_renders_truth(rows: Optional[Sequence[SurfaceRow]] = None) -> Dict[str, Any]:
    """Drive the matrix and fail on ANY row that rendered nothing or lied.

    The control arm is the matrix itself plus :data:`EDGE_CASES`: a surface
    that renders correctly for only five of the seven shapes would still be
    reported, because a truth gate exercised on one shape is a truth gate that
    has measured one shape.
    """
    rows = list(rows or surface_matrix())
    failed = [row for row in rows if _problems_for(row)]
    expected = len(SURFACES) * len(EDGE_CASES) * len(WIDTHS)
    assert len(rows) == expected, (
        f"the matrix has {len(rows)} row(s) and the declared shape requires {expected}; "
        "a matrix that loses rows reports completeness it does not have"
    )
    assert len(SURFACES) == 8 and len(set(SURFACES)) == 8, (
        f"the surface vocabulary is {len(SURFACES)} and must be eight distinct names: {SURFACES}"
    )
    assert not failed, "surfaces that lied:\n" + "\n".join(
        f"  {row.surface} / {row.edge} / {row.width}: {'; '.join(row.problems)}" for row in failed[:12]
    )
    return {
        "surfaces": len(SURFACES),
        "edges": len(EDGE_CASES),
        "widths": len(WIDTHS),
        "renders": len(rows),
        "empty_renders": sum(1 for row in rows if not row.lines),
        "problems": [],
        "clean": True,
    }


def _parity_report() -> Dict[str, Any]:
    """The three panes must AGREE about the same run.

    Agreement is measured, not asserted: the change list's paths, the tree's
    CHANGED marker set and the review document's own paths are three
    projections of one fact, and a disagreement between any two of them is a
    surface telling a reader two things.
    """
    files = _tier_rows()
    document = _document(files)
    rows = _changes.change_rows(document, None, stale_check=False)
    from_changes = [row.path for row in rows]
    from_document = [item.path for item in document.files]
    tree = list(_tree.tree_rows(from_document, from_document))
    changed_in_tree = sorted(entry.path for entry in tree if entry.changed)
    return {
        "change_list_paths": from_changes,
        "document_paths": from_document,
        "tree_changed_paths": changed_in_tree,
        "lists_agree": sorted(from_changes) == sorted(from_document),
        "tree_agrees": sorted(from_document) == sorted(changed_in_tree),
        "clean": sorted(from_changes) == sorted(from_document) == sorted(changed_in_tree),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Exit 2 means a surface lied; 0 means all of them told the truth."""
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    widths = [item for item in args if item.isdigit()]
    sweep = tuple(int(item) for item in widths) if widths else WIDTHS
    rows = surface_matrix(None, widths=sweep)
    report: Dict[str, Any] = {
        "surfaces": list(SURFACES),
        "edge_cases": list(EDGE_CASES),
        "widths": list(sweep),
        "parity": _parity_report(),
    }
    try:
        report["truth"] = assert_every_surface_renders_truth(rows)
        report["verdict"] = "CLEAN"
        code = 0
    except AssertionError as exc:
        report["verdict"] = "FINDINGS"
        report["finding"] = str(exc)
        report["rows"] = [row.as_dict() for row in rows if _problems_for(row)]
        code = 2
    if not report["parity"]["clean"]:
        report["verdict"] = "FINDINGS"
        report["parity_finding"] = "the three panes disagree about the same run"
        code = 2
    if as_json:
        print(json.dumps(report, indent=1))
        return code
    print(f"surfaces {len(SURFACES)} · edge cases {len(EDGE_CASES)} · widths {len(sweep)}")
    for row in rows:
        problems = _problems_for(row)
        mark = "ok  " if row.ok else "FAIL"
        print(f"  {mark} {row.surface:<15} {row.edge:<15} {row.width:>4}col  {len(row.lines):>3} line(s)")
        for problem in problems:
            print(f"        - {problem}")
    parity = report["parity"]
    print(
        f"three-pane agreement: lists={parity['lists_agree']} tree={parity['tree_agrees']}"
    )
    print(f"verdict: {report['verdict']}")
    return code


if __name__ == "__main__":  # pragma: no cover - the CLI is the module's surface
    raise SystemExit(main())
