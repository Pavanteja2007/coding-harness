"""End-to-end tests of harness.core.run_task against the fixture repos,
using the scripted fake model — these exercise the REAL loop controller,
REAL bash tools, REAL local-subprocess sandbox and verifier, and prove:
- verifier-gated completion (never the model's claim),
- valid state.json + full trace.jsonl per task,
- retries, stopping conditions, error paths.

The fake model "fixes" bugs with real bash commands (sed / python -c),
exactly like a real model would, so these tests exercise the whole stack.
"""
import json
from pathlib import Path

import pytest

from harness.core import run_task, _extract_command
from harness.deps import reset_overrides, set_call_model
from shared.types import Task
from tests.fake_model import ExplodingModel, ScriptedModel

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_overrides():
    reset_overrides()
    yield
    reset_overrides()


def make_task(tmp_path, fixture, issue, config=None):
    repo = FIXTURES / fixture
    cfg = {
        "test_command": "python -m pytest -q",
        "command_timeout_s": 60,
        "verify_timeout_s": 180,
        "max_step_turns": 8,
    }
    cfg.update(config or {})
    return Task(
        task_id=f"{fixture}-e2e-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(repo),
        issue_text=issue,
        config=cfg,
    )


# ---------------------------------------------------------------------------
# The 5 hand-picked, genuinely different bugs (Definition of Done)
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [{"id": 1, "description": "fix the bug in the target file",
                  "checkpoint": "target test passes"}]


def test_fix_bug01_wrap_boundary(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/if lines and current and len(current) == width:/if current:/' wrapwrap/textutil.py""",
            "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug01_wrap",
                     "wrap() drops the final line when it is shorter than width")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert result.attempts == 1
    assert result.verification is not None
    assert result.verification.target_test_passed
    assert result.verification.regression_passed
    assert result.diff and "textutil.py" in result.diff
    _assert_logs_complete(tmp_path, task.task_id)


def test_fix_bug02_mean_off_by_one(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
            "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean",
                     "mean() divides by len-1; should divide by len")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert result.diff and "len(values)" in result.diff
    _assert_logs_complete(tmp_path, task.task_id)


def test_fix_bug03_stack_missing_guard(tmp_path):
    commands = [
        "cat stacklib/stack.py",
        """python - <<'EOF'
import re
p = "stacklib/stack.py"
s = open(p).read()
s = s.replace(
    "        return self._items.pop()",
    "        if not self._items:\\n            raise StackEmptyError(\\"pop from empty stack\\")\\n        return self._items.pop()")
open(p, "w").write(s)
EOF""",
        "SUBMIT",
    ]
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={1: [commands]})
    set_call_model(model)
    task = make_task(tmp_path, "bug03_stack",
                     "pop() on empty stack raises IndexError; it should raise StackEmptyError")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    _assert_logs_complete(tmp_path, task.task_id)


def test_fix_bug04_nameerror(tmp_path):
    commands = [
        """sed -i 's/_DAYS_PER_MONTHS\\[month\\]/_DAYS_PER_MONTH[month]/' datelib/dateutil.py""",
        "SUBMIT",
    ]
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={1: [commands]})
    set_call_model(model)
    task = make_task(tmp_path, "bug04_nameerror",
                     "days_in_month() raises NameError: name '_DAYS_PER_MONTHS' is not defined")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    _assert_logs_complete(tmp_path, task.task_id)


def test_fix_bug05_mutable_default(tmp_path):
    clean_fix = [
        "cat cartlib/cart.py",
        """python - <<'EOF'
p = "cartlib/cart.py"
s = open(p).read()
old = "    if report is None:\\n        report = _DEFAULT_REPORT"
new = "    if report is None:\\n        report = []"
assert old in s
open(p, "w").write(s.replace(old, new))
EOF""",
        "SUBMIT",
    ]
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={1: [clean_fix]})
    set_call_model(model)
    task = make_task(tmp_path, "bug05_cart",
                     "price_report() leaks lines across calls because of a mutable default argument")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    _assert_logs_complete(tmp_path, task.task_id)


# ---------------------------------------------------------------------------
# Loop-controller behaviors (verifier gating, retries, stopping conditions)
# ---------------------------------------------------------------------------


def test_success_requires_verifier_never_model_claim(tmp_path):
    """Model claims SUBMIT without fixing anything -> task must FAIL (the
    verifier, not the model, decides success)."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["echo I fixed it", "SUBMIT"],
            ["echo trying again", "SUBMIT"],
            ["echo last try", "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug01_wrap", "wrap() drops short trailing line",
                     config={"max_retries": 3})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "failed"
    assert result.attempts == 3
    assert result.verification is not None
    assert result.verification.target_test_passed is False


def test_retry_recovers_after_failed_first_attempt(tmp_path):
    """First attempt breaks syntax; second attempt fixes it cleanly."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [
            ["echo 'def broken(:' > wrapwrap/textutil.py", "SUBMIT"],   # attempt 1: syntax error
            ["sed -i 's/if lines and current and len(current) == width:/if current:/' wrapwrap/textutil.py",
             "SUBMIT"],                                                  # attempt 2: real fix
        ],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug01_wrap", "wrap() drops short trailing line")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert result.attempts == 2
    state = _read_state(tmp_path, task.task_id)
    # completed_steps reset between attempts: only the (re-executed) step listed once
    assert state["completed_steps"] == ["1. fix the bug in the target file"]


def test_max_retries_stops_loop(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["echo nope", "SUBMIT"]] * 5,
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"max_retries": 2})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "failed"
    assert result.attempts == 2  # stopped at cap, not 5


def test_budget_cap_stops_loop(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["echo nope", "SUBMIT"]] * 10,
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"max_retries": 50, "budget_cap_usd": 0.0002})
    result = run_task(task, log_root=tmp_path / "logs")
    # each call costs 0.0001 -> stops early with a failure, well under 50
    assert result.status == "failed"
    assert result.attempts < 50


def test_wallclock_cap_returns_timeout(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["echo nope", "SUBMIT"]] * 10,
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"max_retries": 50, "max_wallclock_s": 0.05})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "timeout"


def test_passing_pristine_repo_short_circuits_without_model(tmp_path):
    """Target test already passes pre-fix -> success without any model call
    (verifier-gated on the pristine run, not a model claim)."""
    repo = tmp_path / "fixed_repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "from pkg.ok import f\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", encoding="utf-8")
    set_call_model(ExplodingModel())
    task = Task(task_id="prefixed-ok", repo_path=str(repo),
                issue_text="f() should return 1",
                config={"test_command": "python -m pytest -q",
                        "verify_timeout_s": 120})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert result.attempts == 0
    assert result.model_calls == []


def test_protected_path_blocks_edit(tmp_path):
    """Agent tries to fix the bug by editing the TEST (protected) — the
    editor's protected-path check must reject it and the task fails."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/test_pop_empty_raises_stackemptyerror/test_pop_empty_never/' tests/test_stack.py""",
            "SUBMIT"]] * 3,
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug03_stack",
                     "pop() should raise StackEmptyError on empty stack",
                     config={"protected_paths": ["tests/*"]})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "failed"
    # and the actual fixture repo was never mutated
    assert "StackEmptyError" in (FIXTURES / "bug03_stack" / "tests" / "test_stack.py").read_text(encoding="utf-8")


def test_original_repo_never_mutated(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""", "SUBMIT"]],
    })
    set_call_model(model)
    original = (FIXTURES / "bug02_mean" / "numlib" / "mathutil.py").read_text(encoding="utf-8")
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert (FIXTURES / "bug02_mean" / "numlib" / "mathutil.py").read_text(encoding="utf-8") == original


def test_task_result_contract_shape(tmp_path):
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""", "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator")
    result = run_task(task, log_root=tmp_path / "logs")
    # Boundary 3 shape
    assert result.task_id == task.task_id
    assert isinstance(result.attempts, int)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.model_calls, list)
    assert Path(result.log_path).exists()
    assert result.model_calls[0]["step"] == "plan"
    assert result.model_calls[1]["step"] == "step-1"


def test_error_status_when_planner_unparseable(tmp_path):
    class BadPlanner:
        def __call__(self, messages, **kwargs):
            return "I cannot produce a plan."

    set_call_model(BadPlanner())
    task = make_task(tmp_path, "bug01_wrap", "wrap() bug")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "error"


def test_command_extraction_variants():
    assert _extract_command("cat foo.py") == "cat foo.py"
    assert _extract_command("```bash\nsed -i 's/a/b/' x.py\n```") == "sed -i 's/a/b/' x.py"
    assert _extract_command("COMMAND: grep -n def x.py") == "grep -n def x.py"
    assert _extract_command("I think we should look at the file first.") is None


# ---------------------------------------------------------------------------
# Resume / checkpoint (Task A — mirrors Terminal 3's scheduler integration:
# a hard kill mid-run in a SUBPROCESS, a relaunch, and the REAL harness
# resuming from its last completed step out of the real — NOT archived —
# state.json and trace.jsonl)
# ---------------------------------------------------------------------------


def _run_driver(mode, task, logs, tmp_path):
    """Run tests/resume_driver.py in a child process; returns (rc, stdout)."""
    import subprocess
    import sys as _sys

    task_json = tmp_path / "task.json"
    result_json = tmp_path / f"result-{mode}.json"
    task_json.write_text(json.dumps({
        "task_id": task.task_id, "repo_path": task.repo_path,
        "issue_text": task.issue_text, "config": task.config,
    }), encoding="utf-8")
    proc = subprocess.run(
        [_sys.executable, str(Path(__file__).parent / "resume_driver.py"),
         mode, str(task_json), str(logs), str(result_json)],
        capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT_FOR_TESTS),
    )
    return proc.returncode, proc.stdout + proc.stderr


REPO_ROOT_FOR_TESTS = Path(__file__).resolve().parents[1]


def test_resume_after_hard_kill_mid_run(tmp_path):
    """Terminal 3's scenario through the REAL harness loop: the worker
    subprocess is hard-killed (os._exit — no cleanup) after step 1
    completed but before step 2 ran; the relaunch (config['resume']=True)
    must resume from the REAL state.json (not an archived copy), reuse
    the persisted plan, skip step 1, and finish step 2 from the surviving
    work/ edits."""
    logs = tmp_path / "logs"
    task = Task(
        task_id="resume-kill-mid-run",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={"test_command": "python -m pytest -q",
                "command_timeout_s": 60, "verify_timeout_s": 180,
                "max_step_turns": 8, "resume": True},
    )

    # -- launch 1: child is hard-killed inside step 2's first model call --
    rc, out = _run_driver("kill", task, logs, tmp_path)
    assert rc != 0, f"driver should have died hard, got rc=0: {out}"

    log_dir = logs / task.task_id
    state1 = json.loads((log_dir / "state.json").read_text(encoding="utf-8"))
    assert state1["completed_steps"] == ["1. mark the wrong denominator in numlib"]
    trace1_lines = (log_dir / "trace.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    kinds1 = [json.loads(l)["kind"] for l in trace1_lines]
    assert "step_end" in kinds1
    assert "task_end" not in kinds1, "killed run must not look finished"
    # step 1's PARTIAL fix survived in work/ (this is what resume builds on)
    assert "__FIX_ME__" in (log_dir / "work" / "numlib" /
                            "mathutil.py").read_text(encoding="utf-8")
    # nothing was archived mid-task; no result written by the killed run
    assert not [p for p in logs.iterdir() if ".old-" in p.name]

    # -- relaunch: resume from the real state ----------------------------
    rc, out = _run_driver("complete", task, logs, tmp_path)
    assert rc == 0, f"relaunch failed: {out}"
    result = json.loads((tmp_path / "result-complete.json").read_text(
        encoding="utf-8"))

    assert result["status"] == "success"
    assert result["attempts"] == 1, "resume continues the in-flight attempt"
    state2 = json.loads((log_dir / "state.json").read_text(encoding="utf-8"))
    assert state2["completed_steps"] == [
        "1. mark the wrong denominator in numlib",
        "2. replace the marker with the correct len() call",
    ]
    # REAL files were used, not archived copies:
    # - decisions show the resume note
    assert any("resumed from interrupted run" in d
               for d in state2["decisions"])
    # - trace.jsonl is appended to (pre-kill events + relaunch events)
    trace2_lines = (log_dir / "trace.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(trace2_lines) > len(trace1_lines)
    kinds2 = [json.loads(l)["kind"] for l in trace2_lines]
    assert "step_skipped_resume" in kinds2
    assert "plan_reused" in kinds2
    assert kinds2[:len(kinds1)] == kinds1[:len(kinds1)] or \
        json.loads(trace2_lines[0])["kind"] == "task_start"
    # - step 1's pre-crash edit was not rolled back to pristine and got
    #   finished by step 2 (marker replaced by the real fix)
    work_text = (log_dir / "work" / "numlib" / "mathutil.py").read_text(
        encoding="utf-8")
    assert "__FIX_ME__" not in work_text
    assert "len(values))" in work_text
    # - the original repo untouched throughout
    original = (FIXTURES / "bug02_mean" / "numlib" / "mathutil.py").read_text(
        encoding="utf-8")
    assert "len(values) - 1" in original
    # - still no archived dirs; one lineage of state.json
    assert not [p for p in logs.iterdir() if ".old-" in p.name]


def test_resume_disabled_relaunches_fresh(tmp_path):
    """Without resume=True, a relaunch archives the old dir and starts
    from scratch (the pre-existing by-design behavior — regression guard
    for _fresh_paths)."""
    logs = tmp_path / "logs"
    task = Task(
        task_id="no-resume",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={"test_command": "python -m pytest -q",
                "command_timeout_s": 60, "verify_timeout_s": 180,
                "max_step_turns": 8},
    )

    # first launch dies mid-run (resume flag absent -> off)
    rc, _ = _run_driver("kill", task, logs, tmp_path)
    assert rc != 0
    assert (logs / task.task_id / "state.json").exists()

    # relaunch WITHOUT resume: archives the stale dir, starts fresh
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""", "SUBMIT"]],
    })
    set_call_model(model)
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    archived = [p for p in logs.iterdir() if ".old-" in p.name]
    assert archived, "non-resume relaunch must archive the stale dir"
    state = json.loads((logs / task.task_id / "state.json").read_text(encoding="utf-8"))
    assert state["completed_steps"] == ["1. fix the bug in the target file"]
    assert state["plan"] == ["1. fix the bug in the target file"]


def test_resume_with_corrupted_state_starts_fresh(tmp_path):
    """A relaunch where state.json is unreadable must not crash the run:
    it degrades to a fresh start."""
    logs = tmp_path / "logs"
    logs.mkdir()
    d = logs / "corrupt"
    d.mkdir()
    (d / "state.json").write_text("{not valid json", encoding="utf-8")
    (d / "plan.json").write_text(json.dumps({
        "steps": [{"id": 1, "description": "x", "checkpoint": "y",
                   "files_hint": []}], "attempts": 1, "cost_usd": 0.0}),
        encoding="utf-8")
    task = Task(task_id="corrupt", repo_path=str(FIXTURES / "bug02_mean"),
                issue_text="mean() wrong denominator",
                config={"test_command": "python -m pytest -q",
                        "verify_timeout_s": 180, "resume": True})
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""", "SUBMIT"]],
    })
    set_call_model(model)
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    assert state["completed_steps"] == ["1. fix the bug in the target file"]


def test_resume_missing_copies_aborts_to_fresh(tmp_path):
    """state.json + plan.json exist but pristine/work were wiped: the run
    must abort the resume and start fresh rather than continue nonsense."""
    logs = tmp_path / "logs"
    logs.mkdir()
    d = logs / "halfgone"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({
        "task_id": "halfgone", "plan": ["1. x"], "completed_steps": ["1. x"],
        "files_touched": [], "decisions": [], "remaining_plan": [],
    }), encoding="utf-8")
    (d / "plan.json").write_text(json.dumps({
        "steps": [{"id": 1, "description": "x", "checkpoint": "y",
                   "files_hint": []}], "attempts": 1, "cost_usd": 0.0}),
        encoding="utf-8")
    task = Task(task_id="halfgone", repo_path=str(FIXTURES / "bug02_mean"),
                issue_text="mean() wrong denominator",
                config={"test_command": "python -m pytest -q",
                        "verify_timeout_s": 180, "resume": True})
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""", "SUBMIT"]],
    })
    set_call_model(model)
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    kinds = [json.loads(l)["kind"] for l in
             (d / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert "resume_aborted" in kinds
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    assert state["completed_steps"] == ["1. fix the bug in the target file"]
    assert (d / "pristine").is_dir() and (d / "work").is_dir()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_state(tmp_path, task_id):
    return json.loads(
        (tmp_path / "logs" / task_id / "state.json").read_text(encoding="utf-8"))


def _assert_logs_complete(tmp_path, task_id):
    """Definition-of-Done checks: valid state file + full trace, and
    verifier-gated success evidence in the trace."""
    state = _read_state(tmp_path, task_id)
    assert list(state.keys()) == [
        "task_id", "plan", "completed_steps", "files_touched",
        "decisions", "remaining_plan"]
    assert state["task_id"] == task_id
    assert state["files_touched"], "state must record touched files"

    trace_lines = (tmp_path / "logs" / task_id / "trace.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    kinds = [json.loads(l)["kind"] for l in trace_lines]
    for expected in ["task_start", "baseline_verify", "plan", "attempt_start",
                     "model_request", "model_response", "task_end", "result"]:
        assert expected in kinds, f"trace missing {expected}: {kinds}"
    # every model_request is followed by its response (full traceability)
    for i, kind in enumerate(kinds):
        if kind == "model_request":
            assert kinds[i + 1] == "model_response"
