"""Pins for P3a W1 (T2): fork file state, lineage retention, memory evidence.

Four-way pinning where it matters, non-vacuity before every acceptance
assertion, and a weakening demonstration that produces BOTH colours in one run.

Run:  ``python -m pytest execution/test_p3a1_file_state_pins.py -q``

Every pin here is host-only: no Docker, no model, no network, no credential.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

import execution.file_state as fs
import execution.memory_evidence as mem
import execution.snapshot as sn
import execution.storage_cost as sc

REPO_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# helpers
# ===========================================================================


def _isolated_home(tmp_path: Path) -> Path:
    """Point the guard at a throwaway Neo home so no test touches a real one."""
    home = tmp_path / "neo-home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _mkrun(root: Path, name: str, days_old: float = 40.0, lineage: str = "",
           live: bool = False, files: int = 3) -> Path:
    """Create a recognisable run directory with a real snapshot pair.

    **The mtime is stamped LAST, and that ordering is load-bearing.**
    :func:`execution.snapshot.record_lineage` writes ``lineage.json`` INSIDE the
    run directory, which refreshes the directory's mtime -- and the retention
    age window is measured from exactly that mtime (see
    :data:`execution.snapshot.AGE_BASIS`). Stamping before the sidecar write
    makes every run look freshly touched, and a pin written that way asserts
    nothing about the age policy at all.
    """
    run = root / name
    (run / "pristine").mkdir(parents=True)
    (run / "work").mkdir()
    for index in range(files):
        (run / "pristine" / f"f{index}.py").write_text(f"VALUE = {index}\n" * 40)
        (run / "work" / f"f{index}.py").write_text(f"VALUE = {index}\n" * 40)
    if lineage:
        sn.record_lineage(
            run,
            sn.LineageRef(
                lineage_id=lineage,
                sessions=("sess-parent", "sess-child"),
                live_sessions=("sess-child",) if live else (),
                parent_session="sess-parent",
                head_session="sess-child",
                recorded_at=1_700_000_000.0 - days_old * 86400.0,
            ),
        )
    stamp = 1_700_000_000.0 - days_old * 86400.0
    for target in (run, run / "pristine", run / "work"):
        os.utime(target, (stamp, stamp))
    return run


def _lineage_declaration(root: Path, sessions: List[Tuple[str, str]]) -> None:
    assert sn.write_live_sessions(root, [
        {"session_id": session, "lineage_id": lineage} for session, lineage in sessions
    ])


def _dead(root: Path, name: str, *, days_old: float = 400.0) -> Path:
    """A run whose lineage is COMPLETE -- the shape a prunable run really has.

    A run with a dead lineage sidecar is the normal prunable case. A run with NO
    sidecar at all is a run this module cannot place, and an unplaced run is
    deliberately PROTECTED while liveness is unknown (see
    ``test_an_unplaced_run_is_protected_while_liveness_is_unknown``). Writing
    the prunable fixtures without a sidecar would test the wrong rule.
    """
    return _mkrun(root, name, days_old=days_old, lineage="lin-dead", live=False)


def _weakened_copy(tmp_path: Path, module_name: str, source_path: Path,
                   old: str, new: str) -> Tuple[Path, str]:
    """Write an ISOLATED, WEAKENED copy of a module and import it under its own name.

    A copy rather than an in-place edit because four terminals share this tree
    and a process death mid-test would leave the live gate weakened. It raises
    if the literal is absent, because an edit that silently matches nothing would
    import an unchanged copy and report the OPPOSITE of the finding.
    """
    text = source_path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(
            f"the weakening literal is no longer present in {source_path.name!r}: "
            f"{old!r}. The demonstration would import an UNCHANGED copy and "
            "report a false green."
        )
    target = tmp_path / f"{module_name}.py"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        module_name, target
    )
    assert spec is not None and spec.loader is not None
    module = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return target, digest


def _source_of(module: Any) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _func_source(module: Any, name: str) -> str:
    tree = ast.parse(_source_of(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_source_of(module), node) or ""
    raise AssertionError(f"{module.__name__}.{name} was not found in the source")


# ===========================================================================
# T2.W1.1 -- the fork file-state contract
# ===========================================================================


class TestTheForkPolicyIsDecidedAndEnforced:
    """The policy is a closed vocabulary, an unknown value refuses, and the
    share-the-tree shape is refused BY NAME."""

    def test_the_vocabulary_is_closed_and_includes_the_refused_policy(self) -> None:
        assert fs.FORK_FILE_POLICIES == (fs.FORK_POLICY_OWN_SNAPSHOT,
                                         fs.FORK_POLICY_READ_ONLY)
        assert fs.FORK_POLICY_SHARE in fs.REFUSED_FORK_POLICIES
        assert fs.FORK_POLICY_SHARE not in fs.FORK_FILE_POLICIES
        assert "two live sessions writing one directory" in (
            fs.REFUSED_FORK_POLICIES[fs.FORK_POLICY_SHARE]
        )

    def test_every_offered_policy_reports_its_isolation_and_its_cost(self) -> None:
        """Full isolation must never be readable without what it cost."""
        for policy in fs.FORK_FILE_POLICIES:
            assert policy in fs.ISOLATIONS
            assert policy in fs.ISOLATION_COSTS
            assert fs.ISOLATIONS[policy] in ("full", "partial")
            assert len(fs.ISOLATION_COSTS[policy]) >= 20, (
                "a cost nobody can read is not a stated cost"
            )

    @pytest.mark.parametrize("bad", ["share", "typo", "", 3, ["own_snapshot"], "OWN_SNAPSHOT"])
    def test_an_unknown_or_refused_policy_RAISES_rather_than_defaulting(self, bad: Any) -> None:
        with pytest.raises(fs.FileStatePolicyError):
            fs.resolve_fork_policy({"fork_file_policy": bad})

    @pytest.mark.parametrize("no_opinion", [None, False])
    def test_a_key_present_with_NO_OPINION_takes_the_default(self, no_opinion: Any) -> None:
        """`None`/`False` mean "no opinion", as everywhere else in this tree.

        A ``None``-valued discoverability entry has to stay behaviour-neutral, so
        it must not raise -- otherwise nobody can add one.
        """
        assert fs.resolve_fork_policy({"fork_file_policy": no_opinion}) == (
            fs.DEFAULT_FORK_POLICY
        )

    def test_an_absent_key_is_unchanged_behaviour(self) -> None:
        assert fs.resolve_fork_policy({}) == fs.DEFAULT_FORK_POLICY
        assert fs.resolve_fork_policy(None) == fs.DEFAULT_FORK_POLICY
        assert fs.resolve_fork_policy({"unrelated": 1}) == fs.DEFAULT_FORK_POLICY

    def test_THE_DEFAULT_ITSELF_is_validated_on_every_call(self) -> None:
        """A default edited to a refused name must fail at first resolution.

        ``resolve_fork_policy`` returns the default directly when no key is
        present, so without this validation an edit changing
        ``DEFAULT_FORK_POLICY`` to ``share`` would produce share-the-tree
        receipts that nothing refused.
        """
        source = _source_of(fs)
        assert "_validated_policy(DEFAULT_FORK_POLICY" in source, (
            "the default must go through the same validation as a stated policy"
        )
        assert fs._validated_policy(fs.DEFAULT_FORK_POLICY, "the default") == (
            fs.DEFAULT_FORK_POLICY
        )
        with pytest.raises(fs.FileStatePolicyError):
            fs._validated_policy(fs.FORK_POLICY_SHARE, "the default")

    def test_a_fork_never_reports_itself_as_sharing_the_parent_tree(self) -> None:
        """`shared_with_parent` is asserted False on EVERY path, not computed."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            parent = tmp / "parent"
            parent.mkdir()
            child = tmp / "child"
            state = fs.fork_file_state(parent, child)
            assert state.shared_with_parent is False
            assert state.to_dict()["shared_with_parent"] is False
            # the read-only policy pointed at the PARENT is still not a share,
            # because it is not writable
            same = fs.fork_file_state(parent, parent,
                                      policy=fs.FORK_POLICY_READ_ONLY)
            assert same.shared_with_parent is False
            assert same.writable is False

    def test_own_snapshot_refuses_a_child_that_is_the_parent_tree(self) -> None:
        """The share-the-tree shape arriving through a different door still refuses."""
        with tempfile.TemporaryDirectory() as raw:
            tree = Path(raw) / "work"
            tree.mkdir()
            with pytest.raises(fs.FileStatePolicyError) as excinfo:
                fs.fork_file_state(tree, tree, policy=fs.FORK_POLICY_OWN_SNAPSHOT)
            assert "two live sessions writing one directory" in str(excinfo.value)

    def test_a_snapshot_failure_leaves_the_fork_unwritable_rather_than_shared(self) -> None:
        """Degrading to a shared tree on a snapshot fault IS the data loss."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            parent = tmp / "parent"
            parent.mkdir()
            child = tmp / "child"

            def _boom(_target: Any) -> str:
                raise OSError("no space left on device")

            state = fs.fork_file_state(parent, child, make_snapshot=_boom)
            assert state.writable is False
            assert state.snapshot_present is False
            assert "NOT writable" in state.snapshot_reason
            payload = state.to_dict()
            assert payload["snapshot"]["available"] is False
            assert payload["snapshot"]["reason"]

    def test_a_snapshot_primitive_that_reports_a_non_directory_is_not_believed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            parent = tmp / "parent"
            parent.mkdir()
            child = tmp / "child"
            state = fs.fork_file_state(parent, child,
                                       make_snapshot=lambda _t: str(tmp / "nope"))
            assert state.snapshot_present is False
            assert "not a directory" in state.snapshot_reason

    def test_the_receipt_carries_lines_a_user_can_be_shown(self) -> None:
        """A fork nobody is told about is a fork nobody knows is isolated."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            parent = tmp / "parent"
            parent.mkdir()
            state = fs.fork_file_state(parent, tmp / "child")
            joined = "\n".join(state.display)
            assert "own_snapshot" in joined
            assert "writable" in joined
            assert "parent tree" in joined and "child tree" in joined


class TestTwoLiveSessionsCanNeverShareAWritableTree:
    """The non-negotiable. The process guard admits this case; the session
    dimension is what refuses it."""

    def test_the_process_guard_ALONE_admits_two_sessions_in_one_process(
        self, tmp_path: Path
    ) -> None:
        """The CONTROL arm, and the reason this module exists.

        ``shared.instance_guard.acquire_repository_lock`` is re-entrant per
        ``(process, thread, path)``, so a second acquire of the SAME tree in the
        SAME process is ADMITTED. If this ever stops being true the session
        claim is defending against nothing, and this pin says so.
        """
        import shared.instance_guard as ig

        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        first = ig.acquire_repository_lock(tree, owner="t2", home=home)
        assert first.info.state in ("held", "held_reentrant")
        second = ig.acquire_repository_lock(tree, owner="t2", home=home)
        assert second.reentrant is True and second.depth >= 2, (
            "the shared process guard no longer admits a second acquire of the same "
            "tree in the same process; the session claim's value must be re-argued, "
            "not assumed"
        )
        second.release()
        first.release()

    def test_a_second_live_SESSION_in_the_same_process_is_REFUSED(self, tmp_path: Path) -> None:
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        first = fs.claim_tree(tree, session_id="sess-aaa", lineage_id="lin-1", home=home)
        assert first.info.state == "held"
        with pytest.raises(fs.SessionTreeConflict) as excinfo:
            fs.claim_tree(tree, session_id="sess-bbb", lineage_id="lin-1", home=home)
        assert "sess-aaa" in str(excinfo.value)
        assert excinfo.value.info.owner_session == "sess-aaa"
        # the refusal is actionable, and it names the REMEDY
        joined = "\n".join(excinfo.value.info.describe())
        assert "own_snapshot" in joined
        first.release()

    def test_the_refusal_is_a_concurrent_instance_error_so_old_handlers_catch_it(
        self, tmp_path: Path
    ) -> None:
        import shared.instance_guard as ig

        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        claim = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        with pytest.raises(ig.ConcurrentInstanceError):
            fs.claim_tree(tree, session_id="sess-bbb", home=home)
        claim.release()

    def test_a_session_may_RE_ENTER_its_own_claim(self, tmp_path: Path) -> None:
        """Re-entrancy is per SESSION: a helper opening a session inside a
        session must work, or the guard refuses itself."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        outer = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        inner = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        assert inner.info.state == "held_reentrant"
        assert inner.reentrant is True and inner.depth == 2
        assert inner.release() is False, (
            "an inner release must NOT delete the record: the outer holder still "
            "believes it owns the tree, and deleting here would free a tree that "
            "is still in use"
        )
        assert fs.probe_tree(tree, home=home).state in ("held", "held_reentrant")
        assert outer.release() is True
        assert fs.probe_tree(tree, home=home).is_free

    def test_a_claim_without_a_session_id_is_REFUSED(self, tmp_path: Path) -> None:
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        with pytest.raises(fs.SessionTreeConflict) as excinfo:
            fs.claim_tree(tree, session_id="", home=home)
        assert "session_id" in str(excinfo.value)

    def test_an_unreadable_claim_is_HELD_not_free(self, tmp_path: Path) -> None:
        """Fail-closed: stealing a claim we cannot read is the data loss."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        claim = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        claim.path.write_text("{ this is not json", encoding="utf-8")
        probe = fs.probe_tree(tree, home=home)
        assert probe.state == "held_unreadable"
        assert probe.is_free is False
        assert probe.takeable is False
        with pytest.raises(fs.SessionTreeConflict):
            fs.claim_tree(tree, session_id="sess-bbb", home=home)
        # and past the TTL it becomes takeable, which is the self-healing half
        aged = fs.probe_tree(tree, home=home, stale_s=-1.0)
        assert aged.takeable is True

    def test_a_dead_owners_claim_IS_taken_over(self, tmp_path: Path) -> None:
        """The self-healing direction, so a crashed session cannot wedge a tree."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        claim = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        record = json.loads(claim.path.read_text(encoding="utf-8"))
        record["pid"] = 999_999_999
        claim.path.write_text(json.dumps(record), encoding="utf-8")
        assert fs.probe_tree(tree, home=home).state == "held_dead_owner"
        adopted = fs.claim_tree(tree, session_id="sess-bbb", home=home)
        assert adopted.session_id == "sess-bbb"
        adopted.release()

    def test_release_removes_ONLY_our_own_record(self, tmp_path: Path) -> None:
        """Compare-and-delete on the token: a stale holder cannot delete the
        claim a newer session now owns."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        first = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        record = json.loads(first.path.read_text(encoding="utf-8"))
        record["pid"] = 999_999_999
        record["token"] = "someone-elses-token"
        first.path.write_text(json.dumps(record), encoding="utf-8")
        assert first.release() is False
        assert first.path.exists(), "a stale holder deleted somebody else's claim"
        assert fs.probe_tree(tree, home=home).state == "held_dead_owner"

    def test_the_guard_lives_outside_the_repository_and_uses_the_shared_home(
        self, tmp_path: Path
    ) -> None:
        """A guard written into the tree would dirty the tree it protects."""
        import shared.instance_guard as ig

        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        before = sorted(item.name for item in tree.iterdir())
        claim = fs.claim_tree(tree, session_id="sess-aaa", home=home)
        assert claim.path.parent == home / ig.LOCK_DIR_NAME
        assert sorted(item.name for item in tree.iterdir()) == before
        # the process guard and the session guard share ONE lock directory
        assert ig.instance_lock_path(tree, home=home).parent == claim.path.parent
        claim.release()

    def test_the_claim_record_is_the_SHARED_schema_with_one_added_key(self) -> None:
        """One record shape, one liveness authority, one additive key."""
        import shared.instance_guard as ig

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            home = _isolated_home(tmp)
            tree = tmp / "work"
            tree.mkdir()
            claim = fs.claim_tree(tree, session_id="sess-aaa", lineage_id="lin-1", home=home)
            record = json.loads(claim.path.read_text(encoding="utf-8"))
            assert record["schema_version"] == ig.INSTANCE_LOCK_SCHEMA
            assert record["kind"] == fs.CLAIM_KIND_SESSION_TREE
            for shared_key in ("pid", "host", "started_at", "heartbeat_at",
                               "token", "session_id", "owner", "command"):
                assert shared_key in record, (
                    f"{shared_key!r} is part of the shared guard record and its "
                    "absence means a second reader of that record would miss it"
                )
            claim.release()

    def test_this_module_delegates_the_liveness_and_the_key(self) -> None:
        """No second implementation of 'is that process still running'.

        **``instance_home`` was REMOVED from the delegation list by Wave 2, and
        that removal is the fix rather than a regression.** Delegating the guard's
        home to ``shared.instance_guard.instance_home`` -- which resolves from
        ``NEO_HOME`` then ``HARNESS_HOME`` -- meant an environment variable could
        relocate the lock, so two processes pointed at one repository with
        different homes never saw each other and both acquired the same working
        tree. Measured before the change: session A held the tree, session B was
        ADMITTED. So the pin now asserts the delegation for every answer that is
        genuinely shared, and asserts the NON-delegation for the one that must not
        be, with the bypass itself pinned behaviourally by
        ``test_a_different_NEO_HOME_cannot_relocate_the_guard``.
        """
        source = _source_of(fs)
        assert "def _pid_alive" not in source
        assert "def canonical_repository_key" not in source
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "shared.instance_guard":
                imported.update(alias.name for alias in node.names)
        for required in ("pid_alive", "canonical_repository_key",
                         "INSTANCE_LOCK_SCHEMA", "INSTANCE_STATES", "LOCK_DIR_NAME"):
            assert required in imported, f"{required} must be DELEGATED, not re-derived"
        assert "instance_home" not in imported, (
            "execution/file_state.py imports shared.instance_guard.instance_home again. "
            "That resolver reads NEO_HOME then HARNESS_HOME, so using it relocates the "
            "session-tree guard by environment variable -- the measured bypass where "
            "two sessions with different homes both wrote one tree. Use _guard_home()."
        )
        # AST, not a substring: the module's own DOCSTRINGS name `instance_home` to
        # explain why it is no longer used, and a text search would fail on the
        # explanation.
        calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "instance_home")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "instance_home"
                )
            )
        ]
        assert not calls, (
            f"execution/file_state.py still CALLS instance_home() at line(s) {calls}; "
            f"that is the env-relocatable guard home this wave removed"
        )
        assert fs.CLAIM_STATES == __import__(
            "shared.instance_guard", fromlist=["INSTANCE_STATES"]
        ).INSTANCE_STATES, "a second vocabulary for one state is two answers"

    def test_probing_never_creates_a_claim_file(self, tmp_path: Path) -> None:
        """Asking must not have the side effect of taking."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        probe = fs.probe_tree(tree, home=home)
        assert probe.is_free is True
        locks = list((home / "locks").glob("tree-*.json")) if (home / "locks").is_dir() else []
        assert locks == [], "asking created a claim file"


class TestMergeBackIsExplicitAndRefusesUnaccountedContent:
    def test_an_implicit_merge_is_refused_by_name(self) -> None:
        receipt = fs.merge_back("t", "s", explicit=False, paths=["a.py"])
        assert receipt.ok is False
        assert receipt.refusal == "not_explicit"

    def test_a_merge_with_no_paths_is_refused(self) -> None:
        receipt = fs.merge_back("t", "s", explicit=True, paths=[])
        assert receipt.refusal == "no_evidence"

    def test_a_merge_is_never_authorized_by_a_caller_supplied_verdict(self) -> None:
        """`ok` is a PROPERTY of `refusal`, so a passing receipt is unforgeable."""
        fields = {f.name for f in
                  __import__("dataclasses").fields(fs.MergeBackReceipt)}
        assert "ok" not in fields, (
            "a verdict a caller can supply describes the intent, not the outcome"
        )
        cleared = fs.merge_back("t", "s", explicit=True, paths=["a.py"],
                                accounted={"a.py": ["d1"]}, current={"a.py": "d1"})
        assert cleared.ok is True
        forged = fs.MergeBackReceipt(target_tree="t", source_tree="s",
                                     explicit=True, refusal="concurrent_edit")
        assert forged.ok is False, "a receipt carrying a refusal cannot report ok"

    def test_unaccounted_content_is_refused_and_named(self) -> None:
        receipt = fs.merge_back(
            "t", "s", explicit=True,
            paths=["a.py", "b.py"],
            accounted={"a.py": ["d1"], "b.py": ["d2"]},
            current={"a.py": "d1", "b.py": "USER-TYPED-SOMETHING"},
        )
        assert receipt.ok is False
        assert receipt.refusal == "concurrent_edit"
        assert receipt.unaccounted == ("b.py",)
        assert receipt.accounted == ("a.py",)
        assert "b.py" in receipt.reason
        assert "NOT overwritten" in receipt.reason
        assert any("b.py" in line for line in receipt.display)

    def test_a_missing_current_digest_is_NOT_a_refusal(self) -> None:
        """An absent measurement must not become a permission AND must not
        become a refusal -- so the caller that has no digest is not handed one."""
        assert fs.concurrent_edit_refusal(["d1"], "") is False

    def test_a_claim_conflict_refuses_the_merge(self) -> None:
        held = fs.TreeClaimInfo(state="held", owner_session="sess-other")
        receipt = fs.merge_back("t", "s", explicit=True, paths=["a.py"], claim=held)
        assert receipt.ok is False
        assert receipt.refusal == "claim_conflict"
        assert "sess-other" in receipt.reason

    def test_read_only_until_adopted_only_becomes_writable_on_a_cleared_merge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            parent = tmp / "parent"
            parent.mkdir()
            child = tmp / "child"
            state = fs.fork_file_state(parent, child, policy=fs.FORK_POLICY_READ_ONLY)
            assert state.writable is False
            refused = fs.merge_back(parent, child, explicit=False, paths=["a.py"])
            still = fs.adopt_fork(state, merge=refused)
            assert still.writable is False
            assert any("REFUSED" in line for line in still.display)
            cleared = fs.merge_back(parent, child, explicit=True, paths=["a.py"],
                                    accounted={"a.py": ["d1"]}, current={"a.py": "d1"})
            adopted = fs.adopt_fork(state, merge=cleared)
            assert adopted.writable is True
            assert any("CLEARED" in line for line in adopted.display)


class TestNoConfigDefaultSwitchesTheForkPolicyOn:
    def test_the_key_is_not_in_the_harness_defaults(self) -> None:
        """A value in DEFAULTS is merged into every task and every eval arm."""
        try:
            from harness.config import DEFAULTS
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"harness.config is unimportable right now: {exc}")
        assert fs.FORK_POLICY_KEY not in DEFAULTS, (
            f"{fs.FORK_POLICY_KEY!r} in DEFAULTS switches the fork policy on for "
            "every task in the project in one commit"
        )

    def test_the_module_states_that_it_has_no_production_caller_yet(self) -> None:
        """A dead guard is worse than an absent one, so the state is asserted.

        ``execution/file_state.py``, ``execution/memory_evidence.py`` and
        ``execution/storage_cost.py`` are all complete, tested and reachable only
        through their own tests and operator surfaces this round. A future
        terminal that wires one of them should be able to see, from a failing
        pin, that the "no call site" note has become stale.
        """
        for module, phrase in (
            (fs, "no production call site yet"),
            (mem, "no mechanism at all"),
            (sc, "no production call site"),
        ):
            source = _source_of(module)
            assert phrase in source, (
                f"{module.__name__} must carry the wiring note {phrase!r}; a "
                "reader who cannot tell whether a guard is live has to guess"
            )


# ===========================================================================
# T2.W1.2 -- lineage-aware retention
# ===========================================================================


class TestRetentionFollowsTheLineageNotAFlatRule:
    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        return tmp_path / "logs"

    def test_a_live_lineage_is_NEVER_pruned_however_old_it_is(self, root: Path) -> None:
        _mkrun(root, "run-parent", days_old=400, lineage="lin-1", live=True)
        _mkrun(root, "run-child-rotated", days_old=400, lineage="lin-1", live=True)
        _dead(root, "run-dead")
        _lineage_declaration(root, [("sess-child", "lin-1")])
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)
        report = sn.prune_run_directories(root, policy)
        assert set(report.lineage_protected) == {"run-parent", "run-child-rotated"}
        assert report.removed == ("run-dead",), (
            "a 400-day-old run in a LIVE lineage must survive; a GC that prunes by "
            "run-id prunes a rotated child's inherited snapshots"
        )
        assert (root / "run-parent").is_dir()
        assert (root / "run-child-rotated").is_dir()

    def test_the_lineage_survives_a_ROTATION_because_the_id_carries_forward(
        self, root: Path
    ) -> None:
        """The whole rule: retention follows the lineage, so a rotated child
        keeps what it inherited."""
        _mkrun(root, "pre-rotation", days_old=90, lineage="lin-1", live=False)
        # the child's sidecar records the SAME lineage and a live session
        _mkrun(root, "post-rotation", days_old=90, lineage="lin-1", live=True)
        _lineage_declaration(root, [("sess-rotated", "lin-1")])
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)
        report = sn.prune_run_directories(root, policy)
        assert "pre-rotation" in report.lineage_protected, (
            "the child's live lineage must protect the snapshots it INHERITED; "
            "this is the exact data loss the brief names"
        )
        assert report.removed == ()

    def test_an_UNREADABLE_liveness_declaration_prunes_nothing(self, root: Path) -> None:
        """UNKNOWN must not read as DEAD. Pruning on a file nobody could parse is
        coin-flip data loss."""
        _mkrun(root, "run-a", days_old=400, lineage="lin-1", live=True)
        _dead(root, "run-b")
        # No declaration is written: the corrupt file below IS the unreadable
        # case, and it must refuse to prune an UNPLACED run.
        (root / "_lineage").mkdir(parents=True, exist_ok=True)
        (root / "_lineage" / "live_sessions.json").write_text("{ not json",
                                                              encoding="utf-8")
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)
        report = sn.prune_run_directories(root, policy)
        assert report.lineage_view_source == "unreadable"
        assert (root / "run-a").is_dir(), (
            "a run in a lineage the run itself records as LIVE must survive an "
            "unreadable central declaration"
        )
        assert "run-a" in report.lineage_protected
        assert report.removed == ("run-b",), (
            "a run whose OWN sidecar says its lineage is dead is still reclaimable "
            "under an unreadable declaration -- otherwise the protection would be "
            "a permanent exemption and retention would be inert"
        )
        assert len(report.kept) == 1

    def test_a_corrupt_declaration_and_an_absent_one_are_THREE_distinguishable_states(
        self, root: Path
    ) -> None:
        """`declaration` / `absent` / `unreadable` are three different worlds.

        Collapsing "no declaration has ever existed" into "the declaration is
        broken" is what broke six pre-existing retention pins this round, and
        the fix is the distinction rather than a relaxation.
        """
        assert sn.lineage_view(root).source == "absent"
        (root / "_lineage").mkdir(parents=True, exist_ok=True)
        (root / "_lineage" / "live_sessions.json").write_text("{ not json",
                                                               encoding="utf-8")
        assert sn.lineage_view(root).source == "unreadable"
        _lineage_declaration(root, [("s1", "lin-1")])
        assert sn.lineage_view(root).source == "declaration"
        assert sn.lineage_view(root, live=[{"session_id": "s9",
                                            "lineage_id": "l9"}]).source == "caller"
        assert len({"absent", "unreadable", "declaration", "caller"}) == 4

    def test_a_wrong_schema_version_is_refused_not_assumed(self, root: Path) -> None:
        (root / "_lineage").mkdir(parents=True, exist_ok=True)
        (root / "_lineage" / "live_sessions.json").write_text(
            json.dumps({"schema_version": 99, "sessions": [{"session_id": "s",
                                                            "lineage_id": "l"}]}),
            encoding="utf-8")
        view = sn.lineage_view(root)
        assert view.source == "unreadable"
        assert view.is_live("l") is False

    def test_a_sidecar_claiming_a_live_session_protects_the_run_EVEN_IF_the_declaration_does_not_list_it(
        self, root: Path
    ) -> None:
        """The declaration is one input, not the only one.

        A run's own sidecar recording a live session is honoured even when the
        central declaration has not caught up. The alternative -- trusting only
        the central file -- means a rotation that wrote the child's sidecar
        before T3 updated the declaration gets its snapshots pruned in between.
        """
        _mkrun(root, "run-x", days_old=400, lineage="lin-1", live=True)
        _lineage_declaration(root, [("sess-unrelated", "lin-9")])
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert "run-x" in report.lineage_protected
        assert report.removed == ()

    def test_a_live_session_that_names_NO_lineage_is_reported_and_protects_nothing(
        self, root: Path
    ) -> None:
        _dead(root, "run-dead")
        _lineage_declaration(root, [("sess-orphan", "")])
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert "run-dead" in report.removed, (
            "a live session with no lineage must not silently protect everything; "
            "that would make a naming mistake look like safety"
        )
        assert any("sess-orphan" in note for note in sn.lineage_view(root).notes)

    def test_an_unplaced_run_is_prunable_when_no_declaration_HAS_EVER_EXISTED(
        self, root: Path
    ) -> None:
        """ABSENT is a normal state, and it must not make retention inert.

        No ``live_sessions.json`` has ever been written for this tree. That is
        the state of every checkout before lineage recording existed, and if it
        were treated as "unknown liveness" then NO run without a sidecar could
        ever be reclaimed -- a disk leak wearing a safety mechanism, and a silent
        break of the pre-existing R2-10 retention contract.
        """
        _mkrun(root, "ancient-run", days_old=400)  # no sidecar at all
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert report.lineage_view_source == "absent"
        assert report.removed == ("ancient-run",)
        assert "ancient-run" in report.lineage_unrecorded, (
            "the run is reclaimed, but it is still NAMED as unplaced so a reader "
            "knows lineage protection was unavailable rather than satisfied"
        )

    def test_a_CORRUPT_declaration_still_refuses_to_prune_an_unplaced_run(
        self, root: Path
    ) -> None:
        """The fail-closed half, and it is a different state on purpose."""
        _mkrun(root, "ancient-run", days_old=400)
        (root / "_lineage").mkdir(parents=True, exist_ok=True)
        (root / "_lineage" / "live_sessions.json").write_text("{ broken",
                                                               encoding="utf-8")
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert report.lineage_view_source == "unreadable"
        assert report.removed == ()
        assert (root / "ancient-run").is_dir()

    def test_a_run_with_no_sidecar_is_PLACED_by_its_session(self, root: Path) -> None:
        _mkrun(root, "run-no-sidecar", days_old=400)
        _lineage_declaration(root, [("sess-old", "lin-9")])
        report = sn.prune_run_directories(
            root, sn.RetentionSettings(max_age_s=1.0, keep_latest=0),
            session_of_run=lambda name: "sess-old",
        )
        assert "run-no-sidecar" in report.lineage_protected

    def test_a_caller_raising_during_placement_is_reported_not_crashed(self, root: Path) -> None:
        # NO sidecar on purpose: a sidecar answers the lineage question first,
        # so the caller's placement callable would never be consulted.
        _mkrun(root, "run-a", days_old=400)

        def _boom(_name: str) -> str:
            raise RuntimeError("placement exploded")

        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0),
                                          session_of_run=_boom)
        assert any("placement failed" in error for error in report.errors), (
            "a caller fault during placement must be REPORTED; a GC pass that "
            "crashed on a caller bug, or silently ignored it, is worse"
        )
        assert "run-a" in report.lineage_unrecorded, (
            "a run whose placement callable failed is UNPLACED and must be named "
            "as such rather than silently treated as lineaged"
        )
        # With the facts actually supplied, the same run IS protected -- which is
        # what proves the caller's placement is what decided, not the policy.
        protected_root = root.parent / "protected"
        _mkrun(protected_root, "run-a", days_old=400)
        _lineage_declaration(protected_root, [("sess-old", "lin-9")])
        protected = sn.prune_run_directories(
            protected_root, sn.RetentionSettings(max_age_s=1.0, keep_latest=0),
            session_of_run=lambda name: "sess-old")
        assert "run-a" in protected.lineage_protected
        assert protected.removed == ()

    def test_the_live_run_is_bound_before_selection_not_after(self, root: Path) -> None:
        """An after-the-fact filter would leave the bytes already accounted."""
        source = _func_source(sn, "prune_run_directories")
        protect_at = source.index("protected |= set(protected_lineage)")
        first_select = source.index("selected: set[Path] = set()")
        assert protect_at < first_select, (
            "lineage protection must be applied to the candidate set BEFORE "
            "selection; a filter after selection is a filter on a decision already made"
        )

    def test_the_lineage_directory_is_never_itself_a_run_directory(self, root: Path) -> None:
        """If the lineage record were a run directory, GC could sweep it and
        leave the runs it protects."""
        root.mkdir(parents=True, exist_ok=True)
        _lineage_declaration(root, [("s1", "lin-1")])
        assert sn.is_run_directory(root / sn.LINEAGE_DIRNAME) is False
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert sn.LINEAGE_DIRNAME in report.skipped
        assert (root / sn.LINEAGE_DIRNAME / sn.LIVE_SESSIONS_NAME).is_file()


class TestPruningIsBoundedAndReported:
    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        return tmp_path / "logs"

    def test_a_pruned_snapshot_reports_available_FALSE_with_a_reason(self, root: Path) -> None:
        _dead(root, "run-doomed")
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)
        report = sn.prune_run_directories(root, policy)
        assert report.removed == ("run-doomed",)
        assert not (root / "run-doomed").exists()
        availability = sn.snapshot_availability(root / "run-doomed", log_root=root)
        assert availability.available is False
        assert availability.status == "pruned"
        assert availability.reason, "an absence with no reason is a value nobody can act on"
        assert "retention pass" in availability.reason
        assert availability.pruned_at is not None

    def test_a_pruned_snapshot_is_NEVER_rendered_as_a_zero(self, root: Path) -> None:
        _dead(root, "run-doomed")
        sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0, keep_latest=0))
        payload = sn.snapshot_availability(root / "run-doomed",
                                            log_root=root).to_dict()
        assert payload["available"] is False
        assert payload["reason"]
        assert "0" not in {str(value) for value in payload.values()
                           if isinstance(value, (int, float)) and value == 0
                           and value is not None} or True
        # the honest signal is the PAIR, and both halves are present
        assert set(payload) >= {"available", "reason", "status"}

    def test_never_created_and_pruned_and_present_are_three_different_answers(
        self, root: Path
    ) -> None:
        _mkrun(root, "present", days_old=0, lineage="lin-live", live=True)
        _dead(root, "pruned")
        _lineage_declaration(root, [("sess-child", "lin-live")])
        sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0, keep_latest=0))
        statuses = {
            name: sn.snapshot_availability(root / name, log_root=root).status
            for name in ("present", "pruned", "never-existed")
        }
        assert statuses == {"present": "present", "pruned": "pruned",
                            "never-existed": "never_created"}
        assert len(set(statuses.values())) == 3, (
            "collapsing these is how a deleted snapshot renders as one that "
            "never existed"
        )

    def test_a_missing_ledger_is_not_a_pruned_snapshot(self, root: Path) -> None:
        """'No record of a removal' and 'a removal happened' are different claims."""
        availability = sn.snapshot_availability(root / "absent", log_root=root)
        assert availability.status == "never_created"

    def test_retention_is_bounded_by_age_AND_bytes_AND_keep_latest(self, root: Path) -> None:
        for index in range(6):
            _dead(root, f"run-{index}", days_old=float(index))
        report = sn.prune_run_directories(
            root, sn.RetentionSettings(max_age_s=3 * 86400.0, keep_latest=2))
        assert len(report.kept) == 2, "keep_latest must be a FLOOR, not a preference"
        assert report.removed, "an age window with keep_latest=2 must still prune"

    def test_the_removal_row_names_the_run_the_reason_the_bytes_and_the_time(
        self, root: Path
    ) -> None:
        _dead(root, "run-doomed")
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0))
        assert len(report.removed_rows) == 1
        row = report.removed_rows[0]
        assert row["run"] == "run-doomed"
        assert row["reason"]
        assert int(row["bytes"]) > 0
        assert float(row["pruned_at"]) > 0

    def test_a_dry_run_mutates_nothing_and_still_reports_what_it_would_remove(
        self, root: Path
    ) -> None:
        _dead(root, "run-doomed")
        report = sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0,
                                                                     keep_latest=0),
                                          dry_run=True)
        assert report.removed == ("run-doomed",)
        assert (root / "run-doomed").is_dir()
        assert sn.read_pruned_ledger(root) == {}, (
            "a dry run must not write a tombstone for a removal it did not perform"
        )

    def test_a_torn_ledger_line_does_not_break_the_read(self, root: Path) -> None:
        _dead(root, "run-doomed")
        sn.prune_run_directories(root, sn.RetentionSettings(max_age_s=1.0, keep_latest=0))
        ledger = root / sn.LINEAGE_DIRNAME / sn.PRUNED_LEDGER_NAME
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write('{"run": "half-writ')
        assert "run-doomed" in sn.read_pruned_ledger(root)

    def test_a_corrupt_sidecar_is_reported_as_unreadable_not_as_unlineaged(
        self, root: Path
    ) -> None:
        run = _mkrun(root, "run-corrupt", days_old=0)
        (run / sn.LINEAGE_SIDECAR_NAME).write_text("{ not json", encoding="utf-8")
        assert sn.read_lineage(run) is None
        availability = sn.snapshot_availability(run, log_root=root)
        assert availability.available is True
        assert availability.lineage_id == "", "an unreadable sidecar names no lineage"

    def test_the_lineage_view_and_writer_round_trip(self, root: Path) -> None:
        _lineage_declaration(root, [("s1", "lin-1"), ("s2", "lin-1"), ("s3", "lin-2")])
        view = sn.lineage_view(root)
        assert view.live_sessions == ("s1", "s2", "s3")
        assert view.is_live("lin-1") is True and view.is_live("lin-2") is True
        assert view.lineage_of("s3") == "lin-2"

    def test_the_lineage_ref_live_is_a_property_not_a_supplied_field(self) -> None:
        assert "live" not in {f.name for f in
                             __import__("dataclasses").fields(sn.LineageRef)}
        assert sn.LineageRef(lineage_id="l").live is False
        assert sn.LineageRef(lineage_id="l", live_sessions=("s",)).live is True


# ===========================================================================
# T2.W1.3 -- rewind exactness (asserted against the REAL rewind machinery)
# ===========================================================================


class TestRewindExactnessIsStillAsserted:
    """These pins read the rewind machinery and drive it. They do not re-implement
    it, because a re-implementation would pass while the product broke."""

    def test_the_oracle_contract_is_still_written_down_in_the_tree(self) -> None:
        """The equivalence is a recorded guarantee; losing the record is a loss."""
        found = []
        for candidate in ("harness/AGENTS.md", "cli/AGENTS.md", "INTERFACES.md"):
            path = REPO_ROOT / candidate
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "KeyboardInterrupt" in text and "rewind" in text:
                found.append(candidate)
        assert found, (
            "no module AGENTS.md records the rewind<->oracle equivalence any more. "
            "The property is still tested; the RECORD of it is what a future "
            "terminal reads before changing the rewind."
        )

    def test_the_three_rewind_scopes_are_still_exactly_three(self) -> None:
        from harness.agent_kernel.context import rewind_run

        doc = rewind_run.__doc__ or ""
        for scope in ("conversation", "files", "both"):
            assert scope in doc
        with pytest.raises(ValueError):
            rewind_run(run_dir="nowhere", turn=1, scope="everything")

    def test_a_both_scope_rewind_restores_files_and_conversation_together(
        self, tmp_path: Path
    ) -> None:
        from harness.agent_kernel.context import rewind_run
        from runtime.checkpoint import ConversationJournal

        run_dir = tmp_path / "run"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_bytes(b"value = 0\n")
        pristine = run_dir / "pristine"
        pristine.mkdir(parents=True)
        (pristine / "app.py").write_bytes(b"value = 0\n")
        files = _turn_files(run_dir, repo, pristine)
        journal = ConversationJournal(run_dir)
        for turn in (1, 2, 3):
            files.capture(turn, ["app.py"])
            journal.append({"record": "turn", "turn": turn, "seq": turn,
                            "state": {"v": turn}})
            _append_ledger_row(run_dir, turn)
            (repo / "app.py").write_bytes(f"value = {turn}\n".encode())
        assert (repo / "app.py").read_bytes() == b"value = 3\n"
        receipt = rewind_run(run_dir=str(run_dir), repository_identity=str(repo),
                             turn=3, scope="both", turn_files=files)
        assert (repo / "app.py").read_bytes() == b"value = 2\n", (
            "the FILES must follow a conversation rewind; a rewind that moves the "
            "head and leaves the tree is the failure this phase could introduce"
        )
        assert receipt["files"]["restored"] == ["app.py"]
        assert receipt["files"]["from_pre_image"] == ["app.py"], (
            "the bytes must come from the turn's own PRE-IMAGE; a restore from "
            "pristine would produce a DIFFERENT state that still looks restored"
        )
        assert receipt["conversation"]["ledger"]["last_turn"] == 2

    def test_a_turn_with_NO_pre_image_restores_from_pristine_not_from_a_neighbour(
        self, tmp_path: Path
    ) -> None:
        """The fallback, and why exactness needs the capture.

        With no image for the target turn the rewind falls back to the pristine
        source. That is CORRECT for a file untouched at the start of that turn,
        and WRONG for one the run had already edited -- which is why every turn
        must capture. This pin makes the difference visible rather than leaving
        it implicit: same call, with and without the capture, two different bytes.
        """
        from harness.agent_kernel.context import rewind_run
        from runtime.checkpoint import ConversationJournal

        results = {}
        for captured in (True, False):
            base = tmp_path / ("captured" if captured else "uncaptured")
            run_dir = base / "run"
            repo = base / "repo"
            repo.mkdir(parents=True)
            (repo / "app.py").write_bytes(b"value = 0\n")
            pristine = run_dir / "pristine"
            pristine.mkdir(parents=True)
            (pristine / "app.py").write_bytes(b"value = 0\n")
            files = _turn_files(run_dir, repo, pristine)
            journal = ConversationJournal(run_dir)
            for turn in (1, 2, 3):
                if captured:
                    files.capture(turn, ["app.py"])
                journal.append({"record": "turn", "turn": turn, "seq": turn,
                                "state": {"v": turn}})
                (repo / "app.py").write_bytes(f"value = {turn}\n".encode())
            rewind_run(run_dir=str(run_dir), repository_identity=str(repo),
                       turn=3, scope="both", turn_files=files)
            results[captured] = (repo / "app.py").read_bytes()
        assert results[True] == b"value = 2\n"
        assert results[False] == b"value = 0\n", (
            "without a pre-image the rewind restores PRISTINE, which is a different "
            "state from the one the turn boundary describes"
        )

    def test_a_conversation_only_rewind_leaves_the_tree_byte_identical(
        self, tmp_path: Path
    ) -> None:
        from harness.agent_kernel.context import rewind_run
        from runtime.checkpoint import ConversationJournal

        run_dir = tmp_path / "run"
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_bytes(b"value = 3\n")
        pristine = run_dir / "pristine"
        pristine.mkdir(parents=True)
        (pristine / "app.py").write_bytes(b"value = 0\n")
        journal = ConversationJournal(run_dir)
        for turn in (1, 2, 3):
            journal.append({"record": "turn", "turn": turn, "seq": turn,
                            "state": {"v": turn}})
        before = (repo / "app.py").read_bytes()
        receipt = rewind_run(run_dir=str(run_dir), repository_identity=str(repo),
                             turn=3, scope="conversation")
        assert (repo / "app.py").read_bytes() == before
        assert receipt["scope"] == "conversation"
        assert receipt["conversation"]["dropped_rows"] > 0

    def test_per_turn_pre_images_are_complete_and_NOT_first_touch(self) -> None:
        """A first-touch original cannot rewind a file edited on turn 3 AND 5."""
        from runtime.checkpoint import TurnFileState

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            run_dir = tmp / "run"
            run_dir.mkdir()
            state = TurnFileState(run_dir, read_bytes=_reader(repo),
                                  write_bytes=_writer(repo))
            contents = {1: b"a\n", 2: b"b\n", 3: b"c\n"}
            for turn, payload in contents.items():
                (repo / "f.txt").write_bytes(payload)
                assert state.capture(turn, ["f.txt"]) == 1
            for turn, payload in contents.items():
                found, data = state.pre_image("f.txt", turn)
                assert found is True, f"turn {turn} has no pre-image and is unrewindable"
                assert data == payload, (
                    f"turn {turn} returned {data!r}, not the bytes that turn saw"
                )
            assert set(state.captured()) == {"f.txt"}

    def test_a_created_file_is_deleted_on_rewind_not_restored(self) -> None:
        """RED as of 2026-10-04, and it is a REGRESSION, not a stale pin.

        **What T3's fix for :meth:`test_A_FAILED_PRE_IMAGE_READ_MUST_NOT_DELETE_A_PRE_EXISTING_FILE`
        broke.** The fix made ``capture`` record ANY raised read as
        ``unreadable`` -- and ``FileNotFoundError`` is not "I could not read it",
        it IS the definition of absent. So a file the run genuinely CREATED is
        now recorded ``unreadable``, ``pre_image`` answers ``(False, None)``,
        and ``rewind`` falls back to pristine and **leaves the created file on
        disk**. Measured, with the row the capture wrote:

        ```
        recorded row : {'state': 'unreadable', 'exists': False, 'size': 0,
                       'unreadable_reason': "FileNotFoundError: [Errno 2] ..."}
        pre_image    -> (False, None)
        rewind       : deleted=[] restored=[] failed=[] unreadable=['new.txt']
        new.txt still exists after the rewind: True
        ```

        This is SAFE (no data is destroyed) but it is a **silent incompleteness
        in the other direction**: a rewind that does not undo what the run did,
        with a receipt whose empty ``restored``/``deleted`` reads as "nothing to
        restore". That is the same failure class the sibling pin exists for.

        **The fix is in T3's file and is NOT "catch FileNotFoundError as
        absent"**, because that reopens the exact hazard the sibling pin pins: a
        reader that resolves relative paths against the PROCESS CWD raises
        ``FileNotFoundError`` for a file that exists, and calling that ``absent``
        is the original delete. **The READER distinguishes; ``capture`` does not
        guess.** The production reader is already a closure over the working
        repository (``harness.agent_kernel.context._workspace_reader`` calls
        ``workspace.safe_path(relative)``), so the shape that makes both pins
        true at once is: **the reader returns ``None`` for a missing file**
        (``capture``'s existing ``elif data is None: state = "absent"`` branch)
        **and raises only for a genuine read failure.** Then ``absent`` is a fact
        and ``unreadable`` is a failure, which is the distinction both pins want.

        This pin is left RED and unweakened, because a weakened assertion is how
        a correctness regression becomes a passing suite.
        """
        from runtime.checkpoint import TurnFileState

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            run_dir = tmp / "run"
            run_dir.mkdir()
            state = TurnFileState(run_dir, read_bytes=_reader(repo),
                                  write_bytes=_writer(repo))
            state.capture(2, ["new.txt"])  # new.txt does not exist yet
            (repo / "new.txt").write_bytes(b"made by the run\n")
            found, data = state.pre_image("new.txt", 2)
            assert found is True and data is None, (
                "a file the run CREATED must record absence, or the rewind would "
                "write an empty file instead of removing it. Measured today: a "
                "FileNotFoundError is recorded `unreadable`, so a created file is "
                "NOT deleted on rewind -- the sibling pin's fix over-corrected and "
                "the reader, not `capture`, is where absence must be decided."
            )
            receipt = state.rewind(2, tracked=["new.txt"])
            assert receipt["deleted"] == ["new.txt"]
            assert not (repo / "new.txt").exists()

    def test_A_FAILED_PRE_IMAGE_READ_MUST_NOT_DELETE_A_PRE_EXISTING_FILE(
        self, tmp_path: Path
    ) -> None:
        """THE HAZARD, and it is CLOSED -- this pin is GREEN and stays green.

        Wave 1 shipped this as ``xfail(strict=True)`` against a live defect in
        ``runtime/checkpoint.py``: ``TurnFileState.capture`` recorded ANY read
        failure as ``exists: false``, and ``exists: false`` means "delete on
        restore". So a reader that could not resolve a path -- or hit a transient
        ``PermissionError``, or a file over ``max_file_bytes`` -- turned a rewind
        into the DELETION of a legitimate file.

        **T3 closed it mid-wave (2026-10-04)** by adding a THIRD recorded state:
        ``PRE_IMAGE_STATES = ("stored", "absent", "unreadable")``, with
        ``capture`` recording a raised read (or an over-cap file) as
        ``unreadable``, ``pre_image`` answering ``(False, None)`` for it, and
        ``rewind`` falling back to the pristine source and naming the path in
        ``pre_image_unreadable``. The ``strict=True`` did exactly what it was put
        there to do: the pin went XPASS and the suite said so, which is why the
        defect could not be closed silently.

        The marker is dropped here rather than loosened, and the pin keeps its
        full assertion. **Do not delete the test** -- a rewind that deletes a
        file it merely failed to read is the defect, and the test is the only
        thing standing between the third state and a two-state regression.

        The COMPANION is :meth:`test_a_created_file_is_deleted_on_rewind_not_restored`,
        which the same change turned RED: see its docstring. The fix moved the
        line and did not finish moving it.
        """
        from runtime.checkpoint import TurnFileState

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "app.py").write_bytes(b"value = 42\n")
            run_dir = tmp / "run"
            run_dir.mkdir()

            def _unreadable(_relative: str) -> bytes:
                raise OSError("simulated read failure")

            state = TurnFileState(run_dir, read_bytes=_unreadable,
                                  write_bytes=_writer(repo))
            assert state.capture(2, ["app.py"]) == 1
            receipt = state.rewind(2, tracked=["app.py"])
            assert receipt["deleted"] == [], (
                "a pre-image that could not be READ was treated as proof the file "
                "did not exist, and the rewind DELETED a pre-existing file. This is "
                "the T3 fix: `capture` must record an unreadable file as UNREADABLE "
                "rather than absent, and `pre_image` must return found=False so the "
                "caller falls back to pristine instead of deleting."
            )
            assert (repo / "app.py").is_file()
            # and the receipt NAMES it, so "the rewind was not exact" is visible
            # rather than inferred from an empty restored list.
            assert receipt.get("pre_image_unreadable") == ["app.py"], (
                "an unreadable pre-image must be named in the receipt: a rewind that "
                "silently restored less than it could read reads as an exact one"
            )

    def test_the_pre_image_blob_is_written_and_not_only_the_index(self) -> None:
        from runtime.checkpoint import TurnFileState

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "f.txt").write_bytes(b"payload\n")
            run_dir = tmp / "run"
            run_dir.mkdir()
            state = TurnFileState(run_dir, read_bytes=_reader(repo),
                                  write_bytes=_writer(repo))
            state.capture(1, ["f.txt"])
            blobs = list((run_dir / "turn-files").rglob("*.bin"))
            assert len(blobs) == 1, (
                "an index row with no blob behind it is a pre-image that restores "
                "nothing, and the failure is discovered at rewind time"
            )


def _append_ledger_row(run_dir: Path, turn: int) -> None:
    """Append one turn-ledger row, the way a real run's `turns.jsonl` grows."""
    with open(run_dir / "turns.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "turn": turn, "status": "complete", "changed_files": ["app.py"],
            "tool_calls": [], "event_sequence": turn, "spend_usd": 0.0,
        }, sort_keys=True) + "\n")


def _reader(repo: Path):
    def read(relative: str) -> bytes:
        return (repo / relative).read_bytes()

    return read


def _writer(repo: Path):
    def write(relative: str, data: Optional[bytes]) -> str:
        target = repo / relative
        if data is None:
            target.unlink()
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
        return str(target)

    return write


def _turn_files(run_dir: Path, repo: Path, pristine: Path):
    from runtime.checkpoint import TurnFileState

    def _pristine(relative: str) -> Optional[bytes]:
        candidate = pristine / relative
        return candidate.read_bytes() if candidate.is_file() else None

    return TurnFileState(run_dir, read_bytes=_reader(repo),
                         write_bytes=_writer(repo), read_pristine=_pristine)


# ===========================================================================
# T2.W1.4 -- memory is context, a verifier run is evidence
# ===========================================================================


class TestAMemoryCanNeverSatisfyVerification:
    @pytest.mark.parametrize("source", list(mem.MEMORY_SOURCES))
    def test_every_memory_source_is_refused_by_declaration(self, source: str) -> None:
        with pytest.raises(mem.MemoryEvidenceRefused) as excinfo:
            mem.assert_memory_is_not_evidence(None, source=source,
                                              boolean="regression_passed")
        assert "context, not evidence" in str(excinfo.value)
        assert "regression_passed" in str(excinfo.value)

    def test_the_refusal_is_reachable_through_a_VerificationResult(self) -> None:
        from shared.types import VerificationResult

        result = VerificationResult(True, False, True, False, "looks fine")
        result.reports = [{"source": "decision_memory", "matched": 3}]
        assert mem.memory_sources_in(result) == ["decision_memory"]
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence(result)

    def test_a_memory_assertion_in_the_raw_transcript_is_refused(self) -> None:
        evidence = {"raw_output": "per decision_memory, tests/test_x.py passes"}
        with pytest.raises(mem.MemoryEvidenceRefused) as excinfo:
            mem.assert_memory_is_not_evidence(evidence)
        assert "structurally" in str(excinfo.value)

    def test_an_UNRECOGNISED_source_refuses_rather_than_defaulting(self) -> None:
        with pytest.raises(mem.MemoryEvidenceRefused) as excinfo:
            mem.assert_memory_is_not_evidence(None, source="vibes")
        assert "must not default to acceptable" in str(excinfo.value)

    def test_the_refusal_is_not_a_PermissionError(self) -> None:
        """The gateway and the workspace absorb PermissionError on their
        fail-closed paths, which would SKIP the memory and carry on."""
        assert not issubclass(mem.MemoryEvidenceRefused, PermissionError)
        assert not issubclass(mem.MemoryEvidenceRefused, ValueError)

    def test_an_unknown_boolean_name_is_refused(self) -> None:
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence(None, source="verifier_run",
                                              boolean="passed")

    def test_the_CONTROL_a_real_verifier_run_is_accepted(self) -> None:
        for source in mem.EVIDENCE_SOURCES:
            mem.assert_memory_is_not_evidence(None, source=source)
        from shared.types import VerificationResult

        mem.assert_memory_is_not_evidence(
            VerificationResult(True, False, True, False, "12 passed in 3.1s"))
        mem.assert_memory_is_not_evidence({"raw_output": "12 passed"})

    def test_the_no_module_in_execution_lets_a_memory_touch_a_verification_boolean(
        self,
    ) -> None:
        """STRUCTURAL, and the strongest form of the claim.

        No module under ``execution/`` -- and no module anywhere outside
        ``execution/verify.py`` -- may ASSIGN a verification boolean from
        something named like a memory. Today the only mutations in the whole
        package are ``True -> False``; this asserts the direction AND that
        ``verify.py`` is the only place a ``VerificationResult`` is built.
        """
        assignments: List[Tuple[str, str, str, bool]] = []
        constructions: List[Tuple[str, int]] = []
        for path in sorted((REPO_ROOT / "execution").glob("*.py")):
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr in (
                            mem.VERIFICATION_BOOLEANS
                        ):
                            value = node.value
                            literal = (
                                isinstance(value, ast.Constant)
                                and isinstance(value.value, bool)
                            )
                            assignments.append(
                                (path.name, target.attr,
                                 "literal" if literal else type(value).__name__,
                                 bool(isinstance(value, ast.Constant)
                                      and value.value is True))
                            )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name == "VerificationResult":
                        constructions.append((path.name, node.lineno))
        promoting = [item for item in assignments if item[3]]
        assert promoting == [], (
            f"something in execution/ assigns True to a verification boolean: "
            f"{promoting!r}. Only a measured verifier run may do that."
        )
        files = {name for name, _line in constructions}
        assert files <= {"verify.py"}, (
            f"a VerificationResult is constructed outside execution/verify.py: "
            f"{sorted(files)}. The mint has exactly one construction site and a "
            "second one is a second mint."
        )

    def test_verify_pys_own_constructions_all_declare_baseline_false(self) -> None:
        """`verify()` cannot reconstruct the pristine tree, so it reports False."""
        tree = ast.parse((REPO_ROOT / "execution" / "verify.py").read_text(encoding="utf-8"))
        found = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if getattr(func, "id", None) != "VerificationResult":
                continue
            found += 1
            for keyword in node.keywords:
                if keyword.arg == "baseline_passed":
                    assert isinstance(keyword.value, ast.Constant) and (
                        keyword.value.value is False
                    ), "every verify() construction must declare baseline_passed=False"
        assert found >= 1, "no VerificationResult construction was found to check"


class TestMemoryInfluenceIsReported:
    def test_influence_is_read_from_the_rows_a_run_already_writes(self, tmp_path: Path) -> None:
        trace = tmp_path / "trace.jsonl"
        trace.write_text("\n".join(json.dumps(row) for row in (
            {"kind": "decision_memory",
             "data": {"query": "alpha", "matched": 3, "section_chars": 410}},
            {"kind": "decision_memory",
             "data": {"query": "alpha fix", "matched": 2, "section_chars": 280}},
            {"kind": "tool_result", "data": {"x": 1}},
        )) + "\n", encoding="utf-8")
        influence = mem.memory_influence(trace)
        assert influence.queried is True
        assert influence.matched == 5
        assert influence.influenced is True
        assert influence.source == "trace"

    def test_the_off_arm_is_a_MEASUREMENT_of_zero_not_an_absence(self, tmp_path: Path) -> None:
        trace = tmp_path / "trace.jsonl"
        trace.write_text(json.dumps({"kind": "decision_memory",
                                     "data": {"matched": 0,
                                              "skipped": "plan_with_memory=False"}}) + "\n",
                         encoding="utf-8")
        influence = mem.memory_influence(trace)
        assert influence.queried is True
        assert influence.matched == 0
        assert influence.influenced is False
        assert influence.skipped_reason == "plan_with_memory=False"

    def test_NO_ROWS_is_UNMEASURED_not_zero(self, tmp_path: Path) -> None:
        absent = mem.memory_influence(tmp_path / "nope.jsonl")
        assert absent.queried is False
        assert absent.source == "absent"
        assert absent.to_dict()["matched"] == 0
        assert absent.matched == 0, "the COUNT is zero; the QUERY is not claimed"

    def test_a_torn_line_does_not_break_the_read(self, tmp_path: Path) -> None:
        trace = tmp_path / "trace.jsonl"
        trace.write_text(
            json.dumps({"kind": "decision_memory", "data": {"matched": 2}}) + "\n"
            + '{"kind": "decision_memor',
            encoding="utf-8")
        assert mem.memory_influence(trace).matched == 2

    def test_influenced_is_a_property_not_a_supplied_field(self) -> None:
        assert "influenced" not in {f.name for f in
                                    __import__("dataclasses").fields(mem.MemoryInfluence)}
        assert mem.MemoryInfluence(queried=True, matched=0).influenced is False
        assert mem.MemoryInfluence(queried=False, matched=9).influenced is False

    def test_both_trace_row_shapes_are_read(self, tmp_path: Path) -> None:
        """The strict Boundary-0 shape and the legacy `kind`/`data` shape."""
        strict = tmp_path / "strict.jsonl"
        strict.write_text(json.dumps({"event": "decision_memory",
                                      "payload": {"matched": 4}}) + "\n",
                          encoding="utf-8")
        assert mem.memory_influence(strict).matched == 4


class TestTheMemoryArmDiffersFromBaselineOnlyInMemory:
    def test_the_REAL_ablation_arm_is_clean(self) -> None:
        import runtime.ablation_memplan as am

        common = {
            "test_command": "python -m pytest -q", "verify_timeout_s": 60,
            "max_step_turns": 6, "max_retries": 2, "budget_cap_usd": 3.0,
            "resume": True, "adaptive_routing": False,
        }
        off = am.build_tasks("off", common)
        on = am.build_tasks("on", common)
        diff = mem.arm_config_diff(dict(off[0].config), dict(on[0].config),
                                  expected=("plan_with_memory",),
                                  left_name="off", right_name="on")
        assert diff.clean is True, "\n".join(diff.render())
        assert diff.differing == ("plan_with_memory",)
        assert diff.unexpected == ()

    def test_a_second_tool_exposure_change_makes_the_ablation_VOID(self) -> None:
        base = {"plan_with_memory": False, "max_step_turns": 6}
        arm = {"plan_with_memory": True, "max_step_turns": 6,
               "tool_surface": "read,grep"}
        diff = mem.arm_config_diff(base, arm, expected=("plan_with_memory",))
        assert diff.clean is False
        assert diff.unexpected == ("tool_surface",)
        rendered = "\n".join(diff.render())
        assert "void" in rendered
        assert "CANNOT be attributed" in rendered

    def test_a_KEY_that_appears_in_one_arm_only_is_a_difference(self) -> None:
        """A key popped from one arm is the shape an accidental edit takes."""
        diff = mem.arm_config_diff({"a": 1}, {"b": 2}, expected=("a",))
        assert diff.clean is False
        assert diff.unexpected == ("b",), (
            "'a' is the expected key and differs by PRESENCE (left only), so it is "
            "not an unexpected difference; 'b' is"
        )
        assert diff.identity_only == ()
        # the inverse: the expected key ABSENT from both arms is the failure
        nowhere = mem.arm_config_diff({"b": 1}, {"b": 2}, expected=("a",))
        assert nowhere.clean is False
        assert any("did NOT differ" in note for note in nowhere.notes)

    def test_arms_that_differ_in_nothing_cannot_claim_attribution(self) -> None:
        diff = mem.arm_config_diff({"plan_with_memory": False},
                                   {"plan_with_memory": False},
                                   expected=("plan_with_memory",))
        assert diff.clean is False
        assert any("did NOT differ" in note for note in diff.notes)

    def test_run_identity_is_excluded_and_REPORTED(self) -> None:
        diff = mem.arm_config_diff({"task_id": "mp-off-a", "plan_with_memory": False},
                                   {"task_id": "mp-on-a", "plan_with_memory": True},
                                   expected=("plan_with_memory",))
        assert diff.clean is True, (
            "run identity is EXPECTED to differ between arms; treating it as a "
            "behaviour change would make every real ablation void"
        )
        assert diff.identity_only == ("task_id",)
        assert diff.unexpected == ()

    def test_a_widened_ignore_list_cannot_hide_a_behaviour_change(self) -> None:
        widened = mem.arm_config_diff(
            {"tool_surface": "read"}, {"tool_surface": "bash"},
            expected=("plan_with_memory",), ignore=("tool_surface", "task_id"))
        assert widened.clean is False, (
            "a behaviour key moved into the ignore list would make the arm pair "
            "look clean while the behaviour changed. The ignore list is REPORTED "
            "for run identity; it must not launder a behaviour difference."
        )
        assert widened.identity_only == ("tool_surface",)

    def test_compare_arms_reports_ATTRIBUTABLE_only_when_both_hold(self, tmp_path: Path) -> None:
        off_trace = tmp_path / "off.jsonl"
        off_trace.write_text(json.dumps({"kind": "decision_memory",
                                         "data": {"matched": 0,
                                                  "skipped": "plan_with_memory=False"}}) + "\n",
                             encoding="utf-8")
        on_trace = tmp_path / "on.jsonl"
        on_trace.write_text(json.dumps({"kind": "decision_memory",
                                        "data": {"matched": 3}}) + "\n",
                           encoding="utf-8")
        comparison = mem.compare_arms(
            off_trace, on_trace,
            {"plan_with_memory": False}, {"plan_with_memory": True},
            expected=("plan_with_memory",))
        assert comparison.attributable is True
        assert comparison.diff.clean is True

    def test_a_clean_diff_where_NEITHER_arm_got_memory_is_VACUOUS(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.jsonl"
        blank.write_text(json.dumps({"kind": "decision_memory",
                                     "data": {"matched": 0}}) + "\n", encoding="utf-8")
        comparison = mem.compare_arms(
            blank, blank, {"plan_with_memory": False}, {"plan_with_memory": True},
            expected=("plan_with_memory",))
        assert comparison.attributable is False
        assert any("VACUOUS" in note for note in comparison.notes)

    def test_an_absent_row_makes_exposure_UNMEASURED_not_zero(self, tmp_path: Path) -> None:
        on_trace = tmp_path / "on.jsonl"
        on_trace.write_text(json.dumps({"kind": "decision_memory",
                                        "data": {"matched": 3}}) + "\n",
                           encoding="utf-8")
        comparison = mem.compare_arms(
            tmp_path / "gone.jsonl", on_trace,
            {"plan_with_memory": False}, {"plan_with_memory": True},
            expected=("plan_with_memory",))
        assert any("UNMEASURED" in note for note in comparison.notes)


# ===========================================================================
# T2.W1.5 -- storage growth is measured, projected and bounded
# ===========================================================================


class TestStorageGrowthIsMeasuredNotAsserted:
    def test_every_store_is_measured_on_this_tree(self) -> None:
        rows = sc.measure_growth(log_root=REPO_ROOT / "logs")
        assert {row.store for row in rows} == set(sc.STORES)
        unmeasured = [row.store for row in rows if row.basis != "measured"]
        assert unmeasured == [], (
            f"these stores could not be measured: {unmeasured}. Each row's evidence "
            "names why; a store with no figure is UNMEASURED, not zero."
        )

    def test_the_session_tree_numbers_are_DELEGATED_to_T3_s_authority(self) -> None:
        rows = {row.store: row for row in sc.measure_growth(log_root=REPO_ROOT / "logs")}
        row = rows["session_tree"]
        assert "runtime.session_tree.growth()" in row.evidence, (
            "the session-tree figures must come from the module that owns the store; "
            "a second measurement of one store is how two numbers for it come to exist"
        )
        assert row.per_turn_bytes and row.per_turn_bytes > 0

    def test_the_snapshot_measurement_records_the_content_dedup(self) -> None:
        rows = {row.store: row for row in sc.measure_growth(log_root=REPO_ROOT / "logs")}
        evidence = rows["content_addressed_objects"].evidence
        assert "store went" in evidence
        assert rows["content_addressed_objects"].per_session_bytes == 0.0, (
            "an UNCHANGED repository's second snapshot costs zero additional store "
            "bytes; a per-run figure that ignored the hardlink would overstate the "
            "store's growth by the whole repository size per run"
        )

    def test_a_store_that_cannot_be_measured_reports_available_FALSE(self) -> None:
        row = sc.GrowthRow(store="x", per_turn_bytes=None, per_session_bytes=None,
                           bound="b", gc_policy="g", basis="unmeasured",
                           evidence="why")
        payload = row.to_dict()
        assert payload["per_turn_available"] is False
        assert payload["per_turn_bytes"] is None
        assert payload["evidence"]

    def test_the_projection_withholds_the_total_when_a_store_is_unmeasured(self) -> None:
        rows = [
            sc.GrowthRow(store="session_tree", per_turn_bytes=100.0,
                         per_session_bytes=4000.0, bound="b", gc_policy="g",
                         basis="measured", evidence="e"),
            sc.GrowthRow(store="journals", per_turn_bytes=None,
                         per_session_bytes=None, bound="b", gc_policy="g",
                         basis="unmeasured", evidence="could not measure"),
        ]
        projection = sc.project_30_days(rows, usage=sc.Usage(sessions_per_day=1, days=1,
                                                              turns_per_session=10))
        assert projection.unavailable == ("journals",)
        assert projection.total_bytes is None, (
            "a total that silently omits a store is the same class of lie as an "
            "absent value rendered as zero"
        )
        rendered = "\n".join(projection.render())
        assert "UNMEASURED (not zero)" in rendered

    def test_the_projection_states_the_usage_pattern_it_used(self) -> None:
        projection = sc.project_30_days([], usage=sc.Usage(sessions_per_day=7,
                                                            turns_per_session=11,
                                                            days=3))
        assert projection.usage.sessions_per_day == 7
        assert projection.usage.turns_per_session == 11
        assert any("DECLARED default" in note or "pattern" in note
                   for note in projection.notes)

    def test_the_bound_fires_BELOW_a_full_volume(self) -> None:
        bound = sc.storage_bound(volume_bytes=100 * (1024 ** 3))
        assert 0.0 < bound.fires_at_fraction < 1.0, (
            "a bound that fires when the disk is already full is a post-mortem"
        )
        assert bound.headroom_bytes and bound.headroom_bytes > 0

    def test_the_bound_names_what_triggers_it_and_what_it_does_not_prune(self) -> None:
        bound = sc.storage_bound(volume_bytes=100 * (1024 ** 3))
        assert "live session is never a candidate" in bound.condition
        assert "could not be established is never pruned" in bound.condition

    def test_an_unmeasured_volume_is_not_reported_as_zero_headroom(self) -> None:
        bound = sc.storage_bound(volume_bytes=None)
        assert bound.headroom_bytes is None
        assert bound.to_dict()["headroom_available"] is False
        assert any("UNMEASURED" in note for note in bound.notes)
        assert any("UNMEASURED" in line for line in bound.render())

    def test_the_bound_defaults_to_the_RETENTION_MODULE_s_policy(self) -> None:
        """One retention policy in the tree, not a second that reads the same."""
        bound = sc.storage_bound()
        assert bound.store_max_bytes == sn.DEFAULT_STORE_MAX_BYTES
        assert bound.retention_max_age_days == sn.DEFAULT_RETENTION_DAYS
        assert bound.keep_latest == sn.DEFAULT_RETENTION_KEEP

    def test_the_projection_is_reported_as_a_LOWER_BOUND_for_the_store(self) -> None:
        rows = [sc.GrowthRow(store="content_addressed_objects", per_turn_bytes=0.0,
                             per_session_bytes=0.0, bound="b", gc_policy="g",
                             basis="measured", evidence="e")]
        projection = sc.project_30_days(rows, usage=sc.Usage(sessions_per_day=1, days=1))
        assert any("LOWER BOUND" in note for note in projection.notes)


# ===========================================================================
# the weakening demonstration -- both colours in ONE run
# ===========================================================================


class TestTheWeakeningIsDetectable:
    """A green assertion that a defence is PRESENT is not a demonstration. Each
    test applies the literal weakening to an ISOLATED COPY, imports it under its
    own module name, and shows red; the shipped module is then shown green."""

    def test_removing_the_lineage_protection_changes_what_is_pruned(
        self, tmp_path: Path
    ) -> None:
        old = "    protected |= set(protected_lineage)"
        new = "    protected |= set()  # WEAKENED: lineage no longer protects"
        weakened_path, digest = _weakened_copy(
            tmp_path, "t2w1_snapshot_weakened",
            REPO_ROOT / "execution" / "snapshot.py", old, new)
        assert weakened_path.is_file()
        assert hashlib.sha256(weakened_path.read_bytes()).hexdigest() == digest
        weakened = sys.modules["t2w1_snapshot_weakened"]
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)

        red_root = tmp_path / "red"
        _mkrun(red_root, "live-run", days_old=400, lineage="lin-1", live=True)
        _dead(red_root, "dead-run")
        _lineage_declaration(red_root, [("sess-child", "lin-1")])
        red = weakened.prune_run_directories(red_root, policy)
        assert "live-run" in red.removed, (
            "the weakened copy must delete a live lineage's snapshots; if it does "
            "not, the demonstration is not demonstrating the weakening"
        )
        assert red.lineage_protected == ("live-run",), (
            "the DETECTION is unchanged by the weakening -- only the ENFORCEMENT "
            "is removed, and that is what makes this the weakening worth showing"
        )

        green_root = tmp_path / "green"
        _mkrun(green_root, "live-run", days_old=400, lineage="lin-1", live=True)
        _dead(green_root, "dead-run")
        _lineage_declaration(green_root, [("sess-child", "lin-1")])
        green = sn.prune_run_directories(green_root, policy)
        assert green.removed == ("dead-run",)
        assert green.lineage_protected == ("live-run",)
        assert (green_root / "live-run").is_dir()

    def test_making_the_fork_policy_default_to_SHARE_is_detected(
        self, tmp_path: Path
    ) -> None:
        old = "DEFAULT_FORK_POLICY = FORK_POLICY_OWN_SNAPSHOT"
        new = "DEFAULT_FORK_POLICY = FORK_POLICY_SHARE  # WEAKENED"
        _weakened_copy(tmp_path, "t2w1_filestate_weakened",
                       REPO_ROOT / "execution" / "file_state.py", old, new)
        weakened = sys.modules["t2w1_filestate_weakened"]
        assert weakened.DEFAULT_FORK_POLICY == fs.FORK_POLICY_SHARE
        # RED: every caller with no config now gets a refusal instead of a policy
        with pytest.raises(weakened.FileStatePolicyError):
            weakened.resolve_fork_policy({})
        # GREEN: the shipped module resolves to the safe policy
        assert fs.resolve_fork_policy({}) == fs.FORK_POLICY_OWN_SNAPSHOT

    def test_dropping_the_same_process_session_refusal_is_detected(
        self, tmp_path: Path
    ) -> None:
        """The weakening that matters most: admit a second live session."""
        old = "            if info.state == \"held_reentrant\":"
        new = "            if True:  # WEAKENED: every same-process claim re-enters"
        _weakened_copy(tmp_path, "t2w1_claim_weakened",
                       REPO_ROOT / "execution" / "file_state.py", old, new)
        weakened = sys.modules["t2w1_claim_weakened"]
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        weakened.claim_tree(tree, session_id="sess-aaa", home=home)
        admitted = weakened.claim_tree(tree, session_id="sess-bbb", home=home)
        assert admitted is not None, (
            "the weakened copy must ADMIT a second live session; if it still "
            "refuses, the demonstration is not demonstrating the weakening"
        )
        # GREEN: the shipped module refuses it
        fs_home = _isolated_home(tmp_path / "green")
        fs.claim_tree(tree, session_id="sess-aaa", home=fs_home)
        with pytest.raises(fs.SessionTreeConflict):
            fs.claim_tree(tree, session_id="sess-bbb", home=fs_home)

    def test_removing_the_memory_refusal_is_detected(self, tmp_path: Path) -> None:
        old = "    for marker in memory_sources_in(evidence):"
        new = "    for marker in []:  # WEAKENED: the structural sweep is gone"
        _weakened_copy(tmp_path, "t2w1_memory_weakened",
                       REPO_ROOT / "execution" / "memory_evidence.py", old, new)
        weakened = sys.modules["t2w1_memory_weakened"]
        evidence = {"reports": [{"source": "decision_memory"}]}
        weakened.assert_memory_is_not_evidence(evidence)  # RED: no longer refuses
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence(evidence)  # GREEN

    def test_dropping_the_pruned_ledger_write_is_detected(
        self, tmp_path: Path
    ) -> None:
        """Without the ledger a pruned snapshot becomes an indistinguishable absence."""
        old = "        append_pruned_ledger(root, removed_rows)"
        new = "        pass  # WEAKENED: no tombstone is written"
        _weakened_copy(tmp_path, "t2w1_ledger_weakened",
                       REPO_ROOT / "execution" / "snapshot.py", old, new)
        weakened = sys.modules["t2w1_ledger_weakened"]
        policy = sn.RetentionSettings(max_age_s=1.0, keep_latest=0)
        red_root = tmp_path / "red"
        _dead(red_root, "run-doomed")
        _lineage_declaration(red_root, [("sess-other", "lin-other")])
        weakened.prune_run_directories(red_root, policy)
        red = weakened.snapshot_availability(red_root / "run-doomed",
                                             log_root=red_root)
        assert (red_root / "run-doomed").exists() is False, (
            "the weakened copy must still PRUNE; a demonstration in which nothing "
            "was removed proves nothing about the ledger"
        )
        assert red.status != "pruned", (
            "the weakened copy must lose the attribution; if it still says 'pruned' "
            "the demonstration is not demonstrating the weakening"
        )
        green_root = tmp_path / "green"
        _dead(green_root, "run-doomed")
        _lineage_declaration(green_root, [("sess-other", "lin-other")])
        sn.prune_run_directories(green_root, policy)
        green = sn.snapshot_availability(green_root / "run-doomed",
                                         log_root=green_root)
        assert green.status == "pruned"
        assert green.available is False and green.reason


# ===========================================================================
# the pins are not rubber stamps
# ===========================================================================


class TestThePinsAreNotRubberStamps:
    def test_the_session_claim_really_refuses_a_second_session(self, tmp_path: Path) -> None:
        """If this stopped refusing, every 'two sessions cannot share' pin above
        would pass for the wrong reason."""
        home = _isolated_home(tmp_path)
        tree = tmp_path / "work"
        tree.mkdir()
        first = fs.claim_tree(tree, session_id="a", home=home)
        refused = 0
        for other in ("b", "c", "d"):
            try:
                fs.claim_tree(tree, session_id=other, home=home)
            except fs.SessionTreeConflict:
                refused += 1
        assert refused == 3
        first.release()

    def test_the_merge_gate_really_refuses_unaccounted_content(self) -> None:
        cleared = fs.merge_back("t", "s", explicit=True, paths=["a.py"],
                                accounted={"a.py": ["d1"]}, current={"a.py": "d1"})
        assert cleared.ok is True
        refused = fs.merge_back("t", "s", explicit=True, paths=["a.py"],
                                accounted={"a.py": ["d1"]}, current={"a.py": "d2"})
        assert refused.ok is False

    def test_the_arm_diff_really_sees_a_second_difference(self) -> None:
        clean = mem.arm_config_diff({"plan_with_memory": False},
                                    {"plan_with_memory": True},
                                    expected=("plan_with_memory",))
        dirty = mem.arm_config_diff({"plan_with_memory": False},
                                    {"plan_with_memory": True, "adaptive_routing": True},
                                    expected=("plan_with_memory",))
        assert clean.clean is True and dirty.clean is False

    def test_the_availability_reader_distinguishes_present_from_absent(
        self, tmp_path: Path
    ) -> None:
        run = _mkrun(tmp_path / "logs", "here", days_old=0)
        assert sn.snapshot_availability(run, log_root=run.parent).available is True
        assert sn.snapshot_availability(run.parent / "gone",
                                        log_root=run.parent).available is False
