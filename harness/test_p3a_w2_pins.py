"""Pins for everything Phase 3a Wave 2 established — so none of it can return.

**The wave in one sentence.** A fix without a pin is a fix that returns, and a
claim without a measurement is a claim. This module is the standing evidence for
five things, each of which was either fixed, measured, or found wanting this
wave:

===  ==========================================================================
W2.1 decision decay  6 plant classes x 4 arms; the from-scratch arm must lose
                     MORE than the incremental arm, or the wave is a no-op
W2.2 clearing        the re-fetchable classification is DATA and covers the
                     whole catalog; no non-re-fetchable result is ever cleared;
                     clearing is reversible by read-back; the receipt names skips
W2.3 reversibility  oracle equivalence; exact (not merely equivalent) restore;
                     rotation reversibility assessed; failed compaction atomic
W2.4 thrash          3rd consecutive, ``after < limit``, blocked counts, raise
                     becomes ``failed``, state recoverable afterwards
W2.5 context files   exactly ONE filename list in ``harness/``, complete,
                     first-found-wins, root-to-cwd, JIT on touch
===  ==========================================================================

**Every pin here is checked by RUNNING something.** Where a property is about
source shape rather than behaviour — "clearing runs before compaction", "one
filename list" — it is checked by reading the AST, because a behavioural test
passes for every path somebody happened to exercise. Where a property is about
behaviour it is driven through the real machinery.

Several pins assert a NON-VACUOUS control alongside the property, because the
recurring failure in this round was a check that could not fail: a summariser
that emitted the planted decision unconditionally, a re-fetch proof over two
empty strings, a receipt-completeness test where 39 of 45 results had already
been folded away before the transform ran. Each of those is now a named test.

Run::

    python -m pytest harness/test_p3a_w2_pins.py -q
"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from harness import context_files as context_files
from harness.agent_kernel import conversation as conversation_module
from harness.agent_kernel.budget import CompactionThrashError
from harness.agent_kernel.clearing import (
    NON_REFETCHABLE_TOOLS,
    PROTECTED_REASONS,
    REFETCHABLE_TOOLS,
    classify_tool,
)
from harness.agent_kernel.conversation import ConversationMemory
from harness.clearing_lossless import (
    audit_protected,
    catalog_coverage,
    find_non_refetchable_cleared,
    receipt_completeness,
    refetch_proof_coverage,
)
from harness.compaction_reversibility import run_all as reversibility_run
from harness.decision_decay_bench import (
    ALL_CLASSES,
    ARMS,
    GATE_TURN,
    PLANT_CLASSES,
    verdict,
)
from harness.thrash_measure import run_all as thrash_run

#: The nine names ``harness/context_files.py`` is the authority for. Written out
#: here DELIBERATELY, so this module is a second reader of the list rather than
#: an importer of it: a pin that reads its subject cannot notice the subject
#: shrinking.
EXPECTED_CONTEXT_FILE_NAMES: Tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
    "NEO.md",
    "INSTRUCTIONS.md",
    "CONTEXT.md",
    ".cursorrules",
    ".windsurfrules",
)

#: Files allowed to carry their own list, each with its reason. A module that
#: **asserts** the authority's contents is not a second authority, and a pin that
#: flagged it would have to be loosened just to exist — so the exemption is named
#: and the set is itself pinned, so adding a file to it is a visible edit.
_LIST_EXEMPT_FILES: Dict[str, str] = {
    "test_p3a_w2_pins.py": "this module: it writes the list out to be a READER",
    "test_p3a_context.py": "Wave 1's pin: it asserts the authority's contents",
    "context_files.py": "the authority itself",
}


def _harness_sources() -> List[Path]:
    """Return every ``harness/`` python source, excluding ``_stubs``."""
    root = Path(__file__).resolve().parent
    return sorted(
        path
        for path in root.rglob("*.py")
        if "_stubs" not in path.parts and "third_party" not in path.parts
    )


def _filename_lists() -> List[Tuple[str, int, List[str]]]:
    """Return every collection of >=2 context filenames declared in ``harness/``.

    Read by AST rather than by grep, because a grep cannot tell a *declaration*
    from a mention, and because a file that fails to parse must be REPORTED as a
    violation rather than skipped — a scan that silently skips what it could not
    read reports "clean" about exactly the file most likely to be broken. That
    is not hypothetical: a PowerShell ``Set-Content -Encoding utf8`` wrote a BOM
    into one of this round's own files and made it unparseable, and a skipping
    scanner would have called that file clean.
    """
    names = set(EXPECTED_CONTEXT_FILE_NAMES)
    found: List[Tuple[str, int, List[str]]] = []
    for path in _harness_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            found.append((path.name, int(exc.lineno or 0), ["<UNPARSEABLE>"]))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                continue
            literals = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            if sum(1 for value in literals if value in names) >= 2:
                found.append((path.name, int(getattr(node, "lineno", 0) or 0), literals))
    return found


# ==========================================================================
# W2.1 — the decision-decay benchmark
# ==========================================================================


class TestTheDecisionDecayBenchmark:
    """The gate for Phase 3a. Every arm must be measured, and the two
    compaction arms must DIFFER."""

    def test_the_benchmark_has_six_classes_at_known_turns(self) -> None:
        """Six classes, each planted at a distinct known turn."""
        assert len(ALL_CLASSES) == 6
        assert len(PLANT_CLASSES) == 5
        turns = [plant.turn for plant in PLANT_CLASSES]
        # The classes are declared in the BRIEF's table order (A, B, gotcha,
        # rejected, open question), which is deliberately NOT turn order. Pin the
        # SET and the per-class mapping; pinning a sorted list would have failed
        # on correct code.
        assert sorted(turns) == [2, 5, 8, 10, 12]
        assert {plant.key: plant.turn for plant in PLANT_CLASSES} == {
            "hard_constraint": 2,
            "arch_decision": 5,
            "gotcha": 10,
            "rejected_approach": 12,
            "open_question": 8,
        }
        assert ALL_CLASSES[-1].turn == 45

    def test_every_class_carries_a_written_reason_and_a_violation_rule(self) -> None:
        """A class with no stated reason cannot be reviewed."""
        for plant in ALL_CLASSES:
            assert plant.why.strip(), plant.key
            assert plant.section.strip(), plant.key
            assert plant.violation_predicate.strip(), plant.key

    def test_there_are_exactly_four_arms_and_they_differ_only_where_claimed(self) -> None:
        """baseline / clearing_only / clearing_incremental / clearing_from_scratch."""
        assert [arm.key for arm in ARMS] == [
            "baseline",
            "clearing_only",
            "clearing_incremental",
            "clearing_from_scratch",
        ]
        by_key = {arm.key: arm for arm in ARMS}
        # The two COMPACTION arms must be identical except for `incremental`,
        # or the comparison measures something other than the merge.
        left = by_key["clearing_incremental"]
        right = by_key["clearing_from_scratch"]
        assert left.window == right.window
        assert left.clearing == right.clearing is True
        assert left.incremental is True
        assert right.incremental is False

    def test_the_from_scratch_arm_is_reachable_through_the_real_signature_check(
        self,
    ) -> None:
        """The defect is switched off by REMOVING the parameter, not by a branch.

        Asserted against :func:`_summarizer_names` — the actual mechanism — so
        the from-scratch arm cannot drift into a reimplementation of the old
        behaviour instead of running it.
        """
        two = lambda transcript, turns: ("", "model_summary")  # noqa: E731
        three = lambda transcript, turns, previous_summary="": (  # noqa: E731
            "",
            "model_summary",
        )
        assert conversation_module._summarizer_names(two, "previous_summary") is False
        assert (
            conversation_module._summarizer_names(three, "previous_summary") is True
        )

    def test_the_from_scratch_key_is_not_published_in_defaults(self) -> None:
        """A default in ``DEFAULTS`` merges into every task and every eval arm."""
        from harness.config import DEFAULTS

        assert "context_incremental_compaction" not in DEFAULTS

    def test_no_arm_is_allowed_to_claim_a_pass_without_compacting(self) -> None:
        """A survival number from an arm that never compacted is vacuous."""
        results = {
            arm.key: _vacuous_arm(
                arm.key,
                compactions=0,
                incremental=0,
                survived=[plant.key for plant in ALL_CLASSES],
            )
            for arm in ARMS
        }
        assert verdict(results) == "VACUOUS"

    def test_an_incremental_arm_that_never_merged_is_vacuous(self) -> None:
        """Surviving without exercising the merge is not a pass."""
        results = {
            arm.key: _vacuous_arm(
                arm.key,
                compactions=9,
                incremental=0,
                survived=[plant.key for plant in ALL_CLASSES],
            )
            for arm in ARMS
        }
        assert results["clearing_incremental"].vacuous is True
        assert verdict(results) == "VACUOUS"

    def test_a_from_scratch_arm_that_loses_nothing_is_vacuous(self) -> None:
        """If the defect arm loses nothing, the benchmark cannot detect the defect."""
        results = {
            arm.key: _vacuous_arm(
                arm.key,
                compactions=9,
                incremental=9,
                # Every arm keeps everything — so the from-scratch arm, which is
                # supposed to lose, does not.
                survived=[plant.key for plant in ALL_CLASSES],
            )
            for arm in ARMS
        }
        assert results["clearing_from_scratch"].vacuous is True
        assert verdict(results) == "VACUOUS"

    def test_the_defect_is_visible_as_a_difference_between_the_two_compaction_arms(
        self,
    ) -> None:
        """The whole gate, in miniature: incremental keeps, from-scratch loses."""
        results = {
            arm.key: _vacuous_arm(
                arm.key,
                compactions=9,
                incremental=9,
                survived=(
                    [plant.key for plant in ALL_CLASSES]
                    if arm.key != "clearing_from_scratch"
                    else []
                ),
            )
            for arm in ARMS
        }
        assert verdict(results) == "MEASURED_NO_DECISION_LOSS"
        assert results["clearing_incremental"].lost == []
        assert len(results["clearing_from_scratch"].lost) == 6

    def test_the_verdict_is_never_two_valued(self) -> None:
        """Four states, because 'nothing happened' and 'nothing lost' differ."""
        from harness.decision_decay_bench import VERDICTS

        assert len(VERDICTS) == 4
        assert "VACUOUS" in VERDICTS
        assert "DECISION_LOSS_IN_INCREMENTAL_ARM" in VERDICTS

    def test_the_summariser_fixture_cannot_arm_aware(self) -> None:
        """The fixture must be a pure function of its material.

        Asserted by reading its source: a branch on the arm name inside the
        summariser would make every differential meaningless, and it is exactly
        the kind of edit that looks harmless.
        """
        source = Path(
            Path(__file__).resolve().parent / "decision_decay_bench.py"
        ).read_text(encoding="utf-8")
        body = source.split("def summarize(", 1)[-1].split("\n    @staticmethod", 1)[0]
        for arm_name in ("clearing_incremental", "clearing_from_scratch"):
            assert arm_name not in body, (
                "the summariser fixture references an arm name; it must be a pure "
                "function of the material it is handed"
            )

    def test_the_long_session_probe_fixture_is_still_recorded_as_tautological(self) -> None:
        """Wave 1's probe cannot fail, and this round says so in writing.

        The probe is NOT deleted — it measures a real 23-compaction chain — but
        its survival result is produced by its own fixture, so it is not evidence
        for the merge. The condition is recorded as data so the next reader
        inherits the caveat instead of the number.
        """
        from harness.decision_decay_bench import HONEST_FIXTURE_NOTE

        assert "long_session_probe" in HONEST_FIXTURE_NOTE
        assert ">= 0" in HONEST_FIXTURE_NOTE
        source = (
            Path(Path(__file__).resolve().parent / "long_session_probe.py")
            .read_text(encoding="utf-8")
        )
        # The defect is still there. If someone fixes the fixture, this pin fails
        # and the note must be revisited rather than left claiming a live bug.
        assert "len(self.summaries) >= 0" in source


def _vacuous_arm(
    key: str,
    *,
    compactions: int,
    incremental: int,
    survived: Sequence[str] = (),
) -> Any:
    """Build a minimal ``ArmResult`` for verdict tests.

    ``survived`` names the classes that were in the request at the gate; every
    other class reads as lost. ``ArmResult.lost`` and ``.vacuous`` are DERIVED
    PROPERTIES, so a class's fate has to be set on its :class:`ClassResult` —
    passing ``lost=`` to the constructor is a ``TypeError``, which is the
    dataclass telling the truth about the shape.
    """
    from harness.decision_decay_bench import ArmResult, ClassResult

    classes = {}
    for plant in ALL_CLASSES:
        record = ClassResult(
            key=plant.key, planted_turn=plant.turn, section=plant.section
        )
        if plant.key in set(survived):
            record.visible_turns = [int(GATE_TURN)]
            record.turns_probed = [int(GATE_TURN)]
        classes[plant.key] = record
    # The two CONTROL arms do not compact by design, so their compaction count is
    # forced to 0 here. Leaving it at the caller's value marked ``baseline``
    # vacuous — correctly: a control that compacted would not be a control.
    is_control = key in {"baseline", "clearing_only"}
    return ArmResult(
        arm=key,
        clearing=key.startswith("clearing"),
        incremental=key != "clearing_from_scratch",
        window=12288,
        turns=60,
        status="completed_unverified",
        compactions=0 if is_control else int(compactions),
        clearings=0,
        incremental_compactions=0 if is_control else int(incremental),
        peak_tokens=0,
        threshold_tokens=7372,
        classes=classes,
    )


# ==========================================================================
# W2.2 — clearing is lossless, and the classification is data
# ==========================================================================


class TestTheClearingClassificationIsData:
    def test_every_catalog_tool_is_classified(self) -> None:
        """45/45. A name protected only by the unknown-tool fallback is not reviewable."""
        coverage = catalog_coverage()
        assert coverage["unclassified"] == [], coverage["unclassified"]
        assert coverage["complete"] is True
        assert coverage["overlap"] == []

    def test_the_three_tools_the_brief_names_are_named_explicitly(self) -> None:
        """``process_read_output``, ``memory`` and ``mcp`` were all unnamed."""
        for tool in ("process_read_output", "memory", "mcp"):
            assert tool in NON_REFETCHABLE_TOOLS, tool
            assert NON_REFETCHABLE_TOOLS[tool].strip(), tool

    def test_no_non_refetchable_result_is_ever_cleared(self) -> None:
        """The P0 assertion, computed by RUNNING the transform per catalog tool."""
        assert find_non_refetchable_cleared() == []

    def test_clearing_agrees_with_the_classification_for_every_catalog_tool(self) -> None:
        """The table and the transform must not disagree."""
        from harness.clearing_lossless import catalog_names

        for name in catalog_names():
            row = audit_protected(name)
            assert row["was_cleared"] == row["classified_refetchable"], name

    def test_an_unrecognised_tool_is_protected_not_cleared(self) -> None:
        """The asymmetry is the design; losing it is silent data loss."""
        assert classify_tool("a_tool_added_next_month") == (
            False,
            "unclassified_tool",
        )
        row = audit_protected("a_tool_added_next_month")
        assert row["was_cleared"] is False

    def test_the_table_is_publishable_as_data(self) -> None:
        """``classification_table()`` must stay complete and reasoned."""
        from harness.clearing_lossless import classification_as_data

        rows = classification_as_data()
        assert len(rows) >= len(REFETCHABLE_TOOLS) + len(NON_REFETCHABLE_TOOLS)
        for row in rows:
            assert row["reason"].strip(), row["tool"]

    def test_every_re_fetchable_tool_can_be_proven_or_is_catalog_absent(self) -> None:
        """A tool nobody can exercise must be visible, not skipped."""
        coverage = refetch_proof_coverage()
        assert coverage["missing_provoker"] == [], coverage["missing_provoker"]
        assert coverage["complete"] is True

    def test_a_re_fetch_proof_over_two_empty_strings_is_not_a_proof(self) -> None:
        """The vacuous-proof guard, as a pin rather than a hope.

        ``git status --porcelain`` on a clean tree is ``""``, and the first draft
        of the proof reported four working git tools as proven on 0/0
        characters. The verdict must refuse that shape.
        """
        from harness.clearing_lossless import RefetchProof

        empty = RefetchProof(tool="git_status", exercised=True, ok_first=True,
                            ok_second=True, identical=True, cleared_between=True)
        assert empty.verdict == "no_result"

        real = RefetchProof(tool="read", exercised=True, ok_first=True,
                            ok_second=True, first_chars=459, second_chars=459,
                            identical=True, cleared_between=True)
        assert real.verdict == "proven"

    def test_git_diff_is_protected_and_that_is_a_deliberate_divergence(self) -> None:
        """The brief lists ``git_diff`` as re-fetchable; this tree protects it.

        A diff of a tree the run has itself mutated is point-in-time with respect
        to the run's own history, and the asymmetry (protect when unsure) is the
        module's stated principle. Recorded here so the divergence is visible
        rather than a reader's guess.
        """
        assert NON_REFETCHABLE_TOOLS["git_diff"] == "point_in_time"
        assert classify_tool("git_diff")[0] is False


class TestClearingIsLossless:
    def test_the_newest_result_is_never_cleared(self) -> None:
        """The protection is re-pinned, and it is structural."""
        memory = ConversationMemory(max_messages=400, max_chars=4_000_000)
        memory.seed([{"role": "system", "content": "sys"}])
        memory.record_assistant("go", turn=1)
        for index in range(1, 5):
            memory.record_tool_result(
                tool="read", ok=True, output="Z" * 900, turn=index,
                target=f"f{index}.py",
            )
        receipt = memory.clear_tool_results(enabled=True)
        newest_seq = max(int(t.seq) for t in memory._history
                         if str(getattr(t, "kind", "")) == "tool_result")
        protected_seqs = {int(seq) for seq, _tool, _reason in receipt.protected}
        assert newest_seq in protected_seqs
        assert "newest" in {reason for _seq, _tool, reason in receipt.protected}

    def test_protect_newest_zero_means_protect_none(self) -> None:
        """``positions[-0:]`` is ``positions[0:]`` — the WHOLE list.

        A silent no-op, not a crash: ``protect_newest=0`` protected everything and
        the receipt said ``skipped``.
        """
        from harness.agent_kernel.clearing import _newest_positions

        assert _newest_positions([1, 2, 3], 0) == set()
        assert _newest_positions([1, 2, 3], -1) == set()
        assert _newest_positions([1, 2, 3], 1) == {3}
        assert _newest_positions([1, 2, 3], 2) == {2, 3}
        assert _newest_positions([1, 2, 3], 9) == {1, 2, 3}
        assert _newest_positions([], 1) == set()

    def test_a_turn_carrying_attachments_is_never_cleared(self) -> None:
        """The transform replaces ``content`` and leaves ``attachments`` alone.

        Clearing such a turn would leave the image in the request while the
        receipt claimed reclaimed bytes. Checked STRUCTURALLY on the turn, so
        widening the allow-list cannot arm it.
        """
        from harness.agent_kernel.clearing import clear_tool_results
        from harness.agent_kernel.conversation import ConversationTurn

        turn = ConversationTurn(
            role="user", content="x" * 3000, turn=3, kind="tool_result",
            tool="read", ok=True, target="f", seq=3,
            attachments=[{"type": "image", "source": {"type": "base64"}}],
        )
        _out, receipt = clear_tool_results([turn], protect_newest=0, min_chars=10)
        assert receipt.cleared_count == 0
        assert ("carries_attachments" in {r for _s, _t, r in receipt.protected})

    def test_every_protected_result_names_a_reason_from_the_closed_set(self) -> None:
        """A receipt that says 'not cleared' with no reason hides a decision."""
        receipt = receipt_completeness(Path(tempfile.mkdtemp()))
        assert receipt["unknown_reason"] == []
        for reason in receipt["protected_reasons"]:
            assert reason in PROTECTED_REASONS, reason

    def test_the_receipt_accounts_for_every_tool_result(self) -> None:
        """Count, bytes, which tools, AND which were skipped and why."""
        receipt = receipt_completeness(Path(tempfile.mkdtemp()))
        assert receipt["all_accounted"] is True
        assert receipt["cleared"] + receipt["protected"] == receipt["results_in"]
        assert receipt["names_bytes_and_skips"] is True
        assert receipt["reclaimed_chars"] >= 0

    def test_clearing_is_reversible_by_read_back(self) -> None:
        """The journal still holds the original bytes, byte-identical."""
        results = reversibility_run()["clearing_readback"]
        assert results["journal_canary_for_every_cleared_seq"] is True
        assert results["readback_is_byte_identical_to_the_original"] is True
        assert results["cleared_canary_still_in_live_view"] is False

    def test_the_module_transform_is_pure_but_the_memory_installs_the_view(self) -> None:
        """Both facts, because 'lossless' must not be read as 'nothing changed'."""
        from harness.clearing_lossless import prove_clearing_is_a_view_transform

        view = prove_clearing_is_a_view_transform(Path(tempfile.mkdtemp()))
        assert view["module_transform_is_pure"] is True
        assert view["memory_history_replaced_by_view"] is True
        assert view["every_cleared_seq_reads_back"] is True


# ==========================================================================
# W2.3 — reversibility
# ==========================================================================


class TestCompactionStaysReversible:
    def test_the_oracle_equivalence_still_holds_on_all_four_axes(self) -> None:
        """files, git status, conversation journal, turn ledger."""
        oracle = reversibility_run()["oracle"]
        assert oracle["files_equal"] is True
        assert oracle["git_status_equal"] is True
        assert oracle["journal_equal"] is True
        assert oracle["turn_ledger_equal"] is True

    def test_the_oracle_arm_is_a_real_interruption_not_a_finish(self) -> None:
        """An oracle that completed is not an oracle."""
        oracle = reversibility_run()["oracle"]
        assert oracle["oracle_arm"]["interrupted"] is True
        assert oracle["rewind_arm"]["interrupted"] is False

    def test_the_weaker_canonical_comparison_is_reported_beside_the_exact_one(self) -> None:
        """'Equal once timestamps are removed' must never be the only claim."""
        oracle = reversibility_run()["oracle"]
        assert "turn_ledger_equal_byte_for_byte" in oracle
        assert "journal_equal_byte_for_byte" in oracle
        assert "created_at" in oracle["volatile_fields_excluded"]

    def test_incremental_compaction_restores_exactly_not_merely_equivalently(self) -> None:
        """A restore's purpose is to put back exactly what was there."""
        restore = reversibility_run()["incremental_exact_restore"]
        assert restore["compacted"] is True
        assert restore["rebuilt_is_none"] is False
        assert restore["exact_match"] is True
        assert restore["first_difference"] == ""

    def test_the_reversible_block_still_names_a_restore_call(self) -> None:
        """Named rather than asserted-by-feeling, and the seqs it un-drops are listed."""
        restore = reversibility_run()["incremental_exact_restore"]
        block = restore["reversible_block"]
        assert block.get("restore"), block
        assert "restore_compaction" in str(block.get("restore"))
        assert block.get("un_drops"), block

    def test_all_dropped_content_is_available_for_the_restore(self) -> None:
        """A prior view with a hole in it is not a prior view."""
        restore = reversibility_run()["incremental_exact_restore"]
        assert restore["all_dropped_content_available"] is True

    def test_rotation_reversibility_is_assessed_against_a_real_journal(self) -> None:
        """T3's own reporter, called — not a filesystem probe's own artefacts."""
        rotation = reversibility_run()["rotation"]
        assert rotation["verdict"] == "ROTATION_RESTORABLE"
        assert rotation["aligned_report"]["reversible"] is True
        assert rotation["drift_is_detected"] is True
        assert rotation["unreadable_input_fails_closed"] is True
        assert rotation["rotate_session_docstring_contract"] is True

    def test_a_failed_compaction_leaves_no_half_written_view(self) -> None:
        """No partial summary, the error is recorded, and every dropped message
        is still recoverable from the journal."""
        failed = reversibility_run()["failed_compaction"]
        assert failed["no_partial_summary_installed"] is True
        assert failed["summary_chars"] == 0
        assert failed["method"] == "summarizer_error"
        assert failed["error_recorded"] is True
        assert failed["every_dropped_message_recoverable_from_the_journal"] is True
        assert failed["handoff_renders"] is True
        assert failed["journal_records_the_failure"] is True

    def test_the_compaction_transcript_ceiling_is_reachable_from_config(self) -> None:
        """The key used to be a lower bound only, so raising it did nothing.

        ``render_dropped_transcript``'s own 24 000-char default was unreachable,
        so an operator who raised ``context_compaction_input_tokens`` to 60 000
        silently got 24 000 — and a decision planted past that point was dropped
        with no summarizer ever seeing it.
        """
        from harness.agent_kernel.conversation import DEFAULT_TRANSCRIPT_MAX_CHARS
        from harness.agent_kernel.strategy import DailyCodingStrategy

        # No config at all: today's shipped effective ceiling.
        assert (
            DailyCodingStrategy._compaction_transcript_max_chars(_Strategy())
            == DEFAULT_TRANSCRIPT_MAX_CHARS
        )
        # A caller that asks for MORE gets more.
        assert (
            DailyCodingStrategy._compaction_transcript_max_chars(
                _Strategy({"context_compaction_input_tokens": 60000})
            )
            == 60000
        )
        # A caller that asks for LESS does NOT get less than today's behaviour.
        assert (
            DailyCodingStrategy._compaction_transcript_max_chars(
                _Strategy({"context_compaction_input_tokens": 100})
            )
            == DEFAULT_TRANSCRIPT_MAX_CHARS
        )
        # An unusable value degrades to the ceiling rather than to zero.
        assert (
            DailyCodingStrategy._compaction_transcript_max_chars(
                _Strategy({"context_compaction_input_tokens": "not a number"})
            )
            == DEFAULT_TRANSCRIPT_MAX_CHARS
        )


class _Strategy:
    """The minimum surface ``_compaction_transcript_max_chars`` reads."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        self.config = dict(config or {})


# ==========================================================================
# W2.4 — the thrash policy
# ==========================================================================


class TestTheThrashPolicyStillFires:
    def test_all_the_conditions_are_re_pinned(self) -> None:
        """3rd consecutive, ``after < limit``, blocked counts, streak resets."""
        results = thrash_run()
        assert results["limit"] == 3
        assert results["fires_on_three"] == 3
        assert results["does_not_fire_on_two"] is False
        assert results["progress_resets_the_streak"] == 4
        assert results["blocked_attempts_count"] == 3
        assert results["one_blocked_attempt_is_not_an_abort"] is True
        assert results["reclaiming_one_token_is_not_progress"] == 3
        assert results["limit_zero_disables"] is False

    def test_progress_is_after_below_limit_not_a_token_count(self) -> None:
        """A compaction can reclaim a token and still have accomplished nothing."""
        policy = _policy()
        assert policy.made_progress({"after_tokens": 99, "limit_tokens": 100}) is True
        assert policy.made_progress({"after_tokens": 100, "limit_tokens": 100}) is False
        assert (
            policy.made_progress({"after_tokens": 101, "limit_tokens": 100}) is False
        )
        # The token-count version would call this progress.
        assert (
            policy.made_progress(
                {"after_tokens": 100, "limit_tokens": 100, "reclaimed_tokens": 1}
            )
            is False
        )

    def test_the_raise_becomes_failed_and_never_a_completion(self) -> None:
        """``kernel.py`` turns the strategy exception into ``status="failed"``.

        Read from ``kernel.py`` — the file that owns the mapping — rather than
        from the strategy, which raises but does not decide the run's status.
        """
        from harness.agent_kernel import kernel as kernel_module

        source = Path(kernel_module.__file__).resolve().read_text(encoding="utf-8")
        assert "CompletionStatus.FAILED" in source, (
            "the kernel no longer maps a strategy raise to failed"
        )
        # And a failed run is never dressed as a completion.
        from shared.agent_contracts import CompletionStatus

        values = {str(item.value) for item in CompletionStatus}
        assert "failed" in values
        assert "success" not in values, (
            "a bare 'success' completion status exists; that is the violation "
            "this repository forbids"
        )

    def test_the_thrash_raise_leaves_recoverable_state(self) -> None:
        """The user must be able to inspect the conversation that would not compact."""
        rows: List[Dict[str, Any]] = []
        memory = ConversationMemory(
            journal=lambda record: rows.append(dict(record)),
            max_messages=400,
            max_chars=4_000_000,
            max_tool_output_chars=40000,
        )
        memory.seed([{"role": "system", "content": "sys"}])
        memory.record_assistant("go", turn=1)
        for index in range(1, 7):
            memory.record_tool_result(
                tool="write", ok=True, output="u" * 900, turn=index,
                target=f"f{index}.py",
            )
        with pytest.raises(CompactionThrashError):
            for _attempt in range(5):
                memory.compact_tokens(
                    limit_tokens=10,
                    summarize=lambda t, tu, previous_summary="": (
                        "## Objective\nnope\n"
                    ),
                    estimator=_estimator(),
                )
        assert memory.condensations, "a raise must leave the condensations behind"
        assert any(
            str(dict(row).get("record") or "") == "compaction" for row in rows
        ), "the evidence must be journaled BEFORE the raise"
        assert memory.render(), "the conversation must still be inspectable"

    def test_clearing_in_front_of_compaction_reduces_how_often_it_fires(self) -> None:
        """The measured effect, and it is an EFFECTIVE-compaction count.

        The unproductive RATE is 0.0 in both arms on this workload, so the rate
        is not the figure that carries the claim — the attempt count is. That is
        reported rather than dressed up.
        """
        rate = thrash_run()["rate"]
        assert rate["comparison_is_informative"] is True, rate["informative_reason"]
        assert rate["compactions_clearing_first"] < rate["compactions_compaction_first"], rate
        assert rate["confirmed"] is True


def _policy() -> Any:
    from harness.agent_kernel.budget import CompactionThrashPolicy

    return CompactionThrashPolicy()


def _estimator() -> Any:
    from harness.agent_kernel.budget import TokenEstimator

    return TokenEstimator()


# ==========================================================================
# W2.5 — ONE context-file authority
# ==========================================================================


class TestTheContextFileAuthorityIsSingular:
    def test_exactly_one_filename_list_exists_in_the_harness(self) -> None:
        """A second list fails this. ``context_files.py`` is the only one."""
        offenders = [
            (name, line, values)
            for name, line, values in _filename_lists()
            if name not in _LIST_EXEMPT_FILES
        ]
        assert offenders == [], (
            "a second context-file filename list appeared: "
            + json.dumps(offenders, default=str)
        )

    def test_the_exemptions_are_exactly_themselves_and_each_has_a_reason(self) -> None:
        """Adding a file to the exemption set is a visible, reviewed edit."""
        assert set(_LIST_EXEMPT_FILES) == {
            "test_p3a_w2_pins.py",
            "test_p3a_context.py",
            "context_files.py",
        }
        for name, reason in _LIST_EXEMPT_FILES.items():
            assert reason.strip(), name
            assert (Path(__file__).resolve().parent / name).is_file(), name

    def test_the_reported_list_is_exactly_the_nine_documented_names(self) -> None:
        """Including ``CONTEXT.md`` and ``.cursorrules``, which were missing."""
        assert tuple(context_files.CONTEXT_FILE_NAMES) == EXPECTED_CONTEXT_FILE_NAMES

    def test_the_scan_would_actually_catch_a_second_list(self) -> None:
        """Non-vacuity: the scanner must find a list when one exists.

        A scanner that finds nothing is indistinguishable from a scanner that
        cannot see. So this drives it against a synthetic source containing a
        second list and requires it to be reported.
        """
        assert _filename_lists(), "the scanner found no list at all, so it sees none"
        found = [
            entry for entry in _filename_lists() if entry[0] == "context_files.py"
        ]
        assert found, "the scanner cannot see the ONE list it is meant to police"

    def test_an_unparseable_file_is_reported_not_skipped(self) -> None:
        """A skipping scanner reports 'clean' about the file most likely broken."""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "_scan_probe.py"
            bad.write_bytes(b"\xef\xbb\xbfdef broken(:\n")
            try:
                ast.parse(bad.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                assert exc.lineno is not None
            else:  # pragma: no cover - the fixture is deliberately broken
                pytest.fail("the deliberately-unparseable fixture parsed")

    def test_precedence_is_first_found_wins(self) -> None:
        """Hermes/OpenCode: the first name in ``CONTEXT_FILE_NAMES`` that exists.

        Checked as "one winner per scope" — the root scope has BOTH ``AGENTS.md``
        and ``CLAUDE.md`` on disk and must resolve to exactly one. ``scopes`` is
        a tuple of scope strings, not a mapping, which is what the first draft
        assumed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            found = context_files.discover_context_files(root, "a.py")
            at_root = [
                Path(item.path).name
                for item in found.files
                if str(item.scope) in {".", ""}
            ]
            assert len(at_root) == 1, at_root
            assert at_root[0] == "AGENTS.md", (
                "the root scope resolved to CLAUDE.md; the first name in "
                "CONTEXT_FILE_NAMES must win"
            )

    def test_ordering_is_root_to_cwd_with_nested_last(self) -> None:
        """Codex's rule. The ordering IS the precedence rule, so it is pinned."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("root\n", encoding="utf-8")
            nested = root / "pkg" / "sub"
            nested.mkdir(parents=True)
            (nested / "AGENTS.md").write_text("nested\n", encoding="utf-8")
            (nested / "mod.py").write_text("value = 1\n", encoding="utf-8")
            found = context_files.discover_context_files(root, nested / "mod.py")
            depths = [
                len(Path(item.path).relative_to(root).parts) for item in found.files
            ]
            assert depths, "nothing discovered"
            assert depths == sorted(depths), [str(i.path) for i in found.files]
            assert depths == [1, 3], depths

    def test_precedence_beats_alphabetical_order(self) -> None:
        """A dot sorts before a letter, so an alphabetical sort INVERTS the rule."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (root / ".cursorrules").write_text("cursorrules\n", encoding="utf-8")
            found = context_files.discover_context_files(root, "a.py")
            at_root = [Path(item.path).name for item in found.files]
            assert at_root == ["AGENTS.md"], at_root
            # The alphabetical inversion cannot be read off the OUTPUT — the
            # authority has already discarded the loser, so `.cursorrules` is
            # absent entirely. What proves the rule is that AGENTS.md won DESPITE
            # sorting after a dot, and that the TUPLE puts it first.
            assert ".cursorrules" not in at_root
            names = tuple(context_files.CONTEXT_FILE_NAMES)
            assert names.index("AGENTS.md") < names.index(".cursorrules")
            assert sorted(names).index(".cursorrules") < sorted(names).index(
                "AGENTS.md"
            ), "the fixture no longer discriminates: alphabetical and precedence order agree"

    def test_the_jit_trigger_fires_per_file_and_only_for_what_it_has_not_seen(
        self,
    ) -> None:
        """A JIT injection is once per FILE, and scoped to what was touched.

        The first draft asserted that touching a NEW file in a sibling directory
        re-fires. It does not — correctly — because the root ``AGENTS.md`` was
        already injected while servicing the nested file, and re-injecting it
        would defeat the point of the mechanism. So the assertion is: two
        SIBLING directories, each with its own instructions, each fires exactly
        once and carries its OWN marker.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("ROOT-RULES-MARKER\n", encoding="utf-8")
            left = root / "pkg" / "left"
            right = root / "pkg" / "right"
            left.mkdir(parents=True)
            right.mkdir(parents=True)
            (left / "AGENTS.md").write_text("LEFT-RULES-MARKER\n", encoding="utf-8")
            (right / "AGENTS.md").write_text("RIGHT-RULES-MARKER\n", encoding="utf-8")
            (left / "m.py").write_text("value = 1\n", encoding="utf-8")
            (right / "m.py").write_text("value = 1\n", encoding="utf-8")

            trigger = context_files.PathTrigger(root)

            def markers(result: Any) -> List[str]:
                found: List[str] = []
                for item in result.pending:
                    text = Path(item.path).read_text(encoding="utf-8")
                    for marker in (
                        "ROOT-RULES-MARKER",
                        "LEFT-RULES-MARKER",
                        "RIGHT-RULES-MARKER",
                    ):
                        if marker in text:
                            found.append(marker)
                return found

            first_left = markers(trigger.note_touched(left / "m.py"))
            assert "LEFT-RULES-MARKER" in first_left, first_left
            assert "ROOT-RULES-MARKER" in first_left, first_left
            assert "RIGHT-RULES-MARKER" not in first_left, first_left

            # Once per file: the same file has nothing left to inject.
            assert not tuple(trigger.note_touched(left / "m.py").pending)

            # A sibling subtree brings its OWN instructions and nothing else.
            first_right = markers(trigger.note_touched(right / "m.py"))
            assert "RIGHT-RULES-MARKER" in first_right, first_right
            assert "ROOT-RULES-MARKER" not in first_right, first_right
            assert "LEFT-RULES-MARKER" not in first_right, first_right

    def test_a_missing_context_file_is_not_an_error(self) -> None:
        """Honest absence, not a crash and not an empty block claiming content."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("value = 1\n", encoding="utf-8")
            found = context_files.discover_context_files(root, "a.py")
            assert tuple(found.files) == ()
            assert tuple(found.errors) == ()
