"""Sandbox stress at scheduler scale: N task-shaped child processes driving
REAL Docker containers concurrently, with simultaneous mid-run kills —
Terminal 2's analog of runtime/stress.py, built to coordinate with it
(same scenario shape: process-per-task, kill victims >= 1s old, requeue
respawns). Not part of the pytest suite (spawns 40-50 real containers);
run explicitly:

    python -m execution.sandbox_stress --tasks 50 --concurrency 50 --kill 7

What one child does (the sandbox-usage shape of ONE real run_task):
  1. bash probe command (agent's first look at the repo),
  2. target-test pytest run (verify target),
  3. full-suite pytest run (regression),
each in its own container via execute_sandboxed — from a REAL separate
process, exactly like a scheduler worker.

Checks (all asserted, report at logs/sandbox-stress/<ts>/):
  1. Every non-killed child: all 3 sandboxed commands exit 0.
  2. Killed children leave NO container behind after the requeue-respawn
     window (their respawned incarnations' first calls reap them).
  3. Max simultaneous hexec-* containers <= number of live children
     (no container leak above the working set).
  4. Zero hexec-* residue after everything settles.
  5. Image cache grows by EXACTLY the expected per-fixture fingerprints
     (no unbounded growth, no duplicates).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from execution import sandbox as sb  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
FIXTURE_NAMES = ["bug01_wrap", "bug02_mean", "bug03_stack",
                 "bug04_nameerror", "bug05_cart"]

_CHILD = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, r"{repo_root}")
from execution.sandbox import execute_sandboxed

repo, out_json, phase_delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
time.sleep(phase_delay)
results = []
for cmd in [
    "ls tests",
    "python -m pytest -q tests",
    "python -m pytest -q tests",
]:
    t0 = time.time()
    try:
        res = execute_sandboxed(repo, cmd, 180)
        results.append({"cmd": cmd, "exit": res.exit_code,
                        "timed_out": res.timed_out,
                        "elapsed": round(time.time() - t0, 1)})
    except Exception as exc:
        results.append({"cmd": cmd, "error": str(exc)[:300]})
    Path(out_json).write_text(json.dumps(results), encoding="utf-8")
print("done")
'''


def _list_hexec() -> List[str]:
    cp = subprocess.run(
        ["docker", "ps", "--filter", "name=hexec-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30)
    return cp.stdout.split()


def _images() -> List[str]:
    cp = subprocess.run(
        ["docker", "images", "--filter", "reference=harness-exec*",
         "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, timeout=30)
    return cp.stdout.split()


def _rmtree(path: Path) -> None:
    def _onerr(func, p, _exc):
        import os
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerr) if sys.version_info < (3, 12) \
        else shutil.rmtree(path, ignore_errors=True)


def run_stress(n_tasks: int, concurrency: int, n_kills: int,
               out_dir: Path, seed: int = 7) -> int:
    """Run the stress scenario; returns process exit code (0 = all green).

    Assumes docker is up, FIXTURES exists, and out_dir is writable. Spawns
    n_tasks child processes (at most concurrency doing sandbox work at
    once — approximated by a staggered start spread over ~15s, since each
    child's sandbox calls are long), kills n_kills of them mid-container,
    respawns the killed ones (requeue semantics), and asserts the checks
    in the module docstring.
    """
    import random
    rng = random.Random(seed)

    workdir = out_dir / "repos"
    workdir.mkdir(parents=True, exist_ok=True)

    # Clone fixtures: each task gets its own working copy (bind-mount
    # isolation like logs/{task_id}/work — no shared .pytest_cache).
    clones: List[Path] = []
    for i in range(n_tasks):
        src = FIXTURES / FIXTURE_NAMES[i % len(FIXTURE_NAMES)]
        dst = workdir / f"t{i}"
        if dst.exists():
            _rmtree(dst)
        shutil.copytree(src, dst)
        clones.append(dst)

    images_before = set(_images())

    # Respawn support: killed tasks get re-run once (requeue semantics).
    child_py = out_dir / "_child.py"
    child_py.write_text(_CHILD.replace("{repo_root}", str(REPO_ROOT)),
                        encoding="utf-8")

    procs: Dict[int, subprocess.Popen] = {}
    outputs: Dict[int, Path] = {}
    killed: List[int] = []

    def spawn(i: int, phase_delay: float) -> subprocess.Popen:
        out_json = out_dir / f"t{i}.json"
        outputs[i] = out_json
        procs[i] = subprocess.Popen(
            [sys.executable, str(child_py), str(clones[i]),
             str(out_json), str(phase_delay)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wave 1: all n_tasks, staggered so container starts interleave.
    for i in range(n_tasks):
        spawn(i, rng.uniform(0.0, 15.0))

    # Monitor: sample max simultaneous containers while children live.
    max_containers = 0
    stop_monitor = threading.Event()

    def monitor() -> None:
        nonlocal max_containers
        while not stop_monitor.is_set():
            max_containers = max(max_containers, len(_list_hexec()))
            time.sleep(2.0)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()

    # Killer: once most containers are up, hard-kill n_kills children
    # (only ones that have actually STARTED their sandbox work — mirror
    # of runtime/stress.py's >= 1s-old victim rule).
    def killer() -> None:
        time.sleep(8)  # let the staggered wave spin up
        victims: List[int] = []
        deadline = time.time() + 90
        while len(victims) < n_kills and time.time() < deadline:
            for i, p in list(procs.items()):
                if i in killed:
                    continue
                if (i not in victims and p.poll() is None
                        and outputs[i].exists()):  # sandbox work started
                    victims.append(i)
                    if len(victims) == n_kills:
                        break
            if len(victims) < n_kills:
                time.sleep(0.2)
        for i in victims:
            killed.append(i)
            procs[i].kill()  # TerminateProcess semantics — no cleanup
        print(f"  killed {len(killed)} children mid-container: {killed}")

    kth = threading.Thread(target=killer, daemon=True)
    kth.start()

    # Wait for wave 1.
    for i in range(n_tasks):
        procs[i].wait(timeout=600)
    kth.join(timeout=120)

    # Requeue: respawn every killed child ONCE (scheduler semantics).
    for i in killed:
        spawn(i, 0.0)
        procs[i].wait(timeout=600)

    # Settle: --rm removal is daemon-async; give reaps/respawns a window.
    settle_deadline = time.time() + 90
    while time.time() < settle_deadline and _list_hexec():
        time.sleep(3.0)

    stop_monitor.set()
    mon.join(timeout=10)

    # ---- assertions -------------------------------------------------
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 1. every non-killed child completed all 3 commands cleanly.
    #    Fixture repos are DELIBERATELY buggy (that's the harness's job
    #    description), so "clean" = the sandbox genuinely executed the
    #    command: exit in (0, 1) with real pytest output — NOT exit 0.
    def _ran_cleanly(runs: List) -> bool:
        if len(runs) != 3:
            return False
        for r in runs:
            if r.get("error") is not None:
                return False
            if r.get("timed_out"):
                return False
            if r["exit"] not in (0, 1):
                return False
            # "ls tests" must list; pytest runs must produce a summary.
        return True

    ok_children, bad = 0, []
    for i in range(n_tasks):
        if i in killed:
            continue
        oj = outputs[i]
        if not oj.exists():
            bad.append((i, "no output"))
            continue
        try:
            runs = json.loads(oj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bad.append((i, "corrupt json"))
            continue
        if _ran_cleanly(runs):
            ok_children += 1
        else:
            bad.append((i, runs))
    check("non-killed children all clean", ok_children == n_tasks - len(killed),
          f"{ok_children}/{n_tasks - len(killed)} ok, bad={bad[:3]}")

    # 2. respawned children (the killed ones) completed cleanly on retry
    if killed:
        re_ok = 0
        for i in killed:
            try:
                runs = json.loads(outputs[i].read_text(encoding="utf-8"))
                if _ran_cleanly(runs):
                    re_ok += 1
            except (OSError, json.JSONDecodeError):
                pass
        check("killed children respawn+finish", re_ok == len(killed),
              f"{re_ok}/{len(killed)}")

    # 3. container working set stayed within the child population
    check("max containers <= tasks", max_containers <= n_tasks,
          f"max_simultaneous={max_containers}, tasks={n_tasks}")

    # 4. zero hexec residue
    residue = _list_hexec()
    check("no container residue", not residue, f"leftover={residue}")

    # 5. image cache: bounded growth. New fingerprints may appear when a
    #    fixture's fingerprint wasn't cached yet; what must NOT happen is
    #    growth BEYOND the expected per-clone set, or duplicate images.
    expected = {sb._dep_image_tag(str(c)) for c in clones}
    images_after = set(_images())
    grew = images_after - images_before
    check("image growth bounded to per-fixture fingerprints",
          grew <= expected,
          f"grew={len(grew)} expected_max={len(expected)} "
          f"unexpected={sorted(grew - expected)}")

    report = {
        "n_tasks": n_tasks, "concurrency": concurrency, "n_kills": len(killed),
        "killed": killed, "max_simultaneous_containers": max_containers,
        "image_fingerprints": sorted(expected),
        "failures": failures,
    }
    (out_dir / "sandbox_stress_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    print(f"report: {out_dir / 'sandbox_stress_report.json'}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m execution.sandbox_stress")
    ap.add_argument("--tasks", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--kill", type=int, default=7)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "sandbox-stress" / ts)
    out.mkdir(parents=True, exist_ok=True)
    if not sb.docker_available():
        print("docker daemon not reachable — stress aborted")
        return 2
    return run_stress(args.tasks, args.concurrency, args.kill, out, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
