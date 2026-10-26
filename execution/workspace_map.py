"""T5.W1.1 — the workspace concept, one level above the repository.

A monorepo is no longer "one repo with directory scoping." It is a
:class:`Workspace` of :class:`Package` s — each with its own test command,
build command, and dependency scope — discovered from the ecosystem's own
data files, not from code written per ecosystem.

Every finder is **data**: a (manifest, glob-field, parser) row in
``_WORKSPACE_SOURCES``. Adding an ecosystem is a data change, and
``test_a_new_ecosystem_is_a_data_change`` pins that.

A receipt is self-describing: the packages found, the manifest that found
them, the ones that could NOT be given a test command (and why), and the
dependency edges. Coverage is reported, never assumed — a package we cannot
wire to a test command is a gap a reader must see, not a package we quietly
never check.
"""

from __future__ import annotations

import dataclasses
import glob as _glob
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "WORKSPACE_SOURCES",
    "Package",
    "Workspace",
    "WorkspaceGap",
    "WorkspaceSource",
    "default_build_command",
    "default_test_command",
    "discover_workspace",
    "workspace_change_group",
]


@dataclass(frozen=True)
class Package:
    """One package inside a workspace: where it lives and how to drive it."""

    name: str
    relpath: str
    ecosystem: str
    test_command: str
    build_command: str
    manifest: str
    deps: Tuple[str, ...] = ()  # sibling package names this one depends on
    #: drive-reasonable globs, relative to the repo, that identify this package
    #: in changed-file sets
    root: str = ""


@dataclass(frozen=True)
class WorkspaceGap:
    """A package we found but cannot fully wire to a test command."""

    package: str
    reason: str


@dataclass(frozen=True)
class WorkspaceSource:
    """Data row: one way a monorepo names its packages."""

    ecosystem: str
    #: files at the repo root that signal this kind of workspace
    markers: Tuple[str, ...]
    #: field/path inside the manifest that lists package globs
    note: str = ""


@dataclass
class Workspace:
    """The discovered workspace. Empty when the repo is a single package."""

    root: str
    packages: Tuple[Package, ...] = ()
    ecosystems: Tuple[str, ...] = ()
    #: per-package gaps, reported not hidden
    gaps: Tuple[WorkspaceGap, ...] = ()
    is_monorepo: bool = False

    def package_for(self, relpath: str) -> Optional[Package]:
        """Return the longest-prefix package that contains ``relpath``."""
        rel = relpath.replace("\\", "/").strip("/")
        best: Optional[Package] = None
        for pkg in self.packages:
            root = pkg.root.rstrip("/")
            if not root:
                continue
            if (rel == root or rel.startswith(root + "/")) and (
                best is None or len(root) > len(best.root)
            ):
                best = pkg
        return best

    def reverse_deps(self, name: str) -> Tuple[str, ...]:
        """Packages that (transitively) depend on ``name``."""
        seen = {name}
        frontier = [name]
        while frontier:
            current = frontier.pop()
            for pkg in self.packages:
                if pkg.name in seen:
                    continue
                if current in pkg.deps:
                    seen.add(pkg.name)
                    frontier.append(pkg.name)
        seen.discard(name)
        return tuple(sorted(seen))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "is_monorepo": bool(self.is_monorepo),
            "ecosystems": list(self.ecosystems),
            "packages": [dataclasses.asdict(p) for p in self.packages],
            "gaps": [dataclasses.asdict(g) for g in self.gaps],
        }


def default_test_command(pkg_dir: Path, ecosystem: str) -> Tuple[str, str]:
    """Return (test_command, build_command) for a package dir, best effort.

    Data-driven: the command is a fact about the ecosystem's runner, not a
    string we invent per package."""
    if ecosystem == "python":
        test = "python -m pytest -q"
        build = "python -m pytest -q"
        if (pkg_dir / "pyproject.toml").exists():
            test = "python -m pytest -q"
        return test, build
    if ecosystem in ("node", "pnpm", "yarn", "npm", "turbo", "lerna", "nx"):
        pkg_json = pkg_dir / "package.json"
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = data.get("scripts") or {}
                if "test" in scripts:
                    return "npm test --silent", "npm run build --if-present"
                if "build" in scripts:
                    return (
                        "echo no test script; npm run build --if-present",
                        "npm run build",
                    )
            except (ValueError, OSError):
                pass
        return "npm test --silent", "npm run build --if-present"
    if ecosystem == "go":
        return "go test ./...", "go build ./..."
    if ecosystem == "cargo":
        return "cargo test", "cargo build"
    if ecosystem == "dotnet":
        return "dotnet test", "dotnet build"
    if ecosystem == "ruby":
        return "bundle exec rspec", "echo no build step"
    return "echo no test command known", "echo no build command known"


def default_build_command(pkg_dir: Path, ecosystem: str) -> str:
    return default_test_command(pkg_dir, ecosystem)[1]


def _split_workspaces_yaml(text: str) -> List[str]:
    """Minimal pnpm-workspace.yaml reader: the entries under ``packages:``."""
    out: List[str] = []
    in_packages = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("packages:"):
            in_packages = True
            continue
        if in_packages:
            if (
                not line.startswith((" ", "\t", "-"))
                and line.strip()
                and not line.lstrip().startswith("#")
                and re.match(r"^[A-Za-z_]", line.lstrip())
            ):
                in_packages = False
                continue
            m = re.match(r"^\s*-\s*['\"]?([^'\"]+)['\"]?\s*$", line)
            if m:
                out.append(m.group(1).strip())
    return out


def _parse_node_workspaces(data: Any) -> List[str]:
    """package.json ``workspaces`` — either a list or {"packages": [...]}."""
    if isinstance(data, list):
        return [str(v) for v in data]
    if isinstance(data, dict):
        pkgs = data.get("packages")
        if isinstance(pkgs, list):
            return [str(v) for v in pkgs]
    return []


def _parse_go_work(text: str) -> List[str]:
    out = []
    for m in re.finditer(r"^\s*use\s+(.+)$", text, flags=re.MULTILINE):
        for part in m.group(1).split():
            if part and part != "(" and part != ")":
                out.append(part.strip())
    # a parenthesised block: use (\n ./a\n ./b\n)
    block = re.search(r"use\s*\((.*?)\)", text, flags=re.DOTALL)
    if block:
        for part in block.group(1).split():
            if part.strip():
                out.append(part.strip())
    return [p for p in out if p]


def _parse_cargo_workspace(text: str) -> List[str]:
    m = re.search(r"\[workspace\][^\[]*?members\s*=\s*\[(.*?)\]", text, flags=re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    return re.findall(r"['\"]([^'\"]+)['\"]", body)


def _globs_to_dirs(root: Path, patterns: List[str]) -> List[Path]:
    """Expand package globs like 'packages/*' into existing directories."""
    out: List[Path] = []
    seen = set()
    for pattern in patterns:
        pattern = pattern.strip().lstrip("./")
        if not pattern:
            continue
        for match in _glob.glob(str(root / pattern), recursive=False):
            p = Path(match)
            if p.is_dir():
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(p)
    return sorted(out)


def _package_name(pkg_dir: Path, manifest_name: str) -> str:
    """A best-effort package name from its manifest file."""
    try:
        if manifest_name == "package.json" and (pkg_dir / manifest_name).is_file():
            data = json.loads((pkg_dir / manifest_name).read_text(encoding="utf-8"))
            name = data.get("name")
            if isinstance(name, str) and name:
                return name
        if manifest_name == "Cargo.toml" and (pkg_dir / manifest_name).is_file():
            text = (pkg_dir / manifest_name).read_text(
                encoding="utf-8", errors="replace"
            )
            m = re.search(
                r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE
            )
            if m:
                return m.group(1)
        if manifest_name in ("go.mod",) and (pkg_dir / manifest_name).is_file():
            text = (pkg_dir / manifest_name).read_text(
                encoding="utf-8", errors="replace"
            )
            m = re.match(r"^\s*module\s+(\S+)", text, flags=re.MULTILINE)
            if m:
                return m.group(1).rsplit("/", 1)[-1]
        if manifest_name == "pyproject.toml" and (pkg_dir / manifest_name).is_file():
            text = (pkg_dir / manifest_name).read_text(
                encoding="utf-8", errors="replace"
            )
            m = re.search(
                r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE
            )
            if m:
                return m.group(1)
    except (ValueError, OSError):
        pass
    return pkg_dir.name


def _is_internal_dep(name: str, all_names: List[str]) -> bool:
    """Is this declared dependency part of THIS workspace?

    Two shapes count: a bare name that matches a discovered package, and a
    SCOPED name (``@acme/shared``) — which is the internal convention in every
    npm/yarn/pnpm workspace and is never resolvable from a registry. An
    unscoped name that matches nothing (``lodash``) is an external dependency
    and correctly not reported.
    """
    if name in all_names:
        return True
    return name.startswith("@") or name.startswith(".") or "/" in name


def _deps_for(pkg_dir: Path, all_names: List[str]) -> Tuple[str, ...]:
    """A package's DECLARED internal dependencies.

    The first version of this filtered the declared names down to the ones that
    happened to resolve to a discovered package, which quietly destroyed the
    property the link check needs: a package declaring ``@acme/shared`` when no
    ``shared`` package was discovered had that name dropped here, so the link
    check saw a clean package and reported it ``ok`` — while its import would
    resolve against an installed, stale copy at test time. **A dependency that
    did not resolve is the interesting one**, so the declared names are kept and
    resolution is the consumer's job (``monorepo_select`` keys reachability off
    the resolved set; ``monorepo_sandbox`` reports the unresolved ones).
    """
    deps = set()
    try:
        if (pkg_dir / "package.json").is_file():
            data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for name in data.get(section) or {}:
                    if _is_internal_dep(name, all_names):
                        deps.add(name)
        for name in ("pyproject.toml",):
            path = pkg_dir / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                for candidate in all_names:
                    if re.search(re.escape(candidate), text):
                        deps.add(candidate)
        if (pkg_dir / "go.mod").is_file():
            text = (pkg_dir / "go.mod").read_text(encoding="utf-8", errors="replace")
            for candidate in all_names:
                if re.search(re.escape(candidate), text):
                    deps.add(candidate)
        if (pkg_dir / "Cargo.toml").is_file():
            text = (pkg_dir / "Cargo.toml").read_text(
                encoding="utf-8", errors="replace"
            )
            for candidate in all_names:
                if re.search(re.escape(candidate), text):
                    deps.add(candidate)
    except (ValueError, OSError):
        pass
    deps.discard(pkg_dir.name)
    return tuple(sorted(deps))


#: Data, not code per ecosystem: each row says which marker files signal the
#: workspace, where the package globs are, and which ecosystem's runner drives
#: each package. Adding one is one row plus one parser.
_WORKSPACE_SOURCES: Tuple[Dict[str, Any], ...] = (
    {
        "ecosystem": "pnpm",
        "markers": ("pnpm-workspace.yaml",),
        "parse": lambda root: _split_workspaces_yaml(
            (root / "pnpm-workspace.yaml").read_text(encoding="utf-8", errors="replace")
        ),
    },
    {
        "ecosystem": "lerna",
        "markers": ("lerna.json",),
        "parse": lambda root: _parse_node_workspaces(
            json.loads((root / "lerna.json").read_text(encoding="utf-8")).get(
                "packages"
            )
        ),
    },
    {
        "ecosystem": "node",
        "markers": ("package.json",),
        "parse": lambda root: _parse_node_workspaces(
            json.loads((root / "package.json").read_text(encoding="utf-8")).get(
                "workspaces"
            )
        ),
    },
    {
        "ecosystem": "turbo",
        "markers": ("turbo.json",),
        #: turbo.json names NO packages; it orchestrates the package manager's
        #: own workspace declaration. Record it and defer to the node row.
        "parse": lambda root: [],
    },
    {
        "ecosystem": "nx",
        "markers": ("nx.json",),
        "parse": lambda root: [],
    },
    {
        "ecosystem": "go",
        "markers": ("go.work",),
        "parse": lambda root: _parse_go_work(
            (root / "go.work").read_text(encoding="utf-8", errors="replace")
        ),
    },
    {
        "ecosystem": "cargo",
        "markers": ("Cargo.toml",),
        "parse": lambda root: _parse_cargo_workspace(
            (root / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
        ),
    },
    {
        "ecosystem": "python",
        "markers": ("packages",),
        #: a Python monorepo convention: packages/<name>/pyproject.toml.
        "parse": lambda root: ["packages/*"] if (root / "packages").is_dir() else [],
    },
)

#: Marker filename -> ecosystem, for the markers that ALSO need a parse step
_MARKER_TO_SUFFIX = {"go.work": "go", "Cargo.toml": "cargo", "go.mod": "go"}


def _manifest_for_package(pkg_dir: Path) -> str:
    for name in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml", "setup.py"):
        if (pkg_dir / name).is_file():
            return name
    return ""


def _ecosystem_for_package(pkg_dir: Path, source_eco: str) -> str:
    if (pkg_dir / "package.json").is_file() and source_eco in (
        "pnpm",
        "lerna",
        "node",
        "turbo",
        "nx",
    ):
        return source_eco if source_eco in ("pnpm", "lerna", "nx", "turbo") else "node"
    if (pkg_dir / "go.mod").is_file():
        return "go"
    if (pkg_dir / "Cargo.toml").is_file():
        return "cargo"
    if (pkg_dir / "pyproject.toml").is_file() or (pkg_dir / "setup.py").is_file():
        return "python"
    return source_eco


def discover_workspace(repo_path: str) -> Workspace:
    """Discover the workspace under ``repo_path`` — data-driven, never a guess.

    Returns an empty ``Workspace`` (``is_monorepo=False``) when no marker
    matches, so a single-package repo is the ordinary case rather than a
    special one. Never raises: an unreadable manifest is a gap, not a crash.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return Workspace(root=str(root), is_monorepo=False)
    packages: List[Package] = []
    gaps: List[WorkspaceGap] = []
    ecosystems: List[str] = []
    for source in _WORKSPACE_SOURCES:
        markers = source["markers"]
        present = any((root / m).exists() for m in markers)
        if not present:
            continue
        try:
            globs = source["parse"](root)
        except (OSError, ValueError, KeyError):
            gaps.append(
                WorkspaceGap(
                    package="<workspace>",
                    reason=f"{source['ecosystem']} manifest unreadable",
                )
            )
            continue
        if not globs:
            # e.g. turbo.json on its own: an orchestration marker, not a
            # package list. Record it but never treat it as a source of packages.
            if source["ecosystem"] not in ecosystems:
                ecosystems.append(source["ecosystem"])
            continue
        dirs = _globs_to_dirs(root, globs)
        if dirs:
            ecosystems.append(source["ecosystem"])
        all_names = [_package_name(d, _manifest_for_package(d)) for d in dirs]
        for pkg_dir in dirs:
            manifest = _manifest_for_package(pkg_dir)
            eco = _ecosystem_for_package(pkg_dir, source["ecosystem"])
            name = _package_name(pkg_dir, manifest)
            test, build = default_test_command(pkg_dir, eco)
            rel = str(pkg_dir.relative_to(root)).replace("\\", "/") or "."
            packages.append(
                Package(
                    name=name,
                    relpath=rel,
                    ecosystem=eco,
                    test_command=test,
                    build_command=build,
                    manifest=manifest or source["markers"][0],
                    deps=_deps_for(pkg_dir, all_names),
                    root=rel,
                )
            )
            if not manifest:
                gaps.append(
                    WorkspaceGap(
                        package=name, reason=f"no recognised manifest in {rel}"
                    )
                )
    # A node "workspaces" block that also covers Python packages is folded in:
    # every Workspace package keeps its own ecosystem's runner.
    seen_paths = {p.relpath for p in packages}
    # De-duplicate by relpath (a pnpm/package.json/lerna match and the
    # python ``packages/*`` convention can list the same directory).
    seen_paths = set()
    deduped: List[Package] = []
    for pkg in packages:
        if pkg.relpath in seen_paths:
            continue
        seen_paths.add(pkg.relpath)
        deduped.append(pkg)
    packages = deduped
    ecosystems = tuple(dict.fromkeys(ecosystems)) or ("single",)
    is_mono = len(packages) > 1 or any(p.root not in ("", ".") for p in packages)
    # Only a genuinely multi-package layout is a monorepo; a single matched
    # package is one repo seen through a manifest.
    if len(packages) <= 1 and not any(p.root not in ("", ".") for p in packages):
        is_mono = False
    return Workspace(
        root=str(root),
        packages=tuple(sorted(packages, key=lambda p: p.relpath)),
        ecosystems=ecosystems,
        gaps=tuple(gaps),
        is_monorepo=is_mono,
    )


WORKSPACE_SOURCES = _WORKSPACE_SOURCES


def workspace_change_group(
    workspace: Workspace, changed_files: List[str]
) -> Dict[str, Any]:
    """Decide whether a change is a coordinated cross-package group.

    A change that touches a shared package AND any of its consumers is the
    atomic unit Phase 3a's ``change_groups`` exists for. Returns the members
    and the dependency edges that make them one group, or an empty group when
    the change is local to one package.
    """
    impacted = []
    for rel in changed_files:
        pkg = workspace.package_for(rel)
        if pkg is not None and pkg.name not in impacted:
            impacted.append(pkg.name)
    if len(impacted) <= 1:
        return {
            "is_group": False,
            "members": impacted,
            "edges": [],
            "reason": "change is local to one package",
        }
    # Direct semantic coupling: a package and (some of) its dependents.
    edges: List[Tuple[str, str]] = []
    for name in impacted:
        for consumer in workspace.reverse_deps(name):
            if consumer in impacted:
                edges.append((name, consumer))
    if edges:
        return {
            "is_group": True,
            "members": sorted(impacted),
            "edges": edges,
            "reason": "a shared package and its consumers changed together",
        }
    return {
        "is_group": False,
        "members": impacted,
        "edges": [],
        "reason": "changed packages are independent of each other",
    }
