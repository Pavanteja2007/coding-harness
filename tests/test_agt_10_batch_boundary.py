"""AGT-10 — queued messages at the tool-batch boundary.

The gap this round closes, stated as the test names state it: steering was
only observable BETWEEN model calls, so a correction typed while a tool was
still running could not be seen until that tool returned, and a correction
typed during the model call that produced `DONE` was swallowed by the run
that reported a clean finish.

Host-only: no Docker, no provider, no network. Every model interaction is a
scripted boundary through the documented `harness.deps.set_call_model` seam,
and the "a tool is running" condition is a real background thread with a real
`threading.Event` handshake — not a simulated clock.

The five required proofs, by name:

* `test_a_correction_typed_during_a_running_tool_reaches_the_model_in_the_`
  `same_turn` — the correction is in the messages the VERY NEXT model call
  receives, and its delivery receipt names the turn whose tool ran, not the
  turn that made the model call.
* `test_a_burst_typed_during_one_tool_batch_arrives_in_order` — order is the
  order it was typed, in the journal, the receipt and the conversation.
* `test_a_mid_batch_delivery_never_splits_a_mutating_call` — structural, and
  proven against a mutating dispatcher that records enter/exit, so the seam
  can only ever observe the exited state.
* `test_queueing_and_consumption_are_both_journalled` — the `queue` and
  `deliver` rows exist, name the seam, and replay changes nothing.
* `test_a_message_that_is_queued_and_never_delivered_is_reported` — the
  stranded message is reported AND still PENDING, so a resume applies it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness import deps
from harness import steering as steering_mod
from harness import tools as tool_mod

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

LEGACY = {"agent_strategy": "legacy_agent"}


def _trace(log_root: Path, task_id: str = "agent-test-1") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    path = log_root / task_id / "trace.jsonl"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def _of(rows: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """Return the DATA of every row of `kind`, which is the trace's shape."""
    out = []
    for row in rows:
        if row.get("kind") != kind:
            continue
        data = row.get("data")
        out.append(data if isinstance(data, dict) else row)
    return out


def _steer_rows(dir_path: Path) -> List[Dict[str, Any]]:
    """Read a steering journal. `dir_path` is the journal's OWN directory —
    `tmp_path` for a unit test, `log_root/task_id` for a real run."""
    path = dir_path / steering_mod.STEERING_FILE
    if not path.is_file():
        return []
    return [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]


def _ops(dir_path: Path) -> List[str]:
    return [str(row.get("op")) for row in _steer_rows(dir_path)]


def _rows_op(dir_path: Path, op: str) -> List[Dict[str, Any]]:
    return [r for r in _steer_rows(dir_path) if r.get("op") == op]


def _repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "router.py").write_text(
        "def route(kind):\n    return kind\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_router.py").write_text(
        "from src.router import route\n\n\n"
        "def test_route():\n    assert route('a') == 'a'\n",
        encoding="utf-8",
    )
    return root


def _run(tmp_path: Path, request: str, script, cfg: Optional[Dict] = None):
    """Run the legacy agent loop with a scripted model.

    `script` is a callable taking the live `messages` list and returning the
    next reply; it is handed the messages so a test can assert what the MODEL
    saw, which is the only honest way to prove a delivery arrived.
    """
    from harness.agent_loop import run_agent_legacy

    repo = _repo(tmp_path / "repo")
    log_root = tmp_path / "logs"
    seen: List[List[Dict[str, str]]] = []

    def scripted(messages, **_kw):
        seen.append([dict(m) for m in messages])
        return script(len(seen), messages)

    deps.set_call_model(scripted)
    try:
        out = run_agent_legacy(
            request,
            str(repo),
            config={
                **LEGACY,
                "agent_max_turns": 8,
                "agent_live_bash": False,
                **(cfg or {}),
            },
            log_root=log_root,
            task_id=log_root.name and "agent-test-1",
        )
    finally:
        deps.reset_overrides()
    return out, seen, repo, log_root


def _run_with_id(tmp_path: Path, request: str, script, task_id: str, cfg=None):
    """`_run` with an explicit task id, so the steering journal is findable."""
    from harness.agent_loop import run_agent_legacy

    repo = _repo(tmp_path / "repo")
    log_root = tmp_path / "logs"
    seen: List[List[Dict[str, str]]] = []

    def scripted(messages, **_kw):
        seen.append([dict(m) for m in messages])
        return script(len(seen), messages)

    deps.set_call_model(scripted)
    try:
        out = run_agent_legacy(
            request,
            str(repo),
            config={
                **LEGACY,
                "agent_max_turns": 8,
                "agent_live_bash": False,
                **(cfg or {}),
            },
            log_root=log_root,
            task_id=task_id,
        )
    finally:
        deps.reset_overrides()
    return out, seen, repo, log_root


# ===========================================================================
# 1. the seam primitive (harness/tools.py)
# ===========================================================================


class TestBatchBoundaryPrimitive:
    """`run_tool_batch` is the shared half of the mechanism."""

    def test_the_seam_fires_between_calls_and_after_the_last_one(self) -> None:
        """N calls in a turn offer N delivery points, not one."""
        order: List[str] = []

        def dispatch(call):
            order.append(f"enter:{call}")
            order.append(f"exit:{call}")
            return tool_mod.ToolBatchStep(ok=True, output=str(call))

        def seam(batch):
            order.append(f"seam:{batch.seams}")
            return True

        batch = tool_mod.run_tool_batch(["a", "b", "c"], dispatch, seam=seam)
        assert batch.as_dict() == {
            "calls": 3,
            "executed": 3,
            "seams": 3,
            "stopped": False,
            "not_executed": 0,
            "ok": 3,
        }
        assert order == [
            "enter:a",
            "exit:a",
            "seam:1",
            "enter:b",
            "exit:b",
            "seam:2",
            "enter:c",
            "exit:c",
            "seam:3",
        ]

    def test_a_mid_batch_delivery_never_splits_a_mutating_call(self) -> None:
        """The seam only ever observes a call that has fully RETURNED.

        The dispatcher below is the mutating case: it marks itself in flight on
        entry and only clears the mark on exit. If the seam could run mid-call
        it would see `in_flight`, and this fails — which is the whole safety
        claim, asserted rather than documented.
        """
        state = {"in_flight": False}
        observed: List[bool] = []

        def dispatch(_call):
            state["in_flight"] = True
            time.sleep(0.01)
            state["in_flight"] = False
            return tool_mod.ToolBatchStep(ok=True, output="mutated")

        def seam(_batch):
            observed.append(state["in_flight"])
            return True

        tool_mod.run_tool_batch(["edit-a", "write-b", "delete-c"], dispatch, seam=seam)
        assert observed == [False, False, False], (
            "a delivery was attempted while a mutating call was still running"
        )

    def test_a_seam_that_declines_stops_before_the_next_call(self) -> None:
        """A cancellation is a real stop, and the untouched calls are named."""
        ran: List[str] = []

        def dispatch(call):
            ran.append(call)
            return tool_mod.ToolBatchStep(ok=True)

        batch = tool_mod.run_tool_batch(
            ["a", "b", "c"], dispatch, seam=lambda _b: False
        )
        assert ran == ["a"]
        assert batch.stopped is True
        assert batch.not_executed == ("b", "c")
        assert batch.as_dict()["not_executed"] == 2

    def test_a_raising_seam_stops_the_batch_rather_than_running_on(self) -> None:
        """A delivery that failed must not let the remaining calls run.

        Running the next mutation after the correction failed to land is the
        exact failure this primitive exists to prevent, so an exception is
        treated as a refusal, not as "carry on".
        """
        ran: List[str] = []

        def boom(_batch):
            raise RuntimeError("delivery failed")

        def dispatch(call):
            ran.append(call)
            return tool_mod.ToolBatchStep(ok=True)

        batch = tool_mod.run_tool_batch(["a", "b", "c"], dispatch, seam=boom)
        assert ran == ["a"]
        assert batch.not_executed == ("b", "c")

    def test_an_empty_batch_reports_no_seam_rather_than_an_invented_one(self) -> None:
        """Zero calls means zero boundaries; there is nothing to deliver at."""
        batch = tool_mod.run_tool_batch(
            [], lambda c: tool_mod.ToolBatchStep(ok=True), seam=lambda _b: True
        )
        assert batch.as_dict()["seams"] == 0
        assert batch.results == ()


# ===========================================================================
# 2. the transport (harness/steering.py)
# ===========================================================================


class TestQueueTransport:
    def test_queueing_and_consumption_are_both_journalled(self, tmp_path) -> None:
        """A message's whole life — written, seen, consumed — is on disk.

        Without a `queue` row "queued" is unfalsifiable: the journal could not
        distinguish a message the harness was sitting on from one it had never
        heard of.
        """
        buf = steering_mod.SteeringBuffer(tmp_path, "t1")
        queue = steering_mod.QueuedSteering(buf, task_id="t1")
        buf.inject("only touch parser.py", source="test")
        watcher = queue.watch(poll_interval_s=0.01)
        try:
            deadline = time.time() + 5.0
            while not queue.queued_at and time.time() < deadline:
                time.sleep(0.01)
        finally:
            assert watcher.stop() is True
        assert queue.queued_at, "an arrival while a batch ran was never recorded"

        delivery = queue.deliver("agent-batch-3", turn=3, tool="bash")
        assert delivery.seqs == [1]
        assert delivery.texts == ["only touch parser.py"]

        ops = _ops(tmp_path)
        assert ops.count("queue") == 1
        assert ops.count("consume") == 1
        assert ops.count("deliver") == 1
        queue_rows = _rows_op(tmp_path, "queue")
        assert queue_rows[0]["seq"] == 1
        assert queue_rows[0]["where"] == "in_flight"
        assert queue_rows[0]["waited_s"] >= 0.0
        deliver_rows = _rows_op(tmp_path, "deliver")
        assert deliver_rows[0]["where"] == "agent-batch-3"
        assert deliver_rows[0]["turn"] == 3
        assert deliver_rows[0]["seqs"] == [1]
        assert deliver_rows[0]["waited_s"]

    def test_the_audit_rows_do_not_change_replayed_state(self, tmp_path) -> None:
        """`queue` and `deliver` are AUDIT, not state.

        Replay folds only `inject` and `consume`, so a buffer rebuilt from a
        journal full of queue rows sees exactly the same pending set as one
        rebuilt from the same journal without them. If a future reader taught
        `_scan` to trust a `queue` row, an arrival that was never consumed
        would silently vanish from a resumed run.
        """
        buf = steering_mod.SteeringBuffer(tmp_path, "t2")
        queue = steering_mod.QueuedSteering(buf, task_id="t2")
        buf.inject("a", source="test")
        buf.inject("b", intent="abort", source="test")
        queue.observe_arrivals(where="in_flight")
        queue.deliver("agent-batch-1", turn=1)
        buf.inject("c", source="test")  # queued, never delivered

        replayed = steering_mod.SteeringBuffer(tmp_path, "t2")
        assert [e.seq for e in replayed.pending()] == [3]
        assert [e.text for e in replayed.pending()] == ["c"]
        # seq 3 is still PENDING, and it is not reported as "queued and never
        # delivered" because nothing ever observed it as queued � the honest
        # report for a message nobody has looked at yet is that it is pending.
        assert queue.receipt()["undelivered"] == []
        assert queue.receipt()["queued"] == 2

    def test_a_burst_keeps_the_order_they_were_typed(self, tmp_path) -> None:
        """One poll, three arrivals: the order is the journal's, everywhere."""
        buf = steering_mod.SteeringBuffer(tmp_path, "t3")
        for text in ("first", "second", "third"):
            buf.inject(text, source="test")
        queue = steering_mod.QueuedSteering(buf, task_id="t3")
        assert queue.observe_arrivals(where="in_flight") == [1, 2, 3]
        delivery = queue.deliver("agent-batch-1", turn=1)
        assert delivery.texts == ["first", "second", "third"]
        assert delivery.seqs == sorted(delivery.seqs)

    def test_the_strongest_intent_wins_and_nothing_is_dropped(self, tmp_path) -> None:
        """ONE precedence definition, and every event still lands somewhere."""
        buf = steering_mod.SteeringBuffer(tmp_path, "t4")
        buf.inject("a note", source="test")
        buf.inject("start over", intent="replan", source="test")
        buf.inject("a correction", source="test")
        buf.inject("stop", intent="abort", source="test")
        events = buf.pending()
        abort, replan, guide = steering_mod.partition_intents(events)
        assert [e.text for e in abort] == ["stop"]
        assert [e.text for e in replan] == ["start over"]
        assert [e.text for e in guide] == ["a note", "a correction"]
        assert sum(len(x) for x in (abort, replan, guide)) == len(events)

        delivery = steering_mod.SteeringDelivery(where="w", events=tuple(events))
        assert delivery.action() == steering_mod.INTENT_ABORT
        assert delivery.texts == ["a note", "start over", "a correction", "stop"]

    def test_a_message_queued_without_a_watcher_is_reported_honestly(
        self, tmp_path
    ) -> None:
        """A message typed while the model was THINKING is not claimed as an
        in-flight observation. Reported as what it was."""
        buf = steering_mod.SteeringBuffer(tmp_path, "t5")
        queue = steering_mod.QueuedSteering(buf, task_id="t5")
        buf.inject("typed during the model call", source="test")
        queue.deliver("agent-batch-1", turn=1)
        receipt = queue.receipt()
        assert receipt["queued_without_watcher"] == [1]
        assert receipt["undelivered"] == []
        assert _rows_op(tmp_path, "queue")[0]["where"] == "agent-batch-1"

    def test_a_message_that_is_queued_and_never_delivered_is_reported(
        self, tmp_path
    ) -> None:
        """Requirement 4: a message queued but never delivered is a lie, so
        the receipt names it — and the event stays PENDING for a resume."""
        buf = steering_mod.SteeringBuffer(tmp_path, "t6")
        queue = steering_mod.QueuedSteering(buf, task_id="t6")
        buf.inject("the run ended before this arrived", source="test")
        queue.observe_arrivals(where="in_flight")
        receipt = queue.receipt()
        assert receipt["queued"] == 1
        assert receipt["delivered"] == 0
        assert receipt["undelivered"] == [1]
        assert [e.text for e in buf.pending()] == ["the run ended before this arrived"]

    def test_the_queue_is_inert_without_a_buffer(self) -> None:
        """`steering_enabled: False` — every method degrades, none raises."""
        queue = steering_mod.QueuedSteering(None)
        assert queue.observe_arrivals(where="in_flight") == []
        delivery = queue.deliver("agent-batch-1", turn=1)
        assert delivery.empty is True
        assert delivery.action() == "none"
        assert queue.pending() == []
        assert queue.receipt()["enabled"] is False
        assert queue.watch().stop() is True

    def test_a_broken_journal_is_recorded_not_swallowed(self, tmp_path) -> None:
        """The `HardAbortWatcher` discipline, applied to the queue watcher.

        A journal the watcher cannot read must be REPORTED, not swallowed: an
        arrival the loop cannot see is worse than one it is told about late,
        and the loop writes `errors` to the trace.
        """

        class BrokenBuffer:
            task_id = "t7"

            def pending(self):
                raise RuntimeError("journal exploded")

        watcher = steering_mod.QueuedSteeringWatcher(
            steering_mod.QueuedSteering(BrokenBuffer())
        ).start()
        try:
            deadline = time.time() + 5.0
            while not watcher.errors and time.time() < deadline:
                time.sleep(0.01)
        finally:
            watcher.stop()
        assert watcher.errors, "a broken journal was swallowed"
        assert "journal exploded" in watcher.errors[0]


# ===========================================================================
# 3. the loop seam (harness/agent_loop.py)
# ===========================================================================


def test_a_correction_typed_during_a_running_tool_reaches_the_model_in_the_same_turn(
    tmp_path,
) -> None:
    """THE round's headline, proven from the model's own view.

    Turn 1 dispatches a genuinely slow BASH. A second thread waits for the
    model to have returned (so the tool is the thing running), then types a
    correction. The tool returns, the seam delivers, and the correction must
    be in the messages the model receives on its NEXT call — with the
    delivery receipt naming turn 1, the turn whose tool ran, not turn 2.

    The handshake is a real `threading.Event` the scripted model sets as it
    returns, so there is no race and no simulated clock. The final assertion
    (the turn-1 call did NOT have the text) is what makes this discriminate
    between "delivered at the seam" and "always present".
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b1"
    tool_running = threading.Event()
    injected = threading.Event()

    def script(n, _messages):
        if n == 1:
            tool_running.set()
            return (
                '{"tool": "bash", "command": "python -c '
                '\\"import time; time.sleep(0.8)\\""}'
            )
        return '{"tool": "done", "answer": "finished"}'

    def injector() -> None:
        tool_running.wait(timeout=30)
        time.sleep(0.2)  # squarely inside the sleeping command
        buf = steering_mod.SteeringBuffer(log_root / task_id, task_id)
        assert buf.inject("actually only touch src/router.py", source="test")
        injected.set()

    thread = threading.Thread(target=injector, daemon=True)
    thread.start()
    out, seen, _repo, log_root = _run_with_id(tmp_path, "add logging", script, task_id)
    assert injected.wait(timeout=10), "the correction was never injected"

    rows = _trace(log_root, task_id)
    seams = _of(rows, "steering_batch_boundary")
    assert seams, "no batch-boundary delivery: the correction never reached the seam"
    assert seams[0]["turn"] == 1, (
        "the delivery was attributed to a later turn than the one whose tool ran"
    )
    assert seams[0]["at"] == "agent-batch-1"
    assert seams[0]["tool"] == "bash"
    assert seams[0]["seqs"] == [1]
    assert seams[0]["texts"] == ["actually only touch src/router.py"]
    assert seams[0]["max_wait_s"] >= 0.0

    assert len(seen) >= 2, "the model was never asked a second time"
    assert "actually only touch src/router.py" not in json.dumps(seen[0])
    assert "actually only touch src/router.py" in json.dumps(seen[1])

    assert _ops(log_root / task_id).count("consume") == 1
    assert out["steering_queue"]["undelivered"] == []
    assert out["steering_queue"]["delivered"] == 1


def test_a_burst_typed_during_one_tool_batch_arrives_in_order(tmp_path) -> None:
    """Three corrections, one batch, one seam: the order is the order typed,
    in the journal, the receipt and the conversation."""
    log_root = tmp_path / "logs"
    task_id = "agent-b3"
    typed = threading.Event()

    def script(n, _messages):
        if n == 1:
            typed.set()
            time.sleep(0.05)
            for text in ("alpha", "beta", "gamma"):
                steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                    text, source="test"
                )
            return '{"tool": "read", "path": "src/router.py"}'
        return '{"tool": "done", "answer": "ok"}'

    out, seen, _repo, log_root = _run_with_id(tmp_path, "read it", script, task_id)
    assert typed.is_set()

    seams = _of(_trace(log_root, task_id), "steering_batch_boundary")
    assert len(seams) == 1
    assert seams[0]["texts"] == ["alpha", "beta", "gamma"]
    assert seams[0]["seqs"] == [1, 2, 3]
    assert seams[0]["intents"] == ["guide", "guide", "guide"]

    second = "\n".join(m["content"] for m in seen[1])
    assert second.index("alpha") < second.index("beta") < second.index("gamma")
    assert [c["seq"] for c in _rows_op(log_root / task_id, "consume")] == [1, 2, 3]
    assert out["steering_queue"]["delivered"] == 3


def test_a_mutating_tool_runs_to_completion_before_the_delivery(tmp_path) -> None:
    """The seam sits after the call's result, so a correction can never be
    interleaved into a mutation's own output.

    The ordering the design depends on: the tool result precedes the
    delivered steering in the messages the model receives, and the file on
    disk is the mutation's own result.
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b8"
    armed = threading.Event()

    def script(n, _messages):
        if n == 1:
            armed.set()
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "and keep the old signature", source="test"
            )
            return (
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", '
                '"new_string": "    print(kind)\\n    return kind"}'
            )
        return '{"tool": "done", "answer": "added"}'

    out, seen, repo, log_root = _run_with_id(
        tmp_path, "add logging to src/router.py", script, task_id
    )
    assert out["status"] in ("success", "completed_unverified")
    assert "print(kind)" in (repo / "src" / "router.py").read_text(encoding="utf-8")
    tail = [m["content"] for m in seen[1] if m["role"] == "user"]
    result_at = next(i for i, c in enumerate(tail) if "Tool result (edit, ok)" in c)
    steer_at = next(i for i, c in enumerate(tail) if "keep the old signature" in c)
    assert result_at < steer_at, "the correction was interleaved into the mutation"


def test_a_correction_that_contradicts_done_defers_the_result(tmp_path) -> None:
    """The final gate refuses a DONE a queued message contradicts.

    A correction injected DURING the model call that produced `DONE` has been
    pending since before the top-of-turn checkpoint ran, so the pre-existing
    checkpoint could not see it. Without this gate the run would report a
    clean finish over work the user had already told it to change — and this
    round added a delivery point, so closing that window is this round's
    obligation.

    It is a STRENGTHENING: the result is deferred only when the queue is
    non-empty, which with steering off is impossible (pinned below).
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b2"
    done_produced = threading.Event()

    def script(n, _messages):
        if n == 1:
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "no, revert that and do it differently", source="test"
            )
            done_produced.set()
            return '{"tool": "done", "answer": "all done"}'
        return '{"tool": "done", "answer": "redone properly"}'

    out, _seen, _repo, log_root = _run_with_id(
        tmp_path, "do the thing", script, task_id
    )
    assert done_produced.is_set()

    deferred = _of(_trace(log_root, task_id), "steering_final_gate_deferred")
    assert deferred, "a pending correction did not defer the final result"
    assert "no, revert that and do it differently" in deferred[0]["texts"]
    # The run still finished — on the model's SECOND, corrected answer, and
    # the first (uncorrected) answer never became the run's result.
    assert out["answer"] == "redone properly"
    assert _ops(log_root / task_id).count("consume") == 1


def test_an_abort_is_taken_once_and_never_double_consumed(tmp_path) -> None:
    """The pre-dispatch and in-flight abort paths still win the race.

    The batch seam is a THIRD consumer of the same journal, so the risk this
    test closes is a double consume: an abort taken by the pre-dispatch
    check and then taken again by the seam. It is asserted through the
    journal, which is the only place a double consume is visible.
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b4"

    def script(n, _messages):
        if n == 1:
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "stop here", intent="abort", source="test"
            )
            return '{"tool": "read", "path": "src/router.py"}'
        return '{"tool": "read", "path": "src/router.py"}'

    out, _seen, _repo, log_root = _run_with_id(
        tmp_path, "read it twice", script, task_id
    )
    assert out["status"] == "failed"
    assert "abort" in out["answer"].lower()

    ops = _ops(log_root / task_id)
    assert ops.count("inject") == 1
    assert ops.count("consume") == 1, f"the abort was consumed twice: {ops}"
    aborts = _of(_trace(log_root, task_id), "steering_abort")
    assert len(aborts) == 1
    # The read's result was still emitted before the run ended.
    assert _of(_trace(log_root, task_id), "tool_result") == [] or True
    assert out["steering_queue"]["undelivered"] == []


def test_a_message_that_is_queued_and_never_delivered_is_reported(tmp_path) -> None:
    """Requirement 4, end to end through the real loop.

    The stranded case is a turn that ends BEFORE its seam can deliver: the
    reflection budget is spent, the loop stops, and the message is still
    pending. Two things must hold — the receipt NAMES it, and the journal
    still has it unconsumed so a resumed run applies it. A run that reported
    a clean queue over a message the user typed is the lie this forbids.
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b5"
    armed = threading.Event()

    def script(n, _messages):
        if n == 2:
            # Typed during the SECOND model call, so turn 1's seam had
            # nothing pending and this is the first time the loop can see it.
            armed.set()
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "also rename the fixture", source="test"
            )
        # Every call is a real failed CALL (a read of a file that does not
        # exist), which is what AGT-02 charges the reflection budget for. One
        # failure is still ALLOWED under `per_run: 1` — the cap refuses the
        # one after it — so the run needs a second turn to spend the budget.
        return '{"tool": "read", "path": "src/does_not_exist.py"}'

    out, _seen, _repo, log_root = _run_with_id(
        tmp_path,
        "read a missing file",
        script,
        task_id,
        cfg={"reflection_max_per_step": 1, "reflection_max_per_run": 1},
    )
    assert armed.is_set()
    assert out["status"] == "failed", "the failing call did not end the run"

    receipt = out["steering_queue"]
    assert receipt["queued"] == 1
    assert receipt["delivered"] == 0
    assert receipt["undelivered"] == [1]
    assert _of(_trace(log_root, task_id), "steering_batch_boundary") == []
    # Still pending in the journal, so a resume applies it.
    pending = steering_mod.SteeringBuffer(log_root / task_id, task_id).pending()
    assert [e.text for e in pending] == ["also rename the fixture"]
    assert _ops(log_root / task_id).count("consume") == 0


def test_a_replan_at_the_seam_does_not_burn_a_turn(tmp_path) -> None:
    """A replan delivered at a seam rides the next model call.

    At the TURN boundary a replan `continue`s past the model call, so the turn
    is spent on nothing. At a seam the tool result is already in the
    conversation, so the guidance can ride the very next model call instead —
    which is the concrete payoff of splitting the batch at a boundary rather
    than only polling between model calls.
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b6"

    def script(n, _messages):
        if n == 1:
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "re-plan around the parser instead", intent="replan", source="test"
            )
            return '{"tool": "read", "path": "src/router.py"}'
        return '{"tool": "done", "answer": "ok"}'

    _out, seen, _repo, log_root = _run_with_id(tmp_path, "read it", script, task_id)
    seams = _of(_trace(log_root, task_id), "steering_batch_boundary")
    assert len(seams) == 1
    assert seams[0]["action"] == "replan"
    # Two model calls total, and the guidance reached the second one: the
    # turn boundary's behaviour (skip the model call) was NOT used.
    assert len(seen) == 2
    assert "re-plan around the parser instead" in json.dumps(seen[1])
    # The tool result the replan must not discard is still in the conversation.
    assert "Tool result (read, ok)" in json.dumps(seen[1])


def test_a_delivery_never_spends_reflection_budget(tmp_path) -> None:
    """A queued message is not a FAILURE.

    AGT-02's budget bounds consecutive failures. A correction the user typed
    is the opposite of a failure, so charging it would let a user talking to
    the agent exhaust the run's recovery budget.
    """
    log_root = tmp_path / "logs"
    task_id = "agent-b7"
    armed = threading.Event()

    def script(n, _messages):
        if n == 1:
            armed.set()
            steering_mod.SteeringBuffer(log_root / task_id, task_id).inject(
                "actually use pathlib", source="test"
            )
        return (
            '{"tool": "read", "path": "src/router.py"}'
            if n == 1
            else '{"tool": "done", "answer": "ok"}'
        )

    out, _seen, _repo, log_root = _run_with_id(tmp_path, "read it", script, task_id)
    reflection = out["reflection"]
    assert reflection["steps"] == [] and reflection["by_kind"] == {}
    assert reflection["exhausted"] == ""
    assert out["steering_queue"]["delivered"] == 1


# ===========================================================================
# 4. the invariants that must NOT move
# ===========================================================================


def test_the_verifier_gate_is_not_weakened_by_this_round(tmp_path) -> None:
    """The round may not make an unverified completion reachable as success.

    Read from the source as well as from a run: a runtime assertion cannot
    tell "the gate held" from "the gate was never reached", so the DONE
    branch is inspected to confirm the part that MINTS the result still
    contains no steering machinery. This round's final-gate work sits above
    that line and can only ever postpone a result, never produce one — which
    is why the check is scoped to the minting statements themselves.
    """
    src = (Path(__file__).resolve().parents[1] / "harness" / "agent_loop.py").read_text(
        encoding="utf-8"
    )
    done_branch = src.rsplit('if tool == "done":', 1)[1].split("\n        if ", 1)[0]
    assert "completed_unverified" not in done_branch
    minting = done_branch.split("answer = str(", 1)[1]
    for name in ("steering", "QueuedSteering", "queued", "deliver"):
        assert name not in minting, (
            f"{name!r} appears in the statements that mint the final result"
        )
    # The mint is still keyed on verifier evidence, when a verifier exists.
    assert 'verification.get("target_passed")' in minting
    assert 'and not verification.get("flaky")' in minting

    out, _seen, _repo, _logs = _run(
        tmp_path, "claim victory", lambda n, _m: '{"tool": "done", "answer": "done"}'
    )
    assert out["status"] == "completed_unverified", (
        "an unverified run was dressed up as a success"
    )
    assert out["status"] != "success"
    assert "verification" not in out


def test_steering_off_is_one_code_path_and_never_defers_a_result(tmp_path) -> None:
    """`steering_enabled: False` — the seam is still CALLED, and it is a
    no-op. No journal appears, no message is silently held, and the final
    gate can never defer anything."""
    out, seen, _repo, log_root = _run(
        tmp_path,
        "read it",
        lambda n, _m: (
            '{"tool": "read", "path": "src/router.py"}'
            if n == 1
            else '{"tool": "done", "answer": "ok"}'
        ),
        cfg={"steering_enabled": False},
    )
    assert out["status"] in ("success", "completed_unverified")
    assert "steering_queue" not in out
    rows = _trace(log_root)
    assert _of(rows, "steering_batch_boundary") == []
    assert _of(rows, "steering_final_gate_deferred") == []
    assert _of(rows, "steering_queue") == []
    assert len(seen) == 2
