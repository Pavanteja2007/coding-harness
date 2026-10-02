"""R2-03 — a repo's test CONFIGURATION is protected, and its EFFECT is measured.

Protected *files* are the easy half; the prompt's hard half is the effect. A
`conftest.py` in a directory the target test does not reach, a `pytest.ini`
above the repository root, an `addopts` entry that disables a plug-in: each
changes what a "passing" run means without touching a path anyone was
watching. So these tests assert BOTH layers:

1. the refusal (a changed test-config surface is refused by the edit gate,
   unless the run DECLARED that changing test configuration is its purpose);
2. the measurement (the effective configuration is resolved for the pristine
   and working trees and the difference is reported as `test_config_changed`
   with the resolved before/after).

No Docker, no network, no model: everything here is host-side file state, which
is where the guard lives.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness import editor
from harness import test_config as tc
from shared.types import Task

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo_pair(tmp_path):
    """A pristine/work pair shaped like a real Python repo under test."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for root in (pristine, work):
        (root / "tests").mkdir(parents=True)
        _write(root, "pkg/__init__.py", "")
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(
            root,
            "tests/test_mod.py",
            "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n",
        )
        _write(
            root,
            "pyproject.toml",
            '[project]\nname = "demo"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        )
    return pristine, work


class FakeTrace:
    """The minimal `.log(kind, data)` sink `check_edits` writes receipts to."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def log(self, kind, data=None, **_kwargs):
        self.rows.append((str(kind), dict(data or {})))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.rows]

    def first(self, kind: str) -> dict:
        for row_kind, row in self.rows:
            if row_kind == kind:
                return row
        raise AssertionError(f"no {kind} row in {self.kinds()}")


# ---------------------------------------------------------------------------
# REQUIRED 1 — tampering with conftest.py is refused by the edit gate
# ---------------------------------------------------------------------------


def test_editing_conftest_is_refused_by_the_edit_gate(repo_pair):
    pristine, work = repo_pair
    _write(work, "conftest.py", 'collect_ignore_glob = ["*broken*"]\n')
    changed = editor.changed_files(str(pristine), str(work))
    assert "conftest.py" in changed

    ok, message, changed = editor.check_edits(str(pristine), str(work), ["tests/*"])
    assert ok is False
    assert "conftest.py" in message
    assert "test configuration" in message.lower()
    # the existing protected-path vocabulary is preserved for callers that
    # assert on it
    assert "protected" in message
    assert "test_config_change" in message  # names the declared-intent remedy


def test_editing_a_nested_conftest_is_refused_too(repo_pair):
    """conftest.py is directory-scoped, so a nested one changes the run too."""
    pristine, work = repo_pair
    _write(work, "tests/conftest.py", "import pytest\n")
    ok, message, _ = editor.check_edits(str(pristine), str(work), ["tests/*"])
    assert ok is False
    assert "conftest.py" in message


def test_conftest_is_protected_even_with_an_empty_pattern_list():
    """The surface set is independent of `protected_paths` (fail-closed)."""
    assert editor.is_protected("conftest.py", []) is True
    assert editor.is_protected("pkg/conftest.py", []) is True
    assert editor.is_protected("src/pkg/conftest.py", []) is True
    assert editor.is_protected("sitecustomize.py", []) is True
    # ordinary sources are untouched
    assert editor.is_protected("pkg/mod.py", []) is False


# ---------------------------------------------------------------------------
# REQUIRED 2 — tampering with pytest.ini is likewise refused
# ---------------------------------------------------------------------------


def test_editing_pytest_ini_is_refused_by_the_edit_gate(tmp_path):
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(root, "tests/test_mod.py", "def test_f():\n    assert True\n")
        _write(root, "pytest.ini", "[pytest]\naddopts = -q\n")
    _write(work, "pytest.ini", "[pytest]\naddopts = -q -k test_f\n")

    ok, message, changed = editor.check_edits(str(pristine), str(work), [])
    assert ok is False
    assert "pytest.ini" in message
    assert "pytest.ini" in changed


def test_pytest_ini_addopts_relaxation_is_named_in_the_refusal(tmp_path):
    """The refusal says WHAT the change would have done, not just which file."""
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pytest.ini", "[pytest]\n")
    _write(work, "pytest.ini", "[pytest]\naddopts = --ignore=tests/broken.py\n")
    verdict = tc.test_config_guard(
        str(pristine), str(work), changed_paths=["pytest.ini"]
    )
    assert verdict.ok is False
    slugs = {row["slug"] for row in verdict.receipt.relaxations}
    assert "addopts_ignore" in slugs
    assert "addopts_ignore" in verdict.message or "addopts" in verdict.message


def test_adding_a_pytest_ini_over_an_existing_pyproject_table_is_refused(tmp_path):
    """A new inifile that outranks the old one changes which suite runs."""
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(
            root,
            "pyproject.toml",
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        )
    _write(work, "pytest.ini", "[pytest]\n")
    verdict = tc.test_config_guard(
        str(pristine), str(work), changed_paths=["pytest.ini"]
    )
    assert verdict.ok is False
    slugs = {row["slug"] for row in verdict.receipt.relaxations}
    assert "inifile_added" in slugs or "inifile_changed" in slugs
    assert "pytest.ini" in verdict.receipt.changed_paths


# ---------------------------------------------------------------------------
# REQUIRED 3 — a declared test-config change proceeds AND is visible
# ---------------------------------------------------------------------------


def test_declared_test_config_change_proceeds_and_is_visible_in_the_receipt(repo_pair):
    pristine, work = repo_pair
    _write(work, "conftest.py", "# the suite legitimately needs a bootstrap\n")

    report: list[dict] = []
    trace = FakeTrace()
    ok, message, changed = editor.check_edits(
        str(pristine),
        str(work),
        ["tests/*"],
        config={"test_config_change": "task: adopt the shared conftest bootstrap"},
        trace=trace,
        report=report,
    )
    assert ok is True, message
    assert "conftest.py" in changed

    assert len(report) == 1
    receipt = report[0]
    assert receipt["declared"] is True
    assert receipt["declared_reason"] == "task: adopt the shared conftest bootstrap"
    assert receipt["declared_key"] == "test_config_change"
    assert receipt["changed"] is True
    assert "conftest.py" in receipt["changed_paths"]
    assert receipt["after"]["conftests"]["conftest.py"]
    assert receipt["before"]["conftests"] == {}
    # a declared change is a RECEIPT, not a silent bypass: it is still logged
    # under the same first-class event name
    assert "test_config_changed" in trace.kinds()
    assert trace.first("test_config_changed")["declared"] is True


def test_declared_change_of_a_pytest_table_proceeds_and_names_the_relaxation(repo_pair):
    pristine, work = repo_pair
    _write(
        work,
        "pyproject.toml",
        '[project]\nname = "demo"\n\n[tool.pytest.ini_options]\naddopts = "-k mod"\n',
    )
    report: list[dict] = []
    ok, _message, _changed = editor.check_edits(
        str(pristine),
        str(work),
        [],
        config={"test_config_change": {"reason": "issue asks for -k selection"}},
        report=report,
    )
    assert ok is True
    receipt = report[0]
    assert receipt["declared"] is True
    assert receipt["declared_reason"] == "issue asks for -k selection"
    slugs = {row["slug"] for row in receipt["relaxations"]}
    assert "addopts_selector" in slugs
    assert "pyproject_pytest_modified" in slugs
    # before/after are both present: a reader can see what changed
    assert receipt["before"]["options"].get("testpaths") == ["tests"]
    assert receipt["after"]["options"].get("addopts") == "-k mod"


def test_declaration_is_key_presence_not_prose(repo_pair):
    """Issue text is not an authorization: only the config key is."""
    pristine, work = repo_pair
    _write(work, "pytest.ini", "[pytest]\n")
    for config in (
        None,
        {},
        {"test_config_change": None},
        {"test_config_change": False},
        {"test_config_change": ""},
        {"other_key": "please change the test configuration"},
    ):
        ok, _message, _changed = editor.check_edits(
            str(pristine), str(work), [], config=config
        )
        assert ok is False, f"config={config!r} must not authorize the change"


def test_a_declaration_does_not_unlock_ordinary_test_files(repo_pair):
    """A test-config declaration is not a licence to edit the tests."""
    pristine, work = repo_pair
    _write(work, "tests/test_mod.py", "def test_f():\n    assert True\n")
    ok, message, _changed = editor.check_edits(
        str(pristine),
        str(work),
        ["tests/*"],
        config={"test_config_change": "adopt a new pytest option"},
    )
    assert ok is False
    assert "protected path modified: tests/test_mod.py" in message


def test_a_declaration_clears_the_configured_glob_for_that_surface_only(repo_pair):
    """The measured trade of a declaration, pinned so it cannot widen silently.

    `tests/conftest.py` matches the configured `tests/*` glob as well as the
    test-config surface set. A declaration clears it — the same trade
    `harness.core._authorized_test_target` makes — and the receipt says so.
    """
    pristine, work = repo_pair
    _write(work, "tests/conftest.py", "# suite bootstrap\n")
    report: list[dict] = []
    ok, _message, _changed = editor.check_edits(
        str(pristine),
        str(work),
        ["tests/*"],
        config={"test_config_change": "the issue asks for a tests/conftest.py"},
        report=report,
    )
    assert ok is True
    assert report[0]["declared"] is True
    assert "tests/conftest.py" in report[0]["changed_paths"]


# ---------------------------------------------------------------------------
# REQUIRED 4 — an effective-config difference without a declaration is
# reported as `test_config_changed` (the effect layer, not the file layer)
# ---------------------------------------------------------------------------


def test_effective_config_change_outside_the_repo_is_reported(tmp_path):
    """A pytest.ini ABOVE the tree changes the run without touching a path.

    The prompt's hard case: nothing in the repository changed, so a file-level
    guard sees nothing, and the suite that runs is not the baseline's suite.
    The trees are at different depths under the same parent, so the config
    reaches the working tree and not the pristine one.
    """
    outer = tmp_path / "outer"
    _write(outer, "pytest.ini", "[pytest]\naddopts = -q\n")
    pristine = outer / "deep" / "pristine"
    work = outer / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")

    report: list[dict] = []
    trace = FakeTrace()
    ok, message, _changed = editor.check_edits(
        str(pristine), str(work), [], trace=trace, report=report
    )
    assert ok is False
    assert "test configuration changed" in message
    assert "external" in message
    assert "test_config_changed" in trace.kinds()
    assert report[0]["changed"] is True
    assert "external" in report[0]["changed_fields"]
    assert report[0]["violations"] == []  # no file was touched: effect only
    assert any(row["name"] == "pytest.ini" for row in report[0]["after"]["external"])


def test_a_nested_conftest_change_is_reported_as_an_effect_not_only_a_path(tmp_path):
    """A conftest added where the target chain does not reach it is still seen.

    ``changed_paths`` is what makes this cheap: the edit gate already knows the
    file appeared, so no repository-wide walk is needed to notice it.
    """
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(root, "tests/test_mod.py", "def test_f():\n    assert True\n")
    _write(work, "tests/conftest.py", "collect_ignore = ['broken.py']\n")

    report: list[dict] = []
    ok, _message, _changed = editor.check_edits(
        str(pristine),
        str(work),
        [],
        config={"target_test": "tests/test_mod.py"},
        report=report,
    )
    assert ok is False
    receipt = report[0]
    assert receipt["changed"] is True
    assert "tests/conftest.py" in receipt["changed_paths"]
    slugs = {row["slug"] for row in receipt["relaxations"]}
    assert "conftest_added" in slugs


def test_every_gate_call_records_a_test_config_receipt(repo_pair):
    """A clean run is distinguishable from a run that never looked."""
    trace = FakeTrace()
    report: list[dict] = []
    _write(work := repo_pair[1], "pkg/mod.py", "def f():\n    return 2\n")
    ok, _message, _changed = editor.check_edits(
        str(repo_pair[0]), str(work), [], trace=trace, report=report
    )
    assert ok is True
    assert trace.kinds() == ["test_config"]
    assert report[0]["changed"] is False
    assert report[0]["resolved"] is True
    assert report[0]["before"]["inifile"] == "pyproject.toml"


# ---------------------------------------------------------------------------
# The protected surface set: per language, and fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,kind,scope",
    [
        ("conftest.py", "conftest", tc.WHOLE_FILE),
        ("src/pkg/conftest.py", "conftest", tc.WHOLE_FILE),
        ("pytest.ini", "inifile", tc.WHOLE_FILE),
        ("tox.ini", "tox_pytest", tc.TABLE_SCOPED),
        ("setup.cfg", "setup_cfg_pytest", tc.TABLE_SCOPED),
        ("pyproject.toml", "pyproject_pytest", tc.TABLE_SCOPED),
        ("sitecustomize.py", "sitecustomize", tc.WHOLE_FILE),
        ("jest.config.js", "runner_config", tc.WHOLE_FILE),
        ("vitest.config.ts", "runner_config", tc.WHOLE_FILE),
        (".mocharc.json", "runner_config", tc.WHOLE_FILE),
        ("package.json", "package_json", tc.TABLE_SCOPED),
        (".rspec", "runner_config", tc.WHOLE_FILE),
        ("phpunit.xml", "runner_config", tc.WHOLE_FILE),
        ("xunit.runner.json", "runner_config", tc.WHOLE_FILE),
        ("dart_test.yaml", "runner_config", tc.WHOLE_FILE),
        ("app.runsettings", "runner_config", tc.WHOLE_FILE),
        ("pom.xml", "maven_test", tc.TABLE_SCOPED),
    ],
)
def test_surface_classification_is_per_language_and_by_basename(path, kind, scope):
    surface = tc.classify_test_config_path(path)
    assert surface is not None, path
    assert surface.kind == kind
    assert surface.scope == scope


@pytest.mark.parametrize(
    "path",
    [
        "pkg/mod.py",
        "tests/test_mod.py",
        "Makefile",
        ".github/workflows/ci.yml",
        "requirements.txt",
        "docs/conftest.md",
        "src/testing.py",
    ],
)
def test_ordinary_files_are_not_surfaces(path):
    assert tc.is_test_config_path(path) is False


def test_traversal_shaped_path_cannot_evade_the_surface_check():
    assert tc.is_test_config_path("sub/../../conftest.py") is True
    assert tc.is_test_config_path("sub\\..\\..\\pytest.ini") is True


def test_unknown_language_still_gets_the_fail_closed_universal_set():
    surfaces = tc.language_surfaces("brainfuck")
    assert "conftest.py" in surfaces
    assert "pytest.ini" in surfaces
    assert tc.is_test_config_path("conftest.py") is True


def test_register_language_surfaces_extends_without_touching_the_logic():
    tc.register_language_surfaces(
        "zig",
        {"build.zig": ("runner_config", ()), "build.zig.zon": ("runner_config", ())},
    )
    try:
        assert tc.is_test_config_path("build.zig") is True
        assert editor.is_protected("build.zig", []) is True
    finally:
        tc._LANGUAGE_SURFACES.pop("zig", None)


def test_extended_protected_patterns_is_additive_and_stable():
    extended = editor.extended_protected_patterns(["tests/*"])
    assert "tests/*" in extended
    assert "conftest.py" in extended
    assert extended[0] == "tests/*"  # order preserved
    assert editor.extended_protected_patterns(extended) == extended


def test_pyproject_dependency_edit_is_not_a_violation_but_the_pytest_table_is(tmp_path):
    """Precision: the dual-purpose file is protected only where it is test config."""
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(
            root,
            "pyproject.toml",
            '[project]\nname = "demo"\ndependencies = []\n\n'
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        )
    _write(
        work,
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["rich"]\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    )
    ok, message, changed = editor.check_edits(str(pristine), str(work), [])
    assert ok is True, message
    assert changed == ["pyproject.toml"]

    _write(
        work,
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["rich"]\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-k mod"\n',
    )
    ok, message, _changed = editor.check_edits(str(pristine), str(work), [])
    assert ok is False
    assert "pyproject.toml" in message
    assert "tool.pytest.ini_options" in message


# ---------------------------------------------------------------------------
# Effective-configuration resolution
# ---------------------------------------------------------------------------


def test_inifile_precedence_matches_pytests_own_order(tmp_path):
    root = tmp_path / "repo"
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = "-p no:randomly"\n',
    )
    _write(root, "tox.ini", "[pytest]\naddopts = -q\n")
    _write(root, "setup.cfg", "[tool:pytest]\naddopts = --tb=short\n")
    # pyproject's table outranks tox.ini and setup.cfg
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.inifile == "pyproject.toml"
    assert resolved.inifile_kind == "pyproject_pytest"
    assert resolved.addopts == ["-p", "no:randomly"]

    _write(root, "pytest.ini", "[pytest]\naddopts = -x\n")
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.inifile == "pytest.ini"
    assert resolved.addopts == ["-x"]


def test_a_pyproject_without_a_pytest_table_is_not_the_inifile(tmp_path):
    root = tmp_path / "repo"
    _write(root, "pyproject.toml", '[project]\nname = "demo"\nversion = "0.1"\n')
    _write(
        root, "setup.cfg", "[metadata]\nname = demo\n\n[tool:pytest]\naddopts = -q\n"
    )
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.inifile == "setup.cfg"
    assert resolved.addopts == ["-q"]


def test_tox_ini_and_setup_cfg_pytest_tables_are_read(tmp_path):
    root = tmp_path / "tox"
    _write(
        root,
        "tox.ini",
        "[tox]\nenvlist = py311\n\n[pytest]\naddopts = --ignore=broken\n",
    )
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.inifile == "tox.ini"
    assert resolved.inifile_kind == "tox_pytest"
    assert resolved.addopts == ["--ignore=broken"]


def test_empty_pytest_ini_is_still_the_inifile(tmp_path):
    root = tmp_path / "repo"
    _write(root, "pytest.ini", "")
    _write(root, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = -q\n")
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.inifile == "pytest.ini"
    assert resolved.addopts == []


def test_conftest_chain_for_the_declared_target_is_resolved_and_labelled(tmp_path):
    root = tmp_path / "repo"
    _write(root, "conftest.py", "# root\n")
    _write(root, "tests/conftest.py", "# tests\n")
    _write(root, "tests/unit/conftest.py", "# unit\n")
    resolved = tc.resolve_effective_test_config(
        str(root), target="tests/unit/test_mod.py"
    )
    assert resolved.conftest_scope == "target_chain"
    assert set(resolved.conftests) == {
        "conftest.py",
        "tests/conftest.py",
        "tests/unit/conftest.py",
    }
    # a sibling conftest the target chain does not reach is NOT claimed
    assert "tests/other/conftest.py" not in resolved.conftests


def test_without_a_target_the_resolution_says_root_only(tmp_path):
    root = tmp_path / "repo"
    _write(root, "conftest.py", "# root\n")
    _write(root, "tests/conftest.py", "# tests\n")
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.conftest_scope == "root_only"
    assert set(resolved.conftests) == {"conftest.py"}


def test_sitecustomize_is_part_of_the_effective_configuration(tmp_path):
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
    _write(work, "sitecustomize.py", "import sys\n")
    verdict = tc.test_config_guard(
        str(pristine), str(work), changed_paths=["sitecustomize.py"]
    )
    assert verdict.ok is False
    slugs = {row["slug"] for row in verdict.receipt.relaxations}
    assert "sitecustomize_added" in slugs
    assert "sitecustomize.py" in verdict.receipt.after["surfaces"]


def test_identical_trees_resolve_identically(tmp_path):
    """No false positive: the two trees are the same content by construction."""
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pytest.ini", "[pytest]\naddopts = -q\n")
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
    verdict = tc.test_config_guard(str(pristine), str(work), changed_paths=[])
    assert verdict.ok is True
    assert verdict.receipt.changed is False
    assert verdict.receipt.before["digest"] == verdict.receipt.after["digest"]


def test_digest_ignores_the_tree_path(tmp_path):
    """The pristine and working roots differ by construction."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        _write(root, "pytest.ini", "[pytest]\n")
    one = tc.resolve_effective_test_config(str(left))
    two = tc.resolve_effective_test_config(str(right))
    assert one.digest == two.digest


def test_receipt_is_json_serialisable(repo_pair):
    pristine, work = repo_pair
    _write(work, "pytest.ini", "[pytest]\n")
    report: list[dict] = []
    editor.check_edits(str(pristine), str(work), [], report=report)
    assert json.loads(json.dumps(report[0])) == report[0]
    assert report[0]["schema_version"] == tc.SCHEMA_VERSION
    assert report[0]["event"] == "test_config_changed"


# ---------------------------------------------------------------------------
# Relaxation vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,slug",
    [
        ("-k", "addopts_selector"),
        ("--deselect", "addopts_deselect"),
        ("--ignore", "addopts_ignore"),
        ("--ignore=tests/x.py", "addopts_ignore"),
        ("-m", "addopts_marker_filter"),
        ("-o", "addopts_ini_override"),
        ("--override-ini=addopts=", "addopts_ini_override"),
        ("--collect-only", "addopts_collect_only"),
        ("-x", "addopts_fail_fast"),
        ("--maxfail=1", "addopts_maxfail"),
        ("--lf", "addopts_last_failed_only"),
    ],
)
def test_every_narrowing_addopts_token_has_a_stable_slug(token, slug):
    assert slug in tc.relaxation_slugs([token])


def test_plugin_disabling_is_a_relaxation_but_a_plugin_load_is_not():
    assert tc.relaxation_slugs(["-p", "no:randomly"]) == ["addopts_plugin"]
    assert tc.relaxation_slugs(["-p", "no:cacheprovider"]) == ["addopts_plugin"]
    assert tc.relaxation_slugs(["-p", "asyncio"]) == []


def test_benign_addopts_entries_are_not_relaxations():
    assert tc.relaxation_slugs(["-q", "--tb=short", "-ra", "--strict-markers"]) == []


def test_scope_narrowing_is_reported_even_when_the_key_is_unchanged(tmp_path):
    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pytest.ini", '[pytest]\ntestpaths = ["tests", "extra"]\n')
    _write(work, "pytest.ini", '[pytest]\ntestpaths = ["tests"]\n')
    verdict = tc.test_config_guard(
        str(pristine), str(work), changed_paths=["pytest.ini"]
    )
    assert verdict.ok is False
    slugs = {row["slug"] for row in verdict.receipt.relaxations}
    assert "scope_narrowed" in slugs


# ---------------------------------------------------------------------------
# Degradation: an honest guard never claims more than it knows
# ---------------------------------------------------------------------------


def test_a_missing_tree_does_not_crash_and_does_not_claim_a_change(tmp_path):
    verdict = tc.test_config_guard(str(tmp_path / "nope"), str(tmp_path / "nope2"))
    assert verdict.ok is True
    assert verdict.receipt.resolved is False
    assert verdict.receipt.changed is False
    assert "not a directory" in (verdict.receipt.error or "")


def test_a_guard_that_raises_does_not_fail_the_edit_and_records_the_failure(
    repo_pair, monkeypatch
):
    pristine, work = repo_pair
    _write(work, "pkg/mod.py", "def f():\n    return 2\n")

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated guard failure")

    monkeypatch.setattr(tc, "test_config_guard", explode)
    trace = FakeTrace()
    ok, _message, _changed = editor.check_edits(
        str(pristine), str(work), [], trace=trace, report=[]
    )
    assert ok is True
    assert "test_config_guard_failed" in trace.kinds()
    assert "simulated guard failure" in trace.first("test_config_guard_failed")["error"]


def test_a_file_level_violation_still_refuses_when_resolution_failed(
    repo_pair, monkeypatch
):
    """The file verdict needs no resolution, so a resolution failure cannot launder it."""
    pristine, work = repo_pair
    _write(work, "conftest.py", "# tamper\n")

    real_resolve = tc.resolve_effective_test_config

    def unresolved(root, **kwargs):
        return tc.ResolvedTestConfig(root=str(root), resolved=False, error="unreadable")

    monkeypatch.setattr(tc, "resolve_effective_test_config", unresolved)
    ok, message, _changed = editor.check_edits(str(pristine), str(work), [])
    assert ok is False
    assert "conftest.py" in message
    assert real_resolve is not None


def test_malformed_toml_does_not_crash_the_resolver(tmp_path):
    root = tmp_path / "repo"
    _write(root, "pyproject.toml", "[tool.pytest.ini_options\naddopts = -\n")
    resolved = tc.resolve_effective_test_config(str(root))
    assert isinstance(resolved.digest, str)
    assert resolved.inifile in (None, "pyproject.toml")


def test_addopts_as_a_toml_array_is_understood(tmp_path):
    root = tmp_path / "repo"
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = ["-q", "-k", "mod"]\n',
    )
    resolved = tc.resolve_effective_test_config(str(root))
    assert resolved.addopts == ["-q", "-k", "mod"]
    assert "addopts_selector" in tc.relaxation_slugs(resolved.addopts)


# ---------------------------------------------------------------------------
# Backward compatibility: the editor's existing contract is untouched
# ---------------------------------------------------------------------------


def test_three_positional_arg_call_is_byte_identical_when_nothing_changes(repo_pair):
    pristine, work = repo_pair
    _write(work, "pkg/mod.py", "def f():\n    return 2\n")
    ok, message, changed = editor.check_edits(str(pristine), str(work), ["tests/*"])
    assert (ok, message) == (True, "ok")
    assert changed == ["pkg/mod.py"]


def test_a_normal_source_edit_does_not_emit_a_changed_event(repo_pair):
    """The gate must not fire vacuously on ordinary work."""
    pristine, work = repo_pair
    _write(work, "pkg/mod.py", "def f():\n    return 2\n")
    trace = FakeTrace()
    ok, _message, _changed = editor.check_edits(
        str(pristine), str(work), [], trace=trace
    )
    assert ok is True
    assert trace.kinds() == ["test_config"]
    assert "test_config_changed" not in trace.kinds()


def test_existing_protected_path_rules_still_win_for_tests(repo_pair):
    pristine, work = repo_pair
    _write(work, "tests/test_mod.py", "def test_f():\n    assert False\n")
    ok, message, _changed = editor.check_edits(str(pristine), str(work), ["tests/*"])
    assert ok is False
    assert message == "protected path modified: tests/test_mod.py"


def test_protected_path_matrix_has_no_new_over_blocking():
    """The universal surfaces must not swallow ordinary paths."""
    for path in (
        "src/lib.py",
        ".github/workflows/ci.yml",
        "gitignore_rules.py",
        "tests_helper.py",
        "docs/pytest.ini.md",
    ):
        assert editor.is_protected(path, ["tests/*"]) is False, path


# ---------------------------------------------------------------------------
# The real loop: the guard is live on the edit-policy call site `run_task`
# already uses, so this is a production-path proof, not a unit claim.
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


@pytest.fixture
def _clean_model_overrides():
    """Never leak a scripted model into another suite in the same session."""
    from harness.deps import reset_overrides

    reset_overrides()
    yield
    reset_overrides()


@requires_docker
def test_run_task_refuses_a_vacuous_green_via_a_conftest_tamper(
    tmp_path, _clean_model_overrides
):
    """The F4 hole, driven through the REAL loop, REAL Docker sandbox/verifier.

    The scripted "fix" deselects the only test file through a conftest.py the
    edit policy does not watch, so the suite the verifier sees is not the suite
    the baseline ran. `run_task` already calls `editor.check_edits` before the
    final verify, so the guard is live on that call site without any change to
    `harness/core.py` — and the task must FAIL, never mint a vacuous success.
    """
    from harness.core import run_task
    from harness.deps import set_call_model
    from tests.fake_model import ScriptedModel

    plan = [{"id": 1, "description": "make the suite pass", "checkpoint": "SUBMIT"}]
    script = [
        [
            """printf 'collect_ignore_glob = ["test_mathutil.py"]\\n' > conftest.py""",
            "SUBMIT",
        ]
    ]
    set_call_model(ScriptedModel(plan=plan, scripts={1: script * 3}))
    task = Task(
        task_id="r2-03-conftest-tamper",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() has the wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "command_timeout_s": 60,
            "verify_timeout_s": 180,
            "max_step_turns": 8,
            "max_retries": 2,
            "protected_paths": ["tests/*"],
        },
    )
    log_root = tmp_path / "logs"
    result = run_task(task, log_root=log_root)
    assert result.status == "failed"

    rows = [
        json.loads(line)
        for line in (log_root / "r2-03-conftest-tamper" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    # the refusal, not the verifier, is what ended the run
    refusals = [
        row
        for row in rows
        if str(row.get("kind") or row.get("event")) == "final_edit_validation_failed"
    ]
    assert refusals, "the edit policy must poison the attempt before the verify"
    payload = str(refusals[0].get("data", refusals[0]))
    assert "test configuration" in payload.lower()
    assert "conftest.py" in payload
    # and the fixture itself was never touched
    assert not (FIXTURES / "bug02_mean" / "conftest.py").exists()

    # HONEST LIMIT, measured here: `run_task` calls `check_edits` with three
    # positional args, so the guard's RECEIPT is not yet written to this run's
    # trace — only the refusal is. `check_edits` takes `trace=`/`report=` for
    # exactly this, and the call site is a one-line hand-off recorded in
    # harness/AGENTS.md. The next test proves the sink works against the real
    # TraceLogger, so the hand-off is one line, not an integration project.
    assert not [
        row
        for row in rows
        if str(row.get("kind") or row.get("event")) == "test_config_changed"
    ]


def test_the_receipt_reaches_a_real_trace_jsonl(tmp_path):
    """The trace hand-off is proven against the REAL TraceLogger.

    `run_task` passes no sink today; when it does, these are the rows a run's
    `trace.jsonl` will contain. Asserted here against `harness.trace.TraceLogger`
    itself so the hand-off cannot silently produce an unserializable receipt.
    """
    from harness.trace import TraceLogger

    pristine, work = tmp_path / "pristine", tmp_path / "work"
    for root in (pristine, work):
        _write(root, "pkg/mod.py", "def f():\n    return 1\n")
        _write(root, "tests/test_mod.py", "def test_f():\n    assert True\n")
        _write(root, "pytest.ini", "[pytest]\n")
    _write(work, "pytest.ini", "[pytest]\naddopts = -k test_f\n")

    trace = TraceLogger(tmp_path / "log")
    ok, _message, _changed = editor.check_edits(
        str(pristine), str(work), [], trace=trace, report=[]
    )
    assert ok is False
    rows = [
        json.loads(line)
        for line in (tmp_path / "log" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    kinds = [row["kind"] for row in rows]
    assert kinds == ["test_config", "test_config_changed"]
    assert rows[1]["data"]["changed"] is True
    assert rows[1]["data"]["violations"][0]["path"] == "pytest.ini"
