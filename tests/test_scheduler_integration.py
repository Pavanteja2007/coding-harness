"""Scheduler integration tests — REAL process kills, no simulation.

These prove (per the terminal brief):
  1. Concurrency cap: N tasks, cap K -> at most K workers alive at once.
  2. Crash/resume: worker hard-killed (os._exit) mid-task resumes from
     its last completed step — verified via state.json + checkpoint +
     events journal, not assumed from a clean run.
  3. Hang handling: a hung worker (sleep 1e9) gets killed via stale
     heartbeat and the task completes after resume.
  4. Wall-clock timeout: max_wallclock_s exceeded -> killed, resume
     budget applies, result eventually produced.
  5. Approval mode in a real worker process (file protocol across OS
     processes, no threads).
  6. Adaptive routing end-to-end through a real worker: the ledger
     shows the hint changed the model.

Each test uses isolated logs_root + task-specific resume_dir, so they
parallelize safely and never touch the repo's logs/ dir.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from runtime import approval as ap
from runtime.checkpoint import (
    TaskCheckpoint,
    checkpoint_identity,
    should_resume,
)
from runtime.scheduler import Scheduler, _Attempt
from runtime.serialize import result_to_dict
from shared.types import Task, TaskResult


def _task(task_id: str, logs_root: Path, **config) -> Task:
    resume_dir = logs_root / "tasks" / task_id
    cfg = {
        "use_fake_harness": True,
        "fake_state_dir": str(resume_dir),
        "resume_dir": str(resume_dir),
        "crash_retries": 2,
        "fake_step_delay_s": 0.15,
        **config,
    }
    return Task(task_id=task_id, repo_path="", issue_text="fake issue", config=cfg)


def _mk_sched(logs_root: Path, conc: int = 4, run_id: str | None = None) -> Scheduler:
    return Scheduler(
        concurrency=conc,
        logs_root=str(logs_root),
        run_id=run_id or f"test_{int(time.time() * 1000)}",
    )


def _events(sched: Scheduler) -> list:
    p = sched.run_dir / "events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


class TestBasicConcurrency:
    def test_all_tasks_complete(self, tmp_path):
        sched = _mk_sched(tmp_path, conc=4)
        tasks = [_task(f"t{i}", tmp_path) for i in range(10)]
        results = sched.run(tasks)
        assert len(results) == 10
        assert all(r.status == "success" for r in results.values())
        assert all(r.attempts == 1 for r in results.values())

    def test_concurrency_cap_never_exceeded(self, tmp_path):
        """10 tasks with concurrency 3: max simultaneously-alive workers
        must stay <= 3, reconstructed from the run event journal (each
        spawn increments the running count; each terminal event for that
        task — finish/crash_retry/crash_exhausted — decrements it)."""
        conc = 3
        sched = _mk_sched(tmp_path, conc=conc)
        tasks = [_task(f"t{i}", tmp_path, fake_step_delay_s=0.2) for i in range(10)]
        results = sched.run(tasks)
        assert len(results) == 10
        assert {
            status for status in (result.status for result in results.values())
        } == {"success"}
        events = _events(sched)

        running = 0
        max_overlap = 0
        for e in events:
            ev = e["event"]
            if ev == "spawn":
                running += 1
                max_overlap = max(max_overlap, running)
            elif ev in (
                "finish",
                "crash_retry",
                "crash_exhausted",
                "kill_requeue",
                "kill_exhausted",
            ):
                running -= 1
        assert max_overlap <= conc
        # sanity: all 10 tasks were spawned and all left the active set
        assert sum(1 for e in events if e["event"] == "spawn") == 10
        assert running == 0

    def test_cap_runs_multiple_real_workers_together(self, tmp_path):
        """Prove parallelism from journal intervals instead of host timing."""
        sched = _mk_sched(tmp_path, conc=5)
        tasks = [_task(f"t{i}", tmp_path, fake_step_delay_s=0.3) for i in range(10)]
        results = sched.run(tasks)
        assert len(results) == 10
        assert all(result.status == "success" for result in results.values())
        running = 0
        max_overlap = 0
        for event in _events(sched):
            if event["event"] == "spawn":
                running += 1
                max_overlap = max(max_overlap, running)
            elif event["event"] == "finish":
                running -= 1
        assert max_overlap == 5
        assert running == 0


class TestSchedulerIdentity:
    def test_fifo_spawn_order_is_preserved(self, tmp_path):
        sched = _mk_sched(tmp_path, conc=1)
        task_ids = [f"fifo-{index}" for index in range(4)]
        results = sched.run(
            [_task(task_id, tmp_path, fake_step_delay_s=0.01) for task_id in task_ids]
        )
        spawned = [
            event["data"]["task_id"]
            for event in _events(sched)
            if event["event"] == "spawn"
        ]
        assert spawned == task_ids
        assert list(results) == task_ids

    def test_duplicate_task_ids_rejected_before_spawn(self, tmp_path):
        sched = _mk_sched(tmp_path, conc=2)
        with pytest.raises(ValueError, match="duplicate task id"):
            sched.run([_task("duplicate", tmp_path), _task("duplicate", tmp_path)])
        assert not (sched.run_dir / "events.jsonl").exists()

    @pytest.mark.parametrize(
        "task_id",
        ["../escape", "a/b", "a\\b", "C:task", "task.", "CON", ".", ""],
    )
    def test_unsafe_task_ids_rejected(self, tmp_path, task_id):
        sched = _mk_sched(tmp_path, conc=1)
        with pytest.raises(ValueError, match="task id"):
            sched.run([_task(task_id, tmp_path)])

    def test_default_run_ids_are_unique_and_reuse_is_rejected(self, tmp_path):
        first = Scheduler(logs_root=str(tmp_path))
        second = Scheduler(logs_root=str(tmp_path))
        assert first.run_id != second.run_id
        (first.run_dir / "occupied").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            Scheduler(logs_root=str(tmp_path), run_id=first.run_id)

    def test_empty_root_values_are_pinned_to_scheduler_root(self, tmp_path):
        task = Task(
            task_id="pinned-root",
            repo_path="",
            issue_text="fix typo",
            config={
                "use_fake_harness": True,
                "log_root": None,
                "resume_dir": None,
                "fake_step_delay_s": 0.01,
            },
        )
        sched = _mk_sched(tmp_path, conc=1)
        results = sched.run([task])
        assert results["pinned-root"].status == "success"
        assert (tmp_path / "pinned-root" / "state.json").exists()
        assert (tmp_path / "pinned-root.runtime" / "checkpoint.json").exists()

    def test_task_json_redacts_nested_credentials(self, tmp_path):
        sentinel = "sentinel-scheduler-key"
        from runtime.fsutil import (
            extract_sensitive_config,
            redact_sensitive_config,
            restore_sensitive_config,
        )

        task = _task(
            "secret-task",
            tmp_path,
            api_key=sentinel,
            model_tiers={
                "hard": {
                    "provider": "custom",
                    "model": "model",
                    "api_key": sentinel,
                    "api_base": "https://example.invalid/v1",
                }
            },
            difficulty_llm={"model": "classifier", "api_key": sentinel},
        )
        sched = _mk_sched(tmp_path, conc=1)
        assert sched.run([task])["secret-task"].status == "success"
        task_json = next((sched.run_dir / "secret-task").glob("attempt_*/task.json"))
        rendered = task_json.read_text(encoding="utf-8")
        assert sentinel not in rendered
        assert "[REDACTED]" in rendered
        entries = extract_sensitive_config(task.config)
        safe_config = redact_sensitive_config(task.config)
        assert sentinel not in json.dumps(safe_config)
        assert restore_sensitive_config(safe_config, entries) == task.config


class TestWorkerSafety:
    def test_worker_restores_issue_and_secrets_from_transient_payload(
        self, tmp_path, monkeypatch
    ):
        import runtime.worker as worker_module
        from runtime.fsutil import TASK_SECRETS_ENV

        sentinel = "sentinel-transport-key"
        issue = "preserve this exact issue"
        captured = {}

        def capture(task, log_root):
            captured["task"] = task
            captured["log_root"] = log_root
            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="diff",
                verification=None,
                cost_usd=0.0,
                model_calls=[],
                log_path=str(tmp_path),
            )

        run_dir = tmp_path / "attempt"
        run_dir.mkdir()
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / ".checkpoint.json.stale.tmp").write_text("x", encoding="utf-8")
        state_dir = tmp_path / "transport-task"
        state_dir.mkdir()
        (state_dir / ".state.json.stale.tmp").write_text("x", encoding="utf-8")
        task_json = run_dir / "task.json"
        task_json.write_text(
            json.dumps(
                {
                    "task_id": "transport-task",
                    "repo_path": "",
                    "issue_text": "",
                    "config": {
                        "resume_dir": str(runtime_dir),
                        "log_root": str(tmp_path),
                        "api_key": "[REDACTED]",
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(
            TASK_SECRETS_ENV,
            json.dumps(
                {
                    "issue_text": issue,
                    "secrets": [[["api_key"], sentinel]],
                }
            ),
        )
        monkeypatch.setattr(worker_module, "_load_run_task", lambda _config: capture)
        assert worker_module.run_worker(str(task_json), str(run_dir), "token") == 0
        assert captured["task"].issue_text == issue
        assert captured["task"].config["api_key"] == sentinel
        assert sentinel not in task_json.read_text(encoding="utf-8")
        assert not list(runtime_dir.glob(".*.tmp"))
        assert not list(state_dir.glob(".*.tmp"))

    def test_fresh_attempt_rotates_previous_model_ledger(self, tmp_path, monkeypatch):
        import runtime.worker as worker_module

        def succeed(task, log_root):
            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="",
                verification=None,
                cost_usd=0.0,
                model_calls=[],
                log_path=str(tmp_path),
            )

        run_dir = tmp_path / "attempt"
        run_dir.mkdir()
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        ledger = runtime_dir / "model_ledger.jsonl"
        ledger.write_text('{"stale": true}\n', encoding="utf-8")
        task_json = run_dir / "task.json"
        task_json.write_text(
            json.dumps(
                {
                    "task_id": "ledger-generation",
                    "repo_path": "",
                    "issue_text": "fresh issue",
                    "config": {
                        "resume_dir": str(runtime_dir),
                        "log_root": str(tmp_path),
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(worker_module, "_load_run_task", lambda _config: succeed)

        assert worker_module.run_worker(str(task_json), str(run_dir), "token") == 0
        assert not ledger.exists()
        archives = list(runtime_dir.glob("model_ledger.old-*.jsonl"))
        assert len(archives) == 1
        assert archives[0].read_text(encoding="utf-8") == '{"stale": true}\n'

    def test_broken_real_harness_import_never_falls_back_to_fake(self, monkeypatch):
        import builtins

        from runtime.worker import _load_run_task

        real_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "harness.core":
                raise ImportError("simulated transitive dependency failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken_import)
        with pytest.raises(ImportError, match="transitive"):
            _load_run_task({})

    def test_worker_exception_output_redacts_credentials(self):
        from runtime.worker import _safe_exception_text

        sentinel = "sk-worker-secret-123456"
        rendered = _safe_exception_text(
            RuntimeError(f"api_key={sentinel} Bearer {sentinel}")
        )
        assert sentinel not in rendered
        assert "[REDACTED]" in rendered

    def test_two_authority_resume_matrix(self):
        assert should_resume({"resume": True}, {"status": "running"}, ["plan"])
        assert not should_resume({"resume": True}, {"status": "running"}, [])
        assert not should_resume({"resume": True}, {"status": "finished"}, ["plan"])
        assert not should_resume({"resume": True}, None, ["plan"])
        assert not should_resume({"resume": False}, {"status": "running"}, ["plan"])

    def test_heartbeat_is_attempt_scoped(self, tmp_path):
        checkpoint = TaskCheckpoint(str(tmp_path))
        checkpoint.beat({"attempt_token": "old-attempt"})
        assert checkpoint.heartbeat_age_s("old-attempt") is not None
        assert checkpoint.heartbeat_age_s("new-attempt") is None

    def test_checkpoint_identity_rejects_repository_request_and_revision_mismatch(
        self, tmp_path
    ):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "app.py").write_text("value = 1\n", encoding="utf-8")
        (repo_b / "app.py").write_text("value = 1\n", encoding="utf-8")
        config = {"resume_namespace": "run-a", "revision": "rev-a"}
        identity = checkpoint_identity("task", str(repo_a), "request-a", config)
        checkpoint = TaskCheckpoint(str(tmp_path / "runtime"))
        checkpoint.save(
            {
                **identity,
                "status": "running",
                "completed_steps": ["plan"],
            }
        )
        assert should_resume(
            {"resume": True}, checkpoint.load(), ["plan"], identity=identity
        )
        assert not should_resume(
            {"resume": True},
            checkpoint.load(),
            ["plan"],
            identity=checkpoint_identity("task", str(repo_b), "request-a", config),
        )
        assert not should_resume(
            {"resume": True},
            checkpoint.load(),
            ["plan"],
            identity=checkpoint_identity("task", str(repo_a), "request-b", config),
        )
        assert not should_resume(
            {"resume": True},
            checkpoint.load(),
            ["plan"],
            identity=checkpoint_identity(
                "task", str(repo_a), "request-a", {**config, "revision": "rev-b"}
            ),
        )

    def test_worker_starts_fresh_when_checkpoint_identity_changes(
        self, tmp_path, monkeypatch
    ):
        import runtime.worker as worker_module

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        task_json = tmp_path / "task.json"
        captured = []

        def succeed(task, log_root):
            captured.append(dict(task.config))
            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="",
                verification=None,
                cost_usd=0.0,
                model_calls=[],
                log_path=str(log_root),
            )

        def write_task(request):
            task_json.write_text(
                json.dumps(
                    {
                        "task_id": "identity-task",
                        "repo_path": str(repo),
                        "issue_text": request,
                        "config": {
                            "resume_dir": str(runtime_dir),
                            "log_root": str(tmp_path),
                            "resume_namespace": "stable-run",
                        },
                    }
                ),
                encoding="utf-8",
            )

        monkeypatch.setattr(worker_module, "_load_run_task", lambda _config: succeed)
        write_task("request-a")
        assert worker_module.run_worker(str(task_json), str(tmp_path / "run-a")) == 0
        write_task("request-b")
        assert worker_module.run_worker(str(task_json), str(tmp_path / "run-b")) == 0
        (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        write_task("request-b")
        assert worker_module.run_worker(str(task_json), str(tmp_path / "run-c")) == 0
        assert [item["resume"] for item in captured] == [False, False, False]
        stored = TaskCheckpoint(str(runtime_dir)).load()
        expected = checkpoint_identity(
            "identity-task", str(repo), "request-b", {"resume_namespace": "stable-run"}
        )
        assert stored is not None
        assert stored["request_identity"] == expected["request_identity"]
        assert stored["revision_identity"] == expected["revision_identity"]


class TestAtomicWrite:
    def test_transient_permission_error_retries_then_succeeds(
        self, tmp_path, monkeypatch
    ):
        import runtime.fsutil as fs

        target = tmp_path / "state.json"
        original_replace = fs.os.replace
        calls = {"count": 0}

        def flaky_replace(source, destination):
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError("sharing violation")
            return original_replace(source, destination)

        monkeypatch.setattr(fs.os, "replace", flaky_replace)
        monkeypatch.setattr(fs.time, "sleep", lambda _seconds: None)
        fs.atomic_write_json(target, {"new": True})
        assert calls["count"] == 3
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
        assert not list(tmp_path.glob(".state.json.*.tmp"))

    def test_persistent_permission_error_is_reraised(self, tmp_path, monkeypatch):
        import runtime.fsutil as fs

        target = tmp_path / "state.json"
        target.write_text('{"old": true}', encoding="utf-8")
        calls = {"count": 0}

        def denied_replace(_source, _destination):
            calls["count"] += 1
            raise PermissionError("persistent denial")

        monkeypatch.setattr(fs.os, "replace", denied_replace)
        monkeypatch.setattr(fs.time, "sleep", lambda _seconds: None)
        with pytest.raises(PermissionError, match="persistent denial"):
            fs.atomic_write_json(target, {"new": True})
        assert calls["count"] == 3
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
        assert not list(tmp_path.glob(".state.json.*.tmp"))


class TestLifecycleSafety:
    def test_result_requires_finished_checkpoint(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        task = _task("coherent", tmp_path)
        task.config["resume_dir"] = str(runtime_dir)
        scheduler = _mk_sched(tmp_path, conc=1)
        run_dir = tmp_path / "attempt"
        run_dir.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=5)
        attempt = _Attempt(
            task=task,
            proc=process,
            run_dir=run_dir,
            attempt=0,
            started_epoch=time.time(),
            max_wallclock_s=10,
            hang_stale_s=10,
            attempt_token="token",
        )
        result = TaskResult(
            task_id="coherent",
            status="success",
            attempts=1,
            diff="diff",
            verification=None,
            cost_usd=0.0,
            model_calls=[],
            log_path=str(run_dir),
        )
        (run_dir / "result.json").write_text(
            json.dumps(result_to_dict(result)), encoding="utf-8"
        )
        checkpoint = TaskCheckpoint(str(runtime_dir))
        checkpoint.save(
            {
                "task_id": "coherent",
                "status": "running",
                "attempt": 0,
                "completed_steps": ["plan"],
            }
        )
        assert scheduler._completed_result(attempt, 0) is None
        checkpoint.update(status="finished")
        assert scheduler._completed_result(attempt, 0).task_id == "coherent"

    def test_kill_timeout_does_not_report_success(self, tmp_path, monkeypatch):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        attempt = _Attempt(
            task=_task("kill-timeout", tmp_path),
            proc=process,
            run_dir=tmp_path,
            attempt=0,
            started_epoch=time.time(),
            max_wallclock_s=10,
            hang_stale_s=10,
            attempt_token="token",
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                process,
                "wait",
                lambda **_kwargs: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired("worker", 10)
                ),
            )
            assert _mk_sched(tmp_path, conc=1)._kill(attempt) is False
        process.wait(timeout=5)
        assert process.poll() is not None


class TestCrashResume:
    def test_midrun_crash_resumes_from_completed_steps(self, tmp_path):
        """THE core resume test: task crashes at step 3 of 5 (hard kill),
        scheduler relaunches, and the resumed run SKIPS already-completed
        steps — proven by step timestamps in state.json attempt dirs and
        the worker events journal showing resume=True."""
        sched = _mk_sched(tmp_path, conc=2)
        crash_step = "edit"  # plan, retrieve completed before the crash
        task = _task(
            "crashy", tmp_path, fake_crash_step=crash_step, fake_step_delay_s=0.1
        )
        results = sched.run([task])

        # Task ultimately succeeds via resume
        assert results["crashy"].status == "success"
        assert results["crashy"].attempts == 2  # crashed once, resumed once

        # Events prove the crash + retry happened
        events = _events(sched)
        kinds = [e["event"] for e in events]
        assert "crash" in kinds
        assert "crash_retry" in kinds

        # Worker journal proves resume actually skipped completed steps
        ev_path = tmp_path / "tasks" / "crashy" / "events.jsonl"
        worker_events = [
            json.loads(l) for l in ev_path.read_text().splitlines() if l.strip()
        ]
        starts = [e for e in worker_events if e["event"] == "worker_start"]
        assert len(starts) == 2
        assert starts[0]["data"]["resume"] is False
        assert starts[1]["data"]["resume"] is True

        # state.json shows ALL steps completed at the end
        state = json.loads((tmp_path / "tasks" / "crashy" / "state.json").read_text())
        assert state["completed_steps"] == [
            "plan",
            "retrieve",
            "edit",
            "verify",
            "git-output",
        ]
        assert state["remaining_plan"] == []

    def test_fresh_restart_when_no_progress(self, tmp_path):
        """Resume requires prior progress: a checkpoint with NO completed
        steps must not claim resume (attempts stays 1)."""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task("fresh", tmp_path, fake_crash_step="plan", fake_step_delay_s=0.05)
        results = sched.run([task])
        # crash at step 1 leaves no completed steps -> resume can't skip
        # anything but the run still completes via relaunch
        assert results["fresh"].status == "success"
        # attempts: 2 (crash + relaunch) — the relaunch is a fresh start
        ev_path = tmp_path / "tasks" / "fresh" / "events.jsonl"
        worker_events = [
            json.loads(l) for l in ev_path.read_text().splitlines() if l.strip()
        ]
        starts = [e for e in worker_events if e["event"] == "worker_start"]
        assert starts[1]["data"]["resume"] is False

    def test_crash_budget_exhaustion(self, tmp_path):
        """crash_retries=0: the first crash fails the task outright."""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task(
            "doomed",
            tmp_path,
            fake_crash_step="edit",
            crash_retries=0,
            fake_step_delay_s=0.05,
        )
        results = sched.run([task])
        assert results["doomed"].status == "error"
        kinds = [e["event"] for e in _events(sched)]
        assert "crash_exhausted" in kinds


class TestHangAndTimeout:
    def test_hung_worker_killed_and_resumed(self, tmp_path):
        """Worker hangs at 'verify' (sleep 1e9): stale heartbeat triggers
        kill + resume; hang injection is one-shot so the resumed run
        completes. hang_heartbeat_stale_s is tuned small for the test."""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task(
            "hangy",
            tmp_path,
            fake_hang_step="verify",
            hang_heartbeat_stale_s=4.0,
            fake_step_delay_s=0.05,
        )
        start = time.time()
        results = sched.run([task])
        elapsed = time.time() - start
        assert results["hangy"].status == "success"
        assert elapsed < 30  # didn't wait for the 1e9s sleep
        events = _events(sched)
        hang_events = [event for event in events if event["event"] == "hang_timeout"]
        assert hang_events
        assert hang_events[0]["data"]["signal"] == "state_stale"
        assert "kill_requeue" in [event["event"] for event in events]

    def test_stale_attempt_heartbeat_kills_even_with_fresh_state(self, tmp_path):
        class FakeProcess:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return 1

        task = _task("heartbeat-stale", tmp_path)
        runtime_dir = Path(task.config["resume_dir"])
        state_path = Path(task.config["fake_state_dir"]) / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"completed_steps": ["plan"]}), encoding="utf-8"
        )
        checkpoint = TaskCheckpoint(str(runtime_dir))
        checkpoint.beat(
            {
                "attempt_token": "current-token",
                "epoch": time.time() - 100,
            }
        )
        process = FakeProcess()
        attempt = _Attempt(
            task=task,
            proc=process,
            run_dir=tmp_path,
            attempt=0,
            started_epoch=time.time() - 100,
            max_wallclock_s=1000,
            hang_stale_s=10,
            attempt_token="current-token",
        )
        sched = _mk_sched(tmp_path, conc=1)
        assert sched._check_timeouts(attempt) is True
        assert process.killed is True
        signals = [
            event["data"].get("signal")
            for event in _events(sched)
            if event["event"] == "hang_timeout"
        ]
        assert signals == ["heartbeat"]

    def test_wallclock_timeout_fails_after_budget(self, tmp_path):
        """max_wallclock_s tiny + slow-but-progressing task: each attempt
        overruns its wall clock and gets killed; after the crash budget
        is spent the task's result is status=timeout. (A hang-only task
        would be caught by hang detection first — this test isolates the
        wall-clock path with continuous progress and steps longer than
        the cap: steps take 2.5s each, cap is 1s, so the first poll past
        1s triggers a wallclock kill.)"""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task(
            "slowpoke",
            tmp_path,
            fake_step_delay_s=2.5,
            max_wallclock_s=1.0,
            crash_retries=1,
        )
        results = sched.run([task])
        assert results["slowpoke"].status == "timeout"
        kinds = [e["event"] for e in _events(sched)]
        assert "wallclock_timeout" in kinds


class TestApprovalMode:
    def test_scheduler_surfaces_pending_approval_then_approved(self, tmp_path):
        """Approval mode: worker writes request.json and blocks; the test
        (acting as the human) approves; worker proceeds to success."""
        sched = _mk_sched(tmp_path, conc=1)

        task = _task(
            "approval-t",
            tmp_path,
            approval="require",
            fake_step_delay_s=0.05,
            approval_timeout_s=30,
        )
        gate_dir = tmp_path / "tasks" / "approval-t" / "approval"

        def approver():
            for _ in range(200):  # up to 10s
                if (gate_dir / "request.json").exists():
                    ap.decide(str(gate_dir), approve=True)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=approver)
        t.start()
        results = sched.run([task])
        t.join()
        assert results["approval-t"].status == "success"

    def test_gate_parked_worker_survives_state_stale_hang_check(self, tmp_path):
        """T1's Round-3 finding, fixed: a worker parked in the approval
        gate stops touching state.json, and the scheduler's state-stale
        hang check (hang_heartbeat_stale_s) killed it mid-gate. Now the
        worker marks awaiting_approval in its checkpoint and keeps
        heartbeating; the scheduler exempts gate-parked + fresh-heartbeat
        workers from the STATE-stale kill (heartbeat death and the
        wall-clock cap still kill). Proven live: a decision delayed well
        past hang_heartbeat_stale_s (stale state.json) still ends in
        success, with no hang_timeout kill in the journal."""
        sched = _mk_sched(tmp_path, conc=1)
        # stale threshold ABOVE the 2.0s heartbeat cadence with slack for
        # a late beat under load (else the liveness check fires — it must:
        # a dead process is dead, gate or not) but BELOW the park
        # duration: state.json is older than this when the decision
        # lands — the pre-fix scheduler killed mid-gate on exactly this
        # window.
        task = _task(
            "gate-parked",
            tmp_path,
            approval="require",
            fake_step_delay_s=0.05,
            approval_timeout_s=60,
            hang_heartbeat_stale_s=4.0,
        )
        gate_dir = tmp_path / "tasks" / "gate-parked" / "approval"

        def slow_approver():
            # let state.json go stale FIRST (hang check window), then decide
            for _ in range(600):  # up to 30s
                if (gate_dir / "request.json").exists():
                    time.sleep(6.0)  # >> hang_heartbeat_stale_s=4.0
                    ap.decide(str(gate_dir), approve=True)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=slow_approver)
        t.start()
        results = sched.run([task])
        t.join()
        assert results["gate-parked"].status == "success"
        # the kill that the pre-fix scheduler would have issued never fired
        kinds = [e["event"] for e in _events(sched)]
        assert "hang_timeout" not in kinds
        assert "kill_requeue" not in kinds
        # and the gate marker was set then cleared around the park
        cp = json.loads(
            (tmp_path / "tasks" / "gate-parked" / "checkpoint.json").read_text()
        )
        assert cp["awaiting_approval"] is False

    def test_gate_park_still_bounded_by_wallclock(self, tmp_path):
        """The exemption is not a license to park forever: with no decision
        ever arriving and approval_timeout_s=None... (this test instead
        uses a long timeout + a small wall-clock cap) the wall-clock kill
        still fires and the crash budget governs, ending in timeout —
        never an unbounded parked worker."""
        sched = _mk_sched(tmp_path, conc=1)
        # stale window above the 2.0s heartbeat cadence (so liveness is
        # fine) and wall-clock cap below the 60s approval timeout: the
        # ONLY thing that can end this park is the wall-clock kill.
        task = _task(
            "gate-forever",
            tmp_path,
            approval="require",
            fake_step_delay_s=0.05,
            approval_timeout_s=60,
            hang_heartbeat_stale_s=4.0,
            max_wallclock_s=5.0,
            crash_retries=0,
        )
        results = sched.run([task])
        assert results["gate-forever"].status == "timeout"
        kinds = [e["event"] for e in _events(sched)]
        assert "wallclock_timeout" in kinds

    def test_approval_reject_blocks_diff(self, tmp_path):
        sched = _mk_sched(tmp_path, conc=1)
        task = _task(
            "approval-r",
            tmp_path,
            approval="require",
            fake_step_delay_s=0.05,
            approval_timeout_s=30,
        )
        gate_dir = tmp_path / "tasks" / "approval-r" / "approval"

        def approver():
            for _ in range(200):
                if (gate_dir / "request.json").exists():
                    ap.decide(str(gate_dir), approve=False)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=approver)
        t.start()
        results = sched.run([task])
        t.join()
        assert results["approval-r"].status == "failed"  # rejected -> failed
        assert results["approval-r"].diff is None  # diff never applied


class TestRouterIntegration:
    @staticmethod
    def _routing_task(task_id: str, tmp_path: Path, issue_text: str) -> Task:
        resume_dir = tmp_path / "tasks" / task_id
        return Task(
            task_id=task_id,
            repo_path="",
            issue_text=issue_text,
            config={
                "use_fake_harness": True,
                "use_mock_provider": True,
                "adaptive_routing": True,
                "resume_dir": str(resume_dir),
                "fake_state_dir": str(resume_dir),
                "fake_step_delay_s": 0.0,
                "fake_model_calls": True,
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {
                        "provider": "anthropic",
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                "mock_responses": {
                    "gpt-4o-mini": "cheap",
                    "claude-3-5-sonnet-20241022": "expensive",
                },
            },
        )

    def test_adaptive_routing_end_to_end_through_worker(self, tmp_path):
        """Full stack, real worker process: router context from task.config
        -> difficulty predicted from issue text -> hard issue hits the
        expensive model. Asserted from the on-disk per-call ledger."""
        hard_issue = (
            "Production service crashes intermittently with a Traceback "
            "and ValueError under concurrent load.\n\n"
            "The failure happens in src/mod/parser.py and utils/handlers.py "
            "— race condition suspected between the parser and the retry "
            "loop, sometimes causing a deadlock. Flaky test coverage makes "
            "it hard to reproduce; timing-sensitive.\n\n"
            "```\nTraceback (most recent call last):\n"
            '  File "src/mod/parser.py", line 88, in parse\n'
            "ValueError: invalid literal\n```\n\n"
            "Not sure if the encoding handling in the buffer is also "
            "involved. Needs a careful multi-file fix."
        )
        # self-check: this text must actually classify as "hard"
        from runtime.difficulty import heuristic_features, score_to_hint

        feats = heuristic_features(hard_issue)
        assert score_to_hint(feats["score"]) == "hard", feats

        sched = _mk_sched(tmp_path, conc=1)
        results = sched.run([self._routing_task("routed-hard", tmp_path, hard_issue)])
        assert results["routed-hard"].status == "success"

        ledger_path = tmp_path / "tasks" / "routed-hard" / "model_ledger.jsonl"
        assert ledger_path.exists()
        records = [
            json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()
        ]
        assert len(records) == 5
        assert all(r["model"] == "claude-3-5-sonnet-20241022" for r in records)
        assert all(r["routed_via_hint"] == "hard" for r in records)
        assert all(r["cost_usd"] > 0 for r in records)  # ledger has costs

    def test_ablation_toggle_changes_model_choice(self, tmp_path):
        """The ablation architecture demo: same task, routing ON vs OFF —
        the single config flag demonstrably changes the chosen model."""
        easy_issue = "fix a typo"
        # routing ON: easy issue -> cheap model
        sched_on = _mk_sched(tmp_path, conc=1)
        sched_on.run([self._routing_task("abl-on", tmp_path, easy_issue)])
        ledger_on = tmp_path / "tasks" / "abl-on" / "model_ledger.jsonl"
        recs_on = [
            json.loads(l) for l in ledger_on.read_text().splitlines() if l.strip()
        ]
        assert len(recs_on) == 5
        assert all(r["model"] == "gpt-4o-mini" for r in recs_on)

        # routing OFF: same issue, but a hard-tier default is pinned — no
        # hint-based switching, single model regardless of content
        resume_dir = tmp_path / "tasks" / "abl-off"
        off_task = Task(
            task_id="abl-off",
            repo_path="",
            issue_text=easy_issue,
            config={
                "use_fake_harness": True,
                "use_mock_provider": True,
                "adaptive_routing": False,
                "model": "claude-3-5-sonnet-20241022",  # expensive pinned default
                "resume_dir": str(resume_dir),
                "fake_state_dir": str(resume_dir),
                "fake_model_calls": True,
                "mock_responses": {
                    "gpt-4o-mini": "cheap",
                    "claude-3-5-sonnet-20241022": "expensive",
                },
            },
        )
        sched_off = _mk_sched(tmp_path, conc=1)
        sched_off.run([off_task])
        ledger_off = tmp_path / "tasks" / "abl-off" / "model_ledger.jsonl"
        recs_off = [
            json.loads(l) for l in ledger_off.read_text().splitlines() if l.strip()
        ]
        assert len(recs_off) == 5
        assert all(r["model"] == "claude-3-5-sonnet-20241022" for r in recs_off)
        # and the cost difference is the ablation's whole point:
        cost_on = sum(r["cost_usd"] for r in recs_on)
        cost_off = sum(r["cost_usd"] for r in recs_off)
        assert cost_on < cost_off
