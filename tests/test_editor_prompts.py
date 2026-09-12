"""Tests for harness.editor (snapshot/diff/validation) and prompts."""

import json

import pytest

from harness import editor
from harness import prompts


@pytest.fixture
def repo_pair(tmp_path):
    """Pristine + work dirs with one small change."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        d.mkdir()
        (d / "pkg").mkdir()
        (d / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (d / "pkg" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    (work / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (work / "pkg" / "new.py").write_text("y = 2\n", encoding="utf-8")
    (pristine / "pkg" / "gone.py").write_text("z = 3\n", encoding="utf-8")
    return pristine, work


def test_changed_files_detects_all_change_kinds(repo_pair):
    pristine, work = repo_pair
    changed = editor.changed_files(str(pristine), str(work))
    assert "pkg/mod.py" in changed  # modified
    assert "pkg/new.py" in changed  # added
    assert "pkg/gone.py" in changed  # deleted
    assert "pkg/extra.py" not in changed  # untouched


def test_unified_diff_marks_change(repo_pair):
    pristine, work = repo_pair
    diff = editor.unified_diff(str(pristine), str(work))
    assert diff is not None
    assert "--- a/pkg/mod.py" in diff
    assert "+++ b/pkg/mod.py" in diff
    assert "-    return 1" in diff
    assert "+    return 2" in diff


def test_unified_diff_empty_when_no_change(repo_pair):
    pristine, _ = repo_pair
    assert editor.unified_diff(str(pristine), str(pristine)) == ""


def test_unified_diff_reports_binary_not_crash(tmp_path):
    """Round-4 OSS-run regression: a non-UTF-8 changed file (pytest-cov's
    SQLite .coverage materialized in work/ by the verifier) must make
    unified_diff return None (binary), NEVER raise UnicodeDecodeError —
    the old code read with errors="strict" OUTSIDE its try/except, so the
    decode error escaped and killed the task on the SUCCESS path after
    final verify had already passed."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        d.mkdir()
        (d / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (work / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (work / ".coverage").write_bytes(
        b"SQLite format 3\x00\x10\x00\x01\x01\x00@  \xff\xfe\xfa binary"
    )
    result = editor.unified_diff(str(pristine), str(work))  # must not raise
    # .coverage is skipped as a run artifact, so the REAL edit diffuses
    # through; with artifact-skipping disabled the binary file would make
    # this None instead of a crash (covered by the paired unit below).
    assert result is not None and "--- a/mod.py" in result


def test_unified_diff_binary_file_reported_not_crash(tmp_path):
    """The bare binary-safety net (no artifact skipping involved): a
    non-UTF-8 changed file with a real filename must produce None
    (binary), never UnicodeDecodeError — the old code read with
    errors="strict" OUTSIDE its try/except so the decode error escaped."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        d.mkdir()
        (d / "data.bin").write_bytes(b"\xff\xfe\xfa binary junk")
    (work / "data.bin").write_bytes(b"\x00\x10\x01 changed binary")
    (work / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (pristine / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = editor.unified_diff(str(pristine), str(work))  # must not raise
    assert result is None  # binary file present -> reported, not crashed


def test_snapshot_log_root_inside_repo_no_recursion(tmp_path, monkeypatch):
    """Regression (plain-`vex` interactive flow): when the log root lives
    INSIDE the repo being snapshotted (cd <repo>; vex -> logs default to
    ./logs under the repo), snapshot must exclude the log-root chain
    instead of recursing into its own destination until RecursionError.
    Found live by driving the real `vex` no-args session in a scratch
    repo — every scripted caller had placed logs outside the repo, so
    the shape was untested."""
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    logs = repo / "logs"  # where the interactive session puts the run
    logs.mkdir()
    (logs / "stale-run").mkdir()  # a PREVIOUS run's dir under the root
    (logs / "stale-run" / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(repo)  # CWD = repo, exactly the interactive flow
    dst = logs / "fix-abc123" / "pristine"
    try:
        editor.snapshot(str(repo), str(dst))
    except RecursionError:  # pragma: no cover - the pre-fix failure
        pytest.fail("snapshot recursed into its own destination")
    # The pristine copy contains the repo's real content...
    assert (dst / "pkg" / "mod.py").is_file()
    # ...and NOT the log chain (its own destination's ancestors) —
    # harness artifacts never belong in the pristine reference anyway.
    assert not (dst / "logs").exists()


def test_snapshot_outside_log_root_copies_everything(tmp_path, monkeypatch):
    """The guard must NOT over-exclude in the normal scripted shape
    (logs OUTSIDE the repo): the pristine copy then contains the whole
    repo, including any same-named 'logs' dir that is real repo content
    (an unignored sibling — only the chain TO THE DESTINATION is cut)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "logs").mkdir()  # REAL repo content that happens to be named logs
    (repo / "logs" / "important.txt").write_text("data\n", encoding="utf-8")

    outside_root = tmp_path / "run-logs"  # NOT inside the repo
    monkeypatch.chdir(tmp_path)
    dst = outside_root / "fix-abc123" / "pristine"
    editor.snapshot(str(repo), str(dst))
    assert (dst / "pkg" / "mod.py").is_file()
    assert (dst / "logs" / "important.txt").is_file()


def test_snapshot_dst_directly_under_src(tmp_path):
    """Edge of the dst-inside-src guard: dst directly under src (empty
    chain) — snapshotting repo -> repo/<name> must exclude exactly that
    name, not loop or drop other content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    dst = repo / "pristine-copy"
    editor.snapshot(str(repo), str(dst))
    assert (dst / "mod.py").is_file()
    assert not (dst / "pristine-copy").exists()  # no self-nesting


def test_changed_files_ignores_verifier_run_artifacts(tmp_path):
    """Round-4 OSS-run regression: artifacts the verifier itself creates
    in work/ (.coverage SQLite, .pytest_cache) are not the agent's edit —
    they must not appear in changed_files (they'd pollute files_touched,
    the diff's binary heuristic, and git output's commit payload)."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        d.mkdir()
        (d / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (work / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (work / ".coverage").write_bytes(b"SQLite format 3\x00binary")
    cache = work / ".pytest_cache" / "v"
    cache.mkdir(parents=True)
    (cache / "file.txt").write_text("cache", encoding="utf-8")
    changed = editor.changed_files(str(pristine), str(work))
    assert changed == ["mod.py"]


def test_syntax_check_flags_broken_py(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    ok, msg = editor.syntax_check(str(work), ["bad.py"])
    assert not ok
    assert "bad.py" in msg


def test_syntax_check_ok(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "good.py").write_text("def ok():\n    pass\n", encoding="utf-8")
    assert editor.syntax_check(str(work), ["good.py"])[0] is True


def test_is_protected_globs():
    assert editor.is_protected("tests/test_stack.py", ["tests/*"])
    assert editor.is_protected("tests/test_x.py", ["test_*.py"])
    assert editor.is_protected("a/b/c.py", ["b"])
    assert not editor.is_protected("stacklib/stack.py", ["tests/*"])


def test_check_edits_blocks_protected_path(tmp_path):
    pristine, work = tmp_path / "p", tmp_path / "w"
    for d in (pristine, work):
        d.mkdir()
        (d / "tests").mkdir()
        (d / "tests" / "t.py").write_text("a = 1\n", encoding="utf-8")
    (work / "tests" / "t.py").write_text("a = 2\n", encoding="utf-8")
    ok, msg, changed = editor.check_edits(str(pristine), str(work), ["tests/*"])
    assert not ok
    assert "protected" in msg
    assert changed == ["tests/t.py"]


def test_restore_dir_roundtrip(tmp_path):
    pristine, work = tmp_path / "p", tmp_path / "w"
    for d in (pristine, work):
        d.mkdir()
        (d / "f.py").write_text("orig\n", encoding="utf-8")
    (work / "f.py").write_text("mutated\n", encoding="utf-8")
    (work / "junk.pyc").write_text("x", encoding="utf-8")
    editor.restore_dir(str(pristine), str(work))
    assert (work / "f.py").read_text(encoding="utf-8") == "orig\n"
    assert not (work / "junk.pyc").exists()


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

PLAN = [
    {"id": 1, "description": "locate the bug", "checkpoint": "read the file"},
    {"id": 2, "description": "fix the off-by-one", "checkpoint": "target passes"},
]


def test_planner_prompt_mentions_issue_and_files():
    msgs = prompts.render_planner_prompt(
        "mean() returns wrong value",
        "### numlib/mathutil.py\n```py\n...\n```",
        "- tests/*",
    )
    assert msgs[0]["role"] == "system"
    assert "planning a bug fix" in msgs[0]["content"]
    assert "mean() returns wrong value" in msgs[1]["content"]
    assert "numlib/mathutil.py" in msgs[1]["content"]
    assert "tests/*" in msgs[1]["content"]


def test_step_system_renders_plan_and_current_marker():
    sys_prompt = prompts.render_step_system(
        issue_text="mean() broken",
        plan=PLAN,
        step_id=2,
        total_steps=2,
        completed_block="1. locate the bug",
        context_block="file contents",
        max_output_chars=3000,
    )
    assert "your step is #2 of 2" in sys_prompt
    assert "<- CURRENT" in sys_prompt
    assert "do NOT redo" in sys_prompt
    assert "SUBMIT" in sys_prompt


def test_reinjection_includes_constraints_and_remaining():
    block = prompts.render_constraint_reinjection(
        issue_text="stack.pop raises IndexError",
        plan=PLAN,
        step_id=1,
        total_steps=2,
        completed=[],
        protected_paths=["tests/*"],
    )
    assert "stack.pop raises IndexError" in block
    assert "fix the off-by-one" in block  # remaining step listed
    assert "tests/*" in block  # protected restated
    assert "SUBMIT" in block
    # and for the LAST step, no remaining steps are claimed
    last = prompts.render_constraint_reinjection(
        issue_text="x",
        plan=PLAN,
        step_id=2,
        total_steps=2,
        completed=["1. locate the bug"],
        protected_paths=[],
    )
    assert "last step" in last


def test_issue_one_line_truncates():
    long_issue = "word " * 100
    one = prompts.issue_one_line(long_issue, width=50)
    assert len(one) <= 50
    assert one.endswith("…")


def test_parse_plan_json_from_core():
    from harness.core import _parse_plan_json

    good = json.dumps(
        {
            "analysis": "a",
            "plan": [
                {
                    "id": 1,
                    "description": "d1",
                    "checkpoint": "c1",
                    "files_hint": ["f.py"],
                },
            ],
        }
    )
    assert _parse_plan_json(good)[0]["description"] == "d1"
    assert _parse_plan_json("no json here") is None
    assert _parse_plan_json(json.dumps({"plan": []})) is None
    fenced = "```json\n" + good + "\n```"
    assert _parse_plan_json(fenced) is not None
