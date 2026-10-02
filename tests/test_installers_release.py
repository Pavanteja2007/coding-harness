import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_NAMES = ("install.ps1", "install.cmd", "install.sh")


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _raw(name: str) -> bytes:
    return (ROOT / name).read_bytes()


def _function(name: str, script: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?(?=^\}})", script)
    assert match, name
    return match.group(0) + "}\n"


def _msys_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _bash_executable() -> str:
    if os.name == "nt":
        candidates = (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Git"
            / "usr"
            / "bin"
            / "bash.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Git"
            / "usr"
            / "bin"
            / "bash.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash") or "bash"


def _isolated_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    tool_dirs: list[str] = []
    for name in ("bash", "cat", "grep", "awk", "cmp", "mkdir", "rm"):
        executable = shutil.which(name)
        if executable:
            parent = _msys_path(Path(executable).parent)
            if parent not in tool_dirs:
                tool_dirs.append(parent)
    bash_executable = _bash_executable()
    if bash_executable != "bash":
        parent = _msys_path(Path(bash_executable).parent)
        if parent not in tool_dirs:
            tool_dirs.append(parent)
    for parent in ("/usr/bin", "/bin"):
        if parent not in tool_dirs:
            tool_dirs.append(parent)
    temp_dir = home / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": _msys_path(home),
        "USERPROFILE": _msys_path(home),
        "APPDATA": _msys_path(home / "appdata"),
        "LOCALAPPDATA": _msys_path(home / "localappdata"),
        "XDG_CONFIG_HOME": _msys_path(home / ".config"),
        "PIPX_HOME": _msys_path(home / ".local" / "pipx"),
        "PIPX_BIN_DIR": _msys_path(home / ".local" / "bin"),
        "PIPX_LOCAL_VENVS": _msys_path(home / ".local" / "venvs"),
        "HARNESS_HOME": _msys_path(home / ".harness"),
        "HARNESS_DECISIONS_DB": _msys_path(home / "decisions.db"),
        "HARNESS_LOGS_DIR": _msys_path(home / "logs"),
        "NEO_CONFIG": _msys_path(home / "neo-settings.toml"),
        "NEO_TRACE_DIR": _msys_path(home / "trace"),
        "NEO_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": _msys_path(temp_dir),
        "TMP": _msys_path(temp_dir),
        "PATH": ":".join(tool_dirs),
    }
    for name in ("SystemRoot", "WINDIR", "ComSpec", "PATHEXT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if extra:
        for name, value in extra.items():
            if any(
                token in name.upper()
                for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            ):
                assert not value, f"credential-like test override is forbidden: {name}"
            env[name] = value
    return env


def _run_bash(script: str, home: Path, extra: dict[str, str] | None = None):
    return subprocess.run(
        [_bash_executable(), "-c", script],
        cwd=ROOT,
        env=_isolated_env(home, extra),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


def _command(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _fake_python(home: Path, pip_installed: bool) -> Path:
    fake_bin = home / "fake-python"
    executable = fake_bin / "python3"
    body = "\n".join(
        (
            "#!/bin/sh",
            'if [ "$1" = "--version" ]; then echo "Python 3.12.0"; exit 0; fi',
            'case "$*" in',
            '  *"(3, 10) <= sys.version_info < (3, 13)"*) exit 0 ;;',
            '  *"print(sys.executable)"*) printf "%s\\n" "$FAKE_PYTHON_PATH" ;;',
            '  *"sys.platform == \\"win32\\""*) exit 1 ;;',
            '  *"pip show neo-agent-cli"*) [ "$FAKE_PIP_INSTALLED" = 1 ] && exit 0; exit 1 ;;',
            '  *"pip uninstall -y neo-agent-cli"*) rm -f "$OLD_NEO" "$OLD_HARNESS"; exit 0 ;;',
            "esac",
            "exit 0",
        )
    )
    _command(executable, body)
    return executable


def _prepare_fake_venv(home: Path) -> None:
    venv_bin = home / ".neo-venv" / "bin"
    _command(venv_bin / "python", "exit 0")
    _command(venv_bin / "neo", 'echo "neo 0.2.1"')
    _command(venv_bin / "harness", 'echo "neo 0.2.1"')


def _prepare_fake_pipx(home: Path) -> Path:
    pipx_bin = home / ".local" / "bin"
    _command(
        pipx_bin / "pipx",
        'if [ "$1" = environment ]; then echo "$PIPX_BIN_DIR"; fi; exit 0',
    )
    _command(pipx_bin / "neo", 'echo "neo 0.2.1"')
    _command(pipx_bin / "harness", 'echo "neo 0.2.1"')
    return pipx_bin / "pipx"


def _installer_env(
    home: Path,
    python: Path,
    extra_path: list[Path] | None = None,
    pip_installed: bool = False,
) -> dict[str, str]:
    env = _isolated_env(home)
    old_bin = home / "old-python-bin"
    env.update(
        {
            "NEO_PYTHON": _msys_path(python),
            "FAKE_PYTHON_PATH": _msys_path(python),
            "FAKE_PIP_INSTALLED": "1" if pip_installed else "0",
            "OLD_NEO": _msys_path(old_bin / "neo"),
            "OLD_HARNESS": _msys_path(old_bin / "harness"),
        }
    )
    prefixes = [_msys_path(path.parent) for path in extra_path or []]
    env["PATH"] = ":".join([*prefixes, env["PATH"]])
    return env


def test_installer_line_endings_are_preserved():
    assert b"\r\n" in _raw("install.ps1")
    assert b"\r\n" in _raw("install.cmd")
    assert b"\r\n" not in _raw("install.sh")
    assert not re.search(rb"(?<!\r)\n", _raw("install.ps1"))
    assert not re.search(rb"(?<!\r)\n", _raw("install.cmd"))


def test_windows_launcher_probes_keep_executable_and_argv_separate():
    powershell = _text("install.ps1")
    cmd = _text("install.cmd")
    assert "Exe = 'py'; Arguments = @('-3')" in powershell
    assert "& $Exe @Arguments -c" in powershell
    assert "Get-Command -Name $Name" in powershell
    assert "Resolve-ApplicationPath $candidate.Exe" in powershell
    assert 'call :check_python "py" "py -3" "-3"' in cmd
    assert "%PY_CMD% %PY_ARGS% -c" in cmd
    assert '"%PY_CMD%" -c "import sys; print(sys.version_info[0]' in cmd
    assert 'Test-PythonVersion "py -3"' not in powershell
    assert 'Get-Command "py -3"' not in powershell


def test_all_routes_expose_both_entry_points_and_check_release_truthfully():
    for name in SCRIPT_NAMES:
        text = _text(name)
        assert "harness" in text
        assert "update --check" in text
        assert "NEO_SKIP_UPDATE_CHECK" in text
        assert "NEO_REQUIRE_UPDATE_CHECK" in text
        assert "NEO_FORCE_VENV" in text
    sh = _text("install.sh")
    assert "run_route_command harness --version" in sh
    assert "elif run_route_command neo update --check" in sh
    assert 'fail "neo update --check failed' in sh
    assert 'warn "neo update --check failed' in sh
    assert 'HARNESS_BIN="$VENV_DIR/bin/harness"' in sh
    assert "NEO_VERSION" in sh
    ps = _text("install.ps1")
    assert "harness.exe" in ps
    assert "& $resolvedHarness --version" in ps
    assert "if ($LASTEXITCODE -ne 0) { Write-Fail 'harness --version" in ps
    assert "& $resolvedNeo update --check" in ps
    assert "Write-Fail 'neo update --check failed" in ps
    assert "Write-Warn2 'neo update --check failed" in ps
    cmd = _text("install.cmd")
    assert "harness.exe" in cmd
    assert '"%RESOLVED_HARNESS%" --version' in cmd
    assert '"%RESOLVED_NEO%" update --check' in cmd
    assert "harness --version failed" in cmd
    assert "neo update --check failed" in cmd


def test_owned_path_routes_are_normalized_deduped_and_ordered(tmp_path: Path):
    sh = _text("install.sh")
    functions = (
        _function("normalize_path_entry", sh)
        + "\n"
        + _function("path_in_list", sh)
        + "\n"
        + _function("build_path", sh)
    )
    intended = "/opt/neo/bin"
    stale = "/opt/old-neo/bin"
    shared = "/home/test/.local/bin"
    command = (
        functions
        + "\nbuild_path "
        + " ".join(
            shlex.quote(value)
            for value in (
                f"{stale}::{shared}:{intended}/:{shared}",
                intended,
                stale,
            )
        )
        + "\n"
    )
    result = _run_bash(command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{intended}:{shared}"


def test_profile_marker_rewrite_is_idempotent(tmp_path: Path):
    sh = _text("install.sh")
    functions = _function("update_profile", sh)
    profile = tmp_path / ".bashrc"
    route = tmp_path / "route"
    line = f"export NEO_INSTALLER_PATH=1; PATH={_msys_path(route)}:$PATH"
    command = "\n".join(
        (
            functions,
            f"PATH_LINE={shlex.quote(line)}",
            "PATH_MARKER=NEO_INSTALLER_PATH",
            f"update_profile {shlex.quote(_msys_path(profile))}",
            f"update_profile {shlex.quote(_msys_path(profile))}",
        )
    )
    result = _run_bash(command, tmp_path)
    assert result.returncode == 0, result.stderr
    content = profile.read_text(encoding="utf-8")
    assert content.count("NEO_INSTALLER_PATH") == 1
    assert content.count(_msys_path(route)) == 1
    assert "\n\n\n" not in content


def test_route_transition_replaces_legacy_profile_and_records_new_route(tmp_path: Path):
    sh = _text("install.sh")
    functions = (
        _function("update_profile", sh) + "\n" + _function("write_route_marker", sh)
    )
    marker_dir = tmp_path / ".neo"
    marker = marker_dir / "install-route"
    profile = tmp_path / ".bashrc"
    profile.write_text(
        "# Added by the Neo installer\n"
        'export PATH="${PIPX_BIN_DIR:-$HOME/.local/bin}:$PATH"\n'
        "export KEEP_ME=1\n",
        encoding="utf-8",
    )
    new_route = "/tmp/neo-bin"
    new_line = f"export NEO_INSTALLER_PATH=1; PATH={new_route}:$PATH"
    command = "\n".join(
        (
            functions,
            f"PATH_LINE={shlex.quote(new_line)}",
            "PATH_MARKER=NEO_INSTALLER_PATH",
            "PATH_COMMENT='# Added by the Neo installer'",
            f"update_profile {shlex.quote(_msys_path(profile))}",
            f"update_profile {shlex.quote(_msys_path(profile))}",
            f"ROUTE_MARKER_DIR={shlex.quote(_msys_path(marker_dir))}",
            f"ROUTE_MARKER_FILE={shlex.quote(_msys_path(marker))}",
            "INSTALLED_WITH=venv",
            f"NEEDS_PATH={shlex.quote(new_route)}",
            "write_route_marker",
            f"cat {shlex.quote(_msys_path(marker))}",
            f"cat {shlex.quote(_msys_path(profile))}",
        )
    )
    result = _run_bash(command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "route=venv" in result.stdout
    assert f"path={new_route}" in result.stdout
    content = profile.read_text(encoding="utf-8")
    assert "PIPX_BIN_DIR" not in content
    assert content.count("NEO_INSTALLER_PATH") == 1
    assert content.count(new_route) == 1
    assert "export KEEP_ME=1" in content


@pytest.mark.parametrize(
    ("name", "arguments", "status"),
    (
        ("neo", "--version", 7),
        ("harness", "--version", 8),
        ("neo", "update --check", 9),
    ),
)
def test_fake_route_command_failure_propagates(
    tmp_path: Path, name: str, arguments: str, status: int
):
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    executable = command_dir / name
    _command(executable, f"exit {status}")
    sh = _text("install.sh")
    function = _function("run_route_command", sh)
    bash_path = _bash_executable()
    if bash_path != "bash":
        bash_path = _msys_path(Path(bash_path))
    script = "\n".join(
        (
            "set -eu",
            function,
            f"CURRENT_PATH={shlex.quote(_msys_path(command_dir))}",
            f"ROUTE_SHELL={shlex.quote(bash_path)}",
            f"run_route_command {name} {arguments}",
        )
    )
    result = _run_bash(script, tmp_path)
    assert result.returncode == status


def test_windows_routes_migrate_stale_pips_and_verify_fresh_processes():
    powershell = _text("install.ps1")
    cmd = _text("install.cmd")
    for text in (powershell, cmd):
        assert "install-route" in text
        assert "route=" in text
        assert "NEO_PERSIST_REMOVE" in text or "persistRemovePaths" in text
        assert "where neo" in text or "Resolve-RouteCommand" in text
        assert "where harness" in text or "resolvedHarness" in text
        assert "pip uninstall" in text
        assert "fresh" in text.lower()
    assert "New-Item -ItemType Directory -Path $RouteMarkerDir" in powershell
    assert "pipxVenvs" in powershell
    assert "%SystemRoot%\\System32\\WindowsPowerShell" in cmd
    assert "NEO_PIPX_HOME" in cmd
    assert "remove_stale_pip" in cmd


def test_windows_fresh_process_checks_resolve_the_intended_executables():
    powershell = _text("install.ps1")
    cmd = _text("install.cmd")
    assert "NEO_EXPECTED_NEO_BIN" in powershell
    assert "NEO_EXPECTED_HARNESS_BIN" in powershell
    assert (
        "if ($neo.Source.ToLowerInvariant() -ne $env:NEO_EXPECTED_NEO_BIN" in powershell
    )
    assert "$v -ine $e" in cmd
    assert "$h2 -ine $h" in cmd
    assert "exit 51" in cmd
    assert "exit 52" in cmd


def test_disposable_pipx_to_venv_transition_keeps_both_commands_on_fresh_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    python = _fake_python(home, pip_installed=False)
    pipx = _prepare_fake_pipx(home)
    _prepare_fake_venv(home)

    first_env = _installer_env(home, python, [pipx])
    first = _run_bash("source ./install.sh", home, first_env)
    assert first.returncode == 0, first.stderr
    assert "route=pipx" in (home / ".neo" / "install-route").read_text(encoding="utf-8")

    second_env = _installer_env(home, python)
    second = _run_bash("source ./install.sh", home, second_env)
    assert second.returncode == 0, second.stderr
    marker = (home / ".neo" / "install-route").read_text(encoding="utf-8")
    assert "route=venv" in marker
    profile = (home / ".bashrc").read_text(encoding="utf-8")
    assert _msys_path(home / ".neo" / "bin") in profile
    assert _msys_path(home / ".local" / "bin") not in profile
    assert (home / ".neo" / "bin" / "neo").exists()
    assert (home / ".neo" / "bin" / "harness").exists()


def test_disposable_pip_to_venv_transition_removes_stale_commands(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    python = _fake_python(home, pip_installed=True)
    old_bin = home / "old-python-bin"
    old_neo = old_bin / "neo"
    old_harness = old_bin / "harness"
    _command(old_neo, "exit 99")
    _command(old_harness, "exit 99")
    _prepare_fake_venv(home)

    env = _installer_env(home, python, [old_neo, old_harness], pip_installed=True)
    result = _run_bash("source ./install.sh", home, env)

    assert result.returncode == 0, result.stderr
    assert not old_neo.exists()
    assert not old_harness.exists()
    assert (home / ".neo" / "bin" / "neo").exists()
    assert (home / ".neo" / "bin" / "harness").exists()


def test_stale_pip_cleanup_failure_cannot_be_masked_by_route_filtering():
    powershell = _text("install.ps1")
    cmd = _text("install.cmd")
    sh = _text("install.sh")

    assert "$script:NeoStalePipCleanupFailed = $true" in powershell
    assert "if ($script:NeoStalePipCleanupFailed)" in powershell
    assert "if ($systemScriptDir) { $stalePaths" not in powershell
    assert '$freshPath = "$machinePath;$newUserPath"' in powershell
    assert "NEO_STALE_FAILURE" in cmd
    assert "if defined NEO_STALE_FAILURE" in cmd
    assert "NEO_VERIFY_REMOVE" not in cmd
    assert '$fresh="$machine;$stored"' in cmd
    assert 'STALE_PATHS+=("$SYSTEM_SCRIPT_DIR")' not in sh
    assert '"$SYSTEM_PYTHON" -m pip uninstall -y "$PYPI_SPEC"' in sh


def test_posix_fresh_check_uses_the_users_login_shell_and_profile():
    sh = _text("install.sh")
    assert 'LOGIN_SHELL="${SHELL:-$ROUTE_SHELL}"' in sh
    assert 'FRESH_SHELL_NAME="$(basename "$LOGIN_SHELL")"' in sh
    assert 'SHELL="$LOGIN_SHELL"' in sh
    assert '"$LOGIN_SHELL" "${FRESH_ARGS[@]}"' in sh
    assert '"${PERSIST_REMOVE_PATHS[@]}" "$NEEDS_PATH"' in sh


def test_preflight_contracts_remain_explicit():
    sh = _text("install.sh")
    assert "(3, 10) <= sys.version_info < (3, 13)" in sh
    assert "git+*" in sh
    assert "Docker not found" in sh
    ps = _text("install.ps1")
    assert "major -eq 3 -and $minor -ge 10 -and $minor -le 12" in ps
    assert "-like 'git+*'" in ps
    assert "Docker not found" in ps
    cmd = _text("install.cmd")
    assert "if !PY_MAJ! LSS 3" in cmd
    assert "if !PY_MIN! GEQ 13" in cmd
    assert 'findstr /b /l /c:"git+"' in cmd
    assert "Docker not found" in cmd


def test_powershell_parser_accepts_installer_when_available(tmp_path: Path):
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is unavailable")
    parser = (
        "$t=$null;$e=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "(Resolve-Path 'install.ps1'),[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){$e|ForEach-Object { $_.Message };exit 1}"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", parser],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_bash_syntax_is_valid_when_bash_is_available(tmp_path: Path):
    executable = _bash_executable()
    if executable == "bash" and not shutil.which("bash"):
        pytest.skip("Bash is unavailable")
    result = subprocess.run(
        [executable, "-n", "install.sh"],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cmd_labels_are_present_and_unique_without_execution():
    text = _text("install.cmd")
    labels = re.findall(r"(?m)^:([A-Za-z_][A-Za-z0-9_-]*)\r?$", text)
    assert {"py_found", "check_python", "create_venv", "remove_stale_pip"} <= set(
        labels
    )
    assert len(labels) == len(set(labels))
    assert text.endswith("\n")
