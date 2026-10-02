"""Guarded two-build reproducibility from a pinned, immutable source stage.

Why this module exists
----------------------
A byte-comparison of two artifacts only means something if both artifacts were
produced from *the same bytes of source*. Round 1 measured what happens when it
does not: a build pair was rejected because ``acp/client.py`` changed between
build A and build B, and a second pair was rejected because
``runtime/model_router.py`` changed during build B. Both rejections were correct.
Neither could be made into evidence, because there was no clean artifact pair
to compare.

So the guard is the point, not the comparison:

1. The live tree is sampled for a content digest **before** anything is copied
   and **again after every build**. If the digest moved at any point the result
   is ``invalid`` and ``reproducible`` is ``false`` - a moving source cannot
   produce evidence, whatever the two artifacts happen to say.
2. Each build runs in its **own independent stage** directory, copied from one
   pinned snapshot and then made read-only. Build B therefore never reads a
   file build A could have touched, so a shared mutable checkout cannot
   manufacture a false agreement.
3. Every stage is verified to carry the *same* digest as the live sample, and
   to carry no file the live sample did not have.

What is compared
----------------
The wheel is compared **byte for byte** after the two builds. The sdist is
compared after ``scripts.verify_release.normalize_sdist`` with the same
``SOURCE_DATE_EPOCH`` used for the builds, because a tar.gz is not
reproducible without that normalization and comparing the raw bytes would
measure gzip metadata rather than content.

Honesty rules this module will not break
----------------------------------------
- A missing builder, a failed build, or an unnormalizable sdist is
  ``blocked``, never ``reproducible``.
- A digest that moved is ``invalid``, never ``reproducible``.
- Nothing here commits, tags, pushes, uploads, or publishes. It writes only
  into a caller-supplied work directory.

Usage (from the repository root)::

    python -m scripts.reproducibility_guard --work-dir <dir> --json
    python -m scripts.reproducibility_guard --work-dir <dir> --source-date-epoch 1790072773

Assumes ``python -m build`` is importable and that ``pyproject.toml`` +
``MANIFEST.in`` describe a buildable project at ``--project-root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

__all__ = [
    "SCHEMA",
    "STAGE_EXCLUDED_DIRS",
    "STAGE_EXCLUDED_NAMES",
    "BuildOutcome",
    "guarded_two_build",
    "main",
    "manifest_digest",
    "sample_source",
    "stage_sources",
]

SCHEMA = "neo.reproducibility_guard/1"

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories never copied into a build stage. They are generated, enormous,
#: or contain another terminal's in-flight work. ``logs`` is the important
#: one: the product's own default log root lives inside the checkout, so a
#: stage that copied it would both bloat the sdist and race the test run.
STAGE_EXCLUDED_DIRS: Tuple[str, ...] = (
    ".git",
    ".harness",
    ".mypy_cache",
    ".opencode",
    ".playwright-mcp",
    ".pytest_cache",
    ".qwen",
    ".ruff_cache",
    ".shots",
    ".venv",
    ".neo",
    "__pycache__",
    "Temp",
    "build",
    "dist",
    "graphify-out",
    "logs",
    "node_modules",
    "probe_logs",
    "site-v1-backup",
    "venv",
    "neo_agent_cli.egg-info",
)

#: Individual root-level files never copied. Screenshots, a stray redirect
#: file, and two committed measurement scratch scripts that are not part of the
#: distribution payload.
STAGE_EXCLUDED_NAMES: Tuple[str, ...] = (
    "focus-skip.png",
    "og-check.png",
    "tui_rerun.out",
    "x",
)

#: Verdict vocabulary. ``invalid`` is the one that matters: it means the source
#: moved under the measurement, so no claim - not even a negative one - can be
#: made from what was observed.
REPRODUCIBLE = "reproducible"
NOT_REPRODUCIBLE = "not_reproducible"
INVALID = "invalid"
BLOCKED = "blocked"


@dataclass
class StageReport:
    """What one immutable build stage contained.

    ``digest`` is the content digest of the staged files and is the only thing
    compared against the live sample; ``files``/``bytes`` are the honest
    denominator so a stage that copied nothing cannot read as a match.
    """

    path: str
    files: int = 0
    bytes: int = 0
    digest: str = ""
    read_only: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection."""
        return asdict(self)


@dataclass
class BuildOutcome:
    """The result of one ``python -m build`` in one stage.

    ``returncode`` is kept rather than a boolean so a caller can tell a failed
    build (``!= 0``) from a builder that could not be started at all
    (``returncode is None``), which are different kinds of blocked.
    """

    stage: str
    out_dir: str
    returncode: Optional[int] = None
    elapsed_s: float = 0.0
    artifacts: Dict[str, str] = field(default_factory=dict)
    artifact_sizes: Dict[str, int] = field(default_factory=dict)
    error: str = ""
    log_tail: str = ""

    @property
    def built(self) -> bool:
        """True only when the builder exited 0 and produced both archives."""
        return (
            self.returncode == 0
            and "wheel" in self.artifacts
            and "sdist" in self.artifacts
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection, including the derived ``built``."""
        data = asdict(self)
        data["built"] = self.built
        return data


def _iter_source_files(root: Path) -> Iterable[Path]:
    """Yield every file under ``root`` that a build stage should contain.

    Assumes ``root`` exists. Directory pruning is by NAME, so a nested
    ``logs`` directory (a fixture, say) is pruned too; that is deliberate
    because the product's own log root is inside the checkout and the sdist
    must never contain a run directory.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in STAGE_EXCLUDED_DIRS
        )
        for name in sorted(filenames):
            if name in STAGE_EXCLUDED_NAMES:
                continue
            if name.endswith((".pyc", ".pyo", ".pyd", ".log", ".tmp")):
                continue
            yield Path(dirpath) / name


def source_manifest(root: Path) -> Dict[str, str]:
    """Return ``{posix relpath: sha256}`` for every staged file under ``root``.

    Assumes nothing about git: this hashes file CONTENT, so an edit that does
    not change ``git status`` is still detected. Sorted so the projection is
    deterministic.
    """
    manifest: Dict[str, str] = {}
    for path in _iter_source_files(root):
        manifest[path.relative_to(root).as_posix()] = _file_sha256(path)
    return manifest


def manifest_digest(manifest: Dict[str, str]) -> str:
    """Return one SHA-256 over a whole manifest, order-independent by sorting.

    Assumes ``manifest`` maps relative paths to hex digests. An empty manifest
    still produces a digest, so "no source" is comparable rather than special.
    """
    digest = hashlib.sha256()
    for relpath in sorted(manifest):
        digest.update(relpath.encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(manifest[relpath].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 of one file, streamed so a large file costs no memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sample_source(root: Path) -> Dict[str, Any]:
    """Take one content sample of the live tree.

    Returns ``{digest, files, bytes, seconds}``. ``files`` is the denominator:
    a digest of an empty tree is not a match for a digest of a real one, and
    the report shows both so a reader can tell.
    """
    started = time.perf_counter()
    manifest = source_manifest(root)
    total = 0
    for path in _iter_source_files(root):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return {
        "digest": manifest_digest(manifest),
        "files": len(manifest),
        "bytes": total,
        "seconds": round(time.perf_counter() - started, 3),
    }


def stage_sources(root: Path, dest: Path) -> StageReport:
    """Copy the source tree into an independent, read-only build stage.

    Assumes ``dest`` does not exist or is empty (it is removed first). The
    copy is content-complete for everything ``_iter_source_files`` yields, and
    the stage's own manifest is returned so the caller can prove the stage
    matches the live sample rather than assuming it.
    """
    report = StageReport(path=str(dest))
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, str] = {}
    total = 0
    for source in _iter_source_files(root):
        relative = source.relative_to(root)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            report.error = f"copy failed for {relative.as_posix()}: {exc}"
            return report
        manifest[relative.as_posix()] = _file_sha256(target)
        total += target.stat().st_size
    report.files = len(manifest)
    report.bytes = total
    report.digest = manifest_digest(manifest)
    report.read_only = _make_read_only(dest)
    return report


def _make_read_only(root: Path) -> bool:
    """Clear the write bit on every file under ``root``; return True if done.

    Read-only is the *second* line of defence - the digest check is the first -
    but it turns "someone edited the live tree mid-build" from a silent
    corruption into a loud build failure on Windows and a clear error on POSIX.
    """
    ok = True
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                path.chmod(0o444)
            except OSError:
                ok = False
        for name in dirnames:
            path = Path(dirpath) / name
            try:
                path.chmod(0o555)
            except OSError:
                ok = False
    return ok


def _make_writable(root: Path) -> None:
    """Restore write permission under ``root`` so a stage can be deleted."""
    for dirpath, _dirnames, filenames in os.walk(root):
        try:
            Path(dirpath).chmod(0o755)
        except OSError:
            pass
        for name in filenames:
            try:
                (Path(dirpath) / name).chmod(0o644)
            except OSError:
                pass


def _run_build(stage: Path, out_dir: Path, source_date_epoch: int) -> BuildOutcome:
    """Run one isolated ``python -m build`` and return exactly what happened.

    Assumes ``stage`` is a readable project root. The build is isolated from
    ambient configuration (``PIP_NO_INDEX``, ``PIP_CONFIG_FILE``) so a local
    pip config cannot change what is built, and ``PYTHONDONTWRITEBYTECODE``
    stops the stage accumulating ``__pycache__`` between the two builds.
    """
    outcome = BuildOutcome(stage=str(stage), out_dir=str(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment.pop("PIP_INDEX_URL", None)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(out_dir), str(stage)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        outcome.error = "python -m build exceeded its 3600s budget"
        outcome.elapsed_s = round(time.perf_counter() - started, 3)
        return outcome
    except OSError as exc:
        outcome.error = f"could not start the builder: {exc}"
        outcome.elapsed_s = round(time.perf_counter() - started, 3)
        return outcome
    outcome.elapsed_s = round(time.perf_counter() - started, 3)
    outcome.returncode = completed.returncode
    tail = (completed.stdout or "") + (completed.stderr or "")
    outcome.log_tail = tail[-4000:]
    if completed.returncode != 0:
        outcome.error = f"python -m build exited {completed.returncode}"
        return outcome
    for path in sorted(out_dir.iterdir()):
        if path.suffix == ".whl":
            outcome.artifacts["wheel"] = str(path)
        elif path.name.endswith(".tar.gz"):
            outcome.artifacts["sdist"] = str(path)
        else:
            continue
        outcome.artifact_sizes[path.name] = path.stat().st_size
    if not outcome.built:
        outcome.error = "the build exited 0 but produced no wheel/sdist pair"
    return outcome


def _normalize_and_hash(path: str, source_date_epoch: int) -> Tuple[str, int]:
    """Normalize one sdist in place, then return ``(sha256, size_bytes)``.

    Assumes the path is a readable ``.tar.gz``. Raises
    ``scripts.verify_release.ReleaseVerificationError`` if the sdist cannot be
    normalized - which is a ``blocked`` condition for the caller, never a
    silent pass.
    """
    from scripts.verify_release import normalize_sdist

    target = Path(path)
    normalize_sdist(target, source_date_epoch)
    return _file_sha256(target), target.stat().st_size


def guarded_two_build(
    project_root: Path,
    work_dir: Path,
    *,
    source_date_epoch: int = 0,
    keep_stages: bool = False,
) -> Dict[str, Any]:
    """Run the whole guarded lane and return one JSON-safe report.

    Assumes ``project_root`` is a buildable project and ``work_dir`` is a
    writable directory the caller owns. Never raises for an environmental
    failure: an unreachable builder, a failed build, or a moving source all
    come back as a report whose ``verdict`` is ``blocked`` or ``invalid``.

    The order of operations is the guard:

    1. sample the live tree,
    2. stage it twice into two independent read-only directories,
    3. build A in stage A, sample the live tree again,
    4. build B in stage B, sample the live tree again,
    5. compare only if every digest matched.

    A digest that moved at step 1, 3, 4 or 5 makes the report ``invalid``.
    """
    project_root = Path(project_root).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_root": str(project_root),
        "work_dir": str(work_dir),
        "source_date_epoch": source_date_epoch,
        "samples": [],
        "stages": [],
        "builds": [],
        "compare": {},
        "blockers": [],
        "verdict": None,
        "reproducible": False,
        "source_stable": False,
        "elapsed_s": 0.0,
    }
    started = time.perf_counter()

    sample_a = sample_source(project_root)
    report["samples"].append({"label": "before_staging", **sample_a})

    stage_a_dir = work_dir / "stage-a"
    stage_b_dir = work_dir / "stage-b"
    stage_a = stage_sources(project_root, stage_a_dir)
    stage_b = stage_sources(project_root, stage_b_dir)
    report["stages"] = [stage_a.to_dict(), stage_b.to_dict()]

    if stage_a.error or stage_b.error:
        report["blockers"].append(stage_a.error or stage_b.error)
        report["verdict"] = BLOCKED
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        return report

    for label, stage in (("stage_a", stage_a), ("stage_b", stage_b)):
        # Re-sample the stage FROM DISK rather than trusting the report the
        # copier wrote about itself. A stage whose own accounting is wrong is
        # exactly the case where a reproducibility claim would be invented.
        observed = sample_source(Path(stage.path))
        report["stages"].append(
            {
                "path": f"{stage.path} (re-sampled)",
                "files": observed["files"],
                "bytes": observed["bytes"],
                "digest": observed["digest"],
                "read_only": True,
                "error": "",
            }
        )
        if observed["digest"] != stage.digest:
            report["blockers"].append(
                f"{label} does not match its own staging report "
                f"({observed['files']} files on disk, digest {observed['digest'][:12]} vs "
                f"{stage.files} reported, {stage.digest[:12]})"
            )
        if observed["digest"] != sample_a["digest"]:
            report["blockers"].append(
                f"{label} does not match the live sample "
                f"({observed['files']} files, digest {observed['digest'][:12]} vs "
                f"{sample_a['files']} files, {sample_a['digest'][:12]})"
            )
    if stage_a.digest != stage_b.digest:
        report["blockers"].append("the two stages do not carry identical source")

    build_a = _run_build(stage_a_dir, work_dir / "dist-a", source_date_epoch)
    report["builds"].append(build_a.to_dict())
    sample_mid = sample_source(project_root)
    report["samples"].append({"label": "between_builds", **sample_mid})

    build_b = _run_build(stage_b_dir, work_dir / "dist-b", source_date_epoch)
    report["builds"].append(build_b.to_dict())
    sample_after = sample_source(project_root)
    report["samples"].append({"label": "after_builds", **sample_after})

    moved = [
        entry["label"]
        for entry in report["samples"][1:]
        if entry["digest"] != sample_a["digest"]
    ]
    report["source_stable"] = not moved
    if moved:
        report["blockers"].append(
            "the source tree changed during the lane (at: "
            + ", ".join(moved)
            + "); a moving source cannot produce reproducibility evidence"
        )

    for build in (build_a, build_b):
        if not build.built:
            if build.returncode == 0:
                # Named precisely, because "it exited 0" and "it produced
                # artifacts" are different claims and only the second is
                # evidence.
                report["blockers"].append(
                    f"build in {build.stage} exited 0 but produced no wheel/sdist pair"
                )
            else:
                report["blockers"].append(
                    build.error or f"build in {build.stage} did not complete"
                )

    if not report["blockers"] and report["source_stable"]:
        report["compare"] = _compare(build_a, build_b, source_date_epoch, report)
        if report["compare"].get("error"):
            report["blockers"].append(report["compare"]["error"])

    if report["blockers"]:
        report["verdict"] = INVALID if not report["source_stable"] else BLOCKED
    elif report["compare"].get("identical"):
        report["verdict"] = REPRODUCIBLE
        report["reproducible"] = True
    else:
        report["verdict"] = NOT_REPRODUCIBLE

    if not keep_stages:
        for stage_dir in (stage_a_dir, stage_b_dir):
            _make_writable(stage_dir)
            shutil.rmtree(stage_dir, ignore_errors=True)
    report["stages_kept"] = bool(keep_stages)
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    return report


def _compare(
    build_a: BuildOutcome,
    build_b: BuildOutcome,
    source_date_epoch: int,
    report: Dict[str, Any],
) -> Dict[str, Any]:
    """Byte-compare one build pair and return the measured comparison.

    Assumes both builds completed. The wheel is compared byte for byte; the
    sdist is compared after normalization, because raw tar.gz bytes measure
    gzip metadata rather than content. Both hashes are reported whether or not
    they match, so a reader can see the numbers rather than a boolean.
    """
    comparison: Dict[str, Any] = {"error": ""}
    try:
        wheel_a = Path(build_a.artifacts["wheel"])
        wheel_b = Path(build_b.artifacts["wheel"])
        wheel_hash_a = _file_sha256(wheel_a)
        wheel_hash_b = _file_sha256(wheel_b)
        # The RAW sdist bytes are recorded too, before normalization. A
        # reproducibility report that only showed the normalized digest would
        # hide exactly the difference normalization exists to absorb, and
        # "reproducible" would be read as "byte-identical" when it is not.
        sdist_a = Path(build_a.artifacts["sdist"])
        sdist_b = Path(build_b.artifacts["sdist"])
        raw_sha_a = _file_sha256(sdist_a)
        raw_sha_b = _file_sha256(sdist_b)
        raw_bytes_a = sdist_a.stat().st_size
        raw_bytes_b = sdist_b.stat().st_size
        sdist_hash_a, sdist_size_a = _normalize_and_hash(
            build_a.artifacts["sdist"], source_date_epoch
        )
        sdist_hash_b, sdist_size_b = _normalize_and_hash(
            build_b.artifacts["sdist"], source_date_epoch
        )
    except Exception as exc:
        comparison["error"] = (
            f"could not compare the build pair: {type(exc).__name__}: {exc}"
        )
        return comparison
    comparison.update(
        {
            "wheel": {
                "name_a": wheel_a.name,
                "name_b": wheel_b.name,
                "sha256_a": wheel_hash_a,
                "sha256_b": wheel_hash_b,
                "bytes_a": wheel_a.stat().st_size,
                "bytes_b": wheel_b.stat().st_size,
                "identical": wheel_hash_a == wheel_hash_b,
            },
            "sdist": {
                "name_a": sdist_a.name,
                "name_b": sdist_b.name,
                "sha256_a": sdist_hash_a,
                "sha256_b": sdist_hash_b,
                "bytes_a": sdist_size_a,
                "bytes_b": sdist_size_b,
                "identical": sdist_hash_a == sdist_hash_b,
                "normalized_with_source_date_epoch": source_date_epoch,
                "raw_sha256_a": raw_sha_a,
                "raw_sha256_b": raw_sha_b,
                "raw_bytes_a": raw_bytes_a,
                "raw_bytes_b": raw_bytes_b,
                "raw_identical": raw_sha_a == raw_sha_b,
                "claim": (
                    "the reproducibility claim is the NORMALIZED sdist; "
                    "raw_identical reports whether the un-normalized archive "
                    "matched and is recorded so a difference is visible rather "
                    "than absorbed silently"
                ),
            },
        }
    )
    comparison["identical"] = bool(comparison["wheel"]["identical"]) and bool(
        comparison["sdist"]["identical"]
    )
    blockers = report.setdefault("blockers", [])
    if not comparison["wheel"]["identical"]:
        blockers.append("the two wheels differ")
    if not comparison["sdist"]["identical"]:
        blockers.append("the two normalized sdists differ")
    return comparison


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the guarded lane from the command line.

    Exit codes: 0 reproducible, 1 not reproducible, 2 blocked or invalid.
    A blocked or invalid lane is a non-zero exit so a CI gate cannot read it as
    a pass, and the reason is on stdout as JSON.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-root",
        default=str(REPO_ROOT),
        help="the project to build from (default: this repository)",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        help="an empty writable directory for the stages and the two dists",
    )
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=1790072773,
        help="pinned build epoch; both builds and the sdist normalization use it",
    )
    parser.add_argument(
        "--keep-stages",
        action="store_true",
        help="keep the two read-only stages on disk for inspection",
    )
    parser.add_argument(
        "--report",
        default="",
        help="also write the JSON report to this path",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = guarded_two_build(
        Path(args.project_root),
        Path(args.work_dir),
        source_date_epoch=args.source_date_epoch,
        keep_stages=args.keep_stages,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.report:
        Path(args.report).write_text(payload + "\n", encoding="utf-8")
    if report["verdict"] == REPRODUCIBLE:
        return 0
    if report["verdict"] == NOT_REPRODUCIBLE:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
