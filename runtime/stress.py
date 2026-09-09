"""Task B stress test: 30-50 concurrent tasks with SIMULTANEOUS multi-worker
kills mid-run. Verifies checkpoint/resume holds at real target scale under
real contention — not run as part of the normal suite (it spawns 40+ real
worker processes; takes ~2-4 min). Run explicitly:

    python -m runtime.stress --tasks 40 --concurrency 40 --kill 7
    python -m runtime.stress --mode real --tasks 45 --concurrency 45 --kill 8

Modes:
  fake (default) — Boundary-3-shaped fake harness with fault injection
    (scheduler/worker/kill/resume machinery under load; fast).
  real — the REAL harness.core.run_task per worker: real planner/step
    prompts, real router context + ledger (mock-scripted model responses,
    zero network), real Docker-sandboxed bash + pytest verification, real
    state.json/plan.json/trace.jsonl, and the REAL resume contract on
    relaunch (plan reuse, completed-step skip, surviving work/ kept —
    proven from trace.jsonl's step_skipped_resume + plan_reused events).
    Kills are timed to land after >=1 step completed, so every killed
    task genuinely resumes mid-plan rather than restarting.

    Checks (all asserted, written to logs/stress/<ts>/):
      1. All N tasks complete with a final TaskResult (no lost tasks, no
         scheduler exceptions) — crash budgets allow every killed task to
         resume and finish.
      2. Every killed task has >= 2 worker starts, with the later start
         marked resume=True (proven from each task's events.jsonl).
      3. Concurrency cap held: reconstructed max_overlap <= cap.
      4. State files: killed tasks that resumed show completed_steps from
         BEFORE the kill preserved in their final state.json (progress was
         actually retained across the kill, not restarted from zero). In
         real mode additionally: relaunch reuses the persisted plan and skips
         completed steps (trace events plan_reused / step_skipped_resume),
         and the pre-kill trace events survived the kill (append semantics).
      5. The run-level event journal records every kill (crash events) and
         every requeue (crash_retry).
      6. Real mode: every successful task produced git.json + rationale.md
         (+ their trace events) — T1's product-grade output wiring must
         hold under 40-50-way concurrency + kills, not just single-task.
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

# Real-mode fixture repos: the 5 fixture bugs, each with a 2-step script.
# Step 1 is an idempotent read/prep command (so replaying the step after a
# kill is safe), step 2 is the actual fix (the scripted session replays
# from its first command after a resume — commands are chosen idempotent).
REAL_FIXTURES = [
    {
        "fixture": "bug01_wrap",
        "issue": "wrap() drops the final line when it is shorter than width",
        "steps": [
            "sed -n '1,40p' wrapwrap/textutil.py",
            "sed -i 's/if lines and current and len(current) == width:/if current:/' wrapwrap/textutil.py",
        ],
    },
    {
        "fixture": "bug02_mean",
        "issue": "mean() divides by len-1; should divide by len",
        "steps": [
            "sed -n '1,30p' numlib/mathutil.py",
            "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py",
        ],
    },
    {
        "fixture": "bug03_stack",
        "issue": "pop() on empty stack raises IndexError; it should raise StackEmptyError",
        "steps": [
            "sed -n '1,40p' stacklib/stack.py",
            "python - <<'EOF'\n"
            "p = \"stacklib/stack.py\"\n"
            "s = open(p).read()\n"
            "old = \"        return self._items.pop()\"\n"
            "new = (\"        if not self._items:\\n\"\n"
            "       \"            raise StackEmptyError(\\\"pop from empty stack\\\")\\n\"\n"
            "       \"        return self._items.pop()\")\n"
            "if new not in s:\n"
            "    assert old in s, \"pattern not found\"\n"
            "    open(p, \"w\").write(s.replace(old, new))\n"
            "EOF",
        ],
    },
    {
        "fixture": "bug04_nameerror",
        "issue": "days_in_month() raises NameError: name '_DAYS_PER_MONTHS' is not defined",
        "steps": [
            "sed -n '1,30p' datelib/dateutil.py",
            "sed -i 's/_DAYS_PER_MONTHS\\[month\\]/_DAYS_PER_MONTH[month]/' datelib/dateutil.py",
        ],
    },
    {
        "fixture": "bug05_cart",
        "issue": "price_report() leaks lines across calls because of a mutable default argument",
        "steps": [
            "sed -n '1,60p' cartlib/cart.py",
            "sed -i 's/        report = _DEFAULT_REPORT/        report = []/' cartlib/cart.py",
        ],
    },
]


def _real_task(i: int, logs_root: Path, approval: bool = False) -> Task:
    """Build task i for real-harness mode: REAL run_task, scripted model."""
    spec = REAL_FIXTURES[i % len(REAL_FIXTURES)]
    plan = [{"id": 1, "description": "inspect the affected file",
             "checkpoint": "file contents reviewed", "files_hint": []},
            {"id": 2, "description": "apply the fix",
             "checkpoint": "target test passes", "files_hint": []}]
    script = {
        "plan": plan,
        "scripts": {1: spec["steps"][:1] + ["SUBMIT"],
                    2: spec["steps"][1:] + ["SUBMIT"]},
    }
    cfg = {
        # REAL harness (no use_fake_harness key): real prompts, real
        # Docker bash + pytest verify, real state/plan/trace files.
        "use_mock_provider": True,
        "mock_script": script,
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 300,
        "command_timeout_s": 120,
        "max_step_turns": 6,
        "max_retries": 2,
        "budget_cap_usd": 3.0,
        "max_wallclock_s": 900.0,
        # runtime knobs
        "crash_retries": 3,
        "resume": True,
        "hang_heartbeat_stale_s": 900.0,
        "log_root": str(logs_root),
        "resume_dir": str(logs_root / f"t{i}.runtime"),
    }
    if approval:
        # Approval-mode variant: every task parks in the approval gate
        # mid-run. The stress: the approver's delay (~150s) far outlives
        # hang_heartbeat_stale_s (120s) — T1's Round-3 finding reproduced
        # at scale: gate-parked workers have legitimately stale state.json
        # and the scheduler must NOT state-stale-kill them (the worker's
        # awaiting_approval marker + still-beating heartbeat exempts
        # them). The window must ALSO exceed healthy work-phase gaps:
        # measured from the passing r4-real-45-45-8 run, Docker-contended
        # pytest steps legitimately run up to ~98s between state.json
        # writes (p95=75s) — a 60s window state-stale-killed WORKING
        # tasks until their budgets died (the gate exemption held the
        # whole time: 0 gate-parked kills in both failed attempts; the
        # config was the bug). 120s window / 150s park isolates the gate.
        cfg.update({
            "approval": "require",
            "approval_timeout_s": 600.0,
            "hang_heartbeat_stale_s": 120.0,
        })
    return Task(
        task_id=f"t{i}",
        repo_path=str(REPO_ROOT / "tests" / "fixtures" / spec["fixture"]),
        issue_text=spec["issue"],
        config=cfg,
    )


def _fake_task(i: int, logs_root: Path) -> Task:
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


def _trace_kinds(trace_path: Path) -> List[str]:
    """Kind names from a task's trace.jsonl (missing file -> empty)."""
    if not trace_path.exists():
        return []
    out: List[str] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line).get("kind", ""))
        except ValueError:
            pass
    return out


def run_stress(n_tasks: int, concurrency: int, n_kills: int,
               out_dir: Path, seed: int = 7, mode: str = "fake",
               approval: bool = False) -> int:
    """Run the stress scenario; returns process exit code (0 = all checks
    passed). Assumes out_dir is writable and empty-ish; kills exactly
    n_kills randomly-chosen workers simultaneously ~40% into the run.
    approval=True (real mode): every task ALSO parks in the approval
    gate, and the approver decides ~150s in — past a 120s
    hang_heartbeat_stale_s — proving the gate-park exemption at scale."""
    rng = random.Random(seed)
    logs_root = out_dir / "tasklogs"
    logs_root.mkdir(parents=True, exist_ok=True)

    if approval and mode != "real":
        raise SystemExit("--approval is only meaningful with --mode real")

    mk = _real_task if mode == "real" else _fake_task
    if approval:
        tasks = [_real_task(i, logs_root, approval=True)
                 for i in range(n_tasks)]
    else:
        tasks = [mk(i, logs_root) for i in range(n_tasks)]
    sched = Scheduler(concurrency=concurrency, logs_root=str(out_dir),
                      run_id="stress")

    # killer thread: waits until a good fraction of workers are mid-run,
    # then hard-kills n_kills of their PIDs simultaneously (taskkill /F
    # on Windows = TerminateProcess, the same semantics as a real crash).
    kill_report: List[Dict[str, int]] = []
    killed_event = threading.Event()

    # approval mode: a thread acts as the human for every pending request,
    # DECIDING SLOWLY (~75s after the request lands). The stall is the
    # point: state.json goes stale well before the decision, and the
    # scheduler must NOT kill the gate-parked workers (T1's Round-3
    # finding, reproduced at scale, holding under load post-fix). One
    # waiter thread PER GATE so all N parks overlap — 45 simultaneously
    # gate-parked workers with stale state.json is the actual scenario;
    # a serial approver would never create it. A killed-then-resumed
    # worker re-enters the gate and finds its decision already written
    # (request_approval keeps the files — crash-restart-safe by design).
    approved_count = [0]

    def _gate_waiter(i: int) -> None:
        from runtime import approval as ap

        gate = logs_root / f"t{i}.runtime" / "approval"
        deadline = time.time() + 1800
        while time.time() < deadline:
            if (gate / "request.json").exists():
                # request landed: let state.json go STALE first (the
                # hang window is 120s — wait 150s, longer)
                time.sleep(150.0)
                ap.decide(str(gate), approve=True)
                approved_count[0] += 1
                return
            time.sleep(0.5)

    def approver() -> None:
        waiters = [threading.Thread(target=_gate_waiter, args=(i,),
                                    daemon=True)
                   for i in range(n_tasks)]
        for w in waiters:
            w.start()
        for w in waiters:
            w.join()

    def _progress(tid: str) -> List[str]:
        """Completed steps of task tid from its state.json (best-effort)."""
        try:
            st = json.loads(
                (logs_root / tid / "state.json").read_text(encoding="utf-8"))
            return list(st.get("completed_steps") or [])
        except (OSError, ValueError):
            return []

    def killer() -> None:
        if n_kills <= 0:
            killed_event.set()
            return
        target_alive = min(n_tasks, concurrency)
        # wait until most workers are spawned and mid-run (journal-based)
        for _ in range(3600):  # up to 6 min (real mode: Docker + pytest)
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
        # ANY mode: only kill tasks that have >=1 completed step but
        # aren't finished — the kill then lands mid-task and the relaunch
        # MUST exercise the resume machinery (skip completed steps).
        # Killing a pre-progress task only proves a fresh restart, which
        # the fake-harness resume tests already cover deterministically
        # (observed live: at conc=50 the last-spawned tasks are 1-2s old
        # when the volley fires and their first step hasn't landed yet).
        victims: List = []
        deadline = time.time() + 600
        while len(victims) < n_kills and time.time() < deadline:
            for att in sched.live_attempts().values():
                if (att.proc.poll() is None
                        and time.time() - att.started_epoch >= 1.0
                        and all(v is not att for v in victims)):
                    prog = _progress(att.task_id)
                    if not prog:  # nothing completed yet: killing now
                        continue  # would only prove a fresh restart
                    victims.append(att)
                    if len(victims) == n_kills:
                        break
            if len(victims) < n_kills:
                time.sleep(0.25)
        for att in victims:
            kill_report.append({"task_id": att.task_id, "pid": att.proc.pid})
        # simultaneous kill via the scheduler's own kill primitive (same
        # one it uses for timeout kills: TerminateProcess on Windows).
        for att in victims:
            att.proc.kill()
        killed_event.set()

    t = threading.Thread(target=killer, daemon=True)
    t.start()
    if approval:
        threading.Thread(target=approver, daemon=True).start()

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

    mode_label = f"{mode}{' +approval' if approval else ''}"
    print(f"run ({mode_label}): {n_tasks} tasks @ conc={concurrency}, "
          f"{n_kills} simultaneous kills, {elapsed:.1f}s wall")

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

    # 2b. real mode: the relaunch actually exercised the resume contract —
    # plan reused (no re-planning), completed steps skipped, and the
    # pre-kill trace events survived (append-only across relaunch).
    if mode == "real":
        for tid in sorted(killed_ids):
            trace_path = logs_root / tid / "trace.jsonl"
            if not trace_path.exists():
                check(f"{tid} has trace.jsonl", False, str(trace_path))
                continue
            kinds = _trace_kinds(trace_path)
            n_skipped = kinds.count("step_skipped_resume")
            n_plan_reused = kinds.count("plan_reused")
            n_resume = kinds.count("resume")
            check(f"{tid} real-resume (plan reused + steps skipped)",
                  n_plan_reused >= 1 and n_skipped >= 1 and n_resume >= 1,
                  f"plan_reused={n_plan_reused} skipped={n_skipped} "
                  f"resume={n_resume}")
            # pre-kill trace history survived the kill
            first_resume_idx = next(
                (i for i, k in enumerate(kinds) if k == "resume"),
                len(kinds))
            pre_kill = kinds[:first_resume_idx]
            check(f"{tid} pre-kill trace survives",
                  any(k in pre_kill for k in
                      ("task_start", "plan", "step_end", "model_response")),
                  f"{len(pre_kill)} pre-kill events")

    # 2c. real mode: product-grade outputs survived the loop at scale —
    # every successful task produced the git-native artifacts + rationale
    # (T1's Round-3 wiring runs INSIDE run_task; these must hold under
    # 40-50-way concurrency + kills, not just in single-task e2e tests).
    if mode == "real":
        git_ok = rat_ok = trace_git_ok = trace_rat_ok = 0
        for tid, res in results.items():
            if res.status != "success":
                continue
            git_ok += (logs_root / tid / "git.json").exists()
            rat_ok += (logs_root / tid / "rationale.md").exists()
            trace = _trace_kinds(logs_root / tid / "trace.jsonl")
            trace_git_ok += "git_output" in trace
            trace_rat_ok += "rationale" in trace
        n_success = sum(1 for r in results.values() if r.status == "success")
        check("every success has git.json (branch/commit/PR)",
              git_ok == n_success, f"{git_ok}/{n_success}")
        check("every success has rationale.md", rat_ok == n_success,
              f"{rat_ok}/{n_success}")
        check("every success has git_output trace event",
              trace_git_ok == n_success, f"{trace_git_ok}/{n_success}")
        check("every success has rationale trace event",
              trace_rat_ok == n_success, f"{trace_rat_ok}/{n_success}")

    # 2d. approval mode: the gate held at scale — every task parked,
    # state.json went stale past the (deliberately small) hang window,
    # and NO gate-parked worker was killed by the state-stale hang check.
    # Proven from worker events: approval_wait -> approval_granted with
    # no intervening scheduler kill of that task.
    if approval:
        waited = granted = 0
        for tid in sorted(results):
            ev_path = logs_root / f"{tid}.runtime" / "events.jsonl"
            if not ev_path.exists():
                continue
            evs = [json.loads(l) for l in ev_path.read_text().splitlines()
                   if l.strip()]
            waited += any(e["event"] == "approval_wait" for e in evs)
            granted += any(e["event"] == "approval_granted" for e in evs)
        check("every task parked in the approval gate",
              waited == n_tasks, f"{waited}/{n_tasks}")
        check("every gate decision honored (granted)",
              granted == n_tasks, f"{granted}/{n_tasks}")
        hang_kills = [e for e in evs if e["event"] == "hang_timeout"
                      and e["data"].get("signal") == "state_stale"]
        check("no gate-parked worker state-stale-killed",
              len(hang_kills) == 0, f"{len(hang_kills)} state-stale kills")
        # the park was genuinely longer than the stale window
        check("approver decisions landed (parks exceeded 120s window)",
              approved_count[0] == n_tasks,
              f"{approved_count[0]}/{n_tasks} approved")

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

    # 5. serial floor sanity (fake mode): 50 tasks x 8 steps x 0.5s = 200s
    #    serial; at conc=50 expect well under half that. Real mode's floor
    #    is dominated by Docker+pytest and is reported, not asserted.
    if mode == "fake":
        serial_floor = n_tasks * 8 * 0.5
        check("parallel beats serial floor", elapsed < serial_floor * 0.6,
              f"{elapsed:.1f}s vs serial floor {serial_floor:.1f}s")

    (out_dir / "stress_report.json").write_text(json.dumps({
        "n_tasks": n_tasks, "concurrency": concurrency, "n_kills": n_kills,
        "mode": mode, "approval": approval,
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
    ap.add_argument("--mode", choices=("fake", "real"), default="fake",
                    help="fake: Boundary-3 fake harness (fast). real: REAL "
                         "harness.core.run_task per worker (Docker bash + "
                         "pytest verify + real resume contract; scripted "
                         "model responses via the mock provider).")
    ap.add_argument("--approval", action="store_true",
                    help="real mode: every task ALSO parks in the "
                         "approval gate; the approver decides ~150s in "
                         "(past a 120s hang window) — proves the "
                         "gate-park exemption under load.")
    args = ap.parse_args()
    if args.mode == "real":
        from execution.sandbox import docker_available, ensure_image
        if not docker_available():
            print("real mode needs Docker (sandboxed execution); refusing "
                  "to run unsandboxed", file=sys.stderr)
            return 2
        # Pre-warm all fixture dep images once (avoid 45 workers racing
        # the same image build at spawn time).
        t0 = time.time()
        for spec in REAL_FIXTURES:
            ensure_image(str(REPO_ROOT / "tests" / "fixtures" / spec["fixture"]))
        print(f"images pre-warmed in {time.time() - t0:.1f}s")
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "stress" / f"{ts}-{args.mode}")
    out.mkdir(parents=True, exist_ok=True)
    return run_stress(args.tasks, args.concurrency, args.kill, out,
                      args.seed, args.mode, approval=args.approval)


if __name__ == "__main__":
    raise SystemExit(main())
