"""Decision/pattern memory: a persistent store of learned facts.

Architecture decisions, conventions, past bugs, harness learnings —
anything worth remembering across tasks and sessions. Backed by SQLite
(one file, stdlib, zero extra deps). Three ingestion paths:

1. Manual / MCP: ``record(text, ...)`` — the ``record_decision`` MCP tool.
2. Boundary 4 ingestion: ``ingest_state_file`` / ``poll`` — reads the
   ``decisions`` field from Terminal 1's structured state files at
   ``logs/{task_id}/state.json`` (schema owned by harness/context.py).
3. ``watch`` — background polling loop that ingests new state files as
   they appear (for long-running processes; the MCP server instead polls
   lazily on each query, which is self-healing and thread-free).

Deduplication: ingestion uses a unique constraint on (task_id, text) so
re-reading a state.json that grew more decisions is a no-op for the ones
already stored. Manual/MCP records are never deduped (a human may intend
to repeat one).

Search: query words are matched case-insensitively against the text
(LIKE-based, no FTS dependency); rows are ranked by how many query words
they match, then by recency. An empty query returns the most recent
decisions.

Assumes: one store file per project; concurrent access is serialized by
an in-process lock (WAL mode keeps cross-process readers non-blocking
for the common read-heavy MCP workload).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'manual',
    task_id TEXT,
    repo_path TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_text
    ON decisions(task_id, text)
    WHERE task_id IS NOT NULL;
"""


@dataclass
class Decision:
    """One stored fact."""

    id: int
    text: str
    category: str
    source: str
    task_id: Optional[str]
    repo_path: Optional[str]
    created_at: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "category": self.category,
            "source": self.source,
            "task_id": self.task_id,
            "repo_path": self.repo_path,
            "created_at": self.created_at,
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class DecisionStore:
    """Persistent decision/pattern memory (one SQLite file).

    Thread-safe: every method takes an internal lock (the MCP server may
    serve requests from different threads). Assumes the DB file's parent
    directory is writable.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path).resolve())
        self._lock = threading.Lock()
        self._conn = _connect(self.db_path)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- write ---------------------------------------------------------

    def record(
        self,
        text: str,
        category: str = "general",
        source: str = "manual",
        task_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        dedupe: bool = False,
    ) -> Optional[int]:
        """Insert one decision; returns its row id, or None if deduped.

        Assumes `text` is a self-contained sentence. `source` marks the
        origin: "manual", "mcp", or "state-file" (Boundary 4 ingestion).
        With dedupe=True and a task_id, an identical (task_id, text) row
        already present makes this a silent no-op.
        """
        text = (text or "").strip()
        if not text:
            return None
        now = _utc_now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO decisions (text, category, source, task_id, repo_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (text, category, source, task_id, repo_path, now),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # (task_id, text) already ingested — state files are
                # rewritten as they grow; re-ingestion must be a no-op.
                return None

    # -- read ----------------------------------------------------------

    def search(
        self,
        query: str = "",
        limit: int = 20,
        repo_path: Optional[str] = None,
    ) -> List[Decision]:
        """Decisions matching `query`, best-ranked first.

        Empty/blank query returns the most recent decisions. Matching is
        case-insensitive substring per query word; rank = number of query
        words matched, then recency. With `repo_path`, only decisions
        recorded for that same repo are returned (compared as resolved
        absolute paths — the harness records repo_path at task start, so
        planning-time queries can scope to "decisions made in THIS repo");
        decisions without a repo_path are excluded when the filter is on.
        Assumes repo_path is a real filesystem path or None.
        """
        limit = max(1, min(int(limit), 500))
        words = _query_words(query)
        repo_key = _repo_key(repo_path) if repo_path else None
        with self._lock:
            if not words:
                if repo_key:
                    # python-side filtering: stored rows may hold the same
                    # repo as a relative/unnormalized path — the normcase
                    # +resolve key must compare them (see _repo_key).
                    rows = self._conn.execute(
                        "SELECT * FROM decisions ORDER BY id DESC"
                    ).fetchall()
                    picked = [r for r in rows if _repo_key(r["repo_path"]) == repo_key]
                    return [_row_to_decision(r) for r in picked[:limit]]
                rows = self._conn.execute(
                    "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [_row_to_decision(r) for r in rows]

            scores: Dict[int, tuple] = {}
            for row in self._conn.execute("SELECT * FROM decisions"):
                if repo_key and _repo_key(row["repo_path"]) != repo_key:
                    continue
                text_l = " " + (row["text"] or "").lower() + " "
                matched = sum(1 for w in words if w in text_l)
                if matched:
                    scores[int(row["id"])] = (matched, int(row["id"]))
            if not scores:
                return []
            # rank: words matched desc, then recency (id) desc
            ranked_ids = sorted(scores, key=lambda i: (-scores[i][0], -scores[i][1]))
            if len(ranked_ids) > 500:  # keep the SQL fetch bounded
                ranked_ids = ranked_ids[:500]
            placeholders = ",".join("?" * min(len(ranked_ids), 500))
            rows = self._conn.execute(
                f"SELECT * FROM decisions WHERE id IN ({placeholders})", ranked_ids
            ).fetchall()
            by_id = {int(r["id"]): r for r in rows}
            out = []
            for i in ranked_ids[:limit]:
                if i in by_id:
                    out.append(_row_to_decision(by_id[i]))
            return out

    def count(self, source: Optional[str] = None) -> int:
        """Total stored decisions (optionally filtered by source)."""
        with self._lock:
            if source:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE source = ?", (source,)
                )
            else:
                cur = self._conn.execute("SELECT COUNT(*) FROM decisions")
            return int(cur.fetchone()[0])

    def get(self, decision_id: int) -> Optional[Decision]:
        """One decision by row id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (int(decision_id),)
            ).fetchone()
        return _row_to_decision(row) if row else None

    # -- Boundary 4 ingestion -------------------------------------------

    def ingest_state_file(self, path: str) -> int:
        """Ingest the `decisions` list from one structured state file.

        Assumes `path` is a JSON file in the Boundary 4 schema
        (logs/{task_id}/state.json written by harness.context). Malformed
        or missing files are skipped silently (a half-written state file
        must never crash ingestion — the writer guarantees atomicity, but
        be defensive anyway). Returns the number of NEW rows ingested.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        task_id = data.get("task_id") or Path(path).parent.name
        decisions = data.get("decisions")
        if not isinstance(decisions, list):
            return 0
        repo_path = data.get("repo_path")
        new = 0
        for entry in decisions:
            if not isinstance(entry, str) or not entry.strip():
                continue
            rid = self.record(
                text=entry,
                category="task",
                source="state-file",
                task_id=str(task_id),
                repo_path=str(repo_path) if repo_path else None,
                dedupe=True,
            )
            if rid is not None:
                new += 1
        return new

    def poll(self, logs_dir: str) -> int:
        """Ingest every state.json under `logs_dir` once (recursive).

        Idempotent (unique index on task_id+text). Returns count of NEW
        rows ingested. Assumes the harness layout: one subdirectory per
        task holding state.json — but real runs nest deeper (e.g.
        benchmark drivers that stage task logs under
        ``<logs>/ablations/<run>/tasklogs/<task_id>/state.json``), so any
        depth is scanned. Re-ingestion of archived ``{task_id}.old-*``
        dirs is a no-op for texts already stored.
        """
        root = Path(logs_dir)
        if not root.is_dir():
            return 0
        new = 0
        for state in sorted(root.rglob("state.json")):
            new += self.ingest_state_file(str(state))
        return new

    def watch(self, logs_dir: str, interval_s: float = 2.0, stop_event=None) -> None:
        """Poll `logs_dir` forever (until stop_event is set) — for
        long-running processes that want live ingestion.

        Assumes interval_s > 0.2; a polling loop is deliberate (watchdog
        would be a dependency for cross-platform inotify on Windows).
        """
        interval_s = max(float(interval_s), 0.2)
        while stop_event is None or not stop_event.is_set():
            try:
                self.poll(logs_dir)
            except Exception:
                pass  # a bad scan must never kill the watcher
            if stop_event is None:
                time.sleep(interval_s)
            else:
                stop_event.wait(interval_s)

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Close the DB connection (calls at process exit are fine)."""
        with self._lock:
            self._conn.close()


def _repo_key(repo_path: Optional[str]) -> Optional[str]:
    """Canonical comparison key for a repo_path (resolved absolute, OS
    case-normalized) so planner-time queries match recorded rows even when
    one side passed a relative path and the other an absolute one. Returns
    None for empty/None input (callers treat that as 'no filter')."""
    if not repo_path:
        return None
    try:
        return os.path.normcase(str(Path(repo_path).resolve()))
    except OSError:
        return os.path.normcase(str(repo_path))


def open_default_store() -> "DecisionStore":
    """The shared decision store at memory.paths.decisions_db_path().

    The single opener every consumer (harness planner queries, MCP server,
    CLI) should use so they all read/write ONE database. Assumes the
    process has the usual filesystem permissions for HARNESS_HOME.
    """
    from memory.paths import decisions_db_path

    return DecisionStore(str(decisions_db_path()))


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=int(row["id"]),
        text=row["text"],
        category=row["category"],
        source=row["source"],
        task_id=row["task_id"],
        repo_path=row["repo_path"],
        created_at=row["created_at"],
    )


def _query_words(query: str) -> List[str]:
    """Lowercased alphanumeric words from the query, >=2 chars, minus
    stop words that match everything and rank nothing."""
    stop = {
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "what",
        "which",
        "how",
        "why",
        "and",
        "or",
        "did",
        "do",
        "does",
        "was",
        "were",
        "that",
        "this",
        "it",
        "we",
        "i",
        "me",
        "my",
    }
    words = re.findall(r"[a-z0-9_]+", (query or "").lower())
    return [w for w in words if len(w) >= 2 and w not in stop] or words


def format_decisions(decisions: List[Decision], query: str = "") -> str:
    """Render decisions as the human-readable string returned by MCP /
    CLI. Assumes decisions are already ranked; caps at a sane length."""
    if not decisions:
        return "no matching decisions" + (f" for {query!r}" if query else "")
    lines: List[str] = []
    for d in decisions:
        origin = f" [{d.source}]" if d.source != "manual" else ""
        task = f" task:{d.task_id}" if d.task_id else ""
        lines.append(f"- {d.text}{origin}{task}")
    return "\n".join(lines)
