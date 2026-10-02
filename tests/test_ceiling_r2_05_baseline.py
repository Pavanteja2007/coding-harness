"""VEX-CEILING Round 2 / R2-05 - the baseline failure set and environment triage.

The gap this suite pins, in the prompt's own terms:

  R2-G05  the verifier answers "did my target pass", never "was this test
          already broken before I touched anything"
  R2-G06  every failure is treated as a code failure, so an environment
          problem is handed to the model as a defect to fix
  R2-G24  a run that collected zero tests can pass

Each of the prompt's four REQUIRED proofs has a test named after it:

  1. a target that was already failing pre-fix is `preexisting`, not a
     regression, and the run says so
  2. a newly failing test is a regression and blocks success
  3. a missing declared dependency yields an environment classification and a
     non-repairable end state
  4. a zero-collected run is never a pass

The remaining tests pin the properties that make those four meaningful rather
than decorative: the classification vocabulary is closed and stable, the
environment classes live in the ONE existing policy table, a harness policy
refusal is never mistaken for a machine fault, an undeclared import stays a
code defect, an UNOBSERVED baseline cannot excuse a failure, and the new
verdict can only ever ADD a reason to refuse success - never remove one.

**Lane honesty.** This suite is host-only: it builds real `VerificationResult`
objects and drives the real modules (`execution.baseline_set`,
`execution.feedback`, `execution.result_parsing`, `harness.tool_errors`) over
real pytest-shaped captures. It runs NO Docker sandbox, no model, and no
network, so it is not evidence about the real Docker verifier or about a real
provider. The environment classes are driven with the exact messages and the
exact exception class name the sandbox layer uses (`SandboxUnavailableError`),
not with a real absent daemon. The two real end-to-end shapes (a pristine
verify writing the record, and `verify()`'s result assembly calling
`classify_run`) are NOT exercised here because both call sites belong to files
this prompt must not touch - they are filed as written requests in
`execution/AGENTS.md`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import execution.baseline_set as bs
import harness.tool_errors as te
from shared.types import VerificationResult

REPO_ROOT = Path(__file__).resolve().parents[1]

TARGET = "tests/test_mathutil.py::test_mean_two"
OTHER = "tests/test_mathutil.py::test_median_four"


# ---------------------------------------------------------------------------
# real pytest-shaped captures (the shapes the verifier actually produces)
# ---------------------------------------------------------------------------


def _failing_capture(test_id: str, *, expected: str = "3", actual: str = "6.0") -> str:
    """A `-q` capture with one FAILED test, in `verify()`'s own run format.

    The `$ <cmd>` / `exit=N` framing is `execution.verify._format_run`'s, so
    the parse path under test is the one a real run takes.
    """
    name = test_id.split("::")[-1]
    return (
        f"$ python -m pytest -q '{test_id}'\n"
        "exit=1\n"
        "=================================== FAILURES ===================================\n"
        f"_________________________________ {name} __________________________________\n"
        f"tests/test_mathutil.py:9: in {name}\n"
        f"    assert mean([2, 4]) == {expected}\n"
        f"E   assert {actual} == {expected}\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {test_id} - assert {actual} == {expected}\n"
        "========================= 1 failed, 3 passed in 0.12s ==========================\n"
    )


def _clean_capture() -> str:
    return (
        "$ python -m pytest -q\n"
        "exit=0\n"
        "=================== 4 passed in 0.10s ====================\n"
    )


def _missing_dependency_capture(module: str = "requests") -> str:
    return (
        "$ python -m pytest -q\n"
        "exit=1\n"
        "=================================== FAILURES ===================================\n"
        "________________________________ test_import _____________________________\n"
        "tests/test_client.py:3: in test_import\n"
        f"    import {module}\n"
        f"E   ModuleNotFoundError: No module named '{module}'\n"
        "========================= 1 failed in 0.05s ==========================\n"
    )


def _no_tests_capture() -> str:
    return "$ python -m pytest -q\n\nexit=5\nno tests ran (exit code 5)\n"


def _result(
    *,
    passed: bool,
    regression: Optional[bool] = None,
    flaky: bool = False,
    raw: str = "",
    structured: Optional[List[Dict[str, Any]]] = None,
) -> VerificationResult:
    """A real `shared.types.VerificationResult` with a real capture."""
    return VerificationResult(
        target_test_passed=passed,
        baseline_passed=False,
        regression_passed=passed if regression is None else regression,
        flaky=flaky,
        raw_output=raw,
        structured_feedback=list(structured or []),
    )


def _baseline_for(test_id: str = TARGET, **kwargs: Any) -> bs.BaselineSet:
    return bs.baseline_set_from_verification(
        _result(passed=False, raw=_failing_capture(test_id), **kwargs),
        target_test=TARGET,
    )


# ===========================================================================
# REQUIRED PROOF 1 - a pre-existing failure is not a regression
# ===========================================================================


def test_a_target_already_failing_pre_fix_is_preexisting_and_the_run_says_so():
    """PROOF 1. The pristine tree already fails the target; after the fix it
    still fails. That is a PRE-EXISTING failure, not damage this run caused,
    and the verdict must name it and count it as pre-existing."""
    baseline = _baseline_for()
    assert baseline.baseline_observed is True, (
        "a completed failing baseline is observed"
    )
    assert baseline.includes_target() is True, (
        "the baseline must see the target failing"
    )
    assert baseline.failing_ids == (TARGET,), baseline.failing_ids

    post = _result(passed=False, regression=False, raw=_failing_capture(TARGET))
    verdict = bs.classify_run(post, baseline=baseline, target_test=TARGET)

    # Named, counted, and attributed to the baseline - not left as a mystery.
    assert verdict.new_failure_count == 0, verdict.summary_line()
    assert verdict.preexisting_count == 1, verdict.summary_line()
    assert verdict.preexisting_failures[0].test_id == TARGET
    assert verdict.preexisting_still_failing_count == 1, "the intersection proves it"
    assert verdict.preexisting_still_failing[0].test_id == TARGET
    assert verdict.target_preexisting is True
    assert verdict.baseline_known is True
    # The run says so, in words a report can quote.
    assert "ALREADY failing before any edit" in " ".join(verdict.notes)
    assert f"preexisting_failures=1 [{TARGET}]" in verdict.summary_line()
    assert "preexisting_still_failing=1" in verdict.summary_line()
    # ...and an environment stop is NOT what this is: the fault is repairable.
    assert bs.should_stop_for_environment(verdict) is None
    # The target still fails, so the gate still refuses - the pre-existing
    # label changes the ATTRIBUTION, never the verdict.
    assert verdict.blocks_success is True


def test_a_fixed_target_on_an_already_red_repository_reports_the_red_others():
    """The other half of proof 1: when the fix works but the suite was already
    red, the report says so with numbers instead of a bare "passed"."""
    baseline = _baseline_for(OTHER)
    post = _result(passed=True, raw=_clean_capture())
    verdict = bs.classify_run(post, baseline=baseline, target_test=TARGET)

    assert verdict.target_passed is True
    assert verdict.new_failure_count == 0
    assert verdict.preexisting_count == 1
    assert verdict.preexisting_failures[0].test_id == OTHER
    assert "preexisting_failures=1" in verdict.summary_line()
    assert verdict.blocks_success is False, "a clean post-fix run is not blocked"


def test_a_baseline_that_never_completed_cannot_excuse_a_failure():
    """The honesty guard on the whole feature. A baseline that TIMED OUT leaves
    an empty failure set that means UNKNOWN; excusing a post-fix failure against
    it would make a real regression invisible."""
    timed_out = _result(
        passed=False,
        regression=False,
        raw=(
            f"$ python -m pytest -q '{TARGET}'\n"
            "exit=124 TIMEOUT\n"
            "no output survived the kill\n"
        ),
    )
    baseline = bs.baseline_set_from_verification(timed_out, target_test=TARGET)
    assert baseline.baseline_observed is False
    assert baseline.failures == (), "a killed run names no failing tests"
    assert any("UNKNOWN" in note for note in baseline.notes), baseline.notes

    post = _result(passed=False, regression=False, raw=_failing_capture(OTHER))
    verdict = bs.classify_run(post, baseline=baseline, target_test=TARGET)
    assert verdict.baseline_known is False
    assert verdict.new_failure_count == 1, "an unknown baseline excuses nothing"
    assert verdict.preexisting_count == 0
    assert verdict.blocks_success is True


def test_no_baseline_at_all_reports_every_failure_as_new():
    """Same rule with the record absent entirely (a caller that never ran the
    pristine verify, or a lost artifact)."""
    post = _result(passed=False, regression=False, raw=_failing_capture(OTHER))
    verdict = bs.classify_run(post, target_test=TARGET)
    assert verdict.baseline_known is False
    assert verdict.new_failure_count == 1
    assert any("no completed baseline" in note for note in verdict.notes)
    assert verdict.blocks_success is True


# ===========================================================================
# REQUIRED PROOF 2 - a newly failing test is a regression
# ===========================================================================


def test_a_newly_failing_test_is_a_regression_and_blocks_success():
    """PROOF 2. The baseline was green; after the edit a DIFFERENT test fails.
    That is damage this run caused and it must be attributed as such."""
    baseline = _baseline_for()  # the pristine tree fails only the target
    post = _result(
        passed=False,
        regression=False,
        raw=(
            f"$ python -m pytest -q '{OTHER}'\n"
            "exit=1\n"
            "=================================== FAILURES ===================================\n"
            "________________________________ test_median_four _______________________________\n"
            "tests/test_mathutil.py:21: in test_median_four\n"
            "    assert median([1, 2, 3, 4]) == 4\n"
            "E   assert 2.5 == 4\n"
            "=========================== short test summary info ============================\n"
            f"FAILED {OTHER} - assert 2.5 == 4\n"
            "========================= 1 failed in 0.10s ==========================\n"
        ),
    )
    verdict = bs.classify_run(post, baseline=baseline, target_test=TARGET)

    assert verdict.new_failure_count == 1, verdict.summary_line()
    assert verdict.new_failures[0].test_id == OTHER
    # The repository still carries the failure it arrived with, and it is
    # reported as such rather than folded into the new one.
    assert verdict.preexisting_count == 1
    assert verdict.preexisting_failures[0].test_id == TARGET
    assert verdict.preexisting_still_failing_count == 0, "the target is not failing now"
    assert verdict.blocks_success is True, "a new failure must block success"


def test_the_verdict_can_only_add_a_reason_to_refuse_success():
    """Fail-closed by construction. The existing mint requires target AND
    regression AND not-flaky; `blocks_success` must refuse at least everything
    that mint refuses, across the whole truth table, and must additionally
    refuse a zero-test run and a non-repairable environment fault."""
    existing_mint_ok = 0
    refusals_the_old_mint_allowed = 0
    for passed in (True, False):
        for regression in (True, False, None):
            for flaky in (True, False):
                for zero in (True, False):
                    verdict = bs.BaselineVerdict(
                        target_passed=passed,
                        regression_passed=regression,
                        flaky=flaky,
                        zero_tests_collected=zero,
                        baseline_known=True,
                    )
                    old_mint_ok = passed and regression is True and not flaky
                    if old_mint_ok:
                        existing_mint_ok += 1
                    if not old_mint_ok:
                        # Every combination the existing mint refuses, the new
                        # verdict must refuse too.
                        assert verdict.blocks_success is True, (
                            f"weakened the gate: {passed=} {regression=} {flaky=}"
                        )
                    else:
                        # And the new verdict refuses strictly MORE: exactly the
                        # vacuous greens (zero collected with everything else
                        # green) are the cases it adds.
                        if verdict.blocks_success:
                            refusals_the_old_mint_allowed += 1
    assert existing_mint_ok == 2, existing_mint_ok
    assert refusals_the_old_mint_allowed == 1, (
        "the only added refusal must be the vacuous green (zero collected with "
        "everything else green)"
    )

    # The only configuration that passes the new gate.
    ok = bs.BaselineVerdict(
        target_passed=True, regression_passed=True, flaky=False, baseline_known=True
    )
    assert ok.blocks_success is False


def test_an_environment_fault_alone_refuses_a_otherwise_green_run():
    """The last addition `blocks_success` makes over the existing mint: a
    machine that is broken cannot yield a verified fix, even when the verifier's
    own booleans happen to read green."""
    verdict = bs.BaselineVerdict(
        target_passed=True,
        regression_passed=True,
        flaky=False,
        baseline_known=True,
        environment=bs.EnvironmentVerdict(
            environment=True,
            classification=te.KIND_ENV_DOCKER_UNAVAILABLE,
            repairable_by_edit=False,
            run_status="error",
        ),
    )
    assert verdict.blocks_success is True


def test_unknown_regression_evidence_is_not_a_green_light():
    """An absent signal is not a pass: `regression_passed=None` blocks."""
    verdict = bs.BaselineVerdict(
        target_passed=True, regression_passed=None, flaky=False, baseline_known=True
    )
    assert verdict.blocks_success is True


# ===========================================================================
# REQUIRED PROOF 3 - a missing DECLARED dependency is an environment fault
# ===========================================================================


def test_a_missing_declared_dependency_is_environment_and_ends_non_repairable():
    """PROOF 3. `requests` is in requirements.txt and cannot be imported. That
    is a broken machine, the run must end in a state that says so, and nothing
    about editing the repository may be presented as the repair."""
    raw = _missing_dependency_capture("requests")
    post = _result(passed=False, regression=False, raw=raw)

    environment = bs.triage_environment(
        output=raw, declared_dependencies=["requests", "pytest"]
    )
    assert environment.environment is True
    assert environment.classification == te.KIND_ENV_MISSING_DEPENDENCY
    assert environment.repairable_by_edit is False
    assert environment.run_status == "error", "the honest TaskResult status"
    assert "requests" in environment.matched_dependencies

    baseline = _baseline_for()
    verdict = bs.classify_run(
        post, baseline=baseline, target_test=TARGET, declared=["requests"]
    )
    assert verdict.environment.environment is True
    assert verdict.environment.classification == te.KIND_ENV_MISSING_DEPENDENCY
    assert verdict.blocks_success is True

    end_state = bs.should_stop_for_environment(verdict)
    assert end_state is not None, "an environment fault must produce an end state"
    assert end_state["status"] == "error"
    assert end_state["repairable_by_edit"] is False
    assert end_state["classification"] == te.KIND_ENV_MISSING_DEPENDENCY
    assert end_state["operator_action"], "the operator needs to be told what to do"
    # And it never says anything a reader could mistake for success.
    blob = json.dumps(end_state).lower()
    for forbidden in ("success", "verified", "completed_verified"):
        assert forbidden not in blob, f"{forbidden!r} in the end state: {end_state}"


def test_an_environment_fault_stops_the_loop_instead_of_proposing_another_edit():
    """The behavioural half of proof 3: the recovery action for an environment
    class stops the turn, invents no narrower command, and tells the model the
    repository is not the problem."""
    policy = te.RecoveryPolicy()
    err = te.classify_environment(
        output="E   ModuleNotFoundError: No module named 'requests'",
        declared_dependencies=["requests"],
    )
    assert err is not None and err.kind == te.KIND_ENV_MISSING_DEPENDENCY

    action = te.on_environment_failure(policy, err, command="python -m pytest -q")
    assert action.stop is True, "the loop must end, not try again"
    assert action.next_command is None, "no narrower command fixes a missing install"
    assert action.replan is False
    assert action.forbidden == ()
    assert "ENVIRONMENT fault" in action.instruction
    assert "cannot repair it" in action.instruction
    assert "repairable by editing this repository: NO" in action.evidence
    assert te.is_repairable_by_edit(action.kind) is False


def test_an_undeclared_import_stays_a_code_defect():
    """The other half of proof 3, and the guard against over-claiming. A module
    the repository never declared is a code bug; calling it an environment
    fault would excuse a real defect and stop a run that should keep working."""
    raw = _missing_dependency_capture("mylib")
    assert (
        bs.triage_environment(
            output=raw, declared_dependencies=["requests"]
        ).environment
        is False
    )
    # No manifest evidence at all is equally not enough.
    assert (
        bs.triage_environment(output=raw, declared_dependencies=()).environment is False
    )
    # The existing classifier still owns it, as `import_error`.
    assert te.classify(1, raw, "", False).kind == "import_error"


def test_a_harness_policy_refusal_is_never_an_environment_fault():
    """The deny guard's PermissionError shares the OS wording. Without an
    explicit exclusion, `env_repo_permission` would report "fix the machine"
    for a refusal the harness made on purpose."""
    for text in (
        "permission denied on protected path: harness safety guard rejected "
        "the command (rm -rf /)",
        "permission denied on protected path: harness deny-guard refused this",
        "TOOL ERROR [command_rejected]: permission denied on protected path",
    ):
        assert te.classify_environment(output=text) is None, text
        assert bs.triage_environment(output=text).environment is False, text
    # It stays a code-class refusal the existing table already handles.
    assert te.classify(1, "", "permission denied on protected path", False).kind in {
        "permission_denied",
        "command_rejected",
    }


# ===========================================================================
# REQUIRED PROOF 4 - zero collected tests is never a pass
# ===========================================================================


def test_a_zero_collected_run_is_never_a_pass():
    """PROOF 4. Exit 5 / "no tests ran" with a ZERO exit code both land here.
    A vacuous green is the R2-G24 class, and neither the baseline record nor
    the verdict may treat it as clean."""
    for raw, exit_hint in (
        (_no_tests_capture(), "exit 5"),
        (
            "$ python -m pytest -q\n\nexit=0\n"
            "============================= no tests ran =============================\n",
            "exit 0 with a no-tests marker",
        ),
    ):
        baseline = bs.baseline_set_from_verification(
            _result(passed=False, regression=False, raw=raw), target_test=TARGET
        )
        assert baseline.outcome == "no_tests", (exit_hint, baseline.outcome)
        assert baseline.baseline_observed is False, (
            "a baseline that collected nothing did not observe anything"
        )
        assert baseline.failures == (), (
            "a run that collected nothing has no per-test failure set"
        )
        assert any("never a clean baseline" in note for note in baseline.notes)

        verdict = bs.classify_run(
            _result(passed=False, regression=False, raw=raw),
            baseline=baseline,
            target_test=TARGET,
        )
        assert verdict.zero_tests_collected is True, exit_hint
        assert verdict.blocks_success is True, exit_hint
        assert "never a pass" in verdict.summary_line(), exit_hint


def test_a_zero_collected_run_is_not_attributed_to_the_baseline():
    """A collection error names no test, so it must not inflate the pre-existing
    set with a test that never ran."""
    baseline = bs.BaselineSet(
        target_test=TARGET,
        baseline_observed=True,
        outcome="no_tests",
        failures=(),
    )
    post = _result(passed=False, regression=False, raw=_no_tests_capture())
    verdict = bs.classify_run(post, baseline=baseline, target_test=TARGET)
    assert verdict.preexisting_count == 0
    assert verdict.new_failure_count == 0, "no test ran, so no test failed"
    assert verdict.zero_tests_collected is True
    assert verdict.blocks_success is True


# ===========================================================================
# the classification vocabulary
# ===========================================================================


def test_the_environment_vocabulary_is_named_closed_and_stable():
    """The five classes the prompt names, in one place, with the reported
    order pinned. A caller may key off these strings, so the SET is contract."""
    assert bs.ENV_CLASSIFICATION_NONE == "none"
    assert te.ENVIRONMENT_KINDS == (
        "env_docker_unavailable",
        "env_missing_interpreter",
        "env_unreachable_network",
        "env_missing_dependency",
        "env_repo_permission",
    )
    for kind in te.ENVIRONMENT_KINDS:
        assert te.is_environment_kind(kind) is True
        assert te.is_repairable_by_edit(kind) is False
        assert te.environment_action(kind), "every class must name its action"
        assert kind in te.POLICY, "a class with no policy row is not a policy"
    for kind in ("timeout", "import_error", "file_not_found", "not_a_kind", ""):
        assert te.is_environment_kind(kind) is False
        assert te.is_repairable_by_edit(kind) is True
        assert te.environment_action(kind) is None


def test_the_environment_classes_live_in_the_one_existing_policy_table():
    """One table, not two. Two tables is a second policy, and the two drift.
    Distinctness is re-pinned here because adding rows can break it."""
    actions = {kind: value[0] for kind, value in te.POLICY.items()}
    assert len(set(actions.values())) == len(actions), actions
    env_actions = [te.environment_action(kind) for kind in te.ENVIRONMENT_KINDS]
    assert len(set(env_actions)) == len(env_actions), env_actions
    for kind in te.ENVIRONMENT_KINDS:
        assert kind in te.POLICY, f"{kind} has no policy row"
        instruction = te.POLICY[kind][1]
        assert "ENVIRONMENT fault" in instruction, kind
        assert "cannot repair it" in instruction, kind


def test_every_named_environment_class_is_recognised_from_its_real_shape():
    """Each of the five classes, from the exact message the producing layer
    emits. `SandboxUnavailableError` is matched by CLASS NAME (the sandbox
    raises by name rather than falling back), not imported, so this module
    keeps its zero-boundary-dependency property."""
    import harness.tool_errors as tool_errors

    class SandboxUnavailableError(Exception):  # the sandbox's own name
        pass

    cases = {
        te.KIND_ENV_DOCKER_UNAVAILABLE: dict(
            exc=SandboxUnavailableError("the system cannot find the file specified")
        ),
        te.KIND_ENV_MISSING_INTERPRETER: dict(
            output="/bin/sh: 1: python3: not found", command="python3 -m pytest"
        ),
        te.KIND_ENV_UNREACHABLE_NETWORK: dict(
            output=(
                "pip: WARNING: Retrying after connection error: Temporary "
                "failure in name resolution"
            )
        ),
        te.KIND_ENV_MISSING_DEPENDENCY: dict(
            output="ERROR: Could not find a version that satisfies the requirement foo==9.9"
        ),
        te.KIND_ENV_REPO_PERMISSION: dict(
            output="OSError: [Errno 30] Read-only file system: /repo/src/x.py",
            repo_path="/repo",
        ),
    }
    assert set(cases) == set(te.ENVIRONMENT_KINDS), "a class has no recogniser"
    for expected, kwargs in cases.items():
        err = tool_errors.classify_environment(**kwargs)
        assert err is not None, expected
        assert err.kind == expected, f"{expected} <- {err.kind}"
        assert te.is_repairable_by_edit(err.kind) is False
        assert err.hint, "every environment class must say what would repair it"


def test_a_missing_binary_that_is_not_an_interpreter_is_not_an_environment_fault():
    """`ls` is not in the image; the caller may legitimately use the installed
    equivalent, so this stays an ordinary `command_not_found`."""
    assert te.classify_environment(output="ls: command not found", command="ls") is None
    assert (
        bs.triage_environment(
            output="ls: command not found", declared_dependencies=[]
        ).environment
        is False
    )


def test_a_read_only_filesystem_outside_the_repository_is_not_its_fault():
    """A read-only `/usr` is not this run's problem, and neither is a bare
    EACCES with no repository named."""
    assert (
        te.classify_environment(
            output="OSError: [Errno 30] Read-only file system: /usr/lib/x"
        )
        is None
    )
    assert (
        te.classify_environment(
            output="OSError: [Errno 30] Read-only file system: /repo/src/x.py"
        )
        is None
    )
    assert (
        te.classify_environment(
            output="OSError: [Errno 30] Read-only file system: /repo/src/x.py",
            repo_path="/repo",
        ).kind
        == te.KIND_ENV_REPO_PERMISSION
    )


def test_a_successful_run_is_never_an_environment_fault():
    """A passing command that happens to print an environment-shaped sentence
    must not manufacture a fault."""
    result = type(
        "R",
        (),
        {
            "exit_code": 0,
            "stdout": "Cannot connect to the Docker daemon? no.\n4 passed",
            "stderr": "",
            "timed_out": False,
        },
    )()
    assert te.environment_from_result(result) is None


# ===========================================================================
# declared-dependency evidence
# ===========================================================================


def test_declared_dependencies_are_read_from_the_manifests_that_declare_them(tmp_path):
    """The evidence an import error needs. Requirements, pyproject, setup.cfg and
    package.json, with a degraded read reported rather than hidden."""
    (tmp_path / "requirements.txt").write_text(
        "# comment\nrequests>=2.31\nflask==3.0.0\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = [\n  "zope.interface>=5",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8"
    )
    names, notes = bs.declared_dependencies(str(tmp_path))
    assert {"requests", "flask", "zope.interface", "lodash"} <= set(names), names
    assert notes == ()

    # A distribution whose import name differs from its install name still
    # matches, or every renamed distribution would read as a code defect.
    err = te.classify_environment(
        output="E   ModuleNotFoundError: No module named 'zope.interface'",
        declared_dependencies=names,
    )
    assert err is not None and err.kind == te.KIND_ENV_MISSING_DEPENDENCY


def test_an_unreadable_repository_is_reported_not_raised():
    names, notes = bs.declared_dependencies(str(Path(os.sep) / "nope-r2-05"))
    assert names == ()
    assert notes and "not a readable directory" in notes[0]
    # No repository at all is the caller's choice, not a failed scan.
    assert bs.declared_dependencies(None) == ((), ())


# ===========================================================================
# persistence
# ===========================================================================


def test_the_baseline_record_round_trips_through_the_run_directory(tmp_path):
    """The record is auditable evidence, so it must survive the process and
    refuse an unknown schema version rather than half-reading it."""
    recorded = bs.record_baseline(
        _result(passed=False, raw=_failing_capture(TARGET)),
        run_dir=str(tmp_path),
        target_test=TARGET,
    )
    path = tmp_path / bs.BASELINE_FILE
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == bs.SCHEMA_VERSION
    assert on_disk["baseline_observed"] is True
    assert on_disk["failures"][0]["test_id"] == TARGET

    loaded = bs.load_baseline(str(tmp_path))
    assert loaded is not None
    assert loaded.to_dict() == recorded.to_dict()
    assert loaded.includes_target() is True

    # `classify_run` finds the record with only a run_dir.
    verdict = bs.classify_run(
        _result(passed=False, regression=False, raw=_failing_capture(TARGET)),
        run_dir=str(tmp_path),
        target_test=TARGET,
    )
    assert verdict.preexisting_count == 1
    assert verdict.new_failure_count == 0


def test_an_unusable_baseline_record_is_ignored_rather_than_trusted(tmp_path):
    """A corrupt, truncated, or future-version record must not excuse anything."""
    (tmp_path / bs.BASELINE_FILE).write_text("{not json", encoding="utf-8")
    assert bs.load_baseline(str(tmp_path)) is None

    (tmp_path / bs.BASELINE_FILE).write_text(
        json.dumps({"schema_version": 99, "failures": [{"test_id": OTHER}]}),
        encoding="utf-8",
    )
    assert bs.load_baseline(str(tmp_path)) is None
    with pytest.raises(ValueError):
        bs.BaselineSet.from_dict({"schema_version": 99})

    # Absent record + a failing post-fix run = a new failure, still blocking.
    (tmp_path / bs.BASELINE_FILE).unlink()
    verdict = bs.classify_run(
        _result(passed=False, regression=False, raw=_failing_capture(OTHER)),
        run_dir=str(tmp_path),
        target_test=TARGET,
    )
    assert verdict.baseline_known is False
    assert verdict.blocks_success is True


def test_an_unwritable_run_directory_never_fails_the_verification(tmp_path):
    """Persistence is observability, not gating: a lost record must be a note."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory is needed", encoding="utf-8")
    baseline = bs.record_baseline(
        _result(passed=False, raw=_failing_capture(TARGET)),
        run_dir=str(blocked),
        target_test=TARGET,
    )
    assert baseline.failing_ids == (TARGET,), "the in-memory set is still complete"
    assert any("could not be written" in note for note in baseline.notes)
    assert bs.default_baseline_path("") == ""


def test_a_baseline_with_no_result_is_unknown_not_clean():
    """An unobserved baseline changes ATTRIBUTION, not the gate.

    It must never excuse a failure (see the two tests above), but it also must
    not manufacture a refusal out of nothing: when the post-fix run's own
    evidence is fully green there is no failure to attribute, and blocking on a
    missing record would fail correct work over a lost artifact. In the real
    loop a crashed pristine verify already ends the task as `error`
    (`harness/core.py`), so this combination does not arise there.
    """
    baseline = bs.baseline_set_from_verification(None, target_test=TARGET)
    assert baseline.baseline_observed is False
    assert baseline.outcome == "error"
    assert any("no verification result" in note for note in baseline.notes)

    clean = bs.classify_run(
        _result(passed=True, raw=_clean_capture()), baseline=baseline
    )
    assert clean.blocks_success is False
    assert clean.baseline_known is False
    assert clean.new_failure_count == 0

    failing = bs.classify_run(
        _result(passed=False, regression=False, raw=_failing_capture(TARGET)),
        baseline=baseline,
        target_test=TARGET,
    )
    assert failing.new_failure_count == 1, "an unobserved baseline cannot vouch"
    assert failing.preexisting_count == 0
    assert failing.blocks_success is True


# ===========================================================================
# the record contract itself
# ===========================================================================


def test_a_failure_record_always_has_an_identity_and_matches_across_captures():
    """A set whose members cannot be identified cannot be compared, and a
    comparison that never matches reports every failure as new."""
    node = bs.FailureRecord(test_id=TARGET, file="tests/test_mathutil.py", line=9)
    located = bs.FailureRecord(test_id=None, file="tests/test_mathutil.py", line=9)
    named = bs.FailureRecord(summary="test_mean_two failed: expected 3, got 6.0")
    anonymous = bs.FailureRecord()

    for record in (node, located, named, anonymous):
        assert record.key, "every record must be identifiable"
    assert node.key == TARGET
    assert node.matches(located) is True, "file+line must match a node-id record"
    assert node.matches(named) is False
    assert named.key == "test_mean_two failed: expected 3, got 6.0"
    # A garbage row in a persisted record must not crash the reader.
    assert bs.FailureRecord.from_dict({"line": "nonsense"}).line is None
    assert bs.FailureRecord.from_dict({}).key == "unknown_failure"


def test_the_recorded_set_is_deduplicated_and_bounded(tmp_path):
    """The same failure appears in the target run and the suite run; counting
    it twice would overstate the pre-existing set."""
    capture = _failing_capture(TARGET) + "\n" + _failing_capture(TARGET)
    baseline = bs.baseline_set_from_verification(
        _result(passed=False, raw=capture), target_test=TARGET
    )
    assert len(baseline.failures) == 1, baseline.failing_ids
    assert baseline.preexisting_count == 1

    many = "\n".join(_failing_capture(f"tests/t.py::test_{i}") for i in range(12))
    capped = bs.baseline_set_from_verification(
        _result(passed=False, raw=many),
        target_test=TARGET,
        config={"baseline_set_max_failures": 3},
    )
    assert len(capped.failures) == 3
    assert any("lower bound" in note for note in capped.notes), capped.notes


def test_the_verdict_projection_is_json_ready_and_names_both_numbers(tmp_path):
    verdict = bs.classify_run(
        _result(passed=False, regression=False, raw=_failing_capture(TARGET)),
        baseline=_baseline_for(),
        target_test=TARGET,
    )
    payload = verdict.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["new_failure_count"] == 0
    assert payload["preexisting_failure_count"] == 1
    assert payload["target_preexisting"] is True
    assert payload["blocks_success"] is True
    assert payload["summary"].startswith("target_passed=False")
    # No completion-status vocabulary leaks into the projection.
    for forbidden in ("completed_verified", "completed_unverified"):
        assert forbidden not in json.dumps(payload)


# ===========================================================================
# opt-in gating
# ===========================================================================


def test_both_features_are_opt_in_by_key_presence_never_by_a_default_value():
    """Project rule 5. A value placed in `DEFAULTS` is merged into EVERY task
    and every eval arm at once, so an opt-in feature must be gated on the key
    being PRESENT. This suite also proves the keys are NOT in the defaults
    today, so nothing in the tree switched silently."""
    assert bs.enabled_for({}, "baseline_set_enabled") is False
    assert (
        bs.enabled_for({"baseline_set_enabled": False}, "baseline_set_enabled") is True
    )
    assert bs.enabled_for(None, "environment_triage_enabled") is False

    from harness.config import DEFAULTS

    for key in (
        "baseline_set_enabled",
        "environment_triage_enabled",
        "baseline_set_max_failures",
        "baseline_set_max_summary_chars",
    ):
        assert key not in DEFAULTS, (
            f"{key} is in DEFAULTS: it would switch every task and every eval arm"
        )


def test_the_filed_call_site_is_now_applied_and_still_key_presence_gated():
    """VEX-PF-10 APPLIED this round's filed handoff, so the self-arming pin is
    replaced rather than deleted.

    The original assertion was "the call site is an unapplied request" - a pin
    that was designed to FAIL the day somebody wired it, so the gap could not
    quietly become permanent. It has now failed, which is the pin working. The
    replacement is STRONGER than the assertion it supersedes: where the old pin
    only proved the wiring was absent, this one proves the wiring is present
    AND that it cannot fire without an operator's key, AND that its only effect
    on the result is to CLEAR a boolean.

    The module-level invariants from the original test are kept verbatim below:
    `execution/baseline_set.py` must not import a verifier, name a completion
    status, or reach into another module's internals. That is unchanged by any
    amount of wiring and is the reason the wiring could be safe to apply.
    """
    verify_source = (REPO_ROOT / "execution" / "verify.py").read_text(encoding="utf-8")
    core_source = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")

    # 1. The call site is LANDED, in the seam holder and at the run's gates.
    assert "from execution.baseline_set import" in verify_source, (
        "execution/verify.py must import the baseline set at its real call site; "
        "this round applied the request that was filed as a handoff"
    )
    assert "_baseline_set_rungs(" in verify_source, (
        "the seam function that records/consults the failure set is missing"
    )
    assert 'phase="baseline"' in core_source and 'phase="postfix"' in core_source, (
        "harness/core.py must declare WHICH tree each verification looked at; "
        "verify() is stateless about that and guessing from repo_path would be "
        "false precision"
    )
    assert "_verify_rung_kwargs(verify, cfg" in core_source, (
        "harness/core.py must hand the resolved config to the verifier"
    )

    # 2. Activation is by KEY PRESENCE, and the same rule the module owns.
    assert "rung_config: Optional[Mapping[str, Any]] = None" in verify_source, (
        "the seam parameter must default to None so an existing call site is "
        "byte-identical"
    )
    assert "_keys_present(rung_config, BASELINE_SET_CONFIG_KEYS)" in verify_source, (
        "the baseline set must be gated on key PRESENCE, never on a value"
    )

    # 3. The only effect on the result is to CLEAR a boolean. There is no code
    #    path in the seam that assigns True to a `VerificationResult` field.
    seam = verify_source.split("def _baseline_set_rungs(", 1)[1]
    seam = seam.split("\ndef ", 1)[0]
    clears = [
        line.strip()
        for line in seam.splitlines()
        if re.search(r"result\.\w+\s*=\s*True", line)
    ]
    assert not clears, (
        "the baseline-set fold may only CLEAR a boolean, so an unverified run "
        f"can never be dressed as verified; the seam assigns True here: {clears}"
    )

    assert "result.target_test_passed = False" in seam, (
        "the fold must be the fail-closed one it was documented to be"
    )
    assert "verdict.blocks_success" in seam, (
        "the fold must be conditioned on the module's own fail-closed property"
    )

    # 4. The module-level invariants from the original pin, unchanged.
    baseline_source = (REPO_ROOT / "execution" / "baseline_set.py").read_text(
        encoding="utf-8"
    )
    tool_errors_source = (REPO_ROOT / "harness" / "tool_errors.py").read_text(
        encoding="utf-8"
    )
    for source, name in (
        (baseline_source, "execution/baseline_set.py"),
        (tool_errors_source, "harness/tool_errors.py"),
    ):
        for forbidden in (
            "execution.verify",
            "completed_verified",
            "completed_unverified",
        ):
            assert forbidden not in source, f"{name} names {forbidden}"
        for forbidden_import in (
            "import harness.core",
            "from harness.core import",
            "import execution.verify",
            "from execution.verify import",
        ):
            assert forbidden_import not in source, f"{name} imports {forbidden_import}"
    # The classifier is DELEGATED, not forked: baseline_set calls the one table.
    assert "classify_environment" in baseline_source
    assert "_NETWORK_RE" not in baseline_source, (
        "a second environment-pattern table was forked instead of delegating"
    )
