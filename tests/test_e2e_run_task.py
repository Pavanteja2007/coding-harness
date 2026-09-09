"""End-to-end tests of harness.core.run_task against the fixture repos,
using the scripted fake model — these exercise the REAL loop controller,
REAL bash tools, REAL Docker sandbox and verifier (execution.sandbox /
execution.verify via deps.py auto-resolution; the local subprocess stub is
fallback-only), and prove:
- verifier-gated completion (never the model's claim),
- valid state.json + full trace.jsonl per task,
- retries, stopping conditions, error paths.

The fake model "fixes" bugs with real bash commands (sed / python -c),
exactly like a real model would, so these tests exercise the whole stack.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness.core import run_task, _extract_command
from harness.deps import reset_overrides, set_call_model
from shared.types import Task
from tests.fake_model import ExplodingModel, ScriptedModel

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_up() -> bool:
    """Daemon reachable? (mirrors tests/test_sandbox.py's gate)"""
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


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
# Product-grade output on verified success (Round 3 Task C — spec items
# 26/29: git branch/commit/PR description + rationale paragraph, wired
# into run_task; real git, real fixture fix through the real Docker stack)
# ---------------------------------------------------------------------------


@requires_docker
def test_success_produces_git_output_and_rationale(tmp_path):
    """A verified fix must produce, in the PRIVATE work copy: a
    harness/fix-* branch whose HEAD diff vs the pristine first commit IS
    the fix; a [fix]-convention commit message; a PR description; plus
    logs/{task_id}/rationale.md grounded in the trace and a git.json
    record. The ORIGINAL repo must remain a non-repo throughout."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
            "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() divides by len-1; should divide by len")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"

    log_dir = tmp_path / "logs" / task.task_id
    work = log_dir / "work"

    # --- git-native output in the work copy ---
    git_json = json.loads((log_dir / "git.json").read_text(encoding="utf-8"))
    assert git_json["branch"].startswith("harness/fix-")
    assert git_json["commit_sha"]
    assert git_json["commit_message"].startswith("[fix] ")
    assert "len(values)" in git_json["commit_message"] or \
        "mean()" in git_json["commit_message"] or \
        "fix" in git_json["commit_message"]
    assert "## Problem" in git_json["pr_description"]
    assert "## Verification" in git_json["pr_description"]
    assert "What was wrong" in git_json["pr_description"]

    # the branch exists in the work copy's git repo, HEAD == recorded sha
    def git(*args):
        return subprocess.run(["git", "-C", str(work), *args],
                              capture_output=True, text=True, timeout=60)
    assert git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == git_json["branch"]
    assert git("rev-parse", "HEAD").stdout.strip() == git_json["commit_sha"]
    # two commits: pristine state, then the fix
    shas = git("rev-list", "HEAD").stdout.split()
    assert len(shas) == 2
    # the fix commit's diff IS the fix (the whole point of the pristine
    # first commit)
    fix_diff = git("show", "--format=", "HEAD").stdout
    assert "mathutil.py" in fix_diff
    assert "-    return sum(values) / (len(values) - 1)" in fix_diff
    assert "+    return sum(values) / (len(values))" in fix_diff
    # author is the harness bot, never the user's global identity
    assert "harness-bot" in git("log", "-1", "--format=%an <%ae>").stdout

    # --- rationale ---
    rationale = (log_dir / "rationale.md").read_text(encoding="utf-8")
    assert "mathutil.py" in rationale
    assert "verified" in rationale.lower()

    # --- trace carries both events ---
    kinds = [json.loads(l)["kind"] for l in
             (log_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert "rationale" in kinds
    assert "git_output" in kinds
    # rationale is written after task_end (verdict needs the terminal status)
    assert kinds.index("task_end") < kinds.index("rationale")

    # --- the original repo was never touched / never became a repo ---
    assert not (FIXTURES / "bug02_mean" / ".git").exists()


@requires_docker
def test_git_output_degrades_gracefully_when_disabled(tmp_path):
    """git_output=False must not affect the verified fix: no branch is
    created (work/ stays a non-repo) and no rationale.md is written when
    rationale_log=False; the result itself is unchanged."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["""sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
            "SUBMIT"]],
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"git_output": False, "rationale_log": False})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    log_dir = tmp_path / "logs" / task.task_id
    assert not (log_dir / "git.json").exists()
    assert not (log_dir / "rationale.md").exists()
    assert not (log_dir / "work" / ".git").exists()
    kinds = [json.loads(l)["kind"] for l in
             (log_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    assert "git_output" not in kinds and "rationale" not in kinds


@requires_docker
def test_failed_task_gets_rationale_but_no_git_output(tmp_path):
    """Unverified fix: rationale paragraph is still written (valuable on
    failures), but NEVER a branch/commit/PR description — git output is
    gated on verification by construction."""
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [["echo nope", "SUBMIT"]] * 3,
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"max_retries": 2})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "failed"
    log_dir = tmp_path / "logs" / task.task_id
    assert (log_dir / "rationale.md").exists()
    assert "without a verified fix" in (log_dir / "rationale.md").read_text(
        encoding="utf-8")
    assert not (log_dir / "git.json").exists()
    assert not (log_dir / "work" / ".git").exists()


# ---------------------------------------------------------------------------
# Approval mode end-to-end through the REAL stack (Round 3 Task D): the
# gate lives in Terminal 3's worker, wrapping Boundary-3 run_task; this
# drives scheduler -> worker subprocess -> real harness -> file protocol
# ---------------------------------------------------------------------------


def _write_scripted_spec(path: Path, fixture_issue: str, command: str) -> None:
    """Write the scripted-model JSON spec the worker subprocess will use
    (env var HARNESS_SCRIPTED_MODEL -> harness.deps -> ScriptedFileModel).
    """
    spec = {
        "plan": [{"id": 1, "description": "fix the bug in the target file",
                  "checkpoint": "target test passes", "files_hint": []}],
        "scripts": {"1": [[command, "SUBMIT"]]},
    }
    path.write_text(json.dumps(spec), encoding="utf-8")


def _approval_gate_dir(logs_root: Path, task_id: str) -> Path:
    # scheduler pins resume_dir = logs_root/{task_id}.runtime; worker puts
    # the approval gate at runtime_dir/approval
    return logs_root / f"{task_id}.runtime" / "approval"


@requires_docker
def test_approval_mode_blocks_until_decision_then_applies(tmp_path):
    """LIVE approval-mode wiring (not code-reading): a real Scheduler run
    with approval='require' where the worker runs the REAL harness on a
    REAL fixture fix. The worker must write request.json with the REAL
    verified diff and BLOCK; an external approver thread writes
    decision.json (approve); the worker proceeds and the task finishes
    'success' with the diff applied. Proves the harness <-> approval
    protocol compose end-to-end across processes."""
    import threading

    from runtime import approval as ap
    from runtime.scheduler import Scheduler

    logs_root = tmp_path / "logs"
    task_id = "approval-e2e"
    spec_path = tmp_path / "model_spec.json"
    _write_scripted_spec(
        spec_path, "mean() wrong denominator",
        """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""")

    task = Task(
        task_id=task_id,
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
            "approval": "require",
            "approval_timeout_s": 120,
            # A worker blocked in the approval gate legitimately stops
            # touching state.json; without this the scheduler's hang
            # check (default stale threshold 30s) kills it mid-gate.
            # (Same pattern runtime/AGENTS.md documents for long model
            # calls — see the INTERFACES.md Change Log note this test
            # motivated.)
            "hang_heartbeat_stale_s": 300,
            "crash_retries": 0,
            "resume": False,
        },
    )

    # The scheduler spawns `python -m runtime.worker` with the CURRENT
    # env; set the scripted-model hook so the worker's harness uses it.
    os.environ["HARNESS_SCRIPTED_MODEL"] = str(spec_path)
    try:
        approver_thread = threading.Thread(
            target=_approver, args=(_approval_gate_dir(logs_root, task_id), True),
            daemon=True)
        approver_thread.start()

        sched = Scheduler(concurrency=1, logs_root=str(logs_root),
                          run_id="approval-e2e-run")
        results = sched.run([task])
    finally:
        os.environ.pop("HARNESS_SCRIPTED_MODEL", None)
        approver_thread.join(timeout=30)

    result = results[task_id]
    assert result.status == "success", f"approval e2e failed: {result.status}"
    assert result.diff and "len(values))" in result.diff

    # the request the human approved carried the REAL verified diff
    gate = _approval_gate_dir(logs_root, task_id)
    request = json.loads((gate / "request.json").read_text(encoding="utf-8"))
    assert request["task_id"] == task_id
    assert "len(values))" in request["diff"]
    assert "mean()" in request["issue_text"]
    # protocol audit trail: requested -> approved
    review = [json.loads(l) for l in
              (gate / "review.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e["event"] for e in review] == ["requested", "approved"]

    # worker events journal proves the gate sequence
    events = [json.loads(l) for l in (logs_root / f"{task_id}.runtime" / "events.jsonl"
                                      ).read_text(encoding="utf-8").splitlines() if l.strip()]
    seq = [e["event"] for e in events]
    assert "approval_wait" in seq
    assert "approval_granted" in seq
    assert seq.index("approval_wait") < seq.index("approval_granted") < \
        seq.index("worker_finish")

    # git output ran too (Task C composes with approval mode): the work
    # copy got its branch AFTER approval was granted
    git_json = json.loads((logs_root / task_id / "git.json").read_text(
        encoding="utf-8"))
    assert git_json["branch"].startswith("harness/fix-")
    trace_kinds = [json.loads(l)["kind"] for l in
                   (logs_root / task_id / "trace.jsonl").read_text(
                       encoding="utf-8").strip().splitlines()]
    assert "git_output" in trace_kinds


def _approver(gate_dir: Path, approve: bool, timeout_s: float = 120.0) -> None:
    """External approver: wait for request.json, then write the decision."""
    from runtime import approval as ap

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if (gate_dir / "request.json").exists():
            time.sleep(0.3)  # let the worker settle into its block loop
            ap.decide(str(gate_dir), approve=approve)
            return
        time.sleep(0.2)


@requires_docker
def test_approval_reject_blocks_diff(tmp_path):
    """REJECT path live: the worker's success result is downgraded to
    'failed' with diff stripped — the unapproved fix is never applied."""
    import threading

    from runtime.scheduler import Scheduler

    logs_root = tmp_path / "logs"
    task_id = "approval-reject-e2e"
    spec_path = tmp_path / "model_spec.json"
    _write_scripted_spec(
        spec_path, "mean() wrong denominator",
        """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""")

    task = Task(
        task_id=task_id,
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 8,
            "approval": "require",
            "approval_timeout_s": 120,
            "hang_heartbeat_stale_s": 300,  # see approve-path test note
            "crash_retries": 0,
            "resume": False,
        },
    )

    os.environ["HARNESS_SCRIPTED_MODEL"] = str(spec_path)
    try:
        approver_thread = threading.Thread(
            target=_approver, args=(_approval_gate_dir(logs_root, task_id), False),
            daemon=True)
        approver_thread.start()
        sched = Scheduler(concurrency=1, logs_root=str(logs_root),
                          run_id="approval-reject-run")
        results = sched.run([task])
    finally:
        os.environ.pop("HARNESS_SCRIPTED_MODEL", None)
        approver_thread.join(timeout=30)

    result = results[task_id]
    assert result.status == "failed"  # rejected -> failed, diff stripped
    assert result.diff is None

    gate = _approval_gate_dir(logs_root, task_id)
    review = [json.loads(l) for l in
              (gate / "review.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e["event"] for e in review] == ["requested", "rejected"]


# ---------------------------------------------------------------------------
# Round 5 — reversible compaction, closed out (RECALL reinjection) + the
# exhausted-turns state fix (T3's cli-real-smoke flag)
# ---------------------------------------------------------------------------


def test_recall_reinjects_step1_detail_into_step2_session(tmp_path):
    """Spec item 13, the real thing: step 1's session observes a detail (a
    distinctive token in tool output) that is NOT carried into step 2's
    fresh session by design (compaction). Step 2 RECALLs it; the harness
    must re-inject the matching trace entry into step 2's LIVE context.
    The fake model proves receipt content-wise: it only finishes the step
    after the recalled token appears in its own message list."""
    marker = "HARNESS_RECALL_MARKER_XYZ"

    class RecallAwareModel:
        """Serves the plan; step 1 prints the marker via bash; step 2
        RECALLs it and asserts (by reacting to context) it arrived."""

        def __init__(self):
            self.saw_recalled_marker = False
            self.recall_replies_seen = []

        def get_last_usage(self):
            return {"model": "recall-aware", "provider": "fake",
                    "tokens": 20, "cost_usd": 0.0001}

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages
                           if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": [
                    {"id": 1, "description": "inspect the failing function",
                     "checkpoint": "marker printed", "files_hint": []},
                    {"id": 2, "description": "fix the denominator",
                     "checkpoint": "target test passes",
                     "files_hint": ["numlib/mathutil.py"]},
                ]})
            if "your step is #1 of" in system:
                if not getattr(self, "_s1", None):
                    # a command whose OUTPUT carries the marker (echo to
                    # stdout — observed by the session, gone after it)
                    self._s1 = [f"echo {marker} in mathutil.py", "SUBMIT"]
                return self._s1.pop(0)
            if "your step is #2 of" in system:
                # Only the FIRST call of this session gets the RECALL
                # script; afterwards we react to context (below).
                if not getattr(self, "_s2_started", False):
                    self._s2_started = True
                    return f"RECALL {marker}"
                users = [m["content"] for m in messages
                         if m["role"] == "user"]
                context = "\n".join(users)
                if marker in context:
                    # the recalled entry reached the live session — proof
                    self.saw_recalled_marker = True
                    if "RECALL results" in context:
                        self.recall_replies_seen.append(context)
                    return """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"""
                return "SUBMIT"
            return "SUBMIT"

    model = RecallAwareModel()
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() divides by len-1; should divide by len")
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", (
        "step 2 must complete after recalling step 1's detail")
    assert model.saw_recalled_marker, (
        "the RECALLed trace entry never reached step 2's session context")
    assert model.recall_replies_seen, (
        "step 2 never received a 'RECALL results' reply from the harness")

    # the trace records the recall round-trip
    kinds = [json.loads(l)["kind"] for l in
             (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
                 encoding="utf-8").strip().splitlines()]
    assert "recall" in kinds
    recall_events = [json.loads(l) for l in
                     (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
                         encoding="utf-8").strip().splitlines()
                     if json.loads(l)["kind"] == "recall"]
    assert recall_events[0]["data"]["query"] == marker
    assert recall_events[0]["data"]["matched"] >= 1


def test_recall_budget_exhaustion_nudges_back_to_bash(tmp_path):
    """max_recalls_per_step is a real budget: after it's spent, further
    RECALLs get a refusal that names bash/SUBMIT — the step must still be
    able to finish (no deadlock on the escape hatch)."""

    class RecallSpamModel:
        def __init__(self):
            self.refusals_seen = 0

        def get_last_usage(self):
            return {"model": "recall-spam", "provider": "fake",
                    "tokens": 20, "cost_usd": 0.0001}

        def __call__(self, messages, **kwargs):
            system = next((m["content"] for m in messages
                           if m["role"] == "system"), "")
            if "planning a bug fix" in system:
                return json.dumps({"analysis": "scripted", "plan": [
                    {"id": 1, "description": "fix the denominator",
                     "checkpoint": "target test passes",
                     "files_hint": ["numlib/mathutil.py"]},
                ]})
            users = [m["content"] for m in messages if m["role"] == "user"]
            last_user = users[-1] if users else ""
            if "RECALL budget" in last_user and "exhausted" in last_user:
                self.refusals_seen += 1
                return """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"""
            return "RECALL anything"
        # spam RECALL forever; the refusal must convert it into progress

    model = RecallSpamModel()
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() wrong denominator",
                     config={"max_recalls_per_step": 2})
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success"
    assert model.refusals_seen >= 1


def test_verified_success_state_complete_after_exhausted_turns(tmp_path):
    """The cli-real-smoke bug (T3's Change Log flag, root-caused Round 5):
    a step whose commands ALREADY applied the fix but which never got to
    SUBMIT (turn budget exhausted) used to leave completed_steps: [] on a
    VERIFIED success — `harness status` rendered 0/1. Now every step that
    RAN in the verified attempt is recorded complete."""
    # fill all turns with commands; the sed fix runs on turn 0 but no
    # SUBMIT ever arrives
    filler = ["cat numlib/mathutil.py"] * 10
    model = ScriptedModel(plan=ONE_STEP_PLAN, scripts={
        1: [filler],  # exhausted-turns path: ok=False, note=exhausted
    })
    set_call_model(model)
    task = make_task(tmp_path, "bug02_mean", "mean() divides by len-1; should divide by len",
                     config={"max_step_turns": 4})
    # turn 0 must apply the real fix for final verify to pass:
    model.scripts = {1: [[
        """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
        "cat numlib/mathutil.py", "cat numlib/mathutil.py",
        "cat numlib/mathutil.py",  # 4th turn: budget gone, no SUBMIT
    ]]}
    result = run_task(task, log_root=tmp_path / "logs")
    assert result.status == "success", (
        "the fix was applied; exhausted turns must not hide a verified fix")
    state = _read_state(tmp_path, task.task_id)
    assert state["completed_steps"] == ["1. fix the bug in the target file"]
    assert state["remaining_plan"] == []
    kinds = [json.loads(l)["kind"] for l in
             (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
                 encoding="utf-8").strip().splitlines()]
    step_end = [json.loads(l) for l in
                (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
                    encoding="utf-8").strip().splitlines()
                if json.loads(l)["kind"] == "step_end"]
    assert step_end and step_end[0]["data"]["ok"] is False
    assert "exhausted" in step_end[0]["data"]["note"]


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
