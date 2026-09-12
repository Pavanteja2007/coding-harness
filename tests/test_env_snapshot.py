"""Tests for execution.env_snapshot (Round 8, Task C).

The environment snapshot system ties dep-image state into the existing
checkpoint/resume flow: a task's installed-dependency image is snapshotted
at checkpoint time (O(1) docker tag), restored at resume (O(1) retag)
when the image cache was pruned in between, and dropped at finish.

Unit tests cover tag validation and metadata handling without Docker;
Docker-gated integration tests cover the real snapshot/restore/drop
lifecycle including the stale-fingerprint (deps changed) refusal.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from execution import env_snapshot as envs
from execution import sandbox as sb


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_up(),
    reason="docker daemon not reachable",
)


def _mkrepo(tmp_path: Path) -> Path:
    (tmp_path / "mymod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("six\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Unit (no Docker)
# ---------------------------------------------------------------------------


class TestTagValidation:
    def test_normal_id_gets_tag(self):
        assert (
            envs._snap_tag("task-2026-09-10_a") == "harness-envsnap:task-2026-09-10_a"
        )

    def test_hostile_ids_rejected(self):
        # traversal / drive forms / registry syntax / empty / null byte —
        # the shared safe-task-id guard class (memory.paths), plus a
        # docker-tag-charset check for the standalone (no-memory) path
        for bad in (
            "../evil",
            "C:/x",
            "a/b",
            "a b",
            "..",
            "",
            "a:b",
            "a b",
            ".hidden",
            "-lead",
        ):
            assert envs._snap_tag(bad) is None, bad

    def test_snapshot_strict_raises_on_bad_id(self, tmp_path):
        with pytest.raises(envs.EnvSnapshotError):
            envs.snapshot_environment("../evil", str(tmp_path), tmp_path, strict=True)


class TestMetadata:
    def test_snapshot_metadata_path_layout(self):
        assert envs.snapshot_metadata_path(Path("logs/t1.runtime")) == Path(
            "logs/t1.runtime/env_snapshot.json"
        )


# ---------------------------------------------------------------------------
# Integration (Docker)
# ---------------------------------------------------------------------------


@requires_docker
class TestEnvSnapshotLifecycle:
    def setup_method(self):
        # a fresh repo + built dep image per test
        import tempfile

        self.repo = Path(tempfile.mkdtemp(prefix="envsnap-"))
        _mkrepo(self.repo)
        self.rt = self.repo.parent / (self.repo.name + ".runtime")
        self.tid = "envsnap-test-" + self.repo.name
        sb.ensure_image(str(self.repo))
        # make sure no stale snapshot leaks between tests
        envs.drop_snapshot(self.tid)

    def teardown_method(self):
        envs.drop_snapshot(self.tid)
        import shutil

        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.rt, ignore_errors=True)

    def test_snapshot_restore_roundtrip(self):
        dep = sb._dep_image_tag(str(self.repo))
        tag = envs.snapshot_environment(self.tid, str(self.repo), self.rt)
        assert tag == f"harness-envsnap:{self.tid}"
        assert envs.has_snapshot(self.tid, self.rt)
        # metadata recorded for the fingerprint check
        meta = json.loads((self.rt / "env_snapshot.json").read_text(encoding="utf-8"))
        assert meta["dep_tag"] == dep

        # simulate the crash-then-prune-then-resume sequence
        subprocess.run(["docker", "rmi", dep], capture_output=True, timeout=60)
        probe = subprocess.run(
            ["docker", "image", "inspect", dep], capture_output=True, timeout=30
        )
        assert probe.returncode != 0, "prune simulation failed"

        t0 = time.perf_counter()
        restored = envs.restore_environment(self.tid, str(self.repo), self.rt)
        dt = time.perf_counter() - t0
        assert restored == dep
        # restore is an O(1) retag, not a rebuild
        assert dt < 5.0, f"restore took {dt:.2f}s — not an O(1) retag?"
        # and ensure_image's cache probe now hits (no rebuild)
        tag2 = sb.ensure_image(str(self.repo))
        assert tag2 == dep

    def test_restore_noop_when_image_exists(self):
        dep = sb._dep_image_tag(str(self.repo))
        envs.snapshot_environment(self.tid, str(self.repo), self.rt)
        # image still cached: restore is a no-op (nothing to do)
        out = envs.restore_environment(self.tid, str(self.repo), self.rt)
        assert out is None
        assert (
            subprocess.run(
                ["docker", "image", "inspect", dep], capture_output=True, timeout=30
            ).returncode
            == 0
        )

    def test_stale_snapshot_not_restored_when_deps_changed(self):
        envs.snapshot_environment(self.tid, str(self.repo), self.rt)
        # change the dep manifests -> new fingerprint
        (self.repo / "requirements.txt").write_text("six\npyyaml\n", encoding="utf-8")
        new_dep = sb._dep_image_tag(str(self.repo))
        assert (
            new_dep
            != json.loads((self.rt / "env_snapshot.json").read_text(encoding="utf-8"))[
                "dep_tag"
            ]
        )
        # simulate prune of BOTH old and new images
        subprocess.run(["docker", "rmi", new_dep], capture_output=True, timeout=60)
        out = envs.restore_environment(self.tid, str(self.repo), self.rt)
        assert out is None, "stale snapshot restored — deps changed!"
        # stale snapshot is DROPPED so it can't be restored later either
        assert not envs.has_snapshot(self.tid, self.rt)

    def test_drop_removes_tag_keeps_image(self):
        dep = sb._dep_image_tag(str(self.repo))
        envs.snapshot_environment(self.tid, str(self.repo), self.rt)
        assert envs.drop_snapshot(self.tid) is True
        assert not envs.has_snapshot(self.tid, self.rt)
        # dep image itself survives (the tag removal never deletes it)
        assert (
            subprocess.run(
                ["docker", "image", "inspect", dep], capture_output=True, timeout=30
            ).returncode
            == 0
        )

    def test_snapshot_without_dep_image_is_noop(self, tmp_path):
        # a repo whose dep image does not exist yet (pre-plan crash
        # shape — image never built) — nothing to snapshot, no error.
        # NOTE: a no-manifest repo still HAS a fingerprint (deps over
        # just the base tag) whose image may exist from other repos —
        # remove it so this test sees the genuinely-image-less shape.
        empty = tmp_path / "norepo"
        empty.mkdir()
        (empty / "x.py").write_text("x = 1\n", encoding="utf-8")
        dep = sb._dep_image_tag(str(empty))
        subprocess.run(["docker", "rmi", dep], capture_output=True, timeout=60)
        out = envs.snapshot_environment(self.tid, str(empty), self.rt)
        assert out is None

    def test_list_snapshots_includes_new_snapshots(self):
        envs.snapshot_environment(self.tid, str(self.repo), self.rt)
        assert self.tid in envs.list_snapshots()
