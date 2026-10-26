"""Memory ranking with citation tracking and usage decay -- and the receipt.

WHAT THIS ADDS
--------------
The corpus's agent-maintained-memory system injects recalled memories with
**citation tracking and usage-based ranking**: a memory that gets cited often
ranks higher, and one that never gets cited decays. This repository's store had
neither. It had a matched-word-count rank and a recency tiebreak, which is a
*lexical* rank -- it knows nothing about whether a memory ever helped.

This module supplies the missing half, and -- more importantly -- supplies the
**receipt that says which half actually ran**.

THE HONESTY PROBLEM, STATED PLAIMLY
------------------------------------
A usage-based rank over a store with zero citations is arithmetically identical
to a usage-based rank over a store with no usage data at all: every usage term
is 0.0. Rendered carelessly, that reads as "this memory ranked 3rd because it
has been cited 12 times" when the true statement is "the usage term contributed
nothing because nothing has ever been cited". That is precisely the shape of
dishonesty this repository exists to prevent, so:

* :class:`RankingReceipt.usage_applied` is ``False`` -- with a stated reason --
  whenever no citation exists in the scored set;
* every :class:`RankedMemory` carries a ``reasons`` list naming each component's
  actual contribution, including the zero ones;
* :meth:`RankingPolicy.receipt_fields` publishes the weights so a reader can
  recompute a score by hand.

A zero that reads as "the lane was free" is a number nobody took. This module
reports ``vacuous`` whenever a metric had no data behind it.
"""

from __future__ import annotations

import calendar
import math
import os
import sqlite3
import threading
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CitationRecord",
    "RankedMemory",
    "RankingPolicy",
    "RankingReceipt",
    "UsageIndex",
    "citation_rate",
    "decision_recurrence_rate",
    "default_ranking_db_path",
    "open_default_usage_index",
    "rank_memories",
    "recall_at_k",
    "stale_memory_rate",
]

#: The vocabulary a caller uses to say a memory was untrusted-sourced. This is
#: the same ``UNTRUSTED_SOURCES`` vocabulary ``shared.security`` uses at every
#: ingress point; the ranking penalty is deliberately a RANKING effect, not a
#: refusal, because the memory was already admitted -- it is only worth less.
UNTRUSTED_SOURCE_NAMES: Tuple[str, ...] = (
    "issue",
    "repository_instructions",
    "web",
    "skill",
    "plugin",
    "mcp",
    "memory",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_usage (
    decision_id INTEGER PRIMARY KEY,
    citations INTEGER NOT NULL DEFAULT 0,
    first_cited_at TEXT,
    last_cited_at TEXT,
    last_used_at TEXT,
    last_ranked_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_citations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    cited_at TEXT NOT NULL,
    session_id TEXT,
    run_id TEXT,
    turn INTEGER,
    context TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_citations_decision
    ON memory_citations(decision_id);
"""

#: Bound on the citation-event journal. Usage counts are the ranking input; the
#: journal exists so "who cited this, when, in which turn" is answerable. It is
#: bounded, and pruning the journal does NOT change any usage count, so a prune
#: can never silently alter a ranking.
_MAX_CITATION_ROWS = 50_000


def _utc_iso(epoch: Optional[float] = None) -> str:
    """Return one UTC ISO-8601 second-resolution stamp."""
    moment = time.time() if epoch is None else float(epoch)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)) + "Z"


def _parse_iso(value: Any) -> Optional[float]:
    """Parse a ``_utc_iso`` stamp back to epoch seconds, or ``None``.

    Returns ``None`` rather than 0.0 for an unparseable stamp: a row with an
    unreadable timestamp has an UNKNOWN age, and treating it as 1970 makes it
    look maximally stale, which is a fabricated measurement.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return float(calendar.timegm(parsed))


@dataclass(frozen=True)
class CitationRecord:
    """One citation of one memory, with enough context to audit it."""

    decision_id: int
    cited_at: str
    session_id: str = ""
    run_id: str = ""
    turn: Optional[int] = None
    context: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "decision_id": self.decision_id,
            "cited_at": self.cited_at,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn": self.turn,
            "context": self.context,
        }


@dataclass
class _UsageState:
    """Bookkeeping for one open usage index."""

    conn: Optional[sqlite3.Connection] = None
    open_error: str = ""
    citations_recorded: int = 0
    rows_pruned: int = 0
    refusals: int = 0
    last_error: str = ""


class UsageIndex:
    """Citation and usage telemetry for memory rows.

    This is a SIDECAR, deliberately not a schema change on
    :class:`memory.decision_store.DecisionStore`. Three reasons:

    1. Usage telemetry is DERIVED. Deleting this file loses a ranking signal,
       never a memory.
    2. ``DecisionStore`` is a published contract with a scrub/health pass over
       every column; a counter that changes on every citation would put a
       write on the read-adjacent path of a store the MCP server shares.
    3. Keeping it separate means the store's own ``health()`` answer is
       unchanged by whether anyone has been citing memories.

    Thread-safe through one ``RLock``. Keys are the decision store's integer
    row ids.
    """

    def __init__(self, db_path: Any, *, strict: bool = False) -> None:
        """Open the usage index; ``strict`` re-raises an open failure."""
        self.db_path = str(Path(str(db_path)).resolve())
        self._lock = threading.RLock()
        self._state = _UsageState()
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._state.conn = conn
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
            self._state.open_error = f"{type(exc).__name__}: {exc}"[:300]
            if strict:
                raise

    @property
    def available(self) -> bool:
        """True when usage telemetry can be read and written."""
        return self._state.conn is not None

    def capability(self) -> Dict[str, Any]:
        """Report whether usage tracking is usable, and why not when it is not.

        An unusable usage index is NOT a silent zero: :func:`rank_memories`
        checks this and sets ``usage_applied=False`` with the reason, so a
        ranking produced without telemetry says so instead of pretending every
        memory is equally uncited.

        ``refusals``/``last_error`` exist because a write that fails must not
        vanish. ``cite()`` returning ``None`` is a refusal, and the refusal
        count plus the reason are readable here rather than being swallowed.
        """
        return {
            "available": self.available,
            "db_path": self.db_path,
            "refusals": self._state.refusals,
            "last_error": self._state.last_error,
            "reason": (
                ""
                if self.available
                else f"the usage index is not open: {self._state.open_error}"
            ),
        }

    # -- write ----------------------------------------------------------

    def _refuse(self, reason: str) -> None:
        """Record one refused write so it is countable and diagnosable."""
        self._state.refusals += 1
        self._state.last_error = str(reason)[:300]

    def cite(
        self,
        decision_id: Any,
        *,
        session_id: Any = "",
        run_id: Any = "",
        turn: Optional[int] = None,
        context: Any = "",
        at: Optional[float] = None,
    ) -> Optional[CitationRecord]:
        """Record one citation. Returns the record, or ``None`` when refused.

        A citation is a claim that a memory was *used* -- it reached a prompt
        and the run continued. That is why it is separate from "was retrieved":
        a store that counted retrievals would rank whatever matched most
        often, which is a popularity contest rather than a usefulness signal.

        ``None`` means REFUSED, not "recorded". The refusal is counted and its
        reason kept on :meth:`capability`, so a caller that ignores the return
        value still leaves evidence that something was dropped.
        """
        try:
            row_id = int(decision_id)
        except (TypeError, ValueError):
            self._refuse(f"not an integer memory id: {decision_id!r}")
            return None
        if row_id <= 0:
            self._refuse(f"memory id must be positive, got {row_id}")
            return None
        stamp = _utc_iso(at)
        session = str(session_id or "")[:128]
        run = str(run_id or "")[:128]
        safe_turn: Optional[int]
        try:
            safe_turn = int(turn) if turn is not None else None
        except (TypeError, ValueError):
            safe_turn = None
        note = str(context or "")[:512]
        with self._lock:
            conn = self._state.conn
            if conn is None:
                self._refuse("the usage index is not open")
                return None
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO memory_usage (decision_id, citations, "
                    "first_cited_at, last_cited_at) "
                    "VALUES (?, 1, ?, ?) "
                    "ON CONFLICT(decision_id) DO UPDATE SET "
                    "citations = citations + 1, last_cited_at = excluded.last_cited_at",
                    (row_id, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO memory_citations "
                    "(decision_id, cited_at, session_id, run_id, turn, context) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row_id, stamp, session, run, safe_turn, note),
                )
                conn.commit()
            except sqlite3.DatabaseError as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                self._refuse(f"{type(exc).__name__}: {exc}")
                return None
        self._state.citations_recorded += 1
        self._prune(conn)
        return CitationRecord(
            decision_id=row_id,
            cited_at=stamp,
            session_id=session,
            run_id=run,
            turn=safe_turn,
            context=note,
        )

    def cite_many(
        self,
        decision_ids: Iterable[Any],
        *,
        session_id: Any = "",
        run_id: Any = "",
        turn: Optional[int] = None,
    ) -> List[CitationRecord]:
        """Cite every id in ``decision_ids``; returns the accepted records."""
        records: List[CitationRecord] = []
        for raw in decision_ids or ():
            record = self.cite(
                raw, session_id=session_id, run_id=run_id, turn=turn
            )
            if record is not None:
                records.append(record)
        return records

    def mark_ranked(self, decision_ids: Iterable[Any]) -> int:
        """Record that rows were SURFACED (offered), distinct from cited.

        "Ranked but not cited" is the measurement that separates a memory the
        model ignored from a memory it never saw, so the two events are stored
        separately rather than inferred from each other.
        """
        stamp = _utc_iso()
        marked = 0
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return 0
            try:
                for raw in decision_ids or ():
                    try:
                        row_id = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if row_id <= 0:
                        continue
                    conn.execute(
                        "INSERT INTO memory_usage (decision_id, last_ranked_at) "
                        "VALUES (?, ?) ON CONFLICT(decision_id) DO UPDATE SET "
                        "last_ranked_at = excluded.last_ranked_at",
                        (row_id, stamp),
                    )
                    marked += 1
                conn.commit()
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return 0
        return marked

    def forget(self, decision_id: Any) -> bool:
        """Drop all usage telemetry for one id. Used when a memory is revoked."""
        try:
            row_id = int(decision_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return False
            try:
                conn.execute(
                    "DELETE FROM memory_usage WHERE decision_id = ?", (row_id,)
                )
                conn.execute(
                    "DELETE FROM memory_citations WHERE decision_id = ?", (row_id,)
                )
                conn.commit()
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return False
        return True

    def _prune(self, conn: sqlite3.Connection) -> int:
        """Trim the citation journal to :data:`_MAX_CITATION_ROWS` newest rows.

        Usage COUNTS live in ``memory_usage`` and are never touched here, so a
        prune can bound disk without changing a single ranking. Returns the
        number of journal rows removed.
        """
        try:
            total = int(
                conn.execute("SELECT COUNT(*) AS n FROM memory_citations").fetchone()[
                    "n"
                ]
            )
            if total <= _MAX_CITATION_ROWS:
                return 0
            excess = total - _MAX_CITATION_ROWS
            conn.execute(
                "DELETE FROM memory_citations WHERE seq IN ("
                "SELECT seq FROM memory_citations ORDER BY seq ASC LIMIT ?)",
                (excess,),
            )
            conn.commit()
        except sqlite3.DatabaseError:
            return 0
        self._state.rows_pruned += excess
        return excess

    def prune(self) -> int:
        """Public form of the journal prune. Never raises."""
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return 0
            return self._prune(conn)

    # -- read -----------------------------------------------------------

    def usage(self, decision_ids: Iterable[Any]) -> Dict[int, Dict[str, Any]]:
        """Return usage rows for the given ids.

        A row with no entry is NOT reported as zero citations. It is reported
        as ``known: False``, because "we have no record of this memory" and
        "this memory has never been cited" are different claims and only the
        second one is a measurement.
        """
        wanted: List[int] = []
        for raw in decision_ids or ():
            try:
                row_id = int(raw)
            except (TypeError, ValueError):
                continue
            if row_id not in wanted:
                wanted.append(row_id)
        result: Dict[int, Dict[str, Any]] = {}
        with self._lock:
            conn = self._state.conn
            if conn is None:
                for row_id in wanted:
                    result[row_id] = {
                        "known": False,
                        "citations": 0,
                        "first_cited_at": None,
                        "last_cited_at": None,
                        "last_used_at": None,
                        "last_ranked_at": None,
                        "reason": self.capability()["reason"],
                    }
                return result
            for row_id in wanted:
                row = conn.execute(
                    "SELECT citations, first_cited_at, last_cited_at, "
                    "last_used_at, last_ranked_at FROM memory_usage "
                    "WHERE decision_id = ?",
                    (row_id,),
                ).fetchone()
                if row is None:
                    result[row_id] = {
                        "known": False,
                        "citations": 0,
                        "first_cited_at": None,
                        "last_cited_at": None,
                        "last_used_at": None,
                        "last_ranked_at": None,
                        "reason": "no usage row has ever been recorded for this id",
                    }
                    continue
                result[row_id] = {
                    "known": True,
                    "citations": int(row["citations"] or 0),
                    "first_cited_at": row["first_cited_at"],
                    "last_cited_at": row["last_cited_at"],
                    "last_used_at": row["last_used_at"],
                    "last_ranked_at": row["last_ranked_at"],
                    "reason": "",
                }
        return result

    def citations(self, decision_id: Any, *, limit: int = 50) -> List[CitationRecord]:
        """Return the citation journal for one id, newest first."""
        try:
            row_id = int(decision_id)
        except (TypeError, ValueError):
            return []
        try:
            bounded = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            bounded = 50
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    "SELECT decision_id, cited_at, session_id, run_id, turn, "
                    "context FROM memory_citations WHERE decision_id = ? "
                    "ORDER BY seq DESC LIMIT ?",
                    (row_id, bounded),
                ).fetchall()
            except sqlite3.DatabaseError:
                return []
        return [
            CitationRecord(
                decision_id=int(row["decision_id"]),
                cited_at=str(row["cited_at"] or ""),
                session_id=str(row["session_id"] or ""),
                run_id=str(row["run_id"] or ""),
                turn=(int(row["turn"]) if row["turn"] is not None else None),
                context=str(row["context"] or ""),
            )
            for row in rows
        ]

    def totals(self) -> Dict[str, Any]:
        """Return corpus-level usage totals for a health/listing surface."""
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return {
                    "available": False,
                    "tracked_memories": None,
                    "total_citations": None,
                    "never_cited": None,
                    "reason": self.capability()["reason"],
                }
            try:
                tracked = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM memory_usage"
                    ).fetchone()["n"]
                )
                cited = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM memory_usage WHERE citations > 0"
                    ).fetchone()["n"]
                )
                total = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(citations), 0) AS n FROM memory_usage"
                    ).fetchone()["n"]
                )
            except sqlite3.DatabaseError as exc:
                return {
                    "available": False,
                    "tracked_memories": None,
                    "total_citations": None,
                    "never_cited": None,
                    "reason": f"usage totals are unreadable: {type(exc).__name__}",
                }
        return {
            "available": True,
            "tracked_memories": tracked,
            "total_citations": total,
            "never_cited": tracked - cited,
            "reason": "",
        }

    def close(self) -> None:
        """Close the index; repeated calls are harmless."""
        with self._lock:
            if self._state.conn is not None:
                try:
                    self._state.conn.close()
                finally:
                    self._state.conn = None

    def __enter__(self) -> "UsageIndex":
        """Return this index as a context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Close the index when leaving a context-manager block."""
        self.close()


@dataclass(frozen=True)
class RankingPolicy:
    """The weights that turn usage + age + trust into a score.

    The defaults are named and justified in
    :meth:`caps_table`'s sibling docstring in :mod:`memory.snapshot`; the short
    version is that ``citation_weight`` dominates because a citation is a
    measured use, ``decay_half_life_days`` is the corpus's "never cited decays"
    rule made numeric, and ``recency_weight`` is deliberately the smallest term
    because recency is the signal this store already had and is the one that
    makes a memory look important merely by being new.
    """

    citation_weight: float = 1.0
    recency_weight: float = 0.25
    decay_half_life_days: float = 30.0
    untrusted_penalty: float = 0.5
    unknown_age_treated_as_days: Optional[float] = None
    floor_score: float = 0.0

    def clamped(self) -> "RankingPolicy":
        """Return a copy with every bound made safe.

        A negative weight would invert the ranking; a non-positive half-life
        would make ``0.5 ** x`` overflow. Both are clamped rather than trusted,
        because a caller-supplied policy is data.
        """
        half_life = max(0.25, float(self.decay_half_life_days))
        return RankingPolicy(
            citation_weight=max(0.0, float(self.citation_weight)),
            recency_weight=max(0.0, float(self.recency_weight)),
            decay_half_life_days=half_life,
            untrusted_penalty=min(1.0, max(0.0, float(self.untrusted_penalty))),
            unknown_age_treated_as_days=(
                None
                if self.unknown_age_treated_as_days is None
                else max(0.0, float(self.unknown_age_treated_as_days))
            ),
            floor_score=float(self.floor_score),
        )

    def receipt_fields(self) -> Dict[str, Any]:
        """Publish the weights so a reader can recompute a score by hand."""
        safe = self.clamped()
        return {
            "citation_weight": safe.citation_weight,
            "recency_weight": safe.recency_weight,
            "decay_half_life_days": safe.decay_half_life_days,
            "untrusted_penalty": safe.untrusted_penalty,
            "unknown_age_treated_as_days": safe.unknown_age_treated_as_days,
            "floor_score": safe.floor_score,
        }

    @property
    def components(self) -> Tuple[str, ...]:
        """The component names a score is made of, in evaluation order."""
        return ("lexical", "usage", "decay", "recency", "trust")


@dataclass(frozen=True)
class RankedMemory:
    """One ranked memory, with the arithmetic that put it there.

    ``reasons`` is the point of this class. A rank a user cannot interrogate is
    a rank they have to trust, and "why did that surface" is the question a
    human asks the moment a memory surprises them.

    ``source`` and ``provenance`` are carried, not dropped. The ranking is the
    projection layer every consumer of memory reads through, and a projection
    that discarded the source would make trust tiering impossible downstream:
    the untrusted-source marking is a property of WHERE a memory came from, and
    a consumer holding only a :class:`RankedMemory` would have no way to see it.
    """

    memory_id: Any
    text: str
    score: float
    rank: int
    reasons: Tuple[str, ...]
    components: Mapping[str, float]
    citations: Optional[int]
    usage_known: bool
    untrusted_source: bool
    category: str = ""
    age_days: Optional[float] = None
    source: str = ""
    provenance: Mapping[str, Any] = dataclass_field(default_factory=dict)
    task_id: Optional[str] = None
    repo_path: Optional[str] = None
    created_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "memory_id": self.memory_id,
            "text": self.text,
            "score": self.score,
            "rank": self.rank,
            "reasons": list(self.reasons),
            "components": dict(self.components),
            "citations": self.citations,
            "usage_known": self.usage_known,
            "untrusted_source": self.untrusted_source,
            "category": self.category,
            "age_days": self.age_days,
            "source": self.source,
            "provenance": dict(self.provenance),
            "task_id": self.task_id,
            "repo_path": self.repo_path,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RankingReceipt:
    """Why this ranking looks the way it does -- including when it did not run.

    ``usage_applied`` is the load-bearing field. ``False`` with a stated reason
    means every ``usage`` and ``decay`` component is 0.0 for every row, so the
    ordering in play is lexical + recency only. Without that field a reader
    would have to notice that every usage number is zero and infer the rest.
    """

    policy: Mapping[str, Any]
    ranked: Tuple[RankedMemory, ...]
    usage_applied: bool
    usage_reason: str
    considered: int
    dropped_below_floor: int
    floor: float
    untrusted_penalised: int
    unknown_usage: int
    at: str
    report_ranking: bool = True

    @property
    def ranked_count(self) -> int:
        """How many rows were returned."""
        return len(self.ranked)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt.

        ``ranked`` is the full list only when ``report_ranking`` is true; a
        caller that asked not to be told the ranking still gets every field
        that describes HOW it was produced, which is the useful half.
        """
        payload: Dict[str, Any] = {
            "policy": dict(self.policy),
            "components": list(RankingPolicy().components),
            "usage_applied": self.usage_applied,
            "usage_reason": self.usage_reason,
            "considered": self.considered,
            "ranked_count": self.ranked_count,
            "dropped_below_floor": self.dropped_below_floor,
            "floor": self.floor,
            "untrusted_penalised": self.untrusted_penalised,
            "unknown_usage": self.unknown_usage,
            "at": self.at,
            "report_ranking": self.report_ranking,
        }
        if self.report_ranking:
            payload["ranked"] = [row.as_dict() for row in self.ranked]
        return payload

    def render(self) -> str:
        """Render the ranking as the human-facing explanation line.

        This is where "Report the ranking so a user can see why a memory
        surfaced" becomes a real surface rather than a JSON key nobody reads.
        """
        lines = [
            f"ranked {self.ranked_count} of {self.considered} considered "
            f"at {self.at}"
        ]
        if not self.usage_applied:
            lines.append(f"usage ranking NOT applied: {self.usage_reason}")
        for row in self.ranked:
            cited = "unknown" if row.citations is None else str(row.citations)
            lines.append(
                f"{row.rank}. [score {row.score:.4f}] citations={cited} "
                f"untrusted={row.untrusted_source} :: {row.text}"
            )
            lines.append(f"     why: {'; '.join(row.reasons)}")
        if self.dropped_below_floor:
            lines.append(
                f"{self.dropped_below_floor} row(s) scored below the floor "
                f"({self.floor}) and were dropped; the floor is a named bound, "
                "not a silent cut"
            )
        return "\n".join(lines)


def _is_untrusted(row: Any, source_names: Sequence[str]) -> bool:
    """Return whether a row's provenance names an untrusted SOURCE.

    Reads the same fields ``shared.security`` writes: a provenance
    ``untrusted_source`` marker, or a ``source`` that is one of the seven
    ``UNTRUSTED_SOURCES``. A memory the user *stated* is trusted regardless of
    which channel carried it, so an explicit ``trusted_source`` marker wins.
    """
    untrusted = set(source_names or UNTRUSTED_SOURCE_NAMES)
    provenance: Mapping[str, Any] = {}
    if isinstance(row, MappingABC):
        raw = row.get("provenance")
        if isinstance(raw, MappingABC):
            provenance = raw
        if row.get("untrusted_source") is True:
            return True
        if row.get("trusted_source") is True:
            return False
    elif hasattr(row, "provenance"):
        raw = getattr(row, "provenance", None)
        if isinstance(raw, MappingABC):
            provenance = raw
    if provenance.get("trusted_source") is True:
        return False
    if provenance.get("untrusted_source") is True:
        return True
    for field_name in ("source", "provenance_source"):
        value: Any = None
        if isinstance(row, MappingABC):
            value = row.get(field_name)
        else:
            value = getattr(row, field_name, None)
        if value and str(value).strip().casefold() in untrusted:
            return True
    return False


def _lexical_score(row: Any) -> float:
    """Return the row's lexical strength, higher-is-better.

    A caller that already ranked by BM25 passes the score in and this module
    composes with it rather than replacing it, so usage ranking is an ADDITION
    to lexical retrieval rather than a competing answer to it.

    The one sign convention handled here is FTS5's: ``bm25()`` returns a
    NEGATIVE number where a MORE negative value is a BETTER match, so a raw
    ``bm25`` field is negated on the way in. Getting this wrong silently
    inverts the lexical term, which is why it is handled in one place and
    named in the receipt rather than left to each caller.
    """
    for field_name, negate in (
        ("bm25", True),
        ("lexical", False),
        ("lexical_score", False),
        ("score", False),
    ):
        value: Any = None
        if isinstance(row, MappingABC):
            value = row.get(field_name)
        else:
            value = getattr(row, field_name, None)
        if value is None:
            continue
        try:
            raw = float(value)
        except (TypeError, ValueError):
            continue
        return -raw if negate else raw
    return 0.0


def _normalise(values: Sequence[float]) -> Tuple[List[float], str]:
    """Min-max normalise ``values`` into ``[0, 1]``, higher-raw-is-better.

    Normalisation is necessary, not cosmetic. The three signals being combined
    live on incompatible scales: FTS5's BM25 is negative and unbounded, citation
    counts are a small positive integer, and recency is a fraction of a day.
    Adding them raw lets whichever term happens to have the largest magnitude
    silently decide every ranking -- measured on this module's own first
    implementation, a BM25 of -2.0 lost to a BM25 of -0.5 purely because the
    newer row's recency term was larger.

    Two properties the caller can rely on:

    * the mapping is monotonic, so normalisation never reorders a single
      signal relative to itself;
    * when every raw value is equal the signal carries NO information, and
      every normalised value is ``1.0`` with ``"constant"`` returned as the
      note -- because dividing by a zero range is how a flat signal becomes a
      tiebreak that decides the ranking on noise.

    The known property, stated rather than hidden: normalisation is relative to
    the candidate SET. Adding one weak row to a set slightly lowers everyone's
    normalised lexical score. That is why the receipt publishes both the raw
    and the normalised value for each component.
    """
    if not values:
        return [], "empty"
    low = min(values)
    high = max(values)
    if high - low <= 0.0:
        return [1.0] * len(values), "constant"
    span = high - low
    return [(value - low) / span for value in values], "min_max"


def _memory_id(row: Any) -> Any:
    """Return a row's stable identifier under any of its historical names."""
    for field_name in ("id", "memory_id", "decision_id", "doc_id"):
        value: Any = None
        if isinstance(row, MappingABC):
            value = row.get(field_name)
        else:
            value = getattr(row, field_name, None)
        if value not in (None, ""):
            return value
    return None


def _row_text(row: Any) -> str:
    for field_name in ("text", "body", "title"):
        value: Any = None
        if isinstance(row, MappingABC):
            value = row.get(field_name)
        else:
            value = getattr(row, field_name, None)
        if value:
            return str(value)
    return ""


def _row_category(row: Any) -> str:
    """Return a row's category under either a mapping or an object shape."""
    if isinstance(row, MappingABC):
        return str(row.get("category") or "")
    return str(getattr(row, "category", "") or "")


def _row_optional(row: Any, key: str) -> Optional[str]:
    """Return a row's optional string field, or ``None`` when it is absent.

    ``None`` and ``""`` are different answers here: an absent ``task_id`` means
    "this memory was not task-scoped" and an empty one means nothing, so the
    distinction is preserved rather than collapsed.
    """
    if isinstance(row, MappingABC):
        value = row.get(key)
    else:
        value = getattr(row, key, None)
    if value in (None, ""):
        return None
    return str(value)


def _row_source(row: Any) -> str:
    """Return a row's source channel, under either row shape."""
    if isinstance(row, MappingABC):
        return str(row.get("source") or "")
    return str(getattr(row, "source", "") or "")


def _row_provenance(row: Any) -> Mapping[str, Any]:
    """Return a row's provenance mapping (empty when it has none)."""
    if isinstance(row, MappingABC):
        raw = row.get("provenance")
    else:
        raw = getattr(row, "provenance", None)
    return raw if isinstance(raw, MappingABC) else {}


def _row_created_at(row: Any) -> str:
    for field_name in ("created_at", "updated_at", "cited_at"):
        value: Any = None
        if isinstance(row, MappingABC):
            value = row.get(field_name)
        else:
            value = getattr(row, field_name, None)
        return str(value or "")
    return ""


def rank_memories(
    rows: Sequence[Any],
    *,
    usage: Optional[UsageIndex] = None,
    policy: Optional[RankingPolicy] = None,
    limit: Optional[int] = None,
    at: Optional[float] = None,
    report_ranking: bool = True,
    untrusted_source_names: Sequence[str] = UNTRUSTED_SOURCE_NAMES,
) -> RankingReceipt:
    """Rank memory rows by usage and recency, and explain the ranking.

    ``rows`` may be dicts or :class:`memory.decision_store.Decision` objects;
    both are read through the same field probes so a caller does not have to
    convert. Each row needs a stable id under ``id``/``memory_id``/
    ``decision_id``/``doc_id``.

    ``usage`` is the telemetry sidecar. When it is ``None`` or unusable the
    ranking still works and the receipt says ``usage_applied=False`` -- the
    ordering is then lexical + recency, and it says so.

    Decay is applied to the USAGE term only, not to the whole score. A memory
    nobody cites should stop *earning* its citation bonus; it should not have
    its lexical relevance rotted away by age, because an old fact about this
    repository is still a true fact about it.
    """
    safe_policy = (policy or RankingPolicy()).clamped()
    moment = float(at) if at is not None else time.time()
    stamp = _utc_iso(moment)
    candidates = [row for row in (rows or ()) if row is not None]
    ids = [_memory_id(row) for row in candidates]

    capability = usage.capability() if usage is not None else {
        "available": False,
        "reason": "no usage index was supplied to rank_memories",
    }
    telemetry: Dict[Any, Dict[str, Any]] = {}
    if capability.get("available"):
        keyed = {}
        for raw in ids:
            try:
                keyed[int(raw)] = raw
            except (TypeError, ValueError):
                continue
        if keyed:
            fetched = usage.usage(sorted(keyed))
            for numeric, raw in keyed.items():
                telemetry[raw] = fetched.get(numeric, {})

    any_citations = any(
        bool(entry.get("known")) and int(entry.get("citations") or 0) > 0
        for entry in telemetry.values()
    )
    if not capability.get("available"):
        usage_applied = False
        usage_reason = str(capability.get("reason") or "no usage telemetry")
    elif not candidates:
        usage_applied = False
        usage_reason = "there were no rows to rank, so no usage term was computed"
    elif not any_citations:
        usage_applied = False
        usage_reason = (
            "no memory in this set has ever been cited, so every usage and "
            "decay term is 0.0 and the ordering in play is lexical + recency "
            "only; this is not a usage-based ranking"
        )
    else:
        usage_applied = True
        usage_reason = ""

    scored_raw: List[Dict[str, Any]] = []
    unknown_usage = 0
    penalised = 0
    for row in candidates:
        row_id = _memory_id(row)
        entry = telemetry.get(row_id, {})
        usage_known = bool(entry.get("known"))
        citations: Optional[int] = None
        if usage_known:
            citations = int(entry.get("citations") or 0)
        else:
            unknown_usage += 1

        created = _parse_iso(_row_created_at(row))
        last_cited = _parse_iso(entry.get("last_cited_at")) if usage_known else None
        age_days: Optional[float] = None
        age_source = "none"
        if last_cited is not None:
            age_days = max(0.0, (moment - last_cited) / 86400.0)
            age_source = "last_cited"
        elif created is not None:
            age_days = max(0.0, (moment - created) / 86400.0)
            age_source = "created"
        elif safe_policy.unknown_age_treated_as_days is not None:
            age_days = float(safe_policy.unknown_age_treated_as_days)
            age_source = "policy_default"

        scored_raw.append(
            {
                "row": row,
                "id": row_id,
                "lexical_raw": _lexical_score(row),
                "usage_raw": (
                    safe_policy.citation_weight
                    * math.log1p(float(citations or 0))
                    if (usage_applied and usage_known and citations)
                    else 0.0
                ),
                "recency_raw": (
                    1.0 / (1.0 + age_days) if age_days is not None else 0.0
                ),
                "age_days": age_days,
                "age_source": age_source,
                "citations": citations,
                "usage_known": usage_known,
                "untrusted": _is_untrusted(row, untrusted_source_names),
                "text": _row_text(row),
                "category": _row_category(row),
                "source": _row_source(row),
                "provenance": _row_provenance(row),
                "task_id": _row_optional(row, "task_id"),
                "repo_path": _row_optional(row, "repo_path"),
                "created_at": _row_created_at(row),
            }
        )

    if not scored_raw:
        return RankingReceipt(
            policy=safe_policy.receipt_fields(),
            ranked=(),
            usage_applied=usage_applied,
            usage_reason=usage_reason,
            considered=0,
            dropped_below_floor=0,
            floor=safe_policy.floor_score,
            untrusted_penalised=0,
            unknown_usage=0,
            at=stamp,
            report_ranking=report_ranking,
        )

    lexical_n, lexical_note = _normalise([e["lexical_raw"] for e in scored_raw])
    usage_n, usage_note = _normalise([e["usage_raw"] for e in scored_raw])
    recency_n, recency_note = _normalise([e["recency_raw"] for e in scored_raw])

    scored: List[RankedMemory] = []
    for position, entry in enumerate(scored_raw):
        age_days = entry["age_days"]
        citations = entry["citations"]
        usage_known = entry["usage_known"]
        untrusted = entry["untrusted"]
        if untrusted:
            penalised += 1

        reasons: List[str] = []
        components: Dict[str, float] = {
            "lexical": round(lexical_n[position], 8),
            "usage": round(usage_n[position], 8),
            "recency": round(recency_n[position], 8),
        }

        reasons.append(
            f"lexical {entry['lexical_raw']:+.4f} -> {lexical_n[position]:.4f} "
            f"({lexical_note})"
        )
        if usage_applied and usage_known and citations:
            reasons.append(
                f"usage {citations} citation(s) -> raw {entry['usage_raw']:+.4f}, "
                f"normalised {usage_n[position]:.4f} ({usage_note})"
            )
        else:
            reasons.append(
                f"usage +0.0000 (raw {entry['usage_raw']:+.4f}, normalised "
                f"{usage_n[position]:.4f}, {usage_note}) -- "
                + (
                    "no citation has been recorded for this memory"
                    if usage_known
                    else "this memory has NO usage row at all, which is not the "
                    "same as zero citations"
                )
            )
        if age_days is not None:
            decay = (
                0.5 ** (age_days / safe_policy.decay_half_life_days)
                if usage_applied
                else 1.0
            )
            components["decay"] = round(decay, 8)
            reasons.append(
                f"decay x{decay:.4f} from {age_source} age {age_days:.1f}d "
                f"(half-life {safe_policy.decay_half_life_days:g}d)"
                + (
                    ""
                    if usage_applied
                    else " -- not applied, because usage ranking is not in play"
                )
            )
        else:
            components["decay"] = 1.0
            reasons.append(
                "decay x1.0000 -- the row carries no readable timestamp and the "
                "policy declares no default age, so its age is UNKNOWN rather "
                "than zero"
            )
        if age_days is not None:
            reasons.append(
                f"recency {entry['recency_raw']:.4f} -> {recency_n[position]:.4f} "
                f"({recency_note})"
            )
        else:
            reasons.append(
                f"recency +0.0000 (raw {entry['recency_raw']:.4f}, normalised "
                f"{recency_n[position]:.4f}) -- no readable age, and an unknown "
                "age is not an age of zero"
            )

        trust = 1.0
        if untrusted:
            trust = safe_policy.untrusted_penalty
            reasons.append(
                f"untrusted source -> whole score x{trust:.4f} (it was admitted, "
                "but a fact extracted from an untrusted source does not outrank "
                "one the user stated)"
            )
        components["trust"] = trust

        usage_component = usage_n[position] * components["decay"]
        components["usage"] = round(usage_component, 8)
        base = (
            lexical_n[position]
            + safe_policy.citation_weight * usage_component
            + safe_policy.recency_weight * recency_n[position]
        )
        total = base * trust

        scored.append(
            RankedMemory(
                memory_id=entry["id"],
                text=entry["text"],
                score=round(total, 8),
                rank=0,
                reasons=tuple(reasons),
                components=components,
                citations=citations,
                usage_known=usage_known,
                untrusted_source=untrusted,
                category=entry["category"],
                age_days=None if age_days is None else round(age_days, 4),
                source=entry["source"],
                provenance=entry["provenance"],
                task_id=entry["task_id"],
                repo_path=entry["repo_path"],
                created_at=entry["created_at"],
            )
        )

    below = 0
    if safe_policy.floor_score > 0.0:
        kept = [row for row in scored if row.score >= safe_policy.floor_score]
        below = len(scored) - len(kept)
        scored = kept

    # Deterministic ordering: score DESC, then the stable id ASC as a
    # tiebreak, so two rows with equal scores always come back in the same
    # order across runs and platforms.
    scored.sort(key=lambda row: (-row.score, str(row.memory_id)))
    if limit is not None:
        try:
            scored = scored[: max(0, int(limit))]
        except (TypeError, ValueError):
            pass
    ranked = tuple(
        RankedMemory(
            memory_id=row.memory_id,
            text=row.text,
            score=row.score,
            rank=position,
            reasons=row.reasons,
            components=row.components,
            citations=row.citations,
            usage_known=row.usage_known,
            untrusted_source=row.untrusted_source,
            category=row.category,
            age_days=row.age_days,
            source=row.source,
            provenance=row.provenance,
            task_id=row.task_id,
            repo_path=row.repo_path,
            created_at=row.created_at,
        )
        for position, row in enumerate(scored, start=1)
    )
    return RankingReceipt(
        policy=safe_policy.receipt_fields(),
        ranked=ranked,
        usage_applied=usage_applied,
        usage_reason=usage_reason,
        considered=len(candidates),
        dropped_below_floor=below,
        floor=safe_policy.floor_score,
        untrusted_penalised=penalised,
        unknown_usage=unknown_usage,
        at=stamp,
        report_ranking=report_ranking,
    )


# ---------------------------------------------------------------------------
# The metrics the ablation arm measures. Every one of them can return ``None``
# with a reason, because "we did not measure it" and "we measured zero" must
# not look the same in a summary table.
# ---------------------------------------------------------------------------


def recall_at_k(
    retrieved_ids: Sequence[Any],
    expected_ids: Sequence[Any],
    *,
    k: int = 5,
) -> Dict[str, Any]:
    """Return recall@k over ONE query, with the expected set named.

    ``retrieved_ids`` is the ranked id list; ``expected_ids`` is the labelled
    gold set. A memory id that appears twice in ``retrieved`` counts once --
    a duplicate hit is not extra recall, and counting it would let a
    degenerate retriever score 1.0.

    Returns ``{"recall_at_k": None, "reason": ...}`` when there is no labelled
    gold set, rather than 0.0.
    """
    try:
        cutoff = max(1, int(k))
    except (TypeError, ValueError):
        cutoff = 5
    gold = {str(item) for item in (expected_ids or ()) if item not in (None, "")}
    if not gold:
        return {
            "recall_at_k": None,
            "k": cutoff,
            "retrieved": len(retrieved_ids or ()),
            "hits": 0,
            "gold": 0,
            "reason": (
                "this query has no labelled gold set, so recall was not "
                "measured; reporting 0.0 would say 'we looked and found "
                "nothing' when nobody looked"
            ),
            "measured": False,
        }
    seen: List[str] = []
    for item in retrieved_ids or ():
        key = str(item)
        if key not in seen:
            seen.append(key)
    top = seen[:cutoff]
    hits = sum(1 for key in top if key in gold)
    return {
        "recall_at_k": round(hits / len(gold), 6),
        "k": cutoff,
        "retrieved": len(seen),
        "hits": hits,
        "gold": len(gold),
        "reason": "",
        "measured": True,
    }


def citation_rate(
    injected_ids: Sequence[Any],
    cited_ids: Sequence[Any],
) -> Dict[str, Any]:
    """Return the fraction of INJECTED memories that were actually cited.

    ``vacuous: True`` when nothing was injected. A citation rate over zero
    injected memories is not 0% -- it is nobody took the measurement, and a
    0% next to an OFF arm reads as "citation made things worse".
    """
    injected = [str(item) for item in (injected_ids or ()) if item not in (None, "")]
    cited = {str(item) for item in (cited_ids or ()) if item not in (None, "")}
    if not injected:
        return {
            "citation_rate": None,
            "injected": 0,
            "cited": 0,
            "vacuous": True,
            "reason": (
                "no memory was injected, so no citation was possible; this is "
                "an unmeasured rate and not a rate of zero"
            ),
        }
    hits = sum(1 for key in injected if key in cited)
    return {
        "citation_rate": round(hits / len(injected), 6),
        "injected": len(injected),
        "cited": hits,
        "vacuous": False,
        "reason": "",
    }


def stale_memory_rate(
    usage_rows: Sequence[Mapping[str, Any]],
    *,
    at: Optional[float] = None,
    staleness_days: float = 90.0,
) -> Dict[str, Any]:
    """Return the fraction of memories never cited within ``staleness_days``.

    A memory whose usage row is UNKNOWN (no telemetry at all) counts as stale,
    because "we have no record of it being used" is the operational meaning of
    stale. But when NO row is known, the whole measure is vacuous and that is
    reported rather than answered with 1.0.
    """
    try:
        window = max(0.0, float(staleness_days))
    except (TypeError, ValueError):
        window = 90.0
    moment = float(at) if at is not None else time.time()
    # ``UsageIndex.usage()`` returns a mapping of id -> row, which is the
    # natural thing for a caller to pass straight through. Accepting only a
    # sequence would silently iterate the KEYS (integers) and report
    # ``considered: 0`` -- a measurement that measured nothing.
    if isinstance(usage_rows, MappingABC):
        candidates = list(usage_rows.values())
    else:
        candidates = list(usage_rows or ())
    rows = [row for row in candidates if isinstance(row, MappingABC)]
    if not rows:
        return {
            "stale_memory_rate": None,
            "considered": 0,
            "stale": 0,
            "never_cited": 0,
            "vacuous": True,
            "reason": (
                "no usage telemetry exists for this corpus, so staleness was not "
                "measured; every memory would count as stale and reporting 1.0 "
                "would describe the absence of telemetry rather than the memory"
            ),
        }
    stale = never_cited = 0
    for row in rows:
        known = bool(row.get("known"))
        citations = int(row.get("citations") or 0)
        if not known or citations <= 0:
            never_cited += 1
            stale += 1
            continue
        last = _parse_iso(row.get("last_cited_at"))
        if last is None:
            stale += 1
            continue
        if (moment - last) / 86400.0 > window:
            stale += 1
    return {
        "stale_memory_rate": round(stale / len(rows), 6),
        "considered": len(rows),
        "stale": stale,
        "never_cited": never_cited,
        "staleness_days": window,
        "vacuous": False,
        "reason": "",
    }


def decision_recurrence_rate(
    prior_texts: Sequence[Any],
    current_texts: Sequence[Any],
) -> Dict[str, Any]:
    """Return how much of a prior decision set reappeared in a later run.

    This is the metric that says whether memory is doing any work at all: a
    decision the store already holds and the agent takes AGAIN is a decision
    memory failed to prevent. Normalisation is the same shape the store's own
    dedupe uses (casefold + whitespace collapse), so "the same decision" means
    the same thing in both places.

    Returns ``None`` with a reason when either side is empty: recurrence needs
    two observations, and one of them missing is not a rate of zero.
    """
    prior = {str(item).strip().casefold() for item in (prior_texts or ()) if item}
    current = {str(item).strip().casefold() for item in (current_texts or ()) if item}
    if not prior or not current:
        missing = "prior" if not prior else "current"
        return {
            "decision_recurrence_rate": None,
            "prior": len(prior),
            "current": len(current),
            "recurrent": 0,
            "reason": (
                f"the {missing} decision set is empty, so recurrence could not be "
                "computed; one observation is not a rate of zero"
            ),
            "measured": False,
        }
    recurrent = sum(1 for key in current if key in prior)
    return {
        "decision_recurrence_rate": round(recurrent / len(current), 6),
        "prior": len(prior),
        "current": len(current),
        "recurrent": recurrent,
        "reason": "",
        "measured": True,
    }


def default_ranking_db_path() -> Path:
    """The usage sidecar's default path, beside the search index."""
    from memory.paths import memory_root

    return memory_root() / "usage.db"


def open_default_usage_index() -> UsageIndex:
    """The shared usage sidecar. Env-overridable via ``NEO_MEMORY_USAGE_DB``."""
    override = os.environ.get("NEO_MEMORY_USAGE_DB")
    if override and override.strip():
        return UsageIndex(override)
    return UsageIndex(str(default_ranking_db_path()))
