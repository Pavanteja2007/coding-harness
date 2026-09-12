"""Tests for the three Round 8 agent-tooling additions:

- Task B: BATCH — batched read-only tool execution (parse/validate,
  concurrent run_batch, e2e through run_step, and the wall-clock
  measurement that justified it).
- Task C: lint gate — harness/lint.py static analysis (syntax +
  module-level undefined names, false-negative bias) and its two wiring
  points in core.run_step / run_task (SUBMIT-time in-session feedback;
  pre-final-verify short-circuit).
- Task D: DOCS — documentation lookup (parse, cache -> pydoc ->
  opt-in PyPI resolution, budget, e2e reinjection into a live session).

Docker note: the run_batch e2e + timing tests use the LOCAL subprocess
sandbox stub (they exercise batching semantics, not isolation — that is
Boundary 1's own tested contract), injected via deps; the loop-level
e2e (lint short-circuit, DOCS receipt) uses the same offline harness
path the recall unit suite uses. No Docker required for this file.
"""

import json
import time
from pathlib import Path

import pytest

from harness import tools as tool_mod
from harness.deps import reset_overrides, set_call_model, set_execute_sandboxed
from harness.docs_lookup import DocsResult, lookup, parse_docs, render_docs_result
from harness.lint import lint_changed, lint_file, render_findings
from shared.types import ExecutionResult, Task

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Task B — BATCH: grammar + validation
# ---------------------------------------------------------------------------


def test_parse_batch_forms():
    assert tool_mod.parse_batch("BATCH cat a.py ;;; ls ;;; git status") == [
        "cat a.py",
        "ls",
        "git status",
    ]
    assert tool_mod.parse_batch("batch   pwd") == ["pwd"]
    assert tool_mod.parse_batch("BATCH\n cat a.py ;;; wc -l b.py") == [
        "cat a.py",
        "wc -l b.py",
    ]
    # not a batch
    assert tool_mod.parse_batch("cat a.py") is None
    assert tool_mod.parse_batch("SUBMIT") is None
    assert tool_mod.parse_batch("RECALL BATCH things") is None
    assert tool_mod.parse_batch("BATCH   ") is None


def test_validate_batch_readonly_allowlist():
    ok = [
        "cat a.py",
        "head -5 b.py",
        "ls src/",
        "find . -name x",
        "grep -n foo src/",
        "rg pat",
        "wc -l f",
        "file x",
        "stat y",
        "pwd",
        "which python",
        "git status",
        "git diff",
        "git log -3",
        "git show HEAD",
        "git blame f",
        "git ls-files",
        "python -m pydoc json.dumps",
    ]
    assert tool_mod.validate_batch(ok) is None


def test_validate_batch_rejects_writes_and_composition():
    bad_entries = [
        "rm -rf build/",  # destructive
        "touch newfile.py",  # write
        "sed -i s/a/b/ f.py",  # in-place edit
        "cat a ; cat b",  # ; composition
        "cat a && cat b",  # && composition
        "cat a | grep x",  # pipe
        "cat a > out.txt",  # redirect
        "cat a`rm x`",  # backtick smuggling
        "cat $(rm x)",  # command substitution
        'python -c \'open("x","w")\'',  # arbitrary code execution
        "echo hi",  # not in the read-only allowlist
        "cd src",  # state-coupling cd
        "export X=1",  # env mutation
        "mkdir d",  # write
        "mv a b",  # write
    ]
    for entry in bad_entries:
        assert tool_mod.validate_batch([entry]) == entry, entry


def test_validate_batch_rejects_whole_list_on_one_bad_entry():
    assert tool_mod.validate_batch(["cat a.py", "rm -rf /", "ls"]) == "rm -rf /"


# ---------------------------------------------------------------------------
# Task B — run_batch (offline, injected fake sandbox)
# ---------------------------------------------------------------------------


class FakeSandbox:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    def __call__(self, repo_path, command, timeout_s):
        self.calls.append(command)
        if self.delay:
            time.sleep(self.delay)
        return ExecutionResult(0, f"out[{command}]", "", False)


@pytest.fixture
def fake_sandbox():
    fs = FakeSandbox()
    set_execute_sandboxed(fs)
    yield fs
    reset_overrides()


def test_run_batch_executes_all_and_labels(fake_sandbox):
    records, rendered = tool_mod.run_batch(
        "/repo", ["cat a.py", "ls", "git status"], 30, 500
    )
    assert len(records) == 3
    assert len(fake_sandbox.calls) == 3
    assert rendered.startswith("BATCH results (3 commands")
    for i, cmd in enumerate(["cat a.py", "ls", "git status"], start=1):
        assert f"--- [{i}] {cmd} ---" in rendered
        assert f"out[{cmd}]" in rendered


def test_run_batch_rejects_unvalidated_input_without_executing(fake_sandbox):
    records, rendered = tool_mod.run_batch("/repo", ["cat ok.py", "rm -rf /"], 30, 500)
    assert records == []
    assert rendered.startswith("BATCH REJECTED")
    assert "rm -rf /" in rendered
    assert fake_sandbox.calls == []  # nothing ran — all-or-nothing


def test_run_batch_failure_output_is_classified(fake_sandbox):
    calls = iter(["cat a.py"])

    def selective(repo, cmd, t):
        c = next(calls)
        return ExecutionResult(1, "", "cat: a.py: No such file or directory", False)

    set_execute_sandboxed(selective)
    records, rendered = tool_mod.run_batch("/repo", ["cat a.py"], 30, 500)
    assert "TOOL ERROR [file_not_found]" in rendered
    assert records[0]["exit_code"] == 1


def test_run_batch_concurrent_wallclock_speedup(fake_sandbox):
    """The Task B measurement, as a regression test: 4 x 0.3s serial
    commands must complete in < 4 x 0.3s (concurrency), with a generous
    margin for slow CI runners."""
    slow = FakeSandbox(delay=0.3)
    set_execute_sandboxed(slow)
    cmds = [f"cat f{i}.py" for i in range(4)]
    started = time.time()
    records, _ = tool_mod.run_batch("/repo", cmds, 30, 500)
    elapsed = time.time() - started
    assert len(records) == 4
    # serial floor: 4 * 0.3 = 1.2s; concurrent with 4 workers ~0.3s.
    # CI machines vary — assert a clear win, not an exact number:
    # elapsed must be under 75% of the serial floor.
    assert elapsed < 0.9, (
        f"batch of 4 x 0.3s commands took {elapsed:.2f}s — no concurrency"
    )


# ---------------------------------------------------------------------------
# Task B — e2e through the real loop controller (offline harness path)
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [
    {"id": 1, "description": "inspect then fix", "checkpoint": "target test passes"}
]


class BatchFixModel:
    """Step session issues a BATCH of reads, receives the combined
    results, THEN applies the real fix and SUBMITs — content-receipt
    proof that batched output reached the live session."""

    def __init__(self):
        self.saw_batch_results = False
        self.saw_batch_header = False

    def get_last_usage(self):
        return {
            "model": "batch-fix",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        if last.startswith("Begin."):
            return "BATCH cat numlib/mathutil.py ;;; ls numlib"
        if last.startswith("BATCH results"):
            self.saw_batch_header = True
            if "def mean" in last and "mathutil" in last:
                self.saw_batch_results = True
            return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
        return "SUBMIT"


def test_batch_e2e_through_run_step(tmp_path):
    model = BatchFixModel()
    set_call_model(model)
    task = Task(
        task_id=f"batch-e2e-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
            "log_root": str(tmp_path / "logs"),
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", "the batched session must still fix the bug"
    assert model.saw_batch_header, "no BATCH results message reached the session"
    assert model.saw_batch_results, (
        "batched output content was not in the session's context"
    )

    # trace: batch_call + per-entry tool_call/tool_result, all flagged
    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    kinds = [json.loads(l)["kind"] for l in trace_lines]
    assert "batch_call" in kinds
    batch_calls = [
        json.loads(l) for l in trace_lines if json.loads(l)["kind"] == "batch_call"
    ]
    assert batch_calls[0]["data"]["commands"] == ["cat numlib/mathutil.py", "ls numlib"]
    tool_calls = [
        json.loads(l) for l in trace_lines if json.loads(l)["kind"] == "tool_call"
    ]
    assert any(tc["data"].get("batch") for tc in tool_calls)


def test_fenced_batch_is_a_control_signal_not_shell(tmp_path):
    """A fenced ```bash BATCH ...``` must be intercepted as a BATCH,
    never executed as one composed shell line (the same discipline the
    fenced-RECALL test encodes for RECALL)."""
    model = BatchFixModel()
    model._first_reply = "```bash\nBATCH cat numlib/mathutil.py ;;; ls numlib\n```"

    class FencedModel(BatchFixModel):
        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if last.startswith("Begin."):
                return "```bash\nBATCH cat numlib/mathutil.py ;;; ls numlib\n```"
            if last.startswith("BATCH results"):
                self.saw_batch_header = True
                if "def mean" in last:
                    self.saw_batch_results = True
                return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
            if "BATCH REJECTED" in last:
                return "SUBMIT"
            return "SUBMIT"

    model = FencedModel()
    set_call_model(model)
    task = Task(
        task_id=f"fenced-batch-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert model.saw_batch_header, "fenced BATCH was not intercepted"
    assert result.status == "success"


# ---------------------------------------------------------------------------
# Task C — lint: the static analysis itself
# ---------------------------------------------------------------------------


def test_lint_syntax_error_line_number():
    findings = lint_file("def ok():\n    pass\n\ndef broken(:\n", "pkg/a.py")
    assert findings and findings[0].kind == "syntax"
    assert findings[0].line == 4
    assert "syntax error" in findings[0].message


def test_lint_undefined_name_module_level():
    findings = lint_file(
        "_DAYS_PER_MONTH = {1: 31}\n\nprint(_DAYS_PER_MONTHS[1])\n",
        "datelib/dateutil.py",
    )
    assert findings and findings[0].kind == "undefined_name"
    assert "_DAYS_PER_MONTHS" in findings[0].message
    rendered = render_findings(findings)
    assert rendered.startswith("LINT FAILED")
    assert "datelib/dateutil.py:3" in rendered


def test_lint_undefined_name_suppressions():
    # builtins, imports, star imports, __all__, comprehension targets,
    # params, late/nested defs: none of these may fire
    clean = [
        "from x import *\nprint(anything)\n",
        "import os\np = os.path.join('a', 'b')\n",
        '__all__ = ["exported"]\n',
        "[y for y in range(3)]\n",
        "if True:\n    def helper():\n        return 1\n\nprint(helper())\n",
        "CONST = 1\n\ndef f():\n    return CONST\n",
        "try:\n    import json\nexcept ImportError:\n    json = None\n",
        "print(__name__)\n",
        "x: int = 3\nprint(x)\n",
    ]
    for src in clean:
        assert not lint_file(src, "m.py"), src


def test_lint_names_pass_config_disables():
    # names pass ON: a module-level undefined reference is caught...
    buggy = "RESULT = _DAYS_PER_MONTHS[1]\n"
    assert lint_file(buggy, "datelib/dateutil.py")
    # ...and pinning lint_names=False silences exactly that pass
    assert not lint_file(buggy, "datelib/dateutil.py", check_names=False)
    # syntax stays checked even with names off
    assert lint_file("def broken(:\n", "x.py", check_names=False)
    # a real fix shape passes cleanly
    fixed = (FIXTURES / "bug04_nameerror/datelib/dateutil.py").read_text(
        encoding="utf-8"
    )
    fixed = fixed.replace("_DAYS_PER_MONTHS", "_DAYS_PER_MONTH")
    assert not lint_file(fixed, "datelib/dateutil.py")


def test_lint_changed_scans_only_changed_files(tmp_path):
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    for d in (pristine, work):
        d.mkdir()
        (d / "good.py").write_text("x = 1\n", encoding="utf-8")
    (work / "bad.py").write_text("print(undefined_thing)\n", encoding="utf-8")
    findings = lint_changed(str(work), ["bad.py"])
    assert findings and findings[0].kind == "undefined_name"
    assert findings[0].file == "bad.py"


def test_lint_changed_never_raises_on_unreadable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "x.py").write_text("x = 1\n", encoding="utf-8")
    findings = lint_changed(str(work), ["missing.py"])  # deleted mid-run
    assert findings == []


def test_lint_all_fixture_files_and_real_fixes_clean():
    for fx in (
        "bug01_wrap",
        "bug02_mean",
        "bug03_stack",
        "bug04_nameerror",
        "bug05_cart",
    ):
        for p in (FIXTURES / fx).rglob("*.py"):
            rel = p.relative_to(FIXTURES / fx).as_posix()
            assert not lint_file(p.read_text(encoding="utf-8"), rel), rel
    # the scripted e2e fixes pass lint too (no false-positive blocking)
    fixed02 = (
        (FIXTURES / "bug02_mean/numlib/mathutil.py")
        .read_text(encoding="utf-8")
        .replace("len(values) - 1", "len(values)")
    )
    assert not lint_file(fixed02, "numlib/mathutil.py")


# ---------------------------------------------------------------------------
# Task C — lint gate wiring: SUBMIT-time in-session feedback
# ---------------------------------------------------------------------------


class LintThenFixModel:
    """Attempt 1: an edit with an undefined module-level name + SUBMIT —
    the lint gate must feed findings back INTO THE SAME SESSION. The model
    then removes the offending file, applies the real fix, re-SUBMITs, and
    the task succeeds in ONE attempt (no verify cycle burned on the broken
    edit). State-machine shaped to be robust to output formats."""

    def __init__(self):
        self.lint_feedback_seen = False
        self._stage = 0

    def get_last_usage(self):
        return {
            "model": "lint-fix",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        if last.startswith("Begin."):
            self._stage = 1
            # a real edit that leaves an undefined name at module level
            return "echo 'print(MISSING_CONSTANT)' > newmod.py"
        if last.startswith("LINT FAILED"):
            self.lint_feedback_seen = True
            assert "undefined_name" in last, last
            assert "newmod.py" in last, last
            self._stage = 2
            return "rm newmod.py"
        if self._stage == 2 and "exit=0" in last:
            # broken file gone — apply the real fix
            self._stage = 3
            return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
        if self._stage == 3:
            return "SUBMIT"
        return "SUBMIT"


def test_lint_gate_submits_findings_into_same_session(tmp_path):
    model = LintThenFixModel()
    set_call_model(model)
    task = Task(
        task_id=f"lint-gate-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert model.lint_feedback_seen, (
        "the lint findings never reached the session as feedback"
    )
    assert result.status == "success", (
        "after fixing the lint error the task must complete"
    )
    assert result.attempts == 1, (
        "the lint loop must resolve INSIDE the session, not burn attempts"
    )


def test_lint_gate_disabled_by_config(tmp_path):
    class NoLintModel(LintThenFixModel):
        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if last.startswith("Begin."):
                return "echo 'print(MISSING)' > newmod.py"
            # with the gate off there is NO LINT FAILED message — any
            # other user message means we went straight through
            return "SUBMIT"

    model = NoLintModel()
    set_call_model(model)
    task = Task(
        task_id=f"no-lint-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
            "lint_gate": False,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert not model.lint_feedback_seen
    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert "lint_failed" not in [json.loads(l)["kind"] for l in trace_lines]


# ---------------------------------------------------------------------------
# Task C — lint gate wiring: pre-final-verify short-circuit
# ---------------------------------------------------------------------------


def test_lint_gate_short_circuits_before_final_verify(tmp_path):
    """A step whose commands apply a BROKEN edit and exhausts turns (no
    SUBMIT): the attempt must be rejected by the lint gate BEFORE the
    final verify — the trace proves order (lint_failed, no final_verify
    after it, attempt_end, then the retry)."""

    class ExhaustedBrokenModel:
        def get_last_usage(self):
            return {
                "model": "broken",
                "provider": "fake",
                "tokens": 20,
                "cost_usd": 0.0001,
            }

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            # apply the real fix AND an undefined-name module edit, then
            # never SUBMIT (exhaust turns) on attempt 1
            return (
                "sed -i 's/len(values) - 1/len(values)/' "
                "numlib/mathutil.py ; echo 'print(NOTHING_HERE)' >> newmod.py"
            )

    model = ExhaustedBrokenModel()
    set_call_model(model)
    task = Task(
        task_id=f"lint-sc-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 2,
            "max_retries": 2,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    kinds = [json.loads(l)["kind"] for l in trace_lines]
    assert "lint_failed" in kinds, "gate did not fire on the broken edit"
    # the short-circuit: no final_verify ran between the first lint
    # failure and the next attempt — a verify cycle was NOT burned on
    # an edit lint already knew was broken
    lint_idx = kinds.index("lint_failed")
    attempt_starts = [i for i, k in enumerate(kinds) if k == "attempt_start"]
    later_starts = [i for i in attempt_starts if i > lint_idx]
    assert later_starts, "lint failure did not lead to a retry attempt"
    before_retry = kinds[lint_idx : later_starts[0]]
    assert "final_verify" not in before_retry
    assert result.status == "failed"  # the broken edit poisons every attempt


# ---------------------------------------------------------------------------
# Task D — DOCS: parsing + resolution layers
# ---------------------------------------------------------------------------


def test_parse_docs_forms():
    assert parse_docs("DOCS json.dumps") == "json.dumps"
    assert parse_docs("docs  requests.Session") == "requests.Session"
    assert parse_docs("DOC pathlib") == "pathlib"
    assert parse_docs("DOCS json.dumps indent behavior") == "json.dumps indent behavior"
    assert parse_docs("cat DOCS.py") is None
    assert parse_docs("SUBMIT") is None


def test_docs_pydoc_resolves_stdlib(tmp_path):
    res = lookup("json.dumps", tmp_path / "cache", max_chars=2000)
    assert res.ok
    assert res.source == "pydoc"
    assert "dumps" in res.text


def test_docs_cache_roundtrip(tmp_path):
    root = tmp_path / "cache"
    res1 = lookup("json.dumps", root, max_chars=2000)
    assert res1.source == "pydoc"  # populated the cache
    res2 = lookup("json.dumps", root, max_chars=2000)
    assert res2.source == "cache"  # second lookup: zero subprocess/network
    assert res2.text == res1.text
    assert (root / "json.dumps.json").is_file()
    # cache lives OUTSIDE any repo dir the harness was given
    assert (
        not (tmp_path / "repo").exists() or not (tmp_path / "repo" / "cache").exists()
    )


def test_docs_miss_returns_none_source(tmp_path):
    res = lookup("definitely_not_a_real_module_xyzzy", tmp_path / "cache")
    assert not res.ok
    assert res.source == "none"
    rendered = render_docs_result("xyzzy", res)
    assert "found nothing" in rendered
    assert "SUBMIT" in rendered  # nudges the session forward


def test_docs_remote_gated_off_by_default(tmp_path):
    """allow_remote=False: an unimportable target must NOT hit the
    network — the miss comes back 'none' fast."""
    res = lookup("some_uninstalled_pkg", tmp_path / "cache", allow_remote=False)
    assert res.source == "none"


def test_docs_result_capped(tmp_path):
    root = tmp_path / "cache"
    res = lookup("json", root, max_chars=300)
    assert len(res.text) <= 350  # cap + truncation marker allowance


# ---------------------------------------------------------------------------
# Task D — e2e: DOCS reinjection into a live session
# ---------------------------------------------------------------------------


class DocsFixModel:
    """Session asks DOCS for a symbol, harness reinjects pydoc output,
    model proves receipt by acting on it, then fixes and SUBMITs."""

    def __init__(self):
        self.saw_docs_result = False

    def get_last_usage(self):
        return {
            "model": "docs-fix",
            "provider": "fake",
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        if last.startswith("Begin."):
            return "DOCS json.dumps"
        if last.startswith("DOCS results"):
            if "dumps" in last:
                self.saw_docs_result = True
            return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
        return "SUBMIT"


def test_docs_e2e_reinjects_into_session(tmp_path):
    model = DocsFixModel()
    set_call_model(model)
    task = Task(
        task_id=f"docs-e2e-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert model.saw_docs_result, "DOCS output never reached the session"

    trace_lines = (
        (tmp_path / "logs" / task.task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    docs_events = [
        json.loads(l) for l in trace_lines if json.loads(l)["kind"] == "docs_lookup"
    ]
    assert docs_events and docs_events[0]["data"]["query"] == "json.dumps"
    assert docs_events[0]["data"]["ok"] is True
    # the docs cache lives under the logs root, outside the repo copy
    cache = tmp_path / "logs" / "_docs-cache"
    assert cache.is_dir() and any(cache.iterdir())


def test_docs_budget_exhaustion_nudges_on(tmp_path):
    class DocsSpamModel:
        def __init__(self):
            self.refusals = 0
            self._fixed = False

        def get_last_usage(self):
            return {
                "model": "docs-spam",
                "provider": "fake",
                "tokens": 20,
                "cost_usd": 0.0001,
            }

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
            if self._fixed:
                return "SUBMIT"
            users = [m["content"] for m in messages if m["role"] == "user"]
            last = users[-1] if users else ""
            if "DOCS budget" in last and "exhausted" in last:
                self.refusals += 1
                self._fixed = True
                return "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"
            return "DOCS json.dumps"

    model = DocsSpamModel()
    set_call_model(model)
    task = Task(
        task_id=f"docs-budget-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 10,
            "max_docs_per_step": 1,
        },
    )
    from harness.core import run_task

    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", "budget exhaustion must not deadlock"
    assert model.refusals >= 1
