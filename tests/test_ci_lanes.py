"""T5.W1.3 — the six labelled CI lanes, and the pin that keeps the workflows honest.

WHAT THIS FILE IS
-----------------
`evals/ci_lanes.py` is the authority for what the six lanes are. This file
asserts that the authority and the workflows in `.github/workflows/` agree, so
"we have six labelled lanes and the registry gates all of them" is a
**measurement** rather than a claim.

It also pins the rules the lanes exist to enforce:

1. **Exactly six lanes, with these names.** A seventh lane, or a rename, is a
   change to what blocks a merge, and it has to break a test.
2. **The known-failing registry gates EVERY lane.** Not "the lanes that run
   the pins" — every one. The specific failure this prevents: a pin is
   promoted, the promotion fails the `smoke` lane, and the other five lanes
   stay green, so the build looks broken for no reason and somebody adds the
   lane to an allowlist.
3. **The registry must catch a pin failing for the WRONG reason.** That is a
   regression, not a closed gap, and it is the outcome most dangerous to file
   as a known failure. Pinned here against synthetic observations, so it does
   not depend on the real tree happening to contain that case.
4. **Host-dependent timing never lives in a blocking lane.** The one declared
   exception is the Trust Ladder, whose rungs are budget assertions by
   construction, and that exception is named rather than implied.
5. **The `windows` lane includes the `cli/`-local modules.** A glob would
   silently absorb a new file and quietly change what the lane proves, so the
   enumeration is explicit and checked.
"""

from __future__ import annotations

import re as _re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from evals import ci_lanes

#: The YAML key a step's inline script lives under.
_RUN = "run:"

# --------------------------------------------------------------------------
# 1. the six lanes
# --------------------------------------------------------------------------


def test_there_are_exactly_six_lanes_with_these_names():
    """Six is the number. A seventh lane is a change to what blocks a merge."""
    assert ci_lanes.LANE_NAMES == (
        "smoke",
        "trust-ladder",
        "host-only",
        "docker",
        "windows",
        "nightly",
    )
    assert len(ci_lanes.LANES) == 6


def test_every_lane_has_a_budget_and_a_workflow_and_a_job():
    for lane in ci_lanes.LANES:
        assert ci_lanes.BUDGETS.get(lane.name), f"{lane.name} has no budget"
        assert lane.workflow.endswith(".yml"), lane
        assert lane.job
        assert lane.contains, f"{lane.name} claims no contents"
        assert lane.note, f"{lane.name} carries no note explaining what it is for"


def test_the_smoke_lane_is_fast_and_mechanism_only():
    smoke = next(l for l in ci_lanes.LANES if l.name == "smoke")
    assert ci_lanes.BUDGETS["smoke"] <= 1, "the brief's budget is <30s"
    assert smoke.blocking is True
    assert smoke.needs_docker is False
    assert smoke.needs_network is False
    # The import smoke and the sanitiser are named, because those are the two
    # that have each taken the CLI package offline in this repo's history.
    joined = " ".join(smoke.contains)
    assert "test_import_smoke" in joined
    assert "sanitiz" in joined


def test_the_docker_lane_is_ubuntu_only_and_the_windows_lane_is_not():
    docker = next(l for l in ci_lanes.LANES if l.name == "docker")
    windows = next(l for l in ci_lanes.LANES if l.name == "windows")
    assert docker.runs_on == ("ubuntu-latest",)
    assert docker.needs_docker is True
    assert windows.runs_on == ("windows-latest",)
    assert windows.needs_docker is False
    assert windows.needs_network is False


def test_the_nightly_lane_is_not_blocking_and_is_not_a_pr_check():
    nightly = next(l for l in ci_lanes.LANES if l.name == "nightly")
    assert nightly.blocking is False, (
        "the full matrix needs real credentials and real spend; making it a "
        "required PR check is how a gate gets disabled"
    )
    assert "nightly" not in ci_lanes.BLOCKING_LANES


def test_the_three_blocking_lanes_are_the_cheap_ones():
    assert ci_lanes.BLOCKING_LANES == ("smoke", "trust-ladder", "docker", "windows")
    assert "host-only" not in ci_lanes.BLOCKING_LANES
    assert "nightly" not in ci_lanes.BLOCKING_LANES


# --------------------------------------------------------------------------
# 2. host-dependent timing never blocks
# --------------------------------------------------------------------------


def test_host_dependent_timing_lives_only_in_non_blocking_lanes():
    """A flaky timing test in a blocking lane trains people to ignore the gate.

    `host-only` is where the SLO/latency pins live, and it is deliberately
    non-blocking. The Trust Ladder is the one declared exception and it is
    named, not implied — see the next test.
    """
    for lane in ci_lanes.LANES:
        if lane.name in ci_lanes.NON_BLOCKING_TIMING_LANES:
            continue
        if lane.name == "trust-ladder":
            continue
        joined = " ".join(lane.contains)
        assert "test_slos" not in joined, (
            f"{lane.name} is blocking and must not carry the SLO timing lane"
        )
        assert "test_ceiling03_sessions" not in joined, (
            f"{lane.name} is blocking and must not carry the 5,000-session "
            f"p95 timing pin, which cli/AGENTS.md records failing under load"
        )
    assert "host-only" in ci_lanes.NON_BLOCKING_TIMING_LANES


def test_the_trust_ladder_exception_is_declared_and_budget_based():
    """The exception is a budget with the machine named, not a tight threshold."""
    ladder = next(l for l in ci_lanes.LANES if l.name == "trust-ladder")
    assert ladder.blocking is True
    assert "order of magnitude" in ladder.note
    assert "machine named" in ladder.note
    assert "trust-ladder" not in ci_lanes.NON_BLOCKING_TIMING_LANES


def test_the_host_only_lane_says_why_it_is_not_blocking():
    host = next(l for l in ci_lanes.LANES if l.name == "host-only")
    assert host.blocking is False
    assert "NOT blocking" in host.note
    assert "information" in host.note


# --------------------------------------------------------------------------
# 3. the registry gates every lane
# --------------------------------------------------------------------------


def test_the_registry_gate_runs_in_every_lane():
    """THE RULE. Not "most lanes" — every lane.

    A registry some lanes read and others ignore is a registry whose promoted
    pins are promoted in one job and invisible in five, which is precisely the
    silent rot the registry exists to stop.
    """
    missing = ci_lanes.registry_gate_present()
    assert missing == [], (
        f"these lanes do not run {ci_lanes.REGISTRY_COMMAND}: {missing}. "
        f"Every lane must gate on the known-failing registry."
    )
    assert len(ci_lanes.LANES) == 6


def test_the_registry_gate_runs_before_any_test_in_every_lane_job():
    """Before any TEST invocation, so a promoted pin fails in seconds.

    Not "the first step": `actions/checkout` and `pip install` necessarily
    come first, and requiring the gate to precede them would be a rule
    nobody could satisfy honestly. The requirement that actually matters is
    that the gate precedes the first `pytest` / `evals.run` invocation, which
    is what makes a promotion fail in seconds instead of after a suite.
    """

    for lane in ci_lanes.LANES:
        text = ci_lanes.workflow_text(lane.workflow)
        assert text, f"{lane.workflow} is missing"
        block = ci_lanes.job_block(text, lane.job)
        assert block, f"{lane.workflow} has no job {lane.job!r}"
        gate_at = None
        first_test_at = None
        for offset, raw in enumerate(block.splitlines()):
            if gate_at is None and "tests.known_failing_pins" in raw:
                gate_at = offset
            if first_test_at is None and _re.search(
                r"pytest|evals\.run|evals\.live_quality", raw
            ):
                first_test_at = offset
        assert gate_at is not None, f"{lane.name}: no registry gate at all"
        if first_test_at is not None:
            assert gate_at < first_test_at, (
                f"{lane.name}: the registry gate is at line {gate_at} and the "
                f"first test invocation at {first_test_at}; a promoted pin "
                f"would run the whole suite before failing"
            )


def test_every_lane_names_its_registry_gate_consistently():
    """`GATE 1/6` in every lane, so the ordering is auditable by eye too.

    In the Actions UI a reader can see which step is the gate without reading
    the YAML, and a step that lost its label is visible in a diff.
    """
    for lane in ci_lanes.LANES:
        text = ci_lanes.workflow_text(lane.workflow)
        block = ci_lanes.job_block(text, lane.job)
        assert "GATE 1/6" in block, (
            f"{lane.name}: its registry step is not labelled `GATE 1/6`, so the "
            f"lane's gate is not identifiable in the Actions UI"
        )


def test_the_registry_command_is_the_module_not_a_bare_script():
    """`-m` so the import path is the repository's, on every OS."""
    assert ci_lanes.REGISTRY_COMMAND == (
        "python",
        "-m",
        "tests.known_failing_pins",
    )


# --------------------------------------------------------------------------
# 4. the registry catches a pin failing for the WRONG reason
# --------------------------------------------------------------------------


def test_the_registry_distinguishes_a_closed_gap_from_a_changed_reason():
    """Pinned on SYNTHETIC observations, so it does not need the real tree to
    contain the case.

    This is the mechanism that stops a recorded gap rotting quietly: a pin
    that is red for a *different* reason is a regression wearing the pin's
    name, and filing it as a known failure is the worst outcome available.
    """
    from tests.known_failing_pins import (
        AS_RECORDED,
        CHANGED_REASON,
        FAILED,
        PASSED,
        PROMOTE,
        KnownFailingPin,
        Observation,
        classify_known_failing,
    )

    pin = KnownFailingPin(
        node_id="tests/x.py::test_the_gap_is_open",
        terminal="T5",
        owner="T1 / P2",
        closes_when="the offending code is deleted",
        reason_substrings=("THE GAP IS STILL PRESENT", "harness/core.py"),
    )

    as_recorded = classify_known_failing(
        pin,
        Observation(
            outcome=FAILED,
            text="AssertionError: THE GAP IS STILL PRESENT at harness/core.py:797",
        ),
    )
    assert as_recorded.verdict == AS_RECORDED
    assert as_recorded.is_build_failure is False

    promoted = classify_known_failing(pin, Observation(outcome=PASSED, text="1 passed"))
    assert promoted.verdict == PROMOTE
    assert promoted.is_build_failure is True
    assert "PROMOTE THIS" in promoted.detail

    changed = classify_known_failing(
        pin,
        Observation(
            outcome=FAILED, text="AssertionError: TypeError: unrelated explosion"
        ),
    )
    assert changed.verdict == CHANGED_REASON
    assert changed.is_build_failure is True
    assert "regression" in changed.detail.lower()


def test_the_registry_runs_clean_right_now():
    """The real registry, really run. Not mocked."""
    proc = subprocess.run(
        [sys.executable, "-m", "tests.known_failing_pins"],
        cwd=str(ci_lanes.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "0 build failure(s)" in proc.stdout


# --------------------------------------------------------------------------
# 5. the windows lane carries the cli/-local modules
# --------------------------------------------------------------------------


def test_the_windows_lane_runs_the_cli_local_modules():
    """T4's `cli/`-local modules, enumerated.

    Enumerated rather than globbed: a glob would silently absorb a new file
    and quietly change what the lane proves, which is the failure mode the
    lane's own documented guard exists for.
    """
    windows = next(l for l in ci_lanes.LANES if l.name == "windows")
    joined = " ".join(windows.contains)
    for module in (
        "cli/test_cli_import_smoke.py",
        "cli/test_display_contract.py",
        "cli/test_render_path_pin.py",
        "cli/test_sanitize_pipeline.py",
        "cli/test_sanitize_shapes.py",
    ):
        assert module in joined, f"the windows lane does not run {module}"
        assert (ci_lanes.REPO_ROOT / module).is_file(), f"{module} does not exist"


def test_every_file_every_lane_claims_to_run_actually_exists():
    """A lane that names a test file which does not exist covers less than it
    claims, and reports success doing it."""
    for lane in ci_lanes.LANES:
        for item in lane.contains:
            if not item.endswith(".py"):
                continue
            assert (ci_lanes.REPO_ROOT / item).is_file(), (
                f"the {lane.name} lane names {item}, which does not exist"
            )


# --------------------------------------------------------------------------
# 6. the workflows agree with the authority
# --------------------------------------------------------------------------


def test_the_workflows_agree_with_the_lane_authority():
    """No runner mismatch, no over-budget timeout, no Docker in a Docker-free
    lane, and every lane's registry gate present."""
    doc = ci_lanes.report()
    assert doc["ok"] is True, (
        "the workflows and evals/ci_lanes.py disagree:\n"
        + "\n".join(f"  {f['lane']}: {f['problem']}" for f in doc["workflow_findings"])
        + f"\n  lanes missing the registry gate: {doc['lanes_missing_the_registry_gate']}"
    )
    assert doc["verdict"] == "CI_LANES_CONSISTENT"


def test_every_shell_python_step_in_every_workflow_actually_parses():
    """A syntax error in a `shell: python` step shows up only as a red Windows
    job after merge, so every such step is extracted and compiled here.

    Two of the six lanes run Python as their shell, and the extraction is
    deliberately naive -- a `shell: python` marker, then the `run: |` block
    that follows it in the SAME step, dedented by the block's own indent. The
    first version of this test grabbed the wrong indent and failed with
    "unexpected indent" on a step that is perfectly valid, which is how a
    test gets deleted instead of fixed; so the extractor is now checked
    against a planted case with a known body before it is trusted.
    """
    import ast

    def extract(text: str):
        """Yield the script of every `shell: python` step in ``text``."""
        lines = text.splitlines()
        out = []
        index = 0
        while index < len(lines):
            if "shell: python" not in lines[index]:
                index += 1
                continue
            step_indent = len(lines[index]) - len(lines[index].lstrip())
            # Find this step's own `run: |` line. A sibling KEY (`run:` sits
            # at the same indent as `shell:`) is not the next step -- a new
            # step is a line that starts with a YAML sequence dash at this
            # indent. Getting that wrong is what made the first extractor
            # return nothing at all.
            cursor = index + 1
            run_at = None
            while cursor < len(lines):
                candidate = lines[cursor]
                stripped = candidate.strip()
                indent = len(candidate) - len(candidate.lstrip())
                if stripped.startswith("- ") and indent <= step_indent:
                    break
                if (
                    indent <= step_indent
                    and stripped
                    and not stripped.startswith(
                        ("run:", "env:", "if:", "with:", "shell:")
                    )
                ):
                    break
                if _RUN.strip() and stripped.startswith(_RUN):
                    run_at = cursor
                    break
                cursor += 1
            if run_at is None:
                index += 1
                continue
            # The body starts on the NEXT line. Slicing `run_at` itself by
            # block_indent + 1 produced "un: |" -- it chopped the key rather
            # than skipping it -- which is the second half of why the first
            # extractor reported a phantom syntax error.
            body: list = []
            cursor = run_at + 1
            body_indent: Optional[int] = None
            while cursor < len(lines):
                candidate = lines[cursor]
                if not candidate.strip():
                    if body_indent is not None:
                        body.append("")
                    cursor += 1
                    continue
                indent = len(candidate) - len(candidate.lstrip())
                if body_indent is None:
                    body_indent = indent
                if indent < body_indent:
                    break
                body.append(candidate)
                cursor += 1
            out.append(textwrap.dedent("\n".join(body)))
            index = cursor
        return out

    # The extractor is pinned on a case with a known body, so a broken
    # extractor fails as a BROKEN EXTRACTOR rather than as a phantom syntax
    # error in a workflow that is fine.
    sample = (
        "      - name: thing\n"
        "        shell: python\n"
        "        run: |\n"
        "          import sys\n"
        "          if True:\n"
        "              sys.exit(0)\n"
    )
    got = extract(sample)
    assert len(got) == 1, got
    ast.parse(got[0])  # the control: a valid step must extract to valid Python

    checked = 0
    for workflow in sorted(ci_lanes.WORKFLOWS.glob("*.yml")):
        for script in extract(workflow.read_text(encoding="utf-8", errors="replace")):
            try:
                ast.parse(script)
                checked += 1
            except SyntaxError as exc:
                pytest.fail(
                    f"{workflow.name}: a `shell: python` step does not parse: "
                    f"{exc}\n--- script ---\n{script}\n--------------"
                )
    assert checked, "no `shell: python` steps were found to check"


def test_the_lane_cli_runs_and_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "evals.ci_lanes"],
        cwd=str(ci_lanes.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CI_LANES_CONSISTENT" in proc.stdout
    assert "present in every lane" in proc.stdout


def test_the_lane_report_publishes_every_field_a_reader_needs():
    doc = ci_lanes.report()
    assert doc["lane_count"] == 6
    for row in doc["lanes"]:
        for key in (
            "name",
            "runs_on",
            "needs_docker",
            "needs_network",
            "blocking",
            "budget_minutes",
            "workflow",
            "job",
            "registry_gated",
        ):
            assert key in row, f"{row.get('name')} is missing {key}"
        assert row["registry_gated"] is True
    assert Path(ci_lanes.WORKFLOWS).is_dir()
