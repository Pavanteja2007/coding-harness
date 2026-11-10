"""PHASE 4 KNOWN GAPS, pre-registered as INVERTED pins.

**A recorded gap is one somebody can close; an unrecorded one gets
rediscovered.** T2 pre-registered Phase 3's pins and it prevented two rounds of
misunderstanding, so the five gaps this phase starts with are registered HERE,
before they turn into surprises -- each with an owner, a phase, and a
MEASURABLE green condition rather than a description.

Every test in this file is **GREEN today** and asserts that its gap is still
open. Each one is shaped so that closing the gap makes it FAIL, with a message
naming what to do next: promote it, delete the pin, and update the recorded
entry. That is the mechanism ``tests/known_failing_pins.py::INVERTED_PINS``
gates -- ``test_every_registered_inverted_pin_is_green`` runs each of these as a
subprocess and fails the lane if one of them goes red without its registry entry
being updated.

The five gaps are not mine to close: three are T1's, one T2's, one T3's, and the
fifth is mine. Each pin reads the owning module's source or drives its public
callable, and none of them edits it.

Host-only: no Docker, no provider, no network, no credential.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict

import pytest

from evals import multi_session as MS
from harness import long_horizon as LH
from harness.agent_kernel import read_before_edit as RBE
from harness.agent_kernel import tools as AKT


def _source(module: Any) -> str:
    """Return a module's source, read from disk by its own ``__file__``."""
    return Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# GAP 1 -- owner T1 / P4. Read-before-edit false positives.
# ---------------------------------------------------------------------------


def test_read_before_edit_false_positives_are_still_open_so_this_file_is_not_claiming_a_fix():
    """A file this run MUTATED is not unread -- except when the mutation named
    ``source_path``, because the gate's path vocabulary and the recorder's differ.

    **Measured on this tree.** ``read_before_edit._paths_of`` looks at five keys:

        ``path``, ``destination_path``, ``source_path``, ``target``, ``file``

    and ``tools.ToolRegistry._note_run_mutation`` records a run's own write from
    four:

        ``path``, ``destination_path``, ``target``, ``file``

    ``source_path`` is in the first and absent from the second. The consequence is
    a genuine FALSE POSITIVE, in the direction the gate exists to avoid: a tool
    that names ``source_path`` IS gated on it, so the gate knows the path -- and
    the run's write of that path is never recorded, so a later legitimate edit of
    it is refused ``never_read`` on the grounds that the run had no observation
    of a file the run itself just wrote.

    **Goes green (and this pin fails) when** the two vocabularies are converged,
    or when one is derived from the other. Then delete this test and drop its
    ``InvertedPin`` entry in the same change.
    """
    gated = RBE._paths_of("delete", {"source_path": "pkg/mod.py"})
    assert gated == ("pkg/mod.py",), (
        "the gate's path vocabulary changed; re-derive the gap before assuming this pin's shape"
    )

    source = _source(AKT)
    recorder_block = source.split("def _note_run_mutation", 1)[1].split("\n    def ", 1)[0]
    for key in ("path", "destination_path", "target", "file"):
        assert f'"{key}"' in recorder_block, f"the recorder stopped reading {key}; re-derive this gap"
    assert '"source_path"' not in recorder_block, (
        "T1 CONVERGED the recorder's path vocabulary with the gate's "
        "(read_before_edit._paths_of). That CLOSES this recorded gap -- the false "
        "positive a run's own write triggered is gone. Delete this test AND its "
        "InvertedPin entry in tests/known_failing_pins.py in the same change, and "
        "say in the Change Log which one you kept as the authority."
    )


# ---------------------------------------------------------------------------
# GAP 2 -- owner T3 / P4. The subagent split-decision rule is a heuristic, AND
# its receipt is hard-coded to say it did not truncate.
# ---------------------------------------------------------------------------


def test_the_subagent_split_decision_is_still_a_character_threshold_and_still_reports_untruncated():
    """A child's summary is split by LENGTH, and ``truncated`` is never set.

    **Measured on this tree, and the second half is worse than the first.**
    ``runtime.subagents.summarize_child`` cuts a child's verification text and a
    child's error text at a literal ``[:400]`` and cuts ``changed_files`` and
    ``follow_ups`` at their cap constants. That is the split decision, and it is a
    character threshold with no notion of which part of a verification matters.

    ``ChildSummary`` then declares a field named ``truncated`` whose default is
    ``False`` -- and ``summarize_child`` never assigns it. So a summary whose
    verification was cut mid-sentence reports ``truncated: false``, and a parent
    reading that concludes it saw the whole thing. A field that says "nothing was
    dropped" while bytes were dropped is worse than no field.

    **Goes green (and this pin fails) when** the split is evidence-aware (or at
    least marker-bearing) AND ``truncated`` is derived from what was actually cut.
    Then delete this test and its ``InvertedPin`` entry in the same change.
    """
    import runtime.subagents as SUB

    source = inspect.getsource(SUB.summarize_child)
    assert "[:400]" in source, (
        "summarize_child no longer truncates at a literal 400 characters; re-derive this "
        "gap before assuming its shape"
    )
    assert "truncated" not in source, (
        "T3 now SETS `truncated` from what was actually cut. That is half of this gap "
        "closed. The other half -- a character threshold standing in for an evidence-aware "
        "split -- is only closed if the split is also derived from what matters in a "
        "verification. If it is, delete this test AND its InvertedPin entry in the same "
        "change; if it is not, narrow this pin to the remaining half and say so."
    )
    assert SUB.ChildSummary.__dataclass_fields__["truncated"].default is False, (
        "the `truncated` default changed; re-derive this gap"
    )


# ---------------------------------------------------------------------------
# GAP 3 -- owner T2 / P4. End-to-end verification is weaker than unit
# verification, and the ledger inherits the weaker standard.
# ---------------------------------------------------------------------------


def test_end_to_end_verification_is_still_weaker_than_a_unit_check():
    """An ``e2e`` feature's evidence is a list of step exit codes and nothing else.

    **Measured on this tree.** ``harness.long_horizon`` accepts ``e2e`` as one of
    :data:`harness.long_horizon.VERIFICATION_METHODS`, and
    ``run_end_to_end_verification`` returns an :class:`EndToEndReport` whose fields
    are ``feature_id, ran, outcome, steps, teardown_ran, teardown_error,
    duration_s, available, unavailable_reason``.

    There is no regression term and no flake term anywhere in that shape. A
    ``command`` feature is verified by running a declared command, which reaches
    the repository's own machinery (repetitions plus the three-valued flake
    verdict) before a pass is reported. An ``e2e`` feature gets a list of step
    exit codes: **no "did everything else still work", and no "was this stable
    across two runs".** A feature can therefore be marked ``passing`` on evidence
    strictly weaker than the one that mints ``completed_verified``.

    That is the safe direction -- an e2e feature is harder to over-claim on, not
    easier -- and it is a real capability gap: a feature whose only honest check
    is end-to-end is being judged by a weaker standard, and the ledger does not
    say so on the row.

    **Goes green (and this pin fails) when** the e2e path carries a regression
    and/or stability term, or when a ledger row records which standard its
    evidence met. Then delete this test and its ``InvertedPin`` entry.
    """
    import dataclasses

    fields = {field.name for field in dataclasses.fields(LH.EndToEndReport)}
    assert "e2e" in LH.VERIFICATION_METHODS, (
        "the e2e verification method was removed or renamed; re-derive this gap"
    )
    assert fields, "EndToEndReport lost its fields; re-derive this gap"
    forbidden = {"regression", "regression_passed", "flake", "flake_check", "repetitions"}
    assert not (fields & forbidden), (
        f"EndToEndReport now carries {sorted(fields & forbidden)}, so the e2e path has a "
        "regression/stability term it did not have. That CLOSES this recorded gap -- delete "
        "this test AND its InvertedPin entry in the same change."
    )
    # The unit half is asserted too, so the comparison is between two real shapes
    # and not between a shape and an assumption.
    assert LH.VerificationRun.__dataclass_fields__["method"].default == "command", (
        "the unit half of the comparison moved; re-derive this gap"
    )


# ---------------------------------------------------------------------------
# GAP 4 -- owner T3 / P4. Middleware ordering. CLOSED IN P4/W2.3 (mid-wave),
# and the pin is DELETED rather than flipped.
#
# T3 landed `runtime/test_p4w2_middleware_proof.py` (76,846 bytes, **61 tests,
# all green**) DURING this wave. It is what the pin asked for by name:
#
#   * it CONSTRUCTS the real pipeline (`build_pipeline((...)).check(turn, run,
#     phase="pre_call")`), not the declaration;
#   * it drives each declared situation, including the three this pin quoted:
#     `test_cost_is_checked_before_the_dial`,
#     `test_auto_compaction_runs_before_context_exceeded`, and
#     `test_read_only_gates_before_a_write_is_dispatched`;
#   * it is BEHAVIOURAL, not an index comparison. The compaction test asserts
#     `turn.context_compacted is True` and that an `auto_compacted` observation
#     was recorded -- and, critically, carries a CONTROL: the same overflowing
#     turn with **no compactor bound** must still terminate, or "compaction ran
#     first" would be satisfied by a pipeline that never reports overflow at all.
#     That control is the difference between measuring the ordering and
#     restating it.
#
# So the declaration is no longer enforced against itself, and the gap closed on
# arrival -- which is what a pin for a closing gap is supposed to do.
#
# **The ADJACENT conflict is NOT closed by this and is deliberately still open.**
# T3's same landing wired `runtime/turn_middleware` into the daily strategy
# (`harness/agent_kernel/strategy.py:29`), which VIOLATES a different pinned
# invariant --
# `tests/test_ledger_store_structural_pins.py::TestAddingAMiddlewareDoesNotTouch
# TheLoop::test_no_kernel_module_imports_the_middleware_either` -- and that test
# is RED. Two pins disagreed and only a human can pick which one is intended.
# T5 edited neither. See `scripts/p4_test_adjudication.py::
# check_the_kernel_imports_the_middleware`, which keeps reporting it.


# ---------------------------------------------------------------------------
# GAP 5 -- owner T5 / P4 (mine). CLOSED IN P4/W2.3. The pin and its registry
# entry are DELETED rather than flipped, because a registry that keeps reporting
# a closed gap costs more than the gap did.
#
# What closed it: `evals/multi_session.py::TASK_SLUGS` is now
# `("multi_session_shop", "multi_session_cascade")` and the two tasks differ in
# DEPENDENCY SHAPE, not in module names -- the cascade task's feature N imports
# feature N-1's, so a later session can genuinely break an earlier feature's
# green. That is the shape the independent-feature task structurally cannot
# reach, and it is the shape the rework claim needs.
#
# The pin asserted three things had to change together, and all three did:
#   1. publish the new task                    -> TASK_SLUGS has two entries
#   2. rewrite the 'one task' honest_note      -> it now names the task COUNT and
#                                                  the shapes NOT covered
#   3. delete the pin and the InvertedPin      -> this section and that entry
#
# The replacement is NOT a claim that multi-session work is covered. It is a
# named list of what is still missing: `evals/multi_session.py::TASK_SHAPES_
# NOT_COVERED` holds four of them, and the report publishes that list on every
# run, so "two tasks" cannot be read as "the phase measured multi-session
# autonomy".
#
# The shape that still does NOT exist, and is the one a reader should ask for
# next: a task whose constraints are recoverable ONLY from the progress ledger
# rather than from the brief. Every session in both current tasks receives the
# same CONSTRAINTS tuple from `build_session_brief`, so constraint retention is
# currently measured against a brief that always carried them.


class TestTheMultiSessionTaskSetIsNoLongerOneTask:
    """The replacement for the deleted one-task pin, and it is an INVERTED pin.

    Green while the coverage limitation is still real and published; RED the
    moment the honest note stops naming what is uncovered. It protects the
    honesty of the coverage CLAIM rather than the count, because the count is
    now two and a third task would be a win rather than a fix.
    """

    def test_the_report_still_publishes_what_the_task_set_does_not_cover(self) -> None:
        assert len(MS.TASK_SLUGS) >= 2, (
            f"the task set shrank to {MS.TASK_SLUGS}; a coverage regression is not a "
            "measurement improvement"
        )
        assert MS.TASK_SHAPES_NOT_COVERED, (
            "TASK_SHAPES_NOT_COVERED is empty. Two tasks read as coverage, and coverage "
            "without a named boundary is the specific overclaim this phase exists to avoid."
        )
        payload = MS.run_multi_session(
            ["baseline"], repetitions=1, gate=lambda task, target: False, out_root=""
        ).to_dict()
        assert payload["task_shapes_not_covered"], (
            "the report stopped publishing the uncovered shapes; restore it before adding a "
            "third task, not after"
        )
        assert "one task" not in payload["honest_note"], (
            "the 'one task' honest_note is a FACT that is now FALSE, and a stale honest note "
            "is worse than no honest note"
        )
        assert f"{len(MS.TASK_SLUGS)} task(s)" in payload["honest_note"], (
            "the honest_note must state how many tasks were actually run, so a reader cannot "
            "assume the whole table was"
        )



# ---------------------------------------------------------------------------
# The replacement pin for the CLOSED one-task gap, deliberately NOT a class
# method and deliberately NOT in the registry below.
#
# It is a different KIND of pin and conflating the two would be worse than the
# duplication. The four registry entries are **recorded gaps in code this
# terminal does not own**, each with an owner who can close it. This one is
# **T5's own coverage-honesty claim**, and no other terminal can close it --
# there is no owner to hand it to. Registering it would put an entry in the
# "who fixes this" table that nobody can act on, which is how a registry starts
# being read as paperwork.
# ---------------------------------------------------------------------------


def test_the_two_tasks_differ_in_dependency_shape_not_only_in_names() -> None:
    """**The reason the second task counts.**

    Two tasks that differ only in module NAMES measure the same shape twice
    and the median would improve for no reason. So the dependency structure
    has to differ, and it is asserted on the GENERATED SOURCE rather than on
    a shape string a caller could set without changing the fixture.
    """
    import tempfile
    from pathlib import Path

    shapes = {}
    for slug in MS.TASK_SLUGS:
        root = Path(tempfile.mkdtemp(prefix="p4w2-shape-"))
        try:
            built = MS.build_task_repo(root, slug=slug)
            features = list(built["features"])
            last = (
                root / built["package"] / f"{features[-1]}.py"
            ).read_text(encoding="utf-8")
            previous = features[-2] if len(features) > 1 else ""
            shapes[slug] = (
                built["shape"],
                previous in last,
            )
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)
    assert len(set(shapes.values())) == len(shapes), (
        f"the tasks do not differ in dependency shape: {shapes}. Two tasks differing "
        "only in module names measure one shape twice, and the median across them is a "
        "number with no more information in it than either task alone."
    )
    assert any(imports for _shape, imports in shapes.values()), (
        f"no task in {sorted(shapes)} has a feature that imports its predecessor, so the "
        "rework claim has no task that can reach it"
    )


def test_the_dependency_chain_survives_an_actual_session_not_only_the_fixture() -> None:
    """**The pin above measures a repository no session ever works in.**

    ``test_the_two_tasks_differ_in_dependency_shape_not_only_in_names`` reads the
    freshly-built module, and the build DOES write the chain's import. But the
    first version of the session body wrote a bare implementation, so the first
    ``write`` of every session erased it and the ``dependency_chain`` task became
    the ``independent_features`` task one turn in -- while the fixture-only pin
    stayed green for the whole wave.

    So the import has to be asserted on the bytes **after a session has run**.
    This drives a real bounded session over the cascade task and then reads the
    module off disk.
    """
    import shutil
    import tempfile
    from pathlib import Path

    slug = "multi_session_cascade"
    if slug not in MS.TASK_SLUGS:
        pytest.skip(
            f"the cascade task is not in TASK_SLUGS ({MS.TASK_SLUGS}), so there is no chain "
            "to survive a session"
        )
    root = Path(tempfile.mkdtemp(prefix="p4w2-chain-after-"))
    try:
        built = MS.build_task_repo(root, slug=slug)
        features = list(built["features"])
        target, predecessor = features[-1], features[-2]
        outcome = MS._bounded_session(
            built,
            root,
            session_index=0,
            claimed_feature=target,
            config={"read_before_edit": False, "read_before_edit_same_turn": False},
            session_turns=3,
            brief=None,
            gate=lambda features, target: True,
        )
        assert outcome.finished_feature == target, (
            f"the session under test finished {outcome.finished_feature!r} rather than "
            f"{target!r} (reason={outcome.reason!r}); the chain cannot be asserted on a "
            "module the session never produced"
        )
        written = (root / built["package"] / f"{target}.py").read_text(encoding="utf-8")
        assert predecessor in written, (
            f"{slug}: after a REAL session, {target}.py no longer references "
            f"{predecessor}:\n{written!r}\n\nA session's own write erased the dependency the "
            "shape is defined by, so the 'dependency chain' task measured the independent-"
            "features task for the whole run. The chain has to be reconstructed on every "
            "write, not just at build time."
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# The registry itself: five pins, five owners, one place.
# ---------------------------------------------------------------------------


class TestTheGapRegistryIsWellFormed:
    def test_the_four_open_gaps_are_registered_with_an_owner_and_a_phase(self):
        """The brief's table, asserted as data, minus the one that is CLOSED.

        Every gap names an owner (so somebody can close it), a phase (so the
        hand-off trail survives), and a test in THIS file (so the registry cannot
        point at a test that does not exist).
        """
        from tests import known_failing_pins as KFP

        expected: Dict[str, str] = {
            "TestTheGapRegistryIsWellFormed": "",
            "test_read_before_edit_false_positives_are_still_open_so_this_file_is_not_claiming_a_fix": "T1",
            "test_the_subagent_split_decision_is_still_a_character_threshold_and_still_reports_untruncated": "T3",
            "test_end_to_end_verification_is_still_weaker_than_a_unit_check": "T2",
            # The fifth starting gap (one multi-session task only) was CLOSED in
            # P4/W2.3. Its replacement is a module-level function, not a registry
            # entry, and the reason it is not one is asserted below rather than
            # left as a comment.
        }
        this_file = f"tests/{Path(__file__).name}"
        registered = {
            pin.node_id.split("::", 1)[1]: pin.owner
            for pin in KFP.INVERTED_PINS
            if pin.node_id.startswith(f"{this_file}::")
        }
        for name, owner in expected.items():
            if not name.startswith("test_"):
                continue
            assert name in registered, (
                f"{name} is a gap pin in this file but is NOT registered in "
                "tests/known_failing_pins.py::INVERTED_PINS; an unregistered inverted pin "
                "protects nothing, because the lane that would notice it going red does "
                "not know it exists"
            )
            assert registered[name].startswith(owner), (
                f"{name} is owned by {owner} but the registry says {registered[name]!r}"
            )
            assert registered[name].count("/") >= 1, (
                f"{name}'s owner {registered[name]!r} does not name a PHASE; a recorded gap "
                "without a phase has no place in the hand-off trail"
            )

    def test_the_stores_own_gap_registry_is_empty_because_its_one_gap_is_closed(self):
        """One registry per gap, so closing one cannot leave a stale twin.

        The store registered one gap in T1 -- ``ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_
        HATCH`` -- and this test pinned its presence. T1 fixed it mid-phase, so the
        correct end state is that the store owns **no** open gaps and this file's
        five remain the only Phase-4 pins.

        The assertion is written as an equality against the EMPTY set rather than
        as "the old id is gone", so a *new* gap recorded in
        ``harness/long_horizon.py`` without a matching pin here also fails. A
        membership check would have gone quietly green on exactly the case this
        rule exists for: a found gap that nobody can watch.
        """
        from memory import feature_ledger_store as FL

        mine = {
            item["id"]
            for item in FL.RECORDED_GAPS
            if "harness/long_horizon.py" in item.get("where", "")
        }
        assert mine == set(), (
            f"the ledger store now records {sorted(mine)} in harness/long_horizon.py. A "
            "recorded gap needs an inverted pin in its OWN file (tests/"
            "test_feature_ledger_store.py), because a gap nobody watches is a comment. Add "
            "the pin in the same change that adds the entry."
        )
        from tests import known_failing_pins as KFP

        this_file = f"tests/{Path(__file__).name}"
        registered_here = [
            pin.node_id for pin in KFP.INVERTED_PINS if pin.node_id.startswith(f"{this_file}::")
        ]
        assert len(registered_here) == 3, (
            f"{len(registered_here)} pins are registered from this file. THREE of Phase 4's "
            "five starting gaps are still open; the other two were CLOSED in P4/W2.3 -- one "
            "multi-session task only (mine), and the middleware ordering, by T3's "
            "runtime/test_p4w2_middleware_proof.py which landed mid-wave -- and their pins "
            f"were deleted rather than flipped. Registered here: {registered_here}"
        )



    @pytest.mark.parametrize(
        "name",
        [
            "test_read_before_edit_false_positives_are_still_open_so_this_file_is_not_claiming_a_fix",
            "test_the_subagent_split_decision_is_still_a_character_threshold_and_still_reports_untruncated",
            "test_end_to_end_verification_is_still_weaker_than_a_unit_check",
        ],
    )
    def test_every_registered_gap_is_green_right_now(self, name: str):
        """All FOUR open gap pins are green, and each is GREEN ON PURPOSE.

        This is the non-vacuity control for the file: an inverted pin that is red is
        a build failure the lane reports, so the suite asserts all four are currently
        green -- which also means a future gap that is ALREADY closed is caught here
        rather than at the next unrelated run.
        """
        module = __import__("sys").modules[__name__]
        result = getattr(module, name)()
        assert result is None, (
            f"{name} returned {result!r}; a gap pin asserts, it does not compute"
        )
