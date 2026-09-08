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
