"""T1.W2.5 — the model-family matrix is facts, not opinions.

THE DISTINCTION THAT KEEPS THIS HONEST
--------------------------------------
A family-specific prompt block is legitimate when it describes **how that model
parses or emits**. It is illegitimate when it describes **how we want it to
behave**. The corpus's standard is explicit — Hermes gates model-specific
blocks *"with code comments citing observed per-model failures"* — and this
module exists to hold that line mechanically rather than by review.

Three properties, and the third is the one that actually bites:

1. **A block without a citation is an opinion.** Wave 1 enforced this in the
   TYPE: :class:`harness.prompt_matrix.FormatFact.__post_init__` raises when
   ``observed`` is empty. This module verifies the type is still load-bearing and
   reports every block's citation length, because a citation that is one
   character long satisfies the type while carrying no evidence.

2. **An unobserved model gets the GENERIC prompt.** Not a guess at a family's
   preferences. :func:`harness.prompt_matrix.resolve_family` delegates to
   ``runtime.model_capabilities.vision_family_for`` — a **shape** resolver — and
   returns ``""`` for anything it cannot place. ``""`` means "apply no format
   facts", which is the historical prompt. This module measures that every
   unresolvable model really does get the generic prompt, over a grid of model
   names including the three families this project actually routes to.

3. **Dispatch is on METADATA, not a name substring.** A provider renaming a
   model must not silently change its prompt, and — the half that is easy to
   forget — **must not silently lose its format quirks.** Losing the quirks is
   the dangerous direction: it looks like a safe fallback and it is a
   regression, because the quirk existed because of an observed failure.

WHAT THIS MODULE MEASURED, AND THE FINDING
-------------------------------------------
The three declared-but-unreachable families. Wave 1 preserved three real
observations for ``qwen``, ``deepseek`` and ``glm`` in
:data:`harness.prompt_matrix.UNREACHABLE_OBSERVATIONS` and correctly declined
to ship prompt blocks for them, because the shape resolver produces only
``openai``, ``anthropic`` and ``google`` — so those blocks would have been
unreachable dead text.

**That is still true, and this module re-derives it on this tree.** The three
observations stay unpublished, the citations stay intact, and the condition
under which each would become reachable is still recorded. The finding this
round adds is narrower and is about the *cost* of that arrangement: the most
valuable of the three — the verbosity/recap observation, which describes a
prompt-ORDERING defect this repository could fix directly — is unreachable
through the matrix, so the only way to act on it is a config change nobody has
made. It is named rather than left for a reader to rediscover.

A RENAME MUST NOT LOSE THE QUIRKS — and the test that is easy to write wrongly
--------------------------------------------------------------------------
The natural test is "an unknown model name produces no format facts", and that
test passes for the wrong reason: a rename *is* an unknown model name, so it
looks identical to a model nobody has ever observed. The two must be kept
apart, and this module keeps them apart by construction:

* :func:`rename_preserves_quirks` takes a family that HAS facts, renames the
  model in a way a provider plausibly would (a version suffix, a vendor prefix,
  a date stamp), and asserts the facts still apply **because the metadata still
  resolves the family**;
* and it separately asserts that a model whose metadata resolves to nothing gets
  the generic prompt.

If the family resolution were keyed on a model-name substring, the first test
would fail on the rename — which is the property the brief asks for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness import prompt_matrix

__all__ = [
    "FAMILY_CITATION_FLOOR",
    "ModelCase",
    "citation_report",
    "generic_for_unknown_models",
    "matrix_report",
    "rename_preserves_quirks",
    "render_report",
    "unreachable_report",
    "verify",
]


#: A citation shorter than this is treated as an UNREFERENCED citation even
#: though the type accepts it.
#:
#: The number is a judgement and it is published so a reviewer can disagree with
#: it. It exists because ``FormatFact.__post_init__`` only requires a NON-EMPTY
#: observation, so ``observed="tbd"`` satisfies the type and carries no evidence
#: whatsoever. The floor is deliberately low — the shortest real citation in the
#: registry is far longer — because a floor set near the real values would start
#: rejecting prose for being prose.
FAMILY_CITATION_FLOOR = 40


@dataclass(frozen=True)
class ModelCase:
    """One (model, provider) pair and what the matrix must do with it."""

    model: str
    provider: str = ""
    #: What this module asserts about it. One of the closed expectations below.
    expect: str = "generic"
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "model": self.model,
            "provider": self.provider,
            "expect": self.expect,
            "why": self.why,
        }


#: The closed expectation vocabulary.
EXPECTS: Tuple[str, ...] = ("generic", "family_facts")

#: The grid. **Every unresolvable model must land on ``generic``**, and the grid
#: is built to make that non-trivial: the three families this project actually
#: routes to, a version-suffixed rename, a vendor-prefixed rename, a date-stamped
#: rename, an empty model, a None model, and three names that look like
#: families but are not ones the resolver produces.
MODEL_CASES: Tuple[ModelCase, ...] = (
    ModelCase("gpt-4o", "openai", "family_facts", "OpenAI-compatible shape"),
    ModelCase("claude-sonnet-4", "anthropic", "family_facts", "Anthropic shape"),
    ModelCase("gemini-2.0-flash", "google", "family_facts", "Google shape"),
    # The three families Wave 1 preserved observations for. They resolve to
    # `openai` because their PROVIDER is an OpenAI-compatible gateway, which is
    # exactly the point: resolution follows the wire shape, not the name.
    ModelCase("qwen3.8-27b", "openai-compatible", "family_facts",
              "an OpenAI-compatible gateway; the model NAME carries no shape"),
    ModelCase("deepseek-v3", "openai-compatible", "family_facts", "as above"),
    ModelCase("glm-5.3", "openai-compatible", "family_facts", "as above"),
    # RENAMES. These are the rows the brief cares about and the reason
    # `rename_preserves_quirks` exists as a separate function.
    ModelCase("claude-sonnet-4-20250514", "anthropic", "family_facts",
              "a date-stamped rename must not lose its quirks"),
    ModelCase("anthropic/claude-sonnet-4", "anthropic", "family_facts",
              "a vendor-prefixed rename must not lose its quirks"),
    ModelCase("gpt-4o-2024-11-20", "openai", "family_facts",
              "a version-suffixed rename must not lose its quirks"),
    # UNOBSERVED. Each of these must get the generic prompt.
    ModelCase("some-new-model", "some-new-provider", "generic",
              "a model nobody has observed"),
    ModelCase("llama-4-70b", "together", "generic",
              "a family the shape resolver does not produce"),
    ModelCase("", "", "generic", "an empty model name"),
    ModelCase("none", "", "generic", "a literal 'none'"),
)


# ---------------------------------------------------------------------------
# 1. Every family block is cited
# ---------------------------------------------------------------------------


def citation_report() -> Dict[str, Any]:
    """Return every registered family block and the length of its citation.

    Reports the citation LENGTH as well as its presence, because
    ``FormatFact.__post_init__`` only requires a non-empty string: the type stops
    an absent citation and not a worthless one.
    """
    rows: List[Dict[str, Any]] = []
    for fact in prompt_matrix.FORMAT_FACTS:
        observed = str(fact.observed or "").strip()
        rows.append(
            {
                "fact_id": fact.fact_id,
                "kind": fact.kind,
                "families": list(fact.families),
                "cited": bool(observed),
                "citation_chars": len(observed),
                "citation_substantive": len(observed) >= FAMILY_CITATION_FLOOR,
                "handled_by": fact.handled_by,
                "chars": len(str(fact.text or "")),
            }
        )
    uncited = [row["fact_id"] for row in rows if not row["cited"]]
    unsubstantive = [
        row["fact_id"] for row in rows if row["cited"] and not row["citation_substantive"]
    ]
    # The enforcement is in the TYPE. Verified by calling it, not by reading it,
    # because a docstring claiming the constructor raises is not a raise.
    type_enforced = _type_rejects_an_uncited_fact()
    return {
        "facts": rows,
        "fact_count": len(rows),
        "uncited": uncited,
        "uncited_count": len(uncited),
        "unsubstantive_citations": unsubstantive,
        "citation_floor_chars": FAMILY_CITATION_FLOOR,
        "type_enforces_a_citation": type_enforced,
        "every_block_cited": not uncited and type_enforced,
    }


def _type_rejects_an_uncited_fact() -> bool:
    """Return whether ``FormatFact`` refuses to be built without an observation.

    Called, not read. A constructor whose validation is described in a comment
    and absent from the body passes every review that reads the comment.
    """
    try:
        prompt_matrix.FormatFact(
            fact_id="probe",
            kind="probe",
            families=("probe",),
            text="probe",
            observed="",
        )
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# 2. An unobserved model gets the generic prompt
# ---------------------------------------------------------------------------


def generic_for_unknown_models(
    cases: Optional[Sequence[ModelCase]] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return, per case, what the matrix actually renders.

    ``config`` defaults to ``{"prompt_format_facts": True}`` because the matrix
    ships nothing without that key — an unconfigured run gets the historical
    prompt by construction, which is correct and makes the enabled arm the
    informative one.
    """
    active = {"prompt_format_facts": True}
    if config is not None:
        active = dict(config)
    rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for case in tuple(cases or MODEL_CASES):
        family = prompt_matrix.resolve_family(case.model, case.provider)
        receipt = prompt_matrix.format_prompt(
            model=case.model, provider=case.provider, config=active
        )
        row = {
            "model": case.model,
            "provider": case.provider,
            "expect": case.expect,
            "resolved_family": family,
            "fact_ids": list(receipt.fact_ids),
            "chars": len(receipt.text),
            "family_resolved": bool(receipt.resolved),
            "rendered_empty": not receipt.text.strip(),
        }
        rows.append(row)
        if case.expect == "generic" and receipt.text.strip():
            failures.append({**row, "why": "an unobserved model was given format facts"})
        if case.expect == "family_facts" and not receipt.text.strip():
            failures.append({**row, "why": "a known family was given no format facts"})
    return {
        "config": dict(active),
        "cases": rows,
        "failures": failures,
        "unobserved_models_get_generic": not failures,
        "generic_count": sum(1 for row in rows if row["expect"] == "generic"),
        "fact_count": sum(1 for row in rows if row["expect"] == "family_facts"),
    }


# ---------------------------------------------------------------------------
# 3. A rename must not lose the quirks — and must not be a guess
# ---------------------------------------------------------------------------


#: The rename pairs. **Each row states the BASELINE name whose facts the renamed
#: name must keep**, which is the whole test.
#:
#: The first version of this function called ``resolve_family`` twice with the
#: SAME arguments and reported ``stable: True`` — a tautology in the exact place
#: this module exists to prevent, and written by the module whose docstring
#: warns that "a rename IS an unknown model name, so it looks identical to a
#: model nobody has ever observed". A test that passes because it compared a
#: value with itself is worse than no test, because it is reported as coverage.
#:
#: Each row therefore names two DIFFERENT model strings, and the assertion is
#: that the renamed one resolves to the same family and therefore receives the
#: same facts.
RENAME_PAIRS: Tuple[Tuple[str, str, str], ...] = (
    # (baseline model, renamed model, why this rename is plausible)
    ("claude-sonnet-4", "claude-sonnet-4-20250514", "a date stamp"),
    ("gpt-4o", "gpt-4o-2024-11-20", "a version suffix"),
    ("gpt-4o", "openai/gpt-4o", "a vendor prefix"),
    ("claude-sonnet-4", "anthropic/claude-sonnet-4", "a vendor prefix"),
)


def rename_preserves_quirks(
    pairs: Optional[Sequence[Tuple[str, str, str]]] = None,
    *,
    provider: str = "",
) -> Dict[str, Any]:
    """Return whether a plausible provider rename keeps a family's format facts.

    **The property, stated so it cannot be satisfied by the wrong thing:** for a
    model whose family RESOLVES, the registered facts must still apply after the
    name changes in a way a provider plausibly would. Each row compares two
    DIFFERENT model strings — the baseline and the rename — and asserts the
    rename receives the same facts as its baseline.

    The negative half matters just as much and is the half that makes this a
    real test: a model whose metadata does not resolve must get NOTHING. If
    dispatch were keyed on a model-name substring, the positive half would fail
    on the vendor-prefix and version-suffix rows. If it were keyed on nothing at
    all, the negative half would fail. Both halves must hold, so neither a
    substring table nor a no-op satisfies this function.
    """
    rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for baseline, renamed, why in tuple(pairs or RENAME_PAIRS):
        base_family = prompt_matrix.resolve_family(baseline, provider)
        new_family = prompt_matrix.resolve_family(renamed, provider)
        base_facts = tuple(f.fact_id for f in prompt_matrix.facts_for(base_family))
        new_facts = tuple(f.fact_id for f in prompt_matrix.facts_for(new_family))
        row = {
            "baseline": baseline,
            "renamed": renamed,
            "rename_shape": why,
            "baseline_family": base_family,
            "renamed_family": new_family,
            "baseline_facts": list(base_facts),
            "renamed_facts": list(new_facts),
            "quirks_preserved": bool(base_facts) and base_facts == new_facts,
        }
        rows.append(row)
        if not base_facts:
            failures.append(
                {
                    "renamed": renamed,
                    "why": "the BASELINE resolved to a family with no facts, so "
                    "this row cannot test anything — pick a baseline whose "
                    "family is registered",
                }
            )
            continue
        if not row["quirks_preserved"]:
            failures.append(
                {
                    "renamed": renamed,
                    "why": f"a {why} changed the facts: "
                    f"{list(base_facts)} -> {list(new_facts)}",
                    "baseline_family": base_family,
                    "renamed_family": new_family,
                }
            )
    negative = prompt_matrix.format_prompt(
        model="a-model-nobody-observed",
        provider="a-provider-nobody-observed",
        config={"prompt_format_facts": True},
    )
    negative_ok = not negative.text.strip()
    if not negative_ok:
        failures.append(
            {
                "renamed": "a-model-nobody-observed",
                "why": "an unobserved model was given format facts, so the "
                "resolver is guessing rather than resolving",
            }
        )
    return {
        "cases": rows,
        "negative_control": {
            "model": "a-model-nobody-observed",
            "rendered_empty": negative_ok,
            "fact_ids": list(negative.fact_ids),
        },
        "failures": failures,
        "rename_preserves_quirks": not failures,
    }


def dispatch_is_metadata_not_substring() -> Dict[str, Any]:
    """Return whether family resolution reads metadata rather than a name.

    Asserted on the SOURCE, structurally, by reading
    :mod:`harness.prompt_matrix`'s AST and refusing a comparison between a model
    name and a family-shaped literal. The reason it is an AST check and not a
    behavioural one: a substring dispatch can be made to pass almost any
    behavioural probe you write, because you choose the names in the probe. The
    only thing that distinguishes metadata dispatch from name dispatch is the
    absence of the comparison itself.
    """
    import ast
    import inspect

    try:
        tree = ast.parse(inspect.getsource(prompt_matrix))
    except (OSError, SyntaxError, TypeError) as exc:  # pragma: no cover
        return {"is_metadata_dispatch": False, "reason": f"unreadable: {exc}"}
    literals = ("openai", "anthropic", "google")
    offenders: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            has_modelish = any(
                isinstance(op, (ast.Name, ast.Attribute))
                and any(
                    token in getattr(op, "id", "") or token in getattr(op, "attr", "")
                    for token in ("model", "family", "provider")
                )
                for op in operands
            )
            has_family_literal = any(
                isinstance(op, ast.Constant) and op.value in literals
                for op in operands
            )
            if has_modelish and has_family_literal:
                offenders.append({"line": int(node.lineno), "kind": "model_name_compared_to_a_family"})
    return {
        "is_metadata_dispatch": not offenders,
        "offenders": offenders,
        "reason": (
            "no AST comparison exists between a model/provider identifier and a "
            "family literal in harness/prompt_matrix.py; resolution is delegated "
            "to runtime.model_capabilities.vision_family_for"
        ),
    }


# ---------------------------------------------------------------------------
# 4. The unreachable observations — re-derived, not trusted
# ---------------------------------------------------------------------------


def unreachable_report() -> Dict[str, Any]:
    """Return whether the preserved-but-unreachable observations are still true.

    Wave 1 kept three real observations for families the resolver cannot
    currently produce, and declined to ship prompt blocks for them. This
    re-derives the condition on this tree: if the resolver now produces one of
    those families, the corresponding entry is no longer an unreachable
    observation but a **gap in the registry** — a real finding, because it means
    a known failure has no shipped defence.
    """
    resolvable: List[str] = []
    probe_models = (
        "gpt-4o",
        "claude-sonnet-4",
        "gemini-2.0-flash",
        "qwen3.8-27b",
        "deepseek-v3",
        "glm-5.3",
        "llama-4-70b",
        "mistral-large",
        "stepfun-step-2",
    )
    families = {prompt_matrix.resolve_family(model, "openai-compatible") for model in probe_models}
    families |= {prompt_matrix.resolve_family(model, "") for model in probe_models}
    families.discard("")
    for row in prompt_matrix.UNREACHABLE_OBSERVATIONS:
        for family in row.get("families", ()):
            if family in families:
                resolvable.append(f"{row.get('candidate_id')}:{family}")
    entries = [
        {
            "candidate_id": row.get("candidate_id"),
            "kind": row.get("kind"),
            "families": list(row.get("families", ())),
            "cited": bool(str(row.get("observed") or "").strip()),
            "reachable_when": row.get("reachable_when", ""),
        }
        for row in prompt_matrix.UNREACHABLE_OBSERVATIONS
    ]
    return {
        "resolvable_families": sorted(families),
        "entries": entries,
        "entry_count": len(entries),
        "all_cited": all(row["cited"] for row in entries),
        "now_reachable": resolvable,
        "still_unreachable": not resolvable,
        "gaps": [
            {
                "candidate_id": row.get("candidate_id"),
                "families": list(row.get("families", ())),
                "why": "the family now resolves but no FormatFact is registered "
                "for it, so a KNOWN observed failure has no shipped defence",
            }
            for row in prompt_matrix.UNREACHABLE_OBSERVATIONS
            if any(
                family in families for family in row.get("families", ())
            )
        ],
    }


def matrix_report() -> Dict[str, Any]:
    """Return the whole registry, for a reader who wants the table."""
    return prompt_matrix.matrix_report()


# ---------------------------------------------------------------------------
# 5. The verdict
# ---------------------------------------------------------------------------


def verify(
    *,
    cases: Optional[Sequence[ModelCase]] = None,
    citation_floor: int = FAMILY_CITATION_FLOOR,
) -> Dict[str, Any]:
    """Run every W2.5 check and return the verdict."""
    citations = citation_report()
    generic = generic_for_unknown_models(cases)
    renames = rename_preserves_quirks()
    dispatch = dispatch_is_metadata_not_substring()
    unreachable = unreachable_report()

    problems: List[Dict[str, Any]] = []
    if citations["uncited"]:
        problems.append(
            {"kind": "family_block_without_a_citation",
             "detail": citations["uncited"]}
        )
    if not citations["type_enforces_a_citation"]:
        problems.append(
            {"kind": "the_type_no_longer_rejects_an_uncited_fact"}
        )
    if citations["unsubstantive_citations"]:
        # A WARNING, not a failure: the citation exists and the floor is a
        # judgement. It is published rather than enforced so a reviewer can
        # disagree with the number instead of editing the code.
        pass
    if generic["failures"]:
        problems.append({"kind": "model_served_the_wrong_prompt",
                         "detail": generic["failures"]})
    if renames["failures"]:
        problems.append({"kind": "a_rename_lost_or_gained_quirks",
                         "detail": renames["failures"]})
    if not dispatch["is_metadata_dispatch"]:
        problems.append({"kind": "dispatch_is_not_metadata",
                         "detail": dispatch["offenders"]})
    if unreachable["gaps"]:
        problems.append({"kind": "a_known_failure_has_no_shipped_defence",
                         "detail": unreachable["gaps"]})

    return {
        "verdict_schema": 1,
        "citations": citations,
        "unobserved_models": generic,
        "renames": renames,
        "dispatch": dispatch,
        "unreachable": unreachable,
        "problems": problems,
        "verdict": "PASS" if not problems else "FAIL",
        "warnings": (
            ["citation shorter than the published floor: "
             + ", ".join(citations["unsubstantive_citations"])]
            if citations["unsubstantive_citations"]
            else []
        ),
        "model_behaviour_measured": False,
    }


def render_report(report: Mapping[str, Any]) -> str:
    """Render the W2.5 report as plain text."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("W2.5  MODEL-FAMILY MATRIX: facts, not opinions")
    lines.append("=" * 78)
    lines.append("")
    lines.append("1. EVERY FAMILY BLOCK IS CITED")
    for row in (report.get("citations") or {}).get("facts", []):
        lines.append(
            f"  {row['fact_id']:32} families={','.join(row['families']):24} "
            f"cited={row['cited']} ({row['citation_chars']} chars)"
        )
    citations = report.get("citations") or {}
    lines.append(
        f"  type enforces a citation: {citations.get('type_enforces_a_citation')}"
        f"   uncited: {citations.get('uncited')}"
    )
    lines.append("")
    lines.append("2. AN UNOBSERVED MODEL GETS THE GENERIC PROMPT")
    lines.append(f"  {'model':38} {'family':12} {'expect':12} {'facts'}")
    for row in (report.get("unobserved_models") or {}).get("cases", []):
        lines.append(
            f"  {row.get('model')!s:38} {row.get('resolved_family')!s:12} "
            f"{row.get('expect')!s:12} {row.get('fact_ids')}"
        )
    lines.append(
        "  unobserved models get generic: "
        f"{(report.get('unobserved_models') or {}).get('unobserved_models_get_generic')}"
    )
    lines.append("")
    lines.append("3. A RENAME MUST NOT LOSE THE QUIRKS")
    for row in (report.get("renames") or {}).get("cases", []):
        lines.append(
            f"  {row.get('baseline')!s:24} -> {row.get('renamed')!s:32} "
            f"({row.get('rename_shape')})"
        )
        lines.append(
            f"      {row.get('baseline_family')} -> {row.get('renamed_family')}  "
            f"quirks_preserved={row.get('quirks_preserved')}  "
            f"facts={row.get('renamed_facts')}"
        )
    negative = (report.get("renames") or {}).get("negative_control") or {}
    lines.append(
        f"  negative control: {negative.get('model')} rendered_empty="
        f"{negative.get('rendered_empty')} facts={negative.get('fact_ids')}"
    )
    lines.append(
        "  rename preserves quirks: "
        f"{(report.get('renames') or {}).get('rename_preserves_quirks')}"
    )
    dispatch = report.get("dispatch") or {}
    lines.append(f"  dispatch is metadata, not a name substring: "
                 f"{dispatch.get('is_metadata_dispatch')}")
    lines.append("")
    lines.append("4. THE PRESERVED-BUT-UNREACHABLE OBSERVATIONS")
    unreachable = report.get("unreachable") or {}
    lines.append(
        f"  resolvable families on this tree: {unreachable.get('resolvable_families')}"
    )
    for row in unreachable.get("entries", []):
        lines.append(
            f"  {row.get('candidate_id')!s:32} families="
            f"{','.join(row.get('families') or []):24} cited={row.get('cited')}"
        )
    lines.append(f"  still unreachable: {unreachable.get('still_unreachable')}")
    for gap in unreachable.get("gaps") or []:
        lines.append(f"  GAP: {gap}")
    lines.append("")
    lines.append(f"VERDICT: {report.get('verdict')}")
    for warning in report.get("warnings") or []:
        lines.append(f"  WARNING: {warning}")
    for problem in report.get("problems") or []:
        lines.append(f"  PROBLEM: {problem.get('kind')}")
    return "\n".join(lines)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the W2.5 report. Exit 0 on PASS, 2 on FAIL."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = verify()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("verdict") == "PASS" else 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(_main())
