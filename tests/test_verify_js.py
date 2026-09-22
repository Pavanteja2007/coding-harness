"""Tests for multi-language (JS/TS) support — verify autodetect/target
composition, sandbox image selection, language detection, and the
Docker-gated JS/TS verify integration (vitest + jest scenarios).

Unit tests (no Docker): _detect_language, _js_test_command,
_split_js_target, _target_command composition, sandbox._detect_repo_language,
fingerprint separation between Python and JS repos for the same content.

Integration tests (Docker-gated): full verify() on synthetic JS repos —
green/broken/regression/flaky — proving the LANGUAGE-INDEPENDENT
verification logic (baseline/flake/regression) runs with a JS test
runner unchanged.
"""

import os
import subprocess
from pathlib import Path

import pytest

import execution.sandbox as sb
import execution.verify as vf


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


# ---------------------------------------------------------------------------
# Language detection (unit)
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    def test_python_markers_win(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert vf._detect_language(str(tmp_path)) == "python"

    def test_package_json_alone_is_javascript(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert vf._detect_language(str(tmp_path)) == "javascript"

    def test_bare_tests_dir_stays_python(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
        assert vf._detect_language(str(tmp_path)) == "python"

    def test_tests_dir_of_ts_files_is_javascript(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "mean.test.ts").write_text("", encoding="utf-8")
        assert vf._detect_language(str(tmp_path)) == "javascript"

    def test_bare_repo_returns_none(self, tmp_path):
        (tmp_path / "README.md").write_text("nothing", encoding="utf-8")
        assert vf._detect_language(str(tmp_path)) is None


class TestJsTestCommand:
    def test_vitest_detected(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"vitest": "^1.0.0"}}', encoding="utf-8"
        )
        assert vf._js_test_command(str(tmp_path)) == "npx vitest run"

    def test_jest_detected_via_devdeps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"jest": "^29.0.0"}}', encoding="utf-8"
        )
        assert vf._js_test_command(str(tmp_path)) == "npx jest"

    def test_jest_detected_via_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"test": "jest --coverage"}}', encoding="utf-8"
        )
        assert vf._js_test_command(str(tmp_path)) == "npx jest"

    def test_jest_detected_via_config_file(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "jest.config.js").write_text(
            "module.exports = {};", encoding="utf-8"
        )
        assert vf._js_test_command(str(tmp_path)) == "npx jest"

    def test_unreadable_manifest_falls_back_to_vitest(self, tmp_path):
        (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
        assert vf._js_test_command(str(tmp_path)) == "npx vitest run"

    def test_autodetect_composes(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"vitest": "^1.0.0"}}', encoding="utf-8"
        )
        assert vf._autodetect_test_command(str(tmp_path)) == "npx vitest run"


class TestJsTargetCommand:
    def test_file_and_name_form(self):
        cmd = vf._target_command(
            "tests/mean.test.ts - computes the mean",
            "npx vitest run",
            "javascript",
        )
        assert cmd == "npx vitest run tests/mean.test.ts -t 'computes the mean'"

    def test_pytest_style_separator_also_works(self):
        cmd = vf._target_command(
            "tests/mean.test.ts::computes the mean",
            "npx jest",
            "javascript",
        )
        assert cmd == "npx jest tests/mean.test.ts -t 'computes the mean'"

    def test_bare_name_filters_whole_suite(self):
        cmd = vf._target_command("computes the mean", "npx vitest run", "javascript")
        assert cmd == "npx vitest run -t 'computes the mean'"

    def test_runner_inferred_from_suite_cmd(self):
        # lang param omitted: suite_cmd itself carries the runner
        cmd = vf._target_command("a.test.js - foo", "npx jest")
        assert cmd == "npx jest a.test.js -t 'foo'"

    def test_quote_in_test_name_is_escaped(self):
        # an apostrophe inside the name must survive bash quoting
        cmd = vf._target_command(
            "a.test.js - it's one o'clock",
            "npx jest",
            "javascript",
        )
        assert cmd == "npx jest a.test.js -t 'it'\\''s one o'\\''clock'"

    def test_none_target_uses_suite_unchanged(self):
        assert (
            vf._target_command(None, "npx vitest run", "javascript") == "npx vitest run"
        )

    def test_embedded_target_wins(self):
        suite = "npx vitest run tests/mean.test.ts -t computes"
        assert (
            vf._target_command("tests/mean.test.ts - computes", suite, "javascript")
            == suite
        )

    def test_python_path_unchanged(self):
        # the original pytest composition must be byte-identical
        assert (
            vf._target_command(
                "tests/test_x.py::test_y", "python -m pytest -q", "python"
            )
            == "python -m pytest -q tests/test_x.py::test_y"
        )
        assert (
            vf._target_command("tests/test_x.py::test_y", None, "python")
            == "python -m pytest -q tests/test_x.py::test_y"
        )


class TestSandboxLanguageDetection:
    def test_package_json_repo_is_js(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert sb._detect_repo_language(str(tmp_path)) == "js"

    def test_python_markers_win(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert sb._detect_repo_language(str(tmp_path)) == "python"

    def test_src_census_without_manifests(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("", encoding="utf-8")
        assert sb._detect_repo_language(str(tmp_path)) == "js"

    def test_default_python(self, tmp_path):
        (tmp_path / "README.md").write_text("x", encoding="utf-8")
        assert sb._detect_repo_language(str(tmp_path)) == "python"

    def test_fingerprints_separate_languages(self, tmp_path):
        # Same package.json content, one repo with Python markers and one
        # without: different image families, different fingerprints.
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        pkg = '{"name": "x", "devDependencies": {"vitest": "^1.0.0"}}'
        (a / "package.json").write_text(pkg, encoding="utf-8")
        (b / "package.json").write_text(pkg, encoding="utf-8")
        (b / "requirements.txt").write_text("flask", encoding="utf-8")
        assert sb._dep_image_tag(str(a)) != sb._dep_image_tag(str(b))

    def test_js_dockerfile_shape(self):
        text = sb._js_repo_dockerfile_text("harness-exec:test", has_lockfile=True)
        assert "FROM " + sb.node_base_image_tag() in text
        assert "npm ci" in text
        text2 = sb._js_repo_dockerfile_text("harness-exec:test", has_lockfile=False)
        assert "npm install" in text2
        assert "--legacy-peer-deps" in text2

    def test_run_args_js_volume(self):
        args = sb._docker_run_args(
            "harness-exec:x",
            "C:/repo",
            30,
            False,
            None,
            "1g",
            1.0,
            512,
            js_deps="hexec-node-deps-abc",
        )
        assert "hexec-node-deps-abc:/workspace/node_modules" in args
        # RW by design (runner caches write into node_modules; the named
        # volume absorbs them — the host repo is untouched)
        assert not any(a.endswith(":/workspace/node_modules:ro") for a in args)
        args_py = sb._docker_run_args(
            "harness-exec:x",
            "C:/repo",
            30,
            False,
            None,
            "1g",
            1.0,
            512,
        )
        assert not any("node_modules" in a for a in args_py)


# ---------------------------------------------------------------------------
# Integration tests — Docker required; synthetic JS repos (vitest + jest)
# ---------------------------------------------------------------------------

_VITEST_PKG = """{
  "name": "jstest-repo",
  "version": "1.0.0",
  "type": "module",
  "devDependencies": {"vitest": "^1.6.0"}
}
"""

_JEST_PKG = """{
  "name": "jstest-repo",
  "version": "1.0.0",
  "devDependencies": {"jest": "^29.7.0"}
}
"""


def _mk_js_repo(tmp_path: Path, scenario: str, runner: str = "vitest") -> Path:
    """Build a synthetic JS repo for the given scenario.

    Scenarios: green / broken / regress / flaky (mirror the Python
    scenarios in tests/test_verify.py — the LANGUAGE-INDEPENDENT logic
    must behave identically through a JS runner).
    """
    (tmp_path / "package.json").write_text(
        _VITEST_PKG if runner == "vitest" else _JEST_PKG, encoding="utf-8"
    )
    if scenario == "green":
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mathutil.js").write_text(
            "export function mean(values) {\n"
            "  return values.reduce((a, b) => a + b, 0) / values.length;\n"
            "}\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "mathutil.test.js").write_text(
            "import { mean } from '../src/mathutil.js';\n"
            "import { test, expect } from 'vitest';\n"
            "test('computes the mean', () => {\n"
            "  expect(mean([1, 2, 3])).toBe(2);\n"
            "});\n",
            encoding="utf-8",
        )
    elif scenario == "broken":
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mathutil.js").write_text(
            "export function mean(values) {\n"
            "  return values.reduce((a, b) => a + b, 0) / (values.length - 1);\n"
            "}\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "mathutil.test.js").write_text(
            "import { mean } from '../src/mathutil.js';\n"
            "import { test, expect } from 'vitest';\n"
            "test('computes the mean', () => {\n"
            "  expect(mean([1, 2, 3])).toBe(2);\n"
            "});\n",
            encoding="utf-8",
        )
    elif scenario == "regress":
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mathutil.js").write_text(
            "export function mean(values) {\n"
            "  return values.reduce((a, b) => a + b, 0) / values.length;\n"
            "}\n"
            "export function add(a, b) {\n"
            "  return a - b;  // bug: breaks the OTHER test\n"
            "}\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "mean.test.js").write_text(
            "import { mean } from '../src/mathutil.js';\n"
            "import { test, expect } from 'vitest';\n"
            "test('computes the mean', () => {\n"
            "  expect(mean([1, 2, 3])).toBe(2);\n"
            "});\n",
            encoding="utf-8",
        )
        (tmp_path / "tests" / "add.test.js").write_text(
            "import { add } from '../src/mathutil.js';\n"
            "import { test, expect } from 'vitest';\n"
            "test('adds', () => {\n"
            "  expect(add(2, 3)).toBe(5);\n"
            "});\n",
            encoding="utf-8",
        )
    elif scenario == "flaky":
        # order-dependent FS state: run 1 pass, run 2 fail (same shape as
        # the Python flaky scenario — proving timeout-free flake detection
        # also works through a JS runner)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "flaky.test.js").write_text(
            "import { test, expect } from 'vitest';\n"
            "import fs from 'node:fs';\n"
            "import path from 'node:path';\n"
            "const MARKER = path.join(import.meta.dirname, '.flaky_marker');\n"
            "test('flips across runs', () => {\n"
            "  if (fs.existsSync(MARKER)) {\n"
            "    fs.rmSync(MARKER);\n"
            "    throw new Error('second run fails');\n"
            "  }\n"
            "  fs.writeFileSync(MARKER, 'x');\n"
            "});\n",
            encoding="utf-8",
        )
    return tmp_path


@requires_docker
class TestVerifyJsIntegration:
    def test_green_repo_all_true(self, tmp_path):
        repo = _mk_js_repo(tmp_path, "green")
        v = vf.verify(
            str(repo),
            "tests/mathutil.test.js - computes the mean",
            2,
            verify_timeout_s=300,
        )
        assert v.target_test_passed is True
        assert v.flaky is False
        assert v.regression_passed is True
        assert v.baseline_passed is False

    def test_broken_target_detected(self, tmp_path):
        repo = _mk_js_repo(tmp_path, "broken")
        v = vf.verify(
            str(repo),
            "tests/mathutil.test.js - computes the mean",
            2,
            verify_timeout_s=300,
        )
        assert v.target_test_passed is False
        assert v.flaky is False

    def test_regression_detected_when_target_passes(self, tmp_path):
        repo = _mk_js_repo(tmp_path, "regress")
        v = vf.verify(
            str(repo),
            "tests/mean.test.js - computes the mean",
            1,
            verify_timeout_s=300,
        )
        assert v.target_test_passed is True
        assert v.regression_passed is False  # add.test.js fails in the suite

    def test_flaky_flagged_not_misreported(self, tmp_path):
        repo = _mk_js_repo(tmp_path, "flaky")
        v = vf.verify(
            str(repo),
            "tests/flaky.test.js - flips across runs",
            2,
            verify_timeout_s=300,
        )
        assert v.flaky is True
        assert v.target_test_passed in (True, False)

    def test_raw_output_carries_js_command(self, tmp_path):
        repo = _mk_js_repo(tmp_path, "green")
        v = vf.verify(
            str(repo),
            "tests/mathutil.test.js - computes the mean",
            1,
            verify_timeout_s=300,
        )
        assert (
            "$ npx vitest run tests/mathutil.test.js -t 'computes the mean'"
            in v.raw_output
        )
        assert "exit=" in v.raw_output

    def test_sandbox_runs_node_and_resolves_deps(self, tmp_path):
        # the JS sandbox contract end-to-end: node is on PATH inside the
        # container, deps installed by the IMAGE resolve, and the mount
        # layout (node_modules overlay) works
        repo = _mk_js_repo(tmp_path, "green")
        res = sb.execute_sandboxed(
            str(repo), "node --version && npx vitest --version", 300
        )
        assert res.exit_code == 0
        assert "v" in res.stdout
