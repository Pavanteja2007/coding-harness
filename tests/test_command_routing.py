"""Subcommand routing: three browsers become verbs (VEX-CS-01).

The measured problem this suite pins:

    /plugins   argument_hint="[filter|name]"  result_presentation="browser"
    /mcp       argument_hint="[server-label]" result_presentation="browser"
    /skills    argument_hint="[filter]"       result_presentation="browser"

You could LOOK at a plugin, a connector and a skill. You could not install
one. A REPL filters; an agent takes verbs.

The pattern this generalizes is the one that already worked for ``/diff``:
``cli/review.py::DIFF_REVIEW_VERBS`` plus a gate that refuses MUTATING first
words in flight while permitting read-only ones, with the mutating set READ
from the dispatcher's own vocabulary rather than restated beside it. The
change here is that the pattern became a REGISTRY feature - a
``SubcommandSpec`` carries each verb's policy, argument shape, permission
tuple and presentation - instead of a ``/diff`` feature.

One class per required behaviour, one test per behaviour, named after the
behaviour it checks. Everything is host-only: no Docker, no provider, no
network, no credential.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from rich.console import Console

from cli import command_exec
from cli import commands as commands_mod
from cli import interactive
from cli.exit_codes import EXIT_CODES

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The 52 commands that existed before this round. Frozen as a LITERAL rather
#: than read from the registry: a test that reads the current registry and
#: asserts it is unchanged proves nothing about the registry. This is the
#: backward-compatibility contract, stated once, in the file.
PRE_EXISTING_COMMANDS: Tuple[str, ...] = (
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
)

#: The 8 aliases that existed before this round, and the canonical command
#: each one must still reach. A literal for the same reason as above.
#:
#: ``/diff -> /undo`` is the interesting one: ``/diff`` is ALSO a real command
#: name, and ``command_spec`` resolves an exact name BEFORE it consults an
#: alias, so the declared alias is unreachable and has always been. The
#: expected resolution is therefore ``/diff``, which is what a person typing
#: ``/diff`` has always got. Recorded as it behaves, not as it was intended.
PRE_EXISTING_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("/changes", "/diff"),
    ("/checkpoint", "/checkpoints"),
    ("/diff", "/diff"),
    ("/thinking", "/effort"),
    ("/auth", "/connect"),
    ("/exit", "/quit"),
    ("/related", "/relevant"),
    ("/copy", "/copy-diff"),
)

#: The 13 HEADLESS_FLAG_EQUIVALENTS rows that existed before this round,
#: byte-for-byte. The registry gained four more (one per new command); this
#: tuple is what must NOT have moved.
PRE_EXISTING_FLAG_EQUIVALENTS: Dict[str, str] = {
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
    # `/watch` is the 14th. The brief said 13; the table already had 14 before
    # this round, and pinning the literal the brief quotes rather than the
    # table that exists is how a row quietly escapes a compatibility test.
    "/watch": "neo watch <task-id>",
}

#: The four rows item 7 of the brief asked for. The first is a real orphan
#: this round gave a door; the last two are Terminals 09/10's behaviour.
NEW_COMMAND_ROWS: Tuple[str, ...] = (
    "/worktree",
    "/hooks",
    "/migrate",
    "/support-bundle",
)

#: The three browsers this round turned into verbs.
BROWSERS: Tuple[str, ...] = ("/plugins", "/mcp", "/skills")

IDLE = commands_mod.CommandContext(surface="repl")
IN_FLIGHT = commands_mod.CommandContext(surface="repl", in_flight=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _state(repo: Path) -> Dict[str, Any]:
    """Return a REPL session state pointed at a repository."""
    return {"repo": str(repo), "file_config": {}}


@pytest.fixture()
def isolated_plugins(tmp_path, monkeypatch):
    """Point ``cli.plugins`` at a private root for the duration of a test.

    Without this, a test that installs a plugin writes into the DEVELOPER'S
    real ``~/.config/neo/plugins`` and a later test in the same process sees
    it. That is how ``test_bare_browsers_render_exactly_as_they_did`` found
    two plugins where it expected none - a real failure caused by another
    test's residue, and the reason this fixture exists rather than a comment.
    """
    import cli.plugins as plugins_mod

    root = tmp_path / "plugin-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plugins_mod, "plugins_root", lambda: root)
    return root


@pytest.fixture()
def isolated_skills(tmp_path, monkeypatch):
    """Point the skill discovery roots at a private directory.

    ``harness.skills`` reads the developer's global config root and every
    installed plugin, so on a machine with a `demo` plugin a test asserting
    "no skills discovered" sees that plugin's skill instead. Isolating is
    the difference between a test that measures the product and one that
    measures the machine.
    """
    import cli.plugins as plugins_mod
    import harness.skills as skills_mod

    root = tmp_path / "skill-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plugins_mod, "plugins_root", lambda: root)
    monkeypatch.setattr(skills_mod, "_global_skills_root", lambda: root)
    monkeypatch.setattr(skills_mod, "_global_plugins_root", lambda: root)
    monkeypatch.setattr(skills_mod, "_global_config_root", lambda: root)
    return root


def _repl(line: str, repo: Path) -> str:
    """Type one command into the real REPL dispatcher and capture the output.

    The REAL `_slash_command`, not the impl beneath it: the preflight is part
    of the product, and a test that skips it would not notice a command the
    registry says is runnable and the dispatcher refuses.
    """
    from cli import ui

    buffer = io.StringIO()
    console = Console(file=buffer, width=100, no_color=True, force_terminal=False)
    real = ui.console
    ui.console = lambda *a, **k: console
    try:
        interactive._slash_command(line, line, {}, repo, _state(repo))
    finally:
        ui.console = real
    return buffer.getvalue()


def _receipt(command: str, verb: str, rest: str, **kwargs: Any) -> Dict[str, Any]:
    """Call the handler directly and return its plain receipt."""
    spec = commands_mod.command_spec(command)
    handler = interactive.SUBCOMMANDS_BY_COMMAND[command]
    return handler(verb, rest, **kwargs)


def _dispatcher_keys(path: Path) -> set:
    """Read the command keys a ``_slash_command_impl`` body dispatches on.

    An AST read, not a grep: a reformat cannot empty the pin and a comment
    cannot make it pass. Same shape as the existing parity test's, so the two
    cannot disagree about what "dispatches" means.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_slash_command_impl":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test = inner.test
            if not isinstance(test, ast.Compare) or not isinstance(
                test.ops[0], (ast.Eq, ast.In)
            ):
                continue
            sides = (
                [test.left] + list(test.comparators)
                if isinstance(test.ops[0], ast.Eq)
                else list(test.comparators)
            )
            for side in sides:
                if isinstance(side, (ast.Tuple, ast.List, ast.Set)):
                    for element in side.elts:
                        if (
                            isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                            and element.value.startswith("/")
                        ):
                            names.add(element.value)
                elif (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and side.value.startswith("/")
                ):
                    names.add(side.value)
    return names


# ---------------------------------------------------------------------------
# 1. the registry itself
# ---------------------------------------------------------------------------


class TestTheRegistryIsTheSingleDeclaration:
    def test_every_declared_verb_is_typed(self):
        """A verb carries a real policy, not a name and a hope."""
        for command, verbs in commands_mod.SUBCOMMANDS.items():
            for verb in verbs:
                assert verb.summary, f"{command} {verb.name} has no summary"
                assert verb.argument_policy in commands_mod.ARGUMENT_POLICIES
                assert verb.in_flight_policy in commands_mod.BEHAVIOR_POLICIES
                assert verb.required_permissions, (
                    f"{command} {verb.name} declares no permission"
                )
                for permission in verb.required_permissions:
                    assert permission in commands_mod.PERMISSION_SCOPES

    def test_a_spec_declares_no_subcommand_the_registry_does_not_have(self):
        """The two tables cannot disagree in the direction that matters.

        A verb in the hint that the dispatcher does not implement is a
        one-line lie in `/help`; a verb the dispatcher implements that the
        hint omits is a command nobody can find.
        """
        for spec in commands_mod.COMMAND_SPECS:
            declared = commands_mod.subcommand_specs(spec.name)
            assert spec.subcommands == tuple(v.name for v in declared), (
                f"{spec.name} projects {list(spec.subcommands)} but the "
                f"registry holds {[v.name for v in declared]}"
            )

    def test_the_mutating_set_is_derived_never_restated(self):
        """`mutating_verbs` is a projection, so it cannot drift from the verbs.

        The R2-15 §11.1 request was a `CommandSpec` field a gate could read.
        A field is only a fix if nobody has to keep it in sync; this one is
        computed, so the gate and the dispatcher are the same table.
        """
        for spec in commands_mod.COMMAND_SPECS:
            expected = tuple(
                v.name for v in commands_mod.subcommand_specs(spec.name) if v.mutating
            )
            assert spec.mutating_verbs == expected, spec.name

    def test_a_malformed_verb_is_rejected_at_construction(self):
        """A verb with an unknown policy fails before a surface can read it."""
        with pytest.raises(ValueError):
            commands_mod.SubcommandSpec(
                name="nope", summary="x", argument_policy="whatever"
            )
        with pytest.raises(ValueError):
            commands_mod.SubcommandSpec(
                name="nope", summary="x", result_presentation="browser"
            )
        with pytest.raises(ValueError):
            commands_mod.SubcommandSpec(
                name="nope", summary="x", required_permissions=("made:up",)
            )

    def test_a_verb_that_acts_may_not_return_a_dialog(self):
        """The rule the brief names, enforced by the vocabulary itself.

        `SUBRESULT_PRESENTATIONS` has no `browser`, `modal` or `approval` in
        it, so "a verb that ends in a dialog is not a verb" is a construction
        error rather than a review comment.
        """
        assert "browser" not in commands_mod.SUBRESULT_PRESENTATIONS
        assert "modal" not in commands_mod.SUBRESULT_PRESENTATIONS
        assert "approval" not in commands_mod.SUBRESULT_PRESENTATIONS
        for command, verbs in commands_mod.SUBCOMMANDS.items():
            for verb in verbs:
                assert verb.result_presentation in (
                    commands_mod.SUBRESULT_PRESENTATIONS
                ), f"{command} {verb.name}"

    def test_exactly_one_no_argument_case_per_command(self):
        """Two defaults means a bare call is ambiguous."""
        for command, verbs in commands_mod.SUBCOMMANDS.items():
            defaults = [v.name for v in verbs if v.default]
            assert len(defaults) == 1, f"{command} declares {defaults}"

    def test_a_verb_cannot_claim_two_spellings(self):
        """`commit` and `apply` are one action, declared once."""
        for command, verbs in commands_mod.SUBCOMMANDS.items():
            seen: Dict[str, str] = {}
            for verb in verbs:
                for spelling in (verb.name,) + tuple(verb.aliases):
                    assert spelling not in seen, (
                        f"{command}: {spelling!r} claimed by {seen.get(spelling)} "
                        f"and {verb.name}"
                    )
                    seen[spelling] = verb.name


# ---------------------------------------------------------------------------
# 2. every declared subcommand dispatches to its handler
# ---------------------------------------------------------------------------


class TestEverySubcommandDispatches:
    @pytest.mark.parametrize("command", sorted(commands_mod.SUBCOMMANDS))
    def test_every_verb_of_this_command_has_a_handler(self, command):
        """No verb exists that nothing answers.

        This is the gate against a sixth orphan: a row added to the registry
        without a handler is a command the menu advertises and the product
        cannot run. A command whose handler is HANDED OFF is allowed to have
        none, but only when it says which terminal owns it.
        """
        verbs = commands_mod.SUBCOMMANDS[command]
        if command in interactive.SUBCOMMANDS_BY_COMMAND:
            assert callable(interactive.SUBCOMMANDS_BY_COMMAND[command])
            return
        spec = commands_mod.command_spec(command)
        assert commands_mod.interactive_dispatch_refusal(spec), (
            f"{command} declares verbs, has no handler, and does not declare "
            f"interactive_dispatch='flag-only'; nothing can run it"
        )
        assert commands_mod.HANDED_OFF_COMMANDS.get(command), (
            f"{command} has no handler and no named owner"
        )

    @pytest.mark.parametrize(
        "command,verb",
        [
            (command, verb.name)
            for command, verbs in sorted(commands_mod.SUBCOMMANDS.items())
            for verb in verbs
            if command in interactive.SUBCOMMANDS_BY_COMMAND
        ],
    )
    def test_a_verb_returns_a_receipt_and_never_raises(self, command, verb, tmp_path):
        """Every verb answers with a receipt shape, whatever it was given.

        A handler that raises takes a session down; one that returns nothing
        leaves the user with silence. Both are tested by driving every verb
        with a MEANINGLESS argument, which is what a person types when they
        do not know the shape.
        """
        result = interactive.run_subcommand(
            commands_mod.command_spec(command),
            verb + " " + tmp_path.name + "-not-a-real-argument",
            state={"repo": str(tmp_path)},
            log_root=tmp_path / "logs",
            say=lambda text: None,
        )
        assert result is not None, f"{command} {verb} dispatched nothing"
        assert isinstance(result["ok"], bool), f"{command} {verb}"
        assert isinstance(result["lines"], list), f"{command} {verb}"
        assert result["verb"] == verb, f"{command} {verb} reported {result['verb']}"

    def test_a_no_argument_call_still_resolves_to_the_menu_case(self, tmp_path):
        """Bare `/plugin` is the interactive menu, not a refusal."""
        for command in BROWSERS + ("/worktree",):
            spec = commands_mod.command_spec(command)
            resolution = commands_mod.resolve_subcommand(spec, "")
            assert resolution is not None, command
            assert resolution.ok, command
            assert resolution.verb, command
            assert resolution.spec is not None
            assert resolution.spec.default is True, (
                f"{command} bare call resolved to a non-default verb"
            )
            assert commands_mod.resolve_command_line(command, IDLE).status == "ok"

    def test_the_verbs_the_brief_names_are_the_verbs_that_exist(self):
        """The three hints the brief specifies, spelled out.

        A literal expectation, so a registry edit that renames a verb or drops
        one from a hint fails HERE rather than shipping a menu that teaches
        a vocabulary the dispatcher does not speak.
        """
        expected = {
            "/plugins": "list|inspect|install|enable|disable|remove|reload|marketplace",
            "/mcp": "list|add|remove|health|call|pin|reconnect|enable|disable",
            "/skills": "list|inspect|enable|disable|create",
            "/worktree": "list|create|remove|checkout",
        }
        for command, joined in expected.items():
            spec = commands_mod.command_spec(command)
            assert spec.argument_hint == f"[{joined}]", command
            assert spec.subcommands == tuple(joined.split("|")), command

    def test_a_verb_acts_and_returns_rather_than_opening_a_dialog(self, tmp_path):
        """`/plugin reload` returns a receipt; it does not block on a menu.

        Driven through the real REPL: the output is the RECEIPT and the call
        returns. A verb that ended in a dialog would leave the caller inside
        `_slash_command` with the menu open, which is the defect the whole
        distinction exists to remove.
        """
        out = _repl("/plugin reload", tmp_path)
        assert "rescanned" in out, out
        assert "batch verbs" in out, out


# ---------------------------------------------------------------------------
# 3. an unknown subcommand is a usage error, not a silent fall-through
# ---------------------------------------------------------------------------


#: The commands whose FIRST WORD is a closed verb list. `/mcp` and `/skills`
#: are deliberately absent: their historical first argument is a server label
#: and a filter respectively, so an unrecognised word there reaches the
#: `list` verb as its argument. That is declared per-verb
#: (`free_text_argument=True`) and asserted separately, because it is the one
#: place where "an unknown subcommand is a usage error" is deliberately NOT
#: the answer - backward compatibility outranks tidiness, and both forms are
#: pinned by `test_cli_slash2.py`.
CLOSED_VERB_COMMANDS: Tuple[str, ...] = ("/plugins", "/worktree")


class TestAnUnknownSubcommandIsRefused:
    @pytest.mark.parametrize("command", CLOSED_VERB_COMMANDS)
    def test_a_typo_is_told_rather_than_shown_a_menu(self, command, tmp_path):
        """`/plugin instal` is a usage error, not the browser.

        The sentence names the VALID SET and is built by
        `commands.command_usage`, so the hint and the refusal cannot say
        different vocabularies.
        """
        spec = commands_mod.command_spec(command)
        resolution = commands_mod.resolve_command_line(f"{command} instal", IDLE)
        assert resolution.status == "invalid", (command, resolution.status)
        assert resolution.exit_code == EXIT_CODES["usage_error"]
        assert "instal" in resolution.message
        for verb in spec.subcommands:
            assert verb in resolution.message, f"{command} refusal does not list {verb}"
        assert commands_mod.command_usage(spec).split("]")[0] + "]" in (
            resolution.message
        )

    def test_an_open_first_argument_reaches_the_default_verb_instead(self):
        """`/mcp <label>` and `/skills <filter>` were never verbs.

        They are declared `free_text_argument=True` on the verb that absorbs
        them, so the RESOLUTION reaches that verb and the historical renderer
        runs. Turning them into usage errors would be a nicer rule and a
        broken product.
        """
        for command in ("/mcp", "/skills"):
            spec = commands_mod.command_spec(command)
            resolution = commands_mod.resolve_subcommand(spec, "instal")
            assert resolution is not None, command
            assert resolution.ok, command
            assert resolution.verb == "list", command
            assert resolution.rest == "instal", command
        for command in ("/diff", "/undo"):
            resolution = commands_mod.resolve_subcommand(
                commands_mod.command_spec(command), "src/app.py"
            )
            assert resolution.ok, command
            assert resolution.rest == "src/app.py", command

    def test_the_refusal_reuses_the_shared_sentence_and_hints(self, tmp_path):
        """One sentence, from the one authority, plus the recovery footer.

        The REPL prints `command_usage(spec)` and `command_recovery_hint(spec)`
        for an invalid line, so a refusal and a hint are worded by the same
        functions. This asserts the refusal names the same verbs the hint does.
        """
        out = _repl("/plugin instal", tmp_path)
        assert "usage: /plugins" in out, out
        assert "next: " in out, out
        hint = commands_mod.argument_hint("/plugin")
        for verb in commands_mod.command_spec("/plugins").subcommands:
            assert verb in hint

    def test_an_unknown_subcommand_does_not_reach_a_handler(
        self, tmp_path, monkeypatch
    ):
        """The refusal happens in the PREFLIGHT, before any handler runs.

        Booby-trapped: a handler that ran would raise, so a resolution that
        says `invalid` and still executed something would fail here.
        """

        def _boom(*args, **kwargs):
            raise AssertionError("a handler ran for an unknown subcommand")

        for handler in interactive.SUBCOMMANDS_BY_COMMAND.values():
            monkeypatch.setattr(interactive, handler.__name__, _boom)
        interactive._slash_command(
            "/plugin instal", "/plugin instal", {}, tmp_path, _state(tmp_path)
        )


# ---------------------------------------------------------------------------
# 4. a mutating subcommand is refused in flight, with a read-only alternative
# ---------------------------------------------------------------------------


class TestPerVerbMutationGating:
    @pytest.mark.parametrize("command", BROWSERS + ("/worktree",))
    def test_a_mutating_verb_is_refused_while_a_run_is_live(self, command, tmp_path):
        """The gate the brief asks for, generalized from `/diff`.

        Read-only verbs stay available mid-run, which is the entire reason the
        gate is per-verb: a `/plugin` you cannot LIST while a run is live is
        unusable exactly when you want it.
        """
        spec = commands_mod.command_spec(command)
        read_only = [v for v in spec.subcommands if v not in spec.mutating_verbs]
        for verb in spec.mutating_verbs:
            resolution = commands_mod.resolve_command_line(
                f"{command} {verb} x", IN_FLIGHT
            )
            assert resolution.status == "disabled", (command, verb)
            assert "while a run is active" in resolution.message, (command, verb)
            # The refusal NAMES the alternative rather than only saying no.
            alternative = read_only[0] if read_only else None
            if alternative:
                assert alternative in resolution.message, (command, verb)
        for verb in read_only:
            declared = commands_mod.subcommand_verb(command, verb)
            # A verb that REQUIRES an argument needs one, or the line is a
            # usage error and the test would be measuring the wrong thing.
            probe = (
                f"{command} {verb} x"
                if declared.argument_policy == "required"
                else f"{command} {verb}"
            )
            resolution = commands_mod.resolve_command_line(probe, IN_FLIGHT)
            assert resolution.status == "ok", (command, verb, resolution.message)

    def test_a_mutating_verb_is_available_while_idle(self, tmp_path):
        """The gate is about a LIVE run, not about the verb being dangerous."""
        for command in BROWSERS:
            for verb in commands_mod.command_spec(command).mutating_verbs:
                resolution = commands_mod.resolve_command_line(
                    f"{command} {verb} something", IDLE
                )
                assert resolution.status == "ok", (command, verb, resolution.message)

    def test_the_diff_gate_reads_the_registry_not_a_restated_set(self):
        """The `/diff` mutating set is the REVIEW module's own vocabulary.

        Before this round `_diff_mutating_verbs()` unioned a private literal
        with `review.diff_review_verbs()` and a test pinned the two against
        each other - a gate that can be updated without the dispatcher
        changing. Now the gate is derived from the registry, which reads the
        review module, and this asserts the derivation rather than the
        agreement-by-coincidence.
        """
        from cli import review

        review_verbs = set(review.diff_review_verbs())
        registry = {
            v.name for v in commands_mod.subcommand_specs("/diff") if v.mutating
        }
        # Every review verb except the read-only one is mutating...
        assert review_verbs - registry == {"show"}, review_verbs - registry
        # ...and the two words the HISTORICAL engine owns are in there too, so
        # a mutating first word the gate does not know about cannot exist.
        assert {"undo", "all"} <= registry
        assert set(commands_mod.command_spec("/diff").mutating_verbs) == registry

    def test_the_undo_gate_reads_the_staged_undo_dispatchers_vocabulary(self):
        """`/undo`'s mutating verbs come from `cli.fileview.UNDO_VERBS`."""
        from cli import fileview

        declared = {v.name for v in commands_mod.subcommand_specs("/undo")}
        assert set(fileview.UNDO_VERBS) <= declared
        # `plan` is the one read-only verb: it describes what WOULD change.
        assert "plan" not in commands_mod.command_spec("/undo").mutating_verbs

    def test_a_mutating_verb_needs_its_own_permission(self):
        """The per-verb permission gap R2-15 §11.1 asked about, closed.

        `/plugin list` is readable under `extension:read`; `/plugin install`
        additionally needs `filesystem:write`. A gate that consulted only the
        COMMAND's tuple would let a write happen under read permissions.
        """
        spec = commands_mod.command_spec("/plugins")
        read = commands_mod.subcommand_verb("/plugins", "list")
        write = commands_mod.subcommand_verb("/plugins", "install")
        assert "filesystem:write" not in read.required_permissions
        assert "filesystem:write" in write.required_permissions
        denied = commands_mod.CommandContext(
            surface="repl", denied_permissions=("filesystem:write",)
        )
        assert commands_mod.command_availability(spec, denied, read).available
        blocked = commands_mod.command_availability(spec, denied, write)
        assert blocked.available is False
        assert "filesystem:write" in blocked.reason


# ---------------------------------------------------------------------------
# 5. backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_all_fifty_two_pre_existing_commands_still_resolve(self):
        """Every command that existed before this round still resolves.

        Checked by NAME against a frozen literal, and by RESOLUTION: a row
        that exists but no longer resolves in some state is a broken command,
        not a declared one.
        """
        assert len(PRE_EXISTING_COMMANDS) == 52
        for name in PRE_EXISTING_COMMANDS:
            spec = commands_mod.command_spec(name)
            assert spec is not None, f"{name} no longer resolves"
            assert spec.name == name, f"{name} resolves to {spec.name}"

    def test_all_eight_pre_existing_aliases_still_resolve(self):
        """The eight declared aliases, each to the command it always reached."""
        assert len(PRE_EXISTING_ALIASES) == 8
        for alias, canonical in PRE_EXISTING_ALIASES:
            spec = commands_mod.command_spec(alias)
            assert spec is not None, f"{alias} no longer resolves"
            assert spec.name == canonical, (alias, spec.name, canonical)

    def test_the_thirteen_headless_flag_equivalents_are_unchanged(self):
        """The pre-existing flag rows, byte-for-byte.

        The table gained four rows (one per new command). This asserts the
        thirteen that existed before are exactly as they were - not that the
        table has thirteen entries, which it no longer does.
        """
        for name, equivalent in PRE_EXISTING_FLAG_EQUIVALENTS.items():
            assert commands_mod.headless_equivalent(name) == equivalent, name
        for name in PRE_EXISTING_FLAG_EQUIVALENTS:
            assert commands_mod.headless_policy(name) == "flag-only", name

    def test_all_six_exit_codes_are_unchanged(self):
        """0/1/2/3/4/130, and the two this round produces use one of them."""
        assert set(EXIT_CODES.values()) == {0, 1, 2, 3, 4, 130}
        refusal = commands_mod.resolve_command_line("/plugin instal", IDLE)
        assert refusal.exit_code == EXIT_CODES["usage_error"]
        headless = command_exec.run_command_line(
            "/plugin instal", log_root=Path("logs")
        )
        assert headless.exit_code == EXIT_CODES["usage_error"]

    def test_bare_browsers_render_exactly_as_they_did(
        self, tmp_path, isolated_plugins, isolated_skills
    ):
        """`/plugins`, `/mcp` and `/skills` with no verb are unchanged.

        Driven against an empty repository and pinned to the historical
        strings, because "existing behaviour becomes the default subcommand"
        is only true if the DEFAULT subcommand renders the historical lines.
        """
        assert "no plugins installed" in _repl("/plugins", tmp_path)
        assert "no MCP servers" in _repl("/mcp", tmp_path)
        assert "no skills discovered" in _repl("/skills", tmp_path)

    def test_the_free_text_first_argument_forms_still_work(self, tmp_path):
        """`/mcp <label>` and `/skills <filter>` were never verbs.

        Both are load-bearing and pinned by `test_cli_slash2.py`. They are
        declared `free_text_argument=True` on the verb that absorbs them, so
        they reach the SAME renderer as before rather than becoming a usage
        error - which is the whole reason that flag exists.
        """
        for command in ("/mcp", "/skills", "/diff", "/undo"):
            spec = commands_mod.command_spec(command)
            for word in ("mem", "zzz", "src/app.py", "code"):
                resolution = commands_mod.resolve_command_line(
                    f"{command} {word}", IDLE
                )
                assert resolution.status == "ok", (command, word, resolution.message)

    def test_diff_review_verifies_are_untouched(self, tmp_path):
        """`/diff` is a reviewer, not a browser, and still reads as one."""
        spec = commands_mod.command_spec("/diff")
        assert spec.result_presentation == "diff", (
            "a command with subcommands kept its historical presentation"
        )
        assert spec.argument_hint == "[show|accept|reject|revert|undo] [file|all]", (
            "the /diff hint is pinned byte-for-byte by tests/test_diff_review.py"
        )

    def test_no_command_without_subcommands_changed_its_presentation(self, tmp_path):
        """Adding a registry must not restyle a command that has no verbs.

        The constraint the brief states directly. `/help`, `/cost` and the
        other rows that never had actions are asserted against the
        presentations this round started from - including the three that DID
        gain verbs, whose command-level presentation must be unchanged
        because a verb returns and the no-argument case still opens the
        browser.
        """
        historical = {
            "/help": "browser",
            "/status": "card",
            "/sessions": "browser",
            "/checkpoints": "browser",
            "/context": "browser",
            "/trace": "browser",
            "/feed": "browser",
            "/files": "browser",
            "/history": "browser",
            "/diagnostics": "browser",
            "/relevant": "browser",
            "/diff": "diff",
            "/undo": "diff",
            "/redo": "diff",
            "/copy-diff": "diff",
            "/init": "settings",
            "/logout": "settings",
            "/model": "settings",
            "/theme": "settings",
            "/settings": "settings",
            "/connect": "modal",
            "/login": "modal",
            "/plan": "modal",
            "/quit": "exit",
            # The three browsers: their COMMAND presentation is the menu, and
            # the menu did not move.
            "/plugins": "browser",
            "/mcp": "browser",
            "/skills": "browser",
        }
        for name, presentation in historical.items():
            spec = commands_mod.command_spec(name)
            assert spec.result_presentation == presentation, name


# ---------------------------------------------------------------------------
# 6. no orphan: every spec is dispatchable on the surface its policy allows
# ---------------------------------------------------------------------------


class TestNoCommandIsAnOrphan:
    def test_every_spec_is_dispatchable_on_some_surface(self):
        """The sixth-orphan gate.

        A row is dispatchable when a shell dispatches it on BOTH surfaces, OR
        it names a CLI flag that really exists. A row that is neither is a
        command the menu advertises and nothing runs.

        The "both surfaces" half is the important one. `/watch` was once a TUI
        branch and in no registry row, so it ran in one shell, was "unknown"
        in the REPL, and was invisible to the palette, `/help` and the headless
        table; `/steer` was the mirror image. The set-equality requirement is
        what stops a sixth instance.
        """
        from cli import main as cli_main

        repl = _dispatcher_keys(REPO_ROOT / "cli" / "interactive.py")
        tui = _dispatcher_keys(REPO_ROOT / "cli" / "tui.py")
        parser = cli_main.build_parser()
        subcommands = set(parser._subparsers._group_actions[0].choices)
        for spec in commands_mod.COMMAND_SPECS:
            in_repl = spec.name in repl
            in_tui = spec.name in tui
            if in_repl and in_tui:
                assert commands_mod.interactive_dispatch_refusal(spec) == "", (
                    f"{spec.name} is dispatched by both shells AND refuses as "
                    f"handed off; one of the two is a lie"
                )
                continue
            assert not in_repl and not in_tui, (
                f"{spec.name} is dispatched by one shell only "
                f"(repl={in_repl}, tui={in_tui})"
            )
            assert commands_mod.interactive_dispatch_refusal(spec), (
                f"{spec.name} has no shell handler and no declared owner; "
                f"nothing can run it"
            )
            equivalent = commands_mod.headless_equivalent(spec.name)
            assert equivalent.strip(), spec.name
            words = equivalent.split()
            assert words[0] == "neo", equivalent
            assert words[1] in subcommands, (
                f"{spec.name} names `{equivalent}`, which is not a real neo subcommand"
            )

    def test_the_orphans_this_round_found_are_named_not_hidden(self):
        """What the gate found, recorded so it cannot be forgotten.

        `/connect` is a PRE-EXISTING orphan: a registry row and a `flag-only`
        headless policy with no branch in EITHER shell's dispatcher, added by
        VEX-PF-02. It is not this round's regression and this round does not
        mount it (`cli/tui.py` is not this round's file), so it is named here
        rather than quietly excluded from the gate above - an exemption list
        is just a place for the next orphan to hide.
        """
        repl = _dispatcher_keys(REPO_ROOT / "cli" / "interactive.py")
        tui = _dispatcher_keys(REPO_ROOT / "cli" / "tui.py")
        dispatched = repl | tui
        orphans = sorted(
            spec.name
            for spec in commands_mod.COMMAND_SPECS
            if spec.name not in dispatched
        )
        assert orphans == sorted(
            {"/connect", "/worktree", *commands_mod.HANDED_OFF_COMMANDS}
        ), (
            f"the set of undispatched commands changed: {orphans}. A new entry "
            f"is a new orphan; a REMOVED one is a command that gained a "
            f"dispatcher, and this test should be updated with a note."
        )
        # Each one names the flag that reaches the capability, so none of them
        # is a dead row.
        for name in orphans:
            spec = commands_mod.command_spec(name)
            assert commands_mod.interactive_dispatch_refusal(spec), name
            assert commands_mod.headless_equivalent(name).strip(), name

    def test_the_orphaned_worktree_capabilities_have_a_door(self, tmp_path):
        """Item 7: the seven worktree capabilities were argparse-only.

        Named here because the brief names them, and asserted through the
        functions themselves rather than through a string: if a future round
        renames one, this test fails with the name rather than with a
        coverage number.
        """
        for name in (
            "worktree_root",
            "worktree_list",
            "worktree_new",
            "worktree_remove",
            "worktree_path",
            "worktree_run_config",
        ):
            assert callable(getattr(commands_mod, name)), name
        assert issubclass(commands_mod.WorktreeCommandError, ValueError)
        spec = commands_mod.command_spec("/worktree")
        assert spec is not None
        assert spec.subcommands == ("list", "create", "remove", "checkout")
        # The RECEIPT routes to those same functions; nothing is reimplemented.
        result = _receipt("/worktree", "list", "", repo_path=str(tmp_path))
        assert result["ok"] is True
        assert "no managed worktrees" in result["lines"]

    def test_the_four_new_rows_are_declared_with_typed_metadata(self):
        """`/worktree`, `/hooks`, `/migrate` and `/support-bundle` exist."""
        for name in NEW_COMMAND_ROWS:
            spec = commands_mod.command_spec(name)
            assert spec is not None, f"{name} has no registry row"
            assert spec.summary, name
            assert spec.required_permissions, name
            assert spec.failure_recovery, name
            assert spec.subcommands, f"{name} declares no verbs"
            assert commands_mod.headless_policy(name) in (
                "mapped",
                "flag-only",
                "refuse",
            )

    def test_a_handed_off_row_says_which_flag_does_the_work(self, tmp_path):
        """A declared row nobody can run interactively NAMES the flag.

        Silence would leave a person believing a command does not exist. The
        sentence is the same one the palette marks the row disabled with.

        The rendered output is compared with whitespace NORMALIZED: a
        78-column rich console wraps a long flag mid-token, so a raw
        substring check would be testing the terminal's width rather than the
        refusal.
        """
        for name in commands_mod.HANDED_OFF_COMMANDS:
            spec = commands_mod.command_spec(name)
            refusal = commands_mod.interactive_dispatch_refusal(spec)
            assert refusal, f"{name} is handed off but says nothing"
            equivalent = commands_mod.headless_equivalent(name)
            assert equivalent in refusal, name
            out = " ".join(_repl(name, tmp_path).split())
            assert " ".join(equivalent.split()) in out, (name, out)

    def test_the_palette_marks_a_handed_off_row_disabled(self):
        """Discoverability and honesty are the same requirement (rule 5)."""
        rows = {row["label"]: row for row in commands_mod.command_palette_entries()}
        for name in NEW_COMMAND_ROWS:
            assert name in rows, f"{name} is invisible in the palette"
            assert rows[name]["disabled"] is True, name
            assert rows[name]["disabled_reason"], name
            assert (
                commands_mod.headless_equivalent(name)
                in (rows[name]["disabled_reason"])
            )

    def test_no_spec_lacks_a_dispatcher(self):
        """Every spec reaches a handler, a flag, or it is a defect.

        The generalized form of the orphan gate the brief asks for: not "every
        name in COMMAND_SPECS is in a REPL branch" (which would force a
        REPL-only branch for every command) but "every spec is dispatchable
        on the surface its policy allows".
        """
        repl = _dispatcher_keys(REPO_ROOT / "cli" / "interactive.py")
        tui = _dispatcher_keys(REPO_ROOT / "cli" / "tui.py")
        for spec in commands_mod.COMMAND_SPECS:
            in_repl = spec.name in repl
            in_tui = spec.name in tui
            handed_off = bool(commands_mod.interactive_dispatch_refusal(spec))
            assert in_repl == in_tui or handed_off, (
                f"{spec.name} is dispatched by one shell only "
                f"(repl={in_repl}, tui={in_tui}); add the branch to both or "
                f"set interactive_dispatch='flag-only' and name the flag"
            )


# ---------------------------------------------------------------------------
# 7. the rendered surface
# ---------------------------------------------------------------------------


class TestMarkupSafety:
    def test_a_hostile_plugin_description_is_visible_after_rendering(
        self, tmp_path, isolated_plugins
    ):
        """A render failure must never DELETE a message.

        A plugin whose DESCRIPTION carries `[bold red]x[/bold red]` is
        installed on disk and listed. The proof is through a REAL rich Console
        with `force_terminal=True` and asserts the words are VISIBLE
        afterwards - a substring check on the source would pass while the
        message was being eaten, which is the failure this guards.

        The NAME is a plain one on purpose: `cli.plugins` validates names, and
        a name carrying brackets is rejected at install. The DESCRIPTION is
        the untrusted field a reader actually sees, and it is unescaped
        upstream, which is exactly the shape of the bug.
        """
        import cli.plugins as plugins_mod

        target = isolated_plugins / "hostile"
        target.mkdir(parents=True, exist_ok=True)
        (target / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "hostile",
                    "description": "reviews [bold red]auth[/bold red] code",
                }
            ),
            encoding="utf-8",
        )
        out = _repl("/plugin list", tmp_path)
        assert "hostile" in out, out
        assert "auth" in out, out
        buffer = io.StringIO()
        console = Console(file=buffer, width=100, no_color=True, force_terminal=True)
        console.print(out, markup=True, highlight=False)
        rendered = buffer.getvalue()
        assert "auth" in rendered, (
            f"a hostile plugin description was eaten by the markup parser: {rendered!r}"
        )
        # The receipt path escapes too, so a double-escape cannot show the
        # reader a literal backslash before a bracket.
        assert "\\[" not in out, out

    def test_a_hostile_skill_description_survives_the_receipt(self, tmp_path):
        """The same property on the receipt path, which is new this round.

        `_render_skills` emits MARKUP and the receipt is PLAIN text, so the
        two meet. A description carrying `[weird]` used to be swallowed by the
        markup parser; the round escapes the fields and renders the markup
        once to get the plain text back.
        """
        skills = tmp_path / ".neo" / "skills" / "hostile"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\nname: hostile\n"
            "description: reviews [bold red]auth[/bold red] code\n---\n\nBody.\n",
            encoding="utf-8",
        )
        result = _receipt("/skills", "list", "", repo_path=tmp_path)
        joined = "\n".join(result["lines"])
        assert "auth" in joined, joined
        assert "[bold red]" in joined, joined
        # And through a real console, still visible.
        buffer = io.StringIO()
        console = Console(file=buffer, width=100, no_color=True, force_terminal=True)
        from rich.markup import escape as rich_escape

        for line in result["lines"]:
            console.print(rich_escape(line), highlight=False)
        assert "auth" in buffer.getvalue(), buffer.getvalue()

    def test_every_receipt_line_carries_no_markup_delimiters(self, tmp_path):
        """Receipt lines are PLAIN; the caller escapes them.

        Asserted on the property rather than on the rendering: a receipt line
        that already carries `[` would be double-escaped and show a bracket to
        the reader, and one that carries a real tag would be eaten.
        """
        for command in BROWSERS + ("/worktree",):
            spec = commands_mod.command_spec(command)
            for verb in spec.subcommands:
                result = interactive.run_subcommand(
                    spec,
                    f"{verb} hostile[name].py",
                    state={"repo": str(tmp_path)},
                    log_root=tmp_path / "logs",
                    say=lambda text: None,
                )
                for line in result["lines"]:
                    assert "[neo." not in line, (command, verb, line)
                    assert "[/]" not in line, (command, verb, line)


class TestTheMenuTeachesTheVocabulary:
    def test_every_rendered_hint_names_its_own_verbs(self):
        """Rule 5 as a check: the hint is the vocabulary's only advertisement.

        Only for the commands whose hint IS the verb list. `/diff` and
        `/undo` carry a hand-authored hint that also names argument shapes
        (`[file|all]`, `code|task|all|<file>`) the verb list cannot express,
        and both are pinned byte-for-byte by other terminals' suites; the
        assertion that matters for them is that they still name their verbs,
        which is the second half of this test.
        """
        for command in commands_mod.VERB_HINT_COMMANDS:
            spec = commands_mod.command_spec(command)
            for verb in spec.subcommands:
                assert verb in spec.argument_hint, (command, verb)

    def test_a_hand_authored_hint_still_names_its_verbs(self):
        """`/diff` and `/undo`: a rendered-from-registry hint was not wanted.

        Both hints were authored before this round and are pinned by
        `tests/test_diff_review.py` and `tests/test_agt_09_staged_undo.py`.
        They must still teach the whole vocabulary, which is the property
        rule 5 asks for.
        """
        for command in ("/diff", "/undo"):
            spec = commands_mod.command_spec(command)
            for verb in commands_mod.subcommand_names(command):
                assert verb in spec.argument_hint, (command, verb)

    def test_an_untyped_verb_is_not_advertised_as_a_word(self):
        """A row may label a bare call without being a spelling.

        `/undo` STAGES on a bare call, so the row that describes it is named
        `stage` - a word nobody types, and one the pinned hint does not
        contain. Declaring it untyped is what keeps "the hint names every
        verb" an assertion rather than an aspiration, and it keeps the row
        available to the resolver.
        """
        stage = commands_mod.subcommand_verb("/undo", "stage")
        assert stage is not None
        assert stage.typed is False
        assert stage.default is True
        assert "stage" not in commands_mod.command_spec("/undo").argument_hint
        # ...and it is still what a bare `/undo` resolves to.
        resolution = commands_mod.resolve_subcommand(
            commands_mod.command_spec("/undo"), ""
        )
        assert resolution.verb == "stage"

    def test_the_composer_hint_teaches_the_verbs_while_typing(self):
        """A half-typed name already shows the whole ladder."""
        hint = commands_mod.argument_hint("/plug")
        for verb in commands_mod.command_spec("/plugins").subcommands:
            assert verb in hint, (verb, hint)

    def test_help_lists_every_command_the_registry_carries(self):
        """`/help` is a projection of the registry, so the new rows appear."""
        rows = {entry.name for entry in interactive.help_index()}
        for name in NEW_COMMAND_ROWS:
            assert name in rows, f"{name} is missing from /help"
