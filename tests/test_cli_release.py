"""Tests for the Release Readiness / CLI-citizenship round.

Covers (task letter -> test class):
A  --help overview (every public subcommand present) + --version
   matching pyproject/CHANGELOG.
B  exit codes: the category contract (task/config/environment/model/
   interrupted) + legacy 0/1/2 semantics preserved.
C  NO_COLOR env var + --no-color flag end-to-end.
D  shell completion generation (bash/zsh/fish/powershell) + the
   dynamic __completions backend + --install paths.
E  vex update: install-method detection, version-check paths, the
   source-checkout refusal recipe.
F  clean uninstall: plan collection, dry-run, confirmation abort.
G  --json output on fix + status (machine-readable, parse-able).
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cli import completion, exit_codes, selfupdate
from cli.main import build_parser, main

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_overrides():
    import cli.deps as cli_deps

    cli_deps.reset_overrides()
    yield
    cli_deps.reset_overrides()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("HARNESS_HOME", str(h))
    return h


@pytest.fixture
def logs_root(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# Task A — --help and --version
# ---------------------------------------------------------------------------


class TestHelpAndVersion:
    def test_help_lists_every_public_subcommand(self, capsys):
        """`vex --help` shows a clean overview: every public command,
        the key flags, and the exit-code contract. argparse's help
        action exits 0 via SystemExit (same as the real process)."""
        with pytest.raises(SystemExit) as ei:
            main(["--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        for cmd in (
            "fix",
            "run-benchmark",
            "config",
            "status",
            "memory",
            "plugin",
            "dashboard",
            "mcp",
            "update",
            "completion",
            "uninstall",
        ):
            assert cmd in out, f"--help is missing the {cmd} command"
        for flag in (
            "--version",
            "--no-color",
            "--continue",
            "--resume",
            "--list-sessions",
        ):
            assert flag in out
        # the exit-code contract is documented right in --help
        assert "exit codes" in out
        assert "130" in out
        # hidden machinery stays out of the overview
        assert "__completions" not in out

    def test_help_hides_backend_from_metavar(self):
        p = build_parser()
        assert "__completions" in p._subparsers._group_actions[0].choices
        # but the usage metavar lists public commands only
        import io

        buf = io.StringIO()
        p.print_help(buf)
        text = buf.getvalue()
        assert "__completions" not in text.split("options:")[0]

    def test_version_matches_pyproject(self, capsys):
        """`vex --version` prints the installed version, which must match
        pyproject.toml (the release source of truth; CHANGELOG cites it).
        argparse's version action exits 0 via SystemExit — the CLI seam
        a user hits is the real process, also covered below."""
        with pytest.raises(SystemExit) as ei:
            main(["--version"])
        assert ei.value.code == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("vex ")
        version = out.split()[-1]
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        assert m, "pyproject has a version"
        # installed dist or +source fallback — either equals pyproject's
        assert version in (m.group(1), m.group(1) + "+source"), (
            f"version {version!r} does not match pyproject {m.group(1)!r}"
        )

    def test_version_via_real_subprocess(self):
        """The first-30-seconds check, at the same seam a user hits."""
        cp = subprocess.run(
            [sys.executable, "-m", "cli", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert cp.returncode == 0
        assert cp.stdout.strip().startswith("vex ")
        assert "Traceback" not in cp.stderr

    def test_subcommand_help_works(self, capsys):
        for cmd in ("fix", "status", "update", "completion", "uninstall"):
            with pytest.raises(SystemExit) as ei:
                main([cmd, "--help"])
            assert ei.value.code == 0
            out = capsys.readouterr().out
            assert "usage" in out
            assert "--help" in out


# ---------------------------------------------------------------------------
# Task B — meaningful exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_the_contract_table(self):
        assert exit_codes.EXIT_CODES == {
            "success": 0,
            "task_failure": 1,
            "usage_error": 2,
            "environment_error": 3,
            "model_error": 4,
            "interrupted": 130,
        }

    def test_reason_roundtrip(self):
        for cat, code in exit_codes.EXIT_CODES.items():
            assert exit_codes.reason_for(code) == cat
        assert exit_codes.reason_for(99) == "unknown"

    def test_usage_error_is_two(self, home, capsys):
        """Bad arguments -> 2 (the long-standing contract)."""
        rc = main(["fix", "--repo", "Z:/definitely/not/here", "--issue", "x"])
        assert rc == 2

    def test_invalid_flag_is_two(self, home):
        with pytest.raises(SystemExit) as ei:
            main(["status", "--task-id", "x", "--definitely-not-a-flag"])
        assert ei.value.code == 2  # argparse usage error passthrough

    def test_task_failure_is_one(
        self, smoke_repo_fix, home, logs_root, capsys, monkeypatch
    ):
        """A verifier-gated failure (the bug not fixed) -> 1."""
        import harness.deps as hdeps

        class NoFixModel:
            def get_last_usage(self):
                return {
                    "model": "scripted",
                    "provider": "test",
                    "tokens": 1,
                    "cost_usd": 0.0,
                }

            def __call__(self, *a, **k):
                return "SUBMIT"  # never edits anything

        hdeps.set_call_model(NoFixModel())
        monkeypatch.chdir(smoke_repo_fix.parent)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "mean() returns the sum; make it the mean",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
            ]
        )
        assert rc == 1

    def test_environment_error_is_three(
        self, smoke_repo_fix, home, logs_root, monkeypatch
    ):
        """Docker down -> 3, distinct from a task failure (CI can retry
        differently / page the right person). Dummy creds are set so
        the onboarding gate (no usable model -> exit 4) passes and the
        run reaches the crash it is meant to classify."""
        import cli.deps as cli_deps

        monkeypatch.setenv("VEX_MODEL", "test-model")
        monkeypatch.setenv("VEX_API_KEY", "test-key")

        class SandboxUnavailableError(RuntimeError):
            pass

        def exploding_run_task(task, log_root=None):
            raise SandboxUnavailableError("docker daemon not reachable")

        monkeypatch.setattr(cli_deps, "get_run_task", lambda: exploding_run_task)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "it is broken",
                "--log-root",
                str(logs_root),
            ]
        )
        assert rc == 3

    def test_model_error_is_four(self, smoke_repo_fix, home, logs_root, monkeypatch):
        """Model endpoint unreachable/auth-failed -> 4. Dummy creds are
        set so the run reaches the crash (rather than the onboarding
        gate's own exit 4 for missing creds — same code, wrong reason)."""
        import cli.deps as cli_deps

        monkeypatch.setenv("VEX_MODEL", "test-model")
        monkeypatch.setenv("VEX_API_KEY", "test-key")

        class APIConnectionError(RuntimeError):
            pass

        def dead_model_run_task(task, log_root=None):
            raise APIConnectionError("litellm.APIConnectionError: connection refused")

        monkeypatch.setattr(cli_deps, "get_run_task", lambda: dead_model_run_task)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "it is broken",
                "--log-root",
                str(logs_root),
            ]
        )
        assert rc == 4

    def test_classification_never_raises(self):
        class Weird:
            def __str__(self):
                raise RuntimeError("no")

        code = exit_codes.classify_exit_code(Weird())  # type: ignore[arg-type]
        assert code == 1

    def test_explainer_names_the_category(self, capsys):
        """The plain-language explainer now also prints the category +
        numeric code, so interactive users can see what scripts see."""
        from cli import errors

        class SandboxUnavailableError(RuntimeError):
            pass

        errors.explain_exception(
            SandboxUnavailableError("docker daemon not reachable"),
            save_traceback=False,
        )
        err = capsys.readouterr().err
        assert "environment_error" in err
        assert "exit code 3" in err


@pytest.fixture
def smoke_repo_fix(tmp_path: Path) -> Path:
    src = REPO_ROOT / "cli" / "fixtures" / "smoke_repo"
    dst = tmp_path / "repo"
    shutil.copytree(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Task C — NO_COLOR / --no-color
# ---------------------------------------------------------------------------


class TestColorControl:
    def test_no_color_env_strips_ansi(self, home, capsys, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        rc = main(["status", "--task-id", "does-not-exist-xyz"])
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "\x1b[" not in combined  # no color codes
        assert rc == 2

    def test_no_color_flag_strips_ansi(self, home, capsys, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        from cli import ui

        ui.set_no_color(False)  # reset between tests
        rc = main(["--no-color", "status", "--task-id", "does-not-exist-xyz"])
        combined = capsys.readouterr()
        assert "\x1b[" not in combined.out + combined.err
        ui.set_no_color(False)
        assert rc == 2

    def test_no_color_flag_accepted_on_subcommand(self, home):
        """--no-color is accepted late (on the subcommand too)."""
        rc = main(["status", "--task-id", "x", "--no-color"])
        assert rc == 2  # invalid id -> 2, not a usage error about the flag


# ---------------------------------------------------------------------------
# Task D — shell completion
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_every_shell_script_generated(self, capsys):
        for shell in ("bash", "zsh", "fish", "powershell"):
            rc = main(["completion", shell])
            assert rc == 0
            out = capsys.readouterr().out
            assert "vex" in out
            assert "__completions" in out  # the dynamic backend
            if shell == "bash":
                assert "complete -o nosort -F _vex_completions vex" in out
            if shell == "zsh":
                assert "#compdef vex" in out
            if shell == "fish":
                assert "complete -c vex" in out
            if shell == "powershell":
                assert "Register-ArgumentCompleter" in out

    def test_backend_completes_top_level(self):
        got = completion.collect_completions([""])
        assert "fix" in got
        assert "update" in got
        assert "completion" in got
        assert "uninstall" in got
        assert "__completions" not in got

    def test_backend_completes_subcommand_partial(self):
        got = completion.collect_completions(["fi"])
        assert got == ["fix"]

    def test_backend_completes_subcommand_flags(self):
        got = completion.collect_completions(["fix", "--"])
        assert "--model" in got
        assert "--json" in got
        assert "--budget" in got
        # top-level-only flags don't leak into subcommand completions
        assert "--list-sessions" not in got

    def test_backend_completes_nested_subcommands(self):
        got = completion.collect_completions(["config", ""])
        # config's nested subcommands surface
        assert "list" in got
        assert "set" in got

    def test_backend_completes_choice_flags(self):
        got = completion.collect_completions(["config", "set", "--tier", ""])
        assert "global" in got
        assert "project" in got
        assert "local" in got

    def test_backend_via_real_subprocess(self):
        """The exact invocation the generated shell scripts make."""
        out = subprocess.run(
            [sys.executable, "-m", "cli", "__completions", "fi"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert out.returncode == 0
        lines = out.stdout.splitlines()
        assert "fix" in lines
        assert lines[-1] == "--"  # the sentinel
        assert "Traceback" not in out.stderr

    def test_backend_never_raises_on_weird_input(self):
        # a flag needing a value, cut mid-word: completes that flag
        assert completion.collect_completions(["status", "--log-ro"]) == ["--log-root"]
        # empty prefix at top level lists public commands, never the backend
        assert "__completions" not in completion.collect_completions([""])
        # anything at all after an unknown word: no crash, a list out
        assert isinstance(completion.collect_completions(["zzz", ""]), list)

    def test_install_writes_bash_location(self, tmp_path, monkeypatch, capsys, home):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HOME", str(tmp_path))  # POSIX path resolution
        rc = main(["completion", "bash", "--install"])
        assert rc == 0
        target = tmp_path / "xdg" / "bash-completion" / "completions" / "vex"
        assert target.is_file()
        assert "complete" in target.read_text(encoding="utf-8")

    def test_install_powershell_appends_profile(
        self, tmp_path, monkeypatch, capsys, home
    ):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        docs = tmp_path / "Documents" / "WindowsPowerShell"
        docs.mkdir(parents=True)
        rc = main(["completion", "powershell", "--install"])
        assert rc == 0
        profile = docs / "Microsoft.PowerShell_profile.ps1"
        assert profile.exists()
        assert "Register-ArgumentCompleter" in profile.read_text(encoding="utf-8")
        # idempotent: second run doesn't duplicate
        rc2 = main(["completion", "powershell", "--install"])
        assert rc2 == 0
        body = profile.read_text(encoding="utf-8")
        assert body.count("Register-ArgumentCompleter") == 1

    def test_unknown_shell_is_usage_error(self, home):
        with pytest.raises(SystemExit) as ei:
            main(["completion", "tcsh"])
        assert ei.value.code == 2

    def test_no_shell_without_install_is_usage_error(self, home, capsys):
        rc = main(["completion"])
        assert rc == 2
        assert "bash" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Task E — vex update
# ---------------------------------------------------------------------------


class TestSelfUpdate:
    def test_detect_install_method_source(self):
        """This repo IS a source checkout — the honest refusal path."""
        method, desc = selfupdate.detect_install_method()
        assert method == "source"
        assert "source" in desc

    def test_update_on_source_refuses_with_recipe(self, home, capsys):
        rc = main(["update"])
        assert rc == 1
        out = capsys.readouterr().out + ""
        assert "git pull" in out

    def test_check_reports_installed(self, home, capsys, monkeypatch):
        monkeypatch.setattr(
            selfupdate, "latest_available_version", lambda timeout_s=5: "0.2.0"
        )
        rc = main(["update", "--check"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "installed" in out
        assert "up to date" in out

    def test_check_detects_newer(self, home, capsys, monkeypatch):
        monkeypatch.setattr(
            selfupdate, "latest_available_version", lambda timeout_s=5: "99.0.0"
        )
        rc = main(["update", "--check"])
        assert rc == 0
        assert "update available" in capsys.readouterr().out

    def test_check_offline_is_network_error(self, home, capsys, monkeypatch):
        monkeypatch.setattr(
            selfupdate, "latest_available_version", lambda timeout_s=5: None
        )
        rc = main(["update", "--check"])
        assert rc == 4  # network error category
        assert "cannot reach" in capsys.readouterr().err

    def test_update_venv_method_upgrades(self, tmp_path, monkeypatch, capsys, home):
        """Install method venv -> the ~/.vex-venv python runs pip."""
        fake_python = tmp_path / "fakepython"
        calls = []

        monkeypatch.setattr(selfupdate.sys, "executable", str(fake_python))
        monkeypatch.setattr(
            selfupdate, "detect_install_method", lambda: ("venv", "the venv")
        )

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return 0  # same shape as the real _run: the returncode

        monkeypatch.setattr(selfupdate, "_run", fake_run)
        monkeypatch.setattr(selfupdate, "installed_version", lambda: "0.2.0")
        rc = main(["update"])
        assert rc == 0
        assert calls and "pip" in " ".join(calls[0])
        assert "--upgrade" in calls[0]
        out = capsys.readouterr().out
        assert "0.2.0" in out

    def test_pipx_method_uses_pipx(self, tmp_path, monkeypatch, capsys, home):
        calls = []

        monkeypatch.setattr(
            selfupdate, "detect_install_method", lambda: ("pipx", "pipx")
        )

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return 0  # the returncode (never actually spawned)

        monkeypatch.setattr(selfupdate, "_run", fake_run)
        monkeypatch.setattr(selfupdate, "installed_version", lambda: "0.2.0")
        rc = main(["update"])
        assert rc == 0
        assert calls[0][1:3] == ["upgrade", "vex-harness"]
        assert "updated to 0.2.0" in capsys.readouterr().out

    def test_update_failure_is_one(self, tmp_path, monkeypatch, capsys, home):
        monkeypatch.setattr(selfupdate, "detect_install_method", lambda: ("pip", "pip"))

        def failing_run(cmd, **kw):
            import types

            return types.SimpleNamespace(returncode=1)

        monkeypatch.setattr(selfupdate, "_run", failing_run)
        rc = main(["update"])
        assert rc == 1

    def test_install_source_env_override(self, monkeypatch):
        monkeypatch.setenv("VEX_INSTALL_SOURCE", "./local/dist")
        assert selfupdate.install_source() == "./local/dist"


# ---------------------------------------------------------------------------
# Task F — clean uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_dry_run_lists_and_removes_nothing(
        self, tmp_path, monkeypatch, capsys, home
    ):
        """Dry run shows the plan, touches nothing, exits 0."""
        import cli.uninstall as un

        fake_root = tmp_path / "roaming" / "vex"
        fake_root.mkdir(parents=True)
        monkeypatch.setattr(un, "settings_roots", lambda: [fake_root])
        rc = main(["uninstall", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "config directory" in out
        assert "nothing removed" in out
        assert fake_root.exists()  # untouched

    def test_yes_removes_config_roots(self, tmp_path, monkeypatch, capsys, home):
        import cli.uninstall as un

        fake_root = tmp_path / "roaming" / "vex"
        fake_root.mkdir(parents=True)
        (fake_root / "settings.toml").write_text("", encoding="utf-8")
        monkeypatch.setattr(un, "settings_roots", lambda: [fake_root])
        # no installer venv/bin on this machine (host-independent test)
        monkeypatch.setattr(un, "installer_venv", lambda: tmp_path / "nope-venv")
        monkeypatch.setattr(un, "installer_bin", lambda: tmp_path / "nope" / "bin")
        monkeypatch.setattr(un, "pipx_venv_dir", lambda: None)
        rc = main(["uninstall", "--yes"])
        assert rc == 0
        assert not fake_root.exists()

    def test_confirm_abort_removes_nothing(self, tmp_path, monkeypatch, capsys, home):
        import cli.uninstall as un

        fake_root = tmp_path / "roaming" / "vex"
        fake_root.mkdir(parents=True)
        monkeypatch.setattr(un, "settings_roots", lambda: [fake_root])

        class FakeInput:
            def __call__(self, prompt):
                return "n"

        monkeypatch.setattr("builtins.input", FakeInput())
        rc = main(["uninstall"])
        assert rc == 0
        assert fake_root.exists()

    def test_nothing_found_is_clean(self, tmp_path, monkeypatch, capsys, home):
        import cli.uninstall as un

        monkeypatch.setattr(un, "settings_roots", lambda: [])
        monkeypatch.setattr(un, "installer_venv", lambda: tmp_path / "nope-venv")
        monkeypatch.setattr(un, "installer_bin", lambda: tmp_path / "nope" / "bin")
        monkeypatch.setattr(un, "pipx_venv_dir", lambda: None)
        monkeypatch.setattr(un, "_bin_on_user_path", lambda d: False)
        rc = main(["uninstall", "--yes"])
        assert rc == 0
        assert "nothing Vex-created" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Task G — --json output mode
# ---------------------------------------------------------------------------


class ScriptedFixModel:
    """Same scripted shape as test_cli.py's (fixes the fixture bug)."""

    def __init__(self):
        self.step = 0

    def get_last_usage(self):
        return {
            "model": "scripted",
            "provider": "test",
            "tokens": 1,
            "cost_usd": 0.0,
        }

    def __call__(
        self, messages, difficulty_hint=None, provider=None, model=None, api_key=None
    ):
        self.step += 1
        first_user = messages[0].get("content", "") if messages else ""
        if any("planning a bug fix" in m.get("content", "") for m in messages):
            return json.dumps(
                {
                    "analysis": "mean() returns sum; divide by len",
                    "plan": [
                        {
                            "id": 1,
                            "description": "fix mean() to divide by len(values)",
                            "checkpoint": "tests pass",
                            "files_hint": ["mathutil.py"],
                        }
                    ],
                }
            )
        if self.step in (2,) or (self.step > 2 and "Begin" in first_user):
            return (
                "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
                "s = p.read_text(); s = s.replace('return sum(values)', "
                "'return sum(values) / len(values)'); p.write_text(s)\""
            )
        if self.step > 2:
            return "SUBMIT"
        return "SUBMIT"


class TestJsonMode:
    def test_fix_json_success_is_parseable(
        self, smoke_repo_fix, home, logs_root, capsys, monkeypatch
    ):
        import harness.deps as hdeps

        hdeps.set_call_model(ScriptedFixModel())
        monkeypatch.chdir(smoke_repo_fix.parent)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "mean() in mathutil.py returns the sum; make it the mean",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
                "--json",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)  # the WHOLE stdout is the JSON
        assert payload["status"] == "success"
        assert payload["verification"]["target_test_passed"] is True
        assert payload["verification"]["regression_passed"] is True
        assert payload["exit_code"] == 0
        assert payload["exit_reason"] == "success"
        assert "diff" in payload and "mathutil" in payload["diff"]

    def test_fix_json_failure_exit_code_field(
        self, smoke_repo_fix, home, logs_root, capsys, monkeypatch
    ):
        import harness.deps as hdeps

        class NoFixModel(ScriptedFixModel):
            def __call__(self, *a, **k):
                return "SUBMIT"

        hdeps.set_call_model(NoFixModel())
        monkeypatch.chdir(smoke_repo_fix.parent)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "mean() returns the sum; make it the mean",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
                "--json",
            ]
        )
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        # a never-editing model ends failed OR error — either way NOT
        # success, and the machine fields say exactly that
        assert payload["status"] in ("failed", "error")
        assert payload["exit_code"] == 1
        assert payload["exit_reason"] == "task_failure"

    def test_status_json_shape(self, logs_root, home, capsys):
        d = logs_root / "task-json"
        d.mkdir(parents=True)
        (d / "state.json").write_text(
            json.dumps(
                {
                    "task_id": "task-json",
                    "plan": ["1. fix mean", "2. verify"],
                    "completed_steps": ["1. fix mean"],
                    "files_touched": ["mathutil.py"],
                    "decisions": ["chose minimal patch"],
                    "remaining_plan": ["2. verify"],
                }
            ),
            encoding="utf-8",
        )
        (d / "trace.jsonl").write_text(
            json.dumps(
                {"kind": "result", "data": {"status": "success", "cost_usd": 0.01}}
            )
            + "\n",
            encoding="utf-8",
        )
        rc = main(["status", "--task-id", "task-json", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["task_id"] == "task-json"
        assert payload["progress"]["plan_steps"] == 2
        assert payload["progress"]["completed_steps"] == ["1. fix mean"]
        assert payload["progress"]["remaining_steps"] == ["2. verify"]
        assert payload["files_touched"] == ["mathutil.py"]
        assert payload["result"]["status"] == "success"
        assert payload["result"]["cost_usd"] == 0.01

    def test_status_json_missing_is_two(self, logs_root, home, capsys):
        rc = main(["status", "--task-id", "never-was", "--json"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no readable state" in err

    def test_status_json_invalid_id_is_two(self, logs_root, home):
        rc = main(["status", "--task-id", "../escape", "--json"])
        assert rc == 2

    def test_json_output_has_no_theme_markup(
        self, smoke_repo_fix, home, logs_root, monkeypatch, capsys
    ):
        """--json stdout must not contain rich markup or spinner frames —
        only the machine-readable document."""
        import harness.deps as hdeps

        hdeps.set_call_model(ScriptedFixModel())
        monkeypatch.chdir(smoke_repo_fix.parent)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "mean() in mathutil.py returns the sum; make it the mean",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
                "--json",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "[vex." not in out
        json.loads(out)  # still parses after the markup assertion

    def test_json_diverts_foreign_stdout_printers(
        self, smoke_repo_fix, home, logs_root, monkeypatch, capsys
    ):
        """REGRESSION (found live): litellm's error banner prints to
        STDOUT — in --json mode the run's stdout is diverted to stderr so
        the final document stays the ONLY thing on stdout (a real
        no-credentials run polluted the JSON before the divert)."""
        import harness.deps as hdeps

        class LoudBrokenModel(ScriptedFixModel):
            def __call__(self, *a, **k):
                # simulate litellm's stdout noise mid-run, then fail
                print("LiteLLM.Info: give feedback at ...", flush=True)
                raise RuntimeError("litellm.APIConnectionError: refused")

        hdeps.set_call_model(LoudBrokenModel())
        monkeypatch.chdir(smoke_repo_fix.parent)
        rc = main(
            [
                "fix",
                "--repo",
                str(smoke_repo_fix),
                "--issue",
                "anything",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
                "--json",
            ]
        )
        captured = capsys.readouterr()
        # run_task OWNS model failures (structured "error" result, not an
        # escaping exception) -> the normal result path: exit 1. The
        # model_error category (4) is for exceptions that ESCAPE run_task
        # (pinned separately in TestExitCodes).
        assert rc == 1
        # stdout is the pure JSON document (or empty + stderr error) —
        # never the foreign printer's noise
        assert "LiteLLM.Info" not in captured.out
        assert "LiteLLM.Info" in captured.err
        if captured.out.strip():
            payload = json.loads(captured.out)
            assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# Cross-task: the "first 30 seconds" manual checklist, automated
# ---------------------------------------------------------------------------


class TestFirstThirtySeconds:
    def test_help_version_failure_path(self, home):
        """The three things a developer tries first: --help, --version,
        and a failure path with a distinct exit code — via the real
        entrypoint seam."""
        cp1 = subprocess.run(
            [sys.executable, "-m", "cli", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert cp1.returncode == 0
        assert "fix" in cp1.stdout
        assert "exit codes" in cp1.stdout

        cp2 = subprocess.run(
            [sys.executable, "-m", "cli", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert cp2.returncode == 0
        assert cp2.stdout.strip().startswith("vex ")

        # config error: bad --tier value
        cp3 = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli",
                "config",
                "set",
                "model",
                "x",
                "--tier",
                "bogus",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert cp3.returncode == 2
        assert "Traceback" not in cp3.stderr

        # usage error: unknown flag
        cp4 = subprocess.run(
            [sys.executable, "-m", "cli", "fix", "--no-such-flag"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert cp4.returncode == 2
        assert "Traceback" not in cp4.stderr
