"""T5.W1.1 — cross-package test selection that reports its coverage.

``execution.test_selection`` selects tests *within* a repo. This module runs
it **per package** and keys the answer to the workspace's dependency graph:

* a change in ``packages/api`` must not run ``packages/web``'s tests;
* a change in a shared package must run every consumer's tests;
* every package the change could not reach is **named as skipped** — a
  green "verified" that silently covered only half the packages is the
  Phase-3a false-verified-success class wearing a monorepo's clothes.

The skip list is part of the receipt, not a caveat.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from execution.test_selection import TestSelection, select_tests
from execution.workspace_map import Package, Workspace, discover_workspace

__all__ = [
    "MonorepoSelection",
    "PackageSelection",
    "coverage_receipt_across_packages",
    "select_tests_across_packages",
]


@dataclass(frozen=True)
class PackageSelection:
    """What ran (or did not) inside one package."""

    package: str
    relpath: str
    tests: Tuple[str, ...] = ()
    total_tests: int = 0
    reason: str = ""  # why this set ran, or why nothing ran here
    skipped: bool = False  # True => this package was NOT exercised


@dataclass(frozen=True)
class MonorepoSelection:
    """The whole run, per package, with coverage reported — never assumed."""

    workspace: Workspace
    changed_files: Tuple[str, ...] = ()
    per_package: Tuple[PackageSelection, ...] = ()
    #: packages whose tests were intentionally NOT run
    skipped_packages: Tuple[str, ...] = ()
    total_selected: int = 0
    #: a package whose tests selection could not be reasoned about
    unanalysable: Tuple[str, ...] = ()

    @property
    def ran_packages(self) -> Tuple[str, ...]:
        return tuple(p.package for p in self.per_package if not p.skipped)

    def coverage_receipt(self) -> Dict[str, Any]:
        return {
            "changed_files": list(self.changed_files),
            "per_package": [dataclasses.asdict(p) for p in self.per_package],
            "ran_packages": list(self.ran_packages),
            "skipped_packages": list(self.skipped_packages),
            "unanalysable": list(self.unanalysable),
            "total_selected": self.total_selected,
            "complete": bool(self.per_package) and not self.unanalysable,
            "note": (
                f"verified {', '.join(self.ran_packages) or '(none)'}; "
                + (
                    f"skipped {', '.join(self.skipped_packages)} "
                    "(no dependency path)"
                    if self.skipped_packages
                    else ""
                )
            ).strip(),
        }


def _package_relfiles(package: Package, changed: Sequence[str]) -> List[str]:
    root = package.root.rstrip("/")
    out = []
    for rel in changed:
        rel = rel.replace("\\", "/")
        if root in ("", "."):
            out.append(rel)
        elif rel == root or rel.startswith(root + "/"):
            out.append(rel[len(root) + 1:] if rel.startswith(root + "/") else rel)
    return out


def select_tests_across_packages(
    repo_path: str,
    changed_files: Sequence[str],
    *,
    workspace: Optional[Workspace] = None,
    max_files: int = 12,
) -> MonorepoSelection:
    """Select tests across packages, keyed by dependency reachability.

    A package runs tests iff it contains a changed file OR it (transitively)
    consumes a changed package. Every other package is reported skipped by
    name — never silently folded into "verified."
    """
    root = str(repo_path)
    ws = workspace or discover_workspace(root)
    changed = tuple(str(c).replace("\\", "/") for c in changed_files)
    if not ws.is_monorepo or not ws.packages:
        # Single-package repo: delegate, unchanged semantics.
        sel: TestSelection = select_tests(root, changed)
        return MonorepoSelection(
            workspace=ws,
            changed_files=changed,
            per_package=(
                PackageSelection(
                    package="(repo)",
                    relpath=".",
                    tests=tuple(sel.files),
                    total_tests=sel.total_tests,
                    reason=sel.strategy,
                ),
            ),
            skipped_packages=(),
            total_selected=len(sel.files),
        )

    # Which packages contain a changed file?
    directly_touched = []
    for pkg in ws.packages:
        if _package_relfiles(pkg, changed):
            directly_touched.append(pkg)
    # Which packages must run because a dependency they rely on changed?
    consumers_of = set()
    for pkg in directly_touched:
        for dependent in ws.reverse_deps(pkg.name):
            consumers_of.add(dependent)
    must_run = {p.name for p in directly_touched} | consumers_of

    per_package: List[PackageSelection] = []
    skipped: List[str] = []
    unanalysable: List[str] = []
    total = 0
    for pkg in ws.packages:
        if pkg.name not in must_run:
            skipped.append(pkg.name)
            per_package.append(
                PackageSelection(
                    package=pkg.name,
                    relpath=pkg.relpath,
                    reason="skipped (no dependency path)",
                    skipped=True,
                )
            )
            continue
        pkg_changed = _package_relfiles(pkg, changed)
        pkg_root = str(Path(root) / pkg.relpath) if pkg.relpath not in ("", ".") else root
        try:
            sel = select_tests(pkg_root if os.path.isdir(pkg_root) else root,
                               pkg_changed or [pkg.relpath + "/"])
            per_package.append(
                PackageSelection(
                    package=pkg.name,
                    relpath=pkg.relpath,
                    tests=tuple(sel.files),
                    total_tests=sel.total_tests,
                    reason=sel.strategy,
                )
            )
            total += len(sel.files)
        except Exception as exc:  # reported, not raised into a verify
            unanalysable.append(pkg.name)
            per_package.append(
                PackageSelection(
                    package=pkg.name,
                    relpath=pkg.relpath,
                    reason=f"selection unavailable: {exc}",
                    skipped=True,
                )
            )
    return MonorepoSelection(
        workspace=ws,
        changed_files=changed,
        per_package=tuple(per_package),
        skipped_packages=tuple(sorted(skipped)),
        total_selected=total,
        unanalysable=tuple(unanalysable),
    )


def coverage_receipt_across_packages(
    repo_path: str, changed_files: Sequence[str], workspace: Optional[Workspace] = None
) -> Dict[str, Any]:
    """The coverage receipt a reviewer reads: what ran, what skipped, why."""
    return select_tests_across_packages(repo_path, changed_files, workspace=workspace).coverage_receipt()
