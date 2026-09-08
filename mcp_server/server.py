"""MCP server exposing the project memory layer (INTERFACES.md Boundary 5).

Tools:
    query_structure(query, repo)    — code-graph lookups ("callers of X", ...)
    query_decisions(query)          — decision/pattern memory search
    record_decision(text, category) — append to decision memory
    task_status(task_id)            — logs/{task_id}/state.json summary
    list_repos()                    — code-graph indexes available on disk

Design notes:
- query_structure takes an optional repo path; without it, the most
  recently used graph index is reused (tracked in a pointer file under
  HARNESS_HOME/code-graph/). This keeps calls from a generic MCP client
  frictionless while staying multi-repo capable.
- Lazy decision-store ingestion: every query_decisions call first polls
  the logs dir for state.json updates (cheap and idempotent via the
  unique index) — no background watcher thread in the server.
- The server is a plain library object (FastMCP) with a main() — any MCP
  client (Claude Code, Cursor, or this project's tests) connects over
  stdio; nothing here is specific to this repo's CLI.

Assumes: HARNESS_HOME (or cwd) is stable for the process lifetime; repo
paths passed by clients must be local (this is a local memory server).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

try:  # MCP Python SDK 2.x (FastMCP was renamed MCPServer)
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from memory.code_graph import CodeGraph
from memory.decision_store import DecisionStore, format_decisions
from memory.paths import decisions_db_path, default_logs_dir, harness_home

mcp = _Server("harness-memory")

_graph_lock = threading.Lock()
_graph_cache = {}  # repo path str -> CodeGraph (graph objects are read-only)


def _graph_root() -> Path:
    return harness_home() / "code-graph"


def _record_last_repo(repo_path: str) -> None:
    """Persist the most recently queried repo (repo-less queries reuse it)."""
    try:
        root = _graph_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "last_repo").write_text(repo_path, encoding="utf-8")
    except OSError:
        pass


def _default_repo() -> Optional[str]:
    try:
        return (_graph_root() / "last_repo").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _get_graph(repo: Optional[str]) -> Optional[CodeGraph]:
    """CodeGraph for `repo` (or the last-used repo when None), building or
    loading its index on first use. Returns None when no repo is known."""
    path = repo or _default_repo()
    if not path:
        return None
    resolved = str(Path(path).expanduser().resolve())
    with _graph_lock:
        cg = _graph_cache.get(resolved)
        if cg is None:
            if not Path(resolved).is_dir():
                return None
            cg = CodeGraph(resolved, root=str(_graph_root()))
            cg.load_or_build()
            _graph_cache[resolved] = cg
        _record_last_repo(resolved)
        return cg


# ---------------------------------------------------------------------------
# Boundary 5 tools
# ---------------------------------------------------------------------------

@mcp.tool()
def query_structure(query: str, repo: str = "") -> str:
    """Query the structural code graph (functions, classes, imports, calls).

    Query verbs: 'symbol <name>', 'callers <name>', 'callees <name>',
    'importers <module>', 'imports <module>', 'file <path>', 'files',
    'symbols [pattern]', 'help'. Examples: "callers run_task",
    "symbol CodeGraph", "imports harness.core".

    Args:
        query: structural query (see help verb).
        repo: optional local repo path (defaults to the last-used repo).
    """
    cg = _get_graph(repo or None)
    if cg is None:
        return (
            "no code-graph index available — pass repo=<path> once to index "
            "a repository (query_structure 'help', '<abs repo path>')"
        )
    return cg.query(query)


@mcp.tool()
def query_decisions(query: str = "") -> str:
    """Search persistent decision/pattern memory: architecture decisions,
    conventions, past bugs and harness learnings across tasks/sessions.

    Also ingests any new structured state files (logs/*/state.json) before
    answering, so memory is fresh. An empty query returns recent entries.

    Args:
        query: keyword query (ranked by match count, then recency).
    """
    store = DecisionStore(str(decisions_db_path()))
    try:
        store.poll(str(default_logs_dir()))
    except Exception:
        pass  # logs dir may not exist yet — that's fine
    results = store.search(query or "", limit=25)
    return format_decisions(results, query or "")


@mcp.tool()
def record_decision(text: str, category: str = "general") -> str:
    """Record a fact in decision/pattern memory for future tasks and
    sessions. Use for architecture decisions, conventions, bug post-
    mortems, gotchas — anything worth remembering across sessions.

    Args:
        text: the decision/fact as a self-contained sentence.
        category: optional grouping ('general', 'architecture', 'bug', ...).
    """
    store = DecisionStore(str(decisions_db_path()))
    rid = store.record(text, category=category, source="mcp")
    if rid is None:
        return "error: empty decision text"
    return f"recorded decision #{rid}"


# ---------------------------------------------------------------------------
# Extra tools (spec item 31: expose memory queries AND task status)
# ---------------------------------------------------------------------------

@mcp.tool()
def task_status(task_id: str) -> str:
    """Summarize one task's structured state (plan, completed steps,
    decisions, remaining steps) from logs/{task_id}/state.json.

    Args:
        task_id: the task id shown by the harness CLI.
    """
    state_file = default_logs_dir() / task_id / "state.json"
    if not state_file.is_file():
        return f"no state file for task {task_id!r} under {default_logs_dir()}"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read state file: {exc}"

    plan = state.get("plan") or []
    completed = state.get("completed_steps") or []
    remaining = state.get("remaining_plan") or []
    lines = [f"task {state.get('task_id', task_id)}:"]
    lines.append(f"  steps: {len(completed)}/{len(plan)} complete")
    for step in plan:
        lines.append(f"  [{'x' if step in completed else ' '}] {step}")
    if state.get("files_touched"):
        lines.append("  files touched: " + ", ".join(state["files_touched"]))
    for d in state.get("decisions") or []:
        lines.append(f"  decision: {d}")
    if remaining:
        lines.append("  remaining:")
        for r in remaining:
            lines.append(f"    - {r}")
    return "\n".join(lines)


@mcp.tool()
def list_repos() -> str:
    """List repositories with code-graph indexes under HARNESS_HOME, with
    the last-queried one marked."""
    root = _graph_root()
    if not root.is_dir():
        return "no code-graph indexes built yet (call query_structure with a repo)"
    last = _default_repo() or ""
    entries: List[str] = []
    for meta in sorted(root.glob("*/meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        repo = data.get("repo_path", "?")
        mark = "  <- last used" if repo == last else ""
        entries.append(f"- {repo} ({data.get('file_count', '?')} files){mark}")
    return "\n".join(entries) or "no code-graph indexes built yet"


def main() -> None:
    """Run the MCP server over stdio (the standard local transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
