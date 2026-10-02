from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


SCHEMA_VERSION = 1
EXIT_SUCCESS = 0
EXIT_VERIFICATION = 2
EXIT_ENVIRONMENT = 3


class ReleaseVerificationError(RuntimeError):
    """Raised when release artifacts violate the package contract."""


class ReleaseUsageError(ReleaseVerificationError):
    """Raised when command-line arguments are invalid."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that routes usage failures through machine output."""

    def error(self, message: str) -> None:
        raise ReleaseUsageError(message)


def _load_pyproject(path: Path) -> dict[str, Any]:
    """Load one pyproject.toml file and return its mapping."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVerificationError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseVerificationError(f"pyproject is not a mapping: {path}")
    return data


def _project(data: dict[str, Any]) -> dict[str, Any]:
    """Return and validate the project table."""
    value = data.get("project")
    if not isinstance(value, dict):
        raise ReleaseVerificationError("pyproject has no [project] table")
    return value


def enrich_sbom(project_root: Path, sbom_path: Path) -> dict[str, Any]:
    """Validate a CycloneDX SBOM and complete its root dependency graph.

    The project root must exist and ``sbom_path`` must be a JSON CycloneDX
    document whose metadata component and locked dependency components are
    present. The document is rewritten atomically in deterministic key order.
    """
    try:
        payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(
            f"could not read SBOM {sbom_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReleaseVerificationError("SBOM root must be a JSON object")
    spec_version = payload.get("specVersion")
    if not isinstance(spec_version, str) or not spec_version.startswith("1."):
        raise ReleaseVerificationError("SBOM has no CycloneDX 1.x specVersion")
    components = payload.get("components")
    if not isinstance(components, list):
        raise ReleaseVerificationError("SBOM components must be a list")
    metadata = payload.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    root_ref = root.get("bom-ref") if isinstance(root, dict) else None
    if not isinstance(root_ref, str) or not root_ref:
        raise ReleaseVerificationError("SBOM metadata component has no bom-ref")
    by_name: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        name = component.get("name")
        reference = component.get("bom-ref")
        if isinstance(name, str) and isinstance(reference, str) and name and reference:
            by_name.setdefault(canonicalize_name(name), reference)
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise ReleaseVerificationError("SBOM dependencies must be a list")
    project = _project(_load_pyproject(project_root / "pyproject.toml"))
    requirements = project.get("dependencies", [])
    if not isinstance(requirements, list):
        raise ReleaseVerificationError("project dependencies must be a list")
    direct_refs: list[str] = []
    missing: list[str] = []
    for value in requirements:
        if not isinstance(value, str):
            raise ReleaseVerificationError("project dependency entries must be strings")
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ReleaseVerificationError(
                f"invalid project dependency: {value}"
            ) from exc
        reference = by_name.get(canonicalize_name(requirement.name))
        if reference is None:
            missing.append(requirement.name)
        elif reference not in direct_refs:
            direct_refs.append(reference)
    if missing:
        raise ReleaseVerificationError(
            "SBOM is missing direct dependencies: " + ", ".join(sorted(missing))
        )
    root_entry = next(
        (
            item
            for item in dependencies
            if isinstance(item, dict) and item.get("ref") == root_ref
        ),
        None,
    )
    if root_entry is None:
        root_entry = {"ref": root_ref}
        dependencies.append(root_entry)
    existing = root_entry.get("dependsOn", [])
    if not isinstance(existing, list):
        raise ReleaseVerificationError("SBOM root dependsOn must be a list")
    root_entry["dependsOn"] = sorted(
        {value for value in [*existing, *direct_refs] if isinstance(value, str)}
    )
    properties = metadata.get("properties") if isinstance(metadata, dict) else None
    if not isinstance(properties, list):
        properties = []
    if not any(
        isinstance(item, dict)
        and item.get("name") == "neo:direct-dependency-graph-complete"
        for item in properties
    ):
        properties.append(
            {
                "name": "neo:direct-dependency-graph-complete",
                "value": "true",
            }
        )
    metadata["properties"] = properties
    _atomic_write_text(
        sbom_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return {
        "path": str(sbom_path),
        "sha256": _sha256(sbom_path),
        "spec_version": spec_version,
        "component_count": len(components),
        "root_ref": root_ref,
        "direct_dependencies": sorted(direct_refs),
    }


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write one text report atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _git_environment() -> dict[str, str]:
    """Return an environment that excludes ambient Git redirection and config."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"HOME", "LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMP", "WINDIR"}
    }
    for key in tuple(env):
        if key.upper().startswith("GIT_"):
            env.pop(key, None)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _path_ignored(path: str, project_root: Path, ignored_roots: list[Path]) -> bool:
    """Return whether a Git status path is inside a declared output root."""
    candidate = (project_root / path).resolve()
    for ignored in ignored_roots:
        root = ignored.resolve()
        if candidate == root or root in candidate.parents:
            return True
    return False


def _git_provenance(project_root: Path, ignored_roots: list[Path]) -> dict[str, Any]:
    """Return commit, branch, tags, and relevant dirty paths without Git config."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=_git_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    if completed.returncode != 0:
        return {
            "available": False,
            "error": completed.stderr.strip() or "not a Git checkout",
        }
    top = Path(completed.stdout.strip()).resolve()
    if top != project_root.resolve():
        return {"available": False, "error": "project root is not the Git top level"}
    commands = {
        "commit": ["rev-parse", "HEAD"],
        "branch": ["branch", "--show-current"],
        "tag": ["tag", "--points-at", "HEAD"],
        "status": ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    }
    values: dict[str, str] = {}
    for key, arguments in commands.items():
        result = subprocess.run(
            ["git", "-C", str(project_root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=_git_environment(),
            check=False,
        )
        if result.returncode != 0:
            return {"available": False, "error": f"git {key} failed"}
        values[key] = result.stdout.strip()
    status_entries = [entry for entry in values["status"].split("\0") if entry]
    paths: list[str] = []
    for entry in status_entries:
        path = (
            entry[3:]
            if len(entry) > 3
            and entry[:2]
            in {" M", "M ", "MM", "A ", " D", "D ", "R ", "C ", "UU", "??"}
            else entry
        )
        if not _path_ignored(path, project_root, ignored_roots):
            paths.append(path)
    return {
        "available": True,
        "commit": values["commit"],
        "branch": values["branch"] or None,
        "tags": sorted(value for value in values["tag"].splitlines() if value),
        "clean": not paths,
        "dirty_paths": sorted(set(paths)),
    }


def _canonical_version(value: object, source: str) -> str:
    """Return a canonical PEP 440 version or fail with source context."""
    if not isinstance(value, str):
        raise ReleaseVerificationError(f"{source} has no string version")
    try:
        return str(Version(value))
    except InvalidVersion as exc:
        raise ReleaseVerificationError(
            f"{source} has invalid version {value!r}"
        ) from exc


def _artifact_stem(name: str, version: str) -> str:
    """Return the normalized distribution stem used by wheel and sdist names."""
    normalized = re.sub(r"[-_.]+", "_", name).lower()
    return f"{normalized}-{version}"


def _source_payload(data: dict[str, Any], root: Path) -> tuple[set[str], set[str]]:
    """Return required Python modules and configured package-data files."""
    tools = data.get("tool", {})
    setuptools = tools.get("setuptools", {}) if isinstance(tools, dict) else {}
    packages = setuptools.get("packages", []) if isinstance(setuptools, dict) else []
    if not isinstance(packages, list) or not packages:
        raise ReleaseVerificationError(
            "pyproject has no explicit setuptools package list"
        )
    modules: set[str] = set()
    for package in packages:
        if not isinstance(package, str) or not package:
            raise ReleaseVerificationError(
                "setuptools package entries must be non-empty strings"
            )
        package_root = root.joinpath(*package.split("."))
        if not package_root.is_dir():
            raise ReleaseVerificationError(f"configured package is missing: {package}")
        for source in package_root.rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            modules.add(source.relative_to(root).as_posix())

    package_data = setuptools.get("package-data", {})
    if not isinstance(package_data, dict):
        raise ReleaseVerificationError("tool.setuptools.package-data must be a table")
    data_files: set[str] = set()
    for package, patterns in package_data.items():
        if not isinstance(package, str) or not isinstance(patterns, list):
            raise ReleaseVerificationError("invalid package-data entry")
        package_root = root.joinpath(*package.split("."))
        for source in package_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(root).as_posix()
            package_relative = source.relative_to(package_root).as_posix()
            if any(
                fnmatch.fnmatchcase(package_relative, str(pattern))
                for pattern in patterns
            ):
                data_files.add(relative)
    return modules, data_files


def _safe_archive_names(names: Iterable[str]) -> set[str]:
    """Validate archive member names and return them as a set."""
    validated: set[str] = set()
    for raw_name in names:
        name = raw_name.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseVerificationError(f"unsafe archive member: {raw_name}")
        if "__pycache__" in path.parts or name.endswith((".pyc", ".pyo")):
            raise ReleaseVerificationError(
                f"generated Python cache in artifact: {raw_name}"
            )
        validated.add(name)
    return validated


def _metadata_from_bytes(payload: bytes, source: str) -> Any:
    """Parse RFC 822 package metadata from bytes."""
    try:
        return BytesParser().parsebytes(payload)
    except Exception as exc:
        raise ReleaseVerificationError(
            f"invalid package metadata in {source}: {exc}"
        ) from exc


def _verify_metadata(
    metadata: Any,
    project: dict[str, Any],
    source: str,
) -> dict[str, str]:
    """Verify identity fields shared by wheel METADATA and sdist PKG-INFO."""
    name = project.get("name")
    expected_version = _canonical_version(project.get("version"), "pyproject [project]")
    actual_name = str(metadata.get("Name", ""))
    actual_version = str(metadata.get("Version", ""))
    if actual_name.casefold() != str(name).casefold():
        raise ReleaseVerificationError(
            f"{source} Name is {actual_name!r}, expected {name!r}"
        )
    if _canonical_version(actual_version, source) != expected_version:
        raise ReleaseVerificationError(
            f"{source} Version is {actual_version!r}, expected {expected_version!r}"
        )
    if Version(actual_version).local:
        raise ReleaseVerificationError(
            f"{source} contains forbidden local version {actual_version}"
        )
    expected_python = str(project.get("requires-python", ""))
    actual_python = str(metadata.get("Requires-Python", ""))
    try:
        expected_python = str(SpecifierSet(expected_python))
        actual_python = str(SpecifierSet(actual_python))
    except InvalidSpecifier as exc:
        raise ReleaseVerificationError(
            f"{source} has invalid Requires-Python metadata: {exc}"
        ) from exc
    if actual_python != expected_python:
        raise ReleaseVerificationError(
            f"{source} Requires-Python is {actual_python!r}, expected {expected_python!r}"
        )
    return {
        "name": actual_name,
        "version": expected_version,
        "requires_python": actual_python,
    }


def _expected_setuptools_generator(data: dict[str, Any]) -> str:
    """Return the exact setuptools generator pinned by build-system."""
    build_system = data.get("build-system", {})
    requirements = (
        build_system.get("requires", []) if isinstance(build_system, dict) else []
    )
    matches = [item for item in requirements if str(item).startswith("setuptools==")]
    if len(matches) != 1:
        raise ReleaseVerificationError(
            "build-system must pin exactly one setuptools== requirement"
        )
    return "setuptools (" + str(matches[0]).split("==", 1)[1] + ")"


def _verify_wheel(
    path: Path,
    data: dict[str, Any],
    modules: set[str],
    data_files: set[str],
    source_date_epoch: Optional[int],
) -> dict[str, Any]:
    """Verify one wheel's metadata, entry points, and configured payload."""
    project = _project(data)
    version = _canonical_version(project.get("version"), "pyproject [project]")
    expected_stem = _artifact_stem(str(project["name"]), version)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ReleaseVerificationError(f"wheel has a corrupt member: {path}")
        names = _safe_archive_names(archive.namelist())
        dist_infos = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(dist_infos) != 1 or not dist_infos[0].startswith(
            expected_stem + ".dist-info/"
        ):
            raise ReleaseVerificationError(f"wheel has unexpected dist-info: {path}")
        dist_info = dist_infos[0].rsplit("/", 1)[0]
        required = modules | data_files
        missing = sorted(required - names)
        if missing:
            raise ReleaseVerificationError(
                f"wheel is missing configured payload: {missing}"
            )
        metadata = _metadata_from_bytes(archive.read(dist_infos[0]), str(path))
        identity = _verify_metadata(metadata, project, f"wheel {path.name}")
        wheel_name = f"{dist_info}/WHEEL"
        entry_name = f"{dist_info}/entry_points.txt"
        for required_name in (wheel_name, entry_name):
            if required_name not in names:
                raise ReleaseVerificationError(f"wheel is missing {required_name}")
        wheel_metadata = _metadata_from_bytes(archive.read(wheel_name), wheel_name)
        if str(wheel_metadata.get("Root-Is-Purelib", "")).lower() != "true":
            raise ReleaseVerificationError("wheel is not marked purelib")
        expected_generator = _expected_setuptools_generator(data)
        if str(wheel_metadata.get("Generator", "")) != expected_generator:
            raise ReleaseVerificationError(
                f"wheel Generator is {wheel_metadata.get('Generator')!r}, expected {expected_generator!r}"
            )
        entry_text = archive.read(entry_name).decode("utf-8")
        scripts = project.get("scripts", {})
        if not isinstance(scripts, dict):
            raise ReleaseVerificationError("project.scripts must be a table")
        for command, target in scripts.items():
            if f"{command} = {target}" not in entry_text:
                raise ReleaseVerificationError(
                    f"wheel is missing console script {command!r}"
                )
        if source_date_epoch is not None:
            wheel_epoch = source_date_epoch - (source_date_epoch % 2)
            expected = datetime.fromtimestamp(wheel_epoch, timezone.utc).timetuple()[:6]
            for info in archive.infolist():
                if info.date_time != expected:
                    raise ReleaseVerificationError(
                        f"wheel timestamp {info.filename} is {info.date_time}, expected {expected}"
                    )
        return {
            **identity,
            "entry_points": sorted(scripts),
            "python_files": sum(name.endswith(".py") for name in names),
            "package_data_files": len(data_files),
        }


def _verify_sdist(
    path: Path,
    data: dict[str, Any],
    modules: set[str],
    data_files: set[str],
    source_date_epoch: Optional[int],
) -> dict[str, Any]:
    """Verify one sdist's metadata and configured source payload."""
    project = _project(data)
    version = _canonical_version(project.get("version"), "pyproject [project]")
    expected_root = _artifact_stem(str(project["name"]), version)
    with tarfile.open(path, "r:gz") as archive:
        raw_names = [member.name for member in archive.getmembers() if member.isfile()]
        names = _safe_archive_names(raw_names)
        roots = {
            PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts
        }
        if roots != {expected_root}:
            raise ReleaseVerificationError(
                f"sdist has unexpected top-level roots: {sorted(roots)}"
            )
        required = {f"{expected_root}/{name}" for name in modules | data_files}
        missing = sorted(required - names)
        if missing:
            raise ReleaseVerificationError(
                f"sdist is missing configured payload: {missing}"
            )
        inner_roots = {
            PurePosixPath(name).parts[1]
            for name in names
            if len(PurePosixPath(name).parts) > 1
        }
        forbidden_roots = {"logs", "dist", ".git"}
        present_forbidden = sorted(inner_roots & forbidden_roots)
        if present_forbidden:
            raise ReleaseVerificationError(
                f"sdist contains forbidden roots: {present_forbidden}"
            )
        pkg_info_name = f"{expected_root}/PKG-INFO"
        pyproject_name = f"{expected_root}/pyproject.toml"
        for required_name in (pkg_info_name, pyproject_name):
            if required_name not in names:
                raise ReleaseVerificationError(f"sdist is missing {required_name}")
        pkg_info_member = archive.getmember(pkg_info_name)
        pyproject_member = archive.getmember(pyproject_name)
        metadata = _metadata_from_bytes(
            archive.extractfile(pkg_info_member).read(), str(path)
        )
        identity = _verify_metadata(metadata, project, f"sdist {path.name}")
        embedded = _load_toml_bytes(
            archive.extractfile(pyproject_member).read(), pyproject_name
        )
        embedded_project = _project(embedded)
        if (
            embedded_project.get("name") != project.get("name")
            or _canonical_version(embedded_project.get("version"), pyproject_name)
            != version
        ):
            raise ReleaseVerificationError(
                "sdist pyproject version identity differs from source"
            )
        if source_date_epoch is not None:
            for member in archive.getmembers():
                if member.isfile() and member.mtime != source_date_epoch:
                    raise ReleaseVerificationError(
                        f"sdist timestamp {member.name} is {member.mtime}, expected {source_date_epoch}"
                    )
        return {
            **identity,
            "python_files": sum(name.endswith(".py") for name in names),
            "package_data_files": len(data_files),
        }


def _load_toml_bytes(payload: bytes, source: str) -> dict[str, Any]:
    """Load TOML bytes with consistent failure reporting."""
    try:
        data = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVerificationError(f"invalid TOML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseVerificationError(f"TOML is not a mapping: {source}")
    return data


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_names(dist: Path) -> list[Path]:
    """Return every release archive in a dist directory."""
    return sorted(
        path
        for path in dist.iterdir()
        if path.is_file()
        and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    )


def normalize_sdist(path: Path, source_date_epoch: int) -> None:
    """Rewrite one sdist with sorted members and deterministic gzip metadata."""
    temporary = path.with_name(path.name + ".normalized")
    try:
        with tarfile.open(path, "r:gz") as source_archive, temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=source_date_epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target_archive:
                    members = sorted(
                        (
                            member
                            for member in source_archive.getmembers()
                            if member.isfile()
                        ),
                        key=lambda member: member.name,
                    )
                    for member in members:
                        payload = source_archive.extractfile(member)
                        if payload is None:
                            raise ReleaseVerificationError(
                                f"could not read sdist member {member.name}"
                            )
                        normalized = tarfile.TarInfo(member.name)
                        normalized.size = member.size
                        normalized.mtime = source_date_epoch
                        normalized.mode = 0o755 if member.mode & 0o111 else 0o644
                        normalized.uid = 0
                        normalized.gid = 0
                        normalized.uname = ""
                        normalized.gname = ""
                        normalized.type = tarfile.REGTYPE
                        target_archive.addfile(normalized, payload)
        os.replace(temporary, path)
    except (OSError, tarfile.TarError, ReleaseVerificationError) as exc:
        if temporary.exists():
            temporary.unlink()
        raise ReleaseVerificationError(
            f"could not normalize sdist {path}: {exc}"
        ) from exc


def verify_dist(
    project_root: Path,
    dist: Path,
    artifact: str = "both",
    source_date_epoch: Optional[int] = None,
    canonicalize_sdist: bool = False,
    ignored_roots: Optional[list[Path]] = None,
) -> dict[str, Any]:
    """Verify one isolated dist directory and return a machine-readable report."""
    if not project_root.is_dir():
        raise ReleaseVerificationError(f"project root does not exist: {project_root}")
    if not dist.is_dir():
        raise ReleaseVerificationError(f"dist directory does not exist: {dist}")
    data = _load_pyproject(project_root / "pyproject.toml")
    project = _project(data)
    version = _canonical_version(project.get("version"), "pyproject [project]")
    stem = _artifact_stem(str(project["name"]), version)
    expected = {
        "wheel": f"{stem}-py3-none-any.whl",
        "sdist": f"{stem}.tar.gz",
    }
    wanted = ["wheel", "sdist"] if artifact == "both" else [artifact]
    archives = _artifact_names(dist)
    names = [path.name for path in archives]
    expected_names = [expected[kind] for kind in wanted]
    if names != expected_names:
        raise ReleaseVerificationError(
            f"dist must contain exactly {expected_names!r}; found {names!r}"
        )
    if canonicalize_sdist:
        if source_date_epoch is None:
            raise ReleaseVerificationError(
                "--normalize-sdist requires --source-date-epoch"
            )
        if "sdist" in wanted:
            normalize_sdist(dist / expected["sdist"], source_date_epoch)
    modules, data_files = _source_payload(data, project_root)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": str(project["name"]),
        "version": version,
        "dist": str(dist.resolve()),
        "source": _git_provenance(project_root, [dist, *(ignored_roots or [])]),
        "sdist_normalized": canonicalize_sdist and "sdist" in wanted,
        "artifacts": {},
    }
    for kind, path in zip(wanted, archives, strict=True):
        if kind == "wheel":
            details = _verify_wheel(path, data, modules, data_files, source_date_epoch)
        else:
            details = _verify_sdist(path, data, modules, data_files, source_date_epoch)
        report["artifacts"][kind] = {
            "path": str(path.resolve()),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            **details,
        }
    return report


def compare_reports(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Require two verified artifact sets to have identical names and hashes."""
    first_hashes = {kind: value["sha256"] for kind, value in first["artifacts"].items()}
    second_hashes = {
        kind: value["sha256"] for kind, value in second["artifacts"].items()
    }
    if first_hashes != second_hashes:
        raise ReleaseVerificationError(
            f"artifact hashes differ: first={first_hashes}, second={second_hashes}"
        )
    return {"reproducible": True, "sha256": first_hashes}


def _write_checksums(path: Path, report: dict[str, Any]) -> None:
    """Write exact verified artifact hashes in sha256sum-compatible format."""
    lines = [
        f"{details['sha256']}  {Path(details['path']).name}"
        for details in report["artifacts"].values()
    ]
    _atomic_write_text(path, "\n".join(lines) + "\n")


def _emit_event(event: str, **data: Any) -> None:
    """Write one schema-versioned NDJSON event to stdout."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    print(json.dumps(payload, sort_keys=True), flush=True)


def _render(report: dict[str, Any], event_mode: bool) -> None:
    """Render a final report as JSON or a terminal NDJSON event."""
    if not event_mode:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    _emit_event("release_verification_finished", report=report)


def main(argv: Optional[list[str]] = None) -> int:
    """Verify release artifacts with stable exit codes and machine output."""
    parser = _ArgumentParser(description="Verify Neo wheel and sdist release artifacts")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument(
        "--artifact", choices=("both", "wheel", "sdist"), default="both"
    )
    parser.add_argument("--compare-dist", type=Path)
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument("--normalize-sdist", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--ignore-path", action="append", type=Path, default=[])
    parser.add_argument("--require-tag")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--sbom", type=Path)
    parser.add_argument("--format", choices=("json", "ndjson"), default="json")
    parser.add_argument(
        "--events", action="store_true", help="alias for --format ndjson"
    )
    args: Optional[argparse.Namespace] = None
    event_mode = False
    report: Optional[dict[str, Any]] = None
    try:
        args = parser.parse_args(argv)
        event_mode = args.events or args.format == "ndjson"
        if args.source_date_epoch is not None and args.source_date_epoch < 0:
            raise ReleaseUsageError("--source-date-epoch must be non-negative")
        if event_mode:
            _emit_event(
                "release_verification_started",
                dist=str(args.dist),
                artifact=args.artifact,
                compare_dist=str(args.compare_dist) if args.compare_dist else None,
            )
        ignored_roots = [path.resolve() for path in args.ignore_path]
        report = verify_dist(
            args.project_root.resolve(),
            args.dist.resolve(),
            args.artifact,
            args.source_date_epoch,
            args.normalize_sdist,
            ignored_roots,
        )
        if args.compare_dist is not None:
            comparison = verify_dist(
                args.project_root.resolve(),
                args.compare_dist.resolve(),
                args.artifact,
                args.source_date_epoch,
                args.normalize_sdist,
                ignored_roots,
            )
            report["comparison"] = compare_reports(report, comparison)
        source = report.get("source", {})
        if args.require_clean and (
            not source.get("available") or not source.get("clean")
        ):
            raise ReleaseVerificationError(
                "release source is not a clean reviewed Git checkout"
            )
        if args.require_tag:
            tags = source.get("tags", []) if source.get("available") else []
            if args.require_tag not in tags:
                raise ReleaseVerificationError(
                    f"HEAD does not carry required release tag {args.require_tag!r}"
                )
        if args.sbom is not None:
            report["sbom"] = enrich_sbom(
                args.project_root.resolve(), args.sbom.resolve()
            )
        report["status"] = "pass"
        report["exit_code"] = EXIT_SUCCESS
        report["exit_reason"] = "success"
        if args.checksums is not None:
            _write_checksums(args.checksums.resolve(), report)
        if event_mode:
            for kind, details in report["artifacts"].items():
                _emit_event(
                    "artifact_verified",
                    artifact=kind,
                    filename=Path(details["path"]).name,
                    sha256=details["sha256"],
                )
            if report.get("comparison"):
                _emit_event("reproducibility_verified", **report["comparison"])
            if report.get("sbom"):
                _emit_event("sbom_verified", **report["sbom"])
    except ReleaseUsageError as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "error": {
                "kind": "usage_error",
                "message": str(exc),
                "exit_code": EXIT_VERIFICATION,
                "exit_reason": "usage_error",
            },
        }
    except ReleaseVerificationError as exc:
        error = {
            "kind": "verification_error",
            "message": str(exc),
            "exit_code": EXIT_VERIFICATION,
            "exit_reason": "verification_failed",
        }
        if report is None:
            report = {"schema_version": SCHEMA_VERSION}
        report.update(
            {"status": "fail", "exit_code": EXIT_VERIFICATION, "error": error}
        )
    except (
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "error": {
                "kind": "environment_error",
                "message": f"{type(exc).__name__}: {exc}",
                "exit_code": EXIT_ENVIRONMENT,
                "exit_reason": "environment_error",
            },
        }
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "error": {
                "kind": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "exit_code": EXIT_ENVIRONMENT,
                "exit_reason": "internal_error",
            },
        }
    if report is None:
        raise RuntimeError("release verifier produced no report")
    if args is not None and args.report is not None:
        try:
            _atomic_write_text(
                args.report.resolve(),
                json.dumps(report, indent=2, sort_keys=True) + "\n",
            )
        except OSError as exc:
            print(f"release report write failed: {exc}", file=sys.stderr)
            return EXIT_ENVIRONMENT
    if event_mode:
        _render(report, True)
    else:
        _render(report, False)
    return int(report.get("exit_code", EXIT_ENVIRONMENT))


if __name__ == "__main__":
    raise SystemExit(main())
