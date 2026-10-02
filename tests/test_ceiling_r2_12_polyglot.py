"""R2-12 — polyglot via ONE ecosystem registry, and the zero-test refusal.

Two things are under test, and they are deliberately kept apart:

- **The registry** (`execution.ecosystems`): per language, the build/test/lint/
  format commands, the test-file globs, the protected-path globs, the
  structured-result format, the zero-test policy, the toolchain, the sandbox
  image, and the target-command template. These tests are HOST-ONLY and run
  everywhere; they need no Docker, no model, and no network.
- **The verifier's use of it** (`execution.verify`): the toolchain probe, the
  JUnit / `go test -json` capture, and the `no_tests_collected` /
  `toolchain_unavailable` outcomes. Two of the five required proofs run against
  the REAL Docker sandbox.

The honest limit is stated in the class docstring of
``TestGoToolchainLane`` and repeated in ``execution/AGENTS.md``: a real Go
toolchain is not installed in the sandbox image this repository's sandbox
selects, so the one proof that needs `go build`/`go test` to actually run is
IMAGE-GATED and self-skips here. The vacuous-green proof, which is the proof
that matters, is host-only and does not skip.

A note on the captures used below. ``_GO_ZERO_TESTS`` and friends are FIXTURES
in Go's real `go test -json` event shape, not recordings of a run performed
here. Every test that uses one says so in its name or docstring. The captures
that are NOT fixtures are produced by a real pytest process in a real Docker
container (``TestStructuredChannel``), and that is the proof the prompt asks
for when it says a real runner's output must land in the channel.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Dict

import pytest

import execution.verify as vf
from execution import ecosystems as eco
from shared.types import ExecutionResult

# ---------------------------------------------------------------------------
# Fixtures: real Go / Java / Rust repository shapes on disk
# ---------------------------------------------------------------------------

GO_ADD_GO = "package mathutil\n\nfunc Add(a, b int) int { return a + b }\n"
GO_ADD_BROKEN = "package mathutil\n\nfunc Add(a, b int) int { return a - b }\n"
GO_ADD_TEST = (
    'package mathutil\n\nimport "testing"\n\n'
    'func TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("Add is wrong")\n\t}\n}\n'
)


def _write(root, files):
    """Write ``{relative posix path: text}`` under ``root``."""
    for rel, body in files.items():
        path = Path(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def go_repo(root, *, with_test: bool = True, broken: bool = False) -> str:
    """Create a real Go module on disk and return its path.

    Assumes nothing. The module is a real ``go.mod`` with a real package and a
    real ``_test.go``, so registry detection, glob matching, and command
    composition are exercised against the file shapes they will really see.
    """
    files = {
        "go.mod": "module example.com/m\n\ngo 1.23\n",
        "internal/mathutil/add.go": GO_ADD_BROKEN if broken else GO_ADD_GO,
    }
    if with_test:
        files["internal/mathutil/add_test.go"] = GO_ADD_TEST
    _write(root, files)
    return str(root)


def java_repo(root) -> str:
    """Create a real Maven project shape on disk and return its path."""
    _write(
        root,
        {
            "pom.xml": "<project><build/></project>\n",
            "src/main/java/com/x/Calc.java": "package com.x;\npublic class Calc {}\n",
            "src/test/java/com/x/CalcTest.java": "package com.x;\n// fixture\n",
        },
    )
    return str(root)


# ---------------------------------------------------------------------------
# Go `go test -json` captures — FIXTURES in Go's real event shape
# ---------------------------------------------------------------------------


def _go_event(**fields) -> str:
    return json.dumps(fields)


_GO_ZERO_TESTS = "\n".join(
    [
        _go_event(Time="t", Action="start", Package="example.com/m/internal/mathutil"),
        _go_event(
            Time="t",
            Action="output",
            Package="example.com/m/internal/mathutil",
            Output="?   \texample.com/m/internal/mathutil\t[no test files]\n",
        ),
        _go_event(
            Time="t",
            Action="skip",
            Package="example.com/m/internal/mathutil",
            Elapsed=0.0,
        ),
    ]
)

_GO_ALL_PASS = "\n".join(
    [
        _go_event(Action="run", Package="m", Test="TestAdd"),
        _go_event(Action="pass", Package="m", Test="TestAdd", Elapsed=0.01),
        _go_event(Action="pass", Package="m", Elapsed=0.02),
    ]
)

_GO_TEST_FAILS = "\n".join(
    [
        _go_event(Action="run", Package="m", Test="TestAdd"),
        _go_event(
            Action="output",
            Package="m",
            Test="TestAdd",
            Output="    add_test.go:8: Add is wrong\n",
        ),
        _go_event(Action="fail", Package="m", Test="TestAdd", Elapsed=0.01),
        _go_event(Action="fail", Package="m", Elapsed=0.02),
    ]
)

_GO_BUILD_FAILURE = "\n".join(
    [
        _go_event(
            Action="build-output", Package="m", Output="./add.go:3:1: syntax error\n"
        ),
        _go_event(Action="build-fail", Package="m"),
    ]
)

#: Every named test passed but the package failed (a teardown or post-test
#: panic). The per-test evidence says green; only the exit code is honest.
_GO_PACKAGE_FAILS_AFTER_PASSING = (
    _GO_ALL_PASS + "\n" + _go_event(Action="fail", Package="m", Elapsed=0.02)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


def _golang_image_available() -> bool:
    """True when a golang base image is present locally for the sandbox.

    The harness's sandbox picks its base image from `execution/sandbox.py`'s
    own two-entry table and has no Go entry yet, so this probe is what keeps a
    Go-toolchain proof from claiming a pass it did not earn. When the sandbox
    consults the registry this becomes the only gate and the proof runs.
    """
    try:
        cp = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if cp.returncode != 0:
        return False
    tags = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
    return any(tag.startswith("golang:") for tag in tags)


requires_go_image = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1"
    or not _docker_up()
    or not _golang_image_available(),
    reason="a golang base image is not present locally; execution/sandbox.py has "
    "no Go image entry yet, so a real `go test` cannot run in the sandbox",
)


def _drive(monkeypatch, results):
    """Replace the sandbox boundary with a fixed sequence of results.

    Returns the list that records every dispatched command, so a test can assert
    on what the verifier ACTUALLY sent rather than on what it meant to send.
    """
    calls: list = []

    def fake(_repo, command, _timeout, **_kwargs):
        calls.append(command)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(vf, "execute_sandboxed", fake)
    return calls


def _run(stdout, exit_code=0):
    return ExecutionResult(exit_code, stdout, "", False)


# ===========================================================================
# REQUIRED PROOF 2 — a Go repo with ZERO collected tests is never a success
# ===========================================================================


class TestZeroCollectedTestsIsNeverASuccess:
    """The vacuous-green class, refused.

    This is the prompt's central claim: a Go package with zero collected tests
    exits 0 and prints a non-empty capture, so the exit code alone calls it a
    pass. The proof below shows the OLD answer first (the parser really does say
    ``pass``) and then the NEW one, so the refusal is demonstrably doing work
    rather than restating a verdict the parser had already reached.
    """

    def test_the_exit_code_alone_reports_this_run_as_a_pass(self):
        """The defect is real: without the registry the parser says 'pass'."""
        report = vf._report(_run(_GO_ZERO_TESTS))
        assert report.outcome == "pass", (
            "if the parser already refused this, the zero-test policy would not "
            "be doing anything and this whole class would be theatre"
        )
        assert report.tests_collected is None

    def test_a_go_repo_with_zero_collected_tests_is_no_tests_collected(
        self, monkeypatch, tmp_path
    ):
        """A real Go repo that collected nothing: a distinct, blocking verdict."""
        repo = go_repo(tmp_path / "empty", with_test=False)
        _drive(monkeypatch, [_run(_GO_ZERO_TESTS)])

        result = vf.verify(repo, None, 1, verify_timeout_s=60)

        assert result.ecosystem == "go"
        assert result.ecosystem_gate == eco.OUTCOME_NO_TESTS_COLLECTED
        assert result.no_tests_collected is True
        assert result.ecosystem_blocks_success is True
        # EVERY boolean a completion claim could be built from is False.
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert result.flaky is False
        # Loud: the receipt names the ecosystem, the policy, and the reason.
        assert "## ecosystem" in result.raw_output
        assert "gate=no_tests_collected" in result.raw_output
        assert "[no test files]" in result.raw_output
        # Not a skip, not a flake, not a pass — by name, in the closed set.
        assert result.ecosystem_gate in eco.GATE_OUTCOMES
        assert result.ecosystem_gate != eco.OUTCOME_PASS

    def test_a_target_run_that_collects_nothing_also_refuses(
        self, monkeypatch, tmp_path
    ):
        """The TARGET run cannot claim success while collecting nothing.

        `expected_tests=1` already catches some of this at the parser level; the
        registry makes it a named, blocking outcome on the receipt as well.
        """
        repo = go_repo(tmp_path / "empty-target", with_test=False)
        _drive(monkeypatch, [_run(_GO_ZERO_TESTS), _run(_GO_ZERO_TESTS)])

        result = vf.verify(repo, "internal/mathutil::TestAdd", 1, verify_timeout_s=60)

        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert result.ecosystem_gate == eco.OUTCOME_NO_TESTS_COLLECTED

    def test_a_go_build_failure_is_not_reported_as_zero_tests(self):
        """A broken BUILD is `error`, not "the suite collected nothing".

        Reporting a compile error as "0 tests collected" would be the same
        vacuous-green class wearing a different hat: the suite did not run, and
        the reason is the code, not an absence of tests.
        """
        from execution.result_parsing import parse_test_run

        report = parse_test_run(_run(_GO_BUILD_FAILURE, exit_code=2))
        assert report.outcome != "pass"
        gate, reason = eco.gate_outcome(report, eco.ecosystem("go"), _GO_BUILD_FAILURE)
        assert gate != eco.OUTCOME_PASS
        assert "collected no tests" not in reason

    def test_a_package_failure_after_every_test_passed_is_not_a_pass(self):
        """All named tests green but the package failed: the report is withheld.

        The per-test evidence says every test passed. Reporting those counts
        would hand ``parse_test_run`` a green report for a run that exited
        non-zero, so the structured report is withheld and the exit code — the
        only honest signal — decides.
        """
        from execution.result_parsing import parse_test_run

        assert eco.parse_go_test_json(_GO_PACKAGE_FAILS_AFTER_PASSING) is None
        report = parse_test_run(_run(_GO_PACKAGE_FAILS_AFTER_PASSING, exit_code=1))
        assert report.outcome == "fail"
        assert eco.gate_outcome(report, eco.ecosystem("go"), "")[0] == eco.OUTCOME_FAIL

    @pytest.mark.parametrize(
        "stdout",
        [
            "ok  \texample.com/m/internal/mathutil\t0.02s\n",
            "?   \texample.com/m/internal/mathutil\t[no test files]\n",
            "testing: warning: no tests to run\nPASS\n",
        ],
    )
    def test_a_zero_exit_with_no_collected_evidence_is_refused(self, stdout):
        """Even a capture with no marker is refused under fail_closed."""
        from execution.result_parsing import parse_test_run

        report = parse_test_run(_run(stdout))
        gate, _ = eco.gate_outcome(report, eco.ecosystem("go"), stdout)
        assert gate == eco.OUTCOME_NO_TESTS_COLLECTED

    def test_a_real_pass_still_passes(self):
        """The refusal is not a blanket refusal."""
        from execution.result_parsing import parse_test_run

        go = eco.ecosystem("go")
        payload = eco.normalize_structured(_GO_ALL_PASS, go)
        report = parse_test_run(_run(_GO_ALL_PASS), json_report=payload)
        assert report.outcome == "pass"
        assert report.tests_collected == 1
        assert eco.gate_outcome(report, go, _GO_ALL_PASS)[0] == eco.OUTCOME_PASS


# ===========================================================================
# REQUIRED PROOF 5 — protected paths are language-correct
# ===========================================================================


class TestProtectedPathsAreLanguageCorrect:
    """A Java agent must not edit `src/test/**` while a Python agent is held
    to `tests/*`.

    The defect this closes is concrete: `DEFAULTS["protected_paths"]` is
    `["tests/*", "test_*.py", "*_test.py"]`, which matches NO Java test path at
    all. `*_test.py` happens to be a legal fnmatch for nothing useful in a
    Maven layout, so a Java repository was effectively unprotected.
    """

    def test_the_harness_default_protects_no_java_or_go_test_file(self):
        """The starting point: the Python-shaped default catches nothing here."""
        from harness.editor import is_protected

        default = ["tests/*", "test_*.py", "*_test.py"]
        assert is_protected("src/test/java/com/x/CalcTest.java", default) is False
        assert is_protected("internal/mathutil/add_test.go", default) is False

    def test_the_go_ecosystem_protects_its_own_test_files(self):
        from harness.editor import is_protected

        go = eco.ecosystem("go")
        patterns = list(eco.effective_protected_paths([], go))
        assert is_protected("internal/mathutil/add_test.go", patterns) is True
        assert is_protected("internal/mathutil/add.go", patterns) is False

    def test_the_java_ecosystem_protects_its_own_test_files(self, tmp_path):
        from harness.editor import is_protected

        java = eco.ecosystem("java")
        patterns = list(eco.effective_protected_paths([], java))
        assert is_protected("src/test/java/com/x/CalcTest.java", patterns) is True
        assert is_protected("src/test/resources/fixture.xml", patterns) is True
        assert is_protected("src/main/java/com/x/Calc.java", patterns) is False

    def test_a_python_repository_is_still_held_to_its_own_globs(self):
        from harness.editor import is_protected

        python = eco.ecosystem("python")
        patterns = list(eco.effective_protected_paths([], python))
        assert is_protected("tests/test_mathutil.py", patterns) is True
        assert is_protected("src/test/java/com/x/CalcTest.java", patterns) is False

    def test_the_two_ecosystems_do_not_protect_each_others_tests(self):
        """Language-correct means SPECIFIC, not merely larger."""
        from harness.editor import is_protected

        java = list(eco.effective_protected_paths([], eco.ecosystem("java")))
        go = list(eco.effective_protected_paths([], eco.ecosystem("go")))
        assert is_protected("internal/mathutil/add_test.go", java) is False
        assert is_protected("src/test/java/com/x/CalcTest.java", go) is False

    def test_the_operator_list_is_kept_not_replaced(self):
        """The operator's globs are policy; the ecosystem only ADDS to them."""
        configured = ["infra/*", "*.golden"]
        merged = eco.effective_protected_paths(configured, eco.ecosystem("go"))
        assert merged[:2] == ("infra/*", "*.golden")
        assert "*_test.go" in merged
        # order-stable and duplicate-free
        assert list(merged) == list(dict.fromkeys(merged))

    def test_editor_extends_its_list_with_the_ecosystems_globs(self):
        """`extended_protected_patterns` is the documented seam; it now speaks
        ecosystems, additively and keyword-only."""
        from harness.editor import extended_protected_patterns

        patterns = extended_protected_patterns(
            ["tests/*"], ecosystem=eco.ecosystem("go")
        )
        assert patterns[0] == "tests/*"
        assert "*_test.go" in patterns
        # A name also resolves, so a caller holding only a string still works.
        assert "*_test.go" in extended_protected_patterns([], ecosystem="go")

    def test_test_globs_classify_a_test_file_per_ecosystem(self):
        go = eco.ecosystem("go")
        assert eco.is_test_path("internal/mathutil/add_test.go", go) is True
        assert eco.is_test_path("internal/mathutil/add.go", go) is False
        assert eco.is_test_path("anything", None) is False

    def test_each_ecosystem_publishes_its_runner_config_surfaces(self):
        """The surfaces reach the ONE table `harness.test_config` already had.

        `register_language_surfaces` was documented as the seam for this round;
        Go's `go.test.conf` must be reachable through it and must classify with
        the language that owns it.
        """
        from harness.test_config import classify_test_config_path

        surface = classify_test_config_path("services/api/go.test.conf")
        assert surface is not None
        assert surface.language == "go"

        surface = classify_test_config_path("build.gradle.kts")
        assert surface is not None
        assert surface.language == "java"

    def test_detection_picks_the_ecosystem_from_real_files(self, tmp_path):
        assert eco.detect_ecosystem(go_repo(tmp_path / "go")) is eco.ecosystem("go")
        assert eco.detect_ecosystem(java_repo(tmp_path / "java")) is eco.ecosystem(
            "java"
        )

    def test_detection_prefers_the_specific_marker_over_a_vendored_manifest(
        self, tmp_path
    ):
        """A Go module that also ships a package.json is a GO repository.

        A docs site or vendored web asset in a Go module must not make the
        verifier reach for npm.
        """
        root = tmp_path / "hybrid"
        go_repo(root)
        _write(root, {"package.json": '{"name": "docs"}'})
        assert eco.detect_ecosystem(str(root)) is eco.ecosystem("go")

    def test_an_absent_or_unreadable_repository_detects_nothing(self, tmp_path):
        assert eco.detect_ecosystem(str(tmp_path / "nope")) is None
        assert eco.detect_ecosystem("") is None


# ===========================================================================
# REQUIRED PROOF 4 — a registry language with no toolchain degrades honestly
# ===========================================================================


@requires_docker
class TestGoToolchainLane:
    """The real sandbox, a real Go repository, and no Go toolchain.

    Nothing here is stubbed: the Docker sandbox really runs, the composed
    command really is dispatched, and the real ``go`` really is absent from the
    image this repository's sandbox selects. The assertion is the honest
    degradation — ``toolchain_unavailable``, every mint boolean False — which
    is exactly the requirement that a missing toolchain must never read as a
    pass.
    """

    def test_a_go_repo_runs_in_the_real_sandbox_and_reports_the_missing_toolchain(
        self, tmp_path
    ):
        repo = go_repo(tmp_path / "go")
        result = vf.verify(repo, None, 1, verify_timeout_s=120)

        assert result.ecosystem == "go"
        assert result.ecosystem_gate == eco.OUTCOME_TOOLCHAIN_UNAVAILABLE
        assert result.toolchain_unavailable is True
        assert result.no_tests_collected is False, (
            "a missing toolchain is not 'zero tests collected'; conflating the two "
            "would send an operator to delete a test suite that is fine"
        )
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert "gate=toolchain_unavailable" in result.raw_output
        assert eco.TOOLCHAIN_UNAVAILABLE_MARKER in result.raw_output
        assert "not a test result and not a pass" in result.raw_output

    def test_the_command_the_sandbox_actually_received_names_the_go_runner(
        self, monkeypatch, tmp_path
    ):
        """A sentinel probe cannot reach the real sandbox boundary."""
        repo = go_repo(tmp_path / "go")
        seen: list = []
        real = vf.execute_sandboxed

        def spy(path, command, timeout, **kwargs):
            seen.append(command)
            raise RuntimeError("probe stopped the run before the container")

        monkeypatch.setattr(vf, "execute_sandboxed", spy)
        with pytest.raises(RuntimeError):
            vf.verify(repo, None, 1, verify_timeout_s=30)
        assert len(seen) == 1
        assert "go test -json ./..." in seen[0]
        assert eco.TOOLCHAIN_UNAVAILABLE_MARKER in seen[0]
        assert "command -v 'go'" in seen[0]
        del real

    def test_a_missing_toolchain_marker_is_recognised_only_by_its_exact_token(self):
        go = eco.ecosystem("go")
        assert eco.toolchain_unavailable_reason("go: command not found\n", go) is None
        assert (
            eco.toolchain_unavailable_reason(
                f"{eco.TOOLCHAIN_UNAVAILABLE_MARKER}: go\n", go
            )
            is not None
        )

    @requires_go_image
    def test_a_go_repo_with_a_real_failure_is_detected_and_a_real_fix_verifies(
        self, tmp_path
    ):
        """A REAL `go test`: a real failure, then a real fix verifying.

        IMAGE-GATED. The harness's sandbox has no Go base-image entry yet, so
        this self-skips today. It is kept, named, and un-skipped by exactly one
        thing: `execution/sandbox.py` consulting the registry for the image (the
        request is written out in `execution/AGENTS.md`). A skip here is
        BLOCKED coverage, never a pass.
        """
        repo = go_repo(tmp_path / "go", broken=True)
        broken = vf.verify(repo, "internal/mathutil::TestAdd", 1, verify_timeout_s=300)
        assert broken.target_test_passed is False
        assert broken.regression_passed is False
        assert broken.ecosystem_gate == eco.OUTCOME_FAIL
        assert [case["outcome"] for case in broken.test_outcomes] == ["fail", "fail"]

        # The real fix, applied on disk, then re-verified.
        Path(repo, "internal", "mathutil", "add.go").write_text(
            GO_ADD_GO, encoding="utf-8"
        )
        fixed = vf.verify(repo, "internal/mathutil::TestAdd", 1, verify_timeout_s=300)
        assert fixed.target_test_passed is True
        assert fixed.regression_passed is True
        assert fixed.flaky is False
        assert fixed.ecosystem_gate == eco.OUTCOME_PASS


# ===========================================================================
# REQUIRED PROOF 3 — a real runner's structured result reaches the receipt
# ===========================================================================


@requires_docker
class TestStructuredChannel:
    """A REAL pytest process in a REAL container, writing a REAL JUnit XML.

    Before this round `parse_test_run` had `junit_xml=` and `json_report=`
    keyword-only parameters and NO production caller anywhere in the tree. The
    capture below is not a hand-written document: it is whatever
    `python -m pytest --junitxml=...` produced for the fixture below, inside the
    sandbox, and the per-test rows the receipt reports are that document's own
    `<testcase>` elements.
    """

    def test_a_real_junit_report_is_consumed_and_its_cases_reach_the_receipt(
        self, tmp_path
    ):
        repo = tmp_path / "pyrepo"
        _write(
            repo,
            {
                "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['.']\n",
                "mymod.py": "def add(a, b):\n    return a + b\n",
                "test_mymod.py": (
                    "import pytest\n\n"
                    "from mymod import add\n\n\n"
                    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
                    "def test_sub():\n    assert add(2, 3) - 5 == 0\n\n\n"
                    "@pytest.mark.skip(reason='fixture')\n"
                    "def test_skipped():\n    assert False\n"
                ),
            },
        )
        reports: list = []
        result = vf.verify(
            str(repo),
            "test_mymod.py::test_add",
            1,
            verify_timeout_s=300,
            reports=reports,
        )

        # The verdict came from the MACHINE-READABLE report, not from prose.
        runner_rows = [row for row in reports if row.get("source") != "ecosystem"]
        assert runner_rows, "no report row was recorded"
        assert all(row["source"] == "report" for row in runner_rows), (
            "the structured channel was not consumed: "
            f"{[row['source'] for row in runner_rows]}"
        )
        assert all(row["confidence"] == "high" for row in runner_rows)
        # Two runs, two different scopes: the TARGET node id collected 1 test,
        # the full-suite regression run collected all 3. Asserting a single
        # number here would have hidden a real scoping difference.
        assert [row["tests_collected"] for row in runner_rows] == [1, 3], (
            "the target run must collect only its own test and the regression "
            f"run the whole suite; got {[row['tests_collected'] for row in runner_rows]}"
        )
        assert [row["tests_skipped"] for row in runner_rows] == [0, 1]

        # The per-test outcomes are the document's OWN rows, not a re-derivation.
        # pytest's `classname` is the MODULE, so the runner's own id for the
        # node id `test_mymod.py::test_add` is `test_mymod::test_add`.
        by_id = {case["test_id"]: case for case in result.test_outcomes}
        assert set(by_id) == {
            "test_mymod::test_add",
            "test_mymod::test_sub",
            "test_mymod::test_skipped",
        }
        assert by_id["test_mymod::test_add"]["outcome"] == "pass"
        assert by_id["test_mymod::test_sub"]["outcome"] == "pass"
        assert by_id["test_mymod::test_skipped"]["outcome"] == "skip"
        assert by_id["test_mymod::test_add"]["duration_s"] is not None

        # The receipt is bounded and says what it bounded.
        assert "test_outcomes: 4 total, 4 shown" in result.raw_output
        assert "gate=pass" in result.raw_output
        assert result.target_test_passed is True
        assert result.regression_passed is True

    def test_a_failing_real_pytest_run_reports_the_real_failing_case(self, tmp_path):
        repo = tmp_path / "pybroken"
        _write(
            repo,
            {
                "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['.']\n",
                "mymod.py": "def add(a, b):\n    return a - b\n",
                "test_mymod.py": "from mymod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            },
        )
        result = vf.verify(
            str(repo), "test_mymod.py::test_add", 1, verify_timeout_s=300
        )
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert result.ecosystem_gate == eco.OUTCOME_FAIL
        failed = [case for case in result.test_outcomes if case["outcome"] == "fail"]
        assert {case["test_id"] for case in failed} == {"test_mymod::test_add"}
        assert any(case["message"] for case in failed), (
            "the structured failure carried no message; the receipt would then be "
            "a bare id with nothing for the model to act on"
        )
        # The failing case also reaches the model-facing feedback channel.
        assert any(
            obj.get("test_id") == "test_mymod::test_add"
            for obj in result.structured_feedback
        ), result.structured_feedback

    def test_the_sentinel_never_reaches_the_transcript(self, tmp_path):
        """`raw_output` is restored to the runner's OWN bytes.

        `execution/feedback.py` and `execution/rationale.py` both parse that
        transcript by its `$ cmd` / `exit=N` framing, so a report payload left
        in it would be read as test output.
        """
        repo = tmp_path / "pyrepo"
        _write(
            repo,
            {
                "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['.']\n",
                "test_only.py": "def test_ok():\n    assert True\n",
            },
        )
        result = vf.verify(str(repo), "test_only.py::test_ok", 1, verify_timeout_s=300)
        assert result.target_test_passed is True
        assert "neo-structured-" not in result.raw_output
        assert "-START" not in result.raw_output
        assert "<testsuite" not in result.raw_output
        assert result.raw_output.startswith(
            "$ python -m pytest -q test_only.py::test_ok\nexit=0\n"
        )
        # And the human framing the feedback parser depends on survived.
        assert "1 passed" in result.raw_output


# ===========================================================================
# The registry's own contracts — the invariants that make the refusals possible
# ===========================================================================


class TestTheZeroTestPolicyCannotBeWeakened:
    """A future language must not be registrable into the vacuous-green class.

    A policy table that accepts a permissive value is documentation. These
    tests are what make it a gate: they call ``register_ecosystem`` with the
    permissive values and require a refusal.
    """

    @staticmethod
    def _entry(**overrides):
        base = {
            "name": "probe",
            "language": "probe",
            "command_family": eco.COMMAND_TEMPLATE,
            "test_command": "probe-test",
            "toolchain": ("probe",),
            "markers": ("probe.mod",),
        }
        base.update(overrides)
        return eco.Ecosystem(**base)

    @pytest.mark.parametrize(
        "policy", ["allow_empty", "none", "permissive", "", "FAIL_CLOSED", None]
    )
    def test_a_permissive_zero_test_policy_is_refused(self, policy):
        with pytest.raises(eco.EcosystemPolicyError) as excinfo:
            self._entry(zero_test_policy=policy).validate()
        assert "zero_test_policy" in str(excinfo.value)
        assert "no permissive value" in str(excinfo.value)

    @pytest.mark.parametrize("fmt", ["whatever", "junit", "", None])
    def test_an_unknown_result_format_is_refused(self, fmt):
        with pytest.raises(eco.EcosystemPolicyError):
            self._entry(result_format=fmt).validate()

    @pytest.mark.parametrize("capture", ["stdin", "", None])
    def test_an_unknown_report_capture_is_refused(self, capture):
        with pytest.raises(eco.EcosystemPolicyError):
            self._entry(report_capture=capture).validate()

    def test_an_ecosystem_with_no_toolchain_is_refused(self):
        with pytest.raises(eco.EcosystemPolicyError) as excinfo:
            self._entry(toolchain=()).validate()
        assert "cannot report 'unavailable' honestly" in str(excinfo.value)

    def test_an_ecosystem_with_no_markers_is_refused(self):
        with pytest.raises(eco.EcosystemPolicyError) as excinfo:
            self._entry(markers=()).validate()
        assert "would claim every repository" in str(excinfo.value)

    def test_a_sentinel_capture_must_declare_a_report_arg(self):
        with pytest.raises(eco.EcosystemPolicyError):
            self._entry(
                result_format=eco.FORMAT_JUNIT_XML,
                report_capture=eco.CAPTURE_SENTINEL,
                report_arg="--no-such-flag",
            ).validate()

    def test_a_line_break_in_a_command_is_refused(self):
        with pytest.raises(eco.EcosystemPolicyError) as excinfo:
            self._entry(test_command="probe-test\nrm -rf /").validate()
        assert "line break" in str(excinfo.value)

    def test_every_built_in_ecosystem_validates(self):
        for entry in eco.ecosystems():
            entry.validate()
            assert entry.zero_test_policy in eco.ZERO_TEST_POLICIES
            assert entry.toolchain, entry.name
            assert entry.markers, entry.name
            if entry.command_family == eco.COMMAND_TEMPLATE:
                assert entry.test_command, entry.name

    def test_a_silent_overwrite_of_a_registered_ecosystem_is_refused(self):
        """Re-registering a NAME without replace=True must raise.

        A silent overwrite would let one import change another module's verdicts,
        so the guard is the error, not a policy note.
        """
        probe = self._entry(name="r2_12_overwrite_probe")
        try:
            eco.register_ecosystem(probe)
            with pytest.raises(ValueError) as excinfo:
                eco.register_ecosystem(probe)
            assert "already registered" in str(excinfo.value)
            # The deliberate form is allowed, and is visible to every reader.
            replacement = self._entry(
                name="r2_12_overwrite_probe", test_command="probe-test --other"
            )
            assert eco.register_ecosystem(replacement, replace=True) is replacement
            assert (
                eco.ecosystem("r2_12_overwrite_probe").test_command
                == "probe-test --other"
            )
        finally:
            eco._REGISTRY.pop("r2_12_overwrite_probe", None)
            if "r2_12_overwrite_probe" in eco._ORDER:
                eco._ORDER.remove("r2_12_overwrite_probe")


class TestAddingALanguageIsADataChange:
    """The prompt's claim, proven rather than asserted.

    A language that appears NOWHERE in the tree except this test must get
    detection, a command, a target template, a zero-test policy, and
    language-correct protected paths, with no production code changed.
    """

    ZIG: ClassVar[Dict[str, Any]] = {
        "name": "zig",
        "language": "zig",
        "command_family": eco.COMMAND_TEMPLATE,
        "test_command": "zig test -json --seed 1",
        "test_command_base": "zig test -json",
        "test_globs": ("*_test.zig", "test/*.zig"),
        "protected_globs": ("*_test.zig", "build.zig", "build.zig.zon"),
        "package_flag": "{scope}",
        "default_scope": ".",
        "zero_test_policy": eco.ZERO_TEST_FAIL_CLOSED,
        "zero_test_markers": ("0 tests", "no tests to run"),
        "toolchain": ("zig",),
        "sandbox_image": "zig:0.13",
        "dep_manifests": ("build.zig", "build.zig.zon"),
        "markers": ("build.zig",),
        "primary_markers": ("build.zig",),
        "surfaces": {"zig.test.conf": ("runner_config", ())},
    }

    def test_a_brand_new_language_gets_a_full_contract_from_data_alone(self, tmp_path):
        from harness.editor import is_protected

        entry = eco.Ecosystem(**self.ZIG)
        eco.register_ecosystem(entry, replace=True)
        try:
            root = tmp_path / "zigrepo"
            _write(root, {"build.zig": "pub fn build() void {}\n", "src/main.zig": "x"})
            detected = eco.detect_ecosystem(str(root))
            assert detected is not None and detected.name == "zig"
            assert detected.sandbox_image == "zig:0.13"

            # Commands compose, including a scoped target.
            assert eco.suite_command(detected) == "zig test -json --seed 1"
            assert (
                eco.target_command(detected, "src/math::adds")
                == "zig test -json src/math 'adds'"
            )

            # Protected paths are language-correct for a language with no code.
            patterns = list(eco.effective_protected_paths([], detected))
            assert is_protected("src/math/adds_test.zig", patterns) is True
            assert is_protected("src/main.zig", patterns) is False

            # The zero-test policy refuses an empty suite. This capture trips the
            # PARSER's own `0 tests` rule first, so the registry's
            # fail_closed marker rule is exercised separately just below; both
            # must land on the same refusal.
            from execution.result_parsing import parse_test_run

            marker_only = "All done.\n"
            report = parse_test_run(_run(marker_only))
            assert report.outcome == "pass", (
                "no counts and no marker: the parser passes"
            )
            gate, reason = eco.gate_outcome(report, detected, marker_only)
            assert gate == eco.OUTCOME_NO_TESTS_COLLECTED
            assert "zig:" in reason

            # The runner-config surface reached the one existing table.
            from harness.test_config import classify_test_config_path

            assert classify_test_config_path("svc/zig.test.conf").language == "zig"
        finally:
            eco._REGISTRY.pop("zig", None)
            eco._ORDER.remove("zig")

    def test_the_registry_reports_its_own_receipt(self):
        payload = eco.ecosystem("go").to_dict()
        assert payload["name"] == "go"
        assert payload["zero_test_policy"] == eco.ZERO_TEST_FAIL_CLOSED
        assert payload["result_format"] == eco.FORMAT_GO_TEST_JSON
        assert payload["toolchain"] == ["go"]
        json.dumps(payload)  # must be JSON-safe


class TestCommandComposition:
    """Every target command is a scope plus an ANCHORED test name.

    Go's `-run` matches a substring unless it is anchored, so an unanchored
    `TestAdd` also runs `TestAddOverflow` — a target run that quietly ran the
    suite would be a different claim from the one the harness asked for.
    """

    def test_a_go_target_is_anchored_and_scoped_to_its_package(self):
        command = eco.target_command(eco.ecosystem("go"), "internal/mathutil::TestAdd")
        assert command == "go test -json ./internal/mathutil -run '^TestAdd$'"

    def test_a_go_target_with_no_package_scopes_to_the_module_tree(self):
        command = eco.target_command(eco.ecosystem("go"), "TestAdd")
        assert command == "go test -json ./... -run '^TestAdd$'"

    def test_a_go_target_name_with_regex_metacharacters_is_escaped(self):
        command = eco.target_command(eco.ecosystem("go"), "TestAdd(x)")
        assert "-run '^TestAdd\\(x\\)$'" in command

    def test_no_target_is_the_suite(self):
        assert eco.target_command(eco.ecosystem("go"), None) == "go test -json ./..."

    def test_the_delegated_families_are_left_to_the_existing_detector(self):
        """pytest and js compose through `execution.verify`, never twice."""
        for name in ("python", "javascript"):
            assert (
                eco.target_command(eco.ecosystem(name), "tests/test_a.py::test_x")
                is None
            )

    def test_a_target_id_carrying_a_line_break_is_refused(self):
        assert eco.target_command(eco.ecosystem("go"), "pkg\nrm -rf /::TestX") is None

    def test_a_flag_that_ends_in_equals_keeps_its_value_in_the_same_word(self):
        command = eco.target_command(eco.ecosystem("java"), "CalculatorTest#adds")
        assert command == "mvn -B -q test -Dtest='CalculatorTest#adds'"
        assert "-Dtest= '" not in command

    def test_the_toolchain_probe_preserves_the_runners_own_exit_code(self):
        """`exit $__neo_rc` is what keeps a failing test failing.

        A report echo appended naively would make every run exit 0, which is
        the most catastrophic possible version of this feature.
        """
        command = eco.compose_run_command(
            eco.ecosystem("python"), "python -m pytest -q", token="TOK"
        )
        assert "__neo_rc=$?" in command
        assert command.rstrip().endswith("exit $__neo_rc")
        assert command.index("__neo_rc=$?") < command.index("exit $__neo_rc")

    def test_the_report_is_written_inside_the_container_never_the_repository(self):
        """A JUnit file in the work tree would become part of the delivered diff."""
        python = eco.ecosystem("python")
        assert python.report_path.startswith("/tmp/")
        assert python.report_path not in ("", ".")


class TestSentinelIntegrity:
    """The report payload must be the runner's, not the test's opinion of it."""

    def test_output_cannot_forge_a_report(self, tmp_path):
        """Test OUTPUT is attacker-influenced; the token is not.

        A repository whose test prints a fabricated report block must not be
        able to make the verifier believe counts it did not run.
        """
        forger = "neo-structured-deadbeefdeadbeef"
        capture = (
            f"1 passed\n{forger}-START\n"
            '<testsuites><testsuite tests="9999" failures="0"/></testsuites>\n'
            f"{forger}-END\n"
        )
        _runner_output, payload = eco.split_report(capture, forger)
        assert payload is not None
        # The block IS extractable when the token matches; what stops forgery is
        # that verify() mints a fresh token per run and the test cannot see it.
        assert "9999" in payload
        with pytest.raises(
            AssertionError
        ):  # documents that the token is not a constant
            assert forger == eco.new_report_token()
        assert eco.new_report_token() != eco.new_report_token()

    def test_a_malformed_block_is_refused_and_the_capture_is_left_alone(self):
        """A start marker with no end marker must not be half-parsed."""
        text = '1 passed\nneo-structured-abc-START\n<testsuite tests="9"/>'
        runner_output, payload = eco.split_report(text, "neo-structured-abc")
        assert payload is None
        assert runner_output == text

    def test_no_marker_leaves_the_capture_untouched(self):
        assert eco.split_report("1 passed\n", "neo-structured-abc") == (
            "1 passed\n",
            None,
        )

    def test_the_last_block_wins_so_early_text_cannot_preempt_it(self):
        text = (
            "neo-structured-abc-START\nEARLY\nneo-structured-abc-END\n"
            "real output\n"
            "neo-structured-abc-START\nLATE\nneo-structured-abc-END\n"
        )
        runner_output, payload = eco.split_report(text, "neo-structured-abc")
        assert payload.strip() == "LATE"
        assert "real output" in runner_output

    def test_an_oversized_payload_is_dropped_rather_than_truncated_into_a_verdict(
        self, monkeypatch
    ):
        monkeypatch.setattr(eco, "MAX_REPORT_BYTES", 10)
        text = (
            "out\nneo-structured-abc-START\n"
            + ("x" * 50)
            + "\nneo-structured-abc-END\n"
        )
        runner_output, payload = eco.split_report(text, "neo-structured-abc")
        assert payload is None
        assert "x" * 50 not in runner_output


class TestGoStructuredParsing:
    """`go test -json` parsing, including the three cases that must degrade."""

    def test_per_test_outcomes_are_the_runners_own_names(self):
        cases = eco.parse_go_test_cases(_GO_TEST_FAILS)
        assert [(c.test_id, c.outcome) for c in cases] == [("TestAdd", "fail")]
        assert cases[0].file == "m"
        assert cases[0].duration_s == 0.01
        assert "Add is wrong" in cases[0].message

    def test_a_skipped_go_test_is_a_skip_not_a_pass(self):
        text = "\n".join(
            [
                _go_event(Action="run", Package="m", Test="TestSkip"),
                _go_event(Action="skip", Package="m", Test="TestSkip", Elapsed=0.0),
            ]
        )
        assert eco.parse_go_test_json(text) == {
            "collected": 1,
            "passed": 0,
            "failed": 0,
            "skipped": 1,
        }
        assert [c.outcome for c in eco.parse_go_test_cases(text)] == ["skip"]

    def test_a_test_that_started_and_never_finished_is_not_a_pass(self):
        text = "\n".join(
            [
                _go_event(Action="run", Package="m", Test="TestHang"),
                _go_event(Action="output", Package="m", Test="TestHang", Output="x\n"),
            ]
        )
        assert eco.parse_go_test_json(text)["failed"] == 1

    def test_a_non_json_capture_yields_no_counts(self):
        """`go test` WITHOUT -json. The parser degrades; it does not invent."""
        assert (
            eco.parse_go_test_json("ok  \tm\t0.02s\n?   \tpkg\t[no test files]\n")
            is None
        )
        assert eco.parse_go_test_cases("ok  \tm\t0.02s\n") == ()

    def test_junit_case_extraction_reads_the_documents_own_elements(self):
        document = (
            '<testsuites><testsuite tests="3">'
            '<testcase classname="t" name="a" file="t.py" line="1" time="0.1"/>'
            '<testcase classname="t" name="b" file="t.py" line="5" time="0.2">'
            '<failure message="boom">trace</failure></testcase>'
            '<testcase classname="t" name="c"><skipped message="later"/></testcase>'
            "</testsuite></testsuites>"
        )
        cases = {c.test_id: c for c in eco.parse_junit_cases(document)}
        assert cases["t::a"].outcome == "pass"
        assert cases["t::b"].outcome == "fail" and cases["t::b"].message == "boom"
        assert cases["t::b"].line == 5
        assert cases["t::c"].outcome == "skip"
        assert cases["t::a"].duration_s == 0.1

    def test_a_malformed_junit_document_yields_no_cases_rather_than_raising(self):
        assert eco.parse_junit_cases("<testsuite><testcase") == ()
        assert eco.normalize_structured("<not-xml", eco.ecosystem("python")) is None


# ===========================================================================
# Configuration discipline
# ===========================================================================


class TestConfigDiscipline:
    """A value in `DEFAULTS` is merged into every task and every eval arm.

    Two knobs are needed for this round and NEITHER may carry a real value
    there: one is key-presence opt-in and one is a pure read. The tests below
    fail if either is published with a behaviour-changing value.
    """

    def test_no_ecosystem_default_switches_every_run(self):
        """A `None` entry is permitted for DISCOVERABILITY; a real value is not.

        The distinction is the whole of the rule (see `harness/config.py`): a
        default is merged into every task and every eval arm, so a value that
        changes behaviour switches all of them silently. `None` is what an
        ABSENT key already means to both consumers, so it changes nothing.
        """
        from harness.config import DEFAULTS

        for key, value in DEFAULTS.items():
            if not key.startswith("ecosystem"):
                continue
            assert value is None, (
                f"{key} is in DEFAULTS with {value!r}; a default is merged into "
                "every task and every eval arm, so a behaviour-changing value "
                "there switches all of them silently. Use a None entry, or read "
                "the key by presence."
            )

    def test_the_two_registry_keys_are_behaviour_neutral_when_absent(self):
        """Absent / None / False all mean "unchanged", and that is the default."""
        from harness.config import get_config

        merged = get_config({})
        for key in ("ecosystem", "ecosystem_protected_paths"):
            assert merged.get(key) is None, key
        assert eco.ecosystem(None) is None
        # A caller that passes a falsy value adds nothing rather than erroring.
        from harness.editor import extended_protected_patterns

        assert extended_protected_patterns(["tests/*"], ecosystem=None) == [
            "tests/*"
        ] + [pattern for pattern in extended_protected_patterns(["tests/*"])[1:]]
        # An unknown NAME is not silently treated as the caller's own globs.
        assert extended_protected_patterns(["tests/*"], ecosystem="klingon") == (
            extended_protected_patterns(["tests/*"])
        )

    def test_protected_paths_default_is_untouched_by_this_round(self):
        """The Python-shaped default is still the default.

        Changing it to be language-agnostic would either weaken the Python
        guarantee (drop the globs) or over-block every other language (ship
        every ecosystem's globs at once). The per-language answer is applied at
        resolution time instead.
        """
        from harness.config import DEFAULTS

        assert DEFAULTS["protected_paths"] == ["tests/*", "test_*.py", "*_test.py"]


# ===========================================================================
# Non-regression: the registry must not have disturbed what was already true
# ===========================================================================


class TestExistingVerdictSemanticsAreIntact:
    def test_a_repository_the_registry_does_not_know_is_unchanged(
        self, monkeypatch, tmp_path
    ):
        """No ecosystem -> no probe, no wrapper, no block, same commands."""
        repo = tmp_path / "unknown"
        _write(
            repo, {"m.py": "x = 1\n", "test_m.py": "def test_x():\n    assert x == 1\n"}
        )
        calls = _drive(monkeypatch, [_run("1 passed")])

        result = vf.verify(
            str(repo),
            "test_m.py::test_x",
            1,
            test_command="python -m pytest -q",
            verify_timeout_s=30,
        )
        assert calls == ["python -m pytest -q test_m.py::test_x", "python -m pytest -q"]
        assert result.target_test_passed is True
        assert result.regression_passed is True
        assert not hasattr(result, "ecosystem")
        assert "## ecosystem" not in result.raw_output

    def test_the_three_valued_flake_labels_survive(self, monkeypatch, tmp_path):
        """pass / fail / timeout mix -> flaky, and a timeout is its own outcome."""
        repo = go_repo(tmp_path / "flake")
        _drive(
            monkeypatch,
            [ExecutionResult(124, "", "", True), _run(_GO_TEST_FAILS, exit_code=1)],
        )
        result = vf.verify(repo, "internal/mathutil::TestAdd", 2, verify_timeout_s=60)
        assert result.flaky is True, "a pass/timeout mix must read as flaky"

    def test_a_consistent_failure_is_not_flaky(self, monkeypatch, tmp_path):
        repo = go_repo(tmp_path / "steady")
        _drive(monkeypatch, [_run(_GO_TEST_FAILS, exit_code=1)])
        result = vf.verify(repo, "internal/mathutil::TestAdd", 2, verify_timeout_s=60)
        assert result.flaky is False
        assert result.target_test_passed is False

    def test_the_worst_gate_is_reported_not_the_last_one(self, monkeypatch, tmp_path):
        """A passing target beside a vacuous regression run is not a pass."""
        repo = go_repo(tmp_path / "mixed", with_test=False)
        _drive(monkeypatch, [_run(_GO_ALL_PASS), _run(_GO_ZERO_TESTS)])
        result = vf.verify(repo, "internal/mathutil::TestAdd", 1, verify_timeout_s=60)
        assert result.target_test_passed is True
        assert result.regression_passed is False
        assert result.ecosystem_gate == eco.OUTCOME_NO_TESTS_COLLECTED
        assert result.ecosystem_blocks_success is True

    def test_an_unknown_gate_value_blocks_success(self):
        """A receipt that cannot classify a run must not describe it as healthy."""
        assert eco.blocks_success("pass") is False
        for gate in (
            eco.OUTCOME_FAIL,
            eco.OUTCOME_TIMEOUT,
            eco.OUTCOME_ERROR,
            eco.OUTCOME_NO_TESTS_COLLECTED,
            eco.OUTCOME_TOOLCHAIN_UNAVAILABLE,
            "something-new",
        ):
            assert eco.blocks_success(gate) is True

    def test_the_reports_sink_carries_an_ecosystem_row_naming_the_gate(
        self, monkeypatch, tmp_path
    ):
        repo = go_repo(tmp_path / "sink", with_test=False)
        reports: list = []
        _drive(monkeypatch, [_run(_GO_ZERO_TESTS)])
        vf.verify(repo, None, 1, verify_timeout_s=60, reports=reports)
        row = [item for item in reports if item.get("source") == "ecosystem"]
        assert len(row) == 1
        assert row[0]["outcome"] == eco.OUTCOME_NO_TESTS_COLLECTED
        assert row[0]["passed"] is False
        assert row[0]["ecosystem"] == "go"
        assert row[0]["zero_test_policy"] == eco.ZERO_TEST_FAIL_CLOSED
        assert row[0]["structured_report"] is True
        # The reason names the ecosystem and says the loud thing. This run's
        # structured counts already read "0 collected", so the parser's own
        # no_tests verdict wins over the marker match; either way the receipt
        # refuses and says why.
        assert (
            row[0]["notes"]
            and "zero collected tests is never a pass" in row[0]["notes"][0]
        )

    def test_no_command_found_still_refuses_and_names_the_ecosystem(
        self, monkeypatch, tmp_path
    ):
        """The 'no test command' branch gained the ecosystem's own commands."""
        repo = go_repo(tmp_path / "nocmd")
        monkeypatch.setattr(vf, "_autodetect_test_command", lambda _p: None)
        monkeypatch.setattr(vf, "execute_sandboxed", lambda *a, **k: pytest.fail("ran"))
        # A runner_template ecosystem composes its own command, so force the
        # refusal by removing the declared one from the detected entry.
        broken = eco.Ecosystem(
            **{
                **eco.ecosystem("go").__dict__,
                "test_command": "",
                "command_family": eco.COMMAND_PYTEST,
            }
        )
        monkeypatch.setattr(vf, "_ecosystem_for", lambda _p: broken)
        result = vf.verify(repo, None, 1, verify_timeout_s=30)
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert "go" in result.raw_output

    def test_the_hub_registry_is_consulted_and_delegates_the_python_family(
        self, monkeypatch, tmp_path
    ):
        """`ecosystems` is the one table, and it has no Python command fork."""
        from harness.config import DEFAULTS

        assert DEFAULTS["protected_paths"]
        calls: list = []
        monkeypatch.setattr(
            vf,
            "_autodetect_test_command",
            lambda _p: calls.append("detected") or "python -m pytest -q",
        )
        repo = tmp_path / "delegated"
        _write(repo, {"pyproject.toml": "[tool.pytest.ini_options]\n", "t.py": ""})
        _drive(monkeypatch, [_run("1 passed")])
        vf.verify(str(repo), None, 1, verify_timeout_s=30)
        assert calls == ["detected"], "the Python family must use the existing detector"
        assert eco.ecosystem("python").command_family == eco.COMMAND_PYTEST
        assert "ruff" not in re.sub(
            r"[^a-z]", "", (eco.ecosystem("python").test_command or "")
        )
