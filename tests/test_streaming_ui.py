"""Regression tests for VEX-CEILING-10 parts 2-5.

* part 2 — render-loop safety (``cli.streamview``)
* part 3 — steering while a run is live (``cli.interactive.steer_live_run``)
* part 4 — background runs: detach / watch / attach (``cli.background``)
* part 5 — completion AND failure notifications (``cli.notify``)

The five scenarios the ceiling prompt names are pinned as their own tests
and marked with ``test_ceiling_scenario_*``:

1. a real stream produces increasing visible text
2. a slow endpoint shows a phase-specific state
3. 1 event/token and 1 event/100 tokens have equivalent frame cost
4. detach -> watch -> attach completes without gaps
5. a failed run notifies while JSON stdout stays machine-clean

Docker, live-provider, and real-PTY lanes are NOT exercised here. They are
reported as not selected in the handoff, never as passes.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cli.background as background
import cli.notify as notify
import cli.streamview as sv

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_journal(directory: Path, rows: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    trace = directory / "trace.jsonl"
    with trace.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return trace


def _running_rows(**overrides) -> list[dict]:
    base = [
        {"kind": "task_start", "data": {"mode": "fix"}, "ts": 1.0},
        {"kind": "model_request", "data": {"step": "step-1"}, "ts": 2.0},
        {"kind": "model_delta", "data": {"delta": "Reading the file"}, "ts": 2.4},
        {"kind": "model_delta", "data": {"delta": " then editing"}, "ts": 2.6},
        {"kind": "model_response", "data": {"step": "step-1"}, "ts": 2.8},
        {"kind": "tool_call", "data": {"command": "pytest -q"}, "ts": 3.0},
    ]
    base.extend(overrides.get("extra", []))
    return base


def _finished_rows(status: str = "success") -> list[dict]:
    return _running_rows(
        extra=[
            {"kind": "tool_result", "data": {"exit_code": 0}, "ts": 4.0},
            {"kind": "result", "data": {"status": status, "attempts": 1}, "ts": 5.0},
            {"kind": "task_end", "data": {"status": status}, "ts": 5.1},
        ]
    )


class _Clock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


# ---------------------------------------------------------------------------
# part 2 — render-loop safety
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_the_four_states_are_distinct(self):
        phases = {
            sv.RunPhase.AWAITING_FIRST_TOKEN,
            sv.RunPhase.THINKING,
            sv.RunPhase.STREAMING,
            sv.RunPhase.TOOL,
        }
        assert len(phases) == 4
        assert len({p.value for p in phases}) == 4

    def test_model_request_enters_waiting_for_first_token(self):
        clock = _Clock()
        projector = sv.PhaseProjector(clock=clock)
        projector.consume("task_start", {"mode": "fix"})
        state = projector.consume("model_request", {"step": "step-1"})
        assert state is not None
        assert state.phase is sv.RunPhase.AWAITING_FIRST_TOKEN
        assert "step-1" in state.detail

    def test_first_delta_moves_to_streaming(self):
        projector = sv.PhaseProjector(clock=_Clock())
        projector.consume("model_request", {})
        state = projector.consume("model_delta", {"delta": "hi"})
        assert state is not None
        assert state.phase is sv.RunPhase.STREAMING

    def test_model_response_moves_to_thinking(self):
        projector = sv.PhaseProjector(clock=_Clock())
        projector.consume("model_request", {})
        projector.consume("model_delta", {"delta": "x"})
        state = projector.consume("model_response", {})
        assert state is not None
        assert state.phase is sv.RunPhase.THINKING

    def test_tool_call_moves_to_tool_with_the_command(self):
        projector = sv.PhaseProjector(clock=_Clock())
        state = projector.consume("tool_call", {"command": "python -m pytest -q"})
        assert state is not None
        assert state.phase is sv.RunPhase.TOOL
        assert "pytest" in state.detail

    def test_terminal_failure_is_a_distinct_phase(self):
        projector = sv.PhaseProjector(clock=_Clock())
        state = projector.consume("task_end", {"status": "failed"})
        assert state is not None
        assert state.phase is sv.RunPhase.FAILED

    def test_completed_unverified_is_not_a_failure_phase(self):
        projector = sv.PhaseProjector(clock=_Clock())
        state = projector.consume("task_end", {"status": "completed_unverified"})
        assert state is not None
        assert state.phase is sv.RunPhase.DONE

    def test_unchanged_rows_return_none(self):
        projector = sv.PhaseProjector(clock=_Clock())
        projector.consume("task_start", {"mode": "fix"})
        assert projector.consume("model_delta", {"delta": "x"}) is not None
        assert projector.consume("model_delta", {"delta": "y"}) is None

    def test_cancel_overrides_a_later_event(self):
        projector = sv.PhaseProjector(clock=_Clock())
        projector.consume("model_request", {})
        assert projector.cancel().phase is sv.RunPhase.CANCELLING
        projector.consume("model_response", {})
        assert projector.state().phase is sv.RunPhase.CANCELLING

    def test_state_never_raises_on_a_hostile_row(self):
        projector = sv.PhaseProjector(clock=_Clock())
        projector.consume("model_delta", {"delta": object()})  # type: ignore[dict-item]
        assert projector.state().phase in tuple(sv.RunPhase)


class TestSlowEndpoint:
    def test_a_slow_endpoint_says_it_is_waiting(self):
        """Ceiling scenario 2: a slow endpoint shows a phase-SPECIFIC state."""
        clock = _Clock()
        projector = sv.PhaseProjector(slow_first_token_s=8.0, clock=clock)
        projector.consume("model_request", {"step": "planner"})
        fresh = projector.state()
        assert fresh.phase is sv.RunPhase.AWAITING_FIRST_TOKEN
        assert fresh.slow is False
        assert "no token" not in fresh.label()
        clock.advance(9.0)
        slow = projector.state()
        assert slow.phase is sv.RunPhase.AWAITING_FIRST_TOKEN
        assert slow.slow is True
        assert "no token after 9s" in slow.label()

    def test_a_fast_endpoint_never_reports_slow(self):
        clock = _Clock()
        projector = sv.PhaseProjector(slow_first_token_s=8.0, clock=clock)
        projector.consume("model_request", {})
        clock.advance(0.5)
        assert projector.state().slow is False

    def test_a_slow_tool_says_it_is_still_running(self):
        clock = _Clock()
        projector = sv.PhaseProjector(slow_tool_s=20.0, clock=clock)
        projector.consume("tool_call", {"command": "pytest -q"})
        clock.advance(25.0)
        state = projector.state()
        assert state.phase is sv.RunPhase.TOOL
        assert state.slow is True
        assert "25s" in state.label()

    def test_a_slow_endpoint_is_distinguishable_from_a_wedged_tool(self):
        clock = _Clock()
        waiting = sv.PhaseProjector(slow_first_token_s=8.0, clock=clock)
        waiting.consume("model_request", {})
        clock.advance(30.0)
        tool = sv.PhaseProjector(slow_tool_s=20.0, clock=clock)
        tool.consume("tool_call", {"command": "sleep 100"})
        clock.advance(30.0)
        assert waiting.state().phase is not tool.state().phase
        assert waiting.state().label() != tool.state().label()


class TestStreamCoalescer:
    def test_deltas_accumulate_into_growing_text(self):
        """Ceiling scenario 1: visible text INCREASES."""
        clock = _Clock()
        coalescer = sv.StreamCoalescer(clock=clock)
        seen: list[str] = []
        for word in ("one", "two", "three", "four"):
            coalescer.push_delta(word)
            clock.advance(0.2)
            payload = coalescer.poll(clock.now)
            if payload is not None:
                seen.append(payload)
        assert len(seen) >= 2, "text never became visible"
        # Each payload is a strict superset of the previous one.
        for earlier, later in itertools.pairwise(seen):
            assert later.startswith(earlier)
            assert len(later) > len(earlier)
        assert seen[-1] == "onetwothreefour"

    def test_control_bypasses_the_content_queue(self):
        """Ceiling prompt: control messages bypass content rendering queues."""
        clock = _Clock()
        coalescer = sv.StreamCoalescer(clock=clock)
        coalescer.push_delta("a" * 500)
        coalescer.push_control("cancel requested", "warn")
        # Even though no coalescing window has closed, the control lane wins.
        assert coalescer.poll(clock.now) == "cancel requested"
        assert coalescer.frame_cost_receipt()["control_pending"] == 0

    def test_a_control_message_beats_a_flood_of_text(self):
        clock = _Clock()
        coalescer = sv.StreamCoalescer(clock=clock)
        for _ in range(2000):
            coalescer.push_delta("x")
        coalescer.push_control("tool failed", "error")
        assert coalescer.poll(clock.now) == "tool failed"

    def test_the_first_delta_renders_promptly_then_the_window_gates(self):
        """The first token of a call must be visible immediately.

        A user waiting on a slow endpoint needs to see SOMETHING arrive.
        After that first frame the window gates, so a token storm costs
        one repaint per window rather than one per token. The payload is
        the CUMULATIVE live text (a growing body), not the window's slice.
        """
        clock = _Clock()
        coalescer = sv.StreamCoalescer(window_ms=100, clock=clock)
        coalescer.push_delta("a")
        assert coalescer.poll(clock.now) == "a"
        coalescer.push_delta("b")
        assert coalescer.poll(clock.now) is None
        coalescer.push_delta("c")
        assert coalescer.poll(clock.now) is None
        clock.advance(0.15)
        assert coalescer.poll(clock.now) == "abc"

    def test_a_burst_between_frames_is_delivered_as_one_payload(self):
        clock = _Clock()
        coalescer = sv.StreamCoalescer(window_ms=100, clock=clock)
        coalescer.push_delta("a")
        coalescer.poll(clock.now)
        clock.advance(0.15)
        for piece in "bcdefghij":
            coalescer.push_delta(piece)
        payload = coalescer.poll(clock.now)
        assert payload == "abcdefghij"
        assert coalescer.frame_cost_receipt()["content_frames"] == 2

    def test_an_unchanged_payload_is_not_re_rendered(self):
        clock = _Clock()
        coalescer = sv.StreamCoalescer(clock=clock)
        coalescer.push_delta("x")
        clock.advance(0.2)
        assert coalescer.poll(clock.now) == "x"
        assert coalescer.poll(clock.now) is None

    def test_live_text_is_bounded_with_an_explicit_marker(self):
        coalescer = sv.StreamCoalescer(max_live_chars=500, clock=_Clock())
        for _ in range(500):
            coalescer.push_delta("abcdefghij")
        text = coalescer.live_text()
        assert len(text) <= 500 + len(sv.TRIM_MARKER)
        assert sv.TRIM_MARKER in text

    def test_live_lines_are_bounded(self):
        coalescer = sv.StreamCoalescer(max_live_lines=4, clock=_Clock())
        for index in range(50):
            coalescer.push_delta(f"line {index}\n")
        assert len(coalescer.live_text().splitlines()) <= 4

    def test_window_is_clamped_to_the_documented_band(self):
        assert sv.StreamCoalescer(window_ms=1).window_ms == 40
        assert sv.StreamCoalescer(window_ms=10_000).window_ms == 500
        assert (sv.MIN_WINDOW_MS, sv.MAX_WINDOW_MS) == (40, 500)

    def test_reset_keeps_the_control_lane(self):
        coalescer = sv.StreamCoalescer(clock=_Clock())
        coalescer.push_delta("x")
        coalescer.push_control("cancel requested", "warn")
        coalescer.reset()
        assert coalescer.poll() == "cancel requested"

    def test_reset_all_clears_everything(self):
        coalescer = sv.StreamCoalescer(clock=_Clock())
        coalescer.push_delta("x")
        coalescer.push_control("c", "warn")
        coalescer.reset_all()
        assert coalescer.poll() is None
        assert coalescer.events == 0

    def test_receipt_is_json_safe(self):
        coalescer = sv.StreamCoalescer(clock=_Clock())
        coalescer.push_delta("x")
        receipt = coalescer.frame_cost_receipt()
        assert json.loads(json.dumps(receipt))["events"] == 1

    def test_poll_never_raises_on_a_hostile_delta(self):
        coalescer = sv.StreamCoalescer(clock=_Clock())
        coalescer.push_delta(object())  # type: ignore[arg-type]
        assert isinstance(coalescer.live_text(), str)


class TestFrameCostEquivalence:
    def test_one_event_per_token_and_one_per_hundred_cost_the_same(self):
        """Ceiling scenario 3: frame cost is independent of token rate.

        The claim is NOT that the two arms produce identical frame counts —
        a genuinely slower stream legitimately produces fewer frames, and
        that IS the coalescing working. The claim is that neither arm's
        frame count scales with its event count: both stay under the
        wall-clock bound.
        """
        dense = sv.simulate_stream(tokens=2000, span_s=1.0).frame_cost_receipt()
        sparse = sv.simulate_stream(tokens=20, span_s=1.0).frame_cost_receipt()
        verdict = sv.equivalent_frame_cost(dense, sparse, span_s=1.0)
        assert verdict["equal"] is True, verdict["reason"]
        assert dense["events"] == 2000 and sparse["events"] == 20
        assert dense["content_frames"] <= verdict["frames_allowed"]
        assert sparse["content_frames"] <= verdict["frames_allowed"]
        # The dense arm really did coalesce: ~100 events per rendered frame.
        assert verdict["frames_per_event_dense"] < 0.05
        assert verdict["coalescing_ratio"] < 0.1

    def test_the_bound_is_derived_from_wall_clock_only(self):
        assert sv.frames_allowed(1.0, 60) == 17
        assert sv.frames_allowed(2.0, 60) == 34
        assert sv.frames_allowed(0.0, 60) == 1

    def test_a_zero_event_arm_is_not_reported_as_equal(self):
        empty = {"events": 0, "content_frames": 0, "window_ms": 60}
        full = {"events": 100, "content_frames": 5, "window_ms": 60}
        verdict = sv.equivalent_frame_cost(full, empty, span_s=1.0)
        assert verdict["equal"] is False
        assert "exceeded the wall-clock bound" in verdict["reason"]

    def test_an_uncoalesced_stream_is_detected_as_a_failure(self):
        """A frame count that scales with events must FAIL the check."""
        unbounded = {
            "events": 2000,
            "content_frames": 2000,
            "window_ms": 60,
        }
        bounded = {"events": 20, "content_frames": 17, "window_ms": 60}
        verdict = sv.equivalent_frame_cost(unbounded, bounded, span_s=1.0)
        assert verdict["equal"] is False
        assert verdict["dense_within_bound"] is False

    def test_a_live_text_frame_costs_the_same_for_a_long_and_a_short_answer(self):
        short = sv.simulate_stream(tokens=20, span_s=1.0, max_live_chars=4000)
        long = sv.simulate_stream(tokens=20, span_s=1.0, max_live_chars=4000)
        for _ in range(200):
            long.push_delta("y" * 40)
        assert len(short.live_text()) <= 4000
        assert len(long.live_text()) <= 4000 + len(sv.TRIM_MARKER)


# ---------------------------------------------------------------------------
# part 3 — steering
# ---------------------------------------------------------------------------


class TestSteeringWhileRunning:
    def _live(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        task_id = "fix-steering"
        (tmp_path / task_id).mkdir(parents=True, exist_ok=True)
        (tmp_path / task_id / "trace.jsonl").write_text(
            json.dumps({"kind": "model_request", "data": {}}) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(iv, "_set_live_run", lambda tid, root: None, raising=False)
        return task_id

    def test_queued_steering_cannot_stop_a_run(self, tmp_path, monkeypatch):
        """A queued abort is downgraded: queueing must never cancel work."""
        from cli import interactive as iv

        task_id = self._live(tmp_path, monkeypatch)
        acks: list[str] = []
        result = iv.steer_live_run(
            "abort",
            task_id,
            tmp_path,
            source="tui",
            say=acks.append,
            queue_only=True,
        )
        assert result == "guide"
        assert any("not aborted" in line for line in acks)
        rows = [
            json.loads(line)
            for line in (tmp_path / task_id / "steering.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert rows, "nothing was journaled"
        assert all(row.get("intent") != "abort" for row in rows)

    def test_immediate_steering_still_can_abort(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        task_id = self._live(tmp_path, monkeypatch)
        result = iv.steer_live_run(
            "abort", task_id, tmp_path, source="tui", queue_only=False
        )
        assert result == "abort"

    def test_queued_steering_preserves_journal_order(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        task_id = self._live(tmp_path, monkeypatch)
        for text in ("first", "second", "third"):
            iv.steer_live_run(text, task_id, tmp_path, queue_only=True)
        rows = [
            json.loads(line)
            for line in (tmp_path / task_id / "steering.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert [row["text"] for row in rows] == ["first", "second", "third"]
        assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)

    def test_conversational_input_is_never_steered(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        task_id = self._live(tmp_path, monkeypatch)
        assert iv.steer_live_run("hi", task_id, tmp_path, queue_only=True) is None
        assert not (tmp_path / task_id / "steering.jsonl").exists()

    def test_a_refusal_is_honest(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        acks: list[str] = []
        result = iv.steer_live_run(
            "only touch auth.py",
            "task-that-never-started",
            tmp_path,
            say=acks.append,
            queue_only=True,
        )
        assert result == "starting"
        assert any("still starting" in line for line in acks)


class TestSteeringKeysRegistered:
    def test_the_two_steering_keys_are_bound(self):
        import cli.tui as tui

        keys = {
            binding.key for binding in tui.NeoApp.BINDINGS if hasattr(binding, "key")
        }
        assert "ctrl+g" in keys, "steer-at-next-safe-boundary is unbound"
        assert "ctrl+b" in keys, "queue-without-interrupting is unbound"
        assert "ctrl+x" in keys, "the width-independent cancel key is unbound"

    def test_the_steering_actions_exist(self):
        import cli.tui as tui

        for name in (
            "action_steer_boundary",
            "action_steer_queue",
            "action_detach",
            "action_attach",
        ):
            assert callable(getattr(tui.NeoApp, name, None)), name

    def test_the_app_tracks_detached_and_queued_state(self):
        import cli.tui as tui

        init_src = tui.NeoApp.__init__.__code__.co_names
        assert "_detached_task_id" in init_src
        assert "_queued_steering" in init_src


class TestCancelAffordance:
    @pytest.mark.parametrize("width", [62, 70, 80, 100, 120, 200])
    def test_cancel_is_visible_at_and_above_62_columns(self, width):
        from cli.tui_components import contextual_hints, resolve_shell_layout

        layout = resolve_shell_layout(width, 30)
        hints = contextual_hints(layout, active=True)
        assert "cancel" in hints.lower(), f"no cancel affordance at {width} columns"
        assert len(hints) <= width + 24, f"hint overflowed {width} columns"

    def test_cancel_is_visible_at_exactly_62_columns_idle(self):
        from cli.tui_components import contextual_hints, resolve_shell_layout

        layout = resolve_shell_layout(62, 30)
        assert "cancel" in contextual_hints(layout, active=False).lower()

    def test_a_modal_keeps_its_own_escape_hint(self):
        from cli.tui_components import contextual_hints, resolve_shell_layout

        layout = resolve_shell_layout(62, 30)
        assert "esc" in contextual_hints(layout, active=True, waiting=True).lower()


# ---------------------------------------------------------------------------
# part 4 — background runs
# ---------------------------------------------------------------------------


class TestBackgroundControlRecord:
    def test_detach_writes_a_control_record(self, tmp_path):
        path = background.detach(tmp_path, "fix-abc", mode="fix")
        assert path is not None and path.exists()
        record = background.read_control(tmp_path, "fix-abc")
        assert record is not None
        assert record.task_id == "fix-abc"
        assert record.mode == "fix"
        assert record.detached_at > 0
        assert record.pid == os.getpid()

    def test_the_control_record_carries_no_issue_text(self, tmp_path):
        background.detach(tmp_path, "fix-abc")
        raw = (tmp_path / "fix-abc" / background.CONTROL_FILE).read_text(
            encoding="utf-8"
        )
        assert "api_key" not in raw.lower()
        assert set(json.loads(raw)) == {
            "task_id",
            "mode",
            "detached_at",
            "pid",
            "version",
            "note",
        }

    def test_a_traversal_task_id_is_refused(self, tmp_path):
        assert background.detach(tmp_path, "../../etc") is None
        assert background.read_control(tmp_path, "../../etc") is None
        assert background.attach(tmp_path, "../../etc")["attached"] is False

    def test_a_corrupt_control_record_reads_as_absent(self, tmp_path):
        (tmp_path / "fix-x").mkdir(parents=True)
        (tmp_path / "fix-x" / background.CONTROL_FILE).write_text("{", encoding="utf-8")
        assert background.read_control(tmp_path, "fix-x") is None

    def test_a_future_version_is_refused(self, tmp_path):
        (tmp_path / "fix-x").mkdir(parents=True)
        (tmp_path / "fix-x" / background.CONTROL_FILE).write_text(
            json.dumps({"version": 999, "task_id": "fix-x"}), encoding="utf-8"
        )
        assert background.read_control(tmp_path, "fix-x") is None

    def test_list_detached_is_newest_first(self, tmp_path):
        background.detach(tmp_path, "fix-old", note="a")
        time.sleep(0.01)
        background.detach(tmp_path, "fix-new", note="b")
        listed = [record.task_id for record in background.list_detached(tmp_path)]
        assert listed == ["fix-new", "fix-old"]

    def test_clear_control_is_idempotent(self, tmp_path):
        background.detach(tmp_path, "fix-x")
        assert background.clear_control(tmp_path, "fix-x") is True
        assert background.clear_control(tmp_path, "fix-x") is False


class TestAttachReplay:
    def test_attach_replays_every_journal_row(self, tmp_path):
        rows = _running_rows()
        _write_journal(tmp_path / "fix-a", rows)
        receipt = background.attach(tmp_path, "fix-a")
        assert receipt["attached"] is True
        assert receipt["events"] == len(rows)
        assert receipt["gaps"] == 0
        assert "Reading the file" in receipt["live_text"]

    def test_attach_of_a_run_that_never_started_is_honest(self, tmp_path):
        (tmp_path / "fix-none").mkdir(parents=True)
        receipt = background.attach(tmp_path, "fix-none")
        assert receipt["attached"] is False
        assert "never started" in receipt["reason"]

    def test_attach_reports_a_failed_terminal_status(self, tmp_path):
        _write_journal(tmp_path / "fix-f", _finished_rows("failed"))
        receipt = background.attach(tmp_path, "fix-f")
        assert receipt["status"] == "failed"
        assert receipt["phase"] == "failed"

    def test_attach_never_reports_unverified_as_verified(self, tmp_path):
        _write_journal(tmp_path / "fix-u", _finished_rows("completed_unverified"))
        receipt = background.attach(tmp_path, "fix-u")
        assert receipt["status"] == "completed_unverified"
        assert "verified" not in str(receipt.get("status_label", ""))

    def test_a_malformed_line_is_counted_not_fatal(self, tmp_path):
        directory = tmp_path / "fix-m"
        directory.mkdir(parents=True)
        with (directory / "trace.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "task_start", "data": {}}) + "\n")
            handle.write("{not json\n")
            handle.write(
                json.dumps({"kind": "result", "data": {"status": "failed"}}) + "\n"
            )
        receipt = background.attach(tmp_path, "fix-m")
        assert receipt["attached"] is True
        assert receipt["gaps"] == 1
        assert receipt["status"] == "failed"

    def test_attach_notes_that_it_was_detached(self, tmp_path):
        _write_journal(tmp_path / "fix-d", _running_rows())
        background.detach(tmp_path, "fix-d")
        assert background.attach(tmp_path, "fix-d")["was_detached"] is True

    def test_the_frame_receipt_survives_a_replay(self, tmp_path):
        _write_journal(tmp_path / "fix-r", _running_rows())
        receipt = background.attach(tmp_path, "fix-r")
        assert json.loads(json.dumps(receipt["frame_receipt"]))["window_ms"] == 40


class TestWatch:
    def test_watch_follows_a_finished_run_immediately(self, tmp_path):
        _write_journal(tmp_path / "fix-w", _finished_rows("success"))
        receipt = background.watch(tmp_path, "fix-w", max_frames=5)
        assert receipt["watched"] is True
        assert receipt["terminal_seen"] is True
        assert receipt["status"] == "success"
        assert receipt["phase"] == "done"

    def test_watch_of_a_failed_run_reports_failure(self, tmp_path):
        _write_journal(tmp_path / "fix-f", _finished_rows("failed"))
        receipt = background.watch(tmp_path, "fix-f", max_frames=5)
        assert receipt["terminal_seen"] is True
        assert receipt["phase"] == "failed"

    def test_watch_emits_increasing_live_text(self, tmp_path):
        directory = tmp_path / "fix-l"
        directory.mkdir(parents=True)
        _write_journal(
            directory,
            [
                {"kind": "task_start", "data": {"mode": "fix"}},
                {"kind": "model_request", "data": {"step": "s1"}},
                {"kind": "model_delta", "data": {"delta": "alpha"}},
                {"kind": "model_delta", "data": {"delta": "beta"}},
                {"kind": "task_end", "data": {"status": "failed"}},
            ],
        )
        seen: list[str] = []
        background.watch(
            tmp_path,
            "fix-l",
            max_frames=5,
            on_frame=lambda frame: seen.append(str(frame.get("live_text") or "")),
        )
        assert any("alpha" in text for text in seen)
        assert seen[-1].endswith("alphabeta".replace(" ", ""))

    def test_watch_refuses_a_traversal_id(self, tmp_path):
        receipt = background.watch(tmp_path, "../escape")
        assert receipt["watched"] is False
        assert "invalid task id" in receipt["reason"]

    def test_watch_honors_its_frame_bound(self, tmp_path):
        _write_journal(tmp_path / "fix-r", _running_rows())
        receipt = background.watch(tmp_path, "fix-r", max_frames=3)
        assert receipt["watched"] is True
        assert receipt["terminal_seen"] is False
        assert "frame bound" in receipt["reason"]

    def test_watch_waits_for_a_journal_that_has_not_appeared(self, tmp_path):
        clock = _Clock()
        sleep_calls: list[float] = []

        def advancing_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        receipt = background.watch(
            tmp_path,
            "fix-late",
            sleep=advancing_sleep,
            clock=clock,
            timeout_s=5.0,
            max_frames=1,
        )
        assert receipt["watched"] is False
        assert "never started" in receipt["reason"]
        assert sleep_calls, "watch did not wait at all"
        assert receipt["waited_polls"] >= 1

    def test_watch_stops_when_the_clock_never_advances(self, tmp_path):
        """A frozen clock must produce an honest reason, not a hang."""
        clock = _Clock()
        _write_journal(tmp_path / "fix-open", _running_rows())
        receipt = background.watch(
            tmp_path,
            "fix-open",
            clock=clock,
            sleep=lambda _s: None,
            timeout_s=0.0,
            max_frames=0,
        )
        assert receipt["watched"] is True
        assert receipt["terminal_seen"] is False
        assert "did not advance" in receipt["reason"]

    def test_watch_of_a_rotated_journal_rereads_from_the_start(self, tmp_path):
        directory = tmp_path / "fix-rot"
        directory.mkdir(parents=True)
        trace = directory / "trace.jsonl"
        trace.write_text(
            json.dumps({"kind": "model_delta", "data": {"delta": "old"}}) + "\n",
            encoding="utf-8",
        )
        clock = _Clock()

        def advancing_sleep(seconds: float) -> None:
            clock.advance(seconds)

        background.watch(
            tmp_path,
            "fix-rot",
            max_frames=1,
            sleep=advancing_sleep,
            clock=clock,
        )
        trace.write_text(
            json.dumps({"kind": "task_end", "data": {"status": "failed"}}) + "\n",
            encoding="utf-8",
        )
        clock.advance(1.0)
        receipt = background.watch(
            tmp_path,
            "fix-rot",
            max_frames=3,
            sleep=advancing_sleep,
            clock=clock,
        )
        assert receipt["status"] == "failed"


class TestDetachWatchAttachChain:
    def test_detach_kill_watch_attach_completes_without_gaps(self, tmp_path):
        """Ceiling scenario 4: detach, kill the TUI, watch, attach — no gaps.

        "Killing the TUI" is modeled honestly: nothing in this test holds
        the projection. A second process's journal is the only thing that
        carries state across the boundary, which is exactly the property
        being claimed.
        """
        rows = _finished_rows("success")
        _write_journal(tmp_path / "fix-detach", rows)

        # 1. detach: the run is left alive and a control record exists.
        assert background.detach(tmp_path, "fix-detach", mode="fix") is not None

        # 2. "the TUI died": the only surviving state is the journal on
        #    disk plus the control record. Nothing in memory is consulted.
        before = background.attach(tmp_path, "fix-detach")
        assert before["events"] == len(rows)

        # 3. neo watch reconstructs the run from the journal alone.
        watched = background.watch(tmp_path, "fix-detach", max_frames=5)
        assert watched["terminal_seen"] is True

        # 4. attach rebinds with a full replay.
        after = background.attach(tmp_path, "fix-detach")
        assert after["events"] == before["events"] == len(rows)
        assert after["gaps"] == 0
        assert after["was_detached"] is True
        assert after["status"] == "success"

    def test_a_second_observer_sees_the_same_event_count(self, tmp_path):
        """Two independent followers must not disagree."""
        _write_journal(tmp_path / "fix-two", _finished_rows("success"))
        first = background.watch(tmp_path, "fix-two", max_frames=3)
        second = background.watch(tmp_path, "fix-two", max_frames=3)
        assert first["events"] == second["events"]
        assert first["status"] == second["status"]

    def test_describe_reports_liveness_honestly(self, tmp_path):
        _write_journal(tmp_path / "fix-live", _running_rows())
        described = background.describe(tmp_path, "fix-live")
        assert described["liveness"] in (
            "running",
            "unresponsive",
            "unknown",
            "finished",
        )
        assert described["task_id"] == "fix-live"

    def test_a_finished_run_is_reported_as_finished(self, tmp_path):
        _write_journal(tmp_path / "fix-done", _finished_rows("success"))
        assert background.describe(tmp_path, "fix-done")["liveness"] == "finished"

    def test_a_failed_run_is_finished_and_failed(self, tmp_path):
        _write_journal(tmp_path / "fix-bad", _finished_rows("failed"))
        described = background.describe(tmp_path, "fix-bad")
        assert described["liveness"] == "finished"
        assert described["status"] == "failed"


class TestWatchCliSurface:
    def test_the_subcommand_is_registered(self):
        import argparse

        import cli.main as main

        parser = _public_parser(main)
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        assert subparsers, "the CLI exposes no subcommands"
        assert "watch" in subparsers[0].choices
        watch = subparsers[0].choices["watch"]
        dests = {action.dest for action in watch._actions}
        assert {"task_id", "log_root", "json", "interval_s", "timeout_s"} <= dests

    def test_help_mentions_watch(self):
        text = _run_cli(["--help"])
        assert "watch" in text


def _public_parser(main_module):
    """The parser main() builds, whatever it is named in this tree."""
    for name in ("build_parser", "_build_parser", "create_parser", "_create_parser"):
        factory = getattr(main_module, name, None)
        if callable(factory):
            return factory()
    raise AssertionError("cli.main exposes no parser factory to test against")


def _run_cli(argv: list[str]) -> str:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "cli", *argv],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    return proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# part 5 — notifications
# ---------------------------------------------------------------------------


class TestNotifyMode:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("off", notify.NotifyMode.OFF),
            ("OFF", notify.NotifyMode.OFF),
            ("0", notify.NotifyMode.OFF),
            ("false", notify.NotifyMode.OFF),
            ("no", notify.NotifyMode.OFF),
            ("bell", notify.NotifyMode.BELL),
            ("", notify.NotifyMode.BELL),
            ("desktop", notify.NotifyMode.DESKTOP),
            ("both", notify.NotifyMode.BOTH),
            ("all", notify.NotifyMode.BOTH),
            # An unrecognized value must not silence a user who typo'd.
            ("loud", notify.NotifyMode.BELL),
        ],
    )
    def test_every_documented_spelling_resolves(self, value, expected):
        assert notify.resolve_mode(value) is expected

    def test_mode_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("NEO_NOTIFY", "off")
        assert notify.resolve_mode() is notify.NotifyMode.OFF
        monkeypatch.setenv("NEO_NOTIFY", "both")
        assert notify.resolve_mode() is notify.NotifyMode.BOTH

    def test_channel_predicates(self):
        assert notify.NotifyMode.BELL.bell_enabled is True
        assert notify.NotifyMode.BELL.desktop_enabled is False
        assert notify.NotifyMode.DESKTOP.bell_enabled is False
        assert notify.NotifyMode.DESKTOP.desktop_enabled is True
        assert notify.NotifyMode.BOTH.bell_enabled is True
        assert notify.NotifyMode.BOTH.desktop_enabled is True
        assert notify.NotifyMode.OFF.bell_enabled is False
        assert notify.NotifyMode.OFF.desktop_enabled is False


class TestOutcomeClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("completed_verified", notify.Outcome.VERIFIED),
            ("success", notify.Outcome.VERIFIED),
            ("completed", notify.Outcome.COMPLETED),
            ("completed_unverified", notify.Outcome.UNVERIFIED),
            ("failed", notify.Outcome.FAILED),
            ("error", notify.Outcome.FAILED),
            ("timeout", notify.Outcome.FAILED),
            ("cancelled", notify.Outcome.FAILED),
            ("", notify.Outcome.FAILED),
            ("something-new", notify.Outcome.FAILED),
        ],
    )
    def test_status_maps_to_an_outcome(self, status, expected):
        assert notify.classify(status) is expected

    def test_unverified_is_never_a_completion(self):
        assert notify.classify("completed_unverified").is_completion is False
        assert notify.classify("completed_unverified").is_failure is False

    def test_unknown_status_fails_closed(self):
        outcome = notify.classify("a-status-nobody-defined")
        assert outcome.is_failure is True
        assert outcome.is_completion is False


class TestNotifyRun:
    def test_a_failed_run_notifies_and_escalates(self, capsys, monkeypatch):
        """Ceiling scenario 5, part one: a FAILED run notifies."""
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        monkeypatch.setattr(notify, "send_desktop", lambda t, b, **k: (True, ""))
        receipt = notify.notify_run("failed", mode="both", label="fix run")
        assert receipt.outcome is notify.Outcome.FAILED
        assert receipt.bell_rings == notify.FAILURE_BELL_REPEATS
        assert receipt.bell_rings > 1, "failure did not escalate past a completion ring"
        assert receipt.desktop_attempted is True
        assert receipt.desktop_sent is True
        assert "failed" in receipt.title.lower()
        capsys.readouterr()

    def test_a_completed_run_rings_once(self, monkeypatch):
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        receipt = notify.notify_run("success", mode="bell")
        assert receipt.outcome is notify.Outcome.VERIFIED
        assert receipt.bell_rings == 1

    def test_failure_escalation_differs_from_completion(self, monkeypatch):
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        failed = notify.notify_run("failed", mode="bell")
        done = notify.notify_run("success", mode="bell")
        assert failed.bell_rings > done.bell_rings

    def test_unverified_is_worded_honestly(self, monkeypatch):
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        receipt = notify.notify_run("completed_unverified", mode="bell")
        assert receipt.outcome is notify.Outcome.UNVERIFIED
        assert "unverified" in receipt.title.lower()
        assert "verified success" not in receipt.body.lower()

    def test_mode_off_emits_nothing(self, monkeypatch):
        def explode(_rings):  # pragma: no cover - must not run
            raise AssertionError("a bell was written with NEO_NOTIFY=off")

        monkeypatch.setattr(notify, "_write_bell", explode)
        receipt = notify.notify_run("failed", mode="off")
        assert receipt.bell_rings == 0
        assert receipt.suppressed_by == "mode-off"
        assert receipt.desktop_attempted is False

    def test_a_non_tty_gets_no_bell_byte(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "stdout", io.StringIO())
        receipt = notify.notify_run("failed", mode="bell")
        assert receipt.bell_rings == 0
        assert receipt.suppressed_by == "not-a-tty"
        assert "\a" not in receipt.body

    def test_a_headless_surface_skips_desktop_but_keeps_the_mode(self, monkeypatch):
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        receipt = notify.notify_run("success", mode="both", desktop=False)
        assert receipt.mode is notify.NotifyMode.BOTH
        assert receipt.desktop_attempted is False
        assert receipt.suppressed_by == "headless-surface"
        assert receipt.bell_rings == 1

    def test_a_receipt_never_claims_a_toast_it_did_not_send(self, monkeypatch):
        monkeypatch.setattr(
            notify, "send_desktop", lambda t, b, **k: (False, "no display")
        )
        receipt = notify.notify_run("failed", mode="desktop")
        assert receipt.desktop_attempted is True
        assert receipt.desktop_sent is False
        assert receipt.desktop_error == "no display"
        assert receipt.suppressed_by.startswith("no-desktop")

    def test_a_desktop_failure_does_not_raise(self, monkeypatch):
        def boom(title, body, **kwargs):
            raise RuntimeError("no display server")

        monkeypatch.setattr(notify, "send_desktop", boom)
        receipt = notify.notify_run("success", mode="desktop")
        assert receipt.desktop_sent is False

    def test_the_receipt_is_json_safe(self, monkeypatch):
        monkeypatch.setattr(notify, "_write_bell", lambda rings: True)
        payload = notify.notify_run("failed", mode="both", detail="t-1").to_dict()
        assert json.loads(json.dumps(payload))["failure_escalation"] is True

    def test_the_detail_is_truncated(self):
        _, _, title, body = notify.build_notification(
            "failed", mode="bell", detail="x" * 5000
        )
        assert len(body) < 260
        assert title

    def test_a_secret_shaped_detail_is_redacted(self):
        _, _, _, body = notify.build_notification(
            "failed", mode="bell", detail="token sk-abcdefghijklmnopqrstuvwx"
        )
        assert "sk-abcdefghijklmnopqrstuvwx" not in body
        assert "[REDACTED_SECRET]" in body

    def test_redaction_fails_closed_when_no_redactor_resolves(self, monkeypatch):
        for module_name, _attribute in notify.REDACTORS:
            monkeypatch.setitem(sys.modules, module_name, None)
        assert notify._redact("secret-ish text") == (
            "(detail withheld: no redactor available)"
        )


class TestNotifyOnRealSurfaces:
    def test_the_repl_uses_the_shared_policy(self, monkeypatch):
        from cli import interactive as iv

        seen: list[dict] = []
        monkeypatch.setattr(
            notify,
            "notify_run",
            lambda status, **kw: seen.append({"status": status, **kw}),
        )
        monkeypatch.setattr(iv, "NOTIFY", True)
        monkeypatch.setattr(iv, "_ON_TASK_START", None, raising=False)
        iv.notify_done("failed", detail="t-9", label="fix")
        assert seen and seen[0]["status"] == "failed"
        assert seen[0]["detail"] == "t-9"

    def test_the_repl_defers_to_a_mounted_tui(self, monkeypatch):
        from cli import interactive as iv

        called: list[int] = []
        monkeypatch.setattr(notify, "notify_run", lambda *a, **k: called.append(1))
        monkeypatch.setattr(iv, "NOTIFY", True)
        monkeypatch.setattr(iv, "_ON_TASK_START", lambda *a, **k: None, raising=False)
        iv.notify_done("failed")
        assert called == [], "the REPL and the TUI both notified"

    def test_a_bell_write_failure_never_raises(self, monkeypatch):
        def boom(_rings):
            raise OSError("stdout closed")

        monkeypatch.setattr(notify, "_write_bell", boom)
        receipt = notify.notify_run("failed", mode="bell")
        assert receipt.bell_rings == 0


# ---------------------------------------------------------------------------
# the JSON/headless lane stays machine-clean
# ---------------------------------------------------------------------------


class TestJsonStdoutStaysClean:
    def test_a_failed_run_notifies_while_json_stdout_is_parseable(self, tmp_path):
        """Ceiling scenario 5, part two.

        The subprocess runs with `NEO_NOTIFY=both`, so if any channel
        wrote to stdout the JSON document below would not parse — and the
        file is asserted byte-clean of control characters too.
        """
        log_root = tmp_path / "logs"
        _write_journal(log_root / "fix-json", _finished_rows("failed"))
        env = dict(os.environ)
        env.update(
            {
                "NEO_NOTIFY": "both",
                "NEO_NOTIFY_FAILSAFE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli",
                "watch",
                "fix-json",
                "--log-root",
                str(log_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert proc.returncode == 1, (
            f"a failed run must exit 1; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        document = json.loads(proc.stdout)
        assert document["task_id"] == "fix-json"
        assert document["status"] == "failed"
        assert document["terminal_seen"] is True
        # Byte-clean: no bell, no escape, no OSC, and no stray C0 control
        # byte. Printable non-ASCII is fine (the phase label uses a middot
        # separator); what must never appear is anything a terminal would
        # interpret rather than print.
        assert "\a" not in proc.stdout
        assert "\x1b" not in proc.stdout
        assert "\x07" not in proc.stdout
        offenders = {
            char for char in proc.stdout if ord(char) < 0x20 and char not in "\t\r\n"
        }
        assert not offenders, f"control characters in JSON stdout: {offenders!r}"

    def test_the_json_lane_emits_progress_on_stderr_not_stdout(self, tmp_path):
        log_root = tmp_path / "logs"
        _write_journal(log_root / "fix-j", _finished_rows("success"))
        env = dict(os.environ)
        env.update({"NEO_NOTIFY": "off", "PYTHONIOENCODING": "utf-8"})
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli",
                "watch",
                "fix-j",
                "--log-root",
                str(log_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["status"] == "success"
        assert "\a" not in proc.stderr
