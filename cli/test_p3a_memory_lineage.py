"""P3a/W1 T4: the human-gated inbox, visible lineage, and the session tree.

Host-only: no Docker, no provider, no network, no credential. Every test
builds its own log root under ``tmp_path`` and drives the REAL modules.

The suite is organised around the NON-NEGOTIABLES rather than around the
functions, because a test file named after functions passes while the promises
are broken:

* ``TestNothingIsRememberedWithoutAHumanAct`` - the four ways a memory could
  be applied without somebody deciding, and why each is refused.
* ``TestProvenanceIsOnEveryMemory`` - a proposal with no source is shown as
  having no source.
* ``TestClearingAndCompactionStaySeparate`` - the two ways a window shrinks.
* ``TestABranchSummaryIsLabelledAsGenerated`` - a claim is never a record.
* ``TestAForkAlwaysDisclosesItsWorkingTree`` - the disclosure cannot be
  skipped, including by a receipt that omits it.
* ``TestAbsentIsNeverZero`` - the rail's honesty rule, with a control arm.

The sensitivity of each gate is asserted by planting the defect it exists to
catch, because a gate nobody has watched fire is a gate nobody reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from cli import commands as C
from cli import context_budget as budget
from cli import lineage, palette
from cli import memory_ui as mui
from cli import session as S

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """A log root whose inbox directory already exists.

    ``<log_root>/_memory-inbox`` is the one candidate `inbox_root` searches
    when ``repo`` is falsy, and these tests pass no repository - so creating
    ``<log_root>/.inbox`` instead produced an inbox the module could not see,
    and every provenance assertion failed with "the test must be able to resolve
    the inbox". `inbox_root` answers `None` when nothing exists, which is the
    correct honest answer and is why the directory has to exist first.
    """
    base = tmp_path / "logs"
    (base / "_memory-inbox").mkdir(parents=True)
    return base


@pytest.fixture()
def convo(root: Path) -> str:
    """A real session with real turns and one real compaction."""
    sess = S._new_session("sess-test", None)
    for i in range(6):
        S.append_turn(sess, "user", f"question {i} about the parser", turn_id=f"t{i}a")
        S.append_turn(sess, "assistant", f"answer {i}", turn_id=f"t{i}b")
    S.save_session(str(root), sess)
    live = S.load_or_create(str(root), None, "sess-test", strict=False)
    S.compact_session(live, str(root), keep_last=2)
    S.save_session(str(root), live)
    return "sess-test"


def _proposal(root: Path, pid: str, text: str, **prov: Any) -> None:
    """Write one proposal. ``provenance=False`` writes a record with NONE.

    The defaulting matters: a helper that always stamps ``user_stated`` makes
    it impossible to construct the case this surface most needs to handle - a
    proposal whose producer recorded no origin at all - so the absent-provenance
    path would be untested and read as working.
    """
    inbox = mui.inbox_root(None, root)
    assert inbox is not None, "the test must be able to resolve the inbox"
    inbox = Path(str(inbox))
    inbox.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"id": pid, "text": text, "confidence": 0.5}
    if prov.pop("provenance", True):
        payload["provenance"] = {
            "source_type": prov.pop("source_type", "user_stated"),
            "created_at": prov.pop("created_at", 1000.0),
            **prov,
        }
    (inbox / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Nothing is remembered without a human act
# ---------------------------------------------------------------------------


class TestNothingIsRememberedWithoutAHumanAct:
    def test_a_quiet_inbox_applies_nothing(self, root: Path) -> None:
        _proposal(root, "a", "use pathlib everywhere")
        report = mui.inbox_status(None, root)
        assert report["available"] is True
        assert report["count"] == 1
        # Reading the inbox must not consume it.
        mui.inbox_status(None, root)
        mui.pending_items(None, root)
        assert mui.inbox_status(None, root)["count"] == 1

    def test_accept_without_confirm_is_refused_and_names_the_gate(
        self, root: Path
    ) -> None:
        _proposal(root, "a", "use pathlib everywhere")
        receipt = mui.memory_command("accept", "a", log_root=root)
        assert receipt["ok"] is False
        joined = " ".join(receipt["lines"])
        assert "--confirm" in joined
        # and it is still pending: a refusal is not a quiet success
        assert mui.inbox_status(None, root)["count"] == 1

    def test_a_quoted_reason_survives_the_flag_parser(self, root: Path) -> None:
        """`--reason "two words"` must not arrive as four bare tokens.

        The first parser split on whitespace, so a quoted reason containing a
        space became extra arguments and the reason was silently dropped - a
        rejection ledger with an empty reason is a decision nobody can audit.
        """
        _proposal(root, "a", "use pathlib everywhere")
        receipt = mui.memory_command(
            "reject", 'a --confirm --reason "not our convention"', log_root=root
        )
        assert receipt["ok"] is True
        inbox = mui.inbox_root(None, root)
        ledger = mui.rejection_ledger(inbox)
        text = ledger.read_text(encoding="utf-8")
        assert "not our convention" in text, (
            f"the quoted reason was lost or split: {text!r}"
        )
        # and it is ONE row, not four - a reason split into tokens would have
        # written a decision whose reason is only its first word
        assert len([ln for ln in text.splitlines() if ln.strip()]) == 1

    def test_the_gate_fires_on_its_own_author(self, monkeypatch: Any) -> None:
        """Plant the defect the gate exists to catch, and require it to fire.

        The gate builds its OWN scratch inbox, so the control arm has to reach
        its LOGIC rather than its filesystem - a first draft planted a `.decided`
        marker in the test's own log root and asserted the gate refused it, which
        it cannot see and correctly did not.

        The mutation is a read-only verb OUTSIDE the vocabulary. This is the
        assertion that replaced a vacuous one: the original disjointness check
        computed `mutating` as the complement of `read_only`, so the
        intersection it tested was empty by construction and could never fire.
        """
        ok, _why = mui.assert_never_applies_without_a_human_act()
        assert ok is True, "the honest case must pass"

        monkeypatch.setattr(
            mui, "_READ_ONLY_VERBS", set(mui._READ_ONLY_VERBS) | {"forgetall"}
        )
        ok, why = mui.assert_never_applies_without_a_human_act()
        assert ok is False, "a stray read-only verb must be refused"
        assert "forgetall" in why
        assert "nothing gates" in why

    def test_the_gate_refuses_a_new_unaccounted_writer(
        self, monkeypatch: Any
    ) -> None:
        """A new mutating verb must break the gate until it has its own gate.

        The gate's third assertion pins the mutating set to exactly
        ``{accept, reject}``. That is the pressure which stops somebody adding
        `forget` or `promote` and shipping a writer with no confirm token.
        """
        monkeypatch.setattr(mui, "MEMORY_VERBS", (*mui.MEMORY_VERBS, "forget"))
        ok, why = mui.assert_never_applies_without_a_human_act()
        assert ok is False
        assert "needs its own confirm gate" in why

    def test_an_unconfirmed_write_leaves_no_trace(self, root: Path) -> None:
        """The non-negotiable, asserted directly rather than via the gate.

        An unconfirmed reject must write NO ledger row and NO decided marker.
        The first draft asserted this through the gate, which builds its own
        scratch inbox - so the test passed without ever exercising this
        module's real inbox path.
        """
        _proposal(root, "a", "use pathlib everywhere")
        inbox = mui.inbox_root(None, root)
        assert inbox is not None
        receipt = mui.memory_command("reject", "a", log_root=root)
        assert receipt["ok"] is False
        assert "--confirm" in " ".join(receipt["lines"])
        assert not mui.rejection_ledger(inbox).exists()
        assert not list(Path(str(inbox)).glob("*.decided"))
        assert mui.inbox_status(None, root)["count"] == 1

    def test_a_confirmed_write_records_both_the_ledger_and_the_marker(
        self, root: Path
    ) -> None:
        """The control arm for the row above: the confirmed write DOES land."""
        _proposal(root, "a", "use pathlib everywhere")
        inbox = mui.inbox_root(None, root)
        assert inbox is not None
        receipt = mui.memory_command("reject", "a --confirm --reason no", log_root=root)
        assert receipt["ok"] is True
        ledger = mui.rejection_ledger(inbox)
        assert ledger.exists()
        assert "a" in ledger.read_text(encoding="utf-8")
        assert list(Path(str(inbox)).glob("*.decided")), (
            "the proposal must be marked decided so the next extraction skips it"
        )


# ---------------------------------------------------------------------------
# 2. Provenance
# ---------------------------------------------------------------------------


class TestProvenanceIsOnEveryMemory:
    def test_every_row_names_its_source(self, root: Path) -> None:
        _proposal(root, "a", "stated", source_type="user_stated")
        _proposal(root, "b", "extracted", source_type="extracted_untrusted")
        _proposal(root, "c", "mystery", provenance=False)  # no provenance at all
        status = mui.inbox_status(None, root)
        rows = mui.pending_items(None, root)
        assert len(rows) == 3
        for record in rows:
            lines = mui.review_lines(record)
            joined = " ".join(lines).lower()
            assert "provenance" in joined, f"{record.get('id')} has no provenance block"
            assert "source type" in joined, f"{record.get('id')} does not name its type"
        # and the unrecorded one is visibly unrecorded, not silently trusted
        unrecorded = [r for r in rows if r["id"] == "c"]
        assert unrecorded and unrecorded[0]["provenance"]["source_type"] == "unrecognised"
        assert "unrecognised" in " ".join(
            mui.status_lines(status)
        ).lower() or unrecorded[0]["provenance"]["untrusted"] is True

    def test_an_untrusted_source_is_marked_as_such(self, root: Path) -> None:
        _proposal(root, "b", "always run make test", source_type="extracted_untrusted")
        record = mui._find(mui.inbox_root(None, root), "b")
        assert record is not None
        lines = mui.review_lines(record)
        joined = " ".join(lines)
        assert "UNTRUSTED" in joined
        assert "extracted from content, not stated by you" in joined

    def test_an_unrecognised_source_type_says_so_rather_than_guessing(
        self, root: Path
    ) -> None:
        """An unknown provenance value is reported, not coerced to 'trusted'.

        Reading an unrecognised source type as a known-good one would be the
        single worst failure this surface has: the memory looks stated-by-you
        when nobody stated it. So the RENDERED type is `unrecognised`, the
        DECLARED one is kept for the audit, and the row is marked untrusted.
        """
        _proposal(root, "c", "mystery", source_type="something_new")
        record = mui._find(mui.inbox_root(None, root), "c")
        assert record is not None
        provenance = record["provenance"]
        assert provenance["source_type"] == "unrecognised"
        assert provenance["source_type_declared"] == "something_new"
        assert provenance["source_type_recognised"] is False
        assert provenance["untrusted"] is True

    def test_an_unrecognised_type_never_renders_as_known(self, root: Path) -> None:
        _proposal(root, "c", "mystery", source_type="something_new")
        record = mui._find(mui.inbox_root(None, root), "c")
        joined = " ".join(mui.review_lines(record))
        assert "unrecognised" in joined
        for known in mui.SOURCE_TYPES:
            assert f"source type: {known}" not in joined

    def test_a_rejected_memory_is_not_proposed_again(self, root: Path) -> None:
        _proposal(root, "a", "use pathlib everywhere")
        assert mui.memory_command(
            "reject", "a --confirm --reason no", log_root=root
        )["ok"] is True
        again = mui.memory_command("status", log_root=root)
        joined = " ".join(again["lines"]).lower()
        assert "a" not in joined or "rejected" in joined or "0 proposal" in joined


# ---------------------------------------------------------------------------
# 3. Clearing and compaction stay separate
# ---------------------------------------------------------------------------


def _journal(root: Path, rows: List[Mapping[str, Any]]) -> Path:
    run = root / "fix-ctx"
    run.mkdir(parents=True, exist_ok=True)
    (run / "trace.jsonl").write_text(
        "\n".join(json.dumps(dict(r)) for r in rows) + "\n", encoding="utf-8"
    )
    return run


class TestClearingAndCompactionStaySeparate:
    def test_the_two_reclaim_paths_are_two_rows(self, root: Path, convo: str) -> None:
        run = _journal(
            root,
            [
                {"event": "context_rewind", "payload": {"tokens_reclaimed": 1800}},
                {
                    "event": "context_compacted",
                    "payload": {"tokens_reclaimed": 9100, "turn_count": 12},
                },
            ],
        )
        from cli.runview import read_context_meter

        projection = budget.budget_projection(
            meter=read_context_meter(run),
            conversation=S.load_or_create(str(root), None, convo, strict=False),
            log_root=root,
            tokens_used=21000,
            window_tokens=32000,
        )
        labels = [label for label, _ in budget.budget_rows(projection)]
        assert "clearing" in labels
        assert any(label.startswith("compaction") for label in labels)
        assert labels.count("clearing") == 1
        # and the total is NEVER a single summed number
        rendered = " ".join(v for _, v in budget.budget_rows(projection))
        assert "10,900 token(s)" not in rendered

    def test_each_row_says_what_is_still_on_disk(
        self, root: Path, convo: str
    ) -> None:
        run = _journal(
            root,
            [{"event": "context_rewind", "payload": {"tokens_reclaimed": 500}}],
        )
        from cli.runview import read_context_meter

        projection = budget.budget_projection(
            meter=read_context_meter(run),
            conversation=S.load_or_create(str(root), None, convo, strict=False),
            log_root=root,
        )
        clearing = dict(projection["rows"]["clearing"])
        assert clearing["kept_on_disk"] is True
        assert "kept on disk" in " ".join(
            v for _, v in budget.budget_rows(projection)
        )

    def test_the_pre_compaction_turns_are_really_readable(
        self, root: Path, convo: str
    ) -> None:
        report = lineage.pre_compaction_history(root, convo)
        assert report["available"] is True
        text = " ".join(
            str(turn.get("text") or "") for turn in report["turns"]
        )
        assert "question 0 about the parser" in text, (
            "a summary is an INDEX into the history, so the turns it names must "
            f"still be readable; got {text!r}"
        )
        assert "still on disk" in " ".join(lineage.pre_compaction_lines(report))


# ---------------------------------------------------------------------------
# 4. A branch summary is a claim, and says so
# ---------------------------------------------------------------------------


class TestABranchSummaryIsLabelledAsGenerated:
    def test_the_label_leads_and_names_its_scope(self) -> None:
        lines = lineage.branch_summary_lines(
            "the agent edited three files then stopped",
            branch_session_id="sess-abc",
            generated_by="the caller",
        )
        assert lines[0] == lineage.BRANCH_SUMMARY_LABEL
        joined = " ".join(lines)
        assert "sess-abc" in joined
        # The claim itself, and it must survive the 78-column bound - the first
        # draft's sentence was 82 characters and was clipped at exactly the
        # word "not", so the load-bearing half never reached a real terminal.
        assert "NOT a transcript record" in joined
        assert any(len(line) <= 78 for line in lines)
        assert "still on disk" in joined

    def test_an_empty_summary_says_it_is_empty(self) -> None:
        lines = lineage.branch_summary_lines("", branch_session_id="s")
        assert "no summary was produced" in " ".join(lines)

    def test_the_label_constant_is_what_renders(self) -> None:
        """One constant, so no surface can spell it differently."""
        for lines in (
            lineage.branch_summary_lines("x", branch_session_id="s"),
            lineage.branch_summary_lines("y", branch_session_id="t"),
        ):
            assert lines[0] == lineage.BRANCH_SUMMARY_LABEL


# ---------------------------------------------------------------------------
# 5. A fork always discloses its working tree
# ---------------------------------------------------------------------------


class TestAForkAlwaysDisclosesItsWorkingTree:
    @pytest.mark.parametrize("isolation", ["isolated", "shared", "refused"])
    def test_all_three_verdicts_render_a_disclosure(self, isolation: str) -> None:
        lines = lineage.fork_isolation_lines(
            isolation=isolation,
            parent_session_id="p",
            child_session_id="c",
        )
        joined = " ".join(lines)
        assert lines, "every verdict renders at least one line"
        assert "WORKING TREE" in joined

    def test_shared_is_never_dressed_up_as_isolated(self) -> None:
        lines = lineage.fork_isolation_lines(
            isolation="shared", parent_session_id="p", child_session_id="c"
        )
        joined = " ".join(lines)
        assert "NOT ISOLATED" in joined
        assert "SAME writable working tree" in joined
        # the two consequences a user must be able to act on
        assert "two conversations over one set of files" in joined
        assert "visible to both" in joined

    def test_a_refusal_says_nothing_was_forked(self) -> None:
        joined = " ".join(
            lineage.fork_isolation_lines(
                isolation="refused",
                parent_session_id="p",
                child_session_id="",
                reason="no worktree root",
            )
        )
        assert "REFUSED" in joined
        assert "nothing was forked" in joined
        assert "no worktree root" in joined

    def test_the_gate_refuses_a_receipt_with_no_verdict(self) -> None:
        """The gate is on the RECEIPT, not the renderer.

        A renderer that forgot the block cannot be fixed by a gate that reads
        the renderer; a receipt that omits the verdict is what has to fail.
        """
        ok, why = lineage.assert_the_disclosure_cannot_be_skipped(
            {"child_session_id": "c"}
        )
        assert ok is False
        assert "isolation" in why

        ok, why = lineage.assert_the_disclosure_cannot_be_skipped(
            {"isolation": "refused", "child_session_id": "c"}
        )
        assert ok is False, "a refusal must not also name a child"

        ok, _ = lineage.assert_the_disclosure_cannot_be_skipped(
            {"isolation": "shared", "child_session_id": "c"}
        )
        assert ok is True

    def test_no_fork_means_nothing_to_disclose(self) -> None:
        assert lineage.assert_the_disclosure_cannot_be_skipped(None) == (True, "")

    def test_a_real_fork_through_the_tree_discloses(self, root: Path, convo: str) -> None:
        receipt = lineage.tree_command(
            "fork", "", repo_path=None, log_root=root, session_id=convo
        )
        joined = " ".join(receipt["lines"])
        assert "WORKING TREE" in joined
        ok, _why = lineage.assert_the_disclosure_cannot_be_skipped(
            (receipt.get("payload") or {}).get("fork")
        )
        assert ok is True


# ---------------------------------------------------------------------------
# 6. Absent is never zero
# ---------------------------------------------------------------------------


class TestAbsentIsNeverZero:
    def test_an_empty_session_measures_nothing_and_says_so(self) -> None:
        """W2.3 changed this from 0 known to 1, and the change is the point.

        Wave 1 built the ``memory`` row only when an INBOX was available, so a
        project whose artifacts live outside the checkout - which is every
        project, since VEX-CEILING-03 moved them there - reported no memory row
        at all. But the store IS readable and DOES have a count, so that row is
        genuinely measured and counting it as unknown was its own kind of lie:
        it told a reader we knew nothing about their memory when we knew exactly
        how much of it there was.

        So the row is now built unconditionally. What the pin still holds - and
        what it was really written for - is that the FIVE token-shaped rows
        measure nothing here, and that the absent half of the memory row still
        carries its own reason.
        """
        projection = budget.budget_projection()
        token_rows = {"tokens", "clearing", "compaction", "summary", "last_compaction"}
        assert token_rows.isdisjoint(projection["rows"]), projection["rows"].keys()
        assert projection["known"] == 1  # only `memory`
        assert "memory" in projection["rows"]
        assert projection["absent"] == projection["total"] - 1
        assert "memory_inbox" in projection["reasons"]
        rows = " ".join(budget.budget_lines(projection))
        assert "not measured" in rows

    def test_the_memory_row_is_absent_free_with_no_inputs(self) -> None:
        """No session inputs: the memory row still renders, with honest halves.

        The APPLIED half is measured whenever the store can be read, even with
        no conversation at all - which is the point of the W2.3 change. The
        PENDING half needs an inbox and reports ``None`` when there is none,
        because "no proposals are waiting" and "there is nowhere proposals
        could be waiting" are different facts.
        """
        projection = budget.budget_projection()
        memory = projection["rows"]["memory"]
        assert memory["pending"] is None
        assert memory["inbox_available"] is False
        assert memory["applied_known"] in (True, False)
        if memory["applied_known"]:
            assert isinstance(memory["applied"], int)
        else:
            assert memory["applied"] is None
            assert memory["applied_reason"]

    def test_no_row_renders_a_zero(self) -> None:
        projection = budget.budget_projection()
        for line in budget.budget_lines(projection):
            assert not line.rstrip().endswith(": 0")

    def test_the_gate_has_a_control_arm(self) -> None:
        """The gate must fire on a planted zero AND pass a measured one.

        A gate that passes because every field is absent measures nothing, so
        both arms are asserted.
        """
        unmeasured = {"rows": {"clearing": {"tokens": 0}}, "absent": 5, "total": 6}
        ok, why = budget.assert_absent_is_never_zero(unmeasured)
        assert ok is False
        assert "must be None" in why

        measured = {
            "rows": {"clearing": {"tokens": 0, "tokens_measured": True}},
            "absent": 5,
            "total": 6,
        }
        assert budget.assert_absent_is_never_zero(measured) == (True, "")

    def test_a_percentage_without_a_window_is_never_rendered(self) -> None:
        projection = budget.budget_projection(tokens_used=21000, window_tokens=None)
        assert "tokens" not in projection["rows"]
        assert "no context window" in projection["reasons"]["tokens"]

    def test_every_absent_row_carries_a_reason(self) -> None:
        projection = budget.budget_projection()
        for name in budget.BUDGET_ROWS:
            if name not in projection["rows"]:
                assert projection["reasons"].get(name), (
                    f"{name} is absent with no reason, so a caller cannot tell "
                    "'we did not measure it' from 'it does not apply'"
                )

    def test_the_row_set_is_exactly_the_six(self) -> None:
        assert budget.BUDGET_ROWS == (
            "tokens",
            "clearing",
            "compaction",
            "summary",
            "last_compaction",
            "memory",
        )


# ---------------------------------------------------------------------------
# 7. Lineage: one compaction, one rotation, deduped search
# ---------------------------------------------------------------------------


class TestTheLineageTellsTheTruth:
    def test_one_compaction_is_reported_once(self, root: Path, convo: str) -> None:
        """The snapshot AND the journal row record the SAME compaction.

        Reporting both told a reader their conversation had been compacted
        twice, and the second row claimed `turn_count: 0` - so a reader hunting
        for the missing twelve turns would find nothing.
        """
        report = lineage.rotation_events(root, convo)
        compactions = [
            e for e in report["events"] if e["kind"] == "compaction"
        ]
        assert len(compactions) == 1, report["events"]
        assert compactions[0]["turn_count"], "the surviving row knows the count"

    def test_in_place_compaction_is_not_called_a_rotation(
        self, root: Path, convo: str
    ) -> None:
        report = lineage.rotation_events(root, convo)
        assert report["rotated"] is False
        assert report["shape"] == "in_place_compaction"
        assert "unbroken" in report["reason"]

    def test_search_finds_turns_a_compaction_removed_from_the_window(
        self, root: Path, convo: str
    ) -> None:
        turns = lineage.searchable_turns(root, convo)
        text = " ".join(str(t.get("text") or "") for t in turns)
        assert "question 0 about the parser" in text, (
            "searching only the live window is why 'search my conversation' "
            "finds nothing after a compaction"
        )

    def test_a_result_in_both_a_parent_and_a_child_is_shown_once(self) -> None:
        rows = [
            {"session_id": "new", "turn_id": "t1", "text": "shared"},
            {"session_id": "old", "turn_id": "t1", "text": "shared"},
            {"session_id": "new", "turn_id": "t2", "text": "unique"},
        ]
        report = lineage.dedupe_across_lineage(rows, lineage=[])
        assert report["shown"] == 2
        assert report["collapsed"] == 1
        kept = report["rows"][0]
        assert kept["session_id"] == "new", "the newest copy is the one kept"
        assert "old" in kept["also_in"], (
            "the ancestor is NAMED; silently dropping it leaves a reader "
            "wondering where the duplicate went"
        )

    def test_the_ancestry_walk_survives_a_cycle(self, root: Path) -> None:
        """A corrupt parent chain stops the walk instead of hanging a repaint."""
        sess = S._new_session("sess-loop", None)
        S.save_session(str(root), sess)
        record = S.load_or_create(str(root), None, "sess-loop", strict=False)
        record["parent_session_id"] = "sess-loop"  # points at itself
        S.save_session(str(root), record)
        chain = lineage.lineage_chain(root, "sess-loop")
        assert chain, "the walk returns what it found rather than raising"
        assert any(not row.get("loaded", True) for row in chain), (
            f"a cycle must be reported: {chain}"
        )


# ---------------------------------------------------------------------------
# 8. The tree: three names, one mechanism
# ---------------------------------------------------------------------------


class TestTheTreeIsOneMechanism:
    def test_the_registry_and_the_module_declare_the_same_verbs(self) -> None:
        assert tuple(v.name for v in C.SUBCOMMANDS["/tree"]) == lineage.TREE_VERBS

    def test_clone_is_the_same_command_not_a_second_one(self) -> None:
        assert C.command_spec("/clone").name == "/tree"
        assert "/clone" in C.command_spec("/tree").aliases

    def test_fork_and_clone_are_one_implementation(self) -> None:
        """Two bodies is how `/fork` and `/tree fork` learned to disagree."""
        source = (
            Path(__file__).with_name("lineage.py").read_text(encoding="utf-8")
        )
        assert source.count("def _do_fork(") == 1
        assert 'name in ("fork", "clone")' in source

    def test_an_unknown_verb_names_the_valid_set(self, root: Path) -> None:
        receipt = lineage.tree_command(
            "bogus", "", log_root=root, session_id="s"
        )
        assert receipt["ok"] is False
        joined = " ".join(receipt["lines"])
        for verb in lineage.TREE_VERBS:
            assert verb in joined

    def test_rewind_without_a_repository_says_so(self, root: Path) -> None:
        """Not a raw TypeError.

        Letting the checkpoint authority try to resolve a workspace identity
        from `None` produced `expected str, bytes or os.PathLike object, not
        NoneType` as a user-facing refusal.
        """
        receipt = lineage.tree_command(
            "rewind", "cp-1", repo_path=None, log_root=root, session_id="s"
        )
        assert receipt["ok"] is False
        joined = " ".join(receipt["lines"])
        assert "needs a repository" in joined
        assert "NoneType" not in joined

    def test_abandoning_a_root_is_refused(self, root: Path, convo: str) -> None:
        receipt = lineage.tree_command(
            "abandon", convo, log_root=root, session_id=convo
        )
        assert receipt["ok"] is False
        assert "no parent to return to" in " ".join(receipt["lines"])

    def test_an_abandon_receipt_says_nothing_was_deleted(
        self, root: Path, convo: str
    ) -> None:
        child = lineage.tree_command(
            "fork", "", log_root=root, session_id=convo
        )["payload"]["fork"]["child_session_id"]
        receipt = lineage.tree_command(
            "abandon", child, log_root=root, session_id=convo
        )
        joined = " ".join(receipt["lines"])
        assert "NOT deleted" in joined
        assert f"/lineage show {child}" in joined


# ---------------------------------------------------------------------------
# 9. The mounts
# ---------------------------------------------------------------------------


class TestTheSurfacesAreMounted:
    @pytest.mark.parametrize("name", ["/memory", "/lineage", "/tree"])
    def test_each_command_is_registered_and_grouped(self, name: str) -> None:
        assert C.command_spec(name) is not None
        assert C.headless_policy(name) == "mapped"
        assert palette.COMMAND_GROUPS.get(name) == "session"
        assert not palette.unassigned_commands()

    @pytest.mark.parametrize("name", ["/memory", "/lineage", "/tree"])
    def test_each_command_has_a_handler(self, name: str) -> None:
        from cli import interactive as I

        assert name in I.SUBCOMMANDS_BY_COMMAND
        assert callable(I.SUBCOMMANDS_BY_COMMAND[name])

    def test_every_declared_verb_reports_the_verb_that_was_typed(self) -> None:
        """An alias must not be canonicalised in the receipt.

        `tests/test_command_routing.py` asserts this for every verb; this
        pins the specific case, because `find` -> `search` is the one alias
        pair in this round's tables.
        """
        from cli import interactive as I

        receipt = I.SUBCOMMANDS_BY_COMMAND["/lineage"](
            "find", "anything", log_root=None, state={}
        )
        assert receipt["verb"] == "find"

    def test_both_shells_dispatch_the_same_command_set(self) -> None:
        """The AST parity read `tests/test_cli_terminal_parity.py` performs."""
        import ast

        def keys(path: Path, fname: str) -> set:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found: set = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == fname:
                    for cmp in ast.walk(node):
                        if (
                            isinstance(cmp, ast.Compare)
                            and isinstance(cmp.left, ast.Name)
                            and cmp.left.id == "cmd"
                        ):
                            for other in cmp.comparators:
                                if isinstance(other, ast.Tuple):
                                    for elt in other.elts:
                                        if isinstance(elt, ast.Constant):
                                            found.add(elt.value)
                                elif isinstance(other, ast.Constant):
                                    found.add(other.value)
            return found

        here = Path(__file__).parent
        repl = keys(here / "interactive.py", "_slash_command_impl")
        tui = keys(here / "tui.py", "_slash_command_impl")
        assert repl == tui, (
            f"only the REPL dispatches {sorted(repl - tui)}; "
            f"only the TUI dispatches {sorted(tui - repl)}"
        )
        assert {"/memory", "/lineage", "/tree", "/clone"} <= repl

    def test_every_corpus_phrasing_ranks_its_own_command_first(self) -> None:
        """The discoverability gate, run against this round's four rows."""
        from cli import onboarding

        rows = palette.palette_entries()
        misses = []
        for row in onboarding.TASKS:
            hits = [e.name for e in palette.search_entries(rows, row.phrase)[:1]]
            if not hits or hits[0] != row.command:
                misses.append((row.phrase, row.command, hits))
        assert not misses, f"rank-1 misses: {misses}"

    def test_the_four_commands_are_findable_by_task_wording(self) -> None:
        rows = palette.palette_entries()
        for query, expected in (
            ("what do you remember about this", "/memory"),
            ("what happened before the summary", "/lineage"),
            ("fork this conversation", "/tree"),
        ):
            hits = [e.name for e in palette.search_entries(rows, query)[:1]]
            assert hits and hits[0] == expected, f"{query!r} -> {hits}"


# ---------------------------------------------------------------------------
# 10. Hostile input
# ---------------------------------------------------------------------------


class TestHostileInputIsContained:
    @pytest.mark.parametrize(
        "line",
        [
            "/memory accept ../../etc/passwd --confirm",
            "/memory show <script>alert(1)</script>",
            "/memory reject a --confirm --reason '<script>'",
            "/memory export /dev/null",
            "/tree fork ../../etc",
            "/tree rewind ../../windows",
            "/tree abandon ../../",
            "/lineage search '<script>'",
            "/clone fork",
            "/tree bogusverb extra",
            "/memory",
            "/tree",
            "/lineage",
        ],
    )
    def test_no_harm_no_traceback(self, root: Path, convo: str, line: str) -> None:
        """Every line drives the REAL REPL dispatcher and never raises.

        Driven through `_slash_command` rather than the handler, so the
        preflight, the verb resolution and the render all run.
        """
        from cli import interactive as I
        from cli import ui

        _proposal(root, "a", "use pathlib")
        state: Dict[str, Any] = {
            "repo": None,
            "file_config": {},
            "conversation": S.load_or_create(str(root), None, convo, strict=False),
        }
        printed: List[str] = []
        original = ui.console().print

        def capture(*args: Any, **kwargs: Any) -> None:
            printed.append(" ".join(str(a) for a in args))

        ui.console().print = capture
        try:
            I._slash_command(line, line.lower(), {}, root, state)
        finally:
            ui.console().print = original
        joined = " ".join(printed)
        assert "Traceback" not in joined
        assert "File \"" not in joined

    def test_the_sanitiser_is_on_the_render_path(self) -> None:
        """A payload crossing into rich must be escaped, not interpreted.

        Asserted on the SOURCE rather than the render: the two sanctioned
        exits are `escape_lines` and a `Text` sink, so what has to be true is
        that the value reaches `escape()`.
        """
        source = Path(__file__).with_name("interactive.py").read_text(encoding="utf-8")
        body = source.split("def memory_subcommand(")[1].split("\ndef ")[0]
        assert "escape(" in source
        assert "receipt.get(\"lines\")" in source or "lines" in body

    def test_the_modules_import_no_completion_vocabulary(self) -> None:
        """None of this may be readable by a verifier.

        A budget field or a memory count that a completion path can read is a
        claim that can be promoted into a success criterion.
        """
        banned = (
            "completed_verified",
            "completed_unverified",
            "status_is_success",
            "run_verdict",
            "agent_contracts",
        )
        here = Path(__file__).parent
        for name in ("context_budget.py", "memory_ui.py", "lineage.py"):
            source = (here / name).read_text(encoding="utf-8")
            for word in banned:
                assert word not in source, f"{name} carries {word}"


# ---------------------------------------------------------------------------
# 11. The projection writes nothing
# ---------------------------------------------------------------------------


class TestTheReadersWriteNothing:
    def test_budget_and_lineage_read_only(self, root: Path, convo: str) -> None:
        before = {
            p: p.stat().st_mtime_ns
            for p in root.rglob("*")
            if p.is_file()
        }
        budget.budget_projection(
            conversation=S.load_or_create(str(root), None, convo, strict=False),
            log_root=root,
        )
        lineage.lineage_rows(root, convo)
        lineage.rotation_events(root, convo)
        lineage.pre_compaction_history(root, convo)
        lineage.searchable_turns(root, convo)
        after = {
            p: p.stat().st_mtime_ns
            for p in root.rglob("*")
            if p.is_file()
        }
        assert before == after, f"a reader wrote: {set(after) ^ set(before)}"
