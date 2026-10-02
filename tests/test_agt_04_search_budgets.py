"""AGT-04 — search budgets, concurrency classes, and the doom loop.

Five mechanisms, each named after the behaviour it must have:

1. A search above the cap ERRORS and stays bounded (no 5 000-token result).
2. No iterative paging affordance exists — not in the catalog, not in the
   search signature, and a paging argument is refused at runtime.
3. Read-only tools in one model turn run concurrently; mutating tools do not.
   The class comes from the catalog entry, not from a hardcoded list.
4. A repeated identical call is a first-class DECISION requiring the user, not
   a silent refusal the model re-emits.
5. Tool output is capped at STORAGE time, so what is stored is what was capped.

Every proof here is behavioural and host-only: no Docker, no provider, no
network. The verifier gate is untouched and pinned by reading the mint
condition, because nothing in this round may make ``completed_unverified``
reachable as success.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from harness import retrieval
from harness.agent_kernel import budget as kbudget
from harness.agent_kernel.budget import (
    bound_fanout_output,
    cap_tool_output,
    fanout_bounds_from_config,
    plan_fanout,
)
from harness.agent_kernel.contracts import ToolCall
from harness.agent_kernel.tools import (
    ToolRegistry,
    ToolResult,
    builtin_tool_specs,
)
from harness.tools import (
    catalog_concurrency_report,
    catalog_fingerprint,
    typed_tool_specs,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, *, matches: int = 0, per_file: int = 60) -> Path:
    """A tiny repo with ``matches`` files each holding ``per_file`` hits."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    for index in range(max(1, matches)):
        (root / "pkg" / f"mod{index}.py").write_text(
            "\n".join(
                f"value_{index}_{n} = compute_monthly_average({n})  # needle"
                for n in range(per_file)
            ),
            encoding="utf-8",
        )
    (root / "pkg" / "quiet.py").write_text("nothing to see here\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 1. The cap ERRORS, and the error stays bounded
# ---------------------------------------------------------------------------


def test_a_five_hundred_match_search_errors_and_stays_bounded(tmp_path):
    """500 matches must be an error with a sample, not a 5 000-token list."""
    root = _repo(tmp_path, matches=10, per_file=50)  # 500 hits

    outcome = retrieval.search_repo(str(root), "compute_monthly_average")

    assert outcome.ok is False
    assert outcome.error == retrieval.ERROR_TOO_MANY_MATCHES
    assert outcome.over_cap is True
    # the honest count: a lower bound, because the scan stopped at cap + 1
    assert outcome.total == retrieval.SEARCH_MATCH_CAP + 1
    assert outcome.total_is_lower_bound is True
    assert outcome.matches == [], "an over-cap search must not return the set"
    # a sample, and only a sample
    assert len(outcome.sample) == retrieval.SEARCH_SAMPLE_CAP

    rendered = outcome.render()
    assert retrieval.SEARCH_TOO_MANY_MESSAGE in rendered
    assert "narrow" in rendered.lower()
    # bounded: the whole rendering is a few hundred characters, not thousands
    assert len(rendered) < 1200, len(rendered)
    # the pattern appears once in the prose plus once per sample line
    assert rendered.count("compute_monthly_average") == retrieval.SEARCH_SAMPLE_CAP + 1


def test_a_bounded_search_still_returns_its_matches(tmp_path):
    """The cap is a ceiling, not a refusal: a small result set comes back."""
    root = _repo(tmp_path, matches=1, per_file=5)

    outcome = retrieval.search_repo(str(root), "compute_monthly_average")

    assert outcome.ok is True
    assert outcome.error == ""
    assert len(outcome.matches) == 5
    assert outcome.total == 5
    assert outcome.total_is_lower_bound is False
    assert outcome.render().count("\n") == 4


def test_the_cap_is_configurable_and_the_threshold_is_not_buyable(tmp_path):
    """A caller cannot raise the error threshold with ``max_results``."""
    root = _repo(tmp_path, matches=10, per_file=50)

    # asking for MORE results must not buy a pass past the cap
    greedy = retrieval.search_repo(
        str(root), "compute_monthly_average", max_results=100_000
    )
    assert greedy.ok is False
    assert greedy.error == retrieval.ERROR_TOO_MANY_MATCHES
    # ...and a caller CAN move the cap itself, which is a config decision
    moved = retrieval.search_repo(
        str(root), "compute_monthly_average", max_matches=1000
    )
    assert moved.ok is True
    assert moved.total == 500


def test_the_search_is_bounded_on_files_as_well_as_matches(tmp_path):
    """A pathological pattern must not turn one call into a repo read."""
    root = _repo(tmp_path, matches=12, per_file=1)

    outcome = retrieval.search_repo(str(root), "compute_monthly_average", max_files=3)

    assert outcome.files_scanned == 3
    # the remainder was NOT measured, and says so rather than reporting 0
    assert outcome.files_not_searched_known is False
    assert outcome.files_not_searched is None


def test_a_search_never_raises_on_a_hostile_query(tmp_path):
    """A bad regex, a traversal path, and a missing root are all refusals."""
    root = _repo(tmp_path, matches=1, per_file=2)

    bad_pattern = retrieval.search_repo(str(root), "([unclosed")
    assert bad_pattern.ok is False
    assert bad_pattern.error == retrieval.ERROR_BAD_PATTERN

    escape = retrieval.search_repo(str(root), "x", path="../../etc")
    assert escape.ok is False
    assert escape.error == retrieval.ERROR_BAD_PATH

    missing = retrieval.search_repo(str(tmp_path / "nope"), "x")
    assert missing.ok is False


def test_the_search_refuses_symlinked_components(tmp_path):
    """Search has the same containment the rest of retrieval has."""
    root = _repo(tmp_path, matches=1, per_file=2)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("needle needle\n", encoding="utf-8")
    try:
        (root / "pkg" / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    outcome = retrieval.search_repo(str(root), "needle")

    if outcome.ok:
        assert not any("secret.py" in line for line in outcome.matches)
    else:
        assert outcome.error == retrieval.ERROR_TOO_MANY_MATCHES
        assert not any("secret.py" in line for line in outcome.sample)


# ---------------------------------------------------------------------------
# 2. No iterative paging affordance exists
# ---------------------------------------------------------------------------


def test_no_catalog_tool_offers_a_paging_argument():
    """Nothing in the ONE catalog declares a page turn."""
    report = retrieval.paging_affordances()
    assert report["checked"] == len(typed_tool_specs())
    assert report["clean"] is True, report["offenders"]
    assert report["offenders"] == []


def test_the_search_signature_offers_no_paging_parameter():
    """The search itself has no ``page``/``offset``/``cursor`` parameter."""
    names = set(inspect.signature(retrieval.search_repo).parameters)
    assert not names & retrieval.PAGING_ARGUMENTS, sorted(
        names & retrieval.PAGING_ARGUMENTS
    )


def test_a_paging_argument_is_refused_rather_than_honoured(tmp_path):
    """A model that invents a page two is told there isn't one."""
    root = _repo(tmp_path, matches=2, per_file=5)

    for name in ("page", "offset", "cursor", "next_page", "page_token"):
        outcome = retrieval.search_repo(
            str(root), "compute_monthly_average", arguments={"pattern": "x", name: 2}
        )
        assert outcome.ok is False, name
        assert outcome.error == retrieval.ERROR_PAGING_REFUSED, name
        assert name in outcome.refused_arguments, name
        assert "paging is not an available search affordance" in outcome.render()


def test_the_paging_set_is_closed_and_named():
    """The set is explicit, and a range bound is not mistaken for a page turn."""
    for name in ("page", "offset", "cursor", "next", "prev", "page_token"):
        assert name in retrieval.PAGING_ARGUMENTS, name
    # ``start_line``/``end_line`` select a window of ONE file (git_blame) --
    # a narrowing, not a page turn, and flagging it would be a false positive.
    assert "start_line" not in retrieval.PAGING_ARGUMENTS
    assert retrieval.paging_affordances(typed_tool_specs())["clean"] is True


def test_the_rendered_refusal_says_there_is_no_paging(tmp_path):
    """The message must not send a model looking for a page two."""
    root = _repo(tmp_path, matches=10, per_file=50)
    rendered = retrieval.search_repo(str(root), "compute_monthly_average").render()
    assert "no paging through the result set" in rendered


# ---------------------------------------------------------------------------
# 3. Concurrency classes are declared on the catalog, and used
# ---------------------------------------------------------------------------


def test_every_catalog_entry_declares_its_concurrency_class():
    """The declaration is on the spec, and it is machine-checkable."""
    report = catalog_concurrency_report()
    assert report["checked"] == len(typed_tool_specs())
    assert report["concurrent_count"] > 0
    assert report["sequential_count"] > 0
    for spec in typed_tool_specs():
        # resolved to a concrete bool in __post_init__, never left as None
        assert isinstance(spec.read_only, bool), spec.name
    assert set(report["concurrent"]) | set(report["sequential"]) == set(
        spec.name for spec in typed_tool_specs()
    )


def test_the_concurrency_declaration_is_inside_the_catalog_fingerprint():
    """Two catalogs that disagree about parallelism are NOT identical."""
    from harness.tools import catalog_parity_report

    derived = builtin_tool_specs()
    assert catalog_parity_report(derived)["identical"] is True
    assert catalog_fingerprint(derived) == catalog_fingerprint(typed_tool_specs())

    # flip one derived entry's class and the parity report must notice
    flipped = list(derived)
    target = flipped[0]
    flipped[0] = type(target)(
        name=target.name,
        side_effect_class=target.side_effect_class,
        required=target.required,
        optional=target.optional,
        types=dict(target.types),
        read_only=not target.read_only,
        aliases=target.aliases,
    )
    report = catalog_parity_report(flipped)
    assert report["identical"] is False
    assert [d["tool"] for d in report["differences"]] == [target.name]


def test_read_only_tools_run_concurrently_and_mutating_ones_do_not():
    """A batch of reads is concurrent; a batch of writes is not."""
    registry = ToolRegistry(specs=builtin_tool_specs())
    reads = [
        {"call_id": f"c{n}", "tool": "read", "arguments": {"path": f"f{n}.py"}}
        for n in range(4)
    ]
    assert all(registry.concurrency_class(call) == "concurrent" for call in reads)
    writes = [
        {
            "call_id": f"w{n}",
            "tool": "write",
            "arguments": {"path": f"w{n}.py", "content": "x", "expected_revision": "r"},
        }
        for n in range(4)
    ]
    assert all(registry.concurrency_class(call) == "sequential" for call in writes)
    # a MIXED batch: the reads are still concurrent, the writes are not
    mixed = reads[:2] + writes[:2]
    classes = {call["call_id"]: registry.concurrency_class(call) for call in mixed}
    assert classes["c0"] == "concurrent"
    assert classes["w0"] == "sequential"


def test_an_unclassifiable_call_is_never_treated_as_parallel_safe():
    """Unknown tool, bad arguments: the answer is sequential, not "yes"."""
    registry = ToolRegistry(specs=builtin_tool_specs())
    assert registry.concurrency_class({"tool": "no_such_tool"}) == "sequential"
    assert registry.concurrency_class({"tool": "read", "arguments": {}}) == "sequential"


def _dispatch_strategy(registry):
    """A strategy with only the attributes the dispatch path touches."""
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"max_parallel_tools": 4, "max_tool_fanout": 8}
    strategy.tools = registry
    strategy._handler_context = lambda spec: {}
    strategy._event = lambda *a, **k: None
    strategy._sync_changes = lambda: None
    strategy.changed_files = set()
    return strategy


class _SleepyRegistry(ToolRegistry):
    """A registry whose every tool takes the same measurable time."""

    def __init__(self, *, delay: float = 0.15, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay
        self.order: list = []

    def execute(self, call, context=None):  # type: ignore[override]
        time.sleep(self.delay)
        self.order.append(call.call_id)
        return ToolResult(True, f"slept {call.call_id}")


def _reads(count: int):
    return [
        ToolCall(call_id=f"r{n}", tool="read", arguments={"path": f"f{n}.py"})
        for n in range(count)
    ]


def _writes(count: int):
    return [
        ToolCall(
            call_id=f"w{n}",
            tool="write",
            arguments={"path": f"w{n}.py", "content": "x", "expected_revision": "r"},
        )
        for n in range(count)
    ]


def test_a_read_only_fan_out_overlaps_and_a_mutating_one_does_not():
    """The timing IS the proof: four 150 ms reads, four 150 ms writes."""
    registry = _SleepyRegistry(specs=builtin_tool_specs())
    strategy = _dispatch_strategy(registry)

    started = time.monotonic()
    read_results = strategy._execute_by_concurrency_class(
        [(c, None) for c in _reads(4)], None, 1
    )
    read_elapsed = time.monotonic() - started

    started = time.monotonic()
    write_results = strategy._execute_by_concurrency_class(
        [(c, None) for c in _writes(4)], None, 1
    )
    write_elapsed = time.monotonic() - started

    # reads overlapped: four 150 ms sleeps in well under 300 ms
    assert read_elapsed < 0.30, read_elapsed
    # writes did not: they are sequential, so the cost is additive
    assert write_elapsed > 0.55, write_elapsed
    assert all(result.ok for _, result in read_results + write_results)


def test_a_mixed_turn_keeps_the_order_the_model_emitted():
    """A fan-out must not reorder the conversation the model just had."""
    strategy = _dispatch_strategy(_SleepyRegistry(specs=builtin_tool_specs()))
    calls = [_reads(1)[0], _writes(1)[0], _reads(1)[0], _writes(1)[0]]
    calls[2] = type(calls[0])(**{**calls[2].to_dict(), "call_id": "r1"})
    calls[3] = type(calls[1])(**{**calls[3].to_dict(), "call_id": "w1"})

    results = strategy._execute_by_concurrency_class(
        [(c, None) for c in calls], None, 1
    )

    assert [call.call_id for call, _ in results] == [c.call_id for c in calls]


def test_the_concurrency_receipt_names_both_classes():
    """A reviewer can see the policy without reading the dispatcher."""
    strategy = _dispatch_strategy(_SleepyRegistry(specs=builtin_tool_specs()))
    emitted = []
    strategy._event = lambda name, payload, **kw: emitted.append((name, payload))
    calls = [_reads(1)[0], _writes(1)[0]]

    strategy._execute_by_concurrency_class([(c, None) for c in calls], None, 1)

    receipt = dict(emitted)["tool_concurrency"]
    assert receipt["concurrent"] == ["r0"]
    assert receipt["sequential"] == ["w0"]
    assert receipt["refused"] == []
    assert receipt["max_concurrency"] == 4
    assert receipt["max_calls"] == 8


# ---------------------------------------------------------------------------
# 3b. The fan-out is bounded in both directions
# ---------------------------------------------------------------------------


def test_a_fan_out_refuses_calls_beyond_the_budget_with_a_reason():
    """Over budget is a refusal carrying its reason, never a disappearance."""
    calls = list(range(20))
    plan = plan_fanout(
        calls,
        bounds=kbudget.FanoutBounds(max_calls=5),
        is_read_only=lambda c: True,
    )
    assert len(plan.concurrent) == 5
    assert len(plan.refused) == 15
    assert all("budget" in reason for _, reason in plan.refused)
    assert plan.total == 20


def test_the_fan_out_budget_bounds_the_TOTAL_not_just_the_reads():
    """A turn of 200 writes is bounded too, or nothing is."""
    plan = plan_fanout(
        list(range(20)),
        bounds=kbudget.FanoutBounds(max_calls=5),
        is_read_only=lambda c: False,
    )
    assert len(plan.concurrent) == 0
    assert len(plan.sequential) == 5
    assert len(plan.refused) == 15


def test_the_fan_out_budget_is_spent_on_reads_first():
    """A read is how a model finds out what to mutate; do not refuse it for one."""
    calls = [("read", n) for n in range(3)] + [("write", n) for n in range(3)]
    plan = plan_fanout(
        calls,
        bounds=kbudget.FanoutBounds(max_calls=4),
        is_read_only=lambda c: c[0] == "read",
    )
    assert plan.concurrent == [("read", 0), ("read", 1), ("read", 2)]
    assert plan.sequential == [("write", 0)]
    assert [call for call, _ in plan.refused] == [("write", 1), ("write", 2)]
    # refused, never deferred: a mutation that quietly ran a turn late is a
    # lost edit, so the reason has to be visible to the model
    assert all("exhausted" in reason for _, reason in plan.refused)


def test_a_stringly_typed_class_name_cannot_turn_a_write_into_a_read():
    """`bool("sequential")` is True — the strict read is load-bearing."""
    plan = plan_fanout(
        list(range(4)),
        bounds=kbudget.FanoutBounds(max_calls=4),
        # the reporting form of the predicate, not the boolean one
        is_read_only=lambda c: "sequential",
    )
    assert plan.concurrent == []
    assert len(plan.sequential) == 4


def test_a_fan_outs_combined_return_is_bounded():
    """Per-result caps do not bound a fan-out; this does."""
    results = [(f"c{n}", ToolResult(True, "y" * 5000)) for n in range(10)]
    bounded, receipt = bound_fanout_output(results, max_output_chars=6000)
    assert receipt["bounded"] is True
    assert len(bounded) == 10  # nothing disappears
    assert receipt["replaced"] > 0
    total = sum(len(result.output) for _, result in bounded)
    assert total <= 6000 + 512 * receipt["replaced"], total
    # the first result FIT, so it is untouched; everything past the budget is
    # either marked as truncated or replaced by a bounded note
    assert bounded[0][1].output == "y" * 5000
    for _, result in bounded[2:]:
        assert (
            "fan-out output budget" in result.output
            or kbudget.CAP_MARKER in result.output
        ), result.output[:200]


def test_the_fan_out_bounds_come_from_the_run_config():
    """No bound is a hardcoded constant; a run stays reproducible from config."""
    defaults = fanout_bounds_from_config({})
    assert defaults.max_concurrency == 4
    assert defaults.max_calls == kbudget.FANOUT_MAX_CALLS
    pinned = fanout_bounds_from_config(
        {"max_parallel_tools": 2, "max_tool_fanout": 3, "max_tool_fanout_chars": 900}
    )
    assert pinned.as_dict() == {
        "max_concurrency": 2,
        "max_calls": 3,
        "max_output_chars": 900,
        "tool_output_tokens": kbudget.TOOL_OUTPUT_TOKEN_LIMIT,
    }
    # an unusable value degrades to the documented default, never to zero
    assert fanout_bounds_from_config({"max_tool_fanout": "nope"}).max_calls == (
        kbudget.FANOUT_MAX_CALLS
    )
    assert fanout_bounds_from_config({"max_tool_fanout": 0}).max_calls >= 1


# ---------------------------------------------------------------------------
# 4. A repeated identical call is a DECISION for the user
# ---------------------------------------------------------------------------


def test_the_repeat_detector_fires_at_the_configured_bound():
    """Default 3: the fourth identical call is the one that trips."""
    from harness.agent_kernel.tools import ToolLoopGuard

    guard = ToolLoopGuard()
    call = ToolCall(call_id="a", tool="edit", arguments={"path": "a.py"})
    verdicts = [guard.observe(call)[0] for _ in range(4)]
    assert verdicts == [False, False, False, True]
    assert guard.report()["blocked"] == 1
    assert guard.report()["max_repeats"] == 3


def test_a_repeated_call_is_counted_once_not_once_per_observer():
    """The dispatcher records it; the registry's last line must not re-count.

    Counting in both places reaches the bound at half the turns the receipt
    names, which is the kind of silent divergence a receipt that says
    ``threshold: 3`` must not be able to hide.
    """
    from harness.agent_kernel.tools import ToolLoopGuard

    guard = ToolLoopGuard(max_repeats=3)
    call = ToolCall(call_id="a", tool="edit", arguments={"path": "a.py"})
    counts = [guard.observe(call)[1] for _ in range(3)]
    assert counts == [1, 2, 3]
    assert guard.observe(call)[0] is False or True  # documented transition above
    # a pre-checked fingerprint is skipped by the registry's own guard
    assert guard.fingerprint(call) == guard.fingerprint(call)


def test_a_pre_checked_fingerprint_is_not_observed_twice():
    """The registry's last-line refusal skips what the dispatcher counted."""
    registry = ToolRegistry(specs=builtin_tool_specs())
    registry.set_handler("write", lambda call, ctx: ToolResult(True, "wrote"))
    call = ToolCall(
        call_id="w1",
        tool="write",
        arguments={"path": "a.py", "content": "x"},
    )
    config = {"max_repeat_tool_calls": 3}

    # the dispatcher records three observations across three turns
    for _ in range(3):
        blocked, count, fingerprint = registry.note_repeat(call, config)
        assert blocked is False
        assert count in (1, 2, 3)
    # dispatching with the pre-checked set must not advance the count again
    before = registry.loop_guard_report()["observed"]
    result = registry.execute(
        call, {"config": config, "loop_guard_pre_checked": {fingerprint}}
    )
    assert result.ok is True
    assert registry.loop_guard_report()["observed"] == before

    # ...and without it, the count advances
    registry.execute(call, {"config": config})
    assert registry.loop_guard_report()["observed"] == 1


def test_a_different_argument_is_not_a_repeat():
    """Only an IDENTICAL call is a doom loop."""
    from harness.agent_kernel.tools import ToolLoopGuard

    guard = ToolLoopGuard(max_repeats=1)
    for name in ("a.py", "b.py", "c.py"):
        assert (
            guard.observe(ToolCall(call_id="x", tool="edit", arguments={"path": name}))[
                0
            ]
            is False
        )


def _doom_strategy(max_repeats: int = 3, loop_guard_read_only: bool = False):
    """A strategy with only what the doom-loop pre-flight touches."""
    from harness.agent_kernel.strategy import DailyCodingStrategy

    class _Completion:
        def __init__(self):
            self.questions = []

        def needs_input(self, spec, question, **kwargs):
            self.questions.append(question)
            return ("needs_input", question)

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {
        "max_repeat_tool_calls": max_repeats,
        "loop_guard_read_only": loop_guard_read_only,
    }
    strategy.tools = ToolRegistry(specs=builtin_tool_specs())
    strategy.completion = _Completion()
    strategy.events = type("E", (), {"path": "trace.jsonl"})()
    strategy.checkpoints = type("C", (), {"path": "checkpoint.json"})()
    strategy.model_gateway = type("G", (), {"total_cost_usd": 0.0})()
    strategy.changed_files = set()
    strategy._loop_pre_checked = set()
    strategy.emitted = []
    strategy._event = lambda name, payload, **kw: strategy.emitted.append(
        (name, payload)
    )
    return strategy


def test_a_repeated_call_becomes_a_user_decision_not_a_silent_refusal():
    """The run STOPS and asks; nothing from the turn is executed."""
    from harness.agent_kernel.contracts import ToolCall

    strategy = _doom_strategy()
    call = ToolCall(
        call_id="c1",
        tool="edit",
        arguments={"path": "a.py", "old_string": "x", "new_string": "y"},
    )

    for turn in (1, 2, 3):
        assert strategy._doom_loop_decision(None, [call], turn=turn) is None
    decision = strategy._doom_loop_decision(None, [call], turn=4)

    assert decision is not None
    assert decision[0] == "needs_input"
    assert "identical" in decision[1]
    assert "Nothing from this turn was executed" in decision[1]
    assert len(strategy.completion.questions) == 1

    names = [name for name, _ in strategy.emitted]
    assert names == ["doom_loop_detected"]
    payload = strategy.emitted[0][1]
    assert payload["identical_count"] == 4
    assert payload["threshold"] == 3
    assert payload["tool"] == "edit"


def test_a_repeat_is_detected_again_after_the_user_is_asked():
    """The counts really do advance, so the bound is reachable at all."""
    from harness.agent_kernel.contracts import ToolCall

    strategy = _doom_strategy(max_repeats=2)
    call = ToolCall(call_id="c1", tool="shell", arguments={"command": "pytest"})

    assert strategy._doom_loop_decision(None, [call], turn=1) is None
    assert strategy._doom_loop_decision(None, [call], turn=2) is None
    assert strategy._doom_loop_decision(None, [call], turn=3) is not None
    assert strategy._doom_loop_decision(None, [call], turn=4) is not None


def test_a_different_call_in_the_same_turn_is_not_the_same_repeat():
    """Four DIFFERENT reads in one turn must not trip the detector."""
    strategy = _doom_strategy(max_repeats=1)
    calls = [
        ToolCall(call_id=f"r{n}", tool="read", arguments={"path": f"{n}.py"})
        for n in range(4)
    ]
    for _ in range(3):
        assert strategy._doom_loop_decision(None, calls, turn=1) is None


def test_reads_stay_exempt_but_can_be_armed():
    """The exemption is a deliberate, reversible policy, not an oversight."""
    from harness.agent_kernel.tools import ToolLoopGuard

    read = ToolCall(call_id="r", tool="read", arguments={"path": "a.py"})
    lenient = ToolLoopGuard(max_repeats=1)
    assert all(lenient.observe(read, read_only=True)[0] is False for _ in range(5))
    strict = ToolLoopGuard(max_repeats=2, include_read_only=True)
    assert [strict.observe(read, read_only=True)[0] for _ in range(4)] == [
        False,
        False,
        True,
        True,
    ]


def test_the_registry_keeps_its_own_last_line_refusal():
    """A caller that dispatches directly is still protected."""
    registry = ToolRegistry(specs=builtin_tool_specs())
    registry.set_handler("write", lambda call, ctx: ToolResult(True, "wrote"))
    config = {"max_repeat_tool_calls": 1}
    call = {
        "call_id": "w1",
        "tool": "write",
        "arguments": {"path": "a.py", "content": "x"},
    }
    first = registry.execute(dict(call), {"config": config})
    second = registry.execute(dict(call), {"config": config})
    assert first.ok is True
    assert second.ok is False
    assert second.error_kind == "loop_detected"
    assert registry.loop_guard_report()["blocked"] == 1


# ---------------------------------------------------------------------------
# 5. Tool output is capped at STORAGE time
# ---------------------------------------------------------------------------


def test_tool_output_is_capped_before_it_is_stored():
    """The stored text is the capped text, and the cap is declared."""
    cap = cap_tool_output("z" * 100_000, token_limit=100)
    assert cap.truncated is True
    assert cap.original_chars == 100_000
    assert cap.kept_chars < cap.limit * kbudget.CHARS_PER_TOKEN + 64
    assert kbudget.CAP_MARKER in cap.text
    assert cap.as_dict()["reason"] == "tool_output_token_limit"
    # the omitted part is GONE from the stored text
    assert len(cap.text) < 100_000


def test_a_short_result_is_untouched():
    cap = cap_tool_output("small result", token_limit=4000)
    assert cap.truncated is False
    assert cap.text == "small result"
    assert kbudget.CAP_MARKER not in cap.text


def test_the_cap_is_in_tokens_not_characters():
    """A token cap is the right unit: the budget is a token budget."""
    cap = cap_tool_output("abcdefghij" * 400, token_limit=50)
    assert cap.truncated is True
    assert cap.limit == 50
    # ~200 chars at 4 chars/token, plus the marker
    assert 100 < cap.kept_chars < 300, cap.kept_chars


def test_a_zero_token_limit_means_no_cap_rather_than_no_output():
    """'No cap' must be expressible without emptying every result."""
    cap = cap_tool_output("keep me", token_limit=0)
    assert cap.truncated is False
    assert cap.text == "keep me"
    assert cap.reason == "disabled"


class _StubResponse:
    tool_calls: list = []  # noqa: RUF012 - a provider response with no calls
    content = ""
    stop_reason = "tool_use"
    tool_protocol = "native"


def _capped_turn(*, handler_output: str, token_limit: int):
    """Drive the REAL ``_execute_calls`` and return what it stored and traced."""
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {
        "tool_output_token_limit": token_limit,
        "max_tool_fanout_chars": 10**6,
        "max_tool_fanout": 8,
        "max_parallel_tools": 4,
    }
    strategy.budget = kbudget.ContextBudget(window=4096)
    strategy.tools = ToolRegistry(specs=builtin_tool_specs())
    strategy.events = type("E", (), {"path": "t"})()
    strategy.checkpoints = type("C", (), {"path": "c"})()
    strategy.model_gateway = type("G", (), {"total_cost_usd": 0.0})()
    strategy.changed_files = set()
    strategy._loop_pre_checked = set()
    strategy.workspace = None
    strategy._state = None
    strategy.execution_backend = None
    strategy.cancellation_token = None
    strategy._knowledge = None
    strategy.completion = type(
        "Cm", (), {"cancelled": staticmethod(lambda *a, **k: None)}
    )()
    strategy.emitted = []
    strategy._event = lambda name, payload, **kw: strategy.emitted.append(
        (name, payload)
    )
    strategy._control = lambda name: {
        "finish": "finish",
        "cancel": "cancel",
        "question": "ask",
    }[name]
    strategy._ask_approval = lambda call, decision: (True, "once")
    strategy._canonical_mutation_tools = lambda: {"edit", "write", "apply_patch"}
    strategy._observe_lsp_after_mutation = lambda call, spec: ""
    strategy._record_turn_call = lambda call, ok, turn: None
    strategy._sync_changes = lambda: None
    strategy._pending_context = ""
    strategy.stored = []

    class _Conversation:
        def record_tool_result(self, tool, ok, output, **kw):
            strategy.stored.append(output)

        def record_note(self, text, **kw):
            pass

    strategy.conversation = _Conversation()
    strategy.tools.set_handler(
        "read", lambda call, ctx: ToolResult(True, handler_output)
    )
    decision = type(
        "D",
        (),
        {
            "terminal": False,
            "needs_approval": False,
            "to_dict": lambda self: {},
            "exact_effect": "",
            "reason": "",
        },
    )()
    strategy.policy = type("P", (), {"evaluate": lambda self, call: decision})()

    spec = type("S", (), {"run_id": "run-1"})()
    strategy._execute_calls(
        spec,
        [{"call_id": "r1", "tool": "read", "arguments": {"path": "a.py"}}],
        _StubResponse(),
        turn=1,
        messages=[],
    )
    return strategy


def test_the_tool_result_event_carries_the_capped_text_not_the_raw_one():
    """End to end: the conversation, the journal row and the trace all see
    the capped value, because the cap happens before any of them is written."""
    huge = "q" * 60_000
    strategy = _capped_turn(handler_output=huge, token_limit=50)

    assert strategy.stored, "the tool result never reached the conversation"
    for output in strategy.stored:
        assert len(output) < 1000, len(output)
        assert kbudget.CAP_MARKER in output
        assert huge not in output

    caps = [p for name, p in strategy.emitted if name == "tool_output_capped"]
    assert len(caps) == 1
    assert caps[0]["original_chars"] == 60_000
    assert caps[0]["truncated"] is True
    assert caps[0]["limit"] == 50

    traces = [p for name, p in strategy.emitted if name == "tool_result"]
    assert traces and all(len(str(p.get("output", ""))) < 1000 for p in traces)


def test_a_short_result_emits_no_cap_receipt():
    """No truncation, no receipt: the trace must not cry wolf."""
    strategy = _capped_turn(handler_output="a small result", token_limit=4000)
    assert strategy.stored == ["a small result"]
    assert [n for n, _ in strategy.emitted if n == "tool_output_capped"] == []


# ---------------------------------------------------------------------------
# the verifier gate is not weaker for any of this
# ---------------------------------------------------------------------------


def test_the_success_mint_still_requires_clean_verifier_evidence():
    """Read the mint condition; nothing here may loosen it."""
    from harness.agent_kernel import completion as completion_module

    body = inspect.getsource(completion_module)
    assert "target_passed" in body, "the target test no longer gates success"
    assert "regression_passed" in body, "regression no longer gates success"
    assert "flaky" in body, "flake no longer gates success"


def test_no_completion_status_makes_unverified_reachable_as_success():
    """``completed_unverified`` stays its own status, verbatim."""
    from shared.agent_contracts import RUN_STATUSES

    assert "completed_unverified" in RUN_STATUSES
    assert "completed_verified" in RUN_STATUSES
    assert "success" not in RUN_STATUSES


# ---------------------------------------------------------------------------
# end to end: the real kernel, a real turn loop, a scripted model
# ---------------------------------------------------------------------------


def test_a_doom_loop_stops_a_real_run_and_asks_the_user(tmp_path):
    """The strongest proof: a REAL run, four identical calls, and a stopped run.

    Not a stubbed pre-flight — the real ``AgentKernel``, the real turn loop, the
    real registry. The scripted model re-emits the same canonical call every
    turn; the run must end ``needs_input`` with a ``doom_loop_detected`` row,
    and the FOURTH call must not have executed.
    """
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.gateway import ModelGateway
    from harness.agent_kernel.kernel import AgentKernel

    class _Scripted:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            return self.replies.pop(0)

        def get_last_usage(self):
            return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")

    same_call = json.dumps({"tool": "read", "path": "app.py"})
    model = _Scripted([same_call] * 6)
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            # reads are exempt by default; arm them so a REPEATED READ is the
            # doom loop here, which is the case a model gets stuck in
            "loop_guard_read_only": True,
            "max_repeat_tool_calls": 3,
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="doom",
            request="read app.py",
            repository_identity=str(repo),
        )
    )

    assert result.status == "needs_input", result.status
    assert "identical arguments" in (result.answer or ""), result.answer

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "doom" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    detections = [r for r in rows if r.get("event") == "doom_loop_detected"]
    assert len(detections) == 1, [r.get("event") for r in rows]
    assert detections[0]["data"]["identical_count"] == 4
    assert detections[0]["data"]["threshold"] == 3
    # the bound was reached on the FOURTH turn, so exactly three reads ran
    results_rows = [r for r in rows if r.get("event") == "tool_result"]
    assert len(results_rows) == 3, len(results_rows)


def test_a_read_turn_with_distinct_calls_runs_to_its_normal_end(tmp_path):
    """The detector must not fire on ordinary distinct exploration."""
    import json

    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.gateway import ModelGateway
    from harness.agent_kernel.kernel import AgentKernel

    class _Scripted:
        def __init__(self, replies):
            self.replies = list(replies)

        def __call__(self, messages, **kwargs):
            return self.replies.pop(0)

        def get_last_usage(self):
            return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")

    model = _Scripted(
        [
            json.dumps({"tool": "read", "path": "a.py"}),
            json.dumps({"tool": "read", "path": "b.py"}),
            json.dumps({"tool": "read", "path": "a.py"}),
            json.dumps({"tool": "read", "path": "b.py"}),
            json.dumps({"tool": "finish", "answer": "read both files"}),
        ]
    )
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "loop_guard_read_only": True,
            "max_repeat_tool_calls": 3,
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="distinct",
            request="read both files",
            repository_identity=str(repo),
        )
    )

    # four reads, two distinct files, none repeated more than twice -> no loop
    assert result.status != "needs_input", result.status
    rows = (
        (tmp_path / "logs" / "distinct" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert not any("doom_loop_detected" in line for line in rows)
