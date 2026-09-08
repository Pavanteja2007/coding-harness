"""Tests for harness.retrieval (dumb grep retrieval + Phase-2 structural
layer) and harness.tools (bash-only action space)."""
from pathlib import Path

import pytest

from harness.retrieval import extract_terms, rank_files, retrieve_context, search_file_for_lines
from harness import tools as tool_mod


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
        "def pop(self):\n    return self._items.pop()\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.md").write_text(
        "meeting notes: the pop method", encoding="utf-8")
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
        "    \"\"\"Sum of all entries for the month.\"\"\"\n"
        "    return sum(e for e in entries) * 2  # bug: multiplies\n",
        encoding="utf-8")
    (repo / "payroll" / "tests").mkdir()
    (repo / "payroll" / "tests" / "test_engine.py").write_text(
        "from payroll.engine import compute_monthly_total\n\n"
        "def test_monthly_total():\n"
        "    assert compute_monthly_total([1, 2, 3]) == 6\n",
        encoding="utf-8")
    (repo / "payroll" / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", encoding="utf-8")

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
    ctx = retrieve_context(
        str(repo), "the pop method is broken",
        max_files=2, target_test="tests/test_x.py::test_pop",
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
