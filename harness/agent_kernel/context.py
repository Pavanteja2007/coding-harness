"""Structured session continuity and context compaction for the agent kernel.

Two responsibilities live here. The first is session continuity: one bounded,
atomically persisted state file per session, plus the ``[system, user]`` base
frame a run seeds its rolling conversation from. The second is the context
BUDGET: every assembled bundle is measured in prompt tokens, split into the
categories an operator needs to see, and reported as a meter. The budget is
what tells the strategy when compaction is due, and the meter is what makes the
run's context cost observable in the journal, ``--json``, and the TUI.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.trace import redact_secrets

from .budget import ContextBudget, ContextMeter, budget_from_config
from .contracts import SessionState

MAX_TURNS = 24
MAX_SUMMARY_CHARS = 6000
MAX_DIFF_CHARS = 12000


@dataclass
class ContextBundle:
    """The bounded model context assembled for one turn."""

    messages: List[Dict[str, str]]
    state: SessionState
    references: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    compacted: bool = False
    #: Measured prompt-token cost of ``messages`` against the configured window.
    meter: Optional[ContextMeter] = None

    @property
    def window(self) -> int:
        """Return the usable prompt window this bundle was measured against."""
        return int(self.meter.limit) if self.meter is not None else 0

    def as_dict(self) -> Dict[str, Any]:
        """Return a serializable context description without raw messages."""
        return {
            "references": list(self.references),
            "warnings": list(self.warnings),
            "compacted": self.compacted,
            "message_count": len(self.messages),
            "summary": self.state.summary,
            "context_meter": self.meter.as_dict() if self.meter is not None else {},
        }


class SessionStore:
    """Atomically load and save one session state file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._warnings: List[str] = []
        self.last_save_succeeded = True

    @property
    def warnings(self) -> List[str]:
        """Return explicit state warnings accumulated by this store."""
        return list(self._warnings)

    def load(self, session_id: str = "") -> Optional[SessionState]:
        """Load state, returning ``None`` for missing or corrupt state.

        Corruption is never silently converted into a valid empty session;
        the warning remains available through :attr:`warnings` and the
        ``load_with_warnings`` method.
        """
        state, _ = self.load_with_warnings(session_id)
        return state

    def load_with_warnings(
        self, session_id: str = ""
    ) -> Tuple[Optional[SessionState], List[str]]:
        """Load state together with non-fatal corruption/read warnings."""
        warnings: List[str] = []
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, warnings
        except OSError as exc:
            message = f"session state unreadable: {exc}"
            self._record_warning(message)
            return None, [message]
        try:
            raw = json.loads(raw_text)
        except ValueError as exc:
            message = f"session state corrupt JSON: {exc}"
            self._record_warning(message)
            return None, [message]
        if not isinstance(raw, dict):
            message = "session state corrupt: root is not an object"
            self._record_warning(message)
            return None, [message]
        try:
            state = SessionState.from_dict(raw)
        except (TypeError, ValueError) as exc:
            message = f"session state invalid: {exc}"
            self._record_warning(message)
            return None, [message]
        if not state.session_id:
            message = "session state invalid: missing session_id"
            self._record_warning(message)
            return None, [message]
        if session_id and state.session_id != str(session_id):
            message = (
                f"session state identity mismatch: expected {session_id!r}, "
                f"found {state.session_id!r}"
            )
            self._record_warning(message)
            return None, [message]
        return state, warnings

    def save(self, state: SessionState) -> bool:
        """Atomically persist state; return false and warn on write failure."""
        with self._lock:
            state.updated_at = time.time()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(
                    f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                temporary.write_text(
                    json.dumps(
                        redact_secrets(state.to_dict()),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
                self.last_save_succeeded = True
                return True
            except (OSError, TypeError, ValueError) as exc:
                self.last_save_succeeded = False
                self._record_warning(f"session state unwritable: {exc}")
                return False

    def append_turn(
        self,
        state: SessionState,
        user_request: str,
        answer: str = "",
        *,
        summary: str = "",
        changed_files: Optional[Sequence[str]] = None,
        unresolved_questions: Optional[Sequence[str]] = None,
        event_sequence: int = 0,
        max_turns: int = MAX_TURNS,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
    ) -> SessionState:
        """Append a compact turn and save it without retaining raw transcripts."""
        turn = {
            "request": str(user_request or "")[:2000],
            "answer": str(answer or "")[:3000],
            "summary": str(summary or "")[:2000],
            "changed_files": sorted(
                {str(x).replace("\\", "/") for x in changed_files or []}
            ),
            "unresolved_questions": [str(x) for x in unresolved_questions or []],
            "event_sequence": int(event_sequence or 0),
            "created_at": time.time(),
        }
        state.turns.append(turn)
        limit = max(1, int(max_turns or MAX_TURNS))
        compacted = len(state.turns) > limit
        if compacted:
            dropped = state.turns[:-limit]
            state.turns = state.turns[-limit:]
            old_summary = state.summary.strip()
            dropped_text = "; ".join(
                f"request={item.get('request', '')[:180]} answer={item.get('answer', '')[:240]}"
                for item in dropped
            )
            state.summary = _bounded(
                "\n".join(
                    part
                    for part in (
                        old_summary,
                        "Earlier compacted turns: " + dropped_text,
                    )
                    if part
                ),
                max(512, int(max_summary_chars)),
            )
        if summary:
            state.summary = _bounded(
                "\n".join(part for part in (state.summary, str(summary)) if part),
                max(512, int(max_summary_chars)),
            )
        if changed_files:
            state.changed_files = sorted(
                set(state.changed_files)
                | {str(x).replace("\\", "/") for x in changed_files}
            )
        if unresolved_questions is not None:
            state.unresolved_questions = [str(x) for x in unresolved_questions]
        self.save(state)
        return state

    def _record_warning(self, message: str) -> None:
        with self._lock:
            if message not in self._warnings:
                self._warnings.append(message)


class ContextBuilder:
    """Build bounded, structured context from a session and event journal."""

    def __init__(
        self,
        store: SessionStore,
        *,
        max_turns: int = MAX_TURNS,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
        max_diff_chars: int = MAX_DIFF_CHARS,
        budget: Optional[ContextBudget] = None,
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.store = store
        self.max_turns = max(1, int(max_turns))
        self.max_summary_chars = max(512, int(max_summary_chars))
        self.max_diff_chars = max(512, int(max_diff_chars))
        self.budget = budget or budget_from_config(config)
        self.config = dict(config or {})

    def load_state(self, session_id: str) -> Tuple[SessionState, List[str]]:
        """Load or initialize a state object and return any warnings."""
        state, warnings = self.store.load_with_warnings(session_id)
        if state is None:
            if self.store.path.exists() and warnings:
                state = SessionState(session_id=session_id)
            else:
                state = SessionState(session_id=session_id)
        else:
            state.turns = state.turns[-self.max_turns :]
            state.summary = _bounded(state.summary, self.max_summary_chars)
            state.prior_diff = _bounded(state.prior_diff, self.max_diff_chars)
        return state, warnings + list(self.store.warnings)

    def discover_project_instructions(self, repository_identity: str) -> List[str]:
        """Read applicable project instruction files without touching secrets."""
        root = Path(repository_identity or ".").expanduser()
        candidates = [
            root / "AGENTS.md",
            root / ".neo" / "AGENTS.md",
            root.parent / "AGENTS.md",
        ]
        found: List[str] = []
        for candidate in candidates:
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    found.append(
                        f"{candidate.as_posix()}:\n{text[: self.max_summary_chars]}"
                    )
            except OSError:
                continue
        return found

    def measure(
        self, messages: Sequence[Mapping[str, Any]], *, turn_index: int = 0
    ) -> ContextMeter:
        """Return the measured prompt-token cost of one assembled request."""
        return self.budget.measure(messages, turn=turn_index)

    def build(
        self,
        *,
        session_id: str,
        request: str,
        repository_identity: str = "",
        instructions: Optional[Sequence[str]] = None,
        prior_diff: str = "",
        unresolved_questions: Optional[Sequence[str]] = None,
        extra_context: str = "",
        event_references: Optional[Sequence[str]] = None,
        turn_index: int = 0,
    ) -> ContextBundle:
        """Render a system/user context pair with explicit continuity fields."""
        state, warnings = self.load_state(session_id)
        if instructions is None:
            instructions = self.discover_project_instructions(repository_identity)
        instruction_lines = [
            str(item) for item in instructions or [] if str(item).strip()
        ]
        if instruction_lines:
            state.project_instructions = instruction_lines
        active_task = str(request or state.active_task or "")
        if request:
            state.active_task = active_task
        state.repository_identity = str(
            repository_identity or state.repository_identity or ""
        )
        state.prior_diff = _bounded(prior_diff or state.prior_diff, self.max_diff_chars)
        if unresolved_questions is not None:
            state.unresolved_questions = [str(x) for x in unresolved_questions]
        references = [str(x) for x in event_references or []]
        continuity = [
            f"Session: {session_id}",
            f"Current request: {active_task or '(none)'}",
            f"Structured summary: {state.summary or '(none)'}",
            f"Prior changed files: {', '.join(state.changed_files[-20:]) or '(none)'}",
            f"Unresolved questions: {'; '.join(state.unresolved_questions) or '(none)'}",
        ]
        if state.plan_steps:
            continuity.append(
                "Current plan:\n" + "\n".join(f"- {item}" for item in state.plan_steps)
            )
        if state.todo_items:
            continuity.append(
                "Current todo:\n" + "\n".join(f"- {item}" for item in state.todo_items)
            )
        if state.turns:
            recent = []
            for turn in state.turns[-8:]:
                request_text = str(turn.get("request") or "")
                answer_text = str(turn.get("answer") or "")
                if request_text or answer_text:
                    recent.append(
                        f"- request={request_text[:300]}; answer={answer_text[:500]}"
                    )
            if recent:
                continuity.append("Relevant recent turns:\n" + "\n".join(recent))
        if state.prior_diff:
            continuity.append("Prior diff (bounded):\n" + state.prior_diff)
        if state.project_instructions:
            continuity.append(
                "Applicable project instructions:\n"
                + "\n\n".join(state.project_instructions)
            )
        if extra_context:
            continuity.append(
                "Additional retrieved context:\n" + str(extra_context)[:8000]
            )
        try:
            from harness.prompts import render_daily_prompt

            messages = render_daily_prompt(
                active_task or "(none)",
                "\n\n".join(continuity),
                "\n\n".join(state.project_instructions),
            )
        except Exception:
            messages = [
                {
                    "role": "system",
                    "content": "You are the Neo coding agent kernel. Use typed tools and report verification honestly.",
                },
                {"role": "user", "content": "\n\n".join(continuity)},
            ]
        return ContextBundle(
            messages=messages,
            state=state,
            references=references,
            warnings=warnings,
            compacted=len(state.turns) >= self.max_turns,
            meter=self.measure(messages, turn_index=turn_index),
        )

    def persist_turn(
        self,
        state: SessionState,
        request: str,
        answer: str,
        *,
        summary: str = "",
        changed_files: Optional[Sequence[str]] = None,
        unresolved_questions: Optional[Sequence[str]] = None,
        event_sequence: int = 0,
    ) -> SessionState:
        """Persist one bounded turn through the associated store."""
        return self.store.append_turn(
            state,
            request,
            answer,
            summary=summary,
            changed_files=changed_files,
            unresolved_questions=unresolved_questions,
            event_sequence=event_sequence,
            max_turns=self.max_turns,
            max_summary_chars=self.max_summary_chars,
        )


# ---------------------------------------------------------------------------
# Three-way rewind
# ---------------------------------------------------------------------------
#
# A rewind picker offers "undo this turn" without saying which axis it means, and
# a user who wanted the conversation back does not expect their files to move.
# So the two axes are separate functions over the same durable authority:
#
#   conversation -> conversation.jsonl + turns.jsonl + checkpoint.json
#   files        -> the per-turn pre-image store built from the run's snapshot
#
# Both are exact: the conversation axis truncates append-only journals (rotating
# the previous file aside, never deleting it) and the file axis restores the bytes
# that were on disk at the start of the target turn.


def rewind_run(
    *,
    run_dir: Path | str,
    repository_identity: str = "",
    turn: int,
    scope: str = "both",
    protected_paths: Sequence[str] = (),
    turn_files: Any = None,
) -> Dict[str, Any]:
    """Rewind one run's durable state to the exact state before ``turn``.

    ``scope`` is ``conversation``, ``files``, or ``both``. The two axes never
    touch each other's artifacts, so a conversation-only rewind cannot move a
    file and a files-only rewind cannot edit a conversation record.

    ``turn_files`` lets a live strategy reuse the pre-image store it has already
    filled; when omitted, one is opened from ``run_dir`` and driven by the run's
    own pristine snapshot. Returns a receipt naming exactly what was restored,
    dropped, and rotated aside. Raises ``ValueError`` for an unknown scope (a
    picker must not silently rewind the wrong axis).
    """
    from runtime.checkpoint import (
        ConversationJournal,
        TurnFileState,
        rewind_checkpoint,
        rewind_turn_ledger,
    )

    from .workspace import WorkspaceJournal

    axis = str(scope or "both").strip().lower()
    if axis not in {"conversation", "files", "both"}:
        raise ValueError(f"unknown rewind scope: {scope!r}")
    boundary = max(0, int(turn or 0))
    directory = Path(run_dir)
    receipt: Dict[str, Any] = {
        "turn": boundary,
        "scope": axis,
        "run_dir": str(directory),
        "repository": str(repository_identity or ""),
    }
    if axis in {"conversation", "both"}:
        journal = ConversationJournal(directory)
        journal_receipt = journal.rewind(boundary)
        ledger_receipt = rewind_turn_ledger(directory / "turns.jsonl", boundary)
        checkpoint_receipt = rewind_checkpoint(
            directory / "checkpoint.json", ledger_receipt
        )
        receipt["conversation"] = {
            "kept_rows": journal_receipt["kept_rows"],
            "dropped_rows": journal_receipt["dropped_rows"],
            "dropped_turns": journal_receipt["dropped_turns"],
            "backup": journal_receipt["backup"],
            "snapshot": journal_receipt["snapshot"],
            "ledger": {
                "kept": ledger_receipt["kept"],
                "dropped": ledger_receipt["dropped"],
                "backup": ledger_receipt["backup"],
                "last_turn": ledger_receipt["last_turn"],
            },
            "checkpoint": {
                "rewritten": checkpoint_receipt.get("rewritten"),
                "turn_id": (checkpoint_receipt.get("checkpoint") or {}).get(
                    "turn_id", ""
                ),
            },
        }
    if axis in {"files", "both"}:
        workspace = WorkspaceJournal(
            repository_identity or ".",
            directory / "pristine",
            protected_paths=protected_paths,
        )
        store = turn_files
        if store is None:
            store = TurnFileState(
                directory,
                read_bytes=_workspace_reader(workspace),
                write_bytes=_workspace_writer(workspace),
                read_pristine=_pristine_reader(workspace),
            )
        file_receipt = store.rewind(boundary, tracked=workspace.changed_files())
        file_receipt["git_status"] = workspace.git_status()
        file_receipt["changed_files"] = workspace.changed_files()
        receipt["files"] = file_receipt
    return receipt


def _workspace_reader(workspace: Any) -> Callable[[str], Optional[bytes]]:
    def read(relative: str) -> Optional[bytes]:
        path = workspace.safe_path(relative)
        if path is None or not path.is_file() or path.is_symlink():
            return None
        return path.read_bytes()

    return read


def _workspace_writer(workspace: Any) -> Callable[[str, Optional[bytes]], None]:
    def write(relative: str, data: Optional[bytes]) -> None:
        path = workspace.safe_path(relative)
        if path is None:
            raise ValueError(f"refused rewind path: {relative}")
        if data is None:
            if path.is_file() or path.is_symlink():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(data))

    return write


def _pristine_reader(workspace: Any) -> Callable[[str], Optional[bytes]]:
    snapshot = getattr(workspace, "snapshot_path", None)
    root = Path(snapshot) if snapshot else None

    def read(relative: str) -> Optional[bytes]:
        if root is None or not root.is_dir():
            return None
        candidate = root / Path(str(relative).replace("\\", "/"))
        if not candidate.is_file() or candidate.is_symlink():
            return None
        return candidate.read_bytes()

    return read


def _bounded(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[compacted]"
