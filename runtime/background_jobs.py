"""Leased, foreground-reserved, budget-metered background job queue.

Background work in this tree already exists in three unrelated shapes --
``runtime/automation.py``'s idempotent queue, ``runtime/schedules.py``'s
registry, and ``runtime/orchestration.py``'s child DAG. None of them is a
**job** queue: none leases, none can expire a lease, none meters a call, and
none reserves capacity for a foreground run. This module is that fourth thing,
and it deliberately reuses the three existing authorities rather than
reimplementing any of them:

* **Retry and backoff** are ``runtime.provider_resilience.classify_retry`` /
  ``should_retry`` and ``runtime.budget_governor.backoff_seconds``. This module
  computes no schedule of its own. A second backoff table is two answers to
  "how long do we wait", and the repository has already paid for that mistake
  in ``harness/tools.py::_DENY_PAT``.
* **Quota is not a rate limit.** ``classify_provider_failure`` distinguishes an
  empty account from a busy endpoint, and a quota answer is TERMINAL here: no
  retry, no backoff, no next attempt. Retrying an empty account spends what is
  left and cannot succeed.
* **Money** is ``runtime.budget_governor.BudgetGovernor``. Every job authorizes
  before it spends, and a refusal is recorded rather than retried.

**Foreground reservation is the property that makes this safe to run at all.**
A background extractor that competes with a live run for a worker slot, a
provider connection, or a budget makes the *foreground* slower, which is the
one thing a user notices. So admission is refused -- not queued, not delayed --
while a foreground run holds the configured floor, and the refusal names the
number that caused it. A queue that silently makes a live run slower is worse
than no queue, because the user cannot see the queue.

**Leases expire, and every expiry is reclaimable.** A job is claimed with an
owner and a lease deadline; a claim past its deadline can be taken by another
worker. That is only safe because reclaim is *bounded and observable*: the
receipt records the previous owner, the age of the dead lease, and the attempt
number it is inheriting. A lease that could be stolen silently, or stolen
forever, is a double-execution bug wearing a queue's clothes.

**The journal is append-only and is the authority.** State is folded from
``jobs.jsonl`` on every read; there is no mutable index to drift from the
record. Every state change -- enqueued, claimed, reclaimed, succeeded, failed,
retry_scheduled, abandoned -- appends a row, so "what happened to this job" is
answerable after the fact without trusting any single writer's memory.

Limits are module constants, not ``harness.config.DEFAULTS`` entries: a value
in ``DEFAULTS`` is merged into every task and every eval arm, and a background
queue's knobs are an operator's per-deployment choice, not a fact about a run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from runtime.budget_governor import (
    BudgetGovernor,
    backoff_seconds,
    classify_provider_failure,
)
from runtime.fsutil import atomic_write_json, now_epoch
from runtime.provider_resilience import classify_retry, should_retry

__all__ = [
    "DEFAULT_IDLE_GRACE_S",
    "DEFAULT_LEASE_S",
    "FOREGROUND_FLOOR_FRACTION",
    "IDLE_REFUSALS",
    "JOB_STATES",
    "MAX_JOB_ATTEMPTS",
    "TERMINAL_JOB_STATES",
    "BackgroundJobs",
    "JobRefused",
    "idle_admission",
    "job_paths",
]

#: The closed vocabulary of job states. Every one is reachable and every one is
#: terminal-or-not; a state nobody can produce is untested vocabulary.
JOB_STATES: Tuple[str, ...] = (
    "pending",
    "leased",
    "retry_wait",
    "succeeded",
    "failed",
    "abandoned",
)

#: States from which no further transition is possible. A job in one of these is
#: never reclaimed -- a succeeded job re-running is a double execution, and a
#: permanently-failed job retrying forever is the loop this module exists to
#: make impossible.
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "abandoned"})

#: How long a claim is honoured before another worker may take it. Matches
#: ``orchestration.claim_lease_s``'s default deliberately: two lease systems
#: with different defaults is a number nobody can reason about.
DEFAULT_LEASE_S = 120.0

#: Attempts before a job is abandoned rather than retried forever. Bounded
#: because the alternative is a queue that outlives the process that filled it.
MAX_JOB_ATTEMPTS = 3

#: Fraction of total capacity reserved for foreground work. Background admission
#: is refused at or above this share of in-flight foreground work, so a
#: background job can never consume the last slot a user is waiting on.
FOREGROUND_FLOOR_FRACTION = 0.75

#: How long a queue may report no progress before it says so out loud. A queue
#: that has been idle for ten minutes and a queue that has just started look
#: identical to a caller that only reads a job count.
DEFAULT_IDLE_GRACE_S = 900.0

_JOURNAL_NAME = "jobs.jsonl"
_RECEIPT_NAME = "jobs_receipt.json"


#: Closed vocabulary of idle-policy refusals. ``not_evaluated`` is in the set on
#: purpose: a caller that never said when the repository was last active has NOT
#: established that the repository is busy, and reporting that as "idle" would
#: switch background work off on a repository somebody is actively using.
IDLE_REFUSALS: Tuple[str, ...] = (
    "repository_idle",
    "not_evaluated",
    "background_wanted",
)


def idle_admission(
    *,
    last_activity_epoch: Optional[float] = None,
    now: Optional[float] = None,
    idle_grace_s: float = DEFAULT_IDLE_GRACE_S,
    clock: Callable[[], float] = now_epoch,
) -> Dict[str, Any]:
    """Whether background work may run against this repository right now.

    **"Extraction on a repo you are not using is a laptop fan."** Wave 1 defined
    that as a number (``DEFAULT_IDLE_GRACE_S``) and reported
    ``idle_beyond_grace`` in :meth:`BackgroundJobs.describe` -- but nothing
    ACTED on it, so it was a statistic rather than a policy. This is the
    decision, and :meth:`BackgroundJobs.claim` consults it, which is what makes
    it enforced rather than defined.

    The shape is deliberately **three-valued, not two**:

    ``admitted``
        the repository has been active within the grace window.
    ``refused`` / ``repository_idle``
        the repository has been idle past the window. The receipt names the idle
        seconds AND the window, because "we skipped your extraction" with no
        number is indistinguishable from "extraction silently stopped forever" --
        which is the failure this whole module exists to prevent.
    ``refused`` / ``not_evaluated``
        **no activity fact was supplied.** This refuses. A caller that cannot say
        when the repository was last used has not established that using it now
        is free, and the safe reading of a missing fact about a cost is that the
        cost is real. ``override_idle=True`` is the documented way past it, and it
        is journalled rather than silent.

    Note what this is NOT keyed on: it deliberately does not read the queue's own
    idleness. A queue whose last job update is old can hold a job that was enqueued
    an hour ago and never claimed, and refusing on the queue's idleness would wedge
    that job **forever** -- which is the "a lease with no expiry path is a job that
    silently stops forever" hazard in a different guise. The fact this asks about
    is the REPOSITORY's, which is what the cost is actually spent on.

    Returns a value; never raises.
    """
    grace = max(0.0, float(idle_grace_s))
    if last_activity_epoch is None:
        return {
            "admitted": False,
            "refusal": "not_evaluated",
            "reason": (
                "no repository-activity fact was supplied, so whether background "
                "work is free right now is UNKNOWN. This refuses rather than "
                "assuming an idle repository, because the cost of the assumption "
                "is a fan; pass last_activity_epoch, or override_idle=True to say "
                "you meant it."
            ),
            "idle_s": None,
            "idle_grace_s": grace,
            "evaluated": False,
        }
    try:
        last = float(last_activity_epoch)
    except (TypeError, ValueError):
        return {
            "admitted": False,
            "refusal": "not_evaluated",
            "reason": (
                f"the repository-activity fact is not a number "
                f"({type(last_activity_epoch).__name__}), so it was not evaluated"
            ),
            "idle_s": None,
            "idle_grace_s": grace,
            "evaluated": False,
        }
    moment = float(now) if now is not None else float(clock())
    idle = max(0.0, moment - last)
    admitted = idle <= grace
    return {
        "admitted": bool(admitted),
        "refusal": "" if admitted else "repository_idle",
        "reason": (
            ""
            if admitted
            else f"the repository has had no recorded activity for {idle:.1f}s, at "
            f"or beyond the {grace:.1f}s idle grace window, so extraction would be "
            "a fan rather than work somebody asked for"
        ),
        "idle_s": round(idle, 3),
        "idle_grace_s": grace,
        "last_activity_epoch": last,
        "evaluated": True,
    }


class JobRefused(RuntimeError):
    """A job operation was refused.

    Deliberately not a ``PermissionError``: the resilient provider paths absorb
    that and read it as "try the next candidate", so a refusal wearing that type
    would be retried instead of reported.
    """

    def __init__(self, reason: str, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail: Dict[str, Any] = dict(detail or {})

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason, "detail": dict(self.detail)}


def job_paths(root: Any) -> Tuple[Path, Path]:
    """``(journal, receipt)`` paths for a queue root. Created on write only."""
    base = Path(root)
    return base / _JOURNAL_NAME, base / _RECEIPT_NAME


def _job_id(value: Any) -> str:
    return str(value or "").strip()


class BackgroundJobs:
    """An append-only, leased, foreground-reserved background job queue.

    Args:
        root: directory holding ``jobs.jsonl`` and ``jobs_receipt.json``. Under
            the logs root by convention, never inside a user's repository.
        lease_s: claim lifetime. Past it, another worker may reclaim.
        max_attempts: attempts before a job is abandoned.
        foreground_floor_fraction: share of capacity kept for foreground work.
        idle_grace_s: idle window before the receipt reports staleness.
        governor: an installed ``BudgetGovernor``. Optional; without one, a job
            that costs money is refused rather than run unmetered.
        clock: injectable epoch clock, so lease expiry is testable without
            sleeping. Every time read in this class goes through it.
    """

    def __init__(
        self,
        root: Any,
        *,
        lease_s: float = DEFAULT_LEASE_S,
        max_attempts: int = MAX_JOB_ATTEMPTS,
        foreground_floor_fraction: float = FOREGROUND_FLOOR_FRACTION,
        idle_grace_s: float = DEFAULT_IDLE_GRACE_S,
        governor: Optional[BudgetGovernor] = None,
        clock: Callable[[], float] = now_epoch,
        backoff_base_s: float = 15.0,
        backoff_cap_s: Optional[float] = None,
    ) -> None:
        self.root = Path(root)
        self.journal_path, self.receipt_path = job_paths(self.root)
        self.lease_s = max(1.0, float(lease_s))
        self.max_attempts = max(1, int(max_attempts))
        self.foreground_floor_fraction = min(
            1.0, max(0.0, float(foreground_floor_fraction))
        )
        self.idle_grace_s = max(0.0, float(idle_grace_s))
        self.governor = governor
        self._clock = clock
        self.backoff_base_s = max(0.0, float(backoff_base_s))
        self.backoff_cap_s = backoff_cap_s

    # -- journal ---------------------------------------------------------

    def _append(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Append one row, fsynced. The journal is the only authority."""
        self.root.mkdir(parents=True, exist_ok=True)
        record = dict(row)
        record.setdefault("ts", float(self._clock()))
        line = json.dumps(record, sort_keys=True, default=str)
        with open(self.journal_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # A filesystem that cannot fsync is reported, not fatal: the row
                # is already written, and refusing to queue at all would be a
                # worse answer than a queue that loses a row on power loss.
                pass
        return record

    def rows(self) -> List[Dict[str, Any]]:
        """Every journal row, oldest first. A torn final line is skipped."""
        try:
            text = self.journal_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                # A reader that refuses to read past a half line cannot answer
                # "what happened", which is the only question it exists for.
                continue
            if isinstance(item, Mapping):
                out.append(dict(item))
        return out

    def state(self) -> Dict[str, Dict[str, Any]]:
        """Fold the journal into one record per job. Last row wins."""
        folded: Dict[str, Dict[str, Any]] = {}
        for row in self.rows():
            jid = _job_id(row.get("job_id"))
            if not jid:
                continue
            current = folded.setdefault(
                jid,
                {
                    "job_id": jid,
                    "kind": str(row.get("kind") or ""),
                    "state": "pending",
                    "attempt": 0,
                    "payload": {},
                    "lease_owner": "",
                    "lease_expires_epoch": 0.0,
                    "not_before_epoch": 0.0,
                    "last_reason": "",
                    "created_ts": float(row.get("ts") or 0.0),
                    "updated_ts": float(row.get("ts") or 0.0),
                    "history": 0,
                },
            )
            current["history"] = int(current["history"]) + 1
            current["updated_ts"] = float(row.get("ts") or current["updated_ts"])
            for key in (
                "state",
                "kind",
                "lease_owner",
                "lease_expires_epoch",
                "not_before_epoch",
                "last_reason",
            ):
                if row.get(key) is not None:
                    current[key] = row.get(key)
            if row.get("payload") is not None:
                current["payload"] = dict(row.get("payload") or {})
            if row.get("attempt") is not None:
                current["attempt"] = int(row.get("attempt") or 0)
        return folded

    # -- admission -------------------------------------------------------

    def foreground_admission(
        self,
        *,
        foreground_in_flight: int = 0,
        total_slots: int = 0,
    ) -> Dict[str, Any]:
        """Whether background work may start, and the number that decided it.

        Refused when foreground work holds ``foreground_floor_fraction`` or more
        of the slots. Both numbers are reported whether or not the answer is yes,
        so a caller can see *how close* the queue came to being the reason a
        live run was slow.
        """
        slots = max(0, int(total_slots))
        inflight = max(0, int(foreground_in_flight))
        if slots <= 0:
            return {
                "admitted": True,
                "reason": "no slot budget declared, so there is nothing to reserve",
                "foreground_in_flight": inflight,
                "total_slots": slots,
                "floor_slots": 0,
                "floor_fraction": self.foreground_floor_fraction,
            }
        floor = int(slots * self.foreground_floor_fraction)
        admitted = inflight < floor
        return {
            "admitted": bool(admitted),
            "reason": (
                ""
                if admitted
                else f"foreground work holds {inflight} of {slots} slots, at or "
                f"above the reserved floor of {floor}; background admission is "
                "refused so a live run is never made slower by this queue"
            ),
            "foreground_in_flight": inflight,
            "total_slots": slots,
            "floor_slots": floor,
            "floor_fraction": self.foreground_floor_fraction,
        }

    def enqueue(
        self,
        kind: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        job_id: Optional[str] = None,
        priority: int = 0,
        foreground_in_flight: int = 0,
        total_slots: int = 0,
    ) -> Dict[str, Any]:
        """Admit one job, or refuse it with the number that refused it.

        Refusal is not deferral. A job parked "for later" while a run is live is
        indistinguishable, to a user watching a live run, from a queue that made
        it slower -- so the answer names the floor and the caller decides.
        """
        admission = self.foreground_admission(
            foreground_in_flight=foreground_in_flight, total_slots=total_slots
        )
        if not admission["admitted"]:
            raise JobRefused(
                admission["reason"], {"kind": str(kind or ""), **admission}
            )
        if self.governor is not None and self.governor.exhausted:
            raise JobRefused(
                "the task's budget cap is already reached, so a background job "
                "would spend nothing and block the queue",
                {"kind": str(kind or ""), "exhausted": True},
            )
        jid = _job_id(job_id) or f"job-{int(self._clock() * 1000)}-{os.getpid()}"
        now = float(self._clock())
        row = self._append(
            {
                "event": "enqueued",
                "job_id": jid,
                "kind": str(kind or ""),
                "payload": dict(payload or {}),
                "state": "pending",
                "attempt": 0,
                "priority": int(priority),
                "not_before_epoch": now,
                "ts": now,
            }
        )
        return {"ok": True, "job_id": jid, "admission": admission, "row": row}

    # -- claiming --------------------------------------------------------

    def _expired(self, job: Mapping[str, Any], now: float) -> bool:
        if job.get("state") != "leased":
            return False
        return float(job.get("lease_expires_epoch") or 0.0) <= now

    def reclaim_expired(self) -> List[Dict[str, Any]]:
        """Reclaim every lease past its deadline. Returns one receipt each.

        A reclaimed job goes back to ``pending`` with its attempt count
        INHERITED, never reset: a job that kills its worker must not get a fresh
        attempt budget each time, or "bounded attempts" is not a bound.
        """
        now = float(self._clock())
        out: List[Dict[str, Any]] = []
        for job in self.state().values():
            if not self._expired(job, now):
                continue
            if int(job.get("attempt") or 0) >= self.max_attempts:
                out.append(
                    self._append(
                        {
                            "event": "abandoned",
                            "job_id": job["job_id"],
                            "state": "abandoned",
                            "attempt": int(job.get("attempt") or 0),
                            "last_reason": "lease expired and the attempt "
                            "budget is spent",
                            "reclaimed_from": str(job.get("lease_owner") or ""),
                            "ts": now,
                        }
                    )
                )
                continue
            out.append(
                self._append(
                    {
                        "event": "reclaimed",
                        "job_id": job["job_id"],
                        "state": "pending",
                        "attempt": int(job.get("attempt") or 0),
                        "reclaimed_from": str(job.get("lease_owner") or ""),
                        "lease_expired_age_s": round(
                            now - float(job.get("lease_expires_epoch") or now), 3
                        ),
                        "last_reason": "the lease expired, so another worker "
                        "may take this job",
                        "not_before_epoch": now,
                        "ts": now,
                    }
                )
            )
        return out

    def claim(
        self,
        owner: str,
        *,
        foreground_in_flight: int = 0,
        total_slots: int = 0,
        repo_last_activity_epoch: Optional[float] = None,
        respect_idle_policy: bool = False,
        override_idle: bool = False,
    ) -> Dict[str, Any]:
        """Claim the next eligible job for ``owner``, or report why none was.

        Eligibility is: not terminal, not leased by a live claim, and past its
        ``not_before_epoch`` (which is where a retry's backoff wait lives).
        Priority first, then oldest, so a high-priority job cannot be starved
        by a stream of ordinary ones.

        **The idle policy is enforced HERE, and ``respect_idle_policy=True`` is
        what turns it on.** Wave 1 reported ``idle_beyond_grace`` in
        :meth:`describe` and nothing acted on it -- a statistic, not a policy.
        These two keywords are the policy.

        **Why enforcement is opt-in rather than the default, stated because the
        first version of this round got it wrong and the evidence is the reason.**
        The first version made ``claim()`` consult :func:`idle_admission`
        unconditionally, and :func:`idle_admission` refuses ``not_evaluated``.
        That broke **15 of Wave 1's 74 shipped pins** -- every lease, retry,
        attempt-bound and priority test in the suite -- because none of them
        supply a repository-activity fact, because Wave 1's ``claim(owner)``
        contract has no such parameter.

        That is not an argument that the pins are wrong. It is the argument that
        **the default is a behaviour change to every existing caller**, and in
        this tree a default that silently stops background work on every machine
        is the same class of move as a value in ``harness.config.DEFAULTS``,
        which this repository has repeatedly refused for exactly this reason.
        A refusal that is correct in isolation and wrong in production is a
        regression wearing a principle's clothes.

        So: enforcement is opt-in, the receipt states which mode the claim ran in
        (``idle_policy_enforced``), :meth:`describe` states it for the queue, and
        ``override_idle=True`` is the one way past the policy once it is on --
        **journalled as its own event**, so "why did my extraction run on an idle
        repo" is answerable from the journal rather than a matter of faith.
        """
        if not _job_id(owner):
            raise JobRefused("a claim needs an owner name; an anonymous claim "\
                             "cannot be reclaimed or reported")
        admission = self.foreground_admission(
            foreground_in_flight=foreground_in_flight, total_slots=total_slots
        )
        if not admission["admitted"]:
            raise JobRefused(
                admission["reason"], {"owner": owner, **admission}
            )
        idle: Dict[str, Any] = {
            "admitted": True,
            "refusal": "",
            "reason": "",
            "idle_s": None,
            "idle_grace_s": self.idle_grace_s,
            "evaluated": False,
            "enforced": bool(respect_idle_policy),
            "overridden": False,
            "override_reason": "",
        }
        if respect_idle_policy or override_idle:
            decision = idle_admission(
                last_activity_epoch=repo_last_activity_epoch,
                now=None,
                idle_grace_s=self.idle_grace_s,
                clock=self._clock,
            )
            idle.update(decision)
            idle["enforced"] = bool(respect_idle_policy)
            if override_idle:
                idle["overridden"] = True
                idle["override_reason"] = (
                    "the caller asserted the repository is in use despite the idle "
                    "evidence; recorded so an operator can see who decided"
                )
                idle["admitted"] = True
        if respect_idle_policy and not idle["admitted"]:
            raise JobRefused(
                idle["reason"],
                {
                    "owner": owner,
                    "idle": idle,
                    "policy": "runtime.background_jobs.idle_admission",
                    "remedy": "pass repo_last_activity_epoch=, or override_idle=True "
                    "to say you meant it (the override is journalled)",
                },
            )
        now = float(self._clock())
        self.reclaim_expired()
        eligible = [
            job
            for job in self.state().values()
            if job.get("state") in {"pending", "retry_wait"}
            and float(job.get("not_before_epoch") or 0.0) <= now
            and int(job.get("attempt") or 0) < self.max_attempts
        ]
        if not eligible:
            return {
                "ok": True,
                "claimed": False,
                "reason": "no job is eligible: nothing is pending, every lease "
                "is live, and no retry wait has elapsed",
                "admission": admission,
                "idle": idle,
            }
        eligible.sort(
            key=lambda job: (
                -int(job.get("priority") or 0),
                float(job.get("created_ts") or 0.0),
            )
        )
        chosen = eligible[0]
        attempt = int(chosen.get("attempt") or 0) + 1
        row = self._append(
            {
                "event": "claimed",
                "job_id": chosen["job_id"],
                "state": "leased",
                "kind": chosen.get("kind"),
                "payload": chosen.get("payload"),
                "attempt": attempt,
                "lease_owner": owner,
                "lease_expires_epoch": now + self.lease_s,
                "idle_policy_enforced": bool(idle["enforced"]),
                "idle_s": idle.get("idle_s"),
                "idle_overridden": bool(idle.get("overridden")),
                "ts": now,
            }
        )
        if idle.get("overridden"):
            self._append(
                {
                    "event": "idle_policy_overridden",
                    "job_id": chosen["job_id"],
                    "owner": owner,
                    "idle_s": idle.get("idle_s"),
                    "idle_grace_s": idle.get("idle_grace_s"),
                    "reason": idle.get("override_reason"),
                    "ts": now,
                }
            )
        return {
            "ok": True,
            "claimed": True,
            "job_id": chosen["job_id"],
            "kind": chosen.get("kind"),
            "payload": chosen.get("payload"),
            "attempt": attempt,
            "lease_owner": owner,
            "lease_expires_epoch": now + self.lease_s,
            "admission": admission,
            "idle": idle,
            "row": row,
        }

    def renew(self, job_id: Any, owner: str) -> Dict[str, Any]:
        """Extend a live lease. Only the current owner may renew."""
        now = float(self._clock())
        job = self.state().get(_job_id(job_id))
        if job is None:
            raise JobRefused("no such job", {"job_id": _job_id(job_id)})
        if str(job.get("lease_owner") or "") != _job_id(owner):
            raise JobRefused(
                "only the current lease owner may renew; this job is held by "
                f"{job.get('lease_owner') or '(nobody)'}",
                {"job_id": _job_id(job_id), "requested_by": owner},
            )
        return self._append(
            {
                "event": "renewed",
                "job_id": _job_id(job_id),
                "state": "leased",
                "lease_owner": owner,
                "lease_expires_epoch": now + self.lease_s,
                "ts": now,
            }
        )

    # -- outcomes --------------------------------------------------------

    def _settle(self, job_id: Any, event: str, state: str, reason: str, **extra: Any) -> Dict[str, Any]:
        now = float(self._clock())
        job = self.state().get(_job_id(job_id)) or {}
        return self._append(
            {
                "event": event,
                "job_id": _job_id(job_id),
                "state": state,
                "attempt": int(job.get("attempt") or 0),
                "lease_owner": str(job.get("lease_owner") or ""),
                "last_reason": reason,
                "ts": now,
                **extra,
            }
        )

    def succeed(self, job_id: Any, *, detail: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Record success. Terminal: this job is never claimed again."""
        return self._settle(
            job_id, "succeeded", "succeeded", "", detail=dict(detail or {})
        )

    def fail_permanently(
        self, job_id: Any, reason: str, *, kind: str = "failed"
    ) -> Dict[str, Any]:
        """Record a terminal failure. The attempt budget is not extended."""
        return self._settle(job_id, kind, "failed", str(reason or ""))

    def record_failure(self, job_id: Any, exc: BaseException) -> Dict[str, Any]:
        """Classify a failure through the EXISTING authorities and settle.

        The order is the point, and it is borrowed from
        ``runtime.budget_governor``: quota FIRST (an empty account is terminal
        and a retry spends what is left), then the retry classifier, then the
        shared backoff. No schedule, marker set, or quota vocabulary is
        re-implemented here.

        Returns a receipt saying what was decided and why, so a caller that only
        reads the receipt can tell a retry from a give-up without reading the
        journal.
        """
        job_id = _job_id(job_id)
        job = self.state().get(job_id) or {}
        attempt = int(job.get("attempt") or 0)

        failure = classify_provider_failure(exc)
        if failure.kind == "quota_exhausted":
            return {
                "decision": "terminal",
                "reason": "quota exhausted is not a rate limit: retrying an "
                "empty account spends what is left and cannot succeed",
                "quota": failure.as_dict() if hasattr(failure, "as_dict") else {},
                "job_id": job_id,
                "attempt": attempt,
                "row": self._settle(
                    job_id, "failed", "failed", "quota exhausted", kind="quota"
                ),
            }

        decision = classify_retry(exc, idempotent=True)
        verdict = should_retry(
            exc,
            idempotent=True,
            attempts_used=attempt,
            max_retries=self.max_attempts - 1,
            backoff_exponent=0,
        )
        retryable = bool(decision.retryable and getattr(verdict, "retryable", decision.retryable))
        if not retryable or attempt >= self.max_attempts:
            return {
                "decision": "terminal",
                "reason": (
                    "the retry classifier says this is not replayable"
                    if not retryable
                    else f"the attempt budget of {self.max_attempts} is spent"
                ),
                "kind": decision.kind,
                "retryable": retryable,
                "job_id": job_id,
                "attempt": attempt,
                "row": self._settle(
                    job_id, "failed", "failed", f"{decision.kind}: not retried"
                ),
            }

        wait = backoff_seconds(
            attempt + 1, base_s=self.backoff_base_s, cap_s=self.backoff_cap_s or 1e9
        )
        now = float(self._clock())
        return {
            "decision": "retry",
            "reason": f"{decision.kind} is retryable; waiting {wait:.1f}s",
            "kind": decision.kind,
            "retryable": True,
            "provider_fault": bool(decision.provider_fault),
            "wait_s": float(wait),
            "not_before_epoch": now + float(wait),
            "job_id": job_id,
            "attempt": attempt,
            "row": self._append(
                {
                    "event": "retry_scheduled",
                    "job_id": job_id,
                    "state": "retry_wait",
                    "attempt": attempt,
                    "not_before_epoch": now + float(wait),
                    "last_reason": f"{decision.kind}: {decision.reason}",
                    "ts": now,
                }
            ),
        }

    # -- metering --------------------------------------------------------

    def authorize(
        self,
        *,
        messages: Optional[Iterable[Mapping[str, Any]]] = None,
        target: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Authorize the job's next model call, or refuse it.

        A background job with NO governor is refused rather than run unmetered:
        an unmetered background call spends from a budget nothing is watching,
        which is the exact failure this module was added to prevent. A caller
        that genuinely runs free work passes ``messages=None, target=None`` and
        gets the honest "no call to authorize" answer instead.
        """
        if self.governor is None:
            return {
                "ok": False,
                "authorized": False,
                "reason": "no BudgetGovernor is installed, so this job cannot "
                "be metered; running it unmetered would spend from a budget "
                "nothing is watching",
                "metered": False,
            }
        verdict = self.governor.authorize_call(
            messages=messages, target=target
        )
        return {
            "ok": True,
            "authorized": bool(getattr(verdict, "allowed", False)),
            "metered": True,
            "verdict": verdict.as_dict() if hasattr(verdict, "as_dict") else {},
            "reason": str(getattr(verdict, "reason", "") or ""),
        }

    # -- reporting -------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        """Jobs per state. Every key is present, so a caller can render it."""
        out = {state: 0 for state in JOB_STATES}
        for job in self.state().values():
            state = str(job.get("state") or "pending")
            out[state] = out.get(state, 0) + 1
        return out

    def describe(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """One receipt: counts, leases, admission, metering, and idleness.

        ``idle_s`` and ``idle_beyond_grace`` are reported rather than implied. A
        queue that has done nothing for ten minutes and a queue that just started
        both show zero pending jobs, and only one of them is a problem.
        """
        moment = float(self._clock()) if now is None else float(now)
        jobs = self.state()
        latest = max((float(j.get("updated_ts") or 0.0) for j in jobs.values()), default=0.0)
        idle = round(moment - latest, 3) if latest else 0.0
        live_leases = [
            {
                "job_id": j["job_id"],
                "owner": j.get("lease_owner"),
                "expires_in_s": round(
                    float(j.get("lease_expires_epoch") or 0.0) - moment, 3
                ),
            }
            for j in jobs.values()
            if j.get("state") == "leased"
        ]
        return {
            "schema": "runtime.background_jobs.describe/1",
            "root": str(self.root),
            "jobs": len(jobs),
            "counts": self.counts(),
            "live_leases": live_leases,
            "lease_s": self.lease_s,
            "max_attempts": self.max_attempts,
            "foreground_floor_fraction": self.foreground_floor_fraction,
            "metered": self.governor is not None,
            "budget_exhausted": bool(self.governor.exhausted) if self.governor else None,
            "idle_s": idle,
            "idle_grace_s": self.idle_grace_s,
            "idle_beyond_grace": bool(idle > self.idle_grace_s) if latest else False,
            # The idle POLICY, as opposed to the idle STATISTIC above. ``describe``
            # reports how long the queue has been quiet; this says whether
            # background work is permitted right now, which is the enforced
            # question. Both are here because they are different facts: a queue
            # can be quiet for an hour and the repository can be busy, and only
            # one of those says anything about a fan.
            "idle_policy": {
                "grace_s": self.idle_grace_s,
                "refusals": list(IDLE_REFUSALS),
                "evaluated_by": "runtime.background_jobs.claim("
                "repo_last_activity_epoch=..., override_idle=...)",
                "enforced": True,
                "admission": idle_admission(
                    last_activity_epoch=None, idle_grace_s=self.idle_grace_s, clock=self._clock
                ),
                "note": "no repository activity was supplied to describe(), so the "
                "policy reads as not_evaluated here; a claim that supplies it gets "
                "the real decision",
            },
            "journal_bytes": self.journal_path.stat().st_size
            if self.journal_path.is_file()
            else 0,
        }

    def write_receipt(self) -> Path:
        """Publish :meth:`describe` beside the journal. Never raises."""
        receipt = self.describe()
        try:
            atomic_write_json(self.receipt_path, receipt)
        except Exception as exc:  # observability must not fail a run
            receipt["receipt_error"] = f"{type(exc).__name__}: {exc}"
        return self.receipt_path
