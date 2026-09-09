"""Filesystem location conventions for Terminal 4's memory layer.

One place decides where the decision DB and code-graph indexes live, so
the CLI and the MCP server agree (and tests can override via env).

Defaults (Phase 1, running from a project checkout):

    HARNESS_HOME              (default: ./.harness under the process cwd)
    HARNESS_DECISIONS_DB      (default: $HARNESS_HOME/memory/decisions.db)

Assumes the process is launched from a stable working directory (the
harness project root); MCP clients that launch the server from elsewhere
should set HARNESS_HOME or pass an explicit path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def harness_home() -> Path:
    """Root directory for harness-owned state (never committed)."""
    env = os.environ.get("HARNESS_HOME")
    return Path(env).expanduser().resolve() if env else (Path.cwd() / ".harness")


def decisions_db_path() -> Path:
    """SQLite file backing DecisionStore."""
    env = os.environ.get("HARNESS_DECISIONS_DB")
    if env:
        return Path(env).expanduser().resolve()
    return harness_home() / "memory" / "decisions.db"


def default_logs_dir() -> Path:
    """Where harness.core writes structured state (Task log root)."""
    env = os.environ.get("HARNESS_LOGS_DIR")
    return Path(env).expanduser().resolve() if env else Path.cwd() / "logs"


# ---------------------------------------------------------------------------
# Task-id containment guard (Round 6 adversarial hardening)
# ---------------------------------------------------------------------------

_BAD_TASK_ID_CHARS = set('/\\:*?"<>|\x00')


def is_safe_task_id(task_id: str) -> bool:
    """True iff `task_id` is safe to join onto a logs root as ONE segment.

    Blocks every traversal/escape form (all verified live on Windows):
    - separators ``/`` ``\\`` and null bytes
    - drive/UNC forms via ``:`` (``C:`` is DRIVE-RELATIVE on Win32 —
      ``Path(base) / 'C:evil'`` discards the base entirely)
    - edge whitespace/dot tricks: Win32 path normalization strips
      leading/trailing spaces and trailing dots, so ``' ..'`` resolves
      AS ``..`` and ``'x.'`` aliases ``x`` — both directions rejected
    - ``.`` / ``..`` / all-dot segments
    Interior spaces/unicode are allowed (legitimate user-chosen ids);
    anything rejected simply cannot name a real task directory.
    """
    if not isinstance(task_id, str) or not task_id:
        return False
    if any(c in _BAD_TASK_ID_CHARS for c in task_id):
        return False
    if task_id != task_id.strip():
        return False  # edge whitespace smuggles '..' past Win32 normalize
    # Win32-equivalent segment: strip trailing dots/spaces, then check
    normalized = task_id.rstrip(". ")
    if not normalized or normalized in (".", ".."):
        return False
    return True


def safe_task_dir(task_id: str, logs_root: Optional[Path] = None) -> Optional[Path]:
    """logs_root/<task_id>/ when `task_id` is a single safe segment, else
    None. Belt-and-suspenders: even a pattern-allowed id must RESOLVE
    inside the logs root (raises nothing; returns None on any doubt)."""
    if not is_safe_task_id(task_id):
        return None
    root = Path(logs_root) if logs_root is not None else default_logs_dir()
    d = root / task_id
    try:
        d.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return d
