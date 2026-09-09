"""Tests for memory.mcp_client — consuming an EXTERNAL MCP server via the
official SDK's stdio client (spec item 30). Our own mcp_server doubles
as the external target: spawn it exactly the way an outside consumer
would and verify the client half lists tools and calls them.
"""
import json
from pathlib import Path

import pytest

from memory.mcp_client import call_mcp_tool, list_mcp_tools, parse_server_command

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    return home, logs


class TestParseServerCommand:
    def test_simple(self):
        assert parse_server_command("python -m mcp_server") == [
            "python", "-m", "mcp_server"]

    def test_windows_path_kept_intact(self):
        out = parse_server_command(r"C:\tools\server.exe --port 9")
        assert out[0] == r"C:\tools\server.exe"
        assert out[-1] == "9"

    def test_quoted_path_quotes_stripped(self):
        out = parse_server_command('"C:\\Program Files\\py\\python.exe" -m mcp_server')
        assert out[0] == "C:\\Program Files\\py\\python.exe"
        assert out[1:] == ["-m", "mcp_server"]

    def test_empty(self):
        assert parse_server_command("") == []


class TestListTools:
    def test_lists_our_own_server(self, isolated_env):
        out = list_mcp_tools(f'"{__import__("sys").executable}" -m mcp_server',
                             cwd=str(REPO_ROOT))
        assert out["ok"], out
        names = {t["name"] for t in out["tools"]}
        assert {"query_structure", "query_decisions", "record_decision"} <= names

    def test_bad_command_returns_ok_false(self, isolated_env):
        out = list_mcp_tools("definitely-not-a-command-xyz")
        assert not out["ok"]
        assert out.get("error")


class TestCallTool:
    def test_call_query_decisions(self, isolated_env):
        home, logs = isolated_env
        d = logs / "task-mcpclient"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({
            "task_id": "task-mcpclient", "plan": [], "completed_steps": [],
            "files_touched": [], "decisions": ["prefer tree-sitter for parsing"],
            "remaining_plan": [],
        }), encoding="utf-8")
        out = call_mcp_tool(
            f'"{__import__("sys").executable}" -m mcp_server',
            "query_decisions", {"query": "tree-sitter"}, cwd=str(REPO_ROOT))
        assert out["ok"], out
        assert "prefer tree-sitter for parsing" in out["text"]

    def test_call_record_decision(self, isolated_env):
        out = call_mcp_tool(
            f'"{__import__("sys").executable}" -m mcp_server',
            "record_decision", {"text": "mcp client round trip works"},
            cwd=str(REPO_ROOT))
        assert out["ok"], out
        assert "recorded" in out["text"].lower()

    def test_unknown_tool_ok_false(self, isolated_env):
        out = call_mcp_tool(
            f'"{__import__("sys").executable}" -m mcp_server',
            "no_such_tool", {}, cwd=str(REPO_ROOT))
        assert not out["ok"]
        assert out.get("error")

    def test_bad_server_never_raises(self, isolated_env):
        out = call_mcp_tool("no-such-server-abc", "any", {})
        assert not out["ok"]
        assert out.get("error")


class TestCliSurface:
    def test_cli_mcp_call(self, isolated_env, capsys):
        from cli.main import main as cli_main

        rc = cli_main(["mcp", "call",
                       f'"{__import__("sys").executable}" -m mcp_server',
                       "record_decision",
                       "--args", '{"text": "cli mcp surface works"}',
                       "--cwd", str(REPO_ROOT)])
        assert rc == 0
        assert "recorded" in capsys.readouterr().out.lower()

    def test_cli_mcp_list(self, isolated_env, capsys):
        from cli.main import main as cli_main

        rc = cli_main(["mcp", "list-tools",
                       f'"{__import__("sys").executable}" -m mcp_server',
                       "--cwd", str(REPO_ROOT)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "query_decisions" in out
