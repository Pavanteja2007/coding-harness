"""Self-critique gate tests (Agent Intelligence round, Task A).

The gate: after final verify passes but BEFORE success is minted, one model
call reviews the diff against the ORIGINAL issue ("does this diff actually
address what was reported, not just make tests pass"). A "no" verdict
poisons the attempt (retry with the critique's reason as feedback) — the
failure class verification alone misses.

Covers the prompt shape, the verdict-parse matrix, and (Docker-gated, real
loop) the approve / reject-then-approve / disabled paths end-to-end.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness.core import run_task
from harness.deps import reset_overrides, set_call_model
from harness.prompts import render_self_critique_prompt
from shared.types import Task
from tests.fake_model import ScriptedModel

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_gate():
    return pytest.mark.skipif(
        os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
        reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
    )


class CritiqueModel:
    """Scripted fake with a separate queue for self-critique calls.

    Dispatches on the critique system prompt ("skeptical code reviewer");
    every critique call pops the next verdict from critique_verdicts
    (True = approve). Step/planner calls delegate to an inner ScriptedModel.
    """

    def __init__(self, plan, scripts, critique_verdicts):
        self.inner = ScriptedModel(plan=plan, scripts=scripts)
        self.critique_verdicts = list(critique_verdicts)
        self.critique_calls = []
        self.critique_prompts = []

    def get_last_usage(self):
        return self.inner.get_last_usage()

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "skeptical code reviewer" in system:
            self.critique_calls.append(messages)
            self.critique_prompts.append(system)
            verdict = self.critique_verdicts.pop(0) if self.critique_verdicts else True
            return json.dumps(
                {
                    "addresses_issue": verdict,
                    "reason": (
                        ""
                        if verdict
                        else "the diff special-cases the test inputs "
                        "instead of fixing the reported boundary"
                    ),
                }
            )
        return self.inner(messages, **kwargs)


class OneVerdictModel:
    """Always returns one fixed critique verdict; used for the parse-matrix
    unit tests (no Docker, no loop)."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self.reply

    def get_last_usage(self):
        return {}


def make_task(tmp_path, config=None):
    cfg = {
        "test_command": "python -m pytest -q",
        "command_timeout_s": 60,
        "verify_timeout_s": 180,
        "max_step_turns": 8,
    }
    cfg.update(config or {})
    return Task(
        task_id=f"sc-e2e-{abs(hash(str(tmp_path))) % 100000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Prompt shape (no Docker)
# ---------------------------------------------------------------------------


class TestCritiquePrompt:
    def test_prompt_carries_issue_diff_and_feedback(self):
        msgs = render_self_critique_prompt(
            issue_text="the bug text",
            diff="+ a change",
            verification_summary="target passed; suite green",
            feedback_objects=[
                {"test_id": "tests/x.py::t", "summary": "expected 3, got 6"}
            ],
        )
        user = msgs[1]["content"]
        assert "## Original issue" in user and "the bug text" in user
        assert "## Proposed fix (full diff)" in user and "+ a change" in user
        assert "tests/x.py::t" in user
        assert msgs[0]["role"] == "system"
        assert "addresses_issue" in msgs[0]["content"]

    def test_empty_diff_is_visibly_flagged(self):
        msgs = render_self_critique_prompt(issue_text="x", diff="")
        assert "(empty diff)" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# Verdict-parse matrix (no Docker; _self_critique directly)
# ---------------------------------------------------------------------------


class TestVerdictParsing:
    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        yield
        reset_overrides()

    def _run_critique(self, reply, tmp_path, monkeypatch):
        from harness.core import _self_critique
        from harness.trace import TraceLogger
        from shared.types import VerificationResult

        fake = OneVerdictModel(reply)
        set_call_model(fake)
        task = Task(
            task_id="parse-matrix", repo_path=".", issue_text="the issue", config={}
        )
        trace = TraceLogger(tmp_path)
        v = VerificationResult(
            target_test_passed=True,
            baseline_passed=False,
            regression_passed=True,
            flaky=False,
            raw_output="",
            structured_feedback=[],
        )
        return _self_critique(
            task,
            {"self_critique_max_chars": 8000},
            _FakeClient(fake),
            trace,
            "+ the diff",
            v,
            "",
        )

    def test_yes_verdict(self, tmp_path, monkeypatch):
        ok, reason = self._run_critique(
            '{"addresses_issue": true, "reason": "minimal and correct"}',
            tmp_path,
            monkeypatch,
        )
        assert ok is True
        assert reason == "minimal and correct"

    def test_no_verdict(self, tmp_path, monkeypatch):
        ok, reason = self._run_critique(
            '{"addresses_issue": false, "reason": "dodges the complaint"}',
            tmp_path,
            monkeypatch,
        )
        assert ok is False
        assert reason == "dodges the complaint"

    def test_unparseable_reply_approves(self, tmp_path, monkeypatch):
        ok, reason = self._run_critique(
            "The diff looks good to me overall!", tmp_path, monkeypatch
        )
        assert ok is True
        assert reason == ""

    def test_json_without_verdict_approves(self, tmp_path, monkeypatch):
        ok, _ = self._run_critique('{"analysis": "unclear"}', tmp_path, monkeypatch)
        assert ok is True


class _FakeClient:
    """Minimal ModelClient stand-in for direct _self_critique tests."""

    def __init__(self, fake):
        self.fake = fake

    def call(self, messages, step, difficulty_hint=None):
        return self.fake(messages)


# ---------------------------------------------------------------------------
# Boundary 7 consumption in the repair-loop feedback (Task B)
# ---------------------------------------------------------------------------


class TestStructuredFeedbackInRepairLoop:
    """_target_feedback/_regression_feedback render format_objects() when
    structured_feedback is present; raw tail otherwise (graceful adoption,
    per INTERFACES.md Boundary 7's recommended consumption)."""

    def _v(self, structured=None):
        from shared.types import VerificationResult

        raw = (
            "$ python -m pytest -q tests/x.py\nexit=1\nF [100%]\n"
            "==== FAILURES ====\n___ test_mean ___\n"
            "> assert mean([2, 4]) == 3\nE assert 6.0 == 3\n"
            "tests/x.py:5: AssertionError\n"
        )
        return VerificationResult(
            target_test_passed=False,
            baseline_passed=False,
            regression_passed=False,
            flaky=False,
            raw_output=raw,
            structured_feedback=structured or [],
        )

    def test_structured_present_renders_objects(self):
        from harness.core import _target_feedback

        v = self._v(
            structured=[
                {
                    "test_id": "tests/x.py::test_mean",
                    "failure_type": "assertion_mismatch",
                    "summary": "test_mean failed: expected 3, got 6.0",
                    "expected": "3",
                    "actual": "6.0",
                    "file": "tests/x.py",
                    "line": 5,
                    "traceback_summary": ">  assert mean([2, 4]) == 3\nE  assert 6.0 == 3",
                }
            ]
        )
        out = _target_feedback(v, {"description": "fix mean"})
        assert "tests/x.py::test_mean" in out
        assert "expected 3, got 6.0" in out
        # compact: no full pytest source listing
        assert "FAILURES ====" not in out

    def test_structured_absent_falls_back_to_raw_tail(self):
        from harness.core import _target_feedback

        v = self._v()
        out = _target_feedback(v, {"description": "fix mean"})
        assert "assert 6.0 == 3" in out  # the raw tail still carries it

    def test_regression_feedback_uses_objects(self):
        from harness.core import _regression_feedback

        v = self._v(
            structured=[
                {
                    "test_id": "tests/y.py::test_other",
                    "failure_type": "assertion_mismatch",
                    "summary": "test_other failed: expected 1, got 2",
                    "expected": "1",
                    "actual": "2",
                    "file": "tests/y.py",
                    "line": 9,
                    "traceback_summary": "",
                }
            ]
        )
        out = _regression_feedback(v)
        assert "tests/y.py::test_other" in out
        assert "expected 1, got 2" in out


# ---------------------------------------------------------------------------
# E2E through the real loop (Docker-gated)
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [
    {"id": 1, "description": "fix mean()", "checkpoint": "target test passes"}
]

FIX = "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"


@_docker_gate()
class TestSelfCritiqueE2E:
    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        yield
        reset_overrides()

    def test_approve_path_success_one_critique_call(self, tmp_path):
        model = CritiqueModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            critique_verdicts=[True],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 1
        assert len(model.critique_calls) == 1
        # the critique saw the issue + the diff
        user = model.critique_calls[0][1]["content"]
        assert "len-1" in user and "len(values)" in user
        # trace carries the verdict
        trace = (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
            encoding="utf-8"
        )
        assert "self_critique" in trace

    def test_reject_then_approve_retries_with_reason(self, tmp_path):
        model = CritiqueModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},  # same fix on attempt 2
            critique_verdicts=[False, True],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 2
        assert len(model.critique_calls) == 2
        trace = (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
            encoding="utf-8"
        )
        assert "self_critique_reject" in trace
        # the retry's first step session carried the critique reason
        assert "special-cases the test inputs" in trace

    def test_disabled_config_skips_critique_call(self, tmp_path):
        model = CritiqueModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            critique_verdicts=[False],  # would reject if called
        )
        set_call_model(model)
        task = make_task(tmp_path, config={"self_critique": False})
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 1
        assert len(model.critique_calls) == 0
        trace = (tmp_path / "logs" / task.task_id / "trace.jsonl").read_text(
            encoding="utf-8"
        )
        kinds = [json.loads(l)["kind"] for l in trace.splitlines() if l.strip()]
        assert "self_critique" not in kinds
        assert "self_critique_reject" not in kinds

    def test_reject_exhaustion_fails_task(self, tmp_path):
        model = CritiqueModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            critique_verdicts=[False, False],
        )
        set_call_model(model)
        task = make_task(tmp_path, config={"max_retries": 2})
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "failed"
        assert result.attempts == 2
        assert len(model.critique_calls) == 2
