"""Tests for harness.retrieval (dumb grep retrieval + Phase-2 structural
layer) and harness.tools (bash-only action space)."""

from pathlib import Path

import pytest

from harness import tools as tool_mod
from harness.context_compiler import ContextCompiler
from harness.retrieval import (
    changed_symbol_context,
    extract_terms,
    rank_files,
    rank_symbols,
    retrieve_context,
    retrieve_exact_symbol,
    search_file_for_lines,
)


def test_extract_terms_prefers_identifiers(tmp_path):
    terms = extract_terms(
        "The stack.pop method in stacklib raises IndexError instead of "
        "StackEmptyError when popping an empty stack."
    )
    assert "stack.pop" in terms
    assert "stacklib" in terms
    assert "IndexError" in terms
    assert "StackEmptyError" in terms
    # stopwords stay out
    assert "the" not in [t.lower() for t in terms]
    assert "when" not in [t.lower() for t in terms]


def test_extract_terms_handles_file_paths():
    terms = extract_terms("Fix the bug in tests/test_stack.py::test_pop_empty")
    assert any("test_stack.py" in t for t in terms)


def _make_repo(tmp_path):
    (tmp_path / "stacklib").mkdir()
    (tmp_path / "stacklib" / "stack.py").write_text(
        "def pop(self):\n    return self._items.pop()\n", encoding="utf-8"
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.md").write_text(
        "meeting notes: the pop method", encoding="utf-8"
    )
    return tmp_path


def test_rank_files_scores_by_term_hits(tmp_path):
    root = _make_repo(tmp_path)
    terms = extract_terms("pop method raises IndexError")
    ranked = rank_files(str(root), terms, limit=4)
    assert ranked[0] == "stacklib/stack.py"


def test_retrieve_context_shape(tmp_path):
    root = _make_repo(tmp_path)
    ctx = retrieve_context(str(root), "the pop method is broken", max_files=2)
    assert set(ctx.keys()) == {"terms", "files", "greps", "strategy"}
    assert "stacklib/stack.py" in ctx["files"]
    assert ctx["greps"]["stacklib/stack.py"]  # grep found the 'pop' lines


def test_search_file_for_lines_finds_symbol(tmp_path):
    root = _make_repo(tmp_path)
    hits = search_file_for_lines(str(root), "stacklib/stack.py", ["pop"])
    assert hits and "def pop" in hits[0]


def test_search_file_for_lines_rejects_escape(tmp_path):
    root = _make_repo(tmp_path)
    (tmp_path / "outside.py").write_text("secret\n", encoding="utf-8")
    assert search_file_for_lines(str(root), "../outside.py", ["secret"]) == []


# ---------------------------------------------------------------------------
# structural layer (Phase 2)
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _struct_available() -> bool:
    try:
        from harness.deps import get_code_graph_factory

        return get_code_graph_factory() is not None
    except Exception:
        return False


def test_structural_finds_code_via_target_test_without_names(tmp_path):
    """THE Phase-2 scenario: the issue text mentions NO symbol/file names;
    the target test is the only anchor. The structural layer must locate
    the module under test through the repo's actual import/call edges —
    dumb grep alone returns nothing for this issue text."""
    if not _struct_available():
        pytest.skip("memory.code_graph not importable")
    ctx = retrieve_context(
        str(FIXTURES / "bug02_mean"),
        "something is off in how a small collection of numbers is "
        "summarized; results come out wrong",
        max_files=3,
        target_test="tests/test_mathutil.py::test_mean_even_count",
    )
    assert ctx["strategy"].startswith("structural+grep")
    assert "numlib/mathutil.py" in ctx["files"]
    # the anchor chain also surfaces the test itself (high-value context)
    assert "tests/test_mathutil.py" in ctx["files"]


def test_structural_matches_concept_to_identifier(tmp_path):
    """Issue names a CONCEPT ('monthly total') but not the identifier
    (compute_monthly_total) — subword matching over the symbol table must
    find it with zero grep hits."""
    if not _struct_available():
        pytest.skip("memory.code_graph not importable")
    repo = tmp_path / "payroll"
    (repo / "payroll").mkdir(parents=True)
    (repo / "payroll" / "engine.py").write_text(
        "def compute_monthly_total(entries):\n"
        '    """Sum of all entries for the month."""\n'
        "    return sum(e for e in entries) * 2  # bug: multiplies\n",
        encoding="utf-8",
    )
    (repo / "payroll" / "tests").mkdir()
    (repo / "payroll" / "tests" / "test_engine.py").write_text(
        "from payroll.engine import compute_monthly_total\n\n"
        "def test_monthly_total():\n"
        "    assert compute_monthly_total([1, 2, 3]) == 6\n",
        encoding="utf-8",
    )
    (repo / "payroll" / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )

    ctx = retrieve_context(
        str(repo),
        "the monthly total for our payroll run is doubled",
        max_files=3,
        target_test="tests/test_engine.py::test_monthly_total",
    )
    assert ctx["strategy"].startswith("structural+grep")
    assert "payroll/engine.py" in ctx["files"], ctx["files"]


def test_structural_never_writes_into_original_repo(tmp_path):
    """The structural index must live OUTSIDE the repo: the harness
    guarantees the original repo is never mutated."""
    if not _struct_available():
        pytest.skip("memory.code_graph not importable")
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    before = sorted(p.as_posix() for p in repo.rglob("*"))
    retrieve_context(
        str(repo),
        "the pop method is broken",
        max_files=2,
        target_test="tests/test_x.py::test_pop",
        index_root=tmp_path / "index-out",
    )
    after = sorted(p.as_posix() for p in repo.rglob("*"))
    assert before == after, "retrieval wrote into the original repo"
    assert (tmp_path / "index-out").is_dir(), "index should persist outside"


def test_structural_failure_degrades_to_grep(tmp_path, monkeypatch):
    """A broken graph layer must degrade to grep-only — retrieval never
    kills a task run (and reports which strategy ran)."""
    import harness.deps as deps

    class BrokenGraph:
        def __init__(self, *a, **k):
            raise RuntimeError("graph exploded")

    monkeypatch.setattr(deps, "get_code_graph_factory", lambda: BrokenGraph)
    root = _make_repo(tmp_path)
    ctx = retrieve_context(str(root), "the pop method is broken", max_files=2)
    assert ctx["strategy"] == "grep"
    assert "stacklib/stack.py" in ctx["files"]


def test_retrieve_context_reports_strategy(tmp_path):
    root = _make_repo(tmp_path)
    ctx = retrieve_context(str(root), "the pop method is broken", max_files=2)
    assert "strategy" in ctx
    assert ctx["strategy"].startswith(("grep", "structural+grep"))


# ---------------------------------------------------------------------------
# tools: bash session
# ---------------------------------------------------------------------------


def test_truncate_keeps_head_and_tail():
    text = "A" * 100
    out = tool_mod.truncate(text, 30)
    assert len(out) < 100
    assert "omitted" in out
    assert out.startswith("A") and out.rstrip().endswith("A")


def test_is_submit_variants():
    assert tool_mod.is_submit("SUBMIT")
    assert tool_mod.is_submit("  submit  ")
    assert tool_mod.is_submit("SUBMIT\n")
    assert not tool_mod.is_submit("SUBMIT extra text")
    assert not tool_mod.is_submit("echo hello")


class FakeSandbox:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or []

    def __call__(self, repo_path, command, timeout_s):
        self.calls.append((repo_path, command, timeout_s))
        if self.results:
            r = self.results.pop(0)
            return r
        from shared.types import ExecutionResult

        return ExecutionResult(0, "", "", False)


@pytest.fixture
def fake_sandbox():
    fs = FakeSandbox()
    import harness.deps as deps

    deps.set_execute_sandboxed(fs)
    yield fs
    deps.reset_overrides()


def test_session_runs_and_tracks_command(fake_sandbox):
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("echo hello")
    assert "exit=0" in out
    assert len(session.commands) == 1
    assert session.commands[0]["command"] == "echo hello"
    # ran against the configured repo root
    assert fake_sandbox.calls[0][0] == "/repo"


def test_session_composes_cwd(fake_sandbox):
    from shared.types import ExecutionResult

    fake_sandbox.results = [ExecutionResult(0, "", "", False)] * 3
    session = tool_mod.BashSession("/repo", 30, 500)
    session.run("cd sub")
    session.run("ls")
    # second command is prefixed with the tracked cwd
    assert fake_sandbox.calls[1][1] == 'cd "sub" && ls'


def test_session_denies_dangerous_command(fake_sandbox):
    session = tool_mod.BashSession("/repo", 30, 500)
    with pytest.raises(PermissionError):
        session.run("rm -rf /")


def test_session_output_truncated_to_cap(fake_sandbox):
    from shared.types import ExecutionResult

    fake_sandbox.results = [ExecutionResult(0, "x" * 10_000, "", False)]
    session = tool_mod.BashSession("/repo", 30, 500)
    out = session.run("cat big")
    assert len(out) <= 500 + 200  # cap + wrapper text allowance
    assert "omitted" in out


def test_is_observation_command():
    assert tool_mod.is_observation_command("cat foo.py")
    assert tool_mod.is_observation_command("  git diff")
    assert not tool_mod.is_observation_command("python -m pytest tests/")


def test_rank_files_skips_symlink_sources(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def secret_symbol():\n    return 1\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    assert "linked.py" not in rank_files(str(repo), ["secret_symbol"])


def test_context_compiler_preserves_instructions_and_acceptance_at_small_budget(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("ROOT-RULE-MARKER", encoding="utf-8")
    (repo / ".neo").mkdir()
    (repo / ".neo" / "GEMINI.md").write_text("VEX-RULE-MARKER", encoding="utf-8")
    compiler = ContextCompiler(repo, config={"skills_enabled": False})
    bundle = compiler.compile(
        issue_text="x" * 20_000,
        task={
            "issue_text": "x" * 20_000,
            "acceptance_criteria": ["ACCEPTANCE-MARKER"],
        },
        token_budget=20,
        use_cache=False,
    )
    assert "ROOT-RULE-MARKER" in bundle.text
    assert "ACCEPTANCE-MARKER" in bundle.text
    assert bundle.estimated_tokens <= bundle.token_budget
    assert {item["source"] for item in bundle.citations} >= {
        "instruction",
        "criteria",
    }
    assert not any(
        "ACCEPTANCE-MARKER" in str(item.get("content")) for item in bundle.citations
    )


def test_context_compiler_keeps_memory_separate_and_caches_by_digest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("INSTRUCTION-ONLY", encoding="utf-8")
    compiler = ContextCompiler(repo, config={"skills_enabled": False})
    first = compiler.compile(
        issue_text="value",
        task={"acceptance_criteria": ["CRITERION-ONLY"]},
        selected_files=["app.py"],
        decision_memory=["MEMORY-ONLY"],
        token_budget=200,
    )
    second = compiler.compile(
        issue_text="value",
        task={"acceptance_criteria": ["CRITERION-ONLY"]},
        selected_files=["app.py"],
        decision_memory=["MEMORY-ONLY"],
        token_budget=200,
    )
    assert second.cache_hit is True
    assert "INSTRUCTION-ONLY" in first.text
    assert "MEMORY-ONLY" in first.text
    assert "MEMORY-ONLY" not in str(
        [item for item in first.source_references if item["source"] == "instruction"]
    )
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    third = compiler.compile(
        issue_text="value",
        task={"acceptance_criteria": ["CRITERION-ONLY"]},
        selected_files=["app.py"],
        decision_memory=["MEMORY-ONLY"],
        token_budget=200,
    )
    assert third.cache_hit is False
    assert third.source_digest != first.source_digest
    ranged = compiler.compile(
        issue_text="value",
        selected_files=[{"file": "app.py", "line": 1, "end_line": 1}],
        decision_memory=["MEMORY-ONLY"],
        token_budget=200,
    )
    assert ranged.cache_hit is False


def test_weighted_symbol_map_is_deterministic_and_structural(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text(
        "def central():\n    return leaf()\n\ndef leaf():\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "noise.py").write_text("def unrelated():\n    return 0\n", encoding="utf-8")
    first = rank_symbols(repo, terms=["central"], index_root=tmp_path / "index")
    second = rank_symbols(repo, terms=["central"], index_root=tmp_path / "index")
    assert first == second
    assert first[0]["qualified"] == "core.central"
    assert first[0]["pagerank"] >= first[-1]["pagerank"]


def test_exact_symbol_and_dependency_context_use_ranges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text(
        "def target():\n    return 1\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )
    exact = retrieve_exact_symbol(repo, "target", index_root=tmp_path / "index")
    assert len(exact) == 1
    assert exact[0]["line"] == 1 and exact[0]["end_line"] == 2
    assert retrieve_exact_symbol(repo, "tar", index_root=tmp_path / "index") == []
    assert retrieve_exact_symbol(repo, "core", index_root=tmp_path / "index") == []
    assert (
        retrieve_exact_symbol(
            repo, "target", start_line=99, index_root=tmp_path / "index"
        )
        == []
    )
    context = changed_symbol_context(
        repo,
        changed_files=["core.py"],
        index_root=tmp_path / "index",
    )
    assert any(item["name"] == "target" for item in context["changed"])
    assert any(item["name"] == "caller" for item in context["callers"])


def test_context_compaction_keeps_mandatory_sources_and_trace_citations(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("RULE-MARKER", encoding="utf-8")
    compiler = ContextCompiler(repo, config={"skills_enabled": False})
    bundle = compiler.compile(
        issue_text="x" * 500,
        task={"acceptance_criteria": ["ACCEPT-MARKER"]},
        token_budget=200,
        use_cache=False,
    )
    compact = bundle.compact(20)
    assert "RULE-MARKER" in compact.text
    assert "ACCEPT-MARKER" in compact.text
    assert compact.estimated_tokens <= compact.token_budget
    assert compact.source_references
    trace = type(
        "Trace",
        (),
        {
            "events": [],
            "log": lambda self, kind, data: self.events.append((kind, data)),
        },
    )()
    from harness.context_compiler import emit_context_trace

    receipt = emit_context_trace(bundle, trace)
    assert trace.events[0][0] == "context"
    assert receipt["citations"]
    assert all(item["id"].startswith("ctx:") for item in receipt["citations"])


def test_context_sources_are_redacted_and_have_lossless_references(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("RULE", encoding="utf-8")
    compiler = ContextCompiler(repo, config={"skills_enabled": False})
    bundle = compiler.compile(
        issue_text="Bearer TASK-SECRET",
        task={"acceptance_criteria": ["Bearer ACCEPT-SECRET"]},
        skills="Bearer SKILL-SECRET",
        decision_memory="Bearer MEMORY-SECRET",
        token_budget=200,
        use_cache=False,
    )
    serialized = bundle.as_dict()
    assert "TASK-SECRET" not in serialized["text"]
    assert "SKILL-SECRET" not in serialized["text"]
    assert "MEMORY-SECRET" not in serialized["text"]
    assert "TASK-SECRET" not in str(serialized["source_references"])
    assert "SKILL-SECRET" not in str(serialized["source_references"])
    assert "MEMORY-SECRET" not in str(serialized["source_references"])
    assert all(
        reference.get("source_ref") for reference in serialized["source_references"]
    )


def test_instruction_source_truncation_is_explicit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "R" * 65_000
    (repo / "AGENTS.md").write_text(content, encoding="utf-8")
    bundle = ContextCompiler(repo, config={"skills_enabled": False}).compile(
        issue_text="instruction",
        token_budget=200,
        use_cache=False,
    )
    reference = next(
        item for item in bundle.source_references if item["source"] == "instruction"
    )
    assert reference["source_truncated"] is True
    assert reference["source_total_bytes"] > 64_000
    assert reference["source_ref"]["kind"] == "file"
    assert reference["source_ref"]["path"] == "AGENTS.md"


def test_context_compiler_rejects_empty_root_and_outside_paths(tmp_path, monkeypatch):
    compiler = ContextCompiler("", config={"skills_enabled": False})
    bundle = compiler.compile(
        issue_text="outside",
        task={"acceptance_criteria": ["KEEP"]},
        changed_files=["../outside.py"],
        selected_files=["../outside.py"],
        token_budget=100,
        use_cache=False,
    )
    assert "KEEP" in bundle.text
    assert any("invalid repository root" in warning for warning in bundle.warnings)
    assert not bundle.sections or all(
        section["name"] != "repository_map"
        for section in bundle.sections
        if section.get("included")
    )

    calls = []
    manager = type(
        "Manager",
        (),
        {
            "get_diagnostics": lambda self, path, timeout_s=None: (
                calls.append(path) or []
            ),
        },
    )()
    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = ContextCompiler(repo, config={"skills_enabled": False}).compile(
        issue_text="outside",
        changed_files=["../outside.py", "inside.py"],
        lsp_manager=manager,
        token_budget=100,
        use_cache=False,
    )
    assert calls
    assert all("outside.py" not in str(call) for call in calls)


def test_default_skill_discovery_is_fingerprinted_for_cache(tmp_path, monkeypatch):
    import harness.skills as skills_mod

    calls = []

    def scan(**kwargs):
        calls.append(kwargs)
        return {
            "skills_block": "SKILL-MARKER",
            "receipts": [{"name": "demo", "source": "project", "body": "SKILL-MARKER"}],
        }

    monkeypatch.setattr(skills_mod, "scan_skills_for_task", scan)
    repo = tmp_path / "repo"
    repo.mkdir()
    compiler = ContextCompiler(repo)
    first = compiler.compile(issue_text="cache", token_budget=200)
    second = compiler.compile(issue_text="cache", token_budget=200)
    assert "SKILL-MARKER" in first.text
    assert second.cache_hit is True
    assert len(calls) == 1
