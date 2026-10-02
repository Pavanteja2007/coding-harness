"""Live run views — the structured companions to the free-form trace feed.

This module is a PURE read-only layer over data that already exists on
disk (same discipline as cli.tracelog, Task E of the live-trace round):
it folds the SAME trace.jsonl events the harness writes into

- a live TODO CHECKLIST (the plan + step_end/attempt events — the
  harness's own task decomposition, never a parallel tracking system),
- the STATE-MACHINE state (harness/state_machine.py's transitions.jsonl
  audit trail, read with its documented "last valid to-state" contract),
- the COMPLETION CARD facts (status/attempts/cost/model calls/elapsed
  from the trace's own result + final_verify + git_output events, files
  from state.json — the run's own records, re-derived at render time so
  the card can never drift from what actually happened).

It writes NOTHING. The TUI renders it; every public function is total
(malformed events / missing files degrade to honest empties, never
raise — a view layer must not take a run down).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cli import ui

__all__ = [
    "ACTIVE",
    "DONE",
    "EVENT_VOCABULARY",
    "FAILED",
    "HONESTY_SURFACES",
    "MODE_PROJECTIONS",
    "PENDING",
    "RUN_VERDICTS",
    "SKIPPED",
    "VERDICT_LABELS",
    "EventCursor",
    "RunProjection",
    "TodoModel",
    "TodoStep",
    "briefing_facts",
    "briefing_lines",
    "card_lines",
    "context_meter_line",
    "cost_reconciliation",
    "effective_terminal_status",
    "failure_lines",
    "fmt_elapsed",
    "headless_status",
    "honest_row_status",
    "model_call_receipts",
    "normalize_diagnostic",
    "normalize_projection_mode",
    "pending_decision",
    "read_agent_projection",
    "read_checkpoints",
    "read_context_meter",
    "read_context_receipt",
    "read_diagnostics",
    "read_live_projection",
    "read_machine_state",
    "read_rewind_targets",
    "read_run_facts",
    "read_task_progress",
    "run_verdict",
    "run_verdict_label",
    "status_is_completed",
    "status_is_success",
    "status_is_verified",
    "status_label",
    "status_lines",
    "undo_receipt",
    "verdict_is_success",
    "verification_state",
]

# ---------------------------------------------------------------------------
# The todo checklist — plan + step events, folded live (Task A)
# ---------------------------------------------------------------------------

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

#: ACTIVE is not a stored state — TodoModel.active_id marks the step
#: whose session is currently in flight (from model_request {step-N}).
ACTIVE = "active"


def event_parts(event: Any) -> Tuple[str, Dict[str, Any], float, Dict[str, Any]]:
    """Return ``(kind, payload, timestamp, identity)`` for journal rows.

    Both the normalized Boundary-0 fields and the historical trace aliases
    are accepted. The function is deliberately permissive so a UI reader
    can remain useful while a producer is upgrading; malformed rows yield
    an empty event instead of raising.
    """
    if not isinstance(event, Mapping):
        return "", {}, 0.0, {}
    kind = str(event.get("event") or event.get("event_type") or event.get("kind") or "")
    raw_payload = event.get("payload")
    if not isinstance(raw_payload, Mapping):
        raw_payload = event.get("data")
    if isinstance(raw_payload, Mapping):
        payload = dict(raw_payload)
    else:
        ignored = {
            "event",
            "event_type",
            "kind",
            "payload",
            "data",
            "ts",
            "timestamp",
            "sequence",
            "schema_version",
            "session_id",
            "run_id",
            "turn_id",
        }
        payload = {
            str(key): value for key, value in event.items() if key not in ignored
        }
    try:
        timestamp = float(event.get("timestamp", event.get("ts", 0.0)) or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
    identity = {
        "sequence": event.get("sequence", 0),
        "session_id": str(event.get("session_id") or ""),
        "run_id": str(event.get("run_id") or ""),
        "turn_id": str(event.get("turn_id") or ""),
    }
    return kind, payload, timestamp, identity


def event_kind(event: Any) -> str:
    """Return a normalized event discriminator without raising."""
    return event_parts(event)[0]


EVENT_SCHEMA_VERSION = 1


MODE_PROJECTIONS: Dict[str, Dict[str, Any]] = {
    "question": {
        "label": "Question",
        "tools": ("read", "glob", "grep", "memory", "finish"),
        "verification": "not_applicable",
    },
    "plan": {
        "label": "Plan",
        "tools": ("read", "glob", "grep", "memory", "plan", "finish"),
        "verification": "not_run",
    },
    "daily": {
        "label": "Daily coding",
        "tools": ("read", "glob", "grep", "edit", "write", "bash", "verify", "finish"),
        "verification": "optional",
    },
    "verified_fix": {
        "label": "Verified fix",
        "tools": ("read", "glob", "grep", "edit", "write", "bash", "verify", "finish"),
        "verification": "required",
    },
    "build": {
        "label": "Build / project",
        "tools": ("read", "glob", "grep", "edit", "write", "bash", "verify", "finish"),
        "verification": "required",
    },
    "connector": {
        "label": "Connector / MCP",
        "tools": ("read", "glob", "grep", "mcp", "memory", "finish"),
        "verification": "not_applicable",
    },
}


def normalize_projection_mode(value: Any) -> str:
    """Return one of the six stable live-run projection modes.

    Compatibility mode names are accepted, but the projection keeps its
    own vocabulary so a question cannot be mistaken for a verified fix
    and a connector call cannot be mistaken for a workspace edit.
    """
    key = str(value or "").strip().lower()
    aliases = {
        "ask": "question",
        "question": "question",
        "explore": "question",
        "research": "question",
        "planning": "plan",
        "plan": "plan",
        "daily": "daily",
        "daily_coding": "daily",
        "agent": "daily",
        "agent_task": "daily",
        "build": "build",
        "project": "build",
        "implementation": "build",
        "fix": "verified_fix",
        "verified_fix": "verified_fix",
        "connector": "connector",
        "mcp": "connector",
    }
    return aliases.get(key, "daily")


#: The terminal's stable visual language: every run event a surface is
#: expected to be able to render, mapped to the journal event names that
#: carry it. Declaring it here - rather than letting each renderer
#: re-derive it - is what makes "the TUI is a projection of structured run
#: events" checkable: a producer that renames an event, or a surface that
#: cannot render one of these, is a loud test failure instead of a silently
#: missing row in the transcript.
#:
#: Values are the event names accepted for that visual kind. The legacy
#: ``harness.core`` fix-loop names and the canonical agent-kernel names are
#: both listed, because a session can replay either journal (and a resumed
#: run can contain both).
EVENT_VOCABULARY: Dict[str, Tuple[str, ...]] = {
    "task_start": ("run_started", "run_start", "task_start", "project_start"),
    "phase_change": ("phase_changed", "phase_change", "state_change"),
    "model_request": ("model_request", "model_started", "turn_started"),
    "model_response": ("model_response", "model_completed", "run_receipt"),
    "reasoning_summary": ("reasoning_summary", "reasoning", "model_delta"),
    "tool_call": ("tool_call", "batch_call"),
    "tool_result": ("tool_result", "tool_completed", "tool_result_detail"),
    "file_read": ("file_read", "read_file", "qa_read", "recall", "docs_lookup"),
    "file_search": ("file_search", "search_files", "read_symbol"),
    "patch_edit": ("edit_applied", "file_changed", "web_fetch"),
    "approval_request": (
        "approval_required",
        "input_requested",
        "permission_decision",
    ),
    "command_output": ("command_output", "process_output", "sandbox_result"),
    "verification": (
        "verify",
        "verification",
        "final_verify",
        "project_final_verify",
    ),
    "checkpoint": (
        "checkpoint_saved",
        "checkpoint",
        "project_checkpoint",
        "checkpoint_warning",
    ),
    "subagent_start": (
        "subagent_started",
        "subagent_start",
        "child_started",
        "spawn",
        "project_sub_task_start",
    ),
    "subagent_end": (
        "subagent_finished",
        "subagent_end",
        "child_finished",
        "spawn_error",
        "project_sub_task_end",
        "project_sub_task_already_passing",
    ),
    "error_retry": (
        "error",
        "run_error",
        "worker_error",
        "child_error",
        "retry",
        "model_retry",
        "attempt_retry",
        "crash_retry",
        "model_recovery",
        "hang_timeout",
        "kill_requeue",
        "kill_exhausted",
        "approval_error",
        "wallclock_timeout",
    ),
    "task_completion": (
        "result",
        "run_finished",
        "run_finish",
        "task_end",
        "project_end",
        "completion_decision",
        "stop",
        "finish",
    ),
}

#: Journal events that carry no user-visible state change of their own.
#: They are consumed and counted, but they must not be reported as
#: UNMAPPED: a run whose progress receipts the UI cannot classify is a
#: different (and much more serious) finding than a run whose vocabulary
#: has genuinely grown.
INFORMATIONAL_EVENTS: frozenset = frozenset(
    {
        "context_built",
        "context_compiled",
        "context_compaction_restored",
        "context_compaction_warning",
        "context_rewind",
        "context_rewind_warning",
        "context_warning",
        "execution_backend_ready",
        "knowledge_bound",
        "knowledge_close",
        "knowledge_close_failed",
        "turn_recorded",
        "turn_ledger_warning",
        "project_criteria_extracted",
        "project_plan_generated",
        "project_plan_saved",
        "session_context",
        "skills",
        "skill_model_content",
        "decision_memory",
        "retrieval",
        "retrieval_truncated",
        "mcp_catalog",
    }
)


def _evidence_bool(value: Any) -> bool:
    """Return a conservative boolean interpretation of verifier evidence."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed", "ok"}
    return False


def _cost_is_priced(usage: Any, cost: float) -> bool:
    """Return whether one usage receipt actually priced its call.

    The rule is deliberately the tree's existing one (`model_call_receipts`
    uses the identical test against the router ledger): a receipt is priced
    when it carries a positive cost OR names where the cost came from. A
    receipt that says ``cost_usd: 0.0`` and nothing else means "no price
    table matched this model", which is UNKNOWN, not free.
    """
    if not isinstance(usage, Mapping):
        return cost > 0.0
    if usage.get("priced") is True:
        return True
    if usage.get("priced") is False:
        return False
    if usage.get("price_known") is False:
        return False
    if usage.get("cost_source"):
        return True
    return cost > 0.0


def _clean_verification(item: Any) -> bool:
    """Return whether one evidence record proves target and suite health."""
    if not isinstance(item, Mapping):
        return False
    target = item.get("target_passed", item.get("target_test_passed"))
    regression = item.get("regression_passed")
    return (
        _evidence_bool(target)
        and _evidence_bool(regression)
        and not _evidence_bool(item.get("flaky"))
        and not bool(item.get("error"))
    )


def effective_terminal_status(status: Any, evidence: Any) -> str:
    """Normalize a terminal status without allowing unverified success.

    Legacy ``success`` rows are accepted as completion labels, but they
    are displayed as ``completed_unverified`` unless a clean verifier
    record is present. This keeps the TUI from upgrading a model's
    finish claim into a verified result.
    """
    normalized = terminal_status(status) if status not in (None, "") else "unknown"
    records = (
        [evidence]
        if isinstance(evidence, Mapping)
        else [item for item in (evidence or []) if isinstance(item, Mapping)]
    )
    if normalized == "completed_verified" and (
        not records or not _clean_verification(records[-1])
    ):
        return "completed_unverified"
    return normalized


def verification_state(evidence: Any, status: Any = "") -> str:
    """Return a truthful verification state for a live or terminal view."""
    records = (
        [evidence]
        if isinstance(evidence, Mapping)
        else [item for item in (evidence or []) if isinstance(item, Mapping)]
    )
    if not records:
        normalized = terminal_status(status) if status else ""
        if normalized in {"completed_verified", "success", "completed"}:
            return "unknown"
        return "not_run"
    latest = records[-1]
    if _clean_verification(latest):
        return "verified"
    if _evidence_bool(latest.get("flaky")):
        return "flaky"
    target = latest.get("target_passed", latest.get("target_test_passed"))
    if target is not None and not _evidence_bool(target):
        return "failed"
    regression = latest.get("regression_passed")
    if regression is not None and not _evidence_bool(regression):
        return "failed"
    if latest.get("error"):
        return "error"
    return "unknown"


class EventCursor:
    """Validate and order one run journal stream for a UI consumer.

    Canonical rows are accepted only in contiguous sequence order. Future
    rows are held until the missing row arrives, duplicate rows are
    idempotent, and identity/schema violations become explicit warnings.
    Legacy rows without a sequence remain arrival-ordered for compatibility;
    rows carrying a stable call/request id are still deduplicated.
    """

    def __init__(self, task_id: str = "") -> None:
        self.task_id = str(task_id or "")
        self.cursor = 0
        self.session_id = ""
        self.run_id = ""
        self.turn_id = ""
        self.pending: Dict[int, Dict[str, Any]] = {}
        self.seen_sequences: set[int] = set()
        self.legacy_keys: set[str] = set()
        self.warnings: List[str] = []
        self.warning_revision = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.reconnect_count = 0
        self.connected = True
        self.last_sequence = 0
        self.raw_events = 0

    def _warn(self, message: str) -> None:
        text = str(message)
        if text not in self.warnings:
            self.warnings.append(text)
            self.warnings = self.warnings[-32:]
            self.warning_revision += 1

    def _identity_ok(self, identity: Mapping[str, Any]) -> bool:
        session = str(identity.get("session_id") or "")
        run = str(identity.get("run_id") or "")
        turn = str(identity.get("turn_id") or "")
        if session and self.session_id and session != self.session_id:
            self._warn(
                f"event session identity changed: {self.session_id} -> {session}"
            )
            return False
        if run and self.run_id and run != self.run_id:
            self._warn(f"event run identity changed: {self.run_id} -> {run}")
            return False
        if session and not self.session_id:
            self.session_id = session
        if run and not self.run_id:
            self.run_id = run
        if turn:
            self.turn_id = turn
        return True

    @staticmethod
    def _legacy_key(event: Mapping[str, Any]) -> str:
        kind, data, _timestamp, _identity = event_parts(event)
        for key in ("call_id", "request_id", "approval_id", "checkpoint_id"):
            value = data.get(key)
            if value not in (None, ""):
                return f"{kind}:{key}:{value}"
        return ""

    def ingest(self, event: Any) -> List[Dict[str, Any]]:
        """Return newly accepted rows in canonical sequence order."""
        self.raw_events += 1
        if not isinstance(event, Mapping):
            self._warn("event row is not an object")
            return []
        try:
            schema = int(event.get("schema_version", EVENT_SCHEMA_VERSION))
        except (TypeError, ValueError):
            self._warn("event schema version is invalid")
            return []
        if schema != EVENT_SCHEMA_VERSION:
            self._warn(f"unsupported event schema version {schema}")
            return []
        kind, _data, _timestamp, identity = event_parts(event)
        if not kind:
            self._warn("event row has no event kind")
            return []
        if "sequence" not in event or event.get("sequence") in (None, ""):
            if not self._identity_ok(identity):
                return []
            key = self._legacy_key(event)
            if key and key in self.legacy_keys:
                self.duplicate_count += 1
                return []
            if key:
                self.legacy_keys.add(key)
            return [dict(event)]
        try:
            sequence = int(identity.get("sequence") or 0)
        except (TypeError, ValueError):
            self._warn("event sequence is invalid")
            return []
        if sequence <= 0:
            self._warn("event sequence must be positive")
            return []
        if not self._identity_ok(identity):
            return []
        if sequence <= self.cursor or sequence in self.seen_sequences:
            self.duplicate_count += 1
            return []
        if sequence in self.pending:
            self.duplicate_count += 1
            return []
        if sequence > self.cursor + 1:
            self.out_of_order_count += 1
            self._warn(f"event gap: waiting for sequence {self.cursor + 1}")
        self.pending[sequence] = dict(event)
        accepted: List[Dict[str, Any]] = []
        while self.cursor + 1 in self.pending:
            row = self.pending.pop(self.cursor + 1)
            self.cursor += 1
            self.last_sequence = self.cursor
            self.seen_sequences.add(self.cursor)
            accepted.append(row)
        if len(self.pending) > 256:
            self._warn("event buffer exceeded its safety limit")
            for sequence_to_drop in sorted(self.pending)[256:]:
                self.pending.pop(sequence_to_drop, None)
        return accepted

    def mark_reconnect(self, reason: str = "stream reconnected") -> None:
        """Record a reconnect while preserving the validated cursor."""
        self.reconnect_count += 1
        self.connected = True
        self._warn(str(reason or "stream reconnected"))

    def seek(self, sequence: int) -> None:
        """Advance the cursor for an already-replayed existing journal."""
        try:
            value = max(0, int(sequence))
        except (TypeError, ValueError):
            return
        self.cursor = max(self.cursor, value)
        self.last_sequence = max(self.last_sequence, self.cursor)

    def snapshot(self) -> Dict[str, Any]:
        """Return JSON-friendly cursor and warning state."""
        return {
            "cursor": self.cursor,
            "last_sequence": self.last_sequence,
            "pending_sequences": sorted(self.pending),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "warnings": list(self.warnings),
            "duplicate_count": self.duplicate_count,
            "out_of_order_count": self.out_of_order_count,
            "reconnect_count": self.reconnect_count,
            "connected": self.connected,
            "raw_events": self.raw_events,
        }


def terminal_status(value: Any) -> str:
    """Normalize legacy and canonical completion statuses for display."""
    status = str(value or "").strip().lower()
    aliases = {
        "success": "completed_verified",
        "completed": "completed_unverified",
        "already_exists": "completed_unverified",
        "error": "failed",
        "aborted": "cancelled",
        "canceled": "cancelled",
        "interrupted": "cancelled",
        "approval_required": "needs_input",
    }
    return aliases.get(status, status or "failed")


def status_is_verified(value: Any) -> bool:
    """Return whether a status is verifier-backed completion."""
    return terminal_status(value) == "completed_verified"


def status_is_completed(value: Any) -> bool:
    """Return whether a status is a terminal completed state."""
    return terminal_status(value) in {"completed_verified", "completed_unverified"}


def status_is_success(value: Any) -> bool:
    """Return whether a status is safely presentable as verified success."""
    return status_is_verified(value)


def status_label(value: Any) -> str:
    """Return a compact human label for a canonical or legacy status."""
    raw = str(value or "").strip().lower()
    normalized = terminal_status(value)
    if raw == "error":
        return "ERROR"
    if normalized == "completed_verified":
        return "SUCCESS · VERIFIED"
    if normalized == "completed_unverified":
        return "COMPLETED · UNVERIFIED"
    return normalized.replace("_", " ").upper()


# ---------------------------------------------------------------------------
# THE HONESTY AUTHORITY (R2-17)
#
# The project's invariant is that unverified work is never presented as
# done. The defect this section exists to close is that RUN METADATA did
# exactly that: `interactive._session_status_from_trace` collapsed
# `completed_verified` AND `completed_unverified` into the single word
# `"completed"`, so `/sessions`, the TUI sessions browser, the command
# palette's session hint, and `neo --list-sessions` all rendered an
# unverified run with a word indistinguishable from a verified one.
#
# `run_verdict` is the ONE collapse-proof reduction. Every surface that
# can present a run's outcome routes through it, and it fails CLOSED:
#
# - `completed_verified` with no clean evidence -> `unverified`
# - a bare legacy `completed` / `success` / `passed` word -> `unverified`
#   (the collapse destroyed the information; the honest reading of
#   "finished, no proof" is unverified, and it is also what makes every
#     index row written BEFORE this change render honestly)
# - anything unrecognized -> `unknown`, never `verified`
#
# `HONESTY_SURFACES` is the enumeration of the surfaces that can present a
# run's outcome. `tests/test_ceiling_r2_17_daily_truth.py` parameterises
# over it and fails if a surface is added here without a case, so the gate
# cannot silently stop covering a renderer.
# ---------------------------------------------------------------------------

#: The complete vocabulary a surface may speak about a run. Anything a
#: surface is handed that does not resolve into one of these is a defect
#: and resolves to `unknown` — never to a success.
RUN_VERDICTS: Tuple[str, ...] = (
    "verified",
    "unverified",
    "pending",
    "running",
    "cancelled",
    "failed",
    "unknown",
)

#: Short, width-stable labels for a session/index METADATA row. These are
#: deliberately terse (the sessions table is a fixed-width column) and
#: deliberately distinct: `unverified` and `verified` must never render as
#: the same string, which is the whole defect.
VERDICT_LABELS: Dict[str, str] = {
    "verified": "verified",
    "unverified": "unverified",
    "pending": "pending",
    "running": "running",
    "cancelled": "cancelled",
    "failed": "failed",
    "unknown": "unknown",
}

#: Every surface that can present a run's outcome to a human or a script.
#: Kept in the product (not the test) so adding a renderer is a loud
#: failure of the honesty gate rather than a silent gap in coverage.
HONESTY_SURFACES: Tuple[str, ...] = (
    # TUI / REPL result rendering
    "runview.card_lines",
    "runview.status_lines",
    "runview.headless_status",
    "interactive.result_display",
    # run METADATA (the R2-G45 defect)
    "interactive.sessions_rows",
    "session.index_row",
    "tui.palette_session_hint",
    # machine surfaces
    "command_exec.envelope",
    "main.result_json",
    "headless.result_envelope",
    # the notification path
    "notify.receipt",
)

#: Lifecycle words the session/index metadata layer stores. They answer
#: "can this run be continued", NOT "was it verified" — a resumable run
#: has no verdict yet, so it is `pending`, never `unverified`-as-in-failed.
_LIFECYCLE_VERDICTS: Dict[str, str] = {
    "running": "running",
    "live": "running",
    "in_progress": "running",
    "queued": "running",
    "resumable": "pending",
    "needs_input": "pending",
    "approval_required": "pending",
    "blocked": "pending",
    "interrupted": "pending",
    # The collapse. Read conservatively, forever.
    "completed": "unverified",
    "already_exists": "unverified",
    "done": "unverified",
    "ok": "unverified",
    "success": "unverified",
    "passed": "unverified",
    "verified": "unverified",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "aborted": "cancelled",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "timeout": "failed",
    "unknown": "unknown",
}


def run_verdict(
    status: Any,
    *,
    verification_state: str = "",
    evidence: Any = None,
) -> str:
    """Reduce ANY run status to one member of :data:`RUN_VERDICTS`.

    Fails CLOSED in the only direction that matters: no input, however
    flattering, can produce ``"verified"`` without clean verifier
    evidence. Assumes nothing about `status` — it accepts canonical
    statuses (``completed_verified``), the harness's legacy words
    (``success``), the session metadata lifecycle words (``completed``,
    ``resumable``), a ``TaskResult``, or an evidence-free projection
    field. `evidence` accepts a mapping, a list of mappings, or None and
    is consulted exactly as :func:`_clean_verification` reads it.
    `verification_state` is the projection's own fold and is used only to
    break the tie for a status that names no completion of its own.
    """
    raw = str(getattr(status, "status", status) or "").strip().lower()
    state = str(verification_state or "").strip().lower()

    if raw in ("completed_verified",):
        # Same rule as effective_terminal_status: the word alone is not
        # evidence. Reuse it so the two authorities cannot disagree.
        records = (
            [evidence]
            if isinstance(evidence, Mapping)
            else [item for item in (evidence or []) if isinstance(item, Mapping)]
        )
        return (
            "verified" if records and _clean_verification(records[-1]) else "unverified"
        )
    if raw == "completed_unverified":
        return "unverified"
    if raw in _LIFECYCLE_VERDICTS:
        mapped = _LIFECYCLE_VERDICTS[raw]
        # A lifecycle word that claims completion can be upgraded to
        # `verified` ONLY by clean evidence; the reverse (a `completed`
        # word with a clean `verify` state) is not a claim of completion,
        # so the conservative `unverified` stands.
        if (
            mapped == "unverified"
            and state == "verified"
            and _has_clean_evidence(evidence)
        ):
            return "verified"
        return mapped
    if raw == "":
        # No status at all. A projection that is mid-run says so; a
        # projection that never said anything is `unknown`, not success.
        return "running" if state == "running" else "unknown"
    if state in ("verified", "flaky", "failed", "error"):
        return "unverified" if state in ("flaky", "failed", "error") else "unknown"
    return "unknown"


def _has_clean_evidence(evidence: Any) -> bool:
    """True when the newest verification record proves target + suite health."""
    records = (
        [evidence]
        if isinstance(evidence, Mapping)
        else [item for item in (evidence or []) if isinstance(item, Mapping)]
    )
    return bool(records) and _clean_verification(records[-1])


def run_verdict_label(verdict: Any) -> str:
    """Return the width-stable metadata label for a verdict.

    An unrecognized verdict degrades to ``"unknown"`` — a surface must
    never print a status word this module does not define, because an
    undefined word is how a lie gets typed.
    """
    return VERDICT_LABELS.get(str(verdict or "").strip().lower(), "unknown")


def verdict_is_success(verdict: Any) -> bool:
    """Whether a verdict may be presented as success. Only ``verified`` may."""
    return str(verdict or "").strip().lower() == "verified"


def honest_row_status(row: Mapping[str, Any]) -> str:
    """The honest label for one session / index / ledger metadata row.

    Prefers the row's own ``display_status`` and falls back to ``status``.

    The two fields are DIFFERENT LAYERS and conflating them is a real bug
    this function was written to prevent: ``display_status`` is a VERDICT
    already resolved from the run's own journal by :func:`run_verdict`, so
    it is passed through after a vocabulary check; ``status`` is the raw
    lifecycle word and IS reduced. Re-reducing a verdict applied the
    fail-closed rule one layer too deep and turned a recorded `verified`
    back into `unverified` — which would have silently destroyed the exact
    label this whole fix exists to produce.

    A row that carries neither is ``unknown``. Never raises; a
    non-mapping is ``unknown``.

    This is the function every metadata renderer must call. It exists
    because the metadata layer historically stored a word that did not
    distinguish verified from unverified, and a renderer that reads the
    raw field re-introduces the defect on every new surface.
    """
    if not isinstance(row, Mapping):
        return run_verdict_label("unknown")
    recorded = str(row.get("display_status") or "").strip().lower()
    if recorded in VERDICT_LABELS:
        # Already a verdict: pass it through, do NOT re-reduce it.
        return run_verdict_label(recorded)
    return run_verdict_label(run_verdict(row.get("status")))


# ---------------------------------------------------------------------------
# PER-MODEL-CALL RECEIPTS + LEDGER RECONCILIATION (R2-17 item 4)
#
# `/cost` used to sum `trace.jsonl`'s `model_response` usage records, which
# only covers the MAIN conversation. Every OTHER call the product spends
# money on — the difficulty classifier, a routed retry, a failed
# provider attempt, a subagent — is recorded in the router's own ledger
# (`{task_id}.runtime/model_ledger.jsonl`, Boundary 2) and left a real
# spend with no audit trail. These two functions read the LEDGER and
# reconcile it against the trace, so a reader can see both the total and
# the calls the conversation's own view was missing.
# ---------------------------------------------------------------------------

#: A receipt line is bounded; a 200k-token answer costs the same to list
#: as a 20-token one.
RECEIPT_SUMMARY_CHARS = 96
RECEIPT_MAX_ROWS = 64

_LEDGER_COST_KEYS = ("cost_usd", "cost", "usd")
_LEDGER_TOKEN_KEYS = ("tokens", "total_tokens")


def _ledger_cost(record: Mapping[str, Any]) -> float:
    """Read a ledger/usage row's cost from any of its documented keys."""
    usage = record.get("usage")
    source = usage if isinstance(usage, Mapping) else record
    for key in _LEDGER_COST_KEYS:
        try:
            value = source.get(key)
        except AttributeError:
            continue
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _ledger_tokens(record: Mapping[str, Any]) -> int:
    """Read a ledger/usage row's token count, defaulting to 0 when absent."""
    usage = record.get("usage")
    source = usage if isinstance(usage, Mapping) else record
    for key in _LEDGER_TOKEN_KEYS:
        try:
            value = source.get(key)
        except (TypeError, ValueError):
            continue
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def model_call_receipts(log_root: Any, task_id: str) -> List[Dict[str, Any]]:
    """One bounded receipt per model call the run's own ledger recorded.

    Reads ``{log_root}/{task_id}.runtime/model_ledger.jsonl`` — the
    router's Boundary-2 per-call ledger, which records EVERY attempt
    including failed ones, retries, and calls made outside the main
    conversation. Assumes nothing about the row shape beyond it being a
    JSON object; an unreadable, missing, or torn file yields ``[]`` and
    never raises.

    Each receipt carries `priced` (False when no cost was recorded, so an
    unpriced call reads as UNKNOWN rather than as free) and `outcome`
    (``ok`` / ``failed`` / ``skipped`` / ``unknown``), because a
    successful-looking receipt for a call that was skipped by the privacy
    or offline gate is exactly the receipt this exists to prevent.
    """
    receipts: List[Dict[str, Any]] = []
    try:
        path = Path(log_root) / f"{task_id}.runtime" / "model_ledger.jsonl"
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn tail is not a receipt
        if not isinstance(record, Mapping):
            continue
        cost = _ledger_cost(record)
        prompt_tokens = 0
        completion_tokens = 0
        for key in ("prompt_tokens", "completion_tokens"):
            try:
                value = int(record.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if key == "prompt_tokens":
                prompt_tokens = value
            else:
                completion_tokens = value
        outcome = str(record.get("outcome") or "").strip().lower()
        if not outcome:
            outcome = "failed" if record.get("error") else "unknown"
        reason = str(record.get("reason") or record.get("error") or "")[:120]
        receipts.append(
            {
                "call_id": str(record.get("call_id") or record.get("id") or ""),
                "ts": record.get("ts"),
                "model": str(record.get("model") or ""),
                "provider": str(record.get("provider") or ""),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tokens": _ledger_tokens(record) or (prompt_tokens + completion_tokens),
                "cost_usd": round(cost, 8),
                "priced": cost > 0.0 or record.get("cost_source") is not None,
                "cost_source": str(record.get("cost_source") or ""),
                "hint": str(
                    record.get("difficulty_hint") or record.get("routed_via_hint") or ""
                ),
                "streamed": bool(record.get("streamed")),
                "attempt": record.get("attempt"),
                "outcome": outcome,
                "reason": reason,
            }
        )
        if len(receipts) >= RECEIPT_MAX_ROWS:
            break
    return receipts


def cost_reconciliation(log_root: Any, task_id: str) -> Dict[str, Any]:
    """Reconcile the conversation's own spend against the full ledger.

    The conversation view (`trace.jsonl` `model_response` usage) and the
    router ledger (`{task_id}.runtime/model_ledger.jsonl`) are two views
    of the same spend. They can legitimately differ — the trace records
    one row per COMPLETED call while the ledger records every ATTEMPT —
    so the receipt reports the difference rather than asserting they must
    match. `reconciled` is True only when they do.

    A run with NO ledger yields `ledger_available: False` and a reason,
    never a zero that reads as "we checked and it was free". Never raises.
    """
    trace_path = Path(log_root) / str(task_id) / "trace.jsonl"
    trace_calls = 0
    trace_cost = 0.0
    trace_tokens = 0
    try:
        text = (
            trace_path.read_text(encoding="utf-8", errors="replace")
            if trace_path.is_file()
            else ""
        )
    except (OSError, TypeError, ValueError):
        text = ""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data, _ts, _identity = event_parts(event)
        if kind not in ("model_response", "model_completed"):
            continue
        trace_calls += 1
        usage = data.get("usage") or data
        if not isinstance(usage, Mapping):
            continue
        trace_cost += _ledger_cost({"usage": usage})
        trace_tokens += _ledger_tokens({"usage": usage})

    receipts = model_call_receipts(log_root, task_id)
    ledger_cost = round(sum(float(r["cost_usd"]) for r in receipts), 8)
    ledger_tokens = sum(int(r["tokens"]) for r in receipts)
    unpriced = sum(1 for r in receipts if not r["priced"])
    available = bool(
        (Path(log_root) / f"{task_id}.runtime" / "model_ledger.jsonl").is_file()
    )
    return {
        "task_id": str(task_id),
        "ledger_available": available,
        "ledger_calls": len(receipts),
        "ledger_cost_usd": ledger_cost,
        "ledger_tokens": ledger_tokens,
        "unpriced_calls": unpriced,
        "trace_calls": trace_calls,
        "trace_cost_usd": round(trace_cost, 8),
        "trace_tokens": trace_tokens,
        # The whole point: calls the conversation's own view could not see.
        "unreceipted_calls": max(0, len(receipts) - trace_calls),
        "unreceipted_cost_usd": round(max(0.0, ledger_cost - trace_cost), 8),
        "reconciled": bool(
            available
            and len(receipts) == trace_calls
            and abs(ledger_cost - trace_cost) < 1e-6
        ),
        "reason": (
            ""
            if available
            else "no model ledger for this run; only the conversation's own usage rows were available"
        ),
    }


# ---------------------------------------------------------------------------
# THE RESUME BRIEFING (R2-17 item 3)
#
# One honest screen on start: what happened, what it cost, what is
# UNVERIFIED, what happens next. Every number is re-derived from the
# run's own journal at render time, so the briefing cannot drift from
# what actually happened — the same discipline as `read_run_facts`.
# ---------------------------------------------------------------------------


#: The literal every "we do not know" latency renders as. Declared ONCE in
#: the module that owns latency rendering, so the rail, the run view and a
#: `--json` document cannot answer "what do we not know?" three ways.
LATENCY_UNAVAILABLE = "unavailable"

#: The reasons a latency is unavailable, as a CLOSED vocabulary. A bare
#: "unavailable" leaves a reader guessing whether the number was slow, lost,
#: or never existed; naming the reason makes it actionable.
LATENCY_UNAVAILABLE_REASONS = (
    #: The call was not streamed, so no first token ever arrived.
    "streaming_off",
    #: The provider sent no usage frame and nothing could be estimated.
    "not_measured",
    #: The run recorded no such receipt at all.
    "absent",
)

#: The names a producer may give a retrieval duration. Read as an
#: ALTERNATIVE list, not a preferred one, so this is robust to whichever
#: spelling the retrieval producer lands with - a display half that breaks
#: on a key rename is a display half that has to be re-edited for a change
#: that was always going to happen.
_RETRIEVAL_DURATION_KEYS = (
    "duration_s",
    "duration_ms",
    "elapsed_s",
    "took_s",
)
_RETRIEVAL_ENGINE_KEYS = ("engine", "backend", "source")
_RETRIEVAL_BOUND_KEYS = ("max_results", "limit", "cap", "top_k")
_RETRIEVAL_RETURNED_KEYS = ("returned", "returned_count", "count", "results")
_RETRIEVAL_TOTAL_KEYS = ("total_matches", "total", "available")


def _first_number(data: Mapping[str, Any], keys: Any) -> Optional[float]:
    """The first key that carries a real number, or ``None``.

    ``None`` means "this receipt does not say", and every caller renders
    that as the absent state. It is never ``0``: a retrieval that took no
    measurable time is not a fact, it is a receipt that lacked a clock.
    """
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and float(value) > 0.0:
            return float(value)
    return None


def retrieval_projection(data: Any) -> Dict[str, Any]:
    """One retrieval receipt, as a fact set with honest absent states.

    Four rules, and this function is where they are kept:

    * ``duration_s`` is ``None`` when the receipt carried no clock. It is
      never ``0``, because "the search took 0 seconds" is a measurement
      nobody took and reads as a fast one.
    * ``truncated`` is a real boolean, and ``bound`` NAMES the limit when
      one is known. "truncated: true" tells a reader something is missing
      and not how much; a bound is the difference between "your search was
      narrowed" and "narrowed to 20 of 9,000".
    * ``first_token_s`` is ``None`` when the call was not streamed, and the
      renderer says WHY rather than printing a number.
    * Nothing here raises. A malformed receipt is an absent fact.
    """
    record = data if isinstance(data, Mapping) else {}
    if "retrieval" in record and isinstance(record.get("retrieval"), Mapping):
        record = dict(record["retrieval"])
    duration = _first_number(record, _RETRIEVAL_DURATION_KEYS)
    if duration is not None and duration > 1000.0:
        # A millisecond field read as seconds is the classic unit lie. The
        # threshold is generous because a search genuinely taking 20
        # minutes is not a thing; anything past 1000 "seconds" is a ms
        # value, and misreading it as 144 seconds is the exact flattering
        # error W1.5 is about.
        duration = duration / 1000.0
    bound = _first_number(record, _RETRIEVAL_BOUND_KEYS)
    returned = _first_number(record, _RETRIEVAL_RETURNED_KEYS)
    total = _first_number(record, _RETRIEVAL_TOTAL_KEYS)
    truncated = bool(record.get("truncated")) or (
        bound is not None and returned is not None and returned >= bound
    )
    engine = ""
    for key in _RETRIEVAL_ENGINE_KEYS:
        text = str(record.get(key) or "").strip()
        if text:
            engine = text
            break
    return {
        "available": bool(record),
        "duration_s": duration,
        "duration_reason": "" if duration is not None else "absent",
        "engine": engine,
        "truncated": truncated,
        "bound": int(bound) if bound is not None else None,
        "returned": int(returned) if returned is not None else None,
        "total": int(total) if total is not None else None,
    }


def retrieval_lines(facts: Any, *, width: int = 0) -> List[str]:
    """Render retrieval cost, naming the bound when the search was truncated.

    A slow search that happened three turns ago must not render as instant,
    so this reads the receipt rather than the live line - the same
    "re-derive at render time from the run's own records" discipline as
    :func:`read_run_facts`. A run with no retrieval receipt renders NOTHING,
    which is the honest absent state; a `0.0s` here would read as a measured
    instant search that never happened.
    """
    if not isinstance(facts, Mapping) or not facts.get("available"):
        return []
    rows: List[str] = []
    duration = facts.get("duration_s")
    if duration is None:
        rows.append(
            f"retrieval  {LATENCY_UNAVAILABLE} "
            f"({facts.get('duration_reason') or 'absent'})"
        )
    else:
        engine = str(facts.get("engine") or "")
        label = "retrieval"
        if engine:
            label = f"retrieval [{engine}]"
        rows.append(f"{label}  {float(duration):.1f}s")
    if facts.get("truncated"):
        bound = facts.get("bound")
        returned = facts.get("returned")
        total = facts.get("total")
        parts: List[str] = []
        if returned is not None and bound is not None:
            parts.append(f"showing {returned} of a {bound} cap")
        elif bound is not None:
            parts.append(f"capped at {bound}")
        if total is not None:
            parts.append(f"{total} matched")
        detail = "; ".join(parts) if parts else "the bound was not recorded"
        rows.append(f"retrieval  TRUNCATED - {detail}")
    return rows


def briefing_facts(log_root: Any, task_id: str) -> Dict[str, Any]:
    """The real numbers for one run's resume briefing.

    Reads the run's own records: `trace.jsonl` for the terminal event,
    the model ledger for spend, `state.json` for plan progress, and
    `git_output` for what was actually committed. Assumes the run
    directory may be missing or half-written; every field degrades to an
    honest empty/zero with `available: False` rather than raising.
    """
    root = Path(log_root) if log_root is not None else Path("logs")
    tid = str(task_id or "")
    facts: Dict[str, Any] = {
        "available": False,
        "task_id": tid,
        "repo": "",
        "issue": "",
        "mode": "",
        "status": "unknown",
        "verdict": "unknown",
        "verdict_label": "unknown",
        "verified": False,
        "attempts": None,
        "model_calls": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "cost_known": False,
        "elapsed_s": None,
        "files": [],
        "steps_done": 0,
        "steps_total": 0,
        "resumable": False,
        "branch": "",
        "commit": "",
        "unverified_reason": "",
        "next_steps": [],
        "warnings": [],
        #: The retrieval receipt, so a slow search is visible when a reader
        #: scrolls back rather than only on the live line it scrolled past.
        "retrieval": retrieval_projection({}),
    }
    if not tid:
        return facts
    try:
        from memory.paths import safe_task_dir

        task_dir = safe_task_dir(tid, root)
    except Exception:
        try:
            task_dir = root / tid
        except Exception:
            return facts
    if task_dir is None or not task_dir.is_dir():
        return facts

    result: Dict[str, Any] = {}
    evidence: List[Mapping[str, Any]] = []
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    try:
        text = (task_dir / "trace.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    cursor = EventCursor(tid)
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for accepted in cursor.ingest(event):
            kind, data, timestamp, _identity = event_parts(accepted)
            if timestamp:
                if first_ts is None:
                    first_ts = timestamp
                last_ts = timestamp
            if kind in ("run_started", "task_start"):
                facts["repo"] = str(data.get("repo_path") or facts["repo"])
                facts["issue"] = str(data.get("issue_text") or "")[:200]
                facts["mode"] = str(data.get("mode") or "")
                facts["available"] = True
            elif kind in ("model_response", "model_completed"):
                facts["model_calls"] += 1
                facts["tokens"] += _ledger_tokens({"usage": data.get("usage") or data})
            elif kind in ("verify", "verification", "final_verify"):
                record = (
                    data.get("result")
                    if isinstance(data.get("result"), Mapping)
                    else data
                )
                evidence.append(record)
            elif kind in ("result", "run_finished", "task_end", "completion_decision"):
                nested = data.get("result")
                result = (
                    {**dict(nested), **dict(data)}
                    if isinstance(nested, Mapping)
                    else dict(data)
                )
            elif kind == "git_output":
                facts["branch"] = str(data.get("branch") or facts["branch"])
                facts["commit"] = str(
                    data.get("commit") or data.get("commit_sha") or ""
                )
            elif kind in ("retrieval", "retrieval_truncated"):
                # LAST receipt wins, and a bare `retrieval_truncated` marker
                # keeps whatever the earlier receipt already established. A
                # truncation notice that erased the duration would make a
                # slow search look unmeasured - the opposite of the truth.
                prior = facts.get("retrieval") or {}
                merged = dict(prior.get("raw") or {})
                merged.update(dict(data or {}))
                projected = retrieval_projection(merged)
                projected["raw"] = merged
                facts["retrieval"] = projected

    warnings = [str(w) for w in cursor.snapshot().get("warnings") or []]
    facts["warnings"] = warnings[:4]

    status = str(result.get("status") or "")
    if status:
        facts["status"] = status
        facts["available"] = True
    else:
        # No terminal event. Distinguish "interrupted, continuable" from
        # "finished and the terminal event is missing" — the first is a
        # resume candidate, the second is a run whose record is broken.
        facts["status"] = "resumable" if not evidence else "failed"
    facts["attempts"] = result.get("attempts")
    if facts["attempts"] is not None:
        try:
            facts["attempts"] = int(facts["attempts"])
        except (TypeError, ValueError):
            facts["attempts"] = None
    if result.get("aborted"):
        facts["status"] = "cancelled"
    if result.get("note"):
        facts["unverified_reason"] = str(result["note"])[:160]

    try:
        cost = float(result.get("cost_usd", result.get("cost")))
    except (TypeError, ValueError):
        cost = None
    if cost is not None:
        facts["cost_usd"] = round(cost, 6)
        facts["cost_known"] = True

    verdict = run_verdict(facts["status"], evidence=evidence)
    # A run that is still live has no verdict yet; say so rather than
    # dressing an unfinished run as unverified work.
    if not result and evidence:
        verdict = "pending"
    facts["verdict"] = verdict
    facts["verdict_label"] = run_verdict_label(verdict)
    facts["verified"] = verdict_is_success(verdict)

    if first_ts is not None and last_ts is not None and result:
        facts["elapsed_s"] = round(max(0.0, last_ts - first_ts), 1)

    try:
        state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if isinstance(state, Mapping):
        steps = state.get("steps")
        if isinstance(steps, list):
            facts["steps_total"] = len(steps)
            facts["steps_done"] = sum(
                1
                for s in steps
                if isinstance(s, Mapping) and str(s.get("state") or "") == DONE
            )
        touched = state.get("files_touched")
        if isinstance(touched, list):
            facts["files"] = [str(f) for f in touched][:12]

    facts["resumable"] = facts["status"] in ("resumable", "running", "cancelled")
    facts["next_steps"] = _briefing_next_steps(facts)
    return facts


def _briefing_next_steps(facts: Mapping[str, Any]) -> List[str]:
    """The concrete next actions, derived from what the briefing found."""
    steps: List[str] = []
    verdict = str(facts.get("verdict") or "")
    if verdict == "verified":
        steps.append("/diff to review the change · /undo to revert it")
    elif verdict == "unverified":
        steps.append(
            "unverified: the run finished without a clean verifier receipt — "
            "re-run the target test yourself before trusting it"
        )
        if facts.get("resumable"):
            steps.append(f"/resume {facts.get('task_id')} to continue it")
    elif verdict == "pending":
        steps.append(f"/resume {facts.get('task_id')} to continue where it stopped")
    elif verdict == "failed":
        steps.append("/doctor to check the machine · /trace to read the failure")
        if facts.get("resumable"):
            steps.append(f"/resume {facts.get('task_id')} to retry")
    elif verdict == "cancelled":
        steps.append(f"/resume {facts.get('task_id')} to pick it back up")
    steps.append("/cost to reconcile this run's spend against the ledger")
    return steps[:4]


def briefing_lines(facts: Mapping[str, Any], *, width: int = 72) -> List[str]:
    """Markup lines for the resume briefing, from :func:`briefing_facts` only.

    Reads nothing but `facts`, so the briefing cannot claim a number the
    fold did not read. An unavailable run renders one honest line naming
    what is missing rather than a briefing of zeros. Never raises.
    """
    from rich.markup import escape

    data = dict(facts or {})
    dot = ui.DOT
    if not data.get("available"):
        tid = str(data.get("task_id") or "")
        return [
            f"[neo.muted]no run record for "
            f"[{ui.TEXT_PRIMARY}]{escape(tid or '(no run)')}[/][/] "
            f"[neo.muted]— nothing to brief; start one and it will be recorded[/]"
        ]
    verdict = str(data.get("verdict") or "unknown")
    if verdict == "verified":
        style, mark = "neo.ok", ui.GLYPHS["ok"]
    elif verdict in ("unverified", "pending"):
        style, mark = "neo.warn", ui.GLYPHS["wait"]
    elif verdict == "cancelled":
        style, mark = "neo.muted", ui.GLYPHS["wait"]
    else:
        style, mark = "neo.error", ui.GLYPHS["fail"]
    head = (
        f"[neo.accent]since your last run[/] [neo.muted]{dot}[/] "
        f"[{style}]{mark} {escape(run_verdict_label(verdict))}[/] "
        f"[neo.muted]{dot}[/] [{ui.TEXT_PRIMARY}]"
        f"{escape(str(data.get('issue') or data.get('task_id') or ''))[:width]}[/]"
    )
    rows = [head]
    chips: List[str] = []
    if data.get("attempts") is not None:
        chips.append(f"{data['attempts']} attempt(s)")
    if data.get("model_calls"):
        chips.append(f"{int(data['model_calls'])} model call(s)")
    if data.get("elapsed_s") is not None:
        chips.append(fmt_elapsed(data["elapsed_s"]))
    if data.get("cost_known"):
        chips.append(ui.fmt_cost(float(data.get("cost_usd") or 0.0)))
    elif data.get("model_calls"):
        # "unknown cost" — never "$0.0000", which reads as measured free.
        chips.append("cost unknown")
    if data.get("steps_total"):
        chips.append(f"{data['steps_done']}/{data['steps_total']} steps")
    if chips:
        rows.append(
            f"[neo.muted]   {dot}[/] [{ui.TEXT_PRIMARY}]"
            + f" {dot} ".join(chips)
            + "[/]"
        )
    if data.get("files"):
        rows.append(
            f"[neo.muted]   {dot} files[/] [{ui.TEXT_PRIMARY}]"
            f"{escape(', '.join(data['files'][:4]))}[/]"
        )
    if data.get("branch"):
        sha = str(data.get("commit") or "")[:8]
        tail = f" {dot} {sha}" if sha else ""
        rows.append(
            f"[neo.muted]   {dot} branch[/] [neo.accent2]"
            f"{escape(str(data['branch']))}[/][neo.muted]{tail}[/]"
        )
    if verdict == "unverified" and data.get("unverified_reason"):
        rows.append(
            f"[neo.warn]   {dot} note[/] [{ui.TEXT_PRIMARY}]"
            f"{escape(str(data['unverified_reason'])[:width])}[/]"
        )
    for step in data.get("next_steps") or []:
        # Not truncated: a next action cut mid-word ("re-run t") is worse
        # than useless, and these strings are authored and bounded, so a
        # terminal wraps them cleanly where a width-based cut cannot.
        rows.append(f"[neo.muted]   {dot} next[/] [neo.accent]{escape(str(step))}[/]")
    for warning in data.get("warnings") or []:
        rows.append(
            f"[neo.warn]   {dot} journal[/] [{ui.TEXT_PRIMARY}]"
            f"{escape(str(warning)[:width])}[/]"
        )
    return rows


def _mode_tool_allowed(mode: str, tool: str) -> bool:
    """Return whether a projection mode exposes a tool without importing the UI."""
    normalized_mode = normalize_projection_mode(mode)
    aliases = {
        "shell": "bash",
        "test": "verify",
        "process": "bash",
        "git": "git_status",
        "done": "finish",
        "mcp_call": "mcp",
    }
    name = aliases.get(str(tool or "").lower(), str(tool or "").lower())
    return name in set(MODE_PROJECTIONS[normalized_mode]["tools"])


class TodoStep:
    """One plan sub-step and its live state.

    state is PENDING / DONE / FAILED / SKIPPED; the in-flight marker
    lives on the model (active_id), not the step, so a step that is
    being retried renders as "current" while its stored state stays
    honest about the last outcome.
    """

    __slots__ = ("checkpoint", "description", "sid", "state")

    def __init__(self, sid: int, description: str, checkpoint: str = "") -> None:
        self.sid = sid
        self.description = description or ""
        self.checkpoint = checkpoint or ""
        self.state = PENDING

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"TodoStep({self.sid}, {self.state}, {self.description!r})"


class TodoModel:
    """Fold trace events into a live checklist of the task's plan.

    Data sources (all existing, none invented):
    - ``plan``            -> the step list (the harness's decomposition)
    - ``step_end``        -> ok ? check the step off : mark it failed
    - ``step_skipped_resume`` -> check off a step completed pre-crash
    - ``attempt_start`` (>1)   -> uncheck everything (the harness rolls
      the work back and resets completed steps on a retry — the trace
      replays the survivors via step_skipped_resume / step_end)
    - ``model_request`` {step-N} -> the "current" marker

    A RE-PLAN (steering) re-emits ``plan``: steps whose description
    matches one already DONE keep their checkmark — the re-plan builds
    on work/, it does not undo it. Assumes events are dicts in arrival
    order from trace.jsonl; malformed ones are ignored, never raised.
    """

    def __init__(self) -> None:
        self.steps: List[TodoStep] = []
        self.active_id: Optional[int] = None
        self._cursor = EventCursor()

    # -- folding -----------------------------------------------------------

    def consume(self, event: Dict[str, Any]) -> bool:
        """Fold one ordered journal row into the checklist."""
        before_revision = self._cursor.warning_revision
        accepted = self._cursor.ingest(event)
        changed = False
        for row in accepted:
            changed = self._consume_unchecked(row) or changed
        if not accepted and self._cursor.warning_revision != before_revision:
            changed = True
        return changed

    def _consume_unchecked(self, event: Dict[str, Any]) -> bool:
        """Fold one trace event; True when the checklist changed."""
        try:
            kind, data, _timestamp, _identity = event_parts(event)
            if not kind:
                return False
            if kind in ("plan", "todo", "run_started"):
                plan = data.get("plan") or data.get("steps")
                if not isinstance(plan, list) and isinstance(
                    data.get("payload"), Mapping
                ):
                    plan = data["payload"].get("plan")
                if kind == "run_started" and not isinstance(plan, list):
                    run_spec = data.get("run_spec")
                    if isinstance(run_spec, Mapping):
                        plan = run_spec.get("plan")
                if isinstance(plan, list):
                    return self._apply_plan(plan)
            if kind == "step_end":
                return self._end_step(data)
            if kind == "step_skipped_resume":
                return self._skip_step(data)
            if kind == "attempt_start":
                return self._start_attempt(data)
            if kind == "model_request":
                return self._touch_active(data)
            if kind == "tool_call" and str(data.get("tool") or "").lower() in {
                "plan",
                "todo",
            }:
                arguments = data.get("arguments")
                if not isinstance(arguments, Mapping):
                    arguments = data.get("args")
                if isinstance(arguments, Mapping) and isinstance(
                    arguments.get("plan"), list
                ):
                    return self._apply_plan(arguments["plan"])
            if (
                kind
                in (
                    "task_end",
                    "run_finished",
                    "result",
                    "completion_decision",
                    "attempt_end",
                )
                and self.active_id is not None
            ):
                self.active_id = None
                return True
            return False
        except Exception:
            return False

    def _apply_plan(self, plan: List[Any]) -> bool:
        prior = {
            st.description: st.state for st in self.steps if st.state in (DONE, SKIPPED)
        }
        new_steps: List[TodoStep] = []
        for st in plan:
            if not isinstance(st, dict):
                continue
            try:
                sid = int(st.get("id"))
            except (TypeError, ValueError):
                continue
            desc = str(st.get("description") or "")
            step = TodoStep(sid, desc, str(st.get("checkpoint") or ""))
            if desc and prior.get(desc) in (DONE, SKIPPED):
                step.state = prior[desc]
            new_steps.append(step)
        if not new_steps:
            return False
        changed = [(s.sid, s.description, s.state) for s in new_steps] != [
            (s.sid, s.description, s.state) for s in self.steps
        ]
        self.steps = new_steps
        if self.active_id is not None and self.active_id not in {
            s.sid for s in new_steps
        }:
            self.active_id = None
            changed = True
        return changed

    def _end_step(self, data: Dict[str, Any]) -> bool:
        raw = data.get("step_id", data.get("step"))
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            try:
                sid = int(str(raw).removeprefix("step-").split(".", 1)[0])
            except (TypeError, ValueError):
                return False
        step = self.find(sid)
        if step is None:
            return False
        step.state = DONE if _evidence_bool(data.get("ok")) else FAILED
        if self.active_id == sid:
            self.active_id = None
        return True

    def _skip_step(self, data: Dict[str, Any]) -> bool:
        raw_value = data.get("step", data.get("step_id"))
        raw = str(raw_value or "")
        head = raw.removeprefix("step-").split(". ", 1)[0]
        try:
            sid = int(head)
        except ValueError:
            return False
        step = self.find(sid)
        if step is None:
            return False
        step.state = SKIPPED
        return True

    def _start_attempt(self, data: Dict[str, Any]) -> bool:
        try:
            n = int(data.get("attempt", data.get("attempt_number")) or 1)
        except (TypeError, ValueError):
            n = 1
        if n <= 1:
            return False
        changed = self.active_id is not None or any(
            s.state in (DONE, FAILED, SKIPPED) for s in self.steps
        )
        for s in self.steps:
            s.state = PENDING
        self.active_id = None
        return changed

    def _touch_active(self, data: Dict[str, Any]) -> bool:
        raw_step = data.get("step", data.get("step_id", data.get("turn")))
        step = str(raw_step or "")
        if isinstance(raw_step, int) and not isinstance(raw_step, bool):
            sid = raw_step
        elif step.startswith("step-"):
            try:
                sid = int(step[len("step-") :])
            except ValueError:
                return False
        elif step.isdigit():
            sid = int(step)
        else:
            if self.active_id is not None:
                self.active_id = None
                return True
            return False
        if sid == self.active_id:
            return False
        self.active_id = sid
        return True

    # -- reading -----------------------------------------------------------

    def find(self, sid: int) -> Optional[TodoStep]:
        """The step with this id, or None."""
        for s in self.steps:
            if s.sid == sid:
                return s
        return None

    def progress(self) -> Tuple[int, int]:
        """(#done-or-skipped, #total) — the honest checklist count."""
        done = sum(1 for s in self.steps if s.state in (DONE, SKIPPED))
        return done, len(self.steps)

    def state_of(self, step: TodoStep) -> str:
        """ACTIVE for the in-flight step, else its stored state."""
        if self.active_id is not None and step.sid == self.active_id:
            return ACTIVE
        return step.state


class RunProjection:
    """Mode-aware live facts folded from the public trace stream.

    The projection is deliberately independent of ``state.json``. Agent
    tasks mutate the live repository and may never create a fix-loop state
    file, so every displayed fact is derived from task_start, model, tool,
    edit, approval, verification, and terminal events instead.
    """

    def __init__(
        self,
        task_id: str = "",
        mode: str = "agent_task",
        started_at: Optional[float] = None,
    ) -> None:
        self.task_id = str(task_id or "")
        self.mode = normalize_projection_mode(mode)
        self.status = "queued"
        self.current_action = "waiting to start"
        self.current_tool = ""
        self.current_call_id = ""
        self.current_turn: Optional[int] = None
        self.changed_files: List[str] = []
        self.file_changes: Dict[str, Dict[str, Any]] = {}
        self.issue: str = ""
        self.repo_path: str = ""
        self._checkpoint_files: Dict[str, str] = {}
        self.latest_verification: Optional[Dict[str, Any]] = None
        self.verification_evidence: List[Dict[str, Any]] = []
        self.approval = "not required"
        self.approval_scope = ""
        self.approval_effect = ""
        self.last_error = ""
        self.elapsed_s: Optional[float] = None
        self.model_calls = 0
        self.tokens = 0
        self.cost_usd = 0.0
        self.answer = ""
        self.events = 0
        self.raw_events = 0
        self.model_calls_known = False
        self.tokens_known = False
        self.cost_known = False
        self.cost_source = ""
        self.approval_note = ""
        self.stream_chars: Optional[int] = None
        #: VEX-PF-04: questions the run asked and nobody has answered.
        #: A pending question blocks the composer exactly as a pending
        #: permission does, so it has to be a first-class projection fact
        #: rather than something a surface reconstructs from raw rows.
        self.questions: List[Dict[str, Any]] = []
        self.project_id = ""
        self.unmapped_kinds: Dict[str, int] = {}
        self.session_id = ""
        self.run_id = ""
        self.turn_id = ""
        self.strategy = ""
        self.context: Dict[str, Any] = {}
        self.checkpoints: List[Dict[str, Any]] = []
        self.diagnostics: List[Dict[str, Any]] = []
        self.subagents: List[Dict[str, Any]] = []
        self.visible_tools: List[str] = []
        self.mcp_calls = 0
        self.blocked_tools: List[str] = []
        self.resume_availability = ""
        self.resumed = False
        self.result: Dict[str, Any] = {}
        self.verification_status = "not_run"
        self.warnings: List[str] = []
        self._cursor = EventCursor(self.task_id)
        self._last_accepted: List[Dict[str, Any]] = []
        self._warning_revision = 0
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._started_at = started_at

    def consume(self, event: Dict[str, Any]) -> bool:
        """Accept, order, and fold one journal event into the live view."""
        before_revision = self._cursor.warning_revision
        accepted = self._cursor.ingest(event)
        self._last_accepted = list(accepted)
        changed = False
        for row in accepted:
            changed = self._consume_unchecked(row) or changed
        self.raw_events = self._cursor.raw_events
        if self._cursor.warning_revision != before_revision:
            self.warnings = list(self._cursor.warnings)
            self._warning_revision = self._cursor.warning_revision
            if not accepted:
                self.current_action = (
                    f"waiting for event {self._cursor.cursor + 1}"
                    if self._cursor.pending
                    else self.current_action
                )
                changed = True
        if self._cursor.pending:
            self.current_action = f"waiting for event {self._cursor.cursor + 1}"
        return changed

    @property
    def last_accepted_events(self) -> List[Dict[str, Any]]:
        """Rows applied by the most recent consume call, in journal order."""
        return list(self._last_accepted)

    @property
    def cursor_warning_revision(self) -> int:
        """Return the cursor warning revision for UI repaint decisions."""
        return self._cursor.warning_revision

    def _consume_unchecked(self, event: Dict[str, Any]) -> bool:
        """Fold one already-validated event without transport bookkeeping."""
        try:
            kind, data, timestamp, identity = event_parts(event)
            if not kind:
                return False
            self.events += 1
            if kind in ("run_resumed", "resume_started", "resumed"):
                self.resumed = True
                self.status = "running"
                self.current_action = "resuming prior run"
                return True
            if identity.get("session_id"):
                self.session_id = str(identity["session_id"])
            if identity.get("run_id"):
                self.run_id = str(identity["run_id"])
            if identity.get("turn_id"):
                self.turn_id = str(identity["turn_id"])
            if timestamp:
                self._first_ts = timestamp if self._first_ts is None else self._first_ts
                self._last_ts = timestamp
                self.elapsed_s = round(max(0.0, self._last_ts - self._first_ts), 1)
            elif self._started_at is not None:
                self.elapsed_s = round(max(0.0, time.time() - self._started_at), 1)

            if data.get("changed_files") or data.get("files_touched"):
                changed_values = data.get("changed_files") or data.get("files_touched")
                if not isinstance(changed_values, (list, tuple, set)):
                    changed_values = [changed_values]
                self._add_file_list(changed_values)
                for path in changed_values:
                    self._record_file_change(path, data)
            if kind in ("phase_changed", "phase_change", "state_change"):
                phase = str(
                    data.get("phase") or data.get("state") or data.get("to_state") or ""
                )
                self.current_action = phase or "phase changed"
                return True
            if kind in ("reasoning_summary", "reasoning"):
                summary = str(
                    data.get("summary") or data.get("text") or data.get("content") or ""
                )
                self.answer = ui.strip_ansi(summary)[:4000]
                self.current_action = "reasoning"
                return True
            if kind in ("file_read", "read_file"):
                self.current_action = f"reading {data.get('path') or 'a file'}"
                return True
            if kind in ("file_search", "search_files"):
                self.current_action = f"searching {data.get('query') or data.get('pattern') or 'the repository'}"
                return True
            if kind in (
                "subagent_started",
                "subagent_start",
                "child_started",
                "spawn",
                "project_sub_task_start",
            ):
                child = {
                    "id": str(
                        data.get("child_id")
                        or data.get("sub_task_id")
                        or data.get("id")
                        or data.get("name")
                        or ""
                    ),
                    "role": str(
                        data.get("role")
                        or data.get("agent")
                        or data.get("kind_role")
                        or "subagent"
                    ),
                    "status": "running",
                }
                self.subagents = [
                    item for item in self.subagents if item.get("id") != child["id"]
                ]
                self.subagents.append(child)
                self.current_action = f"subagent {child['role']} started"
                return True
            if kind in (
                "subagent_finished",
                "subagent_end",
                "child_finished",
                "spawn_error",
                "project_sub_task_end",
                "project_sub_task_already_passing",
            ):
                child_id = str(
                    data.get("child_id")
                    or data.get("sub_task_id")
                    or data.get("id")
                    or ""
                )
                for child in self.subagents:
                    if not child_id or child.get("id") == child_id:
                        child["status"] = "failed" if data.get("error") else "finished"
                        child["summary"] = str(
                            data.get("summary") or data.get("error") or ""
                        )[:240]
                if kind == "spawn_error" and (data.get("error") or data.get("reason")):
                    self.last_error = ui.strip_ansi(
                        str(data.get("error") or data.get("reason"))
                    )[:240]
                self.current_action = "subagent finished"
                return True
            if kind in (
                "retry",
                "model_retry",
                "attempt_retry",
                "crash_retry",
                "model_recovery",
                "kill_requeue",
            ):
                self.status = "running"
                self.current_action = f"retrying: {data.get('reason') or data.get('attempt') or 'attempt'}"
                return True
            if kind in (
                "error",
                "run_error",
                "worker_error",
                "child_error",
                "hang_timeout",
                "wallclock_timeout",
                "kill_exhausted",
                "project_plan_parse_error",
                "project_criteria_parse_error",
                "project_plan_empty_reply_retry",
                "project_criteria_empty_reply_retry",
                "build_tests_parse_error",
                "build_tests_empty_reply_retry",
            ):
                self.last_error = ui.strip_ansi(
                    str(data.get("error") or data.get("reason") or "run error")
                )[:240]
                self.current_action = "handling error"
                return True
            if kind in ("command_output", "process_output", "sandbox_result"):
                output = ui.strip_ansi(
                    str(data.get("output") or data.get("text") or "")
                )
                if data.get("ok") is False or data.get("error"):
                    self.last_error = ui.strip_ansi(
                        str(data.get("error") or output or "command failed")
                    )[:240]
                    self.current_action = "command failed"
                else:
                    self.current_action = "command output received"
                return True
            if kind in ("run_started", "run_start", "task_start", "project_start"):
                run_spec = data.get("run_spec")
                metadata: Mapping[str, Any] = {}
                if isinstance(run_spec, Mapping):
                    self.strategy = str(run_spec.get("strategy") or self.strategy)
                    if not self.issue:
                        self.issue = ui.strip_ansi(str(run_spec.get("request") or ""))[
                            :4000
                        ]
                    if not self.repo_path:
                        self.repo_path = str(
                            run_spec.get("repo_path")
                            or run_spec.get("repository")
                            or ""
                        )
                    raw_metadata = run_spec.get("metadata")
                    if isinstance(raw_metadata, Mapping):
                        metadata = raw_metadata
                        reported_mode = str(metadata.get("mode") or "")
                        if reported_mode:
                            self.mode = normalize_projection_mode(reported_mode)
                self.resumed = self.resumed or bool(
                    data.get("resume")
                    or data.get("resumed")
                    or (isinstance(run_spec, Mapping) and run_spec.get("resume"))
                    or metadata.get("resume")
                    or metadata.get("resumed")
                )
                self.strategy = str(data.get("strategy") or self.strategy)
                self.issue = ui.strip_ansi(
                    str(data.get("issue_text") or data.get("request") or self.issue)
                )[:4000]
                if kind == "project_start":
                    # Build/project mode is a first-class projection, and its
                    # journal opens with `project_start`. Without this the run
                    # never reports a mode, never reports its request, and
                    # never reaches a terminal status at all.
                    self.mode = "build"
                    if not self.issue:
                        self.issue = ui.strip_ansi(str(data.get("request_text") or ""))[
                            :4000
                        ]
                    self.project_id = str(data.get("project_id") or self.project_id)
                self.repo_path = str(
                    data.get("repo_path") or data.get("repository") or self.repo_path
                )
                reported_mode = str(data.get("mode") or "")
                if reported_mode:
                    self.mode = normalize_projection_mode(reported_mode)
                self.status = "running"
                self.current_action = "reading repository context"
                return True
            if kind == "strategy_selected":
                self.strategy = str(
                    data.get("strategy") or data.get("name") or self.strategy
                )
                return True
            if kind in (
                "context",
                "context_built",
                "context_compiled",
                "session_context",
            ):
                self.context.update(dict(data))
                self.current_action = "assembling context"
                return True
            if kind == "context_budget":
                # The journal is the authority for the live fill level; a UI can
                # show it mid-run without reading any other file.
                measured = dict(data)
                measured.pop("categories", None)
                self.context["meter"] = {
                    **(self.context.get("meter") or {}),
                    **measured,
                }
                return True
            if kind == "context_compacted":
                self.context["last_compaction"] = {
                    key: data.get(key)
                    for key in ("compaction_id", "method", "reclaimed_tokens")
                }
                self.context["compactions"] = (
                    int(self.context.get("compactions") or 0) + 1
                )
                return True
            if kind in ("turn_started", "model_request", "model_started"):
                self.status = "running"
                self.current_turn = self._turn(data)
                self.current_action = "thinking"
                return True
            if kind in ("model_response", "model_completed"):
                self.status = "running"
                self.model_calls += 1
                self.model_calls_known = True
                self._add_usage(data.get("usage") or data)
                text_value = (
                    data.get("text") or data.get("content") or data.get("message")
                )
                if text_value and not self.answer:
                    self.answer = ui.strip_ansi(str(text_value))[:4000]
                if self.current_turn is None:
                    self.current_turn = self._turn(data)
                self.current_action = "reviewing model response"
                return True
            if kind == "tool_call":
                self.status = "running"
                self.current_turn = self._turn(data) or self.current_turn
                self.current_call_id = str(data.get("call_id") or data.get("id") or "")
                self.current_tool = str(
                    data.get("tool") or data.get("name") or ""
                ).lower()
                self.current_action = self._tool_action(data)
                arguments = self._tool_arguments(data)
                if self.current_tool in {
                    "edit",
                    "write",
                    "apply_patch",
                    "patch",
                    "rename",
                    "delete",
                }:
                    self._record_file_change(
                        arguments.get("path") or data.get("target"),
                        data,
                        tool=self.current_tool,
                        actor=str(data.get("actor") or "agent"),
                        reason=str(data.get("reason") or data.get("why") or ""),
                    )
                if self.current_tool:
                    self.visible_tools = sorted(
                        set([*self.visible_tools, self.current_tool])
                    )
                    if self.current_tool in {"mcp", "mcp_call"}:
                        self.mcp_calls += 1
                if (
                    self.current_tool
                    and not _mode_tool_allowed(self.mode, self.current_tool)
                    and self.current_tool not in self.blocked_tools
                ):
                    self.blocked_tools.append(self.current_tool)
                if self.current_tool in self.blocked_tools:
                    self.current_action += " (blocked by mode)"
                return True
            if kind in ("tool_result", "tool_completed"):
                if (
                    data.get("call_id")
                    and str(data.get("call_id")) == self.current_call_id
                ):
                    self.current_call_id = ""
                if data.get("ok") is False or data.get("error"):
                    self.last_error = ui.strip_ansi(
                        str(data.get("error") or data.get("output") or "tool failed")
                    )[:240]
                    self.current_action = "handling tool error"
                else:
                    self.current_action = "tool result received"
                return True
            if kind in ("tool_error", "tool_validation_error", "tool_recovery"):
                self.last_error = ui.strip_ansi(
                    str(
                        data.get("detail")
                        or data.get("error")
                        or data.get("reason")
                        or "tool error"
                    )
                )[:240]
                self.current_action = "handling tool error"
                return True
            if kind in ("edit_applied", "file_changed"):
                path = str(data.get("path") or data.get("target") or "")
                self._record_file_change(
                    path,
                    data,
                    tool="edit",
                    actor=str(data.get("actor") or "agent"),
                    reason=str(data.get("reason") or data.get("why") or ""),
                )
                self.current_action = f"editing {path}" if path else "editing a file"
                return True
            if kind in ("approval_required", "input_requested"):
                self.approval = "waiting"
                self.approval_scope = str(data.get("scope") or "")
                self.approval_effect = str(
                    data.get("exact_effect") or data.get("effect") or ""
                )
                self.current_action = (
                    f"waiting for approval ({data.get('tool') or 'input'})"
                )
                return True
            if kind in ("question_asked", "question_pending", "unresolved_question"):
                self._record_question(kind, data)
                return True
            if kind in (
                "question_answered",
                "question_resolved",
                "question_cleared",
            ):
                self._resolve_question(data)
                return True
            if kind in ("approval_decided", "permission_decision", "approval_denied"):
                self._apply_permission(data)
                return True
            if kind in (
                "verify",
                "verification",
                "final_verify",
                "project_final_verify",
            ):
                evidence = self._verification(kind, data)
                self.latest_verification = evidence
                self.verification_evidence.append(evidence)
                self.verification_status = verification_state(
                    self.verification_evidence
                )
                self._mark_file_verification(
                    verification_state(self.verification_evidence) == "verified"
                )
                self.status = "running"
                self.current_action = "verifying"
                if data.get("error"):
                    self.last_error = ui.strip_ansi(str(data["error"]))[:240]
                return True
            if kind in ("checkpoint_saved", "checkpoint", "project_checkpoint"):
                checkpoint = data.get("checkpoint")
                if not isinstance(checkpoint, Mapping):
                    checkpoint = data
                self.checkpoints.append(dict(checkpoint))
                self._record_checkpoint_files(checkpoint)
                self.resume_availability = str(
                    checkpoint.get("resume_availability") or "available"
                )
                self.current_action = "checkpoint saved"
                return True
            if kind in ("diagnostics", "lsp_diagnostics"):
                values = data.get("items") or data.get("diagnostics") or [data]
                self.diagnostics = [
                    normalize_diagnostic(item)
                    for item in values
                    if isinstance(item, Mapping)
                ]
                self.current_action = "checking diagnostics"
                return True
            if kind == "cancellation_requested":
                self.status = "cancelled"
                self.current_action = "cancelling"
                return True
            if kind in (
                "result",
                "run_finished",
                "run_finish",
                "task_end",
                "project_end",
                "completion_decision",
            ):
                result = data.get("result")
                if isinstance(result, Mapping):
                    self.result.update(dict(result))
                    data = {
                        **dict(result),
                        **{k: v for k, v in data.items() if k != "result"},
                    }
                terminal_evidence = data.get("verification_evidence")
                if isinstance(terminal_evidence, Mapping):
                    terminal_evidence = [terminal_evidence]
                if not isinstance(terminal_evidence, list):
                    terminal_evidence = data.get("verification")
                if isinstance(terminal_evidence, Mapping):
                    terminal_evidence = [terminal_evidence]
                for item in (
                    terminal_evidence if isinstance(terminal_evidence, list) else []
                ):
                    if isinstance(item, Mapping):
                        record = dict(item)
                        self.verification_evidence.append(record)
                        self.latest_verification = record
                terminal_files = data.get("changed_files") or data.get("files_touched")
                if isinstance(terminal_files, Mapping):
                    terminal_files = list(terminal_files.values())
                if not isinstance(terminal_files, (list, tuple, set)):
                    terminal_files = [terminal_files] if terminal_files else []
                self._add_file_list(terminal_files)
                for path in terminal_files:
                    self._record_file_change(path, data)
                status = data.get("status") or self.result.get("status")
                if data.get("aborted"):
                    status = "cancelled"
                self.status = effective_terminal_status(
                    status or self.status or "unknown",
                    self.verification_evidence,
                )
                self.verification_status = verification_state(
                    self.verification_evidence, self.status
                )
                self.resume_availability = str(
                    data.get("resume_availability")
                    or self.result.get("resume_availability")
                    or ""
                )
                if data.get("answer") is not None:
                    self.answer = ui.strip_ansi(str(data.get("answer") or ""))
                if data.get("cost_usd", data.get("cost")) is not None:
                    try:
                        cost = float(data.get("cost_usd", data.get("cost")))
                    except (TypeError, ValueError):
                        cost = -1.0
                    if cost >= 0.0 and _cost_is_priced(data, cost):
                        self.cost_usd = cost
                        self.cost_known = True
                        self.cost_source = str(data.get("cost_source") or "result")
                self.current_action = self.status
                if data.get("reason") or data.get("error"):
                    self.last_error = ui.strip_ansi(
                        str(data.get("reason") or data.get("error"))
                    )[:240]
                return True
            if kind in ("tool_call_detail", "tool_result_detail"):
                return bool(data)
            if kind in ("model_delta",):
                # A streamed chunk is the reasoning/answer surface arriving.
                # It is deliberately NOT counted as a model call: the
                # terminal `model_response` is the call, and counting both
                # would double every call count in a streamed run.
                self.status = "running"
                if self.stream_chars is None:
                    self.stream_chars = 0
                self.stream_chars += len(
                    str(data.get("delta") or data.get("text") or "")
                )
                self.current_action = "streaming response"
                return True
            if kind in INFORMATIONAL_EVENTS:
                return False
            # An event this projection cannot render is recorded, not
            # swallowed. A run whose vocabulary has outgrown the terminal is
            # a real finding; reporting `events: N` with an empty warning
            # list would claim the surface understood all of them.
            self.unmapped_kinds[kind] = self.unmapped_kinds.get(kind, 0) + 1
            return False
        except Exception:
            return False

    def _apply_permission(self, data: Dict[str, Any]) -> None:
        """Fold one permission decision into the tri-state approval view.

        A permission engine answers with THREE values, not two: `allow`,
        `ask`, and `deny`. Collapsing `ask` into `deny` is how a run that is
        correctly WAITING for a human gets rendered as refused - and the
        policy's own reason string ("default policy") then persists as the
        run's error for the rest of the run. A reason attached to an `ask` is
        the explanation of what needs approving, not a failure.
        """
        action = str(data.get("action") or data.get("decision") or "").strip().lower()
        approved = data.get("approved")
        if approved is None:
            if action in {"allow", "allowed", "approve", "approved", "permit"}:
                outcome = "approved"
            elif action in {
                "ask",
                "needs_approval",
                "needs_input",
                "prompt",
                "pending",
                "waiting",
            }:
                outcome = "waiting"
            elif action in {"deny", "denied", "refuse", "refused", "blocked", "reject"}:
                outcome = "rejected"
            else:
                # No readable decision: the honest reading is that the
                # terminal did not say, not that the user refused.
                outcome = "waiting" if data.get("needs_approval") else "unknown"
        else:
            outcome = "approved" if _evidence_bool(approved) else "rejected"
        self.approval = outcome
        self.approval_scope = str(data.get("scope") or "")
        self.approval_effect = str(data.get("exact_effect") or data.get("effect") or "")
        note = str(data.get("reason") or "")
        if outcome == "waiting":
            self.approval_note = note[:240]
            self.current_action = "waiting for approval"
            if not self.approval_effect:
                self.approval_effect = note[:240]
            return
        if outcome == "rejected":
            self.current_action = "approval rejected"
            detail = str(data.get("error") or data.get("reason") or "approval rejected")
            self.last_error = ui.strip_ansi(detail)[:240]
            return
        if outcome == "unknown":
            self.current_action = "approval decision not reported"
            return
        self.current_action = "approval granted"

    def _add_usage(self, usage: Any) -> None:
        """Add one provider usage receipt using all supported aliases.

        Cost is only marked KNOWN when the receipt says it was priced. A
        gateway that had no price table for the model records
        ``cost_usd: 0.0``; treating that as a measurement renders
        ``$0.0000`` for a run nobody has costed, which reads exactly like
        "this was free". The rule is the one `model_call_receipts` already
        applies to the router ledger, so the conversation and the ledger
        cannot disagree about what "priced" means.
        """
        if not isinstance(usage, Mapping):
            return
        token_value = usage.get(
            "tokens", usage.get("total_tokens", usage.get("completion_tokens"))
        )
        cost_value = usage.get("cost", usage.get("cost_usd", usage.get("usd")))
        if token_value is not None:
            self.tokens_known = True
            try:
                self.tokens += max(0, int(token_value or 0))
            except (TypeError, ValueError):
                pass
        if cost_value is not None:
            try:
                cost = max(0.0, float(cost_value or 0.0))
            except (TypeError, ValueError):
                return
            self.cost_usd += cost
            if _cost_is_priced(usage, cost):
                self.cost_known = True
                if not self.cost_source:
                    self.cost_source = str(usage.get("cost_source") or "receipt")
            elif not self.cost_known:
                self.cost_source = "unpriced"

    def _turn(self, data: Dict[str, Any]) -> Optional[int]:
        for key in ("turn", "turn_index", "iteration", "step"):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                match = re.search(r"(?:agent-|step-)(\d+)", value)
                if match:
                    return int(match.group(1))
                if value.isdigit():
                    return int(value)
        return None

    def _tool_arguments(self, data: Dict[str, Any]) -> Dict[str, Any]:
        arguments = data.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = data.get("args")
        return dict(arguments) if isinstance(arguments, Mapping) else {}

    def _tool_action(self, data: Dict[str, Any]) -> str:
        tool = str(data.get("tool") or data.get("name") or "").lower()
        args = self._tool_arguments(data)
        target = str(
            args.get("path")
            or args.get("pattern")
            or args.get("query")
            or args.get("command")
            or data.get("target")
            or ""
        )
        if tool in ("done", "finish"):
            return "finishing"
        if tool == "read":
            return f"reading {target}" if target else "reading a file"
        if tool in ("glob", "grep", "search"):
            return f"searching {target}" if target else "searching the repository"
        if tool in ("bash", "shell", "test"):
            return f"running {target}" if target else "running a command"
        if tool in ("edit", "write", "apply_patch"):
            return f"editing {target}" if target else "editing a file"
        if tool in ("mcp", "mcp_call"):
            return f"calling MCP {target}" if target else "calling an MCP tool"
        if tool == "memory":
            return "recalling project memory"
        if tool == "verify":
            return "verifying"
        if tool == "fetch":
            return "fetching reference"
        if tool == "plan":
            return "planning"
        return tool or "using a tool"

    def _verification(self, kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
        evidence = data.get("evidence")
        if isinstance(evidence, Mapping):
            data = {
                **dict(evidence),
                **{k: v for k, v in data.items() if k != "evidence"},
            }
        raw = ui.strip_ansi(
            str(data.get("raw") or data.get("output") or data.get("error") or "")
        )
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return {
            "kind": "verification" if kind == "verification" else kind,
            "target_passed": data.get("target_passed", data.get("target_test_passed")),
            "regression_passed": data.get("regression_passed"),
            "flaky": _evidence_bool(data.get("flaky")),
            "summary": ui.strip_ansi(lines[-1] if lines else data.get("summary"))[:120],
            "raw": raw[-2000:],
            "evidence": evidence if isinstance(evidence, list) else [],
        }

    def _record_file_change(
        self,
        value: Any,
        data: Optional[Mapping[str, Any]] = None,
        *,
        tool: str = "",
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Record mutation provenance and checkpoint membership for one path."""
        path = ui.strip_ansi(str(value or "")).replace("\\", "/").strip()
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            return
        payload = dict(data or {})
        current = self.file_changes.setdefault(
            path,
            {
                "path": path,
                "actor": actor or "run",
                "reason": reason or "",
                "verified": False,
                "verification_state": "not_run",
                "undoable": False,
                "checkpoint_ids": [],
                "staged": False,
                "unstaged": True,
                "source": "journal",
            },
        )
        if actor:
            current["actor"] = actor
        if (reason or payload.get("reason") or payload.get("why")) and (
            not current.get("reason") or tool
        ):
            current["reason"] = ui.strip_ansi(
                reason or payload.get("reason") or payload.get("why")
            )
        if tool:
            current["last_tool"] = tool
            current["undoable"] = bool(payload.get("undoable", True))
        if payload.get("actor") or payload.get("owner"):
            current["actor"] = str(payload.get("actor") or payload.get("owner"))
        if payload.get("undoable") is not None:
            current["undoable"] = bool(payload.get("undoable"))
        if payload.get("checkpoint_id") or payload.get("resume_token"):
            checkpoint_id = str(
                payload.get("checkpoint_id") or payload.get("resume_token")
            )
            if checkpoint_id not in current["checkpoint_ids"]:
                current["checkpoint_ids"].append(checkpoint_id)
        for key in ("additions", "deletions", "status", "kind"):
            if payload.get(key) is not None:
                current[key] = payload[key]
        if "verified" in payload:
            current["verified"] = bool(payload.get("verified"))
            current["verification_state"] = str(
                payload.get("verification_state")
                or ("verified" if current["verified"] else "not_run")
            )
        elif tool:
            current["verified"] = False
            current["verification_state"] = "not_run"
        self._add_file(path)

    def _mark_file_verification(self, verified: bool) -> None:
        """Apply one clean verification receipt to the current mutation set."""
        state = "verified" if verified else "unknown"
        for value in self.file_changes.values():
            value["verified"] = bool(verified)
            value["verification_state"] = state

    def _record_checkpoint_files(self, checkpoint: Mapping[str, Any]) -> None:
        """Associate checkpoint-owned paths with their journal checkpoint id."""
        identifier = str(
            checkpoint.get("checkpoint_id")
            or checkpoint.get("resume_token")
            or checkpoint.get("last_event_sequence")
            or ""
        )
        if not identifier:
            return
        values = (
            checkpoint.get("agent_owned_changes")
            or checkpoint.get("captured_paths")
            or checkpoint.get("files")
            or []
        )
        if isinstance(values, Mapping):
            values = list(values.values())
        for value in values if isinstance(values, (list, tuple, set)) else []:
            path = (
                value.get("path") or value.get("file")
                if isinstance(value, Mapping)
                else value
            )
            self._record_file_change(path, checkpoint, reason="checkpoint capture")
            self._checkpoint_files[
                ui.strip_ansi(str(path or "")).replace("\\", "/").strip()
            ] = identifier

    def _add_file(self, value: Any) -> None:
        path = ui.strip_ansi(str(value or "")).replace("\\", "/").strip()
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            return
        if path not in self.changed_files:
            self.changed_files.append(path)

    def _add_file_list(self, values: Any) -> None:
        """Add a bounded list of changed-file paths."""
        if isinstance(values, (list, tuple, set)):
            for value in values:
                self._add_file(value)

    def _terminal_status(self, value: str) -> str:
        return effective_terminal_status(value, self.verification_evidence)

    def _record_question(self, kind: str, data: Dict[str, Any]) -> None:
        """Fold one question row into the pending-question list.

        A run may ask more than once, so a question is keyed by its id
        when it has one and by its text otherwise. Re-asking the same
        question replaces the old row rather than stacking a duplicate,
        because two identical pending rows would read as "the model
        asked twice".
        """
        text = ui.strip_ansi(str(data.get("question") or data.get("text") or ""))
        if not text:
            text = "the run is waiting for an answer"
        ident = str(data.get("question_id") or data.get("id") or text)
        for existing in self.questions:
            if existing.get("id") == ident:
                existing.update({"text": text, "status": "waiting", "kind": kind})
                self.current_action = "waiting for an answer"
                return
        self.questions.append(
            {"id": ident, "text": text, "status": "waiting", "kind": kind}
        )
        self.current_action = "waiting for an answer"

    def _resolve_question(self, data: Dict[str, Any]) -> None:
        """Mark a question answered, or clear every question if unnamed."""
        ident = str(data.get("question_id") or data.get("id") or "")
        answered = str(data.get("answer") or data.get("response") or "")
        matched = False
        for existing in self.questions:
            if not ident or existing.get("id") == ident:
                existing["status"] = "answered"
                existing["answer_preview"] = answered[:120]
                matched = True
        if matched and not any(
            row.get("status") == "waiting" for row in self.questions
        ):
            self.current_action = "continuing"

    def pending_question(self) -> Optional[Dict[str, Any]]:
        """The first unanswered question, or ``None``.

        ``None`` is a real answer, not "unknown": the composer's gate
        turns on whether a question is pending, and a projection that
        cannot say must not make the user type into a box that ignores
        them.
        """
        for row in self.questions:
            if str(row.get("status")) == "waiting":
                return dict(row)
        return None

    def mark_reconnect(self, reason: str = "event stream reconnected") -> None:
        """Record a reconnect without discarding the validated cursor."""
        self._cursor.mark_reconnect(reason)
        self.warnings = list(self._cursor.warnings)
        self.current_action = reason

    def reset_stream(self) -> None:
        """Reset journal-derived state after a trace rotation."""
        self._cursor = EventCursor(self.task_id)
        self._last_accepted = []
        self.events = 0
        self.raw_events = 0
        self.status = "queued"
        self.current_action = "reconnecting to event journal"
        self.current_tool = ""
        self.current_call_id = ""
        self.current_turn = None
        self.changed_files = []
        self.file_changes = {}
        self.issue = ""
        self.repo_path = ""
        self._checkpoint_files = {}
        self.latest_verification = None
        self.verification_evidence = []
        self.verification_status = "not_run"
        self.approval = "not required"
        self.approval_scope = ""
        self.approval_effect = ""
        self.last_error = ""
        self.elapsed_s = None
        self.model_calls = 0
        self.model_calls_known = False
        self.tokens = 0
        self.tokens_known = False
        self.cost_usd = 0.0
        self.cost_known = False
        self.cost_source = ""
        self.answer = ""
        self.approval_note = ""
        self.stream_chars = None
        self.project_id = ""
        self.unmapped_kinds = {}
        self.session_id = ""
        self.run_id = ""
        self.turn_id = ""
        self.strategy = ""
        self.context = {}
        self.checkpoints = []
        self.diagnostics = []
        self.subagents = []
        self.visible_tools = []
        self.mcp_calls = 0
        self.blocked_tools = []
        self.resume_availability = ""
        self.resumed = False
        self.result = {}
        self.warnings = []
        self._warning_revision = 0
        self._first_ts = None
        self._last_ts = None

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Return a JSON-friendly copy of the current projection."""
        elapsed = self.elapsed_s
        if elapsed is None and self._started_at is not None:
            current = time.time() if now is None else float(now)
            elapsed = round(max(0.0, current - self._started_at), 1)
        cursor = self._cursor.snapshot()
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "projection_mode": self.mode,
            "mode_label": MODE_PROJECTIONS[self.mode]["label"],
            "status": self.status,
            "current_action": self.current_action,
            "current_tool": self.current_tool,
            "current_call_id": self.current_call_id,
            "current_turn": self.current_turn,
            "changed_files": list(self.changed_files),
            "file_changes": [dict(value) for value in self.file_changes.values()],
            "issue": self.issue,
            "repo_path": self.repo_path,
            "latest_verification": dict(self.latest_verification or {}),
            "verification_evidence": [
                dict(item) for item in self.verification_evidence
            ],
            "verification_state": self.verification_status
            or verification_state(self.verification_evidence, self.status),
            "approval": self.approval,
            "approval_scope": self.approval_scope,
            "approval_effect": self.approval_effect,
            "approval_note": self.approval_note,
            "questions": [dict(item) for item in self.questions],
            "pending_question": dict(self.pending_question() or {})
            if self.questions
            else {},
            "last_error": self.last_error,
            "elapsed_s": elapsed,
            "model_calls": self.model_calls,
            "model_calls_known": self.model_calls_known,
            "tokens": self.tokens,
            "tokens_known": self.tokens_known,
            "cost_usd": round(self.cost_usd, 6),
            "cost_known": self.cost_known,
            "cost_source": self.cost_source,
            "stream_chars": self.stream_chars,
            "project_id": self.project_id,
            "unmapped_kinds": sorted(self.unmapped_kinds),
            "unmapped_events": sum(self.unmapped_kinds.values()),
            "usage_known": {
                "calls": self.model_calls_known,
                "tokens": self.tokens_known,
                "cost": self.cost_known,
            },
            "answer": self.answer,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "strategy": self.strategy,
            "context": dict(self.context),
            "checkpoints": [dict(item) for item in self.checkpoints],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "subagents": [dict(item) for item in self.subagents],
            "visible_tools": list(self.visible_tools),
            "mcp_calls": self.mcp_calls,
            "blocked_tools": list(self.blocked_tools),
            "resume_availability": self.resume_availability,
            "resumed": self.resumed,
            "result": dict(self.result),
            "events": self.events,
            "raw_events": self.raw_events,
            "last_sequence": cursor["last_sequence"],
            "pending_sequences": cursor["pending_sequences"],
            "warnings": cursor["warnings"],
            "duplicate_count": cursor["duplicate_count"],
            "out_of_order_count": cursor["out_of_order_count"],
            "reconnect_count": cursor["reconnect_count"],
            "stream_connected": cursor["connected"],
        }


def _checkpoint_identity(record: Mapping[str, Any]) -> Tuple[Any, Any, Any]:
    """Return the stable identity of one checkpoint receipt.

    A kernel checkpoint names a resume token; a legacy fix-loop checkpoint
    is identified by the event sequence it was cut at. Falling back to the
    creation timestamp LAST matters: the two readers of the same run
    disagree about whether that field is populated, so keying on it first
    would report one checkpoint twice.
    """
    token = record.get("resume_token")
    if token:
        return ("token", token, "")
    sequence = record.get("last_event_sequence")
    if sequence is not None:
        return ("sequence", sequence, "")
    return ("created", record.get("created_at"), record.get("path"))


def _merge_checkpoints(
    journal_records: List[Dict[str, Any]],
    file_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Union two checkpoint views instead of letting one silently win.

    The folded projection and `read_checkpoints` are two readers over the
    same run, so they must AGREE rather than one overwriting the other. The
    earlier code replaced the journal's records with the reader's, which
    silently dropped every checkpoint a build/project journal recorded
    (its event name is `project_checkpoint`). Unioning means a future
    vocabulary gap between the two readers degrades to "more facts", never
    to "fewer".
    """
    merged: List[Dict[str, Any]] = []
    index: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    for record in [*journal_records, *file_records]:
        if not isinstance(record, Mapping):
            continue
        key = _checkpoint_identity(record)
        existing = index.get(key)
        if existing is None:
            copy = dict(record)
            index[key] = copy
            merged.append(copy)
            continue
        for field, value in record.items():
            if field not in existing or existing.get(field) in (None, "", [], {}):
                existing[field] = value
    return merged


def read_live_projection(
    log_dir: Path,
    mode: str = "agent_task",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Read a live projection from a task directory without mutating it."""
    root = Path(log_dir)
    projection = RunProjection(root.name, mode=mode, started_at=now)
    try:
        trace = root / "trace.jsonl"
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            projection.consume(event)
    except OSError:
        pass
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        files = state.get("files_touched") if isinstance(state, dict) else None
        if isinstance(files, list):
            for path in files:
                projection._add_file(path)
    except (OSError, ValueError):
        pass
    projection.checkpoints = _merge_checkpoints(
        projection.checkpoints, read_checkpoints(root)
    )
    projection.diagnostics = read_diagnostics(root)
    context = read_context_receipt(root)
    if context:
        projection.context.update(context)
    return projection.snapshot(now=now)


def read_agent_projection(log_dir: Path) -> Dict[str, Any]:
    """Compatibility alias for the agent-native live projection."""
    return read_live_projection(log_dir, mode="agent_task")


# ---------------------------------------------------------------------------
# The state-machine state (Task B) — transitions.jsonl, the documented
# live-status surface (harness/state_machine.py's reading helpers).
# ---------------------------------------------------------------------------


def read_machine_state(log_dir: Path) -> Optional[str]:
    """The task's current state-machine state from its audit trail.

    Reads logs/{task_id}/transitions.jsonl and returns the LAST record
    that carries a to_state and is not marked invalid — the same
    contract as harness.state_machine.current_phase, minus its blind
    spot: steering rounds append {"event": ...} records WITHOUT a
    to_state (they deliberately keep the phase unchanged), and a reader
    that returns the first valid record's field would surface the
    string "None" for them. Missing/unreadable file -> None ("no state
    yet", never an error).
    """
    try:
        text = (Path(log_dir) / "transitions.jsonl").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("valid") is False:
            continue
        to = rec.get("to_state")
        if to:
            return str(to)
    return None


def read_checkpoints(log_dir: Path) -> List[Dict[str, Any]]:
    """Return checkpoint receipts from a run directory and runtime sibling.

    The journal event is preferred when present; a kernel or runtime
    checkpoint file is used as a compatibility fallback. Missing or
    malformed artifacts return an empty list and never raise.
    """
    root = Path(log_dir)
    records: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        lines = (
            (root / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data, timestamp, _identity = event_parts(event)
        if kind not in ("checkpoint_saved", "checkpoint", "project_checkpoint"):
            continue
        value = data.get("checkpoint")
        record = dict(value) if isinstance(value, Mapping) else dict(data)
        record.setdefault("source", "journal")
        record.setdefault("created_at", timestamp)
        key = (
            record.get("last_event_sequence"),
            record.get("resume_token"),
            record.get("created_at"),
        )
        if key not in seen:
            seen.add(key)
            records.append(record)
    candidates = [
        root / "checkpoint.json",
        root.parent / f"{root.name}.runtime" / "checkpoint.json",
    ]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(value, Mapping):
            continue
        record = dict(value)
        record.setdefault("source", str(path))
        key = (
            record.get("last_event_sequence"),
            record.get("resume_token"),
            record.get("created_at"),
        )
        if key not in seen:
            seen.add(key)
            records.append(record)
    records.sort(key=lambda item: float(item.get("created_at") or 0.0))
    return records


def normalize_diagnostic(value: Any) -> Dict[str, Any]:
    """Normalize journal and LSP diagnostic shapes to one linkable record."""
    if not isinstance(value, Mapping):
        return {
            "path": "",
            "file": "",
            "line": 1,
            "column": 1,
            "end_line": 1,
            "end_column": 1,
            "severity": "error",
            "message": str(value or ""),
            "source": "",
            "code": "",
            "link": "",
        }
    raw_path = value.get("path") or value.get("file") or value.get("uri") or ""
    path = ui.strip_ansi(str(raw_path)).replace("\\", "/")
    lsp_shape = "file" in value and "path" not in value and "column" in value
    raw_line = value.get("line", value.get("row", 1))
    try:
        line = int(raw_line) + (1 if lsp_shape else 0)
    except (TypeError, ValueError):
        line = 1
    try:
        column = int(value.get("column", value.get("char", 0))) + (
            1 if lsp_shape else 0
        )
    except (TypeError, ValueError):
        column = 1
    line = max(1, line)
    column = max(1, column)
    try:
        end_line = int(value.get("end_line", raw_line)) + (1 if lsp_shape else 0)
    except (TypeError, ValueError):
        end_line = line
    try:
        end_column = int(value.get("end_column", 0)) + (1 if lsp_shape else 0)
    except (TypeError, ValueError):
        end_column = column
    result = {
        "path": path,
        "file": path,
        "line": line,
        "column": column,
        "end_line": max(line, end_line),
        "end_column": max(column, end_column),
        "severity": str(value.get("severity") or "error").lower(),
        "message": ui.strip_ansi(
            str(value.get("message") or value.get("detail") or "")
        ).strip(),
        "source": str(value.get("source") or ""),
        "code": value.get("code"),
        "link": "",
    }
    result["link"] = f"{path}:{line}:{column}" if path else ""
    return result


def read_diagnostics(log_dir: Path) -> List[Dict[str, Any]]:
    """Return diagnostic receipts projected from the run journal."""
    root = Path(log_dir)
    values: List[Dict[str, Any]] = []
    try:
        lines = (
            (root / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data, _timestamp, _identity = event_parts(event)
        if kind not in ("diagnostics", "lsp_diagnostics"):
            continue
        items = data.get("items") or data.get("diagnostics")
        if isinstance(items, list):
            values.extend(
                normalize_diagnostic(item)
                for item in items
                if isinstance(item, Mapping)
            )
        else:
            values.append(normalize_diagnostic(data))
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[str, int, int, str]] = set()
    for value in values:
        key = (
            value.get("path", ""),
            int(value.get("line", 1)),
            int(value.get("column", 1)),
            value.get("message", ""),
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def read_context_receipt(log_dir: Path) -> Dict[str, Any]:
    """Return the latest bounded context receipt from a run journal."""
    latest: Dict[str, Any] = {}
    try:
        lines = (
            (Path(log_dir) / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return latest
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data, _timestamp, _identity = event_parts(event)
        if kind in ("context", "context_built", "context_compiled", "session_context"):
            latest.update(dict(data))
    return latest


def read_context_meter(log_dir: Path) -> Dict[str, Any]:
    """Return the run's prompt-token context meter and its compaction history.

    Two sources, in precedence order, because both can be present:

    * ``context.json`` - the kernel's per-run artifact, rewritten atomically on
      every prepared request and once more at completion.
    * the journal's own ``context_budget`` / ``context_compacted`` /
      ``context_rewind`` rows - so a run whose artifact is missing or truncated
      still reports a real measurement instead of an empty dict.

    Returns ``{}`` for a run that never measured a context (a legacy run), and
    never raises: this is a read-only view layer.
    """
    log_dir = Path(log_dir)
    meter: Dict[str, Any] = {}
    try:
        raw = json.loads((log_dir / "context.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        meter = dict(raw)
    samples: List[Dict[str, Any]] = []
    compactions: List[Dict[str, Any]] = []
    rewinds: List[Dict[str, Any]] = []
    try:
        lines = (
            (log_dir / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, data, _timestamp, _identity = event_parts(event)
        if kind == "context_budget":
            samples.append(dict(data))
        elif kind == "context_compacted":
            compactions.append(dict(data))
        elif kind == "context_rewind":
            rewinds.append(dict(data))
    result: Dict[str, Any] = dict(meter)
    if samples:
        latest = dict(samples[-1])
        result["meter"] = {
            **(result.get("meter") or {}),
            **{key: latest.get(key) for key in latest if key != "stage"},
        }
        utilizations = []
        for sample in samples:
            try:
                utilizations.append(float(sample.get("utilization") or 0.0))
            except (TypeError, ValueError):
                continue
        if utilizations:
            ordered = sorted(utilizations)
            index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
            result["utilization_p95"] = round(ordered[index], 6)
            result["utilization_max"] = round(ordered[-1], 6)
        result["samples"] = len(samples)
    if compactions:
        result["compactions"] = list(meter.get("compactions") or []) or compactions
        result["compaction_count"] = len(compactions)
    if rewinds:
        result["rewinds"] = rewinds
        result["rewind_count"] = len(rewinds)
    return result


def read_rewind_targets(log_dir: Path) -> List[Dict[str, Any]]:
    """Return the turns a rewind picker can offer, newest first.

    The picker must not invent a target: every row comes from the run's own
    ``turns.jsonl``, the same append-only ledger a resume reads. Each entry
    carries the tools that ran, the files that turn changed, and whether the
    journal still holds a checkpoint at or after that turn - so a UI can say
    "rewinding here also rewinds the checkpoint" instead of guessing.
    """
    targets: List[Dict[str, Any]] = []
    try:
        text = (Path(log_dir) / "turns.jsonl").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return targets
    files: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            turn = int(row.get("turn") or 0)
        except (TypeError, ValueError):
            continue
        if turn <= 0:
            continue
        tools = [
            str(item.get("tool") or "")
            for item in row.get("tool_calls") or ()
            if isinstance(item, Mapping) and item.get("tool")
        ]
        changed = [str(name) for name in row.get("changed_files") or ()]
        for name in changed:
            if name not in files:
                files.append(name)
        targets.append(
            {
                "turn": turn,
                "status": str(row.get("status") or "in_progress"),
                "tools": tools,
                "changed_files": changed,
                "event_sequence": int(row.get("event_sequence") or 0),
                "spend_usd": float(row.get("spend_usd") or 0.0),
                "message_count": int(row.get("message_count") or 0),
                "dropped_messages": int(row.get("dropped_messages") or 0),
                "files_so_far": list(files),
            }
        )
    targets.reverse()
    return targets


# ---------------------------------------------------------------------------
# Completion-card facts (Task C) — re-derived from the run's own records
# ---------------------------------------------------------------------------


def read_run_facts(log_dir: Path) -> Dict[str, Any]:
    """The numbers for the completion card, from the run's own files.

    Sources: trace.jsonl's result / final_verify / git_output /
    model_response / task_start events (ts span = time taken) and
    state.json's files_touched. A missing file or malformed line leaves
    the corresponding field empty — read_run_facts never raises, and it
    never writes anything. Assumes log_dir is logs/{task_id}/.
    """
    log_dir = Path(log_dir)
    facts: Dict[str, Any] = {
        "task_id": log_dir.name,
        "status": None,
        "display_status": "unknown",
        "verification_state": "not_run",
        "reason": "",
        "attempts": None,
        "cost_usd": None,
        "model_calls": 0,
        "model_calls_known": False,
        "tokens": 0,
        "tokens_known": False,
        "cost_known": False,
        "elapsed_s": None,
        "issue": "",
        "mode": None,
        "current_action": "",
        "current_tool": "",
        "current_call_id": "",
        "current_turn": None,
        "changed_files": [],
        "file_changes": [],
        "repo_path": "",
        "latest_verification": None,
        "verification_evidence": [],
        "approval": "not required",
        "approval_scope": "",
        "approval_effect": "",
        "last_error": "",
        "answer": "",
        "session_id": "",
        "run_id": "",
        "turn_id": "",
        "strategy": "",
        "context": {},
        "context_meter": {},
        "checkpoints": [],
        "diagnostics": [],
        "resume_availability": "",
        "resumed": False,
        "target_passed": None,
        "regression_passed": None,
        "flaky": None,
        "verify_summary": "",
        "files": [],
        "branch": "",
        "commit_sha": "",
    }
    events: List[Dict[str, Any]] = []
    try:
        for line in (
            (log_dir / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        ):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    except OSError:
        events = []

    ts_vals: List[float] = []
    usage_cost = 0.0
    saw_result = False
    projection = RunProjection(log_dir.name, mode="fix")
    ordered_events: List[Dict[str, Any]] = []
    for ev in events:
        projection.consume(ev)
        ordered_events.extend(projection.last_accepted_events)
    for ev in ordered_events:
        kind, data, timestamp, identity = event_parts(ev)
        if timestamp:
            ts_vals.append(timestamp)
        if identity.get("session_id"):
            facts["session_id"] = str(identity["session_id"])
        if identity.get("run_id"):
            facts["run_id"] = str(identity["run_id"])
        if identity.get("turn_id"):
            facts["turn_id"] = str(identity["turn_id"])
        if kind in ("run_started", "task_start"):
            if not facts["issue"]:
                issue = str(data.get("issue_text") or data.get("request") or "")
                if not issue:
                    run_spec = data.get("run_spec")
                    if isinstance(run_spec, Mapping):
                        issue = str(run_spec.get("request") or "")
                facts["issue"] = (
                    ui.strip_ansi(issue).splitlines()[0][:90] if issue else ""
                )
            facts["repo_path"] = str(
                data.get("repo_path")
                or data.get("repository")
                or facts.get("repo_path")
                or ""
            )
            run_spec = data.get("run_spec")
            if isinstance(run_spec, Mapping) and not facts["repo_path"]:
                facts["repo_path"] = str(
                    run_spec.get("repo_path") or run_spec.get("repository") or ""
                )
            spec_mode = ""
            if isinstance(run_spec, Mapping):
                metadata = run_spec.get("metadata")
                if isinstance(metadata, Mapping):
                    spec_mode = str(
                        metadata.get("mode") or metadata.get("agent_mode") or ""
                    )
            if facts["mode"] is None:
                facts["mode"] = spec_mode or data.get("mode")
            facts["strategy"] = str(data.get("strategy") or facts["strategy"])
        elif kind == "strategy_selected":
            facts["strategy"] = str(data.get("strategy") or data.get("name") or "")
        elif kind in (
            "context",
            "context_built",
            "context_compiled",
            "session_context",
        ):
            facts["context"].update(dict(data))
        elif kind == "context_budget":
            # The live per-request measurement: a surface can render the current
            # fill level without waiting for the run to finish.
            measured = dict(data)
            measured.pop("categories", None)
            facts["context_meter"] = {
                **(facts.get("context_meter") or {}),
                **measured,
            }
        elif kind == "context_compacted":
            meter = dict(facts.get("context_meter") or {})
            meter["last_compaction"] = {
                key: data.get(key)
                for key in (
                    "compaction_id",
                    "method",
                    "turn",
                    "before_tokens",
                    "after_tokens",
                    "reclaimed_tokens",
                    "dropped_messages",
                    "fallback_model",
                )
            }
            facts["context_meter"] = meter
        elif kind in ("model_response", "model_completed"):
            facts["model_calls"] += 1
            usage = data.get("usage") or data
            tokens = usage.get(
                "tokens", usage.get("total_tokens", usage.get("completion_tokens", 0))
            )
            cost = usage.get("cost", usage.get("cost_usd", usage.get("usd", 0.0)))
            try:
                facts["tokens"] += int(tokens or 0)
            except (TypeError, ValueError):
                pass
            try:
                usage_cost += float(cost or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind in ("verify", "verification", "final_verify"):
            evidence = projection._verification(kind, data)
            facts["latest_verification"] = evidence
            facts["verification_evidence"].append(evidence)
            facts["target_passed"] = evidence.get("target_passed")
            facts["regression_passed"] = evidence.get("regression_passed")
            facts["flaky"] = evidence.get("flaky")
            facts["verify_summary"] = evidence.get("summary") or facts["verify_summary"]
        elif kind == "git_output":
            facts["branch"] = str(data.get("branch") or "")
            facts["commit_sha"] = str(data.get("commit_sha") or "")
        elif kind in ("checkpoint_saved", "checkpoint"):
            checkpoint = data.get("checkpoint")
            facts["checkpoints"].append(
                dict(checkpoint) if isinstance(checkpoint, Mapping) else dict(data)
            )
        elif kind in ("diagnostics", "lsp_diagnostics"):
            values = data.get("items") or data.get("diagnostics") or [data]
            facts["diagnostics"] = [
                normalize_diagnostic(item)
                for item in values
                if isinstance(item, Mapping)
            ]
        elif kind in ("result", "run_finished", "task_end", "completion_decision"):
            nested = data.get("result")
            if isinstance(nested, Mapping):
                data = {
                    **dict(nested),
                    **{k: v for k, v in data.items() if k != "result"},
                }
            if kind == "result" or kind == "run_finished" or data.get("status"):
                saw_result = saw_result or kind in ("result", "run_finished")
                facts["status"] = str(data.get("status") or facts["status"] or "")
                try:
                    facts["attempts"] = int(data.get("attempts", data.get("attempt")))
                except (TypeError, ValueError):
                    pass
                try:
                    cost = float(data.get("cost_usd", data.get("cost")))
                except (TypeError, ValueError):
                    cost = None
                if cost is not None:
                    facts["cost_usd"] = cost
                if data.get("answer"):
                    facts["answer"] = ui.strip_ansi(str(data["answer"]))
                if data.get("reason") or data.get("error"):
                    facts["reason"] = ui.strip_ansi(
                        data.get("reason") or data.get("error")
                    )[:120]
    if not saw_result and facts["cost_usd"] is None and usage_cost:
        facts["cost_usd"] = round(usage_cost, 6)
    if ts_vals:
        facts["elapsed_s"] = round(max(0.0, ts_vals[-1] - ts_vals[0]), 1)
    snapshot = projection.snapshot()
    for key in (
        "current_action",
        "current_tool",
        "current_call_id",
        "current_turn",
        "changed_files",
        "file_changes",
        "issue",
        "repo_path",
        "latest_verification",
        "verification_evidence",
        "approval",
        "approval_scope",
        "approval_effect",
        "last_error",
        "answer",
        "session_id",
        "run_id",
        "turn_id",
        "strategy",
        "context",
        "checkpoints",
        "diagnostics",
        "resume_availability",
        "resumed",
    ):
        if snapshot.get(key) not in (None, "", [], {}):
            facts[key] = snapshot[key]
    if facts["cost_usd"] is None and snapshot.get("cost_known"):
        facts["cost_usd"] = snapshot["cost_usd"]
    if not facts["tokens"]:
        facts["tokens"] = int(snapshot.get("tokens") or 0)
    if not facts["model_calls"]:
        facts["model_calls"] = int(snapshot.get("model_calls") or 0)
    if projection.events:
        projected_status = str(snapshot.get("status") or "unknown")
        terminal_projection = projected_status in {
            "completed_verified",
            "completed_unverified",
            "needs_input",
            "blocked",
            "failed",
            "cancelled",
            "timeout",
        }
        facts["status"] = (
            str(facts.get("status") or projected_status)
            if terminal_projection
            else projected_status
        )
        facts["mode"] = snapshot.get("mode") or facts.get("mode")
    facts["display_status"] = effective_terminal_status(
        facts.get("status"),
        snapshot.get("verification_evidence") or facts["verification_evidence"],
    )
    facts["verification_state"] = snapshot.get(
        "verification_state"
    ) or verification_state(
        snapshot.get("verification_evidence") or facts["verification_evidence"],
        facts.get("status"),
    )
    facts["latest_verification"] = snapshot.get("latest_verification")
    facts["verification_evidence"] = snapshot.get("verification_evidence") or []
    facts["model_calls_known"] = bool(snapshot.get("model_calls_known"))
    facts["tokens_known"] = bool(snapshot.get("tokens_known"))
    facts["cost_known"] = bool(snapshot.get("cost_known"))
    facts["last_sequence"] = snapshot.get("last_sequence", 0)
    facts["warnings"] = snapshot.get("warnings", [])
    try:
        state = json.loads((log_dir / "state.json").read_text(encoding="utf-8"))
        files = state.get("files_touched") if isinstance(state, dict) else None
        if isinstance(files, list):
            facts["files"] = [str(f) for f in files]
            for path in facts["files"]:
                projection._record_file_change(
                    path,
                    {"source": "state", "undoable": False},
                )
            facts["changed_files"] = list(projection.changed_files)
            facts["file_changes"] = [
                dict(value) for value in projection.file_changes.values()
            ]
    except (OSError, ValueError):
        pass
    if not facts.get("context_meter"):
        # A run whose journal predates the budget rows still has the artifact.
        facts["context_meter"] = read_context_meter(log_dir)
    return facts


def fmt_elapsed(secs: Optional[float]) -> str:
    """Seconds -> a compact human duration (34s / 9m 27s / 1h 03m)."""
    if secs is None:
        return ""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s:02d}s"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


def context_meter_line(meter: Mapping[str, Any]) -> str:
    """Render a run's context meter as one short status line.

    Returns "" when the run never measured a context, so a legacy run simply
    drops the row rather than showing a fabricated zero. The line is the meter's
    own numbers - fill level, peak, compactions - never an estimate made here.
    """
    if not isinstance(meter, Mapping) or not meter:
        return ""
    measured = meter.get("meter") if isinstance(meter.get("meter"), Mapping) else meter
    try:
        used = int(measured.get("used") or 0)
        limit = int(measured.get("limit") or measured.get("window") or 0)
    except (TypeError, ValueError):
        return ""
    if not limit:
        return ""
    percent = round(float(measured.get("utilization") or 0.0) * 100)
    parts = [f"{used:,}/{limit:,} tok ({percent}%)"]
    peak = measured.get("peak_utilization")
    if peak:
        try:
            parts.append(f"peak {round(float(peak) * 100)}%")
        except (TypeError, ValueError):
            pass
    compactions = meter.get("compaction_count")
    if compactions is None:
        compactions = measured.get("compactions")
    try:
        if int(compactions or 0) > 0:
            parts.append(f"{int(compactions)} compaction(s)")
    except (TypeError, ValueError):
        pass
    rewinds = meter.get("rewind_count")
    try:
        if int(rewinds or 0) > 0:
            parts.append(f"{int(rewinds)} rewind(s)")
    except (TypeError, ValueError):
        pass
    return " · ".join(parts)


def card_lines(facts: Dict[str, Any], mode: str = "fix") -> List[str]:
    """Markup lines for the completion card (Task C).

    One polished final view pulling together what is otherwise scattered:
    status/attempts/model calls (the result event), time taken (the trace's
    own ts span), cost (the cost ledger's total), files changed
    (state.json), the verification verdict (final_verify's chips + its own
    summary line), and the git-native output (branch + commit — the
    harness's git output produces a branch/commit/PR description, not a
    hosted PR URL, so the card shows exactly what exists). Question /
    research modes get the read-only variant (no files/tests/branch rows).
    Assumes facts came from read_run_facts; any missing field simply
    drops its row.
    """
    from rich.markup import escape

    dot = ui.DOT
    status = str(facts.get("status") or "unknown")
    evidence = facts.get("verification_evidence") or []
    if not evidence and facts.get("target_passed") is not None:
        evidence = [facts]
    status = effective_terminal_status(status, evidence)
    verified = status_is_verified(status)
    completed = status_is_completed(status)
    mode = str(mode or facts.get("mode") or "fix")
    if verified:
        mark = ui.GLYPHS["ok"]
        style = "neo.ok"
    elif completed:
        mark = ui.GLYPHS["wait"]
        style = "neo.warn"
    else:
        mark = ui.GLYPHS["fail"]
        style = "neo.error"
    label = (
        "ERROR"
        if str(facts.get("status") or "").lower() == "error"
        else status_label(status)
        if status != "unknown"
        else "UNKNOWN"
    )
    head = (
        f"[{style}]{mark} {escape(label)}[/] [neo.muted]{dot}[/] "
        f"[neo.accent]{escape(mode)}[/] [neo.muted]{dot}[/] [{ui.TEXT_PRIMARY}]{_escape_issue(facts)}[/]"
    )
    rows: List[str] = [head]

    chips: List[str] = []
    if facts.get("attempts") is not None:
        chips.append(f"{facts['attempts']} attempt(s)")
    if facts.get("model_calls"):
        n = int(facts["model_calls"])
        chips.append(f"{n} model call{'s' if n != 1 else ''}")
    if facts.get("elapsed_s") is not None:
        chips.append(fmt_elapsed(facts["elapsed_s"]))
    if facts.get("cost_usd") is not None:
        chips.append(ui.fmt_cost(float(facts["cost_usd"])))
    if facts.get("tokens"):
        chips.append(f"{int(facts['tokens']):,} tokens")
    if chips:
        rows.append(
            f"[neo.muted]   {dot}[/] [{ui.TEXT_PRIMARY}]"
            + f" {dot} ".join(chips)
            + "[/]"
        )

    context_row = context_meter_line(facts.get("context_meter") or {})
    if context_row:
        rows.append(
            f"[neo.muted]   {dot} context[/] [{ui.TEXT_PRIMARY}]{escape(context_row)}[/]"
        )

    if mode in ("question", "research", "ask", "explore", "review", "plan"):
        rows.append(
            f"[neo.muted]   {dot} trace[/] [{ui.TEXT_PRIMARY}]{facts.get('task_id') or ''}[/]"
        )
        return rows

    files = facts.get("files") or facts.get("changed_files") or []
    if files:
        shown = ", ".join(str(f) for f in files[:4]) + (
            f" (+{len(files) - 4} more)" if len(files) > 4 else ""
        )
        rows.append(f"[neo.muted]   {dot} files[/] [{ui.TEXT_PRIMARY}]{shown}[/]")
    if mode in ("agent", "agent_task"):
        action = str(facts.get("current_action") or "finished")
        rows.append(f"[neo.muted]   {dot} action[/] [{ui.TEXT_PRIMARY}]{action}[/]")
        if facts.get("approval") and facts.get("approval") != "not required":
            rows.append(
                f"[neo.muted]   {dot} approval[/] [{ui.TEXT_PRIMARY}]{facts['approval']}[/]"
            )
        if facts.get("last_error"):
            rows.append(
                f"[neo.muted]   {dot} error[/] [neo.error]{str(facts['last_error'])[:100]}[/]"
            )
    if (
        facts.get("target_passed") is not None
        or facts.get("regression_passed") is not None
    ):
        target_value = facts.get("target_passed")
        regression_value = facts.get("regression_passed")
        t = (
            "[neo.warn]UNKNOWN[/]"
            if target_value is None
            else "[neo.ok]PASS[/]"
            if _evidence_bool(target_value)
            else "[neo.error]FAIL[/]"
        )
        r = (
            "[neo.warn]UNKNOWN[/]"
            if regression_value is None
            else "[neo.ok]PASS[/]"
            if _evidence_bool(regression_value)
            else "[neo.error]FAIL[/]"
        )
        flaky = " · flaky!" if _evidence_bool(facts.get("flaky")) else ""
        summary = (
            f" [neo.muted]—[/] [{ui.TEXT_PRIMARY}]{facts['verify_summary']}[/]"
            if facts.get("verify_summary")
            else ""
        )
        rows.append(
            f"[neo.muted]   {dot} tests[/] target {t} [neo.muted]{dot}[/] suite {r}"
            f"[neo.warn]{flaky}[/]{summary}"
        )
    if facts.get("branch"):
        sha = str(facts.get("commit_sha") or "")[:8]
        branch = facts["branch"]
        tail = f" [neo.muted]{dot}[/] [{ui.TEXT_PRIMARY}]{sha}[/]" if sha else ""
        rows.append(f"[neo.muted]   {dot} branch[/] [neo.accent2]{branch}[/]{tail}")
    if facts.get("reason") and not verified:
        rows.append(
            f"[neo.muted]   {dot} note[/] [{ui.TEXT_PRIMARY}]{facts['reason'][:100]}[/]"
        )
    return rows


def status_lines(
    snapshot: Dict[str, Any],
    mode: str = "agent_task",
    live: bool = False,
) -> List[str]:
    """Render a compact, truthful status view from a live projection."""
    from rich.markup import escape

    data = snapshot or {}
    status = str(data.get("status") or ("running" if live else "unknown"))
    evidence = data.get("verification_evidence") or []
    if not evidence and data.get("latest_verification"):
        evidence = [data["latest_verification"]]
    normalized = (
        effective_terminal_status(status, evidence) if status != "unknown" else status
    )
    if status_is_verified(normalized):
        style = "neo.ok"
    elif normalized == "completed_unverified" or normalized in (
        "needs_input",
        "cancelled",
        "blocked",
    ):
        style = "neo.warn"
    elif normalized in ("failed", "timeout") or status in ("error", "failed"):
        style = "neo.error"
    else:
        style = "neo.running"
    label = status_label(normalized) if normalized != "unknown" else "UNKNOWN"
    rows = [
        f"[{style}]{escape(label)}[/] [neo.muted]{ui.DOT}[/] "
        f"[neo.accent]{escape(str(mode or data.get('mode') or 'agent_task'))}[/]"
    ]
    rows.append(
        f"[neo.muted]action[/] [{ui.TEXT_PRIMARY}]{escape(str(data.get('current_action') or '—'))}[/]"
    )
    if data.get("current_tool"):
        rows.append(
            f"[neo.muted]tool[/] [{ui.TEXT_PRIMARY}]{escape(str(data['current_tool']))}[/]"
        )
    turn = data.get("current_turn")
    if turn is not None:
        rows.append(f"[neo.muted]turn[/] [{ui.TEXT_PRIMARY}]{escape(str(turn))}[/]")
    live_context = context_meter_line(data.get("context") or {})
    if live_context:
        rows.append(
            f"[neo.muted]context[/] [{ui.TEXT_PRIMARY}]{escape(live_context)}[/]"
        )
    files = data.get("changed_files") or data.get("files") or []
    if files:
        shown = ", ".join(str(f) for f in files[:5])
        if len(files) > 5:
            shown += f" (+{len(files) - 5} more)"
        rows.append(f"[neo.muted]files[/] [{ui.TEXT_PRIMARY}]{escape(shown)}[/]")
    verification = data.get("latest_verification") or {}
    if isinstance(verification, dict) and verification:
        verdict = "unknown"
        if verification.get("target_passed") is not None:
            verdict = (
                "PASS" if _evidence_bool(verification.get("target_passed")) else "FAIL"
            )
        summary = str(verification.get("summary") or "")
        suffix = f" — {summary}" if summary else ""
        rows.append(
            f"[neo.muted]verify[/] [{ui.TEXT_PRIMARY}]{escape(verdict + suffix)}[/]"
        )
    else:
        rows.append("[neo.muted]verify[/] [neo.muted]not run[/]")
    approval = str(data.get("approval") or "not required")
    if data.get("approval_scope"):
        approval += f" ({data['approval_scope']})"
    rows.append(f"[neo.muted]approval[/] [{ui.TEXT_PRIMARY}]{escape(approval)}[/]")
    if data.get("last_error"):
        rows.append(
            f"[neo.muted]error[/] [neo.error]{escape(str(data['last_error'])[:140])}[/]"
        )
    elapsed = data.get("elapsed_s")
    rows.append(
        f"[neo.muted]elapsed[/] [{ui.TEXT_PRIMARY}]{escape(fmt_elapsed(elapsed) or '—')}[/]"
    )
    context = data.get("context") or {}
    if context:
        context_text = context.get("text") or context.get("summary") or ""
        if context_text:
            rows.append(
                f"[neo.muted]context[/] [{ui.TEXT_PRIMARY}]{escape(str(context_text)[:120])}[/]"
            )
    checkpoints = data.get("checkpoints") or []
    if checkpoints:
        rows.append(
            f"[neo.muted]checkpoints[/] [{ui.TEXT_PRIMARY}]{len(checkpoints)}[/]"
        )
    diagnostics = data.get("diagnostics") or []
    if diagnostics:
        rows.append(
            f"[neo.muted]diagnostics[/] [{ui.TEXT_PRIMARY}]{len(diagnostics)} issue(s)[/]"
        )
    usage_known = data.get("usage_known") or {}
    calls_known = bool(
        usage_known.get(
            "calls",
            data.get("model_calls_known", int(data.get("model_calls") or 0) > 0),
        )
    )
    tokens_known = bool(
        usage_known.get(
            "tokens",
            data.get("tokens_known", int(data.get("tokens") or 0) > 0),
        )
    )
    cost_known = bool(
        usage_known.get(
            "cost", data.get("cost_known", data.get("cost_usd") is not None)
        )
    )
    calls_text = (
        f"{int(data.get('model_calls') or 0)} calls" if calls_known else "unknown calls"
    )
    tokens_text = (
        f"{int(data.get('tokens') or 0):,} tokens" if tokens_known else "unknown tokens"
    )
    cost_text = (
        ui.fmt_cost(float(data.get("cost_usd") or 0.0))
        if cost_known
        else "unknown cost"
    )
    rows.append(
        f"[neo.muted]usage[/] [{ui.TEXT_PRIMARY}]{calls_text}[/] "
        f"[neo.muted]{ui.DOT}[/] [{ui.TEXT_PRIMARY}]{tokens_text}[/] "
        f"[neo.muted]{ui.DOT}[/] [neo.accent2]{cost_text}[/]"
    )
    return rows


def _short_model(name: str) -> str:
    """A model id shortened for a narrow dashboard column.
    'openai/gpt-4o' -> 'gpt-4o'; 'z-ai/glm-5.3-free' -> 'glm-5.3-free'."""
    return (name or "").rsplit("/", 1)[-1]


def headless_status(log_dir: Path, mode: str = "agent_task") -> List[str]:
    """Render the same journal-derived status rows for non-TTY callers."""
    snapshot = read_live_projection(Path(log_dir), mode=mode)
    return status_lines(snapshot, mode=str(snapshot.get("mode") or mode), live=False)


def read_task_progress(log_root: Path, task_id: str) -> Dict[str, Any]:
    """Live per-task facts for the multi-task benchmark dashboard
    (interaction-polish round Task D), re-derived from the run's OWN
    records the same way the completion card is — never a second
    tracking system, never a write:

    - ``trace.jsonl``   -> status (running / result), model calls, cost
      (usage sum with the result event as the authority), phase (the
      last lifecycle-ish event kind), first-event ts (the elapsed clock)
    - ``{tid}.runtime/model_ledger.jsonl`` (runtime's per-call routing
      ledger, Boundary 2's own surface) -> the tier hint(s) actually
      routed on + the model names in play

    A task not started yet (no dir / empty trace) still gets an honest
    record (status "queued", zeros everywhere) so the dashboard can
    show the WHOLE set at once, not only what has begun. total=False
    is not an option here: a benchmark view must never raise over a
    half-written file a worker is still appending to.
    """
    facts: Dict[str, Any] = {
        "task_id": task_id,
        "status": "queued",
        "phase": "",
        "model_calls": 0,
        "cost_usd": 0.0,
        "tokens": 0,
        "elapsed_s": None,
        "tier": "",
        "models": [],
        "started_ts": None,
    }
    root = Path(log_root)
    trace = root / task_id / "trace.jsonl"
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    usage_cost = 0.0
    result_cost: Optional[float] = None
    phase = ""
    try:
        text = (
            trace.read_text(encoding="utf-8", errors="replace")
            if trace.is_file()
            else ""
        )
    except OSError:
        text = ""
    cursor = EventCursor(task_id)
    ordered_events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        accepted = cursor.ingest(ev)
        ordered_events.extend(accepted)
    for ev in ordered_events:
        kind, data, timestamp, _identity = event_parts(ev)
        if timestamp:
            if first_ts is None:
                first_ts = timestamp
            last_ts = timestamp
        if kind in ("model_response", "model_completed"):
            facts["model_calls"] += 1
            usage = data.get("usage") or data
            try:
                usage_cost += float(
                    usage.get("cost", usage.get("cost_usd", usage.get("usd", 0.0)))
                    or 0.0
                )
            except (TypeError, ValueError):
                pass
            try:
                facts["tokens"] += int(
                    usage.get(
                        "tokens",
                        usage.get("total_tokens", usage.get("completion_tokens", 0)),
                    )
                    or 0
                )
            except (TypeError, ValueError):
                pass
            model = str(usage.get("model") or data.get("model") or "")
            if model and model not in facts["models"]:
                facts["models"].append(model)
        elif kind in ("run_started", "task_start"):
            facts["status"] = "running"
            phase = "starting"
        elif kind in (
            "attempt_start",
            "plan",
            "baseline_verify",
            "retrieval",
            "context_built",
        ):
            facts["status"] = "running"
            phase = {
                "attempt_start": "editing",
                "plan": "planning",
                "baseline_verify": "baseline",
                "retrieval": "retrieval",
                "context_built": "context",
            }.get(kind, phase)
        elif kind in ("verify", "verification"):
            phase = "verifying"
        elif kind == "final_verify":
            phase = "final verify"
        elif kind in ("result", "run_finished", "task_end", "completion_decision"):
            nested = data.get("result")
            if isinstance(nested, Mapping):
                data = {
                    **dict(nested),
                    **{k: v for k, v in data.items() if k != "result"},
                }
            if facts["status"] in ("queued", "running") or kind in (
                "result",
                "run_finished",
            ):
                facts["status"] = str(data.get("status") or "running")
            try:
                result_cost = float(data.get("cost_usd", data.get("cost")))
            except (TypeError, ValueError):
                result_cost = None
            phase = facts["status"]
    if first_ts is not None:
        facts["started_ts"] = first_ts
        facts["last_ts"] = last_ts
        # A finished run reports its OWN trace ts span; a running one
        # reports nothing here and lets the caller tick elapsed against
        # the wall clock (a task stuck in a 300s model call has no new
        # events for 300s but the time is genuinely burning).
        if facts["status"] not in ("queued", "running"):
            facts["elapsed_s"] = round(max(0.0, (last_ts or first_ts) - first_ts), 1)
    facts["cost_usd"] = (
        round(result_cost, 6) if result_cost is not None else round(usage_cost, 6)
    )
    facts["phase"] = phase

    # routing ledger: the tier hint(s) this task's calls actually used
    ledger = root / f"{task_id}.runtime" / "model_ledger.jsonl"
    hints: List[str] = []
    try:
        if ledger.is_file():
            for line in ledger.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                hint = rec.get("difficulty_hint") or rec.get("routed_via_hint")
                if hint and str(hint) not in hints:
                    hints.append(str(hint))
                m = str(rec.get("model") or "")
                if m and m not in facts["models"]:
                    facts["models"].append(m)
    except OSError:
        pass
    if hints:
        facts["tier"] = "/".join(h[:1].upper() + h[1:] for h in hints[:3])
    facts["models"] = [_short_model(m) for m in facts["models"][:3]]
    return facts


def _escape_issue(facts: Dict[str, Any]) -> str:
    """The issue fragment for the card head, display-safe (no markup)."""
    return _escape(str(facts.get("issue") or facts.get("task_id") or "")[:60])


def _escape(value: Any) -> str:
    """``rich.markup.escape`` that cannot raise, and cannot fail OPEN.

    A card renderer interpolates values it did not author: a pytest excerpt, a
    subprocess stderr, a provider banner, a file name. Marking those up is a
    deletion bug (a ``[bold]`` in a path eats the message between the tags),
    which is why every one of them is escaped.

    But ``escape`` is a third-party function on a render path, and this
    function's documented contract is that it never raises. A raising escaper
    must therefore NOT hand the value back unescaped — a renderer that fails
    closed by rendering the raw value is a renderer that fails open. It
    withholds the value instead, which is the same rule the display sanitiser
    follows, and it says so in the marker so the omission is visible rather
    than looking like a short line.

    Measured cost of the happy path: one extra function call per interpolated
    field, no measurable change on any card.
    """
    try:
        from rich.markup import escape

        text = str(value)
        # Nothing to escape is a no-op escape. Skipping the third-party call
        # here is not an optimisation, it is what lets the card's GUARANTEED
        # affordance — a product-owned literal naming `/doctor` and `/trace` —
        # survive a broken escaper. A card whose only runnable command
        # disappeared because the markup helper raised is a card that renders
        # during exactly the moment a renderer is least able to.
        if not any(ch in text for ch in "[]\\"):
            return text
        return str(escape(text))
    except Exception:
        return "(value withheld: escaper unavailable)"


# ---------------------------------------------------------------------------
# THE FAILURE / RECOVERY CARD (R2-17 item 6)
#
# "Recovery affordances where failures happen: what failed, why, what to
# do next, in the surface where it happened." `cli.fileview.classify_failure`
# already produced the record and the command registry already advertised
# the actions; NO surface drew the card, which is the gap the cli/AGENTS.md
# handoff named. This is that renderer — pure, so the REPL, the TUI
# transcript, and a `--json` document all read the same facts.
#
# Deliberately does NOT invent a failure taxonomy. Classification is
# delegated to `cli.fileview.classify_failure`, which itself delegates to
# `harness.tool_errors`, so a recovery card, a refusal, and the harness's
# retry policy name the same thing.
# ---------------------------------------------------------------------------

#: What a recovery action means to a person. The keys are
#: `cli.commands.RECOVERY_ACTIONS` (the registry's own vocabulary), so a
#: card can never advertise an action the command registry does not know.
_RECOVERY_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("retry", "re-run the same request"),
    ("edit-input", "rephrase the request and run it again"),
    ("resume", "/resume <task-id> to continue where it stopped"),
    ("undo", "/diff undo to revert the change it made"),
    ("inspect-trace", "/trace to read what it actually did"),
    ("cancel-command", "/cancel to stop a run that is still going"),
    ("return-safe-state", "back to the prompt; nothing was applied"),
)


def failure_record(
    excerpt: str,
    *,
    log_root: Any = None,
    task_id: str = "",
) -> Dict[str, Any]:
    """Classify a failure excerpt into the card's facts. Never raises.

    Delegates classification to `cli.fileview.classify_failure` so the
    card, a refusal, and the harness's own retry policy name the same
    failure kind. When the classifier is unavailable the record says
    `kind: "unknown"` with the reason, and the caller still gets a usable
    card — an unclassifiable failure is reported as unclassifiable, never
    quietly filed under a kind that reads better.
    """
    record: Dict[str, Any] = {
        "kind": "unknown",
        "detail": str(excerpt or "")[:200],
        "hint": "",
        "evidence_path": "",
        "actions": ["inspect-trace"],
    }
    if not str(excerpt or "").strip():
        record["detail"] = "no failure detail was recorded"
        return record
    try:
        from cli.fileview import classify_failure

        # `classify_failure(command, stdout, stderr, output)` classifies a
        # COMMAND's captured output. An exception excerpt is its stderr,
        # which is where the harness and the providers put it. Probing the
        # signature keeps this working if a future caller adds an optional
        # keyword rather than hard-coding one arity.
        found = classify_failure(str(task_id or ""), "", str(excerpt), "")
        if isinstance(found, Mapping):
            # The evidence path is only reported when it EXISTS. The
            # classifier's fallback is a conventional `logs/diagnostics.txt`
            # that need not exist, and printing a path a user cannot open
            # is the same class of lie as printing a status nobody earned.
            evidence = str(found.get("evidence_path") or "")
            try:
                evidence = evidence if Path(evidence).exists() else ""
            except (OSError, TypeError, ValueError):
                evidence = ""
            record.update(
                {
                    "kind": str(found.get("kind") or "unknown"),
                    "detail": str(found.get("detail") or record["detail"])[:200],
                    "hint": str(found.get("hint") or "")[:160],
                    "evidence_path": evidence,
                    "actions": [str(a) for a in (found.get("actions") or ()) if str(a)]
                    or list(record["actions"]),
                }
            )
    except Exception as exc:
        record["detail"] = (
            f"{record['detail']} (classifier unavailable: {type(exc).__name__})"
        )
    return record


def recovery_actions(kinds: Any) -> List[str]:
    """Human-readable recovery actions for a failure's action list.

    Accepts BOTH shapes the product has for an action, because both are
    real and dropping either loses a genuine affordance:

    - a ``cli.commands.RECOVERY_ACTIONS`` registry key (``retry``,
      ``resume``, …), which is mapped to its human wording; and
    - a ready-made sentence from ``cli.fileview``'s per-error-kind table,
      which is passed through unchanged because it already names the
      runnable command.

    An action that is neither is DROPPED rather than printed: a
    suggestion the user cannot run is worse than one fewer suggestion.
    An empty result falls back to ``/trace``, which is always runnable.
    Never raises.
    """
    if isinstance(kinds, str):
        kinds = [kinds]
    wanted: List[str] = []
    try:
        for item in kinds or ():
            text = str(item or "").strip()
            if not text:
                continue
            key = text.lower()
            mapped = ""
            for name, wording in _RECOVERY_ACTIONS:
                if key == name:
                    mapped = wording
                    break
            # A registry key maps; anything else that reads as a
            # sentence is already a runnable instruction and passes
            # through. A bare unknown token is dropped.
            chosen = mapped or (text if " " in text else "")
            if chosen and chosen not in wanted:
                wanted.append(chosen)
    except Exception:
        return []
    if not wanted:
        wanted.append(_RECOVERY_ACTIONS[4][1])  # inspect-trace: always honest
    return wanted[:4]


def failure_lines(
    excerpt: str,
    *,
    log_root: Any = None,
    task_id: str = "",
    width: int = 72,
) -> List[str]:
    """The failure card: what failed, why, what to do next.

    Pure and total. Every interpolated value is `escape()`d, so a failure
    message containing `[` cannot become a style tag — the same
    markup-injection discipline the stream paint and the diff path use.
    Returns at least one line for any input, including an empty excerpt,
    because a card that renders nothing at the moment of failure is the
    absence of an affordance.

    The escaping goes through :func:`_escape`, never ``rich.markup.escape``
    directly, so a raising escaper WITHHOLDS a value rather than escaping the
    whole card's contract by raising out of a renderer.
    """
    escape = _escape
    dot = ui.DOT
    record = failure_record(excerpt, log_root=log_root, task_id=task_id)
    head = "[neo.error]what failed[/]"
    if task_id:
        head += f" [neo.muted]{dot}[/] [{ui.TEXT_PRIMARY}]{escape(str(task_id))}[/]"
    rows = [head]
    kind = str(record.get("kind") or "unknown")
    rows.append(f"   [neo.muted]{dot} kind[/] [neo.error]{escape(kind)}[/]")
    detail = str(record.get("detail") or "")
    if detail:
        rows.append(
            f"   [neo.muted]{dot} why[/] [{ui.TEXT_PRIMARY}]{escape(detail[:width])}[/]"
        )
    hint = str(record.get("hint") or "")
    if hint:
        rows.append(
            f"   [neo.muted]{dot} hint[/] [neo.accent]{escape(hint[:width])}[/]"
        )
    actions = recovery_actions(record.get("actions"))
    for action in actions:
        rows.append(f"   [neo.muted]{dot} next[/] [neo.accent]{escape(action)}[/]")
    # GUARANTEED runnable affordance. A classifier action can be pure
    # diagnosis ("wait for the rate-limit window"), which is correct advice
    # and useless as an instruction. The card's whole job is "what to do
    # next", so a card with no `/command` on it always ends with the two
    # that are answerable from any state.
    if not any(re.search(r"/[a-z][a-z0-9_-]+", action) for action in actions):
        rows.append(
            f"   [neo.muted]{dot} next[/] [neo.accent]"
            f"{escape('/doctor for the machine, /trace for the evidence')}[/]"
        )
    evidence = str(record.get("evidence_path") or "")
    if evidence:
        rows.append(
            f"   [neo.muted]{dot} evidence[/] [{ui.TEXT_PRIMARY}]"
            f"{escape(evidence[-width:])}[/]"
        )
    return rows


# ---------------------------------------------------------------------------
# VEX-PF-04 — what the run is waiting for, and what a revert touched
# ---------------------------------------------------------------------------
#
# Two read-only questions a transcript surface cannot answer by itself,
# both derived from the same journal rows everything else in this module
# reads. Neither writes, neither raises, and both degrade to an honest
# "nothing pending" rather than to a guess.

#: The rows that open and close a permission gate, and the rows that open
#: and close a question. Declared once so the gate and the projection
#: cannot disagree about which rows count.
APPROVAL_OPEN_ROWS = ("approval_required", "input_requested")
APPROVAL_CLOSE_ROWS = (
    "approval_decided",
    "permission_decision",
    "approval_denied",
    "approval_error",
)
QUESTION_OPEN_ROWS = ("question_asked", "question_pending", "unresolved_question")
QUESTION_CLOSE_ROWS = ("question_answered", "question_resolved", "question_cleared")


def pending_decision(projection: Any = None, *, log_dir: Any = None) -> Dict[str, Any]:
    """What the run is waiting on the user for. ``{}`` means nothing.

    A permission wins over a question, because a permission is the harder
    gate: a steering message sent into it is also ignored, and the honest
    thing to tell the composer is the permission.

    Accepts a :class:`RunProjection`, a ``snapshot()`` mapping, or a
    ``log_dir`` to read a journal from. The three are the three things a
    surface has on hand, and making each of them build its own answer is
    how a shell ends up typing into a box that ignores it.
    """
    facts: Mapping[str, Any] = {}
    if isinstance(projection, RunProjection):
        facts = projection.snapshot()
    elif isinstance(projection, Mapping):
        facts = projection
    elif projection is not None or log_dir is not None:
        target = log_dir if log_dir is not None else projection
        try:
            facts = read_live_projection(Path(target))
        except (TypeError, ValueError, OSError):
            return {}
    if not isinstance(facts, Mapping):
        return {}
    if str(facts.get("approval") or "").lower() == "waiting":
        return {
            "kind": "permission",
            "scope": str(facts.get("approval_scope") or ""),
            "effect": str(facts.get("approval_effect") or ""),
            "note": str(facts.get("approval_note") or ""),
        }
    question = facts.get("pending_question")
    if isinstance(question, Mapping) and question:
        return {
            "kind": "question",
            "question": str(question.get("text") or ""),
            "question_id": str(question.get("id") or ""),
        }
    rows = facts.get("questions")
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if isinstance(row, Mapping) and str(row.get("status")) == "waiting":
                return {
                    "kind": "question",
                    "question": str(row.get("text") or ""),
                    "question_id": str(row.get("id") or ""),
                }
    return {}


def undo_receipt(
    log_dir: Any, *, task_id: str = "", limit: int = 200
) -> Dict[str, Any]:
    """Derive the inline undo receipt from a run's own journal.

    Reads the rows a revert writes (``undo``/``undo_applied``/
    ``undo_reverted``/``undo_receipt`` and their ``redo`` counterparts) and
    returns the facts :func:`cli.streamview.undo_notice_from_facts` turns
    into the inline block. A journal with no such row returns ``{}``,
    which is the honest answer for "this run has not been reverted" and
    is what stops a surface from printing a zero-file revert that never
    happened.
    """
    trace = Path(log_dir) / "trace.jsonl"
    receipt: Dict[str, Any] = {}
    files: Dict[str, Dict[str, int]] = {}
    try:
        handle = trace.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        for index, line in enumerate(handle):
            if index > int(limit or 0):
                break
            row = _as_event_row(line)
            if not row:
                continue
            kind = str(row[0] or "").strip().lower()
            data = row[1] if isinstance(row[1], Mapping) else {}
            if kind in ("undo_reverted", "undo", "undo_applied"):
                receipt = {
                    "messages_reverted": int(
                        data.get("messages_reverted") or data.get("turns") or 1
                    ),
                    "undone": True,
                    "restore_key": str(data.get("restore_key") or "ctrl+z"),
                    "redo_command": str(data.get("redo_command") or "/redo"),
                }
                for item in data.get("files") or data.get("restored_files") or []:
                    if isinstance(item, Mapping):
                        path = str(item.get("path") or item.get("file") or "")
                        if path:
                            files[path] = {
                                "added": int(item.get("added") or 0),
                                "removed": int(item.get("removed") or 0),
                            }
                    elif isinstance(item, str) and item.strip():
                        files.setdefault(item.strip(), {"added": 0, "removed": 0})
            elif kind in ("redo_applied", "redo"):
                receipt = {"undone": False, "messages_reverted": 0}
                files = {}
    except OSError:
        return {}
    finally:
        handle.close()
    if not receipt:
        return {}
    if files:
        receipt["files"] = [{"path": path, **counts} for path, counts in files.items()]
    if task_id:
        receipt["task_id"] = str(task_id)
    return receipt


def _as_event_row(line: Any) -> Optional[Tuple[str, Any]]:
    """One JSONL journal line as ``(kind, payload)``, or ``None``.

    Reuses the module's own normaliser so a canonical
    ``event``/``payload`` row and a legacy ``kind``/``data`` row are read
    the same way. A torn tail is skipped, not fatal: a revert receipt
    read from a half-written journal is still a useful answer.
    """
    try:
        row = json.loads(line)
    except Exception:
        return None
    if not isinstance(row, Mapping):
        return None
    return (event_kind(row), event_parts(row)[1])
