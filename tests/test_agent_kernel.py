"""Focused contract tests for the shared daily-driver agent kernel."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness.agent_kernel import (
    AgentKernel,
    Checkpoint,
    CheckpointStore,
    CompletionStatus,
    ContextBuilder,
    PolicyEngine,
    PolicyRule,
    ReplayError,
    RunEventJournal,
    RunSpec,
    SessionController,
    SessionStore,
    ToolCall,
    ToolRegistry,
    ToolValidationError,
)
from harness.agent_kernel.gateway import ModelGateway


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = []

    def __call__(self, messages, **kwargs):
        self.messages.append([dict(message) for message in messages])
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _spec(repo, run_id="run-1", session_id="session-1", **kwargs):
    return RunSpec(
        session_id=session_id,
        run_id=run_id,
        request=kwargs.pop("request", "change the value"),
        repository_identity=str(repo),
        **kwargs,
    )


def _kernel(repo, tmp_path, model, **config):
    values = {"agent_approval": "auto", "steering_enabled": False}
    values.update(config)
    return AgentKernel(
        repo_path=str(repo),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config=values,
    )


def test_run_spec_and_event_order_round_trip(tmp_path):
    journal = RunEventJournal(tmp_path / "run.jsonl", "s", "r")
    first = journal.append("started", {"n": 1})
    second = journal.append("finished", {"n": 2})
    assert first.sequence == 1
    assert second.sequence == 2
    rows = [
        json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in rows] == ["started", "finished"]
    assert rows[0]["kind"] == "started"
    assert rows[0]["sequence"] == 1


def test_completed_unverified_reports_checks_and_does_not_claim_success(tmp_path):
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "I am done"})])
    result = _kernel(repo, tmp_path, model).run(_spec(repo))
    assert result.status == "completed_unverified"
    assert not result.completed_verified
    assert any(
        item["kind"] == "verification_unavailable"
        for item in result.verification_evidence
    )
    assert any(item["kind"] == "model_finish" for item in result.verification_evidence)


def test_declared_verification_failure_prevents_completion(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    def verifier(*args, **kwargs):
        calls.append((args, kwargs))
        return {"target_passed": False, "regression_passed": True, "flaky": False}

    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "probably works"})])
    kernel = _kernel(repo, tmp_path, model, target_test="tests/test_app.py")
    kernel.verifier = verifier
    result = kernel.run(
        _spec(repo, verification_policy={"target_test": "tests/test_app.py"})
    )
    assert result.status == "failed"
    assert calls
    assert any(item["kind"] == "verification" for item in result.verification_evidence)


def test_typed_validation_rejects_unknown_and_missing_arguments():
    registry = ToolRegistry()
    with pytest.raises(ToolValidationError):
        registry.validate(ToolCall(tool="read", arguments={}))
    with pytest.raises(ToolValidationError):
        registry.validate(ToolCall(tool="read", arguments={"path": "../secret"}))
    call = registry.validate(
        ToolCall(tool="bash", arguments={"command": "python -m pytest"})
    )
    assert call.tool == "shell"
    assert call.side_effect_class == "process"


def test_mcp_flat_arguments_keep_server_name_and_inner_args():
    from harness.agent_kernel.tools import parse_model_response

    calls = parse_model_response(
        {"tool": "mcp", "server": "broken", "name": "lookup", "args": {"x": 1}}
    )
    assert calls[0]["tool"] == "mcp"
    assert calls[0]["arguments"] == {
        "server": "broken",
        "name": "lookup",
        "args": {"x": 1},
    }
    registry = ToolRegistry()
    call = registry.validate(ToolCall.from_dict(calls[0]))
    assert call.arguments["server"] == "broken"
    assert call.arguments["name"] == "lookup"
    assert call.arguments["args"] == {"x": 1}


def test_mcp_outer_arguments_envelope_is_not_inner_args():
    from harness.agent_kernel.tools import parse_model_response

    calls = parse_model_response(
        {
            "tool": "mcp",
            "arguments": {"server": "broken", "name": "lookup", "args": {"x": 1}},
        }
    )
    assert calls[0]["arguments"]["server"] == "broken"
    assert calls[0]["arguments"]["args"] == {"x": 1}


def test_mcp_flat_call_from_dict_preserves_connector_payload():
    call = ToolCall.from_dict(
        {"tool": "mcp", "server": "broken", "name": "lookup", "args": {"x": 1}}
    )
    assert call.arguments == {
        "server": "broken",
        "name": "lookup",
        "args": {"x": 1},
    }


def test_policy_dimensions_and_terminal_denial():
    policy = PolicyEngine(
        [
            PolicyRule("allow", tool="read", path="src/*"),
            PolicyRule("ask", tool="shell", command_prefix="python"),
            PolicyRule("deny", tool="write", path="secrets/*"),
            PolicyRule("allow", tool="fetch", network_domain="docs.example.com"),
        ],
        default_action="deny",
    )
    assert (
        policy.evaluate(ToolCall(tool="read", arguments={"path": "src/a.py"})).action
        == "allow"
    )
    assert (
        policy.evaluate(
            ToolCall(tool="shell", arguments={"command": "python -m pytest"})
        ).action
        == "ask"
    )
    denied = policy.evaluate(ToolCall(tool="write", arguments={"path": "secrets/key"}))
    assert denied.action == "deny"
    assert denied.terminal
    assert (
        policy.evaluate(
            ToolCall(tool="fetch", arguments={"url": "https://docs.example.com/x"})
        ).action
        == "allow"
    )


def test_hard_protected_paths_override_allow_and_approval_grants():
    policy = PolicyEngine(
        [PolicyRule("allow", tool="write", path="*")],
        default_action="allow",
    )
    calls = [
        ToolCall(tool="write", arguments={"path": ".git/config", "content": "unsafe"}),
        ToolCall(tool="read", arguments={"path": ".env.local"}),
        ToolCall(tool="shell", arguments={"command": "type .env"}),
    ]
    decisions = [policy.evaluate(call) for call in calls]
    assert all(
        decision.action == "deny" and decision.terminal for decision in decisions
    )
    policy.record_approval(calls[0], decisions[0], True, "global")
    assert policy.evaluate(calls[0]).action == "deny"
    configured = PolicyEngine(default_action="allow", protected_paths=["private/*"])
    assert (
        configured.evaluate(
            ToolCall(
                tool="write", arguments={"path": "private/note.txt", "content": "x"}
            )
        ).action
        == "deny"
    )


def test_approval_scopes_cover_exact_path_command_and_global():
    policy = PolicyEngine([PolicyRule("ask", tool="edit")], default_action="ask")
    first = ToolCall(
        call_id="c1",
        tool="edit",
        arguments={"path": "src/a.py", "old_string": "a", "new_string": "b"},
    )
    decision = policy.evaluate(first)
    policy.record_approval(first, decision, True, "session_path")
    same_path = ToolCall(
        call_id="c2",
        tool="edit",
        arguments={"path": "src/a.py", "old_string": "b", "new_string": "c"},
    )
    other_path = ToolCall(
        call_id="c3",
        tool="edit",
        arguments={"path": "src/b.py", "old_string": "b", "new_string": "c"},
    )
    assert policy.evaluate(same_path).action == "allow"
    assert policy.evaluate(other_path).action == "ask"
    shell = ToolCall(
        call_id="c4", tool="shell", arguments={"command": "python -m pytest"}
    )
    shell_decision = policy.evaluate(shell)
    policy.record_approval(shell, shell_decision, True, "session_command_prefix")
    assert (
        policy.evaluate(
            ToolCall(
                call_id="c5",
                tool="shell",
                arguments={"command": "python -m pytest tests"},
            )
        ).action
        == "allow"
    )
    assert (
        policy.evaluate(
            ToolCall(call_id="c6", tool="shell", arguments={"command": "git status"})
        ).action
        == "ask"
    )


def test_context_continuity_and_prior_diff(tmp_path):
    repo = _repo(tmp_path)
    first_model = ScriptedModel(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                }
            ),
            json.dumps({"tool": "finish", "answer": "updated"}),
        ]
    )
    first = _kernel(repo, tmp_path, first_model).run(_spec(repo, run_id="run-a"))
    assert first.status == "completed_unverified"
    second_model = ScriptedModel(
        [json.dumps({"tool": "finish", "answer": "continued"})]
    )
    second_kernel = _kernel(repo, tmp_path, second_model)
    second = second_kernel.run(
        _spec(repo, run_id="run-b", request="continue the change")
    )
    assert second.status == "completed_unverified"
    context = "\n".join(message["content"] for message in second_model.messages[-1])
    assert "updated" in context
    assert "app.py" in context
    assert "value = 2" in context


def test_daily_kernel_routes_mutations_through_safe_workspace_backend(tmp_path):
    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                }
            ),
            json.dumps({"tool": "finish", "answer": "updated"}),
        ]
    )
    result = _kernel(repo, tmp_path, model).run(_spec(repo, run_id="safe-backend"))
    assert result.status == "completed_unverified"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    trace = (tmp_path / "logs" / "safe-backend" / "trace.jsonl").read_text(
        encoding="utf-8"
    )
    assert "execution_backend_ready" in trace
    assert "SafeToolBackend" in trace


def test_tool_feedback_is_rebuilt_into_the_next_model_turn(tmp_path):
    repo = _repo(tmp_path)

    class CheckingModel:
        def __init__(self):
            self.calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2:
                context = "\n".join(message["content"] for message in messages)
                assert "TOOL RESULT bash" in context
                assert "Continue with one typed tool call" in context
            if self.calls == 1:
                return json.dumps(
                    {
                        "tool": "bash",
                        "command": f'"{sys.executable}" -c "import sys; sys.exit(3)"',
                    }
                )
            return json.dumps({"tool": "finish", "answer": "saw the failure"})

    result = _kernel(repo, tmp_path, CheckingModel()).run(
        _spec(repo, run_id="tool-feedback")
    )
    assert result.status == "completed_unverified"


def test_default_shell_handler_honors_a_cancelled_token(tmp_path):
    from execution.workspace import CancellationToken
    from harness.agent_kernel.strategy import build_default_handlers

    repo = _repo(tmp_path)
    registry = ToolRegistry()
    build_default_handlers(registry, repo_path=str(repo))
    token = CancellationToken()
    token.cancel()
    result = registry.execute(
        ToolCall(
            tool="shell",
            arguments={
                "command": f'"{sys.executable}" -c "import time; time.sleep(30)"'
            },
        ),
        {"cancellation_token": token},
    )
    assert not result.ok
    assert "cancel" in str(result.output).lower()


def test_context_compaction_keeps_summary_and_recent_turns(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    builder = ContextBuilder(store, max_turns=2, max_summary_chars=2000)
    state = builder.load_state("s1")[0]
    for index in range(4):
        state = builder.persist_turn(
            state, f"request-{index}", f"answer-{index}", summary=f"summary-{index}"
        )
    loaded = builder.load_state("s1")[0]
    assert len(loaded.turns) == 2
    assert "summary-0" in loaded.summary
    assert "answer-3" in loaded.summary or loaded.turns[-1]["answer"] == "answer-3"


def test_checkpoint_resume_and_monotonic_events(tmp_path):
    repo = _repo(tmp_path)
    first_model = ScriptedModel(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                }
            ),
            json.dumps({"tool": "ask", "question": "which test should I run?"}),
        ]
    )
    first_kernel = _kernel(repo, tmp_path, first_model)
    first = first_kernel.run(_spec(repo, run_id="resume-run"))
    assert first.status == "needs_input"
    checkpoint = json.loads(
        (tmp_path / "logs" / "resume-run" / "checkpoint.json").read_text()
    )
    second_model = ScriptedModel([json.dumps({"tool": "finish", "answer": "resumed"})])
    second = _kernel(repo, tmp_path, second_model).run(
        _spec(
            repo,
            run_id="resume-run",
            request="continue",
            resume_token=checkpoint["resume_token"],
        ),
        resume=True,
    )
    assert second.status == "completed_unverified"
    events = RunEventJournal(
        tmp_path / "logs" / "resume-run" / "trace.jsonl"
    ).read_events()
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert any(event.event_type == "run_started" for event in events)


def test_resume_rejects_repository_request_and_revision_identity_mismatch(tmp_path):
    repo = _repo(tmp_path)
    first_kernel = _kernel(
        repo,
        tmp_path,
        ScriptedModel([json.dumps({"tool": "ask", "question": "which behavior?"})]),
    )
    first = first_kernel.run(
        _spec(
            repo,
            run_id="identity-run",
            request="original request",
            workspace_policy={"revision": "revision-a"},
        )
    )
    assert first.status == "needs_input"
    checkpoint = json.loads(
        (tmp_path / "logs" / "identity-run" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )

    mismatched_request_model = ScriptedModel([])
    mismatched_request = _kernel(repo, tmp_path, mismatched_request_model).run(
        _spec(
            repo,
            run_id="identity-run",
            request="different request",
            resume_token=checkpoint["resume_token"],
            workspace_policy={"revision": "revision-a"},
        ),
        resume=True,
    )
    assert mismatched_request.status == "blocked"
    assert not mismatched_request_model.replies

    mismatched_revision = _kernel(repo, tmp_path, ScriptedModel([])).run(
        _spec(
            repo,
            run_id="identity-run",
            request="original request",
            resume_token=checkpoint["resume_token"],
            workspace_policy={"revision": "revision-b"},
        ),
        resume=True,
    )
    assert mismatched_revision.status == "blocked"

    other_repo = _repo(tmp_path / "other")
    mismatched_repo = _kernel(other_repo, tmp_path, ScriptedModel([])).run(
        _spec(
            other_repo,
            run_id="identity-run",
            request="original request",
            resume_token=checkpoint["resume_token"],
            workspace_policy={"revision": "revision-a"},
        ),
        resume=True,
    )
    assert mismatched_repo.status == "blocked"


def test_model_failure_and_malformed_calls_are_bounded(tmp_path):
    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            RuntimeError("provider down"),
            RuntimeError("provider down"),
            RuntimeError("provider down"),
        ]
    )
    result = _kernel(repo, tmp_path, model, max_model_failures=1).run(
        _spec(repo, run_id="failure")
    )
    assert result.status == "failed"
    malformed = ScriptedModel(
        ["not a tool", "still not a tool", "still not a tool", "still not a tool"]
    )
    result = _kernel(repo, tmp_path, malformed, max_tool_recoveries=1).run(
        _spec(repo, run_id="malformed")
    )
    assert result.status == "failed"


def test_corrupt_session_state_warns_explicitly(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{broken", encoding="utf-8")
    store = SessionStore(path)
    state, warnings = store.load_with_warnings("s1")
    assert state is None
    assert warnings and "corrupt" in warnings[0].lower()


def test_checkpoint_store_atomic_round_trip_and_corruption(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(path, "s", "r")
    checkpoint = Checkpoint(
        last_event_sequence=4, agent_owned_changes=["a.py"], spend=0.2
    )
    assert store.save(checkpoint)
    assert store.load().last_event_sequence == 4
    path.write_text("[]", encoding="utf-8")
    assert store.load() is None
    assert store.warnings


def test_kernel_callback_receives_same_event_authority(tmp_path, capsys):
    repo = _repo(tmp_path)
    events = []
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "ok"})])
    kernel = _kernel(repo, tmp_path, model)
    kernel.on_event = events.append
    result = kernel.run(_spec(repo, run_id="callback-run"))
    assert result.trace_path.endswith("trace.jsonl")
    assert events
    assert all("sequence" in event for event in events)
    assert capsys.readouterr().out == ""


def test_native_structured_calls_and_read_only_parallel_batch(tmp_path):
    repo = _repo(tmp_path)
    native = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"app.py"}',
                            }
                        },
                        {
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"app.py"}',
                            }
                        },
                    ]
                }
            }
        ]
    }
    model = ScriptedModel(
        [native, json.dumps({"tool": "finish", "answer": "read both"})]
    )
    result = _kernel(repo, tmp_path, model).run(_spec(repo, run_id="native"))
    assert result.status == "completed_unverified"
    events = RunEventJournal(tmp_path / "logs" / "native" / "trace.jsonl").read_events()
    results = [event for event in events if event.event_type == "tool_result"]
    assert len([event for event in results if event.payload.get("tool") == "read"]) >= 2


def test_strict_adapter_opt_in_preserves_legacy_api(tmp_path):
    from harness import deps
    from harness.agent_loop import run_agent

    repo = _repo(tmp_path)
    replies = iter([json.dumps({"tool": "finish", "answer": "ok"})])
    deps.set_call_model(lambda messages, **kwargs: next(replies))
    try:
        result = run_agent(
            "change",
            str(repo),
            {"agent_kernel_enabled": True, "agent_approval": "auto"},
            log_root=tmp_path / "logs",
            task_id="strict-adapter",
        )
    finally:
        deps.reset_overrides()
    assert result["status"] == "completed_unverified"
    assert result["kernel_status"] == "completed_unverified"


def test_verified_strategy_translates_runner_result(tmp_path):
    from types import SimpleNamespace

    from harness.agent_kernel import RunResult
    from harness.agent_kernel.verified import VerifiedFixStrategy

    def task_factory(spec, config):
        return SimpleNamespace(
            task_id=spec.run_id,
            repo_path=spec.repository_identity,
            issue_text=spec.request,
            config=config,
        )

    def runner(task, log_root=None, **kwargs):
        return SimpleNamespace(
            status="success",
            attempts=2,
            cost_usd=0.1,
            diff="diff",
            model_calls=[],
            log_path="trace.jsonl",
            verification=SimpleNamespace(
                target_test_passed=True,
                regression_passed=True,
                flaky=False,
                raw_output="ok",
            ),
        )

    strategy = VerifiedFixStrategy(
        log_root=tmp_path,
        task_factory=task_factory,
        runner=runner,
    )
    result = strategy.run(_spec(tmp_path, request="fix", run_id="legacy"))
    assert isinstance(result, RunResult)
    assert result.status == "completed_verified"
    assert result.attempts == 2
    assert result.verification_evidence[0]["target_passed"] is True


def test_shared_contracts_are_versioned_and_round_trip():
    from harness.agent_kernel import contracts as compatibility_contracts
    from shared import agent_contracts as shared_contracts

    assert compatibility_contracts.RunSpec is shared_contracts.RunSpec
    values = [
        shared_contracts.SessionState(session_id="s1", active_task="inspect"),
        shared_contracts.RunSpec(
            session_id="s1",
            run_id="r1",
            request="inspect",
            repository_identity="repo",
        ),
        shared_contracts.ToolCall(tool="read", arguments={"path": "app.py"}),
        shared_contracts.PermissionDecision(
            matched_rule="default",
            action="allow",
            scope="once",
            actor="agent",
            call_id="c1",
            exact_effect="{}",
        ),
        shared_contracts.RunEvent(
            sequence=1,
            timestamp=1.0,
            session_id="s1",
            run_id="r1",
            turn_id="turn-1",
            event_type="started",
        ),
        shared_contracts.Checkpoint(last_event_sequence=1),
        shared_contracts.RunResult(
            status=shared_contracts.CompletionStatus.COMPLETED_UNVERIFIED
        ),
    ]
    for value in values:
        serialized = value.to_dict()
        assert serialized["schema_version"] == shared_contracts.SCHEMA_VERSION
        assert type(value).from_dict(serialized).to_dict() == serialized
    assert {status.value for status in CompletionStatus} == {
        "completed_verified",
        "completed_unverified",
        "needs_input",
        "blocked",
        "failed",
        "cancelled",
        "timeout",
    }
    with pytest.raises(ValueError, match="unsupported"):
        RunSpec.from_dict(
            {
                "session_id": "s1",
                "run_id": "r1",
                "request": "x",
                "repository_identity": "repo",
                "schema_version": 2,
            }
        )


def test_kernel_has_no_cli_imports():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "harness" / "agent_kernel"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith("cli") for item in node.names)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("cli")


def test_timeout_blocked_and_cancelled_are_distinct(tmp_path):
    repo = _repo(tmp_path)
    timeout = _kernel(
        repo,
        tmp_path,
        ScriptedModel([]),
        max_wallclock_s=0,
    ).run(_spec(repo, run_id="timeout"))
    assert timeout.status == "timeout"

    blocked_model = ScriptedModel(
        [json.dumps({"tool": "write", "path": ".git/config", "content": "bad"})]
    )
    blocked = _kernel(repo, tmp_path, blocked_model).run(_spec(repo, run_id="blocked"))
    assert blocked.status == "blocked"

    cancel_model = ScriptedModel([json.dumps({"tool": "cancel", "reason": "stop"})])
    cancelled = _kernel(repo, tmp_path, cancel_model).run(
        _spec(repo, run_id="cancelled")
    )
    assert cancelled.status == "cancelled"


def test_planning_question_and_research_are_registered_strategies(tmp_path):
    repo = _repo(tmp_path)
    for strategy in ("planning", "question", "research"):
        model = ScriptedModel(
            [json.dumps({"tool": "finish", "answer": f"{strategy} complete"})]
        )
        result = _kernel(repo, tmp_path, model).run(
            _spec(repo, run_id=f"strategy-{strategy}", strategy=strategy),
            strategy=strategy,
        )
        assert result.status == "completed_unverified"
        events = RunEventJournal(
            tmp_path / "logs" / f"strategy-{strategy}" / "trace.jsonl"
        ).read_events()
        selected = [
            event for event in events if event.event_type == "strategy_selected"
        ]
        assert selected[-1].payload["strategy"] == strategy


def test_question_strategy_cannot_mutate_repository(tmp_path):
    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps(
                {"tool": "edit", "path": "app.py", "old_string": "1", "new_string": "2"}
            ),
            json.dumps({"tool": "finish", "answer": "read-only answer"}),
        ]
    )
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, run_id="read-only"), strategy="question"
    )
    assert result.status == "completed_unverified"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_unknown_strategy_fails_before_creating_a_run(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="unknown agent strategy"):
        _kernel(repo, tmp_path, ScriptedModel([])).run(
            _spec(repo, run_id="unknown"), strategy="typo"
        )
    assert not (tmp_path / "logs" / "unknown").exists()


def test_journal_replay_is_deterministic_and_corruption_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    _kernel(
        repo,
        tmp_path,
        ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})]),
    ).run(_spec(repo, run_id="replay"))
    trace = tmp_path / "logs" / "replay" / "trace.jsonl"
    first = RunEventJournal(trace).replay()
    second = RunEventJournal(trace).replay()
    assert first.to_dict() == second.to_dict()
    assert first.final_status == "completed_unverified"
    assert first.request == "change the value"
    assert all(
        event["sequence"] == index for index, event in enumerate(first.events, 1)
    )

    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    rows[1]["sequence"] = 9
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match="sequence"):
        RunEventJournal(corrupt).replay()


def test_resume_replays_the_same_versioned_contract(tmp_path):
    repo = _repo(tmp_path)
    first_model = ScriptedModel(
        [json.dumps({"tool": "ask", "question": "which behavior?"})]
    )
    first = _kernel(repo, tmp_path, first_model).run(
        _spec(repo, run_id="contract-resume")
    )
    assert first.status == "needs_input"
    checkpoint = json.loads(
        (tmp_path / "logs" / "contract-resume" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    resumed_spec = _spec(
        repo,
        run_id="contract-resume",
        request="continue",
        resume_token=checkpoint["resume_token"],
    )
    assert RunSpec.from_dict(resumed_spec.to_dict()).to_dict() == resumed_spec.to_dict()
    result = _kernel(
        repo,
        tmp_path,
        ScriptedModel([json.dumps({"tool": "finish", "answer": "continued"})]),
    ).run(resumed_spec, resume=True)
    assert result.status == "completed_unverified"
    projection = RunEventJournal(
        tmp_path / "logs" / "contract-resume" / "trace.jsonl"
    ).replay()
    assert projection.final_status == "completed_unverified"
    assert checkpoint["last_event_sequence"] <= len(projection.events)


def test_session_controller_owns_start_turn_and_close(tmp_path):
    repo = _repo(tmp_path)
    controller = SessionController(
        str(repo),
        log_root=tmp_path / "logs",
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    state = controller.start()
    assert state.session_id == controller.session_id
    controller._kernel = lambda spec, config=None, strategy_options=None: AgentKernel(
        repo_path=str(repo),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(
            call_fn=ScriptedModel(
                [json.dumps({"tool": "finish", "answer": "session done"})]
            )
        ),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    result = controller.run_turn("work", run_id="session-run")
    assert result.session_id == controller.session_id
    closed = controller.close()
    assert closed.active_run_id == ""
    assert controller.events("session-run")


def test_safe_backend_overwrites_and_rolls_back_invalid_syntax(tmp_path):
    repo = _repo(tmp_path)
    overwrite_model = ScriptedModel(
        [
            json.dumps({"tool": "write", "path": "app.py", "content": "value = 3\n"}),
            json.dumps({"tool": "finish", "answer": "written"}),
        ]
    )
    result = _kernel(repo, tmp_path, overwrite_model).run(
        _spec(repo, run_id="safe-overwrite")
    )
    assert result.status == "completed_unverified"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 3\n"

    rollback_repo = _repo(tmp_path / "rollback")
    rollback_model = ScriptedModel(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "app.py",
                    "old_string": "value = 1",
                    "new_string": "value = = 2",
                }
            ),
            json.dumps({"tool": "finish", "answer": "rolled back"}),
        ]
    )
    rollback = _kernel(rollback_repo, tmp_path, rollback_model).run(
        _spec(rollback_repo, run_id="safe-rollback")
    )
    assert rollback.status == "completed_unverified"
    assert (rollback_repo / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_model_gateway_respects_boundary_signature_without_double_calling(tmp_path):
    calls = []

    def router(messages, difficulty_hint=None, provider=None, model=None, api_key=None):
        calls.append(messages)
        return json.dumps({"tool": "finish", "answer": "ok"})

    repo = _repo(tmp_path)
    result = _kernel(repo, tmp_path, router).run(_spec(repo, run_id="gateway"))
    assert result.status == "completed_unverified"
    assert len(calls) == 1


def test_model_gateway_reads_module_level_router_usage(monkeypatch):
    from harness import deps
    from runtime import model_router

    def router(messages, **kwargs):
        return "done"

    router.__module__ = "runtime.model_router"
    monkeypatch.setattr(deps, "get_call_model", lambda: router)
    monkeypatch.setattr(
        model_router,
        "get_last_usage",
        lambda: {"tokens": 7, "cost_usd": 0.7},
    )

    gateway = ModelGateway()
    response = gateway.call([{"role": "user", "content": "hello"}])

    assert response.usage["tokens"] == 7
    assert response.usage["cost"] == 0.7
    assert response.usage["cost_usd"] == 0.7
    assert gateway.snapshot_usage() == {"cost_usd": 0.7, "tokens": 7, "calls": 1}


def test_core_run_task_adapter_preserves_task_result_state_and_one_journal(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from harness import core
    from shared.types import Task, TaskResult, VerificationResult

    repo = _repo(tmp_path)
    expected = TaskResult(
        task_id="legacy-contract",
        status="success",
        attempts=1,
        diff="diff",
        verification=VerificationResult(True, False, True, False, "ok"),
        cost_usd=0.25,
        model_calls=[{"step": "plan"}],
        log_path="",
    )

    def fake_legacy(task, log_root=None, *, _trace=None, _reuse_run_dir=False):
        run_dir = Path(log_root) / task.task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "plan": ["fix"],
                    "completed_steps": ["fix"],
                    "files_touched": ["app.py"],
                    "decisions": ["kept compatibility"],
                    "remaining_plan": [],
                }
            ),
            encoding="utf-8",
        )
        _trace.log("task_start", {"task_id": task.task_id})
        _trace.log("task_end", {"status": "success"})
        expected.log_path = str(run_dir / "trace.jsonl")
        return expected

    monkeypatch.setattr(core, "_run_task_legacy", fake_legacy)
    result = core.run_task(
        Task(
            task_id="legacy-contract",
            repo_path=str(repo),
            issue_text="fix",
            config={"agent_approval": "auto", "steering_enabled": False},
        ),
        log_root=tmp_path / "logs",
    )
    assert result is expected
    state = json.loads(
        (tmp_path / "logs" / "legacy-contract" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(state)[:6] == [
        "task_id",
        "plan",
        "completed_steps",
        "files_touched",
        "decisions",
        "remaining_plan",
    ]
    projection = RunEventJournal(result.log_path).replay()
    assert projection.final_status == "completed_verified"
    assert [event["sequence"] for event in projection.events] == list(
        range(1, len(projection.events) + 1)
    )


def test_default_general_agent_keeps_cli_shape_without_verified_claim(tmp_path):
    from harness import deps
    from harness.agent_loop import run_agent

    repo = _repo(tmp_path)
    replies = iter([json.dumps({"tool": "done", "answer": "legacy answer"})])
    deps.set_call_model(lambda messages, **kwargs: next(replies))
    try:
        result = run_agent(
            "change",
            str(repo),
            {"steering_enabled": False},
            log_root=tmp_path / "logs",
            task_id="legacy-agent-kernel",
        )
    finally:
        deps.reset_overrides()
    # A DEFAULT run keeps the historical result shape and must NOT launder an
    # unverified completion into the historical `success` word: a model asking
    # to stop without clean verifier evidence is not a success.
    #
    # R2-04: the default ENGINE is now the kernel resolver's `daily`, not
    # `legacy_agent` — this assertion used to pin the bug R2-04 fixed (the
    # default was unreachable because `run_agent` forced
    # `explicit="legacy_agent"`). The compatibility engine is still reachable
    # and is covered by `run_agent_legacy` in
    # tests/test_ceiling_r2_04_daily_default.py. Nothing about the honest
    # status below was relaxed to make the engine change pass.
    assert result["status"] == "completed_unverified"
    assert result["status"] != "success"
    assert result["kernel_status"] == "completed_unverified"
    assert result["agent_strategy"] == "daily"
    assert result["agent_strategy_source"] == "default"
    projection = RunEventJournal(result["trace_path"]).replay()
    assert projection.final_status == "completed_unverified"


# ---------------------------------------------------------------------------
# VEX-CEILING-01 — one default agent path
# ---------------------------------------------------------------------------


def _marker_model(marker, required_turn, replies_after=()):
    """A scripted model that requires a turn-1 fact to appear on a later turn."""

    class MarkerModel:
        def __init__(self):
            self.messages = []
            self.turns = 0

        def __call__(self, messages, **kwargs):
            self.turns += 1
            self.messages.append([dict(item) for item in messages])
            rendered = "\n".join(item.get("content", "") for item in messages)
            if self.turns == 1:
                return json.dumps(
                    {
                        "tool": "write",
                        "path": "notes.md",
                        "content": f"discovered: {marker}\n",
                    }
                )
            if self.turns == required_turn:
                assert marker in rendered, (
                    f"turn {self.turns} lost a fact discovered on turn 1"
                )
                return json.dumps({"tool": "finish", "answer": "fact retained"})
            return json.dumps({"tool": "read", "path": "notes.md"})

    return MarkerModel()


def test_fact_discovered_on_turn_one_is_still_visible_on_turn_five(tmp_path):
    """The daily strategy must retain a turn-1 fact on turn 5.

    A rebuilt ``[system, user]`` prompt per turn is the defect this pins: the
    only way turn 5 sees the marker is if turn 1's tool result is still inside
    the model's own message list.
    """
    repo = _repo(tmp_path)
    model = _marker_model("FACT-ALPHA-7f3c", required_turn=5)
    kernel = _kernel(repo, tmp_path, model)
    result = kernel.run(_spec(repo, run_id="continuity-run"), strategy="daily")
    assert result.status == "completed_unverified"
    assert model.turns == 5
    final = "\n".join(item.get("content", "") for item in model.messages[-1])
    assert "FACT-ALPHA-7f3c" in final
    # The rolling list grows: turn 5 is not the same two-message frame.
    assert len(model.messages[0]) == 2
    assert len(model.messages[-1]) > 2
    assert any(item["role"] == "user" for item in model.messages[-1])


def test_conversation_never_rebuilds_a_fresh_prompt_each_turn(tmp_path):
    """Turn N+1 must keep turn N's prompt prefix instead of replacing it."""
    repo = _repo(tmp_path)
    model = _marker_model("FACT-BETA-11", required_turn=3)
    kernel = _kernel(repo, tmp_path, model)
    kernel.run(_spec(repo, run_id="prefix-run"), strategy="daily")
    first = model.messages[0]
    second = model.messages[1]
    third = model.messages[2]
    assert second[: len(first)] == first
    assert third[: len(second)] == second


def test_dropped_turns_compact_into_a_structured_handoff(tmp_path):
    """Turns dropped past the context budget become a structured handoff."""
    from harness.agent_kernel.conversation import ConversationMemory

    memory = ConversationMemory(max_messages=4, max_chars=100000)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    memory.record_assistant("thinking", [], turn=1)
    for index in range(3, 9):
        memory.record_tool_result(
            "read", True, f"body-{index}", turn=index, target="app.py"
        )
    rendered = memory.render()
    handoff = [
        item
        for item in rendered
        if "Compacted earlier turns" in item.get("content", "")
    ]
    assert handoff, "dropped turns must leave a structured handoff message"
    body = handoff[0]["content"]
    assert "Paths touched: app.py" in body
    assert "Findings:" in body
    # The most recent tool result is retained verbatim.
    assert "body-8" in "\n".join(item["content"] for item in rendered)
    assert memory.dropped_messages > 0


def test_hard_kill_and_resume_retain_pre_kill_turns_and_evidence(tmp_path):
    """A real hard kill must not erase the turns and tool evidence before it.

    A child process runs the REAL kernel and is hard-killed with ``os._exit``
    after turn 2. The parent then asserts the durable ledger still holds both
    pre-kill turns and their tool evidence, and that a resume reads them back.
    """
    import os
    import subprocess
    import sys

    from harness.agent_kernel.turns import TurnLedger, replay_turn_ledger

    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    driver = Path(__file__).resolve().parent / "kernel_kill_driver.py"
    replies = json.dumps(
        [
            json.dumps(
                {
                    "tool": "write",
                    "path": "notes.md",
                    "content": "pre-kill fact GAMMA-42\n",
                }
            ),
            json.dumps({"tool": "read", "path": "notes.md"}),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(repo),
            str(log_root),
            "kill-run",
            "2",
            replies,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 70, completed.stderr

    ledger_path = log_root / "kill-run" / "turns.jsonl"
    assert ledger_path.is_file()
    pre_kill = TurnLedger(ledger_path).load()
    assert [item["turn"] for item in pre_kill] == [1]
    assert pre_kill[0]["tool_calls"][0]["tool"] == "write"
    assert "notes.md" in pre_kill[0]["changed_files"]
    # Replay projects the same state a live run holds, with no model call.
    projection = replay_turn_ledger(ledger_path)
    assert projection["turn_count"] == 1
    assert projection["last_turn"] == 1
    assert projection["tool_counts"] == {"write": 1}
    assert "notes.md" in projection["changed_files"]
    assert (repo / "notes.md").read_text(encoding="utf-8") == "pre-kill fact GAMMA-42\n"

    # A resume in the same process reads the pre-kill record back and states
    # it in the model's own message list.
    resumed_model = ScriptedModel([json.dumps({"tool": "finish", "answer": "resumed"})])
    kernel = _kernel(repo, tmp_path, resumed_model)
    kernel.run(_spec(repo, run_id="kill-run"), strategy="daily", resume=True)
    resumed_prompt = "\n".join(
        item.get("content", "") for item in resumed_model.messages[0]
    )
    assert "Durable state recovered from the interrupted run" in resumed_prompt
    assert "notes.md" in resumed_prompt
    after = TurnLedger(ledger_path).load()
    assert [item["turn"] for item in after][:1] == [1]
    assert len(after) > 1
    assert os.path.isfile(ledger_path)


def test_turn_ledger_is_appended_on_every_checkpoint_not_only_at_completion(tmp_path):
    """Each turn leaves a durable record, including a mid-run interruption."""
    from harness.agent_kernel.turns import TurnLedger

    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps({"tool": "read", "path": "app.py"}),
            json.dumps({"tool": "question", "question": "which test?"}),
        ]
    )
    kernel = _kernel(repo, tmp_path, model)
    result = kernel.run(_spec(repo, run_id="ledger-run"), strategy="daily")
    assert result.status == "needs_input"
    records = TurnLedger(tmp_path / "logs" / "ledger-run" / "turns.jsonl").load()
    assert [item["turn"] for item in records] == [1, 2]
    assert records[-1]["status"] == "needs_input"
    assert records[0]["event_sequence"] > 0


def test_default_config_selects_the_kernel_and_writes_session_artifacts(tmp_path):
    """The default daily configuration runs the kernel and leaves artifacts."""
    from harness.agent_kernel import resolve_agent_strategy
    from harness.config import DEFAULTS, get_config

    assert DEFAULTS["agent_strategy"] is None
    selected, source = resolve_agent_strategy(get_config({}))
    assert selected == "daily"
    assert source == "default"
    # An explicitly configured strategy is honored, not ignored.
    assert resolve_agent_strategy(get_config({"agent_strategy": "research"})) == (
        "research",
        "config",
    )
    with pytest.raises(ValueError):
        resolve_agent_strategy(get_config({"agent_strategy": "not-a-strategy"}))

    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps({"tool": "read", "path": "app.py"}),
            json.dumps({"tool": "finish", "answer": "read it"}),
        ]
    )
    kernel = _kernel(repo, tmp_path, model)
    result = kernel.run(_spec(repo, run_id="artifact-run"), strategy="daily")
    assert result.status == "completed_unverified"
    run_dir = tmp_path / "logs" / "artifact-run"
    assert (run_dir / "trace.jsonl").is_file()
    assert (run_dir / "checkpoint.json").is_file()
    assert (run_dir / "turns.jsonl").is_file()
    assert (run_dir / "pristine").is_dir()
    assert (tmp_path / "logs" / "session-1" / "session.json").is_file()
    rows = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in rows if row["event"] == "strategy_selected"] == [
        "strategy_selected"
    ]
    assert rows[0]["payload"]["strategy"] == "daily"
    assert rows[0]["payload"]["strategy_source"] == "explicit"
    assert any(row["event"] == "turn_recorded" for row in rows)


def test_done_without_verification_never_becomes_a_verified_status(tmp_path):
    """A finish request is a request, not proof, on every adapter."""
    from harness import deps
    from harness.agent_loop import run_agent, run_agent_kernel

    repo = _repo(tmp_path)
    for task_id, runner in (
        ("kernel-done", run_agent_kernel),
        ("compat-done", run_agent),
    ):
        replies = iter([json.dumps({"tool": "done", "answer": "all good"})])
        deps.set_call_model(lambda messages, _queue=replies, **kwargs: next(_queue))
        try:
            result = runner(
                "change",
                str(repo),
                {"steering_enabled": False},
                log_root=tmp_path / "logs",
                task_id=task_id,
            )
        finally:
            deps.reset_overrides()
        assert result["status"] == "completed_unverified", task_id
        assert result["status"] != "success", task_id
        assert result["kernel_status"] == "completed_unverified", task_id
        projection = RunEventJournal(result["trace_path"]).replay()
        assert projection.final_status == "completed_unverified", task_id


def test_default_and_compatibility_paths_share_one_verification_contract(tmp_path):
    """The strict kernel and the legacy adapter must agree on status."""
    from harness import deps
    from harness.agent_loop import run_agent, run_agent_kernel

    repo = _repo(tmp_path)
    outcomes = {}
    for task_id, runner in (
        ("strict", run_agent_kernel),
        ("compat", run_agent),
    ):
        replies = iter([json.dumps({"tool": "done", "answer": "same answer"})])
        deps.set_call_model(lambda messages, _queue=replies, **kwargs: next(_queue))
        try:
            result = runner(
                "change",
                str(repo),
                {"steering_enabled": False},
                log_root=tmp_path / "logs",
                task_id=task_id,
            )
        finally:
            deps.reset_overrides()
        outcomes[task_id] = (
            result["status"],
            result["kernel_status"],
            RunEventJournal(result["trace_path"]).replay().final_status,
        )
    assert outcomes["strict"] == outcomes["compat"]
    assert outcomes["strict"][0] == "completed_unverified"


def test_core_run_task_refuses_to_report_unverified_success(tmp_path):
    """The core adapter must never surface success without verifier evidence.

    ``run_task`` maps the kernel's statuses onto the historical ``TaskResult``
    vocabulary. Only a verifier-minted ``completed_verified`` may become
    ``success``; an unverified completion is a non-success outcome.
    """
    import harness.agent_kernel as kernel_module
    from harness import core
    from harness.agent_kernel import AgentKernel, CompletionStatus, RunResult
    from shared.types import Task

    repo = _repo(tmp_path)
    task = Task(
        task_id="unverified-core",
        repo_path=str(repo),
        issue_text="change",
        config={"steering_enabled": False},
    )
    forged = RunResult(
        status=CompletionStatus.COMPLETED_UNVERIFIED,
        answer="claimed",
        run_id="unverified-core",
        session_id="session-unverified-core",
        trace_path=str(tmp_path / "logs" / "unverified-core" / "trace.jsonl"),
    )

    class _ForgedKernel(AgentKernel):
        def run(self, spec, *, strategy=None, resume=False):
            self.last_legacy_result = None
            return forged

    saved = kernel_module.AgentKernel
    kernel_module.AgentKernel = _ForgedKernel
    try:
        result = core.run_task(task, log_root=tmp_path / "logs")
    finally:
        kernel_module.AgentKernel = saved
    assert result.status != "success"
    assert result.status == "failed"
