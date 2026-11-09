"""What a long-lived memory system costs on disk, measured and projected (P3a).

Phase 3a adds an append-only session tree on top of snapshots and a
content-addressed store. **All three grow monotonically.** A memory feature
that fills a disk in a week is not a feature, and the numbers that decide
whether to bound it are not per-turn figures -- they are a projection.

This module produces:

* :func:`measure_growth` -- a per-turn / per-session growth table for the four
  stores, each number either MEASURED from a real artifact or explicitly
  MODELLED from a named basis. **A modelled number never renders as a measured
  one**, because that is the difference between "we know" and "we estimated".
* :func:`project_30_days` -- the projection the decision actually needs.
* :func:`storage_bound` -- the bound, and the firing condition, in the shape an
  operator can act on.

**It has no production call site yet.** It is reachable through
``python -m execution.storage_cost`` and through its own tests; wiring it into a
doctor row or a session summary is a filed cross-terminal request, and the state
is asserted by a pin rather than left to be inferred.

**Every store is delegated to, not re-implemented.** The session-tree numbers
come from :meth:`runtime.session_tree.SessionTree.growth` (T3's own authority);
the snapshot and store numbers come from real :mod:`execution.snapshot` runs on a
real fixture repository; the journal numbers are read off real ``trace.jsonl``
files. A second measurement of the same store is how two numbers for one thing
come to exist and disagree.

**The numbers are for one repository shape and are labelled as such.** A growth
table without the machine, the repository and the workload is a rumour; the
receipt carries all three.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from execution.snapshot import (
    SNAPSHOT_PLAN_BUDGET_KEY,
    SharedStore,
    create_run_snapshot,
    plan_run_snapshot,
    tree_bytes,
)

__all__ = [
    "DEFAULT_USAGE",
    "STORES",
    "BoundEnforcement",
    "GrowthRow",
    "StorageBound",
    "StorageProjection",
    "enforce_storage_bound",
    "measure_growth",
    "project_30_days",
    "storage_bound",
    "storage_report",
]

#: The stores that grow, in the order the brief names them.
STORES: Tuple[str, ...] = (
    "session_tree",
    "snapshots",
    "content_addressed_objects",
    "journals",
)


@dataclass(frozen=True)
class Usage:
    """A stated usage pattern. The projection is only as good as this.

    Every field is a DECLARATION, and the receipt prints them, because "30 days
    of daily use" is not a number -- it is a number plus a number of sessions
    plus a number of turns, and a reader who does not see them cannot tell
    whether the projection describes them.
    """

    sessions_per_day: int = 3
    turns_per_session: int = 40
    forks_per_session: int = 2
    runs_per_session: int = 1
    days: int = 30

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "sessions_per_day": self.sessions_per_day,
            "turns_per_session": self.turns_per_session,
            "forks_per_session": self.forks_per_session,
            "runs_per_session": self.runs_per_session,
            "days": self.days,
        }


#: The declared default pattern. It is a MODULE constant rather than a default
#: argument so that a caller who never states a pattern is visibly using the
#: declared one rather than a value buried in a signature.
DEFAULT_USAGE = Usage()


@dataclass(frozen=True)
class GrowthRow:
    """One store's growth, with the provenance of every number in it.

    ``basis`` is the load-bearing field. It is one of:

    * ``measured`` -- read off a real artifact produced by the real code;
    * ``modelled`` -- computed from a stated per-unit figure this module
      measured once and recorded here.

    A row whose ``basis`` is ``modelled`` never renders as a measurement, and
    :func:`storage_report` prints the basis column for exactly that reason.
    """

    store: str
    per_turn_bytes: Optional[float]
    per_session_bytes: Optional[float]
    bound: str
    gc_policy: str
    basis: str = "modelled"
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "store": self.store,
            "per_turn_bytes": self.per_turn_bytes,
            "per_session_bytes": self.per_session_bytes,
            "per_turn_available": self.per_turn_bytes is not None,
            "per_session_available": self.per_session_bytes is not None,
            "bound": self.bound,
            "gc_policy": self.gc_policy,
            "basis": self.basis,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def _fixture_repository(root: Path) -> Path:
    """Build a small, realistic repository to measure a snapshot against.

    A measurement needs a real copier run over a real tree; measuring against
    the harness's own repository would take minutes (its ``os.walk`` is
    documented at ~360 s) and would describe a tree full of other terminals'
    artifacts. A fixture is honest about what it is: named in ``evidence``.
    """
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("def f():\n    return 1\n" * 40)
    (repo / "src" / "util.py").write_text("X = 1\n" * 80)
    (repo / "tests" / "test_app.py").write_text("def test_f():\n    assert True\n" * 20)
    (repo / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
    (repo / "README.md").write_text("# fixture\n")
    return repo


def _measure_snapshot_stores() -> Tuple[Optional[float], Optional[float], Optional[float], str]:
    """Measure snapshot + store growth from two REAL snapshot runs.

    Returns ``(per_run_bytes, store_growth_second_run, mean_blob_bytes,
    evidence)``. The second figure is the one that matters and the one that is
    usually assumed: **an unchanged repository's second snapshot costs ZERO
    additional store bytes**, because ``pristine/`` hardlinks content the store
    already holds. A per-run figure that ignored that would overstate the
    content-addressed store's growth by the whole repository size per run.
    """
    tmp = Path(tempfile.mkdtemp(prefix="t2-storage-"))
    try:
        repo = _fixture_repository(tmp)
        store = SharedStore(tmp / "store")
        config = {SNAPSHOT_PLAN_BUDGET_KEY: 30.0}
        plan = plan_run_snapshot(repo, tmp / "probe", config=config)
        if plan.truncated:
            return None, None, None, (
                "the fixture plan did not complete, so nothing was measured"
            )
        first = create_run_snapshot(repo, tmp / "run1", store=store, config=config)
        after_first = store.stats()
        second = create_run_snapshot(repo, tmp / "run2", store=store, config=config)
        after_second = store.stats()
        run_bytes = tree_bytes(tmp / "run1" / "pristine") + tree_bytes(tmp / "run1" / "work")
        growth = int(after_second["bytes"]) - int(after_first["bytes"])
        mean_blob = (
            float(after_second["bytes"]) / float(after_second["blobs"])
            if after_second["blobs"]
            else 0.0
        )
        evidence = (
            f"real create_run_snapshot over a {plan.files}-file fixture repo "
            f"({plan.total_bytes} B): run1 written={first.written} "
            f"shared={first.shared} copied={first.copied}; run2 written="
            f"{second.written} shared={second.shared}; store went "
            f"{after_first['bytes']} -> {after_second['bytes']} B "
            f"({after_second['blobs']} blobs)"
        )
        return float(run_bytes), float(growth), mean_blob, evidence
    except Exception as exc:  # a measurement fault is reported, never a zero
        return None, None, None, f"the snapshot measurement did not run: {type(exc).__name__}: {exc}"
    finally:
        try:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def _candidate_roots(log_root: Any) -> List[Path]:
    """The roots a journal measurement may read, in preference order.

    ``memory.paths.default_logs_dir()`` is the AUTHORITY for where runs are
    written, and it is tried first. It is not always where the traces are: on
    this tree it resolves to a per-invocation directory that holds none, while
    the repository's own ``logs/`` holds 27 of them. **Silently substituting
    one root for the other would make the receipt describe a root nobody asked
    about**, so every candidate is tried and the one that actually yielded data
    is named in the evidence.
    """
    roots: List[Path] = []
    if log_root:
        roots.append(Path(log_root))
    try:
        from memory import paths as memory_paths

        resolved = memory_paths.default_logs_dir()
        if resolved:
            roots.append(Path(resolved))
    except Exception:
        pass
    roots.append(Path("logs"))
    seen: List[str] = []
    unique: List[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.append(key)
            unique.append(root)
    return unique


def _measure_journal_rows(log_root: Any, *, limit: int = 400) -> Tuple[Optional[float], str]:
    """Measure bytes-per-journal-row off real trace files.

    Reads real ``trace.jsonl`` files and divides each file's size by its
    non-blank line count, taking the **median** over files rather than the mean:
    a journal row's size is a heavy tail (a row carrying a failing test report
    is orders of magnitude larger than a tool-start row), and a mean would
    describe a row nobody writes. Returns ``(median_bytes_per_row, evidence)``,
    where the evidence NAMES the root it read.
    """
    attempted: List[str] = []
    for root in _candidate_roots(log_root):
        attempted.append(str(root))
        if not root.is_dir():
            continue
        per_row: List[float] = []
        for candidate in sorted(root.glob("*/trace.jsonl"))[:limit]:
            try:
                size = candidate.stat().st_size
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows = sum(1 for line in text.splitlines() if line.strip())
            if rows:
                per_row.append(size / rows)
        if not per_row:
            continue
        per_row.sort()
        return float(statistics.median(per_row)), (
            f"median over {len(per_row)} real trace.jsonl files under {root}: "
            f"min={per_row[0]:.0f} p50={statistics.median(per_row):.0f} "
            f"p90={per_row[int(len(per_row) * 0.9) - 1]:.0f} max={per_row[-1]:.0f} B/row"
        )
    return None, (
        "no readable trace.jsonl under any candidate root "
        f"({attempted}), so journal growth is UNMEASURED rather than zero"
    )


def _measure_session_tree(turns: int = 40, text_chars: int = 400) -> Tuple[Optional[float], Optional[float], str]:
    """Measure per-turn and per-session session-tree growth from T3's own tree.

    Delegates to :meth:`runtime.session_tree.SessionTree.growth`, which is the
    authority for its own store. A session's total includes ``forks_per_session``
    branch rows and one head row, because those are the rows a fork and a head
    move actually append.
    """
    try:
        from runtime.session_tree import SessionTree
    except Exception as exc:
        return None, None, (
            f"runtime.session_tree could not be imported ({type(exc).__name__}), so "
            "session-tree growth is UNMEASURED rather than zero"
        )
    tmp = Path(tempfile.mkdtemp(prefix="t2-tree-"))
    try:
        tree = SessionTree(tmp)
        ids: List[str] = []
        for index in range(1, int(turns) + 1):
            entry = tree.advance(
                turn_id=f"turn-{index}",
                text="x" * int(text_chars),
                payload={"tool": "bash", "ok": True},
            )
            if entry is not None:
                ids.append(entry.id)
        for position in range(0, min(len(ids), 4), 2):
            tree.fork(label="a different approach", at=ids[position])
        if ids:
            tree.move_head(ids[0], reason="measure")
        growth = tree.growth()
        journal_bytes = float(growth.get("journal_bytes") or 0)
        turn_count = float(growth.get("turns") or 0)
        entries = float(growth.get("entries") or 0)
        per_turn = float(growth.get("bytes_per_turn") or 0.0) or (
            journal_bytes / turn_count if turn_count else None
        )
        per_entry = float(growth.get("bytes_per_entry") or 0.0) or (
            journal_bytes / entries if entries else None
        )
        evidence = (
            f"runtime.session_tree.growth() over a real {int(turns)}-turn tree with "
            f"{int(entries)} entries (incl. fork and head rows) at {int(text_chars)}-char "
            f"turn text: journal_bytes={int(journal_bytes)} "
            f"bytes_per_turn={per_turn:.0f} "
            f"bytes_per_entry={per_entry:.0f}"
        )
        return per_turn, per_entry, evidence
    except Exception as exc:
        return None, None, (
            f"the session-tree measurement did not run: {type(exc).__name__}: {exc}"
        )
    finally:
        try:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def measure_growth(
    *,
    log_root: Any = None,
    turns_per_session: int = 40,
    text_chars: int = 400,
) -> List[GrowthRow]:
    """Measure the growth of all four stores. Returns one row per store.

    Each row is independently measured and independently failable: a store whose
    measurement could not run reports ``per_turn_bytes=None`` with the reason in
    ``evidence``, and ``to_dict`` publishes ``per_turn_available: False``. That
    is the difference between "we measured zero growth" and "we could not
    measure", and this repository has a doctrine about it.
    """
    if log_root is None:
        try:
            from memory import paths as memory_paths

            log_root = memory_paths.default_logs_dir()
        except Exception:
            log_root = "logs"

    run_bytes, store_growth, mean_blob, snap_evidence = _measure_snapshot_stores()
    journal_bpr, journal_evidence = _measure_journal_rows(log_root)
    tree_per_turn, tree_bytes_per_entry, tree_evidence = _measure_session_tree(
        turns=turns_per_session, text_chars=text_chars
    )
    if tree_bytes_per_entry is not None:
        # A per-ENTRY figure is the honest way to read the projection, because a
        # fork and a head move each append a row without adding a turn.
        tree_evidence = f"{tree_evidence} | per-entry bytes are the fork/head cost"

    rows: List[GrowthRow] = []

    rows.append(
        GrowthRow(
            store="session_tree",
            per_turn_bytes=tree_per_turn,
            per_session_bytes=(tree_per_turn * turns_per_session)
            if tree_per_turn is not None
            else None,
            bound="grows with turns; one line per turn in entries.jsonl, never rewritten",
            gc_policy=(
                "runtime.session_tree.gc() is OPT-IN, collects only unretained entries "
                "older than min_age_s that no survivor cites, and rotates rather than "
                "deletes; a deliberate fork is a GC root and is never collected by default"
            ),
            basis="measured" if tree_per_turn is not None else "unmeasured",
            evidence=tree_evidence,
        )
    )

    rows.append(
        GrowthRow(
            store="snapshots",
            per_turn_bytes=(run_bytes / turns_per_session)
            if run_bytes is not None
            else None,
            per_session_bytes=run_bytes,
            bound=(
                "one pristine/ + work/ pair PER RUN, not per turn; growth is linear in "
                "runs, and the pair is what makes a rewind possible at all"
            ),
            gc_policy=(
                "execution.snapshot.prune_run_directories: age + total bytes + keep-latest, "
                "with a LIVE LINEAGE'S RUNS NEVER PRUNED and an unreadable liveness "
                "declaration pruning nothing"
            ),
            basis="measured" if run_bytes is not None else "unmeasured",
            evidence=snap_evidence,
        )
    )

    rows.append(
        GrowthRow(
            store="content_addressed_objects",
            per_turn_bytes=(store_growth / turns_per_session)
            if store_growth is not None
            else None,
            per_session_bytes=store_growth,
            bound=(
                "MONOTONIC and never shrinks on its own: one blob per distinct content, "
                "so it grows with the number of DISTINCT file states, not with turns"
            ),
            gc_policy=(
                "execution.snapshot.prune_shared_store: store_max_bytes, oldest mtime "
                "first; mtime is a first-stored approximation of LRU, not a true atime"
            ),
            basis="measured" if store_growth is not None else "unmeasured",
            evidence=(
                f"{snap_evidence} | mean blob {mean_blob:.0f} B" if mean_blob else snap_evidence
            ),
        )
    )

    rows.append(
        GrowthRow(
            store="journals",
            per_turn_bytes=journal_bpr,
            per_session_bytes=(journal_bpr * turns_per_session)
            if journal_bpr is not None
            else None,
            bound=(
                "one row per event; row size is a heavy tail (a row carrying a failing "
                "test report is far larger than a tool-start row)"
            ),
            gc_policy=(
                "per-run: a retention pass removes whole run directories, which removes "
                "their journals with them. Journals have no independent GC."
            ),
            basis="measured" if journal_bpr is not None else "unmeasured",
            evidence=journal_evidence,
        )
    )
    return rows


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageProjection:
    """Disk use over a stated usage pattern, and the honest caveats.

    ``per_store`` maps a store name to projected bytes over the whole window.
    ``unavailable`` names every store whose figure could not be measured, so a
    total that silently omits a store is impossible to render.
    """

    usage: Usage
    per_store: Mapping[str, Optional[float]] = field(default_factory=dict)
    rows: Tuple[GrowthRow, ...] = ()
    unavailable: Tuple[str, ...] = ()
    total_bytes: Optional[float] = None
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "usage": self.usage.to_dict(),
            "per_store": dict(self.per_store),
            "rows": [row.to_dict() for row in self.rows],
            "unavailable": list(self.unavailable),
            "total_bytes": self.total_bytes,
            "total_available": self.total_bytes is not None,
            "notes": list(self.notes),
        }

    def render(self, *, total_gib: float = 0.0) -> List[str]:
        """Render PLAIN lines: the growth table, then the projection."""
        lines: List[str] = ["growth table (bytes unless stated)"]
        lines.append(
            f"  {'store':<26}{'per turn':>12}{'per session':>14}  basis"
        )
        for row in self.rows:
            per_turn = (
                f"{row.per_turn_bytes:.0f}" if row.per_turn_bytes is not None else "n/a"
            )
            per_session = (
                f"{row.per_session_bytes:.0f}"
                if row.per_session_bytes is not None
                else "n/a"
            )
            lines.append(
                f"  {row.store:<26}{per_turn:>12}{per_session:>14}  {row.basis}"
            )
        lines.append("")
        lines.append(
            f"projection over {self.usage.days} days at "
            f"{self.usage.sessions_per_day} sessions/day x "
            f"{self.usage.turns_per_session} turns/session"
        )
        sessions = self.usage.sessions_per_day * self.usage.days
        turns = sessions * self.usage.turns_per_session
        lines.append(f"  {sessions} sessions, {turns} turns in total")
        for name in STORES:
            value = self.per_store.get(name)
            if value is None:
                lines.append(f"  {name:<26} UNMEASURED (not zero)")
            else:
                lines.append(f"  {name:<26} {value / (1024.0 * 1024.0):.2f} MiB")
        if self.total_bytes is None:
            lines.append(
                "  TOTAL                 UNMEASURED - at least one store could not be "
                "measured, and a total that omits a store would be a smaller lie only "
                "by luck"
            )
        else:
            lines.append(f"  {'TOTAL':<26} {self.total_bytes / (1024.0 * 1024.0):.2f} MiB")
            if total_gib > 0.0:
                share = (self.total_bytes / (1024.0 ** 3)) / total_gib * 100.0
                lines.append(
                    f"  as a share of a {total_gib:.0f} GiB volume: {share:.3f}%"
                )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return lines


def project_30_days(
    rows: Optional[Sequence[GrowthRow]] = None,
    *,
    usage: Optional[Usage] = None,
    log_root: Any = None,
    total_gib: float = 0.0,
) -> StorageProjection:
    """Project disk use over the usage pattern, and name every gap.

    **A projection is only useful if it says what it does not know**, so a store
    whose per-unit figure is unavailable appears in ``unavailable`` and the
    ``total`` is ``None`` rather than a smaller number. A total that silently
    omits a store is the same class of lie as an absent value rendered as zero.

    ``rows`` may be supplied to project a MEASURED table; otherwise
    :func:`measure_growth` runs. The ``turns_per_session`` used for a per-turn
    rate comes from the SAME usage object, so a projection cannot mix a per-turn
    figure measured at 40 turns with a total computed at 15.
    """
    pattern = usage or DEFAULT_USAGE
    measured = list(rows) if rows is not None else measure_growth(
        log_root=log_root, turns_per_session=pattern.turns_per_session
    )
    sessions = pattern.sessions_per_day * pattern.days
    turns = sessions * pattern.turns_per_session

    per_store: Dict[str, Optional[float]] = {}
    unavailable: List[str] = []
    notes: List[str] = []
    for row in measured:
        if row.store == "snapshots":
            value = (
                row.per_session_bytes * pattern.runs_per_session * sessions
                if row.per_session_bytes is not None
                else None
            )
            notes.append(
                f"snapshots scale with RUNS ({pattern.runs_per_session}/session), not "
                f"with turns; {sessions} runs projected"
            )
        elif row.store == "content_addressed_objects":
            # The store grows with DISTINCT content, so a per-turn figure measured
            # on an UNCHANGED repository is a LOWER BOUND (it is zero). The
            # projection says so rather than reporting 0.00 MiB and looking free.
            value = row.per_session_bytes * sessions if row.per_session_bytes is not None else None
            notes.append(
                "content_addressed_objects is a LOWER BOUND: it was measured on an "
                "unchanged repository, so a real workload that EDITS files adds a blob "
                "per distinct state. The bound is what makes a bound necessary."
            )
        else:
            value = row.per_turn_bytes * turns if row.per_turn_bytes is not None else None
        per_store[row.store] = value
        if value is None:
            unavailable.append(row.store)

    total: Optional[float]
    if unavailable:
        total = None
        notes.append(
            "the TOTAL is withheld because "
            + ", ".join(unavailable)
            + " could not be measured; a total that omits a store understates"
        )
    else:
        total = sum(float(value or 0.0) for value in per_store.values())

    if usage is None:
        notes.append(
            "the usage pattern is this module's DECLARED default "
            f"({pattern.to_dict()}); it is a pattern, not a measurement of any user"
        )
    else:
        notes.append(
            "the usage pattern was STATED by the caller "
            f"({pattern.to_dict()}); the projection describes that pattern and "
            "no other"
        )
    return StorageProjection(
        usage=pattern,
        per_store=per_store,
        rows=tuple(measured),
        unavailable=tuple(unavailable),
        total_bytes=total,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageBound:
    """The storage ceiling and the exact condition under which it fires.

    ``fires_at_fraction`` is the fraction of the volume at which retention is
    triggered, and it is deliberately BELOW 1.0: a bound that fires when the disk
    is already full is not a bound, it is a post-mortem. ``headroom_bytes`` is
    what is deliberately left free.

    ``condition`` is a sentence an operator can check, and it names BOTH halves
    -- what triggers it and what it does -- because a bound with no stated
    consequence is a number nobody acts on.
    """

    store_max_bytes: int
    retention_max_age_days: float
    keep_latest: int
    volume_bytes: Optional[int]
    fires_at_fraction: float
    headroom_bytes: Optional[int]
    condition: str
    reachable: bool = False
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "store_max_bytes": self.store_max_bytes,
            "retention_max_age_days": self.retention_max_age_days,
            "keep_latest": self.keep_latest,
            "volume_bytes": self.volume_bytes,
            "volume_available": self.volume_bytes is not None,
            "fires_at_fraction": self.fires_at_fraction,
            "headroom_bytes": self.headroom_bytes,
            "headroom_available": self.headroom_bytes is not None,
            "condition": self.condition,
            "reachable": self.reachable,
            "notes": list(self.notes),
        }

    def render(self) -> List[str]:
        """Render PLAIN lines naming the bound and when it fires."""
        lines = [
            f"store ceiling      : {self.store_max_bytes} B "
            f"({self.store_max_bytes / (1024.0 ** 3):.2f} GiB)",
            f"retention age      : {self.retention_max_age_days:.0f} days",
            f"keep-latest        : {self.keep_latest} run directories (a FLOOR, not a preference)",
        ]
        if self.volume_bytes is None:
            lines.append(
                "volume headroom    : UNMEASURED - no volume size was supplied, so the "
                "reserve cannot be computed and is not reported as zero"
            )
        else:
            lines.append(
                f"volume headroom    : {self.headroom_bytes} B reserved "
                f"({self.fires_at_fraction:.0%} of the volume fires retention)"
            )
        lines.append(f"fires when         : {self.condition}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return lines


#: Fraction of a volume at which retention is triggered. Below 1.0 on purpose:
#: a bound that fires when the disk is already full is a post-mortem.
DEFAULT_FIRES_AT_FRACTION = 0.85


def storage_bound(
    *,
    volume_bytes: Optional[int] = None,
    store_max_bytes: Optional[int] = None,
    retention_max_age_days: Optional[float] = None,
    keep_latest: Optional[int] = None,
    projection: Optional[StorageProjection] = None,
    fires_at_fraction: float = DEFAULT_FIRES_AT_FRACTION,
) -> StorageBound:
    """Resolve the storage bound and the condition under which it fires.

    Defaults come from :mod:`execution.snapshot`'s own documented defaults, so
    there is ONE retention policy in the tree rather than a second one that reads
    the same and disagrees. A bound that is stated here and enforced there is a
    number; a bound stated here and nowhere else is a claim.

    ``reachable`` says whether the projected 30-day figure would actually cross
    the store ceiling. That is the decision the brief asks for: whether to bound
    it. A bound that is never reached still belongs in the code -- the alternative
    is discovering the ceiling on the day it is exceeded.
    """
    from execution.snapshot import (
        DEFAULT_RETENTION_DAYS,
        DEFAULT_RETENTION_KEEP,
        DEFAULT_STORE_MAX_BYTES,
    )

    ceiling = int(store_max_bytes if store_max_bytes is not None else DEFAULT_STORE_MAX_BYTES)
    age = float(
        retention_max_age_days
        if retention_max_age_days is not None
        else DEFAULT_RETENTION_DAYS
    )
    keep = int(keep_latest if keep_latest is not None else DEFAULT_RETENTION_KEEP)
    notes: List[str] = []

    headroom: Optional[int] = None
    if volume_bytes is not None and int(volume_bytes) > 0:
        headroom = max(0, int(int(volume_bytes) * (1.0 - float(fires_at_fraction))))
    else:
        notes.append(
            "no volume size was supplied, so the free-space reserve is UNMEASURED. "
            "execution.snapshot enforces its own DEFAULT_RESERVE_BYTES independently; "
            "this receipt does not restate it as a number it did not measure."
        )

    reachable = False
    if projection is not None and projection.total_bytes is not None:
        reachable = float(projection.total_bytes) > float(ceiling)
        notes.append(
            f"the projected 30-day total is {projection.total_bytes / (1024.0 ** 2):.1f} MiB "
            f"against a {ceiling / (1024.0 ** 3):.2f} GiB ceiling, so the ceiling is "
            + ("REACHABLE and the bound is load-bearing." if reachable else "not reached by this pattern.")
        )
    if projection is not None and projection.unavailable:
        notes.append(
            "reachability is UNKNOWN because "
            + ", ".join(projection.unavailable)
            + " could not be measured; a bound decided on a partial measurement is a "
            "guess wearing a number"
        )

    condition = (
        f"the content-addressed store exceeds {ceiling} B, or the logs root exceeds "
        f"{fires_at_fraction:.0%} of its volume, or the oldest run passes {age:.0f} days "
        "with more than "
        f"{keep} runs kept -- whichever comes first. A run whose LINEAGE has a live "
        "session is never a candidate, and a run whose liveness could not be established "
        "is never pruned, so this bound reclaims space rather than correctness."
    )
    return StorageBound(
        store_max_bytes=ceiling,
        retention_max_age_days=age,
        keep_latest=keep,
        volume_bytes=None if volume_bytes is None else int(volume_bytes),
        fires_at_fraction=float(fires_at_fraction),
        headroom_bytes=headroom,
        condition=condition,
        reachable=reachable,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class BoundEnforcement:
    """What enforcing the storage bound ACTUALLY did, as one receipt.

    **This exists because the bound was, until Wave 2, a number nobody acted on.**
    Measured: ``storage_bound()`` had **zero production callers** anywhere outside
    ``execution/`` -- the growth table, the 30-day projection and the 8 GiB
    ceiling were all measured and printed, and nothing read them at run time. A
    projection with no enforcement is a hope, so :func:`enforce_storage_bound`
    is the enforcement and this is its receipt.

    The four questions a caller has after asking "did you enforce it?":

    * ``fired`` -- was the bound over its threshold at all;
    * ``measured`` -- could the current usage be MEASURED. ``False`` means the
      verdict is UNKNOWN, never "within bound": a partial measurement is not a
      passing measurement (``phases/DOCTRINE.md`` §1);
    * ``prune_report`` -- the retention pass's own report, when one ran;
    * ``bound_after`` -- the usage re-measured AFTER the pass, so "we brought it
      under" is a measurement rather than an intention.
    """

    fired: bool
    measured: bool
    reason: str
    threshold: str
    store_bytes_before: Optional[int]
    store_bytes_after: Optional[int]
    log_bytes_before: Optional[int]
    log_bytes_after: Optional[int]
    runs_before: Optional[int]
    runs_after: Optional[int]
    prune_report: Optional[Mapping[str, Any]] = None
    notes: Tuple[str, ...] = ()

    @property
    def reclaimed_bytes(self) -> Optional[int]:
        """Bytes reclaimed, or ``None`` when either side was unmeasured."""
        if self.store_bytes_before is None or self.store_bytes_after is None:
            return None
        return int(self.store_bytes_before) - int(self.store_bytes_after)

    @property
    def within_bound_after(self) -> Optional[bool]:
        """Whether the store is under the ceiling NOW, or ``None`` if unmeasured."""
        ceiling = int(self.threshold.split("|", 1)[0].strip() or 0)
        if self.store_bytes_after is None or ceiling <= 0:
            return None
        return int(self.store_bytes_after) <= ceiling

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "fired": self.fired,
            "measured": self.measured,
            "reason": self.reason,
            "threshold": self.threshold,
            "store_bytes_before": self.store_bytes_before,
            "store_bytes_after": self.store_bytes_after,
            "log_bytes_before": self.log_bytes_before,
            "log_bytes_after": self.log_bytes_after,
            "runs_before": self.runs_before,
            "runs_after": self.runs_after,
            "reclaimed_bytes": self.reclaimed_bytes,
            "within_bound_after": self.within_bound_after,
            "prune_report": dict(self.prune_report) if self.prune_report else None,
            "notes": list(self.notes),
        }

    def render(self) -> List[str]:
        """Render PLAIN lines. Never raises."""
        lines = [f"bound fired       : {self.fired}", f"reason            : {self.reason}"]
        if not self.measured:
            lines.append(
                "usage             : UNMEASURED - the verdict is UNKNOWN, not 'within bound'"
            )
        lines.append(
            f"store before/after: {_human(self.store_bytes_before)} -> "
            f"{_human(self.store_bytes_after)}"
        )
        reclaimed = self.reclaimed_bytes
        lines.append(
            "reclaimed         : " + (_human(reclaimed) if reclaimed is not None else "unmeasured")
        )
        lines.append(f"runs before/after : {self.runs_before} -> {self.runs_after}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return lines


def _human(value: Optional[int]) -> str:
    if value is None:
        return "UNMEASURED"
    if abs(int(value)) >= 1024 * 1024:
        return f"{int(value) / (1024.0 * 1024.0):.2f} MiB"
    return f"{int(value)} B"


def _tree_bytes(root: Any) -> Optional[int]:
    """Total bytes under a directory, or ``None`` when it cannot be read."""
    path = Path(root)
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return None
    return total


def _run_dirs(root: Any) -> Optional[int]:
    """How many run directories a logs root holds, or ``None``."""
    from execution.snapshot import is_run_directory

    path = Path(root)
    try:
        if not path.is_dir():
            return None
        return sum(1 for entry in path.iterdir() if is_run_directory(entry))
    except OSError:
        return None


def enforce_storage_bound(
    log_root: Any,
    *,
    bound: Optional[StorageBound] = None,
    store_max_bytes: Optional[int] = None,
    dry_run: bool = False,
    keep_latest: Optional[int] = None,
) -> BoundEnforcement:
    """ENFORCE the storage bound against a real logs root.

    **What this closes.** Wave 1 published the growth table, the 30-day
    projection and the 8 GiB ceiling, and measured that ``storage_bound`` had
    zero production callers: a projection with no enforcement is a hope. This is
    the enforcement, and it is deliberately built on :mod:`execution.snapshot`'s
    own retention pass rather than a second reaper, so there is ONE thing in the
    tree that deletes a run directory.

    **The order, and each step's refusal direction:**

    1. **Measure.** An unmeasurable root yields ``measured=False`` and
       ``fired=False`` -- and the receipt says UNKNOWN rather than "within
       bound". Refusing to guess is the whole point.
    2. **Decide.** The store is over its ceiling, OR the logs root is over
       ``fires_at_fraction`` of its volume (when a volume size is known), OR the
       run count is over ``keep_latest``.
    3. **Act.** One :func:`execution.snapshot.prune_run_directories` pass, which
       is where the live-lineage protection, the ``keep_latest`` floor, the
       corrupt-sidecar protection and the per-run prune ledger live. This
       function adds a TRIGGER, never a deletion rule of its own.
    4. **Re-measure.** ``store_bytes_after`` is read back off disk, so "we
       brought it under" is a measurement.

    A live lineage is never pruned by step 3 no matter how far over the ceiling
    the root is: the receipt then says so, because a bound that reclaims space
    by deleting a session's rewind points is a different product.

    **One honest cost, measured rather than assumed.** The prune LEDGER is
    written into the root it just pruned, so a root sitting near its ceiling can
    NET-GROW by the size of that ledger: measured, a 3-run root at 824 B pruned
    two runs and finished at **1,074 B**, i.e. ``reclaimed_bytes == -250``. That
    is the price of "pruning is never silent", it is bounded by the ledger's
    growth rather than by the run directories, and a caller that treats
    ``reclaimed_bytes`` as a monotone improvement will be wrong exactly once per
    pass. The ledger is deliberately not excluded from the measurement: a
    retention receipt that hides its own receipt is the same class of lie this
    module is about.
    """
    from execution.snapshot import prune_run_directories, resolve_retention

    root = Path(log_root)
    notes: List[str] = []
    resolved = bound or storage_bound(store_max_bytes=store_max_bytes)

    before_store = _tree_bytes(root)
    before_log = before_store
    before_runs = _run_dirs(root)
    if before_store is None or before_runs is None:
        return BoundEnforcement(
            fired=False,
            measured=False,
            reason=(
                f"{root} could not be measured, so whether it is over the bound is "
                "UNKNOWN. Refusing to report 'within bound' about a root nobody "
                "could read is the only honest answer."
            ),
            threshold=f"{resolved.store_max_bytes} | {resolved.fires_at_fraction}",
            store_bytes_before=before_store,
            store_bytes_after=None,
            log_bytes_before=before_log,
            log_bytes_after=None,
            runs_before=before_runs,
            runs_after=None,
            notes=tuple(notes),
        )

    over_store = before_store > int(resolved.store_max_bytes)
    over_runs = before_runs > int(resolved.keep_latest)
    over_volume = False
    if resolved.volume_bytes:
        fraction = float(before_log) / float(resolved.volume_bytes)
        over_volume = fraction > float(resolved.fires_at_fraction)
        if over_volume:
            notes.append(
                f"the logs root is at {fraction:.2%} of its volume, past the "
                f"{resolved.fires_at_fraction:.0%} trigger"
            )
    fired = bool(over_store or over_runs or over_volume)

    if not fired:
        return BoundEnforcement(
            fired=False,
            measured=True,
            reason=(
                f"within the bound: store {_human(before_store)} <= "
                f"{_human(resolved.store_max_bytes)}, {before_runs} run(s) <= "
                f"{resolved.keep_latest} kept"
            ),
            threshold=f"{resolved.store_max_bytes} | {resolved.fires_at_fraction}",
            store_bytes_before=before_store,
            store_bytes_after=before_store,
            log_bytes_before=before_log,
            log_bytes_after=before_log,
            runs_before=before_runs,
            runs_after=before_runs,
            notes=tuple(notes),
        )

    why = ", ".join(
        part
        for part, hit in (
            (f"the store is {_human(before_store)} over a {_human(resolved.store_max_bytes)} ceiling", over_store),
            (f"{before_runs} run directories is over the {resolved.keep_latest} kept", over_runs),
            ("the logs root is past its volume trigger", over_volume),
        )
        if hit
    )
    settings = resolve_retention({})
    # The bound's THREE numbers drive the pass, and each substitution is
    # recorded on the settings' own notes. `keep_latest` matters as much as the
    # ceiling: it is a FLOOR, so a pass that kept 20 while the bound published 1
    # would protect every run in a small root and enforce nothing at all --
    # measured before this was wired, and it is why the first dry run above
    # reclaimed 0 B on a root the bound had already declared over its ceiling.
    if (
        int(resolved.keep_latest) != int(settings.keep_latest)
        or int(resolved.store_max_bytes) != int(settings.store_max_bytes)
        or float(resolved.retention_max_age_days)
        != (float("inf") if settings.max_age_s is None else settings.max_age_s / 86400.0)
    ):
        settings = replace(
            settings,
            keep_latest=int(resolved.keep_latest),
            store_max_bytes=int(resolved.store_max_bytes),
            max_age_s=float(resolved.retention_max_age_days) * 86400.0,
            notes=(
                *settings.notes,
                "these three numbers came from execution.storage_cost's bound "
                f"(keep_latest={resolved.keep_latest}, "
                f"store_max_bytes={resolved.store_max_bytes}, "
                f"retention_max_age_days={resolved.retention_max_age_days:.0f}), "
                "not from the retention defaults",
            ),
        )
    report = prune_run_directories(
        root, settings, dry_run=dry_run, max_runs=int(resolved.keep_latest)
    )
    after_store = _tree_bytes(root)
    after_runs = _run_dirs(root)
    if not dry_run and report.lineage_protected:
        notes.append(
            f"{len(report.lineage_protected)} run(s) were NOT reclaimed because a LIVE "
            "lineage needs them; the bound reclaims space rather than correctness, so "
            "a root that cannot come under the ceiling while a lineage is live reports "
            "that instead of deleting a session's rewind points"
        )
    if report.errors:
        notes.append(f"{len(report.errors)} error(s) during the pass: {report.errors[:3]}")
    if not dry_run and after_store is not None and after_store > int(resolved.store_max_bytes):
        notes.append(
            f"the store is still {_human(after_store)} after the pass, over the "
            f"{_human(resolved.store_max_bytes)} ceiling: the remainder is live-lineage "
            "state or protected-by-policy runs, and this receipt names that rather "
            "than reporting success"
        )
    return BoundEnforcement(
        fired=True,
        measured=True,
        reason=f"the bound fired: {why}",
        threshold=f"{resolved.store_max_bytes} | {resolved.fires_at_fraction}",
        store_bytes_before=before_store,
        store_bytes_after=after_store,
        log_bytes_before=before_log,
        log_bytes_after=_tree_bytes(root),
        runs_before=before_runs,
        runs_after=after_runs,
        prune_report=report.to_dict(),
        notes=tuple(notes),
    )


def storage_report(
    *,
    log_root: Any = None,
    usage: Optional[Usage] = None,
    volume_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Measure, project, bound, and return the whole receipt as one value.

    The single call an operator surface needs. It returns a dict rather than
    printing, because a receipt a caller can render is a receipt a test can
    assert on -- and because a storage report nobody can check is a number
    nobody will trust.
    """
    projection = project_30_days(log_root=log_root, usage=usage)
    bound = storage_bound(volume_bytes=volume_bytes, projection=projection)
    return {
        "schema": "neo/storage-cost/v1",
        "generated_at": time.time(),
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
        "projection": projection.to_dict(),
        "bound": bound.to_dict(),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the growth table, the projection and the bound.

    Exists so the numbers are reachable without writing a driver, which is the
    difference between a measurement that is taken and a measurement that is
    described. Exit 0 when the report is complete, 2 when a store could not be
    measured -- a partial report is not a pass.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m execution.storage_cost",
        description="Measure, project and bound the storage cost of a long-lived memory system.",
    )
    parser.add_argument("--log-root", default="", help="logs root to measure journals from")
    parser.add_argument("--volume-gib", type=float, default=0.0, help="volume size, for headroom")
    parser.add_argument("--sessions-per-day", type=int, default=DEFAULT_USAGE.sessions_per_day)
    parser.add_argument("--turns-per-session", type=int, default=DEFAULT_USAGE.turns_per_session)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = args.log_root or None
    usage = Usage(
        sessions_per_day=int(args.sessions_per_day),
        turns_per_session=int(args.turns_per_session),
        days=DEFAULT_USAGE.days,
    )
    volume = int(args.volume_gib * (1024 ** 3)) if args.volume_gib > 0 else None
    projection = project_30_days(log_root=root, usage=usage)
    bound = storage_bound(volume_bytes=volume, projection=projection)

    if args.json:
        print(json.dumps({"projection": projection.to_dict(), "bound": bound.to_dict()},
                         indent=2, sort_keys=True))
    else:
        for line in projection.render(total_gib=float(args.volume_gib or 0.0)):
            print(line)
        print()
        print("bound")
        for line in bound.render():
            print(f"  {line}")
    if projection.unavailable:
        print()
        print("PARTIAL: at least one store could not be measured. Not a pass.")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
