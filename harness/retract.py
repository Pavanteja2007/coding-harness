"""Retract and edit-message: the user changes their mind about a run.

Two honest, history-preserving operations over an existing run directory:

* :func:`retract` removes an assistant turn and everything after it and
  restores the file state to that point. It does NOT delete history - the
  durable event journal (``trace.jsonl``) is append-only, so retract moves
  the head and records a marker; nothing is removed (:data:`RETRACTED_NAME`).
* :func:`edit_message` changes a prior user message and re-runs from that
  point using the same machinery.

Every answer first tells the user what will be discarded, then acts only on
explicit confirmation. A retracted run is marked and is not resumable: its
state changed under the resume token, and the marker is consulted by
:func:`is_retracted`/:func:`may_resume`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .agent_kernel.context import rewind_run

__all__ = [
    "RETRACTED_NAME",
    "TOOL_RETRACTED",
    "edit_message",
    "is_retracted",
    "may_resume",
    "preview_retraction",
    "retract",
]

RETRACTED_NAME = "RETRACTED.json"

#: The assistant role of the turn the user is undoing.
TOOL_RETRACTED = "assistant"

_TRACE = "trace.jsonl"
_TURNS = "turns.jsonl"
_CONVERSATION = "conversation.jsonl"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except FileNotFoundError:
        pass
    return rows


def preview_retraction(
    run_dir: Any, *, turn: int, scope: str = "both"
) -> Dict[str, Any]:
    """What a retract to ``turn`` WOULD discard or revert. Pure read.

    A reader who sees only this receipt must be able to decide whether
    retracting is safe: every number names the thing it gives up.
    """
    directory = Path(run_dir)
    boundary = max(0, int(turn or 0))
    convo = _read_jsonl(directory / _CONVERSATION)
    discarded_turns = 0
    for row in convo:
        try:
            if int(row.get("turn", 0) or 0) >= boundary and row.get(
                "kind"
            ) in ("turn", "tool_result", "image"):
                discarded_turns += 1
        except (TypeError, ValueError):
            continue
    files: List[str] = []
    for row in _read_jsonl(directory / _TURNS):
        try:
            if int(row.get("turn", 0) or 0) >= boundary:
                files.extend(str(p) for p in (row.get("changed_files") or []))
        except (TypeError, ValueError):
            continue
    reverted_files = sorted(set(files))
    return {
        "turn": boundary,
        "scope": scope,
        "discarded_turns": discarded_turns,
        "reverted_files": reverted_files,
        "reverted_file_count": len(reverted_files),
        "preview": (
            f"This removes {discarded_turns} turn record(s) and reverts "
            f"{len(reverted_files)} file(s)."
        ),
    }


def _append_trace(run_dir: Path, row: Mapping[str, Any]) -> None:
    """Append one row to the event journal. The journal is APPEND-ONLY:
    retract moves the head; it never deletes a row."""
    line = json.dumps(dict(row), ensure_ascii=False, default=str)
    with (run_dir / _TRACE).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def redacted_trace_rows(run_dir: Any) -> List[Dict[str, Any]]:
    """The durable journal rows, unchanged. A retract caller can always see
    exactly what happened before the head moved."""
    return _read_jsonl(Path(run_dir) / _TRACE)


def is_retracted(run_dir: Any) -> bool:
    return (Path(run_dir) / RETRACTED_NAME).is_file()


def may_resume(run_dir: Any) -> bool:
    """A retracted run MUST NOT resume: the turn state changed under the
    resume token, so the honest answer is a fresh start."""
    return not is_retracted(run_dir)


def retract(
    run_dir: Any,
    *,
    turn: int,
    scope: str = "both",
    repository_identity: str = "",
    preview_only: bool = False,
) -> Dict[str, Any]:
    """Retract an assistant turn and everything after it.

    Scopes ``conversation`` / ``files`` / ``both`` are exact and never cross
    axes (delegated to :func:`rewind_run`). History is preserved: the trace
    journal keeps every row and a ``retract`` marker row is appended. The
    run is marked so it cannot silently resume.
    """
    directory = Path(run_dir)
    preview = preview_retraction(directory, turn=turn, scope=scope)
    if preview_only:
        preview["preview_only"] = True
        return preview
    receipt = rewind_run(
        run_dir=directory,
        repository_identity=repository_identity,
        turn=int(turn or 0),
        scope=scope,
    )
    marker = {
        "retracted": True,
        "at_turn": int(turn or 0),
        "scope": scope,
        "discarded_turns": preview["discarded_turns"],
        "reverted_files": preview["reverted_files"],
        "ts": time.time(),
    }
    (directory / RETRACTED_NAME).write_text(
        json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _append_trace(
        directory,
        {
            "kind": "retract",
            "turn": int(turn or 0),
            "scope": scope,
            "discarded_turns": preview["discarded_turns"],
            "reverted_file_count": preview["reverted_file_count"],
        },
    )
    return {
        **receipt,
        "retracted": True,
        "preview": preview,
        "marker": RETRACTED_NAME,
    }


def edit_message(
    run_dir: Any,
    *,
    user_turn: int,
    new_text: str,
    scope: str = "conversation",
    repository_identity: str = "",
    preview_only: bool = False,
) -> Dict[str, Any]:
    """Edit a prior user message and re-run from that point.

    The rewind to just before ``user_turn`` uses the same machinery as
    :func:`retract`; the following head is a fresh, honestly-marked run - it
    never rewrites the durable journal, and the original conversation that
    produced the discarded state stays in the journal as evidence.
    """
    directory = Path(run_dir)
    preview = preview_retraction(directory, turn=user_turn, scope=scope)
    if preview_only:
        preview["preview_only"] = True
        preview["new_text"] = new_text
        return preview
    receipt = retract(
        directory,
        turn=user_turn,
        scope=scope,
        repository_identity=repository_identity,
    )
    # Record the edit fact so the rewind is auditable, then surface the
    # new text as the prompt a re-run should start from.
    _append_trace(
        directory,
        {
            "kind": "edit_message",
            "turn": int(user_turn or 0),
            "new_text": str(new_text),
        },
    )
    (directory / "EDIT_MESSAGE.json").write_text(
        json.dumps(
            {"user_turn": int(user_turn or 0), "new_text": str(new_text)},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        **receipt,
        "edited": True,
        "new_text": str(new_text),
        "preview": preview,
    }
