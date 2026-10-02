"""AGT-05 — plan-phase research isolation.

Five properties, each named after the behaviour it must have:

1. **Planning cannot mutate.** A write in plan mode is not a policy denial, it
   is a tool the researcher does not have — the failure happens at validation,
   before any handler could run.
2. **Exploration text does not appear in the main context.** The researcher
   reads files; the executor's first request contains the plan and the
   citations and NONE of the file bodies. Proven by putting a unique marker in
   a source file and searching the executor's actual request.
3. **The subagent's return is size-capped.** A 200 000-character plan comes
   back bounded, with the omission stated rather than silent.
4. **A separate planning model is used when configured, and both parts are
   reported.** The architect/coder split, and the receipt names which model
   planned, which model executes, and whether the configured key was honoured.
5. **The approved plan is the plan that executes.** Approving plan A and
   executing plan B is refused, not performed quietly.

Every proof here is behavioural and host-only: no Docker, no provider, no
network. The verifier gate is untouched and pinned by reading the mint
condition, because nothing in this round may make ``completed_unverified``
reachable as success.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from harness.agent_kernel import subagents as subagents_mod
from harness.agent_kernel.contracts import ToolCall
from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.subagents import (
    DEFAULT_PLAN_MAX_CHARS,
    PlanResearchSubagent,
    plan_mode_capabilities,
    plan_mode_is_read_only,
    plan_mode_tools,
    plan_mode_withheld,
)
from harness.agent_kernel.tools import ToolRegistry, ToolValidationError

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: A marker that exists ONLY in a source file's body. If it ever appears in
#: the executor's request, exploration text leaked into the main window.
EXPLORATION_MARKER = "ZZPLANPHASEMARKERZZ-internal-implementation-detail"

#: A marker that exists only in the researcher's own intermediate reasoning
#: (a tool result it read and then summarised in prose). It is a stronger leak
#: test than the file marker: it proves the TRANSCRIPT is gone, not just the
#: file the transcript was about.
REASONING_MARKER = "ZZRESEARCHTRANSCRIPTZZ-observed-three-candidates"


def _repo(tmp_path: Path) -> Path:
    """A small repository whose source carries the exploration markers."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "billing.py").write_text(
        "\n".join(
            [
                f"# {EXPLORATION_MARKER}",
                "def charge(amount, tax=0.0):",
                '    """Charge an amount."""',
                "    return amount + amount * tax",
                "",
                "",
                "def refund(amount):",
                '    """Refund an amount."""',
                "    return -amount",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_billing.py").write_text(
        "from pkg.billing import charge\n\n\n"
        "def test_charge():\n    assert charge(10, 0.1) == 11.0\n",
        encoding="utf-8",
    )
    return root


class _Scripted:
    """A model boundary that replays canned replies and records its requests.

    Records every request it was handed, which is what makes "exploration text
    does not appear in the main context" a measurement rather than an
    assertion about the code.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[list[dict]] = []
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        self.requests.append([dict(m) for m in messages])
        if not self.replies:
            return json.dumps({"tool": "finish", "answer": "no more input"})
        return self.replies.pop(0)

    def get_last_usage(self):
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def _researcher(tmp_path, replies, **kwargs):
    """Build a plan-phase researcher over a real repo with real read handlers."""
    from harness.agent_kernel.strategy import build_default_handlers

    root = _repo(tmp_path)
    registry = ToolRegistry()
    registry.restrict(plan_mode_tools())
    build_default_handlers(registry, repo_path=str(root), config={})
    model = _Scripted(replies)
    researcher = PlanResearchSubagent(
        registry=registry,
        gateway=ModelGateway(call_fn=model),
        context={"config": {}},
        **kwargs,
    )
    return researcher, model, root


# ---------------------------------------------------------------------------
# 1. planning cannot mutate
# ---------------------------------------------------------------------------


def test_a_write_is_not_a_tool_the_researcher_has():
    """The gate is the tool list, so a write fails VALIDATION, not policy."""
    registry = ToolRegistry()
    registry.restrict(plan_mode_tools())

    for tool in ("edit", "write", "apply_patch", "delete", "rename", "undo"):
        with pytest.raises(ToolValidationError) as excinfo:
            registry.validate(ToolCall(tool=tool, arguments={"path": "pkg/billing.py"}))
        assert "unknown tool" in str(excinfo.value)
        assert tool not in registry.canonical_names


def test_a_shell_command_is_not_a_tool_the_researcher_has():
    """Command execution is withheld structurally, for the same reason."""
    registry = ToolRegistry()
    registry.restrict(plan_mode_tools())

    for tool in ("shell", "bash", "process", "test", "verify", "lint", "build"):
        assert tool not in registry.canonical_names
        with pytest.raises(ToolValidationError):
            registry.validate(ToolCall(tool=tool, arguments={"command": "rm -rf /"}))


def test_the_researcher_refuses_a_write_even_with_a_permissive_policy():
    """Defence in depth: the refusal does not depend on the gate being built right.

    The registry restriction is the primary gate, but a caller that handed the
    researcher an unrestricted registry must still get a refusal rather than a
    write. This is the assertion that keeps the property from living in one
    place.
    """
    unrestricted = ToolRegistry()

    class _AlwaysAllows:
        def evaluate(self, call):
            class _Decision:
                allowed = True
                needs_approval = False
                terminal = False
                reason = ""
                exact_effect = "anything"

            return _Decision()

    researcher = PlanResearchSubagent(
        registry=unrestricted,
        gateway=ModelGateway(
            call_fn=_Scripted(
                [
                    json.dumps(
                        {
                            "tool": "write",
                            "arguments": {
                                "path": "pkg/billing.py",
                                "content": "OWNED = True",
                            },
                        }
                    ),
                    json.dumps({"tool": "finish", "answer": "1. do a thing"}),
                ]
            )
        ),
        policy=_AlwaysAllows(),
        context={"config": {}},
    )
    result = researcher.run("make billing better")

    assert result.plan.strip(), result.plan
    # The write never reached the registry: the researcher refused it on the
    # capability gate, before dispatch.
    assert "OWNED = True" not in json.dumps(result.to_dict())


def test_a_real_write_in_the_researcher_never_lands_on_disk(tmp_path):
    """The end-to-end version: the file is byte-identical after the run."""
    root = _repo(tmp_path)
    target = root / "pkg" / "billing.py"
    before = target.read_bytes()

    from harness.agent_kernel.strategy import build_default_handlers

    registry = ToolRegistry()
    registry.restrict(plan_mode_tools())
    build_default_handlers(registry, repo_path=str(root), config={})
    researcher = PlanResearchSubagent(
        registry=registry,
        gateway=ModelGateway(
            call_fn=_Scripted(
                [
                    json.dumps(
                        {
                            "tool": "write",
                            "arguments": {
                                "path": "pkg/billing.py",
                                "content": "OWNED = True",
                                "expected_revision": "0" * 64,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "tool": "finish",
                            "answer": "1. update the tax calculation",
                        }
                    ),
                ]
            )
        ),
        context={"config": {}},
    )
    result = researcher.run("make billing better")

    assert target.read_bytes() == before, "plan research mutated the repository"
    assert "1. update the tax calculation" in result.plan


def test_the_gate_is_derived_from_the_catalog_not_a_hand_list():
    """The gate follows the CAPABILITY surface, so it cannot drift from it.

    The invariant is about capabilities, not effect classes: a tool is reachable
    exactly when the capability it belongs to is granted. ``ask`` and ``finish``
    are ``control``-class tools and are therefore reachable - a researcher that
    could not end its own turn could not return a plan at all - while a
    ``workspace_write`` tool never is. Asserting on effect classes instead would
    encode the wrong rule and would have "failed" on the two tools the mechanism
    needs.
    """
    from harness.agent_kernel import kernel as kernel_mod
    from harness.agent_kernel.subagents import PLAN_RESEARCH_CAPABILITIES
    from harness.tools import typed_tool_specs

    surface = kernel_mod.capability_surface()
    allowed = set(plan_mode_tools())
    granted = {
        tool
        for capability in PLAN_RESEARCH_CAPABILITIES
        for tool in surface.get(capability, ())
    } - set(subagents_mod.PLAN_RESEARCH_WITHHELD_TOOLS)

    assert allowed == granted, (
        "plan_mode_tools() disagrees with the capability surface it claims to "
        "be derived from"
    )
    # The gate is exhaustive over the catalog: every tool lands on exactly one
    # side of it.
    for spec in typed_tool_specs():
        capability = kernel_mod._capability_of(spec)
        if capability in PLAN_RESEARCH_CAPABILITIES and (
            spec.name not in subagents_mod.PLAN_RESEARCH_WITHHELD_TOOLS
        ):
            assert spec.name in allowed, f"{spec.name} is invisible to plan mode"
        else:
            assert spec.name not in allowed, f"{spec.name} is reachable in plan mode"


def test_the_plan_phase_reaches_exactly_the_read_and_control_capabilities():
    """Two capabilities, named, with the reason each is withheld recorded."""
    assert plan_mode_capabilities() == ("control", "read")
    for name in ("edit", "write", "apply_patch", "shell", "task", "mcp"):
        assert not plan_mode_is_read_only(name)


def test_every_withheld_capability_carries_a_reason():
    """A withheld capability without a reason is a silent degradation."""
    withheld = plan_mode_withheld()
    assert {"mutate", "shell", "network", "memory", "mcp", "subagent"} <= set(
        withheld
    ), sorted(withheld)
    for capability, reason in withheld.items():
        assert reason.strip(), f"{capability} is withheld without a reason"
    assert "read-only" in withheld["mutate"]
    assert "run commands" in withheld["shell"]


def test_the_researcher_cannot_spawn_a_child_agent():
    """A researcher that can fork work has an unbounded budget by construction."""
    assert "task" not in plan_mode_tools()
    assert not plan_mode_is_read_only("task")


def test_the_planner_role_shares_the_researchers_gate():
    """A planning ROLE that could mutate is a plan phase that can act."""
    from runtime.roles import build_role_registry, get_role_profile

    profile = get_role_profile("planner")
    assert set(profile.visible_tools) == set(plan_mode_tools())
    registry = build_role_registry("planner")
    for tool in ("edit", "write", "shell", "task", "web_fetch", "mcp"):
        assert tool not in registry.canonical_names, tool


# ---------------------------------------------------------------------------
# 2. exploration text does not enter the main window
# ---------------------------------------------------------------------------


def test_the_researcher_actually_read_the_file_it_plans_about(tmp_path):
    """The control: the marker WAS in the researcher's own window.

    Without this, "the marker is absent from the executor" could pass because
    the researcher never read anything. The leak test below is only meaningful
    next to this one.
    """
    researcher, model, _root = _researcher(
        tmp_path,
        [
            json.dumps({"tool": "read", "path": "pkg/billing.py"}),
            json.dumps({"tool": "finish", "answer": "1. fix the tax path"}),
        ],
    )
    result = researcher.run("billing tax is wrong")

    assert EXPLORATION_MARKER in json.dumps(model.requests), (
        "the researcher never saw the file, so the leak test would be vacuous"
    )
    assert result.transcript_chars > 0
    assert result.transcript_messages > 2
    assert "pkg/billing.py" in [c.target for c in result.citations]


def test_no_exploration_text_reaches_the_executors_first_request(tmp_path):
    """The required proof, end to end through the REAL kernel.

    A real ``AgentKernel`` run with ``plan_research`` on. The scripted model
    plays two roles: the researcher (two turns) and then the executor. The
    executor's FIRST request is read out of the run's own ``model_request``
    journal row, and it must contain the plan and the citation and neither
    marker.
    """
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.kernel import AgentKernel

    root = _repo(tmp_path)
    replies = [
        # --- the researcher ---
        json.dumps({"tool": "read", "path": "pkg/billing.py"}),
        f"{REASONING_MARKER}",
        json.dumps(
            {
                "tool": "finish",
                "answer": (
                    "1. Update the tax calculation in pkg/billing.py\n"
                    "2. Add a regression test for the discounted total\n"
                    "3. Run the billing suite"
                ),
            }
        ),
        # --- the executor ---
        json.dumps({"tool": "read", "path": "pkg/__init__.py"}),
        json.dumps({"tool": "finish", "answer": "planned the billing fix"}),
    ]
    model = _Scripted(replies)
    kernel = AgentKernel(
        repo_path=str(root),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "knowledge_enabled": False,
            "plan_research": True,
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="plan-iso",
            request="billing tax is computed wrongly",
            repository_identity=str(root),
        )
    )

    assert result.status in {"completed_unverified", "completed_verified"}, (
        result.status
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "plan-iso" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    # The FIRST model_request of the run is the executor's first request: the
    # plan phase's own calls go through a different gateway and are recorded
    # under a different step label.
    executor_requests = [
        row
        for row in rows
        if row.get("event") == "model_request"
        and str((row.get("data") or {}).get("step") or "").startswith("agent-")
    ]
    assert executor_requests, [row.get("event") for row in rows]
    first = json.dumps(executor_requests[0]["data"]["messages"])

    # the plan and the citation ARE there
    assert "Update the tax calculation" in first
    assert "pkg/billing.py" in first
    # the exploration is NOT there
    assert EXPLORATION_MARKER not in first, "file body leaked into the main window"
    assert REASONING_MARKER not in first, "research transcript leaked into the window"

    # And the researcher really did hold them.
    research = [row for row in rows if row.get("event") == "plan_research"]
    assert research, [row.get("event") for row in rows]
    receipt = research[0]["data"]
    assert receipt["enabled"] is True
    assert receipt["transcript_chars"] > 0
    assert receipt["transcript_messages"] > 2


def test_the_receipt_carries_no_field_that_could_hold_a_transcript():
    """The isolation is a property of the TYPE, not of a code path.

    A future field added to the result could quietly reintroduce the leak, so
    the assertion is on the shape: the receipt has counters and a digest, and
    no free-text transcript.
    """
    payload = PlanResearchSubagent(
        registry=ToolRegistry(),
        gateway=ModelGateway(call_fn=_Scripted([])),
    ).run("x")
    keys = set(payload.to_dict())
    assert keys == {
        "plan",
        "citations",
        "turns",
        "tool_calls",
        "cost_usd",
        "truncated",
        "finished",
        "model",
        "model_configured",
        "model_honoured",
        "stopped_because",
        "transcript_messages",
        "transcript_chars",
        "transcript_digest",
        "max_chars",
        "max_turns",
        "max_tool_calls",
        "max_cost_usd",
        "tools",
    }
    assert isinstance(payload.transcript_digest, str) and payload.transcript_digest


# ---------------------------------------------------------------------------
# 3. the return is size-capped and summarised
# ---------------------------------------------------------------------------


def test_a_very_long_plan_comes_back_bounded_and_says_so(tmp_path):
    """A 200 000-character plan must not become a 200 000-character context cost."""
    huge = "\n".join(
        f"step {n}: change something in module_{n}.py" for n in range(9000)
    )
    assert len(huge) > 100_000
    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "finish", "answer": huge})],
    )
    result = researcher.run("do a lot of work")

    assert len(result.plan) <= DEFAULT_PLAN_MAX_CHARS, len(result.plan)
    assert result.truncated is True
    assert "chars omitted from the plan" in result.plan, (
        "a cap that is silent is a lie about how much the executor is reading"
    )
    # head AND tail survive: the first step and the last step are both load-bearing
    assert "step 0:" in result.plan
    assert "step 8999:" in result.plan


def test_the_cap_comes_from_the_runs_own_config(tmp_path):
    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "finish", "answer": "x" * 5000})],
        max_chars=200,
    )
    result = researcher.run("bounded")

    assert len(result.plan) <= 200
    assert result.max_chars == 200
    assert result.truncated is True


def test_a_zero_cap_returns_nothing_rather_than_everything(tmp_path):
    """0 must mean "return nothing", not "no cap applied"."""
    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "finish", "answer": "x" * 5000})],
        max_chars=0,
    )
    result = researcher.run("bounded")

    assert result.plan == ""
    assert result.truncated is True
    assert result.finished is True, "the researcher did finish; only the return was cut"


def test_the_turn_and_tool_call_budgets_are_enforced(tmp_path):
    """An unbounded researcher is not a bounded researcher."""
    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "read", "path": "pkg/billing.py"})] * 10,
        max_turns=3,
        max_tool_calls=2,
    )
    result = researcher.run("explore a lot")

    assert result.turns <= 3, result.turns
    assert result.tool_calls <= 2, result.tool_calls
    assert result.stopped_because, "a stopped run must say which bound stopped it"
    assert result.plan == "", "an unfinished researcher must not invent a plan"


def test_the_spend_budget_is_enforced_and_named(tmp_path):
    """The dollar bound is checked BEFORE the next call, not after the fact."""

    class _Expensive(_Scripted):
        def get_last_usage(self):
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.5,
            }

    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "read", "path": "pkg/billing.py"})] * 5,
        max_cost_usd=0.6,
    )
    researcher.gateway = ModelGateway(
        call_fn=_Expensive([json.dumps({"tool": "read", "path": "pkg/billing.py"})] * 5)
    )
    result = researcher.run("explore")

    assert result.stopped_because == "budget", result.stopped_because
    assert result.turns <= 2, result.turns


def test_an_empty_plan_is_honest_and_never_invents_one(tmp_path):
    """A model that cannot produce a plan produces NO plan.

    The researcher asks once for a real plan and then gives up rather than
    looping on an empty answer, so the scripted model runs out of input and the
    run ends. The point is the plan is empty: no placeholder, no restatement of
    the request, nothing the executor could mistake for a plan.
    """
    researcher, _model, _root = _researcher(
        tmp_path,
        [json.dumps({"tool": "finish", "answer": "   "})] * 3,
        max_turns=3,
    )
    result = researcher.run("plan something")

    assert result.plan == "", result.plan
    assert result.finished is False
    assert result.stopped_because, "an unfinished researcher must say why it stopped"


def test_a_model_failure_degrades_to_no_plan_not_a_crash(tmp_path):
    def _boom(messages, **kwargs):
        raise RuntimeError("provider is down")

    researcher, _model, _root = _researcher(tmp_path, [])
    researcher.gateway = ModelGateway(call_fn=_boom)
    result = researcher.run("plan something")

    assert result.plan == ""
    assert result.stopped_because.startswith("model_error"), result.stopped_because


# ---------------------------------------------------------------------------
# 4. a separate planning model, configured and reported
# ---------------------------------------------------------------------------


def test_a_configured_plan_model_is_used_for_planning_and_reported(tmp_path):
    """The architect/coder split, through the REAL kernel."""
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.kernel import AgentKernel

    root = _repo(tmp_path)
    plan_calls: list[dict] = []
    run_calls: list[dict] = []

    class _Split(_Scripted):
        def __call__(self, messages, **kwargs):
            (
                plan_calls if kwargs.get("model") == "architect-model" else run_calls
            ).append(dict(kwargs))
            return super().__call__(messages, **kwargs)

    replies = [
        json.dumps({"tool": "read", "path": "pkg/billing.py"}),
        json.dumps({"tool": "finish", "answer": "1. fix the tax path"}),
        json.dumps({"tool": "finish", "answer": "did the billing fix"}),
    ]
    kernel = AgentKernel(
        repo_path=str(root),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=_Split(replies)),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "knowledge_enabled": False,
            "plan_research": True,
            "model": "coder-model",
            "plan_model": "architect-model",
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="plan-split",
            request="billing tax is wrong",
            repository_identity=str(root),
        )
    )

    assert result.status in {"completed_unverified", "completed_verified"}, (
        result.status
    )
    assert plan_calls, "the architect model was never called"
    assert run_calls, "the coder model was never called"
    # The split is REAL, not just reported: every planning call reached the
    # boundary pinned to the architect model, and no execution call did.
    #
    # The execution half is asserted as "not the architect model" rather than
    # "the coder model" because this test INJECTS its gateway
    # (`ModelGateway(call_fn=...)` with no config), and an injected gateway
    # carries no `model` to forward - it reaches the boundary as `None`. A
    # production kernel builds `ModelGateway(config=self.config)`, which does
    # forward the run's pin. Asserting the positive here would be asserting my
    # own test's construction rather than the product's behaviour.
    assert {call.get("model") for call in plan_calls} == {"architect-model"}
    assert all(call.get("model") != "architect-model" for call in run_calls), (
        "an execution call was priced at the architect model"
    )

    receipt = result.metadata["plan"]
    assert receipt["model_split"] is True
    assert receipt["plan_model_configured"] == "architect-model"
    assert receipt["plan_model"] == "architect-model"
    assert receipt["plan_model_honoured"] is True
    assert receipt["execution_model"] == "coder-model"


def test_a_run_that_pins_a_model_forwards_it_to_the_boundary(tmp_path):
    """The control for the note above: a CONFIGURED gateway forwards its pin.

    This is what the architect/coder split depends on - the plan gateway is
    built from the run's own config with only `model` replaced, so a
    configured run's pin must reach the boundary on both halves.
    """
    from harness.agent_kernel.gateway import ModelGateway as _Gateway

    seen: list[dict] = []

    def _boundary(messages, **kwargs):
        seen.append(dict(kwargs))
        return json.dumps({"tool": "finish", "answer": "ok"})

    strategy = _bare_strategy()
    strategy.config = {"model": "coder-model", "plan_model": "architect-model"}
    strategy.model_gateway = _Gateway(call_fn=_boundary, config=dict(strategy.config))
    strategy._plan_model_gateway = None

    strategy._plan_gateway().call([{"role": "user", "content": "plan"}])

    assert seen[-1]["model"] == "architect-model"


def test_the_receipt_never_implies_an_unhonoured_plan_model():
    """If the boundary is unreachable the key is NOT honoured, and it says so.

    The same honesty rule the compaction summarizer follows: a receipt that
    implies a configured model produced the plan when it did not is worse than
    no key at all.
    """
    strategy = _bare_strategy()
    strategy.config = {"plan_model": "architect", "model": "coder"}
    strategy.model_gateway = ModelGateway()  # no call_fn, no model_client
    strategy._plan_model_gateway = None

    gateway = strategy._plan_gateway()

    assert gateway is strategy.model_gateway, (
        "an unreachable boundary must not dial the default provider"
    )
    assert strategy._plan_model_gateway is None


def test_the_planning_spend_folds_into_the_runs_own_totals():
    """A researcher spent real money; a second ledger would under-report it."""
    strategy = _bare_strategy()
    strategy.config = {
        "plan_research": True,
        "model": "coder",
        "plan_model": "architect",
    }
    strategy.model_gateway = ModelGateway(call_fn=_Scripted([]))
    strategy._plan_model_gateway = ModelGateway(
        call_fn=_Scripted([]),
        config={"model": "architect"},
    )
    strategy._plan_model_gateway.total_cost_usd = 0.42
    strategy._plan_model_gateway.total_tokens = 900
    strategy._plan_model_gateway.calls = [{"step": "plan-research-1"}]

    strategy._absorb_spend(strategy._plan_model_gateway)

    assert strategy.model_gateway.total_cost_usd == pytest.approx(0.42)
    assert strategy.model_gateway.total_tokens == 900
    assert len(strategy.model_gateway.calls) == 1
    assert strategy._plan_model_gateway.total_cost_usd == 0.0


def _bare_strategy():
    """A strategy with only the attributes the plan-phase helpers touch."""
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {}
    strategy.model_gateway = ModelGateway()
    strategy._plan_model_gateway = None
    strategy._plan_research = None
    strategy._plan_block = ""
    strategy._spec = None
    return strategy


# ---------------------------------------------------------------------------
# 5. the approved plan is the plan that executes
# ---------------------------------------------------------------------------


def test_an_approved_plan_that_is_not_the_executed_plan_is_refused():
    """Approving plan A and executing plan B is refused, not done quietly."""
    from harness.agent_kernel.completion import CompletionPolicy
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.strategy import DailyCodingStrategy

    spec = RunSpec(
        session_id="s1",
        run_id="mismatch",
        request="fix billing",
        repository_identity=".",
        metadata={"plan_guidance": "1. rewrite the whole billing module"},
    )
    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True}
    strategy._spec = spec
    strategy._plan_block = (
        "1. Update the tax calculation in pkg/billing.py\n2. Add a test"
    )
    strategy.changed_files = []
    strategy.model_gateway = ModelGateway(call_fn=_Scripted([]))
    strategy.events = _NullEvents()
    strategy.checkpoints = _NullCheckpoints()
    strategy.completion = CompletionPolicy(None, config={})

    result = strategy.approve_plan(spec)

    assert result is not None, "a mismatched approval must stop the run"
    assert result.status == "blocked", result.status
    assert "different plans" in (result.error or result.answer or "")


def test_a_matching_approval_proceeds_and_is_recorded(tmp_path):
    """The positive control for the previous test, and the receipt is emitted."""
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.strategy import DailyCodingStrategy

    plan = "1. Update the tax calculation in pkg/billing.py\n2. Add a test"
    spec = RunSpec(
        session_id="s1",
        run_id="match",
        request="fix billing",
        repository_identity=".",
        metadata={"plan_guidance": plan},
    )
    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True}
    strategy._spec = spec
    strategy._plan_block = plan
    strategy._plan_research = None
    events: list = []
    strategy.events = _ListEvents(events)

    assert strategy.approve_plan(spec) is None

    approvals = [row for row in events if row[0] == "plan_approval"]
    assert len(approvals) == 1
    assert approvals[0][1]["matches"] is True
    assert approvals[0][1]["approved_digest"] == approvals[0][1]["executed_digest"]
    receipt = strategy.plan_approval_receipt()
    assert receipt["matches"] is True
    assert receipt["approved"] is True


def test_an_approved_plan_with_no_research_plan_is_the_callers_plan():
    """Historical behaviour, unchanged: caller's guidance is the plan."""
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.strategy import DailyCodingStrategy

    guidance = "1. rewrite the whole billing module"
    spec = RunSpec(
        session_id="s1",
        run_id="caller",
        request="fix billing",
        repository_identity=".",
        metadata={"plan_guidance": guidance},
    )
    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True}
    strategy._spec = spec
    strategy._plan_block = ""
    events: list = []
    strategy.events = _ListEvents(events)

    assert strategy.approve_plan(spec) is None
    approvals = [row for row in events if row[0] == "plan_approval"]
    assert approvals[0][1]["source"] == "caller_plan_guidance"
    assert approvals[0][1]["matches"] is True


def test_no_approval_is_never_inferred_from_silence():
    """An absent approved plan is not an approval; the run proceeds unapproved
    and the receipt says so rather than claiming approval happened."""
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.strategy import DailyCodingStrategy

    spec = RunSpec(
        session_id="s1", run_id="silent", request="fix", repository_identity="."
    )
    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True}
    strategy._spec = spec
    strategy._plan_block = "1. do the thing"
    strategy.events = _NullEvents()

    assert strategy.approve_plan(spec) is None
    receipt = strategy.plan_approval_receipt()
    assert receipt["approved"] is False
    assert receipt["matches"] is True, "nothing was approved, so nothing mismatched"


class _NullEvents:
    def append(self, event_type, payload, **kwargs):
        return payload

    @property
    def path(self):
        return Path(".")


class _ListEvents:
    def __init__(self, sink):
        self.sink = sink

    def append(self, event_type, payload, **kwargs):
        self.sink.append((event_type, dict(payload)))
        return payload

    @property
    def path(self):
        return Path(".")


class _NullCheckpoints:
    @property
    def path(self):
        return Path(".")


# ---------------------------------------------------------------------------
# the OFF arm, and the config discipline
# ---------------------------------------------------------------------------


def test_the_shipped_default_does_not_switch_the_plan_phase_on():
    """A default merged into every task would switch every run's architecture."""
    from harness.config import DEFAULTS

    assert "plan_research" in DEFAULTS, "the key should be discoverable"
    assert DEFAULTS["plan_research"] is None
    for key in (
        "plan_model",
        "plan_research_max_turns",
        "plan_research_max_tool_calls",
        "plan_research_max_cost_usd",
        "plan_research_max_chars",
        "plan_research_max_citations",
    ):
        assert DEFAULTS.get(key) is None, f"{key} has a behaviour-changing default"


def test_the_off_arm_runs_no_researcher_and_changes_no_prompt(tmp_path):
    """The OFF arm is one code path, and the run's prompt is unchanged."""
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.kernel import AgentKernel

    root = _repo(tmp_path)
    model = _Scripted(
        [
            json.dumps({"tool": "read", "path": "pkg/billing.py"}),
            json.dumps({"tool": "finish", "answer": "read it"}),
        ]
    )
    kernel = AgentKernel(
        repo_path=str(root),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "knowledge_enabled": False,
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="plan-off",
            request="billing tax is wrong",
            repository_identity=str(root),
        )
    )

    assert result.status in {"completed_unverified", "completed_verified"}, (
        result.status
    )
    assert "plan" not in (result.metadata or {}), (
        "an unconfigured run must not publish a plan receipt"
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "plan-off" / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    research = [row for row in rows if row.get("event") == "plan_research"]
    assert len(research) == 1
    assert research[0]["data"]["enabled"] is False
    # exactly two model calls: the executor's read and its finish
    assert model.calls == 2, model.calls


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (1, True),
        ("true", True),
        ("on", True),
        ("research", True),
        (False, False),
        (None, False),
        (0, False),
        ("", False),
        ("nope", False),
        ("off", False),
    ],
)
def test_the_switch_is_key_presence_plus_an_explicit_yes(value, expected):
    """A typo in a settings file must not enable a different architecture."""
    strategy = _bare_strategy()
    strategy.config = {"plan_research": value} if value is not None else {}

    assert strategy._plan_research_enabled() is expected


def test_a_nonsense_bound_degrades_to_the_default_rather_than_to_infinity():
    """An unbounded bound is not a bound."""
    strategy = _bare_strategy()
    strategy.config = {
        "plan_research": True,
        "plan_research_max_turns": "not a number",
        "plan_research_max_cost_usd": "free",
        "plan_research_max_chars": -50,
    }
    bounds = strategy._plan_research_bounds()

    assert bounds["max_turns"] == subagents_mod.DEFAULT_PLAN_MAX_TURNS
    assert bounds["max_cost_usd"] == subagents_mod.DEFAULT_PLAN_MAX_COST_USD
    # a negative cap clamps to zero, which means "return nothing" - a real
    # choice, and emphatically not "no cap".
    assert bounds["max_chars"] == 0


# ---------------------------------------------------------------------------
# the verifier gate is not weaker for any of this
# ---------------------------------------------------------------------------


def test_the_success_mint_still_requires_clean_verifier_evidence():
    """Read the mint condition; nothing here may loosen it."""
    from harness.agent_kernel import completion as completion_module

    body = inspect.getsource(completion_module)
    assert "target_passed" in body, "the target test no longer gates success"
    assert "regression_passed" in body, "regression no longer gates success"
    assert "flaky" in body, "flake no longer gates success"


def test_no_completion_status_makes_unverified_reachable_as_success():
    """``completed_unverified`` stays its own status, verbatim."""
    from shared.agent_contracts import RUN_STATUSES

    assert "completed_unverified" in RUN_STATUSES
    assert "completed_verified" in RUN_STATUSES
    assert "success" not in RUN_STATUSES


def test_a_mismatched_approval_blocks_rather_than_reporting_success():
    """The one place this round could plausibly have weakened the gate."""
    from harness.agent_kernel.completion import CompletionPolicy
    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.strategy import DailyCodingStrategy

    spec = RunSpec(
        session_id="s1",
        run_id="mismatch-verified",
        request="fix billing",
        repository_identity=".",
        metadata={"plan_guidance": "1. rewrite the billing module"},
    )
    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True}
    strategy._spec = spec
    strategy._plan_block = "1. something entirely different"
    strategy.changed_files = []
    strategy.model_gateway = ModelGateway(call_fn=_Scripted([]))
    strategy.events = _NullEvents()
    strategy.checkpoints = _NullCheckpoints()
    strategy.completion = CompletionPolicy(None, config={})

    result = strategy.approve_plan(spec)

    assert result is not None
    assert result.status == "blocked"
    assert result.status not in {"completed_verified", "success"}


def test_the_plan_receipt_carries_no_completion_vocabulary():
    """A plan receipt must not be able to imply anything was verified."""
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy.config = {"plan_research": True, "model": "coder", "plan_model": "a"}
    strategy._plan_research = None
    strategy._plan_model_gateway = None
    strategy.model_gateway = ModelGateway(call_fn=_Scripted([]))
    strategy._spec = None

    rendered = json.dumps(strategy.plan_receipt()).lower()
    for word in ("verified", "success", "passed", "completed"):
        assert word not in rendered, f"the plan receipt says {word!r}"


# ---------------------------------------------------------------------------
# a pre-existing defect this round's tests found
# ---------------------------------------------------------------------------


def test_a_disabled_knowledge_binding_is_never_returned_as_a_binding():
    """`knowledge()` memoises ``False`` for unavailable; callers test ``is None``.

    The plan phase calls ``_compile_knowledge``, so a run with
    ``knowledge_enabled=False`` reached the memo-hit path and was handed a bool
    where every caller expects "a binding or None" - producing
    ``'bool' object has no attribute 'compile'`` on turn one. The fix
    translates the sentinel back at the one place that owns it, rather than
    re-guarding each of the three call sites.

    This test is deliberately a direct probe of the CONTRACT rather than an
    end-to-end run, because the end-to-end shape is what hid the bug: the
    ceiling-05 knowledge OFF-arm test was passing because the run crashed
    before the model was ever called.
    """
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strategy._knowledge = False  # the memoised "unavailable" sentinel

    assert strategy.knowledge() is None, (
        "the unavailable sentinel leaked out as a binding"
    )


def test_a_knowledge_disabled_run_still_completes_and_still_calls_the_model():
    """The anti-vacuity control for the defect above, through the real kernel.

    A crash satisfies "the marker was not injected" trivially, so the proof that
    the OFF arm works is that the run COMPLETES and the model IS called.
    """
    import tempfile

    from harness.agent_kernel.contracts import RunSpec
    from harness.agent_kernel.kernel import AgentKernel

    base = Path(tempfile.mkdtemp(prefix="agt05-knowledgeless-"))
    root = base / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "billing.py").write_text("def charge(a):\n    return a\n")
    (root / "pkg" / "__init__.py").write_text("")

    model = _Scripted(
        [
            json.dumps({"tool": "read", "path": "pkg/billing.py"}),
            json.dumps({"tool": "finish", "answer": "read it"}),
        ]
    )
    kernel = AgentKernel(
        repo_path=str(root),
        log_root=base / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "knowledge_enabled": False,
        },
    )
    result = kernel.run(
        RunSpec(
            session_id="s1",
            run_id="no-knowledge",
            request="look at billing",
            repository_identity=str(root),
        )
    )

    assert result.status in {"completed_unverified", "completed_verified"}, result.error
    assert model.calls == 2, f"the model was called {model.calls} times"
