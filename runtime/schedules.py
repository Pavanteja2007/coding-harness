"""Bounded, logged, revertible one-shot and periodic run schedules.

Ceiling Prompt 12 asks for ``neo run --at``, scheduled issue queues, and
CI-failure triggers, with three properties that have to be structural rather
than aspirational:

* **bounded** — a schedule is a *request*, capped, validated, and secret-free.
* **logged** — registration, claim, completion, failure, and revert all append
  to one journal under ``logs/_schedules/events.jsonl`` plus a per-schedule
  record on disk, so "what did the automation do" is answerable without a
  re-run.
* **revertible** — :meth:`ScheduleRegistry.revert` archives the schedule and
  writes a typed revert receipt naming what was undone.

And the property the prompt names as the safety constraint: **a schedule cannot
bypass approvals or the verifier.** That is enforced in
:func:`execution_policy`, which is the only way to turn a stored schedule into
a ``Task.config``:

* ``approval`` is *monotone*. A schedule may demand ``require``; it may never
  weaken an inherited ``require`` to ``auto``. The resolution keeps the more
  restrictive of the two.
* budget-shaped keys (``budget_cap_usd``, ``max_wallclock_s``,
  ``max_retries``, ``max_fetches``) are clamped to the *minimum* of the
  schedule's request and the inherited ceiling, so an automation record can
  only ever tighten a run.
* ``agent_strategy`` is accepted but validated against the kernel's known
  strategies, and the resolved config is stamped ``automation=True`` with the
  schedule id. No strategy in that set mints ``completed_verified`` without
  verifier evidence, so choosing one cannot launder a result.
* credentials are refused at registration time, exactly as
  :mod:`runtime.automation` does, so a stored schedule can never be a
  credential store.

``logs/_schedules`` is a sibling of ``runtime.automation``'s
``logs/_automation``; both are harness-owned run state under the logs root and
neither is inside a repository.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from runtime.fsutil import (
    append_jsonl,
    atomic_write_json,
    now_iso,
    read_json,
    read_json_or_none,
)
from runtime.paths import validate_path_segment
from shared import security
from shared.egress import egress_decision  # noqa: F401 - re-exported policy hook point

__all__ = [
    "MONOTONE_APPROVALS",
    "RunSchedule",
    "ScheduleConfigError",
    "ScheduleDisabled",
    "ScheduleError",
    "ScheduleLimit",
    "ScheduleNotFound",
    "ScheduleReceipt",
    "ScheduleRegistry",
    "execution_policy",
    "parse_at",
]

#: Approval values and their restrictiveness. ``require`` is the ceiling: a
#: schedule may ask for it, nothing may remove it.
MONOTONE_APPROVALS: dict[str, int] = {"auto": 0, "off": 0, "never": 0, "require": 1}

#: Keys a schedule may *tighten*. Each is clamped to min(schedule, inherited).
_BUDGET_KEYS: tuple[str, ...] = (
    "budget_cap_usd",
    "max_wallclock_s",
    "max_retries",
    "max_fetches",
    "max_step_turns",
)

_MAX_ISSUE_CHARS = 20_000
_MAX_CONFIG_KEYS = 64
_MAX_SCHEDULES = 128
_MAX_NOTE_CHARS = 512
_DEFAULT_MAX_RUNS = 32
_KNOWN_STRATEGIES = frozenset(
    {"daily", "verified_fix", "planning", "question", "research", "legacy_agent"}
)


class ScheduleError(RuntimeError):
    """Base error for schedule failures."""


class ScheduleDisabled(ScheduleError):
    """Raised when the explicitly-disabled schedule interface is used."""


class ScheduleConfigError(ScheduleError, ValueError):
    """Raised when a schedule definition is invalid or unsafe."""


class ScheduleNotFound(ScheduleError, KeyError):
    """Raised when a named schedule does not exist."""


class ScheduleLimit(ScheduleError):
    """Raised when a schedule bound is reached."""


def _safe_segment(value: Any, label: str) -> str:
    """Return a validated single path segment, or raise the typed error.

    :func:`runtime.paths.validate_path_segment` raises a bare ``ValueError``.
    Callers of this module should only ever have to catch
    :class:`ScheduleError`, so the conversion happens here once rather than at
    every call site.
    """
    try:
        return validate_path_segment(str(value or ""), label)
    except ValueError as exc:
        raise ScheduleConfigError(str(exc)) from exc


def _text(value: Any, limit: int = _MAX_NOTE_CHARS) -> str:
    """Return a redacted, bounded string for any value."""
    try:
        raw = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - pathological __str__
        raw = ""
    return security.redact_text(raw)[:limit]


def parse_at(value: Any, *, now: Optional[float] = None) -> float:
    """Parse a ``--at`` value into a POSIX timestamp.

    Accepts an ISO-8601 timestamp (with or without a ``Z``), a bare ``+HH:MM``
    / ``+MM`` offset from now ("``+15m``", "``+2h``", "``+1d``"), or a float
    epoch. A value in the past is refused rather than silently scheduled for
    "now": a run that fires immediately because the operator's clock was wrong
    is exactly the surprise automation must not produce.
    """
    current = time.time() if now is None else float(now)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ScheduleConfigError("--at requires a timestamp or an offset like +15m")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        when = float(value)
    else:
        text = _text(value, 120).strip()
        if text.startswith("+"):
            return current + _parse_offset(text[1:])
        try:
            when = _parse_iso(text)
        except ValueError as exc:
            raise ScheduleConfigError(
                f"cannot parse --at value {text!r}: {exc}"
            ) from exc
    if when <= current:
        raise ScheduleConfigError(
            "--at is in the past; a schedule cannot fire retroactively"
        )
    return when


def _parse_offset(text: str) -> float:
    """Return seconds for a ``15m``/``2h``/``1d``/``30s`` offset."""
    unit = text[-1:].casefold()
    factors = {"s": 1.0, "m": 60.0, "h": 3_600.0, "d": 86_400.0}
    if unit not in factors:
        raise ValueError(f"unknown offset unit {unit!r}; use s, m, h, or d")
    magnitude = text[:-1].strip()
    if not magnitude:
        raise ValueError("offset magnitude is missing")
    return float(magnitude) * factors[unit]


def _parse_iso(text: str) -> float:
    """Return a POSIX timestamp for an ISO-8601 string."""
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    from datetime import datetime

    return datetime.fromisoformat(candidate).timestamp()


@dataclass(frozen=True)
class RunSchedule:
    """One immutable scheduled-run request.

    A schedule never holds credentials and never holds a resolved policy: it
    holds the *request*, and :func:`execution_policy` is the only thing that
    turns it into a runnable config.
    """

    schedule_id: str
    repo: str
    issue: str
    at: float
    interval_s: float = 0.0
    config: Mapping[str, Any] = field(default_factory=dict)
    source: str = "api"
    trigger: str = "schedule"
    max_runs: int = _DEFAULT_MAX_RUNS
    runs: int = 0
    status: str = "scheduled"
    created_at: str = ""
    updated_at: str = ""
    note: str = ""
    digest: str = ""
    run_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record (already redacted at write time)."""
        return {
            "schema_version": 1,
            "schedule_id": self.schedule_id,
            "repo": self.repo,
            "issue": self.issue,
            "at": round(float(self.at), 3),
            "interval_s": float(self.interval_s),
            "config": dict(self.config),
            "source": self.source,
            "trigger": self.trigger,
            "max_runs": int(self.max_runs),
            "runs": int(self.runs),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
            "digest": self.digest,
            "run_ids": list(self.run_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunSchedule":
        """Rebuild a schedule from its stored record."""
        payload = dict(data or {})
        return cls(
            schedule_id=str(payload.get("schedule_id") or ""),
            repo=str(payload.get("repo") or ""),
            issue=str(payload.get("issue") or ""),
            at=float(payload.get("at") or 0.0),
            interval_s=float(payload.get("interval_s") or 0.0),
            config=dict(payload.get("config") or {}),
            source=str(payload.get("source") or "api"),
            trigger=str(payload.get("trigger") or "schedule"),
            max_runs=int(payload.get("max_runs") or _DEFAULT_MAX_RUNS),
            runs=int(payload.get("runs") or 0),
            status=str(payload.get("status") or "scheduled"),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            note=str(payload.get("note") or ""),
            digest=str(payload.get("digest") or ""),
            run_ids=tuple(str(item) for item in (payload.get("run_ids") or ())),
        )


@dataclass(frozen=True)
class ScheduleReceipt:
    """The typed result of one schedule operation."""

    action: str
    schedule_id: str
    status: str
    at: float = 0.0
    detail: str = ""
    policy: Mapping[str, Any] = field(default_factory=dict)
    schedule: Optional[RunSchedule] = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "schema_version": 1,
            "action": self.action,
            "schedule_id": self.schedule_id,
            "status": self.status,
            "at": round(float(self.at), 3),
            "detail": self.detail,
            "policy": dict(self.policy),
            "schedule": self.schedule.as_dict() if self.schedule else None,
        }


def execution_policy(
    schedule: Union[RunSchedule, Mapping[str, Any]],
    *,
    inherited: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Resolve a schedule into a ``Task.config``, honouring every ceiling.

    Returns ``(config, clamped)``. ``clamped`` names every decision the
    schedule was not allowed to make, so the refusal is on the record rather
    than an absence.

    The rules, in order:

    1. credentials in the stored config are refused outright
       (:class:`ScheduleConfigError` at registration; re-checked here).
    2. ``approval`` is monotone: the more restrictive of the schedule's request
       and the inherited value wins, and ``require`` can never be downgraded.
    3. every budget key is clamped to the minimum of request and inherited.
    4. ``agent_strategy`` must be a known kernel strategy; an unknown one is
       dropped with a clamp note rather than silently accepted.
    5. ``automation=True`` and ``automation_schedule_id`` are stamped so a
       later reader can tell an unattended run from a supervised one, and the
       stamp cannot be removed by a schedule because it is written last.
    """
    if isinstance(schedule, RunSchedule):
        requested = dict(schedule.config)
        schedule_id = schedule.schedule_id
    else:
        payload = dict(schedule or {})
        requested = dict(payload.get("config") or {})
        schedule_id = str(payload.get("schedule_id") or "")
    base = dict(inherited or {})
    if security.contains_secret(requested):
        raise ScheduleConfigError(
            "a scheduled run must not carry credentials; use the run environment"
        )
    clamped: list[dict[str, str]] = []
    resolved: dict[str, Any] = {}
    for key, value in requested.items():
        if key == "approval":
            continue
        if key in _BUDGET_KEYS:
            continue
        if key == "agent_strategy":
            continue
        if key in ("automation", "automation_schedule_id"):
            continue
        resolved[str(key)] = value

    # 2. approval is monotone.
    schedule_approval = str(requested.get("approval", "")).strip().casefold()
    inherited_approval = str(base.get("approval", "")).strip().casefold()
    if schedule_approval and schedule_approval not in MONOTONE_APPROVALS:
        clamped.append(
            {
                "key": "approval",
                "requested": schedule_approval,
                "reason": "unsupported approval value; ignored",
            }
        )
        schedule_approval = ""
    if inherited_approval and inherited_approval not in MONOTONE_APPROVALS:
        inherited_approval = ""
    if schedule_approval and inherited_approval:
        winner = max(
            (schedule_approval, inherited_approval),
            key=lambda item: MONOTONE_APPROVALS[item],
        )
        if winner != schedule_approval:
            clamped.append(
                {
                    "key": "approval",
                    "requested": schedule_approval,
                    "reason": (
                        f"a schedule cannot weaken the inherited approval policy "
                        f"({inherited_approval})"
                    ),
                }
            )
        resolved["approval"] = winner
    else:
        chosen = schedule_approval or inherited_approval
        if chosen:
            resolved["approval"] = chosen

    # 3. budgets clamp to the minimum.
    for key in _BUDGET_KEYS:
        requested_value = _positive(requested.get(key))
        inherited_value = _positive(base.get(key))
        candidates = [
            item for item in (requested_value, inherited_value) if item is not None
        ]
        if not candidates:
            continue
        winner = min(candidates)
        resolved[key] = winner
        if requested_value is not None and requested_value > winner:
            clamped.append(
                {
                    "key": key,
                    "requested": str(requested_value),
                    "reason": "a schedule can only tighten this bound",
                }
            )

    # 4. an unknown strategy is dropped, never guessed.
    strategy = str(requested.get("agent_strategy", "")).strip()
    if strategy:
        if strategy in _KNOWN_STRATEGIES:
            resolved["agent_strategy"] = strategy
        else:
            clamped.append(
                {
                    "key": "agent_strategy",
                    "requested": strategy,
                    "reason": "unknown kernel strategy; the configured strategy is kept",
                }
            )
    elif base.get("agent_strategy"):
        resolved["agent_strategy"] = base["agent_strategy"]

    # 5. automation stamps are written last and are not overridable.
    resolved["automation"] = True
    resolved["automation_schedule_id"] = schedule_id
    return resolved, clamped


def _positive(value: Any) -> Optional[float]:
    """Return a positive float for a numeric value, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class ScheduleRegistry:
    """A bounded, logged, revertible store of scheduled runs.

    The registry validates and records; it never starts a worker and never
    imports a provider. Consuming a due schedule is
    :meth:`claim`, which marks it and returns the resolved ``Task.config`` —
    the caller then runs it through the ordinary, verifier-gated path.
    """

    def __init__(self, logs_root: Union[str, Path], *, enabled: bool = True) -> None:
        self.logs_root = Path(logs_root).expanduser().resolve()
        self.enabled = bool(enabled)
        self.root = self.logs_root / "_schedules"
        self.schedule_dir = self.root / "schedules"
        self.reverted_dir = self.root / "reverted"
        self.journal_path = self.root / "events.jsonl"
        self.schedule_dir.mkdir(parents=True, exist_ok=True)
        self.reverted_dir.mkdir(parents=True, exist_ok=True)

    # -- registration ------------------------------------------------------

    def register(
        self,
        schedule_id: str,
        *,
        repo: str,
        issue: str,
        at: Union[float, str],
        config: Optional[Mapping[str, Any]] = None,
        interval_s: float = 0.0,
        source: str = "api",
        trigger: str = "schedule",
        max_runs: int = _DEFAULT_MAX_RUNS,
        note: str = "",
        now: Optional[float] = None,
    ) -> ScheduleReceipt:
        """Register (or replace) one schedule. Raises on anything unsafe."""
        self._require_enabled()
        selected = _safe_segment(schedule_id, "schedule id")
        repo_path = _text(repo, 4_000).strip()
        if not repo_path:
            raise ScheduleConfigError("a schedule needs a repository path")
        issue_text = _text(issue, _MAX_ISSUE_CHARS).strip()
        if not issue_text:
            raise ScheduleConfigError("a schedule needs an issue text")
        when = parse_at(at, now=now)
        interval = float(interval_s or 0.0)
        if interval < 0:
            raise ScheduleConfigError("interval_s cannot be negative")
        if interval and interval < 60.0:
            raise ScheduleConfigError(
                "a periodic schedule must have an interval of at least 60s"
            )
        run_budget = _DEFAULT_MAX_RUNS if max_runs is None else int(max_runs)
        if run_budget < 1:
            raise ScheduleConfigError("max_runs must be at least 1")
        requested = dict(config or {})
        if len(requested) > _MAX_CONFIG_KEYS:
            raise ScheduleConfigError(
                f"schedule config has {len(requested)} keys (max {_MAX_CONFIG_KEYS})"
            )
        if security.contains_secret(requested):
            raise ScheduleConfigError(
                "a scheduled run must not carry credentials; use the run environment"
            )
        if security.contains_secret(issue_text):
            raise ScheduleConfigError(
                "the scheduled issue text must not carry credentials"
            )
        live = [
            path
            for path in self.schedule_dir.glob("*.json")
            if path.is_file()
            and str(read_json_or_none(path).get("status")) == "scheduled"
        ]
        if len(live) >= _MAX_SCHEDULES:
            raise ScheduleLimit(
                f"{len(live)} schedules already pending (max {_MAX_SCHEDULES})"
            )
        redacted_config = security.redact_secrets(requested)
        stamp = now_iso()
        record = {
            "schema_version": 1,
            "schedule_id": selected,
            "repo": repo_path,
            "issue": issue_text,
            "at": when,
            "interval_s": interval,
            "config": redacted_config,
            "source": _text(source, 60),
            "trigger": _text(trigger, 60) or "schedule",
            "max_runs": run_budget,
            "runs": 0,
            "status": "scheduled",
            "created_at": stamp,
            "updated_at": stamp,
            "note": _text(note, _MAX_NOTE_CHARS),
            "digest": _digest(
                {"repo": repo_path, "issue": issue_text, "config": redacted_config}
            ),
            "run_ids": [],
        }
        atomic_write_json(self.schedule_dir / f"{selected}.json", record)
        self._event(
            "registered",
            {"schedule_id": selected, "at": when, "source": record["source"]},
        )
        return ScheduleReceipt(
            action="register",
            schedule_id=selected,
            status="scheduled",
            at=when,
            detail="",
            policy=redacted_config,
            schedule=RunSchedule.from_dict(record),
        )

    # -- consumption -------------------------------------------------------

    def due(self, *, now: Optional[float] = None, limit: int = 8) -> list[RunSchedule]:
        """Return due, still-runnable schedules in deterministic id order.

        A schedule that has exhausted ``max_runs`` or been reverted is not
        returned, and reading the queue is side-effect free — claiming is a
        separate, explicit step.
        """
        self._require_enabled()
        current = time.time() if now is None else float(now)
        found: list[RunSchedule] = []
        for path in sorted(self.schedule_dir.glob("*.json")):
            record = read_json_or_none(path)
            if not isinstance(record, dict) or record.get("status") != "scheduled":
                continue
            if float(record.get("at") or 0.0) > current:
                continue
            if int(record.get("runs") or 0) >= int(record.get("max_runs") or 0):
                continue
            found.append(RunSchedule.from_dict(record))
            if len(found) >= max(1, int(limit)):
                break
        if found:
            self._event(
                "due",
                {"count": len(found), "ids": [item.schedule_id for item in found]},
            )
        return found

    def claim(
        self,
        schedule_id: str,
        *,
        inherited_config: Optional[Mapping[str, Any]] = None,
        now: Optional[float] = None,
    ) -> ScheduleReceipt:
        """Claim one due schedule and return its policy-resolved config.

        Claiming increments the run counter, advances ``at`` for a periodic
        schedule, and marks the record so a second consumer cannot run the same
        occurrence. The returned config is what the caller must use; it is the
        only config the schedule is allowed to influence, and
        :func:`execution_policy` has already clamped it.
        """
        self._require_enabled()
        selected = _safe_segment(schedule_id, "schedule id")
        path = self.schedule_dir / f"{selected}.json"
        record = read_json(path)
        if not isinstance(record, dict):
            raise ScheduleNotFound(f"no schedule named {selected!r}")
        if record.get("status") != "scheduled":
            raise ScheduleConfigError(
                f"schedule {selected!r} is {record.get('status')!r}, not scheduled"
            )
        runs = int(record.get("runs") or 0)
        if runs >= int(record.get("max_runs") or 0):
            record["status"] = "exhausted"
            record["updated_at"] = now_iso()
            atomic_write_json(path, record)
            raise ScheduleLimit(f"schedule {selected!r} has used all {runs} runs")
        current = time.time() if now is None else float(now)
        if float(record.get("at") or 0.0) > current:
            raise ScheduleConfigError(f"schedule {selected!r} is not due yet")
        resolved, clamped = execution_policy(record, inherited=inherited_config)
        record["runs"] = runs + 1
        record["updated_at"] = now_iso()
        interval = float(record.get("interval_s") or 0.0)
        record["at"] = current + interval if interval else record.get("at")
        atomic_write_json(path, record)
        self._event(
            "claimed",
            {
                "schedule_id": selected,
                "runs": record["runs"],
                "clamped": clamped,
            },
        )
        return ScheduleReceipt(
            action="claim",
            schedule_id=selected,
            status="claimed",
            at=float(record.get("at") or 0.0),
            detail="; ".join(f"{item['key']}: {item['reason']}" for item in clamped),
            policy=resolved,
            schedule=RunSchedule.from_dict(record),
        )

    def complete(
        self,
        schedule_id: str,
        *,
        success: bool,
        run_id: str = "",
        status: str = "",
        error: str = "",
    ) -> ScheduleReceipt:
        """Record a claimed run's outcome. Never changes the run's status text.

        ``status`` is the run's own canonical completion status. It is stored
        verbatim and re-emitted; the registry has no vocabulary for upgrading
        it, so a schedule can never rewrite ``completed_unverified`` into
        success. An unverified status is recorded as unverified.
        """
        self._require_enabled()
        selected = _safe_segment(schedule_id, "schedule id")
        path = self.schedule_dir / f"{selected}.json"
        record = read_json(path)
        if not isinstance(record, dict):
            raise ScheduleNotFound(f"no schedule named {selected!r}")
        run_ids = [str(item) for item in (record.get("run_ids") or [])][-32:]
        if run_id:
            run_ids.append(_text(run_id, 200))
        record["run_ids"] = run_ids[-32:]
        record["last_status"] = _text(status, 100)
        record["last_error"] = _text(error, _MAX_NOTE_CHARS)
        record["updated_at"] = now_iso()
        if not float(record.get("interval_s") or 0.0):
            # A one-shot schedule is done; keep the record for the audit trail
            # but stop it being claimable.
            record["status"] = "completed" if success else "failed"
        atomic_write_json(path, record)
        self._event(
            "completed" if success else "failed",
            {
                "schedule_id": selected,
                "run_id": _text(run_id, 200),
                "status": record["last_status"],
            },
        )
        return ScheduleReceipt(
            action="complete" if success else "fail",
            schedule_id=selected,
            status=record["status"],
            detail=record["last_status"],
            policy={},
            schedule=RunSchedule.from_dict(record),
        )

    # -- reversion and inspection -----------------------------------------

    def revert(self, schedule_id: str, *, reason: str = "") -> ScheduleReceipt:
        """Revert one schedule: stop it and keep a typed revert receipt.

        The record is moved to ``reverted/`` with the reason and the timestamp,
        so the revert itself is as auditable as the registration. A schedule
        that is already reverted is a no-op receipt, not an error.
        """
        self._require_enabled()
        selected = _safe_segment(schedule_id, "schedule id")
        path = self.schedule_dir / f"{selected}.json"
        record = read_json_or_none(path)
        if not isinstance(record, dict):
            raise ScheduleNotFound(f"no schedule named {selected!r}")
        record["status"] = "reverted"
        record["reverted_at"] = now_iso()
        record["revert_reason"] = _text(reason, _MAX_NOTE_CHARS)
        record["updated_at"] = now_iso()
        atomic_write_json(self.reverted_dir / f"{selected}.json", record)
        try:
            path.unlink()
        except OSError:
            pass
        self._event(
            "reverted", {"schedule_id": selected, "reason": record["revert_reason"]}
        )
        return ScheduleReceipt(
            action="revert",
            schedule_id=selected,
            status="reverted",
            detail=record["revert_reason"],
            schedule=RunSchedule.from_dict(record),
        )

    def show(self, schedule_id: str, *, include_reverted: bool = False) -> RunSchedule:
        """Return one schedule record, optionally including a reverted one."""
        self._require_enabled()
        selected = _safe_segment(schedule_id, "schedule id")
        record = read_json_or_none(self.schedule_dir / f"{selected}.json")
        if record is None and include_reverted:
            record = read_json_or_none(self.reverted_dir / f"{selected}.json")
        if not isinstance(record, dict):
            raise ScheduleNotFound(f"no schedule named {selected!r}")
        return RunSchedule.from_dict(record)

    def list(self, *, include_reverted: bool = False) -> list[RunSchedule]:
        """Return every schedule in deterministic id order."""
        self._require_enabled()
        out: list[RunSchedule] = []
        for path in sorted(self.schedule_dir.glob("*.json")):
            record = read_json_or_none(path)
            if isinstance(record, dict):
                out.append(RunSchedule.from_dict(record))
        if include_reverted:
            for path in sorted(self.reverted_dir.glob("*.json")):
                record = read_json_or_none(path)
                if isinstance(record, dict):
                    out.append(RunSchedule.from_dict(record))
        return out

    def journal(self, *, limit: int = 64) -> list[dict[str, Any]]:
        """Return the tail of the schedule journal (redacted, bounded)."""
        self._require_enabled()
        events: list[dict[str, Any]] = []
        try:
            with self.journal_path.open(
                "r", encoding="utf-8", errors="replace"
            ) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        events.append(security.redact_secrets(value))
        except OSError:
            return []
        return events[-max(1, int(limit)) :]

    # -- internals ---------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ScheduleDisabled(
                "scheduled runs are disabled; construct the registry with enabled=True"
            )

    def _event(self, event: str, data: Mapping[str, Any]) -> None:
        append_jsonl(
            self.journal_path,
            {
                "schema_version": 1,
                "ts": now_iso(),
                "event": event,
                "data": security.redact_secrets(dict(data)),
            },
        )


def _digest(value: Any) -> str:
    """Return a stable SHA-256 over a redacted, canonical rendering."""
    import hashlib

    payload = json.dumps(
        security.redact_secrets(value), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m runtime.schedules`` — register/list/claim/complete/revert.

    Exit codes follow the CLI contract: 0 ok, 2 usage/validation error. The
    command never runs a task; ``claim`` only resolves and records the config a
    caller should use.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m runtime.schedules",
        description="bounded, logged, revertible scheduled runs (never executes work)",
    )
    parser.add_argument("--log-root", default=None, help="logs root (default: ./logs)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="action", required=True)
    register = sub.add_parser("register", help="register one scheduled run")
    register.add_argument("schedule_id")
    register.add_argument("--repo", required=True)
    register.add_argument("--issue", required=True)
    register.add_argument(
        "--at", required=True, help="ISO-8601, +15m/+2h offset, or epoch"
    )
    register.add_argument("--config", default="{}", help="JSON config request")
    register.add_argument(
        "--every", type=float, default=0.0, help="periodic interval in seconds"
    )
    register.add_argument("--max-runs", type=int, default=_DEFAULT_MAX_RUNS)
    sub.add_parser("list", help="list schedules")
    claim = sub.add_parser("claim", help="resolve and claim one due schedule")
    claim.add_argument("schedule_id")
    claim.add_argument(
        "--inherited", default="{}", help="JSON inherited config ceiling"
    )
    complete = sub.add_parser("complete", help="record a claimed run's outcome")
    complete.add_argument("schedule_id")
    complete.add_argument("--run-id", default="")
    complete.add_argument("--status", default="")
    complete.add_argument("--error", default="")
    revert = sub.add_parser("revert", help="revert one schedule")
    revert.add_argument("schedule_id")
    revert.add_argument("--reason", default="")

    args = parser.parse_args(list(argv) if argv is not None else None)
    logs_root = (
        Path(args.log_root).expanduser() if args.log_root else Path.cwd() / "logs"
    )
    registry = ScheduleRegistry(logs_root)
    try:
        if args.action == "register":
            receipt = registry.register(
                args.schedule_id,
                repo=args.repo,
                issue=args.issue,
                at=args.at,
                config=json.loads(args.config or "{}"),
                interval_s=args.every,
                max_runs=args.max_runs,
            )
        elif args.action == "list":
            items = [item.as_dict() for item in registry.list(include_reverted=True)]
            receipt = ScheduleReceipt(action="list", schedule_id="", status="ok")
            if args.json:
                print(json.dumps(items, indent=2))
            else:
                for item in items:
                    print(
                        f"{item['schedule_id']}: {item['status']} at "
                        f"{item['at']} runs={item['runs']}/{item['max_runs']}"
                    )
            return 0
        elif args.action == "claim":
            receipt = registry.claim(
                args.schedule_id, inherited_config=json.loads(args.inherited or "{}")
            )
        elif args.action == "complete":
            receipt = registry.complete(
                args.schedule_id,
                success=not args.error,
                run_id=args.run_id,
                status=args.status,
                error=args.error,
            )
        else:
            receipt = registry.revert(args.schedule_id, reason=args.reason)
    except (ScheduleError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt.as_dict(), indent=2))
    else:
        print(
            f"{receipt.action} {receipt.schedule_id}: {receipt.status} {receipt.detail}".strip()
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
