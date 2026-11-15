"""P4/W2 pins: the four findings this round produced, made unfalsifiable.

**Every test is named after the behaviour it pins, and every behavioural test has
a control that proves it can fail.** That pairing is Wave 1's discipline rather
than a style preference: ``phases/DOCTRINE.md`` §3 records that *"a test that can
pass vacuously is worse than no test"*, and this round found the vacuous version of
its own measurement twice (a probe reporting "0 leaks" while every session had
crashed, and a rework metric that was zero by construction in one arm).

**The five non-negotiables, each with a pin:**

``A pressure scenario can never mint a false pass``
    ``test_no_pressure_scenario_mints_a_false_pass`` over all eight, plus the
    per-scenario pins, plus ``test_a_scenario_blocked_for_the_wrong_reason_would
   _be_reported_as_a_pass`` -- which is the failure mode that cost this round a
    false "enforcement" reading.
``allow_attested cannot arrive from an agent patch``
    ``test_an_agent_patch_cannot_supply_allow_attested`` -- the live escape hatch
    this round found and closed.
``Evidence must belong to the feature and the command it is claimed for``
    ``test_evidence_from_another_feature_is_refused`` and
    ``test_evidence_from_another_command_is_refused``.
``A flaky check is not a pass``
    ``test_a_flaky_outcome_exists_and_is_not_pass_evidence``.
``A measurement cannot report a pass over a run that did not happen``
    ``test_the_probe_refuses_to_publish_a_verdict_when_nothing_ran``.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Dict

import pytest

import harness.long_horizon as lh
import harness.multi_session_probe as msp
import harness.phase4_measure as p4m
import harness.pressure_attack as pa
import harness.read_before_edit_scope as rbes
import harness.session_boundary as sb

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The agent cannot reach the attested escape hatch
# ---------------------------------------------------------------------------


def _human_ledger() -> lh.FeatureLedger:
    return lh.new_feature_ledger(
        "p",
        "r",
        (
            lh.new_feature(
                "h1",
                "human-checked",
                lh.VerificationMethod(method="human_check", human_check="a human signs off"),
            ),
        ),
    )


def _attested(feature_id: str = "h1") -> lh.VerificationRun:
    return lh.VerificationRun(
        feature_id=feature_id,
        method="human_check",
        command="",
        outcome="not_run",
        kind="attested",
        note="a human said so",
    )


def test_an_agent_patch_cannot_supply_allow_attested():
    """The escape hatch is an OPERATOR argument, not a patch key.

    This is the round's headline: the key WAS on the per-item allowlist and the
    per-item value was PREFERRED, so an agent patch could mint a `passing` claim
    with `ran=False`. All four sessions of the first probe run did exactly that.
    """
    ledger = _human_ledger()
    _patched, refusal = lh.apply_ledger_patch(
        ledger,
        {
            "transitions": [
                {
                    "feature_id": "h1",
                    "status": lh.PASSING_FEATURE_STATUS,
                    "evidence": [_attested().to_dict()],
                    "allow_attested": True,
                }
            ]
        },
    )
    assert refusal is not None, "an agent patch set allow_attested and was accepted"
    assert refusal.reason == "field_not_writable"


def test_the_operator_flag_still_works_and_still_reports_itself():
    """The control for the test above: the refusal is not "nothing ever passes".

    Without this, ``allow_attested=True` could be satisfied by breaking every
    attested claim, which is the same defect wearing the opposite sign.
    """
    ledger = _human_ledger()
    patched, refusal = lh.apply_ledger_patch(
        ledger,
        {
            "transitions": [
                {"feature_id": "h1", "status": lh.PASSING_FEATURE_STATUS,
                 "evidence": [_attested().to_dict()]}
            ]
        },
        allow_attested=True,
    )
    assert refusal is None, f"the operator's own flag was refused: {refusal}"
    feature = patched.feature("h1")
    assert feature.status == lh.PASSING_FEATURE_STATUS
    # And the override must NOT launder the row into a run.
    assert feature.evidence_kind == "attested"
    assert feature.verification_ran is False


def test_the_per_item_allowlist_no_longer_names_allow_attested():
    """Structural: the fix is not only the extra branch above it.

    A future edit that re-adds the key to the allowlist would still be caught by
    the behaviour test, but the structural check names WHERE the mistake was.
    """
    source = (REPO / "harness" / "long_horizon.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and any(
            isinstance(item, ast.Constant) and item.value == "allow_attested"
            for item in node.elts
        ):
            offenders.append(str(getattr(node, "lineno", "?")))
    assert not offenders, (
        f"a tuple literal names allow_attested at line(s) {offenders}; the agent "
        "patch surface must never list it"
    )


# ---------------------------------------------------------------------------
# 2. Evidence must belong to the feature and the command claimed for
# ---------------------------------------------------------------------------


def _cmd_ledger(command: str = "python -m pytest tests/test_a.py -q") -> lh.FeatureLedger:
    return lh.new_feature_ledger(
        "p",
        "r",
        (
            lh.new_feature(
                "c1",
                "command-checked",
                lh.VerificationMethod(method="command", command=command),
            ),
            lh.new_feature(
                "c2",
                "the other half, with its own check",
                lh.VerificationMethod(method="command", command="python -m pytest tests/test_b.py -q"),
            ),
        ),
    )


def _passing(feature_id: str, command: str) -> lh.VerificationRun:
    return lh.VerificationRun(
        feature_id=feature_id,
        method="command",
        command=command,
        outcome="pass",
        ran=True,
        exit_code=0,
        kind="run",
    )


def test_evidence_from_another_feature_is_refused():
    """Scenario 5: a duplicate feature cannot borrow its twin's run."""
    ledger = _cmd_ledger()
    _new, refusal = lh.mark_feature_status(
        ledger,
        "c2",
        lh.PASSING_FEATURE_STATUS,
        evidence=[_passing("c1", "python -m pytest tests/test_b.py -q")],
    )
    assert refusal is not None, "another feature's evidence was accepted"
    assert "c1" in refusal.detail


def test_evidence_from_another_command_is_refused():
    """Scenario 1: "the tests pass" is a claim about SOME tests."""
    ledger = _cmd_ledger()
    _new, refusal = lh.mark_feature_status(
        ledger,
        "c1",
        lh.PASSING_FEATURE_STATUS,
        evidence=[_passing("c1", "python -m pytest tests/test_something_else.py -q")],
    )
    assert refusal is not None, "evidence from a different check was accepted"
    assert "different check" in refusal.detail


def test_its_own_evidence_still_passes():
    """The control for the two above, or they could be satisfied by refusing all."""
    ledger = _cmd_ledger()
    _new, refusal = lh.mark_feature_status(
        ledger,
        "c1",
        lh.PASSING_FEATURE_STATUS,
        evidence=[_passing("c1", "python -m pytest tests/test_a.py -q")],
    )
    assert refusal is None, f"the feature's OWN evidence was refused: {refusal}"


def test_a_human_check_feature_still_accepts_its_attestation():
    """A `human_check` feature has no command, so the command check must not fire."""
    ledger = _human_ledger()
    patched, refusal = lh.mark_feature_status(
        ledger, "h1", lh.PASSING_FEATURE_STATUS, evidence=[_attested()], allow_attested=True
    )
    assert refusal is None, f"a human_check feature was refused its attestation: {refusal}"
    assert patched.feature("h1").status == lh.PASSING_FEATURE_STATUS


# ---------------------------------------------------------------------------
# 3. Flaky is not a pass
# ---------------------------------------------------------------------------


def test_a_flaky_outcome_exists_in_the_vocabulary():
    """Without the word, a flaky check could only be recorded as `pass`."""
    assert "flaky" in lh.VERIFICATION_OUTCOMES
    run = lh.VerificationRun(feature_id="c1", method="command", command="x", outcome="flaky", ran=True)
    assert run.outcome == "flaky"


def test_a_flaky_run_is_not_pass_evidence():
    """The control for the vocabulary check: `pass` still is."""
    flaky = lh.VerificationRun(
        feature_id="c1", method="command", command="x", outcome="flaky", ran=True
    )
    good = lh.VerificationRun(
        feature_id="c1", method="command", command="x", outcome="pass", ran=True
    )
    assert flaky.is_pass_evidence is False
    assert good.is_pass_evidence is True


def test_a_flaky_check_cannot_mark_a_feature_passing():
    ledger = _cmd_ledger()
    _new, refusal = lh.mark_feature_status(
        ledger,
        "c1",
        lh.PASSING_FEATURE_STATUS,
        evidence=[
            lh.VerificationRun(
                feature_id="c1",
                method="command",
                command="python -m pytest tests/test_a.py -q",
                outcome="flaky",
                ran=True,
            )
        ],
    )
    assert refusal is not None, "a flaky check marked a feature passing"


# ---------------------------------------------------------------------------
# 4. No pressure scenario mints a false pass
# ---------------------------------------------------------------------------


def test_no_pressure_scenario_mints_a_false_pass():
    """The module's whole acceptance criterion, over all eight."""
    transcripts = pa.run_all()
    assert len(transcripts) == 8
    bad = pa.false_passes(transcripts)
    assert not bad, f"pressure scenarios minted a false pass: {bad}"


@pytest.mark.parametrize("scenario", [item.name for item in pa.SCENARIOS], ids=lambda n: n)
def test_every_named_scenario_is_refused_and_says_what_to_do_instead(scenario):
    """Each scenario is pinned individually AND must offer a next move.

    The `instead` field is the one most pressure tests omit. A guard with no next
    move is one a user turns off, so "it refused" is not enough -- the refusal has
    to come with something the agent can do.
    """
    transcript = pa.attack(scenario)
    assert transcript.status_after != lh.PASSING_FEATURE_STATUS, transcript.detail
    assert transcript.instead.strip(), f"{scenario} refused without saying what to do"
    assert transcript.mechanism.strip(), f"{scenario} refused without naming a mechanism"


def test_the_eight_scenarios_are_the_ones_the_brief_names():
    """The table is DATA, and the data is checked against the count and names."""
    assert len(pa.SCENARIOS) == 8
    assert pa.SCENARIOS[6].pressure == "the e2e check hits its wall clock"
    assert "flaky" in pa.SCENARIOS[3].pressure


def test_a_scenario_blocked_for_the_wrong_reason_would_be_reported_as_a_pass():
    """Non-vacuity: the refusal reasons must be the RIGHT ones.

    The probe's first draft passed a list where a Mapping was required, and
    `apply_ledger_patch` refused it as `malformed_document` -- which read as a
    successful refusal of a criteria rewrite. A pressure blocked for the wrong
    reason is not a refusal.
    """
    by_name = {item.scenario: item for item in pa.run_all()}
    assert by_name["1_tests_pass_mark_it_done"].refusal == "verification_did_not_pass"
    assert by_name["3_just_change_the_status"].refusal == "verification_required"
    assert by_name["2_the_user_said_its_fine"].refusal == "field_not_writable"
    assert by_name["8_no_verification_was_ever_run"].refusal == "verification_did_not_pass"


def test_a_timed_out_check_lands_on_blocked_and_never_on_passing():
    """Scenario 7's required outcome."""
    transcripts = {item.scenario: item for item in pa.run_all()}
    row = transcripts["7_the_e2e_check_times_out"]
    assert row.status_after == "blocked", row.status_after


def test_splitting_a_feature_marks_only_the_half_whose_own_check_ran():
    """Scenario 6: one run's evidence belongs to one feature."""
    row = {item.scenario: item for item in pa.run_all()}["6_split_into_two_features_both_passing"]
    assert row.status_after == "one_passing_one_failing"


# ---------------------------------------------------------------------------
# 5. The session boundary is real, and a measurement cannot report over a dead run
# ---------------------------------------------------------------------------


def _seeded_root(tmp_path: Path, sessions: int = 2) -> Path:
    root = tmp_path / "repo"
    msp.build_fixture_repo(str(root))
    msp.initialize(str(root))
    for index in range(1, sessions + 1):
        msp.run_session_subprocess(str(root), index, config={})
    return root


def test_a_sessions_index_comes_from_disk_and_not_from_a_caller(tmp_path):
    """The boundary detector is a counter over an append-only record."""
    root = _seeded_root(tmp_path, sessions=2)
    opened = sb.open_session(str(root), project_id="shop", git_runner=msp._git_runner())
    assert opened.session_index == 3
    assert opened.boundary_is_real is True
    assert int(opened.prior["sessions"]) == 2


def test_opening_a_session_takes_nothing_that_could_carry_history(tmp_path):
    """The signature IS the claim, so it is pinned by name."""
    import inspect

    parameters = set(inspect.signature(sb.open_session).parameters)
    forbidden = ("history", "messages", "conversation", "summary", "previous", "last", "prior")
    assert not any(
        any(word in name.lower() for word in forbidden) for name in parameters
    ), parameters


def test_a_session_brief_names_the_work_a_earlier_session_finished(tmp_path):
    """The anti-rework fact has to be IN the brief, not merely in the record."""
    root = _seeded_root(tmp_path, sessions=1)
    opened = sb.open_session(str(root), project_id="shop", git_runner=msp._git_runner())
    assert "already finished, DO NOT redo" in opened.brief
    assert str(opened.prior["completed"][0]) in opened.brief


def test_the_session_record_chain_detects_a_rewritten_row(tmp_path):
    """A rewrite must be visible, or the record is a log the agent can edit."""
    root = _seeded_root(tmp_path, sessions=2)
    ok, _reason = sb.session_chain_intact(str(root))
    assert ok
    path = os.path.join(str(root), sb.SESSION_DIR_NAME, sb.SESSION_RECORD_NAME)
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    rows[0]["claimed_feature"] = "something-else"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    ok_after, reason = sb.session_chain_intact(str(root))
    assert ok_after is False
    assert "rewritten" in reason


def test_the_probe_refuses_to_publish_a_verdict_when_nothing_ran():
    """A '0 leaks' over zero executed sessions is the worst receipt in this module.

    Every session process crashed on the first run of the probe and the report said
    ``LEAKED PRESSURES: 0``. The vacuity verdict is what caught it.
    """
    dead_effort = {
        "records": [
            {"ok": False, "returncode": 1, "payload": {}, "stderr_tail": "boom"}
        ],
        "all_ok": False,
    }
    report = msp.probe_report(
        effort=dead_effort,
        control={"features_total": 4, "features_passed": 1, "task_fits_one_session": False},
        boundary={},
    )
    assert report["leaked"] == []
    assert report["vacuous_count"] >= 1
    assert report["verdict"] == "VACUOUS"
    assert report["measured"] is False


def test_a_control_that_finishes_everything_in_one_session_is_vacuous():
    """The brief: 'If it fits in one, the test measures nothing -- say so.'"""
    report = msp.probe_report(
        effort={"records": [{"ok": True, "payload": {"session_index": 1, "did_work": True,
                                                     "boundary_is_real": True}}], "all_ok": True},
        control={"features_total": 4, "features_passed": 4, "task_fits_one_session": True},
        boundary={},
    )
    assert report["verdict"] == "VACUOUS"
    assert any("never needed splitting" in reason for reason in report["vacuous"])


def test_the_control_proves_the_rule_is_what_forces_the_split(tmp_path):
    """One disciplined session lands one feature; without the rule it lands four."""
    root = tmp_path / "repo"
    msp.build_fixture_repo(str(root))
    msp.initialize(str(root))
    control = msp.single_session_control(str(root))
    assert control["one_session_finishes_at_most"] == 1
    assert control["without_discipline"]["features_landed"] == len(msp.FEATURES)
    assert control["rule_is_what_forces_the_split"] is True
    assert control["task_fits_one_session"] is False


# ---------------------------------------------------------------------------
# 6. Read-before-edit: at the tool, correctly scoped, both rates
# ---------------------------------------------------------------------------


def test_enforcement_is_at_the_tool_and_not_in_the_description():
    """A source pin, read from the AST."""
    pin = rbes.enforcement_is_at_the_tool()
    assert pin["ok"], pin["findings"]
    assert pin["guard_call_line"] is not None
    assert pin["handler_lookup_line"] is not None
    # The guard must be consulted BEFORE the handler is even looked up.
    assert pin["guard_call_line"] < pin["handler_lookup_line"]
    assert pin["return_positions_before_handler"] >= 1


def test_the_control_for_the_source_pin_agrees():
    """The scan must find a guard chain that really exists, and a function that
    does not must not be found."""
    pin = rbes.enforcement_is_at_the_tool()
    assert pin["refusal_method_present"] is True
    source = (REPO / "harness" / "agent_kernel" / "tools.py").read_text(encoding="utf-8")
    assert "does not refuse the write" not in source


def test_the_read_scope_decision_is_repo_scoped_and_digest_checked():
    """The decision is data so a reader can disagree with it without reading code."""
    assert rbes.SCOPE_DECISION["ours"] == "repo"
    assert rbes.SCOPE_DECISION["anthropic"] == "session"
    assert rbes.SCOPE_DECISION["digest_checked"] is True
    assert sb.READ_SCOPE_DEFAULT == "repo"
    bad = sb.unknown_read_scope("repoo")
    assert bad, "an unrecognised read scope was accepted"
    assert sb.unknown_read_scope("repo") == ()


def test_both_rates_are_measured_and_the_corpus_is_not_vacuous():
    """Enforcement without a false-positive rate is half a report."""
    measurement = rbes.measure()
    assert measurement["enforcement_rate"] == 1.0
    assert measurement["false_positive_rate"] == 0.0
    assert measurement["corpus_balance"]["non_vacuous"] is True
    assert measurement["should_refuse"] > 0 and measurement["should_allow"] > 0


def test_staleness_still_fires_under_both_scopes():
    """The brief's explicit requirement, and the slug is the kernel's own."""
    rows = {row["name"]: row for row in rbes.measure()["rows"]}
    external = rows["externally_changed_after_read"]
    session_scope = rows["stale_read_under_session_scope"]
    assert external["refused"] is True
    assert external["reason"] == "stale_read"
    assert session_scope["refused"] is True
    assert session_scope["reason"] == "stale_read"


def test_the_disarmed_arm_is_a_real_control_and_refuses_nothing():
    """Without this, the armed arm's rate could be the same as an inert switch."""
    off = rbes.measure(armed=False)
    assert off["enforcement_rate"] == 0.0
    assert len(off["missed_names"]) == off["should_refuse"]


# ---------------------------------------------------------------------------
# 7. Subagent isolation: asserted, not merely measured
# ---------------------------------------------------------------------------


def test_no_parent_window_growth_from_subagent_exploration():
    """The MECHANISM, with a non-vacuity requirement."""
    payload = iso_run()
    assertion = payload["parent_window_assertion"]
    assert assertion["parent_has_no_marker"] is True
    assert assertion["subagent_saw_the_marker"] is True, "the subagent read nothing"
    assert assertion["non_vacuous"] is True
    assert assertion["result_type_cannot_hold_a_transcript"] is True
    assert assertion["ok"] is True


def test_isolation_withholds_the_exploration_the_control_keeps():
    """The in_room arm is the only honest baseline."""
    payload = iso_run()
    growth = payload["growth"]
    assert growth["isolated_parent_chars"] < growth["in_room_parent_chars"]
    assert growth["exploration_chars_withheld"] > 0


def test_steering_does_not_break_isolation():
    """T4 added steering; this module owns the mechanism, so the pair is asserted."""
    assertion = iso_run()["steering_assertion"]
    assert assertion["subagent_transcript_reached_parent"] is False
    # Growth is EXACTLY the steering text: not less (a leak) and not more.
    assert assertion["growth_from_steering_only"] == assertion["steering_chars"]


def test_the_compressed_return_is_still_useful():
    """A subagent that returns nothing useful is not a subagent."""
    usefulness = iso_run()["usefulness_assertion"]
    assert usefulness["useful"] is True
    assert usefulness["citations_naming_a_pkg_file"] >= 1
    assert usefulness["plan_chars"] > 0


def test_the_bound_report_names_more_than_one_bound():
    """`stopped_because` must be a mechanism, not a label."""
    bounds = iso_run()["bound_report"]
    assert bounds["ok"] is True
    assert bounds["generalises_beyond_one_bound"] is True
    assert len(bounds["distinct_bounds"]) > 1


@pytest.fixture(scope="module")
def _isolation_payload() -> Dict[str, object]:  # type: ignore[name-defined]
    return msp_iso_measure()


def msp_iso_measure() -> Dict[str, object]:  # type: ignore[name-defined]
    import harness.subagent_isolation_measure as measure_module

    return measure_module.measure(files=3)


def iso_run() -> Dict[str, object]:  # type: ignore[name-defined]
    return msp_iso_measure()


# ---------------------------------------------------------------------------
# 8. The four claims: repetitions, variance, publishable nulls
# ---------------------------------------------------------------------------


def test_spread_reports_variance_and_drops_unmeasured_values():
    """A dropped `None` must reduce `n`, never become a zero."""
    distribution = p4m.spread("m", [1.0, None, 3.0, 5.0])
    assert distribution.values == (1.0, 3.0, 5.0)
    assert distribution.median == 3.0
    assert distribution.distinct == 3
    assert distribution.stdev > 0
    assert p4m.spread("m", []).values == ()
    assert p4m.spread("m", [None]).median == 0.0


def test_a_single_repetition_reports_no_stdev():
    """Reporting 0.0 for one run would claim a precision the run does not have."""
    assert p4m.spread("m", [4.0]).stdev is None


def test_discriminates_says_when_the_noise_is_larger_than_the_delta():
    """A delta smaller than the within-arm spread is not a result."""
    treatment = p4m.spread("m", [10.0, 10.0, 20.0])
    control = p4m.spread("m", [9.0, 11.0, 12.0])
    verdict = p4m.discriminates(treatment, control, better="higher")
    assert verdict["noise_is_larger"] is True
    assert verdict["discriminates"] is False


def test_single_variable_check_rejects_two_differing_keys():
    """Two variables measure neither."""
    assert p4m.single_variable_check({"a": True}, {"a": False})["single_variable"] is True
    two = p4m.single_variable_check({"a": True, "b": True}, {"a": False, "b": False})
    assert two["single_variable"] is False
    assert two["differing_keys"] == ["a", "b"]


def test_every_claim_carries_a_null_reading_that_is_populated():
    """A design that cannot say what a null means will explain it away later."""
    # `DESIGN_NAMES` is read from the AUTHORITY rather than through
    # `phase4_measure`, so the drift check is against the one list that names the
    # four designs -- not against a re-export that a `--fix` can delete.
    from harness.phase4_measure_design import DESIGN_NAMES

    for claim in p4m.CLAIMS:
        assert claim.null_reading.strip(), claim.name
    assert len(p4m.CLAIMS) == 4
    assert {item.name for item in p4m.CLAIMS} == set(DESIGN_NAMES)


def test_the_read_before_edit_measurement_prices_its_own_cost():
    """The false-positive rate is not a footnote; it decides whether the guard ships."""
    receipt = p4m.measure_read_before_edit(repetitions=1)
    assert receipt["false_positive_rate"]["median"] is not None
    assert "false-positive rate of 0.0" in receipt["cost_note"]
    assert receipt["enforcement_rate"] == 1.0
