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
  the logs dir for state.json updates (cheap, repo-scoped, and idempotent)
  — no background watcher thread in the server.
- The server is a plain library object (FastMCP) with a main() — any MCP
  client (Claude Code, Cursor, or this project's tests) connects over
  stdio; nothing here is specific to this repo's CLI.

Assumes: HARNESS_HOME (or cwd) is stable for the process lifetime; repo
paths passed by clients must be local (this is a local memory server).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional

try:  # MCP Python SDK 2.x (FastMCP was renamed MCPServer)
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from memory.code_graph import CodeGraph
from memory.decision_store import DecisionStore, format_decisions, redact_secrets
from memory.paths import (
    decisions_db_path,
    default_logs_dir,
    harness_home,
    safe_state_file,
    safe_task_dir,
)
from shared import tracing
from shared.security import (
    authorize_memory_write,
    review_untrusted_source,
    taint_wrap,
)

mcp = _Server("harness-memory")

_graph_lock = threading.Lock()
_graph_cache = {}
_graph_lifecycle_lock = threading.Lock()
_SERVER_TOOL_NAMES = (
    "query_structure",
    "query_decisions",
    "record_decision",
    "task_status",
    "list_repos",
)
# Per-tool side-effect classes for the Ceiling-12 least-privilege policy.
# `record_decision` is the only mutating tool; the three readers and the
# local-only lister are read-only. A tool absent from this map is treated as
# mutating by `mcp_server.namespace`, so adding a tool without classifying it
# fails toward the more restrictive side.
_SERVER_TOOL_SIDE_EFFECTS = {
    "query_structure": "read",
    "query_decisions": "search",
    "task_status": "read",
    "list_repos": "read",
    "record_decision": "mutation",
}
_SERVER_TOOL_DESCRIPTIONS = {
    "query_structure": "Query the structural code graph (functions, classes, imports, calls).",
    "query_decisions": "Search decision memory, optionally restricted to one repository.",
    "record_decision": "Record a fact in decision memory, optionally scoped to a repository.",
    "task_status": "Summarize one task's structured state from logs/{task_id}/state.json.",
    "list_repos": "List repositories with code-graph indexes under HARNESS_HOME.",
}
_server_events: List["ServerLifecycleEvent"] = []


@dataclass(frozen=True)
class ServerLifecycleEvent:
    """One typed observation of the local MCP server lifecycle."""

    phase: str
    sequence: int
    timestamp: float = field(default_factory=time.time)
    tool: str = ""
    ok: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible event projection."""
        return {
            "schema_version": 1,
            "phase": self.phase,
            "sequence": self.sequence,
            "timestamp": round(self.timestamp, 6),
            "tool": self.tool,
            "ok": self.ok,
            "metadata": dict(self.metadata),
        }


def _emit_server_event(
    phase: str,
    *,
    tool: str = "",
    ok: bool = True,
    metadata: Optional[Mapping[str, Any]] = None,
) -> ServerLifecycleEvent:
    with _graph_lifecycle_lock:
        event = ServerLifecycleEvent(
            phase=str(phase),
            sequence=len(_server_events) + 1,
            tool=str(tool or ""),
            ok=bool(ok),
            metadata=dict(metadata or {}),
        )
        _server_events.append(event)
        if len(_server_events) > 256:
            del _server_events[:-256]
        return event


def server_lifecycle_events() -> list[dict[str, Any]]:
    """Return typed server lifecycle events without exposing tool content."""
    with _graph_lifecycle_lock:
        return [event.as_dict() for event in _server_events]


def server_health() -> dict[str, Any]:
    """Return local transport health and the fixed five-tool capability set."""
    with _graph_lifecycle_lock:
        last = _server_events[-1].as_dict() if _server_events else {}
        event_count = len(_server_events)
    return {
        "schema_version": 1,
        "status": "ready",
        "transport": "stdio",
        "tools": sorted(_SERVER_TOOL_NAMES),
        "tool_count": len(_SERVER_TOOL_NAMES),
        "event_count": event_count,
        "last_event": last,
    }


health = server_health
health_check = server_health
lifecycle_events = server_lifecycle_events


def server_name() -> str:
    """Return this server's namespace label used in namespaced tool ids.

    The label is what appears in ``mcp__<server>__<tool>`` identifiers, so it
    is one constant rather than a string repeated at each call site.
    """
    return "harness-memory"


def server_tool_definitions():
    """Return the five tools as namespaced, hash-pinned tool definitions.

    Each definition carries the side-effect class the least-privilege policy
    reads and a SHA-256 digest over ``(server, tool, description,
    inputSchema)``. A client that pins those digests at approval can re-hash
    them immediately before a call and refuse a definition that changed after
    the operator approved it (see :class:`mcp_server.namespace.ToolPinSet`).
    """
    from mcp_server import namespace

    return namespace.tools_for_server(
        server_name(),
        [
            namespace.ToolDefinition(
                server=server_name(),
                tool=name,
                description=_SERVER_TOOL_DESCRIPTIONS.get(name, ""),
                input_schema={"type": "object"},
                side_effect_class=_SERVER_TOOL_SIDE_EFFECTS.get(name, ""),
            )
            for name in _SERVER_TOOL_NAMES
        ],
    )


def server_tool_catalog(policy: Any = None) -> list[dict[str, Any]]:
    """Return the namespaced JSON catalog for this server.

    With no policy every tool is listed. With an
    :class:`mcp_server.namespace.MCPToolPolicy`, only the tools the session
    may actually call are listed, so what a client renders and what it may
    execute are the same list rather than two that can drift.
    """
    from mcp_server import namespace

    return namespace.namespaced_catalog(
        server_name(), server_tool_definitions(), policy=policy
    )


def server_tool_pins() -> dict[str, Any]:
    """Return the approval-time pin record for this server's tools."""
    from mcp_server import namespace

    return namespace.ToolPinSet.from_catalog(server_tool_definitions()).as_dict()


def _safe_task_dir(task_id: str):
    """logs/<task_id>/ for a single-segment task id, else None (containment
    guard for task_status — Round 6 adversarial hardening)."""
    return safe_task_dir(task_id)


def _graph_root() -> Path:
    return harness_home() / "code-graph"


def _record_last_repo(repo_path: str) -> None:
    """Persist the most recently queried repo atomically."""
    try:
        root = _graph_root()
        root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".last_repo.", suffix=".tmp", dir=str(root)
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(repo_path)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, root / "last_repo")
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _default_repo() -> Optional[str]:
    try:
        return (_graph_root() / "last_repo").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _get_graph(repo: Optional[str]) -> Optional[CodeGraph]:
    """CodeGraph for `repo` (or the last-used repo when None), refreshing
    its persisted index on every query. Returns None when no repo is known."""
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
            _graph_cache[resolved] = cg
        cg.load_or_build()
        _record_last_repo(resolved)
        return cg


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


# ---------------------------------------------------------------------------
# Boundary 5 tools
#
# TRUST BOUNDARY: everything these tools RETURN is untrusted content as far as
# the calling agent is concerned. A memory row can hold text an attacker
# wrote through this same server, and a structural query can return
# docstring first lines from an arbitrary repository. Both are reviewed by
# shared.security before they are serialized to the client and carry a
# visible taint banner, so a client cannot mistake a stored instruction for
# an operator instruction. Memory WRITES are gated separately and fail
# closed: a row that claims system-level authority is quarantined, never
# stored.
# ---------------------------------------------------------------------------


def _guard_mcp_result(payload: Any, *, source: str, mode: Optional[str] = None) -> str:
    """Review one MCP result payload and return client-safe text.

    Fails closed: a result the boundary quarantines is replaced with an
    explicit refusal naming the source, never with the hostile content. Never
    raises — a hostile row must not crash the tool.
    """
    review = review_untrusted_source(payload, source=source, mode=mode)
    if review.blocked:
        return (
            f"[UNTRUSTED SOURCE: {source}] {review.text} "
            f"(refused: {review.severity} injection indicators)"
        )
    if review.tainted:
        return taint_wrap(review)
    return review.text


@mcp.tool()
def query_structure(query: str, repo: str = "") -> str:
    """Query the structural code graph (functions, classes, imports, calls).

    Query verbs: 'symbol <name>', 'callers <name>', 'callees <name>',
    'importers <module>', 'imports <module>', 'file <path>', 'files',
    'symbols [pattern]', 'help'. Examples: "callers run_task",
    "symbol CodeGraph", "imports harness.core".

    The rendered result is untrusted repository content and is reviewed
    before it is returned; flagged output carries a visible taint banner.

    Args:
        query: structural query (see help verb).
        repo: optional local repo path (defaults to the last-used repo).
    """
    _emit_server_event("tool_call", tool="query_structure")
    cg = _get_graph(repo or None)
    if cg is None:
        return (
            "no code-graph index available — pass repo=<path> once to index "
            "a repository (query_structure 'help', '<abs repo path>')"
        )
    return _guard_mcp_result(cg.query(redact_secrets(query)), source="mcp")


@mcp.tool()
def query_decisions(query: str = "", repo_path: str = "") -> str:
    """Search decision memory, optionally restricted to one repository.

    Also ingests new structured state files before answering. ``repo_path``
    is optional for backward-compatible global queries; repository-aware
    clients should always provide it.

    The rendered rows are untrusted memory content and are reviewed before
    they are returned; a row that tries to issue instructions is refused
    rather than rendered.
    """
    _emit_server_event("tool_call", tool="query_decisions")
    store = DecisionStore(str(decisions_db_path()))
    try:
        try:
            store.poll(str(default_logs_dir()))
        except Exception:
            pass
        if repo_path:
            results = store.search(query or "", limit=25, repo_path=repo_path)
        else:
            results = store.search(query or "", limit=25)
        try:
            tracing.emit_run(
                "mcp",
                "memory_query",
                run_id="mcp-queries",
                tool="query_decisions",
                query=redact_secrets(query or "")[:200],
                repo_path=redact_secrets(repo_path)[:300],
                matched=len(results),
            )
        except Exception:
            pass
        return _guard_mcp_result(
            format_decisions(results, query or ""), source="memory"
        )

    finally:
        try:
            store.close()
        except Exception:
            pass


@mcp.tool()
def record_decision(
    text: str,
    category: str = "general",
    repo_path: str = "",
    session_id: str = "",
    model: str = "",
    dedupe: bool = True,
) -> str:
    """Record a fact in decision memory, optionally scoped to a repository.

    MEMORY WRITE GATING: a write is refused unless it carries provenance
    and does not claim system-level authority. A row that reads as an
    instruction ("ignore previous instructions", "you are now ...") is
    quarantined and never stored, whatever its category — a stored
    instruction is a persistent prompt-injection channel.

    PROVENANCE: ``session_id`` and ``model`` are optional and additive. When
    supplied they are recorded on the row together with the capture timestamp
    and the repository, so an external client can say WHERE a row came from and
    a later session can tell a client-recorded convention from a harness one.
    ``dedupe`` (default true) makes an identical normalized
    ``(repo_path, category, text)`` row a no-op that returns the existing id
    rather than a second copy. The check is local because the store's own
    dedupe is scoped to a task id, which this tool does not carry.
    """
    _emit_server_event("tool_call", tool="record_decision")
    provenance = {
        "kind": "mcp-tool",
        "source": "mcp",
        "actor": "mcp-client",
        "category": str(category or "general"),
    }
    if str(session_id or "").strip():
        provenance["session_id"] = str(session_id).strip()
    if str(model or "").strip():
        provenance["model"] = str(model).strip()
    if str(repo_path or "").strip():
        provenance["repo_path"] = str(repo_path).strip()
    provenance["timestamp"] = time.time()
    decision = authorize_memory_write(
        text,
        source="mcp",
        actor="mcp-client",
        provenance=provenance,
    )
    if decision.quarantined:
        _emit_server_event(
            "memory_write_refused",
            tool="record_decision",
            ok=False,
            metadata={"reason": decision.reason, "severity": decision.severity},
        )
        return f"error: decision quarantined ({decision.reason})"
    store = DecisionStore(str(decisions_db_path()))
    try:
        if dedupe:
            # The store's own dedupe is scoped to a task id, and this tool
            # records no task id, so it cannot deduplicate across calls. The
            # check has to happen here; the store call therefore keeps its
            # historical argument shape exactly.
            existing = _find_duplicate(store, decision.text, str(category), repo_path)
            if existing is not None:
                return f"already recorded decision #{existing}"
        if repo_path:
            rid = store.record(
                decision.text,
                category=category,
                source="mcp",
                repo_path=repo_path,
                provenance=dict(decision.provenance),
            )
        else:
            rid = store.record(
                decision.text,
                category=category,
                source="mcp",
                provenance=dict(decision.provenance),
            )
        if rid is None:
            return "error: decision rejected or empty"
        return f"recorded decision #{rid}"
    finally:
        try:
            store.close()
        except Exception:
            pass


def _find_duplicate(
    store: Any, text: str, category: str, repo_path: str
) -> Optional[int]:
    """Return the id of an identical stored row, or ``None``.

    Deduplication is by normalized text within the same category and
    repository, which is what an "obvious repeat" means for a fact. It never
    matches across categories: the same sentence recorded as a convention and
    as a gotcha is two different records, and collapsing them would lose the
    distinction a later session filters on.

    Tolerates a store whose ``search`` does not accept the optional
    ``repo_path`` keyword, so an alternate or narrowed store cannot turn a
    duplicate into an unhandled ``TypeError`` on the write path.
    """
    needle = " ".join(redact_secrets(text or "").split()).casefold()
    if not needle:
        return None
    try:
        try:
            rows = store.search(needle[:200], limit=50, repo_path=repo_path or None)
        except TypeError:
            rows = store.search(needle[:200], limit=50)
    except Exception:
        return None
    for row in rows or []:
        candidate = " ".join(str(getattr(row, "text", "") or "").split()).casefold()
        if candidate != needle:
            continue
        if str(getattr(row, "category", "")) != str(category or "general"):
            continue
        row_id = int(getattr(row, "id", 0) or 0)
        return row_id or None
    return None


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
    _emit_server_event("tool_call", tool="task_status")
    task_dir = _safe_task_dir(task_id)
    if task_dir is None:
        return f"invalid task id: {redact_secrets(task_id)!r} (expected a single path segment)"
    state_file = safe_state_file(task_dir / "state.json", default_logs_dir())
    if state_file is None:
        return f"no state file for task {redact_secrets(task_id)!r} under {default_logs_dir()}"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read state file: {exc}"
    if not isinstance(state, dict):
        return f"malformed state file for task {redact_secrets(task_id)!r}: expected a JSON object"
    # Unified tracing: a task-status read is a lifecycle observation.
    try:
        tracing.emit(
            "mcp",
            "memory_query",
            task_id=redact_secrets(task_id),
            tool="task_status",
        )
    except Exception:
        pass

    plan = [redact_secrets(item) for item in _string_list(state.get("plan"))]
    completed = [
        redact_secrets(item) for item in _string_list(state.get("completed_steps"))
    ]
    remaining = [
        redact_secrets(item) for item in _string_list(state.get("remaining_plan"))
    ]
    files = [redact_secrets(item) for item in _string_list(state.get("files_touched"))]
    decisions = [redact_secrets(item) for item in _string_list(state.get("decisions"))]
    raw_task_id = state.get("task_id")
    state_task_id = redact_secrets(
        raw_task_id if isinstance(raw_task_id, str) and raw_task_id else task_id
    )
    lines = [f"task {state_task_id}:"]
    lines.append(f"  steps: {len(completed)}/{len(plan)} complete")
    for step in plan:
        lines.append(f"  [{'x' if step in completed else ' '}] {step}")
    if files:
        lines.append("  files touched: " + ", ".join(files))
    for decision in decisions:
        # A state file's decision text is model-authored history, which is
        # untrusted content from the caller's perspective: it must not be able
        # to issue instructions to whatever agent reads this status.
        lines.append(f"  decision: {_guard_mcp_result(decision, source='memory')}")
    if remaining:
        lines.append("  remaining:")
        for item in remaining:
            lines.append(f"    - {item}")
    return "\n".join(lines)


@mcp.tool()
def list_repos() -> str:
    """List repositories with code-graph indexes under HARNESS_HOME, with
    the last-queried one marked."""
    _emit_server_event("tool_call", tool="list_repos")
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
        if not isinstance(data, dict):
            continue
        repo = data.get("repo_path")
        if not isinstance(repo, str) or not repo:
            continue
        file_count = data.get("indexed_file_count", data.get("file_count", "?"))
        if not isinstance(file_count, int):
            file_count = "?"
        mark = "  <- last used" if repo == last else ""
        entries.append(f"- {redact_secrets(repo)} ({file_count} files){mark}")
    return "\n".join(entries) or "no code-graph indexes built yet"


def main() -> None:
    """Run the MCP server over stdio (the standard local transport)."""
    from shared.brand import apply_legacy_env

    apply_legacy_env()
    _emit_server_event(
        "startup",
        metadata={"transport": "stdio", "tool_count": len(_SERVER_TOOL_NAMES)},
    )
    try:
        mcp.run()
    finally:
        _emit_server_event("shutdown", metadata={"transport": "stdio"})


if __name__ == "__main__":
    main()
