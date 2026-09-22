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
