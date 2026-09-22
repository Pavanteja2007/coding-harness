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
isolated, resource-limited, and networkless by default. The sandbox base
image ships pytest for Python repos and node+npm for JS/TS repos (see
sandbox.py); the repo's own deps come from its per-repo image. The repo's
package is never installed (pip OR npm link) so tests always exercise
the bind-mounted source.

The verification LOGIC (baseline pass, regression check, three-valued
flake detection with timeout as a distinct outcome) is
language-independent; only the test-invocation command differs per
language:

- Python: pytest exit-code map (docs: pytest "usage"): 0 all passed;
  1 failures; 2 interrupted; 3 internal error; 4 usage error; 5 no tests
  collected. Any nonzero => not passed.
- JS/TS (Jest AND Vitest): exit 0 all passed; 1 test failures; nonzero
  => not passed. Both runners honor a "-t <pattern>" filter that accepts
  a test-name substring; file paths are positional args. Target filtering
  therefore takes the Jest form "<file> -t <name>" for both runners —
  the TEST NAME is the filter, the file is the scope.

A run that TIMES OUT counts as a THIRD, distinct outcome for flake
detection (a test that sometimes hangs is flaky by definition) —
"pass"/"fail"/"timeout", so pass/timeout and fail/timeout mixes are
flagged flaky too, not just pass/fail mixes.
"""

import json
import os
from typing import List, Optional, Tuple

from shared.types import ExecutionResult, VerificationResult
from execution.sandbox import execute_sandboxed

_TIMEOUT_EXIT = 124  # sandbox/GNU-timeout convention

# Files that mark a repo as pytest-based (mirrors the stub's autodetect).
_PYTEST_MARKERS = (
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "conftest.py",
    "tox.ini",
)


def _detect_language(repo_path: str) -> Optional[str]:
    """'python' | 'javascript' (JS/TS) | None, from the repo's manifests.

    package.json + NO Python markers => 'javascript'. Python markers
    (pytest configs, requirements/pyproject) with or without package.json
    => 'python' (back-compat: every existing repo). A bare tests/ dir
    with no manifests: classified by its CONTENTS (a dir of .ts/.js
    files is JS; anything else — including empty — stays Python, the
    original autodetect behavior). A package.json repo whose tests live
    in tests/ is still 'javascript' because there are no Python markers.
    Assumes repo_path exists.
    """
    has_pytest_marker = any(
        os.path.exists(os.path.join(repo_path, m)) for m in _PYTEST_MARKERS
    )
    if has_pytest_marker:
        return "python"
    pkg = os.path.isfile(os.path.join(repo_path, "package.json"))
    if pkg:
        return "javascript"
    tests_dir = os.path.join(repo_path, "tests")
    if os.path.isdir(tests_dir):
        try:
            names = os.listdir(tests_dir)
        except OSError:
            names = []
        # JS/TS test files in an otherwise manifest-less repo: JS. No
        # package.json but .py test files (or nothing distinguishable):
        # Python (matches the pre-multi-language behavior exactly).
        if any(n.endswith((".ts", ".js", ".tsx", ".jsx", ".mjs")) for n in names):
            return "javascript"
        return "python"
    return None


def _js_test_command(repo_path: str) -> Optional[str]:
    """Suite command for a JS/TS repo: the runner its package.json names.

    Reads package.json's devDependencies/scripts (best-effort, never
    raises): vitest -> "npx vitest run"; jest (or a jest-config file)
    -> "npx jest". Falls back to vitest (the more common default for new
    repos) when the manifest is unreadable but package.json exists.
    Assumes _detect_language already returned "javascript" (package.json
    present or JS test files found).
    """
    runner = None
    pkg_path = os.path.join(repo_path, "package.json")
    try:
        with open(pkg_path, encoding="utf-8", errors="replace") as fh:
            pkg = json.load(fh)
        dev = pkg.get("devDependencies") or {}
        scripts = pkg.get("scripts") or {}
        if any(
            k.startswith("vitest") or "vitest" in str(v)
            for k, v in list(dev.items()) + list(scripts.items())
        ):
            runner = "vitest"
        elif any(
            k.startswith("jest") or "jest" in str(v)
            for k, v in list(dev.items()) + list(scripts.items())
        ):
            runner = "jest"
        elif any(
            os.path.isfile(os.path.join(repo_path, f))
            for f in (
                "jest.config.js",
                "jest.config.cjs",
                "jest.config.mjs",
                "jest.config.ts",
                "jest.config.json",
            )
        ):
            runner = "jest"
    except (OSError, ValueError):
        runner = None
    if runner == "jest":
        return "npx jest"
    if runner == "vitest":
        return "npx vitest run"
    # manifest unreadable but package.json exists: vitest is the default
    if os.path.isfile(pkg_path):
        return "npx vitest run"
    return None


def _autodetect_test_command(repo_path: str) -> Optional[str]:
    """Return the suite command for the repo's detected language.

    Python: 'python -m pytest -q' when pytest markers/tests dir exist.
    JS/TS: the runner package.json declares (vitest/jest), else None.
    Returns None when nothing marks the repo as testable — verify() then
    reports an explicit "no tests found" result instead of guessing.
    """
    lang = _detect_language(repo_path)
    if lang == "javascript":
        return _js_test_command(repo_path)
    if lang == "python":
        has_tests_dir = os.path.isdir(os.path.join(repo_path, "tests"))
        has_marker = any(
            os.path.exists(os.path.join(repo_path, m)) for m in _PYTEST_MARKERS
        )
        if has_tests_dir or has_marker:
            return "python -m pytest -q"
    return None


def _split_js_target(target_test: str) -> Tuple[str, str]:
    """Split a JS/TS target id "<file> - <name>" into (file, name).

    Accepts the canonical "<file> - <test name>" form the harness uses
    (see _target_command below) and tolerant variants: "<file>::<name>"
    (pytest-style habit), and a bare "<name>" (no file scope — filters
    across the whole suite, which both runners support via -t).
    """
    t = target_test.strip()
    if "::" in t:
        f, _, n = t.partition("::")
        return f.strip(), n.strip()
    if " - " in t:
        f, _, n = t.partition(" - ")
        return f.strip(), n.strip()
    return "", t


def _sh_quote(s: str) -> str:
    """Quote a shell word for the sandbox's bash invocation.

    Wraps in single quotes (the POSIX-safe form — everything between
    them is literal) with the one standard escape for an embedded quote.
    Test names routinely contain spaces (e.g. "computes the mean"), so
    an unquoted -t pattern would be word-split by the runner's arg
    parser into a filter over just the first word.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _target_command(
    target_test: Optional[str], suite_cmd: Optional[str], lang: Optional[str] = None
) -> Optional[str]:
    """Command that runs just the target test (or the suite if no target).

    Assumes target_test is a pytest node id ("file.py::test_name") for
    Python, or "<file> - <test name>" / "<file>::<test name>" / "<name>"
    for JS/TS (vitest/jest both filter with -t <substring> over the
    named file's tests). If the caller's suite command already embeds
    the target, it is used as-is. If the suite command is a runner
    invocation, the target is composed onto it preserving the caller's
    flags; only with no suite command at all do we fall back to a plain
    runner + target form.
    """
    if not target_test:
        return suite_cmd
    if suite_cmd and target_test in suite_cmd:
        return suite_cmd
    if lang == "javascript" or (
        suite_cmd and ("vitest" in suite_cmd or "jest" in suite_cmd)
    ):
        f, name = _split_js_target(target_test)
        base = suite_cmd or "npx vitest run"
        # "already embedded" for JS: the suite command scopes the same
        # file AND filters the same test name (either raw form or as a
        # -t argument) — used as-is, preserving the caller's flags.
        if suite_cmd and f and name:
            if f in suite_cmd and (
                name in suite_cmd or _flag_value(suite_cmd, "t") == name
            ):
                return suite_cmd
        if f and name:
            return f"{base} {f} -t {_sh_quote(name)}"
        if name:
            return f"{base} -t {_sh_quote(name)}"
        if f:
            return f"{base} {f}"
        return base
    if suite_cmd and "pytest" in suite_cmd:
        return f"{suite_cmd} {target_test}"
    return f"python -m pytest -q {target_test}"


def _flag_value(cmd: str, flag: str) -> Optional[str]:
    """Value of `-<flag> <value>` (or `-<flag>=<value>`) in cmd, if present."""
    import re as _re

    m = _re.search(rf"(?:^|\s)-{flag}(?:\s+(\S+)|=(\S+))", cmd)
    if not m:
        return None
    return m.group(1) or m.group(2)


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
    controls which). target_test is a pytest node id (Python) or a
    "<file> - <name>" / "<file>::<name>" / "<name>" id (JS/TS vitest/jest);
    None means the full-suite exit code IS the target gate.
    rerun_for_flake_check is the TOTAL number of target runs: >=2 enables
    flake detection; 0/1 means a single run (flaky can never be True).
    Flake detection uses three outcome labels — pass / fail / timeout —
    so a run that mixes a timeout with a pass or fail is flagged flaky
    (not silently read as a consistent failure). Language detection is
    automatic (Python markers vs package.json); an explicit test_command
    overrides it for both target and suite runs.

    Runs tests inside the Docker sandbox (networkless unless
    allow_network=True — some tasks' tests genuinely need it).

    Returns a VerificationResult; baseline_passed is always False (only
    the caller can know the pristine outcome — see module docstring);
    raw_output carries every command, exit code, and output for the trace.
    Raises only from the sandbox layer (e.g. SandboxUnavailableError) —
    test failures themselves are returned, never raised.
    """
    lang = _detect_language(repo_path)
    suite_cmd = test_command or _autodetect_test_command(repo_path)
    if suite_cmd is None:
        return VerificationResult(
            target_test_passed=False,
            baseline_passed=False,
            regression_passed=False,
            flaky=False,
            raw_output=(
                "verify(): no test command found for repo "
                "(no pytest config/tests dir, no package.json runner, "
                "and none supplied)"
            ),
        )

    target_cmd = _target_command(target_test, suite_cmd, lang)
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
