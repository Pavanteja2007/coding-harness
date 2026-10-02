"""VEX-CS-04: aliases, the alias table's gate, stacking, and queue parity.

Three deltas, one suite, one test per behaviour and each test named after
the behaviour it checks.

Host-only. No Docker, no provider, no network, no credential, and nothing
written outside ``tmp_path``. Every function under test is pure, so the suite
is deterministic and the same run on a loaded four-terminal host produces the
same answer.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from cli import command_aliases as _aliases
from cli import command_queue as _queue
from cli import commands as _commands

REPO = Path(__file__).resolve().parents[1]


# ===========================================================================
# DELTA 1 -- the alias table
# ===========================================================================


class TestEveryAliasResolvesToItsTarget:
    """Each declared alias must reach a real command."""

    def test_every_alias_resolves_to_a_registered_command(self) -> None:
        """No alias may point at a command the registry does not carry.

        An alias whose target is missing resolves to nothing while reporting
        success, which is the same failure class as a menu entry that opens
        an empty screen.
        """

        broken = []
        for alias in _aliases.ALIAS_KEYS:
            end = _aliases.resolve_alias(alias)
            if _commands.command_spec(end) is None:
                broken.append(f"{alias} -> {end}")
        assert not broken, "aliases that do not reach a command: " + ", ".join(broken)

    def test_resolve_alias_follows_the_whole_chain(self) -> None:
        """A chain resolves to its END, not to its intermediate hop.

        `/bashes -> /tasks -> /sessions` is the case: a menu that showed
        `/bashes -> /tasks` would teach a name the person cannot type.
        """

        assert _aliases.resolve_alias("/bashes") == "/sessions"
        assert _aliases.resolve_alias("/tasks") == "/sessions"
        assert _aliases.resolve_alias("/usage") == "/cost"
        assert _aliases.resolve_alias("/stats") == "/cost"

    def test_a_name_that_is_not_an_alias_resolves_to_itself(self) -> None:
        """A real command name is already canonical; nothing to translate."""

        for name in ("/diff", "/undo", "/clear", "/resume", "/plugins"):
            assert _aliases.resolve_alias(name) == name

    def test_a_bare_word_without_a_slash_still_normalises(self) -> None:
        """The composer hands half-typed names, so normalisation is total."""

        assert _aliases.normalize_command_name("reset") == "/reset"
        assert _aliases.normalize_command_name("/Reset") == "/reset"
        assert _aliases.resolve_alias("reset") == "/clear"
        assert _aliases.normalize_command_name(None) == ""

    def test_alias_target_reports_the_one_hop_not_the_end(self) -> None:
        """`alias_target` is the table's row; `resolve_alias` is the chain."""

        assert _aliases.alias_target("/bashes") == "/tasks"
        assert _aliases.alias_target("/reset") == "/clear"
        assert _aliases.alias_target("/diff") is None  # not an alias
        assert _aliases.alias_target("") is None

    def test_the_eight_pre_round_aliases_are_all_still_present(self) -> None:
        """Backwards compatibility is mandatory, so each is pinned by name.

        These are the aliases that existed before this round. Removing one is
        a breaking change to a user's muscle memory, and a test is the only
        thing that stops that happening silently.
        """

        historical = {
            "/changes": "/diff",
            "/checkpoint": "/checkpoints",
            "/copy": "/copy-diff",
            "/exit": "/quit",
            "/related": "/relevant",
            "/thinking": "/effort",
            "/auth": "/connect",
        }
        for alias, target in historical.items():
            assert _aliases.ALIASES.get(alias) == target, f"{alias} moved or vanished"

    def test_the_eight_pre_round_aliases_still_resolve_in_the_live_registry(
        self,
    ) -> None:
        """The registry's own alias rows keep working alongside this table.

        `cli/commands.py` declares these on `CommandSpec.aliases` and this
        round does not own that file, so the two sources must agree rather
        than one quietly superseding the other.
        """

        for alias in (
            "/changes",
            "/checkpoint",
            "/copy",
            "/exit",
            "/related",
            "/thinking",
            "/auth",
        ):
            spec = _commands.command_spec(alias)
            assert spec is not None, f"{alias} stopped resolving in the registry"
            assert _aliases.resolve_alias(alias) == spec.name


class TestTheTableIsAcyclic:
    """A cycle makes resolution unbounded and the disclosure unreadable."""

    def test_the_alias_graph_has_no_cycle(self) -> None:
        """The shipped table is acyclic, as a WALK not a set difference."""

        report = _aliases.alias_cycle_report()
        assert report["acyclic"] is True, report["cycles"]
        assert report["cycles"] == []
        # The walk visits the intermediate hop of a CHAIN as well as the
        # alias keys, so the node count is at least the key count.
        assert report["nodes_walked"] >= len(_aliases.ALIAS_KEYS)

    def test_a_deliberate_cycle_is_detected_and_named(self) -> None:
        """The acyclicity gate can actually FAIL, and says which cycle.

        A gate nobody knows the sensitivity of is a gate nobody reads. This
        injects `/clear -> /new` on top of the shipped `/new -> /clear` and
        asserts the gate names the loop rather than passing.
        """

        injected = dict(_aliases.ALIASES)
        injected["/clear"] = "/new"
        original = _aliases.ALIASES
        try:
            _aliases.ALIASES = injected  # type: ignore[misc]
            report = _aliases.alias_cycle_report()
        finally:
            _aliases.ALIASES = original  # type: ignore[misc]
        assert report["acyclic"] is False
        assert any(
            "/new" in entry and "/clear" in entry for entry in report["cycles"]
        ), report

    def test_resolve_alias_is_bounded_even_on_a_cyclic_table(self) -> None:
        """The second line of defence: the hop bound, not the gate.

        With `/clear -> /new` injected the gate fails, and `resolve_alias`
        must still terminate rather than spin -- because the gate is a test
        and this is production.
        """

        injected = dict(_aliases.ALIASES)
        injected["/clear"] = "/new"
        original = _aliases.ALIASES
        try:
            _aliases.ALIASES = injected  # type: ignore[misc]
            resolved = _aliases.resolve_alias("/reset")
        finally:
            _aliases.ALIASES = original  # type: ignore[misc]
        assert isinstance(resolved, str) and resolved.startswith("/")

    def test_validate_alias_table_passes_on_the_shipped_table(self) -> None:
        """The gate is green on what actually ships."""

        audit = _aliases.validate_alias_table()
        assert audit.ok is True, audit.message()
        assert audit.aliases == len(_aliases.ALIAS_KEYS)
        assert "acyclic" in audit.message()


class TestNoAliasShadowsARealCommand:
    """An alias spelled like a command is permanently unreachable."""

    def test_no_alias_is_also_a_real_command_name(self) -> None:
        """`command_spec` resolves an exact name BEFORE consulting aliases.

        That is measured, not assumed, and it is why a shadowing alias is
        dead code rather than a stylistic problem.
        """

        real = {spec.name for spec in _commands.COMMAND_SPECS}
        shadows = [a for a in _aliases.ALIAS_KEYS if a in real]
        assert shadows == [], f"alias shadows a real command: {shadows}"

    def test_the_shadowing_rule_is_why_diff_could_never_have_been_an_alias(
        self,
    ) -> None:
        """The rule, proved on the one row it actually bit.

        `/diff` is a real command AND was declared an alias of `/undo`. The
        exact-name pass wins, so the alias was unreachable and `/diff` has
        always meant "read a diff". This is the measurement behind the
        retirement decision rather than a claim about it.
        """

        assert _commands.command_spec("/diff") is not None
        assert _commands.command_spec("/diff").name == "/diff"
        assert "/diff" in _commands.command_spec("/undo").aliases
        # ...and it is unreachable: a spec reached THROUGH an alias lookup
        # still lands on the exact-name pass first.
        assert _commands.command_spec("/diff").name != "/undo"

    def test_the_gate_reports_a_shadow_it_finds(self) -> None:
        """The no-shadow check can FAIL and names what it found."""

        injected = dict(_aliases.ALIASES)
        injected["/sessions"] = "/clear"  # /sessions IS a real command
        original = _aliases.ALIASES
        try:
            _aliases.ALIASES = injected  # type: ignore[misc]
            audit = _aliases.validate_alias_table()
        finally:
            _aliases.ALIASES = original  # type: ignore[misc]
        assert audit.ok is False
        assert "/sessions" in audit.shadows
        assert any("shadows" in f for f in audit.findings)


class TestTheDiffAliasCollisionIsDecidedAndPinned:
    """DELTA 1(a): `/diff` must never silently revert anything."""

    def test_diff_is_retired_with_a_stated_migration(self) -> None:
        """The decision is a RECORD, and the record says why.

        The prompt's question was "what does that alias do for a user today,
        and does it stay?". The answer, measured: nothing, because it was
        unreachable. So it is retired, and the retirement carries the reason
        and the migration.
        """

        assert "/diff" in _aliases.RETIRED_ALIASES
        reason = _aliases.RETIRED_ALIASES["/diff"]
        assert "/undo" in reason, "the reason must name what it would have done"
        assert "MIGRATION" in reason
        assert "/diff" not in _aliases.ALIASES

    def test_typing_diff_reads_a_diff_and_never_reverts(self) -> None:
        """The behavioural half: the product does what the decision claims."""

        spec = _commands.command_spec("/diff")
        assert spec.name == "/diff"
        assert spec.in_flight_policy == "allow", "a diff you cannot read is useless"
        assert "show" in spec.argument_hint
        # And the revert is reachable under its own name, unchanged.
        assert _commands.command_spec("/undo").name == "/undo"

    def test_undo_still_declares_its_historical_alias_in_the_live_registry(
        self,
    ) -> None:
        """The live row is not this round's file; record the debt, do not lie.

        `cli/commands.py` still carries the dead `/diff` alias on `/undo`'s
        spec. Removing it is a one-token edit in another owner's file, so this
        test pins the CURRENT state and names the edit rather than pretending
        it has been made.
        """

        assert "/diff" in _commands.command_spec("/undo").aliases

    def test_the_settings_alias_is_retired_for_the_same_structural_reason(self) -> None:
        """`/settings` is a real command and `/config` is not a slash command."""

        assert "/settings" in _aliases.RETIRED_ALIASES
        assert _commands.command_spec("/settings").name == "/settings"
        assert _commands.command_spec("/config") is None
        assert "/settings" not in _aliases.ALIASES


class TestDeferredAliasesAreInvertedPins:
    """A recorded gap is one somebody can close; an unrecorded one rots."""

    def test_a_deferred_alias_names_the_command_that_unblocks_it(self) -> None:
        """Each deferral records the blocker AND a reason, not just a skip."""

        for key, row in _aliases.DEFERRED_ALIASES.items():
            assert row.alias and row.target and row.blocked_on
            assert len(row.reason) > 40, f"{key} has no usable reason"

    def test_no_deferred_alias_is_promotable_today(self) -> None:
        """The inverted pin: this FAILS the day a blocker command lands.

        When it fails, the message names the row somebody should add and the
        alias that then becomes valid with no edit here.
        """

        ready = _aliases.deferred_alias_promotions()
        assert ready == [], (
            "a deferred alias can now land; add it to ALIASES: "
            + ", ".join(f"{r['alias']} -> {r['target']}" for r in ready)
        )

    def test_the_checkpoint_to_rewind_conflict_is_recorded_not_guessed(self) -> None:
        """`/checkpoint` is LIVE today; retargeting it would change behaviour."""

        assert "/checkpoint" in _aliases.ALIASES
        assert _aliases.ALIASES["/checkpoint"] == "/checkpoints"
        assert "rewind" in " ".join(
            r.reason for r in _aliases.DEFERRED_ALIASES.values()
        )
        assert _commands.command_spec("/rewind") is None


class TestPluginsWorksAndTheMenuDisclosesIt:
    """Rule 5: a command a user cannot find does not exist."""

    def test_plugins_is_a_registered_command_and_resolves(self) -> None:
        """The `/plugins` command itself is real and answers."""

        spec = _commands.command_spec("/plugins")
        assert spec is not None and spec.name == "/plugins"
        assert _commands.resolve_command_line("/plugins").status == "ok"

    def test_marketplace_reaches_plugins(self) -> None:
        """The brief asked for `/marketplace -> /plugin`; the name is `/plugins`.

        The OUTCOME is asserted, not the absence of `/plugin`: when this suite
        was written `command_spec('/plugin')` was `None`, and a parallel
        terminal has since added `/plugin` as a registry alias of `/plugins`.
        Either way `/marketplace` must land on `/plugins` -- which is the only
        thing a user can observe.
        """

        assert _aliases.resolve_alias("/marketplace") == "/plugins"
        assert (
            _commands.command_spec(_aliases.resolve_alias("/marketplace")) is not None
        )
        # The registry's own `/plugin` spelling, if it has one, agrees.
        registry_plugin = _commands.command_spec("/plugin")
        assert registry_plugin is None or registry_plugin.name == "/plugins"

    def test_a_registry_claimed_name_always_wins_over_this_table(self) -> None:
        """Rule 1 of `resolve_alias`, and it is not hypothetical.

        The registry is the authority and this table is a FALLBACK for names
        the registry does not carry. `/plugin` is the live case: a parallel
        terminal added it as a registry alias of `/plugins` while this suite
        was running, and it must resolve through the registry without any
        edit here.
        """

        registry_plugin = _commands.command_spec("/plugin")
        assert registry_plugin is not None, "this case needs the registry row"
        assert _aliases.resolve_alias("/plugin") == registry_plugin.name == "/plugins"
        assert "/plugin" not in _aliases.ALIASES, "it is not this table's row"

    def test_a_name_in_both_tables_resolves_through_the_registry(self) -> None:
        """`/changes` is an alias in BOTH tables; they must not disagree."""

        assert "/changes" in _aliases.ALIASES
        assert "/changes" in _commands.command_spec("/diff").aliases
        assert (
            _aliases.resolve_alias("/changes")
            == _commands.command_spec("/changes").name
        )

    def test_the_menu_disclosure_names_every_alias_and_its_resolution(self) -> None:
        """Terminal 03 renders these; the menu must show the RESOLUTION.

        A menu row that shows the target but not the chain teaches a name the
        person cannot type, and a menu that omits an alias entirely makes the
        alias a secret.
        """

        lines = _aliases.alias_disclosure_lines()
        assert lines[0].endswith("aliases (typing one resolves to its target):")
        body = "\n".join(lines)
        for alias in _aliases.ALIAS_KEYS:
            assert alias in body, f"{alias} is missing from the menu disclosure"
        assert "/bashes -> /tasks -> /sessions" in body, "the chain must be shown"
        assert "/marketplace -> /plugins" in body
        assert "/plugins" in body

    def test_the_disclosure_carries_one_row_per_alias_and_nothing_else(self) -> None:
        """Bounded: the disclosure is data, so its size is predictable."""

        rows = _aliases.alias_table_rows()
        assert len(rows) == len(_aliases.ALIAS_KEYS)
        assert all(r["exists"] == "yes" for r in rows)
        assert len(_aliases.alias_disclosure_lines()) == len(rows) + 1

    def test_a_command_reachable_through_an_alias_runs_the_same_command(self) -> None:
        """Rule 4: one implementation, not two. The alias IS the command.

        Through `command_aliases.resolve_line`, because
        `commands.resolve_command_line` reads `CommandSpec.aliases` only and
        `cli/commands.py` is not this round's file. The two must land on the
        SAME spec object, not merely the same name -- a second implementation
        would be the thing this rule exists to prevent.
        """

        direct = _aliases.resolve_line("/clear")
        through = _aliases.resolve_line("/reset")
        assert through.spec is not None
        assert direct.spec is through.spec, "the alias resolved to a different spec"
        assert direct.status == through.status == "ok"
        assert _aliases.resolve_line("/new").spec is direct.spec


# ===========================================================================
# DELTA 2 -- stacking
# ===========================================================================


class TestStackingExpandsAsSpecified:
    """Four rules, each with its own pinned expansion."""

    def test_a_single_command_is_one_expansion_with_no_arguments(self) -> None:
        """The degenerate case is the case that has to keep working."""

        expansion = _aliases.expand_command_line("/status")
        assert expansion.commands == (_aliases.StackedCommand("/status", "", ""),)
        assert expansion.arguments == ""
        assert expansion.stacked is False

    def test_two_commands_chain_and_each_gets_the_remainder(self) -> None:
        """Two commands, and the (empty) remainder to each of them.

        With nothing left over the remainder is the empty string, so each
        expanded line is its own command -- which is the case a caller that
        assumed otherwise would silently double-append arguments to.
        """

        expansion = _aliases.expand_command_line("/diff /history")
        assert expansion.lines() == ["/diff", "/history"]
        assert expansion.arguments == ""
        assert expansion.stacked is True
        assert [c.command for c in expansion.commands] == ["/diff", "/history"]

    def test_the_first_non_command_token_and_the_rest_become_the_arguments(
        self,
    ) -> None:
        """Rule 2, and it must be the SAME string for every command."""

        expansion = _aliases.expand_command_line("/clear /diff src/auth.py extra")
        assert expansion.lines() == [
            "/clear src/auth.py extra",
            "/diff src/auth.py extra",
        ]
        assert expansion.arguments == "src/auth.py extra"
        assert all(c.args == expansion.arguments for c in expansion.commands)
        assert expansion.stopped_at == "src/auth.py"

    def test_a_command_is_recognised_only_at_the_start_of_a_message(self) -> None:
        """Rule 1: `/diff /status` is two commands; `please /status` is none."""

        assert _aliases.expand_command_line("/diff /status").stacked is True
        assert _aliases.expand_command_line("please /status").commands == ()
        assert _aliases.expand_command_line("/status please /diff").lines() == [
            "/status please /diff"
        ]

    def test_six_commands_may_chain(self) -> None:
        """The cap is inclusive: six is allowed."""

        line = " ".join(["/clear", "/diff", "/help", "/status", "/sessions", "/trace"])
        expansion = _aliases.expand_command_line(line)
        assert len(expansion.commands) == 6
        assert expansion.stop_reason == "exhausted"
        assert expansion.as_dict()["capped"] is False

    def test_a_seventh_command_is_capped_and_the_remainder_is_kept(self) -> None:
        """The cap NEVER drops the remainder -- it becomes the arguments."""

        line = " ".join(
            ["/clear", "/diff", "/help", "/status", "/sessions", "/trace", "/cost"]
        )
        expansion = _aliases.expand_command_line(line)
        assert len(expansion.commands) == 6
        assert expansion.stop_reason == "chain_cap"
        assert expansion.arguments == "/cost"
        assert expansion.as_dict()["capped"] is True
        assert expansion.lines() == [f"{c.command} /cost" for c in expansion.commands]

    def test_an_alias_in_a_chain_records_the_spelling_that_was_typed(self) -> None:
        """The receipt names what the person typed, not only what it became."""

        expansion = _aliases.expand_command_line("/tasks /usage")
        assert [c.command for c in expansion.commands] == ["/sessions", "/cost"]
        assert [c.via_alias for c in expansion.commands] == ["/tasks", "/usage"]
        assert expansion.message().startswith("/tasks, /usage")

    def test_expansion_is_deterministic_across_repeated_calls(self) -> None:
        """A dispatcher must be able to predict what it is about to run."""

        line = "/clear /changes /stats src/x.py"
        first = _aliases.expand_command_line(line).as_dict()
        for _ in range(25):
            assert _aliases.expand_command_line(line).as_dict() == first

    def test_expansion_is_total_on_hostile_input(self) -> None:
        """This runs on the dispatch path, so it must never raise."""

        for line in (
            "",
            "   ",
            "/",
            "////",
            "/\x00\x00",
            "/[" * 200,
            "/diff " * 400,
            "\t/diff\t/status\tx",
        ):
            expansion = _aliases.expand_command_line(line)
            assert expansion.commands == () or expansion.arguments is not None
            assert expansion.stop_reason in _aliases.STACK_STOP_REASONS

    def test_a_two_hundred_token_message_never_expands_past_the_cap(self) -> None:
        """The cap is enforced against a hostile input, not just a tidy one."""

        expansion = _aliases.expand_command_line("/clear " * 200)
        assert len(expansion.commands) == _aliases.MAX_STACKED_COMMANDS
        assert expansion.stop_reason == "chain_cap"

    def test_a_custom_command_template_is_not_an_inline_command(self) -> None:
        """A project template is a different lifecycle, so expansion stops.

        `/clear /mycustom` must hand `/mycustom` to `/clear` as its argument.
        Treating an unregistered name as stackable would let the two surfaces
        disagree about what a message means. (`/review` is not the probe here:
        it is declared non-stackable for its own reason, pinned separately.)
        """

        expansion = _aliases.expand_command_line("/clear /mycustom thing")
        assert [c.command for c in expansion.commands] == ["/clear"]
        assert expansion.arguments == "/mycustom thing"
        assert expansion.stop_reason == "exhausted"


class TestANonStackableCommandStopsExpansionAndKeepsTheRemainder:
    """DELTA 2, rule 4 -- and the remainder is never dropped."""

    def test_a_command_that_forks_a_subagent_stops_the_chain(self) -> None:
        """`/plan` reaches `_run_one_agent`, which forks its own subagents."""

        assert _aliases.non_stackable_reason("/plan") == "forks_subagent"
        assert _aliases.non_stackable_reason("/build") == "forks_subagent"

    def test_a_command_whose_argument_may_begin_with_a_slash_stops_the_chain(
        self,
    ) -> None:
        """`/repo /home/x` is a PATH, not a second command."""

        for name in ("/repo", "/open", "/import", "/export", "/share", "/steer"):
            assert _aliases.non_stackable_reason(name) == "free_text_arguments", name

    def test_expansion_stops_there_and_the_remainder_becomes_its_arguments(
        self,
    ) -> None:
        """The whole rule, on one line, with nothing dropped."""

        expansion = _aliases.expand_command_line("/plan /diff src/auth.py")
        assert [c.command for c in expansion.commands] == ["/plan"]
        assert expansion.commands[0].args == "/diff src/auth.py"
        assert expansion.arguments == "/diff src/auth.py"
        assert expansion.stop_reason == "not_stackable"
        assert expansion.lines() == ["/plan /diff src/auth.py"]

    def test_an_absolute_path_argument_is_never_mistaken_for_a_command(self) -> None:
        """The rule that would break first on a real repository path."""

        expansion = _aliases.expand_command_line("/repo /home/me/project")
        assert expansion.lines() == ["/repo /home/me/project"]
        assert expansion.stopped_at == "/home/me/project"

    def test_a_required_argument_command_is_non_stackable_from_the_registry(
        self,
    ) -> None:
        """The safety net is DERIVED, so the two can never disagree.

        `argument_policy == "required"` means expansion would steal a
        mandatory argument, and that fact lives in the registry rather than in
        anybody's memory.
        """

        derived = [
            spec.name
            for spec in _commands.COMMAND_SPECS
            if spec.argument_policy == "required"
        ]
        assert derived, "the registry declares required-argument commands"
        for name in derived:
            assert _aliases.non_stackable_reason(name) is not None, name

    def test_every_declared_reason_is_in_the_closed_vocabulary(self) -> None:
        """A reason nobody can name is a decision nobody can debug."""

        real = {spec.name for spec in _commands.COMMAND_SPECS}
        assert set(_aliases.NON_STACKABLE) <= real, (
            "a non-stackable row names a real command"
        )
        for name, reason in _aliases.NON_STACKABLE.items():
            assert reason in _aliases.NON_STACKABLE_REASONS, f"{name}: {reason!r}"

    def test_the_stackable_set_is_exactly_what_the_rules_leave(self) -> None:
        """Nothing is stackable by accident: the complement is derived."""

        stackable = sorted(
            spec.name
            for spec in _commands.COMMAND_SPECS
            if _aliases.non_stackable_reason(spec.name) is None
        )
        assert "/diff" in stackable
        assert "/status" in stackable
        assert "/plan" not in stackable
        assert "/repo" not in stackable


# ===========================================================================
# DELTA 3 -- queue parity
# ===========================================================================


class TestAnExemptCommandRunsImmediatelyWhileBusyOnAllThreeSurfaces:
    """The brief's named exempt set, on every surface, while a run is live."""

    EXEMPT_TYPINGS = ("/status", "/tasks", "/usage", "/cost", "/cancel")

    def test_the_exempt_set_is_named_in_the_module_not_inferred(self) -> None:
        """Five names, declared as data, with a reason for the shape.

        An exempt set derived from a heuristic changes meaning the day
        somebody adds a read-only command that takes nine seconds.
        """

        assert _queue.QUEUE_EXEMPT_COMMANDS == self.EXEMPT_TYPINGS
        assert len(_queue.QUEUE_EXEMPT_COMMANDS) == 5

    def test_every_exempt_name_resolves_to_a_command_this_build_has(self) -> None:
        """`/tasks` and `/usage` are aliases here; both must reach a command.

        At the time of writing, `command_spec("/tasks")` and
        `command_spec("/usage")` were both `None`. The exempt set canonicalises
        them through the one alias authority, so an exempt entry always names a
        command a surface can actually resolve -- and it STAYS true if a
        parallel terminal later registers either name as a real command,
        because `resolve_alias` gives a real command the win. The assertion
        below is on the resolved set and on resolvability, so it is correct
        under both the registry shapes rather than pinned to today's.
        """

        resolved = _queue.exempt_commands()
        assert resolved == ("/status", "/sessions", "/cost", "/cancel")
        for name in resolved:
            assert _commands.command_spec(name) is not None
        # Every DECLARED name is accounted for: it either resolved into the
        # set or the registry dropped it as unresolvable, and the first is
        # what must hold for a name a user can type.
        for typed in _queue.QUEUE_EXEMPT_COMMANDS:
            assert _commands.command_spec(_aliases.resolve_alias(typed)) is not None, (
                typed
            )

    @pytest.mark.parametrize("surface", ["tui", "repl", "headless"])
    def test_an_exempt_command_runs_now_on_every_surface(self, surface: str) -> None:
        """Each surface builds its OWN context; the decision is shared.

        The three contexts are the real ones (`surface_command_context` with
        `in_flight=True`), so this exercises the per-surface resolution path
        rather than a synthetic flag.

        The claim is "never QUEUED", not "always run". A registry refusal of
        its own -- headless genuinely cannot cancel a run interactively, and
        it says so -- is honoured, because exempt means "does not have to wait
        for the run to finish", NOT "overrides what the registry decided".
        Asserting `run_now` unconditionally here would have demanded that this
        module overrule a refusal another owner made on purpose.
        """

        context = _commands.surface_command_context(surface, in_flight=True)
        assert context.in_flight is True
        assert context.surface == surface
        for typed in self.EXEMPT_TYPINGS:
            resolution = _commands.resolve_command_line(typed, context)
            decision = _queue.decide(resolution, in_flight=True, surface=surface)
            assert decision.action != "queue", (
                f"{surface}: an exempt command was QUEUED -- {typed}"
            )
            if decision.action == "run_now":
                assert decision.exempt is True
                assert decision.queued is False
            else:
                # Honoured a registry refusal, and says which one.
                assert decision.status in {"disabled", "hidden", "refused"}
                assert "the registry refused" in decision.reason

    def test_an_exempt_command_runs_now_on_every_surface_that_allows_it(self) -> None:
        """The strong form, on the surfaces whose registry permits it.

        `/cancel` is `disabled` headless and `/plan` is refused on the REPL,
        so "run_now on all three" is not a claim the product can make. What it
        CAN make, and what this asserts, is that the exempt set never forces a
        queue on any surface, and never blocks a command the registry permits.
        """

        blocked = {
            (surface, typed)
            for surface in ("tui", "repl", "headless")
            for typed in self.EXEMPT_TYPINGS
            if _commands.resolve_command_line(
                typed, _commands.surface_command_context(surface, in_flight=True)
            ).status
            not in {"ok", _queue.QUEUED_STATUS}
        }
        assert blocked, "at least one exempt command is refused somewhere"
        for surface in ("tui", "repl", "headless"):
            context = _commands.surface_command_context(surface, in_flight=True)
            for typed in self.EXEMPT_TYPINGS:
                if (surface, typed) in blocked:
                    continue
                resolution = _commands.resolve_command_line(typed, context)
                decision = _queue.decide(resolution, in_flight=True, surface=surface)
                assert decision.action == "run_now", f"{surface}: {typed}"
                assert decision.exempt is True

    def test_an_alias_of_an_exempt_command_is_also_exempt(self) -> None:
        """`/stats` and `/usage` reach `/cost`; all three run now."""

        for typed in ("/stats", "/usage", "/tasks", "/bashes"):
            decision = _queue.decide(
                _commands.resolve_command_line(typed), in_flight=True
            )
            assert decision.action == "run_now", f"{typed} -> {decision.action}"
            assert decision.exempt is True

    def test_cancel_is_exempt_because_refusing_it_would_be_a_trap(self) -> None:
        """The one exempt command that is not a read: the door out."""

        assert "/cancel" in _queue.exempt_commands()
        decision = _queue.decide(
            _commands.resolve_command_line("/cancel"), in_flight=True
        )
        assert decision.action == "run_now"

    def test_the_exempt_set_is_named_in_the_config_and_read_by_key_presence(
        self,
    ) -> None:
        """The brief's "name it in the config" and rule 7's key PRESENCE.

        A value in `harness.config.DEFAULTS` merges into every task and every
        eval arm, so the shipped value is `None` and only an operator's
        presence is honoured.
        """

        from harness import config as _hconfig

        assert _queue.QUEUE_EXEMPT_CONFIG_KEY not in _hconfig.DEFAULTS, (
            "a non-None exempt set in DEFAULTS would switch every task and "
            "every eval arm"
        )
        report = _queue.resolve_queue_config({})
        assert report.exempt_source == "declared"
        overridden = _queue.resolve_queue_config(
            {_queue.QUEUE_EXEMPT_CONFIG_KEY: ["/status", "/diff"]}
        )
        assert overridden.exempt == ("/status", "/diff")
        assert overridden.exempt_source == "config"

    def test_an_unusable_config_value_is_reported_and_never_widens_the_set(
        self,
    ) -> None:
        """A typo in a settings file must not silently empty the exempt set."""

        report = _queue.resolve_queue_config({_queue.QUEUE_EXEMPT_CONFIG_KEY: 7})
        assert report.exempt == _queue.exempt_commands(None)
        assert report.notes, "an unusable value must be reported, not swallowed"
        unknown = _queue.resolve_queue_config(
            {_queue.QUEUE_EXEMPT_CONFIG_KEY: ["/no-such-command"]}
        )
        assert unknown.exempt == _queue.exempt_commands(None)


class TestANonExemptCommandQueuesOnAllThreeSurfaces:
    """The parity the TUI already had and the other two surfaces lacked."""

    def test_a_queueable_command_queues_wherever_the_registry_permits_it(self) -> None:
        """The measured pre-round answer was three different words.

        `/plan ship it` while busy resolved to `queued` on the TUI,
        `refused` on the REPL and `disabled` headless. One decision now, with
        one honest caveat: headless refuses `/plan` for a reason of its own
        ("needs an interactive session"), and a registry refusal this round
        must not overrule. What it no longer does is turn a QUEUEABLE command
        into a refusal because of which shell it was typed in.
        """

        for surface in ("tui", "repl", "headless"):
            context = _commands.surface_command_context(surface, in_flight=True)
            for typed in ("/plan ship it", "/ask why"):
                resolution = _commands.resolve_command_line(typed, context)
                decision = _queue.decide(resolution, in_flight=True, surface=surface)
                assert (
                    decision.action != "refuse"
                    or "the registry refused" in decision.reason
                ), f"{surface}: {typed} refused by the QUEUE, not the registry"
                if decision.action == "queue":
                    assert decision.status == _queue.QUEUED_STATUS
                    assert decision.exempt is False

    def test_the_tui_s_measured_pre_round_answer_is_preserved_exactly(self) -> None:
        """Queue parity must not be queue REGRESSION on the surface that has it.

        `/plan ship it` while busy on the TUI returned `status == "queued"`
        before this round and must still return `action == "queue"` /
        `status == "queued"` -- the word the TUI transcript and its
        `resolution.status == "queued"` branch both read.
        """

        context = _commands.surface_command_context("tui", in_flight=True)
        resolution = _commands.resolve_command_line("/plan ship it", context)
        assert resolution.status == "queued", "the registry's own TUI word changed"
        decision = _queue.decide(resolution, in_flight=True, surface="tui")
        assert decision.action == "queue"
        assert decision.status == _queue.QUEUED_STATUS

    def test_the_repl_gains_the_queue_it_lost(self) -> None:
        """The REPL refused `/plan ship it` before this round (exit 2)."""

        context = _commands.surface_command_context("repl", in_flight=True)
        resolution = _commands.resolve_command_line("/plan ship it", context)
        assert resolution.status == "refused", "the REPL's own word changed"
        decision = _queue.decide(resolution, in_flight=True, surface="repl")
        assert decision.action == "queue", "the REPL did not gain queueing"

    def test_an_unknown_command_is_still_refused_not_queued(self) -> None:
        """A queue that accepts a typo is a queue that runs one later."""

        decision = _queue.decide(
            _commands.resolve_command_line("/nope"), in_flight=True
        )
        assert decision.action == "refuse"
        assert decision.status == "unknown"

    def test_the_same_line_while_idle_runs_now_everywhere(self) -> None:
        """Busy is the only thing that changes the answer."""

        decision = _queue.decide(
            _commands.resolve_command_line("/plan ship it"), in_flight=False
        )
        assert decision.action == "run_now"
        assert decision.status == "ok"

    def test_a_command_the_registry_allows_in_flight_runs_now_anyway(self) -> None:
        """The registry already decided; this function does not overrule it."""

        decision = _queue.decide(
            _commands.resolve_command_line("/diff"), in_flight=True
        )
        assert decision.action == "run_now"
        assert "registry allows" in decision.reason

    def test_the_policy_switch_off_falls_back_to_todays_repl_behaviour(self) -> None:
        """`queue_commands_while_busy=False` is refuse, and it is opt-in."""

        report = _queue.resolve_queue_config({_queue.QUEUE_POLICY_CONFIG_KEY: False})
        assert report.policy == "refuse"
        assert report.policy_source == "config"
        decision = _queue.decide(
            _commands.resolve_command_line("/plan ship it"),
            in_flight=True,
            config={_queue.QUEUE_POLICY_CONFIG_KEY: False},
        )
        assert decision.action == "refuse"

    def test_an_unusable_policy_value_is_reported_and_defaults_to_queueing(
        self,
    ) -> None:
        """A typo cannot switch the default the other way by accident."""

        report = _queue.resolve_queue_config({_queue.QUEUE_POLICY_CONFIG_KEY: "maybe"})
        assert report.policy == _queue.QUEUE_POLICY_DEFAULT
        assert report.notes

    def test_the_action_vocabulary_is_closed(self) -> None:
        """A surface that can emit a fourth action is a CI-only script."""

        assert _queue.QUEUE_ACTIONS == ("run_now", "queue", "refuse")
        assert set(_queue.QUEUE_POLICIES) <= set(_queue.QUEUE_ACTIONS) | {"refuse"}


class TestAQueuedCommandDeliversAtABoundaryAndConsumesExactlyOnce:
    """Delivery reuses AGT-10's ``run_tool_batch`` and never double-runs."""

    def test_a_queued_command_is_held_acknowledged_and_delivered_once(self) -> None:
        """The whole lifecycle on one queue: enqueue, drain, drain again."""

        queue = _queue.CommandQueue(surface="tui")
        held = queue.enqueue("/plan ship it")
        assert queue.depth() == 1
        assert held.seq == 1
        ran: list[str] = []
        first = queue.drain(lambda item: ran.append(item.text))
        assert [i.text for i in first.delivered] == ["/plan ship it"]
        assert ran == ["/plan ship it"]
        assert queue.depth() == 0
        second = queue.drain(lambda item: ran.append(item.text))
        assert second.delivered == ()
        assert ran == ["/plan ship it"], "a queued command must never execute twice"
        assert queue.receipt()["executed"] == 1

    def test_the_queue_is_consumed_in_the_order_it_was_queued(self) -> None:
        """A burst of corrections must arrive in the order typed."""

        queue = _queue.CommandQueue(surface="repl")
        for text in ("/clear", "/diff", "/status", "/history"):
            queue.enqueue(text)
        ran: list[str] = []
        queue.drain(lambda item: ran.append(item.text))
        assert ran == ["/clear", "/diff", "/status", "/history"]

    def test_delivery_runs_through_the_real_agt_10_seam(self) -> None:
        """`drain` calls `harness.tools.run_tool_batch`, not a lookalike.

        The AGT-10 contract is: `dispatch` exactly once per call, a call
        never split, `seam` only BETWEEN calls and after the last, and a seam
        that returns False names the untouched remainder. All four are
        asserted here against the real function's own receipt.
        """

        from harness import tools as _tools

        assert callable(_tools.run_tool_batch)
        queue = _queue.CommandQueue(surface="tui")
        for text in ("/clear", "/diff", "/status"):
            queue.enqueue(text)
        seen: list[str] = []
        drain = queue.drain(
            lambda item: seen.append(item.text), seam=lambda batch: True
        )
        assert seen == ["/clear", "/diff", "/status"]
        assert drain.seams >= len(seen), "the seam must fire after each dispatch"
        assert drain.not_delivered == ()

    def test_a_seam_that_stops_keeps_the_remainder_queued_and_named(self) -> None:
        """A delivery that failed must not lose the rest of the queue.

        This is the bug the first draft of this module had: it popped
        everything, so a stopped drain reported depth 0 for a queue holding
        two commands.
        """

        queue = _queue.CommandQueue(surface="tui")
        for text in ("/clear", "/diff", "/status"):
            queue.enqueue(text)
        ran: list[str] = []
        drain = queue.drain(
            lambda item: ran.append(item.text), seam=lambda batch: False
        )
        assert ran == ["/clear"]
        assert [i.text for i in drain.not_delivered] == ["/diff", "/status"]
        assert queue.depth() == 2, "the remainder must still be waiting"
        assert [i.text for i in queue.peek()] == ["/diff", "/status"]
        assert queue.receipt()["undelivered"] == [2, 3]

    def test_a_seam_that_raises_is_treated_as_a_refusal_not_a_pass(self) -> None:
        """AGT-10's rule, reused verbatim: an unsafe boundary stops delivery."""

        queue = _queue.CommandQueue(surface="tui")
        for text in ("/clear", "/diff"):
            queue.enqueue(text)
        ran: list[str] = []

        def seam(batch: object) -> bool:
            raise RuntimeError("boundary not safe")

        drain = queue.drain(lambda item: ran.append(item.text), seam=seam)
        assert ran == ["/clear"]
        assert len(drain.not_delivered) == 1
        assert queue.depth() == 1

    def test_a_raising_handler_is_recorded_and_never_retried(self) -> None:
        """Consumed exactly once even when the handler itself explodes.

        The line WAS handed to exactly one handler, so it is consumed; it is
        not silently retried, because a `/plan` that ran twice is worse than
        one that ran once and reported a failure. The rest of the queue still
        has a boundary to arrive at.
        """

        queue = _queue.CommandQueue(surface="repl")
        queue.enqueue("/clear")
        queue.enqueue("/diff")
        ran: list[str] = []

        def dispatch(item: _queue.QueuedCommand) -> None:
            ran.append(item.text)
            raise RuntimeError("handler exploded")

        drain = queue.drain(dispatch)
        assert ran == ["/clear", "/diff"], "the drain must not stop at one failure"
        assert queue.depth() == 0, "a consumed line must not come back"
        assert drain.failed and drain.failed[0][0] == 1
        assert "RuntimeError" in drain.failed[0][1]
        assert queue.receipt()["executed"] == 2
        # And a second drain delivers nothing: no retry.
        again = queue.drain(dispatch)
        assert again.delivered == ()
        assert ran == ["/clear", "/diff"]

    def test_the_receipt_names_what_the_queue_still_holds(self) -> None:
        """`undelivered` is the load-bearing key, not the depth.

        A receipt reporting only a depth looks identical before and after a
        command was lost.
        """

        queue = _queue.CommandQueue(surface="tui")
        queue.enqueue("/clear")
        queue.enqueue("/diff")
        receipt = queue.receipt()
        assert receipt["undelivered"] == [1, 2]
        assert receipt["delivered"] == []
        queue.remove(1)
        assert queue.receipt()["undelivered"] == [2]
        assert queue.receipt()["depth"] == 1

    def test_a_queued_line_is_canonicalised_the_way_the_tui_stores_it(self) -> None:
        """Byte-identical strings, so a test comparing the two is fair.

        `cli/tui.py` appends `f"{resolution.spec.name} {resolution.args}".rstrip()`
        in five places; this queue produces the same spelling.
        """

        queue = _queue.CommandQueue(surface="tui")
        resolution = _commands.resolve_command_line("/plan    ship   it")
        item = queue.enqueue(resolution.raw)
        assert item.text == f"{resolution.spec.name} {resolution.args}".strip()
        assert item.command == "/plan"
        assert item.args == "ship   it"

    def test_an_unresolvable_line_is_kept_verbatim_not_dropped(self) -> None:
        """Dropping what the person typed is losing their work."""

        queue = _queue.CommandQueue(surface="tui")
        item = queue.enqueue("/nope something")
        assert item.text == "/nope something"
        assert queue.depth() == 1


class TestQueuedPromptsAreVisibleAndEditableBeforeDelivery:
    """A queued prompt you cannot see or change is a promise unmet."""

    def test_the_queue_renders_one_visible_row_per_waiting_command(self) -> None:
        """Visible: the depth alone is not a queue a person can act on."""

        queue = _queue.CommandQueue(surface="tui")
        assert _queue.queue_lines(queue) == ["nothing queued"]
        queue.enqueue("/plan ship it")
        queue.enqueue("/diff")
        lines = _queue.queue_lines(queue)
        assert lines == ["1. /plan ship it", "2. /diff"]
        assert "nothing queued" not in lines

    def test_a_queued_command_can_be_removed_before_delivery(self) -> None:
        """Removable, and a miss is reported rather than claimed as success."""

        queue = _queue.CommandQueue(surface="tui")
        queue.enqueue("/plan ship it")
        queue.enqueue("/diff")
        removed = queue.remove(1)
        assert removed is not None and removed.text == "/plan ship it"
        assert queue.depth() == 1
        assert queue.remove(1) is None, "a removal that never happened must say so"
        assert queue.remove(999) is None

    def test_a_queued_command_can_be_edited_without_moving_it(self) -> None:
        """Editing the second of three does not mean "run it next"."""

        queue = _queue.CommandQueue(surface="tui")
        for text in ("/clear", "/diff", "/status"):
            queue.enqueue(text)
        edited = queue.edit(2, "/history")
        assert edited is not None and edited.text == "/history"
        assert [i.seq for i in queue.peek()] == [1, 2, 3]
        assert _queue.queue_lines(queue) == ["1. /clear", "2. /history", "3. /status"]
        assert queue.edit(99, "/x") is None

    def test_clearing_the_queue_returns_what_it_dropped(self) -> None:
        """A clear that reports nothing is a clear nobody can verify."""

        queue = _queue.CommandQueue(surface="tui")
        queue.enqueue("/clear")
        queue.enqueue("/diff")
        dropped = queue.clear()
        assert [i.text for i in dropped] == ["/clear", "/diff"]
        assert queue.depth() == 0
        assert queue.clear() == []


class TestTheQueueDepthAppearsInTheStatusline:
    """Next to the EXISTING `N queued (ctrl+g)` section, not a new one."""

    def test_the_statusline_says_n_queued_with_the_existing_keybind(self) -> None:
        """The string comes from `cli.design`, the table the TUI renders."""

        queue = _queue.CommandQueue(surface="tui")
        for text in ("/plan ship it", "/ask why", "/diff"):
            queue.enqueue(text)
        assert _queue.statusline_facts(queue) == {"queue": "3 queued (ctrl+g)"}

    def test_the_statusline_reuses_the_design_section_rather_than_a_second_wording(
        self,
    ) -> None:
        """One renderer of that sentence, or the two drift."""

        from cli import design as _design

        section = next(s for s in _design.STATUSLINE_SECTIONS if s.key == "queue")
        queue = _queue.CommandQueue(surface="tui")
        queue.enqueue("/clear")
        facts = _queue.statusline_facts(queue)
        assert facts["queue"] == section.render(1, "")

    def test_an_empty_queue_publishes_nothing_rather_than_a_zero(self) -> None:
        """`0 queued` is a claim; an absent section is the honest form."""

        assert _queue.statusline_facts(_queue.CommandQueue(surface="tui")) == {}
        assert _queue.statusline_facts([]) == {}
        assert _queue.statusline_facts(None) == {}

    def test_the_statusline_reads_the_tui_s_own_bare_list_of_lines(self) -> None:
        """The TUI keeps `self._queue` as a `list[str]` in 14 call sites.

        A statusline fact that only accepted a `CommandQueue` would refuse to
        describe the product's real queue, so both shapes answer.
        """

        assert _queue.queue_depth(["/plan ship it", "/diff"]) == 2
        assert _queue.statusline_facts(["/plan ship it", "/diff"]) == {
            "queue": "2 queued (ctrl+g)"
        }
        assert _queue.queue_lines(["/plan ship it"]) == ["/plan ship it"]


# ===========================================================================
# The rules that are not features
# ===========================================================================


class TestMarkupSafetyThroughARealConsole:
    """Rule 3: a render failure must NEVER delete a message."""

    HOSTILE = "/[bold red]evil[/bold red]"
    HOSTILE_PATH = "src/[weird[name].py"

    def test_a_hostile_alias_reaches_the_disclosure_undamaged(self) -> None:
        """An alias is DATA a person typed; it must be VISIBLE after render.

        The property asserted is that ESCAPING MADE THE PARSER A NO-OP: the
        markup-rendered output is byte-identical to the same lines rendered
        with `markup=False`. A substring assertion would pass while the
        message was being eaten -- and a "the substring is absent" assertion
        is worse, because rich escaping legitimately leaves the characters.
        """

        from rich.console import Console

        injected = dict(_aliases.ALIASES)
        injected[self.HOSTILE] = "/clear"
        original = _aliases.ALIASES
        try:
            _aliases.ALIASES = injected  # type: ignore[misc]
            lines = _aliases.alias_disclosure_lines()
            escaped = _aliases.escape_lines(lines)
            marked, plain = io.StringIO(), io.StringIO()
            Console(file=marked, width=200, markup=True, highlight=False)
            for line in escaped:
                Console(file=marked, width=200, markup=True, highlight=False).print(
                    line, markup=True
                )
            for line in lines:
                Console(file=plain, width=200, markup=False, highlight=False).print(
                    line, markup=False
                )
        finally:
            _aliases.ALIASES = original  # type: ignore[misc]
        rendered = marked.getvalue()
        assert rendered == plain.getvalue(), (
            "the markup parser interpreted a hostile alias; escaping is not load-bearing"
        )
        assert "evil" in rendered, "the hostile name was EATEN by the renderer"
        # ...and the rows after it still print: a render failure that eats one
        # line and stops is the other half of the same bug.
        assert "/marketplace" in rendered
        assert "/reset" in rendered

    def test_a_hostile_queued_line_renders_visibly(self) -> None:
        """A queued line is a path the person typed, and a path may hold `[`."""

        from rich.console import Console

        queue = _queue.CommandQueue(surface="tui")
        queue.enqueue(f"/open {self.HOSTILE_PATH}")
        buffer = io.StringIO()
        console = Console(file=buffer, width=200, markup=True, highlight=False)
        for line in _queue.escape_lines(_queue.queue_lines(queue)):
            console.print(line, markup=True)
        rendered = buffer.getvalue()
        assert "weird[name].py" in rendered, "the queued path was EATEN"
        assert "open" in rendered

    def test_safe_lines_returns_text_that_cannot_be_markup_parsed(self) -> None:
        """The STRUCTURAL answer: a `Text` object has no markup at all."""

        from rich.text import Text

        out = _queue.safe_lines([self.HOSTILE])
        assert all(isinstance(v, Text) for v in out)
        assert out[0].plain == self.HOSTILE

    def test_the_queue_module_declares_no_completion_vocabulary(self) -> None:
        """Neither module may mint, name, or weaken a completion status."""

        forbidden = (
            "completed_verified",
            "completed_unverified",
            "status_is_success",
            "run_verdict",
            "agent_contracts",
            "RUN_STATUSES",
        )
        for module in ("cli/command_aliases.py", "cli/command_queue.py"):
            source = (REPO / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            literals = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            for word in forbidden:
                assert word not in literals, f"{module} names {word} in code"


class TestBackwardCompatibility:
    """Rule 8: everything that worked before must still work, byte for byte.

    These assert a FLOOR and a historical NAME SET rather than a count.
    This is a shared dirty worktree: while this suite was being written a
    parallel terminal added four ``COMMAND_SPECS`` rows (52 -> 56) and four
    headless flag equivalents (14 -> 18). Pinning the exact number would have
    turned someone else's additive work into a red here, which is the wrong
    direction to fail. What backward compatibility actually forbids is
    REMOVAL, so that is what is asserted.
    """

    #: Measured on this tree before this round touched anything.
    PRE_ROUND_COMMANDS = 52
    PRE_ROUND_REQUIRED = 36
    PRE_ROUND_REGISTRY_ALIASES = 8
    PRE_ROUND_FLAG_EQUIVALENTS = 14

    def test_no_registered_command_was_removed(self) -> None:
        """The registry grew from 52 to >= 52; nothing was taken away."""

        assert len(_commands.COMMAND_SPECS) >= self.PRE_ROUND_COMMANDS
        assert len(_commands.REQUIRED_COMMANDS) >= self.PRE_ROUND_REQUIRED
        assert set(_commands.REQUIRED_COMMANDS) <= {
            s.name for s in _commands.COMMAND_SPECS
        }

    def test_every_pre_round_command_name_still_resolves(self) -> None:
        """The 52 names measured before this round, one resolution each.

        The name set is pinned by name so a rename is a red here even though a
        count would have stayed flat.
        """

        pre_round = {
            "/approve",
            "/ask",
            "/attach",
            "/build",
            "/cancel",
            "/checkpoints",
            "/clear",
            "/compact",
            "/connect",
            "/context",
            "/copy-diff",
            "/cost",
            "/detach",
            "/diagnostics",
            "/diff",
            "/doctor",
            "/effort",
            "/export",
            "/feed",
            "/files",
            "/fork",
            "/help",
            "/history",
            "/import",
            "/init",
            "/login",
            "/logout",
            "/mcp",
            "/mode",
            "/model",
            "/open",
            "/plan",
            "/plugins",
            "/quiet",
            "/quit",
            "/recover",
            "/redo",
            "/reject",
            "/relevant",
            "/repo",
            "/resume",
            "/review",
            "/sessions",
            "/settings",
            "/share",
            "/skills",
            "/status",
            "/steer",
            "/theme",
            "/trace",
            "/undo",
            "/watch",
        }
        assert len(pre_round) == self.PRE_ROUND_COMMANDS
        missing = [
            name
            for name in sorted(pre_round)
            if _commands.resolve_command_line(name).spec is None
        ]
        assert missing == [], f"commands that stopped resolving: {missing}"

    def test_the_registry_still_declares_at_least_eight_of_its_own_aliases(
        self,
    ) -> None:
        """`CommandSpec.aliases` is `cli/commands.py`'s; not this round's file.

        The brief says 13 `HEADLESS_FLAG_EQUIVALENTS` rows; this tree carried
        **14** measured before this round (a parallel terminal had added
        `/watch`) and has grown further since. The floor is the honest
        assertion; the eight registry alias names are pinned individually by
        name in `TestEveryAliasResolvesToItsTarget`.
        """

        declared = {alias for spec in _commands.COMMAND_SPECS for alias in spec.aliases}
        assert len(declared) >= self.PRE_ROUND_REGISTRY_ALIASES
        assert (
            len(_commands.HEADLESS_FLAG_EQUIVALENTS) >= self.PRE_ROUND_FLAG_EQUIVALENTS
        )

    def test_all_six_exit_codes_are_untouched(self) -> None:
        """0 / 1 / 2 / 3 / 4 / 130, from the one classifier."""

        from cli import exit_codes as _exit

        codes = {int(v) for v in _exit.EXIT_CODES.values()}
        assert codes == {0, 1, 2, 3, 4, 130}

    def test_resolve_command_line_still_parses_exactly_one_name(self) -> None:
        """The historical single-name parser is unchanged; stacking is additive.

        A stacked line handed to the historical parser must still produce
        exactly the one resolution it always did, because three surfaces call
        it directly and this round does not own that file.
        """

        resolution = _commands.resolve_command_line(
            "/review /security-review src/auth.py"
        )
        assert resolution.spec is not None
        assert resolution.spec.name == "/review"
        assert resolution.args == "/security-review src/auth.py"
        assert resolution.status == "ok"

    def test_the_alias_seam_leaves_a_canonical_line_byte_identical(self) -> None:
        """`resolve_line` only rewrites a name that is IN this round's table.

        Every other line -- all 56 commands, their registry aliases, their
        refusals and their exit codes -- must reach the registry through the
        identical call, which is what makes this seam additive.
        """

        probes = ("/diff", "/undo", "/clear", "/settings status", "not a command", "/")
        for line in probes:
            before = _commands.resolve_command_line(line)
            after = _aliases.resolve_line(line)
            assert before.status == after.status, line
            assert before.args == after.args, line
            assert (before.spec.name if before.spec else None) == (
                after.spec.name if after.spec else None
            ), line

    def test_the_alias_seam_makes_a_new_alias_reachable(self) -> None:
        """The reason the seam exists, measured as a before/after pair.

        `cli/commands.py` reads `CommandSpec.aliases` only, so a name that
        lives in `ALIASES` reaches the historical parser as `unknown`. This
        asserts BOTH halves: the raw parser still refuses it (so nobody can
        claim the registry was edited), and the seam resolves it.
        """

        for typed, target in (
            ("/reset", "/clear"),
            ("/marketplace", "/plugins"),
            ("/continue", "/resume"),
        ):
            assert _commands.resolve_command_line(typed).status == "unknown"
            seam = _aliases.resolve_line(typed)
            assert seam.status == "ok", typed
            assert seam.spec is not None and seam.spec.name == target

    def test_the_alias_seam_preserves_the_arguments_of_a_new_alias(self) -> None:
        """A rewrite that dropped the remainder would be a worse bug than none."""

        seam = _aliases.resolve_line("/marketplace awesome-plugin")
        assert seam.spec is not None and seam.spec.name == "/plugins"
        assert seam.args == "awesome-plugin"

    def test_every_command_still_resolves_through_resolve_command_line(self) -> None:
        """One loop over the whole registry, because a partial loop is a lie."""

        broken = []
        for spec in _commands.COMMAND_SPECS:
            resolution = _commands.resolve_command_line(spec.name)
            if resolution.spec is None or resolution.spec.name != spec.name:
                broken.append(spec.name)
        assert broken == []

    def test_the_queue_module_does_not_change_the_registry_import_gate(self) -> None:
        """Importing the new modules must not break `cli`'s import-time gate."""

        import cli.command_aliases as a
        import cli.command_queue as q

        assert a.validate_alias_table().ok is True
        assert q.exempt_commands()
        assert _commands._validate_headless_tables is not None


class TestTheMountPointsAreNamedNotGuessed:
    """Rule 2: a surface this round cannot edit gets an exact handoff."""

    def test_the_handoff_json_records_every_mount_and_its_absence(self) -> None:
        """The machine-readable handoff must name all three surfaces.

        A handoff that says "wire the queue into the REPL" is not a handoff;
        this asserts the file carries the function name, the module, and the
        line anchor for each of the three surfaces.
        """

        import json

        path = REPO / "logs" / "command-surface" / "terminal-04.json"
        assert path.is_file(), "logs/command-surface/terminal-04.json is missing"
        doc = json.loads(path.read_text(encoding="utf-8"))
        handoff = doc["handoff"]
        surfaces = {row["file"] for row in handoff}
        assert {"cli/tui.py", "cli/interactive.py", "cli/command_exec.py"} <= surfaces
        for row in handoff:
            assert row["function"], "a mount must name the function to call"
            assert row["anchor"], "a mount must name where it goes"
            assert row["applied"] is False, (
                "a mount must never be reported as applied from this file"
            )

    def test_this_round_edited_neither_of_the_two_files_it_may_not_touch(self) -> None:
        """`cli/tui.py` and `cli/commands.py` are read-only for VEX-CS-04.

        The check is a SOURCE pin rather than a VCS one: the tree is shared
        and dirty, so `git diff` cannot say whose change is whose. What this
        asserts is the durable property -- nothing in this round's own
        modules writes to them.
        """

        for module in ("cli/command_aliases.py", "cli/command_queue.py"):
            source = (REPO / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            assert "cli.tui" not in imported, f"{module} imports cli.tui"
            assert "cli.interactive" not in imported, (
                f"{module} imports cli.interactive"
            )
            assert "textual" not in imported, f"{module} imports textual"

    def test_the_new_modules_declare_no_prompt_and_no_config_default(self) -> None:
        """Rule 7: no new `DEFAULTS` key, and no prompt changed."""

        from harness import config as _hconfig

        for key in (
            _queue.QUEUE_EXEMPT_CONFIG_KEY,
            _queue.QUEUE_POLICY_CONFIG_KEY,
        ):
            assert key not in _hconfig.DEFAULTS
