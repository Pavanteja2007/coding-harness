"""One release-evidence report, and a publish gate that requires a human.

A release claim is only worth what its evidence is. This module aggregates
every release fact the repository can produce into ONE report with ONE
verdict, so "is this releasable" has a single answer instead of five logs
that each say something different.

What goes in, and why each is here:

- **source state** - a release artifact must be built from an identified,
  stable tree. ``scripts.verify_release`` already computes this; a dirty
  checkout is reported, never ignored.
- **reproducibility** - two independent builds must agree, or the artifact
  is not a release, it is a lottery ticket.
- **SBOM** - what is actually inside the wheel.
- **vulnerability scan** - ``shared.supply_chain.scan_dependencies`` over the
  declared requirements, so a known-bad pin is a finding rather than a
  surprise.
- **clean-room install** - the artifact must install and run with nothing
  from the checkout on the path.
- **installed-wheel flow** - the wheel's own surfaces must work, not just
  import.
- **full test evidence** - a release whose test lane was skipped is not
  green. A skipped lane is reported ``skipped`` and is never counted as a
  pass, which is the whole point of tracking lanes separately.

**Publishing is a separate, human-gated step.** ``publish_gate`` refuses
without an explicit approval token AND a green report, and this module never
invokes an uploader. ``python -m scripts.release_evidence`` cannot publish
anything, by construction, because nothing in it shells out to twine.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "LANES",
    "LaneResult",
    "ReleaseReport",
    "approval_token",
    "collect_report",
    "main",
    "publish_gate",
    "run_lane",
]

SCHEMA = "neo.release_evidence/1"

#: Lane verdicts. ``skipped`` exists so a lane nobody selected can never be
#: folded into a pass.
PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"
UNEVALUATED = "unevaluated"

#: Declared lanes, in report order. Each is (name, why it matters).
LANES: Sequence[str] = (
    "source_state",
    "reproducibility",
    "sbom",
    "vulnerability_scan",
    "clean_room_install",
    "installed_wheel_flow",
    "full_test_suite",
    "docs_truth",
    "capability_probe",
)

#: Every declared lane is BLOCKING. A skipped lane is not a pass, and a
#: pass requires evidence from every lane - so the honest way to run this
#: without a candidate artifact is to be told "NOT RELEASABLE" with a list
#: of what is missing, rather than to receive a green report that quietly
#: skipped the expensive half. The per-lane ``blocking`` flag is retained in
#: the report because a reader wants to see the policy, but the verdict does
#: not consult it.
BLOCKING_LANES = frozenset(LANES)

#: Environment flag that names the human approval for publishing. Absent
#: means no approval, and no approval means no publish.
APPROVAL_ENV = "NEO_RELEASE_APPROVED"

#: The exact phrase a human must set. A bare "1" is not consent to ship.
APPROVAL_PHRASE = "publish"


@dataclass
class LaneResult:
    """One lane's measured outcome."""

    name: str
    status: str
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    blocking: bool = True

    @property
    def counted_as_pass(self) -> bool:
        """Whether this lane may be counted as green."""
        return self.status == PASS

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable record."""
        return {
            "name": self.name,
            "status": self.status,
            "blocking": self.blocking,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass
class ReleaseReport:
    """The aggregate: every lane, one verdict."""

    version: str
    lanes: List[LaneResult] = field(default_factory=list)

    @property
    def by_name(self) -> Dict[str, LaneResult]:
        """Lanes keyed by name."""
        return {lane.name: lane for lane in self.lanes}

    @property
    def skipped(self) -> List[str]:
        """Lane names that were not run. Never reported as passes."""
        return [lane.name for lane in self.lanes if lane.status == SKIPPED]

    @property
    def failures(self) -> List[str]:
        """Lanes that ran and failed."""
        return [lane.name for lane in self.lanes if lane.status == FAIL]

    @property
    def releasable(self) -> bool:
        """Whether EVERY lane produced a pass.

        No lane is exempt. A lane that failed, a lane that was skipped, and
        a lane that could not be evaluated are all "not a pass", because the
        three ways of not having evidence are the same thing to a user
        deciding whether to ship.
        """
        return all(lane.status == PASS for lane in self.lanes) and bool(self.lanes)

    def to_dict(self) -> Dict[str, Any]:
        """Return the machine-readable report."""
        return {
            "schema": SCHEMA,
            "version": self.version,
            "releasable": self.releasable,
            "status": PASS if self.releasable else FAIL,
            "failures": self.failures,
            "skipped": self.skipped,
            "unevaluated": [
                lane.name for lane in self.lanes if lane.status == UNEVALUATED
            ],
            "lanes": [lane.to_dict() for lane in self.lanes],
        }


def approval_token() -> str:
    """The human's publish approval, or "" when there is none."""
    import os

    return str(os.environ.get(APPROVAL_ENV) or "").strip()


def publish_gate(report: ReleaseReport) -> Dict[str, Any]:
    """Decide whether a publish may proceed, and say why.

    Requires BOTH a green report and an explicit human approval phrase. It
    never performs the upload: this function is a decision, and the caller
    is a human running an uploader they chose. Returning
    ``approved: true`` is a statement that the preconditions are met, not
    that anything was published.
    """
    token = approval_token()
    approved = report.releasable and token.lower() == APPROVAL_PHRASE
    reasons: List[str] = []
    if not report.releasable:
        reasons.append("the release report is not green")
    if token.lower() != APPROVAL_PHRASE:
        reasons.append(
            f"no explicit human approval (set {APPROVAL_ENV}={APPROVAL_PHRASE} to "
            "approve a publish)"
        )
    return {
        "schema": "neo.publish_gate/1",
        "approved": approved,
        "reasons": reasons,
        "releasable": report.releasable,
        "approval_present": token.lower() == APPROVAL_PHRASE,
        "published_by_this_tool": False,
    }


def _version(root: Path) -> str:
    import re

    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def _run(command: Sequence[str], cwd: Path, timeout: float = 300.0) -> Dict[str, Any]:
    """Run one bounded subprocess and return a receipt.

    Never raises: a lane that cannot run is ``unevaluated``, which the
    aggregate treats as not-green for a blocking lane.
    """
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ran": True,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }


def _lane_source_state(root: Path) -> LaneResult:
    """A release must come from an identified, stable tree."""
    try:
        from scripts.verify_release import _git_provenance

        provenance = _git_provenance(root.resolve(), [])
    except Exception as exc:
        return LaneResult(
            "source_state",
            UNEVALUATED,
            f"git provenance unavailable: {type(exc).__name__}: {exc}",
        )
    available = bool(provenance.get("available"))
    clean = bool(provenance.get("clean"))
    detail = "clean checkout" if clean else "working tree is DIRTY"
    if not available:
        return LaneResult(
            "source_state",
            UNEVALUATED,
            "no git provenance for this tree",
            {"available": False},
        )
    return LaneResult(
        "source_state",
        PASS if clean else FAIL,
        detail,
        {
            "available": True,
            "clean": clean,
            "commit": provenance.get("commit"),
            "tags": provenance.get("tags") or [],
        },
    )


def _lane_docs_truth(root: Path) -> LaneResult:
    """The site and docs must agree with the release version."""
    try:
        from scripts.docs_truth import run_gates
    except Exception as exc:
        return LaneResult("docs_truth", UNEVALUATED, f"docs gate unavailable: {exc}")
    report = run_gates(root)
    failed = [
        f"{gate['gate']}: {finding['path']}: {finding['detail']}"
        for gate in report["gates"]
        if gate["status"] != "pass"
        for finding in gate["findings"][:5]
    ]
    return LaneResult(
        "docs_truth",
        PASS if report["status"] == "pass" else FAIL,
        "; ".join(failed) if failed else "all documentation gates pass",
        {"gates": report["gates"]},
    )


def _lane_capability_probe(root: Path) -> LaneResult:
    """The installed distribution must provide every advertised capability."""
    try:
        from cli.capability import probe_capabilities
    except Exception as exc:
        return LaneResult(
            "capability_probe", UNEVALUATED, f"capability probe unavailable: {exc}"
        )
    report = probe_capabilities(root)
    return LaneResult(
        "capability_probe",
        PASS if report.ok else FAIL,
        "every advertised capability is present"
        if report.ok
        else f"missing: {', '.join(report.missing)}",
        {"version": report.version, "missing": list(report.missing)},
    )


def _lane_vulnerability_scan(root: Path) -> LaneResult:
    """Scan the declared requirements for known-bad pins."""
    try:
        from shared.supply_chain import scan_manifest_file
    except Exception as exc:
        return LaneResult(
            "vulnerability_scan",
            UNEVALUATED,
            f"supply-chain scanner unavailable: {exc}",
        )
    try:
        findings = scan_manifest_file(root / "pyproject.toml")
    except Exception as exc:
        return LaneResult("vulnerability_scan", UNEVALUATED, f"scan failed: {exc}")
    rows = list(findings or [])
    blocking = [
        row
        for row in rows
        if str(getattr(row, "severity", "")).lower() in {"high", "critical"}
    ]
    return LaneResult(
        "vulnerability_scan",
        FAIL if blocking else PASS,
        f"{len(rows)} advisories, {len(blocking)} high/critical",
        {"advisories": len(rows), "high_or_critical": len(blocking)},
    )


def _external_lane(
    name: str,
    command: Sequence[str],
    root: Path,
    *,
    timeout: float,
    blocking: bool = True,
) -> LaneResult:
    """Run an external verification command as one lane.

    A lane that never ran is ``skipped`` when the command is absent and
    ``unevaluated`` when it ran and could not answer. Both are not-green
    for a blocking lane; the distinction matters because "we did not run
    it" and "we ran it and it crashed" are different problems.
    """
    resolved = list(command)
    if shutil_which(resolved[0]) is None:
        return LaneResult(
            name,
            SKIPPED,
            f"{resolved[0]} is not available on this host",
            {},
            blocking=blocking,
        )
    receipt = _run(resolved, root, timeout=timeout)
    if not receipt.get("ran"):
        return LaneResult(
            name,
            UNEVALUATED,
            str(receipt.get("error") or "lane could not run"),
            {},
            blocking=blocking,
        )
    code = int(receipt.get("returncode") or 0)
    return LaneResult(
        name,
        PASS if code == 0 else FAIL,
        f"exit {code}",
        {
            "returncode": code,
            "stdout": receipt.get("stdout", "")[-1500:],
            "stderr": receipt.get("stderr", "")[-1500:],
        },
        blocking=blocking,
    )


def shutil_which(program: str) -> Optional[str]:
    """Locate an executable without importing shutil at module import time."""
    import shutil

    return shutil.which(program)


_LANE_RUNNERS: Dict[str, Callable[[Path], LaneResult]] = {
    "source_state": _lane_source_state,
    "docs_truth": _lane_docs_truth,
    "capability_probe": _lane_capability_probe,
    "vulnerability_scan": _lane_vulnerability_scan,
}


def run_lane(name: str, root: Path, dist: Optional[Path] = None) -> LaneResult:
    """Run one lane by name.

    External lanes are opt-in through the presence of their inputs: a
    release-evidence run without a ``dist`` directory has nothing to
    verify, and reporting those lanes as skipped is the honest answer.
    """
    if name in _LANE_RUNNERS:
        return _LANE_RUNNERS[name](root)
    if name == "reproducibility":
        if dist is None or not Path(dist).is_dir():
            return LaneResult(
                "reproducibility",
                SKIPPED,
                "no dist directory to compare against",
                {"requires": "--dist"},
            )
        return _external_lane(
            name,
            [
                sys.executable,
                "-m",
                "scripts.verify_release",
                "--dist",
                str(dist),
                "--require-clean",
            ],
            root,
            timeout=600.0,
        )
    if name == "sbom":
        if dist is None or not Path(dist).is_dir():
            return LaneResult(
                "sbom",
                SKIPPED,
                "no dist directory to describe",
                {"requires": "--dist"},
            )
        return _external_lane(
            name,
            [
                sys.executable,
                "-m",
                "scripts.verify_release",
                "--dist",
                str(dist),
                "--sbom",
                str(Path(dist) / "sbom.json"),
            ],
            root,
            timeout=600.0,
        )
    if name == "clean_room_install":
        if dist is None or not Path(dist).is_dir():
            return LaneResult(
                "clean_room_install",
                SKIPPED,
                "no candidate artifact to install",
                {"requires": "--dist"},
                blocking=False,
            )
        return _external_lane(
            name,
            [
                sys.executable,
                "-m",
                "scripts.clean_room_matrix",
                "--dist",
                str(dist),
                "--python",
                sys.executable,
            ],
            root,
            timeout=1800.0,
            blocking=False,
        )
    if name == "installed_wheel_flow":
        if dist is None or not Path(dist).is_dir():
            return LaneResult(
                "installed_wheel_flow",
                SKIPPED,
                "no candidate artifact to install",
                {"requires": "--dist"},
                blocking=False,
            )
        return _external_lane(
            name,
            [sys.executable, "-m", "pytest", "tests/test_installed_user_flow.py", "-q"],
            root,
            timeout=1800.0,
            blocking=False,
        )
    if name == "full_test_suite":
        return _external_lane(
            name,
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"],
            root,
            timeout=3600.0,
        )
    return LaneResult(name, UNEVALUATED, f"unknown lane {name!r}")


def collect_report(
    root: Path,
    *,
    dist: Optional[Path] = None,
    lanes: Optional[Sequence[str]] = None,
) -> ReleaseReport:
    """Run every declared lane and return the aggregate report."""
    report = ReleaseReport(version=_version(root))
    for name in lanes or LANES:
        result = run_lane(name, root, dist)
        result.blocking = name in BLOCKING_LANES
        report.lanes.append(result)
    return report


def _render(report: ReleaseReport, gate: Dict[str, Any]) -> str:
    lines = [
        f"release evidence: {report.version} — "
        f"{'RELEASABLE' if report.releasable else 'NOT RELEASABLE'}"
    ]
    for lane in report.lanes:
        mark = {PASS: "OK  ", FAIL: "FAIL", SKIPPED: "SKIP", UNEVALUATED: "????"}[
            lane.status
        ]
        lines.append(f"  {mark} {lane.name} — {lane.detail}")
    not_run = [name for name in report.skipped] + [
        lane.name for lane in report.lanes if lane.status == UNEVALUATED
    ]
    if not_run:
        lines.append(
            f"  {len(not_run)} lane(s) produced NO passing evidence and are "
            f"not passes: " + ", ".join(sorted(set(not_run)))
        )
    lines.append(
        "  publish gate: "
        + ("APPROVED" if gate["approved"] else "REFUSED")
        + ("" if gate["approved"] else " — " + "; ".join(gate["reasons"]))
    )
    lines.append("  this tool never uploads; a human performs any publish.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Aggregate release evidence with stable exit codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--dist", type=Path, help="candidate artifact directory")
    parser.add_argument(
        "--lane",
        action="append",
        choices=sorted(set(LANES)),
        help="run only these lanes (repeatable)",
    )
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = collect_report(
        args.project_root.resolve(),
        dist=args.dist.resolve() if args.dist else None,
        lanes=args.lane,
    )
    gate = publish_gate(report)
    payload = {**report.to_dict(), "publish_gate": gate}
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2) if args.json else _render(report, gate))
    return 0 if report.releasable else 2


if __name__ == "__main__":
    sys.exit(main())
