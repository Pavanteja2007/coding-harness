"""CI truth: prove that CI actually runs what it claims to run.

Three claims drift silently in every repo:

* "we run the tests"  — while the workflow names six of ninety-two files.
* "the full suite is in the release gate" — while the release aggregate
  needs a lane that runs a subset.
* "timing-sensitive tests are flake-tested" — while nothing repeats them.

This module is the machine-checkable gate for all three. It is
deliberately conservative: a bare ``pytest tests/`` invocation counts as
covering a file *only* when that invocation lives in a job the release
aggregate actually depends on. A full-suite run nobody requires is not
coverage.

Rules implemented (each is a required check, not advice):

    every_test_file_covered        every tests/test_*.py is run by some
                                   workflow, or is on an explicit allowlist
    full_suite_in_release_gate     the release aggregate needs a lane that
                                   runs the whole suite
    required_checks_declared       every required PR check name exists in a
                                   pull_request-triggering workflow
    security_lane_in_pr_path       security regressions run on pull_request
    flake_lane_covers_timing       every declared timing-sensitive test file
                                   is repeated by a lane
    manual_checks_are_manual       opt-in live lanes are not required PR
                                   checks and cannot gate a merge

CLI:
    python -m evals.ci_truth --check --json     # exit 2 on any gap
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Stable job names a repository administrator marks as required PR checks.
#: Declaring a name here is a promise; the gate fails if a workflow stops
#: providing it, so a renamed or deleted lane cannot quietly un-gate a PR.
REQUIRED_PR_CHECKS: Tuple[str, ...] = (
    "prompt-regression-matrix",
    "hostless security regressions",
)

#: Test files deliberately not attached to any workflow, with the reason.
#: Anything not in a workflow and not here is a coverage gap, not a pass.
TEST_ALLOWLIST: Dict[str, str] = {}

#: Tests whose pass/fail depends on wall-clock or process scheduling. They
#: must be repeated by a flake lane before their result is believed.
TIMING_SENSITIVE_TESTS: Dict[str, str] = {
    "tests/test_scheduler_integration.py": "kill/resume and worker-lifecycle timing",
    "tests/test_tui_contract.py": "live Textual frame and drain timing",
    "tests/test_cli_tui.py": "async render-loop timing",
    "tests/test_sandbox.py": "container command timeouts",
    "tests/test_webfetch.py": "network timeout behaviour",
    "tests/test_orchestration.py": "worktree/DAG concurrency timing",
    "tests/test_provider_smoke.py": "live provider latency",
}

#: Lanes that need real Docker, real providers, or manual evidence. These
#: are opt-in and scheduled; requiring them as a PR check would either be
#: skipped (dishonest) or block every PR on infrastructure.
MANUAL_ONLY_LANES: Tuple[str, ...] = (
    "live-quality-nightly",
    "nightly",
    "manual",
)

_TEST_FILE = re.compile(r"^tests/(test_[A-Za-z0-9_]+\.py)$")
_PYTEST_WHOLE_TREE = re.compile(r"pytest[^\n]*\btests/(?![A-Za-z0-9_])")
_TEST_REF = re.compile(r"tests/test_[A-Za-z0-9_]+\.py")
_REPEAT_LOOP = re.compile(r"^\s*(for|while)\b", re.MULTILINE)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_inventory(root: Path) -> List[str]:
    """Every ``tests/test_*.py`` in the repository, sorted and repo-relative."""
    base = Path(root)
    directory = base / "tests"
    if not directory.is_dir():
        return []
    return sorted(
        f"tests/{entry.name}"
        for entry in directory.iterdir()
        if entry.is_file()
        and not entry.is_symlink()
        and _TEST_FILE.match(f"tests/{entry.name}")
    )


def workflow_paths(root: Path) -> List[Path]:
    """Every workflow file, sorted."""
    directory = Path(root) / ".github" / "workflows"
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix in (".yml", ".yaml")
    )


def workflow_documents(root: Path) -> Dict[str, str]:
    """``{relative path: text}`` for every workflow."""
    documents: Dict[str, str] = {}
    for path in workflow_paths(root):
        try:
            documents[str(path.relative_to(Path(root))).replace("\\", "/")] = (
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
    return documents


# ---------------------------------------------------------------------------
# Workflow parsing (line-oriented; deliberately not a YAML dependency)
# ---------------------------------------------------------------------------


def _job_blocks(text: str) -> Dict[str, str]:
    """Map top-level job name -> raw job body."""
    jobs: Dict[str, str] = {}
    current: Optional[str] = None
    lines: List[str] = []
    in_jobs = False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^[A-Za-z_]", line):
            break
        if not in_jobs:
            continue
        match = re.match(r"^ {2}([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            if current:
                jobs[current] = "\n".join(lines)
            current = match.group(1)
            lines = [match.group(2)]
            continue
        if current is not None:
            lines.append(line)
    if current:
        jobs[current] = "\n".join(lines)
    return jobs


def _job_name(block: str) -> str:
    match = re.search(r"^\s*name:\s*(.+)$", block, re.MULTILINE)
    return match.group(1).strip().strip("'\"") if match else ""


def _triggers_pull_request(text: str) -> bool:
    match = re.search(r"^on:\s*(.*)$", text, re.MULTILINE)
    if not match:
        return False
    head = match.group(1)
    if "pull_request" in head:
        return True
    return bool(re.search(r"^  pull_request:", text, re.MULTILINE))


def _triggers_schedule(text: str) -> bool:
    return bool(re.search(r"^\s*schedule:\s*$", text, re.MULTILINE))


def _test_refs(text: str) -> Set[str]:
    return set(_TEST_REF.findall(text))


def _runs_whole_tree(text: str) -> bool:
    return bool(_PYTEST_WHOLE_TREE.search(text))


def _is_repeat_lane(text: str) -> bool:
    return bool(_REPEAT_LOOP.search(text)) and "pytest" in text


def aggregate_needs(release_gate_text: str) -> Set[str]:
    """Job keys the release verdict job depends on."""
    jobs = _job_blocks(release_gate_text)
    for key, block in jobs.items():
        if (
            "release gate verdict" in _job_name(block).casefold()
            or key == "release-gate"
        ):
            body = re.search(r"needs:\s*\n((?:\s*-\s*\S+\s*\n)+)", block)
            if body:
                return {
                    line.strip().lstrip("- ").strip()
                    for line in body.group(1).splitlines()
                    if line.strip()
                }
            inline = re.search(r"needs:\s*\[([^\]]*)\]", block)
            if inline:
                return {
                    item.strip().strip("'\"")
                    for item in inline.group(1).split(",")
                    if item.strip()
                }
    return set()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def coverage_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Which test files CI actually runs, and why any file is uncovered."""
    base = Path(root)
    inventory = test_inventory(base)
    documents = workflow_documents(base)
    release_gate = next(
        (text for name, text in documents.items() if "release-gate" in name), ""
    )
    required_jobs = aggregate_needs(release_gate)

    whole_tree_jobs: List[str] = []
    referenced: Set[str] = set()
    for name, text in documents.items():
        for key, block in _job_blocks(text).items():
            label = f"{name}#{key}"
            if not _TEST_REF.search(block) and not _runs_whole_tree(block):
                continue
            if _runs_whole_tree(block) and label.split("#")[-1] in required_jobs:
                whole_tree_jobs.append(label)
                referenced.update(inventory)
                continue
            referenced.update(_test_refs(block))

    covered = sorted(path for path in inventory if path in referenced)
    uncovered = sorted(
        path
        for path in inventory
        if path not in referenced and path not in TEST_ALLOWLIST
    )
    allowlisted = sorted(path for path in inventory if path in TEST_ALLOWLIST)
    return {
        "n_test_files": len(inventory),
        "covered": covered,
        "covered_count": len(covered),
        "uncovered": uncovered,
        "uncovered_count": len(uncovered),
        "allowlisted": allowlisted,
        "allowlist_reasons": {
            path: TEST_ALLOWLIST[path] for path in allowlisted if path in TEST_ALLOWLIST
        },
        "whole_tree_lanes": sorted(whole_tree_jobs),
        "referenced_test_files": sorted(referenced & set(inventory)),
        "required_release_jobs": sorted(required_jobs),
        "every_test_file_covered": not uncovered,
    }


def release_gate_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Does the release aggregate really require the full suite?"""
    base = Path(root)
    documents = workflow_documents(base)
    release_gate = next(
        (text for name, text in documents.items() if "release-gate" in name), ""
    )
    jobs = _job_blocks(release_gate)
    required_jobs = aggregate_needs(release_gate)
    full_suite_lanes: List[str] = []
    for key in sorted(required_jobs):
        block = jobs.get(key)
        if not block:
            continue
        if _runs_whole_tree(block) and "pytest" in block:
            full_suite_lanes.append(key)
    referenced = _test_refs(release_gate)
    return {
        "workflow_present": bool(release_gate),
        "required_jobs": sorted(required_jobs),
        "full_suite_lanes": full_suite_lanes,
        "full_suite_in_release_gate": bool(full_suite_lanes),
        "explicitly_named_test_files": sorted(referenced),
        "n_required_jobs": len(required_jobs),
    }


def required_check_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Every declared required check must exist in a PR-triggering workflow."""
    base = Path(root)
    documents = workflow_documents(base)
    available: Dict[str, List[str]] = {}
    manual_names: Set[str] = set()
    for name, text in documents.items():
        if not _triggers_pull_request(text):
            continue
        for key, block in _job_blocks(text).items():
            label = _job_name(block) or key
            available.setdefault(label, []).append(f"{name}#{key}")
            lowered = label.casefold()
            if any(marker in lowered for marker in MANUAL_ONLY_LANES):
                manual_names.add(label)
    missing = [name for name in REQUIRED_PR_CHECKS if name not in available]
    required = set(REQUIRED_PR_CHECKS)
    return {
        "declared_required_checks": list(REQUIRED_PR_CHECKS),
        "available_pr_check_names": sorted(available),
        "missing_required_checks": missing,
        "manual_only_checks": sorted(manual_names),
        "manual_checks_are_manual": not (required & manual_names),
        "ok": not missing and not (required & manual_names),
    }


def security_lane_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Security regression tests must run in the pull-request path."""
    base = Path(root)
    documents = workflow_documents(base)
    lanes: List[str] = []
    for name, text in documents.items():
        if not _triggers_pull_request(text):
            continue
        for key, block in _job_blocks(text).items():
            refs = _test_refs(block)
            if not any(
                "security" in ref or "workspace_security" in ref for ref in refs
            ):
                continue
            lanes.append(f"{name}#{key}")
    return {
        "security_lanes": sorted(lanes),
        "security_lane_in_pr_path": bool(lanes),
    }


def flake_lane_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Every declared timing-sensitive file must be repeated by a lane."""
    base = Path(root)
    documents = workflow_documents(base)
    repeated: Set[str] = set()
    lanes: List[str] = []
    for name, text in documents.items():
        for key, block in _job_blocks(text).items():
            if not _is_repeat_lane(block):
                continue
            refs = _test_refs(block)
            if not refs:
                continue
            repeated.update(refs)
            lanes.append(f"{name}#{key}")
    declared = sorted(TIMING_SENSITIVE_TESTS)
    missing = [path for path in declared if path not in repeated]
    return {
        "declared_timing_sensitive": declared,
        "reasons": dict(TIMING_SENSITIVE_TESTS),
        "flake_lanes": sorted(lanes),
        "repeated_test_files": sorted(repeated),
        "missing_from_flake_lane": missing,
        "ok": not missing,
    }


def timing_sensitive_test_inventory(root: Path = REPO_ROOT) -> List[str]:
    """Declared timing-sensitive files that actually exist on disk."""
    base = Path(root)
    return [path for path in sorted(TIMING_SENSITIVE_TESTS) if (base / path).is_file()]


def ci_truth_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """All CI-truth checks plus the single overall verdict."""
    base = Path(root)
    coverage = coverage_report(base)
    release = release_gate_report(base)
    required = required_check_report(base)
    security = security_lane_report(base)
    flake = flake_lane_report(base)
    checks: Dict[str, bool] = {
        "every_test_file_covered": bool(coverage["every_test_file_covered"]),
        "full_suite_in_release_gate": bool(release["full_suite_in_release_gate"]),
        "required_checks_declared": bool(required["ok"]),
        "security_lane_in_pr_path": bool(security["security_lane_in_pr_path"]),
        "flake_lane_covers_timing": bool(flake["ok"]),
        "manual_checks_are_manual": bool(required["manual_checks_are_manual"]),
    }
    errors: List[Dict[str, str]] = []
    for path in coverage["uncovered"]:
        errors.append(
            {
                "code": "test_file_not_in_workflow",
                "message": f"{path} runs in no workflow and is not on the allowlist",
            }
        )
    if not release["full_suite_in_release_gate"]:
        errors.append(
            {
                "code": "full_suite_not_required",
                "message": "no release-required lane runs the whole test suite",
            }
        )
    for name in required["missing_required_checks"]:
        errors.append(
            {
                "code": "missing_required_check",
                "message": f"required PR check {name!r} is not provided by any pull_request workflow",
            }
        )
    if not security["security_lane_in_pr_path"]:
        errors.append(
            {
                "code": "security_lane_missing",
                "message": "no pull_request workflow runs the security regressions",
            }
        )
    for path in flake["missing_from_flake_lane"]:
        errors.append(
            {
                "code": "timing_test_not_flake_tested",
                "message": f"{path} is timing-sensitive but no lane repeats it",
            }
        )
    return {
        "schema_version": 1,
        "ok": not errors,
        "verdict": "CI_TRUTHFUL" if not errors else "CI_GAPS",
        "checks": checks,
        "coverage": coverage,
        "release_gate": release,
        "required_checks": required,
        "security": security,
        "flake": flake,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Print the CI-truth report; ``--check`` exits 2 on any gap."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.ci_truth",
        description="Prove CI runs what it claims to run.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--check", action="store_true", help="exit 2 on any gap")
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output only"
    )
    args = parser.parse_args(argv)
    report = ci_truth_report(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"ci truth: {report['verdict']}")
        for key, value in report["checks"].items():
            print(f"  {'ok  ' if value else 'GAP '} {key}")
        for error in report["errors"]:
            print(f"  {error['code']}: {error['message']}")
    if args.check and not report["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
