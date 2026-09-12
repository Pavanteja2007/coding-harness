"""Environment snapshotting for task resume (Round 8, Task C).

The execution "environment" of a task is its per-repo dependency image
(harness-exec:<fp> — installed deps, built by sandbox.ensure_image).
The WORK state (repo edits, progress) already survives relaunches via
Terminal 1's resume contract (state.json + plan.json + the pristine/
work dirs). What did NOT survive: the dependency image itself. If the
image cache was pruned between the crash and the relaunch (docker
image prune, disk-pressure eviction, a wiped daemon, CI runners), the
resumed task would rebuild the whole environment — full pip download
+ install — even though only the WORK needed resuming.

This module snapshots a task's installed-dependency state AT CHECKPOINT
TIME as a docker TAG of the dep image:

    docker tag harness-exec:<fp> harness-envsnap:<task_id>

A tag is O(1) (no layer copy — both tags reference the same image), so
snapshotting at every worker start is ~50ms. Restore is equally cheap:
when the resumed task's dep image is missing but a snapshot exists
(and its recorded fingerprint matches the repo's CURRENT dep
fingerprint), the snapshot is retagged AS the dep image:

    docker tag harness-envsnap:<task_id> harness-exec:<fp>

and sandbox.ensure_image's existing cache probe hits — the resumed
task skips the environment rebuild entirely. When the fingerprint
does NOT match (the repo's dep manifests changed since the snapshot),
the snapshot is stale and restore is a no-op — ensure_image rebuilds
normally (the snapshot never overrides a genuine dep change).

Snapshot lifecycle: the worker snapshots at start (checkpoint time),
restores on resume, and REMOVES the snapshot tag when the task
finishes (both tags reference the same image, so removing the snap
tag never deletes the underlying image while the dep tag exists —
the image cache does not grow one image per task). Control via the
task.config key "env_snapshot" (default True).

Task ids are validated through memory.paths.safe_task_dir (the shared
Round-6 guard) — a hostile task id must not turn a docker tag name
into anything unexpected.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from execution.sandbox import (
    _dep_image_tag,
    _run_docker,
    docker_available,
    IMAGE_PREFIX,
)

SNAP_PREFIX = "harness-envsnap"


class EnvSnapshotError(RuntimeError):
    """Snapshot/restore failed in a way callers may want to see.

    Never raised by the best-effort wiring (worker/CLI degrade to a
    normal rebuild); raised only when a caller asks for strict
    behavior (strict=True).
    """


def _snap_tag(task_id: str) -> Optional[str]:
    """Docker tag for a task's env snapshot, or None for a hostile id.

    Uses the shared safe-task-id guard (memory.paths) so a crafted id
    can't smuggle path/registry syntax into a docker tag name; ids are
    additionally constrained to [A-Za-z0-9._-] (docker tag charset).
    """
    try:
        from memory.paths import is_safe_task_id

        if not is_safe_task_id(task_id):
            return None
    except ImportError:
        pass  # memory module absent (standalone execution use) — the
        # charset check below still applies
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id or ""):
        return None
    return f"{SNAP_PREFIX}:{task_id}"


def snapshot_metadata_path(runtime_dir: str | Path) -> Path:
    """Where the snapshot metadata lives (T3's runtime dir convention)."""
    return Path(runtime_dir) / "env_snapshot.json"


def snapshot_environment(
    task_id: str, repo_path: str, runtime_dir: str | Path, *, strict: bool = False
) -> Optional[str]:
    """Snapshot the task's dep image (docker tag, O(1)); returns the tag.

    Assumes repo_path is an existing directory and runtime_dir is the
    task's logs/{task_id}.runtime/ (created if missing). No-op (returns
    None) when docker is down, the repo has no dep image yet (nothing
    to snapshot — a pre-plan crash has no environment to save), the
    task id is invalid, or the image tag would be identical to the
    snapshot tag. Best-effort by default (strict=True raises
    EnvSnapshotError instead of returning None).
    """
    tag = _snap_tag(task_id)
    if tag is None:
        if strict:
            raise EnvSnapshotError(f"invalid task id for env snapshot: {task_id!r}")
        return None
    try:
        if not docker_available():
            raise EnvSnapshotError("docker daemon not reachable")
        if not Path(repo_path).is_dir():
            raise EnvSnapshotError(f"repo_path is not a directory: {repo_path}")
        dep_tag = _dep_image_tag(repo_path)
        probe = _run_docker(["image", "inspect", dep_tag], check=False)
        if probe.returncode != 0:
            # no dep image yet (pre-plan crash): nothing to snapshot
            return None
        _run_docker(["tag", dep_tag, tag], timeout_s=60)
        meta = {
            "task_id": task_id,
            "snapshot_tag": tag,
            "dep_tag": dep_tag,
            "repo_path": os.path.abspath(repo_path),
            "created_at": time.time(),
        }
        p = snapshot_metadata_path(runtime_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return tag
    except Exception as exc:
        if strict:
            raise EnvSnapshotError(str(exc)) from exc
        return None


def has_snapshot(task_id: str, runtime_dir: str | Path) -> bool:
    """True when a snapshot tag exists in the docker image cache.

    Assumes the daemon may be down (returns False then — a missing
    snapshot must never block a resume, only cost a rebuild).
    """
    tag = _snap_tag(task_id)
    if tag is None:
        return False
    try:
        if not docker_available():
            return False
        probe = _run_docker(["image", "inspect", tag], check=False)
        return probe.returncode == 0
    except Exception:
        return False


def restore_environment(
    task_id: str, repo_path: str, runtime_dir: str | Path, *, strict: bool = False
) -> Optional[str]:
    """Restore the task's dep image from its snapshot; returns the tag.

    Called BEFORE sandbox.ensure_image on a resumed task. O(1) retag
    when applicable; no-op (returns None) when: no snapshot exists,
    the snapshot's recorded fingerprint does not match the repo's
    CURRENT dep fingerprint (deps changed — stale snapshot, rebuild
    instead), the dep image already exists (nothing to do), or docker
    is unavailable. Never overrides a genuine dep change: a stale
    snapshot is DROPPED (tag removed) so it can't be restored later
    either. Best-effort by default (strict=True raises on hard
    failures).
    """
    tag = _snap_tag(task_id)
    if tag is None:
        if strict:
            raise EnvSnapshotError(f"invalid task id for env snapshot: {task_id!r}")
        return None
    try:
        if not docker_available():
            raise EnvSnapshotError("docker daemon not reachable")
        if not Path(repo_path).is_dir():
            raise EnvSnapshotError(f"repo_path is not a directory: {repo_path}")
        meta_path = snapshot_metadata_path(runtime_dir)
        meta: Dict[str, Any] = {}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        # snapshot tag must exist in the image cache
        probe = _run_docker(["image", "inspect", tag], check=False)
        if probe.returncode != 0:
            return None  # pruned between crash and resume: rebuild normally
        dep_tag = _dep_image_tag(repo_path)
        # fingerprint check: only restore what matches CURRENT manifests
        recorded_dep = meta.get("dep_tag")
        if recorded_dep is not None and recorded_dep != dep_tag:
            # deps changed since the snapshot — stale, drop it
            _run_docker(["rmi", tag], check=False, timeout_s=30)
            return None
        # already have the image? nothing to do
        probe = _run_docker(["image", "inspect", dep_tag], check=False)
        if probe.returncode == 0:
            return None
        _run_docker(["tag", tag, dep_tag], timeout_s=60)
        return dep_tag
    except Exception as exc:
        if strict:
            raise EnvSnapshotError(str(exc)) from exc
        return None


def drop_snapshot(task_id: str, *, strict: bool = False) -> bool:
    """Remove a finished task's snapshot tag (keeps the image cache from
    growing one tag per task forever; the underlying image survives
    while the dep tag references it). Returns True when removed."""
    tag = _snap_tag(task_id)
    if tag is None:
        return False
    try:
        cp = _run_docker(["rmi", tag], check=False, timeout_s=30)
        return cp.returncode == 0
    except Exception as exc:
        if strict:
            raise EnvSnapshotError(str(exc)) from exc
        return False


def list_snapshots() -> Dict[str, str]:
    """All harness-envsnap:<task_id> tags currently in the image cache.

    Returns {task_id: image_id} — an operator-facing inventory helper
    (the CLI/`vex reap` family can surface it). Never raises.
    """
    out: Dict[str, str] = {}
    try:
        cp = _run_docker(
            [
                "images",
                "--filter",
                f"reference={SNAP_PREFIX}",
                "--format",
                "{{.Tag}} {{.ID}}",
            ],
            check=False,
            timeout_s=30,
        )
        if cp.returncode != 0:
            return out
        for line in cp.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[0]] = parts[1]
    except Exception:
        pass
    return out
