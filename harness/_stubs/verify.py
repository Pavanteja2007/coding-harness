"""STUB for INTERFACES.md Boundary 1 — verify() — Terminal 2 owns the real one.

Signature matches execution.verify.verify exactly (plus keyword-only
conveniences the harness passes explicitly; see note below). This stub
runs tests via the local sandbox stub (subprocess) and interprets outcome:
- target test pass/fail on whatever repo state it is handed
- regression check: full suite on the same state
- flake check: rerun the target test `rerun_for_flake_check` times

Contract note: INTERFACES.md's verify() takes (repo_path, target_test,
rerun_for_flake_check). The harness needs to pass the test command /
timeout too, so this stub adds two keyword arguments with defaults
(test_command=None autodetects, verify_timeout_s=300). When Terminal 2
lands the real verify(), either it accepts the same kwargs or we
reconcile via INTERFACES.md — flagged in harness/AGENTS.md.

IMPORTANT (semantic note for Terminal 2): this verify() inspects the repo
state it is given — the harness passes the pristine copy for the baseline
and the edited working copy for post-edit verification. baseline_passed in
the returned VerificationResult is left False here; the harness sets it
from the pristine run. If the real implementation wants to own baseline
logic, coordinate via INTERFACES.md.
"""
import os
import re
from typing import Optional

from shared.types import VerificationResult
from harness._stubs.sandbox import execute_sandboxed


def _autodetect_test_command(repo_path: str, timeout_s: int) -> Optional[str]:
    """Find a test command for the repo if none was supplied.

    Assumes a small Python repo: if a pytest config/tests dir exists and
    pytest is importable, use `python -m pytest -q`. Returns None
    otherwise (verify() then reports an explicit "no tests found" result
    instead of guessing).
    """
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "tox.ini")
    has_tests_dir = os.path.isdir(os.path.join(repo_path, "tests"))
    if not (has_tests_dir or any(os.path.exists(os.path.join(repo_path, m)) for m in markers)):
        return None
    check = execute_sandboxed(repo_path, "python -c \"import pytest\"", timeout_s)
    if check.exit_code == 0:
        return "python -m pytest -q"
    return None


def _target_command(target_test: Optional[str], suite_cmd: Optional[str]) -> Optional[str]:
    """Command that runs just the target test (or the suite if no target)."""
    if not target_test:
        return suite_cmd
    if suite_cmd and target_test in suite_cmd:
        return suite_cmd
    if suite_cmd and "pytest" in suite_cmd:
        return f"{suite_cmd} {target_test}"
    return f"python -m pytest -q {target_test}"


def verify(
    repo_path: str,
    target_test: Optional[str],
    rerun_for_flake_check: int = 1,
    test_command: Optional[str] = None,
    verify_timeout_s: int = 300,
) -> VerificationResult:
    """STUB: verify a repo state by running its tests locally (subprocess).

    Assumes `repo_path` is the repo state to evaluate (pristine copy for a
    baseline run, edited copy for post-edit runs — the caller controls
    which). `target_test` is a pytest node id ("file.py::test_name") or
    None, in which case the full-suite exit code is the gate.

    Returns a VerificationResult: target_test_passed reflects the LAST
    target run; flaky is True iff outcomes differed across reruns;
    regression_passed reflects a full-suite run on the same state;
    baseline_passed is always False here (set by the caller from the
    pristine run); raw_output captures everything for the trace log.
    """
    suite_cmd = test_command or _autodetect_test_command(repo_path, verify_timeout_s)
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

    # 1) Target test on the current state (rerun for flake detection).
    outcomes = []
    raw = []
    for _ in range(max(1, rerun_for_flake_check)):
        res = execute_sandboxed(repo_path, target_cmd, verify_timeout_s)
        outcomes.append(res.exit_code == 0)
        raw.append(f"$ {target_cmd}\nexit={res.exit_code}\n{res.stdout}\n{res.stderr}")

    target_passed = outcomes[-1]
    flaky = len(set(outcomes)) > 1

    # 2) Full-suite regression check on the same state (skip if the target
    #    run already was the full suite).
    if target_test and target_cmd != suite_cmd:
        reg = execute_sandboxed(repo_path, suite_cmd, verify_timeout_s)
        regression_passed = reg.exit_code == 0
        raw.append(f"$ {suite_cmd}\nexit={reg.exit_code}\n{reg.stdout}\n{reg.stderr}")
    else:
        regression_passed = target_passed

    return VerificationResult(
        target_test_passed=target_passed,
        baseline_passed=False,
        regression_passed=regression_passed,
        flaky=flaky,
        raw_output="\n\n".join(raw),
    )
