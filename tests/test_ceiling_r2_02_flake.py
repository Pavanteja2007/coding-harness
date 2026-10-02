"""R2-02 — the flake gate must be CAPABLE OF FIRING on the default config.

The defect this suite pins: one number meant both "how many target runs" and
"do we look for flakes", and the shipped default (`baseline_reruns=1`) made
``flaky`` structurally unsatisfiable. A test that passed once and failed on
the third run was reported as a clean, non-flaky pass.

Every test here is named after the behaviour it proves, and the two
non-vacuity rules are enforced structurally rather than by convention:

* a stability claim must rest on ``repetitions >= 2`` — asserted as a NUMBER,
  never only as the boolean, because ``flaky=False`` is also what a broken
  gate reports;
* a single-repetition run must say ``flake_check == "not_run"``, never
  ``"not_flaky"``, because "not flaky" reads as "we checked and it was
  stable".

Two of the required proofs are driven through REAL ``python -m pytest``
processes on the HOST (``execution.flake.run_local_command``, the documented
host-side measurement lane — production verification still goes through the
Docker sandbox). The genuinely flaky fixture alternates pass/fail via a
counter file, so the repetition series is a real property of the test, not a
scripted double handed to the gate. A real timeout is produced by a real
sleep exceeding a real timeout budget.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

from execution import flake_gate
from execution.flake import run_local_command
from execution.flake_gate import (
    DEFAULT_BASELINE_REPETITIONS,
    DEFAULT_POST_FIX_REPETITIONS,
    FLAKE_CHECK_VALUES,
    FLAKE_DETECTED,
    MAX_REPETITIONS,
    MIN_REPETITIONS_FOR_DETECTION,
    NOT_FLAKY,
    NOT_RUN,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    OUTCOME_TIMEOUT,
    STAGE_BASELINE,
    STAGE_POST_FIX,
    attach_evidence,
    classify_run,
    evaluate_repetitions,
    evidence_of,
    flake_verdict,
    observe_repetitions,
    render_receipt,
    repetitions_for_stage,
    resolve_repetitions,
    verdict_for,
)
from shared.types import ExecutionResult, VerificationResult

# --------------------------------------------------------------------------
# fixtures: a genuinely alternating test and a genuinely stable one
# --------------------------------------------------------------------------

_ALTERNATING = '''\
from pathlib import Path

_COUNTER = Path(__file__).parent / "counter.txt"


def test_alternates():
    """Pass, fail, pass, ... — a real flake, driven by real run-to-run state."""
    seen = int(_COUNTER.read_text()) if _COUNTER.exists() else 0
    _COUNTER.write_text(str(seen + 1))
    assert seen % 2 == 0, f"deliberately failing repetition {seen + 1}"
'''

_STABLE = """\
def test_stable():
    assert 1 + 1 == 2


def test_also_stable():
    assert "a" in "abc"
"""

_HANGING = """\
import time


def test_hangs():
    time.sleep(30)
    assert True
"""


def _write_repo(root: Path, source: str, name: str = "test_target.py") -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / name).write_text(source, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return root


def _run_target(root: Path, name: str = "test_target.py"):
    """Run one target test on the HOST through the documented local lane."""
    return run_local_command(
        [
            sys.executable,
            "-m",
            "pytest",
            f"tests/{name}",
            "-q",
            "-p",
            "no:randomly",
            "--no-header",
            "-o",
            "addopts=",
        ],
        cwd=str(root),
        timeout_s=60,
    )


def _scripted(labels, *, expected_tests=None):
    """A ``run_once`` that replays a fixed label vector through the real labeller.

    Still uses the production classifier — a real ``ExecutionResult`` is built
    and parsed — so the scripted proofs exercise the same labelling path a live
    sandbox run takes. Only the runner is replaced.
    exit 0 with "1 passed" is a pass; exit 1 with a failure summary is a fail;
    exit 124 / ``timed_out`` is a timeout.
    """
    payloads = {
        OUTCOME_PASS: (0, "1 passed in 0.01s"),
        OUTCOME_FAIL: (1, "1 failed, 1 passed in 0.01s"),
        OUTCOME_TIMEOUT: (124, ""),
    }
    sequence = list(labels)
    state = {"index": 0}

    def run_once(_index: int) -> ExecutionResult:
        label = sequence[state["index"] % len(sequence)]
        state["index"] += 1
        code, summary = payloads[label]
        return ExecutionResult(
            exit_code=code,
            stdout=summary,
            stderr="",
            timed_out=(label == OUTCOME_TIMEOUT),
        )

    return run_once


# ==========================================================================
# Required proof 1 — the DEFAULT configuration can detect a genuinely flaky test
# ==========================================================================


def test_default_configuration_can_detect_a_genuinely_flaky_test(tmp_path):
    """The shipped default repetition count can actually fire.

    Real pytest processes, a real alternating fixture. With the DEFAULT post-fix
    repetition count the first two runs already differ, so the verdict is
    ``flaky_detected`` — the behaviour that was structurally impossible before
    this module existed.
    """
    repo = _write_repo(tmp_path / "flaky", _ALTERNATING)
    policy = repetitions_for_stage(STAGE_POST_FIX, {})

    assert policy.repetitions >= MIN_REPETITIONS_FOR_DETECTION, (
        "the default post-fix repetition count must be able to detect a flake; "
        f"got {policy.repetitions}"
    )
    assert policy.detection_possible is True

    run = evaluate_repetitions(lambda _i: _run_target(repo), policy.repetitions)

    assert run.verdict.flake_check == FLAKE_DETECTED
    assert run.verdict.flaky is True
    assert run.verdict.repetitions == policy.repetitions
    assert run.verdict.repetitions >= 2
    assert run.verdict.observed_outcomes == (OUTCOME_PASS, OUTCOME_FAIL)
    assert run.verdict.distinct_outcomes == (OUTCOME_PASS, OUTCOME_FAIL)
    assert run.verdict.check() == []


def test_scripted_pass_fail_pass_is_reported_as_flaky_detected():
    """The prompt's named shape: pass, fail, pass -> ``flaky_detected``."""
    run = evaluate_repetitions(_scripted([OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_PASS]), 3)

    assert run.verdict.flake_check == FLAKE_DETECTED
    assert run.verdict.flaky is True
    assert run.verdict.repetitions == 3
    assert run.verdict.observed_outcomes == (OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_PASS)
    assert len(run.verdict.distinct_outcomes) == 2
    assert run.verdict.check() == []


# ==========================================================================
# Required proof 2 — a stable test reports not_flaky WITH the repetition count
# ==========================================================================


def test_stable_test_reports_not_flaky_with_at_least_two_repetitions(tmp_path):
    """``not_flaky`` must be EARNED by repetition count, not just a boolean.

    The assertion is on ``repetitions >= 2`` explicitly so a future refactor
    cannot make this pass vacuously by collapsing the series to one run and
    still reporting a false "not flaky".
    """
    repo = _write_repo(tmp_path / "stable", _STABLE)
    repetitions = 3

    run = evaluate_repetitions(lambda _i: _run_target(repo), repetitions)

    assert run.verdict.flake_check == NOT_FLAKY
    assert run.verdict.flaky is False
    assert run.verdict.repetitions == repetitions
    assert run.verdict.repetitions >= MIN_REPETITIONS_FOR_DETECTION
    assert run.verdict.detection_possible is True
    assert run.verdict.observed_outcomes == (OUTCOME_PASS,) * repetitions
    assert run.verdict.distinct_outcomes == (OUTCOME_PASS,)
    assert run.verdict.check() == []


def test_the_same_stable_test_on_one_repetition_reports_not_run(tmp_path):
    """The non-vacuity control for the proof above.

    Same fixture, same test, same runner — only the repetition count differs.
    If the ``not_flaky`` above were produced by a broken gate, this one would
    also say ``not_flaky``; it says ``not_run``, which is what makes the
    difference meaningful.
    """
    repo = _write_repo(tmp_path / "stable-once", _STABLE)

    run = evaluate_repetitions(lambda _i: _run_target(repo), 1)

    assert run.verdict.flake_check == NOT_RUN
    assert run.verdict.flaky is False
    assert run.verdict.repetitions == 1
    assert run.verdict.detection_possible is False


# ==========================================================================
# Required proof 3 — a single-run configuration says "not_run", never "not_flaky"
# ==========================================================================


@pytest.mark.parametrize("repetitions", [0, 1])
def test_single_run_configuration_reports_flake_check_not_run(repetitions):
    """0 and 1 both mean one run, and one run means "not checked"."""
    resolution = resolve_repetitions(repetitions, stage=STAGE_POST_FIX)
    assert resolution.repetitions == 1
    assert resolution.detection_possible is False

    run = evaluate_repetitions(
        _scripted([OUTCOME_PASS, OUTCOME_PASS]), resolution.repetitions
    )
    receipt = run.to_dict()

    assert receipt["flake_check"] == NOT_RUN
    assert receipt["flake_check"] != NOT_FLAKY
    assert receipt["repetitions"] == 1
    assert receipt["detection_possible"] is False
    assert receipt["flaky"] is False


def test_a_single_run_never_claims_stability_in_the_receipt():
    """``flaky: false`` with ``flake_check: not_run`` must never read as checked."""
    receipt = flake_verdict(1, [OUTCOME_PASS]).to_dict()
    assert receipt["flaky"] is False
    assert receipt["flake_check"] == NOT_RUN
    assert receipt["detection_possible"] is False
    assert "not_flaky" not in render_receipt(flake_verdict(1, [OUTCOME_PASS]))


def test_requested_repetitions_cannot_manufacture_stability_from_one_observation():
    """A caller that claims 3 repetitions but reports 1 outcome gets ``not_run``."""
    verdict = flake_verdict(3, [OUTCOME_PASS])

    assert verdict.flake_check == NOT_RUN
    assert verdict.repetitions == 1
    assert verdict.requested_repetitions == 3
    assert verdict.detection_possible is False
    assert any("observed" in note for note in verdict.notes)


def test_no_observed_outcome_is_not_run_not_an_error_pass():
    verdict = flake_verdict(0, [])

    assert verdict.flake_check == NOT_RUN
    assert verdict.repetitions == 0
    assert verdict.flaky is False
    assert verdict.check() == []


# ==========================================================================
# Required proof 4 — timeout stays a third outcome
# ==========================================================================


def test_a_timeout_outcome_is_never_a_pass():
    timed_out = ExecutionResult(exit_code=124, stdout="", stderr="", timed_out=True)
    exit_124 = ExecutionResult(exit_code=124, stdout="", stderr="", timed_out=False)

    assert classify_run(timed_out) == OUTCOME_TIMEOUT
    assert classify_run(exit_124) == OUTCOME_TIMEOUT
    assert flake_verdict(1, [classify_run(timed_out)]).flake_check == NOT_RUN


def test_pass_then_timeout_mix_is_flaky_not_a_stable_pass():
    """The documented worst case: a test that passed once then hung."""
    run = evaluate_repetitions(_scripted([OUTCOME_PASS, OUTCOME_TIMEOUT]), 2)

    assert run.verdict.flake_check == FLAKE_DETECTED
    assert run.verdict.repetitions == 2
    assert run.verdict.observed_outcomes == (OUTCOME_PASS, OUTCOME_TIMEOUT)
    assert run.verdict.timed_out is True


def test_fail_then_timeout_mix_is_flaky():
    run = evaluate_repetitions(_scripted([OUTCOME_FAIL, OUTCOME_TIMEOUT]), 2)

    assert run.verdict.flake_check == FLAKE_DETECTED
    assert run.verdict.timed_out is True
    assert set(run.verdict.distinct_outcomes) == {OUTCOME_FAIL, OUTCOME_TIMEOUT}


def test_a_consistently_hanging_test_is_consistently_broken_not_flaky(tmp_path):
    """Timeout is not a flake: every repetition timing out is a stable failure.

    Driven by a real sleep exceeding a real timeout budget through the real
    local runner, not by a synthesised label.
    """
    repo = _write_repo(tmp_path / "hangs", _HANGING)

    def run_once(_index: int):
        return run_local_command(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_target.py",
                "-q",
                "-p",
                "no:randomly",
                "--no-header",
                "-o",
                "addopts=",
            ],
            cwd=str(repo),
            timeout_s=2,
        )

    run = evaluate_repetitions(run_once, 2)

    assert run.verdict.observed_outcomes == (OUTCOME_TIMEOUT, OUTCOME_TIMEOUT)
    assert run.verdict.flake_check == NOT_FLAKY
    assert run.verdict.flaky is False
    assert run.verdict.timed_out is True
    assert run.verdict.detection_possible is True
    assert run.last_result is not None
    assert run.last_result.outcome == OUTCOME_TIMEOUT


# ==========================================================================
# The gate can never convert a failure into a pass, or a pass into a flake
# ==========================================================================


def test_a_consistently_failing_test_is_not_flaky_and_not_a_pass(tmp_path):
    """All-fail over several repetitions: a real failure, correctly labelled."""
    repo = _write_repo(tmp_path / "broken", "def test_broken():\n    assert False\n")

    run = evaluate_repetitions(lambda _i: _run_target(repo), 3)

    assert run.verdict.observed_outcomes == (OUTCOME_FAIL,) * 3
    assert run.verdict.flake_check == NOT_FLAKY
    assert run.verdict.flaky is False
    assert run.last_result.passed is False


def test_the_gate_never_reports_a_flaky_verdict_for_an_identical_series():
    verdict = flake_verdict(MAX_REPETITIONS, [OUTCOME_PASS] * MAX_REPETITIONS)

    assert verdict.flake_check == NOT_FLAKY
    assert verdict.flaky is False
    assert verdict.repetitions == MAX_REPETITIONS


@pytest.mark.parametrize(
    "labels, expected",
    [
        ([OUTCOME_PASS, OUTCOME_FAIL], FLAKE_DETECTED),
        ([OUTCOME_FAIL, OUTCOME_PASS], FLAKE_DETECTED),
        ([OUTCOME_PASS, OUTCOME_TIMEOUT], FLAKE_DETECTED),
        ([OUTCOME_TIMEOUT, OUTCOME_FAIL], FLAKE_DETECTED),
        ([OUTCOME_PASS, OUTCOME_PASS], NOT_FLAKY),
        ([OUTCOME_FAIL, OUTCOME_FAIL], NOT_FLAKY),
        ([OUTCOME_TIMEOUT, OUTCOME_TIMEOUT], NOT_FLAKY),
    ],
)
def test_the_exhaustive_two_repetition_matrix(labels, expected):
    verdict = flake_verdict(2, labels)

    assert verdict.flake_check == expected
    assert verdict.flaky is (expected == FLAKE_DETECTED)
    assert verdict.check() == []


# ==========================================================================
# Repetition policy: the baseline/post-fix split, and the cost ceiling
# ==========================================================================


def test_the_baseline_default_is_one_run_and_cannot_detect():
    policy = repetitions_for_stage(STAGE_BASELINE, {})

    assert policy.repetitions == DEFAULT_BASELINE_REPETITIONS == 1
    assert policy.detection_possible is False


def test_the_post_fix_default_can_detect():
    policy = repetitions_for_stage(STAGE_POST_FIX, {})

    assert policy.repetitions == DEFAULT_POST_FIX_REPETITIONS
    assert policy.repetitions >= MIN_REPETITIONS_FOR_DETECTION
    assert policy.detection_possible is True


def test_an_explicit_zero_disables_detection_rather_than_re_enabling_it():
    """Key MEANING, not truthiness: absent -> default, explicit 0 -> one run.

    A truthiness check would treat ``0`` as "not configured" and silently
    re-enable the very detection a caller switched off.
    """
    absent = repetitions_for_stage(STAGE_POST_FIX, {})
    explicit_zero = repetitions_for_stage(STAGE_POST_FIX, {"post_fix_reruns": 0})
    explicit_none = repetitions_for_stage(STAGE_POST_FIX, {"post_fix_reruns": None})

    assert absent.repetitions >= 2
    assert explicit_zero.repetitions == 1
    assert explicit_zero.detection_possible is False
    assert explicit_none.repetitions == absent.repetitions


def test_the_legacy_config_key_still_resolves_to_one_number():
    """A config written before the split must not silently change meaning."""
    legacy = repetitions_for_stage(STAGE_POST_FIX, {"baseline_reruns": 4})
    new = repetitions_for_stage(STAGE_POST_FIX, {"post_fix_reruns": 4})

    assert legacy.repetitions == new.repetitions == 4
    assert "legacy key" in legacy.source


def test_repetitions_are_capped_so_a_mistyped_value_cannot_buy_unbounded_runs():
    resolution = resolve_repetitions(10_000, stage=STAGE_POST_FIX)

    assert resolution.repetitions == MAX_REPETITIONS
    assert resolution.source == "clamped"
    assert any("clamped" in note for note in resolution.notes)


def test_a_caller_may_tighten_the_ceiling_but_not_loosen_it():
    tightened = resolve_repetitions(50, stage=STAGE_POST_FIX, ceiling=3)
    loosened = resolve_repetitions(50, stage=STAGE_POST_FIX, ceiling=1000)

    assert tightened.repetitions == 3
    assert loosened.repetitions == MAX_REPETITIONS


def test_a_bool_repetition_value_is_refused_rather_than_coerced():
    with pytest.raises(ValueError, match="not a bool"):
        resolve_repetitions(True, stage=STAGE_POST_FIX)
    with pytest.raises(ValueError, match="not a bool"):
        resolve_repetitions(False, stage=STAGE_POST_FIX)


def test_an_unknown_stage_is_refused():
    with pytest.raises(ValueError, match="unknown repetition stage"):
        repetitions_for_stage("whenever", {})


def test_repetition_policy_receipts_are_json_serializable():
    payload = repetitions_for_stage(STAGE_POST_FIX, {"post_fix_reruns": 3}).to_dict()

    assert json.loads(json.dumps(payload))["repetitions"] == 3


# ==========================================================================
# Auditability: repetitions and observed outcomes on the result and the trace
# ==========================================================================


def test_evidence_is_recorded_on_the_verification_result_and_round_trips_from_disk(
    tmp_path,
):
    """The claim must be reconstructable from an artifact, not from memory."""
    result = VerificationResult(
        target_test_passed=True,
        baseline_passed=False,
        regression_passed=True,
        flaky=False,
        raw_output="",
    )
    run = evaluate_repetitions(_scripted([OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_PASS]), 3)

    attach_evidence(result, run.verdict)

    assert result.flake_check == FLAKE_DETECTED
    assert result.repetitions == 3
    assert result.observed_outcomes == [OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_PASS]
    # The historical boolean still means what it always meant.
    assert result.flaky is True
    # The pre-existing fields are untouched.
    assert result.target_test_passed is True
    assert result.regression_passed is True

    trace_row = run.to_dict()
    path = tmp_path / "logs" / "task-1" / "flake_receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace_row, indent=2), encoding="utf-8")

    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["flake_check"] == FLAKE_DETECTED
    assert reloaded["repetitions"] == 3
    assert reloaded["observed_outcomes"] == [
        OUTCOME_PASS,
        OUTCOME_FAIL,
        OUTCOME_PASS,
    ]
    assert reloaded["elapsed_s"] >= 0.0


def test_a_not_run_evidence_never_claims_stability_when_read_back():
    result = VerificationResult(
        target_test_passed=True,
        baseline_passed=False,
        regression_passed=True,
        flaky=False,
        raw_output="",
    )
    attach_evidence(result, flake_verdict(1, [OUTCOME_PASS]))

    assert result.flake_check == NOT_RUN
    assert result.repetitions == 1
    assert result.flaky is False

    recovered = evidence_of(result)
    assert recovered is not None
    assert recovered.flake_check == NOT_RUN
    assert recovered.detection_possible is False


def test_a_result_with_no_receipt_reports_absent_not_not_flaky():
    """Legacy single-run results must read as 'no evidence', not as 'stable'."""
    legacy = VerificationResult(
        target_test_passed=True,
        baseline_passed=False,
        regression_passed=True,
        flaky=False,
        raw_output="",
    )

    assert evidence_of(legacy) is None
    assert evidence_of(None) is None


def test_the_receipt_line_names_the_repetition_count():
    """A reader must be able to tell 'checked and stable' from 'not checked'."""
    checked = render_receipt(flake_verdict(3, [OUTCOME_PASS] * 3))
    unchecked = render_receipt(flake_verdict(1, [OUTCOME_PASS]))

    assert "repetitions=3" in checked
    assert "not_flaky" in checked
    assert "repetitions=1" in unchecked
    assert "not_run" in unchecked
    assert checked != unchecked


def test_every_verdict_rests_in_the_closed_flake_check_vocabulary():
    for labels in (
        [],
        [OUTCOME_PASS],
        [OUTCOME_PASS] * 4,
        [OUTCOME_PASS, OUTCOME_FAIL],
    ):
        assert flake_verdict(len(labels), labels).flake_check in FLAKE_CHECK_VALUES


# ==========================================================================
# Runner behaviour: a broken repetition is recorded, never swallowed
# ==========================================================================


def test_a_raising_repetition_is_recorded_as_error_and_the_series_continues():
    state = {"index": 0}

    def run_once(_index: int) -> ExecutionResult:
        state["index"] += 1
        if state["index"] == 2:
            raise RuntimeError("sandbox blew up")
        return ExecutionResult(
            exit_code=0, stdout="1 passed", stderr="", timed_out=False
        )

    run = evaluate_repetitions(run_once, 3)

    assert run.verdict.observed_outcomes[0] == OUTCOME_PASS
    assert run.verdict.observed_outcomes[1] == flake_gate.OUTCOME_ERROR
    assert run.verdict.flake_check == FLAKE_DETECTED
    assert any("sandbox blew up" in note for note in run.verdict.notes)


def test_the_final_repetition_is_still_the_one_a_single_run_caller_read():
    """Switching a call site to this module must not change target_test_passed.

    The historical rule is "the LAST target run decides the result"; the
    observation must therefore keep handing back the final run.
    """
    run = evaluate_repetitions(_scripted([OUTCOME_FAIL, OUTCOME_PASS]), 2)

    assert run.verdict.observed_outcomes[-1] == OUTCOME_PASS
    assert run.last_result.exit_code == 0
    assert classify_run(run.last_result) == OUTCOME_PASS
    assert run.observation.results[-1] is run.last_result
    assert len(run.observation.results) == 2


def test_an_observation_and_its_verdict_agree():
    observation = observe_repetitions(_scripted([OUTCOME_PASS, OUTCOME_FAIL]), 2)
    verdict = verdict_for(observation)

    assert verdict.flake_check == FLAKE_DETECTED
    assert verdict.repetitions == observation.repetitions == 2
    assert observation.elapsed_s >= 0.0
    assert len(observation.per_repetition_s) == 2


# ==========================================================================
# Cost: measured, not asserted. The gate must stay cheap enough to enable.
# ==========================================================================


def test_the_added_wall_time_of_repetitions_is_measured_and_linear(tmp_path):
    """Measure one run vs three, so the cost claim is a number.

    Real ``python -m pytest`` processes on the host against the same fixture.
    The assertions are the two invariants that cannot be false by luck —
    more repetitions cannot be FASTER than fewer, and every repetition is
    accounted for — while the measured ratio is returned in the observation so
    it can be quoted in the handoff instead of guessed.
    """
    repo = _write_repo(tmp_path / "cost", _STABLE)

    single = evaluate_repetitions(lambda _i: _run_target(repo), 1)
    triple = evaluate_repetitions(lambda _i: _run_target(repo), 3)

    assert single.verdict.flake_check == NOT_RUN
    assert triple.verdict.flake_check == NOT_FLAKY
    assert triple.observation.repetitions == 3
    assert len(triple.observation.per_repetition_s) == 3
    assert triple.observation.elapsed_s >= single.observation.elapsed_s
    # The three-run series costs at most three single runs plus the one extra
    # run's own overhead; anything beyond that would mean repetitions are not
    # independent measurements.
    assert triple.observation.elapsed_s <= 3 * single.observation.elapsed_s + 1.0


# ==========================================================================
# The self-arming wiring pin (honest about what is NOT wired yet)
# ==========================================================================


def test_verify_is_either_unwired_or_uses_a_fireable_repetition_default():
    """A conditional invariant, so it self-arms when the call site lands.

    While ``execution/verify.py`` does not import this module, the assertion
    is that this module's own default can detect a flake. Once the handoff is
    applied, the same test instead asserts that the repetition count verify
    uses is >= :data:`MIN_REPETITIONS_FOR_DETECTION` — so a later refactor
    cannot quietly drop the gate back to one run and turn this test into a
    rubber stamp.
    """
    import execution.verify as verify_module

    source = inspect.getsource(verify_module)
    wired = "flake_gate" in source

    if not wired:
        assert DEFAULT_POST_FIX_REPETITIONS >= MIN_REPETITIONS_FOR_DETECTION
        return

    resolution = repetitions_for_stage(STAGE_POST_FIX, {})
    assert resolution.repetitions >= MIN_REPETITIONS_FOR_DETECTION
    assert flake_verdict(resolution.repetitions, [OUTCOME_PASS, OUTCOME_FAIL]).flaky
