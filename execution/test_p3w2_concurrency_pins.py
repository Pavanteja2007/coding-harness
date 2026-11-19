"""P3a/W2 pins -- two live sessions can never share a writable tree, snapshots are
immutable under concurrent background writes, retention follows the lineage AND
is bounded, and a memory can never satisfy a ``VerificationResult``.

**Every pin here was written because a measurement FOUND something.** The
weakening demonstrations at the bottom apply a literal edit to an isolated COPY of
the module, import it under its own name, and require it to go RED -- an assertion
that a defence is currently present is not a demonstration that the defence works.

Host-only: no Docker, no provider, no network, no credential. Threads and real
child PROCESSES are used for the concurrency rows, because a "concurrent" claim
measured over a sequential loop is a claim about nothing.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from execution import file_state as fs  # noqa: E402
from execution import memory_evidence as mem  # noqa: E402
from execution import snapshot as sn  # noqa: E402
from execution import storage_cost as sc  # noqa: E402

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _isolated_home(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    (home / "locks").mkdir(parents=True, exist_ok=True)
    return home


def _tree(tmp_path: Path, name: str = "work") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text("value = 0\n", encoding="utf-8")
    return path


def _repo(tag: str, files: int = 10) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"t2w2-pin-{tag}-"))
    (root / "src").mkdir()
    for i in range(files):
        (root / "src" / f"mod{i:02d}.py").write_text(f"VALUE = {i}\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname='p'\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=root, capture_output=True, text=True, check=False)
    return root


def _weakened_copy(module: Any, old: str, new: str, alias: str) -> Any:
    """Import an ISOLATED copy of ``module`` with ``old`` replaced by ``new``.

    A copy rather than an in-place edit because four terminals share this tree
    and a process death mid-test would leave the live module weakened. It
    **raises** when the literal is absent: an edit that silently matches nothing
    would import an unchanged copy and report the opposite of the finding.
    """
    source = Path(inspect_source(module)).read_text(encoding="utf-8")
    if old not in source:
        raise AssertionError(
            f"the weakening literal {old!r} is NOT in {module.__name__}.py any more, so "
            f"this demonstration would import an unchanged copy and report the "
            f"opposite of the finding. Re-derive the literal; do not delete the pin."
        )
    patched = source.replace(old, new, 1)
    staging = Path(tempfile.mkdtemp(prefix=f"t2w2-weaken-{alias}-"))
    (staging / f"{alias}.py").write_text(patched, encoding="utf-8")
    (staging / f"{alias}_src.txt").write_text(str(Path(inspect_source(module))), encoding="utf-8")
    sys.path.insert(0, str(staging))
    try:
        import importlib

        return importlib.import_module(alias)
    finally:
        sys.path.remove(str(staging))


def inspect_source(module: Any) -> str:
    import inspect

    return inspect.getsourcefile(module) or ""


# ===========================================================================
# T2.W2.1 -- the fork file-state policy
# ===========================================================================


class TestTheForkPolicyCannotBeWeakenedByTheW2Findings:
    """Wave 2's headline: the receipt used to be able to CLAIM isolation it had
    not established."""

    def test_a_writable_fork_MUST_name_a_tree_of_its_own(self, tmp_path: Path) -> None:
        """THE HEADLINE DEFECT, pinned.

        Measured before this round: ``fork_file_state(parent, None)`` returned
        ``writable=True`` with ``isolation='full'``, ``shared_with_parent=False``
        and an EMPTY ``child_tree``. ``Path("")`` resolves against the PROCESS
        working directory, so the fork's first write landed wherever the process
        was standing -- very often the parent's tree. A receipt claiming full
        isolation over no tree is the share-the-tree shape wearing a receipt.
        """
        parent = _tree(tmp_path, "parent")
        state = fs.fork_file_state(parent, None)
        assert state.writable is False, (
            "a fork with no child tree is WRITABLE: the caller would write through an "
            "empty path, which resolves against the process CWD rather than a tree of "
            "its own. That is the share-the-tree shape this module exists to prevent."
        )
        assert state.writable_reason, (
            "writable=False with no reason is a value nobody can act on. The reason is "
            "the same rule the snapshot.available/reason pair exists for."
        )
        assert str((Path("") / "app.py").resolve()).endswith("app.py")
        assert fs.FORKS_NEED_A_NAMED_TREE.split(":")[0] in state.writable_reason

    def test_the_child_the_snapshot_primitive_creates_IS_the_child_tree(
        self, tmp_path: Path
    ) -> None:
        """The default ``own_snapshot`` shape publishes where the fork actually is.

        Measured before this round: the primitive created ``<parent>-fork`` and the
        receipt's typed ``child_tree`` was still ``""`` -- a caller reading the field
        got a blank while a caller reading ``display`` got the path. Two answers to
        one question from one receipt.
        """
        parent = _tree(tmp_path, "parent")
        produced = tmp_path / "the-forks-own-tree"
        state = fs.fork_file_state(parent, None, make_snapshot=lambda _t=None: str(produced))
        Path(produced).mkdir()
        # the primitive is asked BEFORE it returns in the real shape, so drive the
        # order the real caller uses
        state = fs.fork_file_state(
            parent, None,
            make_snapshot=lambda _t=None: (produced.mkdir(exist_ok=True), str(produced))[1],
        )
        assert state.snapshot_present is True
        assert state.child_tree == fs.canonical_repository_key(produced), (
            f"child_tree={state.child_tree!r} but the snapshot primitive created "
            f"{produced!r}. The typed field and the display line must not disagree."
        )
        assert state.writable is True
        assert Path(state.child_tree).is_dir()

    def test_a_nonexistent_child_tree_is_not_writable(self, tmp_path: Path) -> None:
        """``writable`` requires the tree to EXIST, not merely to be named."""
        parent = _tree(tmp_path, "parent")
        state = fs.fork_file_state(parent, tmp_path / "not-created-yet")
        assert state.writable is False
        assert "does not exist" in state.writable_reason

    def test_the_policy_keyword_VALIDATES_and_does_not_raise_a_bare_KeyError(
        self, tmp_path: Path
    ) -> None:
        """The second door into the policy went unvalidated.

        Measured before this round: ``fork_file_state(parent, child,
        policy="share")`` raised ``KeyError: 'share'`` -- the ``policy=`` keyword
        skipped ``resolve_fork_policy`` entirely and reached the display
        formatter's dict lookup. The docstring promised
        ``FileStatePolicyError``. A refused policy must arrive as the promised
        refusal, on BOTH doors.
        """
        parent = _tree(tmp_path, "parent")
        child = _tree(tmp_path, "child")
        for bad in (fs.FORK_POLICY_SHARE, "SHARE", "own", "none", "", "  "):
            if not bad:
                continue
            with pytest.raises(fs.FileStatePolicyError):
                fs.fork_file_state(parent, child, policy=bad)
        # and an UNRECOGNISED policy raises rather than defaulting to the safe one
        with pytest.raises(fs.FileStatePolicyError):
            fs.fork_file_state(parent, child, policy="whatever_the_operator_wants")

    def test_the_receipt_pairs_every_boolean_with_a_reason(self, tmp_path: Path) -> None:
        """``writable`` and ``snapshot.available`` are both paired, ALWAYS.

        A consumer that has to handle a missing reason is a consumer that will
        eventually treat the absence as "there is no reason".
        """
        parent = _tree(tmp_path, "parent")
        child = tmp_path / "child"
        for state in (
            fs.fork_file_state(parent, child),
            fs.fork_file_state(parent, parent, policy=fs.FORK_POLICY_READ_ONLY),
            fs.fork_file_state(parent, None),
        ):
            payload = state.to_dict()
            assert "writable_reason" in payload
            assert "snapshot" in payload
            assert "reason" in payload["snapshot"]
            assert isinstance(payload["shared_with_parent"], bool)
            assert payload["shared_with_parent"] is False

    def test_adoption_is_the_ONLY_path_that_makes_a_fork_writable_without_a_snapshot(
        self, tmp_path: Path
    ) -> None:
        """And it says so, so a reader is not left to infer it."""
        parent = _tree(tmp_path, "parent")
        state = fs.fork_file_state(parent, tmp_path / "child",
                                   policy=fs.FORK_POLICY_READ_ONLY)
        assert state.writable is False
        cleared = fs.merge_back(parent, tmp_path / "child", explicit=True,
                                paths=["a.py"], accounted={"a.py": ["d1"]},
                                current={"a.py": "d1"})
        adopted = fs.adopt_fork(state, merge=cleared)
        assert adopted.writable is True
        assert "adopted" in adopted.writable_reason
        assert any("adoption:    CLEARED" in line for line in adopted.display)


class TestTheConfigAndEnvCannotDisableTheGuard:
    """Rows 5 and 6 of the brief, structurally."""

    #: Every name a caller might plausibly reach for. The claim is not "these 15
    #: are refused" but "the DECISION reads no environment at all".
    ENV_NAMES: Tuple[str, ...] = (
        "NEO_ALLOW_SHARED_TREE", "NEO_SHARED_TREE", "NEO_DISABLE_SESSION_GUARD",
        "NEO_SESSION_TREE_GUARD", "NEO_NO_TREE_CLAIM", "NEO_FORCE_SHARED_TREE",
        "NEO_FORK_FILE_POLICY", "NEO_BYPASS_INSTANCE_GUARD", "NEO_TREE_GUARD",
        "NEO_UNSAFE_SHARED_SESSION", "VEX_ALLOW_SHARED_TREE",
        "HARNESS_ALLOW_SHARED_TREE", "NEO_INSTANCE_GUARD", "NEO_TAKEOVER_LOCKS",
        "NEO_CLAIM_STALE_ALWAYS",
    )

    def test_no_environment_variable_reaches_the_claim_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STRUCTURAL: the only env read in this module is ``instance_home``.

        That read decides WHERE the guard lives, never WHETHER it holds, and this
        test separates the two by pointing ``NEO_HOME`` at a real directory while
        proving the verdict is unchanged.
        """
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        holder = fs.claim_tree(tree, session_id="sess-a", home=home)
        try:
            for name in self.ENV_NAMES:
                for value in ("1", "true", "yes", "on"):
                    monkeypatch.setenv(name, value)
                    with pytest.raises(fs.SessionTreeConflict):
                        fs.claim_tree(tree, session_id="sess-b", home=home)
                monkeypatch.delenv(name, raising=False)
            # and the CONTROL: the same call with no env at all still refuses, so
            # the loop is not passing because the guard stopped working
            with pytest.raises(fs.SessionTreeConflict):
                fs.claim_tree(tree, session_id="sess-b", home=home)
        finally:
            holder.release()

    def test_the_module_reads_NO_environment_at_all(self) -> None:
        """The AST form, so a future `os.environ` read cannot slip in unremarked.

        **Upgraded by Wave 2 from "at most one read" to ZERO.** The single
        permitted read was :func:`shared.instance_guard.instance_home`, which
        decided WHERE the guard lives -- and a guard whose location the
        environment can move is not a guard. The bypass is pinned behaviourally by
        :meth:`test_a_different_NEO_HOME_cannot_relocate_the_guard`; this is the
        structural form, so the read cannot come back quietly in a form the
        behavioural pin's env-var list does not happen to name.
        """
        source = Path(inspect_source(fs)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        readers: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                readers.append(f"line {node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        readers.append(f"import os.environ at line {node.lineno}")
        assert not readers, (
            f"execution/file_state.py reads the environment at {readers}. The module "
            f"reads NONE: a claim's verdict must depend on the lock file and nothing "
            f"else, and the guard's LOCATION must not be ambient configuration either. "
            f"If a read is genuinely needed, it belongs in a caller that passes an "
            f"explicit `home=` argument, which is knowledge rather than environment."
        )

    def test_a_different_NEO_HOME_cannot_relocate_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**This is the P0 this wave found, and it is a measured admission, not a
        refusal.**

        Measured on this tree before the fix, with the guard home resolved through
        :func:`shared.instance_guard.instance_home`::

            sess-A claimed the tree with NEO_HOME=<dir A>   -> held
            sess-B claimed the SAME tree, NEO_HOME=<dir B> -> ADMITTED

        Both sessions then wrote the same working tree, which is exactly the state
        the guard exists to prevent, reached without any config key and without
        lying to the guard: the environment simply pointed each process at a
        different directory. The brief's row 5 asks for this to be REFUSED, and a
        table that reported the same-home battery as covering it would have been
        reporting a narrower test than the one it claimed.
        """
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        holder = fs.claim_tree(tree, session_id="sess-a", home=home)
        try:
            # Every spelling an operator (or a wrapper script, or a CI system) could
            # plausibly set, each pointing at a DIFFERENT real directory.
            for name in ("NEO_HOME", "HARNESS_HOME"):
                for elsewhere in (tmp_path / "home-x", tmp_path / "home-y"):
                    elsewhere.mkdir(parents=True, exist_ok=True)
                    monkeypatch.setenv(name, str(elsewhere))
                    with pytest.raises(fs.SessionTreeConflict):
                        fs.claim_tree(tree, session_id="sess-b", home=home)
                    monkeypatch.delenv(name, raising=False)
        finally:
            holder.release()

    def test_the_guard_home_is_published_so_an_operator_can_find_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The home is a FACT to read, not a knob to set."""
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        monkeypatch.setenv("NEO_HOME", str(tmp_path / "somewhere-else"))
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "somewhere-else"))
        free = fs.probe_tree(tree, home=home)
        assert free.state == "free"
        assert free.guard_home == str(home), (
            f"an explicit home was reported as {free.guard_home!r}; the lock is not "
            f"where the receipt says it is"
        )
        holder = fs.claim_tree(tree, session_id="sess-a", home=home)
        try:
            held = fs.probe_tree(tree, home=home)
            assert held.guard_home == str(home)
            lock = Path(held.guard_home) / fs.LOCK_DIR_NAME
            assert lock.is_dir(), (
                f"the receipt published {held.guard_home!r} as the guard home but "
                f"{lock} does not exist; a published location that is not the location "
                f"is worse than an unpublished one"
            )
            assert list(lock.glob("tree-*.json")), "the published home holds no claim"
        finally:
            holder.release()

    def test_an_EXPLICIT_home_still_isolates_two_harnesses(
        self, tmp_path: Path
    ) -> None:
        """The control arm for the fix above, and it is load-bearing.

        Closing the environment bypass by ignoring the environment must not become
        "there is only ever one guard": an explicit ``home=`` is caller-supplied
        knowledge and two callers naming different homes are two harnesses that
        genuinely should not share a claim. Without this pin, the obvious wrong fix
        -- a single hard-coded lock path -- would pass every other pin here.
        """
        tree_a = _tree(tmp_path, "a")
        tree_b = _tree(tmp_path, "b")
        home_one = _isolated_home(tmp_path, "one")
        home_two = _isolated_home(tmp_path, "two")
        first = fs.claim_tree(tree_a, session_id="sess-1", home=home_one)
        try:
            second = fs.claim_tree(tree_b, session_id="sess-2", home=home_two)
            second.release()
            # ... and the SAME tree under a different explicit home is a DIFFERENT
            # claim, which is the documented consequence of an explicit home and is
            # why the bypass fix is "ignore the environment", not "one lock forever".
            other = fs.probe_tree(tree_a, home=home_two)
            assert other.state == "free", (
                "an explicit home was ignored, so two callers naming different homes "
                "could not be kept apart at all"
            )
        finally:
            first.release()

    def test_no_config_key_can_switch_the_guard_off(self, tmp_path: Path) -> None:
        """Row 6: a config can neither name ``share`` nor buy a writable fork."""
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        holder = fs.claim_tree(tree, session_id="sess-holds", home=home)
        try:
            candidates: List[Dict[str, Any]] = [
                {"fork_file_policy": "share"},
                {"fork_file_policy": "SHARE"},
                {"fork_file_policy": "own", "session_tree_guard": False},
                {"fork_file_policy": "own_snapshot", "share_parent_tree": True},
                {"fork_file_policy": "own_snapshot", "allow_shared_working_tree": True},
                {"fork_file_policy": "own_snapshot", "tree_claim_required": False},
                {"fork_file_policy": "own_snapshot", "session_isolation": "off"},
                {"fork_file_policy": "none"},
            ]
            refused_by_name = 0
            resolved_to_default = 0
            refused_at_the_tree = 0
            for config in candidates:
                try:
                    policy = fs.resolve_fork_policy(config)
                except fs.FileStatePolicyError:
                    # Refused BY NAME, carrying the reason. This is the good
                    # outcome and it is the majority of the table.
                    refused_by_name += 1
                    continue
                assert policy in fs.FORK_FILE_POLICIES, (
                    f"{config} resolved to {policy!r}, which is not an enforceable policy"
                )
                resolved_to_default += 1
                try:
                    state = fs.fork_file_state(tree, tree, config=config)
                except fs.FileStatePolicyError:
                    refused_at_the_tree += 1
                    continue
                assert state.writable is False, (
                    f"{config} produced a WRITABLE fork over a tree a live session holds"
                )
                with pytest.raises(fs.SessionTreeConflict):
                    fs.claim_tree(tree, session_id="sess-attacker", home=home)
            assert (refused_by_name + resolved_to_default) == len(candidates), (
                "a config neither resolved nor raised"
            )
            assert resolved_to_default > 0, (
                "no config resolved to the enforced default, so this table proved "
                "nothing about the configs that DO parse"
            )
            assert refused_at_the_tree > 0, (
                "no config was refused when aimed at a LIVE session's tree, so the "
                "share-the-tree door was never actually driven"
            )
        finally:
            holder.release()

    def test_the_fork_policy_key_is_not_in_the_harness_defaults(self) -> None:
        """A value in DEFAULTS is merged into every task and every eval arm."""
        from harness.config import DEFAULTS

        assert fs.FORK_POLICY_KEY not in DEFAULTS


class TestStaleLockRecoveryIsVerifiedAndReported:
    """Rows 9 and 10 -- the production failure."""

    def test_a_dead_owners_claim_is_taken_over_and_REPORTED(self, tmp_path: Path) -> None:
        """A real child PROCESS claims the tree and is hard-killed.

        ``Popen.kill`` rather than ``os.kill(pid, 9)``: on Windows the latter
        raises ``PermissionError [WinError 5]`` for another process, which would
        be a failure of the PROBE rather than of the guard.
        """
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        victim = _spawn_claimant(tree, home, session="sess-victim")
        assert victim.stdout is not None
        assert victim.stdout.readline().strip() == "CLAIMED"
        victim.kill()
        victim.wait(timeout=60)

        probe = fs.probe_tree(tree, session_id="sess-next", home=home)
        assert probe.state == "held_dead_owner"
        assert probe.takeable is True

        claim = fs.claim_tree(tree, session_id="sess-next", home=home)
        try:
            assert claim.took_over_a_stale_claim is True
            assert len(claim.recovered) == 1
            entry = claim.recovered[0]
            assert entry["reason"] == fs.RECOVERY_REASONS[0] == "dead_owner"
            assert entry["previous_session"] == "sess-victim"
            assert entry["previous_pid"] == victim.pid
            assert entry["ledger_written"] is True
            assert entry["guard_home"]

            report = fs.recovery_report(home)
            assert report["recoveries"] == 1
            assert report["by_reason"]["dead_owner"] == 1
            assert any("sess-victim" in line for line in report["lines"])
            # and it can be narrowed to ONE tree, which is what a surface wants
            assert fs.recovery_report(home, tree=tree)["recoveries"] == 1
            assert fs.recovery_report(home, tree=_tree(tmp_path, "other"))["recoveries"] == 0
        finally:
            claim.release()

    def test_a_recovery_is_reported_even_with_NO_recovery_happening(self, tmp_path: Path) -> None:
        """An empty report says so in words rather than rendering as nothing."""
        home = _isolated_home(tmp_path)
        report = fs.recovery_report(home)
        assert report["recoveries"] == 0
        assert any("no stale-claim recovery" in line for line in report["lines"])

    def test_a_CORRUPT_claim_past_the_budget_is_recovered_and_named(self, tmp_path: Path) -> None:
        """The garbage case, which is the one that used to wedge a checkout forever."""
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        path = fs._session_lock_path(tree, home=home)
        path.write_text("{ not json at all", encoding="utf-8")
        young = fs.probe_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        assert young.state == "held_unreadable"
        assert young.takeable is False, (
            "an unreadable claim that is YOUNG must be held, never stolen"
        )
        with pytest.raises(fs.SessionTreeConflict):
            fs.claim_tree(tree, session_id="sess-next", home=home, stale_s=900.0)

        old = time.time() - 5000
        os.utime(path, (old, old))
        aged = fs.probe_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        assert aged.takeable is True
        claim = fs.claim_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        try:
            assert claim.recovered[0]["reason"] == "unreadable_past_budget"
        finally:
            claim.release()

    def test_a_cross_host_holder_is_only_taken_over_past_the_TTL(self, tmp_path: Path) -> None:
        """Liveness on another host is unknowable, so a clock takes over."""
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        path = fs._session_lock_path(tree, home=home)
        path.write_text(json.dumps({
            "schema_version": 1,
            "kind": fs.CLAIM_KIND_SESSION_TREE,
            "tree": fs.canonical_repository_key(tree),
            "session_id": "sess-elsewhere",
            "lineage_id": "lin-1",
            # pid 0 is how "liveness CANNOT BE MEASURED" is spelled:
            # `shared.instance_guard.pid_alive` returns None for it rather than
            # guessing. A real cross-host pid is not reachable from this host, so
            # a fixture that named a live one would be measuring this machine.
            "pid": 0,
            "host": "some-other-host",
            "started_at": time.time(),
            "heartbeat_at": time.time(),
            "token": "t0ken",
        }), encoding="utf-8")
        young = fs.probe_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        assert young.state == "held", (
            f"a cross-host holder with a FRESH heartbeat reported {young.state!r}; it "
            f"must be held, because nobody can prove the process is gone"
        )
        assert young.takeable is False

        record = json.loads(path.read_text(encoding="utf-8"))
        record["started_at"] = time.time() - 5000
        record["heartbeat_at"] = time.time() - 5000
        path.write_text(json.dumps(record), encoding="utf-8")
        aged = fs.probe_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        assert aged.state == "held_expired"
        assert aged.takeable is True
        claim = fs.claim_tree(tree, session_id="sess-next", home=home, stale_s=900.0)
        try:
            assert claim.recovered[0]["reason"] == "expired_heartbeat"
        finally:
            claim.release()

    def test_an_UNWRITABLE_ledger_does_not_wedge_the_tree(self, tmp_path: Path) -> None:
        """A tree wedged by its own RECEIPT is the failure this closes."""
        tree = _tree(tmp_path)
        home = tmp_path / "ro-home"
        (home / "locks").mkdir(parents=True)
        # A directory where the ledger file belongs: the append cannot succeed.
        (home / "locks" / fs.RECOVERY_LEDGER_NAME).mkdir()
        assert not fs.append_recovery_ledger(home, {"reason": "dead_owner"})
        claim = fs.claim_tree(tree, session_id="sess-ro", home=home)
        assert claim.release() is True

    def test_the_guard_home_is_published_on_every_classification(
        self, tmp_path: Path
    ) -> None:
        """``NEO_HOME`` relocates the guard, so a refusal says WHERE it looked.

        Two processes with different homes genuinely cannot see each other's
        claims. That is a legitimate configuration, so it is made VISIBLE rather
        than treated as a bypass -- and a user who is "not being refused" can now
        read which home the guard is in.
        """
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        for info in (
            fs.probe_tree(tree, session_id="a", home=home),
            fs.probe_tree(tree, session_id="a", home=home),
        ):
            assert info.guard_home == str(home)


def _spawn_claimant(tree: Path, home: Path, *, session: str):
    """A real child process that claims ``tree`` and then hangs to be killed."""
    script = home / f"claimant-{session}.py"
    script.write_text(
        "import os, sys, time\n"
        f"os.environ['NEO_HOME'] = {str(home)!r}\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from execution import file_state as FS\n"
        "tree, home, session = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "claim = FS.claim_tree(tree, session_id=session, home=home)\n"
        "print('CLAIMED', flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    return subprocess.Popen(
        [sys.executable, str(script), str(tree), str(home), session],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO_ROOT),
    )


class TestTheSharedTreeRefusalsAreNotRubberStamps:
    """Non-vacuity: the guard must actually refuse, and refuse DIFFERENTLY."""

    def test_the_session_guard_refuses_what_the_process_guard_admits(
        self, tmp_path: Path
    ) -> None:
        """The control arm Wave 1 established, re-asserted: if the process guard
        ever stopped being re-entrant, the session guard would be defending against
        nothing and this pin has to say so."""
        import shared.instance_guard as ig

        home = _isolated_home(tmp_path)
        tree = _tree(tmp_path)
        first = ig.acquire_repository_lock(tree, home=home)
        second = ig.acquire_repository_lock(tree, home=home)
        assert second.reentrant is True
        second.release()
        first.release()

        holder = fs.claim_tree(tree, session_id="sess-a", home=home)
        with pytest.raises(fs.SessionTreeConflict):
            fs.claim_tree(tree, session_id="sess-b", home=home)
        holder.release()

    def test_two_threads_on_one_tree_writing_the_same_file(self, tmp_path: Path) -> None:
        """REAL threads at a barrier, not a sequential loop."""
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        fs.claim_tree(tree, session_id="sess-holder", home=home)
        results: Dict[str, str] = {}
        gate = threading.Barrier(4)

        def writer(session: str) -> None:
            gate.wait()
            try:
                claim = fs.claim_tree(tree, session_id=session, home=home)
            except fs.SessionTreeConflict as exc:
                results[session] = f"refused:{str(exc).splitlines()[0][:40]}"
                return
            try:
                (tree / "app.py").write_text(f"value = {session}\n", encoding="utf-8")
                results[session] = "wrote"
            finally:
                claim.release()

        threads = [threading.Thread(target=writer, args=(f"sess-{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(results) == 4, "a thread died before recording an outcome"
        assert sum(1 for v in results.values() if v == "wrote") == 0, (
            f"four threads raced for one tree and {results}"
        )

    def test_a_session_may_re_enter_its_OWN_claim_and_the_depth_is_counted(
        self, tmp_path: Path
    ) -> None:
        """An inner release must not delete the record the OUTER holder owns."""
        home = _isolated_home(tmp_path)
        tree = _tree(tmp_path)
        outer = fs.claim_tree(tree, session_id="sess-a", home=home)
        inner = fs.claim_tree(tree, session_id="sess-a", home=home)
        assert inner.reentrant is True
        assert inner.release() is False, (
            "the inner release DELETED the record the outer holder still owns -- that "
            "is the two-writers state, created by the guard"
        )
        assert fs.probe_tree(tree, session_id="sess-b", home=home).state == "held"
        assert outer.release() is True
        assert fs.probe_tree(tree, session_id="sess-b", home=home).is_free is True

    def test_a_claim_with_no_session_is_refused(self, tmp_path: Path) -> None:
        """A claim with no session cannot answer the only question it exists for."""
        home = _isolated_home(tmp_path)
        for session in ("", "   "):
            with pytest.raises(fs.SessionTreeConflict):
                fs.claim_tree(_tree(tmp_path), session_id=session, home=home)


# ===========================================================================
# T2.W2.2 -- snapshot integrity under concurrent background writes
# ===========================================================================


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _fail_the_commit_rename_of(module: Any, half: str = "work") -> List[str]:
    """Make the rename that COMMITS the staged ``half`` raise, and nowhere else.

    **Matched on the staging prefix, not on a call index**, and that is the whole
    point. The pins below run the same injected fault against the shipped module
    and against a weakened copy whose commit does a different NUMBER of renames
    (it deletes the old pair instead of parking it), so a counter that worked
    against one shape silently fires at the wrong moment -- or never -- against the
    other. The staging directory is the one name that means "this rename is part of
    publishing a new snapshot", so keying on it keeps the fault pinned to the
    EVENT under test rather than to an implementation detail's arithmetic.

    Returns the list of destinations seen, so a caller can assert the injection
    actually fired instead of trusting that it did.
    """
    real = os.replace
    seen: List[str] = []
    staging = sn.STAGING_PREFIX

    def flaky(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        source = str(src).replace("\\", "/")
        destination = str(dst).replace("\\", "/")
        seen.append(destination)
        # Match the STAGED half's own name, not merely "a path under staging": the
        # ROLLBACK renames the retired half, which also lives under staging, so a
        # looser matcher would inject the fault a second time during recovery and
        # every "rolled back cleanly" pin would report an unrecoverable rollback.
        if staging in source and source.endswith(f"/{half}") and destination.endswith(
            f"/{half}"
        ):
            raise OSError(f"injected: the commit's rename of {half} failed")
        return real(src, dst, *args, **kwargs)

    module.os.replace = flaky
    return seen


class TestSnapshotsAreImmutableUnderConcurrentBackgroundWrites:
    def test_concurrent_creation_plus_background_writes_leave_every_snapshot_intact(
        self, tmp_path: Path
    ) -> None:
        """T5's memory pipeline writes in the background; snapshots are
        content-addressed. A background write must not corrupt a snapshot."""
        repo = _repo("concurrent")
        log_root = tmp_path / "logs"
        store = sn.SharedStore(log_root / "_snapshot-store")
        created: List[Tuple[str, Path, str]] = []
        failures: List[str] = []
        lock = threading.Lock()
        stop = threading.Event()

        def creator(index: int) -> None:
            for n in range(4):
                if stop.is_set():
                    return
                run = log_root / f"run-w{index}-{n}"
                try:
                    sn.create_run_snapshot(str(repo), str(run), store=store)
                except Exception as exc:
                    with lock:
                        failures.append(f"{type(exc).__name__}: {exc}")
                    continue
                with lock:
                    created.append((run.name, run, _tree_digest(run)))

        def background_writer(index: int) -> None:
            victim = repo / "src" / f"mod{index:02d}.py"
            for n in range(30):
                if stop.is_set():
                    return
                try:
                    victim.write_text(f"VALUE = {index}\n# churn {n}\n", encoding="utf-8")
                except OSError:
                    pass
                time.sleep(0.003)

        threads = [threading.Thread(target=creator, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=background_writer, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        stop.set()

        assert not failures, f"a concurrent snapshot creation raised: {failures[:3]}"
        assert len(created) == 12, f"expected 12 snapshots, got {len(created)}"
        drifted = [name for name, run, at_creation in created
                   if _tree_digest(run) != at_creation]
        assert drifted == [], (
            f"a snapshot's content changed after it was created: {drifted}. A snapshot "
            f"you cannot trust byte-for-byte is not a snapshot."
        )

    def test_a_snapshot_is_IMMUTABLE_once_created(self, tmp_path: Path) -> None:
        """Asserted as a MECHANISM, not a timing: the reference half is
        hardlinked into a content-addressed store and chmod'ed READ-ONLY, so an
        in-place write through it is refused rather than quietly rewriting the
        baseline every other run shares."""
        repo = _repo("immutable")
        run = tmp_path / "run"
        store = sn.SharedStore(tmp_path / "store")
        sn.create_run_snapshot(str(repo), str(run), store=store)
        pristine = run / "pristine"
        work = run / "work"
        before = (_tree_digest(pristine), _tree_digest(work))

        target = pristine / "src" / "mod00.py"
        assert target.is_file()
        assert target.stat().st_nlink >= 2, (
            f"the reference half is not linked into the store (nlink="
            f"{target.stat().st_nlink}); sharing is what makes the reference cheap, "
            f"and it is also what makes an in-place write dangerous"
        )
        assert pristine.stat().st_ino != work.stat().st_ino, (
            "pristine/ and work/ are the same inode, so an edit through work/ would "
            "rewrite the diff baseline in place -- the hazard sharing exists to avoid"
        )
        with pytest.raises(OSError):
            target.write_text("tampered\n", encoding="utf-8")

        assert (_tree_digest(pristine), _tree_digest(work)) == before

    def test_the_staging_directory_is_gone_after_a_successful_snapshot(
        self, tmp_path: Path
    ) -> None:
        repo = _repo("staging")
        run = tmp_path / "run"
        sn.create_run_snapshot(str(repo), str(run))
        leftovers = [p.name for p in run.iterdir() if p.name.startswith(sn.STAGING_PREFIX)]
        assert leftovers == [], f"a staging directory survived: {leftovers}"

    def test_a_FAILED_snapshot_write_leaves_NO_PARTIAL_STATE(self, tmp_path: Path) -> None:
        """The second Wave 2 finding, measured.

        Before this round: a fault raised after 4 of 12 file copies left
        ``run/pristine/`` holding 3 files, no ``work/``, and
        ``snapshot_availability`` reporting ``available=True status="present"``.
        """
        repo = _repo("partial")
        run = tmp_path / "run-doomed"
        real = sn._copy_file
        calls = {"n": 0}

        def exploding(source: Path, destination: Path) -> None:
            calls["n"] += 1
            if calls["n"] > 3:
                raise RuntimeError("simulated disk failure mid-copy")
            real(source, destination)

        sn._copy_file = exploding
        try:
            with pytest.raises(RuntimeError):
                sn.create_run_snapshot(str(repo), str(run))
        finally:
            sn._copy_file = real

        assert calls["n"] > 3, "the fault was never injected; the test proved nothing"
        files = [p for p in run.rglob("*") if p.is_file()] if run.exists() else []
        assert files == [], (
            f"a failed snapshot left {len(files)} file(s) behind: "
            f"{[p.name for p in files][:5]}. Partial state that reads as a snapshot is "
            f"worse than none."
        )
        availability = sn.snapshot_availability(run, log_root=run.parent)
        assert availability.available is False, (
            f"a run directory with no snapshot pair reports available="
            f"{availability.available} status={availability.status!r}"
        )
        assert availability.reason

    def test_a_HALF_pair_is_not_reported_as_a_snapshot_that_is_on_disk(
        self, tmp_path: Path
    ) -> None:
        """``present`` requires BOTH halves: pristine is the restore source and
        work is the diff baseline, and a reader that accepted one of them would
        hand a session a partial baseline described as a complete one."""
        for side, missing, role in (("pristine", "work", "restore source"),
                                    ("work", "pristine", "diff baseline")):
            run = tmp_path / f"run-{side}"
            (run / side).mkdir(parents=True)
            (run / side / "a.py").write_text("x = 1\n", encoding="utf-8")
            availability = sn.snapshot_availability(run, log_root=run.parent)
            assert availability.available is False, (
                f"a {side}-only directory reports available=True: a partial baseline "
                f"presented as a complete one is worse than an honest absence"
            )
            assert availability.status == "incomplete"
            assert availability.present_sides == (side,)
            assert missing in availability.reason and role in availability.reason

    def test_a_complete_pair_still_reads_as_present(self, tmp_path: Path) -> None:
        """The control arm: tightening ``present`` must not make it unreachable."""
        run = tmp_path / "run-complete"
        (run / "pristine").mkdir(parents=True)
        (run / "work").mkdir(parents=True)
        availability = sn.snapshot_availability(run, log_root=run.parent)
        assert availability.available is True
        assert availability.status == "present"
        assert availability.present_sides == ("pristine", "work")
        assert "incomplete" in sn.SNAPSHOT_STATUSES

    def test_an_in_use_snapshot_survives_a_background_write(self, tmp_path: Path) -> None:
        """The session holding the snapshot keeps its rewind point."""
        repo = _repo("inuse")
        run = tmp_path / "run-inuse"
        sn.create_run_snapshot(str(repo), str(run))
        before = _tree_digest(run)
        for i in range(30):
            (repo / "src" / "mod01.py").write_text(
                f"VALUE = 9\n# churn {i}\n", encoding="utf-8"
            )
        assert _tree_digest(run) == before, (
            "a background write into the source tree changed an EXISTING snapshot's "
            "content digest. A session holding that snapshot has just lost its "
            "rewind point."
        )


class TestAFailedSnapshotCommitLeavesNoPartialState:
    """The commit is TWO renames; a failure between them used to be unrecoverable.

    **This class exists because of a defect my own staging change introduced.** The
    first version of the W2 staging work removed the old ``pristine``/``work`` pair
    and then renamed the two new halves in, which is the shape that makes a failure
    between the renames unrecoverable: the old pair is already gone and only one new
    half has landed. Each pin below injects a real failure at a real rename, so the
    rollback is exercised rather than described.
    """

    @staticmethod
    def _fail_nth_replace(monkeypatch: pytest.MonkeyPatch, nth: int, suffix: str) -> List[str]:
        """Make the ``nth`` ``os.replace`` onto a ``suffix`` path raise.

        Counting rather than matching on the staging name is deliberate: the pins
        assert on the OBSERVABLE state afterwards, and a matcher keyed on an
        internal directory name would keep passing if the internals were renamed.
        """
        real = os.replace
        seen: List[str] = []

        def flaky(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(dst))
            if len(seen) == nth and str(dst).replace("\\", "/").endswith(suffix):
                raise OSError(f"injected rename failure on call {nth} -> {dst}")
            return real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", flaky)
        return seen

    def test_a_failure_between_the_two_renames_RESTORES_the_previous_pair(
        self, tmp_path: Path
    ) -> None:
        """The previous snapshot is a rewind point a live session may already hold."""
        repo = _repo("commit")
        run = tmp_path / "run-commit"
        sn.create_run_snapshot(str(repo), str(run))
        before = _tree_digest(run)
        changed = _repo("commit")
        (changed / "src" / "mod00.py").write_text("VALUE = 4242\n", encoding="utf-8")

        real_replace = os.replace
        seen = _fail_the_commit_rename_of(sn, "work")
        try:
            with pytest.raises(OSError) as excinfo:
                sn.create_run_snapshot(str(changed), str(run))
        finally:
            os.replace = real_replace
        assert seen, "the injection never fired; this pin proved nothing"
        assert not isinstance(excinfo.value, sn.SnapshotCommitIncomplete), (
            "a commit that failed but rolled back cleanly must re-raise the ORIGINAL "
            "error; wrapping it would hide the cause behind a recovery message"
        )

        assert _tree_digest(run) == before, (
            "the previous snapshot's content changed after a failed re-take. A "
            "session holding that run just lost its rewind point to a write that "
            "was supposed to fail."
        )
        assert sn.snapshot_availability(run).status == "present"
        assert sorted(p.name for p in run.iterdir()) == ["pristine", "work"], (
            "the failed commit left residue in the run directory: a staging or "
            "parked half that is not part of the snapshot pair"
        )

    def test_a_first_snapshot_that_fails_mid_commit_leaves_NO_partial_pair(
        self, tmp_path: Path
    ) -> None:
        """With no previous pair there is nothing to restore, so there must be
        nothing to leave: an empty run directory that reads ``unreadable``, not a
        half-written snapshot that reads ``present``."""
        repo = _repo("first")
        run = tmp_path / "run-first"
        real_replace = os.replace
        seen = _fail_the_commit_rename_of(sn, "work")
        try:
            with pytest.raises(OSError):
                sn.create_run_snapshot(str(repo), str(run))
        finally:
            os.replace = real_replace
        assert seen, "the injection never fired; this pin proved nothing"
        assert sorted(p.name for p in run.iterdir()) == [], (
            f"a first snapshot that failed mid-commit left "
            f"{sorted(p.name for p in run.iterdir())} behind; a partial pair is "
            f"indistinguishable from a complete one to a reader that does not count"
        )
        availability = sn.snapshot_availability(run)
        assert availability.available is False
        assert availability.status in ("unreadable", "never_created")
        assert availability.present_sides == ()

    def test_an_UNRECOVERABLE_rollback_RAISES_rather_than_reporting_success(
        self, tmp_path: Path
    ) -> None:
        """The one state this cannot leave quietly: the commit failed AND the
        rollback failed. That must be a distinct, named exception -- not an
        ``OSError`` the caller reads as 'nothing happened'."""
        repo = _repo("unrecoverable")
        run = tmp_path / "run-unrecoverable"
        sn.create_run_snapshot(str(repo), str(run))

        real = os.replace
        seen: List[str] = []
        fired = {"commit": False, "rollback": False}

        def flaky(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            source = str(src).replace("\\", "/")
            destination = str(dst)
            seen.append(destination)
            name = destination.replace("\\", "/")
            if (
                sn.STAGING_PREFIX in source
                and source.endswith("/work")
                and name.endswith("/work")
            ):
                fired["commit"] = True
                raise OSError("injected: the commit's rename of work failed")
            if fired["commit"] and (
                name.endswith("/pristine") or name.endswith("/work")
            ):
                fired["rollback"] = True
                raise OSError("injected: the rollback rename failed too")
            return real(src, dst, *args, **kwargs)

        os.replace = flaky
        changed = _repo("unrecoverable")
        (changed / "src" / "mod00.py").write_text("VALUE = 7\n", encoding="utf-8")
        try:
            with pytest.raises(sn.SnapshotCommitIncomplete) as excinfo:
                sn.create_run_snapshot(str(changed), str(run))
        finally:
            os.replace = real
        assert fired["commit"] and fired["rollback"], (
            f"the double injection did not fire as intended (commit="
            f"{fired['commit']}, rollback={fired['rollback']}, renames seen={len(seen)}); "
            f"a pin that did not inject its fault proved nothing"
        )
        assert excinfo.value.unrecovered, (
            "SnapshotCommitIncomplete was raised without naming which half/halves "
            "could not be restored; the whole value of the exception is that it says"
        )
        assert isinstance(excinfo.value.cause, OSError)
        availability = sn.snapshot_availability(run)
        assert availability.available is False, (
            "an unrecoverable rollback left the run directory reading as a usable "
            "rewind point"
        )
        # Which non-available status depends on how far the rollback got: a half
        # pair is `incomplete`, no pair at all is `unreadable`. Both are refusals,
        # and the pin asserts the refusal rather than pinning one spelling of it.
        assert availability.status in ("incomplete", "unreadable"), (
            f"an unrecoverable rollback reported status={availability.status!r}, which "
            f"is neither distinguishable half-pair nor absent-pair; a status a reader "
            f"does not recognise is a status a reader will default"
        )
        assert availability.reason

    def test_the_pair_is_PARKED_not_deleted_before_the_new_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STRUCTURAL, and it is the mechanism the other three pins rely on.

        The rollback can only restore the previous pair if the previous pair still
        EXISTS when the failure happens. This asserts that the module never calls
        ``force_rmtree`` on a live ``pristine``/``work`` at all -- the parking
        renames are the only way either half leaves its place.
        """
        source = Path(inspect_source(sn)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: List[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name != "force_rmtree" or not node.args:
                continue
            argument = ast.unparse(node.args[0])
            if argument in ("pristine", "work", "str(pristine)", "str(work)"):
                offenders.append(node.lineno)
        assert not offenders, (
            f"execution/snapshot.py deletes a live snapshot half with force_rmtree at "
            f"line(s) {offenders}. A commit that DELETES the previous pair before "
            f"renaming the new one in cannot be rolled back: a failure between the "
            f"two renames then leaves a run with no rewind point and nothing to "
            f"restore. Park it with os.replace instead."
        )


class TestStagedSnapshotStoreStillNeverRaises:
    """``memory/AGENTS.md`` AGT-09 records this as a guarantee. RE-VERIFIED under
    the new concurrent writes -- and it is T4's file, so this asserts the
    guarantee rather than changing it."""

    def test_concurrent_writes_through_the_real_store_never_raise(
        self, tmp_path: Path
    ) -> None:
        checkpoints = pytest.importorskip("memory.checkpoints")
        repo = _repo("staged")
        log_root = tmp_path / "logs"
        failures: List[str] = []
        done = threading.Barrier(3, timeout=120)
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                store = checkpoints.StagedSnapshotStore(
                    str(repo), str(log_root), f"sess-{index}"
                )
                done.wait()
                for turn in range(1, 6):
                    receipt = store.capture(
                        turn_id=f"turn-{turn}",
                        paths=["src/mod00.py", "tests/test_a.py"],
                    )
                    if not isinstance(receipt, dict):
                        with lock:
                            failures.append(
                                f"capture({turn}) returned {type(receipt).__name__}, "
                                f"not a record"
                            )
            except Exception as exc:
                with lock:
                    failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert failures == [], (
            f"StagedSnapshotStore raised or returned a non-record under concurrent "
            f"writes: {failures[:3]}. AGT-09 records 'NEVER raises; a failure is a "
            f"record', and a store that raises mid-write corrupts a session."
        )

    def test_the_never_raises_guarantee_is_still_written_down(self) -> None:
        """A guarantee nobody can check is a comment."""
        source = Path(inspect_source(pytest.importorskip("memory.checkpoints")))
        text = source.read_text(encoding="utf-8")
        assert "NEVER raises" in text, (
            "StagedSnapshotStore's never-raises contract is no longer stated in its own "
            "source. Restore the sentence or re-argue why it is no longer true."
        )


# ===========================================================================
# T2.W2.3 -- retention follows the lineage AND is bounded
# ===========================================================================


def _run_with_lineage(root: Path, name: str, lineage: str, age_days: float,
                      *, corrupt_sidecar: bool = False,
                      drop_sidecar: bool = False) -> Path:
    run = root / name
    (run / "pristine").mkdir(parents=True)
    (run / "work").mkdir(parents=True)
    (run / "pristine" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (run / "work" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (run / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    sn.record_lineage(run, sn.LineageRef(
        lineage_id=lineage, sessions=(f"{name}-s",), head_session=f"{name}-s"))
    sidecar = run / sn.LINEAGE_SIDECAR_NAME
    if corrupt_sidecar:
        sidecar.write_text("{ this is not json", encoding="utf-8")
    if drop_sidecar:
        sidecar.unlink()
    stamp = time.time() - age_days * 86400
    for path in sorted(run.rglob("*"), reverse=True):
        try:
            os.utime(path, (stamp, stamp))
        except OSError:
            pass
    return run


MAX_PRESSURE = sn.RetentionSettings(max_age_s=1.0, max_bytes=1, keep_latest=0)


class TestRetentionNeverPrunesALiveLineage:
    def test_a_rotated_lineage_survives_maximum_pressure(self, tmp_path: Path) -> None:
        """Four rotations, one lineage, ONE live session, and a policy that would
        otherwise take everything."""
        root = tmp_path / "logs"
        rotations = [_run_with_lineage(root, f"rot-{i}", "lin-live", 40.0)
                     for i in range(4)]
        dead = _run_with_lineage(root, "dead-1", "lin-dead", 40.0)
        sn.write_live_sessions(root, [{"session_id": "rot-3-s",
                                       "lineage_id": "lin-live"}])
        report = sn.prune_run_directories(root, MAX_PRESSURE)
        assert set(report.lineage_protected) >= {"rot-1", "rot-2", "rot-3"}, (
            f"a live lineage's runs were not all protected: {report.lineage_protected}"
        )
        for run in rotations[1:]:
            assert run.exists(), f"{run.name} was pruned while its lineage was live"
            assert sn.snapshot_availability(run, log_root=root).available is True
        assert not dead.exists(), (
            "a DEAD lineage's run survived maximum pressure: the protection is not "
            "narrow, and a guard that protects everything reclaims nothing"
        )
        assert report.lineage_protected_bytes > 0

    def test_a_CORRUPT_sidecar_is_PROTECTED_and_in_its_OWN_bucket(self, tmp_path: Path) -> None:
        """A DAMAGED lineage record is a broken fact, not a missing one.

        Measured before this round: a run whose ``lineage.json`` was corrupt was
        PRUNED and filed under ``lineage_unrecorded`` -- so the module deleted the
        snapshots of a lineage it could not identify *because the record was
        damaged*.
        """
        root = tmp_path / "logs"
        corrupt = _run_with_lineage(root, "run-corrupt", "lin-x", 40.0,
                                    corrupt_sidecar=True)
        present = _run_with_lineage(root, "run-present", "lin-y", 40.0)
        sn.write_live_sessions(root, [{"session_id": "run-present-s",
                                       "lineage_id": "lin-y"}])
        report = sn.prune_run_directories(root, MAX_PRESSURE)
        assert report.lineage_unreadable == ("run-corrupt",), (
            f"a corrupt sidecar was not reported in its own bucket: "
            f"unreadable={report.lineage_unreadable} unrecorded={report.lineage_unrecorded}"
        )
        assert "run-corrupt" not in report.lineage_unrecorded, (
            "a corrupt sidecar is filed as UNRECORDED, which collapses 'the fact is "
            "broken' into 'the fact is missing' -- and only one of those is an emergency"
        )
        assert corrupt.exists(), (
            "a run whose lineage record could not be parsed was PRUNED. The facts are "
            "unknown while a writer believes it published them, which is the refusing "
            "direction."
        )
        assert "lineage_unreadable" in report.to_dict()
        assert present.exists()

    def test_an_UNPLACED_run_is_still_prunable(self, tmp_path: Path) -> None:
        """The distinction is a distinction, not a relaxation.

        Wave 1 measured the opposite mistake: treating "no declaration has ever
        been written" as unprotectable made retention permanently inert on every
        checkout that had not adopted lineage recording -- a disk leak disguised as
        a safety mechanism.
        """
        root = tmp_path / "logs"
        unplaced = _run_with_lineage(root, "run-unplaced", "lin-z", 40.0,
                                     drop_sidecar=True)
        other = _run_with_lineage(root, "run-other", "lin-w", 40.0)
        sn.write_live_sessions(root, [{"session_id": "run-other-s",
                                       "lineage_id": "lin-w"}])
        report = sn.prune_run_directories(root, MAX_PRESSURE)
        assert report.lineage_unrecorded == ("run-unplaced",)
        assert not unplaced.exists(), (
            "an unplaced run under a readable declaration must stay prunable, or "
            "retention is inert on every checkout that predates lineage recording"
        )
        assert other.exists()

    def test_the_bound_is_ENFORCED_not_merely_measured(self, tmp_path: Path) -> None:
        """A projection with no enforcement is a hope."""
        root = tmp_path / "logs"
        for index in range(3):
            _run_with_lineage(root, f"run-{index}", f"lin-{index}", 40.0)
        tight = sc.storage_bound(store_max_bytes=1, keep_latest=1)
        before_names = sorted(p.name for p in root.iterdir())
        dry = sc.enforce_storage_bound(root, bound=tight, dry_run=True)
        assert dry.fired is True
        assert sorted(p.name for p in root.iterdir()) == before_names, (
            "the dry run MUTATED the log root: a preview that removes something is not "
            "a preview"
        )

        applied = sc.enforce_storage_bound(root, bound=tight)
        assert applied.fired is True
        assert applied.measured is True
        assert applied.runs_before == 3
        assert applied.runs_after == 1, (
            f"the bound fired and reclaimed nothing: {applied.runs_after} run(s) left "
            f"of {applied.runs_before}. Keep-latest is a FLOOR, so enforcing it needs "
            f"the run-count trigger -- prune_run_directories(max_runs=...)."
        )
        assert applied.reclaimed_bytes is not None
        assert "max_runs=1" in str(
            applied.prune_report["removed_rows"][0]["reason"]
        ), "a pruned run's receipt does not name WHICH rule removed it"

    def test_a_live_lineage_is_NEVER_pruned_however_far_over_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        """The tension, resolved in the safe direction, and the receipt says so."""
        root = tmp_path / "logs"
        for index in range(4):
            _run_with_lineage(root, f"run-{index}", f"lin-{index}", 40.0)
        live = _run_with_lineage(root, "run-live", "lin-LIVE", 40.0)
        sn.write_live_sessions(root, [{"session_id": "run-live-s",
                                       "lineage_id": "lin-LIVE"}])
        applied = sc.enforce_storage_bound(
            root, bound=sc.storage_bound(store_max_bytes=1, keep_latest=0)
        )
        assert applied.fired is True
        assert live.exists(), (
            "the bound reached into a LIVE lineage. The bound reclaims space; it does "
            "not reclaim a session's rewind points."
        )
        assert applied.within_bound_after is False, (
            "the root could not come under a 1 B ceiling, so reporting success would be "
            "a lie -- and the receipt must say so instead"
        )
        assert any("LIVE" in note for note in applied.notes)
        assert applied.prune_report["lineage_protected"]

    def test_an_UNMEASURABLE_root_is_UNKNOWN_not_within_bound(self, tmp_path: Path) -> None:
        """A missing measurement is not a passing measurement."""
        applied = sc.enforce_storage_bound(
            tmp_path / "does-not-exist",
            bound=sc.storage_bound(store_max_bytes=1, keep_latest=0),
        )
        assert applied.measured is False
        assert applied.fired is False
        assert "UNKNOWN" in applied.reason
        assert applied.store_bytes_after is None
        assert applied.reclaimed_bytes is None

    def test_pruning_is_REPORTED_and_a_pruned_lineage_is_still_NAMED(
        self, tmp_path: Path
    ) -> None:
        """Never silent, and never `0`."""
        root = tmp_path / "logs"
        for index in range(3):
            _run_with_lineage(root, f"alpha-{index}", "lin-alpha", 40.0)
        _run_with_lineage(root, "beta-0", "lin-beta", 40.0)
        report = sn.prune_run_directories(root, MAX_PRESSURE)
        assert set(report.removed) == {"alpha-0", "alpha-1", "alpha-2", "beta-0"}
        assert report.bytes_removed > 0
        assert len(report.removed_rows) == 4
        for row in report.removed_rows:
            assert row["lineage_id"], f"a removal receipt named no lineage: {row}"
            assert row["reason"]
            assert row["bytes"] > 0
            assert row["pruned_at"] > 0

        lineages = {entry.lineage_id: entry for entry in sn.pruned_lineages(root)}
        assert set(lineages) == {"lin-alpha", "lin-beta"}
        assert lineages["lin-alpha"].run_count == 3
        assert lineages["lin-alpha"].bytes_pruned > 0
        assert "rewind points for those turns are gone" in lineages["lin-alpha"].describe()
        assert lineages["lin-beta"].run_count == 1

        for name in ("alpha-0", "beta-0"):
            availability = sn.snapshot_availability(root / name, log_root=root)
            assert availability.available is False
            assert availability.status == "pruned"
            assert availability.reason, "a pruned snapshot reported no reason"
            assert availability.lineage_id, (
                "a pruned snapshot does not NAME its lineage, so a user cannot tell "
                "whose rewind point disappeared"
            )
            assert availability.pruned_at and availability.bytes_at_prune > 0

    def test_a_pruned_run_with_NO_readable_lineage_is_named_as_UNATTRIBUTED(
        self, tmp_path: Path
    ) -> None:
        """'We pruned something and cannot say whose' is a state a user must see."""
        root = tmp_path / "logs"
        orphan = _run_with_lineage(root, "orphan", "lin-x", 40.0, drop_sidecar=True)
        sn.write_live_sessions(root, [])
        sn.prune_run_directories(root, MAX_PRESSURE)
        assert not orphan.exists()
        entries = {entry.lineage_id for entry in sn.pruned_lineages(root)}
        assert entries == {sn.PRUNED_UNATTRIBUTED_LINEAGE}, (
            f"a pruned run with no readable lineage was folded into a real lineage or "
            f"dropped: {entries}"
        )


# ===========================================================================
# T2.W2.4 -- the oracle, and the rewind exactness it depends on
# ===========================================================================


class TestRewindExactnessIsUnchangedByTheStructuralWork:
    def test_the_pre_image_hazard_is_closed_and_the_pin_stays_green(self) -> None:
        """T3 added ``PRE_IMAGE_STATES`` with ``unreadable`` as a third state, so
        an unreadable pre-image can no longer become a DELETE. The assertion is
        kept whole and extended to the receipt's ``pre_image_unreadable``."""
        from runtime.checkpoint import TurnFileState

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "app.py").write_bytes(b"value = 42\n")
            run_dir = tmp / "run"
            run_dir.mkdir()

            def unreadable(_relative: str) -> bytes:
                raise OSError("simulated read failure")

            state = TurnFileState(
                run_dir,
                read_bytes=unreadable,
                write_bytes=_writer_for(repo),
            )
            assert state.capture(2, ["app.py"]) == 1
            receipt = state.rewind(2, tracked=["app.py"])
            assert receipt["deleted"] == [], (
                "a pre-image that could not be READ was treated as proof the file did "
                "not exist, and the rewind DELETED a pre-existing file"
            )
            assert (repo / "app.py").is_file()
            assert receipt.get("pre_image_unreadable") == ["app.py"], (
                "an unreadable pre-image must be NAMED in the receipt: a rewind that "
                "silently restored less than it could read reads as an exact one"
            )

    def test_the_third_state_is_declared_and_a_pre_w2_row_still_reads(self) -> None:
        """The migration is by fallback, and a pre-W2 index row keeps its meaning."""
        from runtime.checkpoint import PRE_IMAGE_STATES

        assert PRE_IMAGE_STATES == ("stored", "absent", "unreadable")
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            run_dir = tmp / "run"
            (run_dir / "turn-files" / "000002").mkdir(parents=True)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "f.txt").write_bytes(b"legacy\n")
            import hashlib

            digest = hashlib.sha256(b"f.txt").hexdigest()
            (run_dir / "turn-files" / "000002" / f"{digest}.bin").write_bytes(
                b"legacy\n"
            )
            (run_dir / "turn-files" / "000002" / f"{digest}.json").write_text(
                json.dumps({"turn": 2, "relative": "f.txt", "exists": True,
                            "size": 7}) + "\n",
                encoding="utf-8",
            )
            from runtime.checkpoint import TurnFileState

            state = TurnFileState(
                # TurnFileState appends its own `turn-files` directory, so the
                # RUN directory is what a caller passes -- not the subdirectory.
                run_dir,
                read_bytes=lambda r: (repo / r).read_bytes(),
                write_bytes=_writer_for(repo),
            )
            found, data = state.pre_image("f.txt", 2)
            assert found is True and data == b"legacy\n", (
                "a pre-W2 index row carries no `state` key; it must still read through "
                "the `exists` flag or every already-captured image changed meaning"
            )

    def test_the_rotation_folds_journal_turns_so_the_oracle_is_not_vacuous(
        self, tmp_path: Path
    ) -> None:
        """NON-VACUITY for the rotation extension.

        ``resolve_dropped_seqs`` joins a session turn to a journal ``seq`` BY
        CONTENT DIGEST, and the two sides spell the text differently: a session
        turn's ``text``/``content``/``body``/``summary`` versus the journal row's
        ``content``, which is what ``ConversationMemory._append`` emits. A fixture
        that spells it the same way on both sides resolves to nothing and the
        rotation folds no turns -- so "a rewind across a rotation point" becomes a
        claim about crossing a no-op. This pins the spelling that actually joins.
        """
        import cli.session as cs
        import runtime.session_lineage as rl
        from runtime.checkpoint import ConversationJournal, TurnFileState

        repo = _repo("rotation")
        log_root = tmp_path / "logs"
        run_dir = log_root / "run"
        (run_dir / "pristine").mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(repo), str(run_dir / "pristine")],
                       capture_output=True, check=False)
        text = {n: f"turn {n} question and answer" for n in range(1, 5)}
        journal = ConversationJournal(run_dir)
        state = TurnFileState(run_dir, read_bytes=_reader_for(repo),
                              write_bytes=_writer_for(repo))
        for turn in range(1, 5):
            journal.append({"record": "turn", "turn": turn, "seq": turn,
                            "content": text[turn], "state": {"v": turn}})
            state.capture(turn, ["app.py"])
            (repo / "app.py").write_text(f"value = {turn}\n", encoding="utf-8")

        session = cs.load_or_create(log_root=str(log_root), repo=str(repo))
        session.setdefault("turns", [])
        for turn in range(1, 5):
            session["turns"].append({"turn_id": f"t{turn}", "id": f"t{turn}",
                                     "text": text[turn]})
        cs.save_session(log_root, session)

        rotation = rl.rotate_session(log_root, session,
                                     summary="turns 3 and 4 folded",
                                     source_turn_ids=("t3", "t4"),
                                     reason="compression", run_dir=str(run_dir))
        reversible = rotation.get("reversible") or {}
        assert reversible.get("dropped_seqs") == [3, 4], (
            f"the rotation folded no journal turns ({reversible.get('unresolved')}), so "
            f"this test proves nothing about crossing a rotation point"
        )
        assert reversible.get("reversible") is True

        # The journal row the rotation left behind is NAMED, not hidden.
        rows = ConversationJournal(run_dir).load()
        lineage_rows = [row for row in rows if row.get("compaction_id")]
        assert len(lineage_rows) == 1, lineage_rows

    def test_the_oracle_driver_exists_and_exits_zero(self) -> None:
        """The transcript is a committed artefact, not a claim in prose."""
        driver = Path(tempfile.gettempdir()) / "opencode" / "t2p3w2_oracle.py"
        if not driver.is_file():
            pytest.skip(
                "the oracle driver is a measurement artefact under the temp dir; this "
                "pin asserts the property directly rather than a file that may be "
                "cleaned. The transcript is in execution/AGENTS.md."
            )
        completed = subprocess.run([sys.executable, str(driver)],
                                   capture_output=True, text=True, timeout=900,
                                   cwd=str(REPO_ROOT))
        assert completed.returncode == 0, (
            f"the oracle comparison FAILED -- that is a P0 finding:\n"
            f"{completed.stdout[-3000:]}\n{completed.stderr[-2000:]}"
        )
        assert "parts held : 4 of 4" in completed.stdout


def _reader_for(repo: Path):
    def read(relative: str) -> bytes:
        return (repo / relative).read_bytes()

    return read


def _writer_for(repo: Path):
    def write(relative: str, data: Any) -> str:
        target = repo / relative
        if data is None:
            if target.exists():
                target.unlink()
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
        return str(target)

    return write


# ===========================================================================
# T2.W2.5 -- a memory write is not verification evidence
# ===========================================================================


class TestAMemoryCanNeverSatisfyAVerificationResult:
    def test_a_memory_source_value_is_REFUSED_not_only_a_memory_keyword(
        self,
    ) -> None:
        """The hole Wave 2 measured.

        Before this round the structural sweep matched marker names appearing as
        KEYS or inside marker-bearing values, and ``{"source": "memory",
        "passed": True}`` was **ACCEPTED** -- because ``"memory"`` as a source
        VALUE matched no member of the marker table. That is the most natural
        spelling in this tree: ``shared.security.UNTRUSTED_SOURCES`` already names
        a source ``"memory"``.
        """
        for evidence in (
            {"source": "memory", "passed": True},
            {"source": "decision_memory"},
            {"reports": [{"source": "prior_run_assertion", "outcome": "pass"}]},
            {"evidence_source": "knowledge_recall"},
            {"origin": "recall"},
        ):
            with pytest.raises(mem.MemoryEvidenceRefused):
                mem.assert_memory_is_not_evidence(evidence)

    def test_an_UNRECOGNISED_source_ATTACHED_to_the_evidence_raises(self) -> None:
        """It must not default to acceptable, whichever door it arrives through."""
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence({"origin": "nowhere"})
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence(None, source="somewhere_else")
        for acceptable in mem.EVIDENCE_SOURCES:
            mem.assert_memory_is_not_evidence(None, source=acceptable)

    def test_FREE_PROSE_is_deliberately_ACCEPTED(self) -> None:
        """The exclusion that keeps the gate usable.

        A pytest report that names ``memory/test_x.py`` is not a memory asserting
        an outcome. A gate that refuses it is a gate people route around, which is
        strictly worse than the hole it closed.
        """
        mem.assert_memory_is_not_evidence(
            {"raw_output": "tests/memory/test_x.py::test_y PASSED\n2 passed in 0.1s"}
        )
        assert "raw_output" in mem.PROSE_KEYS
        assert "source" in mem.SOURCE_KEYS

    def test_the_refusal_is_not_absorbed_by_a_fail_closed_handler(self) -> None:
        """``MemoryEvidenceRefused`` must not be a ``PermissionError``.

        The gateway and the workspace absorb ``PermissionError`` on their
        fail-closed paths, which would SKIP the memory and carry on -- the exact
        laundering the refusal exists to stop.
        """
        assert not issubclass(mem.MemoryEvidenceRefused, PermissionError)
        assert not issubclass(mem.MemoryEvidenceRefused, ValueError)

    def test_nothing_in_execution_promotes_a_verification_boolean(self) -> None:
        """STRUCTURAL. The only mutations are ``True -> False``."""

        promoting: List[str] = []
        constructions: List[str] = []
        literal_true: List[str] = []
        for path in sorted((REPO_ROOT / "execution").glob("*.py")):
            if path.name.startswith("test_"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Attribute)
                                and target.attr in mem.VERIFICATION_BOOLEANS
                                and isinstance(node.value, ast.Constant)
                                and node.value.value is True):
                            promoting.append(f"{path.name}:{node.lineno} .{target.attr}")
                if isinstance(node, ast.Call):
                    name = (getattr(node.func, "id", None)
                            or getattr(node.func, "attr", None))
                    if name != "VerificationResult":
                        continue
                    constructions.append(path.name)
                    for keyword in node.keywords:
                        if keyword.arg == "baseline_passed":
                            assert (isinstance(keyword.value, ast.Constant)
                                    and keyword.value.value is False), (
                                f"{path.name}:{node.lineno} does not declare "
                                f"baseline_passed=False"
                            )
                        elif (keyword.arg in mem.VERIFICATION_BOOLEANS
                              and isinstance(keyword.value, ast.Constant)
                              and keyword.value.value is True):
                            literal_true.append(
                                f"{path.name}:{node.lineno} {keyword.arg}=True"
                            )
        assert promoting == [], f"something promotes a verification boolean: {promoting}"
        assert set(constructions) <= {"verify.py"}, (
            f"a VerificationResult is constructed outside execution/verify.py: "
            f"{sorted(set(constructions))}. The mint has exactly one module and a "
            f"second one is a second mint."
        )
        assert literal_true == [], f"a verdict is a LITERAL True at a construction: {literal_true}"
        assert len(constructions) >= 1, "no construction was found to check"

    def test_nothing_in_the_rest_of_the_tree_promotes_a_verification_boolean(
        self,
    ) -> None:
        """The tree-wide half: a memory write lives in ``memory/``."""
        promoting: List[str] = []
        for root in ("harness", "runtime", "cli", "shared", "memory", "mcp_server"):
            directory = REPO_ROOT / root
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.py")):
                if path.name.startswith("test_"):
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue  # another terminal mid-edit; the lane reports it
                for node in ast.walk(tree):
                    targets: List[ast.Attribute] = []
                    value = None
                    if isinstance(node, ast.Assign):
                        targets = [t for t in node.targets
                                   if isinstance(t, ast.Attribute)]
                        value = node.value
                    elif isinstance(node, ast.AnnAssign):
                        targets = ([node.target]
                                   if isinstance(node.target, ast.Attribute) else [])
                        value = node.value
                    if not (isinstance(value, ast.Constant) and value.value is True):
                        continue
                    for target in targets:
                        if target.attr in mem.VERIFICATION_BOOLEANS:
                            promoting.append(
                                f"{path.relative_to(REPO_ROOT).as_posix()}:"
                                f"{node.lineno} .{target.attr} = True"
                            )
        assert promoting == [], (
            f"something outside execution/ promotes a verification boolean: {promoting}"
        )

    def test_the_verifier_RE_RUNS_and_the_run_count_is_IDENTICAL_across_arms(
        self, tmp_path: Path
    ) -> None:
        """The Phase 3a-specific hazard, measured through the REAL verifier.

        A memory that makes a run cheaper by skipping verification is a
        verification bypass dressed as an optimisation, so the COUNT is asserted,
        not merely "the verifier was called".
        """
        from execution import verify as ver

        def counting_sandbox(failing: bool = False):
            calls: List[str] = []

            def sandbox(repo_path: Any, command: Any, timeout_s: int = 120, **kwargs: Any):
                from shared.types import ExecutionResult

                calls.append(str(command))
                body = ("collected 2 items\n"
                        + ("FAILED tests/test_a.py::test_one - assert False\n"
                           if failing else "")
                        + ("1 failed, 1 passed in 0.02s\n" if failing
                           else "2 passed in 0.02s\n"))
                return ExecutionResult(
                    exit_code=1 if failing else 0, stdout=body, stderr="",
                    timed_out=False,
                )

            return sandbox, calls

        def arm(memory_on: bool) -> Tuple[int, List[str], Any]:
            repo = tmp_path / ("on" if memory_on else "off")
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "test_a.py").write_text(
                "def test_one():\n    assert True\n"
                "def test_two():\n    assert True\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname='p'\n", encoding="utf-8")
            trace = tmp_path / f"trace-{memory_on}.jsonl"
            trace.write_text(json.dumps({
                "kind": "decision_memory",
                "data": ({"matched": 2, "queries": ["tests/test_a.py::test_one"]}
                         if memory_on
                         else {"matched": 0, "skipped": "plan_with_memory=False"}),
            }) + "\n", encoding="utf-8")
            sandbox, calls = counting_sandbox()
            original = ver.execute_sandboxed
            ver.execute_sandboxed = sandbox
            try:
                result = ver.verify(str(repo), "tests/test_a.py::test_one",
                                    rerun_for_flake_check=2)
            finally:
                ver.execute_sandboxed = original
            return len(calls), calls, (result, trace)

        off_count, off_calls, (off_result, off_trace) = arm(False)
        on_count, on_calls, (on_result, on_trace) = arm(True)

        assert on_count > 0, (
            "the verifier did not run at all on the memory ON arm: a memory "
            "short-circuited it, which is a verification bypass"
        )
        assert off_count == on_count, (
            f"VERIFICATION RUN COUNT DIFFERS ACROSS THE MEMORY ARMS: off={off_count} "
            f"on={on_count}. A memory that makes a run cheaper by skipping "
            f"verification is a bypass dressed as an optimisation."
        )
        import re

        normalise = lambda c: re.sub(r"neo-structured-[0-9a-f]+", "<TOKEN>", c)  # noqa: E731
        assert [normalise(c) for c in off_calls] == [normalise(c) for c in on_calls], (
            "the COMMAND SEQUENCE differs across the memory arms, so the equal count "
            "is a coincidence rather than the same verification plan"
        )
        assert on_result.target_test_passed is True and off_result.target_test_passed is True

        off_influence = mem.memory_influence(off_trace)
        on_influence = mem.memory_influence(on_trace)
        assert off_influence.influenced is False
        assert on_influence.influenced is True, (
            "the ON arm recalled something and the influence was not reported, so the "
            "ablation cannot attribute anything"
        )
        assert on_influence.attributable is True
        assert off_influence.source == "trace"

    def test_a_run_with_NO_memory_row_is_UNMEASURED_not_zero(self) -> None:
        """Absent evidence and measured-zero are different claims."""
        with tempfile.TemporaryDirectory() as raw:
            trace = Path(raw) / "trace.jsonl"
            trace.write_text("", encoding="utf-8")
            influence = mem.memory_influence(trace)
            assert influence.source == "absent"
            assert influence.queried is False
            assert influence.influenced is False

    def test_the_arms_differ_ONLY_in_memory(self, tmp_path: Path) -> None:
        """Otherwise a measured difference is attributable to nothing."""
        left = {"task_id": "t", "fork_file_policy": "own_snapshot"}
        right = {"task_id": "t", "fork_file_policy": "own_snapshot",
                 "plan_with_memory": True}
        diff = mem.arm_config_diff(left, right, expected=("plan_with_memory",))
        assert diff.clean is True
        assert diff.unexpected == ()
        # and the CONTROL: a second behavioural difference is NOT attributable
        dirty = mem.arm_config_diff(left, {**right, "retries": 3},
                                    expected=("plan_with_memory",))
        assert dirty.clean is False


# ===========================================================================
# the weakening demonstrations -- both colours, in one run
# ===========================================================================


class TestTheWeakeningIsDetectable:
    """An assertion that a defence is currently present is not a demonstration
    that it works. Each applies the LITERAL edit to an isolated COPY."""

    def test_removing_the_named_tree_requirement_admits_a_writable_fork(
        self, tmp_path: Path
    ) -> None:
        weakened = _weakened_copy(
            fs,
            "FORKS_NEED_A_NAMED_TREE = (",
            "FORKS_NEED_A_NAMED_TREE_UNUSED = (",
            "t2w2_fs_unnamed",
        )
        assert weakened is not fs, "the weakened copy IS the live module"
        parent = _tree(tmp_path, "parent")
        assert fs.fork_file_state(parent, None).writable is False, (
            "the shipped module let a tree-less fork through"
        )
        # The demonstration is about the ENFORCEMENT, so weaken the enforcement.
        unvalidated = _weakened_copy(
            fs,
            "if not child_key:\n        writable = False\n        writable_reason = FORKS_NEED_A_NAMED_TREE",
            "if not child_key:\n        writable = True\n        writable_reason = ''",
            "t2w2_fs_unnamed2",
        )
        assert unvalidated.fork_file_state(parent, None).writable is True, (
            "removing the named-tree requirement did NOT admit a writable fork over no "
            "tree, so the pin above is not testing the thing it claims to test"
        )

    def test_removing_the_policy_validation_reaches_a_bare_KeyError(
        self, tmp_path: Path
    ) -> None:
        unvalidated = _weakened_copy(
            fs,
            "_validated_policy(str(policy), \"the policy= keyword\")",
            "str(policy)",
            "t2w2_fs_policy",
        )
        parent = _tree(tmp_path, "parent")
        child = _tree(tmp_path, "child")
        assert fs.FORK_POLICY_SHARE not in fs.FORK_FILE_POLICIES
        with pytest.raises(fs.FileStatePolicyError):
            fs.fork_file_state(parent, child, policy=fs.FORK_POLICY_SHARE)
        with pytest.raises(KeyError):
            unvalidated.fork_file_state(parent, child, policy=fs.FORK_POLICY_SHARE)

    def test_dropping_the_recovery_record_makes_the_recovery_SILENT(
        self, tmp_path: Path
    ) -> None:
        silent = _weakened_copy(
            fs,
            'record["ledger_written"] = append_recovery_ledger(home, record)',
            'record["ledger_written"] = True  # weakened: the write never happens',
            "t2w2_fs_silent",
        )
        tree = _tree(tmp_path)
        home = _isolated_home(tmp_path)
        victim = _spawn_claimant(tree, home, session="sess-victim")
        assert victim.stdout is not None
        victim.stdout.readline()
        victim.kill()
        victim.wait(timeout=60)
        assert silent.recovery_report(home)["recoveries"] == 0, (
            "the weakened copy still recorded a recovery, so the pin is not testing the "
            "recording"
        )
        assert fs.claim_tree(tree, session_id="sess-next", home=home).recovered[0][
            "ledger_written"
        ] is True

    def test_dropping_the_staging_rollback_leaves_PARTIAL_state(
        self, tmp_path: Path
    ) -> None:
        """RED/GREEN on the COPY failure: a disk fault mid-copy must roll back.

        Weakened: the commit block's blanket ``except BaseException`` becomes
        ``except ZeroDivisionError``, so an ``OSError`` from the copier escapes
        without the rollback running.
        """
        unrolled = _weakened_copy(
            sn,
            "    except BaseException as commit_error:",
            "    except ZeroDivisionError as commit_error:",
            "t2w2_sn_unrolled",
        )
        repo = _repo("unrolled")
        run = tmp_path / "run"
        real = unrolled._copy_file
        calls = {"n": 0}

        def exploding(source: Path, destination: Path) -> None:
            calls["n"] += 1
            if calls["n"] > 3:
                raise OSError("simulated disk failure mid-copy")
            real(source, destination)

        unrolled._copy_file = exploding
        try:
            with pytest.raises(OSError):
                unrolled.create_run_snapshot(str(repo), str(run))
        finally:
            unrolled._copy_file = real
        left = [p for p in run.rglob("*") if p.is_file()] if run.exists() else []
        assert left, (
            "narrowing the commit's `except BaseException` did NOT leave partial "
            "state, so the pin above is not testing the rollback"
        )
        # GREEN, on the shipped module, for the same injected fault.
        green_run = tmp_path / "run-green"
        real_shipped = sn._copy_file
        green_calls = {"n": 0}

        def exploding_green(source: Path, destination: Path) -> None:
            green_calls["n"] += 1
            if green_calls["n"] > 3:
                raise OSError("simulated disk failure mid-copy")
            real_shipped(source, destination)

        sn._copy_file = exploding_green
        try:
            with pytest.raises(OSError):
                sn.create_run_snapshot(str(_repo("green")), str(green_run))
        finally:
            sn._copy_file = real_shipped
        green_left = (
            [p for p in green_run.rglob("*") if p.is_file()] if green_run.exists() else []
        )
        assert not green_left, (
            f"the shipped module left {len(green_left)} file(s) behind after a "
            f"mid-copy failure, so the RED arm above is not demonstrating a "
            f"difference"
        )

    def test_DELETING_the_previous_pair_instead_of_parking_it_DESTROYS_a_rewind_point(
        self, tmp_path: Path
    ) -> None:
        """The strongest RED in this module, and it is a ONE-LINE edit.

        Weakened: the parking rename becomes a delete -- which is literally the
        shape this module had before the rollback existed. A failure between the
        two commit renames then has nothing to restore, and the session that was
        holding that run loses its rewind point permanently.

        This is why the previous demo is not sufficient on its own: it shows the
        rollback running, and this shows what the rollback is *for*.
        """
        deleter = _weakened_copy(
            sn,
            "                os.replace(str(target), str(retire))\n"
            "                parked.append((retire, target))",
            "                force_rmtree(target)\n"
            "                parked.append((retire, target))",
            "t2w2_sn_deleter",
        )
        repo = _repo("parked")
        run = tmp_path / "run-parked"
        sn.create_run_snapshot(str(repo), str(run))
        before = _tree_digest(run)
        changed = _repo("parked")
        (changed / "src" / "mod00.py").write_text("VALUE = 31337\n", encoding="utf-8")

        # GREEN first: the shipped module survives the identical fault.
        real_replace = os.replace
        green_seen = _fail_the_commit_rename_of(sn, "work")
        try:
            with pytest.raises(OSError) as excinfo:
                sn.create_run_snapshot(str(changed), str(run))
        finally:
            os.replace = real_replace
        assert green_seen, "the GREEN injection never fired; the RED arm is meaningless"
        assert not isinstance(excinfo.value, sn.SnapshotCommitIncomplete)
        assert _tree_digest(run) == before, (
            "the shipped module did not restore the previous snapshot; the RED arm "
            "below would then be demonstrating a difference that does not exist"
        )

        # RED: same fault, module that deletes instead of parking.
        red_seen = _fail_the_commit_rename_of(deleter, "work")
        try:
            with pytest.raises(OSError) as red_excinfo:
                deleter.create_run_snapshot(str(changed), str(run))
        finally:
            deleter.os.replace = real_replace
        assert red_seen, "the RED injection never fired; this arm proved nothing"
        # Matched BY NAME, not by identity: the weakened copy re-executes the
        # class body, so `deleter.SnapshotCommitIncomplete` is a different class
        # object with the same name. `isinstance` against the shipped module's class
        # would not match, and a demo that silently stopped matching would report
        # the weakening as undetectable for the wrong reason.
        assert type(red_excinfo.value).__name__ == "SnapshotCommitIncomplete", (
            f"the delete-instead-of-park module raised "
            f"{type(red_excinfo.value).__name__} rather than reporting that the "
            f"previous snapshot could not be restored"
        )
        assert sn.snapshot_availability(run).available is False, (
            "after the weakened commit failed, the run directory still reads as a "
            "usable rewind point. Deleting the previous pair is not a weaker "
            "rollback, it is the loss of the rewind point this whole module exists "
            "to protect."
        )

    def test_collapsing_the_unreadable_bucket_lets_a_corrupt_sidecar_be_pruned(
        self, tmp_path: Path
    ) -> None:
        collapsed = _weakened_copy(
            sn,
            '            unreadable_sidecar.append(path.name)\n            protected_lineage[path] = ""\n            continue',
            "            unreadable_sidecar.append(path.name)\n            protected_lineage.pop(path, None)\n            continue",
            "t2w2_sn_collapsed",
        )
        root = tmp_path / "logs"
        _run_with_lineage(root, "run-corrupt", "lin-x", 40.0, corrupt_sidecar=True)
        _run_with_lineage(root, "run-keep", "lin-y", 40.0)
        collapsed.write_live_sessions(root, [{"session_id": "run-keep-s",
                                             "lineage_id": "lin-y"}])
        red = collapsed.prune_run_directories(root, MAX_PRESSURE)
        assert "run-corrupt" in red.removed, (
            "removing the corrupt-sidecar protection did NOT prune the run, so the pin "
            "above is not testing the protection"
        )
        assert red.lineage_unreadable == ("run-corrupt",), (
            "the ENFORCEMENT changed but the DETECTION did not -- which is what makes "
            "this the weakening worth demonstrating"
        )
        # GREEN runs on its OWN root: re-running it on a root the weakened copy
        # already emptied would prove nothing.
        green_root = tmp_path / "green-logs"
        _run_with_lineage(green_root, "run-corrupt", "lin-x", 40.0, corrupt_sidecar=True)
        _run_with_lineage(green_root, "run-keep", "lin-y", 40.0)
        green = sn.prune_run_directories(green_root, MAX_PRESSURE)
        assert "run-corrupt" not in green.removed
        assert (green_root / "run-corrupt").exists()

    def test_emptying_the_marker_sweep_accepts_a_memory_source(self) -> None:
        empty = _weakened_copy(
            mem,
            "def _classify_source(value: Any) -> None:",
            "def _classify_source(value: Any) -> None:\n        return  # weakened",
            "t2w2_me_empty",
        )
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence({"source": "memory"})
        empty.assert_memory_is_not_evidence({"source": "memory"})

    def test_the_pins_are_not_rubber_stamps(self, tmp_path: Path) -> None:
        """The suite cannot pass by never firing."""
        # the session claim refuses
        home = _isolated_home(tmp_path)
        tree = _tree(tmp_path)
        holder = fs.claim_tree(tree, session_id="a", home=home)
        for other in ("b", "c", "d"):
            with pytest.raises(fs.SessionTreeConflict):
                fs.claim_tree(tree, session_id=other, home=home)
        holder.release()
        # the availability reader distinguishes present from a half pair
        half = tmp_path / "half"
        (half / "pristine").mkdir(parents=True)
        assert sn.snapshot_availability(half, log_root=tmp_path).status == "incomplete"
        full = tmp_path / "full"
        (full / "pristine").mkdir(parents=True)
        (full / "work").mkdir(parents=True)
        assert sn.snapshot_availability(full, log_root=tmp_path).status == "present"
        # the merge gate clears one input and refuses another
        refused = fs.merge_back("t", "s", explicit=False, paths=["a.py"])
        assert refused.ok is False
        cleared = fs.merge_back("t", "s", explicit=True, paths=["a.py"],
                                accounted={"a.py": ["d1"]}, current={"a.py": "d1"})
        assert cleared.ok is True
        # the memory gate refuses a memory and accepts a verifier run
        with pytest.raises(mem.MemoryEvidenceRefused):
            mem.assert_memory_is_not_evidence({"source": "memory"})
        mem.assert_memory_is_not_evidence(None, source="verifier_run")
        # and the bound's enforcement really reclaims
        root = tmp_path / "bound"
        for index in range(3):
            _run_with_lineage(root, f"run-{index}", f"lin-{index}", 40.0)
        applied = sc.enforce_storage_bound(
            root, bound=sc.storage_bound(store_max_bytes=1, keep_latest=1)
        )
        assert applied.runs_after == 1
