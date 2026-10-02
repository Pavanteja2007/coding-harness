"""Run isolated wheel and sdist release checks across supported Python runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


SCHEMA_VERSION = 1
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CHECK_NAMES = (
    "create_venv",
    "install_artifact",
    "pip_check",
    "import_packages",
    "sdk_smoke",
    "neo_version",
    "harness_version",
    "module_version",
    "help",
    "path_resolution",
    "login",
    "login_state",
    "logout",
    "logout_state",
    "packaged_smoke",
    "docker_daemon",
    "fixture_copy",
    "real_fix",
    "fix_trace",
    "git_output",
    "source_fixture_unchanged",
    "packaged_fixture_unchanged",
    "original_fixture_unchanged",
    "update_check",
    "uninstall",
    "uninstall_complete",
)
_ENV_ALLOWLIST = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}
_VALIDATOR = Callable[[str, str], Optional[str]]


class MatrixError(RuntimeError):
    """Raised when the release matrix cannot be started safely."""


class MatrixUsageError(MatrixError):
    """Raised when matrix arguments are invalid."""


class _ArgumentParser(argparse.ArgumentParser):
    """Parser that routes usage failures through machine output."""

    def error(self, message: str) -> None:
        raise MatrixUsageError(message)


def _command_text(command: list[str]) -> str:
    """Render one argv list as an exact shell-style command for reports."""
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _plain(value: str) -> str:
    """Remove terminal control sequences from captured output."""
    return _ANSI_RE.sub("", value)


def _tree_sha256(root: Path) -> str:
    """Hash source fixture files while excluding generated Python caches."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            payload = handle.read()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest for one release artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_project(project_root: Path) -> tuple[str, list[str]]:
    """Load the expected version and top-level configured packages."""
    with (project_root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project")
    tools = data.get("tool")
    if not isinstance(project, dict) or not isinstance(tools, dict):
        raise MatrixError("pyproject.toml is missing project or tool tables")
    version = project.get("version")
    setuptools = tools.get("setuptools")
    if not isinstance(version, str) or not isinstance(setuptools, dict):
        raise MatrixError("pyproject.toml is missing release identity")
    packages = setuptools.get("packages")
    if not isinstance(packages, list) or not packages:
        raise MatrixError("pyproject.toml has no configured packages")
    names = sorted({str(package).split(".", 1)[0] for package in packages})
    return version, names


def _parse_python(value: str) -> tuple[str, Path]:
    """Parse one LABEL=PATH Python runtime argument."""
    label, separator, raw_path = value.partition("=")
    if not separator or not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("Python runtime must be LABEL=PATH")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Python runtime does not exist: {path}")
    return label.strip(), path


def _venv_command(venv_root: Path, name: str) -> Path:
    """Return the platform-specific command path inside one venv."""
    if os.name == "nt":
        return venv_root / "Scripts" / f"{name}.exe"
    return venv_root / "bin" / name


def _base_environment(
    output_root: Path,
    lane_root: Path,
    workdir: Path,
    venv_root: Optional[Path] = None,
) -> dict[str, str]:
    """Build a credential-free environment for one clean-room lane."""
    env = {
        key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST
    }
    home = lane_root / "home"
    appdata = lane_root / "appdata"
    localappdata = lane_root / "localappdata"
    temp = lane_root / "temp"
    logs = lane_root / "logs"
    config_home = lane_root / "config"
    for directory in (home, appdata, localappdata, temp, logs, config_home, workdir):
        directory.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "APPDATA": str(appdata),
            "HOME": str(home),
            "HARNESS_DECISIONS_DB": str(lane_root / "decisions.sqlite3"),
            "HARNESS_LOGS_DIR": str(logs),
            "HARNESS_SANDBOX_BASE_IMAGE": "python:3.10-slim",
            "LOCALAPPDATA": str(localappdata),
            "NO_COLOR": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INPUT": "1",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(temp),
            "TMP": str(temp),
            "USERPROFILE": str(home),
            "NEO_CONFIG": str(config_home / "settings.toml"),
            "NEO_TRACE_DIR": str(lane_root / "trace-overlay"),
        }
    )
    if os.name == "nt":
        env["XDG_CONFIG_HOME"] = str(config_home)
    else:
        env["XDG_CONFIG_HOME"] = str(config_home)
    if venv_root is not None:
        env["VIRTUAL_ENV"] = str(venv_root)
        bin_dir = venv_root / "Scripts" if os.name == "nt" else venv_root / "bin"
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")]).rstrip(
            os.pathsep
        )
    return env


def _default_validator(name: str, expected: str) -> _VALIDATOR:
    """Build a validator for one exact-output command."""

    def validate(stdout: str, stderr: str) -> Optional[str]:
        """Return an error when captured output differs from the contract."""
        del stderr
        actual = _plain(stdout).strip()
        if actual == expected:
            return None
        return f"{name} output {actual!r}, expected {expected!r}"

    return validate


def _json_success(stdout: str, stderr: str) -> Optional[str]:
    """Require one clean JSON result document with successful status."""
    del stderr
    text = _plain(stdout).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"fix --json output is not one JSON document: {exc}"
    if not isinstance(payload, dict):
        return "fix --json output is not an object"
    if payload.get("status") != "success" or payload.get("exit_code") != 0:
        return f"fix result is not successful: {payload!r}"
    return None


def _smoke_success(stdout: str, stderr: str) -> Optional[str]:
    """Require the packaged smoke benchmark to report one success."""
    combined = _plain(f"{stdout}\n{stderr}").casefold()
    if "success 1 / 1" in combined or "success 1/1" in combined:
        return None
    return "packaged smoke benchmark did not report success 1 / 1"


def _login_success(stdout: str, stderr: str) -> Optional[str]:
    """Require the installed login command to save an explicit profile."""
    combined = _plain(f"{stdout}\n{stderr}")
    return None if "login saved" in combined else "login did not report a saved profile"


def _logout_success(stdout: str, stderr: str) -> Optional[str]:
    """Require the installed logout command to complete."""
    combined = _plain(f"{stdout}\n{stderr}")
    if "logged out" in combined or "no persisted api_key" in combined:
        return None
    return "logout did not report completion"


def _path_resolution(stdout: str, stderr: str) -> Optional[str]:
    """Require both installed command names to resolve from the lane PATH."""
    del stderr
    try:
        payload = json.loads(_plain(stdout).strip())
    except json.JSONDecodeError as exc:
        return f"PATH resolution output is not JSON: {exc}"
    if not isinstance(payload, dict):
        return "PATH resolution output is not an object"
    for name in ("neo", "harness"):
        value = payload.get(name)
        if not isinstance(value, str) or not Path(value).is_file():
            return f"PATH did not resolve {name}: {value!r}"
    return None


def _trace_events(log_root: Path) -> list[dict[str, Any]]:
    """Read all valid trace events under one log root."""
    events: list[dict[str, Any]] = []
    for path in sorted(log_root.rglob("trace.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _verify_fix_trace(log_root: Path) -> dict[str, Any]:
    """Return baseline, final-verifier, and terminal success evidence."""
    events = _trace_events(log_root)
    baseline = next(
        (event for event in reversed(events) if event.get("kind") == "baseline_verify"),
        None,
    )
    final = next(
        (event for event in reversed(events) if event.get("kind") == "final_verify"),
        None,
    )
    task_end = next(
        (event for event in reversed(events) if event.get("kind") == "task_end"), None
    )
    result = next(
        (event for event in reversed(events) if event.get("kind") == "result"), None
    )
    baseline_data = baseline.get("data", {}) if baseline else {}
    final_data = final.get("data", {}) if final else {}
    task_end_data = task_end.get("data", {}) if task_end else {}
    result_data = result.get("data", {}) if result else {}
    evidence = {
        "baseline_target_passed": baseline_data.get("target_passed_on_pristine"),
        "final_target_passed": final_data.get("target_passed"),
        "final_regression_passed": final_data.get("regression_passed"),
        "final_flaky": final_data.get("flaky"),
        "task_end_status": task_end_data.get("status"),
        "result_status": result_data.get("status"),
    }
    passed = (
        evidence["baseline_target_passed"] is False
        and evidence["final_target_passed"] is True
        and evidence["final_regression_passed"] is True
        and evidence["final_flaky"] is False
        and evidence["task_end_status"] in {"success", "completed_verified"}
        and evidence["result_status"] == "success"
    )
    return {"passed": passed, "evidence": evidence}


def _verify_git_output(log_root: Path) -> dict[str, Any]:
    """Require matching trace and git.json evidence from the installed fix."""
    events = _trace_events(log_root)
    event = next(
        (item for item in reversed(events) if item.get("kind") == "git_output"), None
    )
    data = event.get("data", {}) if event else {}
    files = sorted(log_root.rglob("git.json"))
    payload: dict[str, Any] = {}
    if files:
        try:
            value = json.loads(files[0].read_text(encoding="utf-8"))
            if isinstance(value, dict):
                payload = value
        except (OSError, json.JSONDecodeError):
            payload = {}
    branch = payload.get("branch")
    commit = payload.get("commit_sha")
    passed = (
        isinstance(branch, str)
        and branch.startswith("harness/fix-")
        and isinstance(commit, str)
        and bool(re.fullmatch(r"[0-9a-f]{40,64}", commit))
        and data.get("branch") == branch
        and data.get("commit_sha") == commit
    )
    return {
        "passed": passed,
        "git_json_files": [str(path) for path in files],
        "branch": branch,
        "commit_sha": commit,
    }


def _scripted_spec() -> dict[str, Any]:
    """Return a deterministic two-step model script for the packaged fix."""
    command = (
        "python -c \"from pathlib import Path; p=Path('mathutil.py'); "
        "text=p.read_text(); old='return sum(values)'; assert old in text; "
        "p.write_text(text.replace(old, 'return sum(values) / len(values)', 1))\""
    )
    verify = "python -m pytest -q tests/test_mathutil.py"
    return {
        "plan": [
            {
                "id": 1,
                "description": "Update mean() to divide the sum by the number of values.",
                "checkpoint": "mathutil.mean returns the arithmetic mean.",
                "files_hint": ["mathutil.py"],
            },
            {
                "id": 2,
                "description": "Run the focused math utility tests.",
                "checkpoint": "The focused test file passes.",
                "files_hint": ["tests/test_mathutil.py"],
            },
        ],
        "scripts": {
            "1": [[command, "SUBMIT"]],
            "2": [[verify, "SUBMIT"]],
        },
    }


def _settings_snapshot(env: dict[str, str]) -> dict[str, Any]:
    """Read selected non-secret settings fields without returning credentials."""
    path = Path(env["NEO_CONFIG"])
    if not path.is_file():
        raise MatrixError(f"settings file was not created: {path}")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MatrixError(f"settings file is invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise MatrixError("settings file is not a table")
    return {
        "path": str(path),
        "model": data.get("model"),
        "api_key_present": bool(data.get("api_key")),
    }


class _Lane:
    """Collect exact evidence for one Python and artifact combination."""

    def __init__(
        self,
        label: str,
        python_label: str,
        python_path: Path,
        artifact: Path,
        artifact_kind: str,
        output_root: Path,
        project_root: Path,
        expected_version: str,
        package_names: list[str],
    ) -> None:
        self.label = label
        self.python_label = python_label
        self.python_path = python_path
        self.artifact = artifact
        self.artifact_kind = artifact_kind
        self.artifact_sha256 = _sha256(artifact)
        self.output_root = output_root
        self.project_root = project_root
        self.expected_version = expected_version
        self.package_names = package_names
        self.root = output_root / "lanes" / label
        self.workdir = self.root / "work"
        self.venv_root = self.root / "venv"
        self.venv_python = _venv_command(self.venv_root, "python")
        self.neo = _venv_command(self.venv_root, "neo")
        self.harness = _venv_command(self.venv_root, "harness")
        self.checks: list[dict[str, Any]] = []
        self.hashes_before: dict[str, str] = {}
        self.hashes_after: dict[str, str] = {}
        self.status = "blocked"
        self.error: Optional[str] = None

    def environment(self) -> dict[str, str]:
        """Return the isolated environment for this lane."""
        return _base_environment(
            self.output_root,
            self.root,
            self.workdir,
            self.venv_root,
        )

    def command(
        self,
        name: str,
        command: list[str],
        timeout: int = 600,
        env: Optional[dict[str, str]] = None,
        validator: Optional[_VALIDATOR] = None,
        cwd: Optional[Path] = None,
    ) -> bool:
        """Run and record one subprocess-backed lane check."""
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd or self.workdir),
                env=env or self.environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            exit_code: Optional[int] = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            validation_error = None
            if exit_code == 0 and validator is not None:
                validation_error = validator(stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            validation_error = f"timed out after {timeout}s"
        except OSError as exc:
            exit_code = 127
            stdout = ""
            stderr = str(exc)
            validation_error = None
        passed = exit_code == 0 and validation_error is None
        self.checks.append(
            {
                "name": name,
                "status": "pass" if passed else "fail",
                "command": command,
                "command_text": _command_text(command),
                "cwd": str((cwd or self.workdir).resolve()),
                "exit_code": exit_code,
                "duration_s": round(time.monotonic() - started, 3),
                "validation_error": validation_error,
                "stdout": stdout[-8000:],
                "stderr": stderr[-8000:],
            }
        )
        return passed

    def internal(
        self,
        name: str,
        command: list[str],
        operation: Callable[[], Any],
    ) -> bool:
        """Run and record one in-process filesystem or evidence assertion."""
        started = time.monotonic()
        try:
            details = operation()
        except Exception as exc:
            self.checks.append(
                {
                    "name": name,
                    "status": "fail",
                    "command": command,
                    "command_text": _command_text(command),
                    "cwd": str(self.root.resolve()),
                    "exit_code": 1,
                    "duration_s": round(time.monotonic() - started, 3),
                    "validation_error": f"{type(exc).__name__}: {exc}",
                    "stdout": "",
                    "stderr": "",
                }
            )
            return False
        self.checks.append(
            {
                "name": name,
                "status": "pass",
                "command": command,
                "command_text": _command_text(command),
                "cwd": str(self.root.resolve()),
                "exit_code": 0,
                "duration_s": round(time.monotonic() - started, 3),
                "validation_error": None,
                "stdout": json.dumps(details, sort_keys=True)
                if details is not None
                else "",
                "stderr": "",
            }
        )
        return True

    def mark_blocked(self, name: str, reason: str) -> None:
        """Record one required check that could not run."""
        self.checks.append(
            {
                "name": name,
                "status": "blocked",
                "command": None,
                "command_text": None,
                "cwd": str(self.root.resolve()),
                "exit_code": None,
                "duration_s": 0.0,
                "validation_error": reason,
                "stdout": "",
                "stderr": "",
            }
        )

    def block_remaining(self, current: str, reason: str) -> None:
        """Block every check after a required environmental prerequisite."""
        start = _CHECK_NAMES.index(current)
        for name in _CHECK_NAMES[start:]:
            if not any(check["name"] == name for check in self.checks):
                self.mark_blocked(name, reason)

    def run(self) -> dict[str, Any]:
        """Execute this complete lane and return its machine-readable report."""
        started = time.monotonic()
        if self.root.exists():
            raise MatrixError(
                f"lane root already exists; use a fresh output root: {self.root}"
            )
        self.root.mkdir(parents=True)
        self.workdir.mkdir(parents=True)
        try:
            if not self.command(
                "create_venv",
                [str(self.python_path), "-m", "venv", str(self.venv_root)],
                timeout=300,
            ):
                self.status = "fail"
                self.error = "venv creation failed"
                self.block_remaining("create_venv", self.error)
                return self.report(started)
            env = self.environment()
            if not self.command(
                "install_artifact",
                [
                    str(self.venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--timeout",
                    "120",
                    "--retries",
                    "8",
                    str(self.artifact),
                ],
                timeout=1800,
                env=env,
            ):
                self.status = "fail"
                self.error = "artifact installation failed"
                self.block_remaining("install_artifact", self.error)
                return self.report(started)
            if not self.command(
                "pip_check",
                [str(self.venv_python), "-m", "pip", "check"],
                timeout=120,
                env=env,
            ):
                self.status = "fail"
                self.error = "pip check failed"
                self.block_remaining("pip_check", self.error)
                return self.report(started)
            import_code = (
                "import importlib, importlib.metadata as metadata, json, pathlib, sys; "
                f"names={self.package_names!r}; expected={self.expected_version!r}; "
                "venv=pathlib.Path(sys.prefix).resolve(); "
                "rows={name: str(pathlib.Path(importlib.import_module(name).__file__).resolve()) "
                "for name in names}; "
                "assert all(pathlib.Path(path).is_relative_to(venv) for path in rows.values()), rows; "
                "version=metadata.version('neo-agent-cli'); assert version == expected, (version, expected); "
                "print(json.dumps({'version': version, 'modules': rows}, sort_keys=True))"
            )
            if not self.command(
                "import_packages",
                [str(self.venv_python), "-I", "-c", import_code],
                timeout=180,
                env=env,
            ):
                self.status = "fail"
                self.error = "installed package import/origin check failed"
                self.block_remaining("import_packages", self.error)
                return self.report(started)
            sdk_smoke_code = """
import json
from pathlib import Path
import tempfile
from agent_sdk import LocalAgent

def finish_model(messages, **kwargs):
    return json.dumps({"tool": "finish", "answer": "clean-room-sdk-ok"})

with tempfile.TemporaryDirectory(prefix="neo-clean-sdk-") as temporary:
    root = Path(temporary)
    repo = root / "repo"
    logs = root / "logs"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\\n", encoding="utf-8")
    agent = LocalAgent(
        repo,
        log_root=logs,
        model=finish_model,
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    try:
        result = agent.query("Return the clean-room SDK answer")
        replay = agent.events(result.run_id).replay()
        rows = list(replay)
        assert result.status == "completed_unverified"
        assert result.answer == "clean-room-sdk-ok"
        assert result.schema_version == 1
        assert replay.final_status == "completed_unverified"
        assert [event.sequence for event in rows] == list(range(1, len(rows) + 1))
        assert Path(result.trace_path).resolve().is_relative_to(logs.resolve())
    finally:
        agent.close()
print(json.dumps({"answer": result.answer, "events": len(rows), "status": result.status}, sort_keys=True))
"""
            if not self.command(
                "sdk_smoke",
                [str(self.venv_python), "-I", "-B", "-c", sdk_smoke_code],
                timeout=300,
                env=env,
            ):
                self.status = "fail"
                self.error = "installed agent_sdk functional smoke failed"
                self.block_remaining("sdk_smoke", self.error)
                return self.report(started)
            version_output = f"neo {self.expected_version}"
            if not self.command(
                "neo_version",
                [str(self.neo), "--version"],
                timeout=60,
                env=env,
                validator=_default_validator("neo --version", version_output),
            ):
                self.status = "fail"
                self.error = "neo --version contract failed"
                self.block_remaining("neo_version", self.error)
                return self.report(started)
            if not self.command(
                "harness_version",
                [str(self.harness), "--version"],
                timeout=60,
                env=env,
                validator=_default_validator("harness --version", version_output),
            ):
                self.status = "fail"
                self.error = "harness --version contract failed"
                self.block_remaining("harness_version", self.error)
                return self.report(started)
            if not self.command(
                "module_version",
                [str(self.venv_python), "-I", "-m", "cli", "--version"],
                timeout=60,
                env=env,
                validator=_default_validator("python -m cli --version", version_output),
            ):
                self.status = "fail"
                self.error = "python -m cli --version contract failed"
                self.block_remaining("module_version", self.error)
                return self.report(started)

            def validate_help(stdout: str, stderr: str) -> Optional[str]:
                """Require representative public commands in help output."""
                del stderr
                text = _plain(stdout)
                missing = [
                    word
                    for word in ("fix", "run-benchmark", "update", "uninstall")
                    if word not in text
                ]
                return f"help output missing {missing}" if missing else None

            if not self.command(
                "help",
                [str(self.neo), "--help"],
                timeout=60,
                env=env,
                validator=validate_help,
            ):
                self.status = "fail"
                self.error = "neo --help contract failed"
                self.block_remaining("help", self.error)
                return self.report(started)
            if not self.command(
                "path_resolution",
                [
                    str(self.venv_python),
                    "-I",
                    "-c",
                    (
                        "import json,shutil;"
                        "print(json.dumps({name:shutil.which(name) "
                        "for name in ('neo','harness')},sort_keys=True))"
                    ),
                ],
                timeout=60,
                env=env,
                validator=_path_resolution,
            ):
                self.status = "fail"
                self.error = "installed command PATH resolution failed"
                self.block_remaining("path_resolution", self.error)
                return self.report(started)
            auth_env = self.environment()
            auth_env["NEO_API_KEY"] = "release-check-not-a-credential"
            if not self.command(
                "login",
                [
                    str(self.neo),
                    "login",
                    "--tier",
                    "global",
                    "--provider",
                    "openai",
                    "--model",
                    "release-check-model",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--no-health-check",
                ],
                timeout=60,
                env=auth_env,
                validator=_login_success,
            ):
                self.status = "fail"
                self.error = "installed login failed"
                self.block_remaining("login", self.error)
                return self.report(started)
            if not self.internal(
                "login_state",
                [str(self.neo), "config", "get", "model"],
                lambda: _settings_snapshot(auth_env),
            ):
                self.status = "fail"
                self.error = "installed login did not persist isolated settings"
                self.block_remaining("login_state", self.error)
                return self.report(started)
            login_state = self.checks[-1]
            if '"api_key_present": true' not in login_state["stdout"]:
                self.checks[-1]["status"] = "fail"
                self.status = "fail"
                self.error = "installed login did not persist the isolated credential"
                self.block_remaining("login_state", self.error)
                return self.report(started)
            if not self.command(
                "logout",
                [str(self.neo), "logout"],
                timeout=60,
                env=auth_env,
                validator=_logout_success,
            ):
                self.status = "fail"
                self.error = "installed logout failed"
                self.block_remaining("logout", self.error)
                return self.report(started)
            if not self.internal(
                "logout_state",
                [str(self.neo), "config", "get", "model"],
                lambda: _settings_snapshot(auth_env),
            ):
                self.status = "fail"
                self.error = "installed logout state could not be read"
                self.block_remaining("logout_state", self.error)
                return self.report(started)
            logout_state = self.checks[-1]
            if (
                '"api_key_present": false' not in logout_state["stdout"]
                or '"model": "release-check-model"' not in logout_state["stdout"]
            ):
                self.checks[-1]["status"] = "fail"
                self.status = "fail"
                self.error = "installed logout did not remove only the credential"
                self.block_remaining("logout_state", self.error)
                return self.report(started)
            smoke_root = self.root / "smoke-logs"
            if not self.command(
                "packaged_smoke",
                [
                    str(self.neo),
                    "run-benchmark",
                    "--subset",
                    "smoke",
                    "--concurrency",
                    "1",
                    "--log-root",
                    str(smoke_root),
                ],
                timeout=300,
                env=env,
                validator=_smoke_success,
            ):
                self.status = "fail"
                self.error = "packaged smoke benchmark failed"
                self.block_remaining("packaged_smoke", self.error)
                return self.report(started)
            docker = shutil.which("docker")
            if not self.command(
                "docker_daemon",
                [docker or "docker", "info", "--format", "{{.ServerVersion}}"],
                timeout=120,
                env=env,
            ):
                self.checks[-1]["status"] = "blocked"
                self.status = "blocked"
                self.error = "Docker daemon unavailable"
                self.block_remaining("docker_daemon", self.error)
                return self.report(started)
            package_fixture, copied_fixture, source_fixture = self._fixture_paths()
            self.package_fixture = package_fixture
            self.copied_fixture = copied_fixture
            self.source_fixture = source_fixture

            def copy_fixture() -> dict[str, str]:
                """Copy the packaged fixture and capture all baseline hashes."""
                if not package_fixture.is_dir():
                    raise FileNotFoundError(
                        f"packaged fixture missing: {package_fixture}"
                    )
                if not source_fixture.is_dir():
                    raise FileNotFoundError(f"source fixture missing: {source_fixture}")
                shutil.copytree(package_fixture, copied_fixture)
                hashes = {
                    "source": _tree_sha256(source_fixture),
                    "packaged": _tree_sha256(package_fixture),
                    "original_copy": _tree_sha256(copied_fixture),
                }
                self.hashes_before = hashes
                return hashes

            if not self.internal(
                "fixture_copy",
                [
                    "internal:copy_package_fixture",
                    str(package_fixture),
                    str(copied_fixture),
                ],
                copy_fixture,
            ):
                self.status = "fail"
                self.error = "fixture copy/hash failed"
                self.block_remaining("fixture_copy", self.error)
                return self.report(started)
            spec_path = self.root / "scripted-model.json"
            spec_path.write_text(
                json.dumps(_scripted_spec(), indent=2) + "\n", encoding="utf-8"
            )
            fix_logs = self.root / "fix-logs"
            fix_env = self.environment()
            fix_env.update(
                {
                    "HARNESS_SCRIPTED_MODEL": str(spec_path),
                    "NEO_API_KEY": "",
                    "NEO_MODEL": "scripted-release",
                }
            )
            if not self.command(
                "real_fix",
                [
                    str(self.neo),
                    "fix",
                    "--repo",
                    str(copied_fixture),
                    "--issue",
                    "Fix mean() so it returns the arithmetic mean.",
                    "--target-test",
                    "tests/test_mathutil.py::test_mean",
                    "--max-retries",
                    "1",
                    "--log-root",
                    str(fix_logs),
                    "--model",
                    "scripted-release",
                    "--json",
                    "--no-color",
                ],
                timeout=900,
                env=fix_env,
                validator=_json_success,
            ):
                self.status = "fail"
                self.error = "real Docker-backed fix failed"
                self.block_remaining("real_fix", self.error)
                return self.report(started)

            def verify_trace() -> dict[str, Any]:
                """Require baseline failure and final verifier success evidence."""
                details = _verify_fix_trace(fix_logs)
                if not details["passed"]:
                    raise MatrixError(
                        f"fix trace evidence failed: {details['evidence']}"
                    )
                return details

            if not self.internal(
                "fix_trace",
                ["internal:verify_fix_trace", str(fix_logs)],
                verify_trace,
            ):
                self.status = "fail"
                self.error = "fix trace evidence failed"
                self.block_remaining("fix_trace", self.error)
                return self.report(started)

            def verify_git_output() -> dict[str, Any]:
                """Require branch, commit, trace, and git.json evidence."""
                details = _verify_git_output(fix_logs)
                if not details["passed"]:
                    raise MatrixError(f"git-native output evidence failed: {details}")
                return details

            if not self.internal(
                "git_output",
                ["internal:verify_git_output", str(fix_logs)],
                verify_git_output,
            ):
                self.status = "fail"
                self.error = "git-native output evidence failed"
                self.block_remaining("git_output", self.error)
                return self.report(started)

            fixture_paths = {
                "source": source_fixture,
                "packaged": package_fixture,
                "original_copy": copied_fixture,
            }
            self.hashes_after = {
                key: _tree_sha256(path) for key, path in fixture_paths.items()
            }
            for name, key in (
                ("source_fixture_unchanged", "source"),
                ("packaged_fixture_unchanged", "packaged"),
                ("original_fixture_unchanged", "original_copy"),
            ):

                def verify_fixture(
                    key: str = key, path: Path = fixture_paths[key]
                ) -> dict[str, str]:
                    """Capture and compare one post-fix fixture hash."""
                    current = _tree_sha256(path)
                    if current != self.hashes_before[key]:
                        raise MatrixError(
                            f"{key} fixture changed: {self.hashes_before[key]} -> {current}"
                        )
                    return {
                        "before": self.hashes_before[key],
                        "after": current,
                        "path": str(path),
                    }

                if not self.internal(
                    name,
                    ["internal:verify_unchanged_tree_sha256", str(fixture_paths[key])],
                    verify_fixture,
                ):
                    self.status = "fail"
                    self.error = "fixture integrity failed"
                    self.block_remaining(name, self.error)
                    return self.report(started)
            if not self.command(
                "update_check",
                [str(self.neo), "update", "--check"],
                timeout=120,
                env=self.environment(),
            ):
                if self.checks[-1]["exit_code"] == 4:
                    self.checks[-1]["status"] = "blocked"
                    self.status = "blocked"
                    self.error = (
                        "installed update check could not reach release metadata"
                    )
                    self.block_remaining("uninstall", self.error)
                else:
                    self.status = "fail"
                    self.error = "installed update check failed"
                    self.block_remaining("update_check", self.error)
                return self.report(started)
            if not self.command(
                "uninstall",
                [str(self.neo), "uninstall", "--yes"],
                timeout=300,
                env=self.environment(),
            ):
                self.status = "fail"
                self.error = "installed uninstall failed"
                self.block_remaining("uninstall", self.error)
                return self.report(started)

            def verify_uninstalled() -> dict[str, Any]:
                """Wait for deferred Windows removal, then prove metadata is gone."""
                receipt_payload = None
                if os.name == "nt":
                    uninstall_output = _plain(self.checks[-1]["stdout"]).casefold()
                    if (
                        "scheduled" not in uninstall_output
                        or "external" not in uninstall_output
                        or "interpreter" not in uninstall_output
                    ):
                        raise MatrixError(
                            "Windows uninstall did not report a safe external helper"
                        )
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        receipts = list(
                            (self.root / "temp").glob("neo-uninstall-*.json")
                        )
                        if receipts:
                            try:
                                value = json.loads(
                                    receipts[0].read_text(encoding="utf-8")
                                )
                            except (OSError, json.JSONDecodeError):
                                value = None
                            if isinstance(value, dict):
                                receipt_payload = value
                                if value.get("status") in {"succeeded", "failed"}:
                                    break
                        time.sleep(0.25)
                    if not isinstance(receipt_payload, dict):
                        raise MatrixError("external uninstall receipt was not created")
                    if receipt_payload.get("status") != "succeeded":
                        raise MatrixError(
                            "external uninstall failed: "
                            f"{receipt_payload.get('error', 'unknown error')}"
                        )
                    if (
                        Path(receipt_payload["launcher_path"]).resolve()
                        != self.neo.resolve()
                    ):
                        raise MatrixError(
                            "external helper waited on the wrong launcher"
                        )
                    helper = Path(receipt_payload["helper_interpreter"]).resolve()
                    if helper.is_relative_to(self.venv_root.resolve()):
                        raise MatrixError(
                            "external helper reused the active venv interpreter"
                        )
                    if (
                        Path(receipt_payload["target_interpreter"]).resolve()
                        != self.venv_python.resolve()
                    ):
                        raise MatrixError(
                            "external helper used the wrong target interpreter"
                        )
                deadline = time.monotonic() + 30
                while (
                    self.neo.exists() or self.harness.exists()
                ) and time.monotonic() < deadline:
                    time.sleep(0.25)
                if self.neo.exists() or self.harness.exists():
                    raise MatrixError("installed console entry points still exist")
                code = (
                    "import importlib.metadata as m;"
                    "print('present' if any(d.metadata['Name']=='neo-agent-cli' "
                    "for d in m.distributions()) else 'absent')"
                )
                completed = subprocess.run(
                    [str(self.venv_python), "-I", "-c", code],
                    cwd=str(self.workdir),
                    env=self.environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
                if completed.returncode != 0 or completed.stdout.strip() != "absent":
                    raise MatrixError(
                        f"distribution metadata remains: {completed.stdout.strip()} "
                        f"{completed.stderr.strip()}"
                    )
                return {
                    "console_entrypoints_removed": True,
                    "distribution": "absent",
                    "external_uninstall_receipt": receipt_payload,
                    "settings_removed": not Path(auth_env["NEO_CONFIG"]).exists(),
                }

            if not self.internal(
                "uninstall_complete",
                [str(self.neo), "uninstall", "--yes"],
                verify_uninstalled,
            ):
                self.status = "fail"
                self.error = "installed uninstall did not fully complete"
                self.block_remaining("uninstall_complete", self.error)
                return self.report(started)
            self.status = "pass"
            self.error = None
            return self.report(started)
        except Exception as exc:
            self.status = "fail"
            self.error = f"{type(exc).__name__}: {exc}"
            completed_names = {check["name"] for check in self.checks}
            first_missing = next(
                name for name in _CHECK_NAMES if name not in completed_names
            )
            self.block_remaining(first_missing, self.error)
            return self.report(started)

    def _fixture_paths(self) -> tuple[Path, Path, Path]:
        """Locate packaged, copied, and source fixture roots."""
        code = (
            "import pathlib, cli; "
            "print(pathlib.Path(cli.__file__).resolve().parent / 'fixtures' / 'smoke_repo')"
        )
        completed = subprocess.run(
            [str(self.venv_python), "-I", "-c", code],
            cwd=str(self.workdir),
            env=self.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise MatrixError(
                f"could not locate packaged fixture: {completed.stderr.strip()}"
            )
        package_fixture = Path(completed.stdout.strip()).resolve()
        copied_fixture = self.root / "original-fixture"
        source_fixture = self.project_root / "cli" / "fixtures" / "smoke_repo"
        return package_fixture, copied_fixture, source_fixture

    def report(self, started: float) -> dict[str, Any]:
        """Return one lane's exact checks and pass/fail/blocked totals."""
        counts = {
            status: sum(check["status"] == status for check in self.checks)
            for status in ("pass", "fail", "blocked", "skip")
        }
        return {
            "label": self.label,
            "status": self.status,
            "python": self.python_label,
            "python_executable": str(self.python_path),
            "artifact_kind": self.artifact_kind,
            "artifact": str(self.artifact),
            "artifact_sha256": self.artifact_sha256,
            "expected_version": self.expected_version,
            "package_names": self.package_names,
            "checks": self.checks,
            "pass_count": counts["pass"],
            "fail_count": counts["fail"],
            "blocked_count": counts["blocked"],
            "skip_count": counts["skip"],
            "fixture_hashes_before": self.hashes_before,
            "fixture_hashes_after": self.hashes_after,
            "error": self.error,
            "duration_s": round(time.monotonic() - started, 3),
        }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Atomically write the matrix report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _emit_event(event: str, **data: Any) -> None:
    """Write one schema-versioned matrix event to stdout."""
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **data,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _matrix_failure(
    message: str,
    event_mode: bool,
    exit_code: int = 2,
    reason: str = "usage_error",
) -> None:
    """Render one machine-readable setup failure."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "fail",
        "error": {
            "kind": "matrix_setup_error",
            "message": message,
            "exit_code": exit_code,
            "exit_reason": reason,
        },
    }
    if event_mode:
        _emit_event("clean_room_matrix_finished", report=payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    """Run all requested clean-room lanes with stable machine output."""
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--python", action="append", required=True, type=_parse_python)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "ndjson"), default="json")
    parser.add_argument(
        "--events", action="store_true", help="alias for --format ndjson"
    )
    args: Optional[argparse.Namespace] = None
    event_mode = False
    try:
        args = parser.parse_args(argv)
        event_mode = args.events or args.format == "ndjson"
        project_root = args.project_root.resolve()
        output_root = args.output_root.resolve()
        if output_root.exists():
            if not output_root.is_dir() or any(output_root.iterdir()):
                raise MatrixUsageError(
                    f"output root must be an empty directory: {output_root}"
                )
        else:
            output_root.mkdir(parents=True)
        expected_version, package_names = _load_project(project_root)
        artifacts = (("wheel", args.wheel.resolve()), ("sdist", args.sdist.resolve()))
        for kind, artifact in artifacts:
            if not artifact.is_file():
                raise MatrixUsageError(f"{kind} artifact does not exist: {artifact}")
        started = time.monotonic()
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(project_root),
            "expected_version": expected_version,
            "package_names": package_names,
            "python_runtimes": [
                {"label": label, "executable": str(path)} for label, path in args.python
            ],
            "artifacts": {
                kind: {"path": str(path), "sha256": _sha256(path)}
                for kind, path in artifacts
            },
            "output_root": str(output_root),
            "command": [
                sys.executable,
                str(Path(__file__).resolve()),
                *(argv or sys.argv[1:]),
            ],
            "lanes": [],
        }
        _write_report(args.report, report)
        if event_mode:
            _emit_event("clean_room_matrix_started", report=report)
        for python_label, python_path in args.python:
            for artifact_kind, artifact in artifacts:
                label = f"py{python_label}-{artifact_kind}"
                if event_mode:
                    _emit_event("clean_room_lane_started", lane=label)
                lane = _Lane(
                    label,
                    python_label,
                    python_path,
                    artifact,
                    artifact_kind,
                    output_root,
                    project_root,
                    expected_version,
                    package_names,
                )
                lane_report = lane.run()
                report["lanes"].append(lane_report)
                report["duration_s"] = round(time.monotonic() - started, 3)
                expected_lane_count = len(args.python) * len(artifacts)
                if len(report["lanes"]) == expected_lane_count and all(
                    item["status"] == "pass" for item in report["lanes"]
                ):
                    report["status"] = "pass"
                elif any(item["status"] == "fail" for item in report["lanes"]):
                    report["status"] = "fail"
                else:
                    report["status"] = "blocked"
                _write_report(args.report, report)
                if event_mode:
                    _emit_event("clean_room_lane_finished", report=lane_report)
                else:
                    print(
                        f"{lane_report['status'].upper()} {label}: "
                        f"{lane_report['error'] or 'all checks passed'}"
                    )
        totals = {
            status: sum(lane[status + "_count"] for lane in report["lanes"])
            for status in ("pass", "fail", "blocked", "skip")
        }
        report["totals"] = totals
        report["duration_s"] = round(time.monotonic() - started, 3)
        report["exit_code"] = 0 if report["status"] == "pass" else 2
        report["exit_reason"] = (
            "success" if report["status"] == "pass" else report["status"]
        )
        _write_report(args.report, report)
        if event_mode:
            _emit_event("clean_room_matrix_finished", report=report)
        else:
            print(
                json.dumps(
                    {"status": report["status"], "totals": totals}, sort_keys=True
                )
            )
        return int(report["exit_code"])
    except MatrixUsageError as exc:
        _matrix_failure(str(exc), event_mode)
        return 2
    except (OSError, MatrixError) as exc:
        _matrix_failure(
            f"{type(exc).__name__}: {exc}", event_mode, 3, "environment_error"
        )
        return 3
    except Exception as exc:
        _matrix_failure(f"{type(exc).__name__}: {exc}", event_mode, 3, "internal_error")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
