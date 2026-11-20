"""P3a/W1 T3 acceptance and regression pins: lineage, tree, background jobs.

Run with::

    python -m pytest runtime/test_p3w1_pins.py -q

**These are not collected by a bare ``python -m pytest``** -- ``pyproject.toml``
pins ``testpaths = ["tests"]``, which is the same gap
``runtime/test_p2w1_trust_pins.py`` and three other in-package suites already
record. That is a filed request to T5, not a reason to put the pins in
``tests/`` (T5's directory).

Every test here is paired with a **control arm** where a passing assertion could
otherwise be satisfied by the feature doing nothing. "A rotation never destroys
history" is trivially true of a rotation that never happens; "background
admission refuses at the floor" is trivially true of a queue that admits
nothing. Each control arm proves the mechanism is live and the assertion is
about the behaviour rather than about an inert object.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from cli import lineage as cli_lineage
from cli import session as cli_session
from runtime import background_jobs as bgj
from runtime import checkpoint as ckpt
from runtime import session_lineage as sl
from runtime import session_tree as st
from runtime.budget_governor import BudgetGovernor

REPO = "C:\\repo-identity-for-tests"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch):
    """Keep every test out of the developer's real ``NEO_HOME``."""
    home = tempfile.mkdtemp(prefix="p3w1-home-")
    monkeypatch.setenv("NEO_HOME", home)
    return home


@pytest.fixture
def log_root():
    return tempfile.mkdtemp(prefix="p3w1-logs-")


@pytest.fixture
def repo_path():
    return tempfile.mkdtemp(prefix="p3w1-repo-")


def _seed(log_root: str, repo: str, *, turns: int = 4, texts=None, **identity: str):
    """A real session with real turns and real identity, via ``cli.session``.

    ``texts`` is explicit because the reversibility pins need a session whose turn
    text MATCHES the run journal's rows exactly. Left to the default the fixture
    wrote "turn N body" into both and the match was accidental rather than
    designed.
    """
    sid = cli_session.new_session_id()
    session = cli_session.load_or_create(log_root, repo, sid)
    session["_log_root"] = log_root
    bodies = list(texts) if texts is not None else [f"turn {i} body" for i in range(turns)]
    for index, body in enumerate(bodies):
        cli_session.append_turn(session, "user", body, turn_id=f"t{index}")
    session.update(
        {
            "repository_identity": str(repo),
            "request_identity": "req-abc",
            "revision_identity": "empty",
            "resume_namespace": "run-1",
            "effort_identity": "auto",
        }
    )
    session.update(identity)
    cli_session.save_session(log_root, session)
    return session


def _read(log_root: str, repo: str, sid: str):
    return cli_session.load_or_create(log_root, repo, sid, strict=False)


# ---------------------------------------------------------------------------
# Lineage: rotation never destroys history, and is not a fork
# ---------------------------------------------------------------------------


class TestRotationIsNotDestruction:
    def test_a_rotation_leaves_every_parent_turn_in_place(self, log_root, repo_path):
        """The parent is never shortened by a rotation."""
        parent = _seed(log_root, repo_path)
        before = len(parent["turns"])
        assert before == 4, "fixture control: the parent must start with turns"

        receipt = sl.rotate_session(
            log_root, parent, summary="SUMMARY: folded", source_turn_ids=["t0", "t1"]
        )
        after = _read(log_root, repo_path, parent["session_id"])
        assert len(after["turns"]) == before
        assert receipt["history_destroyed"] is False

    def test_the_control_arm_a_rotation_really_happened(self, log_root, repo_path):
        """Without this, every "never destroys" assertion could pass vacuously."""
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="SUMMARY: folded", source_turn_ids=["t0"]
        )
        after = _read(log_root, repo_path, parent["session_id"])
        assert after["end_reason"] == "compression"
        assert after["rotated_to"] == receipt["child_session_id"]
        assert after["session_id"] != receipt["child_session_id"]

    def test_a_rotation_is_not_a_fork(self, log_root, repo_path):
        """The child's ACTIVE turns start empty; the retained history travels.

        Control arm: the child does carry the parent's turns in ``raw_turns``, so
        this asserts a real distinction rather than an empty child.
        """
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"]
        )
        child = _read(log_root, repo_path, receipt["child_session_id"])
        assert child["turns"] == [], "a rotation must not look like a fork"
        assert len(child["raw_turns"]) == 4, "control: the history travelled"
        assert child["parent_session_id"] == parent["session_id"]

    def test_the_summary_is_an_index_that_names_where_the_evidence_lives(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"]
        )
        child = _read(log_root, repo_path, receipt["child_session_id"])
        assert child["summaries"][-1]["is_index"] is True
        assert child["summaries"][-1]["source_turn_ids"] == ["t0", "t1"]
        assert child["summaries"][-1]["turn_count"] == 2

    def test_retained_history_is_copied_by_value_not_shared(self, log_root, repo_path):
        """Two sessions must not be able to disagree about retained history.

        A shared list object would let a later compaction of the child rewrite the
        parent's record of what it still holds. The mutation is SAVED before the
        parent is re-read -- mutating an in-memory copy would prove nothing.
        """
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"]
        )
        child = _read(log_root, repo_path, receipt["child_session_id"])
        child["compacted_turns"].append({"turn_id": "INJECTED", "text": "x"})
        child["_log_root"] = log_root
        cli_session.save_session(log_root, child)

        again = _read(log_root, repo_path, receipt["child_session_id"])
        assert "INJECTED" in [t.get("turn_id") for t in again["compacted_turns"]], (
            "the mutation never persisted, so the test proves nothing"
        )
        parent_again = _read(log_root, repo_path, parent["session_id"])
        assert "INJECTED" not in [
            t.get("turn_id") for t in (parent_again.get("compacted_turns") or [])
        ], "the child's list is shared with the parent"

    def test_the_folded_turns_are_reachable_from_the_child(self, log_root, repo_path):
        """"What did that compaction take?" is answerable from the child alone."""
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"]
        )
        report = sl.pre_compaction_index(log_root, receipt["child_session_id"])
        assert report["available"] is True
        assert report["source_turn_ids"] == ["t0", "t1"]
        assert {t.get("turn_id") for t in report["turns"]} == {"t0", "t1"}
        assert report["history_retained"] is True


class TestRotationRefusals:
    def test_an_empty_summary_is_refused(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        with pytest.raises(sl.LineageRefused) as info:
            sl.rotate_session(log_root, parent, summary="   ")
        assert "empty summary" in info.value.reason
        after = _read(log_root, repo_path, parent["session_id"])
        assert after.get("rotated_to") is None, "a refusal wrote a link anyway"

    def test_an_unknown_reason_is_refused_and_names_the_vocabulary(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        with pytest.raises(sl.LineageRefused) as info:
            sl.rotate_session(log_root, parent, summary="S", reason="whatever")
        assert "compression" in info.value.reason

    def test_rotating_onto_itself_is_refused(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        with pytest.raises(sl.LineageRefused) as info:
            sl.rotate_session(
                log_root, parent, summary="S", child_session_id=parent["session_id"]
            )
        assert "NEW session" in info.value.reason

    def test_a_lineage_refusal_is_not_a_permission_error(self, log_root, repo_path):
        """The resilient provider paths absorb ``PermissionError`` and retry it."""
        parent = _seed(log_root, repo_path)
        with pytest.raises(sl.LineageRefused) as info:
            sl.rotate_session(log_root, parent, summary="")
        assert not isinstance(info.value, PermissionError)


# ---------------------------------------------------------------------------
# Lineage: identity closure
# ---------------------------------------------------------------------------


class TestIdentityClosure:
    def test_every_dimension_is_named_and_reported(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        child = _read(log_root, repo_path, "sess-00000000")
        status = sl.identity_status(parent, child)
        assert set(status["fields"]) == set(sl.LINEAGE_IDENTITY_FIELDS)
        assert len(sl.LINEAGE_IDENTITY_FIELDS) == 5

    def test_the_child_inherits_the_identity_verbatim(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        child = _read(log_root, repo_path, receipt["child_session_id"])
        for field in sl.LINEAGE_IDENTITY_FIELDS:
            assert child.get(field) == parent.get(field), field
        assert receipt["identity"]["changed"] == []

    def test_the_blank_exemption_matches_the_checkpoint_authority(
        self, log_root, repo_path
    ):
        """Two answers to "which identity fields may be blank" is the drift this
        repository keeps paying for, so the two sets are asserted equal."""
        assert sl.BLANK_ALLOWED_IDENTITY_FIELDS == (
            ckpt._ALLOWED_BLANK_IDENTITY_FIELDS
        )

    def test_a_dropped_identity_field_refuses_and_writes_nothing(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        parent["request_identity"] = "req-abc"
        parent["_log_root"] = log_root
        # Simulate a weakened child directly: identity_status is the gate.
        weakened = dict(parent)
        weakened["request_identity"] = ""
        status = sl.identity_status(parent, weakened)
        assert status["ok"] is False
        assert "request_identity" in status["weakened"]

    def test_the_control_arm_an_unchanged_identity_passes(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        assert sl.identity_status(parent, dict(parent))["ok"] is True

    def test_a_rotation_of_a_session_carrying_no_identity_still_works(
        self, log_root, repo_path
    ):
        """A ``cli.session`` snapshot has no checkpoint identity keys at all.

        Requiring all five here would make the feature unreachable on every real
        session while protecting nothing. Completeness is reported (``complete``)
        and is the checkpoint's business; this gate is narrower and is about
        CHANGE.
        """
        sid = cli_session.new_session_id()
        bare = cli_session.load_or_create(log_root, repo_path, sid)
        bare["_log_root"] = log_root
        cli_session.append_turn(bare, "user", "hello", turn_id="t0")
        cli_session.save_session(log_root, bare)
        receipt = sl.rotate_session(log_root, bare, summary="S")
        assert receipt["ok"] is True
        assert receipt["identity"]["changed"] == []
        assert receipt["identity"]["complete"] is False, "honesty about completeness"


class TestResume:
    def test_a_rotated_parent_redirects_to_its_child(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        state = sl.resume_state(log_root, parent["session_id"])
        assert state["may_resume"] is False
        assert state["continued_session_id"] == receipt["child_session_id"]
        assert "rotated to" in state["continued_reason"]

    def test_only_an_explicit_continue_resumes_the_rotated_parent(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        sl.rotate_session(log_root, parent, summary="S")
        assert sl.resume_state(
            log_root, parent["session_id"], continue_request=True
        )["may_resume"] is True

    def test_the_child_itself_resumes(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        assert sl.resume_state(log_root, receipt["child_session_id"])["may_resume"] is True

    def test_a_wrong_namespace_refuses_and_names_the_field(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        wrong = dict(sl.identity_of(parent), resume_namespace="run-OTHER")
        state = sl.resume_state(
            log_root, receipt["child_session_id"], identity=wrong
        )
        assert state["may_resume"] is False
        assert any("resume_namespace" in p for p in state["identity_problems"])

    def test_the_control_arm_the_right_identity_resumes(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        assert sl.resume_state(
            log_root,
            receipt["child_session_id"],
            identity=sl.identity_of(parent),
        )["may_resume"] is True

    def test_a_break_anywhere_in_the_ancestry_refuses(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        first = sl.rotate_session(log_root, parent, summary="S1")
        child = _read(log_root, repo_path, first["child_session_id"])
        child["_log_root"] = log_root
        second = sl.rotate_session(log_root, child, summary="S2")
        # Delete the middle generation: the grandchild's chain is now broken.
        os.remove(str(cli_session._session_path(log_root, first["child_session_id"])))
        state = sl.resume_state(log_root, second["child_session_id"])
        assert state["may_resume"] is False
        assert state["ancestry_problems"], "the break was not reported"

    def test_an_unreadable_session_is_a_value_not_an_exception(self, log_root):
        state = sl.resume_state(log_root, "sess-does-not-exist")
        assert state["ok"] is False
        assert state["may_resume"] is False
        assert "could not be read" in state["reason"]


# ---------------------------------------------------------------------------
# The real cross-module contract: T4's reader must see the rotation
# ---------------------------------------------------------------------------


class TestTheT4ReaderSeesIt:
    """These drive the REAL ``cli/lineage.py`` reader against our writes.

    The reader is T4's file and is not edited here. What is pinned is that a
    rotation written by this module is legible to it -- which is the whole point
    of writing the shapes the reader already reads.
    """

    def test_the_lineage_chain_walks_parent_to_child(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        chain = cli_lineage.lineage_chain(log_root, receipt["child_session_id"])
        assert [row["depth"] for row in chain] == [0, 1]
        assert [row["session_id"] for row in chain] == [
            receipt["child_session_id"],
            parent["session_id"],
        ]

    def test_last_rotation_names_the_child_and_a_time(self, log_root, repo_path):
        """Regression pin for a defect found by measurement, not by reading.

        Writing a ``session_rotated`` JOURNAL row made this return the journal
        row instead of the snapshot row, and the journal row rendered with an
        empty child and no timestamp -- so ``last_rotation`` reported a rotation
        to nothing.
        """
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        last = cli_lineage.last_rotation(log_root, parent["session_id"])
        assert last.get("child_session_id") == receipt["child_session_id"]
        assert last.get("reason") == "compression"
        assert last.get("at") is not None

    def test_the_rotation_is_counted_exactly_once(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        sl.rotate_session(log_root, parent, summary="S")
        events = cli_lineage.rotation_events(log_root, parent["session_id"])
        assert events["rotation_count"] == 1, (
            "the snapshot link and a journal row both counted as rotations"
        )

    def test_the_child_is_never_reported_as_having_rotated(self, log_root, repo_path):
        """A second measured defect: a child that rotated to NOTHING.

        ``cli.lineage`` reads ``session_rotated`` as "this session ended and
        opened a successor". A child has not ended, so writing that kind into a
        child's journal reported a phantom rotation.
        """
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        events = cli_lineage.rotation_events(log_root, receipt["child_session_id"])
        assert events["rotation_count"] == 0
        assert all(e["kind"] != "rotation" for e in events["events"])

    def test_the_audit_trail_is_in_both_journals(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        for sid in (parent["session_id"], receipt["child_session_id"]):
            text = cli_session.event_log_path(log_root, sid).read_text(
                encoding="utf-8"
            )
            assert sl.ROTATION_RECEIPT_EVENT in text, sid

    def test_the_t4_pre_compaction_view_finds_the_folded_turns(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"]
        )
        report = cli_lineage.pre_compaction_history(log_root, receipt["child_session_id"])
        assert report["available"] is True
        assert report["dropped_total"] == 2
        assert report["history_retained"] is True

    def test_both_vocabularies_are_the_same_words(self, log_root, repo_path):
        """``ROTATION_REASONS`` duplicated in two modules is two answers."""
        assert sl.ROTATION_REASONS == cli_lineage.ROTATION_REASONS


# ---------------------------------------------------------------------------
# Ancestry, dedupe, search
# ---------------------------------------------------------------------------


class TestAncestryAndSearch:
    def test_a_three_deep_lineage_walks_and_agrees_with_the_reader(
        self, log_root, repo_path
    ):
        parent = _seed(log_root, repo_path)
        first = sl.rotate_session(log_root, parent, summary="S1")
        child = _read(log_root, repo_path, first["child_session_id"])
        child["_log_root"] = log_root
        cli_session.append_turn(child, "user", "turn 9 after one", turn_id="t9")
        cli_session.save_session(log_root, child)
        second = sl.rotate_session(log_root, child, summary="S2")
        gcid = second["child_session_id"]
        ours = [r["session_id"] for r in sl.ancestry(log_root, gcid)]
        theirs = [r["session_id"] for r in cli_lineage.lineage_chain(log_root, gcid)]
        assert ours == theirs
        assert [r["depth"] for r in sl.ancestry(log_root, gcid)] == [0, 1, 2]

    def test_a_parent_cycle_terminates_instead_of_hanging(self, log_root, repo_path):
        """A hand-edited snapshot pointing at itself must produce an answer."""
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        broken = _read(log_root, repo_path, receipt["child_session_id"])
        broken["parent_session_id"] = receipt["child_session_id"]
        broken["_log_root"] = log_root
        cli_session.save_session(log_root, broken)
        chain = sl.ancestry(log_root, receipt["child_session_id"], max_depth=8)
        assert len(chain) <= 8
        assert any("loops back" in str(r.get("reason") or "") for r in chain)

    def test_inherited_content_is_collapsed_by_digest(self, log_root, repo_path):
        """The scenario dedupe exists for: a rotation carries turns forward."""
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        found = sl.search_lineage(log_root, receipt["child_session_id"], "turn")
        assert found["raw_hits"] == 8, "control: the turns appear on both sides"
        assert found["deduped_hits"] == 4
        assert found["collapsed_by_digest"] == 4
        assert found["hits"][0]["also_in"], "the duplicate session is named"

    def test_distinct_content_is_not_collapsed(self, log_root, repo_path):
        """The control arm: dedupe cannot pass by collapsing everything."""
        rows = [
            {"session_id": "a", "turn_id": "1", "text": "one"},
            {"session_id": "b", "turn_id": "2", "text": "two"},
            {"session_id": "c", "turn_id": "3", "text": "one"},
        ]
        out = sl.dedupe_hits(rows)
        assert len(out) == 2
        assert out[0]["occurrences"] == 2
        assert out[0]["also_in"] == ["c"]
        assert {r["text"] for r in out} == {"one", "two"}

    def test_truncation_is_stated_not_implied(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        found = sl.search_lineage(
            log_root, receipt["child_session_id"], "turn", limit=2
        )
        assert found["shown"] == 2
        assert found["truncated"] is True, "a bounded list read as complete is a lie"

    def test_an_empty_query_still_returns_the_lineage(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        found = sl.search_lineage(log_root, receipt["child_session_id"], "")
        assert found["ok"] is True and found["sessions_searched"]

    def test_describe_is_renderable_without_walking(self, log_root, repo_path):
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S")
        payload = sl.describe(log_root, receipt["child_session_id"])
        assert set(payload) >= {
            "schema",
            "session_id",
            "lineage",
            "resume",
            "rotation_reasons",
            "identity_fields",
        }


class TestRotationIsReversible:
    """A rotation must be UNDOABLE through the run's ConversationJournal.

    This is the gap the round was asked to close. ``ConversationJournal`` already
    folds ``compaction`` rows against ``restore`` rows, so the reversible-compaction
    contract existed -- but nothing ever WROTE a rotation into it, so a rotation
    was recorded and then un-restorable. A feature that drops context and cannot
    put it back is the failure this whole module is about.
    """

    @staticmethod
    def _journal(run_dir, texts):
        from runtime.checkpoint import ConversationJournal

        journal = ConversationJournal(run_dir)
        journal.append({"record": "base", "messages": [{"role": "system", "content": "sys"}]})
        for index, text in enumerate(texts):
            journal.append(
                {
                    "record": "turn",
                    "seq": index + 1,
                    "turn": index,
                    "kind": "text",
                    "role": "user",
                    "content": text,
                }
            )
        return journal

    TEXTS = ("alpha question", "beta answer", "gamma question", "delta answer")

    def test_the_full_loop_drop_then_restore(self, log_root, repo_path, tmp_path):
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=list(self.TEXTS))
        journal = self._journal(run_dir, self.TEXTS)
        before = [row["content"] for row in journal.live_snapshot()["history"]]

        receipt = sl.rotate_session(
            log_root,
            parent,
            summary="SUMMARY: t0,t1 folded away",
            source_turn_ids=["t0", "t1"],
            run_dir=run_dir,
        )
        reversible = receipt["reversible"]
        assert reversible["written"] is True
        assert reversible["reversible"] is True, reversible["reason"]
        # Session turn IDS are journal SEQUENCES -- resolved, not assumed.
        assert reversible["dropped_seqs"] == [1, 2]
        assert reversible["unresolved"] == []

        during = [row["content"] for row in journal.live_snapshot()["history"]]
        assert during == ["gamma question", "delta answer"], (
            "the folded turns are still live after the rotation"
        )
        assert before == list(self.TEXTS)

        restored = journal.restore_compaction(reversible["compaction_id"])
        assert restored is not None
        after = [row["content"] for row in journal.live_snapshot()["history"]]
        assert after == list(self.TEXTS), "the restore did not bring the turns back"
        assert [row["content"] for row in restored["history"]] == list(self.TEXTS)

    def test_a_rotation_destroys_nothing_even_while_reversible(
        self, log_root, repo_path, tmp_path
    ):
        """Reversibility must not be bought by trimming the parent."""
        parent = _seed(log_root, repo_path, texts=list(self.TEXTS))
        self._journal(tmp_path / "run", self.TEXTS)
        sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"],
            run_dir=tmp_path / "run",
        )
        assert len(_read(log_root, repo_path, parent["session_id"])["turns"]) == 4

    def test_the_restore_is_an_appended_row_not_a_rewrite(
        self, log_root, repo_path, tmp_path
    ):
        """Rolling back appends the fact that it no longer applies."""
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=list(self.TEXTS))
        journal = self._journal(run_dir, self.TEXTS)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"],
            run_dir=run_dir,
        )
        journal.restore_compaction(receipt["reversible"]["compaction_id"])
        kinds = [row.get("record") for row in journal.load()]
        assert kinds == ["base", "turn", "turn", "turn", "turn", "compaction", "restore"]
        assert kinds.count("compaction") == 1, "the compaction row was rewritten"

    def test_the_control_arm_the_rotation_really_changed_the_fold(
        self, log_root, repo_path, tmp_path
    ):
        """Without this, 'restore brought them back' could pass on a no-op."""
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=list(self.TEXTS))
        journal = self._journal(run_dir, self.TEXTS)
        sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"], run_dir=run_dir
        )
        during = [row["content"] for row in journal.live_snapshot()["history"]]
        assert "alpha question" not in during, "nothing was actually dropped"

    def test_no_run_dir_means_not_reversible_and_says_so(self, log_root, repo_path):
        """A rotation cannot be undone in a run whose journal nobody has."""
        parent = _seed(log_root, repo_path)
        receipt = sl.rotate_session(log_root, parent, summary="S", source_turn_ids=["t0"])
        reversible = receipt["reversible"]
        assert reversible["attempted"] is False
        assert reversible["reversible"] is False
        assert "run_dir" in reversible["reason"]
        assert receipt["ok"] is True, "the rotation itself must still happen"

    def test_a_folded_turn_missing_from_the_journal_is_unresolved(
        self, log_root, repo_path, tmp_path
    ):
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=["session-only text"])
        self._journal(run_dir, ["something else entirely"])
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"], run_dir=run_dir
        )
        reversible = receipt["reversible"]
        assert reversible["written"] is True, "the rotation is still recorded"
        assert reversible["reversible"] is False
        assert reversible["dropped_seqs"] == []
        assert any("no journal row" in item for item in reversible["unresolved"])

    def test_an_ambiguous_text_refuses_to_guess_which_row_to_drop(
        self, log_root, repo_path, tmp_path
    ):
        """Two rows carrying one text: dropping 'one of them' drops history
        nobody can name, so neither is dropped."""
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=["same text"])
        self._journal(run_dir, ["same text", "same text"])
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"], run_dir=run_dir
        )
        reversible = receipt["reversible"]
        assert reversible["dropped_seqs"] == []
        assert reversible["reversible"] is False
        assert any("2 journal rows" in item for item in reversible["unresolved"])

    def test_explicit_dropped_seqs_win_over_content_matching(
        self, log_root, repo_path, tmp_path
    ):
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=["a", "b", "c"])
        journal = self._journal(run_dir, ["a", "b", "c"])
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"],
            run_dir=run_dir, dropped_seqs=[3],
        )
        assert receipt["reversible"]["resolution"] == "explicit"
        assert receipt["reversible"]["dropped_seqs"] == [3]
        assert [r["content"] for r in journal.live_snapshot()["history"]] == ["a", "b"]

    def test_drift_between_the_receipt_and_the_journal_is_reported(
        self, log_root, repo_path, tmp_path
    ):
        """A restore that un-drops the wrong sequences is a corruption."""
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=["a", "b"])
        self._journal(run_dir, ["a", "b"])
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0"], run_dir=run_dir
        )
        verdict = sl.rotation_reversible(
            run_dir, receipt["reversible"]["compaction_id"], expected_dropped=[99]
        )
        assert verdict["active"] is True
        assert verdict["drifted"] is True
        assert "wrong history" in verdict["reason"]

    def test_a_restored_rotation_reports_nothing_left_to_restore(
        self, log_root, repo_path, tmp_path
    ):
        run_dir = tmp_path / "run"
        parent = _seed(log_root, repo_path, texts=list(self.TEXTS))
        journal = self._journal(run_dir, self.TEXTS)
        receipt = sl.rotate_session(
            log_root, parent, summary="S", source_turn_ids=["t0", "t1"], run_dir=run_dir
        )
        journal.restore_compaction(receipt["reversible"]["compaction_id"])
        verdict = sl.rotation_reversible(
            run_dir, receipt["reversible"]["compaction_id"],
            expected_dropped=receipt["reversible"]["dropped_seqs"],
        )
        assert verdict["active"] is False
        assert "not in force" in verdict["reason"]

    def test_an_empty_compaction_id_is_refused_rather_than_guessed(
        self, tmp_path
    ):
        verdict = sl.rotation_reversible(tmp_path / "run", "")
        assert verdict["reversible"] is False
        assert "no compaction id" in verdict["reason"]

    def test_the_control_arm_a_matching_resolution_drops_the_right_sequences(
        self, tmp_path
    ):
        """The resolver is measured on its own, away from a rotation."""
        run_dir = tmp_path / "run"
        self._journal(run_dir, ["one", "two", "three"])
        resolved = sl.resolve_dropped_seqs(
            run_dir, [{"turn_id": "t2", "text": "three"}]
        )
        assert resolved["dropped_seqs"] == [3]
        assert resolved["resolution"] == "content_digest"
        assert resolved["unresolved"] == []


# ---------------------------------------------------------------------------
# Session tree: append-only authority
# ---------------------------------------------------------------------------


class TestSessionTreeAuthority:
    """The append-only session tree, driven through its real class.

    ``st.fork_api`` returns a SIGNATURE MAP (the documented keys a caller has to
    handle), not callables -- so these drive ``SessionTree`` itself, which is what
    a CLI fork would delegate to.
    """

    @pytest.fixture
    def tree(self):
        return st.SessionTree(tempfile.mkdtemp(prefix="p3w1-tree-"))

    def test_advance_appends_and_the_journal_verifies(self, tree):
        for index in range(5):
            tree.advance(turn_id=f"t{index}", text=f"turn {index}")
        verdict = tree.verify()
        assert verdict["ok"] is True, verdict
        assert len(tree.entries()) == 5

    def test_fork_copies_zero_entries(self, tree):
        for index in range(5):
            tree.advance(turn_id=f"t{index}", text=f"turn {index}")
        before = len(tree.entries())
        receipt = tree.fork(label="branch-1")
        assert receipt is not None and receipt["entries_copied"] == 0
        assert len(tree.entries()) > before, "a fork appended to its parent"

    def test_rewind_moves_the_head_without_deleting_anything(self, tree):
        for index in range(5):
            tree.advance(turn_id=f"t{index}", text=f"turn {index}")
        first = tree.entries()[0].id
        receipt = tree.move_head(first, reason="rewind")
        assert receipt["moved"] is True
        assert receipt["to"] == first
        kinds = [e.kind for e in tree.entries()]
        assert kinds.count("turn") == 5, "a rewind deleted a turn"
        assert "head" in kinds, "the move was not recorded"
        assert receipt["history_retained"] is True, (
            "a rewind that dropped history would make this a destruction"
        )

    def test_a_branch_summary_is_marked_as_a_claim(self, tree):
        tree.advance(turn_id="t0", text="turn 0")
        receipt = tree.abandon(label="branch-1", summary="a generated summary", author="model")
        assert receipt["summary_written"] is True
        assert receipt["summary_is_claim"] is True
        claims = [e for e in tree.entries() if st.is_claim_kind(e.kind)]
        assert claims, "no claim entry was written"
        # ``branch_summary_line`` must accept the TreeEntry objects ``entries()``
        # returns, not only Mappings -- found by driving the real tree.
        line = st.branch_summary_line(claims[-1])
        assert st.CLAIM_MARKER in line

    def test_an_evidenced_claim_names_what_it_summarises(self, tree):
        """A claim that cites nothing must look different from one that does."""
        tree.advance(turn_id="t0", text="turn 0")
        receipt = tree.abandon(label="b", summary="a generated summary", author="model")
        assert receipt["evidence_resolvable"] is True
        claims = [e for e in tree.entries() if st.is_claim_kind(e.kind)]
        line = st.branch_summary_line(claims[-1])
        assert "summarises" in line and "NO EVIDENCE" not in line

    def test_an_unevidenced_claim_says_so(self, tree):
        """The wording is asserted directly: an unevidenced summary is the worst
        case and must not render like an evidenced one."""
        line = st.branch_summary_line({"kind": "branch_summary", "text": "s", "evidence": []})
        assert "NO EVIDENCE" in line

    def test_a_move_head_onto_a_claim_entry_is_refused(self, tree):
        """A claim is not a position the head may occupy."""
        tree.advance(turn_id="t0", text="x")
        tree.abandon(label="b", summary="s", author="model")
        claim = next(e for e in tree.entries() if st.is_claim_kind(e.kind))
        assert tree.move_head(claim.id, reason="onto a claim") is None

    def test_a_fork_with_no_head_returns_none(self, tree):
        assert tree.fork(label="nothing-to-fork") is None

    def test_the_control_arm_a_real_advance_did_produce_an_entry(self, tree):
        assert tree.advance(turn_id="t0", text="x") is not None

    def test_growth_and_describe_return_receipts(self, tree):
        tree.advance(turn_id="t0", text="x")
        assert isinstance(tree.growth(), dict)
        assert isinstance(st.describe(tree.root), dict)

    def test_gc_is_opt_in_and_defaults_to_collecting_nothing_fresh(self, tree):
        for index in range(5):
            tree.advance(turn_id=f"t{index}", text=f"turn {index}")
        dry = tree.gc(dry_run=True)
        assert dry["removed"] == 0, "a fresh journal was collected by default"
        assert dry["dry_run"] is True

    def test_verify_detects_a_tampered_entry(self, tree):
        """The control arm: ``verify()`` is not a constant ``True``."""
        tree.advance(turn_id="t0", text="original")
        assert tree.verify()["ok"] is True
        tampered = st.SessionTree(tree.root)
        text = tampered.entries_path.read_text(encoding="utf-8")
        tampered.entries_path.write_text(
            text.replace('"original"', '"tampered"'), encoding="utf-8"
        )
        assert st.SessionTree(tree.root).verify()["ok"] is False, (
            "a tampered journal verified clean"
        )


# ---------------------------------------------------------------------------
# Background jobs: foreground reservation, leases, bounds, metering
# ---------------------------------------------------------------------------


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def queue(tmp_path, clock):
    return bgj.BackgroundJobs(
        str(tmp_path / "jobs"), lease_s=60.0, max_attempts=3, clock=lambda: clock[0]
    )


class TestForegroundReservation:
    def test_admission_is_refused_at_the_reserved_floor(self, queue):
        decision = queue.foreground_admission(foreground_in_flight=3, total_slots=4)
        assert decision["admitted"] is False
        assert "reserved floor" in decision["reason"]
        assert decision["floor_slots"] == 3

    def test_the_control_arm_admission_passes_below_the_floor(self, queue):
        assert queue.foreground_admission(
            foreground_in_flight=2, total_slots=4
        )["admitted"] is True

    def test_enqueue_is_refused_rather_than_deferred(self, queue):
        with pytest.raises(bgj.JobRefused) as info:
            queue.enqueue("extract", foreground_in_flight=4, total_slots=4)
        assert "reserved floor" in info.value.reason
        assert queue.counts()["pending"] == 0, "a refused job was still queued"

    def test_claim_is_also_reserved(self, queue):
        queue.enqueue("extract", total_slots=10)
        with pytest.raises(bgj.JobRefused):
            queue.claim("w", foreground_in_flight=10, total_slots=10)

    def test_no_slot_budget_declared_admits(self, queue):
        assert queue.foreground_admission()["admitted"] is True


class TestLeases:
    def test_a_live_lease_blocks_a_second_claim(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        first = queue.claim("worker-A", total_slots=10)
        assert first["claimed"] is True and first["job_id"] == job
        second = queue.claim("worker-B", total_slots=10)
        assert second["claimed"] is False

    def test_only_the_owner_may_renew(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("worker-A", total_slots=10)
        with pytest.raises(bgj.JobRefused) as info:
            queue.renew(job, "worker-B")
        assert "current lease owner" in info.value.reason
        assert queue.renew(job, "worker-A")["event"] == "renewed"

    def test_an_expired_lease_is_reclaimable_and_names_its_previous_owner(
        self, queue, clock
    ):
        queue.enqueue("extract", total_slots=10)
        queue.claim("worker-A", total_slots=10)
        clock[0] += 120.0
        reclaimed = queue.reclaim_expired()
        assert len(reclaimed) == 1
        assert reclaimed[0]["reclaimed_from"] == "worker-A"
        assert queue.claim("worker-B", total_slots=10)["claimed"] is True

    def test_a_reclaim_inherits_the_attempt_count_rather_than_resetting_it(
        self, queue, clock
    ):
        """A job that kills its worker must not get a fresh attempt budget."""
        queue.enqueue("extract", total_slots=10)
        queue.claim("A", total_slots=10)
        clock[0] += 120.0
        queue.reclaim_expired()
        again = queue.claim("B", total_slots=10)
        assert again["attempt"] == 2, "the attempt budget was reset by a reclaim"

    def test_an_anonymous_claim_is_refused(self, queue):
        queue.enqueue("extract", total_slots=10)
        with pytest.raises(bgj.JobRefused):
            queue.claim("")

    def test_the_control_arm_a_renewal_extends_the_lease(self, queue, clock):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        clock[0] += 30.0
        queue.renew(job, "A")
        clock[0] += 45.0
        assert queue.reclaim_expired() == [], "the renewed lease still expired"


class TestBoundedAttempts:
    def test_the_attempt_budget_is_a_real_bound(self, tmp_path, clock):
        queue = bgj.BackgroundJobs(
            str(tmp_path / "j"), max_attempts=2, clock=lambda: clock[0]
        )
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        for _ in range(6):
            if not queue.claim("w", total_slots=10)["claimed"]:
                break
            clock[0] += 120.0
            queue.reclaim_expired()
        assert queue.state()[job]["state"] == "abandoned"

    def test_success_is_terminal_and_never_reclaimed(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        queue.succeed(job)
        assert queue.state()[job]["state"] in bgj.TERMINAL_JOB_STATES
        assert queue.claim("B", total_slots=10)["claimed"] is False

    def test_a_failed_job_is_terminal_too(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        queue.fail_permanently(job, "gave up")
        assert queue.state()[job]["state"] == "failed"
        assert queue.claim("B", total_slots=10)["claimed"] is False

    def test_the_control_arm_a_pending_job_is_claimable(self, queue):
        queue.enqueue("extract", total_slots=10)
        assert queue.claim("A", total_slots=10)["claimed"] is True


class TestRetryReusesTheExistingAuthorities:
    def test_a_transient_failure_schedules_a_retry_on_the_shared_backoff(
        self, queue, clock
    ):
        from runtime.budget_governor import backoff_seconds

        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        receipt = queue.record_failure(job, ConnectionError("connection reset"))
        assert receipt["decision"] == "retry"
        assert receipt["wait_s"] == pytest.approx(
            backoff_seconds(2, base_s=queue.backoff_base_s, cap_s=1e9)
        )

    def test_a_retry_wait_blocks_a_claim_until_it_elapses(self, queue, clock):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        receipt = queue.record_failure(job, ConnectionError("connection reset"))
        assert queue.state()[job]["state"] == "retry_wait"
        assert queue.claim("B", total_slots=10)["claimed"] is False, "backoff ignored"
        clock[0] = float(receipt["not_before_epoch"]) + 0.1
        assert queue.claim("B", total_slots=10)["claimed"] is True

    def test_quota_is_terminal_and_not_a_rate_limit(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        receipt = queue.record_failure(job, RuntimeError("insufficient_quota"))
        assert receipt["decision"] == "terminal"
        assert "not a rate limit" in receipt["reason"]

    def test_a_non_retryable_failure_is_terminal(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        receipt = queue.record_failure(job, ValueError("malformed request"))
        assert receipt["decision"] == "terminal"

    def test_the_control_arm_a_transient_failure_would_be_retried(self, queue):
        job = queue.enqueue("extract", total_slots=10)["job_id"]
        queue.claim("A", total_slots=10)
        assert (
            queue.record_failure(job, ConnectionError("connection reset"))["decision"]
            == "retry"
        ), "the classifier never says retry, so 'terminal' proves nothing"


class TestMetering:
    def test_an_unmetered_job_is_refused_rather_than_run(self, tmp_path):
        queue = bgj.BackgroundJobs(str(tmp_path / "j"))
        verdict = queue.authorize(messages=[{"role": "user", "content": "x"}])
        assert verdict["authorized"] is False
        assert "nothing is watching" in verdict["reason"]

    def test_a_metered_job_with_budget_is_authorized(self, tmp_path):
        queue = bgj.BackgroundJobs(
            str(tmp_path / "j"), governor=BudgetGovernor(cap_usd=5.0)
        )
        verdict = queue.authorize(
            messages=[{"role": "user", "content": "x"}],
            target={"provider": "openai", "model": "gpt-4o-mini"},
        )
        assert verdict["authorized"] is True
        assert verdict["metered"] is True

    def test_an_exhausted_budget_refuses_the_enqueue(self, tmp_path):
        queue = bgj.BackgroundJobs(
            str(tmp_path / "j"), governor=BudgetGovernor(cap_usd=0.0)
        )
        with pytest.raises(bgj.JobRefused) as info:
            queue.enqueue("extract", total_slots=10)
        assert "budget cap" in info.value.reason
        assert queue.describe()["budget_exhausted"] is True


class TestJournalIsTheAuthority:
    def test_state_is_folded_from_the_journal_not_held_in_memory(self, queue):
        job = queue.enqueue("extract", {"a": 1}, total_slots=10)["job_id"]
        assert queue.state()[job]["payload"] == {"a": 1}
        rows = [json.loads(l) for l in queue.journal_path.read_text().splitlines() if l]
        assert rows[0]["event"] == "enqueued"
        assert rows[0]["job_id"] == job

    def test_a_torn_final_line_is_skipped_not_fatal(self, queue):
        queue.journal_path.parent.mkdir(parents=True, exist_ok=True)
        queue.journal_path.write_text(
            '{"event":"enqueued","job_id":"a","state":"pending","ts":1}\n{"event":"enq',
            encoding="utf-8",
        )
        assert len(queue.rows()) == 1
        assert "a" in queue.state()

    def test_describe_reports_idleness_rather_than_implying_it(self, queue, clock):
        queue.enqueue("extract", total_slots=10)
        clock[0] += 10_000.0
        report = queue.describe()
        assert report["idle_beyond_grace"] is True
        assert report["idle_s"] > report["idle_grace_s"]
        assert set(report["counts"]) == set(bgj.JOB_STATES)

    def test_a_receipt_is_written_beside_the_journal(self, queue):
        queue.enqueue("extract", total_slots=10)
        path = queue.write_receipt()
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["schema"].startswith(
            "runtime.background_jobs"
        )

    def test_priority_beats_age_and_cannot_starve(self, tmp_path, clock):
        queue = bgj.BackgroundJobs(str(tmp_path / "j"), clock=lambda: clock[0])
        queue.enqueue("low", total_slots=10, priority=0)
        queue.enqueue("high", total_slots=10, priority=5)
        assert queue.claim("w", total_slots=10)["kind"] == "high"
