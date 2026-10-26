"""P4/W2-T2 pins — the ten attacks, the fifth state, and the two gaps they found.

**These pins are host-only.** No Docker, no model, no network, no credential:
they read the new modules' decision logic, drive the attack suite's structural
rows, and run the guard's classifier directly. The five rows that need a real
container live in :mod:`execution.e2e_attack` and
:mod:`execution.multi_session`, whose CLIs are the gates for those; this module
proves the properties those drivers depend on being able to fail.

The design rules every class here follows, and they are the ones this
repository has learned the hard way:

* **non-vacuity before acceptance.** Every class proves its subject can produce
  the answer it is about to assert before asserting that answer.
* **a control arm for every "never does X".** Without one, a refusal-only suite
  is satisfied by a guard that refuses everything.
* **a published reason for every gap.** A row that cannot be measured says so on
  the row; it is never silently counted as a pass.
* **weakening demonstrations are executable, not transcripts.**
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from execution import (
    e2e_attack,
    e2e_evidence,
    e2e_trust,
    e2e_verify,
    multi_session,
    session_economics,
    test_guard,
    test_removal_e2e,
)
from execution.e2e_verify import E2ECheck

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _tmp_repo(label: str = "repo", *, broken: bool = False) -> Path:
    """A real fixture repository, real files, real git history."""
    return e2e_attack.build_fixture(label, broken=broken)


# ==========================================================================
# 0. Non-vacuity — everything below depends on these
# ==========================================================================


class TestThePinsAreNotRubberStamps:
    """The checks this module leans on must be able to produce BOTH answers."""

    def test_the_trust_assessment_refuses_a_real_attack_and_accepts_a_real_check(self):
        repo = _tmp_repo("nonvacuity")
        weak = e2e_trust.assess_check(
            E2ECheck(id="w", command="python e2e_weak.py", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed in 0.31s ===",
            container_name="hexec-e0-p1-w1",
            ran=True,
            duration_s=3.0,
        )
        good = e2e_trust.assess_check(
            E2ECheck(id="g", command="python e2e_checkout.py", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed in 0.31s ===",
            container_name="hexec-e0-p1-w2",
            ran=True,
            duration_s=3.0,
        )
        assert weak.may_report_passed is False, "the weak check must be refused"
        assert good.may_report_passed is True, "the real check must be trusted"

    def test_the_fast_pass_arm_fires_on_a_stub_and_not_on_a_plausible_duration(self):
        check = E2ECheck(id="c", command="python e2e_checkout.py", feature="checkout")
        stub = e2e_trust.assess_fast_pass(
            check, duration_s=0.02, container_name="", ran=True
        )
        real = e2e_trust.assess_fast_pass(
            check, duration_s=3.2, container_name="hexec-e0-p1-w3", ran=True
        )
        assert stub.suspicious is True
        assert real.suspicious is False

    def test_the_commit_reader_resolves_and_also_refuses_to_invent(self):
        repo = _tmp_repo("commits")
        resolved = e2e_trust.read_commit(str(repo))
        assert resolved.known is True
        assert re.fullmatch(r"[0-9a-f]{40}", resolved.commit), resolved.commit
        # A directory with no .git must NOT invent a commit.
        bare = Path(tempfile.mkdtemp(prefix="t2w2-bare-"))
        missing = e2e_trust.read_commit(str(bare))
        assert missing.known is False
        assert missing.commit == "", "an unresolvable commit must never be invented"

    def test_the_command_gate_refuses_and_allows_in_the_same_run(self):
        must_refuse = (
            "rm src/tests/test_checkout.py",
            "chmod 000 src/tests/test_checkout.py",
            "chmod -x src/tests/test_checkout.py",
            "git checkout -- src/tests/",
            "python -m pytest -q --deselect tests/test_a.py::test_a",
            "python -m pytest -q -m 'not slow'",
        )
        must_allow = (
            "chmod +x scripts/run.sh",
            "python -m pytest -q --ignore htmlcov",
            "rm app.py",
            "git status",
        )
        for command in must_refuse:
            verdict = test_guard.command_deletion_verdict(command, origin="agent")
            assert verdict.refused is True, f"{command!r} should be refused, said {verdict.reason}"
        for command in must_allow:
            verdict = test_guard.command_deletion_verdict(command, origin="agent")
            assert verdict.refused is False, (
                f"{command!r} should be ALLOWED; a gate that refuses it gets disabled"
            )

    def test_the_attack_suite_reaches_every_row(self):
        suite = e2e_attack.run_all(docker=False, rows=["1", "2", "3", "5", "7"])
        assert len(suite.outcomes) == 5, "the five structural rows must all run"
        assert not suite.succeeded, [row.row for row in suite.succeeded]

    def test_the_multi_session_axes_are_closed_and_every_axis_can_report_unobserved(self):
        assert len(multi_session.MULTI_SESSION_AXES) == 6
        assert len(set(multi_session.AXIS_VERDICTS)) == 4
        # An UNOBSERVED axis must be constructible for every axis, so a broken
        # lane can never be forced into a "held" shape by the vocabulary.
        for axis in multi_session.MULTI_SESSION_AXES:
            row = multi_session.MultiSessionRow(
                axis=axis, verdict="UNOBSERVED", observed=False
            )
            assert row.held is False

    def test_the_cost_table_reads_real_measured_numbers(self):
        table = session_economics.cost_table()
        assert table, "the cost table must not be empty"
        warm = next(
            row for row in table if row.name == session_economics.COST_UNIT_VERIFICATION_WARM
        )
        assert warm.seconds is not None and warm.seconds > 0, (
            "the warm unit-verification cost must be a real measured number, not "
            "None and not zero"
        )


# ==========================================================================
# 1. W2.1 — the five states, and the untrustworthy state itself
# ==========================================================================


class TestTheFiveStatesAreNeverCollapsed:
    def test_the_vocabulary_is_exactly_five_and_distinct(self):
        states = e2e_trust.assert_five_states_distinct()
        assert states == (
            "passed",
            "failed",
            "blocked",
            "not_run",
            "untrustworthy",
        )
        assert len(set(states)) == 5

    def test_a_duplicated_member_would_be_refused(self):
        """The check is not merely a count. A duplicate must FAIL it.

        Built by monkeypatching a COPY of the vocabulary, because a test that
        only counts the tuple cannot tell the difference between five states and
        four states with one repeated.
        """
        original = e2e_verify.E2E_STATES
        try:
            e2e_verify.E2E_STATES = ("passed", "failed", "blocked", "not_run", "passed")
            with pytest.raises(AssertionError):
                e2e_trust.assert_five_states_distinct()
        finally:
            e2e_verify.E2E_STATES = original

    @pytest.mark.parametrize(
        "state,passed,settled",
        [
            ("passed", True, True),
            ("failed", False, True),
            ("blocked", False, False),
            ("not_run", False, False),
            ("untrustworthy", False, False),
        ],
    )
    def test_each_state_has_its_own_meaning(self, state, passed, settled):
        """Two states may share `passed`, but none shares both it and `settled`."""
        row = e2e_verify.E2EResult(
            check_id="c",
            feature="f",
            state=state,
            reason="because",
            ran=state in ("passed", "failed", "untrustworthy"),
            exit_code=0 if state in ("passed", "untrustworthy") else 1,
            observations=(state,) if state in ("passed", "untrustworthy") else (),
            trust={"reason": "r"} if state == "untrustworthy" else {},
        )
        assert row.passed is passed, state
        assert row.settled is settled, state

    def test_a_row_outside_the_vocabulary_is_refused(self):
        with pytest.raises(ValueError):
            e2e_verify.E2EResult(
                check_id="c", feature="f", state="skipped", reason="r", ran=True
            )

    def test_every_row_of_a_real_report_carries_a_vocabulary_member(self):
        tally = e2e_trust.assert_five_states_distinct()
        assert_states_not_collapsed = e2e_evidence.assert_states_not_collapsed
        counts = assert_states_not_collapsed([])
        assert [name for name, _ in counts] == list(tally)
        assert all(count == 0 for _, count in counts)


class TestTheUntrustworthyRowIsBuiltAndRead:
    def test_the_constructor_requires_a_reason(self):
        check = E2ECheck(id="c", command="true", feature="f")
        with pytest.raises(ValueError):
            e2e_verify.untrustworthy_result(check, {})

    def test_the_row_carries_its_receipt_and_renders_it(self):
        check = E2ECheck(id="c", command="true", feature="checkout")
        row = e2e_verify.untrustworthy_result(
            check,
            {"signals": ("command_is_inert",), "strength": {"strength": "weak"}},
            reason="the command cannot fail",
            ran=True,
        )
        assert row.state == "untrustworthy"
        assert row.untrustworthy is True
        assert row.passed is False
        assert row.settled is False
        rendered = "\n".join(row.render())
        assert "VERDICT VOID" in rendered
        assert "command_is_inert" in rendered

    def test_the_report_verdict_is_DOMINATED_by_an_untrustworthy_row(self):
        """One green row plus one void row must NOT read `passed`.

        This is the collapse the fifth state exists to prevent, and it is the
        most important single assertion in the module: a report that contains a
        green row and a void row and reads `passed` is exactly the false pass
        the whole mechanism is built to refuse.
        """
        check = E2ECheck(id="c", command="python e2e_checkout.py", feature="f")
        good = e2e_verify.E2EResult(
            check_id="good",
            feature="f",
            state="passed",
            reason="worked",
            ran=True,
            exit_code=0,
            observations=("passed",),
        )
        void = e2e_verify.untrustworthy_result(check, {}, reason="no subject", ran=True)
        report = e2e_verify.E2EReport(
            results=(good, void),
            policy=e2e_verify.should_run_e2e(declared_checks=2, requested=True),
            spec_status=e2e_verify.SPEC_FOUND,
        )
        assert report.verdict == "untrustworthy", report.verdict
        assert report.settled is False
        assert len(report.passed) == 1 and len(report.untrustworthy) == 1

    def test_the_verdict_ordering_is_pinned_over_the_whole_cube(self):
        """Every combination of rows, checked against the precedence."""
        states = ("passed", "failed", "blocked", "not_run", "untrustworthy")
        expectations = {
            "untrustworthy": "untrustworthy",
            "blocked": "blocked",
            "failed": "failed",
            "not_run": "not_run",
            # The all-green combination is the one my first draft LEFT OUT of
            # this table, so `expectations.get("passed")` returned None and the
            # cube test asserted `verdict == None`. A lookup table with a
            # missing default is the same bug as a defaulted coverage of 0.
            "passed": "passed",
        }
        from itertools import combinations_with_replacement

        for combo in combinations_with_replacement(states, 2):
            rows = []
            for index, state in enumerate(combo):
                rows.append(
                    e2e_verify.E2EResult(
                        check_id="c%d" % index,
                        feature="f",
                        state=state,
                        reason="r",
                        ran=state in ("passed", "failed", "untrustworthy"),
                        exit_code=0 if state == "passed" else 1,
                        observations=(state,) if state in ("passed", "untrustworthy") else (),
                        trust={"reason": "r"} if state == "untrustworthy" else {},
                    )
                )
            report = e2e_verify.E2EReport(
                results=tuple(rows),
                policy=e2e_verify.should_run_e2e(declared_checks=2, requested=True),
            )
            want = expectations.get(
                next(
                    (
                        s
                        for s in ("untrustworthy", "blocked", "failed", "not_run")
                        if s in combo
                    ),
                    "passed",
                )
            )
            assert report.verdict == want, (combo, report.verdict, want)


# ==========================================================================
# 2. W2.1 rows 1-3 — ownership
# ==========================================================================


class TestOwnershipIsTheRepositoriesAndNotTheAgents:
    def test_a_test_path_is_refused_as_an_end_to_end_check(self):
        repo = _tmp_repo("own1")
        may_run, verdict = e2e_trust.guard_e2e_check(
            E2ECheck(
                id="a",
                command="python -m pytest -q tests/test_app.py",
                feature="checkout",
            ),
            repo_path=str(repo),
        )
        assert may_run is False
        assert "command_runs_a_test_path" in verdict.signals

    def test_a_moved_declaration_digest_names_the_offender(self):
        repo = _tmp_repo("own2")
        check = E2ECheck(id="r", command="python e2e_checkout.py", feature="checkout")
        first = e2e_attack.write_declaration(repo, [{"id": "r", "command": check.command}])
        second = e2e_attack.write_declaration(
            repo, [{"id": "r", "command": "python e2e_checkout.py || true"}]
        )
        assert first != second
        may_run, verdict = e2e_trust.guard_e2e_check(
            check, repo_path=str(repo), declaration_digest=second, vouched_digest=first
        )
        assert may_run is False
        assert "declaration_digest_moved" in verdict.signals
        assert verdict.ownership.agent_authored is True

    def test_an_EDITED_declaration_is_refused_even_when_the_COMMAND_is_honest(self):
        """The digest binding must not depend on the command being weak.

        Otherwise row 2 collapses into row 3: an agent that edits the declaration
        to something equally plausible is not caught.
        """
        repo = _tmp_repo("own2b")
        check = E2ECheck(id="r", command="python e2e_checkout.py", feature="checkout")
        may_run, verdict = e2e_trust.guard_e2e_check(
            check,
            repo_path=str(repo),
            declaration_digest="deadbeefdeadbeef",
            vouched_digest="cafebabecafebabe",
        )
        assert may_run is False
        assert "declaration_digest_moved" in verdict.signals
        assert "command_is_inert" not in verdict.signals

    @pytest.mark.parametrize(
        "command,expected_signal",
        [
            ("python e2e_checkout.py -k 'not checkout'", "command_skips_its_own_cases"),
            ("python e2e_checkout.py || true", "command_self_neutralised"),
            ("python e2e_checkout.py ; exit 0", "command_self_neutralised"),
            ("python e2e_checkout.py && true", "command_self_neutralised"),
            ("echo '=== 1 passed ==='", "command_forges_its_own_output"),
        ],
    )
    def test_each_neutralising_shape_is_its_own_signal(self, command, expected_signal):
        repo = _tmp_repo("own3")
        may_run, verdict = e2e_trust.guard_e2e_check(
            E2ECheck(id="s", command=command, feature="checkout"), repo_path=str(repo)
        )
        assert may_run is False, command
        assert expected_signal in verdict.signals, (command, verdict.signals)

    def test_the_neutraliser_scan_reads_the_WHOLE_command(self):
        """Regression: the first version split on `||` and lost the very span.

        `... || true` splits into two innocent segments, so a per-segment scan
        reports the most common stub in the world as an honest command.
        """
        assert e2e_trust.self_neutralised_spans("python x.py || true")
        assert e2e_trust.self_neutralised_spans("python x.py && true")
        assert e2e_trust.self_neutralised_spans("python x.py ; exit 0")
        assert not e2e_trust.self_neutralised_spans("python x.py")

    def test_an_invented_origin_does_not_unlock_a_bypass(self):
        """`ORIGINS` is closed to two; `root` must not be a third.

        An EMPTY origin is NOT in this list, and that is a distinction worth
        pinning: `str(origin or DEFAULT_ORIGIN)` maps `""` onto `agent`, so an
        unset origin is treated as the MOST restrictive one rather than as an
        error. `"AGENT"` IS invented -- the vocabulary is closed by value, not
        by case -- so it resolves to `unknown_origin`.
        """
        for origin in ("root", "admin", "AGENT", "system", "Agent", " operator"):
            verdict = test_guard.command_deletion_verdict(
                "rm tests/test_a.py", origin=origin
            )
            assert verdict.refused is True, origin
            assert verdict.shape == "unknown_origin", (origin, verdict.shape)
        # An empty origin resolves to agent and is still refused -- fail-closed
        # rather than fail-open.
        assert test_guard.command_deletion_verdict(
            "rm tests/test_a.py", origin=""
        ).refused is True

    def test_the_user_may_do_everything_the_agent_may_not(self):
        """The control arm. Without it, "the agent may not" is a claim."""
        for command in (
            "rm tests/test_a.py",
            "chmod 000 tests/test_a.py",
            "git checkout -- tests/",
            "python -m pytest -q --deselect tests/test_a.py::test_a",
        ):
            assert test_guard.command_deletion_verdict(command, origin="agent").refused
            assert not test_guard.command_deletion_verdict(command, origin="user").refused


# ==========================================================================
# 3. W2.1 row 4 — check strength, and the `unmeasurable` non-vacuity control
# ==========================================================================


class TestCheckStrength:
    def test_a_check_that_can_fail_is_strong_and_one_that_cannot_is_weak(self):
        repo = _tmp_repo("strength")
        strong = e2e_trust.assess_strength(
            E2ECheck(id="g", command="python e2e_checkout.py", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed in 0.3s ===",
            container_name="hexec-e0-p1-w1",
            ran=True,
        )
        weak = e2e_trust.assess_strength(
            E2ECheck(id="w", command="python e2e_weak.py", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed in 0.3s ===",
            container_name="hexec-e0-p1-w2",
            ran=True,
        )
        assert strong.strength == "strong" and strong.can_fail is True
        assert weak.strength == "weak" and weak.can_fail is True
        assert "command_asserts_nothing" in weak.weak_signals

    def test_can_fail_is_DECIDED_and_published(self):
        """The sharpest single answer, and it is mechanical."""
        repo = _tmp_repo("canfail")
        inert = e2e_trust.assess_strength(
            E2ECheck(id="i", command="true", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="",
            container_name="hexec-e0-p1-w3",
            ran=True,
        )
        assert inert.can_fail is False
        assert inert.may_report_passed is False

    def test_unmeasurable_is_NOT_strong(self):
        """The non-vacuity control: nothing looked at is not a pass.

        A command that matches no weak shape and no strong one either must
        report `unmeasurable`, never `strong`. Without this the assessment
        approves every command it has never seen.
        """
        repo = _tmp_repo("unmeasurable")
        # An empty command is refused by E2ECheck itself ("a check with no
        # command is not a check that passes"), so the unmeasurable case
        # needs a LEGAL command that simply matches no weak or strong shape.
        verdict = e2e_trust.assess_strength(
            E2ECheck(id="x", command="pip install -q requests", feature=""),
            repo_path=str(repo),
        )
        assert verdict.strength in ("unmeasurable", "weak")
        assert verdict.may_report_passed is False

    def test_NO_weak_signal_is_compensable_by_a_strong_one(self):
        """The design decision, asserted.

        A design with a "compensating strong signal" escape hatch has exactly
        one entry, `compensates_everything`, and every weak signal stops being
        weak. So each weak signal is checked in isolation against a command that
        carries plenty of strong signals.
        """
        repo = _tmp_repo("noescape")
        # A command that names an EXISTING file (strong), declares a feature
        # (strong), and carries a result summary (strong) -- and forces its own
        # exit code. It must still be weak.
        verdict = e2e_trust.assess_strength(
            E2ECheck(
                id="c",
                command="python e2e_checkout.py || true",
                feature="checkout",
            ),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed ===",
            container_name="hexec-e0-p1-w4",
            ran=True,
        )
        assert "names_an_existing_repo_path" in verdict.strong_signals
        assert "declares_a_feature" in verdict.strong_signals
        assert verdict.strength == "weak", "a strong signal compensated a weak one"
        assert verdict.may_report_passed is False

    def test_every_weak_and_strong_signal_has_a_published_description(self):
        for name in e2e_trust.WEAK_SIGNALS + e2e_trust.STRONG_SIGNALS:
            assert name in e2e_trust.SIGNAL_DESCRIPTIONS, name
            assert len(e2e_trust.SIGNAL_DESCRIPTIONS[name]) > 45, name

    def test_the_signal_vocabularies_are_disjoint(self):
        assert not set(e2e_trust.WEAK_SIGNALS) & set(e2e_trust.STRONG_SIGNALS)

    def test_a_legitimate_curl_check_is_NOT_refused(self):
        """A false positive on working code is a check that gets disabled.

        Found for real this round: a `curl {host}:{port}/health` check names
        nothing in the repository and no runner, so the first version of
        `command_exercises_nothing` refused a perfectly good end-to-end check.
        """
        repo = _tmp_repo("curl")
        verdict = e2e_trust.assess_check(
            E2ECheck(id="h", command="curl -sS {host}:{port}/health", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output='{"status":"ok"}',
            container_name="hexec-e0-p1-w5",
            ran=True,
            duration_s=4.0,
        )
        assert verdict.may_report_passed is True, verdict.signals

    def test_the_subject_decision_is_ONE_function_and_the_two_verdicts_agree(self):
        """Two assessments, one question. They must not be able to disagree.

        The first version computed "does this command have a subject" twice:
        the strength half learned about external surfaces and the ownership half
        did not, so a legitimate curl check was reported `strong` by one and
        `command_exercises_nothing` by the other.
        """
        for command in (
            "curl -sS {host}:{port}/health",
            "python e2e_checkout.py",
            "true",
            'python -c "import sys; sys.exit(0)"',
            "echo '=== 1 passed ==='",
        ):
            for repo_label in ("subj",):
                repo = _tmp_repo(repo_label)
                verdict = e2e_trust.assess_check(
                    E2ECheck(id="c", command=command, feature="checkout"),
                    repo_path=str(repo),
                    exit_code=0,
                    raw_output="=== 1 passed ===",
                    container_name="hexec-e0-p1-w6",
                    ran=True,
                    duration_s=3.0,
                )
                in_ownership = "command_exercises_nothing" in verdict.ownership.signals
                in_strength = "command_exercises_nothing" in verdict.strength.weak_signals
                assert in_ownership == in_strength, (command, verdict.signals)


# ==========================================================================
# 4. W2.1 row 6 — suspiciously fast
# ==========================================================================


class TestSuspiciouslyFastPasses:
    def test_every_floor_is_measured_with_its_samples_published(self):
        for name, row in e2e_trust.FLOOR_TABLE.items():
            assert row["measured"].startswith("MEASURED"), name
            assert row["samples"] >= 3, name
            assert row["measured_min_s"] > 0, name
            assert len(row["samples_s"]) == row["samples"], name
            assert row["floor_s"] < row["measured_min_s"], (
                f"{name}: a floor ABOVE the measured minimum would fire on this "
                f"host's own slowest honest run"
            )

    def test_a_floor_never_renders_as_zero(self):
        """The Wave 1 lesson: a real 3ms must not print as `0.00s`.

        `0.00s` is the same string a genuinely free operation produces, on the
        one row whose entire purpose is to show a lookup is cheap but not free.
        """
        table = session_economics.cost_table()
        lookup = next(
            row for row in table if row.name == session_economics.COST_CACHE_LOOKUP
        )
        assert lookup.seconds is not None
        assert lookup.rendered != "0.00s"
        assert "ms" in lookup.rendered
        rendered = "\n".join(session_economics.render_cost_table())
        assert "0.00s" not in rendered

    def test_the_structural_arm_needs_no_timing_at_all(self):
        """A driver that stubs the boundary cannot hide by faking a clock."""
        check = E2ECheck(id="c", command="python e2e_checkout.py", feature="checkout")
        verdict = e2e_trust.assess_fast_pass(
            check, duration_s=99.0, container_name="", ran=True
        )
        assert verdict.suspicious is True
        assert any("no container name" in reason for reason in verdict.reasons)

    def test_the_ratio_arm_is_host_independent(self):
        check = E2ECheck(id="c", command="python e2e_checkout.py", feature="checkout")
        verdict = e2e_trust.assess_fast_pass(
            check,
            duration_s=0.05,
            container_name="hexec-e0-p1-w7",
            ran=True,
            prior_durations_s=[3.2, 3.4, 3.1],
        )
        assert verdict.suspicious is True
        assert verdict.ratio_to_prior is not None
        assert verdict.ratio_to_prior < e2e_trust.FAST_PASS_RATIO

    def test_a_real_duration_is_never_suspicious(self):
        check = E2ECheck(id="c", command="python e2e_checkout.py", feature="checkout")
        verdict = e2e_trust.assess_fast_pass(
            check,
            duration_s=3.2,
            container_name="hexec-e0-p1-w8",
            ran=True,
            prior_durations_s=[3.2, 3.4, 3.1],
        )
        assert verdict.suspicious is False

    def test_a_server_check_takes_the_HIGHER_floor(self):
        plain = E2ECheck(id="p", command="python e2e_checkout.py", feature="f")
        server = E2ECheck(id="s", command="python e2e_checkout.py", feature="f",
                          publish_port=8000)
        plain_name, plain_floor = e2e_trust.floor_for(plain)
        server_name, server_floor = e2e_trust.floor_for(server)
        assert plain_name == "any_sandboxed_command"
        assert server_name == "server_starting_command"
        assert server_floor > plain_floor, (
            "a check that binds a port pays for a container AND a bind, so it "
            "takes the higher floor"
        )


# ==========================================================================
# 5. W2.1 rows 5, 7, 8, 9 — the other structural refusals
# ==========================================================================


class TestTheRemainingStructuralRefusals:
    def test_a_passed_verdict_without_a_run_is_IMPOSSIBLE(self):
        with pytest.raises(ValueError):
            e2e_verify.E2EResult(
                check_id="c", feature="f", state="passed", reason="claimed", ran=False
            )

    def test_cross_commit_evidence_RAISES_and_the_exception_is_not_swallowed(self):
        """`CrossCommitRefused` is neither ValueError nor PermissionError.

        A caller with `except ValueError` around its evidence assembly would
        swallow it; a caller with `except PermissionError` would treat it as an
        approval question. Neither is what it is.
        """
        assert not issubclass(e2e_evidence.CrossCommitRefused, ValueError)
        assert not issubclass(e2e_evidence.CrossCommitRefused, PermissionError)
        repo = _tmp_repo("cross")
        anchor = e2e_trust.read_commit(str(repo))
        evidence = e2e_evidence.build_evidence(
            None,
            repo_path=str(repo),
            environment=e2e_evidence.environment_fingerprint(
                image="i", boundary="b", runner="r", network="n"
            ),
        )
        bound = _rebind(evidence, anchor)
        assert e2e_evidence.assert_same_commit(bound, anchor.commit) == anchor.commit
        with pytest.raises(e2e_evidence.CrossCommitRefused):
            e2e_evidence.assert_same_commit(bound, "f" * 40)

    def test_an_UNRESOLVED_anchor_is_refused_not_wildcarded(self):
        repo = _tmp_repo("cross2")
        evidence = e2e_evidence.build_evidence(None, repo_path=str(repo))
        bound = _rebind(
            evidence, e2e_trust.CommitAnchor(commit="", status="unknown")
        )
        with pytest.raises(e2e_evidence.CrossCommitRefused):
            e2e_evidence.assert_same_commit(bound, "a" * 40)
        # Both empty IS accepted, and that is the documented reading: a
        # repository with no commit yet cannot disagree with itself, and the
        # anchor's `status` is what says so. Asserted BOTH ways, because a pin
        # that only checked the refusal would also pass a function that refused
        # unconditionally -- which is exactly the bug this class exists for.
        assert e2e_evidence.assert_same_commit(bound, "") == ""

    def test_a_non_sha_anchor_is_refused_rather_than_stringified(self):
        """Regression: the first version read the CommitAnchor OBJECT.

        `str(CommitAnchor(...))` produced `"CommitAnchor(commit='52b0…', …)"`,
        which matches no sha, so the equality never held and EVERY call raised —
        including the control. A cross-commit check that refuses everything is
        indistinguishable from a working one by its refusals alone.
        """
        repo = _tmp_repo("cross3")
        evidence = e2e_evidence.build_evidence(None, repo_path=str(repo))
        object_anchor = _rebind(evidence, e2e_trust.read_commit(str(repo)))
        # The anchor's `commit` FIELD is the object; the reader must use the
        # `commit_sha` PROPERTY or every comparison fails.
        assert isinstance(object_anchor.anchor.commit, e2e_trust.CommitAnchor)
        assert e2e_evidence.assert_same_commit(
            object_anchor, object_anchor.anchor.commit.commit
        )

    def test_flake_rate_is_a_PROPERTY_so_it_cannot_drift(self):
        """The RATE the brief's row 9 asks for: how often the OTHER answer came.

        Both halves are pinned, and the three-valued verdict is computed by
        :func:`execution.e2e_verify._flaky_check` rather than by constructing a
        row with a hand-written ``flaky_check``. That distinction matters: the
        constructor takes ``flaky_check`` as a field, so a pin that set it
        itself would be asserting its own literal back at itself.
        """
        observations = ("passed", "failed", "passed")
        assert e2e_verify._flaky_check(observations) == "flaky_detected"
        row = e2e_verify.E2EResult(
            check_id="c",
            feature="f",
            state="passed",
            reason="r",
            ran=True,
            exit_code=0,
            observations=observations,
            flaky_check=e2e_verify._flaky_check(observations),
        )
        # flake_rate is rounded to 4 places; the pin compares at that
        # resolution rather than to full float precision.
        assert row.flake_rate == pytest.approx(1 / 3, abs=5e-5)
        assert row.flaky_check == "flaky_detected"
        assert row.flaky is True

        stable_obs = ("passed", "passed")
        stable = e2e_verify.E2EResult(
            check_id="c",
            feature="f",
            state="passed",
            reason="r",
            ran=True,
            exit_code=0,
            observations=stable_obs,
            flaky_check=e2e_verify._flaky_check(stable_obs),
        )
        assert stable.flake_rate == 0.0
        assert stable.flaky_check == "not_flaky"

        # An UNMEASURED rate is None, never 0.0.
        unmeasured = e2e_verify.E2EResult(
            check_id="c", feature="f", state="passed", reason="r", ran=True, exit_code=0
        )
        assert unmeasured.flake_rate is None

    def test_one_observation_is_not_run_not_not_flaky(self):
        row = e2e_verify.E2EResult(
            check_id="c",
            feature="f",
            state="passed",
            reason="r",
            ran=True,
            exit_code=0,
            observations=("passed",),
        )
        assert row.flaky_check == "not_run"
        assert row.flaky is False


def _rebind(evidence: Any, anchor: e2e_trust.CommitAnchor) -> e2e_evidence.E2EEvidence:
    """A copy of the evidence anchored to a specific commit."""
    return e2e_evidence.E2EEvidence(
        anchor=e2e_evidence.EvidenceAnchor(
            what_ran=evidence.anchor.what_ran,
            commit=anchor,
            selection=evidence.anchor.selection,
            declaration_digest=evidence.anchor.declaration_digest,
            content_digest=evidence.anchor.content_digest,
            environment=evidence.anchor.environment,
            session_id=evidence.anchor.session_id,
            repo_path=evidence.anchor.repo_path,
        ),
        rows=evidence.rows,
        coverage=evidence.coverage,
        verdict=evidence.verdict,
        policy=evidence.policy,
        spec_status=evidence.spec_status,
        spec_reason=evidence.spec_reason,
        reuse=evidence.reuse,
        ledger=evidence.ledger,
    )


# ==========================================================================
# 6. W2.2 — the evidence object
# ==========================================================================


class TestTheEvidenceIsSelfDescribing:
    def test_zero_coverage_is_not_run_and_NEVER_a_pass(self):
        """The single most misreadable number in the system.

        A reader scanning a percentage column cannot tell "we ran nothing" from
        "we ran everything and covered nothing", and that confusion IS the
        failure.
        """
        nothing = e2e_evidence.Selection(0, 0, 0, 0)
        receipt = e2e_evidence.CoverageReceipt(nothing)
        assert receipt.verdict == "not_run"
        assert receipt.may_report_passing is False
        assert receipt.fraction is None, "coverage of NOTHING is None, never 0.0"

        zero_of_three = e2e_evidence.CoverageReceipt(e2e_evidence.Selection(3, 3, 0, 0))
        assert zero_of_three.verdict == "not_run"
        assert zero_of_three.fraction == 0.0
        assert zero_of_three.may_report_passing is False
        assert "can never be rendered beside a passing claim" in json.dumps(
            zero_of_three.to_dict()
        )

        partial = e2e_evidence.CoverageReceipt(e2e_evidence.Selection(3, 3, 2, 2))
        assert partial.verdict == "partial"
        assert partial.may_report_passing is False

        complete = e2e_evidence.CoverageReceipt(e2e_evidence.Selection(3, 3, 3, 3))
        assert complete.verdict == "complete"
        assert complete.may_report_passing is True

    def test_the_coverage_vocabulary_is_DISJOINT_from_the_state_vocabulary(self):
        """A coverage verdict must never be mistakable for a state."""
        states = set(e2e_verify.E2E_STATES)
        coverages = {"complete", "partial", "not_run"}
        assert not states & (coverages - {"not_run"}), (
            "'complete' and 'partial' must not exist as states"
        )
        assert "not_run" in states and "not_run" in coverages

    def test_an_impossible_selection_is_refused(self):
        with pytest.raises(ValueError):
            e2e_evidence.Selection(2, 5, 0, 0)
        with pytest.raises(ValueError):
            e2e_evidence.Selection(5, 2, 5, 5)

    def test_a_reused_row_is_marked_and_can_never_be_fresh(self):
        mark = e2e_trust.reuse_of(content_digest="a" * 16, stored_at=0.0)
        assert mark["reused"] is True
        assert mark["fresh_evidence"] is False
        assert mark["distinct_from_fresh"] is True
        assert mark["reused_from"] == "a" * 16
        assert mark["may_mark_feature_passing"] is False
        assert mark["summary"], "a reused row must NAME what it was reused from"

    def test_the_producer_of_a_reuse_mark_cannot_emit_fresh_evidence(self):
        """Structural, not a convention somebody can forget.

        The only way to test a producer cannot emit a value is to check that no
        input produces it — so this asserts the value is a hard-coded literal.
        """
        source = inspect.getsource(e2e_trust.reuse_of)
        tree = ast.parse(source)
        fresh_values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "fresh_evidence"
                        and isinstance(value, ast.Constant)
                    ):
                        fresh_values.append(value.value)
        assert fresh_values, "the key must be present"
        assert set(fresh_values) == {False}, fresh_values

    def test_the_evidence_names_what_commit_and_selection_or_says_why_not(self):
        repo = _tmp_repo("selfdesc")
        environment = e2e_evidence.environment_fingerprint(
            image="i", boundary="b", runner="r", network="n"
        )
        evidence = e2e_evidence.build_evidence(
            None, repo_path=str(repo), session_id="s", environment=environment
        )
        # Nothing was declared, so the anchor says so rather than claiming to be
        # sufficient.
        assert evidence.anchor.self_describing is False
        missing = " ".join(evidence.anchor.unself_describing)
        assert "what_ran" in missing or "selection" in missing
        rendered = "\n".join(evidence.render())
        assert "commit" in rendered
        assert "selection" in rendered
        assert "NOT SUFFICIENT ON ITS OWN" in rendered

    def test_an_unmeasured_environment_is_not_a_wildcard(self):
        unknown = e2e_evidence.environment_fingerprint()
        measured = e2e_evidence.environment_fingerprint(
            image="i", boundary="b", runner="r", network="n"
        )
        assert unknown["fully_measured"] is False
        assert set(unknown["unmeasured_fields"]) == set(
            e2e_evidence.ENVIRONMENT_FIELDS
        )
        assert unknown["digest"] != measured["digest"], (
            "two unknown environments have NOT been shown to match, and giving "
            "them the same digest would make every reuse decision look justified"
        )

    def test_a_row_outside_the_five_states_makes_the_tally_refuse(self):
        class Rogue:
            state = "skipped"

        with pytest.raises(AssertionError):
            e2e_evidence.assert_states_not_collapsed([Rogue()])

    def test_the_ledger_gate_refuses_on_every_axis_and_reports_each(self):
        repo = _tmp_repo("gate")
        evidence = e2e_evidence.build_evidence(None, repo_path=str(repo))
        verdict = e2e_evidence.ledger_gate(evidence)
        assert verdict.provable is False
        assert verdict.e2e_unmet, "an evidence object with nothing measured must refuse"
        joined = " ".join(verdict.e2e_unmet)
        assert "e2e_fresh" in joined
        assert "e2e_coverage_complete" in joined
        assert verdict.e2e_reasons, "a refusal must carry a reason"

    def test_the_ledger_gate_refuses_an_object_with_no_evidence(self):
        with pytest.raises(TypeError):
            e2e_evidence.ledger_gate(object())

    def test_the_unit_half_is_DELEGATED_not_restated(self):
        """One authority. Two provenance gates is a second answer."""
        source = inspect.getsource(e2e_evidence.ledger_gate)
        assert "from execution.verification_evidence import ledger_evidence" in source
        # The end-to-end requirements are its own; the unit ones are named only
        # as the DELEGATED function's members.
        assert len(e2e_evidence.E2E_PROVABILITY) == 5
        for requirement_id, question in e2e_evidence.E2E_PROVABILITY:
            assert question and len(question) > 20, requirement_id


# ==========================================================================
# 7. W2.3 — the multi-session axes
# ==========================================================================


class TestMultiSessionAxesAreHonest:
    def test_an_unobserved_axis_can_NEVER_be_held(self):
        with pytest.raises(ValueError):
            multi_session.MultiSessionRow(
                axis="crosstalk", verdict="HELD", observed=False
            )

    def test_the_axis_verdict_vocabulary_has_four_distinct_values(self):
        assert len(set(multi_session.AXIS_VERDICTS)) == 4
        assert "DEGRADED" in multi_session.AXIS_VERDICTS, (
            "a limit relaxed so the run could proceed is capacity, not a "
            "containment failure, and conflating them sends an operator looking "
            "for an attacker"
        )

    def test_the_aggregate_limit_arithmetic_is_correct(self):

        assert multi_session._parse_memory("1g") == 1024 ** 3
        assert multi_session._parse_memory("512m") == 512 * 1024 ** 2
        assert multi_session._parse_memory("2GiB") == 2 * 1024 ** 3
        assert multi_session._parse_memory("nonsense") == 0

    def test_the_host_memory_reader_reports_the_DAEMON_not_the_OS(self):
        """Measured today: the daemon says 7.62 GiB, the Windows host 15.72 GiB.

        The limit that binds is the one the daemon enforces, so reporting the
        OS's figure would be a measurement of the wrong machine.
        """
        total, source = multi_session._host_memory_pair()
        assert total > 0
        assert "FALLBACK" not in source, (
            f"the daemon's figure was unreadable and the axis fell through to the "
            f"OS's memory ({source})"
        )

    def test_free_memory_is_None_when_the_daemon_does_not_publish_it(self):
        """`None`, never `0`: the axis's whole subject is capacity."""
        free = multi_session._docker_free_memory_bytes()
        assert free is None or free > 0

    def test_an_over_committed_limit_is_DEGRADED_not_breach(self):
        row = multi_session.aggregate_limit_report(sessions=200, mem_limit="1g")
        assert row.verdict == "DEGRADED", row.verdict
        assert row.observed is True
        assert row.evidence["promised_fraction_of_host"] > multi_session.AGGREGATE_HOST_HEADROOM

    def test_a_measurable_host_is_HELD_below_the_bar(self):
        row = multi_session.aggregate_limit_report(sessions=1, mem_limit="256m")
        assert row.verdict == "HELD", row.verdict
        assert row.evidence["host_total_source"]

    def test_every_axis_is_constructible_and_renders(self):
        for axis in multi_session.MULTI_SESSION_AXES:
            row = multi_session.MultiSessionRow(
                axis=axis,
                verdict="HELD",
                detail="synthetic",
                evidence={"k": [1, 2]},
            )
            rendered = "\n".join(row.render())
            assert axis in rendered
            assert json.loads(json.dumps(row.to_dict(), default=str))


# ==========================================================================
# 8. W2.4 — cost, policy, coverage, reuse
# ==========================================================================


class TestVerificationIsAffordableAndHonest:
    def test_every_cost_row_is_measured_or_says_it_is_not(self):
        for row in session_economics.cost_table():
            if row.measured:
                assert row.seconds is not None
                assert "MEASURED" in row.basis
            else:
                assert row.seconds is None
                assert "UNMEASURED" in row.rendered

    def test_the_e2e_multiple_is_DERIVED_not_asserted(self):
        """A hand-written multiple is a number nobody took."""
        multiple = session_economics.e2e_rolloff_multiple()
        if multiple is None:
            return
        assert multiple > 0
        table = {row.name: row for row in session_economics.cost_table()}
        e2e = table[session_economics.COST_E2E_SINGLE_CHECK].seconds
        unit = table[session_economics.COST_UNIT_VERIFICATION_WARM].seconds
        assert multiple == pytest.approx(e2e / unit, abs=5e-4), (
            "e2e_rolloff_multiple() rounds to 3 places, so the pin must "
            "compare at that resolution rather than to full float precision"
        )

    def test_the_policy_covers_every_kind_and_names_its_authority(self):
        policies = session_economics.run_policy()
        kinds = {row.kind for row in policies}
        assert kinds == set(session_economics.POLICY_KINDS)
        for row in policies:
            assert row.when and row.why and row.decided_from
            assert row.verdict

    def test_the_policy_never_says_verification_is_skipped(self):
        """Skipping verification is how a project rots quietly."""
        rendered = "\n".join(session_economics.render_policy()).lower()
        assert "never skipped" in rendered
        assert "rots quietly" in rendered
        assert "never whether it runs" in rendered
        unit = next(row for row in session_economics.run_policy() if row.kind == "unit_verification")
        assert unit.verdict == "ALWAYS"

    def test_coverage_is_REPORTED_and_never_assumed(self):
        """Delegated to the selector's own completeness and soundness methods.

        A second coverage rule in this module would be able to disagree with the
        rule the selector actually used to decide what to run, and the
        disagreement would show up as a false pass rather than as a failure.
        """
        receipt = session_economics.selection_receipt(None)
        assert receipt.may_report_passing is False
        assert receipt.fraction is None
        source = inspect.getsource(session_economics.selection_receipt)
        assert "coverage_is_complete" in source
        assert "coverage_is_sound" in source

    def test_complete_and_sound_are_CONJUNCTIONED(self):
        """`fallback_all` selects everything and is COMPLETE while being UNSOUND.

        Reporting that as a partial selection would train every reader of the
        flag to ignore it, which destroys the only thing it is for.
        """
        receipt = session_economics.CoverageReceipt(
            declared=3, executed=3, complete=True, sound=False, strategy="fallback_all"
        )
        assert receipt.fraction == 1.0
        assert receipt.may_report_passing is False, (
            "a complete but unsound selection may not sit beside a passing claim"
        )
        good = session_economics.CoverageReceipt(
            declared=3, executed=3, complete=True, sound=True, strategy="import_graph"
        )
        assert good.may_report_passing is True

    def test_reuse_requires_ALL_THREE_groups_and_names_the_one_that_failed(self):
        environment = {"digest": "x", "fully_measured": True, "unmeasured_fields": []}
        same = session_economics.reuse_is_sound(
            content_digest="a",
            source_content_digest="a",
            declaration_digest="d",
            source_declaration_digest="d",
            environment=environment,
            source_environment=environment,
        )
        assert same.sound is True

        for kwargs, group in (
            ({"source_content_digest": "b"}, "source"),
            ({"source_declaration_digest": "e"}, "declaration"),
            ({"source_environment": {"digest": "y", "fully_measured": True}}, "environment"),
        ):
            base = {
                "content_digest": "a",
                "source_content_digest": "a",
                "declaration_digest": "d",
                "source_declaration_digest": "d",
                "environment": environment,
                "source_environment": environment,
            }
            base.update(kwargs)
            decision = session_economics.reuse_is_sound(**base)
            assert decision.sound is False, group
            assert group in decision.failed_groups
            assert decision.reasons, group

    def test_an_EMPTY_declaration_digest_is_not_a_wildcard(self):
        decision = session_economics.reuse_is_sound(
            content_digest="a",
            source_content_digest="a",
            declaration_digest="",
            source_declaration_digest="d",
            environment={"digest": "x", "fully_measured": True},
            source_environment={"digest": "x", "fully_measured": True},
        )
        assert decision.sound is False
        assert "declaration" in decision.failed_groups

    def test_the_reuse_rate_reports_both_numbers_and_the_flattering_one_second(self):
        report = session_economics.reuse_rate(total=10, reused=6, reverified=4)
        assert report.reuse_rate == 0.6
        assert report.reverified_rate == 0.4
        rendered = "\n".join(report.render())
        assert "reuse rate" in rendered
        assert "RE-VERIFIED" in rendered
        assert "how much did you actually check" in rendered

    def test_a_run_cannot_be_both_reused_and_re_verified(self):
        with pytest.raises(ValueError):
            session_economics.reuse_rate(total=4, reused=3, reverified=3)

    def test_a_zero_total_is_NOT_a_zero_rate(self):
        report = session_economics.reuse_rate(total=0, reused=0, reverified=0)
        assert report.reuse_rate is None
        assert report.reverified_rate is None


# ==========================================================================
# 9. W2.5 — test-removal refusals, and the two gaps they found
# ==========================================================================


class TestTheTestRemovalGuardHasNoBypassPath:
    @pytest.mark.parametrize(
        "command",
        [
            "rm src/tests/test_checkout.py",
            "chmod 000 src/tests/test_checkout.py",
            "chmod -x src/tests/test_checkout.py",
            "chmod go-w tests/test_a.py",
            "git checkout -- src/tests/",
            "git clean -fd",
            "python -m pytest -q --deselect tests/test_a.py::test_a",
            "python -m pytest -q --ignore tests/",
            "python -m pytest -q -m 'not slow'",
            "python -m pytest -q --collect-only tests/",
        ],
    )
    def test_every_known_bypass_door_is_refused(self, command):
        verdict = test_guard.command_deletion_verdict(command, origin="agent")
        assert verdict.refused is True, (command, verdict.reason)

    @pytest.mark.parametrize(
        "command",
        [
            "chmod +x scripts/run.sh",
            "python -m pytest -q --ignore htmlcov",
            "python -m pytest -q tests/test_a.py -k checkout",
            "rm app.py",
            "git status",
            "ls tests",
        ],
    )
    def test_every_legitimate_command_is_still_allowed(self, command):
        """A guard that refuses working commands is a guard that gets disabled."""
        verdict = test_guard.command_deletion_verdict(command, origin="agent")
        assert verdict.refused is False, (command, verdict.reason)

    def test_the_chmod_row_is_NOT_a_blanket_chmod(self):
        """`chmod +x scripts/run.sh` is a legitimate non-test edit."""
        assert test_guard.command_deletion_verdict("chmod +x scripts/run.sh").refused is False
        assert test_guard.command_deletion_verdict("chmod 000 scripts/run.sh").refused is False

    def test_a_runner_binary_name_is_not_a_test_path(self):
        """Found this round: `_matches_builtin("pytest")` returned True.

        The command gate's own token reader then classified
        `python -m pytest -q --ignore htmlcov` as naming a test path and refused
        it. The false positive predates the round — it was INVISIBLE because
        nothing matched a coverage-removing flag, so the buggy token was never
        consulted for a command with no deletion shape.
        """
        for name in ("pytest", "jest", "vitest", "tox"):
            assert test_guard._matches_builtin(name) is False, name
        # ... and a file that merely CONTAINS a runner name is still a test.
        assert test_guard._matches_builtin("tests/test_pytest_wrapper.py") is True
        assert test_guard._matches_builtin("tests/test_a.py") is True

    def test_the_deselect_table_says_which_ROWS_need_a_test_path(self):
        """`--ignore` names a path; `-m 'not ...'` names cases. Different gates.

        Applying the path gate to a case-naming flag leaves it allowed, because
        such a command names no path at all -- which is how `-m 'not slow'`
        stayed allowed through two rounds.
        """
        needs_path = {
            why: flag for _pattern, why, flag in test_guard.COVERAGE_REMOVING_FLAGS
        }
        assert needs_path["--ignore removes a path from collection"] is True
        assert needs_path["--deselect names a case to exclude"] is False
        assert needs_path["-m 'not ...' excludes by marker"] is False

    def test_the_end_to_end_suite_holds_with_both_arms(self):
        """24 agent attempts refused AND 24 user attempts allowed.

        The user arm is the control. Without it a refusal-only suite is
        satisfied by a guard that refuses everything, which is the failure mode
        this repository has hit twice in one round before.
        """
        suite = test_removal_e2e.run_all(
            shapes=("unlink", "patch_to_empty", "rename_away"),
            paths=("workspace_write", "workspace_shell", "backend_execute"),
        )
        assert suite.verdict == "HELD", [
            (row.shape, row.path, row.origin, row.outcome) for row in suite.wrong
        ]
        agent = suite.agent_attempts
        user = suite.user_attempts
        assert agent and user
        for row in agent:
            assert row.refused is True, row.shape
            assert row.file_changed is False, (
                "a refusal after the bytes moved has not prevented anything"
            )
            assert row.reached_user is True, "a refusal nobody sees gets retried"
        for row in user:
            assert row.refused is False, row.shape

    def test_a_not_applicable_row_is_PUBLISHED_not_dropped(self):
        """A shape this path cannot express must be named, not silently counted.

        A reader counting a silently-dropped row as a pass is reading a gap as a
        guarantee.
        """
        suite = test_removal_e2e.run_all(
            shapes=("skip_deselect",), paths=("workspace_write",)
        )
        assert suite.not_applicable, "the write funnel cannot express a deselect"
        for row in suite.not_applicable:
            assert row.applicable is False
            assert "NOT APPLICABLE" in row.reason
            assert json.loads(json.dumps(row.to_dict()))["applicable"] is False

    def test_the_skip_decision_is_published_and_the_agent_is_told(self):
        assert test_guard.SKIP_DECISION == "skip_is_deletion"
        assert test_guard.SKIP_DECISION_REASON
        assert test_guard.SKIP_DECISION_CONSEQUENCE


# ==========================================================================
# 10. The weakening demonstrations — executable, not transcripts
# ==========================================================================


class TestTheWeakeningIsDetectable:
    """Weaken the product on an ISOLATED COPY, import it, and show the pin reds.

    A copy rather than an in-place edit because four terminals share this tree,
    and it **raises if the literal is absent**, because an edit matching nothing
    would import an unchanged copy and report the opposite of the finding.
    """

    @staticmethod
    def _weakened_copy(source_name: str, old: str, new: str, module_name: str):
        """Import an ISOLATED, WEAKENED copy of one module under a new name.

        A copy rather than an in-place edit because four terminals share this
        tree. It **raises if the literal is absent**, because an edit matching
        nothing would import an unchanged copy and report the opposite of the
        finding — which is the failure this demonstration exists to avoid.

        ``importlib.import_module``, not ``__import__``. The builtin returns the
        TOP-LEVEL package for a dotted name, so every demonstration reported
        ``module has no attribute 'assess_strength'`` against a copy that had
        imported perfectly well.
        """
        import importlib

        original = (_REPO_ROOT / "execution" / source_name).read_text(encoding="utf-8")
        if old not in original:
            raise AssertionError(
                f"the literal {old!r} is no longer in {source_name}; the "
                f"demonstration would import an UNCHANGED copy and report the "
                f"opposite of the finding"
            )
        patched = original.replace(old, new, 1)
        root = Path(tempfile.mkdtemp(prefix="t2w2-weaken-"))
        package = root / module_name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / source_name).write_text(patched, encoding="utf-8")
        sys.path.insert(0, str(root))
        try:
            return importlib.import_module("%s.%s" % (module_name, source_name[:-3]))
        finally:
            sys.path.remove(str(root))

    def test_removing_the_untrustworthy_state_makes_the_tally_refuse(self):
        module = self._weakened_copy(
            "e2e_trust.py",
            '        e2e_verify.E2E_UNTRUSTWORTHY,\n    )',
            "    )",
            "weakened_no_fifth",
        )
        with pytest.raises(AssertionError):
            module.assert_five_states_distinct()

    def test_removing_the_CONTAINER_floor_arm_makes_a_stub_look_plausible(self):
        """The floor and the structural arm are independent.

        Removing the STRUCTURAL arm leaves the timing arm, and removing the
        timing arm leaves the structural one. Either alone still catches the
        stub; removing BOTH does not, which is why both are pinned separately.
        """
        module = self._weakened_copy(
            "e2e_trust.py",
            "    if did_run and not str(container_name or \"\").strip():\n"
            "        reasons.append(NO_CONTAINER_REASON)",
            "    if False:\n        reasons.append(NO_CONTAINER_REASON)",
            "weakened_no_structural",
        )
        check = E2ECheck(id="c", command="python x.py", feature="f")
        # With the structural arm gone and a duration ABOVE the floor, the stub
        # now reads as plausible -- which is exactly what the two arms are for.
        verdict = module.assess_fast_pass(
            check, duration_s=5.0, container_name="", ran=True
        )
        assert verdict.suspicious is False

    def test_removing_the_assertion_check_makes_a_weak_check_STRONG(self):
        module = self._weakened_copy(
            "e2e_trust.py",
            "        if scanned and asserted == 0:",
            "        if False:",
            "weakened_no_assertion",
        )
        repo = _tmp_repo("weakened")
        verdict = module.assess_strength(
            E2ECheck(id="w", command="python e2e_weak.py", feature="checkout"),
            repo_path=str(repo),
            exit_code=0,
            raw_output="=== 1 passed in 0.3s ===",
            container_name="hexec-e0-p1-w9",
            ran=True,
        )
        assert verdict.strength == "strong", (
            "removing the assertion check is what lets row 4 through, so the "
            "weakened copy MUST report the weak check as strong"
        )


# ==========================================================================
# 11. Structural checks over the new modules
# ==========================================================================


class TestTheNewModulesFollowTheConventions:
    NEW_MODULES = (
        "e2e_trust.py",
        "e2e_evidence.py",
        "e2e_attack.py",
        "multi_session.py",
        "session_economics.py",
        "test_removal_e2e.py",
    )

    def test_every_new_module_declares_its_public_surface(self):
        for name in self.NEW_MODULES:
            source = (_REPO_ROOT / "execution" / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            exported = None
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "__all__" for t in node.targets
                ):
                    exported = {
                        item.value for item in node.value.elts if isinstance(item, ast.Constant)
                    }
            assert exported, f"{name} has no __all__"
            source_text = (_REPO_ROOT / "execution" / name).read_text(encoding="utf-8")
            missing = [item for item in exported if item not in source_text]
            assert not missing, (name, missing[:5])

    def test_no_new_module_spawns_a_subprocess_without_a_classification(self):
        """Wave 1's rule: every spawn site in execution/ carries a verdict row.

        The new modules either spawn nothing (and delegate to
        `execution.sandbox`, already classified) or spawn a child and are named
        in the classification table. A new spawn that is not classified is a
        review gap.

        The table is READ BY IMPORT rather than by scanning the file's text. The
        text scan found a plausible-looking set of module names that did not
        include the three rows this round added, and reported them as
        unclassified — a check that cannot see the authority is not a check.
        """
        from execution.test_p2w1_trust_pins import SUBPROCESS_CLASSIFICATION

        classified = set(SUBPROCESS_CLASSIFICATION)
        for name in self.NEW_MODULES:
            source = (_REPO_ROOT / "execution" / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            spawns = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "Popen", "call", "check_output")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ]
            if not spawns:
                continue
            assert name in classified, (
                f"{name} spawns {len(spawns)} subprocess(es) and carries no row in "
                f"execution/test_p2w1_trust_pins.py::SUBPROCESS_CLASSIFICATION"
            )

    def test_no_new_module_writes_outside_execution(self):
        """The scope boundary, asserted by source rather than by trust."""
        for name in self.NEW_MODULES:
            source = (_REPO_ROOT / "execution" / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert "logs/evals" not in stripped or "report" in stripped, (
                    f"{name}: a hard-coded write outside execution/"
                )

    def test_every_new_module_has_a_docstring_naming_its_scope(self):
        for name in self.NEW_MODULES:
            module = __import__("execution.%s" % name[:-3])
            doc = (module.__doc__ or "").strip()
            assert len(doc) > 400, f"{name}'s docstring is {len(doc)} chars"
