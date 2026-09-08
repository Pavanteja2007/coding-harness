"""Tests for the boundary stubs (sandbox, verify) and harness.deps."""
import os
from pathlib import Path

import pytest

from harness._stubs.sandbox import execute_sandboxed
from harness._stubs.verify import verify
from harness.deps import get_call_model, get_execute_sandboxed, reset_overrides, set_call_model, set_execute_sandboxed
from shared.types import ExecutionResult


@pytest.fixture(autouse=True)
def _clean_overrides():
    reset_overrides()
    yield
    reset_overrides()


def test_sandbox_runs_command_and_captures_output(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi from stub')\n", encoding="utf-8")
    res = execute_sandboxed(str(tmp_path), "python hello.py", timeout_s=60)
    assert isinstance(res, ExecutionResult)
    assert res.exit_code == 0
    assert "hi from stub" in res.stdout
    assert not res.timed_out


def test_sandbox_nonzero_exit_and_stderr(tmp_path):
    res = execute_sandboxed(str(tmp_path), "python -c \"import sys; sys.exit(3)\"", 60)
    assert res.exit_code == 3
    assert not res.timed_out


def test_sandbox_timeout(tmp_path):
    (tmp_path / "slow.py").write_text(
        "import time; time.sleep(30)\n", encoding="utf-8")
    res = execute_sandboxed(str(tmp_path), "python slow.py", timeout_s=5)
    assert res.timed_out
    assert res.exit_code == 124


def test_deps_prefers_injected_over_stubs():
    sentinel = ExecutionResult(0, "injected", "", False)

    def fake_exec(repo, cmd, timeout_s):
        return sentinel

    def fake_model(messages, **kwargs):
        return "injected-model"

    set_execute_sandboxed(fake_exec)
    set_call_model(fake_model)
    assert get_execute_sandboxed()("/repo", "ls", 30) is sentinel
    assert get_call_model()([]) == "injected-model"


def test_deps_falls_back_to_stub_when_real_missing():
    # execution/runtime don't exist yet -> stubs are returned.
    exec_fn = get_execute_sandboxed()
    model_fn = get_call_model()
    assert exec_fn.__module__.endswith("sandbox")
    assert model_fn.__module__.endswith("model_router")


FIXTURES = Path(__file__).parent / "fixtures"


def test_verify_detects_failing_target_on_buggy_fixture():
    repo = str(FIXTURES / "bug03_stack")
    v = verify(repo, "tests/test_stack.py::test_pop_empty_raises_stackemptyerror",
               rerun_for_flake_check=1, verify_timeout_s=120)
    assert v.target_test_passed is False
    assert v.raw_output != ""


def test_verify_full_suite_is_the_gate_without_target():
    repo = str(FIXTURES / "bug01_wrap")
    v = verify(repo, None, rerun_for_flake_check=1, verify_timeout_s=120)
    assert v.target_test_passed is False  # suite has the failing bug test
    assert v.regression_passed is False


def test_verify_no_tests_found_reports_cleanly(tmp_path):
    (tmp_path / "README.md").write_text("not a python project", encoding="utf-8")
    v = verify(str(tmp_path), None, verify_timeout_s=60)
    assert v.target_test_passed is False
    assert "no test command" in v.raw_output
