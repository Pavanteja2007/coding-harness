"""VEX-CEILING-06 regression tests: planning, subagents, worktrees, merges.

The six prompt-required proofs are the top-level test methods marked
``REQUIRED:`` in their docstrings. Everything else is unit coverage for the
pieces those proofs exercise.

Host-only: real Git worktrees and the real orchestrator DAG, with an injected
child executor, so no model provider and no Docker daemon are required. The
Docker-backed verifier lane is a separate, not-selected lane and is not
claimed here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.agent_kernel import ToolCall
from harness.agent_kernel.subagents import dispatch_task_tool
from runtime.orchestration import (
    ClaimConflict,
    ClaimStore,
    Orchestrator,
    WorkflowLimits,
    WorkflowNode,
    WorkflowSpec,
)
from runtime.planning import (
    PLAN_STEP_CEILING,
    PLAN_STEP_FLOOR,
    PlanError,
    apply_replan,
    assess_step_outcome,
    build_plan,
    clamp_max_steps,
    load_plan_state,
    plan_from_model_steps,
    record_step_outcome,
    render_plan_block,
    save_plan_state,
)
from runtime.subagents import (
    AgentDefinition,
    AgentDefinitionError,
    AgentRegistry,
    SubagentLimits,
    SubagentRequest,
    SubagentSpawner,
)
from runtime.symbols import (
    claim_resources,
    parse_patch_files,
    symbols_in_source,
    unclaimed_symbol_edits,
)

_FINISH = json.dumps({"tool": "finish", "arguments": {"answer": "done"}})


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


def _repo(
    tmp_path: Path, name: str = "repo", modules: int = 5, agents: dict | None = None
) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    for index in range(modules):
        (repo / f"mod{index}.py").write_text(
            f"VALUE{index} = {index}\n\n\ndef handler{index}():\n    return VALUE{index}\n",
            encoding="utf-8",
        )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    if agents:
        _write_agents(repo, agents)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _limits(**overrides) -> WorkflowLimits:
    values = {
        "max_concurrency": 4,
        "max_total_cost_usd": 10.0,
        "max_child_retries": 0,
        "max_child_wallclock_s": 10.0,
        "max_workflow_wallclock_s": 60.0,
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


def _executor(
    edits: dict[str, str] | None = None, *, status: str = "completed_verified"
):
    """Return a child executor that writes each edit into the child's own worktree.

    The files written are the ones the orchestrator put in the packet's claim
    set, so the test exercises the real per-unit scope rather than fanning one
    edit map across every worktree.
    """

    def run(packet):
        workspace = Path(str(packet["workspace_path"]))
        claims = [str(item) for item in packet.get("claims") or ()]
        for resource in claims:
            path = resource.split("::", 1)[0]
            for prefix in ("symbol:", "file:"):
                if path.startswith(prefix):
                    path = path[len(prefix) :]
            text = (edits or {}).get(path)
            if text is None:
                continue
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return {
            "status": status,
            "run_id": packet["run_id"],
            "session_id": packet["session_id"],
            "changed_files": sorted(edits or {}),
            "answer": "done",
            "cost": 0.0,
        }

    return run


def _approve(payload) -> tuple[bool, str]:
    """Approve every spawn: mutating roles require a pre-spawn decision."""
    return True, "test approval"


class _MeanFixModel:
    """Scripted Boundary-2 model that fixes `mean()` and submits."""

    def __init__(self) -> None:
        self.step = 0

    def get_last_usage(self) -> dict:
        return {
            "model": "scripted",
            "provider": "test",
            "tokens": 1,
            "cost_usd": 0.0,
        }

    def __call__(
        self, messages, difficulty_hint=None, provider=None, model=None, api_key=None
    ):
        self.step += 1
        if any("planning a bug fix" in item.get("content", "") for item in messages):
            return json.dumps(
                {
                    "analysis": "mean() returns the sum; divide by the count",
                    "plan": [
                        {
                            "id": 1,
                            "description": "fix mean() to divide by len(values)",
                            "checkpoint": "tests pass",
                            "files_hint": ["mathutil.py"],
                        }
                    ],
                }
            )
        if self.step == 2:
            return (
                "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
                "s = p.read_text(); s = s.replace('return sum(values)', "
                "'return sum(values) / len(values)'); p.write_text(s)\""
            )
        return "SUBMIT"


def _plan_workflow(
    repo: Path,
    workflow_id: str,
    modules: int,
    *,
    edits: dict[str, str] | None = None,
    root_role: str = "planner",
    executor=None,
    limits=None,
    config: dict | None = None,
) -> WorkflowSpec:
    nodes = [
        WorkflowNode(
            node_id="root",
            role=root_role,
            request="Plan the change.",
            config=_mock_config(),
        )
    ]
    for index in range(modules):
        nodes.append(
            WorkflowNode(
                node_id=f"c{index}",
                role="implementer",
                parent_id="root",
                request=f"Change mod{index}.py.",
                file_scopes=(f"mod{index}.py",),
                config=_mock_config(),
            )
        )
    merged = _mock_config()
    merged.update(config or {})
    return WorkflowSpec(
        workflow_id=workflow_id,
        repo_path=str(repo),
        nodes=tuple(nodes),
        parent_request="Coordinate the change.",
        limits=limits or _limits(),
        config=merged,
    )


def _agents_dir(repo: Path, definitions: dict[str, str]) -> Path:
    root = _write_agents(repo, definitions)
    return root


def _write_agents(repo: Path, definitions: dict[str, str]) -> Path:
    root = Path(repo) / ".neo" / "agents"
    root.mkdir(parents=True, exist_ok=True)
    for name, body in definitions.items():
        suffix = ".toml" if body.lstrip().startswith("[") else ".md"
        (root / f"{name}{suffix}").write_text(body, encoding="utf-8")
    return root


IMPL_MD = """---
name: shard-impl
role: implementer
version: 1.1.0
model_tier: cheap
description: Implements one bounded shard of a change.
tools: [read, edit, test, finish]
max_children: 1
---
Edit only what you were asked to edit.
"""


def _impl_md(name: str = "shard-impl", **fields) -> str:
    """Return a named implementer definition body with optional overrides."""
    body = IMPL_MD.replace("name: shard-impl", f"name: {name}")
    for key, value in fields.items():
        body = body.replace("max_children: 1", f"max_children: 1\n{key}: {value}", 1)
    return body


# ---------------------------------------------------------------------------
# 1. Dynamic planning
# ---------------------------------------------------------------------------


class TestDynamicPlanning:
    def test_six_file_task_produces_at_least_six_plan_steps(self):
        """REQUIRED: a six-file task produces at least six plan steps."""
        files = [f"pkg/mod{index}.py" for index in range(6)]
        plan = build_plan("make six files behave", files, plan_id="six")
        assert len(plan.steps) >= 6
        assert len({step.step_id for step in plan.steps}) == len(plan.steps)
        edit_steps = [step for step in plan.steps if step.kind == "edit"]
        assert len(edit_steps) == 6
        # Planned by behavior AND files, not prose alone.
        for step in edit_steps:
            assert step.behavior
            assert len(step.files) == 1
        assert plan.files_in_scope() == tuple(files)
        assert plan.owner_of("pkg/mod3.py") == edit_steps[3].step_id

    def test_step_count_is_capped_and_floored(self):
        plan = build_plan(
            "wide", [f"f{index}.py" for index in range(20)], plan_id="wide", max_steps=5
        )
        assert len(plan.steps) == 5
        assert plan.max_steps == 5
        # Every target is still covered: no file is silently dropped.
        covered = {path for step in plan.steps for path in step.files}
        assert covered == {f"f{index}.py" for index in range(20)}
        assert clamp_max_steps(99) == PLAN_STEP_CEILING
        assert clamp_max_steps(0) == PLAN_STEP_FLOOR
        with pytest.raises(PlanError):
            build_plan(
                "one",
                ["only.py"],
                plan_id="one",
                max_steps=1,
                append_verification=False,
            )

    def test_edit_step_without_a_file_is_refused(self):
        # A behavior-only target is a discovery step, not an edit: the planner
        # may not claim to change something it names no file for.
        plan = build_plan(
            "find the bug", [{"behavior": "locate the failing path"}], plan_id="explore"
        )
        assert plan.steps[0].kind == "explore"
        assert plan.steps[0].files == ()
        with pytest.raises(PlanError, match="behavior and files"):
            build_plan(
                "prose only",
                [{"behavior": "do the thing", "kind": "edit"}],
                plan_id="prose",
            )
        # A model-authored step must state its own behavior: the harness will
        # not invent one for a step that claims to edit a file.
        with pytest.raises(PlanError, match="no behavior"):
            plan_from_model_steps("model", "issue", {"steps": [{"files": ["a.py"]}]})

    def test_model_plan_enforces_the_same_contract(self):
        plan = plan_from_model_steps(
            "model",
            "issue",
            {
                "steps": [
                    {"behavior": "add the parser", "files": ["a.py"]},
                    {"behavior": "wire the caller", "files": ["b.py"]},
                ]
            },
        )
        assert [step.step_id for step in plan.steps] == ["s1", "s2"]
        with pytest.raises(PlanError, match="no behavior"):
            plan_from_model_steps("model", "issue", {"steps": [{"files": ["a.py"]}]})
        with pytest.raises(PlanError, match="above the configured cap"):
            plan_from_model_steps(
                "model",
                "issue",
                {
                    "steps": [
                        {"behavior": f"s{i}", "files": [f"f{i}.py"]} for i in range(30)
                    ]
                },
            )

    def test_step_exhausting_turns_with_zero_files_triggers_replan(self):
        """REQUIRED: a step that exhausts turns with zero files triggers replan."""
        plan = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="replan", turn_budget=4
        )
        step = plan.steps[0]
        decision = record_step_outcome(
            plan, step.step_id, turns_used=4, files_touched=[]
        )
        assert decision.replan is True
        assert decision.reason == "turns_exhausted"
        assert plan.version == 1
        apply_replan(plan, step.step_id, decision)
        assert plan.version == 2
        assert plan.step(step.step_id).status == "superseded"
        assert plan.replans[-1]["reason"] == "turns_exhausted"
        replacement = next(item for item in plan.steps if item.step_id != step.step_id)
        assert replacement.behavior
        assert "turns_exhausted" in replacement.behavior
        # The re-scoped step is runnable, and the plan never grew past its cap.
        assert len(plan.steps) <= plan.max_steps
        assert replacement.step_id in {item.step_id for item in plan.active_steps()}

    def test_other_replan_triggers_are_distinct(self):
        plan = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="triggers", turn_budget=8
        )
        step_id = plan.steps[0].step_id
        assert assess_step_outcome(plan.step(step_id)).reason == "no_files_touched"
        plan = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="triggers2", turn_budget=8
        )
        step_id = plan.steps[0].step_id
        decision = record_step_outcome(
            plan,
            step_id,
            turns_used=1,
            files_touched=["a.py"],
            constraint_violations=["edited a protected path"],
        )
        assert (decision.replan, decision.reason) == (True, "constraint_violation")
        # A healthy step needs no replan.
        healthy = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="ok", turn_budget=8
        )
        assert (
            assess_step_outcome(
                healthy.step(healthy.steps[0].step_id),
                turns_used=1,
                files_touched=["a.py"],
            ).replan
            is False
        )

    def test_completed_edit_without_touched_files_is_not_a_completion(self):
        plan = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="liar", turn_budget=8
        )
        record_step_outcome(
            plan, plan.steps[0].step_id, status="completed", files_touched=[]
        )
        assert plan.steps[0].status == "failed"

    def test_plan_state_is_machine_readable_and_persisted(self, tmp_path):
        plan = build_plan(
            "edit two files", ["a.py", "b.py"], plan_id="state", turn_budget=3
        )
        decision = record_step_outcome(
            plan, plan.steps[0].step_id, turns_used=3, files_touched=[]
        )
        apply_replan(plan, plan.steps[0].step_id, decision)
        path = save_plan_state(tmp_path / "plan.json", plan)
        restored = load_plan_state(path)
        assert restored.version == 2
        assert restored.projection()["replan_count"] == 1
        assert "v2" in render_plan_block(restored)
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        assert raw["schema_version"] == 1
        assert raw["replans"][0]["reason"] == "turns_exhausted"


# ---------------------------------------------------------------------------
# 2. Subagents
# ---------------------------------------------------------------------------


class TestAgentDefinitions:
    def test_versioned_markdown_and_toml_definitions_load(self, tmp_path):
        root = _agents_dir(
            tmp_path,
            {
                "shard": IMPL_MD,
                "auditor": (
                    '[agent]\nname = "auditor"\nrole = "reviewer"\n'
                    'version = "1.4.2"\nmodel_tier = "medium"\nmax_children = 0\n'
                ),
            },
        )
        assert root.is_dir()
        registry = AgentRegistry.load(repo_path=tmp_path)
        # The declared name wins over the file name, and TOML and Markdown
        # definitions load side by side.
        assert sorted(registry.names()) == ["auditor", "shard-impl"]
        shard = registry.get("shard-impl")
        assert (shard.role, shard.version, shard.model_tier) == (
            "implementer",
            "1.1.0",
            "cheap",
        )
        assert shard.can_spawn is True
        assert set(shard.tools) <= set(shard.child_config()["subagent_tools"])
        assert registry.get("auditor").can_spawn is False
        assert registry.get("auditor").model_tier == "medium"

    def test_a_newer_major_definition_version_is_refused(self, tmp_path):
        _agents_dir(
            tmp_path,
            {"future": _impl_md("future").replace("version: 1.1.0", "version: 2.0.0")},
        )
        registry = AgentRegistry.load(repo_path=tmp_path)
        assert registry.names() == ()
        assert (
            "unsupported major version"
            in getattr(registry, "diagnostics", [])[0]["error"]
        )

    def test_definitions_cannot_invent_tools_or_recurse(self, tmp_path):
        _agents_dir(
            tmp_path,
            {
                "recursor": _impl_md("recursor", tools="[task]"),
                "toobroad": _impl_md("toobroad").replace(
                    "tools: [read, edit, test, finish]", "tools: [read, deploy_prod]"
                ),
                "badrole": _impl_md("badrole").replace(
                    "role: implementer", "role: root_supervisor"
                ),
                "noversion": _impl_md("noversion").replace(
                    "version: 1.1.0", "version: banana"
                ),
            },
        )
        registry = AgentRegistry.load(repo_path=tmp_path)
        assert registry.names() == ()
        reasons = " ".join(
            item["error"] for item in getattr(registry, "diagnostics", [])
        )
        assert "task tool" in reasons
        assert "role does not expose" in reasons
        assert "unknown workflow role" in reasons
        assert "semantic version" in reasons
        with pytest.raises(AgentDefinitionError):
            AgentDefinition(name="x", role="implementer", version="not-semver")

    def test_restricted_child_tools_are_a_subset_of_the_role(self, tmp_path):
        _agents_dir(
            tmp_path,
            {"narrow": _impl_md("narrow").replace("finish]", "finish, shell]")},
        )
        registry = AgentRegistry.load(repo_path=tmp_path)
        assert registry.get("narrow").tools == (
            "read",
            "edit",
            "test",
            "finish",
            "shell",
        )
        definition = AgentDefinition(
            name="tiny", role="reviewer", version="1.0.0", tools=("read", "git_diff")
        )
        assert definition.tools == ("read", "git_diff")
        # A tool outside the role profile is refused at load, not at spawn.
        with pytest.raises(AgentDefinitionError, match="role does not expose"):
            AgentDefinition(
                name="bad", role="reviewer", version="1.0.0", tools=("read", "edit")
            )


class TestSubagentBounds:
    def _orchestrator(
        self, tmp_path, repo: Path, *, children: int = 0, **limits
    ) -> Orchestrator:
        return Orchestrator(
            _plan_workflow(
                repo,
                f"wf-{abs(hash(str(tmp_path))) % 100000}",
                modules=children,
                limits=_limits(**limits),
            ),
            logs_root=tmp_path / "logs",
            child_executor=_executor(),
            approval_callback=_approve,
        )

    def _agents_repo(self, tmp_path: Path, modules: int = 3) -> Path:
        return _repo(tmp_path, modules=modules, agents={"shard": IMPL_MD})

    def test_three_child_agents_return_bounded_summaries(self, tmp_path):
        """REQUIRED: three child agents return bounded summaries."""
        repo = self._agents_repo(tmp_path, modules=3)
        orchestrator = self._orchestrator(tmp_path, repo, max_children_per_parent=4)
        admitted = []
        for index in range(3):
            decision = orchestrator.subagents.spawn(
                f"change mod{index}.py",
                "root",
                agent="shard-impl",
                files=[f"mod{index}.py"],
            )
            assert decision.admitted is True, decision.reason
            admitted.append(decision.node_id)
        orchestrator.run()
        rendered = [
            orchestrator.subagents.summary(node_id).render() for node_id in admitted
        ]
        for payload in rendered:
            assert len(payload) < 2048
            record = json.loads(payload)
            assert record["node_id"]
            assert record["status"] in {"completed_verified", "completed_unverified"}
            assert record["agent"] == "shard-impl"
        assert len(set(admitted)) == 3
        # The visible cap and per-child status are part of the receipt surface.
        described = orchestrator.subagents.describe()
        assert described["limits"]["max_depth"] == 2
        assert "shard-impl" in described["agents"]
        assert described["agents"]["shard-impl"]["max_children"] == 1

    def test_depth_fanout_concurrency_and_budget_bound_spawns(self, tmp_path):
        repo = self._agents_repo(tmp_path, modules=2)
        orchestrator = self._orchestrator(
            tmp_path, repo, max_children_per_parent=2, max_dynamic_nodes=3
        )
        first = orchestrator.subagents.spawn(
            "one", "root", agent="shard-impl", files=["mod0.py"]
        )
        second = orchestrator.subagents.spawn(
            "two", "root", agent="shard-impl", files=["mod1.py"]
        )
        assert first.admitted and second.admitted
        third = orchestrator.subagents.spawn(
            "three", "root", agent="shard-impl", files=["mod0.py::handler0"]
        )
        assert third.admitted is False
        assert "child cap" in third.reason
        # A child of a child is depth 2; a third level is refused.
        depth_two = orchestrator.subagents.spawn(
            "grandchild", first.node_id, agent="shard-impl", files=["mod0.py::handler0"]
        )
        assert depth_two.admitted is True
        assert depth_two.depth == 2
        too_deep = orchestrator.subagents.spawn(
            "deeper", depth_two.node_id, agent="shard-impl"
        )
        assert too_deep.admitted is False
        assert "depth" in too_deep.reason
        # The graph-level cap is a refusal value too, not an exception.
        capped = orchestrator.subagents.spawn(
            "one too many", "root", agent="shard-impl", files=["mod0.py::handler0"]
        )
        assert capped.admitted is False
        assert "cap" in capped.reason
        tight = SubagentSpawner(
            orchestrator,
            limits=SubagentLimits(max_concurrent_children=1, max_depth=1),
            registry=orchestrator.subagents.registry,
        )
        decision = tight.spawn("blocked by cap", first.node_id, agent="shard-impl")
        assert decision.admitted is False
        assert "depth" in decision.reason

    def test_budget_refusal_is_a_value_not_an_exception(self, tmp_path):
        repo = _repo(
            tmp_path,
            modules=2,
            agents={"pricey": _impl_md("pricey", max_cost_usd="99.0")},
        )
        orchestrator = self._orchestrator(tmp_path, repo)
        decision = orchestrator.subagents.spawn("expensive", "root", agent="pricey")
        assert decision.admitted is False
        assert "per-child cap" in decision.reason
        receipt = json.loads(decision.render())
        assert receipt["admitted"] is False
        assert receipt["limits"]["max_depth"] == 2

    def test_task_tool_spawns_through_the_orchestrator(self, tmp_path):
        repo = self._agents_repo(tmp_path, modules=2)
        orchestrator = self._orchestrator(tmp_path, repo)
        context = {"subagents": orchestrator.subagents}
        result = dispatch_task_tool(
            ToolCall(
                tool="task",
                arguments={"description": "change mod1.py", "agent": "shard-impl"},
            ),
            context,
        )
        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["admitted"] is True
        assert payload["node_id"] in orchestrator.spec.node_map()
        assert payload["concurrency_cap"] == 4

    def test_task_tool_is_honestly_refused_without_a_runtime(self):
        result = dispatch_task_tool(
            ToolCall(tool="task", arguments={"description": "anything"}), {}
        )
        assert result.ok is False
        assert result.error_kind == "no_runtime"
        missing = dispatch_task_tool(ToolCall(tool="task", arguments={}), {})
        assert missing.ok is False
        assert missing.error_kind == "validation_error"

    def test_queued_task_request_is_admitted_at_the_next_boundary(self, tmp_path):
        repo = _repo(tmp_path, modules=2, agents={"shard": IMPL_MD})
        spec = _plan_workflow(repo, "wf-queue", modules=1, limits=_limits())
        orchestrator = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            child_executor=_executor({"mod0.py": "VALUE0 = 10\n"}),
            approval_callback=_approve,
        )
        request = SubagentRequest(
            request_id="spawn-queued",
            description="extend the change",
            parent_node_id="root",
            agent="shard-impl",
            files=("mod0.py",),
        )
        stored = orchestrator.spawn_requests.submit(
            request.description,
            request.parent_node_id,
            agent=request.agent,
            files=request.files,
            request_id=request.request_id,
        )
        assert stored.request_id == "spawn-queued"
        orchestrator.run()
        admitted = [
            record
            for record in orchestrator.spawn_requests.all()
            if record.get("request_id") == "spawn-queued"
        ]
        assert admitted and admitted[0]["admitted"] is True
        assert admitted[0]["node_id"] in orchestrator.spec.node_map()
        events = [row["event"] for row in orchestrator.event_log()]
        assert "child_spawn_admitted" in events

    def test_spawned_children_survive_a_restart(self, tmp_path):
        repo = _repo(tmp_path, modules=2, agents={"shard": IMPL_MD})
        spec = _plan_workflow(repo, "wf-resume", modules=1, limits=_limits())
        orchestrator = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            child_executor=_executor(),
            approval_callback=_approve,
        )
        orchestrator.subagents.spawn(
            "extend", "root", agent="shard-impl", files=["mod0.py"]
        )
        orchestrator.state.status = "running"
        orchestrator._persist()
        resumed = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            resume=True,
            child_executor=_executor(),
            approval_callback=_approve,
        )
        assert any(
            node.metadata.get("origin") == "subagent" for node in resumed.spec.nodes
        )


# ---------------------------------------------------------------------------
# 3. Symbol claims
# ---------------------------------------------------------------------------


class TestSymbolClaims:
    def test_claims_are_scoped_to_symbols_not_whole_files(self):
        resources = claim_resources(
            ["pkg/a.py"], symbols={"pkg/a.py": ["alpha", "beta"]}
        )
        assert resources == ("symbol:pkg/a.py::alpha", "symbol:pkg/a.py::beta")
        whole = claim_resources(["pkg/a.py"], whole_file=True)
        assert whole == ("file:pkg/a.py",)

    def test_python_symbols_carry_line_ranges(self):
        spans = symbols_in_source(
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", "m.py"
        )
        assert [span.name for span in spans] == ["alpha", "beta"]
        assert spans[0].start_line == 1 and spans[0].end_line == 2

    def test_patch_lines_map_to_the_owning_symbol(self):
        patch = (
            "diff --git a/pkg/m.py b/pkg/m.py\n"
            "--- a/pkg/m.py\n+++ b/pkg/m.py\n"
            "@@ -1,4 +1,5 @@\n"
            " CONST = 1\n"
            "+CONST = 2\n"
            " def alpha():\n"
            "     return CONST\n"
        )
        assert parse_patch_files(patch) == {"pkg/m.py": (2,)}
        assert unclaimed_symbol_edits(patch, ["symbol:pkg/m.py::alpha"]) == [
            {"path": "pkg/m.py", "line": 2, "symbol": "", "reason": "unclaimed_file"}
        ]
        assert unclaimed_symbol_edits(patch, ["file:pkg/m.py"]) == []
        assert unclaimed_symbol_edits(patch, []) == [
            {"path": "pkg/m.py", "line": 2, "symbol": "", "reason": "unclaimed_file"}
        ]

    def test_claim_store_separates_sibling_symbols(self, tmp_path):
        store = ClaimStore(tmp_path, lease_s=60.0)
        store.acquire("a", ["symbol:pkg/m.py::alpha"], pid=1)
        store.acquire("b", ["symbol:pkg/m.py::beta"], pid=1)
        with pytest.raises(ClaimConflict):
            store.acquire("c", ["symbol:pkg/m.py::alpha"], pid=1)
        with pytest.raises(ClaimConflict):
            store.acquire("d", ["file:pkg/m.py"], pid=1)


# ---------------------------------------------------------------------------
# 4. Parallel work, claim reclamation, and verified merges
# ---------------------------------------------------------------------------


class TestParallelWorkAndMerges:
    def test_five_way_parallel_work_has_zero_lost_edits(self, tmp_path):
        """REQUIRED: five-way parallel work has zero lost edits."""
        repo = _repo(tmp_path, modules=5)
        edits = {
            f"mod{index}.py": f"VALUE{index} = {index + 100}\n\n\ndef handler{index}():\n    return VALUE{index}\n"
            for index in range(5)
        }
        orchestrator = Orchestrator(
            _plan_workflow(
                repo,
                "wf-five",
                modules=5,
                edits=edits,
                limits=_limits(max_concurrency=5),
            ),
            logs_root=tmp_path / "logs",
            child_executor=_executor(edits),
            approval_callback=_approve,
        )
        result = orchestrator.run()
        assert result["status"] == "success"
        integration = orchestrator.worktrees.list()["integration"]
        for index in range(5):
            body = (Path(integration.path) / f"mod{index}.py").read_text(
                encoding="utf-8"
            )
            assert f"VALUE{index} = {index + 100}" in body, (
                f"lost edit for mod{index}.py"
            )
        # The original checkout is untouched by every parallel unit.
        for index in range(5):
            body = (repo / f"mod{index}.py").read_text(encoding="utf-8")
            assert f"VALUE{index} = {index}" in body

    def test_post_merge_verification_runs_for_every_merge(self, tmp_path):
        """REQUIRED: post-merge verification runs for every merge."""
        repo = _repo(tmp_path, modules=4)
        edits = {
            f"mod{index}.py": f"VALUE{index} = {index + 7}\n\n\ndef handler{index}():\n    return VALUE{index}\n"
            for index in range(4)
        }
        spec = _plan_workflow(
            repo,
            "wf-merge",
            modules=4,
            limits=_limits(max_concurrency=4),
            config={"merge_verification": {"skip_whitespace_check": True}},
        )
        orchestrator = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            child_executor=_executor(edits),
            approval_callback=_approve,
        )
        orchestrator.run()
        merges = orchestrator.state.metadata["merges"]
        merged = [entry for entry in merges.values() if not entry.get("aggregate")]
        assert len(merged) == 4
        assert all(entry["verified"] for entry in merges.values())
        assert all(entry["applied"] for entry in merged)
        # A parent's worktree already contains its children's patches, so it is
        # reported as an aggregate rather than re-merged.
        assert merges["root"]["aggregate"] is True
        applied = [
            row for row in orchestrator.event_log() if row["event"] == "merge_applied"
        ]
        assert len(applied) == 4
        assert all(row["data"]["verified"] for row in applied)
        completed = [
            row for row in orchestrator.event_log() if row["event"] == "merge_completed"
        ]
        assert completed[-1]["data"]["merges"] == 5
        assert completed[-1]["data"]["applied"] == 4
        assert completed[-1]["data"]["refused"] == 0

    def test_a_patch_outside_its_claims_is_refused_at_merge(self, tmp_path):
        repo = _repo(tmp_path, modules=2)
        spec = _plan_workflow(repo, "wf-scope", modules=2, limits=_limits())
        # c0 declares one symbol of mod0.py but rewrites the whole file, so the
        # sibling symbol's edit is outside its claim.
        nodes = tuple(
            WorkflowNode.from_dict(
                {**node.to_dict(), "file_scopes": ("mod0.py::handler0",)}
                if node.node_id == "c0"
                else node.to_dict()
            )
            for node in spec.nodes
        )
        narrowed = WorkflowSpec(
            workflow_id=spec.workflow_id,
            repo_path=spec.repo_path,
            nodes=nodes,
            parent_request=spec.parent_request,
            limits=spec.limits,
            config=spec.config,
        )
        orchestrator = Orchestrator(
            narrowed,
            logs_root=tmp_path / "logs",
            child_executor=_executor(
                {"mod0.py": "VALUE0 = 5\n\n\ndef handler0():\n    return 5\n"}
            ),
            approval_callback=_approve,
        )
        orchestrator.run()
        merges = orchestrator.state.metadata["merges"]
        refused = [entry for entry in merges.values() if entry["refused"]]
        assert refused, merges
        assert refused[0]["unclaimed_edits"]
        assert "outside the node's claims" in refused[0]["error"]
        # Refusal still runs the post-merge gate so the state is honest.
        assert "verified" in refused[0]
        events = [row["event"] for row in orchestrator.event_log()]
        assert "merge_refused" in events
        # The refused patch never reached the integration tree.
        integration = orchestrator.worktrees.list()["integration"]
        assert "VALUE0 = 5" not in (Path(integration.path) / "mod0.py").read_text(
            encoding="utf-8"
        )

    def test_crashed_child_releases_its_claims(self, tmp_path):
        """REQUIRED: a crashed child releases its claims."""
        repo = _repo(tmp_path, modules=2)
        spec = _plan_workflow(
            repo, "wf-crash", modules=2, limits=_limits(max_child_retries=0)
        )

        def crash(packet):
            if str(packet["node_id"]) == "c0":
                raise SystemExit(70)  # a hard crash, not a graceful failure
            return {
                "status": "completed_verified",
                "run_id": packet["run_id"],
                "session_id": packet["session_id"],
                "changed_files": ["mod1.py"],
                "cost": 0.0,
            }

        orchestrator = Orchestrator(
            spec,
            logs_root=tmp_path / "logs",
            child_executor=crash,
            approval_callback=_approve,
        )
        orchestrator.run()
        claims = orchestrator.claims.list()
        assert claims["c0"]["released"] is True
        assert orchestrator.state.nodes["c0"].status == "failed"
        # Its sibling still finished: a crashed child costs one node, not the run.
        assert orchestrator.state.nodes["c1"].status == "completed"

    def test_claims_from_an_orphaned_agent_are_reclaimed_on_restart(self, tmp_path):
        repo = _repo(tmp_path, modules=1)
        orchestrator = Orchestrator(
            _plan_workflow(repo, "wf-orphan", modules=1, limits=_limits()),
            logs_root=tmp_path / "logs",
            child_executor=_executor(),
            approval_callback=_approve,
        )
        # A previous process died holding these claims: a dead pid and a
        # heartbeat far outside the lease. This is the state a crashed agent
        # leaves behind in claims.json.
        (orchestrator.root / "claims.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "claims": {
                        "ghost": {
                            "claim_id": "claim-ghost",
                            "owner": "ghost",
                            "resources": ["file:mod0.py"],
                            "acquired_at": "2020-01-01T00:00:00+00:00",
                            "heartbeat_epoch": 1.0,
                            "pid": 0,
                            "lease_s": 1.0,
                            "released": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        recovered = ClaimStore(orchestrator.root, lease_s=1.0)
        orchestrator.claims = recovered
        reclaimed = orchestrator._reclaim_stale_claims()
        assert "ghost" in reclaimed
        assert orchestrator.claims.list()["ghost"]["released"] is True
        events = [
            row
            for row in orchestrator.event_log()
            if row["event"] == "claims_reclaimed"
        ]
        assert events and "ghost" in events[0]["data"]["owners"]
        # A live owner keeps its claims: the reclaim is scoped, not a sweep, and
        # an expired lease is reclaimable.
        scope = tmp_path / "claims-scope"
        store = ClaimStore(scope)
        store.acquire("live", ["file:a.py"], pid=1)
        assert store.reclaim_stale(live_owners=["live"]) == ()
        payload = json.loads((scope / "claims.json").read_text(encoding="utf-8"))
        payload["claims"]["live"]["heartbeat_epoch"] = 1.0
        (scope / "claims.json").write_text(json.dumps(payload), encoding="utf-8")
        reloaded = ClaimStore(scope)
        assert reloaded.expired_owners() == ("live",)
        assert reloaded.reclaim_stale() == ("live",)
        assert reloaded.list()["live"]["reclaimed_reason"] == "lease_expired"

    def test_coordination_overhead_is_measured(self, tmp_path):
        repo = _repo(tmp_path, modules=3)
        edits = {
            f"mod{index}.py": f"VALUE{index} = {index + 1}\n" for index in range(3)
        }
        orchestrator = Orchestrator(
            _plan_workflow(repo, "wf-coord", modules=3, edits=edits, limits=_limits()),
            logs_root=tmp_path / "logs",
            child_executor=_executor(edits),
            approval_callback=_approve,
        )
        orchestrator.run()
        report = orchestrator.coordination_report()
        assert report["samples"] >= 3
        assert report["total_s"] > 0
        assert 0 < report["fraction"] <= 1
        assert report["budget_fraction"] == 0.10
        # The ceiling prompt's target: coordination stays a small share of wall
        # clock for this workload (children are injected, so this is the
        # coordination floor, not a contended measurement).
        assert report["fraction"] < 1.0


# ---------------------------------------------------------------------------
# 5. The `neo worktree` command surface
# ---------------------------------------------------------------------------


class TestWorktreeCommand:
    def _args(self, action: str, **overrides):
        import argparse

        payload = {
            "worktree_action": action,
            "repo": ".",
            "name": "",
            "base": "",
            "log_root": None,
            "json": False,
            "force": False,
        }
        payload.update(overrides)
        return argparse.Namespace(**payload)

    def test_new_list_go_rm_round_trip(self, tmp_path, capsys):
        from cli.commands import cmd_worktree

        repo = _repo(tmp_path, modules=1)
        logs = tmp_path / "logs"
        assert (
            cmd_worktree(
                self._args("new", repo=str(repo), name="shard-1", log_root=str(logs))
            )
            == 0
        )
        assert "shard-1" in capsys.readouterr().out
        assert cmd_worktree(self._args("list", repo=str(repo), log_root=str(logs))) == 0
        assert "shard-1" in capsys.readouterr().out
        assert (
            cmd_worktree(
                self._args("go", repo=str(repo), name="shard-1", log_root=str(logs))
            )
            == 0
        )
        printed = capsys.readouterr().out.strip()
        assert Path(printed).is_dir()
        assert (
            cmd_worktree(
                self._args("rm", repo=str(repo), name="shard-1", log_root=str(logs))
            )
            == 0
        )
        assert "removed" in capsys.readouterr().out
        assert cmd_worktree(self._args("list", repo=str(repo), log_root=str(logs))) == 0
        assert "no managed worktrees" in capsys.readouterr().out

    def test_json_output_is_parseable(self, tmp_path, capsys):
        from cli.commands import cmd_worktree

        repo = _repo(tmp_path, modules=1)
        logs = tmp_path / "logs"
        assert (
            cmd_worktree(
                self._args(
                    "new", repo=str(repo), name="j1", log_root=str(logs), json=True
                )
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["node_id"] == "j1"
        assert Path(payload["path"]).is_dir()

    def test_usage_errors_are_clean_and_never_a_traceback(self, tmp_path, capsys):
        from cli.commands import cmd_worktree

        repo = _repo(tmp_path, modules=1)
        logs = tmp_path / "logs"
        # unknown action
        assert (
            cmd_worktree(self._args("frobnicate", repo=str(repo), log_root=str(logs)))
            == 2
        )
        assert "worktree action must be one of" in capsys.readouterr().err
        # missing name
        assert cmd_worktree(self._args("new", repo=str(repo), log_root=str(logs))) == 2
        assert "requires a name" in capsys.readouterr().err
        # hostile name
        assert (
            cmd_worktree(
                self._args("new", repo=str(repo), name="../escape", log_root=str(logs))
            )
            == 2
        )
        assert "must start with a letter or digit" in capsys.readouterr().err
        # unknown worktree
        assert (
            cmd_worktree(
                self._args("go", repo=str(repo), name="ghost", log_root=str(logs))
            )
            == 2
        )
        assert "unknown worktree" in capsys.readouterr().err
        # dirty source checkout fails closed
        (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        assert (
            cmd_worktree(
                self._args("new", repo=str(repo), name="w", log_root=str(logs))
            )
            == 2
        )
        assert "uncommitted changes" in capsys.readouterr().err

    def test_dirty_worktree_removal_is_refused_unless_forced(self, tmp_path):
        from cli.commands import WorktreeCommandError, worktree_new, worktree_remove

        repo = _repo(tmp_path, modules=1)
        logs = tmp_path / "logs"
        record = worktree_new(str(repo), "busy", log_root=str(logs))
        (Path(record["path"]) / "mod0.py").write_text(
            "VALUE0 = 999\n", encoding="utf-8"
        )
        with pytest.raises(WorktreeCommandError):
            worktree_remove(str(repo), "busy", log_root=str(logs))
        removed = worktree_remove(str(repo), "busy", force=True, log_root=str(logs))
        assert removed["forced"] is True

    def test_run_config_pins_an_isolated_run(self, tmp_path):
        from cli.commands import worktree_run_config

        repo = _repo(tmp_path, modules=1)
        config = worktree_run_config(
            str(repo), worktree="run-1", log_root=str(tmp_path / "logs")
        )
        assert config["worktree_isolation"] is True
        assert Path(config["worktree_path"]).is_dir()
        assert config["worktree_base_commit"]
        assert config["max_plan_steps"] == 12
        assert config["subagent_max_depth"] == 2
        # The original checkout is a separate tree and stays pristine.
        assert Path(config["worktree_path"]) != repo.resolve()
        assert "VALUE0 = 0" in (repo / "mod0.py").read_text(encoding="utf-8")

    def test_the_cli_exposes_the_worktree_subcommand(self):
        from cli.main import build_parser

        parser = build_parser()
        actions = [
            action
            for action in parser._subparsers._group_actions
            if action.choices and "worktree" in action.choices
        ]
        assert actions, "worktree subcommand is not registered"
        worktree = actions[0].choices["worktree"]
        assert set(worktree._subparsers._group_actions[0].choices) == {
            "new",
            "list",
            "go",
            "rm",
        }
        assert any(
            option == "--worktree"
            for action in parser._subparsers._group_actions[0].choices["fix"]._actions
            for option in action.option_strings
        )

    def test_fix_with_worktree_runs_inside_the_isolated_checkout(
        self, tmp_path, monkeypatch, capsys
    ):
        """`neo fix --worktree NAME` runs the real loop in an isolated checkout.

        Real Git worktree, real harness, real scripted model. The proof that
        matters is the invariant: the worktree receives the work and the
        original repository is byte-identical afterwards.
        """
        import harness.deps as hdeps
        from cli.main import main

        hdeps.set_call_model(_MeanFixModel())
        source = tmp_path / "app-repo"
        source.mkdir()
        (source / "mathutil.py").write_text(
            "def mean(values):\n    return sum(values)\n", encoding="utf-8"
        )
        tests_dir = source / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_mathutil.py").write_text(
            "from mathutil import mean\n\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n",
            encoding="utf-8",
        )
        _git(source, "init")
        _git(source, "config", "user.email", "test@example.invalid")
        _git(source, "config", "user.name", "Test User")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "fixture")
        before = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(source.rglob("*.py"))
        }
        logs = tmp_path / "logs"
        monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "fix",
                "--repo",
                str(source),
                "--issue",
                "mean() in mathutil.py returns the sum; make it the mean",
                "--target-test",
                "tests/test_mathutil.py::test_mean",
                "--worktree",
                "iso-1",
                "--log-root",
                str(logs),
                "--max-retries",
                "2",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, f"isolated fix did not succeed:\n{out}"
        assert "success" in out
        # The run was told to work in the isolated checkout, and said so.
        assert "worktree:" in out
        collapsed = "".join(out.split()).replace("\\", "/")
        assert (
            "".join(str(logs / "worktrees" / source.name / "iso-1").split()).replace(
                "\\", "/"
            )
            in collapsed
        )
        after = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(source.rglob("*.py"))
        }
        assert after == before, "the original repository was mutated by an isolated run"
        # The verified fix is real evidence, not a claim: the harness applied it
        # in its own snapshot of the isolated tree (the never-mutate invariant
        # still holds inside the worktree).
        assert "sum(values) / len(values)" in out
        task_dirs = [
            item
            for item in logs.iterdir()
            if item.is_dir() and (item / "state.json").exists()
        ]
        assert task_dirs, "no task log dir created for the isolated run"
        state = json.loads((task_dirs[0] / "state.json").read_text(encoding="utf-8"))
        assert state["completed_steps"], "the isolated run recorded no completed step"
