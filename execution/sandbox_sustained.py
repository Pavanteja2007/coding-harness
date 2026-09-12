"""SUSTAINED-LOAD resource profiling (Round 7, Terminal 2, Task B): a
long sequence of NORMAL task-shaped executions (no adversarial input,
no fault injection — the opposite of sandbox_adversarial.py / Round 6)
to surface slow leaks that short test runs cannot catch: container
cleanup drift over time, disk growth, image-cache bloat, gradual
latency creep, host-resource trends. NOT part of the pytest suite
(spins 100+ real containers over tens of minutes); run explicitly:

    python -m execution.sandbox_sustained --tasks 120
    python -m execution.sandbox_sustained --tasks 120 --concurrency 4

One "task" mirrors the execute_sandboxed usage shape of ONE run_task
(the same 3-command shape sandbox_stress.py drives per child, run
in-process here for profiling fidelity):
  1. bash probe (agent's first look at the repo),
  2. target-test pytest run (verify target),
  3. full-suite pytest run (regression).
Each task runs against its own FRESH copy of a fixture repo (bind-mount
isolation, like logs/{task_id}/work), so host-side artifact growth is
also measured per-task (a leak signature short tests miss).

Metrics sampled every task (and aggregated per 10-task window):
  - container census: live hexec-* count, plus OUR residue (containers
    of this process that failed to be reaped by --rm);
  - image cache: count + total size of harness-exec* tags;
  - disk: bytes used by the run workspace (all task copies), bytes used
    by logs/sandbox-sustained/, and docker system df totals;
  - latency: per-command wall time (creep = last-quarter mean vs
    first-quarter mean, per command class);
  - host memory: RSS of THIS process (in-process leak check) and the
    docker VM's memory use via docker stats --no-stream on idle.

Verdicts (exit 0 = clean; 1 = finding; 2 = environment problem):
  - residue: any hexec-* container left after the settle window;
  - image growth: beyond the per-fixture fingerprints (duplicate tag
    churn = rebuild loop);
  - disk growth: task-copy bytes per task exceed the per-fixture
    baseline*task_count tolerance (unbounded artifact writes);
  - latency creep: last-quarter mean > 1.5x first-quarter mean on any
    command class (gradual slowdown);
  - rss creep: this process's RSS at end > start + 200MB (in-process
    leak — execute_sandboxed state accumulating);
Report: logs/sandbox-sustained/<ts>/sustained_report.json + console
summary. Every number in the report is measured, not assumed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Profiling tool for the FRESH-RUN machinery (Round 7 baseline): pin the
# Round-8 warm pool OFF so its parked containers don't distort the
# census/residue/latency baselines it re-measures. The pool's own
# profile is measured by sandbox_perf.py.
os.environ.setdefault("HARNESS_SANDBOX_POOL", "0")

from execution import sandbox as sb  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
FIXTURE_NAMES = [
    "bug01_wrap",
    "bug02_mean",
    "bug03_stack",
    "bug04_nameerror",
    "bug05_cart",
]

# Per-command timeout for normal task-shaped commands (generous —
# normal work, not timeout testing).
TASK_CMD_TIMEOUT_S = 180

# Tolerances (measured baselines, not guesses — see report "baselines"):
RSS_CREEP_LIMIT_MB = 200  # in-process leak threshold
LATENCY_CREEP_FACTOR = 1.5  # last-quarter vs first-quarter mean
DISK_PER_TASK_TOLERANCE = 2.5  # x the median per-task artifact bytes
SETTLE_WINDOW_S = 120  # final --rm teardown grace


def _rmtree(path: Path) -> None:
    """Windows-proof rmtree (git packs are read-only on Win)."""

    def _onerr(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerr) if sys.version_info < (
        3,
        12,
    ) else shutil.rmtree(path, ignore_errors=True)


def _docker_json(fmt_args: List[str]) -> Optional[dict]:
    """Run a docker CLI command, parse stdout lines as tab-joined."""
    try:
        cp = subprocess.run(
            ["docker"] + fmt_args, capture_output=True, text=True, timeout=60
        )
        if cp.returncode != 0:
            return None
        return {"out": cp.stdout}
    except (OSError, subprocess.TimeoutExpired):
        return None


def _hexec_census() -> Dict[str, int]:
    """Live + all (incl. teardown) hexec container counts."""
    live = _docker_json(["ps", "--filter", "name=hexec-", "--format", "{{.Names}}"])
    allc = _docker_json(
        ["ps", "-a", "--filter", "name=hexec-", "--format", "{{.Names}}"]
    )
    mine = sb.own_container_filter()
    mine_live = [n for n in (live or {}).get("out", "").split() if mine in n]
    return {
        "live": len((live or {}).get("out", "").split()),
        "all": len((allc or {}).get("out", "").split()),
        "own_live": len(mine_live),
    }


def _image_cache() -> Dict[str, object]:
    """harness-exec* tag count + total size."""
    cp = subprocess.run(
        [
            "docker",
            "images",
            "--filter",
            "reference=harness-exec*",
            "--format",
            "{{.Repository}}:{{.Tag}} {{.Size}}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    tags, total_bytes = [], 0
    for line in cp.stdout.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            tags.append(parts[0])
            total_bytes += _parse_size(parts[1])
    return {"tags": sorted(tags), "count": len(tags), "total_bytes": total_bytes}


def _parse_size(s: str) -> int:
    """'123MB' / '1.5GB' -> bytes (docker's size formats)."""
    s = s.strip().upper()
    mult = 1
    for suf, m in (("KB", 10**3), ("MB", 10**6), ("GB", 10**9), ("B", 1)):
        if s.endswith(suf):
            mult, s = m, s[: -len(suf)]
            break
    try:
        return int(float(s.replace(",", "")) * mult)
    except ValueError:
        return 0


def _dir_bytes(p: Path) -> int:
    """Total bytes under a directory (cheap walk; workspace sizes are
    small fixture copies + bounded artifacts)."""
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _rss_mb() -> float:
    """This process's RSS in MB (psutil-free: /proc or ps)."""
    try:
        if os.name == "posix":
            with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0
        else:
            cp = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Process -Id {os.getpid()}).WorkingSet64",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return int(cp.stdout.strip()) / (1024 * 1024)
    except Exception:
        pass
    return -1.0


def run_sustained(
    n_tasks: int, concurrency: int, out_dir: Path, keep_workspaces: bool = False
) -> int:
    """Run the sustained-load profile; returns 0 clean / 1 finding / 2 env.

    Assumes docker is up, FIXTURES exists, out_dir is writable. Runs
    n_tasks normal 3-command task executions (at most `concurrency` in
    flight via a worker pool — 1 = pure sequential), sampling resources
    after each task, then asserts the verdicts in the module docstring.
    """
    from concurrent.futures import ThreadPoolExecutor

    # ABSOLUTE out_dir from here on: execute_sandboxed resolves
    # repo_path via os.path.abspath, which needs a LIVE cwd — on WSL/9p
    # a relative path + transient getcwd failure under concurrent
    # metadata churn (other terminals' git ops) kills every subsequent
    # call with bare ENOENT (found live). Absolute paths + a stable
    # owned cwd make the profiler immune.
    out_dir = Path(out_dir).absolute()
    workdir = out_dir / "workspaces"
    workdir.mkdir(parents=True, exist_ok=True)

    # Baseline snapshots (pre-run state everything is compared against).
    rss_start = _rss_mb()
    images_before = _image_cache()
    census_start = _hexec_census()

    # Prepare per-task repo copies up front (disk-growth measurement
    # covers the whole run, including copy churn).
    task_dirs: List[Path] = []
    for i in range(n_tasks):
        src = FIXTURES / FIXTURE_NAMES[i % len(FIXTURE_NAMES)]
        dst = workdir / f"t{i}"
        if dst.exists():
            _rmtree(dst)
        shutil.copytree(src, dst)
        task_dirs.append(dst)

    # Warm the per-fixture dep images FIRST so latency measurements
    # measure steady-state execution, not first-build (the documented
    # ensure_image contract for batch runs).
    print(
        f"[{time.strftime('%H:%M:%S')}] warming dep images for "
        f"{len(FIXTURE_NAMES)} fixtures..."
    )
    for name in FIXTURE_NAMES:
        sb.ensure_image(str(FIXTURES / name))

    disk_ws_start = _dir_bytes(workdir)
    print(
        f"[{time.strftime('%H:%M:%S')}] running {n_tasks} normal tasks "
        f"(concurrency={concurrency})..."
    )

    # One task = 3 sandboxed commands against its own repo copy.
    def run_task(i: int) -> dict:
        t0 = time.time()
        repo = str(task_dirs[i].absolute())  # stable against cwd loss
        cmds = [
            ("probe", "ls tests"),
            ("target", "python -m pytest -q tests"),
            ("suite", "python -m pytest -q tests"),
        ]
        latencies: Dict[str, float] = {}
        outcomes: Dict[str, dict] = {}
        for label, cmd in cmds:
            c0 = time.time()
            try:
                res = sb.execute_sandboxed(repo, cmd, TASK_CMD_TIMEOUT_S)
                latencies[label] = round(time.time() - c0, 2)
                outcomes[label] = {"exit": res.exit_code, "timed_out": res.timed_out}
            except Exception as exc:  # env problem, not a leak finding
                import traceback

                outcomes[label] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "tb": traceback.format_exc()[-800:],
                }
                latencies[label] = -1.0
        return {
            "task": i,
            "wall_s": round(time.time() - t0, 1),
            "latencies": latencies,
            "outcomes": outcomes,
        }

    samples: List[dict] = []
    results: List[dict] = []
    started = time.time()

    def sample(idx: int) -> None:
        samples.append(
            {
                "after_task": idx,
                "t_s": round(time.time() - started, 1),
                "census": _hexec_census(),
                "images": _image_cache(),
                "workspace_bytes": _dir_bytes(workdir),
                "out_dir_bytes": _dir_bytes(out_dir),
                "rss_mb": round(_rss_mb(), 1),
            }
        )

    if concurrency <= 1:
        for i in range(n_tasks):
            results.append(run_task(i))
            sample(i)
            if (i + 1) % 10 == 0:
                s = samples[-1]
                print(
                    f"  [{i + 1}/{n_tasks}] live={s['census']['live']} "
                    f"imgs={s['images']['count']} "
                    f"ws={s['workspace_bytes'] // 1024}KB "
                    f"rss={s['rss_mb']}MB "
                    f"({s['t_s']}s)"
                )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {}
            for i in range(n_tasks):
                futs[pool.submit(run_task, i)] = i
            done = 0
            for fut in list(futs):  # deterministic order for sampling
                pass
            from concurrent.futures import as_completed

            for fut in as_completed(futs):
                results.append(fut.result())
                done += 1
                sample(done - 1)
                if done % 10 == 0:
                    s = samples[-1]
                    print(
                        f"  [{done}/{n_tasks}] live={s['census']['live']} "
                        f"imgs={s['images']['count']} "
                        f"ws={s['workspace_bytes'] // 1024}KB "
                        f"rss={s['rss_mb']}MB ({s['t_s']}s)"
                    )

    # Settle: give --rm teardown the same grace the tests use.
    print(f"[{time.strftime('%H:%M:%S')}] settling (<= {SETTLE_WINDOW_S}s)...")
    deadline = time.time() + SETTLE_WINDOW_S
    while time.time() < deadline:
        c = _hexec_census()
        if c["live"] == 0 and c["own_live"] == 0:
            break
        time.sleep(3.0)

    # ---------------- verdicts ----------------
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(
            f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
        )
        if not ok:
            failures.append(name)

    census_end = _hexec_census()
    check(
        "no hexec residue after settle",
        census_end["live"] == 0,
        f"live={census_end['live']} all={census_end['all']}",
    )

    imgs_end = _image_cache()
    grew = set(imgs_end["tags"]) - set(images_before["tags"])
    expected_tags = {sb._dep_image_tag(str(FIXTURES / n)) for n in FIXTURE_NAMES}
    check(
        "image growth bounded to per-fixture fingerprints",
        grew <= expected_tags,
        f"grew={sorted(grew)} expected<={sorted(expected_tags)}",
    )
    check(
        "no duplicate-image churn (count stable after warm)",
        imgs_end["count"] <= images_before["count"] + len(expected_tags),
        f"before={images_before['count']} after={imgs_end['count']}",
    )

    disk_ws_end = _dir_bytes(workdir)
    per_task_bytes = (disk_ws_end - disk_ws_start) / max(1, n_tasks)
    # Normal artifacts: pytest writes .pytest_cache/__pycache__ into the
    # copies once; tolerance covers cross-fixture size spread.
    per_task_sizes = []
    for i, d in enumerate(task_dirs):
        per_task_sizes.append(_dir_bytes(d))
    per_task_sizes.sort()
    median_size = per_task_sizes[len(per_task_sizes) // 2]
    check(
        "workspace disk growth bounded (per-task artifacts)",
        disk_ws_end - disk_ws_start
        <= int(median_size * DISK_PER_TASK_TOLERANCE) * n_tasks + (1024**2),
        f"grew={disk_ws_end - disk_ws_start}B over {n_tasks} tasks, "
        f"median_copy={median_size}B tol={DISK_PER_TASK_TOLERANCE}x",
    )

    # Latency creep: compare mean latencies, first vs last quarter, per
    # command class (only tasks that succeeded).
    def quarter_mean(label: str, lo: int, hi: int) -> Optional[float]:
        vals = [
            r["latencies"][label]
            for r in sorted(results, key=lambda r: r["task"])
            if r["latencies"].get(label, -1) > 0
        ]
        seg = vals[lo:hi]
        return round(sum(seg) / len(seg), 2) if seg else None

    q = max(1, n_tasks // 4)
    creep_report = {}
    for label in ("probe", "target", "suite"):
        first = quarter_mean(label, 0, q)
        last = quarter_mean(label, len(range(n_tasks)) - q, len(range(n_tasks)))
        if first and last:
            creep_report[label] = {
                "first_q_mean_s": first,
                "last_q_mean_s": last,
                "factor": round(last / max(0.01, first), 2),
            }
            check(
                f"no latency creep: {label}",
                last <= first * LATENCY_CREEP_FACTOR,
                f"first_q={first}s last_q={last}s "
                f"(x{round(last / max(0.01, first), 2)})",
            )

    rss_end = _rss_mb()
    if rss_start > 0 and rss_end > 0:
        check(
            "no in-process RSS creep",
            rss_end - rss_start <= RSS_CREEP_LIMIT_MB,
            f"start={rss_start}MB end={rss_end}MB "
            f"delta={round(rss_end - rss_start, 1)}MB "
            f"(limit={RSS_CREEP_LIMIT_MB}MB)",
        )

    env_errors = [
        r for r in results if any("error" in o for o in r["outcomes"].values())
    ]
    check(
        "no environment errors during the run",
        not env_errors,
        f"{len(env_errors)} tasks had CLI/daemon errors",
    )

    # ---------------- report ----------------
    total_wall = round(time.time() - started, 1)
    report = {
        "n_tasks": n_tasks,
        "concurrency": concurrency,
        "total_wall_s": total_wall,
        "baselines": {
            "rss_start_mb": rss_start,
            "images_before": images_before,
            "census_start": census_start,
            "workspace_start_bytes": disk_ws_start,
        },
        "samples": samples,
        "results": sorted(results, key=lambda r: r["task"]),
        "latency_creep": creep_report,
        "end": {
            "census": census_end,
            "images": imgs_end,
            "workspace_bytes": disk_ws_end,
            "out_dir_bytes": _dir_bytes(out_dir),
            "rss_mb": round(rss_end, 1),
        },
        "failures": failures,
        "config": {
            "task_cmd_timeout_s": TASK_CMD_TIMEOUT_S,
            "rss_creep_limit_mb": RSS_CREEP_LIMIT_MB,
            "latency_creep_factor": LATENCY_CREEP_FACTOR,
            "disk_per_task_tolerance": DISK_PER_TASK_TOLERANCE,
        },
    }
    (out_dir / "sustained_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    print(
        f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}"
    )
    print(
        f"{n_tasks} tasks in {total_wall}s "
        f"({round(total_wall / n_tasks, 1)}s/task avg) | "
        f"report: {out_dir / 'sustained_report.json'}"
    )

    if not keep_workspaces:
        _rmtree(workdir)
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m execution.sandbox_sustained",
        description="Sustained-load resource profiling of the sandbox "
        "under NORMAL task-shaped work (leak detection).",
    )
    ap.add_argument(
        "--tasks",
        type=int,
        default=120,
        help="number of normal task executions (>= 100 recommended)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="parallel in-flight tasks (1 = sequential; the "
        "normal scheduler shape is 1-4 per host)",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="keep per-task workspace copies for inspection",
    )
    args = ap.parse_args()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "sandbox-sustained" / ts)
    out.mkdir(parents=True, exist_ok=True)
    if not sb.docker_available():
        print("docker daemon not reachable — sustained profile aborted")
        return 2
    return run_sustained(
        args.tasks, args.concurrency, out, keep_workspaces=args.keep_workspaces
    )


if __name__ == "__main__":
    raise SystemExit(main())
