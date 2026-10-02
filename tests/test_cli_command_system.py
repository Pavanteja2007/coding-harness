"""Regression tests for the shared command contract (VEX-TERM-UX-04).

Covers the three surfaces as one system:
- the typed registry in ``cli.commands`` (metadata, availability, recovery),
- the REPL preflight that dispatches through it,
- the headless adapter's stable exit codes and refusal honesty,
- the approval-scope normalization every surface shares.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List

import pytest

from cli import command_exec
from cli import commands as commands_mod
from cli.exit_codes import EXIT_CODES

REQUIRED = (
    "/help",
    "/status",
    "/plan",
    "/build",
    "/ask",
    "/review",
    "/diff",
    "/checkpoints",
    "/undo",
    "/redo",
    "/sessions",
    "/resume",
    "/share",
    "/export",
    "/cost",
    "/context",
    "/trace",
    "/feed",
    "/mcp",
    "/skills",
    "/plugins",
    "/init",
    "/login",
    "/logout",
    "/model",
    "/compact",
    "/steer",
    "/cancel",
    "/approve",
    "/reject",
    "/theme",
    "/settings",
    "/open",
    "/doctor",
    "/repo",
    "/quit",
)


class TestCommandRegistryContract:
    def test_every_required_command_is_registered(self):
        for name in REQUIRED:
            spec = commands_mod.command_spec(name)
            assert spec is not None, f"{name} is not registered"
            assert spec.name == name

    def test_required_command_set_matches_the_registry_claim(self):
        assert set(commands_mod.REQUIRED_COMMANDS) == set(REQUIRED)

    def test_every_spec_declares_full_metadata(self):
        for spec in commands_mod.COMMAND_SPECS:
            assert spec.summary, f"{spec.name} has no summary"
            assert spec.argument_policy in commands_mod.ARGUMENT_POLICIES
            assert spec.idle_policy in commands_mod.BEHAVIOR_POLICIES
            assert spec.in_flight_policy in commands_mod.BEHAVIOR_POLICIES
            assert spec.result_presentation in commands_mod.RESULT_PRESENTATIONS
            assert spec.required_permissions, f"{spec.name} declares no permissions"
            assert spec.failure_recovery, f"{spec.name} declares no recovery"
            for action in spec.failure_recovery:
                assert action in commands_mod.RECOVERY_ACTIONS
            for permission in spec.required_permissions:
                assert permission in commands_mod.PERMISSION_SCOPES

    def test_permissions_are_registered_scopes(self):
        for spec in commands_mod.COMMAND_SPECS:
            for permission in spec.required_permissions:
                assert permission in commands_mod.PERMISSION_SCOPES
                assert ":" in permission

    def test_registry_serializes_for_a_palette_or_headless_caller(self):
        for spec in commands_mod.COMMAND_SPECS:
            row = spec.to_dict()
            assert row["name"] == spec.name
            assert row["required_permissions"] == list(spec.required_permissions)
            assert row["result_presentation"] == spec.result_presentation
            assert row["failure_recovery"] == list(spec.failure_recovery)

    def test_aliases_resolve_to_the_canonical_spec(self):
        assert commands_mod.command_spec("/changes").name == "/diff"
        # An exact name always wins over an alias that shares its spelling,
        # so "/diff" is the diff view, never the undo alias.
        assert commands_mod.command_spec("/diff").name == "/diff"
        assert commands_mod.command_spec("diff").name == "/diff"
        assert commands_mod.command_spec("/NOPE") is None

    def test_usage_line_is_copyable(self):
        usage = commands_mod.command_usage(commands_mod.command_spec("/diff"))
        assert usage.startswith("usage: /diff")


class TestAvailabilityAndRecovery:
    def test_idle_refusal_is_explicit(self):
        spec = commands_mod.command_spec("/steer")
        availability = commands_mod.command_availability(
            spec, commands_mod.CommandContext(surface="repl", in_flight=False)
        )
        assert availability.available is False
        assert "no active run" in availability.reason

    def test_steer_is_available_while_a_run_is_live(self):
        spec = commands_mod.command_spec("/steer")
        availability = commands_mod.command_availability(
            spec, commands_mod.CommandContext(surface="repl", in_flight=True)
        )
        assert availability.available is True

    def test_in_flight_refusal_is_explicit(self):
        spec = commands_mod.command_spec("/login")
        availability = commands_mod.command_availability(
            spec, commands_mod.CommandContext(surface="repl", in_flight=True)
        )
        assert availability.available is False
        assert "run is active" in availability.reason

    def test_missing_permission_is_named(self):
        spec = commands_mod.command_spec("/undo")
        availability = commands_mod.command_availability(
            spec,
            commands_mod.CommandContext(
                surface="repl", denied_permissions=("workspace:write",)
            ),
        )
        assert availability.available is False
        assert "workspace:write" in availability.reason

    def test_no_task_is_a_note_not_a_silent_gate(self):
        # /status degrades honestly in its own handler, so availability
        # annotates the palette instead of pre-empting the real message.
        spec = commands_mod.command_spec("/status")
        availability = commands_mod.command_availability(
            spec, commands_mod.CommandContext(surface="repl", has_task=False)
        )
        assert availability.available is True
        assert "needs a task" in availability.note

    def test_recovery_hint_is_actionable(self):
        hint = commands_mod.command_recovery_hint(commands_mod.command_spec("/diff"))
        assert hint.startswith("next: ")
        assert "/trace" in hint

    def test_unknown_recovery_hint_still_offers_a_way_forward(self):
        hint = commands_mod.command_recovery_hint(None)
        assert "edit input" in hint
        assert "safe state" in hint


class TestResolution:
    def test_unknown_command_is_a_usage_error(self):
        resolution = commands_mod.resolve_command_line("/definitely-not-real")
        assert resolution.status == "unknown"
        assert resolution.exit_code == EXIT_CODES["usage_error"]
        assert "edit-input" in resolution.recovery

    def test_missing_required_argument_is_a_usage_error(self):
        resolution = commands_mod.resolve_command_line("/steer")
        assert resolution.status == "invalid"
        assert "usage: /steer" in resolution.message
        assert resolution.exit_code == EXIT_CODES["usage_error"]

    def test_unexpected_argument_is_a_usage_error(self):
        resolution = commands_mod.resolve_command_line("/quiet loud")
        assert resolution.status == "invalid"

    def test_optional_argument_accepts_both_forms(self):
        assert commands_mod.resolve_command_line("/diff").status == "ok"
        assert commands_mod.resolve_command_line("/diff undo app.py").status == "ok"

    def test_alias_resolves_to_canonical_with_arguments_preserved(self):
        resolution = commands_mod.resolve_command_line("/changes")
        assert resolution.spec is not None
        assert resolution.spec.name == "/diff"
        assert commands_mod.resolve_command_line("/changes undo").args == "undo"

    def test_plain_text_is_not_a_command(self):
        resolution = commands_mod.resolve_command_line("just a sentence")
        assert resolution.status == "not_command"

    def test_queued_command_reports_its_queue_state(self):
        spec = commands_mod.command_spec("/build")
        assert spec.in_flight_policy == "queue"
        resolution = commands_mod.resolve_command_line(
            "/build thing", commands_mod.CommandContext(surface="tui", in_flight=True)
        )
        assert resolution.status == "queued"
        assert resolution.exit_code == EXIT_CODES["success"]


class TestPaletteContract:
    def test_palette_is_generated_from_the_registry(self):
        entries = commands_mod.command_palette_entries()
        labels = [entry["label"] for entry in entries]
        for name in REQUIRED:
            assert name in labels

    def test_palette_entries_carry_execution_and_display_metadata(self):
        for entry in commands_mod.command_palette_entries():
            assert entry["kind"] == "command"
            assert isinstance(entry["run"], bool)
            assert entry["result_presentation"]
            assert entry["failure_recovery"]

    def test_palette_marks_a_disabled_command_with_a_reason(self):
        entries = commands_mod.command_palette_entries(
            commands_mod.CommandContext(surface="repl", in_flight=True)
        )
        login = next(e for e in entries if e["label"] == "/login")
        assert login["disabled"] is True
        assert "run is active" in login["disabled_reason"]

    def test_contextual_hints_change_with_run_state(self):
        idle = commands_mod.contextual_command_hints(active=False, width=120)
        active = commands_mod.contextual_command_hints(active=True, width=120)
        assert idle != active
        assert "/steer" in active
        assert "modal open" in commands_mod.contextual_command_hints(waiting=True)


class TestArgumentHints:
    def test_hint_reflects_the_command_under_typing(self):
        assert "/diff" in commands_mod.argument_hint("/dif")
        assert "undo" in commands_mod.argument_hint("/diff")

    def test_unknown_command_hint_points_at_help(self):
        assert "/help" in commands_mod.argument_hint("/zzz")

    def test_hint_never_invents_arguments(self):
        assert "no arguments" in commands_mod.argument_hint("/copy-diff")


class TestApprovalContract:
    def test_request_view_normalizes_paths_from_a_diff(self):
        view = commands_mod.approval_request_view(
            {
                "request_id": "r1",
                "task_id": "t1",
                "diff": "--- a/cli/x.py\n+++ b/cli/x.py\n@@\n-a\n+b\n",
                "command": "pytest tests/test_x.py",
            }
        )
        assert view.paths == ("cli/x.py",)
        assert view.command == "pytest tests/test_x.py"
        assert view.side_effect == "workspace_write"

    def test_request_view_redacts_secret_shaped_values(self):
        view = commands_mod.approval_request_view(
            {"diff": "export TOKEN=sk-abcdefghijklmnopqrstuvwx", "command": "deploy"}
        )
        assert "sk-abcdefghijklmnopqrstuvwx" not in view.diff

    def test_request_view_defaults_to_a_control_side_effect(self):
        assert commands_mod.approval_request_view({}).side_effect == "control"

    def test_path_scope_grants_only_the_named_paths(self):
        policy = commands_mod.ApprovalPolicy()
        first = commands_mod.approval_request_view({"paths": ["cli/x.py"]})
        policy.record(first, "path")
        assert policy.matching(first) is not None
        other = commands_mod.approval_request_view({"paths": ["cli/other.py"]})
        assert policy.matching(other) is None

    def test_path_scope_never_widens_to_an_unapproved_path(self):
        policy = commands_mod.ApprovalPolicy()
        policy.record(
            commands_mod.approval_request_view({"paths": ["cli/x.py"]}), "path"
        )
        assert (
            policy.matching(commands_mod.approval_request_view({"paths": ["cli/x.py"]}))
            is not None
        )
        assert (
            policy.matching(
                commands_mod.approval_request_view(
                    {"paths": ["cli/x.py", "config/settings.py"]}
                )
            )
            is None
        )

    def test_session_scope_stays_pinned_to_the_same_effect(self):
        policy = commands_mod.ApprovalPolicy()
        policy.record(commands_mod.approval_request_view({"paths": ["a"]}), "session")
        # The same exact effect later in the session is covered.
        assert (
            policy.matching(commands_mod.approval_request_view({"paths": ["a"]}))
            is not None
        )
        # A DIFFERENT effect is not — a session grant is not a blanket pass.
        assert (
            policy.matching(commands_mod.approval_request_view({"paths": ["b"]}))
            is None
        )

    def test_session_scope_does_not_leak_across_repositories(self):
        policy = commands_mod.ApprovalPolicy()
        policy.record(
            commands_mod.approval_request_view({"repo_path": "/repo/one"}), "session"
        )
        assert (
            policy.matching(
                commands_mod.approval_request_view({"repo_path": "/repo/two"})
            )
            is None
        )

    def test_once_scope_never_grants_a_second_request(self):
        policy = commands_mod.ApprovalPolicy()
        # "once" is consumed at the decision site, so it is never retained.
        assert policy.record(commands_mod.approval_request_view({}), "once") is None
        assert policy.matching(commands_mod.approval_request_view({})) is None

    def test_command_scope_grants_only_a_command_prefix(self):
        policy = commands_mod.ApprovalPolicy()
        policy.record(
            commands_mod.approval_request_view({"command": "pytest tests/"}), "command"
        )
        assert (
            policy.matching(
                commands_mod.approval_request_view(
                    {"command": "pytest tests/test_a.py"}
                )
            )
            is not None
        )
        assert (
            policy.matching(commands_mod.approval_request_view({"command": "rm -rf /"}))
            is None
        )

    def test_reject_is_never_recorded_as_a_grant(self):
        policy = commands_mod.ApprovalPolicy()
        view = commands_mod.approval_request_view({"paths": ["a"]})
        assert policy.record(view, "reject") is None
        assert policy.matching(view) is None

    def test_a_reset_session_forgets_every_grant(self):
        policy = commands_mod.ApprovalPolicy()
        policy.record(commands_mod.approval_request_view({"paths": ["a"]}), "path")
        policy.reset()
        assert (
            policy.matching(commands_mod.approval_request_view({"paths": ["a"]}))
            is None
        )

    def test_scope_words_normalize(self):
        assert commands_mod.normalize_approval_scope("P") == "path"
        assert commands_mod.normalize_approval_scope("session") == "session"
        assert commands_mod.normalize_approval_scope("nonsense") == "once"

    def test_typed_answers_map_to_decision_and_scope(self):
        assert commands_mod.approval_from_answer("s", scoped=True) == (True, "session")
        assert commands_mod.approval_from_answer("p", scoped=True) == (True, "path")
        assert commands_mod.approval_from_answer("n", scoped=True)[0] is False
        assert commands_mod.approval_from_answer("y") == (True, "once")

    @pytest.mark.parametrize("state", commands_mod.SURFACE_STATES)
    def test_every_command_resolves_deterministically_in_every_state(self, state):
        waiting = state == "waiting_for_approval"
        active = state in {"running", "waiting_for_approval", "resumed"}
        evidence = (
            [{"target_passed": True, "regression_passed": True, "flaky": False}]
            if state == "completed_verified"
            else []
        )
        snapshot = {
            "task_id": "agent-matrix",
            "status": state,
            "approval": "waiting" if waiting else "approved",
            "verification_evidence": evidence,
        }
        context = commands_mod.surface_command_context(
            "tui",
            in_flight=active,
            snapshot=snapshot,
            task_id="agent-matrix",
            pending_approval=waiting,
        )
        for spec in commands_mod.COMMAND_SPECS:
            args = "value" if spec.argument_policy == "required" else ""
            line = f"{spec.name} {args}".strip()
            resolution = commands_mod.resolve_command_line(line, context)
            assert resolution.spec is spec
            if (spec.idle_policy == "refuse" and not active) or (
                spec.in_flight_policy == "refuse" and active
            ):
                assert resolution.status == "disabled"
            elif spec.in_flight_policy == "queue" and active:
                assert resolution.status == "queued"
            else:
                assert resolution.status == "ok"


class TestHeadlessSurface:
    def test_help_lists_every_required_command(self):
        rendered = command_exec.headless_help()
        for name in REQUIRED:
            assert name in rendered

    def test_unknown_command_exits_with_usage_error(self):
        result = command_exec.run_command_line("/nope", log_root=Path("logs"))
        assert result.exit_code == EXIT_CODES["usage_error"]
        assert result.status == "unknown"

    def test_non_command_line_is_refused(self):
        result = command_exec.run_command_line("hello there", log_root=Path("logs"))
        assert result.exit_code == EXIT_CODES["usage_error"]

    def test_empty_line_is_refused(self):
        assert (
            command_exec.run_command_line("   ", log_root=Path("logs")).exit_code
            == EXIT_CODES["usage_error"]
        )

    def test_command_needing_an_interactive_session_is_refused(self):
        result = command_exec.run_command_line(
            "/plan do a thing", log_root=Path("logs")
        )
        assert result.exit_code == EXIT_CODES["usage_error"]
        assert "interactive session" in result.unavailable_reason
        assert result.recovery

    def test_flag_only_command_points_at_its_flag(self):
        result = command_exec.run_command_line("/status", log_root=Path("logs"))
        assert result.status == "flag"
        assert result.requires.startswith("neo status")
        assert result.exit_code == EXIT_CODES["usage_error"]

    def test_help_succeeds(self, tmp_path):
        result = command_exec.run_command_line("/help", log_root=tmp_path / "logs")
        assert result.ok is True
        assert "headless surface" in result.text

    def test_result_serializes_for_json_output(self, tmp_path):
        result = command_exec.run_command_line("/help", log_root=tmp_path / "logs")
        row = result.to_dict()
        assert row["command"] == "/help"
        assert row["exit_code"] == EXIT_CODES["success"]
        assert isinstance(row["recovery"], list)

    def test_mapped_read_only_command_runs_the_shared_handler(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / ".neo-sessions.jsonl").write_text("", encoding="utf-8")
        result = command_exec.run_command_line("/sessions", log_root=logs)
        assert result.ok is True
        assert "sessions" in result.text.lower()

    def test_headless_display_output_redacts_session_secrets(self, tmp_path):
        from cli import interactive

        logs = tmp_path / "logs"
        logs.mkdir()
        interactive.record_session(
            logs,
            "agent-secret",
            "rotate token=sk-abcdefghijklmnopqrstuvwx now",
            str(tmp_path),
            "success",
        )
        result = command_exec.run_command_line("/sessions", log_root=logs)
        assert result.ok is True
        assert "sk-abcdefghijklmnopqrstuvwx" not in result.text
        assert "[REDACTED_SECRET]" in result.text

    def test_semantic_handler_failure_is_not_reported_as_success(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        result = command_exec.run_command_line(
            "/approve missing-task",
            log_root=logs,
            repo=tmp_path,
            state={"active_task_id": "missing-task"},
        )
        assert result.status == "failed"
        assert result.exit_code != EXIT_CODES["success"]
        assert result.events[-1]["exit_code"] == result.exit_code

    def test_headless_table_covers_every_registered_command(self):
        assert set(commands_mod.HEADLESS_COMMAND_POLICIES) == {
            spec.name for spec in commands_mod.COMMAND_SPECS
        }

    def test_headless_table_only_names_real_commands(self):
        for name in commands_mod.HEADLESS_COMMAND_POLICIES:
            assert commands_mod.command_spec(name) is not None
        for name in commands_mod.HEADLESS_FLAG_EQUIVALENTS:
            assert commands_mod.headless_policy(name) == "flag-only"
            assert commands_mod.headless_equivalent(name)


class TestReplPreflight:
    def _state(self, tmp_path):
        return {"repo": str(tmp_path), "file_config": {}}

    def test_repl_refuses_a_command_that_needs_a_live_run(
        self, tmp_path, capsys, monkeypatch
    ):
        from cli import interactive

        monkeypatch.setattr(interactive, "_live_run", lambda: None)
        assert (
            interactive._slash_command(
                "/steer now",
                "/steer now",
                {},
                tmp_path / "logs",
                self._state(tmp_path),
            )
            is None
        )
        out = capsys.readouterr().out
        assert "no active run" in out
        assert "next:" in out

    def test_repl_rejects_unexpected_arguments_honestly(
        self, tmp_path, capsys, monkeypatch
    ):
        from cli import interactive

        monkeypatch.setattr(interactive, "_live_run", lambda: None)
        interactive._slash_command(
            "/quiet loud", "/quiet loud", {}, tmp_path / "logs", self._state(tmp_path)
        )
        assert "usage: /quiet" in capsys.readouterr().out

    def test_repl_still_serves_custom_commands(self, tmp_path, capsys, monkeypatch):
        from cli import interactive

        monkeypatch.setattr(interactive, "_live_run", lambda: None)
        handled = interactive._slash_command(
            "/no-such-template",
            "/no-such-template",
            {},
            tmp_path / "logs",
            self._state(tmp_path),
        )
        assert handled == "unknown"
        assert "/help" in capsys.readouterr().out

    def test_repl_help_mentions_the_new_commands(self, tmp_path, capsys, monkeypatch):
        from cli import interactive

        monkeypatch.setattr(interactive, "_live_run", lambda: None)
        interactive._slash_command(
            "/help", "/help", {}, tmp_path / "logs", self._state(tmp_path)
        )
        out = capsys.readouterr().out
        for name in ("/build", "/ask", "/theme", "/settings", "/plugins", "/quit"):
            assert name in out


class TestApprovalGateInFlagPath:
    def test_outcome_is_not_requested_without_a_gate(self, tmp_path):
        from cli.main import _approval_outcome

        state = _approval_outcome(tmp_path / "logs", "task-x")
        assert state["required"] is False
        assert state["decision"] == "not_requested"

    def test_rejected_gate_is_never_reported_as_approved(self, tmp_path):
        from cli.main import _approval_outcome

        gate = tmp_path / "logs" / "task-x.runtime" / "approval"
        gate.mkdir(parents=True)
        (gate / "review.log").write_text(
            '{"event": "requested"}\n{"event": "rejected"}\n', encoding="utf-8"
        )
        state = _approval_outcome(tmp_path / "logs", "task-x")
        assert state["decision"] == "rejected"
        assert state["required"] is True

    def test_timed_out_gate_is_never_reported_as_approved(self, tmp_path):
        from cli.main import _approval_outcome

        gate = tmp_path / "logs" / "task-y.runtime" / "approval"
        gate.mkdir(parents=True)
        (gate / "review.log").write_text(
            '{"event": "requested"}\n{"event": "timeout"}\n', encoding="utf-8"
        )
        assert _approval_outcome(tmp_path / "logs", "task-y")["decision"] == "timeout"

    def test_approved_gate_is_reported_from_the_gate_record(self, tmp_path):
        from cli.main import _approval_outcome

        gate = tmp_path / "logs" / "task-z.runtime" / "approval"
        gate.mkdir(parents=True)
        (gate / "review.log").write_text(
            '{"event": "requested"}\n{"event": "approved"}\n', encoding="utf-8"
        )
        assert _approval_outcome(tmp_path / "logs", "task-z")["decision"] == "approved"

    def test_malformed_review_log_degrades_to_pending(self, tmp_path):
        from cli.main import _approval_outcome

        gate = tmp_path / "logs" / "task-w.runtime" / "approval"
        gate.mkdir(parents=True)
        (gate / "review.log").write_text("not json\n{}\n", encoding="utf-8")
        state = _approval_outcome(tmp_path / "logs", "task-w")
        assert state["decision"] == "not_requested"


# ---------------------------------------------------------------------------
# Round 2 (2026-09-28). Three defects found by RUNNING the surfaces, not by
# reading the registry. Each test below failed on the tree this round started
# from; the reasons are recorded in cli/AGENTS.md.
# ---------------------------------------------------------------------------


class TestKeyboardShortcutsArePartOfTheCommandSystem:
    """Prompt 04 lists "keyboard shortcuts" beside the palette and the footer
    hints. Before this round they existed only as raw Textual `BINDINGS`, so
    nothing could tell a user that ctrl+g is `/steer`, and a rebind could not
    fail anything. The registry is now the single source of truth."""

    def test_shortcuts_are_declared_on_the_spec(self):
        steer = commands_mod.command_spec("/steer")
        cancel = commands_mod.command_spec("/cancel")
        quit_spec = commands_mod.command_spec("/quit")
        assert "ctrl+g" in steer.shortcuts
        assert "ctrl+b" in steer.shortcuts
        assert "ctrl+x" in cancel.shortcuts
        assert "ctrl+c" in cancel.shortcuts
        assert "ctrl+q" in quit_spec.shortcuts

    def test_an_unknown_key_is_rejected_at_construction(self):
        with pytest.raises(ValueError):
            commands_mod.CommandSpec(
                name="/nope",
                summary="x",
                aliases=(),
                argument_policy="none",
                idle_policy="allow",
                in_flight_policy="allow",
                palette_behavior="run",
                shortcuts=("ctrl+alt+f12",),
            )

    def test_every_declared_shortcut_is_a_closed_vocabulary_entry(self):
        allowed = commands_mod.SHORTCUT_KEYS
        declared = {
            key for spec in commands_mod.COMMAND_SPECS for key in spec.shortcuts
        }
        assert declared, "no command declares a shortcut at all"
        assert declared <= allowed

    def test_surface_shortcuts_cover_the_palette_and_history(self):
        keys = {row["key"] for row in commands_mod.keyboard_shortcuts()["surface"]}
        assert {"ctrl+p", "ctrl+r", "ctrl+y"} <= keys

    def test_the_palette_shows_the_key_next_to_the_command(self):
        entries = {
            entry["label"]: entry
            for entry in commands_mod.command_palette_entries()
            if entry["kind"] == "command"
        }
        assert "ctrl+g" in entries["/steer"]["shortcut"]
        assert "ctrl+g" in entries["/steer"]["hint"]

    def test_help_rows_carry_the_shortcut(self):
        from cli import interactive

        rows = {row.name: row for row in interactive.help_index()}
        assert "ctrl+g" in rows["/steer"].shortcut
        assert "ctrl+q" in rows["/quit"].shortcut
        # A command with no key says so rather than rendering a blank column.
        assert rows["/settings"].shortcut == ""

    def test_rendered_help_advertises_a_key(self):
        from cli import interactive

        assert "ctrl+g" in interactive.render_help("steer")

    def test_declared_shortcuts_are_really_bound_in_the_tui(self):
        """The drift gate. A declared key that nothing binds is a lie in the
        palette; an unbound TUI action is a key nobody can discover."""
        from cli.tui import NeoApp

        bound = {str(binding.key) for binding in NeoApp.BINDINGS}
        for row in commands_mod.keyboard_shortcuts()["commands"]:
            for key in row["keys"]:
                assert key in bound, f"{row['command']} declares unbound {key}"

    def test_every_tui_binding_has_a_declared_purpose(self):
        """The other half of the drift gate, with NO exemption list.

        An exemption list is just a place for the next unbound key to hide:
        the first version of this test exempted "navigational" keys inline
        and immediately missed a bound `shift+pageup`. Every key the TUI
        binds now has a declared row in one of the three groups.
        """
        from cli.tui import NeoApp

        table = commands_mod.keyboard_shortcuts()
        declared = {row["key"] for row in table["surface"]}
        declared |= {row["key"] for row in table["navigation"]}
        declared |= {key for row in table["commands"] for key in row["keys"]}
        for binding in NeoApp.BINDINGS:
            key = str(binding.key)
            assert key in declared, f"{key} is bound but nothing declares it"

    def test_every_declared_key_has_a_purpose_in_words(self):
        table = commands_mod.keyboard_shortcuts()
        rows = list(table["surface"]) + list(table["navigation"])
        rows += [
            {"purpose": purpose}
            for row in table["commands"]
            for purpose in row["purposes"]
        ]
        assert rows
        for row in rows:
            assert str(row.get("purpose") or "").strip(), (
                "a key with no stated purpose teaches nothing"
            )


class TestApprovalTimeoutIsReal:
    """Prompt 04 requires once / session / path-command / reject / TIMEOUT.
    Before this round the runtime gate never wrote its own deadline into the
    request, so every interactive surface read `timeout_s: None` and the TUI
    modal's bound fell back to 24 hours."""

    def test_the_gate_publishes_its_own_deadline(self, tmp_path):

        from runtime import approval as approval_mod

        gate = tmp_path / "gate"
        with pytest.raises(Exception):
            approval_mod.request_approval(
                gate_dir=str(gate),
                task_id="t-timeout",
                diff="--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\n",
                issue_text="why",
                timeout_s=0.4,
                poll_interval_s=0.05,
            )
        view = commands_mod.approval_request_view(
            approval_mod.pending_request(str(gate))
        )
        assert view.timeout_s == pytest.approx(0.4, abs=0.05)

    def test_a_request_without_a_deadline_still_normalizes_to_none(self, tmp_path):
        assert commands_mod.approval_request_view({}).timeout_s is None

    def test_the_gate_outcome_is_one_shared_authority(self, tmp_path):
        gate = tmp_path / "task-t.runtime" / "approval"
        gate.mkdir(parents=True)
        (gate / "review.log").write_text(
            '{"event": "requested"}\n{"event": "timeout"}\n', encoding="utf-8"
        )
        assert commands_mod.approval_gate_outcome(gate)["decision"] == "timeout"
        # The flag path delegates to the same function, so the two surfaces
        # cannot report one gate two different ways.
        from cli.main import _approval_outcome

        assert _approval_outcome(tmp_path, "task-t")["decision"] == "timeout"

    def test_a_missing_gate_reads_as_not_requested(self, tmp_path):
        state = commands_mod.approval_gate_outcome(tmp_path / "absent")
        assert state["requested"] is False
        assert state["decision"] == "not_requested"

    def test_an_expired_prompt_dismisses_its_modal(self, tmp_path):
        """A bounded wait that leaves the screen on top is a zombie modal: the
        user answers a question whose run already ended, and the next prompt
        stacks on top of it."""
        import asyncio

        from cli.tui import NeoApp, _ConfirmScreen, _PromptScreen

        app = NeoApp(log_root=tmp_path / "logs", repo=tmp_path)
        result = {}

        async def _drive():
            async with app.run_test():
                worker = threading.Thread(
                    target=lambda: result.update(
                        value=app._prompt_modal(
                            "approve this fix? [y/N] ", timeout_s=0.2
                        )
                    ),
                    daemon=True,
                )
                worker.start()
                opened = False
                settled = False
                # Never block the event loop: the modal can only be pushed and
                # popped BY the loop, so a `join()` here would deadlock the
                # very timeout this test is about.
                for _ in range(120):
                    await asyncio.sleep(0.05)
                    if not opened and isinstance(
                        app.screen, (_PromptScreen, _ConfirmScreen)
                    ):
                        opened = True
                    if opened and worker.is_alive() is False:
                        settled = True
                        break
                assert opened, "the approval modal never opened"
                assert settled, "the wait ignored its own deadline"
                assert result["value"] is None
                assert app._prompt_timed_out is True
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if not isinstance(app.screen, (_PromptScreen, _ConfirmScreen)):
                        break
                assert not isinstance(app.screen, (_PromptScreen, _ConfirmScreen)), (
                    "the expired modal was left on screen as a zombie"
                )

        asyncio.run(_drive())


class TestReplApprovalPromptShowsTheExactEffect:
    """Prompt 04: "Approval UX must show the exact path, command, server,
    diff, or side effect. Support once, session, path/command scope, reject,
    and timeout." The REPL gate showed a diff and a y/N and nothing else."""

    def _run(self, tmp_path, monkeypatch, capsys, answer, timeout_s=60.0):
        from cli import commands as cmds
        from cli import interactive

        # A process-scoped policy would otherwise leak one test's grant into
        # the next test's prompt. The product's own flag path has no session
        # object, so that fallback is real; the test just must not inherit it.
        cmds.reset_session_approval_policy()
        gate = tmp_path / "task-r.runtime" / "approval"
        gate.mkdir(parents=True)
        import json

        (gate / "request.json").write_text(
            json.dumps(
                {
                    "task_id": "task-r",
                    "request_id": "req-r",
                    "diff": "--- a/cli/x.py\n+++ b/cli/x.py\n@@\n-a\n+b\n",
                    "issue_text": "mathutil returns the sum",
                    "command": "pytest tests/test_x.py",
                    "server": "docs-mcp",
                    "timeout_s": timeout_s,
                }
            ),
            encoding="utf-8",
        )
        import builtins

        stop = threading.Event()
        prompts: List[str] = []

        def _fake_input(prompt=""):
            prompts.append(prompt)
            stop.set()  # one iteration is enough; the watcher polls after it
            return answer

        monkeypatch.setattr(builtins, "input", _fake_input)
        interactive.watch_for_approvals(tmp_path, ["task-r"], stop, poll_s=0.01)
        printed = capsys.readouterr().out
        # `input()`'s prompt IS what the user reads, but capsys cannot see it,
        # so the question is checked against what was actually asked.
        return printed + "\n" + "\n".join(prompts)

    def test_the_prompt_names_the_exact_effect(self, tmp_path, monkeypatch, capsys):
        out = self._run(tmp_path, monkeypatch, capsys, "y")
        # A console wraps, so compare on whitespace-normalized text: what the
        # user READS is not what fits on one terminal line.
        flat = " ".join(out.split())
        assert "cli/x.py" in flat
        assert "pytest tests/test_x.py" in flat
        assert "docs-mcp" in flat
        assert "side effect" in flat

    def test_the_prompt_advertises_the_scopes_and_the_deadline(
        self, tmp_path, monkeypatch, capsys
    ):
        out = self._run(tmp_path, monkeypatch, capsys, "y")
        flat = " ".join(out.split())
        for token in ("y=once", "s=session", "p=path", "c=command", "n=reject"):
            assert token in flat
        assert "60" in flat

    def test_a_scope_answer_is_accepted_and_recorded(
        self, tmp_path, monkeypatch, capsys
    ):
        from cli import commands as cmds

        self._run(tmp_path, monkeypatch, capsys, "p")
        # The grant is retained by the shared policy, not a REPL-local table.
        view = cmds.approval_request_view(
            {
                "task_id": "task-r",
                "diff": "--- a/cli/x.py\n+++ b/cli/x.py\n@@\n-a\n+b\n",
                "command": "pytest tests/test_x.py",
            }
        )
        assert cmds.session_approval_policy().matching(view) is not None
        cmds.reset_session_approval_policy()
        assert cmds.session_approval_policy().matching(view) is None, (
            "forgetting a session's grants must revoke them"
        )

    def test_a_decision_is_not_reported_as_an_applied_fix(
        self, tmp_path, monkeypatch, capsys
    ):
        """The REPL used to print "the run continues with the fix", which is a
        claim about a run the prompt cannot observe: the gate applies a diff
        only if the run's own verification passes."""
        out = self._run(tmp_path, monkeypatch, capsys, "y")
        assert "the run continues with the fix" not in out
        assert "verification" in out.lower()

    def test_a_decision_after_the_gate_expired_says_it_has_no_effect(
        self, tmp_path, monkeypatch, capsys
    ):
        gate = tmp_path / "task-e.runtime" / "approval"
        gate.mkdir(parents=True)
        import json

        (gate / "request.json").write_text(
            json.dumps({"task_id": "task-e", "request_id": "r", "diff": "x"}),
            encoding="utf-8",
        )
        (gate / "review.log").write_text(
            '{"event": "requested"}\n{"event": "timeout"}\n', encoding="utf-8"
        )
        import builtins

        from cli import interactive

        stop = threading.Event()
        monkeypatch.setattr(builtins, "input", lambda prompt="": stop.set() or "y")
        interactive.watch_for_approvals(tmp_path, ["task-e"], stop, poll_s=0.01)
        out = " ".join(capsys.readouterr().out.split())
        # The gate is settled, so nothing is asked and the user is told why.
        assert "no effect" in out or "expired" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
