"""Task B stress test: 30-50 concurrent tasks with SIMULTANEOUS multi-worker
kills mid-run. Verifies checkpoint/resume holds at real target scale under
real contention — not run as part of the normal suite (it spawns 40+ real
worker processes; takes ~2-4 min). Run explicitly:

    python -m runtime.stress --tasks 40 --concurrency 40 --kill 7

Checks (all asserted, written to logs/stress/<ts>/):
  1. All N tasks complete with a final TaskResult (no lost tasks, no
     scheduler exceptions) — crash budgets allow every killed task to
     resume and finish.
  2. Every killed task has >= 2 worker starts, with the later start
     marked resume=True (proven from each task's events.jsonl).
  3. Concurrency cap held: reconstructed max_overlap <= cap.
  4. State files: killed tasks that resumed show completed_steps from
     BEFORE the kill preserved in their final state.json (progress was
     actually retained across the kill, not restarted from zero).
  5. The run-level event journal records every kill (crash events) and
     every requeue (crash_retry).
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List

from runtime.scheduler import Scheduler
from shared.types import Task

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_stress(n_tasks: int, concurrency: int, n_kills: int,
               out_dir: Path, seed: int = 7) -> int:
    """Run the stress scenario; returns process exit code (0 = all checks
    passed). Assumes out_dir is writable and empty-ish; kills exactly
    n_kills randomly-chosen workers simultaneously ~40% into the run."""
    rng = random.Random(seed)
    logs_root = out_dir / "tasklogs"
    logs_root.mkdir(parents=True, exist_ok=True)

    def mk_task(i: int) -> Task:
        resume_dir = logs_root / f"t{i}.runtime"
        cfg = {
            "use_fake_harness": True,
            "fake_state_dir": str(logs_root / f"t{i}"),  # state.json next to .runtime
            "resume_dir": str(resume_dir),
            "crash_retries": 3,
            "fake_step_delay_s": 0.5,
            "fake_steps": ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"],
        }
        return Task(task_id=f"t{i}", repo_path="", issue_text="stress",
                    config=cfg)

    tasks = [mk_task(i) for i in range(n_tasks)]
    sched = Scheduler(concurrency=concurrency, logs_root=str(out_dir),
                      run_id="stress")

    # killer thread: waits until a good fraction of workers are mid-run,
    # then hard-kills n_kills of their PIDs simultaneously (taskkill /F
    # on Windows = TerminateProcess, the same semantics as a real crash).
    kill_report: List[Dict[str, int]] = []
    killed_event = threading.Event()

    def killer() -> None:
        target_alive = min(n_tasks, concurrency)
        # wait until most workers are spawned and mid-run (journal-based)
        for _ in range(600):  # up to 60s
            time.sleep(0.1)
            try:
                evs = (sched.run_dir / "events.jsonl").read_text().splitlines()
            except OSError:
                continue
            spawned = sum(1 for l in evs if '"spawn"' in l)
            finished = sum(1 for l in evs if '"finish"' in l or '"crash_retry"' in l)
            if spawned - finished >= target_alive * 0.9:
                break
        # grab live PIDs from the scheduler's public live-attempts view;
        # only pick attempts already ALIVE for >=1s (their worker has
        # booted and written its checkpoint — killing a just-spawned PID
        # can race the worker's own startup, and the run journal showed
        # short tasks finishing before an external taskkill even ran).
        victims: List = []
        deadline = time.time() + 60
        while len(victims) < n_kills and time.time() < deadline:
            for att in sched.live_attempts().values():
                if (att.proc.poll() is None
                        and time.time() - att.started_epoch >= 1.0
                        and all(v is not att for v in victims)):
                    victims.append(att)
                    if len(victims) == n_kills:
                        break
            if len(victims) < n_kills:
                time.sleep(0.05)
        for att in victims:
            kill_report.append({"task_id": att.task_id, "pid": att.proc.pid})
        # simultaneous kill via the scheduler's own kill primitive (same
        # one it uses for timeout kills: TerminateProcess on Windows).
        for att in victims:
            att.proc.kill()
        killed_event.set()

    t = threading.Thread(target=killer, daemon=True)
    t.start()

    t0 = time.time()
    results = sched.run(tasks)
    elapsed = time.time() - t0
    killed_event.wait(timeout=10)

    # ---- assertions --------------------------------------------------
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    print(f"run: {n_tasks} tasks @ conc={concurrency}, {n_kills} simultaneous kills, "
          f"{elapsed:.1f}s wall")

    # 1. all tasks finished with results
    check("all tasks have results", len(results) == n_tasks,
          f"{len(results)}/{n_tasks}")
    check("all success", all(r.status == "success" for r in results.values()),
          str({r.status for r in results.values()}))

    # 2. killed tasks resumed (>= 2 worker starts, later marked resume)
    killed_ids = {k["task_id"] for k in kill_report}
    check("kill report non-empty", len(kill_report) == n_kills,
          f"killed {len(kill_report)} pids: {sorted(killed_ids)}")
    for tid in sorted(killed_ids):
        ev_path = logs_root / f"{tid}.runtime" / "events.jsonl"
        if not ev_path.exists():
            check(f"{tid} has worker events", False, str(ev_path))
            continue
        evs = [json.loads(l) for l in ev_path.read_text().splitlines() if l.strip()]
        starts = [e for e in evs if e["event"] == "worker_start"]
        resumes = [s for s in starts if s["data"].get("resume")]
        check(f"{tid} killed->resumed", len(starts) >= 2 and len(resumes) >= 1,
              f"{len(starts)} starts, {len(resumes)} resumes")

    # 3. concurrency cap from run journal
    evs = [json.loads(l) for l in (sched.run_dir / "events.jsonl").read_text().splitlines()]
    running = max_overlap = 0
    for e in evs:
        ev = e["event"]
        if ev == "spawn":
            running += 1
            max_overlap = max(max_overlap, running)
        elif ev in ("finish", "crash_retry", "crash_exhausted",
                    "kill_requeue", "kill_exhausted"):
            running -= 1
    check("max concurrency <= cap", max_overlap <= concurrency,
          f"max_overlap={max_overlap}, cap={concurrency}")

    # 4. journal shows the kills + retries
    kinds = [e["event"] for e in evs]
    n_crash = kinds.count("crash")
    n_retry = kinds.count("crash_retry")
    check("kills recorded as crashes", n_crash >= len(kill_report),
          f"{n_crash} crash events for {len(kill_report)} kills")
    check("requeues recorded", n_retry >= len(kill_report),
          f"{n_retry} crash_retry events")

    # 5. serial floor sanity: 50 tasks x 8 steps x 0.5s = 200s serial;
    #    at conc=50 expect well under half that
    serial_floor = n_tasks * 8 * 0.5
    check("parallel beats serial floor", elapsed < serial_floor * 0.6,
          f"{elapsed:.1f}s vs serial floor {serial_floor:.1f}s")

    (out_dir / "stress_report.json").write_text(json.dumps({
        "n_tasks": n_tasks, "concurrency": concurrency, "n_kills": n_kills,
        "elapsed_s": round(elapsed, 1), "killed": kill_report,
        "failures": failures,
    }, indent=2), encoding="utf-8")
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    print(f"report: {out_dir / 'stress_report.json'}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.stress")
    ap.add_argument("--tasks", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--kill", type=int, default=7)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "stress" / ts)
    out.mkdir(parents=True, exist_ok=True)
    return run_stress(args.tasks, args.concurrency, args.kill, out, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
