"""Durable per-turn state for one agent-kernel run.

The kernel checkpoints on every turn, but until now the only durable per-turn
artifact was ``checkpoint.json`` — a single mutable file whose previous
contents are lost on the next write. A hard kill between two turns therefore
erased the evidence of what the agent had already done.

This module adds an append-only ``turns.jsonl`` ledger: one compact record per
turn, written and flushed to disk at every checkpoint, not only at completion.
A hard kill keeps every pre-kill record, a resume reads them back, and
:func:`replay_turn_ledger` derives the same projection a live run holds in
memory — so replay and a live run agree without calling a model or executing a
tool.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

TURN_SCHEMA_VERSION = 1
LEDGER_NAME = "turns.jsonl"


def turn_ledger_path(run_dir: Path | str) -> Path:
    """Return the ledger path for one run directory."""
    return Path(run_dir) / LEDGER_NAME


class TurnLedger:
    """Append-only, flushed-on-write ledger of per-turn records.

    Assumes the parent directory exists (the kernel creates the run directory
    before constructing strategies). Write failures are never fatal: the
    ledger is evidence, not the authority for the run's outcome, so a
    failure is surfaced through :attr:`warnings` and the run continues.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._warnings: List[str] = []
        self.last_append_succeeded = True

    @property
    def warnings(self) -> List[str]:
        """Return explicit write/read warnings accumulated by this ledger."""
        return list(self._warnings)

    def append(
        self,
        *,
        turn: int,
        event_sequence: int = 0,
        tool_calls: Optional[Sequence[Mapping[str, Any]]] = None,
        changed_files: Optional[Sequence[str]] = None,
        facts: Optional[Sequence[Mapping[str, Any]]] = None,
        message_count: int = 0,
        dropped_messages: int = 0,
        handoff: Optional[Mapping[str, Any]] = None,
        spend_usd: float = 0.0,
        model_calls: int = 0,
        status: str = "in_progress",
        note: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Append one compact turn record durably and return it.

        The record is written with an explicit flush and ``fsync`` so a hard
        kill immediately after this call cannot lose the turn. Returns
        ``None`` when the write failed; the failure is recorded in
        :attr:`warnings` rather than raised into the agent loop.
        """
        record: Dict[str, Any] = {
            "schema_version": TURN_SCHEMA_VERSION,
            "turn": int(turn or 0),
            "event_sequence": int(event_sequence or 0),
            "status": str(status or "in_progress"),
            "tool_calls": [dict(item) for item in (tool_calls or [])],
            "changed_files": sorted(
                {str(item) for item in (changed_files or []) if item}
            ),
            "facts": [dict(item) for item in (facts or [])],
            "message_count": int(message_count or 0),
            "dropped_messages": int(dropped_messages or 0),
            "handoff": dict(handoff) if isinstance(handoff, Mapping) else {},
            "spend_usd": round(float(spend_usd or 0.0), 6),
            "model_calls": int(model_calls or 0),
            "note": str(note or ""),
            "created_at": time.time(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            self.last_append_succeeded = False
            self._record_warning(f"turn ledger unwritable: {exc}")
            return None
        self.last_append_succeeded = True
        return record

    def load(self) -> List[Dict[str, Any]]:
        """Return every well-formed record; a torn tail is skipped honestly."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            self._record_warning(f"turn ledger unreadable: {exc}")
            return []
        records: List[Dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(value, dict) and int(value.get("turn") or 0) > 0:
                records.append(value)
        return records

    def _record_warning(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)


def replay_turn_ledger(path: Path | str) -> Dict[str, Any]:
    """Project a ledger into a deterministic turn-by-turn state summary.

    This is the replay half of the durable-turn contract: it never calls a
    model and never executes a tool, and a live run holding the same ledger
    on disk projects to exactly the same structure.
    """
    records = TurnLedger(path).load()
    turns: List[Dict[str, Any]] = []
    tool_counts: Dict[str, int] = {}
    files: List[str] = []
    spend = 0.0
    model_calls = 0
    highest_sequence = 0
    for record in records:
        tools = []
        for item in record.get("tool_calls") or []:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("tool") or "")
            if not name:
                continue
            tools.append(name)
            tool_counts[name] = tool_counts.get(name, 0) + 1
        for name in record.get("changed_files") or []:
            if str(name) not in files:
                files.append(str(name))
        highest_sequence = max(highest_sequence, int(record.get("event_sequence") or 0))
        spend = max(spend, float(record.get("spend_usd") or 0.0))
        model_calls = max(model_calls, int(record.get("model_calls") or 0))
        turns.append(
            {
                "turn": int(record.get("turn") or 0),
                "event_sequence": int(record.get("event_sequence") or 0),
                "status": str(record.get("status") or "in_progress"),
                "tools": tools,
                "changed_files": [
                    str(name) for name in record.get("changed_files") or []
                ],
                "message_count": int(record.get("message_count") or 0),
                "dropped_messages": int(record.get("dropped_messages") or 0),
                "spend_usd": float(record.get("spend_usd") or 0.0),
            }
        )
    return {
        "turn_count": len(turns),
        "last_turn": max((item["turn"] for item in turns), default=0),
        "last_event_sequence": highest_sequence,
        "tool_counts": dict(sorted(tool_counts.items())),
        "changed_files": files,
        "spend_usd": round(spend, 6),
        "model_calls": model_calls,
        "turns": turns,
    }
