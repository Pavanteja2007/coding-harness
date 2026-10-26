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
    "export-redaction": 1,
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


#: THE SEVEN LANES. Seven rows, and a test pins the count.
#:
#: The seventh is `export-redaction`, added by P5/W1-T5 because a leak in an
#: exported session is the one boundary whose artifact is designed to leave
#: the machine. Six lanes protected the running system; this one protects the
#: copy somebody sends to a colleague.
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
            "cli/test_display_contract.py",
            "cli/test_inert_surface_pin.py",
            "cli/test_render_path_pin.py",
            "tests/test_trust_ladder.py",
            "tests/test_known_failing_pins.py",
            "tests/test_provenance_report.py",
            "tests/test_provenance_binaries.py",
            "tests/test_ci_lanes.py",
            "tests/test_skip_authority.py",
            "tests/test_config_defaults_match_source.py",
            # P2/W1-T5: the untrusted-source boundary and the policy language.
            # Both are cheap, deterministic and Docker-free, which is the whole
            # reason they belong HERE and not in `host-only`: a boundary that is
            # only checked in a 15-minute non-blocking lane is a boundary that
            # is not checked on most merges. `evals/policy_examples.py` is the
            # executable form of the same evidence (every shipped rule's
            # examples, the arity table, the seven sources, the breakout corpus
            # and the visibility contract in both directions) and it exits 2 on
            # a regression, so it belongs in a BLOCKING lane.
            "tests/test_p2w1_untrusted_sources.py",
            "tests/test_p2w1_policy_language.py",
            "evals/policy_examples.py",
            # P3b/W1-T5: THE PROMPT-CHANGE GATE, and it belongs here for the
            # lane's own reasons rather than for convenience. AHEAD measured
            # this repository's own agent system prompt at -2.3pp for PROSE
            # ALONE, and this phase's rule is "prompts land as eval arms, never
            # as edits". A convention does not survive a deadline, so it is a
            # gate - and a gate that only ran in the 15-minute NON-BLOCKING
            # `host-only` lane would be a gate most merges never meet.
            #
            # It is fast: one AST parse of a single file, no Docker, no network,
            # no provider, no test collection, which is the only reason it can
            # live in a sub-30s lane at all. `python -m evals.prompt_pin` exits
            # 2 on any unarmed prompt edit, and its exemption list is refused if
            # it is a wildcard.
            "evals/prompt_pin.py",
            # P3b/W2-T5: the three gates added when the prompt pin was ATTACKED
            # and the ablation was actually RUN. Each exists because a
            # measurement was wrong or a pin was decorative, and each is the
            # mechanical form of a finding prose would not have stopped:
            #
            #   `--go-no-go`      the discoverability metric this phase calls the
            #                      go/no-go was STRUCTURALLY 0.0 and could not be
            #                      moved by any setting of any parameter, because
            #                      `measure_arm` resolved the needed tool
            #                      unconditionally. A metric that cannot fail is
            #                      not a gate.
            #   `--scorer-audit`  the two REGISTERED scorers disagreed on most of
            #                      the catalog, so the token number is
            #                      scorer-dependent and has to be published with
            #                      the scorer named.
            #   `prompt_pin --json` the exemption COUNT and the coverage-gap
            #                      count, which are not findings today and are
            #                      findings the moment they move.
            #
            # All three are host-only and sub-5s, which is the only reason they
            # can live in a sub-30s blocking lane at all.
            "evals/tools_ablation.py",
            # The pins for the deferred-tool seam, the skills registry and the
            # tools ablation. Host-only and fast; the SECURITY section is the
            # part that must not wait for a nightly - it forges a withheld tool
            # becoming reachable and asserts the report catches it.
            "tests/test_p3b_tools_and_prompts.py",
            "tests/test_p3b_w2_truth.py",
            # P5/W1-T5, and the three the phase named explicitly. Each is
            # MEASURED, not assumed:
            #
            #   cli/test_rail_honesty.py                1.44 s, 15 tests
            #   tests/test_shared_keymap.py             0.90 s, 43 tests
            #   tests/test_agent_server_loopback.py     (see its own note)
            #
            # The rail one is the honest-absent pin: every rail field must
            # render `unavailable` + a reason rather than a `0`, and a gate
            # that only checks the flattering cases is the exact hole W1.4
            # was written to close. The keymap one is the fail-closed pin:
            # a corrupt user keymap must yield the DEFAULTS plus an error, and
            # `load_keymap_state` must never raise -- one bad character once
            # took the whole CLI offline. The loopback one is an agent with a
            # network identity: `AgentServer` binds 127.0.0.1 and REFUSES
            # `0.0.0.0` without the flag, and `cli/serve.py`'s non-loopback
            # refusal needs a token as well as the flag.
            #
            # All three are pure-python, host-only, and free of Docker,
            # network and provider. `cli/test_wave2_render_sanitiser.py` (the
            # diff-pane / file-tree / git-UI sanitiser pin) is deliberately
            # NOT here despite being a security boundary: it measures 21.9 s,
            # which this lane cannot afford. It is in `host-only` with its
            # measurement, rather than added here on a promise.
            "cli/test_rail_honesty.py",
            "tests/test_shared_keymap.py",
            "tests/test_agent_server_loopback.py",
        ),
        note="mechanism assertions only: fast, deterministic, and they catch "
        "the regressions that matter most. No timing test lives here. P1/W2 "
        "promoted four more: the inert-surface pin, the render-path pin, the "
        "skip-authority census, and the config-vs-source pin (which caught a "
        "stale __pycache__ serving the pre-Phase-1 turn ceiling for a whole "
        "session). P4/W1-T5 asked for three more promotions into this lane and "
        "got ZERO, deliberately: this lane is already over its budget (see "
        "`host-only`'s note for the measurement), and adding ~27 s to a lane "
        "that cannot afford it is a budget decision for whoever owns the "
        "number, not a side effect of a promotion. The three are in "
        "`host-only` instead and are re-promotable the moment the budget is "
        "re-measured on a quiet host.",
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
            # P1/W2: the four terminals' module-local pins, in one selection.
            # They live inside their own package, so `pytest tests/` never
            # collects them -- a pin nobody runs is a comment with a filename.
            "harness/test_p1w1_performance.py",
            "harness/test_p1w2_budgets.py",
            "harness/test_p1w2_timing.py",
            "execution/test_w1_latency_honesty_pins.py",
            "execution/test_w2_latency_and_staleness_pins.py",
            "execution/test_selection.py",
            "runtime/test_latency_pins.py",
            "runtime/test_w2_pins.py",
            "runtime/test_structural_predictor_guard.py",
            # P4/W1-T5: the Phase-4 ledger store's BEHAVIOURAL half, its
            # structural half, the five pre-registered gap pins, and the
            # multi-session evaluation's pins.
            #
            # **All four are HERE and not in `smoke`, and that is a measured
            # decision rather than a preference.** Phase 4's brief asked for three
            # of these to be promoted to the blocking smoke lane. Two of them
            # meet smoke's own stated criteria -- `test_ledger_store_structural_
            # pins.py` is 1.2 s and pure AST, `test_p4_long_horizon_gaps.py` is
            # 1.6 s and host-only -- and the third (`test_agent_loop_matrix.py`,
            # the 14-scenario pure-vs-adapter agreement, 24.6 s) is the net T3's
            # middleware refactor needs. None of them is promoted because smoke
            # is ALREADY over its budget: the note below records 96.3 s measured
            # against a `<= 1` minute budget, and re-measuring on this tree did
            # not finish inside FIFTEEN minutes with four parallel terminals on
            # it. That figure is a lower bound contaminated by host load, so it
            # is not quoted as a clean measurement -- but it is not evidence that
            # there is room either, and widening a lane that is already over is
            # a budget decision for whoever owns the number, not a side effect of
            # a promotion. See the handoff: the promotion is unblocked the moment
            # the budget is re-measured on a quiet host.
            #
            # `tests/test_agent_loop_matrix.py` also arrives with one repair: its
            # `test_the_three_mint_conditions_agree_across_the_tree` imported
            # `harness.agent_kernel.legacy`, deleted by the P2/W1-T1 subtractive
            # pass, which made the whole file error at collection. The import is
            # gone and the legacy mint condition is still pinned AS A SHAPE, so no
            # assertion was weakened.
            "tests/test_feature_ledger_store.py",
            "tests/test_ledger_store_structural_pins.py",
            "tests/test_p4_long_horizon_gaps.py",
            "tests/test_multi_session_eval.py",
            # P4/W2.2: the drift audit's pins. Found in this lane by T5.W2.4's own
            # adjudication -- `check_multi_session_tests_are_in_the_nightly_lane`
            # reported this file as selected by NO lane, which makes the drift
            # evidence a comment. It is host-only and deterministic (a temp
            # repository per probe), and one of its tests shells out to
            # `scripts/p4_drift_audit.py`, so it is the slowest of the group.
            "tests/test_p4_drift_audit.py",
            # P4/W2.3: T3's middleware ordering proof. It landed mid-wave and it
            # CLOSED the "declared ordering is asserted by nothing" gap, so its
            # pin was deleted -- which would have left the proof in NO lane and
            # therefore in no CI at all. 61 host-only tests, 50.8s measured.
            "runtime/test_p4w2_middleware_proof.py",
            "tests/test_agent_loop_matrix.py",
            # P3a/W1-T5: the whole P3a memory suite, in ONE lane rather than
            # split. It was MEASURED here and belongs in `smoke` by the lane's
            # own criteria -- deterministic, Docker-free, network-free, and it
            # is a SECURITY boundary (revocation completeness, the human-gated
            # inbox, engine honesty) -- at 2.51 + 1.74 + 2.76 + 4.33 + 17.08 +
            # 5.60 s.
            #
            # It is NOT in `smoke` because `smoke` is ALREADY over its pinned
            # budget without it: measured 96.3 s against
            # `tests/test_ci_lanes.py::test_the_smoke_lane_is_fast_and_mechanism_
            # only`'s `<= 1` minute, and 118.5 s with these six. That overrun
            # is PRE-EXISTING and widening it is not this change's call --
            # `evals/ci_lanes.BUDGETS` was raised and then reverted for exactly
            # that reason. The correct follow-up is to SPLIT `smoke`, which now
            # mixes import smoke, provenance, policy and (soon) memory.
            #
            # Consequence, stated rather than hidden: until that split, a red in
            # the memory trust boundary is INFORMATION here, not an obstruction.
            "tests/test_p3a_search_index.py",
            "tests/test_p3a_snapshot.py",
            "tests/test_p3a_inbox.py",
            "tests/test_p3a_governance.py",
            "tests/test_p3a_pipeline.py",
            "tests/test_p3a_memory_ablation.py",
            # P3a-W2. Host-only and fast: the inbox gate is AST + tempfile +
            # sqlite3, and the security re-pins are pure gate/store calls. Both
            # are here rather than in `smoke` because `test_p3a_memory_quality`
            # drives the REAL loop, which is Docker work - so that one is NOT
            # in this lane, and saying so beats a glob quietly absorbing it.
            "tests/test_p3a_inbox_gate.py",
            "tests/test_p3a_security_repins.py",
            # P5/W1-T5. Three new selections, and each is here for a stated
            # reason rather than by default:
            #
            #   cli/test_wave2_render_sanitiser.py   21.9 s measured. The
            #       diff-pane / file-tree / git-UI sanitiser pin, and a
            #       security boundary. Too slow for `smoke` (which is already
            #       over budget) and therefore a red HERE is information rather
            #       than an obstruction -- which is the right place for it.
            #   tests/test_agent_server_supervision.py
            #       Drives real threads, a real socket and wall-clock waits
            #       (the restart loop is polled), so it is host-dependent by
            #       construction rather than by assertion.
            #   tests/test_acp_client_services.py
            #       Deterministic in-memory round trips, but it belongs with
            #       the ACP surface (`tests/test_acp.py` runs in the `windows`
            #       lane for the same reason) rather than widening a lane
            #       that has already been measured over budget.
            "cli/test_wave2_render_sanitiser.py",
            "tests/test_agent_server_supervision.py",
            "tests/test_acp_client_services.py",
        ),
        note="NOT blocking. This is where host-dependent timing lives: a red "
        "here is information, not an obstruction. A flaky timing test in a "
        "blocking lane trains people to ignore the gate. The canonical case is "
        "tests/test_ceiling03_sessions.py::"
        "test_five_thousand_indexed_sessions_list_under_100ms_p95 -- it failed "
        "in this phase's full-suite run, passed standalone, and its own "
        "diagnostic reported the host 1.89x slowed. P1/W2 added the "
        "module-local pins here rather than in `smoke` because every one of "
        "them carries a wall-clock or p95 assertion.",
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
            "cli/test_inert_surface_pin.py",
            "cli/test_toggles_flip.py",
            "cli/test_wave2_render_sanitiser.py",
            "tests/test_import_smoke.py",
            "tests/test_config_defaults_match_source.py",
            "tests/test_skip_authority.py",
        ),
        note="the Docker-free CLI subset, INCLUDING the cli/-local modules T4 "
        "added. They are enumerated explicitly, never globbed: a glob would "
        "silently absorb a new file and quietly change what the lane proves, "
        "and the step REFUSES to run when a named file is absent. P1/W2 grew "
        "the list from five to ten and added the two shared pins worth running "
        "on a second platform -- the config-vs-source pin (newline translation) "
        "and the skip-authority census (one walk, one skip set).",
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
            # P4/W1-T5: the MULTI-SESSION suite. It is `nightly` and NOT
            # blocking, for two reasons that are both about the measurement
            # rather than about convenience.
            #
            # 1. It needs REAL sessions -- several sequential runs over one
            #    working tree, each with its own fresh context. That is the whole
            #    subject; a shortened variant would stop measuring the thing.
            # 2. Its repetitions are the cost knob (DEFAULT_REPETITIONS = 3), and
            #    one run is noise. In nightly those repetitions get a real budget;
            #    in a blocking lane they would either be cut to one -- which
            #    `phases/DOCTRINE.md` treats as reporting noise as a result -- or
            #    they would dominate the lane.
            #
            # It exits 2 when NO arm produced a measurement, which is what makes
            # it a gate rather than a log line; a suite that runs cleanly and
            # measures nothing is `not_run`.
            "python -m evals.multi_session --repetitions 3",
        ),
        note="scheduled + manual only. The full 8-arm matrix, the full "
        "suite and the multi-session evaluation; it needs real credentials and "
        "real spend, so it is never a required PR check (evals/ci_truth.py "
        "enforces that separately).",
    ),
Lane(
        name="export-redaction",
        runs_on=("ubuntu-latest",),
        needs_docker=False,
        needs_network=False,
        blocking=True,
        workflow="ci-lanes.yml",
        job="export-redaction",
        contains=(
            "tests/test_session_share.py",
        ),
        note="P5/W1-T5's seventh lane, and the one the phase asked for by "
        "name: a leak in an exported session is the worst possible leak, "
        "because the artifact is DESIGNED TO LEAVE THE MACHINE. Every other "
        "boundary in this index protects the running system; this one "
        "protects the copy someone sends to a colleague. It is BLOCKING and "
        "Docker-free on purpose: the gate is the canary corpus (normal AND "
        "obfuscated) asserting that no canary token survives redaction, and a "
        "secret that survives a share cannot be un-shared. 35 passed / 1 "
        "skipped (the symlink case needs a privilege Windows does not grant "
        "by default) in 4.3 s, which is why this is a lane rather than a step "
        "inside another one -- a boundary checked only as part of a larger "
        "lane is a boundary nobody can name when it goes red.",
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
