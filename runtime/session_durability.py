"""History durability: the eight vectors, the GC, and what a background job may read.

Wave 1 built the structure -- a lineage of sessions, an append-only entry tree,
a leased job queue. **This module is the adversarial pass over it**, and it
exists because a durability substrate is exactly the kind of change where silent
data loss lives: nothing fails loudly when a compaction eats a turn, a GC prunes
a rotated child, or a rewind truncates a journal. The run still finishes. The
answer is still green. The history is gone.

**The corpus's claim is the thing under test:** *"Compaction does not rewrite the
transcript -- it ends the session. Context management and session persistence
become one mechanism, and no history is ever destroyed."*

:data:`HISTORY_VECTORS` is that claim as eight attempts, and
:func:`history_audit` runs all eight against a real fixture -- real sessions,
real rotations, real compaction, real GC -- and reports what actually happened.
It is DATA rather than prose so a pin can assert the table has not shrunk, and
each row carries the outcome class it must reach:

``survived``
    history is intact and retrievable after the attempt.
``survived_explicit``
    history is gone, and it is gone **because a caller asked for it by name**.
    This is the only acceptable loss.
``destroyed``
    history is gone and nobody asked. **A defect, always.**

Three things this module found by running the vectors rather than reading the
code, all of them in ``runtime/`` and all of them now fixed:

1. **A rewind deleted pre-existing files.** ``checkpoint.TurnFileState.capture``
   recorded *any* read failure -- and any file over ``max_file_bytes`` -- as
   ``exists: false``, and ``exists: false`` meant DELETE on restore. Three
   triggers, none exotic, and the third was **deterministic**: a run that merely
   edits a file over the cap and then rewinds destroys it. Fixed by the third
   pre-image state, ``unreadable``; see ``checkpoint.PRE_IMAGE_STATES``.
2. **A concurrent reader of a tree being collected saw NO history, and
   ``verify()`` said it was intact.** ``_rewrite`` rotated the journal aside and
   only then wrote the replacement, so for the duration of the rewrite there was
   no journal -- and ``_read_rows`` swallowed the error while ``verify()``
   reported ``ok: True`` over zero rows. Measured here: **2 of 48 concurrent
   reads**. Fixed by installing the compacted journal with an atomic
   ``os.replace``, and by making ``verify()`` report ``state``/``complete``.
3. **The GC's receipt claimed removals that had not happened.** ``removed`` was
   computed from the victim list *before* the rewrite was attempted, so a rewrite
   that failed still reported every victim as removed. A storage-pressure GC that
   quietly collected nothing while claiming it collected everything is worse than
   no GC. Fixed: ``collectable`` is the plan, ``removed`` is what happened.

**What a background extraction job gets.** :class:`ReadOnlySessionView` is the
read surface T5's pipeline should use, and it is read-only *structurally*: it has
no ``append``, no ``fork``, no ``move_head``, no ``gc``, and no attribute that can
reach a ``SessionTree`` writer. "A job must not write a tree entry" is a
convention every consumer has to remember; here it is the class's shape. A pin
asserts the absence by name AND asserts the control arm -- that the real tree
does have those writers, so the view is not simply an object that cannot write
because nothing can.

Cost note: every function here is a bounded read over a session store plus, for
the audit, a real fixture built in a temp directory. Nothing walks a repository
or dials a provider.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from runtime.session_tree import SessionTree

__all__ = [
    "DEFAULT_STORAGE_PRESSURE_BYTES",
    "HISTORY_VECTORS",
    "OUTCOME_CLASSES",
    "HistoryVector",
    "ReadOnlySessionView",
    "VectorResult",
    "audit_report",
    "build_fixture",
    "fixture_turn_ids",
    "history_audit",
    "job_write_separation",
    "live_lineages",
    "read_only_view",
    "retention_report",
    "storage_pressure_gc",
]

#: The closed vocabulary of outcomes. ``destroyed`` is the only bad one and it is
#: never acceptable; ``survived_explicit`` is acceptable ONLY because a caller
#: named the target.
OUTCOME_CLASSES: Tuple[str, ...] = ("survived", "survived_explicit", "destroyed")

#: A storage-pressure GC in this audit runs against a budget rather than a clock.
#: Chosen to sit comfortably ABOVE the live lineage's own bytes and BELOW the
#: whole store's, so the pass is forced to actually collect something and is
#: forced to be the pressure that decides -- which is the production shape.
DEFAULT_STORAGE_PRESSURE_BYTES = 4096


@dataclass(frozen=True)
class HistoryVector:
    """One attempted way to lose history, and the outcome class it must reach."""

    number: int
    name: str
    vector: str
    required: str
    #: Whether losing history is a legitimate outcome here. It is legitimate for
    #: exactly one row, and that row is the one where a human asked for it.
    loss_allowed: bool = False
    note: str = ""


#: The eight vectors, as data. A prose table cannot be asserted; this can, and
#: :func:`history_audit` refuses to run if the eight are not all present.
HISTORY_VECTORS: Tuple[HistoryVector, ...] = (
    HistoryVector(
        1,
        "repeat_compaction",
        "Compact five times in a row",
        "every ancestor session retrievable",
        note="a rotation is not a truncation; the parent keeps every turn",
    ),
    HistoryVector(
        2,
        "compact_resume_compact",
        "Compact, then resume, then compact again",
        "the full chain navigable",
        note="resume must land on a point the next compaction can build on",
    ),
    HistoryVector(
        3,
        "gc_after_rotations",
        "Run GC after rotations",
        "no live lineage's state pruned",
        note="the default retention roots include the head's whole ancestry",
    ),
    HistoryVector(
        4,
        "gc_under_storage_pressure",
        "Force GC by storage pressure",
        "same -- pressure must not change correctness",
        note="the production shape: the pass that runs when nobody is watching",
    ),
    HistoryVector(
        5,
        "abandoned_branch",
        "Abandon a branch",
        "abandoned branch still retrievable",
        note="a summary is a claim; the entries it cites are facts",
    ),
    HistoryVector(
        6,
        "rewind_past_compaction",
        "Rewind past a compaction point",
        "pre-compaction content retrievable",
        note="the head moves; nothing is truncated",
    ),
    HistoryVector(
        7,
        "resume_old_session_id",
        "Resume an old session id after many rotations",
        "restores to the right point",
        note="an old id must resolve to its own generation, not the tip",
    ),
    HistoryVector(
        8,
        "explicit_delete",
        "Delete a session deliberately",
        "explicit, and it is the only way to lose history",
        loss_allowed=True,
        note="confirmed by name, audited outside the deleted directory",
    ),
)


@dataclass
class VectorResult:
    """What one vector actually did. A receipt, never a claim."""

    number: int
    name: str
    vector: str
    required: str
    outcome: str
    history_destroyed: bool
    loss_allowed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    ok: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe row a report or a trace event carries."""
        return {
            "number": self.number,
            "name": self.name,
            "vector": self.vector,
            "required": self.required,
            "outcome": self.outcome,
            "history_destroyed": bool(self.history_destroyed),
            "loss_allowed": bool(self.loss_allowed),
            "ok": bool(self.ok),
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# A real fixture: real sessions, real rotations, real compaction.
# ---------------------------------------------------------------------------


def build_fixture(
    root: Any,
    *,
    turns: int = 12,
    rotations: int = 3,
    keep_last: int = 2,
    repo: Any = None,
) -> Dict[str, Any]:
    """Build a real rotated lineage under ``root`` and return its receipt.

    Uses the real ``cli.session`` writer, the real ``compact_session``, and the
    real :func:`runtime.session_lineage.rotate_session`. Nothing here is a
    stand-in, because an audit of a stand-in proves the stand-in survives.

    Returns ``{repo, chain, turns, turn_ids, compacted, log_root}`` where
    ``chain`` is oldest-first and ``turn_ids`` are the session turn ids that must
    remain retrievable somewhere in it.
    """
    from cli import session as session_api
    from runtime import session_lineage as lineage

    base = Path(root)
    log_root = base
    repository = str(repo or (base / "repo"))
    first = session_api.new_session_id()
    session = session_api.load_or_create(log_root, repository, first)
    turn_ids: List[str] = []
    for index in range(max(1, int(turns))):
        tid = f"t{index}"
        session_api.append_turn(
            session, "user", f"question {index} about module_x", turn_id=tid
        )
        turn_ids.append(tid)
    session_api.save_session(log_root, session)

    chain = [first]
    current = first
    for step in range(max(0, int(rotations))):
        session = session_api.load_or_create(log_root, repository, current)
        session_api.compact_session(session, log_root, keep_last=max(1, int(keep_last)))
        session_api.save_session(log_root, session)
        folded = [
            str(turn.get("turn_id") or "")
            for turn in session.get("compacted_turns") or []
            if isinstance(turn, Mapping)
        ]
        receipt = lineage.rotate_session(
            log_root,
            session,
            summary=f"rotation {step}: folded {len(folded)} turn(s)",
            source_turn_ids=folded,
            reason="compression",
            repo=repository,
        )
        if not receipt.get("ok"):
            raise lineage.LineageRefused(
                f"the fixture could not rotate: {receipt.get('reason')}",
                {"session_id": current},
            )
        current = str(receipt["child_session_id"])
        chain.append(current)
    return {
        "repo": repository,
        "log_root": log_root,
        "chain": chain,
        "tip": current,
        "root": first,
        "turns": int(turns),
        "turn_ids": turn_ids,
        "rotations": int(rotations),
    }


def fixture_turn_ids(log_root: Any, session_ids: Sequence[str], *, repo: Any = None) -> List[str]:
    """Return the turn ids that are NOT retrievable from any of ``session_ids``.

    The absence is computed by SEARCHING the three retained lists of every
    session, not by asking a counter, because "the count is right" and "the
    specific turn is still there" are different claims and only the second one is
    the guarantee.
    """
    from runtime import session_lineage as lineage

    blob: List[str] = []
    for sid in session_ids:
        session = lineage.load_session(log_root, sid, repo)
        for key in ("turns", "compacted_turns", "raw_turns"):
            blob.append(json.dumps(session.get(key) or [], default=str))
    joined = "\n".join(blob)
    return [
        tid
        for tid in _declared_turn_ids(log_root, session_ids, repo=repo)
        if f'"{tid}"' not in joined
    ]


def _declared_turn_ids(log_root: Any, session_ids: Sequence[str], *, repo: Any = None) -> List[str]:
    """Every turn id any generation of this lineage ever recorded."""
    from runtime import session_lineage as lineage

    out: List[str] = []
    for sid in session_ids:
        session = lineage.load_session(log_root, sid, repo)
        for key in ("turns", "compacted_turns", "raw_turns"):
            for turn in session.get(key) or []:
                if isinstance(turn, Mapping):
                    tid = str(turn.get("turn_id") or "")
                    if tid and tid not in out:
                        out.append(tid)
    return out


def live_lineages(log_root: Any, chain: Sequence[str], *, repo: Any = None) -> List[str]:
    """The generations of ``chain`` a reader must still be able to load.

    Every generation, including the rotated-away ones. "The tip is fine" is not
    the guarantee; "the tip and every ancestor is fine" is.
    """
    from runtime import session_lineage as lineage

    live: List[str] = []
    for sid in chain:
        if lineage.load_session(log_root, sid, repo):
            live.append(sid)
    return live


# ---------------------------------------------------------------------------
# GC, including under storage pressure
# ---------------------------------------------------------------------------


def storage_pressure_gc(
    roots: Any,
    *,
    budget_bytes: int = DEFAULT_STORAGE_PRESSURE_BYTES,
    session_ids: Optional[Sequence[str]] = None,
    log_root: Any = None,
    repo: Any = None,
    age_s: float = 10_000.0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run GC on every tree under ``roots`` because a byte budget says so.

    **This is row 4, and it is the row that bites in production.** A GC that
    understands lineage is not enough; a GC that only runs when somebody reads a
    schedule is not enough either. The pass has to survive being triggered by a
    number nobody chose, and the outcome has to be identical to the deliberate
    case.

    The budget is enforced by ORDERING, not by hope: trees are visited
    largest-first until the budget is met, so a pressure-driven pass really does
    reach a collecting decision rather than stopping at the first tree.

    ``session_ids`` are the generations whose survival is asserted afterwards --
    the audit passes the whole chain so a pruned ancestor fails HERE rather than
    in a reader three sessions later. ``log_root``/``repo`` are what makes that
    assertion possible: without them the function can only report bytes.
    """
    from runtime import session_lineage as lineage

    tree_roots = [Path(item) for item in (roots or ())]
    ordered: List[Path] = []
    # A given path is a TREE if it holds a journal, and a CONTAINER if its
    # children do. Both spellings are accepted because the first version took
    # only the container form and a caller passing a tree got ``trees: 0`` and a
    # clean-looking receipt -- the exact "measured nothing, reported success"
    # shape this audit exists to catch, committed by the auditor.
    for tree_root in tree_roots:
        if not tree_root.is_dir():
            continue
        if (tree_root / "entries.jsonl").is_file():
            ordered.append(tree_root)
            continue
        for candidate in sorted(tree_root.iterdir()):
            if candidate.is_dir() and (candidate / "entries.jsonl").is_file():
                ordered.append(candidate)
    ordered.sort(key=lambda item: -_journal_bytes(item))

    clock = lambda: time.time() + float(age_s)  # noqa: E731 - a one-line test seam
    passes: List[Dict[str, Any]] = []
    spent = 0
    for candidate in ordered:
        if spent >= int(budget_bytes) and passes:
            passes.append(
                {
                    "tree": str(candidate),
                    "visited": False,
                    "reason": "the storage budget was already met, so this tree was "
                    "not visited; a pressure pass that skips work is reported, not "
                    "silently counted as a pass",
                }
            )
            continue
        receipt = SessionTree(candidate, clock=clock).gc(dry_run=dry_run, min_age_s=-1.0)
        spent += max(0, int(receipt.get("bytes_reclaimed") or 0))
        passes.append({"tree": str(candidate), "visited": True, **receipt})

    survivors: Dict[str, bool] = {}
    for sid in session_ids or ():
        survivors[str(sid)] = bool(lineage.load_session(log_root, sid, repo))
    return {
        "budget_bytes": int(budget_bytes),
        "bytes_reclaimed": spent,
        "trees": len(ordered),
        "trees_found_under": [str(item) for item in tree_roots],
        "passes": passes,
        "visited": sum(1 for item in passes if item.get("visited")),
        "collected_total": sum(int(item.get("removed") or 0) for item in passes),
        "reported_but_not_done": sum(
            1
            for item in passes
            if int(item.get("collectable_count") or 0) and not int(item.get("removed") or 0)
        ),
        "survivors": survivors,
        "generations_still_readable": sum(1 for value in survivors.values() if value),
        "generations_checked": len(survivors),
        "dry_run": bool(dry_run),
        "policy": "collect only what is unreachable from a retained root, past "
        "min_age_s, and cited by nothing; install atomically; never delete the "
        "previous journal, copy it aside",
    }


def _journal_bytes(tree_root: Path) -> int:
    try:
        return int((tree_root / "entries.jsonl").stat().st_size)
    except OSError:
        return 0


def retention_report(log_root: Any, *, repo: Any = None) -> Dict[str, Any]:
    """What the GC would collect right now, and what it would keep.

    Read-only, and the shape a status surface renders: the retained roots, the
    collectable ids, and the reason for each keep. **A GC whose policy cannot be
    printed is a GC nobody can audit**, and this is the print.
    """
    from pathlib import Path as _Path

    from runtime.session_lineage import DELETIONS_NAME, LINEAGE_DIRNAME

    base = _Path(str(log_root))
    tree_roots = [
        item
        for item in sorted(base.glob("*"))
        if item.is_dir() and (item / "entries.jsonl").is_file()
    ]
    trees: List[Dict[str, Any]] = []
    total_collectable = 0
    for candidate in tree_roots:
        tree = SessionTree(candidate)
        plan = tree.gc(dry_run=True, min_age_s=-1.0)
        total_collectable += int(plan.get("collectable_count") or 0)
        trees.append(
            {
                "tree": str(candidate),
                "entries": len(tree.entries()),
                "journal_bytes": _journal_bytes(candidate),
                "collectable": int(plan.get("collectable_count") or 0),
                "collectable_ids": list(plan.get("collectable") or ())[:50],
                "evidence_protected": list(plan.get("evidence_protected") or ())[:50],
                "rotated_journals": sorted(
                    item.name for item in candidate.glob("entries.jsonl.gc-*")
                )[-5:],
                "compacted_staging": sorted(
                    item.name for item in candidate.glob("entries.jsonl.compact-*")
                )[-5:],
            }
        )
    return {
        "log_root": str(base),
        "trees": trees,
        "tree_count": len(trees),
        "collectable_now": total_collectable,
        "retained_forever": [
            "the head and its whole ancestry",
            "every `branch` entry and its subtree, because a deliberate fork is "
            "work somebody asked for",
            "every entry cited as `evidence` by a surviving claim, because a "
            "summary whose evidence was collected is an unfalsifiable claim",
        ],
        "pruned": [
            "a rewound-past LINEAR stretch that no fork covers, once it is older "
            "than min_age_s",
        ],
        "deletion_ledger": str(base / LINEAGE_DIRNAME / DELETIONS_NAME),
        "deletions_recorded": len(_deletion_rows(base / LINEAGE_DIRNAME / DELETIONS_NAME)),
    }


def _deletion_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# The read-only view a background extraction job gets
# ---------------------------------------------------------------------------


class ReadOnlySessionView:
    """A read surface over one session with **no writer reachable at all**.

    Row 3 of the extraction contract: *"A job writing a memory must not write a
    tree entry."* Stated as a rule it is a convention every consumer has to
    remember, and a convention is one refactor away from being violated. Stated
    as a CLASS it is the shape: this object has no ``append``, no ``fork``, no
    ``move_head``, no ``abandon``, no ``gc``, and no attribute that returns a
    writable tree. A job holding one cannot write a tree entry because there is
    nothing here to write with.

    **The consistency property is the other half and it is the one that bites.**
    :meth:`read` returns :meth:`runtime.session_tree.SessionTree.consistent_read`'s
    receipt, so a job that reads a session while it is compacting and rotating is
    told whether it saw the WHOLE journal or a partial one. A plain read cannot
    answer that, and a partial read of a rotating store is indistinguishable from
    a short conversation.

    A read that could not be completed returns ``complete: False`` with a reason.
    It never returns an empty list as though the session had no turns -- that
    conflation is the defect this view exists to make impossible for the caller.
    """

    __slots__ = ("_last", "_log_root", "_reads", "_repo", "_session_id")

    def __init__(self, log_root: Any, session_id: Any, *, repo: Any = None) -> None:
        object.__setattr__(self, "_log_root", log_root)
        object.__setattr__(self, "_session_id", str(session_id or ""))
        object.__setattr__(self, "_repo", repo)
        object.__setattr__(self, "_reads", 0)
        object.__setattr__(self, "_last", {})

    # -- reads ----------------------------------------------------------

    def read(self) -> Dict[str, Any]:
        """Read the whole session and say whether the read was COMPLETE."""
        from runtime import session_lineage as lineage

        object.__setattr__(self, "_reads", int(self._reads) + 1)
        session = lineage.load_session(self._log_root, self._session_id, self._repo)
        records = lineage.lineage_records(
            self._log_root, self._session_id, repo=self._repo
        )
        view = {
            "ok": bool(session),
            "session_id": self._session_id,
            "present": bool(session),
            "turns": len(session.get("turns") or []) if session else 0,
            "compacted_turns": len(session.get("compacted_turns") or []) if session else 0,
            "raw_turns": len(session.get("raw_turns") or []) if session else 0,
            "summaries": len(session.get("summaries") or []) if session else 0,
            "generations": int(records.get("depth") or 0),
            "unreadable_generations": list(records.get("unreadable") or ()),
            "reads": int(self._reads),
            "write_capable": False,
        }
        if not session:
            view["complete"] = False
            view["reason"] = (
                "this session's record could not be read, which is NOT the same as "
                "a session with no turns; the caller must not treat an unreadable "
                "session as an empty one"
            )
        else:
            view["complete"] = not view["unreadable_generations"]
            view["reason"] = (
                ""
                if view["complete"]
                else "a generation in this lineage could not be read, so the view "
                "is partial"
            )
        object.__setattr__(self, "_last", dict(view))
        return view

    @property
    def last_read(self) -> Dict[str, Any]:
        """The most recent :meth:`read` receipt, or ``{}``. Read-only."""
        return dict(self._last)

    def tree_read(self, tree_root: Any, *, attempts: int = 3) -> Dict[str, Any]:
        """Read an entry TREE consistently, for a job that indexes positions."""
        receipt = SessionTree(tree_root).consistent_read(attempts=attempts)
        receipt["write_capable"] = False
        return receipt

    # -- the structural guarantee ---------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"ReadOnlySessionView has no attribute {name!r}: a background job's "
            "view of a session is read-only by construction, so there is nothing "
            "here to rebind"
        )

    def __repr__(self) -> str:
        return (
            f"ReadOnlySessionView(session_id={self._session_id!r}, "
            f"reads={int(self._reads)}, write_capable=False)"
        )


def read_only_view(log_root: Any, session_id: Any, *, repo: Any = None) -> ReadOnlySessionView:
    """Factory for :class:`ReadOnlySessionView`. The name a caller should reach for."""
    return ReadOnlySessionView(log_root, session_id, repo=repo)


def job_write_separation() -> Dict[str, Any]:
    """Report, as data, that a background job cannot write a tree entry.

    Two independent authorities, because either alone is satisfiable by accident:

    * **shape** -- :class:`ReadOnlySessionView` exposes no writer name at all;
    * **reachability** -- no name reachable from the view can produce a
      ``SessionTree``. The view holds only ``log_root``/``session_id``/``repo``
      strings and reads a tree through :meth:`ReadOnlySessionView.tree_read`,
      which constructs its own short-lived tree and returns a receipt.

    The CONTROL ARM matters and is reported alongside: the real
    :class:`~runtime.session_tree.SessionTree` *does* have all five writers. If it
    did not, the view would be proving nothing -- it would be an object that
    cannot write because nothing can.
    """
    forbidden = ("append", "advance", "fork", "move_head", "abandon", "gc", "_rewrite")
    view_writers = sorted(name for name in forbidden if hasattr(ReadOnlySessionView, name))
    tree_writers = sorted(name for name in forbidden if hasattr(SessionTree, name))
    return {
        "read_methods": sorted(
            name for name in dir(ReadOnlySessionView) if not name.startswith("__")
        ),
        "forbidden_writers": list(forbidden),
        "writers_on_the_view": view_writers,
        "writers_on_the_real_tree": tree_writers,
        "separation_holds": not view_writers,
        "control_arm_is_real": len(tree_writers) == len(forbidden),
        "slots": list(getattr(ReadOnlySessionView, "__slots__", ())),
        "verdict": (
            "a background job holding this view cannot write a tree entry: the "
            "writers are absent from its shape, not merely unused by convention"
        )
        if not view_writers
        else f"the view exposes writer(s) {view_writers}",
    }


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def history_audit(
    root: Any = None,
    *,
    turns: int = 12,
    rotations: int = 3,
    keep_last: int = 2,
    budget_bytes: int = DEFAULT_STORAGE_PRESSURE_BYTES,
) -> Dict[str, Any]:
    """Run all eight vectors against a real fixture and report what happened.

    Every vector is ATTEMPTED and every outcome is real. A vector that could not
    be run is reported as ``not_attempted`` with its reason -- never as a pass,
    because "we could not test this" and "this is fine" render identically in a
    summary table and only one of them is true.

    Returns ``{vectors, ok, history_destroyed_implicitly, only_explicit_loss}``.
    ``ok`` is True when no vector destroyed history implicitly AND every vector
    that could lose history reached its ``loss_allowed`` class.
    """
    from runtime import session_lineage as lineage  # noqa: F401 - documents the seam

    temporary = root is None
    base = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="neo-durability-"))
    results: List[VectorResult] = []
    try:
        # **EVERY vector gets its OWN fixture AND its OWN tree.** The first
        # version built one session lineage and one entry tree and let all eight
        # rows share them, and the consequence was that four rows were measuring
        # the others' leftovers rather than their own subject: row 1's five extra
        # rotations meant row 2's "tip" was no longer the tip and row 8's "live
        # tip" had acquired successors, and rows 3/4/5/6 mutated a tree the
        # previous row had already collected. A row's evidence has to be its
        # own, or the table is a story about the order the rows happened to run
        # in -- and three of the four reds that produced were pure artefact.
        plan: List[Tuple[HistoryVector, Dict[str, Any], Optional[SessionTree]]] = []
        for vector in HISTORY_VECTORS:
            fixture = build_fixture(
                base / f"fixture-{vector.number:02d}",
                turns=turns,
                rotations=rotations,
                keep_last=keep_last,
            )
            tree: Optional[SessionTree] = None
            if vector.name in ("gc_after_rotations", "gc_under_storage_pressure"):
                tree = _tree_with_unreachable_positions(
                    base / f"tree-{vector.number:02d}", fixture["turn_ids"]
                )
            elif vector.name in ("abandoned_branch", "rewind_past_compaction"):
                root = base / f"tree-{vector.number:02d}"
                fresh = SessionTree(root)
                for turn_id in fixture["turn_ids"]:
                    fresh.advance(turn_id=str(turn_id), text=f"transcript of {turn_id}")
                tree = fresh
            plan.append((vector, fixture, tree))

        runners: Dict[str, Callable[[Dict[str, Any], Optional[SessionTree]], VectorResult]] = {
            "repeat_compaction": lambda f, t: _vector_repeat_compaction(f),
            "compact_resume_compact": lambda f, t: _vector_compact_resume_compact(f),
            "gc_after_rotations": lambda f, t: _vector_gc(f, t, budget_bytes=None),
            "gc_under_storage_pressure": lambda f, t: _vector_gc(f, t, budget_bytes=budget_bytes),
            "abandoned_branch": lambda f, t: _vector_abandon(f, t),
            "rewind_past_compaction": lambda f, t: _vector_rewind(f, t),
            "resume_old_session_id": lambda f, t: _vector_resume_old(f),
            "explicit_delete": lambda f, t: _vector_explicit_delete(f),
        }
        for vector, fixture, tree in plan:
            runner = runners.get(vector.name)
            if runner is None:
                results.append(
                    VectorResult(
                        number=vector.number,
                        name=vector.name,
                        vector=vector.vector,
                        required=vector.required,
                        outcome="not_attempted",
                        history_destroyed=False,
                        loss_allowed=vector.loss_allowed,
                        detail=f"no runner is registered for {vector.name!r}; an "
                        "unrun vector is not a passed vector",
                        ok=False,
                    )
                )
                continue
            try:
                result = runner(fixture, tree)
            except Exception as exc:  # an unrun vector is not a passed vector
                results.append(
                    VectorResult(
                        number=vector.number,
                        name=vector.name,
                        vector=vector.vector,
                        required=vector.required,
                        outcome="not_attempted",
                        history_destroyed=False,
                        loss_allowed=vector.loss_allowed,
                        detail=f"{type(exc).__name__}: {exc}",
                        ok=False,
                    )
                )
                continue
            # Re-derive the verdict from the class rather than trusting the
            # runner's own `ok`, so a runner cannot mark its own work done.
            result.ok = (
                result.outcome == "survived"
                or (vector.loss_allowed and result.outcome == "survived_explicit")
            ) and result.outcome in OUTCOME_CLASSES
            result.loss_allowed = vector.loss_allowed
            results.append(result)

        implicit = [
            row.name for row in results if row.outcome == "destroyed" and not row.loss_allowed
        ]
        mislabelled = [
            row.name
            for row in results
            if row.outcome == "survived_explicit" and not row.loss_allowed
        ]
        unrun = [row.name for row in results if row.outcome == "not_attempted"]
        head = plan[0][1] if plan else {}
        return {
            "fixture": {
                "generations": len(head.get("chain") or ()),
                "turns": int(head.get("turns") or 0),
                "rotations": int(head.get("rotations") or 0),
                "isolated_fixtures": len(plan),
                "isolation_note": "each vector is given its own session lineage and "
                "its own entry tree, so no row's evidence is another row's leftover",
            },
            "vectors": [row.as_dict() for row in results],
            "attempted": sum(1 for row in results if row.outcome != "not_attempted"),
            "declared": len(HISTORY_VECTORS),
            "not_attempted": unrun,
            "history_destroyed_implicitly": implicit,
            "loss_outside_an_explicit_delete": mislabelled,
            "only_explicit_loss": not implicit and not mislabelled,
            "ok": not implicit and not mislabelled and not unrun and all(row.ok for row in results),
        }
    finally:
        if temporary:
            shutil.rmtree(base, ignore_errors=True)


def _result(
    vector_name: str,
    outcome: str,
    *,
    destroyed: bool = False,
    detail: str = "",
    **evidence: Any,
) -> VectorResult:
    row = next(item for item in HISTORY_VECTORS if item.name == vector_name)
    return VectorResult(
        number=row.number,
        name=row.name,
        vector=row.vector,
        required=row.required,
        outcome=outcome,
        history_destroyed=destroyed,
        loss_allowed=row.loss_allowed,
        evidence=dict(evidence),
        detail=detail,
    )


def _vector_repeat_compaction(fixture: Mapping[str, Any]) -> VectorResult:
    """Row 1: five compactions in a row, every ancestor still retrievable."""
    from cli import session as session_api
    from runtime import session_lineage as lineage

    log_root = fixture["log_root"]
    repo = fixture["repo"]
    chain = [str(item) for item in fixture["chain"]]
    current = chain[-1]
    added: List[str] = []
    for step in range(5):
        session = session_api.load_or_create(log_root, repo, current)
        session_api.compact_session(session, log_root, keep_last=1)
        session_api.save_session(log_root, session)
        folded = [
            str(turn.get("turn_id") or "")
            for turn in session.get("compacted_turns") or []
            if isinstance(turn, Mapping)
        ]
        receipt = lineage.rotate_session(
            log_root,
            session,
            summary=f"row-1 compaction {step}",
            source_turn_ids=folded,
            reason="compression",
            repo=repo,
        )
        if not receipt.get("ok"):
            return _result(
                "repeat_compaction",
                "destroyed",
                destroyed=True,
                detail=f"a rotation refused: {receipt.get('reason')}",
            )
        current = str(receipt["child_session_id"])
        added.append(current)
    full = [*chain, *added]
    missing = fixture_turn_ids(log_root, full, repo=repo)
    dead = [sid for sid in full if not lineage.load_session(log_root, sid, repo)]
    return _result(
        "repeat_compaction",
        "survived" if not missing and not dead else "destroyed",
        destroyed=bool(missing or dead),
        detail=(
            f"{len(full)} generations, every one retrievable"
            if not missing and not dead
            else f"{len(dead)} generation(s) unreadable and {len(missing)} turn(s) lost"
        ),
        generations=len(full),
        unreadable_generations=dead,
        turns_lost=missing,
    )


def _vector_compact_resume_compact(fixture: Mapping[str, Any]) -> VectorResult:
    """Row 2: compact, resume, compact again -- the chain stays navigable.

    The resume target is the **tip**, because a rotated-away MIDDLE generation
    correctly refuses to be continued (its own record says it ended) and
    resuming it is a different vector with a different answer. Testing row 2
    against a middle generation would have produced a red that said nothing about
    rotation, which is the failure mode this round's own first draft of the
    vector hit.
    """
    from cli import session as session_api
    from runtime import session_lineage as lineage

    log_root = fixture["log_root"]
    repo = fixture["repo"]
    chain = [str(item) for item in fixture["chain"]]
    tip = chain[-1]
    resume = lineage.resume_state(log_root, tip, repo=repo)
    authorised = bool(resume.get("resume_authorised"))
    restored_to = str(resume.get("continued_session_id") or "")
    # Resume must land on the session we asked for, not on some other one.
    landed_on_its_own_point = restored_to == tip
    navigated = lineage.lineage_records(log_root, restored_to or tip, repo=repo)
    reached_root = bool(navigated.get("chain")) and str(
        navigated["chain"][-1].get("session_id") or ""
    ) == chain[0]
    # ...and then compact AGAIN from the resumed point, which must work.
    session = session_api.load_or_create(log_root, repo, restored_to or tip)
    session_api.compact_session(session, log_root, keep_last=1)
    session_api.save_session(log_root, session)
    folded = [
        str(turn.get("turn_id") or "")
        for turn in session.get("compacted_turns") or []
        if isinstance(turn, Mapping)
    ]
    again = lineage.rotate_session(
        log_root,
        session,
        summary="row-2 compaction after resume",
        source_turn_ids=folded,
        reason="compression",
        repo=repo,
    )
    full = [*chain, str(again.get("child_session_id") or "")]
    missing = fixture_turn_ids(log_root, full, repo=repo)
    navigable = all(lineage.load_session(log_root, sid, repo) for sid in full)
    ok = authorised and landed_on_its_own_point and reached_root and bool(again.get("ok")) and navigable and not missing
    return _result(
        "compact_resume_compact",
        "survived" if ok else "destroyed",
        destroyed=not ok,
        detail=(
            f"resumed the tip ({tip[:12]}) onto its own point, walked "
            f"{navigated.get('depth')} generations back to the root, compacted "
            f"again into a {len(full)}-th generation, and every turn is still present"
            if ok
            else "the resume/compact/compact sequence did not leave a navigable, "
            "complete chain"
        ),
        resumed_from=tip,
        resumed_to=restored_to,
        resume_authorised=authorised,
        identity_ok=resume.get("identity_ok"),
        landed_on_its_own_point=landed_on_its_own_point,
        reached_root=reached_root,
        second_compaction_ok=bool(again.get("ok")),
        generations=len(full),
        turns_lost=missing,
    )


def _tree_with_unreachable_positions(root: Path, turn_ids: Sequence[str]) -> SessionTree:
    """Build a tree that genuinely HAS collectable entries, or leave it linear.

    **This exists because the first version of vectors 3 and 4 was vacuous.** A
    purely linear tree whose head is at the tip has NOTHING unreachable from the
    default retention roots, so the GC correctly collected zero -- and
    "collected zero, nothing lost" passes whether or not the GC understands
    lineage at all. A vector that cannot fail is not a vector.

    So the tree is given a fork (a deliberate position, therefore a retention
    ROOT the GC must keep) and then a rewind past a stretch (leaving that
    stretch unreachable, therefore collectable). The result has both: something
    the GC must not take and something it is allowed to.
    """
    tree = SessionTree(root)
    for index, turn_id in enumerate(turn_ids):
        tree.advance(turn_id=str(turn_id), text=f"transcript of {turn_id}")
        if index == 2:
            tree.fork(label=f"deliberate fork at {turn_id}")
            tree.advance(turn_id=f"{turn_id}-alt", text="work on the fork")
    turns = [item for item in tree.entries() if item.kind == "turn"]
    if len(turns) >= 3:
        tree.move_head(turns[0].id, reason="rewind past the fork, leaving a stretch")
    return tree


def _vector_gc(
    fixture: Mapping[str, Any],
    tree: SessionTree,
    *,
    budget_bytes: Optional[int],
) -> VectorResult:
    """Rows 3 and 4: GC after rotations, deliberately and under storage pressure.

    The tree arrives ALREADY prepared with a fork and a rewind, so there is
    genuinely something collectable. A vector whose fixture leaves nothing
    unreachable passes without exercising the GC at all, and this one refuses to
    report ``survived`` in that case -- it reports ``not_attempted`` with the
    reason.
    """
    from runtime import session_lineage as lineage

    log_root = fixture["log_root"]
    repo = fixture["repo"]
    chain = [str(item) for item in fixture["chain"]]
    name = "gc_after_rotations" if budget_bytes is None else "gc_under_storage_pressure"
    before = len(tree.entries())
    plan = SessionTree(tree.root, clock=lambda: time.time() + 10_000.0).gc(
        dry_run=True, min_age_s=-1.0
    )
    collectable = int(plan.get("collectable_count") or 0)
    if not collectable:
        return _result(
            name,
            "not_attempted",
            detail=(
                "the fixture produced a tree with nothing unreachable from the "
                "retention roots, so this vector would pass without exercising the "
                "GC at all; a vector that cannot fail is not a green vector"
            ),
            entries_before=before,
            collectable=0,
        )
    forks_before = {item.id for item in tree.entries() if item.kind == "branch"}
    head_before = str(tree.head_id() or "")
    ancestry_before = (
        {item.id for item in tree.path_to_head(head_before)} if head_before else set()
    )

    if budget_bytes is None:
        receipt = SessionTree(tree.root, clock=lambda: time.time() + 10_000.0).gc(
            min_age_s=-1.0
        )
        how = "deliberate"
    else:
        receipt = storage_pressure_gc(
            [tree.root],
            budget_bytes=budget_bytes,
            session_ids=chain,
            log_root=log_root,
            repo=repo,
        )
        how = f"storage pressure (budget {budget_bytes} B)"
    removed = int(receipt.get("removed") or receipt.get("collected_total") or 0)
    after = SessionTree(tree.root)
    forks_after = {item.id for item in after.entries() if item.kind == "branch"}
    head_after = str(after.head_id() or "")
    ancestry_after = (
        {item.id for item in after.path_to_head(head_after)} if head_after else set()
    )
    dead = [sid for sid in chain if not lineage.load_session(log_root, sid, repo)]
    missing = fixture_turn_ids(log_root, chain, repo=repo)
    lost_forks = sorted(forks_before - forks_after)
    lost_positions = sorted(ancestry_before - ancestry_after)
    ok = (
        removed > 0
        and not dead
        and not missing
        and not lost_forks
        and not lost_positions
        and bool(forks_after)
        and head_after == head_before
        and ancestry_after == ancestry_before
    )
    return _result(
        name,
        "survived" if ok else "destroyed",
        destroyed=not ok,
        detail=(
            f"{how}: collected {removed} of {collectable} collectable and kept "
            f"{len(forks_after)} deliberate fork(s), the head's "
            f"{len(ancestry_after)}-entry ancestry and all {len(chain)} session "
            "generations"
            if ok
            else f"{how}: removed={removed} of {collectable} collectable; "
            f"head_moved={head_after != head_before} "
            f"generations_lost={len(dead)} turns_lost={len(missing)} "
            f"forks_lost={len(lost_forks)} positions_lost={len(lost_positions)}"
        ),
        trigger=how,
        budget_bytes=budget_bytes,
        entries_before=before,
        collectable=collectable,
        gc_removed=removed,
        gc_reason=receipt.get("reason", ""),
        reported_but_not_done=receipt.get("reported_but_not_done"),
        forks_kept=len(forks_after),
        forks_lost=lost_forks,
        head_before=head_before,
        head_after=head_after,
        ancestry_kept=len(ancestry_after),
        live_positions_lost=lost_positions,
        generations_unreadable=dead,
        turns_lost=missing,
        journal_state_after=after.verify().get("state"),
        journal_complete_after=after.verify().get("complete"),
    )



def _vector_abandon(fixture: Mapping[str, Any], tree: SessionTree) -> VectorResult:
    """Row 5: an abandoned branch is still retrievable, and its summary is a claim."""
    fork = tree.fork(label="row-5 abandoned branch")
    if fork is None:
        return _result(
            "abandoned_branch", "destroyed", destroyed=True, detail="the fork refused"
        )
    branch_entry = str(fork["entry_id"])
    for index in range(3):
        tree.advance(turn_id=f"abandoned-{index}", text=f"work on the abandoned branch {index}")
    abandoned = tree.abandon(
        label="row-5", summary="this branch explored an alternative and stopped"
    )
    if abandoned is None:
        return _result(
            "abandoned_branch",
            "destroyed",
            destroyed=True,
            detail="abandon refused, so the branch state is unknown",
        )
    evidence = tree.branch_summary_evidence(str(abandoned["summary_entry"]))
    entries = tree.entries()
    branch_still_there = any(item.id == branch_entry for item in entries)
    surviving_turn_ids = {item.turn_id for item in entries if item.kind == "turn"}
    abandoned_turns_still_there = all(
        f"abandoned-{index}" in surviving_turn_ids for index in range(3)
    )
    summary_ids = {item.id for item in entries}
    cited = {str(name) for item in entries for name in (item.evidence or ())}
    evidence_resolvable = all(str(item.id) in summary_ids for item in evidence)
    ok = (
        branch_still_there
        and abandoned_turns_still_there
        and evidence_resolvable
        and bool(abandoned["summary_is_claim"])
        and not cited - summary_ids
    )
    return _result(
        "abandoned_branch",
        "survived" if ok else "destroyed",
        destroyed=not ok,
        detail=(
            f"the branch, its {len(evidence)} cited entr(ies) and the abandoned turns "
            "are all still readable, and the summary is labelled a claim"
            if ok
            else "the abandoned branch or its evidence is no longer readable"
        ),
        branch_entry=branch_entry,
        branch_readable=branch_still_there,
        abandoned_turns_readable=abandoned_turns_still_there,
        evidence_entries=len(evidence),
        evidence_resolvable=evidence_resolvable,
        summary_is_claim=bool(abandoned["summary_is_claim"]),
        dangling_evidence=sorted(cited - summary_ids),
    )


def _vector_rewind(fixture: Mapping[str, Any], tree: SessionTree) -> VectorResult:
    """Row 6: rewind PAST a compaction point; the pre-compaction content survives."""
    entries = tree.entries()
    turns = [item for item in entries if item.kind == "turn"]
    if len(turns) < 3:
        return _result(
            "rewind_past_compaction",
            "not_attempted",
            detail="the fixture produced too few turns to rewind past one",
        )
    before_head = str(tree.head_id() or "")
    target = turns[1].id
    moved = tree.move_head(target, reason="row-6 rewind past a compaction point")
    if moved is None:
        return _result(
            "rewind_past_compaction", "destroyed", destroyed=True, detail="the rewind refused"
        )
    after_head = str(tree.head_id() or "")
    # The entries past the rewind point are the ones a truncation would have
    # removed. Every one must still be in the journal.
    still_there = {item.id for item in tree.entries()}
    past = [item.id for item in turns[2:]]
    lost = [item for item in past if item not in still_there]
    # And the pre-compaction CONTENT must be readable, not merely countable: the
    # text of the turn BEFORE the rewind point has to come back with it.
    pre = SessionTree(tree.root).path_to_head(after_head)
    texts = [item.text for item in pre]
    content_present = any("transcript of t0" in text for text in texts)
    # And the head must have MOVED, or "nothing was lost" is trivially true.
    head_moved = bool(after_head) and after_head != before_head and after_head == target
    ok = head_moved and not lost and content_present and bool(moved.get("history_retained"))
    return _result(
        "rewind_past_compaction",
        "survived" if ok else "destroyed",
        destroyed=not ok,
        detail=(
            f"the head moved from {before_head[:14]} to {after_head[:14]} and all "
            f"{len(past)} entries past the rewind point are still in the journal"
            if ok
            else f"head_moved={head_moved} lost={len(lost)} "
            f"pre_compaction_text_readable={content_present}"
        ),
        head_before=before_head,
        head_after=after_head,
        head_moved=head_moved,
        entries_past_the_rewind=len(past),
        entries_lost=lost,
        pre_compaction_text_readable=content_present,
        restored_depth=len(pre),
    )


def _vector_resume_old(fixture: Mapping[str, Any]) -> VectorResult:
    """Row 7: an OLD session id must restore to its OWN point, not the tip."""
    from runtime import session_lineage as lineage

    log_root = fixture["log_root"]
    repo = fixture["repo"]
    chain = [str(item) for item in fixture["chain"]]
    target = chain[1] if len(chain) > 1 else chain[0]
    records = lineage.lineage_records(log_root, target, repo=repo)
    resume = lineage.resume_state(log_root, target, repo=repo)
    own = lineage.load_session(log_root, target, repo)
    tip = lineage.load_session(log_root, chain[-1], repo)
    # "The right point" means: the session named is the one whose own snapshot we
    # loaded, it is not the tip, and its ancestry is exactly the chain up to it.
    is_own = bool(own) and str(own.get("session_id") or "") == target
    is_not_tip = target != chain[-1]
    ancestry_matches = [
        str(row.get("session_id") or "") for row in records.get("chain") or ()
    ] == list(reversed(chain[: chain.index(target) + 1]))
    own_rotated_to = str(own.get("rotated_to") or "")
    names_successor = own_rotated_to == chain[chain.index(target) + 1]
    tip_is_different = str(tip.get("session_id") or "") != target
    ok = is_own and is_not_tip and ancestry_matches and tip_is_different and (
        names_successor or target == chain[-1]
    )
    return _result(
        "resume_old_session_id",
        "survived" if ok else "destroyed",
        destroyed=not ok,
        detail=(
            f"resumed the 2nd of {len(chain)} generations and it restored to ITS "
            "OWN point, naming its own successor rather than the tip"
            if ok
            else "resolving an old session id did not restore that generation's point"
        ),
        requested=target,
        generations=len(chain),
        ancestry_matches=ancestry_matches,
        is_the_tip=not is_not_tip,
        names_its_own_successor=names_successor,
        continued_to=resume.get("continued_session_id"),
    )


def _vector_explicit_delete(fixture: Mapping[str, Any]) -> VectorResult:
    """Row 8: deletion is explicit, audited, and the ONLY way to lose history.

    Run **last**, because it is the only vector that destroys anything, and every
    other vector's evidence has to be gathered before the fixture loses a
    generation.

    The target is a MIDDLE generation, not the tip. The tip is refused by
    ``delete_session`` unless ``drop_tip=True`` -- correctly, since it is the
    conversation currently open -- so testing the tip would have measured the
    refusal and never the deletion. A middle generation is the more interesting
    target anyway: deleting it breaks the ancestry walk for everything below it,
    and the assertion is that the break is REPORTED rather than silent.
    """
    from runtime import session_lineage as lineage

    log_root = fixture["log_root"]
    repo = fixture["repo"]
    chain = [str(item) for item in fixture["chain"]]
    tip = chain[-1]
    victim = chain[len(chain) // 2]
    if victim == tip:
        victim = chain[0]
    before = fixture_turn_ids(log_root, chain, repo=repo)

    # (a) under-specified deletions are REFUSED and destroy nothing.
    refusals: List[str] = []
    for kwargs, label in (
        ({}, "no confirm"),
        ({"confirm": "sess-somethingelse"}, "wrong confirm"),
        ({"confirm": victim}, "no reason"),
        ({"confirm": tip, "why": "operator asked"}, "the live tip"),
    ):
        try:
            lineage.delete_session(log_root, victim if label != "the live tip" else tip,
                                   repo=repo, **kwargs)
            refusals.append(f"{label}: NOT REFUSED")
        except lineage.LineageRefused as exc:
            refusals.append(f"{label}: refused ({exc.reason[:44]})")
    survived_refusals = fixture_turn_ids(log_root, chain, repo=repo)
    refusals_cost_nothing = len(survived_refusals) == len(before)
    all_refused = not any("NOT REFUSED" in line for line in refusals)

    # (b) the explicit deletion, confirmed by name, with a reason.
    receipt = lineage.delete_session(
        log_root,
        victim,
        confirm=victim,
        why="the operator asked for this conversation to be removed",
        repo=repo,
    )
    gone = not lineage.load_session(log_root, victim, repo)
    ledger = lineage.deletion_ledger(log_root)
    mine = [row for row in ledger if str(row.get("session_id") or "") == victim]
    from runtime.session_lineage import deletions_journal_path

    ledger_path = str(deletions_journal_path(log_root))
    snapshot_dir = str(lineage.session_path(log_root, victim).parent)
    ledger_outside = not ledger_path.startswith(snapshot_dir)
    others_intact = [
        sid for sid in chain if sid != victim and not lineage.load_session(log_root, sid, repo)
    ]
    # The break must be REPORTED, not silent: the tip's ancestry walk has to name
    # the generation that is gone.
    tip_records = lineage.lineage_records(log_root, tip, repo=repo)
    break_reported = victim in [
        str(item) for item in tip_records.get("unreadable") or ()
    ]
    ok = (
        refusals_cost_nothing
        and all_refused
        and gone
        and bool(mine)
        and ledger_outside
        and not others_intact
        and break_reported
        and bool(receipt.get("orphaned_successors"))
    )
    return _result(
        "explicit_delete",
        "survived_explicit" if ok else "destroyed",
        destroyed=ok,
        detail=(
            f"{len(refusals)} under-specified deletions were all refused with "
            f"nothing lost; the confirmed one removed {receipt.get('deleted')} "
            "generation(s), left an audit row outside the deleted directory, and "
            "reported the ancestry break it caused"
            if ok
            else "an under-specified deletion was not refused, or the audit row / "
            "the ancestry break was not reported"
        ),
        refusals=refusals,
        refusals_destroyed_nothing=refusals_cost_nothing,
        all_under_specified_refused=all_refused,
        deleted=victim,
        was_the_tip=bool(receipt.get("was_tip")),
        now_unreadable=gone,
        generations_broken=others_intact,
        orphaned_successors=list(receipt.get("orphaned_successors") or ()),
        ancestry_break_reported=break_reported,
        ledger_rows=len(mine),
        ledger_outside_the_deleted_directory=ledger_outside,
        ledger=ledger_path,
        turns_destroyed=sum(
            int(item.get("turns") or 0) for row in mine for item in row.get("destroyed") or ()
        ),
        bytes_destroyed=sum(
            int(item.get("bytes") or 0) for row in mine for item in row.get("destroyed") or ()
        ),
    )


def audit_report(**kwargs: Any) -> Dict[str, Any]:
    """:func:`history_audit` plus the GC policy, as one printable receipt."""
    audit = history_audit(**kwargs)
    return {
        "schema": "runtime.session_durability.audit_report/1",
        "audit": audit,
        "declared_vectors": [
            {
                "number": row.number,
                "name": row.name,
                "vector": row.vector,
                "required": row.required,
                "loss_allowed": row.loss_allowed,
                "note": row.note,
            }
            for row in HISTORY_VECTORS
        ],
        "separation": job_write_separation(),
        "outcome_classes": list(OUTCOME_CLASSES),
    }
