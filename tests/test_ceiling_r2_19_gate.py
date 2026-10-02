"""R2-19 gate tests: the reproducibility guard's own honesty.

A reproducibility gate is only worth running if it can FAIL. These tests are
named after the behaviours that make it fail closed:

- a source tree that moves during the lane is ``invalid``, not reproducible -
  even when the two artifacts would have matched;
- a build that fails, or a builder that cannot start, is ``blocked``;
- a stage that does not carry the live tree's bytes is refused;
- the two builds run in two INDEPENDENT read-only stages, so a shared mutable
  checkout cannot manufacture agreement;
- the sdist is compared after normalization, so gzip metadata is not mistaken
  for content;
- ``logs`` and other generated roots are never staged;
- the guard never invokes git, and a non-reproducible verdict is a non-zero exit.

The one real end-to-end test builds a throwaway project twice through the
actual builder. The rest pin the decision logic with a substituted builder so
the suite stays fast; the substituted builder still has to produce real files
for the comparison to be meaningful, so it is not a mock that can pass
vacuously.
"""

from __future__ import annotations

import gzip
import io
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import reproducibility_guard as guard

MINI_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "r219probe"
version = "0.0.1"
"""


def _mini_project(root: Path) -> Path:
    """Create a minimal buildable project at ``root`` and return the path.

    Assumes ``root`` exists. The package body is one module so the wheel has
    real content to differ over.
    """
    package = root / "r219probe"
    package.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(MINI_PYPROJECT, encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _make_tar_gz(path: Path, members: Dict[str, bytes], mtime: int) -> Path:
    """Write a ``.tar.gz`` with the given members and one gzip mtime.

    Assumes ``path``'s parent exists. Member order is insertion order, so two
    archives with the same members in a different order are a *different* raw
    archive and the *same* normalized one.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 1700000000
            archive.addfile(info, io.BytesIO(payload))
    with path.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=mtime) as gz:
            gz.write(raw.getvalue())
    return path


def _fake_build(
    dist_dir: Path,
    wheel_payload: bytes,
    sdist_members: Dict[str, bytes],
    *,
    returncode: int = 0,
    raw_sdist_mtime: int = 1700000000,
) -> Dict[str, Any]:
    """Write a wheel/sdist pair into ``dist_dir`` and describe it for _run_build.

    Assumes ``dist_dir`` will be created. Returns kwargs suitable for
    substituting :func:`scripts.reproducibility_guard._run_build`.
    ``raw_sdist_mtime`` lets a caller vary the archive metadata while keeping
    the member content identical.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel = dist_dir / "r219probe-0.0.1-py3-none-any.whl"
    wheel.write_bytes(wheel_payload)
    sdist = dist_dir / "r219probe-0.0.1.tar.gz"
    _make_tar_gz(sdist, sdist_members, raw_sdist_mtime)

    def _run(stage: Path, out: Path, epoch: int) -> guard.BuildOutcome:
        out.mkdir(parents=True, exist_ok=True)
        w = out / wheel.name
        s = out / sdist.name
        w.write_bytes(wheel_payload)
        _make_tar_gz(s, sdist_members, raw_sdist_mtime)
        outcome = guard.BuildOutcome(stage=str(stage), out_dir=str(out))
        outcome.returncode = returncode
        outcome.artifacts = {"wheel": str(w), "sdist": str(s)}
        if returncode != 0:
            outcome.error = f"python -m build exited {returncode}"
        return outcome

    return {"runner": _run}


class TestGuardRejectsAMovingSource:
    """A moving source is the failure mode this whole module exists for."""

    def test_a_source_that_moves_mid_lane_is_invalid_not_reproducible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editing the project between the two builds must void the comparison."""
        project = _mini_project(tmp_path / "project")
        work = tmp_path / "work"
        fake = _fake_build(work / "seed", b"wheel-bytes", {"pkg/a.py": b"x = 1\n"})
        runner = fake["runner"]
        original_source = (project / "r219probe" / "__init__.py").read_text(
            encoding="utf-8"
        )
        calls = {"n": 0}

        def _run(stage: Path, out: Path, epoch: int) -> guard.BuildOutcome:
            # Move the source exactly between the two builds.
            calls["n"] += 1
            if calls["n"] == 2:
                (project / "r219probe" / "__init__.py").write_text(
                    original_source + "MOVED = True\n", encoding="utf-8"
                )
            return runner(stage, out, epoch)

        monkeypatch.setattr(guard, "_run_build", _run)

        report = guard.guarded_two_build(project, work, source_date_epoch=1700000000)

        assert calls["n"] == 2, "the lane must build twice"
        assert report["source_stable"] is False
        assert report["reproducible"] is False
        assert report["verdict"] == guard.INVALID
        assert any("changed during the lane" in b for b in report["blockers"])

    def test_a_moving_source_is_invalid_even_when_both_artifacts_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identical bytes are NOT evidence when the source they came from moved."""
        project = _mini_project(tmp_path / "project")
        work = tmp_path / "work"
        fake = _fake_build(work / "seed", b"identical-wheel", {"pkg/a.py": b"x = 1\n"})
        runner = fake["runner"]
        seen = {"n": 0}

        def _run(stage: Path, out: Path, epoch: int) -> guard.BuildOutcome:
            seen["n"] += 1
            if seen["n"] == 2:
                (project / "newfile.py").write_text("ADDED = 1\n", encoding="utf-8")
            return runner(stage, out, epoch)

        monkeypatch.setattr(guard, "_run_build", _run)
        report = guard.guarded_two_build(project, work, source_date_epoch=1700000000)

        # The two builds produced byte-identical artifacts...
        assert report["builds"][0]["artifacts"] and report["builds"][1]["artifacts"]
        # ...and the verdict is still not reproducibility.
        assert report["verdict"] == guard.INVALID
        assert report["reproducible"] is False


class TestGuardRejectsAFailedBuild:
    """A build that did not complete is blocked, never reproducible."""

    def test_a_non_zero_build_exit_is_blocked(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = _mini_project(tmp_path / "project")
        work = tmp_path / "work"
        fake = _fake_build(work / "seed", b"w", {"a": b"b"}, returncode=1)
        monkeypatch.setattr(guard, "_run_build", fake["runner"])

        report = guard.guarded_two_build(project, work, source_date_epoch=1700000000)

        assert report["verdict"] == guard.BLOCKED
        assert report["reproducible"] is False
        assert any("exited 1" in b for b in report["blockers"])

    def test_a_builder_that_exits_zero_without_artifacts_is_blocked(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = _mini_project(tmp_path / "project")
        work = tmp_path / "work"

        def _run(stage: Path, out: Path, epoch: int) -> guard.BuildOutcome:
            out.mkdir(parents=True, exist_ok=True)
            return guard.BuildOutcome(stage=str(stage), out_dir=str(out), returncode=0)

        monkeypatch.setattr(guard, "_run_build", _run)
        report = guard.guarded_two_build(project, work, source_date_epoch=1700000000)

        assert report["verdict"] == guard.BLOCKED
        assert any("no wheel/sdist pair" in b for b in report["blockers"])

    def test_a_builder_that_cannot_start_is_blocked_with_its_own_reason(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An OSError at the subprocess boundary is reported, never raised."""
        stage = _mini_project(tmp_path / "project")

        def _explode(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no builder on this host")

        monkeypatch.setattr(guard.subprocess, "run", _explode)
        outcome = guard._run_build(stage, tmp_path / "dist", 1700000000)

        assert outcome.returncode is None
        assert outcome.built is False
        assert "no builder on this host" in outcome.error

    def test_a_build_timeout_is_blocked_rather_than_hanging_forever(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        stage = _mini_project(tmp_path / "project")

        def _timeout(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="python -m build", timeout=1)

        monkeypatch.setattr(guard.subprocess, "run", _timeout)
        outcome = guard._run_build(stage, tmp_path / "dist", 1700000000)

        assert outcome.returncode is None
        assert "3600s budget" in outcome.error


class TestStagesAreIndependentAndImmutable:
    """The comparison is only evidence if the two builds read the same bytes."""

    def test_two_stages_carry_identical_bytes_and_are_read_only(
        self, tmp_path: Path
    ) -> None:
        project = _mini_project(tmp_path / "project")
        first = guard.stage_sources(project, tmp_path / "a")
        second = guard.stage_sources(project, tmp_path / "b")

        assert first.error == "" and second.error == ""
        assert first.digest == second.digest
        assert first.files == second.files
        assert first.read_only is True and second.read_only is True
        sample = tmp_path / "a" / "r219probe" / "__init__.py"
        assert not (sample.stat().st_mode & 0o200), "stage files must be read-only"
        guard._make_writable(tmp_path / "a")
        guard._make_writable(tmp_path / "b")

    def test_the_two_stages_are_separate_directories_not_one_shared_tree(
        self, tmp_path: Path
    ) -> None:
        project = _mini_project(tmp_path / "project")
        first = guard.stage_sources(project, tmp_path / "a")
        second = guard.stage_sources(project, tmp_path / "b")

        assert Path(first.path) != Path(second.path)
        assert Path(first.path).is_dir() and Path(second.path).is_dir()
        assert first.path != str(project), "a stage must not be the live tree"
        for stage in (tmp_path / "a", tmp_path / "b"):
            guard._make_writable(stage)

    def test_a_stage_missing_the_live_bytes_is_refused(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The stage is re-sampled from disk, so a doctored copy is caught."""
        project = _mini_project(tmp_path / "project")
        real_stage = guard.stage_sources

        def _drop_a_file(root: Path, dest: Path) -> guard.StageReport:
            report = real_stage(root, dest)
            victim = dest / "r219probe" / "__init__.py"
            if victim.exists():
                guard._make_writable(dest)
                victim.unlink()
                report.files -= 1  # the REPORT lies; the disk does not
            return report

        monkeypatch.setattr(guard, "stage_sources", _drop_a_file)
        fake = _fake_build(tmp_path / "seed", b"w", {"a": b"b"})
        monkeypatch.setattr(guard, "_run_build", fake["runner"])

        report = guard.guarded_two_build(
            project, tmp_path / "work", source_date_epoch=1
        )

        assert report["verdict"] == guard.BLOCKED
        assert report["reproducible"] is False
        assert any("its own staging report" in b for b in report["blockers"])

    def test_the_guard_re_samples_each_stage_from_disk(self, tmp_path: Path) -> None:
        """Two stage rows per stage: the copier's claim and an independent read."""
        project = _mini_project(tmp_path / "project")
        fake = _fake_build(tmp_path / "seed", b"w", {"a": b"b"})
        original = fake["runner"]
        monkey_calls: List[Path] = []

        def _run(stage: Path, out: Path, epoch: int) -> guard.BuildOutcome:
            monkey_calls.append(stage)
            return original(stage, out, epoch)

        saved = guard._run_build
        guard._run_build = _run  # type: ignore[assignment]
        try:
            report = guard.guarded_two_build(
                project, tmp_path / "work", source_date_epoch=1
            )
        finally:
            guard._run_build = saved  # type: ignore[assignment]

        paths = [row["path"] for row in report["stages"]]
        assert sum(1 for p in paths if p.endswith("(re-sampled)")) == 2
        assert len(monkey_calls) == 2
        assert len(set(monkey_calls)) == 2, "the two builds must use two stages"


class TestComparisonMeasuresContent:
    """The comparison must measure content, not archive metadata."""

    def test_a_one_byte_difference_in_the_wheel_is_not_reproducible(
        self, tmp_path: Path
    ) -> None:
        dist = tmp_path / "d"
        members = {"pkg/a.py": b"x = 1\n"}
        a = _fake_build(dist / "seed", b"WHEEL-A", members)
        b = _fake_build(dist / "seed2", b"WHEEL-B", members)
        build_a = a["runner"](tmp_path, dist / "a", 1)
        build_b = b["runner"](tmp_path, dist / "b", 1)
        report: Dict[str, Any] = {}

        comparison = guard._compare(build_a, build_b, 1700000000, report)

        assert comparison["wheel"]["identical"] is False
        assert comparison["wheel"]["sha256_a"] != comparison["wheel"]["sha256_b"]
        assert report["blockers"] == ["the two wheels differ"]

    def test_gzip_metadata_alone_is_not_a_content_difference(
        self, tmp_path: Path
    ) -> None:
        """Two archives of the same members with different gzip mtimes normalize equal."""
        members = {"pkg/a.py": b"x = 1\n"}
        first = _make_tar_gz(tmp_path / "one.tar.gz", members, mtime=1)
        second = _make_tar_gz(tmp_path / "two.tar.gz", members, mtime=999999999)
        assert first.read_bytes() != second.read_bytes(), "raw bytes must differ"

        hash_one, _ = guard._normalize_and_hash(str(first), 1700000000)
        hash_two, _ = guard._normalize_and_hash(str(second), 1700000000)

        assert hash_one == hash_two

    def test_a_different_member_is_a_real_content_difference(
        self, tmp_path: Path
    ) -> None:
        first = _make_tar_gz(tmp_path / "one.tar.gz", {"pkg/a.py": b"x = 1\n"}, mtime=1)
        second = _make_tar_gz(
            tmp_path / "two.tar.gz", {"pkg/a.py": b"x = 2\n"}, mtime=1
        )

        hash_one, _ = guard._normalize_and_hash(str(first), 1700000000)
        hash_two, _ = guard._normalize_and_hash(str(second), 1700000000)

        assert hash_one != hash_two

    def test_the_raw_sdist_difference_is_reported_not_absorbed(
        self, tmp_path: Path
    ) -> None:
        """Normalization must not hide that the un-normalized archives differed."""
        members = {"pkg/a.py": b"x = 1\n"}
        dist = tmp_path / "d"
        a = _fake_build(dist / "seed", b"SAME", members, raw_sdist_mtime=1)
        b = _fake_build(dist / "seed2", b"SAME", members, raw_sdist_mtime=999999999)
        build_a = a["runner"](tmp_path, dist / "a", 1)
        build_b = b["runner"](tmp_path, dist / "b", 1)
        report: Dict[str, Any] = {}

        comparison = guard._compare(build_a, build_b, 1700000000, report)

        assert comparison["sdist"]["identical"] is True
        assert comparison["sdist"]["raw_identical"] is False, (
            "the raw archives really did differ, and the report must say so"
        )
        assert (
            comparison["sdist"]["raw_sha256_a"] != comparison["sdist"]["raw_sha256_b"]
        )
        assert "NORMALIZED sdist" in comparison["sdist"]["claim"]

    def test_a_matching_pair_is_reported_identical(self, tmp_path: Path) -> None:
        members = {"pkg/a.py": b"x = 1\n"}
        a = _fake_build(tmp_path / "seed", b"SAME", members)
        b = _fake_build(tmp_path / "seed2", b"SAME", members)
        build_a = a["runner"](tmp_path, tmp_path / "a", 1)
        build_b = b["runner"](tmp_path, tmp_path / "b", 1)
        report: Dict[str, Any] = {}

        comparison = guard._compare(build_a, build_b, 1700000000, report)

        assert comparison["identical"] is True
        assert report["blockers"] == []


class TestManifestIsContentBased:
    def test_manifest_digest_ignores_key_order(self) -> None:
        left = {"a.py": "11" * 32, "b.py": "22" * 32}
        right = {"b.py": "22" * 32, "a.py": "11" * 32}
        assert guard.manifest_digest(left) == guard.manifest_digest(right)

    def test_manifest_digest_changes_when_content_changes(self) -> None:
        left = {"a.py": "11" * 32}
        right = {"a.py": "33" * 32}
        assert guard.manifest_digest(left) != guard.manifest_digest(right)

    def test_manifest_digest_is_not_vacuous_for_an_empty_tree(self) -> None:
        assert guard.manifest_digest({}) != guard.manifest_digest({"a.py": "00" * 32})

    def test_generated_roots_are_never_staged(self, tmp_path: Path) -> None:
        """logs/ holds every run's output; staging it is the leak this avoids."""
        project = _mini_project(tmp_path / "project")
        for excluded in (
            "logs",
            "graphify-out",
            "dist",
            "build",
            "__pycache__",
            "node_modules",
        ):
            victim = project / excluded
            victim.mkdir(parents=True, exist_ok=True)
            (victim / "big.bin").write_bytes(b"x" * 4096)
        (project / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")

        staged = guard.stage_sources(project, tmp_path / "stage")

        assert staged.error == ""
        names = {
            p.relative_to(tmp_path / "stage").as_posix()
            for p in (tmp_path / "stage").rglob("*")
        }
        assert "keep.py" in names
        assert not any(n.startswith("logs/") for n in names)
        assert not any(n.startswith("graphify-out/") for n in names)
        assert not any(n.startswith("dist/") for n in names)
        assert not any(n.startswith("build/") for n in names)
        assert not any(n.startswith("__pycache__/") for n in names)
        assert "pyproject.toml" in names
        guard._make_writable(tmp_path / "stage")


class TestCliContract:
    def test_a_blocked_or_invalid_lane_exits_non_zero(self, tmp_path: Path) -> None:
        code = guard.main(
            [
                "--project-root",
                str(tmp_path / "does-not-exist"),
                "--work-dir",
                str(tmp_path / "work"),
            ]
        )
        assert code == 2, "blocked/invalid must never exit 0"

    def test_the_guard_never_invokes_git(self) -> None:
        """The gate builds and compares; it must not touch version control."""
        source = Path(guard.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "git commit",
            "git push",
            "git tag",
            "git add",
            "git checkout",
        ):
            assert forbidden not in source
        assert "subprocess" in source
        # The only subprocess call in the module is the builder.
        assert source.count("subprocess.run") == 1


class TestEndToEndTwoBuilds:
    """The one real run: two real builds of one pinned stage, byte compared."""

    def test_two_real_builds_of_one_pinned_stage_are_byte_identical(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("build", reason="the builder is the thing under test")
        project = _mini_project(tmp_path / "project")
        work = tmp_path / "work"

        report = guard.guarded_two_build(project, work, source_date_epoch=1700000000)

        assert report["builds"], "the lane must have run a build"
        for build in report["builds"]:
            assert build["built"] is True, build.get("error")
        assert report["source_stable"] is True
        assert report["verdict"] == guard.REPRODUCIBLE, report["blockers"]
        assert report["reproducible"] is True
        assert report["compare"]["wheel"]["identical"] is True
        assert report["compare"]["sdist"]["identical"] is True
        assert len(report["compare"]["wheel"]["sha256_a"]) == 64
