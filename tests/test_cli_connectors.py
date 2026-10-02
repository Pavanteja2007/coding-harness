"""MCP connectors tests (unified plugins round).

Covers: `neo mcp add/remove/list` persisted in the global settings'
[mcp_servers] table (through cli.neoconfig's existing tier machinery),
label validation, project connectors.toml + local overrides with
global<project<local precedence, plugin-server discovery (and its
disabling), `neo mcp health` ok/fail reporting without tracebacks,
secret masking, and label resolution in list-tools/call.
"""

import json
from pathlib import Path

import pytest

from cli import connectors as conn_mod


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Isolated home + global settings + no project tier by default."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(
        "USERPROFILE" if __import__("os").name == "nt" else "HOME", str(home)
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
    monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
    yield tmp_path


def _cli(argv):
    from cli.main import main

    return main(argv)


# ---------------------------------------------------------------------------
# add / remove / list (global settings persistence)
# ---------------------------------------------------------------------------


def test_add_remove_roundtrip_persists_in_global_settings(tmp_path):
    label = conn_mod.add_server("demo", "python -m mcp_server")
    assert label == "demo"
    assert conn_mod.global_servers() == {"demo": "python -m mcp_server"}
    # the global file really holds the table (import, don't reimplement:
    # written through cli.neoconfig's serializer)
    text = Path(__import__("os").environ["NEO_CONFIG"]).read_text(encoding="utf-8")
    assert "mcp_servers" in text and "demo" in text

    assert conn_mod.remove_server("demo") == "demo"
    assert conn_mod.global_servers() == {}


def test_add_validates_label_and_command():
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("bad label!", "python -m x")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("../escape", "python -m x")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("", "python -m x")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("ok-label", "")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("ok-label", "   ")


def test_remove_unknown_label_raises():
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.remove_server("never-configured")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.remove_server("../escape")


def test_cli_mcp_add_list_remove_roundtrip(capsys):
    rc = _cli(["mcp", "add", "demo", "--", "python", "-m", "mcp_server"])
    assert rc == 0
    assert "added MCP server demo" in capsys.readouterr().out

    rc = _cli(["mcp", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out and "global" in out

    rc = _cli(["mcp", "remove", "demo"])
    assert rc == 0
    assert "removed MCP server demo" in capsys.readouterr().out
    rc = _cli(["mcp", "list"])
    assert rc == 0
    assert "no MCP servers" in capsys.readouterr().out


def test_cli_mcp_add_needs_command(capsys):
    rc = _cli(["mcp", "add", "demo"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_mcp_add_remove_errors_exit_2(capsys):
    rc = _cli(["mcp", "add", "bad label!", "--", "python", "-m", "x"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
    rc = _cli(["mcp", "remove", "never-configured"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_mcp_list_empty(capsys):
    rc = _cli(["mcp", "list"])
    assert rc == 0
    assert "no MCP servers" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# project connectors.toml + local overrides (global < project < local)
# ---------------------------------------------------------------------------


def _write_connectors(repo_neo: Path, name: str, table: dict) -> None:
    lines = ["[mcp_servers]"]
    for k, v in table.items():
        lines.append(f"{json.dumps(k)} = {json.dumps(v)}")
    (repo_neo / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_and_local_precedence(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    neo = repo / ".neo"
    neo.mkdir(parents=True)
    monkeypatch.setenv("NEO_PROJECT_DIR", str(neo))

    conn_mod.add_server("demo", "python -m global-cmd")
    _write_connectors(
        neo,
        "connectors.toml",
        {"demo": "python -m project-cmd", "proj": "python -m proj-cmd"},
    )
    disc = conn_mod.discover_mcp_servers(str(repo))
    assert disc["demo"] == {"command": "python -m project-cmd", "source": "project"}
    assert disc["proj"]["source"] == "project"

    _write_connectors(neo, "connectors.local.toml", {"proj": "python -m local-cmd"})
    disc = conn_mod.discover_mcp_servers(str(repo))
    assert disc["proj"] == {"command": "python -m local-cmd", "source": "local"}
    # project still wins over global where local is silent
    assert disc["demo"]["source"] == "project"
    # local file is the personal-override layer, not the committable one
    assert (neo / "connectors.local.toml").is_file()


def test_broken_connectors_file_degrades_not_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    neo = repo / ".neo"
    neo.mkdir(parents=True)
    monkeypatch.setenv("NEO_PROJECT_DIR", str(neo))
    (neo / "connectors.toml").write_text("not valid toml [[[\n", encoding="utf-8")
    assert conn_mod.project_servers(str(repo)) == {}
    assert conn_mod.discover_mcp_servers(str(repo)) == {}


def test_broken_global_file_refuses_overwrite(tmp_path):
    gp = Path(__import__("os").environ["NEO_CONFIG"])
    gp.write_text("not valid toml [[[\n", encoding="utf-8")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.add_server("demo", "python -m mcp_server")
    with pytest.raises(conn_mod.ConnectorError):
        conn_mod.remove_server("demo")


# ---------------------------------------------------------------------------
# plugin servers in discovery (+ disabled plugins hidden)
# ---------------------------------------------------------------------------


def test_plugin_servers_discovered_and_disabled_hidden(tmp_path, monkeypatch):
    from cli import plugins as plugins_mod

    src = Path(__file__).parent / "fixtures" / "plugin-webapp-toolkit"
    plugins_mod.install_from_local(str(src))
    disc = conn_mod.discover_mcp_servers()
    assert "structure-memory" in disc
    assert disc["structure-memory"]["command"] == "python -m mcp_server"
    assert disc["structure-memory"]["source"].startswith("plugin")

    plugins_mod.disable("webapp-toolkit")
    assert "structure-memory" not in conn_mod.discover_mcp_servers()
    plugins_mod.enable("webapp-toolkit")
    assert "structure-memory" in conn_mod.discover_mcp_servers()


def test_configured_label_beats_plugin_label(tmp_path):
    from cli import plugins as plugins_mod

    src = Path(__file__).parent / "fixtures" / "plugin-webapp-toolkit"
    plugins_mod.install_from_local(str(src))
    conn_mod.add_server("structure-memory", "python -m override-cmd")
    disc = conn_mod.discover_mcp_servers()
    assert disc["structure-memory"] == {
        "command": "python -m override-cmd",
        "source": "global",
    }


# ---------------------------------------------------------------------------
# resolve + health + masking
# ---------------------------------------------------------------------------


def test_resolve_server_label_or_passthrough():
    conn_mod.add_server("demo", "python -m mcp_server")
    assert conn_mod.resolve_server("demo") == "python -m mcp_server"
    raw = "python -m mcp_server --extra 1"
    assert conn_mod.resolve_server(raw) == raw
    assert conn_mod.resolve_server("") is None


def test_health_ok_and_fail_without_traceback(capsys):
    conn_mod.add_server("good", "python -m mcp_server")
    conn_mod.add_server("bad", "python -m definitely_not_a_module_neo")
    results = conn_mod.check_health()
    by_label = {r["label"]: r for r in results}
    assert by_label["good"]["ok"] is True
    assert len(by_label["good"]["tools"]) > 0
    assert by_label["good"]["error"] is None
    assert by_label["bad"]["ok"] is False
    assert by_label["bad"]["error"]  # data, not a raise

    rc = _cli(["mcp", "health"])
    out = capsys.readouterr().out
    assert "ok" in out and "fail" in out
    assert "good" in out and "bad" in out
    assert rc == 1  # any failure -> exit 1, still no traceback


def test_health_ok_exit_0(capsys):
    conn_mod.add_server("good", "python -m mcp_server")
    rc = _cli(["mcp", "health"])
    assert rc == 0
    assert "ok" in capsys.readouterr().out


def test_health_empty(capsys):
    rc = _cli(["mcp", "health"])
    assert rc == 0
    assert "no MCP servers" in capsys.readouterr().out


def test_mask_command_hides_secrets():
    masked = conn_mod.mask_command("srv --api-key sk-abc123XYZ789 --url http://x")
    assert "sk-abc123XYZ789" not in masked
    assert "***" in masked
    assert "--url http://x" in masked
    masked = conn_mod.mask_command("srv --token hunter2 run")
    assert "hunter2" not in masked
    assert "***" in masked
    masked = conn_mod.mask_command("srv --token=abc --other 1")
    assert "abc" not in masked
    # no secrets: unchanged
    assert conn_mod.mask_command("python -m mcp_server") == "python -m mcp_server"


def test_list_masks_secrets(capsys):
    conn_mod.add_server("s", "srv --api-key sk-abc123XYZ789")
    servers = conn_mod.list_servers()
    assert servers[0]["command"] != "srv --api-key sk-abc123XYZ789"
    assert "sk-abc123XYZ789" not in servers[0]["command"]
    rc = _cli(["mcp", "list"])
    assert rc == 0
    assert "sk-abc123XYZ789" not in capsys.readouterr().out


def test_list_tools_accepts_label(capsys):
    conn_mod.add_server("demo", "python -m mcp_server")
    from cli.main import main

    rc = main(["mcp", "list-tools", "demo"])
    assert rc == 0
    assert "query_decisions" in capsys.readouterr().out


def test_duplicate_plugin_labels_keep_winning_source(tmp_path):
    from cli import plugins as plugins_mod

    first = tmp_path / "first"
    second = tmp_path / "second"
    for source, name, command in (
        (first, "first", "python -m first_server"),
        (second, "second", "python -m second_server"),
    ):
        (source / "commands").mkdir(parents=True)
        (source / "commands" / "run.md").write_text("run", encoding="utf-8")
        (source / "plugin.json").write_text(
            json.dumps({"name": name, "mcp_servers": {"shared": command}}),
            encoding="utf-8",
        )
        plugins_mod.install_from_local(str(source))
    found = conn_mod.discover_mcp_servers()
    assert found["shared"]["command"] == "python -m first_server"
    assert found["shared"]["source"] == "plugin:first"


def test_health_timeout_is_honest_and_bounded(monkeypatch):
    import time

    import memory.mcp_client as client

    conn_mod.add_server("slow", "python -m slow_server")
    monkeypatch.setattr(
        client,
        "list_mcp_tools",
        lambda command: time.sleep(0.2) or {"ok": True, "tools": []},
    )
    result = conn_mod.check_health(timeout_s=0.02)
    assert result[0]["ok"] is False
    assert "timed out" in result[0]["error"]


def test_health_error_redacts_secret(monkeypatch):
    import memory.mcp_client as client

    conn_mod.add_server("secret-server", "python -m server --token raw-secret")
    monkeypatch.setattr(
        client,
        "list_mcp_tools",
        lambda command: {"ok": False, "tools": [], "error": "token=raw-secret"},
    )
    result = conn_mod.check_health()
    assert "raw-secret" not in result[0]["error"]


def test_project_label_resolution_uses_explicit_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    neo = repo / ".neo"
    neo.mkdir(parents=True)
    _write_connectors(
        neo, "connectors.toml", {"project-only": "python -m project_server"}
    )
    monkeypatch.chdir(tmp_path)
    assert (
        conn_mod.resolve_server("project-only", repo_path=str(repo))
        == "python -m project_server"
    )
    assert "project-only" not in conn_mod.discover_mcp_servers()


def test_connector_add_remove_supports_project_and_local_tiers(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    neo = repo / ".neo"
    neo.mkdir()
    monkeypatch.setenv("NEO_PROJECT_DIR", str(neo))
    conn_mod.add_server("p", "python -m project", tier="project", repo_path=str(repo))
    conn_mod.add_server(
        "l", "python -m local --token=secret", tier="local", repo_path=str(repo)
    )
    discovered = conn_mod.discover_mcp_servers(str(repo))
    assert discovered["p"]["source"] == "project"
    assert discovered["l"]["source"] == "local"
    assert ".neo/connectors.local.toml" in (repo / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert conn_mod.remove_server("p", tier="project", repo_path=str(repo)) == "p"
    assert conn_mod.remove_server("l", tier="local", repo_path=str(repo)) == "l"
    assert conn_mod.discover_mcp_servers(str(repo)) == {}


def test_cli_connector_tier_roundtrip(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / ".neo").mkdir(parents=True)
    monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".neo"))
    assert (
        _cli(
            [
                "mcp",
                "add",
                "project",
                "--tier",
                "project",
                "--repo",
                str(repo),
                "--",
                "python",
                "-m",
                "server",
            ]
        )
        == 0
    )
    assert "project settings" in capsys.readouterr().out
    assert _cli(["mcp", "list", "--repo", str(repo)]) == 0
    assert "project" in capsys.readouterr().out
    assert (
        _cli(["mcp", "remove", "project", "--tier", "project", "--repo", str(repo)])
        == 0
    )
    assert "project tier" in capsys.readouterr().out


def test_connector_call_is_bounded_and_redacted(monkeypatch):
    import time

    import memory.mcp_client as client

    monkeypatch.setattr(
        client,
        "call_mcp_tool",
        lambda *a, **k: time.sleep(0.2) or {"ok": True, "text": "late"},
    )
    result = conn_mod.call_tool("python -m server", "tool", timeout_s=0.02)
    assert result["ok"] is False
    assert "timed out" in result["error"]

    monkeypatch.setattr(
        client,
        "call_mcp_tool",
        lambda *a, **k: {"ok": False, "error": "url=https://u:p@host/v1?key=secret"},
    )
    from cli.neoconfig import redact_text

    assert "secret" not in redact_text(conn_mod.call_tool("srv", "tool")["error"])


def test_mcp_call_label_roundtrip(monkeypatch, capsys):
    conn_mod.add_server("memory", "python -m mcp_server")
    seen = {}

    def call(ref, tool, args, **kwargs):
        seen.update({"ref": ref, "tool": tool, "args": args})
        return {"ok": True, "text": "memory-ok"}

    monkeypatch.setattr(conn_mod, "call_tool", call)
    rc = _cli(["mcp", "call", "memory", "query_decisions", "--args", "{}"])
    assert rc == 0
    assert seen == {"ref": "memory", "tool": "query_decisions", "args": {}}
    assert "memory-ok" in capsys.readouterr().out
