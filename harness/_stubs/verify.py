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


def _detect_language(repo_path: str) -> Optional[str]:
    """'python' | 'javascript' | None — mirrors execution.verify's policy.

    Python markers win over package.json; package.json alone means JS/TS;
    a bare tests/ dir is classified by its contents (.ts/.js files -> JS).
    Kept in sync with the real module so stub-mode runs pick the same
    runner the real verify would.
    """
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "tox.ini")
    if any(os.path.exists(os.path.join(repo_path, m)) for m in markers):
        return "python"
    if os.path.isfile(os.path.join(repo_path, "package.json")):
        return "javascript"
    tests_dir = os.path.join(repo_path, "tests")
    if os.path.isdir(tests_dir):
        try:
            names = os.listdir(tests_dir)
        except OSError:
            names = []
        if any(n.endswith((".ts", ".js", ".tsx", ".jsx", ".mjs")) for n in names):
            return "javascript"
        return "python"
    return None


def _js_suite_command(repo_path: str) -> Optional[str]:
    """Runner its package.json declares (vitest/jest), else vitest default.

    Mirrors execution.verify._js_test_command's selection policy.
    """
    runner = None
    pkg_path = os.path.join(repo_path, "package.json")
    try:
        with open(pkg_path, encoding="utf-8", errors="replace") as fh:
            import json as _json

            pkg = _json.load(fh)
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
    if os.path.isfile(pkg_path):
        return "npx vitest run"
    return None


def _autodetect_test_command(repo_path: str, timeout_s: int) -> Optional[str]:
    """Find a test command for the repo if none was supplied.

    Assumes a small repo: language detection mirrors the real module
    (Python markers/package.json/tests contents). Python needs pytest
    importable on the host; JS needs node/npx on PATH — when the host
    lacks the toolchain, returns None (verify() then reports an explicit
    "no tests found" result instead of guessing).
    """
    lang = _detect_language(repo_path)
    if lang == "javascript":
        return _js_suite_command(repo_path)
    if lang == "python":
        markers = (
            "pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "conftest.py",
            "tox.ini",
        )
        has_tests_dir = os.path.isdir(os.path.join(repo_path, "tests"))
        if not (
            has_tests_dir
            or any(os.path.exists(os.path.join(repo_path, m)) for m in markers)
        ):
            return None
        check = execute_sandboxed(repo_path, 'python -c "import pytest"', timeout_s)
        if check.exit_code == 0:
            return "python -m pytest -q"
        return None
    return None


def _split_js_target(target_test: str):
    """Split a JS/TS target id into (file, name) — tolerant forms.

    Accepts "<file> - <name>", "<file>::<name>" (pytest habit), and a
    bare "<name>" (no file scope). Mirrors execution.verify.
    """
    t = target_test.strip()
    if "::" in t:
        f, _, n = t.partition("::")
        return f.strip(), n.strip()
    if " - " in t:
        f, _, n = t.partition(" - ")
        return f.strip(), n.strip()
    return "", t


def _target_command(
    target_test: Optional[str],
    suite_cmd: Optional[str],
    lang: Optional[str] = None,
) -> Optional[str]:
    """Command that runs just the target test (or the suite if no target).

    Python: pytest node id appended to the suite command. JS/TS: the
    target composes as "<runner> <file> -t <name>" (vitest and jest both
    filter with -t <substring>). Mirrors execution.verify._target_command.
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
        quote = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731
        if f and name:
            return f"{base} {f} -t {quote(name)}"
        if name:
            return f"{base} -t {quote(name)}"
        if f:
            return f"{base} {f}"
        return base
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
    which). `target_test` is a pytest node id ("file.py::test_name") or a
    JS/TS id ("<file> - <name>" / "<file>::<name>" / "<name>"), or None,
    in which case the full-suite exit code is the gate. Language is
    autodetected (mirrors the real module); an explicit test_command
    overrides for both target and suite runs.

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
                "(no pytest config/tests dir, no package.json runner, "
                "and none supplied)"
            ),
        )

    lang = _detect_language(repo_path)
    target_cmd = _target_command(target_test, suite_cmd, lang)

    # 1) Target test on the current state (rerun for flake detection).
    #    Three-valued outcome labels (pass/fail/timeout) so a timeout is
    #    a DISTINCT outcome — a pass/timeout mix is flaky, matching the
    #    real execution.verify and INTERFACES.md's documented semantics.
    outcomes = []
    raw = []
    for _ in range(max(1, rerun_for_flake_check)):
        res = execute_sandboxed(repo_path, target_cmd, verify_timeout_s)
        if res.timed_out or res.exit_code == 124:
            outcomes.append("timeout")
        else:
            outcomes.append("pass" if res.exit_code == 0 else "fail")
        raw.append(f"$ {target_cmd}\nexit={res.exit_code}\n{res.stdout}\n{res.stderr}")

    target_passed = outcomes[-1] == "pass"
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
