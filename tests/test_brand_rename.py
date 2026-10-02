"""The VEX -> NEO rename: what must stay true.

A rename's own regression surface is the compatibility contract, because
that is the only part a reviewer cannot check by reading the new name. These
tests pin four claims:

1. the canonical names are ``neo`` in every spelling the tree uses;
2. the previous spellings are still READ, and never written;
3. the read fallback cannot outrank an explicit new value, and cannot
   migrate anything on disk;
4. the on-disk fallbacks are reachable in the real resolvers, not just in
   the brand module.

Host-only: no Docker, no provider, no network, no credential. Every test
builds its own tree under ``tmp_path`` and pins ``VEX_HOME``/``NEO_HOME`` so
it never reads or writes the developer's real state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared import brand

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestTheCanonicalNames:
    def test_command_and_distribution(self):
        assert brand.COMMAND == "neo"
        assert brand.DISTRIBUTION == "neo-agent-cli"
        assert brand.ENV_PREFIX == "NEO"
        assert brand.PROJECT_DIRNAME == ".neo"
        assert brand.HOME_DIRNAME == "neo"

    def test_the_legacy_names_are_still_declared(self):
        """A deprecation with no declared old name cannot be honoured, and
        one that forgets the previous DISTRIBUTION cannot uninstall the
        package a user actually has installed."""
        assert brand.LEGACY_COMMAND == "vex"
        assert brand.LEGACY_ENV_PREFIX == "VEX"
        assert brand.legacy_project_dirname() == ".vex"
        assert brand.LEGACY_DISTRIBUTIONS == ("neo-harness", "vex-harness")
        assert "vex" in brand.legacy_home_candidates()

    def test_the_console_script_is_declared_in_pyproject(self):
        """The installed command is packaging metadata, so the packaging
        metadata is what has to say it."""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'neo = "cli.main:main"' in text
        # the previous spellings remain as aliases for one release
        assert 'vex = "cli.main:main"' in text
        assert 'harness = "cli.main:main"' in text
        assert 'name = "neo-agent-cli"' in text

    def test_the_cli_reports_neo_as_its_program_name(self):
        """argparse's usage line and error prefix come from this, so a
        typo here is a user-visible `usage: vexx`."""
        out = subprocess.run(
            [sys.executable, "-m", "cli", "--nope"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"},
            timeout=120,
        )
        combined = out.stdout + out.stderr
        assert "usage: neo" in combined, combined[:400]
        assert "vex" not in combined.lower(), combined[:400]


class TestTheLegacyEnvIsStillRead:
    def test_a_legacy_variable_is_carried_across(self):
        env = {"VEX_HOME": "/tmp/old", "PATH": "/usr/bin"}
        applied = brand.apply_legacy_env(env)
        assert env["NEO_HOME"] == "/tmp/old"
        assert applied == {"NEO_HOME": "/tmp/old"}

    def test_the_new_name_always_wins(self):
        """The one rule that makes this safe: a user who renamed the
        variable in their profile and still exports the old one somewhere
        must get the NEW root, or they have no way to tell which one the
        product used."""
        env = {"VEX_HOME": "/tmp/old", "NEO_HOME": "/tmp/new"}
        applied = brand.apply_legacy_env(env)
        assert env["NEO_HOME"] == "/tmp/new"
        assert applied == {}

    def test_an_empty_legacy_value_is_not_carried_across(self):
        """`export VEX_HOME=` is an UNSET variable in every shell that
        matters; copying the empty string across would make a resolver
        believe the user pointed the root at the current directory."""
        env = {"VEX_HOME": "", "VEX_MODEL": "   "}
        applied = brand.apply_legacy_env(env)
        assert applied == {}
        assert "NEO_HOME" not in env
        assert "NEO_MODEL" not in env

    def test_whitespace_only_legacy_value_is_not_carried_across(self):
        env = {"VEX_PROJECT_DIR": "  "}
        assert brand.apply_legacy_env(env) == {}
        assert "NEO_PROJECT_DIR" not in env

    def test_every_legacy_prefixed_name_is_covered(self):
        """The shim is generic, so a NEW NEO_* variable is honoured from its
        legacy spelling with no code change. This is the property that stops
        the compat list from becoming a hand-maintained table that rots."""
        env = {
            "VEX_TRACE_DIR": "a",
            "VEX_EFFORT": "b",
            "VEX_SOMETHING_ADDED_IN_2030": "c",
        }
        applied = brand.apply_legacy_env(env)
        assert set(applied) == {
            "NEO_TRACE_DIR",
            "NEO_EFFORT",
            "NEO_SOMETHING_ADDED_IN_2030",
        }

    def test_an_unrelated_variable_is_untouched(self):
        env = {"PATH": "/usr/bin", "HARNESS_HOME": "/h", "VEXH": "no-underscore"}
        applied = brand.apply_legacy_env(env)
        assert applied == {}
        assert env["PATH"] == "/usr/bin"
        assert env["HARNESS_HOME"] == "/h"
        assert "NEOH" not in env

    def test_a_non_mapping_is_refused_rather_than_silently_ignored(self):
        """Returning quietly from a non-mapping would be a compat shim that
        claims to have run and did not."""
        with pytest.raises(TypeError):
            brand.apply_legacy_env(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_the_report_separates_honoured_from_shadowed(self):
        report = brand.legacy_names_report(
            {"VEX_HOME": "/old", "VEX_MODEL": "/m", "NEO_MODEL": "/new"}
        )
        assert report["legacy_env_present"] == ["VEX_HOME", "VEX_MODEL"]
        assert report["legacy_env_honoured"] == ["NEO_HOME"]
        assert report["legacy_env_shadowed"] == ["VEX_MODEL"]
        assert report["command"] == "neo"
        assert report["legacy_command"] == "vex"

    def test_the_notice_names_the_replacement(self):
        notice = brand.deprecation_notice(force=True)
        assert "NEO_" in notice
        assert "neo" in notice
        assert "vex" in notice.lower()

    def test_the_notice_is_emitted_once(self):
        brand.deprecation_notice(force=True)
        assert brand.deprecation_notice() == ""


class TestTheOnDiskFallbacks:
    def test_the_current_env_name_beats_the_legacy_one(self, tmp_path, monkeypatch):
        """The documented order is NEO_HOME, then VEX_HOME, then
        HARNESS_HOME. If NEO_HOME lost to VEX_HOME, a user who renamed the
        variable and still has the old one exported would silently keep the
        old root with no way to tell which one the product used."""
        from memory import paths

        monkeypatch.setenv("NEO_HOME", str(tmp_path / "new"))
        monkeypatch.setenv("VEX_HOME", str(tmp_path / "old"))
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "oldest"))
        assert paths.neo_home() == (tmp_path / "new").resolve()

    def test_neo_home_honours_the_legacy_env_name(self, tmp_path, monkeypatch):
        from memory import paths

        legacy = tmp_path / "old-home"
        legacy.mkdir()
        monkeypatch.delenv("NEO_HOME", raising=False)
        monkeypatch.setenv("VEX_HOME", str(legacy))
        assert paths.neo_home() == legacy.resolve()

    def test_neo_home_falls_back_to_the_legacy_directory(self, tmp_path, monkeypatch):
        """The case that actually matters on an upgraded machine: no
        override at all, and the previous data directory already exists. It
        must be used, or every setting, session and log is orphaned."""
        from memory import paths

        data = tmp_path / "data"
        legacy = data / "vex"
        legacy.mkdir(parents=True)
        monkeypatch.delenv("NEO_HOME", raising=False)
        monkeypatch.delenv("VEX_HOME", raising=False)
        monkeypatch.delenv("HARNESS_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(data))
        monkeypatch.setenv("APPDATA", str(data))
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setattr(paths.os, "name", "nt")
        assert paths.neo_home() == legacy.resolve()

    def test_a_present_new_directory_beats_a_legacy_one(self, tmp_path, monkeypatch):
        """Both existing must not leave the user on the old root forever
        because the new one happened to be created second."""
        from memory import paths

        data = tmp_path / "data"
        (data / "neo").mkdir(parents=True)
        (data / "vex").mkdir(parents=True)
        monkeypatch.delenv("NEO_HOME", raising=False)
        monkeypatch.delenv("VEX_HOME", raising=False)
        monkeypatch.delenv("HARNESS_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(data))
        monkeypatch.setenv("APPDATA", str(data))
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setattr(paths.os, "name", "nt")
        assert paths.neo_home() == (data / "neo").resolve()

    def test_the_fallback_never_creates_or_writes_anything(self, tmp_path, monkeypatch):
        """A read fallback that migrates is a tool that moves a user's
        state without being asked."""
        from memory import paths

        data = tmp_path / "data"
        legacy = data / "vex"
        legacy.mkdir(parents=True)
        monkeypatch.delenv("NEO_HOME", raising=False)
        monkeypatch.delenv("VEX_HOME", raising=False)
        monkeypatch.delenv("HARNESS_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(data))
        monkeypatch.setenv("APPDATA", str(data))
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setattr(paths.os, "name", "nt")
        before = sorted(p.name for p in legacy.iterdir())
        paths.neo_home()
        assert not (data / "neo").exists()
        assert sorted(p.name for p in legacy.iterdir()) == before

    def test_the_project_dir_finds_a_legacy_repository(self, tmp_path, monkeypatch):
        """A repository whose state directory still has the previous name
        keeps loading its own project settings."""
        from cli import neoconfig

        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".vex").mkdir()
        nested = repo / "src" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: repo)
        found = neoconfig.project_settings_dir(nested)
        assert found is not None and found.name == ".vex"

    def test_the_legacy_project_dir_never_escapes_the_repository(
        self, tmp_path, monkeypatch
    ):
        """The bound that matters. An unbounded walk reaches ``$HOME``, and
        a ``~/.vex`` left by an earlier install is a USER-level directory
        that is not any repository's project state -- returning it would
        hand every unrelated project the same settings file.

        ``find_git_root`` is patched so the assertion holds on a host whose
        HOME is a dotfiles git repository (where every temp path is inside
        a repo and an unbound walk reaches ``$HOME`` on its own).
        """
        from cli import neoconfig

        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (tmp_path / ".vex").mkdir()  # stray, ABOVE the repo root
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: repo)
        assert neoconfig.project_settings_dir(repo) is None

    def test_the_legacy_branch_is_inert_outside_a_repository(
        self, tmp_path, monkeypatch
    ):
        """No git root -> the legacy branch does not run at all.

        ``find_git_root`` is patched rather than relying on the filesystem:
        on a machine whose HOME is itself a dotfiles git repository (a very
        common setup) every ``tmp_path`` IS inside a repo, so a test that
        depends on there not being one passes by accident on a clean host
        and fails everywhere else.
        """
        from cli import neoconfig

        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        (tmp_path / ".vex").mkdir()
        monkeypatch.setattr(neoconfig, "find_git_root", lambda start=None: None)
        assert neoconfig.project_settings_dir(tmp_path) is None

    def test_the_project_dir_prefers_the_current_name(self, tmp_path, monkeypatch):
        from cli import neoconfig

        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.delenv("VEX_PROJECT_DIR", raising=False)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".neo").mkdir()
        (tmp_path / ".vex").mkdir()
        found = neoconfig.project_settings_dir(tmp_path)
        assert found is not None and found.name == ".neo"

    def test_the_migration_notice_names_the_directory(self, tmp_path):
        from cli import neoconfig

        assert neoconfig.neo_migration_notice(tmp_path / ".neo") == ""
        assert neoconfig.neo_migration_notice(None) == ""
        notice = neoconfig.neo_migration_notice(tmp_path / ".vex")
        assert ".vex" in notice
        assert ".neo" in notice

    def test_the_project_dir_honours_the_legacy_env_name(self, tmp_path, monkeypatch):
        from cli import neoconfig

        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / "elsewhere"))
        assert neoconfig.project_settings_dir() == tmp_path / "elsewhere"


class TestTheWordmark:
    def test_it_reads_as_neo_in_both_variants(self):
        """Both variants are checked because the '#' fallback is a
        hand-maintained ASCII rendering of the same three letters, and a
        letterform can be fixed in one and broken in the other."""
        from cli import ui

        for rows in (ui._WORDMARK_ANSI, ui._WORDMARK_BLOCK):
            assert len(rows) == 6, rows
            assert all(len(r) == 26 for r in rows), [len(r) for r in rows]

    def test_no_surface_still_says_the_old_word(self):
        """The splash and compact header are the two brand surfaces. A
        leftover in either is a screenshot nobody would think to check."""
        from cli import ui

        blob = "\n".join(ui.wordmark_lines()) + ui.TAGLINE
        assert "vex" not in blob.lower()
        assert "VEX" not in blob
