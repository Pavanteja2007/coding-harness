"""Long-horizon planning for build mode (multi-session projects).

The round's contract under test:
- Task B: extract_acceptance_criteria turns a large feature request
  into an explicit criteria contract (id'd, testable sentences) via
  ONE model call; empty-reply flake retried once; unparseable output
  aborts honestly.
- Task A: plan_project decomposes into ordered, independently
  checkpointed sub-tasks each mapped to criteria ids; coverage gaps
  are a hard planning error; the project plan persists to
  logs/{project_id}/project.json (atomic writes) and resumes across
  sessions — completed sub-tasks are never re-run, the accumulated
  verified tree carries forward as the next sub-task's start repo.
- Task C: a real multi-sub-task feature (several DISTINCT module
  changes, not a single-file toggle) executing across >=2 real
  sessions with checkpoint/resume between them (Docker-gated e2e).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def _docker_up() -> bool:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=10,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    __import__("os").environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


def _trace_kinds(log_dir: Path):
    lines = (log_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l)["kind"] for l in lines]


# ---------------------------------------------------------------------------
# Task B — criteria extraction
# ---------------------------------------------------------------------------


class TestCriteriaExtraction:
    def _run(self, tmp_path, reply, request="build reporting for the shop"):
        from harness import deps
        from harness.build_plan import extract_acceptance_criteria
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class M:
            def __init__(self, out):
                self.out = out
                self.n = 0
                self.prompts = []

            def __call__(self, messages, **kw):
                self.n += 1
                self.prompts.append(messages)
                return self.out

            def get_last_usage(self):
                return {
                    "model": "fake",
                    "provider": "fake",
                    "tokens": 10,
                    "cost_usd": 0.0001,
                }

        m = M(reply)
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            return (
                extract_acceptance_criteria(
                    request,
                    str(FIXTURES / "feat02_shop"),
                    get_config({}),
                    trace,
                    ModelClient(trace, get_config({})),
                ),
                trace,
                m,
            )
        finally:
            deps.reset_overrides()

    def test_extracts_normalized_criteria(self, tmp_path):
        reply = json.dumps(
            {
                "criteria": [
                    {
                        "id": "receipts_render",
                        "description": "Checkout receipts "
                        "render a plain-text line bundle per cart.",
                    },
                    {
                        "id": "Loyalty Points!",
                        "description": "Customers earn 1 "
                        "point per whole dollar spent, after discounts.",
                    },
                    {
                        "id": "csv_export",
                        "description": "Carts export to CSV with one row per line.",
                    },
                ]
            }
        )
        (criteria, note), _trace, m = self._run(tmp_path, reply)
        assert criteria is not None
        ids = [c["id"] for c in criteria]
        assert ids == ["receipts_render", "loyalty_points", "csv_export"]
        assert all(c["description"] for c in criteria)
        assert "extracted 3" in note
        kinds = _trace_kinds(tmp_path / "t")
        assert "project_criteria_extracted" in kinds
        # the prompt carried the request + retrieval files
        assert "ACCEPTANCE-CRITERIA" in m.prompts[0][0]["content"]

    def test_unparseable_reply_aborts_honestly(self, tmp_path):
        (criteria, note), _trace, _ = self._run(tmp_path, "sure, sounds good")
        assert criteria is None
        assert "unparseable" in note
        assert "project_criteria_parse_error" in _trace_kinds(tmp_path / "t")

    def test_empty_reply_retried_once_then_good(self, tmp_path):
        """The endpoint's reasoning-burn flake: ONE retry with a repair
        nudge, then the honest failure path (same discipline as
        build_mode's authoring call)."""
        from harness import deps
        from harness.build_plan import extract_acceptance_criteria
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class EmptyThenGood:
            def __init__(self):
                self.n = 0
                self.retry_prompt = ""

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return ""
                self.retry_prompt = messages[-1]["content"]
                return json.dumps(
                    {
                        "criteria": [
                            {"id": "csv_export", "description": "Carts export to CSV."}
                        ]
                    }
                )

            def get_last_usage(self):
                return {"tokens": 10, "cost_usd": 0.0001}

        m = EmptyThenGood()
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            criteria, _note = extract_acceptance_criteria(
                "build reporting",
                str(FIXTURES / "feat02_shop"),
                get_config({}),
                trace,
                ModelClient(trace, get_config({})),
            )
        finally:
            deps.reset_overrides()
        assert criteria and criteria[0]["id"] == "csv_export"
        assert m.n == 2
        assert "EMPTY" in m.retry_prompt
        assert "project_criteria_empty_reply_retry" in _trace_kinds(tmp_path / "t")

    def test_empty_reply_twice_aborts(self, tmp_path):
        from harness import deps
        from harness.build_plan import extract_acceptance_criteria
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class EmptyAlways:
            def __init__(self):
                self.n = 0

            def __call__(self, *a, **k):
                self.n += 1
                return ""

            def get_last_usage(self):
                return {"tokens": 10, "cost_usd": 0.0}

        m = EmptyAlways()
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            criteria, _note = extract_acceptance_criteria(
                "build reporting",
                str(FIXTURES / "feat02_shop"),
                get_config({}),
                trace,
                ModelClient(trace, get_config({})),
            )
        finally:
            deps.reset_overrides()
        assert criteria is None
        assert m.n == 2

    def test_criteria_capped_at_config_max(self, tmp_path):
        many = {
            "criteria": [
                {"id": f"c{i}", "description": f"behavior {i}"} for i in range(12)
            ]
        }
        (criteria, _), _trace, _ = self._run(tmp_path, json.dumps(many))
        assert len(criteria) == 8  # project_criteria_max default
        assert "project_criteria_capped" in _trace_kinds(tmp_path / "t")

    def test_prompt_render_carries_criteria_contract(self):
        from harness.prompts import render_project_criteria_prompt

        msgs = render_project_criteria_prompt(
            request_text="build receipts, loyalty points, and CSV export",
            context_files=["shoplib/model.py"],
        )
        assert "receipts" in msgs[1]["content"]
        assert "shoplib/model.py" in msgs[1]["content"]
        assert "snake_case" in msgs[0]["content"]
        assert '"criteria"' in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Task A — decomposition + plan persistence + coverage gate
# ---------------------------------------------------------------------------


class TestProjectDecomposition:
    def _plan(self, tmp_path, reply):
        from harness import deps
        from harness.build_plan import plan_project
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class M:
            def __init__(self, out):
                self.out = out
                self.prompts = []

            def __call__(self, messages, **kw):
                self.prompts.append(messages)
                return self.out

            def get_last_usage(self):
                return {"tokens": 10, "cost_usd": 0.0001}

        m = M(reply)
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            criteria = [
                {"id": "receipts_render", "description": "Receipts render."},
                {"id": "loyalty_points", "description": "Points accrue."},
                {"id": "csv_export", "description": "CSV export works."},
            ]
            return (
                plan_project(
                    "build reporting",
                    str(FIXTURES / "feat02_shop"),
                    criteria,
                    get_config({}),
                    trace,
                    ModelClient(trace, get_config({})),
                ),
                trace,
                m,
            )
        finally:
            deps.reset_overrides()

    def test_decomposes_with_criteria_mapping(self, tmp_path):
        reply = json.dumps(
            {
                "analysis": "three modules, three sub-tasks",
                "sub_tasks": [
                    {
                        "id": 1,
                        "description": "add receipts rendering to shoplib",
                        "criteria": ["receipts_render"],
                        "files_hint": ["shoplib/serializers.py"],
                    },
                    {
                        "id": 2,
                        "description": "add loyalty point accrual to pricing",
                        "criteria": ["loyalty_points"],
                        "files_hint": ["shoplib/pricing.py"],
                    },
                    {
                        "id": 3,
                        "description": "add CSV export of carts",
                        "criteria": ["csv_export"],
                        "files_hint": ["shoplib/serializers.py"],
                    },
                ],
            }
        )
        (sub_tasks, note), _trace, m = self._plan(tmp_path, reply)
        assert sub_tasks is not None
        assert [st["id"] for st in sub_tasks] == [1, 2, 3]
        assert sub_tasks[0]["criteria"] == ["receipts_render"]
        assert sub_tasks[1]["files_hint"] == ["shoplib/pricing.py"]
        assert "decomposed into 3" in note
        # the prompt carried the criteria block
        assert "receipts_render" in m.prompts[0][1]["content"]
        assert "project_plan_generated" in _trace_kinds(tmp_path / "t")

    def test_sub_tasks_capped(self, tmp_path):
        many = {
            "analysis": "x",
            "sub_tasks": [
                {
                    "id": i,
                    "description": f"sub {i}",
                    "criteria": ["c"],
                    "files_hint": [],
                }
                for i in range(1, 8)
            ],
        }
        (sub_tasks, _), _trace, _ = self._plan(tmp_path, json.dumps(many))
        assert len(sub_tasks) == 4  # project_max_sub_tasks default
        assert "project_plan_capped" in _trace_kinds(tmp_path / "t")

    def test_unparseable_plan_aborts(self, tmp_path):
        (sub_tasks, note), _trace, _ = self._plan(tmp_path, "I would split it...")
        assert sub_tasks is None
        assert "unparseable" in note
        assert "project_plan_parse_error" in _trace_kinds(tmp_path / "t")

    def test_coverage_gap_detection(self):
        from harness.build_plan import _criteria_coverage_gap

        criteria = [
            {"id": "a", "description": ""},
            {"id": "b", "description": ""},
            {"id": "c", "description": ""},
        ]
        covered = [
            {"id": 1, "description": "x", "criteria": ["a", "b"]},
            {"id": 2, "description": "y", "criteria": []},
        ]
        assert _criteria_coverage_gap(criteria, covered) == ["c"]
        assert (
            _criteria_coverage_gap(
                criteria, [*covered, {"id": 3, "description": "z", "criteria": ["c"]}]
            )
            == []
        )


# ---------------------------------------------------------------------------
# Project plan file — persistence, resume, atomicity
# ---------------------------------------------------------------------------


class TestProjectFile:
    def test_write_read_roundtrip(self, tmp_path):
        from harness.build_plan import _write_project, read_project

        proj = {
            "project_id": "p1",
            "request_text": "build reporting",
            "repo_path": "/some/repo",
            "criteria": [{"id": "csv_export", "description": "CSV."}],
            "sub_tasks": [
                {
                    "id": 1,
                    "description": "csv",
                    "criteria": ["csv_export"],
                    "files_hint": [],
                },
            ],
            "completed": [],
            "current_tree": None,
            "sessions": 1,
            "status": "active",
        }
        _write_project(tmp_path, proj)
        assert (tmp_path / "project.json").is_file()
        assert not (tmp_path / "project.json.tmp").exists()
        assert read_project(tmp_path) == proj

    def test_read_tolerates_missing_and_broken(self, tmp_path):
        from harness.build_plan import read_project

        assert read_project(tmp_path) is None  # no file
        (tmp_path / "project.json").write_text("{ not json", encoding="utf-8")
        assert read_project(tmp_path) is None
        (tmp_path / "project.json").write_text('{"other": 1}', encoding="utf-8")
        assert read_project(tmp_path) is None  # no project_id

    def test_atomic_write_leaves_valid_file_on_crash(self, tmp_path):
        """A crash between tmp-write and replace can never leave a
        partial project.json (tmp+replace discipline)."""
        import os

        from harness.build_plan import _write_project, read_project

        _write_project(tmp_path, {"project_id": "p1"})
        # simulate: a later write's tmp exists but replace never ran
        (tmp_path / "project.json.tmp").write_text(
            '{"project_id": "p2", "comple', encoding="utf-8"
        )
        assert read_project(tmp_path) == {"project_id": "p1"}
        os.remove(tmp_path / "project.json.tmp")

    def test_next_sub_task_order(self):
        from harness.build_plan import _next_sub_task

        sts = [
            {"id": 1, "description": "a"},
            {"id": 2, "description": "b"},
            {"id": 3, "description": "c"},
        ]
        assert _next_sub_task(sts, [])["id"] == 1
        assert _next_sub_task(sts, [1])["id"] == 2
        assert _next_sub_task(sts, [1, 2])["id"] == 3
        assert _next_sub_task(sts, [1, 2, 3]) is None


# ---------------------------------------------------------------------------
# Router wiring — build_project dispatches to the project layer
# ---------------------------------------------------------------------------


class TestRouterWiring:
    def test_router_routes_large_build_to_project(self, tmp_path):
        from harness.router import route

        calls = {}

        def fake_project(**kw):
            calls["kw"] = kw
            return {
                "mode": "build",
                "status": "checkpointed",
                "project_id": "proj-x",
            }

        handlers = {"build": fake_project}
        out = route(
            "build a full reporting suite: receipts, loyalty points, and CSV export",
            str(FIXTURES / "feat02_shop"),
            config={"build_project": True},
            log_root=tmp_path,
            handlers=handlers,
        )
        assert out["status"] == "checkpointed"
        assert calls["kw"]["text"].startswith("build a full reporting")
        assert calls["kw"]["config"]["build_project"] is True

    def test_router_small_build_stays_single_session(self, tmp_path):
        from harness.router import route

        def explode_project(**kw):
            raise AssertionError("single-session build must not reach run_project")

        out = route(
            "add a mode() function for the most frequent value",
            str(FIXTURES / "feat02_shop"),
            config={},  # build_project defaults False
            log_root=tmp_path,
            handlers={
                "build": lambda **kw: {
                    "mode": "build",
                    "status": "single-session-called",
                    "kw": kw,
                }
            },
        )
        assert out["status"] == "single-session-called"
        assert out["kw"]["config"].get("build_project") is None


# ---------------------------------------------------------------------------
# Config keys
# ---------------------------------------------------------------------------


class TestConfigKeys:
    def test_defaults_present(self):
        from harness.config import DEFAULTS

        assert DEFAULTS["build_project"] is False
        assert DEFAULTS["project_max_sub_tasks"] == 4
        assert DEFAULTS["project_sub_tasks_per_session"] == 1
        assert DEFAULTS["project_criteria_max"] == 8
        assert DEFAULTS["project_resume"] is False


# ---------------------------------------------------------------------------
# Whole-project already_exists + by-verification completions
# ---------------------------------------------------------------------------


class TestProjectAlreadyExists:
    """run_project's documented whole-project "already_exists" verdict.

    Every sub-task completing BY VERIFICATION (its authored tests
    already pass on the start tree — nothing was built anywhere) is
    an honest "the feature exists" report, NOT a fake "built" success
    — and it must never run the final verify on the ORIGINAL repo
    (the sandbox mounts it read-write; each sub-task's verifier runs
    already passed on the tree that mattered).
    """

    CRITERIA_REPLY = json.dumps(
        {
            "criteria": [
                {"id": "receipts_render", "description": "Receipts render."},
                {"id": "csv_export", "description": "CSV export works."},
            ]
        }
    )
    PLAN_REPLY = json.dumps(
        {
            "analysis": "two sub-tasks",
            "sub_tasks": [
                {
                    "id": 1,
                    "description": "add receipts rendering",
                    "criteria": ["receipts_render"],
                    "files_hint": [],
                },
                {
                    "id": 2,
                    "description": "add CSV export",
                    "criteria": ["csv_export"],
                    "files_hint": [],
                },
            ],
        }
    )

    def _run(self, tmp_path, monkeypatch, sub_statuses, budget=1):
        """Drive run_project with a scripted model + mocked sub-builds.

        sub_statuses maps sub-task id -> the status its run_build
        returns; a dict value {"status": "success", "work": [files]}
        also materializes a work/ tree so pinning can succeed.
        """
        from harness import deps
        from harness.build_plan import run_project

        criteria_reply, plan_reply = self.CRITERIA_REPLY, self.PLAN_REPLY

        class M:
            def __call__(self, messages, **kw):
                system = next(
                    (m["content"] for m in messages if m["role"] == "system"),
                    "",
                )
                if "ACCEPTANCE-CRITERIA" in system:
                    return criteria_reply
                if "span SEVERAL work sessions" in system:
                    return plan_reply
                raise AssertionError(f"unexpected model call: {system[:120]}")

            def get_last_usage(self):
                return {"tokens": 1, "cost_usd": 0.0}

        builds = {}

        def fake_run_build(request_text, repo_path, config, log_root, task_id):
            n = int(task_id.rsplit("-s", 1)[1])
            builds[n] = {"request": request_text, "repo": repo_path}
            spec = sub_statuses[n]
            if spec["status"] != "success":
                return {"status": spec["status"], "note": "scripted"}
            work = Path(log_root) / task_id / "work"
            work.mkdir(parents=True, exist_ok=True)
            for f in spec.get("work", []):
                (work / f).parent.mkdir(parents=True, exist_ok=True)
                (work / f).write_text("# built\n", encoding="utf-8")
            return {"status": "success"}

        verify_calls = []

        def fake_verify(repo, *_a, **_k):
            verify_calls.append(repo)
            from execution.verify import VerificationResult

            return VerificationResult(
                target_test_passed=True,
                baseline_passed=False,
                regression_passed=True,
                flaky=False,
                raw_output="",
                structured_feedback=[],
            )

        monkeypatch.setattr("harness.build_mode.run_build", fake_run_build)
        monkeypatch.setattr("harness.build_plan._get_verify", lambda: fake_verify)
        m = M()
        deps.set_call_model(m)
        try:
            out = run_project(
                request_text="build receipts and CSV export",
                repo_path=str(FIXTURES / "feat02_shop"),
                config={"project_sub_tasks_per_session": budget},
                log_root=tmp_path / "logs",
                project_id="proj-ae",
            )
        finally:
            deps.reset_overrides()
        return out, builds, verify_calls

    def test_all_already_passing_mints_project_already_exists(
        self, tmp_path, monkeypatch
    ):
        """Every sub-task already-passing = the whole feature exists:
        status already_exists, no final verify (the original repo is
        never touched), and both sub-tasks resolve in ONE session
        (by-verification completions don't consume the build budget)."""
        out, builds, verify_calls = self._run(
            tmp_path,
            monkeypatch,
            {1: {"status": "already_exists"}, 2: {"status": "already_exists"}},
        )
        assert out["status"] == "already_exists", out.get("note")
        assert out["completed"] == [1, 2]
        assert out["sessions"] == 1
        # both sub-builds actually ran (per-sub-task honest checks)
        assert set(builds) == {1, 2}
        # NO verify ever ran — and never on the ORIGINAL repo
        assert verify_calls == []
        proj = json.loads(
            (tmp_path / "logs" / "proj-ae" / "project.json").read_text(encoding="utf-8")
        )
        assert proj["status"] == "already_exists"
        assert proj["current_tree"] is None

    def test_mixed_build_and_already_passing_reports_success(
        self, tmp_path, monkeypatch
    ):
        """One real build + one already-passing sub-task = a genuine
        success: the final verify DOES run, on the accumulated tree
        (never the original repo)."""
        out, _builds, verify_calls = self._run(
            tmp_path,
            monkeypatch,
            {
                1: {"status": "success", "work": ["shoplib/receipts.py"]},
                2: {"status": "already_exists"},
            },
            budget=2,
        )
        assert out["status"] == "success", out.get("note")
        assert out["completed"] == [1, 2]
        # the final verify ran exactly once, on the pinned tree
        assert len(verify_calls) == 1
        assert "proj-ae.tree-s1" in verify_calls[0].replace("\\", "/")

    def test_resume_of_already_exists_project_is_noop(self, tmp_path, monkeypatch):
        """Resuming a finished already_exists project is the same
        honest no-op as resuming a success."""
        out, _builds, _v = self._run(
            tmp_path,
            monkeypatch,
            {1: {"status": "already_exists"}, 2: {"status": "already_exists"}},
        )
        assert out["status"] == "already_exists"
        # a LATER session: nothing re-runs, the verdict stands
        from harness import deps
        from harness.build_plan import run_project

        def explode(*_a, **_k):
            raise AssertionError("a finished project must not re-run builds")

        monkeypatch.setattr("harness.build_mode.run_build", explode)
        deps.set_call_model(explode)
        try:
            out2 = run_project(
                request_text="build receipts and CSV export",
                repo_path=str(FIXTURES / "feat02_shop"),
                config={"project_resume": True},
                log_root=tmp_path / "logs",
                project_id="proj-ae",
            )
        finally:
            deps.reset_overrides()
        assert out2["status"] == "already_exists"
        assert out2["completed"] == [1, 2]
        assert "already complete" in out2["note"]
