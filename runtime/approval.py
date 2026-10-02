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

APPROVAL INTEGRITY. The ceiling invariant is that the command/effect shown
for approval equals the effect actually executed. Three pieces enforce it
here, all built on shared.approval (one implementation, not a local copy):

1. The request carries a CANONICAL EFFECT — the argv-shaped effect record
   plus its rendered command line — derived from the same values the worker
   will act on, so what the approver reads cannot be a hand-written
   approximation of what runs.
2. The approval is bound to a DIGEST of that canonical effect. The historical
   ``fingerprint`` (task + canonical repo + diff + issue) is preserved
   unchanged for cross-version re-entry; ``effect_digest`` is the new,
   effect-shaped binding and both must match before a decision is honored.
3. The effect is RECONSTRUCTED AND RE-CHECKED immediately before the gate
   returns "approve" (``recheck_effect``). A material change between the
   request and the decision — a rewritten diff, a different repo, a mutated
   command — invalidates the stored decision and the caller must re-approve.

Assumes at most one active request per task at a time, one approver.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from shared.approval import (
    canonical_effect,
    effect_digest,
    verify_before_execution,
)

from .fsutil import (
    append_jsonl,
    atomic_write_json,
    now_iso,
    read_json_or_none,
)

DECISION_POLL_INTERVAL_S = 0.2


class ApprovalTimeout(Exception):
    """The approval gate waited past approval_timeout_s with no decision."""


class ApprovalRejected(Exception):
    """The human rejected the proposed diff. Carries the request summary."""


class ApprovalStale(Exception):
    """The approved effect changed before execution; re-approval is required."""


def _canonical_repo(repo_path: str) -> str:
    value = str(repo_path or "")
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(value))


def _fingerprint(task_id: str, repo_path: str, diff: str, issue_text: str) -> str:
    payload = "\0".join(
        (str(task_id), _canonical_repo(repo_path), str(diff), str(issue_text))
    )
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def _effect(
    task_id: str,
    repo_path: str,
    diff: str,
    issue_text: str,
    command: str = "",
):
    """Return the canonical effect for one approval request.

    The ``diff`` travels as a single argv element (it is data the gate
    applies, not a command line), and ``command`` — when the caller supplies
    one — is the exact argv the executor will run. Everything the executor
    would treat as material is inside the returned record, so its digest
    changes if any of it changes.
    """
    argv = [str(command)] if command else ["apply-diff", str(diff)]
    return canonical_effect(
        "approval",
        argv,
        working_directory=_canonical_repo(repo_path),
        target=str(diff),
        side_effect_class="workspace_write",
    )


def _request_payload(
    task_id: str,
    repo_path: str,
    diff: str,
    issue_text: str,
    summary: Optional[str],
    command: str = "",
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    fingerprint = _fingerprint(task_id, repo_path, diff, issue_text)
    effect = _effect(task_id, repo_path, diff, issue_text, command)
    payload: Dict[str, Any] = {
        "request_id": uuid.uuid4().hex,
        "fingerprint": fingerprint,
        "effect_digest": effect.digest,
        "effect": effect.as_dict(),
        "task_id": task_id,
        "repo_path": repo_path,
        "ts": now_iso(),
        "issue_text": issue_text,
        "diff": diff,
        "summary": summary,
    }
    # The gate's OWN deadline travels with the request. Without it an
    # approving surface (TUI modal, REPL prompt) has no way to bound its own
    # wait, so a request the worker has already given up on stays on screen
    # asking a question nobody can still answer. The key is omitted entirely
    # when no deadline was configured, so an unbounded request stays
    # byte-identical to what it was before.
    if timeout_s is not None:
        payload["timeout_s"] = float(timeout_s)
    return payload


def recheck_effect(gate_dir: str) -> str:
    """Re-derive the stored request's effect digest and compare it.

    Returns the digest when it still matches the stored request, and raises
    :class:`ApprovalStale` when it does not. Called by the gate immediately
    before it honors a decision, so an approval cannot be replayed onto a
    materially different effect. A missing or unreadable request is stale by
    definition: there is nothing to re-check against.
    """
    request = read_json_or_none(Path(gate_dir) / "request.json")
    if not isinstance(request, dict):
        raise ApprovalStale("approval request is missing or unreadable")
    stored_effect = request.get("effect")
    stored_digest = str(request.get("effect_digest", "") or "")
    if not isinstance(stored_effect, dict) or not stored_digest:
        # A legacy request without a canonical effect cannot be integrity
        # checked, so it must not be honored as if it had been.
        raise ApprovalStale("approval request carries no canonical effect binding")
    actual = effect_digest(stored_effect)
    if actual != stored_digest:
        raise ApprovalStale(
            "approval request effect was mutated after it was written "
            f"(expected={stored_digest[:12]} actual={actual[:12]})"
        )
    return stored_digest


def request_approval(
    gate_dir: str,
    task_id: str,
    diff: str,
    issue_text: str,
    summary: Optional[str] = None,
    timeout_s: Optional[float] = None,
    poll_interval_s: float = DECISION_POLL_INTERVAL_S,
    _clock=time.sleep,
    repo_path: str = "",
    command: str = "",
) -> str:
    """Block until a human approves/rejects the proposed diff; returns
    "approve" (or raises ApprovalRejected / ApprovalTimeout / ApprovalStale).

    Assumes: gate_dir is a directory owned by this task's worker process;
    an external approver (test, CLI, human driving `runtime.approval`) is
    free to write decision.json at any time. Re-entrant after a crash only
    when the task, repository, issue, exact diff, and canonical effect
    fingerprint the stored request. A mismatched or legacy request is rotated
    and its old decision is invalidated. The stored effect is re-checked
    immediately before "approve" is returned, so a material change between
    request and decision raises ApprovalStale instead of executing.
    """
    d = Path(gate_dir)
    d.mkdir(parents=True, exist_ok=True)
    request_path = d / "request.json"
    decision_path = d / "decision.json"
    review_log = d / "review.log"

    fingerprint = _fingerprint(task_id, repo_path, diff, issue_text)
    effect = _effect(task_id, repo_path, diff, issue_text, command)
    existing_request = read_json_or_none(request_path)
    same_request = (
        isinstance(existing_request, dict)
        and existing_request.get("task_id") == task_id
        and existing_request.get("fingerprint") == fingerprint
        and existing_request.get("effect_digest") == effect.digest
        and bool(existing_request.get("request_id"))
    )
    if same_request:
        request_id = str(existing_request["request_id"])
    else:
        payload = _request_payload(
            task_id, repo_path, diff, issue_text, summary, command, timeout_s
        )
        request_id = str(payload["request_id"])
        atomic_write_json(request_path, payload)
        try:
            decision_path.unlink(missing_ok=True)
        except OSError:
            pass
        event = "request_rotated" if existing_request is not None else "requested"
        append_jsonl(
            review_log,
            {
                "ts": now_iso(),
                "event": event,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "effect_digest": effect.digest,
            },
        )

    deadline = None if timeout_s is None else time.time() + timeout_s
    while True:
        decision = read_json_or_none(decision_path)
        if (
            isinstance(decision, dict)
            and decision.get("request_id") == request_id
            and decision.get("fingerprint") == fingerprint
        ):
            verdict = str(decision.get("decision", "")).strip().lower()
            if verdict == "approve":
                # Re-derive the canonical effect from the stored request and
                # re-check it against the decision's own binding immediately
                # before execution. Any material change is a refusal, not a
                # warning, and every refusal is auditable.
                try:
                    stored_digest = recheck_effect(gate_dir)
                except ApprovalStale as stale:
                    append_jsonl(
                        review_log,
                        {
                            "ts": now_iso(),
                            "event": "stale_refused",
                            "request_id": request_id,
                            "reason": str(stale),
                        },
                    )
                    raise
                check = verify_before_execution(
                    {
                        "effect_digest": stored_digest,
                        "decision": "approved",
                        "expires_at": None,
                    },
                    effect,
                )
                if not check.ok:
                    append_jsonl(
                        review_log,
                        {
                            "ts": now_iso(),
                            "event": "stale_refused",
                            "request_id": request_id,
                            "reason": check.reason,
                            "expected_digest": check.expected_digest,
                            "actual_digest": check.actual_digest,
                        },
                    )
                    raise ApprovalStale(
                        f"{check.reason} "
                        f"(expected={check.expected_digest[:12]} "
                        f"actual={check.actual_digest[:12]})"
                    )
                append_jsonl(
                    review_log,
                    {
                        "ts": now_iso(),
                        "event": "approved",
                        "effect_digest": stored_digest,
                    },
                )
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
    request = pending_request(gate_dir) or {}
    payload = {
        "decision": "approve" if approve else "reject",
        "ts": now_iso(),
        "request_id": request.get("request_id", ""),
        "fingerprint": request.get("fingerprint", ""),
    }
    atomic_write_json(d / "decision.json", payload)


def pending_request(gate_dir: str) -> Optional[Dict[str, Any]]:
    """Return the pending approval request (request.json content) if any."""
    data = read_json_or_none(Path(gate_dir) / "request.json")
    return data if isinstance(data, dict) else None
