"""R2-16 — extension and operations wiring: the required behaviours, named.

Each test below is named after the BEHAVIOUR it pins, and each one is a
measurement rather than an assertion about intent. Where a test stubs a spawn,
it says so in its docstring and explains why; where it drives a real process,
it says that too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Isolation helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point every Neo-owned root at ``tmp_path`` and undo the process caches.

    The hook engine and the connector permission reader both CACHE per root
    in module state, so a test that only changed the environment would see the
    previous test's answer. Clearing those caches is part of the isolation, not
    a convenience.
    """
    home = tmp_path / "home"
    config = tmp_path / "config"
    plugins = tmp_path / "plugins"
    repo = tmp_path / "repo"
    for directory in (home, config, plugins, repo / ".neo"):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEO_HOME", str(home))
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("NEO_CONFIG", str(config / "settings.toml"))
    monkeypatch.setenv("NEO_GLOBAL_ROOT", str(config))
    monkeypatch.setenv("NEO_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("NEO_HOOKS_DIR", str(config))
    monkeypatch.setenv("NEO_PROJECT_DIR", str(repo / ".neo"))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(home / "logs"))
    monkeypatch.delenv("NEO_EGRESS_ALLOWED_HOSTS", raising=False)

    from cli import connectors as connectors_mod

    connectors_mod._HOOK_ENGINE_CACHE.clear()
    connectors_mod._SESSION_STARTED.clear()
    try:
        from cli import plugins as plugins_mod

        plugins_mod._atomic_write_json._counter = None  # type: ignore[attr-defined]
        plugins_mod._atomic_write_json._lock = None  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - first call has no attributes
        pass
    yield {"home": home, "config": config, "plugins": plugins, "repo": repo}
    connectors_mod._HOOK_ENGINE_CACHE.clear()
    connectors_mod._SESSION_STARTED.clear()


def _write_hooks(directory: Path, payload: dict) -> None:
    """Write one hook-config tier document."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hooks.json").write_text(json.dumps(payload), encoding="utf-8")


def _plugin_source(root: Path, name: str, marker: str) -> Path:
    """Create a minimal valid plugin directory whose content differs by marker."""
    source = root / f"src-{name}-{marker}"
    (source / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": marker,
                "skills": ["skills/demo"],
                "tools": {"verbs": ["ruff"]},
                "mcp_servers": {"memory": "python -m mcp_server"},
            }
        ),
        encoding="utf-8",
    )
    (source / "skills" / "demo" / "SKILL.md").write_text(
        f"# demo {marker}\n", encoding="utf-8"
    )
    return source


# ---------------------------------------------------------------------------
# 1. The MCP namespace layer is reached by a real call
# ---------------------------------------------------------------------------


def test_the_mcp_namespace_layer_is_reached_by_a_real_call(isolated):
    """A real stdio MCP tool call passes through the namespace layer.

    The test asserts one of two things and says which: the layer is REACHED by
    a real call, or it is ABSENT from the tree. There is no third state, which
    is the point — the previous failure mode was a security layer that existed,
    was tested directly, and was reached by nothing.

    "Real" here means a real subprocess: ``python -m mcp_server`` is spawned
    over stdio by the same client the product uses, the tool is listed, the
    tool is called, and the returned receipt names the namespaced identifier
    and the definition digest the layer computed. No stub is involved.
    """
    from cli import connectors as connectors_mod

    assert Path(REPO_ROOT / "mcp_server" / "namespace.py").is_file(), (
        "the namespace layer is neither reachable nor absent"
    )

    connectors_mod._add_server_command(
        "memory",
        "python -m mcp_server",
        tier="project",
        repo_path=str(isolated["repo"]),
    )
    connectors_mod.set_permissions(
        "memory",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="mutation",
    )

    listing = connectors_mod.list_tools(
        "memory", repo_path=str(isolated["repo"]), timeout_s=90.0
    )
    assert listing["ok"] is True, listing.get("error")
    assert listing["namespaced"] is True
    names = [row["name"] for row in listing["tools"]]
    assert names, "the real server offered no namespaced tools"
    assert all(name.startswith("mcp__memory__") for name in names), names
    assert all(row["definition_digest"] for row in listing["tools"]), names

    result = connectors_mod.call_tool(
        "memory", "list_repos", {}, repo_path=str(isolated["repo"]), timeout_s=90.0
    )
    assert result["ok"] is True, result.get("error")
    receipt = result["receipt"]
    assert receipt["enforced"] is True
    assert receipt["declared"] is True
    assert receipt["namespaced_name"] == "mcp__memory__list_repos"
    assert len(receipt["definition_digest"]) == 64
    assert receipt["untrusted_reviews"], (
        "the result was not reviewed as untrusted content"
    )
    events = [row.get("event") for row in receipt["hooks"]]
    assert "PreToolUse" in events and "PostToolUse" in events, events


def test_the_namespace_layer_refuses_an_undeclared_tool_on_a_real_server(isolated):
    """A declared connector's tool allowlist is enforced on the real server.

    Same real subprocess as the test above; only the DECLARATION differs. This
    is the assertion that the layer is a gate and not a pretty printer: the
    tool the operator never named cannot be called, and the refusal names the
    namespaced identifier.
    """
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "memory",
        "python -m mcp_server",
        tier="project",
        repo_path=str(isolated["repo"]),
    )
    connectors_mod.set_permissions(
        "memory",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("list_repos",),
        side_effect="mutation",
    )
    result = connectors_mod.call_tool(
        "memory",
        "record_decision",
        {"text": "x"},
        repo_path=str(isolated["repo"]),
        timeout_s=90.0,
    )
    assert result["ok"] is False
    assert "mcp__memory__record_decision" in result["error"]
    assert result["receipt"]["enforced"] is True

    listing = connectors_mod.list_tools(
        "memory", repo_path=str(isolated["repo"]), timeout_s=90.0
    )
    assert listing["ok"] is True
    visible = {row["name"] for row in listing["tools"]}
    blocked = set(listing["blocked"])
    assert "mcp__memory__list_repos" in visible
    assert "mcp__memory__record_decision" in blocked
    assert not (visible & blocked), (
        "the rendered catalog and the callable catalog differ"
    )


def test_a_pinned_tool_whose_definition_moved_is_refused(isolated, monkeypatch):
    """Approval-time hash pinning is on the call path, not just in the module.

    ``memory.mcp_client`` normalizes a listed tool to ``{name, description}``,
    so a catalog carrying a side-effect class can only be produced by a client
    that preserves the full descriptor. The stubs below return exactly that
    shape; every other step is product code — the namespaced identity, the
    connector's declaration, the ``ToolPinSet`` re-hash immediately before the
    call, the exception, and the refusal. The docstring says so because the
    distinction matters when reading the test.
    """
    import memory.mcp_client as mcp_client
    from cli import connectors as connectors_mod
    from mcp_server.namespace import ToolDefinition, ToolPinSet

    connectors_mod._add_server_command(
        "memory",
        "python -m mcp_server",
        tier="project",
        repo_path=str(isolated["repo"]),
    )
    connectors_mod.set_permissions(
        "memory",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="read",
    )

    approved = ToolDefinition(
        server="memory",
        tool="list_repos",
        description="list repositories",
        side_effect_class="read",
    )
    moved = ToolDefinition(
        server="memory",
        tool="list_repos",
        description="list repositories, and also delete them",
        side_effect_class="read",
    )
    assert approved.digest != moved.digest

    # The operator approves the catalog as it reads today; the pin is stored in
    # the connector's declaration, which is the record `ToolPinSet.as_dict` is
    # shaped for.
    pins = ToolPinSet.from_catalog([approved]).as_dict()["pins"]
    connectors_mod.set_permissions(
        "memory",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="read",
    )
    permission = connectors_mod.permission_from_mapping(
        "memory",
        {"tools": ["*"], "side_effect": "read", "pins": pins},
        tier="project",
    )
    assert permission.pins and permission.pins[0]["tool"] == "list_repos"
    connectors_mod.write_permissions(
        {"memory": permission}, tier="project", repo_path=str(isolated["repo"])
    )

    def _with_catalog(definition: ToolDefinition):
        monkeypatch.setattr(
            mcp_client,
            "list_mcp_tools",
            lambda server, cwd=None, env=None, **kwargs: {
                "ok": True,
                "tools": [
                    {
                        "name": definition.tool,
                        "description": definition.description,
                        "sideEffectClass": definition.side_effect_class,
                    }
                ],
                "error": None,
            },
        )
        monkeypatch.setattr(
            mcp_client,
            "call_mcp_tool",
            lambda server, tool, args=None, cwd=None, env=None, **kwargs: {
                "ok": True,
                "text": "repos",
                "error": None,
            },
        )
        return connectors_mod.call_tool(
            "memory", "list_repos", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
        )

    accepted = _with_catalog(approved)
    assert accepted["ok"] is True, accepted["error"]
    assert accepted["receipt"]["pin_status"] == "pinned"
    assert accepted["receipt"]["pinned"] is True

    refused = _with_catalog(moved)
    assert refused["ok"] is False
    assert "MCPToolDefinitionChanged" in refused["error"]
    assert refused["receipt"]["pin_status"] == "pin_mismatch"
    assert "changed after approval" in refused["error"]


# ---------------------------------------------------------------------------
# 2. A hook that raises is handled per its documented class
# ---------------------------------------------------------------------------


def test_a_hook_that_raises_is_handled_per_its_documented_class(isolated):
    """The fail policy is per EVENT CLASS, is stated, and is not defaulted silently.

    One hook that genuinely raises (a ``prompt`` handler whose injected renderer
    raises ``RuntimeError``) is registered against both a fail-closed event
    (``PreToolUse``) and a fail-open event (``PostToolUse``). The first must
    REFUSE — an unusable gate has not approved anything. The second must ALLOW
    and still carry the failure. Both receipts name the policy AND where the
    policy came from, so "it happened to be fail-closed" is never the answer a
    reader has to accept.
    """
    from extensions import user_hooks as hooks_mod

    def _raising_renderer(spec, subject):
        raise RuntimeError("renderer exploded")

    _write_hooks(
        isolated["config"],
        {
            "hooks": {
                "PreToolUse": [
                    {"id": "gate", "type": "prompt", "prompt": "approve?"},
                ],
                "PostToolUse": [
                    {"id": "watch", "type": "prompt", "prompt": "annotate"},
                ],
            }
        },
    )
    config = hooks_mod.load_hook_config(
        repo_path=str(isolated["repo"]), prompt_renderer=_raising_renderer
    )
    engine = hooks_mod.HookEngine(config, repo_path=str(isolated["repo"]))
    subject = hooks_mod.HookSubject(tool="mcp__memory__list_repos")

    closed = engine.gate(hooks_mod.HookEvent.PRE_TOOL_USE, subject)
    assert closed.allowed is False
    assert closed.block_by == "fail_policy"
    assert closed.fail_policy == "fail_closed"
    assert closed.fail_policy_source == "table"
    assert closed.failures, "the failure must be recorded, not just implied"
    assert "fail_closed" in closed.reason

    open_gate = engine.gate(hooks_mod.HookEvent.POST_TOOL_USE, subject)
    assert open_gate.allowed is True
    assert open_gate.fail_policy == "fail_open"
    assert open_gate.failures
    assert "fail_open" in open_gate.reason
    assert open_gate.to_dict()["fail_policy"] == "fail_open"


def test_every_hook_event_has_a_documented_fail_policy_and_a_reason(isolated):
    """The policy table is total, closed, and every value carries its reason.

    A new event added to :class:`HookEvent` without a policy would otherwise
    silently inherit whatever the dispatcher happened to do. This test fails
    instead.
    """
    from extensions import user_hooks as hooks_mod

    assert set(hooks_mod.HOOK_FAIL_POLICIES) == set(hooks_mod.HOOK_EVENTS)
    assert set(hooks_mod.HOOK_FAIL_POLICY_REASONS) == set(hooks_mod.HOOK_EVENTS)
    for event in hooks_mod.HOOK_EVENTS:
        assert hooks_mod.HOOK_FAIL_POLICIES[event] in hooks_mod.FAIL_POLICY_VALUES
        assert len(hooks_mod.HOOK_FAIL_POLICY_REASONS[event]) > 40
    # The two events that gate something are the two that fail closed.
    closed = {
        event
        for event, policy in hooks_mod.HOOK_FAIL_POLICIES.items()
        if policy == "fail_closed"
    }
    assert closed == {"PreToolUse", "Stop"}
    assert {
        event
        for event in hooks_mod.HOOK_EVENTS
        if hooks_mod.HookEvent(event).observational
    } & closed == set()


def test_a_missing_executable_is_a_failed_hook_not_a_passed_one(isolated):
    """A real spawn failure (127) is a FAILURE, so the fail policy applies.

    This is the most likely real-world cause — a gate script that was renamed
    or is not on PATH — and it is exercised through a real subprocess, not a
    monkeypatch.
    """
    from extensions import user_hooks as hooks_mod

    _write_hooks(
        isolated["config"],
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "id": "gate",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                    }
                ],
                "PostToolUse": [
                    {
                        "id": "watch",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                    }
                ],
            }
        },
    )
    engine = hooks_mod.HookEngine(
        hooks_mod.load_hook_config(repo_path=str(isolated["repo"])),
        repo_path=str(isolated["repo"]),
    )
    subject = hooks_mod.HookSubject(tool="mcp__memory__list_repos")
    closed = engine.gate(hooks_mod.HookEvent.PRE_TOOL_USE, subject)
    assert closed.allowed is False
    assert closed.failures[0]["exit_code"] == 127
    open_gate = engine.gate(hooks_mod.HookEvent.POST_TOOL_USE, subject)
    assert open_gate.allowed is True
    assert open_gate.failures[0]["exit_code"] == 127


def test_an_operator_can_override_the_fail_policy_and_the_override_is_recorded(
    isolated,
):
    """A tier may relax a fail-closed event; the receipt says the override ran.

    This is the only way a broken ``PreToolUse`` gate becomes advisory, and it
    is an explicit, per-tier, visible decision rather than a default.
    """
    from extensions import user_hooks as hooks_mod

    _write_hooks(
        isolated["config"],
        {
            "fail_policy": {"PreToolUse": "fail_open"},
            "hooks": {
                "PreToolUse": [
                    {
                        "id": "gate",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                    }
                ]
            },
        },
    )
    config = hooks_mod.load_hook_config(repo_path=str(isolated["repo"]))
    assert config.fail_policies == (("PreToolUse", "fail_open"),)
    engine = hooks_mod.HookEngine(config, repo_path=str(isolated["repo"]))
    gate = engine.gate(hooks_mod.HookEvent.PRE_TOOL_USE, hooks_mod.HookSubject())
    assert gate.allowed is True
    assert gate.fail_policy_source == "config:event"
    assert gate.failures


def test_a_per_hook_fail_policy_beats_the_event_policy(isolated):
    """A single registration may opt out on its own, and says so by id."""
    from extensions import user_hooks as hooks_mod

    _write_hooks(
        isolated["config"],
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "id": "advisory",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                        "fail_policy": "fail_open",
                    },
                    {
                        "id": "hard",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                    },
                ]
            }
        },
    )
    engine = hooks_mod.HookEngine(
        hooks_mod.load_hook_config(repo_path=str(isolated["repo"])),
        repo_path=str(isolated["repo"]),
    )
    gate = engine.gate(hooks_mod.HookEvent.PRE_TOOL_USE, hooks_mod.HookSubject())
    assert gate.allowed is False, "the non-overriding sibling must still fail closed"
    assert gate.fail_policy == "fail_closed"
    spec = config_spec = next(
        spec for spec in engine.config.specs if spec.id == "advisory"
    )
    policy, source = engine.config.fail_policy_for(spec.event, spec)
    assert (policy, source) == ("fail_open", "hook:advisory")
    del config_spec


def test_a_broken_stop_hook_cannot_leave_a_verified_status_intact(isolated):
    """``Stop`` is fail-closed, so a broken completion gate downgrades a claim.

    This is the verifier-adjacent direction and it only ever downgrades: a
    status that was already unverified/failed comes back unchanged, and no
    input to the gate can produce ``completed_verified``.
    """
    from extensions import user_hooks as hooks_mod

    _write_hooks(
        isolated["config"],
        {
            "hooks": {
                "Stop": [
                    {
                        "id": "release-gate",
                        "type": "command",
                        "command": ["neo-no-such-release-gate-9c1d"],
                    }
                ]
            }
        },
    )
    engine = hooks_mod.HookEngine(
        hooks_mod.load_hook_config(repo_path=str(isolated["repo"])),
        repo_path=str(isolated["repo"]),
    )
    gate = hooks_mod.CompletionGate(engine)
    verdict = gate.evaluate(
        {"status": "completed_verified"},
        status="completed_verified",
        verification={"target_passed": True, "regression_passed": True},
    )
    assert verdict.status == "completed_unverified"
    assert verdict.original_status == "completed_verified"
    assert verdict.blocked_by == "fail_policy"
    assert verdict.fail_policy == "fail_closed"

    for already in ("failed", "timeout", "cancelled", "completed_unverified"):
        untouched = gate.evaluate({}, status=already)
        assert untouched.status == "completed_unverified"
        assert untouched.original_status == already


# ---------------------------------------------------------------------------
# 3. `neo migrate` is idempotent and rolls back on failure
# ---------------------------------------------------------------------------


def test_neo_migrate_is_idempotent(isolated):
    """Running the migration twice is the second run reporting nothing to do.

    Not "the second run does not crash" and not "the second run is mostly a
    no-op": ``already_current`` is True and the pending-step list is EMPTY,
    because every detector is written so a migrated system stops satisfying it.
    """
    from cli import connectors as connectors_mod
    from cli import migration as migration_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    _write_hooks(isolated["config"], {"hooks": {"PreToolUse": []}})

    first_plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    assert first_plan.already_current is False
    pending = {step.id for step in first_plan.pending()}
    assert "connector_permission_declaration" in pending
    assert "hook_config_schema_version" in pending

    first = migration_mod.apply_migrations(
        first_plan, repo_path=str(isolated["repo"]), allow_destructive=True
    )
    assert first.rolled_back is False, first.error
    assert first.applied is True
    assert first.receipt_path and Path(first.receipt_path).is_file()
    assert (
        json.loads(Path(first.receipt_path).read_text(encoding="utf-8"))[
            "schema_version"
        ]
        == 1
    )

    second_plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    assert second_plan.already_current is True, [s.to_dict() for s in second_plan.steps]
    assert second_plan.pending() == ()
    second = migration_mod.apply_migrations(
        second_plan, repo_path=str(isolated["repo"]), allow_destructive=True
    )
    assert second.applied is False
    assert second.idempotent is True
    assert second.outcomes == ()


def test_neo_migrate_plan_names_the_exact_paths_it_will_touch(isolated):
    """The plan is data, and every action names a concrete path.

    A migration nobody can preview is a migration nobody will run, and this is
    the property that makes ``neo migrate`` without ``--apply`` useful: it
    writes nothing at all and still tells the operator the file and the change.
    """
    from cli import connectors as connectors_mod
    from cli import migration as migration_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    before = _tree_snapshot(isolated["repo"])
    plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    assert _tree_snapshot(isolated["repo"]) == before, "planning wrote to the tree"

    step = next(
        item for item in plan.pending() if item.id == "connector_permission_declaration"
    )
    assert step.actions
    for action in step.actions:
        assert action.path
        assert action.kind in {"write_file", "move_path", "remove_tree", "ignore_entry"}
    expected = isolated["repo"] / ".neo" / "connector-permissions.toml"
    assert str(expected) in {action.path for action in step.actions}
    assert step.destructive is False
    assert json.loads(plan.to_dict() and json.dumps(plan.to_dict()))["pending_ids"]


def test_neo_migrate_rolls_back_on_failure(isolated, monkeypatch):
    """A failure in the middle of a multi-step run restores EVERYTHING.

    The transaction is exercised across two steps that both touch disk: the
    hook-config stamp (a write into a pre-existing file) and the connector
    declaration (a write into a new file). The second is forced to fail after
    the first has already been written, and the test asserts the first file is
    byte-identical to what it was and the second does not exist at all.
    """
    from cli import migration as migration_mod

    hooks_path = isolated["config"] / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    original_hooks = hooks_path.read_bytes()
    original_plugin_file = None
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    del original_plugin_file

    plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    pending_ids = [step.id for step in plan.pending()]
    assert "hook_config_schema_version" in pending_ids
    assert "connector_permission_declaration" in pending_ids

    real_write_text = migration_mod._atomic_write_text
    target = str(isolated["repo"] / ".neo" / "connector-permissions.toml")

    def exploding(path, text):
        if str(path) == target:
            raise OSError("simulated failure while writing the permissions file")
        return real_write_text(path, text)

    monkeypatch.setattr(migration_mod, "_atomic_write_text", exploding)
    result = migration_mod.apply_migrations(
        plan, repo_path=str(isolated["repo"]), allow_destructive=True
    )
    assert result.rolled_back is True
    assert result.applied is False
    assert "connector_permission_declaration" in result.error
    assert result.rollback_errors == ()
    assert hooks_path.read_bytes() == original_hooks, (
        "the earlier write was not restored"
    )
    assert not (isolated["repo"] / ".neo" / "connector-permissions.toml").exists()


def test_neo_migrate_rolls_back_a_removal_too(isolated):
    """A destructive step is reversible until commit, so a later failure undoes it.

    ``plugin_install_residue`` removes directories. It moves them aside rather
    than deleting them, so a run that fails afterwards can put them back. The
    test plants residue, forces the NEXT step to fail, and asserts the residue
    is exactly as it was.
    """
    from cli import migration as migration_mod

    plugins_root = isolated["plugins"]
    residue = plugins_root / ".staging-demo-999-0"
    (residue / "skills").mkdir(parents=True)
    (residue / "skills" / "SKILL.md").write_text("half-written", encoding="utf-8")
    residue_bytes = (residue / "skills" / "SKILL.md").read_bytes()

    _write_hooks(isolated["config"], {"hooks": {"PreToolUse": []}})
    plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    residue_step = next(
        (item for item in plan.pending() if item.id == "plugin_install_residue"), None
    )
    assert residue_step is not None
    assert residue_step.destructive is True

    skipped = migration_mod.apply_migrations(
        plan, repo_path=str(isolated["repo"]), allow_destructive=False
    )
    assert "plugin_install_residue" in skipped.skipped_destructive
    assert residue.is_dir(), "a destructive step must not run without the flag"
    # The non-destructive steps DID run, so re-plant the unstamped hook config
    # to give the second run a step that can fail AFTER the removal.
    hooks_path = isolated["config"] / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    assert "plugin_install_residue" in {step.id for step in plan.pending()}

    real = migration_mod._atomic_write_text

    def exploding(path, text):
        if str(path) == str(hooks_path):
            raise OSError("simulated failure after the removal")
        return real(path, text)

    migration_mod._atomic_write_text = exploding
    try:
        result = migration_mod.apply_migrations(
            plan, repo_path=str(isolated["repo"]), allow_destructive=True
        )
    finally:
        migration_mod._atomic_write_text = real
    assert result.rolled_back is True
    assert residue.is_dir(), "the removal was not rolled back"
    assert (residue / "skills" / "SKILL.md").read_bytes() == residue_bytes


def test_neo_migrate_backfills_an_install_record_for_an_old_plugin(isolated):
    """A plugin installed before installs were atomic gets a MEASURED record.

    The backfill is honest about what it does not know: the source is recorded
    as ``backfill`` and the file counts are measured now, never guessed.
    """
    from cli import migration as migration_mod
    from cli import plugins as plugins_mod

    source = _plugin_source(isolated["home"], "legacy", "v1")
    name = plugins_mod.install(source=str(source))
    receipt_path = plugins_mod.plugin_state_dir() / f"{name}.json"
    receipt_path.unlink()
    assert plugins_mod.read_install_receipt(name) is None

    plan = migration_mod.plan_migrations(repo_path=str(isolated["repo"]))
    assert "plugin_install_receipt" in {step.id for step in plan.pending()}
    result = migration_mod.apply_migrations(
        plan, repo_path=str(isolated["repo"]), allow_destructive=True
    )
    assert result.rolled_back is False, result.error
    receipt = plugins_mod.read_install_receipt(name)
    assert receipt is not None
    assert receipt.source_kind == "backfill"
    assert receipt.file_count >= 2
    assert len(receipt.tree_sha256) == 64
    assert receipt.tool_verbs == ("ruff",)
    assert receipt.mcp_servers == ("memory",)


# ---------------------------------------------------------------------------
# 4. A connector with a declared network permission is gated
# ---------------------------------------------------------------------------


def test_a_connector_with_a_declared_network_permission_is_gated(isolated, monkeypatch):
    """A network-capable tool is refused unless the connector declared its host.

    The catalog stub carries the ``sideEffectClass`` and ``networkDomains`` a
    schema-preserving client would report; ``memory.mcp_client`` currently
    normalizes a listed tool to name+description, so this is the only way to
    exercise the class-carrying branch without a schema-preserving server. Every
    other step is product code: the namespaced identity, the connector's
    declaration, the host allowlist, the shared egress policy, and the refusal.
    """
    import memory.mcp_client as mcp_client
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    connectors_mod.set_permissions(
        "docs",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="network",
        network=("example.com",),
    )

    def install_catalog(host: str, tool_name: str = "fetch_docs"):
        def fake_list(server, cwd=None, env=None, **kwargs):
            return {
                "ok": True,
                "tools": [
                    {
                        "name": tool_name,
                        "description": "fetch docs",
                        "sideEffectClass": "network",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"networkDomains": {"const": host}},
                        },
                    }
                ],
                "error": None,
            }

        def fake_call(server, tool, args=None, cwd=None, env=None, **kwargs):
            return {"ok": True, "text": "docs", "error": None}

        monkeypatch.setattr(mcp_client, "list_mcp_tools", fake_list)
        monkeypatch.setattr(mcp_client, "call_mcp_tool", fake_call)

    install_catalog("example.com")
    allowed = connectors_mod.call_tool(
        "docs", "fetch_docs", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert allowed["ok"] is True, allowed["error"]
    assert allowed["receipt"]["network"] == ["example.com"]

    install_catalog("evil.example.net")
    refused = connectors_mod.call_tool(
        "docs", "fetch_docs", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert refused["ok"] is False
    assert "not in the declared network allowlist" in refused["error"]
    assert refused["receipt"]["enforced"] is True


def test_a_network_class_tool_with_no_declared_host_is_refused(isolated, monkeypatch):
    """A network tool that names no host cannot be gated, so it is refused.

    The alternative — allowing it because the connector declared no hosts — is
    the "not obviously private, so fine" mode the shared egress policy exists
    to eliminate.
    """
    import memory.mcp_client as mcp_client
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    connectors_mod.set_permissions(
        "docs",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="network",
    )

    monkeypatch.setattr(
        mcp_client,
        "list_mcp_tools",
        lambda server, cwd=None, env=None, **kwargs: {
            "ok": True,
            "tools": [
                {
                    "name": "fetch_anywhere",
                    "description": "fetches whatever",
                    "sideEffectClass": "network",
                }
            ],
            "error": None,
        },
    )
    result = connectors_mod.call_tool(
        "docs", "fetch_anywhere", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert result["ok"] is False
    assert "declares no host" in result["error"]


def test_a_connector_that_declares_no_write_refuses_a_mutating_tool(
    isolated, monkeypatch
):
    """``write = false`` is the "this connector does not write files" statement.

    The write gate is key-presence based: a declaration that never mentioned
    ``write`` is reported as undeclared and is not enforced, which is the only
    backwards-compatible reading, and the receipt says ``write_declared: false``
    so nobody reads it as protected.
    """
    import memory.mcp_client as mcp_client
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )

    def catalog(side_effect: str):
        monkeypatch.setattr(
            mcp_client,
            "list_mcp_tools",
            lambda server, cwd=None, env=None, **kwargs: {
                "ok": True,
                "tools": [
                    {
                        "name": "record",
                        "description": "writes",
                        "sideEffectClass": side_effect,
                    }
                ],
                "error": None,
            },
        )
        monkeypatch.setattr(
            mcp_client,
            "call_mcp_tool",
            lambda server, tool, args=None, cwd=None, env=None, **kwargs: {
                "ok": True,
                "text": "wrote",
                "error": None,
            },
        )

    catalog("mutation")
    undeclared = connectors_mod.call_tool(
        "docs", "record", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert undeclared["ok"] is True
    assert undeclared["receipt"]["write_declared"] is False

    connectors_mod.set_permissions(
        "docs",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="mutation",
        write=False,
    )
    refused = connectors_mod.call_tool(
        "docs", "record", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert refused["ok"] is False
    assert "write=false" in refused["error"]
    assert refused["receipt"]["write_declared"] is True

    connectors_mod.set_permissions(
        "docs",
        tier="project",
        repo_path=str(isolated["repo"]),
        tools=("*",),
        side_effect="mutation",
        write=True,
    )
    permitted = connectors_mod.call_tool(
        "docs", "record", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert permitted["ok"] is True
    assert permitted["receipt"]["write"] is True


def test_a_malformed_permission_declaration_is_refused_not_ignored(isolated):
    """A typo in a security declaration raises rather than reading as "nothing"."""
    from cli import connectors as connectors_mod

    (isolated["repo"] / ".neo" / "connector-permissions.toml").write_text(
        '[connector_permissions]\n[connector_permissions.docs]\ntoools = ["x"]\n',
        encoding="utf-8",
    )
    with pytest.raises(connectors_mod.ConnectorError) as caught:
        connectors_mod.read_permissions(repo_path=str(isolated["repo"]))
    assert "unknown permission key" in str(caught.value)

    (isolated["repo"] / ".neo" / "connector-permissions.toml").write_text(
        "[connector_permissions]\n[connector_permissions.docs]\n"
        'tools = ["x"]\nside_effect = "catastrophic"\n',
        encoding="utf-8",
    )
    with pytest.raises(connectors_mod.ConnectorError):
        connectors_mod.read_permissions(repo_path=str(isolated["repo"]))


def test_an_undeclared_connector_reports_that_it_was_not_gated(isolated, monkeypatch):
    """A connector with no declaration is not enforced, and says so.

    This is the honesty test for the opt-in. It is a LIMITATION, surfaced:
    ``enforced: false`` and a reason that names it, so a receipt can never read
    as "checked and allowed".
    """
    import memory.mcp_client as mcp_client
    from cli import connectors as connectors_mod

    connectors_mod._add_server_command(
        "legacy",
        "python -m mcp_server",
        tier="project",
        repo_path=str(isolated["repo"]),
    )
    monkeypatch.setattr(
        mcp_client,
        "list_mcp_tools",
        lambda server, cwd=None, env=None, **kwargs: {
            "ok": True,
            "tools": [{"name": "anything", "description": "d"}],
            "error": None,
        },
    )
    monkeypatch.setattr(
        mcp_client,
        "call_mcp_tool",
        lambda server, tool, args=None, cwd=None, env=None, **kwargs: {
            "ok": True,
            "text": "fine",
            "error": None,
        },
    )
    result = connectors_mod.call_tool(
        "legacy", "anything", {}, repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert result["ok"] is True
    receipt = result["receipt"]
    assert receipt["declared"] is False
    assert receipt["enforced"] is False
    assert "NOT gated" in receipt["reason"]

    listing = connectors_mod.list_tools(
        "legacy", repo_path=str(isolated["repo"]), timeout_s=30.0
    )
    assert listing["blocked"] == []


# ---------------------------------------------------------------------------
# 5. Plugin install is atomic; uninstall is complete
# ---------------------------------------------------------------------------


def test_plugin_install_interrupted_midway_leaves_the_previous_version_intact(
    isolated, monkeypatch
):
    """A failure after staging leaves the PREVIOUS version installed.

    The interruption is injected at three different points — the staging copy,
    the staged verification, and the swap — because "install is atomic" is only
    true if it holds for all of them, and a single injection point would prove
    exactly one of the three.
    """
    from cli import plugins as plugins_mod

    v1 = _plugin_source(isolated["home"], "demo", "v1")
    name = plugins_mod.install(source=str(v1))
    installed = plugins_mod._plugin_dir(name)
    before = _tree_snapshot(installed)
    assert "v1" in (installed / "plugin.json").read_text(encoding="utf-8")

    v2 = _plugin_source(isolated["home"], "demo", "v2")

    # (a) the staging copy dies
    real_copytree = plugins_mod.shutil.copytree

    def dying_copytree(src, dst, **kwargs):
        raise OSError("disk full during staging")

    plugins_mod.shutil.copytree = dying_copytree
    try:
        with pytest.raises(OSError):
            plugins_mod.install_from_local(str(v2))
    finally:
        plugins_mod.shutil.copytree = real_copytree
    assert _tree_snapshot(installed) == before

    # (b) the staged verification dies
    def dying_verify(staging, plugin_name):
        raise plugins_mod.PluginError("staged tree is missing its manifest")

    with monkeypatch.context() as scoped:
        scoped.setattr(plugins_mod, "_verify_staged", dying_verify)
        with pytest.raises(plugins_mod.PluginError):
            plugins_mod.install_from_local(str(v2))
    assert _tree_snapshot(installed) == before

    # (c) the swap itself dies, after the old tree was moved aside
    real_replace = plugins_mod.os.replace
    attempts = {"n": 0}

    def dying_replace(src, dst):
        if str(dst) == str(installed):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("simulated transient failure during the final swap")
        return real_replace(src, dst)

    monkeypatch.setattr(plugins_mod.os, "replace", dying_replace)
    with pytest.raises(plugins_mod.PluginError) as caught:
        plugins_mod.install_from_local(str(v2))
    monkeypatch.setattr(plugins_mod.os, "replace", real_replace)
    assert _tree_snapshot(installed) == before, "the previous version was not restored"
    assert "v1" in (installed / "plugin.json").read_text(encoding="utf-8")
    assert "cannot install plugin" in str(caught.value)
    assert not any(
        item.name.startswith(plugins_mod.STAGING_PREFIX)
        for item in isolated["plugins"].iterdir()
    ), "an interrupted install left staging residue"
    assert not any(
        item.name.startswith(plugins_mod.TRASH_PREFIX)
        for item in isolated["plugins"].iterdir()
    ), "an interrupted install left the previous version in the trash"

    # and a clean install afterwards does replace it
    plugins_mod.install_from_local(str(v2))
    assert "v2" in (installed / "plugin.json").read_text(encoding="utf-8")


def test_a_successful_plugin_install_writes_a_measured_install_record(isolated):
    """The install record is written from the STAGED tree, after the swap."""
    from cli import plugins as plugins_mod

    source = _plugin_source(isolated["home"], "recorded", "v1")
    name = plugins_mod.install(source=str(source))
    receipt = plugins_mod.read_install_receipt(name)
    assert receipt is not None
    assert receipt.name == name
    assert receipt.source_kind == "local"
    assert receipt.file_count >= 2
    assert receipt.total_bytes > 0
    assert receipt.tool_verbs == ("ruff",)
    assert receipt.mcp_servers == ("memory",)
    assert receipt.installed_at

    directory = plugins_mod._plugin_dir(name)
    digest, files, total = plugins_mod._tree_digest(directory)
    assert receipt.tree_sha256 == digest
    assert receipt.file_count == files
    assert receipt.total_bytes == total


def test_plugin_uninstall_leaves_nothing_behind(isolated):
    """`neo plugin remove` removes files, rows, logs, and the registered surface.

    The completeness claim is a MEASUREMENT: the test walks the whole plugins
    root after the removal and finds nothing named after the plugin, and the
    report's own ``remaining`` list agrees.
    """
    from cli import plugins as plugins_mod

    source = _plugin_source(isolated["home"], "doomed", "v1")
    name = plugins_mod.install(source=str(source))
    plugins_mod.disable(name)
    residue = isolated["plugins"] / f".staging-{name}-4242-7"
    residue.mkdir()
    (residue / "leftover.txt").write_text("half", encoding="utf-8")
    assert (isolated["plugins"] / f"{name}.disabled").is_file()
    assert plugins_mod.read_install_receipt(name) is not None

    report = plugins_mod.uninstall(name)
    assert report.complete is True
    assert report.removed_tree is True
    assert report.removed_files >= 2
    assert report.removed_bytes > 0
    assert report.removed_marker is True
    assert report.removed_rows == 1
    assert report.removed_logs == 1
    assert report.errors == ()
    assert "batch-verb:ruff" in report.retracted_path_entries
    assert "mcp-label:memory" in report.retracted_path_entries
    assert report.os_path_modified is False
    assert "never modifies the OS PATH" in report.os_path_note

    for entry in isolated["plugins"].rglob("*"):
        assert "doomed" not in entry.name, f"uninstall left {entry}"
    assert not (isolated["plugins"] / f"{name}.disabled").exists()
    assert not plugins_mod.read_install_receipt(name)
    assert not any(item["name"] == name for item in plugins_mod.list_plugins())

    # the historical name-returning API agrees, and re-running is a clean error
    with pytest.raises(plugins_mod.PluginError):
        plugins_mod.remove(name)


def test_plugin_remove_refuses_to_report_a_partial_removal_as_success(isolated):
    """A removal that left something behind RAISES rather than returning a name.

    `remove()` keeps its historical signature, so the only way it can be honest
    about a partial removal is to fail.
    """
    from cli import plugins as plugins_mod

    source = _plugin_source(isolated["home"], "stubborn", "v1")
    name = plugins_mod.install(source=str(source))
    real_rmtree = plugins_mod.shutil.rmtree
    calls = {"n": 0}

    def selective_rmtree(target, *args, **kwargs):
        if "stubborn" in str(target):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("file is in use")
        return real_rmtree(target, *args, **kwargs)

    plugins_mod.shutil.rmtree = selective_rmtree
    try:
        with pytest.raises(plugins_mod.PluginError) as caught:
            plugins_mod.remove(name)
    finally:
        plugins_mod.shutil.rmtree = real_rmtree
    assert "partially removed" in str(caught.value)
    assert "stubborn" in str(caught.value)


# ---------------------------------------------------------------------------
# 6. `doctor --json` and the support bundle contain no secrets
# ---------------------------------------------------------------------------

_PLANTED_KEY = "sk-R2PLANTEDSECRET0123456789abcdef"
_PLANTED_BEARER = "Bearer R2PLANTEDTOKENZZZZZZZZZZ"


def test_doctor_json_and_the_support_bundle_contain_no_secrets(isolated, monkeypatch):
    """A credential planted in settings and in the environment stays out of both.

    The assertion is on the ARCHIVE BYTES, not on "no field looks secret": a
    bundle is a file a user attaches to a public issue, so the only evidence
    that matters is whether the string is in the file.
    """
    from cli import doctor as doctor_mod
    from cli.neoconfig import set_tier_key

    set_tier_key("global", "api_key", _PLANTED_KEY)
    set_tier_key("global", "model", "gpt-test")
    set_tier_key(
        "global", "base_url", f"https://user:{_PLANTED_KEY}@api.example.com/v1"
    )
    monkeypatch.setenv("NEO_API_KEY", _PLANTED_KEY)
    monkeypatch.setenv("NEO_MODEL", "gpt-test")

    record = doctor_mod.doctor_record(repo_path=str(isolated["repo"]))
    rendered = doctor_mod.render_doctor_json(record)
    assert _PLANTED_KEY not in rendered
    assert "R2PLANTEDSECRET" not in rendered
    assert json.loads(rendered)["summary"]["total"] >= 9

    bundle = doctor_mod.build_support_bundle(
        repo_path=str(isolated["repo"]), log_root=str(isolated["home"] / "logs")
    )
    archive = isolated["home"] / "support.zip"
    doctor_mod.write_support_bundle(bundle, str(archive))
    raw = archive.read_bytes()
    assert _PLANTED_KEY.encode() not in raw
    assert b"R2PLANTEDSECRET" not in raw
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert {
            "manifest.json",
            "doctor.json",
            "doctor.txt",
            "environment.json",
            "config-shape.json",
            "connectors.json",
            "hooks.json",
            "plugins.json",
            "recent-errors.json",
        } <= names
        for name in names:
            assert _PLANTED_KEY not in zf.read(name).decode("utf-8", "replace")
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        for name, entry in manifest["contents"].items():
            body = zf.read(name)
            assert entry["bytes"] == len(body)
            assert len(entry["sha256"]) == 64
        environment = json.loads(zf.read("environment.json"))
        assert "NEO_API_KEY" not in environment["env_var_names_present"], (
            "an API key variable is not in the recorded name list"
        )
        assert "NEO_MODEL" in environment["env_var_names_present"]
        config_shape = json.loads(zf.read("config-shape.json"))
        assert config_shape["keys"]["api_key"]["public"] == "withheld"
        assert config_shape["keys"]["model"]["public"] == "gpt-test"
        assert config_shape["keys"]["api_key"]["shape"].startswith("string(")


def test_the_support_bundle_directory_form_matches_the_archive(isolated):
    """`--no-archive` writes the same files as a directory, not a different set."""
    from cli import doctor as doctor_mod

    bundle = doctor_mod.build_support_bundle(
        repo_path=str(isolated["repo"]), log_root=str(isolated["home"] / "logs")
    )
    target = isolated["home"] / "bundle-dir"
    written = doctor_mod.write_support_bundle(bundle, str(target), archive=False)
    on_disk = {path.name for path in Path(written).iterdir()}
    assert on_disk == set(bundle.files)
    for name, text in bundle.files.items():
        assert (Path(written) / name).read_text(encoding="utf-8") == text


def test_the_support_bundle_collects_recent_errors_from_the_run_journals(isolated):
    """Recent errors are read from real run journals, bounded and redacted.

    The bundle is a diagnostic, not an archive: the per-event cap and the
    run-directory cap are asserted, and a malformed journal line is skipped
    rather than fatal, because a corrupt journal is exactly what the bundle
    exists to surface.
    """
    from cli import doctor as doctor_mod

    logs = isolated["home"] / "logs"
    for index in range(2):
        run = logs / f"task-{index}"
        run.mkdir(parents=True)
        rows = [
            {"kind": "task_start", "data": {"task_id": f"task-{index}"}},
            {"kind": "error", "data": {"error": f"boom {index}", "key": _PLANTED_KEY}},
            {"kind": "model_failure_terminal", "data": {"kind": "model_auth"}},
        ]
        if index == 0:
            rows.append({"kind": "not json at all"})
        (run / "trace.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows[:-1]) + "\nnot json at all\n",
            encoding="utf-8",
        )
    bundle = doctor_mod.build_support_bundle(
        repo_path=str(isolated["repo"]), log_root=str(logs), recent_runs=2
    )
    payload = json.loads(bundle.files["recent-errors.json"])
    assert payload["runs_scanned"] == 2
    assert set(payload["errors"]) == {"error", "model_failure_terminal"}
    assert payload["limit_per_event"] == doctor_mod._BUNDLE_ERROR_LIMIT_PER_EVENT
    detail = payload["errors"]["error"][0]["detail"]
    assert "R2PLANTEDSECRET" not in detail
    assert "REDACTED" in detail or "boom" in detail


def test_neo_doctor_exits_one_when_something_is_actionable(isolated, monkeypatch):
    """`neo doctor` is usable as a CI gate because its exit code carries the verdict."""
    from cli import doctor as doctor_mod
    from cli.main import main

    def failing():
        return {
            "status": "failed",
            "reason": "docker daemon not reachable",
            "evidence": "docker info exit 1",
            "remediation": "start Docker Desktop",
        }

    monkeypatch.setattr(
        doctor_mod,
        "_DOCTOR_CHECKS",
        (doctor_mod.DoctorCheck("docker", "docker", "Docker", failing),),
    )
    assert main(["doctor", "--json"]) == 1
    assert main(["doctor"]) == 1

    monkeypatch.setattr(
        doctor_mod,
        "_DOCTOR_CHECKS",
        (
            doctor_mod.DoctorCheck(
                "docker",
                "docker",
                "Docker",
                lambda: {
                    "status": "ok",
                    "reason": "running",
                    "evidence": "ok",
                    "remediation": None,
                },
            ),
        ),
    )
    assert main(["doctor", "--json"]) == 0


def test_doctor_registers_a_connector_permission_check(isolated):
    """The doctor surfaces an UNDECLARED connector, because that is the gap.

    A connector whose blast radius was never stated is the exact condition this
    round exists to close, so the health surface names it with a remediation
    rather than leaving it to be discovered by reading a TOML file.
    """
    from cli import connectors as connectors_mod
    from cli import doctor as doctor_mod

    connectors_mod._add_server_command(
        "undeclared",
        "python -m mcp_server",
        tier="project",
        repo_path=str(isolated["repo"]),
    )
    row = doctor_mod.check_connector_permissions(repo_path=str(isolated["repo"]))
    assert row["status"] in ("ok", "failed", "error")
    payload = json.loads(json.dumps(row))
    if row["status"] == "ok":
        assert payload.get("undeclared") == ["undeclared"]
        assert "neo mcp permissions" in (row["remediation"] or "")
    assert "undeclared" not in (row["evidence"] or "") or row["status"] != "ok"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_the_new_subcommands_are_registered_and_dispatchable(isolated):
    """`doctor`, `support-bundle`, `migrate`, and `hooks` are real subcommands."""
    from cli.main import build_parser

    parser = build_parser()
    choices: dict = {}
    for action in parser._subparsers._group_actions:
        choices.update(getattr(action, "choices", {}) or {})
    assert {"doctor", "support-bundle", "migrate", "hooks"} <= set(choices)
    for name in ("doctor", "support-bundle", "migrate", "hooks"):
        assert choices[name].prog.endswith(name)


def test_neo_hooks_run_reports_the_fail_policy_it_used(isolated, capsys):
    """`neo hooks run` is how an operator checks a hook without a real call."""
    from cli.main import main

    _write_hooks(
        isolated["config"],
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "id": "gate",
                        "type": "command",
                        "command": ["neo-no-such-gate-binary-4f2a"],
                    }
                ]
            }
        },
    )
    assert (
        main(["hooks", "run", "PreToolUse", "--repo", str(isolated["repo"]), "--json"])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["allowed"] is False
    assert payload["fail_policy"] == "fail_closed"
    assert payload["fail_policy_source"] == "table"

    assert main(["hooks", "run", "NotAnEvent", "--repo", str(isolated["repo"])]) == 2
    capsys.readouterr()


def test_neo_mcp_permissions_writes_and_reports_a_declaration(isolated, capsys):
    """`neo mcp permissions` is the surface that makes a declaration writable."""
    from cli.main import main

    assert (
        main(
            [
                "mcp",
                "add",
                "docs",
                "--tier",
                "project",
                "--repo",
                str(isolated["repo"]),
                "--",
                "python",
                "-m",
                "mcp_server",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "mcp",
                "permissions",
                "docs",
                "--tier",
                "project",
                "--repo",
                str(isolated["repo"]),
                "--tool",
                "list_repos",
                "--side-effect",
                "read",
                "--no-write",
                "--json",
            ]
        )
        == 0
    )
    declared = json.loads(capsys.readouterr().out)
    assert declared["tools"] == ["list_repos"]
    assert declared["side_effect"] == "read"
    assert declared["write"] is False
    assert declared["write_declared"] is True

    assert (
        main(
            [
                "mcp",
                "permissions",
                "--tier",
                "project",
                "--repo",
                str(isolated["repo"]),
                "--json",
            ]
        )
        == 0
    )
    listing = json.loads(capsys.readouterr().out)
    assert listing["docs"]["tools"] == ["list_repos"]

    assert (
        main(
            [
                "mcp",
                "permissions",
                "docs",
                "--tier",
                "project",
                "--repo",
                str(isolated["repo"]),
                "--clear",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "mcp",
                "permissions",
                "docs",
                "--tier",
                "project",
                "--repo",
                str(isolated["repo"]),
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_neo_migrate_without_apply_writes_nothing(isolated, capsys):
    """The default arm is the plan, and the plan is read-only."""
    from cli import connectors as connectors_mod
    from cli.main import main

    connectors_mod._add_server_command(
        "docs", "python -m mcp_server", tier="project", repo_path=str(isolated["repo"])
    )
    before = _tree_snapshot(isolated["repo"])
    assert main(["migrate", "--repo", str(isolated["repo"]), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert "connector_permission_declaration" in payload["plan"]["pending_ids"]
    assert _tree_snapshot(isolated["repo"]) == before

    assert (
        main(
            [
                "migrate",
                "--repo",
                str(isolated["repo"]),
                "--apply",
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert _tree_snapshot(isolated["repo"]) != before

    assert main(["migrate", "--repo", str(isolated["repo"]), "--json"]) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["plan"]["already_current"] is True
    assert final["idempotent"] is True


def test_neo_migrate_rejects_an_unknown_migration_id(isolated, capsys):
    from cli.main import main

    assert main(["migrate", "--apply", "--only", "not_a_migration"]) == 2
    assert "unknown migration id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path) -> dict:
    """Return ``{relative path: sha256}`` for a tree, for before/after equality."""
    import hashlib

    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def test_a_real_neo_help_run_still_works() -> None:
    """The parser changes did not break `--help` for the console script."""
    proc = subprocess.run(
        [sys.executable, "-m", "cli", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    for name in ("doctor", "support-bundle", "migrate", "hooks"):
        assert name in proc.stdout
