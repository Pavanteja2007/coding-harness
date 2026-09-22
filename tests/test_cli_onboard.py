"""Tests for first-run model onboarding (`vex login` + no-model wizard).

Covers:
- Detection: effective model+key resolution (flags/env/files) decides
  whether the wizard triggers; VEX_NO_ONBOARD=1 opts out; local
  (Ollama-loopback) endpoints need no key.
- Save tiers: api_key+base_url ALWAYS land in the GLOBAL file (never
  the project file); the model goes global unless --tier project;
  official presets clear stale router bases; project-tier api_key
  writes are refused.
- Secrets: api_key masked in list/get; settings files chmod 600
  (POSIX best-effort).
- Wizard: Custom (base_url + model + key) happy path saves; a wrong
  key fails the live TEST with a retry and saves NOTHING; /skip
  saves nothing; official + Ollama paths.
- Gates: flag commands with no creds exit 4 without prompting
  (--json included, with a parseable JSON doc); `vex login` needs a
  TTY; `vex logout` strips the key; no-prompt under pipes/opt-out.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cli import main as m
from cli import onboard as ob
from cli import vexconfig


@pytest.fixture(autouse=True)
def _isolated_onboard_env(tmp_path, monkeypatch):
    """Point every tier at tmp_path so tests never touch the real
    config dirs, and no real VEX_* model env leaks in."""
    monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "global" / "settings.toml"))
    monkeypatch.setenv("VEX_LEGACY_CONFIG", str(tmp_path / "legacy" / "config.toml"))
    monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / "proj" / ".vex"))
    for var in (*vexconfig._ENV_KEYS, "VEX_NO_ONBOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _feeder(values):
    it = iter(values)

    def _feed(prompt: str = "") -> str:
        return next(it)

    return _feed


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    def test_nothing_set_needs_onboarding(self):
        assert ob.needs_onboarding() is True

    def test_model_and_key_set_ok(self):
        gp = vexconfig.global_settings_path()
        _write(gp, 'model = "gpt-4o-mini"\napi_key = "sk-test"\n')
        assert ob.needs_onboarding() is False

    def test_model_without_key_needs_onboarding(self):
        gp = vexconfig.global_settings_path()
        _write(gp, 'model = "gpt-4o-mini"\n')
        ok, reason = ob.credentials_status(ob.effective_credentials())
        assert ok is False and "api_key" in reason
        assert ob.needs_onboarding() is True

    def test_local_endpoint_needs_no_key(self):
        gp = vexconfig.global_settings_path()
        _write(
            gp,
            'model = "qwen2.5"\nbase_url = "http://localhost:11434/v1"\n',
        )
        ok, _ = ob.credentials_status(ob.effective_credentials())
        assert ok is True
        assert ob.needs_onboarding() is False

    def test_env_creds_satisfy(self, monkeypatch):
        monkeypatch.setenv("VEX_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("VEX_API_KEY", "sk-env")
        assert ob.needs_onboarding() is False

    def test_flags_satisfy(self):
        assert ob.needs_onboarding({"model": "m", "api_key": "k"}) is False

    def test_opt_out_never_needs(self, monkeypatch):
        monkeypatch.setenv("VEX_NO_ONBOARD", "1")
        assert ob.needs_onboarding() is False

    def test_second_vex_no_prompt_after_save(self):
        ob.save_credentials("openai", "gpt-4o-mini", "sk-x", None, "global")
        assert ob.needs_onboarding() is False


class TestPromptAllowed:
    def test_json_never_prompts(self):
        assert ob.prompt_allowed(as_json=True) is False

    def test_opt_out_never_prompts(self, monkeypatch):
        monkeypatch.setenv("VEX_NO_ONBOARD", "1")
        assert ob.prompt_allowed() is False

    def test_pipe_never_prompts(self, monkeypatch):
        class _Stdin:
            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdin", _Stdin())
        assert ob.prompt_allowed() is False

    def test_tty_prompts(self, monkeypatch):
        class _Stdin:
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _Stdin())
        assert ob.prompt_allowed() is True


# ---------------------------------------------------------------------------
# Save tiers + secrets discipline
# ---------------------------------------------------------------------------


class TestSaveTiers:
    def test_router_saves_key_base_global_model_global(self):
        written = ob.save_credentials(
            "openai",
            "z-ai/glm-5.3-free",
            "sk-r",
            "https://api.tokenrouter.com/v1",
            "global",
        )
        assert written == {
            "api_key": "global",
            "base_url": "global",
            "provider": "global",
            "model": "global",
        }
        eff = vexconfig.effective_settings()
        assert eff["api_key"] == "sk-r"
        assert eff["base_url"] == "https://api.tokenrouter.com/v1"
        assert eff["model"] == "z-ai/glm-5.3-free"

    def test_model_tier_project_keeps_secrets_global(self, tmp_path):
        ob.save_credentials(
            "openai", "m-proj", "sk-r", "https://r.example/v1", "project"
        )
        proj = Path(os.environ["VEX_PROJECT_DIR"]) / "settings.toml"
        glob = vexconfig.global_settings_path()
        assert "api_key" not in proj.read_text(encoding="utf-8")
        assert "api_key" in glob.read_text(encoding="utf-8")
        assert vexconfig.load_vex_config(proj)["model"] == "m-proj"

    def test_official_save_clears_stale_base(self):
        ob.save_credentials(
            "openai", "old", "sk-r", "https://router.example/v1", "global"
        )
        ob.save_credentials("openai", "gpt-4o-mini", "sk-new", None, "global")
        data = vexconfig.load_vex_config(vexconfig.global_settings_path())
        assert "base_url" not in data and "api_base" not in data
        assert data["model"] == "gpt-4o-mini"

    def test_project_tier_api_key_refused(self):
        with pytest.raises(ValueError, match="refusing to store api_key"):
            vexconfig.set_tier_key("project", "api_key", "sk-nope")

    def test_config_set_project_api_key_exits_2(self, capsys):
        rc = m.main(["config", "set", "api_key", "sk-nope", "--tier", "project"])
        assert rc == 2

    def test_settings_file_chmod(self):
        p, _ = vexconfig.set_tier_key("global", "model", "m")
        if os.name == "nt":
            pytest.skip("POSIX-only permission check")
        assert (p.stat().st_mode & 0o777) == 0o600


class TestMasking:
    def test_mask_secret_shapes(self):
        assert vexconfig.mask_secret("sk-abcdefgh12345678").endswith("(set)")
        assert vexconfig.mask_secret("short") == "***"

    def test_config_get_masks_key(self, capsys):
        vexconfig.set_tier_key("global", "api_key", "sk-abcdefgh12345678")
        rc = m.main(["config", "get", "api_key"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "sk-abcdefgh12345678" not in out
        assert "(set)" in out

    def test_config_list_shows_source(self, capsys):
        vexconfig.set_tier_key("global", "model", "m-list")
        rc = m.main(["config", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "m-list" in out and "(global)" in out


# ---------------------------------------------------------------------------
# Wizard flows
# ---------------------------------------------------------------------------


class TestWizard:
    def test_custom_happy_path_saves(self):
        seen = {}

        def _test(provider, model, key, base):
            seen.update(
                {"provider": provider, "model": model, "key": key, "base": base}
            )
            return True, ""

        ok = ob.run_repl_wizard(
            input_fn=_feeder(["7", "https://openrouter.ai/api/v1", "my-model"]),
            getpass_fn=_feeder(["sk-good"]),
            test_fn=_test,
            print_fn=lambda s: None,
        )
        assert ok is True
        assert seen == {
            "provider": "openai",
            "model": "my-model",
            "key": "sk-good",
            "base": "https://openrouter.ai/api/v1",
        }
        eff = vexconfig.effective_settings()
        assert eff["model"] == "my-model"
        assert eff["api_key"] == "sk-good"
        assert eff["base_url"] == "https://openrouter.ai/api/v1"
        assert ob.needs_onboarding() is False

    def test_wrong_key_retries_and_saves_nothing(self):
        calls = []

        def _test(provider, model, key, base):
            calls.append(key)
            return False, "auth rejected (401)"

        ok = ob.run_repl_wizard(
            input_fn=_feeder(["7", "https://openrouter.ai/api/v1", "my-model", "n"]),
            getpass_fn=_feeder(["sk-bad"]),
            test_fn=_test,
            print_fn=lambda s: None,
        )
        assert ok is False
        assert calls == ["sk-bad"]  # tested once, then declined retry
        assert "api_key" not in vexconfig.effective_settings()
        assert ob.needs_onboarding() is True

    def test_retry_then_fix_key_saves(self):
        results = [(False, "auth rejected"), (True, "")]
        answers_calls = []

        def _test(provider, model, key, base):
            ok, err = results.pop(0)
            answers_calls.append(key)
            return ok, err

        ok = ob.run_repl_wizard(
            input_fn=_feeder(
                ["5", "", "", "y", ""]  # tokenrouter defaults, retry yes
            ),
            getpass_fn=_feeder(["sk-bad", "sk-fixed"]),
            test_fn=_test,
            print_fn=lambda s: None,
        )
        assert ok is True
        assert answers_calls == ["sk-bad", "sk-fixed"]
        assert vexconfig.effective_settings()["api_key"] == "sk-fixed"

    def test_skip_saves_nothing(self):
        ok = ob.run_repl_wizard(
            input_fn=_feeder(["/skip"]),
            getpass_fn=_feeder([]),
            test_fn=lambda *a: (_ for _ in ()).throw(AssertionError("no test on skip")),
            print_fn=lambda s: None,
        )
        assert ok is False
        assert vexconfig.effective_settings() == {}

    def test_official_uses_defaults(self):
        seen = {}

        def _test(provider, model, key, base):
            seen.update(
                {"provider": provider, "model": model, "key": key, "base": base}
            )
            return True, ""

        ok = ob.run_repl_wizard(
            input_fn=_feeder(["1", "", ""]),  # openai, default base+model
            getpass_fn=_feeder(["sk-openai"]),
            test_fn=_test,
            print_fn=lambda s: None,
        )
        assert ok is True
        assert seen == {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "key": "sk-openai",
            "base": None,
        }
        data = vexconfig.load_vex_config(vexconfig.global_settings_path())
        assert "base_url" not in data

    def test_ollama_allows_empty_key(self):
        def _test(provider, model, key, base):
            assert key == ""
            assert base == "http://localhost:11434/v1"
            return True, ""

        ok = ob.run_repl_wizard(
            input_fn=_feeder(["6", "", ""]),
            getpass_fn=_feeder([""]),
            test_fn=_test,
            print_fn=lambda s: None,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Flag-command gates: exit 4, never prompt
# ---------------------------------------------------------------------------


class TestGates:
    def test_missing_exit_is_4(self):
        assert ob.missing_credentials_exit({}) == 4

    def test_usable_config_proceeds(self):
        assert ob.missing_credentials_exit({"model": "m", "api_key": "k"}) is None

    def test_offline_models_exempt(self, monkeypatch):
        assert ob.missing_credentials_exit({"use_fake_harness": True}) is None
        assert ob.missing_credentials_exit({"mock_script": {"x": 1}}) is None
        monkeypatch.setenv("HARNESS_SCRIPTED_MODEL", "spec.json")
        assert ob.missing_credentials_exit({}) is None

    def test_injected_model_exempt(self):
        from harness import deps as _hdeps

        old = _hdeps._call_model_override
        _hdeps.set_call_model(lambda *a, **k: "hi")
        try:
            assert ob.missing_credentials_exit({}) is None
        finally:
            _hdeps.set_call_model(old)

    def test_fix_without_creds_exits_4(self, tmp_path, monkeypatch, capsys):
        from cli import deps

        def _explode(*a, **k):
            raise AssertionError("run_task must not run without creds")

        monkeypatch.setattr(deps, "get_run_task", lambda: _explode)
        rc = m.main(["fix", "--repo", str(tmp_path), "--issue", "something is broken"])
        assert rc == 4
        assert "vex login" in capsys.readouterr().err

    def test_fix_json_without_creds_exits_4_with_json(
        self, tmp_path, monkeypatch, capsys
    ):
        from cli import deps

        monkeypatch.setattr(
            deps,
            "get_run_task",
            lambda: (_ for _ in ()).throw(
                AssertionError("run_task must not run without creds")
            ),
        )
        rc = m.main(
            [
                "fix",
                "--repo",
                str(tmp_path),
                "--issue",
                "something is broken",
                "--json",
            ]
        )
        assert rc == 4
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["exit_code"] == 4 and doc["exit_reason"] == "model_error"

    def test_login_needs_tty(self, monkeypatch, capsys):
        class _Stdin:
            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdin", _Stdin())
        rc = m.main(["login"])
        assert rc == 2

    def test_logout_strips_key(self, capsys):
        vexconfig.set_tier_key("global", "api_key", "sk-gone")
        vexconfig.set_tier_key("global", "model", "m-keep")
        assert m.main(["logout"]) == 0
        assert "api_key" not in vexconfig.effective_settings()
        assert vexconfig.effective_settings()["model"] == "m-keep"
        assert m.main(["logout"]) == 1  # nothing left: honest nonzero


# ---------------------------------------------------------------------------
# TUI modal (headless Pilot drives — no terminal needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def _tui_hooks_clean():
    """Reset cli.interactive's embedded-UI hooks around TUI mounts (the
    app sets them on mount; owner-tagged unmount clears, but a failed
    drive must not leak them into neighboring suites)."""
    import cli.interactive as iv

    old = (iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY)
    yield
    iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY = old
    iv._clear_live_run()


def _tui_app(tmp_path, **kw):
    import cli.tui as t

    args = {
        "repo": tmp_path,
        "log_root": tmp_path / "logs",
        "state": {"repo": str(tmp_path), "file_config": {}},
        "file_config": {},
    }
    args.update(kw)
    return t.VexApp(**args)


async def _wait_for(pilot, cond, tries=100):
    for _ in range(tries):
        await pilot.pause()
        if cond():
            return True
    return False


class TestTuiOnboard:
    @pytest.mark.anyio
    async def test_no_modal_by_default(self, tmp_path, _tui_hooks_clean):
        import cli.tui as t

        app = _tui_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not isinstance(app.screen, t._OnboardScreen)

    @pytest.mark.anyio
    async def test_modal_offers_when_no_creds(self, tmp_path, _tui_hooks_clean):
        import cli.tui as t

        app = _tui_app(tmp_path, onboard_prompt=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, t._OnboardScreen)

    @pytest.mark.anyio
    async def test_no_modal_when_creds_set(self, tmp_path, _tui_hooks_clean):
        ob.save_credentials("openai", "gpt-4o-mini", "sk-x", None, "global")
        app = _tui_app(tmp_path, onboard_prompt=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.screen_stack) == 1

    @pytest.mark.anyio
    async def test_escape_skips_and_saves_nothing(self, tmp_path, _tui_hooks_clean):
        import cli.tui as t

        app = _tui_app(tmp_path, onboard_prompt=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, t._OnboardScreen)
            await pilot.press("escape")
            ok = await _wait_for(pilot, lambda: len(app.screen_stack) == 1)
            assert ok
            assert "api_key" not in vexconfig.effective_settings()

    @pytest.mark.anyio
    async def test_full_flow_saves(self, tmp_path, _tui_hooks_clean, monkeypatch):
        from textual.widgets import Input

        import cli.tui as t

        monkeypatch.setattr(ob, "test_credentials", lambda *a, **k: (True, ""))
        app = _tui_app(tmp_path, onboard_prompt=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, t._OnboardScreen)
            # pick: first option (OpenAI official) + Enter
            await pilot.press("enter")
            assert await _wait_for(pilot, lambda: modal._step == "base")
            # base: official default empty + Enter
            await pilot.press("enter")
            assert await _wait_for(pilot, lambda: modal._step == "model")
            # model: prefilled first suggestion + Enter
            assert modal.query_one("#onboard-input", Input).value == "gpt-4o-mini"
            await pilot.press("enter")
            assert await _wait_for(pilot, lambda: modal._step == "key")
            # key: masked input, type + Enter -> test (mocked) -> save
            modal.query_one("#onboard-input", Input).value = "sk-tui"
            await pilot.press("enter")
            assert await _wait_for(pilot, lambda: len(app.screen_stack) == 1, tries=200)
            eff = vexconfig.effective_settings()
            assert eff["model"] == "gpt-4o-mini"
            assert eff["api_key"] == "sk-tui"
            assert ob.needs_onboarding() is False
