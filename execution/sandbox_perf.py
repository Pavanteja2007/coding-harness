"""Standalone execution performance probes using the current fresh-container API.

    python -m execution.sandbox_perf --all
    python -m execution.sandbox_perf --task-a
    python -m execution.sandbox_perf --task-b
    python -m execution.sandbox_perf --task-c

The historical container-pool and pip-cache probes were removed when those
APIs were removed; this driver reports the capabilities that exist instead of
referencing nonexistent attributes. Results are written under
``logs/sandbox-perf/<ts>/perf_report.json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

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
    """Measure repeated fresh-container task latency."""
    from execution import sandbox as sb

    repo = str(_mkrepo(workdir / "repoA", "six\n"))
    sb.ensure_image(repo)
    _task_latency(repo)
    samples = [_task_latency(repo) for _ in range(5)]
    return {
        "task": "A: fresh-container latency",
        "pool_supported": False,
        "samples_s": [round(value, 3) for value in samples],
        "mean_s": round(statistics.mean(samples), 3),
        "median_s": round(statistics.median(samples), 3),
    }


def measure_task_b(workdir: Path) -> Dict[str, object]:
    """Measure one real dependency-image rebuild after pruning its cache."""
    from execution import sandbox as sb

    repo = Path(_mkrepo(workdir / "repoB", "numpy==2.2.6\n"))
    tag = sb._dep_image_tag(str(repo))
    _run(["docker", "rmi", tag])
    _prune_regular_cache()
    started = time.perf_counter()
    rebuilt = sb.ensure_image(str(repo), rebuild=True)
    elapsed = time.perf_counter() - started
    return {
        "task": "B: dependency-image rebuild",
        "wheel_cache_supported": False,
        "tag": rebuilt,
        "seconds": round(elapsed, 1),
    }


def measure_task_c(workdir: Path) -> Dict[str, object]:
    """Env snapshot restore vs rebuild after an image-cache prune."""
    from execution import env_snapshot as envs
    from execution import sandbox as sb

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
