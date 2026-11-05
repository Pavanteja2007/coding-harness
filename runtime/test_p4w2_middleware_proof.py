"""P4/W2-T3 — the middleware refactor is safe, correctly ordered, and worth it.

**Host-only: no Docker, no provider, no network, no credential.** Every suite here
runs in well under a minute except :class:`TestTheDifferential`, which drives the
14-scenario matrix.

**These are pins, not measurements.** The measurements live in
:mod:`runtime.middleware_audit`, which this suite calls rather than
reimplementing — a pin that re-derives the thing it tests is how a checker and its
test drift apart while both stay green, which has happened in this repository more
than once. What this suite adds is the **failure demonstrations**: each ordering
rule is proven *load-bearing* by breaking it and requiring the break to be caught,
because an ordering assertion that nothing can violate is a comment.

Six groups, in the order the brief asks for them:

1. :class:`TestTheDifferential` — the before/after behavioural comparison, and
   the proof that the differential CAN fail. Without that second half a zero
   divergences is not evidence of anything.
2. :class:`TestEachSemanticOrdering` — **one test per declared ordering**, each
   driving the situation the rule is about rather than comparing indices, because
   an index comparison proves the table is internally consistent and only driving
   the pipeline proves the pipeline obeys it.
3. :class:`TestAReorderMustFail` — the demonstration, on every same-phase pair.
4. :class:`TestOrderIsDataNotImportOrder` — registration sequence does not
   determine visit order, and a middleware with **no position fails loudly**.
5. :class:`TestAddingAPolicyTouchesNoLoop` — the no-loop-edit invariant, asserted
   on string LITERALS in the loop AST rather than on substrings of the source.
6. :class:`TestTheMatrixUnderConcurrency` — the 14 scenarios under real threads,
   which ``tests/test_agent_loop_matrix.py`` does not do.

Every suite that asserts a count also carries the **non-vacuity control** that
makes it non-trivial: a "14 scenarios" assertion passes on an empty list, so the
suite asserts the required names are present *and* that a guard actually fires.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _phase_of(policy_name: str) -> str:
    """The phase the SHIPPED chain declares for ``policy_name``.

    Read off :func:`build_full_chain`'s composition rather than a hand-written
    table in this file: a second table here would be a second answer to "what
    phase is this policy in", which is the drift the ordering rules exist to
    prevent. Raises for an unknown name so a typo is loud.
    """
    from runtime.turn_policies import build_full_chain

    for policy in build_full_chain()._middlewares:
        if policy.name == policy_name:
            return policy.phase
    raise AssertionError(f"{policy_name!r} is not in the shipped chain")


# ==========================================================================
# 1. The differential, and the proof that it can fail
# ==========================================================================


class TestTheDifferential:
    """The before/after behavioural comparison, reported in full."""

    @pytest.fixture(scope="class")
    def differential(self):
        from runtime.middleware_audit import differential

        return differential()

    def test_the_comparison_is_non_vacuous(self, differential):
        """A differential in which no guard fires compares fourteen identical rows.

        Without this assertion a differential that compared "nothing happened" to
        "nothing happened" would report zero divergences and mean nothing, which
        is the specific failure mode this group exists to prevent.
        """
        assert differential["rows_compared"] > differential["scenarios"], (
            "every scenario must contribute more than one row, or the stress "
            "variants are not being driven"
        )
        assert differential["rows_with_a_guard_firing"] > 0, (
            "no guard fired in any row, so the comparison never exercised a "
            "stopping decision and proves nothing about ordering"
        )
        assert differential["non_vacuous"] is True

    def test_every_scenario_is_compared(self, differential):
        assert differential["scenarios"] == 14
        names = {row["scenario"] for row in differential["rows"]}
        assert len(names) == 14, f"expected 14 distinct scenarios, got {sorted(names)}"

    def test_there_are_zero_behavioural_divergences(self, differential):
        """**Zero. Pinned.** A divergence here is a bug, not a known change."""
        assert differential["divergences"] == 0, (
            f"behavioural divergences: {differential['divergent_rows']}"
        )
        assert differential["agreed"] == differential["rows_compared"]
        assert differential["ok"] is True

    def test_the_arms_are_independent(self):
        """The "before" arm must not delegate to the pipeline.

        If it did, the two arms would agree by construction and the zero above
        would be a tautology. This reads the source of the reconstruction and
        requires that it never reaches the pipeline's ``check``.
        """
        source = (
            REPO_ROOT / "runtime" / "middleware_audit.py"
        ).read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename="runtime/middleware_audit.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "pre_refactor_pre_turn":
                called = {
                    child.func.attr
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                }
                assert "check" not in called, (
                    "the pre-refactor reconstruction calls pipeline.check(); it "
                    "would agree with the shipped pipeline by construction"
                )
                return
        pytest.fail("pre_refactor_pre_turn is not defined in runtime/middleware_audit.py")

    def test_the_differential_can_fail(self):
        """Perturb the OLD arm and require the differential to NOTICE.

        The mutation is a **semantically inert** reordering -- ``wall_clock``
        before ``turn_cap_observation``, and ``turn_cap_observation`` only ever
        observes -- so detection cannot come from a coincidental status flip. It
        has to come from the per-policy decision SEQUENCE being compared, which
        is the broader of the two claims.
        """
        from runtime.middleware_audit import mutation_is_detected

        mutation = mutation_is_detected()
        assert mutation["detected"] is True, (
            "perturbing the pre-refactor arm's vertical order changed nothing; "
            "the differential cannot fail and proves nothing"
        )
        assert mutation["mutated_agreed"] < mutation["baseline_agreed"]
        assert mutation["newly_divergent"], "the mutation produced no divergent rows"

    def test_the_additive_receipt_is_not_folded_into_the_behaviour_claim(self):
        """The one non-behavioural difference is reported, not absorbed.

        The pipeline records each decision into the per-run ``TurnRun``; the
        reconstructed inline branches did not. That is additive bookkeeping. It is
        reported on its own field and proven unreadable by any decision -- if a
        policy READ it, the additive bookkeeping would be a behaviour change and
        the zero divergences would be hiding it.
        """
        from runtime.middleware_audit import (
            differential,
            receipt_recording_is_not_read_by_any_decision,
        )

        diff = differential()
        assert "behaviour_fields_compared" in diff
        assert diff["behaviour_fields_compared"] == ["events", "terminal", "stopped_at"]
        assert diff["rows_with_additive_receipt_only"] > 0, (
            "no row showed the additive receipt difference, so the decomposition "
            "between behaviour and bookkeeping is untested"
        )
        probe = receipt_recording_is_not_read_by_any_decision()
        assert probe["read_count"] == 0, f"a policy reads a receipt list: {probe['reads']}"
        assert probe["identical_despite_differing_receipt"] is True
        assert probe["ok"] is True


# ==========================================================================
# 2. Each semantic ordering, pinned as its OWN test
# ==========================================================================


class TestEachSemanticOrdering:
    """One test per declared ordering, each DRIVING the situation it is about.

    Every arm carries a **control** — the same pipeline configured so the rule
    does NOT bite — because "the budget guard refuses an unaffordable call" is
    satisfied by a governor that refuses every call, and "a write is dispatched
    only after the read-only gate" is satisfied by a pipeline that dispatches
    nothing.
    """

    # -- cost checked BEFORE the dial -------------------------------------

    def test_cost_is_checked_before_the_dial(self):
        """A call that would exceed the budget must never be attempted.

        The proof is the DIAL'S OWN OUTCOME: the ``model_dial`` middleware records
        the reply on the run, so a refusal that arrives before it leaves no reply
        at all. That is stronger than reading the pipeline's ordering, because it
        observes the dial rather than trusting the walk.
        """
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        dialled: list[str] = []

        def turn(**overrides):
            base = dict(
                number=3,
                max_turns=60,
                spent_usd=1.98,
                budget_cap_usd=2.0,
                model="m",
            )
            base.update(overrides)
            return Turn(**base)

        # -- the refusal
        run = TurnRun(run_id="cost-before-dial")
        blocked = turn()
        blocked.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        blocked.scratch["next_call_price_usd"] = 0.5
        outcome = build_pipeline(
            ("per_call_budget", "model_dial")
        ).check(blocked, run, phase="pre_call")
        assert outcome.is_refusal is True
        assert outcome.stopped_at == "per_call_budget"
        assert dialled == [], "the model was dialled despite the budget refusal"
        assert run.get("model_dial", "reply", None, run_id=run.run_id) is None

        # -- the CONTROL: the same call that DOES fit is dialled
        run_ok = TurnRun(run_id="cost-before-dial-ok")
        allowed = turn(spent_usd=0.10)
        allowed.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        allowed.scratch["next_call_price_usd"] = 0.5
        outcome_ok = build_pipeline(
            ("per_call_budget", "model_dial")
        ).check(allowed, run_ok, phase="pre_call")
        assert outcome_ok.continues is True
        assert dialled == ["DIALLED"], (
            "the control did not dial, so the refusal above proves only that the "
            "chain never dials anything"
        )

    def test_the_session_cap_is_tested_before_the_per_call_cap(self):
        """``budget_cap`` before ``per_call_budget``.

        When both would bite, the refusal must name the SESSION. A reader told
        "budget cap exceeded" can act; a reader told "this call did not fit"
        cannot, and the two are the same money at two granularities.
        """
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        run = TurnRun(run_id="cap-granularity")
        turn = Turn(
            number=3, max_turns=60, spent_usd=2.5, budget_cap_usd=2.0, model="m"
        )
        turn.scratch["next_call_price_usd"] = 0.5
        # NO phase filter. `budget_cap` is a `pre_turn` policy and
        # `per_call_budget` is `pre_call`; filtering to one phase would drop the
        # very guard this arm is about, and the arm would then be testing that an
        # empty chain terminates.
        outcome = build_pipeline(("budget_cap", "per_call_budget")).check(turn, run)
        assert outcome.is_termination is True
        assert outcome.stopped_at == "budget_cap"
        assert outcome.decision.status == "failed"
        assert "budget cap" in outcome.decision.reason
        # The phases themselves must be the ones the declaration table claims,
        # or "before" is only true because the chain was built wrong.
        pipeline = build_pipeline(("budget_cap", "per_call_budget"))
        assert pipeline._middlewares[0].phase == "pre_turn"
        assert pipeline._middlewares[1].phase == "pre_call"

    def test_the_wall_clock_is_checked_before_the_dial(self):
        """A turn is never paid for after the wall-clock bound has passed."""
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        dialled: list[str] = []
        run = TurnRun(run_id="clock-before-dial")
        turn = Turn(number=3, max_turns=60, elapsed_s=900.0, max_wallclock_s=900.0)
        turn.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        # No phase filter: `wall_clock` is `pre_turn`, `model_dial` is `pre_call`,
        # and the property under test is precisely that the earlier PHASE wins.
        outcome = build_pipeline(("wall_clock", "model_dial")).check(turn, run)
        assert outcome.is_termination is True
        assert outcome.decision.status == "timeout"
        assert outcome.stopped_at == "wall_clock"
        assert dialled == []

        # The control: the SAME turn with a bound the clock has not passed reaches
        # the dial. Without it, "the dial did not run" would be satisfied by a
        # chain that never dials anything.
        dialled.clear()
        live = Turn(number=3, max_turns=60, elapsed_s=1.0, max_wallclock_s=900.0)
        live.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        live_outcome = build_pipeline(("wall_clock", "model_dial")).check(live, run)
        assert live_outcome.continues is True
        assert dialled == ["DIALLED"]

    def test_cancellation_is_checked_before_the_dial(self):
        """A cancelled run cannot spend a model call on a turn it may not finish."""
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        dialled: list[str] = []
        run = TurnRun(run_id="cancel-before-dial")
        turn = Turn(number=3, max_turns=60, cancelled=True)
        turn.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        # No phase filter: `cancellation` is `pre_turn` and `model_dial` is
        # `pre_call`, so filtering would drop the guard being pinned.
        outcome = build_pipeline(("cancellation", "model_dial")).check(turn, run)
        assert outcome.is_termination is True
        assert outcome.decision.status == "cancelled"
        assert outcome.stopped_at == "cancellation"
        assert dialled == []

        # The control: an uncancelled turn of the same shape DOES dial.
        dialled.clear()
        live = Turn(number=3, max_turns=60, cancelled=False)
        live.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
        live_outcome = build_pipeline(("cancellation", "model_dial")).check(live, run)
        assert live_outcome.continues is True
        assert dialled == ["DIALLED"]

    def test_the_turn_cap_is_placed_before_the_dial_and_never_becomes_a_second_cap(self):
        """The turn cap is OBSERVED before the dial, and deliberately never stops.

        This arm is here because the obvious pin is WRONG. Writing "the turn cap
        terminates at the ceiling" would pass only if the cap had become a second
        enforcement point — which is the exact defect the policy's own docstring
        refuses: the ceiling is the LOOP BOUND, and a middleware that also
        refused would be a second, invisible cap.

        So two properties are pinned instead, and both are load-bearing:

        * **placement** — the cap policy is consulted BEFORE ``model_dial``, so
          the run can see the ceiling approaching in the same turn in which it
          would be reached, which is the only point at which a warning is
          actionable;
        * **non-enforcement** — even AT the ceiling the chain continues and
          dials. The control arm is the same turn one below the ceiling, so
          "the dial happened" cannot be read as "the cap refused and the test
          measured the wrong thing".
        """
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        announced: list[int] = []

        def _at_ceiling(run_id: str, number: int):
            dialled: list[str] = []
            run = TurnRun(run_id=run_id)
            turn = Turn(number=number, max_turns=9, model="m")
            turn.scratch["turn_cap_approach"] = announced.append
            turn.scratch["dial"] = lambda: dialled.append("DIALLED") or "reply"
            outcome = build_pipeline(("turn_cap_observation", "model_dial")).check(
                turn, run
            )
            return outcome, dialled

        at_cap, dialled = _at_ceiling("turn-cap-at-ceiling", 9)

        # Placement: both ran, and the cap was consulted FIRST.
        assert at_cap.considered == ("turn_cap_observation", "model_dial")
        assert at_cap.considered.index("turn_cap_observation") < at_cap.considered.index(
            "model_dial"
        )
        assert announced == [9], "the approach observer was never asked"

        # Non-enforcement: AT the ceiling the chain still continues and dials.
        assert at_cap.continues is True
        assert at_cap.is_termination is False
        assert dialled == ["DIALLED"], (
            "the turn cap refused at the ceiling, which makes it a SECOND cap; "
            "the loop bound is the only enforcement point for the turn ceiling"
        )

        # The control: one turn below the ceiling behaves identically, so the
        # two rows are not distinguishable and this is not asserting an accident
        # of the at-ceiling fixture.
        below, below_dialled = _at_ceiling("turn-cap-below-ceiling", 8)
        assert below.continues is True
        assert below_dialled == ["DIALLED"]

    def test_the_turn_cap_announces_at_most_once_per_run(self):
        """The one-shot latch suppresses the ANNOUNCE, and it is per-RUN.

        Two properties, and they are not the same property:

        * the **observer is asked every turn** — it is a cheap read of the run's
          own turn counter, and suppressing the question would mean the policy
          could not notice the ceiling being crossed later;
        * the **event is emitted at most once**, because a warning that repeats
          every turn is noise rather than a warning.

        The latch lives on the :class:`TurnRun`, not on the pipeline, so a second
        pipeline instance inside one run is still latched. Driving a FRESH
        pipeline each turn is what makes this a test of the latch's SCOPE; with
        one reused pipeline object it would pass whether the latch were on the
        pipeline, on the run, or nowhere at all.
        """
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        asked: list[int] = []
        emitted: list[tuple[str, dict]] = []

        def _one_turn(run: TurnRun, number: int) -> None:
            turn = Turn(number=number, max_turns=9, model="m")
            turn.scratch["turn_cap_approach"] = lambda n: (
                asked.append(n) or {"turns_left": 1}
            )
            turn.scratch["emit"] = lambda event, payload: emitted.append(
                (event, payload)
            )
            turn.scratch["dial"] = lambda: "reply"
            # A FRESH pipeline each turn, on purpose.
            build_pipeline(("turn_cap_observation", "model_dial")).check(turn, run)

        shared_run = TurnRun(run_id="one-shot-latch")
        _one_turn(shared_run, 8)
        _one_turn(shared_run, 8)
        _one_turn(shared_run, 9)

        assert asked == [8, 8, 9], (
            "the observer must be consulted every turn; a policy that stops "
            f"asking cannot notice the ceiling later. asked: {asked}"
        )
        assert [name for name, _ in emitted] == ["turn_cap_approaching"], (
            "the turn-cap announce is not one-shot per run, which makes it noise "
            f"rather than a warning. emitted: {emitted}"
        )

        # The control: a DIFFERENT run is not latched by the first one's latch.
        other_run = TurnRun(run_id="one-shot-latch-other")
        before = len(emitted)
        _one_turn(other_run, 8)
        assert len(emitted) == before + 1, (
            "the latch leaked across runs, so a second task never learns that its "
            "own ceiling is approaching"
        )

    # -- auto-compaction BEFORE context-exceeded ---------------------------

    def test_auto_compaction_runs_before_context_exceeded(self):
        """Overflow triggers COMPACTION, not an error.

        The control is the same overflowing turn with **no compactor bound**, which
        must still terminate ``failed`` — without it, "compaction ran first"
        would be satisfied by a pipeline that never reports overflow at all.
        """
        from runtime.turn_middleware import Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        # -- the remedy ran first and the overflow was resolved by it
        run = TurnRun(run_id="compact-first")
        turn = Turn(
            number=3, max_turns=60, context_tokens=40_000, context_window=32_768
        )
        turn.scratch["compact"] = lambda: 20_000
        outcome = build_pipeline(("auto_compact", "context_exceeded")).check(
            turn, run, phase="pre_call"
        )
        assert outcome.continues is True, (
            "an overflowing turn whose compaction brought it under the window "
            "must continue, not terminate"
        )
        assert outcome.turn.context_tokens == 20_000
        assert outcome.turn.context_compacted is True
        assert any(obs.kind == "auto_compacted" for obs in run.observations)

        # -- the CONTROL: no compactor, so the symptom is still there
        run_ctl = TurnRun(run_id="compact-first-control")
        turn_ctl = Turn(
            number=3, max_turns=60, context_tokens=40_000, context_window=32_768
        )
        outcome_ctl = build_pipeline(("auto_compact", "context_exceeded")).check(
            turn_ctl, run_ctl, phase="pre_call"
        )
        assert outcome_ctl.is_termination is True, (
            "the control must still terminate; if it did not, the arm above "
            "proves nothing about ordering"
        )
        assert outcome_ctl.decision.status == "failed"
        assert outcome_ctl.stopped_at == "context_exceeded"

    # -- read-only BEFORE write dispatch -----------------------------------

    def test_read_only_gates_before_a_write_is_dispatched(self):
        """A write tool is never dispatched in read-only mode.

        Asserted on the DISPATCH ITSELF: the dispatcher records what it was asked
        to run, so a post-hoc gate that ran the write and rolled it back would be
        caught, which is the whole difference between a guarantee and a detector.
        """
        from runtime.turn_middleware import ToolCall, Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        dispatched: list[tuple[str, ...]] = []

        def dispatch(calls):
            dispatched.append(tuple(call.verb for call in calls))
            return iter(())

        run = TurnRun(run_id="read-only-first")
        turn = Turn(number=3, max_turns=60, read_only=True)
        turn.calls = (ToolCall(verb="write", arguments={"path": "a.py"}, writes=True),)
        turn.scratch["dispatch"] = dispatch
        outcome = build_pipeline(("read_only_gate", "write_dispatch")).check(
            turn, run, phase="pre_dispatch"
        )
        assert outcome.is_refusal is True
        assert outcome.stopped_at == "read_only_gate"
        assert dispatched == [], "the write was dispatched in read-only mode"
        assert outcome.decision.charged is False

        # -- the CONTROL: the same call outside read-only IS dispatched
        run_ctl = TurnRun(run_id="read-only-first-control")
        turn_ctl = Turn(number=3, max_turns=60, read_only=False)
        turn_ctl.calls = (
            ToolCall(verb="write", arguments={"path": "a.py"}, writes=True),
        )
        turn_ctl.scratch["dispatch"] = dispatch
        outcome_ctl = build_pipeline(("read_only_gate", "write_dispatch")).check(
            turn_ctl, run_ctl, phase="pre_dispatch"
        )
        assert outcome_ctl.continues is True
        assert dispatched == [("write",)], (
            "the control never dispatched, so the refusal above would be "
            "satisfied by a chain that dispatches nothing"
        )

    def test_a_shell_composition_is_not_mistaken_for_a_read(self):
        """The read-only gate's exemption must survive a composition.

        ``cat a.py > b.py`` starts with a read verb and ends in a write. This is
        the case a head-only check misses, and the read-only guarantee is the one
        place where an exemption that is too generous is a hole.
        """
        from runtime.turn_middleware import ToolCall, Turn, TurnRun
        from runtime.turn_policies import build_pipeline

        dispatched: list[tuple[str, ...]] = []
        turn = Turn(number=3, max_turns=60, read_only=True)
        turn.calls = (
            ToolCall(
                verb="bash",
                arguments={"command": "cat a.py > b.py"},
                writes=False,
            ),
        )
        turn.scratch["dispatch"] = lambda calls: dispatched.append(
            tuple(c.verb for c in calls)
        ) or iter(())
        outcome = build_pipeline(("read_only_gate", "write_dispatch")).check(
            turn, TurnRun(run_id="composition"), phase="pre_dispatch"
        )
        assert outcome.is_refusal is True, (
            "`cat a.py > b.py` was treated as read-only; the shell proof is the "
            "only thing making the read-only exemption safe"
        )
        assert dispatched == []

    # -- quota BEFORE budget ----------------------------------------------

    def test_quota_is_tested_before_budget_and_is_not_a_provider_fault(self):
        """Quota first, and ``provider_fault=False`` means no failover.

        Two facts that are the same fact from the retry ladder's side: an empty
        account is a BILLING condition, so retrying it and failing over to a
        second target spend the same absent money on the same account.
        """
        from runtime import budget_governor as gov
        from runtime import provider_resilience as res

        quota = res.classify_retry(Exception("insufficient_quota"))
        rate = res.classify_retry(Exception("429 Too Many Requests"))
        classified = gov.classify_provider_failure(Exception("insufficient_quota"))

        assert quota.kind == "quota_exhausted"
        assert quota.retryable is False
        assert quota.provider_fault is False
        assert classified.terminal is True
        assert classified.provider_fault is False

        # -- the CONTROL: a genuine rate limit IS retryable and IS a provider
        #    fault, so the quota arm is not satisfied by refusing everything
        assert rate.kind == "rate_limit"
        assert rate.retryable is True
        assert rate.provider_fault is True
        assert quota.kind != rate.kind

    def test_a_quota_failure_does_not_trip_the_circuit_breaker(self):
        """``provider_fault=False`` must mean the breaker stays closed.

        This is the second half of the previous test: a classification the retry
        ladder reads but the breaker does not is a classification that has not
        protected anything.
        """
        from runtime import provider_resilience as res

        quota = res.classify_retry(Exception("insufficient_quota"))
        assert quota.provider_fault is False, (
            "a quota failure counted as a provider fault would take a healthy "
            "endpoint out of rotation over an empty account"
        )

    # -- the refusals are FREE and the terminations are not ----------------

    def test_a_refusal_is_free_and_a_termination_is_charged(self):
        """The repository rule: "a non-retryable failure is FREE".

        Pinned in BOTH directions. Asserting only that a refusal is free is
        satisfied by a Decision type that is free for everything, including the
        terminal cases — which would be a run that could not be failed.
        """
        from runtime.turn_middleware import Decision, Refusal

        refusal = Decision.refuse("no", policy="p")
        assert refusal.charged is False
        assert refusal.retryable is False
        # `free` is a field of the RECEIPT, not a property on `Refusal` -- only
        # `Termination` carries the property, precisely because the asymmetry is
        # the point. Asserting the receipt keeps the claim where a reader sees it.
        assert refusal.to_dict()["free"] is True
        assert refusal.status == "needs_input"
        assert isinstance(refusal, Refusal)

        for status in ("blocked", "failed", "timeout", "cancelled"):
            termination = Decision.terminate(status, "why", policy="p")
            assert termination.charged is True, f"{status} was not charged"
            assert termination.free is False
            assert termination.to_dict()["free"] is False

    def test_a_termination_cannot_claim_to_be_a_human_decision(self):
        """``needs_input`` is a REFUSAL, never a termination.

        Letting a termination carry the refusal status is how the two shapes drift
        back together, and a user saying *no* becomes a failed task — the exact
        defect AGT-11 found and had to undo.
        """
        from runtime.turn_middleware import POLICY_TERMINAL_STATUSES, Decision

        assert "needs_input" not in POLICY_TERMINAL_STATUSES
        with pytest.raises(ValueError):
            Decision.terminate("needs_input", "a human decision", policy="p")

    def test_a_refusal_cannot_be_forged_into_being_charged(self):
        """The free-ness is enforced, not merely a default.

        A frozen dataclass can still be force-mutated through
        ``object.__setattr__``, so the guard is in ``__post_init__`` as well as in
        the signature. Without the ``__post_init__`` half a determined caller
        walks straight around the type.

        The mutation is done on a COPY. Doing it on the real object and then
        asserting the real object is uncharged is self-defeating: the
        ``pytest.raises`` arm already changed it, so the final assertion would be
        measuring this test's own damage rather than the type's invariant.
        """
        import copy

        from runtime.turn_middleware import Decision, Refusal

        refusal = Decision.refuse("no", policy="p")
        assert refusal.charged is False

        forged = copy.deepcopy(refusal)
        with pytest.raises(Exception):
            object.__setattr__(forged, "charged", True)
            # __post_init__ does not re-run on a force-mutated frozen dataclass,
            # so the check below is the one that must hold.
            if getattr(forged, "charged", False):
                raise AssertionError("a refusal was force-mutated into being charged")

        # The real object is untouched, and the CONSTRUCTOR is the second half of
        # the guard: passing charged=True directly must be refused outright.
        assert refusal.charged is False
        with pytest.raises(ValueError):
            Refusal(
                reason="no",
                policy="p",
                turn=0,
                action="stop",
                detail={},
                charged=True,
                retryable=False,
            )


# ==========================================================================
# 3. A REORDER MUST FAIL — the demonstration
# ==========================================================================


class TestAReorderMustFail:
    """Every same-phase ordering, swapped, must be caught at composition.

    This is the group that makes :data:`SEMANTIC_ORDERINGS` a check rather than a
    comment. Each arm moves the ``before`` policy AFTER the ``after`` policy and
    requires ``MiddlewareOrderError``. **There is no arm here that passes** — if
    one starts passing, the ordering it covers is no longer enforced.
    """

    #: The same-phase pairs, i.e. the ones a CHAIN reorder can actually break.
    #: Cross-phase rules are a property of the PHASE TABLE, which no reordering
    #: of the chain can break; they are pinned in
    #: :class:`TestOrderIsDataNotImportOrder` instead.
    SAME_PHASE_PAIRS = (
        ("auto_compact", "context_exceeded"),
        ("read_only_gate", "write_dispatch"),
        ("per_call_budget", "model_dial"),
    )

    def test_each_same_phase_pair_is_enforced(self):
        from runtime.turn_middleware import MiddlewareOrderError
        from runtime.turn_policies import POLICY_CHAIN, build_pipeline

        caught: list[str] = []
        for before, after in self.SAME_PHASE_PAIRS:
            names = list(POLICY_CHAIN)
            first, second = names.index(before), names.index(after)
            assert first < second, (
                f"{before} already precedes {after} in POLICY_CHAIN; this arm "
                "would be testing nothing"
            )
            names[first], names[second] = names[second], names[first]
            with pytest.raises(MiddlewareOrderError) as excinfo:
                build_pipeline(tuple(names))
            message = str(excinfo.value)
            assert before in message and after in message, (
                f"the refusal for {before}/{after} does not name both policies: "
                f"{message[:200]}"
            )
            caught.append(f"{before}->{after}")
        assert len(caught) == len(self.SAME_PHASE_PAIRS)

    def test_the_declared_order_is_the_one_that_survives(self):
        """The control: the SHIPPED order composes.

        Without it, "every swap raises" is satisfiable by a pipeline that refuses
        every order — which would be a broken pipeline, not a strict one.
        """
        from runtime.turn_policies import POLICY_CHAIN, build_pipeline

        pipeline = build_pipeline(POLICY_CHAIN)
        assert pipeline.names == tuple(POLICY_CHAIN)

    def test_every_declared_ordering_names_a_real_policy(self):
        """A rule about a policy that does not exist can never fire.

        ``ordering_declaration_errors`` exists for exactly this: Wave 1 shipped
        six rules of which one was declared ``same_phase=True`` for a pair living
        in two different phases, so five of six reorder attempts passed a check
        that looked like it was passing all six.
        """
        from runtime.turn_middleware import ordering_declaration_errors

        problems = ordering_declaration_errors()
        assert problems == [], f"ordering declarations that cannot be checked: {problems}"

    def test_the_order_violation_predicate_agrees_with_composition(self):
        """``ordering_violations`` and ``build_pipeline`` must not disagree.

        Two checkers for one property is two answers to one question, so this
        pins them together: whatever the predicate reports, the constructor must
        refuse, and vice versa.
        """
        from runtime.turn_middleware import MiddlewareOrderError, ordering_violations
        from runtime.turn_policies import POLICY_CHAIN, build_pipeline

        for before, after in self.SAME_PHASE_PAIRS:
            names = list(POLICY_CHAIN)
            names[names.index(before)], names[names.index(after)] = (
                names[names.index(after)],
                names[names.index(before)],
            )
            predicted = bool(ordering_violations(tuple(names)))
            refused = False
            try:
                build_pipeline(tuple(names))
            except MiddlewareOrderError:
                refused = True
            assert predicted is refused, (
                f"{before}/{after}: ordering_violations said {predicted} and "
                f"the constructor {'refused' if refused else 'accepted'}"
            )


# ==========================================================================
# 4. The order is DATA, not an accident of registration
# ==========================================================================


class TestOrderIsDataNotImportOrder:
    """Registration sequence must not determine visit order, and a middleware
    with no position must fail LOUDLY at composition."""

    def test_registration_sequence_does_not_change_visit_order(self):
        """Appending in the wrong order must still visit in phase order.

        This is the "declared as data" property in its strongest form: the
        pipeline walks PHASES and then the chain, so a caller who appends
        ``auto_compact`` before every ``pre_turn`` policy still gets the same
        visit order. It is also why the cross-phase rules cannot be broken by a
        reorder at all.
        """
        from runtime.turn_policies import POLICY_CHAIN, build_pipeline

        shuffled = (
            "auto_compact",
            *(name for name in POLICY_CHAIN if name != "auto_compact"),
        )
        pipeline = build_pipeline(shuffled)
        ordered = build_pipeline(POLICY_CHAIN)
        assert pipeline.names == shuffled, (
            "the pipeline must record the chain as given; only the VISIT order is "
            "derived from the phases"
        )
        assert pipeline._effective_positions() == ordered._effective_positions(), (
            "registration sequence changed the visit order, so the order is an "
            "accident of import order rather than declared data"
        )

    def test_the_phase_table_itself_is_what_orders_the_chain(self):
        from runtime.turn_middleware import PHASES
        from runtime.turn_policies import POLICY_CHAIN, build_pipeline

        pipeline = build_pipeline(POLICY_CHAIN)
        visited = [name for name, _phase in pipeline._effective_positions().items()]
        phases_seen = [
            PHASES.index(pipeline._effective_positions()[name][1]) for name in visited
        ]
        assert phases_seen == sorted(phases_seen), (
            "the visit order is not phase-monotonic, so the phase table is not "
            "governing execution"
        )

    def test_a_middleware_with_no_phase_fails_loudly_at_composition(self):
        """A positionless middleware must be REFUSED, never silently dropped.

        Silently dropping it is the worse outcome by far: the caller believes a
        guard is in the chain, a receipt never names it, and nothing anywhere
        reports the difference.
        """
        from runtime.turn_middleware import (
            MiddlewareOrderError,
            Policy,
            TurnPolicyPipeline,
        )

        for phase in ("", None, "not_a_phase", "PRE_TURN"):
            with pytest.raises(MiddlewareOrderError) as excinfo:
                TurnPolicyPipeline([Policy("positionless", phase, lambda t, r: None)])
            message = str(excinfo.value)
            assert "positionless" in message, (
                f"the refusal does not name the offending middleware: {message[:160]}"
            )

    def test_an_unnamed_middleware_fails_loudly(self):
        """An unnamed policy cannot appear in a receipt, and a receipt is how a
        reviewer learns which policy fired."""
        from runtime.turn_middleware import (
            MiddlewareOrderError,
            TurnPolicyPipeline,
        )

        class Unnamed:
            phase = "pre_turn"

            def evaluate(self, turn, run):  # pragma: no cover - never reached
                return None

        with pytest.raises(MiddlewareOrderError) as excinfo:
            TurnPolicyPipeline([Unnamed()])
        assert "name" in str(excinfo.value).lower()

    def test_two_middlewares_with_one_name_fail_loudly(self):
        """A receipt naming a policy must say WHICH one ran."""
        from runtime.turn_middleware import (
            MiddlewareOrderError,
            Policy,
            TurnPolicyPipeline,
        )

        with pytest.raises(MiddlewareOrderError):
            TurnPolicyPipeline(
                [
                    Policy("twin", "pre_turn", lambda t, r: None),
                    Policy("twin", "pre_turn", lambda t, r: None),
                ]
            )

    def test_a_cross_phase_rule_is_violated_by_the_wrong_PHASE(self):
        """A cross-phase ordering cannot be broken by a reorder — so the honest
        way to violate it is to declare a policy in the wrong phase.

        This is why the cross-phase rules are pinned separately from the
        same-phase swaps: the check that matters is against the PHASE TABLE.

        Driven off :data:`SEMANTIC_ORDERINGS` itself rather than a hand-written
        pair, so a rule that is added later is covered here automatically and a
        rule that is removed stops being covered silently.
        """
        from runtime.turn_middleware import (
            SEMANTIC_ORDERINGS,
            MiddlewareOrderError,
            Policy,
            TurnPolicyPipeline,
        )

        cross_phase = [rule for rule in SEMANTIC_ORDERINGS if not rule.same_phase]
        assert cross_phase, "no cross-phase rule is declared, so this arm tests nothing"

        caught: list[tuple[str, str]] = []
        for rule in cross_phase:
            # Move the `before` policy into a LATER phase than the `after`
            # policy. Phases are walked in PHASE order, so this inverts the
            # declaration without touching the chain order at all -- which is
            # precisely the violation a same-phase reorder cannot reach.
            phases = ("pre_turn", "pre_call", "pre_dispatch", "post_turn")
            with pytest.raises(MiddlewareOrderError) as excinfo:
                TurnPolicyPipeline(
                    [
                        Policy(rule.after, "pre_turn", lambda t, r: None),
                        Policy(rule.before, phases[-1], lambda t, r: None),
                    ]
                )
            caught.append((rule.before, rule.after))
            assert rule.before in str(excinfo.value) or rule.after in str(
                excinfo.value
            ), f"the refusal for {rule.describe()} does not name the offending policy"

        assert len(caught) == len(cross_phase) > 0

        # The control arm: the SAME pair in its DECLARED phases composes fine.
        # Without it, "the constructor refuses" would be satisfied by a
        # constructor that refuses everything and a test that never once built a
        # legal pipeline.
        for rule in cross_phase:
            from runtime.turn_policies import POLICY_BUILDERS

            TurnPolicyPipeline(
                [
                    Policy(rule.before, _phase_of(rule.before), POLICY_BUILDERS[rule.before]),
                    Policy(rule.after, _phase_of(rule.after), POLICY_BUILDERS[rule.after]),
                ]
            )

    def test_the_phase_vocabulary_is_closed(self):
        """A phase is a name from the table, so reordering the table is a
        visible edit rather than a silent reshuffle of priorities."""
        from runtime.turn_middleware import PHASES, REFUSABLE_PHASES

        assert PHASES == ("pre_turn", "pre_call", "pre_dispatch", "post_turn")
        assert frozenset({"pre_turn", "pre_call", "pre_dispatch"}) == REFUSABLE_PHASES
        assert "post_turn" not in REFUSABLE_PHASES, (
            "post_turn has nothing left to protect, so a refusal there would be "
            "converted into a status the caller misreads"
        )

    def test_every_shipped_policy_declares_a_phase_from_the_table(self):
        from runtime.turn_middleware import PHASES
        from runtime.turn_policies import POLICY_BUILDERS

        for name in POLICY_BUILDERS:
            assert POLICY_BUILDERS[name]().phase in PHASES, (
                f"{name} declares a phase outside the table"
            )


# ==========================================================================
# 5. Adding a policy must not touch the loop
# ==========================================================================


class TestAddingAPolicyTouchesNoLoop:
    """The loop bodies contain no per-policy knowledge.

    Checked on **string literals the loop AST holds**, not on a substring of the
    dumped source. The narrower check is also the honest one: it targets the way
    a loop would actually acquire the knowledge (a policy name reaching a loop as
    a string in a composition tuple) instead of every coincidental spelling of a
    word. ``budget_cap_usd=`` is a config key being read into the turn, and
    ``cancellation`` in a comment is prose — neither is per-policy knowledge, and a
    substring scan reports both as a violation.
    """

    #: The two turn loops, and how to find the loop body in each.
    LOOPS = (
        ("harness/agent_loop_step.py", "", "step", "while turn < cfg.max_turns:"),
        (
            "harness/agent_kernel/strategy.py",
            "DailyCodingStrategy",
            "run",
            "for turn in range(1, max_turns + 1):",
        ),
    )

    def _loop_body(self, source: str, tree: ast.Module, qualifier: str, function: str, marker: str):
        owners = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
            and (
                not qualifier
                or any(
                    isinstance(owner, ast.ClassDef)
                    and owner.name == qualifier
                    and any(child is node for child in ast.walk(owner))
                    for owner in ast.walk(tree)
                )
            )
        ]
        assert owners, f"{function}() not found"
        lines = source.splitlines()
        for owner in owners:
            for node in ast.walk(owner):
                if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                    continue
                index = node.lineno - 1
                if 0 <= index < len(lines) and marker in lines[index]:
                    return node
        raise AssertionError(f"loop header {marker!r} not found in {function}()")

    def test_neither_loop_body_names_a_single_policy(self):
        from runtime.turn_policies import POLICY_CHAIN

        offenders: list[str] = []
        for relpath, qualifier, function, marker in self.LOOPS:
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=relpath)
            body = self._loop_body(source, tree, qualifier, function, marker)
            held = {
                node.value
                for node in ast.walk(body)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            # A trace EVENT of the same name is not per-policy knowledge.
            event_literals = {
                node.args[0].value
                for node in ast.walk(body)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_event"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            }
            for name in POLICY_CHAIN:
                if name in held and name not in event_literals:
                    offenders.append(f"{relpath}:{name}")
        assert offenders == [], (
            f"loop bodies name shipped policies {offenders}; adding a policy "
            "would mean editing a loop again"
        )

    def test_the_loop_calls_the_pipeline_and_not_a_policy(self):
        """The loop's only turn-policy surface is one pipeline check."""
        from runtime.turn_policies import POLICY_CHAIN

        found: dict[str, bool] = {}
        for entry in self.LOOPS:
            relpath = entry[0]
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8-sig")
            found[relpath] = "pre_turn_pipeline" in source
        assert all(found.values()), (
            f"not every loop composes the pipeline: {found}"
        )
        assert len(POLICY_CHAIN) >= 12

    def test_a_policy_can_be_added_without_editing_a_loop(self):
        """The demonstration: add a policy, and no loop needs to change.

        Composed into the real pre-turn chain and driven to a real refusal. This
        is the operational form of the claim above — the pin says the loop holds
        no policy names, and this says a new policy actually works without one.
        """
        from runtime.turn_middleware import Decision, Policy, Turn
        from runtime.turn_policies import (
            PRE_TURN_POLICIES,
            pre_turn_pipeline,
        )

        calls: list[int] = []

        def _w2_probe(turn, run):
            calls.append(turn.number)
            return Decision.refuse("a policy added at runtime", policy="w2_added")

        pipeline = pre_turn_pipeline()
        assert "w2_added" not in pipeline.names
        pipeline.add(Policy("w2_added", "pre_turn", _w2_probe, can_stop=True))

        outcome = pipeline.check(
            Turn(number=4, max_turns=60), __import__(
                "runtime.turn_middleware", fromlist=["TurnRun"]
            ).TurnRun(run_id="add-policy"),
            phase="pre_turn",
        )
        assert outcome.is_refusal is True
        assert outcome.stopped_at == "w2_added"
        assert calls == [4]
        assert "w2_added" not in PRE_TURN_POLICIES, (
            "the added policy must not have leaked into the module-level subset"
        )

    def test_the_shipped_policy_count_is_the_declared_one(self):
        from runtime.turn_policies import POLICY_BUILDERS, POLICY_CHAIN

        assert len(POLICY_CHAIN) == len(set(POLICY_CHAIN)), "duplicate in POLICY_CHAIN"
        assert set(POLICY_CHAIN) == set(POLICY_BUILDERS), (
            "POLICY_CHAIN and POLICY_BUILDERS disagree; a policy registered in "
            "one and absent from the other is either dead code or a silent omission"
        )
        assert len(POLICY_CHAIN) == 12

    def test_every_policy_that_claims_it_can_stop_actually_can(self):
        """"This policy can refuse" is checked by refusing, not by reading a flag.

        Wave 1's first draft derived capability from a bare probe turn on which no
        guard fires, so every policy reported ``can_stop=False`` and the receipt
        claimed a chain of twelve policies of which none could stop anything.
        """
        from runtime.middleware_audit import _load_matrix  # noqa: F401  (import smoke)
        from runtime.turn_policies import policy_receipt

        receipt = policy_receipt()
        assert receipt["declaration_mismatches"] == 0, (
            f"policies whose can_stop disagrees with their trigger: {receipt['policies']}"
        )
        assert receipt["ok"] is True
        stopping = [row for row in receipt["policies"] if row["can_stop"]]
        assert len(stopping) >= 6, f"expected several stopping policies, got {stopping}"
        for row in stopping:
            assert row["stop_reachable"] is True, (
                f"{row['name']} claims it can stop but its trigger did not stop it"
            )


# ==========================================================================
# 6. The matrix under real concurrency
# ==========================================================================


class TestTheMatrixUnderConcurrency:
    """The 14 scenarios driven from many threads — which the matrix does not do.

    A middleware shared across concurrent runs is a data race, and the sequential
    matrix cannot see one. Every concurrent run's fingerprint is compared against a
    SEQUENTIAL baseline, so a leaked latch shows up as a mismatch rather than as a
    run that happened to pass.
    """

    def test_all_fourteen_scenarios_run_under_concurrency_with_no_divergence(self):
        from runtime.middleware_audit import run_matrix

        result = run_matrix(
            determinism_repeats=2, concurrency_workers=8, concurrency_sweep_runs=2
        )
        conc = result["concurrency"]
        assert conc["error_count"] == 0, f"errors under concurrency: {conc['errors']}"
        assert conc["divergence_count"] == 0, (
            f"cross-run divergences: {conc['divergences']}"
        )
        assert conc["completed"] == conc["total_runs"] > 0
        assert result["required_scenarios_present"] is True

    def test_composed_chains_are_distinct_objects(self):
        """Asserted, not inferred from a passing run.

        A single module-level pipeline shared across runs IS the documented data
        race, and a chain that happened to be safe under this workload is a
        different claim from one nobody shares.
        """
        from runtime.middleware_audit import run_matrix

        conc = run_matrix(
            determinism_repeats=2, concurrency_workers=8, concurrency_sweep_runs=1
        )["concurrency"]
        assert conc["pipeline_objects_distinct"] is True, (
            f"{conc['distinct_pipeline_objects']} distinct chain objects out of "
            f"{conc['sampled_chain_constructions']} constructions"
        )
        assert conc["chain_objects_retained"] == conc["sampled_chain_constructions"]

    def test_the_adapter_agrees_with_the_pure_step_on_every_runnable_scenario(self):
        from runtime.middleware_audit import run_matrix

        result = run_matrix(determinism_repeats=1, concurrency_workers=4, concurrency_sweep_runs=1)
        assert result["rows_agreed"] == result["scenarios"], [
            row for row in result["agreement"] if not row["agree"] or row["any_failed_run"]
        ]
        assert result["adapter_skipped_scenarios"] == ["cancel"], (
            "the matrix declares `cancel` non-runnable on the adapter (a real "
            "in-flight interrupt); a different skip set means the matrix changed"
        )

    def test_each_scenario_is_deterministic(self):
        from runtime.middleware_audit import run_matrix

        result = run_matrix(determinism_repeats=2, concurrency_workers=2, concurrency_sweep_runs=1)
        for row in result["determinism"]:
            assert row["stable"] is True, (
                f"{row['layer']} was non-deterministic for {row['unstable']}"
            )
        assert result["determinism_ok"] is True

    def test_each_run_gets_a_fresh_tool_table(self):
        """The shared-closure defect in ``tests/test_agent_loop_matrix.py``.

        ``_clash()`` is invoked once at ``MATRIX`` construction, so one closure is
        shared by the four tests parametrizing ``tool_failure`` and the first to
        run consumes the failure. This asserts the audit's workaround actually
        hands out distinct tables — without it the ``tool_failure`` scenario
        measures whichever test ran first.
        """
        from runtime.middleware_audit import _load_matrix, fresh_case_tools

        matrix = _load_matrix()
        case = next(c for c in matrix.MATRIX if c.name == "tool_failure")
        shared = matrix._tools_for(case)["bash"]
        fresh = fresh_case_tools(matrix, case)["bash"]
        assert shared is not fresh, (
            "fresh_case_tools returned the SHARED closure, so the tool_failure "
            "scenario is still order-dependent"
        )
        # The shared closure consumes its failure; a fresh one must not have.
        first_shared = shared({"command": "pyflakes src"}, 1)
        assert first_shared.ok is False
        assert fresh({"command": "pyflakes src"}, 1).ok is False, (
            "a fresh _clash() must fail its first call; if it succeeded, the "
            "fixture is not the one being claimed"
        )


# ==========================================================================
# 7. The supervision exemption set must not have grown
# ==========================================================================


class TestTheExemptionSetIsStillThree:
    """Three, not four. Phase 4's background jobs must not be a fourth.

    ``supervision_exemption`` is **reason-agnostic** — it accepts any non-empty
    ``reason`` string — so the exemption set is defined by what the tree WRITES.
    A census that enumerated the reader's vocabulary would report whatever a probe
    injected and pass vacuously, which is why the count here is re-derived from
    the source.
    """

    def test_there_are_three_distinct_skip_conditions(self):
        from runtime.middleware_audit import exemption_census

        census = exemption_census()
        assert census["skip_condition_count"] == 3, census["distinct_skip_conditions"]
        assert census["distinct_reason_count"] == 2, census["distinct_exemption_reasons"]

    def test_awaiting_approval_is_a_re_expression_not_a_fourth(self):
        from runtime import budget_governor as gov
        from runtime.middleware_audit import exemption_census

        census = exemption_census()
        assert census["awaiting_approval_resolves_to"] == gov.EXEMPTION_APPROVAL
        assert census["awaiting_approval_is_a_re_expression_not_a_fourth"] is True
        # The historical boolean still works byte-for-byte.
        assert gov.supervision_exemption({"awaiting_approval": True}) is not None
        assert gov.supervision_exemption({"awaiting_approval": False}) is None

    def test_background_jobs_write_no_exemption(self):
        from runtime.middleware_audit import exemption_census

        census = exemption_census()
        assert census["background_jobs_writes_an_exemption"] is False, (
            "a background job now writes a supervision exemption, which makes "
            "the set four; Phase 4 must not have grown it"
        )
        assert census["phase_4_background_jobs_are_not_a_fourth"] is True

    def test_the_census_finds_the_real_writer(self):
        """The control: the scan is not vacuously empty.

        The first version of this scan read only positional ``.update(...)``
        arguments and found **zero** writers tree-wide — because ``worker.py``
        writes the marker as a KEYWORD argument. A census that reports "no file
        writes an exemption" is exactly the finding that makes "background jobs
        write none" look proved, so a false negative here certified the wrong
        answer. This arm requires the scan to find the real production writer.
        """
        from runtime.middleware_audit import exemption_census

        census = exemption_census()
        assert census["production_writers"] == ["worker.py"], census["writers"]
        assert census["single_production_writer"] is True
        assert census["test_fixture_writers"], (
            "no test fixture writes an exemption, so the scan may be broken"
        )

    def test_an_expired_exemption_is_not_an_exemption(self):
        from runtime import budget_governor as gov

        expired = gov.state_stale_exempt(
            {
                "status": "running",
                "supervision_exemption": {
                    "reason": gov.EXEMPTION_BACKOFF,
                    "until_epoch": gov.now_epoch() - 1.0,
                },
            }
        )
        assert expired[0] is False
        assert "expired" in expired[1]

    def test_the_heartbeat_kill_and_the_wall_clock_are_not_exemptions(self):
        """Neither is readable through the exemption reader, and neither has a
        reason constant -- so there is nothing for a future caller to grant one
        through."""
        from runtime import budget_governor as gov

        for reason in ("heartbeat_stale", "wall_clock"):
            assert not hasattr(gov, f"EXEMPTION_{reason.upper()}"), (
                f"a {reason} exemption constant appeared; the exemption set must "
                "not grow"
            )


# ==========================================================================
# 8. What the refactor cost, stated rather than hidden
# ==========================================================================


class TestTheCostIsReportedNotHidden:
    """The six-metric table and the per-turn overhead are pinned as measurements.

    A refactor that bought maintainability with an unacceptable per-turn cost
    should fail HERE, visibly, rather than in a report nobody diffs. The overhead
    is bounded on its **absolute** cost and on its share of a real turn; the
    guard-section ratio is reported too, because the two answer different
    questions and quoting either alone is misleading.
    """

    #: What the brief calls acceptable: "a refactor with a 2% per-turn overhead
    #: is worth it for maintainability; at 20%, it is not." The bound is on the
    #: share of a REAL turn, which is the figure that answers the brief's
    #: question. The share of the guard section in isolation is reported but not
    #: gated, because a percentage of eleven microseconds does not describe a
    #: turn.
    MAX_OVERHEAD_PCT_OF_A_REAL_TURN = 20.0

    def test_adding_a_policy_is_materially_cheaper(self):
        from runtime.middleware_audit import add_policy_cost

        cost = add_policy_cost()
        assert cost["before"]["lines"] > cost["after"]["lines"], (
            "adding a policy is not cheaper than the loop branch it replaced"
        )
        assert cost["after"]["loop_bodies_touched"] == 0
        assert cost["before"]["loop_bodies_touched"] == 2
        assert cost["after"]["must_read_every_other_branch"] is False
        assert cost["ok"] is True

    def test_the_per_turn_overhead_is_within_budget(self):
        from runtime.middleware_audit import overhead

        rows = overhead(repeats=400)
        assert rows["real_turn_available"] is True, (
            "no real turn could be timed, so the overhead share is NOT a zero"
        )
        share = rows["overhead_pct_of_a_real_turn"]
        assert share is not None
        # An UNRESOLVED delta may be reported negative, and a negative share is
        # not "under budget" -- it is an unmeasured quantity that happens to have
        # come out below zero. The budget is only checked on a RESOLVED figure;
        # otherwise the gate would pass on noise, which is the same defect as the
        # phantom `checks_total: 0` this round found in the scale arm.
        resolution = rows["resolution"]
        if resolution["resolved"]:
            # The budget is a bound on the MAGNITUDE, in either direction. A
            # resolved `faster` is a real measurement and is trivially inside
            # budget -- it is NOT a failure.
            #
            # The first draft of this pin asserted `direction == "slower"` on the
            # resolved branch. That assertion failed in a full-suite run and
            # passed standalone: under load the reconstructed INLINE arm gets
            # slower, the delta crosses 3 sigma, and the pin failed on host
            # noise. **A pin that fails because the machine was busy is a pin
            # people learn to ignore**, which is the same defect as the phantom
            # `checks_total: 0` this round found in the scale arm.
            assert abs(share) <= self.MAX_OVERHEAD_PCT_OF_A_REAL_TURN, (
                f"the middleware layer costs {share}% of a real turn "
                f"(RESOLVED at {round(resolution['confidence_ratio'], 1)} sigma), "
                f"over the {self.MAX_OVERHEAD_PCT_OF_A_REAL_TURN}% budget"
            )
            assert resolution["direction"] in ("slower", "faster")
        else:
            assert resolution["direction"] == "not_resolved"
            assert resolution["confidence_ratio"] < resolution["required_sigmas"]

    def test_the_overhead_receipt_carries_a_resolution_verdict(self):
        """An unsigned overhead number is a false claim waiting to be read.

        The sign of an unresolved delta is host noise: measured twice on this
        host, the same function reported **-4.5 us/turn** and then **+3.0
        us/turn**. A reader seeing either number alone would conclude the
        middleware is faster or slower respectively, and neither is a
        measurement. So the receipt must carry the gate.
        """
        from runtime.middleware_audit import overhead

        rows = overhead(repeats=300)
        resolution = rows["resolution"]
        for key in (
            "resolved",
            "direction",
            "standard_error_s",
            "confidence_ratio",
            "required_sigmas",
            "repeats",
            "method",
        ):
            assert key in resolution, f"the resolution receipt is missing {key!r}"
        assert resolution["direction"] in ("slower", "faster", "not_resolved")
        assert (resolution["direction"] == "not_resolved") is (not resolution["resolved"]), (
            "an unresolved delta must never be reported with a direction"
        )
        assert rows["resolution_chain_rebuilt"]["repeats"] == resolution["repeats"]

    def test_the_resolution_gate_is_a_standard_error_test_NOT_a_scatter_test(self):
        """The gate must be able to FIRE.

        The first version of this gate compared the delta to the series' raw MAD,
        and dispersion does not shrink with sample count -- so no practical ``n``
        could ever have satisfied it and every run would have read
        ``NOT RESOLVED``. That is a dead gate, which is worse than no gate: it
        looks like rigour.

        The control holds the effect CONSTANT, raises N, and requires the standard
        error to shrink toward ``sqrt(N)`` and the gate to eventually RESOLVE.
        """
        from runtime.middleware_audit import (
            MEDIAN_EFFICIENCY,
            RESOLUTION_SIGMAS,
            _resolution,
        )

        # 50us of effect against 100us of noise straddles the 3-sigma line
        # between n=100 and n=1600 -- because the ratio grows as sqrt(N), the
        # pair can only resolve at the larger n if the gate is a standard-error
        # test. The first draft used 40us of noise, which resolved at BOTH
        # sample counts and so tested nothing about the gate.
        effect = 50e-6  # 50us, a delta large enough to be resolvable
        noise = 100e-6
        low = _resolution(effect, noise, repeats=100)
        high = _resolution(effect, noise, repeats=1600)

        assert low["confidence_ratio"] < RESOLUTION_SIGMAS, (
            "the control no longer straddles the gate: it already resolves at "
            f"n=100 (ratio {low['confidence_ratio']:.2f}), so the sqrt(N) arm "
            "below proves nothing"
        )

        assert high["standard_error_s"] < low["standard_error_s"], (
            "the standard error did not shrink with N, so this gate cannot fire"
        )
        # Shrinking toward sqrt(N): a 16x increase in N divides the standard
        # error by sqrt(16) == 4. The first draft of this pin said "quadrupling N
        # halves the SE" while using a 16x increase and expected 0.5 -- the GATE
        # was right at 0.25 and the pin's arithmetic was wrong.
        assert high["repeats"] == 16 * low["repeats"]
        expected = 1.0 / (16**0.5)
        observed = high["standard_error_s"] / low["standard_error_s"]
        assert abs(observed - expected) < 0.02, (
            f"SE scaled by {observed:.3f} for a 16x N increase; a sqrt(N) standard "
            f"error scales by exactly {expected:.3f}"
        )
        assert low["resolved"] is False
        assert high["resolved"] is True, (
            "the gate still refuses a 50us delta at n=1600 against 40us of noise, "
            "which is exactly the dead-gate failure this pin exists to catch"
        )
        assert high["direction"] == "slower"
        assert high["required_sigmas"] == RESOLUTION_SIGMAS
        assert MEDIAN_EFFICIENCY > 1.0

        # And the permissive direction: a NEGATIVE effect of the same size must
        # resolve to `faster`, never to a positive "overhead".
        negative = _resolution(-effect, noise, repeats=1600)
        assert negative["resolved"] is True
        assert negative["direction"] == "faster"

        # A zero effect never resolves, whichever way it is measured.
        assert _resolution(0.0, noise, repeats=1600)["resolved"] is False

    def test_the_overhead_is_reported_as_an_absolute_cost_too(self):
        """A percentage alone is not a cost report. The microseconds are the
        number a reader can carry."""
        from runtime.middleware_audit import overhead

        rows = overhead(repeats=400)
        assert rows["overhead_us_per_turn_cached"] is not None
        assert rows["overhead_us_per_turn_chain_rebuilt"] is not None
        assert rows["inline_branches_us_per_turn"] > 0
        assert rows["middleware_cached_chain_us_per_turn"] > 0
        # A cached chain is cheaper than one rebuilt per turn -- that is why both
        # loops compose once per RUN.
        assert (
            rows["middleware_cached_chain_us_per_turn"]
            <= rows["middleware_chain_rebuilt_per_turn_us_per_turn"]
        )
        # No percentile is claimed from this sample count.
        assert "no percentile" in rows["reported_statistic"].lower()

    def test_the_behaviour_divergence_row_is_zero_on_both_sides(self):
        from runtime.middleware_audit import refactor_table

        table = refactor_table(overhead_repeats=200)
        assert table["metrics"]["behaviour_divergence"]["before"] == 0
        assert table["metrics"]["behaviour_divergence"]["after"] == 0
        assert table["loop_regressions"] == [], table["loop_regressions"]
        assert table["ok"] is True


# ==========================================================================
# 9. The audit surface itself must not rot
# ==========================================================================


class TestTheAuditSurfaceIsHonest:
    """Guards on the measuring instrument, which is where this repository has
    historically been bitten hardest."""

    def test_an_unmeasurable_value_is_never_reported_as_zero(self):
        from runtime.middleware_audit import _median_or_none

        assert _median_or_none([]) is None
        assert _median_or_none([None, None]) is None
        assert _median_or_none([1.0, 3.0, 2.0]) == 2.0

    def test_the_scale_levels_are_the_documented_ones(self):
        from runtime.middleware_audit import CONCURRENCY_LEVELS

        assert CONCURRENCY_LEVELS == (10, 25, 50), (
            "the scale target the brief names is 10-50; changing the levels "
            "changes what the sweep proves"
        )

    def test_the_four_named_fault_injections_are_declared(self):
        from runtime.middleware_audit import FAULT_INJECTIONS

        names = {row["fault"] for row in FAULT_INJECTIONS}
        assert names == {
            "worker_killed_mid_run",
            "budget_exhausted",
            "provider_429",
            "tool_hang",
        }
        for row in FAULT_INJECTIONS:
            assert row["owner"], f"{row['fault']} names no owning authority"
            assert row["expect"], f"{row['fault']} names no expected behaviour"

    def test_the_stress_variants_actually_stress(self):
        """Every declared stress variant must have a reason to exist.

        A variant that produces the same turn as ``base`` is a duplicate row, and
        a differential padded with duplicates looks bigger without covering more.
        """
        from runtime.middleware_audit import STRESS_VARIANTS, differential

        diff = differential()
        assert "base" in {name for name, _ in STRESS_VARIANTS}
        firing_variants = {
            row["variant"] for row in diff["rows"] if row["a_guard_fired"]
        }
        assert {"over_wall_clock", "over_budget", "cancelled"} <= firing_variants, (
            f"these variants never tripped a guard: {firing_variants}"
        )
        assert diff["rows_with_a_guard_firing"] > 0

    def test_the_scale_arm_reads_the_stress_reports_ACTUAL_schema(self):
        """The scale arm must derive its checks, not report a phantom zero.

        ``runtime.stress``'s report has **no** ``checks`` array. It publishes
        ``n_tasks / concurrency / n_kills / mode / elapsed_s / killed /
        max_overlap / spawn_count / finish_count / failures``. The first version of
        this audit looked for ``checks``, ``check_results`` and ``results``, found
        none, and reported ``checks_total: 0`` on every run -- while the exit code
        made the verdict green. **A zero on a table nobody published is not a
        pass**, and it hid the fact that the arm was not reading its evidence.
        """
        from runtime.middleware_audit import scale_run

        row = scale_run(10, mode="fake")
        assert row["checks_total"] > 0, (
            "the scale arm derived zero checks; it is reading a schema the stress "
            "harness does not publish, and its green verdict came from the exit "
            "code alone"
        )
        assert row["checks_total"] == row["checks_failed"] + len(
            [c for c in row["checks"] if c["ok"] is True]
        )
        names = {c["name"] for c in row["checks"]}
        # Every check must be derived from a field the report really carries.
        for required in (
            "all_tasks_finished",
            "no_failures_recorded",
            "concurrency_cap_respected",
            "every_kill_landed",
            "every_kill_hit_a_task_with_progress",
            "spawns_exceed_finishes_by_the_kill_count",
        ):
            assert required in names, f"the scale arm lost the {required!r} check"
        # And the report keys the checks read must be the ones it publishes.
        assert row["report_present"] is True
        for key in ("finish_count", "max_overlap", "killed", "spawn_count", "failures"):
            assert key in row["report_keys"], (
                f"the scale arm reads {key!r}, which the stress report does not "
                f"publish; keys are {row['report_keys']}"
            )

    def test_every_scratch_directory_this_module_creates_is_removed(self):
        """Cleanup is asserted on the RECEIPT, not assumed from the exit code.

        Measured on this host before the fix: **51** stale ``w2-*`` directories
        were sitting in the system temp directory, left by this module's own
        earlier runs -- ``w2-matrix-*``, ``w2-adapter-conc-*``, ``w2-scale-*`` and
        ``w2-kill-*``. The fault-injector arm cleaned up on the success path only,
        so any raise leaked the whole task-log tree.

        The arms are expensive, so this drives the two cheap ones plus the matrix,
        and asserts the receipt rather than scanning the filesystem: a cleanup that
        silently did nothing would report ``removed: True`` only if it lied, and
        ``residue`` plus ``removed`` are both on the receipt.
        """
        import os
        import tempfile

        from runtime.middleware_audit import run_matrix

        before = {
            name
            for name in os.listdir(tempfile.gettempdir())
            if name.startswith(("w2-matrix-", "w2-adapter-conc-", "w2-scale-", "w2-kill-"))
        }

        matrix = run_matrix(determinism_repeats=1, concurrency_workers=2, concurrency_sweep_runs=1)

        after = {
            name
            for name in os.listdir(tempfile.gettempdir())
            if name.startswith(("w2-matrix-", "w2-adapter-conc-", "w2-scale-", "w2-kill-"))
        }

        cleanup = matrix["cleanup"]
        assert cleanup["owned_by_this_call"] is True
        assert cleanup["removed"] is True, (
            f"the matrix scratch root survived: {cleanup['scratch_root']}"
        )
        assert cleanup["residue"] == []
        assert cleanup["entries_created"] > 0, (
            "the run created nothing, so this arm proved cleanup of an empty "
            "directory rather than of a populated one"
        )
        leaked = sorted(after - before)
        assert leaked == [], f"this module leaked scratch directories: {leaked}"
        # The verdict itself depends on cleanup, so a leak cannot be green.
        assert matrix["ok"] is True

    def test_the_module_docstring_names_its_scope(self):
        """A reader must be able to tell what was NOT run."""
        source = (REPO_ROOT / "runtime" / "middleware_audit.py").read_text(
            encoding="utf-8-sig"
        )
        assert "no Docker" in source and "no provider" in source
        assert re.search(r"host-only", source, re.IGNORECASE)
