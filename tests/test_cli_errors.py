"""Tests for cli/errors.py + the CLI's plain-language error surfaces
(Round 8, Task D — end-user error message clarity).

The contract under test: when something fails at the CLI level the user
sees (1) a plain-language cause and (2) "check:" lines for what to
inspect — and NEVER a raw Python traceback on the terminal. The full
traceback is saved to a file (for bug reports) and its path printed.
"""

from pathlib import Path

import pytest

from cli import errors
from cli.main import main

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HARNESS_HOME so tests never touch the real .harness/."""
    h = tmp_path / "home"
    monkeypatch.setenv("HARNESS_HOME", str(h))
    return h


@pytest.fixture
def logs_root(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(d))
    return d


@pytest.fixture
def smoke_repo(tmp_path):
    """A copy of the bundled smoke fixture repo (has a real bug+test)."""
    import shutil

    dst = tmp_path / "smoke_repo"
    shutil.copytree(REPO_ROOT / "cli" / "fixtures" / "smoke_repo", dst)
    return dst


@pytest.fixture(autouse=True)
def _tb_dir(tmp_path, monkeypatch):
    """Keep saved tracebacks out of the real temp dir for assertions."""
    d = tmp_path / "tb"
    monkeypatch.setenv("VEX_TRACEBACK_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# explain_exception: classification
# ---------------------------------------------------------------------------


def _explain_out(exc, capsys) -> str:
    errors.explain_exception(exc)
    captured = capsys.readouterr()
    return captured.out + captured.err


class SandboxUnavailableError(RuntimeError):
    pass


def test_explain_docker_down(capsys):
    out = _explain_out(SandboxUnavailableError("docker daemon not reachable"), capsys)
    assert "Docker sandbox" in out
    assert "docker version" in out  # a concrete check to run
    assert "Traceback" not in out


def test_explain_docker_by_message(capsys):
    out = _explain_out(RuntimeError("cannot connect to the Docker daemon"), capsys)
    assert "Docker" in out
    assert "check:" in out


def test_explain_auth_error(capsys):
    class AuthenticationError(Exception):
        pass

    out = _explain_out(AuthenticationError("litellm.AuthenticationError: 401"), capsys)
    assert "credentials" in out or "API key" in out
    assert "--api-key" in out
    assert "Traceback" not in out


def test_explain_rate_limit(capsys):
    class RateLimitError(Exception):
        pass

    out = _explain_out(RateLimitError("litellm.RateLimitError: 429"), capsys)
    assert "rate-limiting" in out


def test_explain_model_unreachable(capsys):
    class APIConnectionError(Exception):
        pass

    out = _explain_out(
        APIConnectionError("litellm.APIConnectionError: connection refused"), capsys
    )
    assert "endpoint" in out


def test_explain_file_not_found(capsys):
    out = _explain_out(FileNotFoundError("no such file: logs/x/state.json"), capsys)
    assert "does not exist" in out
    assert "Traceback" not in out


def test_explain_permission_error(capsys):
    out = _explain_out(
        PermissionError(13, "Access is denied", "work/diff.patch"), capsys
    )
    assert "permission" in out.lower()


def test_explain_unknown_still_clean_and_saves_traceback(capsys, _tb_dir):
    """An unmapped exception gets the honest fallback: cause + checks +
    the path to the SAVED full traceback — never the traceback inline."""
    out = _explain_out(ValueError("catastrophic widget misalignment"), capsys)
    assert "unexpected internal error" in out
    assert "Traceback" not in out  # nothing raw on the terminal
    # the traceback WAS saved for the bug report...
    tbs = list(_tb_dir.glob("vex-traceback-*.txt"))
    assert tbs, "traceback file was written for the unmapped exception"
    assert "ValueError: catastrophic widget misalignment" in tbs[0].read_text()


def test_explain_never_raises(capsys):
    """Even hostile input to the explainer must not raise (a failure
    handler that itself crashes is worse than the original failure)."""

    class Weird:
        def __str__(self):
            raise RuntimeError("no string for you")

    try:
        errors.explain_exception(Weird())  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - only on regression
        pytest.fail(f"explain_exception raised: {exc}")
    assert "Traceback" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# wiring: the CLI surfaces actually use it
# ---------------------------------------------------------------------------


def test_fix_crash_explained_plainly(smoke_repo, home, logs_root, capsys, monkeypatch):
    """A harness crash mid-run surfaces as cause + checks, not a raw
    traceback and not a bare `error: run_task crashed: ...` line.
    Exit-code contract (cli.exit_codes): a Docker/sandbox crash is an
    ENVIRONMENT error -> 3 (was the old catch-all 1; scripts can now
    distinguish it from a task-level failure). Dummy creds are set so
    the onboarding gate (no usable model -> exit 4) passes and the run
    reaches the crash it is meant to classify."""
    import cli.deps as cli_deps

    monkeypatch.setenv("VEX_MODEL", "test-model")
    monkeypatch.setenv("VEX_API_KEY", "test-key")

    def exploding_run_task(task, log_root=None):
        raise SandboxUnavailableError("docker daemon not reachable")

    monkeypatch.setattr(cli_deps, "get_run_task", lambda: exploding_run_task)
    rc = main(
        [
            "fix",
            "--repo",
            str(smoke_repo),
            "--issue",
            "it is broken",
            "--log-root",
            str(logs_root),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 3
    assert "Docker sandbox" in captured.err
    assert "check:" in captured.err
    assert "Traceback" not in captured.out + captured.err


def test_top_level_net_catches_subcommand_escapes(home, capsys, monkeypatch):
    """Whatever escapes a subcommand still never reaches the user as a
    raw traceback (the main() safety net explains + saves it). An
    unmapped exception stays exit 1 (task-level failure category)."""

    def boom(args):
        raise ValueError("kaboom from deep inside a command")

    # build a one-off parser with a hostile command, keep the real
    # main() dispatch path (parse -> func -> net) by monkeypatching func.
    monkeypatch.setattr(
        "cli.main.build_parser", lambda: _parser_with_explosive_command(boom)
    )
    rc = main(["explode"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.out + captured.err
    assert "kaboom" in captured.err  # the real cause is still named


def _parser_with_explosive_command(func):
    import argparse

    p = argparse.ArgumentParser(prog="vex")
    sub = p.add_subparsers(dest="command")
    pe = sub.add_parser("explode")
    pe.set_defaults(func=func)
    return p


def test_python_dash_m_cli_import_failure_is_clean(tmp_path, monkeypatch, capsys):
    """`python -m cli` with a broken install prints install guidance,
    not an ImportError traceback. Tested at the exact seam that owns
    this (__main__._run's import guard), since shadowing a package on
    PYTHONPATH from a subprocess is host-dependent."""
    import cli.__main__ as dunder_main

    real_import = __import__

    def broken_import(name, *a, **k):
        if name == "cli.main":
            raise ImportError("cannot import name 'main' from 'cli'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", broken_import)
    rc = dunder_main._run()
    captured = capsys.readouterr()
    monkeypatch.undo()
    assert rc == 2
    assert "Traceback" not in captured.err + captured.out
    assert "pip install" in captured.err
    assert "check:" in captured.err


def test_saved_traceback_off_by_flag(capsys, _tb_dir, monkeypatch):
    """save_traceback=False skips the file write entirely (callers that
    already own failure reporting can keep the temp dir clean)."""
    errors.explain_exception(ValueError("quiet please"), save_traceback=False)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert not list(_tb_dir.glob("vex-traceback-*.txt"))
