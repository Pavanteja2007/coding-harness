"""T5.W2.1 — the META-TEST for the known-failing pin registry.

This file does not test product behaviour. It tests **the registry in
``tests/known_failing_pins.py``**, and it is the reason a deliberately-red
pin and a real regression can no longer be confused in a CI log.

What it enforces, one test per mechanic
---------------------------------------

1. :func:`test_every_registered_known_failing_pin_is_red_for_its_stated_reason`
   — each known-failing entry is failing AND every ``reason_substrings`` entry
   appears in the failure text. This is the "for the stated reason" half.
2. :func:`test_a_known_failing_pin_that_has_gone_green_fails_the_meta_test`
   — a pin that PASSES is a build failure carrying the word PROMOTE and the
   instruction to delete the registry entry in the same change.
3. :func:`test_a_known_failing_pin_that_broke_differently_is_a_regression`
   — a pin that is red but missing a stated substring is reported as a
   REGRESSION, never as a known gap.
4. :func:`test_a_registry_entry_that_cannot_be_run_is_a_build_failure`
   — missing / not-collected / timed-out are build failures, never skips.
5. :func:`test_no_registered_pin_carries_a_suppression_decorator`
   — no registered pin is ``xfail``\\ ed or ``skip``\\ ped (checked by AST, not
   by substring, so the keywords in this file's own prose cannot match).
6. :func:`test_no_pytest_configuration_can_hide_a_class_of_tests`
   — no ``addopts`` ``--ignore``/``--deselect``/``--ignore-glob``/marker
   filter. This is the blanket-suppression door.
7. :func:`test_the_registry_and_the_meta_test_cannot_both_be_empty`
   — non-vacuity: an empty registry passes nothing. A registry with zero
   entries would make every other test here vacuous, so it is a failure.
8. :func:`test_an_inverted_pin_that_has_gone_red_is_a_regression`
   — the deliberately-GREEN class is checked with inverted sense.

Mechanics 1-4 are additionally demonstrated on **synthetic entries** (no
subprocess, no repository dependency) in
:func:`test_the_four_mechanics_fire_on_a_synthetic_entry`, so the behaviour
is proven even for the promotion case that the real tree cannot currently
produce — the whole point being that a future green pin fails this file
rather than passing quietly.
"""

from __future__ import annotations

import pytest

from tests.known_failing_pins import (
    AS_RECORDED,
    BUILD_FAILURES,
    CHANGED_REASON,
    FAILED,
    INVERTED_PINS,
    KNOWN_FAILING_PINS,
    MISSING,
    NOT_COLLECTED,
    PASSED,
    PROMOTE,
    TIMED_OUT,
    VERDICTS,
    InvertedPin,
    KnownFailingPin,
    Observation,
    RegistryError,
    Verdict,
    all_entries,
    blanket_marker_offenders,
    check_all,
    classify_inverted,
    classify_known_failing,
    observe,
    render_report,
    suppression_offenders,
)

# ---------------------------------------------------------------------------
# the synthetic pin used to demonstrate the mechanics with no subprocess
# ---------------------------------------------------------------------------

SYNTHETIC = KnownFailingPin(
    node_id="tests/known_failing_pins.py::test_a_synthetic_pin",
    terminal="T5 (synthetic)",
    owner="nobody / never",
    closes_when="nothing; this pin exists only to be classified.",
    reason_substrings=("SYNTHETIC GAP STILL PRESENT", "synthetic/module.py"),
)

SYNTHETIC_OBSERVATIONS = {
    "as_recorded": Observation(
        outcome=FAILED,
        text="E AssertionError: SYNTHETIC GAP STILL PRESENT\n"
        "  synthetic/module.py:12 from harness._stubs import ...",
    ),
    "promote": Observation(outcome=PASSED, text="1 passed in 0.10s"),
    "changed_reason": Observation(
        outcome=FAILED,
        text="E AssertionError: some entirely different defect\n  other.py:1",
    ),
    "not_collected": Observation(
        outcome=NOT_COLLECTED, text="no tests ran", returncode=5
    ),
    "timed_out": Observation(
        outcome=TIMED_OUT, text="did not finish within 300s", returncode=None
    ),
}


# ---------------------------------------------------------------------------
# MECHANICS 1-4, demonstrated on a synthetic entry (no repository dependency)
# ---------------------------------------------------------------------------


def test_the_four_mechanics_fire_on_a_synthetic_entry() -> None:
    """Every outcome is reachable and distinguishable, with no subprocess.

    This is the demonstration the brief asks for. Each arm is a real
    classification of a real :class:`Observation`; none of them touches the
    repository, so all four mechanics are proven even though the current tree
    can only produce one of them.
    """
    got = {
        name: classify_known_failing(SYNTHETIC, obs)
        for name, obs in SYNTHETIC_OBSERVATIONS.items()
    }

    # 1. as recorded -> the only healthy outcome, and NOT a build failure
    assert got["as_recorded"].verdict == AS_RECORDED
    assert got["as_recorded"].is_build_failure is False

    # 2. promote -> a BUILD FAILURE that says PROMOTE and names the owner, so
    #    the reader knows both that the gap closed and who has to act on it.
    promote = got["promote"]
    assert promote.verdict == PROMOTE
    assert promote.is_build_failure is True
    assert "PROMOTE THIS" in promote.detail
    assert "nobody / never" in promote.detail
    assert "KNOWN_FAILING_PINS" in promote.detail, (
        "the promotion message must name the registry to delete from"
    )

    # 3. changed reason -> a REGRESSION, explicitly not a closed gap
    changed = got["changed_reason"]
    assert changed.verdict == CHANGED_REASON
    assert changed.is_build_failure is True
    assert "REGRESSION" in changed.detail or "DIFFERENT REASON" in changed.detail
    assert "SYNTHETIC GAP STILL PRESENT" in changed.detail, (
        "the message must quote what the recorded failure used to say, or a "
        "reader cannot tell which pin changed"
    )

    # 4. cannot be run -> a build failure, never a skip
    for name in ("not_collected", "timed_out"):
        verdict = got[name]
        assert verdict.verdict == MISSING
        assert verdict.is_build_failure is True
        assert "could not be RUN" in verdict.detail


def test_every_verdict_is_in_the_closed_vocabulary() -> None:
    """No classifier can produce a value outside :data:`VERDICTS`.

    A new outcome invented in a classifier without being written down here is
    how a gate grows a verdict nobody reads.
    """
    for obs in SYNTHETIC_OBSERVATIONS.values():
        assert classify_known_failing(SYNTHETIC, obs).verdict in VERDICTS
    synthetic_inverted = InvertedPin(
        node_id="tests/known_failing_pins.py::test_a_synthetic_inverted_pin",
        terminal="T5 (synthetic)",
        owner="nobody / never",
        why_green="synthetic: a closed-by-refusal gap.",
        goes_red_when="the refusal is removed.",
    )
    for obs in SYNTHETIC_OBSERVATIONS.values():
        assert classify_inverted(synthetic_inverted, obs).verdict in VERDICTS


def test_build_failures_are_a_strict_subset_of_the_vocabulary() -> None:
    """Every build-failure value is a real verdict (no dead entries)."""
    for value in BUILD_FAILURES:
        assert value in VERDICTS
    assert AS_RECORDED not in BUILD_FAILURES


def test_an_inverted_pin_is_classified_with_the_sense_inverted() -> None:
    """Green is healthy for an inverted pin; red is a regression, not a promotion.

    The two classes are registered separately for exactly this reason: a red
    inverted pin is NOT a closed gap, and filing it as one is the confusion
    this module exists to prevent.
    """
    pin = InvertedPin(
        node_id="tests/known_failing_pins.py::test_a_synthetic_inverted_pin",
        terminal="T5 (synthetic)",
        owner="nobody / never",
        why_green="synthetic: a closed-by-refusal gap.",
        goes_red_when="the refusal is removed.",
    )
    green = classify_inverted(pin, Observation(outcome=PASSED, text="1 passed"))
    assert green.verdict == AS_RECORDED
    assert green.is_build_failure is False

    red = classify_inverted(
        pin, Observation(outcome=FAILED, text="E AssertionError: refusal gone")
    )
    assert red.verdict == CHANGED_REASON
    assert red.is_build_failure is True
    assert "REGRESSION" in red.detail
    assert "not a promotion" not in red.detail.lower()


# ---------------------------------------------------------------------------
# the registry itself must be registrable
# ---------------------------------------------------------------------------


def test_the_registry_and_the_meta_test_cannot_both_be_empty() -> None:
    """Non-vacuity: a registry with no entries is a FAILURE, not a pass.

    Every other test in this file iterates the registry. If it were empty they
    would all pass while proving nothing — the same vacuity DOCTRINE §3 warns
    about, applied to the gate that is supposed to prevent vacuity.
    """
    entries = all_entries()
    assert entries, (
        "KNOWN_FAILING_PINS and INVERTED_PINS are both empty. The registry "
        "exists to record deliberate pins; an empty one makes every check in "
        "this file vacuous."
    )
    assert len(entries) >= 2, (
        "the registry has fewer than two entries, so the deliberately-red and "
        "the deliberately-green classes are not both represented and the "
        "distinction between them is untested"
    )


def test_every_registry_entry_is_a_real_file_on_disk() -> None:
    """A node id whose file does not exist is how a real failure gets filed as known."""
    for kind, entry in all_entries():
        path = entry.path  # type: ignore[attr-defined]
        assert path.endswith(".py"), f"{entry.node_id}: not a .py path"  # type: ignore[attr-defined]
        from tests.known_failing_pins import REPO_ROOT

        assert (REPO_ROOT / path).is_file(), (
            f"{kind} pin {entry.node_id!r} names {path!r}, which does not "
            "exist. A registry pointing at a test that is not there is how a "
            "real failure gets filed as a known one."
        )


def test_registry_construction_refuses_a_non_exact_node_id() -> None:
    """A file, a glob or a ``-k`` expression is not registrable.

    Such an entry cannot distinguish "the pin went green" from "the file was
    renamed", which is the whole job.
    """
    for bad in (
        "tests/test_something.py",  # a file, not a test
        "tests/test_*.py::test_x",  # a glob path
        "tests/test_something.py::test_*",  # a glob name
        "tests/test_something.py::test_x[a-z]",  # a character class
    ):
        with pytest.raises(RegistryError):
            KnownFailingPin(
                node_id=bad,
                terminal="T5",
                owner="T5",
                closes_when="x",
                reason_substrings=("a", "b"),
            )


def test_registry_construction_refuses_a_reason_that_is_not_a_reason() -> None:
    """Zero or one substring cannot identify a red; an empty owner is not an owner."""
    with pytest.raises(RegistryError, match="at least 2 reason substrings"):
        KnownFailingPin(
            node_id="tests/test_something.py::test_x",
            terminal="T5",
            owner="T5",
            closes_when="x",
            reason_substrings=("only one",),
        )
    for field_name in ("terminal", "owner", "closes_when"):
        kwargs = {
            "node_id": "tests/test_something.py::test_x",
            "terminal": "T5",
            "owner": "T5",
            "closes_when": "x",
            "reason_substrings": ("a", "b"),
            field_name: "   ",
        }
        with pytest.raises(RegistryError, match=field_name):
            KnownFailingPin(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MECHANIC 1 — the real registry, observed for real
# ---------------------------------------------------------------------------


def test_every_registered_known_failing_pin_is_red_for_its_stated_reason() -> None:
    """Mechanic 1, against the real tree with a real subprocess per pin.

    This is the load-bearing test: it is the one that makes "this red is
    recorded" true rather than claimed. It runs each known-failing pin exactly
    as CI would, and requires BOTH that it is red and that the recorded
    substrings are in the red.
    """
    assert KNOWN_FAILING_PINS, (
        "no known-failing pins are registered; if the tree genuinely has none, "
        "say so in KNOWN_FAILING_PINS with a comment rather than leaving it "
        "silently empty"
    )
    failures: list[str] = []
    for pin in KNOWN_FAILING_PINS:
        verdict = classify_known_failing(pin, observe(pin.node_id))
        if verdict.verdict != AS_RECORDED:
            failures.append(f"{verdict.verdict}: {verdict.node_id}\n{verdict.detail}")
    assert not failures, (
        "a registered known-failing pin is not red for its recorded reason:\n\n"
        + "\n\n".join(failures)
        + "\n\nA pin that started passing is a CLOSED GAP (delete the entry in "
        "the same change). A pin that is red for a DIFFERENT reason is a "
        "REGRESSION. Neither is a known gap."
    )


def test_every_registered_inverted_pin_is_green() -> None:
    """The deliberately-green class must be green; a red is a regression."""
    failures: list[str] = []
    for pin in INVERTED_PINS:
        verdict = classify_inverted(pin, observe(pin.node_id))
        if verdict.verdict != AS_RECORDED:
            failures.append(f"{verdict.verdict}: {verdict.node_id}\n{verdict.detail}")
    assert not failures, (
        "a registered INVERTED pin is not green. An inverted pin guards a "
        "closed-by-refusal gap, so a red here means somebody removed the "
        "refusal:\n\n" + "\n\n".join(failures)
    )


# ---------------------------------------------------------------------------
# MECHANIC 5 — no blanket suppression
# ---------------------------------------------------------------------------


def test_no_registered_pin_carries_a_suppression_decorator() -> None:
    """No registered pin is xfail-ed or skip-ped. Checked by AST, not by text.

    A suppressed pin is not red anywhere, so the registry's central claim
    becomes unverifiable while still looking true. The suppression keywords
    necessarily appear in THIS file's prose, so a substring scan over the
    repository would match itself and pass vacuously.
    """
    offenders = suppression_offenders()
    assert not offenders, (
        "a registered pin carries a suppression decorator: "
        f"{offenders!r}. The registry is the SINGLE place a deliberate pin is "
        "recorded; an xfail/skip in place of an entry makes a recorded gap "
        "indistinguishable from a fixed one."
    )


def test_no_pytest_configuration_can_hide_a_class_of_tests() -> None:
    """No ``--ignore``/``--deselect``/marker filter in pytest addopts.

    Entries are named, individually. A config that drops a class of tests from
    collection would hide a promoted pin and every real failure in it, and it
    would do so without any single test looking changed.
    """
    offenders = blanket_marker_offenders()
    assert not offenders, (
        f"pytest configuration can suppress a class of tests: {offenders!r}. "
        "Suppression must be per-entry and named, never a configuration-wide "
        "filter."
    )


def test_suppression_detection_actually_catches_a_suppressed_pin(tmp_path) -> None:
    """The AST check is not theatre: a planted ``xfail`` is caught.

    Without this arm, "no offenders" is indistinguishable from "the detector
    never looks", which is the exact vacuity this repository keeps paying for.
    """
    from tests.known_failing_pins import REPO_ROOT, suppression_offenders

    planted = tmp_path / "planted_pin_module.py"
    planted.write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.xfail(reason='suppressed in place')\n"
        "def test_a_planted_suppressed_pin():\n"
        "    assert False\n",
        encoding="utf-8",
    )
    entry = KnownFailingPin(
        node_id="planted_pin_module.py::test_a_planted_suppressed_pin",
        terminal="T5",
        owner="T5",
        closes_when="nothing",
        reason_substrings=("a", "b"),
    )
    # Point the detector at the planted file by monkeypatching REPO_ROOT is not
    # possible (it is read at call time from the module global, so it is).
    import tests.known_failing_pins as registry

    original = registry.REPO_ROOT
    try:
        registry.REPO_ROOT = tmp_path
        offenders = suppression_offenders([entry])
    finally:
        registry.REPO_ROOT = original
    assert REPO_ROOT.name  # the real root is still readable after the swap
    assert offenders, (
        "the AST suppression detector found nothing for a module whose only "
        "test function carries @pytest.mark.xfail. The detector is not "
        "working, so the 'no offenders' assertion above proves nothing."
    )
    assert "xfail" in offenders[0][1]
    assert entry.node_id in offenders[0][0]


def test_a_planted_addopts_filter_is_caught(tmp_path) -> None:
    """The config check is not theatre either: a planted ``--deselect`` is caught."""
    planted = tmp_path / "pyproject.toml"
    planted.write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = ["-q", "--ignore=tests/test_known_failing_pins.py"]\n',
        encoding="utf-8",
    )
    offenders = blanket_marker_offenders(planted)
    assert offenders, "a planted --ignore in addopts was not detected"
    assert "--ignore=" in offenders[0][1]


# ---------------------------------------------------------------------------
# the runner / reporting surface
# ---------------------------------------------------------------------------


def test_check_all_drives_an_injected_observer_over_the_real_registry() -> None:
    """``check_all`` really visits every entry, with the observer injectable.

    ``observe_fn`` is what lets the meta-test exercise the promotion and
    regression arms without paying a subprocess per case — and this arm proves
    the injection is wired to the whole registry rather than to a sample.
    """
    seen: list[str] = []

    def fake(node_id: str) -> Observation:
        seen.append(node_id)
        entry = next(e for _k, e in all_entries() if e.node_id == node_id)
        if isinstance(entry, KnownFailingPin):
            # A known-failing pin fed a failure is `as_recorded` ONLY if the
            # recorded substrings are present, so the fake has to carry them.
            return Observation(
                outcome=FAILED,
                text="E AssertionError: " + " ".join(entry.reason_substrings),
            )
        # An inverted pin fed a failure is a regression, always.
        return Observation(outcome=FAILED, text="E AssertionError: refusal gone")

    verdicts = check_all(observe_fn=fake)
    registered = [entry.node_id for _k, entry in all_entries()]  # type: ignore[attr-defined]
    assert sorted(seen) == sorted(registered)
    assert len(verdicts) == len(registered)
    # An inverted pin fed a "failed" observation must NOT be reported healthy.
    by_id = {v.node_id: v for v in verdicts}
    inverted_ids = {p.node_id for p in INVERTED_PINS}
    for _kind, entry in all_entries():
        verdict = by_id[entry.node_id]  # type: ignore[attr-defined]
        if entry.node_id in inverted_ids:  # type: ignore[attr-defined]
            assert verdict.verdict == CHANGED_REASON, (
                f"{entry.node_id} is an INVERTED pin and was fed a failure; it "
                f"must be reported as a regression, not {verdict.verdict!r}"
            )
        else:
            assert verdict.verdict == AS_RECORDED


def test_check_all_can_select_one_class() -> None:
    """The class filter is honoured, so a report can be scoped."""
    only = check_all(
        observe_fn=lambda _n: SYNTHETIC_OBSERVATIONS["as_recorded"], kinds={"inverted"}
    )
    assert only
    assert all(v.kind == "inverted" for v in only)
    assert len(only) == len(INVERTED_PINS)


def test_the_report_names_every_verdict_and_the_build_failure_count() -> None:
    """A reader of a red CI run must be able to see what to do from the report alone."""
    verdicts = [
        Verdict("known_failing", "a::t", AS_RECORDED, "still red"),
        Verdict("known_failing", "b::t", PROMOTE, "PROMOTE THIS"),
    ]
    report = render_report(verdicts)
    assert "as_recorded" in report
    assert "promote_me" in report
    assert "a::t" in report and "b::t" in report
    assert "1 build failure(s) across 2 registered pin(s)." in report


def test_an_empty_verdict_list_still_reports_zero_rather_than_crashing() -> None:
    """The reporter is total. A gate that crashes on an empty registry is a gate
    that reports nothing on the day the registry is emptied by accident."""
    report = render_report([])
    assert "0 build failure(s) across 0 registered pin(s)." in report
    assert "counts:" in report
