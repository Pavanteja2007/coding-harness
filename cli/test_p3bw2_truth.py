"""P3b/W2 T4 - the event vocabulary is COMPLETE, and every kind has a reason.

This module exists because six rounds of `cli/AGENTS.md` recorded the same
class of finding and closed it by adding a kind to a table rather than by
closing the question "what happens to a journal row this build does not
understand". The measured debt on the tree this round received:

======================================  =====  ==================================
drift class                             count  what it means at runtime
======================================  =====  ==================================
shown-declared, not handled                   11  a row the vocabulary PROMISES a
                                                 surface can render, recorded as
                                                 UNMAPPED when it arrives
handled, declared nowhere                     27  a row a surface DOES render,
                                                 classified as dropped
producer kinds with no classification         11  including `stop`, so every
                                                 kernel-driven run reported an
                                                 UNMAPPED kind for its own last row
======================================  =====  ==================================

All three are zero here. Two of the three were invisible to the existing pins:
`test_terminal03_event_projection` checks that one event maps to one visual
kind but never checks that the projection HANDLES it, and nothing anywhere
checked the reverse. This file adds both directions by reading the projection's
own AST, because a hand-kept list of "what the fold handles" is a second
answer to that question and would drift within one round.

The file lives in ``cli/`` rather than ``tests/`` because T5 owns ``tests/**``
and a gate its own module's owner cannot edit without a cross-terminal request
is a gate that rots. That is this package's recorded practice, applied by
every previous round in it.

Everything here is host-only: no Docker, no provider, no network, no
credential, no filesystem writes.
"""

from __future__ import annotations

import ast
import textwrap
from typing import Dict, FrozenSet, List, Set

import pytest

from cli import runview

HERE = "cli/test_p3bw2_truth.py"

#: The kind names this round added a projection for. Declared here so a test
#: can iterate them and so a reader can see the debt closed in one place.
#: Every one is a member of ``harness.agent_loop_step.EVENT_KINDS``, which is
#: the producer's own closed vocabulary - asserted below rather than trusted.
PROJECTIONS_ADDED_THIS_ROUND = (
    "environment_unavailable",
    "loop_guard",
    "model_failure",
    "policy_refused",
    "reflection",
    "steering_abort",
    "steering_delivered",
    "stop",
    "turn_cap_approaching",
    "verify_skipped",
)

#: Kinds the vocabulary had declared for three rounds while the projection
#: could not render them. ``stop`` is the sharp one: it is the kernel's own
#: spelling of a finished run, so every kernel-driven run reported an UNMAPPED
#: event for its last row.
DECLARED_BUT_UNHANDLED_BEFORE = (
    "approval_error",
    "batch_call",
    "checkpoint_warning",
    "docs_lookup",
    "finish",
    "qa_read",
    "read_symbol",
    "recall",
    "run_receipt",
    "stop",
    "web_fetch",
)

#: Kinds the projection already folded while no table claimed them. They are
#: the inverse drift and were classified as dropped while being rendered.
HANDLED_BUT_UNDECLARED_BEFORE = (
    "approval_decided",
    "approval_denied",
    "build_tests_empty_reply_retry",
    "build_tests_parse_error",
    "context",
    "context_budget",
    "context_compacted",
    "diagnostics",
    "lsp_diagnostics",
    "project_criteria_empty_reply_retry",
    "project_criteria_parse_error",
    "project_plan_empty_reply_retry",
    "project_plan_parse_error",
    "question_answered",
    "question_asked",
    "question_cleared",
    "question_pending",
    "question_resolved",
    "resume_started",
    "resumed",
    "run_resumed",
    "strategy_selected",
    "tool_call_detail",
    "tool_error",
    "tool_recovery",
    "tool_validation_error",
    "unresolved_question",
)


# --------------------------------------------------------------------------
# the source pin, read once per process
# --------------------------------------------------------------------------
def _projected_event_kinds() -> FrozenSet[str]:
    """Return every kind ``RunProjection._consume_unchecked`` BRANCHES on.

    Read from the AST rather than from a list, so a new branch is covered the
    moment it is written and a deleted branch stops being claimed the moment
    it is deleted.

    Two things are deliberately excluded, and the second was found by this
    file's own sensitivity driver rather than by reading the code:

    * A comparison whose left operand is not literally ``kind``. A tool NAME
      compared against ``self.current_tool`` is not a journal kind, and
      counting those would make this pin pass on strings that are not event
      names.
    * A ``kind == ...`` inside an ``ast.IfExp`` - a ternary. A kind compared
      only to CHOOSE A LABEL is not a branch arm: deleting the handling and
      leaving the label ternary behind would keep the kind "projected" and the
      gate would pass over a kind nothing renders any more. Only ``ast.If``
      tests count, which is the construct a fold arm is written with.
    """
    with open(runview.__file__, encoding="utf-8") as handle:
        source = handle.read()
    lines = source.split("\n")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "RunProjection":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name != "_consume_unchecked":
                continue
            segment = ast.parse(
                textwrap.dedent("\n".join(lines[item.lineno - 1 : item.end_lineno]))
            )
            found: Set[str] = set()
            for sub in ast.walk(segment):
                if not isinstance(sub, ast.If):
                    continue
                test = sub.test
                if not isinstance(test, ast.Compare):
                    continue
                if not isinstance(test.left, ast.Name) or test.left.id != "kind":
                    continue
                for operand in [test.left, *test.comparators]:
                    if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                        for element in operand.elts:
                            if isinstance(element, ast.Constant) and isinstance(
                                element.value, str
                            ):
                                found.add(element.value)
                    elif isinstance(operand, ast.Constant) and isinstance(
                        operand.value, str
                    ):
                        found.add(operand.value)
            return frozenset(found)
    raise AssertionError(
        "RunProjection._consume_unchecked not found: the source pin has nothing "
        "to read, and a gate that measures nothing is worse than no gate"
    )


PROJECTED: FrozenSet[str] = _projected_event_kinds()


def _producer_event_kinds() -> FrozenSet[str]:
    """Return the producer's own closed vocabulary, or skip if unimportable."""
    try:
        import harness.agent_loop_step as producer
    except Exception as exc:  # pragma: no cover - a broken import is a finding
        pytest.skip(f"the producer's event vocabulary is unreadable: {exc}")
    return frozenset(getattr(producer, "EVENT_KINDS", ()) or ())


PRODUCER: FrozenSet[str] = _producer_event_kinds()


# --------------------------------------------------------------------------
# the gate, and the proof that it can fire
# --------------------------------------------------------------------------
class TestTheClassificationIsComplete:
    """Every kind has a NAME and a REASON. Neither without the other."""

    def test_the_gate_is_green_on_this_tree(self):
        runview.assert_event_dispositions_are_complete()

    def test_the_gate_fires_when_a_declared_kind_has_no_reason(self):
        """CONTROL ARM. A gate nobody knows the sensitivity of is not read.

        The registry is mutated for the duration and restored from a COPY -
        an earlier round in this package saved an alias instead of a copy and
        its ``finally`` restored the broken table, turning one control arm
        into fourteen unrelated failures.
        """
        saved = dict(runview.EVENT_DISPOSITIONS)
        try:
            runview.EVENT_DISPOSITIONS.pop("verify_skipped", None)
            with pytest.raises(AssertionError) as caught:
                runview.assert_event_dispositions_are_complete()
            assert "verify_skipped" in str(caught.value)
        finally:
            runview.EVENT_DISPOSITIONS.clear()
            runview.EVENT_DISPOSITIONS.update(saved)
        runview.assert_event_dispositions_are_complete()

    def test_the_gate_fires_when_a_whole_visual_kind_goes_unclassified(self):
        """CONTROL ARM, second direction: the error names the missing row."""
        saved = dict(runview.EVENT_DISPOSITIONS)
        try:
            for name in runview.EVENT_VOCABULARY["run_control"]:
                runview.EVENT_DISPOSITIONS.pop(name, None)
            with pytest.raises(AssertionError) as caught:
                runview.assert_event_dispositions_are_complete()
            for name in runview.EVENT_VOCABULARY["run_control"]:
                assert name in str(caught.value)
        finally:
            runview.EVENT_DISPOSITIONS.clear()
            runview.EVENT_DISPOSITIONS.update(saved)
        runview.assert_event_dispositions_are_complete()

    def test_every_reason_is_written_and_is_not_a_restatement(self):
        """A reason of ``""``, ``"tbd"``, or the kind's own name is no reason.

        Deliberately NOT a length threshold. An earlier draft of this test
        demanded 40 characters, which failed twenty-one correct rows - every
        ``legacy spelling of X`` alias - and the only way to satisfy it would
        have been padding. That is the "make the gate green by writing more
        words" failure this package records repeatedly, and a length bar is
        the wrong instrument: what catches an unwritten row is the absence of
        content, not the absence of characters.

        The four real failure modes are a placeholder, an empty string, a bare
        noun phrase, and a restatement of the kind's own name.
        """
        placeholders = {"", "tbd", "todo", "?", "n/a", "none", "xxx", "..."}
        problems: List[str] = []
        for name, reason in runview.EVENT_DISPOSITIONS.items():
            text = reason.strip()
            if text.lower() in placeholders:
                problems.append(f"{name}: placeholder {reason!r}")
                continue
            if len(text) < 20:
                problems.append(f"{name}: bare fragment {reason!r}")
                continue
            normalised = "".join(ch for ch in text.lower() if ch.isalnum())
            kind_normalised = "".join(ch for ch in name.lower() if ch.isalnum())
            if normalised in {kind_normalised, kind_normalised + "event"}:
                problems.append(f"{name}: restates its own name ({reason!r})")
        assert not problems, f"classifications without a written reason: {problems}"
    def test_a_legacy_alias_reason_says_which_row_it_is_an_alias_of(self):
        """The twenty-one terse rows are terse because they name their twin.

        A legacy spelling earns its place in the table only if a reader can
        tell which row it duplicates, so that is asserted structurally rather
        than by asking every one of them to be wordy.
        """
        terse = [
            name
            for name, reason in runview.EVENT_DISPOSITIONS.items()
            if reason.strip().lower().startswith("legacy spelling of")
        ]
        assert len(terse) >= 20, (
            "the legacy-alias reasons disappeared; this gate is measuring "
            f"nothing now ({terse})"
        )
        for name in terse:
            reason = runview.EVENT_DISPOSITIONS[name]
            assert " of " in reason, f"{name}: alias with no target named"

    def test_no_reason_may_claim_something_the_table_disagrees_with(self):
        """A reason saying "no user-visible change" on a RENDERED kind.

        This is the failure the derivation exists to make impossible: a kind
        with a visual kind and a reason saying it is only counted is a
        contradiction a reader would resolve by believing the prose.
        """
        contradictions = []
        for name, reason in runview.EVENT_DISPOSITIONS.items():
            disposition, _ = runview._event_disposition(name)
            lowered = reason.lower()
            if disposition == runview.DISPOSITION_SHOWN and "no user-visible" in lowered:
                contradictions.append((name, reason))
        assert not contradictions, f"shown kinds whose reason denies it: {contradictions}"


class TestEveryAuthorityAgrees:
    """Producer, declaration tables, and registry - one vocabulary."""

    def test_every_producer_kind_is_classified(self):
        unclassified = sorted(PRODUCER - runview._classified_event_kinds())
        assert not unclassified, f"producer kinds with no classification: {unclassified}"

    def test_the_names_this_round_added_are_real_producer_kinds(self):
        """The debt list is read from the producer, not asserted by the author."""
        assert set(PROJECTIONS_ADDED_THIS_ROUND) <= PRODUCER, (
            "the kinds this round claims to have projected are not all in the "
            "producer's closed vocabulary: "
            f"{sorted(set(PROJECTIONS_ADDED_THIS_ROUND) - PRODUCER)}"
        )

    def test_every_shown_kind_is_handled_by_the_projection(self):
        """DIRECTION ONE. A name a surface is told it can render, rendered."""
        shown = {name for names in runview.EVENT_VOCABULARY.values() for name in names}
        unhandled = sorted(shown - PROJECTED)
        assert not unhandled, (
            "the vocabulary promises a surface can render these and the "
            f"projection does not: {unhandled}"
        )

    def test_every_handled_kind_is_declared(self):
        """DIRECTION TWO. A rendered row, declared. The inverse drift.

        Undeclared-but-handled kinds were classified INFORMATIONAL while being
        rendered, so the registry claimed a dropped row was a shown one. Nothing
        anywhere checked this direction, which is why twenty-seven kinds
        reached the tree.
        """
        undeclared = sorted(PROJECTED - runview._declared_event_kinds())
        assert not undeclared, (
            f"the projection folds these but no table declares them: {undeclared}"
        )

    def test_the_three_drift_lists_are_actually_closed(self):
        """The measured before-state, named, so this cannot quietly regress."""
        assert not set(DECLARED_BUT_UNHANDLED_BEFORE) - PROJECTED
        assert not set(PROJECTIONS_ADDED_THIS_ROUND) - PROJECTED
        assert not set(HANDLED_BUT_UNDECLARED_BEFORE) - runview._declared_event_kinds()

    def test_a_kind_is_never_claimed_by_two_visual_kinds(self):
        """One event, one visual kind. (Mirrors the T5-owned pin's rule.)"""
        seen: Dict[str, str] = {}
        for kind, names in runview.EVENT_VOCABULARY.items():
            for name in names:
                assert seen.setdefault(name, kind) == kind, (
                    f"{name} is declared under both {seen.get(name)} and {kind}"
                )

    def test_the_legacy_hook_vocabulary_is_not_a_journal_vocabulary(self):
        """`extensions.hook_events.LEGACY_EVENTS` is a DIFFERENT stream.

        It was briefly a candidate authority for this pin and it is not one:
        those are Claude Code hook event names (`PreToolUse`, `SessionStart`),
        they never reach a journal, and treating them as event kinds would have
        added seven phantom classifications that could never be exercised.
        """
        try:
            import extensions.hook_events as hooks
        except Exception:
            pytest.skip("the hook vocabulary is unreadable on this tree")
        legacy = set(getattr(hooks, "LEGACY_EVENTS", ()) or ())
        if not legacy:
            pytest.skip("this tree declares no hook vocabulary")
        assert not legacy & runview._declared_event_kinds(), (
            "hook names have leaked into the journal vocabulary"
        )


# --------------------------------------------------------------------------
# the projections themselves - driven, not read
# --------------------------------------------------------------------------
def _feed(projection: runview.RunProjection, kind: str, **data) -> None:
    projection.consume(
        {
            "event": kind,
            "payload": data,
            "timestamp": float(projection.events + 1),
            "sequence": projection.events + 1,
            "task_id": projection.task_id or "p3bw2",
        }
    )


def _project(*rows) -> runview.RunProjection:
    projection = runview.RunProjection(task_id="p3bw2")
    for kind, data in rows:
        _feed(projection, kind, **(data or {}))
    return projection


class TestTheEnvironmentAndProviderFailuresAreVisible:
    def test_an_unavailable_environment_says_so_in_its_own_words(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("environment_unavailable", {"error": "docker daemon not reachable"}),
        )
        assert "environment unavailable" in projection.current_action
        assert projection.last_error == "docker daemon not reachable"
        assert any(
            "environment unavailable" in w for w in projection.projection_warnings
        )

    def test_a_provider_failure_reaches_the_error_row(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("model_failure", {"error": "litellm.APIConnectionError"}),
        )
        assert projection.last_error == "litellm.APIConnectionError"
        assert projection.unmapped_kinds == {}

    def test_a_model_recovery_is_visible_not_only_the_failure(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("model_failure", {"error": "boom"}),
            ("model_recovery", {"reason": "second provider"}),
        )
        assert "retrying" in projection.current_action


class TestPolicyRefusalIsNotAApproval:
    """A reader hunting for a decision to revoke will not find one."""

    def test_a_policy_refusal_is_its_own_approval_state(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("policy_refused", {"tool": "bash", "reason": "floor rule: rm -rf"}),
        )
        assert projection.approval == "refused by policy"
        assert "refused by policy" in projection.current_action
        assert "bash" in projection.current_action
        assert any("floor rule" in w for w in projection.projection_warnings)

    def test_a_policy_refusal_does_not_render_as_waiting_for_a_human(self):
        """The exact inversion: `waiting` sends a reader to wait forever."""
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("policy_refused", {"tool": "bash", "reason": "floor rule"}),
        )
        assert projection.approval != "waiting"
        assert "waiting for approval" not in projection.current_action

    def test_a_real_approval_still_waits(self):
        """The control arm: the previous behaviour was not broken to pass this."""
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("approval_required", {"tool": "bash", "scope": "once"}),
        )
        assert projection.approval == "waiting"
        assert "waiting for approval" in projection.current_action


class TestVerificationSkippedCannotBecomeVerified:
    """The highest-consequence kind in the table.

    `verify_skipped` is a record of the ABSENCE of a check. A run that reports
    `completed_verified` after one is the defect this whole package keeps
    finding in new places, and it is the reason this row was the last of the
    ten to be given a projection rather than the first to be declared
    informational.
    """

    def test_a_skipped_verification_is_not_verified(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("verify_skipped", {"reason": "read-only mode"}),
        )
        assert projection.verification_status != "verified"
        assert projection.verification_status == "unknown"

    def test_a_skipped_verification_carries_its_reason_and_its_absence(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("verify_skipped", {"reason": "read-only mode"}),
        )
        record = projection.verification_evidence[-1]
        assert record["skipped"] is True
        assert record["reason"] == "read-only mode"
        assert record["target_passed"] is None
        assert record["regression_passed"] is None

    def test_a_skipped_verification_warns_that_the_run_is_not_verified(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("verify_skipped", {"reason": "no docker"}),
        )
        assert any("NOT verified" in w for w in projection.projection_warnings)

    def test_a_run_that_finished_verified_over_a_skip_reads_unverified(self):
        """CONTROL ARM, the whole point: `stop` says verified, the row says no.

        The terminal row is trusted for the STATUS and the verification
        evidence is trusted for the VERDICT, and they disagree here. If the
        projection ever resolves this to `completed_verified`, the skip row
        stopped meaning anything.
        """
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("verify_skipped", {"reason": "read-only mode"}),
            ("stop", {"status": "completed_verified"}),
        )
        snapshot = projection.snapshot()
        assert snapshot["status"] == "completed_unverified"
        assert snapshot["verification_state"] != "verified"

    def test_a_genuine_clean_verification_still_verifies(self):
        """CONTROL ARM, other direction: the honest path is not broken."""
        projection = _project(
            ("task_start", {"issue": "x"}),
            (
                "verify",
                {
                    "target_passed": True,
                    "regression_passed": True,
                    "flaky": False,
                },
            ),
            ("stop", {"status": "completed_verified"}),
        )
        snapshot = projection.snapshot()
        assert snapshot["verification_state"] == "verified"
        assert snapshot["status"] == "completed_verified"

    def test_a_skipped_verification_never_enters_the_verified_file_marking(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("edit_applied", {"path": "a.py"}),
            ("verify_skipped", {"reason": "no docker"}),
        )
        assert all(
            not change.get("verified") for change in projection.file_changes.values()
        )


class TestTheControlSignalsAreNotFailures:
    """A cap warning beside a crash teaches a reader to ignore the class."""

    def test_a_cap_warning_is_a_warning_not_an_error(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("turn_cap_approaching", {"cap": "max_turns 40", "current": 38}),
        )
        assert "approaching cap" in projection.current_action
        assert projection.last_error == ""
        assert any("max_turns 40" in w for w in projection.projection_warnings)

    def test_a_loop_guard_is_a_warning_not_an_error(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("loop_guard", {"reason": "same tool three times"}),
        )
        assert "loop guard" in projection.current_action
        assert projection.last_error == ""
        assert any("same tool three times" in w for w in projection.projection_warnings)

    def test_a_steering_delivery_is_visible(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("steering_delivered", {"message": "try the other branch"}),
        )
        assert projection.current_action == "steering message delivered"
        assert projection.unmapped_kinds == {}

    def test_a_steering_abort_is_resumable_not_torn_down(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("steering_abort", {"reason": "user asked to stop"}),
        )
        assert projection.status == "cancelled"
        assert "resumable" in projection.current_action
        assert projection.approval_note == "user asked to stop"

    def test_the_control_signals_are_not_in_the_error_visual_kind(self):
        """A structural pin: the taxonomy cannot drift back without it."""
        error_names = set(runview.EVENT_VOCABULARY["error_retry"])
        control_names = set(runview.EVENT_VOCABULARY["run_control"])
        assert not error_names & control_names
        for name in ("loop_guard", "turn_cap_approaching", "steering_abort"):
            assert name in control_names


class TestTheLongDeclaredKindsNowRender:
    """The eleven rows the vocabulary promised and the fold never delivered."""

    def test_a_batched_call_reports_every_tool_and_the_batch_size(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            (
                "batch_call",
                {
                    "calls": [
                        {"tool": "read"},
                        {"tool": "edit", "arguments": {"path": "a.py"}},
                    ]
                },
            ),
        )
        assert projection.visible_tools == ["edit", "read"]
        assert "2 tool(s)" in projection.current_action
        assert "a.py" in projection.changed_files
        assert projection.unmapped_kinds == {}

    def test_a_batched_call_of_one_is_still_a_tool_call(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("batch_call", {"calls": [{"tool": "read"}]}),
        )
        assert projection.current_action == "running read"

    def test_a_run_receipt_is_a_model_call(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("run_receipt", {"usage": {"total_tokens": 12}}),
        )
        assert projection.model_calls == 1
        assert projection.tokens == 12

    def test_a_web_fetch_is_a_read_and_not_a_mutation(self):
        """A fetched page mutates no local file.

        It was declared under BOTH `file_read` and `patch_edit`, and the T5
        pin correctly refused one event mapping to two visual kinds. Filing a
        network read under the mutation kind is what would have made a fetch
        read as an edit in the changed-files panel.
        """
        names = [name for names in runview.EVENT_VOCABULARY.values() for name in names]
        assert names.count("web_fetch") == 1
        assert "web_fetch" not in runview.EVENT_VOCABULARY["patch_edit"]
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("web_fetch", {"url": "https://example.test/spec"}),
        )
        assert "https://example.test/spec" in projection.current_action
        assert projection.changed_files == []

    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("qa_read", "reading"),
            ("recall", "reading"),
            ("docs_lookup", "reading"),
            ("file_read", "reading"),
        ],
    )
    def test_the_read_family_renders_as_a_read(self, kind, expected):
        projection = _project(("task_start", {"issue": "x"}), (kind, {"path": "a.py"}))
        assert projection.current_action.startswith(expected)
        assert projection.unmapped_kinds == {}

    def test_a_symbol_read_renders_as_a_search(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("read_symbol", {"symbol": "RunProjection"}),
        )
        assert "RunProjection" in projection.current_action

    def test_a_reflection_lands_on_the_reasoning_surface(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("reflection", {"summary": "the guard is the wrong shape"}),
        )
        assert projection.current_action == "self-critique"
        assert "guard is the wrong shape" in projection.answer

    @pytest.mark.parametrize("kind", ["stop", "finish"])
    def test_the_kernel_spellings_of_a_finished_run_fold(self, kind):
        """`stop` is why every kernel run reported an UNMAPPED last row."""
        projection = _project(
            ("task_start", {"issue": "x"}), (kind, {"status": "completed"})
        )
        assert projection.unmapped_kinds == {}
        assert projection.status.startswith("completed")

    def test_an_approval_error_is_reachable_and_is_an_error(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("approval_error", {"error": "gate unreadable"}),
        )
        assert projection.last_error == "gate unreadable"
        assert projection.unmapped_kinds == {}

    def test_a_checkpoint_warning_belongs_to_the_checkpoint_visual_kind(self):
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("checkpoint_warning", {"error": "partial write"}),
        )
        assert "checkpoint" in projection.current_action
        assert any("partial write" in w for w in projection.projection_warnings)


# --------------------------------------------------------------------------
# the diagnostic surface - unknown kinds stay visible
# --------------------------------------------------------------------------
class TestTheUnmappedDiagnostic:
    def test_a_kind_this_build_never_heard_of_is_reported_as_such(self):
        projection = _project(
            ("task_start", {"issue": "x"}), ("a_kind_from_the_future", {"n": 1})
        )
        lines = runview.unmapped_event_lines(projection.snapshot())
        assert any("UNMAPPED EVENT KIND" in line for line in lines)
        assert any(
            "a_kind_from_the_future" in line and "unclassified" in line
            for line in lines
        )

    def test_a_kind_this_build_dropped_still_reaches_the_snapshot(self):
        projection = _project(("a_kind_from_the_future", {}))
        snapshot = projection.snapshot()
        assert snapshot["unmapped_kinds"] == ["a_kind_from_the_future"]
        assert snapshot["unmapped_events"] == 1
        assert snapshot["unclassified_kinds"] == ["a_kind_from_the_future"]

    def test_a_classified_kind_reports_classification_known(self):
        """CONTROL ARM: the two labels are distinguishable, not one number."""
        projection = _project(("a_kind_from_the_future", {}))
        detail = projection.snapshot()["unmapped_details"][0]
        assert detail["classification_known"] is False
        assert detail["disposition"] == runview.DISPOSITION_UNCLASSIFIED

        disposition, reason = runview._event_disposition("verify_skipped")
        assert disposition == runview.DISPOSITION_SHOWN
        assert "VERIFIER DID NOT RUN" in reason

    def test_the_diagnostic_is_empty_when_nothing_was_dropped(self):
        """A block that always renders something is a block nobody reads."""
        projection = _project(
            ("task_start", {"issue": "x"}),
            ("tool_call", {"tool": "read", "path": "a.py"}),
        )
        assert runview.unmapped_event_lines(projection.snapshot()) == []

    def test_the_diagnostic_states_the_total_and_the_omission(self):
        projection = _project(
            *[(f"future_{index}", {}) for index in range(6)]
        )
        snapshot = projection.snapshot()
        lines = runview.unmapped_event_lines(snapshot, limit=2)
        assert any("6 journal row(s)" in line for line in lines)
        assert any("and 4 more kind(s)" in line for line in lines)

    def test_the_diagnostic_bounds_to_width_and_marks_the_cut(self):
        projection = _project(("a_kind_from_the_future", {}))
        for width in (60, 78, 100, 200):
            for line in runview.unmapped_event_lines(
                projection.snapshot(), width=width
            ):
                assert len(line) <= width, f"line overflows {width}: {line!r}"

    def test_the_diagnostic_names_the_registry_as_the_place_to_add_a_kind(self):
        """An unmapped row must be actionable, not merely reported.

        The first draft of this test asserted ``"EVENT_DISPOSITIONS" in
        runview.__doc__ or True`` - an assertion that cannot fail and an
        ``or True`` ruff then correctly flagged. Both halves were the problem:
        the registry's name is in the DIAGNOSTIC vocabulary's docstring and in
        the module docstring above, and a gate that asserts a docstring
        contains a word measures the docstring. What it should assert is that
        the door a reader is pointed at actually exists and actually fires.
        """
        assert runview.UNMAPPED_EVENT_DIAGNOSTIC == "UNMAPPED EVENT KIND"
        assert callable(runview.assert_event_dispositions_are_complete)
        assert callable(runview._event_disposition)
        assert callable(runview._unclassified_event_kinds)
        # And the door works from a reader's position, not only from in here.
        assert runview._unclassified_event_kinds() == []


class TestTheDiagnosticIsMountedOnTheRail:
    """The renderer is REACHED, not merely importable.

    ``cli/tui.py`` used to hand-roll this block: a `[:44]`-clipped list of
    kind names with no count, no reason, and no way to tell a vocabulary gap
    from a projection bug. Re-pointing it at the one renderer is the fix, and
    a fix nobody can observe from a terminal is a fix in a comment - so this
    drives the real mounted rail.

    The rail is Textual, so this is the one host-only test in the file that
    costs a mount. It is worth the seconds: it is the only assertion here that
    would notice the mount being reverted while every projection test stayed
    green.
    """

    def _rendered_grid(self, tmp_path) -> str:
        """Return the plain text a person at a terminal would actually SEE.

        Read from the COMPOSITOR, not from a widget. `str(widget.renderable)`
        is the markup and reads back empty for this composed rail, and
        `tui_components.surface_text` returned '' for a `display: block`
        widget in the same run - so a widget-level assertion here would have
        reported "the rail renders nothing" over a diagnostic that was plainly
        on screen. Reading the rendered strips is the method this package's own
        terminal-UX rounds settled on for exactly that reason.
        """
        import asyncio

        from cli import tui as _tui

        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)

        async def drive():
            app = _tui.NeoApp(
                repo=repo,
                log_root=tmp_path / "logs",
                state={
                    "model": None,
                    "provider": None,
                    "plan_preview": None,
                    "quiet": False,
                    "repo": str(repo),
                    "file_config": {},
                },
                file_config={},
                version="9.9.9",
            )
            async with app.run_test(size=(140, 60)) as pilot:
                run = _tui._RunState(task_id="p3bw2")
                for seq, kind in (
                    (1, "task_start"),
                    (2, "a_kind_from_the_future"),
                ):
                    run.projection.consume(
                        {
                            "event": kind,
                            "payload": {"issue": "x"} if seq == 1 else {},
                            "timestamp": float(seq),
                            "sequence": seq,
                            "task_id": "p3bw2",
                        }
                    )
                app._run = run
                app._render_side(run)
                await pilot.pause()
                strips = app.screen._compositor.render_strips()
                return "\n".join(
                    "".join(segment.text for segment in strip) for strip in strips
                )

        return asyncio.run(drive())

    def test_the_kind_reaches_the_visible_grid(self, tmp_path):
        grid = self._rendered_grid(tmp_path)
        assert "UNMAPPED EVENT KIND" in grid, (
            f"the rail did not render the diagnostic; grid tail {grid[-400:]!r}"
        )
        # The rail's real slot is ~30 columns after its own `journal` prefix, so
        # the KIND NAME is bounded and marked. What must survive whole is the
        # LABEL, because that is what distinguishes a vocabulary gap from a bug
        # in this package, and the bound must be visible rather than silent.
        assert "unclassified" in grid, "the label did not survive to screen"
        assert "a_kind_from" in grid, "the kind name was lost entirely"
        row = next(line for line in grid.splitlines() if "a_kind_from" in line)
        assert "…" in row or "..." in row, (
            f"the kind name was truncated without a marker: {row!r}"
        )

    def test_the_row_reports_where_the_full_list_lives(self, tmp_path):
        """A bounded row must still say where the rest of it is."""
        grid = self._rendered_grid(tmp_path)
        assert "/trace" in grid, (
            "a truncated diagnostic with no pointer to the full list is a "
            "silent truncation"
        )

    def test_the_old_clipped_wording_is_gone(self, tmp_path):
        """The old block cut the kind list at 44 characters, unmarked."""
        grid = self._rendered_grid(tmp_path)
        assert "unreadable event(s)" not in grid, (
            "the old clipped wording is still on screen: two renderers of one fact"
        )

    def test_the_rendered_row_is_not_overflowing_its_columns(self, tmp_path):
        """The diagnostic is bounded to the rail's OWN measured width.

        ``_rail_measurement`` is the authority for that width, and calling it
        without ``self`` was the first version of this mount - ruff's F821
        caught a NameError that no projection test would ever have reached.
        """
        grid = self._rendered_grid(tmp_path)
        for line in grid.splitlines():
            if "UNMAPPED" in line or "a_kind_from" in line:
                assert len(line.rstrip()) <= 140, f"row overflows the viewport: {line!r}"


class TestTheDispositionsAreDerived:
    """A third table answering the same question would defeat the registry."""

    def test_shown_and_informational_are_derived_not_declared(self):
        assert runview._event_disposition("verify_skipped")[0] == runview.DISPOSITION_SHOWN
        assert (
            runview._event_disposition("skills")[0] == runview.DISPOSITION_INFORMATIONAL
        )
        assert (
            runview._event_disposition("a_kind_from_the_future")[0]
            == runview.DISPOSITION_UNCLASSIFIED
        )

    def test_a_kind_in_both_tables_is_rendered(self):
        """The rule, checked over the whole vocabulary rather than by example.

        There is deliberately no kind in both tables on this tree - the two
        tables answer different questions and an overlap would be the drift
        this module exists to catch. So the rule is asserted over every name
        rather than against one convenient overlap, which is also what would
        catch somebody ADDING an overlap later.
        """
        shown = {name for names in runview.EVENT_VOCABULARY.values() for name in names}
        for name in shown:
            disposition, reason = runview._event_disposition(name)
            assert disposition == runview.DISPOSITION_SHOWN, (
                f"{name} has a visual kind but classifies as {disposition}"
            )
            assert reason
        for name in runview.INFORMATIONAL_EVENTS:
            if name in shown:
                continue
            assert (
                runview._event_disposition(name)[0] == runview.DISPOSITION_INFORMATIONAL
            ), f"{name} is only informational but does not classify as such"

    def test_every_classified_kind_reports_a_non_empty_reason(self):
        for name in runview._classified_event_kinds():
            _, reason = runview._event_disposition(name)
            assert reason, f"{name} classifies with no reason"

    def test_the_unclassified_helper_finds_nothing_on_this_tree(self):
        assert runview._unclassified_event_kinds() == []
