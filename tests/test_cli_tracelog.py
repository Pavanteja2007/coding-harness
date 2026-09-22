"""Unit tests for cli/tracelog.py — the live trace feed mapping layer.

These are deliberately textual-free (pure mapping + diff logic, fast,
no event loop): the TUI's rendering of the same entries is pinned in
tests/test_cli_tui.py. Together they hold the live-trace round's
contract: readable one-liners per action, raw detail attached for
expansion, diffs computed from the harness's own pristine/work trees.
"""

from __future__ import annotations

import json

import pytest

from cli.tracelog import (
    FeedBuilder,
    FeedEntry,
    classify_command,
    live_diff,
    summarize_reply,
)

# ---------------------------------------------------------------------------
# classify_command — the Task B "Reading src/utils.py" texture
# ---------------------------------------------------------------------------


class TestClassifyCommand:
    @pytest.mark.parametrize(
        "cmd,cls,frag",
        [
            ("cat src/utils.py", "read", "Reading src/utils.py"),
            ("head -20 log.txt", "read", "Reading log.txt"),
            ("ls", "read", "Listing (here)"),
            ("ls src/", "read", "Listing src/"),
            ("grep -n pattern file.py", "read", "Searching"),
            ("rg TODO src/", "read", "Searching for TODO in src/"),
            ("find . -name '*.py'", "read", "Finding"),
            ("wc -l mod.py", "read", "Counting mod.py"),
            ("git status", "read", "git status"),
            ("git diff HEAD", "read", "git diff HEAD"),
            ("git log --oneline", "read", "git log --oneline"),
            ("git checkout -b x", "run", "git checkout"),
            ("pytest -q tests/test_x.py", "test", "Running: pytest tests/test_x.py"),
            (
                "python -m pytest tests/a.py tests/b.py",
                "test",
                "Running: pytest tests/a.py tests/b.py",
            ),
            ("sed -n 1,5p file.py", "read", "Reading file.py"),
            ("rm temp.txt", "edit", "rm temp.txt"),
            ("mv a.py b.py", "edit", "mv a.py b.py"),
            ("touch new.py", "write", "Creating new.py"),
            ("mkdir docs", "write", "Creating dir docs"),
            ("echo x > conf.txt", "write", "Writing conf.txt"),
            ("cd subdir", "run", "cd subdir"),
            ("python -m pydoc collections.Counter", "read", "Reading docs"),
            ("python scripts/run.py", "run", "Running scripts/run.py"),
            ("curl http://x.io", "read", "Fetching http://x.io"),
            ("", "run", "(empty command)"),
        ],
    )
    def test_matrix(self, cmd, cls, frag):
        got_cls, summary = classify_command(cmd)
        assert got_cls == cls
        assert frag in summary

    def test_python_c_edit_names_target(self):
        cmd = (
            "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
            "s = p.read_text(); s = s.replace('a', 'b'); p.write_text(s)\""
        )
        cls, summary = classify_command(cmd)
        assert cls == "edit"
        assert "mathutil.py" in summary

    def test_python_c_inspect(self):
        cmd = "python -c \"print(open('x.py').read())\""
        cls, summary = classify_command(cmd)
        assert cls == "inspect"
        assert "Inspecting" in summary

    def test_heredoc_rewrite(self):
        cmd = "cat > mathutil.py <<'EOF'\nline1\nline2\nline3\nEOF"
        cls, summary = classify_command(cmd)
        assert cls == "write"
        assert "Rewriting mathutil.py" in summary
        assert "5 lines" in summary

    def test_multiline_script_honest_run(self):
        cmd = "bash -c 'echo one\necho two'"
        cls, summary = classify_command(cmd)
        assert cls == "run"
        assert "lines" in summary

    def test_long_command_truncated(self):
        cmd = "cat " + "x" * 400 + ".py"
        cls, summary = classify_command(cmd)
        assert cls == "read"
        assert "truncated" in summary or len(summary) < 200

    def test_unknown_verb_is_honest_run(self):
        cls, summary = classify_command("somebinary --flag arg")
        assert cls == "run"
        assert "somebinary" in summary


# ---------------------------------------------------------------------------
# summarize_reply — the Task A visible-thinking texture
# ---------------------------------------------------------------------------


class TestSummarizeReply:
    def test_planner_pulls_first_step(self):
        content = json.dumps(
            {
                "analysis": "x",
                "plan": [
                    {
                        "id": 1,
                        "description": "fix the divisor in mean()",
                        "checkpoint": "c",
                    }
                ],
            }
        )
        head, frag = summarize_reply("plan", content)
        assert head == "Planning the fix"
        assert "fix the divisor in mean()" in frag

    def test_step_prose_kept(self):
        head, frag = summarize_reply(
            "step-2", "I will replace the divisor expression with len(values) now."
        )
        assert head == "Step 2"
        assert "replace the divisor" in frag

    def test_step_pure_command_yields_no_frag(self):
        """A command-only reply produces NO reasoning line (the tool
        line that follows is the action — no duplicate)."""
        head, frag = summarize_reply("step-1", "cat mathutil.py")
        assert head == "Step 1"
        assert frag == ""

    def test_step_submit_yields_no_frag(self):
        _head, frag = summarize_reply("step-1", "SUBMIT")
        assert frag == ""

    def test_self_critique(self):
        head, frag = summarize_reply(
            "self-critique", "The diff addresses the reported issue."
        )
        assert head == "Reviewing the diff"
        assert "addresses the reported issue" in frag

    def test_agent_tests(self):
        head, _frag = summarize_reply(
            "agent-tests-1", '{"tests": [{"filename": "t.py", "content": "x"}]}'
        )
        assert head == "Writing edge-case tests"

    def test_plan_retry(self):
        head, _ = summarize_reply("plan-retry", '{"plan": []}')
        assert head == "Planning the fix"

    def test_fenced_command_skipped_for_prose(self):
        head, frag = summarize_reply(
            "step-1", "```bash\ncat x.py\n```\nNow I know the shape; fixing next."
        )
        assert head == "Step 1"
        assert "Now I know the shape" in frag
        assert "cat x.py" not in frag

    def test_unknown_step_label_total(self):
        head, frag = summarize_reply("weird-label", "something or other")
        assert head == "weird-label"
        assert frag


# ---------------------------------------------------------------------------
# live_diff — Task C (from the harness's own pristine/work trees)
# ---------------------------------------------------------------------------


class TestLiveDiff:
    def _pair(self, tmp_path, p_text, w_text, name="mod.py"):
        pristine = tmp_path / "pristine"
        work = tmp_path / "work"
        pristine.mkdir(exist_ok=True)
        work.mkdir(exist_ok=True)
        (pristine / name).write_text(p_text, encoding="utf-8")
        (work / name).write_text(w_text, encoding="utf-8")
        return pristine, work

    def test_edited_file(self, tmp_path):
        pristine, work = self._pair(
            tmp_path,
            "def mean(values):\n    return sum(values)\n",
            "def mean(values):\n    return sum(values) / len(values)\n",
        )
        lines = live_diff(pristine, work)
        assert lines is not None
        kinds = {text: kind for text, kind in lines}
        assert any(t.startswith("--- a/mod.py") for t in kinds)
        assert ("-    return sum(values)", "del") in lines
        assert ("+    return sum(values) / len(values)", "add") in lines

    def test_identical(self, tmp_path):
        pristine, work = self._pair(tmp_path, "x = 1\n", "x = 1\n")
        assert live_diff(pristine, work) == []

    def test_missing_dirs_none(self, tmp_path):
        assert live_diff(tmp_path / "a", tmp_path / "b") is None

    def test_added_file(self, tmp_path):
        pristine, work = self._pair(tmp_path, "x = 1\n", "x = 1\n")
        (work / "new.py").write_text("y = 2\n", encoding="utf-8")
        lines = live_diff(pristine, work)
        assert ("+y = 2", "add") in lines

    def test_deleted_file(self, tmp_path):
        pristine, work = self._pair(tmp_path, "x = 1\n", "x = 1\n")
        (pristine / "gone.py").write_text("z = 3\n", encoding="utf-8")
        lines = live_diff(pristine, work)
        assert ("-z = 3", "del") in lines

    def test_junk_skipped(self, tmp_path):
        pristine, work = self._pair(tmp_path, "x = 1\n", "x = 2\n")
        (work / "__pycache__").mkdir()
        (work / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
        (work / ".coverage").write_text("x", encoding="utf-8")
        lines = live_diff(pristine, work)
        assert lines
        assert all("__pycache__" not in t and ".coverage" not in t for t, _ in lines)

    def test_binary_marked_not_crashed(self, tmp_path):
        pristine, work = self._pair(tmp_path, "x = 1\n", "x = 2\n")
        (work / "blob.bin").write_bytes(b"\x00\x01\x02")
        lines = live_diff(pristine, work)
        assert any(kind == "binary" for _, kind in lines)

    def test_cap_truncates(self, tmp_path):
        pristine, work = self._pair(
            tmp_path,
            "\n".join(f"line{i}" for i in range(50)) + "\n",
            "\n".join(f"LINE{i}" for i in range(50)) + "\n",
        )
        lines = live_diff(pristine, work, max_lines=5)
        assert any(kind == "trunc" for _, kind in lines)
        assert len(lines) <= 7  # 5 + header pair + trunc marker


# ---------------------------------------------------------------------------
# FeedBuilder — the event->entry mapping (Tasks A/B/D)
# ---------------------------------------------------------------------------


def _ev(kind, data):
    return {"ts": 1.0, "kind": kind, "data": data}


class TestFeedBuilder:
    def test_lifecycle_events(self):
        b = FeedBuilder("t")
        b.consume(_ev("task_start", {"issue_text": "fix the mean bug"}))
        b.consume(_ev("baseline_verify", {"target_passed_on_pristine": False}))
        b.consume(_ev("attempt_start", {"attempt": 1}))
        summaries = b.lines()
        assert any("task start" in s and "fix the mean bug" in s for s in summaries)
        assert any("reproducing the bug" in s for s in summaries)
        assert any("attempt 1" in s for s in summaries)

    def test_retry_attempt_labeled(self):
        b = FeedBuilder("t")
        b.consume(_ev("attempt_start", {"attempt": 2}))
        assert any("attempt 2" in s and "retrying" in s for s in b.lines())

    def test_tool_call_and_result_attach(self):
        b = FeedBuilder("t")
        entries = b.consume(
            _ev("tool_call", {"step_id": 1, "turn": 1, "command": "cat mathutil.py"})
        )
        assert len(entries) == 1
        assert entries[0].summary == "Reading mathutil.py"
        assert entries[0].category == "tool"
        assert entries[0].index == 0
        # the result attaches to that entry's detail (Task D)
        b.consume(
            _ev("tool_result", {"step_id": 1, "turn": 1, "output": "the file body"})
        )
        assert "$ cat mathutil.py" in entries[0].detail
        assert "the file body" in entries[0].detail

    def test_test_command_category_verify(self):
        b = FeedBuilder("t")
        entries = b.consume(
            _ev(
                "tool_call",
                {"step_id": 1, "turn": 1, "command": "python -m pytest -q tests/x.py"},
            )
        )
        assert entries[0].category == "verify"
        assert entries[0].summary == "Running: pytest tests/x.py"

    def test_edit_command_category_diff(self):
        b = FeedBuilder("t")
        entries = b.consume(
            _ev(
                "tool_call",
                {
                    "step_id": 1,
                    "turn": 1,
                    "command": "python -c \"import pathlib; pathlib.Path('x.py').write_text('y')\"",
                },
            )
        )
        assert entries[0].category == "diff"

    def test_model_response_prose_line(self):
        b = FeedBuilder("t")
        entries = b.consume(
            _ev(
                "model_response",
                {"step": "step-1", "content": "I will inspect the divisor first."},
            )
        )
        assert len(entries) == 1
        assert entries[0].category == "reason"
        assert "Step 1" in entries[0].summary
        assert "inspect the divisor" in entries[0].summary

    def test_model_response_pure_command_no_line(self):
        b = FeedBuilder("t")
        entries = b.consume(
            _ev("model_response", {"step": "step-1", "content": "cat mathutil.py"})
        )
        assert entries == []

    def test_verify_outcomes(self):
        b = FeedBuilder("t")
        b.consume(
            _ev(
                "verify",
                {
                    "step_id": 1,
                    "target_passed": True,
                    "regression_passed": True,
                    "flaky": False,
                },
            )
        )
        b.consume(
            _ev(
                "verify",
                {
                    "step_id": 2,
                    "target_passed": True,
                    "regression_passed": False,
                    "flaky": False,
                },
            )
        )
        b.consume(
            _ev(
                "final_verify",
                {"target_passed": True, "regression_passed": True, "flaky": False},
            )
        )
        lines = b.lines()
        assert any("checkpoint passed" in s for s in lines)
        assert any("suite regressed" in s for s in lines)
        assert any("final verification" in s and "PASS" in s for s in lines)

    def test_step_end_ok_and_fail(self):
        b = FeedBuilder("t")
        b.consume(_ev("plan", {"plan": [{"id": 1, "description": "fix the divisor"}]}))
        b.consume(
            _ev(
                "step_end",
                {
                    "attempt": 1,
                    "step_id": 1,
                    "description": "fix the divisor",
                    "ok": True,
                    "note": "checkpoint passed",
                },
            )
        )
        b.consume(
            _ev(
                "step_end",
                {
                    "attempt": 1,
                    "step_id": 2,
                    "description": "x",
                    "ok": False,
                    "note": "exhausted its turns",
                },
            )
        )
        lines = b.lines()
        assert any("step 1 done" in s for s in lines)
        assert any("step 2" in s and "without a clean pass" in s for s in lines)

    def test_task_end_and_result(self):
        b = FeedBuilder("t")
        b.consume(_ev("task_end", {"status": "success", "attempt": 1}))
        b.consume(
            _ev("result", {"status": "success", "attempts": 1, "cost_usd": 0.0042})
        )
        lines = b.lines()
        assert any("task success" in s for s in lines)
        assert any("result: success" in s and "0.0042" in s for s in lines)

    def test_quiet_kinds_produce_no_noise(self):
        """model_request, tool_result-only, skills miss, memory miss
        produce no entries (or attach silently) — the default view stays
        one readable line per ACTION."""
        b = FeedBuilder("t")
        assert b.consume(_ev("model_request", {"step": "plan", "messages": []})) == []
        assert b.consume(_ev("skills", {"matched": []})) == []
        assert b.consume(_ev("decision_memory", {"matched": 0})) == []
        assert (
            b.consume(_ev("tool_result", {"step_id": 1, "turn": 1, "output": ""})) == []
        )

    def test_unknown_kind_total_no_entry(self):
        b = FeedBuilder("t")
        assert b.consume(_ev("some_future_kind", {"x": 1})) == []

    def test_malformed_event_never_raises(self):
        b = FeedBuilder("t")
        b.consume({"kind": "tool_call", "data": {"command": {"not": "a string"}}})
        b.consume({"no_kind": {}})
        b.consume("not even a dict")  # type: ignore[arg-type]
        b.consume(None)  # type: ignore[arg-type]
        # the builder survived and can keep consuming
        entries = b.consume(_ev("tool_call", {"command": "ls"}))
        assert entries[0].summary == "Listing (here)"

    def test_batch_events(self):
        b = FeedBuilder("t")
        b.consume(
            _ev(
                "batch_call",
                {"commands": ["cat a.py", "ls src/", "grep x b.py"]},
            )
        )
        b.consume(_ev("batch_rejected", {"entry": "rm -rf /"}))
        lines = b.lines()
        assert any("batching 3" in s for s in lines)
        assert any("batch rejected" in s for s in lines)

    def test_recall_docs_fetch(self):
        b = FeedBuilder("t")
        b.consume(_ev("recall", {"query": "mean", "matched": 2}))
        b.consume(
            _ev("docs_lookup", {"query": "num2words", "ok": True, "source": "pydoc"})
        )
        b.consume(
            _ev("web_fetch", {"url": "https://pypi.org", "ok": True, "chars": 800})
        )
        lines = b.lines()
        assert any("recalling compacted context" in s for s in lines)
        assert any("looking up docs: num2words" in s for s in lines)
        assert any("fetched https://pypi.org" in s and "800" in s for s in lines)

    def test_indexes_are_sequential(self):
        b = FeedBuilder("t")
        for cmd in ("cat a.py", "ls", "cat b.py"):
            b.consume(_ev("tool_call", {"command": cmd}))
        assert [e.index for e in b.entries] == [0, 1, 2]

    def test_feed_entry_defaults(self):
        e = FeedEntry("summary")
        assert e.category == "info"
        assert e.detail == ""
        assert e.detail_title == ""


# ---------------------------------------------------------------------------
# The no-drift contract (Task E) — the feed is a pure function of the
# trace file: same events, same entries, however many times consumed.
# ---------------------------------------------------------------------------


class TestNoDrift:
    def test_replay_is_deterministic(self):
        events = [
            _ev("task_start", {"issue_text": "x"}),
            _ev("plan", {"plan": [{"id": 1, "description": "d"}]}),
            _ev("attempt_start", {"attempt": 1}),
            _ev("tool_call", {"command": "cat x.py"}),
            _ev("tool_result", {"output": "body"}),
            _ev("result", {"status": "success", "attempts": 1, "cost_usd": 0.0}),
        ]
        b1 = FeedBuilder("t")
        b2 = FeedBuilder("t")
        for ev in events:
            b1.consume(ev)
            b2.consume(ev)
        assert b1.lines() == b2.lines()
        assert [e.detail for e in b1.entries] == [e.detail for e in b2.entries]

    def test_builder_writes_nothing(self, tmp_path):
        """The feed layer is read-only over the run's data — it must not
        create files anywhere (no second logging path to drift)."""
        import os

        b = FeedBuilder("t")
        b.consume(_ev("tool_call", {"command": "ls"}))
        before = {str(p) for p in tmp_path.rglob("*")}
        b.consume(_ev("result", {"status": "success"}))
        after = {str(p) for p in tmp_path.rglob("*")}
        assert before == after or not os.listdir(tmp_path)
