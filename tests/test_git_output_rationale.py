"""Tests for execution.git_output and execution.rationale.

git_output tests run against real git (host git, not Docker — git plumbing
is metadata manipulation; see git_output.py docstring). rationale tests
build synthetic trace.jsonl/state.json dirs in tmp_path and check the
paragraph is grounded in them.
"""
import json
from pathlib import Path

import pytest

from execution import git_output as go
from execution import rationale as ra


# ---------------------------------------------------------------------------
# git_output
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basics(self):
        assert go._slugify("Fix the pop() crash on empty stack!") == \
            "fix-the-pop-crash-on-empty-stack"
        assert go._slugify("  Spaces  &  Symbols!!  ") == "spaces-symbols"
        assert go._slugify("") == "fix"

    def test_max_len(self):
        assert len(go._slugify("x" * 200)) <= go.SLUG_MAX


class TestCommitMessage:
    def test_shape(self):
        msg = go.commit_message_from(
            "pop() on an empty stack raises IndexError instead of "
            "StackEmptyError. Please fix.",
            ["stacklib/stack.py"],
            verification_summary="target test passed; full suite green",
        )
        lines = msg.splitlines()
        assert lines[0].startswith("[fix] ")
        assert "What changed:" in msg
        assert "- stacklib/stack.py" in msg
        assert "Verification:" in msg

    def test_degrades_without_extras(self):
        msg = go.commit_message_from("boom", [])
        assert msg.splitlines()[0].startswith("[fix]")


class TestPRDescription:
    def test_contains_sections(self):
        desc = go.pr_description_from(
            issue_text="The widget crashes.",
            changed_files=["a.py", "b.py"],
            diff="--- a.py\n+++ b.py\n@@\n-x\n+y",
            verification_summary="all tests pass",
            rationale="The bug was an off-by-one.",
        )
        for section in ("## Problem", "## What was wrong", "## Changes",
                        "## Verification", "## Diff"):
            assert section in desc
        assert "```diff" in desc
        assert "- `a.py`" in desc

    def test_degrades_gracefully(self):
        desc = go.pr_description_from("issue", [], None, None, None)
        assert "## Problem" in desc
        assert "## Diff" not in desc


class TestProduceGitOutput:
    def test_end_to_end_fresh_repo(self, tmp_path):
        # pristine dir: pre-fix state; work dir: post-fix state.
        pristine = tmp_path / "pristine"
        work = tmp_path / "work"
        for d, bug in ((pristine, True), (work, False)):
            d.mkdir()
            (d / "stacklib").mkdir()
            (d / "stacklib" / "stack.py").write_text(
                "def pop(stack):\n    " +
                ("return stack.pop()  # IndexError on empty" if bug
                 else "def pop(stack):\n    if not stack:\n        raise StackEmptyError\n    return stack.pop()"),
                encoding="utf-8")
        out = go.produce_git_output(
            str(work), "pop() raises the wrong exception",
            ["stacklib/stack.py"], diff="…", verification_summary="green",
            rationale="pop() lacked the empty check.",
            pristine_dir=str(pristine),
        )
        assert out["branch"].startswith("harness/fix-")
        assert len(out["commit_sha"]) == 40
        assert out["commit_message"].startswith("[fix]")
        assert "## Problem" in out["pr_description"]

        # Git state checks: two commits (pristine + fix), fix diff correct.
        import subprocess
        def git(*a):
            return subprocess.run(
                ["git", "-C", str(work), *a], capture_output=True,
                text=True, check=True).stdout.strip()
        assert git("rev-list", "--count", "HEAD") == "2"
        names = git("diff", "--name-only", "HEAD~1", "HEAD")
        assert names == "stacklib/stack.py"

    def test_existing_git_repo_branches_not_commits_main(self, tmp_path):
        # Work dir that IS a git repo: fix must land on a NEW branch, and
        # the branch the user was on must NOT have the fix commit.
        work = tmp_path / "repo"
        work.mkdir()
        import subprocess
        def git(*a):
            return subprocess.run(
                ["git", "-C", str(work)] + list(a), capture_output=True,
                text=True, check=True).stdout.strip()
        git("init", "-q")
        (work / "main.txt").write_text("original\n", encoding="utf-8")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "-m", "base")
        original_branch = git("branch", "--show-current")
        (work / "main.txt").write_text("fixed\n", encoding="utf-8")

        out = go.produce_git_output(
            str(work), "main.txt content wrong", ["main.txt"])
        assert git("branch", "--show-current") == out["branch"]
        # The original branch must be untouched.
        assert git("show", f"{original_branch}:main.txt") == "original"

    def test_branch_name_collision_appends_suffix(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        (work / "f.txt").write_text("v1\n", encoding="utf-8")
        out1 = go.produce_git_output(str(work), "fix one", ["f.txt"])
        # A rerun with NEW changes wants the same slug branch name.
        (work / "f.txt").write_text("v2\n", encoding="utf-8")
        out2 = go.branch_and_commit(str(work), "[fix] fix one", None)
        assert out2[0] != out1["branch"]
        assert out2[0].startswith("harness/fix-fix-one")


# ---------------------------------------------------------------------------
# rationale
# ---------------------------------------------------------------------------

def _write_trace(log_dir: Path, events, state=None):
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    if state is not None:
        (log_dir / "state.json").write_text(
            json.dumps(state), encoding="utf-8")


class TestBuildRationale:
    def test_success_story(self, tmp_path):
        events = [
            {"ts": 1, "kind": "task_start", "data": {
                "task_id": "t1", "issue_text": "pop() raises wrong error."}},
            {"ts": 2, "kind": "baseline_verify", "data": {
                "target_passed_on_pristine": False, "flaky": False,
                "raw": "FAILED tests/test_stack.py::test_pop_empty - "
                       "___ test_pop_empty_raises ___\nE AssertionError: boom"}},
            {"ts": 3, "kind": "plan", "data": {"plan": [
                {"id": 1, "description": "add empty check"}]}},
            {"ts": 4, "kind": "attempt_start", "data": {"attempt": 1}},
            {"ts": 5, "kind": "final_verify", "data": {
                "target_passed": True, "regression_passed": True}},
            {"ts": 6, "kind": "task_end", "data": {"status": "success"}},
        ]
        state = {
            "task_id": "t1", "plan": ["1. add empty check"],
            "completed_steps": ["1. add empty check"],
            "files_touched": ["stacklib/stack.py"],
            "decisions": ["added explicit empty check before pop()"],
            "remaining_plan": [],
        }
        _write_trace(tmp_path, events, state)
        text = ra.build_rationale(str(tmp_path))
        assert "test_pop_empty" in text
        assert "stacklib/stack.py" in text
        assert "empty check" in text
        assert "verified" in text.lower()
        assert text.count("\n") == 0  # one paragraph, no line breaks

    def test_failed_story(self, tmp_path):
        events = [
            {"ts": 1, "kind": "task_start", "data": {
                "task_id": "t2", "issue_text": "bug X"}},
            {"ts": 2, "kind": "task_end", "data": {"status": "failed"}},
        ]
        _write_trace(tmp_path, events, {"task_id": "t2"})
        text = ra.build_rationale(str(tmp_path))
        assert "without a verified fix" in text

    def test_empty_trace_returns_empty(self, tmp_path):
        assert ra.build_rationale(str(tmp_path)) == ""

    def test_missing_log_dir_returns_empty(self, tmp_path):
        assert ra.build_rationale(str(tmp_path / "nope")) == ""


class TestRationaleHelpers:
    def test_first_failed_assertion_from_pytest_output(self):
        raw = "=== FAILURES ===\n____ test_add ____\nE assert 2 == 3\n"
        assert ra._first_failed_assertion(raw) == "test_add"

    def test_failed_marker_format(self):
        raw = "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
        assert "test_y" in (ra._first_failed_assertion(raw) or "")

    def test_error_excerpt_e_line(self):
        raw = "E AssertionError: expected StackEmptyError, got IndexError\n"
        assert "expected StackEmptyError" in ra._error_excerpt(raw)

    def test_issue_text_override_wins(self, tmp_path):
        events = [
            {"ts": 1, "kind": "task_start", "data": {
                "task_id": "t3", "issue_text": "trace version"}},
            {"ts": 2, "kind": "task_end", "data": {"status": "success"}},
        ]
        _write_trace(tmp_path, events)
        text = ra.build_rationale(str(tmp_path), issue_text="override version")
        assert "override version" in text
        assert "trace version" not in text
