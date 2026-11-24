"""P5/W1 T3 — supervision pins: survival, no double execution, bounded, reapable.

Every vector here runs REAL processes. There is no simulated supervisor, no
mocked ``poll()``, and no assertion that can be satisfied by a code path that
would not run in production:

* the kill -9 vectors use ``TerminateProcess`` (Windows) / ``SIGKILL`` (POSIX)
  on ONE pid, deliberately NOT ``taskkill /T`` — a tree kill would take the
  supervisor with the parent and prove nothing. The helper's docstring records
  why, because the first version of this file got it wrong and "killed the
  parent" was really "killed the tree".
* the worker is a real ``python -m runtime.worker`` running the fake harness,
  so resume is ``runtime.checkpoint.should_resume`` deciding, not this file
  asserting its own fiction.
* every "it survived" claim is read back from the supervision RECORD on disk,
  which is what T4 renders.

Host-only: no Docker, no network, no credential, no provider.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from runtime import supervisor
from runtime.fsutil import atomic_write_json, is_pid_alive, read_json_or_none
from shared.types import Task

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def hard_kill(pid: int) -> None:
    """``kill -9`` ONE process, cross-platform, without the tree.

    On POSIX this is ``SIGKILL``. On Windows ``signal.SIGKILL`` does not exist,
    so the usual ``taskkill /T /F`` shortcut would kill the process TREE — which
    includes a detached supervisor and would make every "the run survived"
    assertion pass for the wrong reason (the supervisor was killed too, by the
    evidence collector). ``TerminateProcess`` on a single pid is the faithful
    analogue: it kills exactly that process and leaves its children running,
    which is the property under test.
    """
    if os.name == "nt":  # pragma: no cover - platform-specific
        handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x0400, False, int(pid))
        assert handle, f"could not open pid {pid} for termination"
        ctypes.windll.kernel32.TerminateProcess(handle, 137)
        ctypes.windll.kernel32.CloseHandle(handle)
        return
    import signal

    os.kill(int(pid), signal.SIGKILL)


def _dead_pid() -> int:
    """A pid that is POSITIVE and provably not running.

    Positive matters: pid 0 is not a process, and the lock reader treats a
    non-positive pid as UNDECIDABLE (fail-closed), so a fixture using 0 would be
    testing the undecidable path while claiming to test the dead path.
    """
    candidate = 999_999
    while candidate > 1000:
        if not is_pid_alive(candidate):
            return candidate
        candidate -= 1
    raise AssertionError("no free pid found")  # pragma: no cover


def _fake_task(task_id: str, logs_root: Path, **config) -> Task:
    """A task the fake harness can run to completion, offline."""
    resume_dir = logs_root / "tasks" / task_id
    cfg = {
        "use_fake_harness": True,
        "use_mock_provider": True,
        "fake_state_dir": str(resume_dir),
        "resume_dir": str(resume_dir),
        "fake_step_delay_s": 0.15,
        **config,
    }
    return Task(
        task_id=task_id,
        repo_path="",
        issue_text="fix the failing test",
        config=cfg,
    )


def _wait_for(predicate, timeout_s: float = 90.0, interval_s: float = 0.2):
    """Poll ``predicate`` until it returns truthy or the budget runs out."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    return last


def _terminal_record(task_id: str, logs_root: Path):
    def probe():
        record = supervisor.read_supervision(task_id, logs_root=logs_root)
        return record if record and record.get("terminal") else None

    return _wait_for(probe, timeout_s=120.0)


def _runtime_dir(task_id: str, logs_root: Path) -> Path:
    """The run's OWN runtime dir, read from its supervision record.

    Resolving it through the record rather than re-deriving it from the config is
    deliberate: the first version of this file guessed ``logs/{task_id}.runtime``
    and read events from a directory that never had any, which would have made
    every event assertion vacuously pass or fail for the wrong reason.
    """
    record = supervisor.read_supervision(task_id, logs_root=logs_root)
    if record and record.get("runtime_dir"):
        return Path(record["runtime_dir"])
    return supervisor.supervision_paths(task_id, logs_root=logs_root).runtime_dir


def _events(logs_root: Path, task_id: str) -> list:
    path = _runtime_dir(task_id, logs_root) / supervisor._EVENTS_NAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _worker_events(logs_root: Path, task_id: str) -> list:
    """The WORKER's own journal (the authority on resume)."""
    path = _runtime_dir(task_id, logs_root) / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_result(attempt_dir: Path, payload: dict) -> None:
    """Stand in for a worker that wrote a verdict, for the classifier's
    fidelity (which verdict wins), not for the worker itself."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(attempt_dir / "result.json", payload)


# ---------------------------------------------------------------------------
# 1. survival
# ---------------------------------------------------------------------------


class TestASurvivesKillNineOfItsParent:
    def test_a_run_survives_the_hard_kill_of_the_process_that_started_it(
        self, tmp_path
    ):
        """THE acceptance vector: kill -9 the caller; the run still finishes.

        A caller process detaches the run and then idles. We hard-kill the
        CALLER (one pid, no tree). The supervisor and its worker must still be
        alive, and the run must reach its own terminal result — read from the
        record on disk, not from any in-process state.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        caller_script = tmp_path / "caller.py"
        caller_script.write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "from shared.types import Task\n"
            "from runtime import supervisor\n"
            "logs = Path(sys.argv[1]); task_id = sys.argv[2]\n"
            "cfg = {'use_fake_harness': True, 'use_mock_provider': True,\n"
            "       'fake_state_dir': str(logs / 'tasks' / task_id),\n"
            "       'resume_dir': str(logs / 'tasks' / task_id),\n"
            "       'fake_step_delay_s': 0.4}\n"
            "task = Task(task_id=task_id, repo_path='', issue_text='fix it', config=cfg)\n"
            "out = supervisor.detach_task(task, logs_root=logs, detach=True)\n"
            "print(out.supervisor_pid, flush=True)\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        caller = subprocess.Popen(
            [sys.executable, str(caller_script), str(logs), "kill9run"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        sup_pid = None
        try:
            sup_pid = int(caller.stdout.readline().strip())
            assert sup_pid > 0

            def _has_worker():
                found = supervisor.read_supervision("kill9run", logs_root=logs)
                return found if found and found.get("worker_pid") else None

            record = _wait_for(_has_worker)
            assert record is not None, "supervisor never reported a live worker"
            worker_pid = int(record["worker_pid"])

            hard_kill(caller.pid)
            caller.wait(timeout=30)
            assert caller.returncode is not None, "the caller was not killed"

            # The parent is gone. Its children are the point.
            assert is_pid_alive(sup_pid), (
                "the supervisor did not survive its parent's death — detach is"
                " not doing what the module claims"
            )
            assert is_pid_alive(worker_pid), "the worker did not survive either"

            final = _terminal_record("kill9run", logs)
            assert final is not None, "the run never reached a terminal state"
            assert final["state"] == "finished"
            assert final["worker_status"] == "success"
            assert final["restarts"] == 0
        finally:
            if sup_pid and is_pid_alive(sup_pid):
                hard_kill(sup_pid)

    def test_the_detached_launch_uses_real_detach_flags(self):
        """The survival claim rests on these flags; assert them, not the prose."""
        kwargs = supervisor._detached_popen_kwargs()
        if os.name == "nt":  # pragma: no cover - platform-specific
            import subprocess as sp

            flags = int(kwargs["creationflags"])
            assert flags & getattr(sp, "DETACHED_PROCESS", 0)
            assert flags & getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            assert kwargs.get("start_new_session") is True

    def test_the_supervisor_never_preloads_the_provider_library(self):
        """A supervisor that pays the provider import cannot be a supervisor.

        ``import runtime`` starts a background litellm preload for eligible
        processes; the detached supervisor must run with it off, and must
        RESTORE the caller's setting for its worker child so a supervised
        worker behaves exactly like a scheduler-spawned one.
        """
        import inspect

        source = inspect.getsource(supervisor.detach_task)
        assert 'child_env["NEO_PRELOAD_LITELLM"] = "off"' in source
        assert 'child_env.pop("NEO_PRELOAD_LITELLM", None)' in inspect.getsource(
            supervisor._spawn_worker
        )


# ---------------------------------------------------------------------------
# 2. restart-or-resume
# ---------------------------------------------------------------------------


class TestRestartOrResume:
    def test_a_kill_9_worker_is_restarted_and_the_run_resumes(self, tmp_path):
        """Kill the WORKER; the supervisor relaunches and the run RESUMES.

        The resume half is read from the worker's OWN journal (``resume: true``
        on the second ``worker_start``), because "it restarted" and "it resumed"
        are different claims and only the worker can tell which one happened.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("resume-run", logs, fake_step_delay_s=0.4)
        paths = supervisor.supervision_paths("resume-run", logs_root=logs)

        thread_result: dict = {}

        def _run():
            thread_result["outcome"] = supervisor.detach_task(
                task, logs_root=logs, detach=False, max_restarts=3
            )

        thread = threading.Thread(target=_run)
        thread.start()
        try:

            def _progressed():
                record = read_json_or_none(paths.record_path)
                state = read_json_or_none(paths.runtime_dir / "state.json")
                if not (record and state and record.get("worker_pid")):
                    return None
                return record if (state.get("completed_steps") or []) else None

            record = _wait_for(_progressed, timeout_s=90.0)
            assert record is not None, "the run never made progress to kill"
            victim = int(record["worker_pid"])
            hard_kill(victim)

            thread.join(timeout=180)
            assert not thread.is_alive(), (
                "the supervisor did not reach a terminal state"
            )
            outcome = thread_result["outcome"]
            assert outcome.state == "finished"
            assert outcome.worker_status == "success"
            assert outcome.attempts == 2
            assert outcome.restarts == 1

            starts = [
                e["data"]
                for e in _worker_events(logs, "resume-run")
                if e["event"] == "worker_start"
            ]
            assert len(starts) == 2, starts
            assert starts[0]["resume"] is False
            assert starts[1]["resume"] is True, (
                "the relaunched worker restarted from scratch instead of"
                " resuming — the supervisor relaunched the payload but the"
                " checkpoint did not authorise it"
            )
            assert "worker_restarting" in [
                e["event"] for e in _events(logs, "resume-run")
            ]
        finally:
            if thread.is_alive():  # pragma: no cover - only on a failed run
                thread.join(timeout=5)

    def test_a_restart_relaunches_the_same_payload(self, tmp_path):
        """One payload, two attempts: a restart is not a fresh task in disguise."""
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("same-payload", logs, fake_crash_step="s2")
        outcome = supervisor.detach_task(
            task, logs_root=logs, detach=False, max_restarts=2
        )
        paths = _runtime_dir("same-payload", logs)
        assert outcome.state == "finished"
        assert outcome.attempts == 2, "the crash was not restarted"
        first = (paths / "attempts" / "attempt_0" / "task.json").read_text(
            encoding="utf-8"
        )
        second = (paths / "attempts" / "attempt_1" / "task.json").read_text(
            encoding="utf-8"
        )
        assert first == second, "the relaunch used a different task payload"

    def test_a_written_verdict_is_never_overruled_by_the_supervisor(self, tmp_path):
        """A worker that reached a verdict is terminal, whatever the exit code.

        Control arm for the classifier: without this, "never restarts a verdict"
        is satisfiable by never restarting anything.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("verdict-run", logs)
        outcome = supervisor.detach_task(
            task, logs_root=logs, detach=False, max_restarts=2
        )
        paths = _runtime_dir("verdict-run", logs)
        assert outcome.state == "finished"
        result = read_json_or_none(paths / "attempts" / "attempt_0" / "result.json")
        assert result is not None
        assert outcome.worker_status == result["status"]
        assert outcome.attempts == 1, "a finished run was relaunched anyway"


# ---------------------------------------------------------------------------
# 3. single-owner, no double execution
# ---------------------------------------------------------------------------


class TestNoDoubleExecution:
    def test_a_second_supervisor_is_refused_while_the_first_is_alive(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("double", logs_root=logs)
        assert supervisor.acquire_supervision_lock(paths) == 0
        with pytest.raises(supervisor.SupervisionDenied) as excinfo:
            supervisor.acquire_supervision_lock(paths)
        assert excinfo.value.reason == "run_already_supervised"
        assert not isinstance(excinfo.value, PermissionError), (
            "a PermissionError here could be absorbed by the provider gateway"
            " as 'try the next candidate'"
        )

    def test_racing_supervisors_produce_exactly_one_winner(self, tmp_path):
        """The kernel decides the lock, not a check-then-write.

        Twelve processes race for the same run; exactly one may win. A
        check-then-write lock would let several through and DOUBLE-EXECUTE the
        run, which is the failure this whole mechanism exists to prevent.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        script = tmp_path / "racer.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from runtime import supervisor\n"
            "paths = supervisor.supervision_paths('race', logs_root=Path(sys.argv[1]))\n"
            "try:\n"
            "    supervisor.acquire_supervision_lock(paths)\n"
            "    print('WON', flush=True)\n"
            "except supervisor.SupervisionDenied as exc:\n"
            "    print('DENIED', exc.reason, flush=True)\n",
            encoding="utf-8",
        )
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(logs)],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(12)
        ]
        winners = 0
        for proc in procs:
            out, _ = proc.communicate(timeout=60)
            winners += out.count("WON")
        assert winners == 1, f"{winners} processes believed they owned the run"

    def test_a_dead_owners_lock_is_taken_over(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("deadlock", logs_root=logs)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        dead = _dead_pid()
        atomic_write_json(
            paths.lock_path,
            {"pid": dead, "host": supervisor._host(), "at": "then", "epoch": 0.0},
        )
        assert supervisor.acquire_supervision_lock(paths) == 1, (
            "a dead owner's lock must be taken over, or a killed supervisor"
            " would make its run permanently unsupervisable"
        )

    def test_an_unreadable_lock_is_refused_fail_closed(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("garbage-lock", logs_root=logs)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        paths.lock_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(supervisor.SupervisionDenied) as excinfo:
            supervisor.acquire_supervision_lock(paths)
        assert excinfo.value.reason == "lock_unreadable"

    def test_a_lock_from_another_host_is_refused_not_guessed(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("foreign", logs_root=logs)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            paths.lock_path,
            {
                "pid": os.getpid(),
                "host": "some-other-machine",
                "at": "then",
                "epoch": 0.0,
            },
        )
        with pytest.raises(supervisor.SupervisionDenied) as excinfo:
            supervisor.acquire_supervision_lock(paths)
        assert excinfo.value.reason == "lock_owner_undecidable"

    def test_a_denied_supervision_writes_no_worker_and_says_why(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("denied-run", logs)
        paths = supervisor.supervision_paths("denied-run", logs_root=logs)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            paths.lock_path,
            {"pid": os.getpid(), "host": supervisor._host(), "at": "now", "epoch": 0.0},
        )
        outcome = supervisor.detach_task(task, logs_root=logs, detach=False)
        assert outcome.state == "denied"
        assert outcome.attempts == 0, "a denied supervision still launched a worker"
        assert json.loads(outcome.detail)["reason"] == "run_already_supervised"
        assert not (paths.attempts_dir / "attempt_0" / "worker.log").exists()


# ---------------------------------------------------------------------------
# 4. observable
# ---------------------------------------------------------------------------


class TestObservable:
    def test_the_record_answers_which_run_since_when_where_and_last_progress(
        self, tmp_path
    ):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("visible", logs, fake_step_delay_s=0.2)
        outcome = supervisor.detach_task(task, logs_root=logs, detach=False)
        record = supervisor.read_supervision("visible", logs_root=logs)
        assert record is not None
        for key in (
            "task_id",
            "state",
            "host",
            "supervisor_pid",
            "started_epoch",
            "uptime_s",
            "attempts",
            "restarts",
            "max_restarts",
            "worker_pid",
            "worker_pids",
            "last_progress_epoch",
            "last_progress_age_s",
            "worker_status",
        ):
            assert key in record, f"a live-run surface needs {key}"
        assert record["host"] == supervisor._host()
        assert record["supervisor_pid"] == os.getpid()
        assert record["worker_status"] == outcome.worker_status

    def test_live_supervisions_separates_live_from_terminal(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("listed", logs)
        supervisor.detach_task(task, logs_root=logs, detach=False)
        rows = supervisor.live_supervisions(logs)
        assert [r["task_id"] for r in rows] == ["listed"]
        assert rows[0]["live"] is False, "a finished run must not read as live"
        assert rows[0]["terminal"] is True

    def test_the_record_publishes_no_cost_of_its_own(self, tmp_path):
        """A supervised run's cost is reported ONCE, by its owner.

        The governor publishes ``budget.json``; the router ledger publishes
        per-call money. If the supervision record also published a cost — even
        a "correct" one — every restart would be a chance to add a second,
        contradictory number to a dashboard. The check is literal because the
        failure mode is a field that quietly appears.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("cost-once", logs, fake_crash_step="s2")
        outcome = supervisor.detach_task(
            task, logs_root=logs, detach=False, max_restarts=2
        )
        assert outcome.attempts == 2, "the run did not actually restart"
        assert "cost" not in json.dumps(outcome.to_dict()).lower()
        record = supervisor.read_supervision("cost-once", logs_root=logs)
        assert "cost" not in json.dumps(record).lower()
        # The run's own money still exists, in the worker's file.
        budget = read_json_or_none(_runtime_dir("cost-once", logs) / "budget.json")
        assert isinstance(budget, dict) and "spent_usd" in budget


# ---------------------------------------------------------------------------
# 5. bounded
# ---------------------------------------------------------------------------


class TestBounded:
    def test_a_repeatedly_crashing_worker_exhausts_and_reports_it(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("crasher", logs)
        outcome = supervisor.detach_task(
            task,
            logs_root=logs,
            detach=False,
            max_restarts=2,
            worker_argv=[sys.executable, "-c", "import sys; sys.exit(137)"],
        )
        assert outcome.state == "exhausted"
        assert outcome.attempts == 3, "the budget was not respected"
        assert outcome.restarts == 2
        assert "exhausted" in outcome.exhausted_reason
        assert outcome.worker_status is None, (
            "an exhausted run has no verdict and must not borrow one"
        )
        events = [e["event"] for e in _events(logs, "crasher")]
        assert events.count("worker_launched") == 3
        assert events[-1] == "restart_budget_exhausted"

    def test_the_default_budget_is_finite(self):
        assert supervisor.DEFAULT_MAX_RESTARTS >= 1
        assert supervisor.DEFAULT_MAX_RESTARTS <= 10, (
            "a restart budget is a resource bound; 10+ is an unbounded loop"
            " wearing a number"
        )

    def test_a_supervisor_that_cannot_spawn_says_so_instead_of_looping(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("no-spawn", logs)
        outcome = supervisor.detach_task(
            task,
            logs_root=logs,
            detach=False,
            max_restarts=5,
            worker_argv=[str(tmp_path / "definitely-not-an-executable")],
        )
        assert outcome.state == "failed"
        assert json.loads(outcome.detail)["reason"] == "worker_spawn_failed"
        assert outcome.attempts == 0, "a launch that never happened is not an attempt"


# ---------------------------------------------------------------------------
# 6. cancelled is not timeout
# ---------------------------------------------------------------------------


class TestInterruptedIsNotTimedOut:
    def test_exit_130_is_cancelled(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("interrupted", logs)
        outcome = supervisor.detach_task(
            task,
            logs_root=logs,
            detach=False,
            max_restarts=3,
            worker_argv=[sys.executable, "-c", "import sys; sys.exit(130)"],
        )
        assert outcome.worker_status == "cancelled"
        assert outcome.worker_status != "timeout"
        assert outcome.restarts == 0, "an interrupted run was restarted"

    def test_exit_124_is_timeout(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("timed-out", logs)
        outcome = supervisor.detach_task(
            task,
            logs_root=logs,
            detach=False,
            max_restarts=3,
            worker_argv=[sys.executable, "-c", "import sys; sys.exit(124)"],
        )
        assert outcome.worker_status == "timeout"
        assert outcome.worker_status != "cancelled"
        assert outcome.restarts == 0

    def test_the_two_codes_are_not_interchangeable(self):
        cancelled = supervisor.classify_attempt(None, supervisor.EXIT_INTERRUPTED)
        timed_out = supervisor.classify_attempt(None, supervisor.EXIT_TIMEOUT)
        assert cancelled["terminal"] == "cancelled"
        assert timed_out["terminal"] == "timeout"
        assert cancelled["terminal"] != timed_out["terminal"]

    def test_a_written_cancelled_verdict_survives_an_odd_exit_code(self, tmp_path):
        """A result.json says `cancelled`; the exit code says 1. Result wins."""
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("verdict-cancel", logs_root=logs)
        _write_result(paths.attempts_dir / "attempt_0", {"status": "cancelled"})
        verdict = supervisor.classify_attempt(
            {"status": "cancelled", "cost_usd": 0.0}, 1
        )
        assert verdict["terminal"] == "cancelled"
        assert verdict["retryable"] is False

    def test_only_the_absent_result_is_retryable(self):
        crash = supervisor.classify_attempt(None, 137)
        assert crash["retryable"] is True and crash["terminal"] is None
        for status in (
            "success",
            "failed",
            "error",
            "timeout",
            "cancelled",
            "needs_input",
        ):
            verdict = supervisor.classify_attempt({"status": status}, 0)
            assert verdict["retryable"] is False
            assert verdict["terminal"] == status


# ---------------------------------------------------------------------------
# 7. nothing leaks
# ---------------------------------------------------------------------------


class TestNothingLeaks:
    def test_a_finished_run_leaves_no_live_worker_and_no_lock(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("clean", logs)
        outcome = supervisor.detach_task(task, logs_root=logs, detach=False)
        paths = supervisor.supervision_paths("clean", logs_root=logs)
        assert outcome.state == "finished"
        assert not paths.lock_path.exists(), "the lock outlived its supervisor"
        for pid in outcome.worker_pids:
            assert not is_pid_alive(int(pid)), f"worker {pid} outlived the run"

    def test_an_exhausted_run_leaves_no_live_worker(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("exhausted-clean", logs)
        outcome = supervisor.detach_task(
            task,
            logs_root=logs,
            detach=False,
            max_restarts=1,
            worker_argv=[sys.executable, "-c", "import sys; sys.exit(137)"],
        )
        paths = supervisor.supervision_paths("exhausted-clean", logs_root=logs)
        assert outcome.state == "exhausted"
        assert not paths.lock_path.exists()
        for pid in outcome.worker_pids:
            assert not is_pid_alive(int(pid))

    def test_reap_reports_what_it_reaped(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        task = _fake_task("reap-me", logs)
        supervisor.detach_task(task, logs_root=logs, detach=False)
        paths = supervisor.supervision_paths("reap-me", logs_root=logs)
        # Plant the debris a hard-killed supervisor leaves behind: a lock owned
        # by a dead pid and a record that still claims to be live.
        dead = _dead_pid()
        atomic_write_json(
            paths.lock_path,
            {"pid": dead, "host": supervisor._host(), "at": "then", "epoch": 0.0},
        )
        record = read_json_or_none(paths.record_path)
        record["state"] = "worker_running"
        record["terminal"] = False
        record["supervisor_pid"] = dead
        atomic_write_json(paths.record_path, record)

        report = supervisor.reap_supervisions(logs)
        assert "reap-me" in report["reaped_locks"]
        assert "reap-me" in report["reaped_records"]
        assert not paths.lock_path.exists()
        after = supervisor.read_supervision("reap-me", logs_root=logs)
        assert after["state"] == "reaped"
        assert after["terminal"] is True

    def test_reap_never_removes_a_lock_it_cannot_prove_free(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("foreign-reap", logs_root=logs)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            paths.lock_path,
            {"pid": os.getpid(), "host": "elsewhere", "at": "then", "epoch": 0.0},
        )
        report = supervisor.reap_supervisions(logs)
        assert paths.lock_path.exists(), "reap cleared a lock it could not evaluate"
        assert report["skipped_locks"], "an undecidable lock must be reported"
        assert "foreign-reap" not in report["reaped_locks"]

    def test_a_live_owner_is_never_reaped(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        paths = supervisor.supervision_paths("live-owner", logs_root=logs)
        supervisor.acquire_supervision_lock(paths)
        atomic_write_json(
            paths.record_path,
            {
                "task_id": "live-owner",
                "state": "worker_running",
                "terminal": False,
                "supervisor_pid": os.getpid(),
                "worker_pids": {},
            },
        )
        report = supervisor.reap_supervisions(logs)
        assert paths.lock_path.exists()
        assert "live-owner" not in report["reaped_records"]


# ---------------------------------------------------------------------------
# 8. it did not grow the exemption vocabulary
# ---------------------------------------------------------------------------


class TestNoFourthExemption:
    def test_the_supervisor_never_writes_a_supervision_exemption(self):
        """A supervisor that parks on a dead heartbeat must not exempt itself.

        The exemption vocabulary is two reasons over three skip conditions and
        belongs to the scheduler. A new reason here would make an unreadable
        checkpoint, a wedged worker, or a slow provider into "not a hang", which
        is precisely how a hang detector gets switched off.
        """
        import inspect

        source = inspect.getsource(supervisor)
        assert "EXEMPTION_APPROVAL" not in source
        assert "EXEMPTION_BACKOFF" not in source
        for invented in (
            "supervisor_exempt",
            "supervised_exempt",
            "watcher_exempt",
            "watch_exempt",
            "throughput_exempt",
        ):
            assert invented not in source, (
                f"{invented} would be a FOURTH hang-kill exemption"
            )

    def test_the_supervisor_reads_the_shared_exemption_reader(self):
        """It may READ the exemption (so an approval park is not a hang) and
        that read is the only thing it is allowed to do with one."""
        import inspect

        source = inspect.getsource(supervisor)
        assert "state_stale_exempt" in source
        assert "supervision_exemption" not in source

    def test_the_exemption_vocabulary_is_still_exactly_two(self):
        from runtime import budget_governor

        reasons = {
            getattr(budget_governor, name)
            for name in dir(budget_governor)
            if name.startswith("EXEMPTION_")
        }
        assert reasons == {"approval_gate", "provider_backoff"}, reasons
