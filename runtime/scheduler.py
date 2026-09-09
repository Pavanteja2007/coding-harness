"""Scheduler — concurrent, supervised execution of tasks (Boundary 6).

Runs many tasks by spawning one worker process per task (`python -m
runtime.worker`), each calling Boundary 3's run_task. Design:

  - Concurrency cap: at most `concurrency` worker processes alive at once;
    waiting tasks sit in a FIFO queue and are spawned into free slots.
  - Supervision (scheduler polls every worker):
      * wall-clock timeout: attempt age > max_wallclock_s -> kill
      * hang timeout: heartbeat older than hang_heartbeat_stale_s
        (grace: hang check applies only after the attempt itself is older
        than the stale threshold, so a fresh attempt is never killed for
        a stale heartbeat left by the previous killed attempt)
  - Crash/retry: a worker that exits WITHOUT result.json is relaunched
    with the same task JSON; the worker resumes from completed steps
    (one-shot fault injection pops in the worker). The crash budget is
    tracked PER TASK (crash_retries), never reset by a respawn.
  - Approvals: the worker blocks in the approval gate; the scheduler
    just surfaces pending requests (see runtime/approval.py).

Run artifacts, under logs/{run_id}/:
  events.jsonl                  run-level event journal
  {task_id}/attempt_{n}/        worker stdout log + result.json
"""
from __future__ import annotations

import queue
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime.checkpoint import TaskCheckpoint
from runtime.config import apply_defaults
from runtime.fsutil import append_jsonl, atomic_write_json, now_iso, now_epoch, read_json
from runtime.paths import runtime_root, state_json_path
from runtime.serialize import result_from_dict

from shared.types import Task, TaskResult

POLL_INTERVAL_S = 0.1
DEFAULT_HANG_STALE_S = 30.0
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Attempt:
    task: Task
    proc: subprocess.Popen
    run_dir: Path
    attempt: int
    started_epoch: float
    max_wallclock_s: float
    hang_stale_s: float

    @property
    def task_id(self) -> str:
        return self.task.task_id


class Scheduler:
    """Supervised, concurrency-capped task runner.

    Assumes: unique task_ids; each task's log root (resume_dir or
    logs/{task_id}/) is exclusively owned by that task's workers; this
    process is the only scheduler for this run_id. Never raises on a
    task's behalf — failures come back as TaskResults.
    """

    def __init__(self, concurrency: int = 10, logs_root: str = "logs",
                 run_id: Optional[str] = None) -> None:
        self.concurrency = max(1, int(concurrency))
        # Absolute so worker subprocesses (different CWDs) and this
        # process always resolve the same tree.
        self.logs_root = Path(logs_root).resolve()
        self.run_id = run_id or f"run_{int(now_epoch())}"
        self.run_dir = self.logs_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._spawn_count: Dict[str, int] = {}
        self._crash_budget: Dict[str, int] = {}
        self._active: Dict[str, "_Attempt"] = {}  # live_attempts() view

    # -- public API ------------------------------------------------------

    def run(self, tasks: List[Task], poll_interval_s: float = POLL_INTERVAL_S) -> Dict[str, TaskResult]:
        """Run all tasks concurrently (capped); returns {task_id: TaskResult}.

        On KeyboardInterrupt, kills live workers and returns the partial
        result set — checkpoints stay on disk, so a later run resumes.
        """
        results: Dict[str, TaskResult] = {}
        pending: "queue.Queue[Task]" = queue.Queue()
        for t in tasks:
            if not t.task_id:
                raise ValueError("every task needs a non-empty task_id")
            pending.put(t)
        active: Dict[str, _Attempt] = {}
        self._active = active  # exposed via live_attempts() for observers

        self._log("run_start", {"n_tasks": len(tasks),
                                "concurrency": self.concurrency})
        try:
            while not pending.empty() or active:
                self._reap(active, pending, results)
                while not pending.empty() and len(active) < self.concurrency:
                    task = pending.get()
                    att = self._spawn(task)
                    active[task.task_id] = att
                if active or not pending.empty():
                    time.sleep(poll_interval_s)
        except KeyboardInterrupt:
            self._log("run_interrupted", {"live": len(active)})
            for att in active.values():
                self._kill(att)
            raise
        self._log("run_finish", {"n_results": len(results)})
        return results

    def live_attempts(self) -> Dict[str, "_Attempt"]:
        """Snapshot of currently-running attempts (task_id -> _Attempt).

        Public observability hook for supervisors/dashboards that need
        the live worker set (e.g. the stress harness killing workers
        mid-run). Assumes run() is executing on another thread; returns
        a copy — callers must not mutate. NOTE: this needs the run loop
        to share its active map; run() stores it on self._active.
        """
        return dict(self._active)

    # -- spawning --------------------------------------------------------

    def _spawn(self, task: Task) -> _Attempt:
        """Write task JSON + launch one worker process for `task`."""
        cfg = apply_defaults(task.config)
        # Pin the task's runtime bookkeeping + harness log root to THIS
        # scheduler's logs tree unless the task config already overrides
        # them. Without this, a scheduler given a non-default logs_root
        # would look for checkpoints in its own tree while its workers
        # wrote theirs to ./logs/ (worker default) — split-brain state.
        cfg.setdefault("resume_dir", str(self.logs_root / f"{task.task_id}.runtime"))
        cfg.setdefault("log_root", str(self.logs_root))
        n = self._spawn_count.get(task.task_id, 0)
        self._spawn_count[task.task_id] = n + 1
        if task.task_id not in self._crash_budget:
            self._crash_budget[task.task_id] = int(cfg.get("crash_retries", 1))

        run_dir = self.run_dir / task.task_id / f"attempt_{n}"
        run_dir.mkdir(parents=True, exist_ok=True)
        task_json = run_dir / "task.json"
        atomic_write_json(task_json, {
            "task_id": task.task_id,
            "repo_path": task.repo_path,
            "issue_text": task.issue_text,
            "config": cfg,
        })

        worker_log = open(run_dir / "worker.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "runtime.worker",
             "--task-json", str(task_json), "--run-dir", str(run_dir)],
            cwd=str(REPO_ROOT),
            stdout=worker_log,
            stderr=subprocess.STDOUT,
        )
        worker_log.close()
        self._log("spawn", {"task_id": task.task_id, "attempt": n, "pid": proc.pid})
        return _Attempt(
            task=task, proc=proc, run_dir=run_dir, attempt=n,
            started_epoch=now_epoch(),
            max_wallclock_s=float(cfg.get("max_wallclock_s", 900.0)),
            hang_stale_s=float(cfg.get("hang_heartbeat_stale_s", DEFAULT_HANG_STALE_S)),
        )

    # -- supervision -----------------------------------------------------

    def _reap(self, active: Dict[str, _Attempt], pending: "queue.Queue[Task]",
             results: Dict[str, TaskResult]) -> None:
        """Poll every live attempt; finish, kill, or requeue as needed."""
        for task_id, att in list(active.items()):
            exit_code = att.proc.poll()

            if exit_code is None:  # still alive — supervision checks
                if self._check_timeouts(att):
                    self._after_kill(att, pending, results, reason="timeout")
                    del active[task_id]
                continue

            result_path = att.run_dir / "result.json"
            if result_path.exists():
                results[task_id] = result_from_dict(read_json(result_path))
                self._log("finish", {"task_id": task_id,
                                      "status": results[task_id].status,
                                      "attempt": att.attempt})
            else:
                # exited without a result: crash/kill — retry with resume
                self._log("crash", {"task_id": task_id, "exit_code": exit_code})
                self._after_crash(att, pending, results, exit_code)
            del active[task_id]

    def _check_timeouts(self, att: _Attempt) -> bool:
        """Kill on wall-clock overrun or a hung worker; True if killed.

        Hang = no PROGRESS for hang_stale_s: state.json (Boundary 4,
        rewritten per step by the harness) whose mtime went stale. The
        heartbeat alone can't detect hangs — it's a daemon thread that
        keeps beating while the main thread is stuck — so it stays a
        process-liveness signal (a stale heartbeat means something worse
        than a hang; still kill+requeue). Grace: checks apply only once
        the attempt itself is older than hang_stale_s, so a fresh attempt
        never inherits the previous attempt's stale mtime.
        """
        age = now_epoch() - att.started_epoch
        if age > att.max_wallclock_s:
            self._log("wallclock_timeout", {"task_id": att.task_id, "age_s": age})
            self._kill(att)
            return True
        if age > att.hang_stale_s:
            cfg = apply_defaults(att.task.config)
            cp = TaskCheckpoint(str(runtime_root(att.task_id, cfg, self.logs_root)))
            hb_age = cp.heartbeat_age_s()
            state_path = state_json_path(att.task_id, cfg, self.logs_root)
            state_age = (now_epoch() - state_path.stat().st_mtime
                         if state_path.exists() else None)
            if hb_age is not None and hb_age > att.hang_stale_s:
                self._log("hang_timeout", {"task_id": att.task_id,
                                            "signal": "heartbeat",
                                            "heartbeat_age_s": hb_age})
                self._kill(att)
                return True
            # A worker parked in the approval gate is alive but makes no
            # harness progress — state.json is EXPECTED to go stale there.
            # It keeps heartbeating (the daemon stops only after the
            # gate), so: gate-parked + fresh heartbeat = not a hang; the
            # wall-clock cap remains the backstop for an unbounded park,
            # and a dead heartbeat still kills (caught above).
            # awaiting_approval is set by the worker around the gate and
            # cleared atomically with status="finished" (worker.py); a
            # FINISHED checkpoint also exempts the state-stale kill — the
            # worker may be writing result.json milliseconds before exit.
            cp_data = cp.load() or {}
            if (bool(cp_data.get("awaiting_approval"))
                    or cp_data.get("status") == "finished"):
                if hb_age is not None:
                    return False
                # no heartbeat file at all: fall through to the
                # state-stale check (can't prove liveness)
            if state_age is not None and state_age > att.hang_stale_s:
                self._log("hang_timeout", {"task_id": att.task_id,
                                            "signal": "state_stale",
                                            "state_age_s": state_age})
                self._kill(att)
                return True
        return False

    def _after_crash(self, att: _Attempt, pending: "queue.Queue[Task]",
                     results: Dict[str, TaskResult], exit_code: int) -> None:
        if self._crash_budget.get(att.task_id, 0) > 0:
            self._crash_budget[att.task_id] -= 1
            self._log("crash_retry", {"task_id": att.task_id,
                                       "retries_left": self._crash_budget[att.task_id]})
            pending.put(att.task)
        else:
            results[att.task_id] = self._failed_result(att, "error", exit_code)
            self._log("crash_exhausted", {"task_id": att.task_id,
                                           "exit_code": exit_code})

    def _after_kill(self, att: _Attempt, pending: "queue.Queue[Task]",
                    results: Dict[str, TaskResult], reason: str) -> None:
        """A scheduler-side kill (timeout/hang): resume if budget allows."""
        if self._crash_budget.get(att.task_id, 0) > 0:
            self._crash_budget[att.task_id] -= 1
            self._log("kill_requeue", {"task_id": att.task_id, "reason": reason,
                                       "retries_left": self._crash_budget[att.task_id]})
            pending.put(att.task)
        else:
            results[att.task_id] = self._failed_result(att, "timeout")
            self._log("kill_exhausted", {"task_id": att.task_id, "reason": reason})

    def _failed_result(self, att: _Attempt, status: str,
                       exit_code: Optional[int] = None) -> TaskResult:
        return TaskResult(
            task_id=att.task_id,
            status=status,  # type: ignore[arg-type]
            attempts=att.attempt + 1,
            diff=None, verification=None, cost_usd=0.0,
            model_calls=[], log_path=str(att.run_dir),
        )

    def _kill(self, att: _Attempt) -> None:
        if att.proc.poll() is None:
            att.proc.kill()
            try:
                att.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    # -- helpers ---------------------------------------------------------

    def _task_log_root(self, task: Task) -> Path:
        cfg = apply_defaults(task.config)
        return runtime_root(task.task_id, cfg, self.logs_root)

    def _log(self, event: str, data: Dict[str, Any]) -> None:
        append_jsonl(self.run_dir / "events.jsonl",
                     {"ts": now_iso(), "event": event, "data": data})


def run(tasks: List[Task], concurrency: int = 10, logs_root: str = "logs",
        run_id: Optional[str] = None) -> Dict[str, TaskResult]:
    """Boundary 6 convenience entry: supervised concurrent run of `tasks`.

    Assumes unique task_ids; returns task_id -> TaskResult for every task
    (error/timeout statuses included — never raises per task).
    """
    return Scheduler(concurrency=concurrency, logs_root=logs_root,
                     run_id=run_id).run(tasks)
