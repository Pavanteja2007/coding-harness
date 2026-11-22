"""Prompts land as eval arms, never as edits - enforced mechanically.

THE RULE AND WHY IT IS CODE
---------------------------
The corpus's one measurement of this repository's own agent system prompt found
**-2.3pp for prose alone**. That is the most expensive single number in the
phase, and it is a number about exactly the change a well-meaning person makes
when a task goes wrong: add a sentence to the prompt.

So the rule is: **a change to agent-facing prompt text is only allowed if an
eval arm exercises the change.** A convention does not survive a deadline, and
this is a deadline-shaped phase. :func:`check` runs in the ``smoke`` CI lane on
every commit.

WHAT IT DETECTS
---------------
A change to an agent-facing prompt constant in ``harness/prompts.py`` that no
registered eval arm covers. It reads the file by AST and digests the string
value of each constant, so:

* editing a **docstring** is not a prompt change (the constant's value is
  unchanged) - and a comment edit is not one either;
* editing a **prompt** IS a change, even if the diff is one character;
* adding a **new** agent-facing constant is a change, and it is reported as
  unclassified until somebody says which arm covers it.

AST rather than a file digest because a file digest reports "the file changed",
which fires on a reformat and on a comment, and a gate that cries wolf is a gate
people disable. This repository already learned that lesson the hard way:
``tests/test_config_trace_state.py::test_trace_redacts_nested_credentials`` and
the P0-W2 round's BOM incident are both cases of a pin that was too blunt to
be trusted.

HOW A LEGITIMATE EDIT IS DISTINGUISHED FROM AN ILLEGITIMATE ONE
---------------------------------------------------------------
The mechanism the brief asks for is that **an arm declares the prompt variant it
covers, and an unreferenced variant is flagged.** That is :data:`ARM_COVERAGE`.
Each agent-facing constant names the arms that exercise it; an edit is legal
when those arms are all real and registered.

So:

* **legitimate** - the constant's digest changes AND every arm in its coverage
  set exists in :data:`REGISTERED_ARMS`. The report calls it ``armed_edit``.
* **illegitimate** - the digest changes and the coverage set is empty, or names
  an arm that does not exist. Reported as ``unarmed_prompt_edit``, which fails
  the lane.
* **dangling** - the coverage set names an arm that has been renamed or deleted.
  This fires even with NO digest change, because a coverage set that points at
  nothing is a coverage set that has stopped covering.

WHY IT CANNOT BE TRIVIALLY BYPASSED
------------------------------------
Four specific ways this gate is designed to survive being worked around, each
of which has been how a pin in this repository was defeated before:

1. **No blanket suppression.** :data:`EXEMPTIONS` is keyed by CONSTANT NAME and
   :func:`check` REFUSES a wildcard entry at load time. There is no
   ``"ignore all prompts"`` value, no environment variable, and no
   ``skip=`` parameter. A blanket exemption is a deletion of the gate wearing a
   config file.
2. **An exemption is evidence, not a switch.** Every entry needs a non-empty
   ``reason`` and an ``owner``, and a changed digest under an exemption is a
   FINDING rather than a pass - the exemption's recorded digest has to be moved
   deliberately, so the re-review shows up in a diff on this file.
3. **The baseline cannot be quietly emptied.** :func:`check` fails if
   :data:`PINNED_DIGESTS` holds fewer entries than
   :data:`MIN_PINNED_CONSTANTS`. Deleting the pins to make a gate green is then
   a larger diff than the edit the gate was complaining about.
4. **A prompt constant that nobody classified is itself a finding**
   (``unclassified_prompt``). The gate therefore also fails when somebody ADDS
   a prompt without saying which arm covers it, which is the other direction of
   the same rule.

THE SEAM FOR WHOEVER LANDS A PROMPT CHANGE
------------------------------------------
Three steps, in this order, and the gate is happy on the third:

1. Edit the constant in ``harness/prompts.py``.
2. Register an arm that exercises the change - in :data:`evals.run.ARMS` for a
   prompt-adjacent config key, or in :data:`REGISTERED_ARMS` here for an arm
   this driver owns - and name the constant in its :data:`ARM_COVERAGE` entry.
3. Re-run ``python -m evals.prompt_pin``. It reports ``armed_edit`` and exits 0.

Skipping step 2 exits 2. That is the whole discipline, and it is 3 commands.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - path boot
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "AGENT_FACING",
    "ARM_COVERAGE",
    "EXEMPTIONS",
    "FINDING_KINDS",
    "MIN_PINNED_CONSTANTS",
    "PINNED_DIGESTS",
    "PROMPTS_PATH",
    "REGISTERED_ARMS",
    "PromptConstant",
    "check",
    "digest_of",
    "enumerated_constants",
    "main",
    "render",
    "update_pins",
]


#: The file the constants live in. Named so a test can prove the pin is reading
#: the file it claims to be reading rather than a copy.
PROMPTS_PATH = REPO_ROOT / "harness" / "prompts.py"

#: The prompt constants that are READ BY THE AGENT. This is the whole
#: judgement in the module and it is a judgement, so it is data and a test
#: pins it against the rendered call sites.
#:
#: The EXCLUSIONS are as load-bearing as the inclusions and are the reason this
#: gate does not fire on maintenance prompts:
#:
#: * ``CONTEXT_COMPACTION_*`` - a summariser request. No agent ever reads these
#:   as instructions; ``harness/test_p3a_context.py`` already pins that the
#:   merge prose never reaches an agent-facing constant. Editing one cannot cost
#:   the agent 2.3pp because the agent never sees it.
#: * ``SELF_CRITIQUE_SYSTEM`` / ``AGENT_TESTS_SYSTEM`` / ``BUILD_TESTS_SYSTEM`` /
#:   ``PROJECT_*_SYSTEM`` - these ARE agent-adjacent (a model reads them) but
#:   they are SINGLE-SHOT maintenance calls on a bounded task, not the
#:   multi-turn loop prompt. They are excluded from the pin's reach and named as
#:   exempt below rather than silently dropped, so the gap is visible in a
#:   report instead of being invisible by omission.
#:
#: ``AGENT_TESTS_*`` and ``PROJECT_*`` are genuinely a coverage gap in this
#: repo's eval matrix, not a decision that they do not matter. They are listed
#: in :data:`EXEMPTIONS` with that reason so a reviewer sees the hole.
AGENT_FACING: Tuple[str, ...] = (
    "PLANNER_SYSTEM",
    "PLANNER_USER",
    "PLANNER_COORDINATION_RULES",
    "STEP_SYSTEM_TEMPLATE",
    "CONSTRAINT_REINJECTION_TEMPLATE",
    "STEERING_MSG_TEMPLATE",
    "CONTEXT_REINJECTION_TEMPLATE",
    "DAILY_SYSTEM",
    "DAILY_USER_TEMPLATE",
)


#: Digests of the agent-facing constants as of the round that created this pin.
#:
#: Recorded by :func:`update_pins`, which is a DELIBERATE act - moving a digest
#: here is the diff that says "I changed this prompt on purpose, and here is
#: the arm that exercises it". Never edited to make a gate green; the empty
#: baseline is itself a finding (:data:`MIN_PINNED_CONSTANTS`).
PINNED_DIGESTS: Dict[str, str] = {
    "CONSTRAINT_REINJECTION_TEMPLATE": "e9f90acd0d79c04f8e6ff8ee98445fc4d4f034d1460dac4ab63fe9fb36b67e3b",
    "CONTEXT_REINJECTION_TEMPLATE": "9b45a47713c7446c4b3d6cddfec1f3e9f9243e6017866217f7c74e485f1935d3",
    "DAILY_SYSTEM": "60a7ba8fa0775b79e9955e47fa3dd1afa4291b0eedf1a268ca6cfeea7f9272c2",
    "DAILY_USER_TEMPLATE": "29801329cb7fecf2d25bab585f70030539dda2fe86427d28491dfc018a50b1d6",
    "PLANNER_COORDINATION_RULES": "b9571b3077b68105ac80e8bcc2ccb7250c3235f0f467224337d35cfa9b2fcbf9",
    "PLANNER_SYSTEM": "2901a687b09fecef983929369e03d442f535e69cfb4a3c19635a30289d2861d1",
    "PLANNER_USER": "4a571773c3fca0ea85b6152185570b7e9454308897f0aa5331dd6100116ec701",
    "STEERING_MSG_TEMPLATE": "4a6612a30463461c30b39a8e63ad120ba370ceab7665bf5063175bc3b5721fab",
    "STEP_SYSTEM_TEMPLATE": "48a657c2bfb760bb20478b3837ce1f520b3a474bb88135936f1fd0d51874ef17",
}


def digest_of(value: str) -> str:
    """The sha256 of a constant's string VALUE, normalised for line endings.

    Normalised because a checkout on Windows and one on Linux must produce the
    same pin, and an ``\\r\\n``/``\\n`` difference is not a prompt change. The
    first version of this helper hashed the raw source slice, which made every
    Windows checkout of a CRLF file report all of its prompts as edited - the
    exact cry-wolf failure this module exists to avoid.
    """
    normalised = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptConstant:
    """One agent-facing prompt constant as it exists on disk right now."""

    name: str
    value: str
    digest: str
    lineno: int
    kind: str = "str"

    @property
    def chars(self) -> int:
        return len(self.value)


#: Finding kinds. A closed set, because a finding a reader has to interpret is a
#: finding somebody will ignore.
FINDING_KINDS: Tuple[str, ...] = (
    "unarmed_prompt_edit",  # the digest moved and no arm covers it
    "dangling_arm_coverage",  # a coverage entry names an arm that is not real
    "unclassified_prompt",  # a new agent-facing constant nobody classified
    "stale_exemption",  # an exemption's content changed under it
    "invalid_exemption",  # an exemption is a wildcard or has no reason/owner
    "baseline_emptied",  # somebody deleted the pins
    "prompt_unreadable",  # the file could not be read or parsed
)


def enumerated_constants(
    path: Optional[Path] = None,
    *,
    agent_facing: Sequence[str] = AGENT_FACING,
) -> Tuple[List[PromptConstant], List[str]]:
    """Read the agent-facing constants by AST.

    Returns ``(constants, problems)``. ``problems`` is non-empty when the file
    could not be read or parsed, and the caller turns it into a
    ``prompt_unreadable`` finding rather than reporting "no prompts changed" -
    a scanner that reports clean on a file it could not read is the class of
    defect this repository keeps paying for.

    A constant in :data:`AGENT_FACING` that is ABSENT from the file is also a
    problem, not a silent skip: the list and the file have drifted.
    """
    target = Path(path or PROMPTS_PATH)
    try:
        source = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"cannot read {target}: {exc}"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [f"cannot parse {target}: {exc}"]
    wanted = set(agent_facing)
    found: Dict[str, PromptConstant] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target_node in targets:
            if not isinstance(target_node, ast.Name):
                continue
            if target_node.id not in wanted:
                continue
            found[target_node.id] = PromptConstant(
                name=target_node.id,
                value=value.value,
                digest=digest_of(value.value),
                lineno=int(getattr(node, "lineno", 0)),
            )
    problems = [
        f"agent-facing constant {name!r} is not present in {target.name}"
        for name in sorted(wanted)
        if name not in found
    ]
    return [found[name] for name in sorted(found)], problems


def update_pins(
    path: Optional[Path] = None,
    *,
    agent_facing: Sequence[str] = AGENT_FACING,
) -> Dict[str, str]:
    """Return the CURRENT digests, formatted for pasting into this file.

    Does NOT write. Writing the pin file from inside the gate would let the gate
    be satisfied by running the thing it is supposed to police; the digest move
    has to be a human edit to a tracked file so it appears in a review diff.
    """
    constants, problems = enumerated_constants(path, agent_facing=agent_facing)
    if problems:
        raise ValueError(f"cannot derive pins: {problems}")
    return {
        constant.name: constant.digest for constant in constants
    }


#: The arms that EXIST, as far as this gate is concerned.
#:
#: Read from ``evals.run.ARMS`` at check time and UNIONED with the arms this
#: module's own drivers register, so an arm added to either place is
#: automatically a real arm. An arm name that is in neither is dangling, which
#: is the finding that catches an arm being renamed while a coverage entry kept
#: its old name - a rename that silently stops the pin from applying.
def registered_arms() -> Dict[str, str]:
    """Every arm name that exists, mapped to where it is defined."""
    arms: Dict[str, str] = {}
    try:
        from evals import run as evals_run

        for name in getattr(evals_run, "ARMS", {}) or {}:
            arms[str(name)] = "evals.run.ARMS"
    except Exception as exc:  # pragma: no cover - import guard
        arms["__evals_run_import_error__"] = f"{type(exc).__name__}: {exc}"
    for name, owner in _EXTRA_ARMS.items():
        arms.setdefault(str(name), owner)
    return arms


#: Arms this module's own drivers register. Kept here rather than imported so
#: the prompt pin does not depend on a driver that may be mid-refactor; an arm
#: that moves between drivers keeps its name and therefore keeps its coverage.
_EXTRA_ARMS: Dict[str, str] = {
    "eager": "evals.tools_ablation.ARMS",
    "deferred": "evals.tools_ablation.ARMS",
}

REGISTERED_ARMS: Tuple[str, ...] = ()


#: The constants this gate is REQUIRED to have an opinion about, independent of
#: what :data:`ARM_COVERAGE` currently holds.
#:
#: Written out rather than derived, because deriving it from ``ARM_COVERAGE`` is
#: precisely the bypass this exists to close - if the set of constants that must
#: be classified is itself read from the table somebody edits to disarm the gate,
#: then deleting a row deletes the requirement.
#:
#: The two rules are therefore deliberately redundant: a constant in
#: ``REVIEWED_AGENT_FACING`` must appear in ``ARM_COVERAGE`` or ``EXEMPTIONS``.
#: Redundant is the point. See the ``Rule 1b`` comment in :func:`check`.
REVIEWED_AGENT_FACING: Tuple[str, ...] = (
    "PLANNER_SYSTEM",
    "PLANNER_USER",
    "PLANNER_COORDINATION_RULES",
    "STEP_SYSTEM_TEMPLATE",
    "CONSTRAINT_REINJECTION_TEMPLATE",
    "STEERING_MSG_TEMPLATE",
    "CONTEXT_REINJECTION_TEMPLATE",
    "DAILY_SYSTEM",
    "DAILY_USER_TEMPLATE",
)

#: Arms that run EVERYTHING and therefore isolate nothing.
#:
#: This constant exists because the gate was DEMONSTRATED to be vacuous before
#: it was fixed. Every coverage entry included ``baseline``, ``baseline``
#: exists, so every edit to every prompt read as "armed" - and a gate that
#: passes everything is worse than no gate, because it manufactures the
#: confidence this phase exists to remove.
#:
#: The reasoning: an ablation arm's entire purpose is to DIFFER from baseline.
#: A prompt that only ``baseline`` exercises is exercised IDENTICALLY by every
#: arm in the matrix, so a change to it moves every arm together and no
#: comparison in the whole suite can see it. Such a prompt has **no eval arm
#: that would detect the change**, which is precisely the situation "prompts
#: land as eval arms, never as edits" is meant to exclude.
#:
#: So ``baseline`` - and ``pre_round``, which is the whole-round-off control -
#: do not count toward arming an edit.
CONTROL_ONLY_ARMS: Tuple[str, ...] = ("baseline", "pre_round")


#: Which arms exercise which prompt constant.
#:
#: Every name in :data:`AGENT_FACING` must appear here or in
#: :data:`EXEMPTIONS`. This is the "an arm declares the prompt variant it
#: covers" mechanism, and it is a declared mapping rather than a convention
#: because a convention is exactly what this gate replaces.
#:
#: The mappings are derived from the config keys the arms actually toggle, read
#: from ``evals.run.ARMS``:
#:
#: ==========================  =========================================
#: ``PLANNER_SYSTEM``          ``no_memory`` (plan_with_memory),
#:                             ``no_skills`` (skills_enabled)
#: ``STEP_SYSTEM_TEMPLATE``    ``no_docs``, ``no_webfetch``,
#:                             ``no_agent_tests``
#: ==========================  =========================================
#:
#: ``CONSTRAINT_REINJECTION_TEMPLATE`` and ``DAILY_SYSTEM`` are covered by
#: ``baseline`` alone, which is honest rather than flattering: every arm runs
#: the baseline prompt, so ``baseline`` genuinely exercises them, and a change
#: to them is measured by the whole matrix rather than by one arm.
ARM_COVERAGE: Dict[str, Tuple[str, ...]] = {
    "PLANNER_SYSTEM": ("baseline", "no_memory", "no_skills"),
    "PLANNER_USER": ("baseline",),
    "PLANNER_COORDINATION_RULES": ("baseline",),
    "STEP_SYSTEM_TEMPLATE": ("baseline", "no_docs", "no_webfetch", "no_agent_tests"),
    "CONSTRAINT_REINJECTION_TEMPLATE": ("baseline",),
    "STEERING_MSG_TEMPLATE": ("baseline",),
    "CONTEXT_REINJECTION_TEMPLATE": ("baseline",),
    "DAILY_SYSTEM": ("baseline",),
    "DAILY_USER_TEMPLATE": ("baseline",),
}


#: Per-skill permission claims are capped by the run's own envelope, and that
#: cap is computed in :mod:`extensions.skill_policy` and applied by the harness
#: strategy. This entry records WHY there is no prompt constant for it, so the
#: absence reads as a decision rather than as an oversight - and so a future
#: reader who wants to add prompt prose about permissions finds the enforcement
#: layer first.
EXEMPTIONS: Dict[str, Dict[str, str]] = {
    "SELF_CRITIQUE_SYSTEM": {
        "reason": (
            "single-shot maintenance call on a bounded diff, not the "
            "multi-turn loop prompt; the corpus's -2.3pp is a measurement about "
            "the loop prompt. NOT COVERED BY ANY EVAL ARM, which is a real "
            "coverage gap and is why this row exists"
        ),
        "owner": "T5",
        "reviewed": "P3b-W1",
    },
    "AGENT_TESTS_SYSTEM": {
        "reason": (
            "single-shot maintenance call; no eval arm exercises it. Recorded "
            "as a gap rather than omitted"
        ),
        "owner": "T5",
        "reviewed": "P3b-W1",
    },
}


#: The gate fails below this many pinned constants. A floor rather than an exact
#: count, so adding a prompt is fine and DELETING the pins is not.
MIN_PINNED_CONSTANTS = 5


#: THE EXEMPTION LIST IS COUNTED, because a growing exemption list is the pin
#: decaying and nothing else would show it.
#:
#: Every one of the five bypasses this gate was attacked with in P3b-W2 was
#: refused, and four of the five were refused by an EXEMPTION-related rule rather
#: than by the digest comparison:
#:
#: ==========================  ===============================================
#: attack                       refused by
#: ==========================  ===============================================
#: add an exemption             the digest still moves, so the edit is still seen
#:                              (``stale_exemption`` is a FINDING, not a pass)
#: empty the pins               ``MIN_PINNED_CONSTANTS``
#: shrink the coverage          the constant is now UNCLASSIFIED, so the digest
#:                              move alone cannot arm it
#: move the string              ``prompt_unreadable``
#: construct at runtime         ``prompt_unreadable``
#: ==========================  ===============================================
#:
#: None of those refusals depends on the exemption list staying short, which is
#: exactly why it needs its own bound: a gate whose integrity is carried by "we
#: have not added too many exemptions yet" has made its integrity a function of
#: an unwatched number. So the count is a PIN, not a convention - raising it is a
#: deliberate edit to this file and shows up in a diff on the rule rather than as
#: a mysteriously longer list.
#:
#: At the P3b-W2 round this is **2**: ``SELF_CRITIQUE_SYSTEM`` and
#: ``AGENT_TESTS_SYSTEM``, both single-shot maintenance calls with no eval arm.
#: Both are real coverage gaps, named rather than omitted.
MAX_EXEMPTIONS = 2


def _wildcard_exemption_findings() -> List[Dict[str, Any]]:
    """Refuse a blanket exemption, and refuse too many of them.

    An exemption keyed by a glob, a prefix, or any name not present in the
    agent-facing list is a bulk suppression. There is deliberately no way to
    write one that this function accepts, and no config path that skips it.

    THE COUNT IS CHECKED HERE, IN THE SAME FUNCTION, so the two ways an
    exemption list can rot - a blanket entry and a long list - are one check
    with one finding kind rather than two rules in two places. See
    :data:`MAX_EXEMPTIONS` for why a bound is needed at all when every bypass
    above is already refused structurally.
    """
    findings: List[Dict[str, Any]] = []
    if len(EXEMPTIONS) > MAX_EXEMPTIONS:
        findings.append(
            {
                "kind": "invalid_exemption",
                "constant": "",
                "detail": (
                    f"the exemption list holds {len(EXEMPTIONS)} entries, over "
                    f"the pinned maximum of {MAX_EXEMPTIONS}. Every entry needs "
                    "a reason and an owner, and that is what makes the list "
                    "reviewable; without a bound, reviewability decays "
                    "silently because each individual entry still looks "
                    "justified. Raise MAX_EXEMPTIONS deliberately, in this "
                    "file, so the change appears in a diff on the rule"
                ),
                "exemption_count": len(EXEMPTIONS),
                "max_exemptions": MAX_EXEMPTIONS,
                "exempted": sorted(EXEMPTIONS),
            }
        )
    for name, entry in EXEMPTIONS.items():
        if any(ch in str(name) for ch in "*?[]"):
            findings.append(
                {
                    "kind": "invalid_exemption",
                    "constant": str(name),
                    "detail": (
                        "exemption names a pattern, not a constant; a blanket "
                        "suppression is a deletion of the gate wearing a "
                        "config-file costume and is refused"
                    ),
                }
            )
            continue
        reason = str(entry.get("reason", "")).strip()
        owner = str(entry.get("owner", "")).strip()
        if not reason:
            findings.append(
                {
                    "kind": "invalid_exemption",
                    "constant": name,
                    "detail": "exemption has no reason; evidence, not a switch",
                }
            )
        if not owner:
            findings.append(
                {
                    "kind": "invalid_exemption",
                    "constant": name,
                    "detail": "exemption has no owner",
                }
            )
    return findings


def check(path: Optional[Path] = None) -> Dict[str, Any]:
    """Run the whole gate and return the publishable report.

    Seven finding kinds, all failing, all named. Exits 2 on any of them. There
    is no "warn" severity: a prompt edit nobody armed is not a warning, it is
    the thing the phase exists to prevent.
    """
    constants, problems = enumerated_constants(path)
    findings: List[Dict[str, Any]] = []
    if problems:
        findings.extend(
            {"kind": "prompt_unreadable", "constant": "", "detail": problem}
            for problem in problems
        )
        return {
            "schema_version": 1,
            "prompts_file": str(path or PROMPTS_PATH),
            "constants": [],
            "findings": findings,
            "finding_kinds": list(FINDING_KINDS),
            "ok": False,
            "verdict": "PROMPT_UNREADABLE",
        }

    current = {c.name: c for c in constants}
    arms = registered_arms()
    for finding in _wildcard_exemption_findings():
        findings.append(finding)

    if len(PINNED_DIGESTS) < MIN_PINNED_CONSTANTS:
        findings.append(
            {
                "kind": "baseline_emptied",
                "constant": "",
                "detail": (
                    f"PINNED_DIGESTS holds {len(PINNED_DIGESTS)} entries, "
                    f"below the floor of {MIN_PINNED_CONSTANTS}; deleting the "
                    "pins is a bigger diff than the edit being policed and is "
                    "refused"
                ),
            }
        )

    # Rule 1: dangling coverage. Checked with NO regard to digests, because a
    # coverage entry pointing at a renamed arm has stopped covering whether or
    # not the prompt moved today.
    for name in sorted(ARM_COVERAGE):
        for arm in ARM_COVERAGE[name]:
            if arm not in arms:
                findings.append(
                    {
                        "kind": "dangling_arm_coverage",
                        "constant": name,
                        "detail": (
                            f"coverage names arm {arm!r}, which no longer "
                            f"exists; a coverage set pointing at nothing has "
                            f"stopped covering"
                        ),
                        "arm": arm,
                        "known_arms": sorted(arms),
                    }
                )

    # Rule 1b: THE COVERAGE-TABLE SHRINK. Found by ATTACKING this gate, and it
    # was the whole gate.
    #
    # Deleting a constant from ARM_COVERAGE and then running `--update` and
    # pasting the new digest satisfied every rule above: the digest matched, so
    # there was no edit to report; MIN_PINNED_CONSTANTS was satisfied because a
    # digest was MOVED rather than deleted; CONTROL_ONLY_ARMS was never
    # consulted because there was no coverage entry left to consult; and
    # EXEMPTIONS was untouched. The gate returned ok=True on an unarmed edit to
    # the loop's system prompt - the exact -2.3pp change it exists to police,
    # passing because its own coverage table was edited in the same commit.
    #
    # The fix is to require COVERAGE for every reviewed agent-facing constant,
    # so removing coverage is a finding rather than a silent disarm. The
    # alternative - reading ARM_COVERAGE as authoritative - is what let this
    # through in the first place.
    for name in REVIEWED_AGENT_FACING:
        if name not in ARM_COVERAGE and name not in EXEMPTIONS:
            findings.append(
                {
                    "kind": "unclassified_prompt",
                    "constant": name,
                    "detail": (
                        f"agent-facing prompt {name!r} is reviewed but is "
                        "in neither ARM_COVERAGE nor EXEMPTIONS. Removing a "
                        "coverage entry is how this gate gets disarmed: with "
                        "no entry there is no coverage set to check against "
                        "CONTROL_ONLY_ARMS, so moving the digest alone makes "
                        "an unarmed edit pass. Classify it, or record it as a "
                        "named exemption with a reason and an owner"
                    ),
                    "reviewed": True,
                    "coverage": [],
                    "isolating_arms": [],
                }
            )

    rows: List[Dict[str, Any]] = []
    for name in sorted(AGENT_FACING):
        constant = current.get(name)
        if constant is None:
            continue
        pinned = PINNED_DIGESTS.get(name)
        digest = constant.digest
        coverage = tuple(ARM_COVERAGE.get(name, ()))
        exempted = name in EXEMPTIONS
        if pinned is None:
            state = "unclassified"
            rows.append(
                {
                    "constant": name,
                    "chars": constant.chars,
                    "lineno": constant.lineno,
                    "state": state,
                    "digest": digest,
                    "arms": list(coverage),
                    "exempt": exempted,
                }
            )
            continue
        if name not in ARM_COVERAGE and not exempted:
            # UNCLASSIFIED IS CHECKED BEFORE THE DIGEST, and that ordering is
            # the fix for the coverage-table-shrink bypass.
            #
            # The first version checked `digest == pinned` first, so a constant
            # whose coverage entry had been deleted AND whose digest had been
            # moved reported `unchanged` and passed. Deleting a row from
            # ARM_COVERAGE and running `--update` disarmed the gate completely:
            # no coverage entry means CONTROL_ONLY_ARMS is never consulted, and
            # a moved digest means there is no edit to report.
            #
            # So a reviewed constant with no coverage and no exemption is
            # reported as `unclassified` and fails, whatever its digest says.
            # Rule 1b already recorded the FINDING; this records the STATE, so
            # a reader of the per-constant table sees why.
            state = "unclassified"
        elif digest == pinned:
            state = "unchanged"
        elif exempted:
            state = "exempt_changed"
            findings.append(
                {
                    "kind": "stale_exemption",
                    "constant": name,
                    "detail": (
                        "the constant changed under an EXEMPTION. The "
                        "exemption's reason was written about the old content; "
                        "move PINNED_DIGESTS deliberately so the re-review "
                        "appears in a diff on this file"
                    ),
                    "reason_on_file": EXEMPTIONS.get(name, {}).get("reason", ""),
                }
            )
        elif coverage and all(arm in arms for arm in coverage):
            isolating = [
                arm for arm in coverage if arm not in CONTROL_ONLY_ARMS
            ]
            if isolating:
                state = "armed_edit"
            else:
                # Only control arms. Nothing in the matrix would see this
                # change move, so it is unarmed in the sense that matters.
                state = "unarmed_prompt_edit"
                findings.append(
                    {
                        "kind": "unarmed_prompt_edit",
                        "constant": name,
                        "detail": (
                            f"agent-facing prompt {name!r} changed and the ONLY "
                            f"arms exercising it are {list(coverage)}, which are "
                            "control arms: every arm in the matrix would see the "
                            "same text, so no comparison can detect this change. "
                            "Add an arm that differs from baseline and touches "
                            f"this prompt, list it in ARM_COVERAGE[{name!r}], "
                            "then move its digest with "
                            "`python -m evals.prompt_pin --update`"
                        ),
                        "coverage": list(coverage),
                        "isolating_arms": [],
                        "known_arms": sorted(arms),
                    }
                )
        else:
            state = "unarmed_prompt_edit"
            findings.append(
                {
                    "kind": "unarmed_prompt_edit",
                    "constant": name,
                    "detail": (
                        f"agent-facing prompt {name!r} changed and NO eval arm "
                        "exercises the change. Land it as an arm: register the "
                        "arm, add this constant to its ARM_COVERAGE entry, then "
                        "move its digest in PINNED_DIGESTS with "
                        "`python -m evals.prompt_pin --update`"
                    ),
                    "coverage": list(coverage),
                    "known_arms": sorted(arms),
                }
            )
        rows.append(
            {
                "constant": name,
                "chars": constant.chars,
                "lineno": constant.lineno,
                "state": state,
                "digest": digest,
                "arms": list(coverage),
                "exempt": exempted,
            }
        )

    ok = not findings
    # Prompts whose only coverage is control arms. NOT a finding - the tree is
    # green today and stays green - but published on every run, because it is the
    # work list: each one is an edit nobody would catch. Deleting this key would
    # hide a known gap, so a test asserts it is present and non-empty.
    coverage_gaps = sorted(
        {
            name
            for name in AGENT_FACING
            if name in ARM_COVERAGE
            and not [
                arm for arm in ARM_COVERAGE[name] if arm not in CONTROL_ONLY_ARMS
            ]
        }
    )
    return {
        "schema_version": 1,
        "prompts_file": str(path or PROMPTS_PATH),
        "agent_facing": list(AGENT_FACING),
        "constants": rows,
        "constants_tracked": len(rows),
        "armed_edits": [r["constant"] for r in rows if r["state"] == "armed_edit"],
        "unarmed_edits": [
            r["constant"] for r in rows if r["state"] == "unarmed_prompt_edit"
        ],
        "coverage_gaps": coverage_gaps,
        "coverage_gap_count": len(coverage_gaps),
        "coverage_gap_note": (
            "prompts whose only exercising arms are CONTROL arms "
            f"{list(CONTROL_ONLY_ARMS)}; an edit to any of them is not "
            "detectible by any comparison in the matrix, and the gate will "
            "refuse one. This is the work list, not a failure."
        ),
        "exemptions": {k: dict(v) for k, v in EXEMPTIONS.items()},
        # The count is published on every run for the same reason
        # ``coverage_gaps`` is: a number nobody looks at is a number that decays
        # without anybody noticing, and the exemption list is the one part of
        # this gate that gets LONGER as the project ages.
        "exemption_count": len(EXEMPTIONS),
        "max_exemptions": MAX_EXEMPTIONS,
        "exemption_headroom": MAX_EXEMPTIONS - len(EXEMPTIONS),
        "registered_arms": sorted(arms),
        "findings": findings,
        "finding_kinds": list(FINDING_KINDS),
        "min_pinned_constants": MIN_PINNED_CONSTANTS,
        "ok": ok,
        "verdict": "PROMPTS_ARMED" if ok else "PROMPTS_UNARMED",
    }


def render(doc: Mapping[str, Any]) -> str:
    """The human table. The report is the product; this is how it is read."""
    lines = [
        f"prompt pin: {doc['verdict']}",
        "",
        f"{'constant':34} {'chars':>6} {'state':16} arms",
        "-" * 88,
    ]
    for row in doc.get("constants", []):
        lines.append(
            f"{row['constant']:34} {row['chars']:>6} {row['state']:16} "
            f"{','.join(row['arms']) or ('EXEMPT' if row['exempt'] else '-')}"
        )
    lines += [
        "",
        f"registered arms: {len(doc.get('registered_arms', []))}",
        (
            f"exemptions: {doc.get('exemption_count')}/"
            f"{doc.get('max_exemptions')} "
            f"({', '.join(sorted(doc.get('exemptions', {}))) or 'none'})"
        ),
        (
            f"coverage gaps: {doc.get('coverage_gap_count')} "
            f"({', '.join(doc.get('coverage_gaps', [])) or 'none'})"
        ),
        f"findings: {len(doc.get('findings', []))}",
    ]
    for finding in doc.get("findings", []):
        lines.append(
            f"  [{finding['kind']}] {finding.get('constant') or '-'}: "
            f"{finding['detail']}"
        )
    if not doc.get("findings"):
        lines.append("  none")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI. Exit 2 on any finding. ``--update`` PRINTS the digests, not writes.

    Printing rather than writing is deliberate: the digest move must be a human
    edit to a tracked file so it shows up in review. A gate that can rewrite its
    own baseline is a gate whose baseline means nothing.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.prompt_pin",
        description="Fail an agent-facing prompt edit that no eval arm exercises.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--update",
        action="store_true",
        help="print the current digests for pasting into PINNED_DIGESTS",
    )
    args = parser.parse_args(argv)
    if args.update:
        try:
            print(json.dumps(update_pins(), indent=2, sort_keys=True))
        except ValueError as exc:
            print(f"prompt pin: {exc}")
            return 2
        return 0
    doc = check()
    print(json.dumps(doc, indent=2, default=str) if args.json else render(doc))
    return 0 if doc["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
