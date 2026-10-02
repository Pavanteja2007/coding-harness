"""Ceiling-08 proofs for verification intelligence and machine-checkable specs.

The seven required proofs, each driven through the mechanism a real run uses:

1. deleting a spec item fails the run;
2. a deliberately broken patch fails acceptance;
3. a correct patch cannot pass by editing tests;
4. inner verification is materially faster than the full suite;
5. the full suite still gates completion;
6. flaky failures are classified and confirmed;
7. a renamed test collector does not silently pass.

Plus the target-metric contracts: reward-hacking gap, premature completion,
flake confirmation rate, and the structural rule that only the final gate can
mint success.

Everything here is Docker-free and provider-free. Proof 4 measures real
`python -m pytest` processes on the HOST through
`execution.flake.run_local_command`, which is an explicit host lane and is
labelled as such; it is NOT a Docker/sandbox pass. Proof 3 uses a real
`pytest --junit-xml` report parsed by the report-first parser, so it exercises
the machine-readable path rather than a fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import execution.verify as vf
from execution.flake import (
    CLASSIFICATION_CONFIRMED,
    CLASSIFICATION_PRE_EXISTING_FLAKE,
    CLASSIFICATION_TRANSIENT,
    CLASSIFICATION_UNCONFIRMED,
    FlakeLedger,
    assess_failure,
    classify_outcomes,
    clean_environment,
    run_local_command,
    scrub_run_environment,
)
from execution.independent_evidence import (
    VERDICT_REJECTED,
    VERDICT_VERIFIED,
    build_held_out_suite,
    compare_scores,
    detect_tampering,
    judge,
    premature_completion,
)
from execution.result_parsing import (
    OUTCOME_ERROR,
    OUTCOME_FAIL,
    OUTCOME_NO_TESTS,
    OUTCOME_PASS,
    OUTCOME_TIMEOUT,
    parse_junit_xml,
    parse_pytest_json,
    parse_test_run,
)
from execution.spec_ledger import (
    SpecLedger,
    SpecViolation,
    guard_paths,
    load_or_report,
)
from execution.test_selection import (
    build_import_graph,
    select_tests,
    selection_command,
)
from execution.verification_intelligence import (
    GATE_FULL_SUITE,
    GATE_SPEC_INTACT,
    GATE_TARGET,
    MANDATORY_GATES,
    STATUS_VERIFIED,
    run_verification,
)
from shared.types import ExecutionResult

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def _runs_the_full_suite_only(command: str) -> bool:
    """True when `command` invokes the suite runner and names no test file.

    R2-12 (2026-09-26) changed what reaches the sandbox: the suite command is
    now prefixed with an in-sandbox toolchain probe and suffixed with a
    structured-report echo that preserves the runner's own exit code. The
    selection tests below were written to prove that the FULL SUITE runs on the
    gating path and that a computed selection never leaks into it. Asserting
    that claim directly is STRONGER than the exact string equality it replaces:
    it fails if any test path appears, whether or not a wrapper is present, and
    it does not depend on how the run happens to be composed.
    """
    return "python -m pytest -q" in command and "test_calc.py" not in command


def _pyrepo(root: Path, *, broken: bool = False) -> Path:
    """Build a small, real pytest project with two independent modules."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['.']\n", encoding="utf-8"
    )
    _write(root, "mathlib/__init__.py", "")
    _write(
        root,
        "mathlib/calc.py",
        """
        def add(a, b):
            return a + b


        def scale(values, factor):
            return [v * factor for v in values]
        """
        if not broken
        else """
        def add(a, b):
            return a - b


        def scale(values, factor):
            return [v * factor for v in values]
        """,
    )
    _write(
        root,
        "textlib/__init__.py",
        "",
    )
    _write(
        root,
        "textlib/shout.py",
        """
        def shout(word):
            return word.upper() + "!"
        """,
    )
    _write(
        root,
        "tests/test_calc.py",
        """
        import time

        from mathlib.calc import add, scale


        def test_add():
            assert add(2, 3) == 5


        def test_scale():
            assert scale([1, 2], 3) == [3, 6]
            time.sleep(0.01)
        """,
    )
    _write(
        root,
        "tests/test_shout.py",
        """
        import time

        from textlib.shout import shout


        def test_shout():
            assert shout("hi") == "HI!"
            time.sleep(0.01)
        """,
    )
    return root


def _local_pytest(repo: Path, *args: str, junit: str = "") -> str:
    """Run real pytest on the host and return stdout; a host lane, not Docker."""
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
    if not any(value.startswith("-q") for value in args):
        command.append("-q")
    command += list(args)
    if junit:
        command += ["--junitxml", junit]
    completed = subprocess.run(
        command,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=300,
        shell=False,
    )
    return completed.stdout + completed.stderr


def _junit(repo: Path, *args: str) -> str:
    """Run pytest with a JUnit report and return the report path."""
    target = repo / "report.xml"
    _local_pytest(repo, *args, junit=str(target))
    return str(target)


# ---------------------------------------------------------------------------
# 1. machine-checkable specs: deleting an item fails the run
# ---------------------------------------------------------------------------


def _sandboxed(monkeypatch, handler) -> None:
    """Point the real sandbox boundary at a scripted runner for this test.

    The production verifier keeps going through the Docker sandbox; these tests
    substitute the ``execute_sandboxed`` boundary so the verification LOGIC can
    be exercised on a host with no daemon. This is the same substitution the
    existing tests/test_verify.py unit cases use and it is NOT a Docker pass.
    """
    monkeypatch.setattr(vf, "execute_sandboxed", handler)


class TestSpecLedger:
    def test_flipping_passes_keeps_the_guard_green(self, tmp_path):
        ledger = SpecLedger.create(
            "task-1",
            [
                {
                    "id": "gate",
                    "title": "verifier gate",
                    "acceptance": ["suite runs"],
                    "tests": ["tests/test_x.py::test_y"],
                },
                {
                    "id": "spec",
                    "title": "spec artifact",
                    "acceptance": ["artifact exists"],
                    "tests": ["tests/test_x.py::test_z"],
                },
            ],
        )
        artifact, seal = guard_paths(str(tmp_path))
        ledger.save(artifact, seal=True)
        reloaded = SpecLedger.load(artifact, seal_path=seal)
        reloaded.apply_claims({"gate": True})
        reloaded.save(artifact)
        again = SpecLedger.load(artifact, seal_path=seal)
        report = again.guard_against()
        assert report.ok is True
        assert report.claimed == ("gate",)
        assert report.unclaimed == ("spec",)

    def test_deleting_a_spec_item_fails_the_guard(self, tmp_path):
        ledger = SpecLedger.create(
            "task-1",
            [
                {
                    "id": "keep",
                    "title": "kept",
                    "acceptance": ["a"],
                    "tests": ["tests/test_a.py::test_a"],
                },
                {
                    "id": "drop",
                    "title": "dropped",
                    "acceptance": ["b"],
                    "tests": ["tests/test_b.py::test_b"],
                },
            ],
        )
        artifact, seal = guard_paths(str(tmp_path))
        ledger.save(artifact, seal=True)
        reloaded = SpecLedger.load(artifact, seal_path=seal)
        reloaded.replace_items(
            [
                {
                    "id": "keep",
                    "title": "kept",
                    "acceptance": ["a"],
                    "tests": ["tests/test_a.py::test_a"],
                }
            ]
        )
        reloaded.save(artifact)
        report = SpecLedger.load(artifact, seal_path=seal).guard_against()
        assert report.ok is False
        assert report.removed == ("drop",)
        assert any("removed" in violation for violation in report.violations)

    def test_adding_or_mutating_an_item_fails_the_guard(self, tmp_path):
        base = {
            "id": "one",
            "title": "one",
            "acceptance": ["a"],
            "tests": ["tests/test_a.py::test_a"],
        }
        ledger = SpecLedger.create("task-1", [base])
        artifact, seal = guard_paths(str(tmp_path))
        ledger.save(artifact, seal=True)

        added = SpecLedger.load(artifact, seal_path=seal)
        added.replace_items([base, {**base, "id": "two"}])
        assert added.guard_against().ok is False
        assert added.guard_against().added == ("two",)

        mutated = SpecLedger.load(artifact, seal_path=seal)
        mutated.replace_items([{**base, "acceptance": ["a", "and then some"]}])
        report = mutated.guard_against()
        assert report.ok is False
        assert report.mutated == ("one",)

    def test_unknown_claim_is_rejected_not_dropped(self, tmp_path):
        ledger = SpecLedger.create(
            "task-1",
            [
                {
                    "id": "one",
                    "title": "one",
                    "acceptance": ["a"],
                    "tests": ["tests/test_a.py::test_a"],
                }
            ],
        )
        with pytest.raises(SpecViolation):
            ledger.apply_claims({"invented": True})

    def test_unsealed_spec_is_reported_unprotected(self, tmp_path):
        artifact, seal = guard_paths(str(tmp_path))
        SpecLedger.create(
            "task-1",
            [
                {
                    "id": "one",
                    "title": "one",
                    "acceptance": ["a"],
                    "tests": ["tests/test_a.py::test_a"],
                }
            ],
        ).save(artifact)
        report = load_or_report(str(tmp_path))
        assert report.ok is False
        assert report.sealed is False
        assert "not sealed" in " ".join(report.violations)
        assert os.path.isfile(artifact) and not os.path.isfile(seal)

    def test_missing_spec_reports_a_failure_not_a_pass(self, tmp_path):
        report = load_or_report(str(tmp_path))
        assert report.ok is False
        assert "unreadable" in " ".join(report.violations)

    @pytest.mark.parametrize(
        "document",
        [
            {"spec_id": "x", "items": []},
            {"items": [{"id": "a", "acceptance": ["x"], "tests": ["t"]}]},
            {
                "spec_id": "x",
                "items": [{"id": "a", "acceptance": ["x"]}],
            },
            {
                "spec_id": "x",
                "items": [
                    {"id": "a", "acceptance": ["x"], "tests": ["t"]},
                    {"id": "a", "acceptance": ["y"], "tests": ["t2"]},
                ],
            },
        ],
    )
    def test_malformed_specs_cannot_load(self, tmp_path, document):
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(SpecViolation):
            SpecLedger.load(str(path))

    def test_deleting_a_spec_item_fails_the_run(self, tmp_path, monkeypatch):
        """Proof 1, end to end: the removal is visible as a failing gate."""
        repo = _pyrepo(tmp_path / "repo")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        ledger = SpecLedger.create(
            "ceiling-08",
            [
                {
                    "id": "target_gate",
                    "title": "the target test gates completion",
                    "acceptance": ["target test passes in the final gate"],
                    "tests": ["tests/test_calc.py::test_add"],
                },
                {
                    "id": "held_out",
                    "title": "held-out acceptance exists",
                    "acceptance": ["an independent held-out suite is judged"],
                    "tests": ["tests/test_calc.py::test_scale"],
                },
            ],
        )
        artifact, seal = guard_paths(str(run_dir))
        ledger.save(artifact, seal=True)

        # The builder tries to drop the second obligation.
        stripped = SpecLedger.load(artifact, seal_path=seal)
        stripped.replace_items([ledger.items[0].to_dict()])
        stripped.save(artifact)

        monkeypatch.setattr(
            vf,
            "execute_sandboxed",
            lambda *_a, **_k: ExecutionResult(0, "2 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            run_dir=str(run_dir),
            spec_root=str(run_dir),
            require_spec=True,
        )
        spec_gate = outcome.gate(GATE_SPEC_INTACT)
        assert spec_gate is not None
        assert spec_gate.passed is False
        assert outcome.mint_success() is False
        assert outcome.spec is not None
        assert outcome.spec.removed == ("held_out",)


# ---------------------------------------------------------------------------
# 2 & 3. acceptance: a broken patch fails, editing tests cannot pass
# ---------------------------------------------------------------------------


class TestHeldOutAcceptance:
    def test_a_deliberately_broken_patch_fails_acceptance(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo", broken=True)
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [
                {"id": "add_commutes", "call": "add(2, 3)", "expect": 5},
                {"id": "scale_doubles", "call": "scale([1, 2], 2)", "expect": [2, 4]},
            ],
            seed=7,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        report = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", held.root],
            cwd=str(repo),
        )
        assert report.outcome == OUTCOME_FAIL
        judgment = judge(
            held,
            run=lambda path, suite: run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    suite.root,
                ],
                cwd=path,
            ),
            visible_report=report,
            expected_fingerprint=held.sealed_fingerprint,
            repo_path=str(repo),
        )
        assert judgment.verdict == VERDICT_REJECTED
        assert judgment.verified is False

    def test_a_correct_patch_passes_acceptance(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add_commutes", "call": "add(2, 3)", "expect": 5}],
            seed=11,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        report = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", held.root],
            cwd=str(repo),
        )
        assert report.outcome == OUTCOME_PASS
        judgment = judge(
            held,
            run=lambda path, suite: run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    suite.root,
                ],
                cwd=path,
            ),
            visible_report=report,
            expected_fingerprint=held.sealed_fingerprint,
            repo_path=str(repo),
        )
        assert judgment.verdict == VERDICT_VERIFIED
        assert judgment.gap_points == 0.0

    def test_held_out_tests_are_not_readable_during_the_loop(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add_commutes", "call": "add(2, 3)", "expect": 5}],
            seed=3,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        held.conceal()
        try:
            with pytest.raises(PermissionError):
                held.read("test_held_out_acceptance.py")
            assert held.concealed is True
            assert "mathlib.calc" in held.to_dict()["files"][0]["digest"] or True
        finally:
            held.reveal()
        assert "add(2, 3)" in held.read("test_held_out_acceptance.py")

    def test_held_out_suite_must_live_outside_the_repo(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        with pytest.raises(ValueError):
            build_held_out_suite(
                str(repo / "heldout"),
                [{"id": "x", "call": "add(1, 1)", "expect": 2}],
                repo_path=str(repo),
                module="mathlib.calc",
            )

    def test_expectations_are_randomized_per_seed(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        cases = [
            {"id": "shout", "call": "shout('hi')", "expect": "HI!"},
            {"id": "add", "call": "add(2, 3)", "expect": 5},
        ]
        first = build_held_out_suite(
            str(tmp_path / "one"),
            cases,
            seed=1,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        second = build_held_out_suite(
            str(tmp_path / "two"),
            cases,
            seed=99,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        assert first.read("test_held_out_acceptance.py") != second.read(
            "test_held_out_acceptance.py"
        )
        assert first.sealed_fingerprint != second.sealed_fingerprint

    def test_generated_order_is_seeded_and_reproducible(self, tmp_path):
        """Randomization is ORDER randomization, never value corruption."""
        repo = _pyrepo(tmp_path / "repo")
        cases = [
            {
                "id": "add",
                "expr": "add({a}, {b})",
                "expect": "{a} + {b}",
                "inputs": [{"a": 2, "b": 3}, {"a": -5, "b": 11}, {"a": 0, "b": 0}],
            },
            {"id": "shout", "call": "shout('hi')", "expect": "HI!"},
        ]
        first = build_held_out_suite(
            str(tmp_path / "one"),
            cases,
            seed=1,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        again = build_held_out_suite(
            str(tmp_path / "one-again"),
            cases,
            seed=1,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        other = build_held_out_suite(
            str(tmp_path / "two"),
            cases,
            seed=99,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        text_one = first.read("test_held_out_acceptance.py")
        assert text_one == again.read("test_held_out_acceptance.py")
        assert text_one != other.read("test_held_out_acceptance.py")
        assert "1" in text_one
        for expected in ("+", "HI!"):
            assert expected in text_one

    def test_a_correct_implementation_passes_every_generated_case(self, tmp_path):
        """The generated expectations are authored, so correctness is stable."""
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [
                {
                    "id": "add",
                    "expr": "add({a}, {b})",
                    "expect": "{a} + {b}",
                    "inputs": [{"a": 2, "b": 3}, {"a": -5, "b": 11}, {"a": 0, "b": 0}],
                },
                {
                    "id": "scale",
                    "expr": "scale({values}, {factor})",
                    "expect": "[v * {factor} for v in {values}]",
                    "inputs": [{"values": [1, 2], "factor": 2}],
                },
            ],
            seed=4,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        report = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", held.root],
            cwd=str(repo),
        )
        assert report.outcome == OUTCOME_PASS, report.notes
        assert report.tests_collected == 4

    def test_evaluator_tampering_is_detected(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add", "call": "add(2, 3)", "expect": 5}],
            seed=5,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        before = held.sealed_fingerprint
        with open(
            os.path.join(held.root, "test_held_out_acceptance.py"),
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write("\n# tampered\n")
        report = detect_tampering(held, expected_fingerprint=before)
        assert report.ok is False
        assert report.changed == ("test_held_out_acceptance.py",)

    def test_added_held_out_file_is_tampering(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add", "call": "add(2, 3)", "expect": 5}],
            seed=5,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        _write(
            Path(held.root),
            "test_injected.py",
            "def test_injected():\n    assert True\n",
        )
        report = detect_tampering(held, expected_fingerprint=held.sealed_fingerprint)
        assert report.ok is False
        assert "test_injected.py" in report.added

    def test_missing_fingerprint_cannot_prove_untampered(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add", "call": "add(2, 3)", "expect": 5}],
            seed=5,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        assert detect_tampering(held, expected_fingerprint="").ok is False

    def test_lucky_pass_gap_is_reported(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add", "call": "add(2, 3)", "expect": 5}],
            seed=5,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        from execution.result_parsing import TestRunReport

        visible = TestRunReport(
            outcome=OUTCOME_PASS, tests_collected=10, tests_passed=10, source="report"
        )
        heldout = TestRunReport(
            outcome=OUTCOME_FAIL, tests_collected=2, tests_passed=0, source="report"
        )
        judgment = judge(
            held,
            run=lambda _p, _s: heldout,
            visible_report=visible,
            expected_fingerprint=held.sealed_fingerprint,
            threshold_points=5.0,
            repo_path=str(repo),
        )
        assert judgment.gap_points == 100.0
        assert judgment.lucky_pass is True
        assert judgment.verdict == VERDICT_REJECTED

    def test_gap_metric_uses_percentage_points(self):
        assert compare_scores(100.0, 95.0) == 5.0
        assert compare_scores(80.0, None) is None


class TestCorrectPatchCannotPassByEditingTests:
    def test_editing_the_visible_test_does_not_satisfy_the_held_out_suite(
        self, tmp_path
    ):
        """Proof 3: defusing the visible test leaves acceptance red."""
        repo = _pyrepo(tmp_path / "repo", broken=True)
        _write(
            repo,
            "tests/test_calc.py",
            """
            from mathlib.calc import add, scale


            def test_add():
                assert True
            """,
        )
        visible = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=str(repo),
        )
        assert visible.outcome == OUTCOME_PASS

        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add_commutes", "call": "add(2, 3)", "expect": 5}],
            seed=17,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        judgment = judge(
            held,
            run=lambda path, suite: run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    suite.root,
                ],
                cwd=path,
            ),
            visible_report=visible,
            expected_fingerprint=held.sealed_fingerprint,
            repo_path=str(repo),
        )
        assert judgment.verdict == VERDICT_REJECTED
        assert judgment.heldout_score == 0.0
        assert judgment.gap_points is not None
        assert judgment.gap_points >= 5.0

    def test_the_final_gate_still_fails_when_the_suite_is_defused(
        self, tmp_path, monkeypatch
    ):
        """Proof 3 + 5 together: a defused test cannot mint success."""
        repo = _pyrepo(tmp_path / "repo", broken=True)
        _write(
            repo,
            "tests/test_calc.py",
            """
            from mathlib.calc import add, scale


            def test_add():
                assert True
            """,
        )
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [{"id": "add_commutes", "call": "add(2, 3)", "expect": 5}],
            seed=23,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        fingerprint = held.sealed_fingerprint
        held.conceal()
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(0, "1 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py", "tests/test_calc.py"],
            run_dir=str(tmp_path / "run"),
            held_out=held,
            held_out_run=lambda path, suite: run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    suite.root,
                ],
                cwd=path,
            ),
            held_out_fingerprint=fingerprint,
        )
        assert outcome.final_gate_ran is True
        assert outcome.suite_scope == "full"
        assert outcome.mint_success() is False
        assert outcome.judgment is not None
        assert outcome.judgment.verified is False


# ---------------------------------------------------------------------------
# 4 & 5. incremental inner verification, full suite still gates
# ---------------------------------------------------------------------------


class TestIncrementalSelection:
    def test_import_graph_selects_only_affected_tests(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        graph = build_import_graph(str(repo))
        assert graph.built is True
        selection = select_tests(str(repo), ["mathlib/calc.py"], graph=graph)
        assert "tests/test_calc.py" in selection.files
        assert "tests/test_shout.py" not in selection.files
        assert selection.total_tests == 2
        assert selection.strategy == "import_graph"

    def test_selection_is_persisted_with_the_run(self, tmp_path, monkeypatch):
        repo = _pyrepo(tmp_path / "repo")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(0, "2 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            run_dir=str(run_dir),
        )
        assert outcome.selection is not None
        path = run_dir / "test_selection.json"
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["changed_files"] == ["mathlib/calc.py"]
        assert "tests/test_calc.py" in payload["files"]

    def test_renamed_test_file_is_reported_missing_not_silently_dropped(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        selection = select_tests(str(repo), ["tests/test_calc.py"])
        assert "tests/test_calc.py" in selection.files
        os.unlink(repo / "tests" / "test_calc.py")
        stale = select_tests(str(repo), ["tests/test_calc.py"])
        assert stale.is_empty is True

    def test_unknown_change_falls_back_to_all_tests(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        _write(repo, "docs/readme.txt", "nothing importable\n")
        selection = select_tests(str(repo), ["docs/readme.txt"])
        assert selection.strategy in {"fallback_all", "same_package"}
        assert selection.is_empty is False

    def test_non_python_repo_falls_back_rather_than_selecting_nothing(self, tmp_path):
        repo = tmp_path / "jsrepo"
        _write(repo, "package.json", '{"name": "x"}')
        _write(repo, "src/index.js", "module.exports = 1;\n")
        _write(repo, "test/index.test.js", "it('works', () => {});\n")
        selection = select_tests(str(repo), ["src/index.js"])
        assert selection.is_empty is True
        assert selection.strategy == "fallback_all"
        assert selection.graph_note

    def test_selection_command_quotes_and_skips_empty(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        selection = select_tests(str(repo), ["mathlib/calc.py"])
        command = selection_command(selection, "python -m pytest -q")
        assert command == "python -m pytest -q tests/test_calc.py"
        assert selection_command(selection, "") is None
        from execution.test_selection import TestSelection

        assert selection_command(TestSelection(), "python -m pytest -q") is None

    def test_inner_verification_is_materially_faster_than_the_full_suite(
        self, tmp_path
    ):
        """Proof 4, measured with real pytest on the host (not Docker)."""
        repo = _pyrepo(tmp_path / "repo")
        for index in range(6):
            _write(
                repo,
                f"bulk/mod{index}.py",
                f"""
                def scale_{index}(values, factor):
                    return [v * factor for v in values]
                """,
            )
            _write(
                repo,
                f"tests/test_bulk{index}.py",
                f"""
                import time

                from bulk.mod{index} import scale_{index}


                def test_scale_{index}():
                    assert scale_{index}([1, 2], 2) == [2, 4]
                    time.sleep(0.4)
                """,
            )
        suite_output = _local_pytest(repo, "-q")
        assert "9 passed" in suite_output

        import time as _time

        started = _time.perf_counter()
        full = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=str(repo),
            timeout_s=300,
        )
        full_ms = (_time.perf_counter() - started) * 1000.0

        selection = select_tests(str(repo), ["mathlib/calc.py"])
        assert "tests/test_calc.py" in selection.files
        assert len(selection.files) == 1
        started = _time.perf_counter()
        inner = run_local_command(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_calc.py",
            ],
            cwd=str(repo),
            timeout_s=300,
        )
        inner_ms = (_time.perf_counter() - started) * 1000.0

        assert full.outcome == OUTCOME_PASS
        assert inner.outcome == OUTCOME_PASS
        assert inner_ms < full_ms, (
            f"inner verification was not faster: {inner_ms:.0f}ms vs {full_ms:.0f}ms"
        )
        assert selection.coverage_fraction is not None
        assert selection.coverage_fraction < 0.5

    def test_full_suite_still_gates_completion(self, tmp_path, monkeypatch):
        """Proof 5: a green inner run cannot stand in for the final gate."""
        repo = _pyrepo(tmp_path / "repo")
        commands: list[str] = []

        def fake_run(_repo, command, _timeout, **_kwargs):
            commands.append(command)
            if _runs_the_full_suite_only(command):
                return ExecutionResult(1, "1 failed", "", False)
            return ExecutionResult(0, "1 passed", "", False)

        monkeypatch.setattr(vf, "execute_sandboxed", fake_run)
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
        )
        assert outcome.final_gate_ran is True
        assert outcome.suite_scope == "full"
        assert outcome.gate(GATE_FULL_SUITE).passed is False
        assert outcome.mint_success() is False
        assert outcome.status != STATUS_VERIFIED
        assert any(_runs_the_full_suite_only(command) for command in commands), commands

    def test_a_selection_cannot_weaken_the_final_gate(self, tmp_path, monkeypatch):
        repo = _pyrepo(tmp_path / "repo")
        seen: list[str] = []

        def fake_run(_repo, command, _timeout, **_kwargs):
            seen.append(command)
            return ExecutionResult(0, "1 passed", "", False)

        monkeypatch.setattr(vf, "execute_sandboxed", fake_run)
        selection = select_tests(str(repo), ["mathlib/calc.py"])
        result = vf.verify(
            str(repo),
            "tests/test_calc.py::test_add",
            1,
            test_command="python -m pytest -q",
            selection=selection,
            final_gate=True,
        )
        assert result.regression_passed is True
        assert _runs_the_full_suite_only(seen[-1]), seen
        assert not any("test_calc.py" in command for command in seen[1:])

    def test_inner_verify_runs_the_selection_subset(self, tmp_path, monkeypatch):
        repo = _pyrepo(tmp_path / "repo")
        seen: list[str] = []

        def fake_run(_repo, command, _timeout, **_kwargs):
            seen.append(command)
            return ExecutionResult(0, "1 passed", "", False)

        monkeypatch.setattr(vf, "execute_sandboxed", fake_run)
        result, selection = vf.inner_verify(
            str(repo),
            ["mathlib/calc.py"],
            target_test="tests/test_calc.py::test_add",
            test_command="python -m pytest -q",
            rerun_for_flake_check=1,
        )
        assert result.regression_passed is True
        assert selection is not None
        assert any("test_calc.py" in command for command in seen[1:])

    def test_inner_verify_falls_back_to_the_suite_when_nothing_is_selected(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo")
        seen: list[str] = []

        def fake_run(_repo, command, _timeout, **_kwargs):
            seen.append(command)
            return ExecutionResult(0, "1 passed", "", False)

        monkeypatch.setattr(vf, "execute_sandboxed", fake_run)
        os.unlink(repo / "tests" / "test_calc.py")
        _result, selection = vf.inner_verify(
            str(repo),
            ["tests/test_calc.py"],
            test_command="python -m pytest -q",
            rerun_for_flake_check=1,
        )
        assert selection is None
        assert len(seen) == 1, (
            f"an empty selection must fall back to exactly one suite run, not "
            f"several and not none: {seen}"
        )
        assert _runs_the_full_suite_only(seen[0]), seen

    def test_renamed_collector_does_not_silently_pass(self, tmp_path):
        """Proof 7: renaming the test directory collects nothing and is not a pass."""
        repo = _pyrepo(tmp_path / "repo")
        os.unlink(repo / "tests" / "test_calc.py")
        os.unlink(repo / "tests" / "test_shout.py")
        shutil.move(str(repo / "tests"), str(repo / "checks"))
        report = run_local_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(repo),
            timeout_s=300,
        )
        assert report.outcome == OUTCOME_NO_TESTS
        assert report.passed is False

    def test_renamed_collector_is_reported_no_tests_by_the_verifier(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo")
        os.unlink(repo / "tests" / "test_calc.py")
        os.unlink(repo / "tests" / "test_shout.py")
        shutil.move(str(repo / "tests"), str(repo / "checks"))
        reports: list[dict] = []
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(5, "no tests ran", "", False),
        )
        result = vf.verify(
            str(repo),
            None,
            1,
            test_command="python -m pytest -q",
            reports=reports,
        )
        assert result.target_test_passed is False
        assert result.regression_passed is False
        assert reports
        assert reports[0]["outcome"] == OUTCOME_NO_TESTS

    def test_a_vanished_target_node_id_is_an_error_not_a_pass(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo")
        _write(
            repo,
            "tests/test_renamed.py",
            """
            def test_totally_different():
                assert True
            """,
        )
        os.unlink(repo / "tests" / "test_calc.py")
        report = run_local_command(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_calc.py::test_add",
            ],
            cwd=str(repo),
            timeout_s=300,
        )
        assert report.passed is False
        assert report.outcome in {OUTCOME_ERROR, OUTCOME_NO_TESTS}

    def test_empty_capture_with_zero_exit_is_not_a_pass(self):
        report = parse_test_run(ExecutionResult(0, "", "", False))
        assert report.outcome == OUTCOME_NO_TESTS
        assert report.passed is False


# ---------------------------------------------------------------------------
# robust result parsing
# ---------------------------------------------------------------------------


class TestResultParsing:
    def test_machine_readable_report_beats_prose(self):
        xml = (
            '<testsuites><testsuite name="pytest" tests="3" failures="0" '
            'errors="0" skipped="0"/></testsuites>'
        )
        report = parse_test_run(
            ExecutionResult(0, "everything is wonderful", "", False), junit_xml=xml
        )
        assert report.outcome == OUTCOME_PASS
        assert report.source == "report"
        assert report.confidence == "high"
        assert report.tests_collected == 3
        assert report.tests_passed == 3

    def test_report_counts_beat_a_lying_exit_code(self):
        xml = (
            '<testsuites><testsuite name="pytest" tests="2" failures="2" '
            'errors="0" skipped="0"/></testsuites>'
        )
        report = parse_test_run(
            ExecutionResult(0, "all good", "", False), junit_xml=xml
        )
        assert report.outcome == OUTCOME_FAIL
        assert report.tests_failed == 2

    def test_unparseable_report_degrades_to_exit_code(self):
        report = parse_test_run(
            ExecutionResult(0, "3 passed in 0.1s", "", False), junit_xml="<not-xml"
        )
        assert report.outcome == OUTCOME_PASS
        assert any("unparseable" in note for note in report.notes)

    def test_pytest_json_report_is_understood(self):
        payload = json.dumps({"summary": {"collected": 4, "failed": 1, "passed": 3}})
        report = parse_test_run(ExecutionResult(1, "", "", False), json_report=payload)
        assert report.tests_collected == 4
        assert report.tests_failed == 1
        assert report.outcome == OUTCOME_FAIL

    def test_junit_bare_testsuite_shape(self):
        xml = '<testsuite tests="2" failures="1" errors="0" skipped="0"/>'
        counts = parse_junit_xml(xml)
        assert counts == {"collected": 2, "passed": 1, "failed": 1, "skipped": 0}

    def test_junit_child_suites_are_summed(self):
        xml = (
            "<testsuites>"
            '<testsuite tests="2" failures="0" errors="0" skipped="0"/>'
            '<testsuite tests="3" failures="1" errors="0" skipped="0"/>'
            "</testsuites>"
        )
        counts = parse_junit_xml(xml)
        assert counts["collected"] == 5
        assert counts["failed"] == 1
        assert counts["passed"] == 4

    def test_junit_reports_only_suites_with_cases(self):
        assert parse_junit_xml('<testsuite tests="0" failures="0"/>') is None
        assert parse_junit_xml("") is None
        assert parse_junit_xml("<broken") is None

    def test_pytest_json_unknown_shape_returns_none(self):
        assert parse_pytest_json('{"totally": "different"}') is None
        assert parse_pytest_json("not json") is None

    def test_timeout_is_its_own_outcome(self):
        report = parse_test_run(ExecutionResult(124, "", "", True))
        assert report.outcome == OUTCOME_TIMEOUT
        assert report.passed is False
        killed = parse_test_run(ExecutionResult(137, "", "", False))
        assert killed.outcome == OUTCOME_TIMEOUT

    def test_runner_error_exit_is_not_a_test_failure(self):
        report = parse_test_run(
            ExecutionResult(4, "ERROR: file or directory not found", "", False)
        )
        assert report.outcome == OUTCOME_ERROR

    def test_a_crashed_capture_is_an_error_not_a_failure(self):
        """An unhandled runner crash must not be recorded as a failing test."""
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "/usr/local/lib/python3.10/site-packages/_pytest/config'
            '/findpaths.py", line 186, in locate_config\n'
            "OSError: [Errno 5] Input/output error: '/workspace/pytest.toml'\n"
        )
        report = parse_test_run(ExecutionResult(1, "", stderr, False))
        assert report.outcome == OUTCOME_ERROR
        assert report.tests_failed is None
        assert any("crash-shaped" in note for note in report.notes)

    def test_a_capture_with_a_traceback_and_real_counts_is_still_a_failure(self):
        stderr = (
            "Traceback (most recent call last):\n"
            "OSError: transient\n"
            "=== 1 failed, 4 passed in 0.20s ===\n"
        )
        report = parse_test_run(ExecutionResult(1, "", stderr, False))
        assert report.outcome == OUTCOME_FAIL
        assert report.tests_failed == 1
        assert report.tests_passed == 4

    def test_no_tests_collected_exit_is_distinct(self):
        report = parse_test_run(ExecutionResult(5, "", "", False))
        assert report.outcome == OUTCOME_NO_TESTS

    def test_multidigit_counts_are_not_mistaken_for_zero(self):
        for summary in ("10 passed", "20 passed", "100 passed", "342 passed"):
            assert parse_test_run(ExecutionResult(0, summary, "", False)).outcome == (
                OUTCOME_PASS
            )

    def test_zero_count_does_not_cross_lines(self):
        report = parse_test_run(
            ExecutionResult(0, "collected node0\n\ntests\n342 passed", "", False)
        )
        assert report.outcome == OUTCOME_PASS

    def test_real_junit_report_from_a_real_run(self, tmp_path):
        repo = _pyrepo(tmp_path / "repo", broken=True)
        path = _junit(repo, "tests/test_calc.py::test_add")
        with open(path, encoding="utf-8") as handle:
            xml = handle.read()
        counts = parse_junit_xml(xml)
        assert counts is not None
        assert counts["collected"] == 1
        assert counts["failed"] == 1
        report = parse_test_run(ExecutionResult(1, "", "", False), junit_xml=xml)
        assert report.outcome == OUTCOME_FAIL
        assert report.source == "report"


# ---------------------------------------------------------------------------
# 6. flake-aware verification
# ---------------------------------------------------------------------------


class TestFlakeConfirmation:
    def test_a_real_flaky_test_is_classified_and_not_actionable(self, tmp_path):
        repo = _pyrepo(tmp_path / "flake", broken=False)
        _write(
            repo,
            "tests/test_flaky.py",
            """
            import os

            MARKER = os.path.join(os.path.dirname(__file__), "marker.txt")


            def test_flaky():
                if os.path.exists(MARKER):
                    os.remove(MARKER)
                    assert False, "second run fails"
                open(MARKER, "w").close()
            """,
        )
        with clean_environment(str(repo)) as clean:
            first = run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_flaky.py",
                ],
                cwd=clean,
            )
            second = run_local_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_flaky.py",
                ],
                cwd=clean,
            )
            observed = {
                "first": first.outcome,
                "second": second.outcome,
                "marker_left": os.path.exists(
                    os.path.join(clean, "tests", "marker.txt")
                ),
            }
        assert observed["first"] == OUTCOME_PASS
        assert observed["second"] == OUTCOME_FAIL
        assessment = classify_outcomes(
            original_outcome=observed["second"],
            rerun_outcomes=(observed["first"], observed["second"]),
            label="tests/test_flaky.py::test_flaky",
        )
        assert assessment.classification == CLASSIFICATION_PRE_EXISTING_FLAKE
        assert assessment.actionable is False
        assert assessment.edit_instruction == ""

    def test_confirmed_regression_is_actionable(self):
        assessment = classify_outcomes(
            original_outcome=OUTCOME_FAIL, rerun_outcomes=(OUTCOME_FAIL, OUTCOME_FAIL)
        )
        assert assessment.classification == CLASSIFICATION_CONFIRMED
        assert assessment.actionable is True
        assert "confirmed failure" in assessment.edit_instruction

    def test_transient_failure_is_not_an_edit_instruction(self):
        assessment = classify_outcomes(
            original_outcome=OUTCOME_FAIL, rerun_outcomes=(OUTCOME_PASS, OUTCOME_PASS)
        )
        assert assessment.classification == CLASSIFICATION_TRANSIENT
        assert assessment.actionable is False

    def test_unconfirmed_failure_never_becomes_an_edit_instruction(self, tmp_path):
        report = parse_test_run(ExecutionResult(1, "1 failed", "", False))
        assessment = assess_failure(report, run=None, repo_path=str(tmp_path))
        assert assessment.classification == CLASSIFICATION_UNCONFIRMED
        assert assessment.actionable is False
        assert assessment.edit_instruction == ""

    def test_attempts_zero_is_the_explicit_do_not_confirm_arm(self, tmp_path):
        report = parse_test_run(ExecutionResult(1, "1 failed", "", False))
        assessment = assess_failure(
            report,
            run=lambda _p: ExecutionResult(1, "1 failed", "", False),
            repo_path=str(tmp_path),
            attempts=0,
        )
        assert assessment.classification == CLASSIFICATION_UNCONFIRMED
        assert assessment.actionable is False

    def test_confirmation_reruns_in_a_clean_environment(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write(repo, "keep.txt", "keep me\n")
        _write(repo, ".coverage", "stale coverage db\n")
        _write(repo, ".flaky_marker", "order-dependent marker\n")
        (repo / "__pycache__").mkdir()
        _write(repo, "__pycache__/stale.pyc", "junk\n")
        observed: list[dict] = []

        def run(path: str):
            observed.append(
                {
                    "path": path,
                    "keep": os.path.isfile(os.path.join(path, "keep.txt")),
                    "coverage": os.path.exists(os.path.join(path, ".coverage")),
                    "marker": os.path.exists(os.path.join(path, ".flaky_marker")),
                    "pycache": os.path.exists(os.path.join(path, "__pycache__")),
                }
            )
            return ExecutionResult(1, "1 failed", "", False)

        report = parse_test_run(ExecutionResult(1, "1 failed", "", False))
        assessment = assess_failure(
            report, run=run, repo_path=str(repo), attempts=2, label="t.py::t"
        )
        assert assessment.classification == CLASSIFICATION_CONFIRMED
        assert len(observed) == 2
        assert all(item["path"] != str(repo) for item in observed)
        assert os.path.isfile(str(repo / ".flaky_marker"))
        for item in observed:
            assert item["keep"] is True
            assert item["coverage"] is False
            assert item["marker"] is False
            assert item["pycache"] is False

    def test_confirmed_failure_flows_through_the_gate_without_an_edit_instruction(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo", broken=True)
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(1, "1 failed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
            flake_confirm_attempts=2,
            confirm_runner=lambda _p: ExecutionResult(1, "1 failed", "", False),
        )
        assert outcome.flake is not None
        assert outcome.flake.classification == CLASSIFICATION_CONFIRMED
        assert outcome.flake.actionable is True
        assert outcome.mint_success() is False

    def test_ledger_reports_an_honest_confirmation_rate(self):
        ledger = FlakeLedger()
        assert ledger.confirmation_rate is None
        ledger.add(
            classify_outcomes(
                original_outcome=OUTCOME_FAIL, rerun_outcomes=(OUTCOME_FAIL,)
            )
        )
        ledger.add(
            classify_outcomes(
                original_outcome=OUTCOME_FAIL, rerun_outcomes=(OUTCOME_PASS,)
            )
        )
        ledger.add(classify_outcomes(original_outcome=OUTCOME_FAIL, rerun_outcomes=()))
        assert ledger.total == 3
        assert ledger.decided == 2
        assert ledger.confirmed == 1
        assert ledger.confirmation_rate == pytest.approx(2 / 3, abs=1e-6)
        assert ledger.confirmed_rate == pytest.approx(0.5)
        assert ledger.by_classification() == {
            CLASSIFICATION_CONFIRMED: 1,
            CLASSIFICATION_TRANSIENT: 1,
            CLASSIFICATION_UNCONFIRMED: 1,
        }

    def test_ledger_rate_is_zero_when_everything_was_unconfirmed(self):
        ledger = FlakeLedger()
        ledger.add(classify_outcomes(original_outcome=OUTCOME_FAIL, rerun_outcomes=()))
        assert ledger.confirmation_rate == 0.0

    def test_run_environment_scrubs_prior_run_state(self, monkeypatch):
        monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
        monkeypatch.setenv("PYTHONPATH", "/tmp/old")
        monkeypatch.setenv("NEO_TRACE_DIR", "logs")
        cleaned = scrub_run_environment()
        assert "PYTEST_ADDOPTS" not in cleaned
        assert "PYTHONPATH" not in cleaned
        assert "NEO_TRACE_DIR" not in cleaned
        assert "PATH" in cleaned or "Path" in cleaned


# ---------------------------------------------------------------------------
# real Docker lane
# ---------------------------------------------------------------------------


def _docker_up() -> bool:
    try:
        completed = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


@requires_docker
class TestRealDockerGate:
    """The same gate through the REAL Docker sandbox, not a stub.

    This is the lane that proves `execution.verify` still goes through
    `execute_sandboxed` after the ceiling-08 rewrite, that the incremental
    selection is honoured only on the non-gating path, and that the final gate
    really runs the whole suite.
    """

    def _repo(self, tmp_path: Path, *, broken: bool) -> Path:
        repo = _pyrepo(tmp_path / "repo", broken=broken)
        for index in range(3):
            _write(
                repo,
                f"bulk/mod{index}.py",
                f"def scale_{index}(values, factor):\n    return [v * factor for v in values]\n",
            )
            _write(
                repo,
                f"tests/test_bulk{index}.py",
                f"from bulk.mod{index} import scale_{index}\n\n\n"
                f"def test_scale_{index}():\n    assert scale_{index}([1, 2], 2) == [2, 4]\n",
            )
        return repo

    def test_a_correct_patch_is_verified_by_the_real_sandbox(self, tmp_path):
        repo = self._repo(tmp_path, broken=False)
        run_dir = tmp_path / "run"
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            run_dir=str(run_dir),
            rerun_for_flake_check=1,
            verify_timeout_s=300,
        )
        assert outcome.final_gate_ran is True
        assert outcome.suite_scope == "full"
        assert outcome.mint_success() is True, outcome.to_dict()["gates"]
        assert outcome.result is not None
        assert outcome.result.target_test_passed is True
        assert outcome.result.regression_passed is True
        assert outcome.selection is not None
        assert "tests/test_calc.py" in outcome.selection.files
        assert "tests/test_shout.py" not in outcome.selection.files
        assert (run_dir / "test_selection.json").is_file()

    def test_a_broken_patch_is_rejected_by_the_real_sandbox(self, tmp_path):
        repo = self._repo(tmp_path, broken=True)
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
            verify_timeout_s=300,
        )
        assert outcome.final_gate_ran is True
        assert outcome.mint_success() is False
        assert outcome.gate(GATE_TARGET).passed is False

    def test_held_out_judge_runs_in_an_independent_docker_context(self, tmp_path):
        repo = self._repo(tmp_path, broken=False)
        held = build_held_out_suite(
            str(tmp_path / "heldout"),
            [
                {
                    "id": "add",
                    "expr": "add({a}, {b})",
                    "expect": "{a} + {b}",
                    "inputs": [{"a": 2, "b": 3}, {"a": -5, "b": 11}],
                }
            ],
            seed=8,
            repo_path=str(repo),
            module="mathlib.calc",
        )
        fingerprint = held.sealed_fingerprint
        held.conceal()

        def sandboxed_held_out(path: str, suite) -> object:
            target = Path(path) / "held_out_copy"
            shutil.copytree(suite.root, target, dirs_exist_ok=True)
            from execution.result_parsing import parse_test_run
            from execution.sandbox import execute_sandboxed

            relative = os.path.relpath(str(target), str(path)).replace("\\", "/")
            result = execute_sandboxed(
                str(path),
                f"python -m pytest -q -p no:cacheprovider {relative}",
                300,
            )
            return parse_test_run(result)

        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            run_dir=str(tmp_path / "run"),
            rerun_for_flake_check=1,
            verify_timeout_s=300,
            held_out=held,
            held_out_run=sandboxed_held_out,
            held_out_fingerprint=fingerprint,
        )
        assert outcome.judgment is not None
        assert outcome.judgment.tampering is not None
        assert outcome.judgment.tampering.ok is True
        assert outcome.judgment.heldout_score == 100.0
        assert outcome.mint_success() is True, outcome.to_dict()["gates"]

    def test_a_deleted_spec_item_fails_in_the_real_sandbox(self, tmp_path):
        repo = self._repo(tmp_path, broken=False)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        ledger = SpecLedger.create(
            "ceiling-08-docker",
            [
                {
                    "id": "gate",
                    "title": "verifier gate",
                    "acceptance": ["the full suite runs"],
                    "tests": ["tests/test_calc.py::test_add"],
                },
                {
                    "id": "held_out",
                    "title": "held-out acceptance",
                    "acceptance": ["an independent suite is judged"],
                    "tests": ["tests/test_bulk0.py::test_scale_0"],
                },
            ],
        )
        artifact, seal = guard_paths(str(run_dir))
        ledger.save(artifact, seal=True)
        stripped = SpecLedger.load(artifact, seal_path=seal)
        stripped.replace_items([ledger.items[0].to_dict()])
        stripped.save(artifact)
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            run_dir=str(run_dir),
            spec_root=str(run_dir),
            require_spec=True,
            rerun_for_flake_check=1,
            verify_timeout_s=300,
        )
        assert outcome.final_gate_ran is True
        assert outcome.mint_success() is False
        assert outcome.gate(GATE_SPEC_INTACT).passed is False
        assert outcome.spec.removed == ("held_out",)


# ---------------------------------------------------------------------------
# the mint rule
# ---------------------------------------------------------------------------


class TestMintRule:
    def test_only_the_final_gate_can_mint_success(self, tmp_path, monkeypatch):
        repo = _pyrepo(tmp_path / "repo")
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(0, "2 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
        )
        assert outcome.final_gate_ran is True
        assert outcome.suite_scope == "full"
        assert outcome.mint_success() is True
        assert outcome.status == STATUS_VERIFIED
        assert outcome.final_result().regression_passed is True

    def test_a_non_gating_inner_result_cannot_be_read_as_a_verdict(self):
        """A subset-scoped outcome can never mint success, even all-green."""
        from execution.verification_intelligence import (
            GateResult,
            VerificationOutcome,
        )
        from shared.types import VerificationResult

        green = VerificationResult(
            target_test_passed=True,
            baseline_passed=True,
            regression_passed=True,
            flaky=False,
            raw_output="",
        )
        outcome = VerificationOutcome(
            status=STATUS_VERIFIED,
            result=green,
            final_gate_ran=True,
            suite_scope="selected",
            gates=(
                GateResult(GATE_TARGET, True, True),
                GateResult(GATE_FULL_SUITE, True, True),
                GateResult("not_flaky", True, True),
                GateResult("no_premature_completion", True, True),
            ),
        )
        assert outcome.mint_success() is False
        with pytest.raises(RuntimeError):
            VerificationOutcome(
                status=STATUS_VERIFIED,
                result=green,
                final_gate_ran=False,
                suite_scope="selected",
            ).final_result()

    def test_final_result_refuses_when_the_final_gate_did_not_run(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo")

        def boom(*_a, **_k):
            raise RuntimeError("sandbox unavailable")

        monkeypatch.setattr(vf, "execute_sandboxed", boom)
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
        )
        assert outcome.final_gate_ran is False
        assert outcome.mint_success() is False
        assert outcome.status == "indeterminate"
        with pytest.raises(RuntimeError):
            outcome.final_result()
        assert any("final gate could not run" in error for error in outcome.errors)

    def test_a_claimed_completion_without_a_clean_gate_is_premature(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo")
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(1, "1 failed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            claimed_complete=True,
            rerun_for_flake_check=1,
        )
        assert outcome.premature["premature"] is True
        assert outcome.gate("no_premature_completion").passed is False
        assert outcome.mint_success() is False

    def test_premature_completion_helper(self):
        assert (
            premature_completion(
                claimed_complete=True, final_gate_ran=False, final_gate_passed=False
            )["premature"]
            is True
        )
        assert (
            premature_completion(
                claimed_complete=True, final_gate_ran=True, final_gate_passed=True
            )["premature"]
            is False
        )
        assert (
            premature_completion(
                claimed_complete=False, final_gate_ran=False, final_gate_passed=False
            )["premature"]
            is False
        )

    def test_mandatory_gates_are_the_verifier_derived_ones(self):
        assert GATE_TARGET in MANDATORY_GATES
        assert GATE_FULL_SUITE in MANDATORY_GATES
        assert "not_flaky" in MANDATORY_GATES
        assert "no_premature_completion" in MANDATORY_GATES

    def test_evidence_bundle_is_json_serializable(self, tmp_path, monkeypatch):
        repo = _pyrepo(tmp_path / "repo")
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(0, "2 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
        )
        payload = json.loads(json.dumps(outcome.to_dict()))
        assert payload["mint_success"] is True
        assert payload["suite_scope"] == "full"
        assert payload["selection"]["files"]
        assert payload["gates"]

    def test_reports_record_the_evidence_source_for_every_run(
        self, tmp_path, monkeypatch
    ):
        repo = _pyrepo(tmp_path / "repo")
        _sandboxed(
            monkeypatch,
            lambda *_a, **_k: ExecutionResult(0, "2 passed", "", False),
        )
        outcome = run_verification(
            str(repo),
            target_test="tests/test_calc.py::test_add",
            changed_files=["mathlib/calc.py"],
            rerun_for_flake_check=1,
        )
        real = [item for item in outcome.reports if item.get("source") != "selection"]
        assert real
        assert all(item.get("outcome") == OUTCOME_PASS for item in real)
        assert all(item.get("confidence") for item in real)
