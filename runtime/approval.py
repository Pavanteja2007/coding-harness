"""Human-in-the-loop approval gate (cross-process, file-based).

The scheduler's worker processes can't prompt a human directly, so the
approval pause is a small file protocol. The worker:
  1. writes a request file describing the proposed fix (diff, issue, task)
  2. blocks, polling for a decision file (worker is restart-safe: on resume
     it re-checks the existing request file first)
  3. on APPROVE: proceeds to the git-output step; on REJECT: ends the task
     as rejected (never applies the diff); on timeout: ends as failed.

Files, under logs/{task_id}/approval/:
  request.json   — what the human is approving (written by the worker)
  decision.json  — {"decision": "approve"|"reject"} (written by the approver)
  review.log     — append-only audit trail of the protocol's events

Assumes at most one active request per task at a time, one approver.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .fsutil import append_jsonl, atomic_write_json, now_iso, read_json, read_json_or_none

DECISION_POLL_INTERVAL_S = 0.2


class ApprovalTimeout(Exception):
    """The approval gate waited past approval_timeout_s with no decision."""


class ApprovalRejected(Exception):
    """The human rejected the proposed diff. Carries the request summary."""


def request_approval(
    gate_dir: str,
    task_id: str,
    diff: str,
    issue_text: str,
    summary: Optional[str] = None,
    timeout_s: Optional[float] = None,
    poll_interval_s: float = DECISION_POLL_INTERVAL_S,
    _clock=time.sleep,
) -> str:
    """Block until a human approves/rejects the proposed diff; returns
    "approve" (or raises ApprovalRejected / ApprovalTimeout).

    Assumes: gate_dir is a directory owned by this task's worker process;
    an external approver (test, CLI, human driving `runtime.approval`) is
    free to write decision.json at any time. Re-entrant after a crash:
    if request.json already exists it is kept (not rewritten), so a
    resumed task's approval state survives restarts.
    """
    d = Path(gate_dir)
    d.mkdir(parents=True, exist_ok=True)
    request_path = d / "request.json"
    decision_path = d / "decision.json"
    review_log = d / "review.log"

    if not request_path.exists():
        payload: Dict[str, Any] = {
            "task_id": task_id,
            "ts": now_iso(),
            "issue_text": issue_text,
            "diff": diff,
            "summary": summary,
        }
        atomic_write_json(request_path, payload)
        append_jsonl(review_log, {"ts": now_iso(), "event": "requested"})

    deadline = None if timeout_s is None else time.time() + timeout_s
    while True:
        decision = read_json_or_none(decision_path)
        if isinstance(decision, dict):
            verdict = str(decision.get("decision", "")).strip().lower()
            if verdict == "approve":
                append_jsonl(review_log, {"ts": now_iso(), "event": "approved"})
                return "approve"
            if verdict == "reject":
                append_jsonl(review_log, {"ts": now_iso(), "event": "rejected"})
                raise ApprovalRejected(
                    f"human rejected proposed diff for task {task_id}"
                )
        if deadline is not None and time.time() > deadline:
            append_jsonl(review_log, {"ts": now_iso(), "event": "timeout"})
            raise ApprovalTimeout(
                f"no approval decision within {timeout_s}s for task {task_id}"
            )
        _clock(poll_interval_s)


def decide(gate_dir: str, approve: bool) -> None:
    """External-side helper: write the decision file for a pending request.

    Assumes a request.json exists in gate_dir (the worker writes it before
    blocking); overwriting a previous decision is allowed while the worker
    is still waiting.
    """
    d = Path(gate_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"decision": "approve" if approve else "reject", "ts": now_iso()}
    atomic_write_json(d / "decision.json", payload)


def pending_request(gate_dir: str) -> Optional[Dict[str, Any]]:
    """Return the pending approval request (request.json content) if any."""
    data = read_json_or_none(Path(gate_dir) / "request.json")
    return data if isinstance(data, dict) else None
