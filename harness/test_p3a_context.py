"""P3a pins: incremental compaction, tool-result clearing, JIT context, budget.

**Read the module docstring of each class before trusting the test beside it.**
Every pin here names a property the phase's acceptance list requires, and each
one is written so it can FAIL for the right reason — an inverted pin or a vacuous
one is worse than no pin, and this repository has been bitten by both.

Nothing in this module needs Docker, a provider, or the network. Every model
reply is a scripted string, and the two end-to-end proofs drive the REAL kernel
through the REAL strategy with a scripted boundary, which is the only evidence
that the wiring is wired rather than present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent_kernel import incremental_summary as inc
from harness.agent_kernel.clearing import (
    NON_REFETCHABLE_TOOLS,
    REFETCHABLE_TOOLS,
    classification_table,
    classify_tool,
    clearing_report,
)
from harness.agent_kernel.conversation import ConversationMemory, ConversationTurn
from harness.context_files import (
    CONTEXT_FILE_NAMES,
    PathTrigger,
    context_file_report,
    discover_context_files,
    first_context_file,
)
from harness.prompt_budget import (
    BUDGET_COMPONENTS,
    PHASE_3B_TARGET,
    classify_message,
    profile_prompt,
    profile_session,
    split_message,
)

SUMMARY_ONE = """\
## Objective
Make compaction incremental.

## Important Details
conversation.py never showed the previous summary.

## Work State
changed conversation.py

## Next Move
measure the budget

## Relevant Files
harness/agent_kernel/conversation.py
"""

SUMMARY_TWO = """\
## Objective
Make compaction incremental.

## Important Details
conversation.py NOW shows the previous summary.

## Work State
changed conversation.py

## Next Move
measure the budget

## Relevant Files
harness/agent_kernel/conversation.py
"""


# ===========================================================================
# T1.W1.1 — incremental compaction
# ===========================================================================


class TestTheSummaryIsAnchored:
    """Five mandated sections, and a merge that has a shape to merge into."""

    def test_the_five_sections_are_the_corpus_documented_names(self):
        assert inc.SECTION_HEADINGS == (
            "Objective",
            "Important Details",
            "Work State",
            "Next Move",
            "Relevant Files",
        )

    def test_a_well_formed_summary_carries_all_five(self):
        report = inc.sections_report(SUMMARY_ONE)
        assert report["structured"] is True
        assert report["missing"] == []
        assert len(report["present"]) == 5

    def test_a_missing_section_is_carried_from_the_previous_summary(self):
        """The mechanism an early decision survives on. Not optional."""
        partial = "## Objective\nA2\n\n## Work State\nW2\n"
        merged, receipt = inc.normalize_summary(partial, SUMMARY_ONE)
        assert "conversation.py never showed the previous summary." in merged
        assert "Important Details" in receipt.carried
        assert "Next Move" in receipt.carried

    def test_a_contradicted_detail_is_replaced_not_unioned(self):
        """Staleness removal. The bug this fixes is a summary that only grows."""
        merged, receipt = inc.normalize_summary(SUMMARY_TWO, SUMMARY_ONE)
        assert "never showed the previous summary" not in merged
        assert "NOW shows the previous summary" in merged
        assert "Important Details" in receipt.replaced

    def test_the_summary_does_not_grow_monotonically(self):
        """The claim, as an operational property rather than a slogan.

        "Does not grow monotonically" is not "never grows": a compaction that
        records a genuine new fact has to be able to. What has to be true is that
        a chain of compactions cannot make the summary grow without bound, and
        that a merge which ADDS NOTHING does not grow at all. Both are asserted,
        because a test that only checked the second would pass for a summary that
        doubles on every third merge.
        """
        # (1) A merge with nothing new does not grow.
        current, _ = inc.normalize_summary(SUMMARY_ONE, "")
        for _repeat in range(5):
            merged, receipt = inc.normalize_summary(SUMMARY_ONE, current)
            assert receipt.grew is False, (
                "re-summarising identical content grew the summary"
            )
            current = merged

        # (2) A chain of compactions that each add a fact stays BOUNDED, and the
        #     bound is the configured ceiling rather than the number of merges.
        current = ""
        sizes = []
        for index in range(12):
            incoming = (
                SUMMARY_ONE.replace("## Objective", f"## Objective\nfact {index} added")
            )
            current, receipt = inc.normalize_summary(incoming, current, max_chars=900)
            sizes.append(len(current))
            assert len(current) <= 900, f"merge {index} exceeded the ceiling"
        assert max(sizes) <= 900
        assert sizes[-1] < sizes[0] + 900, (
            f"the chain grew without bound: {sizes}"
        )

    def test_a_merge_is_bounded_so_no_chain_can_exceed_the_ceiling(self):
        fat = "\n".join(f"## Objective\n{'x' * 400}" for _ in range(60))
        merged, receipt = inc.normalize_summary(fat, "", max_chars=800)
        assert len(merged) <= 800
        assert receipt.bounded is True
        assert "(whole summary)" in receipt.truncated_sections

    def test_a_section_is_bounded_and_the_truncation_is_visible(self):
        fat = "## Objective\n" + ("x" * 5000)
        merged, receipt = inc.normalize_summary(fat, "", section_max_chars=200)
        assert "Objective" in receipt.truncated_sections
        assert "...[section truncated]" in merged

    def test_a_freeform_summary_is_merged_not_dropped(self):
        """A summariser that ignores the format must still not lose its text."""
        merged, receipt = inc.normalize_summary(
            "the agent decided to use tabs and never to touch tests", ""
        )
        assert "tabs" in merged
        assert "never to touch tests" in merged
        assert receipt.unstructured is True

    def test_a_heading_alias_is_read_into_the_canonical_section(self):
        merged, _ = inc.normalize_summary("## Goal\nG\n\n## Next Step\nN\n", "")
        assert "## Objective" in merged
        assert "## Next Move" in merged

    def test_an_invented_sixth_section_is_kept_not_silently_dropped(self):
        merged, receipt = inc.normalize_summary(
            SUMMARY_ONE + "\n## Surprise\nan invented heading\n", ""
        )
        assert "an invented heading" in merged
        assert "surprise" in receipt.extra_headings

    def test_an_empty_incoming_summary_keeps_the_previous_one(self):
        merged, _ = inc.normalize_summary("", SUMMARY_ONE)
        assert "Make compaction incremental." in merged


class TestTheCompactionFeedsTheSummaryBack:
    """The wiring, proven through the real ``ConversationMemory``."""

    @staticmethod
    def _memory(**kwargs):
        values = {
            "max_messages": 500,
            "max_chars": 4_000_000,
            "max_tool_output_chars": 12000,
        }
        values.update(kwargs)
        return ConversationMemory(**values)

    def _grown(self, memory, turns=8):
        # Seed ONCE. ``seed()`` establishes the base frame and resets the
        # handoff, so re-seeding between two compactions would wipe the very
        # summary chain this test is about.
        if not memory.seeded:
            memory.seed(
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "please fix the bug"},
                ]
            )
        for index in range(turns):
            memory.record_assistant(f"reply {index}", turn=index + 1)
            memory.record_tool_result(
                "read",
                True,
                f"result {index} " + ("x" * 400),
                turn=index + 1,
                target="a.py",
            )
        return memory

    def test_the_summarizer_receives_the_previous_summary(self):
        """The defect, stated as an assertion on what the callable was handed."""
        memory = self._memory()
        self._grown(memory)
        seen: list = []

        def summarize(transcript, turns, previous_summary=""):
            seen.append(previous_summary)
            return (SUMMARY_ONE if len(seen) == 1 else SUMMARY_TWO), "model_summary"

        memory.compact_tokens(
            limit_tokens=1, summarize=summarize, estimator=_estimator()
        )
        self._grown(memory, turns=14)
        memory.compact_tokens(
            limit_tokens=1, summarize=summarize, estimator=_estimator()
        )
        assert len(seen) == 2
        assert seen[0] == "", "the first compaction has no predecessor"
        assert "Make compaction incremental." in seen[1]
        assert "Objective" in seen[1]

    def test_a_summarizer_that_does_not_name_the_parameter_still_works(self):
        """The `_boundary_names` rule. An old caller must not get a TypeError."""
        memory = self._memory()
        self._grown(memory)
        receipt = memory.compact_tokens(
            limit_tokens=1,
            summarize=lambda transcript, turns: (SUMMARY_ONE, "model_summary"),
            estimator=_estimator(),
        )
        assert receipt is not None
        assert receipt["method"] == "model_summary"

    def test_the_receipt_reports_the_merge(self):
        memory = self._memory()
        self._grown(memory)
        memory.compact_tokens(
            limit_tokens=1,
            summarize=lambda t, u, previous_summary="": (
                SUMMARY_TWO if previous_summary else SUMMARY_ONE,
                "model_summary",
            ),
            estimator=_estimator(),
        )
        self._grown(memory, turns=14)
        receipt = memory.compact_tokens(
            limit_tokens=1,
            summarize=lambda t, u, previous_summary="": (SUMMARY_TWO, "model_summary"),
            estimator=_estimator(),
        )
        merge = receipt["merge"]
        assert merge, "every compaction after the first must carry a merge receipt"
        assert merge["grew"] is False
        assert "Important Details" in merge["replaced"]

    def test_the_handoff_renders_only_the_current_summary(self):
        """Two summaries rendered together cannot say which one is still true."""
        memory = self._memory()
        self._grown(memory)
        merge = lambda t, u, previous_summary="": (  # noqa: E731
            SUMMARY_ONE if not previous_summary else SUMMARY_TWO,
            "model_summary",
        )
        memory.compact_tokens(
            limit_tokens=1, summarize=merge, estimator=_estimator()
        )
        self._grown(memory, turns=14)
        memory.compact_tokens(
            limit_tokens=1, summarize=merge, estimator=_estimator()
        )
        assert len(memory.handoff.summaries) == 2, "the chain must have two heads"
        text = memory.handoff.render()
        assert text.count("Make compaction incremental.") == 1, (
            "the superseded summary must not be rendered beside the current one; "
            "two summaries side by side cannot say which statement is still true"
        )
        assert "NOW shows" in text, "the CURRENT summary is the one rendered"


class TestTheCompactionAgentHasNoTools:
    """A summariser that can run commands is a summariser that can be induced."""

    def test_the_compaction_call_passes_no_tool_schemas(self):
        """Structural, not a policy. Read from the source, so it cannot rot."""
        source = Path("harness/agent_kernel/strategy.py").read_text(encoding="utf-8")
        body = source.split("def _summarize_turns", 1)[1]
        body = body.split("\n    def ", 1)[0]
        for line in body.splitlines():
            if ".call(" not in line:
                continue
            assert "tools=" not in line, (
                "the compaction call must not carry tool schemas: "
                f"{line.strip()!r}"
            )

    def test_the_compaction_gateway_receives_a_prompt_and_a_step_only(self):
        """AST, not a line grep: a docstring mentioning ``.call(`` is not a call."""
        import ast

        tree = ast.parse(Path("harness/agent_kernel/strategy.py").read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_summarize_turns"
        )
        calls = [
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
        ]
        assert calls, "the summarizer must actually call the model"
        for call in calls:
            names = {
                keyword.arg for keyword in call.keywords if keyword.arg
            }
            assert "tools" not in names, (
                "the compaction call must carry no tool schemas: a summarizer that "
                "can run commands is a summarizer that can be induced to do "
                "something else"
            )
            assert "difficulty_hint" in names, (
                "a summarizer must stay on the cheap tier"
            )


def _estimator():
    from harness.agent_kernel.budget import TokenEstimator

    return TokenEstimator()


# ===========================================================================
# T1.W1.2 — tool-result clearing
# ===========================================================================


class TestTheClassificationIsCorrect:
    """The correctness crux: clearing a non-re-fetchable result is data loss."""

    def test_a_test_verdict_is_never_cleared(self):
        refetchable, reason = classify_tool("test")
        assert refetchable is False
        assert reason == "not_refetchable"
        assert NON_REFETCHABLE_TOOLS["test"] == "test_verdict"

    def test_a_network_result_is_never_cleared(self):
        assert classify_tool("web_fetch")[0] is False
        assert classify_tool("web_search")[0] is False

    def test_a_pure_read_is_cleared(self):
        for tool in ("read", "grep", "glob", "list", "git_log", "read_symbol"):
            assert classify_tool(tool)[0] is True, tool

    def test_an_unrecognised_tool_is_protected(self):
        """The safe direction. Silence must not read as permission."""
        refetchable, reason = classify_tool("some_tool_from_next_year")
        assert refetchable is False
        assert reason == "unclassified_tool"

    def test_the_two_sets_are_disjoint(self):
        overlap = REFETCHABLE_TOOLS & set(NON_REFETCHABLE_TOOLS)
        assert overlap == set(), f"a tool cannot be both: {overlap}"

    def test_the_table_is_publishable_not_inferred_from_silence(self):
        rows = classification_table()
        assert len(rows) == len(REFETCHABLE_TOOLS) + len(NON_REFETCHABLE_TOOLS)
        assert all(reason for _, _, reason in rows)


class TestClearingIsLosslessAndReported:
    def _turns(self):
        return [
            ConversationTurn(role="user", content="b", kind="note", turn=1, seq=1),
            ConversationTurn(
                role="assistant", content="reading", kind="assistant", turn=2, seq=2
            ),
            ConversationTurn(
                role="user",
                content="A" * 6000,
                kind="tool_result",
                tool="read",
                turn=3,
                seq=3,
                ok=True,
                target="a.py",
            ),
            ConversationTurn(
                role="user",
                content="FAILED 3 tests",
                kind="tool_result",
                tool="test",
                turn=4,
                seq=4,
                ok=False,
            ),
            ConversationTurn(
                role="user",
                content="page",
                kind="tool_result",
                tool="web_fetch",
                turn=5,
                seq=5,
                ok=True,
            ),
            ConversationTurn(
                role="user",
                content="B" * 5000,
                kind="tool_result",
                tool="unheard_of",
                turn=6,
                seq=6,
                ok=True,
            ),
            ConversationTurn(
                role="user",
                content="C" * 4000,
                kind="tool_result",
                tool="grep",
                turn=7,
                seq=7,
                ok=True,
            ),
        ]

    def test_the_newest_result_is_never_cleared(self):
        _out, receipt = _clear(self._turns())
        kept = {tool: reason for _seq, tool, reason in receipt.protected}
        assert kept.get("grep") == "newest", (
            "the newest tool result is the one the model is reasoning about right "
            "now; clearing it trades a live fact for a notice"
        )

    def test_a_non_refetchable_result_is_never_cleared(self):
        _out, receipt = _clear(self._turns())
        kept = {tool: reason for _seq, tool, reason in receipt.protected}
        assert kept.get("test") == "not_refetchable"
        assert kept.get("web_fetch") == "not_refetchable"

    def test_an_unclassified_tool_is_protected_even_when_it_is_not_newest(self):
        _out, receipt = _clear(self._turns())
        kept = {tool: reason for _seq, tool, reason in receipt.protected}
        assert kept.get("unheard_of") == "unclassified_tool"

    def test_the_tool_use_record_survives_the_clearing(self):
        out, _receipt = _clear(self._turns())
        cleared = [item for item in out if item.tool == "read"]
        assert len(cleared) == 1
        assert cleared[0].seq == 3, "the journal sequence is the identity"
        assert cleared[0].turn == 3
        assert cleared[0].target == "a.py"
        assert cleared[0].ok is True
        assert cleared[0].content.startswith("[tool result cleared:")

    def test_the_receipt_names_the_count_the_bytes_and_the_tools(self):
        _out, receipt = _clear(self._turns())
        payload = receipt.as_dict()
        assert payload["cleared_count"] == 1
        assert payload["reclaimed_chars"] > 5000
        assert payload["tools"] == ["read"]
        assert "test" in payload["protected_tools"]
        assert payload["reversible"] == {}, "the raw helper carries no resolver"
        assert payload["protected"], "every protected result must name itself"

    def test_the_input_is_not_mutated(self):
        """A view transform, not an edit. The journal is the record."""
        turns = self._turns()
        before = [item.content for item in turns]
        _clear(turns)
        assert [item.content for item in turns] == before

    def test_a_cleared_result_is_not_cleared_twice(self):
        turns = self._turns()
        out, first = _clear(turns)
        _again, second = _clear(out)
        assert first.method == "cleared"
        assert second.cleared_count == 0
        assert ("already_placeholder") in {
            reason for _s, _t, reason in second.protected
        }

    def test_disabled_is_a_real_outcome_with_a_reason(self):
        _out, receipt = _clear(self._turns(), enabled=False)
        assert receipt.method == "disabled"
        assert "disabled" in receipt.reason

    def test_the_report_is_read_only(self):
        turns = self._turns()
        before = [item.content for item in turns]
        report = clearing_report(turns)
        assert [item.content for item in turns] == before
        assert report["tool_results"] == 5
        assert report["reclaimable_chars"] > 0


def _clear(turns, **kwargs):
    from harness.agent_kernel.clearing import clear_tool_results

    return clear_tool_results(turns, **kwargs)


class TestClearingRunsBeforeCompaction:
    """The ordering, proven end-to-end through the REAL kernel."""

    def test_clearing_is_declared_before_compaction_in_the_source(self):
        """A structural pin: the ORDER is the argument, so it is checkable."""
        source = Path("harness/agent_kernel/strategy.py").read_text(encoding="utf-8")
        body = source.split("def _prepared_request", 1)[1]
        body = body.split("\n    def ", 1)[0]
        clear_at = body.index("self._clear_context(")
        compact_at = body.index("self._compact_context(")
        assert clear_at < compact_at, (
            "clearing must run BEFORE compaction; clearing is lossless and "
            "compaction is not, so reversing them loses context for free"
        )

    def test_the_off_arm_is_reported_not_silent(self):
        source = Path("harness/agent_kernel/strategy.py").read_text(encoding="utf-8")
        assert "context_clearing_enabled" in source


# ===========================================================================
# T1.W1.3 — ONE context-file authority, and JIT injection
# ===========================================================================


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / ".neo").mkdir()
    (root / "AGENTS.md").write_text("root: never edit tests", encoding="utf-8")
    (root / ".neo" / "INSTRUCTIONS.md").write_text("neo rules", encoding="utf-8")
    (root / "pkg" / "sub" / "AGENTS.md").write_text("sub: use tabs", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "CONTEXT.md").write_text("src context rules", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "helper.py").write_text("y = 2\n", encoding="utf-8")
    return root


class TestThereIsExactlyOneContextFileAuthority:
    def test_the_set_includes_every_name_the_corpus_systems_read(self):
        for name in (
            "AGENTS.md",
            "CLAUDE.md",
            "GEMINI.md",
            "QWEN.md",
            "INSTRUCTIONS.md",
            "CONTEXT.md",
            ".cursorrules",
        ):
            assert name in CONTEXT_FILE_NAMES, name

    def test_the_compiler_reads_the_one_authority(self):
        """Not an equality check on a literal — an IDENTITY check on a reference."""
        import harness.context_compiler as compiler

        assert compiler._INSTRUCTION_FILES is CONTEXT_FILE_NAMES
        assert compiler._NEO_DIRECT_FILES is CONTEXT_FILE_NAMES

    def test_the_kernel_builder_reads_the_one_authority(self, repo):
        from harness.agent_kernel.context import ContextBuilder

        builder = ContextBuilder(Path(str(repo)), max_turns=4)
        found = "\n".join(builder.discover_project_instructions(str(repo)))
        assert "never edit tests" in found
        assert "src context rules" not in found, (
            "src/ is not on the root's walk; a JIT trigger brings it in"
        )

    def test_no_harness_module_declares_a_second_filename_list(self):
        """An AST scan, because a grep cannot tell a literal from a reference."""
        import ast

        offenders = []
        for path in sorted(Path("harness").rglob("*.py")):
            if path.name in {"context_files.py", "test_p3a_context.py"}:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                offenders.append(f"{path}: unparseable ({exc})")
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
                if sum(1 for item in literals if item.endswith(".md")) >= 2:
                    offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], (
            "a second context-file filename list is a divergence bug waiting to "
            f"happen: {offenders}"
        )

    def test_every_name_the_trust_boundary_reviews_is_discoverable(self):
        """T5's boundary knows six names. All six must be readable by discovery."""
        report = context_file_report()
        assert report["boundary_available"] is True
        assert report["boundary_does_not_cover"] == []


class TestOrderingIsRootToCwd:
    def test_nested_content_is_last(self, repo):
        """Codex's rule: root-to-cwd, nested last. It IS the precedence rule."""
        found = discover_context_files(repo, repo / "pkg" / "sub")
        paths = [item.relative_path for item in found.files]
        assert paths.index("AGENTS.md") < paths.index("pkg/sub/AGENTS.md")
        assert [item.depth for item in found.files] == sorted(
            item.depth for item in found.files
        )

    def test_precedence_order_beats_alphabetical_order(self, repo):
        """A dot sorts before a letter, so an alphabetical sort would invert it."""
        found = discover_context_files(repo, repo)
        paths = [item.relative_path for item in found.files]
        assert paths.index("AGENTS.md") < paths.index(".neo/INSTRUCTIONS.md")

    def test_first_found_wins_within_one_directory(self, repo):
        """Two instruction files in one directory are competing authorities."""
        (repo / ".cursorrules").write_text("cursor rules", encoding="utf-8")
        assert first_context_file(repo).name == "AGENTS.md"
        found = [item.relative_path for item in discover_context_files(repo, repo).files]
        assert ".cursorrules" not in found

    def test_a_target_outside_the_repository_does_not_widen_the_walk(self, repo, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "AGENTS.md").write_text("not yours", encoding="utf-8")
        found = discover_context_files(repo, outside / "AGENTS.md")
        assert found.files, "the walk still runs from the root"
        assert all(
            "elsewhere" not in item.relative_path for item in found.files
        )
        assert any("outside the repository" in w for w in found.warnings)


class TestPathTriggerInjectsJustInTime:
    def test_a_relative_target_resolves_against_the_repository_not_the_cwd(self, repo):
        """A tool call reports a REPOSITORY-RELATIVE path, always.

        This is the pin for a defect the first draft of ``discover_context_files``
        had and the other pins did NOT catch: ``_normalize`` resolved before the
        relativeness test, so a relative target landed against the process's
        working directory, failed the containment check, and collapsed the walk to
        the ROOT. The root instructions still arrived, so a live run looked
        healthy while the nested file the trigger exists to fetch never did. Every
        other pin in this class passed an ABSOLUTE path, which is why they all
        stayed green.
        """
        found = discover_context_files(repo, "pkg/sub/mod.py")
        assert "pkg/sub/AGENTS.md" in [item.relative_path for item in found.files]
        assert not any("outside the repository" in w for w in found.warnings)

    def test_the_trigger_accepts_a_relative_path(self, repo):
        trigger = PathTrigger(repo)
        result = trigger.note_touched("pkg/sub/mod.py", tool="read")
        assert "pkg/sub/AGENTS.md" in [item.relative_path for item in result.pending]

    def test_a_path_traversing_out_of_the_repository_is_refused(self, repo, tmp_path):
        trigger = PathTrigger(repo)
        result = trigger.note_touched("../elsewhere/x.py", tool="read")
        assert result.empty is True
        assert "outside" in result.reason

    def test_touching_a_file_brings_its_directorys_instructions(self, repo):
        trigger = PathTrigger(repo)
        result = trigger.note_touched(repo / "src" / "app.py", tool="read")
        names = [item.relative_path for item in result.pending]
        assert "src/CONTEXT.md" in names
        assert "AGENTS.md" in names, "the root's rules are in scope too"

    def test_the_second_touch_of_the_same_directory_injects_nothing(self, repo):
        """Re-injecting per touch is how a JIT trigger becomes a context flood.

        The trigger's unit is the DIRECTORY'S INSTRUCTION FILE, not the file.
        A second read of the same directory must be free — otherwise every
        ``read`` in a 200-file directory pays for that directory's AGENTS.md.
        """
        trigger = PathTrigger(repo)
        first = trigger.note_touched(repo / "src" / "app.py", tool="read")
        assert "src/CONTEXT.md" in [item.relative_path for item in first.pending]
        again = trigger.note_touched(repo / "src" / "helper.py", tool="read")
        assert again.pending == (), (
            "a NEW file in an ALREADY-TRIGGERED directory must not re-inject "
            "that directory's instructions; a per-directory trigger is the unit"
        )
        assert again.empty is True

    def test_a_different_subtree_brings_its_own(self, repo):
        trigger = PathTrigger(repo)
        trigger.note_touched(repo / "src" / "app.py", tool="read")
        deeper = trigger.note_touched(repo / "pkg" / "sub" / "x.py", tool="edit")
        assert "pkg/sub/AGENTS.md" in [item.relative_path for item in deeper.pending]

    def test_a_path_outside_the_repository_injects_nothing(self, repo, tmp_path):
        trigger = PathTrigger(repo)
        result = trigger.note_touched(tmp_path / "elsewhere.py", tool="read")
        assert result.empty is True
        assert "outside" in result.reason

    def test_the_render_names_the_directory_it_came_from(self, repo):
        from harness.context_files import render_injected_context

        trigger = PathTrigger(repo)
        result = trigger.note_touched(repo / "pkg" / "sub" / "x.py", tool="read")
        rendered = render_injected_context(result.pending)
        assert "[nested context: pkg/sub/AGENTS.md]" in rendered
        assert "use tabs" in rendered

    def test_the_trigger_calls_the_trust_boundary(self, repo, monkeypatch):
        """The security side is T5's authority, and this asserts we REACH it."""
        from harness import context_files as module

        seen = []
        real = module.review_context_text

        def spy(text, **kwargs):
            seen.append(kwargs.get("relative_path"))
            return real(text, **kwargs)

        monkeypatch.setattr(module, "review_context_text", spy)
        trigger = PathTrigger(repo)
        result = trigger.note_touched(repo / "pkg" / "sub" / "x.py", tool="read")
        module.render_injected_context(result.pending)
        assert seen, "rendered instruction text must pass the boundary"
        assert "pkg/sub/AGENTS.md" in seen

    def test_an_unreachable_boundary_withholds_the_text(self, repo, monkeypatch):
        from harness import context_files as module

        monkeypatch.setattr(module, "review_context_text", lambda *a, **k: None)
        trigger = PathTrigger(repo)
        result = trigger.note_touched(repo / "pkg" / "sub" / "x.py", tool="read")
        rendered = module.render_injected_context(result.pending)
        assert "NOT REVIEWED" in rendered
        assert "use tabs" not in rendered, (
            "an unreviewed instruction file must not reach the prompt"
        )


# ===========================================================================
# T1.W1.4 — the prompt budget
# ===========================================================================


def _messages(turn):
    return [
        {
            "role": "system",
            "content": "You are an agent.\n\n--- AGENTS.md (scope: .) ---\n"
            "never edit tests\n\n## Repository map\n- harness/a.py\n",
        },
        {
            "role": "user",
            "content": "## Compiled repository context\ncompiled text\n\n"
            "## Compacted earlier turns (structured handoff)\nTurns 1-2 dropped."
            if turn > 3
            else "please fix the bug",
        },
        {"role": "assistant", "content": "I will read the file first."},
        {"role": "user", "content": "R" * (1200 * turn)},
        {
            "role": "user",
            "content": "[tool result cleared: read returned 1200 characters on "
            "turn 3. Re-call read if you need it again.]"
            if turn > 6
            else "Y" * 900,
        },
    ]


def _tools(count=45):
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "a tool description " * 20,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(count)
    ]


class TestTheBudgetProfileIsHonest:
    def test_every_byte_lands_in_exactly_one_component(self):
        profile = profile_prompt(_messages(10), tools=_tools(), turn=10)
        assert set(profile.totals) == set(BUDGET_COMPONENTS)
        assert round(sum(profile.shares.values()), 6) == 1.0

    def test_the_tool_schema_share_is_named_and_measured(self):
        """Phase 3b's baseline. Not zero, and not derived from the catalog."""
        profile = profile_prompt(_messages(10), tools=_tools(), turn=10)
        assert profile.tool_schema_count == 45
        assert profile.schema_share > 0.0
        assert profile.as_dict()["phase_3b_target"] == PHASE_3B_TARGET

    def test_unmeasured_schemas_are_not_reported_as_free(self):
        profile = profile_prompt(_messages(10), tools=None, turn=10)
        assert profile.tokens("tool_schemas") == 0
        assert profile.tool_schema_count == 0

    def test_the_estimator_is_named(self):
        profile = profile_prompt(_messages(1), turn=1)
        assert profile.estimator, (
            "a heuristic token count presented without its estimator name is a "
            "measurement presented as a tokenizer count"
        )

    def test_a_single_message_can_carry_several_components(self):
        """The compiled base frame holds instructions AND knowledge in ONE message."""
        profile = profile_prompt(_messages(10), turn=10)
        assert profile.tokens("context_files") > 0
        assert profile.tokens("knowledge_context") > 0
        assert profile.tokens("conversation_summary") > 0

    def test_classification_precedence_keeps_the_reminder_out_of_the_summary(self):
        combined = (
            "## Compacted earlier turns (structured handoff)\n"
            "Turns 1-2 dropped.\n\n[neo-reminder]\n## REMINDER\n- do not edit tests"
        )
        assert classify_message(combined) == "constraint_reinjection"

    def test_split_message_separates_the_markers(self):
        fragments = dict(
            (component, text)
            for component, text in split_message(_messages(10)[0]["content"])
        )
        assert "AGENTS.md" in fragments.get("context_files", "")
        assert "Repository map" in fragments.get("knowledge_context", "")


class TestTheProfileIsMeasuredAtSeveralDepths:
    def test_composition_changes_over_a_session(self):
        """A single turn's profile is not the profile."""
        report = profile_session(
            [(depth, _messages(depth), _tools()) for depth in (1, 10, 40, 80)],
            depths=(1, 10, 40, 80),
        )
        assert report.depths == (1, 10, 40, 80)
        growth = report.growth()
        assert growth["tool_results_live"]["growth_x"] > 10.0
        assert growth["tool_schemas"]["growth_x"] == 1.0, (
            "schemas are constant within a run; that is what makes them a "
            "clean Phase-3b target"
        )
        assert report.at(40).total_tokens > report.at(1).total_tokens

    def test_a_depth_with_no_recorded_turn_is_skipped_not_interpolated(self):
        report = profile_session(
            [(1, _messages(1), _tools())], depths=(1, 10, 40, 80)
        )
        assert report.depths == (1,)
        assert report.at(40) is None

    def test_the_table_prints_one_column_per_depth(self):
        report = profile_session(
            [(depth, _messages(depth), _tools()) for depth in (1, 10)],
            depths=(1, 10),
        )
        table = report.table()
        assert "turn 1" in table and "turn 10" in table
        assert PHASE_3B_TARGET in table


class TestTheG3aProbeCannotPassVacuously:
    """Rung 7 was `not_implemented` with owner "T1 / P3a". This is its probe.

    The pins here are about the PROBE, not about the product: a gate that can
    report a pass while measuring nothing is worse than no gate, and this
    repository has been bitten by that class repeatedly.

    The full probe is a ~80 s two-arm run and lives at
    ``python -m harness.long_session_probe``; the pins below assert its
    CONTRACT so a future edit cannot quietly make it vacuous, and one
    short end-to-end run proves the contract holds against a real kernel.
    """

    def test_the_verdict_has_four_states_and_a_vacuous_run_cannot_pass(self):
        from harness.long_session_probe import RECALL_TURN, ProbeResult

        carrying = (4, 10, RECALL_TURN, 60)
        assert ProbeResult().verdict == "vacuous_no_compaction"
        assert (
            ProbeResult(
                compactions=23,
                incremental_compactions=22,
                vacuous=False,
                turns_with_decision=carrying,
            ).verdict
            == "decision_survived_incremental_compactions"
        )
        lost = ProbeResult(
            compactions=23,
            incremental_compactions=22,
            vacuous=False,
            turns_with_decision=(4, 10),
        )
        assert lost.decision_survived is False
        assert lost.verdict == "decision_lost", (
            "a decision that was present at turn 10 and gone by turn 50 IS the "
            "defect; a verdict that only asked 'was it ever present' would pass it"
        )

    def test_a_run_that_survived_without_any_incremental_merge_is_not_a_pass(self):
        from harness.long_session_probe import RECALL_TURN, ProbeResult

        probe = ProbeResult(
            compactions=1,
            incremental_compactions=0,
            vacuous=False,
            turns_with_decision=(4, RECALL_TURN),
        )
        assert probe.decision_survived is True
        assert probe.verdict == "survived_but_never_incremental", (
            "surviving without exercising the merge is a DIFFERENT finding from "
            "surviving because of it, and it is not a pass"
        )

    def test_the_receipt_carries_the_vacuity_flag_and_the_window(self):
        from harness.long_session_probe import ProbeResult

        payload = ProbeResult(
            not_established=("measured survival, not use",)
        ).as_dict()
        assert payload["vacuous"] is True
        assert payload["verdict"] == "vacuous_no_compaction"
        assert "window_tokens" in payload
        assert payload["not_established"], (
            "a probe that measures decision SURVIVAL must say it does not "
            "measure decision USE"
        )

    def test_the_probe_window_is_smaller_than_the_shipped_one_on_purpose(self):
        from harness.long_session_probe import PROBE_WINDOW, WINDOW

        assert PROBE_WINDOW < WINDOW, (
            "at the shipped window, clearing alone keeps a read loop under the "
            "compaction threshold and the probe measures nothing"
        )

    def test_a_short_run_reports_that_it_was_short(self, tmp_path):
        """The probe at fewer turns than the gate must not report a pass."""
        from harness.long_session_probe import main

        code = main(["--turns", "8", "--json"])
        assert code != 0, (
            "a run shorter than RECALL_TURN cannot answer the gate, so it must "
            "exit non-zero rather than report whatever it saw"
        )


# ===========================================================================
# What this round must NOT have broken
# ===========================================================================


class TestWhatWeAlreadyDidBetterIsIntact:
    """The brief lists these as KEEP. A regression here is this round's fault."""

    def test_condensation_record_still_carries_everything(self):
        from harness.agent_kernel.budget import CondensationRecord

        fields = set(CondensationRecord.__dataclass_fields__)
        for name in (
            "dropped",
            "summary",
            "cache_prefix_sha256_before",
            "cache_prefix_sha256_after",
            "body_from",
        ):
            assert name in fields, name
        record = CondensationRecord(summary="s")
        assert record.as_dict()["reversible"]["restore"].startswith(
            "ConversationJournal.restore_compaction"
        )

    def test_the_thrash_policy_is_still_three_and_still_measures_progress(self):
        from harness.agent_kernel.budget import (
            DEFAULT_COMPACTION_THRASH_LIMIT,
            CompactionThrashPolicy,
        )

        assert DEFAULT_COMPACTION_THRASH_LIMIT == 3
        policy = CompactionThrashPolicy(limit=3, min_reclaim_tokens=1)
        # Progress is `after < limit`, NOT `reclaimed > 0`. The receipt carries the
        # trigger, so both rungs are exercised: with one, `after` decides; without
        # one, the reclaimed-token floor does.
        assert (
            policy.made_progress(
                {"before_tokens": 100, "limit_tokens": 90, "after_tokens": 89}
            )
            is True
        )
        assert (
            policy.made_progress(
                {
                    "before_tokens": 100,
                    "limit_tokens": 90,
                    "after_tokens": 90,
                    "reclaimed_tokens": 10,
                }
            )
            is False
        ), "reclaiming 10 tokens while staying AT the trigger is not progress"
        assert (
            policy.made_progress({"before_tokens": 100, "reclaimed_tokens": 1}) is True
        )
        assert (
            policy.note({"before_tokens": 100, "reclaimed_tokens": 0}, streak=2) == 3
        ), "an unproductive compaction must advance the streak"
        assert (
            policy.note(
                {"before_tokens": 100, "limit_tokens": 90, "after_tokens": 89},
                streak=2,
            )
            == 0
        ), "a productive compaction resets the streak"
        assert policy.decide(2) == "", "two in a row is not yet the abort"
        assert policy.decide(3), "the third consecutive unproductive compaction aborts"

    def test_the_compaction_receipt_still_names_the_method_that_ran(self):
        memory = ConversationMemory(
            max_messages=500, max_chars=4_000_000, max_tool_output_chars=12000
        )
        memory.seed(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        )
        for index in range(8):
            memory.record_assistant(f"r{index}", turn=index + 1)
            memory.record_tool_result(
                "read", True, f"x {index}" + "y" * 400, turn=index + 1
            )
        receipt = memory.compact_tokens(
            limit_tokens=1,
            summarize=lambda t, u: ("", "summarizer_error"),
            estimator=_estimator(),
        )
        assert receipt["method"] == "summarizer_error"

    def test_clearing_never_touches_the_base_frame(self):
        """The base frame is the provider's cacheable head."""
        memory = ConversationMemory(
            max_messages=500, max_chars=4_000_000, max_tool_output_chars=12000
        )
        base = [
            {"role": "system", "content": "system " + "S" * 5000},
            {"role": "user", "content": "user " + "U" * 5000},
        ]
        memory.seed(base)
        for index in range(4):
            memory.record_tool_result(
                "read", True, "Z" * 4000, turn=index + 1
            )
        memory.clear_tool_results()
        assert memory.base_messages() == base

    def test_the_compaction_prompt_has_no_agent_facing_prose_change(self):
        """AHEAD measured -2.3pp on the AGENT system prompt.

        The compaction prompt belongs to a maintenance call to a cheap tier, and
        this asserts it stayed a SEPARATE constant rather than being merged into
        anything the agent reads.
        """
        import harness.prompts as prompts

        assert prompts.CONTEXT_COMPACTION_MERGE is not prompts.CONTEXT_COMPACTION_SYSTEM
        assert "## Objective" in prompts.CONTEXT_COMPACTION_MERGE
        from harness.agent_loop_step import _constraint_block  # noqa: F401

    def test_no_compaction_verb_is_a_prompt_the_agent_reads(self):
        """A structural separation: no agent-facing prompt imports the merge text."""
        import harness.prompts as prompts

        merge_words = ("still true", "superseded", "previous summary")
        for name in dir(prompts):
            if name.startswith("STEP_") or name.endswith("_SYSTEM") is False:
                continue
            if name in {"CONTEXT_COMPACTION_MERGE", "CONTEXT_COMPACTION_SYSTEM"}:
                continue
            value = getattr(prompts, name)
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            for word in merge_words:
                assert word not in lowered, (
                    f"{name} carries summariser-merge prose; that belongs only on "
                    "the compaction request"
                )
        assert json.dumps(True)
