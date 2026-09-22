"""Custom commands + plugins tests (Plugins round, Tasks B and C).

Covers: command template discovery/precedence/$ARGUMENTS substitution,
built-in shadowing protection, the interactive /<name> dispatch (echo +
run-as-fix-request), plugin manifest validation (implicit + explicit),
install from local path / git URL, list, remove, replace-on-reinstall,
and the plugin tool-verb extension of the BATCH allowlist (including
the hostile-verb deny list).
"""

import json
from pathlib import Path

import pytest

from cli import commands as commands_mod
from cli import plugins as plugins_mod


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Isolated ~/.config so global skills/commands/plugins and the
    installed-plugin root never touch the real user home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(
        "USERPROFILE" if __import__("os").name == "nt" else "HOME", str(home)
    )
    # Path.home() consults HOME (posix) / USERPROFILE (win) — patching
    # the env is enough on both, but be explicit for belt-and-braces:
    monkeypatch.setattr(Path, "home", lambda: home)
    yield home


# ---------------------------------------------------------------------------
# Task B — custom commands
# ---------------------------------------------------------------------------


def _write_command(root: Path, name: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(body, encoding="utf-8")


def test_load_command_from_project_root(tmp_path):
    repo = tmp_path / "repo"
    _write_command(
        repo / ".vex" / "commands",
        "review",
        "# Review $ARGUMENTS\nDo the review of $ARGUMENTS carefully.",
    )
    tpl = commands_mod.load_command("review", repo_path=str(repo))
    assert tpl is not None
    assert "$ARGUMENTS" in tpl


def test_load_command_from_global_root(tmp_path):
    _write_command(
        Path.home() / ".config" / "vex" / "commands",
        "globalcmd",
        "global command body",
    )
    assert commands_mod.load_command("globalcmd", repo_path=None) == (
        "global command body"
    )


def test_project_beats_global_on_collision(tmp_path):
    repo = tmp_path / "repo"
    _write_command(repo / ".vex" / "commands", "dup", "PROJECT VERSION")
    _write_command(
        Path.home() / ".config" / "vex" / "commands", "dup", "GLOBAL VERSION"
    )
    assert commands_mod.load_command("dup", repo_path=str(repo)) == "PROJECT VERSION"


def test_builtin_commands_never_shadowed():
    _write_command(
        Path.home() / ".config" / "vex" / "commands",
        "help",
        "hostile /help replacement must not load",
    )
    assert commands_mod.load_command("help") is None
    assert commands_mod.load_command("status") is None


def test_load_command_rejects_unsafe_names():
    assert commands_mod.load_command("../escape") is None
    assert commands_mod.load_command("has/slash") is None
    assert commands_mod.load_command("") is None
    assert commands_mod.load_command(".hidden") is None


def test_fill_template_substitutes_arguments():
    tpl = "Review $ARGUMENTS now"
    assert (
        commands_mod.fill_template(tpl, "the auth module")
        == "Review the auth module now"
    )
    assert commands_mod.fill_template(tpl, "") == "Review  now"
    # no slot -> unchanged
    assert commands_mod.fill_template("no slot", "args") == "no slot"


def test_list_commands_and_names(tmp_path):
    repo = tmp_path / "repo"
    _write_command(repo / ".vex" / "commands", "review", "Review the code.")
    _write_command(repo / ".vex" / "commands", "audit", "# Audit\nAudit stuff.")
    listed = commands_mod.list_commands(str(repo))
    assert set(listed) == {"review", "audit"}
    assert "Review the code." in listed["review"]
    # heading lines are skipped for the description
    assert listed["audit"] == "Audit stuff."
    assert commands_mod.command_names(str(repo)) == ["audit", "review"]


def test_list_skips_builtins_and_hidden(tmp_path):
    _write_command(
        Path.home() / ".config" / "vex" / "commands", "help", "shadow attempt"
    )
    _write_command(Path.home() / ".config" / "vex" / "commands", ".hidden", "hidden")
    assert commands_mod.list_commands() == {}


# ---------------------------------------------------------------------------
# interactive dispatch: /<custom> runs as a fix request
# ---------------------------------------------------------------------------


def test_slash_custom_command_runs_fix_request(tmp_path, monkeypatch, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_command(
        repo / ".vex" / "commands",
        "review",
        "# Review $ARGUMENTS\nRun the review of $ARGUMENTS.",
    )
    ran = {}

    def fake_run(issue, repo_arg, state, log_root, file_config=None):
        ran["issue"] = issue
        ran["repo"] = repo_arg
        return {"task_id": "t-1", "diff": "x", "status": "success"}

    monkeypatch.setattr("cli.interactive._run_one_fix", fake_run)
    state = {"repo": str(repo), "file_config": {}}
    out = _slash_command(
        "/review the auth module", "/review the auth module", {}, tmp_path, state
    )
    assert out is None
    assert "the auth module" in ran["issue"]
    assert "Run the review of the auth module." in ran["issue"]
    assert Path(ran["repo"]) == repo
    captured = capsys.readouterr()
    assert "running as a fix request" in captured.out


def test_slash_custom_command_without_arguments(tmp_path, monkeypatch, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_command(
        repo / ".vex" / "commands", "fullcheck", "Check everything ($ARGUMENTS)."
    )
    ran = {}

    monkeypatch.setattr(
        "cli.interactive._run_one_agent",
        lambda issue, r, s, l, file_config=None, task_id=None: (
            ran.update(issue=issue) or None
        ),
    )
    state = {"repo": str(repo), "file_config": {}}
    _slash_command("/fullcheck", "/fullcheck", {}, tmp_path, state)
    assert "Check everything ()." in ran["issue"]


def test_slash_unknown_lists_available_customs(tmp_path, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_command(repo / ".vex" / "commands", "deploy", "Deploy it.")
    state = {"repo": str(repo)}
    out = _slash_command("/nope", "/nope", {}, tmp_path, state)
    assert out == "unknown"
    captured = capsys.readouterr()
    assert "/deploy" in captured.out


# ---------------------------------------------------------------------------
# Task C — plugins
# ---------------------------------------------------------------------------


PLUGIN_SRC = Path(__file__).parent / "fixtures" / "plugin-webapp-toolkit"


def test_read_manifest_explicit():
    mf = plugins_mod.read_manifest(PLUGIN_SRC)
    assert mf["name"] == "webapp-toolkit"
    assert "skills/django-style" in mf["skills"]
    assert "commands/review.md" in mf["commands"]
    assert mf["tools"]["verbs"] == ["ruff", "ruff check", "ruff --version"]
    assert mf["mcp_servers"]["structure-memory"] == "python -m mcp_server"


def test_validate_plugin_ok():
    mf = plugins_mod.read_manifest(PLUGIN_SRC)
    plugins_mod.validate_plugin(PLUGIN_SRC, mf)  # no raise


def test_validate_rejects_bad_shapes(tmp_path):
    # missing SKILL.md in a listed skill dir
    src = tmp_path / "p1"
    (src / "skills" / "broken").mkdir(parents=True)
    (src / "plugin.json").write_text(
        json.dumps({"name": "p1", "skills": ["skills/broken"]}), encoding="utf-8"
    )
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.validate_plugin(src, plugins_mod.read_manifest(src))

    # command entry not a file
    src2 = tmp_path / "p2"
    (src2 / "commands").mkdir(parents=True)
    (src2 / "plugin.json").write_text(
        json.dumps({"name": "p2", "commands": ["commands/gone.md"]}),
        encoding="utf-8",
    )
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.validate_plugin(src2, plugins_mod.read_manifest(src2))

    # unparseable manifest
    src3 = tmp_path / "p3"
    src3.mkdir()
    (src3 / "plugin.json").write_text("not json {", encoding="utf-8")
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.read_manifest(src3)

    # bad name
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.validate_plugin(src3, {"name": "../escape"})


def test_implicit_manifest_from_layout(tmp_path):
    src = tmp_path / "implicit-plugin"
    (src / "skills" / "alpha").mkdir(parents=True)
    (src / "skills" / "alpha" / "SKILL.md").write_text("alpha body", encoding="utf-8")
    (src / "commands").mkdir()
    (src / "commands" / "do.md").write_text("do the thing", encoding="utf-8")
    mf = plugins_mod._implicit_manifest(src)
    assert mf["name"] == "implicit-plugin"
    assert mf["skills"] == ["skills/alpha"]
    assert mf["commands"] == ["commands/do.md"]


def test_implicit_manifest_rejects_unrecognized_shape(tmp_path):
    src = tmp_path / "not-a-plugin"
    src.mkdir()
    (src / "random").mkdir()
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod._implicit_manifest(src)


def test_install_from_local_and_discovery(tmp_path):
    # install the shipped example plugin
    name = plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert name == "webapp-toolkit"
    dest = plugins_mod.plugins_root() / "webapp-toolkit"
    assert (dest / "plugin.json").is_file()
    assert (dest / "skills" / "django-style" / "SKILL.md").is_file()

    listed = plugins_mod.list_plugins()
    assert [p["name"] for p in listed] == ["webapp-toolkit"]
    entry = listed[0]
    assert entry["skills_on_disk"] == ["django-style"]
    assert entry["commands_on_disk"] == ["review"]
    assert entry["tools"]["verbs"] == ["ruff", "ruff check", "ruff --version"]

    # installed skills are discoverable by the harness's skill scan
    from harness import skills as skills_mod

    found = skills_mod.discover_skills(repo_path=str(tmp_path))
    names = [s.name for s in found]
    assert "django-style" in names
    django = next(s for s in found if s.name == "django-style")
    assert django.origin == "plugin"

    # installed commands load through the command surface
    tpl = commands_mod.load_command("review", repo_path=None)
    assert tpl is not None and "$ARGUMENTS" in tpl


def test_install_replaces_existing(tmp_path):
    name = plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert name == "webapp-toolkit"
    # install again (upgrade path): replace, not error
    name2 = plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert name2 == "webapp-toolkit"
    assert len(plugins_mod.list_plugins()) == 1


def test_install_rejects_missing_source():
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.install_from_local("C:/definitely/not/here")


def test_remove_roundtrip(tmp_path):
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert plugins_mod.remove("webapp-toolkit") == "webapp-toolkit"
    assert plugins_mod.list_plugins() == []
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.remove("webapp-toolkit")  # already gone


def test_remove_rejects_unsafe_name():
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.remove("../escape")


def test_install_from_git_dispatch(tmp_path, monkeypatch):
    cloned = {}

    def fake_git(url, name_override=None):
        cloned["url"] = url
        return "from-git"

    monkeypatch.setattr(plugins_mod, "install_from_git", fake_git)
    monkeypatch.setattr(
        plugins_mod, "install_from_local", lambda s, name_override=None: s
    )
    # git-shaped source dispatches to the git path
    assert plugins_mod.install("https://github.com/x/y.git") == "from-git"
    assert cloned["url"] == "https://github.com/x/y.git"
    # local path dispatches to the local path
    assert plugins_mod.install(str(PLUGIN_SRC)) == str(PLUGIN_SRC)


def test_install_from_git_bad_url():
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.install_from_git("not a url at all")


def test_install_from_git_clone_failure(tmp_path, monkeypatch):
    import subprocess as sp

    def fake_run(*a, **k):
        class R:
            returncode = 128
            stderr = "fatal: repository not found"
            stdout = ""

        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    with pytest.raises(plugins_mod.PluginError) as ei:
        plugins_mod.install_from_git("https://github.com/nope/nope.git")
    assert "repository not found" in str(ei.value)


# ---------------------------------------------------------------------------
# plugin tool verbs -> BATCH allowlist
# ---------------------------------------------------------------------------


def test_apply_tool_extensions_feeds_verbs(tmp_path, monkeypatch):

    applied = []
    monkeypatch.setattr(
        "harness.tools.extend_batch_verbs",
        lambda verbs: applied.extend(verbs),
    )
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    out = plugins_mod.apply_tool_extensions()
    assert "ruff check" in out
    assert "ruff" in applied


def test_plugin_verbs_never_whitelist_destructive(tmp_path):
    """The deny-token list in harness.tools must hold even when a plugin
    manifest (hand-edited post-install) asks for destructive verbs."""
    from harness import tools as tools_mod

    before = list(tools_mod._extra_batch_verbs)
    tools_mod.extend_batch_verbs(
        ["rm -rf", "sed -n p", "python -c x", "curl -s url", "git push", "ruff"]
    )
    try:
        after = tools_mod._extra_batch_verbs
        assert "ruff" in after
        assert not any(v.startswith("rm") for v in after)
        assert not any(v.startswith("sed") for v in after)
        assert not any(v.startswith("python") for v in after)
        assert not any(v.startswith("curl") for v in after)
        assert not any(v.startswith("git") for v in after)
        # and the actual validation refuses them as batch entries
        assert tools_mod.validate_batch(["rm -rf /"]) == "rm -rf /"
        assert tools_mod.validate_batch(["sed -n 1p f.py"]) == "sed -n 1p f.py"
        assert tools_mod.validate_batch(["ruff check src/"]) is None
    finally:
        tools_mod._extra_batch_verbs[:] = before


def test_extended_verbs_validate_like_builtins():
    from harness import tools as tools_mod

    before = list(tools_mod._extra_batch_verbs)
    tools_mod.extend_batch_verbs(["ruff check"])
    try:
        # allowed verb passes, composition still rejected
        assert tools_mod.validate_batch(["ruff check src/ || x"]) is not None
        assert tools_mod.validate_batch(["ruff check src/; rm x"]) is not None
        assert tools_mod.validate_batch(["ruff check src/"]) is None
        assert tools_mod.validate_batch(["cat a.py", "ruff check b/"]) is None
    finally:
        tools_mod._extra_batch_verbs[:] = before


# ---------------------------------------------------------------------------
# CLI surface: vex plugin install/list/remove
# ---------------------------------------------------------------------------


def _cli(argv):
    from cli.main import main

    return main(argv)


def test_cli_plugin_roundtrip(capsys):
    rc = _cli(["plugin", "install", str(PLUGIN_SRC)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "installed plugin webapp-toolkit" in out
    assert "skills:" in out and "django-style" in out
    assert "/review" in out
    assert "ruff check" in out
    assert "structure-memory" in out

    rc = _cli(["plugin", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "webapp-toolkit" in out

    rc = _cli(["plugin", "remove", "webapp-toolkit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed plugin webapp-toolkit" in out
    rc = _cli(["plugin", "list"])
    assert rc == 0
    assert "webapp-toolkit" not in capsys.readouterr().out


def test_cli_plugin_errors_exit_2(capsys):
    rc = _cli(["plugin", "install", "C:/definitely/not/here"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
    rc = _cli(["plugin", "remove", "never-installed"])
    assert rc == 2


def test_cli_plugin_list_empty(capsys):
    rc = _cli(["plugin", "list"])
    assert rc == 0
    assert "no plugins installed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# enable / disable (unified plugins round: dir stays + .disabled marker)
# ---------------------------------------------------------------------------


def test_disable_enable_roundtrip_and_discovery(tmp_path):
    from harness import skills as skills_mod

    name = plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert name == "webapp-toolkit"
    assert plugins_mod.is_plugin_disabled(name) is False

    plugins_mod.disable(name)
    assert plugins_mod.is_plugin_disabled(name) is True
    dest = plugins_mod.plugins_root() / "webapp-toolkit"
    assert dest.is_dir()  # dir stays; only the marker mutes it
    assert (plugins_mod.plugins_root() / "webapp-toolkit.disabled").is_file()

    # discovery skips the disabled plugin everywhere
    assert "django-style" not in [
        s.name for s in skills_mod.discover_skills(repo_path=str(tmp_path))
    ]
    assert commands_mod.load_command("review", repo_path=None) is None
    assert plugins_mod.config_tool_verbs() == []

    # ...but the listing stays honest about what's installed
    listed = {p["name"]: p for p in plugins_mod.list_plugins()}
    assert listed["webapp-toolkit"]["enabled"] is False

    plugins_mod.enable(name)
    assert plugins_mod.is_plugin_disabled(name) is False
    assert "django-style" in [
        s.name for s in skills_mod.discover_skills(repo_path=str(tmp_path))
    ]
    assert commands_mod.load_command("review", repo_path=None) is not None
    assert "ruff check" in plugins_mod.config_tool_verbs()


def test_disable_enable_idempotent_and_unknown():
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    plugins_mod.disable("webapp-toolkit")
    plugins_mod.disable("webapp-toolkit")  # no-op, no raise
    plugins_mod.enable("webapp-toolkit")
    plugins_mod.enable("webapp-toolkit")  # no-op, no raise
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.disable("never-installed")
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.enable("never-installed")
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.disable("../escape")
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.enable("../escape")


def test_disabled_tool_verbs_stay_denied(tmp_path, monkeypatch):
    """Disabling mutes verbs; re-enabling never bypasses the deny list."""
    applied = []
    monkeypatch.setattr(
        "harness.tools.extend_batch_verbs",
        lambda verbs: applied.extend(verbs),
    )
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    plugins_mod.disable("webapp-toolkit")
    assert plugins_mod.apply_tool_extensions() == []
    assert applied == []
    plugins_mod.enable("webapp-toolkit")
    assert "ruff check" in plugins_mod.apply_tool_extensions()


def test_reinstall_clears_disabled_marker():
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    plugins_mod.disable("webapp-toolkit")
    assert plugins_mod.is_plugin_disabled("webapp-toolkit") is True
    # upgrade path: a fresh install is enabled again
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    assert plugins_mod.is_plugin_disabled("webapp-toolkit") is False


def test_remove_clears_disabled_marker():
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    plugins_mod.disable("webapp-toolkit")
    plugins_mod.remove("webapp-toolkit")
    assert not (plugins_mod.plugins_root() / "webapp-toolkit.disabled").exists()


def test_cli_plugin_enable_disable_roundtrip(capsys):
    rc = _cli(["plugin", "install", str(PLUGIN_SRC)])
    assert rc == 0
    capsys.readouterr()
    rc = _cli(["plugin", "disable", "webapp-toolkit"])
    assert rc == 0
    assert "disabled plugin webapp-toolkit" in capsys.readouterr().out
    rc = _cli(["plugin", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "webapp-toolkit" in out and "disabled" in out
    rc = _cli(["plugin", "enable", "webapp-toolkit"])
    assert rc == 0
    assert "enabled plugin webapp-toolkit" in capsys.readouterr().out


def test_cli_plugin_enable_disable_errors_exit_2(capsys):
    rc = _cli(["plugin", "disable", "never-installed"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
    rc = _cli(["plugin", "enable", "never-installed"])
    assert rc == 2
    rc = _cli(["plugin", "disable", "../escape"])
    assert rc == 2


# ---------------------------------------------------------------------------
# vex skills list/show (read-only view; plugin skills carry origin "plugin")
# ---------------------------------------------------------------------------


def test_cli_skills_list_shows_plugin_origin(capsys, tmp_path):
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    rc = _cli(["skills", "list", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "django-style" in out
    assert "plugin" in out


def test_cli_skills_list_empty(capsys, tmp_path):
    rc = _cli(["skills", "list", "--repo", str(tmp_path)])
    assert rc == 0
    assert "no skills installed" in capsys.readouterr().out


def test_cli_skills_show_body_on_demand(capsys, tmp_path):
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    rc = _cli(["skills", "show", "django-style", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "webapp-toolkit plugin" in out  # the skill body marker
    assert "plugin" in out


def test_cli_skills_show_unknown_exits_2(capsys, tmp_path):
    rc = _cli(["skills", "show", "no-such-skill", "--repo", str(tmp_path)])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_skills_list_hides_disabled_plugin(capsys, tmp_path):
    plugins_mod.install_from_local(str(PLUGIN_SRC))
    plugins_mod.disable("webapp-toolkit")
    rc = _cli(["skills", "list", "--repo", str(tmp_path)])
    assert rc == 0
    assert "django-style" not in capsys.readouterr().out
