"""VEX-TERM-UX-03 second pass — the live run view as a projection of run events.

Every case here exists because it was measured, not because the prompt
asked for it. The four defects this file pins were each reproduced first
against a REAL `harness.agent_kernel` journal (no Docker, no provider, a
scripted model through the documented `ModelGateway(call_fn=...)` seam):

1. a `permission_decision` of ``action="ask"`` was folded as
   ``approval="rejected"`` and its policy reason became the run's
   persistent ``last_error``, so a run correctly WAITING for a human
   displayed a fabricated failure;
2. a gateway with no price table records ``cost_usd: 0.0``, and that was
   marked ``cost_known=True`` — rendering ``$0.0000``, which reads exactly
   like "this was free";
3. build/project journals open with ``project_start`` and close with
   ``project_end``; neither was in the projection's vocabulary, so a build
   run reported no mode, no sub-tasks, and NEVER a terminal status;
4. an event kind the projection cannot render was dropped silently while
   ``events`` still counted it, so the surface claimed to have understood
   a run whose vocabulary it ignored.

The ordering / deduplication / replay / reconnect / out-of-order and
kill-resume contracts below are the second requirement of the prompt, and
the vocabulary test is the first: the sixteen required visual kinds are
now DECLARED, so a renamed event or an unrenderable one is a loud
failure instead of a missing row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import runview


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


def _write_run(root: Path, task_id: str, rows: list) -> Path:
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return task_dir


def _fold(rows: list, *, mode: str = "daily") -> dict:
    projection = runview.RunProjection("t", mode=mode)
    for row in rows:
        projection.consume(row)
    return projection.snapshot()


# ---------------------------------------------------------------------------
# 1. The declared visual language
# ---------------------------------------------------------------------------


class TestDeclaredVisualLanguage:
    def test_every_required_visual_kind_is_declared(self):
        # The sixteen kinds the terminal prompt requires, spelled out here
        # independently of the module so a deletion is a test failure.
        required = {
            "task_start",
            "phase_change",
            "model_request",
            "model_response",
            "reasoning_summary",
            "tool_call",
            "tool_result",
            "file_read",
            "file_search",
            "patch_edit",
            "approval_request",
            "command_output",
            "verification",
            "checkpoint",
            "subagent_start",
            "subagent_end",
            "error_retry",
            "task_completion",
        }
        assert required <= set(runview.EVENT_VOCABULARY)

    def test_the_three_surfaces_that_share_a_vocabulary_agree(self):
        """A name accepted by the reader must also be declared."""
        accepted = {
            name for names in runview.EVENT_VOCABULARY.values() for name in names
        }
        for name in (
            "project_start",
            "project_end",
            "project_checkpoint",
            "project_final_verify",
            "project_sub_task_start",
            "project_sub_task_end",
            "spawn",
            "spawn_error",
            "model_delta",
        ):
            assert name in accepted, name

    def test_no_event_name_is_declared_twice_under_two_kinds(self):
        """One event, one visual kind — otherwise the mapping is a guess."""
        seen: dict[str, str] = {}
        for kind, names in runview.EVENT_VOCABULARY.items():
            for name in names:
                assert seen.setdefault(name, kind) == kind, name


# ---------------------------------------------------------------------------
# 2. Ordering, deduplication, replay, reconnect, out-of-order
# ---------------------------------------------------------------------------


class TestStreamContracts:
    def test_in_order_stream_folds_every_event_once(self):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "model_request", {"turn": 1}),
                _event(
                    3,
                    "model_response",
                    {"usage": {"total_tokens": 10, "cost_usd": 0.5}},
                ),
                _event(4, "run_finished", {"status": "completed_unverified"}),
            ]
        )
        assert snapshot["events"] == 4
        assert snapshot["last_sequence"] == 4
        assert snapshot["pending_sequences"] == []
        assert snapshot["status"] == "completed_unverified"

    def test_out_of_order_rows_buffer_until_the_gap_closes(self):
        projection = runview.RunProjection("t", mode="daily")
        projection.consume(_event(1, "run_started", {"mode": "daily"}))
        projection.consume(
            _event(4, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}})
        )
        snapshot = projection.snapshot()
        assert snapshot["events"] == 1
        assert snapshot["pending_sequences"] == [4]
        assert snapshot["out_of_order_count"] >= 1
        # The gap is stated, not hidden: the action says what is awaited.
        assert "waiting for event 2" in snapshot["current_action"]
        projection.consume(_event(2, "model_request", {"turn": 1}))
        projection.consume(_event(3, "model_response", {}))
        snapshot = projection.snapshot()
        assert snapshot["events"] == 4
        assert snapshot["pending_sequences"] == []
        # A buffered mutation still lands once the gap closes.
        assert snapshot["changed_files"] == ["a.py"]

    def test_duplicate_delivery_is_idempotent(self):
        rows = [
            _event(1, "run_started", {"mode": "daily"}),
            _event(
                2, "model_response", {"usage": {"total_tokens": 9, "cost_usd": 0.25}}
            ),
            _event(3, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}}),
        ]
        projection = runview.RunProjection("t", mode="daily")
        for row in rows:
            projection.consume(row)
        before = projection.snapshot()
        # A reconnect re-delivers the tail the consumer already folded.
        for row in rows:
            projection.consume(row)
        after = projection.snapshot()
        for key in ("events", "model_calls", "tokens", "cost_usd", "changed_files"):
            assert after[key] == before[key], key
        assert after["duplicate_count"] == len(rows)

    def test_replay_of_the_same_journal_is_identical(self):
        rows = [
            _event(1, "run_started", {"mode": "verified_fix"}),
            _event(2, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}}),
            _event(3, "verify", {"target_passed": True, "regression_passed": True}),
            _event(4, "run_finished", {"status": "completed_verified"}),
        ]
        first = _fold(rows, mode="verified_fix")
        second = _fold(rows, mode="verified_fix")
        for key in (
            "status",
            "verification_state",
            "changed_files",
            "model_calls",
            "cost_usd",
            "cost_known",
            "events",
        ):
            assert first[key] == second[key], key

    def test_reconnect_keeps_the_validated_cursor_and_counts_itself(self):
        projection = runview.RunProjection("t", mode="daily")
        projection.consume(_event(1, "run_started", {"mode": "daily"}))
        projection.consume(_event(2, "model_response", {"usage": {"total_tokens": 3}}))
        projection.mark_reconnect("event stream reconnected")
        # A reconnect re-delivers what the consumer already has; the fold
        # must not double it and must not forget it.
        projection.consume(_event(1, "run_started", {"mode": "daily"}))
        projection.consume(_event(2, "model_response", {"usage": {"total_tokens": 3}}))
        snapshot = projection.snapshot()
        assert snapshot["reconnect_count"] == 1
        assert snapshot["model_calls"] == 1
        assert snapshot["tokens"] == 3
        assert snapshot["tokens_known"] is True
        assert snapshot["stream_connected"] is True

    def test_torn_final_line_is_ignored_and_the_rest_still_folds(self, tmp_path):
        task_dir = _write_run(
            tmp_path,
            "torn",
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "model_request", {"turn": 1}),
            ],
        )
        path = task_dir / "trace.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"sequence": 3, "event": "model_res')
        facts = runview.read_live_projection(task_dir)
        assert facts["events"] == 2
        assert facts["status"] == "running"


# ---------------------------------------------------------------------------
# 3. A worker killed mid-run and resumed
# ---------------------------------------------------------------------------


class TestKilledAndResumed:
    def test_a_killed_run_stays_running_and_resumes_without_double_counting(
        self, tmp_path
    ):
        """The journal a killed worker leaves, then the resume that appends to it."""
        killed = [
            _event(1, "run_started", {"mode": "daily"}),
            _event(2, "model_request", {"turn": 1}),
            _event(
                3, "model_response", {"usage": {"total_tokens": 20, "cost_usd": 0.01}}
            ),
            _event(4, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}}),
            # no terminal row: the worker died here
        ]
        task_dir = _write_run(tmp_path, "killed", killed)
        facts = runview.read_live_projection(task_dir)
        assert facts["status"] == "running"
        assert facts["changed_files"] == ["a.py"]
        assert facts["model_calls"] == 1
        assert facts["tokens"] == 20
        assert facts["resumed"] is False

        resumed = [
            _event(5, "run_resumed", {}),
            _event(6, "model_request", {"turn": 2}),
            _event(
                7, "model_response", {"usage": {"total_tokens": 30, "cost_usd": 0.02}}
            ),
            _event(8, "verify", {"target_passed": True, "regression_passed": True}),
            _event(9, "run_finished", {"status": "completed_verified"}),
        ]
        with (task_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n".join(json.dumps(row) for row in resumed) + "\n")

        facts = runview.read_live_projection(task_dir)
        assert facts["resumed"] is True
        assert facts["status"] == "completed_verified"
        assert facts["verification_state"] == "verified"
        # Usage accumulates across the resume: the pre-crash call is still
        # part of what the run cost.
        assert facts["model_calls"] == 2
        assert facts["tokens"] == 50
        assert facts["cost_usd"] == pytest.approx(0.03)
        assert facts["changed_files"] == ["a.py"]

    def test_resume_replay_produces_exactly_one_completion(self, tmp_path):
        rows = [
            _event(1, "run_started", {"mode": "daily"}),
            _event(2, "tool_call", {"tool": "edit", "arguments": {"path": "a.py"}}),
            _event(3, "run_finished", {"status": "completed_unverified"}),
        ]
        task_dir = _write_run(tmp_path, "once", rows)
        # An attach/reconnect re-reads the whole journal from the start.
        first = runview.read_live_projection(task_dir)
        second = runview.read_live_projection(task_dir)
        assert first["status"] == second["status"] == "completed_unverified"
        assert len(second["result"]) == len(first["result"])

    def test_a_resumed_run_never_downgrades_a_verified_verdict(self, tmp_path):
        rows = [
            _event(1, "run_started", {"mode": "verified_fix"}),
            _event(2, "verify", {"target_passed": True, "regression_passed": True}),
            _event(3, "run_finished", {"status": "completed_verified"}),
            _event(4, "run_resumed", {}),
        ]
        facts = runview.read_live_projection(_write_run(tmp_path, "keep", rows))
        assert facts["resumed"] is True
        assert facts["status"] == "running"  # a resume re-opens the run
        assert facts["verification_state"] == "verified"  # evidence is kept


# ---------------------------------------------------------------------------
# 4. The four measured defects
# ---------------------------------------------------------------------------


class TestApprovalIsTriState:
    def test_a_pending_decision_is_waiting_not_rejected(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "permission_decision",
                    {"action": "ask", "reason": "default policy"},
                )
            ]
        )
        assert snapshot["approval"] == "waiting"
        assert snapshot["last_error"] == ""

    def test_a_pending_decision_keeps_its_reason_as_context_not_failure(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "permission_decision",
                    {"action": "ask", "reason": "default policy"},
                )
            ]
        )
        assert snapshot["approval_note"] == "default policy"
        assert snapshot["current_action"] == "waiting for approval"

    def test_a_real_denial_is_rejected_and_is_an_error(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "permission_decision",
                    {"action": "deny", "reason": "protected path"},
                )
            ]
        )
        assert snapshot["approval"] == "rejected"
        assert snapshot["last_error"] == "protected path"

    def test_an_explicit_boolean_decision_still_wins(self):
        assert (
            _fold([_event(1, "approval_decided", {"approved": True})])["approval"]
            == "approved"
        )
        assert (
            _fold([_event(1, "approval_decided", {"approved": False})])["approval"]
            == "rejected"
        )

    def test_an_unreadable_decision_is_not_reported_as_a_refusal(self):
        snapshot = _fold([_event(1, "permission_decision", {"scope": "once"})])
        assert snapshot["approval"] == "unknown"

    def test_a_denied_string_is_not_truthy_by_accident(self):
        snapshot = _fold([_event(1, "approval_decided", {"approved": "false"})])
        assert snapshot["approval"] == "rejected"


class TestUnpricedCostIsUnknown:
    def test_a_zero_cost_with_no_price_source_is_not_a_measurement(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "model_response",
                    {
                        "usage": {
                            "prompt_tokens": 10,
                            "total_tokens": 15,
                            "cost_usd": 0.0,
                        }
                    },
                )
            ]
        )
        assert snapshot["tokens"] == 15
        assert snapshot["tokens_known"] is True
        assert snapshot["cost_known"] is False
        assert snapshot["cost_source"] == "unpriced"

    def test_a_priced_receipt_is_a_measurement(self):
        snapshot = _fold([_event(1, "model_response", {"usage": {"cost_usd": 0.25}})])
        assert snapshot["cost_known"] is True
        assert snapshot["cost_usd"] == pytest.approx(0.25)

    def test_an_explicit_cost_source_prices_a_zero(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "model_response",
                    {"usage": {"cost_usd": 0.0, "cost_source": "provider"}},
                )
            ]
        )
        assert snapshot["cost_known"] is True
        assert snapshot["cost_source"] == "provider"

    def test_an_explicit_unpriced_flag_wins_over_a_nonzero_number(self):
        snapshot = _fold(
            [_event(1, "model_response", {"usage": {"cost_usd": 0.5, "priced": False}})]
        )
        assert snapshot["cost_known"] is False

    def test_the_priced_rule_is_the_one_the_ledger_uses(self, tmp_path):
        """One rule, two readers: a receipt priced here is priced there too."""
        ledger = tmp_path / "t.runtime"
        ledger.mkdir()
        (ledger / "model_ledger.jsonl").write_text(
            json.dumps({"cost_usd": 0.25, "outcome": "ok", "model": "m"}) + "\n",
            encoding="utf-8",
        )
        receipt = runview.model_call_receipts(tmp_path, "t")
        assert receipt[0]["priced"] is True
        assert runview._cost_is_priced({"cost_usd": 0.25}, 0.25) is True
        assert runview._cost_is_priced({"cost_usd": 0.0}, 0.0) is False
        assert (
            runview._cost_is_priced({"cost_usd": 0.0, "cost_source": "estimate"}, 0.0)
            is True
        )

    def test_a_terminal_result_does_not_mint_a_measured_zero(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "run_finished",
                    {"status": "completed_unverified", "cost_usd": 0.0},
                )
            ]
        )
        assert snapshot["cost_known"] is False
        assert snapshot["status"] == "completed_unverified"

    def test_an_unpriced_run_never_renders_a_dollar_amount(self):
        text = "\n".join(
            runview.status_lines(
                {
                    "status": "completed_unverified",
                    "model_calls": 2,
                    "model_calls_known": True,
                    "tokens": 40,
                    "tokens_known": True,
                    "cost_usd": 0.0,
                    "cost_known": False,
                },
                live=True,
            )
        )
        assert "$" not in text
        assert "40 tokens" in text


class TestBuildProjectProjection:
    def _project_rows(self):
        return [
            _event(
                1, "project_start", {"project_id": "p1", "request_text": "add a flag"}
            ),
            _event(2, "project_plan_generated", {"sub_tasks": 2}),
            _event(3, "project_sub_task_start", {"sub_task_id": "s1"}),
            _event(4, "project_sub_task_end", {"sub_task_id": "s1"}),
            _event(5, "project_checkpoint", {"resume_token": "cp-1"}),
            _event(
                6,
                "project_final_verify",
                {"target_passed": True, "regression_passed": True},
            ),
            _event(7, "project_end", {"status": "success"}),
        ]

    def test_a_project_journal_reaches_a_terminal_status(self):
        snapshot = _fold(self._project_rows(), mode="build")
        assert snapshot["status"] == "completed_verified"
        assert snapshot["verification_state"] == "verified"

    def test_a_project_start_selects_the_build_projection(self):
        snapshot = _fold(self._project_rows(), mode="build")
        assert snapshot["mode"] == "build"
        assert snapshot["mode_label"] == "Build / project"
        assert snapshot["issue"] == "add a flag"
        assert snapshot["project_id"] == "p1"

    def test_sub_tasks_appear_in_the_subagent_tree(self):
        snapshot = _fold(self._project_rows(), mode="build")
        assert [item["id"] for item in snapshot["subagents"]] == ["s1"]
        assert snapshot["subagents"][0]["status"] == "finished"

    def test_a_project_checkpoint_is_retained_by_both_readers(self, tmp_path):
        task_dir = _write_run(tmp_path, "p1", self._project_rows())
        facts = runview.read_live_projection(task_dir)
        assert [item.get("resume_token") for item in facts["checkpoints"]] == ["cp-1"]

    def test_a_project_success_without_evidence_stays_unverified(self):
        rows = [
            _event(1, "project_start", {"project_id": "p1", "request_text": "x"}),
            _event(2, "project_end", {"status": "success"}),
        ]
        snapshot = _fold(rows, mode="build")
        assert snapshot["status"] == "completed_unverified"

    def test_a_project_parse_error_is_visible(self):
        snapshot = _fold(
            [
                _event(1, "project_start", {"project_id": "p1"}),
                _event(2, "project_plan_parse_error", {"error": "no plan in reply"}),
            ],
            mode="build",
        )
        assert snapshot["last_error"] == "no plan in reply"


class TestSubagentAndRetryVocabulary:
    def test_a_spawn_appears_and_its_error_is_reported(self):
        snapshot = _fold(
            [
                _event(1, "spawn", {"id": "c1", "agent": "explorer"}),
                _event(2, "spawn_error", {"id": "c1", "error": "no_runtime"}),
            ]
        )
        assert snapshot["subagents"][0]["role"] == "explorer"
        assert snapshot["subagents"][0]["status"] == "failed"
        assert snapshot["last_error"] == "no_runtime"

    def test_a_crash_retry_keeps_the_run_live(self):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "crash_retry", {"attempt": 2, "reason": "worker died"}),
            ]
        )
        assert snapshot["status"] == "running"
        assert "worker died" in snapshot["current_action"]

    def test_a_hang_timeout_is_an_error(self):
        snapshot = _fold(
            [_event(1, "hang_timeout", {"error": "no heartbeat for 300s"})]
        )
        assert snapshot["last_error"] == "no heartbeat for 300s"

    def test_a_streamed_chunk_is_not_counted_as_a_model_call(self):
        snapshot = _fold(
            [
                _event(1, "model_response", {"usage": {"total_tokens": 5}}),
                _event(2, "model_delta", {"delta": "hello "}),
                _event(3, "model_delta", {"delta": "world"}),
            ]
        )
        assert snapshot["model_calls"] == 1
        assert snapshot["stream_chars"] == len("hello world")
        assert snapshot["current_action"] == "streaming response"


class TestUnknownStateIsExplicit:
    def test_an_unrenderable_event_is_recorded_not_swallowed(self):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "quantum_flux_phase", {}),
                _event(3, "hologram_sync", {}),
                _event(4, "quantum_flux_phase", {}),
            ]
        )
        assert snapshot["unmapped_kinds"] == ["hologram_sync", "quantum_flux_phase"]
        assert snapshot["unmapped_events"] == 3

    def test_a_fully_understood_run_reports_no_unreadable_events(self):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "turn_started", {"turn": 1}),
                _event(3, "model_response", {"usage": {"total_tokens": 2}}),
                _event(4, "tool_call", {"tool": "read", "arguments": {"path": "a.py"}}),
                _event(5, "tool_result", {"ok": True}),
                _event(6, "turn_recorded", {"changed_files": []}),
                _event(7, "checkpoint_saved", {"resume_token": "cp"}),
                _event(8, "run_finished", {"status": "completed_unverified"}),
            ]
        )
        assert snapshot["unmapped_kinds"] == []
        assert snapshot["unmapped_events"] == 0

    def test_reset_clears_the_unreadable_record(self):
        projection = runview.RunProjection("t", mode="daily")
        projection.consume(_event(1, "run_started", {"mode": "daily"}))
        projection.consume(_event(2, "quantum_flux_phase", {}))
        projection.reset_stream()
        snapshot = projection.snapshot()
        assert snapshot["unmapped_kinds"] == []
        assert snapshot["unmapped_events"] == 0

    def test_progress_receipts_are_not_reported_as_unreadable(self):
        """An event with no state change of its own is not a vocabulary gap."""
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "daily"}),
                _event(2, "execution_backend_ready", {}),
                _event(3, "knowledge_bound", {}),
                _event(4, "context_compiled", {}),
                _event(5, "context_budget", {}),
                _event(6, "turn_recorded", {}),
                _event(7, "knowledge_close", {}),
            ]
        )
        assert snapshot["unmapped_kinds"] == []


# ---------------------------------------------------------------------------
# 5. The six mode projections
# ---------------------------------------------------------------------------


class TestSixModeProjections:
    def test_each_mode_declares_its_verification_expectation(self):
        expectations = {
            mode: runview.MODE_PROJECTIONS[mode]["verification"]
            for mode in runview.MODE_PROJECTIONS
        }
        assert expectations == {
            "question": "not_applicable",
            "plan": "not_run",
            "daily": "optional",
            "verified_fix": "required",
            "build": "required",
            "connector": "not_applicable",
        }

    @pytest.mark.parametrize(
        "mode",
        ["question", "plan", "daily", "verified_fix", "build", "connector"],
    )
    def test_every_mode_projects_the_same_required_live_facts(self, mode):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": mode}),
                _event(
                    2, "model_response", {"usage": {"total_tokens": 4, "cost_usd": 0.1}}
                ),
                _event(3, "checkpoint_saved", {"resume_token": "cp"}),
            ],
            mode=mode,
        )
        for key in (
            "current_action",
            "changed_files",
            "verification_state",
            "approval",
            "elapsed_s",
            "model_calls",
            "tokens",
            "cost_usd",
        ):
            assert key in snapshot, key
        assert snapshot["mode"] == mode
        assert snapshot["model_calls"] == 1

    def test_a_read_only_mode_does_not_claim_verification(self):
        snapshot = _fold(
            [
                _event(1, "run_started", {"mode": "question"}),
                _event(2, "run_finished", {"status": "completed"}),
            ],
            mode="question",
        )
        assert snapshot["status"] == "completed_unverified"
        assert snapshot["verification_state"] in {"not_run", "unknown"}


# ---------------------------------------------------------------------------
# 6. The transcript stays clean
# ---------------------------------------------------------------------------


class TestTranscriptHygiene:
    def test_a_captured_ansi_banner_never_becomes_the_action(self):
        snapshot = _fold(
            [
                _event(
                    1,
                    "tool_result",
                    {"ok": True, "output": "\x1b[31mWARNING\x1b[0m litellm banner\r\n"},
                )
            ]
        )
        assert "\x1b" not in snapshot["current_action"]
        assert "\r" not in snapshot["current_action"]

    def test_only_one_terminal_row_is_ever_accepted_as_the_result(self):
        rows = [
            _event(1, "run_started", {"mode": "daily"}),
            _event(2, "completion_decision", {"status": "completed_unverified"}),
            _event(3, "run_finished", {"status": "completed_unverified"}),
        ]
        projection = runview.RunProjection("t", mode="daily")
        for row in rows:
            projection.consume(row)
        snapshot = projection.snapshot()
        assert snapshot["status"] == "completed_unverified"
        assert snapshot["events"] == 3
        # The answer is the terminal fact, not a concatenation of both rows.
        assert isinstance(snapshot["answer"], str)
