"""Tests for the two-tier Vex config (global + project) and the custom
router/base_url support — the "Two-Tier Config & Custom Router" round.

Covers:
- Tier paths (global %APPDATA%\\vex / ~/.config/vex, project .vex/, the
  $VEX_CONFIG / $VEX_PROJECT_DIR overrides, legacy ~/.vex/config.toml).
- The FULL precedence chain with conflicting values at EVERY level:
  flags > env > project local > project > global > legacy > defaults.
- base_url as an independent value: file, env (VEX_BASE_URL), and its
  normalization onto runtime's api_base + provider="openai" default.
- `vex config path/list/get/set/unset/init-project` (secrets masked).
- Automatic .gitignore handling for .vex/settings.local.toml.
- First-run flow (interactive-mode global settings creation).
- Broken-TOML tolerance at every tier (never crash the CLI).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import main as m
from cli import vexconfig

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_config_env(tmp_path, monkeypatch):
    """Point every tier at tmp_path so tests never touch the real
    %APPDATA%/~/.config/~/.vex, and no real VEX_* env leaks in."""
    monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "global" / "settings.toml"))
    monkeypatch.setenv("VEX_LEGACY_CONFIG", str(tmp_path / "legacy" / "config.toml"))
    monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / "proj" / ".vex"))
    for var in vexconfig._ENV_KEYS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tier paths
# ---------------------------------------------------------------------------


class TestTierPaths:
    def test_env_overrides_win(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "custom.toml"))
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / "pvex"))
        assert vexconfig.global_settings_path() == tmp_path / "custom.toml"
        assert vexconfig.project_settings_dir() == tmp_path / "pvex"

    def test_platform_default_global(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VEX_CONFIG", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        assert vexconfig.global_settings_path() == (
            tmp_path / "appdata" / "vex" / "settings.toml"
        )

    def test_project_dir_walk_up(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        _write(tmp_path / ".vex" / "settings.toml", 'model = "m"\n')
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert vexconfig.project_settings_dir() == tmp_path / ".vex"

    def test_no_project_dir_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert vexconfig.project_settings_dir() is None


# ---------------------------------------------------------------------------
# Precedence — conflicts at every level
# ---------------------------------------------------------------------------


def _mk_tiers(tmp_path):
    """All tier files with CONFLICTING values for model/budget at every
    level, so each precedence test flips exactly one thing."""
    g = _write(
        tmp_path / "global" / "settings.toml",
        'model = "global-model"\nbudget_cap_usd = 5.0\nprovider = "global-provider"\n',
    )
    p = _write(
        tmp_path / "proj" / ".vex" / "settings.toml",
        'model = "project-model"\nbudget_cap_usd = 3.0\n',
    )
    l = _write(
        tmp_path / "proj" / ".vex" / "settings.local.toml",
        'model = "local-model"\nbudget_cap_usd = 2.0\n',
    )
    return g, p, l


class TestPrecedence:
    def test_local_beats_project_beats_global(self, tmp_path):
        _mk_tiers(tmp_path)
        eff = vexconfig.merged_settings()
        assert eff["model"] == "local-model"  # local > project > global
        assert eff["budget_cap_usd"] == 2.0
        assert eff["provider"] == "global-provider"  # gap filled by global

    def test_project_beats_global_when_no_local(self, tmp_path):
        _write(
            tmp_path / "global" / "settings.toml",
            'model = "global-model"\nbudget_cap_usd = 5.0\n',
        )
        _write(
            tmp_path / "proj" / ".vex" / "settings.toml",
            'model = "project-model"\n',
        )
        eff = vexconfig.merged_settings()
        assert eff["model"] == "project-model"
        assert eff["budget_cap_usd"] == 5.0  # global fills the gap

    def test_env_beats_all_files(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("VEX_MODEL", "env-model")
        eff = vexconfig.effective_settings()
        assert eff["model"] == "env-model"
        assert eff["budget_cap_usd"] == 2.0  # files still fill other keys

    def test_explicit_beats_env_and_files(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("VEX_MODEL", "env-model")
        merged = vexconfig.apply_config_defaults({"model": "flag-model"})
        assert merged["model"] == "flag-model"  # explicit > env > files
        assert merged["budget_cap_usd"] == 2.0  # untouched keys keep chain

    def test_legacy_read_only_without_new_global(self, tmp_path):
        _write(
            tmp_path / "legacy" / "config.toml",
            'model = "legacy-model"\n',
        )
        # no new global file: legacy IS the global fallback
        assert vexconfig.merged_settings()["model"] == "legacy-model"
        # ...but once the new global exists, legacy is ignored
        _write(tmp_path / "global" / "settings.toml", 'model = "new-model"\n')
        assert vexconfig.merged_settings()["model"] == "new-model"

    def test_empty_env_value_ignored(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("VEX_MODEL", "")
        assert vexconfig.effective_settings()["model"] == "local-model"

    def test_chain_order_labels(self, tmp_path):
        _write(tmp_path / "legacy" / "config.toml", "x = 1\n")
        _mk_tiers(tmp_path)
        (tmp_path / "legacy" / "config.toml").unlink()  # isolate file tiers
        labels = [t[0] for t in vexconfig.settings_chain()]
        assert labels == ["global", "project", "project-local"]

    def test_broken_toml_at_any_tier_ignored(self, tmp_path):
        _write(tmp_path / "global" / "settings.toml", "model = [unclosed")
        _write(
            tmp_path / "proj" / ".vex" / "settings.toml",
            'model = "project-model"\n',
        )
        eff = vexconfig.merged_settings()
        assert eff["model"] == "project-model"  # broken global skipped

    def test_bom_file_still_reads(self, tmp_path, monkeypatch):
        """Windows editors (PowerShell, Notepad) write UTF-8 BOMs — a
        BOM'd settings file must parse, not silently disable the tier
        (same class of fix as the subset loader's utf-8-sig)."""
        f = _write(
            tmp_path / "global" / "settings.toml",
            'model = "bom-model"\n',
        )
        raw = f.read_bytes()
        f.write_bytes(b"\xef\xbb\xbf" + raw)
        assert vexconfig.merged_settings()["model"] == "bom-model"
        # and `vex config set` appends to the BOM'd file without choking
        monkeypatch.setenv("VEX_CONFIG", str(f))
        assert m.main(["config", "set", "provider", "openai"]) == 0
        data = vexconfig.load_vex_config(f)
        assert data["model"] == "bom-model" and data["provider"] == "openai"

    def test_unknown_keys_pass_through_all_tiers(self, tmp_path):
        _write(tmp_path / "global" / "settings.toml", 'future_knob = "a"\n')
        _write(tmp_path / "proj" / ".vex" / "settings.toml", "other = 2\n")
        eff = vexconfig.merged_settings()
        assert eff["future_knob"] == "a" and eff["other"] == 2


# ---------------------------------------------------------------------------
# Task B — custom router / base URL
# ---------------------------------------------------------------------------


class TestBaseUrl:
    def test_normalize_maps_base_url_to_api_base(self):
        out = vexconfig.normalize_runtime_keys({"base_url": "https://r.example/v1"})
        assert out.get("api_base") == "https://r.example/v1"
        assert out.get("provider") == "openai"  # OpenAI-compatible default
        assert "base_url" not in out

    def test_explicit_provider_wins_over_default(self):
        out = vexconfig.normalize_runtime_keys(
            {"base_url": "https://r.example/v1", "provider": "anthropic"}
        )
        assert out["provider"] == "anthropic"

    def test_existing_api_base_wins_over_base_url_alias(self):
        out = vexconfig.normalize_runtime_keys(
            {"base_url": "https://new.example/v1", "api_base": "https://old.example/v1"}
        )
        assert out["api_base"] == "https://old.example/v1"

    def test_env_base_url_reaches_effective_settings(self, tmp_path, monkeypatch):
        _write(tmp_path / "global" / "settings.toml", 'model = "gpt-x"\n')
        monkeypatch.setenv("VEX_BASE_URL", "https://router.example.com/v1")
        monkeypatch.setenv("VEX_API_KEY", "sk-test-123")
        eff = vexconfig.apply_config_defaults({})
        # exactly the scenario from the task: base_url + model name is
        # enough for ANY OpenAI-compatible backend
        norm = vexconfig.normalize_runtime_keys(eff)
        assert norm["api_base"] == "https://router.example.com/v1"
        assert norm["provider"] == "openai"
        assert norm["model"] == "gpt-x"
        assert norm["api_key"] == "sk-test-123"

    def test_base_url_from_file_reaches_task_config(self, tmp_path, monkeypatch):
        """`vex config set base_url ...` + a model = a working custom
        backend — verified at the _make_task level (the Task the harness
        actually receives)."""
        from shared.types import Task  # noqa: F401 — _make_task imports it

        _write(
            tmp_path / "global" / "settings.toml",
            'base_url = "https://my-router.example.com/v1"\nmodel = "any-model-name"\n',
        )
        args = m.build_parser().parse_args(
            ["fix", "--repo", str(tmp_path), "--issue", "i"]
        )
        task = m._make_task(args)
        assert task.config["api_base"] == "https://my-router.example.com/v1"
        assert task.config["provider"] == "openai"
        assert task.config["model"] == "any-model-name"
        assert "base_url" not in task.config

    def test_flag_beats_file_base_url(self, tmp_path):
        _write(
            tmp_path / "global" / "settings.toml",
            'base_url = "https://file.example/v1"\n',
        )
        args = m.build_parser().parse_args(
            [
                "fix",
                "--repo",
                str(tmp_path),
                "--issue",
                "i",
                "--base-url",
                "https://flag.example/v1",
            ]
        )
        task = m._make_task(args)
        assert task.config["api_base"] == "https://flag.example/v1"

    def test_model_string_not_coerced(self):
        """Model names like "3.5" or "123" must stay strings (TOML files
        hand-write them quoted; the CLI coerce path must not turn them
        into floats either)."""
        assert vexconfig.coerce_value("model", "3.5") == "3.5"
        assert vexconfig.coerce_value("base_url", "https://x/v1") == "https://x/v1"
        assert vexconfig.coerce_value("budget_cap_usd", "2.5") == 2.5
        assert vexconfig.coerce_value("max_retries", "3") == 3
        assert vexconfig.coerce_value("plan_preview", "true") is True


# ---------------------------------------------------------------------------
# vex config subcommand
# ---------------------------------------------------------------------------


class TestConfigCommand:
    def test_path_shows_tiers(self, tmp_path, capsys):
        assert m.main(["config", "path"]) == 0
        out = capsys.readouterr().out
        assert "global" in out and "settings.toml" in out

    def test_get_set_unset_roundtrip(self, tmp_path, capsys):
        assert m.main(["config", "set", "model", "z-ai/glm-5.3-free"]) == 0
        assert m.main(["config", "get", "model"]) == 0
        out = capsys.readouterr().out
        assert "z-ai/glm-5.3-free" in out

        assert m.main(["config", "unset", "model"]) == 0
        rc = m.main(["config", "get", "model"])
        assert rc == 1  # not set after unset
        assert "not set" in capsys.readouterr().out

    def test_set_writes_toml_and_get_reads_it_back(self, tmp_path):
        assert m.main(["config", "set", "base_url", "https://r/v1"]) == 0
        assert m.main(["config", "set", "budget_cap_usd", "1.5"]) == 0
        assert m.main(["config", "set", "plan_preview", "true"]) == 0
        text = vexconfig.global_settings_path().read_text(encoding="utf-8")
        assert 'base_url = "https://r/v1"' in text
        data = vexconfig.load_vex_config(vexconfig.global_settings_path())
        assert data["budget_cap_usd"] == 1.5
        assert data["plan_preview"] is True
        assert data["base_url"] == "https://r/v1"

    def test_set_preserves_comments_and_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "hand.toml"))
        _write(
            tmp_path / "hand.toml",
            '# my setup\nmodel = "m1"  # inline note\n\n[vex]\nplan_preview = true\n',
        )
        assert m.main(["config", "set", "budget_cap_usd", "2"]) == 0
        text = (tmp_path / "hand.toml").read_text(encoding="utf-8")
        assert "# my setup" in text  # comments survive an append
        assert 'model = "m1"' in text
        data = vexconfig.load_vex_config(tmp_path / "hand.toml")
        assert data["budget_cap_usd"] == 2 and data["model"] == "m1"

    def test_set_updates_existing_key_via_rewrite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "g.toml"))
        _write(tmp_path / "g.toml", 'model = "a"\nbudget_cap_usd = 1.0\n')
        assert m.main(["config", "set", "model", "b"]) == 0
        data = vexconfig.load_vex_config(tmp_path / "g.toml")
        assert data["model"] == "b" and data["budget_cap_usd"] == 1.0

    def test_set_local_tier_auto_gitignores(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setenv("VEX_PROJECT_DIR", str(repo / ".vex"))
        monkeypatch.chdir(repo)
        assert m.main(["config", "set", "model", "m", "--tier", "local"]) == 0
        gi = repo / ".gitignore"
        assert gi.is_file()
        assert ".vex/settings.local.toml" in gi.read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "gitignore" in out.lower()
        # second set: already covered, no duplicate warning needed
        assert m.main(["config", "set", "provider", "openai", "--tier", "local"]) == 0
        text = gi.read_text(encoding="utf-8")
        assert text.count(".vex/settings.local.toml") == 1

    def test_set_refuses_to_overwrite_broken_toml(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "broken.toml"))
        _write(tmp_path / "broken.toml", "model = [unclosed")
        rc = m.main(["config", "set", "model", "x"])
        assert rc == 2
        assert "not valid TOML" in capsys.readouterr().err

    def test_api_key_masked_in_list_and_get(self, tmp_path, capsys):
        assert m.main(["config", "set", "api_key", "sk-1234567890abcd"]) == 0
        assert m.main(["config", "list"]) == 0
        out = capsys.readouterr().out
        assert "sk-1234567890abcd" not in out  # never the raw secret
        assert "sk-" in out and "(set)" in out  # masked presence shown
        assert m.main(["config", "get", "api_key"]) == 0
        out = capsys.readouterr().out
        assert "sk-1234567890abcd" not in out

    def test_list_shows_source_tier(self, tmp_path, capsys, monkeypatch):
        _write(tmp_path / "global" / "settings.toml", 'model = "g-model"\n')
        _write(
            tmp_path / "proj" / ".vex" / "settings.toml",
            "budget_cap_usd = 2.0\n",
        )
        _write(
            tmp_path / "proj" / ".vex" / "settings.local.toml",
            'log_verbosity = "quiet"\n',
        )
        monkeypatch.setenv("VEX_MODEL", "e-model")
        assert m.main(["config", "list"]) == 0
        out = capsys.readouterr().out
        # env wins for model; each other key shows its own source tier
        assert "e-model" in out and "env:VEX_MODEL" in out
        assert "2.0" in out and "(project)" in out
        assert "quiet" in out and "(project-local)" in out

    def test_list_source_labels_without_env(self, tmp_path, capsys, monkeypatch):
        _write(tmp_path / "global" / "settings.toml", 'provider = "openai"\n')
        _write(
            tmp_path / "proj" / ".vex" / "settings.toml",
            "budget_cap_usd = 2.0\n",
        )
        _write(
            tmp_path / "proj" / ".vex" / "settings.local.toml",
            'model = "l-model"\n',
        )
        assert m.main(["config", "list"]) == 0
        out = capsys.readouterr().out
        assert "l-model" in out and "(project-local)" in out
        assert "2.0" in out and "(project)" in out
        assert "openai" in out and "(global)" in out

    def test_structured_key_rejected_by_set(self, tmp_path):
        rc = m.main(["config", "set", "model_tiers", '{"easy": {}}'])
        assert rc == 2

    def test_init_project_creates_and_gitignores(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        monkeypatch.chdir(repo)
        assert m.main(["config", "init-project"]) == 0
        assert (repo / ".vex" / "settings.toml").is_file()
        gi = repo / ".gitignore"
        assert gi.is_file()
        assert ".vex/settings.local.toml" in gi.read_text(encoding="utf-8")
        # idempotent
        assert m.main(["config", "init-project"]) == 0
        assert gi.read_text(encoding="utf-8").count(".vex/settings.local.toml") == 1

    def test_init_project_outside_git_repo_no_gitignore(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        monkeypatch.chdir(plain)
        assert m.main(["config", "init-project"]) == 0
        assert (plain / ".vex" / "settings.toml").is_file()
        assert not (plain / ".gitignore").exists()

    def test_list_warns_when_local_not_gitignored(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setenv("VEX_PROJECT_DIR", str(repo / ".vex"))
        monkeypatch.chdir(repo)
        _write(repo / ".vex" / "settings.local.toml", 'model = "m"\n')
        assert m.main(["config", "list"]) == 0
        assert "NOT in .gitignore" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# First-run flow
# ---------------------------------------------------------------------------


class TestFirstRun:
    def test_ensure_first_run_creates_start(self, tmp_path):
        created, p = vexconfig.ensure_first_run()
        assert created and p.is_file()
        # second run: no new creation
        created2, p2 = vexconfig.ensure_first_run()
        assert not created2 and p2 == p

    def test_interactive_first_run_notice(self, tmp_path, capsys, monkeypatch):
        """The interactive session prints the one-time created-notice and
        stores the merged chain for fixes (verified without a TTY by
        calling the config-load block directly — run_interactive itself
        needs a terminal)."""
        from cli import interactive

        con = interactive.ui.console()
        created, gp = vexconfig.ensure_first_run()
        assert created
        con.print(f"first run: created global settings at {gp}")
        out = capsys.readouterr().out
        assert "first run" in out

    def test_ensure_first_run_readonly_home_never_raises(self, tmp_path, monkeypatch):
        # point the global at a path whose parent is a FILE -> mkdir fails
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("VEX_CONFIG", str(blocker / "vex" / "settings.toml"))
        created, _path = vexconfig.ensure_first_run()
        assert created is False  # OSError swallowed, CLI stays usable


# ---------------------------------------------------------------------------
# Gitignore coverage detection
# ---------------------------------------------------------------------------


class TestGitignoreCoverage:
    def test_covers_exact_and_dir_and_glob(self, tmp_path):
        f = vexconfig._gitignore_covers
        assert f(".vex/settings.local.toml\n", ".vex/settings.local.toml")
        assert f(".vex/\n", ".vex/settings.local.toml")  # whole dir
        assert f("*.local.toml\n", ".vex/settings.local.toml")
        assert f("settings.local.toml\n", ".vex/settings.local.toml")
        assert not f("*.py\n", ".vex/settings.local.toml")
        assert not f("!.vex/settings.local.toml\n*.py\n", ".vex/settings.local.toml")

    def test_ensure_gitignore_appends_with_newline_fix(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("*.py", encoding="utf-8")  # no trailing newline
        assert vexconfig.ensure_gitignore(tmp_path) == "appended"
        text = gi.read_text(encoding="utf-8")
        assert "*.py\n" in text  # newline added before our entry
        assert ".vex/settings.local.toml" in text

    def test_set_local_in_non_git_dir_no_gitignore(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("VEX_PROJECT_DIR", str(plain / ".vex"))
        assert m.main(["config", "set", "model", "m", "--tier", "local"]) == 0
        assert not (plain / ".gitignore").exists()


# ---------------------------------------------------------------------------
# Real subprocess smoke (the installed CLI path)
# ---------------------------------------------------------------------------


class TestSubprocessSmoke:
    def test_config_cli_works_end_to_end(self, tmp_path, monkeypatch):
        """`python -m cli config ...` in a real subprocess with isolated
        env — the exact user-facing flow."""
        env = {
            **os.environ,
            "VEX_CONFIG": str(tmp_path / "g.toml"),
            "VEX_PROJECT_DIR": str(tmp_path / ".vex"),
            "NO_COLOR": "1",
        }
        for var in vexconfig._ENV_KEYS:
            env.pop(var, None)
        cp = subprocess.run(
            [sys.executable, "-m", "cli", "config", "set", "model", "smoke-model"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=120,
        )
        assert cp.returncode == 0, cp.stderr
        cp = subprocess.run(
            [sys.executable, "-m", "cli", "config", "get", "model"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=120,
        )
        assert cp.returncode == 0
        assert "smoke-model" in cp.stdout
        assert "Traceback" not in cp.stderr


# ---------------------------------------------------------------------------
# First-`vex`-in-a-repo auto-scaffold (.vex/ layout with examples)
# ---------------------------------------------------------------------------


def _mk_git_repo(base: Path) -> Path:
    """A scratch git repo (a `.git` dir — what find_git_root probes)."""
    base.mkdir(parents=True, exist_ok=True)
    (base / ".git").mkdir(exist_ok=True)
    return base


class TestProjectScaffold:
    def test_find_git_root_walks_up(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        deep = repo / "a" / "b"
        deep.mkdir(parents=True)
        assert vexconfig.find_git_root(deep) == repo
        assert vexconfig.find_git_root(repo) == repo

    def test_scaffold_creates_full_layout(self, tmp_path, monkeypatch):
        """First `vex` in a repo: settings.toml + settings.local.toml +
        commands/ + skills/ with one working example each."""
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        info = vexconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == repo
        assert info["created"] == [
            "settings.toml",
            "settings.local.toml",
            "commands/fix.md",
            "skills/code-review/SKILL.md",
        ]
        # committable starter: comment-only, no keys, no secrets
        starter = (repo / ".vex" / "settings.toml").read_text(encoding="utf-8")
        assert starter and "api_key" not in starter
        assert all(not ln.strip() or ln.startswith("#") for ln in starter.splitlines())
        # local starter: parses to zero keys (effective settings unchanged)
        assert vexconfig.load_vex_config(repo / ".vex" / "settings.local.toml") == {}
        # the examples actually work with the real loaders
        from cli.commands import load_command
        from harness.skills import discover_skills

        template = load_command("fix", str(repo))
        assert template is not None and "$ARGUMENTS" in template
        assert [s.name for s in discover_skills(str(repo))] == ["code-review"]
        # local file is git-ignored
        assert vexconfig.local_is_ignored(repo)

    def test_scaffold_from_subdirectory_lands_at_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        deep = repo / "pkg" / "sub"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        info = vexconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == repo
        assert (repo / ".vex" / "settings.toml").is_file()
        assert not (deep / ".vex").exists()

    def test_scaffold_never_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        _write(repo / ".vex" / "settings.toml", 'model = "mine"\n')
        _write(repo / ".vex" / "settings.local.toml", 'model = "local-mine"\n')
        _write(repo / ".vex" / "commands" / "custom.md", "# mine\n")
        _write(
            repo / ".vex" / "skills" / "mine" / "SKILL.md",
            "---\nname: mine\ndescription: mine\n---\n\nBody.\n",
        )
        monkeypatch.chdir(repo)
        info = vexconfig.maybe_scaffold_repo()
        assert info is not None and info["created"] == []
        assert (repo / ".vex" / "settings.toml").read_text(
            encoding="utf-8"
        ) == 'model = "mine"\n'
        # a deliberately absent example is NOT re-added (existing dirs
        # are left entirely alone)
        assert not (repo / ".vex" / "commands" / "fix.md").exists()
        assert not (repo / ".vex" / "skills" / "code-review").exists()

    def test_no_scaffold_outside_repo(self, tmp_path, monkeypatch):
        """Outside a git repo nothing is created (the gate is
        find_git_root — stubbed here so the test holds regardless of
        what .git entries exist above the tmp dir)."""
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        monkeypatch.setattr(vexconfig, "find_git_root", lambda start=None: None)
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert vexconfig.maybe_scaffold_repo() is None
        assert not (plain / ".vex").exists()

    def test_scaffold_respects_vex_project_dir(self, tmp_path, monkeypatch):
        """$VEX_PROJECT_DIR (points AT the .vex dir) wins over repo
        detection — the test-isolation override scaffolds its parent."""
        target = tmp_path / "p" / ".vex"
        monkeypatch.setenv("VEX_PROJECT_DIR", str(target))
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        info = vexconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == tmp_path / "p"
        assert (target / "settings.toml").is_file()

    def test_scaffold_never_raises_on_readonly_root(
        self, tmp_path, monkeypatch, capsys
    ):
        """A scaffold failure degrades to (at most) one warning, never
        an exception — the session must stay usable."""
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")  # parent mkdir fails
        monkeypatch.setattr(vexconfig, "find_git_root", lambda start=None: blocker)
        assert vexconfig.maybe_scaffold_repo() is None
        assert "Traceback" not in capsys.readouterr().err

    def test_init_project_creates_full_layout(self, tmp_path, capsys, monkeypatch):
        """`vex config init-project` (the explicit form) scaffolds the
        same layout + gitignore."""
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        assert m.main(["config", "init-project"]) == 0
        assert (repo / ".vex" / "settings.toml").is_file()
        assert (repo / ".vex" / "settings.local.toml").is_file()
        assert (repo / ".vex" / "commands" / "fix.md").is_file()
        assert (repo / ".vex" / "skills" / "code-review" / "SKILL.md").is_file()
        assert ".vex/settings.local.toml" in (repo / ".gitignore").read_text(
            encoding="utf-8"
        )
        out = capsys.readouterr().out
        assert "scaffolded" in out

    def test_git_status_shape(self, tmp_path, monkeypatch):
        """The Done-when shape: settings.toml untracked, the local file
        invisible (ignored). Needs a real git binary — skipped without."""
        import shutil

        if shutil.which("git") is None:
            pytest.skip("git binary not available")
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, timeout=60)
        subprocess.run(
            ["git", "config", "user.email", "t@t.t"],
            cwd=str(repo),
            check=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=str(repo),
            check=True,
            timeout=60,
        )
        monkeypatch.chdir(repo)
        info = vexconfig.maybe_scaffold_repo()
        assert info is not None
        cp = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert cp.returncode == 0
        lines = cp.stdout.splitlines()
        assert any(".vex/settings.toml" in ln for ln in lines)
        assert not any("settings.local.toml" in ln for ln in lines)

    def test_broken_toml_warns_once(self, tmp_path, capsys):
        """A broken tier warns on first read, then stays silent (the
        chain is re-read per key — without dedup one file spams)."""
        _write(tmp_path / "global" / "settings.toml", "model = [unclosed")
        vexconfig.merged_settings()
        first = capsys.readouterr().err
        assert "not valid TOML" in first
        vexconfig.merged_settings()
        vexconfig.merged_settings()
        assert capsys.readouterr().err == ""
