"""Unit tests for harness.config, harness.trace, harness.context."""
import json
from pathlib import Path

from harness.config import DEFAULTS, get_config
from harness.context import (
    STATE_KEYS, TaskState, read_plan_bookkeeping, read_state,
)
from harness.trace import TraceLogger


def test_get_config_merges_defaults_and_overrides():
    merged = get_config({"max_retries": 5, "custom_key": "x"})
    assert merged["max_retries"] == 5
    assert merged["custom_key"] == "x"
    assert merged["budget_cap_usd"] == DEFAULTS["budget_cap_usd"]
    # task.config must not leak into DEFAULTS
    assert "custom_key" not in DEFAULTS


def test_get_config_handles_none():
    merged = get_config(None)  # type: ignore[arg-type]
    assert merged == dict(DEFAULTS)


def test_trace_logger_roundtrip(tmp_path):
    trace = TraceLogger(tmp_path)
    trace.log("task_start", {"task_id": "t1"})
    trace.log("tool_result", {"output": "exit=0"})
    trace.log("bare_event")
    events = trace.read_all()
    assert [e["kind"] for e in events] == ["task_start", "tool_result", "bare_event"]
    assert events[0]["data"]["task_id"] == "t1"
    assert "data" not in events[2]


def test_trace_logger_serializes_paths_and_dataclasses(tmp_path):
    from shared.types import ExecutionResult
    trace = TraceLogger(tmp_path)
    trace.log("x", {"path": Path("a/b.py"), "result": ExecutionResult(0, "out", "", False)})
    events = trace.read_all()
    # Path str() is platform-dependent — compare as Path, not raw string.
    assert Path(events[0]["data"]["path"]) == Path("a/b.py")
    assert events[0]["data"]["result"]["exit_code"] == 0


def test_state_file_schema(tmp_path):
    state = TaskState(tmp_path, "t-42")
    state.set_plan(["1. fix pop", "2. add test"])
    state.complete_step("1. fix pop")
    state.record_file_touched("stacklib/stack.py")
    state.record_decision("chose guard clause over try/except")

    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert list(on_disk.keys()) == list(STATE_KEYS)
    assert on_disk["task_id"] == "t-42"
    assert on_disk["completed_steps"] == ["1. fix pop"]
    assert on_disk["remaining_plan"] == ["2. add test"]
    assert on_disk["files_touched"] == ["stacklib/stack.py"]
    assert on_disk["decisions"] == ["chose guard clause over try/except"]


def test_state_file_records_every_update(tmp_path):
    state = TaskState(tmp_path, "t-42")
    state.set_plan(["1. only"])
    state.complete_step("1. only")
    state.reset_completed()
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["completed_steps"] == []
    assert on_disk["remaining_plan"] == ["1. only"]


def test_state_idempotent_file_touch(tmp_path):
    state = TaskState(tmp_path, "t")
    state.record_file_touched("a/b.py")
    state.record_file_touched("a\\b.py")  # backslash normalizes to same entry
    state.record_file_touched("a/b.py")
    assert state.files_touched == ["a/b.py"]


# ---------------------------------------------------------------------------
# resume bookkeeping (Task A)
# ---------------------------------------------------------------------------

def test_read_state_roundtrip_and_invalid(tmp_path):
    state = TaskState(tmp_path, "t-1")
    state.set_plan(["1. only"])
    loaded = read_state(tmp_path)
    assert loaded is not None
    assert loaded["task_id"] == "t-1"
    assert loaded["plan"] == ["1. only"]

    (tmp_path / "state.json").write_text("{ broken", encoding="utf-8")
    assert read_state(tmp_path) is None
    empty = tmp_path.parent / "no-such-dir"
    assert read_state(empty) is None


def test_task_state_resume_hydrates_from_disk(tmp_path):
    """A TaskState created with resume=True must inherit the pre-crash
    fields instead of wiping the file (THE original resume bug)."""
    state = TaskState(tmp_path, "t-9")
    state.set_plan(["1. fix", "2. polish"])
    state.complete_step("1. fix")
    state.record_file_touched("numlib/mathutil.py")
    state.record_decision("marker approach")

    revived = TaskState(tmp_path, "t-9", resume=True)
    assert revived.completed_steps == ["1. fix"]
    assert revived.files_touched == ["numlib/mathutil.py"]
    assert revived.decisions == ["marker approach"]
    assert revived.remaining_plan == ["2. polish"]
    # set_plan keeps the completed step (does not reset remaining)
    revived.set_plan(["1. fix", "2. polish"])
    assert revived.completed_steps == ["1. fix"]
    assert revived.remaining_plan == ["2. polish"]


def test_plan_bookkeeping_roundtrip(tmp_path):
    steps = [{"id": 1, "description": "d", "checkpoint": "c", "files_hint": []}]
    state = TaskState(tmp_path, "t-2")
    state.save_plan_steps(steps, attempts=2, cost_usd=0.5)
    loaded = read_plan_bookkeeping(tmp_path)
    assert loaded is not None
    assert loaded[0] == steps
    assert loaded[1] == 2
    assert loaded[2] == 0.5

    # missing / invalid file -> None (fresh start)
    gone = tmp_path.parent / "elsewhere"
    assert read_plan_bookkeeping(gone) is None
    (tmp_path / "plan.json").write_text("[]", encoding="utf-8")
    assert read_plan_bookkeeping(tmp_path) is None
    (tmp_path / "plan.json").write_text("not json", encoding="utf-8")
    assert read_plan_bookkeeping(tmp_path) is None
    # clamping of nonsense attempts values
    (tmp_path / "plan.json").write_text(
        json.dumps({"steps": steps, "attempts": -3, "cost_usd": "junk"}),
        encoding="utf-8")
    loaded = read_plan_bookkeeping(tmp_path)
    assert loaded is not None
    assert loaded[1] == 1
    assert loaded[2] == 0.0
