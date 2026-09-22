"""Second slash-command surface round (cli_wiring): /init /model /login
/logout /mcp /skills /cost /undo /clear in BOTH shells.

REPL half drives cli.interactive._slash_command directly (offline, no
model/Docker); TUI half drives the REAL VexApp through Pilot
(app.run_test()) and asserts on the transcript. Shared helpers
(trace_usage_sum, history_matches, undo_result) are pinned directly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import commands as commands_mod

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def _isolated_env(tmp_path, monkeypatch):
    """Isolated home + harness home so settings/memory never touch prod."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    hhome = tmp_path / "harness-home"
    hhome.mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(hhome))
    monkeypatch.setenv("HARNESS_DECISIONS_DB", str(hhome / "decisions.db"))
    yield tmp_path


@pytest.fixture
def clean_hooks():
    import cli.interactive as iv

    old_start, old_cancel = iv._ON_TASK_START, iv._CANCEL_RUN
    iv._ON_TASK_START, iv._CANCEL_RUN = None, None
    yield
    iv._ON_TASK_START, iv._CANCEL_RUN = old_start, old_cancel
    iv._clear_live_run()


def _state(repo, **kw):
    base = {
        "model": None,
        "provider": None,
        "plan_preview": None,
        "quiet": False,
        "repo": str(repo),
        "file_config": {},
    }
    base.update(kw)
    return base


def _transcript_plain(app) -> str:
    from rich.text import Text

    t = Text()
    for line in app.query_one("#vex-body").lines:
        for seg in line._segments:
            t.append(seg.text, style=seg.style)
    return t.plain


def _make_tui_app(tmp_path, repo=None):
    import cli.tui as t

    repo = repo or (tmp_path / "repo")
    repo.mkdir(exist_ok=True)
    return t.VexApp(
        repo=repo,
        log_root=tmp_path / "logs",
        state={"repo": str(repo), "file_config": {}},
        file_config={},
    )


# -- builtin shadowing guard ------------------------------------------------


NEW_BUILTINS = [
    "/init",
    "/model",
    "/login",
    "/logout",
    "/mcp",
    "/skills",
    "/cost",
    "/undo",
    "/clear",
]


def test_new_names_are_builtins_never_shadowed(tmp_path, _isolated_env):
    repo = tmp_path / "repo"
    cmds = repo / ".vex" / "commands"
    cmds.mkdir(parents=True)
    for name in NEW_BUILTINS:
        assert name in commands_mod.BUILTIN_SLASH_COMMANDS, name
        (cmds / f"{name.lstrip('/')}.md").write_text(
            "shadow body $ARGUMENTS", encoding="utf-8"
        )
        assert commands_mod.load_command(name.lstrip("/"), str(repo)) is None
    listed = commands_mod.list_commands(str(repo))
    for name in NEW_BUILTINS:
        assert name.lstrip("/") not in listed


def test_help_lists_new_commands():
    from cli.interactive import _HELP

    for name in NEW_BUILTINS:
        assert name in _HELP, name


# -- /init ------------------------------------------------------------------


def test_repl_init_scaffolds_then_idempotent(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "logs"
    log.mkdir()
    assert _slash_command("/init", "/init", {}, log, _state(repo)) is None
    out = capsys.readouterr().out
    assert "repo setup" in out
    assert (repo / ".vex" / "settings.toml").is_file()
    assert _slash_command("/init", "/init", {}, log, _state(repo)) is None
    assert "already set up" in capsys.readouterr().out


def test_repl_init_rejects_args(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert (
        _slash_command("/init extra", "/init extra", {}, tmp_path, _state(tmp_path))
        is None
    )
    assert "usage: /init" in capsys.readouterr().out


# -- /model -----------------------------------------------------------------


def test_repl_model_show_and_pin(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    state = _state(tmp_path)
    assert _slash_command("/model", "/model", {}, tmp_path, state) is None
    assert "model:" in capsys.readouterr().out
    assert _slash_command("/model gpt-x", "/model gpt-x", {}, tmp_path, state) is None
    assert state["model"] == "gpt-x"
    assert "pinned" in capsys.readouterr().out


# -- /login /logout ----------------------------------------------------------


def test_repl_login_bad_tier_is_usage(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert (
        _slash_command("/login bogus", "/login bogus", {}, tmp_path, _state(tmp_path))
        is None
    )
    assert "usage: /login" in capsys.readouterr().out


def test_repl_login_success_reloads_config(
    tmp_path, _isolated_env, monkeypatch, capsys
):
    import cli.onboard as ob
    from cli.interactive import _slash_command

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ob, "run_repl_wizard", lambda model_tier="global": True)
    state = _state(tmp_path)
    assert _slash_command("/login", "/login", {}, tmp_path, state) is None
    assert "model configured" in capsys.readouterr().out


def test_repl_logout_nothing_stored_and_removal(tmp_path, _isolated_env, capsys):
    from cli import vexconfig
    from cli.interactive import _slash_command

    assert _slash_command("/logout", "/logout", {}, tmp_path, _state(tmp_path)) is None
    assert "nothing to remove" in capsys.readouterr().out
    vexconfig.set_tier_key("global", "api_key", "sk-test-123")
    assert _slash_command("/logout", "/logout", {}, tmp_path, _state(tmp_path)) is None
    assert "logged out" in capsys.readouterr().out


def test_repl_logout_rejects_args(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert (
        _slash_command("/logout now", "/logout now", {}, tmp_path, _state(tmp_path))
        is None
    )
    assert "usage: /logout" in capsys.readouterr().out


# -- /mcp --------------------------------------------------------------------


def test_repl_mcp_empty_lists_nothing(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert _slash_command("/mcp", "/mcp", {}, tmp_path, _state(tmp_path)) is None
    assert "no MCP servers" in capsys.readouterr().out


def test_repl_mcp_lists_plugin_servers(tmp_path, _isolated_env, monkeypatch, capsys):
    import cli.plugins as plugins_mod
    from cli.interactive import _slash_command

    monkeypatch.setattr(
        plugins_mod,
        "list_plugins",
        lambda: [{"name": "p", "mcp_servers": {"mem": "python -m mcp_server"}}],
    )
    assert _slash_command("/mcp", "/mcp", {}, tmp_path, _state(tmp_path)) is None
    out = capsys.readouterr().out
    assert "mem" in out and "plugin:p" in out


def test_repl_mcp_unknown_label_and_tools(tmp_path, _isolated_env, monkeypatch, capsys):
    import cli.plugins as plugins_mod
    import harness.agent_loop as al
    import memory.mcp_client as mc
    from cli.interactive import _slash_command

    monkeypatch.setattr(
        plugins_mod,
        "list_plugins",
        lambda: [{"name": "p", "mcp_servers": {"mem": "python -m mcp_server"}}],
    )
    assert (
        _slash_command("/mcp nope", "/mcp nope", {}, tmp_path, _state(tmp_path)) is None
    )
    assert "unknown MCP server" in capsys.readouterr().out
    monkeypatch.setattr(al, "_resolve_mcp_server", lambda label, cfg: "cmd")
    monkeypatch.setattr(
        mc,
        "list_mcp_tools",
        lambda cmd: {
            "ok": True,
            "tools": [{"name": "t1", "description": "does things"}],
        },
    )
    assert (
        _slash_command("/mcp mem", "/mcp mem", {}, tmp_path, _state(tmp_path)) is None
    )
    out = capsys.readouterr().out
    assert "t1" in out


def test_repl_mcp_multiarg_is_usage(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert (
        _slash_command("/mcp a b", "/mcp a b", {}, tmp_path, _state(tmp_path)) is None
    )
    assert "usage: /mcp" in capsys.readouterr().out


# -- /skills -----------------------------------------------------------------


def _write_skill(repo: Path) -> None:
    d = repo / ".vex" / "skills" / "my-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: reviews auth code\n---\n\nBody lines here.\n",
        encoding="utf-8",
    )


def test_repl_skills_empty_then_project(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    repo.mkdir()
    assert _slash_command("/skills", "/skills", {}, tmp_path, _state(repo)) is None
    assert "no skills discovered" in capsys.readouterr().out
    _write_skill(repo)
    assert _slash_command("/skills", "/skills", {}, tmp_path, _state(repo)) is None
    out = capsys.readouterr().out
    assert "my-skill" in out and "project" in out
    assert (
        _slash_command("/skills zzz", "/skills zzz", {}, tmp_path, _state(repo)) is None
    )
    assert "no skills match" in capsys.readouterr().out


# -- /cost -------------------------------------------------------------------


def _write_trace(log_root: Path, tid: str, events) -> None:
    d = log_root / tid
    d.mkdir(parents=True, exist_ok=True)
    with (d / "trace.jsonl").open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def test_trace_usage_sum_unit(tmp_path, _isolated_env):
    from cli.interactive import trace_usage_sum

    assert trace_usage_sum(tmp_path / "nope.jsonl") == (0, 0, 0.0)
    tf = tmp_path / "t.jsonl"
    tf.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 100, "cost": 0.01}},
                    }
                ),
                "garbage{",
                json.dumps({"kind": "plan", "data": {}}),
                json.dumps(
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 50, "cost": 0.02}},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    assert trace_usage_sum(tf) == (2, 150, pytest.approx(0.03))


def test_repl_cost_no_run_then_sums(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    log = tmp_path / "logs"
    log.mkdir()
    assert _slash_command("/cost", "/cost", {}, log, _state(tmp_path)) is None
    out = capsys.readouterr().out
    assert "no run in this session yet" in out and "session total" in out
    _write_trace(
        log,
        "t1",
        [
            {
                "kind": "model_response",
                "data": {"usage": {"tokens": 10, "cost": 0.005}},
            },
            {
                "kind": "model_response",
                "data": {"usage": {"tokens": 20, "cost": 0.007}},
            },
        ],
    )
    last = {"task_id": "t1"}
    assert _slash_command("/cost", "/cost", last, log, _state(tmp_path)) is None
    out = capsys.readouterr().out
    assert "last run" in out and "2 model call(s)" in out and "$" in out


def test_repl_cost_rejects_args(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert _slash_command("/cost x", "/cost x", {}, tmp_path, _state(tmp_path)) is None
    assert "usage: /cost" in capsys.readouterr().out


# -- /undo + /diff undo --------------------------------------------------------


def _patch_undo(monkeypatch):
    import harness.agent_loop as al

    monkeypatch.setattr(
        al, "undo_edits", lambda *a, **k: {"restored": ["a.py"], "deleted": []}
    )
    monkeypatch.setattr(al, "agent_diff", lambda *a, **k: "diff --git a.py")


def test_repl_undo_not_agent_and_done(tmp_path, _isolated_env, monkeypatch, capsys):
    from cli.interactive import _slash_command

    _patch_undo(monkeypatch)
    assert _slash_command("/undo", "/undo", {}, tmp_path, _state(tmp_path)) is None
    assert "agent sessions" in capsys.readouterr().out
    last = {"task_id": "agent-abc"}
    assert _slash_command("/undo", "/undo", last, tmp_path, _state(tmp_path)) is None
    out = capsys.readouterr().out
    assert "undone 1 file(s)" in out
    assert last["diff"] == "diff --git a.py"
    # /diff undo shares the core
    last2 = {"task_id": "agent-abc"}
    assert (
        _slash_command("/diff undo", "/diff undo", last2, tmp_path, _state(tmp_path))
        is None
    )
    assert "undone 1 file(s)" in capsys.readouterr().out


def test_repl_undo_hostile_never_tracebacks(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    last = {"task_id": "agent-abc"}
    assert (
        _slash_command(
            "/undo \x00../../x", "/undo \x00../../x", last, tmp_path, _state(tmp_path)
        )
        is None
    )
    capsys.readouterr()  # any honest line, no raise


# -- /clear --------------------------------------------------------------------


def test_repl_clear_fresh_old_kept(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command
    from cli.session import save_session

    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "logs"
    log.mkdir()
    conv = {"session_id": "sess-old", "turns": [], "history": ["hi"], "summary": ""}
    save_session(log, conv)
    state = _state(repo, conversation=conv)
    assert _slash_command("/clear", "/clear", {}, log, state) is None
    out = capsys.readouterr().out
    assert "cleared" in out and "sess-old" in out
    assert state["conversation"]["session_id"] != "sess-old"
    assert (log / "_conversations" / "sess-old.json").is_file()


def test_repl_clear_rejects_args(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    assert (
        _slash_command("/clear x", "/clear x", {}, tmp_path, _state(tmp_path)) is None
    )
    assert "usage: /clear" in capsys.readouterr().out


# -- /history grammar ------------------------------------------------------------


def test_history_matches_grammar():
    from cli.interactive import history_matches

    assert history_matches("fix the login bug", "")
    assert history_matches("fix the login bug", "fix login")
    assert not history_matches("run pytest", "fix login")
    # key:value tokens narrow honestly (history carries no status)
    assert not history_matches("fix the login bug", "status:failed")


def test_repl_history_uses_grammar(tmp_path, _isolated_env, capsys):
    from cli.interactive import _slash_command

    state = _state(
        tmp_path,
        conversation={
            "session_id": "s",
            "turns": [],
            "history": ["fix the login bug", "run pytest suite", "fix login redirect"],
            "summary": "",
        },
    )
    assert (
        _slash_command("/history fix login", "/history fix login", {}, tmp_path, state)
        is None
    )
    out = capsys.readouterr().out
    assert "fix the login bug" in out and "fix login redirect" in out
    assert "run pytest suite" not in out
    assert (
        _slash_command(
            "/history status:failed", "/history status:failed", {}, tmp_path, state
        )
        is None
    )
    assert "no history matches" in capsys.readouterr().out


# -- TUI half ---------------------------------------------------------------------


def test_tui_palette_finds_new_entries(tmp_path, _isolated_env):
    import cli.tui as t

    app = _make_tui_app(tmp_path)
    labels = [e["label"] for e in app._palette_entries()]
    for cmd in NEW_BUILTINS:
        assert cmd in labels, cmd
    scr = t._PaletteScreen(app._palette_entries())
    assert "/cost" in [e["label"] for e in scr._rank("cst")][:5]
    assert "/mcp" in [e["label"] for e in scr._rank("mcp")][:5]


class TestTuiNewSlashes:
    async def _drive(self, app, pilot, line: str) -> str:
        from textual.widgets import Input

        app.query_one("#vex-input", Input).value = line
        await pilot.press("enter")
        await pilot.pause()
        return _transcript_plain(app)

    async def test_cost(self, tmp_path, _isolated_env, clean_hooks):
        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            _write_trace(
                tmp_path / "logs",
                "t1",
                [
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 5, "cost": 0.001}},
                    }
                ],
            )
            app.last["task_id"] = "t1"
            plain = await self._drive(app, pilot, "/cost")
            assert "last run" in plain and "session total" in plain
            plain = await self._drive(app, pilot, "/cost extra")
            assert "usage: /cost" in plain

    async def test_skills(self, tmp_path, _isolated_env, clean_hooks):
        repo = tmp_path / "repo"
        app = _make_tui_app(tmp_path, repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = await self._drive(app, pilot, "/skills")
            assert "no skills discovered" in plain
            _write_skill(repo)
            plain = await self._drive(app, pilot, "/skills")
            assert "my-skill" in plain and "project" in plain

    async def test_mcp(self, tmp_path, _isolated_env, clean_hooks, monkeypatch):
        import cli.plugins as plugins_mod

        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = await self._drive(app, pilot, "/mcp")
            assert "no MCP servers" in plain
            monkeypatch.setattr(
                plugins_mod,
                "list_plugins",
                lambda: [{"name": "p", "mcp_servers": {"mem": "cmd"}}],
            )
            plain = await self._drive(app, pilot, "/mcp")
            assert "mem" in plain

    async def test_init(self, tmp_path, _isolated_env, clean_hooks):
        repo = tmp_path / "repo"
        app = _make_tui_app(tmp_path, repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = await self._drive(app, pilot, "/init")
            assert "repo setup" in plain
            assert (repo / ".vex" / "settings.toml").is_file()
            plain = await self._drive(app, pilot, "/init x")
            assert "usage: /init" in plain

    async def test_login_opens_modal_then_esc(
        self, tmp_path, _isolated_env, clean_hooks
    ):
        import cli.tui as t

        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = await self._drive(app, pilot, "/login bogus")
            assert "usage: /login" in plain
            from textual.widgets import Input

            app.query_one("#vex-input", Input).value = "/login"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, t._OnboardScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert "offline mode" in _transcript_plain(app)

    async def test_logout(self, tmp_path, _isolated_env, clean_hooks):
        from cli import vexconfig

        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            vexconfig.set_tier_key("global", "api_key", "sk-test-1")
            plain = await self._drive(app, pilot, "/logout")
            assert "logged out" in plain
            plain = await self._drive(app, pilot, "/logout x")
            assert "usage: /logout" in plain

    async def test_clear(self, tmp_path, _isolated_env, clean_hooks):
        from cli.session import save_session

        repo = tmp_path / "repo"
        app = _make_tui_app(tmp_path, repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            old_id = app.conversation["session_id"]
            save_session(app.log_root, app.conversation)
            plain = await self._drive(app, pilot, "/clear")
            assert "cleared" in plain
            assert app.conversation["session_id"] != old_id
            assert (app.log_root / "_conversations" / f"{old_id}.json").is_file()

    async def test_undo(self, tmp_path, _isolated_env, clean_hooks, monkeypatch):
        _patch_undo(monkeypatch)
        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            plain = await self._drive(app, pilot, "/undo")
            assert "agent sessions" in plain
            app.last["task_id"] = "agent-abc"
            plain = await self._drive(app, pilot, "/undo")
            assert "undone 1 file(s)" in plain

    async def test_history_grammar(self, tmp_path, _isolated_env, clean_hooks):
        from cli.session import append_history, save_session

        app = _make_tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            for line in ["fix the login bug", "run pytest suite"]:
                append_history(app.conversation, line)
            save_session(app.log_root, app.conversation)
            plain = await self._drive(app, pilot, "/history fix login")
            assert "fix the login bug" in plain
            assert "run pytest suite" not in plain
