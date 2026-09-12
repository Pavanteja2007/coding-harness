"""Performance probe for the execution sandbox (Round 8): measured, not
assumed, before/after numbers for the three performance tasks.

    python -m execution.sandbox_perf --all
    python -m execution.sandbox_perf --task-a   # pool vs fresh-run latency
    python -m execution.sandbox_perf --task-b   # pip cache hit rate / rebuild
    python -m execution.sandbox_perf --task-c   # env snapshot vs rebuild

NOT part of the pytest suite — like sandbox_stress/adversarial/sustained,
this is a standalone measurement harness. Every number lands in
logs/sandbox-perf/<ts>/perf_report.json.

Methodology (each task measures its OWN baseline in the same process):
- Task A: one "task" = the 6-command sandbox shape of one run_task
  (agent bash + import probe + 4 verifies-ish commands) against one
  repo. BEFORE arm pins HARNESS_SANDBOX_POOL=0 (fresh docker run per
  command — the pre-Round-8 production path); AFTER arm uses the pool.
  Both arms run after a warm-up task so image builds / first-touch
  effects don't pollute; medians over 5 tasks per arm.
- Task B: builds a dep image for a repo with real wheels (six + pyyaml),
  then measures (1) COLD build (wheel cache + image deleted, builder's
  regular cache pruned), (2) WARM rebuild with the Round-8 pip cache
  mount (image deleted, regular layer cache pruned, cache mount kept),
  (3) the legacy --no-cache-dir path in the same state. Hit rate comes
  from pip's own "Using cached"/"Downloading" lines in build output.
- Task C: snapshots a task's dep image, prunes the image, then measures
  restore_environment (O(1) retag) vs ensure_image rebuild-from-scratch
  in the same pruned state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_CMDS = [
    "true",  # agent bash probe
    'python -c "import six"',  # import sanity
    "true",
    "true",
    "true",
    "true",  # verify-ish runs
]


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        **kw,
    )


def _mkrepo(path: Path, reqs: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "mymod.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    (path / "pyproject.toml").write_bytes(
        b'[tool.pytest.ini_options]\ntestpaths = ["."]\n'
    )
    (path / "requirements.txt").write_bytes(reqs.encode())
    return path


def _task_latency(repo: str) -> float:
    """Run one 6-command task shape; returns elapsed seconds."""
    sys.path.insert(0, str(REPO_ROOT))
    from execution.sandbox import execute_sandboxed

    t0 = time.perf_counter()
    for cmd in TASK_CMDS:
        res = execute_sandboxed(repo, cmd, 180)
        assert res.exit_code == 0, (cmd, res.stderr[-300:])
    return time.perf_counter() - t0


def _prune_regular_cache() -> None:
    # prune ONLY regular build cache — cache mounts (the Task-B win)
    # survive this, exactly like a real machine that prunes layers to
    # reclaim disk but keeps the pip wheel cache
    _run(["docker", "builder", "prune", "-f", "--filter", "type=regular"])


def measure_task_a(workdir: Path) -> Dict[str, object]:
    """Pool vs fresh-run per-task latency (same process, both arms)."""
    from execution import sandbox as sb

    repo = str(_mkrepo(workdir / "repoA", "six\n"))

    # build dep image once for both arms
    sb.ensure_image(repo)

    report: Dict[str, object] = {"task": "A: pre-warmed container pool"}
    # AFTER arm (pool on — the live default) with warm-up
    sb.pool_shutdown()
    _task_latency(repo)  # warm-up: daemon + first pool create
    pooled = [_task_latency(repo) for _ in range(5)]
    report["after_pool_warm_tasks_s"] = {
        "mean": round(statistics.mean(pooled), 3),
        "median": round(statistics.median(pooled), 3),
        "min": round(min(pooled), 3),
        "max": round(max(pooled), 3),
    }
    st = sb.pool_status()["stats"]  # read BEFORE shutdown resets counters
    report["after_pool_stats"] = st
    sb.pool_shutdown()

    # BEFORE arm: pool disabled in-process (same daemon, same images)
    prev = sb.POOL_ENABLED
    sb.POOL_ENABLED = False
    try:
        _task_latency(repo)  # warm-up
        fresh = [_task_latency(repo) for _ in range(5)]
    finally:
        sb.POOL_ENABLED = prev
    report["before_fresh_run_tasks_s"] = {
        "mean": round(statistics.mean(fresh), 3),
        "median": round(statistics.median(fresh), 3),
        "min": round(min(fresh), 3),
        "max": round(max(fresh), 3),
    }
    m_before = statistics.mean(fresh)
    m_after = statistics.mean(pooled)
    report["delta"] = {
        "speedup": round(m_before / m_after, 2),
        "saved_s_per_task": round(m_before - m_after, 3),
        "pct_faster": round(100 * (1 - m_after / m_before), 1),
    }
    return report


def measure_task_b(workdir: Path) -> Dict[str, object]:
    """Persistent pip WHEEL cache (host dir + offline install): hit rate
    + rebuild time, before/after.

    Uses numpy as the probe dependency: a single ~17MB wheel with real
    install time — the realistic shape where wheel caching pays (tiny
    pure-python wheels download so fast they mask the difference).

    Arms (all with the dep image deleted + regular layer cache pruned —
    the rebuild-after-prune / dep-change scenario):
    - cold: wheel cache EMPTY (first-ever build of this dep set);
    - after: wheel cache WARM — the Round-8 mechanism (pip download
      skips present files; the build installs fully offline via
      --no-index --find-links);
    - before: the LEGACY online build (the pre-Round-8 shape) in the
      same state.
    """
    from execution import sandbox as sb

    repo = Path(_mkrepo(workdir / "repoB", "numpy==2.2.6\n"))
    tag = sb._dep_image_tag(str(repo))
    # isolate the wheel cache for this probe (fresh = honest cold arm)
    cache_root = workdir / "_pipcache"
    orig_root = sb.PIP_CACHE_ROOT
    sb.PIP_CACHE_ROOT = str(cache_root)
    report: Dict[str, object] = {"task": "B: persistent pip wheel cache"}

    def rebuild(mode: str) -> Dict[str, object]:
        # delete the dep image + prune regular layers: pure rebuild
        _run(["docker", "rmi", tag])
        _prune_regular_cache()
        prev_mode = sb.PIP_CACHE_MODE
        sb.PIP_CACHE_MODE = mode
        try:
            t0 = time.perf_counter()
            stats = sb._populate_wheel_cache(str(repo)) if mode == "wheels" else {}
            tag_out = sb.ensure_image(str(repo), rebuild=True)
            dt = time.perf_counter() - t0
        finally:
            sb.PIP_CACHE_MODE = prev_mode
        assert tag_out == tag
        return {
            "seconds": round(dt, 1),
            "wheel_hits": stats.get("hit", 0),
            "wheel_downloads": stats.get("downloaded", 0),
        }

    try:
        # 1) COLD (wheel cache empty): the first-ever build of this dep set
        import shutil as _shutil

        if cache_root.exists():
            _shutil.rmtree(cache_root)
        report["cold_build"] = rebuild("wheels")
        # 2) AFTER: wheel cache warm, image + layer cache gone
        report["after_wheelcache_rebuild"] = rebuild("wheels")
        hits = report["after_wheelcache_rebuild"]["wheel_hits"]
        downs = report["after_wheelcache_rebuild"]["wheel_downloads"]
        wheels = hits + downs
        report["cache_hit_rate_pct"] = round(100 * hits / wheels, 1) if wheels else None
        # 3) BEFORE: legacy online build, same image/layer state
        report["before_legacy_rebuild"] = rebuild("off")
    finally:
        sb.PIP_CACHE_ROOT = orig_root
    before = report["before_legacy_rebuild"]["seconds"]
    after = report["after_wheelcache_rebuild"]["seconds"]
    cold = report["cold_build"]["seconds"]
    report["delta"] = {
        "saved_s_vs_legacy": round(before - after, 1),
        "speedup_vs_legacy": round(before / after, 2) if after else None,
        "saved_s_vs_cold": round(cold - after, 1),
        "speedup_vs_cold": round(cold / after, 2) if after else None,
        "pct_time_saved_vs_legacy": round(100 * (1 - after / before), 1)
        if before
        else None,
    }
    return report


def measure_task_c(workdir: Path) -> Dict[str, object]:
    """Env snapshot restore vs rebuild after an image-cache prune."""
    from execution import sandbox as sb
    from execution import env_snapshot as envs

    repo = _mkrepo(workdir / "repoC", "six\npyyaml\n")
    tag = sb._dep_image_tag(str(repo))
    rt = workdir / "repoC.runtime"
    tid = "perf-task-c"

    # ensure image + snapshot exist
    sb.ensure_image(str(repo))
    envs.drop_snapshot(tid)
    envs.snapshot_environment(tid, str(repo), rt)

    # restore path (AFTER): image pruned, snapshot present
    _run(["docker", "rmi", tag])
    _prune_regular_cache()
    t0 = time.perf_counter()
    restored = envs.restore_environment(tid, str(repo), rt)
    t_restore = time.perf_counter() - t0
    t0 = time.perf_counter()
    sb.ensure_image(str(repo))  # cache probe must hit now
    t_ensure_hit = time.perf_counter() - t0

    # rebuild path (BEFORE): image pruned, NO snapshot
    _run(["docker", "rmi", tag])
    _prune_regular_cache()
    t0 = time.perf_counter()
    sb.ensure_image(str(repo))
    t_rebuild = time.perf_counter() - t0

    envs.drop_snapshot(tid)
    report = {
        "task": "C: env snapshot restore vs rebuild after prune",
        "after_restore_s": round(t_restore + t_ensure_hit, 3),
        "restored_tag": restored,
        "before_rebuild_s": round(t_rebuild, 1),
        "delta": {
            "speedup": round(t_rebuild / (t_restore + t_ensure_hit), 1)
            if (t_restore + t_ensure_hit)
            else None,
            "saved_s": round(t_rebuild - (t_restore + t_ensure_hit), 1),
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m execution.sandbox_perf")
    parser.add_argument("--all", action="store_true", help="run all tasks")
    parser.add_argument("--task-a", action="store_true")
    parser.add_argument("--task-b", action="store_true")
    parser.add_argument("--task-c", action="store_true")
    parser.add_argument("--out", default=None, help="report directory")
    args = parser.parse_args()
    if not (args.all or args.task_a or args.task_b or args.task_c):
        args.all = True

    sys.path.insert(0, str(REPO_ROOT))
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else REPO_ROOT / "logs" / "sandbox-perf" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "work"
    workdir.mkdir(exist_ok=True)

    print(f"perf probe — report under {out_dir}")
    report: Dict[str, object] = {"ts": ts, "machine": _docker_version()}

    if args.all or args.task_a:
        print("\n[Task A] pool vs fresh-run latency ...")
        report["task_a"] = measure_task_a(workdir)
        print(json.dumps(report["task_a"], indent=2))
    if args.all or args.task_b:
        print("\n[Task B] pip cache hit rate / rebuild times ...")
        report["task_b"] = measure_task_b(workdir)
        print(json.dumps(report["task_b"], indent=2))
    if args.all or args.task_c:
        print("\n[Task C] env snapshot vs rebuild ...")
        report["task_c"] = measure_task_c(workdir)
        print(json.dumps(report["task_c"], indent=2))

    (out_dir / "perf_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(f"\nreport: {out_dir / 'perf_report.json'}")
    return 0


def _docker_version() -> str:
    try:
        cp = _run(
            [
                "docker",
                "version",
                "--format",
                "{{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})",
            ]
        )
        return cp.stdout.strip() if cp.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
