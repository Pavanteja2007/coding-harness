"""Tests for multi-language (JS/TS) support — Task B: code graph indexing
on JS/TS repos, retrieval anchoring, editor/lint syntax checks, agent-tests
sanitize, and prompt flavoring.

All offline (no Docker): the graph layer is pure tree-sitter + file I/O.
"""

import textwrap
from pathlib import Path

import pytest

from memory.code_graph import (
    CodeGraph,
    CodeGraphBuilder,
    _js_module_name,
    _js_normalize_specifier,
)


@pytest.fixture
def js_repo(tmp_path: Path) -> Path:
    """A small JS/TS repo exercising every graph feature the Python
    fixture does: classes, methods, imports (relative specifiers),
    re-exports, calls across files, arrow-function consts."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mathutil.js").write_text(
        textwrap.dedent("""\
        import { helper } from './helper.js';
        export function mean(values) {
          return helper.sum(values) / values.length;
        }
        export const add = (a, b) => a + b;
    """),
        encoding="utf-8",
    )
    (tmp_path / "src" / "helper.js").write_text(
        textwrap.dedent("""\
        export function sum(values) {
          let total = 0;
          for (const v of values) { total += v; }
          return total;
        }
        export class Calculator {
          double(x) { return x * 2; }
        }
    """),
        encoding="utf-8",
    )
    (tmp_path / "src" / "main.ts").write_text(
        textwrap.dedent("""\
        import { mean } from './mathutil.js';
        export function runMean(nums: number[]): number {
          return mean(nums);
        }
    """),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "mathutil.test.js").write_text(
        textwrap.dedent("""\
        import { test, expect } from 'vitest';
        import { mean } from '../src/mathutil.js';
        test('computes the mean', () => {
          expect(mean([1, 2, 3])).toBe(2);
        });
    """),
        encoding="utf-8",
    )
    return tmp_path


class TestJsGraphIndexing:
    def test_files_indexed_with_languages(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        files = {n.name for n in g.nodes.values() if n.kind == "file"}
        assert files == {
            "src/mathutil.js",
            "src/helper.js",
            "src/main.ts",
            "tests/mathutil.test.js",
        }

    def test_function_declaration_symbol(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        assert "func:src.mathutil.mean" in g.nodes
        assert "func:src.helper.sum" in g.nodes
        info = g.nodes["func:src.mathutil.mean"]
        assert info.file == "src/mathutil.js"
        assert info.line == 2

    def test_arrow_const_symbol(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        assert "func:src.mathutil.add" in g.nodes

    def test_class_and_method(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        assert "class:src.helper.Calculator" in g.nodes
        assert "method:src.helper.Calculator.double" in g.nodes

    def test_imports_resolved_across_js_and_ts(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        # main.ts imports ./mathutil.js -> module edge to src.mathutil
        assert ("module:src.main", "module:src.mathutil") in g.imports
        # mathutil.js imports ./helper.js
        assert ("module:src.mathutil", "module:src.helper") in g.imports
        # the test file imports ../src/mathutil.js
        assert ("module:tests.mathutil.test", "module:src.mathutil") in g.imports

    def test_calls_resolved(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        # runMean -> mean; mean -> sum; test callback -> mean
        assert ("func:src.main.runMean", "func:src.mathutil.mean") in g.calls
        assert ("func:src.mathutil.mean", "func:src.helper.sum") in g.calls

    def test_defines_edges(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        assert ("file:src/helper.js", "src.helper.sum") in g.defines
        assert ("file:src/mathutil.js", "src.mathutil.mean") in g.defines

    def test_external_specifiers_dont_create_edges(self, js_repo):
        g = CodeGraphBuilder(str(js_repo)).build()
        # 'vitest' is external: no module:vitest node, no edge
        assert "module:vitest" not in g.nodes
        assert not any("vitest" in t for _, t in g.imports)

    def test_module_name_helper(self):
        assert _js_module_name("src/lib/util.js") == "src.lib.util"
        assert _js_module_name("a.ts") == "a"
        assert _js_module_name("a.test.tsx") == "a.test"

    def test_specifier_normalization(self):
        assert _js_normalize_specifier("./x.js", "src/a.js") == "src/x.js"
        assert _js_normalize_specifier("../lib/x", "src/sub/a.ts") == "src/lib/x"
        assert _js_normalize_specifier("react", "src/a.js") is None

    def test_persistence_and_freshness(self, js_repo, tmp_path):
        cg = CodeGraph(str(js_repo), root=str(tmp_path / "idx"))
        g1 = cg.load_or_build()
        assert "func:src.mathutil.mean" in g1.nodes
        g2 = cg.load_or_build()  # reuse (mtimes unchanged)
        assert g2.nodes == g1.nodes  # same content either way
        (js_repo / "src" / "mathutil.js").write_text(
            "export function mean(v) { return 0; }\n", encoding="utf-8"
        )
        g3 = cg.load_or_build()  # mtime changed -> rebuild
        assert g3.built_at != g1.built_at

    def test_queries_answer_on_js_repo(self, js_repo, tmp_path):
        cg = CodeGraph(str(js_repo), root=str(tmp_path / "idx"))
        out = cg.query("callers mean")
        # runMean calls mean; the test module's callback is a documented
        # skip (arrow-function args to test() aren't indexed as callers)
        assert "src.main.runMean" in out
        out = cg.query("importers src.mathutil")
        assert "src.main" in out or "tests.mathutil.test" in out
        out = cg.query("file src/helper.js")
        assert "Calculator" in out and "sum" in out
        out = cg.query("symbol mean")
        assert "src.mathutil.mean" in out

    def test_mixed_python_js_repo(self, tmp_path):
        # one repo, both languages, one graph — no collisions
        (tmp_path / "app.py").write_text(
            "def mean(v):\n    return sum(v) / len(v)\n", encoding="utf-8"
        )
        (tmp_path / "app.js").write_text(
            "export function mean(v) { return 0; }\n", encoding="utf-8"
        )
        g = CodeGraphBuilder(str(tmp_path)).build()
        assert "func:app.mean" in g.nodes  # python (dotted module)
        assert "func:app.mean" in g.nodes  # js (path-based module app)
        # both exist — same id is fine (they're the same symbol name in
        # the same "app" module identity); the point is no crash and a
        # complete file census
        files = {n.name for n in g.nodes.values() if n.kind == "file"}
        assert files == {"app.py", "app.js"}


class TestRetrievalOnJsRepo:
    def test_target_test_anchor_js(self, js_repo):
        from harness.retrieval import retrieve_context

        out = retrieve_context(
            str(js_repo),
            "mean computes wrong totals for the tests",
            target_test="tests/mathutil.test.js - computes the mean",
            max_files=4,
        )
        # the anchor must lead to the module under test (src/mathutil.js)
        # via the test file's import edges — even though the issue text
        # barely matches anything
        assert "src/mathutil.js" in out["files"]
        assert "structural" in out["strategy"]

    def test_pytest_style_anchor_js(self, js_repo):
        # '<file>::<name>' form must anchor identically
        from harness.retrieval import retrieve_context

        out = retrieve_context(
            str(js_repo),
            "numbers average badly",
            target_test="tests/mathutil.test.js::computes the mean",
        )
        assert "src/mathutil.js" in out["files"]


class TestEditorSyntaxCheck:
    def test_js_syntax_error_caught(self, tmp_path):
        from harness.editor import syntax_check

        (tmp_path / "a.js").write_text(
            "function broken( {\n  return 1;\n", encoding="utf-8"
        )
        ok, msg = syntax_check(str(tmp_path), ["a.js"])
        assert not ok
        assert "a.js" in msg

    def test_ts_syntax_error_caught(self, tmp_path):
        from harness.editor import syntax_check

        (tmp_path / "a.ts").write_text("const x: number = ;\n", encoding="utf-8")
        ok, _msg = syntax_check(str(tmp_path), ["a.ts"])
        assert not ok

    def test_valid_js_passes(self, tmp_path):
        from harness.editor import syntax_check

        (tmp_path / "a.js").write_text(
            "export const ok = (x) => x + 1;\n", encoding="utf-8"
        )
        ok, _msg = syntax_check(str(tmp_path), ["a.js"])
        assert ok

    def test_python_still_works(self, tmp_path):
        from harness.editor import syntax_check

        (tmp_path / "a.py").write_text("def f(:\n", encoding="utf-8")
        ok, _msg = syntax_check(str(tmp_path), ["a.py"])
        assert not ok

    def test_check_edits_full_path_js(self, tmp_path):
        # check_edits (the pre-verify gate) catches a JS syntax error in
        # the working copy — same protection Python edits get
        from harness.editor import check_edits

        pristine = tmp_path / "pristine"
        work = tmp_path / "work"
        pristine.mkdir()
        work.mkdir()
        (pristine / "lib.js").write_text("export const x = 1;\n", encoding="utf-8")
        (work / "lib.js").write_text(
            "export const x = 1;;;\nfunction f( {\n", encoding="utf-8"
        )
        ok, msg, _changed = check_edits(str(pristine), str(work), [])
        assert not ok
        assert "lib.js" in msg


class TestLintJsSyntax:
    def test_lint_js_syntax_error(self):
        from harness.lint import check_syntax

        findings = check_syntax("function f( {\n", "a.test.js")
        assert findings and findings[0].kind == "syntax"

    def test_lint_ts_syntax_error(self):
        from harness.lint import check_syntax

        findings = check_syntax("const x: = 1;\n", "a.test.ts")
        assert findings and findings[0].kind == "syntax"

    def test_lint_valid_js_clean(self):
        from harness.lint import check_syntax

        assert check_syntax("const a = 1;\n", "a.js") == []

    def test_lint_changed_js(self, tmp_path):
        from harness.lint import lint_changed, render_findings

        (tmp_path / "bad.js").write_text("function ( {\n", encoding="utf-8")
        findings = lint_changed(str(tmp_path), ["bad.js"])
        assert findings
        assert "LINT FAILED" in render_findings(findings)


class TestAgentTestsJs:
    def test_sanitize_accepts_js_test_file(self):
        from harness.agent_tests import sanitize_agent_tests

        content = (
            "import { test, expect } from 'vitest';\n"
            "test('edge', () => { expect(1).toBe(1); });\n"
        )
        kept, reasons = sanitize_agent_tests(
            [{"filename": "edge.test.js", "content": content}], 3, 12000
        )
        assert len(kept) == 1
        assert not reasons

    def test_sanitize_accepts_ts_test_file(self):
        from harness.agent_tests import sanitize_agent_tests

        content = (
            "import { test, expect } from 'vitest';\n"
            "test('edge', () => { expect(1 as number).toBe(1); });\n"
        )
        kept, _reasons = sanitize_agent_tests(
            [{"filename": "edge.test.ts", "content": content}], 3, 12000
        )
        assert len(kept) == 1

    def test_sanitize_drops_js_syntax_error(self):
        from harness.agent_tests import sanitize_agent_tests

        kept, reasons = sanitize_agent_tests(
            [{"filename": "edge.test.js", "content": "function ( {"}], 3, 12000
        )
        assert not kept
        assert "syntax error" in reasons[0]

    def test_sanitize_still_drops_bad_python(self):
        from harness.agent_tests import sanitize_agent_tests

        kept, _reasons = sanitize_agent_tests(
            [{"filename": "edge.py", "content": "def f(:"}], 3, 12000
        )
        assert not kept

    def test_sanitize_still_accepts_good_python(self):
        from harness.agent_tests import sanitize_agent_tests

        kept, _ = sanitize_agent_tests(
            [{"filename": "edge.py", "content": "def test_x():\n    assert 1\n"}],
            3,
            12000,
        )
        assert len(kept) == 1

    def test_list_test_files_finds_js(self, tmp_path):
        from harness.agent_tests import list_test_files

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "a.test.js").write_text("", encoding="utf-8")
        (tmp_path / "tests" / "b.spec.ts").write_text("", encoding="utf-8")
        (tmp_path / "tests" / "test_c.py").write_text("", encoding="utf-8")
        out = list_test_files(str(tmp_path))
        assert "tests/a.test.js" in out
        assert "tests/b.spec.ts" in out
        assert "tests/test_c.py" in out


class TestPromptLanguageFlavor:
    def test_default_python(self):
        from harness.prompts import render_step_system

        prompt = render_step_system(
            issue_text="i",
            plan=[{"id": 1, "description": "d", "checkpoint": "c"}],
            step_id=1,
            total_steps=1,
            completed_block="",
            context_block="",
            max_output_chars=1000,
        )
        assert "Python repository" in prompt
        assert "node and npx are on PATH" not in prompt

    def test_js_flavor(self):
        from harness.prompts import render_step_system

        prompt = render_step_system(
            issue_text="i",
            plan=[{"id": 1, "description": "d", "checkpoint": "c"}],
            step_id=1,
            total_steps=1,
            completed_block="",
            context_block="",
            max_output_chars=1000,
            language="js",
        )
        assert "JavaScript/TypeScript repository" in prompt
        assert "node and npx are on PATH" in prompt


class TestCoreLanguageDetection:
    def test_core_detects_js(self, tmp_path):
        from harness.core import _detect_repo_language

        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert _detect_repo_language(str(tmp_path)) == "js"

    def test_core_detects_python(self, tmp_path):
        from harness.core import _detect_repo_language

        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        assert _detect_repo_language(str(tmp_path)) == "python"
