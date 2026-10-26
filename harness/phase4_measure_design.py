"""Phase 4's four measurement designs, published as DATA.

The number this round is measured against is AHEAD's: **middleware at +2.2pp**,
system-prompt prose at **-2.3pp**. So Phase 4's architecture claims have to be
measured the way middleware was, and *"the mechanism is in the tree"* is not a
measurement.

**Why designs rather than results.** T5 runs these; this round builds and pins
the mechanisms. A design published as prose is a design nobody can execute, and
an eval arm whose metric is undefined is an arm that will quietly measure
something else. So every entry below carries:

``metric``
    one named number, with its unit and its denominator;
``arms``
    the arm that has the mechanism and the arm that does not;
``control``
    what makes a difference attributable. A design with no control measures the
    harness, not the mechanism.
``replicate``
    how many repetitions. One repetition of a success rate is a coin flip and
    this repository has already paid for that class of claim;
``falsified_by``
    the observation that would refute the claim. A design with no falsifier is
    a design that cannot fail, and a measurement that cannot fail is a
    tautology;
``null_means``
    **what a null result would mean**, which is the field this file exists for.
    The brief asks for honest negatives and this repository has precedent for
    them. A design that cannot say what a null means will have its null
    explained away after the fact.

``DESIGNS`` is keyed by name and :func:`design` raises ``KeyError`` on an
unknown name rather than returning ``{}`` -- for the same reason
``harness/prompt_budget_measure.py::eval_arm_recipe`` raises: a driver handed an
empty config would run the BASELINE and publish it as the arm, and an arm that
silently does nothing is indistinguishable from an arm that works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DESIGNS",
    "DESIGN_NAMES",
    "Phase4Design",
    "design",
    "design_table",
    "render_design",
    "unknown_design_names",
]


@dataclass(frozen=True)
class Phase4Design:
    """One measurement, fully specified enough for somebody else to run it."""

    name: str
    claim: str
    metric: str
    unit: str
    denominator: str
    arms: Tuple[str, ...]
    control: str
    replicate: int
    falsified_by: str
    null_means: str
    implemented_by: Tuple[str, ...] = ()
    measured_here: bool = False
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this design."""
        return {
            "name": self.name,
            "claim": self.claim,
            "metric": self.metric,
            "unit": self.unit,
            "denominator": self.denominator,
            "arms": list(self.arms),
            "control": self.control,
            "replicate": int(self.replicate),
            "falsified_by": self.falsified_by,
            "null_means": self.null_means,
            "implemented_by": list(self.implemented_by),
            "measured_here": bool(self.measured_here),
            "notes": self.notes,
        }


DESIGNS: Dict[str, Phase4Design] = {}


def _register(design_obj: Phase4Design) -> Phase4Design:
    DESIGNS[design_obj.name] = design_obj
    return design_obj


_register(
    Phase4Design(
        name="initializer_ledger",
        claim=(
            "An initializer agent plus a JSON feature ledger improves multi-session "
            "completion over a high-level prompt alone."
        ),
        metric="features_passing_verified / features_total at the end of the effort",
        unit="fraction of features",
        denominator="every feature in the ledger at the end of the effort",
        arms=(
            "with_ledger: harness.long_horizon.initialize_project writes init.sh, "
            "PROGRESS.md, features.json and the initial commit, and every session "
            "reads render_session_brief and ends with LongHorizonSession.end",
            "without_ledger: the same task set, the same turn budget, the same "
            "verifier, NO initializer artefacts -- the model's own plan is the only "
            "statement of what it is building",
        ),
        control=(
            "IDENTICAL task set, turn cap, verifier and scripted model in both arms. "
            "The single variable is whether the initializer artefacts exist. Diff the "
            "two config dicts and assert they differ in the initializer keys and "
            "nothing else -- two variables measures neither."
        ),
        replicate=3,
        falsified_by=(
            "the without_ledger arm passes the same fraction of features, or passes "
            "more. Anthropic's documented failure is a frontier model falling short "
            "on a high-level prompt, so a null here would mean the corpus's tasks are "
            "narrower than 'a production-quality web app' -- which is a statement "
            "about the corpus and must be reported as one, not as a refutation."
        ),
        null_means=(
            "the ledger adds ceremony without changing the outcome ON THIS TASK SET. "
            "The honest reading is that the discipline helps when the effort spans "
            "sessions and does nothing when it does not -- so the claim to measure is "
            "'does the effort span sessions', and the finding is that the corpus does "
            "not exercise it."
        ),
        implemented_by=(
            "harness.long_horizon.initialize_project",
            "harness.long_horizon.render_session_brief",
            "harness.long_horizon.LongHorizonSession",
        ),
        notes=(
            "NOT MEASURED in this round. It needs a task set whose effort genuinely "
            "spans 3+ sessions; evals/tasks.py is a test-fixing corpus whose tasks "
            "each finish inside one turn budget, so running this design on it would "
            "produce a null for the wrong reason. That is the single most important "
            "gap in Phase 4's evidence and it is a CORPUS gap, not a mechanism gap."
        ),
    )
)


_register(
    Phase4Design(
        name="read_before_edit",
        claim="Read-before-edit enforcement reduces bad edits.",
        metric="edits refused by the gate / mutations attempted",
        unit="fraction of mutations",
        denominator="every mutating tool call the model attempted",
        arms=(
            "gate_on: config {read_before_edit: True, read_before_edit_same_turn: True}",
            "gate_off: the same run with both keys absent",
        ),
        control=(
            "IDENTICAL task set, model script, turn cap and verifier; the two config "
            "dicts differ in the two gate keys and nothing else. Plus the honest "
            "counterweight arm: require_edit_digest=True ALONE, because R2-06 "
            "measured it at 41/52 daily-driver arms and any claim about the gate has "
            "to be separable from that."
        ),
        replicate=3,
        falsified_by=(
            "the gate_on arm makes the SAME number of bad edits, or the edit-error "
            "rate is unchanged. Also falsified by gate_on refusing so much that "
            "verified completions fall -- an enforcement that costs correctness has "
            "not reduced bad edits, it has moved them."
        ),
        null_means=(
            "on a scripted model that reads before it edits, the gate has nothing to "
            "catch, and the rate is 0 in BOTH arms. That is a null about the FIXTURE, "
            "not about a real model. A null here is a demand for a fixture that "
            "demonstrably over-writes, not a reason to ship the gate silently."
        ),
        implemented_by=(
            "harness.agent_kernel.read_before_edit.TurnReadLedger",
            "harness.agent_kernel.read_before_edit.check_read_before_edit",
            "harness.agent_kernel.tools.ToolRegistry._check_read_before_edit",
        ),
        notes=(
            "DEFAULT OFF, and that is measured rather than cautious: the mechanism is "
            "complete, wired into ToolRegistry.execute's guard chain ahead of the "
            "digest check, and pinned. What is NOT measured is the blast radius of "
            "turning it on for real arms. R2-06's measurement of the weaker digest "
            "requirement (41/52) is the reason the default is not flipped blind."
        ),
    )
)


_register(
    Phase4Design(
        name="subagent_isolation",
        claim="Subagent isolation reduces the parent's context.",
        metric="parent_tokens_per_task",
        unit="tokens of parent request per task",
        denominator="the sum over every model request the PARENT issued",
        arms=(
            "isolated: exploration runs in a subagent with its own message list and "
            "returns a compressed summary; harness.agent_kernel.isolation.IsolationReceipt",
            "in_room: the same exploration runs in the parent's own window",
        ),
        control=(
            "IDENTICAL task set, model, turn cap and verifier. The control is the "
            "in_room arm, which is the only honest baseline: it is the same work in "
            "the same window the mechanism is supposed to avoid. Plus a non-vacuity "
            "requirement -- the receipt must show transcript_chars > returned_chars, "
            "or the isolation never happened and both arms are the same run."
        ),
        replicate=5,
        falsified_by=(
            "parent_tokens_per_task is not lower in the isolated arm, OR the "
            "isolation receipt reports a compression ratio at or above the "
            "MIN_COMPRESSION_RATIO floor. A receipt that reports the exploration and "
            "the return as the same size is a refutation regardless of the token "
            "count, because it means the trace came back."
        ),
        null_means=(
            "the parent's window was never the binding constraint on this corpus, so "
            "moving bytes out of it changed nothing. The honest reading is that "
            "isolation buys ROOM, not speed -- and 'room' only pays when the run "
            "would otherwise have compacted, so the null predicts the parent arms' "
            "compaction counts were already zero. THAT is the thing to check next, "
            "and a design that does not check it cannot tell 'no room was needed' "
            "from 'isolation did not work'."
        ),
        implemented_by=(
            "harness.agent_kernel.isolation.isolation_receipt",
            "harness.agent_kernel.isolation.assert_isolated",
            "harness.agent_kernel.isolation.isolation_report",
        ),
        measured_here=True,
        notes=(
            "The MECHANISM's measurement is in-tree and pinned (the compression ratio, "
            "the worst ratio alongside the mean, and the receipt-shape check against "
            "PlanResearchResult). The END-TO-END token number is NOT measured and "
            "needs a task set whose parent window actually fills."
        ),
    )
)


_register(
    Phase4Design(
        name="one_feature_per_session",
        claim="One-feature-per-session prevents one-shotting.",
        metric="rework_rate",
        unit="fraction of features needing more than one session",
        denominator="every feature in the ledger at the end of the effort",
        arms=(
            "one_feature: LongHorizonSession.claim() refuses a second feature, so a "
            "session's blast radius is one feature",
            "free_form: the same task set with no scope claim, so one session may "
            "touch every failing feature",
        ),
        control=(
            "IDENTICAL task set, session budget and verifier. The per-session budget "
            "is held EQUAL in turns, not in features -- otherwise 'one feature' is "
            "just 'fewer features' and the design measures nothing. Plus a "
            "within-arm instrumentation requirement: the refused second claim must "
            "appear in LongHorizonSession.attempts(), or the arm never bound."
        ),
        replicate=3,
        falsified_by=(
            "the free_form arm's rework rate is the same or lower, OR the one_feature "
            "arm's completion rate falls without its rework rate falling. Splitting "
            "work that succeeds when done together is not a win."
        ),
        null_means=(
            "on a corpus where every feature fits in one session there is no "
            "one-shotting to prevent, and both arms are the same run. The null "
            "therefore measures the CORPUS, not the rule -- and a design that reports "
            "it as 'the rule does not work' has mis-attributed its own fixture."
        ),
        implemented_by=(
            "harness.long_horizon.LongHorizonSession.claim",
        ),
        notes=(
            "The refusal is ENFORCED and pinnable: attempts() reports the refused "
            "claim with its outcome, so an arm that bound and an arm that did not "
            "are distinguishable from the receipt alone."
        ),
    )
)

#: The four names, in the order the brief lists them.
DESIGN_NAMES: Tuple[str, ...] = (
    "initializer_ledger",
    "read_before_edit",
    "subagent_isolation",
    "one_feature_per_session",
)


def design(name: str) -> Phase4Design:
    """Return one design by name. Raises ``KeyError`` on an unknown name.

    Raising rather than returning a default is the whole point: a driver handed
    an empty config would run whatever its default behaviour is and publish the
    result as this design's measurement.
    """
    key = str(name or "").strip()
    if key not in DESIGNS:
        raise KeyError(
            f"unknown Phase-4 design {name!r}; known designs are {sorted(DESIGNS)}"
        )
    return DESIGNS[key]


def unknown_design_names() -> Tuple[str, ...]:
    """Return any design in :data:`DESIGN_NAMES` that is not in :data:`DESIGNS`.

    The list and the table can drift, and a name in the list with no design is a
    measurement T5 believes exists. This is the same drift detector
    ``prompt_budget_measure.unknown_recipe_keys`` exists for, and it is why it
    exists.
    """
    return tuple(name for name in DESIGN_NAMES if name not in DESIGNS)


def design_table() -> Dict[str, Any]:
    """Return every design as a JSON-compatible projection."""
    return {name: item.to_dict() for name, item in sorted(DESIGNS.items())}


def render_design(name: str = "") -> str:
    """Render one design (or the whole table) as plain lines."""
    items = [design(name)] if name else [DESIGNS[key] for key in DESIGN_NAMES]
    lines: List[str] = []
    for item in items:
        lines.append(f"=== {item.name} ===")
        lines.append(f"claim:    {item.claim}")
        lines.append(f"metric:   {item.metric} ({item.unit})")
        lines.append(f"denom:    {item.denominator}")
        lines.append("arms:")
        for arm in item.arms:
            lines.append(f"  - {arm}")
        lines.append(f"control:  {item.control}")
        lines.append(f"replicate:{item.replicate}")
        lines.append(f"falsified:{item.falsified_by}")
        lines.append(f"null:     {item.null_means}")
        lines.append(f"measured_here: {item.measured_here}")
        if item.notes:
            lines.append(f"notes:    {item.notes}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: Optional[List[str]] = None) -> int:
    """Print the design table. Exit 2 if the list and the table disagree."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m harness.phase4_measure_design")
    parser.add_argument("--design", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.json:
        import json

        print(json.dumps(design_table(), indent=2, sort_keys=True))
    else:
        print(render_design(args.design))
    unknown = unknown_design_names()
    if unknown:
        print(f"UNKNOWN DESIGNS (named but not defined): {', '.join(unknown)}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's CLI
    raise SystemExit(main())
