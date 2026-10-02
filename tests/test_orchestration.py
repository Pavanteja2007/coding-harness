"""Regression tests for bounded runtime orchestration and automation."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from harness.agent_kernel import ToolCall
from runtime.automation import (
    AutomationConfig,
    AutomationDisabled,
    AutomationIngress,
    IdempotencyConflict,
)
from runtime.orchestration import (
    ClaimConflict,
    ClaimStore,
    Orchestrator,
    SpawnApprovalGate,
    WorkflowLimits,
    WorkflowNode,
    WorkflowSpec,
    decompose_tasks,
)
from runtime.roles import (
    ROLE_NAMES,
    build_role_policy,
    build_role_registry,
    get_role_profile,
)
from runtime.worktrees import WorktreeBusy, WorktreeError, WorktreeManager

_FINISH = json.dumps({"tool": "finish", "arguments": {"answer": "done"}})


def _docker_up() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _limits(**overrides) -> WorkflowLimits:
    values = {
        "max_concurrency": 2,
        "max_total_cost_usd": 10.0,
        "max_child_retries": 1,
        "max_child_wallclock_s": 5.0,
        "max_workflow_wallclock_s": 20.0,
    }
    values.update(overrides)
    return WorkflowLimits(**values)


def _mock_config() -> dict:
    return {
        "use_mock_provider": True,
        "provider": "mock",
        "model": "mock",
        "orchestration_turns": [_FINISH],
        "max_step_turns": 2,
    }


def _workflow(repo: Path, workflow_id: str = "wf-basic") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        repo_path=str(repo),
        nodes=(
            WorkflowNode(
                node_id="root",
                role="planner",
                request="Plan the bounded review.",
                config=_mock_config(),
            ),
            WorkflowNode(
                node_id="review",
                role="reviewer",
                parent_id="root",
                request="Review the plan.",
                config=_mock_config(),
            ),
        ),
        limits=_limits(),
    )


class TestRoleProfiles:
    def test_all_roles_are_closed_and_default_deny(self):
        profiles = {name: get_role_profile(name) for name in ROLE_NAMES}
        assert set(profiles) == set(ROLE_NAMES)
        for profile in profiles.values():
            registry = build_role_registry(profile)
            canonical = {
                registry.spec(name).name
                for name in registry.names
                if registry.spec(name) is not None
            }
            assert canonical == set(profile.visible_tools)
            assert "mcp" not in canonical
            policy = build_role_policy(profile)
            assert policy.default_action == "deny"

    def test_policy_denies_hidden_or_forbidden_effects(self):
        assert get_role_profile("architect").name == "planner"
        assert get_role_profile("tester").name == "debugger"
        policy = build_role_policy("debugger")
        assert policy.evaluate(
            ToolCall(tool="edit", arguments={"path": "app.py"})
        ).terminal
        assert policy.evaluate(
            ToolCall(tool="shell", arguments={"command": "python -c 'print(1)'"})
        ).allowed
        assert get_role_profile("implementer").mutates is True
        assert get_role_profile("verifier").accepts_unverified is False


class TestWorkflowGraph:
    def test_decomposition_and_topological_order(self, tmp_path):
        spec = decompose_tasks(
            "wf-decompose",
            str(_repo(tmp_path)),
            "Plan the change.",
            [
                {"id": "one", "role": "planner", "description": "Explore."},
                {"id": "two", "role": "reviewer", "depends_on": ["one"]},
            ],
            limits=_limits(),
        )
        assert [node.node_id for node in spec.nodes] == ["root", "one", "two"]
        assert spec.topological_order() == ("root", "one", "two")

    @pytest.mark.parametrize(
        "nodes, message",
        [
            (
                (("a", "planner", "a", "", ("b",)), ("b", "reviewer", "b", "a", ())),
                "cycle",
            ),
            (
                (("a", "planner", "a", "", ()), ("b", "reviewer", "b", "missing", ())),
                "missing",
            ),
        ],
    )
    def test_invalid_graphs_fail_closed(self, tmp_path, nodes, message):
        node_objects = tuple(
            WorkflowNode(
                node_id=node_id,
                role=role,
                request=request,
                parent_id=parent,
                depends_on=dependencies,
            )
            for node_id, role, request, parent, dependencies in nodes
        )
        with pytest.raises(ValueError, match=message):
            WorkflowSpec(
                workflow_id="wf-invalid",
                repo_path=str(_repo(tmp_path)),
                nodes=node_objects,
                limits=_limits(),
            )

    def test_depth_fanout_and_cost_limits(self, tmp_path):
        repo = _repo(tmp_path)
        with pytest.raises(ValueError, match="max_depth"):
            WorkflowSpec(
                workflow_id="wf-depth",
                repo_path=str(repo),
                nodes=(
                    WorkflowNode("a", "planner", "a"),
                    WorkflowNode("b", "planner", "b", parent_id="a"),
                    WorkflowNode("c", "planner", "c", parent_id="b"),
                ),
                limits=_limits(max_depth=1),
            )
        with pytest.raises(ValueError, match="max_fanout"):
            WorkflowSpec(
                workflow_id="wf-fanout",
                repo_path=str(repo),
                nodes=(
                    WorkflowNode("a", "planner", "a"),
                    WorkflowNode("b", "planner", "b", parent_id="a"),
                    WorkflowNode("c", "planner", "c", parent_id="a"),
                ),
                limits=_limits(max_fanout=1),
            )
        with pytest.raises(ValueError, match="max_child_cost"):
            WorkflowSpec(
                workflow_id="wf-cost",
                repo_path=str(repo),
                nodes=(WorkflowNode("a", "implementer", "a", estimated_cost_usd=3.0),),
                limits=_limits(max_child_cost_usd=2.0),
            )


class TestClaimsAndApprovals:
    def test_claim_conflict_and_release(self, tmp_path):
        claims = ClaimStore(tmp_path)
        claims.acquire("one", ["file:src/app.py"])
        with pytest.raises(ClaimConflict):
            claims.acquire("two", ["file:src/app.py"])
        claims.release("one")
        claims.acquire("two", ["file:src/app.py"])
        assert "two" in claims.list()

    def test_rejected_approval_prevents_side_effects(self, tmp_path):
        node = WorkflowNode("implementer-node", "implementer", "edit")
        gate = SpawnApprovalGate(tmp_path / "approvals")
        assert gate.request(node, "run-1", {"estimated_cost_usd": 1.0}) is False
        assert list((tmp_path / "approvals").glob("implementer-node.run-1.*.json"))
        assert not list((tmp_path / "worktrees").glob("*"))


class TestWorktrees:
    def test_real_worktrees_capture_and_apply_dependency_patch(self, tmp_path):
        repo = _repo(tmp_path)
        manager = WorktreeManager(repo, tmp_path / "trees")
        manager.ensure_clean()
        first = manager.create("first")
        (Path(first.path) / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        patch = manager.capture_patch(first)
        second = manager.create("second", dependency_patches=[("first", patch)])
        assert (Path(second.path) / "app.py").read_text(
            encoding="utf-8"
        ) == "VALUE = 2\n"
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        with pytest.raises(WorktreeBusy):
            manager.remove(first)
        with pytest.raises(WorktreeBusy):
            manager.remove(second)
        third = manager.create("third")
        manager.remove(third)
        assert not Path(third.path).exists()

    def test_dirty_source_refuses_worktrees(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "app.py").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(WorktreeError, match="uncommitted"):
            WorktreeManager(repo, tmp_path / "trees").ensure_clean()


class TestOrchestratorSubprocess:
    def test_parent_child_sessions_handoffs_and_journal(self, tmp_path):
        repo = _repo(tmp_path)
        orchestrator = Orchestrator(_workflow(repo), logs_root=tmp_path / "logs")
        result = orchestrator.run()
        assert result["status"] == "success", result
        nodes = result["nodes"]
        assert nodes["root"]["status"] == "completed"
        assert nodes["review"]["status"] == "completed"
        assert nodes["root"]["session_id"] != nodes["review"]["session_id"]
        assert (
            orchestrator.handoff("review")["parent_run_id"] == nodes["root"]["run_id"]
        )
        assert orchestrator.handoff("review")["node_id"] == "review"
        events = orchestrator.event_log()
        assert any(event["event"] == "child_spawned" for event in events)
        assert any(event["event"] == "parent_continuation_ready" for event in events)
        assert (orchestrator.root / "orchestration.json").is_file()
        assert (orchestrator.root / "events.jsonl").is_file()

    def test_child_crash_resumes_same_run_checkpoint(self, tmp_path):
        repo = _repo(tmp_path)
        spec = WorkflowSpec(
            workflow_id="wf-crash",
            repo_path=str(repo),
            nodes=(
                WorkflowNode(
                    "only",
                    "planner",
                    "finish after recovery",
                    config={**_mock_config(), "_orchestration_fault": "crash"},
                ),
            ),
            limits=_limits(),
        )
        result = Orchestrator(spec, logs_root=tmp_path / "logs").run()
        assert result["status"] == "success", result
        assert result["nodes"]["only"]["attempts"] == 2
        events = Orchestrator(spec, logs_root=tmp_path / "logs", resume=True)
        assert events.status()["nodes"]["only"]["attempts"] == 2
        kinds = [event["event"] for event in events.event_log()]
        assert "child_crash" in kinds
        assert "child_spawned" in kinds

    def test_global_cost_reservation_blocks_before_worktree(self, tmp_path):
        repo = _repo(tmp_path)
        spec = WorkflowSpec(
            workflow_id="wf-budget",
            repo_path=str(repo),
            nodes=(
                WorkflowNode(
                    "costly",
                    "planner",
                    "too expensive",
                    estimated_cost_usd=2.0,
                ),
            ),
            limits=_limits(max_total_cost_usd=1.0, max_child_cost_usd=2.0),
        )
        result = Orchestrator(spec, logs_root=tmp_path / "logs").run()
        assert result["status"] == "blocked"
        assert result["nodes"]["costly"]["status"] == "blocked"
        assert not list(
            (tmp_path / "logs" / "orchestrations" / "wf-budget" / "worktrees").glob(
                "*/"
            )
        )

    @pytest.mark.skipif(not _docker_up(), reason="docker daemon not reachable")
    def test_real_docker_verifier_child_runs_in_isolated_worktree(self, tmp_path):
        repo = _repo(tmp_path)
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text(
            "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add verifier")
        turns = [
            json.dumps(
                {
                    "tool": "write",
                    "arguments": {"path": "app.py", "content": "VALUE = 2\n"},
                }
            ),
            _FINISH,
        ]
        spec = WorkflowSpec(
            workflow_id="wf-docker-verified",
            repo_path=str(repo),
            nodes=(
                WorkflowNode(
                    "implementer",
                    "implementer",
                    "make the verifier pass",
                    config={**_mock_config(), "orchestration_turns": turns},
                    verification_policy={"target_test": "tests/test_app.py"},
                ),
            ),
            limits=_limits(
                max_child_retries=0,
                max_child_wallclock_s=120.0,
                max_workflow_wallclock_s=240.0,
            ),
        )
        result = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            approval_callback=lambda _payload: True,
        ).run()
        assert result["status"] == "success", result
        assert (
            result["nodes"]["implementer"]["result"]["status"] == "completed_verified"
        )
        assert result["nodes"]["implementer"]["result"]["changed_files"] == ["app.py"]
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert (
            Path(result["nodes"]["implementer"]["workspace_path"], "app.py").read_text(
                encoding="utf-8"
            )
            == "VALUE = 2\n"
        )

    def test_mutating_node_requires_approval_before_workspace_creation(self, tmp_path):
        repo = _repo(tmp_path)
        spec = WorkflowSpec(
            workflow_id="wf-approval",
            repo_path=str(repo),
            nodes=(
                WorkflowNode(
                    "implementer",
                    "implementer",
                    "mutate after approval",
                    file_scopes=("app.py",),
                ),
            ),
            limits=_limits(),
        )
        result = Orchestrator(spec, logs_root=tmp_path / "logs").run()
        assert result["status"] == "blocked"
        assert result["nodes"]["implementer"]["status"] == "blocked"
        assert "implementer" not in result["worktrees"]

    def test_claim_conflict_blocks_second_node_without_spawn(self, tmp_path):
        repo = _repo(tmp_path)
        spec = WorkflowSpec(
            workflow_id="wf-claim-conflict",
            repo_path=str(repo),
            nodes=(
                WorkflowNode("first", "planner", "one", file_scopes=("shared.py",)),
                WorkflowNode("second", "planner", "two", file_scopes=("shared.py",)),
            ),
            limits=_limits(max_concurrency=2),
        )

        def child(packet):
            return {
                "status": "completed_unverified",
                "run_id": packet["run_id"],
                "session_id": packet["session_id"],
            }

        result = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            child_executor=child,
        ).run()
        assert result["status"] == "blocked"
        assert result["nodes"]["second"]["status"] == "blocked"
        assert "second" not in result["worktrees"]
        assert not (
            tmp_path
            / "logs"
            / "orchestrations"
            / "wf-claim-conflict"
            / "children"
            / "second"
        ).exists()

    def test_child_wall_timeout_retries_and_resumes(self, tmp_path):
        repo = _repo(tmp_path)
        spec = WorkflowSpec(
            workflow_id="wf-timeout",
            repo_path=str(repo),
            nodes=(
                WorkflowNode(
                    "only",
                    "planner",
                    "finish after timeout",
                    config={**_mock_config(), "_orchestration_fault": "hang"},
                ),
            ),
            limits=_limits(max_child_wallclock_s=3.0, max_workflow_wallclock_s=15.0),
        )
        result = Orchestrator(spec, logs_root=tmp_path / "logs").run()
        assert result["status"] == "success", result
        assert result["nodes"]["only"]["attempts"] == 2
        assert any(
            event["event"] == "child_timeout"
            for event in Orchestrator(
                spec, logs_root=tmp_path / "logs", resume=True
            ).event_log()
        )


class TestAutomation:
    def test_disabled_by_default(self, tmp_path):
        from runtime.config import DEFAULTS

        assert DEFAULTS["orchestration_automation_enabled"] is False
        ingress = AutomationIngress(tmp_path)
        with pytest.raises(AutomationDisabled):
            ingress.submit(_workflow(_repo(tmp_path)), idempotency_key="event-1")

    def test_idempotent_queue_schedule_and_authenticated_webhook(self, tmp_path):
        repo = _repo(tmp_path)
        ingress = AutomationIngress(
            tmp_path,
            AutomationConfig(enabled=True, webhook_token="webhook-secret"),
        )
        first = ingress.submit(
            _workflow(repo, "wf-automation"), idempotency_key="event-1"
        )
        second = ingress.submit(
            _workflow(repo, "wf-automation"), idempotency_key="event-1"
        )
        assert first["queue_id"] == second["queue_id"]
        with pytest.raises(IdempotencyConflict):
            ingress.submit(_workflow(repo, "wf-other"), idempotency_key="event-1")
        ingress.register_schedule(
            "daily", _workflow(repo, "wf-scheduled"), interval_s=60
        )
        assert ingress.due(now=time.time() + 61)
        receipt = ingress.accept_webhook(
            _workflow(repo, "wf-webhook"),
            authorization="Bearer webhook-secret",
            idempotency_key="event-2",
        )
        assert receipt["status"] == "queued"
        assert len(ingress.pending()) == 2
        assert (
            ingress.acknowledge(receipt["queue_id"], success=True)["status"]
            == "completed"
        )
        with pytest.raises(AutomationDisabled):
            AutomationIngress(tmp_path).submit(
                _workflow(repo, "wf-disabled"), idempotency_key="event-3"
            )
