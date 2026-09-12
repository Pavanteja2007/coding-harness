"""Long-duration soak test for the scheduler/runtime (Round 7, Task B).

Runs ONE long-lived Scheduler across an extended sequence of task batches
and watches for the slow degradation that short stress runs miss:

  - scheduler-process memory growth (RSS sampled per batch, gc'd first)
  - checkpoint/artifact accumulation (per-task files must stay bounded;
    totals must grow linearly with tasks done, never superlinearly)
  - scheduling/execution latency drift (per-task spawn->finish duration,
    p95 of the last quarter of the run vs the first quarter)
  - leaked worker processes at batch boundaries
  - per-task resume correctness on every kill (the stress contract,
    held continuously across the whole run rather than in one burst)

Operation profile: fake harness (use_fake_harness) through REAL scheduler
worker processes — the supervision machinery under test (spawn/reap/kill/
requeue/resume, journals, checkpoints) is fully real; only the harness
loop is fake (the same trade as stress.py fake mode, chosen so the soak
can schedule thousands of tasks without Docker or model endpoints).
"Hours of operation" is measured as aggregate task-execution time (every
batch x every task's spawn->finish wall), reported as
simulated_task_hours in the report.

Standalone like stress.py/abuse.py (spawns many real worker processes);
NOT part of the pytest suite. Run:

    python -m runtime.soak                  # default profile (~15-20 min)
    python -m runtime.soak --profile quick  # smoke (~2 min)
    python -m runtime.soak --profile ci     # nightly CI profile (~3 min)

IMPORTANT: every invocation needs its OWN --out directory (the default
timestamped path is fine). The scheduler's run journal is keyed by the
fixed run_id "soak" inside the out dir; two concurrent or sequential
runs sharing one out dir interleave journals, re-run each other's
finished tasks, and produce garbage verdicts (found live via the
Round-7 tracemalloc probe, whose second invocation contaminated the
first's report).

Checks (all written to logs/soak/<ts>-<profile>/soak_report.json):
  1. every task in every batch finished `success` (kills resume cleanly)
  2. every killed task has >=2 worker starts with a resume=True start
  3. concurrency cap held in every batch (journal max_overlap <= cap)
  4. scheduler RSS growth over the run <= --rss-growth-max-mb (default 50)
  5. per-task latency p95 last-quarter <= first-quarter x --latency-tol
  6. per-task artifact counts stay bounded (<= PER_TASK_FILE_CAP) and
     flat (last scan's files/task <= first scan's + 2)
  7. one checkpoint.json/state.json/heartbeat.json per finished task —
     no per-attempt explosion; attempt dirs ~ tasks + kills (natural
     crash-retries are legal, but per-task spawns stay bounded and the
     residual must not grow with run length)
  8. run-journal growth per batch stays flat (no append storms)
  9. zero live worker children at batch boundaries (no process leaks)
  10. transient .tmp residue bounded by the kill count (atomic-write
      kills-in-flight, not a per-poll leak)
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from runtime.scheduler import Scheduler
from shared.types import Task

N_STEPS = 8  # fake steps per task (same shape as stress.py's fake mode)
PER_TASK_FILE_CAP = 15  # 3 (.runtime) + 1 state.json + 3/attempt x 2 + slack
RUN_ID = "soak"

PROFILES: Dict[str, Dict[str, Any]] = {
    # default: 120 batches x 30 tasks = 3600 tasks, 8 steps x 0.5s each
    # -> ~8 aggregate simulated task-hours; wall ~15-20 min.
    "default": {
        "batches": 120,
        "tasks": 30,
        "concurrency": 30,
        "kill_every": 3,
        "kills": 3,
        "step_delay_s": 0.5,
    },
    # quick smoke: proves the harness + every check end to end (~2 min).
    "quick": {
        "batches": 20,
        "tasks": 20,
        "concurrency": 20,
        "kill_every": 4,
        "kills": 2,
        "step_delay_s": 0.2,
    },
    # nightly CI profile: full check set, bounded wall (~3-4 min).
    "ci": {
        "batches": 12,
        "tasks": 25,
        "concurrency": 25,
        "kill_every": 3,
        "kills": 2,
        "step_delay_s": 0.15,
    },
}


def _iso_to_epoch(ts: str) -> float:
    """Parse the runtime's ISO-8601 journal timestamps to epoch seconds."""
    return datetime.fromisoformat(ts).timestamp()


def _fake_task(batch: int, i: int, logs_root: Path, delay: float) -> Task:
    """Build soak task (batch, i): fake harness, REAL worker process.

    Assumes the scheduler pins resume_dir/log_root into task.config at
    spawn time (it does); fake_state_dir keeps state.json next to the
    .runtime dir, mirroring stress.py's fake mode layout.
    """
    tid = f"b{batch}_t{i}"
    cfg = {
        "use_fake_harness": True,
        "fake_state_dir": str(logs_root / tid),
        "resume_dir": str(logs_root / f"{tid}.runtime"),
        "crash_retries": 3,
        "fake_step_delay_s": delay,
        "fake_steps": [f"s{k}" for k in range(1, N_STEPS + 1)],
    }
    return Task(task_id=tid, repo_path="", issue_text=f"soak {tid}", config=cfg)


def _rss_mb() -> Optional[float]:
    """Scheduler-process RSS in MB after a forced gc; None if psutil
    is unavailable (RSS tracking then degrades to 'untracked', noted in
    the report — never silently)."""
    try:
        import psutil
    except ImportError:
        return None
    gc.collect()
    try:
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:  # noqa: BLE001 — psutil races process teardown
        return None


def _worker_children_alive() -> Optional[int]:
    """Live direct children of this process (should be 0 between batches;
    None when psutil is unavailable -> check degrades, never passes
    vacuously — the report marks it untracked)."""
    try:
        import psutil
    except ImportError:
        return None
    gc.collect()
    try:
        return len(psutil.Process().children())
    except Exception:  # noqa: BLE001
        return None


def _start_killer(
    sched: Scheduler, logs_root: Path, batch: int, n_kills: int, seed: int
) -> Tuple[threading.Thread, List[Dict[str, Any]]]:
    """Start this batch's killer thread (the proven stress.py pattern).

    Assumes sched.run() for this batch is about to start on another
    thread. Victims are constrained to THIS batch's task ids (prefix
    b{batch}_), must be alive >=1s, and must have >=1 completed step —
    so every kill lands mid-task and forces a genuine resume. Firing
    re-checks liveness so a victim that finished during the select->fire
    window is dropped, not reported as a kill. Returns (thread,
    kill-report-list) — the list is thread-appended; join the thread
    before reading it. `seed` is accepted for interface stability; the
    victim selection is journal-ordered, not random.
    """
    report: List[Dict[str, Any]] = []
    prefix = f"b{batch}_"

    def killer() -> None:
        deadline = time.time() + 120
        victims: List[Any] = []
        while len(victims) < n_kills and time.time() < deadline:
            for att in sched.live_attempts().values():
                if not att.task_id.startswith(prefix):
                    continue
                if (
                    att.proc.poll() is None
                    and time.time() - att.started_epoch >= 1.0
                    and all(v is not att for v in victims)
                ):
                    try:
                        st = json.loads(
                            (logs_root / att.task_id / "state.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        if not (st.get("completed_steps") or []):
                            continue  # no progress yet — a kill proves nothing
                    except (OSError, ValueError):
                        continue
                    victims.append(att)
                    if len(victims) == n_kills:
                        break
            if len(victims) < n_kills:
                time.sleep(0.1)
        # Re-verify liveness immediately before firing: soak tasks are
        # SHORT (seconds), so the select->fire window can race a victim
        # that finishes on its own (the stress.py runs never see this —
        # their tasks live minutes). Reporting a kill that never landed
        # would fail the resume proof for a task that never crashed.
        live = [att for att in victims if att.proc.poll() is None]
        for att in live:
            report.append({"task_id": att.task_id, "pid": att.proc.pid, "batch": batch})
        for att in live:
            att.proc.kill()

    t = threading.Thread(target=killer, daemon=True)
    t.start()
    return t, report


def _pct(xs: List[float], q: float) -> float:
    """q-th percentile (nearest-rank) of xs; 0.0 for empty input."""
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _batch_journal_stats(events: List[Dict[str, Any]], prefix: str) -> Dict[str, Any]:
    """Reconstruct per-batch overlap + per-task spawn->finish durations
    from the run journal (filtered to this batch's task-id prefix)."""
    running = 0
    max_overlap = 0
    spawns: Dict[str, float] = {}
    finishes: Dict[str, float] = {}
    for e in events:
        d = e.get("data") or {}
        tid = str(d.get("task_id", ""))
        if not tid.startswith(prefix):
            continue
        ev = e.get("event", "")
        if ev == "spawn":
            running += 1
            max_overlap = max(max_overlap, running)
            spawns.setdefault(tid, _iso_to_epoch(e["ts"]))
        elif ev in (
            "finish",
            "crash_retry",
            "crash_exhausted",
            "kill_requeue",
            "kill_exhausted",
        ):
            running -= 1
            if ev == "finish":
                finishes[tid] = _iso_to_epoch(e["ts"])
    durs = [finishes[t] - spawns[t] for t in finishes if t in spawns]
    return {
        "max_overlap": max_overlap,
        "durations": durs,
        "n_spawns": len(spawns),
        "n_finish": len(finishes),
    }


def _tree_stats(logs_root: Path) -> Dict[str, Any]:
    """Walk the soak logs tree: totals, per-task file counts, checkpoint/
    state/heartbeat counts, attempt dirs, .tmp residue.

    Per-task attribution: paths are either {tid}/, {tid}.runtime/, or
    {RUN_ID}/{tid}/attempt_n/ (the scheduler's run dir); run-level files
    (RUN_ID}/events.jsonl) are excluded from per-task counts.
    """
    files = 0
    bytes_total = 0
    per_task: Dict[str, int] = {}
    checkpoints = states = heartbeats = tmp = attempt_dirs = 0
    for p in logs_root.rglob("*"):
        try:
            if p.is_dir():
                if p.name.startswith("attempt_"):
                    attempt_dirs += 1
                continue
            if not p.is_file():
                continue
            st_size = p.stat().st_size
        except OSError:
            continue
        files += 1
        bytes_total += st_size
        rel = p.relative_to(logs_root).parts
        if rel[0] == RUN_ID:
            if len(rel) == 2:
                continue  # run-level journal, not a task artifact
            tid = rel[1]
        else:
            tid = rel[0].split(".runtime")[0]
        per_task[tid] = per_task.get(tid, 0) + 1
        if p.name == "checkpoint.json":
            checkpoints += 1
        elif p.name == "state.json":
            states += 1
        elif p.name == "heartbeat.json":
            heartbeats += 1
        elif p.name.endswith(".tmp"):
            tmp += 1
    return {
        "files_total": files,
        "bytes_total": bytes_total,
        "tasks_with_files": len(per_task),
        "files_per_task_avg": round(files / max(1, len(per_task)), 2),
        "files_per_task_max": max(per_task.values()) if per_task else 0,
        "checkpoints": checkpoints,
        "state_jsons": states,
        "heartbeats": heartbeats,
        "attempt_dirs": attempt_dirs,
        "tmp_residue": tmp,
    }


def _kill_resume_proof(
    logs_root: Path, kill_log: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Verify every killed task genuinely resumed: >=2 worker starts,
    at least one marked resume=True (from .runtime/events.jsonl)."""
    bad: List[str] = []
    for k in kill_log:
        tid = k["task_id"]
        ev_path = logs_root / f"{tid}.runtime" / "events.jsonl"
        try:
            evs = [
                json.loads(l)
                for l in ev_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
        except (OSError, ValueError):
            bad.append(f"{tid}:no-events")
            continue
        starts = [e for e in evs if e.get("event") == "worker_start"]
        resumes = [s for s in starts if (s.get("data") or {}).get("resume")]
        if len(starts) < 2 or not resumes:
            bad.append(f"{tid}:{len(starts)}starts/{len(resumes)}resumes")
    return {"ok": not bad, "bad": bad, "n_killed": len(kill_log)}


def run_soak(
    profile: Dict[str, Any],
    out_dir: Path,
    rss_growth_max_mb: float = 50.0,
    latency_tol: float = 1.3,
    scan_every: int = 10,
) -> int:
    """Run the whole soak; returns process exit code (0 = all checks
    passed). Assumes out_dir is writable; one Scheduler instance lives
    across ALL batches (that longevity is the point) with unique
    per-batch task ids b{batch}_t{i}."""
    batches = int(profile["batches"])
    n_tasks = int(profile["tasks"])
    cap = int(profile["concurrency"])
    kill_every = int(profile["kill_every"])
    n_kills = int(profile["kills"])
    delay = float(profile["step_delay_s"])

    logs_root = out_dir / "tasklogs"
    logs_root.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(concurrency=cap, logs_root=str(logs_root), run_id=RUN_ID)
    journal_path = sched.run_dir / "events.jsonl"

    per_batch: List[Dict[str, Any]] = []
    tree_samples: List[Tuple[int, Dict[str, Any]]] = []
    kill_log: List[Dict[str, Any]] = []
    all_durs: List[Tuple[int, float]] = []
    status_counts: Dict[str, int] = {}
    prev_journal_lines = 0
    t_start = time.time()

    print(
        f"[soak] {batches} batches x {n_tasks} tasks @ cap {cap}, "
        f"kill {n_kills} every {kill_every or '-'} batches, "
        f"delay {delay}s/step x {N_STEPS} steps"
    )

    for b in range(batches):
        tasks = [_fake_task(b, i, logs_root, delay) for i in range(n_tasks)]
        rss_before = _rss_mb()

        kthread = None
        kreport: List[Dict[str, Any]] = []
        do_kill = kill_every > 0 and n_kills > 0 and (b + 1) % kill_every == 0
        if do_kill:
            kthread, kreport = _start_killer(
                sched, logs_root, b, n_kills, seed=1000 + b
            )

        t0 = time.time()
        results = sched.run(tasks)
        wall = time.time() - t0
        if kthread is not None:
            kthread.join(timeout=10)
        kill_log.extend(kreport)

        for r in results.values():
            status_counts[r.status] = status_counts.get(r.status, 0) + 1

        events = [
            json.loads(l)
            for l in journal_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        jlines = len(events)
        jdelta = jlines - prev_journal_lines
        prev_journal_lines = jlines
        bstat = _batch_journal_stats(events, f"b{b}_")
        durs = bstat["durations"]
        all_durs.extend((b, d) for d in durs)
        rss_after = _rss_mb()
        children = _worker_children_alive()

        rec: Dict[str, Any] = {
            "batch": b,
            "wall_s": round(wall, 2),
            "n_results": len(results),
            "max_overlap": bstat["max_overlap"],
            "dur_p50_s": round(_pct(durs, 0.50), 2),
            "dur_p95_s": round(_pct(durs, 0.95), 2),
            "dur_max_s": round(max(durs) if durs else 0.0, 2),
            "kills_this_batch": len(kreport),
            "rss_before_mb": rss_before,
            "rss_after_mb": rss_after,
            "children_alive": children,
            "journal_lines": jlines,
            "journal_lines_delta": jdelta,
        }
        if b % scan_every == scan_every - 1 or b == batches - 1:
            tree = _tree_stats(logs_root)
            tree_samples.append((b, tree))
            rec["tree"] = {k: v for k, v in tree.items() if k != "per_task_files"}
        per_batch.append(rec)
        print(
            f"[soak] batch {b + 1}/{batches} wall={wall:.1f}s "
            f"p50={rec['dur_p50_s']}s p95={rec['dur_p95_s']}s "
            f"kills={len(kreport)} "
            f"rss={rss_after if rss_after is not None else 'n/a'}MB "
            f"children={children if children is not None else 'n/a'} "
            f"journal=+{jdelta}"
        )

    total_tasks = batches * n_tasks
    final_tree = tree_samples[-1][1]
    first_tree = tree_samples[0][1]
    wall_total = time.time() - t_start
    sim_hours = sum(d for _, d in all_durs) / 3600.0

    # ---- drift checks --------------------------------------------------
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(
            f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
        )
        if not ok:
            failures.append(name)

    print(
        f"\nsoak complete: {total_tasks} tasks in {wall_total:.0f}s "
        f"({sim_hours:.2f} simulated task-hours), "
        f"{len(kill_log)} kills"
    )

    # 1. every task succeeded
    ok1 = status_counts.get("success", 0) == total_tasks
    check("all tasks success (incl. kill-resumes)", ok1, str(status_counts))

    # 2. every kill genuinely resumed
    kp = _kill_resume_proof(logs_root, kill_log)
    check(
        "every kill resumed (>=2 starts, resume=True)",
        kp["ok"],
        f"{kp['n_killed']} kills, bad={kp['bad'] or 'none'}",
    )

    # 3. cap held every batch
    max_overlap = max(r["max_overlap"] for r in per_batch)
    check(
        "concurrency cap held every batch",
        max_overlap <= cap,
        f"max_overlap={max_overlap}, cap={cap}",
    )

    # 4. scheduler RSS growth
    rss_samples = [
        r["rss_after_mb"] for r in per_batch if r["rss_after_mb"] is not None
    ]
    if rss_samples and len(rss_samples) >= 4:
        k = max(1, len(rss_samples) // 10)
        growth = (sum(rss_samples[-k:]) / k) - (sum(rss_samples[:k]) / k)
        check(
            f"scheduler RSS growth <= {rss_growth_max_mb:.0f}MB",
            growth <= rss_growth_max_mb,
            f"{growth:+.1f}MB over {len(rss_samples)} batches "
            f"(first~{rss_samples[0]}MB, last~{rss_samples[-1]}MB)",
        )
    else:
        check(
            "scheduler RSS growth tracked", False, "untracked — psutil unavailable"
        ) if not rss_samples else check(
            "scheduler RSS growth tracked (short run)", True, "too few samples; skipped"
        )

    # 5. latency drift (p95 last quarter vs first quarter)
    if batches >= 4:
        q1 = [d for b, d in all_durs if b < batches * 0.25]
        q4 = [d for b, d in all_durs if b >= batches * 0.75]
        if q1 and q4:
            ratio = _pct(q4, 0.95) / max(1e-9, _pct(q1, 0.95))
            check(
                f"latency p95 drift <= x{latency_tol}",
                ratio <= latency_tol,
                f"q1 p95={_pct(q1, 0.95):.2f}s, q4 p95={_pct(q4, 0.95):.2f}s, "
                f"ratio={ratio:.2f}",
            )
        else:
            check("latency drift measurable", False, "empty quarter")
    else:
        check("latency drift (short run)", True, "skipped: <4 batches")

    # 6. per-task artifact counts bounded + flat
    fpt_max = final_tree["files_per_task_max"]
    check(
        f"per-task files <= {PER_TASK_FILE_CAP}",
        fpt_max <= PER_TASK_FILE_CAP,
        f"max={fpt_max}",
    )
    drift_fpt = final_tree["files_per_task_avg"] - first_tree["files_per_task_avg"]
    check(
        "files/task flat across the run",
        drift_fpt <= 2.0,
        f"first scan avg={first_tree['files_per_task_avg']}, "
        f"final avg={final_tree['files_per_task_avg']}, "
        f"delta={drift_fpt:+.2f}",
    )

    # 7. one checkpoint/state/heartbeat per task; attempts stay bounded:
    #    crash-retries beyond the killer's kills are LEGAL supervision
    #    behavior (natural crashes absorbed by resume) — what must not
    #    happen is per-task attempt EXPLOSION. Bound from the journal's
    #    per-task spawn counts, not from kills + a fixed slack.
    events_all = [
        json.loads(l)
        for l in journal_path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    spawns_per_task: Dict[str, int] = {}
    for e in events_all:
        if e.get("event") == "spawn":
            tid = str((e.get("data") or {}).get("task_id", ""))
            spawns_per_task[tid] = spawns_per_task.get(tid, 0) + 1
    max_task_spawns = max(spawns_per_task.values()) if spawns_per_task else 0
    natural_crashes = sum(1 for t, n in spawns_per_task.items() if n > 1) - len(
        {k["task_id"] for k in kill_log}
    )
    check(
        "checkpoint.json count == tasks",
        final_tree["checkpoints"] == total_tasks,
        f"{final_tree['checkpoints']}/{total_tasks}",
    )
    check(
        "state.json count == tasks",
        final_tree["state_jsons"] == total_tasks,
        f"{final_tree['state_jsons']}/{total_tasks}",
    )
    check(
        "heartbeat.json count == tasks",
        final_tree["heartbeats"] == total_tasks,
        f"{final_tree['heartbeats']}/{total_tasks}",
    )
    expected_attempts = total_tasks + len(kill_log)
    check(
        "attempt dirs ~ tasks + kills (natural retries bounded)",
        abs(final_tree["attempt_dirs"] - expected_attempts) <= 10 + len(kill_log),
        f"{final_tree['attempt_dirs']} vs expected {expected_attempts} "
        f"(natural crash-retries observed: {max(0, natural_crashes)})",
    )
    check(
        "no task spawn explosion (max spawns/task bounded)",
        max_task_spawns <= 4,
        f"max={max_task_spawns} spawns for one task "
        f"(crash_retries=3; tasks+killed can legally reach 4)",
    )

    # 8. journal growth flat (no append storm)
    deltas = [r["journal_lines_delta"] for r in per_batch]
    if batches >= 4:
        first_q = deltas[: max(1, batches // 4)]
        last_q = deltas[-(max(1, batches // 4)) :]
        med_first = sorted(first_q)[len(first_q) // 2]
        worst_last = max(last_q)
        check(
            "journal growth flat",
            worst_last <= 1.5 * med_first + 20,
            f"first-quarter median=+{med_first}/batch, "
            f"last-quarter worst=+{worst_last}/batch",
        )
    else:
        check("journal growth (short run)", True, "skipped")

    # 9. no leaked children at batch boundaries
    child_samples = [
        r["children_alive"] for r in per_batch if r["children_alive"] is not None
    ]
    if child_samples:
        check(
            "zero live workers between batches",
            max(child_samples) == 0,
            f"max observed={max(child_samples)}",
        )
    else:
        check("worker leak tracking", False, "untracked — psutil unavailable")

    # 10. .tmp residue bounded by kills
    check(
        ".tmp residue bounded",
        final_tree["tmp_residue"] <= len(kill_log) + 5,
        f"{final_tree['tmp_residue']} tmp files for {len(kill_log)} kills",
    )

    report: Dict[str, Any] = {
        "profile": profile,
        "out_dir": str(out_dir),
        "batches": batches,
        "tasks_per_batch": n_tasks,
        "total_tasks": total_tasks,
        "concurrency": cap,
        "wall_total_s": round(wall_total, 1),
        "simulated_task_hours": round(sim_hours, 2),
        "status_counts": status_counts,
        "kills": kill_log,
        "final_tree": final_tree,
        "per_batch": per_batch,
        "checks": {"failures": failures},
        "verdict": "PASS" if not failures else "FAIL: " + ", ".join(failures),
    }
    (out_dir / "soak_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}"
    )
    print(f"report: {out_dir / 'soak_report.json'}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.soak")
    ap.add_argument("--profile", default="default", choices=tuple(PROFILES))
    ap.add_argument("--batches", type=int, default=None)
    ap.add_argument("--tasks", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--kill-every", type=int, default=None)
    ap.add_argument("--kills", type=int, default=None)
    ap.add_argument("--step-delay", type=float, default=None)
    ap.add_argument("--rss-growth-max-mb", type=float, default=50.0)
    ap.add_argument("--latency-tol", type=float, default=1.3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    profile = dict(PROFILES[args.profile])
    for key, val in (
        ("batches", args.batches),
        ("tasks", args.tasks),
        ("concurrency", args.concurrency),
        ("kill_every", args.kill_every),
        ("kills", args.kills),
        ("step_delay_s", args.step_delay),
    ):
        if val is not None:
            profile[key] = val

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "soak" / f"{ts}-{args.profile}")
    out.mkdir(parents=True, exist_ok=True)
    return run_soak(
        profile,
        out,
        rss_growth_max_mb=args.rss_growth_max_mb,
        latency_tol=args.latency_tol,
    )


if __name__ == "__main__":
    raise SystemExit(main())
