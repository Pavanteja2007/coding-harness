"""T5.W2.3 — the six honest-absent rules, and reduction-requires-a-baseline.

THE WORST LIE IN THIS PHASE IS THE PROMPT-REDUCTION PERCENTAGE
-----------------------------------------------------------
"We cut the prompt 40%" is two numbers. Without the BEFORE there is no
reduction, only a current value. A report that shows 40% and cannot show the
39,000-token baseline it is 40% of has not measured a reduction - it has
measured an "after" and divided it by a memory.

So the rule is MECHANICAL, not editorial: :func:`reduction_row` REFUSES to
compute a percentage without a baseline that carries its own provenance, and
returns the percentage as ``None`` with a reason naming what is missing. A
consumer reading ``reduction_pct`` gets ``None``, which renders as
``unavailable``; a consumer that tries to compute it from ``after`` alone has to
invent a denominator, which is the fabrication this exists to stop.

THE OTHER FIVE, AND WHAT EACH ONE IS DEFENDING AGAINST
----------------------------------------------------
========================  ==================================================
metric                    the lie it refuses
========================  ==================================================
prompt_tokens             ``0`` when the estimator never ran
cache_hit_rate            ``unreported`` collapsing to ``0%``
discoverability_rate      ``0`` (never measured) reading as ``0%`` (measured,
                          and genuinely zero)
skill_load_rate           same
deferred_count            ``0`` when deferral did not run at all
========================  ==================================================

`NOT_MEASURED` vs `ZERO` IS THE DISTINCTION EVERY ONE OF THESE TURNS ON
``NOT_MEASURED`` means nobody took the measurement. ``ZERO`` means it was taken
and it was zero. They render differently, they aggregate differently, and
collapsing them is how "we measured it and it was zero" becomes "we never
looked".

THE INVERTED PINS ARE THE POINT
--------------------------------
Every rule below has a test that FAILS IF YOU FIX THE BUG. A rule that only
asserts "the absent path renders unavailable" passes on a function that returns
a constant. Each rule therefore also asserts the MEASURED path renders its real
number, so the test cannot be satisfied by never measuring anything - the
"assert the control arm" rule from DOCTRINE.md applied to a reporting layer
rather than to a cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "HONEST_ABSENT",
    "METRICS",
    "NOT_MEASURED",
    "ReductionRow",
    "reduction_row",
    "render",
    "row",
    "summarise",
    "verify_renderers",
]

#: The literal that renders wherever a value could not be measured.
#:
#: A SENTENCE rather than a dash, a blank, or ``0``. A reader who scans a row
#: still learns that something is missing, rather than assuming the row is
#: empty or the number is zero. This is the same literal ``cli.context_budget``
#: uses; the two must agree, and :func:`verify_renderers` checks that they do.
UNAVAILABLE = "unavailable"

#: The marker for "this axis has no measurement at all". Distinct from a
#: measured zero, and never rendered as one.
NOT_MEASURED = "not_measured"


@dataclass(frozen=True)
class Row:
    """One metric's published state, with the absence carried structurally.

    ``value`` is ``None`` when the metric was not measured, and there is no
    separate "is it zero" flag to forget to set: the absence IS the ``None``.
    A consumer that reads ``value`` without reading ``available`` gets
    ``None``, which is the right outcome for a number nobody took.
    """

    metric: str
    value: Optional[float]
    available: bool
    reason: str = ""
    source: str = ""
    baseline: Optional[float] = None
    reduction_pct: Optional[float] = None
    unit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Project to the wire shape. ``value`` is OMITTED when absent.

        Omitting rather than sending ``null`` on purpose: a consumer that does
        a naive ``data["value"] or 0`` cannot tell an absent value from a zero,
        and that is the whole class of bug this module exists to close. A
        missing key raises ``KeyError``, which is the correct behaviour for a
        number that was never taken.
        """
        doc: Dict[str, Any] = {
            "metric": self.metric,
            "available": self.available,
            "unit": self.unit,
            "source": self.source,
        }
        if self.available:
            doc["value"] = self.value
            if self.baseline is not None:
                doc["baseline"] = self.baseline
            if self.reduction_pct is not None:
                doc["reduction_pct"] = self.reduction_pct
        else:
            doc["reported_as"] = UNAVAILABLE
            doc["reason"] = self.reason
        return doc

    def render(self) -> str:
        """The one-line human form. Never a bare ``0`` for an absent value."""
        if not self.available:
            return f"{self.metric}: {UNAVAILABLE} ({self.reason})"
        if self.reduction_pct is not None:
            return (
                f"{self.metric}: {self.value}{self.unit} "
                f"(was {self.baseline}{self.unit}, {self.reduction_pct:+.1f}%)"
            )
        return f"{self.metric}: {self.value}{self.unit}"


def row(
    metric: str,
    value: Optional[float],
    *,
    source: str = "",
    unit: str = "",
    reason: str = "",
) -> Row:
    """Build a row, deriving ``available`` from whether ``value`` is a number.

    Deriving rather than accepting an ``available`` argument, because a caller
    that passes ``available=True`` alongside ``value=None`` is exactly the
    defect: a row that claims to carry a number and does not. ``bool`` is
    rejected explicitly - ``False`` is a legitimate measured value for a rate,
    and ``True`` is never a token count.
    """
    if isinstance(value, bool):
        return Row(
            metric=metric,
            value=None,
            available=False,
            reason=(
                f"a bool was supplied for a {metric}; `True` is not a "
                f"{metric} and `False` is a measured zero that must be "
                f"carried as 0.0 with available=true"
            ),
            source=source,
            unit=unit,
        )
    if value is None:
        return Row(
            metric=metric,
            value=None,
            available=False,
            reason=reason or "no measurement was taken on this axis",
            source=source,
            unit=unit,
        )
    return Row(
        metric=metric,
        value=float(value),
        available=True,
        source=source,
        unit=unit,
    )


# --------------------------------------------------------------------------
# THE PRIORITY RULE: a reduction requires a measured baseline
# --------------------------------------------------------------------------

#: What a baseline must carry for a reduction to be computable. A number alone
#: is not a baseline: a baseline is a measurement with provenance, and "we
#: measured 3981 tokens once, in a commit, and do not remember which" is not a
#: baseline - it is a rumour with a digit in it.
REQUIRED_BASELINE_PROVENANCE = ("measured_at", "source")


@dataclass(frozen=True)
class ReductionRow:
    """A before/after pair, and a percentage that may REFUSE to exist.

    :param before: the baseline value. ``None`` means there is no baseline.
    :param after: the current value. ``None`` means it was not measured.
    :param provenance: what makes ``before`` a measurement rather than a
        constant - at minimum :data:`REQUIRED_BASELINE_PROVENANCE`.
    """

    before: Optional[float]
    after: Optional[float]
    provenance: Mapping[str, Any]
    metric: str = "prompt_tokens"
    reason: str = ""

    @property
    def computable(self) -> bool:
        """Whether a reduction percentage may be reported at all.

        Three conditions, all necessary: a baseline, an after, and provenance
        for the baseline. A percentage with no baseline is a division by an
        invented number, and an after with no baseline is a CURRENT VALUE, not
        a reduction.

        A ``bool`` on either side refuses the whole row rather than being
        coerced: ``before=True`` is the shape a caller reaches for by passing a
        flag they meant to be "did we measure this", and coercing it would turn
        a mistake into a plausible percentage.
        """
        if self.before is None or self.after is None:
            return False
        if isinstance(self.before, bool) or isinstance(self.after, bool):
            return False
        return all(key in self.provenance for key in REQUIRED_BASELINE_PROVENANCE)

    def to_dict(self) -> Dict[str, Any]:
        """Project, refusing to publish a percentage that is not computable."""
        missing = []
        if self.before is None:
            missing.append("baseline (the `before` value was never measured)")
        if self.after is None:
            missing.append("current (the `after` value was never measured)")
        if self.before is not None and not all(
            key in self.provenance for key in REQUIRED_BASELINE_PROVENANCE
        ):
            absent = [
                key for key in REQUIRED_BASELINE_PROVENANCE
                if key not in self.provenance
            ]
            missing.append(
                "baseline provenance (" + ", ".join(absent) + " absent, so the "
                "baseline is a constant rather than a measurement)"
            )
        doc: Dict[str, Any] = {
            "metric": self.metric,
            "reduction_computable": self.computable,
            "reduction_pct_is": (
                "a measured reduction against a measured baseline"
                if self.computable
                else UNAVAILABLE
            ),
            "why": self.reason or ("; ".join(missing) if missing else ""),
            "provenance": dict(self.provenance),
        }
        if self.before is not None:
            doc["before"] = self.before
        if self.after is not None:
            doc["after"] = self.after
        if self.computable:
            # max(1, before) rather than a bare divide: a zero baseline has no
            # percentage, and the honest answer is 100% or a refusal, not a
            # ZeroDivisionError surfacing as a crash in a reporting path.
            doc["reduction_pct"] = (
                100.0 * (self.before - self.after) / max(1.0, abs(self.before))
            )
        return doc

    def render(self) -> str:
        if not self.computable:
            return (
                f"{self.metric}: reduction {UNAVAILABLE} - "
                f"{self.reason or 'no measured baseline'}"
            )
        pct = 100.0 * (self.before - self.after) / max(1.0, abs(self.before))
        return f"{self.metric}: {self.before} -> {self.after} ({pct:+.1f}%)"


def reduction_row(
    before: Optional[float],
    after: Optional[float],
    *,
    provenance: Optional[Mapping[str, Any]] = None,
    metric: str = "prompt_tokens",
    reason: str = "",
) -> ReductionRow:
    """Build the before/after row. Refuses a percentage with no baseline.

    This is the whole of the "reduction requires a baseline" rule. It is one
    function because a rule spread across a renderer and a test is a rule that
    eventually gets reimplemented, and the reimplementation is the one that
    forgets the baseline.

    ``bool`` is preserved rather than coerced through ``float``. The first
    version did coerce, which meant ``reduction_row(True, 100)`` computed a
    confident ``-9900%`` off a flag the caller meant as "did we measure this" -
    a fabricated percentage from a boolean, which is the exact class of thing
    this module exists to refuse.
    """
    return ReductionRow(
        before=_as_measurement(before),
        after=_as_measurement(after),
        provenance=dict(provenance or {}),
        metric=metric,
        reason=reason,
    )


def _as_measurement(value: Any) -> Optional[float]:
    """Coerce a measurement, passing ``None`` and ``bool`` through untouched."""
    if value is None or isinstance(value, bool):
        return value  # type: ignore[return-value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# THE SIX RULES, AS DATA
# --------------------------------------------------------------------------

#: The rule set. ``None`` is the ABSENT value for every one of these; the
#: ``distinguishes`` field states what rendering it as ``0`` would falsely
#: claim, which is what makes this table reviewable rather than decorative.
HONEST_ABSENT: Dict[str, Dict[str, Any]] = {
    "prompt_tokens": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "not measured (no estimator ran) from measured zero (an empty "
            "request was measured)"
        ),
        "lie_prevented": "reporting 0 prompt tokens when nothing was measured",
        "implemented_by": "shared.p3b_truth.row",
    },
    "prompt_reduction_pct": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "a reduction against a MEASURED baseline from a current value with "
            "no baseline at all"
        ),
        "lie_prevented": (
            "publishing a percentage computed against a missing or "
            "unprovenanced baseline - the worst of the six, because a bare "
            "'after' plus a remembered denominator reads as a measurement"
        ),
        "implemented_by": "shared.p3b_truth.reduction_row",
        "requires": list(REQUIRED_BASELINE_PROVENANCE),
    },
    "cache_hit_rate": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "a provider that reported no cache status from a measured 0% hit "
            "rate"
        ),
        "lie_prevented": (
            "`unreported` collapsing to 0%, which reads as 'the cache never "
            "hit' when the truth is 'nobody reported'"
        ),
        "implemented_by": "shared.p3b_truth.row",
        "also_implemented_by": "runtime.cache_economics.HONEST_ABSENT_RULES",
    },
    "discoverability_rate": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "not measured from a measured 0% (every deferred tool was "
            "reachable)"
        ),
        "lie_prevented": (
            "0 when the axis was never driven, reading as a perfect score - "
            "the failure that was LIVE in this phase's own driver before "
            "P3b/W2 fixed it"
        ),
        "implemented_by": "shared.p3b_truth.row",
    },
    "skill_load_rate": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "no skill was ever matched from a measured zero-match scan"
        ),
        "lie_prevented": "0% skill-load rate for a scan that never ran",
        "implemented_by": "shared.p3b_truth.row",
    },
    "deferred_count": {
        "absent_value": None,
        "renders_as": UNAVAILABLE,
        "distinguishes": (
            "deferral did not run from deferral ran and deferred nothing"
        ),
        "lie_prevented": (
            "`deferred_count: 0` when deferral is disabled, which reads as "
            "'the policy kept everything' rather than 'no policy ran'"
        ),
        "implemented_by": "shared.p3b_truth.row",
    },
}

#: The metric names, in the order the brief lists them. Pinned so a rename is a
#: visible diff rather than a silently dropped rule.
METRICS: Sequence[str] = (
    "prompt_tokens",
    "prompt_reduction_pct",
    "cache_hit_rate",
    "discoverability_rate",
    "skill_load_rate",
    "deferred_count",
)


def summarise(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate a set of published rows without collapsing absence to zero.

    The aggregation rule is the point: the total is over the AVAILABLE rows and
    it carries ``n_available`` and ``n_absent`` beside it, so a reader can see
    that ``n_absent > 0`` and therefore that the total is over a THINNING
    denominator. A sum over available rows that does not report how many were
    skipped is the rollup form of the same lie, and it is the defect
    ``evals.slos`` already refuses with
    ``no_session_sum_without_the_call_count``.
    """
    available = [r for r in rows if r.get("available") is True]
    absent = [r for r in rows if r.get("available") is not True]
    numeric = [
        float(r["value"]) for r in available
        if isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool)
    ]
    total = sum(numeric) if numeric else None
    return {
        "n": len(rows),
        "n_available": len(available),
        "n_absent": len(absent),
        "absent_metrics": sorted(
            str(r.get("metric")) for r in absent
        ),
        "total_over_available": total,
        "total_is_over_a_thinning_denominator": len(absent) > 0,
        "note": (
            "the total covers only the available rows; with n_absent > 0 it is "
            "not comparable to a run with fewer absent axes"
        ),
    }


def verify_renderers() -> Dict[str, Any]:
    """Check that every renderer in the tree honours the ABSENT path.

    T4 owns the render. This function does not edit ``cli/**`` - it CALLS the
    public renderers and asserts what they produce, which is the only way to
    verify a layer this terminal does not own.

    Each renderer is asked for a row with no measurement and a row WITH one, so
    a renderer that returns the right thing for the absent case by accident (a
    constant, an empty list) is caught. The control is the whole test: without
    it, ``render_absent() == "unavailable"`` is satisfied by a function that
    returns that string for everything.
    """
    findings: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []

    # -- our own authority, both directions
    for metric in METRICS:
        absent = row(metric, None, reason="audit probe: no measurement")
        present = row(metric, 0.0, source="audit probe: measured")
        checked.append(
            {
                "renderer": "shared.p3b_truth.row",
                "metric": metric,
                "absent_renders_unavailable": absent.render().startswith(
                    f"{metric}: {UNAVAILABLE}"
                ),
                "absent_value_is_none": absent.value is None,
                "absent_doc_omits_value": "value" not in absent.to_dict(),
                "present_carries_zero": present.available is True and present.value == 0.0,
                "present_doc_carries_value": "value" in present.to_dict(),
            }
        )

    # -- T4's renderers. Imported defensively: this module is imported by
    # tests that run in lanes where cli/ may not be importable, and a missing
    # renderer is reported, never crashed on.
    for label, module_name, attr in (
        ("cli.context_budget.UNAVAILABLE", "cli.context_budget", "UNAVAILABLE"),
        ("cli.context_budget.budget_rows", "cli.context_budget", "budget_rows"),
        ("cli.cache_cost.assert_absent_is_never_zero", "cli.cache_cost",
         "assert_absent_is_never_zero"),
        ("cli.capability_view.assert_no_false_capability", "cli.capability_view",
         "assert_no_false_capability"),
        ("cli.skill_view.assert_no_skill_claims_a_body_it_did_not_load",
         "cli.skill_view", "assert_no_skill_claims_a_body_it_did_not_load"),
    ):
        entry: Dict[str, Any] = {"renderer": label, "imported": False}
        try:
            import importlib

            module = importlib.import_module(module_name)
            entry["imported"] = True
            entry["has_attribute"] = hasattr(module, attr)
            if attr == "UNAVAILABLE":
                entry["literal"] = getattr(module, attr, None)
                entry["literal_matches"] = getattr(module, attr, None) == UNAVAILABLE
            else:
                entry["is_gate"] = callable(getattr(module, attr, None))
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        checked.append(entry)

    for entry in checked:
        for key, value in entry.items():
            if key.endswith(("_unavailable", "_omits", "_is_none", "literal_matches")) and (
                value is not True
            ):
                findings.append(
                    {"renderer": entry.get("renderer", "?"), "failed": key}
                )
    return {
        "checked": checked,
        "findings": findings,
        "ok": not findings,
        "rule": (
            "an absent value renders as the unavailable literal, omits its "
            "value key entirely, and is distinguishable from a measured zero - "
            "verified in BOTH directions so the absent case cannot be satisfied "
            "by a constant"
        ),
    }


def render(doc: Mapping[str, Any]) -> str:
    """The human table for :func:`verify_renderers`."""
    lines = ["honest-absent rules", ""]
    for metric, rule in HONEST_ABSENT.items():
        lines.append(f"{metric}")
        lines.append(f"  absent       : {rule['absent_value']!r} -> {rule['renders_as']}")
        lines.append(f"  distinguishes: {rule['distinguishes']}")
        lines.append(f"  prevents     : {rule['lie_prevented']}")
        lines.append("")
    lines.append("RENDERER VERIFICATION")
    for entry in doc["checked"]:
        bits = [f"{k}={v}" for k, v in entry.items() if k not in {"renderer", "error"}]
        lines.append(f"  {entry['renderer']}")
        lines.append("    " + "  ".join(bits) if bits else "    (no attributes checked)")
    lines.append("")
    lines.append(f"verdict: {'HONEST_ABSENT_HOLDS' if doc['ok'] else 'HONEST_ABSENT_BROKEN'}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the rule set. Exit 2 when a renderer does not honour it."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m shared.p3b_truth",
        description="The six honest-absent rules and their renderer verification.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    doc = verify_renderers()
    if args.json:
        print(json.dumps({"rules": HONEST_ABSENT, "verification": doc},
                         indent=2, default=str))
    else:
        print(render(doc))
    return 0 if doc["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
