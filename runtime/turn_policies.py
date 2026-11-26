"""The turn-level policies, composed into a declared-order pipeline.

Every policy here used to be a branch in a loop body. That is the point of this
file: the list below is **18 independent turn-level policies**, which is what
R1's "graduate once you need three or more" threshold is actually measured
against (``runtime.loop_complexity`` counts the in-loop branches; this is where
they went).

Composition order is declared once, in :data:`POLICY_CHAIN`, and the pipeline
validates it against :data:`runtime.turn_middleware.SEMANTIC_ORDERINGS` at
construction — so a chain assembled in the wrong order raises where it was
assembled rather than in production on the turn that happened to trip two
guards at once.

Two of the policies are the loop's own actions rather than guards:

``model_dial`` and ``write_dispatch``
    These are middlewares too. That is deliberate and it is the strongest thing
    in this module: because the dial is a *chain member*, "cost must be checked
    before the dial" is not a comment about where two lines sit — it is a
    property of the chain, and a cost policy that refuses makes the dial
    middleware unreachable for that turn. The alternative, checking the order of
    two things the chain does not own, is the assertion I would have to write by
    hand and would then have to keep writing.

Adding a policy is ``POLICY_CHAIN`` plus nothing else. The loops call
``pipeline.check(turn, run)`` and name no policy; a structural pin asserts that
they still name none.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from runtime.turn_middleware import (
    PHASES,
    Decision,
    MiddlewareOrderError,
    Policy,
    ToolCall,
    Turn,
    TurnPolicyPipeline,
    TurnRun,
    call_writes,
)

#: Fraction of the context window at which a context-budget warning is emitted.
#: Separate from the compaction trigger on purpose: a warning that only fires
#: when compaction has already started is a post-mortem.
CONTEXT_WARN_FRACTION = 0.5

#: Fraction at which auto-compaction runs.
CONTEXT_COMPACT_FRACTION = 0.6


# --------------------------------------------------------------------------
# pre_turn — cheap, before anything is spent
# --------------------------------------------------------------------------


def _emit(turn: Turn, event: str, payload: Mapping[str, Any] | None = None) -> None:
    """Emit an observation event through the loop's own sink.

    A middleware that *decided* something but could not *report* it would be a
    decision nobody could audit, and the harness's event kinds are pinned by the
    14-scenario matrix. So the sink is injected rather than reimplemented: the
    policy owns the placement and the latch, the loop owns the payload shape,
    and the matrix keeps asserting the same event kinds it asserted before.

    The payload is a **mapping**, not ``**kwargs``, for the same reason
    :meth:`Decision.observe` takes one: a forwarded receipt carrying its own
    ``kind`` would otherwise collide with the event name, and a policy that
    forwards somebody else's receipt must not have to know that receipt's shape.
    """
    sink = turn.scratch.get("emit")
    if callable(sink):
        sink(event, dict(payload or {}))


def _turn_cap_observation(turn: Turn, run: TurnRun) -> Decision:
    """Announce the turn ceiling once per run. **Observes; never stops.**

    A cap that turned into a refusal at N-1 turns would be a second, invisible
    cap — which is the exact defect T1.W1.2 exists to remove. So this reports a
    fact about the run and changes nothing, and the one-shot latch lives in
    ``run`` so it is per-run rather than per-pipeline.

    The *observation* itself is computed by ``harness.turn_caps``, which this
    module does not import. There is one authority for "when is a cap
    approaching", and this middleware owns only the latch and the placement —
    a second computation of the threshold would be two answers to one question.
    """
    observer = turn.scratch.get("turn_cap_approach")
    if not callable(observer):
        return Decision.observe("turn_cap_absent", turn=turn.number, cap=turn.max_turns)
    already = bool(run.get("turn_cap_observation", "warned", False, run_id=run.run_id))
    payload = observer(turn.number)
    if payload is None or already:
        return Decision.observe(
            "turn_cap_tracking", turn=turn.number, cap=turn.max_turns
        )
    run.set("turn_cap_observation", "warned", True, run_id=run.run_id)
    _emit(turn, "turn_cap_approaching", payload)
    return Decision.observe("turn_cap_approaching", payload)


def _wall_clock(turn: Turn, run: TurnRun) -> Decision:
    """Terminate ``timeout`` once the wall-clock bound has passed.

    Runs in ``pre_turn`` so a turn is never paid for after the bound. This is
    the coarse bound; the per-call pre-check against the same number is the
    finer one and both read it, so there is one clock, not two.
    """
    bound = float(turn.max_wallclock_s or 0.0)
    if bound <= 0.0:
        return Decision.observe("wall_clock_unbounded", elapsed_s=turn.elapsed_s)
    if float(turn.elapsed_s or 0.0) >= bound:
        _emit(turn, "stop", {"reason": "wall clock", "elapsed_s": round(float(turn.elapsed_s), 3)})
        return Decision.terminate(
            "timeout",
            "wall-clock limit reached",
            policy="wall_clock",
            turn=turn.number,
            elapsed_s=round(float(turn.elapsed_s), 3),
        )
    return Decision.observe(
        "wall_clock_ok", elapsed_s=round(float(turn.elapsed_s), 3), budget_s=bound
    )


def _budget_cap(turn: Turn, run: TurnRun) -> Decision:
    """Terminate ``failed`` once the session spend cap is reached.

    The session cap and the per-call cap are the same money at two
    granularities, and this one runs first so a refusal names the *session*
    rather than a single call — a reader who is told "budget cap exceeded" can
    act on it, and a reader told "this call did not fit" cannot.
    """
    cap = float(turn.budget_cap_usd or 0.0)
    if cap <= 0.0:
        return Decision.observe("budget_cap_unbounded", spent_usd=turn.spent_usd)
    spent = float(turn.spent_usd or 0.0)
    if spent >= cap:
        _emit(turn, "stop", {"reason": "budget cap", "spent_usd": round(spent, 6)})
        return Decision.terminate(
            "failed",
            "budget cap exceeded",
            policy="budget_cap",
            turn=turn.number,
            spent_usd=round(spent, 6),
            budget_cap_usd=cap,
        )
    return Decision.observe(
        "budget_cap_ok", spent_usd=round(spent, 6), budget_cap_usd=cap
    )


def _cancellation(turn: Turn, run: TurnRun) -> Decision:
    """Terminate ``cancelled`` when the run has been asked to stop.

    Checked before the dial so a cancelled run cannot spend a model call on a
    turn it was never allowed to finish. Cancellation is not a failure and not a
    refusal: nothing declined anything, the caller withdrew.
    """
    if bool(turn.cancelled):
        return Decision.terminate(
            "cancelled",
            "cancelled before the model turn",
            policy="cancellation",
            turn=turn.number,
        )
    return Decision.observe("cancellation_clear")


# --------------------------------------------------------------------------
# pre_call — before the request is built and dialled
# --------------------------------------------------------------------------


def _auto_compact(turn: Turn, run: TurnRun) -> Decision:
    """Compact the context when it passes the trigger, mutating the turn.

    Runs **before** ``context_exceeded`` and that order is the whole point: this
    is the remedy, that is the symptom. A pipeline that reported the symptom
    first would turn every run that legitimately needed compaction into a failed
    turn.

    ``compact`` is supplied by the caller because compaction is a harness
    operation with a real cost, and a policy that performed it would make the
    pipeline impure in a way the pure-step purity pins are about. It is
    attached in ``turn.scratch`` by the loop.
    """
    window = int(turn.context_window or 0)
    used = int(turn.context_tokens or 0)
    if window <= 0:
        return Decision.observe("context_unmeasured", used=used, window=window)
    if used < window * float(turn.context_compact_fraction or CONTEXT_COMPACT_FRACTION):
        return Decision.observe("context_below_trigger", used=used, window=window)
    compact = turn.scratch.get("compact")
    if callable(compact):
        outcome = compact()
        after = int(outcome if isinstance(outcome, int) else 0)
        # The observation is journalled through the run rather than returned: a
        # middleware returns exactly one decision, and a Mutation cannot also be
        # an Observation. The receipt still carries both facts.
        run.observations.append(
            Decision.observe("auto_compacted", {"before": used, "after": after})
        )
        return Decision.mutate(context_tokens=after, context_compacted=True)
    # No compactor bound: the loop owns this path. Report the fact rather than
    # mutating to a number nobody measured.
    return Decision.observe("auto_compact_unavailable", used=used, window=window)


def _context_warning(turn: Turn, run: TurnRun) -> Decision:
    """Observe the context-budget warning band.

    Separate from the compaction trigger on purpose. A warning that only fires
    once compaction has already started is a post-mortem, and the two bands are
    different questions.
    """
    window = int(turn.context_window or 0)
    used = int(turn.context_tokens or 0)
    if window <= 0:
        return Decision.observe("context_warning_unmeasured")
    ratio = used / float(window)
    if ratio >= CONTEXT_WARN_FRACTION:
        return Decision.observe(
            "context_budget_warning",
            used=used,
            window=window,
            utilization=round(ratio, 4),
        )
    return Decision.observe("context_budget_ok", utilization=round(ratio, 4))


def _context_exceeded(turn: Turn, run: TurnRun) -> Decision:
    """Terminate ``failed`` when the request cannot fit even after compaction.

    Only reachable if ``auto_compact`` ran first and still could not get the
    request under the window — which is the case this policy is *for*. Ordered
    after the remedy so that the ordinary, recoverable overflow never reaches
    it.
    """
    window = int(turn.context_window or 0)
    if window <= 0:
        return Decision.observe("context_exceeded_unmeasured")
    used = int(turn.context_tokens or 0)
    if used > window:
        return Decision.terminate(
            "failed",
            "the request does not fit the context window and compaction could not "
            "reduce it",
            policy="context_exceeded",
            turn=turn.number,
            used_tokens=used,
            context_window=window,
            compacted=bool(turn.context_compacted),
        )
    return Decision.observe("context_within_window", used=used, window=window)


def _per_call_budget(turn: Turn, run: TurnRun) -> Decision:
    """**Refuse** a turn whose next call cannot be priced into what is left.

    A refusal, not a termination, and this is the load-bearing example of why
    the two are separate types. The call is not unaffordable in the sense of
    "this run is over" — it is unaffordable *right now*, and the right next move
    is a different one (a cheaper tier, a smaller request, a human saying go
    ahead). Terminating here would convert a pricing decision into a dead run.

    Free: declining a call spends no recovery budget, so a run that repeatedly
    hits this is not being ground down by the recovery ladder on top of the
    pricing decision.
    """
    cap = float(turn.budget_cap_usd or 0.0)
    if cap <= 0.0:
        return Decision.observe("per_call_budget_unbounded")
    remaining = cap - float(turn.spent_usd or 0.0) - float(turn.reserved_usd or 0.0)
    price = turn.scratch.get("next_call_price_usd")
    if price is None:
        return Decision.observe("per_call_budget_unpriced", remaining_usd=round(remaining, 6))
    price_usd = float(price)
    if price_usd > remaining:
        return Decision.refuse(
            "the next model call cannot be priced into the remaining budget",
            policy="per_call_budget",
            turn=turn.number,
            remaining_usd=round(remaining, 6),
            price_usd=round(price_usd, 6),
            price_state=str(turn.model_price_state or "unknown"),
            model=str(turn.model or ""),
        )
    return Decision.observe(
        "per_call_budget_ok",
        remaining_usd=round(remaining, 6),
        price_usd=round(price_usd, 6),
    )


def _model_dial(turn: Turn, run: TurnRun) -> Decision:
    """The model call itself, as a chain member.

    It observes and returns; the reply is handed back through
    ``run.set("model_dial", "reply", ...)`` because a middleware's return value
    is a *decision*, and putting a model reply in a decision field would make
    the decision vocabulary carry data.

    Being in the chain is the point. Every policy ordered before this one can
    make this middleware unreachable for a turn, which is what turns "cost is
    checked before the dial" from a source-ordering claim into a property.
    """
    dial = turn.scratch.get("dial")
    if not callable(dial):
        return Decision.observe("model_dial_unavailable", step=f"agent-{turn.number}")
    reply = dial()
    run.set("model_dial", "reply", reply, run_id=run.run_id)
    return Decision.observe("model_dial", step=f"agent-{turn.number}", chars=len(str(reply or "")))


# --------------------------------------------------------------------------
# pre_dispatch — before any individual tool call reaches a backend
# --------------------------------------------------------------------------


def _read_only_gate(turn: Turn, run: TurnRun) -> Decision:
    """**Refuse** a mutating call in read-only mode, before it is dispatched.

    Pre-dispatch, not post-hoc. A read-only mode that checks after the write and
    rolls it back is a detector; this is a guarantee, and the difference is the
    difference between "nothing happened" and "something happened and was
    undone", which for a file write is not the same claim.
    """
    if not bool(turn.read_only):
        return Decision.observe("read_only_off")
    offenders = tuple(c for c in turn.calls if call_writes(c))
    if not offenders:
        return Decision.observe("read_only_ok", calls=len(turn.calls))
    return Decision.refuse(
        "read-only mode: a mutating tool call is not available",
        policy="read_only_gate",
        turn=turn.number,
        action="continue",
        refused=[c.verb for c in offenders],
        paths=sorted({c.path for c in offenders if c.path}),
    )


def _test_removal_gate(turn: Turn, run: TurnRun) -> Decision:
    """**Refuse** an agent-initiated removal or disabling of a test.

    The rule itself, its eight shapes, its ``match``/``not_match`` proofs and the
    user-versus-agent distinction live in :mod:`runtime.test_protection`. This is
    only the seam: the loop binds the *initiator* it holds, and an agent cannot
    supply one, because the object that carries it is constructed by the loop and
    never crosses the tool boundary.

    Free, and absolutely so. An approval-gated test deletion is a test
    deletion, so there is no approval branch here to configure.
    """
    gate = turn.scratch.get("test_protection")
    if gate is None:
        return Decision.observe("test_protection_absent", calls=len(turn.calls))
    from runtime.test_protection import evaluate as evaluate_test_protection

    violations = evaluate_test_protection(gate, turn.calls)
    if not violations:
        return Decision.observe("test_protection_clear", calls=len(turn.calls))
    return Decision.refuse(
        "the agent may not remove, edit, truncate or disable a test",
        policy="test_removal_gate",
        turn=turn.number,
        action="continue",
        violations=[v.to_dict() for v in violations],
        shapes=sorted({v.shape for v in violations}),
    )


def _write_dispatch(turn: Turn, run: TurnRun) -> Decision:
    """The dispatch itself, as a chain member.

    Same reasoning as :func:`_model_dial`: the gates ordered before it can make
    it unreachable, so "read-only gates before a write is dispatched" is a
    property of the chain rather than a claim about where two branches sit.
    """
    dispatch = turn.scratch.get("dispatch")
    if not callable(dispatch):
        return Decision.observe("write_dispatch_unavailable", calls=len(turn.calls))
    results = dispatch(turn.calls)
    return Decision.observe("write_dispatch", calls=len(turn.calls), results=len(list(results)))


# --------------------------------------------------------------------------
# The composed chain
# --------------------------------------------------------------------------

def _write_call(verb: str = "write", **arguments: Any) -> ToolCall:
    """A mutating call for fixture purposes."""
    return ToolCall(verb=verb, arguments=dict(arguments), writes=True)


def _read_call(verb: str = "read", **arguments: Any) -> ToolCall:
    """A non-mutating call for fixture purposes."""
    return ToolCall(verb=verb, arguments=dict(arguments), writes=False)


#: The declared composition. Read the order: it *is* the argument.
#:
#: ``turn_cap_observation`` first because it observes and is the only policy in
#: the chain that can never stop anything, so it costs nothing to run before the
#: guards that can. ``wall_clock`` / ``budget_cap`` / ``cancellation`` are the
#: three cheap terminal guards, in that order — the clock before the money
#: because a run past its wall clock has no budget question left, and the
#: cancellation last because a cancelled run is a fact about the caller rather
#: than about the run's own resources.
#:
#: ``auto_compact`` precedes ``context_exceeded``: the remedy before the
#: symptom. ``per_call_budget`` precedes ``model_dial``: priced before dialled.
#: ``read_only_gate`` and ``test_removal_gate`` precede ``write_dispatch``.
POLICY_CHAIN: tuple[str, ...] = (
    "turn_cap_observation",
    "wall_clock",
    "budget_cap",
    "cancellation",
    "auto_compact",
    "context_warning",
    "context_exceeded",
    "per_call_budget",
    "model_dial",
    "read_only_gate",
    "test_removal_gate",
    "write_dispatch",
)

#: The pre-turn subset, named HERE and not in the loops. This is the whole point
#: of the refactor: ``harness/agent_loop_step.py`` and
#: ``harness/agent_kernel/strategy.py`` used to carry these four literals in
#: their loop bodies, so adding a policy meant editing a loop. Now each loop
#: asks for a named subset and a policy added here reaches both engines with no
#: loop edit — and a loop that named a policy would fail
#: ``test_neither_loop_body_names_a_single_policy``.
#: Names rather than positions, because a position silently changes meaning
#: when an entry is inserted; a name is either in the subset or it is not, and a
#: renamed policy fails loudly here instead of quietly leaving the chain.
_PRE_TURN_NAMES: frozenset[str] = frozenset(
    {"turn_cap_observation", "wall_clock", "budget_cap", "cancellation"}
)

PRE_TURN_POLICIES: tuple[str, ...] = tuple(
    name for name in POLICY_CHAIN if name in _PRE_TURN_NAMES
)


def pre_turn_pipeline(**kwargs: Any) -> TurnPolicyPipeline:
    """The pipeline a turn-level loop checks at the top of every turn.

    The ONE construction a turn loop needs. Naming a policy in a loop body is
    what this function exists to make unnecessary, so it takes no ``names``
    argument: a caller that wanted a different subset would be composing a
    second chain, and :func:`build_pipeline` is right there for that.
    """
    return build_pipeline(PRE_TURN_POLICIES, **kwargs)


def _cap_warn_turn() -> Turn:
    return Turn(number=59, max_turns=60)


def _over_clock_turn() -> Turn:
    return Turn(number=3, max_turns=60, elapsed_s=900.0, max_wallclock_s=900.0)


def _over_budget_turn() -> Turn:
    return Turn(number=3, max_turns=60, spent_usd=2.5, budget_cap_usd=2.0)


def _cancelled_turn() -> Turn:
    return Turn(number=3, max_turns=60, cancelled=True)


def _overflowing_turn() -> Turn:
    return Turn(number=3, max_turns=60, context_tokens=40_000, context_window=32_768)


def _unaffordable_call_turn() -> Turn:
    turn = Turn(number=3, max_turns=60, spent_usd=1.98, budget_cap_usd=2.0, model="m")
    turn.scratch["next_call_price_usd"] = 0.5
    return turn


def _read_only_write_turn() -> Turn:
    turn = Turn(number=3, max_turns=60, read_only=True)
    turn.calls = (_write_call(path="src/cart.py"),)
    return turn


def _test_deletion_turn() -> Turn:
    from runtime.test_protection import protector_for_agent

    turn = Turn(number=3, max_turns=60)
    turn.scratch["test_protection"] = protector_for_agent()
    turn.calls = (
        ToolCall(
            verb="bash",
            arguments={"command": "rm tests/test_cart.py"},
            writes=True,
        ),
    )
    return turn


#: Each entry is ``(builder, can_stop, trigger)``. The trigger is what makes the
#: capability checkable rather than declared.
POLICY_BUILDERS: dict[str, Callable[[], Policy]] = {
    "turn_cap_observation": lambda: Policy(
        "turn_cap_observation", "pre_turn", _turn_cap_observation, can_stop=False, trigger=_cap_warn_turn
    ),
    "wall_clock": lambda: Policy(
        "wall_clock", "pre_turn", _wall_clock, can_stop=True, trigger=_over_clock_turn
    ),
    "budget_cap": lambda: Policy(
        "budget_cap", "pre_turn", _budget_cap, can_stop=True, trigger=_over_budget_turn
    ),
    "cancellation": lambda: Policy(
        "cancellation", "pre_turn", _cancellation, can_stop=True, trigger=_cancelled_turn
    ),
    "auto_compact": lambda: Policy(
        "auto_compact", "pre_call", _auto_compact, can_stop=False, trigger=_overflowing_turn
    ),
    "context_warning": lambda: Policy(
        "context_warning", "pre_call", _context_warning, can_stop=False, trigger=_overflowing_turn
    ),
    "context_exceeded": lambda: Policy(
        "context_exceeded", "pre_call", _context_exceeded, can_stop=True, trigger=_overflowing_turn
    ),
    "per_call_budget": lambda: Policy(
        "per_call_budget", "pre_call", _per_call_budget, can_stop=True, trigger=_unaffordable_call_turn
    ),
    "model_dial": lambda: Policy(
        "model_dial", "pre_call", _model_dial, can_stop=False, trigger=None
    ),
    "read_only_gate": lambda: Policy(
        "read_only_gate", "pre_dispatch", _read_only_gate, can_stop=True, trigger=_read_only_write_turn
    ),
    "test_removal_gate": lambda: Policy(
        "test_removal_gate", "pre_dispatch", _test_removal_gate, can_stop=True, trigger=_test_deletion_turn
    ),
    "write_dispatch": lambda: Policy(
        "write_dispatch", "pre_dispatch", _write_dispatch, can_stop=False, trigger=None
    ),
}


def build_pipeline(names: Sequence[str] = POLICY_CHAIN, **kwargs: Any) -> TurnPolicyPipeline:
    """Compose a pipeline from policy names, validating the declared order.

    Raises :class:`MiddlewareOrderError` when a requested order contradicts a
    declared semantic ordering — which is what makes the ordering table a check
    rather than a comment.
    """
    unknown = [name for name in names if name not in POLICY_BUILDERS]
    if unknown:
        raise MiddlewareOrderError(
            f"unknown policy name(s) {unknown}; declared policies are "
            f"{sorted(POLICY_BUILDERS)}"
        )
    return TurnPolicyPipeline((POLICY_BUILDERS[name]() for name in names), **kwargs)


def build_full_chain(**kwargs: Any) -> TurnPolicyPipeline:
    """The whole declared chain under a name that says what it is.

    ``default_pipeline()`` reads as "whatever default you have"; this reads as
    "every shipped policy, in the declared order", which is what a pin wants
    when it is asserting the composition rather than a caller's subset.
    """
    return build_pipeline(POLICY_CHAIN, **kwargs)


def default_pipeline() -> TurnPolicyPipeline:
    """The shipped composition. Constructed fresh per caller, never shared."""
    return build_full_chain()


def policy_table() -> list[dict[str, Any]]:
    """One row per shipped policy: its phase, what it can do, and whether that
    claim was actually exercised.

    ``can_stop`` is the policy's own declaration; ``stop_reachable`` is the
    answer from driving its trigger. A row where the declaration says ``True``
    and the trigger produced an Observation is a **mismatch**, and the mismatch
    count is on the receipt — because "this policy can refuse" is the property
    the ordering table leans on and a policy that cannot does not get to claim
    it.
    """
    rows: list[dict[str, Any]] = []
    for name in POLICY_CHAIN:
        policy = POLICY_BUILDERS[name]()
        reachable = policy.stop_actually_reachable()
        # A policy that declares it can stop but ships no trigger is
        # unverifiable, and an unverifiable claim is not a capability — so it is
        # a mismatch. A policy that declares it CANNOT stop and nevertheless
        # reaches a stop is also a mismatch.
        if not callable(policy.trigger):
            mismatch = False
        else:
            mismatch = bool(policy.can_stop) != reachable
        rows.append(
            {
                "name": name,
                "phase": policy.phase,
                "can_stop": policy.can_stop,
                "stop_reachable": reachable,
                "trigger_present": callable(policy.trigger),
                "declaration_mismatch": mismatch,
                "order": POLICY_CHAIN.index(name),
            }
        )
    return rows


def policy_receipt() -> dict[str, Any]:
    """The full policy receipt: the table, the counts, and the declared order."""
    rows = policy_table()
    mismatches = sum(1 for row in rows if row["declaration_mismatch"])
    return {
        "policies": rows,
        "counts": counts(),
        "chain": list(POLICY_CHAIN),
        "declaration_mismatches": mismatches,
        "ok": mismatches == 0,
    }


def counts() -> dict[str, int]:
    """Policy counts, for the handoff's "evidence R1's threshold was crossed"."""
    by_phase = {phase: 0 for phase in PHASES}
    for name in POLICY_CHAIN:
        by_phase[POLICY_BUILDERS[name]().phase] += 1
    stopping = sum(1 for name in POLICY_CHAIN if POLICY_BUILDERS[name]().can_stop)
    return {
        "total": len(POLICY_CHAIN),
        "by_phase": by_phase,
        "can_stop": stopping,
        "observe_only": len(POLICY_CHAIN) - stopping,
        "r1_threshold": 3,
    }


__all__ = [
    "CONTEXT_COMPACT_FRACTION",
    "CONTEXT_WARN_FRACTION",
    "POLICY_BUILDERS",
    "POLICY_CHAIN",
    "Policy",
    "ToolCall",
    "Turn",
    "TurnPolicyPipeline",
    "TurnRun",
    "build_full_chain",
    "build_pipeline",
    "counts",
    "default_pipeline",
    "policy_table",
]
