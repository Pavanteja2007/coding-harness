"""Tests for mcp_server/server.py — tool registration, in-process calls,
and a REAL stdio MCP client round-trip (the Definition of Done: server
responds correctly when queried by a generic MCP client)."""
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """Isolated HARNESS_HOME + LOGS for the server process/objects."""
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    monkeypatch.setattr("mcp_server.server._graph_cache", {})
    return home, logs


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(textwrap.dedent("""\
        def helper(x):
            return x + 1

        def caller():
            return helper(41)
    """), encoding="utf-8")
    return repo


async def _call(server_mod, name, args):
    """Call a tool through the in-process MCPServer object."""
    result = await server_mod.mcp.call_tool(name, args)
    # v2 wraps content in TextContent; unwrap uniformly
    if hasattr(result, "content"):
        texts = [getattr(c, "text", str(c)) for c in result.content]
        return texts[0] if texts else ""
    return result


async def test_tools_registered(mcp_env):
    import mcp_server.server as srv

    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    assert {"query_structure", "query_decisions", "record_decision"} <= names
    # Boundary 5 tools have descriptions (MCP clients show them)
    by_name = {t.name: t for t in tools}
    assert by_name["record_decision"].description


async def test_record_and_query_decisions(mcp_env):
    import mcp_server.server as srv

    r = await _call(srv, "record_decision", {"text": "prefer argparse for CLIs", "category": "convention"})
    assert "recorded" in r
    r2 = await _call(srv, "query_decisions", {"query": "argparse"})
    assert "prefer argparse" in r2
    r3 = await _call(srv, "query_decisions", {"query": ""})
    assert "prefer argparse" in r3  # empty query -> recent


async def test_query_decisions_ingests_state_files(mcp_env):
    """query_decisions must lazily ingest logs/*/state.json before answering."""
    home, logs = mcp_env
    d = logs / "task-77"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-77", "plan": [], "completed_steps": [],
        "files_touched": [], "decisions": ["chose minimal diff patching"],
        "remaining_plan": [],
    }), encoding="utf-8")
    import mcp_server.server as srv

    r = await _call(srv, "query_decisions", {"query": "minimal diff"})
    assert "chose minimal diff patching" in r


async def test_query_structure_indexing_and_callers(mcp_env, tmp_path):
    repo = _make_repo(tmp_path)
    import mcp_server.server as srv

    r = await _call(srv, "query_structure", {"query": "callers helper", "repo": str(repo)})
    assert "pkg.core.caller" in r
    # repo-less call now works (last-repo pointer recorded)
    r2 = await _call(srv, "query_structure", {"query": "callees caller"})
    assert "pkg.core.helper" in r2


async def test_query_structure_no_repo(mcp_env):
    import mcp_server.server as srv

    r = await _call(srv, "query_structure", {"query": "files"})
    assert "no code-graph index" in r


async def test_task_status_tool(mcp_env):
    home, logs = mcp_env
    d = logs / "task-88"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-88", "plan": ["1. do"],
        "completed_steps": [], "files_touched": [],
        "decisions": [], "remaining_plan": ["1. do"],
    }), encoding="utf-8")
    import mcp_server.server as srv

    r = await _call(srv, "task_status", {"task_id": "task-88"})
    assert "task-88" in r and "[ ] 1. do" in r
    r2 = await _call(srv, "task_status", {"task_id": "missing"})
    assert "no state file" in r2


async def test_list_repos(mcp_env, tmp_path):
    repo = _make_repo(tmp_path)
    import mcp_server.server as srv

    await _call(srv, "query_structure", {"query": "files", "repo": str(repo)})
    r = await _call(srv, "list_repos", {})
    assert str(repo) in r or repo.name in r
    assert "files" in r  # file count reported


# ---------------------------------------------------------------------------
# REAL stdio client round-trip (subprocess server, like an external client)
# ---------------------------------------------------------------------------

async def test_stdio_round_trip(mcp_env, tmp_path):
    """Launch the server as a subprocess over stdio (exactly how Claude
    Code / Cursor would) and exercise the three Boundary 5 tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from memory.mcp_client import _server_errlog

    repo = _make_repo(tmp_path)
    home, logs = mcp_env
    # write a state file the server should lazily ingest
    d = logs / "task-rt"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-rt", "plan": [], "completed_steps": [],
        "files_touched": [], "decisions": ["record via stdio round-trip"],
        "remaining_plan": [],
    }), encoding="utf-8")

    env = {**os.environ,
           "HARNESS_HOME": str(home),
           "HARNESS_LOGS_DIR": str(logs)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    async with stdio_client(params, errlog=_server_errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"query_structure", "query_decisions", "record_decision"} <= names

            # 1) record_decision
            res = await session.call_tool("record_decision", {
                "text": "stdio clients can record decisions",
                "category": "test",
            })
            assert not res.is_error
            text = res.content[0].text
            assert "recorded" in text

            # 2) query_decisions — sees both the manual record and the
            #    lazily-ingested state file
            res2 = await session.call_tool("query_decisions", {"query": "stdio"})
            text2 = res2.content[0].text
            assert "stdio clients can record decisions" in text2
            assert "record via stdio round-trip" in text2

            # 3) query_structure — indexes a repo and answers
            res3 = await session.call_tool("query_structure", {
                "query": "callers helper", "repo": str(repo),
            })
            text3 = res3.content[0].text
            assert "pkg.core.caller" in text3

            # task_status bonus tool
            res4 = await session.call_tool("task_status", {"task_id": "task-rt"})
            assert "task-rt" in res4.content[0].text
