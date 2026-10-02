"""Unit tests for cli/runview.py — the live todo / status / card layer.

Pure mapping tests (textual-free, fast): they pin the round's contract
that the todo checklist, machine state, and completion-card numbers are
all derived from the run's OWN records (trace.jsonl events, state.json,
transitions.jsonl) — never a parallel tracking system — and that every
public function degrades honestly on malformed input instead of raising.

The TUI's rendering of the same data is pinned in tests/test_cli_tui.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import runview


def _ev(kind: str, data: dict) -> dict:
    return {"ts": 1.0, "kind": kind, "data": data}


PLAN_2 = [
    {"id": 1, "description": "inspect the divisor", "checkpoint": "understand it"},
    {
        "id": 2,
        "description": "fix the divisor to len(values)",
        "checkpoint": "suite passes",
    },
]


# ---------------------------------------------------------------------------
# TodoModel — the live checklist (Task A)
# ---------------------------------------------------------------------------


class TestTodoModel:
    def test_plan_creates_pending_steps(self):
        m = runview.TodoModel()
        assert m.consume(_ev("plan", {"plan": PLAN_2}))
        assert [s.description for s in m.steps] == [
            "inspect the divisor",
            "fix the divisor to len(values)",
        ]
        assert all(s.state == runview.PENDING for s in m.steps)

    def test_step_end_checks_off_the_step(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        assert m.consume(_ev("step_end", {"attempt": 1, "step_id": 1, "ok": True}))
        assert m.steps[0].state == runview.DONE
        assert m.steps[1].state == runview.PENDING
        done, total = m.progress()
        assert (done, total) == (1, 2)

    def test_step_end_failed_marks_red(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("step_end", {"attempt": 1, "step_id": 1, "ok": False}))
        assert m.steps[0].state == runview.FAILED

    def test_active_step_from_model_request(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        assert m.consume(_ev("model_request", {"step": "step-2"}))
        assert m.active_id == 2
        assert m.state_of(m.steps[1]) == runview.ACTIVE
        assert m.state_of(m.steps[0]) == runview.PENDING
        # non-step model calls (planner, critique) clear the marker
        assert m.consume(_ev("model_request", {"step": "plan"}))
        assert m.active_id is None

    def test_step_end_clears_active_marker(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("model_request", {"step": "step-1"}))
        m.consume(_ev("step_end", {"step_id": 1, "ok": True}))
        assert m.active_id is None
        assert m.steps[0].state == runview.DONE

    def test_attempt_retry_unchecks_then_replay_rechecks(self):
        """The harness resets completed steps on attempt 2 (work rolled
        back); the trace replays survivors via step_end/skipped events."""
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("step_end", {"attempt": 1, "step_id": 1, "ok": True}))
        assert m.consume(_ev("attempt_start", {"attempt": 2}))
        assert all(s.state == runview.PENDING for s in m.steps)
        m.consume(_ev("step_end", {"attempt": 2, "step_id": 1, "ok": True}))
        m.consume(_ev("step_end", {"attempt": 2, "step_id": 2, "ok": True}))
        done, total = m.progress()
        assert (done, total) == (2, 2)

    def test_attempt_resume_does_not_reset(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("step_end", {"step_id": 1, "ok": True}))
        # the FIRST iteration of a resumed run continues the attempt:
        # attempt number stays 1, nothing unchecks
        assert not m.consume(_ev("attempt_start", {"attempt": 1}))
        assert m.steps[0].state == runview.DONE

    def test_step_skipped_resume_checks_off(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        assert m.consume(
            _ev("step_skipped_resume", {"attempt": 1, "step": "1. inspect the divisor"})
        )
        assert m.steps[0].state == runview.SKIPPED
        done, _ = m.progress()
        assert done == 1

    def test_replan_keeps_completed_checkmarks(self):
        """A steering re-plan re-emits `plan`; steps whose description
        already completed keep their checkmark (the re-plan builds on
        work/, it never undoes it)."""
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("step_end", {"step_id": 1, "ok": True}))
        new_plan = [
            {"id": 1, "description": "inspect the divisor", "checkpoint": ""},
            {
                "id": 2,
                "description": "fix the divisor to len(values)",
                "checkpoint": "",
            },
            {"id": 3, "description": "add a guard for empty input", "checkpoint": ""},
        ]
        assert m.consume(_ev("plan", {"plan": new_plan}))
        assert m.steps[0].state == runview.DONE
        assert m.steps[1].state == runview.PENDING
        assert m.steps[2].state == runview.PENDING

    def test_terminal_events_clear_active(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        m.consume(_ev("model_request", {"step": "step-1"}))
        assert m.consume(_ev("task_end", {"status": "success"}))
        assert m.active_id is None

    def test_malformed_events_never_raise(self):
        m = runview.TodoModel()
        for bad in [
            {},
            {"kind": "plan"},
            {"kind": "plan", "data": None},
            {"kind": "plan", "data": {"plan": "not-a-list"}},
            {"kind": "plan", "data": {"plan": [{"id": "x", "description": 5}]}},
            {"kind": "step_end", "data": {"step_id": "NaN"}},
            {"kind": "step_skipped_resume", "data": {"step": ""}},
            {"kind": "attempt_start", "data": {"attempt": []}},
            {"kind": "model_request", "data": {"step": 17}},
            {"kind": "model_request", "data": {"step": "step-x"}},
            {"kind": "bogus_kind", "data": {}},
        ]:
            assert m.consume(bad) in (True, False)

    def test_unknown_step_id_is_ignored(self):
        m = runview.TodoModel()
        m.consume(_ev("plan", {"plan": PLAN_2}))
        assert not m.consume(_ev("step_end", {"step_id": 99, "ok": True}))


# ---------------------------------------------------------------------------
# read_machine_state — transitions.jsonl (Task B)
# ---------------------------------------------------------------------------


class TestReadMachineState:
    def test_real_trail_shape(self, tmp_path: Path):
        d = tmp_path / "t"
        d.mkdir()
        (d / "transitions.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {"from_state": None, "to_state": "planning", "valid": True}
                    ),
                    json.dumps(
                        {"from_state": "planning", "to_state": "testing", "valid": True}
                    ),
                    json.dumps(
                        {"from_state": "testing", "to_state": "editing", "valid": True}
                    ),
                ]
            ),
            encoding="utf-8",
        )
        assert runview.read_machine_state(d) == "editing"

    def test_steering_records_do_not_wipe_the_phase(self, tmp_path: Path):
        """Steering appends {event:...} records WITHOUT to_state; the
        last record carrying one wins (current_phase's blind spot)."""
        d = tmp_path / "t"
        d.mkdir()
        (d / "transitions.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {"from_state": None, "to_state": "editing", "valid": True}
                    ),
                    json.dumps(
                        {
                            "event": "steering",
                            "phase": "editing",
                            "valid": True,
                            "detail": {},
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        assert runview.read_machine_state(d) == "editing"

    def test_invalid_records_are_skipped(self, tmp_path: Path):
        d = tmp_path / "t"
        d.mkdir()
        (d / "transitions.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {"from_state": None, "to_state": "planning", "valid": True}
                    ),
                    json.dumps(
                        {
                            "from_state": "planning",
                            "to_state": "bogus-edge",
                            "valid": False,
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        assert runview.read_machine_state(d) == "planning"

    def test_missing_file_is_none(self, tmp_path: Path):
        assert runview.read_machine_state(tmp_path) is None

    def test_empty_and_garbage_are_none(self, tmp_path: Path):
        d = tmp_path / "t"
        d.mkdir()
        (d / "transitions.jsonl").write_text("\nnot json\n\n", encoding="utf-8")
        assert runview.read_machine_state(d) is None


# ---------------------------------------------------------------------------
# read_run_facts + card_lines — the completion card (Task C)
# ---------------------------------------------------------------------------


def _write_run(dir_path: Path, events: list, state: dict | None = None) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    with (dir_path / "trace.jsonl").open("w", encoding="utf-8") as fh:
        for kind, data, ts in events:
            fh.write(json.dumps({"ts": ts, "kind": kind, "data": data}) + "\n")
    if state is not None:
        (dir_path / "state.json").write_text(json.dumps(state), encoding="utf-8")


class TestReadRunFacts:
    def test_full_success_run(self, tmp_path: Path):
        d = tmp_path / "logs" / "fix-01"
        _write_run(
            d,
            [
                (
                    "task_start",
                    {"issue_text": "mean() is wrong; make it the mean"},
                    100.0,
                ),
                ("model_response", {"step": "plan", "usage": {"cost": 0.001}}, 101.0),
                ("model_response", {"step": "step-1", "usage": {"cost": 0.002}}, 102.0),
                (
                    "final_verify",
                    {
                        "target_passed": True,
                        "regression_passed": True,
                        "flaky": False,
                        "raw": "$ pytest\nexit=0\n6 passed in 0.14s",
                    },
                    103.0,
                ),
                (
                    "git_output",
                    {"branch": "harness/fix-mean", "commit_sha": "a745eb27"},
                    104.0,
                ),
                (
                    "result",
                    {"status": "success", "attempts": 1, "cost_usd": 0.009313},
                    105.0,
                ),
                ("task_end", {"status": "success", "attempt": 1}, 105.5),
            ],
            state={"files_touched": ["numlib/mathutil.py"]},
        )
        f = runview.read_run_facts(d)
        assert f["status"] == "success"
        assert f["attempts"] == 1
        assert f["model_calls"] == 2
        assert f["cost_usd"] == pytest.approx(0.009313)
        assert f["elapsed_s"] == pytest.approx(5.5, abs=0.1)
        assert f["target_passed"] is True and f["regression_passed"] is True
        assert f["flaky"] is False
        assert f["verify_summary"] == "6 passed in 0.14s"
        assert f["files"] == ["numlib/mathutil.py"]
        assert f["branch"] == "harness/fix-mean"
        assert f["commit_sha"] == "a745eb27"
        assert f["issue"] == "mean() is wrong; make it the mean"

    def test_failed_run_without_result_event(self, tmp_path: Path):
        """A crashed task (planner failure) has task_end but no result."""
        d = tmp_path / "logs" / "fix-02"
        _write_run(
            d,
            [
                ("task_start", {"issue_text": "x"}, 100.0),
                ("model_response", {"step": "plan", "usage": {"cost": 0.0004}}, 101.0),
                (
                    "task_end",
                    {"status": "error", "reason": "planner failed: boom"},
                    102.0,
                ),
            ],
        )
        f = runview.read_run_facts(d)
        assert f["status"] == "error"
        assert f["reason"].startswith("planner failed")
        assert f["cost_usd"] == pytest.approx(0.0004)
        assert f["elapsed_s"] == pytest.approx(2.0, abs=0.1)
        assert f["files"] == []

    def test_cost_falls_back_to_usage_sum_when_absent(self, tmp_path: Path):
        d = tmp_path / "logs" / "fix-03"
        _write_run(
            d,
            [
                ("task_start", {"issue_text": "x"}, 100.0),
                ("model_response", {"step": "plan", "usage": {"cost": 0.001}}, 101.0),
                ("task_end", {"status": "failed", "reason": ""}, 101.5),
            ],
        )
        f = runview.read_run_facts(d)
        assert f["cost_usd"] == pytest.approx(0.001)

    def test_mode_from_task_start(self, tmp_path: Path):
        d = tmp_path / "logs" / "qa-01"
        _write_run(
            d,
            [
                (
                    "task_start",
                    {"mode": "question", "issue_text": "how does X work?"},
                    100.0,
                ),
                (
                    "model_response",
                    {"step": "answer", "usage": {"cost": 0.0001}},
                    101.0,
                ),
                ("task_end", {"status": "success", "mode": "question"}, 102.0),
            ],
        )
        f = runview.read_run_facts(d)
        assert f["mode"] == "question"
        assert f["status"] == "success"
        assert f["target_passed"] is None  # no verify rows for qa mode

    def test_missing_dir_is_total(self, tmp_path: Path):
        f = runview.read_run_facts(tmp_path / "nope")
        assert f["status"] is None
        assert f["elapsed_s"] is None
        assert f["files"] == []

    def test_garbage_lines_are_skipped(self, tmp_path: Path):
        d = tmp_path / "logs" / "fix-04"
        d.mkdir(parents=True)
        (d / "trace.jsonl").write_text(
            "not json\n"
            + json.dumps({"ts": 1.0, "kind": "result", "data": {"status": "failed"}})
            + "\n",
            encoding="utf-8",
        )
        f = runview.read_run_facts(d)
        assert f["status"] == "failed"


class TestCardLines:
    def test_success_card_rows(self):
        facts = {
            "task_id": "fix-01",
            "status": "success",
            "attempts": 1,
            "model_calls": 7,
            "elapsed_s": 116.4,
            "cost_usd": 0.009313,
            "issue": "mean() is wrong",
            "files": ["numlib/mathutil.py"],
            "target_passed": True,
            "regression_passed": True,
            "flaky": False,
            "verify_summary": "6 passed in 0.14s",
            "branch": "harness/fix-mean",
            "commit_sha": "a745eb2704d8",
        }
        rows = runview.card_lines(facts, mode="fix")
        joined = "\n".join(rows)
        assert "SUCCESS" in rows[0]
        assert "fix" in rows[0]
        assert "mean() is wrong" in rows[0]
        assert "1 attempt(s)" in joined
        assert "7 model calls" in joined
        assert "1m 56s" in joined
        assert "$0.009313" in joined
        assert "numlib/mathutil.py" in joined
        assert "PASS" in joined
        assert "harness/fix-mean" in joined
        assert "a745eb27" in joined

    def test_partial_verification_is_explicitly_unknown(self):
        rows = runview.card_lines(
            {
                "task_id": "partial",
                "status": "completed_unverified",
                "target_passed": True,
                "regression_passed": None,
            },
            mode="verified_fix",
        )
        assert "UNKNOWN" in "\n".join(rows)

    def test_failed_card_shows_reason_not_branch(self):
        facts = {
            "task_id": "fix-02",
            "status": "error",
            "reason": "planner failed: litellm timeout",
            "attempts": 0,
            "model_calls": 1,
            "elapsed_s": 30.0,
            "cost_usd": 0.0002,
            "issue": "fix the thing",
            "files": [],
            "branch": "",
        }
        rows = runview.card_lines(facts, mode="fix")
        joined = "\n".join(rows)
        assert "ERROR" in rows[0]
        assert "planner failed: litellm timeout" in joined
        assert "branch" not in joined

    def test_question_mode_card_is_read_only_variant(self):
        facts = {
            "task_id": "qa-01",
            "status": "success",
            "model_calls": 1,
            "elapsed_s": 12.0,
            "cost_usd": 0.0003,
            "issue": "how does the verify step work?",
            "mode": "question",
        }
        rows = runview.card_lines(facts, mode="question")
        joined = "\n".join(rows)
        assert "question" in rows[0]
        assert "qa-01" in joined
        assert "files" not in joined
        assert "tests" not in joined
        assert "branch" not in joined

    def test_missing_fields_drop_rows_not_crash(self):
        rows = runview.card_lines({}, mode="fix")
        assert rows
        assert "UNKNOWN" in rows[0]

    def test_issue_with_markup_is_escaped(self):
        facts = {"task_id": "t", "status": "success", "issue": "fix [bold]x[/bold] now"}
        rows = runview.card_lines(facts, mode="fix")
        assert "[bold]" in rows[0]  # escaped, not live markup


class TestFmtElapsed:
    @pytest.mark.parametrize(
        "secs,expect",
        [
            (None, ""),
            (0, "0s"),
            (34, "34s"),
            (59, "59s"),
            (61, "1m 01s"),
            (567, "9m 27s"),
            (3741, "1h 02m"),
        ],
    )
    def test_shapes(self, secs, expect):
        assert runview.fmt_elapsed(secs) == expect


class TestAgentProjection:
    def test_native_agent_events_provide_live_facts(self, tmp_path: Path):
        task_dir = tmp_path / "agent-live"
        task_dir.mkdir()
        events = [
            _ev("task_start", {"mode": "agent", "issue_text": "change the parser"}),
            _ev("model_request", {"step": "agent-1", "turn": 1}),
            _ev("model_response", {"usage": {"tokens": 12, "cost": 0.002}}),
            _ev("tool_call", {"tool": "read", "args": {"path": "src/a.py"}, "turn": 1}),
            _ev("tool_result", {"ok": False, "output": "missing file"}),
            _ev("tool_call", {"tool": "edit", "args": {"path": "src/a.py"}, "turn": 2}),
            _ev("edit_applied", {"path": "src/a.py"}),
            _ev("approval_required", {"tool": "write"}),
            _ev("approval_decided", {"tool": "write", "approved": True}),
            _ev(
                "verify",
                {"target_passed": True, "regression_passed": True, "raw": "ok"},
            ),
        ]
        for event in events:
            (task_dir / "trace.jsonl").open("a", encoding="utf-8").write(
                json.dumps(event) + "\n"
            )
        facts = runview.read_live_projection(task_dir)
        assert facts["status"] == "running"
        assert facts["current_turn"] == 2
        assert facts["changed_files"] == ["src/a.py"]
        assert facts["latest_verification"]["target_passed"] is True
        assert facts["approval"] == "approved"
        assert facts["last_error"] == "missing file"
        assert facts["model_calls"] == 1
        assert facts["tokens"] == 12
        assert facts["cost_usd"] == pytest.approx(0.002)

    def test_agent_projection_does_not_require_fix_state(self, tmp_path: Path):
        task_dir = tmp_path / "agent-no-state"
        task_dir.mkdir()
        (task_dir / "trace.jsonl").write_text(
            json.dumps(_ev("task_start", {"mode": "agent"})) + "\n", encoding="utf-8"
        )
        facts = runview.read_live_projection(task_dir)
        assert facts["changed_files"] == []
        assert facts["status"] == "running"
        assert "verify" in "\n".join(runview.status_lines(facts, live=True))

    def test_status_lines_shows_cost_and_elapsed(self):
        facts = {
            "status": "running",
            "current_action": "editing src/a.py",
            "current_turn": 3,
            "changed_files": ["src/a.py"],
            "latest_verification": {"target_passed": True},
            "approval": "not required",
            "elapsed_s": 12,
            "model_calls": 2,
            "tokens": 100,
            "cost_usd": 0.004,
        }
        text = "\n".join(runview.status_lines(facts, live=True))
        for value in (
            "editing src/a.py",
            "turn",
            "src/a.py",
            "PASS",
            "12s",
            "2 calls",
            "100 tokens",
            "$0.004",
        ):
            assert value in text

    def test_status_lines_preserve_explicit_zero_usage(self):
        text = "\n".join(
            runview.status_lines(
                {
                    "status": "running",
                    "model_calls": 0,
                    "model_calls_known": True,
                    "tokens": 0,
                    "tokens_known": True,
                    "cost_usd": 0.0,
                    "cost_known": True,
                },
                live=True,
            )
        )
        assert "0 calls" in text
        assert "0 tokens" in text
        assert "$0.0000" in text

    def test_status_lines_are_valid_textual_markup(self):
        from textual.content import Content

        from cli.tui import _m

        lines = runview.status_lines(
            {
                "status": "running",
                "current_action": "editing src/a.py",
                "current_turn": 3,
                "changed_files": ["src/a.py"],
                "latest_verification": {"target_passed": True},
                "approval": "not required",
                "elapsed_s": 12,
                "model_calls": 2,
                "tokens": 100,
                "cost_usd": 0.004,
            },
            live=True,
        )
        Content.from_markup(_m("\n".join(lines)))


class TestNormalizedJournalSurfaces:
    def test_event_parts_reads_normalized_event_shape(self):
        kind, data, timestamp, identity = runview.event_parts(
            {
                "event": "run_started",
                "payload": {"mode": "build", "request": "change it"},
                "timestamp": 12.5,
                "sequence": 3,
                "session_id": "s1",
                "run_id": "r1",
                "turn_id": "turn-1",
            }
        )
        assert kind == "run_started"
        assert data["request"] == "change it"
        assert timestamp == 12.5
        assert identity["sequence"] == 3
        assert identity["run_id"] == "r1"

    def test_projection_folds_canonical_lifecycle_and_mode_policy(self):
        projection = runview.RunProjection("r1", mode="plan")
        projection.consume(
            {
                "event": "run_started",
                "payload": {
                    "mode": "planning",
                    "run_spec": {"metadata": {"mode": "plan"}},
                },
                "timestamp": 1,
            }
        )
        projection.consume(
            {
                "event": "turn_started",
                "payload": {"turn": 2},
                "timestamp": 2,
            }
        )
        projection.consume(
            {
                "event": "tool_call",
                "payload": {
                    "tool": "edit",
                    "arguments": {"path": "a.py"},
                    "side_effect_class": "workspace_write",
                },
                "timestamp": 3,
            }
        )
        projection.consume(
            {
                "event": "checkpoint_saved",
                "payload": {
                    "checkpoint": {
                        "last_event_sequence": 4,
                        "agent_owned_changes": ["a.py"],
                        "resume_availability": "available",
                    }
                },
                "timestamp": 4,
            }
        )
        projection.consume(
            {
                "event": "lsp_diagnostics",
                "payload": {"items": [{"path": "a.py", "message": "hint"}]},
                "timestamp": 5,
            }
        )
        projection.consume(
            {
                "event": "run_finished",
                "payload": {"status": "completed_unverified"},
                "timestamp": 6,
            }
        )
        snapshot = projection.snapshot()
        assert snapshot["mode"] == "plan"
        assert snapshot["current_turn"] == 2
        assert snapshot["blocked_tools"] == ["edit"]
        assert snapshot["checkpoints"][0]["last_event_sequence"] == 4
        assert snapshot["diagnostics"][0]["message"] == "hint"
        assert snapshot["status"] == "completed_unverified"

    def test_readers_project_native_sidecar_records(self, tmp_path: Path):
        task_dir = tmp_path / "native"
        task_dir.mkdir()
        (task_dir / "trace.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "event": "checkpoint_saved",
                            "payload": {
                                "checkpoint": {"resume_token": "r", "created_at": 2}
                            },
                            "timestamp": 2,
                        }
                    ),
                    json.dumps(
                        {
                            "event": "lsp_diagnostics",
                            "payload": {"items": [{"message": "unused"}]},
                            "timestamp": 3,
                        }
                    ),
                    json.dumps(
                        {
                            "event": "context_built",
                            "payload": {"chars": 42, "sources": ["AGENTS.md"]},
                            "timestamp": 4,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert runview.read_checkpoints(task_dir)[0]["resume_token"] == "r"
        assert runview.read_diagnostics(task_dir)[0]["message"] == "unused"
        assert runview.read_context_receipt(task_dir)["chars"] == 42

    def test_canonical_unverified_status_is_not_success(self):
        lines = runview.card_lines(
            {"task_id": "r1", "status": "completed_unverified", "mode": "ask"},
            mode="ask",
        )
        assert "COMPLETED · UNVERIFIED" in lines[0]
        assert "SUCCESS" not in lines[0]

    def test_headless_status_uses_the_same_snapshot_renderer(self, tmp_path: Path):
        task_dir = tmp_path / "headless"
        task_dir.mkdir()
        (task_dir / "trace.jsonl").write_text(
            json.dumps(
                {
                    "event": "run_finished",
                    "payload": {"status": "completed_unverified"},
                    "timestamp": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines = runview.headless_status(task_dir, mode="ask")
        assert lines
        assert "COMPLETED · UNVERIFIED" in lines[0]


class TestTerminalEventProjection:
    @staticmethod
    def _event(sequence: int, event: str, payload: dict, *, run_id: str = "r1") -> dict:
        return {
            "schema_version": 1,
            "sequence": sequence,
            "session_id": "s1",
            "run_id": run_id,
            "turn_id": "turn-1",
            "timestamp": 100.0 + sequence,
            "event": event,
            "payload": payload,
        }

    def test_out_of_order_rows_wait_for_the_gap(self):
        projection = runview.RunProjection("t", mode="daily")
        assert projection.consume(self._event(1, "run_started", {"mode": "daily"}))
        assert projection.consume(
            self._event(3, "tool_call", {"tool": "read", "arguments": {"path": "a.py"}})
        )
        snapshot = projection.snapshot()
        assert snapshot["events"] == 1
        assert snapshot["pending_sequences"] == [3]
        assert "waiting for event 2" in snapshot["current_action"]
        assert projection.consume(self._event(2, "model_request", {"step": "plan"}))
        snapshot = projection.snapshot()
        assert snapshot["events"] == 3
        assert snapshot["pending_sequences"] == []
        assert snapshot["last_sequence"] == 3

    def test_duplicate_reconnect_delivery_is_idempotent(self):
        projection = runview.RunProjection("t", mode="question")
        rows = [
            self._event(1, "run_started", {"mode": "question"}),
            self._event(
                2, "model_response", {"usage": {"total_tokens": 9, "cost_usd": 0.25}}
            ),
            self._event(3, "run_finished", {"status": "completed_unverified"}),
        ]
        for row in rows:
            projection.consume(row)
        before = projection.snapshot()
        for row in [*rows[1:], rows[-1]]:
            projection.consume(row)
        after = projection.snapshot()
        assert after["events"] == before["events"]
        assert after["model_calls"] == before["model_calls"]
        assert after["tokens"] == before["tokens"] == 9
        assert after["cost_usd"] == before["cost_usd"] == 0.25
        assert after["duplicate_count"] == 3
        assert after["status"] == "completed_unverified"

    def test_schema_and_identity_violations_are_explicit(self):
        projection = runview.RunProjection("t")
        projection.consume(self._event(1, "run_started", {"mode": "daily"}))
        bad_schema = self._event(2, "model_request", {})
        bad_schema["schema_version"] = 2
        projection.consume(bad_schema)
        projection.consume(self._event(2, "model_request", {}, run_id="other"))
        projection.consume(self._event(2, "model_request", {}))
        snapshot = projection.snapshot()
        assert snapshot["warnings"]
        assert snapshot["last_sequence"] == 2
        assert any("schema" in warning for warning in snapshot["warnings"])
        assert any("identity" in warning for warning in snapshot["warnings"])

    def test_reads_do_not_become_changed_files(self):
        projection = runview.RunProjection("t", mode="daily")
        projection.consume(self._event(1, "run_started", {"mode": "daily"}))
        projection.consume(
            self._event(
                2, "tool_call", {"tool": "read", "arguments": {"path": "src/a.py"}}
            )
        )
        projection.consume(
            self._event(
                3, "tool_call", {"tool": "edit", "arguments": {"path": "src/a.py"}}
            )
        )
        assert projection.snapshot()["changed_files"] == ["src/a.py"]

    def test_verified_status_accepts_single_evidence_mapping(self):
        assert (
            runview.effective_terminal_status(
                "completed_verified",
                {"target_passed": True, "regression_passed": True},
            )
            == "completed_verified"
        )
        assert (
            runview.verification_state(
                {"target_passed": True, "regression_passed": True}
            )
            == "verified"
        )

    def test_verified_status_fails_closed_after_latest_failure(self):
        assert (
            runview.effective_terminal_status(
                "completed_verified",
                [
                    {"target_passed": True, "regression_passed": True},
                    {"target_passed": False, "regression_passed": False},
                ],
            )
            == "completed_unverified"
        )

    def test_approval_and_step_strings_are_not_truthy_by_accident(self):
        todo = runview.TodoModel()
        todo.consume(self._event(1, "plan", {"plan": [{"id": 1, "description": "x"}]}))
        todo.consume(self._event(2, "step_end", {"step_id": 1, "ok": "false"}))
        projection = runview.RunProjection("t")
        projection.consume(self._event(1, "approval_decided", {"approved": "false"}))
        assert todo.steps[0].state == runview.FAILED
        assert projection.snapshot()["approval"] == "rejected"

    def test_verification_strings_are_not_truthy_by_accident(self):
        evidence = {"target_passed": "false", "regression_passed": "false"}
        assert (
            runview.effective_terminal_status("completed_verified", evidence)
            == "completed_unverified"
        )
        assert runview.verification_state(evidence) == "failed"

    def test_nested_terminal_result_preserves_changed_files(self):
        projection = runview.RunProjection("t", mode="verified_fix")
        projection.consume(
            self._event(
                1,
                "run_finished",
                {
                    "result": {
                        "status": "completed_unverified",
                        "changed_files": ["a.py"],
                    }
                },
            )
        )
        assert projection.snapshot()["changed_files"] == ["a.py"]

    def test_nested_terminal_evidence_can_verify_projection(self):
        projection = runview.RunProjection("t", mode="verified_fix")
        projection.consume(
            self._event(
                1,
                "run_finished",
                {
                    "result": {
                        "status": "completed_verified",
                        "verification_evidence": [
                            {"target_passed": True, "regression_passed": True}
                        ],
                    }
                },
            )
        )
        snapshot = projection.snapshot()
        assert snapshot["status"] == "completed_verified"
        assert snapshot["verification_state"] == "verified"

    def test_verified_status_requires_clean_evidence(self):
        projection = runview.RunProjection("t", mode="verified_fix")
        projection.consume(self._event(1, "run_started", {"mode": "verified_fix"}))
        projection.consume(
            self._event(2, "run_finished", {"status": "completed_verified"})
        )
        assert projection.snapshot()["status"] == "completed_unverified"
        projection.consume(
            self._event(3, "verify", {"target_passed": True, "regression_passed": True})
        )
        projection.consume(
            self._event(4, "run_finished", {"status": "completed_verified"})
        )
        assert projection.snapshot()["status"] == "completed_verified"
        assert projection.snapshot()["verification_state"] == "verified"

    def test_six_projection_modes_are_stable(self):
        assert {
            runview.normalize_projection_mode(value)
            for value in ("question", "planning", "daily", "fix", "project", "mcp")
        } == {"question", "plan", "daily", "verified_fix", "build", "connector"}
        assert set(runview.MODE_PROJECTIONS) == {
            "question",
            "plan",
            "daily",
            "verified_fix",
            "build",
            "connector",
        }

    def test_read_run_facts_does_not_mint_success_from_a_gap(self, tmp_path):
        task_dir = tmp_path / "gap"
        task_dir.mkdir()
        rows = [
            self._event(1, "run_started", {"mode": "verified_fix"}),
            self._event(
                3, "run_finished", {"status": "completed_verified", "cost_usd": 9.0}
            ),
        ]
        (task_dir / "trace.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        facts = runview.read_run_facts(task_dir)
        assert facts["status"] == "running"
        assert facts["display_status"] != "completed_verified"
        assert facts["cost_usd"] is None
        assert facts["model_calls"] == 0
        assert facts["warnings"]

    def test_reset_stream_clears_all_journal_facts(self):
        projection = runview.RunProjection("t", mode="daily")
        rows = [
            self._event(1, "run_started", {"mode": "daily", "session": "s"}),
            self._event(2, "model_response", {"usage": {"tokens": 4, "cost": 0.25}}),
            self._event(
                3, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}}
            ),
            self._event(
                4, "verify", {"target_passed": True, "regression_passed": True}
            ),
        ]
        for row in rows:
            projection.consume(row)
        projection.reset_stream()
        snapshot = projection.snapshot()
        assert snapshot["events"] == 0
        assert snapshot["changed_files"] == []
        assert snapshot["model_calls"] == 0
        assert snapshot["model_calls_known"] is False
        assert snapshot["tokens_known"] is False
        assert snapshot["cost_known"] is False
        assert snapshot["verification_evidence"] == []
        assert snapshot["visible_tools"] == []
        assert snapshot["blocked_tools"] == []
        assert snapshot["warnings"] == []
