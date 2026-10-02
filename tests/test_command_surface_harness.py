"""The command-surface regression gate, and the tests for ``/adopt``.

Two halves, and the split is the point:

**Part A - what a user SEES.** Driven through the REAL dispatchers
(``cli.command_exec.run_command_line`` for the headless surface,
``cli.interactive.run_subcommand`` for a session, and one bounded Textual
``Pilot`` mount for the TUI) rather than read out of the tables. A registry
assertion proves a row exists; only a drive proves a command ACTS.

**Part B - invariants over the whole registry.** The alias table, the palette
groups, the command types, the summaries, the flag documentation and the
backward-compatibility floors. A dropped or duplicated command is a defect, and
an invariant suite is the only thing that notices a duplicate.

Every class is named after the behaviour it checks and every test is named
after the sentence a person would want to be true. Host-only: no Docker, no
provider, no network, no credential. Every test that touches the filesystem
uses ``tmp_path`` and pins ``NEO_HOME`` so it never reads or writes the
developer's real configuration.

REQUIRED COMMAND::

    python -m pytest tests/test_command_surface_harness.py -p no:randomly -q
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import io
import json
import os
import re
from pathlib import Path

import pytest
from rich.console import Console

from cli import command_aliases as aliases
from cli import (
    command_exec,
    command_import,
    command_types,
    commands,
    connectors,
    exit_codes,
    interactive,
    palette,
    plugin_runtime,
    skill_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Journal event names a renderer must never print at a user. Declared at
#: module scope so a ``parametrize`` decorator can see it on every pytest
#: version this tree has run against.
RAW_EVENT_NAMES = (
    "model_delta",
    "command_started",
    "command_finished",
    "state_changed",
    "task_end",
    "attempt_start",
    "step_end",
    "step_skipped_resume",
)

#: Commands driven for real, end to end, by the output-integrity tests. Each
#: one is read-only or degrades to an honest line, so driving it cannot mutate
#: the developer's machine.
READ_ONLY_DRIVE = (
    "/help",
    "/status",
    "/cost",
    "/history",
    "/diagnostics",
    "/files",
    "/relevant",
    "/quiet",
    "/mode",
    "/doctor",
    "/settings",
    "/skills list",
    "/plugins list",
    "/mcp list",
    "/worktree list",
)

#: Sorted once, at import, so the parametrised ids are stable.
FLAG_EQUIVALENT_NAMES = tuple(sorted(commands.HEADLESS_FLAG_EQUIVALENTS))

#: Every ``CommandSpec.result_presentation`` the registry declares.
PRESENTATIONS = frozenset(
    {"inline", "browser", "modal", "card", "diff", "settings", "approval", "exit"}
)

#: MEASURED on this host with rich: unescaped through a markup parser these
#: render to NOTHING or raise, so they are the payloads that actually delete a
#: message. ``[bold red]`` renders to the empty string; ``name[/]more`` raises
#: ``MarkupError: closing tag '[/]' ... has nothing to close``. A balanced pair
#: such as ``[bold red]x[/bold red]`` renders as ``x`` - visible, merely
#: restyled - which is why it is NOT used as the hostile payload here.
HOSTILE_EMPTY = "[bold red]"
HOSTILE_RAISES = "name[/]more"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Pin every home-rooted surface at ``tmp_path``.

    ``NEO_HOME`` covers the artifact and auth roots, ``NEO_GLOBAL_ROOT``
    covers the plugin/global-config root, and the destination repo is a fresh
    directory - so a test that forgets to isolate one of them fails loudly
    against an empty tree instead of quietly polluting the developer's
    machine. This is the fixture class of bug this repo records four times
    (Terminal 06's plugin-root and skill-root pollution).
    """
    home = tmp_path / "neohome"
    home.mkdir(parents=True, exist_ok=True)
    # A label-based source resolves against the USER HOME first, and this
    # machine has a real ~/.claude. Pinning HOME is what makes `/adopt claude`
    # mean "no such directory" instead of silently reading a developer's real
    # configuration.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HARNESS_HOME", str(fake_home))
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEO_HOME", str(home))
    monkeypatch.setenv("NEO_GLOBAL_ROOT", str(home))
    monkeypatch.delenv("NEO_EFFORT", raising=False)
    monkeypatch.delenv("NEO_NOTIFY", raising=False)
    return {"home": home, "repo": repo}


@pytest.fixture()
def repo(tmp_path) -> Path:
    """A fresh destination repository with no config in it."""
    return tmp_path / "repo"


@pytest.fixture()
def log_root(tmp_path) -> Path:
    """A fresh artifact root."""
    root = tmp_path / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _drive_headless(line: str, repo: Path, log_root: Path):
    """Drive one command line through the REAL headless surface."""
    return command_exec.run_command_line(line, log_root=log_root, repo=repo)


def _render(text: str, *, markup: bool, width: int = 100) -> str:
    """Render text through a REAL rich console.

    Not a substring check on the un-rendered string: measured on this host,
    rich renders ``[bold red]`` as the EMPTY STRING with markup on, so an
    assertion made before rendering passes while the text is being eaten.
    """
    buffer = io.StringIO()
    console = Console(
        file=buffer, width=width, markup=markup, force_terminal=False, no_color=True
    )
    for row in str(text).splitlines() or [""]:
        try:
            console.print(row, markup=markup, highlight=False, soft_wrap=True)
        except Exception as exc:
            # A markup parser that RAISES is the loudest form of the same
            # defect: the message was lost and the surface crashed with it.
            buffer.write(f"<render-error {type(exc).__name__}>")
    return buffer.getvalue()


def _all_commands():
    """Every registry row, in declaration order."""
    return tuple(commands.command_specs())


def _registry_names() -> set:
    """The registry's own COMMAND names (not ``command_names()``, which lists
    the project's custom ``.neo/commands`` templates)."""
    return {spec.name for spec in _all_commands()}


def _verbed_commands():
    """Commands with a declared verb TABLE in the registry.

    Deliberately narrower than "has subcommands": ``/diff`` and ``/undo`` carry
    DERIVED verb projections (from ``cli.review`` and ``cli.fileview``), and
    their handlers are another owner's branches, so ``run_subcommand`` answers
    ``None`` for them by design - ``None`` means "not mine", not "broken".
    The derived half has its own test below.
    """
    return tuple(s for s in _all_commands() if s.name in commands.SUBCOMMANDS)


def _session_repo() -> str:
    """The repository path the session-surface drives resolve against."""
    return str(REPO_ROOT)


def _session_log() -> Path:
    """The artifact root the session-surface drives write under."""
    return Path(os.environ["NEO_HOME"]) / "logs"


def _sub_receipt(spec, verb):
    """Dispatch one verb through the REAL session dispatcher."""
    return interactive.run_subcommand(
        spec, verb, state={"repo": _session_repo()}, log_root=_session_log()
    )


def _alias_chain(alias: str, *, limit: int = 12) -> str:
    """Follow an alias to the command it finally reaches."""
    seen = {alias}
    current = alias
    for _ in range(limit):
        nxt = aliases.ALIASES.get(current)
        if nxt is None:
            return current
        if nxt in seen:
            return current
        seen.add(nxt)
        current = nxt
    return current


def _claude_source(root: Path) -> Path:
    """Build a realistic Claude Code source tree and return its path.

    Every unconvertible case in the fixture is a case Claude's real layout
    produces, so the report test measures the product rather than a synthetic
    shape: a command with a frontmatter header and no body, a skill directory
    with no ``SKILL.md``, and a whole-application ``settings.json`` that must
    never be silently adopted.
    """
    src = root / ".claude"
    (src / "commands").mkdir(parents=True, exist_ok=True)
    (src / "skills" / "code-review").mkdir(parents=True, exist_ok=True)
    (src / "skills" / "no-manifest").mkdir(parents=True, exist_ok=True)
    (src / "skills" / "no-manifest" / "notes.txt").write_text("hi\n", encoding="utf-8")
    (src / "agents").mkdir(parents=True, exist_ok=True)
    (src / "CLAUDE.md").write_text("# team rules\nbe careful\n", encoding="utf-8")
    (src / "commands" / "deploy.md").write_text(
        "---\nname: deploy\ndescription: deploy the app\n---\n\nRun the deploy "
        "checklist for $ARGUMENTS\n",
        encoding="utf-8",
    )
    (src / "commands" / "empty.md").write_text(
        "---\nname: empty\ndescription: nothing here\n---\n", encoding="utf-8"
    )
    (src / "skills" / "code-review" / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: review code\n---\n\nLook hard.\n",
        encoding="utf-8",
    )
    (src / "agents" / "triage.md").write_text(
        "---\nname: triage\ndescription: triage bugs\ntools: Read, Grep\n---\n\nTriage.\n",
        encoding="utf-8",
    )
    (src / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gh": {"command": "gh-mcp", "args": ["stdio"]}}}),
        encoding="utf-8",
    )
    return src


def _hostile_command_source(root: Path) -> Path:
    """A source whose command description is a rich style tag."""
    src = root / "hostile"
    (src / "commands").mkdir(parents=True)
    (src / "commands" / "weird.md").write_text(
        f"---\nname: weird\ndescription: {HOSTILE_EMPTY}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return src


def _gemini_source(root: Path) -> Path:
    """Build a Gemini CLI source, whose commands are TOML rather than markdown."""
    src = root / ".gemini"
    (src / "commands").mkdir(parents=True, exist_ok=True)
    (src / "GEMINI.md").write_text("# house style\n", encoding="utf-8")
    (src / "commands" / "notes.toml").write_text(
        'description = "notes"\nprompt = """\nWrite the release notes.\n"""\n',
        encoding="utf-8",
    )
    (src / "commands" / "broken.toml").write_text(
        'description = "x"\n', encoding="utf-8"
    )
    return src


# ==========================================================================
# PART A - what a user SEES
# ==========================================================================


class TestEveryCommandResolvesFromTheMenu:
    """A command nobody can find does not exist (rule 5)."""

    def test_every_registry_command_appears_in_the_slash_menu(self):
        """Every non-hidden command has a row in the `/` menu."""
        rows = palette.palette_entries(include_dynamic=False)
        listed = {getattr(row, "name", "") for row in rows}
        missing = sorted(
            spec.name
            for spec in _all_commands()
            if not spec.hidden and spec.name not in listed
        )
        assert not missing, f"commands with no menu row: {missing}"

    def test_a_hidden_command_is_hidden_on_purpose_and_names_itself(self):
        """Hiding is a DECISION with a stated reason, not an accident."""
        hidden = [s for s in _all_commands() if s.hidden]
        for spec in hidden:
            assert spec.hidden_reason, f"{spec.name} is hidden with no reason"
        listed = {
            getattr(r, "name", "")
            for r in palette.palette_entries(include_dynamic=False)
        }
        for spec in hidden:
            assert spec.name not in listed, f"{spec.name} is hidden yet in the menu"

    def test_every_command_is_searchable_by_its_own_name(self):
        """The menu's search finds each command by the name a user types."""
        rows = palette.palette_entries(include_dynamic=False)
        missed = []
        for spec in _all_commands():
            if spec.hidden:
                continue
            hits = palette.search_entries(rows, spec.name.lstrip("/"), limit=5)
            if spec.name not in {getattr(h, "name", "") for h in hits}:
                missed.append(spec.name)
        assert not missed, f"commands the menu search cannot find by name: {missed}"

    def test_the_registry_projection_and_the_menu_agree_on_the_same_set(self):
        """Two surfaces, one command list."""
        from_menu = {
            str(row.get("value") or "") for row in commands.command_palette_entries()
        }
        from_menu.discard("")
        assert _registry_names() == from_menu, (
            f"only in the registry: {sorted(_registry_names() - from_menu)}; "
            f"only in the menu: {sorted(from_menu - _registry_names())}"
        )

    def test_the_menu_receipt_agrees_with_the_projection(self):
        """`palette_receipt` is the machine-readable half of the same read."""
        receipt = palette.palette_receipt()
        assert isinstance(receipt, dict) and receipt, "the palette receipt is empty"
        assert any(str(v) for v in receipt.values()), receipt


class TestEverySubcommandActsAndReturnsAReceipt:
    """A verb that returns nothing is indistinguishable from a missing verb."""

    def test_every_declared_verb_dispatches_and_returns_a_non_empty_receipt(self):
        """Every ``(command, verb)`` row reaches a handler with real lines."""
        missing: list[str] = []
        empty: list[str] = []
        for spec in _verbed_commands():
            for verb in spec.subcommands:
                receipt = _sub_receipt(spec, verb)
                label = f"{spec.name} {verb}"
                if receipt is None:
                    missing.append(label)
                    continue
                if not receipt.get("lines"):
                    empty.append(label)
        assert not missing, f"declared verbs with no dispatcher: {missing}"
        assert not empty, f"declared verbs that returned no lines: {empty}"

    def test_every_receipt_carries_its_verb_and_a_payload_a_script_can_branch_on(self):
        """A receipt names what it did, or a script cannot use it."""
        for spec in _verbed_commands():
            verb = interactive.default_subcommand_name(spec.name)
            receipt = _sub_receipt(spec, verb)
            assert receipt is not None, f"{spec.name} default verb returned None"
            assert receipt.get("verb"), f"{spec.name} receipt has no verb"
            assert "ok" in receipt, f"{spec.name} receipt has no ok field"
            assert isinstance(receipt.get("payload"), dict)

    def test_a_mutating_verb_with_no_argument_never_reports_an_empty_success(self):
        """`/plugins install` with no reference must not read as "installed"."""
        suspicious: list[str] = []
        for spec in _verbed_commands():
            for verb_row in commands.SUBCOMMANDS.get(spec.name, ()):
                if not getattr(verb_row, "mutating", False):
                    continue
                receipt = _sub_receipt(spec, verb_row.name)
                if receipt is None:
                    continue
                body = " ".join(receipt.get("lines") or [])
                if receipt.get("ok") and not body.strip():
                    suspicious.append(f"{spec.name} {verb_row.name}")
        assert not suspicious, (
            f"mutating verbs reporting an empty success: {suspicious}"
        )

    def test_a_command_whose_handler_is_owned_elsewhere_says_so_and_names_the_door(
        self,
    ):
        """A handed-off row is a sentence, not a silent no-op."""
        for name in commands.HANDED_OFF_COMMANDS:
            receipt = interactive.unimplemented_subcommand(
                name, interactive.default_subcommand_name(name)
            )
            body = " ".join(receipt.get("lines") or [])
            assert receipt.get("ok") is False, f"{name} handed off but reported ok"
            assert name in body, f"{name} does not even name itself: {body!r}"
            # A handed-off row must point somewhere. /connect is the one
            # pre-existing row with no CLI form yet (Terminal 07 recorded it as
            # an orphan); it is listed here rather than exempted by name
            # silently, so a second one fails.
            assert ("neo " in body or "NEO_" in body) or name == "/connect", (
                f"{name} handed off without naming a way to run it: {body!r}"
            )

    def test_a_derived_verb_projection_is_declared_and_reaches_its_own_owner(self):
        """`/diff` and `/undo` carry verbs projected from their own modules."""
        derived = sorted(
            s.name
            for s in _all_commands()
            if s.subcommands and s.name not in commands.SUBCOMMANDS
        )
        assert derived, (
            "no derived verb projections exist, so this gate measures nothing"
        )
        for name in derived:
            spec = commands.command_spec(name)
            assert spec.subcommands, f"{name} has no projected verbs"
            # The session dispatcher answers None for these BY DESIGN - their
            # handlers are a branch in another owner's file, not a verb table.
            assert _sub_receipt(spec, spec.subcommands[0]) is None, (
                f"{name} answers the shared verb dispatcher now; update this pin"
            )


class TestAnUnknownSubcommandIsAUsageError:
    """A typo in a verb must not be a silent fall-through."""

    def test_an_unknown_verb_is_refused_with_exit_two(self):
        """`/plugins instal` is a usage error, not the plugin browser."""
        resolution = commands.resolve_subcommand(
            commands.command_spec("/plugins"), "instal"
        )
        assert resolution is not None
        assert resolution.ok is False
        assert resolution.known is False
        assert resolution.exit_code == exit_codes.EXIT_CODES["usage_error"]

    def test_the_refusal_lists_the_valid_set(self):
        """A refusal that does not say what is valid sends the user to /help."""
        spec = commands.command_spec("/plugins")
        resolution = commands.resolve_subcommand(spec, "frobnicate")
        message = str(resolution.message or "")
        for verb in commands.SUBCOMMANDS[spec.name]:
            assert verb.name in message, (
                f"the refusal omitted {verb.name!r}: {message!r}"
            )
        assert "frobnicate" in message

    def test_an_unknown_verb_returns_nothing_to_a_dispatcher_rather_than_guessing(self):
        """`None` means "not mine" - never a wrong handler."""
        assert _sub_receipt(commands.command_spec("/plugins"), "frobnicate") is None

    def test_an_unknown_command_is_a_usage_error_on_the_headless_surface(self):
        """A typo a user actually typed is refused, not recorded as ok."""
        result = _drive_headless("/nope-not-a-command", REPO_ROOT, _session_log())
        assert result.status == "unknown"
        assert result.exit_code == exit_codes.EXIT_CODES["usage_error"]

    def test_the_usage_error_for_an_unknown_verb_survives_a_markup_parser(self):
        """The refusal echoes the typed word, which is user data."""
        resolution = commands.resolve_subcommand(
            commands.command_spec("/plugins"), HOSTILE_EMPTY
        )
        rendered = _render(str(resolution.message or ""), markup=True)
        assert rendered.strip(), "the usage refusal rendered to nothing"


class TestStackingExpandsExactlyAsSpecified:
    """Terminal 04's four rules, read from the product's own expansion."""

    def test_a_chain_expands_in_order_and_every_command_sees_the_same_arguments(self):
        """`/clear /diff src/auth.py` -> two commands, one argument string."""
        expansion = aliases.expand_command_line("/clear /diff src/auth.py")
        assert [c.command for c in expansion.commands] == ["/clear", "/diff"]
        rests = {c.args for c in expansion.commands}
        assert len(rests) == 1, f"commands disagreed about their arguments: {rests}"

    def test_the_chain_stops_at_the_first_command_that_cannot_stack(self):
        """The remainder becomes that command's arguments, never truncated."""
        expansion = aliases.expand_command_line("/ask explain routing please")
        assert [c.command for c in expansion.commands] == ["/ask"]
        assert expansion.stopped_at == "explain", (
            f"the chain stopped at {expansion.stopped_at!r}, expected the non-command token"
        )
        assert expansion.stop_reason in aliases.STACK_STOP_REASONS
        assert "explain routing please" in expansion.arguments

    def test_the_chain_is_capped_and_the_cap_is_reported(self):
        """Eight `/clear`s -> six commands, and the reason is named."""
        expansion = aliases.expand_command_line(" ".join(["/clear"] * 8))
        assert len(expansion.commands) == aliases.MAX_STACKED_COMMANDS
        assert expansion.stop_reason in aliases.STACK_STOP_REASONS

    def test_a_line_that_is_not_a_chain_is_returned_unchanged(self):
        """Stacking must not rewrite an ordinary sentence or a lone command."""
        for line in ("/help", "/diff src/a.py"):
            expansion = aliases.expand_command_line(line)
            assert len(expansion.commands) == 1
            assert expansion.commands[0].command == line.split(" ")[0]


class TestAnAliasWorksAndIsDisclosed:
    """An undocumented alias is a command nobody can find."""

    def test_every_alias_resolves_to_a_real_command_through_the_resolver(self):
        """The registry wins, then the alias table - Terminal 04's rule 1."""
        for alias in aliases.ALIASES:
            resolution = aliases.resolve_line(f"{alias} value")
            assert resolution.spec is not None, f"{alias} did not resolve"
            assert resolution.spec.name == _alias_chain(alias), (
                f"{alias} resolved to {resolution.spec.name}, expected {_alias_chain(alias)}"
            )

    def test_the_disclosure_names_every_alias_and_its_target(self):
        """The alias block is generated from the table, not restated."""
        lines = aliases.alias_disclosure_lines()
        body = "\n".join(lines)
        assert lines, "the alias disclosure is empty"
        for alias in aliases.ALIASES:
            assert alias in body, f"the disclosure omitted {alias}"
            assert aliases.ALIASES[alias] in body, (
                f"the disclosure omitted {alias}'s target"
            )

    def test_the_disclosure_survives_a_markup_parser_with_data_in_it(self):
        """A hostile alias block stays VISIBLE after rendering."""
        body = "\n".join(aliases.alias_disclosure_lines())
        assert body.strip(), "the disclosure is empty, so nothing is at risk"
        assert _render(body, markup=True).strip(), "the disclosure rendered to nothing"


class TestARefusedCommandShowsItsReason:
    """A refusal without a reason is a bug report waiting to be filed."""

    def test_every_unavailable_command_in_every_state_names_its_reason(self):
        """Nine states x every command: unavailable always carries a reason."""
        refusals = 0
        for state in commands.SURFACE_STATES:
            for spec in _all_commands():
                availability = commands.command_availability(
                    spec,
                    commands.CommandContext(
                        surface="repl",
                        in_flight=state
                        in {"running", "waiting_for_approval", "resumed"},
                        waiting_for_approval=state == "waiting_for_approval",
                        has_task=state != "idle",
                        denied_permissions=("workspace:write",),
                        disabled_reasons=((spec.name, "matrix probe"),)
                        if state == "blocked"
                        else (),
                    ),
                )
                if availability.available:
                    continue
                refusals += 1
                assert availability.reason, (
                    f"{spec.name} unavailable in {state} with no reason"
                )
        assert refusals > 0, "the matrix refused nothing, so it proves nothing"

    def test_the_refusal_reaches_the_receipt_with_a_recovery_hint(self):
        """The reason and the way out both ride the record."""
        resolution = commands.resolve_command_line(
            "/undo",
            commands.CommandContext(surface="repl", in_flight=True, has_task=True),
        )
        assert resolution.status != "ok"
        assert resolution.message
        assert resolution.recovery

    def test_a_command_with_no_task_is_a_NOTE_not_a_refusal(self):
        """ "no task in this session" is a note, because the handler degrades."""
        availability = commands.command_availability(
            commands.command_spec("/status"),
            commands.CommandContext(surface="repl", in_flight=False, has_task=False),
        )
        assert availability.available is True
        assert availability.note

    def test_a_permission_the_command_needs_and_has_not_got_names_the_scope(self):
        """A refusal that does not name the scope leaves the user guessing."""
        spec = commands.command_spec("/undo")
        availability = commands.command_availability(
            spec,
            commands.CommandContext(
                surface="repl",
                in_flight=False,
                has_task=True,
                denied_permissions=tuple(spec.required_permissions),
            ),
        )
        assert availability.available is False
        assert availability.reason


class TestNoCommandLosesAMessageOrLeaksInternals:
    """Rule 3, measured through a REAL renderer rather than asserted."""

    @pytest.mark.parametrize("line", READ_ONLY_DRIVE)
    def test_a_command_returns_a_record_rather_than_nothing(self, line, tmp_path):
        """Every drive produced a status and text - the surface answered."""
        result = _drive_headless(line, tmp_path / "repo", tmp_path / "logs")
        assert result.status in commands.COMMAND_STATUSES, (
            f"{line} produced the undeclared status {result.status!r}"
        )
        assert result.exit_code in set(exit_codes.EXIT_CODES.values())
        assert str(result.text or "").strip(), f"{line} returned an empty document"

    @pytest.mark.parametrize("line", READ_ONLY_DRIVE)
    def test_no_command_prints_a_library_traceback(self, line, tmp_path):
        """A traceback is the product telling the user to file a bug."""
        result = _drive_headless(line, tmp_path / "repo", tmp_path / "logs")
        body = str(result.text or "")
        assert "Traceback (most recent call last)" not in body
        for marker in ("TypeError:", "AttributeError:", "KeyError:", "MarkupError:"):
            assert marker not in body, f"{line} leaked {marker!r}"

    @pytest.mark.parametrize("line", READ_ONLY_DRIVE)
    def test_no_command_prints_a_raw_journal_event_name(self, line, tmp_path):
        """`model_delta` is an internal vocabulary word, not a sentence."""
        result = _drive_headless(line, tmp_path / "repo", tmp_path / "logs")
        body = str(result.text or "")
        found = [name for name in RAW_EVENT_NAMES if name in body]
        assert not found, f"{line} printed the raw event name(s) {found}"

    @pytest.mark.parametrize("line", READ_ONLY_DRIVE)
    def test_no_command_raises_through_the_surface(self, line, tmp_path):
        """`run_command_line` answers a failure; it does not propagate one."""
        result = _drive_headless(line, tmp_path / "repo", tmp_path / "logs")
        assert result is not None and result.command, f"{line} returned no record"

    def test_a_hostile_repository_name_is_visible_after_a_real_render(self, tmp_path):
        """A directory named like a style tag renders to nothing unescaped."""
        hostile_repo = tmp_path / HOSTILE_EMPTY.replace("[", "[").replace("]", "")
        hostile_repo.mkdir()
        result = _drive_headless("/status", hostile_repo, tmp_path / "logs")
        body = str(result.text or "")
        if HOSTILE_EMPTY not in body:
            pytest.skip(
                "/status does not echo the repository path, so nothing is at risk"
            )
        assert HOSTILE_EMPTY in _render(body, markup=True)

    def test_a_hostile_command_name_is_refused_without_echoing_a_traceback(self):
        """`/<tag>` is a refusal, and the refusal renders."""
        result = _drive_headless(f"/{HOSTILE_RAISES}", REPO_ROOT, _session_log())
        assert result.status in {"unknown", "invalid", "not_command"}
        assert "Traceback (most recent call last)" not in str(result.text or "")

    def test_the_control_shows_a_hostile_tag_is_eaten_so_the_escaping_is_load_bearing(
        self,
    ):
        """Without this control any escaping test can pass vacuously."""
        assert _render(HOSTILE_EMPTY, markup=True).strip() == "", (
            "the unescaped control was NOT eaten, so the escaping proof is void"
        )


class TestTheHeadlessSurfaceRefusesDialogsHonestly:
    """A script cannot answer a question, and must not be asked one."""

    @pytest.mark.parametrize("name", FLAG_EQUIVALENT_NAMES)
    def test_a_flag_only_command_names_the_line_a_script_should_type(self, name):
        """`flag-only` is only honest if it says what to type."""
        assert commands.command_spec(name) is not None, (
            f"{name} names a flag with no row"
        )
        assert commands.HEADLESS_COMMAND_POLICIES[name] == "flag-only"
        result = _drive_headless(name, REPO_ROOT, _session_log())
        assert result.status == "flag"
        assert result.exit_code == exit_codes.EXIT_CODES["usage_error"]
        body = str(result.text or "")
        head = commands.HEADLESS_FLAG_EQUIVALENTS[name].split()[0]
        assert head in body, f"{name} did not name {head!r}: {body!r}"

    def test_a_mapped_command_actually_acts_on_the_headless_surface(self):
        """`mapped` means the SAME handler ran, not that a refusal came back."""
        result = _drive_headless("/help", REPO_ROOT, _session_log())
        assert result.status != "flag"
        assert "help" in str(result.text or "").lower()


class TestTheTuiMountsAndDispatchesTheSameSet:
    """The TUI is the third surface; the mount is bounded so it FAILS, not hangs."""

    def test_the_tui_dispatcher_and_the_repl_dispatcher_dispatch_the_same_set(self):
        """Read with `ast`, so a reformat cannot empty the check."""

        def branch_keys(path: str, function: str) -> set:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name != function:
                    continue
                keys: set = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        value = sub.value
                        if value.startswith("/") and value.count("/") == 1:
                            keys.add(value)
                return keys
            return set()

        tui = branch_keys(str(REPO_ROOT / "cli" / "tui.py"), "_slash_command_impl")
        repl = branch_keys(
            str(REPO_ROOT / "cli" / "interactive.py"), "_slash_command_impl"
        )
        assert tui, "could not read the TUI dispatcher branches"
        assert repl, "could not read the REPL dispatcher branches"
        assert tui == repl, (
            f"only the TUI dispatches: {sorted(tui - repl)}; "
            f"only the REPL dispatches: {sorted(repl - tui)}"
        )

    def test_the_real_tui_app_mounts_and_the_menu_lists_the_registry(self):
        """A real mount, bounded: a hang fails the test instead of the suite."""
        from cli import tui

        async def _drive():
            app = tui.NeoApp(repo=str(REPO_ROOT), log_root=_session_log(), state={})
            async with app.run_test() as pilot:
                await pilot.pause()
                return len(list(app.screen.query("*")))

        widgets = asyncio.run(asyncio.wait_for(_drive(), timeout=60))
        assert widgets > 0, "the app mounted nothing"
        rows = palette.open_rows("", context=commands.CommandContext(surface="tui"))
        assert {getattr(r, "name", "") for r in rows} >= {
            s.name for s in _all_commands() if not s.hidden
        }


# ==========================================================================
# PART B - registry invariants
# ==========================================================================


class TestTheAliasTableIsAcyclicAndUnambiguous:
    """A cycle in a resolver is a hang; an ambiguity is a coin flip."""

    def test_the_alias_table_reports_no_cycle(self):
        """Terminal 04's DFS gate, read as a fact rather than trusted."""
        report = aliases.alias_cycle_report()
        assert report["acyclic"] is True, f"alias cycles: {report['cycles']}"
        assert not report["cycles"]

    def test_no_alias_is_spelled_like_a_real_command(self):
        """An alias spelled like a command is dead code: the registry wins."""
        shadowing = sorted(set(aliases.ALIASES) & _registry_names())
        assert not shadowing, f"aliases shadow real commands: {shadowing}"

    def test_every_alias_chain_terminates_at_a_real_command(self):
        """`/bashes -> /tasks -> /sessions`: two hops, one real destination."""
        names = _registry_names()
        dangling = sorted(
            alias for alias in aliases.ALIASES if _alias_chain(alias) not in names
        )
        assert not dangling, f"alias chains that end nowhere: {dangling}"

    def test_the_product_own_alias_audit_reports_no_unknown_target(self):
        """`cli.command_aliases.validate_alias_table` is the one gate."""
        report = aliases.validate_alias_table()
        assert report.ok is True, report.findings
        assert not report.unknown_targets, report.findings


class TestEveryCommandIsFullyClassified:
    """A row with a missing field is a row that renders as nothing."""

    def test_every_command_has_a_type(self):
        """`cli.command_types` is the authority and every row resolves in it."""
        known = {t.name for t in command_types.TYPES}
        untyped = [
            f"{spec.name} -> {spec.type_name!r}"
            for spec in _all_commands()
            if spec.type_name not in known
        ]
        assert not untyped, f"commands with an unknown type: {untyped}"

    def test_every_command_has_a_non_empty_summary(self):
        """A row with no summary cannot teach anybody anything."""
        blank = [s.name for s in _all_commands() if not str(s.summary or "").strip()]
        assert not blank, f"commands with no summary: {blank}"

    def test_every_command_has_a_declared_result_presentation_and_recovery(self):
        """A command with no way out is a command that strands a person."""
        for spec in _all_commands():
            assert spec.result_presentation in PRESENTATIONS
            assert spec.failure_recovery, f"{spec.name} declares no recovery"
            for action in spec.failure_recovery:
                assert action in commands.RECOVERY_ACTIONS, (
                    f"{spec.name} declares unknown recovery {action!r}"
                )

    def test_every_command_names_only_permission_scopes_the_registry_knows(self):
        """An invented scope is a permission nothing checks."""
        for spec in _all_commands():
            for scope in spec.required_permissions:
                assert scope in commands.PERMISSION_SCOPES, (
                    f"{spec.name} requires unknown scope {scope!r}"
                )

    def test_the_cost_projection_agrees_with_the_type_on_every_command(self):
        """Terminal 02's authority, re-asserted: no second cost table."""
        for spec in _all_commands():
            kind = command_types.command_type(spec)
            assert kind.model_consuming in {True, False}
            if kind.model_consuming:
                assert spec.type_name in {"prompt", "skill"}, (
                    f"{spec.name} is a {spec.type_name} yet claims to spend tokens"
                )


class TestEveryCommandIsInExactlyOnePaletteGroup:
    """A duplicated group entry shows a command twice; a missing one hides it."""

    def test_every_registry_command_has_exactly_one_group(self):
        """`cli.palette.COMMAND_GROUPS` is the one map."""
        missing = sorted(_registry_names() - set(palette.COMMAND_GROUPS))
        assert not missing, f"commands in no palette group: {missing}"
        unassigned = sorted(palette.unassigned_commands())
        assert not unassigned, f"commands with no group row: {unassigned}"

    def test_no_command_appears_in_two_groups(self):
        """Duplicated rows are a defect the user sees as a doubled command."""
        seen: dict = {}
        duplicates: list[str] = []
        for name, key in palette.COMMAND_GROUPS.items():
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} and {key}")
            seen[name] = key
        assert not duplicates, f"commands in more than one group: {duplicates}"

    def test_every_group_key_is_a_declared_group(self):
        """A typo'd group key is a command nobody can find."""
        keys = {getattr(g, "key", "") for g in palette.PALETTE_GROUPS}
        unknown = sorted({key for key in palette.COMMAND_GROUPS.values()} - keys)
        assert not unknown, f"commands grouped under an undeclared group: {unknown}"

    def test_a_thin_group_still_publishes_its_rows(self):
        """Anti-clutter hides a HEADING, never a command."""
        minimum = int(palette.anti_clutter_min_entries())
        for group in palette.PALETTE_GROUPS:
            names = [
                name for name, key in palette.COMMAND_GROUPS.items() if key == group.key
            ]
            if not names or len(names) >= minimum:
                continue
            found = {getattr(row, "name", "") for row in palette.group_entries(group)}
            missing = sorted(set(names) - found)
            assert not missing, (
                f"group {group.title!r} dropped {missing} for being thin"
            )

    def test_a_group_with_no_commands_is_not_declared_at_all(self):
        """An empty heading is a section nobody asked for."""
        keys = {getattr(g, "key", "") for g in palette.PALETTE_GROUPS}
        used = set(palette.COMMAND_GROUPS.values())
        assert not (keys - used), (
            f"declared groups with no commands: {sorted(keys - used)}"
        )


class TestEveryDeclaredFlagIsDocumented:
    """A flag a help line does not mention is a flag nobody finds."""

    def test_every_flag_equivalent_names_a_real_command_or_an_env_var(self):
        """Each row's head resolves, or is an env var, or is `neo`."""
        for name, equivalent in commands.HEADLESS_FLAG_EQUIVALENTS.items():
            assert equivalent, f"{name} declares an empty equivalent"
            head = equivalent.split()[0]
            if head == "neo":
                continue
            if re.fullmatch(r"[A-Z_]+=<[^>]+>", head):
                continue
            assert re.fullmatch(r"[a-z][a-z0-9-]*", head), (
                f"{name} names {equivalent!r}, whose head is neither a neo "
                f"subcommand nor an env-var assignment"
            )

    def test_every_command_with_a_required_argument_teaches_one(self):
        """`argument_hint` is what the composer teaches while it is being typed."""
        for spec in _all_commands():
            if spec.argument_policy == "required":
                assert spec.argument_hint, (
                    f"{spec.name} requires an argument and teaches none"
                )

    def test_every_typed_subcommand_verb_is_named_in_its_commands_hint(self):
        """A hint promising a word the dispatcher refuses is a defect."""
        for name, rows in commands.SUBCOMMANDS.items():
            spec = commands.command_spec(name)
            assert spec is not None
            advertised = set(re.findall(r"[a-z][a-z0-9_-]*", spec.argument_hint or ""))
            typed = {row.name for row in rows if getattr(row, "typed", True)}
            missing = sorted(typed - advertised)
            assert not missing, (
                f"{name} declares typed verbs {missing} its hint never names"
            )

    def test_every_keyboard_shortcut_is_declared_with_a_purpose(self):
        """A bound key nothing declares is a key nobody can find."""
        bound = shortcuts_audit()
        for key, action in sorted(bound.items()):
            assert (
                key in commands.SHORTCUT_KEYS
                or key in commands.SURFACE_SHORTCUTS_TOKENS
            ), f"{key} is bound to {action} and declares nothing"


def shortcuts_audit() -> dict:
    """Return the TUI's own bound keys, read from the app's BINDINGS table."""
    from cli import tui

    out: dict = {}
    for binding in getattr(tui.NeoApp, "BINDINGS", ()):
        try:
            key, action = binding[0], binding[1]
        except (TypeError, IndexError):
            continue
        out[str(key)] = str(action)
    return out


class TestBackwardCompatibilityIsByteIdentical:
    """Rule 8. Additions are not regressions; renames and removals are."""

    #: The brief says 52 commands; the tree had 52 before VEX-CS-01 landed
    #: four more. The FLOOR is 52 and every pre-wave NAME is pinned, so a
    #: rename or a removal is red while somebody else's additive work is not.
    PRE_WAVE_COMMAND_FLOOR = 52
    PRE_WAVE_ALIAS_FLOOR = 8
    #: The brief says 13 flag rows. Terminal 01 MEASURED 14 before its own
    #: round (the 14th is `/watch`) and added four. Pinning the brief's number
    #: would pin a count this tree never had, which is how a compatibility
    #: check ends up measuring the brief instead of the product.
    PRE_WAVE_FLAG_FLOOR = 14
    #: Every command name present before the command-surface wave.
    PRE_WAVE_COMMANDS = (
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
        "/worktree",
        "/hooks",
        "/migrate",
        "/support-bundle",
    )

    def test_every_command_the_registry_had_before_is_still_here(self):
        """A removal is red; an addition is not."""
        names = _registry_names()
        assert len(names) >= self.PRE_WAVE_COMMAND_FLOOR, (
            f"the registry shrank to {len(names)}"
        )
        missing = sorted(set(self.PRE_WAVE_COMMANDS) - names)
        assert not missing, f"commands removed since the wave began: {missing}"

    def test_no_command_name_is_duplicated(self):
        """A duplicated row renders a doubled command and an ambiguous dispatch."""
        seen = [spec.name for spec in _all_commands()]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        assert not duplicates, f"duplicated command rows: {duplicates}"

    def test_the_eight_original_aliases_are_all_still_present_and_unchanged(self):
        """The pre-wave aliases, pinned by value."""
        original = {
            "/changes": "/diff",
            "/checkpoint": "/checkpoints",
            "/copy": "/copy-diff",
            "/exit": "/quit",
            "/related": "/relevant",
        }
        for alias, target in original.items():
            assert aliases.ALIASES.get(alias) == target, (
                f"alias {alias} changed or vanished"
            )
        registry_aliases = {a for s in _all_commands() for a in s.aliases}
        assert "/plugin" in registry_aliases, "the /plugin registry alias vanished"
        assert {"/auth", "/thinking"} <= (set(aliases.ALIASES) | registry_aliases)
        assert len(aliases.ALIASES) >= self.PRE_WAVE_ALIAS_FLOOR

    def test_every_pre_wave_flag_equivalent_row_is_byte_identical(self):
        """Every flag row the wave inherited, pinned value by value."""
        original = {
            "/connect": "neo connect",
            "/cost": "neo status --task-id <task-id>",
            "/hooks": "neo hooks list",
            "/init": "neo config init-project",
            "/login": "neo login",
            "/logout": "neo logout",
            "/mcp": "neo mcp list",
            "/model": "neo login",
            "/plugins": "neo plugin list",
            "/settings": "neo config list",
            "/skills": "neo skills list",
            "/status": "neo status --task-id <task-id>",
            "/support-bundle": "neo support-bundle",
            "/theme": "neo config set theme <name>",
            "/watch": "neo watch <task-id>",
            "/worktree": "neo worktree list",
        }
        for name, value in original.items():
            assert commands.HEADLESS_FLAG_EQUIVALENTS.get(name) == value, (
                f"flag equivalent {name} changed from {value!r} to "
                f"{commands.HEADLESS_FLAG_EQUIVALENTS.get(name)!r}"
            )
        assert len(commands.HEADLESS_FLAG_EQUIVALENTS) >= self.PRE_WAVE_FLAG_FLOOR

    def test_all_six_exit_codes_keep_their_values(self):
        """0 / 1 / 2 / 3 / 4 / 130 - the whole public contract."""
        assert exit_codes.EXIT_CODES == {
            "success": 0,
            "task_failure": 1,
            "usage_error": 2,
            "environment_error": 3,
            "model_error": 4,
            "interrupted": 130,
        }

    def test_a_usage_error_is_two_on_every_surface_that_can_produce_one(self):
        """A usage error is 2 whether it came from a shell or a script."""
        assert exit_codes.classify_exit_code(ValueError("bad flag")) in set(
            exit_codes.EXIT_CODES.values()
        ), "classify_exit_code produced a code outside the public vocabulary"
        assert exit_codes.EXIT_CODES["usage_error"] == 2

    def test_import_still_means_import_a_session_export(self):
        """The collision this round had to resolve, pinned from both sides."""
        spec = commands.command_spec("/import")
        assert spec is not None
        assert spec.summary == "import a session export as a new conversation"
        assert spec.argument_policy == "required"
        assert spec.argument_hint == "<path> [--overwrite]"
        assert commands.HEADLESS_COMMAND_POLICIES["/import"] == "mapped"
        # And the new verb is a DIFFERENT name, not an overload of this one.
        assert command_import.ADOPT_COMMAND_NAME not in _registry_names()
        assert "/adopt" not in spec.aliases

    def test_every_command_has_a_headless_policy_and_the_tables_agree(self):
        """The import-time validator's job, re-asserted at runtime."""
        for spec in _all_commands():
            assert spec.name in commands.HEADLESS_COMMAND_POLICIES, (
                f"{spec.name} has no headless policy row"
            )
            policy = commands.HEADLESS_COMMAND_POLICIES[spec.name]
            assert policy in commands.HEADLESS_POLICIES
            if policy == "flag-only":
                assert spec.name in commands.HEADLESS_FLAG_EQUIVALENTS, (
                    f"{spec.name} is flag-only and names nothing to type instead"
                )
            else:
                assert spec.name not in commands.HEADLESS_FLAG_EQUIVALENTS, (
                    f"{spec.name} declares a flag equivalent but is {policy}"
                )

    def test_every_required_command_is_still_declared(self):
        """`REQUIRED_COMMANDS` is the contract the other suites pin."""
        missing = sorted(set(commands.REQUIRED_COMMANDS) - _registry_names())
        assert not missing, f"required commands with no row: {missing}"


class TestTheVerifierGateIsUntouched:
    """Nothing in the command surface may mint or promote a verdict."""

    def test_the_mint_vocabulary_is_the_shared_contracts(self):
        """`completed_verified` comes from one place and this is not it."""
        from shared import agent_contracts

        assert "completed_verified" in agent_contracts.RUN_STATUSES
        assert "completed_unverified" in agent_contracts.RUN_STATUSES

    def test_the_new_module_carries_no_completion_vocabulary(self):
        """A cost receipt a verifier could read becomes a success criterion."""
        assert command_import.self_check() == [], command_import.self_check()

    def test_the_verdict_reduction_still_fails_closed(self):
        """`runview.run_verdict` is unchanged and still refuses to promote."""
        from cli import runview

        assert runview.run_verdict("completed_verified", evidence=None) == "unverified"
        assert runview.run_verdict("completed_verified") == "unverified"
        assert (
            runview.verdict_is_success(runview.run_verdict("completed_unverified"))
            is False
        )

    def test_the_declared_boundary_check_is_still_the_shared_implementation(self):
        """`shared.approval.command_prefix_matches` is the one boundary."""
        from shared import approval

        assert approval.command_prefix_matches("git stash", "git sta") is False
        assert (
            approval.command_prefix_matches("pytest tests/x.py", "pytest tests/")
            is True
        )
        assert approval.command_prefix_matches("anything", "") is False

    def test_no_configuration_default_was_added_for_this_feature(self):
        """A value in DEFAULTS merges into every task and every eval arm."""
        from harness import config as harness_config

        for key in ("adopt", "import_agent", "adopt_namespace", "adopt_approve"):
            assert key not in harness_config.DEFAULTS, f"{key} is in DEFAULTS"


# ==========================================================================
# PART C - /adopt
# ==========================================================================


class TestAdoptConvertsAndNeverMoves:
    """The source is opened read-only and stays byte-identical."""

    def test_an_apply_leaves_the_source_tree_byte_identical(self, tmp_path, repo):
        """Hash every source file before and after, and compare."""
        src = _claude_source(tmp_path)
        before = command_import.hash_tree(src)
        assert before, "the fixture produced no source files to hash"
        command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert command_import.hash_tree(src) == before

    def test_the_plan_writes_nothing_at_all(self, tmp_path, repo):
        """Dry-run is the default, and it creates no directory."""
        src = _claude_source(tmp_path)
        before = command_import.hash_tree(repo)
        receipt = command_import.adopt_command("plan", str(src), repo_path=repo)
        assert receipt.payload["wrote_nothing"] is True
        assert not (repo / ".neo").exists(), "a plan created the destination tree"
        assert command_import.hash_tree(repo) == before

    def test_the_converted_copy_is_namespaced_so_both_remain_available(
        self, tmp_path, repo
    ):
        """The user's own `/deploy` still wins; the import is `/claude-deploy`."""
        (repo / ".neo" / "commands").mkdir(parents=True)
        (repo / ".neo" / "commands" / "deploy.md").write_text(
            "mine\n", encoding="utf-8"
        )
        src = _claude_source(tmp_path)
        command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert (repo / ".neo" / "commands" / "deploy.md").read_text(
            encoding="utf-8"
        ) == "mine\n"
        assert (repo / ".neo" / "commands" / "claude-deploy.md").is_file()
        assert "claude-deploy" in commands.command_names(repo)

    def test_a_converted_command_resolves_through_the_real_command_loader(
        self, tmp_path, repo
    ):
        """The namespaced command is reachable, which is the point of an import."""
        src = _claude_source(tmp_path)
        command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert commands.load_command("claude-deploy", repo), (
            "the imported command does not resolve"
        )

    def test_a_converted_subagent_loads_through_the_real_agent_registry(
        self, tmp_path, repo
    ):
        """`runtime.subagents` is the reader; the frontmatter must satisfy it."""
        src = _claude_source(tmp_path)
        command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        roster = skill_catalog.AgentRoster(repo_path=repo)
        names = [row["name"] for row in roster.rows()]
        assert "claude-triage" in names, (
            f"roster {names}; diags {roster.load().get('diagnostics')}"
        )

    def test_a_converted_skill_loads_through_the_real_skill_scanner(
        self, tmp_path, repo
    ):
        """`skill_catalog.discover_skills` is the reader."""
        src = _claude_source(tmp_path)
        command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        diagnostics: list = []
        found = skill_catalog.discover_skills(repo, diagnostics=diagnostics)
        names = [getattr(skill, "name", "") for skill in found]
        assert "claude-code-review" in names, f"skills {names}; diags {diagnostics}"

    def test_a_converted_mcp_server_is_read_back_by_the_real_connector_reader(
        self, tmp_path, repo
    ):
        """The writer and the reader must agree on the file's shape."""
        src = _claude_source(tmp_path)
        receipt = command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert not receipt.payload["errors"], receipt.payload["errors"]
        servers = connectors.project_servers(repo)
        assert servers.get("claude-gh") == "gh-mcp stdio", f"connectors: {servers}"

    def test_applying_twice_writes_nothing_the_second_time(self, tmp_path, repo):
        """Idempotence, so a second run cannot half-overwrite the first."""
        src = _claude_source(tmp_path)
        first = command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert first.payload["written"]
        second = command_import.adopt_command(
            "apply", str(src), repo_path=repo, approve=lambda _r: True
        )
        assert second.payload["written"] == [], second.payload
        assert len(second.payload["preserved"]) == len(first.payload["written"]) - 1

    def test_a_path_to_a_foreign_directory_reads_that_agents_layout(
        self, tmp_path, repo
    ):
        """`/adopt ~/.claude` reads Claude's shapes, not the generic ones."""
        src = _claude_source(tmp_path)
        by_path = command_import.build_plan(str(src), repo_path=repo)
        by_label = command_import.build_plan("claude", repo_path=repo, cwd=tmp_path)
        assert by_path.source.label == "claude"
        assert by_path.namespace == by_label.namespace
        assert {i.name for i in by_path.by_kind("command")} == {
            i.name for i in by_label.by_kind("command")
        }


class TestAdoptReportsEverythingItCouldNotConvert:
    """A silent drop is the worst outcome this tool can produce."""

    def test_a_command_with_no_body_is_reported_with_a_closed_set_reason(
        self, tmp_path, repo
    ):
        """A command with a header and no prompt is reported, not written."""
        src = _claude_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo)
        bad = [i for i in plan.by_kind("command") if i.name == "empty"]
        assert len(bad) == 1, (
            f"expected one empty command, got {[i.name for i in plan.by_kind('command')]}"
        )
        assert bad[0].action == "unconvertible"
        assert bad[0].reason in command_import.UNCONVERTIBLE_REASONS
        assert bad[0].detail

    def test_a_skill_directory_without_a_manifest_is_reported(self, tmp_path, repo):
        """A directory under `skills/` with no SKILL.md is not a skill."""
        src = _claude_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo)
        bad = [i for i in plan.by_kind("skill") if i.name == "no-manifest"]
        assert len(bad) == 1, f"skills: {[i.name for i in plan.by_kind('skill')]}"
        assert bad[0].reason == "unsupported_kind"
        assert bad[0].detail

    def test_a_toml_command_without_a_prompt_key_is_reported(self, tmp_path, repo):
        """Gemini commands are TOML; a header with no `prompt` cannot convert."""
        src = _gemini_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo)
        bad = [i for i in plan.by_kind("command") if i.name == "broken"]
        assert len(bad) == 1, (
            f"commands: {[(i.name, i.action) for i in plan.by_kind('command')]}"
        )
        assert bad[0].action == "unconvertible"
        assert bad[0].reason in command_import.UNCONVERTIBLE_REASONS

    def test_a_toml_command_with_a_prompt_key_converts_to_markdown(
        self, tmp_path, repo
    ):
        """The Gemini shape really converts, which is what makes the gate above real."""
        src = _gemini_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo, namespace="gemini")
        good = [i for i in plan.by_kind("command") if i.name == "notes"]
        assert len(good) == 1 and good[0].action == "converted"
        assert good[0].target_path.endswith("gemini-notes.md")

    def test_the_command_suffix_is_read_from_the_source_not_assumed(
        self, tmp_path, repo
    ):
        """A markdown `.md` under Gemini is NOT a Gemini command."""
        src = _gemini_source(tmp_path)
        (src / "commands" / "notes.md").write_text(
            "---\nname: md\n---\n\nbody\n", "utf-8"
        )
        plan = command_import.build_plan(str(src), repo_path=repo, namespace="gemini")
        assert "md" not in {i.name for i in plan.by_kind("command")}

    def test_every_unconvertible_reason_comes_from_the_closed_vocabulary(
        self, tmp_path, repo
    ):
        """A reason nobody can tally is not a reason."""
        src = _claude_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo)
        assert plan.unconvertible, "the fixture produced nothing unconvertible"
        for item in plan.unconvertible:
            assert item.reason in command_import.UNCONVERTIBLE_REASONS
            assert item.detail.strip(), f"{item.name} has no detail"

    def test_the_receipt_enumerates_every_unconvertible_item(self, tmp_path, repo):
        """The user must be able to see exactly what they will lose."""
        src = _claude_source(tmp_path)
        receipt = command_import.adopt_command("plan", str(src), repo_path=repo)
        body = "\n".join(receipt.lines)
        for item in command_import.build_plan(str(src), repo_path=repo).unconvertible:
            assert item.name in body, f"the receipt omitted {item.name}"
        assert "NOT converted" in body
        assert "nothing was dropped silently" in body

    def test_the_summary_counts_state_the_counts(self, tmp_path, repo):
        """The headline numbers and the rows must agree."""
        src = _claude_source(tmp_path)
        plan = command_import.build_plan(str(src), repo_path=repo)
        body = "\n".join(command_import.import_lines(plan))
        assert f"{len(plan.converted)} would be written" in body
        assert f"{len(plan.unconvertible)} NOT converted" in body

    def test_a_whole_application_settings_file_is_reported_not_converted(
        self, tmp_path, repo
    ):
        """Credentials and provider routing must never be silently adopted."""
        src = _claude_source(tmp_path)
        (src / "settings.json").write_text(
            '{"env": {"ANTHROPIC_API_KEY": "sk-x"}}', "utf-8"
        )
        (src / "hooks").mkdir(exist_ok=True)
        (src / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
        plan = command_import.build_plan(str(src), repo_path=repo)
        names = {i.name for i in plan.unconvertible}
        assert "settings.json" in names, f"unconvertible: {sorted(names)}"
        assert "hooks" in names, f"unconvertible: {sorted(names)}"
        body = "\n".join(command_import.import_lines(plan))
        assert "sk-x" not in body, "a credential value leaked into the receipt"

    @pytest.mark.parametrize(
        "probe", ["", "   ", "nope-does-not-exist", "\x00bad", "C:/no/such/dir"]
    )
    def test_a_missing_or_hostile_source_is_a_report_not_an_exception(
        self, probe, repo
    ):
        """A hostile or absent path never raises out of the command."""
        receipt = command_import.adopt_command("plan", probe, repo_path=repo)
        assert receipt.lines, f"{probe!r} produced an empty receipt"
        assert not (repo / ".neo").exists()

    def test_the_scan_never_follows_a_path_outside_the_source(self, tmp_path, repo):
        """A symlink out of the source is not read as a source file."""
        src = _claude_source(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret\n", encoding="utf-8")
        try:
            (src / "commands" / "link.md").symlink_to(outside / "secret.md")
        except (OSError, NotImplementedError):
            pytest.skip("this platform refuses the symlink (a Windows privilege case)")
        plan = command_import.build_plan(str(src), repo_path=repo)
        assert not any("secret" in i.source_path for i in plan.items)


class TestAdoptIsATrustEvent:
    """Scan and show the blast radius before anything is written."""

    def test_the_trust_renderer_is_the_plugin_runtimes_own(self):
        """One enumeration, delegated - not a second copy that can drift."""
        source = (REPO_ROOT / "cli" / "command_import.py").read_text(encoding="utf-8")
        assert "plugin_runtime.trust_lines" in source
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "trust_lines" in calls, "trust_lines is not called by attribute"

    def test_the_report_is_the_plugin_runtimes_own_type(self, tmp_path, repo):
        """`TrustReport`, so a shape change cannot leave /adopt stale."""
        src = _claude_source(tmp_path)
        report = command_import.blast_radius(
            command_import.build_plan(str(src), repo_path=repo)
        ).as_trust_report()
        assert isinstance(report, plugin_runtime.TrustReport)

    def test_the_scan_verb_acts_and_writes_nothing(self, tmp_path, repo):
        """`scan` is the read-only trust report."""
        src = _claude_source(tmp_path)
        before = command_import.hash_tree(repo)
        receipt = command_import.adopt_command("scan", str(src), repo_path=repo)
        assert receipt.ok is True
        assert receipt.payload["requires_review"] is True
        assert "MCP servers that start" in "\n".join(receipt.lines)
        assert command_import.hash_tree(repo) == before

    def test_an_mcp_server_is_what_makes_an_import_a_trust_event(self, tmp_path, repo):
        """Instructions and commands alone are not; a server definition is."""
        plain = tmp_path / "plain"
        (plain / "commands").mkdir(parents=True)
        (plain / "commands" / "x.md").write_text("body\n", encoding="utf-8")
        quiet = command_import.build_plan(str(plain), repo_path=repo)
        assert command_import.blast_radius(quiet).requires_review is False
        loud = command_import.build_plan(_claude_source(tmp_path), repo_path=repo)
        assert command_import.blast_radius(loud).requires_review is True

    def test_an_unapproved_trust_requiring_apply_is_refused_and_writes_nothing(
        self, tmp_path, repo
    ):
        """Fail closed: no approver means no approval, not a default yes."""
        src = _claude_source(tmp_path)
        before = command_import.hash_tree(repo)
        receipt = command_import.adopt_command("apply", str(src), repo_path=repo)
        assert receipt.ok is False
        assert receipt.payload["error"] == "trust_not_approved"
        assert receipt.payload["wrote_nothing"] is True
        assert command_import.hash_tree(repo) == before
        assert not (repo / ".neo").exists()

    def test_an_approver_that_raises_is_a_refusal_not_an_approval(self, tmp_path, repo):
        """A broken confirmation dialog must not become consent."""

        def _boom(_radius):
            raise RuntimeError("the dialog exploded")

        receipt = command_import.adopt_command(
            "apply", str(_claude_source(tmp_path)), repo_path=repo, approve=_boom
        )
        assert receipt.ok is False
        assert "exploded" in receipt.payload["approval"]
        assert not (repo / ".neo").exists()

    def test_the_receipt_records_who_approved(self, tmp_path, repo):
        """An approval nobody can audit is not an approval."""
        receipt = command_import.adopt_command(
            "apply",
            str(_claude_source(tmp_path)),
            repo_path=repo,
            approve=lambda _r: True,
        )
        assert receipt.payload["approval"] == "approver"

    def test_the_scan_names_the_launch_command_and_what_it_can_reach(
        self, tmp_path, repo
    ):
        """A trust report that says "a server" and not which one is not a report."""
        plan = command_import.build_plan(str(_claude_source(tmp_path)), repo_path=repo)
        body = "\n".join(command_import.trust_lines(plan))
        assert "claude-gh" in body
        assert "gh-mcp stdio" in body
        assert "can reach:" in body

    def test_a_network_launcher_is_labelled_network_and_a_bare_binary_local(self):
        """The reach label is a claim, so it is derived and tested."""
        assert command_import._launch_reach("npx -y @scope/server") == "network"
        assert command_import._launch_reach("https://example.test/mcp") == "network"
        assert command_import._launch_reach("gh-mcp stdio") == "local"
        assert command_import._launch_reach("") == "unknown"

    def test_the_dry_run_flag_downgrades_an_apply_to_a_plan(self, tmp_path, repo):
        """`--dry-run` on `apply` must be the plan, not a quieter apply."""
        src = _claude_source(tmp_path)
        receipt = command_import.adopt_command(
            "apply", f"{src} --dry-run", repo_path=repo
        )
        assert receipt.verb == "plan"
        assert not (repo / ".neo").exists()


class TestAdoptRendersHostileNamesWithoutLosingThem:
    """Rule 3, with the control that makes the proof non-vacuous."""

    def test_a_hostile_command_description_survives_the_escaping_exit(
        self, tmp_path, repo
    ):
        """A description is DATA; through the escaping exit it must be VISIBLE."""
        plan = command_import.build_plan(
            str(_hostile_command_source(tmp_path)), repo_path=repo, namespace="src"
        )
        body = "\n".join(command_import.import_lines(plan))
        assert HOSTILE_EMPTY in body, "the hostile text never reached the receipt"
        escaped = "\n".join(
            command_import.escape_lines(command_import.import_lines(plan))
        )
        assert HOSTILE_EMPTY in _render(escaped, markup=True), (
            "the escaped receipt still lost the text through a markup parser"
        )

    def test_the_plain_lines_handed_straight_to_a_markup_sink_ARE_eaten(
        self, tmp_path, repo
    ):
        """The reason a surface must call `escape_lines`, not a hope.

        ``import_lines`` returns PLAIN text by contract - it must not escape,
        or a surface that escapes would double-escape. So the burden is on the
        caller, and this test is what makes that burden visible.
        """
        plan = command_import.build_plan(
            str(_hostile_command_source(tmp_path)), repo_path=repo, namespace="src"
        )
        body = "\n".join(command_import.import_lines(plan))
        assert HOSTILE_EMPTY in body, "the hostile text never reached the receipt"
        assert HOSTILE_EMPTY not in _render(body, markup=True), (
            "the unescaped plain lines SURVIVED, so nothing is at risk and the "
            "escaping proof above is void"
        )

    def test_the_escaped_lines_render_exactly_as_plain_text_would(self):
        """Escaping made the parser a no-op - the property, not the absence of `[`."""
        body = [f"before {HOSTILE_EMPTY} after", f"a name {HOSTILE_RAISES} here"]
        escaped = "\n".join(command_import.escape_lines(body))
        assert _render(escaped, markup=True) == _render("\n".join(body), markup=False)

    def test_the_unescaped_lines_are_eaten_or_raise_so_the_escape_proves_something(
        self,
    ):
        """Without this control the test above could pass vacuously."""
        assert _render(HOSTILE_EMPTY, markup=True).strip() == ""
        broken = _render(f"a {HOSTILE_RAISES}", markup=True)
        assert "render-error" in broken, (
            f"the raising control did not raise: {broken!r}"
        )

    def test_the_structural_exit_is_text_which_has_no_parser(self):
        """`safe_lines` is the answer that does not depend on escaping."""
        rows = command_import.safe_lines([f"a {HOSTILE_EMPTY}"])
        assert all(hasattr(row, "plain") for row in rows)
        assert all(HOSTILE_EMPTY in row.plain for row in rows)


class TestAdoptIsThePrimaryImplementationAndTheScriptSurfaceDelegates:
    """Rule 4: one behaviour, one function."""

    def test_no_per_verb_ladder_exists_in_the_module(self):
        """A ladder is where a second implementation grows."""
        source = (REPO_ROOT / "cli" / "command_import.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        ladders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(
                isinstance(c, ast.Constant) and c.value == "verb"
                for c in node.comparators
            )
        ]
        assert not ladders, "a per-verb ladder exists; use the ADOPT_VERBS mapping"

    def test_the_parser_registration_calls_the_same_command_function(self):
        """`neo adopt` and `/adopt` share `adopt_command`."""
        source = (REPO_ROOT / "cli" / "command_import.py").read_text(encoding="utf-8")
        inside = source[source.index("def register_adopt_parser") :]
        assert "adopt_command(" in inside, "the parser does not delegate to the command"

    def test_the_parser_registration_builds_a_real_parser_that_dispatches(
        self, tmp_path
    ):
        """The script surface is buildable and its handler reaches the receipt."""
        src = _claude_source(tmp_path)
        holder = argparse.ArgumentParser()
        sub = holder.add_subparsers(dest="cmd", required=True)
        command_import.register_adopt_parser(sub)
        args = holder.parse_args(
            ["adopt", "plan", str(src), "--repo", str(tmp_path / "repo")]
        )
        assert args.verb == "plan"
        assert args.func(args) == exit_codes.EXIT_CODES["success"], (
            "the script surface refused a plan for a source that exists"
        )

    def test_the_parser_reports_a_usage_error_as_exit_two(self, tmp_path, capsys):
        """A bad verb is a usage error in a script, exactly as in a shell."""
        holder = argparse.ArgumentParser()
        sub = holder.add_subparsers(dest="cmd", required=True)
        command_import.register_adopt_parser(sub)
        with pytest.raises(SystemExit) as excinfo:
            holder.parse_args(["adopt", "frobnicate", "claude"])
        assert excinfo.value.code == 2

    def test_the_parser_is_declared_every_verb_and_they_match_the_command(
        self, tmp_path
    ):
        """`choices` and `ADOPT_VERBS` cannot disagree."""
        holder = argparse.ArgumentParser()
        sub = holder.add_subparsers(dest="cmd", required=True)
        command_import.register_adopt_parser(sub)
        action = next(
            act
            for act in holder._subparsers._group_actions
            if isinstance(act, argparse._SubParsersAction)
        )
        parser = action.choices["adopt"]
        declared = next(
            a
            for a in parser._actions
            if "--help" not in a.option_strings and a.dest == "verb"
        )
        assert tuple(declared.choices) == command_import.ADOPT_VERBS

    def test_the_mount_is_not_applied_so_the_claim_is_not_overstated(self, tmp_path):
        """`cli/main.py` is another terminal's file; this round files the mount."""
        from cli import main as cli_main

        parser = cli_main.build_parser()
        action = next(
            act
            for act in parser._subparsers._group_actions
            if isinstance(act, argparse._SubParsersAction)
        )
        assert "adopt" not in action.choices, (
            "`neo adopt` is already mounted; update this pin and the handoff"
        )


class TestTheAdoptRegistryRowIsOneTheRegistryAccepts:
    """A row that fails `CommandSpec.__post_init__` cannot be appended."""

    def test_the_row_is_a_real_command_spec(self):
        """Built by the registry's own class, not a lookalike."""
        assert isinstance(command_import.ADOPT_SPEC, commands.CommandSpec)
        assert command_import.ADOPT_SPEC.name == "/adopt"

    def test_the_row_declares_the_two_tables_the_import_time_validator_requires(self):
        """Both rows are declared, so the mount is a paste."""
        name, policy = command_import.ADOPT_HEADLESS_ROW
        assert name == command_import.ADOPT_COMMAND_NAME
        assert policy in commands.HEADLESS_POLICIES
        key, equivalent = command_import.ADOPT_FLAG_ROW
        assert key == command_import.ADOPT_COMMAND_NAME
        assert equivalent.split()[0] in {"neo", "NEO_"} or equivalent.startswith("neo ")

    def test_the_row_is_absent_from_the_registry_so_the_claim_is_not_overstated(self):
        """This round cannot edit `cli/commands.py`; the pin says so loudly."""
        assert command_import.ADOPT_COMMAND_NAME not in _registry_names()
        assert command_import.ADOPT_COMMAND_NAME not in palette.COMMAND_GROUPS


class TestTheNameCollisionIsResolvedDeliberately:
    """Two meanings on one name is a defect; the resolution is recorded."""

    def test_import_and_adopt_are_different_commands(self):
        """The choice is a different name, and this is the pin."""
        assert command_import.ADOPT_COMMAND_NAME == "/adopt"
        assert commands.command_spec("/import") is not None
        assert commands.command_spec("/adopt") is None, (
            "/adopt exists in the registry, so the handoff landed; update this pin"
        )

    def test_the_adopt_alias_does_not_collide_with_the_import_row(self):
        """`/import-agent` is an alias of the NEW row, never of `/import`."""
        spec = commands.command_spec("/import")
        assert command_import.ADOPT_ALIASES[0] not in spec.aliases
        assert not set(command_import.ADOPT_ALIASES) & set(aliases.ALIASES)
        assert not set(command_import.ADOPT_ALIASES) & _registry_names()

    def test_the_resolution_is_documented_in_the_module_it_lives_in(self):
        """A decision recorded only in a chat log will be re-litigated."""
        source = (REPO_ROOT / "cli" / "command_import.py").read_text(encoding="utf-8")
        assert "REJECTED" in source and "CHOSEN" in source
        assert 'ADOPT_COMMAND_NAME = "/adopt"' in source

    def test_the_alias_module_does_not_silently_overload_import(self):
        """Nothing in the alias table points `/import` at the new verb."""
        for alias, target in aliases.ALIASES.items():
            assert not (alias == "/import" or target == "/import"), (
                "the alias table touches /import, which owns session exports"
            )
