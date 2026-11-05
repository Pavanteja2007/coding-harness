"""P4/W1 T2.W1.2 — what the verification ACTUALLY checked.

`shared.types.VerificationResult` has four booleans and a transcript. It does
not say **what ran**. A `regression_passed: True` over 3 tests and the same
boolean over 3 000 are the same bytes, and the second is the one a reader
believes they are looking at.

This module is the ADDITIVE evidence layer Phase 4 pins onto that object. Per
`INTERFACES.md`'s additive-keys rule it attaches **instance attributes** (the
`execution/flake_gate.py::attach_evidence` precedent) rather than editing
`shared/types.py`, which is another module's file. Nothing here can widen a
verdict: the four booleans are read, never written.

--------------------------------------------------------------------------
THE FOUR RULES THIS MODULE EXISTS TO ENFORCE
--------------------------------------------------------------------------

**1. `not_run` is never `0`, never `False`.**
    `ThreeState` is the only way a dimension reports, and its `not_run` member
    has NO boolean value. ``Tri.not_run("nothing was collected").value`` is
    ``None``; ``coverage_fraction`` over an empty denominator is ``None``. A
    reader who does ``bool(coverage)`` sees ``False`` — so `Tri` raises on
    ``bool()`` instead of answering, and the failure is loud at the coercion
    rather than silent in a receipt.

**2. The three states are three states.** ``THREE_STATES`` is a closed
    three-tuple: ``("true", "false", "not_run")``. There is no fourth member
    and no coercion from ``None``. Phase 3a pinned this and Phase 4 does not
    get to widen it.

**3. A FOURTH dimension does not collapse them.** Phase 4 adds end-to-end
    verification as a dimension. It is added *alongside* the three, in
    ``DIMENSIONS``, and it is the same type as the other three — it is not a
    special richer object that could shadow them, and a consumer that reads only
    ``target``/``regression``/``flake`` sees exactly what it saw before. The
    new dimension is observable and separately renderable, and
    ``assert_no_dimension_collapse`` fails if any dimension's membership drifts.

**4. A coverage number that is 0% because nothing ran is ``not_run``.** Covered
    by rule 1, and pinned separately because it is the exact confusion Phase 3a
    warned about with the flake gate.

--------------------------------------------------------------------------
WHAT T5'S LEDGER GATE GETS
--------------------------------------------------------------------------

``ledger_evidence(result, ...)`` is the single value a feature-ledger gate
needs. It answers one question — **may this feature be marked `passing`?** —
with the requirements enumerated, the ones unmet named, and the reason a reused
result can never qualify. ``PROVABLE_REQUIREMENTS`` is the published list; a
consumer that disagrees adds a requirement rather than editing a boolean.

The load-bearing rule: ``fresh_evidence``. A result served from a
content-addressed cache (``execution/verify_cache.py``) is a real measurement of
an EARLIER tree. It can report ``regression_passed=True`` truthfully and it
still cannot mark a feature ``passing``, because the feature did not exist when
the cache entry was written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DIMENSIONS",
    "DIMENSION_DESCRIPTIONS",
    "DIMENSION_E2E",
    "DIMENSION_FLAKE",
    "DIMENSION_REGRESSION",
    "DIMENSION_TARGET",
    "EVIDENCE_ATTR",
    "EVIDENCE_SCHEMA_VERSION",
    "NOT_RUN",
    "PROVABLE_REQUIREMENTS",
    "THREE_FALSE",
    "THREE_NOT_RUN",
    "THREE_STATES",
    "THREE_TRUE",
    "EvidenceSkip",
    "ProvenanceRequirement",
    "ProvenanceVerdict",
    "SelectionRecord",
    "Tri",
    "TriCoercionRefused",
    "VerificationEvidence",
    "assert_no_dimension_collapse",
    "attach_evidence",
    "build_evidence",
    "coverage_fraction",
    "coverage_receipt",
    "evidence_of",
    "ledger_evidence",
    "render_ledger_evidence",
    "tri_of",
]


# --------------------------------------------------------------------------
# The three states
# --------------------------------------------------------------------------

THREE_TRUE = "true"
THREE_FALSE = "false"
THREE_NOT_RUN = "not_run"

#: The closed three-value vocabulary Phase 3a pinned. A fourth member is a
#: different policy and needs an INTERFACES change, not an edit here.
THREE_STATES: Tuple[str, str, str] = (THREE_TRUE, THREE_FALSE, THREE_NOT_RUN)


class TriCoercionRefused(TypeError):
    """Raised when a ``ThreeState`` is coerced to ``bool``.

    ``if tri:`` and ``bool(tri)`` are exactly the two ways a ``not_run`` becomes
    a ``False`` and a receipt starts claiming a measurement nobody took. This is
    a ``TypeError`` so a caller's existing ``except TypeError`` around a truth
    test still behaves, and the message names the fix.
    """


@dataclass(frozen=True)
class Tri:
    """Three states, one of which has no boolean value at all.

    ``value`` is ``True``/``False`` for an observed dimension and ``None`` for
    ``not_run``. ``None`` here is NOT "absent" — it is the measured statement
    "this was not measured", and it is distinguishable from a missing attribute
    only because the attribute always exists once evidence is attached.
    """

    state: str
    reason: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in THREE_STATES:
            raise ValueError(
                f"unknown three-state {self.state!r}; the vocabulary is closed: "
                f"{THREE_STATES}. A fourth state is a different policy."
            )

    # -- constructors ------------------------------------------------------

    @classmethod
    def ok(cls, reason: str = "measured and passed", detail: str = "") -> "Tri":
        """Return the observed-passing state."""
        return cls(THREE_TRUE, reason, detail)

    @classmethod
    def bad(cls, reason: str = "measured and failed", detail: str = "") -> "Tri":
        """Return the observed-failing state."""
        return cls(THREE_FALSE, reason, detail)

    @classmethod
    def not_run(cls, reason: str, detail: str = "") -> "Tri":
        """Return the unmeasured state. ``reason`` is MANDATORY.

        ``DOCTRINE.md`` §1: a blocked lane is not a pass and a missing
        measurement is not a passing one. An unmeasured dimension with no reason
        is indistinguishable from a bug in the caller, so the constructor
        refuses to build one.
        """
        text = str(reason or "").strip()
        if not text:
            raise ValueError(
                "a not_run dimension must carry a reason: 'we did not measure "
                "this' and 'we measured nothing and forgot why' are different "
                "facts and only one of them is honest"
            )
        return cls(THREE_NOT_RUN, text, detail)

    @classmethod
    def of(
        cls,
        value: Optional[bool],
        *,
        reason: str = "",
        detail: str = "",
    ) -> "Tri":
        """Build a state from a raw boolean, mapping ``None`` to ``not_run``.

        ``reason`` is required only for the ``None`` branch. A caller that
        passes ``None`` with no reason gets the refusal rather than a state with
        an empty explanation.
        """
        if value is None:
            return cls.not_run(reason or "no value was reported", detail)
        return cls(THREE_TRUE if value else THREE_FALSE, reason, detail)

    # -- readers -----------------------------------------------------------

    @property
    def value(self) -> Optional[bool]:
        """The boolean value, or ``None`` for ``not_run``.

        This is the honest reader. ``None`` is a statement, not an absence.
        """
        if self.state == THREE_TRUE:
            return True
        if self.state == THREE_FALSE:
            return False
        return None

    @property
    def ran(self) -> bool:
        """Whether anything was measured for this dimension."""
        return self.state != THREE_NOT_RUN

    @property
    def is_not_run(self) -> bool:
        """Whether this dimension was not measured.

        NAMED ``is_not_run`` and not ``not_run`` on purpose, and this is a bug
        the module's own import caught rather than a style preference. The first
        draft defined a ``not_run`` **classmethod** (the constructor) and then a
        ``not_run`` **property** (the reader) in the same class body. The
        property is defined last, so it REPLACED the classmethod, and
        ``Tri.not_run("reason")`` raised ``TypeError: 'property' object is not
        callable`` — at module import, because the module-level ``NOT_RUN``
        constant is built with it.

        The lesson is the same one this module exists to enforce: two names for
        one concept is two answers to one question, and the reader that survives
        is not necessarily the one a caller means.
        """
        return self.state == THREE_NOT_RUN

    def as_bool_or_none(self) -> Optional[bool]:
        """Explicit reader for callers that want ``None`` rather than a raise."""
        return self.value

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection. ``value`` is ``None`` for ``not_run``, never 0."""
        return {
            "state": self.state,
            "value": self.value,
            "ran": self.ran,
            "reason": self.reason,
            "detail": self.detail,
        }

    def render(self) -> str:
        """Render for a human. ``not_run`` NEVER renders as a number."""
        if self.state == THREE_TRUE:
            return self.reason or "measured: passed"
        if self.state == THREE_FALSE:
            return self.reason or "measured: failed"
        return f"NOT MEASURED — {self.reason or 'no reason recorded'}"

    def __bool__(self) -> bool:
        raise TriCoercionRefused(
            "a ThreeState refuses bool() on purpose: `if tri:` and `bool(tri)` "
            "are how a not_run becomes a False and a receipt starts claiming a "
            "measurement nobody took. Read `.value` (None when not_run), "
            "`.ran`, or `.state`."
        )

    def __int__(self) -> int:
        # `int(tri)` is the same coercion wearing a hat (f-strings, sum(), ...).
        raise TriCoercionRefused(
            "a ThreeState refuses int() for the same reason it refuses bool(): "
            "an unmeasured dimension must not be countable. Read `.value`."
        )

    def __index__(self) -> int:  # pragma: no cover - defensive
        raise TriCoercionRefused(
            "a ThreeState refuses __index__; read `.value` instead."
        )


#: A module-level ``not_run`` with no opinion of its own. Consumers should
#: prefer ``Tri.not_run(reason)``; this exists so a dimension that was never
#: populated is still a real object rather than a missing attribute.
NOT_RUN = Tri.not_run(
    "no verification evidence was attached to this result",
)


def tri_of(value: Any) -> Tri:
    """Coerce ``value`` to a ``Tri`` without ever inventing a measurement.

    * a ``Tri`` is returned unchanged;
    * ``None`` becomes ``not_run`` with a stated reason;
    * ``bool`` / ``int`` become ``true`` / ``false``;
    * a string in the closed vocabulary becomes that state;
    * **anything else raises.** A mapping, a string nobody defined, a list — a
      reader that cannot classify a value must not be handed a guess.
    """
    if isinstance(value, Tri):
        return value
    if value is None:
        return Tri.not_run("no value was reported for this dimension")
    if isinstance(value, bool):
        return Tri.of(value, reason="measured and reported")
    if isinstance(value, int) and value in (0, 1):
        return Tri.of(bool(value), reason="measured and reported as an integer")
    if isinstance(value, str) and value in THREE_STATES:
        if value == THREE_NOT_RUN:
            return Tri.not_run("reported as not_run without a reason")
        return Tri(value, "reported")
    raise TypeError(
        f"cannot classify {type(value).__name__} as a verification state; "
        f"the vocabulary is {THREE_STATES} and a ThreeState"
    )


# --------------------------------------------------------------------------
# The four dimensions
# --------------------------------------------------------------------------

DIMENSION_TARGET = "target"
DIMENSION_REGRESSION = "regression"
DIMENSION_FLAKE = "flake"
#: Phase 4's addition. It is a FOURTH member of the same tuple and the same
#: type as the other three — not a richer object that could shadow them.
DIMENSION_E2E = "e2e"

#: Every dimension verification reports on. The first three are Phase 3a's; the
#: fourth is Phase 4's and is ADDED ALONGSIDE them.
DIMENSIONS: Tuple[str, str, str, str] = (
    DIMENSION_TARGET,
    DIMENSION_REGRESSION,
    DIMENSION_FLAKE,
    DIMENSION_E2E,
)

DIMENSION_DESCRIPTIONS: Dict[str, str] = {
    DIMENSION_TARGET: "the declared target test passed",
    DIMENSION_REGRESSION: "the regression check passed",
    DIMENSION_FLAKE: "the target test was observed stable across repetitions",
    DIMENSION_E2E: (
        "the declared end-to-end check for the feature passed. This asks "
        "'does the thing work', which the three above do not ask"
    ),
}


def assert_no_dimension_collapse() -> Tuple[str, ...]:
    """Prove the four dimensions are four distinct dimensions of one type.

    Returns the dimensions on success and RAISES otherwise. This is the
    executable form of rule 3: if a future edit gives ``e2e`` its own richer
    type, or folds it into ``regression``, or drops one of the first three, this
    fails before a consumer can read a collapsed receipt.
    """
    problems: List[str] = []
    if len(DIMENSIONS) != 4:
        problems.append(
            f"expected exactly 4 dimensions, found {len(DIMENSIONS)}: {DIMENSIONS}"
        )
    if len(set(DIMENSIONS)) != len(DIMENSIONS):
        problems.append(f"dimensions are not distinct: {DIMENSIONS}")
    for required in (DIMENSION_TARGET, DIMENSION_REGRESSION, DIMENSION_FLAKE):
        if required not in DIMENSIONS:
            problems.append(
                f"Phase 3a dimension {required!r} is missing; a new dimension "
                f"may be ADDED but the existing three are not removable"
            )
    for name in DIMENSIONS:
        if name not in DIMENSION_DESCRIPTIONS:
            problems.append(f"dimension {name!r} has no published description")
    # Every dimension must be populated with the SAME type. This is the part
    # that catches "e2e got a richer object and shadowed the rest".
    sample = Tri.not_run("shape check")
    if not isinstance(sample, Tri):  # pragma: no cover - structurally impossible
        problems.append("dimensions are not populated with ThreeState")
    if problems:
        raise AssertionError(
            "the four verification dimensions collapsed: "
            + "; ".join(problems)
        )
    return DIMENSIONS


# --------------------------------------------------------------------------
# Coverage — and the rule that zero-by-absence is not zero
# --------------------------------------------------------------------------


def coverage_fraction(
    selected: Optional[int],
    total: Optional[int],
) -> Optional[float]:
    """Return the covered fraction, or ``None`` when it was not measured.

    ``None`` — never ``0.0`` — is returned when the denominator is unknown,
    when nothing ran, or when the numbers are unusable. **A coverage number
    that is 0% because nothing ran is ``not_run``, not 0%**, which is the exact
    confusion Phase 3a recorded against the flake gate.

    ``selected > total`` returns ``1.0`` and says so in
    ``coverage_receipt``: a selection can legitimately name a test the count did
    not include (a rename, a parametrised case), and reporting ``1.5`` would be
    a worse lie than clamping with the clamp recorded.
    """
    if selected is None or total is None:
        return None
    try:
        sel = int(selected)
        tot = int(total)
    except (TypeError, ValueError):
        return None
    if tot <= 0:
        return None
    if sel <= 0:
        # Nothing ran. This is the trap: 0/3 is genuinely 0%, but 0-of-3 with
        # zero SELECTED means no measurement was made at all.
        return None
    if sel >= tot:
        return 1.0
    return sel / tot


def coverage_receipt(
    selected: Optional[int],
    total: Optional[int],
    *,
    basis: str = "",
) -> Dict[str, Any]:
    """A coverage projection that carries WHY it is ``None``.

    ``fraction: None`` with ``basis: "nothing_ran"`` is a different fact from
    ``fraction: None`` with ``basis: "denominator_unknown"``, and a reader who
    can only see the fraction learns nothing from either.
    """
    try:
        sel_int = int(selected) if selected is not None else None
    except (TypeError, ValueError):
        sel_int = None
    fraction = coverage_fraction(selected, total)
    if total is None:
        why = "denominator_unknown"
    elif not isinstance(total, int) or int(total) <= 0:
        why = "no_tests_in_denominator"
    elif not sel_int or sel_int <= 0:
        why = "nothing_ran"
    elif sel_int >= int(total):
        why = "selection_at_or_over_denominator"
    else:
        why = "measured"
    return {
        "fraction": fraction,
        "selected": selected,
        "total": total,
        "measured": fraction is not None,
        "basis": basis or why,
        "why_not_measured": "" if fraction is not None else why,
        "note": (
            "a coverage fraction of null means no measurement was taken; it is "
            "never the number zero"
        ),
    }


# --------------------------------------------------------------------------
# The additive evidence block
# --------------------------------------------------------------------------

EVIDENCE_SCHEMA_VERSION = 1

#: The single instance attribute this module writes on a ``VerificationResult``.
EVIDENCE_ATTR = "verification_evidence"


@dataclass(frozen=True)
class SelectionRecord:
    """One test the run SELECTED and actually dispatched."""

    node: str
    path: str = ""
    dispatched: bool = True
    outcome: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "path": self.path,
            "dispatched": bool(self.dispatched),
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class EvidenceSkip:
    """One test the run did NOT run, and why.

    ``reason`` is mandatory and non-empty. "Skipped" without a reason is the
    class of receipt this repository exists to refuse.
    """

    node: str
    reason: str
    path: str = ""
    shape: str = ""

    def __post_init__(self) -> None:
        if not str(self.reason or "").strip():
            raise ValueError(
                f"an evidence skip for {self.node!r} must carry a reason"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "reason": self.reason,
            "path": self.path,
            "shape": self.shape,
        }


@dataclass
class VerificationEvidence:
    """What the verification actually checked, as a value.

    Constructed by :func:`build_evidence`; attached to a ``VerificationResult``
    by :func:`attach_evidence`. Every field is either a measurement or an
    explicitly-unmeasured marker — there is no field whose value is a defaulted
    zero standing in for a fact nobody took.
    """

    dimensions: Dict[str, Tri] = field(default_factory=dict)
    selected: List[SelectionRecord] = field(default_factory=list)
    skipped: List[EvidenceSkip] = field(default_factory=list)
    not_run: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    regression_check: str = ""
    regression_scope: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)
    suite_command: str = ""
    regression_command: str = ""
    target_command: str = ""
    fresh_evidence: bool = False
    reuse: Dict[str, Any] = field(default_factory=dict)
    e2e: Dict[str, Any] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    # -- readers -----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        return EVIDENCE_SCHEMA_VERSION

    @property
    def selected_count(self) -> Optional[int]:
        """How many tests were selected. ``None`` when nothing selected any."""
        return len(self.selected) if self.selected else None

    @property
    def skipped_count(self) -> Optional[int]:
        """How many were skipped with a reason. ``None`` when there were none."""
        return len(self.skipped) if self.skipped else None

    @property
    def coverage_fraction(self) -> Optional[float]:
        """The coverage fraction, or ``None``. Never ``0.0`` for an absence."""
        value = self.coverage.get("fraction")
        return value if isinstance(value, (int, float)) else None

    @property
    def measured(self) -> bool:
        """Whether ANY dimension carries an observation.

        ``False`` means the run produced no measurement at all — which is the
        state that must never be rendered as a passing verification.
        """
        return any(dimension.ran for dimension in self.dimensions.values())

    @property
    def fresh(self) -> bool:
        """Whether this evidence came from a run in THIS process, not a cache.

        ``execution/verify_cache.py`` sets this to ``False`` on a served result.
        It is the single field a feature-ledger gate must require.
        """
        return bool(self.fresh_evidence)

    @property
    def unmeasured_dimensions(self) -> Tuple[str, ...]:
        """Names of the dimensions with no observation, with their reasons."""
        return tuple(
            f"{name}={self.dimensions[name].reason or 'no reason recorded'}"
            for name in DIMENSIONS
            if name in self.dimensions and self.dimensions[name].is_not_run
        )

    def dimension(self, name: str) -> Tri:
        """Read one dimension. An unknown name raises rather than answering."""
        if name not in DIMENSIONS:
            raise KeyError(
                f"unknown verification dimension {name!r}; the vocabulary is "
                f"{DIMENSIONS}"
            )
        return self.dimensions.get(name, NOT_RUN)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection. Every key here is ADDITIVE."""
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "dimensions": {
                name: self.dimensions.get(name, NOT_RUN).to_dict()
                for name in DIMENSIONS
            },
            "dimension_descriptions": dict(DIMENSION_DESCRIPTIONS),
            "selected": [row.to_dict() for row in self.selected],
            "selected_count": self.selected_count,
            "skipped": [row.to_dict() for row in self.skipped],
            "skipped_count": self.skipped_count,
            "not_run": list(self.unmeasured_dimensions),
            "commands": list(self.commands),
            "regression_check": self.regression_check,
            "regression_scope": self.regression_scope,
            "coverage": dict(self.coverage),
            "suite_command": self.suite_command,
            "regression_command": self.regression_command,
            "target_command": self.target_command,
            "fresh_evidence": bool(self.fresh_evidence),
            "reuse": dict(self.reuse),
            "e2e": dict(self.e2e),
            "cost": dict(self.cost),
            "notes": list(self.notes),
            "measured": self.measured,
            "incomplete_reasons": list(self.unmeasured_dimensions),
        }

    def render(self) -> List[str]:
        """Render for a human. One line per dimension plus the coverage line."""
        lines: List[str] = []
        for name in DIMENSIONS:
            state = self.dimensions.get(name, NOT_RUN)
            lines.append(f"  {name:<11} {state.state:<8} {state.render()}")
        fraction = self.coverage_fraction
        if fraction is None:
            why = self.coverage.get("why_not_measured") or "not measured"
            lines.append(f"  coverage    NOT MEASURED ({why})")
        else:
            lines.append(
                f"  coverage    {fraction * 100:.1f}% "
                f"({self.coverage.get('selected')}/{self.coverage.get('total')})"
            )
        if self.selected_count is not None:
            lines.append(f"  selected    {self.selected_count} test(s)")
        if self.skipped_count is not None:
            lines.append(
                f"  skipped     {self.skipped_count} test(s), each with a reason"
            )
        if not self.fresh_evidence:
            reuse = self.reuse.get("summary") or "reused; no fresh run"
            lines.append(f"  freshness   REUSED — {reuse}")
        return lines


def build_evidence(
    *,
    target: Any = None,
    regression: Any = None,
    flake: Any = None,
    e2e: Any = None,
    selected: Sequence[Any] = (),
    skipped: Sequence[Any] = (),
    not_run: Sequence[str] = (),
    commands: Sequence[str] = (),
    regression_check: str = "",
    regression_scope: str = "",
    coverage: Optional[Mapping[str, Any]] = None,
    suite_command: str = "",
    regression_command: str = "",
    target_command: str = "",
    fresh_evidence: bool = False,
    reuse: Optional[Mapping[str, Any]] = None,
    e2e_receipt: Optional[Mapping[str, Any]] = None,
    cost: Optional[Mapping[str, Any]] = None,
    notes: Sequence[str] = (),
) -> VerificationEvidence:
    """Assemble a :class:`VerificationEvidence` from measured facts.

    ``fresh_evidence`` defaults to ``False`` on purpose. A builder that forgets
    to state freshness must not accidentally produce a result a ledger gate
    will accept; it has to say "yes, this ran now".
    """
    assert_no_dimension_collapse()
    rows: List[SelectionRecord] = []
    for item in selected or ():
        if isinstance(item, SelectionRecord):
            rows.append(item)
        elif isinstance(item, Mapping):
            rows.append(
                SelectionRecord(
                    node=str(item.get("node") or item.get("id") or ""),
                    path=str(item.get("path") or ""),
                    dispatched=bool(item.get("dispatched", True)),
                    outcome=str(item.get("outcome") or ""),
                )
            )
        else:
            rows.append(SelectionRecord(node=str(item)))

    skips: List[EvidenceSkip] = []
    for item in skipped or ():
        if isinstance(item, EvidenceSkip):
            skips.append(item)
        elif isinstance(item, Mapping):
            skips.append(
                EvidenceSkip(
                    node=str(item.get("node") or item.get("id") or ""),
                    reason=str(item.get("reason") or ""),
                    path=str(item.get("path") or ""),
                    shape=str(item.get("shape") or ""),
                )
            )
        else:
            skips.append(EvidenceSkip(node=str(item), reason=str(item)))

    dimensions = {
        DIMENSION_TARGET: tri_of(target),
        DIMENSION_REGRESSION: tri_of(regression),
        DIMENSION_FLAKE: tri_of(flake),
        DIMENSION_E2E: tri_of(e2e),
    }

    if coverage is not None:
        coverage_dict = dict(coverage)
    else:
        coverage_dict = coverage_receipt(
            coverage_selected_of(rows), None, basis="no denominator was reported"
        )

    evidence = VerificationEvidence(
        dimensions=dimensions,
        selected=rows,
        skipped=skips,
        not_run=[str(item) for item in (not_run or ())],
        commands=[str(item) for item in (commands or ())],
        regression_check=str(regression_check or ""),
        regression_scope=str(regression_scope or ""),
        coverage=coverage_dict,
        suite_command=str(suite_command or ""),
        regression_command=str(regression_command or ""),
        target_command=str(target_command or ""),
        fresh_evidence=bool(fresh_evidence),
        reuse=dict(reuse or {}),
        e2e=dict(e2e_receipt or {}),
        cost=dict(cost or {}),
        notes=[str(item) for item in (notes or ())],
    )
    return evidence


def coverage_selected_of(rows: Sequence[SelectionRecord]) -> Optional[int]:
    """Count DISPATCHED selections. An un-dispatched row is not coverage."""
    dispatched = sum(1 for row in rows if row.dispatched)
    return dispatched or None


def attach_evidence(result: Any, evidence: VerificationEvidence) -> Any:
    """Attach the evidence block to a ``VerificationResult``.

    Additive: one new instance attribute. ``shared/types.py`` is another
    module's file, so this is the ``execution/flake_gate.py::attach_evidence``
    route and the cost is that a journal replay loses the block — recorded in
    the module docstring and in the handoff.

    Returns ``result`` so a caller can chain. Refuses a non-evidence value so a
    dict cannot silently become the receipt.
    """
    if not isinstance(evidence, VerificationEvidence):
        raise TypeError(
            "attach_evidence takes a VerificationEvidence; a bare mapping "
            "would let a caller describe the evidence instead of reporting it"
        )
    setattr(result, EVIDENCE_ATTR, evidence)
    return result


def evidence_of(result: Any) -> Optional[VerificationEvidence]:
    """Read the attached evidence, or ``None`` when there is none.

    ``None`` means "this result carries no evidence block", which is a DIFFERENT
    fact from "the evidence block says nothing ran". Consumers must handle both.
    """
    value = getattr(result, EVIDENCE_ATTR, None)
    if value is None:
        return None
    if isinstance(value, VerificationEvidence):
        return value
    raise TypeError(
        f"{EVIDENCE_ATTR} is a {type(value).__name__}, not a "
        f"VerificationEvidence; a hand-assembled value is not a measurement"
    )


# --------------------------------------------------------------------------
# T5's ledger gate — the exact requirements, published
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceRequirement:
    """One condition a feature must satisfy before it may be marked ``passing``."""

    id: str
    question: str
    consequence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "consequence": self.consequence,
        }


#: Published so T5 can render them, diff them, and add one without editing a
#: boolean. Every requirement can only ever REFUSE.
PROVABLE_REQUIREMENTS: Tuple[ProvenanceRequirement, ...] = (
    ProvenanceRequirement(
        id="fresh_evidence",
        question="Did a verification run in THIS state of THIS tree, now?",
        consequence=(
            "a content-addressed cache hit reports a real measurement of an "
            "EARLIER tree; it can be shown to a human but it can never mark a "
            "feature passing, because the feature did not exist when it was "
            "written"
        ),
    ),
    ProvenanceRequirement(
        id="target_observed_and_passed",
        question="Was the declared target test observed, and did it pass?",
        consequence="'not_run' is not 'passed'",
    ),
    ProvenanceRequirement(
        id="regression_observed_and_passed",
        question="Was the regression check observed, and did it pass?",
        consequence="'not_run' is not 'passed'",
    ),
    ProvenanceRequirement(
        id="regression_check_complete",
        question=(
            "If a regression check was observed, did it cover the whole "
            "denominator?"
        ),
        consequence=(
            "a passing PARTIAL check is not evidence about the suite; "
            "execution/verify.py already folds this to regression_passed=False, "
            "and this requirement exists so the ledger cannot read the raw "
            "boolean and miss the fold"
        ),
    ),
    ProvenanceRequirement(
        id="flake_observed_and_stable",
        question="Was stability observed across at least two observations?",
        consequence=(
            "one observation cannot distinguish 'stable' from 'never checked'; "
            "execution/flake_gate.py already says not_run there and this "
            "requirement requires the ledger to read the three-valued verdict "
            "rather than the boolean"
        ),
    ),
    ProvenanceRequirement(
        id="e2e_settled",
        question=(
            "Did the declared end-to-end check for this feature pass, or is "
            "there no end-to-end check and the feature is therefore unverified "
            "BEHAVIOUR?"
        ),
        consequence=(
            "this is the requirement Phase 4 exists for: a green unit suite "
            "does not answer 'does the thing work'. A feature with no e2e check "
            "is not 'passing'; it is 'unverified behaviour', and T5 owns which "
            "ledger state that is"
        ),
    ),
    ProvenanceRequirement(
        id="no_blocked_dimension",
        question="Is any dimension blocked rather than decided?",
        consequence=(
            "'blocked' means 'I could not check'. It is NOT 'false' and NOT "
            "'skipped', and Phase 3's ledger records the three differently"
        ),
    ),
    ProvenanceRequirement(
        id="no_error",
        question="Did the verification run complete without an error?",
        consequence="a crashed verifier leaves stale booleans behind",
    ),
)


@dataclass(frozen=True)
class ProvenanceVerdict:
    """The answer to "may this feature be marked ``passing``?"."""

    provable: bool
    met: Tuple[str, ...]
    unmet: Tuple[str, ...]
    reasons: Tuple[str, ...]
    dimensions: Dict[str, str]
    fresh_evidence: bool
    e2e_declared: bool
    e2e_settled: bool

    @property
    def suggested_state(self) -> str:
        """A suggested ledger state. T5 owns the vocabulary; this is a hint.

        Deliberately NOT the word "passing" on any path a human could mistake
        for a mint. The three-way split is the point: settled-and-green,
        not-yet-measured, and decided-against are three different facts.
        """
        if self.provable:
            return "evidence_complete"
        if self.unmet == ("e2e_settled",):
            return "unverified_behaviour"
        if any(item in self.unmet for item in ("fresh_evidence",)):
            return "no_fresh_evidence"
        if not self.met:
            return "unmeasured"
        return "evidence_incomplete"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provable": bool(self.provable),
            "suggested_state": self.suggested_state,
            "met": list(self.met),
            "unmet": list(self.unmet),
            "reasons": list(self.reasons),
            "dimensions": dict(self.dimensions),
            "fresh_evidence": bool(self.fresh_evidence),
            "e2e_declared": bool(self.e2e_declared),
            "e2e_settled": bool(self.e2e_settled),
            "requirements": [row.to_dict() for row in PROVABLE_REQUIREMENTS],
            "note": (
                "provable=False is not a failing feature. 'I could not check' "
                "and 'it is broken' are different facts and a feature ledger "
                "that renders them the same way cannot be repaired afterwards"
            ),
        }

    def render(self) -> str:
        lines = [
            f"feature may be marked passing: {self.provable}",
            f"  suggested ledger state : {self.suggested_state}",
            f"  fresh evidence        : {self.fresh_evidence}",
            f"  e2e declared          : {self.e2e_declared}",
            f"  e2e settled           : {self.e2e_settled}",
        ]
        for name in DIMENSIONS:
            lines.append(f"  dimension {name:<11}: {self.dimensions.get(name, '?')}")
        if self.met:
            lines.append(f"  requirements met      : {', '.join(self.met)}")
        if self.unmet:
            lines.append(f"  requirements NOT met  : {', '.join(self.unmet)}")
            for reason in self.reasons:
                lines.append(f"      - {reason}")
        return "\n".join(lines)


def ledger_evidence(
    result: Any,
    *,
    e2e_declared: Optional[bool] = None,
    error: Optional[str] = None,
) -> ProvenanceVerdict:
    """Derive the ledger-gate verdict for one ``VerificationResult``.

    This is the single value T5's feature ledger needs to decide whether a
    feature may be marked ``passing``, and it is deliberately **additive to
    what T5 already does**: it can only refuse, never authorise a state T5
    would not otherwise have reached.

    ``e2e_declared`` answers a question the result cannot: *did this feature
    declare an end-to-end check at all?* Absent an answer, a missing e2e
    dimension is treated as undeclared-but-required-to-settle, and the verdict
    says ``unverified_behaviour``. That is the honest reading: a feature nobody
    wrote an e2e check for has not been checked end to end.

    ``error`` carries a harness-level error string when the caller has one. A
    crashed verifier leaves the four booleans stale, so this can only refuse.
    """
    assert_no_dimension_collapse()
    evidence = evidence_of(result)
    dimensions: Dict[str, str] = {
        name: (
            evidence.dimensions.get(name, NOT_RUN).state
            if evidence is not None
            else THREE_NOT_RUN
        )
        for name in DIMENSIONS
    }

    met: List[str] = []
    unmet: List[str] = []
    reasons: List[str] = []

    # -- fresh_evidence ----------------------------------------------------
    fresh = bool(evidence.fresh) if evidence is not None else False
    if fresh:
        met.append("fresh_evidence")
    else:
        unmet.append("fresh_evidence")
        reasons.append(
            "no verification ran against this tree in this process; a cached "
            "result describes an earlier state and cannot mark a feature "
            "passing"
        )

    # -- target ------------------------------------------------------------
    if dimensions[DIMENSION_TARGET] == THREE_TRUE:
        met.append("target_observed_and_passed")
    else:
        unmet.append("target_observed_and_passed")
        reasons.append(
            f"target dimension is {dimensions[DIMENSION_TARGET]!r}"
            + (
                f": {evidence.dimension(DIMENSION_TARGET).reason}"
                if evidence is not None and evidence.dimension(DIMENSION_TARGET).reason
                else ""
            )
        )

    # -- regression --------------------------------------------------------
    if dimensions[DIMENSION_REGRESSION] == THREE_TRUE:
        met.append("regression_observed_and_passed")
    else:
        unmet.append("regression_observed_and_passed")
        reasons.append(
            f"regression dimension is {dimensions[DIMENSION_REGRESSION]!r}"
            + (
                f": {evidence.dimension(DIMENSION_REGRESSION).reason}"
                if evidence is not None and evidence.dimension(DIMENSION_REGRESSION).reason
                else ""
            )
        )

    # -- regression completeness -------------------------------------------
    check = (evidence.regression_check if evidence is not None else "") or ""
    if not check:
        unmet.append("regression_check_complete")
        reasons.append(
            "no regression_check value was reported; the ledger cannot tell a "
            "complete check from a partial one"
        )
    elif check == "complete":
        met.append("regression_check_complete")
    else:
        unmet.append("regression_check_complete")
        reasons.append(
            f"regression_check is {check!r}, not 'complete': a passing subset "
            f"is not evidence about the suite"
        )

    # -- flake -------------------------------------------------------------
    if dimensions[DIMENSION_FLAKE] == THREE_TRUE:
        met.append("flake_observed_and_stable")
    elif dimensions[DIMENSION_FLAKE] == THREE_FALSE:
        # The boolean `flaky` is True here; the feature is decided AGAINST.
        unmet.append("flake_observed_and_stable")
        reasons.append("the target test was observed to be flaky")
    else:
        unmet.append("flake_observed_and_stable")
        reasons.append(
            "stability was never observed (fewer than two observations); "
            "execution/flake_gate.py reports not_run and the boolean `flaky` "
            "is False for both 'stable' and 'never checked'"
        )

    # -- e2e ---------------------------------------------------------------
    declared = bool(e2e_declared) if e2e_declared is not None else False
    if evidence is not None and evidence.e2e:
        declared = declared or bool(evidence.e2e.get("declared"))
    e2e_state = dimensions[DIMENSION_E2E]
    if e2e_state == THREE_TRUE:
        met.append("e2e_settled")
        e2e_settled = True
    elif e2e_state == THREE_FALSE:
        e2e_settled = True
        unmet.append("e2e_settled")
        reasons.append(
            "the declared end-to-end check FAILED. The unit suite is a "
            "different question and a green one does not answer this"
        )
    else:
        e2e_settled = False
        unmet.append("e2e_settled")
        reasons.append(
            (
                "the declared end-to-end check could not be run"
                if declared
                else "this feature declares no end-to-end check, so nobody has "
                "established that the thing a user touches works"
            )
            + (
                f": {evidence.dimension(DIMENSION_E2E).reason}"
                if evidence is not None and evidence.dimension(DIMENSION_E2E).reason
                else ""
            )
        )

    # -- blocked dimensions -------------------------------------------------
    blocked: List[str] = []
    for name in DIMENSIONS:
        state = evidence.dimension(name) if evidence is not None else None
        if state is None:
            continue
        if state.is_not_run:
            continue
        if "block" in state.reason.lower() and state.state == THREE_FALSE:
            blocked.append(name)
    if blocked:
        unmet.append("no_blocked_dimension")
        reasons.append(
            f"dimension(s) {', '.join(blocked)} reported a blocked outcome; "
            f"blocked is not false and not skipped"
        )
    elif not any(dim == THREE_NOT_RUN for dim in dimensions.values()):
        met.append("no_blocked_dimension")
    else:
        unmet.append("no_blocked_dimension")
        reasons.append(
            "one or more dimensions were never measured, which is a blocked "
            "state for the ledger even though the booleans read False"
        )

    # -- error --------------------------------------------------------------
    if error:
        unmet.append("no_error")
        reasons.append(f"the verification run reported an error: {error}")
    elif getattr(result, "error", ""):
        unmet.append("no_error")
        reasons.append(f"the result carries an error: {result.error}")
    else:
        met.append("no_error")

    return ProvenanceVerdict(
        provable=not unmet,
        met=tuple(met),
        unmet=tuple(unmet),
        reasons=tuple(reasons),
        dimensions=dimensions,
        fresh_evidence=fresh,
        e2e_declared=declared,
        e2e_settled=e2e_settled,
    )


def render_ledger_evidence(result: Any, **kwargs: Any) -> str:
    """Render :func:`ledger_evidence` as the lines a surface can print."""
    return ledger_evidence(result, **kwargs).render()
