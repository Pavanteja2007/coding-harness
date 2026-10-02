"""Hermetic regression tests for the release uninstall path."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from cli import uninstall

_REAL_PIP_INSTALLED = uninstall.pip_installed
_REAL_PIPX_VENV_DIR = uninstall.pipx_venv_dir
_REAL_BIN_ON_USER_PATH = uninstall._bin_on_user_path


@pytest.fixture
def isolated_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each uninstall test a private home and no real installer state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in (
        "APPDATA",
        "XDG_CONFIG_HOME",
        "NEO_CONFIG",
        "NEO_LEGACY_CONFIG",
        "PIPX_HOME",
        "PIPX_LOCAL_VENVS",
        "NEO_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "HARNESS_HOME",
        "HARNESS_DECISIONS_DB",
        "HARNESS_LOGS_DIR",
        "NEO_TRACE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(uninstall, "_windows", lambda: False)
    monkeypatch.setattr(uninstall, "pip_installed", lambda: False)
    monkeypatch.setattr(uninstall, "pipx_venv_dir", lambda: None)
    monkeypatch.setattr(uninstall, "_bin_on_user_path", lambda _bin: False)
    return home


def _args(*, yes: bool = True, dry_run: bool = False) -> argparse.Namespace:
    """Build the command namespace used by uninstall tests."""
    return argparse.Namespace(yes=yes, dry_run=dry_run)


def _fake_result(returncode: int = 0) -> SimpleNamespace:
    """Build a subprocess-like result without starting a process."""
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def test_plain_pip_uninstalls_distribution_with_current_interpreter(
    isolated_uninstall: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Plain pip uses sys.executable and leaves entry-point removal to pip."""
    calls: List[Tuple[List[str], dict[str, Any]]] = []

    def fake_run(command: List[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return _fake_result()

    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(uninstall, "pip_entry_point_paths", lambda _name=None: [])
    monkeypatch.setattr(uninstall.subprocess, "run", fake_run)
    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert calls[0][0] == [
        sys.executable,
        "-m",
        "pip",
        "uninstall",
        "-y",
        "neo-agent-cli",
    ]
    assert calls[0][1].get("shell", False) is False
    assert "neo/harness entry points" in capsys.readouterr().out


def test_windows_launcher_pip_uninstall_is_deferred_to_external_helper(
    isolated_uninstall: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    venv = tmp_path / "active-venv"
    python = venv / "Scripts" / "python.exe"
    neo = venv / "Scripts" / "neo.exe"
    harness = venv / "Scripts" / "harness.exe"
    for path in (python, neo, harness):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fake", encoding="utf-8")
    outside = tmp_path / "base-python.exe"
    outside.write_text("fake", encoding="utf-8")
    receipt = tmp_path / "neo-uninstall-test.json"
    calls: List[Tuple[List[str], dict[str, Any]]] = []

    def fake_popen(command: List[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(kill=lambda: None)

    monkeypatch.setattr(uninstall, "_windows", lambda: True)
    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(uninstall, "pip_distribution_name", lambda: "neo-agent-cli")
    monkeypatch.setattr(
        uninstall, "pip_entry_point_paths", lambda _name=None: [neo, harness]
    )
    monkeypatch.setattr(
        uninstall, "_windows_launcher_process", lambda _paths: (4321, neo)
    )
    monkeypatch.setattr(uninstall, "_outside_python", lambda _venv: outside)
    monkeypatch.setattr(uninstall, "_new_uninstall_receipt", lambda: receipt)
    monkeypatch.setattr(
        uninstall, "_wait_for_uninstall_receipt", lambda _path: {"status": "waiting"}
    )
    monkeypatch.setattr(uninstall.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "locked launcher invoked pip synchronously"
        ),
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:4] == [str(outside), "-I", "-u", "-c"]
    assert command[4] == uninstall._WINDOWS_PIP_HELPER
    assert command[5:] == [
        "4321",
        str(neo.resolve()),
        str(venv.resolve()),
        str(python.resolve()),
        "neo-agent-cli",
        str(receipt),
    ]
    assert "shell=False" in command[4]
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == (
        uninstall._DETACHED_PROCESS | uninstall._CREATE_NEW_PROCESS_GROUP
    )
    assert kwargs["env"]["PIP_CONFIG_FILE"] == uninstall.os.devnull
    output = capsys.readouterr().out
    assert "external" in output
    assert "interpreter" in output
    assert "Neo uninstall scheduled" in output


def test_windows_launcher_schedule_failure_stops_before_other_cleanup(
    isolated_uninstall: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = isolated_uninstall / ".config" / "neo"
    config_root.mkdir(parents=True)
    settings = config_root / "settings.toml"
    settings.write_text("model = 'local'\n", encoding="utf-8")
    monkeypatch.setattr(uninstall, "_windows", lambda: True)
    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(uninstall, "pip_distribution_name", lambda: "neo-agent-cli")
    monkeypatch.setattr(uninstall, "pip_entry_point_paths", lambda _name=None: [])

    monkeypatch.setattr(
        uninstall,
        "_windows_launcher_process",
        lambda _paths: (4321, tmp_path / "neo.exe"),
    )
    monkeypatch.setattr(
        uninstall,
        "_schedule_windows_pip_uninstall",
        lambda *_args: (1, "external helper unavailable"),
    )
    monkeypatch.setattr(
        uninstall,
        "_remove_target",
        lambda _target: pytest.fail("cleanup continued after helper scheduling failed"),
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 1
    assert settings.is_file()
    assert "could not schedule safe pip uninstall" in capsys.readouterr().err


def test_pipx_uses_safe_uninstall_command(
    isolated_uninstall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipx removal is delegated to the installed pipx executable."""
    pipx_home = isolated_uninstall / ".local" / "pipx"
    venv = pipx_home / "venvs" / "neo-agent-cli"
    venv.mkdir(parents=True)
    pipx = tmp_path / "fake-pipx"
    pipx.write_text("fake", encoding="utf-8")
    calls: List[List[str]] = []

    def fake_run(command: List[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        shutil.rmtree(venv)
        return _fake_result()

    monkeypatch.setattr(uninstall, "pipx_venv_dir", lambda: venv)
    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: str(pipx))
    monkeypatch.setattr(uninstall.subprocess, "run", fake_run)
    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert calls == [[str(pipx), "uninstall", "neo-agent-cli"]]
    assert not venv.exists()


def test_custom_config_parent_and_outside_canary_are_preserved(
    isolated_uninstall: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A custom config path never grants ownership of its arbitrary parent."""
    outside = tmp_path / "user-owned"
    outside.mkdir()
    canary = outside / "keep.txt"
    canary.write_text("keep", encoding="utf-8")
    custom = outside / "settings.toml"
    custom.write_text("model = 'local'", encoding="utf-8")
    legacy = tmp_path / "another-user-dir" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("model = 'legacy'", encoding="utf-8")
    monkeypatch.setenv("NEO_CONFIG", str(custom))
    monkeypatch.setenv("NEO_LEGACY_CONFIG", str(legacy))
    monkeypatch.setattr(uninstall, "settings_roots", lambda: [outside])

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert canary.read_text(encoding="utf-8") == "keep"
    assert custom.exists()
    assert legacy.exists()
    assert outside.exists()
    assert "outside documented Neo roots" in capsys.readouterr().out


def test_owned_config_files_and_installer_roots_are_contained(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup removes owned files and known children, not neighboring data."""
    config_root = isolated_uninstall / ".config" / "neo"
    config_root.mkdir(parents=True)
    (config_root / "settings.toml").write_text("model = 'x'", encoding="utf-8")
    (config_root / "keep.txt").write_text("keep", encoding="utf-8")
    plugin_root = config_root / "plugins"
    plugin_root.mkdir()
    (plugin_root / "demo.txt").write_text("owned", encoding="utf-8")
    venv = isolated_uninstall / ".neo-venv"
    venv.mkdir()
    (venv / "canary").write_text("owned", encoding="utf-8")
    shim_root = isolated_uninstall / ".neo" / "bin"
    shim_root.mkdir(parents=True)
    (shim_root.parent / "install-route").write_text("route=venv\n", encoding="utf-8")
    (shim_root / "neo").write_text("shim", encoding="utf-8")

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert not venv.exists()
    assert not shim_root.exists()
    assert not (shim_root.parent / "install-route").exists()
    assert not (config_root / "settings.toml").exists()
    assert not plugin_root.exists()
    assert (config_root / "keep.txt").exists()


def test_containment_helper_refuses_outside_root(
    isolated_uninstall: Path, tmp_path: Path
) -> None:
    """Resolved containment blocks recursive removal outside owned roots."""
    root = isolated_uninstall / ".config" / "neo"
    root.mkdir(parents=True)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep", encoding="utf-8")

    code, message = uninstall._remove_tree(outside, [root])

    assert code == 1
    assert "outside Neo-owned roots" in message
    assert canary.exists()


def test_repeated_uninstall_is_a_successful_noop(
    isolated_uninstall: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second uninstall does not invoke package tools or report an error."""
    installed = True
    calls: List[List[str]] = []

    def fake_run(command: List[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal installed
        calls.append(command)
        installed = False
        return _fake_result()

    monkeypatch.setattr(uninstall, "pip_installed", lambda: installed)
    monkeypatch.setattr(uninstall, "pip_entry_point_paths", lambda _name=None: [])
    monkeypatch.setattr(uninstall.subprocess, "run", fake_run)

    assert uninstall.cmd_uninstall(_args()) == 0
    first_output = capsys.readouterr().out
    assert uninstall.cmd_uninstall(_args()) == 0
    second_output = capsys.readouterr().out

    assert len(calls) == 1
    assert "Neo uninstall complete" in first_output
    assert "already uninstalled" in second_output


def test_pip_failure_is_nonzero(
    isolated_uninstall: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed package operation is surfaced as an incomplete uninstall."""
    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda command, **kwargs: _fake_result(1),
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 1
    assert "pip uninstall failed" in capsys.readouterr().err


def test_pipx_failure_is_nonzero(
    isolated_uninstall: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed pipx operation is surfaced as an incomplete uninstall."""
    venv = isolated_uninstall / ".local" / "pipx" / "venvs" / "neo-agent-cli"
    venv.mkdir(parents=True)
    monkeypatch.setattr(uninstall, "pipx_venv_dir", lambda: venv)
    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: None)

    result = uninstall.cmd_uninstall(_args())

    assert result == 1
    assert "pipx uninstall failed" in capsys.readouterr().err
    assert venv.exists()


def test_path_cleanup_failure_is_nonzero(
    isolated_uninstall: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A persistent PATH edit failure produces a failed uninstall."""
    monkeypatch.setattr(uninstall, "_bin_on_user_path", lambda _bin: True)
    monkeypatch.setattr(
        uninstall, "_remove_user_path_entry", lambda _bin: (1, "denied")
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 1
    assert "PATH cleanup did not complete" in capsys.readouterr().err


def test_dry_run_never_starts_package_tool(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run inspection does not execute pip or pipx."""
    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("dry run started a package tool"),
    )
    monkeypatch.setattr(
        uninstall.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("dry run started an external helper"),
    )

    assert uninstall.cmd_uninstall(_args(dry_run=True)) == 0


def test_posix_path_cleanup_removes_only_installer_line(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX cleanup edits only the marked installer PATH block."""
    profile = isolated_uninstall / ".bashrc"
    bin_dir = isolated_uninstall / ".neo" / "bin"
    profile.write_text(
        f'before\n# Added by the Neo installer\nexport PATH="{bin_dir}:$PATH"\nafter\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(uninstall, "_bin_on_user_path", _REAL_BIN_ON_USER_PATH)
    assert uninstall._bin_on_user_path(bin_dir) is True
    code, _message = uninstall._remove_user_path_entry(bin_dir)

    assert code == 0
    text = profile.read_text(encoding="utf-8")
    assert "before" in text
    assert "after" in text
    assert "# Added by the Neo installer" not in text
    assert str(bin_dir) not in text


def test_windows_running_venv_cleanup_is_deferred_outside_venv(
    isolated_uninstall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running Windows venv is scheduled with an interpreter outside itself."""
    venv = isolated_uninstall / ".neo-venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("fake", encoding="utf-8")
    outside = tmp_path / "base-python.exe"
    outside.write_text("fake", encoding="utf-8")
    calls: List[Tuple[List[str], dict[str, Any]]] = []

    def fake_popen(command: List[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(uninstall, "_windows", lambda: True)
    monkeypatch.setattr(sys, "executable", str(venv / "Scripts" / "python.exe"))
    monkeypatch.setattr(uninstall, "_outside_python", lambda _venv: outside)
    monkeypatch.setattr(uninstall.subprocess, "Popen", fake_popen)

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert venv.exists()
    assert calls[0][0][0] == str(outside)
    assert calls[0][0][1] == "-I"
    assert calls[0][0][0] != str(venv)
    assert str(venv) in calls[0][0]
    assert "if os.name == 'nt':" in calls[0][0][3]
    assert "WaitForSingleObject" in calls[0][0][3]


def test_windows_running_venv_refuses_without_outside_interpreter(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cleanup refuses rather than deleting a venv it cannot safely defer."""
    venv = isolated_uninstall / ".neo-venv"
    venv.mkdir()
    monkeypatch.setattr(uninstall, "_windows", lambda: True)
    monkeypatch.setattr(sys, "executable", str(venv / "Scripts" / "python.exe"))
    monkeypatch.setattr(uninstall, "_outside_python", lambda _venv: None)

    result = uninstall.cmd_uninstall(_args())

    assert result == 1
    assert venv.exists()


def test_dedicated_installer_venv_is_not_uninstalled_through_itself(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedicated venv is removed directly rather than pip-uninstalling itself."""
    venv = isolated_uninstall / ".neo-venv"
    python = venv / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(uninstall, "pip_installed", lambda: True)
    monkeypatch.setattr(uninstall, "pip_entry_point_paths", lambda _name=None: [])
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "dedicated venv invoked pip against itself"
        ),
    )

    assert uninstall.cmd_uninstall(_args()) == 0
    assert not venv.exists()


def test_symlinked_legacy_root_cannot_redirect_recursive_deletion(
    isolated_uninstall: Path, tmp_path: Path
) -> None:
    """A ~/.neo symlink cannot make the uninstaller delete an outside tree."""
    outside = tmp_path / "outside"
    outside_bin = outside / "bin"
    outside_bin.mkdir(parents=True)
    canary = outside / "keep.txt"
    canary.write_text("keep", encoding="utf-8")
    (outside_bin / "neo").write_text("shim", encoding="utf-8")
    legacy = isolated_uninstall / ".neo"
    try:
        legacy.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    assert uninstall.cmd_uninstall(_args()) == 1
    assert canary.read_text(encoding="utf-8") == "keep"
    assert outside_bin.exists()


def test_current_and_fish_installer_profile_lines_are_removed(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both current POSIX marker formats are recognized as installer-owned."""
    target = isolated_uninstall / ".neo" / "bin"
    bash = isolated_uninstall / ".bashrc"
    fish = isolated_uninstall / ".config" / "fish" / "config.fish"
    fish.parent.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_uninstall / ".config"))
    bash.write_text(
        "# Added by the Neo installer\n"
        f"export NEO_INSTALLER_PATH=1; PATH={target.as_posix()}:$PATH\n"
        "export KEEP=1\n",
        encoding="utf-8",
    )
    fish.write_text(
        "# Added by the Neo installer\n"
        f"set -gx NEO_INSTALLER_PATH 1; set -gx PATH {target.as_posix()} $PATH\n",
        encoding="utf-8",
    )

    code, message = uninstall._remove_profile_path_entry(target)

    assert code == 0
    assert "removed" in message
    assert "NEO_INSTALLER_PATH" not in bash.read_text(encoding="utf-8")
    assert "KEEP=1" in bash.read_text(encoding="utf-8")
    assert "NEO_INSTALLER_PATH" not in fish.read_text(encoding="utf-8")


def test_current_single_line_profile_marker_is_detected(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer's current one-line PATH marker is persistent and removable."""
    target = isolated_uninstall / ".neo" / "bin"
    profile = isolated_uninstall / ".bashrc"
    profile.write_text(
        f"export NEO_INSTALLER_PATH=1; PATH={target.as_posix()}:$PATH\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(uninstall, "_bin_on_user_path", _REAL_BIN_ON_USER_PATH)
    assert uninstall._bin_on_user_path(target) is True
    code, _message = uninstall._remove_user_path_entry(target)
    assert code == 0
    assert "NEO_INSTALLER_PATH" not in profile.read_text(encoding="utf-8")


def test_windows_running_pipx_environment_defers_pipx_uninstall(
    isolated_uninstall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running Windows pipx process schedules pipx removal after exit."""
    venv = isolated_uninstall / ".local" / "pipx" / "venvs" / "neo-agent-cli"
    python = venv / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("fake", encoding="utf-8")
    outside = tmp_path / "base-python.exe"
    outside.write_text("fake", encoding="utf-8")
    pipx = tmp_path / "pipx.exe"
    pipx.write_text("fake", encoding="utf-8")
    calls: List[Tuple[List[str], dict[str, Any]]] = []

    def fake_popen(command: List[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(uninstall, "_windows", lambda: True)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(uninstall, "pipx_venv_dir", lambda: venv)
    monkeypatch.setattr(uninstall, "_outside_python", lambda _venv: outside)
    monkeypatch.setattr(uninstall, "_pipx_executable", lambda: str(pipx))
    monkeypatch.setattr(uninstall.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("running pipx called pipx synchronously"),
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert venv.exists()
    assert calls[0][0][0] == str(outside)
    assert str(pipx) in calls[0][0]
    assert str(venv) in calls[0][0]
    assert "uninstall" in calls[0][0][3]
    assert "neo-agent-cli" in calls[0][0][3]
    assert "WaitForSingleObject" in calls[0][0][3]


def test_all_installer_owned_path_routes_are_cleaned(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent venv and pipx public-bin routes are both removed."""
    pipx_bin = isolated_uninstall / ".local" / "bin"
    monkeypatch.setenv("PIPX_BIN_DIR", str(pipx_bin))
    monkeypatch.setattr(
        uninstall,
        "_bin_on_user_path",
        lambda path: path in (uninstall.installer_bin(), pipx_bin),
    )
    cleaned: List[Path] = []
    monkeypatch.setattr(
        uninstall,
        "_remove_user_path_entry",
        lambda path: cleaned.append(path) or (0, "removed"),
    )

    result = uninstall.cmd_uninstall(_args())

    assert result == 0
    assert cleaned == [uninstall.installer_bin(), pipx_bin]


def test_distribution_metadata_is_detected_from_source_checkout(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source location does not hide a real pip-installed Neo distribution."""
    dist_info = isolated_uninstall / "neo_agent_cli-0.2.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: neo-agent-cli\nVersion: 0.2.1\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(isolated_uninstall))

    monkeypatch.setattr(uninstall, "pip_installed", _REAL_PIP_INSTALLED)
    assert uninstall.pip_distribution_name() == "neo-agent-cli"
    assert uninstall.pip_installed() is True


def test_pipx_venv_is_detected_from_running_interpreter(
    isolated_uninstall: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active pipx interpreter is authoritative even without pipx env vars."""
    venv = isolated_uninstall / ".local" / "pipx" / "venvs" / "neo-agent-cli"
    python = venv / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(python))

    monkeypatch.setattr(uninstall, "pipx_venv_dir", _REAL_PIPX_VENV_DIR)
    assert uninstall.pipx_venv_dir() == venv
