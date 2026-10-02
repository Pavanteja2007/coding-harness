"""Offline regression tests for the transport-neutral agent SDK."""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest

from agent_sdk import (
    Agent,
    Conversation,
    Event,
    EventEnvelope,
    Events,
    LocalAgent,
    LocalWorkspaceManager,
    Result,
    RunHandle,
    RunRequest,
    Tool,
    ToolNotFoundError,
    UnsupportedVersionError,
    VersionNegotiation,
    WorkspaceActiveError,
    WorkspaceError,
)
from agent_sdk.errors import EventReplayError
from extensions import HookDecision, HookManager, HookPoint
from integrations import DeferredTool, ToolCatalog, ToolDescriptor


class FinishModel:
    """Return a deterministic typed finish response."""

    def __init__(self, answer: str = "finished") -> None:
        """Store the answer returned by the injected model."""
        self.answer = answer
        self.calls = 0

    def __call__(self, messages, **kwargs):
        """Return one finish tool call and record invocation count."""
        self.calls += 1
        return json.dumps({"tool": "finish", "answer": self.answer})


def _repo(tmp_path: Path) -> Path:
    """Create a small local repository fixture."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _agent(tmp_path: Path, model=None, **kwargs) -> LocalAgent:
    """Create a local agent with deterministic offline defaults."""
    return LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "logs",
        model=model or FinishModel(),
        config={"agent_approval": "auto", "steering_enabled": False},
        **kwargs,
    )


def test_headless_query_result_mapping_and_conversation(tmp_path):
    """Query synchronously and expose canonical result mapping semantics."""
    agent = _agent(tmp_path, FinishModel("query answer"))
    try:
        result = agent.query("what is the value?")
        assert isinstance(result, Result)
        assert result.status == "completed_unverified"
        assert not result.completed_verified
        assert result["answer"] == "query answer"
        assert result.run_result.schema_version == 1
        assert isinstance(agent.conversation, Conversation)
        assert agent.conversation.id == agent.conversation.session_id
        assert agent.conversation.query("again").status == "completed_unverified"
    finally:
        agent.close()


def test_run_handle_replay_events_and_no_duplicate_reconnect(tmp_path):
    """Run asynchronously and reconnect from an exclusive sequence cursor."""
    agent = _agent(tmp_path)
    try:
        handle = agent.run("make a change", wait=False)
        assert isinstance(handle, RunHandle)
        result = handle.wait(timeout=5)
        assert result.status == "completed_unverified"
        events = agent.events(handle.run_id)
        replay = events.replay()
        assert replay.final_status == "completed_unverified"
        assert [event.sequence for event in replay] == list(range(1, len(replay) + 1))
        assert all(event.schema_version == 1 for event in replay)
        tail = list(events.iter_after_sequence(after_sequence=3))
        assert [event.sequence for event in tail] == list(range(4, len(replay) + 1))
        assert list(events.iter_after_sequence(after_sequence=3)) == tail
        projection = agent.replay(handle.run_id)
        assert projection.run_id == handle.run_id
        assert projection.final_status == "completed_unverified"
        assert handle.result().status == "completed_unverified"
    finally:
        agent.close()


def test_stream_waits_for_a_new_run_without_initial_event_gaps(tmp_path):
    """Iterate a newly started stream while its first journal rows are being written."""
    agent = _agent(tmp_path)
    try:
        events = agent.stream("stream immediately")
        assert [event.sequence for event in events] == list(
            range(1, len(list(agent.events(events.run_id).replay())) + 1)
        )
    finally:
        agent.close()


def test_replay_rejects_tampered_sequence(tmp_path):
    """Reject a journal changed outside the public replay contract."""
    agent = _agent(tmp_path)
    try:
        result = agent.run("tamper", wait=True)
        rows = [
            json.loads(line)
            for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()
        ]
        rows[1]["sequence"] = 99
        Path(result.trace_path).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        with pytest.raises(EventReplayError):
            agent.replay(result.run_id)
    finally:
        agent.close()


def test_cancel_calls_kernel_directly_and_preserves_status(tmp_path):
    """Cancel a blocked run through its actual kernel without a controller."""
    entered = threading.Event()
    release = threading.Event()

    def model(messages, **kwargs):
        entered.set()
        release.wait(5)
        return json.dumps({"tool": "finish", "answer": "late"})

    agent = _agent(tmp_path, model)
    try:
        handle = agent.run("long running", wait=False)
        assert entered.wait(2)
        assert agent.cancel(handle.run_id)
        release.set()
        result = handle.wait(timeout=5)
        assert result.status == "cancelled"
    finally:
        release.set()
        agent.close()


def test_resume_reuses_public_checkpoint_and_same_run_identity(tmp_path):
    """Resume a needs-input run with a new kernel and one contiguous journal."""
    replies = iter(
        [
            json.dumps({"tool": "ask", "question": "which behavior?"}),
            json.dumps({"tool": "finish", "answer": "continued"}),
        ]
    )
    agent = _agent(tmp_path, lambda messages, **kwargs: next(replies))
    try:
        first = agent.run("start", wait=True, run_id="resume-run")
        assert first.status == "needs_input"
        second = agent.resume("resume-run", "continue", wait=True)
        assert second.status == "completed_unverified"
        assert second.run_id == first.run_id
        events = agent.events(first.run_id).replay()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    finally:
        agent.close()


def test_result_never_upgrades_unverified_evidence(tmp_path):
    """Downgrade a forged verified result that lacks clean evidence."""
    result = Result(
        {
            "schema_version": 1,
            "status": "completed_verified",
            "answer": "claimed",
            "verification_evidence": [],
        }
    )
    assert result.status == "completed_unverified"
    assert not result.completed_verified
    assert result["status"] == "completed_unverified"


def test_version_negotiation_is_deterministic_and_typed():
    """Choose the highest common version and reject incompatible requests."""
    negotiated = VersionNegotiation.negotiate([2, 1], [1])
    assert negotiated.protocol_version == 1
    assert negotiated.schema_version == 1
    assert negotiated.to_dict()["version"] == 1
    with pytest.raises(UnsupportedVersionError):
        VersionNegotiation.negotiate(2, 1)
    with pytest.raises(UnsupportedVersionError):
        VersionNegotiation.from_request({}, {}, {}, required=True)


def test_event_envelope_preserves_canonical_schema_and_redacts_payload():
    """Expose a versioned Event projection with redacted payload data."""
    event = Event.from_dict(
        {
            "sequence": 1,
            "timestamp": 1.0,
            "session_id": "session-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "event": "model_response",
            "payload": {"api_key": "secret-value", "text": "ok"},
            "schema_version": 1,
        }
    )
    envelope = EventEnvelope(event=event)
    assert envelope.sequence == 1
    assert envelope["schema_version"] == 1
    assert "secret-value" not in json.dumps(envelope.to_dict())


def test_workspace_lifecycle_is_durable_and_active_delete_is_refused(tmp_path):
    """Persist workspace lifecycle state and protect active workspaces."""
    root = tmp_path / "workspaces"
    manager = LocalWorkspaceManager(root)
    workspace = manager.create("demo", workspace_id="workspace-demo")
    assert workspace["state"] == "ready"
    assert manager.get("workspace-demo").id == workspace.id
    reloaded = LocalWorkspaceManager(root)
    assert reloaded.get("workspace-demo").path == workspace.path
    manager.claim("workspace-demo", "run-active")
    with pytest.raises(WorkspaceActiveError):
        manager.delete("workspace-demo")
    manager.release("workspace-demo", "run-active")
    deleted = manager.delete("workspace-demo")
    assert deleted.state == "deleted"
    assert not deleted.exists()
    assert manager.list() == []


def test_workspace_rejects_unsafe_id_and_source_recursion(tmp_path):
    """Keep workspace identifiers and source copies inside safe boundaries."""
    manager = LocalWorkspaceManager(tmp_path / "managed")
    with pytest.raises(Exception):
        manager.create("bad", workspace_id="../escape")
    with pytest.raises(WorkspaceError):
        manager.create(
            "recursive", workspace_id="workspace-recursive", source_path=tmp_path
        )


def test_tool_descriptor_creates_typed_call(tmp_path):
    """Convert a public Tool descriptor into a Boundary-0 ToolCall."""
    tool = Tool("read", description="read a file", parameters={"path": "string"})
    call = tool.to_call({"path": "app.py"}, call_id="call-1")
    assert call.tool == "read"
    assert call.arguments["path"] == "app.py"
    assert call.schema_version == 1
    assert tool["name"] == "read"
    agent = LocalAgent(
        _repo(tmp_path), log_root=tmp_path / "schema-logs", model=FinishModel()
    )
    try:
        read_tool = next(item for item in agent.tools if item.name == "read")
    finally:
        agent.close()
    assert read_tool.parameters["required"] == ["path"]
    assert read_tool.parameters["properties"]["path"]["type"] == "string"


def test_public_sdk_has_no_kernel_private_or_cli_imports():
    """Keep the SDK transport-neutral and independent of kernel internals."""
    package = Path(__file__).resolve().parents[1] / "agent_sdk"
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith("cli") for item in node.names)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not (module == "cli" or module.startswith("cli."))
                assert not module.endswith("agent_kernel.kernel")


def test_agent_and_conversation_aliases_share_local_semantics(tmp_path):
    """Keep the generic and explicit local facades behaviorally identical."""
    agent = Agent(
        _repo(tmp_path),
        log_root=tmp_path / "logs",
        model=FinishModel(),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    second_repo = tmp_path / "second"
    second_repo.mkdir()
    (second_repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    local = LocalAgent(
        second_repo,
        log_root=tmp_path / "second-logs",
        model=FinishModel(),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    try:
        assert agent.query("one").status == local.query("two").status
        assert agent.conversation.session_id == agent.session_id
    finally:
        agent.close()
        if local is not None:
            local.close()


def test_run_request_and_query_request_are_versioned():
    """Normalize public request objects and reject incompatible schema data."""
    request = RunRequest(request="x", schema_version=1)
    assert request.to_dict()["schema_version"] == 1
    with pytest.raises(UnsupportedVersionError):
        RunRequest(request="x", schema_version=2)


def test_events_type_alias_is_public():
    """Keep Events and Event available from the package root."""
    assert Events.__name__ == "Events"
    assert Event.__name__ == "Event"


def test_task_hooks_block_before_kernel_and_keep_observational_trace(tmp_path):
    """Block before model execution while retaining a canonical traced result."""
    points = []
    hooks = HookManager()
    hooks.register(
        HookPoint.TASK_BEFORE,
        lambda context: (
            points.append(context.point.value)
            or HookDecision.deny("blocked by test hook")
        ),
    )
    hooks.register(
        HookPoint.COMPLETION_BEFORE,
        lambda context: points.append(context.point.value),
    )
    hooks.register(
        HookPoint.COMPLETION_AFTER,
        lambda context: points.append(context.point.value),
    )
    hooks.register(
        HookPoint.TASK_AFTER,
        lambda context: points.append(context.point.value),
    )
    model_calls = []

    def model(messages, **kwargs):
        model_calls.append(True)
        raise AssertionError("model must not run after task denial")

    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "blocked-logs",
        model=model,
        hooks=hooks,
    )
    try:
        result = agent.run("blocked task", wait=True)
        assert result.status == "blocked"
        assert result.error == "blocked by test hook"
        assert model_calls == []
        assert points == [
            "task.before",
            "completion.before",
            "completion.after",
            "task.after",
        ]
        events = agent.events(result.run_id).replay()
        assert events.final_status == "blocked"
        assert "run_started" in [event.event_type for event in events]
        assert "run_finished" in [event.event_type for event in events]
    finally:
        agent.close()


def test_ordinary_hook_failure_isolated_and_security_denial_fails_closed(tmp_path):
    """Isolate ordinary hook errors and turn explicit security denial into blocked."""
    ordinary = HookManager()
    ordinary.register(
        HookPoint.TASK_BEFORE,
        lambda context: (_ for _ in ()).throw(RuntimeError("ordinary hook error")),
        priority=10,
    )
    ordinary.register(HookPoint.TASK_BEFORE, lambda context: None, priority=0)
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "ordinary-logs",
        model=FinishModel(),
        hooks=ordinary,
    )
    try:
        result = agent.run("ordinary", wait=True)
        assert result.status == "completed_unverified"
        assert any(record.error for record in ordinary.records)
    finally:
        agent.close()

    security = HookManager()
    security.register(
        HookPoint.TASK_BEFORE,
        lambda context: HookDecision(
            action="deny", reason="security", security_violation=True
        ),
    )
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "security-logs",
        model=FinishModel(),
        hooks=security,
    )
    try:
        result = agent.run("security", wait=True)
        assert result.status == "blocked"
    finally:
        agent.close()


def test_strict_tool_and_permission_hooks_wrap_public_kernel_components(tmp_path):
    """Enforce tool denial before handlers and permission restriction after policy."""
    replies = iter(
        [
            json.dumps({"tool": "read", "arguments": {"path": "app.py"}}),
            json.dumps({"tool": "finish", "answer": "done"}),
        ]
    )
    after_results = []
    hooks = HookManager()
    hooks.register(
        HookPoint.TOOL_BEFORE,
        lambda context: (
            HookDecision.deny("read denied") if context.tool == "read" else None
        ),
    )
    hooks.register(
        HookPoint.TOOL_AFTER,
        lambda context: after_results.append(
            (
                context.tool,
                context.result.get("ok") if hasattr(context.result, "get") else None,
            )
        ),
    )
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "tool-logs",
        model=lambda messages, **kwargs: next(replies),
        hooks=hooks,
        config={"agent_approval": "auto", "agent_max_turns": 2},
    )
    try:
        result = agent.run("read", wait=True)
        assert result.status == "completed_unverified"
        assert ("read", False) in after_results
        tool_results = [
            event.payload
            for event in agent.events(result.run_id).replay()
            if event.event_type == "tool_result"
        ]
        assert any(
            "hook denied execution" in item.get("output", "") for item in tool_results
        )
    finally:
        agent.close()

    permission_after = []
    permission_hooks = HookManager()
    permission_hooks.register(
        HookPoint.PERMISSION_BEFORE,
        lambda context: (
            HookDecision.deny("edit denied")
            if context.permission_action == "allow"
            else None
        ),
    )
    permission_hooks.register(
        HookPoint.PERMISSION_AFTER,
        lambda context: permission_after.append(context.permission_action),
    )
    replies = iter(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "arguments": {
                        "path": "app.py",
                        "old_string": "value = 1",
                        "new_string": "value = 2",
                    },
                }
            ),
            json.dumps({"tool": "finish", "answer": "must not run"}),
        ]
    )
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "permission-logs",
        model=lambda messages, **kwargs: next(replies),
        hooks=permission_hooks,
        config={"agent_approval": "auto", "agent_max_turns": 2},
    )
    try:
        result = agent.run("edit", wait=True)
        assert result.status == "blocked"
        assert permission_after == ["deny"]
        assert (
            agent.repo_path and Path(agent.repo_path, "app.py").read_text()
        ) == "value = 1\n"
    finally:
        agent.close()


def test_permission_hook_can_escalate_allow_to_ask_without_becoming_deny(tmp_path):
    """Keep approval-required distinct from terminal denial in hook precedence."""
    replies = iter(
        [
            json.dumps({"tool": "read", "arguments": {"path": "app.py"}}),
            json.dumps({"tool": "finish", "answer": "unreachable"}),
        ]
    )
    observed = []
    hooks = HookManager()
    hooks.register(
        HookPoint.PERMISSION_BEFORE,
        lambda context: (
            HookDecision(action="ask", reason="operator review")
            if context.permission_action == "allow"
            else None
        ),
    )
    hooks.register(
        HookPoint.PERMISSION_AFTER,
        lambda context: observed.append(context.permission_action),
    )
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "ask-logs",
        model=lambda messages, **kwargs: next(replies),
        hooks=hooks,
        config={"agent_approval": "auto", "agent_max_turns": 2},
    )
    try:
        result = agent.run("read", wait=True)
    finally:
        agent.close()
    assert result.status == "needs_input"
    assert observed == ["ask"]


def test_local_tool_catalog_keeps_deferred_schemas_explicit(tmp_path):
    """List/search without loading deferred schemas and resolve only on demand."""
    calls = []

    async def loader(name):
        calls.append(name)
        return {"type": "object", "properties": {"value": {"type": "string"}}}

    catalog = ToolCatalog(
        [ToolDescriptor("eager", "Eager tool", {"type": "object"})],
        [DeferredTool("lazy", "Lazy tool", loader=loader)],
    )
    agent = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "catalog-logs",
        model=FinishModel(),
        tool_catalog=catalog,
    )
    try:
        assert [tool.name for tool in agent.list_tools()] == ["eager", "lazy"]
        assert [tool.name for tool in agent.search_tools("lazy")] == ["lazy"]
        assert agent.get_tool("lazy").deferred is True
        assert agent.get_tool("lazy").parameters == {}
        assert calls == []
        assert agent.resolve_tool_schema("lazy")["type"] == "object"
        assert calls == ["lazy"]
    finally:
        agent.close()

    empty = LocalAgent(
        _repo(tmp_path),
        log_root=tmp_path / "empty-logs",
        model=FinishModel(),
    )
    try:
        assert empty.list_tools() == []
        assert empty.search_tools("anything") == []
        with pytest.raises(ToolNotFoundError):
            empty.get_tool("missing")
    finally:
        empty.close()


def test_package_star_exports_resolve():
    """Keep every package-root export importable through star imports."""
    import agent_sdk

    for name in agent_sdk.__all__:
        assert getattr(agent_sdk, name) is not None
    assert agent_sdk.ProtocolCapabilities.__name__ == "ProtocolCapabilities"
    assert agent_sdk.Agent.__name__ == "Agent"
