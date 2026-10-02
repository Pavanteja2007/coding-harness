"""Regression coverage for the extension hook and plugin lifecycle contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from extensions.hooks import (
    HookContext,
    HookDecision,
    HookManager,
    HookPoint,
    HookSecurityError,
)
from extensions.plugins import (
    Plugin,
    PluginManager,
    PluginManifest,
    PluginManifestError,
    PluginRegistrationError,
    PluginSecurityError,
    PluginSpec,
    PluginState,
    load_manifest,
    load_plugin_file,
)
from shared import security
from shared.agent_contracts import RunResult


def test_every_hook_point_dispatches_and_records_one_row() -> None:
    manager = HookManager()
    points = [
        HookPoint.TASK_BEFORE,
        HookPoint.TASK_AFTER,
        HookPoint.TOOL_BEFORE,
        HookPoint.TOOL_AFTER,
        HookPoint.PERMISSION_BEFORE,
        HookPoint.PERMISSION_AFTER,
        HookPoint.COMPLETION_BEFORE,
        HookPoint.COMPLETION_AFTER,
    ]
    for point in points:
        manager.register(point, lambda context: HookDecision(), plugin_id="test")

    for point in points:
        outcome = manager.dispatch(
            point, HookContext(result=RunResult(status="failed"))
        )
        assert outcome.point == point
        assert len(outcome.records) == 1
        assert outcome.records[0].point == point
        assert manager.records[-1].point == point


def test_priority_then_registration_order_is_deterministic() -> None:
    manager = HookManager()
    order: list[str] = []
    manager.register(HookPoint.TASK_BEFORE, lambda context: order.append("low") or None)
    manager.register(
        HookPoint.TASK_BEFORE, lambda context: order.append("high") or None, priority=10
    )
    manager.register(
        HookPoint.TASK_BEFORE,
        lambda context: order.append("high-2") or None,
        priority=10,
    )

    outcome = manager.dispatch(HookPoint.TASK_BEFORE)

    assert order == ["high", "high-2", "low"]
    assert [record.registration_order for record in outcome.records] == [2, 3, 1]


def test_before_hooks_merge_bounded_mutations_and_can_deny() -> None:
    manager = HookManager()
    manager.register(
        HookPoint.TOOL_BEFORE,
        lambda context: HookDecision(
            mutation={"arguments": {"path": "safe.py"}, "payload": {"size": 1}}
        ),
        priority=2,
    )
    manager.register(
        HookPoint.TOOL_BEFORE,
        lambda context: HookDecision(mutation={"arguments": {"mode": "read"}}),
        priority=1,
    )
    outcome = manager.dispatch(HookPoint.TOOL_BEFORE, {"payload": {"old": True}})

    assert outcome.allowed
    assert outcome.mutation["arguments"] == {"path": "safe.py", "mode": "read"}
    assert outcome.payload == {"size": 1}

    bounded = HookDecision(
        mutation={"items": list(range(1000)), "secret": "api_key=sk-unit-value"}
    )
    assert len(bounded.mutation["items"]) <= 128
    assert "sk-unit-value" not in repr(bounded)

    manager.clear()
    manager.register(HookPoint.TASK_BEFORE, lambda context: HookDecision.deny("policy"))
    manager.register(
        HookPoint.TASK_BEFORE, lambda context: (_ for _ in ()).throw(AssertionError())
    )
    denied = manager.dispatch(HookPoint.TASK_BEFORE)
    assert denied.denied
    assert not denied.allowed
    assert denied.reason if hasattr(denied, "reason") else True
    assert denied.records[1].skipped


def test_short_circuit_is_not_a_security_warning() -> None:
    manager = HookManager()
    manager.register(
        HookPoint.TASK_BEFORE,
        lambda context: {"short_circuit": True, "payload": {"stop": True}},
    )
    outcome = manager.dispatch(HookPoint.TASK_BEFORE)

    assert outcome.short_circuited
    assert not outcome.should_continue
    assert outcome.payload == {"stop": True}


def test_observational_after_and_completion_cannot_upgrade_result() -> None:
    manager = HookManager()
    result = RunResult(status="failed", answer="canonical")
    for point in (
        HookPoint.TASK_AFTER,
        HookPoint.TOOL_AFTER,
        HookPoint.COMPLETION_AFTER,
    ):
        manager.register(
            point,
            lambda context: HookDecision(
                action="allow",
                mutation={"status": "completed_verified"},
                payload={"status": "completed_verified"},
            ),
        )
        outcome = manager.dispatch(point, {"result": result})
        assert outcome.result is result
        assert outcome.result.status == "failed"
        assert outcome.context.result.status == "failed"
        assert outcome.allowed


def test_completion_callback_receives_redacted_canonical_run_result() -> None:
    manager = HookManager()
    seen: list[RunResult] = []
    secret = "sk-live-unit-secret-value"
    result = RunResult(
        status="failed",
        answer=secret,
        metadata={"api_key": secret, "tokens": 4},
    )
    manager.register(
        HookPoint.COMPLETION_AFTER, lambda context: seen.append(context.result)
    )
    outcome = manager.dispatch(HookPoint.COMPLETION_AFTER, {"result": result})

    assert len(seen) == 1
    assert isinstance(seen[0], RunResult)
    assert seen[0].status == "failed"
    assert secret not in repr(seen[0])
    assert seen[0].metadata["api_key"] == security.REDACTED_SECRET
    assert seen[0].metadata["tokens"] == 4
    assert outcome.result is result
    assert secret not in repr(outcome)
    assert secret not in json.dumps(outcome.to_dict())


def test_permission_hooks_accept_only_canonical_actions() -> None:
    manager = HookManager()
    manager.register(HookPoint.PERMISSION_BEFORE, lambda context: {"action": "ask"})
    asked = manager.dispatch(
        HookPoint.PERMISSION_BEFORE, {"permission": {"action": "allow"}}
    )
    assert asked.action == "ask"
    assert not asked.allowed
    assert asked.decision.needs_approval

    manager.clear()
    manager.register(
        HookPoint.PERMISSION_BEFORE, lambda context: {"action": "allow_all"}
    )
    invalid = manager.dispatch(HookPoint.PERMISSION_BEFORE)
    assert invalid.action == "allow"
    assert invalid.records[0].error
    assert not invalid.denied

    manager.clear()
    manager.register(HookPoint.PERMISSION_BEFORE, lambda context: {"action": "deny"})
    denied = manager.dispatch(HookPoint.PERMISSION_BEFORE)
    assert denied.denied


def test_bad_hook_is_recorded_and_later_hook_still_runs() -> None:
    manager = HookManager()
    seen: list[str] = []
    secret = "sk-unit-secret-value"
    manager.register(
        HookPoint.TASK_BEFORE,
        lambda context: (_ for _ in ()).throw(RuntimeError(secret)),
        priority=2,
    )
    manager.register(
        HookPoint.TASK_BEFORE, lambda context: seen.append("ran") or None, priority=1
    )

    outcome = manager.dispatch(HookPoint.TASK_BEFORE, {"metadata": {"api_key": secret}})

    assert seen == ["ran"]
    assert outcome.allowed
    assert len(outcome.records) == 2
    assert outcome.records[0].error
    assert secret not in repr(outcome.records[0])
    assert secret not in repr(outcome)
    assert manager.records[0].error


def test_explicit_security_violation_is_not_downgraded_to_warning() -> None:
    manager = HookManager()
    manager.register(
        HookPoint.TASK_BEFORE,
        lambda context: HookDecision(
            action="deny", reason="blocked", security_violation=True
        ),
    )
    with pytest.raises(HookSecurityError):
        manager.dispatch(HookPoint.TASK_BEFORE)
    assert manager.records[0].error


def test_context_metadata_and_repr_redaction_uses_shared_policy() -> None:
    secret = "api_key=sk-unit-secret-value"
    context = HookContext(
        task_id="task-1",
        metadata={"authorization": secret, "tokens": 9},
        data={"message": secret},
        payload={"password": secret},
    )

    assert context.metadata["authorization"] == security.REDACTED_SECRET
    assert context.metadata["tokens"] == 9
    assert secret not in repr(context)
    assert secret not in json.dumps(context.to_dict())


def test_plugin_lifecycle_is_idempotent_and_cleans_hooks() -> None:
    hooks = HookManager()
    activations: list[str] = []
    deactivations: list[str] = []

    def activate(manager: HookManager) -> str:
        activations.append("active")
        return manager.register(HookPoint.TASK_BEFORE, lambda context: None)

    def deactivate(manager: HookManager) -> None:
        deactivations.append("inactive")

    manager = PluginManager(hooks=hooks)
    plugin = Plugin("alpha", activate=activate, deactivate=deactivate)
    registered = manager.register(plugin)

    assert registered.state == PluginState.REGISTERED
    assert manager.activate("alpha") is registered
    assert manager.activate("alpha") is registered
    assert activations == ["active"]
    assert registered.state == PluginState.ACTIVE
    assert hooks.registration_ids("alpha")

    manager.deactivate("alpha")
    manager.deactivate("alpha")
    assert deactivations == ["inactive"]
    assert registered.state == PluginState.DEACTIVATED
    assert not hooks.registration_ids("alpha")

    manager.activate("alpha")
    assert registered.activation_count == 2
    manager.remove("alpha")
    assert registered.state == PluginState.REMOVED
    assert "alpha" not in manager


def test_factory_is_lazy_hydrated_once_and_reload_uses_fresh_instance() -> None:
    hooks = HookManager()
    created: list[int] = []

    def factory() -> Plugin:
        created.append(1)
        return Plugin("factory", activate=lambda manager: None)

    manager = PluginManager(hooks=hooks, factories={"factory": factory})
    plugin = manager.register("factory")
    assert created == []
    manager.activate("factory")
    manager.activate("factory")
    assert created == [1]
    reloaded = manager.reload("factory")
    assert created == [1, 1]
    assert reloaded is not plugin
    assert reloaded.state == PluginState.ACTIVE


def test_activation_failure_rolls_back_partial_hooks_and_isolates_peer() -> None:
    hooks = HookManager()
    peer_calls: list[str] = []

    def bad_activate(manager: HookManager) -> None:
        manager.register(HookPoint.TASK_BEFORE, lambda context: None)
        raise RuntimeError("activation failed sk-unit-secret-value")

    def peer_activate(manager: HookManager) -> None:
        peer_calls.append("peer")
        manager.register(
            HookPoint.TASK_BEFORE, lambda context: {"mutation": {"peer": True}}
        )

    manager = PluginManager(hooks=hooks)
    bad = manager.register(Plugin("bad", activate=bad_activate))
    peer = manager.register(Plugin("peer", activate=peer_activate))
    manager.activate("bad")
    manager.activate("peer")

    assert bad.state == PluginState.FAILED
    assert not hooks.registration_ids("bad")
    assert "sk-unit-secret-value" not in repr(bad)
    assert peer.state == PluginState.ACTIVE
    assert peer_calls == ["peer"]
    assert hooks.registration_ids("peer")


def test_disable_enable_remove_state_transitions() -> None:
    hooks = HookManager()
    manager = PluginManager(hooks=hooks)
    plugin = manager.register(Plugin("toggle", activate=lambda manager: None))
    manager.activate("toggle")
    manager.disable("toggle")
    assert plugin.state == PluginState.DISABLED
    assert not plugin.enabled
    manager.enable("toggle")
    assert plugin.state == PluginState.REGISTERED
    assert plugin.enabled
    manager.activate("toggle")
    manager.remove("toggle")
    assert plugin.state == PluginState.REMOVED


def test_manifest_validation_rejects_unsafe_shapes() -> None:
    manifest = PluginManifest.from_dict(
        {"name": "safe", "version": "1.2.3", "metadata": {"api_key": "sk-unit"}}
    )
    assert manifest.name == "safe"
    assert manifest.metadata["api_key"] == security.REDACTED_SECRET
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict({"name": "../escape"})
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict({"name": "safe", "entrypoint": "../outside.py"})
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict({"name": "safe", "enabled": "yes"})


def test_explicit_local_file_loader_is_contained(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    source = root / "local.py"
    source.write_text(
        "def create_plugin():\n    return {'activate': lambda hooks: None}\n",
        encoding="utf-8",
    )
    spec = load_plugin_file(source, root)
    assert spec.name == "local"
    manager = PluginManager(plugin_root=root)
    plugin = manager.load_plugin(source)
    manager.activate("local")
    assert plugin.state == PluginState.ACTIVE

    outside = tmp_path / "outside.py"
    outside.write_text("def create_plugin():\n    return {}\n", encoding="utf-8")
    with pytest.raises(PluginSecurityError):
        load_plugin_file(outside, root)


def test_local_manifest_loader_and_symlink_rejection(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    manifest_path = root / "plugin.json"
    manifest_path.write_text(
        json.dumps({"name": "manifest", "version": "1.0.0"}), encoding="utf-8"
    )
    assert load_manifest(manifest_path, root).name == "manifest"

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside"}), encoding="utf-8")
    with pytest.raises(PluginSecurityError):
        load_manifest(outside, root)

    try:
        link = root / "linked.json"
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(PluginSecurityError):
        load_manifest(link, root)


def test_duplicate_registration_is_typed() -> None:
    manager = PluginManager()
    manager.register(PluginSpec(name="one", plugin=Plugin("one")))
    with pytest.raises(PluginRegistrationError):
        manager.register(PluginSpec(name="one", plugin=Plugin("one")))


def test_concurrent_registration_and_dispatch_is_safe() -> None:
    manager = HookManager()
    errors: list[BaseException] = []

    def register_worker(index: int) -> None:
        try:
            manager.register(
                HookPoint.TASK_BEFORE,
                lambda context: None,
                plugin_id=f"p{index}",
            )
        except BaseException as exc:
            errors.append(exc)

    def dispatch_worker(_: int) -> None:
        try:
            for _ in range(20):
                outcome = manager.dispatch(HookPoint.TASK_BEFORE)
                assert outcome.allowed
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(register_worker, index) for index in range(20)]
        futures.extend(executor.submit(dispatch_worker, index) for index in range(8))
        for future in futures:
            future.result()

    assert not errors
    assert len(manager.registrations(HookPoint.TASK_BEFORE)) == 20
    assert len(manager.records) >= 20
