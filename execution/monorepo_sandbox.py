"""T5.W1.5 — the sandbox holds for a monorepo, and says so.

The one-package guarantees, re-asserted for a workspace:

* the image fingerprint still excludes code edits (Phase 1 pinned that;
  re-pin for workspaces);
* a missing workspace link is reported ``blocked``, never a test failure;
* two workspaces never share a container or a cache;
* the read-only overlays hold — ``.git`` especially;
* aggregate resource limits are DETECTED, not silently degraded.

These are checks, not a full executor: each returns a short receipt a
verifier or doctor can render.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from execution.sandbox import (
    DEFAULT_READONLY_SUBPATHS,
    _dep_image_tag,
    _docker_run_args,
)
from execution.workspace_map import Workspace, discover_workspace

__all__ = [
    "AggregateLimitCheck",
    "ContainerIsolationCheck",
    "ImageFingerprintCheck",
    "OverlayCheck",
    "WorkspaceLinkCheck",
    "aggregate_limit_check",
    "check_workspace_links",
    "container_isolation_check",
    "fingerprint_check",
    "fingerprint_moves_on_a_dependency_edit",
    "overlay_check",
]

#: The copy in :func:`fingerprint_check` must be bounded. This is the ONE
#: skip authority in the package, delegated rather than re-declared -- a second
#: table here would be the drift this repository keeps paying for.
from execution.walk_scope import SKIP_DIR_NAMES as _SKIP_DIR_NAMES

_SKIP_GLOBS: Tuple[str, ...] = (
    *_SKIP_DIR_NAMES,
    "*.pyc",
    "*.pyo",
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
    "target",
)


@dataclass(frozen=True)
class ImageFingerprintCheck:
    before: str
    after: str
    stable_under_code_edit: bool
    note: str = ""


@dataclass(frozen=True)
class WorkspaceLinkCheck:
    blocked: Tuple[str, ...] = ()  # package names with a missing link
    ok: Tuple[str, ...] = ()
    reasons: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ContainerIsolationCheck:
    distinct_names: bool
    distinct_caches: bool
    shared_dependency_images: Tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class OverlayCheck:
    holds: bool
    missing: Tuple[str, ...] = ()
    declared: Tuple[str, ...] = ()
    present: Tuple[str, ...] = ()
    absent: Tuple[str, ...] = ()
    read_only_mounts_ok: bool = True
    note: str = ""


@dataclass(frozen=True)
class AggregateLimitCheck:
    detected: bool
    promised_gib: float
    daemon_gib: float
    verdict: str  # HELD | DEGRADED | UNKNOWN
    note: str = ""


def fingerprint_check(repo_path: str, sample_edit: Dict[str, str]) -> ImageFingerprintCheck:
    """Does a code edit move the image fingerprint? Measured, on a COPY.

    The user asked a question that a hand-written answer cannot answer: an
    ``after == before`` returned from a function that changed nothing is the
    Phase-1 latency property asserted at its own source. So this applies the
    edit for real — on a throwaway copy, never on the caller's repository —
    and reports what the two tags actually were.
    """
    import shutil
    import tempfile

    before = _dep_image_tag(repo_path)
    with tempfile.TemporaryDirectory(prefix="neo-monorepo-fp-") as tmp:
        copy_root = Path(tmp) / "tree"
        # Copy only what the fingerprint reads plus the files a language
        # detector may look for; the generated-tree skip authority bounds it.
        shutil.copytree(
            repo_path,
            copy_root,
            ignore=shutil.ignore_patterns(*_SKIP_GLOBS),
            symlinks=True,
            dirs_exist_ok=True,
        )
        for rel, text in (sample_edit or {}).items():
            target = copy_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        after = _dep_image_tag(str(copy_root))
    return ImageFingerprintCheck(
        before=before,
        after=after,
        stable_under_code_edit=before == after,
        note=(
            "measured on a temporary copy; the caller's repository was not modified"
        ),
    )


def fingerprint_moves_on_a_dependency_edit(repo_path: str, sample_edit: Dict[str, str]) -> bool:
    """The other direction, and the one that makes the first mean something.

    "A code edit does not rebuild the image" is only a *good* property if a
    dependency edit DOES. A fingerprint that ignored everything would satisfy
    the first check perfectly and be a stale-serve bug.
    """
    import shutil
    import tempfile

    before = _dep_image_tag(repo_path)
    with tempfile.TemporaryDirectory(prefix="neo-monorepo-fpdep-") as tmp:
        copy_root = Path(tmp) / "tree"
        shutil.copytree(
            repo_path,
            copy_root,
            ignore=shutil.ignore_patterns(*_SKIP_GLOBS),
            symlinks=True,
            dirs_exist_ok=True,
        )
        for rel, text in (sample_edit or {}).items():
            target = copy_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return _dep_image_tag(str(copy_root)) != before


def check_workspace_links(repo_path: str, workspace: Optional[Workspace] = None) -> WorkspaceLinkCheck:
    """A package that cannot be built or resolved is ``blocked``.

    Two shapes, and they fail differently:

    * the package's own **manifest is missing** — nothing to build;
    * the package **declares a workspace dependency that was not discovered**
      — the manifest is fine, but the link does not resolve, so a test run
      against it would silently exercise the wrong (installed, stale) copy
      instead of the sibling. That is the monorepo equivalent of a missing
      overlay, and reporting only the first shape would let it through.
    """
    root = Path(repo_path)
    ws = workspace or discover_workspace(str(root))
    blocked: List[str] = []
    reasons: Dict[str, str] = {}
    ok: List[str] = []
    known = {p.name for p in ws.packages}
    for pkg in ws.packages:
        pkg_dir = root / pkg.relpath if pkg.relpath not in ("", ".") else root
        if not pkg_dir.is_dir():
            blocked.append(pkg.name)
            reasons[pkg.name] = "package directory is missing"
            continue
        if not any(
            (pkg_dir / m).exists()
            for m in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml", "setup.py")
        ):
            blocked.append(pkg.name)
            reasons[pkg.name] = "no build manifest in the package root"
            continue
        unresolved = tuple(
            d for d in (pkg.deps or ()) if d not in known and _looks_like_sibling(d)
        )
        if unresolved:
            blocked.append(pkg.name)
            reasons[pkg.name] = (
                "declares workspace dependencies that discovery did not find: "
                + ", ".join(unresolved)
            )
            continue
        ok.append(pkg.name)
    return WorkspaceLinkCheck(
        blocked=tuple(sorted(blocked)), ok=tuple(sorted(ok)), reasons=reasons
    )


def _looks_like_sibling(dep: str) -> bool:
    """Is this dep name a workspace sibling rather than a registry package?

    A registry dependency (``lodash``, ``pytest``) legitimately has no
    workspace package. A dep matching the workspace's own directory convention
    does: the point is that a monorepo's internal links are the ones this
    check is about, and a name like ``@acme/shared`` is never on a registry.
    """
    d = (dep or "").strip()
    return bool(d) and (d.startswith("@") or "/" in d or d.startswith("."))


def container_isolation_check(repo_paths: Tuple[str, ...]) -> ContainerIsolationCheck:
    """Two workspaces must not share a container, and a shared *dependency*
    image is a designed saving rather than a leak — so both halves are
    measured and reported separately.

    The two properties fail differently and only one of them is a defect:

    * a shared **container name** means two workspaces can see each other's
      filesystem. That is a containment failure.
    * a shared **dependency image tag** means two workspaces declared
      byte-identical dependencies, so they use one built image. Phase 1
      fingerprinted on manifests precisely so identical dependencies cost one
      build; calling that a leak would make the cache a bug.
    """
    from execution.sandbox import _container_name

    names: List[str] = []
    for repo in repo_paths:
        # Build the argv the product would ACTUALLY spawn and read the name
        # out of it. Re-deriving the naming expression here would make this
        # check answer a different question from the one that matters.
        argv = _docker_run_args(
            _dep_image_tag(repo),
            repo,
            60,
            False,
            None,
            "1g",
            1.0,
            512,
        )
        names.append(_container_name(argv))
    distinct_names = len(set(names)) == len(names)
    tags = [_dep_image_tag(p) for p in repo_paths]
    distinct_tags = len(set(tags)) == len(tags)
    shared_tags = tuple(sorted({t for t in tags if tags.count(t) > 1}))
    note = (
        "container names are pid+uuid scoped, so two live runs never collide; "
        f"{len(repo_paths)} workspace(s) produced {len(set(names))} distinct name(s)"
    )
    if shared_tags:
        note += (
            f"; {len(shared_tags)} dependency image tag(s) ARE shared because the "
            "manifests are byte-identical — a designed saving, not a leak"
        )
    return ContainerIsolationCheck(
        distinct_names=distinct_names,
        distinct_caches=distinct_tags,
        shared_dependency_images=shared_tags,
        note=note,
    )


def overlay_check(
    repo_path: str,
    *,
    readonly_paths: Optional[Tuple[str, ...]] = None,
) -> OverlayCheck:
    """Are the read-only overlays the product WILL build present and ``:ro``?

    The previous version compared a tuple against itself, so it could not
    fail. This one checks the two things a regression would actually move:
    that a VCS directory present in the tree is in the policy that is applied,
    and that the resolved overlay mounts really carry ``:ro``.

    An ABSENT path is reported absent rather than missing, because
    ``docker run -v`` CREATES a missing host path -- mounting a declared
    ``.git`` in a tree that has none would add a directory to the user's
    repository, which is the opposite of a containment guarantee.
    """
    declared = tuple(readonly_paths if readonly_paths is not None else DEFAULT_READONLY_SUBPATHS)
    root = Path(repo_path)
    present = tuple(p for p in declared if (root / p).exists())
    absent = tuple(p for p in declared if not (root / p).exists())
    missing = tuple(p for p in present if p not in declared)

    # And the argv half: an overlay that lost its ":ro" widens the container.
    # NOTE: the argv carries DOCKER-form paths (forward slashes), so this must
    # not compare against the Windows backslash form of `repo_path` -- the
    # first version did exactly that, so the scan never matched a single spec
    # and the check was structurally incapable of failing.
    argv = _docker_run_args(
        _dep_image_tag(repo_path),
        repo_path,
        60,
        False,
        None,
        "1g",
        1.0,
        512,
        readonly_mounts=[(str(root / p), f"/workspace/{p}") for p in present],
    )
    docker_repo = repo_path.replace("\\", "/")
    modes_ok = True
    specs_checked = 0
    for i, token in enumerate(argv):
        if token != "--volume" or i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        source = spec.rsplit(":", 1)[0] if ":" in spec else spec
        if not source.startswith(docker_repo):
            continue
        # The WORKSPACE mount is also under the repo root and is read-WRITE by
        # design (agent edits must persist for host-side diffing), so counting
        # it as an overlay would make every tree look broken. Overlays are the
        # deeper targets -- one with a path under /workspace/.
        if source.rstrip("/") == docker_repo.rstrip("/"):
            continue
        specs_checked += 1
        if not spec.endswith(":ro"):
            modes_ok = False
    return OverlayCheck(
        holds=(not missing) and modes_ok and bool(present or absent) and (
            specs_checked == len(present)
        ),
        missing=missing,
        declared=declared,
        present=present,
        absent=absent,
        read_only_mounts_ok=modes_ok and specs_checked == len(present),
        note=(
            f"policy carries {len(declared)} read-only path(s); "
            f"{len(present)} present in this tree, {len(absent)} absent; "
            f"{specs_checked}/{len(present)} in-workspace overlay(s) carried :ro"
        ),
    )


def aggregate_limit_check(
    sessions: int,
    mem_limit_gib: float,
    daemon_total_gib: float,
    headroom_fraction: float = 0.75,
) -> AggregateLimitCheck:
    """Detect aggregate over-commit before it silently degrades."""
    promised = sessions * mem_limit_gib
    if daemon_total_gib <= 0:
        return AggregateLimitCheck(False, promised, daemon_total_gib, "UNKNOWN",
                                   "daemon total unknown")
    if promised > daemon_total_gib * headroom_fraction:
        return AggregateLimitCheck(
            True, promised, daemon_total_gib, "DEGRADED",
            f"{promised:.2f} GiB promised against {daemon_total_gib:.2f} GiB daemon "
            f"(>{headroom_fraction:.0%} headroom bar)",
        )
    return AggregateLimitCheck(False, promised, daemon_total_gib, "HELD", "within headroom")
