"""Installed-wheel user flow with private home, config, logs, and credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_ENV_NAMES = (
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
)

_INSTALLED_SDK_SMOKE = r"""
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import tempfile

import agent_sdk

checkout = Path(sys.argv[1]).resolve()
venv = Path(sys.prefix).resolve()
assert not Path.cwd().resolve().is_relative_to(checkout)
assert not any(
    entry and Path(entry).resolve().is_relative_to(checkout)
    for entry in sys.path
), sys.path
origin = Path(agent_sdk.__file__).resolve()
assert origin.is_relative_to(venv), origin
assert {"Agent", "AgentServer", "LocalAgent", "Result"} <= set(agent_sdk.__all__)

def finish_model(messages, **kwargs):
    return json.dumps({"tool": "finish", "answer": "installed-sdk-ok"})

with tempfile.TemporaryDirectory(prefix="neo-installed-sdk-") as temporary:
    root = Path(temporary)
    repo = root / "repo"
    logs = root / "logs"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    agent = agent_sdk.LocalAgent(
        repo,
        log_root=logs,
        model=finish_model,
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    try:
        result = agent.query("Return the deterministic installed SDK answer")
        replay = agent.events(result.run_id).replay()
        events = list(replay)
        assert result.status == "completed_unverified"
        assert not result.completed_verified
        assert result.answer == "installed-sdk-ok"
        assert result.schema_version == 1
        assert replay.final_status == "completed_unverified"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert all(event.schema_version == 1 for event in events)
        assert Path(result.trace_path).resolve().is_relative_to(logs.resolve())
    finally:
        agent.close()

print(json.dumps({
    "agent_sdk": str(origin),
    "answer": result.answer,
    "events": len(events),
    "status": result.status,
    "version": metadata.version("neo-agent-cli"),
}, sort_keys=True))
"""


def _private_env(root: Path) -> dict[str, str]:
    env = {name: os.environ[name] for name in _SYSTEM_ENV_NAMES if name in os.environ}
    home = root / "home"
    appdata = root / "appdata"
    localappdata = root / "localappdata"
    config = root / "config"
    memory = root / "memory"
    logs = root / "logs"
    trace = root / "trace"
    temp = root / "temp"
    for path in (home, appdata, localappdata, config, memory, logs, trace, temp):
        path.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(root / "data"),
            "HARNESS_HOME": str(memory),
            "HARNESS_DECISIONS_DB": str(memory / "decisions.db"),
            "HARNESS_LOGS_DIR": str(logs),
            "NEO_CONFIG": str(config / "settings.toml"),
            "NEO_TRACE_DIR": str(trace),
            "TEMP": str(temp),
            "TMP": str(temp),
            "NEO_GLOBAL_ROOT": str(config / "neo"),
            "NEO_PLUGINS_DIR": str(config / "neo" / "plugins"),
            "NEO_NO_ONBOARD": "1",
            "NEO_NOTIFY": "0",
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_CACHE_DIR": str(root / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    for name in tuple(env):
        if "API_KEY" in name or "TOKEN" in name or "PASSWORD" in name:
            env.pop(name, None)
    return env


def _run(
    command: list[str], cwd: Path, env: dict[str, str], timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-user-flow")
    project = REPO_ROOT
    candidate_dist = os.environ.get("NEO_RELEASE_DIST")
    if candidate_dist:
        dist = Path(candidate_dist).resolve()
        wheels = list(dist.glob("neo_agent_cli-*.whl"))
        sdists = list(dist.glob("neo_agent_cli-*.tar.gz"))
        assert len(wheels) == 1
        assert len(sdists) == 1
        wheel = wheels[0]
        sdist = sdists[0]
    else:
        build = _run(
            [sys.executable, "-m", "build", "--outdir", str(root / "dist")],
            project,
            _private_env(root / "build-env"),
            timeout=180,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        wheels = list((root / "dist").glob("neo_agent_cli-*.whl"))
        sdists = list((root / "dist").glob("neo_agent_cli-*.tar.gz"))
        assert len(wheels) == 1
        assert len(sdists) == 1
        wheel = wheels[0]
        sdist = sdists[0]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
    assert not any(
        "__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names
    )
    assert "harness/agent_kernel/kernel.py" in names
    assert "harness/agent_kernel/strategy.py" in names
    assert "execution/workspace.py" in names
    assert "memory/project_context.py" in names
    assert "cli/tui.py" in names
    assert "recipes/builtin/portable_code_review.yaml" in names
    assert "recipes/builtin/summarize_findings.yaml" in names
    for module in (
        "agent_sdk/__init__.py",
        "agent_sdk/client.py",
        "agent_sdk/local.py",
        "agent_sdk/remote.py",
        "agent_sdk/server.py",
    ):
        assert module in names
    for package in (
        "cli",
        "harness",
        "runtime",
        "execution",
        "evals",
        "acp",
        "agent_sdk",
        "extensions",
        "integrations",
        "recipes",
    ):
        assert f"{package}/__init__.py" in names
    assert "recipes/builtin/__init__.py" in names
    assert metadata["License-Expression"] == "MIT"
    requirements = metadata.get_all("Requires-Dist") or []
    assert any(
        requirement.lower().startswith("mcp")
        and ">=2" in requirement
        and "<3" in requirement
        for requirement in requirements
    ), requirements
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())
        pkg_info_name = next(name for name in sdist_names if name.endswith("/PKG-INFO"))
        pkg_info = Parser().parsestr(
            archive.extractfile(pkg_info_name).read().decode("utf-8")
        )
    assert not any(
        "__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in sdist_names
    )
    assert any(name.endswith("/LICENSE") for name in sdist_names)
    assert any(name.endswith("/pyproject.toml") for name in sdist_names)
    assert any(name.endswith("/harness/agent_kernel/kernel.py") for name in sdist_names)
    assert any(name.endswith("/execution/workspace.py") for name in sdist_names)
    assert any(name.endswith("/memory/project_context.py") for name in sdist_names)
    assert any(
        name.endswith("/recipes/builtin/portable_code_review.yaml")
        for name in sdist_names
    )
    assert any(
        name.endswith("/recipes/builtin/summarize_findings.yaml")
        for name in sdist_names
    )
    for module in (
        "agent_sdk/__init__.py",
        "agent_sdk/client.py",
        "agent_sdk/local.py",
        "agent_sdk/remote.py",
        "agent_sdk/server.py",
    ):
        assert any(name.endswith(f"/{module}") for name in sdist_names)
    for package in (
        "cli",
        "harness",
        "runtime",
        "execution",
        "evals",
        "acp",
        "agent_sdk",
        "extensions",
        "integrations",
        "recipes",
    ):
        assert any(name.endswith(f"/{package}/__init__.py") for name in sdist_names)
    assert any(name.endswith("/recipes/builtin/__init__.py") for name in sdist_names)
    assert pkg_info["Version"] == metadata["Version"]
    assert pkg_info["License-Expression"] == "MIT"

    venv = root / "venv"
    created = _run(
        [sys.executable, "-m", "venv", str(venv)],
        root,
        _private_env(root / "venv-env"),
        timeout=300,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    if os.name == "nt":
        python = venv / "Scripts" / "python.exe"
        neo = venv / "Scripts" / "neo.exe"
        harness = venv / "Scripts" / "harness.exe"
    else:
        python = venv / "bin" / "python"
        neo = venv / "bin" / "neo"
        harness = venv / "bin" / "harness"
    install = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--timeout",
            "120",
            "--retries",
            "8",
            str(wheel),
        ],
        root,
        _private_env(root / "install-env"),
        timeout=600,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return {
        "root": root,
        "wheel": wheel,
        "sdist": sdist,
        "venv": venv,
        "python": python,
        "neo": neo,
        "harness": harness,
    }


def test_installed_console_entrypoints_resolve_from_private_venv(
    installed_wheel, tmp_path
):
    work = tmp_path / "outside-checkout"
    work.mkdir()
    env = _private_env(tmp_path / "isolation")
    version = _run([str(installed_wheel["neo"]), "--version"], work, env)
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.strip()
    legacy = _run([str(installed_wheel["harness"]), "--version"], work, env)
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    origin = _run(
        [
            str(installed_wheel["python"]),
            "-I",
            "-B",
            "-c",
            (
                "import importlib.metadata,json,acp,agent_sdk,cli,extensions,harness,integrations,recipes,recipes.builtin,runtime,execution,evals;"
                "print(json.dumps({'version':importlib.metadata.version('neo-agent-cli'),"
                "'acp':acp.__file__,'agent_sdk':agent_sdk.__file__,'cli':cli.__file__,"
                "'extensions':extensions.__file__,'harness':harness.__file__,"
                "'integrations':integrations.__file__,'recipes':recipes.__file__,"
                "'recipes.builtin':recipes.builtin.__file__,"
                "'runtime':runtime.__file__,'execution':execution.__file__,'evals':evals.__file__}))"
            ),
        ],
        work,
        env,
    )
    assert origin.returncode == 0, origin.stdout + origin.stderr
    data = json.loads(origin.stdout.strip().splitlines()[-1])
    assert data["version"]
    venv = installed_wheel["venv"].resolve()
    for key in (
        "cli",
        "harness",
        "runtime",
        "execution",
        "evals",
        "acp",
        "agent_sdk",
        "extensions",
        "integrations",
        "recipes",
    ):
        assert Path(data[key]).resolve().is_relative_to(venv), (key, data[key])


def test_installed_agent_sdk_runs_outside_source_checkout(installed_wheel, tmp_path):
    work = tmp_path / "sdk-smoke"
    work.mkdir()
    result = _run(
        [
            str(installed_wheel["python"]),
            "-I",
            "-B",
            "-c",
            _INSTALLED_SDK_SMOKE,
            str(REPO_ROOT),
        ],
        work,
        _private_env(tmp_path / "sdk-smoke-env"),
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "completed_unverified"
    assert payload["answer"] == "installed-sdk-ok"
    assert payload["events"] > 0
    assert (
        Path(payload["agent_sdk"])
        .resolve()
        .is_relative_to(installed_wheel["venv"].resolve())
    )


def test_installed_package_help_surfaces_resolve(installed_wheel, tmp_path):
    work = tmp_path / "module-help"
    work.mkdir()
    env = _private_env(tmp_path / "module-help-env")
    for package in ("harness", "cli", "runtime", "execution", "evals"):
        result = _run(
            [str(installed_wheel["python"]), "-I", "-m", package, "--help"],
            work,
            env,
        )
        assert result.returncode == 0, (package, result.stdout, result.stderr)
        assert "usage:" in result.stdout.casefold()


def test_installed_first_user_scaffold_stays_in_private_state(
    installed_wheel, tmp_path
):
    repo = tmp_path / "first-user-repo"
    repo.mkdir()
    init = _run(["git", "init"], repo, _private_env(tmp_path / "git-env"))
    assert init.returncode == 0, init.stderr
    env = _private_env(tmp_path / "flow-env")
    result = _run([str(installed_wheel["neo"]), "config", "init-project"], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        repo / ".neo" / "settings.toml",
        repo / ".neo" / "settings.local.toml",
        repo / ".neo" / "commands" / "fix.md",
        repo / ".neo" / "skills" / "code-review" / "SKILL.md",
        repo / ".neo" / "connectors.toml",
        repo / ".neo" / "connectors.local.toml",
    )
    assert all(path.is_file() for path in expected)
    assert all(path.resolve().is_relative_to(repo.resolve()) for path in expected)
    assert Path(env["NEO_CONFIG"]).is_relative_to(tmp_path)
    assert Path(env["HARNESS_DECISIONS_DB"]).is_relative_to(tmp_path)
    assert Path(env["HARNESS_LOGS_DIR"]).is_relative_to(tmp_path)
    assert Path(env["NEO_TRACE_DIR"]).is_relative_to(tmp_path)
    for name in ("NEO_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert name not in env
    ignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".neo/settings.local.toml" in ignore_text
    assert ".neo/connectors.local.toml" in ignore_text


@pytest.mark.skipif(
    os.name != "nt", reason="Windows console-launcher uninstall contract"
)
def test_installed_windows_uninstall_waits_for_locked_launcher(
    installed_wheel, tmp_path
):
    root = tmp_path / "windows-uninstall"
    venv = root / "venv"
    work = root / "work"
    work.mkdir(parents=True)
    env = _private_env(root / "env")
    created = _run(
        [sys.executable, "-I", "-B", "-m", "venv", str(venv)],
        root,
        env,
        timeout=300,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = venv / "Scripts" / "python.exe"
    neo = venv / "Scripts" / "neo.exe"
    harness = venv / "Scripts" / "harness.exe"
    installed = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            str(installed_wheel["wheel"]),
        ],
        work,
        env,
        timeout=600,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert neo.is_file()
    assert harness.is_file()

    process = subprocess.Popen(
        [str(neo), "uninstall", "--yes"],
        cwd=work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stdout + stderr
    assert "scheduled" in stdout.casefold()

    deadline = time.monotonic() + 120
    payload = None
    while time.monotonic() < deadline:
        receipts = list(Path(env["TEMP"]).glob("neo-uninstall-*.json"))
        if receipts:
            try:
                candidate = json.loads(receipts[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                payload = candidate
                if candidate.get("status") in {"succeeded", "failed"}:
                    break
        time.sleep(0.25)
    assert payload is not None
    assert payload["status"] == "succeeded", payload
    assert payload["launcher_pid"] == process.pid
    assert Path(payload["launcher_path"]).resolve() == neo.resolve()
    assert (
        not Path(payload["helper_interpreter"]).resolve().is_relative_to(venv.resolve())
    )
    assert Path(payload["target_interpreter"]).resolve() == python.resolve()
    assert payload["distribution"] == "neo-agent-cli"
    assert not neo.exists()
    assert not harness.exists()
    assert python.is_file()

    metadata = _run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            (
                "import importlib.metadata as m;"
                "print('present' if any(d.metadata['Name']=='neo-agent-cli' "
                "for d in m.distributions()) else 'absent')"
            ),
        ],
        work,
        env,
        timeout=60,
    )
    assert metadata.returncode == 0, metadata.stdout + metadata.stderr
    assert metadata.stdout.strip() == "absent"
