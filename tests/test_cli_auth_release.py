from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import interactive as interactive
from cli import neoconfig
from cli import onboard as onboard


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith("NEO_") or "API_KEY" in upper:
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    harness = tmp_path / "harness"
    repo = tmp_path / "repo"
    project = repo / ".neo"
    logs = tmp_path / "logs"
    for path in (home, appdata, harness, project, logs):
        path.mkdir(parents=True, exist_ok=True)
    values = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "XDG_CONFIG_HOME": str(appdata / "xdg"),
        "HARNESS_HOME": str(harness),
        "HARNESS_DECISIONS_DB": str(harness / "memory" / "decisions.db"),
        "HARNESS_LOGS_DIR": str(logs),
        "NEO_CONFIG": str(tmp_path / "config" / "settings.toml"),
        "NEO_LEGACY_CONFIG": str(tmp_path / "legacy" / "config.toml"),
        "NEO_PROJECT_DIR": str(project),
        "NEO_TRACE_DIR": str(tmp_path / "trace"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    return {
        "root": tmp_path,
        "home": home,
        "repo": repo,
        "logs": logs,
        "global": Path(values["NEO_CONFIG"]),
        "legacy": Path(values["NEO_LEGACY_CONFIG"]),
        "project": project / "settings.toml",
        "local": project / "settings.local.toml",
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_logout_removes_every_persisted_tier_and_keeps_project_write_protected(env):
    paths = env
    _write(paths["global"], 'model = "m"\napi_key = "synthetic-global"\n')
    _write(paths["local"], 'api_key = "synthetic-local"\n')
    _write(paths["project"], 'api_key = "synthetic-project"\n')
    _write(paths["legacy"], 'api_key = "synthetic-legacy"\n')

    result = onboard.logout_result()

    assert result["state"] == "persisted_removed"
    assert result["persisted_removed"] is True
    assert result["env_active"] is False
    assert not result["errors"]
    assert set(result["removed"]) == {
        "global",
        "project-local",
        "legacy",
        "project",
    }
    for path in (paths["global"], paths["local"], paths["project"], paths["legacy"]):
        assert "api_key" not in neoconfig.load_neo_config(path)
    with pytest.raises(ValueError, match="refusing to store api_key"):
        neoconfig.set_tier_key("project", "api_key", "synthetic-new")


def test_logout_reports_env_only_without_printing_or_unsetting_it(
    env, monkeypatch, capsys
):
    secret = "synthetic-environment-value"
    monkeypatch.setenv("NEO_API_KEY", secret)

    result = onboard.logout_result()
    rc = onboard.cmd_logout(None)
    output = capsys.readouterr()

    assert result["state"] == "env_only"
    assert result["persisted_removed"] is False
    assert result["env_only"] is True
    assert result["env_active"] is True
    assert rc == 1
    assert "no persisted api_key found" in output.out
    assert "environment credentials remain active" in output.out
    assert "NEO_API_KEY" in output.out
    assert secret not in output.out + output.err
    assert os.environ["NEO_API_KEY"] == secret


def test_logout_reports_provider_environment_names_without_values(
    env, monkeypatch, capsys
):
    """Provider credential variables are reported by name only."""
    secret = "synthetic-provider-environment-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    result = onboard.logout_result()
    rc = onboard.cmd_logout(None)
    output = capsys.readouterr()

    assert result["environment_credentials"] == ["OPENROUTER_API_KEY"]
    assert rc == 1
    assert "OPENROUTER_API_KEY" in output.out
    assert secret not in output.out + output.err


def test_no_onboard_still_enforces_flag_command_credential_gate(env, monkeypatch):
    """The prompt suppression flag never bypasses the non-interactive gate."""
    monkeypatch.setenv("NEO_NO_ONBOARD", "1")
    monkeypatch.setenv("NEO_MODEL", "synthetic-model")

    assert onboard.missing_credentials_exit({}) == 4


def test_logout_with_persisted_and_environment_reports_both(env, monkeypatch, capsys):
    secret = "synthetic-environment-value"
    _write(env["global"], 'api_key = "synthetic-global"\n')
    monkeypatch.setenv("NEO_API_KEY", secret)

    rc = onboard.cmd_logout(None)
    output = capsys.readouterr()

    assert rc == 0
    assert "persisted api_key removed" in output.out
    assert "environment credentials remain active" in output.out
    assert "NEO_API_KEY" in output.out
    assert secret not in output.out + output.err
    assert os.environ["NEO_API_KEY"] == secret
    assert "api_key" not in neoconfig.load_neo_config(env["global"])


def test_child_logout_cannot_unset_parent_environment(env, monkeypatch):
    secret = "synthetic-parent-environment-value"
    monkeypatch.setenv("NEO_API_KEY", secret)
    child = subprocess.run(
        [sys.executable, "-m", "cli", "logout"],
        cwd=str(env["root"]),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert child.returncode == 1
    assert os.environ["NEO_API_KEY"] == secret
    assert secret not in child.stdout + child.stderr


def test_repl_logout_uses_selected_repo_for_project_tiers(env, monkeypatch):
    monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
    _write(env["local"], 'api_key = "synthetic-local"\n')
    state = {"repo": str(env["repo"]), "file_config": {"api_key": "cached"}}

    interactive._do_logout(state=state, repo=env["repo"])

    assert "api_key" not in neoconfig.load_neo_config(env["local"])
    assert "api_key" not in state["file_config"]


def test_repl_logout_reloads_cached_file_config(env, capsys):
    _write(env["global"], 'model = "m"\napi_key = "synthetic-global"\n')
    cached = {"model": "m", "api_key": "cached-secret"}
    state = {"file_config": cached}

    interactive._slash_command("/logout", "/logout", {}, env["logs"], state)

    assert "api_key" not in cached
    assert "api_key" not in state["file_config"]
    assert state["file_config"].get("model") == "m"
    assert "api_key" not in interactive._mode_config({}, state["file_config"])


def test_tui_logout_reloads_cached_file_config(env, monkeypatch):
    from cli import tui

    monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
    _write(env["local"], 'api_key = "synthetic-project-local"\n')
    _write(env["global"], 'model = "m"\napi_key = "synthetic-global"\n')
    state_config = {"model": "m", "api_key": "state-secret"}
    file_config = {"model": "m", "api_key": "tui-secret"}
    state = {"repo": str(env["repo"]), "file_config": state_config}
    app = tui.NeoApp(
        repo=env["repo"],
        log_root=env["logs"],
        state=state,
        file_config=file_config,
    )
    app.transcript = lambda _value: None
    monkeypatch.setattr(app, "_render_header", lambda: None)

    app._slash_command("/logout", "/logout")

    assert "api_key" not in file_config
    assert "api_key" not in state_config
    assert "api_key" not in neoconfig.load_neo_config(env["local"])
    assert file_config.get("model") == "m"
    assert state_config.get("model") == "m"
