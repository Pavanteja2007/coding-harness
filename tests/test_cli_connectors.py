"""MCP connectors tests (unified plugins round).

Covers: `vex mcp add/remove/list` persisted in the global settings'
[mcp_servers] table (through cli.vexconfig's existing tier machinery),
label validation, project connectors.toml + local overrides with
global<project<local precedence, plugin-server discovery (and its
disabling), `vex mcp health` ok/fail reporting without tracebacks,
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
    monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "settings.toml"))
    monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
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
    # written through cli.vexconfig's serializer)
    text = Path(__import__("os").environ["VEX_CONFIG"]).read_text(encoding="utf-8")
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


def _write_connectors(repo_vex: Path, name: str, table: dict) -> None:
    lines = ["[mcp_servers]"]
    for k, v in table.items():
        lines.append(f"{json.dumps(k)} = {json.dumps(v)}")
    (repo_vex / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_and_local_precedence(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    vex = repo / ".vex"
    vex.mkdir(parents=True)
    monkeypatch.setenv("VEX_PROJECT_DIR", str(vex))

    conn_mod.add_server("demo", "python -m global-cmd")
    _write_connectors(
        vex,
        "connectors.toml",
        {"demo": "python -m project-cmd", "proj": "python -m proj-cmd"},
    )
    disc = conn_mod.discover_mcp_servers(str(repo))
    assert disc["demo"] == {"command": "python -m project-cmd", "source": "project"}
    assert disc["proj"]["source"] == "project"

    _write_connectors(vex, "connectors.local.toml", {"proj": "python -m local-cmd"})
    disc = conn_mod.discover_mcp_servers(str(repo))
    assert disc["proj"] == {"command": "python -m local-cmd", "source": "local"}
    # project still wins over global where local is silent
    assert disc["demo"]["source"] == "project"
    # local file is the personal-override layer, not the committable one
    assert (vex / "connectors.local.toml").is_file()


def test_broken_connectors_file_degrades_not_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    vex = repo / ".vex"
    vex.mkdir(parents=True)
    monkeypatch.setenv("VEX_PROJECT_DIR", str(vex))
    (vex / "connectors.toml").write_text("not valid toml [[[\n", encoding="utf-8")
    assert conn_mod.project_servers(str(repo)) == {}
    assert conn_mod.discover_mcp_servers(str(repo)) == {}


def test_broken_global_file_refuses_overwrite(tmp_path):
    gp = Path(__import__("os").environ["VEX_CONFIG"])
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
    conn_mod.add_server("bad", "python -m definitely_not_a_module_vex")
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
