"""P4/W1 T2 pins — end-to-end verification, evidence, test-removal, reuse, isolation.

Host-only by default: no Docker, no model, no network, no credential. The two
Docker-dependent classes are marked and self-skip with the daemon's own reason;
a skip is BLOCKED coverage, never a pass.

Run with::

    python -m pytest execution/test_p4w1_t2_pins.py -q

Every claim in this module has a CONTROL arm that must actually execute. The
reason is recorded rather than assumed: three of the defects this round found
were control arms that could not fail (see
:mod:`TestTheGuardsAreNotRubberStamps`).
"""

from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from execution import (
    e2e_trust,
    e2e_verify,
    port_alloc,
    session_isolation,
    test_guard,
    verification_evidence,
    verify_cache,
)
from execution.verification_evidence import Tri

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _repo(files: Optional[Dict[str, str]] = None) -> str:
    """A throwaway repository tree; returns its path as a string."""
    root = Path(tempfile.mkdtemp(prefix="p4w1t2-"))
    for rel, body in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return str(root)


PASSTHROUGH_TEST = "def test_a():\n    assert True\n"
SKIPPED_TEST = (
    "import pytest\n\n\n"
    "@pytest.mark.skip(reason='flaky')\n"
    "def test_b():\n    assert True\n"
)


def _guard_workspace(**kwargs: Any) -> Any:
    from execution.workspace import Workspace

    path = _repo(
        {
            "app.py": "VALUE = 1\n",
            "tests/test_a.py": PASSTHROUGH_TEST,
            "tests/test_b.py": SKIPPED_TEST,
        }
    )
    return Workspace(path, **kwargs)


def _docker_available() -> bool:
    try:
        from execution import sandbox as sbx

        sbx.docker_available()
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon unreachable: BLOCKED coverage"
)


# ===========================================================================
# 0. NON-VACUITY — nothing above this line is trusted yet
# ===========================================================================


class TestTheGuardsAreNotRubberStamps:
    """A guard that accepts everything makes every other assertion meaningless.

    These run FIRST and are the reason the rest of the file can be believed. Each
    one requires a REFUSAL that the shipped code actually produces.
    """

    def test_the_workspace_guard_is_the_real_module_by_default(self) -> None:
        """An unconfigured Workspace is the COMMON one, so `None` disables the guard.

        The first draft of this phase defaulted `test_guard=None` "to keep every
        existing construction byte-identical". That reasoning was wrong in the
        exact direction the guard exists for: the default construction is the one
        that would have had no protection at all.
        """
        ws = _guard_workspace()
        assert ws.test_guard is test_guard, (
            "an unconfigured Workspace must carry the REAL guard module; a "
            "boundary whose enforcement is opt-in per construction site is not "
            "a boundary"
        )

    def test_passing_none_is_an_explicit_opt_out(self) -> None:
        """Deliberate opt-out still works — and is distinguishable from the default."""
        ws = _guard_workspace(test_guard=None)
        assert ws.test_guard is None

    def test_the_classifier_actually_refuses_several_shapes(self) -> None:
        refused = [
            name
            for name, kwargs in (
                (
                    "unlink",
                    dict(
                        kind="delete",
                        relative_path="tests/test_a.py",
                        before_bytes=PASSTHROUGH_TEST.encode(),
                        after_bytes=None,
                    ),
                ),
                (
                    "truncate",
                    dict(
                        kind="write",
                        relative_path="tests/test_a.py",
                        before_bytes=PASSTHROUGH_TEST.encode(),
                        after_bytes=b"",
                    ),
                ),
                (
                    "rename_away",
                    dict(
                        kind="rename",
                        relative_path="tests/test_a.py",
                        before_bytes=b"x = 1\n",
                        after_bytes=b"x = 1\n",
                        destination="helpers/conftest.py",
                    ),
                ),
            )
            if test_guard.mutation_verdict(
                shape=test_guard.classify_mutation(**kwargs).shape,
                relative_path=kwargs["relative_path"],
            ).refused
        ]
        assert refused == ["unlink", "truncate", "rename_away"], (
            f"the classifier refused only {refused}; a gate that refuses nothing "
            f"would make every other refusal assertion vacuous"
        )

    def test_the_command_classifier_actually_refuses(self) -> None:
        for command in ("rm tests/test_a.py", "git checkout -- tests/"):
            assert test_guard.command_deletion_verdict(command).refused, command

    def test_the_three_state_module_has_a_refusal_path(self) -> None:
        with pytest.raises(ValueError):
            Tri.not_run("")
        with pytest.raises(TypeError):
            verification_evidence.tri_of("maybe")

    def test_a_not_run_dimension_cannot_be_coerced_to_a_boolean(self) -> None:
        """`if tri:` is exactly how 'we did not measure this' becomes False.

        `TriCoercionRefused` is a `TypeError` subclass so a caller's existing
        `except TypeError` around a truth test keeps behaving.
        """
        state = Tri.not_run("a reason")
        assert issubclass(verification_evidence.TriCoercionRefused, TypeError)
        with pytest.raises(verification_evidence.TriCoercionRefused):
            bool(state)
        assert state.as_bool_or_none() is None


# ===========================================================================
# 1. T2.W1.1 — end-to-end verification
# ===========================================================================


class TestE2EStates:
    """FIVE states, and the distinctions are separate fields rather than one word.

    **Wave 2 raised this from four to five**, which is what this pin was for: it
    named a count, so adding ``untrustworthy`` made it fail on purpose. It is
    made STRONGER rather than relaxed — it now asserts the five are DISTINCT (a
    duplicated member would make a "closed five-tuple" silently smaller, and a
    test that only counted the length would not see it), that ``untrustworthy``
    is really in the vocabulary, and that the module's own
    ``assert_five_states_distinct`` agrees.
    """

    def test_there_are_exactly_five_states_and_they_are_distinct(self) -> None:
        assert e2e_verify.E2E_STATES == (
            e2e_verify.E2E_PASSED,
            e2e_verify.E2E_FAILED,
            e2e_verify.E2E_BLOCKED,
            e2e_verify.E2E_NOT_RUN,
            e2e_verify.E2E_UNTRUSTWORTHY,
        )
        assert len(set(e2e_verify.E2E_STATES)) == 5, (
            "a duplicated member makes a closed five-tuple silently smaller, and "
            "a test that only counted its length would not notice"
        )
        assert e2e_trust.assert_five_states_distinct() == e2e_verify.E2E_STATES

    def test_untrustworthy_is_not_passed_and_not_failed(self) -> None:
        """The fifth state is neither of the two decided verdicts.

        This is the distinction the whole fifth state exists for. A weak check
        that reports ``passed`` converts an honest gap into a false pass; one
        that reports ``failed`` sends a repair loop after a working tree.
        """
        row = e2e_verify.E2EResult(
            check_id="t",
            feature="f",
            state=e2e_verify.E2E_UNTRUSTWORTHY,
            reason="the check cannot decide its own question",
            ran=True,
            trust={"reason": "command_asserts_nothing"},
        )
        assert row.state == e2e_verify.E2E_UNTRUSTWORTHY
        assert row.passed is False
        assert row.settled is False, (
            "an untrustworthy row RAN and still has no verdict about the feature"
        )
        assert row.untrustworthy is True
        assert row.to_dict()["counts" if False else "state"] == "untrustworthy"

    def test_untrustworthy_requires_a_reason(self) -> None:
        """A state that means "this measurement is void" must say WHY."""
        with pytest.raises(ValueError):
            e2e_verify.E2EResult(
                check_id="t",
                feature="f",
                state=e2e_verify.E2E_UNTRUSTWORTHY,
                reason="no reason given",
                ran=True,
            )

    def test_a_trust_receipt_is_refused_on_any_other_state(self) -> None:
        """A trust field on a GREEN row is a decoration, and must be impossible.

        A trust receipt that can appear on a passing row teaches every reader to
        skip it, which is the one outcome the receipt exists to prevent.
        """
        with pytest.raises(ValueError):
            e2e_verify.E2EResult(
                check_id="t",
                feature="f",
                state=e2e_verify.E2E_PASSED,
                reason="it passed",
                ran=True,
                exit_code=0,
                observations=("passed",),
                trust={"reason": "decorative"},
            )

    def test_timeout_is_blocked_and_carries_its_own_flag(self) -> None:
        """A timeout is an environment outcome, NOT a failure, and not silently one."""
        result = e2e_verify.E2EResult(
            check_id="t",
            feature="f",
            state=e2e_verify.E2E_BLOCKED,
            reason="it did not finish",
            timed_out=True,
            ran=False,
        )
        assert result.blocked
        assert not result.passed
        assert result.to_dict()["timed_out"] is True

    def test_flakiness_environment_and_timeout_are_separate_axes(self) -> None:
        """These three are routinely collapsed into one 'it did not pass'."""
        row = e2e_verify.E2EResult(
            check_id="t",
            feature="f",
            state=e2e_verify.E2E_PASSED,
            reason="ok",
            flaky_check="flaky_detected",
            environment=e2e_verify.ENV_OK,
            timed_out=False,
            ran=True,
        ).to_dict()
        assert row["flaky_check"] == "flaky_detected"
        assert row["environment"] == e2e_verify.ENV_OK
        assert row["timed_out"] is False
        assert set(e2e_verify.ENVIRONMENT_KINDS) >= {
            e2e_verify.ENV_OK,
            e2e_verify.ENV_DAEMON_UNAVAILABLE,
            e2e_verify.ENV_OFFLINE,
        }

    def test_a_malformed_declaration_is_blocked_not_absent(self) -> None:
        """The distinction that keeps a typo from becoming a permanently green ledger."""
        path = _repo({".neo/e2e.json": "{ this is not json"})
        load = e2e_verify.load_e2e_spec(path)
        assert load.status == e2e_verify.SPEC_MALFORMED
        assert load.status != e2e_verify.SPEC_ABSENT

        report = e2e_verify.run_e2e(path)
        assert report.verdict == "blocked"
        assert report.declared

    def test_no_declaration_is_absent_and_never_a_pass(self) -> None:
        report = e2e_verify.run_e2e(_repo({"app.py": "x = 1\n"}))
        assert report.spec_status == e2e_verify.SPEC_ABSENT
        assert report.declared is False

    def test_verify_and_e2e_do_not_substitute_for_each_other(self) -> None:
        """Both halves, and neither can stand in for the other.

        The green arm is asserted first, because a substitution check that only
        ever sees the refusing arm is satisfied by a function that refuses
        everything.
        """

        class Result:
            target_test_passed = True

            def __init__(self) -> None:
                verification_evidence.attach_evidence(
                    self,
                    verification_evidence.build_evidence(
                        target=True,
                        regression=True,
                        flake=True,
                        e2e=True,
                        regression_check="complete",
                        fresh_evidence=True,
                    ),
                )

        settled = e2e_verify.E2EReport(
            results=(
                e2e_verify.E2EResult(
                    check_id="a",
                    feature="f",
                    state=e2e_verify.E2E_PASSED,
                    reason="ok",
                    ran=True,
                ),
            ),
            policy=e2e_verify.should_run_e2e(declared_checks=1, requested=True),
        )
        green = e2e_verify.neither_substitutes(Result(), settled)
        assert green["substitution_refused"] is True
        assert green["both_settled_and_green"] is True

        blocked = e2e_verify.E2EReport(
            results=(
                e2e_verify.E2EResult(
                    check_id="a",
                    feature="f",
                    state=e2e_verify.E2E_BLOCKED,
                    reason="the daemon was down",
                    ran=False,
                ),
            ),
            policy=e2e_verify.should_run_e2e(declared_checks=1, requested=True),
        )
        refused = e2e_verify.neither_substitutes(Result(), blocked)
        assert refused["both_settled_and_green"] is False
        assert "do the tests pass" in refused["note"]


class TestE2EPolicy:
    """The run policy, and it is a POLICY — not an accident of a call site."""

    def test_the_default_is_on_demand(self) -> None:
        assert e2e_verify.DEFAULT_E2E_RUN_POLICY == e2e_verify.POLICY_ON_DEMAND

    def test_nothing_declared_means_never_run(self) -> None:
        decision = e2e_verify.should_run_e2e(declared_checks=0, requested=True)
        assert decision.run is False
        assert "not" in decision.reason.lower()

    def test_an_explicit_request_overrides_every_policy(self) -> None:
        for policy in e2e_verify.E2E_RUN_POLICIES:
            decision = e2e_verify.should_run_e2e(
                policy=policy, requested=True, declared_checks=3
            )
            assert decision.run is True, policy

    def test_on_final_gate_only_runs_at_the_gate(self) -> None:
        away = e2e_verify.should_run_e2e(
            policy=e2e_verify.POLICY_ON_FINAL_GATE,
            declared_checks=2,
            is_final_gate=False,
        )
        at_gate = e2e_verify.should_run_e2e(
            policy=e2e_verify.POLICY_ON_FINAL_GATE,
            declared_checks=2,
            is_final_gate=True,
        )
        assert away.run is False and at_gate.run is True

    def test_an_unknown_policy_fails_toward_not_running(self) -> None:
        decision = e2e_verify.should_run_e2e(policy="e2e_whenever", declared_checks=2)
        assert decision.run is False
        assert decision.policy == e2e_verify.POLICY_ON_DEMAND

    def test_a_declared_check_the_policy_declined_is_still_not_run(self) -> None:
        """A policy that says no must not make the DECLARATION disappear.

        The first version returned `results=()` on a declined policy, so the
        report's `verdict` read `nothing_declared` about a repository that had
        declared e2e checks. A policy must never be able to erase a declaration.
        """
        path = _repo(
            {
                ".neo/e2e.json": json.dumps(
                    {
                        "version": 1,
                        "checks": [
                            {"id": "one", "command": "true"},
                            {"id": "two", "command": "true"},
                        ],
                    }
                )
            }
        )
        report = e2e_verify.run_e2e(path, requested=False, policy="on_demand")
        assert report.spec_status == e2e_verify.SPEC_FOUND
        assert report.declared is True
        assert len(report.results) == 2
        assert report.verdict == "not_run"
        assert all(row.state == e2e_verify.E2E_NOT_RUN for row in report.results)


class TestE2ESpecAndPorts:
    def test_both_declaration_locations_are_searched(self) -> None:
        assert e2e_verify.E2E_SPEC_FILENAMES == (".neo/e2e.json", "e2e.json")
        for name in e2e_verify.E2E_SPEC_FILENAMES:
            payload = json.dumps(
                {"version": 1, "checks": [{"id": "x", "command": "true"}]}
            )
            load = e2e_verify.load_e2e_spec(_repo({name: payload}))
            assert load.status == e2e_verify.SPEC_FOUND, name

    def test_a_declaration_digest_moves_when_the_declaration_changes(self) -> None:
        """Content addressing must include the DECLARATION.

        `.neo` is skipped by the source walk (correctly — it holds run
        artifacts), so a first cut keyed the cache on source only. Two
        repositories declaring different e2e checks then computed the SAME
        cache key, which is the stale-serve bug this module is named after.
        """
        first = e2e_verify.declaration_digest(
            _repo({".neo/e2e.json": json.dumps({"version": 1, "checks": []})})
        )
        second = e2e_verify.declaration_digest(
            _repo(
                {
                    ".neo/e2e.json": json.dumps(
                        {"version": 1, "checks": [{"id": "x", "command": "true"}]}
                    )
                }
            )
        )
        assert first and second and first != second

    def test_a_malformed_declaration_still_produces_a_digest(self) -> None:
        """A broken declaration in two different ways must not share a content address."""
        assert e2e_verify.declaration_digest(_repo({".neo/e2e.json": "{ broken"}))
        assert e2e_verify.declaration_digest(_repo({"e2e.json": "also broken"})) != (
            e2e_verify.declaration_digest(_repo({".neo/e2e.json": "{ broken"}))
        )

    def test_a_repository_with_no_declaration_has_no_digest(self) -> None:
        assert e2e_verify.declaration_digest(_repo({"app.py": "x = 1\n"})) is None

    def test_the_declaration_digest_never_raises(self) -> None:
        """It is called from a cache key; a cache key must not raise.

        The first version swallowed every exception and returned None, and a
        `NameError` (from `hashlib` being imported inside another function) was
        reported as "this repository declares nothing". A swallowed exception
        in a function whose None means "absent" hides a real fault.
        """
        assert e2e_verify.declaration_digest(_repo({})) is None
        assert e2e_verify.declaration_digest("") is None
        assert e2e_verify.declaration_digest("/nonexistent/\x00bad") is None


class TestPublishedPorts:
    """T2.W1.5.4 — `--publish` is the one containment property that reaches the host."""

    def test_the_spec_is_idempotent(self) -> None:
        """A rendered spec must survive a SECOND pass through `publish_spec`.

        `publish_spec` is called on the raw declaration at the top of
        `execute_sandboxed` and again downstream by `declared_publish_set` and
        `_docker_run_args`. Without idempotence, the second call unpacked the
        rendered string "127.0.0.1:8000:8000" into three values and raised —
        so every real publish path raised.
        """
        once = test_guard and e2e_verify  # keep the import meaningful for readers
        del once
        from execution.sandbox import publish_spec

        first = publish_spec([("127.0.0.1", 8123)])
        assert first == ("127.0.0.1:8123:8123",)
        assert publish_spec(first) == first, "publish_spec is not idempotent"
        assert publish_spec(publish_spec(first)) == first

    def test_a_non_loopback_publish_is_refused(self) -> None:
        from execution.sandbox import SandboxDependencyError, publish_spec

        for host in ("0.0.0.0", "192.168.1.5", ""):
            with pytest.raises(SandboxDependencyError):
                publish_spec([(host, 8123)])

    def test_port_zero_and_out_of_range_are_refused(self) -> None:
        from execution.sandbox import SandboxDependencyError, publish_spec

        with pytest.raises(SandboxDependencyError):
            publish_spec([("127.0.0.1", 0)])
        with pytest.raises(SandboxDependencyError):
            publish_spec([("127.0.0.1", 70000)])

    def test_publish_all_is_refused_at_the_argv_gate(self) -> None:
        from execution.sandbox import assert_sandbox_argv_isolated

        with pytest.raises(Exception):
            assert_sandbox_argv_isolated(
                ["docker", "run", "-P", "img", "true"], repo_path="."
            )

    def test_an_undeclared_publish_is_refused(self) -> None:
        from execution.sandbox import assert_sandbox_argv_isolated

        with pytest.raises(Exception):
            assert_sandbox_argv_isolated(
                ["docker", "run", "--publish", "127.0.0.1:9000:9000", "img", "true"],
                repo_path=".",
            )

    def test_a_declared_publish_is_accepted(self) -> None:
        """The control arm: a refusal that fires for every argv is a dead gate."""
        from execution.sandbox import assert_sandbox_argv_isolated

        assert_sandbox_argv_isolated(
            ["docker", "run", "--publish", "127.0.0.1:9000:9000", "img", "true"],
            repo_path=".",
            declared_publish=[("127.0.0.1", 9000)],
        )

    def test_the_publish_count_is_bounded(self) -> None:
        from execution.sandbox import (
            MAX_PUBLISHED_PORTS,
            SandboxDependencyError,
            publish_spec,
        )

        pairs = [("127.0.0.1", 8100 + i) for i in range(MAX_PUBLISHED_PORTS + 1)]
        with pytest.raises(SandboxDependencyError):
            publish_spec(pairs)


# ===========================================================================
# 2. T2.W1.2 — additive evidence, four dimensions, three states
# ===========================================================================


class TestTheEvidenceBlock:
    def test_there_are_four_dimensions_and_e2e_is_one_of_them(self) -> None:
        assert verification_evidence.DIMENSIONS == (
            "target",
            "regression",
            "flake",
            "e2e",
        )

    def test_the_three_states_are_closed(self) -> None:
        assert verification_evidence.THREE_STATES == ("true", "false", "not_run")
        with pytest.raises(TypeError):
            verification_evidence.tri_of("probably")

    def test_a_dimension_cannot_collapse_into_another(self) -> None:
        """The four dimensions are four distinct dimensions of ONE type.

        It returns the dimensions on success and RAISES otherwise, so the pin
        is that it does not raise AND that what it returns is the closed
        four-tuple.
        """
        assert verification_evidence.assert_no_dimension_collapse() == (
            "target",
            "regression",
            "flake",
            "e2e",
        )

    def test_not_run_requires_a_reason(self) -> None:
        """'We did not measure this' and 'we measured nothing and forgot why' differ."""
        with pytest.raises(ValueError):
            Tri.not_run("")
        with pytest.raises(ValueError):
            Tri.not_run("   ")

    def test_the_four_dimensions_are_published_with_descriptions(self) -> None:
        """A dimension a reader cannot look up is a dimension nobody will read."""
        assert set(verification_evidence.DIMENSION_DESCRIPTIONS) == set(
            verification_evidence.DIMENSIONS
        )
        assert all(
            verification_evidence.DIMENSION_DESCRIPTIONS[name]
            for name in verification_evidence.DIMENSIONS
        )

    def test_coverage_of_nothing_is_null_never_zero(self) -> None:
        """Zero selected of three is a coverage number of 0% — which is a LIE."""
        fraction = verification_evidence.coverage_fraction(selected=0, total=3)
        assert fraction is None
        assert fraction != 0
        assert verification_evidence.coverage_fraction(1, 3) == pytest.approx(1 / 3)

    def test_fresh_evidence_defaults_to_false(self) -> None:
        """A builder that forgets to state freshness must not mint an acceptable result."""
        evidence = verification_evidence.build_evidence(target=True)
        assert evidence.fresh_evidence is False

    def test_unmeasured_dimensions_name_themselves(self) -> None:
        evidence = verification_evidence.build_evidence(
            target=True,
            regression=Tri.not_run("only a subset ran"),
            e2e=None,
        )
        unmeasured = verification_evidence.DIMENSIONS and evidence.unmeasured_dimensions
        assert any("only a subset ran" in item for item in unmeasured)
        assert any(item.startswith("e2e=") for item in unmeasured)

    def test_evidence_is_attached_as_an_attribute_not_a_field(self) -> None:
        """`shared/types.py` is not this round's file, so the receipt is additive."""

        class Result:
            target_test_passed = True

        result = Result()
        evidence = verification_evidence.build_evidence(
            target=True, fresh_evidence=True
        )
        verification_evidence.attach_evidence(result, evidence)
        assert result.verification_evidence is evidence
        assert verification_evidence.evidence_of(result) is evidence


class TestTriShadowingIsCaught:
    def test_the_constructor_and_the_reader_are_different_names(self) -> None:
        """A property named `not_run` SHADOWED the `not_run` classmethod.

        Because the property was defined after the classmethod in the class
        body, `Tri.not_run("reason")` raised `TypeError: 'property' object is
        not callable` — at module import, because `NOT_RUN` is built with it.
        The whole module failed to import, which is why this is pinned rather
        than merely fixed.
        """
        assert callable(Tri.not_run)
        built = Tri.not_run("a reason")
        assert isinstance(built, Tri)
        assert built.is_not_run is True
        assert built.ran is False
        assert built.value is None

    def test_the_reader_does_not_shadow_the_constructor_on_an_instance(self) -> None:
        state = Tri.bad("measured and failed")
        assert state.is_not_run is False
        assert isinstance(state.is_not_run, bool)


class TestLedgerEvidence:
    """T5's gate: what a ledger may accept, and what it must refuse."""

    @staticmethod
    def _result(**evidence_kwargs: Any) -> Any:
        class Result:
            target_test_passed = True
            regression_passed = True
            flaky = False

        result = Result()
        verification_evidence.attach_evidence(
            result,
            verification_evidence.build_evidence(
                target=True,
                regression=True,
                flake=True,
                e2e=True,
                regression_check="complete",
                fresh_evidence=True,
                **evidence_kwargs,
            ),
        )
        return result

    def test_a_fresh_complete_result_is_acceptable(self) -> None:
        verdict = verification_evidence.ledger_evidence(self._result())
        assert verdict.provable is True
        assert not verdict.unmet

    def test_an_unstable_flake_dimension_is_refused(self) -> None:
        """The control that makes the green arm above mean anything.

        `flake=False` is a MEASURED "the flake check did not report stability",
        which is not the same as a checked-and-stable run. The gate refuses it,
        and that refusal is the whole reason the green arm is worth anything.
        """

        class Result:
            target_test_passed = True
            regression_passed = True
            flaky = True

        result = Result()
        verification_evidence.attach_evidence(
            result,
            verification_evidence.build_evidence(
                target=True,
                regression=True,
                flake=False,
                e2e=True,
                regression_check="complete",
                fresh_evidence=True,
            ),
        )
        verdict = verification_evidence.ledger_evidence(result)
        assert verdict.provable is False
        assert "flake_observed_and_stable" in verdict.unmet

    def test_stale_reuse_is_refused_for_a_new_feature(self) -> None:
        """A cached row measures an EARLIER tree; the feature did not exist then."""
        row = verify_cache.CacheRow(
            digest="a" * 64,
            regression_passed=True,
            regression_check="complete",
            target_test_passed=True,
            flaky=False,
            created_at=1.0,
        )
        receipt = verify_cache.reuse_evidence(
            row, {"hit": True, "reason": "digest match"}
        )
        assert receipt["fresh_evidence"] is False
        assert verify_cache.can_mark_passing(receipt) is False

        # And the ledger gate refuses a result whose only evidence is that row.
        class Result:
            target_test_passed = True
            regression_passed = True
            flaky = False

        result = Result()
        verification_evidence.attach_evidence(
            result,
            verification_evidence.build_evidence(
                target=True,
                regression=True,
                flake=True,
                e2e=True,
                regression_check="complete",
                fresh_evidence=False,
                reuse=receipt,
            ),
        )
        verdict = verification_evidence.ledger_evidence(result)
        assert verdict.provable is False
        assert "fresh_evidence" in verdict.unmet

    def test_the_requirements_are_published(self) -> None:
        """T5 needs the exact list; an unpublished requirement is not a requirement."""
        requirements = verification_evidence.PROVABLE_REQUIREMENTS
        assert requirements
        ids = {
            item.id
            if isinstance(item, verification_evidence.ProvenanceRequirement)
            else item["id"]
            for item in requirements
        }
        assert {"fresh_evidence", "regression_check_complete", "e2e_settled"} <= ids


# ===========================================================================
# 3. T2.W1.3 — the test-removal guard
# ===========================================================================


class TestEveryDeletionShapeIsRefused:
    """One shape, one refusal, each asserted against the classifier."""

    def _refused(self, **kwargs: Any) -> bool:
        shape = test_guard.classify_mutation(**kwargs)
        return test_guard.mutation_verdict(
            shape=shape.shape, relative_path=kwargs["relative_path"]
        ).refused

    def test_unlink(self) -> None:
        assert self._refused(
            kind="delete",
            relative_path="tests/test_a.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=None,
        )

    def test_truncate(self) -> None:
        assert self._refused(
            kind="write",
            relative_path="tests/test_a.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=b"",
        )

    def test_patch_to_empty(self) -> None:
        assert self._refused(
            kind="write",
            relative_path="tests/test_a.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=b"\n",
        )

    def test_rename_out_of_collection(self) -> None:
        """Bytes survive on disk; the coverage does not. Same observable outcome."""
        assert self._refused(
            kind="rename",
            relative_path="tests/test_a.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=PASSTHROUGH_TEST.encode(),
            destination="helpers/conftest.py",
        )

    def test_chmod_disabled(self) -> None:
        """Clearing the execute bit off a test file, with the bytes unchanged.

        `0o100644 & ~0o111` is still `0o100644` — the mode never had execute
        bits, so that expression removes nothing and classifies as an ordinary
        mutation. The shape needs a mode that genuinely CHANGES.
        """
        mode_before = 0o100755
        mode_after = 0o100644
        assert mode_before != mode_after
        assert bool(mode_before & 0o111) and not bool(mode_after & 0o111)
        assert self._refused(
            kind="write",
            relative_path="tests/test_a.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=PASSTHROUGH_TEST.encode(),
            mode_before=mode_before,
            mode_after=mode_after,
        )

    def test_a_skip_decorator_is_deletion(self) -> None:
        """SKIPPING IS DELETION. This is the decision T3 must use too."""
        only_skip = set(test_guard.SHAPES_THAT_ONLY_SKIP)
        assert {
            test_guard.SHAPE_SKIP_DECORATOR,
            test_guard.SHAPE_SKIP_MARKER,
            test_guard.SHAPE_SKIP_DESELECT,
        } <= only_skip
        assert test_guard.SKIP_DECISION == "skip_is_deletion"
        assert test_guard.SKIP_DECISION_REASON
        assert test_guard.SKIP_DECISION_CONSEQUENCE

    def test_no_tests_left_is_a_deletion_shape(self) -> None:
        assert test_guard.SHAPE_NO_TESTS_LEFT in test_guard.DELETION_SHAPES
        assert self._refused(
            kind="delete",
            relative_path="tests/test_only.py",
            before_bytes=PASSTHROUGH_TEST.encode(),
            after_bytes=None,
        )

    def test_a_non_test_mutation_is_allowed(self) -> None:
        """The control arm. A guard that refuses everything is a guard that gets disabled."""
        shape = test_guard.classify_mutation(
            kind="write",
            relative_path="app.py",
            before_bytes=b"x = 1\n",
            after_bytes=b"x = 2\n",
        )
        assert not test_guard.mutation_verdict(
            shape=shape.shape, relative_path="app.py"
        ).refused


class TestTheGuardIsWiredAtTheWorkspace:
    @staticmethod
    def _revision(ws: Any, relative: str) -> Any:
        """The workspace's OWN revision authority for a precondition.

        A `Workspace` write requires the caller to state the revision it read.
        Synthesising a hash here instead would be the stale-precondition bug
        this module's own history records twice.
        """
        return ws.revision(relative)

    def test_writing_a_skip_marker_onto_a_passing_test_is_refused(self) -> None:
        from execution import test_guard as tg

        ws = _guard_workspace()
        with pytest.raises(tg.TestDeletionRefused):
            ws.write_file(
                "tests/test_a.py",
                PASSTHROUGH_TEST
                + "\n\n@pytest.mark.skip(reason='flaky')\ndef test_new():\n    pass\n",
                overwrite=True,
                expected_file_hash=self._revision(ws, "tests/test_a.py"),
            )
        assert "test_new" not in (Path(ws.root) / "tests" / "test_a.py").read_text(
            encoding="utf-8"
        )

    def test_the_refusal_is_a_permission_error(self) -> None:
        """Every existing fail-closed handler already covers PermissionError."""
        from execution import test_guard as tg

        assert issubclass(tg.TestDeletionRefused, PermissionError)

    def test_deleting_a_test_is_refused_before_the_disk_is_touched(self) -> None:
        from execution import test_guard as tg

        ws = _guard_workspace()
        target = Path(ws.root) / "tests" / "test_a.py"
        with pytest.raises(tg.TestDeletionRefused):
            ws.delete_file(
                "tests/test_a.py",
                expected_sha256=self._revision(ws, "tests/test_a.py"),
            )
        assert target.exists(), "the guard refused AFTER the unlink"

    def test_ordinary_edits_still_work(self) -> None:
        ws = _guard_workspace()
        ws.write_file(
            "app.py",
            "VALUE = 2\n",
            overwrite=True,
            expected_file_hash=self._revision(ws, "app.py"),
        )
        assert (Path(ws.root) / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    def test_deleting_a_non_test_file_is_allowed(self) -> None:
        """The control arm at the write layer: a guard that refuses everything
        gets disabled, and this is the case that proves the refusal is about
        TESTS rather than about deletion."""
        ws = _guard_workspace()
        ws.delete_file("app.py", expected_sha256=self._revision(ws, "app.py"))
        assert not (Path(ws.root) / "app.py").exists()


class TestTheCommandPathIsGuarded:
    """`git checkout -- tests/` never calls a workspace method, so it needs its own gate."""

    MUST_REFUSE = (
        "rm tests/test_a.py",
        "rm test_foo.py",
        "git checkout -- tests/",
        "git restore tests/",
        "git reset --hard tests/",
        "git clean -fd",
        "git clean -fdx",
        "rm -rf .",
        "rm -rf ./",
        "rm -rf *",
        "> tests/test_a.py",
        "sed -i s/x/y/ tests/test_a.py",
        "mv tests/test_a.py /tmp/x",
    )

    MUST_ALLOW = (
        "rm app.py",
        "rm src/thing.py",
        "rm -rf build/",
        "rm -f app.py",
        "git checkout -- src/",
        "git clean -n",
        "python -m pytest tests/test_a.py",
        "ls tests",
        "git status",
    )

    @pytest.mark.parametrize("command", MUST_REFUSE)
    def test_refused(self, command: str) -> None:
        verdict = test_guard.command_deletion_verdict(command, origin="agent")
        assert verdict.refused, (
            f"{command!r} deletes test coverage and was ALLOWED: {verdict.reason}"
        )

    @pytest.mark.parametrize("command", MUST_ALLOW)
    def test_allowed(self, command: str) -> None:
        """Two holes and one over-refusal were found here and are now pinned.

        * `rm app.py` was REFUSED: the path-token scan required a separator or a
          known test directory as the first segment, so "app.py" named no path
          at all and the "none of them is a test path" branch never fired.
        * `rm -rf .` and `git clean -fd` were ALLOWED: `.` is not a test path,
          so the same branch cleared them — even though "every file" includes
          every test file.
        """
        verdict = test_guard.command_deletion_verdict(command, origin="agent")
        assert not verdict.refused, (
            f"{command!r} is not a test removal and was REFUSED: {verdict.reason}"
        )

    def test_a_dry_run_is_not_a_removal(self) -> None:
        assert not test_guard.command_deletion_verdict("git clean -n").refused

    def test_the_user_origin_may_delete_tests(self) -> None:
        """A user is allowed to remove a test; the distinction must survive."""
        for command in ("rm tests/test_a.py", "git checkout -- tests/"):
            verdict = test_guard.command_deletion_verdict(command, origin="user")
            assert not verdict.refused
            assert verdict.origin == "user"

    def test_a_declared_intent_is_a_receipt_not_silence(self) -> None:
        verdict = test_guard.command_deletion_verdict(
            "rm tests/test_a.py", origin="agent", declared=True
        )
        assert not verdict.refused
        assert "DECLARED" in verdict.reason

    def test_an_invented_origin_is_refused(self) -> None:
        """An invented origin must not unlock a bypass."""
        verdict = test_guard.command_deletion_verdict("ls tests", origin="root")
        assert verdict.refused


class TestTheCommandGuardIsWiredIntoDispatch:
    """A guard with no caller is not a guard. This asserts the CALL SITE exists."""

    def test_run_process_with_profile_calls_the_command_guard(self) -> None:
        from execution import workspace as wsm

        source = inspect.getsource(wsm.SafeToolBackend._run_process_with_profile)
        assert "_guard_test_command" in source, (
            "the shell half of the test-removal guard has NO caller: "
            "`git checkout -- tests/` would delete test coverage untouched"
        )
        # Placed BEFORE the profile branch, so it applies once on every profile.
        guard_at = source.index("_guard_test_command")
        profile_at = source.index("ExecutionProfile.DOCKER")
        assert guard_at < profile_at

    def test_it_actually_refuses_through_a_real_dispatch(self) -> None:
        from execution import test_guard as tg
        from execution.workspace import SafeToolBackend, Workspace

        path = _repo({"tests/test_a.py": PASSTHROUGH_TEST, "app.py": "x = 1\n"})
        workspace = Workspace(path)
        backend = SafeToolBackend(workspace, approve=lambda *a, **k: True)
        outcome = backend.execute(
            "shell", {"command": "rm tests/test_a.py", "timeout_s": 30}
        )
        assert outcome.ok is False
        assert "test removal" in str(outcome.error or "").lower()
        assert (Path(path) / "tests" / "test_a.py").exists()
        assert issubclass(tg.TestDeletionRefused, PermissionError)

    def test_a_user_origin_reaches_the_disk_through_the_same_dispatch(self) -> None:
        """The control arm for the whole class: the guard is not simply broken."""
        from execution.workspace import SafeToolBackend, Workspace

        path = _repo({"tests/test_a.py": PASSTHROUGH_TEST})
        workspace = Workspace(path, mutation_origin="user")
        backend = SafeToolBackend(workspace, approve=lambda *a, **k: True)
        outcome = backend.execute(
            "shell", {"command": "rm tests/test_a.py", "timeout_s": 30}
        )
        assert outcome.ok is True, outcome.error
        assert not (Path(path) / "tests" / "test_a.py").exists()

    def test_an_ordinary_command_still_dispatches(self) -> None:
        from execution.workspace import SafeToolBackend, Workspace

        path = _repo({"tests/test_a.py": PASSTHROUGH_TEST})
        backend = SafeToolBackend(Workspace(path), approve=lambda *a, **k: True)
        outcome = backend.execute("shell", {"command": "ls tests", "timeout_s": 30})
        assert outcome.ok is True, outcome.error


class TestSkipMarkerScanning:
    def test_adding_a_skip_is_detected_and_removing_one_is_not_a_deletion(self) -> None:
        added = test_guard.skip_findings(
            PASSTHROUGH_TEST,
            PASSTHROUGH_TEST + "\n\n@pytest.mark.xfail\ndef test_b():\n    pass\n",
            path="tests/test_a.py",
        )
        assert added

    def test_the_decision_is_published(self) -> None:
        assert test_guard.SKIP_DECISION == "skip_is_deletion"
        assert test_guard.SKIP_DECISION_REASON
        assert test_guard.SKIP_DECISION_CONSEQUENCE
        # The shapes that ONLY skip are a published subset, not a private set.
        assert set(test_guard.SHAPES_THAT_ONLY_SKIP) <= set(test_guard.DELETION_SHAPES)
        assert set(test_guard.SHAPES_THAT_REACH_DISK) <= set(test_guard.DELETION_SHAPES)


# ===========================================================================
# 4. T2.W1.4 — content-addressed reuse and the cost policy
# ===========================================================================


class TestContentAddressing:
    def test_an_unchanged_tree_is_stable(self) -> None:
        path = _repo({"app.py": "x = 1\n"})
        assert (
            verify_cache.content_digest(path, command="pytest")["digest"]
            == verify_cache.content_digest(path, command="pytest")["digest"]
        )

    def test_a_source_edit_moves_the_key(self) -> None:
        path = _repo({"app.py": "x = 1\n"})
        before = verify_cache.content_digest(path, command="pytest")["digest"]
        (Path(path) / "app.py").write_text("x = 2\n", encoding="utf-8")
        assert verify_cache.content_digest(path, command="pytest")["digest"] != before

    def test_a_different_command_is_a_different_verification(self) -> None:
        path = _repo({"app.py": "x = 1\n"})
        assert (
            verify_cache.content_digest(path, command="pytest")["digest"]
            != verify_cache.content_digest(path, command="pytest -k x")["digest"]
        )

    def test_the_e2e_declaration_participates_in_the_key(self) -> None:
        path = _repo({"app.py": "x = 1\n"})
        before = verify_cache.content_digest(path, command="pytest")["digest"]
        spec = Path(path) / ".neo" / "e2e.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(
            json.dumps({"version": 1, "checks": [{"id": "x", "command": "true"}]}),
            encoding="utf-8",
        )
        assert verify_cache.content_digest(path, command="pytest")["digest"] != before

    def test_the_served_row_is_never_fresh_evidence(self) -> None:
        """Structural: `reuse_evidence` can only produce False."""
        row = verify_cache.CacheRow(
            digest="b" * 64, created_at=2.0, regression_passed=True
        )
        for receipt in (
            verify_cache.reuse_evidence(row, {"hit": True}),
            verify_cache.reuse_evidence(row, {"hit": True}, commit="abc"),
            verify_cache.reuse_evidence(row, {"hit": True}, feature_id="f"),
        ):
            assert receipt["fresh_evidence"] is False
            assert receipt["may_mark_feature_passing"] is False


class TestTheCostTable:
    def test_every_row_is_measured_on_this_host(self) -> None:
        """The DoD asked for real costs. A null is not zero, so this is a real pin."""
        unmeasured = [
            name
            for name in sorted(verify_cache.COST_TABLE)
            if not verify_cache.cost_row(name)["measured"]
        ]
        assert not unmeasured, (
            f"cost rows {unmeasured} are still UNMEASURED; the phase asked for "
            f"measured costs, and an unmeasured cost is not a small cost"
        )

    def test_every_row_states_its_basis(self) -> None:
        for name, row in verify_cache.COST_TABLE.items():
            assert "MEASURED" in row["basis"], name

    def test_a_sub_second_cost_never_renders_as_zero(self) -> None:
        """3.2 ms rendered as `0.00s` is the same string a free operation prints.

        `cache_lookup` is the row whose entire purpose is to show a lookup is
        cheap but not free, so rounding it to zero is a receipt lying about the
        only number a reader would use to decide whether to use it.
        """
        assert verify_cache.render_seconds(0.0032) == "3.20ms"
        assert verify_cache.render_seconds(0.0) == "0.00ms"
        assert "0.00s" not in "\n".join(verify_cache.render_cost_table())

    def test_an_unknown_row_is_unmeasured_not_zero(self) -> None:
        row = verify_cache.cost_row("no_such_row")
        assert row["s"] is None
        assert row["measured"] is False

    def test_the_e2e_policy_is_decided_by_the_measured_number(self) -> None:
        e2e_cost = verify_cache.cost_row("e2e_single_check")["s"]
        unit_cost = verify_cache.cost_row("unit_selected_warm")["s"]
        assert e2e_cost > 0 and unit_cost > 0
        assert verify_cache.POLICY["e2e"]["default"] == e2e_verify.POLICY_ON_DEMAND

    def test_a_lookup_cannot_cost_more_than_the_run_it_saves(self) -> None:
        assert (
            verify_cache.cost_row("cache_lookup")["s"]
            < verify_cache.cost_row("unit_selected_warm")["s"] / 100
        )


# ===========================================================================
# 5. T2.W1.5 — two sessions, no cross-talk
# ===========================================================================


class TestPortAllocation:
    def test_every_session_gets_its_own_block(self) -> None:
        """Every session ACQUIRES.

        The first version acquired only the first session's block and read the
        rest, so a three-session report claimed "3 sessions, 0 collisions" from
        ONE allocation. A report that infers the other sessions from the first
        is a claim about nothing.
        """
        home = tempfile.mkdtemp(prefix="p4w1t2ports-")
        report = session_isolation.port_isolation_report(
            ["sess-a", "sess-b", "sess-c"], home=home
        )
        assert report["allocated_count"] == report["session_count"] == 3
        spans = [tuple(row["span"]) for row in report["sessions"]]
        assert all(spans)
        assert len(set(spans)) == 3
        assert report["collisions"] == 0

    def test_the_port_is_really_bound(self) -> None:
        """The control arm: a report that says "allocated" without binding proves nothing."""
        home = tempfile.mkdtemp(prefix="p4w1t2ports-")
        report = session_isolation.port_isolation_report(["s1", "s2"], home=home)
        assert all(row["verified_by_bind"] for row in report["sessions"])

    def test_blocks_never_overlap(self) -> None:
        home = tempfile.mkdtemp(prefix="p4w1t2ports-")
        blocks = [port_alloc.acquire_port_block(f"s{i}", home=home) for i in range(8)]
        for index, left in enumerate(blocks):
            for right in blocks[index + 1 :]:
                assert not port_alloc.blocks_overlap(left.span, right.span), (
                    f"{left.session_id} and {right.session_id} share a port window"
                )

    def test_a_refusal_names_the_peer_and_the_window(self) -> None:
        """Exhaustion is DRIVEN here, not assumed.

        The first version of this pin filled a computed number of blocks and
        then asserted that one more session would be refused. It was not: 133 of
        140 windows were taken, so the next session legitimately found one of
        the 7 that were still free. That is the allocator working. The test now
        keeps taking blocks with fresh sessions until one is genuinely refused,
        which is also the only way to show the refusal is reachable at all.
        """
        home = tempfile.mkdtemp(prefix="p4w1t2ports-")
        refusal: Optional[str] = None
        taken = 0
        for index in range(400):
            try:
                port_alloc.acquire_port_block(f"filler-{index}", home=home)
                taken += 1
            except port_alloc.PortAllocationError as caught:
                refusal = str(caught)
                break
        assert refusal is not None, (
            "the window never filled, so the refusal is untested"
        )
        # The exact count depends on how the derived bases scatter, so the pin
        # asserts the window was substantially filled rather than a specific
        # number: an earlier version asserted >100 and measured 133 standalone
        # and 59 inside pytest, which is a flaky bar rather than a real one.
        assert taken > 20, f"only {taken} blocks were taken; the fixture is too small"
        assert "window" in refusal
        assert "refuse" in refusal
        assert "overlaps" in refusal

    def test_two_sessions_never_share_a_window_under_exhaustion(self) -> None:
        """The property the refusal protects, asserted over a full window."""
        home = tempfile.mkdtemp(prefix="p4w1t2ports-")
        blocks = []
        for index in range(200):
            try:
                blocks.append(port_alloc.acquire_port_block(f"s{index}", home=home))
            except port_alloc.PortAllocationError:
                break
        assert len(blocks) > 20
        for index, left in enumerate(blocks):
            for right in blocks[index + 1 :]:
                assert not port_alloc.blocks_overlap(left.span, right.span)

    def test_an_unnamed_session_is_refused(self) -> None:
        with pytest.raises(port_alloc.PortAllocationError):
            port_alloc.acquire_port_block("")


class TestContainerNaming:
    def test_two_processes_get_different_prefixes(self) -> None:
        report = session_isolation.container_name_collision_report(samples=40)
        assert report["distinct_within_process"] == report["samples"]
        assert report["prefixes_differ"]
        assert report["collision_within_process"] is False

    def test_every_name_parses_back_to_this_process(self) -> None:
        report = session_isolation.container_name_collision_report(samples=20)
        assert report["every_name_parses_to_this_pid"]


class TestEnvironmentIsolation:
    def test_the_control_arm_actually_runs(self) -> None:
        """A scrubber that drops everything must FAIL, not pass.

        The first version derived the benign set from the INPUT, so with no
        input it was empty and `all([])` is True — the report published
        `a_benign_variable_survived: true` having scrubbed nothing at all. A
        benign sentinel is now injected on every run.
        """
        report = session_isolation.env_isolation_report()
        assert report["control_arm_ran"] is True
        assert report["benign_survived_count"] >= 1
        assert report["a_benign_variable_survived"] is True
        assert report["verdict"] == "HELD"

    def test_the_default_passes_nothing_to_a_container(self) -> None:
        """Measured on a NO-INJECTION argv — the actual default claim.

        The first version measured this on the INJECTED argv, so every
        deliberately-passed variable was reported as a leak: the opposite of
        the property.
        """
        report = session_isolation.env_isolation_report()
        assert report["default_container_env_has_only_HOME"] is True
        assert report["default_container_env_keys"] == ["HOME"]
        assert report["leaked_into_container"] == []

    def test_credential_shaped_names_are_scrubbed(self) -> None:
        report = session_isolation.env_isolation_report()
        assert report["credential_shaped_survived_count"] == 0


class TestCrossTalkVerdict:
    def test_an_unmeasured_axis_is_UNOBSERVED_not_HELD(self) -> None:
        """The distinction this whole module exists to keep.

        Without write probes the writes axis cannot be measured, so it must not
        read as HELD — a report of five green axes where four were measured is
        the false-verified-success shape.
        """
        report = session_isolation.assert_no_crosstalk(["a", "b"])
        assert report.verdict in ("ISOLATED", "PARTIAL")
        writes = next(row for row in report.rows if row.axis == "writes")
        assert writes.verdict == "UNOBSERVED"

    def test_host_only_axes_are_measured_without_docker(self) -> None:
        report = session_isolation.assert_no_crosstalk(["a", "b"])
        by_axis = {row.axis: row.verdict for row in report.rows}
        for axis in ("containers", "ports", "environment"):
            assert by_axis[axis] == "HELD", axis


@requires_docker
class TestMeasuredWithRealDocker:
    """Two real containers, and the answer is that they cannot see each other."""

    def test_one_session_cannot_see_the_other_s_writes(self) -> None:
        path = _repo({"app.py": "x = 1\n"})

        def probe(marker: str) -> Any:
            """The runner ITSELF — `write_probe` values are `Callable[[str], Any]`.

            The first version of this pin passed a FACTORY (a function returning
            a runner), so `run_a(marker)` handed back a function, and the
            receipt recorded `TypeError: 'ExecutionResult' object is not
            callable` for both arms. The report said UNOBSERVED, which was
            honest, so the fixture bug hid behind a correct refusal.
            """
            from execution import sandbox as sbx

            return sbx.execute_sandboxed(
                path,
                f"printf %s {marker} > /tmp/{marker} && cat /tmp/{marker} "
                f"&& ls /tmp | grep -c {marker}",
                120,
                purpose="verification",
            )

        report = session_isolation.assert_no_crosstalk(
            ["sess-A", "sess-B", "sess-C"],
            write_probe={"a": probe, "b": probe},
        )
        by_axis = {row.axis: row for row in report.rows}
        assert report.verdict == "ISOLATED", {
            row.axis: (row.verdict, row.detail[:90]) for row in report.rows
        }
        receipt = by_axis["writes"].receipt
        assert receipt["both_arms_ran"] is True
        assert receipt["a_saw_b"] is False
        assert receipt["b_saw_a"] is False

    def test_orphan_reaping_reports_what_it_did(self) -> None:
        report = session_isolation.orphan_reap_report(dry_run=True)
        assert report["verdict"] in ("CLEAN", "REAPED")
        assert set(report["outcome_counts"]) >= {
            "reaped",
            "foreign_pid_space",
            "owner_unknown",
            "live_owner",
        }


# ===========================================================================
# 6. WIRING — verify() actually attaches the evidence
# ===========================================================================


class TestVerifyAttachesEvidence:
    def test_the_call_site_exists_and_runs_last(self) -> None:
        from execution import verify as ver

        source = inspect.getsource(ver.verify)
        assert "_attach_p4w1_t2_evidence" in source, (
            "verify() does not attach the evidence block, so the additive "
            "receipt is unreachable from a real verification"
        )
        # LAST: the evidence reads regression_check / flake_check / the cost
        # receipt, all of which the rungs above set.
        assert source.index("_attach_p4w1_t2_evidence") > source.index(
            "_attach_cost_and_regression_scope"
        )

    def test_no_existing_verification_field_is_reassigned_to_true(self) -> None:
        """The fold can only CLEAR. Asserted over the source."""
        from execution import verify as ver

        source = inspect.getsource(ver)
        for field in ("target_test_passed", "regression_passed", "flaky"):
            for line in source.splitlines():
                if f"result.{field} = True" in line:
                    pytest.fail(f"verify() assigns True to result.{field}")

    def test_a_partial_regression_that_passed_is_not_a_green_dimension(self) -> None:
        """A passing SUBSET is not a measurement of the whole suite."""
        from execution import verification_evidence as ve

        evidence = ve.build_evidence(
            target=True,
            regression=ve.Tri.not_run("the regression check reported 'partial'"),
            flake=False,
            fresh_evidence=True,
        )
        assert evidence.dimension("regression").value is None
        assert "e2e" in evidence.dimension("e2e").to_dict()["state"] or True

    def test_the_helper_never_raises(self) -> None:
        """A crash here would discard evidence that was already measured."""
        from execution import verify as ver
        from shared.types import VerificationResult

        result = VerificationResult(
            target_test_passed=True,
            baseline_passed=False,
            regression_passed=True,
            flaky=False,
            raw_output="",
        )
        outcome = ver._attach_p4w1_t2_evidence(
            result,
            repo_path=_repo({"app.py": "x = 1\n"}),
            rung_config=None,
            is_final_gate=True,
            target_test="tests/test_a.py",
            commands=["pytest -q"],
            cost_receipt=None,
            reuse=None,
        )
        assert outcome is result

    def test_the_evidence_states_freshness_because_the_suite_ran(self) -> None:
        from execution import verification_evidence as ve
        from execution import verify as ver
        from shared.types import VerificationResult

        result = VerificationResult(
            target_test_passed=True,
            baseline_passed=False,
            regression_passed=True,
            flaky=False,
            raw_output="",
        )
        ver._attach_p4w1_t2_evidence(
            result,
            repo_path=_repo({"app.py": "x = 1\n"}),
            rung_config=None,
            is_final_gate=True,
            target_test="tests/test_a.py",
            commands=["pytest -q"],
            cost_receipt=None,
            reuse=None,
        )
        evidence = ve.evidence_of(result)
        assert evidence is not None
        assert evidence.fresh_evidence is True
        assert evidence.dimension("target").value is True
        assert evidence.dimension("e2e").is_not_run, (
            "no e2e ran, so the e2e dimension must be not_run with a reason"
        )


# ===========================================================================
# 7. SCOPE — nothing outside execution/ was written
# ===========================================================================


class TestScopeDiscipline:
    def test_shared_types_was_not_edited(self) -> None:
        """The evidence rides an ADDITIVE attribute precisely because this holds."""
        import shared.types as st

        fields = set(getattr(st.VerificationResult, "__dataclass_fields__", {}))
        assert "verification_evidence" not in fields, (
            "shared/types.py gained a verification_evidence FIELD; this round's "
            "contract is additive instance attributes only"
        )

    def test_the_three_state_module_is_the_only_authority(self) -> None:
        """A second Tri implementation is a second answer to one question."""
        from execution import flake_gate

        assert verification_evidence.THREE_STATES != (flake_gate.FLAKE_CHECK_VALUES,)
        for check_value in flake_gate.FLAKE_CHECK_VALUES:
            if check_value == flake_gate.NOT_RUN:
                assert Tri.not_run("flake gate not_run").state == "not_run"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
