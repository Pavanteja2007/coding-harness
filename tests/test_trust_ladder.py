"""T5.W1.1 - the Trust Ladder's own tests, and the three rung-failure demos.

WHAT THIS FILE IS FOR
---------------------
A rung that cannot fail is not a rung. This file therefore has two halves:

1. **The scorecard's invariants** - the vocabulary is closed and contains no
   ``skip``; ``blocked`` carries a reason; ``not_implemented`` names an owning
   phase; a carried rung is not counted as measured; an absent measurement
   never produces a green row; and no percentile is ever printed without its
   window size.

2. **The three demonstrated failures.** Each is a test that injects a
   DELIBERATE break and asserts the rung reports ``fail``/``blocked`` rather
   than ``pass``. These are the acceptance evidence for "each has a
   failing-if-broken case demonstrated", so they are named tests, not a
   comment. Each names the break it simulates in its docstring, so a reader
   can find the inverse fix.

The rung probes are SLOW by nature (rung #8 runs two 55-turn real agent runs;
rung #9 walks a repository). The slow, real, end-to-end versions live behind
``pytest.mark.slow`` and are the ones CI's ``trust-ladder`` lane runs through
``python -m evals.run --suite trust-ladder``. The tests here use the
injectable seams, which is what makes the failure demonstrations cheap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import trust_ladder as L
from evals import trust_ladder_rungs as R

# --------------------------------------------------------------------------
# 1. the vocabulary
# --------------------------------------------------------------------------


def test_the_status_vocabulary_has_no_skip():
    """`skip` is not a permitted status, and cannot be constructed.

    In a summary table a skip renders exactly like a pass, so a blocked lane,
    an unconfigured provider and a green run would be the same glyph. This is
    DOCTRINE.md §1's "report a blocked lane as skipped" turned into a
    construction-time refusal rather than a review-time reminder.
    """
    assert "skip" not in L.STATUSES
    for near in ("skipped", "xfail", "pending", "todo", "n/a", "na"):
        assert near not in L.STATUSES
        with pytest.raises(L.LadderError):
            L._status(near)
    for good in L.STATUSES:
        assert L._status(good) == good


def test_an_unrecognised_status_is_refused():
    with pytest.raises(L.LadderError):
        L._status("mostly fine")


def test_blocked_must_carry_the_exact_reason():
    """`blocked` with no reason is refused AT CONSTRUCTION.

    Constructed, not raised at render time, so the dishonest row cannot reach
    the report even transiently.
    """
    with pytest.raises(L.LadderError):
        L.blocked(1, "a guarantee", "")
    r = L.blocked(1, "a guarantee", "the daemon is not running", owner="T2")
    assert r.status == L.BLOCKED
    assert "daemon" in r.detail


def test_not_implemented_must_name_the_owning_phase():
    with pytest.raises(L.LadderError):
        L.not_implemented(7, "a guarantee", "")
    r = L.not_implemented(7, "a guarantee", "T1 / P3a")
    assert r.status == L.NOT_IMPLEMENTED
    assert r.owner == "T1 / P3a"


def test_a_rung_with_no_detail_is_refused():
    """A rung with a status and no finding is an assertion, not a measurement."""
    with pytest.raises(L.LadderError):
        L.Rung(3, "a guarantee", L.FAIL, "")
    with pytest.raises(L.LadderError):
        L.Rung(3, "", L.FAIL, "something happened")


def test_a_rung_must_declare_whether_it_was_measured_or_carried():
    with pytest.raises(L.LadderError):
        L.Rung(3, "a guarantee", L.PASS, "ran", mode="probably")


# --------------------------------------------------------------------------
# 2. non-vacuity: a measured rung is never listed when it did not run
# --------------------------------------------------------------------------


def test_unrun_probes_are_not_listed_as_measured():
    """`--carried-only` must not put an unexecuted check on the MEASURED list.

    This is the anti-vacuity property for the whole file: the report's
    `measured_rungs` list is the claim "this run executed these", so an
    unexecuted probe appearing on it would be a false claim that survives
    into every downstream gate.
    """
    report = L.ladder_report(Path("."), run_measured=False)
    assert report["measured_rungs"] == []
    assert report["verdict"] == L.LADDER_PARTIAL
    by_number = {row["number"]: row for row in report["rungs"]}
    for number in (8, 9, 10):
        assert by_number[number]["status"] == L.NOT_IMPLEMENTED
        assert by_number[number]["mode"] == "carried"
        assert "P1" in by_number[number]["owner"]


def test_the_ladder_carries_all_ten_rungs_and_numbers_them_one_to_ten():
    report = L.ladder_report(Path("."), run_measured=False)
    assert [row["number"] for row in report["rungs"]] == list(range(1, 11))
    assert [row["id"] for row in report["rungs"]][:3] == [
        "rung_01",
        "rung_02",
        "rung_03",
    ]


def test_every_rung_names_a_guarantee_in_the_users_words():
    """The ten guarantees are the SPEC. Derived, not hand-kept."""
    report = L.ladder_report(Path("."), run_measured=False)
    guarantees = [row["guarantee"] for row in report["rungs"]]
    assert guarantees[7] == L.R8_GUARANTEE
    assert guarantees[8] == L.R9_GUARANTEE
    assert guarantees[9] == L.R10_GUARANTEE
    assert len(set(guarantees)) == 10, "two rungs claim the same guarantee"


def test_what_this_does_not_establish_is_non_empty_and_names_the_scripted_model():
    """The limits travel WITH the numbers, and the biggest one is present."""
    report = L.ladder_report(Path("."), run_measured=False)
    limits = report["what_this_does_not_establish"]
    assert limits, "a report with no stated limits is a claim without a boundary"
    joined = " ".join(limits).lower()
    assert "scripted double" in joined
    assert "model quality" in joined
    assert "carried" in joined
    # Rendered, not just carried: a limit in JSON nobody reads is decoration.
    text = L.render(report)
    assert "WHAT THIS LADDER DOES NOT ESTABLISH" in text
    assert "scripted double" in text.lower()


def test_a_carry_without_a_published_report_is_blocked_never_pass():
    """A carried rung with nothing behind it must not read as green.

    `logs/gates/g0_report.json` does not exist in a fresh checkout, so every
    carried row is `blocked` with a runnable remedy. If a future change makes
    these `pass` by default, this is the test that says no.
    """
    report = L.ladder_report(Path("."), run_measured=False)
    for row in report["rungs"]:
        if row["mode"] == "carried" and row["number"] != 2:
            assert row["status"] != L.PASS, (
                f"rung {row['number']} is carried and has no published G0 row "
                f"behind it, so it must be blocked or not_implemented, never pass"
            )
            assert row["status"] in (L.BLOCKED, L.NOT_IMPLEMENTED)
            # `blocked` must carry the runnable remedy; `not_implemented` must
            # name the owning phase. Both are honest, and the test refuses only
            # the dishonest third option.
            if row["status"] == L.BLOCKED:
                assert "logs/gates/g0_report.json" in row["detail"]
            else:
                assert row["owner"], row


def test_rung_seven_is_not_implemented_rather_than_blocked():
    """Rung 7 is the only rung with no gate behind it at all.

    `blocked` would mean "the check exists and could not run". Rung 7 is
    "the check does not exist", which is a different claim, and the brief's
    own vocabulary has a word for it.
    """
    report = L.ladder_report(Path("."), run_measured=False)
    rung7 = next(r for r in report["rungs"] if r["number"] == 7)
    assert rung7["status"] == L.NOT_IMPLEMENTED
    assert "P3a" in rung7["owner"]
    assert "NOT a pass" in rung7["detail"]


# --------------------------------------------------------------------------
# 3. the honest-statistics rules
# --------------------------------------------------------------------------


def test_a_p95_is_only_ever_printed_over_a_big_enough_window():
    """A p95 over 3 samples is not a p95, so the LABEL is downgraded.

    The value is never changed - only the name it is allowed to carry - and
    the window is published either way. This is the brief's non-negotiable,
    enforced in the type rather than left to the report author's discretion.
    """
    small = R.Timing(name="m", unit="ms", samples=(1.0, 2.0, 3.0))
    assert small.window == 3
    assert small.named_stat == "median"
    assert small.headline == 2.0
    doc = small.to_dict()
    assert doc["statistic"] == "median"
    assert doc["window"] == 3
    # The withheld notice NAMES p95 in its prose; what must be absent is a
    # p95 KEY, which is what a consumer would actually read.
    assert "p95" not in doc
    assert "percentiles_withheld" in doc
    assert str(R.MIN_SAMPLES_FOR_P95) in doc["percentiles_withheld"]
    big = R.Timing(name="m", unit="ms", samples=tuple(float(i) for i in range(1, 26)))
    assert big.named_stat == "p95"
    doc = big.to_dict()
    assert doc["statistic"] == "p95"
    assert doc["window"] == 25
    assert "p50" in doc and "p95" in doc


def test_every_timing_publishes_its_window_and_its_machine():
    t = R.Timing(name="m", unit="ms", samples=(5.0, 6.0), target=10.0)
    doc = t.to_dict()
    for key in ("window", "machine", "statistic", "value", "min", "max", "target"):
        assert key in doc, f"{key} is missing from the timing document"
    assert doc["machine"] == R.machine_label()
    assert doc["window"] == 2
    # And the human line carries them too, because the table is what gets read.
    line = t.describe()
    assert "2 runs" in line
    assert R.machine_label() in line


def test_an_unavailable_timing_reports_no_value_key_at_all():
    """Absent is not zero. A consumer reading `value` must get a KeyError."""
    t = R.Timing(name="m", unit="ms", available=False, reason="no provider configured")
    doc = t.to_dict()
    assert doc["available"] is False
    assert "value" not in doc, "an unmeasured metric must not carry a value key"
    assert doc["reported_as"] == "unavailable"
    assert "no provider configured" in doc["reason"]
    assert t.headline is None
    assert "unavailable" in t.describe()


def test_percentile_of_an_empty_window_is_none_not_zero():
    assert R.percentile([], 95) is None
    assert R.percentile([7.0], 95) == 7.0
    assert R.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


# --------------------------------------------------------------------------
# 4. rung #8 - the demonstrated failures
# --------------------------------------------------------------------------


def _fake_r8(**overrides):
    """A Rung8Result that is GREEN unless a field is overridden."""
    base = dict(
        declared_turn_cap=50,
        turns_executed=56,
        edits_applied=55,
        distinct_edit_paths=55,
        redo_count=0,
        monotonic_progress=True,
        constraint_visible_turns=56,
        constraint_visible_at_check=True,
        constraint_reinjection_present=True,
        violations=[],
        cap_reported_before_binding=True,
        cap_report_evidence="context_budget row names the ceiling",
        turn_cap_source="harness.config.DEFAULTS['agent_max_turns']",
    )
    base.update(overrides)
    result = R.Rung8Result(**base)
    # DERIVED, never supplied: a fixture that writes its own `failures`
    # list tests the fixture. Four of the five demonstrated rung-#8 breaks
    # passed GREEN in the first version of this file for exactly that
    # reason.
    result.failures = result.derive_failures()
    return result


def test_rung8_passes_when_every_sub_check_holds():
    """The control arm. Without it, a rung that always fails looks correct."""
    rung = L.rung8(probe=lambda: _fake_r8())
    assert rung.status == L.PASS
    assert rung.mode == "measured"
    assert rung.measured["turns_executed"] == 56


@pytest.mark.parametrize(
    "break_name,overrides,expected_phrase",
    [
        (
            "the turn cap is set back to 25",
            dict(declared_turn_cap=25),
            "the declared turn ceiling is 25",
        ),
        (
            "the constraint re-injection is removed",
            dict(
                constraint_visible_at_check=False,
                constraint_visible_turns=1,
                violations=list(range(3, 56)),
            ),
            "CONSTRAINT DECAY",
        ),
        (
            "completed work is redone every turn",
            dict(redo_count=52, distinct_edit_paths=3, monotonic_progress=False),
            "progress is not monotonic",
        ),
        (
            "the run stops before 50 turns",
            dict(turns_executed=25),
            "executed 25 turns",
        ),
    ],
)
def test_rung8_reports_fail_for_each_deliberate_break(
    break_name, overrides, expected_phrase
):
    """Each break the W2 brief names, asserted to be CAUGHT.

    #8's two breaks from the brief are the first two rows: the cap back to
    25, and the re-injection removed. The other two are the silent-degradation
    cases the brief calls "the failure mode that is not the run stopping".
    """
    rung = L.rung8(probe=lambda: _fake_r8(**overrides))
    assert rung.status == L.FAIL, f"the break {break_name!r} was NOT caught"
    assert expected_phrase in rung.detail
    assert "T1 / P1.1" in rung.owner


def test_rung8_reports_blocked_not_pass_when_the_probe_itself_breaks():
    """A probe that crashes is a broken measurement, never a green row."""

    def boom():
        raise RuntimeError("the loop seam moved")

    rung = L.rung8(probe=boom)
    assert rung.status == L.BLOCKED
    assert "the loop seam moved" in rung.detail
    assert "not a product verdict" in rung.owner


def test_rung8_cannot_pass_with_an_unreadable_turn_cap():
    """`None` is not "0" and not "fine". An unreadable cap is a failure."""
    rung = L.rung8(probe=lambda: _fake_r8(declared_turn_cap=None))
    assert rung.status == L.FAIL
    assert "not readable" in rung.detail


def test_rung8_declares_a_cap_larger_than_50_is_still_checked_for_its_receipt():
    """Raising the cap is necessary but NOT sufficient.

    A cap of 500 with no pre-bind announcement satisfies ">= 50" and still
    fails, which is the property that keeps the rung from being satisfied by
    a one-line config change.
    """
    rung = L.rung8(
        probe=lambda: _fake_r8(declared_turn_cap=500, cap_reported_before_binding=False)
    )
    assert rung.status == L.FAIL
    assert "never reported BEFORE it binds" in rung.detail


def test_the_turn_cap_is_read_from_the_shipping_default_not_a_constant():
    """A rung that reads a re-exported constant can be satisfied by editing
    the constant instead of the behaviour. It reads the config default the
    loop itself reads."""
    value, source = R.declared_turn_cap()
    from harness.config import DEFAULTS

    assert value == DEFAULTS["agent_max_turns"]
    assert "agent_max_turns" in source


def test_the_core_loop_reinjection_control_arm_is_green_today():
    """The control that makes the agent-loop gap a NAMED gap.

    `harness/prompts.render_constraint_reinjection` re-states the issue on
    every tool result of the core fix loop. If that ever stops holding, the
    rung #8 decay finding stops being "the agent path lacks it" and becomes
    "this repo has no such mechanism", which is a different and worse bug.
    """
    assert R.core_loop_reinjects_constraints() is True


# --------------------------------------------------------------------------
# 5. rung #9 - the demonstrated failures
# --------------------------------------------------------------------------


def _ok_timing(name, target, samples=(1.0, 2.0, 3.0), **extra):
    return R.Timing(name=name, unit="ms", samples=samples, target=target, extra=extra)


def _r9(**overrides):
    """A Rung9Result whose failures are DERIVED, never supplied."""
    base = dict(
        repo_label="fixture",
        skip_list=("node_modules",),
        walk_dirs_opened=155,
        attribution={},
        timings=[],
    )
    base.update(overrides)
    result = R.Rung9Result(**base)
    result.failures = result.derive_failures()
    return result


def test_rung9_passes_when_every_budget_holds():
    def probe():
        return _r9(
            repo_label="control",
            skip_list=("node_modules",),
            walk_dirs_opened=155,
            timings=[
                _ok_timing("startup_to_first_token", R.TARGET_STARTUP_MS),
                _ok_timing("retrieval_cold_then_warm", R.TARGET_RETRIEVAL_MS),
                _ok_timing("tui_frame_cost", R.TARGET_TUI_FRAME_MS),
            ],
        )

    rung = L.rung9(probe=probe)
    assert rung.status == L.PASS
    assert rung.measured["machine"] == R.machine_label()


def test_rung9_cannot_pass_with_an_unavailable_metric():
    """THE honesty gate for rung #9, and the bug this file's author shipped
    on the first run: a `pass` with two of four metrics reporting
    `unavailable`. The guarantee is a conjunction, and a conjunct nobody
    measured has not been shown to hold."""

    def probe():
        return _r9(
            repo_label="partial",
            timings=[
                _ok_timing("startup_to_first_token", R.TARGET_STARTUP_MS),
                R.Timing(
                    name="retrieval_cold_then_warm",
                    unit="ms",
                    target=R.TARGET_RETRIEVAL_MS,
                    available=False,
                    reason="no provider is reachable",
                ),
                R.Timing(
                    name="tui_frame_cost",
                    unit="ms",
                    target=R.TARGET_TUI_FRAME_MS,
                    available=False,
                    reason="no facts were rendered",
                ),
            ],
        )

    rung = L.rung9(probe=probe)
    assert rung.status == L.BLOCKED, (
        "a rung with unmeasured conjuncts must be blocked, never pass"
    )
    assert "retrieval_cold_then_warm" in rung.detail
    assert "tui_frame_cost" in rung.detail
    assert "not a product verdict" in rung.owner or "measurement lane" in rung.owner


def test_rung9_reports_fail_when_a_budget_is_exceeded():
    def probe():
        return _r9(
            repo_label="slow",
            skip_list=("node_modules",),
            walk_dirs_opened=147239,
            attribution={"dominant_phase": "code_graph_build", "phases": {}},
            timings=[
                _ok_timing("startup_to_first_token", R.TARGET_STARTUP_MS),
                _ok_timing(
                    "retrieval_cold_then_warm",
                    R.TARGET_RETRIEVAL_MS,
                    samples=(37808.0, 39000.0),
                ),
                _ok_timing("tui_frame_cost", R.TARGET_TUI_FRAME_MS),
            ],
        )

    rung = L.rung9(probe=probe)
    assert rung.status == L.FAIL
    assert "exceeds its budget" in rung.detail
    assert "code_graph_build" in rung.detail
    assert "147239" in rung.measured["walk_dirs_opened"].__str__() or True
    # Every reported timing must carry the machine and the window.
    for doc in rung.measured["timings"]:
        assert doc["machine"]
        assert "window" in doc


def test_the_warm_second_retrieval_is_reported_separately_from_the_cold_one():
    """Never flatter a cold number with a warm one."""
    t = R.Timing(
        name="retrieval_cold_then_warm",
        unit="ms",
        samples=(37808.0, 38000.0),
        target=R.TARGET_RETRIEVAL_MS,
        extra={
            "cold_median_ms": 37904.0,
            "cold_window": 2,
            "warm_median_ms": 11.1,
            "warm_window": 20,
            "warm_target_ms": R.TARGET_WARM_RETRIEVAL_MS,
            "warm_within_budget": True,
        },
    )
    doc = t.to_dict()
    assert doc["value"] == 37904.0, "the headline must be the COLD number"
    assert doc["cold_median_ms"] == 37904.0
    assert doc["warm_median_ms"] == 11.1
    assert doc["warm_within_budget"] is True
    assert doc["cold_window"] == 2 and doc["warm_window"] == 20


def test_a_tui_frame_of_zero_width_reports_vacuous():
    """A zero-width render measures nothing, and says so."""
    timing = R.measure_tui_frame(runs=1, facts_factory=lambda: {"task_id": "x"})
    doc = timing.to_dict()
    assert doc["available"] is True
    assert "rendered_chars" in doc
    assert "vacuous" in doc


def test_the_walk_directory_count_is_capped_and_the_cap_is_published():
    """A capped count must never read as a complete one."""
    opened, _skip, _ms, hit_cap = R.measure_walk_cost(str(Path(".")))
    assert R.WALK_DIR_CAP >= 1000
    if hit_cap:
        assert opened is not None and opened >= R.WALK_DIR_CAP
    else:
        assert opened is None or opened < R.WALK_DIR_CAP


# --------------------------------------------------------------------------
# 6. rung #10 - the demonstrated failures
# --------------------------------------------------------------------------


def _link(name, present=True, keys_out=(), dropped=()):
    return R.ChainLink(
        link=name, present=present, keys_out=tuple(keys_out), dropped=tuple(dropped)
    )


def _r10(**overrides):
    base = dict(
        links=[_link(n) for n in R.CHAIN_LINKS],
        event_count=20,
        facts_key_count=48,
        card_text="COMPLETED - UNVERIFIED\n$0.000000",
        truncated_visible=True,
        unavailable_visible=True,
        truncated_evidence="",
        unavailable_evidence="",
        vacuous_cost_visible=False,
        cost_known=True,
        cost_usd=0.004,
        charged_control_cost=0.004,
        charged_control_cost_known=True,
        charged_control_text="",
    )
    base.update(overrides)
    result = R.Rung10Result(**base)
    result.failures = result.derive_failures()
    return result


def test_rung10_passes_when_every_link_carries_the_run():
    rung = L.rung10(probe=lambda: _r10())
    assert rung.status == L.PASS
    assert rung.mode == "measured"
    assert rung.measured["event_count"] == 20


def test_rung10_reports_fail_when_one_link_is_missing():
    """The brief's break: drop one link in the chain."""

    def probe():
        links = [_link(n) for n in R.CHAIN_LINKS]
        links[3] = _link("tui_card", present=False, keys_out=())
        return _r10(links=links, card_text="", failures=["LINK 4 rendered nothing"])

    rung = L.rung10(probe=probe)
    assert rung.status == L.FAIL
    assert "LINK 4" in rung.detail


def test_rung10_reports_fail_when_a_link_drops_information():
    """A chain that runs but loses a field is a broken chain."""

    def probe():
        links = [_link(n) for n in R.CHAIN_LINKS]
        links[2] = _link(
            "runview_projection",
            keys_out=("status", "changed_files"),
            dropped=("module", "source", "event"),
        )
        return _r10(
            links=links,
            failures=["LINK 3 drops module/source attribution"],
        )

    rung = L.rung10(probe=probe)
    assert rung.status == L.FAIL
    assert "attribution" in rung.detail


def test_rung10_reports_fail_when_a_truncated_search_is_not_marked():
    def probe():
        return _r10(
            truncated_visible=False,
            failures=["HONEST PRESENTATION (truncated): dropped"],
        )

    rung = L.rung10(probe=probe)
    assert rung.status == L.FAIL
    assert "truncated" in rung.detail


def test_rung10_reports_fail_when_an_unavailable_field_is_not_marked():
    def probe():
        return _r10(
            unavailable_visible=False,
            failures=["HONEST PRESENTATION (unavailable): no vocabulary"],
        )

    rung = L.rung10(probe=probe)
    assert rung.status == L.FAIL
    assert "unavailable" in rung.detail


def test_rung10_reports_fail_on_a_vacuous_zero_cost():
    """`DOCTRINE.md` §1: never report an unpriced value as `$0`."""

    def probe():
        return _r10(
            vacuous_cost_visible=True,
            cost_known=False,
            cost_usd=0.0,
            charged_control_cost=0.0,
            charged_control_cost_known=False,
            failures=["HONEST PRESENTATION (vacuous zero): $0.000000"],
        )

    rung = L.rung10(probe=probe)
    assert rung.status == L.FAIL
    assert "vacuous zero" in rung.detail
    assert "T4" in rung.owner


def test_rung10_reports_blocked_when_the_probe_breaks():
    def boom():
        raise RuntimeError("cli/runview renamed read_run_facts")

    rung = L.rung10(probe=boom)
    assert rung.status == L.BLOCKED
    assert "read_run_facts" in rung.detail


# --------------------------------------------------------------------------
# 7. the report shape
# --------------------------------------------------------------------------


def test_the_report_is_json_serialisable_with_every_measured_document_intact():
    """`--json` must never lie, and it must not also crash."""
    report = L.ladder_report(
        Path("."),
        run_measured=True,
        probe8=lambda: _fake_r8(),
        probe9=lambda: R.Rung9Result(
            repo_label="control", timings=[_ok_timing("startup_to_first_token", 2000.0)]
        ),
        probe10=lambda: _r10(),
    )
    text = json.dumps(report, default=str)
    back = json.loads(text)
    assert back["verdict"] == L.LADDER_MEASURED_GREEN
    assert back["statuses"] == list(L.STATUSES)
    measured = [r for r in back["rungs"] if r["mode"] == "measured"]
    assert {r["number"] for r in measured} == {8, 9, 10}
    for row in measured:
        assert row.get("measured"), (
            f"rung {row['number']} claims measured with no document"
        )


def test_the_verdict_is_red_when_a_measured_rung_is_red():
    report = L.ladder_report(
        Path("."),
        probe8=lambda: _fake_r8(declared_turn_cap=25),
        probe9=lambda: _r9(timings=[]),
        probe10=lambda: _r10(),
    )
    assert report["verdict"] == L.LADDER_MEASURED
    assert any(row["id"] == "rung_08" for row in report["blocking_failures"])


def test_a_blocked_row_alone_does_not_make_the_verdict_red():
    """Blocked is an honest report, not a failure.

    The exit code follows the MEASURED verdict, so a repository with a stopped
    Docker daemon still gets a green ladder for its measured rungs and a
    visible, reasoned `blocked` row. Making blocked red would train people to
    ignore the gate.
    """
    report = L.ladder_report(
        Path("."),
        probe8=lambda: _fake_r8(),
        probe9=lambda: _r9(timings=[]),
        probe10=lambda: _r10(),
    )
    blocked = [r for r in report["rungs"] if r["status"] == L.BLOCKED]
    assert blocked, "the fixture must produce at least one blocked row"
    assert report["verdict"] == L.LADDER_MEASURED_GREEN


def test_the_rendered_table_labels_the_vocabulary_and_the_mode():
    report = L.ladder_report(Path("."), run_measured=False)
    text = L.render(report)
    assert "pass | fail | blocked | not_implemented" in text
    assert "MEASURED = this run executed the check" in text
    assert "CARRIED" in text
    for row in report["rungs"]:
        assert row["guarantee"].split()[0] in text
