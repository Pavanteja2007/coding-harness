"""Persistent conversation sessions for the `vex` interactive shell.

The TUI and the rich REPL used to treat every submitted line as an
isolated fix run (one Task per line, history kept only as task dirs).
This module gives `vex` an opencode/claude-style persistent session:

- ONE state file per conversation under ``<log_root>/_conversations/``,
  holding the multi-turn transcript (user + assistant turns), the raw
  input history, and a compacted summary — not one Task row per line.
- ``@path`` mention expansion against the repo's file list (the same
  ``scan_repo_files`` surface the palette searches).
- ``compact_session`` — deterministic compaction that reuses the
  harness's EXISTING recall primitive (``TraceLogger.find_events``)
  to enrich the summary, then drops old turns.
- Memory-first hooks: ``session_memory_brief`` (structural + decision
  memory queried automatically on session start) and
  ``ingest_session_facts`` (facts ingested automatically on finish),
  so no manual ``vex memory`` calls are needed.

Everything here is best-effort and total: every public function accepts
any input and never raises (a session helper must never take down a
verified fix's reporting). File IO uses atomic tmp+replace writes and
``errors="replace"`` reads so cp1252 consoles and half-written files
degrade to honest empties, never tracebacks.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "append_history",
    "append_turn",
    "compact_session",
    "conversations_dir",
    "copy_text_to_clipboard",
    "expand_at_mentions",
    "ingest_session_facts",
    "load_or_create",
    "new_session_id",
    "save_session",
    "session_memory_brief",
]

_CONVERSATIONS_DIR = "_conversations"

_MAX_TURNS = 200
_MAX_HISTORY = 200
_MAX_SNIPPET_CHARS = 6000
_MAX_SNIPPET_LINES = 60

_AT_PAT = re.compile(r"(?<!\w)@([A-Za-z0-9_./\\-]{1,120})")
_TRAILING_PUNCT = ".,;:!?)}]'\""


def new_session_id() -> str:
    """A fresh conversation id (``sess-<8 hex>``)."""
    return f"sess-{uuid.uuid4().hex[:8]}"


def conversations_dir(log_root: Any) -> Path:
    """``<log_root>/_conversations`` (created on save, never on read)."""
    return Path(log_root) / _CONVERSATIONS_DIR


def load_or_create(
    log_root: Any,
    repo: Any,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Load a conversation session file, or create a fresh one.

    Assumes log_root is a logs directory and repo the session repo.
    A missing/corrupt file yields a new session (never raises).
    """
    try:
        root = conversations_dir(log_root)
    except Exception:
        root = Path(".") / _CONVERSATIONS_DIR
    sid = (session_id or "").strip() or new_session_id()
    session: Dict[str, Any] = {
        "session_id": sid,
        "repo": str(repo or ""),
        "started_ts": time.time(),
        "turns": [],
        "history": [],
        "summary": "",
    }
    try:
        path = root / f"{sid}.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                for key in ("turns", "history"):
                    if not isinstance(data.get(key), list):
                        data[key] = []
                if not isinstance(data.get("summary"), str):
                    data["summary"] = ""
                data["session_id"] = sid
                return data
    except Exception:
        pass
    return session


def save_session(log_root: Any, session: Dict[str, Any]) -> None:
    """Persist a conversation session atomically (never raises)."""
    try:
        root = conversations_dir(log_root)
        root.mkdir(parents=True, exist_ok=True)
        sid = str((session or {}).get("session_id") or new_session_id())
        tmp = root / f"{sid}.json.tmp"
        dst = root / f"{sid}.json"
        tmp.write_text(
            json.dumps(session, ensure_ascii=False, indent=1)[:500_000],
            encoding="utf-8",
        )
        tmp.replace(dst)
    except Exception:
        pass


def append_turn(
    session: Dict[str, Any],
    role: str,
    text: str,
    task_id: Optional[str] = None,
) -> None:
    """Append one transcript turn (capped; never raises)."""
    try:
        turns = session.get("turns")
        if not isinstance(turns, list):
            turns = session["turns"] = []
        entry: Dict[str, Any] = {
            "role": str(role or "user"),
            "text": str(text or "")[:8000],
            "ts": time.time(),
        }
        if task_id:
            entry["task_id"] = str(task_id)
        turns.append(entry)
        del turns[: max(0, len(turns) - _MAX_TURNS)]
    except Exception:
        pass


def append_history(session: Dict[str, Any], line: str) -> None:
    """Append one raw input line to the session history (never raises)."""
    try:
        text = (line or "").strip()
        if not text:
            return
        hist = session.get("history")
        if not isinstance(hist, list):
            hist = session["history"] = []
        if hist and hist[-1] == text:
            return
        hist.append(text)
        del hist[: max(0, len(hist) - _MAX_HISTORY)]
    except Exception:
        pass


def expand_at_mentions(
    text: str,
    repo: Any,
    files: Optional[List[str]] = None,
    max_snippets: int = 3,
    max_chars: int = _MAX_SNIPPET_CHARS,
) -> Tuple[str, List[str]]:
    """Expand ``@path`` mentions into file-context blocks.

    Assumes text is the user's raw line and repo the session repo.
    Each ``@token`` that resolves to a repo file appends a fenced
    context block (first lines, capped); unresolvable tokens are left
    verbatim. Returns (expanded_text, inserted_relpaths). Never raises.
    """
    try:
        raw = str(text or "")
    except Exception:
        return str(text or ""), []
    if "@" not in raw:
        return raw, []
    try:
        root = Path(str(repo or ""))
    except Exception:
        return raw, []
    inserted: List[str] = []
    blocks: List[str] = []
    try:
        tokens = [m.group(1).rstrip(_TRAILING_PUNCT) for m in _AT_PAT.finditer(raw)]
    except Exception:
        return raw, []
    seen = set()
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        if len(inserted) >= max(1, max_snippets):
            break
        rel = _resolve_mention(token, root, files or [])
        if rel is None:
            continue
        snippet = _read_snippet(root, rel, max_chars=max_chars)
        if snippet is None:
            continue
        inserted.append(rel)
        blocks.append(f"--- {rel}\n{snippet}")
    if not blocks:
        return raw, []
    return raw + "\n\n@path context:\n" + "\n".join(blocks), inserted


def _resolve_mention(token: str, root: Path, files: List[str]) -> Optional[str]:
    """Resolve one @ token to a repo-relative path (None when unknown)."""
    try:
        cand = root / token
        if cand.is_file():
            try:
                return cand.relative_to(root).as_posix()
            except ValueError:
                return None
        # basename fallback: @utils.py matches src/utils.py when unique
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if files and base:
            exact = [f for f in files if f == token or f.endswith("/" + token)]
            if len(exact) == 1:
                return exact[0]
            bases = [f for f in files if f.rsplit("/", 1)[-1] == base]
            if len(bases) == 1:
                return bases[0]
            if bases:
                try:
                    from cli import fuzzy as _fz

                    ranked = _fz.rank(bases, token)
                    if ranked:
                        return ranked[0][0]
                except Exception:
                    return sorted(bases)[0]
    except Exception:
        return None
    return None


def _read_snippet(
    root: Path, rel: str, max_chars: int = _MAX_SNIPPET_CHARS
) -> Optional[str]:
    """First lines of a repo file (None when unreadable/binary)."""
    try:
        data = (root / rel).read_bytes()[: max(256, max_chars)]
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()[:_MAX_SNIPPET_LINES]
    snippet = "\n".join(lines).strip()
    if not snippet:
        return None
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n... [truncated]"
    return snippet


def compact_session(
    session: Dict[str, Any],
    log_root: Any,
    keep_last: int = 12,
) -> str:
    """Compact the transcript: summarize old turns, keep recent ones.

    The summary is deterministic (turn head + task outcomes) and is
    enriched with the harness's EXISTING recall primitive —
    ``TraceLogger.find_events`` over each referenced task's trace —
    so compacted-away tool detail stays retrievable via RECALL.
    Returns the summary string ("" when nothing to compact). Never raises.
    """
    try:
        turns = session.get("turns")
        if not isinstance(turns, list) or len(turns) <= max(1, keep_last):
            return str(session.get("summary") or "")
        old = turns[: len(turns) - max(1, keep_last)]
        recent = turns[len(turns) - max(1, keep_last) :]
        bits: List[str] = []
        prev_summary = str(session.get("summary") or "").strip()
        if prev_summary:
            bits.append(f"prior: {prev_summary[:1500]}")
        for turn in old:
            try:
                role = str(turn.get("role") or "?")
                text = str(turn.get("text") or "").replace("\n", " ").strip()
                tid = str(turn.get("task_id") or "")
                head = text[:160]
                bits.append(f"{role}{(' ' + tid) if tid else ''}: {head}")
            except Exception:
                continue
        # Enrich with the existing recall primitive: one RECALL-style
        # lookup per referenced task (result/verify outcomes only).
        recall_notes = _recall_task_outcomes(old, log_root)
        if recall_notes:
            bits.append("recalled outcomes: " + "; ".join(recall_notes)[:1500])
        summary = "\n".join(bits)[:4000]
        session["summary"] = summary
        session["turns"] = recent
        try:
            save_session(log_root, session)
        except Exception:
            pass
        return summary
    except Exception:
        return ""


def _recall_task_outcomes(turns: List[Any], log_root: Any) -> List[str]:
    """Outcome lines for referenced tasks via TraceLogger.find_events."""
    out: List[str] = []
    try:
        tids: List[str] = []
        for turn in turns:
            try:
                tid = str((turn or {}).get("task_id") or "")
            except Exception:
                continue
            if tid and tid not in tids:
                tids.append(tid)
        from harness.trace import TraceLogger

        for tid in tids[:5]:
            try:
                logger = TraceLogger(Path(log_root) / tid)
                for entry in logger.find_events("result verify", limit=3):
                    try:
                        kind = str(entry.get("kind") or "?")
                        data = str(entry.get("data") or "")[:160]
                        out.append(f"{tid} {kind}: {data}")
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return out


def session_memory_brief(repo: Any, log_root: Any, limit: int = 5) -> List[str]:
    """Automatic memory context for a fresh session (never raises).

    Queries decision memory (repo-scoped recent rows) and structural
    memory (the persisted code-graph index, load-only so session start
    stays instant). Returns short human lines; [] when nothing applies.
    """
    lines: List[str] = []
    try:
        from memory.decision_store import open_default_store

        try:
            store = open_default_store()
        except Exception:
            store = None
        if store is not None:
            try:
                rows = store.search(
                    "", limit=max(1, min(int(limit), 10)), repo_path=str(repo or "")
                )
                if not rows:
                    rows = store.search("", limit=3)
                for row in rows[: max(1, min(int(limit), 10))]:
                    text = str(getattr(row, "text", "") or "").strip()
                    if text:
                        lines.append(f"memory: {text[:160]}")
            except Exception:
                pass
            try:
                store.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        from memory.code_graph import CodeGraph

        try:
            graph = CodeGraph(str(repo or "")).load()
        except Exception:
            graph = None
        if graph is not None:
            try:
                files = int(getattr(graph, "file_count", 0) or 0)
                nodes = len(getattr(graph, "nodes", {}) or {})
                if files:
                    lines.append(f"structure: {files} files, {nodes} symbols indexed")
            except Exception:
                pass
    except Exception:
        pass
    return lines[:10]


def ingest_session_facts(
    log_root: Any,
    task_id: str,
    issue: str,
    repo: Any,
    status: str,
) -> None:
    """Ingest one finished run's facts into memory (never raises).

    Best-effort Boundary-4 poll (picks up the run's state.json
    decisions) plus one session fact row, so future sessions recall
    this one without any manual ``vex memory`` call.
    """
    try:
        tid = str(task_id or "").strip()
        if not tid:
            return
        try:
            from memory.decision_store import open_default_store

            store = open_default_store()
        except Exception:
            store = None
        if store is not None:
            try:
                store.poll(str(log_root))
            except Exception:
                pass
            try:
                short_issue = str(issue or "")[:160].replace("\n", " ")
                store.record(
                    text=f"vex session {tid}: {short_issue} -> {status}",
                    category="session",
                    source="session",
                    task_id=tid,
                    repo_path=str(repo or "") or None,
                )
            except Exception:
                pass
            try:
                store.close()
            except Exception:
                pass
    except Exception:
        pass


def copy_text_to_clipboard(text: str) -> bool:
    """Copy text to the OS clipboard (True on success; never raises).

    Uses platform clipboard utilities (no new dependency): Windows
    ``clip``, macOS ``pbcopy``, Linux ``xclip``/``xsel``. Falls back
    to False so callers print the text instead.
    """
    try:
        import subprocess

        data = (text or "").encode("utf-8", errors="replace")
        if not data:
            return False
        import os

        if os.name == "nt":
            proc = subprocess.run(["clip"], input=data, capture_output=True, timeout=10)
            return proc.returncode == 0
        import shutil

        if shutil.which("pbcopy"):
            proc = subprocess.run(
                ["pbcopy"], input=data, capture_output=True, timeout=10
            )
            return proc.returncode == 0
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard"]):
            if shutil.which(cmd[0]):
                proc = subprocess.run(cmd, input=data, capture_output=True, timeout=10)
                if proc.returncode == 0:
                    return True
        return False
    except Exception:
        return False
