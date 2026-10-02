"""Fail-closed daily-driver TUI performance and interaction contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from evals import daily_driver

_REQUIRED_TUI_RECEIPTS = (
    "tui_status_populated_receipt",
    "tui_diff_populated_receipt",
    "tui_input_ack_receipt",
    "tui_event_stream_receipt",
    "tui_command_response_receipt",
    "tui_modal_open_receipt",
    "tui_resize_recovery_receipt",
    "tui_memory_cpu_receipt",
    "tui_ui_stall_receipt",
    "tui_performance_gates_receipt",
    "tui_verifier_gate_receipt",
)


def _run_tui_worker(root: Path) -> dict:
    command = [
        sys.executable,
        "-m",
        "evals.daily_driver",
        "--case",
        "dd_20_live_tui_status_diff",
        "--arm",
        "baseline",
        "--case-root",
        str(root),
    ]
    completed = subprocess.run(
        command,
        cwd=daily_driver.REPO_ROOT,
        env=daily_driver._isolated_child_env(root),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "completed_verified", result
    return result


def _assert_hard_performance_contract(result: dict) -> None:
    metrics = result["metrics"]
    performance = metrics["performance"]
    assert result["ui_thread_stalls"] == 0
    assert len(metrics["event_to_ui_latency_ms"]) >= 3
    assert performance["sampled_event_sequences"] == [2, 3, 4, 5]
    assert len(performance["event_samples"]) >= 3
    assert all(sample["visible"] is True for sample in performance["event_samples"])
    assert len({sample["sequence"] for sample in performance["event_samples"]}) >= 3
    assert len(metrics["input_ack_latency_ms"]) >= 3
    assert all(sample["visible"] is True for sample in performance["input_ack_samples"])
    assert metrics["input_ack_latency_ms"]
    assert metrics["event_to_ui_latency_ms"]
    assert performance["input_ack"]["p95"] < 100
    assert performance["event_to_ui"]["p95"] < 250
    assert performance["gates"] == {
        "input_ack_p95_under_100_ms": True,
        "event_to_ui_p95_under_250_ms": True,
        "no_ui_thread_stall_over_500_ms": True,
    }
    assert metrics["ui_thread_stall_ms"]
    assert max(metrics["ui_thread_stall_ms"]) <= 500
    assert len(metrics["command_response_latency_ms"]) >= 1
    assert all(sample["visible"] is True for sample in performance["command_samples"])
    assert len(metrics["modal_open_latency_ms"]) >= 1
    assert all(sample["visible"] is True for sample in performance["modal_samples"])
    assert len(metrics["resize_recovery_latency_ms"]) >= 1
    assert all(sample["recovered"] is True for sample in performance["resize_samples"])
    assert len(metrics["memory_rss_mb"]) >= 2
    assert all(isinstance(value, (int, float)) for value in metrics["memory_rss_mb"])
    assert len(metrics["cpu_percent"]) >= 2
    assert all(isinstance(value, (int, float)) for value in metrics["cpu_percent"])


def test_live_tui_populates_status_diff_and_non_vacuous_performance(tmp_path):
    result = _run_tui_worker(tmp_path / "live")
    for receipt in _REQUIRED_TUI_RECEIPTS:
        assert result["receipts"].get(receipt) is True, receipt
    assert result["assertions"]["cost_and_events_populated"] is True
    assert result["assertions"]["meaningful_final_diff"] is True
    assert result["assertions"]["worker_drained"] is True
    assert result["assertions"]["final_status_idle"] is True
    assert result["evidence"]["journal_event_count"] >= 12
    assert result["evidence"]["command_response_visible"] is True
    assert result["evidence"]["modal_seen"] is True
    assert result["evidence"]["resize_state_preserved"] is True
    assert result["evidence"]["verifier"]["kind"] == "deterministic_local_test_function"
    assert result["evidence"]["verifier"]["docker_used"] is False
    assert result["evidence"]["verifier"]["provider_used"] is False
    _assert_hard_performance_contract(result)


def test_tui_contract_repeats_in_fixed_order_without_leaks(tmp_path):
    results = [_run_tui_worker(tmp_path / f"repeat-{index}") for index in range(3)]
    for result in results:
        assert result["assertions"]["global_hooks_restored"] is True
        assert result["assertions"]["builtins_restored"] is True
        assert result["unauthorized_mutations"] == 0
        assert result["lost_edits"] == 0
        assert result["assertions"]["worker_drained"] is True
        assert result["assertions"]["final_status_idle"] is True
        _assert_hard_performance_contract(result)


def test_live_status_diff_renderer_balances_markup():
    from rich.markup import render
    from rich.text import Text

    import cli.tui as tui
    import cli.ui as ui
    from cli.tui_components import LoadingState

    state = LoadingState(
        task_id="task-with-markup",
        phase="working [/] literal payload",
        mode="agent_task",
        events=2,
        cost_text="$0.0001",
        elapsed_s=3,
        thinking=True,
        spinner="*",
        joke="",
    )
    render(tui._m(state.render()))
    diff_lines = ui.diff_render_lines(
        "--- a/app.py\n+++ b/app.py\n@@\n+literal [/] payload\n"
    )
    assert diff_lines
    assert all(isinstance(line, Text) for line in diff_lines)
    assert "literal [/] payload" in diff_lines[-1].plain


def test_tui_case_has_no_hidden_skip_or_vacuous_receipt():
    case = daily_driver.case_map()["dd_20_live_tui_status_diff"]
    assert case.lane == "deterministic_tui"
    assert case.required_receipts["baseline"] == _REQUIRED_TUI_RECEIPTS
    assert case.required_receipts["adversarial"] == _REQUIRED_TUI_RECEIPTS


def test_tui_worker_artifacts_stay_under_private_root(tmp_path):
    root = tmp_path / "private"
    _run_tui_worker(root)
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert (root / "logs" / "tui-live-task" / "trace.jsonl").is_file()
    assert (root / "result.json").is_file()
