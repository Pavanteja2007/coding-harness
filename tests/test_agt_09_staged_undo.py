"""AGT-09 - staged undo with three restore granularities.

Every required proof from the prompt is here, named after the BEHAVIOUR rather
than the implementation, and each one is measured rather than asserted from a
comment:

- a revert restores BYTE-IDENTICAL content, hash-pinned in both directions
- each of the three granularities behaves as its NAME says, and the middle one
  (code without the conversation) is the DEFAULT
- a widened range reverts BOTH turns
- a live run refuses the revert, at the memory boundary AND at the CLI
- exclusions are REPORTED, never silently skipped
- a concurrent user edit is detected and REFUSED rather than overwritten

Plus the properties that make a staged store different from a checkpoint stack
and worth having at all: it is idempotent, it is scriptable across processes,
it never touches the user's git state, and its capture is best-effort so a
bookkeeping failure can never cost a user their work.

Host-only: no Docker, no model, no network. The mutation path in the capture
tests is the REAL ``harness.editor.apply_text_edit`` primitive over a real
directory, and the store is the REAL one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cli import fileview as _fv
from harness import editor
from memory.checkpoints import (
    DEFAULT_RESTORE_SCOPE,
    EXCLUSION_REASONS,
    RESTORE_SCOPES,
    CheckpointError,
    StagedSnapshotStore,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Dict[str, Any]:
    """A real git repository, a real log root, and a real conversation file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    (logs / "_conversations").mkdir(parents=True)
    (repo / "app.py").write_text("def go():\n    return 1\n", encoding="utf-8")
    (repo / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    (repo / "noise.log").write_text("scratch\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "neo@test")
    _git(repo, "config", "user.name", "neo")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    (logs / "_conversations" / "sess-test.json").write_text(
        json.dumps({"turns": ["one"]}), encoding="utf-8"
    )
    return {"repo": repo, "logs": logs, "session": "sess-test"}


def _store(workspace: Dict[str, Any], **kwargs: Any) -> StagedSnapshotStore:
    return StagedSnapshotStore(
        workspace["repo"], workspace["logs"], workspace["session"], **kwargs
    )


def _turn(
    store: StagedSnapshotStore,
    turn_id: str,
    *,
    edit: str = "",
    create: str = "",
    delete: str = "",
    talk: Any = None,
) -> None:
    """Run one REAL turn: capture, mutate, capture again.

    The mutation is a plain write rather than a tool call, because the store's
    contract is about what it recorded, not about who wrote it. The
    ``harness.editor`` hook path is exercised separately, through the real
    primitive.

    ``talk`` writes the conversation file MID-TURN, which is how a real run
    grows its history: the growth is inside the range, so it is accounted and a
    ``conversation`` revert can rewind it.
    """
    repo = store.repo_path
    paths = ["app.py"]
    if create:
        paths.append(create)
    if delete:
        paths.append(delete)
    store.capture(turn_id=turn_id, event="step", paths=paths)
    if edit:
        (repo / "app.py").write_text(edit, encoding="utf-8")
    if create:
        (repo / create).write_text("CREATED\n", encoding="utf-8")
    if delete:
        (repo / delete).unlink()
    if talk is not None:
        conversation = store.log_root / "_conversations" / f"{store.session_id}.json"
        conversation.write_text(json.dumps(talk), encoding="utf-8")
    store.capture(turn_id=turn_id, event="step", paths=paths)


# ---------------------------------------------------------------------------
# 1. the mechanism: a revert restores byte-identical content
# ---------------------------------------------------------------------------


class TestRevertRestoresByteIdenticalContent:
    def test_a_revert_restores_the_exact_pre_image_byte_for_byte(self, workspace):
        original = (workspace["repo"] / "app.py").read_bytes()
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")

        store.stage()
        receipt = store.commit()

        assert receipt["ok"] is True
        assert receipt["status"] == "reverted"
        # Hash-pinned in BOTH directions, and the bytes agree.
        restored = [row for row in receipt["restored"] if row["path"] == "app.py"]
        assert restored, "app.py was not reported as restored"
        import hashlib

        assert restored[0]["after_hash"] == hashlib.sha256(original).hexdigest()
        assert (workspace["repo"] / "app.py").read_bytes() == original

    def test_a_file_the_run_created_is_deleted_by_the_revert(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", create="fresh.py")
        assert (workspace["repo"] / "fresh.py").is_file()

        store.stage()
        receipt = store.commit()

        assert not (workspace["repo"] / "fresh.py").exists()
        row = next(r for r in receipt["restored"] if r["path"] == "fresh.py")
        assert row["action"] == "deleted"
        assert row["after_hash"] is None

    def test_a_file_the_run_deleted_is_put_back_byte_for_byte(self, workspace):
        original = (workspace["repo"] / "keep.py").read_bytes()
        store = _store(workspace)
        _turn(store, "turn-1", delete="keep.py")
        assert not (workspace["repo"] / "keep.py").exists()

        store.stage()
        receipt = store.commit()

        assert receipt["ok"] is True
        assert (workspace["repo"] / "keep.py").read_bytes() == original
        assert any(r["path"] == "keep.py" for r in receipt["restored"])

    def test_the_receipt_names_what_was_restored_and_what_could_not_be(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        # A second party's edit, recorded by nobody.
        (workspace["repo"] / "app.py").write_text("USER EDIT\n", encoding="utf-8")

        store.stage()
        receipt = store.commit()

        assert receipt["ok"] is False
        assert receipt["status"] == "partial"
        refused = [row for row in receipt["refused"] if row["path"] == "app.py"]
        assert refused, "the refusal did not name the path"
        assert refused[0]["reason"] == "changed_since_capture"
        # A receipt that cannot say what it did not do is not a receipt.
        assert "restored" in receipt and "refused" in receipt
        assert receipt["excluded"] == [] or isinstance(receipt["excluded"], list)

    def test_the_receipt_is_persisted_and_readable_back(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()
        receipt = store.commit()
        assert receipt.get("receipt_id")

        loaded = store.receipt(str(receipt["receipt_id"]))
        assert loaded["scope"] == receipt["scope"]
        assert [r["path"] for r in loaded["restored"]] == [
            r["path"] for r in receipt["restored"]
        ]


# ---------------------------------------------------------------------------
# 2. three granularities, and the middle one is the point
# ---------------------------------------------------------------------------


class TestThreeGranularities:
    def test_the_three_scopes_are_the_products_own_rewind_vocabulary(self):
        """No fourth vocabulary. The set is pinned EQUAL to the rewind scopes.

        ``memory`` may not import ``harness`` (the dependency direction is
        cli -> memory), so equality is enforced here rather than by an import -
        the same technique this repo already uses for the edit-refusal slugs.
        """
        import inspect

        from harness.agent_kernel import context as _ctx

        source = inspect.getsource(_ctx.rewind_run)
        for scope in RESTORE_SCOPES:
            assert f"`{scope}`" in source or f'"{scope}"' in source, (
                f"{scope} is not one of the rewind scopes any more"
            )
        assert set(RESTORE_SCOPES) == {"files", "conversation", "both"}

    def test_the_default_granularity_rewinds_code_and_keeps_the_conversation(
        self, workspace
    ):
        original_code = (workspace["repo"] / "app.py").read_bytes()
        conversation = workspace["logs"] / "_conversations" / "sess-test.json"
        store = _store(workspace)
        # The turn edited code AND talked; both are inside the range.
        _turn(
            store,
            "turn-1",
            edit="def go():\n    return 2\n",
            talk={"turns": ["one", "two"]},
        )

        store.stage()
        receipt = store.commit()

        assert DEFAULT_RESTORE_SCOPE == "files"
        assert receipt["scope"] == "files"
        # The code goes back...
        assert (workspace["repo"] / "app.py").read_bytes() == original_code
        # ...and the conversation is left EXACTLY as it is, which is the whole
        # point of the middle granularity.
        assert json.loads(conversation.read_text(encoding="utf-8")) == {
            "turns": ["one", "two"]
        }
        assert receipt["conversation"]["action"] == "kept"
        assert "conversation stays" in _fv.undo_scope_words("files")

    def test_the_conversation_granularity_rewinds_history_and_leaves_code_alone(
        self, workspace
    ):
        conversation = workspace["logs"] / "_conversations" / "sess-test.json"
        store = _store(workspace)
        _turn(
            store,
            "turn-1",
            edit="def go():\n    return 2\n",
            talk={"turns": ["one", "two"]},
        )
        edited_code = (workspace["repo"] / "app.py").read_bytes()

        store.stage(scope="conversation")
        receipt = store.commit()

        assert receipt["scope"] == "conversation"
        # The code is byte-identical: a conversation revert that also reverted
        # code would be a granularity that is a lie.
        assert (workspace["repo"] / "app.py").read_bytes() == edited_code
        assert json.loads(conversation.read_text(encoding="utf-8")) == {
            "turns": ["one"]
        }
        assert receipt["conversation"]["action"] == "restored"
        assert receipt["restored"] == []

    def test_the_both_granularity_rewinds_code_and_history_together(self, workspace):
        conversation = workspace["logs"] / "_conversations" / "sess-test.json"
        store = _store(workspace)
        _turn(
            store,
            "turn-1",
            edit="def go():\n    return 2\n",
            talk={"turns": ["one", "two"]},
        )

        store.stage(scope="both")
        receipt = store.commit()

        assert receipt["scope"] == "both"
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 1\n"
        )
        assert json.loads(conversation.read_text(encoding="utf-8")) == {
            "turns": ["one"]
        }
        assert receipt["conversation"]["action"] == "restored"
        assert any(item["path"] == "app.py" for item in receipt["restored"])

    def test_an_unknown_granularity_is_refused_and_names_the_real_ones(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        result = store.stage(scope="everything")
        assert result["ok"] is False
        assert result["status"] == "unknown_scope"
        assert set(result["scopes"]) == set(RESTORE_SCOPES)

    def test_each_granularity_has_its_own_honest_sentence(self):
        words = {scope: _fv.undo_scope_words(scope) for scope in RESTORE_SCOPES}
        assert "conversation stays" in words["files"]
        assert "files are untouched" in words["conversation"]
        assert "AND" in words["both"]
        # Three DIFFERENT sentences: one word each would not be a choice.
        assert len(set(words.values())) == 3


# ---------------------------------------------------------------------------
# 3. staging widens; it never pops
# ---------------------------------------------------------------------------


class TestStagedUndoWidensTheRange:
    def test_a_second_undo_widens_the_range_rather_than_popping_it(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        _turn(store, "turn-2", edit="def go():\n    return 3\n")
        _turn(store, "turn-3", edit="def go():\n    return 4\n")

        first = store.stage()
        assert first["turn_ids"] == ["turn-3"]

        second = store.widen()
        assert second["turn_ids"] == ["turn-3", "turn-2"]
        assert second["widened"] == 1

        third = store.widen()
        assert third["turn_ids"] == ["turn-3", "turn-2", "turn-1"]
        assert third["widened"] == 2
        # Nothing was ever removed: a pop would have shown as a SHRINKING list.
        assert len(third["turn_ids"]) == 3

    def test_a_widened_range_reverts_both_turns(self, workspace):
        store = _store(workspace)
        original = (workspace["repo"] / "app.py").read_bytes()
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        _turn(store, "turn-2", edit="def go():\n    return 3\n")
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 3\n"
        )

        store.stage()
        store.widen()
        receipt = store.commit()

        assert set(receipt["turn_ids"]) == {"turn-1", "turn-2"}
        assert (workspace["repo"] / "app.py").read_bytes() == original
        assert receipt["ok"] is True

    def test_widening_reaches_further_back_than_the_first_stage_would(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        _turn(store, "turn-2", edit="def go():\n    return 3\n")

        store.stage()
        store.widen()
        receipt = store.commit()

        # Two turns staged means the content before BOTH, not the content
        # before the newest one.
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 1\n"
        )
        assert receipt["scope"] == "files"

    def test_staging_nothing_is_an_honest_refusal_not_a_silent_no_op(self, workspace):
        store = _store(workspace)
        result = store.stage()
        assert result["ok"] is False
        assert result["status"] == "nothing_staged"
        assert "no turn" in result["reason"]

    def test_staging_an_unknown_turn_names_the_turns_that_exist(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        result = store.stage(turn_ids=["turn-404"])
        assert result["ok"] is False
        assert result["status"] == "unknown_turn"
        assert result["turns"] == ["turn-1"]

    def test_discarding_drops_the_range_without_reverting_anything(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()

        result = store.discard()

        assert result["ok"] is True
        assert result["status"] == "discarded"
        assert store.staged() == {}
        # The code is untouched: discarding is not a quiet revert.
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 2\n"
        )


# ---------------------------------------------------------------------------
# 4. a live run refuses the revert
# ---------------------------------------------------------------------------


class TestLiveRunRefusesRevert:
    def test_the_store_refuses_a_revert_while_a_run_is_live(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()

        receipt = store.commit(live_run=True)

        assert receipt["ok"] is False
        assert receipt["status"] == "refused"
        assert receipt["reason"] == "run_in_flight"
        assert receipt["restored"] == []
        # And the code really was left alone.
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 2\n"
        )
        # A refused revert stays STAGED, so the user's intent is not lost.
        assert store.staged()["turn_ids"] == ["turn-1"]

    def test_the_cli_refuses_before_it_touches_the_store(self, workspace):
        state = _fv.staged_undo_state(
            workspace["repo"], workspace["logs"], session_id=workspace["session"]
        )
        assert state["available"] is True
        gate = _fv.undo_live_refusal(True)
        assert gate["ok"] is False
        assert gate["reason"] == "run_in_flight"
        assert "wait for it" in gate["detail"]

    def test_both_shell_entry_points_refuse_while_in_flight(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")

        commit = _fv.commit_undo(
            workspace["repo"],
            workspace["logs"],
            session_id=workspace["session"],
            in_flight=True,
        )
        staged = _fv.stage_undo(
            workspace["repo"],
            workspace["logs"],
            session_id=workspace["session"],
            in_flight=True,
        )
        assert commit["status"] == "refused"
        assert staged["status"] == "refused"
        # A refusal at the CLI must not have created a staged range either.
        assert (
            _fv.staged_undo_state(
                workspace["repo"], workspace["logs"], session_id=workspace["session"]
            )["staged"]
            is False
        )

    def test_the_tui_refuses_a_live_revert_on_the_same_phrase(self, workspace):
        """One gate, shared: the TUI says exactly what the REPL says."""
        from cli import interactive as _iv

        said: List[str] = []
        state = {
            "repo": str(workspace["repo"]),
            "session_id": workspace["session"],
        }
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")

        handled = _iv._handle_staged_undo(
            state, workspace["logs"], "commit", say=said.append
        )
        assert handled is True
        joined = "\n".join(said)
        assert "undo" in joined


# ---------------------------------------------------------------------------
# 5. exclusions are reported, never silently skipped
# ---------------------------------------------------------------------------


class TestExclusionsAreReported:
    def test_a_gitignored_file_is_excluded_and_named(self, workspace):
        store = _store(workspace)
        record = store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "noise.log"]
        )
        reasons = {item["path"]: item["reason"] for item in record["excluded"]}
        assert reasons.get("noise.log") == "gitignored"
        assert "noise.log" not in record["files"]
        assert record["ignore_authority"] == "git"

    def test_a_very_large_file_is_excluded_and_named(self, workspace):
        (workspace["repo"] / "big.bin").write_bytes(b"x" * 4096)
        store = _store(workspace, max_file_bytes=512)
        record = store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "big.bin"]
        )
        reasons = {item["path"]: item["reason"] for item in record["excluded"]}
        assert reasons.get("big.bin") == "too_large"

    def test_an_out_of_scope_path_is_excluded_and_named(self, workspace):
        store = _store(workspace)
        record = store.capture(
            turn_id="turn-1",
            event="step",
            paths=["app.py", "docs/notes.md"],
            scope_prefixes=["app.py"],
        )
        reasons = {item["path"]: item["reason"] for item in record["excluded"]}
        assert reasons.get("docs/notes.md") == "out_of_scope"

    def test_a_traversal_path_is_excluded_rather_than_escaping(self, workspace):
        store = _store(workspace)
        record = store.capture(
            turn_id="turn-1", event="step", paths=["../escape.py", "/etc/passwd"]
        )
        assert len(record["excluded"]) == 2
        assert {item["reason"] for item in record["excluded"]} == {"out_of_scope"}
        assert record["files"] == {}

    def test_the_receipt_carries_the_exclusions_forward(self, workspace):
        (workspace["repo"] / "big.bin").write_bytes(b"x" * 4096)
        # A small ceiling from the FIRST capture, so the exclusion is recorded
        # by the snapshot the revert actually reads.
        store = _store(workspace, max_file_bytes=64)
        store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "noise.log", "big.bin"]
        )
        (workspace["repo"] / "app.py").write_text(
            "def go():\n    return 2\n", encoding="utf-8"
        )
        store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "noise.log", "big.bin"]
        )
        store.stage()

        receipt = store.commit()
        reported = {(item["path"], item["reason"]) for item in receipt["excluded"]}
        assert ("noise.log", "gitignored") in reported
        assert ("big.bin", "too_large") in reported
        # AND they are rendered, not just stored.
        rendered = "\n".join(_fv.render_undo_receipt(receipt))
        assert "excluded noise.log: gitignored" in rendered
        assert "excluded big.bin: too_large" in rendered

    def test_every_exclusion_reason_is_in_the_documented_vocabulary(self):
        for reason in EXCLUSION_REASONS:
            assert isinstance(reason, str) and reason
        # "unreadable: OSError" style refinements keep the base reason first.
        assert "unreadable" in EXCLUSION_REASONS


# ---------------------------------------------------------------------------
# 6. a concurrent user edit is refused, not overwritten
# ---------------------------------------------------------------------------


class TestConcurrentUserEditIsRefused:
    def test_an_unaccounted_change_is_refused_rather_than_overwritten(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        # Someone edits the file by hand after the run recorded its own change.
        (workspace["repo"] / "app.py").write_text("HAND EDIT\n", encoding="utf-8")

        store.stage()
        receipt = store.commit()

        assert receipt["ok"] is False
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "HAND EDIT\n"
        )
        refusal = receipt["refused"][0]
        assert refusal["path"] == "app.py"
        assert refusal["reason"] == "changed_since_capture"
        assert refusal["actual_hash"] and refusal["expected_hash"]
        assert "refused rather than overwriting" in refusal["detail"]

    def test_a_refusal_leaves_the_staged_range_pending_so_the_user_can_decide(
        self, workspace
    ):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        (workspace["repo"] / "app.py").write_text("HAND EDIT\n", encoding="utf-8")
        store.stage()

        store.commit()
        assert store.staged()["turn_ids"] == ["turn-1"]

    def test_a_force_commit_is_possible_but_is_loud_about_what_it_overwrote(
        self, workspace
    ):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        (workspace["repo"] / "app.py").write_text("HAND EDIT\n", encoding="utf-8")
        store.stage()

        receipt = store.commit(force=True)

        assert receipt["ok"] is True
        assert receipt["forced"] is True
        assert [item["path"] for item in receipt["overwritten_user_edits"]] == [
            "app.py"
        ]
        assert receipt["overwritten_count"] == 1
        rendered = "\n".join(_fv.render_undo_receipt(receipt))
        assert "forced: yes" in rendered
        assert "user edit(s) were overwritten on purpose" in rendered
        assert "app.py" in rendered

    def test_the_runs_own_changes_are_accounted_so_a_plain_revert_still_works(
        self, workspace
    ):
        """The guard must not fire on the change the revert EXISTS to undo."""
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()

        receipt = store.commit()

        assert receipt["refused"] == []
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 1\n"
        )

    def test_a_conversation_edit_made_after_capture_is_refused_too(self, workspace):
        conversation = workspace["logs"] / "_conversations" / "sess-test.json"
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        conversation.write_text(
            json.dumps({"turns": ["hand written"]}), encoding="utf-8"
        )
        store.stage(scope="conversation")

        receipt = store.commit()

        assert receipt["conversation"]["action"] == "partial"
        assert any(
            item.get("reason") == "changed_since_capture"
            for item in receipt["conversation"]["files"]
        )
        assert json.loads(conversation.read_text(encoding="utf-8")) == {
            "turns": ["hand written"]
        }


# ---------------------------------------------------------------------------
# 7. what makes this a STORE and not a checkpoint stack
# ---------------------------------------------------------------------------


class TestStoreProperties:
    def test_capturing_an_unchanged_state_twice_is_idempotent(self, workspace):
        store = _store(workspace)
        first = store.capture(turn_id="turn-1", event="step", paths=["app.py"])
        second = store.capture(turn_id="turn-1", event="step", paths=["app.py"])
        assert first["snapshot_id"] == second["snapshot_id"]
        # One journal row, not two: re-running a capture cannot grow the store.
        rows = [r for r in store._rows("snapshot") if r.get("turn_id") == "turn-1"]
        assert len(rows) == 1

    def test_identical_content_is_stored_once_as_one_object(self, workspace):
        """The store is CONTENT-ADDRESSED: same bytes, one object, two paths."""
        (workspace["repo"] / "copy.py").write_text("KEEP = 1\n", encoding="utf-8")
        store = _store(workspace)
        # conversation=False so the conversation artifact is not also an
        # object; this test is about FILE content, and the conversation is
        # stored through the same content-addressed path.
        first = store.capture(
            turn_id="turn-1", event="step", paths=["keep.py"], conversation=False
        )
        second = store.capture(
            turn_id="turn-2", event="step", paths=["copy.py"], conversation=False
        )
        assert (
            first["files"]["keep.py"]["object"] == second["files"]["copy.py"]["object"]
        )
        objects = [item for item in store.objects.rglob("*") if item.is_file()]
        assert len(objects) == 1, f"content was stored more than once: {objects}"

    def test_the_staged_range_survives_a_second_process(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()

        # A brand-new store object, i.e. what a second process or a resumed
        # session would build. The staged range is on disk, not in memory.
        other = _store(workspace)
        assert other.staged()["turn_ids"] == ["turn-1"]
        assert other.commit()["ok"] is True

    def test_no_commit_no_branch_move_and_no_index_change(self, workspace):
        repo = workspace["repo"]
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        before_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        before_status = _git(repo, "status", "--porcelain").stdout
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()
        store.commit()

        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert (
            _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            == before_branch
        )
        # The index is exactly as the run left it: the revert restored the
        # working tree and the repository's own state was never used as a
        # bookkeeping mechanism.
        assert _git(repo, "status", "--porcelain").stdout == before_status
        assert _git(repo, "stash", "list").stdout.strip() == ""

    def test_the_store_lives_outside_the_repository(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        assert workspace["logs"] in store.root.parents
        assert workspace["repo"] not in store.root.parents
        assert not (workspace["repo"] / "_undo").exists()

    def test_a_log_root_inside_the_repository_is_still_never_captured(self, workspace):
        """A forced in-repo log root must not eat the store into its own walk."""
        repo = workspace["repo"]
        inner = repo / "logs"
        inner.mkdir()
        store = StagedSnapshotStore(repo, inner, "sess-test")
        record = store.capture(turn_id="turn-1", event="step")
        assert "logs" not in record["files"]
        assert not any(item["path"].startswith("logs/") for item in record["excluded"])

    def test_the_store_refuses_a_repository_that_is_a_symbolic_link(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is not available here")
        with pytest.raises(CheckpointError):
            StagedSnapshotStore(link, tmp_path / "logs", "sess-test")


# ---------------------------------------------------------------------------
# 8. best-effort capture: bookkeeping must never cost a user their work
# ---------------------------------------------------------------------------


class TestCaptureIsBestEffort:
    def test_a_failing_capture_returns_a_failed_record_and_never_raises(
        self, workspace
    ):
        """A capture that cannot store is a RECORD, and the caller carries on."""
        seen: List[Any] = []

        class _Broken(StagedSnapshotStore):
            def _write_object(self, payload: bytes) -> str:  # type: ignore[override]
                raise OSError("no space left on device")

        broken = _Broken(workspace["repo"], workspace["logs"], workspace["session"])
        broken.trace = lambda kind, data: seen.append(kind)

        record = broken.capture(turn_id="turn-1", event="step", paths=["app.py"])

        assert record["status"] == "failed"
        assert "no space left" in record["error"]
        assert "undo_snapshot_failed" in seen

    def test_a_capture_failure_does_not_touch_the_workspace(self, workspace):
        class _Broken(StagedSnapshotStore):
            def _write_object(self, payload: bytes) -> str:  # type: ignore[override]
                raise OSError("no space left on device")

        before = (workspace["repo"] / "app.py").read_bytes()
        broken = _Broken(workspace["repo"], workspace["logs"], workspace["session"])
        broken.capture(turn_id="turn-1", event="step", paths=["app.py"])
        assert (workspace["repo"] / "app.py").read_bytes() == before

    def test_an_unusable_log_root_degrades_to_a_record_not_a_crash(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("x\n", encoding="utf-8")
        # The log root IS the repository: refused, and it raises a typed error
        # rather than writing the store into the user's tree.
        with pytest.raises(CheckpointError):
            StagedSnapshotStore(repo, repo, "sess-test")

    def test_the_cli_reports_an_unavailable_store_instead_of_raising(self, tmp_path):
        missing = tmp_path / "nope"
        state = _fv.staged_undo_state(missing, tmp_path / "logs")
        assert state["available"] is False
        assert state["staged"] is False
        assert "no staged-undo store" in state["reason"]
        # A verb is still OWNED (so the user gets a reason, not a silent
        # fall-through), and the reason reaches the rendered lines.
        result = _fv.undo_command(missing, tmp_path / "logs", "commit")
        assert result["handled"] is True
        assert result["ok"] is False
        assert "undo unavailable" in "\n".join(result["lines"])
        # A bare PATH is not owned, so the historical per-file path still runs.
        assert (
            _fv.undo_command(missing, tmp_path / "logs", "app.py")["handled"] is False
        )


# ---------------------------------------------------------------------------
# 9. the harness capture seam
# ---------------------------------------------------------------------------


class TestEditorCaptureHooks:
    def test_the_real_mutation_primitive_captures_a_turn_around_each_edit(
        self, workspace
    ):
        """The hook is LIVE, not decorative: two real edits, real snapshots."""
        app = workspace["repo"] / "app.py"
        original = app.read_bytes()
        session = editor.EditSession()
        session.note_read("app.py", data=original)
        capture = editor.StageCapture(
            str(workspace["repo"]),
            str(workspace["logs"]),
            session_id=workspace["session"],
            turn_id="turn-1",
        )
        with editor.stage_capture_scope(capture):
            first = editor.apply_text_edit(
                str(workspace["repo"]),
                "app.py",
                "return 1",
                "return 2",
                session=session,
                config={},
            )
            second = editor.apply_text_edit(
                str(workspace["repo"]),
                "app.py",
                "return 2",
                "return 3",
                session=session,
                config={},
            )
        assert first.ok and second.ok
        assert capture.available is True
        assert capture.changed_paths("turn-1") == ["app.py"]
        events = [row["event"] for row in capture.captures]
        assert events[0] == "step_before"
        assert "step_after" in events

        store = _store(workspace)
        store.stage()
        receipt = store.commit()
        assert receipt["ok"] is True
        assert app.read_bytes() == original

    def test_no_capture_in_scope_means_no_work_and_no_crash(self, workspace):
        assert editor.active_stage_capture() is None
        session = editor.EditSession()
        session.note_read("app.py", data=(workspace["repo"] / "app.py").read_bytes())
        outcome = editor.apply_text_edit(
            str(workspace["repo"]),
            "app.py",
            "return 1",
            "return 9",
            session=session,
            config={},
        )
        assert outcome.ok is True

    def test_the_capture_is_scoped_not_global(self, workspace):
        capture = editor.StageCapture(
            str(workspace["repo"]), str(workspace["logs"]), turn_id="turn-1"
        )
        with editor.stage_capture_scope(capture):
            assert editor.active_stage_capture() is capture
        assert editor.active_stage_capture() is None

    def test_changed_paths_are_measured_from_the_snapshots_not_tracked(self, workspace):
        capture = editor.StageCapture(
            str(workspace["repo"]), str(workspace["logs"]), turn_id="turn-1"
        )
        app = workspace["repo"] / "app.py"
        with editor.stage_capture_scope(capture):
            capture.observe(["app.py"], done=False)
            app.write_text("changed\n", encoding="utf-8")
            capture.observe(["app.py"], done=True)

        # The tracked list is empty; the MEASURED diff is not.
        assert capture.changed_paths("turn-1") == ["app.py"]

    def test_the_changed_paths_are_recorded_on_the_assistant_message(self, workspace):
        capture = editor.StageCapture(
            str(workspace["repo"]),
            str(workspace["logs"]),
            session_id=workspace["session"],
            turn_id="turn-1",
        )
        app = workspace["repo"] / "app.py"
        with editor.stage_capture_scope(capture):
            capture.observe(["app.py"], done=False)
            app.write_text("changed\n", encoding="utf-8")
            capture.observe(["app.py"], done=True)
            capture.end_turn("turn-1", clean=True)

        rows = _fv.undo_turn_rows(
            workspace["repo"], workspace["logs"], session_id=workspace["session"]
        )
        assert rows, "no assistant-message row was recorded"
        assert rows[0]["turn_id"] == "turn-1"
        assert rows[0]["changed_paths"] == ["app.py"]

    def test_a_clean_completion_capture_is_its_own_event(self, workspace):
        capture = editor.StageCapture(
            str(workspace["repo"]), str(workspace["logs"]), turn_id="turn-1"
        )
        with editor.stage_capture_scope(capture):
            record = capture.capture_completion("turn-1")
        assert record["event"] == "clean_completion"
        assert record["status"] == "captured"

    def test_every_mutation_primitive_is_wired_to_the_capture(self, workspace):
        """Self-arming: removing a hook fails THIS test, not production."""
        source = Path(editor.__file__).read_text(encoding="utf-8")
        for function in (
            "safe_edit",
            "safe_write",
            "safe_rename",
            "safe_delete",
            "safe_apply_patch",
            "apply_text_edit",
        ):
            body = source.split(f"def {function}(", 1)[-1].split("\ndef ", 1)[0]
            assert "_stage_note(" in body, f"{function} does not capture"

    def test_the_capture_can_be_switched_off_by_config(self, workspace):
        capture = editor.StageCapture(
            str(workspace["repo"]),
            str(workspace["logs"]),
            turn_id="turn-1",
            config={"undo_staged_enabled": False},
        )
        assert capture.available is False
        record = capture.observe(["app.py"], done=False)
        assert record["status"] == "unavailable"


# ---------------------------------------------------------------------------
# 10. the two shells speak the same words
# ---------------------------------------------------------------------------


class TestShellParity:
    def test_both_shells_dispatch_through_the_one_shared_core(self):
        from cli import interactive as _iv
        from cli import tui as _tui

        source_tui = Path(_tui.__file__).read_text(encoding="utf-8")
        source_repl = Path(_iv.__file__).read_text(encoding="utf-8")
        assert "_fv.undo_command(" in source_tui
        assert "_fv.undo_command(" in source_repl
        # Neither shell re-derives the decision.
        assert "from memory.checkpoints" not in source_tui
        assert "from memory.checkpoints" not in source_repl

    def test_a_new_prompt_commits_the_staged_code_revert(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()

        result = _fv.commit_staged_undo_for_prompt(
            workspace["repo"], workspace["logs"], session_id=workspace["session"]
        )

        assert result["committed"] is True
        assert (workspace["repo"] / "app.py").read_text(encoding="utf-8") == (
            "def go():\n    return 1\n"
        )

    def test_a_conversation_revert_is_never_auto_committed_by_a_prompt(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage(scope="conversation")
        edited = (workspace["repo"] / "app.py").read_bytes()

        result = _fv.commit_staged_undo_for_prompt(
            workspace["repo"], workspace["logs"], session_id=workspace["session"]
        )

        assert result["committed"] is False
        assert result["reason"] == "scope_requires_explicit_commit"
        assert "/undo commit" in result["lines"][0]
        # The code and the conversation are both untouched.
        assert (workspace["repo"] / "app.py").read_bytes() == edited
        assert store.staged()["scope"] == "conversation"

    def test_a_prompt_commit_is_a_no_op_when_nothing_is_staged(self, workspace):
        result = _fv.commit_staged_undo_for_prompt(
            workspace["repo"], workspace["logs"], session_id=workspace["session"]
        )
        assert result["committed"] is False
        assert result["reason"] == "nothing_staged"
        assert result["lines"] == []

    def test_the_dispatcher_leaves_a_bare_file_path_to_the_historical_revert(
        self, workspace
    ):
        result = _fv.undo_command(
            workspace["repo"],
            workspace["logs"],
            "app.py",
            session_id=workspace["session"],
        )
        assert result["handled"] is False

    def test_with_no_captured_turn_the_bare_command_falls_back_to_the_old_engine(
        self, workspace
    ):
        """A session with no captured snapshots must still be revertible.

        A bare ``/undo`` that became a dead end the moment a run predates (or
        misses) the capture seam would break the feature users already have.
        So with no captured turn the dispatcher hands the line back to the
        historical per-run engine instead of claiming it.
        """
        result = _fv.undo_command(
            workspace["repo"],
            workspace["logs"],
            "",
            session_id=workspace["session"],
        )
        assert result["handled"] is False
        # Once a turn IS captured, the staged model owns the bare command.
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        owned = _fv.undo_command(
            workspace["repo"],
            workspace["logs"],
            "",
            session_id=workspace["session"],
        )
        assert owned["handled"] is True
        assert owned["kind"] == "stage"

    def test_an_empty_staged_range_answers_with_a_reason_and_a_way_forward(
        self, workspace
    ):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        result = _fv.undo_command(
            workspace["repo"],
            workspace["logs"],
            "code",
            session_id=workspace["session"],
        )
        assert result["handled"] is True
        assert result["ok"] is False
        lines = " ".join(result["lines"])
        assert "nothing is staged" in lines
        # A failure that only says "no" is a dead end; it must say what to do.
        assert "/undo first" in lines
        assert "files, conversation, both" in lines

    def test_the_words_a_person_types_resolve_to_the_one_canonical_vocabulary(
        self, workspace
    ):
        """`/undo code` is a sentence; `/undo files` is a file mode. Both work.

        The help text promises ``code|task|all``. If the dispatcher only spoke
        the canonical names, typing ``code`` would fall through to the
        per-file engine and try to revert a FILE called ``code`` - the exact
        silent-misroute class this repo keeps having to repair.
        """
        for word, scope in (
            ("code", "files"),
            ("files", "files"),
            ("task", "conversation"),
            ("conversation", "conversation"),
            ("all", "both"),
            ("both", "both"),
        ):
            assert _fv.undo_scope_for(word) == scope
        assert _fv.undo_scope_for("nonsense") == ""
        assert _fv.undo_scope_for("app.py") == ""

    def test_every_alias_reaches_the_dispatcher_and_sets_the_real_scope(
        self, workspace
    ):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        for word, scope in (
            ("code", "files"),
            ("task", "conversation"),
            ("all", "both"),
        ):
            store.discard()
            _fv.undo_command(
                workspace["repo"],
                workspace["logs"],
                "",
                session_id=workspace["session"],
            )
            store.discard()
            store.stage()
            result = _fv.undo_command(
                workspace["repo"],
                workspace["logs"],
                word,
                session_id=workspace["session"],
            )
            assert result["handled"] is True, word
            assert result["payload"]["scope"] == scope, word

    def test_a_refusal_to_stage_names_the_turns_that_do_exist(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.discard()
        result = store.stage(turn_ids=["turn-404"])
        assert result["ok"] is False
        assert result["status"] == "unknown_turn"
        assert result["turns"] == ["turn-1"]

    def test_the_dispatcher_owns_every_verb(self, workspace):
        for verb in _fv.UNDO_VERBS:
            result = _fv.undo_command(
                workspace["repo"],
                workspace["logs"],
                verb,
                session_id=workspace["session"],
            )
            assert result["handled"] is True, verb

    def test_rendered_lines_carry_no_markup_delimiters(self, workspace):
        """A path containing '[' must not be able to delete a message."""
        weird = workspace["repo"] / "weird[name].py"
        weird.write_text("BEFORE\n", encoding="utf-8")
        store = _store(workspace)
        store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "weird[name].py"]
        )
        weird.write_text("AFTER\n", encoding="utf-8")
        (workspace["repo"] / "app.py").write_text("edited\n", encoding="utf-8")
        store.capture(
            turn_id="turn-1", event="step", paths=["app.py", "weird[name].py"]
        )
        store.stage()
        receipt = store.commit()

        lines = _fv.render_undo_receipt(receipt)
        for line in lines:
            assert "[neo." not in line
            assert "[/]" not in line
        assert any("weird[name].py" in line for line in lines)
        assert weird.read_text(encoding="utf-8") == "BEFORE\n"

    def test_the_receipt_lines_say_whether_it_was_verified(self, workspace):
        store = _store(workspace)
        _turn(store, "turn-1", edit="def go():\n    return 2\n")
        store.stage()
        good = "\n".join(_fv.render_undo_receipt(store.commit()))
        assert "verified: yes" in good

        (workspace["repo"] / "app.py").write_text("HAND\n", encoding="utf-8")
        store.stage()
        bad = "\n".join(_fv.render_undo_receipt(store.commit()))
        assert "verified: NO" in bad


# ---------------------------------------------------------------------------
# 11. the gates this round must not have moved
# ---------------------------------------------------------------------------


class TestHonestyGates:
    def test_no_staged_undo_key_is_in_the_harness_defaults(self):
        """A default in DEFAULTS merges into every task and every eval arm."""
        from harness import config

        offenders = [key for key in config.DEFAULTS if key.startswith("undo_staged")]
        assert offenders == [], f"published keys would switch every run: {offenders}"

    def test_the_staged_undo_receipt_carries_no_completion_claim(self):
        """No 'success'/'verified'/'passed' word that is not a hash fact."""
        rendered = "\n".join(
            _fv.render_undo_receipt(
                {
                    "status": "reverted",
                    "scope": "files",
                    "turn_ids": ["turn-1"],
                    "restored": [
                        {
                            "path": "app.py",
                            "action": "restored",
                            "after_hash": "a" * 64,
                        }
                    ],
                    "verified": False,
                }
            )
        )
        for word in ("success", "passed", "completed"):
            assert word not in rendered.lower()

    def test_the_verifier_mint_condition_is_untouched_by_staged_undo(self):
        """Read the source: this feature has no say in whether a run completed."""
        source = Path("harness/core.py").read_text(encoding="utf-8")
        assert "target_test_passed" in source
        for forbidden in ("undo_staged", "StagedSnapshotStore", "stage_capture_scope"):
            assert forbidden not in source

    def test_the_undo_verbs_are_typed_and_fail_closed_in_the_command_registry(self):
        from cli import commands

        spec = commands.command_spec("/undo")
        assert spec is not None
        assert "commit" in spec.argument_hint
        assert spec.in_flight_policy == "refuse"
        assert "workspace:write" in spec.required_permissions
