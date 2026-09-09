"""Minimal MCP CLIENT for consuming external MCP servers (spec item 30:
"consume external MCP tools/connectors where useful").

The official Python SDK ships both halves: we already run the server
half (mcp_server/); this module is the client half — a thin, stdio-only
connector that spawns an external MCP server (by command), lists its
tools, and calls one. Deliberately minimal:

  - stdio transport only (standard for local servers; matches our own
    server's transport),
  - one call per connection (spawn -> initialize -> call -> close) —
    simple, no session/pooling to get wrong,
  - never raises for expected outcomes: missing server, bad tool name,
    tool error — each returns a plain result dict with an "ok" flag so
    CLI callers can print instead of traceback.

Uses the same SDK the tests already pin (mcp>=2.x, with the 1.x
FastMCP import kept working on the server side; the client API used
here exists in both).

Example:
    from memory.mcp_client import call_mcp_tool
    out = call_mcp_tool(["python", "-m", "mcp_server"], "query_decisions",
                        {"query": "pytest"})
    # -> {"ok": True, "text": "..."} or {"ok": False, "error": "..."}

Also usable from the CLI: `harness mcp call "<server-cmd>" <tool>
[--args '{...}']` (see cli/main.py) — that's the demoable "consume an
external MCP tool" surface, and our own server doubles as the test
target.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run(coro):
    """Run an async fn on a fresh loop (sync API for CLI/sync callers;
    safe when the caller has no running loop of its own)."""
    return asyncio.run(coro)


def parse_server_command(server: str) -> List[str]:
    """Split a server command string (quoted args respected) into argv.

    Assumes a shell-style command line like "python -m mcp_server" or a
    quoted executable path ('"C:\\tools\\server.exe" --port 9'). Uses
    non-posix shlex so Windows backslash paths survive, then strips one
    matching pair of surrounding quotes per token (posix=False keeps
    them embedded, which would break subprocess spawn).
    """
    if not server:
        return []
    out: List[str] = []
    for tok in shlex.split(server, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        out.append(tok)
    return out


async def _with_session(server_argv: List[str], cwd: Optional[str],
                        env: Optional[Dict[str, str]]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=server_argv[0],
        args=server_argv[1:],
        env=env if env is not None else dict(os.environ),
        cwd=cwd or str(Path.cwd()),
    )
    transport = stdio_client(params)
    read, write = await transport.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    return transport, session


async def _close(transport, session) -> None:
    try:
        await session.__aexit__(None, None, None)
    finally:
        await transport.__aexit__(None, None, None)


async def _list_tools(server_argv: List[str], cwd: Optional[str],
                        env: Optional[Dict[str, str]]) -> Dict[str, Any]:
    transport = session = None
    try:
        transport, session = await _with_session(server_argv, cwd, env)
        await session.initialize()
        tools = await session.list_tools()
        return {
            "ok": True,
            "tools": [
                {"name": t.name,
                 "description": (t.description or "").strip().splitlines()[0]
                 if t.description else ""}
                for t in tools.tools
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tools": [], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if transport is not None:
            await _close(transport, session)


async def _call_tool(server_argv: List[str], tool: str,
                     args: Dict[str, Any], cwd: Optional[str],
                     env: Optional[Dict[str, str]]) -> Dict[str, Any]:
    transport = session = None
    try:
        transport, session = await _with_session(server_argv, cwd, env)
        await session.initialize()
        res = await session.call_tool(tool, args)
        text = ""
        if getattr(res, "content", None):
            texts = [getattr(c, "text", str(c)) for c in res.content]
            text = texts[0] if texts else ""
        # a tool-level error still yields text (e.g. "Unknown tool: X")
        # — surface BOTH the ok flag and the message; error mirrors text
        # so callers can check one field without losing the other.
        return {"ok": not getattr(res, "is_error", False),
                "text": text, "error": text or None}
    except Exception as exc:  # noqa: BLE001 — surface as data, not a raise
        return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if transport is not None:
            await _close(transport, session)


# -- sync public API (CLI + callers without their own event loop) ---------

def list_mcp_tools(server: str, cwd: Optional[str] = None,
                   env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Spawn the external MCP server at `server` (command string) and
    list its tools. Returns {"ok": bool, "tools": [...]} or
    {"ok": False, "error": str}. Assumes `server` is a launchable
    command; spawn failures come back as ok=False, never a raise."""
    argv = parse_server_command(server)
    if not argv:
        return {"ok": False, "error": "empty server command", "tools": []}
    try:
        return _run(_list_tools(argv, cwd, env))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tools": []}


def call_mcp_tool(server: str, tool: str, args: Optional[Dict[str, Any]] = None,
                  cwd: Optional[str] = None,
                  env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Call ONE tool on an external MCP server (spawn -> initialize ->
    call -> close). Returns {"ok": bool, "text": str, "error": str|None};
    never raises — connection/spawn/tool errors return ok=False."""
    argv = parse_server_command(server)
    if not argv:
        return {"ok": False, "text": "",
                "error": "empty server command"}
    return _run(_call_tool(argv, tool, args or {}, cwd, env))
