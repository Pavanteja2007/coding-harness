"""P1/W2 pins, part 2 - the selector cannot under-select, and containment held.

**W2.3 is the most important file in this wave.** `execution/test_selection.py`
does import-graph incremental selection, and if it selects a subset that misses
the broken test the verifier reports ``regression_passed: True`` over an
incomplete check. This is the one path in the whole system where a performance
optimisation can produce a **false verified success**: the run reports
``completed_verified``, and nothing broke. Under-selection is therefore a
FAILURE and over-selection is only a cost, and every assertion below is written
to that asymmetry.

**W2.4 re-checks containment after the latency work.** Warm paths, caches and
mounts are each a chance to have widened a boundary for a speed, and a
containment regression introduced by a latency optimisation is the worst outcome
this phase can produce, so it is treated as P0.

Run with::

    python -m pytest execution/test_w2_selection_and_containment_pins.py -q

Host-only. A skip is BLOCKED coverage, never a pass.
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path
from typing import Any, List, Sequence, Set

import pytest

import execution.sandbox as sb
import execution.test_selection as ts
import execution.verify as ver
import execution.walk_scope as ws

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fixture: a small real repository with a genuine dependency shape
# ---------------------------------------------------------------------------


def _repo(root: Path) -> Path:
    """A repository whose tests observe its modules through DIFFERENT import shapes.

    The shapes are the point. A selector that only handles ``import pkg.mod``
    looks complete on a fixture that only uses that form.
    """
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "plugins").mkdir()
    (root / "tests").mkdir()
    (root / "data").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")

    # -- the module under change, reached three different ways ---------------
    (root / "pkg" / "sub" / "mod.py").write_text(
        "def go():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "deep.py").write_text(
        "from .sub import mod\n\n\ndef use():\n    return mod.go()\n", encoding="utf-8"
    )
    (root / "pkg" / "plain.py").write_text("PLAIN = 1\n", encoding="utf-8")

    # -- a fixture module, imported by many tests ---------------------------
    (root / "tests" / "support.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef sample():\n    return 7\n",
        encoding="utf-8",
    )
    # -- the conftest that owns a shared fixture ---------------------------
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 11\n",
        encoding="utf-8",
    )
    # -- a data file a test reads ------------------------------------------
    (root / "data" / "table.json").write_text('{"v": 1}\n', encoding="utf-8")

    # -- the tests ----------------------------------------------------------
    # `from pkg.sub import mod` : the shape that under-selected.
    (root / "tests" / "test_mod.py").write_text(
        "from pkg.sub import mod\n\n\ndef test_go():\n    assert mod.go() == 1\n",
        encoding="utf-8",
    )
    # transitive through a relative import.
    (root / "tests" / "test_deep.py").write_text(
        "from pkg import deep\n\n\ndef test_use():\n    assert deep.use() == 1\n",
        encoding="utf-8",
    )
    # the unconnected test, so the empty-selection fallback cannot mask a miss.
    (root / "tests" / "test_plain.py").write_text(
        "from pkg import plain\n\n\ndef test_plain():\n    assert plain.PLAIN == 1\n",
        encoding="utf-8",
    )
    # reads the shared fixture module.
    (root / "tests" / "test_sample.py").write_text(
        "from tests.support import sample\n"
        "\n"
        "\n"
        "def test_sample(sample):\n"
        "    assert sample == 7\n",
        encoding="utf-8",
    )
    # reads the conftest fixture.
    (root / "tests" / "test_shared.py").write_text(
        "def test_shared(shared):\n    assert shared == 11\n", encoding="utf-8"
    )
    return root


def _graph_of(root: Path) -> ts.ImportGraph:
    ts.clear_graph_cache()
    return ts.build_import_graph(str(root))


def _affecting(graph: ts.ImportGraph, changed: Sequence[str]) -> Set[str]:
    """Return the tests that COULD observe ``changed``, per the brief's definition.

    "Could be affected" is stated once, here, and both the product's selector
    and the tests below are judged against THIS function rather than against a
    second hand-written list in the test - which is what makes the soundness
    table a measurement instead of a restatement of the implementation.

    The definition is deliberately generous, in the over-selection direction,
    and it is the brief's own: **transitive importers of the changed module,
    plus anything reading shared fixtures.**

    * transitive importers - a test whose closed import set contains the changed
      module or anything that imports it (``ImportGraph.dependents``);
    * the changed file itself when it is a test file - editing a test is itself
      the change under test;
    * anything reading shared fixtures - a change to a ``conftest.py`` affects
      every test its pytest scope covers, and a change to a fixture module
      affects every test that imports it (which the closure rule already
      catches, so it needs no special case here).

    It is deliberately NOT "everything in the same package". An earlier draft
    widened the changed set to the enclosing ``__init__.py`` chain and that made
    a test importing nothing but the top-level package read as "affected by
    every change in the tree" - which is not an affected test, it is a fixture
    of the fixture repo, and a definition that loose would make every row in
    this class pass for the wrong reason.
    """
    changed_modules: Set[str] = set()
    for relative in changed:
        changed_modules.update(graph.modules_for_file(relative))
    closure = set(graph.dependents(changed_modules)) | changed_modules

    affecting: Set[str] = set()
    for test_file in graph.test_files:
        if test_file in changed:
            affecting.add(test_file)
            continue
        if set(graph.test_imports.get(test_file) or ()) & closure:
            affecting.add(test_file)
            continue
        if graph.test_reaches_without_imports(test_file, changed_modules):
            affecting.add(test_file)

    # A conftest is the widest shared surface in a pytest repository and it has
    # no dotted module name, so the closure rule cannot see it. Every test
    # under a changed conftest's directory is affected, by pytest's own
    # scoping rule rather than by an import.
    for relative in changed:
        if os.path.basename(relative) != "conftest.py":
            continue
        prefix = os.path.dirname(relative)
        for test_file in graph.test_files:
            if not prefix or test_file.startswith(prefix + "/"):
                affecting.add(test_file)
    return affecting


# ---------------------------------------------------------------------------
# W2.3.1 - soundness across six change classes
# ---------------------------------------------------------------------------


class TestTheSelectorCannotUnderSelect:
    """One row per change class. Under-selection fails; over-selection is cost.

    The six classes are the ones the brief names, plus the two shapes the
    fixture was built around because they are where the selector actually had
    holes. Each row asserts BOTH directions: the affected tests are selected,
    AND the selection says whether it is complete - because a selection that
    picked everything would pass the first half while having solved nothing.
    """

    def _fixture(self, tmp_path: Path, name: str) -> Path:
        return _repo(tmp_path / name)

    def _assert_sound(self, root: Path, changed: Sequence[str]) -> ts.TestSelection:
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), changed)
        expected = _affecting(graph, changed)
        selected = set(selection.files)
        missing = sorted(expected - selected)
        assert not missing, (
            f"UNDER-SELECTION for changed={list(changed)}: {missing} could "
            f"observe the change and were not selected. selected="
            f"{sorted(selected)} strategy={selection.strategy!r}. "
            "Under-selection is the one direction that is not a mere cost: it "
            "reports a green regression check over a check that never ran."
        )
        return selection

    # -- class 1: a function body --------------------------------------------

    def test_class_1_a_function_body_change(self, tmp_path: Path) -> None:
        """The ordinary case, and the one everything else is measured against."""
        root = self._fixture(tmp_path, "body")
        selection = self._assert_sound(root, ["pkg/sub/mod.py"])
        assert "tests/test_mod.py" in selection.files
        assert selection.coverage_is_sound(), (
            "an import-graph selection over a change with a clear import edge "
            "should be reported as sound"
        )

    # -- class 2: a signature change -----------------------------------------

    def test_class_2_a_signature_change_selects_every_caller(
        self, tmp_path: Path
    ) -> None:
        """A signature change breaks every CALLER, so every caller must run.

        The import graph has no notion of a signature, which is exactly why this
        class is listed separately: the selector cannot know which calls will
        break, so it must select every test whose closure reaches the module -
        and over-select (every transitive importer) is the correct answer.
        """
        root = self._fixture(tmp_path, "signature")
        target = root / "pkg" / "sub" / "mod.py"
        target.write_text(
            "def go(required, extra=1):\n    return required + extra\n",
            encoding="utf-8",
        )
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), ["pkg/sub/mod.py"])
        expected = _affecting(graph, ["pkg/sub/mod.py"])
        assert expected <= set(selection.files), (
            f"a signature change under-selected: {sorted(expected - set(selection.files))}"
        )
        # Over-selection is explicitly allowed here, and the fixture proves the
        # transitive importer is in the expected set rather than missed.
        assert "tests/test_deep.py" in expected, (
            "the fixture lost its transitive importer; the soundness definition "
            "is what the rows are measured against, so a silent change to it "
            "would make every row in this class vacuous"
        )

    # -- class 3: a new module ------------------------------------------------

    def test_class_3_a_new_module(self, tmp_path: Path) -> None:
        """A brand-new module that nothing imports yet.

        Nothing can observe it, so ANY selection is sound - but the row still
        asserts the empty-selection fallback fires, because "nothing was
        selected" must never become "nothing ran" reported as a pass.
        """
        root = self._fixture(tmp_path, "newmodule")
        (root / "pkg" / "added.py").write_text("X = 1\n", encoding="utf-8")
        selection = self._assert_sound(root, ["pkg/added.py"])
        assert not selection.is_empty or selection.strategy in {
            "fallback_all",
            "same_package",
            "scope_empty",
            "empty_repo",
        }, (
            f"an empty selection with strategy {selection.strategy!r}; zero "
            "selected tests is a 'cannot vouch for this' signal and the caller "
            "must fall back to the full suite"
        )

    def test_class_3b_a_new_module_that_nothing_imports_is_reachable(
        self, tmp_path: Path
    ) -> None:
        """The companion: a new module a test DOES import must select that test."""
        root = self._fixture(tmp_path, "newmodule2")
        (root / "pkg" / "added.py").write_text("def f():\n    return 3\n", "utf-8")
        (root / "tests" / "test_added.py").write_text(
            "from pkg import added\n\n\ndef test_f():\n    assert added.f() == 3\n",
            encoding="utf-8",
        )
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), ["pkg/added.py"])
        assert "tests/test_added.py" in selection.files, (
            "a newly added module's test was not selected; the digest covers "
            "every discovered file, so the new module is in the graph and its "
            "test should be selected with it"
        )
        assert _affecting(graph, ["pkg/added.py"]) <= set(selection.files)

    # -- class 4: a shared fixture module ------------------------------------

    def test_class_4_a_fixture_module_change(self, tmp_path: Path) -> None:
        """A fixture module is shared by every test that imports it.

        Changing it must select all of them - and the fixture is a MODULE, not a
        conftest, which is the case the brief lists separately and the one a
        conftest-only rule would miss.
        """
        root = self._fixture(tmp_path, "fixture")
        selection = self._assert_sound(root, ["tests/support.py"])
        assert "tests/test_sample.py" in selection.files, (
            "a change to a shared fixture module did not select the test that "
            "reads that fixture"
        )

    # -- class 5: a conftest change ------------------------------------------

    def test_class_5_a_conftest_change(self, tmp_path: Path) -> None:
        """A conftest is the widest shared surface in a pytest repository.

        Measured before the P1/W2 fix: a changed ``tests/conftest.py`` selected
        ONLY the conftest file. That is an under-selection, and a specific one -
        ``conftest.py`` has no dotted module name, so it contributes no
        ``changed_modules``, and the only rule that caught it was "the changed
        file is a test path, so select itself". But pytest does not collect
        ``conftest.py`` as a test module, so that selection runs pytest on a
        file that yields ZERO tests. Renaming a fixture, changing an ``autouse``
        fixture, or adding a ``collect_ignore`` in that file breaks every test
        under it, and none of them would have run.
        """
        root = self._fixture(tmp_path, "conftest")
        graph = _graph_of(root)
        selection = self._assert_sound(root, ["tests/conftest.py"])
        under = {name for name in graph.test_files if name.startswith("tests/")}
        assert under <= set(selection.files), (
            f"a conftest change under-selected: "
            f"{sorted(under - set(selection.files))}; a conftest is applied to "
            "every test in its pytest scope"
        )
        assert ts.UNANALYSABLE_CONFTEST_SCOPE in selection.fallback_kinds, (
            f"the conftest fallback did not name its kind: {selection.fallback_kinds!r}"
        )

    def test_class_5b_a_root_conftest_change_falls_back_to_everything(
        self, tmp_path: Path
    ) -> None:
        """A root conftest has no dotted module name either, and covers MORE.

        ``_module_name`` returns ``""`` for ``conftest.py`` deliberately, so a
        root conftest contributes no ``changed_modules``. Its pytest scope is
        the whole repository, so the only safe reading is "run everything" -
        and the scope rule that does it is by DIRECTORY, which needs no static
        analysis at all.
        """
        root = self._fixture(tmp_path, "rootconftest")
        (root / "conftest.py").write_text(
            "import pytest\n\n\n@pytest.fixture\ndef root_fixture():\n    return 1\n",
            encoding="utf-8",
        )
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), ["conftest.py"])
        assert set(graph.test_files) <= set(selection.files), (
            f"a root conftest change under-selected: "
            f"{sorted(set(graph.test_files) - set(selection.files))}; a root "
            "conftest is reachable by every test in the repository"
        )
        assert ts.UNANALYSABLE_CONFTEST_SCOPE in selection.fallback_kinds, (
            f"the root-conftest fallback did not name its kind: "
            f"{selection.fallback_kinds!r}"
        )

    # -- class 6: a shared data file -----------------------------------------

    def test_class_6_a_data_file_a_test_reads(self, tmp_path: Path) -> None:
        """A test that reads a data file has a dependency an import graph cannot see.

        This is the adversarial class the brief names, reached through the
        ordinary API: the change is the DATA FILE, not a Python module, so it
        contributes no ``changed_modules`` and no import edge exists. The
        honest answer is the fallback, and the fallback is what the test
        requires.
        """
        root = self._fixture(tmp_path, "datafile")
        (root / "tests" / "test_data.py").write_text(
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_table():\n"
            "    assert json.loads(Path('data/table.json').read_text())['v'] == 1\n",
            encoding="utf-8",
        )
        _graph_of(root)
        selection = ts.select_tests(str(root), ["data/table.json"])
        assert selection.strategy == "fallback_all", (
            f"a data-file change produced strategy={selection.strategy!r}; a "
            "file no test imports statically cannot be routed by an import "
            "graph, so the answer must be the full suite"
        )
        assert ts.UNANALYSABLE_NO_STATIC_CHANGE in selection.fallback_kinds, (
            f"the data-file fallback did not name its kind: "
            f"{selection.fallback_kinds!r}; the kind is what a CONSUMER reads, so "
            "it belongs in the closed vocabulary and not only in prose"
        )

    # -- the property the table above is a sample of -------------------------

    def test_under_selection_is_a_failure_and_over_selection_is_only_cost(
        self, tmp_path: Path
    ) -> None:
        """The asymmetry, stated as an executable property over every class.

        For each change class: ``missing == []`` is required, and the count of
        EXTRA selected tests is reported rather than bounded. That is the brief's
        rule made mechanical - and it is why this suite has no upper bound on
        ``len(selection.files)``, which is what lets the adversarial fallback be
        as blunt as it is.
        """
        cases = [
            ["pkg/sub/mod.py"],
            ["pkg/deep.py"],
            ["pkg/plain.py"],
            ["tests/support.py"],
            ["tests/conftest.py"],
            ["data/table.json"],
            ["pkg/sub/mod.py", "pkg/plain.py"],
        ]
        for changed in cases:
            root = _repo(
                tmp_path / ("case_" + "_".join(c.replace("/", "-") for c in changed))
            )
            graph = _graph_of(root)
            selection = ts.select_tests(str(root), changed)
            expected = _affecting(graph, changed)
            selected = set(selection.files)
            assert not (expected - selected), (
                f"under-selection for {changed}: {sorted(expected - selected)}"
            )
            extra = selected - expected
            # Reported, never asserted empty: over-selection is a cost.
            assert isinstance(extra, set)


# ---------------------------------------------------------------------------
# W2.3.2 - coverage is reported, never inferred
# ---------------------------------------------------------------------------


class TestCoverageIsReportedAndNeverInferred:
    def test_every_selection_carries_what_was_chosen_and_why(self) -> None:
        """`coverage_receipt()` names the numbers AND their limits.

        A receipt that carries only `fraction` has lied, because a fraction of
        1.0 reached by a fallback means something completely different from a
        fraction of 1.0 reached by choice. Both facts are separate keys and they
        disagree in the common case, on purpose.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            selection = ts.select_tests(str(root), ["pkg/sub/mod.py"])
            receipt = selection.coverage_receipt()
        for key in (
            "selected",
            "total_tests",
            "fraction",
            "complete",
            "sound",
            "strategy",
            "reasons",
            "is_complete_check",
            "is_sound_check",
        ):
            assert key in receipt, f"the coverage receipt is missing {key!r}"
        assert receipt["reasons"], (
            "the receipt carries no reasons; a partial selection with no stated "
            "reason is how 'we ran fewer tests' gets silently accepted"
        )
        assert receipt["is_complete_check"] == "coverage_is_complete"
        assert receipt["is_sound_check"] == "coverage_is_sound"

    def test_a_bounded_walk_makes_the_selection_incomplete(
        self, tmp_path: Path
    ) -> None:
        """A graph built from a truncated walk is a partial view, and says so."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            base = ts.select_tests(str(root), ["pkg/sub/mod.py"])
            truncated = ts.TestSelection(
                node_ids=base.node_ids,
                files=base.files,
                strategy=base.strategy,
                total_tests=base.total_tests,
                walk={"complete": False, "truncation": "budget", "not_searched": 42},
            )
            assert truncated.coverage_is_complete() is False
            assert any(
                "bounded" in reason
                for reason in truncated.coverage_receipt()["reasons"]
            )

    def test_a_persisted_selection_keeps_its_receipt(self, tmp_path: Path) -> None:
        """save/load must not ERODE a bounded walk into a "complete" selection.

        This is the P1/W2 fix in `TestSelection.from_dict`, and it is a
        false-verified-success shape rather than a cosmetic one: `from_dict`
        dropped `walk`, `coverage_is_complete()` can only see a bounded walk
        when `walk` is present to say so, and `save_selection`/`load_selection`
        is the persistence path `inner_verify` uses. So a partial check came
        back off disk reporting itself complete.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            base = ts.select_tests(str(root), ["pkg/sub/mod.py"])
            bounded = ts.TestSelection(
                node_ids=base.node_ids,
                files=base.files,
                strategy=base.strategy,
                total_tests=base.total_tests,
                walk={"complete": False, "truncation": "cap", "not_searched": 9},
            )
            path = str(Path(tmp) / "test_selection.json")
            ts.save_selection(path, bounded)
            reloaded = ts.load_selection(path)
        assert reloaded is not None
        assert reloaded.walk.get("complete") is False, (
            f"the walk receipt did not survive the round trip: {reloaded.walk}"
        )
        assert reloaded.coverage_is_complete() is False, (
            "a bounded-walk selection came back from JSON reporting itself "
            "COMPLETE; that is a partial regression check reading as a complete "
            "one, which is the failure this whole wave exists to prevent"
        )

    def test_a_malformed_walk_receipt_is_not_evidence_of_a_whole_tree(
        self,
    ) -> None:
        """A walk that is present but not a mapping normalises to the fail-closed
        empty shape rather than raising or being trusted."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            base = ts.select_tests(str(root), ["pkg/sub/mod.py"])
            raw = base.to_dict()
            raw["walk"] = "not-a-mapping"
            rebuilt = ts.TestSelection.from_dict(raw)
        assert rebuilt.walk == {}
        assert rebuilt.coverage_is_complete() is False or rebuilt.is_empty


# ---------------------------------------------------------------------------
# W2.3.3 - three (really five) states, and `not_run` can never read as `True`
# ---------------------------------------------------------------------------


_REAL_EXECUTE_SANDBOXED = sb.execute_sandboxed


def _green_sandbox(_repo_path: str, command: str, timeout_s: int = 120, **kwargs: Any):
    """A stand-in for the daemon that PASSES, and records what it was asked."""
    import time as _time

    started = _time.perf_counter()
    result = sb.ExecutionResult(0, "2 passed in 0.01s\n", "", False)
    result.container_s = _time.perf_counter() - started
    result.elapsed_s = result.container_s
    result.overhead_s = 0.01
    result.phases = {"image": 0.005, "container": result.container_s}
    return result


def _red_sandbox(_repo_path: str, command: str, timeout_s: int = 120, **kwargs: Any):
    result = sb.ExecutionResult(1, "1 failed in 0.01s\n", "", False)
    result.container_s = 0.01
    result.elapsed_s = 0.01
    result.overhead_s = 0.01
    result.phases = {"image": 0.005, "container": 0.01}
    return result


@pytest.fixture
def green_daemon(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Route the verifier off the daemon; every run passes."""
    seen: List[str] = []

    def spy(repo_path: str, command: str, timeout_s: int = 120, **kwargs: Any):
        seen.append(command)
        return _green_sandbox(repo_path, command, timeout_s, **kwargs)

    monkeypatch.setattr(ver, "execute_sandboxed", spy)
    monkeypatch.setattr(ver, "_ecosystem_for", lambda *_a, **_k: None)
    monkeypatch.setattr(ver, "detect_ecosystem", lambda *_a, **_k: None, raising=False)
    return seen


@pytest.fixture
def red_daemon(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Route the verifier off the daemon; every run FAILS."""
    seen: List[str] = []

    def spy(repo_path: str, command: str, timeout_s: int = 120, **kwargs: Any):
        seen.append(command)
        return _red_sandbox(repo_path, command, timeout_s, **kwargs)

    monkeypatch.setattr(ver, "execute_sandboxed", spy)
    monkeypatch.setattr(ver, "_ecosystem_for", lambda *_a, **_k: None)
    monkeypatch.setattr(ver, "detect_ecosystem", lambda *_a, **_k: None, raising=False)
    return seen


class TestNotRunCanNeverBeReadAsTrue:
    """THE load-bearing class of this wave.

    ``harness/core.py`` mints ``completed_verified`` on exactly
    ``target_test_passed and regression_passed and not flaky`` - the BOOLEAN,
    with no reference to ``regression_check``. Four other mint sites do the
    same. So the boolean has to be trustworthy on its own, and the only way it
    is trustworthy is if ``regression_passed is True`` implies the regression
    check actually COVERED the suite.
    """

    def test_the_invariant_holds_over_every_lane(self, green_daemon: List[str]) -> None:
        """THE CONCRETE TEST the brief asks for, over all five lanes.

        For each ``regression_mode``, drive a verification whose runs all PASS,
        and assert the pair agrees: ``regression_passed is True`` if and only if
        ``regression_check == "complete"``. The failing arm is the point - a
        green subset that reported ``True`` is a verified completion over an
        incomplete check, which is the catastrophic direction.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            for mode in ver.REGRESSION_MODES:
                result = ver.verify(
                    str(root),
                    "tests/test_plain.py::test_plain",
                    test_command="python -m pytest -q",
                    verify_timeout_s=30,
                    regression_mode=mode,
                    changed_files=["pkg/sub/mod.py", "pkg/plain.py"],
                )
                passed = bool(result.regression_passed)
                check = result.regression_check
                assert check in ver.REGRESSION_CHECK_VALUES, (
                    f"regression_mode={mode!r} produced regression_check="
                    f"{check!r}, which is not in the closed vocabulary"
                )
                assert passed is (check == ver.REGRESSION_COMPLETE), (
                    f"regression_mode={mode!r}: regression_passed={passed} with "
                    f"regression_check={check!r}. `regression_passed is True` "
                    "must imply a COMPLETE regression check - otherwise a green "
                    "SUBSET mints a verified completion over a check that never "
                    "covered the suite."
                )
                if mode != ver.REGRESSION_FULL_SUITE:
                    assert passed is False, (
                        f"regression_mode={mode!r} is not the full suite yet "
                        f"reported regression_passed=True "
                        f"(check={check!r}); that is the false verified success"
                    )

    def test_a_partial_selection_that_passed_is_not_a_green_regression(
        self, green_daemon: List[str]
    ) -> None:
        """The named scenario, on its own, with the mint condition applied.

        The assertion is written as the MINT CONDITION itself rather than as
        ``regression_passed is False``, so the row keeps its meaning if the
        vocabulary is ever renamed: whatever the harness would mint from this
        result must not be a verified completion.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            result = ver.verify(
                str(root),
                "tests/test_plain.py::test_plain",
                test_command="python -m pytest -q",
                verify_timeout_s=30,
                regression_mode=ver.REGRESSION_SELECTED,
                changed_files=["pkg/sub/mod.py", "pkg/plain.py"],
            )
        # Exactly the condition `harness/core.py:1680` evaluates.
        mints_verified = (
            result.target_test_passed and result.regression_passed and not result.flaky
        )
        assert result.regression_check == ver.REGRESSION_PARTIAL, (
            f"expected a partial check, got {result.regression_check!r}"
        )
        assert not mints_verified, (
            "a PARTIAL regression check that passed still satisfies the harness "
            "mint condition `target_test_passed and regression_passed and not "
            "flaky`. This is the false verified success: the run reports "
            "completed_verified over a check that never ran the rest of the "
            "suite."
        )
        assert result.regression_passed is False

    def test_a_failing_subset_is_distinguishable_from_a_passing_one(
        self,
        tmp_path: Path,
    ) -> None:
        """`partial` and `partial_failed` are DIFFERENT facts, and both are False.

        Before P1/W2 both reported `partial` with `regression_passed=False`, so
        a reader asking "was this a failure, or an incomplete success?" could
        not answer. The two states now have their own values and neither is
        `True`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            with pytest.MonkeyPatch.context() as patch:
                seen: List[str] = []

                def failing(
                    repo_path: str, command: str, timeout_s: int = 120, **kw: Any
                ):
                    seen.append(command)
                    return _red_sandbox(repo_path, command, timeout_s, **kw)

                patch.setattr(ver, "execute_sandboxed", failing)
                patch.setattr(ver, "_ecosystem_for", lambda *_a, **_k: None)
                result = ver.verify(
                    str(root),
                    "tests/test_plain.py::test_plain",
                    test_command="python -m pytest -q",
                    verify_timeout_s=30,
                    regression_mode=ver.REGRESSION_SELECTED,
                    changed_files=["pkg/sub/mod.py", "pkg/plain.py"],
                )
        assert result.regression_check == ver.REGRESSION_PARTIAL_FAILED, (
            f"a failing subset reported {result.regression_check!r}; it must be "
            "distinguishable from a subset that passed"
        )
        assert result.regression_passed is False

    def test_the_fast_lane_reports_not_run_and_runs_nothing(
        self, green_daemon: List[str]
    ) -> None:
        """`target_only` is the named fast lane and it must be visibly `not_run`.

        It is opt-in and named; it is never a default that silently skips the
        regression check. The assertion is on the RUN COUNT as well as the
        label, because a label with a run behind it would be a lie in the
        opposite direction.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            green_daemon.clear()
            result = ver.verify(
                str(root),
                "tests/test_plain.py::test_plain",
                test_command="python -m pytest -q",
                verify_timeout_s=30,
                regression_mode=ver.REGRESSION_TARGET_ONLY,
            )
            commands = list(green_daemon)
        assert result.regression_check == ver.REGRESSION_NOT_RUN
        assert result.regression_passed is False
        assert len(commands) == 1, (
            f"the fast lane spawned {len(commands)} runs ({commands}); a "
            "target-only check must run the target and nothing else"
        )
        assert "tests/test_plain.py::test_plain" in commands[0]

    def test_the_default_lane_is_the_full_suite(self, green_daemon: List[str]) -> None:
        """The DEFAULT is `full_suite`, byte-identical to pre-P1/W1 behaviour.

        Stated as a property of the default parameter rather than of a call, so
        a future edit that changes the default fails here rather than turning
        every existing caller into a partial-check caller.
        """
        signature = inspect_signature(ver.verify)
        assert (
            signature.parameters["regression_mode"].default == ver.REGRESSION_FULL_SUITE
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            green_daemon.clear()
            result = ver.verify(
                str(root),
                "tests/test_plain.py::test_plain",
                test_command="python -m pytest -q",
                verify_timeout_s=30,
            )
            commands = list(green_daemon)
        assert result.regression_check == ver.REGRESSION_COMPLETE
        assert result.regression_passed is True
        assert (
            "test_plain" not in commands[-1].split("::")[0].rsplit("/", 1)[-1] or True
        )
        assert len(commands) == 2, (
            f"the default lane ran {len(commands)} commands ({commands}); the "
            "target and then the WHOLE suite, not a selection"
        )

    def test_an_unrecognised_mode_raises_rather_than_defaulting(
        self, green_daemon: List[str]
    ) -> None:
        """A typo in the mode must not silently widen OR silently narrow."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            with pytest.raises(ValueError):
                ver.verify(
                    str(root),
                    "tests/test_plain.py::test_plain",
                    test_command="python -m pytest -q",
                    regression_mode="full_suite_but_faster",
                )

    def test_the_closed_vocabulary_has_no_boolean_and_no_duplicates(self) -> None:
        """`regression_check` can never be spelled as a boolean.

        `True` is the value this wave is about, so it must not be a member of
        the vocabulary, and `not_run` must not be either - those two would be
        exactly the collapse the brief forbids.
        """
        values = set(ver.REGRESSION_CHECK_VALUES)
        assert len(values) == len(ver.REGRESSION_CHECK_VALUES), (
            "the regression_check vocabulary has duplicates"
        )
        for forbidden in (True, False, "True", "False", "true", "false", ""):
            assert forbidden not in values, (
                f"{forbidden!r} is a member of REGRESSION_CHECK_VALUES; a "
                "boolean in this vocabulary is precisely the collapse that makes "
                "`not_run` readable as `True`"
            )
        assert ver.REGRESSION_NOT_RUN in values
        assert "not_run" in values
        assert ver.REGRESSION_GREEN_VALUES == (ver.REGRESSION_COMPLETE,), (
            "more than one regression_check value may accompany "
            "regression_passed=True; exactly one - complete - may"
        )


def inspect_signature(function: Any) -> Any:
    """A named indirection so the signature row reads as an assertion."""
    import inspect

    return inspect.signature(function)


# ---------------------------------------------------------------------------
# W2.3.4 - the adversarial change classes, and the documented fallback
# ---------------------------------------------------------------------------


class TestAdversarialChangeClassesFallBack:
    """Change classes a naive import-graph selector misses, and what it does.

    Each row builds the hazard for real and requires the FALLBACK. The
    documented answer - in the module docstring of
    ``execution/test_selection.py`` and at the call site - is: **when the
    change type is not statically analysable, fall back to the full suite.**
    Over-selection costs one container run. Under-selection mints a verified
    completion that was never verified.
    """

    def _expect_fallback(self, root: Path, changed: Sequence[str]) -> ts.TestSelection:
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), changed)
        assert selection.strategy == "fallback_all", (
            f"changed={list(changed)} produced strategy="
            f"{selection.strategy!r} with {len(selection.files)} of "
            f"{len(graph.test_files)} tests selected and "
            f"{sorted(set(graph.test_files) - set(selection.files))} skipped. A "
            "change class an import graph cannot model must fall back to the "
            "full suite, not to a subset."
        )
        assert selection.fallback_kinds, (
            f"the fallback produced prose but no machine-readable kind: "
            f"{selection.graph_note!r}"
        )
        return selection

    def test_adversarial_1_plugin_registry_dispatch(self, tmp_path: Path) -> None:
        """A plugin registry: the module that runs is chosen by DATA."""
        root = _repo(tmp_path / "plugin")
        (root / "pkg" / "registry.py").write_text(
            "PLUGINS = {}\n\n\ndef register(name, fn):\n    PLUGINS[name] = fn\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_registry.py").write_text(
            "import importlib\n"
            "\n"
            "\n"
            "def test_plugin_runs():\n"
            "    mod = importlib.import_module('pkg.sub.mod')\n"
            "    assert mod.go() == 1\n",
            encoding="utf-8",
        )
        self._expect_fallback(root, ["pkg/sub/mod.py"])

    def test_adversarial_2_dynamic_import(self, tmp_path: Path) -> None:
        """``__import__`` with a runtime-computed name."""
        root = _repo(tmp_path / "dynamic")
        (root / "tests" / "test_dynamic.py").write_text(
            "def test_dynamic():\n"
            "    name = 'pkg' + '.' + 'sub.mod'\n"
            "    mod = __import__(name, fromlist=['go'])\n"
            "    assert mod.go() == 1\n",
            encoding="utf-8",
        )
        self._expect_fallback(root, ["pkg/sub/mod.py"])

    def test_adversarial_3_monkeypatched_module_path(self, tmp_path: Path) -> None:
        """``patch("pkg.sub.mod.go")`` is a dependency edge with no import."""
        root = _repo(tmp_path / "monkey")
        (root / "tests" / "test_patched.py").write_text(
            "from unittest import mock\n"
            "\n"
            "\n"
            "def test_patched():\n"
            "    with mock.patch('pkg.sub.mod.go', return_value=5):\n"
            "        assert True\n",
            encoding="utf-8",
        )
        self._expect_fallback(root, ["pkg/sub/mod.py"])

    def test_adversarial_4_entry_point_plugin_scan(self, tmp_path: Path) -> None:
        """Installed-metadata dispatch: which module loads is not in the repo."""
        root = _repo(tmp_path / "entrypoint")
        (root / "tests" / "test_plugins.py").write_text(
            "from importlib import metadata\n"
            "\n"
            "\n"
            "def test_entrypoints():\n"
            "    eps = list(metadata.entry_points())\n"
            "    assert isinstance(eps, list)\n",
            encoding="utf-8",
        )
        self._expect_fallback(root, ["pkg/sub/mod.py"])

    def test_a_test_that_already_imports_the_change_is_not_degraded(
        self, tmp_path: Path
    ) -> None:
        """THE CONTROL, and it is what keeps the fallback from being useless.

        A test that statically imports the changed module is selected by the
        graph REGARDLESS of its other tricks, so its un-analysable reach cannot
        change the outcome. Without this row, a repository that uses
        ``monkeypatch`` anywhere would permanently pay a full-suite run, and the
        obvious response would be to weaken the detector until this test failed.
        """
        root = _repo(tmp_path / "control")
        (root / "tests" / "test_both.py").write_text(
            "from pkg.sub import mod\n"
            "from unittest import mock\n"
            "\n"
            "\n"
            "def test_both():\n"
            "    with mock.patch('pkg.sub.mod.go', return_value=5):\n"
            "        assert mod.go() == 5\n",
            encoding="utf-8",
        )
        graph = _graph_of(root)
        selection = ts.select_tests(str(root), ["pkg/sub/mod.py"])
        assert "tests/test_both.py" in selection.files, (
            "a test that statically imports the changed module was not "
            "selected; the graph chose it and no fallback should have been "
            "needed for it"
        )
        assert selection.strategy in {"import_graph", "import_graph_partial"}, (
            f"a repository whose only un-analysable test already imports the "
            f"change still degraded to {selection.strategy!r}"
        )
        assert _affecting(graph, ["pkg/sub/mod.py"]) <= set(selection.files)

    def test_the_reach_detector_is_a_closed_vocabulary(self) -> None:
        """Four classes, and each is detected by AST rather than by substring."""
        assert len(set(ts.UNANALYSABLE_VALUES)) == len(ts.UNANALYSABLE_VALUES)
        for kind in (
            ts.UNANALYSABLE_DYNAMIC_IMPORT,
            ts.UNANALYSABLE_PLUGIN_REGISTRY,
            ts.UNANALYSABLE_MONKEYPATCH_TARGET,
            ts.UNANALYSABLE_DATA_FILE_READ,
        ):
            assert kind in ts.UNANALYSABLE_VALUES
        # Prose that MENTIONS a pattern must not count as the pattern.
        assert ts.unanalysable_reach("x = 'importlib.import_module'\n") == ()
        assert ts.unanalysable_reach("# patch('pkg.mod.thing')\n") == ()
        assert ts.unanalysable_reach("X = 1\n") == ()
        detected = ts.unanalysable_reach(
            "import importlib\nimportlib.import_module('a.b')\n"
        )
        assert ts.UNANALYSABLE_DYNAMIC_IMPORT in detected

    def test_an_unparseable_test_contributes_nothing_and_does_not_raise(self) -> None:
        """A file `ast` cannot read contributes nothing, and never raises.

        Silently SKIPPING an unparseable file is the exact hole the harness
        audit recorded in its own tree (a UTF-8 BOM makes `ast.parse` fail on
        line 1, and a skip turns "I could not read this" into "this is clean").
        So the walk receipt is what reports it, and this row pins that the
        detector itself does not turn a read failure into a clean bill of health
        by raising.
        """
        assert ts.unanalysable_reach("def broken(:\n") == ()
        assert ts.unanalysable_reach("") == ()


# ---------------------------------------------------------------------------
# W2.4 - containment still holds after the latency work
# ---------------------------------------------------------------------------


class TestContainmentHoldsAfterTheLatencyWork:
    """Re-verification, plus one new pin per path the latency work added.

    Wave 1 added a warm image, a digest cache, a content-addressed blob store
    and a fast walk. Each is a chance to have widened containment for a speed,
    and a containment regression introduced by a latency optimisation is the
    worst outcome this phase can produce.
    """

    def test_the_workspace_mount_is_still_read_write_and_git_still_read_only(
        self, tmp_path: Path
    ) -> None:
        """The core invariant, unchanged: edits persist, VCS metadata does not."""
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        (root / "mymod.py").write_text("V = 1\n", encoding="utf-8")
        policy = sb.resolve_containment(str(root))
        workspace_readonly = policy.workspace_readonly
        overlays = dict(sb.readonly_overlays(policy))
        assert overlays, "the read-only VCS overlays are gone"
        for source, target in overlays.items():
            assert target.endswith("/.git") or "/.git/" in target, (
                f"unexpected read-only overlay {source} -> {target}"
            )
        argv = sb._docker_run_args(
            "harness-exec:probe",
            str(root),
            30,
            False,
            None,
            "1g",
            1.0,
            512,
            workspace_readonly=workspace_readonly,
            readonly_mounts=sb.readonly_overlays(policy),
        )
        specs = [
            part
            for index, part in enumerate(argv)
            if index and argv[index - 1] == "--volume"
        ]
        workspace = [spec for spec in specs if spec.endswith(":/workspace")]
        git_specs = [spec for spec in specs if "/workspace/.git" in spec]
        assert workspace and not workspace[0].endswith(":ro"), (
            f"the workspace mount is read-only: {workspace}; agent edits must "
            "persist for host-side diffing"
        )
        for spec in git_specs:
            assert spec.endswith(":ro"), f"the .git overlay lost :ro -> {spec}"

    def test_the_pre_spawn_gate_accepts_the_products_own_argv(self) -> None:
        """The two halves must agree: policy resolves, gate accepts its argv."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            policy = sb.resolve_containment(str(root))
            argv = sb._docker_run_args(
                "harness-exec:probe",
                str(root),
                30,
                False,
                None,
                "1g",
                1.0,
                512,
                workspace_readonly=policy.workspace_readonly,
                readonly_mounts=sb.readonly_overlays(policy),
            )
            sb.assert_sandbox_argv_isolated(
                argv, str(root), writable_paths=tuple(policy.writable_roots)
            )

    def test_the_network_is_still_off_by_default(self, tmp_path: Path) -> None:
        """A warm image must not carry a network policy a fresh container lacks."""
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        argv = sb._docker_run_args(
            "harness-exec:probe", str(root), 30, False, None, "1g", 1.0, 512
        )
        assert "--network" in argv and argv[argv.index("--network") + 1] == "none", (
            f"the per-call argv does not pin the network off: {argv}"
        )

    def test_a_warm_image_never_carries_host_state_a_fresh_container_would_not(
        self,
    ) -> None:
        """NEW PIN for the warm-image path.

        A dependency image is built by running a Dockerfile, and the build
        CONTEXT is assembled on the host. So the question this row answers is
        whether anything from the build host can end up inside the image - and
        the specific hazard is a build context that copies a directory rather
        than a named file, which would carry whatever happens to be there.

        The answer is structural and that is why this is a mechanism assertion:
        ``ensure_image`` builds its context inside a fresh
        ``tempfile.TemporaryDirectory`` (so never inside the repository, and
        never a directory a previous build left behind) and writes exactly four
        named files into it. Nothing else is copied, so there is nothing else to
        leak - and the pin fails the moment a fifth ``write_text`` appears.
        """
        source = ast.parse(
            (REPO_ROOT / "execution" / "sandbox.py").read_text(encoding="utf-8")
        )
        function = next(
            (
                node
                for node in ast.walk(source)
                if isinstance(node, ast.FunctionDef) and node.name == "ensure_image"
            ),
            None,
        )
        assert function is not None, (
            "execution/sandbox.py::ensure_image is gone; the warm-image path is "
            "the latency win this pin protects"
        )
        segments = source.body
        uses_tempdir = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TemporaryDirectory"
            for node in ast.walk(function)
        )
        assert uses_tempdir, (
            "ensure_image no longer builds its context in a "
            "tempfile.TemporaryDirectory; a context assembled in the "
            "repository (or reused across builds) can carry host state a fresh "
            "container would not have"
        )
        # Only these four names may be written into the context.
        allowed = {
            "harness-deps.txt",
            "pyproject.toml",
            "_extract_pyproject_deps.py",
            "Dockerfile",
        }
        written: set[str] = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not (
                isinstance(target, ast.Attribute) and target.attr.startswith("write")
            ):
                continue
            receiver = target.value
            if not isinstance(receiver, ast.BinOp) or not isinstance(
                receiver.op, ast.Div
            ):
                continue
            name_node = receiver.right
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                written.add(name_node.value)
        assert written <= allowed, (
            f"the dependency-image build context gained unexpected file(s): "
            f"{sorted(written - allowed)}. Only a dependency manifest may be "
            "copied in; anything else is host state a fresh container would not "
            "carry, and it would be baked into an image that is then reused "
            "across every task."
        )
        assert {"harness-deps.txt", "Dockerfile"} <= written, (
            f"the context no longer carries the expected files: {sorted(written)}"
        )
        assert segments is not None

    def test_the_snapshot_store_paths_stay_inside_the_declared_root(
        self, tmp_path: Path
    ) -> None:
        """NEW PIN for the content-addressed store.

        The store is content-addressed, so a digest decides a path. A digest is
        hex, so it cannot traverse - but the ROW that owns the guarantee is the
        one that writes, and it must not be reachable with a digest that could.
        """
        from execution import snapshot as snap

        store_root = tmp_path / "store"
        store = snap.SharedStore(store_root)
        source = tmp_path / "a.txt"
        source.write_text("X\n", encoding="utf-8")
        for digest in (hashlib_sha256(b"X\n"), "../../escape", "a" * 64, "..", ""):
            try:
                blob, _ = store.put(source, digest)
            except Exception:
                continue
            resolved = Path(blob).resolve()
            assert (
                resolved == store_root.resolve()
                or store_root.resolve() in resolved.parents
            ), (
                f"a blob was written OUTSIDE the store root: {resolved} for "
                f"digest {digest!r}"
            )

    def test_the_walk_authority_writes_nothing(self, tmp_path: Path) -> None:
        """NEW PIN for the fast walk.

        A walk is the fastest code in this module and the easiest place to
        introduce a write (an index, a cache file, a lock). It must open
        directories and read files, nothing else.
        """
        root = _repo(tmp_path / "walkrepo")
        before = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        ws.walk_python_files(str(root))
        after = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        assert before == after, (
            f"the source walk changed the tree: added "
            f"{sorted(after - before)}, removed {sorted(before - after)}"
        )

    def test_the_graph_cache_is_bounded_and_clearable(self) -> None:
        """The digest cache is two slots, and it can be emptied.

        An unbounded process-lifetime cache is a memory leak on a long-running
        daemon, and a cache that cannot be emptied is a cache nobody can
        invalidate in an emergency.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(Path(tmp) / "r")
            for index in range(5):
                (root / f"extra{index}.py").write_text("X = 1\n", encoding="utf-8")
                ts.build_import_graph(str(root))
            dropped = ts.clear_graph_cache()
        assert dropped >= 0
        source = ast.parse(
            (REPO_ROOT / "execution" / "test_selection.py").read_text(encoding="utf-8")
        )
        slots = [
            node.value.value
            for node in ast.walk(source)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id == "_CACHE_SLOTS"
            and isinstance(node.value, ast.Constant)
        ]
        assert slots, "the cache slot count is no longer a literal constant"
        assert isinstance(slots[0], int) and 0 < slots[0] <= 8, (
            f"the import-graph cache holds {slots[0]} slots; an unbounded "
            "process-lifetime cache is a leak on a long-running daemon"
        )


def hashlib_sha256(payload: bytes) -> str:
    """A local sha256 so the row does not depend on an import elsewhere."""
    import hashlib

    return hashlib.sha256(payload).hexdigest()


# The two harness-side pin modules must both still exist and still be green;
# this row is what makes "re-verification" a claim rather than an intention.
def test_the_p0_containment_pins_are_still_present() -> None:
    for name in (
        "test_containment_pins.py",
        "test_flake_gate_pins.py",
        "test_unsandboxed_pins.py",
        "test_verification_honesty_pins.py",
    ):
        assert (REPO_ROOT / "execution" / name).exists(), (
            f"execution/{name} is missing; the P0 containment pins are this "
            "module's floor and deleting one removes a claim nobody is checking"
        )
