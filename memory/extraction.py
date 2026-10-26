"""Memory extraction, phase 1 of the two-phase pipeline: LEASED JOBS, BACKOFF,
REDACTION BEFORE WRITE.

WHY TWO PHASES AT ALL
---------------------
The corpus's agent-maintained design splits long-term memory maintenance in two:

**Phase 1 (here)** mines completed transcripts with a cheap model and emits
STRUCTURED proposals. **Phase 2** (:mod:`memory.consolidation`) consolidates
landed memories into filesystem artifacts under a git-baselined memory root, by
a sub-agent reviewing a git-style diff rather than by code rewriting memory
files.

The split is what makes the pipeline safe to run automatically. Phase 1 touches
only untrusted input (a transcript) and produces only PROPOSALS, which is the
half that can go wrong. Phase 2 touches only reviewed input (memories a human
applied) and produces a reviewable diff. Neither phase can silently persist
anything: phase 1 cannot (see :mod:`memory.inbox`), and phase 2's output is a
proposal for the same inbox.

THE JOB QUEUE, AND WHY IT IS LEASED
------------------------------------
Extraction runs in the BACKGROUND while a user works, so two extractors can
observe the same completed session. A "pending -> running" flag is not enough:
a worker that dies mid-session leaves the job stuck in ``running`` for ever. So
each job is LEASED with an expiry, and :meth:`ExtractionQueue.reap` returns an
expired lease to ``pending``. A duplicate lease is therefore possible, and that
is handled rather than prevented -- an idempotent proposal id makes a duplicate
extract harmless.

RETRY BACKOFF IS NOT DECORATIVE
-------------------------------
A transcript that trips the provider's rate limit, or a model that returns
unparseable JSON, will do so again immediately. The backoff is exponential with
a ceiling and a maximum attempt count, and a job that exhausts its attempts goes
to ``failed`` with the reason kept. ``failed`` is a real terminal state, not an
infinite retry: a job queue that retries forever is a queue that fills the disk
with the same error.

REDACTION BEFORE WRITE, LITERALLY
---------------------------------
The transcript is redacted by :func:`shared.security.redact_text` BEFORE it is
stored in the job row. Not before the proposal is written, not before the model
sees it -- before the bytes reach the database. A job queue is a long-lived
store of raw user conversation, and it is the least-examined corner of a memory
system; a credential sitting in it would outlive every later redaction pass.
:meth:`ExtractionQueue.enqueue` returns the redaction receipt so a caller can
see whether anything was removed.

The extractor is INJECTED
------------------------
``extractor`` is a callable, never a hard-coded provider. That is what makes
this testable without a network, and it is also the honest shape: the default
is a DETERMINISTIC rule-based extractor that reports ``model_calls: 0``, so a
pipeline run with no provider configured never claims to have consulted one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.security import redact_text, redact_text_scanned

__all__ = [
    "ExtractionJob",
    "ExtractionQueue",
    "ExtractionReceipt",
    "JobState",
    "default_extraction_db_path",
    "extract_memories",
    "open_default_extraction_queue",
    "rule_based_extractor",
]

#: Bound on a stored transcript. A transcript is unbounded user conversation;
#: the bound is reported, never applied silently, and the receipt names how much
#: was dropped.
MAX_TRANSCRIPT_CHARS = 200_000

#: Backoff schedule. Base 2 s, doubling, ceiling 300 s.
BACKOFF_BASE_S = 2.0
BACKOFF_CEILING_S = 300.0
MAX_ATTEMPTS = 5

#: Default lease duration. Long enough for a cheap extraction call, short
#: enough that a crashed worker's job is back in the queue before an operator
#: would file a bug about it.
DEFAULT_LEASE_S = 120.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    repo_path TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    transcript_chars INTEGER NOT NULL DEFAULT 0,
    transcript_truncated INTEGER NOT NULL DEFAULT 0,
    redacted INTEGER NOT NULL DEFAULT 0,
    redaction_receipt TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    lease_owner TEXT,
    lease_expires_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    result TEXT,
    proposal_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_state
    ON extraction_jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_session
    ON extraction_jobs(session_id);
"""


class JobState(str, Enum):
    """The lifecycle of one extraction job.

    ``failed`` is TERMINAL. An extraction that failed MAX_ATTEMPTS times is not
    going to succeed on the 6th try of the same transcript by the same code, and
    a queue with no terminal failure state retries forever and fills the disk
    with one error.
    """

    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"

    def __str__(self) -> str:
        """Return the serialized state."""
        return self.value


@dataclass(frozen=True)
class ExtractionJob:
    """One extraction job's durable state."""

    job_id: str
    state: JobState
    transcript: str
    transcript_chars: int
    transcript_truncated: bool
    redacted: bool
    attempts: int
    max_attempts: int
    created_at: str
    updated_at: str
    repo_path: str = ""
    session_id: str = ""
    run_id: str = ""
    task_id: str = ""
    lease_owner: str = ""
    lease_expires_at: float = 0.0
    next_attempt_at: float = 0.0
    last_error: str = ""
    proposal_id: str = ""
    result: Optional[Mapping[str, Any]] = None

    @property
    def terminal(self) -> bool:
        """Whether this job will never run again without intervention."""
        return self.state in (JobState.DONE, JobState.FAILED)

    @property
    def exhausted(self) -> bool:
        """Whether this job has used all its attempts."""
        return self.attempts >= self.max_attempts

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection.

        ``transcript`` is INCLUDED because this is the queue's own operator
        surface and a job that cannot show what it was going to extract cannot
        be diagnosed. Callers that render this to a user should redact it; the
        text was redacted at enqueue time, so it already is.
        """
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "transcript": self.transcript,
            "transcript_chars": self.transcript_chars,
            "transcript_truncated": self.transcript_truncated,
            "redacted": self.redacted,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "exhausted": self.exhausted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "repo_path": self.repo_path,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "proposal_id": self.proposal_id,
            "result": dict(self.result) if isinstance(self.result, MappingABC) else None,
            "terminal": self.terminal,
        }


@dataclass(frozen=True)
class ExtractionReceipt:
    """What one extraction run did, end to end.

    ``proposed`` is the number of PROPOSALS written and ``persisted`` is always
    0 from this module -- the field exists so a caller does not have to know
    the rule in order to check it.
    """

    job_id: str
    ok: bool
    proposed: int
    persisted: int
    quarantined: int
    untrusted: int
    downgraded: int
    proposal_id: str
    engine: str
    model_calls: int
    attempts: int
    skipped: bool
    reason: str
    elapsed_s: float = 0.0

    @property
    def store_untouched(self) -> bool:
        """Always True here -- extraction cannot write the store."""
        return self.persisted == 0

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "proposed": self.proposed,
            "persisted": self.persisted,
            "store_untouched": self.store_untouched,
            "quarantined": self.quarantined,
            "untrusted": self.untrusted,
            "downgraded": self.downgraded,
            "proposal_id": self.proposal_id,
            "engine": self.engine,
            "model_calls": self.model_calls,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "reason": self.reason,
            "elapsed_s": round(self.elapsed_s, 6),
            "phase": 1,
            "persists_nothing": True,
        }


def _utc(epoch: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def _backoff_s(attempts: int) -> float:
    """Exponential backoff, capped. ``attempts`` is 1-based."""
    try:
        step = max(1, int(attempts))
    except (TypeError, ValueError):
        step = 1
    return min(BACKOFF_CEILING_S, BACKOFF_BASE_S * (2 ** (step - 1)))


@dataclass
class _QueueState:
    conn: Optional[sqlite3.Connection] = None
    open_error: str = ""
    enqueued: int = 0
    leased: int = 0
    completed: int = 0
    failed: int = 0
    reaped: int = 0
    refusals: int = 0
    last_error: str = ""


class ExtractionQueue:
    """A leased, backed-off job queue for phase-1 extraction.

    Thread-safe through one ``RLock`` and safe across PROCESSES: every
    transition that must be exclusive (lease, complete, fail) runs under
    ``BEGIN IMMEDIATE``, which is the same mechanism
    :class:`memory.decision_store.DecisionStore` uses for cross-instance dedupe.
    Two extractors in two terminals cannot both take the same lease.
    """

    def __init__(self, db_path: Any, *, strict: bool = False) -> None:
        """Open the queue; ``strict`` re-raises an unusable database."""
        self.db_path = str(Path(str(db_path)).resolve())
        self._lock = threading.RLock()
        self._state = _QueueState()
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
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
        """True when the queue can be read and written."""
        return self._state.conn is not None

    def capability(self) -> Dict[str, Any]:
        """Report queue health, including refusals.

        ``refusals`` and ``last_error`` exist because an enqueue that failed
        must be countable. A queue that silently drops a session's transcript
        means that session is never mined, and nothing in the product would say
        so.
        """
        return {
            "available": self.available,
            "db_path": self.db_path,
            "enqueued": self._state.enqueued,
            "leased": self._state.leased,
            "completed": self._state.completed,
            "failed": self._state.failed,
            "reaped": self._state.reaped,
            "refusals": self._state.refusals,
            "last_error": self._state.last_error,
            "reason": (
                "" if self.available else f"queue is not open: {self._state.open_error}"
            ),
        }

    def _refuse(self, reason: str) -> None:
        self._state.refusals += 1
        self._state.last_error = str(reason)[:300]

    # -- enqueue --------------------------------------------------------

    def enqueue(
        self,
        transcript: Any,
        *,
        repo_path: Any = "",
        session_id: Any = "",
        run_id: Any = "",
        task_id: Any = "",
        job_id: Any = "",
        max_attempts: int = MAX_ATTEMPTS,
        at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Store a redacted transcript as a PENDING job.

        Redaction happens HERE, before the INSERT. ``redacted`` and
        ``redaction_receipt`` come back in the receipt so the caller can see
        whether anything was removed -- a queue that silently scrubbed a
        transcript would be indistinguishable from one that stored it raw.
        """
        if not self.available:
            self._refuse(f"queue is not open: {self._state.open_error}")
            return {"ok": False, "job_id": "", "reason": self._state.last_error}
        raw = str(transcript or "")
        if not raw.strip():
            self._refuse("an empty transcript was refused")
            return {"ok": False, "job_id": "", "reason": "empty transcript"}
        truncated = False
        if len(raw) > MAX_TRANSCRIPT_CHARS:
            raw = raw[:MAX_TRANSCRIPT_CHARS]
            truncated = True
        safe, scan = redact_text_scanned(raw)
        redacted = safe != raw or bool(scan.redactions) if hasattr(scan, "redactions") else safe != raw
        moment = float(at) if at is not None else time.time()
        jid = str(job_id or "").strip() or f"job-{uuid.uuid4().hex[:16]}"
        try:
            attempts_allowed = max(1, min(int(max_attempts), 20))
        except (TypeError, ValueError):
            attempts_allowed = MAX_ATTEMPTS
        receipt = scan.to_dict() if hasattr(scan, "to_dict") else {"redacted": redacted}
        with self._lock:
            conn = self._state.conn
            if conn is None:
                self._refuse("queue closed during enqueue")
                return {"ok": False, "job_id": "", "reason": "queue closed"}
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO extraction_jobs (job_id, state, repo_path, "
                    "session_id, run_id, task_id, transcript, transcript_chars, "
                    "transcript_truncated, redacted, redaction_receipt, attempts, "
                    "max_attempts, next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                    (
                        jid,
                        JobState.PENDING.value,
                        str(repo_path or ""),
                        str(session_id or ""),
                        str(run_id or ""),
                        str(task_id or ""),
                        safe,
                        len(safe),
                        1 if truncated else 0,
                        1 if redacted else 0,
                        json.dumps(receipt, default=str),
                        attempts_allowed,
                        moment,
                        moment,
                        moment,
                    ),
                )
                conn.commit()
            except sqlite3.DatabaseError as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                self._refuse(f"{type(exc).__name__}: {exc}")
                return {"ok": False, "job_id": "", "reason": self._state.last_error}
        self._state.enqueued += 1
        return {
            "ok": True,
            "job_id": jid,
            "state": JobState.PENDING.value,
            "transcript_chars": len(safe),
            "transcript_truncated": truncated,
            "redacted": redacted,
            "redaction_receipt": receipt,
            "reason": "",
        }

    # -- lease ----------------------------------------------------------

    def lease(
        self, *, owner: Any = "", lease_s: float = DEFAULT_LEASE_S, at: Optional[float] = None
    ) -> Optional[ExtractionJob]:
        """Atomically lease the oldest due PENDING job, or return ``None``.

        ``None`` means "nothing is due", which is an ordinary outcome and not an
        error: a queue polled faster than sessions complete always finds
        nothing. The caller distinguishes it from a failure via
        :meth:`capability`.
        """
        if not self.available:
            return None
        moment = float(at) if at is not None else time.time()
        try:
            duration = max(1.0, float(lease_s))
        except (TypeError, ValueError):
            duration = DEFAULT_LEASE_S
        who = str(owner or f"worker-{uuid.uuid4().hex[:8]}")
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return None
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM extraction_jobs WHERE state = ? "
                    "AND next_attempt_at <= ? AND attempts < max_attempts "
                    "ORDER BY next_attempt_at ASC, created_at ASC LIMIT 1",
                    (JobState.PENDING.value, moment),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                conn.execute(
                    "UPDATE extraction_jobs SET state = ?, lease_owner = ?, "
                    "lease_expires_at = ?, attempts = attempts + 1, updated_at = ? "
                    "WHERE job_id = ?",
                    (
                        JobState.LEASED.value,
                        who,
                        moment + duration,
                        moment,
                        str(row["job_id"]),
                    ),
                )
                conn.commit()
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return None
        self._state.leased += 1
        return self._reload(str(row["job_id"]))

    def reap(self, *, at: Optional[float] = None) -> int:
        """Return jobs whose lease EXPIRED to ``pending``. Returns the count.

        This is the difference between a queue and a stuck flag. A worker that
        is killed mid-extraction leaves a ``leased`` row; without a reap that row
        is never attempted again and the session is silently never mined.
        """
        if not self.available:
            return 0
        moment = float(at) if at is not None else time.time()
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return 0
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE extraction_jobs SET state = ?, lease_owner = NULL, "
                    "lease_expires_at = NULL, next_attempt_at = ?, updated_at = ? "
                    "WHERE state = ? AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at <= ?",
                    (
                        JobState.PENDING.value,
                        moment,
                        moment,
                        JobState.LEASED.value,
                        moment,
                    ),
                )
                conn.commit()
                reaped = int(cursor.rowcount or 0)
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return 0
        self._state.reaped += reaped
        return reaped

    # -- finish ---------------------------------------------------------

    def complete(
        self, job_id: Any, result: Mapping[str, Any], *, at: Optional[float] = None
    ) -> bool:
        """Mark a job ``done``. The stored result is redacted before writing."""
        return self._finish(
            job_id,
            state=JobState.DONE,
            result=result,
            error="",
            at=at,
        )

    def fail(
        self, job_id: Any, error: Any, *, at: Optional[float] = None
    ) -> Dict[str, Any]:
        """Mark a job failed, or re-queue it with BACKOFF.

        A job under its attempt limit goes back to ``pending`` with
        ``next_attempt_at`` set by exponential backoff. A job AT its limit goes
        to ``failed`` and stays there. Both outcomes are returned, so the caller
        knows whether it will be retried without querying again.
        """
        if not self.available:
            return {"ok": False, "state": "", "will_retry": False, "reason": "queue is not open"}
        job = self._reload(str(job_id))
        if job is None:
            return {"ok": False, "state": "", "will_retry": False, "reason": f"no job {job_id!r}"}
        moment = float(at) if at is not None else time.time()
        if job.attempts >= job.max_attempts:
            self._finish(job.job_id, state=JobState.FAILED, result=None, error=error, at=moment)
            self._state.failed += 1
            return {
                "ok": True,
                "state": JobState.FAILED.value,
                "will_retry": False,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "reason": (
                    f"job {job.job_id} exhausted {job.max_attempts} attempts and is "
                    "now terminally failed; it will not be retried"
                ),
            }
        delay = _backoff_s(job.attempts)
        self._finish(
            job.job_id,
            state=JobState.PENDING,
            result=None,
            error=error,
            at=moment,
            next_attempt_at=moment + delay,
        )
        return {
            "ok": True,
            "state": JobState.PENDING.value,
            "will_retry": True,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "backoff_s": delay,
            "next_attempt_at": _utc(moment + delay),
            "reason": "",
        }

    def _finish(
        self,
        job_id: Any,
        *,
        state: JobState,
        result: Optional[Mapping[str, Any]],
        error: Any,
        at: Optional[float],
        next_attempt_at: Optional[float] = None,
    ) -> bool:
        if not self.available:
            return False
        moment = float(at) if at is not None else time.time()
        encoded = None
        if isinstance(result, MappingABC):
            # The result carries model output, which is untrusted. Redact it
            # before it lands, for the same reason the transcript is redacted
            # before it lands.
            safe_result = {
                str(key): redact_text(value) for key, value in result.items()
            }
            encoded = json.dumps(safe_result, default=str)
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE extraction_jobs SET state = ?, result = ?, "
                    "last_error = ?, lease_owner = NULL, lease_expires_at = NULL, "
                    "updated_at = ?, next_attempt_at = ? WHERE job_id = ?",
                    (
                        state.value,
                        encoded,
                        str(error or "")[:500],
                        moment,
                        float(next_attempt_at) if next_attempt_at is not None else moment,
                        str(job_id),
                    ),
                )
                conn.commit()
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return False
        return True

    # -- read -----------------------------------------------------------

    def _reload(self, job_id: str) -> Optional[ExtractionJob]:
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            except sqlite3.DatabaseError:
                return None
        return None if row is None else _row_to_job(row)

    def get(self, job_id: Any) -> Optional[ExtractionJob]:
        """Return one job by id, or ``None``."""
        return self._reload(str(job_id or ""))

    def counts(self) -> Dict[str, Any]:
        """Return per-state counts, for a status surface."""
        result: Dict[str, int] = {state.value: 0 for state in JobState}
        if self.available:
            with self._lock:
                conn = self._state.conn
                if conn is not None:
                    try:
                        for row in conn.execute(
                            "SELECT state, COUNT(*) AS n FROM extraction_jobs "
                            "GROUP BY state"
                        ):
                            result[str(row["state"])] = int(row["n"])
                    except sqlite3.DatabaseError:
                        result = {state.value: 0 for state in JobState}
        return {
            "available": self.available,
            "counts": result,
            "total": sum(result.values()),
            "reason": "" if self.available else self._state.open_error,
            **_state_counters(self._state),
        }

    def close(self) -> None:
        """Close the queue; repeated calls are harmless."""
        with self._lock:
            if self._state.conn is not None:
                try:
                    self._state.conn.close()
                finally:
                    self._state.conn = None

    def __enter__(self) -> "ExtractionQueue":
        """Return this queue as a context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Close the queue when leaving a context-manager block."""
        self.close()


def _state_counters(state: _QueueState) -> Dict[str, int]:
    return {
        "counter_enqueued": state.enqueued,
        "counter_leased": state.leased,
        "counter_completed": state.completed,
        "counter_failed": state.failed,
        "counter_reaped": state.reaped,
        "counter_refusals": state.refusals,
    }


def _row_to_job(row: sqlite3.Row) -> ExtractionJob:
    """Map a job row, reporting an unparseable result as ``None``."""
    raw_result = row["result"]
    parsed: Optional[Mapping[str, Any]] = None
    if raw_result:
        try:
            candidate = json.loads(str(raw_result))
            if isinstance(candidate, MappingABC):
                parsed = candidate
        except (TypeError, ValueError):
            parsed = None
    try:
        state = JobState(str(row["state"]))
    except ValueError:
        state = JobState.PENDING
    return ExtractionJob(
        job_id=str(row["job_id"]),
        state=state,
        transcript=str(row["transcript"] or ""),
        transcript_chars=int(row["transcript_chars"] or 0),
        transcript_truncated=bool(row["transcript_truncated"]),
        redacted=bool(row["redacted"]),
        attempts=int(row["attempts"] or 0),
        max_attempts=int(row["max_attempts"] or 5),
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
        repo_path=str(row["repo_path"] or ""),
        session_id=str(row["session_id"] or ""),
        run_id=str(row["run_id"] or ""),
        task_id=str(row["task_id"] or ""),
        lease_owner=str(row["lease_owner"] or ""),
        lease_expires_at=float(row["lease_expires_at"] or 0.0),
        next_attempt_at=float(row["next_attempt_at"] or 0.0),
        last_error=str(row["last_error"] or ""),
        proposal_id=str(row["proposal_id"] or ""),
        result=parsed,
    )


# ---------------------------------------------------------------------------
# The extractor seam.
# ---------------------------------------------------------------------------

_DECISION_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:decision|decided|chose|choosing|we will|approach)"
    r"\s*[:\-]\s*(?P<body>.{8,400})$"
)
_CONVENTION_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:convention|always|never|prefer|prefers|rule|style)"
    r"\s*[:\-]\s*(?P<body>.{8,400})$"
)
_GOTCHA_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:gotcha|caveat|watch out|note that|beware|"
    r"turns out|actually)\s*[:\-]?\s*(?P<body>.{8,400})$"
)
_FACT_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:fact|learned|it turns out|discovered)\s*[:\-]\s*"
    r"(?P<body>.{8,400})$"
)

#: Which regex maps to which category. The category vocabulary is the store's
#: existing one, so an extracted fact and a hand-recorded one are the same kind
#: of row.
_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("decision", _DECISION_RE),
    ("convention", _CONVENTION_RE),
    ("gotcha", _GOTCHA_RE),
    ("fact", _FACT_RE),
)


def rule_based_extractor(transcript: str) -> List[Dict[str, Any]]:
    """A deterministic, zero-model extractor.

    This is the DEFAULT, and it is deterministic on purpose: a memory pipeline
    whose extraction moves with the provider's mood cannot be tested, cannot be
    diffed between two runs, and cannot be blamed for anything. A real cheap
    model is injected by the caller when one is configured, and the receipt says
    which of the two ran via ``engine`` and ``model_calls``.
    """
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for category, pattern in _RULES:
        for match in pattern.finditer(str(transcript or "")):
            body = " ".join(match.group("body").split()).strip()
            if len(body) < 8:
                continue
            key = body.casefold()
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "text": body,
                    "category": category,
                    "rationale": (
                        f"matched the deterministic {category} rule in the "
                        "transcript; no model was consulted"
                    ),
                    "evidence": [],
                }
            )
    return results


def extract_memories(
    transcript: str,
    *,
    extractor: Optional[Callable[[str], Sequence[Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run ``extractor`` over ``transcript`` and return ``(memories, receipt)``.

    The receipt names ``engine`` and ``model_calls`` so a caller can never
    report an extraction that used no model as though it had. An extractor that
    raises is caught: extraction failure is a job failure to be retried with
    backoff, not a crash of the caller that leased the job.
    """
    engine = "rules" if extractor is None else f"callable:{getattr(extractor, '__name__', 'extractor')}"
    calls = 0 if extractor is None else 1
    if extractor is None:
        try:
            memories = rule_based_extractor(transcript)
        except Exception as exc:  # a broken rule set must be reportable
            return [], {
                "engine": engine,
                "model_calls": calls,
                "ok": False,
                "count": 0,
                "reason": f"the rule-based extractor failed: {type(exc).__name__}",
            }
    else:
        if not callable(extractor):
            return [], {
                "engine": engine,
                "model_calls": 0,
                "ok": False,
                "count": 0,
                "reason": f"extractor {extractor!r} is not callable",
            }
        try:
            raw = extractor(str(transcript or ""))
        except Exception as exc:
            return [], {
                "engine": engine,
                "model_calls": calls,
                "ok": False,
                "count": 0,
                "reason": f"the extractor raised: {type(exc).__name__}: {exc}"[:200],
            }
        memories = []
        if isinstance(raw, MappingABC):
            raw = [raw]
        for item in raw or ():
            if isinstance(item, str):
                memories.append({"text": item})
            elif isinstance(item, MappingABC) and item.get("text"):
                memories.append(dict(item))
    return memories, {
        "engine": engine,
        "model_calls": calls,
        "ok": True,
        "count": len(memories),
        "reason": "",
    }


def default_extraction_db_path() -> Path:
    """The queue's default path, under the memory root."""
    from memory.paths import memory_root

    return memory_root() / "extraction.db"


def open_default_extraction_queue() -> ExtractionQueue:
    """The shared extraction queue. Env-overridable via ``NEO_MEMORY_EXTRACTION_DB``."""
    import os

    override = os.environ.get("NEO_MEMORY_EXTRACTION_DB")
    if override and override.strip():
        return ExtractionQueue(override)
    return ExtractionQueue(str(default_extraction_db_path()))
