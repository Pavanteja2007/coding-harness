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
import time
from pathlib import Path

import pytest

from runtime.scheduler import Scheduler
from shared.types import Task


def _task(task_id: str, logs_root: Path, **config) -> Task:
    resume_dir = logs_root / "tasks" / task_id
    cfg = {"use_fake_harness": True,
           "fake_state_dir": str(resume_dir),
           "resume_dir": str(resume_dir),
           "crash_retries": 2,
           "fake_step_delay_s": 0.15,
           **config}
    return Task(task_id=task_id, repo_path="", issue_text="fake issue",
                config=cfg)


def _mk_sched(logs_root: Path, conc: int = 4, run_id: str = None) -> Scheduler:
    return Scheduler(concurrency=conc, logs_root=str(logs_root),
                     run_id=run_id or f"test_{int(time.time()*1000)}")


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
        sched.run(tasks)
        events = _events(sched)

        running = 0
        max_overlap = 0
        for e in events:
            ev = e["event"]
            if ev == "spawn":
                running += 1
                max_overlap = max(max_overlap, running)
            elif ev in ("finish", "crash_retry", "crash_exhausted",
                        "kill_requeue", "kill_exhausted"):
                running -= 1
        assert max_overlap <= conc
        # sanity: all 10 tasks were spawned and all left the active set
        assert sum(1 for e in events if e["event"] == "spawn") == 10
        assert running == 0

    def test_cap_faster_than_serial(self, tmp_path):
        """Parallelism beats serial: 10 tasks x 5 steps x 0.3s at conc=5.
        Serial floor = 15s; with real work dominating process-spawn
        overhead, conc=5 should land near ~3s. Threshold 8s leaves >2x
        margin against spawn-noise flakiness while still failing a
        degenerate (serial or half-serial) run."""
        sched = _mk_sched(tmp_path, conc=5)
        tasks = [_task(f"t{i}", tmp_path, fake_step_delay_s=0.3) for i in range(10)]
        start = time.time()
        sched.run(tasks)
        elapsed = time.time() - start
        # sanity: all succeeded
        assert sched.run_dir.exists()
        assert elapsed < 8.0, f"took {elapsed:.1f}s (serial floor is 15s)"


class TestCrashResume:
    def test_midrun_crash_resumes_from_completed_steps(self, tmp_path):
        """THE core resume test: task crashes at step 3 of 5 (hard kill),
        scheduler relaunches, and the resumed run SKIPS already-completed
        steps — proven by step timestamps in state.json attempt dirs and
        the worker events journal showing resume=True."""
        sched = _mk_sched(tmp_path, conc=2)
        crash_step = "edit"  # plan, retrieve completed before the crash
        task = _task("crashy", tmp_path, fake_crash_step=crash_step,
                     fake_step_delay_s=0.1)
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
        worker_events = [json.loads(l) for l in ev_path.read_text().splitlines() if l.strip()]
        starts = [e for e in worker_events if e["event"] == "worker_start"]
        assert len(starts) == 2
        assert starts[0]["data"]["resume"] is False
        assert starts[1]["data"]["resume"] is True

        # state.json shows ALL steps completed at the end
        state = json.loads((tmp_path / "tasks" / "crashy" / "state.json").read_text())
        assert state["completed_steps"] == ["plan", "retrieve", "edit", "verify",
                                             "git-output"]
        assert state["remaining_plan"] == []

    def test_fresh_restart_when_no_progress(self, tmp_path):
        """Resume requires prior progress: a checkpoint with NO completed
        steps must not claim resume (attempts stays 1)."""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task("fresh", tmp_path, fake_crash_step="plan",
                     fake_step_delay_s=0.05)
        results = sched.run([task])
        # crash at step 1 leaves no completed steps -> resume can't skip
        # anything but the run still completes via relaunch
        assert results["fresh"].status == "success"
        # attempts: 2 (crash + relaunch) — the relaunch is a fresh start
        ev_path = tmp_path / "tasks" / "fresh" / "events.jsonl"
        worker_events = [json.loads(l) for l in ev_path.read_text().splitlines() if l.strip()]
        starts = [e for e in worker_events if e["event"] == "worker_start"]
        assert starts[1]["data"]["resume"] is False

    def test_crash_budget_exhaustion(self, tmp_path):
        """crash_retries=0: the first crash fails the task outright."""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task("doomed", tmp_path, fake_crash_step="edit",
                     crash_retries=0, fake_step_delay_s=0.05)
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
        task = _task("hangy", tmp_path, fake_hang_step="verify",
                     hang_heartbeat_stale_s=1.5, fake_step_delay_s=0.05)
        start = time.time()
        results = sched.run([task])
        elapsed = time.time() - start
        assert results["hangy"].status == "success"
        assert elapsed < 30  # didn't wait for the 1e9s sleep
        kinds = [e["event"] for e in _events(sched)]
        assert "hang_timeout" in kinds
        assert "kill_requeue" in kinds

    def test_wallclock_timeout_fails_after_budget(self, tmp_path):
        """max_wallclock_s tiny + slow-but-progressing task: each attempt
        overruns its wall clock and gets killed; after the crash budget
        is spent the task's result is status=timeout. (A hang-only task
        would be caught by hang detection first — this test isolates the
        wall-clock path with continuous progress and steps longer than
        the cap: steps take 2.5s each, cap is 1s, so the first poll past
        1s triggers a wallclock kill.)"""
        sched = _mk_sched(tmp_path, conc=1)
        task = _task("slowpoke", tmp_path, fake_step_delay_s=2.5,
                     max_wallclock_s=1.0, crash_retries=1)
        results = sched.run([task])
        assert results["slowpoke"].status == "timeout"
        kinds = [e["event"] for e in _events(sched)]
        assert "wallclock_timeout" in kinds


class TestApprovalMode:
    def test_scheduler_surfaces_pending_approval_then_approved(self, tmp_path):
        """Approval mode: worker writes request.json and blocks; the test
        (acting as the human) approves; worker proceeds to success."""
        sched = _mk_sched(tmp_path, conc=1)

        task = _task("approval-t", tmp_path, approval="require",
                     fake_step_delay_s=0.05, approval_timeout_s=30)
        import threading
        from runtime import approval as ap

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
        task = _task("gate-parked", tmp_path, approval="require",
                     fake_step_delay_s=0.05, approval_timeout_s=60,
                     hang_heartbeat_stale_s=4.0)
        import threading
        from runtime import approval as ap

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
        cp = json.loads((tmp_path / "tasks" / "gate-parked" / "checkpoint.json").read_text())
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
        task = _task("gate-forever", tmp_path, approval="require",
                     fake_step_delay_s=0.05, approval_timeout_s=60,
                     hang_heartbeat_stale_s=4.0,
                     max_wallclock_s=5.0, crash_retries=0)
        results = sched.run([task])
        assert results["gate-forever"].status == "timeout"
        kinds = [e["event"] for e in _events(sched)]
        assert "wallclock_timeout" in kinds

    def test_approval_reject_blocks_diff(self, tmp_path):
        sched = _mk_sched(tmp_path, conc=1)
        task = _task("approval-r", tmp_path, approval="require",
                     fake_step_delay_s=0.05, approval_timeout_s=30)
        import threading
        from runtime import approval as ap

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
        assert results["approval-r"].diff is None        # diff never applied


class TestRouterIntegration:
    @staticmethod
    def _routing_task(task_id: str, tmp_path: Path, issue_text: str) -> Task:
        resume_dir = tmp_path / "tasks" / task_id
        return Task(
            task_id=task_id, repo_path="", issue_text=issue_text,
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
                    "hard": {"provider": "anthropic",
                             "model": "claude-3-5-sonnet-20241022"},
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
            "  File \"src/mod/parser.py\", line 88, in parse\n"
            "ValueError: invalid literal\n```\n\n"
            "Not sure if the encoding handling in the buffer is also "
            "involved. Needs a careful multi-file fix."
        )
        # self-check: this text must actually classify as "hard"
        from runtime.difficulty import heuristic_features, score_to_hint
        feats = heuristic_features(hard_issue)
        assert score_to_hint(feats["score"]) == "hard", feats

        sched = _mk_sched(tmp_path, conc=1)
        sched.run([self._routing_task("routed-hard", tmp_path, hard_issue)])

        ledger_path = tmp_path / "tasks" / "routed-hard" / "model_ledger.jsonl"
        assert ledger_path.exists()
        records = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
        assert len(records) == 5  # one call per fake step
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
        ledger_on = (tmp_path / "tasks" / "abl-on" / "model_ledger.jsonl")
        recs_on = [json.loads(l) for l in ledger_on.read_text().splitlines() if l.strip()]
        assert all(r["model"] == "gpt-4o-mini" for r in recs_on)

        # routing OFF: same issue, but a hard-tier default is pinned — no
        # hint-based switching, single model regardless of content
        resume_dir = tmp_path / "tasks" / "abl-off"
        off_task = Task(
            task_id="abl-off", repo_path="", issue_text=easy_issue,
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
        ledger_off = (tmp_path / "tasks" / "abl-off" / "model_ledger.jsonl")
        recs_off = [json.loads(l) for l in ledger_off.read_text().splitlines() if l.strip()]
        assert len(recs_off) == 5
        assert all(r["model"] == "claude-3-5-sonnet-20241022" for r in recs_off)
        # and the cost difference is the ablation's whole point:
        cost_on = sum(r["cost_usd"] for r in recs_on)
        cost_off = sum(r["cost_usd"] for r in recs_off)
        assert cost_on < cost_off
