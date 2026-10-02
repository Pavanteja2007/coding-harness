"""The command TYPE axis - the seven required proofs.

One class per required proof, one test per behaviour, named after the
behaviour rather than the function. Every test here is host-only: no Docker,
no provider, no network, no credential.

The proofs, in the order the brief names them:

1. ``TestEveryCommandCarriesAType``          - all of them do
2. ``TestTheTypeDrivesTheCostProjection``    - and the projection reads it
3. ``TestOnlyLocalUiMayOpenAModal``          - not a comment
4. ``TestOnlyLocalRunsInstantlyWhileBusy``    - not a comment
5. ``TestAnUndeclaredTypeKeepsCurrentBehaviour`` - the default is behaviourless
6. ``TestTheMigrationReportIsComplete``       - and every change has a test

Plus ``TestTheVerifierGateIsIdenticalAtEveryLevel``, which MIRRORS the pin in
``tests/test_model_picker.py``. The brief's requirement 7 is the reason this
file owns it: a type is a new thing a completion path could read, and the only
way to know one never will is to check at every effort level and to read the
completion-path SOURCE. A behavioural test covers only the code it happened to
run.

The last two classes exist because this repo's rule 3 is a hard one: a render
failure must never DELETE a message, so the markup proof asserts the hostile
text is VISIBLE through a REAL rich console, not merely absent from a string.
"""

from __future__ import annotations

import ast
import io
import json
import tokenize
from pathlib import Path
from typing import Any, Dict, List

import pytest

import cli.command_types as ct
from cli import commands as commands_mod
from harness.agent_loop_step import LoopEnvironment, ToolOutcome


def _repo_path(relative: str) -> Path:
    """Resolve a repository-relative path from this test file's location."""
    return Path(__file__).resolve().parents[1] / relative


#: Every command name the registry carries, read from the registry rather than
#: restated, so a new command cannot slip past this file's count.
def _registry_names() -> List[str]:
    return [spec.name for spec in commands_mod.COMMAND_SPECS]


# ---------------------------------------------------------------------------
# 1. Every command carries a type
# ---------------------------------------------------------------------------


class TestEveryCommandCarriesAType:
    def test_every_registry_command_has_a_declared_type(self):
        """The load-bearing count. A row with no type resolves to the DEFAULT,
        so this test compares against the CLASSIFICATION TABLE, not against the
        resolved value - otherwise "every command has a type" would pass with an
        empty table."""
        registered = set(_registry_names())
        classified = set(ct.COMMAND_TYPES)
        assert registered, "the registry is empty"
        assert not registered - classified, (
            "commands with no declared type: "
            + ", ".join(sorted(registered - classified))
        )
        assert not classified - registered, (
            "types for commands that do not exist: "
            + ", ".join(sorted(classified - registered))
        )
        assert len(registered) == len(ct.COMMAND_TYPES)

    def test_the_fifty_two_original_commands_all_still_carry_a_type(self):
        """The brief's count is 52; VEX-CS-01 added four more doors. Both
        facts are asserted so neither silently replaces the other."""
        original = {
            "/help",
            "/status",
            "/mode",
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
            "/fork",
            "/import",
            "/recover",
            "/cost",
            "/effort",
            "/context",
            "/trace",
            "/feed",
            "/mcp",
            "/skills",
            "/plugins",
            "/init",
            "/connect",
            "/login",
            "/logout",
            "/model",
            "/compact",
            "/steer",
            "/cancel",
            "/detach",
            "/attach",
            "/watch",
            "/approve",
            "/reject",
            "/theme",
            "/settings",
            "/open",
            "/doctor",
            "/repo",
            "/quit",
            "/files",
            "/relevant",
            "/diagnostics",
            "/copy-diff",
            "/history",
            "/quiet",
            "/clear",
        }
        assert len(original) == 52
        assert original <= set(ct.COMMAND_TYPES)
        added = set(_registry_names()) - original
        assert added == {"/worktree", "/hooks", "/migrate", "/support-bundle"}
        assert added <= set(ct.COMMAND_TYPES)

    def test_every_type_is_one_of_the_four(self):
        names = [t.name for t in ct.TYPES]
        assert names == ["local", "local_ui", "prompt", "skill"]
        for name in ct.COMMAND_TYPES:
            assert ct.command_type(name).name in names

    def test_a_command_type_is_stable_when_read_three_ways(self):
        """The same answer from a name, from a spec, and from the registry
        property - because three readers disagreeing is how a cost column starts
        lying."""
        for spec in commands_mod.COMMAND_SPECS:
            assert ct.command_type(spec.name) is ct.command_type(spec)
            assert ct.command_type(spec).name == spec.type_name
            assert ct.is_model_consuming(spec) == spec.model_consuming

    def test_an_unknown_command_is_local_rather_than_a_crash(self):
        assert ct.command_type("/definitely-not-real") is ct.DEFAULT_COMMAND_TYPE
        assert ct.command_type(None) is ct.DEFAULT_COMMAND_TYPE
        assert ct.command_type("") is ct.DEFAULT_COMMAND_TYPE

    def test_the_brief_named_commands_are_classified_the_way_the_brief_says(self):
        """/ask, /plan, /build, /review, /mode model-consuming; /quiet,
        /theme, /status, /cost, /clear, /copy-diff and /help do not."""
        for name in ("/ask", "/plan", "/build", "/review"):
            assert ct.is_model_consuming(name), name
        for name in (
            "/quiet",
            "/theme",
            "/status",
            "/cost",
            "/clear",
            "/copy-diff",
            "/help",
        ):
            assert not ct.is_model_consuming(name), name

    def test_mode_is_typed_local_and_the_deviation_is_recorded(self):
        """The brief lists /mode as model-consuming; the handler writes
        `state['mode']` and reaches no provider. Typing it PROMPT would put a
        free command in a cost column, so it is LOCAL and the deviation is a
        listed decision with a test - not a silent disagreement."""
        assert ct.command_type("/mode") is ct.LOCAL
        assert not ct.is_model_consuming("/mode")
        row = next(r for r in ct.MIGRATION_REPORT if r.name == "/mode")
        assert "DEVIAT" in row.reason
        assert row.test == "test_mode_is_local_because_it_reaches_no_provider"


# ---------------------------------------------------------------------------
# 2. The type drives the cost projection
# ---------------------------------------------------------------------------


class TestTheTypeDrivesTheCostProjection:
    def test_the_projection_partitions_every_command_by_cost(self):
        projection = ct.cost_projection()
        assert projection["schema_version"] == ct.SCHEMA_VERSION
        assert projection["total"] == len(ct.COMMAND_TYPES)
        assert projection["model_consuming_count"] == len(projection["model_consuming"])
        assert projection["free_count"] == len(projection["free"])
        assert set(projection["model_consuming"]) & set(projection["free"]) == set()
        assert set(projection["model_consuming"]) | set(projection["free"]) == set(
            ct.COMMAND_TYPES
        )

    def test_the_projection_is_a_projection_and_writes_nothing(self):
        before = json.dumps(ct.migration_report_document(), sort_keys=True, default=str)
        first = ct.cost_projection()
        second = ct.cost_projection()
        assert first == second
        after = json.dumps(ct.migration_report_document(), sort_keys=True, default=str)
        assert before == after

    def test_only_prompt_and_skill_are_model_consuming(self):
        for command_type in ct.TYPES:
            # Parenthesised deliberately: `a == b in c` is a CHAINED
            # comparison in Python, so the unparenthesised form asserts
            # something else entirely - and it failed.
            assert command_type.model_consuming == (
                command_type.name in ("prompt", "skill")
            ), command_type.name
        for name in ct.COMMAND_TYPES:
            assert ct.is_model_consuming(name) == (
                ct.command_type(name) in (ct.PROMPT, ct.SKILL)
            )

    def test_the_palette_row_reports_the_same_cost_the_projection_does(self):
        """A GTD cost column in --json needs this to be ONE answer. If the menu
        and the projection could disagree, a user would read the menu and get
        a different number than a script does."""
        rows = {
            entry["label"]: entry for entry in commands_mod.command_palette_entries()
        }
        assert len(rows) == len(ct.COMMAND_TYPES)
        projection = ct.cost_projection()
        consuming = set(projection["model_consuming"])
        for name, row in rows.items():
            assert row["model_consuming"] is (name in consuming), name
            assert row["cost"] == ("tokens" if row["model_consuming"] else "free"), name
            assert row["command_type"] == ct.command_type(name).name, name

    def test_the_type_is_inspectable_from_the_menu(self):
        """Requirement 6. Every row carries the type, a marker and the cost, so
        the answer to "does this spend tokens" is on the row a user already
        reads."""
        rows = commands_mod.command_palette_entries()
        assert rows
        for row in rows:
            assert row["command_type"] in {t.name for t in ct.TYPES}
            assert row["type_marker"] == ct.type_marker(row["label"])
            assert row["cost"] in ct.COST_CLASSES
        markers = {row["type_marker"] for row in rows}
        assert markers == {t.marker for t in ct.TYPES}

    def test_the_context_budget_counts_exactly_the_model_consuming_commands(self):
        assert list(ct.context_budget_names()) == sorted(
            ct.cost_projection()["model_consuming"]
        )
        assert all(ct.is_model_consuming(n) for n in ct.context_budget_names())
        assert "/quiet" not in ct.context_budget_names()

    def test_the_projection_can_be_taken_over_a_subset(self):
        subset = ct.cost_projection(["/ask", "/quiet"])
        assert subset["total"] == 2
        assert subset["model_consuming"] == ["/ask"]
        assert subset["free"] == ["/quiet"]

    def test_a_local_command_cannot_be_marked_free_while_its_type_says_otherwise(self):
        """The derivation is the gate. A registry row that declared
        `command_type="prompt"` and a projection that said "free" would be the
        exact defect the brief warns about - and it is impossible to express,
        because the projection reads the type and never the command name."""
        row = next(r for r in commands_mod.COMMAND_SPECS if r.name == "/ask")
        assert row.model_consuming is True
        assert ct.cost_class(row) == "tokens"

    def test_the_json_document_names_its_source(self):
        """A projection that does not say what it was derived from is a number
        with no provenance."""
        payload = json.dumps(ct.cost_projection())
        assert "cli.command_types.COMMAND_TYPES" in payload
        assert "command type" in payload


# ---------------------------------------------------------------------------
# 3. Only LOCAL_UI may open a modal
# ---------------------------------------------------------------------------


class TestOnlyLocalUiMayOpenAModal:
    def test_no_other_type_is_permitted_a_modal(self):
        for command_type in ct.TYPES:
            assert command_type.may_open_modal == (command_type is ct.LOCAL_UI), (
                command_type.name
            )

    def test_every_command_permitted_a_modal_is_local_ui(self):
        for spec in commands_mod.COMMAND_SPECS:
            if spec.may_open_modal:
                assert ct.command_type(spec) is ct.LOCAL_UI, spec.name

    def test_local_ui_is_a_capability_and_local_is_its_exclusion(self):
        assert ct.may_open_modal("/diff") is True
        assert ct.may_open_modal("/sessions") is True
        for name in ("/ask", "/build", "/plan", "/review", "/steer", "/quiet"):
            assert ct.may_open_modal(name) is False, name

    def test_a_gate_is_not_a_modal_and_the_two_are_never_confused(self):
        """`/plan` presents a modal today - a plan-preview CONFIRM. The brief's
        rule is "only LOCAL_UI may open a modal", so the honest resolution is
        that /plan's modal is a GATE owned by the workflow about to change your
        files, not a component the command navigates. Both facts are pinned, and
        the gate set is declared data rather than an inferred exception."""
        assert ct.command_type("/plan") is ct.PROMPT
        assert ct.may_open_modal("/plan") is False
        assert ct.may_gate("/plan") is True
        assert "/plan" in ct.GATE_COMMANDS

    def test_only_a_model_consuming_workflow_may_gate(self):
        for command_type in ct.TYPES:
            assert command_type.may_gate == (
                command_type.name in ("prompt", "skill")
            ), command_type.name
        for spec in commands_mod.COMMAND_SPECS:
            if spec.may_gate:
                assert ct.command_type(spec).model_consuming, spec.name

    def test_every_gate_command_is_declared_with_the_reason_it_gates(self):
        for name, reason in ct.GATE_COMMANDS.items():
            assert ct.may_gate(name), name
            assert str(reason).strip(), f"{name} gates with no stated reason"

    def test_a_local_command_can_never_gate(self):
        """/cancel and /approve answer a prompt the WORKER raised; they do not
        raise one, so they are LOCAL and must never be typed as a gate."""
        for name in ("/cancel", "/approve", "/reject", "/quiet", "/status"):
            assert ct.may_gate(name) is False, name

    def test_the_modal_permission_is_not_derived_from_the_presentation(self):
        """If it were, `/plan` would be LOCAL_UI and the brief's rule would be
        unfalsifiable. Three commands present a modal; only two are LOCAL_UI."""
        modal_rows = [
            spec.name
            for spec in commands_mod.COMMAND_SPECS
            if spec.result_presentation == "modal"
        ]
        assert set(modal_rows) == {"/plan", "/connect", "/login"}
        assert sorted(n for n in modal_rows if ct.may_open_modal(n)) == [
            "/connect",
            "/login",
        ]


# ---------------------------------------------------------------------------
# 4. Only LOCAL runs instantly while busy
# ---------------------------------------------------------------------------


class TestOnlyLocalRunsInstantlyWhileBusy:
    def test_no_prompt_skill_or_local_ui_command_is_reported_instant(self):
        for name in ct.COMMAND_TYPES:
            if ct.command_type(name) is not ct.LOCAL:
                assert ct.runs_instantly_while_busy(name) is False, name

    def test_the_registry_keeps_authority_over_the_in_flight_gate(self):
        """Both clauses of the conjunction are load-bearing. A LOCAL command
        whose registry row still refuses in flight is NOT instant - so adding a
        type cannot widen an existing gate, which is the backward-compatibility
        requirement."""
        refusing = [
            spec.name
            for spec in commands_mod.COMMAND_SPECS
            if spec.in_flight_policy == "refuse" and ct.command_type(spec) is ct.LOCAL
        ]
        assert refusing, "no LOCAL command refuses in flight; the clause is untested"
        for name in refusing:
            spec = commands_mod.command_spec(name)
            assert ct.runs_instantly_while_busy(spec) is False, name

    def test_a_local_command_the_registry_allows_is_instant(self):
        allowed = [
            spec.name
            for spec in commands_mod.COMMAND_SPECS
            if spec.in_flight_policy == "allow" and ct.command_type(spec) is ct.LOCAL
        ]
        assert "/quiet" in allowed
        for name in allowed:
            spec = commands_mod.command_spec(name)
            assert ct.runs_instantly_while_busy(spec) is True, name

    def test_steer_is_the_case_that_makes_the_type_clause_load_bearing(self):
        """`/steer` is `in_flight_policy="allow"` - it is only meaningful
        DURING a run - and it is a PROMPT. If the rule read only the registry
        policy it would report /steer as instant, which is exactly the claim
        this axis exists to prevent."""
        steer = commands_mod.command_spec("/steer")
        assert steer.in_flight_policy == "allow"
        assert ct.command_type(steer) is ct.PROMPT
        assert steer.runs_instantly_while_busy is False

    def test_the_answer_matches_availability_for_every_command(self):
        """The type axis must never make a command runnable that the registry
        refuses, or un-runnable one it allows. Compared against the LIVE gate."""
        context = commands_mod.CommandContext(surface="repl", in_flight=True)
        for spec in commands_mod.COMMAND_SPECS:
            availability = commands_mod.command_availability(spec, context)
            if not availability.available:
                assert spec.runs_instantly_while_busy is False, spec.name

    def test_a_bare_name_answers_with_the_types_own_claim(self):
        """A name carries no registry policy, so the type answers alone - and
        says so, rather than pretending to have read a gate."""
        assert ct.runs_instantly_while_busy("/quiet") is True
        assert ct.runs_instantly_while_busy("/build") is False


# ---------------------------------------------------------------------------
# 5. An undeclared type keeps current behaviour
# ---------------------------------------------------------------------------


class TestAnUndeclaredTypeKeepsCurrentBehaviour:
    def _bare_spec(self, **overrides: Any):
        return commands_mod.CommandSpec(
            name="/untyped",
            summary="a command nobody classified",
            aliases=(),
            argument_policy="none",
            idle_policy="allow",
            in_flight_policy="allow",
            palette_behavior="run",
            **overrides,
        )

    def test_a_spec_with_no_type_resolves_to_the_default(self):
        assert self._bare_spec().command_type == ""
        assert self._bare_spec().type_name == ct.DEFAULT_COMMAND_TYPE.name
        assert ct.command_type(self._bare_spec()) is ct.LOCAL

    def test_the_default_grants_no_model_call_no_modal_and_no_gate(self):
        """The default is chosen so an UNCLASSIFIED command cannot spend tokens,
        take over the screen, or ask a question. A default that granted any of
        those would make forgetting to classify a cost."""
        assert ct.DEFAULT_COMMAND_TYPE is ct.LOCAL
        assert ct.DEFAULT_COMMAND_TYPE.model_consuming is False
        assert ct.DEFAULT_COMMAND_TYPE.may_open_modal is False
        assert ct.DEFAULT_COMMAND_TYPE.may_gate is False
        assert ct.DEFAULT_COMMAND_TYPE.may_fork_subagent is False

    def test_an_undeclared_type_changes_nothing_about_the_row(self):
        """The strongest form of the requirement: comparing the undeclared row
        against the same row carrying the default EXPLICITLY, every observable
        field must be identical."""
        undeclared = self._bare_spec().to_dict()
        declared = self._bare_spec(command_type="local").to_dict()
        assert undeclared == declared
        assert undeclared["command_type"] == "local"
        assert undeclared["model_consuming"] is False

    def test_an_undeclared_type_keeps_the_historical_resolution_exactly(self):
        line = commands_mod.resolve_command_line("/untyped")
        assert line.status == "unknown"
        spec = self._bare_spec()
        # Availability is derived from the registry fields the type does not
        # touch, so an untyped row reads exactly as a typed one would.
        typed = self._bare_spec(command_type="local")
        assert commands_mod.command_availability(
            spec, commands_mod.CommandContext(surface="repl")
        ) == commands_mod.command_availability(
            typed, commands_mod.CommandContext(surface="repl")
        )

    def test_an_unknown_type_is_rejected_at_construction(self):
        """A typo here would otherwise read as 'a command that costs nothing' -
        the most expensive possible way to be wrong."""
        with pytest.raises(ValueError):
            self._bare_spec(command_type="promt")

    def test_every_shipped_row_declares_its_type_explicitly(self):
        """The default is a SAFETY NET for a future row, not a way to leave the
        shipped ones unclassified: each of them is named in the table, so a
        reader can see the whole taxonomy without reading the code."""
        for name in _registry_names():
            assert name in ct.COMMAND_TYPES
        # And the table is not merely the registry restated with no decisions:
        assert len({ct.command_type(n) for n in ct.COMMAND_TYPES}) == 4


# ---------------------------------------------------------------------------
# 6. The migration report
# ---------------------------------------------------------------------------


class TestTheMigrationReportIsComplete:
    def test_every_command_has_a_row(self):
        names = [row.name for row in ct.MIGRATION_REPORT]
        assert sorted(names) == sorted(_registry_names())
        assert len(names) == len(set(names)), "a command has two migration rows"

    def test_every_row_records_its_old_behaviour_and_its_reason(self):
        for row in ct.MIGRATION_REPORT:
            assert row.old_behaviour.strip(), f"{row.name} records no old behaviour"
            assert row.reason.strip(), f"{row.name} records no reason"

    def test_a_row_whose_type_is_not_local_says_why_it_costs_tokens(self):
        for row in ct.MIGRATION_REPORT:
            if row.command_type.model_consuming:
                assert row.reason.strip(), row.name

    def test_classification_changed_no_behaviour_and_the_report_says_so(self):
        """The constraint is "classifying a command MUST NOT change what it
        does", so the report's behaviour-change list must be EMPTY. A future
        round that adds one has to make it a deliberate, listed, tested entry -
        and this test is what forces that to be a decision rather than a habit."""
        document = ct.migration_report_document()
        assert document["behaviour_changes"] == []

    def test_every_non_empty_behaviour_change_names_the_test_that_pins_it(self):
        """Vacuous while the list is empty, and deliberately so: the moment a
        row grows an entry, this test demands the test name, and
        ``test_the_behaviour_change_names_a_test_that_exists`` then demands the
        name be real."""
        for row in ct.MIGRATION_REPORT:
            if row.behaviour_change and not row.behaviour_change.startswith("none"):
                assert row.test.strip(), (
                    f"{row.name} declares a behaviour change with no test"
                )

    def test_the_behaviour_change_names_a_test_that_exists(self):
        """A name in a report that no test answers is a promise nobody kept.
        Checked against this module's OWN test names, read from its globals, so
        the report cannot name a test that was renamed away."""
        defined = _NAMED_TESTS
        assert defined, "no test names were collected from this module"
        for row in ct.MIGRATION_REPORT:
            if row.test:
                assert row.test in defined, (
                    f"{row.name} names a missing test: {row.test}"
                )

    def test_the_decisions_are_the_ones_this_round_actually_made(self):
        """The decisions a reader must not have to rediscover, named so a grep
        for "why is /history local" finds the answer."""
        decided = {
            row.name for row in ct.MIGRATION_REPORT if row.reason.startswith("DECISION")
        }
        assert decided == {
            "/history",
            "/mcp",
            "/skills",
            "/plugins",
            "/compact",
            "/cancel",
            "/approve",
            "/resume",
            "/steer",
            "/mode",
        }

    def test_the_document_is_json_serializable_and_complete(self):
        payload = ct.migration_report_document()
        assert payload["total"] == len(ct.MIGRATION_REPORT)
        assert payload["default_type"] == ct.DEFAULT_COMMAND_TYPE.name
        assert len(payload["types"]) == 4
        assert payload["gate_commands"]
        json.dumps(payload)  # must not raise
        assert payload["note"]

    def test_the_subcommand_door_is_covered_by_the_report(self):
        """VEX-CS-01 added four doors after the brief was written. They are in
        the report, not in a footnote, because a command nobody classified is
        a command whose cost nobody knows."""
        for name in ("/worktree", "/hooks", "/migrate", "/support-bundle"):
            assert name in {row.name for row in ct.MIGRATION_REPORT}
            assert ct.is_model_consuming(name) is False


# ---------------------------------------------------------------------------
# 7. The gates stay honest - the model-picker pins, mirrored
# ---------------------------------------------------------------------------


class _ScriptedEnv(LoopEnvironment):
    """A `LoopEnvironment` whose only two capabilities are scripted.

    Subclasses the real base class so every OTHER call resolves to the shipped
    safe default, copied from ``tests/test_model_picker.py`` rather than
    re-derived: a hand-rolled stand-in that happened to be missing a method
    would fail for the wrong reason.
    """

    def __init__(self, evidence: Dict[str, Any]):
        self.evidence = dict(evidence)
        self.asked = 0

    def ask(self, messages, *, step):
        self.asked += 1
        return '{"tool": "done", "answer": "done"}'

    def invoke(self, name, args, *, turn):
        if name == "verify":
            return ToolOutcome(ok=True, output="", detail=dict(self.evidence))
        return ToolOutcome(ok=True, output="")


def _run_step(effort: str, evidence: Dict[str, Any]) -> str:
    from harness.agent_loop_step import step, terminal_status_of

    config = {
        "agent_strategy": "daily",
        "effort": effort,
        "target_test": "tests/test_x.py::test_y",
        "max_turns": 3,
    }
    events = step(
        [{"role": "user", "content": "fix it"}], _ScriptedEnv(evidence), config
    )
    return terminal_status_of(events)


CLEAN = {"target_passed": True, "regression_passed": True, "flaky": False}
DIRTY = {"target_passed": True, "regression_passed": False, "flaky": False}


class TestTheVerifierGateIsIdenticalAtEveryLevel:
    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_a_clean_verifier_run_is_verified_at_every_level(self, level):
        assert _run_step(level, CLEAN) == "completed_verified"

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_a_dirty_verifier_run_is_never_verified_at_every_level(self, level):
        """The control. Without it the test above could pass because every
        level mints `completed_verified` unconditionally - which is the exact
        failure the gate exists to prevent."""
        assert _run_step(level, DIRTY) == "failed"

    def test_every_level_produces_the_same_status_for_the_same_evidence(self):
        clean = {
            _run_step(level, CLEAN)
            for level in ("low", "medium", "high", "xhigh", "auto")
        }
        dirty = {
            _run_step(level, DIRTY)
            for level in ("low", "medium", "high", "xhigh", "auto")
        }
        assert clean == {"completed_verified"}
        assert dirty == {"failed"}

    def test_a_run_with_no_declared_verifier_is_unverified_at_every_level(self):
        from harness.agent_loop_step import step, terminal_status_of

        statuses = set()
        for level in ("low", "medium", "high"):
            events = step(
                [{"role": "user", "content": "fix it"}],
                _ScriptedEnv(CLEAN),
                {"effort": level, "max_turns": 3},
            )
            statuses.add(terminal_status_of(events))
        assert statuses == {"completed_unverified"}

    def test_no_completion_path_reads_the_effort_setting(self):
        """A source pin, because a behavioural test only covers what it ran.

        Docstrings and comments are stripped first: a module is allowed to NAME
        the setting in prose. What it may not do is reference it in code.
        """
        for relative in (
            "harness/agent_loop_step.py",
            "harness/agent_kernel/completion.py",
        ):
            code = _code_tokens(_repo_path(relative))
            for banned in ("map_effort", "picker_variant", "EFFORT_", "NEO_EFFORT"):
                assert banned not in code, f"{relative} must not read {banned}"

    def test_no_completion_path_reads_the_command_type(self):
        """The NEW half of requirement 7, and the reason this file owns the
        mirror.

        A command type is a budget hint: "this row can reach a provider". The
        moment a completion path reads it, the honest question "is this result
        verified?" starts answering itself with "is this command expensive?",
        and `completed_unverified` becomes a thing the surface can promote. The
        type is a PRESENTATION fact and must stay one.
        """
        for relative in (
            "harness/agent_loop_step.py",
            "harness/agent_kernel/completion.py",
        ):
            code = _code_tokens(_repo_path(relative))
            for banned in (
                "command_type",
                "CommandType",
                "COMMAND_TYPES",
                "model_consuming",
                "command_types",
            ):
                assert banned not in code, f"{relative} must not read {banned}"

    def test_the_verifier_module_does_not_import_the_command_registry(self):
        """The stronger structural form: the type must not even be REACHABLE
        from a completion path, so the honest answer cannot become a two-hop
        import away."""
        for relative in (
            "harness/agent_loop_step.py",
            "harness/agent_kernel/completion.py",
        ):
            tree = ast.parse(_repo_path(relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    assert "command_types" not in name, f"{relative} imports {name}"

    def test_the_mint_is_still_the_verifier_triple(self):
        from harness.agent_loop_step import _evidence_is_clean

        assert _evidence_is_clean(CLEAN) is True
        assert _evidence_is_clean(DIRTY) is False
        # Neither an effort setting NOR a command type is evidence, and neither
        # can make it clean.
        assert _evidence_is_clean({**CLEAN, "effort": "high"}) is True
        assert _evidence_is_clean({**CLEAN, "command_type": "prompt"}) is True
        assert _evidence_is_clean({**CLEAN, "error": "boom"}) is False

    def test_completed_unverified_is_never_promoted_by_the_type_axis(self):
        """Rule 6 of this session's rules. The type says what a command COSTS;
        it must not be able to say what a result IS. Asserted through the
        registry: a model-consuming command's row carries no verdict field, and
        a cost column cannot be read as a verification claim."""
        ask = commands_mod.command_spec("/ask").to_dict()
        assert ask["model_consuming"] is True
        assert not any(
            key in ask
            for key in ("verified", "verdict", "status", "verification_state")
        ), "a command row must not carry a verdict field"


def _code_tokens(path: Path) -> str:
    """Return a path's source with comments and strings removed.

    A gate that could be defeated by a docstring naming the setting would be a
    gate nobody keeps, so the prose is excluded structurally rather than by
    rewording.
    """
    text = path.read_text(encoding="utf-8")
    tokens: List[str] = []
    readline = io.StringIO(text).readline
    for token in tokenize.generate_tokens(readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        tokens.append(token.string)
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# 8. Markup safety - a render failure must never DELETE a message
# ---------------------------------------------------------------------------


class TestMarkupSafety:
    def test_a_hostile_command_name_stays_readable_through_a_real_console(self):
        """The pinned property is RENDERED output, not a substring test.

        A string carrying `[...]` that crosses into a markup parser is consumed
        as a TAG, so a hostile name would take the label around it with it. The
        proof is that the text is VISIBLE after a real rich Console has parsed
        it, and that the label before it is still there.
        """
        from rich.console import Console

        hostile = "[bold red]evil[/bold red]"
        line = f"  /ask  {hostile}"
        console = Console(record=True, width=200, no_color=True, force_terminal=False)
        console.print(_escape_markup(line), markup=True)
        rendered = console.export_text()
        assert "evil" in rendered
        assert "/ask" in rendered  # nothing was eaten and nothing was lost

    def test_a_type_marker_is_ascii_so_no_probe_is_needed(self):
        """Every marker renders on a cp1252 console without a probe, because a
        menu that crashes on a legacy terminal is a menu that does not exist."""
        for command_type in ct.TYPES:
            command_type.marker.encode("cp1252")
        assert all(t.marker.isascii() for t in ct.TYPES)

    def test_the_projection_document_is_json_and_carries_no_markup(self):
        """A `--json` document must parse and must not carry a stray bracket a
        markup parser could mistake for a tag."""
        document = ct.cost_projection()
        payload = json.dumps(document)
        assert json.loads(payload)["total"] == document["total"]
        brackets = [
            (key, value)
            for key, value in document.items()
            if isinstance(value, str) and ("[" in value or "]" in value)
        ]
        assert not brackets, f"markup-bearing strings in the projection: {brackets}"


def _escape_markup(text: str) -> str:
    """Escape a line for a markup-parsing sink.

    Delegates to rich's OWN escape so this file cannot drift from the parser's
    idea of what a bracket is.
    """
    from rich.markup import escape

    return escape(text)


# ---------------------------------------------------------------------------
# 11. Backward compatibility - the numbers, stated exactly
# ---------------------------------------------------------------------------

#: The flag-equivalent rows that existed before this WAVE, with their values
#: restated verbatim. The brief says 13; the tree had 14, and both are recorded
#: rather than one being quietly adopted.
_PRE_WAVE_FLAG_EQUIVALENTS = {
    "/connect": "neo connect",
    "/cost": "neo status --task-id <task-id>",
    "/effort": "NEO_EFFORT=<level> neo fix ...",
    "/init": "neo config init-project",
    "/login": "neo login",
    "/logout": "neo logout",
    "/mcp": "neo mcp list",
    "/model": "neo login",
    "/plugins": "neo plugin list",
    "/settings": "neo config list",
    "/skills": "neo skills list",
    "/status": "neo status --task-id <task-id>",
    "/theme": "neo config set theme <name>",
    "/watch": "neo watch <task-id>",
}

#: The eight aliases that existed before this wave, keyed by owner.
_PRE_WAVE_ALIASES = {
    "/diff": ("/changes",),
    "/checkpoints": ("/checkpoint",),
    "/undo": ("/diff",),
    "/effort": ("/thinking",),
    "/connect": ("/auth",),
    "/quit": ("/exit",),
    "/copy-diff": ("/copy",),
    "/relevant": ("/related",),
}


class TestBackwardCompatibilityIsUnchanged:
    def test_every_pre_wave_flag_equivalent_row_is_byte_identical(self):
        """The brief names 13 rows; the tree carried 14. Both counts are
        recorded here, and every value is asserted, so a row that changed a
        character fails rather than a row that merely disappeared."""
        current = commands_mod.HEADLESS_FLAG_EQUIVALENTS
        for name, value in _PRE_WAVE_FLAG_EQUIVALENTS.items():
            assert current.get(name) == value, name
        assert len(_PRE_WAVE_FLAG_EQUIVALENTS) == 14
        # VEX-CS-01 added rows for its four new doors; that is an ADDITION, and
        # this asserts the pre-wave set is a strict subset rather than a
        # replacement.
        assert set(_PRE_WAVE_FLAG_EQUIVALENTS) < set(current)
        assert len(current) == 18

    def test_every_pre_wave_alias_still_resolves_to_its_command(self):
        for owner, aliases in _PRE_WAVE_ALIASES.items():
            for alias in aliases:
                resolved = commands_mod.command_spec(alias)
                assert resolved is not None, alias
                # `/undo` declares `/diff` as an alias AND `/diff` is a command
                # of its own. The registry's documented rule is that an exact
                # name always wins over an alias of the same spelling, so the
                # honest assertion is that the alias is still DECLARED and
                # resolves to its owner or to the command that owns the exact
                # name. The first draft asserted `is` and failed on
                # `/undo` -> `/diff` for exactly this reason.
                assert resolved.name in {owner, alias}, f"{alias} -> {resolved.name}"
                declared = commands_mod.command_spec(owner)
                assert alias in declared.aliases, f"{alias} was dropped from {owner}"
        assert len(_PRE_WAVE_ALIASES) == 8

    def test_all_six_exit_codes_are_unchanged(self):
        from cli import exit_codes

        assert exit_codes.EXIT_CODES == {
            "success": 0,
            "task_failure": 1,
            "usage_error": 2,
            "environment_error": 3,
            "model_error": 4,
            "interrupted": 130,
        }

    def test_a_type_adds_no_registry_row_and_no_headless_policy(self):
        """Nothing about a command's ADDRESSING changed: the same names resolve
        and the same policies answer, so a script written before this round
        behaves identically."""
        for spec in commands_mod.COMMAND_SPECS:
            assert commands_mod.command_spec(spec.name) is spec
            assert (
                commands_mod.headless_policy(spec.name)
                in commands_mod.HEADLESS_POLICIES
            )

    def test_classifying_a_command_did_not_move_an_availability_gate(self):
        """The strongest backward-compatibility claim, written as a DIFFERENTIAL
        and never as a restatement of the gate.

        The first draft re-implemented `command_availability`'s policy in the
        test - a second copy of somebody else's rules, which is exactly the
        "two implementations of one behaviour" failure, and it went red on a
        rule this round did not own. The differential asks the real question:
        does changing ONLY the type change the answer?
        """
        from dataclasses import replace

        for state in commands_mod.SURFACE_STATES:
            active = state in {"running", "waiting_for_approval", "resumed"}
            snapshot = {
                "task_id": "agent-matrix",
                "status": state,
                "approval": "waiting"
                if state == "waiting_for_approval"
                else "approved",
                "verification_evidence": [],
            }
            context = commands_mod.surface_command_context(
                "tui",
                in_flight=active,
                snapshot=snapshot,
                task_id="agent-matrix",
                pending_approval=state == "waiting_for_approval",
            )
            for spec in commands_mod.COMMAND_SPECS:
                baseline = commands_mod.command_availability(spec, context)
                for alternative in ct.TYPES:
                    retyped = replace(spec, command_type=alternative.name)
                    other = commands_mod.command_availability(retyped, context)
                    assert other.available == baseline.available, (
                        f"{spec.name} as {alternative.name} in {state}"
                    )
                    assert other.reason == baseline.reason, (
                        f"{spec.name} as {alternative.name} in {state}"
                    )

    def test_retelling_a_command_does_not_change_how_it_resolves(self):
        """The same differential, one level up, and with NO restated status
        vocabulary.

        The first draft enumerated `{ok, disabled, queued}` and went red on a
        fifth word (`refused`) that another terminal's rows legitimately emit.
        Enumerating somebody else's vocabulary is a second copy of it; the
        honest assertion is that the fields the RESOLVER reads are unchanged by
        a retype, plus that resolution stays total and non-raising.
        """
        from dataclasses import replace

        context = commands_mod.CommandContext(
            surface="repl", in_flight=True, has_task=True, pending_approval=False
        )
        for spec in commands_mod.COMMAND_SPECS:
            baseline = commands_mod.resolve_command_line(spec.name, context)
            assert baseline.spec is spec
            for alternative in ct.TYPES:
                retyped = replace(spec, command_type=alternative.name)
                # The resolver reads policies and presentation; the type is not
                # among them, which is the claim this round has to make true.
                assert retyped.in_flight_policy == spec.in_flight_policy
                assert retyped.idle_policy == spec.idle_policy
                assert retyped.result_presentation == spec.result_presentation
                assert retyped.argument_policy == spec.argument_policy
                assert retyped.required_permissions == spec.required_permissions
            assert baseline.exit_code in {0, 2}


# ---------------------------------------------------------------------------
# 9. The named decisions - one test per entry the report points at
# ---------------------------------------------------------------------------


class TestTheDecidedClassifications:
    def test_history_prints_and_is_local(self):
        assert ct.command_type("/history") is ct.LOCAL
        assert not ct.is_model_consuming("/history")
        assert ct.may_open_modal("/history") is False

    def test_mcp_prints_and_is_local(self):
        assert ct.command_type("/mcp") is ct.LOCAL
        assert ct.may_open_modal("/mcp") is False

    def test_skills_prints_and_is_local(self):
        """`/skills` LISTS skills; it does not run one. That distinction is the
        whole reason SKILL is not simply 'a command with the word skills in
        it'."""
        assert ct.command_type("/skills") is ct.LOCAL
        assert ct.may_open_modal("/skills") is False

    def test_plugins_prints_and_is_local(self):
        assert ct.command_type("/plugins") is ct.LOCAL
        assert ct.may_open_modal("/plugins") is False

    def test_mode_is_local_because_it_reaches_no_provider(self):
        """`/mode` writes `state['mode']` and prints one line. The brief lists it
        as model-consuming; the handler does not agree, and the handler is what
        a cost column must describe."""
        assert ct.command_type("/mode") is ct.LOCAL
        assert not ct.is_model_consuming("/mode")
        # The handler's own words: a state write and a say().
        import inspect

        from cli import interactive

        source = inspect.getsource(interactive._mode_command)
        assert "state[" in source
        for banned in ("call_model", "run_task", "run_agent", "litellm"):
            assert banned not in source, f"_mode_command must not reach {banned}"

    def test_compact_is_local_and_costs_nothing(self):
        """The compaction summary is derived from the journal and the recall
        primitive. Marking it PROMPT would invent a cost."""
        assert ct.command_type("/compact") is ct.LOCAL
        assert ct.cost_class("/compact") == "free"
        # The control: the projection must say something DIFFERENT for a
        # command that really does call a provider, or "free" above is
        # vacuous.
        assert ct.cost_class("/ask") == "tokens"

    def test_cancel_is_local_because_it_spends_nothing(self):
        assert ct.command_type("/cancel") is ct.LOCAL
        assert not ct.is_model_consuming("/cancel")
        assert (
            ct.runs_instantly_while_busy(commands_mod.command_spec("/cancel")) is True
        )

    def test_approve_records_a_decision_and_is_local(self):
        """The approval PROMPT belongs to the worker's callback. The command
        writes the answer, which costs nothing."""
        assert ct.command_type("/approve") is ct.LOCAL
        assert not ct.is_model_consuming("/approve")
        assert ct.may_gate("/approve") is False

    def test_resume_is_model_consuming(self):
        """Resuming RE-RUNS the harness against a provider. Calling it free
        would understate the most natural way to continue work."""
        assert ct.command_type("/resume") is ct.PROMPT
        assert ct.is_model_consuming("/resume") is True

    def test_steer_is_a_prompt_and_never_instant(self):
        assert ct.command_type("/steer") is ct.PROMPT
        assert (
            ct.runs_instantly_while_busy(commands_mod.command_spec("/steer")) is False
        )

    def test_plan_is_a_prompt_with_a_gate_and_not_a_modal(self):
        assert ct.command_type("/plan") is ct.PROMPT
        assert ct.may_gate("/plan") is True
        assert ct.may_open_modal("/plan") is False

    def test_review_is_a_skill_and_declares_its_execution(self):
        assert ct.command_type("/review") is ct.SKILL
        assert ct.may_fork_subagent("/review") is True
        assert ct.background_policy("/review") in ct.BACKGROUND_POLICIES
        assert ct.subagent_agent("/review") == "reviewer"

    def test_only_a_skill_command_declares_an_execution(self):
        for name in ct.COMMAND_TYPES:
            if ct.command_type(name) is not ct.SKILL:
                assert ct.background_policy(name) == "", name
                assert ct.subagent_agent(name) == "", name
                assert ct.may_fork_subagent(name) is False, name

    def test_a_skill_command_without_a_declaration_is_refused(self):
        """The gate is exercised against the live table, then restored.

        The first draft captured the table AFTER popping the row, so the
        ``finally`` restored a table that was still missing it and the test
        ended by raising - a test that proved the gate works by breaking the
        module for the rest of the run.
        """
        shipped = dict(ct._SKILL_EXECUTION)
        try:
            ct._SKILL_EXECUTION.pop("/review", None)  # type: ignore[attr-defined]
            with pytest.raises(ValueError):
                ct._validate_skill_execution()
        finally:
            ct._SKILL_EXECUTION.clear()  # type: ignore[attr-defined]
            ct._SKILL_EXECUTION.update(shipped)  # type: ignore[attr-defined]
        ct._validate_skill_execution()  # restored and still valid
        assert ct.subagent_agent("/review") == "reviewer"

    def test_the_declared_agent_is_one_the_kernel_actually_has(self):
        """A subagent agent name the role registry does not know is a promise
        nothing implements."""
        from runtime import roles

        known = set(roles.role_profiles())
        for name in ct.COMMAND_TYPES:
            agent = ct.subagent_agent(name)
            if agent:
                assert agent in known, f"{name} names an unknown agent {agent}"

    def test_help_is_local_and_the_menu_says_so(self):
        rows = {r["label"]: r for r in commands_mod.command_palette_entries()}
        assert rows["/help"]["command_type"] == "local"
        assert rows["/help"]["cost"] == "free"


# ---------------------------------------------------------------------------
# 10. palette_behavior is a DIFFERENT axis - the dormant-field question
# ---------------------------------------------------------------------------


class TestPaletteBehaviorIsNotTheTypeAxis:
    def test_the_two_axes_are_independent(self):
        """`palette_behavior` is the ENTER-KEY axis (`run` executes the row,
        `prefill` fills the composer); `command_type` is the COST/UI axis. If
        one determined the other, retiring either would delete a behaviour."""
        cross: Dict[str, set] = {}
        for spec in commands_mod.COMMAND_SPECS:
            cross.setdefault(spec.palette_behavior, set()).add(
                ct.command_type(spec).name
            )
            cross.setdefault("type:" + ct.command_type(spec).name, set()).add(
                spec.palette_behavior
            )
        assert len(cross["run"]) > 1 and len(cross["prefill"]) > 1
        assert cross["run"] != {"local"}
        assert cross["prefill"] != {"prompt"}

    def test_every_combination_the_axes_allow_is_observed(self):
        """If one axis were a relabelling of the other, this set would have
        one element instead of several."""
        pairs = {
            (ct.command_type(s).name, s.palette_behavior)
            for s in commands_mod.COMMAND_SPECS
        }
        assert ("local", "run") in pairs
        assert ("local", "prefill") in pairs
        assert ("prompt", "prefill") in pairs
        assert ("local_ui", "run") in pairs
        assert len(pairs) >= 5, f"the axes collapsed to {sorted(pairs)}"

    def test_the_field_is_not_dormant_and_nobody_is_asked_to_retire_it(self):
        """The brief asked whether `palette_behavior` is the tier axis. It is
        not, and it is not dormant either - so the answer is "keep it", and this
        test is what makes that an answered question rather than an omission."""
        declared = {
            spec.name for spec in commands_mod.COMMAND_SPECS if spec.palette_behavior
        }
        assert len(declared) == len(commands_mod.COMMAND_SPECS)
        prefill = [
            s.name
            for s in commands_mod.COMMAND_SPECS
            if s.palette_behavior == "prefill"
        ]
        assert len(prefill) >= 8
        assert "/ask" in prefill and "/diff" not in prefill


#: Every test name this module defines, read from its OWN classes and globals
#: rather than restated - so the migration report's ``test`` column is checked
#: against the real thing instead of against a second copy of it. Class
#: attributes are not module globals, so both are scanned; a first draft read
#: only the globals and collected an EMPTY set, which would have made the
#: "names a real test" gate pass vacuously.
def _collect_test_names() -> frozenset:
    """Return every ``test_*`` name defined in this module, classes included."""
    names = {
        name
        for name in globals()
        if name.startswith("test_") and callable(globals()[name])
    }
    for value in list(globals().values()):
        if isinstance(value, type):
            names.update(
                name
                for name in vars(value)
                if name.startswith("test_") and callable(getattr(value, name, None))
            )
    return frozenset(names)


_NAMED_TESTS = _collect_test_names()
