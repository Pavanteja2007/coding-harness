"""Agent-written edge-case tests (Improvement Round 2, Tasks A+B).

The gate: after final verify passes but BEFORE success is minted, one
model call writes edge-case tests implied by the issue; they run through
the SAME verify() pipeline (baseline stage on pristine + post-fix stage
with flake-rerun + suite regression). A post-fix failure poisons the
attempt (retry with the failing test as feedback); a GENERATION problem
skips the gate — a verified fix is never overturned over test-writing
quality.

Covers the parse/sanitize matrix (no Docker) and, Docker-gated, the
gate-pass / gate-poison / gate-skip / disabled paths through the REAL
loop (real bash, real Docker sandbox, real verify).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness.agent_tests import (
    list_test_files,
    parse_agent_tests,
    sanitize_agent_tests,
    wipe_agent_tests,
)
from harness.core import run_task
from harness.deps import reset_overrides, set_call_model
from harness.prompts import render_agent_tests_prompt
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


_docker_gate = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


def make_task(tmp_path, config=None):
    cfg = {
        "test_command": "python -m pytest -q",
        "command_timeout_s": 60,
        "verify_timeout_s": 180,
        "max_step_turns": 8,
    }
    cfg.update(config or {})
    return Task(
        task_id=f"at-e2e-{abs(hash(str(tmp_path))) % 100000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Prompt shape (no Docker)
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_carries_issue_diff_tests_tree(self):
        msgs = render_agent_tests_prompt(
            issue_text="mean divides wrong",
            diff="+ the diff",
            tests_tree="tests/test_mathutil.py",
            max_tests=2,
        )
        user = msgs[1]["content"]
        assert "## Original issue" in user and "mean divides wrong" in user
        assert "## Proposed fix" in user and "+ the diff" in user
        assert "tests/test_mathutil.py" in user
        assert "2" in msgs[0]["content"]  # max_tests bound

    def test_empty_diff_visibly_flagged(self):
        msgs = render_agent_tests_prompt(issue_text="x", diff="", tests_tree="")
        assert "(empty diff)" in msgs[1]["content"]
        assert "(none found)" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# Parse + sanitize matrix (no Docker)
# ---------------------------------------------------------------------------

GOOD = "def test_edge():\n    assert 1 + 1 == 2\n"


def _reply(files):
    return json.dumps({"tests": files})


class TestParsing:
    def test_parse_basic_and_fenced(self):
        tests = parse_agent_tests(_reply([{"filename": "test_a.py", "content": GOOD}]))
        assert tests == [{"filename": "test_a.py", "content": GOOD}]
        fenced = f"```json\n{_reply([{'filename': 'test_b.py', 'content': GOOD}])}\n```"
        assert parse_agent_tests(fenced)[0]["filename"] == "test_b.py"

    def test_parse_empty_tests_list_is_none(self):
        assert parse_agent_tests('{"tests": []}') is None
        assert parse_agent_tests("no json here at all") is None

    def test_parse_drops_entries_missing_fields(self):
        tests = parse_agent_tests(
            _reply(
                [
                    {"filename": "", "content": GOOD},
                    {"filename": "test_a.py", "content": ""},
                    {"filename": "test_b.py", "content": GOOD},
                ]
            )
        )
        assert tests and len(tests) == 1 and tests[0]["filename"] == "test_b.py"


class TestSanitize:
    def SAN(self, tests, **kw):
        return sanitize_agent_tests(
            tests,
            max_files=kw.get("max_files", 3),
            max_chars=kw.get("max_chars", 12000),
        )

    def test_keeps_good_files(self):
        kept, reasons = self.SAN(
            [
                {"filename": "test_a.py", "content": GOOD},
                {"filename": "test_b.py", "content": GOOD},
            ]
        )
        assert len(kept) == 2 and not reasons

    def test_drops_path_tricks_and_bad_names(self):
        kept, reasons = self.SAN(
            [
                {"filename": "../evil.py", "content": GOOD},
                {"filename": "tests/x.py", "content": GOOD},
                {"filename": r"C:\abs.py", "content": GOOD},
                {"filename": "test_ok.py", "content": GOOD},
                {"filename": "test.exe.py.exe", "content": GOOD},
            ]
        )
        assert [t["filename"] for t in kept] == ["test_ok.py"]
        assert len(reasons) == 4

    def test_drops_syntax_error_content(self):
        kept, reasons = self.SAN(
            [{"filename": "test_bad.py", "content": "def broken(:"}]
        )
        assert kept == [] and "syntax error" in reasons[0]

    def test_caps_files_and_chars_and_dupes(self):
        many = [{"filename": f"test_{i}.py", "content": GOOD} for i in range(5)]
        kept, reasons = self.SAN(many, max_files=2)
        assert len(kept) == 2 and len(reasons) == 3
        dupe = [
            {"filename": "test_a.py", "content": GOOD},
            {"filename": "test_a.py", "content": GOOD + GOOD},
        ]
        kept, reasons = self.SAN(dupe)
        assert len(kept) == 1 and "duplicate" in reasons[0]
        big = [{"filename": "test_big.py", "content": "x = 1\n" * 5000}]
        kept, reasons = self.SAN(big, max_chars=100)
        assert kept == [] and "content cap" in reasons[0]


class TestTreeHelpers:
    def test_list_test_files(self, tmp_path):
        (tmp_path / "tests" / "sub").mkdir(parents=True)
        (tmp_path / "tests" / "test_one.py").write_text("")
        (tmp_path / "tests" / "sub" / "test_two.py").write_text("")
        (tmp_path / "tests" / "not_a_test.py").write_text("")
        out = list_test_files(str(tmp_path))
        assert "tests/test_one.py" in out and "tests/sub/test_two.py" in out
        assert "tests/not_a_test.py" not in out

    def test_list_test_files_missing_repo(self, tmp_path):
        assert list_test_files(str(tmp_path / "nope")) == ""

    def test_wipe_noop_and_real(self, tmp_path):
        d = tmp_path / "tests" / "_agent_generated"
        d.mkdir(parents=True)
        (d / "x.py").write_text("y = 1\n")
        wipe_agent_tests(str(tmp_path), "tests/_agent_generated")
        assert not d.exists()
        wipe_agent_tests(str(tmp_path), "tests/_agent_generated")  # no-op


# ---------------------------------------------------------------------------
# E2E through the real loop (Docker-gated)
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [
    {"id": 1, "description": "fix mean()", "checkpoint": "target test passes"}
]

FIX = "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"

# A real edge-case test the ISSUE implies: a SINGLE-element list exercises
# the len-1==0 division (the buggy code even ZeroDivisionErrors there).
# It fails on the pre-fix tree and passes under a correct fix.
GOOD_EDGE_TEST = (
    "from numlib.mathutil import mean\n"
    "\n"
    "\n"
    "def test_mean_edges():\n"
    "    # single element: the buggy len-1 denominator was zero\n"
    "    assert mean([10]) == 10.0\n"
    "    # negative + float mix through the same public call\n"
    "    assert mean([-2, 2]) == 0.0\n"
    "    assert mean([0.5, 1.5]) == 1.0\n"
)

# A test that PASSES on the pre-fix tree: probes nothing, must be dropped
# by the baseline stage (the final gate already covers this territory).
ALREADY_GREEN_TEST = (
    "from numlib.mathutil import median\n"
    "\n"
    "\n"
    "def test_median_untouched_by_fix():\n"
    "    assert median([3, 1, 2]) == 2\n"
)


class AgentTestsModel:
    """Scripted fake with a queue for the agent-tests generation call.

    Dispatches on the generation system prompt ("EDGE-CASE tests");
    verdicts pop from gen_replies (raw JSON strings). Step/planner
    calls delegate to an inner ScriptedModel.
    """

    def __init__(self, plan, scripts, gen_replies):
        self.inner = ScriptedModel(plan=plan, scripts=scripts)
        self.gen_replies = list(gen_replies)
        self.gen_calls = []

    def get_last_usage(self):
        return self.inner.get_last_usage()

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "EDGE-CASE tests" in system:
            self.gen_calls.append(messages)
            return (
                self.gen_replies.pop(0)
                if self.gen_replies
                else json.dumps({"tests": []})
            )
        return self.inner(messages, **kwargs)


@_docker_gate
class TestAgentTestsE2E:
    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        yield
        reset_overrides()

    def test_gate_pass_success_one_generation_call(self, tmp_path):
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            gen_replies=[
                _reply(
                    [
                        {"filename": "test_edges.py", "content": GOOD_EDGE_TEST},
                        # baseline-passing probe must be dropped, not fatal
                        {"filename": "test_green.py", "content": ALREADY_GREEN_TEST},
                    ]
                )
            ],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 1
        assert len(model.gen_calls) == 1
        # the generation call saw the issue + the candidate diff
        user = model.gen_calls[0][1]["content"]
        assert "len-1" in user and "mathutil" in user
        # both stages ran through the real verify; both events recorded
        events = self._events(tmp_path, task)
        kinds = [e["kind"] for e in events]
        assert "agent_tests_generated" in kinds
        assert "agent_tests_baseline_pass" in kinds  # green probe dropped
        assert "agent_tests_passed" in kinds
        # the surviving test is saved for human review
        saved = tmp_path / "logs" / task.task_id / "agent_tests" / "saved"
        assert (saved / "attempt1_test_edges.py").read_text(
            encoding="utf-8"
        ) == GOOD_EDGE_TEST
        # hygiene: work/ never held the generated dir; diff is clean
        assert not (
            tmp_path / "logs" / task.task_id / "work" / "tests" / "_agent_generated"
        ).exists()
        assert result.diff and "mathutil" in result.diff
        assert "_agent_generated" not in result.diff

    def test_gate_poison_retries_then_passes(self, tmp_path):
        """The critical path: the fix passes the WHOLE existing suite but
        fails an issue-implied edge case the generated tests catch ->
        attempt is poisoned, the failing test becomes feedback, attempt 2
        (the correct fix) mints the success."""
        # passes every existing suite test, fails the edge test's
        # negative-mix case (see test_poison_exhaustion_fails_task)
        wrong_fix = (
            "python - <<'EOF'\n"
            "p = 'numlib/mathutil.py'\n"
            "s = open(p).read()\n"
            "old = 'return sum(values) / (len(values) - 1)'\n"
            'new = "return sum(values) / len(values) + (0.5 if values and min(values) < 0 else 0.0)"\n'
            "assert old in s\n"
            "open(p, 'w').write(s.replace(old, new))\n"
            "EOF"
        )
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[wrong_fix, "SUBMIT"], [FIX, "SUBMIT"]]},
            gen_replies=[
                _reply([{"filename": "test_edges.py", "content": GOOD_EDGE_TEST}]),
                _reply([{"filename": "test_edges.py", "content": GOOD_EDGE_TEST}]),
            ],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 2
        assert len(model.gen_calls) == 2
        events = self._events(tmp_path, task)
        kinds = [e["kind"] for e in events]
        assert "agent_tests_failed" in kinds
        failed = [e for e in events if e["kind"] == "agent_tests_failed"]
        assert failed[0]["data"]["attempt"] == 1
        # the poison feedback reached attempt 2's first step session
        step_prompts = [
            e
            for e in events
            if e["kind"] == "model_request" and e["data"]["step"] == "step-1"
        ]
        firsts = [m["data"]["messages"][1]["content"] for m in step_prompts]
        # first user msg of attempt 2's step-1 = "## Feedback..." + gate text
        assert any("EDGE-CASE tests" in c for c in firsts), firsts

    def test_unparseable_generation_skips_gate(self, tmp_path):
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            gen_replies=["I will write tests later, definitely."],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert result.attempts == 1
        kinds = [e["kind"] for e in self._events(tmp_path, task)]
        assert "agent_tests_skip" in kinds
        skip = [
            e for e in self._events(tmp_path, task) if e["kind"] == "agent_tests_skip"
        ]
        assert "unparseable" in skip[0]["data"]["reason"]

    def test_all_baseline_pass_skips_gate(self, tmp_path):
        """Every generated test passes on the PRE-FIX tree: none probe
        the issue, so the gate honestly declines to judge (skip, not
        pass-by-default and not a poison)."""
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            gen_replies=[
                _reply([{"filename": "test_green.py", "content": ALREADY_GREEN_TEST}])
            ],
        )
        set_call_model(model)
        task = make_task(tmp_path)
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        kinds = [e["kind"] for e in self._events(tmp_path, task)]
        assert "agent_tests_all_baseline_pass" in kinds
        assert "agent_tests_failed" not in kinds
        # nothing saved: no test survived the baseline filter
        saved = tmp_path / "logs" / task.task_id / "agent_tests" / "saved"
        assert not saved.exists() or not list(saved.iterdir())

    def test_disabled_config_skips_generation_entirely(self, tmp_path):
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"]]},
            gen_replies=["}not json at all["],  # would skip if called
        )
        set_call_model(model)
        task = make_task(tmp_path, config={"agent_tests": False})
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "success"
        assert len(model.gen_calls) == 0
        kinds = [e["kind"] for e in self._events(tmp_path, task)]
        assert not any(k.startswith("agent_tests_") for k in kinds)

    def test_poison_exhaustion_fails_task(self, tmp_path):
        """Every attempt's fix fails the edge gate -> retries exhaust ->
        task failed (the gate is a real verifier-class signal, and the
        final state.json records what happened)."""
        model = AgentTestsModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[FIX, "SUBMIT"], [FIX, "SUBMIT"]]},
            gen_replies=[
                _reply([{"filename": "test_edges.py", "content": GOOD_EDGE_TEST}]),
                _reply([{"filename": "test_edges.py", "content": GOOD_EDGE_TEST}]),
            ],
        )
        # Both attempts apply a fix that passes the ENTIRE existing suite
        # (even/empty/variance all correct) but fails the edge test's
        # negative-mix case — exactly the incompleteness class the gate
        # exists to catch (issue says "wrong expected values", so
        # negatives are an implied edge the suite never covered).
        edge_failing_fix = (
            "python - <<'EOF'\n"
            "p = 'numlib/mathutil.py'\n"
            "s = open(p).read()\n"
            "old = 'return sum(values) / (len(values) - 1)'\n"
            'new = "return sum(values) / len(values) + (0.5 if values and min(values) < 0 else 0.0)"\n'
            "assert old in s\n"
            "open(p, 'w').write(s.replace(old, new))\n"
            "EOF"
        )
        model.inner.scripts = {
            1: [[edge_failing_fix, "SUBMIT"], [edge_failing_fix, "SUBMIT"]]
        }
        set_call_model(model)
        task = make_task(tmp_path, config={"max_retries": 2})
        result = run_task(task, log_root=tmp_path / "logs")
        assert result.status == "failed"
        assert result.attempts == 2
        assert len(model.gen_calls) == 2
        kinds = [e["kind"] for e in self._events(tmp_path, task)]
        assert kinds.count("agent_tests_failed") == 2

    # -- helper ----------------------------------------------------------

    def _events(self, tmp_path, task):
        path = tmp_path / "logs" / task.task_id / "trace.jsonl"
        return [
            json.loads(l)
            for l in path.read_text(encoding="utf-8").strip().splitlines()
            if l
        ]
