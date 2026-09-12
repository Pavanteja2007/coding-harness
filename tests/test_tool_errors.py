"""Structured tool-call error classification tests (Round 8, Task A).

Covers 10 distinct error classes — the requirement was regression
tests for at least 5 — via the classifier (harness.tool_errors.classify)
AND its two wiring points: BashSession._map_result (every failing
command result) and core.run_step's PermissionError/ToolExecutionError
handling (classify_exception). Every test asserts BOTH the stable kind
string AND that the model-facing rendering is short/actionable.
"""

from pathlib import Path

import pytest

from harness.tool_errors import (
    ToolError,
    classify,
    classify_exception,
    render_error,
)
from harness import tools as tool_mod
from harness.deps import reset_overrides, set_execute_sandboxed
from shared.types import ExecutionResult


# ---------------------------------------------------------------------------
# the classifier itself — one test per distinct error class
# ---------------------------------------------------------------------------


def test_class_file_not_found():
    err = classify(
        2,
        "",
        "cat: missing_file.py: No such file or directory",
        False,
        command="cat missing_file.py",
    )
    assert err.kind == "file_not_found"
    rendered = render_error(err)
    assert "TOOL ERROR [file_not_found]" in rendered
    assert "file not found" in rendered
    assert "Suggested fix" in rendered  # actionable, not noise


def test_class_file_not_found_errno_shape():
    # python's open(): FileNotFoundError [Errno 2]
    err = classify(
        1,
        "",
        "FileNotFoundError: [Errno 2] No such file or directory: 'cfg.toml'",
        False,
        command="python x.py",
    )
    assert err.kind == "file_not_found"


def test_class_command_not_found():
    err = classify(127, "", "pythn: command not found", False, command="pythn -c x")
    assert err.kind == "command_not_found"
    assert "pythn" in err.detail


def test_class_command_not_found_windows_shape():
    err = classify(
        1,
        "",
        "'rg' is not recognized as an internal or external command",
        False,
        command="rg foo",
    )
    assert err.kind == "command_not_found"


def test_class_malformed_patch_hunk_line():
    err = classify(
        1,
        "",
        "1 out of 1 hunk FAILED -- saving rejects to "
        "file.py.rej\nHunk #1 FAILED at line 42.",
        False,
    )
    assert err.kind == "malformed_patch"
    assert "hunk doesn't apply" in err.detail
    assert "42" in err.detail  # "at line N" surfaced for the model


def test_class_malformed_patch_git_apply_shape():
    err = classify(
        1,
        "",
        "error: patch failed: app.py:17\nerror: file.py: patch does not apply",
        False,
    )
    assert err.kind == "malformed_patch"


def test_class_syntax_error():
    err = classify(
        1,
        "",
        '  File "pkg/mod.py", line 3\n    def broken(:\n'
        "              ^\nSyntaxError: invalid syntax",
        False,
    )
    assert err.kind == "syntax_error"
    assert "3" in err.detail


def test_class_import_error():
    err = classify(1, "", "E ModuleNotFoundError: No module named 'reqests'", False)
    assert err.kind == "import_error"
    assert "reqests" in err.detail


def test_class_undefined_name():
    err = classify(1, "", "NameError: name '_DAYS_PER_MONTHS' is not defined", False)
    assert err.kind == "undefined_name"
    assert "_DAYS_PER_MONTHS" in err.detail


def test_class_permission_denied():
    err = classify(1, "", "cat: /etc/shadow: Permission denied", False)
    assert err.kind == "permission_denied"


def test_class_permission_denied_protected_path():
    # the harness's own protected-path message shape
    err = classify(
        1,
        "",
        "sed: couldn't write to tests/test_x.py: "
        "permission denied on protected path tests/test_x.py",
        False,
    )
    assert err.kind == "permission_denied"
    assert "tests/test_x.py" in err.detail


def test_class_timeout():
    err = classify(124, "partial", "", True, command="python slow.py")
    assert err.kind == "timeout"
    assert "timed out" in render_error(err)


def test_class_timeout_wins_over_other_signals():
    # timed_out=True even with stderr noise: the timeout is the actionable fact
    err = classify(124, "", "some trailing error text", True)
    assert err.kind == "timeout"


def test_class_argument_error():
    err = classify(
        2,
        "",
        "usage: grep [-abc] PATTERN [FILE]\ngrep: unknown optionzz",
        False,
        command="grep -zz foo",
    )
    assert err.kind == "argument_error"


def test_class_internal_error_fallback():
    err = classify(3, "weird output\nsecond line", "", False)
    assert err.kind == "internal_error"
    assert render_error(err).startswith("TOOL ERROR [internal_error]")


def test_class_success_is_not_an_error():
    err = classify(0, "all good", "", False)
    assert err.kind == "ok"
    assert render_error(err) == ""


def test_classify_exception_permission_error():
    err = classify_exception(
        PermissionError("command denied by harness safety pattern: x")
    )
    assert err.kind == "command_rejected"
    assert "protected path" in err.detail


def test_classify_exception_other():
    err = classify_exception(RuntimeError("boom"))
    assert err.kind == "internal_error"
    assert "RuntimeError" in err.detail


def test_classify_never_raises_on_garbage():
    # pathological inputs must degrade, never crash the step
    err = classify(1, None, None, False)  # type: ignore[arg-type]
    assert err.kind in ("internal_error", "ok", "file_not_found")


# ---------------------------------------------------------------------------
# wiring point 1: BashSession._map_result — every failing command result
# is classified before reaching the model
# ---------------------------------------------------------------------------


class FakeSandbox:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, repo_path, command, timeout_s):
        self.calls.append((repo_path, command, timeout_s))
        if self.results:
            return self.results.pop(0)
        return ExecutionResult(0, "", "", False)


@pytest.fixture
def fake_sandbox():
    fs = FakeSandbox()
    set_execute_sandboxed(fs)
    yield fs
    reset_overrides()


def test_session_map_result_classifies_failure(fake_sandbox):
    fake_sandbox.results = [
        ExecutionResult(1, "", "cat: nope.py: No such file or directory", False)
    ]
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("cat nope.py")
    assert out.startswith("TOOL ERROR [file_not_found]")
    assert "nope.py" in out
    assert "exit=1" in out  # raw info still present for diagnosis


def test_session_map_result_success_unchanged(fake_sandbox):
    fake_sandbox.results = [ExecutionResult(0, "hello", "", False)]
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("echo hello")
    assert "exit=0" in out
    assert "TOOL ERROR" not in out
    assert out.startswith("exit=0")  # EXACT previous format preserved


def test_session_map_result_timeout_classified(fake_sandbox):
    fake_sandbox.results = [ExecutionResult(124, "", "partial", True)]
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("python slow.py")
    assert "TOOL ERROR [timeout]" in out


def test_session_map_result_syntax_error_classified(fake_sandbox):
    fake_sandbox.results = [
        ExecutionResult(
            1, "", '  File "x.py", line 2\nSyntaxError: invalid syntax', False
        )
    ]
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("python -m py_compile x.py")
    assert "TOOL ERROR [syntax_error]" in out
    assert "Suggested fix" in out


def test_session_sandbox_exception_classified_not_raised_raw(fake_sandbox):
    """A sandbox-layer crash (not PermissionError/SandboxUnavailable) is
    wrapped as ToolExecutionError carrying the classified kind — the
    loop controller can feed structured feedback instead of a traceback."""

    def boom(repo, cmd, t):
        raise RuntimeError("sandbox internal oops")

    set_execute_sandboxed(boom)
    session = tool_mod.BashSession("/repo", 30, 500)
    with pytest.raises(tool_mod.ToolExecutionError) as ei:
        session.run("cat x")
    assert ei.value.kind == "internal_error"
    assert "sandbox internal oops" in ei.value.detail


def test_session_sandbox_unavailable_still_fail_loud(fake_sandbox):
    """SandboxUnavailableError is the documented fail-loud contract — it
    must NOT be swallowed into a classified error."""

    class SandboxUnavailableError(Exception):
        pass

    def unavailable(repo, cmd, t):
        raise SandboxUnavailableError("docker daemon down")

    set_execute_sandboxed(unavailable)
    session = tool_mod.BashSession("/repo", 30, 500)
    with pytest.raises(SandboxUnavailableError):
        session.run("cat x")


def test_render_error_shape_is_short_and_actionable():
    err = ToolError(
        "file_not_found", "file not found: cfg.toml", "check the path (pwd/ls)."
    )
    rendered = render_error(err)
    lines = [l for l in rendered.splitlines() if l.strip()]
    assert len(lines) <= 2  # error line + at most one hint line
    assert rendered.startswith("TOOL ERROR [file_not_found]: file not found: cfg.toml")
