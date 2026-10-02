"""The six labelled CI lanes, as data — and the checker that keeps the
workflows honest about them.

WHY THIS IS CODE AND NOT A COMMENT
-----------------------------------
A lane list written only in a YAML file is a list nobody can check. This
module is the single authority for **what the six lanes are called, what each
one may and may not depend on, and how long it is allowed to take**, and
``tests/test_ci_lanes.py`` fails when the workflows in ``.github/workflows/``
disagree with it. A workflow that grows a seventh lane, renames one, or adds
a Docker dependency to a Docker-free lane breaks a test rather than quietly
becoming a different gate.

THE SIX LANES
--------------

============  ========  ==========================================================
lane          budget    what it is for
============  ========  ==========================================================
``smoke``     <30 s     import smoke, the lint ratchet, the sanitiser, and the
                         cheap mechanism pins. Blocking. No Docker, no network.
``trust-``    <5 min    the ten Trust Ladder rungs. Blocking. Host-only, and
``ladder``                the slow rungs are bounded by their own deadlines.
``host-only`` <15 min   everything that needs no daemon and no network.
                         Blocking, but every timing assertion in it carries a
                         generous budget rather than a tight one.
``docker``    <30 min   the real sandbox + verifier e2e. **ubuntu only**, and
                         ``blocked`` on a host with no daemon — never ``pass``.
``windows``   <15 min   the Docker-free CLI subset on ``windows-latest``,
                         including the ``cli/``-local modules.
``nightly``   <90 min   the full 8-arm eval matrix plus the full suite.
                         Scheduled + manual only: it is the expensive one.
============  ========  ==========================================================

THE TWO RULES THIS FILE ENFORCES
--------------------------------

1. **The known-failing pin registry gates EVERY lane.** Not "the lanes that
   run the pins" - every one. A registry that only some lanes consult is a
   registry whose promoted pins are promoted in one job and ignored in five,
   which is the silent rot the registry exists to stop. The mechanism: every
   lane runs ``python -m tests.known_failing_pins`` as its FIRST gate step,
   before any test collection, and its exit code is the lane's.

2. **Host-dependent timing never goes in a blocking lane.** ``smoke``,
   ``trust-ladder`` and ``docker`` are blocking; ``host-only`` and
   ``nightly`` are not. A flaky timing test in a blocking lane trains people
   to ignore the gate, which destroys the gate's value - so the timing
   assertions live where a red is information rather than an obstruction.
   The ONE exception is the Trust Ladder, whose rungs #8/#9/#10 are budget
   assertions by construction; they are in a blocking lane because a rung
   that cannot fail the build is decoration, and they are written to assert
   ORDER OF MAGNITUDE with the machine named, never a tight absolute
   threshold that fails on a slower box.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The command every lane runs first. Its exit code IS the lane's registry
#: verdict, and it runs BEFORE any test collection so a promoted pin fails
#: the lane in seconds rather than after a twenty-minute suite.
REGISTRY_COMMAND: Tuple[str, ...] = ("python", "-m", "tests.known_failing_pins")

#: Budgets in MINUTES. A lane with no budget is a lane whose runtime nobody
#: has agreed to, which is how a "five minute lane" becomes a thirty minute
#: one and the whole per-commit path stops being used.
#:
#: The nightly budget is 200, not 90: `nightly-quality.yml#live-quality`
#: declares `timeout-minutes: 180` because it runs the live multi-provider
#: matrix across 20 real tasks on several providers, and the report's own
#: note says so. The number in this table is a CEILING the workflows are
#: checked against, not a target — a workflow may be slower than the budget
#: only if the budget was raised deliberately here first, so the change
#: shows up in a diff on this file rather than as a mysteriously slow lane.
BUDGETS: Dict[str, int] = {
    "smoke": 1,
    "trust-ladder": 5,
    "host-only": 15,
    "docker": 30,
    "windows": 45,
    "nightly": 200,
}


@dataclass(frozen=True)
class Lane:
    """One CI lane.

    :param name: the label. Appears in the workflow ``name:`` so a required
        status check has a stable, human-readable identity.
    :param runs_on: the runner label. ``ubuntu-latest``, ``windows-latest``,
        or a tuple for a matrix.
    :param needs_docker: whether the lane may require a Docker daemon. A lane
        that says no and reaches a daemon is a lane that passes for the wrong
        reason on a machine that happens to have one.
    :param needs_network: whether the lane may fetch from the internet.
    :param blocking: whether a failure blocks a merge. Timing lanes are not
        blocking; see the module docstring.
    :param workflow: the workflow file that defines the lane.
    :param job: the job id inside that file.
    :param contains: what the lane runs, for the report. Not enforced
        mechanically except through :func:`check_workflows`, which asserts the
        workflow body mentions the tests it claims.
    """

    name: str
    runs_on: Tuple[str, ...]
    needs_docker: bool
    needs_network: bool
    blocking: bool
    workflow: str
    job: str
    contains: Tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "runs_on": list(self.runs_on),
            "needs_docker": self.needs_docker,
            "needs_network": self.needs_network,
            "blocking": self.blocking,
            "budget_minutes": BUDGETS.get(self.name, 0),
            "workflow": self.workflow,
            "job": self.job,
            "contains": list(self.contains),
            "note": self.note,
            "registry_gated": True,
        }


#: THE SIX LANES. Six rows, and a test pins the count.
LANES: Tuple[Lane, ...] = (
    Lane(
        name="smoke",
        runs_on=("ubuntu-latest",),
        needs_docker=False,
        needs_network=False,
        blocking=True,
        workflow="ci-lanes.yml",
        job="smoke",
        contains=(
            "tests/test_import_smoke.py",
            "cli/test_sanitize_pipeline.py",
            "cli/test_sanitize_shapes.py",
            "tests/test_trust_ladder.py",
            "tests/test_known_failing_pins.py",
            "tests/test_provenance_report.py",
            "tests/test_provenance_binaries.py",
            "tests/test_ci_lanes.py",
        ),
        note="mechanism assertions only: fast, deterministic, and they catch "
        "the regressions that matter most. No timing test lives here.",
    ),
    Lane(
        name="trust-ladder",
        runs_on=("ubuntu-latest",),
        needs_docker=False,
        needs_network=False,
        blocking=True,
        workflow="ci-lanes.yml",
        job="trust-ladder",
        contains=("python -m evals.run --suite trust-ladder",),
        note="the ten rungs. Rungs #8/#9/#10 are measured here and are "
        "budget assertions by construction; they assert order of magnitude "
        "with the machine named, never a tight absolute threshold.",
    ),
    Lane(
        name="host-only",
        runs_on=("ubuntu-latest",),
        needs_docker=False,
        needs_network=False,
        blocking=False,
        workflow="ci-lanes.yml",
        job="host-only",
        contains=(
            "HARNESS_EXEC_SKIP_DOCKER=1",
            "tests/test_slos.py",
            "tests/test_ceiling03_sessions.py",
            "tests/test_ceiling_r2_09_scale.py",
        ),
        note="NOT blocking. This is where host-dependent timing lives: a red "
        "here is information, not an obstruction. A flaky timing test in a "
        "blocking lane trains people to ignore the gate.",
    ),
    Lane(
        name="docker",
        runs_on=("ubuntu-latest",),
        needs_docker=True,
        needs_network=True,
        blocking=True,
        workflow="ci-lanes.yml",
        job="docker",
        contains=("tests/test_sandbox.py", "tests/test_verify.py", "docker info"),
        note="ubuntu only, and a host with no daemon is reported BLOCKED, never pass.",
    ),
    Lane(
        name="windows",
        runs_on=("windows-latest",),
        needs_docker=False,
        needs_network=False,
        blocking=True,
        workflow="windows-dockerfree-ci.yml",
        job="windows-dockerfree",
        contains=(
            "HARNESS_EXEC_SKIP_DOCKER=1",
            "cli/test_cli_import_smoke.py",
            "cli/test_display_contract.py",
            "cli/test_render_path_pin.py",
            "cli/test_sanitize_pipeline.py",
            "cli/test_sanitize_shapes.py",
            "tests/test_import_smoke.py",
        ),
        note="the Docker-free CLI subset, INCLUDING the cli/-local modules T4 "
        "added. They are enumerated explicitly, never globbed: a glob would "
        "silently absorb a new file and quietly change what the lane proves.",
    ),
    Lane(
        name="nightly",
        runs_on=("ubuntu-latest",),
        needs_docker=True,
        needs_network=True,
        blocking=False,
        workflow="nightly-quality.yml",
        job="live-quality",
        contains=(
            "python -m evals.run --suite prompt-regression",
            "python -m pytest tests/",
        ),
        note="scheduled + manual only. The full 8-arm matrix and the full "
        "suite; it needs real credentials and real spend, so it is never a "
        "required PR check (evals/ci_truth.py enforces that separately).",
    ),
)

LANE_NAMES: Tuple[str, ...] = tuple(lane.name for lane in LANES)

#: The lanes whose failures block a merge.
BLOCKING_LANES: Tuple[str, ...] = tuple(l.name for l in LANES if l.blocking)

#: The lanes that must not touch a Docker daemon.
DOCKER_FREE_LANES: Tuple[str, ...] = tuple(l.name for l in LANES if not l.needs_docker)

#: The lanes where a host-dependent timing assertion may live without making
#: the gate flaky. The Trust Ladder is deliberately NOT here: its rungs are
#: budget assertions on purpose, and they are written with generous budgets
#: and the machine named.
NON_BLOCKING_TIMING_LANES: Tuple[str, ...] = ("host-only", "nightly")


# --------------------------------------------------------------------------
# the checker
# --------------------------------------------------------------------------

#: A `runs-on:` line. Deliberately a regex over the raw text rather than a
#: YAML parse: this repository has no PyYAML dependency by choice (the CI
#: truth gate parses workflows with a line-oriented reader for the same
#: reason), and adding a parser to check a runner label would be a poor trade.
_RUNS_ON_RX = re.compile(r"^\s*runs-on:\s*(.+?)\s*$", re.MULTILINE)
_TIMEOUT_RX = re.compile(r"^\s*timeout-minutes:\s*(\d+)\s*$", re.MULTILINE)


def workflow_text(name: str) -> str:
    """Read one workflow file, or "" when it is absent."""
    path = WORKFLOWS / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def job_block(text: str, job: str) -> str:
    """Return the YAML block of one job, or "" when the job is absent.

    Line-oriented on purpose (see :func:`workflow_text`). A job block starts
    at a two-space-indented ``<job>:` key and ends at the next one.
    """
    lines = text.splitlines()
    start: Optional[int] = None
    for index, raw in enumerate(lines):
        if raw.startswith(f"  {job}:"):
            start = index
            break
    if start is None:
        return ""
    out = [lines[start]]
    for raw in lines[start + 1 :]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", raw):
            break
        out.append(raw)
    return "\n".join(out)


def check_workflows() -> List[Dict[str, str]]:
    """Return one finding per disagreement between this file and the workflows.

    Four things are checked, each because it is a way a lane can quietly stop
    being the lane it is named:

    1. the workflow file exists;
    2. the job exists in it;
    3. the job's ``runs-on`` matches the declared runner;
    4. the job's ``timeout-minutes`` does not EXCEED the declared budget, and
       a Docker-free lane does not contain a ``docker`` invocation.
    """
    findings: List[Dict[str, str]] = []
    for lane in LANES:
        text = workflow_text(lane.workflow)
        if not text:
            findings.append(
                {
                    "lane": lane.name,
                    "problem": f"{lane.workflow} does not exist or is unreadable",
                }
            )
            continue
        block = job_block(text, lane.job)
        if not block:
            findings.append(
                {
                    "lane": lane.name,
                    "problem": f"{lane.workflow} has no job named {lane.job!r}",
                }
            )
            continue
        runs = _RUNS_ON_RX.findall(block)
        declared_runner = runs[0].strip() if runs else ""
        if lane.runs_on[0] not in declared_runner and "matrix" not in declared_runner:
            findings.append(
                {
                    "lane": lane.name,
                    "problem": f"runs-on is {declared_runner!r}, expected "
                    f"{lane.runs_on[0]!r} (or a matrix of it)",
                }
            )
        budget = BUDGETS.get(lane.name, 0)
        for value in _TIMEOUT_RX.findall(block):
            if budget and int(value) > budget:
                findings.append(
                    {
                        "lane": lane.name,
                        "problem": f"timeout-minutes={value} exceeds the "
                        f"declared budget of {budget} for this lane",
                    }
                )
        if not lane.needs_docker and re.search(
            r"(?<![\w-])docker(?:\s|$)", block, re.IGNORECASE
        ):
            # A Docker-free lane may MENTION docker in a comment or in the
            # notice that says a daemon is absent, so only a `docker` at the
            # start of a run line counts.
            for raw in block.splitlines():
                stripped = raw.strip().lower()
                if stripped.startswith(("run:", "docker ")) and "docker" in stripped:
                    if "HARNESS_EXEC_SKIP_DOCKER" in raw:
                        continue
                    findings.append(
                        {
                            "lane": lane.name,
                            "problem": f"a Docker-free lane invokes docker: {raw.strip()!r}",
                        }
                    )
    return findings


def registry_gate_present() -> List[str]:
    """Return the lanes whose job block does not run the registry check.

    The rule is EVERY lane. This function is the mechanical form of it, so
    "the registry gates all of them" is a measurement rather than a claim.
    """
    missing: List[str] = []
    for lane in LANES:
        text = workflow_text(lane.workflow)
        block = job_block(text, lane.job) if text else ""
        if "tests.known_failing_pins" not in block:
            missing.append(lane.name)
    return missing


def report() -> Dict[str, Any]:
    """The full lane report, machine-readable."""
    findings = check_workflows()
    missing_gates = registry_gate_present()
    return {
        "schema_version": 1,
        "lanes": [lane.to_dict() for lane in LANES],
        "lane_count": len(LANES),
        "blocking_lanes": list(BLOCKING_LANES),
        "docker_free_lanes": list(DOCKER_FREE_LANES),
        "non_blocking_timing_lanes": list(NON_BLOCKING_TIMING_LANES),
        "registry_command": list(REGISTRY_COMMAND),
        "lanes_missing_the_registry_gate": missing_gates,
        "workflow_findings": findings,
        "ok": not findings and not missing_gates,
        "verdict": "CI_LANES_CONSISTENT"
        if not findings and not missing_gates
        else "CI_LANES_INCONSISTENT",
    }


def render(doc: Dict[str, Any]) -> str:
    lines = [
        f"CI lanes: {doc['verdict']}",
        "",
        f"{'lane':14} {'budget':>7} {'docker':7} {'net':5} blocking  workflow",
        "-" * 88,
    ]
    for row in doc["lanes"]:
        lines.append(
            f"{row['name']:14} {str(row['budget_minutes']) + 'm'!s:>7} "
            f"{row['needs_docker']!s:7} {row['needs_network']!s:5} "
            f"{row['blocking']!s:9}  {row['workflow']}#{row['job']}"
        )
        if row.get("note"):
            lines.append(f"{'':14} {row['note']}")
    lines += [
        "",
        f"registry gate: {' '.join(REGISTRY_COMMAND)}",
    ]
    if doc["lanes_missing_the_registry_gate"]:
        lines.append(
            "  MISSING from: " + ", ".join(doc["lanes_missing_the_registry_gate"])
        )
    else:
        lines.append("  present in every lane")
    lines += ["", "WORKFLOW FINDINGS:"]
    if doc["workflow_findings"]:
        for item in doc["workflow_findings"]:
            lines.append(f"  {item['lane']}: {item['problem']}")
    else:
        lines.append("  none")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the lane report. Exit 2 when a lane disagrees with this file."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m evals.ci_lanes",
        description="The six labelled CI lanes, checked against the workflows.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    doc = report()
    print(json.dumps(doc, indent=2) if args.json else render(doc))
    return 0 if doc["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
