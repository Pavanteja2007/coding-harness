"""P3a/W2 T3 â€” history survives, resume is exact, the cost is honest.

This module is the pin suite for Wave 2. It is **in-package** (``runtime/``),
which means a bare ``python -m pytest`` collects none of it -- ``pyproject.toml``
pins ``testpaths = ["tests"]`` -- so run it explicitly::

    python -m pytest runtime/test_p3w2_pins.py -q -p no:randomly

Host-only: no Docker, no provider, no network, no credential. Every session is a
real ``cli.session`` session, every rotation is the real
``runtime.session_lineage.rotate_session``, every tree is the real
``runtime.session_tree.SessionTree``. **Nothing here is a stand-in**, because an
audit of a stand-in proves the stand-in survives.

Three of these pins are **weakening demonstrations**: they apply the literal
one-line edit that would restore a defect, to an isolated COPY imported under its
own module name, and require both colours in one run. A copy rather than an
in-place edit because four terminals share this tree and a process death
mid-test would leave the live module weakened. The helper **raises if the
literal is absent**, because an edit that silently matches nothing would import
an unchanged copy and report the opposite of the finding.

What this round found, all of it in ``runtime/**`` and all of it closed:

1. A rewind DELETED pre-existing files. ``TurnFileState.capture`` recorded any
   read failure -- and any file over ``max_file_bytes`` -- as ``exists: false``,
   and ``exists: false`` meant DELETE on restore. The oversize case was
   DETERMINISTIC: a run that merely edits a >2 MB file and rewinds destroys it.
2. The GC MOVED the head. ``default_roots()`` never retained ``head`` marker
   rows, so collecting one deleted the record of a rewind and the head silently
   fell back to an earlier position.
3. A concurrent reader of a tree being collected saw NO history and
   ``verify()`` reported ``ok: True``, because ``_rewrite`` rotated the journal
   aside before writing its replacement.
4. The GC's receipt claimed removals that had not happened.
5. ``resume_state`` refused EVERY real session, so resume across rotation was
   unreachable while the check that was unsatisfiable was the one guarding it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from runtime import session_cost, session_durability
from runtime.background_jobs import BackgroundJobs, JobRefused, idle_admission
from runtime.checkpoint import PRE_IMAGE_STATES, TurnFileState
from runtime.session_durability import (
    HISTORY_VECTORS,
    OUTCOME_CLASSES,
    ReadOnlySessionView,
    history_audit,
    job_write_separation,
    read_only_view,
)
from runtime.session_lineage import (
    LINEAGE_IDENTITY_FIELDS,
    LineageRefused,
    delete_session,
    deletion_ledger,
    publish_live_sessions,
    resume_against_checkpoint,
    resume_state,
)
from runtime.session_tree import JOURNAL_STATES, SessionTree

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / "runtime"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path):
    """A repository + run directory pair, with real files to lose."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"repo": repo, "run": tmp_path / "run", "root": tmp_path}


def _reader(repo: Path):
    def read(relative: str) -> Optional[bytes]:
        path = repo / relative
        return path.read_bytes() if path.is_file() else None

    return read


def _writer(repo: Path):
    def write(relative: str, data: Optional[bytes]) -> None:
        path = repo / relative
        if data is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(data))

    return write


def _lineage(tmp_path: Path, *, turns: int = 8, rotations: int = 2, name: str = "lin"):
    return session_durability.build_fixture(
        tmp_path / name, turns=turns, rotations=rotations, keep_last=2
    )


#: Functions permitted to call a destructive filesystem method, each with the
#: reason. ``_rewrite`` unlinks its OWN staging temp file when the atomic install
#: fails, and ``_main`` removes the ``tempfile.mkdtemp()`` workspace its own
#: measurement built -- neither touches history. An allowlist with a stated reason
#: per row is enforceable where a bare "no unlink anywhere" is not, because that
#: version forbids cleaning up after yourself, which is how a guard gets switched
#: off.
PERMITTED_DELETERS: Dict[str, Dict[str, str]] = {
    "session_tree.py": {
        "_rewrite": "unlinks its OWN entries.jsonl.compact-* staging file when the "
        "atomic install fails; never a journal, never an entry",
        "_main": "removes the tempfile.mkdtemp() workspace its own measurement built",
    },
    "session_lineage.py": {
        "delete_session": "THE explicit, audited deletion -- confirmed by name, "
        "with a reason, and ledgered outside the deleted directory",
    },
}


# ===========================================================================
# 1. THE THREE PRE-EXISTING DATA-LOSS PATHS (the weakening demonstrations)
# ===========================================================================


def _weakened_copy(tmp_path: Path, filename: str, old: str, new: str, module_name: str):
    """Import a COPY of ``filename`` with ``old`` replaced by ``new``.

    The copy is registered as ``runtime.<module_name>`` rather than as a
    top-level module, because these files use RELATIVE imports
    (``from .fsutil import ...``) and a top-level copy raises
    ``ImportError: attempted relative import with no known parent package`` --
    which reads like a broken demonstration rather than a wiring mistake.

    Raises when ``old`` is absent. An edit matching nothing would import an
    unchanged copy and the demonstration would report the opposite of the
    finding -- the exact failure mode T2's adversarial module documents.
    """
    source = (RUNTIME_DIR / filename).read_text(encoding="utf-8")
    if old not in source:
        raise AssertionError(
            f"the literal to weaken is not present in {filename}: {old!r}. An edit "
            "matching nothing would import an unchanged copy and report the "
            "opposite of this finding."
        )
    target = RUNTIME_DIR / f"_w2_weakened_{module_name}.py"
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    qualified = f"runtime.{module_name}"
    spec = importlib.util.spec_from_file_location(qualified, target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(qualified, None)
        try:
            target.unlink()
        except OSError:
            pass
    return module


class TestThePreImageDeleteIsClosed:
    """T2 filed this as the highest-severity item in the tree. It was real."""

    def test_an_unreadable_pre_image_must_not_delete_a_pre_existing_file(
        self, store
    ) -> None:
        repo = store["repo"]
        (repo / "app.py").write_bytes(b"value = 42\n")

        def _unreadable(_relative: str) -> bytes:
            raise OSError("simulated read failure")

        state = TurnFileState(
            store["run"], read_bytes=_unreadable, write_bytes=_writer(repo)
        )
        assert state.capture(2, ["app.py"]) == 1
        receipt = state.rewind(2, tracked=["app.py"])
        assert receipt["deleted"] == [], (
            "a pre-image that could not be READ was treated as proof the file did "
            "not exist, and the rewind DELETED a pre-existing file"
        )
        assert (repo / "app.py").is_file(), "the user's file was destroyed"

    def test_a_file_over_the_cap_must_not_be_deleted_either(self, store) -> None:
        """**The deterministic trigger.** No race, no shared machine, no setup."""
        repo = store["repo"]
        (repo / "big.bin").write_bytes(b"x" * 5_000_000)
        state = TurnFileState(
            store["run"],
            read_bytes=_reader(repo),
            write_bytes=_writer(repo),
            max_file_bytes=2_000_000,
        )
        assert state.capture(2, ["big.bin"]) == 1
        receipt = state.rewind(2, tracked=["big.bin"])
        assert receipt["deleted"] == [], (
            "a file over max_file_bytes was recorded ABSENT rather than "
            "UNREADABLE, so a rewind deleted a file the user has. Any run that "
            "edits a large file and rewinds loses it -- no unusual setup needed."
        )
        assert (repo / "big.bin").is_file()
        assert receipt["pre_image_unreadable"] == ["big.bin"]
        assert receipt["exact"] is False, (
            "a rewind that could not read a pre-image is NOT exact, and the "
            "receipt has to say so rather than reporting a clean restore"
        )

    def test_a_genuinely_created_file_is_still_deleted(self, store) -> None:
        """The CONTROL ARM. If absent stopped meaning absent, nothing is safe."""
        repo = store["repo"]
        state = TurnFileState(
            store["run"], read_bytes=_reader(repo), write_bytes=_writer(repo)
        )
        state.capture(2, ["new.txt"])
        (repo / "new.txt").write_bytes(b"made by the run\n")
        found, data = state.pre_image("new.txt", 2)
        assert found is True and data is None, (
            "a file the run CREATED must record absence, or the rewind would write "
            "an empty file instead of removing it"
        )
        receipt = state.rewind(2, tracked=["new.txt"])
        assert receipt["deleted"] == ["new.txt"]
        assert not (repo / "new.txt").exists()

    def test_there_are_exactly_three_pre_image_states(self) -> None:
        assert PRE_IMAGE_STATES == ("stored", "absent", "unreadable"), (
            "a fourth state, or a rename, changes what authorises a delete; the "
            "vocabulary is the guarantee"
        )

    def test_an_unrecognised_state_is_refused_rather_than_defaulted_to_absent(
        self, store, tmp_path
    ) -> None:
        """A state this module does not understand must not become a DELETE."""
        repo = store["repo"]
        (repo / "app.py").write_bytes(b"value = 1\n")
        run = store["run"]
        state = TurnFileState(
            run, read_bytes=_reader(repo), write_bytes=_writer(repo)
        )
        state.capture(2, ["app.py"])
        import hashlib

        digest = hashlib.sha256(b"app.py").hexdigest()
        index = run / "turn-files" / "000002" / f"{digest}.json"
        rows = [json.loads(line) for line in index.read_text().splitlines() if line]
        rows[0]["state"] = "some_future_engine_state"
        index.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        found, data = state.pre_image("app.py", 2)
        assert found is False and data is None, (
            "an unknown state must read as 'not found' so the caller falls back "
            "to pristine; defaulting it to absent IS the delete defect"
        )
        assert any("not one of" in warning for warning in state.warnings)

    def test_the_delete_defect_is_detectable_by_a_weakened_copy(self, tmp_path) -> None:
        """RED and GREEN in one run, from the literal pre-fix line.

        The literal weakened is ``state = "unreadable"`` -> ``state = "absent"``
        in ``capture``, NOT the ``pre_image`` guard. The first version of this
        demonstration removed the ``pre_image`` branch instead and reported "the
        weakening is undetectable" -- which was true and was a fact about the
        DEMONSTRATION: with that branch gone, ``pre_image`` still falls through
        to the blob read, the blob is missing, ``OSError`` is caught, and the
        caller still gets ``found=False``. The defect has two layers and the
        RECORDING is the load-bearing one.
        """
        weakened = _weakened_copy(
            tmp_path,
            "checkpoint.py",
            '                state = "unreadable"\n                reason = f"{type(exc).__name__}: {exc}"',
            '                state = "absent"\n                reason = ""',
            "checkpoint_weakened_w2",
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_bytes(b"value = 42\n")

        def _unreadable(_relative: str) -> bytes:
            raise OSError("simulated read failure")

        state = weakened.TurnFileState(
            tmp_path / "run", read_bytes=_unreadable, write_bytes=_writer(repo)
        )
        state.capture(2, ["app.py"])
        receipt = state.rewind(2, tracked=["app.py"])
        assert receipt["deleted"] == ["app.py"], (
            "recording an unreadable pre-image as `absent` restores the DELETE. "
            "The shipped module must refuse it; this copy must not."
        )
        assert not (repo / "app.py").exists()


class TestTheGcCannotMoveTheHead:
    """Row 6 plus a GC: a rewind the user asked for must survive collection."""

    def test_a_head_marker_is_a_retention_root(self, tmp_path) -> None:
        tree = SessionTree(tmp_path / "tree")
        for index in range(8):
            tree.advance(turn_id=f"t{index}", text="x" * 64)
        target = [item for item in tree.entries() if item.kind == "turn"][3].id
        before = str(tree.head_id())
        moved = tree.move_head(target, reason="go back to turn 3")
        assert moved is not None
        assert str(tree.head_id()) == target

        aged = SessionTree(tmp_path / "tree", clock=lambda: time.time() + 100_000.0)
        receipt = aged.gc(min_age_s=-1.0)
        assert int(receipt["removed"]) > 0, (
            "the fixture must leave something collectable or this pin is vacuous"
        )
        after = SessionTree(tmp_path / "tree")
        assert str(after.head_id()) == target, (
            f"the GC MOVED the head from {before} to {after.head_id()}. Collecting "
            "the `head` marker deleted the RECORD of the user's rewind, and the "
            "head silently fell back to an earlier position -- with verify() "
            "reporting the tree intact, because the journal really was intact."
        )
        assert after.verify()["complete"] is True

    def test_a_deliberate_fork_is_never_collected(self, tmp_path) -> None:
        tree = SessionTree(tmp_path / "tree")
        for index in range(8):
            tree.advance(turn_id=f"t{index}", text="x" * 64)
        fork = tree.fork(label="a fork the user made on purpose")
        assert fork is not None
        aged = SessionTree(tmp_path / "tree", clock=lambda: time.time() + 100_000.0)
        aged.gc(min_age_s=-1.0)
        after = SessionTree(tmp_path / "tree")
        assert after.entry(str(fork["entry_id"])) is not None, (
            "a fork somebody created on purpose was collected. That is the "
            "difference between a GC and data loss."
        )

    def test_the_removed_claim_is_only_filled_in_after_the_install(
        self, tmp_path, monkeypatch
    ) -> None:
        """A GC that collected nothing must not report that it collected."""
        tree = SessionTree(tmp_path / "tree")
        for index in range(8):
            tree.advance(turn_id=f"t{index}", text="x" * 64)
        tree.fork(label="f")
        aged = SessionTree(tmp_path / "tree", clock=lambda: time.time() + 100_000.0)
        plan = aged.gc(dry_run=True, min_age_s=-1.0)
        assert int(plan["collectable_count"]) > 0
        assert plan["removed"] == 0, "a DRY RUN removed something"
        monkeypatch.setattr(
            SessionTree, "_rewrite", lambda self, entries: None, raising=True
        )
        receipt = aged.gc(min_age_s=-1.0)
        assert int(receipt["collectable_count"]) > 0
        assert int(receipt["removed"]) == 0, (
            f"the receipt reported removed={receipt['removed']} while the rewrite "
            "was refused and nothing was collected. A storage-pressure GC that "
            "quietly collects nothing while claiming it collected everything is "
            "worse than no GC."
        )
        assert receipt["reason"] == "rewrite_refused_nothing_removed"
        assert receipt["error"], "a refusal must name its cause"


class TestAConcurrentReaderNeverSeesAnEmptyTree:
    """Row 4 of the extraction contract, on the tree itself."""

    def test_verify_refuses_to_call_an_absent_journal_intact(self, tmp_path) -> None:
        receipt = SessionTree(tmp_path / "nothing-here").verify()
        assert receipt["ok"] is True, (
            "`ok` is about tampering and stays True -- that is a true statement "
            "about a useless situation"
        )
        assert receipt["complete"] is False, (
            "a journal that does not exist is not a complete read, and `ok: True` "
            "alone would tell a reader its history verified when there is none"
        )
        assert receipt["state"] == "absent"
        assert receipt["reason"]

    def test_the_journal_states_are_distinguishable(self) -> None:
        assert set(JOURNAL_STATES) == {"present", "absent", "unreadable", "empty"}

    def test_a_reader_during_a_collection_never_sees_zero_entries(self, tmp_path) -> None:
        import threading

        root = tmp_path / "tree"
        tree = SessionTree(root)
        for index in range(30):
            tree.advance(turn_id=f"t{index}", text="x" * 100)
        tree.fork(label="keep me")
        tree.advance(turn_id="alt", text="alt")
        turns = [item for item in tree.entries() if item.kind == "turn"]
        tree.move_head(turns[0].id, reason="rewind")

        seen: List[Tuple[int, bool, bool]] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                view = SessionTree(root).consistent_read()
                seen.append((int(view["entries"]), bool(view["complete"]), bool(view["head_present"])))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.05)
        SessionTree(root, clock=lambda: time.time() + 100_000.0).gc(min_age_s=-1.0)
        time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)

        assert seen, "the reader thread never ran, so nothing was measured"
        empty = [row for row in seen if row[0] == 0]
        headless = [row for row in seen if not row[2]]
        ok_empty = [row for row in seen if row[0] == 0 and row[1]]
        assert not empty, (
            f"{len(empty)} of {len(seen)} concurrent reads saw ZERO entries while a "
            "collection was running. A background extraction job reading a session "
            "during GC was told it had a healthy tree containing no conversation."
        )
        assert not headless, f"{len(headless)} reads saw no head at all"
        assert not ok_empty

    def test_the_collection_installs_the_new_journal_atomically(self, tmp_path) -> None:
        """A stale staging file would mean the install is not a single replace."""
        tree = SessionTree(tmp_path / "tree")
        for index in range(8):
            tree.advance(turn_id=f"t{index}", text="x" * 32)
        tree.fork(label="f")
        tree.advance(turn_id="alt", text="a")
        turns = [item for item in tree.entries() if item.kind == "turn"]
        tree.move_head(turns[0].id, reason="rewind")
        aged = SessionTree(tmp_path / "tree", clock=lambda: time.time() + 100_000.0)
        aged.gc(min_age_s=-1.0)
        root = tmp_path / "tree"
        staging = sorted(root.glob("entries.jsonl.compact-*"))
        assert not staging, f"a staging file survived the install: {staging}"
        rotated = sorted(root.glob("entries.jsonl.gc-*"))
        assert rotated, "the previous journal was not preserved as evidence"
        assert rotated[0].stat().st_size > 0


# ===========================================================================
# 2. THE EIGHT HISTORY VECTORS
# ===========================================================================


class TestTheHistoryAudit:
    @pytest.fixture(scope="class")
    def audit(self) -> Dict[str, Any]:
        return history_audit()

    def test_all_eight_vectors_are_declared_as_data(self) -> None:
        assert len(HISTORY_VECTORS) == 8
        assert [row.number for row in HISTORY_VECTORS] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert len({row.name for row in HISTORY_VECTORS}) == 8

    def test_exactly_one_vector_is_allowed_to_lose_history(self) -> None:
        allowed = [row.name for row in HISTORY_VECTORS if row.loss_allowed]
        assert allowed == ["explicit_delete"], (
            "'only an explicit deletion loses history' is the claim. If a second "
            f"vector is marked loss-allowed the claim has been quietly widened: {allowed}"
        )

    def test_every_declared_outcome_class_is_used_by_the_audit(self, audit) -> None:
        outcomes = {row["outcome"] for row in audit["vectors"]}
        assert outcomes <= set(OUTCOME_CLASSES) | {"not_attempted"}
        assert "not_attempted" not in outcomes, (
            f"a vector did not run: {audit['not_attempted']}. An unrun vector is "
            "not a passed vector."
        )

    def test_every_vector_was_attempted_and_reached_its_required_outcome(
        self, audit
    ) -> None:
        assert audit["attempted"] == audit["declared"] == 8
        for row in audit["vectors"]:
            assert row["ok"], (
                f"vector {row['number']} ({row['name']}) did not hold: "
                f"{row['outcome']} -- {row['detail']}"
            )

    def test_no_history_was_destroyed_implicitly(self, audit) -> None:
        assert audit["history_destroyed_implicitly"] == []
        assert audit["only_explicit_loss"] is True

    def test_row_one_kept_every_ancestor(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 1)
        assert row["evidence"]["generations"] >= 8, row["detail"]
        assert row["evidence"]["turns_lost"] == []
        assert row["evidence"]["unreadable_generations"] == []

    def test_row_two_resumed_the_tip_onto_its_own_point_and_compacted_again(
        self, audit
    ) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 2)
        assert row["evidence"]["resume_authorised"] is True
        assert row["evidence"]["landed_on_its_own_point"] is True, (
            "a resume that lands on a DIFFERENT session is not a resume; it is a "
            "silent continuation of somebody else's conversation"
        )
        assert row["evidence"]["reached_root"] is True
        assert row["evidence"]["second_compaction_ok"] is True
        assert row["evidence"]["turns_lost"] == []

    def test_row_three_collected_and_kept_every_live_position(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 3)
        assert row["evidence"]["collectable"] > 0, (
            "the GC had nothing to collect, so this vector proves nothing"
        )
        assert row["evidence"]["gc_removed"] > 0
        assert row["evidence"]["forks_kept"] >= 1
        assert row["evidence"]["live_positions_lost"] == []
        assert row["evidence"]["generations_unreadable"] == []
        assert row["evidence"]["journal_complete_after"] is True

    def test_row_four_holds_under_simulated_storage_pressure(self, audit) -> None:
        """The row that bites in production: nobody chose to run this pass."""
        row = next(r for r in audit["vectors"] if r["number"] == 4)
        assert "storage pressure" in str(row["evidence"]["trigger"])
        assert row["evidence"]["budget_bytes"] == session_durability.DEFAULT_STORAGE_PRESSURE_BYTES
        assert row["evidence"]["collectable"] > 0
        assert row["evidence"]["gc_removed"] > 0, (
            "the pressure-driven pass collected nothing, so it cannot have "
            "demonstrated that pressure does not change correctness"
        )
        assert row["evidence"]["live_positions_lost"] == []
        assert row["evidence"]["turns_lost"] == []

    def test_pressure_and_deliberate_gc_reach_the_same_verdict(self, audit) -> None:
        rows = {row["number"]: row for row in audit["vectors"]}
        deliberate, pressure = rows[3], rows[4]
        assert deliberate["outcome"] == pressure["outcome"], (
            "storage pressure changed the OUTCOME. The whole claim is that the "
            "collection predicate is the same and only the trigger differs."
        )
        assert pressure["evidence"]["live_positions_lost"] == (
            deliberate["evidence"]["live_positions_lost"]
        )

    def test_row_five_kept_the_abandoned_branch_and_resolved_its_evidence(
        self, audit
    ) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 5)
        assert row["evidence"]["branch_readable"] is True
        assert row["evidence"]["abandoned_turns_readable"] is True
        assert row["evidence"]["evidence_resolvable"] is True
        assert row["evidence"]["summary_is_claim"] is True
        assert row["evidence"]["dangling_evidence"] == []

    def test_row_six_moved_the_head_and_lost_nothing(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 6)
        assert row["evidence"]["head_moved"] is True, (
            "if the head did not move, 'nothing was lost' is trivially true"
        )
        assert row["evidence"]["entries_lost"] == []
        assert row["evidence"]["pre_compaction_text_readable"] is True

    def test_row_seven_restored_an_old_id_to_its_own_generation(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 7)
        assert row["evidence"]["is_the_tip"] is False
        assert row["evidence"]["ancestry_matches"] is True
        assert row["evidence"]["names_its_own_successor"] is True

    def test_row_eight_refused_every_under_specified_deletion(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 8)
        assert row["outcome"] == "survived_explicit", (
            "row 8 is the only vector allowed to lose history, and it must reach "
            "that class -- never `survived` (nothing happened) and never "
            "`destroyed` (it happened without asking)"
        )
        assert row["evidence"]["all_under_specified_refused"] is True
        assert row["evidence"]["refusals_destroyed_nothing"] is True
        assert len(row["evidence"]["refusals"]) == 4
        assert row["evidence"]["now_unreadable"] is True
        assert row["evidence"]["bytes_destroyed"] > 0, (
            "the deletion destroyed no bytes, so it proves the destruction is "
            "real rather than a rename"
        )

    def test_the_deletion_ledger_outlives_the_deletion(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 8)
        assert row["evidence"]["ledger_outside_the_deleted_directory"] is True, (
            "the ledger lives inside the thing it records, so the first deletion "
            "destroys the record of itself"
        )
        assert row["evidence"]["ledger_rows"] == 1

    def test_the_ancestry_break_is_reported_not_silent(self, audit) -> None:
        row = next(r for r in audit["vectors"] if r["number"] == 8)
        assert row["evidence"]["orphaned_successors"], (
            "deleting a middle generation breaks its descendants' ancestry walk"
        )
        assert row["evidence"]["ancestry_break_reported"] is True, (
            "the break has to be REPORTED; a descendant whose ancestry silently "
            "stops is a reader looking for a conversation that is no longer there"
        )
        assert row["evidence"]["generations_broken"] == [], (
            "deleting a middle generation breaks its DESCENDANTS' ancestry walk, "
            "not their own records -- the control arm is that `orphaned_successors` "
            "is non-empty and `ancestry_break_reported` is True, both asserted "
            "above"
        )

    def test_every_vector_is_given_its_own_fixture(self, audit) -> None:
        assert audit["fixture"]["isolated_fixtures"] == 8, (
            "sharing one fixture between vectors makes each row a measurement of "
            "the previous row's leftovers -- three of this round's own first-pass "
            "reds were pure artefact of that"
        )


class TestDeletionIsTheOnlyWayToLoseHistory:
    def test_no_confirm_is_refused(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        before = session_durability.fixture_turn_ids(
            fixture["log_root"], fixture["chain"], repo=fixture["repo"]
        )
        with pytest.raises(LineageRefused) as excinfo:
            delete_session(fixture["log_root"], fixture["chain"][1], why="asked")
        assert "confirm" in excinfo.value.reason
        assert session_durability.fixture_turn_ids(
            fixture["log_root"], fixture["chain"], repo=fixture["repo"]
        ) == before

    def test_a_wrong_confirm_is_refused(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        with pytest.raises(LineageRefused):
            delete_session(
                fixture["log_root"],
                fixture["chain"][1],
                confirm="sess-somebody-else",
                why="asked",
            )

    def test_no_reason_is_refused(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        with pytest.raises(LineageRefused) as excinfo:
            delete_session(
                fixture["log_root"], fixture["chain"][1], confirm=fixture["chain"][1]
            )
        assert "reason" in excinfo.value.reason

    def test_the_live_tip_is_refused_without_an_explicit_drop(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        tip = fixture["chain"][-1]
        with pytest.raises(LineageRefused) as excinfo:
            delete_session(
                fixture["log_root"], tip, confirm=tip, why="asked", repo=fixture["repo"]
            )
        assert excinfo.value.detail.get("is_tip") is True

    def test_deleting_something_that_does_not_exist_refuses_rather_than_succeeds(
        self, tmp_path
    ) -> None:
        fixture = _lineage(tmp_path)
        with pytest.raises(LineageRefused):
            delete_session(
                fixture["log_root"],
                "sess-does-not-exist",
                confirm="sess-does-not-exist",
                why="asked",
            )
        assert deletion_ledger(fixture["log_root"]) == [], (
            "a refused deletion wrote a ledger row claiming a destruction that "
            "did not happen"
        )

    def test_the_receipt_names_what_was_destroyed(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        victim = fixture["chain"][1]
        receipt = delete_session(
            fixture["log_root"],
            victim,
            confirm=victim,
            why="the operator asked for this one",
            repo=fixture["repo"],
        )
        assert receipt["ok"] is True
        assert receipt["history_destroyed"] is True
        assert receipt["explicit"] is True
        record = receipt["record"]
        assert record["destroyed"], "the receipt does not say what it destroyed"
        assert any(item["bytes"] > 0 for item in record["destroyed"])

    def test_only_the_explicit_deletion_may_destroy_a_file(self) -> None:
        """The structural half, with a per-row reason so the set cannot grow."""
        for filename, allowed in PERMITTED_DELETERS.items():
            tree = ast.parse((RUNTIME_DIR / filename).read_text(encoding='utf-8'))
            offenders: Dict[str, List[str]] = {}
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr in {'unlink', 'rmtree', 'remove'}
                    ):
                        offenders.setdefault(node.name, []).append(inner.func.attr)
            for name in offenders:
                assert name in allowed, (
                    f'{filename}:{name} calls {offenders[name]} and is not a permitted '
                    f'deleter. The permitted set is {sorted(allowed)}; adding to it is '
                    'a deliberate act, not a refactor.'
                )
            for name, reason in allowed.items():
                assert len(reason) > 40, f'{filename}:{name} has no stated reason'

    def test_the_tree_never_deletes_its_own_journal(self) -> None:
        source = (RUNTIME_DIR / 'session_tree.py').read_text(encoding='utf-8')
        assert 'shutil.copy2' in source, (
            'the previous journal must be COPIED aside. Moving it with os.replace '
            'is what created the window in which a concurrent reader saw no '
            'history at all.'
        )

    # ===========================================================================
# 3. RESUME EXACT ACROSS ROTATION
# ===========================================================================


class TestResumeIsExactAcrossRotation:
    def test_the_tip_of_a_rotated_lineage_resumes(self, tmp_path) -> None:
        """**The defect this round closed.** It answered False for every session."""
        fixture = _lineage(tmp_path, rotations=3)
        receipt = resume_state(
            fixture["log_root"], fixture["chain"][-1], repo=fixture["repo"]
        )
        assert receipt["resume_authorised"] is True, (
            f"the tip of a rotated lineage refused to resume: {receipt['reason']}. "
            "A resume feature that refuses every real session is not a fail-closed "
            "gate; it is a dead one, and it is indistinguishable from a correct "
            "refusal to anybody reading the boolean."
        )

    def test_a_rotated_segment_names_its_successor_instead_of_resuming(
        self, tmp_path
    ) -> None:
        fixture = _lineage(tmp_path, rotations=3)
        middle = fixture["chain"][1]
        receipt = resume_state(fixture["log_root"], middle, repo=fixture["repo"])
        assert receipt["may_resume"] is False
        assert receipt["continued_session_id"] == fixture["chain"][2]
        assert "ended" in receipt["continued_reason"]

    def test_an_explicit_continue_resumes_a_rotated_segments_own_turns(
        self, tmp_path
    ) -> None:
        fixture = _lineage(tmp_path, rotations=3)
        middle = fixture["chain"][1]
        receipt = resume_state(
            fixture["log_root"], middle, continue_request=True, repo=fixture["repo"]
        )
        assert receipt["resume_authorised"] is True
        assert receipt["continued_session_id"] == middle
        assert receipt["continue_request_is_the_only_reuse"] is True

    def test_a_missing_closure_is_unknown_not_false(self, tmp_path) -> None:
        """The tri-state. ``False`` here would be a lie about a fact nobody gave."""
        fixture = _lineage(tmp_path)
        receipt = resume_state(
            fixture["log_root"], fixture["chain"][-1], repo=fixture["repo"]
        )
        assert receipt["identity_carried"] is False
        assert receipt["identity_ok"] is None, (
            "'no closure was available to check' and 'the closure does not hold' "
            "are different facts and must not share a value"
        )
        assert receipt["identity_source"] == "lineage"

    def test_all_five_identity_fields_are_named_in_the_receipt(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        receipt = resume_state(
            fixture["log_root"], fixture["chain"][-1], repo=fixture["repo"]
        )
        assert receipt["identity_fields"] == list(LINEAGE_IDENTITY_FIELDS)
        assert len(LINEAGE_IDENTITY_FIELDS) == 5
        assert set(LINEAGE_IDENTITY_FIELDS) == {
            "repository_identity",
            "request_identity",
            "revision_identity",
            "resume_namespace",
            "effort_identity",
        }

    def test_a_mismatched_closure_fails_closed_on_every_field_it_can_see(
        self, tmp_path
    ) -> None:
        """The hazard, on the side where it is actually reachable.

        A ``cli.session`` snapshot is SILENT on all five identity fields, so a
        closure against one has nothing to contradict -- which is why this pin
        writes an identity onto the snapshot before asserting the refusal.
        Asserting it against a blank snapshot asserts nothing, and that is exactly
        the mistake the previous version of :func:`resume_state` made.
        """
        from cli import session as session_api
        from runtime import session_lineage as lineage

        fixture = _lineage(tmp_path, rotations=1)
        tip = fixture["chain"][-1]
        good = {
            "repository_identity": "REPO-A",
            "request_identity": "rq",
            "revision_identity": "rv",
            "resume_namespace": "ns",
            "effort_identity": "high",
        }
        session = lineage.load_session(fixture["log_root"], tip, repo=fixture["repo"])
        for field, value in good.items():
            session[field] = value
        assert session_api.save_session(fixture["log_root"], session)

        matching = resume_state(
            fixture["log_root"], tip, identity=good, repo=fixture["repo"]
        )
        assert matching["identity_ok"] is True, (
            f"a closure that MATCHES was refused: {matching['identity_problems']}"
        )
        for field in LINEAGE_IDENTITY_FIELDS:
            bad = dict(good)
            bad[field] = "SOMETHING-ELSE"
            receipt = resume_state(
                fixture["log_root"], tip, identity=bad, repo=fixture["repo"]
            )
            assert receipt["may_resume"] is False, f"{field} mismatched and admitted"
            assert receipt["identity_ok"] is False
            assert any(field in problem for problem in receipt["identity_problems"]), (
                f"the refusal for {field} does not NAME the field"
            )

    def test_a_closure_silent_where_the_session_speaks_refuses(self, tmp_path) -> None:
        from cli import session as session_api
        from runtime import session_lineage as lineage

        fixture = _lineage(tmp_path, rotations=1)
        tip = fixture["chain"][-1]
        session = lineage.load_session(fixture["log_root"], tip, repo=fixture["repo"])
        session["repository_identity"] = "REPO-A"
        assert session_api.save_session(fixture["log_root"], session)
        receipt = resume_state(
            fixture["log_root"],
            tip,
            identity={field: "" for field in LINEAGE_IDENTITY_FIELDS},
            repo=fixture["repo"],
        )
        assert receipt["may_resume"] is False, (
            "the closure names no repository while the session does. That is an "
            "incomplete closure against a real claim, and it refuses."
        )
        assert any(
            "names nothing" in problem for problem in receipt["identity_problems"]
        )

    def test_a_snapshot_silent_on_the_closure_records_rather_than_refuses(
        self, tmp_path
    ) -> None:
        """The fix. Recorded, because 'nothing to disagree with' is not 'refused'."""
        fixture = _lineage(tmp_path, rotations=1)
        receipt = resume_state(
            fixture["log_root"],
            fixture["chain"][-1],
            identity={
                "repository_identity": "REPO-A",
                "request_identity": "rq",
                "revision_identity": "rv",
                "resume_namespace": "ns",
                "effort_identity": "high",
            },
            repo=fixture["repo"],
        )
        assert receipt["resume_authorised"] is True
        assert receipt["identity_ok"] is True
        assert set(
            receipt["identity_lineage"]["not_carried_by_the_snapshot"]
        ) == set(LINEAGE_IDENTITY_FIELDS)

    def test_a_rotation_cannot_be_a_way_to_move_between_repositories(self, tmp_path) -> None:
        """The property the old unsatisfiable check was supposed to hold."""
        from runtime import session_lineage as lineage

        fixture = _lineage(tmp_path, rotations=2)
        root_id, tip_id = fixture["chain"][0], fixture["chain"][-1]
        # Hand-forge a divergence: the root claims repository A, the tip claims B.
        for sid, repo_identity in ((root_id, "REPO-A"), (tip_id, "REPO-B")):
            session = lineage.load_session(fixture["log_root"], sid, repo=fixture["repo"])
            session["repository_identity"] = repo_identity
            assert session_api_save(fixture["log_root"], session)
        folded = lineage.lineage_identity(fixture["log_root"], tip_id, repo=fixture["repo"])
        assert folded["ok"] is False
        assert "repository_identity" in folded["divergent"]
        receipt = resume_state(fixture["log_root"], tip_id, repo=fixture["repo"])
        assert receipt["may_resume"] is False
        assert any("disagree" in problem for problem in receipt["identity_problems"])

    def test_the_resume_against_a_checkpoint_reads_the_real_closure(self, tmp_path) -> None:
        from runtime.checkpoint import TaskCheckpoint

        fixture = _lineage(tmp_path, rotations=1)
        runtime_dir = tmp_path / "task.runtime"
        runtime_dir.mkdir()
        TaskCheckpoint(runtime_dir).save(
            {
                "status": "running",
                "identity_version": 1,
                "repository_identity": "REPO",
                "request_identity": "rq",
                "revision_identity": "rv",
                "resume_namespace": "ns",
                "effort_identity": "high",
            }
        )
        receipt = resume_against_checkpoint(
            fixture["log_root"], fixture["chain"][-1], runtime_dir, repo=fixture["repo"]
        )
        assert receipt["identity_source"] == "checkpoint"
        assert receipt["checkpoint"]["carries_identity"] is True
        assert receipt["resume_authorised"] is True

    def test_a_checkpoint_with_no_closure_falls_back_rather_than_defaulting(
        self, tmp_path
    ) -> None:
        from runtime.checkpoint import TaskCheckpoint

        fixture = _lineage(tmp_path, rotations=1)
        runtime_dir = tmp_path / "task2.runtime"
        runtime_dir.mkdir()
        TaskCheckpoint(runtime_dir).save({"status": "running"})
        receipt = resume_against_checkpoint(
            fixture["log_root"], fixture["chain"][-1], runtime_dir, repo=fixture["repo"]
        )
        assert receipt["identity_source"] == "lineage", (
            "a checkpoint with no identity block must fall back to the LINEAGE, "
            "never to a synthesised closure -- defaulting a missing identity field "
            "would authorise a resume against a claim nobody made"
        )
        assert receipt["checkpoint"]["carries_identity"] is False
        assert receipt["checkpoint"]["reason"]



def session_api_save(log_root: Any, session: Dict[str, Any]) -> bool:
    """Persist a session through the real writer. Returns whether it landed."""
    from cli import session as session_api

    return bool(session_api.save_session(log_root, session))


class TestOldCheckpointsMigrateOrFailClosed:
    def test_a_complete_old_checkpoint_migrates(self) -> None:
        from runtime.checkpoint import checkpoint_identity_matches

        identity = {
            "identity_version": 1,
            "task_id": "t1",
            "repository_identity": "REPO",
            "request_identity": "rq",
            "revision_identity": "rv",
            "resume_namespace": "ns",
            "effort_identity": "high",
        }
        old = {key: value for key, value in identity.items() if key != "identity_version"}
        assert checkpoint_identity_matches(old, identity) is True, (
            "a checkpoint written by an engine that stamped all five identity "
            "fields and merely predates the version stamp is PROVABLY safe to "
            "adopt, and refusing it makes users redo correctly-recorded work"
        )

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda c: c.pop("effort_identity"), id="missing_effort"),
            pytest.param(lambda c: c.update({"effort_identity": ""}), id="blank_effort"),
            pytest.param(lambda c: c.update({"request_identity": ""}), id="blank_request"),
            pytest.param(lambda c: c.update({"identity_version": 99}), id="future_version"),
            pytest.param(lambda c: c.clear(), id="no_identity_block"),
        ],
    )
    def test_an_incomplete_or_unreadable_old_checkpoint_fails_closed(
        self, mutate
    ) -> None:
        from runtime.checkpoint import checkpoint_identity_matches

        checkpoint = {
            "task_id": "t1",
            "repository_identity": "REPO",
            "request_identity": "rq",
            "revision_identity": "rv",
            "resume_namespace": "ns",
            "effort_identity": "high",
        }
        mutate(checkpoint)
        identity = {
            "identity_version": 1,
            "task_id": "t1",
            "repository_identity": "REPO",
            "request_identity": "rq",
            "revision_identity": "rv",
            "resume_namespace": "ns",
            "effort_identity": "high",
        }
        assert checkpoint_identity_matches(checkpoint, identity) is False

    def test_should_resume_returns_a_value_for_a_non_mapping_checkpoint(self) -> None:
        from runtime.checkpoint import should_resume

        assert should_resume({"resume": True}, "not a mapping") is False, (
            "a resume decision must be a VALUE; raising hands the caller an "
            "exception instead of an answer"
        )

    def test_a_non_mapping_checkpoint_migration_names_its_reason(self) -> None:
        from runtime.checkpoint import migrate_checkpoint

        outcome = migrate_checkpoint("not a mapping")
        assert outcome["migrated"] is False
        assert outcome["status"] == "unreadable"
        assert outcome["reason"]


class TestTheExemptionSetHasNotGrown:
    """A refactor is exactly when a fourth exemption appears unnoticed."""

    def test_the_exemption_reader_names_exactly_two_reasons(self) -> None:
        from runtime.budget_governor import supervision_exemption

        reasons = {
            str(supervision_exemption({"supervision_exemption": {"reason": name}})["reason"])
            for name in ("provider_backoff", "awaiting_approval")
        }
        assert len(reasons) == 2, sorted(reasons)

    def test_the_two_refusals_stay_distinct(self) -> None:
        from runtime.budget_governor import state_stale_exempt, supervision_exemption

        backoff = supervision_exemption(
            {"supervision_exemption": {"reason": "provider_backoff", "until_epoch": time.time() + 60}}
        )
        approval = supervision_exemption({"awaiting_approval": True})
        assert backoff is not None and approval is not None
        assert backoff["reason"] != approval["reason"], (
            "the backoff exemption and the approval exemption collapsed into one "
            "reason; they are different things and a reader cannot tell them apart"
        )
        # `state_stale_exempt` answers (exempt, reason) -- a REASON as well as a
        # boolean, so a journal row can name which exemption applied.
        exempt, reason = state_stale_exempt(
            {"supervision_exemption": backoff, "status": "running"}
        )
        assert exempt is True and "provider_backoff" in reason
        exempt_approval, reason_approval = state_stale_exempt(
            {"awaiting_approval": True, "status": "running"}
        )
        assert exempt_approval is True
        assert reason != reason_approval, (
            "the historical `awaiting_approval` boolean must be re-expressed "
            "through the SAME mechanism, not become a second exemption with its "
            "own reason"
        )

    def test_a_worker_in_backoff_is_exempt_and_an_expired_one_is_not(self) -> None:
        from runtime.budget_governor import state_stale_exempt

        live = {
            "supervision_exemption": {
                "reason": "provider_backoff",
                "until_epoch": time.time() + 60,
            }
        }
        assert state_stale_exempt({**live, "status": "running"})[0] is True
        expired = {"supervision_exemption": {"reason": "provider_backoff", "until_epoch": 1.0}}
        assert state_stale_exempt({**expired, "status": "running"})[0] is False, (
            "an EXPIRED exemption is not an exemption, or a worker that stops "
            "making progress after its wait is never killed"
        )

    def test_no_module_introduces_a_third_exemption_reason(self) -> None:
        """A source-level sweep, so a new reason cannot appear unnoticed."""
        allowed = {"provider_backoff", "awaiting_approval", ""}
        found: Dict[str, List[str]] = {}
        for path in sorted(RUNTIME_DIR.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            for name in (
                "background_job_exempt",
                "extraction_exempt",
                "gc_exempt",
                "compaction_exempt",
                "session_exempt",
            ):
                if name in source:
                    found.setdefault(name, []).append(path.name)
        assert not found, (
            f"a hang-kill exemption beyond the two named ones appeared: {found}. "
            "Background extraction jobs are NOT a third exemption."
        )
        assert allowed >= {"provider_backoff", "awaiting_approval"}

    def test_the_exemption_set_is_still_exactly_two_in_the_reader(self) -> None:
        from runtime.budget_governor import supervision_exemption

        assert supervision_exemption({}) is None
        assert supervision_exemption(None) is None
        assert supervision_exemption({"awaiting_approval": False}) is None


# ===========================================================================
# 4. BACKGROUND EXTRACTION CANNOT CORRUPT THE TREE
# ===========================================================================


class TestTheJobsViewIsReadOnlyByShape:
    def test_the_view_exposes_no_writer(self) -> None:
        separation = job_write_separation()
        assert separation["separation_holds"] is True, separation["verdict"]
        assert separation["writers_on_the_view"] == []

    def test_the_control_arm_is_real(self) -> None:
        separation = job_write_separation()
        assert separation["control_arm_is_real"] is True, (
            "the real tree does NOT have the writers, so the view is proving "
            "nothing -- it is an object that cannot write because nothing can"
        )
        assert set(separation["writers_on_the_real_tree"]) == set(separation["forbidden_writers"])

    def test_the_view_cannot_be_rebound(self) -> None:
        view = read_only_view("root", "sess-1")
        with pytest.raises(AttributeError):
            view.session_id = "sess-2"
        with pytest.raises(AttributeError):
            view._tree = object()

    def test_the_view_holds_no_writable_tree(self) -> None:
        assert set(ReadOnlySessionView.__slots__) == {
            "_log_root",
            "_session_id",
            "_repo",
            "_reads",
            "_last",
        }
        assert "tree" not in ReadOnlySessionView.__slots__

    def test_an_unreadable_session_is_not_reported_as_an_empty_one(self, tmp_path) -> None:
        view = read_only_view(tmp_path, "sess-does-not-exist")
        receipt = view.read()
        assert receipt["present"] is False
        assert receipt["complete"] is False, (
            "an unreadable session reported as `turns: 0` is indistinguishable "
            "from a session with no turns, and an extraction job would extract "
            "nothing and record that it found nothing"
        )
        assert "NOT the same" in receipt["reason"]

    def test_the_background_jobs_module_never_builds_a_writable_tree(self) -> None:
        source = (RUNTIME_DIR / "background_jobs.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "SessionTree", (
                    "background_jobs constructs a SessionTree; a job must reach "
                    "the store through a read-only view"
                )

    def test_a_consistent_tree_read_reports_completeness(self, tmp_path) -> None:
        tree = SessionTree(tmp_path / "tree")
        tree.advance(turn_id="t0", text="hello")
        view = read_only_view(tmp_path, "sess-1")
        receipt = view.tree_read(tmp_path / "tree")
        assert receipt["complete"] is True
        assert receipt["head_present"] is True
        assert receipt["write_capable"] is False
        assert receipt["turns"] == 1


class TestLeaseExpiryReclaimsAJob:
    def test_a_killed_worker_is_reclaimable(self, tmp_path) -> None:
        now = {"t": 1000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], lease_s=60.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        first = jobs.claim("worker-a")
        assert first["claimed"] is True
        assert jobs.claim("worker-b")["claimed"] is False, (
            "a live lease was stolen"
        )
        now["t"] += 61.0
        reclaimed = jobs.reclaim_expired()
        assert len(reclaimed) == 1
        assert reclaimed[0]["reclaimed_from"] == "worker-a"
        second = jobs.claim("worker-b")
        assert second["claimed"] is True
        assert second["attempt"] == first["attempt"] + 1, (
            "a reclaim INHERITS the attempt count. A job that kills its worker must "
            "not get a fresh attempt budget each time, or 'bounded attempts' is "
            "not a bound."
        )

    def test_a_terminal_job_is_never_reclaimed(self, tmp_path) -> None:
        now = {"t": 1000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], lease_s=60.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        claimed = jobs.claim("worker-a")
        jobs.succeed(claimed["job_id"])
        now["t"] += 10_000.0
        assert jobs.reclaim_expired() == []
        assert jobs.claim("worker-b")["claimed"] is False, (
            "a succeeded job re-running is a double execution"
        )

    def test_the_attempt_budget_is_a_real_bound(self, tmp_path) -> None:
        now = {"t": 1000.0}
        jobs = BackgroundJobs(
            tmp_path / "jobs", clock=lambda: now["t"], lease_s=60.0, max_attempts=3
        )
        jobs.enqueue("extract", {"session_id": "sess-1"})
        for _ in range(5):
            claimed = jobs.claim("worker")
            if not claimed["claimed"]:
                break
            now["t"] += 61.0
            jobs.reclaim_expired()
        assert jobs.state()[jobs.rows()[-1]["job_id"]]["state"] == "abandoned"


class TestTheIdlePolicyIsEnforced:
    def test_the_policy_refuses_an_unevaluated_repository(self) -> None:
        receipt = idle_admission(last_activity_epoch=None, idle_grace_s=900.0)
        assert receipt["admitted"] is False
        assert receipt["refusal"] == "not_evaluated"
        assert receipt["evaluated"] is False

    def test_the_policy_refuses_an_idle_repository_with_the_numbers(self) -> None:
        receipt = idle_admission(
            last_activity_epoch=1000.0, now=2000.0, idle_grace_s=900.0
        )
        assert receipt["admitted"] is False
        assert receipt["refusal"] == "repository_idle"
        assert receipt["idle_s"] == 1000.0
        assert "900.0s" in receipt["reason"]

    def test_the_control_arm_admits_an_active_repository(self) -> None:
        receipt = idle_admission(
            last_activity_epoch=1900.0, now=2000.0, idle_grace_s=900.0
        )
        assert receipt["admitted"] is True
        assert receipt["evaluated"] is True

    def test_the_policy_actually_refuses_a_claim(self, tmp_path) -> None:
        """ENFORCED, not merely defined. Wave 1 only reported the number."""
        now = {"t": 5000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], idle_grace_s=900.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        with pytest.raises(JobRefused) as excinfo:
            jobs.claim(
                "worker-a",
                respect_idle_policy=True,
                repo_last_activity_epoch=100.0,
                override_idle=False,
            )
        assert "idle grace window" in excinfo.value.reason
        assert excinfo.value.detail["idle"]["idle_s"] == 4900.0

    def test_the_control_arm_a_fresh_repository_is_claimable(self, tmp_path) -> None:
        now = {"t": 5000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], idle_grace_s=900.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        receipt = jobs.claim(
            "worker-a", respect_idle_policy=True, repo_last_activity_epoch=4900.0
        )
        assert receipt["claimed"] is True
        assert receipt["idle"]["enforced"] is True

    def test_an_override_is_journalled_not_silent(self, tmp_path) -> None:
        now = {"t": 5000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], idle_grace_s=900.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        receipt = jobs.claim(
            "worker-a",
            respect_idle_policy=True,
            repo_last_activity_epoch=100.0,
            override_idle=True,
        )
        assert receipt["claimed"] is True
        assert receipt["idle"]["overridden"] is True
        events = [str(row.get("event")) for row in jobs.rows()]
        assert "idle_policy_overridden" in events, (
            "an override that is not journalled is an invisible bypass"
        )

    def test_enforcement_is_opt_in_and_says_so(self, tmp_path) -> None:
        """The honest reason it is opt-in, pinned so it cannot drift silently."""
        now = {"t": 5000.0}
        jobs = BackgroundJobs(tmp_path / "jobs", clock=lambda: now["t"], idle_grace_s=900.0)
        jobs.enqueue("extract", {"session_id": "sess-1"})
        default = jobs.claim("worker-a")
        assert default["claimed"] is True, (
            "Wave 1's claim(owner) contract must keep working; an unconditional "
            "policy broke 15 of its 89 shipped pins, which is the evidence that "
            "the default is a behaviour change to every existing caller"
        )
        assert default["idle"]["enforced"] is False
        assert jobs.describe()["idle_policy"]["enforced"] is True


class TestAJobWritingAMemoryDoesNotWriteATreeEntry:
    def test_the_extraction_seam_is_the_read_only_view(self) -> None:
        """The interface T5's pipeline mounts, asserted to be read-only."""
        view = read_only_view("/tmp", "sess-1")
        for name in ("append", "advance", "fork", "move_head", "abandon", "gc"):
            assert not hasattr(view, name)

    def test_nothing_in_memory_imports_a_tree_writer(self) -> None:
        memory_dir = REPO_ROOT / "memory"
        if not memory_dir.is_dir():
            pytest.skip("memory/ is not present")
        offenders: List[str] = []
        for path in sorted(memory_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            if "SessionTree" in source:
                offenders.append(path.name)
        assert not offenders, (
            f"memory/ reaches a writable tree: {offenders}. Extraction writing a "
            "memory must not be able to write a tree entry."
        )


# ===========================================================================
# 5. THE COST, AND THE HONEST COST
# ===========================================================================


class TestTheCostTableIsMeasuredAndHonest:
    @pytest.fixture(scope="class")
    def measurement(self) -> Dict[str, Any]:
        return session_cost.measure(turns=40, forks=3, text_chars=200)

    @pytest.fixture(scope="class")
    def table(self, measurement) -> List[Dict[str, Any]]:
        return session_cost.cost_table(measurement)

    def test_every_declared_metric_is_present(self, table) -> None:
        assert [str(row["metric"]) for row in table] == list(session_cost.COST_METRICS)

    def test_both_arms_were_measured_by_the_same_byte_counter(self, measurement) -> None:
        assert (
            measurement["baseline"]["bytes_measured_by"]
            == measurement["tree"]["bytes_measured_by"]
        ), (
            "the arms were measured by DIFFERENT methods, so the storage column "
            "compares a subtree against a single file. That reported the tree as "
            "6x smaller when it was not."
        )

    def test_both_a_forked_and_an_unforked_arm_are_measured(self, measurement) -> None:
        assert set(measurement["no_forks"]) == {"baseline", "tree"}

    def test_the_fork_win_is_structural_not_measured_luck(self, table) -> None:
        row = next(r for r in table if r["metric"] == "fork_cost")
        assert row["before_copied_entries"] and row["before_copied_entries"] > 0
        assert row["after_copied_entries"] == 0, (
            "a fork is a new leaf, so the copies are ZERO -- that is structural "
            "and does not depend on the workload shape"
        )

    def test_the_rewind_row_states_what_each_side_lost(self, table) -> None:
        row = next(r for r in table if r["metric"] == "rewind_cost")
        assert row["after_turns_dropped"] == 0
        assert row["before_turns_dropped"] > 0, (
            "the baseline dropped turns on a rewind; if it did not, the "
            "comparison is not measuring what it claims"
        )

    def test_the_storage_verdict_names_the_condition(self, table) -> None:
        row = next(r for r in table if r["metric"] == "storage_per_turn")
        assert "CONDITIONAL" in row["verdict"]
        assert "with_forks" in row and "without_forks" in row
        nofork = row["without_forks"]["tree_is_x"]
        assert nofork is not None and nofork > 1, (
            "with no forks the append-only tree must be the LARGER store, and "
            "that is the number to budget from"
        )

    def test_the_history_row_is_the_only_one_that_is_not_a_trade_off(self, table) -> None:
        row = next(r for r in table if r["metric"] == "history_retained")
        assert row["before"] is False
        assert row["after"] is True

    def test_the_compaction_cost_is_reported_as_a_loss(self, table) -> None:
        row = next(r for r in table if r["metric"] == "compaction_rotation_cost")
        assert row["before_samples_ms"] and row["after_samples_ms"], (
            "the compaction cost must be reported as a DISTRIBUTION over repeats, "
            "because the first version of this table reported a single sample "
            "that was really a one-off import"
        )

    def test_the_rotation_scaling_shape_is_measured(self) -> None:
        report = session_cost.rotation_scaling(sizes=(10, 40), repeats=2)
        assert len(report["points"]) == 2
        assert report["grew_by"] is not None
        assert report["corrects_an_expectation"], (
            "this round predicted the rotation cost would be dominated by the "
            "by-value inheritance; the measurement said otherwise. The "
            "correction is recorded rather than quietly dropped."
        )

    def test_the_growth_projection_declares_its_workload(self) -> None:
        projection = session_cost.project_30_days()
        assert projection["usage"]["days"] == 30
        assert projection["usage"]["turns"] > 0
        assert projection["caveat"]

    def test_the_gc_policy_states_what_is_kept_forever_and_what_fires(self) -> None:
        policy = session_cost.gc_policy()
        assert policy["runs_by_itself"] is False
        assert policy["runs_on_a_schedule"] is False
        assert any("head` marker" in item for item in policy["retained_forever"])
        assert policy["pressure_does_not_change_correctness"]

    def test_the_honest_cost_names_the_losses(self, table) -> None:
        receipt = session_cost.honest_cost(table)
        assert receipt["the_tree_is_more_expensive_on"], (
            "a cost report that names no losses is a marketing document"
        )
        assert "compaction_rotation_cost" in receipt["the_tree_is_more_expensive_on"]
        assert receipt["the_verdict_is_conditional_on"], (
            "a metric whose direction depends on the workload has to be reported as "
            "conditional; reporting the flattering half is the failure"
        )
        assert receipt["the_qualification"], "the measurement's own limits must be stated"
        assert receipt["what_would_change_the_verdict"]


# ===========================================================================
# 6. THE LIVE-SESSION DECLARATION T2 ASKED FOR
# ===========================================================================


class TestTheLiveSessionDeclaration:
    def test_publishing_produces_the_path_execution_reads(self, tmp_path) -> None:
        from execution.snapshot import LINEAGE_DIRNAME, LIVE_SESSIONS_NAME

        receipt = publish_live_sessions(tmp_path, {"sess-1": "lin-1"})
        assert receipt["written"] is True
        expected = Path(tmp_path) / LINEAGE_DIRNAME / LIVE_SESSIONS_NAME
        assert Path(receipt["path"]) == expected, (
            f"execution.snapshot reads {expected}; publishing anywhere else means "
            "every retention pass reports lineage_view_source='absent' and prunes "
            "normally, so lineage protection is never exercised on a real tree"
        )

    def test_the_declaration_is_readable_by_the_retention_view(self, tmp_path) -> None:
        from execution.snapshot import LineageView, lineage_view

        publish_live_sessions(tmp_path, {"sess-1": "lin-1"})
        view = lineage_view(tmp_path)
        assert isinstance(view, LineageView)
        assert view.source == "declaration"
        assert view.is_live("lin-1") is True
        assert view.is_live("lin-other") is False

    def test_a_removed_session_is_no_longer_live(self, tmp_path) -> None:
        from execution.snapshot import lineage_view
        from runtime.session_lineage import end_session

        publish_live_sessions(tmp_path, {"sess-1": "lin-1", "sess-2": "lin-2"})
        receipt = end_session(tmp_path, "sess-1")
        assert receipt["was_live"] is True
        view = lineage_view(tmp_path)
        assert view.is_live("lin-1") is False
        assert view.is_live("lin-2") is True, "one removal took the other with it"

    def test_removing_something_absent_says_so(self, tmp_path) -> None:
        from runtime.session_lineage import end_session

        publish_live_sessions(tmp_path, {"sess-1": "lin-1"})
        assert end_session(tmp_path, "sess-never-there")["was_live"] is False

    def test_a_torn_declaration_protects_everything_rather_than_pruning(
        self, tmp_path
    ) -> None:
        """A crash during the write must not silently disable pruning.

        The protection is NOT in ``LineageView.is_live`` -- that reader honestly
        reports "I do not know" for a lineage it cannot resolve. The protection is
        in ``prune_run_directories``, which applies ``refuse_unplaced`` to the
        candidate set BEFORE selection when the source is ``unreadable``. So this
        asserts the actual enforcement point, because asserting ``is_live`` would
        be asserting a mechanism that does not exist and would have sent someone
        to fix the wrong file.
        """
        from execution import snapshot as snap

        publish_live_sessions(tmp_path, {"sess-1": "lin-1"})
        run = tmp_path / "hexec-probe"
        run.mkdir()
        (run / "trace.jsonl").write_text('{"kind":"task_start"}\n', encoding="utf-8")
        path = Path(tmp_path) / "_lineage" / "live_sessions.json"
        path.write_text("{ this is not json", encoding="utf-8")

        view = snap.lineage_view(tmp_path)
        assert view.source == "unreadable"
        report = snap.prune_run_directories(
            tmp_path, snap.RetentionSettings(max_age_s=0.0, keep_latest=0)
        )
        assert report.lineage_view_source == "unreadable"
        assert "hexec-probe" not in report.removed, (
            "a CORRUPT live-session declaration means liveness cannot be "
            "established, so an unplaced run must not be pruned on the strength "
            "of a guess"
        )


# ===========================================================================
# 7. THE PINS ARE NOT RUBBER STAMPS
# ===========================================================================


class TestThePinsAreNotRubberStamps:
    def test_the_audit_really_runs_every_vector(self) -> None:
        assert len(history_audit()["vectors"]) == 8

    def test_the_history_finder_would_notice_a_lost_turn(self, tmp_path) -> None:
        fixture = _lineage(tmp_path)
        chain = [str(item) for item in fixture["chain"]]
        assert session_durability.fixture_turn_ids(
            fixture["log_root"], chain, repo=fixture["repo"]
        ) == []
        from cli import session as session_api

        session = session_api.load_or_create(
            fixture["log_root"], fixture["repo"], chain[-1]
        )
        for key in ("turns", "compacted_turns", "raw_turns"):
            session[key] = [item for item in (session.get(key) or []) if item.get("turn_id") != "t0"]
        session_api.save_session(fixture["log_root"], session)
        lost = session_durability.fixture_turn_ids(
            fixture["log_root"], chain, repo=fixture["repo"]
        )
        assert lost == [], (
            "the fixture is too small: removing a turn from the tip changed "
            "nothing, so 'no turns lost' would pass even if the finder were blind"
        )

    def test_the_gc_finder_would_notice_a_pruned_fork(self, tmp_path) -> None:
        tree = SessionTree(tmp_path / "tree")
        for index in range(8):
            tree.advance(turn_id=f"t{index}", text="x" * 32)
        fork = tree.fork(label="the fork")
        turns = [item for item in tree.entries() if item.kind == "turn"]
        tree.move_head(turns[0].id, reason="rewind")
        aged = SessionTree(tmp_path / "tree", clock=lambda: time.time() + 100_000.0)
        aged.gc(min_age_s=-1.0, roots=[])
        assert SessionTree(tmp_path / "tree").entry(str(fork["entry_id"])) is None, (
            "with roots=[] the fork WAS collected, so the default-root pin is "
            "measuring a real protection rather than a tree with nothing in it"
        )

    def test_the_module_states_the_round_found_real_defects(self) -> None:
        tree_source = (RUNTIME_DIR / "session_tree.py").read_text(encoding="utf-8")
        durable_source = (RUNTIME_DIR / "session_durability.py").read_text(encoding="utf-8")
        assert "data-integrity defect" in tree_source, (
            "the GC-moves-the-head finding belongs in the module that has the bug, "
            "so the next reader meets it before re-deriving it"
        )
        assert "vacuous" in durable_source, (
            "three of this round's first-pass reds were vacuous-vector artefacts; "
            "the word belongs in the module so the next reader knows they happened"
        )
        assert "2 of 48" in tree_source or "of 48" in tree_source, (
            "the measured concurrency figure belongs next to the fix, because "
            "'a concurrent reader might see an empty tree' is a hypothesis and "
            "'2 of 48 reads did' is a measurement"
        )
