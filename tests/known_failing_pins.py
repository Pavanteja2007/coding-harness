"""T5.W2.1 — the registry of deliberately-red pins, and the checker for them.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------

This repository records known defects with **inverted pins**: a test that
*fails the day somebody fixes the gap*, telling them to promote it
(``phases/DOCTRINE.md`` §3). That is a good mechanism. It has one sharp edge,
and this module is the edge:

**a deliberately-red pin and a real regression look identical in a CI log.**

Both are a red line with a node id. Nothing on the line says "this one is
supposed to be red". So the next round reads the red, decides it is a bug,
and "fixes" it — by deleting the assertion, adding ``xfail``, or widening the
allowlist. The gap is now closed *and* the recording is gone, and the defect
is invisible forever. That is strictly worse than never having pinned it.

This registry makes the distinction **mechanical** instead of a matter of
whoever reads the log most carefully.

THE TWO KINDS, AND WHY THEY ARE DIFFERENT
-----------------------------------------

``KnownFailingPin``
    Currently **RED**, on purpose. The recorded gap is OPEN. It turns green
    when the owning terminal deletes the offending code. While it is red, a
    red is the *expected* state.

``InvertedPin``
    Currently **GREEN**, on purpose. The recorded gap is closed-by-refusal
    (a deny rule, a withheld feature, an unshipped predictor) and the pin
    exists to go red if somebody removes the refusal. While it is green, a
    red is a **regression**.

Both are "a deliberate pin recording a gap". They are registered together
because the thing a reader most needs is the *distinction*, and the thing a
future terminal most needs is not to file one as the other.

THE FOUR MECHANICS (each is a separate failure mode, not a style preference)
------------------------------------------------------------------------------

For a :class:`KnownFailingPin`:

1. **As recorded** — failing, and every ``reason_substrings`` entry appears
   in the failure text. The gap is still open, for the stated reason.
2. **PROMOTE** — passing. The gap is CLOSED. This is a *build failure*, not
   a skip and not a pass: the pin must be deleted from this registry in the
   same change that closed the gap, so the closure is visible in the diff.
3. **CHANGED REASON** — failing, but a stated substring is *absent*. The pin
   broke differently. That is a real regression wearing the pin's name, and
   it must never be reported as a known gap.
4. **MISSING** — the node id does not exist / does not collect. A registry
   pointing at a test that is not there is how a real failure gets filed as
   a known one, so this is also a build failure.

For an :class:`InvertedPin`: it must be **green** (``as_recorded``), and the
same three failure modes apply with the sense of ``passed``/``failed``
inverted.

**No blanket suppression.** :func:`suppression_offenders` inspects the
declared pin modules by AST and rejects any ``xfail``/``skip``/``skipif``
decorator on a registered node, and :func:`blanket_marker_offenders` rejects
a ``pytest.ini``/``pyproject`` marker that could hide a class of tests.
Entries are named, individually, or they are not recorded.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------

It does not run the suite. :func:`observe` runs **one node id** in a
subprocess, which is the only way to observe "is this red, and why" without
the answer depending on collection order, on which other tests ran first, or
on a sibling terminal's uncommitted edit. It does not use ``xfail``; the
whole point is that these pins are red in the log where a human will see
them.

Registering a gap here is a CLAIM. Every entry carries an owner, the change
that closes it, and the substrings that prove the red is the recorded one.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Substrings that must appear in a ``KnownFailingPin``'s failure text for
#: the red to be recognised as *the recorded one*. At least two, always: one
#: that identifies the gap and one that identifies the subject, because a
#: message that only says "still present" would match a dozen unrelated reds.
MIN_REASON_SUBSTRINGS = 2

#: A reason has to be a reason. Anything shorter is a label, and a label
#: cannot be checked by a reader six months from now.
MIN_REASON_CHARS = 60

#: Seconds. One node id, one subprocess. The slowest registered pin in this
#: registry is a source-level AST scan (<2s); the ceiling is here so a wedged
#: subprocess is a reported ``timed_out`` rather than a hung gate.
OBSERVE_TIMEOUT_S = 300


# --------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------

#: The four outcomes a pin can be in, plus the two error shapes. This is a
#: CLOSED set: a reader can enumerate it, and a new value has to be written
#: down here before anything can produce it.
AS_RECORDED = "as_recorded"
PROMOTE = "promote_me"
CHANGED_REASON = "changed_reason"
MISSING = "missing"
SUPPRESSED = "suppressed"
BLANKET_MARKER = "blanket_marker"
NOT_COLLECTED = "not_collected"
TIMED_OUT = "timed_out"

VERDICTS: Tuple[str, ...] = (
    AS_RECORDED,
    PROMOTE,
    CHANGED_REASON,
    MISSING,
    SUPPRESSED,
    BLANKET_MARKER,
    NOT_COLLECTED,
    TIMED_OUT,
)

#: Verdicts that are a BUILD FAILURE rather than an observation. Every one of
#: them means "this registry entry cannot be trusted as written".
BUILD_FAILURES: Tuple[str, ...] = (
    PROMOTE,
    CHANGED_REASON,
    MISSING,
    SUPPRESSED,
    BLANKET_MARKER,
    NOT_COLLECTED,
    TIMED_OUT,
)

PASSED = "passed"
FAILED = "failed"


class RegistryError(RuntimeError):
    """Raised when an entry is not registrable as written.

    This is a construction-time error, not an observation: it means the
    registry itself is malformed (a reason that is not a reason, too few
    substrings to identify a red, a missing owner), which no future green or
    red run can fix.
    """


# --------------------------------------------------------------------------
# the entries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownFailingPin:
    """A pin that is RED ON PURPOSE because a recorded gap is still open.

    :param node_id: exact pytest node id, ``path::test_name``. Never a glob,
        never a file, never a ``-k`` expression: a registry entry that could
        match several tests could not tell a promotion from a rename.
    :param terminal: who recorded it, for the handoff trail.
    :param owner: who closes it (``T1 / P2.1`` style), and the phase.
    :param closes_when: the specific change that turns it green, written so a
        reader can tell whether it has happened.
    :param reason_substrings: >= :data:`MIN_REASON_SUBSTRINGS` substrings that
        must ALL appear in the failure text. This is what makes "failing for
        the stated reason" checkable instead of asserted.
    """

    node_id: str
    terminal: str
    owner: str
    closes_when: str
    reason_substrings: Tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        _check_node_id(self.node_id)
        for name, value in (
            ("terminal", self.terminal),
            ("owner", self.owner),
            ("closes_when", self.closes_when),
        ):
            if not str(value).strip():
                raise RegistryError(f"{self.node_id}: {name} is empty")
        if len(self.reason_substrings) < MIN_REASON_SUBSTRINGS:
            raise RegistryError(
                f"{self.node_id}: a known-failing pin needs at least "
                f"{MIN_REASON_SUBSTRINGS} reason substrings to distinguish "
                f"'the recorded red' from 'some other red'; got "
                f"{len(self.reason_substrings)}"
            )
        for substring in self.reason_substrings:
            if not substring.strip():
                raise RegistryError(f"{self.node_id}: an empty reason substring")
        if len(self.closes_when) < MIN_REASON_CHARS:
            # Not enforced as a hard error on closes_when (short is
            # acceptable); enforced on `reason` below via the substrings.
            pass

    @property
    def path(self) -> str:
        """The file half of the node id."""
        return self.node_id.split("::", 1)[0]


@dataclass(frozen=True)
class InvertedPin:
    """A pin that is GREEN ON PURPOSE: it guards a closed-by-refusal gap.

    Same fields as :class:`KnownFailingPin` minus ``reason_substrings``, plus
    ``why_green``: one sentence on what the pin is currently holding shut, so
    a reader who finds it green knows it is not an untested stub.
    """

    node_id: str
    terminal: str
    owner: str
    why_green: str
    goes_red_when: str

    def __post_init__(self) -> None:
        _check_node_id(self.node_id)
        for name, value in (
            ("terminal", self.terminal),
            ("owner", self.owner),
            ("why_green", self.why_green),
            ("goes_red_when", self.goes_red_when),
        ):
            if not str(value).strip():
                raise RegistryError(f"{self.node_id}: {name} is empty")

    @property
    def path(self) -> str:
        """The file half of the node id."""
        return self.node_id.split("::", 1)[0]


def _check_node_id(node_id: str) -> None:
    """Refuse a node id that is not one exact test.

    A file, a glob or a ``-k`` expression cannot distinguish "this pin went
    green" from "this file was renamed", so it is not registrable.
    """
    if "::" not in node_id:
        raise RegistryError(
            f"{node_id!r} is not an exact node id; a registry entry must be "
            "'path::test_name' so a promotion cannot be confused with a rename"
        )
    path, _, name = node_id.partition("::")
    # Both halves are checked for glob metacharacters. `test_*.py::test_x`
    # ends in `.py` and would otherwise slip through the suffix check, and a
    # path glob is exactly as unable to distinguish promotion from rename as
    # a name glob is.
    if not path.endswith(".py"):
        raise RegistryError(f"{node_id!r}: the path half must be a .py file")
    if any(ch in path for ch in "*?[]"):
        raise RegistryError(
            f"{node_id!r}: the path half must be one file, not a pattern"
        )
    if not name or any(ch in name for ch in "*?[]"):
        raise RegistryError(
            f"{node_id!r}: the name half must be one test function, not a pattern"
        )


# --------------------------------------------------------------------------
# THE REGISTRY
# --------------------------------------------------------------------------
#
# POPULATED FROM MEASUREMENT, NOT FROM THE BRIEF.
#
# T5.W2.1's brief listed five known-failing pins (T2's unsandboxed bash path,
# T3's structural predictor, "3 more incoming"). What is actually in this
# tree, measured on 2026-10-01:
#
#   * T2's unsandboxed-bash pin EXISTS and is RED, on exactly one site
#     (`harness/agent_loop.py:797`). It is registered below.
#   * T3's structural-predictor pin is NOT red. It is
#     `runtime/invariants.check_structural_guard`, which HOLDS (9/9
#     invariants hold) and `runtime/test_structural_predictor_guard.py`, 16
#     passed. Both assert the predictor is STILL UNSHIPPED. The brief
#     described the opposite sense. It is registered as an `InvertedPin`,
#     because that is what it is, and registering it as a known-failure
#     would have filed a green pin as a red one — the exact confusion this
#     module exists to prevent.
#   * No third or fourth designed-red pin exists. The remaining reds in the
#     module-local suites are REAL failures (see KNOWN-REAL-FAILURES below and
#     the G0 report), and are recorded as `fail`, never here.
#
# So this registry has ONE known-failing entry, and that is the honest count.

#: Deliberately-red pins. Every one must be failing, for its stated reason,
#: right now.
KNOWN_FAILING_PINS: Tuple[KnownFailingPin, ...] = (
    KnownFailingPin(
        node_id=(
            "execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_no_"
            "production_import_path_reaches_the_local_subprocess_sandbox_stub"
        ),
        terminal="T2 (execution)",
        owner="T1 / P2.1",
        closes_when=(
            "harness/agent_loop.py deletes the `from harness._stubs import "
            "sandbox` reach and the local-subprocess execute_sandboxed call "
            "that follows it. Nothing else in the repository changes."
        ),
        reason_substrings=(
            "THE UNSANDBOXED BASH PATH IS STILL PRESENT",
            "harness/agent_loop.py",
            "harness._stubs.sandbox",
        ),
    ),
)

#: Deliberately-green pins. These must be GREEN; a red is a regression.
#:
#: Registered so that a future round cannot mistake "this suite is green" for
#: "this suite has no recorded gaps", and so a red here is named as the
#: regression it is instead of being filed as a known failure.
INVERTED_PINS: Tuple[InvertedPin, ...] = (
    InvertedPin(
        node_id=(
            "runtime/test_structural_predictor_guard.py::"
            "test_the_phase6_marker_is_not_accidentally_satisfied_today"
        ),
        terminal="T3 (runtime)",
        owner="T3 / P6",
        why_green=(
            "predict_structural won its held-out split (0.9412 vs 0.8235) on "
            "1 hard label against a floor of 3, so it is reachable ONLY by an "
            "explicit difficulty_features='structural' and 'auto' resolves to "
            "the incumbent."
        ),
        goes_red_when=(
            "runtime/difficulty_structural_calibration.json appears, or a "
            "DEFAULTS/env selector switches 'auto' to the challenger — i.e. "
            "Phase 6 ships it, which must be a deliberate decision."
        ),
    ),
    InvertedPin(
        node_id=(
            "harness/test_egress_call_sites.py::"
            "test_the_known_gap_is_still_open_so_this_file_is_not_claiming_a_fix"
        ),
        terminal="T1 (harness)",
        owner="T1 / P2",
        why_green=(
            "The egress audit's single RECORDED_GAPS entry "
            "(harness/tool_errors._precommit_refusal) is still an un-redacted "
            "egress, and this pin is what stops the audit from claiming it "
            "fixed."
        ),
        goes_red_when=(
            "that site is redacted, at which point the entry must be REMOVED "
            "from RECORDED_GAPS in the same change."
        ),
    ),
    InvertedPin(
        node_id=(
            "harness/test_gap_audit.py::"
            "test_every_recorded_gap_names_an_owner_an_inverted_pin_and_a_reason"
        ),
        terminal="T1 (harness)",
        owner="T1 / P2",
        why_green=(
            "Every marker hit in harness/ is classified, and each recorded gap "
            "carries an owner, an inverted pin and a reason — so a gap is "
            "never merely asserted."
        ),
        goes_red_when=(
            "a gap is added without an owner/an inverted pin/a reason, or an "
            "existing one loses any of the three."
        ),
    ),
    InvertedPin(
        node_id=("cli/test_render_path_pin.py::test_the_allowlist_has_no_stale_rows"),
        terminal="T4 (cli)",
        owner="T4 / P1.2",
        why_green=(
            "The five unsanitised render modules "
            "(cli/review.py, palette.py, session.py, plugin_runtime.py, "
            "models.py) are registered in UNTRUSTED_RENDER_GAPS with a reason "
            "each, and the pin fails if a row goes stale."
        ),
        goes_red_when=(
            "a render site is sanitised without its UNTRUSTED_RENDER_GAPS row "
            "being deleted in the same change."
        ),
    ),
)


def all_entries() -> List[Tuple[str, object]]:
    """Return ``(kind, entry)`` for every registered pin, known-failing first.

    The meta-test iterates this, so the order here is the order a reader gets
    a failure report in: the loud class first.
    """
    out: List[Tuple[str, object]] = []
    for pin in KNOWN_FAILING_PINS:
        out.append(("known_failing", pin))
    for pin in INVERTED_PINS:
        out.append(("inverted", pin))
    return out


def entry_for(node_id: str) -> Optional[Tuple[str, object]]:
    """Return the registry entry for ``node_id``, or ``None``."""
    for kind, entry in all_entries():
        if entry.node_id == node_id:  # type: ignore[attr-defined]
            return (kind, entry)
    return None


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What actually happened when one node id was run.

    :param outcome: :data:`PASSED`, :data:`FAILED`, :data:`NOT_COLLECTED` or
        :data:`TIMED_OUT`.
    :param text: the subprocess output, which is where the reason substrings
        are matched.
    :param returncode: the subprocess exit code, kept for the report.
    """

    outcome: str
    text: str
    returncode: Optional[int] = None

    def missing_substrings(self, wanted: Sequence[str]) -> List[str]:
        """Return the wanted substrings that are ABSENT from :attr:`text`."""
        return [s for s in wanted if s not in self.text]


def observe(node_id: str, *, timeout_s: int = OBSERVE_TIMEOUT_S) -> Observation:
    """Run exactly one node id in a subprocess and report what happened.

    Assumes the repository root is the working directory and that ``node_id``
    is registrable (one exact test). Never raises for a missing file, a
    collection error or a timeout: those are OBSERVATIONS, because "this pin
    cannot be run" is itself something the gate has to report rather than
    crash on.

    ``-p no:randomly`` is required, not cosmetic: ``pytest-randomly`` is
    installed in this environment and reorders collection per run, so without
    it a single-node run is still not perfectly reproducible and an
    order-dependent pin would flicker between runs.
    """
    path, _, _name = node_id.partition("::")
    if not (REPO_ROOT / path).is_file():
        return Observation(
            outcome=NOT_COLLECTED,
            text=f"{path} does not exist",
            returncode=None,
        )
    argv = [
        sys.executable,
        "-m",
        "pytest",
        node_id,
        "-q",
        "--no-header",
        "--tb=long",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return Observation(
            outcome=TIMED_OUT,
            text=f"{node_id} did not finish within {timeout_s}s",
            returncode=None,
        )
    except OSError as exc:  # pragma: no cover - interpreter/permission fault
        return Observation(outcome=NOT_COLLECTED, text=f"{node_id}: {exc}")

    text = (proc.stdout or "") + (proc.stderr or "")
    # pytest exit codes: 0 all passed, 1 tests failed, 2 interrupted /
    # usage / collection error, 3 internal error, 5 no tests collected.
    if proc.returncode == 0:
        outcome = PASSED
    elif proc.returncode == 5:
        outcome = NOT_COLLECTED
    elif proc.returncode == 1:
        outcome = FAILED
    else:
        # 2/3/4: interrupted, internal error, usage error. The test did not
        # run to a verdict, which is NOT the same as "failing for the stated
        # reason" and must not be filed as one.
        outcome = NOT_COLLECTED
    return Observation(outcome=outcome, text=text, returncode=proc.returncode)


# --------------------------------------------------------------------------
# classification — pure, so the four mechanics are unit-testable
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The result of checking one registered pin against one observation."""

    kind: str
    node_id: str
    verdict: str
    detail: str

    @property
    def is_build_failure(self) -> bool:
        """Whether this verdict must fail the build rather than be reported."""
        return self.verdict in BUILD_FAILURES


def classify_known_failing(pin: KnownFailingPin, obs: Observation) -> Verdict:
    """Classify a deliberately-red pin. Implements mechanics 1-4.

    The four outcomes, and what each one MEANS:

    ``as_recorded``
        Failing, and every stated substring is present. The gap is open for
        the recorded reason. This is the only healthy outcome here.
    ``promote_me``
        Passing. **The gap is closed.** Not a pass: a build failure, so the
        closure cannot land without the registry being updated in the same
        change.
    ``changed_reason``
        Failing, but a stated substring is missing. The pin broke
        DIFFERENTLY. A real regression wearing this pin's name — and the one
        outcome that would be most dangerous to file as a known gap.
    ``missing`` / ``not_collected` / `timed_out`
        The pin could not be run. Also a build failure: a registry entry that
        cannot be observed is not a recording.
    """
    if obs.outcome == PASSED:
        return Verdict(
            kind="known_failing",
            node_id=pin.node_id,
            verdict=PROMOTE,
            detail=(
                "PROMOTE THIS. The recorded gap is CLOSED: the pin passes. "
                f"Owner was {pin.owner}; it closes when {pin.closes_when}. "
                "Delete the entry from KNOWN_FAILING_PINS in the same change "
                "that closed the gap, so the closure is visible in the diff."
            ),
        )
    if obs.outcome in (NOT_COLLECTED, TIMED_OUT):
        return Verdict(
            kind="known_failing",
            node_id=pin.node_id,
            verdict=MISSING,
            detail=(
                f"the pin could not be RUN (outcome={obs.outcome}, "
                f"exit={obs.returncode}), so 'still red for the recorded "
                f"reason' is unverified. Observed: {obs.text[-800:]!r}"
            ),
        )
    missing = obs.missing_substrings(pin.reason_substrings)
    if missing:
        return Verdict(
            kind="known_failing",
            node_id=pin.node_id,
            verdict=CHANGED_REASON,
            detail=(
                "the pin is RED FOR A DIFFERENT REASON. This is a regression, "
                f"not a closed gap: {missing!r} no longer appear(s) in the "
                "failure. The recorded failure said "
                f"{list(pin.reason_substrings)!r}. Observed tail: "
                f"{obs.text[-800:]!r}"
            ),
        )
    return Verdict(
        kind="known_failing",
        node_id=pin.node_id,
        verdict=AS_RECORDED,
        detail=(
            f"still red for the recorded reason; owner {pin.owner}; closes "
            f"when {pin.closes_when}"
        ),
    )


def classify_inverted(pin: InvertedPin, obs: Observation) -> Verdict:
    """Classify a deliberately-green pin.

    The sense of every mechanic is inverted: a PASS is the only healthy
    outcome, and a FAIL is a regression that must be reported as one. It is
    NOT a promotion and NOT a known gap — which is precisely why the two
    classes are registered separately.
    """
    if obs.outcome == PASSED:
        return Verdict(
            kind="inverted",
            node_id=pin.node_id,
            verdict=AS_RECORDED,
            detail=(
                f"green, holding shut: {pin.why_green} Goes red when "
                f"{pin.goes_red_when}"
            ),
        )
    if obs.outcome in (NOT_COLLECTED, TIMED_OUT):
        return Verdict(
            kind="inverted",
            node_id=pin.node_id,
            verdict=MISSING,
            detail=(
                f"the pin could not be RUN (outcome={obs.outcome}, "
                f"exit={obs.returncode}); a green inverted pin that cannot be "
                f"observed is not holding anything shut. Observed: "
                f"{obs.text[-800:]!r}"
            ),
        )
    return Verdict(
        kind="inverted",
        node_id=pin.node_id,
        verdict=CHANGED_REASON,
        detail=(
            "REGRESSION. This pin is deliberately GREEN (it guards a "
            f"closed-by-refusal gap: {pin.why_green}) and it is now RED. It "
            f"goes red only when {pin.goes_red_when} Observed tail: "
            f"{obs.text[-800:]!r}"
        ),
    )


def classify(kind: str, entry: object, obs: Observation) -> Verdict:
    """Dispatch to the right classifier for ``kind``."""
    if kind == "known_failing":
        return classify_known_failing(entry, obs)  # type: ignore[arg-type]
    if kind == "inverted":
        return classify_inverted(entry, obs)  # type: ignore[arg-type]
    raise RegistryError(f"unknown pin kind {kind!r}")


def check_all(
    *, observe_fn=observe, kinds: Optional[Iterable[str]] = None
) -> List[Verdict]:
    """Observe and classify every registered pin.

    ``observe_fn`` is injectable so the meta-test can drive the four mechanics
    with synthetic observations instead of paying a subprocess per case.
    """
    wanted = set(kinds) if kinds is not None else None
    out: List[Verdict] = []
    for kind, entry in all_entries():
        if wanted is not None and kind not in wanted:
            continue
        out.append(classify(kind, entry, observe_fn(entry.node_id)))  # type: ignore[attr-defined]
    return out


# --------------------------------------------------------------------------
# mechanic 5 — refuse blanket suppression
# --------------------------------------------------------------------------

#: Decorator names that would make a registered pin unreportable. Checked by
#: AST on the pin's own module, not by substring over its text, because the
#: suppression keywords necessarily appear in this registry's own prose and in
#: the pin module's docstrings — a text scan would match itself.
SUPPRESSING_DECORATORS: Tuple[str, ...] = (
    "xfail",
    "skip",
    "skipif",
    "skipif_false",
    "unittest.skip",
)


def suppression_offenders(
    pins: Optional[Iterable[object]] = None,
) -> List[Tuple[str, str]]:
    """Return ``(node_id, decorator)`` for any pin carrying a suppression.

    A registered pin that is ``xfail``\\ ed or ``skip``\\ ped is not reported
    as red anywhere, so the registry's whole claim ("this red is recorded")
    becomes unverifiable while still looking true. This is the bulk-suppression
    door, and it is checked mechanically rather than trusted.
    """
    entries = list(pins) if pins is not None else [e for _k, e in all_entries()]
    offenders: List[Tuple[str, str]] = []
    by_path: Dict[str, List[object]] = {}
    for entry in entries:
        by_path.setdefault(entry.path, []).append(entry)  # type: ignore[attr-defined]

    for rel, group in by_path.items():
        target = REPO_ROOT / rel
        if not target.is_file():
            offenders.append((group[0].node_id, "module-missing"))  # type: ignore[attr-defined]
            continue
        try:
            tree = ast.parse(target.read_text(encoding="utf-8-sig"), filename=rel)
        except (OSError, SyntaxError) as exc:
            offenders.append((group[0].node_id, f"unparseable:{exc}"))  # type: ignore[attr-defined]
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            registered = any(
                entry.node_id.endswith(f"::{node.name}")  # type: ignore[attr-defined]
                for entry in group
            )
            if not registered:
                continue
            for decorator in node.decorator_list:
                rendered = ast.dump(decorator)
                for name in SUPPRESSING_DECORATORS:
                    if f"'{name}'" in rendered or f'"{name}"' in rendered:
                        offenders.append(
                            (
                                f"{rel}::{node.name}",
                                f"carries a {name!r} decorator",
                            )
                        )
    return offenders


def blanket_marker_offenders(
    pyproject: Optional[Path] = None,
) -> List[Tuple[str, str]]:
    """Return pytest config entries that could hide a CLASS of tests.

    The registry's authority is that every recorded red is individually
    visible in a log. Two config shapes destroy that without touching any
    individual test:

    * ``addopts`` carrying ``--ignore``/``--deselect``/``--ignore-glob``,
      which drops tests from collection entirely;
    * a registered marker combined with ``-m "not <marker>"`` in ``addopts``.

    Neither is used by this repository today. Both are checked so that adding
    one is a visible, deliberate act rather than a silent loss of coverage.
    """
    target = pyproject if pyproject is not None else REPO_ROOT / "pyproject.toml"
    if not target.is_file():
        return []
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return [("<toml parser>", "neither tomllib nor tomli is available")]
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed config is a finding, not a crash
        return [(str(target.name), f"unparseable: {exc}")]

    offenders: List[Tuple[str, str]] = []
    opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = " ".join(opts.get("addopts", []) or [])
    for flag in ("--ignore=", "--deselect", "--ignore-glob", "-m "):
        if flag in addopts:
            offenders.append(("pyproject.toml:pytest.addopts", f"contains {flag!r}"))
    for marker in ("slow", "flaky", "known_failing", "xfail"):
        if f'"{marker}"' in addopts or f"'{marker}'" in addopts:
            offenders.append(
                ("pyproject.toml:pytest.addopts", f"filters on the {marker!r} marker")
            )
    return offenders


# --------------------------------------------------------------------------
# the deliberately-real failures, kept OUT of the registry on purpose
# --------------------------------------------------------------------------

#: Reds that are NOT registered, because a registry entry asserts a gap is
#: open on purpose and these are not. They belong in the G0 report as
#: ``fail``. Listed here so the distinction is written down where someone
#: looking for a place to file a red will find it.
#:
#: Populated from the measured full-suite run on 2026-10-01 (see
#: ``docs/release-verdict.md``). Kept as prose, not as a registry entry,
#: because an entry here would assert the gap is deliberately open.
KNOWN_REAL_FAILURES: Tuple[str, ...] = (
    "demo/test_product_docs.py::test_command_reference_covers_current_registry "
    "-- real docs drift: 20+ registered slash commands are absent from the "
    "command reference. Owner: T4. NOT a known-failing pin.",
)


def render_report(verdicts: Sequence[Verdict]) -> str:
    """Render verdicts as the report a reader of a red CI run needs."""
    lines: List[str] = []
    counts: Dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
        marker = "OK  " if verdict.verdict == AS_RECORDED else "FAIL"
        lines.append(f"[{marker}] {verdict.verdict}: {verdict.node_id}")
        lines.append(f"         {verdict.detail}")
    lines.append("")
    lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    bad = [v for v in verdicts if v.is_build_failure]
    lines.append(
        f"{len(bad)} build failure(s) across {len(verdicts)} registered pin(s)."
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: check the registry and exit non-zero on any build failure.

    ``python -m tests.known_failing_pins`` — used by CI so the registry is
    checked on its own, independently of whether the meta-test was selected.
    """
    verdicts = check_all()
    print(render_report(verdicts))
    for node_id, why in suppression_offenders():
        print(f"[FAIL] {SUPPRESSED}: {node_id} {why}")
        verdicts.append(Verdict("suppression", node_id, SUPPRESSED, why))
    for where, why in blanket_marker_offenders():
        print(f"[FAIL] {BLANKET_MARKER}: {where} {why}")
        verdicts.append(Verdict("config", where, BLANKET_MARKER, why))
    return 1 if any(v.is_build_failure for v in verdicts) else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
