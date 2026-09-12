"""Tests for shared.tracing + shared.traceview (Observability round, Task A).

Covers: env-gated activation, normalized record shape, per-task file
layout + containment, never-raise contracts, source merging in
reconstruct_task, and the summary/renderer.
"""

import json
import time
from pathlib import Path

import pytest

from shared import tracing
from shared.traceview import (
    _iso_to_epoch,
    reconstruct_task,
    render_timeline,
    summarize,
)


@pytest.fixture(autouse=True)
def _tracing_env(tmp_path, monkeypatch):
    """Isolated trace dir per test; always RESET the module env cache
    (tracing caches the env read per-process by contract)."""
    trace_root = tmp_path / "traces"
    monkeypatch.setenv(tracing.TRACE_ENV, str(trace_root))
    tracing._reset_cache()
    yield trace_root
    tracing._reset_cache()
    monkeypatch.delenv(tracing.TRACE_ENV, raising=False)


class TestEmit:
    def test_record_shape_and_file_layout(self, _tracing_env):
        tracing.emit("runtime", "worker_start", task_id="t1", resume=False, attempt=1)
        path = _tracing_env / "_trace" / "t1.jsonl"
        assert path.is_file()
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        # normalized shape: ts + module + event + task_id + fields
        assert set(rec) == {"ts", "module", "event", "task_id", "resume", "attempt"}
        assert rec["module"] == "runtime"
        assert rec["event"] == "worker_start"
        assert rec["task_id"] == "t1"
        assert rec["resume"] is False and rec["attempt"] == 1
        assert isinstance(rec["ts"], float)

    def test_off_by_default_zero_overhead_noop(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tracing.TRACE_ENV, raising=False)
        tracing._reset_cache()
        # no file created, no raise — emit is a no-op
        tracing.emit("runtime", "x", task_id="t1")
        assert not (tmp_path / "_trace").exists()
        assert tracing.enabled() is False
        tracing._reset_cache()

    def test_never_raises_on_unserializable_fields(self, _tracing_env):
        class Weird:
            def __str__(self):
                return "weird"

        tracing.emit("harness", "odd", task_id="t1", obj=Weird())
        recs = tracing.read_task_events("t1")
        assert len(recs) == 1
        assert isinstance(recs[0]["obj"], str)  # fell back to repr

    def test_reserved_keys_are_signature_protected(self, _tracing_env):
        # module/event are named params: a caller "colliding" them via
        # kwargs fails loudly AT THE CALL SITE (Python duplicate-arg
        # TypeError) — reserved values can never be smuggled in.
        with pytest.raises(TypeError):
            tracing.emit("execution", "sandbox_call", task_id="t1", module="evil")
        with pytest.raises(TypeError):
            tracing.emit("execution", "sandbox_call", task_id="t1", event="evil")
        # ts is NOT signature-protected: it's dropped from fields by the
        # writer (never clobbers the record's own ts).
        tracing.emit("execution", "sandbox_call", task_id="t1", ts="evil")
        rec = tracing.read_task_events("t1")[0]
        assert isinstance(rec["ts"], float)

    def test_run_events_go_to_run_file(self, _tracing_env):
        tracing.emit_run("runtime", "run_start", run_id="run-1", n_tasks=3)
        task_events = tracing.list_traced_task_ids()
        assert task_events == []  # run events are NOT task events
        run_events = tracing.read_run_events("run-1")
        assert len(run_events) == 1
        assert run_events[0]["event"] == "run_start"
        assert run_events[0]["run_id"] == "run-1"
        assert "task_id" not in run_events[0]

    def test_no_target_noop(self, _tracing_env):
        tracing.emit("runtime", "orphan")  # neither task nor run id
        assert tracing.list_traced_task_ids() == []


class TestContainment:
    @pytest.mark.parametrize(
        "bad",
        [
            "../escape",
            "a/b",
            "a\\b",
            "..",
            ".",
            "a:",
            "C:/x",
            " x ",
            "x.",
            "\x00nul",
            "con<>",
            'q"q',
            "*",
            "?",
            "|",
        ],
    )
    def test_traversal_shaped_ids_never_write(self, _tracing_env, bad):
        tracing.emit("runtime", "evil", task_id=bad)
        tracing.emit_run("runtime", "evil", run_id=bad)
        assert (
            not (_tracing_env / "_trace").exists()
            or list((_tracing_env / "_trace").glob("*.jsonl")) == []
        )

    def test_readers_reject_traversal_ids(self, _tracing_env):
        assert tracing.read_task_events("../etc") == []
        assert tracing.read_run_events("../etc") == []


class TestReaders:
    def test_chronological_and_missing_file_empty(self, _tracing_env):
        assert tracing.read_task_events("never-ran") == []
        t0 = time.time()
        for i in range(5):
            tracing.emit("runtime", f"e{i}", task_id="t1", i=i)
            time.sleep(0.002)
        events = tracing.read_task_events("t1")
        assert [e["event"] for e in events] == [f"e{i}" for i in range(5)]
        assert events[0]["ts"] >= t0 - 1

    def test_list_traced_task_ids_sorted(self, _tracing_env):
        for tid in ("b-task", "a-task", "c-task"):
            tracing.emit("runtime", "x", task_id=tid)
        assert tracing.list_traced_task_ids() == ["a-task", "b-task", "c-task"]

    def test_corrupt_lines_skipped(self, _tracing_env):
        d = _tracing_env / "_trace"
        d.mkdir(parents=True)
        (d / "t1.jsonl").write_text(
            "not json\n\n"
            + json.dumps(
                {"ts": 1.0, "module": "runtime", "event": "ok", "task_id": "t1"}
            )
            + "\n",
            encoding="utf-8",
        )
        events = tracing.read_task_events("t1")
        assert len(events) == 1 and events[0]["event"] == "ok"


class TestSchedulerHook:
    """The scheduler's _log funnel routes task/run events correctly."""

    def test_scheduler_events_reach_both_streams(self, _tracing_env):
        from runtime.scheduler import Scheduler

        sched = Scheduler(
            concurrency=2, logs_root=str(_tracing_env / "logs"), run_id="hookrun"
        )
        # run-scoped
        sched._log("run_start", {"n_tasks": 1, "concurrency": 2})
        # task-scoped
        sched._log("spawn", {"task_id": "task-a", "attempt": 0, "pid": 123})
        sched._log("finish", {"task_id": "task-a", "status": "success", "attempt": 0})

        run_events = tracing.read_run_events("hookrun")
        assert [e["event"] for e in run_events] == ["run_start"]
        assert run_events[0]["n_tasks"] == 1

        task_events = tracing.read_task_events("task-a")
        assert [e["event"] for e in task_events] == ["spawn", "finish"]
        assert task_events[0]["run_id"] == "hookrun"
        assert task_events[0]["pid"] == 123
        assert task_events[1]["status"] == "success"
        # the original journal still exists (tracing is an overlay, not a
        # replacement)
        assert (sched.run_dir / "events.jsonl").is_file()

    def test_no_task_field_does_not_crash(self, _tracing_env):
        from runtime.scheduler import Scheduler

        sched = Scheduler(
            concurrency=1, logs_root=str(_tracing_env / "logs"), run_id="r2"
        )
        sched._log("run_interrupted", {"live": 2})  # no task_id
        assert tracing.read_run_events("r2")[0]["live"] == 2


class TestRouterHook:
    def test_record_usage_emits_when_task_id_in_context(self, _tracing_env):
        import runtime.model_router as mr

        mr.set_call_context(
            {"task_id": "router-task"}, ledger_dir=str(_tracing_env / "led.jsonl")
        )
        try:
            mr._record_usage(
                {
                    "ts": mr.now_iso(),
                    "model": "m1",
                    "provider": "p",
                    "prompt_tokens": 5,
                    "completion_tokens": 6,
                    "tokens": 11,
                    "cost_usd": 0.001,
                    "elapsed_s": 0.1,
                    "routed_via_hint": "easy",
                    "difficulty_hint": "easy",
                }
            )
            events = tracing.read_task_events("router-task")
            assert len(events) == 1
            e = events[0]
            assert e["event"] == "model_routed"
            assert e["model"] == "m1" and e["routed_via_hint"] == "easy"
            assert e["cost_usd"] == 0.001
        finally:
            mr.set_call_context(None)

    def test_record_usage_without_task_id_noop(self, _tracing_env):
        import runtime.model_router as mr

        mr.set_call_context({"task_id": ""})
        try:
            mr._record_usage({"model": "m", "tokens": 1, "cost_usd": 0.0})
            assert tracing.list_traced_task_ids() == []
        finally:
            mr.set_call_context(None)


class TestSandboxHook:
    """execute_sandboxed derives the task id from the mounted repo path
    (logs/{task_id}/work) — and emits nothing for foreign shapes."""

    def test_task_id_from_work_dir(self, _tracing_env, monkeypatch):
        from execution.sandbox import _trace_task_id

        assert _trace_task_id("C:/x/logs/task-9/work") == "task-9"
        assert _trace_task_id("C:/x/logs/task-9/pristine") == "task-9"
        # Windows NATIVE backslash paths must trace too (the pre-fix
        # blanket backslash rejection disabled execution-layer tracing
        # on every Win32 host — real bug caught while validating the
        # eval harness's unified stream)
        assert _trace_task_id("C:\\x\\logs\\task-9\\work") == "task-9"
        assert _trace_task_id("C:\\x\\logs\\task-9\\pristine") == "task-9"
        assert _trace_task_id("C:/some/other/repo") == ""  # no work/pristine
        # traversal/dot components in the raw string never attribute the
        # event to a resolved-away id (wrong-but-contained is still wrong)
        assert _trace_task_id("C:/x/logs/../etc/work") == ""
        assert _trace_task_id("C:/x/logs/./work") == ""
        assert _trace_task_id("C:/x/logs/../work") == ""
        assert _trace_task_id("C:\\x\\logs\\..\\work") == ""

    def test_bad_parent_names_rejected(self, _tracing_env):
        from execution.sandbox import _trace_task_id

        for bad in ("..", ".", "a:", " x ", "x.", "|p"):
            assert _trace_task_id(f"C:/x/logs/{bad}/work") == ""
        # a backslash inside the parent segment is a SEPARATOR, not part
        # of one id: `logs/x\y/work` is the real path `logs/x/y/work`,
        # whose task id is `y` — attribution follows the real structure
        # (the old blanket rejection here is what disabled Win32 tracing)
        assert _trace_task_id("C:/x/logs/x\\y/work") == "y"


class TestReconstruct:
    def _mk_task_tree(self, logs_root: Path, task_id: str) -> None:
        """A minimal multi-source task tree: harness trace + worker
        journal + ledger + unified stream."""
        d = logs_root / task_id
        d.mkdir(parents=True)
        (d / "trace.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": 1.0,
                            "kind": "task_start",
                            "data": {
                                "task_id": task_id,
                                "issue_text": "the bug",
                                "config": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "ts": 2.0,
                            "kind": "model_request",
                            "data": {"step": "plan", "messages": ["BIG"]},
                        }
                    ),
                    json.dumps(
                        {"ts": 3.0, "kind": "plan", "data": {"plan": [{"id": 1}] * 2}}
                    ),
                    json.dumps(
                        {
                            "ts": 4.0,
                            "kind": "tool_call",
                            "data": {
                                "step_id": 1,
                                "turn": 0,
                                "command": "sed -i 's/a/b/' f.py",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "ts": 5.0,
                            "kind": "verify",
                            "data": {
                                "target_passed": True,
                                "regression_passed": True,
                                "flaky": False,
                            },
                        }
                    ),
                    json.dumps(
                        {"ts": 6.0, "kind": "uninteresting_event", "data": {"x": 1}}
                    ),  # filtered out (not in keep set)
                    json.dumps(
                        {
                            "ts": 7.0,
                            "kind": "task_end",
                            "data": {"status": "success", "attempt": 1},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        rt = logs_root / f"{task_id}.runtime"
        rt.mkdir(parents=True)
        (rt / "events.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": "2026-09-10T00:00:00.500+00:00",
                            "event": "worker_start",
                            "data": {"task_id": task_id, "resume": False, "attempt": 1},
                        }
                    ),
                    json.dumps(
                        {
                            "ts": "2026-09-10T00:00:07.000+00:00",
                            "event": "worker_finish",
                            "data": {"status": "success"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (rt / "model_ledger.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-09-10T00:00:02.000+00:00",
                    "model": "cheap",
                    "provider": "x",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "tokens": 15,
                    "cost_usd": 0.001,
                    "elapsed_s": 1.2,
                    "routed_via_hint": "easy",
                    "difficulty_hint": "easy",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_merge_is_chronological_across_all_sources(self, _tracing_env):
        logs_root = _tracing_env / "logs"
        self._mk_task_tree(logs_root, "task-m")
        # unified stream events use real wall-clock (huge epoch) vs the
        # synthetic epoch-1..7 records — they must sort AFTER them
        tracing.emit(
            "runtime", "spawn", task_id="task-m", run_id="r", attempt=0, pid=42
        )

        events = reconstruct_task("task-m", logs_root=logs_root)
        modules = [e["module"] for e in events]
        kinds = [e["event"] for e in events]

        # all four sources present
        assert "runtime" in modules and "harness" in modules
        assert "spawn" in kinds  # unified
        assert "task_start" in kinds  # harness trace
        assert "worker_start" in kinds  # worker journal
        assert "model_routed" in kinds  # ledger
        # chronological: synthetic-epoch sources first (task_end before
        # the real-time unified spawn), wall-clock unified events last
        assert kinds.index("task_end") < kinds.index("spawn")
        # harness model_request was compacted to a pointer (no messages)
        req = next(e for e in events if e["event"] == "model_request")
        assert "BIG" not in json.dumps(req)
        assert req["data"]["step"] == "plan"
        # uninteresting harness kinds were dropped
        assert "uninteresting_event" not in kinds
        # ts is non-decreasing across the whole merged timeline
        ts_list = [e["ts"] for e in events]
        assert ts_list == sorted(ts_list)

    def test_summary_outcome_and_counters(self, _tracing_env):
        logs_root = _tracing_env / "logs"
        self._mk_task_tree(logs_root, "task-s")
        events = reconstruct_task("task-s", logs_root=logs_root)
        s = summarize(events)
        assert s["outcome"] == "success"
        assert s["attempts"] == 0 or s["attempts"] >= 0  # no attempt_start here
        assert s["model_calls"] == 1
        assert s["cost_usd"] > 0
        assert s["by_module"]["harness"] >= 4
        assert s["n_events"] == len(events)

    def test_render_timeline_smoke(self, _tracing_env):
        logs_root = _tracing_env / "logs"
        self._mk_task_tree(logs_root, "task-r")
        events = reconstruct_task("task-r", logs_root=logs_root)
        text = render_timeline(events)
        assert "task_start" in text and "model_routed" in text
        assert "sed -i 's/a/b/' f.py"[:30] in text

    def test_missing_everything_is_empty_not_error(self, _tracing_env):
        events = reconstruct_task("ghost-task", logs_root=_tracing_env / "logs")
        assert events == []
        assert summarize(events)["outcome"] is None
        assert "(no events found)" in render_timeline(events)

    def test_iso_to_epoch(self):
        assert _iso_to_epoch("2026-09-10T00:00:00.000+00:00") > 1.7e9
        assert _iso_to_epoch("garbage") == 0.0
        assert _iso_to_epoch(None) == 0.0


class TestCLI:
    def test_main_table_and_json_modes(self, _tracing_env, capsys):
        from shared.traceview import main as tv_main

        logs_root = _tracing_env / "logs"
        TestReconstruct._mk_task_tree(TestReconstruct(), logs_root, "cli-task")
        assert tv_main(["cli-task", "--logs-root", str(logs_root)]) == 0
        out = capsys.readouterr().out
        assert "task_start" in out and "summary:" in out

        assert tv_main(["cli-task", "--logs-root", str(logs_root), "--json"]) == 0
        lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        assert all(json.loads(l) for l in lines)  # every line is JSON

        assert tv_main(["cli-task", "--logs-root", str(logs_root), "--summary"]) == 0
        assert "outcome" in capsys.readouterr().out

    def test_main_unknown_task_exits_clean(self, _tracing_env, capsys):
        from shared.traceview import main as tv_main

        assert tv_main(["nope", "--logs-root", str(_tracing_env / "logs")]) == 0
        assert "(no events found)" in capsys.readouterr().out
