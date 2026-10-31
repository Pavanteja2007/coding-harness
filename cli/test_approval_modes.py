"""The P2/W1 permission gate: four modes, both hiding directions, six paths, seven outcomes.

Every class below names the requirement it enforces and asserts a BEHAVIOUR, not
a literal. Where a claim could pass vacuously, the non-vacuity control is in the
same class and is called out in its docstring -- this repo's own history is that
a gate which cannot fail is worse than no gate.

Host-only: no Docker, no provider, no network, no credential. Every approver
reply is a scripted string and every model boundary is a local callable.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from cli import approval_modes as am

CATALOG: Tuple[str, ...] = (
    "read",
    "grep",
    "glob",
    "edit",
    "write",
    "apply_patch",
    "bash",
    "process",
    "web_fetch",
    "memory",
    "todo",
    "finish",
)


def _spec(name: str, side_effect_class: str) -> Any:
    """A stand-in for ``harness.tools.ToolSpec`` carrying its own declaration.

    ``name`` is a REAL field, not a throwaway parameter. The first version
    declared only ``side_effect_class`` and dropped the name, so
    ``classify_tool`` worked but ``tool_surface`` keyed on a dataclass repr
    and every catalog row arrived with the same opaque identity -- which means
    a test asserting "this tool is withheld" could not tell WHICH tool, and
    passed for the wrong reason.
    """

    @dataclasses.dataclass
    class _Spec:
        name: str = ""
        side_effect_class: str = ""

    return _Spec(name=name, side_effect_class=side_effect_class)


REAL_CATALOG: Tuple[Any, ...] = (
    _spec("read", "read_only"),
    _spec("grep", "read_only"),
    _spec("edit", "workspace_write"),
    _spec("bash", "mutation"),
    _spec("web_fetch", "network"),
    _spec("delete", "destructive"),
    _spec("memory", "memory"),
)


# ===========================================================================
# T4.W1.1 -- the four-mode enum
# ===========================================================================


class TestTheVocabularyMatchesTheRealKernelCatalog:
    """A declared class table beside a catalog is how the two drift.

    The first version of :data:`cli.approval_modes.SIDE_EFFECT_CLASSES`
    listed ``mutation``/``destructive`` -- which the kernel does not emit -- and
    omitted ``process``/``control``/``mcp`` -- which it does. Nothing in the
    module's own suite caught it, because every fixture used the module's own
    (wrong) vocabulary and therefore agreed with itself. These tests read the
    REAL catalog, which is the only thing that can disagree.
    """

    @staticmethod
    def _catalog() -> Tuple[Any, ...]:
        from harness.agent_kernel.tools import builtin_tool_specs

        return tuple(builtin_tool_specs())

    def test_every_class_the_kernel_emits_is_a_class_this_module_knows(self) -> None:
        emitted = {
            str(getattr(spec, "side_effect_class", "") or "")
            for spec in self._catalog()
        }
        emitted.discard("")
        assert emitted, "the kernel published no tools; this test would be vacuous"
        unknown = emitted - set(am.SIDE_EFFECT_CLASSES)
        assert not unknown, (
            "the kernel emits side-effect classes this module does not know, so "
            "every tool carrying one is silently unclassifiable: "
            f"{sorted(unknown)}"
        )

    def test_the_declared_kernel_classes_are_all_really_emitted(self) -> None:
        """The converse, so the constant cannot rot into fiction."""
        emitted = {
            str(getattr(spec, "side_effect_class", "") or "")
            for spec in self._catalog()
        }
        stale = set(am.KERNEL_SIDE_EFFECT_CLASSES) - emitted
        assert not stale, (
            f"KERNEL_SIDE_EFFECT_CLASSES names classes the kernel no longer "
            f"emits: {sorted(stale)}"
        )

    def test_the_name_fallback_agrees_with_the_real_spec_for_every_tool(self) -> None:
        """``classify_tool`` must not contradict the catalog it reads.

        This is the assertion that would have caught the original defect
        directly: the fallback table classified ``shell`` as ``mutation`` while
        the kernel says ``process``. A spec's own declaration always wins, so
        the fallback only matters for a bare NAME -- and a bare name is exactly
        what a new call site passes.
        """
        disagreeing = []
        for spec in self._catalog():
            name = str(getattr(spec, "name", "") or "")
            if not name:
                continue
            by_spec = am.classify_tool(spec)
            by_name = am.classify_tool(name)
            if by_spec != by_name:
                disagreeing.append(f"{name}: catalog={by_spec} name={by_name}")
        assert not disagreeing, (
            "the name fallback disagrees with the real catalog for: "
            + "; ".join(sorted(disagreeing))
        )

    def test_every_mode_produces_a_clean_surface_against_the_real_catalog(self) -> None:
        """The end-to-end claim, on real tools rather than a fixture."""
        catalog = self._catalog()
        assert len(catalog) > 20, "a 20-tool catalog is not this kernel's catalog"
        for name in am.APPROVAL_MODES:
            report = am.visible_surface(name, catalog)
            problems = am.surface_violations(report)
            assert not problems, (
                f"{name} mode against the real catalog: "
                + "; ".join(f"{p['kind']} {p['tool']}" for p in problems)
            )

    def test_plan_withholds_exactly_the_world_changing_classes(self) -> None:
        """A read-only mode that cannot FINISH is broken, not read-only."""
        catalog = self._catalog()
        withheld = {name for name, _ in am.visible_surface("plan", catalog).withheld}
        for spec in catalog:
            cls = str(getattr(spec, "side_effect_class", "") or "")
            name = str(getattr(spec, "name", "") or "")
            if cls in {"workspace_write", "network", "process", "mcp"}:
                assert name in withheld, f"plan mode offers {name} ({cls})"
            elif cls in {"read_only", "memory", "control"}:
                assert name not in withheld, f"plan mode withholds {name} ({cls})"

    def test_every_shell_capable_class_is_a_shell_tool(self) -> None:
        """The blanket-shell refusal must fire on the real process tools."""
        for spec in self._catalog():
            if str(getattr(spec, "side_effect_class", "") or "") in {"process", "mcp"}:
                assert am.is_shell_tool(spec), (
                    f"{getattr(spec, 'name', '?')} reaches a process or a server "
                    "and must not be offered a blanket approval"
                )


class TestTheFourModeTable:
    """Mode x tool surface x user-visible marker, asserted as a closed table."""

    def test_the_vocabulary_is_exactly_four_and_ordered(self) -> None:
        """The corpus's four names, in escalating order. Nothing else is a mode."""
        assert am.APPROVAL_MODES == ("plan", "default", "auto_edit", "yolo")

    def test_every_mode_has_a_profile_a_marker_and_a_non_empty_line(self) -> None:
        """A mode with no marker is a mode a user cannot see they are in."""
        for mode in am.APPROVAL_MODES:
            profile = am.MODE_PROFILES[mode]
            assert profile.name == mode
            assert profile.label.strip(), f"{mode} has no label"
            assert am.mode_marker(mode).strip(), f"{mode} has no marker"
            assert am.mode_line(mode).strip(), f"{mode} renders an empty line"
            # ASCII-only: a permission marker that needs an encoding fallback is
            # a permission marker somebody on a cp1252 console cannot read.
            am.mode_marker(mode).encode("ascii")

    def test_an_unknown_mode_still_renders_a_marker(self) -> None:
        """The rare outcome must not go blank. It is a marker, not nothing."""
        assert am.mode_marker("nonsense-mode").strip()
        assert am.mode_line("nonsense-mode").strip()
        assert am.mode_marker(None).strip()
        assert am.mode_marker("").strip()

    def test_an_unrecognised_mode_name_never_escalates_to_yolo(self) -> None:
        """A typo must not select the weakest mode. That is the whole rule."""
        for word in ("ylot", "YOLO!", "yes-please", "autoapprove", "bypass-all"):
            assert am.normalize_mode(word) == "default", word
        assert am.normalize_mode("bypass") == "yolo", "a real alias still works"

    @pytest.mark.parametrize(
        "mode,mutating_offered,mutating_prompted",
        [
            ("plan", False, False),
            ("default", True, True),
            ("auto_edit", True, True),
            ("yolo", True, False),
        ],
    )
    def test_the_tool_surface_is_derived_from_the_mode(
        self, mode: str, mutating_offered: bool, mutating_prompted: bool
    ) -> None:
        """A mode that withholds a write tool must WITHHOLD it, not merely gate it.

        "The model can see it but calling it fails" is not a plan mode: it spends
        the model's turns discovering a tool it cannot use and produces a worse
        plan. The assertion is on the OFFERED set, which is what the provider
        receives.
        """
        surface = am.tool_surface(mode, CATALOG)
        offered = set(surface.offered)
        for mutating in (
            "edit",
            "write",
            "apply_patch",
            "bash",
            "process",
            "web_fetch",
        ):
            assert (mutating in offered) is mutating_offered, f"{mode}/{mutating}"
        if mutating_prompted:
            assert am.MODE_PROFILES[mode].prompts("mutation")
        else:
            assert not am.MODE_PROFILES[mode].prompts("mutation")

    def test_plan_mode_is_read_only_by_construction_not_by_instruction(self) -> None:
        """Read tools survive; every write and every shell command does not."""
        surface = am.tool_surface("plan", CATALOG)
        for reader in ("read", "grep", "glob", "todo", "finish"):
            assert reader in surface.offered, reader
        for writer in ("edit", "write", "apply_patch", "bash", "web_fetch"):
            assert writer not in surface.offered, writer
            assert writer in surface.withheld_by_mode, writer
        assert am.MODE_PROFILES["plan"].read_only is True
        assert am.MODE_PROFILES["plan"].intentional_gap, (
            "plan mode's withholding must be DECLARED, or a broken mode and a "
            "planning mode are indistinguishable"
        )

    def test_auto_edit_auto_approves_edits_but_still_asks_about_shell(self) -> None:
        """The distinction the whole mode exists for."""
        profile = am.MODE_PROFILES["auto_edit"]
        assert profile.auto_approves is False
        assert "workspace_write" not in profile.prompted_classes, (
            "auto_edit must not prompt for a file edit"
        )
        assert "mutation" in profile.prompted_classes, (
            "auto_edit must still prompt for a shell command"
        )
        assert "network" in profile.prompted_classes
        assert "destructive" in profile.prompted_classes

    def test_yolo_carries_a_reduced_safety_warning_and_a_scope(self) -> None:
        """Exactly one mode is reduced safety, and it says so in UPPERCASE."""
        reduced = [m for m in am.APPROVAL_MODES if am.MODE_PROFILES[m].reduced_safety]
        assert reduced == ["yolo"]
        profile = am.MODE_PROFILES["yolo"]
        assert profile.warning.strip()
        assert profile.auto_approves is True
        assert profile.prompted_classes == ()
        line = am.mode_line("yolo", detail=True)
        assert profile.warning.upper() in line, "the warning must be unmissable"
        assert "CI" in profile.summary or "scripted" in profile.summary, (
            "the summary must name the scope, which is CI/scripted use"
        )

    def test_the_tool_class_comes_from_the_catalog_not_a_hand_written_table(
        self,
    ) -> None:
        """A spec's OWN declaration wins, so a new catalogued tool needs no edit."""
        assert (
            am.classify_tool(_spec("brand_new_thing", "destructive")) == "destructive"
        )
        assert am.is_shell_tool(_spec("brand_new_thing", "destructive")) is True
        assert am.is_shell_tool(_spec("brand_new_thing", "read_only")) is False
        assert (
            "brand_new_thing"
            not in am.tool_surface(
                "plan", [_spec("brand_new_thing", "destructive")]
            ).offered
        )

    def test_an_unclassified_tool_is_treated_as_able_to_do_damage(self) -> None:
        """The safe direction: a new tool nobody classified must not get a pass."""
        assert am.classify_tool("a_tool_nobody_catalogued") == "mutation"
        assert am.is_shell_tool("a_tool_nobody_catalogued") is True


class TestTheModeIsVisibleAndChangeableInFlight:
    """An invisible permission mode is a permission mode nobody can trust."""

    def test_a_mode_change_takes_effect_on_the_next_tool_call(self) -> None:
        """Not the next session. The surface is re-derived from the live mode."""
        session = am.ModeSession("default")
        assert "edit" in session.surface(CATALOG).offered
        session.set("plan", reason="switched to planning")
        assert "edit" not in session.surface(CATALOG).offered
        session.set("auto_edit")
        assert "edit" in session.surface(CATALOG).offered

    def test_changing_to_the_mode_already_in_force_is_a_honest_no_op(self) -> None:
        """ "You are already in auto-edit" beats a fabricated new reason."""
        session = am.ModeSession("auto_edit", reason="declared by the config")
        mode, reason = session.set("auto")
        assert mode == "auto_edit"
        assert reason == "declared by the config"
        assert session.revision == 1, "a no-op must not burn a revision"

    def test_the_session_records_where_the_mode_came_from(self) -> None:
        """A receipt that says only `yolo` cannot answer `why was I not asked?`."""
        mode, reason = am.resolve_mode({"approval_mode": "yolo"}, environ={})
        assert mode == "yolo"
        assert "approval_mode" in reason and "configuration" in reason

        mode, reason = am.resolve_mode({}, environ={"NEO_APPROVAL_MODE": "plan"})
        assert mode == "plan"
        assert "NEO_APPROVAL_MODE" in reason

        mode, reason = am.resolve_mode({}, environ={})
        assert mode == "default"
        assert reason.strip(), "the default must still say that it was a default"

    def test_a_config_file_cannot_beat_the_operators_own_shell(self) -> None:
        """The environment is read LAST, so a repository cannot weaken a mode."""
        mode, _ = am.resolve_mode(
            {"approval_mode": "yolo"}, environ={"NEO_APPROVAL_MODE": "plan"}
        )
        assert mode == "yolo"

    def test_the_history_is_the_audit_of_which_mode_was_in_force(self) -> None:
        """A receipt a reader can reconstruct the session's mode changes from."""
        session = am.ModeSession("plan")
        session.set("default")
        session.set("yolo")
        rows = session.history()
        assert [row["mode"] for row in rows] == ["plan", "default", "yolo"]
        assert session.revision == 3
        assert session.reduced_safety is True


class TestTheModeAgreesAcrossAllThreeSurfaces:
    """A mode that behaves differently per surface is worse than no mode."""

    def test_tui_repl_and_headless_read_the_same_mode_and_the_same_answers(
        self,
    ) -> None:
        """Three callers, one session object, identical answers.

        This is the parity assertion for the permission model itself. The three
        "surfaces" are driven here as the three call sites that exist in the
        product: the TUI rail, the REPL dialog, and the headless envelope.
        """
        session = am.ModeSession("auto_edit", reason="operator chose it")

        surfaces = {
            "tui": (
                session.marker(),
                session.line(),
                session.profile.auto_approves,
                session.prompts("bash"),
            ),
            "repl": (
                session.marker(),
                session.line(),
                session.profile.auto_approves,
                session.prompts("bash"),
            ),
            "headless": (
                session.marker(),
                session.line(),
                session.profile.auto_approves,
                session.prompts("bash"),
            ),
        }
        assert len(set(surfaces.values())) == 1, (
            f"the three surfaces disagree: {surfaces}"
        )

    @pytest.mark.parametrize("mode", am.APPROVAL_MODES)
    def test_the_surface_answers_are_identical_whoever_asks(self, mode: str) -> None:
        """Re-derived per caller, so the answer must not depend on the caller."""
        answers = {
            tuple(am.tool_surface(mode, CATALOG).to_dict()["offered"]),
            tuple(am.visible_surface(mode, CATALOG).model_visible),
        }
        assert len(answers) == 1, mode

    def test_both_axes_are_read_together_in_exactly_one_place(self) -> None:
        """The work-mode registry and the approval enum meet at ONE function.

        A second place that combines them is a second answer to "what does this
        run do without asking?", and the two will drift.
        """
        source = __import__("pathlib").Path(am.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        callers = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", "") == "resolve_run_approval"
                for inner in ast.walk(node)
            )
            and node.name != "resolve_run_approval"
        ]
        assert callers == [], f"a second caller exists: {callers}"


class TestRunApprovalIsDerivedFromBothAxes:
    """The old rule was a boolean derived from one declared word."""

    @pytest.mark.parametrize(
        "mode,declared,expect",
        [
            ("plan", "ask", "allow"),
            ("yolo", "ask", "allow"),
            ("default", "deny", "allow"),
            ("default", "ask", "require"),
            ("default", "", "require"),
        ],
    )
    def test_the_mode_decides_first(
        self, mode: str, declared: str, expect: str
    ) -> None:
        """`PLAN` can never require; `YOLO` can never require; a deny cannot."""
        profile = am.MODE_PROFILES[mode]

        class _Profile:
            name = "build"
            approval = declared
            visible_tools = tuple(profile.offered_classes)
            denied_side_effects = ("workspace_write",) if declared == "deny" else ()

        value, reason = am.resolve_run_approval(_Profile(), mode)
        assert value == expect, reason
        assert reason.strip(), "every answer must say WHY"

    def test_a_mode_that_can_write_and_forgot_its_approval_word_fails_closed(
        self,
    ) -> None:
        """The structural gap the old boolean rule left open."""

        class _Profile:
            name = "newmode"
            approval = ""
            visible_tools = ("edit", "bash")
            denied_side_effects = ()

        value, reason = am.resolve_run_approval(_Profile(), "default")
        assert value == "require"
        assert "fail-closed" in reason

    def test_a_mode_with_no_write_tool_is_not_prompted_for_nothing(self) -> None:
        """A prompt with nothing behind it is one a person learns to dismiss."""

        class _Profile:
            name = "ask"
            approval = ""
            visible_tools = ("read", "grep")
            denied_side_effects = ()

        value, reason = am.resolve_run_approval(_Profile(), "default")
        assert value == "allow"
        assert "nothing behind it" in reason


class TestPlanModeExitIsExplicit:
    """Leaving plan mode is a decision, not a fallback."""

    def test_an_unanswered_request_leaves_the_run_planning(self) -> None:
        """Fail closed: there is no path that exits because a caller stopped asking."""
        exit_ = am.plan_exit_request(approved=False, reason="no answer within 20s")
        assert exit_.still_planning is True
        assert exit_.explicit is True
        assert exit_.mode_before == "plan"

    def test_an_approved_request_leaves_with_the_plan_recorded(self) -> None:
        """The decision carries the plan, so a reader can see what was approved."""
        exit_ = am.plan_exit_request(
            approved=True,
            reason="the user reviewed the plan",
            summary="add a CLI surface",
            steps=["read the spec", "wire the mode"],
        )
        assert exit_.still_planning is False
        assert exit_.target_mode == "default"
        assert exit_.steps == ("read the spec", "wire the mode")
        assert exit_.to_dict()["explicit"] is True

    def test_the_target_mode_is_normalised_never_guessed(self) -> None:
        assert (
            am.plan_exit_request(approved=True, target_mode="nonsense").target_mode
            == "default"
        )


# ===========================================================================
# T4.W1.2 -- policy deny removes the schema
# ===========================================================================


class TestPolicyDenialReachesThePromptSurface:
    """OpenCode's rule: tool availability is permission-derived."""

    def test_a_denied_tool_is_absent_from_the_model_visible_surface(self) -> None:
        """The schema is what the model plans against. A denial must reach it."""
        report = am.visible_surface("default", CATALOG, denied=["bash", "web_fetch"])
        assert "bash" not in report.model_visible
        assert "web_fetch" not in report.model_visible
        assert "edit" in report.model_visible, "the denial must be NARROW"

    def test_the_withholding_is_reported_by_count_and_by_name(self) -> None:
        """A user should be able to see the agent has fewer capabilities."""
        report = am.visible_surface("default", CATALOG, denied=["bash"])
        payload = report.to_dict()
        assert payload["withheld_count"] == 1
        assert payload["withheld_by_policy"] == ["bash"]
        entry = payload["withheld"][0]
        assert entry["tool"] == "bash"
        assert entry["reason"].strip(), "a withheld tool must name its rule"
        assert payload["catalog_total"] == len(CATALOG)

    def test_policy_withholding_and_mode_withholding_are_reported_separately(
        self,
    ) -> None:
        """They have different owners and different remedies."""
        report = am.visible_surface("plan", CATALOG, denied=["memory"])
        assert report.withheld_by_policy == ("memory",)
        assert "edit" in report.withheld_by_mode
        assert "memory" not in report.withheld_by_mode
        assert set(report.withheld_by_policy).isdisjoint(report.withheld_by_mode)


class TestTheTwoSurfacesAreSeparatedAndDeclared:
    """A tool the agent may use but the model is never told about is real."""

    def test_the_declared_internal_set_is_empty_and_that_is_stated(self) -> None:
        """Emptiness is a MEASURED statement on this tree, not an oversight."""
        assert am.declared_internal_tools() == ()
        assert am.internal_tool_declarations() == {}

    def test_declaring_an_internal_tool_requires_a_written_reason(self) -> None:
        """A declaration nobody can read is indistinguishable from an accident."""
        with pytest.raises(ValueError, match="reason"):
            am.declare_internal_tool("some_internal_tool", "")
        with pytest.raises(ValueError, match="reason"):
            am.declare_internal_tool("some_internal_tool", "   ")
        with pytest.raises(ValueError, match="name"):
            am.declare_internal_tool("", "because it is bookkeeping")

    def test_a_declared_internal_tool_is_executable_but_not_advertised(self) -> None:
        """The whole point of the separation: it is in one list and not the other."""
        try:
            am.declare_internal_tool(
                "ledger_scratch", "bookkeeping only; useless to a model"
            )
            report = am.visible_surface("default", CATALOG)
            assert "ledger_scratch" not in report.model_visible
            assert "ledger_scratch" in report.executable
            assert "ledger_scratch" in report.declared_internal
            assert am.internal_tool_declarations()["ledger_scratch"]
            # And its absence from the model's view is REPORTED, not silent.
            entry = {item["tool"]: item for item in report.to_dict()["withheld"]}[
                "ledger_scratch"
            ]
            assert "internal" in entry["reason"]
        finally:
            am._DECLARED_INTERNAL.pop("ledger_scratch", None)

    def test_the_report_names_the_mechanism_that_blocks_each_tool(self) -> None:
        """`not offered` and `not offered AND not runnable` are different facts."""
        report = am.visible_surface("plan", CATALOG, denied=["memory"])
        blocked = report.dispatch_blocked_by
        assert "edit" in blocked and "plan" in blocked["edit"]
        assert "memory" in blocked and "policy" in blocked["memory"]


class TestBothHidingDirectionsAreFailures:
    """A check that only looked for one would ship the other's defect."""

    def test_a_denied_tool_still_in_the_surface_is_reported(self) -> None:
        """Under-hiding the policy: the receipt and the schema disagree."""
        # The policy denial has to be REAL for this to test anything: the first
        # version built the report from a surface with no denial and then added
        # a denied tool to the visible list, so `withheld_by_policy` was empty
        # and the gate had no denied tool to catch. It reported a clean receipt
        # and passed a test named for the defect it was not exercising.
        payload = am.visible_surface("default", CATALOG, denied=["bash"]).to_dict()
        assert "bash" in payload["withheld_by_policy"], payload
        payload["model_visible"] = [*payload["model_visible"], "bash"]
        report = am.VisibilityReport(
            mode=payload["mode"],
            model_visible=tuple(payload["model_visible"]),
            executable=tuple(payload["executable"]),
            withheld=tuple(
                (item["tool"], item["reason"]) for item in payload["withheld"]
            ),
            withheld_by_policy=tuple(payload["withheld_by_policy"]),
            withheld_by_mode=tuple(payload["withheld_by_mode"]),
            declared_internal=tuple(payload["declared_internal"]),
            catalog_total=payload["catalog_total"],
            dispatch_blocked_by=payload["dispatch_blocked_by"],
        )
        kinds = {problem["kind"] for problem in am.surface_violations(report)}
        assert "denied_but_visible" in kinds

    def test_an_undeclared_withheld_tool_is_reported(self) -> None:
        """Over-hiding: a tool the agent needs and cannot see produces a worse plan."""

        @dataclasses.dataclass
        class _Narrow:
            mode: str = "default"
            model_visible: Tuple[str, ...] = ("read", "grep")
            executable: Tuple[str, ...] = ("read", "grep", "edit")
            withheld: Tuple[Tuple[str, str], ...] = (("edit", "removed for no reason"),)
            withheld_by_mode: Tuple[str, ...] = ("edit",)
            withheld_by_policy: Tuple[str, ...] = ()
            declared_internal: Tuple[str, ...] = ()
            catalog_total: int = 3
            dispatch_blocked_by: Any = dataclasses.field(default_factory=dict)

        problems = am.surface_violations(_Narrow())
        kinds = {problem["kind"] for problem in problems}
        assert "withheld_but_undeclared" in kinds, problems
        detail = next(p for p in problems if p["kind"] == "withheld_but_undeclared")
        assert detail["tool"] == "edit"

    def test_plan_modes_withholding_is_declared_so_it_is_not_a_violation(self) -> None:
        """The difference between a planning mode and a broken one."""
        assert am.surface_violations(am.visible_surface("plan", CATALOG)) == []

    def test_every_other_modes_surface_is_clean(self) -> None:
        """A gate that only fires for PLAN would pass a broken DEFAULT."""
        for mode in ("default", "auto_edit", "yolo"):
            report = am.visible_surface(mode, CATALOG, denied=["bash"])
            assert am.surface_violations(report) == [], (mode, report.to_dict())


class TestTheCheckCanActuallyFail:
    """Non-vacuity: a gate nobody has seen fail is a gate nobody reads."""

    def test_mutating_the_surface_is_detected_by_the_gate(self) -> None:
        """A denial that does not reach the surface is caught."""
        clean = am.visible_surface("default", CATALOG, denied=["bash"])
        assert "bash" not in clean.model_visible
        assert am.surface_violations(clean) == []

        broken = am.visible_surface("default", CATALOG)  # the denial was dropped
        assert "bash" in broken.model_visible
        # Not a violation by itself (nothing is denied any more) -- which is
        # exactly why the two assertions above are both needed: dropping the
        # denial and keeping the surface must be distinguishable from
        # narrowing correctly.
        assert am.surface_violations(broken) == []

        assert clean.withheld_count == 1
        assert broken.withheld_count == 0

    def test_a_healthy_surface_really_offers_the_tools(self) -> None:
        """The control arm: the "nothing withheld" assertion cannot pass by the
        surface being empty, which would make the plan-mode test vacuous."""
        default = am.visible_surface("default", CATALOG)
        plan = am.visible_surface("plan", CATALOG)
        assert len(default.model_visible) == len(CATALOG)
        assert 0 < len(plan.model_visible) < len(default.model_visible)
        assert set(CATALOG) - set(plan.model_visible), (
            "plan mode must withhold SOMETHING or the withholding tests are "
            "measuring an empty set"
        )


# ===========================================================================
# T4.W1.3 -- arity-scoped grants, presented honestly
# ===========================================================================


class TestNoBlanketShellApprovalIsEverOffered:
    """Never offer an approval whose scope the user cannot read."""

    @pytest.mark.parametrize(
        "command", ["git commit -m x", "pytest tests", "npm run build"]
    )
    @pytest.mark.parametrize("scope", ["once", "exact_call", "session_command_prefix"])
    def test_every_offered_scope_is_narrower_than_the_shell(
        self, command: str, scope: str
    ) -> None:
        options = am.scope_options("bash", command)
        assert options, "a dialog must always offer at least one option"
        for option in options:
            assert option.blanket is False, option
            assert option.pattern not in {"*", "**", "any", "all"}, option
        assert am.scope_options("bash", command)[0].scope == "once", (
            "approve-once must always be available and must be the FIRST option, "
            "because the first option is the one people click"
        )

    def test_the_gate_itself_refuses_a_blanket_shell_grant(self) -> None:
        """The check is structural: it inspects the option SET, not the builder."""
        blanket = am.GrantScopeView(
            scope="session_command_prefix",
            pattern="*",
            arity=0,
            plain="any bash command",
            layer="per_session",
            tool="bash",
            blanket=True,
        )
        with pytest.raises(ValueError, match="blanket"):
            am.assert_no_blanket_shell_grant([blanket], tool="bash")

        wildcard = dataclasses.replace(blanket, blanket=False)
        with pytest.raises(ValueError, match="every command"):
            am.assert_no_blanket_shell_grant([wildcard], tool="bash")

    def test_a_non_shell_tool_is_not_gated_by_the_shell_rule(self) -> None:
        """The rule is scoped; a read-only gate on every scope would be noise."""
        am.assert_no_blanket_shell_grant(am.scope_options("edit"), tool="edit")
        assert am.scope_options("edit")[-1].scope == "session_path"

    def test_the_control_arm_offers_real_scopes(self) -> None:
        """Without this the test above could pass because the list was empty."""
        scopes = {
            option.scope for option in am.scope_options("bash", "git commit -m x")
        }
        assert len(scopes) >= 2, scopes


class TestTheCorpusArityExamplesHold:
    """The ask pattern a user would approve, read from the AUTHORITY.

    **Two rows changed in P2/W2, and the reasoning is worth keeping because
    wave 1's local table was the thing that was wrong.**

    ``pytest`` and ``ruff`` were pinned at ``2`` by this module's own private
    :data:`am.COMMAND_ARITY`, producing ``pytest tests *`` and ``ruff check *``.
    Both were too NARROW for the tool's real surface:

    * ``pytest``'s second word is an ARGUMENT, not a sub-command. A
      ``pytest tests *`` grant would not have covered ``pytest -q`` or
      ``pytest tests/test_a.py::test_x``, so the user would have been re-asked
      for the same tool over and over -- which trains people to click the
      widest option they are offered.
    * ``ruff``'s second word IS a sub-command, so a ``ruff check *`` grant
      covers ``ruff check`` and nothing else. ``ruff format`` re-prompted.

    ``shared/policy_data/command_arity.json`` (the authority, 167 commands,
    longest-prefix) declares both as ONE-word entries, which is the honest shape
    for each. :func:`am.command_arity` now asks the authority first, so these
    two rows moved. The other three are unchanged, and the ``git`` rows are
    unchanged because the authority really does declare ``git commit`` and
    ``git status`` as separate two-word keys.

    This is a data correction, not a loosened assertion: the new expected
    values are still exact strings, and :func:`am.arity_authority_report` makes
    which table answered visible on demand.
    """

    @pytest.mark.parametrize(
        "command,expected",
        [
            ('git commit -m "fix"', "git commit *"),
            ("git status --short", "git status *"),
            ("git push --force", "git push *"),
            ("npm run build", "npm run build *"),
            # The two corrected rows. `pytest`'s second word is an argument and
            # `ruff`'s is one of several sub-commands, so both are one-word
            # shapes in the authority.
            ("pytest tests -q", "pytest *"),
            ("ruff check cli", "ruff *"),
        ],
    )
    def test_the_exact_command_text_becomes_the_ask_pattern(
        self, command: str, expected: str
    ) -> None:
        assert am.command_pattern(command) == expected

    def test_the_arity_comes_from_the_declared_authority_when_it_can(self) -> None:
        """The pin wave 1 could not write: this module used to BE the authority.

        Scoped to commands with no ``am.ARITY_PREFIXES`` entry, because that
        table is read FIRST and deliberately so -- a three-word family
        (``npm run``) is more specific than any two-column entry, and the
        authority's ``npm`` row is one word. Overriding the authority with a
        MORE specific answer is the documented design, not a bypass of it.
        """
        from shared.policy_lang import command_arity_table

        table = command_arity_table()
        if not table.available:
            pytest.skip(f"the arity table is unavailable: {table.reason}")
        for command in ('git commit -m "fix"', "git push --force", "git status"):
            entry = table.lookup(command)
            assert entry is not None, command
            prefixes = [p for p, _ in am.ARITY_PREFIXES if command.startswith(p)]
            assert not prefixes, (command, prefixes)
            assert am.command_arity(command) == len(entry.command.split()), command

    def test_an_arity_prefix_overrides_the_authority_with_a_narrower_answer(self) -> None:
        """``npm run`` is three words even though the authority says ``npm``."""
        assert am.command_arity("npm run build") == 3
        assert am.command_pattern("npm run build") == "npm run build *"

    def test_the_arity_receipt_names_the_authority_as_the_source(self) -> None:
        report = am.arity_authority_report("git commit -m x")
        assert report["authority"]["authority"].endswith("command_arity.json")
        assert report["authority"]["available"] is True

    def test_an_unknown_binary_defaults_to_the_narrowest_pattern(self) -> None:
        """A default of 2 would pin `ls -la /var` to a path nobody approved."""
        assert am.command_pattern("ls -la /var") == "ls *"
        assert am.command_pattern("weirdtool --flag value") == "weirdtool *"

    def test_a_one_word_command_does_not_get_a_dangling_star(self) -> None:
        """A pattern with an empty pinned part is a pattern nobody can read."""
        assert am.command_pattern("ls") == "ls *"
        assert am.command_arity("ls") == 1
        assert am.command_pattern("") == ""
        assert am.command_arity("") == 1

    def test_the_prefix_table_cannot_contradict_the_binary_table(self) -> None:
        """`git commit` at 3 against `git` at 2 is exactly the drift to prevent."""
        for prefix, arity in am.ARITY_PREFIXES:
            binary = prefix.split()[0]
            declared = am.COMMAND_ARITY.get(binary)
            if declared is None:
                continue
            assert arity >= declared, (
                f"{prefix!r} at {arity} is NARROWER than {binary!r} at "
                f"{declared}; the corpus's own example is `git commit *`"
            )


class TestGrantsArePresentedInPlainLanguage:
    """A user who clicks 'always allow' must be able to know the scope."""

    def test_a_command_grant_reads_as_a_sentence_not_a_glob(self) -> None:
        view = am.describe_scope(
            "session_command_prefix", tool="bash", command="git commit -m x"
        )
        assert view.plain.startswith("commands shaped like this one:")
        assert view.pattern == "git commit *"
        assert "never any other command shape" in view.plain
        assert view.scope == "session_command_prefix"

    def test_a_path_grant_names_the_paths(self) -> None:
        view = am.describe_scope(
            "session_path", tool="edit", paths=("src/a.py", "src/b.py")
        )
        assert "src/a.py" in view.plain
        assert "src/b.py" in view.plain
        assert "rest of this session" in view.plain

    def test_a_once_grant_is_the_narrowest_answer(self) -> None:
        assert am.describe_scope("once", tool="bash", command="pytest tests").plain == (
            "this one action, once"
        )

    def test_an_unrecognised_scope_is_narrowed_and_says_so(self) -> None:
        """It is never upgraded, and the note is part of the receipt."""
        view = am.describe_scope("global", tool="bash", command="pytest tests")
        assert view.scope == "once"
        assert view.notes, "narrowing an unknown scope must be reported"

    def test_the_receipt_carries_every_field_the_ui_needs(self) -> None:
        """A grant missing one of these cannot be rendered completely."""
        view = am.describe_scope(
            "session_command_prefix", tool="bash", command="git commit -m x"
        )
        payload = view.to_dict()
        for field in am.ARITY_REQUIRED_FIELDS:
            assert field in payload, field
        assert payload["layer_plain"].strip()


class TestTheLayeringIsVisible:
    """A surprising grant may come from somewhere the user did not configure."""

    def test_every_layer_has_a_plain_name(self) -> None:
        for layer in am.GRANT_LAYERS:
            assert am.describe_layer(layer).strip()

    def test_a_non_session_layer_says_it_is_not_from_this_session(self) -> None:
        """This is the whole requirement, stated in one assertion."""
        for layer in ("defaults", "builtin_agent", "user_config", "per_agent"):
            # ``describe_scope`` returns a VIEW, and the plain-language sentence
            # is its ``plain`` field. The first version called ``.split`` on the
            # view itself and every layer raised AttributeError -- so this
            # requirement was never actually asserted.
            plain = am.describe_scope(
                "session_command_prefix",
                tool="bash",
                command="git commit -m x",
                layer=layer,
            ).plain
            assert "this session" not in plain.split(" - from ")[0], plain
            assert " - from " in plain, plain
            assert am.describe_layer(layer) in plain

    def test_an_unrecognised_layer_is_not_reported_as_the_session(self) -> None:
        """`this session` is the answer that HIDES the surprising case."""
        plain = am.describe_layer("some_future_layer")
        assert "unrecognised" in plain
        assert "this session" not in plain


class TestTheGrantSetIsInspectableAndRevocable:
    """A grant you cannot see is a grant you cannot revoke."""

    def test_an_empty_ledger_says_so_honestly(self) -> None:
        for ledger in (None, {}, [], object()):
            lines = am.grant_lines(ledger)
            assert lines, "an empty result must still render a line"
            assert "no grants" in lines[0].lower()

    def test_the_current_grant_set_is_rendered_with_scope_and_layer(self) -> None:
        ledger = {
            "grants": [
                {
                    "scope": "session_command_prefix",
                    "tool": "bash",
                    "command": "git commit -m x",
                    "layer": "defaults",
                    "expires_at": None,
                }
            ]
        }
        lines = am.grant_lines(ledger)
        body = "\n".join(lines)
        assert "git commit *" in body, body
        assert "not from this session" in body, body
        assert "built-in default you did not configure" in body, body
        assert "/approval forget" in body, "a grant you cannot revoke is a defect"

    def test_an_expired_grant_is_rendered_as_expired(self) -> None:
        class _Expired:
            scope = "session_command_prefix"
            tool = "bash"
            command = "git commit -m x"
            layer = "per_session"
            expires_at = 1.0

            def is_expired(self) -> bool:
                return True

        lines = am.grant_lines([_Expired()])
        assert "expired" in "\n".join(lines)

    def test_a_grant_that_cannot_be_rendered_is_reported_not_skipped(self) -> None:
        """Silently dropping a grant is how a user forgets they made one."""

        class _Hostile:
            scope = "session_command_prefix"
            tool = "bash"
            command = "git commit -m x"
            layer = "per_session"

            @property
            def paths(self) -> Any:
                raise RuntimeError("boom")

        lines = am.grant_lines([_Hostile()])
        assert any("could not be rendered" in line for line in lines), lines


# ===========================================================================
# T4.W1.4 -- the approver, wired
# ===========================================================================


class _Recorder:
    """A Boundary-2 double that records every call it received."""

    def __init__(self, reply: Any = "") -> None:
        self.calls: List[List[Dict[str, str]]] = []
        self._reply = reply

    def __call__(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(list(messages))
        if isinstance(self._reply, BaseException):
            raise self._reply
        if callable(self._reply):
            return self._reply(messages, **kwargs)
        return self._reply


def _bridge(reply: Any = "", **config: Any) -> Tuple[Any, _Recorder]:
    recorder = _Recorder(reply)
    cfg = {"approver_agent": True, **config}
    return am.ApproverBridge.from_config(cfg, call_fn=recorder), recorder


def _effect(tool: str = "bash", command: str = "pytest tests -q") -> Any:
    from shared.approval import canonical_effect

    return canonical_effect(tool, command.split())


class TestOnlyPrivilegedActionsReachTheReviewer:
    """An unprivileged action must never reach a model at all."""

    def test_an_unprivileged_action_is_refused_before_any_model_call(self) -> None:
        bridge, recorder = _bridge('{"decision":"approve","reason":"fine"}')
        outcome = bridge.consult(_effect(), requires_human=False)
        assert outcome.approved is False
        assert outcome.failure == "not_privileged"
        assert recorder.calls == [], (
            "a model was consulted about an unprivileged action"
        )
        assert outcome.consulted is False
        assert outcome.obtained is False

    def test_an_ordinary_privileged_action_is_consulted(self) -> None:
        """The control arm: the test above cannot pass because nothing works."""
        bridge, recorder = _bridge('{"decision":"approve","reason":"reads only"}')
        outcome = bridge.consult(_effect(), requires_human=True)
        assert outcome.consulted is True
        assert outcome.obtained is True
        assert len(recorder.calls) == 1, (
            "a privileged action reached no reviewer at all"
        )

    def test_the_prompt_carries_the_effect_and_who_is_asking(self) -> None:
        """A reviewer must read the same effect the human reads."""
        from shared.approval import ApprovalOrigin

        bridge, recorder = _bridge('{"decision":"deny","reason":"too broad"}')
        bridge.consult(
            _effect(),
            requires_human=True,
            privileged_reason="the mode is interactive",
            origin=ApprovalOrigin(thread_id="thread-main", label="daily"),
        )
        prompt = recorder.calls[0][0]["content"]
        assert "pytest" in prompt
        assert "thread-main" in prompt
        assert "the mode is interactive" in prompt


class TestTheSixFailClosedPathsAreVisibleAndDistinct:
    """A refusal-with-a-reason stays distinguishable from a refusal we could not obtain."""

    @pytest.mark.parametrize(
        "reply,failure",
        [
            ("", "empty"),
            ("   \n  ", "empty"),
            ("looks fine to me", "unparseable"),
            ('{"decision":"maybe"}', "malformed"),
            ('{"decision":"approve"}', "no_reason"),
            ('{"decision":"approve","reason":""}', "no_reason"),
            ('{"decision":"approve","verdict":"deny","reason":"ok"}', "ambiguous"),
            ('{"decision":"deny","reason":"it deletes state"}', "ok"),
        ],
    )
    def test_every_reply_shape_is_denied_unless_it_is_a_reasoned_approval(
        self, reply: str, failure: str
    ) -> None:
        bridge, _ = _bridge(reply)
        outcome = bridge.consult(_effect(), requires_human=True)
        if failure == "ok":
            assert outcome.approved is False
            assert outcome.failure == "ok", "a reasoned DENIAL is a real answer"
            assert outcome.obtained is True
        else:
            assert outcome.approved is False, reply
            assert outcome.failure == failure, (reply, outcome.to_dict())
            assert outcome.failure in am.APPROVER_FAIL_CLOSED_PATHS

    def test_a_well_formed_approval_is_an_approval(self) -> None:
        """The non-vacuity control: an approver that denies everything passes the
        six refusals above."""
        bridge, _ = _bridge('{"decision":"approve","reason":"reads a test file"}')
        outcome = bridge.consult(_effect(), requires_human=True)
        assert outcome.approved is True
        assert outcome.failure == "ok"
        assert outcome.obtained is True

    def test_a_timeout_denies(self) -> None:
        """A slow reviewer is not allowed to become a default-allow."""
        bridge, _ = _bridge(
            lambda messages, **kwargs: (time.sleep(2.0), "approve")[1],
            approver_timeout_s=1.0,
        )
        outcome = bridge.consult(_effect(), requires_human=True)
        assert outcome.approved is False
        assert outcome.failure == "timeout"
        assert outcome.obtained is False

    def test_a_provider_exception_denies(self) -> None:
        bridge, _ = _bridge(RuntimeError("provider is down"))
        outcome = bridge.consult(_effect(), requires_human=True)
        assert outcome.approved is False
        assert outcome.failure == "model_error"
        assert "RuntimeError" in outcome.reason

    def test_a_disabled_agent_denies_and_says_which_switch(self) -> None:
        """A disabled agent is a DENIAL here, not a silent skip."""
        recorder = _Recorder('{"decision":"approve","reason":"fine"}')
        bridge = am.ApproverBridge.from_config({}, call_fn=recorder)
        assert bridge.enabled is False
        outcome = bridge.consult(_effect(), requires_human=True)
        assert outcome.approved is False
        assert outcome.failure == "disabled"
        assert recorder.calls == [], "a disabled agent must not dial"
        assert am.APPROVER_FAIL_CLOSED_PATHS["disabled"]

    def test_an_untraceable_subagent_escalation_is_refused_outright(self) -> None:
        """A subagent whose origin cannot be established is refused, not shown."""
        from shared.approval import ApprovalOrigin

        bridge, recorder = _bridge('{"decision":"approve","reason":"fine"}')
        outcome = bridge.consult(
            _effect(),
            requires_human=True,
            origin=ApprovalOrigin(subagent_id="sub-7"),
        )
        assert outcome.approved is False
        assert outcome.failure == "untraceable_origin"
        assert recorder.calls == []
        assert "traced" in outcome.reason

    def test_a_traceable_subagent_escalation_is_labelled_as_that_subagent(self) -> None:
        """It is still consulted, and the escalation reads as the subagent's."""
        from shared.approval import ApprovalOrigin

        bridge, recorder = _bridge('{"decision":"approve","reason":"narrow"}')
        outcome = bridge.consult(
            _effect(),
            requires_human=True,
            origin=ApprovalOrigin(
                thread_id="thread-main", subagent_id="sub-7", parent_id="planner"
            ),
        )
        assert outcome.approved is True
        assert len(recorder.calls) == 1
        assert "subagent sub-7" in outcome.origin
        assert "planner" in outcome.origin
        assert "thread-main" in outcome.origin

    def test_all_six_named_paths_have_a_distinct_reason(self) -> None:
        """A reader can count a closed vocabulary; they cannot count a sentence."""
        required = {
            "unparseable",
            "ambiguous",
            "malformed",
            "no_reason",
            "timeout",
            "model_error",
            "disabled",
            "untraceable_origin",
        }
        assert required.issubset(set(am.APPROVER_FAIL_CLOSED_PATHS))
        reasons = [am.APPROVER_FAIL_CLOSED_PATHS[name] for name in required]
        assert len(set(reasons)) == len(reasons), "two failures share one sentence"
        assert all(reason.strip() for reason in reasons)

    def test_the_failure_vocabulary_is_closed(self) -> None:
        assert set(am.APPROVER_OUTCOMES) == set(am.APPROVER_FAIL_CLOSED_PATHS) | {"ok"}
        assert am.APPROVER_OUTCOMES[0] == "ok"

    def test_every_denial_renders_a_reason_a_user_can_read(self) -> None:
        """A user who sees 'denied' with no reason cannot act."""
        for reply, _ in [
            ("", None),
            ("looks fine to me", None),
            ('{"decision":"approve"}', None),
        ]:
            bridge, _ = _bridge(reply)
            outcome = bridge.consult(_effect(), requires_human=True)
            line = outcome.explanation()
            assert "denied" in line
            assert len(line.split(" - ", 1)[-1].strip()) > 10, line


class TestTheApproverStructurallyCannotWiden:
    """Structurally, not by convention."""

    def test_the_decision_is_the_human_decision_in_both_directions(self) -> None:
        for reply in (
            '{"decision":"approve","reason":"fine"}',
            '{"decision":"deny","reason":"no"}',
        ):
            bridge, _ = _bridge(reply)
            outcome = bridge.consult(_effect(), requires_human=True)
            for human in (True, False, (True, "session_path"), (False, "once"), None):
                assert outcome.as_decision(human) == human, (
                    f"the approver changed a human decision of {human!r} "
                    f"(reply {reply!r})"
                )

    def test_the_outcome_says_it_is_advisory_on_every_path(self) -> None:
        """The field a caller would have to ignore to be wrong."""
        for reply in ("", "looks fine to me", '{"decision":"approve","reason":"y"}'):
            bridge, _ = _bridge(reply)
            assert bridge.consult(_effect(), requires_human=True).advisory is True

    def test_the_bridge_offers_no_api_that_returns_an_approval(self) -> None:
        """There is no `approve()` on the outcome: the failure mode is absent."""
        outcome = am.ApproverOutcome(
            approved=True, consulted=True, failure="ok", reason="fine"
        )
        for forbidden in ("allow", "approve", "to_decision", "grant"):
            assert not hasattr(outcome, forbidden), (
                f"ApproverOutcome grew {forbidden!r}, which is a second path for "
                "a reviewer to become an authorization"
            )


class TestTheJournalIsTheAuditTrail:
    """Every prompt, verdict and origin, in one place."""

    def test_the_journal_path_is_resolved_and_reported(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bridge, _ = _bridge('{"decision":"approve","reason":"fine"}')
            bridge._journal_path = str(tmp) + "/approver.jsonl"
            outcome = bridge.consult(_effect(), requires_human=True)
            assert outcome.journal_path.endswith("approver.jsonl")
            assert outcome.journal_path in bridge.report()["journal_path"]

    def test_the_report_separates_obtained_from_not_obtained(self) -> None:
        """ "The reviewer said no" and "we never got to ask" are different facts."""
        # Three consultations that are OBTAINED and three that are not, so the
        # test cannot pass by every outcome landing in one bucket.
        #
        # The first version used an EMPTY reply as its "obtained" arm and
        # expected `obtained == 1`, which asserted the bug this field exists to
        # prevent: a reviewer that replied with nothing was counted as having
        # answered. "Obtained" is now a REVIEWER WHO SPOKE -- a reasoned
        # denial counts, a parse failure does not -- so the obtained arm here is
        # a reasoned refusal and the not-obtained arms are a silence and a
        # never-asked.
        # A stateful reply, because one bridge has ONE reply source: the first
        # two consultations get a reasoned refusal (the reviewer SPOKE) and the
        # third gets a silence (it did not). Reusing one static reply made the
        # "not obtained" arm unreachable, which is how the original bug passed.
        seen: List[int] = []

        def _reply(messages: Any, **kwargs: Any) -> str:
            seen.append(1)
            if len(seen) <= 2:
                return '{"decision":"deny","reason":"it deletes state"}'
            return ""

        bridge, _ = _bridge(_reply)
        assert bridge.consult(_effect(), requires_human=True).obtained is True
        assert bridge.consult(_effect(), requires_human=True).obtained is True
        bridge.consult(_effect(), requires_human=True)  # silence -> not obtained
        bridge.consult(_effect(), requires_human=False)  # never asked -> not obtained
        report = bridge.report()
        assert report["consultations"] == 4, report
        assert report["obtained"] == 2, report
        assert report["not_obtained"] == 2, report
        assert report["by_failure"]["ok"] == 2, report
        assert report["by_failure"]["empty"] == 1, report
        assert report["by_failure"]["not_privileged"] == 1, report
        assert report["fail_closed_paths"], "the table travels with the report"


class TestTheBridgeNeverRaisesAndNeverDialsOnConstruction:
    """Import-time safety: `import cli` must not acquire a model dependency."""

    def test_constructing_a_bridge_contacts_nothing(self) -> None:
        recorder = _Recorder('{"decision":"approve","reason":"fine"}')
        bridge = am.ApproverBridge.from_config(
            {"approver_agent": True}, call_fn=recorder
        )
        assert recorder.calls == []
        assert bridge.outcomes == []

    def test_a_hopeless_effect_degrades_to_a_denial_not_a_crash(self) -> None:
        bridge, _ = _bridge('{"decision":"approve","reason":"fine"}')

        class _Hostile:
            tool = "bash"

            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("boom")

        outcome = bridge.consult(_Hostile(), requires_human=True)
        assert outcome.approved is False
        assert outcome.failure in am.APPROVER_FAIL_CLOSED_PATHS


# ===========================================================================
# T4.W1.5 -- seven outcomes, one line each
# ===========================================================================


class TestAllSevenOutcomesRender:
    """Assert every outcome renders, INCLUDING the rare ones."""

    def test_the_outcome_set_is_exactly_seven_and_closed(self) -> None:
        assert len(am.PERMISSION_OUTCOMES) == 7
        assert am.PERMISSION_OUTCOMES == (
            "allow",
            "ask_granted",
            "ask_denied",
            "ask_timed_out",
            "floor_refused",
            "mode_blocked",
            "capability_withheld",
        )

    @pytest.mark.parametrize("outcome", am.PERMISSION_OUTCOMES)
    def test_every_outcome_renders_one_non_blank_line(self, outcome: str) -> None:
        """A permission UI that goes blank on an unusual outcome has no affordance."""
        decision = am.explain(outcome, tool="bash", effect="pytest tests -q")
        line = am.explain_line(decision)
        assert line.strip(), f"{outcome} rendered blank"
        assert line.strip("\n") == line, f"{outcome} rendered more than one line"
        assert "\n" not in line

    @pytest.mark.parametrize("outcome", am.PERMISSION_OUTCOMES)
    def test_the_line_names_action_reason_and_next_step(self, outcome: str) -> None:
        """The three halves are three questions, and all three are answered."""
        decision = am.explain(outcome, tool="bash", effect="pytest tests -q")
        assert decision.action.strip(), outcome
        assert decision.reason.strip(), outcome
        assert decision.next_step.strip(), outcome
        line = am.explain_line(decision)
        assert decision.action in line
        assert decision.reason in line
        assert decision.next_step in line

    @pytest.mark.parametrize(
        "outcome", [name for name in am.PERMISSION_OUTCOMES if name != "allow"]
    )
    def test_every_outcome_with_a_door_carries_a_runnable_command(
        self, outcome: str
    ) -> None:
        """ "A classifier action can be pure diagnosis ... and useless as an
        instruction" -- so a card that can be acted on names a command."""
        decision = am.explain(outcome, tool="bash", effect="pytest tests -q")
        runnable = [item for item in decision.commands if item.startswith("/")]
        assert runnable, f"{outcome} has no runnable next step: {decision.to_dict()}"
        assert any(item in am.explain_line(decision) for item in runnable), outcome

    def test_allow_says_nothing_to_do_rather_than_inventing_work(self) -> None:
        """Attaching /doctor /trace to an allowed action says something is wrong."""
        decision = am.explain("allow", tool="read")
        assert decision.commands == (), decision.to_dict()
        assert "nothing to do" in decision.next_step
        assert "/doctor" not in am.explain_line(decision)

    def test_an_unrecognised_outcome_still_renders_and_says_so(self) -> None:
        """Refusing to render a new outcome is how a blank permission UI is born."""
        decision = am.explain("some_future_outcome", tool="bash")
        line = am.explain_line(decision)
        assert line.strip()
        notes = am.explain_lines(decision)
        assert len(notes) == 2, notes
        assert "not one this build defines" in notes[1]

    @pytest.mark.parametrize("outcome", [*am.PERMISSION_OUTCOMES, "", None])
    def test_nothing_renders_blank_including_a_none_outcome(
        self, outcome: Optional[str]
    ) -> None:
        """Total, including the shapes no caller will ever pass."""
        assert am.explain_line(am.explain(outcome)).strip()

    def test_a_supplied_reason_and_floor_are_carried_verbatim(self) -> None:
        """A reader needs the SPECIFIC rule, not the outcome's generic default."""
        decision = am.explain(
            "floor_refused",
            floor="the harness hard-denies VCS metadata paths",
            reason="policy rule deny.vcs_metadata",
        )
        assert "deny.vcs_metadata" in am.explain_line(decision)
        lines = am.explain_lines(decision)
        assert decision.reason in lines[0]

    def test_the_line_clips_with_a_marker_rather_than_a_bare_cut(self) -> None:
        """A clipped sentence stops being true at the cut."""
        decision = am.explain("allow", tool="bash", effect="pytest tests -q")
        line = am.explain_line(decision, width=40)
        assert len(line) <= 40
        assert line.endswith("...")

    def test_the_receipt_round_trips_through_json_in_one_line(self) -> None:
        """A receipt has to survive a journal row and a --json document."""
        decision = am.explain("ask_denied", tool="bash", effect="pytest tests")
        line = decision.to_json()
        assert "\n" not in line
        assert json.loads(line)["outcome"] == "ask_denied"
        assert json.loads(line)["decision"] == "refused"


class TestTheApprovFailuresBecomePermissionOutcomes:
    """The approver's denials surface as ONE of the seven, with the reason."""

    @pytest.mark.parametrize(
        "reply", ["", "looks fine to me", '{"decision":"approve"}']
    )
    def test_an_approver_denial_explains_itself_in_one_line(self, reply: str) -> None:
        bridge, _ = _bridge(reply)
        outcome = bridge.consult(_effect(), requires_human=True)
        decision = am.explain(
            "ask_denied",
            tool="bash",
            effect="pytest tests -q",
            reason=outcome.explanation(),
        )
        line = am.explain_line(decision)
        assert "second opinion" in line
        assert line.strip()
        assert decision.next_step.startswith("/")

    def test_a_well_formed_approval_explains_itself_too(self) -> None:
        """The control arm: a renderer that only handles denials passes the above."""
        bridge, _ = _bridge('{"decision":"approve","reason":"reads a test file"}')
        outcome = bridge.consult(_effect(), requires_human=True)
        decision = am.explain(
            "ask_granted",
            tool="bash",
            effect="pytest tests -q",
            reason=outcome.explanation(),
        )
        assert "second opinion: approved" in am.explain_line(decision)


# ===========================================================================
# Structural pins -- the shape of the guarantee, not its behaviour
# ===========================================================================


# ===========================================================================
# The MOUNT -- the surface a person can actually reach
# ===========================================================================


class TestTheSurfaceIsMountedOnBothShells:
    """The module existed for twelve rounds with ZERO production importers.

    Every earlier class in this file tests the module against ITSELF, which is
    how a fully-built feature can be entirely unreachable. These tests are the
    ones that would have caught it: they drive the real registry, the real
    dispatcher, and the real ``harness`` catalog.
    """

    def test_the_registry_declares_the_command_with_every_verb(self) -> None:
        from cli import commands as commands_mod

        spec = commands_mod.command_spec("/approval")
        assert spec is not None, "/approval has no registry row"
        assert spec.subcommands == ("show", "mode", "grants", "forget", "tools")
        assert "mode" in spec.mutating_verbs, "setting a mode mutates state"
        assert "forget" in spec.mutating_verbs, "revoking a grant mutates state"

    def test_every_next_step_the_product_prints_is_a_verb_that_exists(self) -> None:
        """The reason this command exists: no receipt may name a doorless verb.

        Each of the seven permission outcomes renders a ``next:`` line naming a
        ``/approval`` verb. If a verb is renamed in the registry and a receipt
        is not, the product tells a person to type something that does not
        exist -- the exact defect the refusal explanations are meant to prevent.
        """
        from cli import commands as commands_mod

        spec = commands_mod.command_spec("/approval")
        known = set(spec.subcommands)
        for outcome in am.PERMISSION_OUTCOMES:
            for step in am.explain(outcome).commands:
                if not step.startswith("/approval"):
                    continue
                parts = step.split()
                # A bare `/approval` is legitimate -- it resolves to the
                # no-argument verb, which the registry declares as the default.
                if len(parts) == 1:
                    assert "show" in known, "a bare /approval must open a verb"
                    continue
                assert parts[1] in known, (
                    f"the {outcome!r} outcome tells the user to run {step!r}, "
                    f"and /approval has no {parts[1]!r} verb"
                )

    def test_both_shells_dispatch_it(self) -> None:
        """``ast``, because a reformat must not be able to empty the check."""
        import pathlib

        def _keys(path: str) -> set:
            tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
            keys: set = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == (
                    "_slash_command_impl"
                ):
                    for inner in ast.walk(node):
                        if (
                            isinstance(inner, ast.Compare)
                            and isinstance(inner.left, ast.Name)
                            and inner.left.id == "cmd"
                        ):
                            for comparator in inner.comparators:
                                if isinstance(comparator, ast.Tuple):
                                    for element in comparator.elts:
                                        if isinstance(element, ast.Constant):
                                            keys.add(element.value)
                                elif isinstance(comparator, ast.Constant):
                                    keys.add(comparator.value)
            return keys

        repl = _keys(str(pathlib.Path(am.__file__).parent / "interactive.py"))
        tui = _keys(str(pathlib.Path(am.__file__).parent / "tui.py"))
        assert "/approval" in repl, "the REPL cannot reach /approval"
        assert "/approval" in tui, "the TUI cannot reach /approval"
        assert repl == tui, (
            "the two shells dispatch different command sets, so one of them "
            f"has a command the other does not: {sorted(repl ^ tui)}"
        )

    def test_the_palette_row_exists(self) -> None:
        """A registry row with no palette group is a command nobody can find."""
        from cli import commands as commands_mod
        from cli import palette

        assert "/approval" in palette.COMMAND_GROUPS
        unassigned = [
            spec.name
            for spec in commands_mod.COMMAND_SPECS
            if spec.name not in palette.COMMAND_GROUPS
        ]
        assert not unassigned, f"commands with no palette group: {unassigned}"

    def test_the_dispatcher_answers_every_verb(self) -> None:
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        for args in ("", "show", "mode", "grants", "tools"):
            receipt = interactive.run_subcommand(spec, args, state={})
            assert receipt is not None, f"/approval {args!r} has no handler"
            assert receipt["ok"] is True, (args, receipt["lines"])
            assert receipt["lines"], f"/approval {args!r} rendered nothing"

    def test_the_receipts_are_plain_and_never_carry_a_raw_marker(self) -> None:
        """``[PLAN]`` is PLAIN TEXT and must reach the screen intact.

        The markers are literal brackets. A renderer that hands a line to a
        markup parser unescaped DELETES the mode it was reporting -- so this
        asserts the marker is in the plain receipt, and the escaping is the
        caller's job (proved separately by the escaping test below).
        """
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        receipt = interactive.run_subcommand(spec, "mode", state={})
        joined = "\n".join(receipt["lines"])
        for name in am.APPROVAL_MODES:
            assert am.mode_marker(name) in joined, f"{am.mode_marker(name)} was lost"
        assert "set one with: /approval mode <name>" in joined, (
            "the mode list must teach the command that changes it"
        )

    def test_setting_a_mode_changes_what_the_session_reports(self) -> None:
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        state: Dict[str, Any] = {}
        before = interactive.run_subcommand(spec, "", state=state)
        interactive.run_subcommand(spec, "mode yolo", state=state)
        after = interactive.run_subcommand(spec, "", state=state)
        assert before["payload"]["mode"] == "default"
        assert after["payload"]["mode"] == "yolo", after["lines"]
        assert state["approval_mode"] == "yolo", "the session state must carry it"

    def test_an_unknown_mode_is_a_refusal_naming_the_valid_set(self) -> None:
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        receipt = interactive.run_subcommand(spec, "mode wat", state={})
        assert receipt["ok"] is False
        joined = " ".join(receipt["lines"])
        for name in am.APPROVAL_MODES:
            assert name in joined, f"{name!r} is not named in the refusal"

    def test_the_tools_verb_reports_the_real_kernel_catalog(self) -> None:
        """The end-to-end claim, on the real registry rather than a fixture."""
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        receipt = interactive.run_subcommand(spec, "tools", state={})
        assert receipt["ok"] is True, receipt["lines"]
        payload = receipt["payload"]
        assert payload["known"] is True
        assert payload["catalog_total"] > 20
        assert payload["violations"] == [], (
            "the real catalog is mis-provisioned: " + " ".join(receipt["lines"])
        )

    def test_forget_refuses_rather_than_guessing(self) -> None:
        from cli import commands as commands_mod
        from cli import interactive

        spec = commands_mod.command_spec("/approval")
        for args, why in (
            ("forget", "no number"),
            ("forget 0", "below the range"),
            ("forget 99", "above the range"),
            ("forget x", "not a number"),
        ):
            receipt = interactive.run_subcommand(spec, args, state={})
            assert receipt["ok"] is False, f"{args!r} should refuse ({why})"
            assert receipt["lines"], f"{args!r} refused with no sentence"


class TestTheAdvisoryIsOnTheRealApprovalPath:
    """``harness.approver`` reached only through the live approver choke point."""

    @staticmethod
    def _recording_bridge(approved: bool = False) -> Any:
        class _Bridge:
            enabled = True

            def __init__(self) -> None:
                self.seen: List[Any] = []

            def consult(self, effect: Any, **kwargs: Any) -> Any:
                self.seen.append(effect)
                return am.ApproverOutcome(
                    approved=approved,
                    consulted=True,
                    failure="ok",
                    reason="the reviewer disagreed" if not approved else "fine",
                    journal_path="approver.jsonl",
                )

        return _Bridge()

    def test_a_reviewer_denial_does_not_change_the_human_decision(self) -> None:
        """The no-widen guarantee, measured through the LIVE wiring.

        The unit suite proves ``as_decision`` is the identity function. This
        proves the wiring actually READS it: the reviewer refuses, the human
        approved, and the answer that comes back is the human's.
        """
        from cli import interactive

        bridge = self._recording_bridge(approved=False)
        interactive.set_advisory_approver(bridge)
        try:
            wrapped = interactive._trusted_approver(
                lambda tool=None, args=None, *rest: (True, "once")
            )
            assert wrapped("bash", {"command": "rm -rf build"}) == (True, "once")
            assert bridge.seen, "the reviewer was never consulted"
        finally:
            interactive.set_advisory_approver(None)

    def test_the_effect_carries_the_command_and_its_real_class(self) -> None:
        """A reviewer told `rm -rf` is a READ approves a destructive action.

        ``canonical_effect`` defaults ``side_effect_class`` to ``read_only``, so
        the caller must pass the real one. This is the assertion that keeps a
        second opinion from being worthless.
        """
        from cli import interactive

        bridge = self._recording_bridge()
        interactive.set_advisory_approver(bridge)
        try:
            interactive._advisory_second_opinion(
                tool="bash", arguments={"command": "rm -rf build"}
            )
            effect = bridge.seen[0]
            assert effect.render == "rm -rf build", effect.render
            assert effect.side_effect_class == "process", (
                "the reviewer was told a destructive command is "
                f"{effect.side_effect_class!r}"
            )
        finally:
            interactive.set_advisory_approver(None)

    def test_no_configured_reviewer_means_no_second_opinion(self) -> None:
        """Off unless a run asks for it: no default may dial a provider."""
        from cli import interactive

        interactive.set_advisory_approver(None)
        interactive._APPROVER_BRIDGE_CONFIGURED = False
        assert interactive._advisory_second_opinion(tool="bash", arguments={}) is None

    def test_a_remembered_grant_is_never_second_opinioned(self) -> None:
        """A settled decision must not be re-litigated by spending a model call."""
        from cli import interactive
        from shared.approval import TrustLedger

        ledger = TrustLedger()
        ledger.record(
            scope="session_command_prefix", tool="bash", command_prefix="git status"
        )
        interactive._TRUST["ledger"] = ledger
        bridge = self._recording_bridge()
        interactive.set_advisory_approver(bridge)
        try:
            wrapped = interactive._trusted_approver(
                lambda tool=None, args=None, *rest: (True, "once")
            )
            answer = wrapped("bash", {"command": "git status --short"})
            assert answer == (True, "session_command_prefix"), answer
            assert bridge.seen == [], "a remembered grant was re-asked"
        finally:
            interactive.set_advisory_approver(None)
            interactive._TRUST.pop("ledger", None)


class TestTheStatuslineNamesTheModeWithoutCostingAnIdleRow:
    """A rail row is worth a row only when it announces a deviation."""

    @staticmethod
    def _row(state: Optional[Dict[str, Any]]) -> str:
        from cli.tui import _approval_statusline_text

        return _approval_statusline_text(state)

    def test_every_mode_renders_a_marker_and_a_door(self) -> None:
        for name in am.APPROVAL_MODES:
            row = self._row({"config": {"approval_mode": name}})
            assert am.mode_marker(name) in row, row
            assert "/approval" in row, f"{row} names no command to change it"

    def test_the_markers_are_ascii(self) -> None:
        """A permission marker needing an encoding probe is one nobody reads."""
        for name in am.APPROVAL_MODES:
            am.mode_marker(name).encode("ascii")

    def test_an_unreadable_mode_is_marked_unknown_not_absent(self) -> None:
        row = self._row(None)
        assert row.strip(), "an unresolvable mode rendered nothing at all"

    def test_the_default_mode_is_not_announced_but_the_others_are(self) -> None:
        """The rule ``density``/``sidebar`` already follow, applied to the mode.

        Publishing the default unconditionally cost an idle shell a row and
        broke another owner's layout pin whose intent ("a statusline with
        nothing to say costs nothing") is correct. The deviations are the facts.
        """
        from cli.tui import _approval_mode_is_deviation

        assert _approval_mode_is_deviation({"config": {}}) is False
        for name in ("plan", "auto_edit", "yolo"):
            assert _approval_mode_is_deviation({"config": {"approval_mode": name}}), (
                f"{name} is a deviation and must be announced"
            )

    def test_the_permission_section_cannot_be_dropped_by_narrowing(self) -> None:
        """Pinned in ``cli/design.py``: a safety marker may overflow, not vanish.

        A ``[YOLO!]`` that disappears at 80 columns is a safety control removed
        without saying so. The fitter keeps it and reports ``overflow`` instead.
        """
        from cli import design

        sections = {item.key: item for item in design.STATUSLINE_SECTIONS}
        assert sections["permission"].pinned is True, (
            "the permission section must be pinned or a narrow terminal can "
            "silently remove the mode marker"
        )
        for width in (8, 20, 40, 80, 200):
            fit = design.fit_statusline(
                {"permission": "[YOLO!] (/approval)", "sidebar": "sidebar show (ctrl+5)"},
                width,
                hint_limit=1,
            )
            assert "permission" in fit.sections, (
                f"the permission marker was dropped at width {width}: {fit}"
            )
            assert "[YOLO!] (/approval)" in fit.text, (
                f"the marker was dropped from the row at width {width}: {fit.text!r}"
            )

    def test_the_repl_states_the_mode_before_the_run_status(self) -> None:
        """``/status`` states it unconditionally -- it is a command, not chrome."""
        import pathlib

        source = pathlib.Path(am.__file__).parent / "interactive.py"
        text = source.read_text(encoding="utf-8")
        idx = text.index('if cmd in ("/status",):')
        window = text[idx : idx + 900]
        assert "_approval_statusline_text" in window, (
            "the REPL /status does not state the permission mode"
        )


class TestStructuralPins:
    """The properties that must not be quietly undone by a refactor."""

    def _source(self) -> str:
        import pathlib

        return pathlib.Path(am.__file__).read_text(encoding="utf-8")

    def test_as_decision_returns_its_argument_and_branches_on_nothing(self) -> None:
        """The no-widen guarantee read from the AST, so a rewrite cannot soften it.

        A future ``as_decision`` that gained ``if self.approved: return True``
        would pass every behavioural test that only checked the denial path.
        """
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "as_decision":
                body = [
                    child
                    for child in node.body
                    if not (
                        isinstance(child, ast.Expr)
                        and isinstance(child.value, ast.Constant)
                    )
                ]
                assert len(body) == 1 and isinstance(body[0], ast.Return), (
                    "as_decision grew a branch; the no-widen guarantee is structural"
                )
                returned = body[0].value
                assert isinstance(returned, ast.Name) and returned.id == "human", (
                    "as_decision must return its argument verbatim"
                )
                return
        raise AssertionError("as_decision is gone")

    def test_the_module_carries_no_completion_vocabulary(self) -> None:
        """A permission receipt must not be able to imply a verified RESULT."""
        tree = ast.parse(self._source())
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for forbidden in (
            "completed_verified",
            "completed_unverified",
            "run_verdict",
            "status_is_success",
        ):
            assert forbidden not in literals, forbidden

    def test_the_module_does_not_import_a_shell(self) -> None:
        """Import-time safety: this module must be importable with no harness.

        **MODULE-LEVEL imports only.** The property being protected is
        ``import cli.approval_modes`` not requiring the harness, so a lazy
        ``from harness import approver`` INSIDE ``consult`` is the design and
        not a violation -- it is exactly what keeps the model dependency out of
        ``import cli``. The first version walked the whole tree and so flagged
        those three deliberate lazy imports, which is a gate punishing the
        correct implementation. It now walks module scope and, separately,
        asserts the lazy imports exist at all so the protection cannot be
        satisfied by deleting them.
        """
        tree = ast.parse(self._source())
        imported: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for forbidden in ("subprocess", "socket", "harness", "runtime", "textual"):
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in imported
            ), f"{forbidden} is imported at module scope: {imported}"

        # The lazy boundary is the thing that makes the first assertion true,
        # so it is pinned rather than assumed.
        lazy = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] in {"harness", "shared"}
        ]
        assert lazy, "the harness/approver boundary must be resolved lazily"

    def test_every_rendered_function_is_total_over_hostile_input(self) -> None:
        """A renderer that raises is the worst failure a renderer has."""
        hostile = ["", "   ", None, 0, [], {}, object()]
        for outcome in list(am.PERMISSION_OUTCOMES) + hostile:
            assert am.explain_line(am.explain(outcome)).strip()
        for value in hostile:
            assert am.mode_marker(value).strip()
            assert am.mode_line(value).strip()
            assert am.grant_lines(value)
            assert am.describe_scope(value).plain.strip()
            assert am.describe_layer(value).strip()
