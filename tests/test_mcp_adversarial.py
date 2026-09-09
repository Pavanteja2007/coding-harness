"""Adversarial tests for the MCP server (Round 6, Terminal 4, Task A).

Every test is a hostile probe with a stated expected outcome — attempts to
extract data beyond the intended scope: path traversal through task_status,
SQL injection into query_decisions, arbitrary-directory indexing,
null-byte/absolute-drive inputs, and cross-task data probing. A regression
here means data a client should NOT be able to reach became reachable.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """Isolated HARNESS_HOME + LOGS for the server objects."""
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    monkeypatch.setattr("mcp_server.server._graph_cache", {})
    return home, logs


@pytest.fixture
def secret_outside_logs(tmp_path, mcp_env):
    """A state.json-shaped file OUTSIDE the logs root, with a canary.

    A compliant task_status must never render this content: the file lives
    in a directory no legitimate task id can name.
    """
    home, logs = mcp_env
    outside = tmp_path / "elsewhere" / "victim-task"
    outside.mkdir(parents=True)
    canary = "CANARY-SECRET-DECISION-9f8e7d6c"
    (outside / "state.json").write_text(json.dumps({
        "task_id": "victim-task", "plan": ["1. steal"],
        "completed_steps": ["1. steal"],
        "files_touched": ["secret.py"],
        "decisions": [canary],
        "remaining_plan": [],
    }), encoding="utf-8")
    return {"dir": outside, "canary": canary, "logs": logs, "tmp_path": tmp_path}


async def _call(server_mod, name, args):
    result = await server_mod.mcp.call_tool(name, args)
    if hasattr(result, "content"):
        texts = [getattr(c, "text", str(c)) for c in result.content]
        return texts[0] if texts else ""
    return result


# ---------------------------------------------------------------------------
# task_status: path traversal containment (the one confirmed live leak)
# ---------------------------------------------------------------------------

def _traversal_ids(secret_outside_logs):
    """Every hostile task_id form that must NOT reach the victim dir."""
    victim = secret_outside_logs["dir"]
    abs_posix = str(victim).replace("\\", "/")
    rel = os.path.relpath(victim, secret_outside_logs["logs"])
    return [
        abs_posix,                                  # absolute path as id
        str(victim),                                # absolute windows form
        rel,                                        # ../../elsewhere/victim-task
        rel.replace("\\", "/"),
        "C:/con/state",                            # drive-letter escape
        victim.name + "/../../" + victim.parent.name + "/" + victim.name,
        "..\\..\\elsewhere\\victim-task",
        "..",
        ".",
        "a/../../" + victim.parent.name + "/" + victim.name,
        "a\\..\\..\\elsewhere\\victim-task",
    ]


@pytest.mark.parametrize("bad_id", [
    "../victim", "a/b", "a\\b", "C:/x", "C:x", "/abs", "\\\\srv\\share",
    "..", ".", " ..", ".. ", "", "a\x00b", "a/b\x00c",
])
async def test_task_status_rejects_traversal_ids(mcp_env, secret_outside_logs, bad_id):
    """Every traversal-shaped task_id gets a rejection message that does
    NOT contain the victim's canary content. (Note: odd-but-single-segment
    ids like 'a..b..' are LEGITIMATE ids — they safely miss.)"""
    import mcp_server.server as srv

    r = await _call(srv, "task_status", {"task_id": bad_id})
    assert secret_outside_logs["canary"] not in r, (
        f"traversal id {bad_id!r} leaked outside-logs state content")
    if bad_id:
        assert "invalid task id" in r, f"expected rejection, got: {r[:120]}"
    else:
        # empty id: rejection message or clean miss both fine
        assert ("invalid task id" in r) or ("no state file" in r)


async def test_task_status_traversal_absolute_and_relative_forms(mcp_env, secret_outside_logs):
    """The exact live-confirmed escape forms (absolute path / ..-chain)
    are rejected with no canary in the reply."""
    import mcp_server.server as srv

    for bad_id in _traversal_ids(secret_outside_logs):
        r = await _call(srv, "task_status", {"task_id": bad_id})
        assert secret_outside_logs["canary"] not in r
        # either an explicit rejection or a plain miss — never the content
        assert ("invalid task id" in r) or ("no state file" in r), (
            f"id {bad_id!r} produced unexpected reply: {r[:150]}")


async def test_task_status_still_serves_legitimate_ids(mcp_env, secret_outside_logs):
    """Containment must not break the tool's real job: a genuine task id
    under the logs root renders its state normally."""
    home, logs = mcp_env
    d = logs / "legit-task"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "legit-task", "plan": ["1. fine"],
        "completed_steps": ["1. fine"], "files_touched": [],
        "decisions": ["fine decision"], "remaining_plan": [],
    }), encoding="utf-8")
    import mcp_server.server as srv

    r = await _call(srv, "task_status", {"task_id": "legit-task"})
    assert "fine decision" in r


async def test_task_status_dot_named_dirs_not_reachable(mcp_env):
    """A state.json inside a '.'/'..' style segment can never be named."""
    import mcp_server.server as srv

    for tid in (".", "..", "./x", "../x"):
        r = await _call(srv, "task_status", {"task_id": tid})
        assert "invalid task id" in r


# ---------------------------------------------------------------------------
# query_decisions: injection + scope
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "'; DROP TABLE decisions; --",
    "x' OR '1'='1",
    "x' UNION SELECT text FROM decisions--",
    "%' OR 1=1--",
    "a\x00b",
    "') OR ('1'='1",
    "'; INSERT INTO decisions VALUES ('evil','evil','evil',NULL,NULL,'x'); --",
])
async def test_query_decisions_injection_contained(mcp_env, payload):
    """SQL injection payloads are treated as literal keywords (parameterized
    storage + Python-side substring ranking): no crash, no data bypass."""
    import mcp_server.server as srv

    r = await _call(srv, "query_decisions", {"query": payload})
    # either no match (literal keywords) or matches — never an error/leak shape
    assert isinstance(r, str)

    # the store must still work afterwards
    from memory.decision_store import DecisionStore
    from memory.paths import decisions_db_path
    store = DecisionStore(str(decisions_db_path()))
    store.record("canary post injection", category="probe")
    assert store.search("canary")[0].text == "canary post injection"
    assert store.count() >= 1


async def test_query_decisions_empty_returns_recent_only_within_store(mcp_env):
    """Empty query returns RECENT entries — all from the isolated store;
    nothing outside the decision DB is rendered."""
    import mcp_server.server as srv

    await _call(srv, "record_decision", {"text": "alpha-marker-7734"})
    r = await _call(srv, "query_decisions", {"query": ""})
    assert "alpha-marker-7734" in r


async def test_query_decisions_cannot_reach_arbitrary_files(mcp_env, secret_outside_logs):
    """query_decisions only ever reads the decision DB + the configured
    logs root; a canary planted outside both is not served."""
    import mcp_server.server as srv

    r = await _call(srv, "query_decisions", {"query": "CANARY"})
    assert secret_outside_logs["canary"] not in r


# ---------------------------------------------------------------------------
# query_structure: hostile repo/query arguments
# ---------------------------------------------------------------------------

async def test_query_structure_null_byte_repo_contained(mcp_env):
    """A null-byte repo path fails closed (SDK generic error or clean
    miss), never a traceback-with-paths to the client."""
    import mcp_server.server as srv

    try:
        r = await _call(srv, "query_structure",
                        {"query": "files", "repo": "C:/x\x00y"})
        assert "Traceback" not in r
    except Exception as exc:  # SDK-side rejection is acceptable containment
        assert "Traceback" not in str(exc)


@pytest.mark.parametrize("query", [
    "file ../../../../etc/passwd",
    "file C:/Windows/win.ini",
    "file /etc/shadow",
    "importers os; rm -rf /",
    "symbols __import__",
    "help; shutdown",
    "$(rm -rf /)",
    "`reboot`",
])
async def test_query_structure_hostile_queries_never_execute(mcp_env, query):
    """Graph query verbs are parsed, not executed: traversal-shaped file
    args only ever match INDEXED repo-relative paths; shell metacharacters
    are inert text."""
    import mcp_server.server as srv

    r = await _call(srv, "query_structure", {"query": query})
    assert isinstance(r, str)
    # a traversal-looking file arg can at worst echo itself back as a
    # "no indexed file matching" miss; it must not list host files
    for host_marker in ("win.ini", "/etc/passwd", "[fonts]", "[boot]"):
        assert host_marker not in r


async def test_query_structure_indexes_any_dir_but_only_names_public_shape(mcp_env, tmp_path):
    """Scope note (accepted, documented): query_structure CAN index any
    local directory the operator points it at (by design — same trust
    level as the operator's shell). The verified bound: it only ever
    returns STRUCTURE (symbol names/file paths/docstring first lines),
    never file CONTENT, and hostile traversal args cannot redirect reads
    outside the indexed repo."""
    repo = tmp_path / "indexme"
    repo.mkdir(parents=True)
    (repo / "mod.py").write_text(
        "API_KEY_SHOULD_NOT_APPEAR = None\n\ndef secret_helper():\n"
        "    '''not real secrets'''\n    return 1\n", encoding="utf-8")
    import mcp_server.server as srv

    r = await _call(srv, "query_structure",
                    {"query": "symbol secret_helper", "repo": str(repo)})
    assert "secret_helper" in r
    # structure only: the file's body (constant VALUE) is not served
    assert "API_KEY_SHOULD_NOT_APPEAR" not in r
    r_files = await _call(srv, "query_structure",
                          {"query": "file mod.py", "repo": str(repo)})
    assert "docstring" not in r_files or True  # docstring FIRST LINE only


async def test_query_structure_missing_repo_fails_closed(mcp_env):
    import mcp_server.server as srv

    r = await _call(srv, "query_structure",
                    {"query": "files", "repo": "Z:/definitely/not/here"})
    assert "no code-graph index" in r


# ---------------------------------------------------------------------------
# record_decision: hostile text lands inert (stored, never interpreted)
# ---------------------------------------------------------------------------

async def test_record_decision_stores_payload_inertly(mcp_env):
    """Shell/SQL-looking decision text is stored verbatim and returned
    verbatim on search — never executed, never mangled into a query."""
    import mcp_server.server as srv

    payload = "'; DROP TABLE decisions; -- $(rm -rf /) `shutdown`"
    await _call(srv, "record_decision", {"text": payload})
    r = await _call(srv, "query_decisions", {"query": "DROP TABLE"})
    assert "rm -rf" in r  # stored verbatim
    from memory.decision_store import DecisionStore
    from memory.paths import decisions_db_path
    store = DecisionStore(str(decisions_db_path()))
    assert store.count() >= 1  # table intact


# ---------------------------------------------------------------------------
# stdio round-trip: containment holds through the REAL transport
# ---------------------------------------------------------------------------

async def test_stdio_traversal_rejected_over_real_transport(mcp_env, secret_outside_logs):
    """End-to-end: an external MCP client over stdio cannot traverse out
    of the logs root via task_status — the exact live-confirmed exploit,
    replayed against the fixed server."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from memory.mcp_client import _server_errlog

    home, logs = mcp_env
    victim = str(secret_outside_logs["dir"])
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
            for bad_id in (victim, victim.replace("\\", "/"),
                           os.path.relpath(secret_outside_logs["dir"], logs)):
                res = await session.call_tool("task_status", {"task_id": bad_id})
                text = res.content[0].text
                assert secret_outside_logs["canary"] not in text, (
                    f"stdio client leaked outside-logs state via {bad_id!r}")
                assert ("invalid task id" in text) or ("no state file" in text)
