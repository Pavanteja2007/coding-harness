import asyncio
import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

import mcp_server.server as srv
from memory.code_graph import CodeGraph, _repo_key
from memory.decision_store import DecisionStore
from memory.mcp_client import call_mcp_tool, list_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_state(root: Path, relative: str, payload) -> Path:
    task_dir = root / relative
    task_dir.mkdir(parents=True, exist_ok=True)
    state_file = task_dir / "state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    return state_file


def _state_payload(task_id: str, text: str, repo: Path) -> dict:
    return {
        "task_id": task_id,
        "plan": [],
        "completed_steps": [],
        "files_touched": [],
        "decisions": [text],
        "remaining_plan": [],
        "repo_path": str(repo),
    }


def test_decision_dedupe_is_repo_scoped_and_dedupe_false_inserts(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store = DecisionStore(str(tmp_path / "memory" / "decisions.db"))
    try:
        first = store.record(
            "same decision",
            task_id="shared-task",
            repo_path=str(repo_a),
            dedupe=True,
        )
        second = store.record(
            "same decision",
            task_id="shared-task",
            repo_path=str(repo_b),
            dedupe=True,
        )
        duplicate = store.record(
            "same decision",
            task_id="shared-task",
            repo_path=str(repo_a),
            dedupe=True,
        )
        opted_out = store.record(
            "same decision",
            task_id="shared-task",
            repo_path=str(repo_a),
            dedupe=False,
        )
        assert first is not None
        assert second is not None
        assert first != second
        assert duplicate is None
        assert opted_out is not None
        assert store.count() == 3
        assert len(store.search("", repo_path=str(repo_a))) == 2
        assert len(store.search("", repo_path=str(repo_b))) == 1
    finally:
        store.close()


def test_poll_keeps_same_task_and_text_separate_across_repos(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _write_state(
        logs, "run-a/shared-task", _state_payload("shared-task", "same", repo_a)
    )
    _write_state(
        logs, "run-b/shared-task", _state_payload("shared-task", "same", repo_b)
    )
    store = DecisionStore(str(tmp_path / "memory" / "decisions.db"))
    try:
        assert store.poll(str(logs)) == 2
        assert store.poll(str(logs)) == 0
        assert store.count() == 2
    finally:
        store.close()


def test_decision_poll_skips_state_symlink_escape(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_state(logs, "valid", _state_payload("valid", "inside", tmp_path / "repo"))
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = "OUTSIDE-DECISION-CANARY"
    (outside / "state.json").write_text(
        json.dumps(_state_payload("outside", canary, tmp_path / "other-repo")),
        encoding="utf-8",
    )
    escape = logs / "escape"
    escape.mkdir()
    try:
        (escape / "state.json").symlink_to(outside / "state.json")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"state-file symlinks unavailable: {exc}")
    store = DecisionStore(str(tmp_path / "memory" / "decisions.db"))
    try:
        assert store.poll(str(logs)) == 1
        rows = store.search("CANARY")
        assert rows == []
        assert store.count() == 1
    finally:
        store.close()


def test_cli_structure_query_is_read_only_and_uses_harness_home(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    home.mkdir()
    logs.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "mod.py"
    source.write_text("def cli_only_symbol():\n    return 7\n", encoding="utf-8")
    before = source.read_bytes()
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    monkeypatch.setenv("HARNESS_DECISIONS_DB", str(home / "memory" / "decisions.db"))
    from cli.main import main as cli_main

    rc = cli_main(
        [
            "memory",
            "query-structure",
            "--repo",
            str(repo),
            "symbol cli_only_symbol",
        ]
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert "cli_only_symbol" in output
    assert source.read_bytes() == before
    assert not (repo / ".harness").exists()
    graph_files = list((home / "code-graph").glob("*/graph.json"))
    assert len(graph_files) == 1
    assert CodeGraph(str(repo)).graph_dir == graph_files[0].parent


def test_repo_keys_distinguish_long_shared_prefixes(tmp_path):
    shared = "C:/" + "same-prefix-segment/" * 12
    key_a = _repo_key(shared + "/repo-a")
    key_b = _repo_key(shared + "/repo-b")
    assert key_a != key_b
    assert key_a == _repo_key(shared + "/repo-a")
    assert len(key_a.rsplit("-", 1)[1]) == 64
    assert len(key_b.rsplit("-", 1)[1]) == 64
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    assert CodeGraph(str(repo_a)).graph_dir != CodeGraph(str(repo_b)).graph_dir
    assert (
        CodeGraph(str(repo_a)).graph_dir == CodeGraph(str(repo_a.resolve())).graph_dir
    )


def test_mcp_graph_cache_refreshes_on_every_query(tmp_path, monkeypatch):
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    home.mkdir()
    logs.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "mod.py"
    source.write_text("def first_symbol():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    monkeypatch.setattr(srv, "_graph_cache", {})
    try:
        first = srv.query_structure("symbols first_symbol", str(repo))
        assert "first_symbol" in first
        source.write_text(
            "def first_symbol():\n    return 1\n\ndef second_symbol():\n    return 2\n",
            encoding="utf-8",
        )
        stat = source.stat()
        os.utime(source, (stat.st_atime, stat.st_mtime + 2))
        second = srv.query_structure("symbols second_symbol", str(repo))
        assert "second_symbol" in second
    finally:
        srv._graph_cache.clear()


def test_mcp_decision_tools_close_their_stores(monkeypatch):
    class FakeStore:
        instances: ClassVar[list] = []

        def __init__(self, _db_path):
            self.closed = False
            self.__class__.instances.append(self)

        def poll(self, _logs_dir):
            return 0

        def search(self, _query, limit=20):
            return []

        def record(
            self,
            _text,
            category="general",
            source="manual",
            task_id=None,
            repo_path=None,
            dedupe=False,
            provenance=None,
            metadata=None,
            promote=False,
            explicit=False,
        ):
            # Mirrors memory.decision_store.DecisionStore.record rather than
            # accepting **kwargs: this double stands in for a real
            # collaborator, so a rename or a dropped keyword on the real
            # signature must fail HERE instead of silently passing. It went
            # stale when record_decision began passing repo_path/provenance.
            return 1

        def close(self):
            self.closed = True

    monkeypatch.setattr(srv, "DecisionStore", FakeStore)
    assert "no matching" in srv.query_decisions("")
    assert "recorded" in srv.record_decision("close me")
    assert len(FakeStore.instances) == 2
    assert all(store.closed for store in FakeStore.instances)


def test_task_status_and_list_repos_tolerate_malformed_json_shapes(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    home.mkdir()
    logs.mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    malformed_task = logs / "malformed"
    malformed_task.mkdir()
    (malformed_task / "state.json").write_text("[]", encoding="utf-8")
    odd_task = logs / "odd"
    odd_task.mkdir()
    (odd_task / "state.json").write_text(
        json.dumps(
            {
                "task_id": ["not", "a", "string"],
                "plan": "not a list",
                "completed_steps": {"bad": True},
                "files_touched": [1, {"bad": True}],
                "decisions": None,
                "remaining_plan": 7,
            }
        ),
        encoding="utf-8",
    )
    assert "malformed state file" in srv.task_status("malformed")
    odd_status = srv.task_status("odd")
    assert "task odd:" in odd_status
    assert "0/0 complete" in odd_status

    good_meta = home / "code-graph" / "good"
    bad_meta = home / "code-graph" / "bad"
    good_meta.mkdir(parents=True)
    bad_meta.mkdir(parents=True)
    (good_meta / "meta.json").write_text(
        json.dumps({"repo_path": str(tmp_path / "repo"), "file_count": 3}),
        encoding="utf-8",
    )
    (bad_meta / "meta.json").write_text("[]", encoding="utf-8")
    repos = srv.list_repos()
    assert str(tmp_path / "repo") in repos
    assert "3 files" in repos
    assert "bad" not in repos


def test_task_status_containment_remains_enforced(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = "TASK-STATUS-OUTSIDE-CANARY"
    (outside / "state.json").write_text(
        json.dumps({"task_id": "outside", "decisions": [canary]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    for task_id in (str(outside), os.path.relpath(outside, logs)):
        result = srv.task_status(task_id)
        assert "invalid task id" in result
        assert canary not in result


def test_mcp_server_keeps_all_five_live_tools():
    tools = asyncio.run(srv.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "query_structure",
        "query_decisions",
        "record_decision",
        "task_status",
        "list_repos",
    }


def test_external_client_is_total_and_handles_mcp_content_shapes(tmp_path):
    server_script = tmp_path / "external_server.py"
    server_script.write_text(
        """from mcp.server.mcpserver import MCPServer
server = MCPServer(\"external-test\")

@server.tool()
def pieces() -> list[str]:
    return [\"first\", \"second\"]

@server.tool(description=\"   \")
def blank_description() -> str:
    return \"ok\"

@server.tool()
def broken() -> str:
    raise RuntimeError(\"external failure\")

server.run()
""",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{server_script}"'
    listed = list_mcp_tools(command, cwd=str(REPO_ROOT))
    assert listed["ok"], listed
    tools = {tool["name"]: tool for tool in listed["tools"]}
    assert tools["blank_description"]["description"] == ""
    pieces = call_mcp_tool(command, "pieces", cwd=str(REPO_ROOT))
    assert pieces["ok"], pieces
    assert pieces["text"].splitlines() == ["first", "second"]
    broken = call_mcp_tool(command, "broken", cwd=str(REPO_ROOT))
    assert not broken["ok"]
    assert broken["text"] or broken["error"]
    malformed = call_mcp_tool('"unterminated', "anything")
    assert not malformed["ok"]
    assert malformed["error"]
