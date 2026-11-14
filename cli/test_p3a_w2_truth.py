"""Pins for P3a/W2: nothing memory or lineage does is invisible.

T4.W2's round-local suite. It lives in ``cli/`` rather than ``tests/`` for the
reason ``cli/AGENTS.md`` records three times: a gate its own module's owner
cannot edit without filing a cross-terminal request is a gate that rots.

Every test here drives the REAL renderer. Nothing asserts that a helper works
in isolation where a surface could bypass it, because the defect class this
round exists to catch is precisely a surface that renders something the helper
never produced.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from cli import context_budget as budget
from cli import lineage, memory_ui
from cli import memory_provenance as prov
from cli import surface_truth as truth

SECRET = "key=sk-FAKE-SECRET-0123456789"


@pytest.fixture()
def scratch() -> Path:
    with tempfile.TemporaryDirectory() as name:
        yield Path(name)


def _session_with_turns(log_root: Path, texts: tuple = ("never use eval()", "understood")) -> str:
    from cli import session as cs

    sid = cs.new_session_id()
    conversation = cs.load_or_create(log_root, None, sid, strict=False)
    for index, text in enumerate(texts, start=1):
        cs.append_turn(conversation, "user" if index == 1 else "assistant", text, turn_id=f"t-{index}")
    cs.save_session(log_root, conversation)
    return sid


# ---------------------------------------------------------------------------
# W2.1 - memory provenance
# ---------------------------------------------------------------------------


class TestTheCitationResolvesOrIsNamedBroken:
    def test_a_citation_into_a_live_turn_resolves_and_quotes_it(self, scratch):
        sid = _session_with_turns(scratch)
        row = {"id": 1, "text": "never use eval()", "source": "user",
               "provenance": {"session_id": sid, "turn": "t-2"}}
        target = prov.citation_target(scratch, sid, "t-2")
        assert target["verdict"] == "resolvable"
        assert target["turn_text"] == "understood"
        rendered = "\n".join(memory_ui.memory_detail_lines(row, log_root=scratch))
        assert "opens:" in rendered and "open it with:" in rendered

    def test_a_deleted_source_session_is_DANGLING_and_offers_no_link(self, scratch):
        """The brief's named case. A dead citation must not look live."""
        from cli import session as cs

        sid = _session_with_turns(scratch)
        row = {"id": 2, "text": "never use eval()", "source": "user",
               "provenance": {"session_id": sid, "turn": "t-2"}}
        cs._session_path(scratch, sid).unlink()
        journal = cs._event_log_path(scratch, sid)
        if journal.exists():
            journal.unlink()
        report = prov.provenance_report(row, memory_id=2, log_root=scratch)
        assert report["citation"]["verdict"] == "session_missing"
        rendered = "\n".join(memory_ui.memory_detail_lines(row, log_root=scratch))
        assert "DANGLING" in rendered
        assert "no longer exists" in rendered
        # The forbidden half: no followable command beside a dead citation.
        assert "open it with:" not in rendered
        ok, why = prov.assert_no_dangling_citation_renders_as_verifiable(report)
        assert ok, why

    def test_a_missing_turn_in_a_live_session_is_STALE_not_DANGLING(self, scratch):
        sid = _session_with_turns(scratch)
        row = {"id": 3, "text": "x", "source": "user",
               "provenance": {"session_id": sid, "turn": "t-99"}}
        report = prov.provenance_report(row, memory_id=3, log_root=scratch)
        assert report["citation"]["verdict"] == "turn_missing"
        rendered = "\n".join(memory_ui.memory_detail_lines(row, log_root=scratch))
        assert "STALE" in rendered and "the rest of the session is intact" in rendered

    def test_no_citation_at_all_is_NEITHER_dangling_nor_stale(self, scratch):
        """Silence from a producer is not a citation that rotted."""
        report = prov.provenance_report({"id": 4, "text": "y", "source": "user"}, memory_id=4)
        assert report["citation"]["verdict"] == "no_session_recorded"
        rendered = "\n".join(prov.provenance_report_lines(report))
        assert "NONE" in rendered
        assert "DANGLING" not in rendered and "STALE" not in rendered

    def test_every_verdict_renders_non_empty_and_passes_its_own_gate(self):
        ok, why = prov.assert_no_dangling_citation_renders_as_verifiable()
        assert ok, why

    def test_the_gate_FIRES_on_a_dead_link_presented_as_live(self):
        """A control arm. A gate that cannot fail measures nothing."""
        report = {
            "citation": {"verdict": "session_missing", "session_id": "sess-x",
                         "reason": "gone", "command": "/lineage show sess-x"},
        }
        lines = prov.citation_lines(report["citation"])
        # Force the bad render the gate exists to catch.
        poisoned = prov.citation_lines(
            {"verdict": "resolvable", "session_id": "sess-x", "command": ""}
        )
        assert poisoned  # sanity: the renderer produced lines
        ok, why = prov.assert_no_dangling_citation_renders_as_verifiable(
            {"citation": {"verdict": "resolvable", "session_id": "sess-x", "command": ""}}
        )
        assert not ok and "without a way to open it" in why
        assert lines


class TestSourceTypeIsVisuallyDistinct:
    def test_the_four_trust_classes_get_their_own_ascii_marker(self):
        assert prov.assert_trust_markers_are_distinct() == (True, "")

    def test_a_shared_marker_is_caught(self):
        broken = dict(prov.TRUST_MARKERS)
        broken["untrusted"] = broken["user_stated"]
        # A COPY, not a reference. `saved = prov.TRUST_MARKERS` aliases the very
        # dict the mutation empties, so the `finally` restored the BROKEN table
        # and every later test in the session failed with KeyError. A control
        # arm that corrupts the module it is testing measures nothing.
        saved = dict(prov.TRUST_MARKERS)
        try:
            prov.TRUST_MARKERS.clear()
            prov.TRUST_MARKERS.update(broken)
            ok, why = prov.assert_trust_markers_are_distinct()
        finally:
            prov.TRUST_MARKERS.clear()
            prov.TRUST_MARKERS.update(saved)
        assert not ok and "share the marker" in why
        assert prov.assert_trust_markers_are_distinct() == (True, "")

    def test_an_untrusted_source_renders_differently_from_a_stated_one(self):
        untrusted = prov.provenance_report({"id": 5, "text": "x", "source": "issue"}, memory_id=5)
        stated = prov.provenance_report({"id": 6, "text": "x", "source": "manual"}, memory_id=6)
        assert untrusted["trust"]["marker"] != stated["trust"]["marker"]
        assert untrusted["trust"]["class"] == "untrusted"
        assert stated["trust"]["class"] == "user_stated"

    def test_a_row_with_no_source_is_unattributed_not_extracted(self):
        """Reading an absent provenance as an observed fact asserts trust from silence."""
        result = prov.trust_class_of({"text": "no source at all"})
        assert result["class"] == "unattributed"
        assert result["marker"] == prov.TRUST_MARKERS["unattributed"]
        assert result["untrusted"] is True

    def test_the_marker_is_the_first_character_of_the_row(self):
        for source, expected in (("issue", "!"), ("manual", "="), ("session", "~")):
            lines = prov.provenance_report_lines(
                prov.provenance_report({"id": 7, "text": "t", "source": source}, memory_id=7)
            )
            assert lines[0].startswith(f"memory {expected} ")


class TestRankingIsExplained:
    def test_a_ranked_memory_carries_a_reason_for_its_position(self):
        ok, why = prov.assert_ranking_is_explained()
        assert ok, why

    def test_the_recall_count_is_shown_and_distinguishes_unknown_from_zero(self):
        receipt = prov.ranking_why(1, ranked=_two_row_receipt())
        assert receipt["explained"] is True
        assert receipt["citations"] is None  # no usage row at all
        rendered = "\n".join(
            prov.provenance_report_lines(
                {
                    "memory_id": 1,
                    "text": "t",
                    "trust": {"marker": "=", "reason": ""},
                    "facts": {},
                    "citation": {"verdict": "no_session_recorded"},
                    "ranking": receipt,
                    "recorded_citations": [],
                    "recorded_citations_available": False,
                    "recorded_citations_reason": "",
                }
            )
        )
        assert "recalled UNKNOWN" in rendered
        assert "NOT zero" in rendered

    def test_the_list_shows_a_recall_count_per_row(self):
        ranked = {
            "available": True,
            "rows": [
                {"id": 1, "text": "a", "marker": "=", "untrusted": False,
                 "trust": {"class": "user_stated", "reason": ""},
                 "ranking": {"rank": 1, "score": 2.25, "citations": 14,
                             "explained": True, "reasons": ("usage 14 citation(s)",)}},
            ],
            "considered": 1,
            "usage_applied": True,
            "dropped_below_floor": 0,
        }
        rendered = "\n".join(memory_ui.ranked_memory_lines(ranked))
        assert "recalled 14x" in rendered
        assert "rank 1" in rendered

    def test_a_list_that_is_not_a_usage_ranking_says_so(self):
        # The reason text is the ranking module's OWN, not a paraphrase: a
        # caveat tested against invented wording proves nothing about the
        # caveat the product actually shows. Ranked without a usage index, that
        # module's reason is its "no usage index was supplied" sentence.
        real = _two_row_receipt().usage_reason
        assert real, "the ranking receipt must carry a reason for not applying usage"
        ranked = {
            "available": True, "rows": [], "considered": 0,
            "usage_applied": False,
            "usage_reason": real,
            "dropped_below_floor": 0,
        }
        rendered = "\n".join(memory_ui.ranked_memory_lines(ranked))
        assert "NOT a usage ranking" in rendered
        assert real.rstrip(".") in rendered
        # The caveat renders even with nothing to rank - the first version
        # returned early on an empty set and dropped it entirely.
        assert "no memory has been applied" in rendered

    def test_a_usage_ranking_says_nothing_about_not_being_one(self):
        ranked = {
            "available": True, "rows": [], "considered": 0,
            "usage_applied": True, "usage_reason": "",
            "dropped_below_floor": 0,
        }
        rendered = "\n".join(memory_ui.ranked_memory_lines(ranked))
        assert "NOT a usage ranking" not in rendered


def _two_row_receipt() -> Any:
    from memory import ranking as ranking_module

    return ranking_module.rank_memories(
        [{"id": 1, "text": "one", "source": "user"}, {"id": 2, "text": "two", "source": "user"}]
    )


class TestRejectionIsDurableAtTheUiLayer:
    def _propose(self, inbox: Path, name: str) -> None:
        (inbox / name).write_text(
            json.dumps({"id": name.split(".")[0], "text": "the same sentence every time",
                        "source_type": "user_stated", "created_at": 1780000000}),
            encoding="utf-8",
        )

    def test_a_re_extraction_cannot_be_accepted_after_a_refusal(self, scratch):
        inbox = scratch / ".inbox"
        inbox.mkdir()
        self._propose(inbox, "p1.json")
        first = memory_ui.memory_command(
            "reject", "p1 --confirm --reason 'not mine'", repo_path=scratch
        )
        assert first["ok"] is True
        # A re-extraction mints a NEW proposal id for the SAME text.
        self._propose(inbox, "p2.json")
        second = memory_ui.memory_command("accept", "p2 --confirm", repo_path=scratch)
        assert second["ok"] is False
        assert second["payload"]["apply"]["refused"] is True
        assert "REFUSED AND STAYS REFUSED" in "\n".join(second["lines"])

    def test_a_refused_proposal_is_visible_with_its_refusal_not_hidden(self, scratch):
        inbox = scratch / ".inbox"
        inbox.mkdir()
        self._propose(inbox, "p1.json")
        memory_ui.memory_command("reject", "p1 --confirm --reason 'no'", repo_path=scratch)
        self._propose(inbox, "p2.json")
        listing = "\n".join(memory_ui.memory_command("list", "", repo_path=scratch)["lines"])
        assert "REFUSED" in listing and "stays refused" in listing
        assert "you said: no" in listing

    def test_show_refuses_to_offer_accept_for_a_refused_proposal(self, scratch):
        """`list` and `show` must not disagree about the refusal."""
        inbox = scratch / ".inbox"
        inbox.mkdir()
        self._propose(inbox, "p1.json")
        memory_ui.memory_command("reject", "p1 --confirm --reason 'no'", repo_path=scratch)
        self._propose(inbox, "p2.json")
        shown = "\n".join(memory_ui.memory_command("show", "p2", repo_path=scratch)["lines"])
        assert "YOU REFUSED THIS" in shown
        assert "accept with:" not in shown

    def test_the_refusal_is_matched_on_content_not_on_proposal_id(self):
        """Two extractions of one sentence have different ids and one digest."""
        first = memory_ui.digest_of("the same sentence")
        second = memory_ui.digest_of("  The   Same   Sentence. ")
        assert first == second


# ---------------------------------------------------------------------------
# W2.2 - generated is never a transcript fact
# ---------------------------------------------------------------------------


class TestGeneratedContentIsLabelled:
    def test_a_compaction_summary_is_labelled_generated(self, scratch):
        from cli import session as cs

        sid = _session_with_turns(scratch)
        conversation = cs.load_or_create(scratch, None, sid, strict=False)
        # A compaction summary and the turns it folded away are BOTH needed:
        # a session carrying `summaries` with no `compacted_turns` has summarised
        # nothing, which is a different state from the one under test.
        conversation["summaries"] = [
            {"text": "## what happened\nthe user asked about eval()", "turn_count": 2,
             "source_turn_ids": ["t-1", "t-2"]}
        ]
        conversation["compacted_turns"] = [
            {"role": "user", "text": "never use eval()", "turn_id": "t-1"},
            {"role": "assistant", "text": "understood", "turn_id": "t-2"},
        ]
        cs.save_session(scratch, conversation)
        report = lineage.pre_compaction_history(scratch, sid)
        rendered = "\n".join(lineage.pre_compaction_lines(report))
        assert lineage.COMPACTION_SUMMARY_LABEL in rendered
        assert "NOT a transcript record" in rendered
        # The turns remain reachable, so the summary is an index not a replacement.
        assert "never use eval()" in rendered
        assert "still on disk" in rendered

    def test_a_branch_summary_is_labelled_and_names_the_files(self):
        rendered = "\n".join(
            lineage.branch_summary_lines(
                "tried an alternative", branch_session_id="e-branch-1",
                generated_by="the model",
            )
        )
        assert lineage.BRANCH_SUMMARY_LABEL in rendered
        assert "NOT a transcript record" in rendered
        assert "still on disk" in rendered
        assert "YOUR FILES ARE UNTOUCHED" in rendered

    def test_a_tiny_branch_summary_still_renders(self):
        """A summary for a one-entry branch: no crash, no empty panel."""
        for summary in ("", "x", None):
            rendered = lineage.branch_summary_lines(summary, branch_session_id="e-b")
            assert rendered
            assert lineage.BRANCH_SUMMARY_LABEL in "\n".join(rendered)

    def test_the_two_generated_labels_are_identical_text(self):
        """One word for a reader to learn, two constants for the code to name."""
        assert lineage.BRANCH_SUMMARY_LABEL == lineage.COMPACTION_SUMMARY_LABEL
        assert len(lineage.GENERATED_LABELS) == 2


# ---------------------------------------------------------------------------
# W2.3 - clearing and compaction cannot be conflated
# ---------------------------------------------------------------------------


class TestClearingAndCompactionAreSeparate:
    def _projection(self, **kwargs: Any) -> Dict[str, Any]:
        return budget.budget_projection(
            meter={
                "rewinds": [{"tokens_reclaimed": 4200, "messages": 12, "bytes": 88000,
                             "tools": ["read", "grep"]}],
                "compactions": [{"tokens_reclaimed": 15000, "turn_count": 14}],
            },
            tokens_used=60000,
            window_tokens=200000,
            **kwargs,
        )

    def test_the_two_receipts_are_two_line_lists(self):
        receipts = budget.reclaim_receipts(self._projection())
        assert set(receipts) == {"clearing", "compaction"}
        assert receipts["clearing"] is not receipts["compaction"]

    def test_clearing_states_re_fetchability_and_compaction_states_the_loss(self):
        receipts = budget.reclaim_receipts(self._projection())
        clearing = "\n".join(receipts["clearing"])
        compaction = "\n".join(receipts["compaction"])
        assert "RE-FETCHABLE" in clearing
        assert "LOSSY" in compaction
        assert "SUMMARISED" in compaction
        # The reassurance must NOT be shared: that was Wave 1's "(kept on disk)"
        # on both rows, which is true of both and reassurance about neither.
        assert "RE-FETCHABLE" not in compaction
        assert "LOSSY" not in clearing

    def test_compaction_offers_the_detail_view(self):
        compaction = "\n".join(budget.compaction_receipt(self._projection()))
        assert "/lineage before" in compaction

    def test_both_report_what_was_dropped(self):
        receipts = budget.reclaim_receipts(self._projection())
        assert "12 messages" in "\n".join(receipts["clearing"])
        assert "88000 bytes" in "\n".join(receipts["clearing"])
        assert "2 tools" in "\n".join(receipts["clearing"])
        # The compaction producer recorded no drop detail, and the receipt says
        # unavailable rather than 0.
        compaction = "\n".join(receipts["compaction"])
        assert "messages unavailable" in compaction
        assert "0 messages" not in compaction

    def test_clearing_is_user_controllable_and_off_is_visible(self):
        on = budget.clearing_receipt(self._projection(config={"clearing_enabled": "on"}))
        off = budget.clearing_receipt(self._projection(config={"clearing_enabled": "off"}))
        assert "policy:      on" in "\n".join(on)
        assert "switched OFF" in "\n".join(off)
        assert "configured by the user" in "\n".join(off)

    def test_a_typo_in_the_knob_falls_back_to_the_lossless_setting(self):
        policy = budget.clearing_policy({"clearing_enabled": "flase"})
        assert policy["policy"] == "auto"
        assert policy["reason"]
        assert budget.clearing_policy({})["policy"] == "auto"

    def test_the_knob_is_not_in_harness_defaults(self):
        """A value in DEFAULTS merges into every task and every eval arm."""
        from harness import config as harness_config

        assert budget.CLEARING_POLICY_KEY not in (harness_config.DEFAULTS or {})

    def test_absent_is_never_zero_for_both_receipts(self):
        empty = budget.budget_projection()
        receipts = budget.reclaim_receipts(empty)
        for cause in ("clearing", "compaction"):
            assert "not measured" in "\n".join(receipts[cause])
        assert budget.assert_absent_is_never_zero(empty) == (True, "")

    def test_the_gate_FIRES_on_a_zero_with_no_measurement(self):
        """A control arm: a gate that cannot fire is a green light on nothing."""
        bad = {"rows": {"clearing": {"tokens": 0}}, "absent": 1, "total": 6, "reasons": {}}
        ok, why = budget.assert_absent_is_never_zero(bad)
        assert not ok and "0 with no measurement" in why

    def test_the_memory_row_does_not_report_a_fabricated_applied_count(self):
        """Wave 1 shipped `len(report_dict)` here, which is always 4.

        `applied_memories` returns a REPORT dict, so `len()` of it is its key
        count - a constant. The rail therefore said "4 remembered" for every
        project on every machine, and the absent-is-never-zero gate could not
        see it because a confident wrong number is not an absent one.
        """
        broken = {"available": True, "count": None, "rows": [],
                  "reason": "the memory store could not be read"}
        saved = memory_ui.applied_memories
        try:
            memory_ui.applied_memories = lambda *a, **k: broken
            projected = budget.budget_projection()
        finally:
            memory_ui.applied_memories = saved
        memory_row = projected["rows"].get("memory", {})
        # The row exists even with no inbox: pending and applied are different
        # questions and neither implies the other.
        assert memory_row, projected["rows"].keys()
        assert memory_row.get("applied") is None
        assert memory_row.get("applied_known") is False

    def test_the_memory_row_survives_the_absence_of_an_inbox(self):
        """Artifacts live outside the checkout, so no inbox is the NORMAL case."""
        projected = budget.budget_projection(repo_path=Path(tempfile.gettempdir()) / "nope")
        memory_row = projected["rows"].get("memory", {})
        assert memory_row, "the memory row must not vanish with the inbox"
        assert memory_row["inbox_available"] is False
        assert "memory_inbox" in projected["reasons"]


# ---------------------------------------------------------------------------
# W2.4 - the session tree is safe without reading the docs
# ---------------------------------------------------------------------------


class TestTheSessionTree:
    def _tree(self, scratch: Path, sid: str = "sess-tree99") -> Any:
        from runtime.session_tree import SessionTree

        return SessionTree(lineage.tree_root(scratch, sid))

    def test_the_verb_table_and_the_registry_cannot_drift(self):
        import cli.commands as commands

        assert tuple(spec.name for spec in commands.SUBCOMMANDS["/tree"]) == lineage.TREE_VERBS

    def test_a_leaf_only_conversation_renders_a_tree_view(self, scratch):
        snapshot = lineage.tree_snapshot(scratch, "sess-nothing")
        rendered = "\n".join(lineage.tree_lines(snapshot))
        assert "never been branched" in rendered
        assert "no tree yet" in rendered

    def test_the_view_shows_reason_and_head(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        tree.fork(label="the other approach")
        tree.advance(turn_id="t-2", text="carried on")
        rendered = "\n".join(
            lineage.tree_lines(lineage.tree_snapshot(scratch, "sess-tree99"))
        )
        assert "created because: fork" in rendered
        assert "head:" in rendered
        assert "H " in rendered  # the head marker

    def test_a_generated_claim_is_marked_in_the_tree_view(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        forked = tree.fork(label="the other approach")
        tree.abandon(
            label="the other approach", summary="the branch stopped early",
            evidence=(str(forked["entry_id"]),),
        )
        rendered = "\n".join(
            lineage.tree_lines(lineage.tree_snapshot(scratch, "sess-tree99"))
        )
        assert "generated claim" in rendered
        assert lineage.BRANCH_SUMMARY_LABEL in rendered

    def test_the_branch_remains_accessible_after_a_merge(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        forked = tree.fork(label="the other approach")
        branch_id = str(forked["entry_id"])
        # Forking from the ROOT turn leaves the new branch as the head, because
        # T3 anchors a fork under the fork point's parent. Advancing moves the
        # head back onto the main line, which is the state a branch is in
        # normally - and `_do_merge` correctly refuses to merge the head.
        tree.advance(turn_id="t-2", text="the conversation carried on")
        result = lineage.tree_command(
            "merge", branch_id, log_root=scratch, session_id="sess-tree99"
        )
        assert result["ok"] is True, result["lines"]
        assert result["payload"]["history_retained"] is True
        # The entry is still on disk after the merge.
        assert tree.entry(branch_id) is not None
        assert "still on disk" in "\n".join(result["lines"])

    def test_a_diverged_branch_is_refused_and_nothing_is_written(self, scratch):
        tree = self._tree(scratch)
        root_turn = tree.advance(turn_id="t-1", text="the original ask")
        branch = tree.append(
            "branch", text="the other approach",
            parent_id=str(getattr(root_turn, "parent_id", "") or ""),
            evidence=("e-turn-elsewhere",),
        )
        tree.advance(turn_id="t-2", text="carried on")
        before = len(tree.entries())
        result = lineage._do_merge(
            str(branch.id), repo=None, log_root=scratch, session_id="sess-tree99"
        )
        assert result["ok"] is False
        assert result["payload"]["diverged"] is True
        rendered = "\n".join(result["lines"])
        assert "REFUSED" in rendered and "nothing was written" in rendered
        assert len(tree.entries()) == before

    def test_a_delete_refuses_without_confirmation_and_states_the_count(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        forked = tree.fork(label="the other approach")
        branch_id = str(forked["entry_id"])
        tree.advance(turn_id="t-2", text="carried on")
        result = lineage.tree_command(
            "delete", branch_id, log_root=scratch, session_id="sess-tree99"
        )
        assert result["ok"] is False
        rendered = "\n".join(result["lines"])
        assert "rewind point" in rendered
        assert "--confirm" in rendered
        assert result["payload"]["rewind_points"] >= 1
        assert tree.entry(branch_id) is not None  # nothing was deleted

    def test_a_confirmed_delete_reports_rotation_rather_than_erasure(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        forked = tree.fork(label="the other approach")
        branch_id = str(forked["entry_id"])
        tree.advance(turn_id="t-2", text="carried on")
        result = lineage.tree_command(
            "delete", branch_id + " --confirm", log_root=scratch, session_id="sess-tree99"
        )
        assert result["ok"] is True
        rendered = "\n".join(result["lines"])
        assert "NOTHING WAS ERASED" in rendered
        assert "rotated" in rendered

    def test_a_prune_is_a_dry_run_by_default(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="something to retain")
        result = lineage.tree_command("prune", "", log_root=scratch, session_id="sess-tree99")
        assert result["payload"]["dry_run"] is True
        assert "DRY RUN" in "\n".join(result["lines"])

    def test_abandon_states_that_the_files_are_untouched(self, scratch):
        tree = self._tree(scratch)
        tree.advance(turn_id="t-1", text="the original ask")
        forked = tree.fork(label="the other approach")
        tree.advance(turn_id="t-2", text="carried on")
        result = lineage.tree_command(
            "abandon", str(forked["entry_id"]), log_root=scratch,
            session_id="sess-tree99", summary="stopped early",
        )
        assert result["ok"] is True
        rendered = "\n".join(result["lines"])
        assert "YOUR FILES ARE UNTOUCHED" in rendered
        assert result["payload"]["files_touched"] is False

    def test_the_fork_disclosure_names_the_working_tree_in_all_three_verdicts(self):
        for isolation in ("isolated", "shared", "refused"):
            rendered = "\n".join(
                lineage.fork_isolation_lines(
                    isolation=isolation, parent_session_id="p", child_session_id="c"
                )
            )
            assert "WORKING TREE" in rendered

    def test_reading_the_lineage_of_a_missing_session_does_not_create_it(self, scratch):
        """A read that writes makes 'no record' and 'a new empty session' identical."""
        from cli import session as cs

        rows = lineage.lineage_rows(scratch, "sess-00000000")
        assert rows
        assert not rows[0].get("loaded", True)
        assert not cs._session_path(scratch, "sess-00000000").exists()


# ---------------------------------------------------------------------------
# W2.5 - the eleven surfaces
# ---------------------------------------------------------------------------


class TestTheElevenSurfaces:
    def test_all_eleven_are_declared_and_each_renders_on_its_edge_case(self):
        report = truth.surface_report()
        assert report["surfaces"] == 11
        assert report["rendered"] == 11
        assert report["clean"] is True, report["problems"]
        assert {row["surface"] for row in report["rows"]} == set(truth.SURFACES)

    def test_the_gate_names_the_failing_surface(self):
        ok, why = truth.assert_every_surface_renders()
        assert ok, why

    def test_no_surface_renders_a_traceback(self):
        for row in truth.surface_report()["rows"]:
            ok, why = truth.assert_no_traceback_unless_it_is_one(row["lines"])
            assert ok, f"{row['surface']}: {why}"

    def test_memory_content_goes_through_the_sanitiser(self):
        assert truth.assert_memory_content_is_sanitised() == (True, "")

    def test_the_sanitiser_gate_FIRES_on_an_unsanitised_surface(self):
        """A control arm. Poisoning the sanitiser must make this gate red."""
        from cli import ui as cli_ui

        saved = cli_ui.sanitize_text
        try:
            cli_ui.sanitize_text = lambda value: str(value)  # the leak
            ok, why = truth.assert_memory_content_is_sanitised()
        finally:
            cli_ui.sanitize_text = saved
        assert not ok and "credential-shaped payload" in why

    def test_an_empty_inbox_is_distinguished_from_no_inbox(self, scratch):
        inbox = scratch / ".inbox"
        inbox.mkdir()
        empty = memory_ui.inbox_status(scratch)
        missing = memory_ui.inbox_status(scratch / "nowhere")
        assert empty["available"] is True and empty["count"] == 0
        assert missing["available"] is False and missing["count"] is None
        assert "0 proposal(s) pending" in "\n".join(memory_ui.status_lines(empty))
        assert "UNAVAILABLE" in "\n".join(memory_ui.status_lines(missing))

    def test_a_memory_whose_source_is_deleted_says_so_on_both_surfaces(self, scratch):
        from cli import session as cs

        sid = _session_with_turns(scratch)
        row = {"id": 9, "text": "never use eval()", "source": "user",
               "provenance": {"session_id": sid, "turn": "t-1"}}
        live = "\n".join(memory_ui.memory_detail_lines(row, log_root=scratch))
        cs._session_path(scratch, sid).unlink()
        journal = cs._event_log_path(scratch, sid)
        if journal.exists():
            journal.unlink()
        gone = "\n".join(memory_ui.memory_detail_lines(row, log_root=scratch))
        assert "opens:" in live
        assert "DANGLING" in gone and "opens:" not in gone

    def test_a_tiny_branch_summary_renders_in_the_surface_driver(self):
        row = next(r for r in truth.surface_report()["rows"] if r["surface"] == "branch_summary")
        assert row["non_empty"] and not row["missing"]


class TestThePinsCannotDrift:
    def test_every_memory_fact_is_rendered_by_the_detail_surface(self):
        report = prov.provenance_report({"id": 1, "text": "t", "source": "user"}, memory_id=1)
        rendered = "\n".join(prov.provenance_report_lines(report))
        for fact in prov.MEMORY_FACTS:
            assert fact.split("_")[0] in rendered.lower() or fact in rendered

    def test_the_verb_table_partitions_into_destructive_and_lossless(self):
        assert set(lineage.DESTRUCTIVE_TREE_VERBS) <= set(lineage.TREE_VERBS)
        assert set(lineage.LOSSLESS_TREE_VERBS) <= set(lineage.TREE_VERBS)
        assert not (set(lineage.DESTRUCTIVE_TREE_VERBS) & set(lineage.LOSSLESS_TREE_VERBS))

    def test_a_read_only_memory_verb_cannot_write(self):
        assert memory_ui.assert_never_applies_without_a_human_act() == (True, "")
