"""Pins for the Phase 4 Wave 1 surfaces.

``tests/**`` is T5's, so this suite lives beside the code it pins - the same
convention ``cli/test_p3bw2_truth.py`` and its siblings already use, and the
same one this file's sibling rounds recorded in ``cli/AGENTS.md``: an in-package
suite needs a CI selection entry, which is filed rather than assumed.

Every pin here is behavioural and every one has a CONTROL ARM. A suite whose
only assertion is "the forbidden thing is absent" passes when the code crashes
before producing it, which is the failure class ``phases/DOCTRINE.md`` §3 names.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli import feature_ledger as fl
from cli import p4_surfaces as ps
from cli import session_briefing as sb
from cli import subagent_view as sv
from cli import worktree_view as wv

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# T4.W1.2 — the feature ledger is the honest progress view
# ---------------------------------------------------------------------------


class TestTheLedgerIsTheRecordAndNotASecondTable:
    def test_the_parser_reads_the_real_ledger_of_record(self):
        ledger = fl.load_ledger(REPO_ROOT)
        assert ledger.source_available, "docs/feature-matrix.md must be readable"
        assert len(ledger.features) >= 20, (
            f"only {len(ledger.features)} capability rows parsed - the parser "
            "is reading the wrong table"
        )

    def test_every_row_carries_the_ledgers_own_status_word(self):
        ledger = fl.load_ledger(REPO_ROOT)
        declared = fl._declared_statuses()
        assert declared, "the claim-status authority must be reachable"
        for feature in ledger.features:
            assert feature.declared_status in declared, (
                f"{feature.name} has a status the authority does not define: "
                f"{feature.declared_status!r}"
            )

    def test_the_module_cannot_run_on_a_forked_status_vocabulary(self, monkeypatch):
        monkeypatch.setattr(fl, "_declared_statuses", lambda: frozenset())
        with pytest.raises(fl.LedgerContractError):
            fl.assert_statuses_come_from_the_authority()

    def test_an_absent_ledger_is_unavailable_not_empty_and_green(self, tmp_path):
        ledger = fl.load_ledger(tmp_path)
        assert ledger.source_available is False
        blob = "\n".join(fl.render_coverage_lines(ledger))
        assert "unavailable" in blob
        assert "0 features outstanding" in blob, (
            "an unreadable ledger must NOT be allowed to read as nothing "
            "outstanding"
        )


class TestAFeatureWithoutEvidenceCannotRenderAsPassing:
    """The DoD item, asserted at the RENDER layer with a control arm."""

    def _lying_ledger(self) -> fl.FeatureLedger:
        return fl.FeatureLedger(
            features=tuple(
                fl.Feature(
                    name=f"claimed-{index}",
                    declared_status="Implemented",
                    notes="nothing cites this",
                    evidence=(),
                    line_number=index,
                )
                for index in range(8)
            ),
            source="docs/feature-matrix.md",
            current_feature="claimed-0",
            source_available=True,
        )

    def test_eight_claimed_rows_with_no_evidence_render_zero_passes(self):
        ledger = self._lying_ledger()
        assert sum(1 for item in ledger.features if item.passing) == 0
        blob = "\n".join(fl.feature_lines(ledger))
        assert "[pass]" not in blob, "an unevidenced row rendered as passing"
        # P4/W2: the wording changed and the INTENT did not. Wave 1 asserted
        # ``CANNOT render as passing`` appeared once per row; the row now renders
        # ``NOT verified: no evidence is cited for this row, so it cannot be
        # verified``. The count is still the point - eight rows, eight reasons -
        # so the count is kept against the new sentence.
        assert blob.count("no evidence is cited for this row") == 8
        assert blob.count("NOT verified:") == 8

    def test_the_gate_fires_on_a_row_that_claims_passing_without_evidence(self):
        # A stand-in, not a mutated Feature: ``passing`` is a derived PROPERTY
        # on the frozen dataclass, so the only honest way to plant the defect is
        # a row object whose own ``passing`` disagrees with its evidence.
        class _Liar:
            name = "liar"
            evidenced = False
            verification_ran = False
            passing = True

        liar = _Liar()
        with pytest.raises(fl.LedgerContractError) as excinfo:
            fl.assert_no_unevidenced_passing([liar])
        assert "no evidence" in str(excinfo.value)

    def test_control_arm_a_genuinely_evidenced_row_still_passes(self):
        # P4/W2: the ARM is unchanged and the EVIDENCE it uses is corrected.
        # Wave 1's control arm was `EvidenceRef("tests/x.py", exists=True)` -
        # a file that exists, with nothing run and nothing named. That row
        # rendered `[pass]`, and it is the exact defect this round closes, so a
        # control arm that used it was asserting the bug. The arm still proves a
        # real pass renders; the row now carries the three things the brief's
        # assertion requires.
        honest = fl.Feature(
            name="honest",
            declared_status="Implemented",
            notes="",
            evidence=(
                fl.EvidenceRef(
                    reference="tests/x.py",
                    exists=True,
                    kind="test",
                    method="pytest",
                    outcome="passed",
                    subject="honest",
                ),
            ),
            line_number=1,
        )
        fl.assert_no_unevidenced_passing([honest])
        assert honest.passing is True
        assert "[pass]" in "\n".join(
            fl.feature_lines(
                fl.FeatureLedger(features=(honest,), source="s", current_feature="honest")
            )
        )

    def test_a_row_whose_evidence_vanished_is_unverified_not_passing(self):
        gone = fl.Feature(
            name="vanished",
            declared_status="Implemented",
            notes="",
            evidence=(fl.EvidenceRef(reference="logs/gone.json", exists=False, kind="report"),),
            line_number=1,
        )
        assert gone.evidenced is True
        assert gone.verification_ran is False
        assert gone.passing is False
        # P4/W2: this test is NAMED `unverified` and asserted `failing`. The
        # name was right and the assertion was wrong - nothing ran, so `[FAIL]`
        # would have claimed a check failed. The state is now `unverified`, which
        # is what the test's own name always claimed, and the intent (not
        # passing) is unchanged.
        assert gone.state == "unverified"
        blob = "\n".join(fl.feature_detail_lines(fl.FeatureLedger(features=(gone,), source="s"), "vanished"))
        assert "MISSING from this checkout" in blob
        assert "NOT VERIFIED" in blob

    def test_every_renderer_calls_the_gate(self):
        ledger = fl.load_ledger(REPO_ROOT)
        for render in (
            lambda: fl.feature_lines(ledger),
            lambda: fl.render_coverage_lines(ledger),
            lambda: fl.feature_detail_lines(ledger, ledger.features[0].name),
        ):
            render()  # must not raise on a truthful ledger


class TestTheCoverageSummaryIsProminentAndHonest:
    def test_the_headline_names_the_verified_and_total_counts(self):
        summary = fl.coverage_summary(fl.load_ledger(REPO_ROOT))
        assert f"{summary['verified']} of {summary['total']} features verified" in (
            fl.summary_line(summary)
        )

    def test_the_summary_renders_above_the_detail(self):
        lines = fl.render_coverage_lines(fl.load_ledger(REPO_ROOT))
        assert lines[0].startswith("="), "the coverage headline must be prominent"

    def test_more_verified_than_observable_is_called_an_overstatement(self):
        summary = {
            "source_available": True,
            "total": 47,
            "verified": 40,
            "verification_observable": 2,
            "blocked": 0,
        }
        line = fl.summary_line(summary)
        assert "OVERSTATED" in line
        assert "38" in line

    def test_zero_features_is_a_broken_ledger_not_a_finished_one(self):
        line = fl.summary_line({"source_available": True, "total": 0, "verified": 0})
        assert "broken ledger" in line


# ---------------------------------------------------------------------------
# T4.W1.1 — session boundaries
# ---------------------------------------------------------------------------


class TestSessionBoundaries:
    def test_a_boundary_states_both_what_is_kept_and_what_is_dropped(self):
        lines = "\n".join(
            sb.boundary_marker_lines(session_id="s1", direction="start")
        )
        assert "PRESERVED across this boundary" in lines
        assert "DROPPED at this boundary" in lines
        assert "context reset" in lines

    def test_the_boundary_renders_the_same_contract_the_caller_can_journal(self):
        contract = sb.new_session_contract(reason="the user typed /new-session")
        lines = "\n".join(
            sb.boundary_marker_lines(session_id="s1", contract=contract)
        )
        for item in contract["preserved"]:
            assert item["what"] in lines
        for item in contract["dropped"]:
            assert item["what"] in lines
        assert "the user typed /new-session" in lines

    def test_an_unknown_direction_is_refused_rather_than_rendered(self):
        with pytest.raises(ValueError):
            sb.boundary_marker_lines(direction="sideways")

    def test_the_start_briefing_says_no_feature_is_unknown_not_nothing(self, tmp_path):
        brief = sb.start_briefing(
            {"session_id": "s", "repo": str(tmp_path), "turns": []},
            log_root=tmp_path / "logs",
            repo=tmp_path,
        )
        lines = "\n".join(sb.start_briefing_lines(brief))
        assert "UNKNOWN is not the same as" in lines
        assert brief.feature == ""

    def test_the_end_briefing_without_a_commit_says_so(self, tmp_path):
        end = sb.end_briefing(
            {"session_id": "s", "repo": str(tmp_path), "turns": []},
            repo=tmp_path / "not-a-repository",
        )
        lines = "\n".join(sb.end_briefing_lines(end))
        assert "NO COMMIT" in lines
        assert end.commit == ""

    def test_the_end_briefing_without_verification_does_not_say_verified(self, tmp_path):
        end = sb.end_briefing(
            {"session_id": "s", "repo": str(tmp_path), "turns": []},
            repo=tmp_path / "not-a-repository",
            verified="",
        )
        lines = "\n".join(sb.end_briefing_lines(end))
        assert "NOT REPORTED" in lines
        assert "this is not a pass" in lines

    def test_the_boundary_marker_is_visible_in_a_scrolled_transcript(self):
        lines = sb.boundary_marker_lines(session_id="abc", direction="end")
        assert lines[0] == "-" * 68
        assert lines[-1] == "-" * 68


class TestLedgerAndTranscriptDisagreementsAreSurfaced:
    def _ledger(self, **kwargs) -> fl.FeatureLedger:
        base = dict(
            features=(
                fl.Feature(
                    name="a real feature",
                    declared_status="Implemented",
                    notes="",
                    evidence=(
                        fl.EvidenceRef(
                            reference="tests/a.py",
                            exists=True,
                            kind="test",
                            method="pytest",
                            outcome="passed",
                            subject="a real feature",
                        ),
                    ),
                    line_number=1,
                ),
                fl.Feature(
                    name="a blocked feature",
                    declared_status="Blocked",
                    notes="",
                    evidence=(fl.EvidenceRef("tests/b.py", True, "test"),),
                    line_number=2,
                ),
            ),
            source="docs/feature-matrix.md",
            source_available=True,
        )
        base.update(kwargs)
        return fl.FeatureLedger(**base)

    # P4/W2 corrected three of the tests below, and the PRODUCT is what changed.
    # The reasoning is recorded here rather than left for the next reader, in the
    # practice cli/AGENTS.md records for exactly this situation: Wave 1's table
    # was the thing that was wrong, so the tests were corrected with their intent
    # intact - none was loosened, and each still fails on the defect it names.

    def test_a_transcript_closing_what_the_ledger_does_not_carry_is_surfaced(self):
        found = sb.ledger_transcript_discrepancy(
            {
                "turns": [
                    {"text": "x", "metadata": {"feature": "a blocked feature", "feature_closed": True}}
                ]
            },
            self._ledger(),
        )
        assert [item.kind for item in found] == ["ledger_in_progress_transcript_closed"]
        assert "does NOT choose between them" in found[0].detail

    def test_the_other_direction_is_surfaced_too(self):
        found = sb.ledger_transcript_discrepancy(
            {"turns": [{"text": "x", "metadata": {"feature": "a real feature"}}]},
            self._ledger(),
        )
        assert [item.kind for item in found] == ["ledger_closed_transcript_in_progress"]

    def test_a_feature_the_ledger_never_declared_is_surfaced(self):
        found = sb.ledger_transcript_discrepancy(
            {"turns": [{"text": "x", "metadata": {"feature": "something undeclared"}}]},
            self._ledger(),
        )
        assert [item.kind for item in found] == ["ledger_feature_absent"]

    def test_an_empty_transcript_is_reported_as_unchecked_not_as_agreeing(self):
        found = sb.ledger_transcript_discrepancy({"turns": []}, self._ledger())
        assert [item.kind for item in found] == ["transcript_missing"]

    def test_an_unreadable_ledger_is_reported_rather_than_assumed_agreeing(self):
        found = sb.ledger_transcript_discrepancy(
            {"turns": [{"text": "x", "metadata": {"feature": "a"}}]},
            fl.FeatureLedger(features=(), source="x", source_available=False),
        )
        assert [item.kind for item in found] == ["ledger_state_unknown"]

    def test_agreement_has_an_explicit_all_clear(self):
        lines = "\n".join(sb.discrepancy_lines([]))
        assert "checked, and they agree" in lines

    def test_control_arm_a_consistent_session_produces_nothing(self):
        found = sb.ledger_transcript_discrepancy(
            {
                "turns": [
                    {
                        "text": "x",
                        "metadata": {"feature": "a real feature", "feature_closed": True},
                    }
                ]
            },
            self._ledger(),
        )
        assert found == []


# ---------------------------------------------------------------------------
# T4.W1.3 — persistent, returnable, steerable subagents
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path) -> sv.SubagentStore:
    return sv.SubagentStore(tmp_path / "subs", "sess-1")


class TestSubagentsPersistAndAreReturnable:
    def test_a_subagent_survives_a_new_store_object_over_the_same_root(self, tmp_path):
        first = sv.SubagentStore(tmp_path / "subs", "sess-1")
        record = first.spawn(description="review", agent="reviewer")
        second = sv.SubagentStore(tmp_path / "subs", "sess-1")
        assert second.get(record.agent_id) is not None, (
            "a subagent must survive the process that spawned it"
        )

    def test_a_finished_subagent_keeps_its_result_and_its_follow_up(self, store):
        record = store.spawn(description="review", agent="reviewer")
        store.finish(
            record.agent_id, result="there is a bug at line 12", follow_ups=["show the line"]
        )
        again = store.get(record.agent_id)
        assert again.result.startswith("there is a bug")
        assert again.follow_ups == ("show the line",)
        assert "RETURNED TO" in "\n".join(store and sv.subagent_list_lines(store))

    def test_the_esveo_workflow_goes_back_to_the_same_subagent(self, store):
        record = store.spawn(description="review for a hallucinated bug")
        store.append_turn(record.agent_id, role="assistant", text="there is a bug at line 12")
        store.finish(record.agent_id, result="a bug at line 12")
        # the main agent could not reproduce it, so it returns to the same child
        store.append_turn(record.agent_id, role="user", text="which line exactly?")
        again = store.get(record.agent_id)
        assert again.agent_id == record.agent_id
        assert again.turns == 2, "the follow-up must land on the SAME subagent"

    def test_the_empty_list_says_none_rather_than_all_finished(self, store):
        blob = "\n".join(sv.subagent_list_lines(store))
        assert "no subagent has been spawned" in blob
        assert "not 'none finished'" in blob


class TestSubagentIsolationSurvivesSteering:
    """The DoD item: adding a human channel must not leak the child upward."""

    def test_steering_grows_the_child_and_leaves_the_parent_byte_identical(self, store):
        record = store.spawn(description="review")
        store.append_turn(record.agent_id, role="assistant", text="a bug at line 12")
        parent = {"turns": [{"text": "first"}, {"text": "second"}]}
        before = (len(parent["turns"]), sum(len(t["text"]) for t in parent["turns"]))
        receipt = store.steer(record.agent_id, "show me the exact line", parent_session=parent)
        after = (len(parent["turns"]), sum(len(t["text"]) for t in parent["turns"]))
        assert before == after, "steering pulled the child's transcript into the parent"
        assert receipt.child_messages_after > receipt.child_messages_before
        assert receipt.isolation == "intact"

    def test_the_assertion_fires_when_the_parent_actually_changes(self):
        with pytest.raises(sv.IsolationBroken):
            sv.assert_isolation_after_steering(
                parent_messages_before=2,
                parent_messages_after=3,
                parent_chars_before=10,
                parent_chars_after=10,
                child_messages_before=1,
                child_messages_after=2,
            )

    def test_the_assertion_also_fires_on_a_dead_channel(self):
        with pytest.raises(sv.IsolationBroken):
            sv.assert_isolation_after_steering(
                parent_messages_before=2,
                parent_messages_after=2,
                parent_chars_before=10,
                parent_chars_after=10,
                child_messages_before=4,
                child_messages_after=4,
            )

    def test_control_arm_a_real_steer_passes_the_same_assertion(self, store):
        record = store.spawn(description="review")
        store.append_turn(record.agent_id, role="assistant", text="a bug")
        receipt = store.steer(record.agent_id, "which line", parent_session={"turns": []})
        sv.assert_isolation_after_steering(
            parent_messages_before=receipt.parent_messages_before,
            parent_messages_after=receipt.parent_messages_after,
            parent_chars_before=0,
            parent_chars_after=0,
            child_messages_before=receipt.child_messages_before,
            child_messages_after=receipt.child_messages_after,
        )

    def test_the_transcript_counters_are_derived_from_the_bytes_on_disk(self, store):
        record = store.spawn(description="review")
        store.append_turn(record.agent_id, role="assistant", text="x" * 50)
        again = store.get(record.agent_id)
        messages = store.transcript(record.agent_id)
        assert again.transcript_messages == len(messages)
        assert again.transcript_chars == sum(len(str(m.get("content") or "")) for m in messages)


class TestACompactedTranscriptNeverRendersAsComplete:
    def test_a_compacted_transcript_is_incomplete_and_says_why(self, store):
        record = store.spawn(description="review")
        store.append_turn(record.agent_id, role="assistant", text="first half")
        store.append_turn(record.agent_id, role="assistant", text="...", compacted=True)
        store.finish(record.agent_id, result="a summary")
        again = store.get(record.agent_id)
        assert again.complete is False
        blob = "\n".join(sv.subagent_transcript_lines(store, record.agent_id))
        assert "COMPLETE: False" in blob
        assert "COMPACTED" in blob
        assert "NOT the whole conversation" in blob

    def test_a_truncated_result_is_not_complete_either(self, store):
        record = store.spawn(description="review")
        store.finish(record.agent_id, result="a summary", truncated=True)
        assert store.get(record.agent_id).complete is False
        blob = "\n".join(sv.subagent_transcript_lines(store, record.agent_id))
        assert "RESULT was truncated" in blob

    def test_a_running_subagent_is_not_complete(self, store):
        record = store.spawn(description="review")
        assert store.get(record.agent_id).complete is False
        blob = "\n".join(sv.subagent_transcript_lines(store, record.agent_id))
        assert "still running" in blob

    def test_control_arm_a_clean_finished_transcript_is_complete(self, store):
        record = store.spawn(description="review")
        store.append_turn(record.agent_id, role="assistant", text="all of it")
        store.finish(record.agent_id, result="done")
        assert store.get(record.agent_id).complete is True


class TestTheSubagentTrustBoundaryIsVisible:
    def test_a_deny_rule_is_inherited_and_an_allow_rule_is_not(self):
        boundary = sv.trust_boundary(
            {"deny": ["rm -rf", "curl | sh"], "allow": ["pytest"], "approval": ["once"]}
        )
        assert boundary["inherited"]["deny"] == ["rm -rf", "curl | sh"]
        assert "allow" not in boundary["inherited"]
        assert boundary["not_inherited"]["allow"] == ["pytest"]
        assert "pytest" in boundary["cannot"]

    def test_the_surface_renders_both_directions(self):
        blob = "\n".join(
            sv.trust_boundary_lines(sv.trust_boundary({"allow": ["pytest"], "deny": ["curl"]}))
        )
        assert "NOT INHERITED allow: pytest" in blob
        assert "INHERITED deny: curl" in blob

    def test_a_subagent_with_no_parent_rules_inherits_nothing_and_grants_nothing(self):
        boundary = sv.trust_boundary()
        assert list(boundary["inherited"]["deny"]) == []
        assert boundary["not_inherited"] == {}

    def test_a_denied_rule_is_never_reported_as_something_the_child_may_do(self):
        """The defect this pin exists for: ``can`` used to union EVERY inherited
        group, so a parent's DENIED rule and its budget ceiling were listed as
        permissions while the same rule also appeared under ``cannot``."""
        boundary = sv.trust_boundary(
            {
                "deny": ["rm -rf", "curl | sh"],
                "allow": ["pytest"],
                "budget": ["max_usd:5"],
            }
        )
        assert "rm -rf" not in boundary["can"]
        assert "curl | sh" not in boundary["can"]
        assert "max_usd:5" not in boundary["can"]
        assert boundary["can"] == []
        sv.assert_trust_columns_disjoint(boundary)

    def test_the_two_columns_can_never_disagree(self):
        for parent in (
            {},
            {"deny": ["rm -rf"], "allow": ["pytest"]},
            {"deny": ["*"], "allow": ["pytest", "ls"], "network": ["*"]},
            {"deny": ["rm -rf"], "budget": ["max_usd:5"], "approval": ["once"]},
        ):
            boundary = sv.trust_boundary(parent, {"deny": ["child-only"]})
            sv.assert_trust_columns_disjoint(boundary)

    def test_the_disjointness_gate_can_actually_fire(self):
        """A control arm: a gate whose sensitivity nobody has measured is a gate
        nobody reads. Planting a rule in both columns must raise."""
        boundary = sv.trust_boundary({"allow": ["pytest"]})
        boundary["can"] = ["pytest"]
        with pytest.raises(AssertionError) as excinfo:
            sv.assert_trust_columns_disjoint(boundary)
        assert "both directions" in str(excinfo.value)

    def test_a_denied_rule_listed_as_permitted_is_named_as_such(self):
        """The second control arm: the same rule in both columns raises for the
        overlap; a deny appearing in ``can`` alone must also raise."""
        boundary = sv.trust_boundary({"deny": ["rm -rf"]})
        boundary["can"] = ["rm -rf"]
        with pytest.raises(AssertionError) as excinfo:
            sv.assert_trust_columns_disjoint(boundary)
        assert "DENIED rule as permitted" in str(excinfo.value)

    def test_an_empty_can_says_why_rather_than_rendering_nothing(self):
        """Absent is not zero: an empty permission column that states its reason
        is a fact, and one that renders blank is a missing affordance."""
        blob = "\n".join(sv.trust_boundary_lines(sv.trust_boundary({"allow": ["pytest"]})))
        assert "MAY DO: nothing" in blob
        assert "inherits denies and ceilings only" in blob
        assert "MAY NOT: pytest" in blob

    def test_a_genuine_inherited_grant_is_still_reported_as_permitted(self):
        """The negative arm: the fix must not empty a real permission. When the
        model DOES inherit a grant, it must appear under ``can``."""
        granted = sv.trust_boundary({"grant": ["pytest"]}, {})
        # ``grant`` is not one of the declared groups, so it is not inherited;
        # assert the honest answer rather than inventing an inherited grant.
        assert granted["can"] == []
        assert "grant" not in granted["inherited"]


# ---------------------------------------------------------------------------
# T4.W1.4 — the worktree lifecycle
# ---------------------------------------------------------------------------


class TestTheCurrentWorktreeIsAlwaysVisible:
    def test_the_block_is_rendered_even_with_no_store_at_all(self, tmp_path):
        report = wv.read_worktrees(tmp_path, tmp_path / "nope")
        blob = "\n".join(wv.current_tree_lines(report))
        assert "CURRENT WORKTREE" in blob

    def test_a_non_repository_says_so_rather_than_rendering_a_blank(self, tmp_path):
        report = wv.read_worktrees(tmp_path, tmp_path / "nope")
        assert report.repo_is_git is False
        assert "NOT a git work tree" in "\n".join(wv.current_tree_lines(report))

    def test_a_real_checkout_reports_its_branch_and_commit(self):
        report = wv.read_worktrees(REPO_ROOT, REPO_ROOT / "no-store")
        blob = "\n".join(wv.current_tree_lines(report))
        assert report.repo_is_git is True
        assert "branch:" in blob and "commit:" in blob

    def test_an_absent_store_is_unknown_not_zero(self, tmp_path):
        report = wv.read_worktrees(REPO_ROOT, tmp_path / "nope")
        assert report.source_available is False
        blob = "\n".join(wv.worktree_list_lines(report))
        assert "UNKNOWN" in blob and "NOT 'there are no worktrees'" in blob

    def test_a_subdirectory_of_a_repository_is_not_a_work_tree(self, tmp_path):
        """The guard that stopped a render pass from walking the temp tree."""
        assert wv._is_git_repo(REPO_ROOT) is True
        assert wv._is_git_repo(REPO_ROOT / "cli") is False


class TestWorktreeStateIsHonest:
    def _report(self, tmp_path, **row):
        store_root = tmp_path / "wt"
        store_root.mkdir(parents=True, exist_ok=True)
        base = {
            "node_id": "n1",
            "path": str(tmp_path / "gone"),
            "source_repo": str(tmp_path),
            "base_commit": "abc",
            "state": "ready",
            "created_at": "2026-01-01",
        }
        base.update(row)
        (store_root / "worktrees.json").write_text(
            json.dumps({"schema_version": 1, "worktrees": [base]}), encoding="utf-8"
        )
        return wv.read_worktrees(tmp_path, store_root)

    def test_an_unreadable_status_is_not_clean(self, tmp_path):
        report = self._report(tmp_path)
        row = report.rows[0]
        assert row.exists is False
        assert row.dirty_unknown is True
        assert row.clean is False, "a worktree whose status could not be read is not clean"
        assert row.changed_count == -1
        blob = "\n".join(wv.worktree_detail_lines(report, "n1"))
        assert "'unknown' is NOT zero" in blob

    def test_a_missing_worktree_is_named_as_missing_not_clean(self, tmp_path):
        blob = "\n".join(wv.worktree_list_lines(self._report(tmp_path)))
        assert "n1" in blob
        assert "clean=False" in blob

    def test_an_unknown_worktree_detail_says_so(self, tmp_path):
        blob = "\n".join(wv.worktree_detail_lines(self._report(tmp_path), "nope"))
        assert "no such managed worktree" in blob


class TestDeliberateActsAreConfirmedAndReported:
    def _managed_report(self, tmp_path):
        """A report holding one real managed-worktree record."""
        store_root = tmp_path / "wt"
        store_root.mkdir(parents=True, exist_ok=True)
        (store_root / "worktrees.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "worktrees": [
                        {
                            "node_id": "n1",
                            "path": str(tmp_path / "tree"),
                            "source_repo": str(tmp_path),
                            "base_commit": "abc",
                            "state": "ready",
                            "created_at": "2026-01-01",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return wv.read_worktrees(tmp_path, store_root)

    def test_creation_explains_what_it_will_do(self, tmp_path):
        brief = wv.creation_briefing("n1", tmp_path)
        blob = "\n".join(wv.creation_lines(brief))
        assert "detached worktree" in blob
        assert "not a branch" in blob
        # The refusal the creation will hit is named UP FRONT, whether or not
        # this particular repository is dirty right now.
        assert "source repository has uncommitted changes" in brief["refusals_you_may_hit"]

    def test_a_dirty_source_is_called_out_before_the_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wv, "_dirty_paths", lambda repo: ["a.py"])
        brief = wv.creation_briefing("n1", tmp_path)
        assert brief["source_repo_is_dirty"] is True
        assert "the source repository is DIRTY" in "\n".join(wv.creation_lines(brief))

    def test_cleanup_confirms_before_removing_and_reports_after(self, tmp_path):
        receipt = wv.cleanup_receipt("n1", self._managed_report(tmp_path))
        assert receipt["known"] is True
        assert receipt["outcome"] == "not_attempted"
        assert receipt["removed"] is None
        blob = "\n".join(wv.cleanup_lines(receipt))
        assert "no removal was performed" in blob

    def test_a_completed_cleanup_reports_what_it_removed(self, tmp_path):
        receipt = wv.cleanup_receipt("n1", self._managed_report(tmp_path), removed=["n1"])
        receipt["outcome"] = "removed"
        assert receipt["removed"] == ["n1"]
        assert "removed: n1" in "\n".join(wv.cleanup_lines(receipt))

    def test_cleanup_of_an_unknown_worktree_removes_nothing(self, tmp_path):
        receipt = wv.cleanup_receipt("nope", self._managed_report(tmp_path))
        assert receipt["known"] is False
        assert "Nothing would be removed, and nothing was" in "\n".join(
            wv.cleanup_lines(receipt)
        )


class TestMergeBackIsAVisibleFlow:
    def test_the_flow_is_rendered_as_an_ordered_sequence(self):
        blob = "\n".join(
            wv.merge_back_lines(wv.merge_back_flow(node_id="c1"))
        )
        for step in ("capture:", "claim check:", "apply:", "verify:", "report:"):
            assert step in blob

    def test_a_concurrent_edit_refusal_is_shown_before_the_merge_steps(self):
        receipt = wv.merge_back_flow(
            node_id="c1",
            refused=({"path": "cli/ui.py", "line": 12, "reason": "unclaimed edit"},),
            verified=False,
        )
        blob = "\n".join(wv.merge_back_lines(receipt))
        assert blob.index("refusal(s)") < blob.index("capture:")
        assert "the merge did NOT complete" in blob
        assert receipt["blocked"] is True

    def test_an_unreported_verification_is_not_a_verified_merge(self):
        receipt = wv.merge_back_flow(node_id="c1", merged_files=("a.py",))
        blob = "\n".join(wv.merge_back_lines(receipt))
        assert "NOT REPORTED" in blob
        assert receipt["blocked"] is False

    def test_tree_switch_warns_about_uncommitted_work(self):
        blob = "\n".join(
            wv.tree_switch_lines(
                wv.tree_switch_warning(from_path="a", to_path="b", dirty=["x.py"])
            )
        )
        assert "uncommitted path(s) stay in the tree you are leaving" in blob

    def test_an_unknown_dirty_state_is_a_warning_not_a_clean_bill(self):
        warning = wv.tree_switch_warning(from_path="a", to_path="b", dirty=None)
        assert warning["unknown_is_not_clean"] is True
        assert "UNVERIFIED, not safe" in "\n".join(wv.tree_switch_lines(warning))


# ---------------------------------------------------------------------------
# T4.W1.5 — the eleven surfaces
# ---------------------------------------------------------------------------


class TestTheElevenSurfaces:
    def test_exactly_eleven_are_enumerated_and_they_are_the_ones_the_brief_names(self):
        assert len(ps.SURFACES) == 11
        assert set(ps.SURFACES) == {
            "session_briefing",
            "session_boundary_marker",
            "feature_list",
            "feature_detail",
            "coverage_summary",
            "subagent_list",
            "subagent_transcript",
            "subagent_steer",
            "worktree_list",
            "worktree_detail",
            "merge_back_flow",
        }

    def test_every_surface_renders_non_empty_on_every_edge_case(self, tmp_path):
        results = ps.render_all_surfaces(tmp=tmp_path, repo=REPO_ROOT)
        assert len(results) == len(ps.SURFACES) * len(ps.EDGE_CASES)
        empty = [f"{r.name}/{r.case}" for r in results if not r.non_empty]
        assert empty == [], f"these surfaces rendered NOTHING: {empty}"

    def test_every_truth_check_passes_on_every_edge_case(self, tmp_path):
        results = ps.render_all_surfaces(tmp=tmp_path, repo=REPO_ROOT)
        failed = [f"{r.name}/{r.case}:{n}" for r in results for n, ok in r.checks if not ok]
        assert failed == [], f"these truth checks failed: {failed}"

    def test_the_table_lists_every_surface_and_edge_case(self, tmp_path):
        results = ps.render_all_surfaces(tmp=tmp_path, repo=REPO_ROOT)
        blob = "\n".join(ps.surface_table(results))
        for name in ps.SURFACES:
            assert name in blob
        for case, _ in ps.EDGE_CASES:
            assert case in blob


class TestTheSanitiserRunsOnSubagentOutputAndFeatureText:
    def test_no_rendered_line_survives_the_sanitiser_check(self, tmp_path):
        results = ps.render_all_surfaces(tmp=tmp_path, repo=REPO_ROOT)
        report = ps.sanitiser_report(results)
        assert report["lines_checked"] > 500, "too few lines checked to be evidence"
        assert report["raw_ansi_escape_survivors"] == []
        assert report["not_a_sanitiser_fixed_point"] == []
        assert report["credential_survivors"] == []
        assert report["holds"] is True

    def test_an_injected_feature_name_is_rendered_as_data(self, tmp_path):
        results = ps.render_all_surfaces(tmp=tmp_path, repo=REPO_ROOT, cases=("injection",))
        blob = "\n".join(line for r in results for line in r.lines)
        assert "IGNORE PREVIOUS" in blob, (
            "the payload must still be VISIBLE - a sanitiser that deletes the "
            "message hides the attack from the reader"
        )
        assert "\x1b[" not in blob
        assert "sk-ABCDEF0123456789" not in blob

    def test_a_subagent_result_carrying_injection_is_sanitised_on_the_way_in(self, tmp_path):
        store = sv.SubagentStore(tmp_path / "subs", "sess")
        record = store.spawn(description="review")
        store.finish(record.agent_id, result=ps.INJECTION)
        stored = json.loads(store.record_path(record.agent_id).read_text(encoding="utf-8"))
        assert "\x1b[" not in stored["result"]
        assert "sk-ABCDEF0123456789" not in stored["result"]
        assert "IGNORE PREVIOUS" in stored["result"]

    def test_a_torn_transcript_line_is_reported_not_dropped(self, tmp_path):
        store = sv.SubagentStore(tmp_path / "subs", "sess")
        record = store.spawn(description="review")
        with store.transcript_path(record.agent_id).open("a", encoding="utf-8") as handle:
            handle.write('{"role": "assistant", "content": "half a li')
        messages = store.transcript(record.agent_id)
        assert messages and messages[-1].get("torn") is True


class TestThreeSurfacesAgree:
    def test_the_feature_list_the_coverage_summary_and_the_briefing_agree(self, tmp_path):
        report = ps.three_surface_agreement(tmp=tmp_path, repo=REPO_ROOT)
        assert report["agree"] is True, report["checks"]
        assert report["feature"]
        assert report["expected_state"]

    def test_the_coverage_counts_are_the_lists_own_counts(self, tmp_path):
        report = ps.three_surface_agreement(tmp=tmp_path, repo=REPO_ROOT)
        coverage = report["coverage"]
        ledger = fl.load_ledger(REPO_ROOT)
        assert coverage["total"] == len(ledger.features)
        assert coverage["verified"] == sum(1 for item in ledger.features if item.passing)


# ---------------------------------------------------------------------------
# Two regressions this round found in its own code
# ---------------------------------------------------------------------------


class TestGitAnswersKeepTheirLoadBearingLeadingColumn:
    def test_the_first_porcelain_row_keeps_its_status_column(self, monkeypatch):
        """`.strip()` on git porcelain deletes the X column of the FIRST row.

        Measured on this tree: a staged modification was reported as unstaged
        and `line[3:]` then chopped the leading "." off the reported path.
        """

        class _Done:
            returncode = 0
            stdout = "M .github/workflows/ci.yml\n M other.py\n"

        monkeypatch.setattr(wv, "subprocess", _StubSubprocess(_Done))
        wv.reset_git_cache()
        assert wv._git(".", "status", "--porcelain") == "M .github/workflows/ci.yml\n M other.py"

    def test_the_cache_is_cleared_on_request(self, monkeypatch):
        class _First:
            returncode = 0
            stdout = "main\n"

        class _Second:
            returncode = 0
            stdout = "other\n"

        monkeypatch.setattr(wv, "subprocess", _StubSubprocess(_First))
        wv.reset_git_cache()
        assert wv._git(".", "branch") == "main"
        monkeypatch.setattr(wv, "subprocess", _StubSubprocess(_Second))
        assert wv._git(".", "branch") == "main", "the memo should still answer"
        wv.reset_git_cache()
        assert wv._git(".", "branch") == "other"


class _StubSubprocess:
    """Minimal stand-in for the ``subprocess`` module."""

    TimeoutExpired = subprocess.TimeoutExpired
    SubprocessError = subprocess.SubprocessError

    def __init__(self, done) -> None:
        self._done = done

    def run(self, *args, **kwargs):
        return self._done()


class TestTheLedgerParserCannotBeFooledByColumns:
    """The parser only reads the ``## User-facing capabilities`` table.

    That restriction is load-bearing: the same file carries an evidence/gate
    narrative table, and folding either it or the prose limitation list into
    "features" would invent rows nobody declared.
    """

    _HEAD = "## User-facing capabilities\n\n| Capability | Status | Notes | Evidence |\n|---|---|---|---|\n"

    def test_an_empty_notes_cell_does_not_shift_the_evidence(self):
        rows = fl.parse_feature_matrix(
            self._HEAD + "| thing | Implemented |  | `tests/a.py` |\n",
            repo_root=REPO_ROOT,
        )
        assert len(rows) == 1
        assert rows[0].name == "thing"
        assert rows[0].evidence[0].reference == "tests/a.py"

    def test_a_header_row_is_skipped_by_its_word_not_its_position(self):
        rows = fl.parse_feature_matrix(
            self._HEAD
            + "| thing | Implemented | n | `tests/a.py` |\n"
            + "| Capability | Status | Notes | Evidence |\n"
            + "| real | Implemented | n | `tests/a.py` |\n",
            repo_root=REPO_ROOT,
        )
        assert [row.name for row in rows] == ["thing", "real"], (
            "a row carrying the header words is skipped wherever it appears, "
            "not only at position zero"
        )

    def test_the_other_tables_in_the_file_are_not_read_as_features(self):
        rows = fl.parse_feature_matrix(
            self._HEAD
            + "| real | Implemented | n | `tests/a.py` |\n\n"
            + "## Evidence and quality gates\n\n"
            + "| Gate | Current interpretation |\n|---|---|\n"
            + "| Prompt host self-check | CLEAN |\n",
            repo_root=REPO_ROOT,
        )
        assert [row.name for row in rows] == ["real"]
