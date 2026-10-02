"""T2.W2.2 — the three-valued flake gate, pinned against its documented misreading.

``INTERFACES.md`` Boundary 1 carries a warning that is easy to get wrong, and
this module exists because it is easy to get wrong:

    ``verify()`` returns ``flaky`` which is **not checked** when
    ``run_count == 1``. Because ``max(1, rerun_for_flake_check)`` makes
    ``run_count == 1`` for ``{0, 1}``, ``flaky = len(set(outcomes)) > 1`` is
    unsatisfiable at those values, so ``flaky=False`` there means **"NOT
    CHECKED"**, not "stable". Callers must read ``flake_check`` from
    ``execution/flake_gate.py`` — never ``flaky`` alone.

The three properties pinned here
-------------------------------

1. :func:`test_flake_verdict_is_not_run_below_two_observed_repetitions` —
   fewer than two observed outcomes yields ``not_run``, never ``not_flaky``.
2. :func:`test_a_caller_that_reports_fewer_outcomes_than_it_requested_degrades_to_not_run`
   — the gate is fail-closed against its OWN caller: asking for three
   repetitions and reporting one degrades to ``not_run``, not to a
   manufactured ``not_flaky``.
3. :func:`test_every_flaky_read_site_is_paired_or_allowlisted` — an AST scan
   over every ``flaky`` read in ``execution/``, where each site either also
   reads ``flake_check`` / ``detection_possible`` in the same function, or
   appears in a WRITTEN allowlist carrying a reason and a direction.

Why an AST scan and not a grep
------------------------------

A grep finds ``.flaky`` and misses ``getattr(result, "flaky", None)``, which is
the form three of the call sites in this package actually use — including
``execution/baseline_set.py::classify_run``, where the string is the only thing
that reads the field. A grep also matches prose in docstrings and comments, so
it cannot distinguish a read from a mention, or a read from a WRITE. Both
misses are exactly the direction this pin has to be right in.

Run: ``python -m pytest execution/test_flake_gate_pins.py -q``
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

from execution.flake_gate import (
    FLAKE_CHECK_VALUES,
    MIN_REPETITIONS_FOR_DETECTION,
    NOT_FLAKY,
    NOT_RUN,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    OUTCOME_TIMEOUT,
    attach_evidence,
    flake_verdict,
)

#: The three-valued vocabulary must stay CLOSED. A fourth value would be a
#: consumer's problem, so the set itself is pinned here and re-exported by the
#: module under test.
EXPECTED_FLAKE_CHECK_VALUES: Tuple[str, ...] = (
    "flaky_detected",
    "not_flaky",
    "not_run",
)


def test_the_flake_check_vocabulary_is_closed() -> None:
    """A closed set is what lets a consumer REJECT an unknown value.

    ``not_run`` exists only because ``flaky=False`` reads as "we checked and it
    was stable". Without the third value the two states are the same bytes.
    """
    assert FLAKE_CHECK_VALUES == EXPECTED_FLAKE_CHECK_VALUES
    assert MIN_REPETITIONS_FOR_DETECTION == 2


# ---------------------------------------------------------------------------
# 1 + 2 — the gate itself
# ---------------------------------------------------------------------------


def test_flake_verdict_is_not_run_below_two_observed_repetitions() -> None:
    """Property 1: fewer than two OBSERVED outcomes ⇒ ``not_run``.

    ``flaky`` is ``False`` in every one of these cases. The assertion that
    matters is ``detection_possible is False`` and ``flake_check == NOT_RUN``:
    a reader who only sees ``flaky is False`` has been told nothing.

    The OBSERVED count is the threshold, not the requested one — that is the
    whole fail-closed design, and
    :func:`test_a_caller_that_reports_fewer_outcomes_than_it_requested_degrades_to_not_run`
    exercises it from the other direction.
    """
    cases: Tuple[Tuple[int, Tuple[str, ...]], ...] = (
        (1, (OUTCOME_PASS,)),
        (1, (OUTCOME_FAIL,)),
        (1, (OUTCOME_TIMEOUT,)),
        (0, ()),
        (1, ()),
        (0, (OUTCOME_PASS,)),
    )
    for repetitions, outcomes in cases:
        verdict = flake_verdict(repetitions, outcomes)
        assert len(outcomes) < MIN_REPETITIONS_FOR_DETECTION
        assert verdict.flake_check == NOT_RUN, (
            f"repetitions={repetitions} outcomes={outcomes} produced "
            f"{verdict.flake_check!r}; below two observations nothing was "
            "checked, so 'not_flaky' would be a claim the gate cannot make"
        )
        assert verdict.flake_check != NOT_FLAKY
        assert verdict.flaky is False
        assert verdict.detection_possible is False
        assert verdict.not_run is True
        assert verdict.repetitions == len(outcomes)
        assert verdict.check() == [], verdict.check()


def test_the_observed_count_wins_over_the_requested_count() -> None:
    """The control in the other direction: more observations than requested.

    ``flake_verdict(1, ("pass", "fail"))`` sees TWO observed outcomes, so the
    check genuinely ran and the verdict is ``flaky_detected`` — the requested
    count is recorded as a mismatch but never used as the evidence.
    """
    verdict = flake_verdict(1, (OUTCOME_PASS, OUTCOME_FAIL))
    assert verdict.flake_check == "flaky_detected"
    assert verdict.flaky is True
    assert verdict.repetitions == 2
    assert verdict.requested_repetitions == 1
    assert verdict.check() == [], verdict.check()


def test_two_or_more_observations_is_the_threshold_and_it_is_derived() -> None:
    """The control that makes the ``not_run`` assertions above mean something.

    The same test with the same runner at two repetitions must reach a real
    verdict, so ``not_run`` cannot pass because the gate never fires.
    """
    stable = flake_verdict(2, (OUTCOME_PASS, OUTCOME_PASS))
    assert stable.flake_check == NOT_FLAKY
    assert stable.detection_possible is True
    assert stable.flaky is False
    assert stable.check() == [], stable.check()

    mixed = flake_verdict(2, (OUTCOME_PASS, OUTCOME_TIMEOUT))
    assert mixed.flake_check == "flaky_detected"
    assert mixed.flaky is True
    assert mixed.timed_out is True
    assert mixed.check() == [], mixed.check()

    # A consistent hang is consistently broken, not intermittently broken.
    always_hangs = flake_verdict(3, (OUTCOME_TIMEOUT,) * 3)
    assert always_hangs.flake_check == NOT_FLAKY
    assert always_hangs.flaky is False
    assert always_hangs.timed_out is True


def test_a_caller_that_reports_fewer_outcomes_than_it_requested_degrades_to_not_run() -> (
    None
):
    """Property 2: a CALLER bug must never manufacture evidence.

    Three shapes of the same defect, all of which a naive implementation would
    answer ``not_flaky`` (i.e. "stable"):

    * asked for 3, reported 1;
    * asked for 2, reported 0;
    * asked for 0, reported 1 (an unrunnable configuration that somehow
      produced an outcome).
    """
    # asked 3, reported 1 -> the single observed pass cannot support a verdict
    asked_three_got_one = flake_verdict(3, (OUTCOME_PASS,))
    assert asked_three_got_one.flake_check == NOT_RUN, (
        "a caller that reported 1 of 3 requested repetitions got a verdict "
        f"instead of 'not_run': {asked_three_got_one.flake_check!r}"
    )
    assert asked_three_got_one.flaky is False
    assert asked_three_got_one.detection_possible is False
    assert any("observed" in note for note in asked_three_got_one.notes), (
        f"the mismatch must be RECORDED, not silently resolved: "
        f"{list(asked_three_got_one.notes)!r}"
    )

    # asked 2, reported 0
    asked_two_got_none = flake_verdict(2, ())
    assert asked_two_got_none.flake_check == NOT_RUN
    assert asked_two_got_none.repetitions == 0
    assert asked_two_got_none.observed_outcomes == ()

    # asked 0, reported 1: the requested count is nonsense, the OBSERVED count
    # is the only thing that can be trusted.
    asked_zero_got_one = flake_verdict(0, (OUTCOME_PASS,))
    assert asked_zero_got_one.flake_check == NOT_RUN
    assert asked_zero_got_one.requested_repetitions == 0
    assert asked_zero_got_one.repetitions == 1

    # ...and the reported outcomes are preserved verbatim so a reviewer can
    # recompute the verdict from the receipt alone.
    assert asked_three_got_one.observed_outcomes == (OUTCOME_PASS,)
    assert asked_three_got_one.to_dict()["observed_outcomes"] == [OUTCOME_PASS]
    assert asked_three_got_one.to_dict()["requested_repetitions"] == 3
    assert asked_three_got_one.to_dict()["detection_possible"] is False


def test_attaching_a_not_run_evidence_receipt_never_sets_flaky_true() -> None:
    """The receipt written onto a ``VerificationResult`` must not lie either.

    ``attach_evidence`` projects the three-valued verdict onto the historical
    boolean. For ``not_run`` that projection is ``False`` — the value the field
    has always had for a single run — while ``flake_check`` on the SAME object
    says the check never ran. A consumer can therefore read one or the other,
    and cannot get the same answer from both unless the check really ran.
    """

    class _Result:
        target_test_passed = True
        regression_passed = True
        flaky = True  # a hostile pre-value: the projection must overwrite it

    result = _Result()
    verdict = flake_verdict(1, (OUTCOME_PASS,))
    returned = attach_evidence(result, verdict)

    assert returned.flaky is False, "the projection must overwrite a stale True"
    assert returned.flake_check == NOT_RUN
    assert returned.repetitions == 1
    assert returned.observed_outcomes == [OUTCOME_PASS]

    # The evidence payload carries the third state, so the object itself is not
    # the only place the distinction exists.
    payload = returned.flake_evidence
    assert payload["detection_possible"] is False
    assert payload["flake_check"] == NOT_RUN

    # The differential that makes the misreading visible: for `not_flaky` the
    # boolean and the verdict AGREE, and for `not_run` the boolean is the SAME
    # False while the verdict disagrees. A consumer reading `flaky` alone
    # cannot tell those two rows apart; a consumer reading `flake_check` can.
    agreement: Dict[str, bool] = {}
    for label, outcomes in (
        ("not_flaky", (OUTCOME_PASS, OUTCOME_PASS)),
        ("not_run", (OUTCOME_PASS,)),
    ):
        applied = attach_evidence(_Result(), flake_verdict(len(outcomes), outcomes))
        agreement[label] = applied.flake_check == (
            "flaky_detected" if applied.flaky else "not_flaky"
        )
    assert agreement["not_flaky"] is True, agreement
    assert agreement["not_run"] is False, (
        f"a not_run verdict projects to flaky=False, which a reader reads as "
        f"not_flaky: {agreement!r}"
    )


# ---------------------------------------------------------------------------
# 3 — the read-site scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakyReadSite:
    """One place in ``execution/`` that reads the ``flaky`` field."""

    module: str
    function: str
    lineno: int
    form: str
    is_write: bool

    @property
    def key(self) -> Tuple[str, str, str]:
        """A line-number-free identity, so reflowing a file cannot shift it."""
        return (self.module, self.function, self.form)

    def describe(self) -> str:
        return f"{self.module}::{self.function} (line {self.lineno}, {self.form})"


#: Modules that OWN the ``flake_check`` vocabulary. A ``flaky`` read inside
#: one of these is the gate's own projection of its three-valued verdict, which
#: is the definition of the correct consumer — but the projection itself is
#: pinned behaviourally by
#: :func:`test_the_verdict_projection_is_derived_from_flake_check` below, so the
#: exemption cannot hide a divergence.
OWNER_MODULES: FrozenSet[str] = frozenset({"flake_gate.py"})

#: Names whose presence in the SAME function marks a read as correctly paired.
PAIRED_NAMES: Tuple[str, ...] = ("flake_check", "detection_possible")

#: The WRITTEN allowlist. Every entry is a bare ``flaky`` read that does not
#: read ``flake_check`` in the same function. Each carries a reason and the
#: direction the read errs in, because "it is fine" is not an answer and
#: "it errs conservatively" is.
#:
#: ``direction`` is ``"refuse"`` when a wrong answer can only produce MORE
#: evidence or MORE refusal, and ``"gap"`` when the read carries the documented
#: misreading and is recorded as such rather than being quietly allowed.
ALLOWLIST: Dict[Tuple[str, str, str], Tuple[str, str]] = {
    ("verify.py", "_attach_structured_feedback", "attr.read"): (
        "refuse",
        "Attaches PARSEABLE FAILURE OBJECTS when the run was not clean. A false "
        "flaky only ADDS feedback to the model; a false not-flaky drops some. "
        "The read errs conservatively. It also runs BEFORE the flake gate is "
        "attached, so `result.flaky` here is the repetition loop's own "
        "`len(set(outcomes)) > 1`, not the gate's projection.",
    ),
    ("baseline_set.py", "_outcome_for", "getattr.read"): (
        "refuse",
        "`flaky=True` maps to the OUTCOME_FAIL label, so the read can only make "
        "the run look worse than it is. Part of an outcome LABEL, not a "
        "stability claim.",
    ),
    ("baseline_set.py", "classify_run", "getattr.read"): (
        "refuse",
        "Popsulates `BaselineVerdict.flaky`, defaulting to None (absent) rather "
        "than to False. The decision that consumes it is the next entry; this "
        "one only moves the value.",
    ),
    ("baseline_set.py", "blocks_success", "attr.read"): (
        "gap",
        "KNOWN GAP, recorded not fixed: this reads `self.flaky` (an "
        "Optional[bool] taken from the result) and never consults `flake_check`. "
        "A `flake_check='not_run'` verdict therefore arrives as `flaky=False` "
        "and does NOT block, which is the documented misreading. It is fail-"
        "open only in the `not_run` case: `flaky=True` still blocks, and every "
        "OTHER term in `blocks_success` is fail-closed. See execution/AGENTS.md "
        "W2 handoff section 8.2 — the fix is `flake_check` awareness, which is "
        "a behaviour change to a fail-closed gate and was not applied here.",
    ),
    ("baseline_set.py", "to_dict", "attr.read"): (
        "refuse",
        "A receipt: it reports the value alongside `blocks_success`, which is "
        "the computed answer. Nothing here decides anything.",
    ),
    ("verification_gate.py", "_baseline_verdict", "getattr.read"): (
        "gap",
        "KNOWN GAP, recorded not fixed: renders the harness's own mint "
        "condition, so it cannot DISAGREE with the mint, but for a "
        "`flake_check='not_run'` result it renders the reason 'the target test "
        "passed, the full suite passed, and the target was not flaky' — which "
        "says 'not flaky' about a check that never ran. The rung is documented "
        "as recorded-and-never-applied, and `plan_fold` only ever CLEARS "
        "`target_test_passed`, so it cannot widen a claim. The misreading is in "
        "the WORDING of a receipt.",
    ),
    ("verification_gate.py", "_baseline_snapshot", "getattr.read"): (
        "refuse",
        "A pre-fold SNAPSHOT of the baseline booleans, taken so the fold can be "
        "explained after the fact. It records what was there; it decides "
        "nothing.",
    ),
    ("verification_intelligence.py", "run_verification", "attr.read"): (
        "refuse",
        "`bool(final_result.flaky) if final_result else True` — the ABSENT case "
        "defaults to True, i.e. to blocking. Fail-closed by construction.",
    ),
    ("verification_intelligence.py", "to_dict", "attr.read"): (
        "refuse",
        "A receipt field, reported beside `target_test_passed` and "
        "`regression_passed`. Nothing here decides anything.",
    ),
}


def _production_sources() -> List[Tuple[str, Path]]:
    """Return ``(module name, path)`` for every non-test module of execution/."""
    here = Path(__file__).resolve().parent
    return [
        (path.name, path)
        for path in sorted(here.glob("*.py"))
        if not path.name.startswith("test_")
    ]


def _read_sites(tree: ast.AST) -> List[FlakyReadSite]:
    """Return every ``flaky`` read site in one parsed module.

    Three syntactic forms, because the field is read three ways in this package:
    a plain ``obj.flaky`` attribute, a ``getattr(obj, "flaky", ...)`` call, and
    a ``obj["flaky"]`` subscript. Missing any one of them would leave a real
    read site unpinned, which is the failure mode this pin exists to prevent.
    """
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    store_ids = set()
    for node in ast.walk(tree):
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute):
                    store_ids.add(id(sub))

    def enclosing_function(node: ast.AST) -> str:
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
        return "<module>"

    sites: List[FlakyReadSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "flaky":
            is_write = id(node) in store_ids
            sites.append(
                FlakyReadSite(
                    module="",
                    function=enclosing_function(node),
                    lineno=node.lineno,
                    form="attr.WRITE" if is_write else "attr.read",
                    is_write=is_write,
                )
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "flaky"
        ):
            sites.append(
                FlakyReadSite(
                    module="",
                    function=enclosing_function(node),
                    lineno=node.lineno,
                    form="getattr.read",
                    is_write=False,
                )
            )
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "flaky"
        ):
            # A subscript read cannot be an assignment target unless it is
            # augmented/deleted; both of those are handled as writes above.
            sites.append(
                FlakyReadSite(
                    module="",
                    function=enclosing_function(node),
                    lineno=node.lineno,
                    form="subscript.read",
                    is_write=False,
                )
            )
    return sites


def _function_bodies(tree: ast.AST) -> Dict[str, ast.AST]:
    """Return ``{function name: the node}`` for every function in the module."""
    bodies: Dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies.setdefault(node.name, node)
    return bodies


def _names_read_in(node: ast.AST) -> FrozenSet[str]:
    """Return every attribute name READ anywhere inside ``node``."""
    return frozenset(
        child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
    )


def enumerate_flaky_read_sites() -> List[FlakyReadSite]:
    """Enumerate every ``flaky`` read site in ``execution/``, module attributed."""
    found: List[FlakyReadSite] = []
    for module_name, path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for site in _read_sites(tree):
            found.append(
                FlakyReadSite(
                    module=module_name,
                    function=site.function,
                    lineno=site.lineno,
                    form=site.form,
                    is_write=site.is_write,
                )
            )
    return sorted(found, key=lambda item: (item.module, item.lineno, item.form))


def test_every_flaky_read_site_is_paired_or_allowlisted() -> None:
    """Property 3: zero UNLISTED ``flaky`` read sites in ``execution/``.

    A site passes if either

    * it lives in a module that OWNS ``flake_check`` (:data:`OWNER_MODULES`) —
      the gate's own projection — or
    * the same function also reads ``flake_check`` / ``detection_possible``, so
      the reader has the three-valued answer in hand — or
    * it appears in :data:`ALLOWLIST` with a reason and a direction.

    Writes are excluded: setting the field is what ``attach_evidence`` is FOR,
    and the projection it writes is pinned behaviourally below. But a WRITE is
    still enumerated, so a new write site cannot slip past unnoticed.
    """
    all_sites = enumerate_flaky_read_sites()
    reads = [site for site in all_sites if not site.is_write]
    writes = [site for site in all_sites if site.is_write]
    assert writes, (
        "no WRITE site was found; `attach_evidence` setting the field is what "
        "the module is for, so a scan that found none has stopped working"
    )
    assert {site.module for site in writes} <= {"flake_gate.py"}, (
        f"a module outside flake_gate writes the flaky field: {writes!r}. Only "
        "the gate's own projection may write it, or two authorities are setting "
        "the same boolean"
    )

    unlisted: List[str] = []
    for module_name, path in _production_sources():
        if module_name in OWNER_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bodies = _function_bodies(tree)
        for site in reads:
            if site.module != module_name:
                continue
            if site.key in ALLOWLIST:
                continue
            body = bodies.get(site.function)
            if body is not None and _names_read_in(body) & set(PAIRED_NAMES):
                continue
            unlisted.append(site.describe())

    assert not unlisted, (
        "a bare `.flaky` read with no `flake_check` beside it and no written "
        "reason is the bug this pin prevents:\n  "
        + "\n  ".join(unlisted)
        + "\nEither pair the read with `flake_check` / `detection_possible`, or "
        "add it to execution/test_flake_gate_pins.py::ALLOWLIST with a reason "
        "and a direction."
    )
    # Non-vacuity: the scan must actually be finding sites. A scan that found
    # nothing would make this test permanently green and worthless.
    assert len(reads) >= len(ALLOWLIST), (
        f"the scan found {len(reads)} read sites but the allowlist has "
        f"{len(ALLOWLIST)} entries; either the scan stopped working or the "
        "allowlist grew entries that no longer exist"
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still correspond to a real read site.

    A stale entry is not harmless: it is a permission to keep reading ``flaky``
    somewhere that no longer exists, and the day the function comes back it
    inherits the exemption silently.
    """
    live = {site.key for site in enumerate_flaky_read_sites()}
    stale = [key for key in ALLOWLIST if key not in live]
    assert not stale, (
        f"allowlist entries no longer match any read site: {stale!r}. Remove them "
        "or re-point them; an unused exemption is a permission with no subject"
    )
    for key, (direction, reason) in ALLOWLIST.items():
        assert direction in ("refuse", "gap"), (key, direction)
        assert len(reason) >= 60, (
            f"{key!r} carries a reason too short to be a reason: {reason!r}"
        )
        if direction == "gap":
            assert "KNOWN GAP" in reason, (
                f"{key!r} is marked a gap and must say so out loud: {reason!r}"
            )


def test_the_verdict_projection_is_derived_from_flake_check() -> None:
    """Pin the OWNER module's projection so its exemption cannot hide a bug.

    ``FlakeVerdict.flaky`` must be exactly ``flake_check == FLAKE_DETECTED`` —
    nothing else. This is asserted over the whole closed vocabulary, so an
    added value has to decide what it projects to.
    """
    from execution import flake_gate

    expected = {
        flake_gate.FLAKE_DETECTED: True,
        flake_gate.NOT_FLAKY: False,
        flake_gate.NOT_RUN: False,
    }
    for value, want in expected.items():
        outcomes = {
            flake_gate.FLAKE_DETECTED: (OUTCOME_PASS, OUTCOME_FAIL),
            flake_gate.NOT_FLAKY: (OUTCOME_PASS, OUTCOME_PASS),
            flake_gate.NOT_RUN: (OUTCOME_PASS,),
        }[value]
        verdict = flake_verdict(len(outcomes), outcomes)
        assert verdict.flake_check == value, (value, verdict.flake_check)
        assert verdict.flaky is want, (value, verdict.flaky)
        assert verdict.to_dict()["flaky"] is want
    assert set(expected) == set(FLAKE_CHECK_VALUES)


def test_the_execution_verifier_surfaces_the_three_valued_verdict_not_just_the_bool() -> (
    None
):
    """``verify()`` must publish ``flake_check`` whenever it publishes ``flaky``.

    Proved WITHOUT a daemon: the seam helper is the one that decides whether
    the receipt is produced, so requiring it to be callable and to return a
    dict carrying both keys is the Docker-free half of the claim. The Docker
    half is ``execution/test_verification_honesty_pins.py``.
    """
    from execution import verify as verify_module

    rows = verify_module._flake_gate_rungs(
        ".", "tests/test_x.py", 2, [OUTCOME_PASS, OUTCOME_PASS], {"post_fix_reruns": 2}
    )
    assert isinstance(rows, tuple) and len(rows) == 2, rows
    verdict, evidence = rows
    assert verdict.flake_check == NOT_FLAKY
    assert verdict.flaky is False
    assert evidence["verdict"]["flake_check"] == NOT_FLAKY
    assert evidence["verdict"]["repetitions"] == 2
    assert evidence["policy"]["repetitions"] == 2
    # The rendered receipt names the verdict in words, so a reader of a trace
    # row is not left with a bare boolean.
    assert "flake_check=not_flaky" in evidence["rendered"], evidence["rendered"]
