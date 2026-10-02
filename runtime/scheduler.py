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
      * supervision exemption: a worker that is legitimately not making
        harness progress -- parked in the approval gate, or inside a
        provider backoff -- is exempt from the STATE-STALE kill only, and
        only while its exemption is live (R2-14). The heartbeat kill and
        the wall-clock cap are never exempted. The exemption is read from
        `runtime.budget_governor.state_stale_exempt` on the SAME clock
        (`runtime.fsutil.now_epoch`) that armed it, so a backoff longer
        than the stale window can no longer be mistaken for a hang.
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

import json
import os
import queue
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime import budget_governor
from runtime import config as config_mod
from runtime.checkpoint import TaskCheckpoint
from runtime.config import apply_defaults
from runtime.fsutil import (
    TASK_SECRETS_ENV,
    append_jsonl,
    atomic_write_json,
    extract_sensitive_config,
    now_epoch,
    now_iso,
    read_json,
    redact_sensitive_config,
)
from runtime.paths import runtime_root, state_json_path, validate_path_segment
from runtime.serialize import result_from_dict
from shared import tracing
from shared.types import Task, TaskResult

POLL_INTERVAL_S = 0.1
# The default state-stale window lives in runtime/config.py (one source for
# the kill threshold AND the worker's post-backoff grace); re-exported here
# because `DEFAULT_HANG_STALE_S` is this module's historical public name.
DEFAULT_HANG_STALE_S = config_mod.DEFAULT_HANG_STALE_S
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
    attempt_token: str
    #: Whether a live PROVIDER-BACKOFF exemption is honoured (R2-14).
    #: Defaults to True; an explicit ``False`` restores the historical
    #: behaviour of state-stale-killing a backing-off worker, so the OFF
    #: arm is a real measured comparison rather than a claim. The
    #: approval-gate exemption is NOT switchable: it predates this round
    #: and an operator turning off the backoff fix must not silently
    #: re-introduce the mid-gate kill.
    hang_backoff_exempt: bool = True

    @property
    def task_id(self) -> str:
        return self.task.task_id


class Scheduler:
    """Supervised, concurrency-capped task runner.

    Assumes: task IDs are unique and path-safe; each task's log root is
    exclusively owned by that task's workers; this
    process is the only scheduler for this run_id. Never raises on a
    task's behalf — failures come back as TaskResults.
    """

    def __init__(
        self,
        concurrency: int = 10,
        logs_root: str = "logs",
        run_id: Optional[str] = None,
    ) -> None:
        self.concurrency = max(1, int(concurrency))
        self.logs_root = Path(logs_root).resolve()
        self.run_id = validate_path_segment(
            run_id or f"run_{time.time_ns()}_{uuid.uuid4().hex[:8]}",
            "run id",
        )
        self.run_dir = self.logs_root / self.run_id
        if self.run_dir.exists() and any(self.run_dir.iterdir()):
            raise ValueError(f"run directory already exists: {self.run_dir}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._spawn_count: Dict[str, int] = {}
        self._crash_budget: Dict[str, int] = {}
        self._active: Dict[str, "_Attempt"] = {}  # live_attempts() view

    # -- public API ------------------------------------------------------

    def run(
        self, tasks: List[Task], poll_interval_s: float = POLL_INTERVAL_S
    ) -> Dict[str, TaskResult]:
        """Run all tasks concurrently (capped); returns {task_id: TaskResult}.

        On KeyboardInterrupt, live workers are killed and the interrupt is
        re-raised after cleanup; checkpoints stay on disk for later resume.
        """
        results: Dict[str, TaskResult] = {}
        pending: "queue.Queue[Task]" = queue.Queue()
        seen: set[str] = set()
        for task in tasks:
            validate_path_segment(task.task_id, "task id")
            if task.task_id in seen:
                raise ValueError(f"duplicate task id: {task.task_id}")
            seen.add(task.task_id)
            pending.put(task)
        active: Dict[str, _Attempt] = {}
        self._active = active  # exposed via live_attempts() for observers

        self._log("run_start", {"n_tasks": len(tasks), "concurrency": self.concurrency})
        try:
            while not pending.empty() or active:
                self._reap(active, pending, results)
                while not pending.empty() and len(active) < self.concurrency:
                    task = pending.get()
                    try:
                        attempt = self._spawn(task)
                    except Exception as exc:
                        results[task.task_id] = TaskResult(
                            task_id=task.task_id,
                            status="error",
                            attempts=0,
                            diff=None,
                            verification=None,
                            cost_usd=0.0,
                            model_calls=[],
                            log_path=str(self.run_dir / task.task_id),
                        )
                        self._log(
                            "spawn_error",
                            {
                                "task_id": task.task_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        continue
                    active[task.task_id] = attempt
                if active or not pending.empty():
                    time.sleep(poll_interval_s)
        except KeyboardInterrupt:
            self._log("run_interrupted", {"live": len(active)})
            for attempt in active.values():
                self._kill(attempt)
            raise
        except Exception:
            for attempt in active.values():
                self._kill(attempt)
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
        if not cfg.get("resume_dir"):
            cfg["resume_dir"] = str(self.logs_root / f"{task.task_id}.runtime")
        if not cfg.get("log_root"):
            cfg["log_root"] = str(self.logs_root)
        if not cfg.get("resume_namespace"):
            cfg["resume_namespace"] = self.run_id
        n = self._spawn_count.get(task.task_id, 0)
        self._spawn_count[task.task_id] = n + 1
        if task.task_id not in self._crash_budget:
            self._crash_budget[task.task_id] = int(cfg.get("crash_retries", 1))

        run_dir = self.run_dir / task.task_id / f"attempt_{n}"
        run_dir.mkdir(parents=True, exist_ok=True)
        task_json = run_dir / "task.json"
        secret_entries = extract_sensitive_config(cfg)
        atomic_write_json(
            task_json,
            {
                "task_id": task.task_id,
                "repo_path": task.repo_path,
                "issue_text": "",
                "config": redact_sensitive_config(cfg),
            },
        )
        child_env = os.environ.copy()
        child_env[TASK_SECRETS_ENV] = json.dumps(
            {"issue_text": task.issue_text, "secrets": secret_entries}
        )
        attempt_token = uuid.uuid4().hex
        proc: Optional[subprocess.Popen] = None
        try:
            with (run_dir / "worker.log").open("w", encoding="utf-8") as worker_log:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "runtime.worker",
                        "--task-json",
                        str(task_json),
                        "--run-dir",
                        str(run_dir),
                        "--attempt-token",
                        attempt_token,
                    ],
                    cwd=str(REPO_ROOT),
                    env=child_env,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                )
            self._log(
                "spawn",
                {
                    "task_id": task.task_id,
                    "attempt": n,
                    "pid": proc.pid,
                    "attempt_token": attempt_token,
                },
            )
        except BaseException:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        if proc is None:
            raise RuntimeError("worker process was not created")
        return _Attempt(
            task=task,
            proc=proc,
            run_dir=run_dir,
            attempt=n,
            started_epoch=now_epoch(),
            max_wallclock_s=float(cfg.get("max_wallclock_s", 900.0)),
            hang_stale_s=float(cfg.get("hang_heartbeat_stale_s", DEFAULT_HANG_STALE_S)),
            attempt_token=attempt_token,
            # Absent = the fix is on. Only an explicit False turns it off,
            # which is the `recovery_loop_guard_read_only` precedent: the
            # safe reading is the default and turning it off is a
            # deliberate act.
            hang_backoff_exempt=cfg.get("hang_backoff_exempt", True) is not False,
        )

    # -- supervision -----------------------------------------------------

    def _completed_result(
        self, attempt: _Attempt, exit_code: Optional[int]
    ) -> Optional[TaskResult]:
        """Return a coherent result only when worker and checkpoint both finished."""
        if exit_code != 0:
            return None
        config = apply_defaults(attempt.task.config)
        checkpoint = TaskCheckpoint(
            str(runtime_root(attempt.task_id, config, self.logs_root))
        ).load()
        if not isinstance(checkpoint, dict) or checkpoint.get("status") != "finished":
            return None
        result_path = attempt.run_dir / "result.json"
        if not result_path.exists():
            return None
        try:
            result = result_from_dict(read_json(result_path))
        except (OSError, TypeError, ValueError, KeyError):
            return None
        if result.task_id != attempt.task_id:
            return None
        if result.status not in {"success", "failed", "error", "timeout"}:
            return None
        return result

    def _reap(
        self,
        active: Dict[str, _Attempt],
        pending: "queue.Queue[Task]",
        results: Dict[str, TaskResult],
    ) -> None:
        """Poll every live attempt; finish, kill, or requeue as needed."""
        for task_id, att in list(active.items()):
            exit_code = att.proc.poll()

            if exit_code is None:  # still alive — supervision checks
                if self._check_timeouts(att):
                    self._after_kill(att, pending, results, reason="timeout")
                    del active[task_id]
                continue

            result = self._completed_result(att, exit_code)
            if result is not None:
                results[task_id] = result
                # R2-14: a terminal reason recorded by the worker (today:
                # `quota_exhausted`) rides the run journal, so "your credit
                # is gone" is visible in the run's own event stream and not
                # only inside a worker log. `TaskResult.status` stays inside
                # the historical four-value vocabulary.
                terminal_reason = self._terminal_reason(att)
                self._log(
                    "finish",
                    {
                        "task_id": task_id,
                        "status": result.status,
                        "attempt": att.attempt,
                        **(
                            {"terminal_reason": terminal_reason}
                            if terminal_reason
                            else {}
                        ),
                    },
                )
            else:
                self._log(
                    "crash",
                    {
                        "task_id": task_id,
                        "exit_code": exit_code,
                        "result_present": (att.run_dir / "result.json").exists(),
                    },
                )
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

        The state-stale signal is the ONE that needs an exemption, because
        it is the one that cannot tell "stuck" from "waiting on purpose".
        A worker inside a provider backoff is waiting on purpose: the
        measured 429 backoff (~225 s) used to exceed this window (~30 s),
        so a provider backing off CORRECTLY was killed as hung. The
        exemption is the approval-park marker generalized
        (`supervision_exemption` in the runtime checkpoint, read through
        `runtime.budget_governor.state_stale_exempt`), so there is one
        concept with two reasons rather than two exemptions. It is NOT
        honoured for a dead heartbeat (a parked worker whose process died
        is still dead) and NOT for the wall-clock cap (an unbounded wait
        is still bounded).
        """
        age = now_epoch() - att.started_epoch
        if age > att.max_wallclock_s:
            self._log("wallclock_timeout", {"task_id": att.task_id, "age_s": age})
            if self._kill(att):
                return True
            self._log(
                "kill_unconfirmed", {"task_id": att.task_id, "reason": "wallclock"}
            )
            return False
        if age > att.hang_stale_s:
            cfg = apply_defaults(att.task.config)
            cp = TaskCheckpoint(str(runtime_root(att.task_id, cfg, self.logs_root)))
            hb_age = cp.heartbeat_age_s(att.attempt_token)
            state_path = state_json_path(att.task_id, cfg, self.logs_root)
            state_age = (
                now_epoch() - state_path.stat().st_mtime
                if state_path.exists()
                else None
            )
            if hb_age is not None and hb_age > att.hang_stale_s:
                self._log(
                    "hang_timeout",
                    {
                        "task_id": att.task_id,
                        "signal": "heartbeat",
                        "heartbeat_age_s": hb_age,
                    },
                )
                if self._kill(att):
                    return True
                self._log(
                    "kill_unconfirmed",
                    {"task_id": att.task_id, "reason": "heartbeat"},
                )
                return False
            # A worker that is legitimately not making harness progress is
            # not hung. Two reasons today, ONE mechanism: the approval gate
            # (a human is deciding) and a provider backoff (the provider
            # said 429 and the router is waiting it out). Both arrive as
            # `supervision_exemption` in the runtime checkpoint, and both
            # are read on the same clock that armed them. The backoff's
            # deadline is derived from `backoff_seconds` -- the same value
            # the retry loop sleeps -- so the watchdog and the backoff
            # cannot disagree about what "slow" means. An EXPIRED
            # exemption is not an exemption, so a worker that stops making
            # progress after its wait ends is still killed.
            cp_data = cp.load()
            if cp.path.exists() and cp_data is None:
                self._log(
                    "checkpoint_unreadable",
                    {"task_id": att.task_id, "signal": "state_stale"},
                )
                return False
            cp_data = cp_data or {}
            exempt, exempt_reason = budget_governor.state_stale_exempt(cp_data)
            if exempt and not att.hang_backoff_exempt:
                # The documented OFF arm: an operator who wants the
                # historical behaviour gets it, and the refusal is recorded
                # rather than being indistinguishable from a bug.
                exempt = False
                exempt_reason = (
                    f"{exempt_reason} (suppressed by hang_backoff_exempt=False)"
                )
                self._log(
                    "hang_exempt_suppressed",
                    {
                        "task_id": att.task_id,
                        "signal": "state_stale",
                        "reason": exempt_reason,
                    },
                )
            if (exempt or cp_data.get("status") == "finished") and hb_age is not None:
                if exempt:
                    # Logged once per distinct reason so the exemption is
                    # auditable: a reader must be able to see that the
                    # worker was NOT killed and why.
                    self._log(
                        "hang_exempt",
                        {
                            "task_id": att.task_id,
                            "signal": "state_stale",
                            "reason": exempt_reason,
                            "state_age_s": state_age,
                            "hang_stale_s": att.hang_stale_s,
                        },
                    )
                return False
            # no heartbeat file at all: fall through to the
            # state-stale check (can't prove liveness)
            if state_age is not None and state_age > att.hang_stale_s:
                self._log(
                    "hang_timeout",
                    {
                        "task_id": att.task_id,
                        "signal": "state_stale",
                        "state_age_s": state_age,
                    },
                )
                if self._kill(att):
                    return True
                self._log(
                    "kill_unconfirmed",
                    {"task_id": att.task_id, "reason": "state_stale"},
                )
                return False
        return False

    def _terminal_reason(self, att: _Attempt) -> Optional[str]:
        """Return the worker's recorded terminal reason, or None.

        R2-14: the worker stamps `terminal_reason` on its FINAL checkpoint
        (currently `quota_exhausted`). Reading it here rather than inferring
        a reason from the status keeps "an error" and "your credit is gone"
        distinguishable in the run journal. A checkpoint that cannot be read
        returns None, which the caller treats as "no reason recorded" -- the
        historical behaviour.
        """
        try:
            cfg = apply_defaults(att.task.config)
            checkpoint = TaskCheckpoint(
                str(runtime_root(att.task_id, cfg, self.logs_root))
            ).load()
        except OSError:
            return None
        if not isinstance(checkpoint, dict):
            return None
        reason = checkpoint.get("terminal_reason")
        return str(reason) if reason else None

    def _after_crash(
        self,
        att: _Attempt,
        pending: "queue.Queue[Task]",
        results: Dict[str, TaskResult],
        exit_code: int,
    ) -> None:
        if self._crash_budget.get(att.task_id, 0) > 0:
            self._crash_budget[att.task_id] -= 1
            self._log(
                "crash_retry",
                {
                    "task_id": att.task_id,
                    "retries_left": self._crash_budget[att.task_id],
                },
            )
            pending.put(att.task)
        else:
            results[att.task_id] = self._failed_result(att, "error", exit_code)
            self._log(
                "crash_exhausted", {"task_id": att.task_id, "exit_code": exit_code}
            )

    def _after_kill(
        self,
        att: _Attempt,
        pending: "queue.Queue[Task]",
        results: Dict[str, TaskResult],
        reason: str,
    ) -> None:
        """A scheduler-side kill (timeout/hang): resume if budget allows."""
        if self._crash_budget.get(att.task_id, 0) > 0:
            self._crash_budget[att.task_id] -= 1
            self._log(
                "kill_requeue",
                {
                    "task_id": att.task_id,
                    "reason": reason,
                    "retries_left": self._crash_budget[att.task_id],
                },
            )
            pending.put(att.task)
        else:
            results[att.task_id] = self._failed_result(att, "timeout")
            self._log("kill_exhausted", {"task_id": att.task_id, "reason": reason})

    def _failed_result(
        self, att: _Attempt, status: str, exit_code: Optional[int] = None
    ) -> TaskResult:
        return TaskResult(
            task_id=att.task_id,
            status=status,  # type: ignore[arg-type]
            attempts=att.attempt + 1,
            diff=None,
            verification=None,
            cost_usd=0.0,
            model_calls=[],
            log_path=str(att.run_dir),
        )

    def _kill(self, att: _Attempt) -> bool:
        """Kill an attempt and return only after its process is reaped."""
        if att.proc.poll() is not None:
            return True
        try:
            att.proc.kill()
        except ProcessLookupError:
            return True
        try:
            att.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return False
        return True

    # -- helpers ---------------------------------------------------------

    def _task_log_root(self, task: Task) -> Path:
        cfg = apply_defaults(task.config)
        return runtime_root(task.task_id, cfg, self.logs_root)

    def _log(self, event: str, data: Dict[str, Any]) -> None:
        append_jsonl(
            self.run_dir / "events.jsonl",
            {"ts": now_iso(), "event": event, "data": data},
        )
        # Cross-module structured tracing (shared.tracing): the same event
        # rides the unified per-task stream so a task's lifecycle is
        # reconstructible from one place. task-scoped events (spawn/
        # finish/crash/kill*) go to that task's stream; run-scoped ones
        # (run_start/run_finish) to the run overlay. No-op when
        # NEO_TRACE_DIR is unset; never raises (tracing is observability,
        # never correctness).
        task_id = str(data.get("task_id") or "")
        if task_id:
            tracing.emit(
                "runtime",
                event,
                task_id=task_id,
                run_id=self.run_id,
                **{k: v for k, v in data.items() if k != "task_id"},
            )
        else:
            tracing.emit_run("runtime", event, run_id=self.run_id, **data)


def run(
    tasks: List[Task],
    concurrency: int = 10,
    logs_root: str = "logs",
    run_id: Optional[str] = None,
) -> Dict[str, TaskResult]:
    """Boundary 6 convenience entry: supervised concurrent run of `tasks`.

    Assumes unique task_ids; returns task_id -> TaskResult for every task
    (error/timeout statuses included — never raises per task).
    """
    return Scheduler(concurrency=concurrency, logs_root=logs_root, run_id=run_id).run(
        tasks
    )
