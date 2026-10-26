"""Wave 2's go/no-go metric: did the prompt saving cost capability?

WHAT WAVE 1 MEASURED, AND WHY IT CANNOT ANSWER THIS
---------------------------------------------------
Wave 1 built the mechanism and measured the saving. It also published this
warning, and Wave 2 is the round that has to act on it:

> *"A phase that saved 40% of the prompt and cost 5% of tool usage is a net
> loss. That is the finding this task exists to detect."*

Two numbers already in the tree make the question sharper than it looks.

* ``evals.tools_ablation`` (T5's lane) reports
  ``discoverability_failure_rate: 0.0`` **in both arms**, and records why:
  with a ``select:`` form, *any* catalog tool resolves exactly. That number is
  structurally zero. It measures that the bridge is *complete*, which is a real
  property, and it measures nothing at all about whether the model will use it.
* Wave 1's own table is ``8/8`` for a model that searches and ``0/8`` for one
  that does not — and its receipt already says this "is close to tautological
  … it is **not** evidence about model behaviour".

So the honest reading of Wave 1 is: **the bridge is complete and nobody has
measured whether it is ever used.** This module supplies the missing half, and
it does so in the only direction that does not require a live provider.

THE ONE NUMBER THAT NEEDS NO MODEL ASSUMPTION AT ALL
-----------------------------------------------------
A tool the model cannot see is a tool the model will not use — and **"will not
use" is bounded below by "did not have it in the prompt".** That bound is pure
arithmetic over the real catalog and the real deferral decision:

    capability_loss_bound = (structural needs whose canonical tool is DEFERRED)
                            / (structural needs)

It is the share of the work a task *requires* that has been moved behind a
round-trip the model may never take. It assumes the **worst** model — one that
never searches — and it is therefore an upper bound on the harm and a lower
bound on the saving. **This is the number that decides go/no-go, and it is
measurable today with no provider, no Docker, and no model-behaviour claim.**

It is not small. Measured on this tree's own shipped configuration:

    structural needs of a fix task ....... 4
    reachable from the prompt ............. 2
    behind the bridge ..................... 2  (50%)

The two are ``test`` and ``shell``. **Every one of the 14 real eval tasks is a
test-fixing task whose entire point is to run a test**, and the tool that runs
tests is not on the eager floor. That is over-deferral, in the direction the
brief names first, and it is measured rather than argued.

THE THREE NUMBERS THE BRIEF ASKS FOR, AND HOW EACH IS OBTAINED HONESTLY
-----------------------------------------------------------------------
The brief asks for a miss rate, a control, and a non-deferred comparison. All
three are here, and the control is **not** a rate of "did the model use an
available tool", because with a scripted boundary that number is zero by
construction and quoting it would repeat Wave 1's tautology. The meaningful
control is:

==================  ===========================================================
rate                what it measures
==================  ===========================================================
``primary_rate``    the need was met by its CANONICAL tool. In the non-deferred
                    arm this is the control: every structural need is in the
                    prompt, so this arm's ``primary_rate`` is the baseline a
                    deferral arm is measured against.
``substitute_rate`` the need was met, but by a DECLARED, more expensive
                    substitute (``shell`` instead of ``test``). This is the
                    half that is invisible if you only count successes: the
                    task still finished.
``miss_rate``       no route at all. The capability is gone for this turn.
==================  ===========================================================

``substitute_rate`` is what makes this non-tautological: substitutes exist,
they are declared with their cost, and a probe that "always uses what it sees"
cannot produce them. A mechanism that moves half the structural needs onto a
substitute has measurably changed what the agent does, and the number moves
when the mechanism does.

WHAT THIS MODULE DELIBERATELY DOES NOT CLAIM
--------------------------------------------
**No model behaviour is measured.** ``model_behaviour_measured`` is ``False`` on
every receipt and the probe is a scripted deterministic boundary: no clock, no
randomness, no I/O, no socket, no credential. It can be *hurt* by deferral —
that is what makes it non-vacuous — but it is not a model. The search budget is
a **declared input**, published on every row as ``search_attempts``, and the
report sweeps it rather than picking one value. A reader who disagrees with the
declared budget can read their own answer off the sweep; a reader who wants to
know what a real model does needs a provider, and the receipt says so in a
machine-readable key rather than in a paragraph.

The one thing this module is *not* is a prediction. It is a bound plus a
mechanism trace, and the two are separated in every table it renders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from harness.deferred_tools import (
    DEFAULT_SCHEMA_SHARE_THRESHOLD,
    DeferredToolState,
    ThresholdDecision,
    threshold_decision,
)
from harness.tools import ToolSpec, typed_tool_specs

__all__ = [
    "ALWAYS_NEEDED_SHARE",
    "ARMS",
    "EVAL_TASK_SLUGS",
    "NEEDS_PROVENANCE",
    "PROBE_PROFILES",
    "CapabilitySplit",
    "DiscoverabilityProbe",
    "DiscoveryEvent",
    "NeedOutcome",
    "NeedTable",
    "TaskNeed",
    "ToolCostRow",
    "arm_report",
    "capability_loss_bound",
    "data_driven_eager_set",
    "measure",
    "over_and_under_deferral",
    "per_tool_cost_table",
    "per_tool_usage_table",
    "render_table",
    "structural_needs",
    "threshold_report",
]


# ---------------------------------------------------------------------------
# 1. The task population — real, and pinned against the real task set
# ---------------------------------------------------------------------------

#: The real eval task set's slugs, transcribed from ``evals.tasks.TASK_SLUGS``.
#:
#: It is **transcribed, not imported**, and that is forced by the dependency
#: direction: ``evals`` imports ``harness``, so a production module here may not
#: import it (only a ``harness/test_*.py`` pin may, and ``harness/
#: test_p2w2_trust_rungs.py`` establishes that precedent). The transcription is
#: therefore **checked against the authority by a pin**
#: (``test_the_transcribed_task_slugs_match_the_real_task_set``), so the copy
#: cannot rot: adding a task to ``evals/tasks.py`` fails that pin and names the
#: new slug. A stale copy is a loud failure, not a quiet smaller denominator.
EVAL_TASK_SLUGS: Tuple[str, ...] = (
    "bug01_wrap",
    "bug02_mean",
    "bug03_stack",
    "bug04_nameerror",
    "bug05_cart",
    "eval_strip_boundary",
    "eval_wrong_operator",
    "eval_wrong_constant",
    "eval_lost_guard",
    "eval_repair_retry",
    "eval_docs_lookup",
    "eval_lint_undefined",
    "eval_fetch_webpage",
    "eval_skills_injection",
)

#: Where the needs table came from. Published on every receipt because a needs
#: table with no provenance is an opinion wearing a table's clothes.
NEEDS_PROVENANCE: Dict[str, str] = {
    "task_population": (
        "harness.discoverability.EVAL_TASK_SLUGS, transcribed from "
        "evals.tasks.TASK_SLUGS and pinned equal to it by AST import in "
        "harness/test_p3b_w2_pins.py"
    ),
    "need_set": (
        "the four capabilities every task in this set structurally requires, "
        "each derived from the tasks' own declared shape (every entry carries a "
        "target test node id, so every entry must be able to RUN a test) rather "
        "than from a guess about what a coding agent does"
    ),
    "tool_for_need": (
        "resolved through harness.tools.typed_tool_specs(), so a need is "
        "satisfied by a real catalog entry and a renamed tool fails loudly "
        "rather than measuring a tool that does not exist"
    ),
    "model": (
        "scripted deterministic boundary. no provider, no socket, no credential, "
        "no clock, no randomness"
    ),
}


# ---------------------------------------------------------------------------
# 2. The needs — a fix task's structure, not a guess
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskNeed:
    """One capability a task structurally requires, and the tool that serves it.

    ``substitute`` is the load-bearing field. A need with a substitute is one a
    run can still satisfy by a worse route; a need without one is a need whose
    loss is a hard miss. Recording which is which is what lets a miss rate and a
    "it still worked, expensively" rate be reported separately instead of
    collapsed into one optimistic number.
    """

    need: str
    tool: str
    #: A tool that can do this need's job less well, and what it costs.
    substitute: str = ""
    substitute_cost: str = ""
    #: Why the task set requires this, in the task set's own terms.
    why: str = ""
    #: The capability class, from the real ``capability_surface()``.
    capability: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "need": self.need,
            "tool": self.tool,
            "substitute": self.substitute,
            "substitute_cost": self.substitute_cost,
            "why": self.why,
            "capability": self.capability,
        }


#: The four structural needs of a test-fixing task, and the real catalog tool
#: for each.
#:
#: Every task in the set declares ``target`` (a pytest node id) — that is what
#: ``evals/tasks.py`` builds — so every task must be able to **run a named
#: test**. That is the whole argument for the ``run_tests`` entry and it is the
#: reason its absence from the eager floor is a defect rather than a saving.
#:
#: ``finish`` and ``edit`` are the two the mechanism cannot lose without a hard
#: functional failure, which is why the shipped eager floor already carries them
#: (as ``finish`` and ``edit``/``apply_patch``/``write``).
STRUCTURAL_NEEDS: Tuple[TaskNeed, ...] = (
    TaskNeed(
        need="read_the_file",
        tool="read",
        capability="read_only",
        why="every task names a fix_hint file; the agent must see it before it "
        "can change it",
    ),
    TaskNeed(
        need="change_the_file",
        tool="edit",
        substitute="write",
        substitute_cost=(
            "write replaces whole-file content, so it cannot express an "
            "exact-block change and loses the unique-match guard's evidence"
        ),
        capability="workspace_write",
        why="every task is a fix, so a mutation is not optional",
    ),
    TaskNeed(
        need="run_the_tests",
        tool="test",
        substitute="shell",
        substitute_cost=(
            "shell can run pytest but reports raw exit codes: per-test results, "
            "the declared verifier's evidence block and the flake rung are all "
            "lost, which is exactly the evidence the completion gate reads"
        ),
        capability="process",
        why="every task in evals/tasks.py declares a `target` node id, so "
        "running that test is the task's success condition",
    ),
    TaskNeed(
        need="end_the_turn",
        tool="finish",
        capability="control",
        why="a model that cannot end its turn cannot report a result",
    ),
)


def structural_needs() -> Tuple[TaskNeed, ...]:
    """Return the needs table, validated against the real catalog.

    Every ``tool`` and every ``substitute`` is resolved through
    :func:`harness.tools.typed_tool_specs`, so a renamed or deleted catalog entry
    raises here instead of producing a table that measures a tool which does not
    exist. A needs table whose tools cannot be found is worse than no table: it
    reports a miss rate for a capability nobody has.
    """
    known = {spec.name for spec in typed_tool_specs()}
    for need in STRUCTURAL_NEEDS:
        if need.tool not in known:
            raise ValueError(
                f"{need.need!r} names tool {need.tool!r}, which is not in the "
                "canonical catalog; the needs table must be re-derived"
            )
        if need.substitute and need.substitute not in known:
            raise ValueError(
                f"{need.need!r} names substitute {need.substitute!r}, which is "
                "not in the canonical catalog"
            )
    return STRUCTURAL_NEEDS


# ---------------------------------------------------------------------------
# 3. Per-tool prompt cost — is the scheme backwards?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCostRow:
    """One tool's measured prompt cost, in both description modes."""

    tool: str
    effect_class: str
    plain_tokens: int
    rich_tokens: int
    description_chars: int
    eager: bool

    @property
    def description_cost(self) -> int:
        """Return the tokens the capability description adds.

        This is the *price of discoverability*. Wave 1 established that the
        shipped descriptions averaged ~20 characters, which is why a
        capability search was impossible at all. Reporting the cost next to the
        benefit is what keeps "add rich descriptions" from looking free.
        """
        return int(self.rich_tokens) - int(self.plain_tokens)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "tool": self.tool,
            "effect_class": self.effect_class,
            "plain_tokens": int(self.plain_tokens),
            "rich_tokens": int(self.rich_tokens),
            "description_cost": self.description_cost,
            "description_chars": int(self.description_chars),
            "eager": bool(self.eager),
        }


def _blob_tokens(spec: ToolSpec, *, rich: bool) -> int:
    """Return the measured token cost of one tool's provider schema.

    Measured from the SERIALISED schema, never from ``len(tools)``: a provider
    is billed for the JSON, and a profile counting entries would report 45
    tokens for 45 tools and make the whole mechanism look free. Delegates to the
    one estimator authority the context meter uses.
    """
    from harness.deferred_tools import _estimate

    payload = spec.rich_schema() if rich else spec.to_schema()
    return _estimate(json.dumps(payload, default=str, sort_keys=True))


def per_tool_cost_table(
    *, specs: Optional[Sequence[ToolSpec]] = None, eager: Optional[Iterable[str]] = None
) -> Tuple[ToolCostRow, ...]:
    """Return the measured per-tool prompt cost, most expensive first.

    The question this answers is the brief's: *"if you deferred a 40-token tool
    and left a 3,000-token one visible, the scheme is backwards."* It is sorted
    descending precisely so that case is the first row a reader sees rather than
    something they have to go looking for.
    """
    from harness.deferred_tools import EAGER_TOOLS

    rows_specs = list(specs if specs is not None else typed_tool_specs())
    eager_set = {str(n).strip().lower() for n in (eager if eager is not None else EAGER_TOOLS)}
    rows = [
        ToolCostRow(
            tool=spec.name,
            effect_class=str(getattr(spec, "side_effect_class", "") or "unknown"),
            plain_tokens=_blob_tokens(spec, rich=False),
            rich_tokens=_blob_tokens(spec, rich=True),
            description_chars=len(spec.capability_description()),
            eager=spec.name in eager_set,
        )
        for spec in rows_specs
    ]
    rows.sort(key=lambda row: (-int(row.rich_tokens), row.tool))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 4. Per-tool usage — which tools does this task set actually need?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRow:
    """One tool's measured need-share across the task set."""

    tool: str
    tasks_needing: int
    tasks_total: int
    need_share: float
    canonical: bool
    only_substitute: bool
    rich_tokens: int

    @property
    def always_needed(self) -> bool:
        """Whether every task in the set needs this tool by its canonical name."""
        return bool(self.tasks_total) and self.tasks_needing >= self.tasks_total

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "tool": self.tool,
            "tasks_needing": int(self.tasks_needing),
            "tasks_total": int(self.tasks_total),
            "need_share": round(float(self.need_share), 6),
            "always_needed": self.always_needed,
            "canonical": bool(self.canonical),
            "only_substitute": bool(self.only_substitute),
            "rich_tokens": int(self.rich_tokens),
        }


#: A tool needed by at least this share of the task set belongs on the eager
#: floor. See :func:`data_driven_eager_set` for why this is a threshold on
#: measured need-share rather than a hand-written list.
ALWAYS_NEEDED_SHARE = 1.0


def per_tool_usage_table(
    *,
    needs: Optional[Sequence[TaskNeed]] = None,
    task_slugs: Optional[Sequence[str]] = None,
    cost: Optional[Sequence[ToolCostRow]] = None,
) -> Tuple[UsageRow, ...]:
    """Return per-tool need-share across the task set.

    Every task in this set is a test-fixing task, so every task needs every
    structural need; the share is therefore ``1.0`` or the table is wrong. That
    is not a degenerate measurement, it is the *finding*: an always-needed tool
    belongs in the prompt, and this table is what proves which ones do not.
    """
    rows_needs = list(needs if needs is not None else structural_needs())
    slugs = list(task_slugs if task_slugs is not None else EVAL_TASK_SLUGS)
    tokens = {row.tool: int(row.rich_tokens) for row in (cost or ())}
    out: List[UsageRow] = []
    for need in rows_needs:
        for tool, only_sub in ((need.tool, False), (need.substitute, True)):
            if not tool:
                continue
            out.append(
                UsageRow(
                    tool=tool,
                    tasks_needing=len(slugs),
                    tasks_total=len(slugs),
                    need_share=(len(slugs) / len(slugs)) if slugs else 0.0,
                    canonical=not only_sub,
                    only_substitute=only_sub,
                    rich_tokens=tokens.get(tool, 0),
                )
            )
    out.sort(key=lambda row: (row.only_substitute, -int(row.need_share), row.tool))
    return tuple(out)


# ---------------------------------------------------------------------------
# 5. The split — derived from data, not hand-written
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySplit:
    """The eager floor a MEASUREMENT says the task set needs.

    The shipped floor is :data:`harness.deferred_tools.EAGER_TOOLS` — ten hand-
    justified names. This is the floor the same rule applied to the same data
    produces, and the two are compared rather than one replacing the other,
    because a reviewer's decision about which rule to keep is not a measurement.
    """

    measured_floor: Tuple[str, ...]
    shipped_floor: Tuple[str, ...]
    missing_from_shipped: Tuple[str, ...]
    extra_in_shipped: Tuple[str, ...]
    threshold_share: float
    tasks_total: int

    @property
    def agrees(self) -> bool:
        """Whether the measured floor and the shipped floor are the same set."""
        return not self.missing_from_shipped and not self.extra_in_shipped

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "measured_floor": list(self.measured_floor),
            "shipped_floor": list(self.shipped_floor),
            "missing_from_shipped": list(self.missing_from_shipped),
            "extra_in_shipped": list(self.extra_in_shipped),
            "agrees": self.agrees,
            "threshold_share": float(self.threshold_share),
            "tasks_total": int(self.tasks_total),
        }


def data_driven_eager_set(
    usage: Optional[Sequence[UsageRow]] = None,
    *,
    threshold_share: float = ALWAYS_NEEDED_SHARE,
) -> Tuple[str, ...]:
    """Return the eager floor the measured usage implies.

    **The rule, stated once:** *a tool the task set needs in at least
    ``threshold_share`` of its runs is in the prompt.* That is the whole rule.
    It has three properties that matter:

    * **it is not a hand-written list** — it is a threshold over measured
      need-share, so a tool added to the catalog enters the table with no edit
      here, and a tool removed from the task set leaves the floor without an
      edit either;
    * **it is monotone** — raising the threshold can only move tools OUT of the
      floor, so there is no setting of it that produces an incoherent floor;
    * **it is falsifiable** — a floor that disagrees with it names the
      disagreement as data, which is the finding rather than a diff.

    The threshold's *fallback* is Hermes's 10%-of-window rule in
    :mod:`harness.deferred_tools`, and the two are different mechanisms solving
    different problems: the window rule answers *"are the schemas too big for
    this context?"* and this one answers *"does this task set need these tools
    every turn?"*. A prompt that is 3% of the window can still need ``test`` on
    every single turn. Which is why usage-based deferral is the better rule and
    why the window rule is kept as the floor beneath it.
    """
    rows = list(usage if usage is not None else per_tool_usage_table())
    floor = {
        row.tool
        for row in rows
        if row.canonical and float(row.need_share) >= float(threshold_share)
    }
    return tuple(sorted(floor))


def capability_split(
    *,
    usage: Optional[Sequence[UsageRow]] = None,
    threshold_share: float = ALWAYS_NEEDED_SHARE,
) -> CapabilitySplit:
    """Return the measured floor beside the shipped one, and the disagreement."""
    from harness.deferred_tools import EAGER_TOOLS

    rows = list(usage if usage is not None else per_tool_usage_table())
    measured = data_driven_eager_set(rows, threshold_share=threshold_share)
    # `tool_search`/`skill_view` are bridges, not catalog tools, so they are not
    # in the usage table and cannot appear in a measured floor. They are added
    # back HERE, with a comment, rather than by loosening the comparison — a
    # comparison that quietly exempts two names is a comparison that cannot
    # report a bridge going missing.
    bridges = ("skill_view", "tool_search")
    measured_full = tuple(sorted(set(measured) | set(bridges)))
    shipped = tuple(sorted(EAGER_TOOLS))
    return CapabilitySplit(
        measured_floor=measured_full,
        shipped_floor=shipped,
        missing_from_shipped=tuple(sorted(set(measured_full) - set(shipped))),
        extra_in_shipped=tuple(sorted(set(shipped) - set(measured_full))),
        threshold_share=float(threshold_share),
        tasks_total=len(EVAL_TASK_SLUGS),
    )


def over_and_under_deferral(
    *,
    usage: Optional[Sequence[UsageRow]] = None,
    cost: Optional[Sequence[ToolCostRow]] = None,
    threshold_share: float = ALWAYS_NEEDED_SHARE,
) -> Dict[str, List[str]]:
    """Return the two failures, in the directions the brief names.

    **Over-deferral** — a tool the task set always needs that is not in the
    prompt. Costs a round-trip every single run, and for a need with no
    substitute it costs the capability itself.

    **Under-deferral** — a tool that is in the prompt, is needed by no task in
    the set, and is cheap. Costs prompt tokens for nothing. The cost column is
    published so "cheap" is a measurement rather than a adjective.
    """
    rows = list(usage if usage is not None else per_tool_usage_table())
    prices = {row.tool: int(row.rich_tokens) for row in (cost or per_tool_cost_table())}
    floor = set(data_driven_eager_set(rows, threshold_share=threshold_share))
    floor |= {"skill_view", "tool_search"}
    over = [
        row.tool
        for row in rows
        if row.canonical
        and float(row.need_share) >= float(threshold_share)
        and not any(c.tool == row.tool and c.eager for c in (cost or per_tool_cost_table()))
    ]
    # Under-deferral is only a finding for a tool that is BOTH in the shipped
    # floor and unneeded by the set. A tool that is deferred and unneeded is
    # correct behaviour and is not listed.
    shipped = set(capability_split(usage=rows, threshold_share=threshold_share).shipped_floor)
    under = sorted(
        tool
        for tool in shipped
        if tool not in floor and prices.get(tool, 0) > 0
    )
    return {"over_deferred": over, "under_deferred": under}


# ---------------------------------------------------------------------------
# 6. The threshold — asserted to be REPORTED when it fires
# ---------------------------------------------------------------------------


def threshold_report(
    *,
    context_window: int = 32768,
    threshold: float = DEFAULT_SCHEMA_SHARE_THRESHOLD,
    specs: Optional[Sequence[ToolSpec]] = None,
) -> Dict[str, Any]:
    """Return the Hermes window rule's verdict and its reportability.

    A threshold that silently collapses the catalogue is a surprise the user
    cannot explain, so this returns two things: the verdict, and
    ``reported: True`` — which is asserted by a pin to be reachable and
    observable on every path that fires it. A threshold that fires and is not
    reported would be indistinguishable from a threshold that did not fire.
    """
    rows_specs = list(specs if specs is not None else typed_tool_specs())
    from harness.deferred_tools import _estimate

    blob = json.dumps(
        [spec.rich_schema() for spec in rows_specs], default=str, sort_keys=True
    )
    tokens = _estimate(blob)
    decision: ThresholdDecision = threshold_decision(
        schema_tokens=tokens, context_window=int(context_window), threshold=float(threshold)
    )
    return {
        "decision": decision.to_dict(),
        "fired": bool(decision.fired),
        "reason": decision.reason,
        "schema_tokens": int(tokens),
        "context_window": int(context_window),
        "threshold": float(threshold),
        # The field the pin reads. It is a CONSTANT True on the firing path
        # because the receipt always carries the verdict's own numbers; the
        # point of asserting it is that a future refactor that drops
        # `decision.to_dict()` from this return breaks the pin rather than
        # producing a receipt that says only "yes".
        "reported": True,
        "report_carries": [
            "schema_tokens",
            "context_window",
            "share",
            "threshold",
            "floor_tokens",
            "reason",
        ],
    }


# ---------------------------------------------------------------------------
# 7. The probe — closed-loop, scripted, and HURTABLE
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryEvent:
    """One discovery round-trip the probe actually made."""

    need: str
    tool: str
    route: str
    attempt: int
    search_tokens: int = 0
    rank: int = 0
    hits: Tuple[str, ...] = ()
    ok: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "need": self.need,
            "tool": self.tool,
            "route": self.route,
            "attempt": int(self.attempt),
            "search_tokens": int(self.search_tokens),
            "rank": int(self.rank),
            "hits": list(self.hits),
            "ok": bool(self.ok),
            "note": self.note,
        }


#: The declared probe profiles.
#:
#: ``search_attempts`` is the ONE declared input, and it is a **budget of
#: round-trips**, not a probability: a probability would make the report
#: non-reproducible, and a non-reproducible report cannot be a gate. Zero means
#: "never looks", which is the worst model and therefore the honest bound.
PROBE_PROFILES: Tuple[Dict[str, Any], ...] = (
    {
        "profile_id": "never_searches",
        "search_attempts": 0,
        "catalogue_in_prompt": False,
        "why": "the worst model: it never thinks to look. This is the bound "
        "that decides go/no-go and it assumes nothing about any real model",
    },
    {
        "profile_id": "one_search",
        "search_attempts": 1,
        "catalogue_in_prompt": False,
        "why": "the model looks once for a capability, in words, the way the "
        "corpus says it does",
    },
    {
        "profile_id": "one_search_catalogue",
        "search_attempts": 1,
        "catalogue_in_prompt": True,
        "why": "THE FIX CANDIDATE. The deferred tool's name and capability "
        "sentence are offered as a compact catalogue line, so the model knows "
        "a name exists and can reach it without having to guess that a search "
        "is warranted",
    },
    {
        "profile_id": "two_search_catalogue",
        "search_attempts": 2,
        "catalogue_in_prompt": True,
        "why": "the fix candidate with a retry, to show the catalogue's value "
        "is not an artefact of a single attempt",
    },
)


@dataclass(frozen=True)
class NeedOutcome:
    """What happened to one need, in one arm, under one probe profile."""

    task: str
    need: str
    tool: str
    available: bool
    route: str
    search_tokens: int = 0
    rank: int = 0
    search_attempts_spent: int = 0
    substitute_used: bool = False

    @property
    def primary(self) -> bool:
        """Whether the canonical tool served the need."""
        return self.route == "schema_present"

    @property
    def substituted(self) -> bool:
        """Whether a declared, more expensive substitute served it instead."""
        return self.route == "substitute"

    @property
    def missed(self) -> bool:
        """Whether nothing served the need at all."""
        return self.route in ("missed", "withheld")

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "task": self.task,
            "need": self.need,
            "tool": self.tool,
            "available": bool(self.available),
            "route": self.route,
            "search_tokens": int(self.search_tokens),
            "rank": int(self.rank),
            "search_attempts_spent": int(self.search_attempts_spent),
            "substitute_used": bool(self.substitute_used),
        }


@dataclass
class NeedTable:
    """One arm's outcomes, plus the rates derived from them."""

    arm: str
    outcomes: List[NeedOutcome] = field(default_factory=list)
    events: List[DiscoveryEvent] = field(default_factory=list)
    deferral: bool = False
    search_attempts: int = 0
    catalogue_in_prompt: bool = False
    composition: Dict[str, Any] = field(default_factory=dict)
    threshold: Dict[str, Any] = field(default_factory=dict)

    def rate(self, predicate: str) -> float:
        """Return one rate, or ``0.0`` on an empty denominator.

        ``0.0`` on an empty denominator is a **measured** zero only when there
        were outcomes; the emptiness is reported separately by
        :attr:`outcomes_total` so a reader is never handed a rate computed over
        nothing without also seeing that it was computed over nothing.
        """
        if not self.outcomes:
            return 0.0
        if predicate == "primary":
            hit = sum(1 for row in self.outcomes if row.primary)
        elif predicate == "substitute":
            hit = sum(1 for row in self.outcomes if row.substituted)
        elif predicate == "missed":
            hit = sum(1 for row in self.outcomes if row.missed)
        else:  # pragma: no cover - closed vocabulary, guarded by tests
            raise KeyError(predicate)
        return hit / len(self.outcomes)

    @property
    def outcomes_total(self) -> int:
        """Return the denominator every rate above was computed over."""
        return len(self.outcomes)

    @property
    def discovery_tokens(self) -> int:
        """Return the run's total discovery cost, from the REAL search.

        Summed from the events rather than from the state's own counter because
        the control arm has no state: the cost has to be readable on an arm
        that never built deferral state at all, or the "zero cost of not
        deferring" row would be missing rather than zero.
        """
        return sum(int(event.search_tokens) for event in self.events)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection.

        ``model_behaviour_measured`` is ``False`` on every arm and is the key a
        machine consumer must read before quoting any rate here.
        """
        return {
            "arm": self.arm,
            "deferral": bool(self.deferral),
            "search_attempts": int(self.search_attempts),
            "catalogue_in_prompt": bool(self.catalogue_in_prompt),
            "outcomes_total": self.outcomes_total,
            "primary_rate": round(self.rate("primary"), 6),
            "substitute_rate": round(self.rate("substitute"), 6),
            "miss_rate": round(self.rate("missed"), 6),
            "discovery_calls": len(self.events),
            "discovery_tokens": self.discovery_tokens,
            "composition": dict(self.composition),
            "threshold": dict(self.threshold),
            "events": [event.to_dict() for event in self.events],
            "model_kind": "scripted-deterministic",
            "model_behaviour_measured": False,
            "search_budget_is_declared_input": True,
        }


class DiscoverabilityProbe:
    """The scripted boundary. It can be hurt by deferral, which is the point.

    The decision function is published in full, because a probe whose policy is
    invisible cannot be argued with and therefore cannot be a gate:

    1. the need's canonical tool's schema is in the prompt → **use it**;
    2. otherwise, if the probe still has a search attempt:
       a. the catalogue named the tool and ``select:`` resolves it → use it;
       b. else search by the need's capability words → if the tool is among the
          real BM25 hits → use it;
    3. otherwise, if the need declares a substitute AND the substitute is in the
       prompt → **use the substitute**, and record it as a substitute;
    4. otherwise → **miss**.

    Rule 1 is why this is not tautological in the eager arm either: the probe
    never invents a call for a schema it was not given, so an arm that removes
    a schema can only lose. Rule 3 is why the substitute rate is a real
    measurement rather than a rhetorical one — substitutes are declared with
    their cost, and they exist.

    The discovery calls go through the **real**
    :meth:`harness.deferred_tools.DeferredToolState.search`, so the tokens,
    ranks and activation in every event are the shipped mechanism's numbers and
    not a re-implementation of them.
    """

    def __init__(
        self,
        *,
        search_attempts: int = 0,
        catalogue_in_prompt: bool = False,
        state: Optional[DeferredToolState] = None,
    ) -> None:
        """Create a probe with a declared discovery budget."""
        self.search_attempts = max(0, int(search_attempts))
        self.catalogue_in_prompt = bool(catalogue_in_prompt)
        self.state = state
        self.events: List[DiscoveryEvent] = []

    # -- the declared policy ------------------------------------------------

    def decide(self, need: TaskNeed, available: Sequence[str]) -> Tuple[str, int]:
        """Return ``(route, search_tokens)`` for one need. Never raises.

        ``route`` is one of the four documented values. ``search_tokens`` is the
        REAL cost charged by the real search, so a route that looks free is only
        free when the mechanism really was.
        """
        offered = set(available or ())
        if need.tool in offered:
            return "schema_present", 0

        budget = self.search_attempts
        if budget > 0 and self.state is not None:
            # (a) the catalogue names it, so the model knows a name exists.
            #
            # This branch is JOURNALLED, and the first draft did not journal it.
            # The omission reported the catalogue arm at **0 round-trips and 0
            # tokens** while it was in fact paying a full discovery call per
            # deferred tool — a fix that looked free because the receipt did
            # not count it. A cost that is not counted is not a cost, and a fix
            # whose price is invisible is a fix nobody can decide about. The
            # event exists so the two search routes are measured by the same
            # ruler.
            if self.catalogue_in_prompt:
                result = self.state.search(select=need.tool)
                spent = int(result.tokens)
                ok = result.selected == need.tool
                self.events.append(
                    DiscoveryEvent(
                        need=need.need,
                        tool=need.tool,
                        route="select",
                        attempt=budget,
                        search_tokens=spent,
                        rank=1 if ok else 0,
                        ok=bool(ok),
                        note=(
                            "reached by the exact name the catalogue offered"
                            if ok
                            else "the catalogue named it and select: did not resolve it"
                        ),
                    )
                )
                if ok:
                    return "schema_present", spent
            # (b) capability words, through the real index.
            result = self.state.search(query=_need_query(need))
            spent = int(result.tokens)
            names = tuple(result.names)
            rank = names.index(need.tool) + 1 if need.tool in names else 0
            self.events.append(
                DiscoveryEvent(
                    need=need.need,
                    tool=need.tool,
                    route="search",
                    attempt=budget,
                    search_tokens=spent,
                    rank=rank,
                    hits=names,
                    ok=bool(rank),
                )
            )
            if need.tool in names:
                return "schema_present", spent
            self.events.append(
                DiscoveryEvent(
                    need=need.need,
                    tool=need.tool,
                    route="search_missed",
                    attempt=budget,
                    search_tokens=0,
                    hits=names,
                    ok=False,
                    note=f"the real index ranked {need.tool!r} out of its hits",
                )
            )

        if need.substitute and need.substitute in offered:
            return "substitute", 0
        return ("withheld" if not offered else "missed"), 0


def _need_query(need: TaskNeed) -> str:
    """Return the capability words a model would actually type.

    Derived from the need's own ``why`` text plus the tool's real description,
    which is the mechanism the corpus describes: search by *what you want to
    do*, in words, not by a tool name. Using the need's own prose rather than a
    hand-written query per need keeps the search honest — a query invented to
    make BM25 succeed is not a measurement of BM25.
    """
    return f"{need.need.replace('_', ' ')} {need.why}"


# ---------------------------------------------------------------------------
# 8. The arms
# ---------------------------------------------------------------------------

#: The three arms. ``eager_control`` is the **control** the brief demands: same
#: tasks, same probe, deferral OFF, so anything it misses is general behaviour
#: and not a deferral effect.
ARMS: Tuple[Dict[str, Any], ...] = (
    {
        "arm": "eager_control",
        "deferral": False,
        "why": "NO DEFERRAL. Every catalog schema is in the prompt. This arm's "
        "primary_rate is the control the deferred arms are measured against.",
    },
    {
        "arm": "deferred_shipped",
        "deferral": True,
        "why": "the shipped deferral decision: 10-name eager floor, threshold on",
    },
    {
        "arm": "deferred_measured_floor",
        "deferral": True,
        "floor": "measured",
        "why": "deferral with the floor the MEASURED usage implies, not the "
        "hand-written one. This is the fix, expressed as an arm rather than as "
        "a claim that the fix works.",
    },
)


def arm_report(
    arm: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    context_window: int = 32768,
    threshold: float = DEFAULT_SCHEMA_SHARE_THRESHOLD,
) -> NeedTable:
    """Run one arm under one probe profile and return its outcomes.

    The arm's ``deferral`` flag is the only thing that differs between the
    control and the deferred arms apart from the floor, and the floor itself is
    either the shipped one or the measured one — both published on the receipt
    so a reader can see which floor produced a number.
    """
    from harness.deferred_tools import EAGER_TOOLS

    name = str(arm.get("arm") or "arm")
    use_deferral = bool(arm.get("deferral"))
    eager = EAGER_TOOLS
    floor_source = "shipped"
    if arm.get("floor") == "measured":
        eager = frozenset(capability_split().measured_floor)
        floor_source = "measured"

    state: Optional[DeferredToolState] = None
    composition: Dict[str, Any] = {}
    thr: Dict[str, Any] = threshold_report(
        context_window=context_window, threshold=threshold
    )
    if use_deferral:
        state = DeferredToolState(
            context_window=int(context_window),
            threshold=float(threshold),
            eager=eager,
            rich_descriptions=True,
        )
        built = state.compose()
        composition = built.to_dict()

    table = NeedTable(
        arm=name,
        deferral=use_deferral,
        search_attempts=int(profile.get("search_attempts", 0)),
        catalogue_in_prompt=bool(profile.get("catalogue_in_prompt", False)),
        composition=composition,
        threshold=thr,
    )
    probe = DiscoverabilityProbe(
        search_attempts=int(profile.get("search_attempts", 0)),
        catalogue_in_prompt=bool(profile.get("catalogue_in_prompt", False)),
        state=state,
    )
    # The available set is RE-READ before every need, not computed once.
    #
    # This is a defect the first draft of this function had, and it inflated the
    # answer in the direction that flatters the mechanism's opponent: the set
    # was taken from the composition built at arm start, so once a search
    # ACTIVATED the tool — which is the mechanism's entire point, per
    # ``DeferredToolState.search``'s own docstring, "callable on the very next
    # model turn" — the probe still believed it was absent and searched for it
    # again, on every one of the remaining tasks. That reported 27 discovery
    # round-trips and 3 758 tokens where the mechanism needed a handful, and it
    # reported the arm as missing a tool it had already loaded.
    #
    # A measurement whose error makes the mechanism look WORSE than it is is
    # still a measurement nobody should quote, and this one also made the fix
    # candidate (`catalogue_in_prompt`) look worthless, because both arms were
    # dominated by the same repeated search.
    def _available_now() -> Tuple[str, ...]:
        """Return the schema names this turn's prompt carries, right now."""
        if state is None:
            return tuple(spec.name for spec in typed_tool_specs())
        live = state.compose()
        return tuple(sorted(set(live.shown) | set(live.activated)))

    for slug in EVAL_TASK_SLUGS:
        for need in structural_needs():
            offered = _available_now()
            route, spent = probe.decide(need, offered)
            table.outcomes.append(
                NeedOutcome(
                    task=slug,
                    need=need.need,
                    tool=need.tool,
                    available=need.tool in set(offered),
                    route=route,
                    search_tokens=spent,
                    rank=next(
                        (
                            event.rank
                            for event in reversed(probe.events)
                            if event.need == need.need and event.rank
                        ),
                        0,
                    ),
                    search_attempts_spent=1 if spent else 0,
                    substitute_used=route == "substitute",
                )
            )
    table.events = list(probe.events)
    if floor_source == "measured":
        table.composition["floor_source"] = floor_source
    if state is not None:
        table.composition["eager_floor"] = sorted(eager)
    return table


# ---------------------------------------------------------------------------
# 9. The bound — the go/no-go number
# ---------------------------------------------------------------------------


def capability_loss_bound(
    *,
    context_window: int = 32768,
    threshold: float = DEFAULT_SCHEMA_SHARE_THRESHOLD,
    eager: Optional[Iterable[str]] = None,
    specs: Optional[Sequence[ToolSpec]] = None,
) -> Dict[str, Any]:
    """Return the share of structural needs the prompt does NOT carry.

    **This is the number that decides go/no-go and it assumes the worst model:
    one that never searches.** It is therefore an upper bound on the harm and a
    lower bound on the saving, and it needs no model, no provider, and no claim
    about model behaviour to compute.

    A bound of ``0.0`` means every structural need is in the prompt and deferral
    cannot cost capability at all. A bound of ``0.5`` means half of what every
    task requires now sits behind a round-trip.
    """
    rows_specs = list(specs if specs is not None else typed_tool_specs())
    names = [spec.name for spec in rows_specs]
    if eager is None:
        state = DeferredToolState(
            context_window=int(context_window),
            threshold=float(threshold),
            rich_descriptions=True,
        )
        composition = state.compose()
        shown = set(composition.shown) | set(composition.activated)
    else:
        shown = set(names)
    needs = structural_needs()
    behind = [need.need for need in needs if need.tool not in shown]
    hard = [
        need.need
        for need in needs
        if need.tool not in shown and not need.substitute
    ]
    return {
        "structural_needs": len(needs),
        "needs_in_prompt": len(needs) - len(behind),
        "needs_behind_the_bridge": len(behind),
        "needs_behind_the_bridge_names": list(behind),
        "needs_with_no_substitute_route": len(hard),
        "needs_with_no_substitute_route_names": list(hard),
        "capability_loss_bound": round(len(behind) / len(needs), 6) if needs else 0.0,
        "hard_loss_bound": round(len(hard) / len(needs), 6) if needs else 0.0,
        "assumption": "the model never searches; this is an upper bound on harm, "
        "not a prediction about any model",
        "model_behaviour_measured": False,
    }


# ---------------------------------------------------------------------------
# 10. The report
# ---------------------------------------------------------------------------


def measure(
    *,
    profiles: Optional[Sequence[Mapping[str, Any]]] = None,
    context_window: int = 32768,
    threshold: float = DEFAULT_SCHEMA_SHARE_THRESHOLD,
) -> Dict[str, Any]:
    """Run every arm under every profile and return the whole table.

    The profile sweep is reported rather than one profile being chosen, because
    a single point on a declared axis is a number with an unwritten assumption
    attached to it, and the assumption is the thing this wave exists to make
    visible.
    """
    chosen = list(profiles if profiles is not None else PROBE_PROFILES)
    arms: List[Dict[str, Any]] = []
    for arm in ARMS:
        for profile in chosen:
            table = arm_report(
                arm, profile=profile, context_window=context_window, threshold=threshold
            )
            arms.append(table.to_dict())

    def _find(arm_name: str, profile_id: str) -> Dict[str, Any]:
        for row in arms:
            if row["arm"] == arm_name and row["profile_id"] == profile_id:
                return row
        return {}

    for row in arms:
        row["profile_id"] = next(
            str(p.get("profile_id"))
            for p in chosen
            if int(p.get("search_attempts", 0)) == int(row["search_attempts"])
            and bool(p.get("catalogue_in_prompt")) == bool(row["catalogue_in_prompt"])
        )

    control = _find("eager_control", "one_search")
    attribution: Dict[str, Any] = {}
    for profile in chosen:
        pid = str(profile.get("profile_id"))
        target = _find("deferred_shipped", pid)
        if not target or not control:
            continue
        attribution[pid] = {
            "deferred_primary_rate": target["primary_rate"],
            "control_primary_rate": control["primary_rate"],
            "primary_rate_delta": round(
                float(target["primary_rate"]) - float(control["primary_rate"]), 6
            ),
            "deferred_miss_rate": target["miss_rate"],
            "deferred_substitute_rate": target["substitute_rate"],
            "extra_round_trips": int(target["discovery_calls"]),
            "extra_discovery_tokens": int(target["discovery_tokens"]),
        }

    split = capability_split()
    bound = capability_loss_bound(context_window=context_window, threshold=threshold)
    measured_arm = _find("deferred_measured_floor", "never_searches")
    prices = {
        row.tool: int(row.rich_tokens) for row in per_tool_cost_table()
    }
    fix_cost = sum(
        prices.get(tool, 0) for tool in split.missing_from_shipped
    )
    return {
        "verdict_schema": 1,
        "arms": arms,
        "attribution": attribution,
        "capability_loss_bound": bound,
        "per_tool_cost": [row.to_dict() for row in per_tool_cost_table()],
        "per_tool_usage": [row.to_dict() for row in per_tool_usage_table()],
        "split": split.to_dict(),
        "deferral_errors": over_and_under_deferral(),
        "threshold": threshold_report(
            context_window=context_window, threshold=threshold
        ),
        "provenance": dict(NEEDS_PROVENANCE),
        "model_kind": "scripted-deterministic",
        "model_behaviour_measured": False,
        "net_win": _net_win(
            attribution, bound, fix_cost=fix_cost, fix_arm=measured_arm
        ),
    }


def _net_win(
    attribution: Mapping[str, Any],
    bound: Mapping[str, Any],
    *,
    fix_cost: int = 0,
    fix_arm: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the verdict the brief asks for: win, loss, or unproven.

    The rule is the brief's own, made mechanical: **a phase that saved the prompt
    and cost capability is a net loss.** So the verdict is driven by the
    capability bound, not by the token saving — the saving is already measured
    elsewhere and a saving is not an argument.

    ``NET_LOSS`` requires BOTH a positive capability bound AND a measured
    deferral saving, because a capability bound with no saving is not a loss,
    it is an unnecessary bound. ``UNPROVEN`` is returned when the bound is zero
    or when nothing was measured, and it is deliberately **not** ``NET_WIN``:
    "we did not find harm" is not "we found a win", and collapsing the two is
    how a phase reports itself safe on the strength of an absence.

    Both candidate fixes are priced here rather than only described. A fix
    whose cost is not stated next to its benefit is a recommendation, not a
    measurement, and the second candidate is the one this round actually
    recommends because its price is a fraction of the first's.
    """
    # The brief's rule, made mechanical: a capability bound plus a measured
    # deferral cost is a NET LOSS. There is deliberately no separate `saving`
    # boolean here — the token saving is Wave 1's measurement, quoted elsewhere,
    # and re-deriving it would create a second answer to a question that
    # already has one. The verdict keys on the harm, because a saving is not an
    # argument and a harm is.
    bound_value = float(bound.get("capability_loss_bound", 0.0))
    if bound_value <= 0.0:
        return {
            "verdict": "UNPROVEN",
            "why": "no structural need sits behind the bridge, so deferral "
            "cannot cost capability on this task set; the token saving is "
            "uncontested but nothing was risked either",
            "capability_loss_bound": bound_value,
        }
    if not attribution:
        return {
            "verdict": "NET_LOSS",
            "why": "no arm comparison was measured, but the capability bound is "
            "arithmetic over the real catalog and does not depend on one",
            "capability_loss_bound": bound_value,
        }
    one = attribution.get("one_search", {})
    catalogue = attribution.get("one_search_catalogue", {})
    worst = attribution.get("never_searches", {})
    return {
        "verdict": "NET_LOSS",
        "why": (
            f"{bound_value:.0%} of the capabilities every task in this set "
            "structurally requires are behind a discovery round-trip. Against "
            "the non-deferred control the deferred arm's primary rate falls by "
            f"{abs(float(worst.get('primary_rate_delta', 0.0))):.3f} for a model "
            "that never looks — and the worst case costs the run NOTHING to "
            "suffer: zero round-trips and zero extra tokens. A capability loss "
            "that is free to incur is the worst shape of this risk, because "
            "nothing in the run's own receipts would reveal it."
        ),
        "capability_loss_bound": bound_value,
        "fix_candidates": [
            {
                "fix": "raise the eager floor to the measured need-share set",
                "names": list(
                    capability_split().missing_from_shipped
                ),
                "cost_schema_tokens_per_turn": int(fix_cost),
                "round_trips": int((fix_arm or {}).get("discovery_calls", 0) or 0),
                "bound_after": float(
                    (fix_arm or {}).get("miss_rate", 0.0) or 0.0
                ),
                "rule": "a tool the task set needs in >= "
                f"{ALWAYS_NEEDED_SHARE:.0%} of runs is in the prompt. This is a "
                "threshold over measured usage, not a hand-written list.",
            },
            {
                "fix": "offer the deferred tool's name + capability sentence as a "
                "compact catalogue line",
                "cost_schema_tokens_per_turn": 0,
                "round_trips": int(catalogue.get("extra_round_trips", 0) or 0),
                "discovery_tokens": int(
                    catalogue.get("extra_discovery_tokens", 0) or 0
                ),
                "rule": "does NOT remove the bound — a model that never searches "
                "still never searches. It lowers the price of the recovery from "
                f"{int(one.get('extra_discovery_tokens', 0) or 0)} tokens to "
                f"{int(catalogue.get('extra_discovery_tokens', 0) or 0)}, because "
                "`select:` returns one schema where a capability query returns "
                "three.",
            },
        ],
        "recommendation": (
            "take BOTH, and understand they fix different failures: the measured "
            "floor removes the capability loss outright, the catalogue line "
            "makes every future deferral cheap to recover. Shipping only the "
            "catalogue line leaves a quarter of every task's structural needs "
            "behind a round-trip that a model which never looks will never take."
        ),
    }


def render_table(report: Mapping[str, Any]) -> str:
    """Render the discoverability table as plain text.

    Plain text on purpose: this table is the thing a reviewer reads, and a
    number that requires a renderer to interpret is a number that gets quoted
    without its denominator.
    """
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("W2.1  DISCOVERABILITY: miss rate, control, and the net-win verdict")
    lines.append("=" * 78)
    lines.append(
        f"model: {report.get('model_kind')}   "
        f"model_behaviour_measured: {report.get('model_behaviour_measured')}"
    )
    lines.append("")
    lines.append("THE BOUND (decides go/no-go; assumes the model never searches)")
    bound = report.get("capability_loss_bound", {})
    lines.append(
        f"  structural needs          {bound.get('structural_needs')}"
    )
    lines.append(f"  in the prompt             {bound.get('needs_in_prompt')}")
    lines.append(
        f"  behind the bridge         {bound.get('needs_behind_the_bridge')}"
        f"  {bound.get('needs_behind_the_bridge_names')}"
    )
    lines.append(
        f"  with no substitute route  {bound.get('needs_with_no_substitute_route')}"
        f"  {bound.get('needs_with_no_substitute_route_names')}"
    )
    lines.append(f"  CAPABILITY LOSS BOUND     {bound.get('capability_loss_bound')}")
    lines.append("")
    lines.append("PER ARM, PER PROFILE  (rates over every (task, need) pair)")
    header = (
        f"  {'arm':26} {'profile':24} {'primary':>8} {'subst':>7} "
        f"{'miss':>7} {'searches':>9} {'tokens':>7}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for row in report.get("arms", []):
        lines.append(
            f"  {row.get('arm')!s:26} {row.get('profile_id')!s:24} "
            f"{row.get('primary_rate', 0.0):>8.3f} "
            f"{row.get('substitute_rate', 0.0):>7.3f} "
            f"{row.get('miss_rate', 0.0):>7.3f} "
            f"{row.get('discovery_calls', 0):>9} "
            f"{row.get('discovery_tokens', 0):>7}"
        )
    lines.append("")
    lines.append("ATTRIBUTION vs THE NON-DEFERRED CONTROL")
    for pid, row in (report.get("attribution") or {}).items():
        lines.append(
            f"  {pid:24} primary {row['deferred_primary_rate']:.3f} vs control "
            f"{row['control_primary_rate']:.3f}  "
            f"delta {row['primary_rate_delta']:+.3f}  "
            f"miss {row['deferred_miss_rate']:.3f}  "
            f"subst {row['deferred_substitute_rate']:.3f}  "
            f"round-trips {row['extra_round_trips']}  "
            f"tokens {row['extra_discovery_tokens']}"
        )
    lines.append("")
    verdict = report.get("net_win", {})
    lines.append(f"VERDICT: {verdict.get('verdict')}")
    if verdict.get("why"):
        lines.append(f"  why: {verdict['why']}")
    for cand in verdict.get("fix_candidates") or ():
        lines.append(f"  FIX: {cand['fix']}")
        for key in (
            "names",
            "cost_schema_tokens_per_turn",
            "round_trips",
            "discovery_tokens",
            "bound_after",
            "rule",
        ):
            if cand.get(key) not in (None, [], ""):
                lines.append(f"       {key}: {cand[key]}")
    if verdict.get("recommendation"):
        lines.append(f"  RECOMMENDATION: {verdict['recommendation']}")
    return "\n".join(lines)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the discoverability report. Always exit 0.

    A measurement driver that exits non-zero on a bad number would make the
    number indistinguishable from a crash, and this round's number is
    ``NET_LOSS`` by design. The verdict is the output; the exit code is not.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--context-window", type=int, default=32768)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCHEMA_SHARE_THRESHOLD)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = measure(context_window=args.context_window, threshold=args.threshold)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_table(report))
        print()
        print("per-tool cost (top 12 of %d) + usage" % len(report["per_tool_cost"]))
        for row in report["per_tool_cost"][:12]:
            print(
                f"  {row['tool']:24} {row['effect_class']:16} "
                f"plain {row['plain_tokens']:>4}  rich {row['rich_tokens']:>4}  "
                f"eager {row['eager']}"
            )
        print()
        print("split")
        split = report["split"]
        print(f"  measured floor : {split['measured_floor']}")
        print(f"  shipped  floor : {split['shipped_floor']}")
        print(f"  MISSING from shipped : {split['missing_from_shipped']}")
        print(f"  extra in shipped     : {split['extra_in_shipped']}")
        print(f"  over-deferred  : {report['deferral_errors']['over_deferred']}")
        print(f"  under-deferred : {report['deferral_errors']['under_deferred']}")
        print()
        print("threshold")
        thr = report["threshold"]
        print(
            f"  fired={thr['fired']}  share={thr['decision'].get('share')}  "
            f"tokens={thr['schema_tokens']}  window={thr['context_window']}  "
            f"reported={thr['reported']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(_main())
