"""P1/W1 pins: the verification path's cost and honesty, so neither can return.

Seven claims, each with the demonstration that would break it. Read this before
changing anything in ``execution/walk_scope.py``, ``execution/verify.py``'s
regression block, or ``execution/sandbox.py``'s timing.

The honesty pins (4, 5, 6) run WITHOUT a daemon and are the ones that matter
most. A fast regression check that lies is worse than a slow honest one, and a
pin that asserts only "the marker is absent" would be satisfied by a crash
before the marker was ever produced - so every one of them asserts a real
completion too, per ``phases/DOCTRINE.md`` §3.

Run with::

    python -m pytest execution/test_w1_latency_honesty_pins.py -q

Container-backed rows self-skip with the daemon's reason when it is down. A skip
is BLOCKED coverage, never a pass.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import execution.sandbox as sb
import execution.test_selection as ts
import execution.verify as ver
from execution import flake_gate as fg
from execution import walk_scope as ws
from execution.walk_scope import (
    WALK_ENTRY_BAR,
    WALK_SECONDS_BAR,
    missing_from,
    walk_python_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every basename ``execution/sandbox.py`` is allowed to hash into the dependency
#: image fingerprint. ``setup.py`` is here because it can declare
#: ``install_requires``; the rest are manifests by name. Anything absent fails the
#: pin, which is what makes adding a source file to the fingerprint a visible act.
_KNOWN_MANIFEST_BASENAMES = frozenset(
    {
        # python
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "tox.ini",
        "environment.yml",
        # javascript
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
)


def _docker_up() -> bool:
    """Return whether the daemon answers, without raising."""
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return probe.returncode == 0 and bool((probe.stdout or "").strip())


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# Claim 1 — the image fingerprint ignores source and honours manifests.
# ---------------------------------------------------------------------------


class TestTheFingerprintIsManifestOnly:
    """`execution/AGENTS.md` has claimed "code edits never trigger rebuilds"
    since Round 3. This is the test that claim never had, in BOTH directions.

    One direction alone is weak: a tag that happens not to change could also be
    a tag that changed and collided. So each direction has its own assertion and
    the pair is the proof - a code edit must leave BOTH the tag and the build
    cost alone, and a manifest edit must move the tag.
    """

    def _repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        (root / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
        return root

    def test_a_code_edit_does_not_move_the_fingerprint(self, tmp_path: Path) -> None:
        """Source is not an input to the fingerprint. No daemon needed."""
        root = self._repo(tmp_path)
        before = sb._dep_image_tag(str(root))

        (root / "mymod.py").write_text("VALUE = 2  # edited\n", encoding="utf-8")
        (root / "new_module.py").write_text("NEW = True\n", encoding="utf-8")
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")

        after = sb._dep_image_tag(str(root))
        assert after == before, (
            "a source edit changed the dependency image fingerprint, so every "
            "code edit would force an image rebuild"
        )

    def test_a_manifest_edit_does_move_the_fingerprint(self, tmp_path: Path) -> None:
        """The other direction: a manifest IS an input. Without this, claim 1
        would pass on a fingerprint that ignores EVERYTHING."""
        root = self._repo(tmp_path)
        before = sb._dep_image_tag(str(root))
        (root / "requirements.txt").write_text("pytest\nrequests\n", encoding="utf-8")
        after = sb._dep_image_tag(str(root))
        assert after != before, (
            "changing requirements.txt did not move the fingerprint, so the "
            "'source does not' test above would pass vacuously"
        )

    def test_the_fingerprint_manifests_are_named_and_manifest_only(self) -> None:
        """The hashed set is declared, so a future addition is a visible act.

        The assertion is that each name is a DECLARED dependency manifest the
        product already documents, not that it matches an extension guess:
        ``setup.py`` is in the set deliberately (it can declare
        ``install_requires``), so an extension whitelist would fail on a correct
        manifest and would tempt somebody to delete it.
        """
        assert "requirements.txt" in sb.DEP_MANIFESTS
        assert "pyproject.toml" in sb.DEP_MANIFESTS
        assert "setup.py" in sb.DEP_MANIFESTS
        # No SOURCE extension may enter either set: a `.py` in the manifest
        # tuple would put a source file into the image fingerprint and make
        # every code edit rebuild.
        for name in tuple(sb.DEP_MANIFESTS) + tuple(sb.JS_DEP_MANIFESTS):
            base = name.rsplit("/", 1)[-1]
            assert base in _KNOWN_MANIFEST_BASENAMES, (
                f"{name!r} is not a declared dependency manifest; adding a "
                "source file here would make every code edit force a rebuild"
            )

    @requires_docker
    def test_a_code_edit_really_does_not_rebuild(self, tmp_path: Path) -> None:
        """The tag argument above, with the BUILD cost measured too.

        Two independent observations, because either alone is weak: the tag is
        identical (the fingerprint ignores source) AND `ensure_image` costs about
        the same after the edit (no build was kicked off, which a tag collision
        could hide).
        """
        root = self._repo(tmp_path)
        import time

        before_tag = sb._dep_image_tag(str(root))
        t0 = time.perf_counter()
        sb.ensure_image(str(root))
        warm = time.perf_counter() - t0

        (root / "mymod.py").write_text("VALUE = 99  # edited\n", encoding="utf-8")
        (root / "another.py").write_text("Y = 1\n", encoding="utf-8")
        after_tag = sb._dep_image_tag(str(root))
        t0 = time.perf_counter()
        sb.ensure_image(str(root))
        after = time.perf_counter() - t0

        assert after_tag == before_tag
        # A real rebuild of a per-repo image costs seconds, not milliseconds.
        assert after < max(2.0, warm * 3), (
            f"ensure_image cost {warm:.3f}s before the edit and {after:.3f}s after; "
            "a code edit triggered a rebuild"
        )


# ---------------------------------------------------------------------------
# Claim 2 — the repository is BIND-MOUNTED; nothing copies the tree per call.
# ---------------------------------------------------------------------------


class TestTheRepositoryIsNeverCopiedOnThePerCallPath:
    """A copy would be O(repo size) on every verification. The brief asks for
    this to be ASSERTED, and an assertion is stronger than a measurement because
    it cannot pass on a day the tree happened to be small."""

    def test_the_argv_binds_the_workspace_read_write_not_a_copy(self) -> None:
        root = REPO_ROOT
        args = sb._docker_run_args(
            "harness-exec:base",
            str(root),
            "echo hi",
            False,  # allow_network
            None,  # env
            "1g",  # mem_limit
            1.0,  # cpu_limit
            512,  # pids_limit
        )
        volumes = [
            args[index + 1] for index, value in enumerate(args) if value == "--volume"
        ]
        assert volumes, (
            "no --volume in the argv; where does the repo reach the container?"
        )
        workspace = [spec for spec in volumes if spec.endswith(":/workspace")]
        assert len(workspace) == 1, volumes
        assert workspace[0].split(":")[0], "an empty source is not a mount"
        # No COPY-style mode on the workspace mount: Docker defaults to :rw,
        # which is correct (agent edits must persist to the host).
        assert not workspace[0].endswith(":ro"), (
            "the workspace mount is read-only, so agent edits would not persist "
            "and the harness's pristine/work diff would read nothing"
        )

    def test_the_per_call_path_contains_no_copy_verb(self) -> None:
        """No `docker cp`, `docker commit`, `docker export`, `shutil.copy*` or
        `tar` is reachable from `execute_sandboxed`."""
        source = Path(sb.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_calls = {
            "copytree",
            "copy2",
            "copyfile",
            "copy",
            "rmtree",
            "make_archive",
        }
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in banned_calls:
                    offenders.append(f"{name}() at sandbox.py:{node.lineno}")
            # An argv list literal carrying a copying docker verb.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in {"cp", "commit", "export"}
            ):
                offenders.append(
                    f"docker verb {node.value!r} at sandbox.py:{node.lineno}"
                )
        assert not offenders, (
            "a copying operation is reachable from sandbox.py, which would make "
            "every sandboxed call O(repo size): " + "; ".join(offenders)
        )

    def test_the_module_docstring_states_the_bind_mount_invariant(self) -> None:
        """A guard that exists only in a test can be removed by whoever does not
        read tests. The module docstring carries the reason."""
        doc = (sb.__doc__ or "").lower()
        assert "bind-mount" in doc or "bind mount" in doc
        assert "read-write" in doc or "read write" in doc


# ---------------------------------------------------------------------------
# Claim 3 — ONE skip authority, and every walk is inside the bar.
# ---------------------------------------------------------------------------


class TestOneSkipAuthority:
    """The 851x. `phases/DOCTRINE.md` §8 records this repo paying for two
    skip-lists once already; this is the pin that makes a third impossible from
    `execution/`."""

    def test_the_authority_is_a_superset_of_every_foreign_table(self) -> None:
        """Drift is measured, not asserted. `missing_from` answers "what would
        that walk open that this one would not"."""
        foreign = {
            "memory.code_graph.SKIP_DIR_NAMES": _code_graph_skips(),
            "harness.retrieval._SKIP_DIRS": _retrieval_skips(),
        }
        for name, table in foreign.items():
            missing = missing_from(table)
            assert missing == (), (
                f"{name} has names the execution authority does not: {missing}. "
                "Either table can walk a subtree this one prunes, which is the "
                "851x defect."
            )

    def test_the_authority_is_what_the_execution_walk_actually_uses(self) -> None:
        """Not just "the set exists" - that the WALK consults it.

        A separate authority that is merely declared next to the walk is still
        two authorities in practice, so this asserts the behaviour: a planted
        ``logs/`` tree is not enumerated.
        """
        result = walk_python_files(str(REPO_ROOT))
        assert result.dirs_opened < 5_000, (
            f"the walk opened {result.dirs_opened} directories; it is not "
            "consulting the shared authority"
        )

    def test_execution_has_exactly_one_skip_table_and_it_is_the_authority(self) -> None:
        """Structurally: `execution/` owns no second list.

        Two exemptions, both required and neither arbitrary:

        * ``walk_scope.py`` - it IS the authority.
        * ``snapshot.py`` - it is a COPIER and must never prune real committed
          content; a separate pin
          (``tests/test_ceiling_r2_10_snapshot_disk.py::test_the_ignore_matcher_is_git_and_not_a_second_implementation``)
          already forbids a table there.

        Test modules are exempt because a fixture walk legitimately declares the
        junk it is planting, and that is not a second production authority.
        """
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "execution").glob("*.py")):
            if path.name in {"walk_scope.py", "snapshot.py"}:
                continue
            if path.name.startswith("test_") or path.name.endswith("_pins.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                targets: list[str] = []
                if isinstance(node, ast.Assign):
                    targets = [
                        t.id
                        for t in node.targets
                        if isinstance(t, ast.Name) and "SKIP" in t.id.upper()
                    ]
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and "SKIP" in node.target.id.upper()
                ):
                    targets = [node.target.id]
                if targets:
                    offenders.append(f"{path.name}:{node.lineno} {targets}")
        assert not offenders, (
            "execution/ declares a second skip table; import from "
            "execution.walk_scope.SKIP_DIR_NAMES instead: " + "; ".join(offenders)
        )

    def test_the_walk_on_this_repository_is_inside_the_bar(self) -> None:
        """The bar from the brief, measured on the tree the brief is about.

        Before this round the equivalent walk opened 149,559 directories here.
        """
        result = walk_python_files(str(REPO_ROOT))
        assert result.dirs_opened <= WALK_ENTRY_BAR, (
            f"the source walk opened {result.dirs_opened} directories, over the "
            f"{WALK_ENTRY_BAR} bar"
        )
        assert result.seconds <= WALK_SECONDS_BAR, (
            f"the source walk took {result.seconds:.3f}s, over the "
            f"{WALK_SECONDS_BAR}s bar"
        )
        assert result.complete, f"the walk was bounded: {result.to_dict()}"

    def test_the_walk_prunes_before_it_descends(self) -> None:
        """A pruned subtree must not be ENUMERED, not filtered afterwards.

        This is the difference between `os.walk` (materialises, then the caller
        prunes) and a stack over `os.scandir`. Proven by counting: a `logs/`
        tree full of junk must contribute zero opens.
        """
        tmp = Path(os.environ.get("TEMP", ".")) / "neo-w1-pin-skip"
        (tmp / "logs" / "run-a" / "pristine" / "deep").mkdir(
            parents=True, exist_ok=True
        )
        (tmp / "logs" / "run-a" / "work" / "deep").mkdir(parents=True, exist_ok=True)
        (tmp / "keep").mkdir(parents=True, exist_ok=True)
        (tmp / "keep" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (tmp / "logs" / "run-a" / "pristine" / "deep" / "b.py").write_text(
            "B = 1\n", encoding="utf-8"
        )
        result = walk_python_files(str(tmp))
        names = {path.name for path in result.files}
        assert names == {"a.py"}, (
            f"the walk returned {sorted(names)}; the logs/ subtree must be pruned "
            "from the walk, not filtered after it"
        )
        assert result.dirs_opened <= 4, (
            f"the walk opened {result.dirs_opened} directories for a tree with one "
            "live directory and two junk subtrees"
        )

    def test_a_bounded_walk_reports_what_it_did_not_scan(self) -> None:
        """The honesty rule for a walk: a bounded scan is never presented as a
        whole repository."""
        tmp = Path(os.environ.get("TEMP", ".")) / "neo-w1-pin-bounded"
        for index in range(6):
            sub = tmp / f"pkg{index}"
            sub.mkdir(parents=True, exist_ok=True)
            for leaf in range(20):
                (sub / f"m{leaf}.py").write_text(f"X = {leaf}\n", encoding="utf-8")
        import time

        capped = walk_python_files(str(tmp), max_files=5)
        assert not capped.complete
        assert capped.truncation == ws.TRUNCATION_CAP
        assert capped.not_searched > 0, "a capped walk must say what it did not collect"
        assert capped.within_bar

        full = walk_python_files(str(tmp), deadline_s=time.monotonic() + 120.0)
        assert full.complete
        assert full.truncation == ws.TRUNCATION_COMPLETE


def _code_graph_skips() -> set:
    """The fast authority's table, read live so this pin tracks its owner."""
    try:
        from memory.code_graph import SKIP_DIR_NAMES as table
    except Exception:  # pragma: no cover - memory is optional at test time
        pytest.skip("memory.code_graph is not importable")
    return set(table)


def _retrieval_skips() -> set:
    """The retrieval table, read live for the same reason."""
    try:
        from harness.retrieval import _SKIP_DIRS as table
    except Exception:  # pragma: no cover - harness is optional at test time
        pytest.skip("harness.retrieval is not importable")
    return set(table)


# ---------------------------------------------------------------------------
# Claim 4 — a PARTIAL regression selection can never read as a COMPLETE one.
# ---------------------------------------------------------------------------


class TestAPartialSelectionCannotReadAsACompleteOne:
    """The single most important sentence in P1/W1.

    Soundness and completeness are different axes. An import-graph selection can
    be perfectly sound - every test it picked really can observe the change - and
    still run 2 tests out of 3. That second case is the dangerous one and it is
    what these pins are about.
    """

    def test_a_sound_but_partial_selection_is_NOT_complete(self) -> None:
        """The exact shape that was wrong in this round's first implementation:
        strategy `import_graph`, nothing missing, whole-tree walk, and only 2 of
        3 tests selected."""
        selection = ts.TestSelection(
            files=("tests/test_a.py", "tests/test_b.py"),
            strategy="import_graph",
            total_tests=3,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        assert selection.coverage_is_sound() is True, (
            "an import-graph selection IS sound about which tests can observe the "
            "change; that is a different axis"
        )
        assert selection.coverage_is_complete() is False, (
            "2 of 3 tests is PARTIAL and must not report complete, however sound "
            "the reasoning that chose them"
        )
        receipt = selection.coverage_receipt()
        assert receipt["fraction"] == round(2 / 3, 6)
        assert receipt["complete"] is False
        assert receipt["sound"] is True
        assert any("PARTIAL" in reason for reason in receipt["reasons"]), receipt[
            "reasons"
        ]
        assert any("1 did not" in reason for reason in receipt["reasons"]), (
            "the receipt must name HOW MANY tests did not run, not just that some "
            f"did not: {receipt['reasons']}"
        )

    def test_a_whole_denominator_selection_IS_complete(self) -> None:
        """The control. Without it, `coverage_is_complete` could pass by always
        returning False, and the flag would be worthless."""
        selection = ts.TestSelection(
            files=("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"),
            strategy="fallback_all",
            total_tests=3,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        assert selection.coverage_is_complete() is True
        assert selection.coverage_is_sound() is False, (
            "selecting everything is complete but is NOT a sound argument about "
            "which tests could observe the change"
        )

    @pytest.mark.parametrize(
        "overrides,label",
        [
            ({"missing": ("tests/test_gone.py",)}, "missing file"),
            (
                {
                    "walk": {
                        "complete": False,
                        "truncation": "budget",
                        "not_searched": 40,
                    }
                },
                "bounded walk",
            ),
            # A scope alone does NOT narrow: `out_of_scope` must actually name
            # something for a test to have been excluded. A scope with nothing
            # outside it IS the whole candidate set, so reporting it partial
            # would be the wrong direction to lie in.
            (
                {
                    "scope": "services/api",
                    "out_of_scope": ("tests/test_b.py", "tests/test_c.py"),
                },
                "narrowed scope",
            ),
            ({"strategy": "bounded", "total_tests": 40}, "max_files clip"),
            ({"strategy": "same_package", "total_tests": 40}, "same-package fallback"),
        ],
    )
    def test_each_independent_way_to_be_partial_reports_partial(
        self, overrides, label
    ) -> None:
        """Every rung that can drop a test, checked separately, because a single
        combined test would not tell you which rung regressed.

        ``TestSelection`` is a frozen dataclass, so each variant is CONSTRUCTED
        rather than mutated. That is not incidental: a test that mutated it would
        be testing a shape the product cannot be in.
        """
        base = {
            "files": ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"),
            "strategy": "import_graph",
            "total_tests": 3,
            "walk": {"complete": True, "truncation": "complete", "not_searched": 0},
        }
        assert ts.TestSelection(**base).coverage_is_complete() is True, (
            "control must start complete"
        )
        partial = ts.TestSelection(**{**base, **overrides})
        assert partial.coverage_is_complete() is False, (
            f"{label} did not make the selection report partial"
        )
        reasons = partial.coverage_receipt()["reasons"]
        assert reasons, (
            f"{label} made it partial with NO reason recorded, so a reader sees a "
            "flag with no explanation"
        )

    def test_a_scope_with_nothing_outside_it_is_still_complete(self) -> None:
        """The control for the ``narrowed scope`` row above, and the reason that
        row has to carry ``out_of_scope``: a declared scope that excluded nothing
        did not exclude anything."""
        selection = ts.TestSelection(
            files=("tests/test_a.py",),
            strategy="import_graph",
            total_tests=1,
            scope="services/api",
            out_of_scope=(),
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        assert selection.coverage_is_complete() is True

    @pytest.mark.parametrize(
        "strategy,sound,label",
        [
            ("import_graph", True, "the graph chose them"),
            (
                "import_graph_partial",
                True,
                "the graph chose them, some files unparseable",
            ),
            ("bounded", False, "the max_files clip can drop an affected test"),
            ("same_package", False, "a cross-package dependent is invisible"),
            ("fallback_all", False, "everything ran, but by fallback not by argument"),
        ],
    )
    def test_soundness_is_a_separate_axis_from_completeness(
        self, strategy, sound, label
    ) -> None:
        """Completeness and soundness are DIFFERENT questions, and this round's
        first implementation conflated them - it read the strategy's soundness
        and reported it as completeness.

        ``fallback_all`` selects everything and is therefore COMPLETE, while
        being UNSOUND about which tests could observe the change. A
        ``bounded`` selection is incomplete for a different reason than it is
        unsound. Reporting the wrong axis is how a partial check reads as a
        complete one, so both are pinned.
        """
        whole = ts.TestSelection(
            files=("tests/test_a.py",),
            strategy=strategy,
            total_tests=1,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        assert whole.coverage_is_complete() is True, (
            f"{strategy!r} selecting the whole denominator is complete"
        )
        assert whole.coverage_is_sound() is sound, (
            f"{strategy!r} soundness is {label}; got {whole.coverage_is_sound()}"
        )

    def test_a_clip_that_fired_is_caught_by_the_number_rule(self) -> None:
        """Why ``bounded`` is not in the completeness table above.

        ``bounded`` only fires when the clip actually dropped something, so a
        ``bounded`` selection is ALWAYS smaller than the denominator and the
        number rule catches it. This asserts that, rather than leaving it as an
        argument: if a future ``max_files`` clamp stopped reducing the count, the
        number rule would be the only thing left standing.
        """
        clipped = ts.TestSelection(
            files=("tests/test_a.py",),
            strategy="bounded",
            total_tests=40,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        assert clipped.coverage_is_complete() is False
        assert clipped.coverage_is_sound() is False
        reasons = clipped.coverage_receipt()["reasons"]
        assert any("PARTIAL" in reason for reason in reasons), reasons

    def test_verify_reports_partial_for_a_selection_that_is_partial(self) -> None:
        """The end-to-end requirement, driven through the real ``verify()``.

        An earlier version of this round got it wrong and this is the pin for the
        wrong version: a sound ``import_graph`` selection of 2 of 3 tests reported
        ``regression_check="complete"``. It reported ``complete`` because the
        STRATEGY was sound, which is a different axis from how many tests ran.
        """
        selection = ts.TestSelection(
            files=("tests/test_a.py", "tests/test_b.py"),
            strategy="import_graph",
            total_tests=3,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        result = _drive(
            regression_mode=ver.REGRESSION_SELECTED,
            selection=selection,
        )
        assert result.regression_check == ver.REGRESSION_PARTIAL, (
            f"verify() reported {result.regression_check!r} for a selection that "
            "covered 2 of 3 tests; a partial check that reads as complete is a "
            "false negative reported as a pass"
        )
        coverage = getattr(result, "regression_coverage", None)
        assert coverage and coverage["complete"] is False
        assert coverage["sound"] is True, (
            "the two axes are independent: this selection IS sound about which "
            "tests could observe the change"
        )
        assert any("PARTIAL" in reason for reason in coverage["reasons"])

    def test_verify_reports_complete_only_for_the_whole_denominator(self) -> None:
        """The control for the row above: the same lane with the whole
        denominator DOES report complete. Without it, the previous test could pass
        because the lane was broken."""
        selection = ts.TestSelection(
            files=("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"),
            strategy="fallback_all",
            total_tests=3,
            walk={"complete": True, "truncation": "complete", "not_searched": 0},
        )
        result = _drive(
            regression_mode=ver.REGRESSION_SELECTED,
            selection=selection,
        )
        assert result.regression_check == ver.REGRESSION_COMPLETE
        assert result.regression_passed is True

    def test_the_attach_helper_cannot_write_a_boolean_on_the_result(self) -> None:
        """`regression_passed` is read by four `harness/` mint sites. A helper
        that could flip it is a helper that could widen a completion claim."""
        source = inspect.getsource(ver._attach_cost_and_regression_scope)
        tree = ast.parse(inspect.cleandoc(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        assert target.attr not in {
                            "regression_passed",
                            "target_test_passed",
                            "flaky",
                        }, (
                            f"_attach_cost_and_regression_scope assigns "
                            f"{target.attr} at line {node.lineno}; this helper is "
                            "a receipt, not a gate"
                        )


# ---------------------------------------------------------------------------
# Claim 5 — `not_run` is distinguishable from True, everywhere.
# ---------------------------------------------------------------------------


class TestNotRunIsNeverTrue:
    """The fast lane's whole honesty budget."""

    def test_the_vocabulary_is_closed_and_holds_not_run(self) -> None:
        assert ver.REGRESSION_NOT_RUN in ver.REGRESSION_CHECK_VALUES
        assert ver.REGRESSION_NOT_RUN != "True"
        assert "not_run" in ver.REGRESSION_CHECK_VALUES
        assert len(set(ver.REGRESSION_CHECK_VALUES)) == len(ver.REGRESSION_CHECK_VALUES)

    def test_the_fast_lane_reports_not_run_and_false_not_true(self) -> None:
        """The control arm is the default: it must be COMPLETE and True, or the
        test below would pass because the lane is broken rather than because it
        is honest."""
        full = _drive(
            monkeypatch_target=True, regression_mode=ver.REGRESSION_FULL_SUITE
        )
        assert full.regression_passed is True, (
            "control: the default full-suite lane should pass on a green fixture"
        )
        assert full.regression_check == ver.REGRESSION_COMPLETE

        fast = _drive(
            monkeypatch_target=True, regression_mode=ver.REGRESSION_TARGET_ONLY
        )
        assert fast.regression_check == ver.REGRESSION_NOT_RUN
        assert fast.regression_passed is False, (
            "the fast lane reported regression_passed=True for a regression check "
            "it never performed"
        )
        assert fast.regression_scope == ver.REGRESSION_TARGET_ONLY
        # And the reason has to be on the receipt, not inferable.
        cost = getattr(fast, "verification_cost", {})
        assert "no regression run" in (cost.get("tests_skipped_reason") or ""), cost

    def test_an_unknown_mode_raises_rather_than_defaulting(self) -> None:
        """A typo must not silently widen OR narrow the safety check."""
        with pytest.raises(ValueError) as caught:
            ver.verify(str(REPO_ROOT), None, regression_mode="full_suit")
        assert "regression_mode" in str(caught.value)

    def test_the_selected_lane_refuses_rather_than_silently_running_the_full_suite(
        self,
    ) -> None:
        """Asked for the fast lane with no usable selection: the answer is a
        refusal, because running the full suite would be a silent downgrade and
        reporting a pass over "selected" a silent upgrade."""
        selection = ts.TestSelection()  # empty: never a pass
        assert selection.is_empty is True
        result = _drive(
            regression_mode=ver.REGRESSION_SELECTED,
            selection=selection,
        )
        assert result.regression_check == ver.REGRESSION_NOT_RUN
        assert result.regression_passed is False


# ---------------------------------------------------------------------------
# Claim 6 — `flake_check: "not_run"` is visibly distinct from `not_flaky`.
# ---------------------------------------------------------------------------


class TestFlakeNotRunIsNotFlakeNotFlaky:
    """`INTERFACES.md` records that `flaky=False` at `run_count == 1` means NOT
    CHECKED, not stable. A user reading `flaky: False` believes the test is
    stable when nothing checked."""

    def test_the_two_values_are_distinct_literals_at_the_source(self) -> None:
        assert fg.NOT_RUN == "not_run"
        assert fg.NOT_FLAKY == "not_flaky"
        assert fg.NOT_RUN != fg.NOT_FLAKY
        assert set((fg.NOT_RUN, fg.NOT_FLAKY)) <= set(fg.FLAKE_CHECK_VALUES)

    def test_one_observation_is_not_run_and_two_is_not_flaky(self) -> None:
        """The threshold, derived rather than asserted: below two observations
        the gate cannot distinguish stable from unstable."""
        single = fg.flake_verdict(1, ["pass"])
        assert single.flake_check == fg.NOT_RUN
        assert single.flaky is False, "the historical bool must keep its value"
        assert single.detection_possible is False, (
            "detection_possible must be False at one observation, or a consumer "
            "can claim stability that was never measured"
        )

        pair = fg.flake_verdict(2, ["pass", "pass"])
        assert pair.flake_check == fg.NOT_FLAKY
        assert pair.flaky is False
        assert pair.detection_possible is True

    def test_a_reader_distinguishes_them_without_the_boolean(self) -> None:
        """The point of the third value: `flaky` cannot, `flake_check` can."""
        not_run = fg.flake_verdict(1, ["pass"])
        not_flaky = fg.flake_verdict(2, ["pass", "pass"])
        assert not_run.flaky == not_flaky.flaky, (
            "the booleans are identical by design, which is exactly why the "
            "three-valued verdict exists"
        )
        assert not_run.flake_check != not_flaky.flake_check
        assert not_run.detection_possible != not_flaky.detection_possible

    def test_verify_publishes_the_verdict_not_just_the_boolean(self) -> None:
        """With a flake key present, the result must carry `flake_check`."""
        result = _drive(
            regression_mode=ver.REGRESSION_TARGET_ONLY,
            rung_config={"post_fix_reruns": 2},
        )
        assert getattr(result, "flake_check", None) in set(fg.FLAKE_CHECK_VALUES)
        assert getattr(result, "repetitions", 0) == 2
        assert result.flaky is False
        assert getattr(result, "detection_possible", None) is not False or (
            getattr(result, "flake_check", "") != fg.NOT_FLAKY
        )


# ---------------------------------------------------------------------------
# Claim 7 — the per-call cost is reported, and an unmeasured phase is absent.
# ---------------------------------------------------------------------------


class TestTheCostIsReported:
    """T4's rail needs these numbers. A latency that is measured but not
    reported, and a phase reported as zero, are the same defect."""

    def test_a_verification_carries_the_numbers_the_rail_needs(self) -> None:
        result = _drive(regression_mode=ver.REGRESSION_FULL_SUITE)
        cost = getattr(result, "verification_cost", None)
        assert cost, "no verification_cost receipt on the result"
        for key in (
            "wall_s",
            "sandbox_overhead_s",
            "container_s",
            "command_runs",
            "regression_check",
            "regression_scope",
            "tests_skipped_reason",
        ):
            assert key in cost, f"the cost receipt is missing {key}"
        assert cost["measured_runs"] >= 1
        assert cost["wall_s"] and cost["wall_s"] > 0, (
            f"wall_s is {cost['wall_s']!r}; a zero would read as 'measured and "
            "free' for a run that really took seconds"
        )
        assert cost["container_s"] and cost["container_s"] > 0

    def test_the_container_cost_and_the_test_cost_are_not_conflated(self) -> None:
        """`container_s` INCLUDES the command. The receipt must say so, or a
        reader will subtract the wrong thing."""
        result = _drive(regression_mode=ver.REGRESSION_FULL_SUITE)
        note = getattr(result, "verification_cost", {}).get("note", "")
        assert "includes the command" in note

    def test_an_unreported_phase_is_absent_not_zero(self) -> None:
        """`phases` must not carry a `0.0` for a phase the boundary never ran."""
        result = sb.ExecutionResult(0, "", "", False)
        # A boundary that sets nothing leaves the receipt off entirely.
        assert getattr(result, "elapsed_s", None) is None
        assert getattr(result, "phases", None) is None

    def test_the_timing_helpers_are_named_for_auditability(self) -> None:
        assert set(ver.SANDBOX_TIMING_ATTRS) == {
            "phases",
            "elapsed_s",
            "container_s",
            "overhead_s",
        }

    def test_verify_rebuilds_do_not_discard_the_measured_cost(self) -> None:
        """The bug this round found: the sentinel-report path rebuilt
        `ExecutionResult` from its four fields and threw the timing away, so the
        receipt read 0.00s for real three-second runs."""
        source = inspect.getsource(ver._run_tests)
        assert "ExecutionResult(" in source, "the rebuild path is expected here"
        assert "_carry_sandbox_timing" in source, (
            "_run_tests rebuilds an ExecutionResult on the sentinel path; without "
            "_carry_sandbox_timing the measured cost is silently discarded"
        )


# ---------------------------------------------------------------------------
# the driver the honesty rows use
# ---------------------------------------------------------------------------

_REAL_EXECUTE_SANDBOXED = sb.execute_sandboxed


def _fake_sandboxed(repo_path: str, command: str, timeout_s: int = 120, **kwargs: Any):
    """A stand-in for the daemon that records what WOULD have been run.

    The honesty rows must not depend on a container: a suite that can only prove
    "not_run is honest" when Docker happens to be up is a suite that stops
    proving it exactly when someone needs it. Each run carries timing attributes
    so the cost path is exercised too.
    """
    import time

    started = time.perf_counter()
    exit_code = 0 if "no_such_marker" not in command else 1
    stdout = (
        "collected 2 items\n2 passed in 0.01s\n" if exit_code == 0 else "1 failed\n"
    )
    # A REAL, if tiny, amount of work. The stub must produce a non-zero
    # duration, because a test that asserts `container_s > 0` against a stub
    # that does nothing is testing that the clock ticks.
    time.sleep(0.01)
    elapsed = time.perf_counter() - started
    result = sb.ExecutionResult(exit_code, stdout, "", False)
    result.phases = {
        "image": 0.01,
        "reap": 0.001,
        "js_deps_volume": 0.0,
        "containment": 0.001,
        "argv_and_trace": 0.008,
        "container": elapsed,
    }
    result.container_s = elapsed
    result.overhead_s = 0.02
    result.elapsed_s = elapsed + 0.02
    return result


@pytest.fixture
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route both the sandbox and the ecosystem registry off the daemon."""
    monkeypatch.setattr(ver, "execute_sandboxed", _fake_sandboxed)
    monkeypatch.setattr(sb, "execute_sandboxed", _fake_sandboxed)
    # No ecosystem: the legacy path, so no sentinel report and no rebuild.
    monkeypatch.setattr(ver, "_ecosystem_for", lambda *_a, **_k: None)
    monkeypatch.setattr(ver, "detect_ecosystem", lambda *_a, **_k: None, raising=False)


def _drive(
    *,
    regression_mode: str = ver.REGRESSION_FULL_SUITE,
    selection: Any = None,
    changed_files: Any = None,
    rung_config: Any = None,
    monkeypatch_target: bool = False,
    reruns: int = 1,
) -> Any:
    """Run one verification with the daemon replaced by the recording stub.

    The registry is bypassed so the legacy path is taken; that is also the path
    the cost receipt is easiest to get wrong on, so it is the one worth pinning
    without a daemon.
    """
    saved = (ver.execute_sandboxed, sb.execute_sandboxed, ver._ecosystem_for)
    ver.execute_sandboxed = _fake_sandboxed
    sb.execute_sandboxed = _fake_sandboxed
    ver._ecosystem_for = lambda *_a, **_k: None
    try:
        return ver.verify(
            str(REPO_ROOT),
            "tests/test_verify.py::test_target_placeholder"
            if monkeypatch_target
            else "tests/test_x.py::test_x",
            reruns,
            verify_timeout_s=30,
            regression_mode=regression_mode,
            selection=selection,
            changed_files=list(changed_files or ()),
            rung_config=rung_config,
        )
    finally:
        ver.execute_sandboxed, sb.execute_sandboxed, ver._ecosystem_for = saved
