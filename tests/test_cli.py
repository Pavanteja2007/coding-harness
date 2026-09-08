"""Tests for cli/main.py — parser, status, memory subcommands, fix and
run-benchmark against a scripted fake model (full offline e2e)."""
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from cli import deps
from cli.main import build_parser, main


@pytest.fixture(autouse=True)
def _clean_overrides():
    deps.reset_overrides()
    yield
    deps.reset_overrides()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HARNESS_HOME so tests never touch the real .harness/."""
    h = tmp_path / "home"
    monkeypatch.setenv("HARNESS_HOME", str(h))
    return h


@pytest.fixture
def logs_root(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# parser / status
# ---------------------------------------------------------------------------

def test_parser_shape():
    p = build_parser()
    args = p.parse_args(["status", "--task-id", "t1"])
    assert args.task_id == "t1"
    args = p.parse_args(["fix", "--repo", "r", "--issue", "i"])
    assert args.command == "fix" and args.model is None


def test_status_missing_task(logs_root, home, capsys):
    rc = main(["status", "--task-id", "nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no state file" in err


def test_status_renders_state(logs_root, home, capsys):
    d = logs_root / "task-9"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-9",
        "plan": ["1. fix mean", "2. verify"],
        "completed_steps": ["1. fix mean"],
        "files_touched": ["mathutil.py"],
        "decisions": ["chose minimal patch"],
        "remaining_plan": ["2. verify"],
    }), encoding="utf-8")
    rc = main(["status", "--task-id", "task-9"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task-9" in out
    assert "[x] 1. fix mean" in out
    assert "[ ] 2. verify" in out
    assert "mathutil.py" in out
    assert "chose minimal patch" in out


def test_status_enriches_from_trace(logs_root, home, capsys):
    d = logs_root / "task-10"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-10", "plan": ["1. x"], "completed_steps": [],
        "files_touched": [], "decisions": [], "remaining_plan": ["1. x"],
    }), encoding="utf-8")
    (d / "trace.jsonl").write_text(
        json.dumps({"kind": "result", "data": {"status": "success", "cost_usd": 0.01}}) + "\n",
        encoding="utf-8")
    rc = main(["status", "--task-id", "task-10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "success" in out
    assert "0.0100" in out


# ---------------------------------------------------------------------------
# memory subcommands
# ---------------------------------------------------------------------------

def test_memory_record_and_query(home, capsys):
    rc = main(["memory", "record", "prefer diff-based edits"])
    assert rc == 0
    rc = main(["memory", "query-decisions", "diff"])
    out = capsys.readouterr().out
    assert "prefer diff-based edits" in out


def test_memory_ingest(home, logs_root, capsys):
    d = logs_root / "task-i"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "task_id": "task-i", "plan": [], "completed_steps": [],
        "files_touched": [], "decisions": ["inested fact"],
        "remaining_plan": [],
    }), encoding="utf-8")
    rc = main(["memory", "ingest"])
    assert rc == 0
    assert "ingested 1" in capsys.readouterr().out
    rc = main(["memory", "query-decisions", "inested"])
    assert "inested fact" in capsys.readouterr().out


def test_memory_query_structure(home, capsys, tmp_path):
    repo = tmp_path / "struct_repo"
    _make_struct_repo(repo)
    rc = main(["memory", "query-structure", "--repo", str(repo), "callees bar"])
    assert rc == 0
    assert "alpha.foo" in capsys.readouterr().out


def _make_struct_repo(root: Path):
    root.mkdir(parents=True)
    (root / "alpha.py").write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return foo()\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# fix — full offline e2e with a scripted model
# ---------------------------------------------------------------------------

class ScriptedModel:
    """Replays canned replies: a planner JSON, then bash commands that fix
    the fixture bug, then SUBMIT."""

    def __init__(self):
        self.step = 0
        self.usage = {"model": "scripted", "provider": "test", "tokens": 1, "cost_usd": 0.0}

    def get_last_usage(self):
        return dict(self.usage)

    def __call__(self, messages, difficulty_hint=None, provider=None, model=None, api_key=None):
        self.step += 1
        first_user = messages[0].get("content", "") if messages else ""
        # Planner call (system prompt asks for a plan JSON)
        if any("planning a bug fix" in m.get("content", "") for m in messages):
            return json.dumps({
                "analysis": "mean() returns sum; divide by len",
                "plan": [{
                    "id": 1,
                    "description": "fix mean() to divide by len(values)",
                    "checkpoint": "tests pass",
                    "files_hint": ["mathutil.py"],
                }],
            })
        # First step turn: apply the fix via a python one-liner (portable),
        # then later SUBMIT.
        if self.step in (2,) or (self.step > 2 and "Begin" in first_user):
            return (
                "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
                "s = p.read_text(); s = s.replace('return sum(values)', "
                "'return sum(values) / len(values)'); p.write_text(s)\""
            )
        if self.step > 2:
            return "SUBMIT"
        # feedback retry path: re-apply fix is idempotent, then submit
        return "SUBMIT"


@pytest.fixture
def smoke_repo(tmp_path: Path) -> Path:
    """Copy of the CLI smoke fixture (buggy mean) as a fresh repo each run."""
    import shutil

    src = Path(__file__).resolve().parent.parent / "cli" / "fixtures" / "smoke_repo"
    dst = tmp_path / "repo"
    shutil.copytree(src, dst)
    return dst


def test_fix_e2e_offline(smoke_repo, home, logs_root, capsys, monkeypatch):
    """CLI fix -> real run_task -> scripted model fixes mean() -> verify
    passes -> success summary printed; decision memory ingestible."""
    import harness.deps as hdeps

    fake = ScriptedModel()
    hdeps.set_call_model(fake)

    # point harness at the same isolated logs root
    monkeypatch.chdir(smoke_repo.parent)

    rc = main([
        "fix",
        "--repo", str(smoke_repo),
        "--issue", "mean() in mathutil.py returns the sum; make it the mean",
        "--target-test", "tests/test_mathutil.py::test_mean",
        "--max-retries", "2",
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"fix did not succeed:\n{out}"
    assert "=== task" in out and "success" in out
    assert "target test:   PASS" in out
    assert "diff" in out.lower()

    # state.json written for the run (find the newest task dir)
    task_dirs = [p for p in logs_root.iterdir() if p.is_dir()]
    assert task_dirs, "no task log dir created"
    state = json.loads((task_dirs[0] / "state.json").read_text(encoding="utf-8"))
    assert state["decisions"], "harness should record at least one decision"

    # memory ingest pipeline sees it
    rc2 = main(["memory", "ingest"])
    assert "ingested" in capsys.readouterr().out
    rc3 = main(["memory", "query-decisions", "verified"])
    assert "verified" in capsys.readouterr().out.lower()


def test_fix_bad_repo(home, capsys):
    rc = main(["fix", "--repo", "Z:/definitely/not/here", "--issue", "x"])
    assert rc == 2
    assert "--repo" in capsys.readouterr().err


def test_fix_issue_from_file(smoke_repo, home, logs_root, capsys, monkeypatch, tmp_path):
    import harness.deps as hdeps

    hdeps.set_call_model(ScriptedModel())
    monkeypatch.chdir(smoke_repo.parent)
    issue_file = tmp_path / "issue.txt"
    issue_file.write_text(
        "mean() in mathutil.py returns the sum; make it the mean",
        encoding="utf-8")
    rc = main([
        "fix", "--repo", str(smoke_repo), "--issue", f"@{issue_file}",
        "--target-test", "tests/test_mathutil.py::test_mean",
    ])
    assert rc == 0


def test_fix_reports_clean_model_error(smoke_repo, home, capsys, monkeypatch):
    """No API key + broken model -> a structured 'error' result (run_task
    owns failure handling — the CLI never sees a raw traceback)."""
    import harness.deps as hdeps

    def broken_model(*a, **k):
        raise RuntimeError("litellm is not importable on this machine")

    hdeps.set_call_model(broken_model)
    monkeypatch.chdir(smoke_repo.parent)
    rc = main([
        "fix", "--repo", str(smoke_repo),
        "--issue", "anything", "--target-test", "tests/test_mathutil.py::test_mean",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "=== task" in captured.out and "error" in captured.out
    assert "Traceback" not in captured.out + captured.err
    hdeps.reset_overrides()


# ---------------------------------------------------------------------------
# run-benchmark
# ---------------------------------------------------------------------------

def test_run_benchmark_smoke_offline(home, logs_root, capsys, monkeypatch):
    """run-benchmark --subset smoke: full loop through Terminal 3's REAL
    scheduler (worker subprocesses, fake harness per its documented
    offline config) — proving the Boundary 6 wiring end to end."""
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)  # repo root
    rc = main([
        "run-benchmark", "--subset", "smoke",
        "--concurrency", "2",
        "--log-root", str(logs_root),
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"smoke benchmark failed:\n{out}"
    assert "benchmark: smoke" in out
    assert "success 1 / 1" in out


def test_run_benchmark_unknown_subset(home, capsys):
    rc = main(["run-benchmark", "--subset", "does-not-exist"])
    assert rc == 2
    assert "unknown subset" in capsys.readouterr().err


def test_run_benchmark_json_subset(home, logs_root, capsys, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_fixture_repo_copy(repo)
    subset = tmp_path / "subset.json"
    subset.write_text(json.dumps([{
        "repo": str(repo),
        "issue": "mean() returns the sum; make it the mean",
        "task_id": "bench-0",
        "config": {"use_fake_harness": True,
                   "fake_steps": ["plan", "edit", "verify"],
                   "fake_step_delay_s": 0.05,
                   "crash_retries": 0},
    }]), encoding="utf-8")
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    rc = main(["run-benchmark", "--subset", str(subset), "--concurrency", "1",
               "--log-root", str(logs_root)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "bench-0" in out and "success 1 / 1" in out


def _make_fixture_repo_copy(dst: Path):
    import shutil

    src = Path(__file__).resolve().parent.parent / "cli" / "fixtures" / "smoke_repo"
    shutil.copytree(src, dst)


def test_scheduler_stub_fanout(home, capsys):
    """The stub scheduler itself: N tasks, distinct results, order kept."""
    from cli._stubs.scheduler import run
    from shared.types import Task, TaskResult

    def fake_run_task(task):
        time.sleep(0.02)
        return TaskResult(
            task_id=task.task_id, status="success", attempts=1, diff=None,
            verification=None, cost_usd=0.0, model_calls=[], log_path="",
        )

    tasks = [Task(task_id=f"t{i}", repo_path=".", issue_text="x", config={})
             for i in range(6)]
    results = run(tasks, concurrency=3, run_task=fake_run_task)
    assert [r.task_id for r in results] == [t.task_id for t in tasks]
    assert all(r.status == "success" for r in results)


def test_scheduler_stub_crash_isolated(home, capsys):
    from cli._stubs.scheduler import run
    from shared.types import Task

    def flaky(task):
        if task.task_id == "bad":
            raise RuntimeError("boom")
        return _ok_result(task.task_id)

    tasks = [Task(task_id=n, repo_path=".", issue_text="x", config={})
             for n in ("good", "bad", "also-good")]
    results = run(tasks, concurrency=2, run_task=flaky)
    statuses = {r.task_id: r.status for r in results}
    assert statuses == {"good": "success", "bad": "error", "also-good": "success"}


def _ok_result(task_id):
    from shared.types import TaskResult

    return TaskResult(
        task_id=task_id, status="success", attempts=1, diff=None,
        verification=None, cost_usd=0.0, model_calls=[], log_path="",
    )
