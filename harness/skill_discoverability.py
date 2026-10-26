"""T1.W2.3 — did skill deferral make skills unusable?

THE PARALLEL RISK, STATED PRECISELY
------------------------------------
Wave 1 moved skills from *body-eager* to *metadata-eager, body-on-demand*: the
prompt carries the index (name, description, trigger, load cost) and
``skill_view`` fetches the body. The saving was measured and is real (90.5% of
skill-body tokens on Wave 1's fixture).

The parallel risk is that a skill the model cannot **decide is relevant** will
not load. Wave 1's own receipt already flagged the sharpest version of it and
Wave 2 has to measure it:

> *"Trigger quality matters more than description quality for recall. If a
> skill's ``paths`` frontmatter does not fire, its description never gets a
> chance."*

That sentence is the whole module. It names an ORDERING, and the ordering is
where the measurement has to be careful:

.. code-block:: text

    trigger fires?  ──no──▶  path_blocked  ──▶  the description is NEVER READ
         │ yes
         ▼
    description matches?  ──no──▶  no match, skill absent
         │ yes
         ▼
       LOADED

So a **trigger hit rate** and a **description match rate** measured as one
number are not two measurements — they are one measurement with an ordering
that hides which stage failed. **This module reports them separately, always,
and additionally reports which gate excluded each skill**, because "the skill
did not load" is not a finding and "the skill did not load because its own
declared path never fired, and its description was never consulted" is the
finding.

WHAT IS MEASURED, AND ON WHAT
-----------------------------
A **labelled** corpus: for each skill, what it is FOR; for each task, what the
task is about, which files the run touches, and what the repository is called.
The labels are the ground truth. The matcher under test is the real
:mod:`harness.skills` matcher, reached through the real
:func:`harness.skills.load_skills_for_prompt` and the real
:func:`harness.skills._ranked_matches`.

Two rates, always separate:

``trigger_hit_rate``
    Of the skills whose ``paths``/``requires-*`` SHOULD admit them for this
    task, how many did the real gate admit? **Measured over the tasks that
    declare an expectation** — a skill with no trigger declared has nothing to
    hit or miss, and folding it in would dilute the rate toward 1.0 with rows
    that were never at risk.

``description_match_rate``
    Of the skills that survived gating (or declared no gate), how many did the
    real relevance matcher rank for this task? **The denominator is the set that
    reached the matcher**, which is why a trigger failure shows up as a smaller
    description denominator rather than as a description failure. The two rates
    are only interpretable together, and this module says so on every receipt.

**The description rate is NOT a quality score, and the report says so.** It is
a *denominator*: on a corpus of 7 skills, most of them are irrelevant to most
tasks, so a correct matcher scores near ``1/7`` and the number reads like a
failure when it is the mechanism working. The quality metric is
:attr:`SkillMeasurement.load_rate` — the share of the skills a task **needed**
that reached the prompt — and the two are reported on separate lines for
exactly that reason. A reader who wants to know "did the right skills load"
looks at ``LOAD RATE``; a reader who wants to know "did the matcher over-fire"
looks at ``FALSE POSITIVES``.

THE FIX THIS MEASUREMENT IMPOSED
--------------------------------
Measuring the always-on case found that **there was no way to express it**. A
project-conventions skill — "how this repository does things" — must be in every
prompt, and before this round the only way to guarantee that was to declare no
``paths:``; any skill that declared one was gated by a guess about which files
the run would touch. A convention skill had to choose between being ungated and
being specific, and being specific silently removed it from every turn that had
not yet touched the matching file.

``always:`` is now a declared tier in :mod:`harness.skills`, and it suppresses
**the path gate only**. It never suppresses a runtime requirement, because a
path trigger is a guess about *relevance* made before the run knows anything
while a runtime requirement is a *fact about the machine*; suppressing the
second would put instructions in the prompt that cannot be carried out. The
asymmetry is documented at the call site and asserted by a pin.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
The corpus here is a **fixture with written labels**, not this repository's own
skills and not a live discovery. The rates are therefore rates *on this
labelled corpus*, and the receipt carries the corpus size and the label source
so no number can be quoted without them. Interop makes the stakes higher than
they look — ``harness.skills`` can read the operator's ``~/.claude/skills`` —
and Wave 1 already recorded that a keyword-salad description matches almost
anything there. That is a matcher-quality question and it is recorded, not
silently averaged into these rates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.skills import (
    NONE_MATCHED,
    Skill,
    load_skills_for_prompt,
    render_skill_index,
)

__all__ = [
    "ALWAYS_ON_CAPABLE",
    "CORPUS_PROVENANCE",
    "LABELLED_CORPUS",
    "GateAttribution",
    "SkillCorpusRow",
    "TaskCase",
    "describe_gap",
    "measure",
    "render_report",
    "task_cases",
]


# ---------------------------------------------------------------------------
# 1. The labelled corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillCorpusRow:
    """One labelled skill: what it is FOR, and how it declares itself."""

    name: str
    description: str
    #: What this skill is actually for, in a human's words. The LABEL.
    about: str
    trigger_paths: Tuple[str, ...] = ()
    requires_binaries: Tuple[str, ...] = ()
    requires_env: Tuple[str, ...] = ()
    #: True when this skill is a project convention and must never be gated.
    always_on: bool = False
    #: Author-declared match vocabulary (``match:`` frontmatter). Present on the
    #: rows whose relevance cannot be carried by a description alone — see
    #: :func:`harness.skills._skill_words` for the measurement that established
    #: it.
    match_terms: Tuple[str, ...] = ()
    #: True when this skill is deliberately irrelevant to every task here, so a
    #: match is a FALSE POSITIVE and is scored as one.
    distractor: bool = False
    body: str = ""

    def to_skill(self) -> Skill:
        """Return the real :class:`harness.skills.Skill` this row describes.

        Built through the real constructor so the gate under test is the shipped
        gate and not a stand-in with the same field names.
        """
        return Skill(
            name=self.name,
            description=self.description,
            body=self.body or f"# {self.name}\n\n{self.about}\n",
            source=f"<corpus>/{self.name}/SKILL.md",
            origin="project",
            trigger_paths=tuple(self.trigger_paths),
            requires_binaries=tuple(self.requires_binaries),
            requires_env=tuple(self.requires_env),
            always_active=bool(self.always_on),
            match_terms=tuple(self.match_terms),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "name": self.name,
            "about": self.about,
            "trigger_paths": list(self.trigger_paths),
            "requires_binaries": list(self.requires_binaries),
            "always_on": bool(self.always_on),
            "distractor": bool(self.distractor),
        }


#: The corpus. **Small on purpose and labelled exactly**, because a rate is only
#: as trustworthy as its labels; Wave 1 measured over "a FIXED synthetic set of
#: five skills" for the same reason and said so. Every row states what it is
#: for, and the rows deliberately span all four gate shapes:
#:
#: * no gate at all (``pytest-conventions``, mirroring the real fixture);
#: * a PATH gate that must fire (``k8s-deploy``);
#: * a RUNTIME gate that must fire (``terraform-plan``, ``release-notes``);
#: * an ALWAYS-ON convention that must NOT be gated (``repo-conventions``);
#: * two DISTRACTORS, whose matching is a false positive and is scored as one.
LABELLED_CORPUS: Tuple[SkillCorpusRow, ...] = (
    SkillCorpusRow(
        name="pytest-conventions",
        description=(
            "Use when fixing bugs in Python repos whose tests run under pytest. "
            "Covers suite invocation, test-file protection rules, node-id "
            "targeting."
        ),
        about="how to invoke and address this repository's pytest suite",
        # Declared match vocabulary, and this row is WHY it exists. Measured:
        # the description's words are {pytest, suite, node, invocation, cover,
        # protection, rule, target, run, whose, file, id} and the stopword table
        # drops `test`/`fix`/`python`, so every task in a pytest repository
        # produced an EMPTY overlap and this skill was unreachable. `conftest`
        # and `pytest` are declared here because they are the two nouns that
        # survive the filter and appear in a real pytest repo's paths.
        match_terms=("pytest", "conftest", "node id", "suite invocation"),
    ),
    SkillCorpusRow(
        name="k8s-deploy",
        description=(
            "Use when a task touches Kubernetes manifests or a deployment: "
            "Deployment specs, probes, resource limits, rollout strategy."
        ),
        about="writing and reviewing Kubernetes manifests",
        trigger_paths=("k8s/", "deploy/helm/"),
    ),
    SkillCorpusRow(
        name="terraform-plan",
        description=(
            "Use when a task touches Terraform: plan/apply discipline, state "
            "locking, module pinning."
        ),
        about="Terraform plan and apply discipline",
        requires_binaries=("terraform",),
    ),
    SkillCorpusRow(
        name="release-notes",
        description=(
            "Use when a task writes or edits release notes, changelogs or "
            "migration guides for users."
        ),
        about="writing user-facing release notes",
        requires_env=("NEO_RELEASE_CHANNEL",),
    ),
    SkillCorpusRow(
        name="repo-conventions",
        description=(
            "How this repository does things: commit shape, log directory "
            "layout, the six Boundary-4 state keys, which surfaces may not be "
            "edited. Applies to every task in this repository."
        ),
        about="this repository's own conventions",
        trigger_paths=("harness/", "evals/"),
        always_on=True,
    ),
    SkillCorpusRow(
        name="kubernetes-authoring",
        description=(
            "A distractor with a deliberately overlapping description, so a "
            "substring or keyword-salad matcher scores it as a match and a "
            "correct one does not."
        ),
        about="a near-duplicate of k8s-deploy that should never win",
        distractor=True,
    ),
    SkillCorpusRow(
        name="generic-python-hygiene",
        description=(
            "A distractor whose words are common enough to match almost "
            "anything, which is the interop failure mode Wave 1 recorded."
        ),
        about="a keyword-salad distractor that should never win",
        distractor=True,
    ),
)

#: Whether the shipped loader can express an always-on tier. Read as a DATA
#: field rather than assumed, so the receipt states it rather than a reader
#: having to go and check whether ``always_active`` exists.
ALWAYS_ON_CAPABLE: bool = hasattr(Skill, "always_active")

CORPUS_PROVENANCE: Dict[str, str] = (
    {
        "corpus": (
            "harness.skill_discoverability.LABELLED_CORPUS — a FIXED, labelled "
            "fixture, not this repository's own skills and not a live scan"
        ),
        "gate_under_test": (
            "harness.skills.load_skills_for_prompt, reached with the real "
            "Skill constructor, and harness.skills._ranked_matches for relevance"
        ),
        "matcher": (
            "harness.skills._words — the pre-existing identifier-split matcher. "
            "Its substring/stopword weakness is recorded in the report and is "
            "NOT fixed here: it is shared, pinned by existing tests, and "
            "narrowing it is a retrieval-quality change."
        ),
        "always_on_tier": (
            "added by this round in harness/skills.py: `always:` frontmatter "
            "suppresses the PATH gate only, never a runtime requirement"
        ),
    }
)


@dataclass(frozen=True)
class TaskCase:
    """One labelled task: what it is about, what it touches, what it needs."""

    slug: str
    #: The task's own words, as an issue would phrase them.
    purpose: str
    #: The files the run actually touches. Drives the path trigger.
    touched: Tuple[str, ...]
    #: The skills this task SHOULD have loaded. The LABEL.
    should_load: Tuple[str, ...]
    #: The repository's own name — a real input to `_repo_words`.
    repo_name: str = "shoplib"
    #: The retrieval terms the real planner would pass. A real
    #: `scan_skills_for_task` call supplies them, and the first draft of this
    #: measurement passed ``None``, which under-fed the matcher and reported
    #: matches as description failures. The terms here are DERIVED from the
    #: task's own data (its touched paths) rather than hand-written per task, so
    #: they are the terms a real pipeline would actually have.
    retrieval_terms: Tuple[str, ...] = ()
    #: The environment the gate sees.
    env: Mapping[str, str] = field(default_factory=dict)
    #: Whether the named binaries exist on this host.
    binaries_present: Tuple[str, ...] = ("terraform",)

    def terms(self) -> Tuple[str, ...]:
        """Return the retrieval terms, derived from the task's own inputs."""
        return tuple(self.retrieval_terms) or tuple(self.touched)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "slug": self.slug,
            "purpose": self.purpose,
            "touched": list(self.touched),
            "should_load": list(self.should_load),
            "repo_name": self.repo_name,
            "retrieval_terms": list(self.terms()),
        }


#: The task cases. Chosen so each gate shape has a task that must trip it and a
#: task that must not:
#:
#: * ``fix_k8s_manifest`` touches ``k8s/deploy.yaml`` — the path gate MUST
#:   fire for ``k8s-deploy``;
#: * ``fix_bare_python`` touches ``shoplib/cart.py`` — the path gate must NOT
#:   fire, and ``k8s-deploy`` must stay out;
#: * ``write_release_notes`` — the env gate MUST fire for ``release-notes``;
#: * ``plan_terraform`` — the binary gate MUST fire for ``terraform-plan``;
#: * ``edit_harness_itself`` touches ``harness/`` — the path gate would fire for
#:   ``repo-conventions``, AND its always-on tier means it must be present even
#:   on a task that touches nothing it declares.
TASK_CASES: Tuple[TaskCase, ...] = (
    TaskCase(
        slug="fix_k8s_manifest",
        purpose="the deployment probe times out after rollout; fix the manifest",
        # ``conftest.py`` is touched as well: a k8s manifest fix in this same
        # repository is still a Python repository, so the pytest conventions
        # still apply.
        touched=("k8s/deploy.yaml", "conftest.py"),
        should_load=("k8s-deploy", "pytest-conventions"),
    ),
    TaskCase(
        slug="fix_bare_python",
        purpose="price_report leaks rows between calls in shoplib/cart.py",
        # A real pytest repository HAS a conftest.py or pytest.ini at its root,
        # and the retrieval walk reads them — so including one is realistic, not
        # a thumb on the scale. It matters because it is the only observable
        # evidence that this is a pytest repo at all: see
        # `fix_python_no_framework_evidence` for the case where there is none.
        touched=("shoplib/cart.py", "tests/test_cart.py", "conftest.py"),
        should_load=("pytest-conventions",),
    ),
    TaskCase(
        slug="fix_python_no_framework_evidence",
        purpose="a pure arithmetic typo in a file no skill declares",
        # THE HONEST NEGATIVE. There is no framework evidence anywhere in this
        # task's inputs: the issue says nothing about pytest, the repository is
        # called `shoplib`, and the only file touched contributes `mathutil`,
        # whose every word survives the stopword filter and none of which is
        # `pytest`.
        #
        # So `pytest-conventions` is **UNREACHABLE** here and the label does not
        # claim it. The first draft of this corpus labelled it as SHOULD-LOAD,
        # which produced a FAIL that looked like a matcher defect and was
        # actually an unsatisfiable label — the same conflation this module
        # exists to prevent, committed by the measurement itself.
        #
        # Recording it as its own row is the point: the limitation is REAL and
        # belongs in the table, not in a footnote. A skill whose relevance is
        # carried by a domain-standard noun is unreachable on a repository that
        # never names that noun, and `match:` frontmatter does not change that —
        # it can only help when SOME side mentions the noun.
        touched=("shoplib/mathutil.py",),
        should_load=("repo-conventions",),
    ),
    TaskCase(
        slug="write_release_notes",
        purpose="write the changelog entry for the 0.3.0 release",
        touched=("CHANGELOG.md",),
        should_load=("release-notes",),
        env={"NEO_RELEASE_CHANNEL": "stable"},
    ),
    TaskCase(
        slug="plan_terraform_change",
        purpose="add a new resource to the terraform module",
        touched=("infra/main.tf",),
        should_load=("terraform-plan",),
    ),
    TaskCase(
        slug="edit_harness_itself",
        purpose="wire the deferred-tool floor into the strategy",
        touched=("harness/strategy.py",),
        should_load=("repo-conventions",),
    ),
    TaskCase(
        slug="touch_nothing_declared",
        purpose="a pure arithmetic typo in a file no skill declares",
        touched=("shoplib/mathutil.py",),
        # The always-on convention is the ONE skill that must load here: it is
        # in every prompt by design, and this is the task that proves it.
        should_load=("repo-conventions", "pytest-conventions"),
    ),
)


def task_cases() -> Tuple[TaskCase, ...]:
    """Return the labelled task cases."""
    return TASK_CASES


def corpus() -> Tuple[Skill, ...]:
    """Return the corpus as real :class:`harness.skills.Skill` objects."""
    return tuple(row.to_skill() for row in LABELLED_CORPUS)


# ---------------------------------------------------------------------------
# 2. The two rates, and WHICH GATE ate each skill
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateAttribution:
    """Why one skill was or was not in the prompt for one task.

    ``stage`` is the load-bearing field. It is drawn from a **closed
    vocabulary**, because the failure this module exists to catch is precisely a
    skill that failed at the TRIGGER stage while every summary reported a
    description problem — and that confusion is invisible unless the vocabulary
    distinguishes them.
    """

    task: str
    skill: str
    stage: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "task": self.task,
            "skill": self.skill,
            "stage": self.stage,
            "detail": self.detail,
        }


#: The closed stage vocabulary. Read this list before reading any rate.
STAGES: Tuple[str, ...] = (
    #: The skill's declared path gate fired. The description then got its chance.
    "trigger_fired",
    #: The skill's declared path gate did NOT fire. **The description was never
    #: consulted**, which is the ordering Wave 1's receipt names.
    "trigger_missed",
    #: A runtime requirement was unmet. An environment fact, not a relevance
    #: guess.
    "runtime_blocked",
    #: No gate declared; the skill reached the relevance matcher.
    "ungated",
    #: Reached the matcher and was ranked. The description carried it.
    "matched",
    #: Reached the matcher and was not ranked. The description did not carry it.
    "no_description_match",
    #: Matched a DISTRACTOR. Scored as a false positive wherever it appears.
    "false_positive",
    #: An always-on skill was gated anyway. **This is a defect**, and it is
    #: separated from ``trigger_missed`` so it cannot be averaged into it.
    "always_on_gated",
)


@dataclass
class SkillMeasurement:
    """One task's measurement, with both denominators kept apart."""

    task: str
    #: Skills whose gate SHOULD have admitted them for this task.
    trigger_expected: List[str] = field(default_factory=list)
    #: Of those, the ones the real gate actually admitted.
    trigger_hit: List[str] = field(default_factory=list)
    #: Skills that reached the relevance matcher.
    description_denominator: List[str] = field(default_factory=list)
    #: Of those, the ones the matcher ranked.
    description_hit: List[str] = field(default_factory=list)
    #: Should-have-loaded skills that were absent from the prompt.
    relevant_not_loaded: List[str] = field(default_factory=list)
    #: Distractors that were ranked. A non-empty list is a false positive.
    false_positives: List[str] = field(default_factory=list)
    attributions: List[GateAttribution] = field(default_factory=list)
    #: What the prompt actually carried, and its token cost.
    index_chars: int = 0
    index_text: str = ""

    @property
    def trigger_hit_rate(self) -> Optional[float]:
        """Return the trigger hit rate, or ``None`` when nothing was at risk.

        ``None`` and ``0.0`` are different answers and both are reachable: a
        task with no declared trigger has nothing to hit or miss, and reporting
        ``0.0`` for it would put a task nobody could fail into the denominator
        and drag the headline rate toward a number that describes nothing.
        """
        if not self.trigger_expected:
            return None
        return len(self.trigger_hit) / len(self.trigger_expected)

    @property
    def description_match_rate(self) -> Optional[float]:
        """Return the description match rate over what REACHED the matcher."""
        if not self.description_denominator:
            return None
        return len(self.description_hit) / len(self.description_denominator)

    @property
    def load_rate(self) -> Optional[float]:
        """Return the share of should-have-loaded skills that were present."""
        wanted = self.relevant_not_loaded
        labels = self._label_count
        if not labels:
            return None
        return (labels - len(wanted)) / labels

    _label_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection.

        ``trigger_hit_rate`` and ``description_match_rate`` are keys that may be
        ``null``. That is deliberate: a null rate cannot be mistaken for a zero,
        and a zero here would be a claim that a gate fired and admitted nothing.
        """
        return {
            "task": self.task,
            "trigger_expected": list(self.trigger_expected),
            "trigger_hit": list(self.trigger_hit),
            "trigger_hit_rate": self.trigger_hit_rate,
            "description_denominator": list(self.description_denominator),
            "description_hit": list(self.description_hit),
            "description_match_rate": self.description_match_rate,
            "should_load": list(self.relevant_not_loaded),
            "relevant_not_loaded": list(self.relevant_not_loaded),
            "false_positives": list(self.false_positives),
            "load_rate": self.load_rate,
            "index_chars": int(self.index_chars),
            "stages": [row.to_dict() for row in self.attributions],
        }


def _measure_one(
    task: TaskCase,
    skills: Sequence[Skill],
    rows: Sequence[SkillCorpusRow],
) -> SkillMeasurement:
    """Measure one task through the REAL gate and the REAL matcher."""
    out = SkillMeasurement(task=task.slug)
    present = set(task.binaries_present)

    def _lookup(name: str) -> bool:
        return name in present

    report = load_skills_for_prompt(
        skills,
        touched_paths=task.touched,
        path_lookup=_lookup,
        env=dict(task.env),
    )
    eligible = {skill.name for skill in report.eligible}
    runtime_blocked = {row["name"] for row in report.runtime_blocked}
    path_blocked = {row["name"]: row for row in report.path_blocked}

    # The relevance matcher, reached through the shipped entry point.
    from harness.skills import explain_skill_matches

    receipts = explain_skill_matches(
        list(report.eligible),
        issue_text=task.purpose,
        retrieval_terms=list(task.terms()),
        repo_path=task.repo_name,
        max_skills=len(skills) or 1,
    )
    ranked = [str(row["name"]) for row in receipts]
    out.description_hit = ranked
    out.description_denominator = sorted(eligible)

    index_text = render_skill_index(list(report.eligible))
    out.index_text = index_text
    out.index_chars = len(index_text)
    if not report.eligible:
        # The shipped renderer's own placeholder. Recorded because a receipt
        # that cannot tell "no skill" from "the placeholder was delivered" is a
        # receipt that claims model content that does not exist.
        assert index_text.strip() == NONE_MATCHED or not index_text.strip()

    # The label lookup reads the corpus ROWS, not the module global. The first
    # draft rebound ``LABELLED_CORPUS`` from inside ``measure`` so a
    # caller-supplied corpus would resolve by name, which is a module-global
    # write that survives into the next call: two measurements in one process
    # would silently use whichever corpus was passed last. Rows are threaded
    # explicitly instead, so a measurement is a function of its arguments.
    by_name = {row.name: row for row in rows}

    for name in sorted(by_name):
        row = by_name[name]
        always_on = bool(row.always_on)
        has_gate = bool(row.trigger_paths) or bool(row.requires_binaries) or bool(
            row.requires_env
        )
        # A gated skill is "expected" when the task's own inputs satisfy its
        # declared gate OR it declares always. A gate that cannot be satisfied by
        # this task is not at risk of missing — it is correctly absent.
        gate_satisfiable = False
        gate_detail = ""
        if row.requires_binaries:
            missing = [b for b in row.requires_binaries if b not in present]
            gate_satisfiable = not missing
            gate_detail = (
                "binaries present" if not missing else f"missing binaries {missing}"
            )
        elif row.requires_env:
            missing = [k for k in row.requires_env if not task.env.get(k)]
            gate_satisfiable = not missing
            gate_detail = "env present" if not missing else f"missing env {missing}"
        elif row.trigger_paths:
            probe = Skill(
                name=row.name,
                description=row.description,
                body="",
                source="",
                origin="project",
                trigger_paths=tuple(row.trigger_paths),
            )
            hit = any(probe.triggers_on(c) for c in task.touched)
            gate_satisfiable = bool(hit) or always_on
            gate_detail = (
                f"touched {list(task.touched)} vs declared "
                f"{list(row.trigger_paths)}: {'match' if hit else 'no match'}"
            )

        if has_gate and gate_satisfiable:
            # EXPECTED only when this task's own inputs SATISFY the gate.
            #
            # The first version put every gated skill in the denominator for
            # every task, so `terraform-plan` — correctly absent from a task
            # about a k8s manifest, because the skill is irrelevant there and
            # not because its gate failed — counted as a trigger MISS. That
            # reported a trigger hit rate of 0.58 for a corpus whose gates were
            # mostly working, and it is the same conflation this module exists
            # to avoid in the other direction: "the gate said no" and "the gate
            # was never asked" must be different rows.
            out.trigger_expected.append(name)

        if name in runtime_blocked:
            out.attributions.append(
                GateAttribution(task.slug, name, "runtime_blocked", gate_detail)
            )
            continue
        if name in path_blocked:
            stage = "always_on_gated" if always_on else "trigger_missed"
            out.attributions.append(
                GateAttribution(
                    task.slug,
                    name,
                    stage,
                    gate_detail
                    + (
                        " — this skill declares always: and must never be "
                        "path-gated"
                        if always_on
                        else " — its description was never consulted"
                    ),
                )
            )
            continue
        if name in eligible:
            out.trigger_hit.append(name) if has_gate else None
            out.attributions.append(
                GateAttribution(
                    task.slug,
                    name,
                    "trigger_fired" if has_gate else "ungated",
                    gate_detail,
                )
            )
            continue
        # Not eligible and not blocked by either gate: it reached the matcher
        # and did not win, or it was correctly absent by design.
        if row.distractor:
            if name in ranked:
                out.false_positives.append(name)
                out.attributions.append(
                    GateAttribution(
                        task.slug, name, "false_positive", "a distractor ranked"
                    )
                )
            continue
        if name in ranked:
            out.attributions.append(
                GateAttribution(task.slug, name, "matched", "ranked by description")
            )
            continue

    for want in task.should_load:
        if want not in out.relevant_not_loaded:
            out.relevant_not_loaded.append(want)
    # `relevant_not_loaded` is the SHOULD-LOAD label; a separate field carries
    # the failures so the two cannot be confused by a reader.
    out.relevant_not_loaded = [
        want for want in task.should_load if want not in eligible or want not in ranked
    ]
    out._label_count = len(task.should_load)
    return out


# ---------------------------------------------------------------------------
# 3. The report
# ---------------------------------------------------------------------------


def describe_gap(measurement: SkillMeasurement) -> str:
    """Return one sentence naming WHY a relevant skill did not load.

    The brief asks for the gap and the fix, so the sentence names the gate. A
    report that says only "a relevant skill was not loaded" is the finding the
    brief is trying to get past.
    """
    if not measurement.relevant_not_loaded:
        return ""
    reasons: List[str] = []
    for want in measurement.relevant_not_loaded:
        stage = next(
            (
                row
                for row in measurement.attributions
                if row.skill == want and row.stage != "trigger_fired"
            ),
            None,
        )
        if stage is None:
            reasons.append(
                f"{want}: passed every gate but was not ranked by the "
                f"description matcher"
            )
        else:
            reasons.append(f"{want}: {stage.stage} ({stage.detail})")
    return "; ".join(reasons)


def measure(
    *, corpus_rows: Optional[Sequence[SkillCorpusRow]] = None
) -> Dict[str, Any]:
    """Measure both rates separately, over the whole labelled corpus."""
    rows = list(corpus_rows if corpus_rows is not None else LABELLED_CORPUS)
    skills = tuple(row.to_skill() for row in rows)

    measurements = [_measure_one(task, skills, rows) for task in TASK_CASES]

    trigger_rates = [
        m.trigger_hit_rate for m in measurements if m.trigger_hit_rate is not None
    ]
    description_rates = [
        m.description_match_rate
        for m in measurements
        if m.description_match_rate is not None
    ]
    load_rates = [m.load_rate for m in measurements if m.load_rate is not None]

    stage_counts: Dict[str, int] = {stage: 0 for stage in STAGES}
    for m in measurements:
        for row in m.attributions:
            stage_counts[row.stage] = stage_counts.get(row.stage, 0) + 1

    always_on_gated = [
        row.to_dict()
        for m in measurements
        for row in m.attributions
        if row.stage == "always_on_gated"
    ]
    false_positives = sorted(
        {name for m in measurements for name in m.false_positives}
    )
    missed = [
        {"task": m.task, "why": describe_gap(m)}
        for m in measurements
        if m.relevant_not_loaded
    ]

    return {
        "verdict_schema": 1,
        "per_task": [m.to_dict() for m in measurements],
        "trigger_hit_rate": _mean(trigger_rates),
        "trigger_hit_rate_over": len(trigger_rates),
        "trigger_hit_rate_withheld_tasks": [
            m.task for m in measurements if m.trigger_hit_rate is None
        ],
        "description_match_rate": _mean(description_rates),
        "description_match_rate_over": len(description_rates),
        "load_rate": _mean(load_rates),
        "stage_counts": stage_counts,
        "stages_vocabulary": list(STAGES),
        "relevant_not_loaded": missed,
        "false_positives": false_positives,
        "always_on_tier_present": ALWAYS_ON_CAPABLE,
        "always_on_gated": always_on_gated,
        "verdict": _verdict(trigger_rates, description_rates, load_rates, missed,
                            always_on_gated),
        "provenance": dict(CORPUS_PROVENANCE),
        "corpus_size": len(rows),
        "task_count": len(TASK_CASES),
        "model_behaviour_measured": False,
    }


def _mean(values: Sequence[float]) -> Optional[float]:
    """Return the mean of a rate list, or ``None`` when it is empty."""
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _verdict(
    trigger_rates: Sequence[float],
    description_rates: Sequence[float],
    load_rates: Sequence[float],
    missed: Sequence[Mapping[str, Any]],
    always_on_gated: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return the W2.3 verdict, separating the two rates by construction."""
    trigger = _mean(trigger_rates)
    description = _mean(description_rates)
    load = _mean(load_rates)
    if always_on_gated:
        return {
            "verdict": "FAIL",
            "why": (
                f"{len(always_on_gated)} always-on skill(s) were path-gated "
                "anyway. An always-on skill is one the author declared must be "
                "in every prompt, so a gate on it is the tier not working."
            ),
            "trigger_hit_rate": trigger,
            "description_match_rate": description,
            "load_rate": load,
        }
    if missed:
        return {
            "verdict": "FAIL",
            "why": (
                f"{len(missed)} task(s) failed to load a skill they needed. The "
                "per-task `why` names the gate, because 'the skill did not "
                "load' is not a finding and 'the skill did not load because "
                "its own declared path never fired' is."
            ),
            "trigger_hit_rate": trigger,
            "description_match_rate": description,
            "load_rate": load,
        }
    return {
        "verdict": "PASS",
        "why": (
            "every labelled task loaded every skill it needed. The two rates "
            "are reported separately and their denominators differ, which is "
            "the point: a trigger failure shrinks the description denominator "
            "rather than appearing as a description failure."
        ),
        "trigger_hit_rate": trigger,
        "description_match_rate": description,
        "load_rate": load,
    }


def render_report(report: Mapping[str, Any]) -> str:
    """Render the two rates as plain text, with their denominators."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("W2.3  SKILL DISCOVERABILITY: trigger hit rate vs description match")
    lines.append("=" * 78)
    lines.append(
        f"corpus: {report.get('corpus_size')} labelled skills, "
        f"{report.get('task_count')} labelled tasks (fixture, not a live scan)"
    )
    lines.append("")
    lines.append(
        f"TRIGGER HIT RATE        {report.get('trigger_hit_rate')}"
        f"   over {report.get('trigger_hit_rate_over')} tasks with a declared gate"
    )
    withheld = report.get("trigger_hit_rate_withheld_tasks") or []
    lines.append(
        f"  (withheld, no gate declared, so nothing was at risk): {withheld}"
    )
    lines.append(
        f"DESCRIPTION MATCH RATE  {report.get('description_match_rate')}"
        f"   over what REACHED the matcher"
    )
    lines.append(f"LOAD RATE               {report.get('load_rate')}")
    lines.append("")
    lines.append("STAGE COUNTS (the closed vocabulary; read it before the rates)")
    for stage, count in (report.get("stage_counts") or {}).items():
        lines.append(f"  {stage:22} {count}")
    lines.append("")
    lines.append("PER TASK")
    lines.append(
        f"  {'task':24} {'trigger':>8} {'descr':>8} {'load':>8}  miss"
    )
    for row in report.get("per_task", []):
        lines.append(
            f"  {row.get('task')!s:24} "
            f"{_fmt(row.get('trigger_hit_rate')):>8} "
            f"{_fmt(row.get('description_match_rate')):>8} "
            f"{_fmt(row.get('load_rate')):>8}  "
            f"{','.join(row.get('relevant_not_loaded') or []) or '-'}"
        )
    lines.append("")
    if report.get("false_positives"):
        lines.append(f"FALSE POSITIVES (distractors that ranked): {report['false_positives']}")
    else:
        lines.append("FALSE POSITIVES: none")
    if report.get("always_on_tier_present"):
        lines.append("ALWAYS-ON TIER: present (`always:` frontmatter)")
    else:
        lines.append("ALWAYS-ON TIER: ABSENT — recorded, not claimed")
    lines.append("")
    verdict = report.get("verdict", {})
    lines.append(f"VERDICT: {verdict.get('verdict')}")
    if verdict.get("why"):
        lines.append(f"  {verdict['why']}")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    """Render a possibly-``None`` rate. ``None`` prints as ``n/a``."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return "n/a"


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the skill-discoverability report and exit 0 on PASS, 2 otherwise."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = measure()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("verdict", {}).get("verdict") == "PASS" else 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(_main())
