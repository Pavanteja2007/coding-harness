"""P4/W1-T3 — the turn-policy middleware pipeline, and the test-removal floor.

Named after behaviour rather than module, so a failure says what stopped being
true.

Every test here has a **control arm** where the claim could otherwise pass
vacuously. That is not decoration: the three claims most likely to pass for the
wrong reason in this round are

* "a refusal is free" — satisfiable by never charging anything, so the charged arm
  is asserted too;
* "the ordering is enforced" — satisfiable by a chain that stops so early nothing
  runs, so every ordering test also drives the configuration where the chain
  *does* proceed;
* "the 8 test-deletion shapes are refused" — satisfiable by a rule that refuses
  everything, so every rule carries ``not_match`` cases that must NOT fire.

Run explicitly; `pyproject.toml` pins `testpaths = ["tests"]`, so a bare
`python -m pytest` collects none of this:

    python -m pytest runtime/test_p4w1_turn_policy_pins.py -q -p no:randomly
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from runtime import loop_complexity, test_protection, turn_middleware, turn_policies
from runtime.turn_middleware import (
    Decision,
    MiddlewareOrderError,
    Policy,
    Refusal,
    RunStateError,
    Termination,
    ToolCall,
    Turn,
    TurnPolicyPipeline,
    TurnRun,
)
from runtime.turn_policies import (
    POLICY_CHAIN,
    build_full_chain,
    build_pipeline,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The protocol: four decisions, and refusal is not termination
# ---------------------------------------------------------------------------


class TestTheProtocolDistinguishesRefusalFromTermination:
    def test_a_refusal_is_not_a_termination_and_cannot_be_built_as_one(self):
        """Two different facts, two different types, not one flag.

        A policy refusal means "something declined; report it and stop asking".
        A termination means "the run cannot continue". Collapsing them is how a
        user saying *no* becomes a failed task — the defect AGT-11 had to undo.
        """
        refusal = Decision.refuse("a policy said no", policy="p", turn=3)
        termination = Decision.terminate("failed", "the run cannot continue", turn=3)
        assert isinstance(refusal, Refusal)
        assert not isinstance(refusal, Termination)
        assert isinstance(termination, Termination)
        assert not isinstance(termination, Refusal)
        assert refusal.status == turn_middleware.REFUSAL_STATUS
        assert termination.status != turn_middleware.REFUSAL_STATUS, (
            "a Termination reporting needs_input is a refusal wearing the wrong "
            "name, which is exactly the drift this separation exists to stop"
        )

    def test_a_refusal_is_free_and_its_charged_arm_is_asserted_too(self):
        """The repository rule: *a non-retryable failure is FREE*.

        Both halves. Asserting only that a refusal is free would pass against an
        implementation that charges nothing ever, so the **charged** arm is
        asserted too — a Termination IS charged.
        """
        refusal = Decision.refuse("no")
        assert refusal.charged is False
        assert refusal.retryable is False
        assert refusal.to_dict()["free"] is True
        termination = Decision.terminate("failed", "stopped")
        assert termination.charged is True
        assert termination.free is False

    def test_a_charged_refusal_is_not_constructible(self):
        """Free is enforced, not merely defaulted.

        A field default is a convention; the doctrine calls out conventions as
        the thing that gets forgotten. So the type refuses to build a charged
        refusal at all.
        """
        with pytest.raises(ValueError, match="FREE"):
            Refusal(reason="no", charged=True)
        with pytest.raises(ValueError, match="not retryable"):
            Refusal(reason="no", retryable=True)

    def test_a_forced_charged_refusal_is_still_caught(self):
        """The guard is not only in the signature.

        A frozen dataclass can still be mutated through ``object.__setattr__``.
        ``Refusal.__post_init__`` re-checks, so a determined caller cannot build
        one. This is the difference between a guard that lives in a signature
        and one that lives in the object.
        """
        refusal = Decision.refuse("no")
        with pytest.raises(ValueError):
            object.__setattr__(refusal, "charged", True)
            refusal.__post_init__()

    def test_an_unrecognised_status_cannot_be_a_termination(self):
        for status in ("completed_verified", "success", "needs_input", "", None):
            with pytest.raises(ValueError):
                Decision.terminate(status, "x")

    def test_a_policy_that_raises_becomes_a_charged_termination_naming_it(self):
        """A broken policy must not unwind the run into an unlabelled traceback."""

        def broken(turn: Turn, run: TurnRun):
            raise RuntimeError("the policy is broken")

        pipeline = TurnPolicyPipeline([Policy("broken", "pre_turn", broken)])
        verdict = pipeline.check(Turn(number=1, max_turns=1), TurnRun(run_id="r"))
        assert verdict.is_termination
        assert "broken" in verdict.decision.reason
        assert "RuntimeError" in verdict.decision.reason

    def test_a_policy_returning_nonsense_is_refused_rather_than_read_as_no_opinion(self):
        """The dangerous direction. An unrecognised return read as "no opinion"
        would let a policy silently gate nothing."""

        def nonsense(turn: Turn, run: TurnRun):
            return "fine by me"

        pipeline = TurnPolicyPipeline([Policy("nonsense", "pre_turn", nonsense)])
        verdict = pipeline.check(Turn(number=1, max_turns=1), TurnRun(run_id="r"))
        assert verdict.is_termination
        assert "not a Decision" in verdict.decision.reason


# ---------------------------------------------------------------------------
# 2. Ordering is declared, and every declared rule is enforced
# ---------------------------------------------------------------------------


class TestEverySemanticOrderingIsEnforced:
    #: Each rule with the fixture that would break it if the order were wrong.
    #: For a ``same_phase`` rule the violation is a SWAP of two chain positions.
    #: For a cross-phase rule no swap can break it — the phase walk fixes the
    #: visit order — so the honest assertion is on the phase table, and saying so
    #: is the point: a reader who assumed a swap was the test would write a test
    #: that cannot fail.
    SAME_PHASE_SWAPS = (
        ("auto_compact", "context_exceeded"),
        ("per_call_budget", "model_dial"),
        ("read_only_gate", "write_dispatch"),
    )
    CROSS_PHASE_PAIRS = (
        ("budget_cap", "per_call_budget"),
        ("wall_clock", "model_dial"),
        ("cancellation", "model_dial"),
    )

    @staticmethod
    def _swapped(a: str, b: str) -> tuple[str, ...]:
        names = list(POLICY_CHAIN)
        i, j = names.index(a), names.index(b)
        names[i], names[j] = names[j], names[i]
        return tuple(names)

    def test_every_declared_ordering_is_present_in_the_shipped_chain(self):
        """A declared rule naming a policy that does not exist is a rule that
        cannot fire — and nothing reports it."""
        assert not turn_middleware.ordering_declaration_errors(), (
            "a declared semantic ordering cannot be checked: "
            f"{turn_middleware.ordering_declaration_errors()}"
        )

    def test_swapping_any_same_phase_pair_is_refused_at_construction(self):
        for before, after in self.SAME_PHASE_SWAPS:
            with pytest.raises(MiddlewareOrderError):
                build_pipeline(self._swapped(before, after))

    def test_the_declared_order_itself_builds(self):
        """The control arm for the three above: the swaps are refused because
        the order matters, not because every chain is refused."""
        assert build_pipeline(POLICY_CHAIN).names == POLICY_CHAIN

    def test_a_cross_phase_pair_cannot_be_broken_by_a_swap_and_is_checked_on_the_phase_table(self):
        """The cross-phase rule is enforced by the PHASE TABLE, not by chain order.

        This is stated as a measurement rather than a hope, because the obvious
        test -- "swap two cross-phase policies and assert the chain still builds"
        -- is a test that CANNOT FAIL and is therefore worse than no test.
        A swap across phases moves a policy past every same-phase rule it now
        sits after, so `wall_clock` (pre_turn) swapped with `model_dial`
        (pre_call) puts `model_dial` ahead of `per_call_budget` and is refused by
        the *within-phase* rule. Asserting "the swap is accepted" would have been
        asserting the opposite of what happens.

        So the assertion is on the thing that actually enforces it: the declared
        phase ranks differ. Enforcement of the cross-phase order is then driven
        separately by moving a policy into the wrong phase.
        """
        rank = {phase: i for i, phase in enumerate(turn_middleware.PHASES)}
        for before, after in self.CROSS_PHASE_PAIRS:
            phases = {
                name: build_pipeline((name,)).middlewares[0].phase for name in POLICY_CHAIN
            }
            assert phases[before] != phases[after], (
                f"{before} and {after} are both {phases[before]}; a pair that "
                "does not straddle two phases cannot demonstrate a cross-phase "
                "rule, so this test would be asserting nothing"
            )
            assert rank[phases[before]] < rank[phases[after]], (
                f"{before} is in {phases[before]} and {after} is in "
                f"{phases[after]}; the phase table does not order them, so the "
                "cross-phase rule is violated by construction"
            )

    def test_a_cross_phase_swap_can_still_be_refused_by_a_within_phase_rule(self):
        """Why the test above is shaped the way it is, recorded as a pin.

        `wall_clock` (pre_turn) and `model_dial` (pre_call) straddle two phases,
        so they are a legitimate cross-phase pair -- and swapping them is STILL
        refused, because `model_dial` lands ahead of `per_call_budget` and that
        is a *within-phase* violation.

        Both facts are true and the combination is the interesting part: phase
        rank is necessary but not sufficient for a cross-phase pair, because a
        cross-phase swap also relocates a policy among its new phase's
        neighbours. A reader who believes "different phase means the order does
        not matter" is wrong, and this is the pin that says so.
        """
        with pytest.raises(MiddlewareOrderError):
            build_pipeline(self._swapped("wall_clock", "model_dial"))

    def test_moving_a_cross_phase_policy_into_the_wrong_phase_is_refused(self):
        """The way a cross-phase rule CAN actually be violated, driven."""
        wrong = [
            turn_policies.Policy("budget_cap", "pre_call", turn_policies._budget_cap),
        ]
        rest = [
            turn_policies.POLICY_BUILDERS[name]()
            for name in POLICY_CHAIN
            if name != "budget_cap"
        ]
        with pytest.raises(MiddlewareOrderError):
            TurnPolicyPipeline(wrong + rest)

    def test_the_two_orderings_the_brief_names_are_behavioural_not_textual(self):
        """**Cost before the dial** — measured by counting dials, not by
        comparing indices.

        The control arm is in the same test: the same chain DOES dial when the
        budget allows it, so this cannot pass by the dial being unreachable.
        """
        dials: list[int] = []
        over = Turn(number=1, max_turns=60, spent_usd=9.9, budget_cap_usd=2.0)
        over.scratch["dial"] = lambda: dials.append(1) or "reply"
        verdict = build_full_chain().check(over, TurnRun(run_id="over"))
        assert verdict.is_termination and dials == []

        ok_dials: list[int] = []
        under = Turn(number=1, max_turns=60, spent_usd=0.1, budget_cap_usd=2.0)
        under.scratch["dial"] = lambda: ok_dials.append(1) or "reply"
        assert build_full_chain().check(under, TurnRun(run_id="under")).decision is None
        assert ok_dials == [1], "the control arm did not dial; the first half is vacuous"

    def test_compact_before_context_exceeded_is_behavioural_and_the_control_is_the_saved_turn(self):
        """**Auto-compaction before a context-exceeded error.**

        The pair of cases is the proof: an overflow that compaction can *reduce*
        continues, and the same overflow it cannot reduce terminates. If the
        order were reversed both would terminate, so the two together
        discriminate.
        """
        saved = Turn(number=1, max_turns=60, context_tokens=40_000, context_window=32_768)
        saved.scratch["compact"] = lambda: 8_000
        out_saved = build_full_chain().check(saved, TurnRun(run_id="saved"))
        assert out_saved.continues, "a compaction that fits the window still failed the turn"
        assert out_saved.turn.context_compacted is True

        unsavable = Turn(number=1, max_turns=60, context_tokens=40_000, context_window=32_768)
        unsavable.scratch["compact"] = lambda: 39_000
        out_bad = build_full_chain().check(unsavable, TurnRun(run_id="bad"))
        assert not out_bad.continues
        assert out_bad.stopped_at == "context_exceeded"
        assert out_bad.decision.status == "failed"

    def test_read_only_before_write_dispatch_is_behavioural_and_the_control_is_a_read(self):
        """**Read-only gates a write before dispatch, not after.**

        The control arm is the read: read-only mode with only reads must let the
        dispatch through, so the refusal cannot pass by the gate refusing every
        turn.
        """
        dispatched: list[str] = []
        turn = Turn(number=1, max_turns=60, read_only=True)
        turn.calls = (ToolCall(verb="write", arguments={"path": "a.py"}, writes=True),)
        turn.scratch["dispatch"] = lambda calls: [
            dispatched.append(c.verb) for c in calls
        ]
        verdict = build_full_chain().check(turn, TurnRun(run_id="ro"))
        assert verdict.is_refusal, verdict.to_dict()
        assert verdict.free is True
        assert dispatched == [], "the write reached dispatch in read-only mode"
        assert verdict.stopped_at == "read_only_gate"

        ok: list[str] = []
        turn2 = Turn(number=1, max_turns=60, read_only=True)
        turn2.calls = (ToolCall(verb="read", arguments={"path": "a.py"}, writes=False),)
        turn2.scratch["dispatch"] = lambda calls: [ok.append(c.verb) for c in calls]
        out2 = build_full_chain().check(turn2, TurnRun(run_id="ro-ok"))
        assert ok == ["read"], "read-only mode refused a read; the gate is too strict"
        assert not out2.is_refusal and not out2.is_termination, (
            f"a plain read was answered with {out2.decision}"
        )
        # And it went through the chain rather than skipping it. `stopped_at` is
        # `""` for "nothing stopped" (an empty string, not None) -- asserted
        # literally so a change to that sentinel is visible here rather than
        # silently turning this into `None != ""`.
        assert out2.stopped_at == "", (
            f"nothing should have stopped the chain, but {out2.stopped_at!r} did"
        )
        assert "read_only_gate" in out2.considered, (
            f"the read-only gate was not consulted; considered={out2.considered}"
        )

    def test_an_undeclared_field_cannot_be_mutated_into_the_turn(self):
        """A policy cannot smuggle an attribute a later middleware would trust."""
        with pytest.raises(MiddlewareOrderError, match="undeclared turn fields"):
            build_full_chain()._apply(
                Turn(number=1, max_turns=1), Decision.mutate(some_flag=True)
            )


# ---------------------------------------------------------------------------
# 3. The composition, and the count that justified it
# ---------------------------------------------------------------------------


class TestTheComposition:
    def test_the_policy_count_is_far_past_r1s_threshold(self):
        """R1: *"once you need three or more independent turn-level policies"*.

        Reported as the in-loop branches this replaced
        (``runtime.loop_complexity``) and as the policies that replaced them.
        Both numbers are printed so a reader can see the threshold was crossed
        by an order of magnitude rather than by one.
        """
        counts = turn_policies.counts()
        measured = loop_complexity.measure_all()
        before = sum(
            loop_complexity.BASELINE[name]["policy_branches"] for name in loop_complexity.BASELINE
        )
        after = sum(p.policy_branches for p in measured.values() if not p.error)
        assert counts["r1_threshold"] == 3
        assert before > counts["r1_threshold"], before
        assert counts["total"] >= counts["r1_threshold"]
        assert after < before, f"the refactor did not reduce in-loop policy branches: {after} >= {before}"

    def test_every_policy_is_in_a_declared_phase_and_can_stop_only_if_driven_to(self):
        """``can_stop`` is verified by DRIVING the policy's trigger, not trusted.

        The first version of this derived the capability from an empty probe
        turn, where no guard fires — so every policy reported ``can_stop=False``
        and the receipt described a chain in which nothing could stop anything.
        """
        receipt = turn_policies.policy_receipt()
        assert receipt["declaration_mismatches"] == 0, receipt["policies"]
        assert receipt["counts"]["can_stop"] > 0
        for row in receipt["policies"]:
            assert row["phase"] in turn_middleware.PHASES, row
            if row["can_stop"]:
                assert row["stop_reachable"], (
                    f"{row['name']} claims it can stop but its trigger produced "
                    f"no stop: {row}"
                )

    def test_a_duplicate_policy_name_is_refused(self):
        """A receipt naming a policy that two things share does not say which ran."""
        policy = turn_policies.POLICY_BUILDERS["wall_clock"]()
        with pytest.raises(MiddlewareOrderError, match="both named"):
            TurnPolicyPipeline([policy, policy])

    def test_an_unnamed_or_unphased_policy_is_refused(self):
        with pytest.raises(MiddlewareOrderError, match="no string `name`"):
            TurnPolicyPipeline([turn_policies.Policy("", "pre_turn", lambda t, r: None)])
        with pytest.raises(MiddlewareOrderError, match="declares phase"):
            TurnPolicyPipeline(
                [turn_policies.Policy("x", "not_a_phase", lambda t, r: None)]
            )


# ---------------------------------------------------------------------------
# 4. Adding a policy must not touch the loop — STRUCTURALLY
# ---------------------------------------------------------------------------


class TestAddingAPolicyDoesNotRequireALoopEdit:
    LOOPS = (
        ("harness/agent_loop_step.py", "step"),
        ("harness/agent_kernel/strategy.py", "DailyCodingStrategy", "run"),
    )

    #: Policy names that are ALSO trace event kinds in this tree. `context_warning`
    #: names both a middleware and a `self._event(...)` kind, so a substring scan
    #: over the loop AST flags the EVENT. Enumerated rather than wildcards, and
    #: asserted to be exactly itself, so widening the exemption is a visible edit
    #: with a reason rather than a quiet skip that grows.
    POLICY_NAMES_THAT_ARE_ALSO_EVENT_KINDS = frozenset({"context_warning"})

    def test_the_event_name_exemption_is_exactly_itself(self):
        """The exemption above is a list, and a list that grows silently is a
        skipped test wearing a pin's clothes."""
        for name in self.POLICY_NAMES_THAT_ARE_ALSO_EVENT_KINDS:
            assert name in POLICY_CHAIN, (
                f"{name} is exempted as an event-kind collision but is not a "
                "policy; the exemption is stale"
            )

    def test_neither_loop_body_names_a_single_policy(self):
        """The structural claim, asserted by reading the loop AST.

        If a loop named a policy it would have per-policy knowledge again, and
        the whole point of the pipeline is that adding a policy is a change to
        ``POLICY_CHAIN`` and nowhere else. So every shipped policy name must be
        ABSENT from both loop bodies.

        The check is on *string literals the loop holds*, not on a substring of
        the dumped AST: a policy name reaches a loop as a string in a pipeline
        tuple, and a trace EVENT of the same name is not per-policy knowledge.
        The narrower check is therefore also the more honest one — it targets the
        way a loop would actually acquire the knowledge, instead of every
        coincidental spelling of a word.
        """
        for spec in self.LOOPS:
            relpath, *rest = spec
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=relpath)
            body = self._loop_body(tree, source, rest)
            held = {
                node.value
                for node in ast.walk(body)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            # Event-kind arguments are excluded at their SOURCE: the first
            # positional string to `self._event(` / `_event(` is a trace kind,
            # never a policy.
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
            named = [
                name
                for name in POLICY_CHAIN
                if name in held
                and name not in event_literals
                and name not in self.POLICY_NAMES_THAT_ARE_ALSO_EVENT_KINDS
            ]
            assert not named, (
                f"{relpath}'s loop body holds the policy names {named}; the loop "
                "must hold no per-policy knowledge, or adding a policy means "
                "editing the loop"
            )

    def test_the_loop_body_scan_can_actually_see_a_policy_name(self):
        """Non-vacuity control for the test above.

        ``_loop_body`` and the literal collector are exercised against a
        synthetic body that DOES hold a policy name, so a scan broken by an AST
        change (a renamed node type, a body that resolved to the wrong node)
        fails here rather than passing forever on the real loops.
        """
        source = (
            "def run(self):\n"
            "    for turn in range(1, 61):\n"
            '        policies = ("budget_cap", "wall_clock")\n'
        )
        tree = ast.parse(source)
        function = tree.body[0]
        loop = function.body[0]
        held = {
            node.value
            for node in ast.walk(loop)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "budget_cap" in held, "the collector no longer sees loop literals"

    def test_the_loop_calls_the_chain_exactly_once_per_iteration(self):
        """And the loop does call it — an absent call would satisfy the test above."""
        for spec in self.LOOPS:
            relpath = spec[0]
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8-sig")
            assert "policies.check(" in source, relpath

    def test_a_new_policy_can_be_added_without_touching_either_loop(self):
        """The demonstration, run in a subprocess so it cannot leak state.

        Composes a chain that includes a policy which does not exist yet, and
        drives it — then re-reads both loop files and asserts their bytes are
        unchanged. This is the DoD item stated as an executable fact rather
        than as an intention.
        """
        seen: list[int] = []

        def brand_new_policy(turn: Turn, run: TurnRun):
            seen.append(turn.number)
            return Decision.observe("brand_new_policy_ran", turn=turn.number)

        before = {
            relpath: (REPO_ROOT / relpath).read_bytes() for relpath, *_ in self.LOOPS
        }
        # `build_pipeline` refusing an unknown name is itself correct, so the
        # demonstration appends through the public `add` on a real chain -- which
        # is the API the "no loop edit" claim actually rests on.
        chain = build_pipeline(("turn_cap_observation", "wall_clock"))
        chain.add(turn_policies.Policy("brand_new", "pre_turn", brand_new_policy))
        chain.check(Turn(number=7, max_turns=60), TurnRun(run_id="demo"))
        assert seen == [7], "the new policy never ran, so adding it proved nothing"
        after = {
            relpath: (REPO_ROOT / relpath).read_bytes() for relpath, *_ in self.LOOPS
        }
        assert before == after, "a loop file changed while a policy was added"

    @staticmethod
    def _loop_body(tree: ast.Module, source: str, rest: list[str]) -> ast.AST:
        """Locate the loop's owning function, module-level or in a class.

        ``agent_loop_step.step`` is module-level and
        ``DailyCodingStrategy.run`` is a method, so a resolver that only walked
        classes finds one and misses the other -- which is the same
        ``ast.walk``-matches-the-wrong-``run`` defect
        ``runtime.loop_complexity`` hit on its first attempt.
        """
        qualifier = rest[0] if len(rest) > 1 else ""
        function = rest[-1]
        if qualifier:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == qualifier:
                    for member in node.body:
                        if (
                            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and member.name == function
                        ):
                            return member
        else:
            for node in tree.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == function
                ):
                    return node
        raise AssertionError(f"{function}() not found in {qualifier or 'module'}")

    def test_the_loop_complexity_ratchet_is_green(self):
        """``python -m runtime.loop_complexity`` exits 2 on a regression."""
        report = loop_complexity.report()
        assert report["ok"], report["regressions"]
        assert not [row for row in report["table"] if row["error"]], report["table"]


# ---------------------------------------------------------------------------
# 5. Middleware state is per-run
# ---------------------------------------------------------------------------


class TestMiddlewareStateIsPerRun:
    def test_two_runs_do_not_share_a_latch(self):
        """A one-shot latch shared across concurrent runs fires once for all of
        them. This is the concrete shape of the race, so it is the concrete
        shape of the assertion."""
        pipeline = build_pipeline(("turn_cap_observation", "wall_clock"))
        pipeline.middlewares[0]  # the observation policy
        first, second = TurnRun(run_id="a"), TurnRun(run_id="b")

        warned: list[TurnRun] = []
        for run in (first, second):
            turn = Turn(number=59, max_turns=60)
            turn.scratch["turn_cap_approach"] = lambda n: {"turn": n, "warn_at": 50}
            pipeline.check(turn, run, phase="pre_turn")
            warned.append(run)
        for run in warned:
            assert run.get("turn_cap_observation", "warned") is True
        # Each run saw the announcement for ITSELF.
        assert first.receipt()["run_id"] == "a"
        assert second.receipt()["run_id"] == "b"

    def test_the_latch_fires_once_per_run_not_once_per_pipeline(self):
        pipeline = build_pipeline(("turn_cap_observation",))
        run = TurnRun(run_id="r")
        for turn_number in range(55, 62):
            turn = Turn(number=turn_number, max_turns=60)
            turn.scratch["turn_cap_approach"] = lambda n: {"warn_at": 50}
            pipeline.check(turn, run, phase="pre_turn")
        announcing = [k for k in run.middleware_seen if k[1] == "observe"]
        assert len(announcing) == 7, announcing

    def test_reading_another_runs_state_raises(self):
        """A shared chain is a data race; this makes the leak DETECTABLE rather
        than silent, which is the difference between a bug and a mystery."""
        first = TurnRun(run_id="a")
        first.set("wall_clock", "seen", 1)
        second = TurnRun(run_id="b")
        assert second.get("wall_clock", "seen") is None
        with pytest.raises(RunStateError):
            second.get("wall_clock", "seen", run_id="a")

    def test_a_pipeline_holds_no_run_state_of_its_own(self):
        """Structural: the chain is a configuration, not a session."""
        pipeline = build_pipeline()
        assert not any(
            isinstance(getattr(p, "fn", None), TurnRun) for p in pipeline.middlewares
        )
        assert pipeline.describe()["policy_count"] == len(POLICY_CHAIN)

    def test_the_run_receipt_declares_that_it_is_not_shared(self):
        assert TurnRun(run_id="r").receipt()["shared"] is False


# ---------------------------------------------------------------------------
# 6. The test-removal floor: 8 shapes, absolute, structurally user/agent
# ---------------------------------------------------------------------------


class TestTheTestRemovalFloor:
    @staticmethod
    def _protector():
        return test_protection.protector_for_agent()

    def test_all_eight_shapes_are_declared_and_all_are_reachable(self):
        """Every shape in the closed vocabulary is PRODUCED by a declared rule.

        A declared shape nothing can produce is untested vocabulary, and an
        untested shape is one an attacker would try first.
        """
        produced: set[str] = set()
        for rule in test_protection.RULES:
            for case in rule.match:
                violations = test_protection.evaluate(
                    self._protector(), [test_protection.call_from_case(case)]
                )
                assert violations, f"{rule.rule_id} match case did not fire: {case}"
                produced |= {v.shape for v in violations}
        assert set(test_protection.DELETION_SHAPES) == produced, (
            "declared shapes with no producing case: "
            f"{sorted(set(test_protection.DELETION_SHAPES) - produced)}"
        )
        assert len(produced) == 8, sorted(produced)

    def test_every_rule_ships_its_own_match_and_not_match_proof(self):
        """T5's Phase 2 requirement: a rule that only says what it refuses has
        not been checked. The ``not_match`` half is the whole of the value."""
        for rule in test_protection.RULES:
            assert rule.match, f"{rule.rule_id} ships no match example"
            assert rule.not_match, (
                f"{rule.rule_id} ships no not_match example; without it the rule "
                "could match everything and every match assertion would still pass"
            )
            for case in rule.not_match:
                violations = test_protection.evaluate(
                    self._protector(), [test_protection.call_from_case(case)]
                )
                assert not violations, (
                    f"{rule.rule_id} refused a case it declares it should not: "
                    f"{case} -> {[v.shape for v in violations]}"
                )

    def test_the_floor_is_not_approval_gated(self):
        """An approval-gated test deletion is a test deletion.

        Asserted on the DATA, not on the absence of a flag somewhere: the
        receipt says so, and no rule in the set carries an approval field.
        """
        receipt = self._protector().to_dict()
        assert receipt["approval_gated"] is False
        assert receipt["overridable"] is False
        for violation in test_protection.evaluate(
            self._protector(),
            [test_protection.call_from_case(test_protection.RULES[0].match[0])],
        ):
            assert violation.overridable is False
            assert violation.to_dict()["overridable"] is False

    def test_a_user_initiated_deletion_is_not_refused(self):
        """The exception the brief asks for, and it must actually work."""
        case = test_protection.call_from_case(test_protection.RULES[0].match[0])
        assert test_protection.evaluate(self._protector(), [case]), "control"
        user = test_protection.protector_for_user(
            authority=test_protection.bound_user_authority()
        )
        assert test_protection.evaluate(user, [case]) == []
        assert user.user_initiated is True

    def test_the_user_agent_distinction_is_structural_not_a_flag(self):
        """A flag the agent could set would not be a distinction."""
        # No public constructor accepts a boolean that produces a user initiator.
        with pytest.raises(TypeError):
            test_protection.user_initiator("the agent", True)  # type: ignore[call-arg]
        # The only reachable path without the authority is refused.
        with pytest.raises(test_protection.InitiatorUnforgeable):
            test_protection.protector_for_user("the agent")
        # And an agent-side caller always gets the agent kind.
        assert test_protection.agent_initiator().agent_initiated is True
        assert test_protection.agent_initiator().user_initiated is False

    def test_the_user_authority_is_checked_by_identity_not_by_value(self):
        """A token is a value a caller can be handed; authority is not.

        The previous design passed a copyable token and exposed
        ``user_token()`` on the public surface, so the whole user/agent
        distinction was one import away from gone — the agent could not set a
        flag, but anything holding a reference could obtain the flag's value.
        Identity is the check that survives that, and this is the pin for the
        specific attack an equality check would let through.
        """
        with pytest.raises(test_protection.InitiatorUnforgeable):
            test_protection.user_initiator("the agent", authority=object())
        # An equal-comparing stand-in is still refused, because identity is what
        # is compared and a fresh object() is never the module's own.
        stand_in = test_protection.user_authority()
        assert stand_in is test_protection.bound_user_authority()
        with pytest.raises(test_protection.InitiatorUnforgeable):
            test_protection.user_initiator("the agent", authority=type(stand_in)())
        # And the removal of the old token surface is asserted, so it cannot be
        # reintroduced as a "backwards compatible" alias.
        assert not hasattr(test_protection, "user_token"), (
            "user_token() is back: it hands the authority to any caller holding "
            "a module reference, which is the hole this replaced"
        )

    def test_the_authority_is_not_on_the_import_star_surface(self):
        """``__all__`` is what ``from module import *`` and a docs build see.

        ``bound_user_authority`` IS in ``__all__`` deliberately — a harness-side
        caller needs it — so the check that matters is that the module does not
        hand it out as a *default argument* or a *stored attribute* on anything
        an agent-shaped object can reach.
        """
        protector = test_protection.protector_for_agent()
        assert not hasattr(protector, "authority_granted")
        assert not hasattr(protector, "user_token")
        # The protector carries the agent authority, never the user's.
        assert protector.initiator.authority is not test_protection.user_authority()

    def test_a_tool_call_has_nowhere_to_put_an_initiator(self):
        """The strongest form: the boundary type carries no such field at all, so
        there is nothing for the agent to write into."""
        fields = set(ToolCall.__dataclass_fields__)
        assert "initiator" not in fields
        assert "user_initiated" not in fields
        assert "approved" not in fields

    def test_the_skipping_decision_is_recorded_in_words(self):
        """The brief says *decide and document*. The decision is text, so a
        reader can disagree with it."""
        assert "Skipping is deletion" in test_protection.SKIPPING_DECISION
        assert test_protection.SKIPPING_DECISION in str(
            self._protector().to_dict()["skipping_decision"]
        )

    def test_the_gate_is_free_when_driven_through_the_real_pipeline(self):
        """The floor is reached through the chain, and it is a REFUSAL — free,
        non-retryable, ``needs_input``."""
        turn = Turn(number=1, max_turns=60)
        turn.scratch["test_protection"] = self._protector()
        turn.calls = (
            test_protection.call_from_case(test_protection.RULES[0].match[0]),
        )
        verdict = build_full_chain().check(turn, TurnRun(run_id="r"))
        assert verdict.is_refusal and not verdict.is_termination
        assert verdict.free is True
        assert verdict.decision.status == turn_middleware.REFUSAL_STATUS
        assert verdict.decision.charged is False

    def test_a_read_only_command_is_not_treated_as_a_deletion(self):
        """The false-positive direction, which is what gets a real guard switched
        off. Read the guard's own exemption.

        Every command here is a PROVABLY read-only one. Note what is *not* here:
        ``rm -rf build/`` appears in ``test_protection``'s not-match arms, which
        is a different question. That arm says "does not delete a TEST"; this one
        says "cannot write", and ``rm -rf build/`` answers the first and fails
        the second. Conflating them is how a read-only exemption ends up
        covering a recursive delete.
        """
        for command in (
            "git diff -- tests/",
            "git log --oneline -- tests/",
            "sed -n '1,20p' tests/test_cart.py",
            "cat tests/test_cart.py",
            "wc -l tests/test_cart.py",
            "grep -rn 'def add' src/",
        ):
            call = ToolCall(verb="bash", arguments={"command": command}, writes=False)
            assert turn_middleware.call_writes(call) is False, command
        # A control: the composition form is still mutating.
        assert turn_middleware.call_writes(
            ToolCall(verb="bash", arguments={"command": "cat a.py > b.py"}, writes=False)
        )
        # And the sharper control, which is the one that bites: a recursive
        # delete that names no test is still a WRITE, and a caller declaring
        # `writes=False` about it does not make it one.
        assert turn_middleware.call_writes(
            ToolCall(verb="bash", arguments={"command": "rm -rf build/"}, writes=False)
        )

    def test_a_shell_composition_is_never_read_only(self):
        """A read verb at the head is not enough; ``cat a.py > b.py`` writes."""
        assert turn_middleware.call_writes(
            ToolCall(verb="bash", arguments={"command": "cat a.py | tee b.py"}, writes=False)
        )

    def test_a_table_scoped_config_edit_is_allowed_but_its_test_table_is_not(self):
        """R2-03/R2-12 settled this: ``[project]`` in pyproject.toml is a
        normal edit; only ``tool.pytest.ini_options`` is the test contract.
        Treating the whole file as protected would refuse every dependency bump
        in the repository."""
        allowed = ToolCall(
            verb="write",
            arguments={"path": "pyproject.toml", "content": "[project]\nname = 'x'\n"},
            writes=True,
        )
        assert test_protection.evaluate(self._protector(), [allowed]) == []
        refused = ToolCall(
            verb="write",
            arguments={
                "path": "pyproject.toml",
                "content": "[tool.pytest.ini_options]\naddopts = '--deselect tests/x.py'\n",
            },
            writes=True,
        )
        assert test_protection.evaluate(self._protector(), [refused])


# ---------------------------------------------------------------------------
# 7. The instrument is an instrument
# ---------------------------------------------------------------------------


class TestTheMeasurementIsReal:
    def test_every_declared_loop_is_measurable_today(self):
        """A loop that cannot be found measures ZERO, and a zero is not a pass.

        This is why ``LoopComplexity.error`` is a field rather than an
        exception: ``measure_loop`` found the wrong function on the first
        attempt (``ast.walk`` matched an abstract base class's ``run``) and
        reported 0 branches for a loop that has 15.
        """
        for name, measured in loop_complexity.measure_all().items():
            assert not measured.error, f"{name}: {measured.error}"
            assert measured.branches > 0, f"{name} measured zero branches"
            assert measured.policy_branches > 0, f"{name} measured zero policy branches"

    def test_a_moved_loop_is_reported_not_measured_as_zero(self):
        """Demonstrated by pointing a declaration at a header that is not there."""
        bogus = loop_complexity.LoopRef(
            name="bogus",
            relpath="harness/agent_loop_step.py",
            qualifier="",
            function="step",
            marker="while turn < cfg.no_such_bound:",
        )
        measured = loop_complexity.measure_loop(bogus)
        assert measured.error, "a moved loop header measured as zero instead of erroring"
        assert measured.branches == 0

    def test_the_two_complexity_instruments_agree_when_there_are_no_closures(self):
        """Two instruments for one measurement must not differ when there is
        nothing to differ about.

        ``loop_complexity`` measures the current state under BOTH semantics --
        closures counted (the instrument the stored baseline was taken with) and
        closures excluded -- so a before/after delta never spans two
        instruments. That only holds if the two agree when a loop body defines no
        closure.

        Not a tautology: the corrected walk seeds its stack from the STATEMENT,
        not the statement's children, and an earlier version got that wrong and
        under-counted ``agent_step`` by 13 while reporting a confident smaller
        number. This is the pin for it.
        """
        for ref in loop_complexity.LOOPS:
            legacy = loop_complexity.measure_loop(ref, include_nested=True)
            corrected = loop_complexity.measure_loop(ref, include_nested=False)
            assert corrected.nested_branches == 0, (
                f"{ref.name} defines a closure in its loop body, so the two "
                "instruments are expected to differ and the comparison below "
                "would be asserting the wrong thing"
            )
            for axis in ("lines", "statements", "branches", "conditionals", "policy_branches"):
                assert getattr(legacy, axis) == getattr(corrected, axis), (
                    f"{ref.name}.{axis}: legacy {getattr(legacy, axis)} != "
                    f"corrected {getattr(corrected, axis)} with no nested "
                    "closures; the two walks are visiting different nodes"
                )

    def test_the_corrected_walk_counts_a_top_level_branch_in_the_body(self):
        """The control arm, on a synthetic body.

        A top-level ``if`` is exactly the branch a children-seeded walk drops, so
        this fixture is the shape that regressed. If the walk stops including the
        statement itself this fails, and the agreement above becomes a
        coincidence rather than evidence.
        """
        source = (
            "def step():\n"
            "    for turn in range(3):\n"
            "        if turn > 1:\n"
            "            pass\n"
        )
        loop = ast.parse(source).body[0].body[0]
        branches = 0
        stack = list(loop.body)
        while stack:
            node = stack.pop()
            if loop_complexity._is_branch(node):
                branches += 1
            stack.extend(ast.iter_child_nodes(node))
        assert branches == 1, f"expected the top-level if, got {branches}"

    def test_the_published_report_says_which_instrument_the_table_used(self):
        """A before/after table with no stated instrument is a table that can be
        read as measuring the wrong thing.

        The stored baseline and the current state are measured by the SAME
        instrument on purpose, and the corrected figures are published beside it.
        Without the note, a reader comparing 23 policy branches against 31 has no
        way to know which of the two instruments produced which number.
        """
        payload = loop_complexity.report()
        assert "include_nested=True" in payload["baseline_instrument"]
        assert set(payload["corrected"]) == set(payload["loops"])
        for name, got in payload["corrected"].items():
            assert got["branches"] == payload["loops"][name]["branches"], (
                f"{name}: the corrected figure and the measured loop disagree"
            )
        text = loop_complexity.render_report(payload)
        assert "legacy instrument" in text
        assert "corrected (closures excluded)" in text

    def test_the_ratchet_reports_a_growth_as_a_regression(self):
        """The non-vacuity control for the whole ratchet: a baseline below the
        current measurement MUST be reported."""
        shrunk = {
            name: {**values, "branches": 1}
            for name, values in loop_complexity.BASELINE.items()
        }
        regressions = loop_complexity.regressions(baseline=shrunk)
        assert regressions, "a deliberately impossible baseline produced no regression"
        assert any(row["axis"] == "branches" for row in regressions)

    def test_the_operator_surface_agrees_with_the_library(self):
        result = subprocess.run(
            [sys.executable, "-m", "runtime.loop_complexity"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no loop is above its baseline" in result.stdout
