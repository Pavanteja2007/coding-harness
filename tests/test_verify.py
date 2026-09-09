"""Tests for execution.verify — target/regression/flake logic.

Unit tests cover pure helpers (no Docker). Docker-gated integration tests
run verify() end-to-end against small synthetic repos in tmp_path, covering
the four semantic outcomes:
- all green: target passes, suite passes, not flaky
- broken target: target fails (the harness's fix hasn't landed yet)
- regression: target passes but suite fails (fix broke something else)
- flaky: target outcome differs across reruns (flagged, not reported as
  a stable pass or fail — the core DoD requirement for this module)
"""
import os
import subprocess
from pathlib import Path

import pytest

import execution.verify as vf  # NOT "from execution import verify" — __init__.py
# re-exports the *function* verify, which shadows the module attribute.
import execution.sandbox as sb
from shared.types import VerificationResult


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------

class TestAutodetect:
    def test_tests_dir_triggers_pytest(self, tmp_path):
        (tmp_path / "tests").mkdir()
        assert vf._autodetect_test_command(str(tmp_path)) == "python -m pytest -q"

    def test_pytest_marker_file_triggers(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        assert vf._autodetect_test_command(str(tmp_path)) == "python -m pytest -q"

    def test_bare_repo_returns_none(self, tmp_path):
        (tmp_path / "README.md").write_text("nothing here", encoding="utf-8")
        assert vf._autodetect_test_command(str(tmp_path)) is None


class TestTargetCommand:
    def test_none_target_uses_suite(self):
        assert vf._target_command(None, "python -m pytest -q") == "python -m pytest -q"

    def test_node_id_appended(self):
        cmd = vf._target_command("tests/test_x.py::test_y", "python -m pytest -q")
        assert cmd == "python -m pytest -q tests/test_x.py::test_y"

    def test_target_embedded_in_command_wins(self):
        cmd = vf._target_command(
            "tests/test_x.py::test_y", "python -m pytest -q tests/test_x.py::test_y")
        assert cmd == "python -m pytest -q tests/test_x.py::test_y"

    def test_no_suite_falls_back_to_plain_pytest(self):
        assert vf._target_command("test_a.py::test_b", None) == \
            "python -m pytest -q test_a.py::test_b"


class TestFormatRun:
    def test_rendering_includes_command_exit_and_output(self):
        from shared.types import ExecutionResult
        res = ExecutionResult(exit_code=1, stdout="out", stderr="err",
                              timed_out=False)
        text = vf._format_run("cmd", res)
        assert text.startswith("$ cmd\nexit=1\n")
        assert "out" in text and "err" in text
        assert "TIMEOUT" not in text

    def test_timeout_annotated(self):
        from shared.types import ExecutionResult
        res = ExecutionResult(exit_code=124, stdout="", stderr="",
                              timed_out=True)
        assert "TIMEOUT" in vf._format_run("cmd", res)


# ---------------------------------------------------------------------------
# Integration tests — Docker required
# ---------------------------------------------------------------------------

def _mk_repo(tmp_path: Path, scenario: str) -> Path:
    """Build a synthetic repo for the given scenario.

    Scenarios:
    - green:   one passing test
    - broken:  module bug makes the target test fail, suite fails too
    - regress: target passes but another suite test fails
    - flaky:   order-dependent state leak — run 1 pass, run 2 fail
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\".\"]\n", encoding="utf-8")
    if scenario == "green":
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        (tmp_path / "test_mymod.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8")
    elif scenario == "broken":
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a - b  # bug\n", encoding="utf-8")
        (tmp_path / "test_mymod.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8")
    elif scenario == "regress":
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n"
            "    return 0  # bug: breaks the OTHER test\n", encoding="utf-8")
        (tmp_path / "test_target.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8")
        (tmp_path / "test_other.py").write_text(
            "from mymod import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
            encoding="utf-8")
    elif scenario == "flaky":
        (tmp_path / "test_flaky.py").write_text(
            "import os\n\n"
            "MARKER = os.path.join(os.path.dirname(__file__), \".flaky_marker\")\n\n"
            "def test_flaky():\n"
            "    # order-dependent state leak: passes 1st run, fails 2nd\n"
            "    if os.path.exists(MARKER):\n"
            "        os.remove(MARKER)\n"
            "        assert False, \"second run fails\"\n"
            "    open(MARKER, \"w\").close()\n",
            encoding="utf-8")
    elif scenario == "hang-then-fail":
        # Round-4 audit case: run 1 HANGS (times out), run 2 fails fast.
        # Pre-fix bug: timeout was collapsed into "fail", so the mixed
        # outcomes read as a stable failure — never flagged flaky. The
        # marker makes the test sleep forever on the first run only.
        (tmp_path / "test_hang.py").write_text(
            "import os\n"
            "import time\n\n"
            "MARKER = os.path.join(os.path.dirname(__file__), \".hang_marker\")\n\n"
            "def test_hangs_first():\n"
            "    if not os.path.exists(MARKER):\n"
            "        open(MARKER, \"w\").close()\n"
            "        time.sleep(300)  # exceeds verify_timeout_s on run 1\n"
            "    assert False, \"second run fails fast\"\n",
            encoding="utf-8")
    elif scenario == "pass-then-hang":
        # Mirror case: run 1 passes, run 2 hangs -> pass/timeout mix.
        (tmp_path / "test_hang2.py").write_text(
            "import os\n"
            "import time\n\n"
            "MARKER = os.path.join(os.path.dirname(__file__), \".hang2_marker\")\n\n"
            "def test_passes_then_hangs():\n"
            "    if os.path.exists(MARKER):\n"
            "        time.sleep(300)  # hangs on run 2\n"
            "    else:\n"
            "        open(MARKER, \"w\").close()\n",
            encoding="utf-8")
    return tmp_path


@requires_docker
class TestVerifyIntegration:
    def test_green_repo_all_true(self, tmp_path):
        repo = _mk_repo(tmp_path, "green")
        v = vf.verify(str(repo), "test_mymod.py::test_add", 2,
                      verify_timeout_s=120)
        assert v.target_test_passed is True
        assert v.flaky is False
        assert v.regression_passed is True
        assert v.baseline_passed is False  # caller-owned field, always False

    def test_broken_target_detected(self, tmp_path):
        repo = _mk_repo(tmp_path, "broken")
        v = vf.verify(str(repo), "test_mymod.py::test_add", 2,
                      verify_timeout_s=120)
        assert v.target_test_passed is False
        assert v.flaky is False

    def test_regression_detected_when_target_passes(self, tmp_path):
        repo = _mk_repo(tmp_path, "regress")
        v = vf.verify(str(repo), "test_target.py::test_add", 1,
                      verify_timeout_s=120)
        assert v.target_test_passed is True
        assert v.regression_passed is False  # test_other fails in the suite

    def test_flaky_flagged_not_misreported(self, tmp_path):
        repo = _mk_repo(tmp_path, "flaky")
        # NOTE: fresh marker state needed — the scenario test above may have
        # consumed it. _mk_repo's tmp_path is per-test, so we're clean.
        v = vf.verify(str(repo), "test_flaky.py::test_flaky", 2,
                      verify_timeout_s=120)
        # Either (pass, fail) or (fail, pass): flaky MUST be True, and the
        # reported target outcome must be the LAST run's, never a stale one.
        assert v.flaky is True
        assert v.target_test_passed in (True, False)

    def test_timeout_fail_mix_flagged_flaky(self, tmp_path):
        # Round-4 audit regression: run 1 TIMES OUT (short verify_timeout),
        # run 2 fails fast. Timeout is a DISTINCT outcome — the mix must be
        # flagged flaky, never read as a stable failure (the pre-fix bug:
        # both runs collapsed to "fail" so flaky was False).
        repo = _mk_repo(tmp_path, "hang-then-fail")
        v = vf.verify(str(repo), "test_hang.py::test_hangs_first", 2,
                      verify_timeout_s=15)
        assert v.flaky is True
        assert v.target_test_passed is False  # last run failed fast
        assert "TIMEOUT" in v.raw_output       # run 1's timeout is visible

    def test_pass_timeout_mix_flagged_flaky(self, tmp_path):
        # Mirror case: run 1 passes, run 2 times out -> flaky (the pass/
        # timeout mix previously collapsed to a stable pass — the worst
        # variant, since a hanging test would read as fully verified).
        repo = _mk_repo(tmp_path, "pass-then-hang")
        v = vf.verify(str(repo), "test_hang2.py::test_passes_then_hangs", 2,
                      verify_timeout_s=15)
        assert v.flaky is True

    def test_no_test_command_reports_cleanly(self, tmp_path):
        (tmp_path / "README.md").write_text("not a test repo", encoding="utf-8")
        v = vf.verify(str(tmp_path), None, 1, verify_timeout_s=60)
        assert v.target_test_passed is False
        assert "no test command" in v.raw_output

    def test_rerun_zero_still_runs_once(self, tmp_path):
        # harness.core calls rerun_for_flake_check=0 for the baseline run —
        # that must mean ONE run, not zero (a crash would read as "passed").
        repo = _mk_repo(tmp_path, "green")
        v = vf.verify(str(repo), "test_mymod.py::test_add", 0,
                      verify_timeout_s=120)
        assert v.target_test_passed is True
        assert v.flaky is False

    def test_explicit_test_command_respected(self, tmp_path):
        repo = _mk_repo(tmp_path, "green")
        v = vf.verify(str(repo), "test_mymod.py::test_add", 1,
                      test_command="python -m pytest -q test_mymod.py",
                      verify_timeout_s=120)
        assert v.target_test_passed is True

    def test_raw_output_contains_commands_and_exits(self, tmp_path):
        repo = _mk_repo(tmp_path, "green")
        v = vf.verify(str(repo), "test_mymod.py::test_add", 1,
                      verify_timeout_s=120)
        assert "$ python -m pytest -q test_mymod.py::test_add" in v.raw_output
        assert "exit=" in v.raw_output
