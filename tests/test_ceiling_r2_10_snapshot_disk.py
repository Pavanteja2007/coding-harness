"""R2-10 — snapshots honour .gitignore, respect a task scope, keep a disk
budget, share immutable content safely, and prune on a documented policy.

Every test here drives the real mechanism (`execution.snapshot`, the real
`git check-ignore` subprocess, the real `os.link`) against real trees on a real
filesystem, and every size claim is a MEASURED byte count rather than a
comment. There is no Docker and no model in this suite: the mechanism is
filesystem work, and pretending otherwise would only add latency.

The four tests named after the prompt's required proofs are marked
``REQUIRED`` in their docstrings:

1. ``test_a_repo_with_a_large_ignored_directory_snapshots_under_a_size_bound``
2. ``test_a_scoped_task_snapshots_filters_and_selects_tests_inside_the_scope``
3. ``test_a_run_whose_snapshot_would_exceed_the_budget_refuses_with_the_numbers``
4. ``test_retention_pruning_removes_only_what_it_claims``
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from cli import doctor
from execution import snapshot as snap
from execution import test_selection
from memory import paths as memory_paths

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GIT = (
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)


def _git(*args: str, cwd: Path) -> str:
    """Run a fixed-argv git command and return stdout, failing the test on error."""
    completed = subprocess.run(
        [*_GIT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.returncode} {completed.stderr}"
        )
    return completed.stdout


def _write(root: Path, relative: str, data: str) -> Path:
    """Write a text file with LF endings.

    `newline=""` keeps the fixture byte-exact on Windows, where text mode would
    otherwise translate every `\n` into `\r\n` and turn a byte comparison
    against a hardlinked reference into a comparison of two different files.
    """
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8", newline="")
    return target


def _write_bytes(root: Path, relative: str, size: int, seed: bytes = b"\0") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    block = (seed * ((size // len(seed)) + 1))[:size]
    target.write_bytes(block)
    return target


def _make_repo(root: Path, gitignore: str = "") -> Path:
    """Initialize a real git repository at ``root`` with optional ignore rules."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    if gitignore:
        _write(root, ".gitignore", gitignore)
    return root


def _relatives(base: Path) -> Tuple[str, ...]:
    """Every file under ``base`` as a base-relative POSIX path."""
    found: List[str] = []
    for current, _dirs, names in os.walk(base, followlinks=False):
        for name in names:
            found.append((Path(current) / name).relative_to(base).as_posix())
    return tuple(sorted(found))


def _monorepo(root: Path) -> Path:
    """A two-service monorepo with real packages, real tests, and real config."""
    _make_repo(root)
    _write(root, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    _write(root, "README.md", "monorepo\n")
    _write(root, "services/api/__init__.py", "")
    _write(root, "services/api/handlers.py", "def handle(x):\n    return x + 1\n")
    _write(root, "services/api/tests/__init__.py", "")
    _write(
        root,
        "services/api/tests/test_handlers.py",
        "from services.api.handlers import handle\n\n\n"
        "def test_handle():\n    assert handle(1) == 2\n",
    )
    _write(root, "services/web/__init__.py", "")
    _write(root, "services/web/pages.py", "def render():\n    return '<html>'\n")
    _write(root, "services/web/tests/__init__.py", "")
    _write(
        root,
        "services/web/tests/test_pages.py",
        "from services.web.pages import render\n\n\n"
        "def test_render():\n    assert render()\n",
    )
    return root


def _run_dir(root: Path, name: str, *, age_s: float = 0.0) -> Path:
    """Create a recognisable run directory, optionally back-dated by mtime."""
    task = root / name
    _write(task, "trace.jsonl", "{}\n")
    _write(task, "state.json", '{"task_id": "%s"}\n' % name)
    _write(task, "work/app.py", "x = 1\n")
    _write(task, "pristine/app.py", "x = 1\n")
    if age_s:
        stamp = os.stat(task).st_mtime - age_s
        for current, _dirs, names in os.walk(task):
            for entry in names:
                candidate = Path(current) / entry
                os.utime(candidate, (stamp, stamp))
        os.utime(task, (stamp, stamp))
    return task


# ---------------------------------------------------------------------------
# 1. REQUIRED — a large ignored directory stays out of the snapshot
# ---------------------------------------------------------------------------


class TestIgnoreHonouredInSnapshot:
    def test_a_repo_with_a_large_ignored_directory_snapshots_under_a_size_bound(
        self, tmp_path: Path
    ) -> None:
        """REQUIRED. A repo whose .gitignore declares `build/` generated must
        snapshot under a stated size bound, and the ignored bytes must be
        reported rather than silently dropped.

        The bound is STATED and then asserted: the repository's real source is
        a few kilobytes, the ignored `build/` tree is 4 MiB of real bytes on
        disk, and the whole `pristine` + `work` pair must land under 1 MiB
        each. A copier that walked `build/` would blow that bound by more than
        8x, so the assertion discriminates.
        """
        repo = _make_repo(tmp_path / "repo", gitignore="build/\n")
        _write(repo, "pyproject.toml", "[tool.pytest.ini_options]\n")
        _write(repo, "src/app.py", "VALUE = 1\n" * 40)
        for index in range(8):
            _write_bytes(repo, f"build/artifact-{index}.bin", 512 * 1024)
        ignored_bytes = sum(
            (repo / f"build/artifact-{index}.bin").stat().st_size for index in range(8)
        )
        assert ignored_bytes == 4 * 1024 * 1024, "fixture must really be 4 MiB"

        run = tmp_path / "logs" / "task-1"
        plan = snap.plan_run_snapshot(repo, run)

        # The rules came from git, not from a local pattern table.
        assert plan.ignore_source == snap.IGNORE_SOURCE_GIT, plan.ignore_source
        assert plan.ignored_bytes >= ignored_bytes, plan.to_dict()
        assert any(item.startswith("build/") for item in plan.ignored_sample), (
            plan.ignored_sample
        )

        receipt = snap.create_run_snapshot(repo, run)
        pristine = Path(receipt.pristine)
        work = Path(receipt.work)

        # 1. the bound, measured on both halves of the pair
        bound = 1024 * 1024
        assert snap.tree_bytes(pristine) < bound, snap.tree_bytes(pristine)
        assert snap.tree_bytes(work) < bound, snap.tree_bytes(work)

        # 2. the ignored tree is absent, not merely small
        assert not (pristine / "build").exists()
        assert not (work / "build").exists()
        kept = _relatives(pristine)
        assert "src/app.py" in kept
        assert "pyproject.toml" in kept
        # `.gitignore` is repository content, not generated output.
        assert ".gitignore" in kept

        # 3. the plan's upper bound is what the budget is checked against:
        #    3 kept files x (pristine + work)
        assert plan.files == 3 * 2, plan.to_dict()
        assert plan.total_bytes < bound * 2, plan.total_bytes

    def test_the_ignore_matcher_is_git_and_not_a_second_implementation(self) -> None:
        """There is no forked gitignore matcher in this module.

        The prompt required reusing the existing machinery rather than writing a
        second matcher, and the strongest available proof is structural: the
        only ignore decision this module makes is one `git check-ignore`
        subprocess, and it holds no table of gitignore patterns of its own. Any
        future literal-pattern table here would be a second matcher, and this
        test fails when one appears.
        """
        source = Path(snap.__file__).read_text(encoding="utf-8")
        assert "check-ignore" in source
        for table_name in (
            "IGNORE_PATTERNS",
            "IGNORED_DIRS",
            "GITIGNORE_PATTERNS",
            "_SKIP_DIRS",
            "_ARTIFACT_DIRS",
        ):
            assert table_name not in source, table_name
        # The only literal names this module owns are ROOT CONFIGURATION files
        # it carries into a scoped snapshot, and those are not ignore rules.
        assert snap.DEFAULT_SCOPE_CONFIG_FILES

    def test_a_gitignored_directory_is_excluded_along_with_its_whole_subtree(
        self, tmp_path: Path
    ) -> None:
        """One ignore entry prunes the subtree rather than matching every child.

        A per-file matcher would have to ask git about every descendant; the
        prefix rule means the walk reports the directory once and the copy never
        sees it.
        """
        repo = _make_repo(tmp_path / "repo", gitignore="generated/\n")
        _write(repo, "keep.py", "KEEP = 1\n")
        _write(repo, "generated/a.py", "A = 1\n")
        _write(repo, "generated/deep/b.py", "B = 1\n")
        receipt = snap.create_run_snapshot(repo, tmp_path / "logs" / "task-1")
        assert _relatives(Path(receipt.pristine)) == (".gitignore", "keep.py"), (
            _relatives(Path(receipt.pristine))
        )

    def test_a_non_git_source_says_so_instead_of_claiming_git_semantics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source git cannot answer for falls back to the EXISTING generated
        set, and the receipt says which rules actually ran.

        This is a real honesty property, not decoration. `git check-ignore`
        outside a repository exits 1 with no output -- byte-identical to
        "nothing is ignored" -- so a naive reader would report `git+generated`
        for a directory git never looked at. The module asks
        `git rev-parse --show-toplevel` first and reports the fallback instead.
        """
        plain = tmp_path / "plain"
        _write(plain, "app.py", "X = 1\n")
        _write(plain, "node_modules/dep/index.js", "module.exports = 1\n")
        _write(plain, "__pycache__/app.cpython-310.pyc", "cached")

        # This host's temp dir is inside a git work tree, so the question is
        # forced rather than assumed: make the toplevel lookup fail.
        monkeypatch.setattr(snap, "_git_toplevel", lambda root, env: None)
        assert snap.git_ignored_paths(plain, ("app.py",)) is None

        receipt = snap.create_run_snapshot(plain, tmp_path / "logs" / "task-1")
        plan = receipt.plan
        assert plan.ignore_source == snap.IGNORE_SOURCE_GENERATED, plan.to_dict()
        assert _relatives(Path(receipt.pristine)) == ("app.py",)
        assert plan.ignored_files >= 2, plan.to_dict()

    def test_a_probe_failure_degrades_to_the_generated_set(
        self, tmp_path: Path
    ) -> None:
        """A raising ignore probe is a recorded degradation, not a crash."""

        def _boom(root: Path, relatives: Any) -> Any:
            raise RuntimeError("simulated probe failure")

        rules = snap.IgnoreRules.for_tree(tmp_path, ("a.py",), probe=_boom)
        assert rules.generated_only is True
        assert rules.source == snap.IGNORE_SOURCE_GENERATED
        assert "RuntimeError" in rules.note
        assert rules.to_dict()["generated_only"] is True

    def test_a_source_that_is_a_subdirectory_of_a_repo_uses_that_repo_rules(
        self, tmp_path: Path
    ) -> None:
        """The query is rebased onto the work tree's top level.

        A monorepo package cloned inside a repository inherits that repository's
        ignore rules; feeding git paths relative to the subdirectory instead
        would silently answer the wrong question.
        """
        repo = _make_repo(tmp_path / "repo", gitignore="services/api/generated/\n")
        _write(repo, "services/api/handlers.py", "H = 1\n")
        _write(repo, "services/api/generated/blob", "junk\n")
        sub = repo / "services" / "api"
        assert snap.git_ignored_paths(sub, ("generated/blob",)) == {"generated/blob"}
        receipt = snap.create_run_snapshot(sub, tmp_path / "logs" / "task-1")
        assert _relatives(Path(receipt.pristine)) == ("handlers.py",), _relatives(
            Path(receipt.pristine)
        )


# ---------------------------------------------------------------------------
# 2. REQUIRED — a scoped task stays inside its scope, and an escape is reported
# ---------------------------------------------------------------------------


class TestTaskScope:
    def test_a_scoped_task_snapshots_filters_and_selects_tests_inside_the_scope(
        self, tmp_path: Path
    ) -> None:
        """REQUIRED. For `task_scope = services/api`, the snapshot, the path
        filter, and the test selection all stay inside the scope, and an edit
        that escapes it is reported.

        The one documented exception is asserted in both directions: the
        repository's ROOT test configuration is carried (without it a scoped
        verifier would run a different suite from the baseline, which is the
        "is this still the same suite" failure R2-03 exists to prevent), and it
        is named as carried rather than counted as scoped source.
        """
        repo = _monorepo(tmp_path / "repo")
        config = {snap.SCOPE_CONFIG_KEY: "services/api"}
        run = tmp_path / "logs" / "task-1"

        receipt = snap.create_run_snapshot(repo, run, config=config)
        pristine_files = _relatives(Path(receipt.pristine))
        work_files = _relatives(Path(receipt.work))

        # 1. nothing from the other service is in EITHER half
        assert not any(name.startswith("services/web/") for name in pristine_files), (
            pristine_files
        )
        assert not any(name.startswith("services/web/") for name in work_files)
        # 2. the scoped service is fully present
        assert "services/api/handlers.py" in pristine_files
        assert "services/api/tests/test_handlers.py" in pristine_files
        # 3. the carried root configuration is present and REPORTED as carried
        assert "pyproject.toml" in pristine_files
        assert receipt.plan.config_files == ("pyproject.toml",), (
            receipt.plan.config_files
        )
        # 4. a plain file outside the scope is not
        assert "README.md" not in pristine_files
        assert receipt.plan.out_of_scope_files >= 1

        # 5. the retrieval-facing path filter honours the same boundary
        scope = snap.TaskScope.from_config(config, repo)
        candidates = (
            "services/api/handlers.py",
            "services/web/pages.py",
            "README.md",
            "pyproject.toml",
        )
        assert scope.filter(candidates) == (
            "services/api/handlers.py",
            "pyproject.toml",
        )
        assert scope.outside(candidates) == (
            "services/web/pages.py",
            "README.md",
        )

        # 6. test selection narrows to the scope's tests
        selection = test_selection.select_tests(
            str(repo), ["services/api/handlers.py"], scope=scope
        )
        assert selection.files == ("services/api/tests/test_handlers.py",), (
            selection.files
        )
        assert "services/web/tests/test_pages.py" in selection.out_of_scope
        assert selection.scope == "services/api"

        # 7. an out-of-scope edit is reported, not folded in
        verdict = snap.scope_verdict(
            scope, ["services/api/handlers.py", "services/web/pages.py"]
        )
        assert verdict.escaped == ("services/web/pages.py",)
        assert verdict.respected is False
        assert verdict.to_dict()["escaped"] == ["services/web/pages.py"]

    def test_the_scope_is_reported_on_the_receipt_itself(self, tmp_path: Path) -> None:
        """The snapshot receipt names the scope and any escape.

        A reader of the run's artifacts should not need a second pass to learn
        either fact.
        """
        repo = _monorepo(tmp_path / "repo")
        receipt = snap.create_run_snapshot(
            repo,
            tmp_path / "logs" / "task-1",
            config={snap.SCOPE_CONFIG_KEY: "services/api"},
            changed=["services/api/handlers.py", "README.md"],
        )
        assert receipt.scope is not None
        assert receipt.scope.scope.declared is True
        assert receipt.scope.escaped == ("README.md",)
        payload = json.loads(json.dumps(receipt.to_dict()))
        assert payload["scope"]["scope"]["roots"] == ["services/api"]
        assert payload["scope"]["escaped"] == ["README.md"]

    def test_an_undeclared_scope_is_the_whole_repository(self, tmp_path: Path) -> None:
        """Absent / None / False / "" all mean "not declared".

        A missing key must leave every existing run byte-identical, so this
        pins that resolution cannot accidentally narrow a run that never asked
        to be narrowed -- and that a whole-repository scope reports NO carried
        configuration, because the exception is not in force.
        """
        repo = _monorepo(tmp_path / "repo")
        undeclared = (
            {},
            {snap.SCOPE_CONFIG_KEY: None},
            {snap.SCOPE_CONFIG_KEY: False},
            {snap.SCOPE_CONFIG_KEY: ""},
            {snap.SCOPE_CONFIG_KEY: []},
        )
        for config in undeclared:
            scope = snap.TaskScope.from_config(config, repo)
            assert scope.whole_repository is True, config
            assert scope.declared is False, config

        run = tmp_path / "logs" / "task-1"
        receipt = snap.create_run_snapshot(repo, run)
        files = _relatives(Path(receipt.pristine))
        assert "services/web/pages.py" in files
        assert "README.md" in files
        assert receipt.plan.config_files == ()
        assert receipt.plan.out_of_scope_files == 0

    def test_a_scope_that_escapes_or_is_malformed_refuses(self, tmp_path: Path) -> None:
        """An unusable scope raises instead of silently widening to everything.

        A scope that degraded to "the whole repository" would snapshot the full
        tree while the operator believed they had scoped the run to one
        package -- the exact surprise the scope exists to prevent. A
        whitespace-only value is MALFORMED, not "undeclared": one is a typo the
        operator should hear about, the other is an absent boundary.
        """
        repo = _monorepo(tmp_path / "repo")
        for value in (
            "../outside",
            "/etc",
            "C:/Windows",
            ".",
            "./.",
            "   ",
            "services/../../etc",
            "no-such-service",
            17,
            [17],
            ["services/api", "../etc"],
        ):
            with pytest.raises(snap.SnapshotScopeError):
                snap.TaskScope.from_config({snap.SCOPE_CONFIG_KEY: value}, repo)

    def test_a_scope_may_name_several_directories(self, tmp_path: Path) -> None:
        """A multi-root scope is a union, and duplicates collapse."""
        repo = _monorepo(tmp_path / "repo")
        scope = snap.TaskScope.from_config(
            {snap.SCOPE_CONFIG_KEY: ["services/api", "services/api", "services/web"]},
            repo,
        )
        assert scope.roots == ("services/api", "services/web")
        assert scope.contains("services/web/pages.py") is True
        assert scope.contains("README.md") is False

    def test_an_empty_in_scope_test_set_never_reads_as_a_passing_verification(
        self, tmp_path: Path
    ) -> None:
        """A scope with no tests yields an EMPTY selection, not a green one.

        The inner loop's documented soundness rule is that "zero selected
        tests" falls back to the full suite; this asserts the scoped variant
        reaches that same state instead of quietly selecting nothing, and that
        the non-empty case is genuinely non-empty.
        """
        repo = _monorepo(tmp_path / "repo")
        scope = snap.TaskScope.from_config(
            {snap.SCOPE_CONFIG_KEY: "services/api"}, repo
        )
        selection = test_selection.select_tests(
            str(repo), ["services/api/handlers.py"], scope=scope
        )
        assert selection.is_empty is False
        assert selection.files

        other = _monorepo(tmp_path / "repo2")
        _write(other, "services/empty/thing.py", "X = 1\n")
        empty_scope = snap.TaskScope.from_config(
            {snap.SCOPE_CONFIG_KEY: "services/empty"}, other
        )
        empty = test_selection.select_tests(
            str(other), ["services/empty/thing.py"], scope=empty_scope
        )
        assert empty.is_empty is True
        assert empty.strategy == "scope_empty"
        assert empty.total_tests == 0

    def test_a_declared_target_outside_the_scope_is_still_selected(
        self, tmp_path: Path
    ) -> None:
        """The declared target outranks the scope, and the reason is recorded.

        Silently dropping a declared target would change what the verifier is
        asked to run -- a weaker verification presented as a scoped one.
        """
        repo = _monorepo(tmp_path / "repo")
        scope = snap.TaskScope.from_config(
            {snap.SCOPE_CONFIG_KEY: "services/api"}, repo
        )
        selection = test_selection.select_tests(
            str(repo),
            ["services/api/handlers.py"],
            target_test="services/web/tests/test_pages.py::test_render",
            scope=scope,
        )
        assert "services/web/tests/test_pages.py" in selection.files
        assert "services/web/tests/test_pages.py" in selection.out_of_scope

    def test_the_scope_filter_survives_a_json_round_trip(self, tmp_path: Path) -> None:
        """A persisted selection keeps its scope receipt."""
        repo = _monorepo(tmp_path / "repo")
        scope = snap.TaskScope.from_config(
            {snap.SCOPE_CONFIG_KEY: "services/api"}, repo
        )
        selection = test_selection.select_tests(
            str(repo), ["services/api/handlers.py"], scope=scope
        )
        restored = test_selection.TestSelection.from_dict(
            json.loads(json.dumps(selection.to_dict()))
        )
        assert restored.scope == selection.scope
        assert restored.out_of_scope == selection.out_of_scope
        assert restored.files == selection.files


# ---------------------------------------------------------------------------
# 3. REQUIRED — a run over its budget refuses to start, with the numbers
# ---------------------------------------------------------------------------


class TestDiskBudget:
    def test_a_run_whose_snapshot_would_exceed_the_budget_refuses_with_the_numbers(
        self, tmp_path: Path
    ) -> None:
        """REQUIRED. A snapshot that would exceed the ceiling is refused BEFORE
        any byte is written, and the refusal shows the measurement, the ceiling,
        and the overage.

        "Before any byte is written" is the load-bearing half and is asserted by
        checking the destination does not exist afterwards, not by trusting the
        message.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n" * 10)
        for index in range(4):
            _write_bytes(repo, f"mod{index}.bin", 256 * 1024)
        run = tmp_path / "logs" / "task-1"
        ceiling_key = snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]
        config = {ceiling_key: 64 * 1024}

        with pytest.raises(snap.SnapshotBudgetExceeded) as raised:
            snap.create_run_snapshot(repo, run, config=config)
        verdict = raised.value.verdict
        assert verdict.allowed is False
        assert verdict.reason == "over_operator_budget"
        assert verdict.reason in snap.BUDGET_REASONS
        assert verdict.estimated_bytes > 64 * 1024
        assert verdict.budget_bytes == 64 * 1024
        assert verdict.overage_bytes == verdict.estimated_bytes - 64 * 1024

        # the numbers are in the message a caller would print
        message = str(raised.value)
        assert "refused" in message
        assert "64.0 KiB" in message
        assert "over_operator_budget" in message

        # nothing was created
        assert not run.exists()

    def test_the_refusal_shape_is_available_without_an_exception(
        self, tmp_path: Path
    ) -> None:
        """A caller that renders a message rather than failing gets the receipt."""
        repo = _make_repo(tmp_path / "repo")
        _write_bytes(repo, "big.bin", 128 * 1024)
        run = tmp_path / "logs" / "task-1"
        receipt = snap.create_run_snapshot(
            repo,
            run,
            config={snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]: 1024},
            allow_budget_refusal=True,
        )
        assert receipt.budget is not None
        assert receipt.budget.allowed is False
        assert receipt.written == 0
        assert receipt.shared == 0
        assert not (run / "pristine").exists()
        assert not (run / "work").exists()

    def test_a_snapshot_inside_its_budget_is_written(self, tmp_path: Path) -> None:
        """The positive control: a ceiling that fits does not refuse anything."""
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        run = tmp_path / "logs" / "task-1"
        receipt = snap.create_run_snapshot(
            repo,
            run,
            config={snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]: 4 * 1024 * 1024},
        )
        assert receipt.budget is not None
        assert receipt.budget.allowed is True
        # one kept file (app.py) x pristine + work
        assert receipt.written == 2, receipt.to_dict()
        assert (Path(receipt.pristine) / "app.py").is_file()
        assert (Path(receipt.work) / "app.py").is_file()

    def test_an_unmeasurable_volume_refuses_rather_than_assuming_room(
        self, tmp_path: Path
    ) -> None:
        """Unknown free space is a refusal, not a pass.

        The point of the reserve is to refuse rather than discover the disk is
        full halfway through a copy, so "could not measure" must not read as
        "plenty of room".
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        plan = snap.plan_run_snapshot(repo, tmp_path / "logs" / "task-1")
        verdict = snap.enforce_budget(
            plan, snap.resolve_budget({}), free_bytes=None, measured_free=False
        )
        assert verdict.allowed is False
        assert verdict.reason == "free_space_unknown"
        assert "unknown" in verdict.render()

    def test_the_reserve_refuses_before_the_filesystem_is_full(
        self, tmp_path: Path
    ) -> None:
        """The reserve is the second, independent refusal.

        A ceiling can be generous and the volume can still be nearly full; the
        two checks are separate on purpose.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        plan = snap.plan_run_snapshot(repo, tmp_path / "logs" / "task-1")
        verdict = snap.enforce_budget(
            plan,
            snap.resolve_budget({snap.SNAPSHOT_BUDGET_CONFIG_KEYS[1]: 1024}),
            free_bytes=plan.total_bytes + 512,
        )
        assert verdict.allowed is False
        assert verdict.reason == "below_reserve"

    def test_a_budget_value_that_cannot_be_used_is_reported_not_guessed(self) -> None:
        """A `bool` or a non-numeric ceiling produces a note, never a coercion.

        `int(True) == 1` byte would silently turn a misconfiguration into "one
        byte of budget", which reads as a working gate.
        """
        ceiling = snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]
        settings = snap.resolve_budget({ceiling: True})
        assert settings.max_bytes is None
        assert settings.declared is False
        assert any("bool" in note for note in settings.notes), settings.notes

        settings = snap.resolve_budget({ceiling: "many"})
        assert settings.max_bytes is None
        assert settings.notes

    def test_a_plan_that_could_not_walk_the_whole_tree_refuses(
        self, tmp_path: Path
    ) -> None:
        """A truncated plan is a REFUSAL, because its byte total is a lower bound.

        The planning walk is bounded, and a bounded walk that stopped early
        measured only part of the tree. Certifying "within budget" against a
        lower bound is optimistic in exactly the direction this mechanism exists
        to stop, so the verdict names `plan_incomplete` instead.
        """
        repo = _make_repo(tmp_path / "repo")
        for index in range(20):
            _write(repo, f"mod{index}.py", f"V = {index}\n")
        # A clock that advances past the budget on the second reading.
        ticks = iter([0.0, 0.0, 99.0, 99.0, 199.0, 199.0, 299.0, 399.0])
        plan = snap.plan_run_snapshot(
            repo,
            tmp_path / "logs" / "task-1",
            config={snap.SNAPSHOT_PLAN_BUDGET_KEY: 1.0},
            clock=lambda: next(ticks, 999.0),
        )
        assert plan.truncated is True
        assert plan.walked is True
        verdict = snap.enforce_budget(
            plan,
            snap.resolve_budget({snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]: 10**9}),
        )
        assert verdict.allowed is False
        assert verdict.reason == "plan_incomplete"
        assert verdict.reason in snap.BUDGET_REASONS

    def test_the_planning_budget_is_bounded_and_configurable(self) -> None:
        """The walk budget has a bounded default and one opt-in key."""
        assert snap.DEFAULT_PLAN_BUDGET_S == 30.0
        assert snap._plan_budget_s(None) == snap.DEFAULT_PLAN_BUDGET_S
        assert snap._plan_budget_s({}) == snap.DEFAULT_PLAN_BUDGET_S
        assert snap._plan_budget_s({snap.SNAPSHOT_PLAN_BUDGET_KEY: 5}) == 5.0
        # A value that cannot be used takes the default rather than disabling
        # the bound, which is the optimistic direction.
        for bad in (True, "soon", 0, -1.0):
            assert snap._plan_budget_s({snap.SNAPSHOT_PLAN_BUDGET_KEY: bad}) == (
                snap.DEFAULT_PLAN_BUDGET_S
            )

    def test_only_the_run_directory_is_excluded_not_a_committed_logs_tree(
        self, tmp_path: Path
    ) -> None:
        """The walk prunes the run directory, and nothing it merely resembles.

        A repository may legitimately commit a `logs/` directory of its own --
        `harness.editor.snapshot` already pins that a same-named directory which
        is real repo content still copies -- so the enclosing log root is NOT
        guessed. The run directory is excluded because this operation is about to
        write there; a caller whose log root really is inside the repository
        passes it in `exclude_roots`, which is knowledge only the caller has.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        _write(repo, "logs/keep-here.md", "a real repo file\n")
        for index in range(5):
            _write(repo, f"logs/fix-{index}/trace.jsonl", "{}\n")

        # the default: only the run directory is excluded
        plan = snap.plan_run_snapshot(repo, repo / "logs" / "task-1")
        assert "logs/keep-here.md" in plan.in_scope, plan.in_scope
        assert plan.out_of_scope_files == 0

        # the run directory's own tree is never walked
        _write(repo, "logs/task-1/work/self.py", "written = True\n")
        plan = snap.plan_run_snapshot(repo, repo / "logs" / "task-1")
        assert not any(name.startswith("logs/task-1/") for name in plan.in_scope), (
            plan.in_scope
        )

        # a caller that KNOWS its log root is an artifact can say so
        scoped_plan = snap.plan_run_snapshot(
            repo, repo / "logs" / "task-1", exclude_roots=[repo / "logs"]
        )
        assert scoped_plan.in_scope == ("app.py",), scoped_plan.in_scope
        assert scoped_plan.out_of_scope_files == 0
        assert scoped_plan.total_bytes == (repo / "app.py").stat().st_size * 2

    def test_no_snapshot_key_is_a_behaviour_changing_harness_default(self) -> None:
        """None of this round's keys is in `harness/config.py::DEFAULTS`.

        A default is merged into EVERY task and every eval arm, so adding one
        here would switch all of them in the same commit. This is the guard that
        makes the key-PRESENCE opt-in real rather than aspirational.
        """
        from harness import config as harness_config

        for key in snap.SNAPSHOT_BUDGET_CONFIG_KEYS:
            assert key not in harness_config.DEFAULTS, key
        assert snap.SCOPE_CONFIG_KEY not in harness_config.DEFAULTS

    def test_activation_is_key_presence_and_not_a_value_check(self) -> None:
        """A written key opts in; a written value decides what it means."""
        ceiling = snap.SNAPSHOT_BUDGET_CONFIG_KEYS[0]
        # None means "no ceiling declared" -- present, and read as no value.
        assert snap.resolve_budget({ceiling: None}).max_bytes is None
        # A zero ceiling is refused as unusable rather than treated as "off".
        zeroed = snap.resolve_budget({ceiling: 0})
        assert zeroed.max_bytes is None
        assert any("not positive" in note for note in zeroed.notes)
        # A real ceiling resolves.
        assert snap.resolve_budget({ceiling: 4096}).max_bytes == 4096
        # An absent key leaves the bounded internal reserve in place.
        assert snap.resolve_budget({}).reserve_bytes == snap.DEFAULT_RESERVE_BYTES


# ---------------------------------------------------------------------------
# 4. Shared immutable content — measured, with the write path separated
# ---------------------------------------------------------------------------


class TestSharedContent:
    def test_the_shared_reference_is_read_only_and_the_write_path_is_private(
        self, tmp_path: Path
    ) -> None:
        """The safety property, proven by writing IN PLACE through `work/`.

        An agent edits through a bind-mounted sandbox, where a shell redirect
        truncates in place. If `work/` were hardlinked to the shared baseline,
        that write would silently rewrite the diff baseline and every other run
        sharing the inode. So `pristine/` is the shared read path and is made
        read-only; `work/` is a private copy. The assertions are on inode
        identity and on the resulting bytes, not on a config flag.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        _write(repo, "other.py", "OTHER = 2\n")
        store = snap.SharedStore(tmp_path / "store")
        run = tmp_path / "logs" / "task-1"

        receipt = snap.create_run_snapshot(repo, run, store=store)
        pristine = Path(receipt.pristine) / "app.py"
        work = Path(receipt.work) / "app.py"

        assert receipt.shared >= 1, receipt.to_dict()
        assert receipt.work_is_private_copy is True

        # a) pristine and work are DIFFERENT inodes
        assert os.stat(pristine).st_ino != os.stat(work).st_ino
        assert os.stat(pristine).st_nlink >= 2, "pristine should be a shared link"

        # b) pristine is read-only where the platform supports the mode
        if os.name != "nt":
            assert not os.stat(pristine).st_mode & 0o222, oct(os.stat(pristine).st_mode)

        # c) an in-place write through work/ does not touch pristine
        with open(work, "r+b") as handle:
            handle.write(b"EVIL = 9\n")
        assert pristine.read_text(encoding="utf-8") == "VALUE = 1\n"
        # ...nor the stored blob
        digest = snap.content_digest(repo / "app.py")
        assert digest is not None
        assert store.path_for(digest).read_bytes() == b"VALUE = 1\n"

        # d) an in-place write through pristine/ FAILS instead of corrupting
        if os.name != "nt":
            with pytest.raises(PermissionError):
                with open(pristine, "r+b") as handle:
                    handle.write(b"EVIL = 9\n")

    def test_two_runs_of_an_unchanged_tree_share_one_copy_of_the_bytes(
        self, tmp_path: Path
    ) -> None:
        """The measured win: a second run's reference costs no new content bytes.

        The blob store holds one copy of each distinct content, so the second
        run's `pristine` is entirely hardlinks and the store does not grow. This
        is the "two runs of the same unchanged tree" case the prompt names, and
        it is asserted on measured `tree_bytes` and on real inode identity.
        """
        repo = _make_repo(tmp_path / "repo")
        for index in range(6):
            _write(repo, f"mod{index}.py", f"VALUE = {index}\n" * 20)
        store = snap.SharedStore(tmp_path / "store")

        first = snap.create_run_snapshot(
            repo, tmp_path / "logs" / "task-1", store=store
        )
        store_after_first = store.stats()
        assert first.shared == 6, first.to_dict()

        second = snap.create_run_snapshot(
            repo, tmp_path / "logs" / "task-2", store=store
        )
        store_after_second = store.stats()

        assert second.shared == 6, second.to_dict()
        assert store_after_second["blobs"] == store_after_first["blobs"] == 6
        assert store_after_second["bytes"] == store_after_first["bytes"]

        # the two references really are the same inodes
        for index in range(6):
            left = Path(first.pristine) / f"mod{index}.py"
            right = Path(second.pristine) / f"mod{index}.py"
            assert os.stat(left).st_ino == os.stat(right).st_ino

        # and editing one run's work copy leaves the other run's baseline alone
        with open(Path(second.work) / "mod0.py", "r+b") as handle:
            handle.write(b"EVIL")
        expected = "VALUE = 0\n" * 20
        assert (Path(first.pristine) / "mod0.py").read_text(
            encoding="utf-8"
        ) == expected
        assert (Path(second.pristine) / "mod0.py").read_text(
            encoding="utf-8"
        ) == expected

    def test_sharing_never_claims_a_share_it_did_not_get(self, tmp_path: Path) -> None:
        """A store that cannot link falls back to copying and says so.

        `os.link` can fail for a different volume, a filesystem without hardlink
        support, or a permission denial. Reporting `shared=True` in that case
        would make the disk accounting optimistic exactly when it must not be.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        store = snap.SharedStore(tmp_path / "store")

        def _refuse(blob: Path, destination: Path) -> Tuple[bool, str]:
            return False, "hardlink unavailable: simulated"

        store.link = _refuse  # type: ignore[method-assign]
        receipt = snap.create_run_snapshot(
            repo, tmp_path / "logs" / "task-1", store=store
        )
        assert receipt.shared == 0, receipt.to_dict()
        assert "simulated" in receipt.shared_disabled_reason
        assert (Path(receipt.pristine) / "app.py").read_text(
            encoding="utf-8"
        ) == "VALUE = 1\n"

    def test_without_a_store_the_snapshot_is_still_a_correct_pair(
        self, tmp_path: Path
    ) -> None:
        """Sharing is additive; omitting it changes no output content."""
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        plain = snap.create_run_snapshot(repo, tmp_path / "logs" / "task-1")
        assert plain.shared == 0
        assert plain.shared_disabled_reason == "sharing not requested"
        assert (Path(plain.pristine) / "app.py").read_text(encoding="utf-8") == (
            Path(plain.work) / "app.py"
        ).read_text(encoding="utf-8")

    def test_a_store_never_holds_a_truncated_blob(self, tmp_path: Path) -> None:
        """Blobs are written to a temp file and moved into place.

        A crash mid-write must not leave a short blob that a later run would
        hardlink as if it were the real content.
        """
        source = _write(tmp_path / "src", "app.py", "VALUE = 1\n")
        store = snap.SharedStore(tmp_path / "store")
        digest = snap.content_digest(source)
        assert digest is not None
        blob, present = store.put(source, digest)
        assert present is False
        assert blob.read_text(encoding="utf-8") == "VALUE = 1\n"
        again, present = store.put(source, digest)
        assert present is True
        assert again == blob
        leftovers = [
            name
            for _c, _d, names in os.walk(store.blob_root)
            for name in names
            if name.endswith(".tmp")
        ]
        assert leftovers == [], leftovers

    def test_a_snapshot_destination_inside_the_source_does_not_recurse(
        self, tmp_path: Path
    ) -> None:
        """The `neo`-in-the-repo shape still cannot recurse into its own output.

        This was a live `RecursionError` in the interactive flow: the log root
        defaults inside the target repository, so the destination is inside the
        source.
        """
        repo = _make_repo(tmp_path / "repo")
        _write(repo, "app.py", "VALUE = 1\n")
        _write(repo, "logs/README.md", "harness artifacts\n")
        run = repo / "logs" / "task-1"
        receipt = snap.create_run_snapshot(repo, run)
        assert "app.py" in _relatives(Path(receipt.pristine))
        assert not (Path(receipt.pristine) / "logs").exists()


# ---------------------------------------------------------------------------
# 5. REQUIRED — retention prunes what it claims and nothing else
# ---------------------------------------------------------------------------


class TestRetention:
    def test_retention_pruning_removes_only_what_it_claims(
        self, tmp_path: Path
    ) -> None:
        """REQUIRED. The pass removes the expired run directories it names, and
        nothing else in the log root moves.

        The sentinels are the proof: a plain directory, a session index, the
        shared store, and the newest run all survive, and the report names each
        skipped entry so "narrow" is checkable rather than asserted.
        """
        root = tmp_path / "logs"
        root.mkdir()
        old = _run_dir(root, "task-old", age_s=90 * 86400)
        recent = _run_dir(root, "task-recent", age_s=3600)
        sentinel = _write(root, "notes/keepme.txt", "user data\n")
        index = _write(root, "session-index/index.json", "{}\n")
        store_blob = _write(
            root,
            memory_paths.SNAPSHOT_STORE_DIRNAME + "/blobs/ab/" + "c" * 64,
            "blob\n",
        )

        # keep_latest=1 protects the newest run; the expired one is the candidate.
        policy = snap.RetentionSettings(
            max_age_s=7 * 86400, max_bytes=None, keep_latest=1
        )
        report = snap.prune_run_directories(root, policy)

        assert report.removed == ("task-old",), report.to_dict()
        assert not old.exists()
        assert recent.exists()
        assert sentinel.read_text(encoding="utf-8") == "user data\n"
        assert index.exists()
        assert store_blob.exists()
        assert "notes" in report.skipped
        assert "session-index" in report.skipped
        assert memory_paths.SNAPSHOT_STORE_DIRNAME in report.skipped
        assert report.bytes_removed > 0
        assert report.errors == ()
        assert report.kept == ("task-recent",), report.to_dict()

    def test_keep_latest_protects_an_expired_run(self, tmp_path: Path) -> None:
        """Age and keep-latest compose: a run is removed only if BOTH say so.

        The documented semantics are "keep the newest N, and among the rest drop
        whatever is outside the age window". A policy that ignored keep-latest
        would delete the run an operator is most likely to want; one that
        ignored the window would keep everything forever.
        """
        root = tmp_path / "logs"
        root.mkdir()
        _run_dir(root, "task-0", age_s=90 * 86400)
        _run_dir(root, "task-1", age_s=91 * 86400)
        policy = snap.RetentionSettings(max_age_s=7 * 86400, keep_latest=2)
        report = snap.prune_run_directories(root, policy)
        assert report.removed == ()
        assert set(report.kept) == {"task-0", "task-1"}

    def test_a_dry_run_reports_the_same_set_and_removes_nothing(
        self, tmp_path: Path
    ) -> None:
        """A dry run must name the same directories the real pass would remove.

        A dry run that under-reports is worse than none: it is the thing an
        operator uses to decide whether to press the button.
        """
        root = tmp_path / "logs"
        root.mkdir()
        _run_dir(root, "task-old", age_s=90 * 86400)
        _run_dir(root, "task-recent", age_s=3600)
        policy = snap.RetentionSettings(max_age_s=7 * 86400, keep_latest=1)
        preview = snap.prune_run_directories(root, policy, dry_run=True)
        assert preview.removed == ("task-old",)
        assert preview.dry_run is True
        assert (root / "task-old").exists()
        applied = snap.prune_run_directories(root, policy)
        assert applied.removed == preview.removed

    def test_keep_latest_is_never_a_candidate(self, tmp_path: Path) -> None:
        """Byte eviction protects the newest `keep_latest` runs.

        Otherwise an over-eager byte budget would delete the run the operator is
        most likely to want.
        """
        root = tmp_path / "logs"
        root.mkdir()
        for index in range(4):
            _run_dir(root, f"task-{index}", age_s=(10 - index) * 86400)
        policy = snap.RetentionSettings(max_age_s=None, max_bytes=1, keep_latest=2)
        report = snap.prune_run_directories(root, policy)
        assert set(report.removed) == {"task-0", "task-1"}, report.to_dict()
        assert (root / "task-2").exists()
        assert (root / "task-3").exists()

    def test_a_directory_that_is_not_a_run_directory_is_never_removed(
        self, tmp_path: Path
    ) -> None:
        """The run-directory test is what keeps pruning narrow.

        `logs/notes`, `logs/_code-graph` and an empty directory all look like
        directories; none is a task run, and none may be deleted by a policy
        that only claims to remove runs.
        """
        root = tmp_path / "logs"
        root.mkdir()
        for name in ("notes", "_code-graph", "empty-dir", "_conversations"):
            _write(root, f"{name}/payload.bin", "x")
        policy = snap.RetentionSettings(max_age_s=1, keep_latest=0)
        report = snap.prune_run_directories(root, policy)
        assert report.removed == ()
        assert sorted(report.skipped) == sorted(
            ["notes", "_code-graph", "empty-dir", "_conversations"]
        )
        for name in ("notes", "_code-graph", "empty-dir", "_conversations"):
            assert (root / name / "payload.bin").exists()

    def test_a_read_only_shared_tree_is_still_prunable(self, tmp_path: Path) -> None:
        """Retention can reclaim exactly the read-only entries it created.

        On Windows a read-only file cannot be deleted, so the policy would fail
        on precisely the shared entries it exists to reclaim without a
        chmod-on-error handler.
        """
        root = tmp_path / "logs"
        root.mkdir()
        task = _run_dir(root, "task-shared", age_s=90 * 86400)
        _write(task, "pristine/app.py", "VALUE = 1\n")
        os.chmod(task / "pristine" / "app.py", snap.SharedStore.READ_ONLY_MODE)
        policy = snap.RetentionSettings(max_age_s=7 * 86400, keep_latest=0)
        report = snap.prune_run_directories(root, policy)
        assert report.removed == ("task-shared",), report.to_dict()
        assert not task.exists()
        assert report.errors == ()

    def test_one_locked_run_does_not_make_the_whole_policy_unrunnable(
        self, tmp_path: Path
    ) -> None:
        """An error on one directory is reported; the others still go.

        Swallowing it would be the same as reporting success; aborting the pass
        would make the policy unrunnable in practice.
        """
        root = tmp_path / "logs"
        root.mkdir()
        _run_dir(root, "task-a", age_s=90 * 86400)
        locked = _run_dir(root, "task-b", age_s=91 * 86400)
        _run_dir(root, "task-c", age_s=92 * 86400)

        real = snap.force_rmtree

        def _refuse_one(path: Any) -> None:
            if Path(path) == locked:
                raise OSError("simulated: directory is in use")
            real(path)

        snap.force_rmtree = _refuse_one  # type: ignore[assignment]
        try:
            report = snap.prune_run_directories(
                root, snap.RetentionSettings(max_age_s=7 * 86400, keep_latest=0)
            )
        finally:
            snap.force_rmtree = real  # type: ignore[assignment]

        assert set(report.removed) == {"task-a", "task-c"}, report.to_dict()
        assert len(report.errors) == 1
        assert "task-b" in report.errors[0]
        assert locked.exists()

    def test_a_missing_log_root_is_reported_not_raised(self, tmp_path: Path) -> None:
        """A prune against a root that does not exist says so."""
        report = snap.prune_run_directories(
            tmp_path / "nope", snap.RetentionSettings(keep_latest=0)
        )
        assert report.removed == ()
        assert report.errors
        assert "not a directory" in report.errors[0]

    def test_the_shared_store_is_pruned_on_its_own_ceiling(
        self, tmp_path: Path
    ) -> None:
        """Both store triggers are independent and are named in the report."""
        store = snap.SharedStore(tmp_path / "store")
        for index in range(4):
            blob = store.blob_root / "ab"
            blob.mkdir(parents=True, exist_ok=True)
            (blob / f"{index:064d}").write_bytes(b"x" * 1024)
        oldest = store.blob_root / "ab" / f"{0:064d}"
        stamp = os.stat(oldest).st_mtime - 10 * 86400
        os.utime(oldest, (stamp, stamp))

        # the AGE window alone: one blob, with a ceiling that removes nothing
        age_only = snap.RetentionSettings(
            max_age_s=7 * 86400, keep_latest=0, store_max_bytes=1024 * 1024
        )
        report = snap.prune_shared_store(store, age_only)
        assert report.removed == (oldest.name,), report.to_dict()
        assert report.blob_bytes_removed == 1024
        assert len(list(store.iter_blobs())) == 3

        # the BYTE ceiling alone: keep only the newest 1024 bytes
        byte_only = snap.RetentionSettings(
            max_age_s=None, keep_latest=0, store_max_bytes=1024
        )
        report = snap.prune_shared_store(store, byte_only)
        assert len(report.removed) == 2, report.to_dict()
        assert len(list(store.iter_blobs())) == 1

    def test_the_retention_policy_is_the_shared_policy_shape(self) -> None:
        """One policy vocabulary, not two that read the same and disagree."""
        from shared.retention import RetentionPolicy

        settings = snap.RetentionSettings(max_age_s=10.0, max_bytes=20, keep_latest=3)
        policy = settings.to_policy()
        assert isinstance(policy, RetentionPolicy)
        assert policy.max_age_s == 10.0
        assert policy.max_bytes == 20
        assert policy.keep_latest == 3

    def test_the_documented_defaults_are_actual_defaults(self) -> None:
        """The policy an operator sees with nothing configured is the documented one."""
        settings = snap.resolve_retention({})
        assert settings.max_age_s == snap.DEFAULT_RETENTION_DAYS * 86400.0
        assert settings.keep_latest == snap.DEFAULT_RETENTION_KEEP
        assert settings.store_max_bytes == snap.DEFAULT_STORE_MAX_BYTES
        assert settings.max_bytes is None

    def test_the_policy_resolves_from_the_environment_and_the_config(self) -> None:
        """Config wins over environment, and an unusable value is a note."""
        from_env = snap.retention_settings_from_env(
            {
                "NEO_SNAPSHOT_RETENTION_DAYS": "2",
                "NEO_SNAPSHOT_RETENTION_KEEP": "3",
                "NEO_SNAPSHOT_RETENTION_BYTES": "not-a-number",
            }
        )
        assert from_env.max_age_s == 2 * 86400.0
        assert from_env.keep_latest == 3
        assert from_env.max_bytes is None
        assert from_env.notes, "an unparseable value must be reported"

        from_config = snap.resolve_retention(
            {"snapshot_retention_days": 1, "snapshot_retention_keep": 1}
        )
        assert from_config.max_age_s == 86400.0
        assert from_config.keep_latest == 1
        assert from_config.source == "config"


# ---------------------------------------------------------------------------
# 6. The doctor disk section and the memory.paths receipts
# ---------------------------------------------------------------------------


class TestOperatorVisibility:
    def test_doctor_reports_the_disk_budget_and_retention_policy(
        self, tmp_path: Path
    ) -> None:
        """The retention policy is visible in `doctor`, with real numbers."""
        root = tmp_path / "logs"
        _run_dir(root, "task-a")
        record = doctor.run_doctor(log_root=str(root))
        row = [item for item in record["checks"] if item["key"] == "snapshot_disk"]
        assert len(row) == 1, [item["key"] for item in record["checks"]]
        row = row[0]
        assert row["status"] == "ok"
        assert "retention keep_latest=" in row["evidence"]
        assert "free " in row["evidence"]
        assert row["remediation"] is None
        assert "snapshot_disk" in json.dumps(record)

    def test_doctor_reports_a_run_root_that_does_not_exist_yet(
        self, tmp_path: Path
    ) -> None:
        """An unused artifact root is onboarding state, not breakage."""
        record = doctor.run_doctor(log_root=str(tmp_path / "absent"))
        row = next(item for item in record["checks"] if item["key"] == "snapshot_disk")
        assert row["status"] == "ok"
        assert "does not exist" in row["evidence"]

    def test_doctor_fails_loudly_when_the_root_exceeds_its_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-budget root is `failed` with a remediation, not a note."""
        root = tmp_path / "logs"
        _run_dir(root, "task-a")
        _write_bytes(root, "task-a/work/big.bin", 256 * 1024)
        record = doctor.run_doctor(log_root=str(root))
        row = next(item for item in record["checks"] if item["key"] == "snapshot_disk")
        # the numbers are present whatever the verdict
        assert "budget ceiling=" in row["evidence"]

        monkeypatch.setattr(
            doctor,
            "snapshot_disk_receipt",
            lambda **kwargs: {
                "log_root": str(root),
                "log_root_exists": True,
                "total_bytes": 10**9,
                "run_directories": 1,
                "largest": [],
                "pristine_bytes": 1,
                "work_bytes": 1,
                "store_root": str(root / "store"),
                "store": {"exists": False, "blobs": 0, "bytes": 0},
                "free_bytes": 10**9,
                "total_capacity_bytes": 2 * 10**9,
                "budget": {
                    "allowed": False,
                    "reason": "over_operator_budget",
                    "estimated_bytes": 10**9,
                    "budget_bytes": 1024,
                    "free_bytes": 10**9,
                    "reserve_bytes": 512,
                    "overage_bytes": 10**9 - 1024,
                    "settings_source": "config",
                },
                "retention": {
                    "keep_latest": 1,
                    "max_age_days": 30.0,
                    "store_max_bytes": 1024,
                },
                "error": "",
            },
        )
        record = doctor.run_doctor(log_root=str(root))
        row = next(item for item in record["checks"] if item["key"] == "snapshot_disk")
        assert row["status"] == "failed"
        assert "outside its budget" in row["reason"]
        assert "execution.snapshot" in row["remediation"]
        assert row["key"] in [item["key"] for item in record["actionable_failures"]]

    def test_doctor_reports_an_unmeasured_root_as_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk section that could not be measured says so."""

        def _boom(**kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("simulated")

        monkeypatch.setattr(doctor, "snapshot_disk_receipt", _boom)
        record = doctor.run_doctor(log_root=str(tmp_path / "logs"))
        row = next(item for item in record["checks"] if item["key"] == "snapshot_disk")
        assert row["status"] == "error"
        assert "RuntimeError" in row["reason"]

    def test_doctor_keeps_the_active_log_root_scoped_to_one_call(
        self, tmp_path: Path
    ) -> None:
        """The module-level active root is cleared, so probes cannot go stale.

        `DoctorCheck.probe` takes no arguments, so the disk check reads module
        state; a leaked value would make a later, unrelated `/doctor` report
        against the wrong root.
        """
        first = tmp_path / "one"
        _run_dir(first, "task-a")
        second = tmp_path / "two"
        _run_dir(second, "task-b")
        doctor.run_doctor(log_root=str(first))
        assert doctor._ACTIVE_LOG_ROOT is None
        row = doctor.check_snapshot_disk()
        assert "log root" in row["evidence"]
        assert str(first) not in row["evidence"]

    def test_the_store_default_is_inside_the_log_root_and_overridable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content is per-repository, and the store is not itself a run dir."""
        root = tmp_path / "logs"
        assert memory_paths.snapshot_store_root(root) == (
            root / memory_paths.SNAPSHOT_STORE_DIRNAME
        )
        monkeypatch.setenv("NEO_SNAPSHOT_STORE", str(tmp_path / "elsewhere"))
        assert memory_paths.snapshot_store_root(root) == tmp_path / "elsewhere"

        store = snap.SharedStore(root / memory_paths.SNAPSHOT_STORE_DIRNAME)
        (store.blob_root / "ab").mkdir(parents=True)
        (store.blob_root / "ab" / ("a" * 64)).write_text("blob\n", encoding="utf-8")
        assert snap.is_run_directory(store.root) is False

    def test_the_disk_receipt_is_total_and_measured(self, tmp_path: Path) -> None:
        """`snapshot_disk_receipt` measures a real tree and never raises."""
        root = tmp_path / "logs"
        _run_dir(root, "task-a")
        _write_bytes(root, "task-a/work/x.bin", 4096)
        receipt = memory_paths.snapshot_disk_receipt(log_root=root)
        assert receipt["log_root_exists"] is True
        assert receipt["run_directories"] == 1
        assert receipt["total_bytes"] >= 4096
        assert receipt["work_bytes"] >= 4096
        assert receipt["pristine_bytes"] >= 0
        assert receipt["free_bytes"] > 0
        assert receipt["error"] == ""
        assert receipt["budget"]["allowed"] is True
        assert receipt["retention"]["keep_latest"] == snap.DEFAULT_RETENTION_KEEP
        assert json.loads(json.dumps(receipt)) == receipt

        missing = memory_paths.snapshot_disk_receipt(log_root=tmp_path / "absent")
        assert missing["log_root_exists"] is False
        assert missing["total_bytes"] is None

    def test_the_prune_receipt_is_total_and_honest(self, tmp_path: Path) -> None:
        """`prune_snapshot_artifacts` reports failure instead of raising."""
        root = tmp_path / "logs"
        _run_dir(root, "task-old", age_s=90 * 86400)
        config = {"snapshot_retention_days": 1, "snapshot_retention_keep": 0}
        preview = memory_paths.prune_snapshot_artifacts(
            log_root=root, dry_run=True, config=config
        )
        assert preview["ok"] is True
        assert preview["dry_run"] is True
        assert preview["removed"] == ["task-old"]
        assert (root / "task-old").exists()

        applied = memory_paths.prune_snapshot_artifacts(log_root=root, config=config)
        assert applied["ok"] is True
        assert applied["removed"] == ["task-old"]
        assert not (root / "task-old").exists()

    def test_the_operator_surface_runs_and_is_json(self, tmp_path: Path) -> None:
        """`python -m execution.snapshot` is a real command doctor can name."""
        root = tmp_path / "logs"
        _run_dir(root, "task-old", age_s=90 * 86400)
        code = snap.main(
            [
                "--log-root",
                str(root),
                "--store",
                str(tmp_path / "store"),
                "--prune",
                "--dry-run",
                "--json",
            ]
        )
        assert code == 0
        # a dry run names the candidate and removes nothing
        assert (root / "task-old").exists()

    def test_the_retention_receipt_describes_the_policy_without_a_run(self) -> None:
        """`retention_receipt` answers the policy question with no tree present."""
        receipt = memory_paths.retention_receipt(log_root=Path("no-such-root"))
        assert receipt["log_root_exists"] is False
        assert receipt["error"] == ""
        assert receipt["policy"]["keep_latest"] == snap.DEFAULT_RETENTION_KEEP
        assert receipt["store_root"].endswith(memory_paths.SNAPSHOT_STORE_DIRNAME)


# ---------------------------------------------------------------------------
# 7. Structural pins
# ---------------------------------------------------------------------------


class TestStructuralInvariants:
    def test_the_budget_ceiling_is_enforced_on_the_upper_bound(
        self, tmp_path: Path
    ) -> None:
        """Sharing can only reduce the real figure, so the plan is the ceiling.

        A budget checked against a post-sharing estimate would be a budget that
        gets looser precisely when sharing works, which is the wrong direction.
        """
        repo = _make_repo(tmp_path / "repo")
        expected = 0
        for index in range(4):
            _write(repo, f"m{index}.py", f"V = {index}\n" * 10)
            expected += (repo / f"m{index}.py").stat().st_size
        plan = snap.plan_run_snapshot(repo, tmp_path / "out")
        assert plan.total_bytes == expected * 2  # pristine + work
        assert plan.files == 8

    def test_every_public_callable_is_documented(self) -> None:
        """Type hints and docstrings on the public surface."""
        import inspect

        missing: List[str] = []
        for name in snap.__all__:
            value = getattr(snap, name)
            if inspect.isclass(value):
                if not value.__doc__:
                    missing.append(name)
                for attr, member in vars(value).items():
                    if attr.startswith("_") or not inspect.isfunction(member):
                        continue
                    if not member.__doc__:
                        missing.append(f"{name}.{attr}")
            elif inspect.isfunction(value):
                if not value.__doc__:
                    missing.append(name)
                signature = inspect.signature(value)
                if signature.return_annotation is inspect.Signature.empty:
                    missing.append(f"{name} (no return annotation)")
        assert missing == [], missing

    def test_the_retention_keys_and_scope_key_are_declared_in_one_tuple(self) -> None:
        """A consumer can discover every opt-in key without reading the module."""
        assert set(snap.SNAPSHOT_BUDGET_CONFIG_KEYS) == {
            "snapshot_budget_bytes",
            "snapshot_reserve_bytes",
            "snapshot_share_enabled",
            "snapshot_retention_days",
            "snapshot_retention_bytes",
            "snapshot_retention_keep",
        }
        assert snap.SCOPE_CONFIG_KEY == "task_scope"
        assert sorted(snap.BUDGET_REASONS) == sorted(
            [
                "within_budget",
                "over_operator_budget",
                "below_reserve",
                "over_budget_and_below_reserve",
                "free_space_unknown",
                "plan_incomplete",
            ]
        )

    def test_the_shared_store_uses_one_config_key_for_sharing(self) -> None:
        """The sharing switch is discoverable in the same tuple as the rest."""
        assert "snapshot_share_enabled" in snap.SNAPSHOT_BUDGET_CONFIG_KEYS
        source = Path(snap.__file__).read_text(encoding="utf-8")
        assert "snapshot_share_enabled" in source
