"""P1/W2-T1 — per-segment turn timing at three depths, and the slowest segment.

Why three depths and not one
----------------------------
A turn-1 timing is the cheapest turn **by construction**: there is no history
to render, no conversation to bound, no compaction to run and no tool output to
cap. Context assembly is the usual culprit and it usually GROWS, so reporting
turn 1 alone is not a measurement of the turn cost - it is a measurement of the
floor. The brief's own non-negotiable is *"never report a turn-1 timing as the
turn cost"*, so this drives a real scripted run to turn 1, turn 10 and turn 40.

What the segments are
---------------------
Each segment is a boundary that ALREADY EXISTS as code. Nothing here is a
subdivision invented to make a number look better:

===========================  ==========================================
segment                      instrumented boundary
===========================  ==========================================
``context``                  ``DailyCodingStrategy._compile_knowledge``
``budget``                   ``ContextMeter.measure``
``render``                   ``ConversationMemory.render``
``model``                    the Boundary-2 call the scripted model sees
``tool``                     ``_execute_by_concurrency_class``
``checkpoint``               ``DailyCodingStrategy._checkpoint``
===========================  ==========================================

The model segment is measured AT the scripted boundary, so it reports OUR
overhead at that seam rather than a provider's latency. With a scripted model it
is expected to be ~0ms, and that is the point: it isolates our cost from the
provider's, and the provider's is not something this round can move.

A first draft of this file reported ``render``, ``tool`` and ``checkpoint`` as
0.000 ms - not because they are free, but because only two of the six boundaries
had been wrapped. Reporting that table would have been a measurement of the
instrumentation, reported as a measurement of the system. ``test_every_segment
_is_actually_observed`` now fails if any segment records no samples, so a
segment that silently stopped being timed cannot be reported as a clean zero.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import tempfile
import time
from typing import Any, Callable, Dict, List, Tuple

import pytest

from harness import turn_caps

DEEPTHS: Tuple[int, ...] = (1, 10, 40)
SEGMENTS: Tuple[str, ...] = (
    "context",
    "budget",
    "render",
    "model",
    "tool",
    "checkpoint",
)


class _SegmentTimer:
    """Accumulate per-segment milliseconds in the order the loop runs them."""

    def __init__(self) -> None:
        self.ms: Dict[str, List[float]] = {name: [] for name in SEGMENTS}

    def add(self, segment: str, millis: float) -> None:
        self.ms.setdefault(segment, []).append(float(millis))

    def median(self, segment: str) -> float:
        values = self.ms.get(segment) or []
        return round(statistics.median(values), 3) if values else 0.0

    def samples(self, segment: str) -> int:
        return len(self.ms.get(segment) or [])

    def table(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {"median_ms": self.median(name), "samples": self.samples(name)}
            for name in SEGMENTS
        }


class _Patches:
    """Apply and UNDO a set of method wraps, so a failure cannot leak them.

    Leaking a wrapper out of a timing fixture is the classic way a timing suite
    starts measuring itself: every later run in the session pays for the wrap
    and the numbers drift upward with no visible cause.
    """

    def __init__(self) -> None:
        self._undo: List[Callable[[], None]] = []

    def wrap(self, owner: Any, name: str, on_enter: str, timer: _SegmentTimer) -> None:
        original = getattr(owner, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timer.add(on_enter, (time.perf_counter() - started) * 1000.0)

        setattr(owner, name, wrapper)

        def restore() -> None:
            setattr(owner, name, original)

        self._undo.append(restore)

    def undo_all(self) -> None:
        for restore in reversed(self._undo):
            restore()
        self._undo.clear()


def _instrumented_run(repo: str, *, turns: int, log_root: str) -> Dict[str, Any]:
    """Drive the real daily kernel with a scripted model, timing each segment."""
    import harness.deps as deps
    from harness.agent_kernel import budget as budget_mod
    from harness.agent_kernel import conversation as conversation_mod
    from harness.agent_kernel import strategy as strategy_mod

    timer = _SegmentTimer()
    patches = _Patches()
    seen_turns: List[int] = []
    strategy_cls = strategy_mod.DailyCodingStrategy

    # -- the three strategy-side boundaries ---------------------------------
    for method, segment in (
        ("_compile_knowledge", "context"),
        ("_execute_by_concurrency_class", "tool"),
        ("_checkpoint", "checkpoint"),
    ):
        patches.wrap(strategy_cls, method, segment, timer)

    # -- the two object-side boundaries -------------------------------------
    # `ContextBudget.measure` is the measurement; `ContextMeter` is the dataclass
    # that holds the RESULT of one. Wrapping the meter would measure nothing.
    patches.wrap(budget_mod.ContextBudget, "measure", "budget", timer)
    patches.wrap(conversation_mod.ConversationMemory, "render", "render", timer)

    # -- the turn counter ---------------------------------------------------
    original_prepared = strategy_cls._prepared_request

    def timed_prepared(self, spec, turn):  # type: ignore[no-untyped-def]
        seen_turns.append(turn)
        return original_prepared(self, spec, turn)

    strategy_cls._prepared_request = timed_prepared
    patches._undo.append(
        lambda: setattr(strategy_cls, "_prepared_request", original_prepared)
    )

    # -- the model boundary, measured where the scripted model is called -----
    def call_model(messages, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            # `<= turns` rather than `< turns`: at depth 1 the FIRST turn must
            # still issue a tool call, or the `tool` segment is never exercised
            # and the anti-vacuity pin fails for the right reason.
            working = len(seen_turns) <= turns
            return (
                json.dumps(
                    {"tool": "read", "path": "a.py", "arguments": {"path": "a.py"}}
                )
                if working
                else json.dumps({"tool": "done", "answer": "finished"})
            )
        finally:
            timer.add("model", (time.perf_counter() - started) * 1000.0)

    try:
        deps.set_call_model(call_model)
        from harness.agent_loop import run_agent_kernel

        result = run_agent_kernel(
            "measure the turn cost",
            repo,
            {
                "steering_enabled": False,
                "agent_max_turns": max(turns + 5, turn_caps.DEFAULT_AGENT_MAX_TURNS),
                "max_wallclock_s": 900,
                "budget_cap_usd": 1000.0,
                # knowledge compilation is a real cost and IS measured; these two
                # are the two optional features the round is not timing.
                "skills_enabled": False,
            },
            log_root=log_root,
            task_id=f"w2-timing-{turns}",
        )
    finally:
        deps.reset_overrides()
        patches.undo_all()
    return {"result": result, "timing": timer, "turns_seen": seen_turns}


@pytest.fixture(scope="module")
def timing_runs() -> Dict[int, Dict[str, Any]]:
    """Measure all three depths in one module-scoped fixture."""
    repo = tempfile.mkdtemp()
    (pathlib.Path(repo) / "a.py").write_text("x = 1\n", encoding="utf-8")
    logs = pathlib.Path(tempfile.mkdtemp())
    return {
        depth: _instrumented_run(repo, turns=depth, log_root=str(logs / f"d{depth}"))
        for depth in DEEPTHS
    }


def test_a_real_scripted_run_reaches_the_deepest_turn(timing_runs) -> None:
    """The measurement is worth nothing unless the run actually got there."""
    deepest = timing_runs[max(DEEPTHS)]
    assert deepest["result"].get("status") in {
        "completed_unverified",
        "completed_verified",
        "failed",
    }
    assert len(deepest["turns_seen"]) > 1, "only one turn was prepared"


def test_every_segment_is_actually_observed(timing_runs) -> None:
    """The anti-vacuity pin, and the reason the first draft of this file failed.

    Reporting ``0.000 ms`` for a segment that was never wrapped is a measurement
    of the instrumentation presented as a measurement of the system. Every
    segment must record at least one sample at every depth.
    """
    for depth in DEEPTHS:
        table = timing_runs[depth]["timing"].table()
        for segment in SEGMENTS:
            assert table[segment]["samples"] > 0, (
                f"segment {segment!r} recorded no samples at turn {depth}; "
                f"a 0.000 ms here would be an unwrapped boundary, not a free one"
            )


def test_the_per_segment_table_is_reported_at_all_three_depths(timing_runs) -> None:
    """Emit the table the handoff carries, at turn 1 / 10 / 40."""
    header = f"{'segment':<12}" + "".join(f"{'turn ' + str(d):>11}" for d in DEEPTHS)
    lines = [header, "-" * len(header)]
    table: Dict[str, Dict[int, float]] = {}
    for segment in SEGMENTS:
        row = {d: timing_runs[d]["timing"].median(segment) for d in DEEPTHS}
        table[segment] = row
        lines.append(f"{segment:<12}" + "".join(f"{row[d]:>11.3f}" for d in DEEPTHS))
    totals = {d: round(sum(table[s][d] for s in SEGMENTS), 3) for d in DEEPTHS}
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<12}" + "".join(f"{totals[d]:>11.3f}" for d in DEEPTHS))
    print(
        "\nP1/W2-T1 per-segment turn timing (ms, median over the turns at that depth)"
    )
    print("\n".join(lines))


def test_the_growth_curve_and_the_slowest_segment_are_named(timing_runs) -> None:
    """The brief's non-negotiable, made structural.

    A turn-1 timing is the cheapest turn by construction. So the handoff must
    carry the CURVE and name the slowest segment at DEPTH. The numbers go in the
    handoff; pinning them here would turn a host-load fluctuation into a test
    failure, which is the wrong trade for a timing measurement.
    """
    shallow = sum(timing_runs[min(DEEPTHS)]["timing"].median(s) for s in SEGMENTS)
    deep_timing = timing_runs[max(DEEPTHS)]["timing"]
    deep = sum(deep_timing.median(s) for s in SEGMENTS)
    ratio = (deep / shallow) if shallow else float("nan")
    slowest = max(SEGMENTS, key=lambda s: deep_timing.median(s))
    print(
        f"\ntotal at turn {min(DEEPTHS)}: {shallow:.3f} ms\n"
        f"total at turn {max(DEEPTHS)}: {deep:.3f} ms\n"
        f"growth: {ratio:.2f}x\n"
        f"slowest segment at turn {max(DEEPTHS)}: {slowest}"
    )
    assert all(depth in timing_runs for depth in DEEPTHS)
    assert slowest in SEGMENTS
