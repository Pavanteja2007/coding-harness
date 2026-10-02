"""VEX-CEILING-04 regressions: native tool protocol, one catalog, safe edits.

Every test here is offline and deterministic: the provider is a fake
callable or a fake litellm module, and no network, credential, or Docker lane
is used. A skipped lane is never reported as a pass.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from harness.agent_kernel import ModelGateway, ToolRegistry, ToolValidationError
from harness.agent_kernel.gateway import STOP_EMPTY, STOP_TRUNCATED
from harness.agent_kernel.tools import (
    ERROR_AMBIGUOUS_MATCH,
    ERROR_LOOP_DETECTED,
    ERROR_NO_MATCH,
    ERROR_STALE_READ,
    ToolResult,
    builtin_tool_specs,
    catalog_parity,
    dedupe_tool_calls,
    json_safe,
    parse_model_response,
)
from harness.tools import (
    TypedToolValidationError,
    catalog_fingerprint,
    catalog_parity_report,
    typed_tool_schemas,
    typed_tool_specs,
    validate_typed_arguments,
)

# -- helpers -------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _context(repo: Path, **config) -> dict:
    payload = {"repo_path": str(repo), "config": {"max_repeat_tool_calls": 2}}
    payload.update(config)
    return payload


def _recorder(seen: list):
    """Return a handler that records its call and reports success."""

    def handler(call, context):
        seen.append(dict(call.arguments))
        return ToolResult(True, "ran", "")

    return handler


def _read_handler(repo: Path):
    def read(call, context):
        text = (repo / call.arguments["path"]).read_text(encoding="utf-8")
        return ToolResult(True, text, "")

    return read


# -- 1. native provider tool calling -------------------------------------


def test_native_tool_schema_reaches_a_fake_provider_request():
    """The catalog's provider schema is actually sent, not dropped."""
    seen: dict = {}
    schemas = typed_tool_schemas()

    def fake_provider(messages, **kwargs):
        seen.update(kwargs)
        return {
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": {"path": "app.py"}},
                }
            ],
        }

    gateway = ModelGateway(call_fn=fake_provider, tool_schemas=schemas)
    response = gateway.call([{"role": "user", "content": "go"}], tools=schemas)

    assert seen["tools"] == schemas
    assert any(entry["function"]["name"] == "read" for entry in seen["tools"]), (
        "the read schema must reach the provider verbatim"
    )
    assert response.tool_protocol == "native"
    assert response.stop_reason == "tool_calls"
    assert response.tool_calls[0]["tool"] == "read"
    assert response.tool_calls[0]["arguments"] == {"path": "app.py"}
    assert gateway.calls[-1]["tool_protocol"] == "native"
    assert gateway.calls[-1]["tools_sent"] == len(schemas)


def test_router_forwards_tools_to_litellm_and_returns_normalized_calls(monkeypatch):
    """The router reaches the provider with the schemas and normalizes the reply."""
    from runtime import model_router
    from runtime.model_router import get_last_usage, set_call_context

    capture: dict = {}
    schemas = typed_tool_schemas()

    def completion(**kwargs):
        capture.update(kwargs)
        message = types.SimpleNamespace(
            content=None,
            tool_calls=[
                types.SimpleNamespace(
                    id="call-7",
                    type="function",
                    function=types.SimpleNamespace(
                        name="read", arguments='{"path": "app.py"}'
                    ),
                )
            ],
        )
        choice = types.SimpleNamespace(message=message, finish_reason="tool_calls")
        return types.SimpleNamespace(
            choices=[choice],
            usage=types.SimpleNamespace(prompt_tokens=9, completion_tokens=3),
            _hidden_params={},
        )

    monkeypatch.setitem(
        __import__("sys").modules,
        "litellm",
        types.SimpleNamespace(completion=completion),
    )
    set_call_context({"model": "gpt-4o-mini"})
    try:
        out = model_router.call_model(
            [{"role": "user", "content": "read app.py"}], tools=schemas
        )
        usage = get_last_usage()
    finally:
        set_call_context(None)

    assert capture["tools"] == schemas
    assert isinstance(out, dict)
    assert out["tool_calls"] == [
        {
            "id": "call-7",
            "type": "function",
            "function": {"name": "read", "arguments": {"path": "app.py"}},
        }
    ]
    assert out["finish_reason"] == "tool_calls"
    assert usage["stop_reason"] == "tool_calls"
    assert usage["tool_calls"] == 1


def test_router_keeps_the_historical_string_when_no_native_call(monkeypatch):
    from runtime import model_router
    from runtime.model_router import set_call_context

    def completion(**kwargs):
        message = types.SimpleNamespace(content="plain text", tool_calls=None)
        choice = types.SimpleNamespace(message=message, finish_reason="stop")
        return types.SimpleNamespace(
            choices=[choice],
            usage=types.SimpleNamespace(prompt_tokens=2, completion_tokens=1),
            _hidden_params={},
        )

    monkeypatch.setitem(
        __import__("sys").modules,
        "litellm",
        types.SimpleNamespace(completion=completion),
    )
    set_call_context({"model": "gpt-4o-mini"})
    try:
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}], tools=typed_tool_schemas()
        )
    finally:
        set_call_context(None)
    assert out == "plain text"


def test_text_protocol_fallback_is_explicit_in_the_trace():
    """A text reply is recorded as a fallback, never as a native call."""
    events: list = []

    class Trace:
        def log(self, kind, payload):
            events.append((kind, payload))

    gateway = ModelGateway(
        call_fn=lambda messages, **kwargs: json.dumps(
            {"tool": "read", "path": "app.py"}
        ),
        tool_schemas=typed_tool_schemas(),
        trace=Trace(),
    )
    response = gateway.call([{"role": "user", "content": "go"}])
    calls = gateway.parse(response, ["read"])

    assert response.tool_protocol == "text"
    assert calls[0]["tool"] == "read"
    assert gateway.calls[-1]["tool_protocol"] == "text"
    assert gateway.calls[-1]["tools_sent"] == len(typed_tool_schemas())
    kinds = [kind for kind, _ in events]
    assert "model_response" in kinds
    payload = next(p for kind, p in events if kind == "model_response")
    assert payload["usage"]["tool_protocol"] == "text"


def test_boundary_without_tools_parameter_still_works():
    """A pre-tool-protocol boundary keeps working; only accepted kwargs pass."""

    def old_boundary(
        messages, difficulty_hint=None, provider=None, model=None, api_key=None
    ):
        return {"content": json.dumps({"tool": "read", "path": "app.py"})}

    gateway = ModelGateway(call_fn=old_boundary, tool_schemas=typed_tool_schemas())
    response = gateway.call([{"role": "user", "content": "go"}])
    assert not response.failed
    assert gateway.parse(response, ["read"])[0]["tool"] == "read"


# -- 2. validation before persistence ------------------------------------


def test_malformed_tool_json_does_not_wedge_a_session(tmp_path):
    """Malformed JSON becomes model-facing feedback, never a dead session."""
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    reply = '{"tool": "read", "path": "app.py"'
    calls = parse_model_response(reply, registry.names)
    assert calls and "malformed" in calls[0]
    assert calls[0]["recovery"] == "emit_one_tool_call"

    results = registry.dispatch(calls, _context(repo))
    assert len(results) == 1
    assert results[0].ok is False
    assert "Emit one valid typed tool call." in str(results[0].output)
    assert registry.failure_report()["protocol_failures"] >= 1

    # The session is still usable: the next well-formed call runs.
    registry.set_handler("read", _read_handler(repo))
    good = parse_model_response(
        json.dumps({"tool": "read", "path": "app.py"}), registry.names
    )
    results = registry.dispatch(good, _context(repo))
    assert results[0].ok is True, str(results[0].output)


def test_validation_errors_are_returned_as_tool_results(tmp_path):
    registry = ToolRegistry()
    results = registry.dispatch(
        [
            {"tool": "read"},  # missing required argument
            {"tool": "not_a_tool", "arguments": {}},  # unknown tool
            {"tool": "read", "arguments": {"path": "../escape"}},  # traversal
        ],
        _context(_repo(tmp_path)),
    )
    assert [item.ok for item in results] == [False, False, False]
    assert all("TOOL ERROR" in str(item.output) for item in results)
    report = registry.failure_report()
    assert report["protocol_failures"] == 3
    assert report["task_failures"] == 0
    with pytest.raises(ToolValidationError):
        registry.validate({"tool": "read", "arguments": {}})


def test_persisted_tool_calls_are_always_json_serializable():
    class Weird:
        def __repr__(self):
            return "<weird>"

    calls = parse_model_response(
        {"tool": "edit", "path": "app.py", "old_string": "a", "new_string": Weird()},
        None,
    )
    payload = json.dumps(calls)
    assert "<weird>" in payload
    assert json_safe({"bad": {1, 2}}) == {"bad": [1, 2]}


def test_duplicated_stream_event_executes_a_tool_exactly_once(tmp_path):
    """A retried provider event must not run the tool twice."""
    repo = _repo(tmp_path)
    executed: list = []
    registry = ToolRegistry()
    registry.set_handler("shell", _recorder(executed))
    events = [
        {
            "event_id": "evt-1",
            "tool": "shell",
            "arguments": {"command": "python -m pytest"},
        },
        {
            "event_id": "evt-1",
            "tool": "shell",
            "arguments": {"command": "python -m pytest"},
        },
    ]
    seen: set = set()
    calls = dedupe_tool_calls(
        parse_model_response(events, registry.names), seen_event_ids=seen
    )
    assert len(calls) == 1
    for call in calls:
        registry.execute(call, _context(repo, config={"max_repeat_tool_calls": 5}))
    assert [item["command"] for item in executed] == ["python -m pytest"]
    assert registry.failure_report()["deduplicated_events"] == 0


def test_gateway_deduplicates_repeated_event_ids_across_turns():
    gateway = ModelGateway(
        call_fn=lambda messages, **kwargs: {
            "content": "",
            "tool_calls": [
                {
                    "id": "dup-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": {"path": "a.py"}},
                }
            ],
        }
    )
    first = gateway.parse(gateway.call([]), ["read"])
    second = gateway.parse(gateway.call([]), ["read"])
    assert len(first) == 1
    assert second == []


def test_registry_note_event_rejects_a_repeated_provider_event():
    registry = ToolRegistry()
    assert registry.note_event("evt-1") is True
    assert registry.note_event("evt-1") is False
    assert registry.failure_report()["deduplicated_events"] == 1


def test_incomplete_stop_reason_is_inferred_not_silently_ended():
    truncated = ModelGateway._normalize(
        {"choices": [{"message": {"content": "half a sen"}, "finish_reason": "length"}]}
    )
    assert truncated.stop_reason == STOP_TRUNCATED
    empty = ModelGateway._normalize({"choices": [{"message": {"content": ""}}]})
    assert empty.stop_reason == STOP_EMPTY


def test_protocol_failures_are_tracked_separately_from_task_failures(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    registry.set_handler(
        "shell",
        lambda call, context: ToolResult(False, "exit=1", ""),
    )
    with pytest.raises(ToolValidationError):
        registry.execute({"tool": "read", "arguments": {}}, _context(repo))
    for index in range(3):
        registry.execute(
            {"tool": "shell", "arguments": {"command": f"run-{index}"}},
            _context(repo),
        )
    report = registry.failure_report()
    assert report["protocol_failures"] == 1
    assert report["task_failures"] == 3


# -- 3. one catalog ------------------------------------------------------


def test_kernel_and_legacy_catalogs_have_identical_names_and_schemas():
    """The kernel catalog IS the production catalog, provably."""
    derived = builtin_tool_specs()
    canonical = list(typed_tool_specs())
    assert [spec.name for spec in derived] == [spec.name for spec in canonical]
    assert catalog_fingerprint(derived) == catalog_fingerprint(canonical)
    parity = catalog_parity()
    assert parity["identical"] is True
    assert parity["differences"] == []

    report = catalog_parity_report(derived)
    assert report["missing"] == []
    assert report["extra"] == []
    assert report["divergent"] == []

    registry = ToolRegistry()
    assert registry.canonical_names == [spec.name for spec in canonical]
    assert registry.schemas() == typed_tool_schemas()


def test_catalog_divergence_is_detected_not_tolerated():
    """A hand-edited kernel spec is reported instead of silently shipped."""
    from harness.agent_kernel.tools import ToolSpec

    drifted = [ToolSpec("read", "read_only", (), (), {}, True)]
    report = catalog_parity_report(drifted)
    assert report["identical"] is False
    assert len(report["missing"]) == len(typed_tool_specs()) - 1
    assert report["differences"]


def test_catalog_covers_the_required_tool_set():
    names = {spec.name for spec in typed_tool_specs()}
    for required in (
        "read",
        "edit",
        "write",
        "apply_patch",
        "delete",
        "rename",
        "undo",
        "test",
        "lint",
        "typecheck",
    ):
        assert required in names, f"{required} must be in the one catalog"


def test_restrict_keeps_a_tool_named_by_its_alias():
    registry = ToolRegistry()
    registry.restrict(["read", "question"])
    assert "ask" in registry.canonical_names
    registry.validate(
        {
            "tool": "question",
            "arguments": {"question": "which behavior?"},
        }
    )


def test_kernel_registers_a_handler_for_every_catalog_tool():
    """No catalogued tool is advertised without a way to run it."""
    from harness.agent_kernel import build_default_handlers

    registry = ToolRegistry()
    build_default_handlers(registry, repo_path=".", config={})
    context = _context(Path("."))
    for name in registry.canonical_names:
        call = {"tool": name, "arguments": _minimal_arguments(name)}
        result = registry.execute(call, context)
        refused = "no handler registered" in str(result.output)
        assert not refused, f"{name} has no handler in the strict kernel"


def _minimal_arguments(name: str) -> dict:
    from harness.tools import typed_tool_spec

    spec = typed_tool_spec(name)
    payload: dict = {}
    for field in spec.required:
        kind = spec.types.get(field, str)
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if str in kinds:
            payload[field] = "x"
        elif int in kinds:
            payload[field] = 1
        elif bool in kinds:
            payload[field] = True
        elif list in kinds:
            payload[field] = ["x"]
        elif dict in kinds:
            payload[field] = {}
    return payload


# -- 4. safe edits -------------------------------------------------------


def test_read_returns_a_content_digest(tmp_path):
    import hashlib

    repo = _repo(tmp_path)
    registry = ToolRegistry()
    registry.set_handler("read", _read_handler(repo))
    result = registry.execute(
        {"tool": "read", "arguments": {"path": "app.py"}}, _context(repo)
    )
    assert result.ok is True
    assert result.digest == hashlib.sha256(b"value = 1\n").hexdigest()
    assert "[neo-file-digest]" in str(result.output)
    assert f"sha256={result.digest}" in str(result.output)
    assert registry.failure_report()["read_digests"] == 1


def test_stale_edit_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    executed: list = []
    registry.set_handler("read", _read_handler(repo))
    registry.set_handler("edit", _recorder(executed))
    read = registry.execute(
        {"tool": "read", "arguments": {"path": "app.py"}}, _context(repo)
    )
    stale_digest = read.digest
    # Somebody else edits the file after the agent read it.
    (repo / "app.py").write_text("value = 99\n", encoding="utf-8")

    result = registry.execute(
        {
            "tool": "edit",
            "arguments": {
                "path": "app.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
                "expected_revision": stale_digest,
            },
        },
        _context(repo),
    )
    assert result.ok is False
    assert result.error_kind == ERROR_STALE_READ
    assert "stale_read" in str(result.output)
    assert executed == []
    assert registry.failure_report()["stale_reads"] == 1


def test_mutation_binds_a_recorded_digest_never_a_dispatch_time_hash(tmp_path):
    """An omitted digest binds the session's earlier observation."""
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    captured: list = []
    registry.set_handler("read", _read_handler(repo))
    registry.set_handler("edit", _recorder(captured))
    registry.execute({"tool": "read", "arguments": {"path": "app.py"}}, _context(repo))
    (repo / "app.py").write_text("value = 42\n", encoding="utf-8")
    result = registry.execute(
        {
            "tool": "edit",
            "arguments": {
                "path": "app.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
        },
        _context(repo),
    )
    assert result.ok is False
    assert result.error_kind == ERROR_STALE_READ
    assert captured == []


def test_strict_digest_policy_refuses_a_file_the_session_never_read(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    executed: list = []
    registry.set_handler("edit", _recorder(executed))
    context = _context(repo)
    context["config"]["require_edit_digest"] = True
    result = registry.execute(
        {
            "tool": "edit",
            "arguments": {
                "path": "app.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
        },
        context,
    )
    assert result.ok is False
    assert result.error_kind == ERROR_STALE_READ
    assert "never read" in str(result.output)
    assert executed == []


def test_ambiguous_multi_match_replacement_is_never_applied_silently(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    registry = ToolRegistry()
    executed: list = []
    registry.set_handler("edit", _recorder(executed))
    result = registry.execute(
        {
            "tool": "edit",
            "arguments": {
                "path": "dup.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
            },
        },
        _context(repo),
    )
    assert result.ok is False
    assert result.error_kind == ERROR_AMBIGUOUS_MATCH
    assert executed == []
    assert registry.failure_report()["ambiguous_matches"] == 1


def test_missing_old_string_is_refused_with_an_exact_slug(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    executed: list = []
    registry.set_handler("edit", _recorder(executed))
    result = registry.execute(
        {
            "tool": "edit",
            "arguments": {
                "path": "app.py",
                "old_string": "not present",
                "new_string": "x",
            },
        },
        _context(repo),
    )
    assert result.ok is False
    assert result.error_kind == ERROR_NO_MATCH


def test_production_typed_runtime_still_requires_the_digest():
    """The strict production validation is unchanged and still fail-closed."""
    with pytest.raises(TypedToolValidationError, match="expected_revision"):
        validate_typed_arguments(
            "edit",
            {"path": "app.py", "old_string": "a", "new_string": "b"},
        )


# -- 5. loop protection --------------------------------------------------


def test_repeated_identical_calls_trigger_loop_protection(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    executed: list = []
    registry.set_handler("shell", _recorder(executed))
    call = {"tool": "shell", "arguments": {"command": "python -m pytest"}}
    first = registry.execute(dict(call), _context(repo))
    second = registry.execute(dict(call), _context(repo))
    third = registry.execute(dict(call), _context(repo))

    assert first.ok and second.ok
    assert third.ok is False
    assert third.error_kind == ERROR_LOOP_DETECTED
    assert [item["command"] for item in executed] == [
        "python -m pytest",
        "python -m pytest",
    ]
    report = registry.failure_report()
    assert report["loop"]["blocked"] == 1
    assert report["loop"]["repeated"] == 1


def test_repeated_read_only_calls_are_not_refused(tmp_path):
    repo = _repo(tmp_path)
    registry = ToolRegistry()
    registry.set_handler(
        "read",
        lambda call, context: ToolResult(True, "value = 1\n", ""),
    )
    call = {"tool": "read", "arguments": {"path": "app.py"}}
    for _ in range(4):
        assert registry.execute(dict(call), _context(repo)).ok is True


def test_loop_guard_can_be_reset_between_phases():
    from harness.agent_kernel.tools import ToolLoopGuard

    guard = ToolLoopGuard(max_repeats=1)
    call = {"tool": "shell", "arguments": {"command": "x"}}
    assert guard.observe(call)[0] is False
    assert guard.observe(call)[0] is True
    guard.reset()
    assert guard.observe(call)[0] is False


def test_read_only_batch_parallelism_still_uses_the_catalog():
    registry = ToolRegistry()
    assert registry.can_run_parallel(
        [
            {"tool": "read", "arguments": {"path": "a.py"}},
            {"tool": "glob", "arguments": {"pattern": "*.py"}},
        ]
    )
    assert not registry.can_run_parallel(
        [{"tool": "shell", "arguments": {"command": "ls"}}]
    )
