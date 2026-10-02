"""Terminal 07 command, event, state, and global-isolation parity tests.

Round 2 (2026-09-29) added the ONE-command-system classes at the bottom.
Round 1 pinned the nine-state matrix; round 2 pins the ENVELOPE — that the
three surfaces publish the same record, the same event rows, and the same
exit codes, and that no surface owns a state the others cannot see.
"""

from __future__ import annotations

import ast
import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from cli import command_exec, interactive, runview, ui
from cli import commands as commands_mod
from cli.exit_codes import EXIT_CODES


@pytest.fixture
def anyio_backend() -> str:
    """Use Textual's event loop for the mounted global-state test."""
    return "asyncio"


def _write_events(log_root: Path, task_id: str, events: list[dict[str, Any]]) -> None:
    task_dir = log_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    task_dir.joinpath("trace.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _state_events(state: str) -> list[dict[str, Any]]:
    evidence = {
        "target_passed": True,
        "regression_passed": True,
        "flaky": False,
        "summary": "clean verifier receipt",
    }
    start = {
        "kind": "task_start",
        "data": {
            "issue_text": f"matrix {state}",
            "repo_path": ".",
            "resume": state == "resumed",
        },
    }
    if state == "running":
        return [start]
    if state == "waiting_for_approval":
        return [start, {"kind": "approval_required", "data": {"tool": "edit"}}]
    if state == "cancelled":
        return [
            start,
            {"kind": "cancellation_requested", "data": {}},
            {"kind": "result", "data": {"status": "cancelled"}},
        ]
    if state == "failed":
        return [start, {"kind": "result", "data": {"status": "failed"}}]
    if state == "completed_verified":
        return [
            start,
            {"kind": "final_verify", "data": evidence},
            {
                "kind": "result",
                "data": {
                    "status": "completed_verified",
                    "verification_evidence": evidence,
                },
            },
        ]
    if state == "completed_unverified":
        return [start, {"kind": "result", "data": {"status": "completed"}}]
    if state == "blocked":
        return [start, {"kind": "result", "data": {"status": "blocked"}}]
    if state == "resumed":
        return [start, {"kind": "run_resumed", "data": {"resume": True}}]
    return []


def _matrix_projection(log_root: Path, state: str) -> dict[str, Any]:
    if state == "idle":
        return runview.read_live_projection(log_root / "missing")
    task_id = f"agent-matrix-{state}"
    _write_events(log_root, task_id, _state_events(state))
    return runview.read_live_projection(log_root / task_id)


def _make_app(tmp_path: Path) -> Any:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    from cli.tui import NeoApp

    return NeoApp(
        repo=repo,
        log_root=tmp_path / "logs",
        state={"repo": str(repo), "file_config": {}},
        file_config={},
    )


@pytest.mark.parametrize("state", commands_mod.SURFACE_STATES)
def test_nine_state_matrix_normalizes_identically_on_every_surface(
    tmp_path: Path, state: str
) -> None:
    snapshot = _matrix_projection(tmp_path / "logs", state)
    task_id = str(snapshot.get("task_id") or "")
    waiting = state == "waiting_for_approval"
    active = state in {"running", "waiting_for_approval", "resumed"}
    contexts = [
        commands_mod.surface_command_context(
            surface,
            in_flight=active,
            snapshot=snapshot,
            task_id=task_id,
            pending_approval=waiting,
        )
        for surface in ("repl", "tui", "headless")
    ]
    context_facts = {
        (
            context.in_flight,
            context.waiting_for_approval,
            context.has_task,
            context.pending_approval,
            context.denied_permissions,
        )
        for context in contexts
    }
    assert len(context_facts) == 1
    assert contexts[0].in_flight is active
    assert contexts[0].waiting_for_approval is waiting
    assert (
        commands_mod.normalize_terminal_state(
            snapshot,
            in_flight=active,
            waiting_for_approval=waiting,
            has_task=bool(task_id),
        )
        == state
    )


@pytest.mark.parametrize("state", commands_mod.SURFACE_STATES)
def test_command_matrix_uses_journal_state_across_tui_repl_and_headless(
    tmp_path: Path, state: str
) -> None:
    log_root = tmp_path / "logs"
    snapshot = _matrix_projection(log_root, state)
    task_id = str(snapshot.get("task_id") or "")

    app = _make_app(tmp_path)
    if task_id:
        app.last["task_id"] = task_id
    app._slash_command("/status", "/status")

    repl_last = {"task_id": task_id} if task_id else {}
    repl_state = {
        "repo": str(tmp_path / "repo"),
        "file_config": {},
        "approval_policy": commands_mod.ApprovalPolicy(),
    }
    interactive._slash_command("/status", "/status", repl_last, log_root, repl_state)

    headless_state: dict[str, Any] = {"repo": str(tmp_path / "repo")}
    if task_id:
        headless_state["active_task_id"] = task_id
    result = command_exec.run_command_line(
        "/status", log_root=log_root, repo=tmp_path / "repo", state=headless_state
    )

    assert app.last_command["state_after"] == state
    assert repl_state["last_command"]["state_after"] == state
    assert result.state_after == state
    assert {event["event"] for event in result.events} >= {
        "command_started",
        "command_finished",
    }
    assert all(
        event["surface"] == "headless" and event["state"] in commands_mod.SURFACE_STATES
        for event in result.events
    )


def test_main_headless_json_accepts_options_after_command(
    tmp_path: Path, capsys
) -> None:
    from cli import main as main_module

    exit_code = main_module.main(
        ["run", "/help", "--json", "--log-root", str(tmp_path / "logs")]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["state"] == "idle"
    assert [event["event"] for event in payload["events"]] == [
        "command_started",
        "command_finished",
    ]


def test_repl_result_presentation_is_fail_closed() -> None:
    unverified = interactive._terminal_result_display("success", None)
    verified = interactive._terminal_result_display(
        "success",
        {"target_passed": True, "regression_passed": True, "flaky": False},
    )
    assert unverified[0] == "COMPLETED · UNVERIFIED"
    assert unverified[1] == "neo.warn"
    assert verified[0] == "SUCCESS · VERIFIED"
    assert verified[1] == "neo.ok"


def test_success_without_clean_verifier_evidence_stays_unverified() -> None:
    snapshot = {"task_id": "task-1", "status": "success"}
    assert (
        commands_mod.normalize_terminal_state(snapshot, has_task=True)
        == "completed_unverified"
    )
    evidence = {
        "target_passed": True,
        "regression_passed": True,
        "flaky": False,
    }
    snapshot["verification_evidence"] = [evidence]
    assert (
        commands_mod.normalize_terminal_state(snapshot, has_task=True)
        == "completed_verified"
    )


def test_tui_repl_and_headless_refusals_have_equivalent_meaning(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._slash_command("/quiet loud", "/quiet loud")
    repl_state = {"repo": str(tmp_path / "repo"), "file_config": {}}
    interactive._slash_command(
        "/quiet loud", "/quiet loud", {}, tmp_path / "logs", repl_state
    )
    result = command_exec.run_command_line(
        "/quiet loud", log_root=tmp_path / "logs", repo=tmp_path / "repo"
    )

    assert app.last_command["status"] == "invalid"
    assert repl_state["last_command"]["status"] == "invalid"
    assert result.status == "invalid"
    assert app.last_command["exit_code"] == 2
    assert repl_state["last_command"]["exit_code"] == 2
    assert result.exit_code == 2
    assert app.last_command["state_after"] == "idle"
    assert repl_state["last_command"]["state_after"] == "idle"
    assert result.state_after == "idle"


def test_mutating_argument_forms_are_refused_during_live_run(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    task_id = "agent-live"
    _write_events(log_root, task_id, _state_events("running"))
    interactive._set_live_run(task_id, log_root)
    try:
        for line in (
            "/diff undo",
            "/checkpoints restore cp-1",
            "/settings model other",
            "/mcp memory",
            "/review the active change",
            "/mode build",
        ):
            app = _make_app(tmp_path)
            app.last["task_id"] = task_id
            app._slash_command(line, line.lower())
            repl_state = {
                "repo": str(tmp_path / "repo"),
                "file_config": {},
                "approval_policy": commands_mod.ApprovalPolicy(),
            }
            interactive._slash_command(
                line, line.lower(), {"task_id": task_id}, log_root, repl_state
            )
            result = command_exec.run_command_line(
                line,
                log_root=log_root,
                repo=tmp_path / "repo",
                state={"active_task_id": task_id},
            )
            assert app.last_command["status"] == "disabled", line
            assert repl_state["last_command"]["status"] == "disabled", line
            assert result.status == "disabled", line
            assert app.last_command["exit_code"] == 2
            assert repl_state["last_command"]["exit_code"] == 2
            assert result.exit_code == 2
    finally:
        interactive._clear_live_run()


def test_journal_activity_refuses_mutations_without_a_local_live_run(
    tmp_path: Path,
) -> None:
    log_root = tmp_path / "logs"
    task_id = "agent-cross-process"
    _write_events(log_root, task_id, _state_events("running"))
    result = command_exec.run_command_line(
        "/diff undo",
        log_root=log_root,
        repo=tmp_path,
        state={"active_task_id": task_id},
    )
    assert result.status == "disabled"
    assert result.exit_code == EXIT_CODES["usage_error"]


def test_journal_resume_receipt_keeps_the_task_active(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    task_id = "agent-cross-resume"
    _write_events(log_root, task_id, _state_events("resumed"))
    context = commands_mod.surface_command_context(
        "headless",
        in_flight=False,
        snapshot=runview.read_live_projection(log_root / task_id),
        task_id=task_id,
    )
    assert context.in_flight is True
    assert (
        commands_mod.normalize_terminal_state(
            runview.read_live_projection(log_root / task_id)
        )
        == "resumed"
    )


def _write_approval(log_root: Path, task_id: str, path: str) -> None:
    gate = log_root / f"{task_id}.runtime" / "approval"
    gate.mkdir(parents=True, exist_ok=True)
    gate.joinpath("request.json").write_text(
        json.dumps(
            {
                "request_id": f"request-{task_id}",
                "fingerprint": f"fingerprint-{path}",
                "task_id": task_id,
                "repo_path": str(log_root.parent),
                "paths": [path],
                "side_effect": "workspace_write",
                "diff": f"--- a/{path}\n+++ b/{path}\n@@\n-a\n+b\n",
            }
        ),
        encoding="utf-8",
    )


def test_approval_decisions_and_scope_policy_are_shared(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    path = "shared/file.py"

    repl_task = "agent-repl"
    _write_approval(log_root, repl_task, path)
    repl_policy = commands_mod.ApprovalPolicy()
    repl_state = {
        "repo": str(tmp_path / "repo"),
        "file_config": {},
        "approval_policy": repl_policy,
    }
    interactive._slash_command(
        f"/approve {repl_task} path",
        f"/approve {repl_task} path",
        {"task_id": repl_task},
        log_root,
        repl_state,
    )

    tui_task = "agent-tui"
    _write_approval(log_root, tui_task, path)
    tui_policy = commands_mod.ApprovalPolicy()
    app = _make_app(tmp_path)
    app.state["approval_policy"] = tui_policy
    app.last["task_id"] = tui_task
    app._slash_command(f"/approve {tui_task} path", f"/approve {tui_task} path")

    headless_task = "agent-headless"
    _write_approval(log_root, headless_task, path)
    headless_policy = commands_mod.ApprovalPolicy()
    result = command_exec.run_command_line(
        f"/approve {headless_task} path",
        log_root=log_root,
        repo=tmp_path / "repo",
        state={
            "active_task_id": headless_task,
            "approval_policy": headless_policy,
        },
    )

    assert result.ok is True
    for task_id in (repl_task, tui_task, headless_task):
        decision = json.loads(
            (log_root / f"{task_id}.runtime" / "approval" / "decision.json").read_text(
                encoding="utf-8"
            )
        )
        assert decision["decision"] == "approve"
    for policy in (repl_policy, tui_policy, headless_policy):
        grant = policy.grants[0]
        assert grant.scope == "path"
        view = commands_mod.approval_request_view(
            {
                "repo_path": str(tmp_path),
                "paths": [path],
                "side_effect": "workspace_write",
            }
        )
        assert policy.matching(view) is grant


def test_manual_approval_task_ids_are_contained_and_identity_checked(
    tmp_path: Path,
) -> None:
    log_root = tmp_path / "logs"
    _write_approval(log_root, "agent-valid", "shared/file.py")
    outside = tmp_path.parent / "outside.runtime"
    assert interactive._decide_pending(log_root, "../outside", approve=True) is None
    assert not (outside / "approval" / "decision.json").exists()
    assert interactive._decide_pending(log_root, "agent-valid", approve=True) is True

    request = json.loads(
        (log_root / "agent-valid.runtime" / "approval" / "request.json").read_text(
            encoding="utf-8"
        )
    )
    request["task_id"] = "agent-other"
    gate = log_root / "agent-valid.runtime" / "approval"
    gate.joinpath("request.json").write_text(json.dumps(request), encoding="utf-8")
    (gate / "decision.json").unlink(missing_ok=True)
    assert interactive._decide_pending(log_root, "agent-valid", approve=True) is None
    assert not (gate / "decision.json").exists()


def test_resume_dispatches_through_the_shared_handler_on_all_surfaces(
    tmp_path: Path, monkeypatch
) -> None:
    log_root = tmp_path / "logs"
    task_id = "agent-resume"
    _write_events(
        log_root,
        task_id,
        [
            {"kind": "task_start", "data": {"repo_path": str(tmp_path)}},
            {"kind": "result", "data": {"status": "completed_unverified"}},
        ],
    )
    calls: list[str] = []

    def fake_resume(resumed_id: str, _log_root: Path, _state: dict[str, Any]):
        calls.append(resumed_id)
        return {"task_id": resumed_id, "status": "running", "diff": ""}

    monkeypatch.setattr(interactive, "_resume_task", fake_resume)
    monkeypatch.setattr(interactive, "session_status", lambda *_args: "resumable")

    app = _make_app(tmp_path)
    app._slash_command(f"/resume {task_id}", f"/resume {task_id}")
    if app._worker_thread is not None:
        app._worker_thread.join(timeout=5)

    repl_state = {"repo": str(tmp_path / "repo"), "file_config": {}}
    interactive._slash_command(
        f"/resume {task_id}",
        f"/resume {task_id}",
        {},
        log_root,
        repl_state,
    )
    result = command_exec.run_command_line(
        f"/resume {task_id}",
        log_root=log_root,
        repo=tmp_path / "repo",
        state={},
    )

    assert result.ok is True
    assert calls == [task_id, task_id, task_id]


def test_model_skill_connector_and_undo_state_reuse_shared_backends(
    tmp_path: Path, monkeypatch
) -> None:
    app = _make_app(tmp_path)
    repl_state = {"repo": str(tmp_path / "repo"), "file_config": {}}

    app._slash_command("/model shared-model", "/model shared-model")
    interactive._slash_command(
        "/model shared-model", "/model shared-model", {}, app.log_root, repl_state
    )
    assert app.state["model"] == repl_state["model"] == "shared-model"

    skill_calls: list[tuple[Any, str, str]] = []
    monkeypatch.setattr(
        interactive,
        "_render_skills",
        lambda repo, say=None, filt="": skill_calls.append((repo, filt, str(say))),
    )
    app._slash_command("/skills auth", "/skills auth")
    interactive._slash_command(
        "/skills auth", "/skills auth", {}, app.log_root, repl_state
    )
    assert [call[1] for call in skill_calls] == ["auth", "auth"]

    connector_calls: list[Any] = []
    monkeypatch.setattr(
        interactive,
        "mcp_server_table",
        lambda config, repo_path=None: (
            connector_calls.append((config, repo_path))
            or [
                {
                    "label": "memory",
                    "source": "project",
                    "command": "python -m mcp_server",
                }
            ]
        ),
    )
    interactive._slash_command("/mcp", "/mcp", {}, app.log_root, repl_state)
    app._slash_command("/mcp", "/mcp")
    if app._connector_thread is not None:
        app._connector_thread.join(timeout=5)
    assert len(connector_calls) == 2

    undo_calls: list[str] = []
    monkeypatch.setattr(
        interactive,
        "undo_result",
        lambda last, _logs, _repo, arg: (
            undo_calls.append(str(arg)) or {"outcome": "nothing"}
        ),
    )
    app._slash_command("/undo", "/undo")
    interactive._slash_command(
        "/undo", "/undo", {"task_id": "agent-undo"}, app.log_root, repl_state
    )
    assert undo_calls == ["", ""]


def test_prompt_patch_context_is_reentrant_and_always_restores() -> None:
    import cli.tui as tui

    repo = Path(".")
    app = tui.NeoApp(
        repo=repo,
        log_root=Path("logs"),
        state={"repo": str(repo), "file_config": {}},
        file_config={},
    )
    original_input = builtins.input
    original_print = builtins.print
    with app._prompt_patches():
        outer_input = builtins.input
        with app._prompt_patches():
            assert builtins.input is not original_input
        assert builtins.input is outer_input
    assert builtins.input is original_input
    assert builtins.print is original_print
    assert tui._PROMPT_PATCH_STACK == []


@pytest.mark.anyio
async def test_tui_mount_restores_theme_hooks_and_builtins_after_repeats(
    tmp_path: Path,
) -> None:
    import cli.tui as tui

    original_tokens = ui.active_tokens()
    original_input = builtins.input
    original_print = builtins.print
    original_hooks = (
        interactive._ON_TASK_START,
        interactive._CANCEL_RUN,
        interactive._PROMPT_BODY,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    for index, theme in enumerate(
        ("high-contrast", "default", "reduced-motion", "default")
    ):
        app = tui.NeoApp(
            repo=repo,
            log_root=tmp_path / f"logs-{index}",
            state={"repo": str(repo), "file_config": {}},
            file_config={},
            theme=theme,
        )
        async with app.run_test(size=(100, 30)):
            await app.workers.wait_for_complete()
            assert ui.active_tokens().theme == theme
        assert ui.active_tokens() == original_tokens
        assert builtins.input is original_input
        assert builtins.print is original_print
        assert original_hooks == (
            interactive._ON_TASK_START,
            interactive._CANCEL_RUN,
            interactive._PROMPT_BODY,
        )
        assert tui._PROMPT_PATCH_STACK == []


# ===========================================================================
# Round 2 (VEX-TERM-UX-07) — one command system, one envelope, one meaning
# ===========================================================================

#: The keys every surface's command record must carry. Expressed as a
#: constant rather than inlined so a surface cannot be compared against a
#: list that was written for the surface being tested.
SHARED_RECORD_KEYS = frozenset(
    {
        "command",
        "args",
        "status",
        "state",
        "state_before",
        "state_after",
        "exit_code",
        "task_id",
        "verification_state",
        "verdict",
        "verified",
        "events",
        "recovery",
        "presentation",
    }
)

#: What each surface adds on top of the shared record. Declared, not
#: discovered: a surface gaining a key the others lack is a decision
#: somebody makes, and this list is where the decision is written down.
SURFACE_ONLY_KEYS = {
    "repl": set(),
    "tui": set(),
    "headless": {
        "cost",
        "model",
        "provider",
        "requires",
        "text",
        "unavailable_reason",
    },
}

#: Keys the INTERACTIVE record carries and the headless one expresses
#: differently. `surface` is genuinely different (it says which surface
#: published it) and `schema_version`/`message` are the wire envelope the
#: headless document replaces with its own flat fields.
INTERACTIVE_ONLY_KEYS = {"message", "schema_version", "surface"}


def _repl_record(
    log_root: Path, line: str, repo: Path, last: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one line through the REPL dispatcher and return its record."""
    state: dict[str, Any] = {
        "repo": str(repo),
        "file_config": {},
        "approval_policy": commands_mod.ApprovalPolicy(),
    }
    interactive._slash_command(line, line.lower(), dict(last or {}), log_root, state)
    return dict(state["last_command"])


def _tui_record(app: Any, line: str) -> dict[str, Any]:
    """Run one line through the TUI dispatcher and return its record."""
    app._slash_command(line, line.lower())
    return dict(app.last_command)


def _dispatcher_keys(path: Path, function: str) -> set[str]:
    """Return the slash names a `_slash_command_impl` body dispatches on.

    Structural, not textual: the branch shape is
    ``if cmd in ("/a", "/b"):`` or ``if cmd == "/a":`` and the tuple may
    span lines. Reading the AST instead of grepping means a reformat cannot
    silently empty the pin, and a comment cannot make it pass.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test = inner.test
            if isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.Eq):
                for side in (test.left, test.comparators[0]):
                    if (
                        isinstance(side, ast.Constant)
                        and isinstance(side.value, str)
                        and side.value.startswith("/")
                    ):
                        names.add(side.value)
            elif isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.In):
                container = test.comparators[0]
                if isinstance(container, (ast.Tuple, ast.List, ast.Set)):
                    for element in container.elts:
                        if (
                            isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                            and element.value.startswith("/")
                        ):
                            names.add(element.value)
    return names


class TestOneCommandSystem:
    """The record, the envelope, and the vocabulary are one contract."""

    def test_every_surface_builds_its_record_through_the_shared_reducer(
        self,
    ) -> None:
        """No surface hand-builds an outcome.

        Three call sites used to call ``command_outcome`` directly with
        their own `presentation`, their own status, and their own exit
        code. That is the duplication the prompt names, and a source pin
        is the only thing that catches its return: a behavioural test
        passes for every status anybody happened to exercise.
        """
        repo_root = Path(__file__).resolve().parents[1]
        for name in ("command_exec.py", "interactive.py", "tui.py"):
            source = (repo_root / "cli" / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            calls = [
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ]
            assert "command_outcome" not in calls, (
                f"cli/{name} calls commands.command_outcome directly; the record "
                "must go through commands.command_record so the presentation, "
                "the recovery list, and the status cannot be chosen per surface"
            )
            assert "command_record" in calls, (
                f"cli/{name} builds no command record through the shared reducer"
            )

    def test_the_shared_record_keys_are_present_on_every_surface(
        self, tmp_path: Path
    ) -> None:
        """One key set, checked against a CONSTANT rather than a copy.

        Headless used to be the only surface carrying `verdict`/`verified`,
        so a script could ask a machine question about a run that a TUI
        user could not ask and a REPL user could not ask.
        """
        log_root = tmp_path / "logs"
        tui_record = _tui_record(_make_app(tmp_path), "/status")
        repl_record = _repl_record(log_root, "/status", tmp_path / "repo")
        headless = command_exec.run_command_line(
            "/status", log_root=log_root, repo=tmp_path / "repo"
        )
        headless_record = headless.to_dict()

        for surface, record in (
            ("tui", tui_record),
            ("repl", repl_record),
            ("headless", headless_record),
        ):
            assert not SHARED_RECORD_KEYS - set(record), (
                f"{surface} record is missing {sorted(SHARED_RECORD_KEYS - set(record))}"
            )
            extra = set(record) - SHARED_RECORD_KEYS
            assert extra == SURFACE_ONLY_KEYS[surface] | (
                INTERACTIVE_ONLY_KEYS if surface != "headless" else set()
            ), f"{surface} record has undeclared keys: {sorted(extra)}"

    def test_the_status_vocabulary_is_closed_and_observed_statuses_are_declared(
        self, tmp_path: Path
    ) -> None:
        """A new status word cannot appear in one shell by accident.

        `status` is what a script branches on, so a word that only the TUI
        can emit is a script that works in a terminal and fails in CI.
        """
        log_root = tmp_path / "logs"
        app = _make_app(tmp_path)
        observed: set[str] = set()
        for line in ("/status", "/nope", "/quiet loud", "/effort high", "/watch"):
            observed.add(_tui_record(app, line)["status"])
            observed.add(_repl_record(log_root, line, tmp_path / "repo")["status"])
            observed.add(
                command_exec.run_command_line(
                    line, log_root=log_root, repo=tmp_path / "repo"
                ).status
            )
        assert observed, "no status was observed; the gate cannot fail"
        assert not observed - commands_mod.COMMAND_STATUSES, (
            f"undeclared statuses: {sorted(observed - commands_mod.COMMAND_STATUSES)}"
        )

    def test_the_event_envelope_is_authored_by_the_surface_that_published_it(
        self, tmp_path: Path
    ) -> None:
        """A headless event row says `headless`, even for a mapped command.

        The headless adapter used to copy the REPL handler's event rows
        verbatim, so a mapped command published `surface: "repl"` while an
        early refusal published `surface: "headless"` — the same event
        kinds under two labels depending only on which branch built the
        envelope.
        """
        log_root = tmp_path / "logs"
        mapped = command_exec.run_command_line(
            "/quiet", log_root=log_root, repo=tmp_path / "repo"
        )
        refused = command_exec.run_command_line(
            "/nope", log_root=log_root, repo=tmp_path / "repo"
        )
        for result in (mapped, refused):
            assert result.events, "a headless result published no events"
            assert {event["surface"] for event in result.events} == {"headless"}, (
                f"headless published {sorted({e['surface'] for e in result.events})}"
            )
            for event in result.events:
                assert event["event"] in commands_mod.COMMAND_EVENT_TYPES
                assert event["state"] in commands_mod.SURFACE_STATES

    def test_an_unknown_command_is_a_refusal_on_every_surface(
        self, tmp_path: Path
    ) -> None:
        """The TUI must not accept a name no registry row describes.

        The TUI's preflight was gated on `resolution.spec is not None`, so
        an unknown name skipped the refusal, fell through to the
        dispatcher, came back as "no handler ran", and was recorded
        `ok`/0 — a green record for a command the user got wrong, on the
        one surface where a typo is easiest to make (a palette-less
        keystroke).
        """
        log_root = tmp_path / "logs"
        tui_record = _tui_record(_make_app(tmp_path), "/definitely-not-a-command")
        repl_record = _repl_record(
            log_root, "/definitely-not-a-command", tmp_path / "repo"
        )
        headless = command_exec.run_command_line(
            "/definitely-not-a-command", log_root=log_root, repo=tmp_path / "repo"
        )
        for surface, status, exit_code in (
            ("tui", tui_record["status"], tui_record["exit_code"]),
            ("repl", repl_record["status"], repl_record["exit_code"]),
            ("headless", headless.status, headless.exit_code),
        ):
            assert status == "unknown", f"{surface} said {status!r} for a typo"
            assert exit_code == EXIT_CODES["usage_error"], surface

    def test_a_project_custom_command_is_not_an_unknown_command(
        self, tmp_path: Path
    ) -> None:
        """The "unknown" rule must not delete project commands.

        `.neo/commands/<name>.md` is a real command that lives outside
        the built-in registry, so an unregistered name is only a refusal
        when no template backs it. Making `unknown` unconditional in the
        preflight removed the feature while still looking correct for
        genuine typos — the two are indistinguishable from the name.
        """
        repo = tmp_path / "repo"
        commands_dir = repo / ".neo" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "shipit.md").write_text("do the thing", encoding="utf-8")
        assert commands_mod.is_custom_command_line("/shipit", str(repo)) is True
        assert commands_mod.is_custom_command_line("/nope-not-here", str(repo)) is False

        log_root = tmp_path / "logs"
        headless = command_exec.run_command_line(
            "/shipit", log_root=log_root, repo=repo
        )
        assert headless.exit_code == EXIT_CODES["usage_error"]
        assert headless.status == "refused", (
            f"a real project command was reported as {headless.status!r}"
        )
        assert "custom command" in headless.text

    def test_a_flag_only_refusal_names_what_to_run_instead(
        self, tmp_path: Path
    ) -> None:
        """A `flag-only` row must name the flag, on every surface.

        The registry validation only checked `has an equivalent => is
        flag-only`, so `/effort` was `flag-only` with NO equivalent and the
        adapter rendered "…has a dedicated flag in this surface: " with
        nothing after the colon. A one-directional gate is not a gate.
        """
        log_root = tmp_path / "logs"
        for spec in commands_mod.COMMAND_SPECS:
            if commands_mod.headless_policy(spec.name) != "flag-only":
                continue
            equivalent = commands_mod.headless_equivalent(spec.name)
            assert equivalent.strip(), (
                f"{spec.name} is flag-only and names nothing to run instead"
            )
            # A `flag-only` command with `argument_policy == "none"` is
            # refused as `invalid` before the policy is consulted, so the
            # bare name is the line that reaches the flag branch.
            line = spec.name if spec.argument_policy == "none" else f"{spec.name} probe"
            headless = command_exec.run_command_line(
                line, log_root=log_root, repo=tmp_path / "repo"
            )
            assert headless.status == "flag", (spec.name, headless.status)
            assert headless.exit_code == EXIT_CODES["usage_error"], spec.name
            assert headless.requires == equivalent, spec.name
            assert equivalent in headless.text, spec.name

    def test_the_verdict_is_derived_from_the_run_not_from_the_command_word(
        self, tmp_path: Path
    ) -> None:
        """A verified run reads verified, everywhere, and nothing else does.

        The record used to reduce the COMMAND's lifecycle word, so a
        command against a run whose journal said `completed_verified` with
        clean evidence reported `unverified` / `verified: false`. Denying a
        verified run is the same class of lie as promoting an unverified
        one.
        """
        log_root = tmp_path / "logs"
        verified = "agent-verdict-verified"
        _write_events(log_root, verified, _state_events("completed_verified"))
        bare = "agent-verdict-bare"
        _write_events(
            log_root,
            bare,
            [
                {"kind": "task_start", "data": {"repo_path": "."}},
                {"kind": "result", "data": {"status": "success"}},
            ],
        )

        for surface, record in (
            # No task is selected, so there is no run to have a verdict
            # about and both interactive records must say `no_run`.
            ("tui", _tui_record(_make_app(tmp_path), "/status")),
            ("repl", _repl_record(log_root, "/status", tmp_path / "repo")),
        ):
            assert record["verdict"] == commands_mod.NO_RUN_VERDICT, surface
            assert record["verified"] is False, surface

        snapshot = runview.read_live_projection(log_root / verified)
        for surface, record in (
            (
                "tui",
                _verified_record(_make_app(tmp_path), verified, snapshot),
            ),
            (
                "repl",
                _verified_record_repl(log_root, tmp_path / "repo", verified, snapshot),
            ),
        ):
            assert record["verdict"] == "verified", (
                f"{surface} denied a run with clean verifier evidence: {record['verdict']}"
            )
            assert record["verified"] is True, surface
            assert record["verification_state"] == "verified", surface

        bare_snapshot = runview.read_live_projection(log_root / bare)
        bare_record = _verified_record_repl(
            log_root, tmp_path / "repo", bare, bare_snapshot
        )
        assert bare_record["verified"] is False
        assert bare_record["verdict"] == "unverified"

    def test_a_refusal_carries_the_same_meaning_on_every_surface(
        self, tmp_path: Path
    ) -> None:
        """The refusal vocabulary, all three surfaces, one meaning.

        A command a surface DECLINES by policy is a different case from
        one it refuses by mistake: `/mode` is `headless: refuse` because
        mode selection needs a live run, and that is a declared policy,
        not a divergence. The pin is therefore split in two — the
        vocabulary that must agree, and the policies that are allowed to
        differ and must say so in the registry.
        """
        log_root = tmp_path / "logs"
        app = _make_app(tmp_path)
        for line in ("/quiet loud", "/definitely-not-a-command"):
            tui_record = _tui_record(app, line)
            repl_record = _repl_record(log_root, line, tmp_path / "repo")
            headless = command_exec.run_command_line(
                line, log_root=log_root, repo=tmp_path / "repo"
            )
            assert tui_record["status"] == repl_record["status"] == headless.status, (
                line
            )
            assert (
                tui_record["exit_code"]
                == repl_record["exit_code"]
                == headless.exit_code
            ), line
            assert tui_record["recovery"] == repl_record["recovery"], line
            assert tui_record["state_before"] == repl_record["state_before"], line

        # A policy refusal is a refusal, and it names its own reason.
        headless = command_exec.run_command_line(
            "/mode build", log_root=log_root, repo=tmp_path / "repo"
        )
        assert commands_mod.headless_policy("/mode") == "refuse"
        assert headless.exit_code == EXIT_CODES["usage_error"]
        assert headless.unavailable_reason, "a refusal with no reason is not actionable"

    def test_a_command_that_runs_says_so_on_every_surface(self, tmp_path: Path) -> None:
        """The success side of the same contract, not just the refusals.

        `/quiet` is chosen because it is ``headless: mapped`` and takes no
        arguments, so the same line reaches the same branch on all three
        surfaces instead of tripping a policy divergence.
        """
        log_root = tmp_path / "logs"
        app = _make_app(tmp_path)
        assert commands_mod.headless_policy("/quiet") == "mapped"
        tui_record = _tui_record(app, "/quiet")
        repl_record = _repl_record(log_root, "/quiet", tmp_path / "repo")
        headless = command_exec.run_command_line(
            "/quiet", log_root=log_root, repo=tmp_path / "repo"
        )
        for surface, record in (
            ("tui", tui_record),
            ("repl", repl_record),
            ("headless", {"status": headless.status, "exit_code": headless.exit_code}),
        ):
            assert record["status"] == "ok", surface
            assert record["exit_code"] == 0, surface


def _verified_record(
    app: Any, task_id: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Run `/status` on a TUI app that has selected one task."""
    app.last["task_id"] = task_id
    app._active_snapshot = lambda: snapshot  # the journal is the source
    return _tui_record(app, "/status")


def _verified_record_repl(
    log_root: Path, repo: Path, task_id: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Run `/status` on a REPL that has selected one task."""
    return _repl_record(log_root, "/status", repo, last={"task_id": task_id})


class TestErrorsAndExitCodesAreEquivalent:
    """One exception, one meaning, on all three surfaces."""

    @pytest.mark.parametrize(
        "exc_name", ["KeyboardInterrupt", "SandboxUnavailableError", "Boom"]
    )
    def test_an_escaping_handler_means_the_same_thing_everywhere(
        self, tmp_path: Path, monkeypatch, exc_name: str
    ) -> None:
        """The three surfaces used to answer this differently.

        | exception | REPL | TUI | headless |
        |---|---|---|---|
        | Ctrl+C | `error` / 1 | `error` / 1 | `cancelled` / 130 |
        | Docker down | `error` / 1 | `error` / 1 | `error` / 3 |

        So a `Ctrl+C` was a *task failure* in two shells and an
        *interruption* in a script, and a broken sandbox was "retry the
        run" in two shells and "fix the machine" in a script. Each of
        those is a lie about which door the user is standing in.
        """
        log_root = tmp_path / "logs"
        if exc_name == "KeyboardInterrupt":
            exc: BaseException = KeyboardInterrupt()
        elif exc_name == "SandboxUnavailableError":
            from execution.sandbox import SandboxUnavailableError

            exc = SandboxUnavailableError("daemon unreachable")
        else:
            exc = RuntimeError("handler blew up")

        expected_status, expected_exit = commands_mod.command_failure(exc)

        monkeypatch.setattr(
            interactive,
            "_slash_command_impl",
            lambda *a, **k: (_ for _ in ()).throw(exc),
        )
        state: dict[str, Any] = {
            "repo": str(tmp_path / "repo"),
            "file_config": {},
            "approval_policy": commands_mod.ApprovalPolicy(),
        }
        with pytest.raises(BaseException):
            interactive._slash_command("/quiet", "/quiet", {}, log_root, state)
        repl_record = state["last_command"]
        # Headless runs the SAME patched handler, so the patch stays in
        # place for this call: the two answers have to be the two
        # surfaces' answers to the same exception, not the same surface
        # before and after a patch removal.
        headless = command_exec.run_command_line(
            "/quiet", log_root=log_root, repo=tmp_path / "repo"
        )

        assert repl_record["status"] == expected_status == headless.status, exc_name
        assert repl_record["exit_code"] == expected_exit == headless.exit_code, (
            f"{exc_name}: repl={repl_record['exit_code']} headless={headless.exit_code}"
        )
        if exc_name == "KeyboardInterrupt":
            assert expected_exit == EXIT_CODES["interrupted"]

    def test_the_tui_records_the_same_answer_as_the_repl(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The TUI is the third surface, not a bystander."""
        from cli import tui as tui_mod

        exc = KeyboardInterrupt()
        monkeypatch.setattr(
            tui_mod.NeoApp,
            "_slash_command_impl",
            lambda self, *a, **k: (_ for _ in ()).throw(exc),
        )
        app = _make_app(tmp_path)
        with pytest.raises(KeyboardInterrupt):
            app._slash_command("/quiet", "/quiet")
        status, exit_code = commands_mod.command_failure(exc)
        assert app.last_command["status"] == status
        assert app.last_command["exit_code"] == exit_code
        assert exit_code == EXIT_CODES["interrupted"]


class TestNoSurfaceOnlyBehaviour:
    """TUI-only state must not change what a command does."""

    def test_the_dispatcher_branches_are_the_same_set_on_both_shells(
        self,
    ) -> None:
        """A command is a command, and both dispatchers know the set.

        `/watch` existed as a TUI branch and in no registry row, so it ran
        in one shell, was "unknown" in the REPL, and was invisible to the
        palette, `/help`, and the headless table — a command a user could
        learn from one surface and not use in another. `/steer` was the
        mirror image: a reader-thread branch in the REPL and nothing in
        the main-loop dispatcher, so where your keystroke landed decided
        whether it worked. The AST pins the SET, so a future branch has to
        be registered in the same change.
        """
        root = Path(__file__).resolve().parents[1] / "cli"
        repl_keys = _dispatcher_keys(root / "interactive.py", "_slash_command_impl")
        tui_keys = _dispatcher_keys(root / "tui.py", "_slash_command_impl")
        assert repl_keys, "the AST read found no REPL branches; the pin is empty"
        assert tui_keys, "the AST read found no TUI branches; the pin is empty"
        assert tui_keys - repl_keys == set(), (
            f"TUI dispatches commands the REPL does not: {sorted(tui_keys - repl_keys)}"
        )
        assert repl_keys - tui_keys == set(), (
            f"REPL dispatches commands the TUI does not: {sorted(repl_keys - tui_keys)}"
        )
        unregistered = {
            name
            for name in repl_keys | tui_keys
            if commands_mod.command_spec(name) is None
        }
        assert not unregistered, (
            f"dispatched commands with no registry row: {sorted(unregistered)}"
        )
        assert "/watch" in repl_keys and "/watch" in tui_keys, (
            "the set-equality above is satisfied by both dropping a command; "
            "/watch is the one that was TUI-only, so it is named here"
        )

    def test_a_delegated_command_record_is_stamped_by_the_surface_that_ran_it(
        self, tmp_path: Path
    ) -> None:
        """`/settings`, `/plugins`, `/theme` run the REPL handler here.

        The TUI used to copy that handler's record verbatim, so a TUI
        session published `surface: "repl"` — a record it did not author.
        The DECISION the delegated handler made is still kept (that is
        the point of sharing), but the envelope is the running surface's
        and the provenance is stated rather than implied.
        """
        app = _make_app(tmp_path)
        app._slash_command("/plugins", "/plugins")
        if app._connector_thread is not None:
            app._connector_thread.join(timeout=5)
        assert app.last_command["surface"] == "tui", app.last_command["surface"]
        assert app.last_command.get("delegated_from") == "repl", (
            "the TUI must say which handler decided, not inherit its envelope"
        )

    def test_a_delegated_theme_change_is_still_restored_on_unmount(
        self, tmp_path: Path
    ) -> None:
        """TUI-only global state that outlives the mount.

        `/theme` installs process-wide tokens inside the REPL handler. For
        `/theme reset` the argument is not a theme NAME, so the TUI's own
        `_apply_theme` never ran, `self._tokens` went stale, and the
        `on_unmount` guard `ui.active_tokens() == self._tokens` stopped
        holding — so the pre-mount theme was never restored. A session
        that only ever typed `/theme reset` left the process wearing the
        last theme it was told to reset.
        """
        before = ui.active_tokens()
        app = _make_app(tmp_path)
        for line in ("/theme high-contrast", "/theme reset"):
            app._slash_command(line, line)
        assert ui.active_tokens().theme == "default", ui.active_tokens().theme
        app.on_unmount()
        assert ui.active_tokens() == before, (
            f"mount/unmount leaked the theme: {before.theme} -> {ui.active_tokens().theme}"
        )

    def test_a_live_run_registered_on_one_surface_is_live_on_the_other(
        self, tmp_path: Path
    ) -> None:
        """`_LIVE_RUN` is shared state, and it has to behave that way.

        The TUI's in-flight predicate and the REPL's both read
        `interactive._LIVE_RUN`, which is the point — a run started in one
        shell must make a mutating command unavailable in the other, or
        the "no TUI-only global state changes behaviour" promise is false
        for the one global that decides whether a mutation is allowed.
        """
        log_root = tmp_path / "logs"
        task_id = "agent-shared-live"
        _write_events(log_root, task_id, _state_events("running"))
        interactive._set_live_run(task_id, log_root)
        try:
            app = _make_app(tmp_path)
            app.last["task_id"] = task_id
            tui_record = _tui_record(app, "/diff undo")
            repl_record = _repl_record(
                log_root, "/diff undo", tmp_path / "repo", last={"task_id": task_id}
            )
            headless = command_exec.run_command_line(
                "/diff undo",
                log_root=log_root,
                repo=tmp_path / "repo",
                state={"active_task_id": task_id},
            )
            for surface, record in (
                ("tui", tui_record),
                ("repl", repl_record),
                (
                    "headless",
                    {"status": headless.status, "exit_code": headless.exit_code},
                ),
            ):
                assert record["status"] == "disabled", surface
                assert record["exit_code"] == EXIT_CODES["usage_error"], surface
        finally:
            interactive._clear_live_run()

    def test_the_registry_declares_a_headless_policy_for_every_command(self) -> None:
        """Both directions of the flag-equivalent relationship.

        The import-time validator ran the "has an equivalent => is
        flag-only" direction only. The missing direction is the one a
        user meets: a refusal that cannot say what to run instead.
        """
        known = {spec.name for spec in commands_mod.COMMAND_SPECS}
        assert not known - set(commands_mod.HEADLESS_COMMAND_POLICIES)
        for name, policy in commands_mod.HEADLESS_COMMAND_POLICIES.items():
            if policy == "flag-only":
                assert commands_mod.HEADLESS_FLAG_EQUIVALENTS.get(name, "").strip(), (
                    name
                )
            else:
                assert name not in commands_mod.HEADLESS_FLAG_EQUIVALENTS, name
        assert not set(commands_mod.REQUIRED_COMMANDS) - known
