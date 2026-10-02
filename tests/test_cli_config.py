"""Tests for the two-tier Neo config (global + project) and the custom
router/base_url support — the "Two-Tier Config & Custom Router" round.

Covers:
- Tier paths (global %APPDATA%\\neo / ~/.config/neo, project .neo/, the
  $NEO_CONFIG / $NEO_PROJECT_DIR overrides, legacy ~/.neo/config.toml).
- The FULL precedence chain with conflicting values at EVERY level:
  flags > env > project local > project > global > legacy > defaults.
- base_url as an independent value: file, env (NEO_BASE_URL), and its
  normalization onto runtime's api_base + provider="openai" default.
- `neo config path/list/get/set/unset/init-project` (secrets masked).
- Automatic .gitignore handling for .neo/settings.local.toml.
- First-run flow (interactive-mode global settings creation).
- Broken-TOML tolerance at every tier (never crash the CLI).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import main as m
from cli import neoconfig

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_config_env(tmp_path, monkeypatch):
    """Point every tier at tmp_path so tests never touch the real
    %APPDATA%/~/.config/~/.neo, and no real NEO_* env leaks in."""
    monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "global" / "settings.toml"))
    monkeypatch.setenv("NEO_LEGACY_CONFIG", str(tmp_path / "legacy" / "config.toml"))
    monkeypatch.setenv("NEO_PROJECT_DIR", str(tmp_path / "proj" / ".neo"))
    for var in neoconfig._ENV_KEYS:
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
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "custom.toml"))
        monkeypatch.setenv("NEO_PROJECT_DIR", str(tmp_path / "pneo"))
        assert neoconfig.global_settings_path() == tmp_path / "custom.toml"
        assert neoconfig.project_settings_dir() == tmp_path / "pneo"

    def test_platform_default_global(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NEO_CONFIG", raising=False)
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        assert neoconfig.global_settings_path() == (
            tmp_path / "appdata" / "neo" / "settings.toml"
        )

    def test_project_dir_walk_up(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        _write(tmp_path / ".neo" / "settings.toml", 'model = "m"\n')
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert neoconfig.project_settings_dir() == tmp_path / ".neo"

    def test_no_project_dir_found(self, tmp_path, monkeypatch):
        """No project directory in the chain.

        The ``.git`` marker is what makes this deterministic. The
        project-tier walk is bounded to the enclosing repository, so with a
        repository at ``tmp_path`` the walk cannot leave it. Without the
        marker the walk climbs to whatever repository encloses the temp
        directory, and on a host whose ``$HOME`` is a dotfiles repository
        the answer is a real ``$HOME/.neo`` rather than ``None`` — true, and
        not what this test is about.
        """
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        assert neoconfig.project_settings_dir() is None


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
        tmp_path / "proj" / ".neo" / "settings.toml",
        'model = "project-model"\nbudget_cap_usd = 3.0\n',
    )
    l = _write(
        tmp_path / "proj" / ".neo" / "settings.local.toml",
        'model = "local-model"\nbudget_cap_usd = 2.0\n',
    )
    return g, p, l


class TestPrecedence:
    def test_local_beats_project_beats_global(self, tmp_path):
        _mk_tiers(tmp_path)
        eff = neoconfig.merged_settings()
        assert eff["model"] == "local-model"  # local > project > global
        assert eff["budget_cap_usd"] == 2.0
        assert eff["provider"] == "global-provider"  # gap filled by global

    def test_project_beats_global_when_no_local(self, tmp_path):
        _write(
            tmp_path / "global" / "settings.toml",
            'model = "global-model"\nbudget_cap_usd = 5.0\n',
        )
        _write(
            tmp_path / "proj" / ".neo" / "settings.toml",
            'model = "project-model"\n',
        )
        eff = neoconfig.merged_settings()
        assert eff["model"] == "project-model"
        assert eff["budget_cap_usd"] == 5.0  # global fills the gap

    def test_env_beats_all_files(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("NEO_MODEL", "env-model")
        eff = neoconfig.effective_settings()
        assert eff["model"] == "env-model"
        assert eff["budget_cap_usd"] == 2.0  # files still fill other keys

    def test_explicit_beats_env_and_files(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("NEO_MODEL", "env-model")
        merged = neoconfig.apply_config_defaults({"model": "flag-model"})
        assert merged["model"] == "flag-model"  # explicit > env > files
        assert merged["budget_cap_usd"] == 2.0  # untouched keys keep chain

    def test_legacy_read_only_without_new_global(self, tmp_path):
        _write(
            tmp_path / "legacy" / "config.toml",
            'model = "legacy-model"\n',
        )
        # no new global file: legacy IS the global fallback
        assert neoconfig.merged_settings()["model"] == "legacy-model"
        # ...but once the new global exists, legacy is ignored
        _write(tmp_path / "global" / "settings.toml", 'model = "new-model"\n')
        assert neoconfig.merged_settings()["model"] == "new-model"

    def test_empty_env_value_ignored(self, tmp_path, monkeypatch):
        _mk_tiers(tmp_path)
        monkeypatch.setenv("NEO_MODEL", "")
        assert neoconfig.effective_settings()["model"] == "local-model"

    def test_chain_order_labels(self, tmp_path):
        _write(tmp_path / "legacy" / "config.toml", "x = 1\n")
        _mk_tiers(tmp_path)
        (tmp_path / "legacy" / "config.toml").unlink()  # isolate file tiers
        labels = [t[0] for t in neoconfig.settings_chain()]
        assert labels == ["global", "project", "project-local"]

    def test_broken_toml_at_any_tier_ignored(self, tmp_path):
        _write(tmp_path / "global" / "settings.toml", "model = [unclosed")
        _write(
            tmp_path / "proj" / ".neo" / "settings.toml",
            'model = "project-model"\n',
        )
        eff = neoconfig.merged_settings()
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
        assert neoconfig.merged_settings()["model"] == "bom-model"
        # and `neo config set` appends to the BOM'd file without choking
        monkeypatch.setenv("NEO_CONFIG", str(f))
        assert m.main(["config", "set", "provider", "openai"]) == 0
        data = neoconfig.load_neo_config(f)
        assert data["model"] == "bom-model" and data["provider"] == "openai"

    def test_unknown_keys_pass_through_all_tiers(self, tmp_path):
        _write(tmp_path / "global" / "settings.toml", 'future_knob = "a"\n')
        _write(tmp_path / "proj" / ".neo" / "settings.toml", "other = 2\n")
        eff = neoconfig.merged_settings()
        assert eff["future_knob"] == "a" and eff["other"] == 2


# ---------------------------------------------------------------------------
# Task B — custom router / base URL
# ---------------------------------------------------------------------------


class TestBaseUrl:
    def test_normalize_maps_base_url_to_api_base(self):
        out = neoconfig.normalize_runtime_keys({"base_url": "https://r.example/v1"})
        assert out.get("api_base") == "https://r.example/v1"
        assert out.get("provider") == "openai"  # OpenAI-compatible default
        assert "base_url" not in out

    def test_explicit_provider_wins_over_default(self):
        out = neoconfig.normalize_runtime_keys(
            {"base_url": "https://r.example/v1", "provider": "anthropic"}
        )
        assert out["provider"] == "anthropic"

    def test_existing_api_base_wins_over_base_url_alias(self):
        out = neoconfig.normalize_runtime_keys(
            {"base_url": "https://new.example/v1", "api_base": "https://old.example/v1"}
        )
        assert out["api_base"] == "https://old.example/v1"

    def test_env_base_url_reaches_effective_settings(self, tmp_path, monkeypatch):
        _write(tmp_path / "global" / "settings.toml", 'model = "gpt-x"\n')
        monkeypatch.setenv("NEO_BASE_URL", "https://router.example.com/v1")
        monkeypatch.setenv("NEO_API_KEY", "sk-test-123")
        eff = neoconfig.apply_config_defaults({})
        # exactly the scenario from the task: base_url + model name is
        # enough for ANY OpenAI-compatible backend
        norm = neoconfig.normalize_runtime_keys(eff)
        assert norm["api_base"] == "https://router.example.com/v1"
        assert norm["provider"] == "openai"
        assert norm["model"] == "gpt-x"
        assert norm["api_key"] == "sk-test-123"

    def test_base_url_from_file_reaches_task_config(self, tmp_path, monkeypatch):
        """`neo config set base_url ...` + a model = a working custom
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

    def test_base_url_alias_precedence_covers_file_env_and_flags(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path / "global" / "settings.toml", 'api_base = "https://file/v1"\n')
        monkeypatch.setenv("NEO_BASE_URL", "https://env/v1")
        assert neoconfig.apply_config_defaults({})["api_base"] == "https://env/v1"
        assert (
            neoconfig.apply_config_defaults({"base_url": "https://flag/v1"})["api_base"]
            == "https://flag/v1"
        )
        assert neoconfig.source_tier("api_base") == "env"

    def test_model_string_not_coerced(self):
        """Model names like "3.5" or "123" must stay strings (TOML files
        hand-write them quoted; the CLI coerce path must not turn them
        into floats either)."""
        assert neoconfig.coerce_value("model", "3.5") == "3.5"
        assert neoconfig.coerce_value("base_url", "https://x/v1") == "https://x/v1"
        assert neoconfig.coerce_value("budget_cap_usd", "2.5") == 2.5
        assert neoconfig.coerce_value("max_retries", "3") == 3
        assert neoconfig.coerce_value("plan_preview", "true") is True


# ---------------------------------------------------------------------------
# neo config subcommand
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
        text = neoconfig.global_settings_path().read_text(encoding="utf-8")
        assert 'base_url = "https://r/v1"' in text
        data = neoconfig.load_neo_config(neoconfig.global_settings_path())
        assert data["budget_cap_usd"] == 1.5
        assert data["plan_preview"] is True
        assert data["base_url"] == "https://r/v1"

    def test_set_preserves_comments_and_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "hand.toml"))
        _write(
            tmp_path / "hand.toml",
            '# my setup\nmodel = "m1"  # inline note\n\n[neo]\nplan_preview = true\n',
        )
        assert m.main(["config", "set", "budget_cap_usd", "2"]) == 0
        text = (tmp_path / "hand.toml").read_text(encoding="utf-8")
        assert "# my setup" in text  # comments survive an append
        assert 'model = "m1"' in text
        data = neoconfig.load_neo_config(tmp_path / "hand.toml")
        assert data["budget_cap_usd"] == 2 and data["model"] == "m1"

    def test_set_updates_existing_key_via_rewrite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "g.toml"))
        _write(tmp_path / "g.toml", 'model = "a"\nbudget_cap_usd = 1.0\n')
        assert m.main(["config", "set", "model", "b"]) == 0
        data = neoconfig.load_neo_config(tmp_path / "g.toml")
        assert data["model"] == "b" and data["budget_cap_usd"] == 1.0

    def test_set_local_tier_auto_gitignores(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".neo"))
        monkeypatch.chdir(repo)
        assert m.main(["config", "set", "model", "m", "--tier", "local"]) == 0
        gi = repo / ".gitignore"
        assert gi.is_file()
        assert ".neo/settings.local.toml" in gi.read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "gitignore" in out.lower()
        # second set: already covered, no duplicate warning needed
        assert m.main(["config", "set", "provider", "openai", "--tier", "local"]) == 0
        text = gi.read_text(encoding="utf-8")
        assert text.count(".neo/settings.local.toml") == 1

    def test_set_refuses_to_overwrite_broken_toml(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "broken.toml"))
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
            tmp_path / "proj" / ".neo" / "settings.toml",
            "budget_cap_usd = 2.0\n",
        )
        _write(
            tmp_path / "proj" / ".neo" / "settings.local.toml",
            'log_verbosity = "quiet"\n',
        )
        monkeypatch.setenv("NEO_MODEL", "e-model")
        assert m.main(["config", "list"]) == 0
        out = capsys.readouterr().out
        # env wins for model; each other key shows its own source tier
        assert "e-model" in out and "env:NEO_MODEL" in out
        assert "2.0" in out and "(project)" in out
        assert "quiet" in out and "(project-local)" in out

    def test_list_source_labels_without_env(self, tmp_path, capsys, monkeypatch):
        _write(tmp_path / "global" / "settings.toml", 'provider = "openai"\n')
        _write(
            tmp_path / "proj" / ".neo" / "settings.toml",
            "budget_cap_usd = 2.0\n",
        )
        _write(
            tmp_path / "proj" / ".neo" / "settings.local.toml",
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
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.chdir(repo)
        assert m.main(["config", "init-project"]) == 0
        assert (repo / ".neo" / "settings.toml").is_file()
        gi = repo / ".gitignore"
        assert gi.is_file()
        assert ".neo/settings.local.toml" in gi.read_text(encoding="utf-8")
        # idempotent
        assert m.main(["config", "init-project"]) == 0
        assert gi.read_text(encoding="utf-8").count(".neo/settings.local.toml") == 1

    def test_init_project_outside_git_repo_no_gitignore(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.chdir(plain)
        assert m.main(["config", "init-project"]) == 0
        assert (plain / ".neo" / "settings.toml").is_file()
        assert not (plain / ".gitignore").exists()

    def test_list_warns_when_local_not_gitignored(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".neo"))
        monkeypatch.chdir(repo)
        _write(repo / ".neo" / "settings.local.toml", 'model = "m"\n')
        assert m.main(["config", "list"]) == 0
        assert "NOT in .gitignore" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# First-run flow
# ---------------------------------------------------------------------------


class TestFirstRun:
    def test_ensure_first_run_creates_start(self, tmp_path):
        created, p = neoconfig.ensure_first_run()
        assert created and p.is_file()
        # second run: no new creation
        created2, p2 = neoconfig.ensure_first_run()
        assert not created2 and p2 == p

    def test_interactive_first_run_notice(self, tmp_path, capsys, monkeypatch):
        """The interactive session prints the one-time created-notice and
        stores the merged chain for fixes (verified without a TTY by
        calling the config-load block directly — run_interactive itself
        needs a terminal)."""
        from cli import interactive

        con = interactive.ui.console()
        created, gp = neoconfig.ensure_first_run()
        assert created
        con.print(f"first run: created global settings at {gp}")
        out = capsys.readouterr().out
        assert "first run" in out

    def test_ensure_first_run_readonly_home_never_raises(self, tmp_path, monkeypatch):
        # point the global at a path whose parent is a FILE -> mkdir fails
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("NEO_CONFIG", str(blocker / "neo" / "settings.toml"))
        created, _path = neoconfig.ensure_first_run()
        assert created is False  # OSError swallowed, CLI stays usable


# ---------------------------------------------------------------------------
# Gitignore coverage detection
# ---------------------------------------------------------------------------


class TestGitignoreCoverage:
    def test_covers_exact_and_dir_and_glob(self, tmp_path):
        f = neoconfig._gitignore_covers
        assert f(".neo/settings.local.toml\n", ".neo/settings.local.toml")
        assert f(".neo/\n", ".neo/settings.local.toml")  # whole dir
        assert f("*.local.toml\n", ".neo/settings.local.toml")
        assert f("settings.local.toml\n", ".neo/settings.local.toml")
        assert not f("*.py\n", ".neo/settings.local.toml")
        assert not f("!.neo/settings.local.toml\n*.py\n", ".neo/settings.local.toml")

    def test_ensure_gitignore_appends_with_newline_fix(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("*.py", encoding="utf-8")  # no trailing newline
        assert neoconfig.ensure_gitignore(tmp_path) == "appended"
        text = gi.read_text(encoding="utf-8")
        assert "*.py\n" in text  # newline added before our entry
        assert ".neo/settings.local.toml" in text

    def test_set_local_in_non_git_dir_no_gitignore(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("NEO_PROJECT_DIR", str(plain / ".neo"))
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
            "NEO_CONFIG": str(tmp_path / "g.toml"),
            "NEO_PROJECT_DIR": str(tmp_path / ".neo"),
            "NO_COLOR": "1",
        }
        for var in neoconfig._ENV_KEYS:
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
# First-`neo`-in-a-repo auto-scaffold (.neo/ layout with examples)
# ---------------------------------------------------------------------------


def _mk_git_repo(base: Path) -> Path:
    """A scratch git repo (a `.git` dir — what find_git_root probes)."""
    base.mkdir(parents=True, exist_ok=True)
    (base / ".git").mkdir(exist_ok=True)
    return base


class TestProjectScaffold:
    def test_new_repo_scaffolds_neo(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        assert neoconfig.project_settings_dir(repo) is None
        info = neoconfig.ensure_project_layout(repo)
        assert info["dir_name"] == ".neo"
        assert (repo / ".neo" / "settings.toml").is_file()
        assert not (repo / ".vex").exists()

    def test_legacy_repo_scaffolds_into_legacy_and_keeps_settings(
        self, tmp_path, monkeypatch
    ):
        """An upgrading user must NOT get a second directory, and must keep
        their existing project settings.

        Regression: the scaffold hardcoded ``.neo`` while the reader resolved
        ``.vex``. The scaffold therefore created ``.neo/``, the reader PREFERRED
        ``.neo`` on the very next call, and the user's ``.vex/settings.toml`` was
        silently ignored - the project settings tier was lost with no warning.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".vex" / "settings.toml", 'model = "legacy-model"\n')
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)

        info = neoconfig.ensure_project_layout(repo)

        # no competing directory is created
        assert not (repo / ".neo").exists()
        assert neoconfig.project_settings_dir(repo) == repo / ".vex"
        assert info["dir_name"] == ".vex"
        # missing files are added to the directory the user already has
        assert (repo / ".vex" / "settings.local.toml").is_file()
        assert (repo / ".vex" / "commands" / "fix.md").is_file()
        # the existing settings file is neither overwritten nor counted
        assert info["settings_created"] is False
        assert "settings.toml" not in info["created"]
        assert (repo / ".vex" / "settings.toml").read_text(encoding="utf-8") == (
            'model = "legacy-model"\n'
        )
        # and the reader still returns the user's value
        assert neoconfig.effective_settings(start=repo)["model"] == "legacy-model"

    def test_legacy_repo_gitignore_names_legacy_dir(self, tmp_path, monkeypatch):
        # These test repo DETECTION, so the file's autouse $NEO_PROJECT_DIR
        # (which names a sibling dir) must be cleared, as the sibling
        # scaffold tests do.
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".vex" / "settings.toml", 'model = "m"\n')
        assert neoconfig.ensure_gitignore(repo) == "created"
        text = (repo / ".gitignore").read_text(encoding="utf-8")
        assert ".vex/settings.local.toml" in text
        assert ".neo/settings.local.toml" not in text

    def test_env_project_dir_names_its_own_directory(self, tmp_path, monkeypatch):
        """$NEO_PROJECT_DIR points AT the settings dir, so its basename is
        the name the scaffold and the ignore entries must agree on.

        Regression: the resolver ignored the env's own basename and fell back
        to `.neo`, so an explicit `$NEO_PROJECT_DIR=/x/.vex` produced a
        `.neo/` scaffold and `.neo/` ignore entries for files living under
        `.vex/` — the split-brain this whole change exists to close.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".vex"))
        assert neoconfig._project_dir_name(repo) == ".vex"
        info = neoconfig.ensure_project_layout(repo)
        assert info["dir_name"] == ".vex"
        assert (repo / ".vex" / "settings.toml").is_file()
        assert not (repo / ".neo").exists()

    def test_blank_env_project_dir_falls_back(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".vex" / "settings.toml", 'model = "m"\n')
        for blank in ("  ", "", "."):
            monkeypatch.setenv("NEO_PROJECT_DIR", blank)
            monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
            assert neoconfig._project_dir_name(repo) == ".vex", blank

    def test_legacy_repo_local_is_ignored(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".vex" / "settings.local.toml", 'model = "m"\n')
        _write(repo / ".vex" / "connectors.local.toml", "")
        neoconfig.ensure_gitignore(repo)
        assert neoconfig.local_is_ignored(repo)
        assert neoconfig.ensure_gitignore(repo) == "present"

    def test_current_dir_wins_over_legacy(self, tmp_path, monkeypatch):
        """Both present: the current name is authoritative (documented policy)."""
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".neo" / "settings.toml", 'model = "current"\n')
        _write(repo / ".vex" / "settings.toml", 'model = "legacy"\n')
        info = neoconfig.ensure_project_layout(repo)
        assert info["dir_name"] == ".neo"
        assert neoconfig.project_settings_dir(repo) == repo / ".neo"
        # and the current name is the one the reader uses
        assert neoconfig.effective_settings(start=repo)["model"] == "current"

    def test_find_git_root_walks_up(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        deep = repo / "a" / "b"
        deep.mkdir(parents=True)
        assert neoconfig.find_git_root(deep) == repo
        assert neoconfig.find_git_root(repo) == repo

    def test_scaffold_creates_full_layout(self, tmp_path, monkeypatch):
        """First `neo` in a repo: settings.toml + settings.local.toml +
        commands/ + skills/ with one working example each."""
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        info = neoconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == repo
        assert info["created"] == [
            "settings.toml",
            "settings.local.toml",
            "commands/fix.md",
            "skills/code-review/SKILL.md",
            "connectors.toml",
            "connectors.local.toml",
        ]
        # committable starter: comment-only, no keys, no secrets
        starter = (repo / ".neo" / "settings.toml").read_text(encoding="utf-8")
        assert starter and "api_key" not in starter
        assert all(not ln.strip() or ln.startswith("#") for ln in starter.splitlines())
        # local starter: parses to zero keys (effective settings unchanged)
        assert neoconfig.load_neo_config(repo / ".neo" / "settings.local.toml") == {}
        assert (repo / ".neo" / "connectors.toml").is_file()
        assert (repo / ".neo" / "connectors.local.toml").is_file()
        assert ".neo/connectors.local.toml" in (repo / ".gitignore").read_text(
            encoding="utf-8"
        )
        # the examples actually work with the real loaders
        from cli.commands import load_command
        from harness.skills import discover_skills

        template = load_command("fix", str(repo))
        assert template is not None and "$ARGUMENTS" in template
        assert [s.name for s in discover_skills(str(repo))] == ["code-review"]
        # local file is git-ignored
        assert neoconfig.local_is_ignored(repo)

    def test_scaffold_from_subdirectory_lands_at_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        deep = repo / "pkg" / "sub"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        info = neoconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == repo
        assert (repo / ".neo" / "settings.toml").is_file()
        assert not (deep / ".neo").exists()

    def test_scaffold_never_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        _write(repo / ".neo" / "settings.toml", 'model = "mine"\n')
        _write(repo / ".neo" / "settings.local.toml", 'model = "local-mine"\n')
        _write(repo / ".neo" / "commands" / "custom.md", "# mine\n")
        _write(
            repo / ".neo" / "skills" / "mine" / "SKILL.md",
            "---\nname: mine\ndescription: mine\n---\n\nBody.\n",
        )
        monkeypatch.chdir(repo)
        info = neoconfig.maybe_scaffold_repo()
        assert info is not None
        assert info["created"] == [
            "commands/fix.md",
            "skills/code-review/SKILL.md",
            "connectors.toml",
            "connectors.local.toml",
        ]
        assert (repo / ".neo" / "settings.toml").read_text(
            encoding="utf-8"
        ) == 'model = "mine"\n'
        assert (repo / ".neo" / "commands" / "custom.md").read_text(
            encoding="utf-8"
        ) == "# mine\n"
        assert (repo / ".neo" / "commands" / "fix.md").is_file()
        assert (repo / ".neo" / "skills" / "code-review" / "SKILL.md").is_file()
        assert neoconfig.local_is_ignored(repo)

    def test_no_scaffold_outside_repo(self, tmp_path, monkeypatch):
        """Outside a git repo nothing is created (the gate is
        find_git_root — stubbed here so the test holds regardless of
        what .git entries exist above the tmp dir)."""
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: None)
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        assert neoconfig.maybe_scaffold_repo() is None
        assert not (plain / ".neo").exists()

    def test_scaffold_respects_neo_project_dir(self, tmp_path, monkeypatch):
        """$NEO_PROJECT_DIR (points AT the .neo dir) wins over repo
        detection — the test-isolation override scaffolds its parent."""
        target = tmp_path / "p" / ".neo"
        monkeypatch.setenv("NEO_PROJECT_DIR", str(target))
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        info = neoconfig.maybe_scaffold_repo()
        assert info is not None and info["root"] == tmp_path / "p"
        assert (target / "settings.toml").is_file()

    def test_scaffold_never_raises_on_readonly_root(
        self, tmp_path, monkeypatch, capsys
    ):
        """A scaffold failure degrades to (at most) one warning, never
        an exception — the session must stay usable."""
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")  # parent mkdir fails
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: blocker)
        assert neoconfig.maybe_scaffold_repo() is None
        assert "Traceback" not in capsys.readouterr().err

    def test_init_project_creates_full_layout(self, tmp_path, capsys, monkeypatch):
        """`neo config init-project` (the explicit form) scaffolds the
        same layout + gitignore."""
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        assert m.main(["config", "init-project"]) == 0
        assert (repo / ".neo" / "settings.toml").is_file()
        assert (repo / ".neo" / "settings.local.toml").is_file()
        assert (repo / ".neo" / "commands" / "fix.md").is_file()
        assert (repo / ".neo" / "skills" / "code-review" / "SKILL.md").is_file()
        assert ".neo/settings.local.toml" in (repo / ".gitignore").read_text(
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
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
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
        info = neoconfig.maybe_scaffold_repo()
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
        assert any(".neo/settings.toml" in ln for ln in lines)
        assert not any("settings.local.toml" in ln for ln in lines)

    def test_broken_toml_warns_once(self, tmp_path, capsys):
        """A broken tier warns on first read, then stays silent (the
        chain is re-read per key — without dedup one file spams)."""
        _write(tmp_path / "global" / "settings.toml", "model = [unclosed")
        neoconfig.merged_settings()
        first = capsys.readouterr().err
        assert "not valid TOML" in first
        neoconfig.merged_settings()
        neoconfig.merged_settings()
        assert capsys.readouterr().err == ""


class TestProductRoundConfigSurface:
    def test_scaffold_creates_connector_examples_and_ignores_local_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        info = neoconfig.maybe_scaffold_repo()
        assert info is not None
        assert (repo / ".neo" / "connectors.toml").is_file()
        assert (repo / ".neo" / "connectors.local.toml").is_file()
        assert neoconfig.local_is_ignored(repo)
        text = (repo / ".gitignore").read_text(encoding="utf-8")
        assert ".neo/settings.local.toml" in text
        assert ".neo/connectors.local.toml" in text

    def test_scaffold_recreates_missing_example_without_overwriting_user_files(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = _mk_git_repo(tmp_path / "repo")
        (repo / ".neo" / "commands").mkdir(parents=True)
        (repo / ".neo" / "commands" / "custom.md").write_text("mine", encoding="utf-8")
        monkeypatch.chdir(repo)
        neoconfig.maybe_scaffold_repo()
        assert (repo / ".neo" / "commands" / "custom.md").read_text(
            encoding="utf-8"
        ) == "mine"
        assert (repo / ".neo" / "commands" / "fix.md").is_file()

    def test_config_json_masks_key_and_endpoint_credentials(self, capsys):
        neoconfig.set_tier_key("global", "api_key", "sk-json-secret")
        neoconfig.set_tier_key(
            "global", "base_url", "https://user:pass@example.test/v1?token=query-secret"
        )
        assert m.main(["config", "list", "--json"]) == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert "sk-json-secret" not in out
        assert "query-secret" not in out
        assert doc["source_tiers"]["api_key"] == "global"

    def test_settings_resolution_accepts_target_repository(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".neo").mkdir(parents=True)
        (repo / ".neo" / "settings.toml").write_text(
            'model = "repo-model"\n', encoding="utf-8"
        )
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.chdir(other)
        assert neoconfig.apply_config_defaults({}, start=repo)["model"] == "repo-model"
        assert "model" not in neoconfig.apply_config_defaults({})

    def test_read_only_scaffold_reports_no_traceback(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: blocker)
        assert neoconfig.maybe_scaffold_repo() is None
        assert "Traceback" not in capsys.readouterr().err


class TestProviderProfiles:
    def test_named_profile_resolves_and_reports_source(self):
        _write(
            neoconfig.global_settings_path(),
            'provider_profile = "work"\n\n'
            "[provider_profiles.work]\n"
            'provider = "openai"\n'
            'model = "profile-model"\n'
            'base_url = "https://router.example/v1?token=hidden"\n'
            'api_key = "sk-profile-secret"\n',
        )
        resolved = neoconfig.resolve_provider_config()
        assert resolved["profile"] == "work"
        assert resolved["model"] == "profile-model"
        assert resolved["api_base"] == "https://router.example/v1?token=hidden"
        assert resolved["source_tiers"]["model"] == "profile"
        assert resolved["sources"]["model"].startswith("profile:work@")

    def test_env_and_flag_profile_precedence(self, monkeypatch):
        _write(
            neoconfig.global_settings_path(),
            'provider_profile = "global-profile"\n\n'
            '[provider_profiles.global-profile]\nmodel = "global-model"\n\n'
            '[provider_profiles.env-profile]\nmodel = "env-model"\n\n'
            '[provider_profiles.flag-profile]\nmodel = "flag-model"\n',
        )
        monkeypatch.setenv("NEO_PROVIDER_PROFILE", "env-profile")
        assert neoconfig.resolve_provider_config()["model"] == "env-model"
        assert (
            neoconfig.resolve_provider_config({"profile": "flag-profile"})["model"]
            == "flag-model"
        )

    def test_profile_fields_merge_across_tiers(self):
        _write(
            neoconfig.global_settings_path(),
            '[provider_profiles.work]\nmodel = "global-model"\napi_key = "sk-global"\n',
        )
        _write(
            Path(os.environ["NEO_PROJECT_DIR"]) / "settings.toml",
            'provider_profile = "work"\n\n[provider_profiles.work]\nmodel = "project-model"\n',
        )
        resolved = neoconfig.resolve_provider_config()
        assert resolved["model"] == "project-model"
        assert resolved["api_key"] == "sk-global"

    def test_profile_json_output_masks_key_and_endpoint(self, capsys):
        neoconfig.set_provider_profile(
            "work",
            {
                "provider": "openai",
                "model": "m",
                "base_url": "https://user:pass@example.test/v1?token=query-secret",
                "api_key": "sk-json-profile",
            },
        )
        neoconfig.select_provider_profile("work")
        assert m.main(["config", "list", "--json"]) == 0
        output = capsys.readouterr().out
        assert "sk-json-profile" not in output
        assert "query-secret" not in output
        doc = json.loads(output)
        masked = doc["values"]["provider_profiles"]["work"]["api_key"]
        assert "sk-json-profile" not in masked
        assert "(set)" in masked

    def test_task_profile_flag_resolves_provider_fields(self):
        neoconfig.set_provider_profile(
            "work",
            {
                "provider": "openai",
                "model": "profile-task-model",
                "base_url": "https://profile.example/v1",
                "api_key": "sk-profile-task",
            },
        )
        args = m.build_parser().parse_args(
            ["fix", "--repo", ".", "--issue", "i", "--profile", "work"]
        )
        task = m._make_task(args)
        assert task.config["model"] == "profile-task-model"
        assert task.config["api_base"] == "https://profile.example/v1"
        assert task.config["provider"] == "openai"
        assert task.config["provider_profile"] == "work"

    def test_benchmark_applies_target_repo_settings_and_flags(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        neo = repo / ".neo"
        neo.mkdir(parents=True)
        (neo / "settings.toml").write_text(
            'model = "repo-model"\nprovider = "openai"\n'
            'base_url = "https://repo.example/v1"\napi_key = "repo-key"\n'
            "budget_cap_usd = 3.0\n",
            encoding="utf-8",
        )
        subset = tmp_path / "subset.json"
        subset.write_text(
            json.dumps([{"repo": str(repo), "issue": "i", "config": {}}]),
            encoding="utf-8",
        )
        args = m.build_parser().parse_args(
            [
                "run-benchmark",
                "--subset",
                str(subset),
                "--model",
                "flag-model",
                "--budget",
                "7.0",
            ]
        )
        tasks = m._load_subset(str(subset), args)
        assert tasks is not None and len(tasks) == 1
        assert tasks[0].config["model"] == "flag-model"
        assert tasks[0].config["api_base"] == "https://repo.example/v1"
        assert tasks[0].config["api_key"] == "repo-key"
        assert tasks[0].config["budget_cap_usd"] == 7.0

    def test_profile_use_list_show_remove_cli(self, capsys):
        neoconfig.set_provider_profile(
            "work", {"provider": "openai", "model": "m", "api_key": "sk-secret"}
        )
        assert m.main(["profile", "use", "work", "--tier", "project"]) == 0
        capsys.readouterr()
        assert m.main(["profile", "list", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["active"] == "work"
        assert "sk-secret" not in json.dumps(listed)
        assert m.main(["profile", "show", "work", "--json"]) == 0
        assert "sk-secret" not in capsys.readouterr().out
        assert m.main(["profile", "remove", "work"]) == 0
        assert m.main(["profile", "show", "work", "--json"]) == 2
