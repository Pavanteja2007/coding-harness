"""Test-based verification (INTERFACES.md Boundary 1).

Contract semantics (matches harness/_stubs/verify.py, which Terminal 1's
core.py already calls with these exact kwargs — see the Change Log):

verify() is a STATELESS evaluator of one repo state: it runs the target
test (N times for flake detection), then the full suite for regression.
WHICH state (pristine vs. edited) is the caller's choice of repo_path:
- baseline: the harness calls verify() on its pristine copy with
  rerun_for_flake_check=0 BEFORE any edit and reads target_test_passed;
- post-edit: the harness calls verify() on the working copy.

baseline_passed in the returned VerificationResult is therefore always
False here — only the harness knows both states; it fills the field from
the pristine run (core.py: _with_baseline). This division is deliberate:
verify() cannot recover the pristine state from an already-edited
repo_path, so pretending it could would be false precision.

Test runs execute in the Docker sandbox (execution.sandbox), so they are
isolated, resource-limited, and networkless by default. pytest is baked
into the sandbox base image; the repo's own deps come from its per-repo
image. The repo's package is never pip-installed (see sandbox.py docstring)
so tests always exercise the bind-mounted source.

Pytest exit-code map (docs: pytest docs "usage"):
  0 all passed; 1 failures; 2 interrupted; 3 internal error; 4 usage error;
  5 no tests collected. Any nonzero => not passed. A run that TIMES OUT
  counts as a THIRD, distinct outcome for flake detection (a test that
  sometimes hangs is flaky by definition) — "pass"/"fail"/"timeout",
  so pass/timeout and fail/timeout mixes are flagged flaky too, not
  just pass/fail mixes.
"""
import os
from typing import List, Optional

from shared.types import ExecutionResult, VerificationResult
from execution.sandbox import execute_sandboxed

_TIMEOUT_EXIT = 124  # sandbox/GNU-timeout convention

# Files that mark a repo as pytest-based (mirrors the stub's autodetect).
_PYTEST_MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "tox.ini")


def _autodetect_test_command(repo_path: str) -> Optional[str]:
    """Return 'python -m pytest -q' if the repo looks pytest-based.

    Assumes a Python repo (locked tech decision: Python-only for now).
    The sandbox image ships pytest, so no importability probe is needed
    (unlike the host-side stub). Returns None when nothing marks the repo
    as testable — verify() then reports an explicit "no tests found"
    result instead of guessing a command.
    """
    has_tests_dir = os.path.isdir(os.path.join(repo_path, "tests"))
    has_marker = any(
        os.path.exists(os.path.join(repo_path, m)) for m in _PYTEST_MARKERS
    )
    if has_tests_dir or has_marker:
        return "python -m pytest -q"
    return None


def _target_command(target_test: Optional[str], suite_cmd: Optional[str]) -> Optional[str]:
    """Command that runs just the target test (or the suite if no target).

    Assumes target_test is a pytest node id ("file.py::test_name") or None.
    If the caller's suite command already embeds the target node id, it is
    used as-is. If the suite command is a pytest invocation, the target is
    APPENDED to it — the caller's command may carry flags the repo needs
    (e.g. '-o addopts=' to bypass pytest-cov-only addopts), which must not
    be dropped for target runs. Only with no suite command at all do we
    fall back to a plain 'python -m pytest -q {target}'.
    """
    if not target_test:
        return suite_cmd
    if suite_cmd and target_test in suite_cmd:
        return suite_cmd
    if suite_cmd and "pytest" in suite_cmd:
        return f"{suite_cmd} {target_test}"
    return f"python -m pytest -q {target_test}"


def _format_run(cmd: str, res: ExecutionResult) -> str:
    """Human/trace-friendly rendering of one sandbox run."""
    return (
        f"$ {cmd}\nexit={res.exit_code}"
        + (" TIMEOUT" if res.timed_out else "")
        + f"\n{res.stdout}\n{res.stderr}"
    )


def verify(
    repo_path: str,
    target_test: Optional[str],
    rerun_for_flake_check: int = 1,
    test_command: Optional[str] = None,
    verify_timeout_s: int = 300,
    *,
    allow_network: bool = False,
) -> VerificationResult:
    """Evaluate one repo state: target test (with flake check) + full suite.

    Assumes repo_path is an existing directory (the state to evaluate —
    pristine for a baseline call, edited for post-edit calls; the caller
    controls which). target_test is a pytest node id or None (None means
    the full-suite exit code IS the target gate). rerun_for_flake_check
    is the TOTAL number of target runs: >=2 enables flake detection;
    0/1 means a single run (flaky can never be True). Flake detection
    uses three outcome labels — pass / fail / timeout — so a run that
    mixes a timeout with a pass or fail is flagged flaky (not silently
    read as a consistent failure).

    Runs tests inside the Docker sandbox (networkless unless
    allow_network=True — some tasks' tests genuinely need it).

    Returns a VerificationResult; baseline_passed is always False (only
    the caller can know the pristine outcome — see module docstring);
    raw_output carries every command, exit code, and output for the trace.
    Raises only from the sandbox layer (e.g. SandboxUnavailableError) —
    test failures themselves are returned, never raised.
    """
    suite_cmd = test_command or _autodetect_test_command(repo_path)
    if suite_cmd is None:
        return VerificationResult(
            target_test_passed=False,
            baseline_passed=False,
            regression_passed=False,
            flaky=False,
            raw_output=(
                "verify(): no test command found for repo "
                "(no pytest config/tests dir and none supplied)"
            ),
        )

    target_cmd = _target_command(target_test, suite_cmd)
    raw: List[str] = []

    # 1) Target test, rerun_for_flake_check times total (min 1).
    #    Outcome labels are three-valued, NOT pass/fail booleans: a timed-
    #    out run is "timeout" (distinct from "fail"), so a pass/timeout or
    #    fail/timeout mix across reruns IS flagged flaky (a test that
    #    sometimes hangs is flaky by definition — INTERFACES.md's
    #    "a timeout counts as a distinct outcome").
    outcomes: List[str] = []
    for _ in range(max(1, rerun_for_flake_check)):
        res = execute_sandboxed(
            repo_path, target_cmd, verify_timeout_s, allow_network=allow_network
        )
        if res.timed_out or res.exit_code == _TIMEOUT_EXIT:
            outcomes.append("timeout")
        else:
            outcomes.append("pass" if res.exit_code == 0 else "fail")
        raw.append(_format_run(target_cmd, res))

    target_passed = outcomes[-1] == "pass"
    flaky = len(set(outcomes)) > 1

    # 2) Regression: full suite on the same state. Skipped when the target
    #    run already WAS the full suite (no target, or the caller's command
    #    embeds it). Runs even if the target failed: the harness needs the
    #    suite signal either way (e.g. "suite fine, target broken").
    if target_cmd != suite_cmd:
        reg = execute_sandboxed(
            repo_path, suite_cmd, verify_timeout_s, allow_network=allow_network
        )
        regression_passed = reg.exit_code == 0
        raw.append(_format_run(suite_cmd, reg))
    else:
        regression_passed = target_passed

    return VerificationResult(
        target_test_passed=target_passed,
        baseline_passed=False,  # caller fills this from the pristine run
        regression_passed=regression_passed,
        flaky=flaky,
        raw_output="\n\n".join(raw),
    )
