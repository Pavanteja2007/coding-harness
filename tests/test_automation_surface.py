"""The automation surface: one vocabulary, one implementation, six exit codes.

VEX-CS-10. ``cli/main.py`` is the only file these tests exercise for
behaviour, and it declares two DATA tables that the tests read rather than
restate:

* ``cli.main.SCRIPT_FORM_COMMANDS`` - a top-level command a session command
  already hands the user verbatim, kept dispatchable and dropped from the
  top-level help LISTING.
* ``cli.main.ONE_IMPLEMENTATION`` - the declared relationship for every
  top-level command that a slash command also reaches, and HOW the two
  doors relate (``shared-handler`` / ``script-form`` / ``two-renderers`` /
  ``same-capability``).

Why the tests read the tables instead of restating the numbers: a test that
hard-codes the list it is checking cannot fail when the list drifts, and a
test that hard-codes a *different* list fails for a reason nobody can act on.
What these tests pin is the RELATIONSHIP between the parser, the slash
registry, the help output, and the receipts - which is the thing that can rot.

Host-only: no Docker, no provider, no network, no credential. Every path
under test writes inside ``tmp_path``. The one real subprocess lane reads
``neo --help`` and drives ``neo login``/``neo connect`` with standard input at
EOF; both are read-only or refusal paths.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from cli import command_exec as _exec  # noqa: E402
from cli import commands as _commands  # noqa: E402
from cli import interactive as _interactive  # noqa: E402
from cli import main as _main  # noqa: E402
from cli.exit_codes import EXIT_CODES  # noqa: E402

#: A real ``neo`` child process gets a bounded budget. A refusal that hangs
#: is the failure this suite exists to catch, so the bound is part of the
#: assertion rather than a safety net bolted on afterwards.
CHILD_TIMEOUT_S = 60

#: The roster this round DELIVERED, pinned by name rather than derived.
#:
#: Every other assertion in this file reads ``cli.main``'s tables, so it
#: proves the parser, the help screen, the docs, and the tables agree. It
#: cannot prove the tables shrank — emptying one would make half of the
#: suite vacuously true. The reduction is the deliverable, so the reduction
#: is pinned here: moving a command between the two lists becomes a
#: deliberate edit to BOTH halves, which is the point.
ADVERTISED_ROSTER = (
    "acp",
    "analyze-history",
    "capabilities",
    "completion",
    "dashboard",
    "doctor",
    "fix",
    "memory",
    "profile",
    "run",
    "run-benchmark",
    "scan",
    "serve",
    "support-bundle",
    "uninstall",
    "update",
)
SCRIPT_FORM_ROSTER = (
    "auth",
    "config",
    "connect",
    "hooks",
    "login",
    "logout",
    "mcp",
    "migrate",
    "plugin",
    "skills",
    "status",
    "watch",
    "worktree",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _help_text() -> str:
    """``neo --help`` from a REAL child process.

    A help screen assembled from the parser object would not catch a string
    that argparse formats differently, and the formatting is the thing under
    test.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "cli", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_S,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "NEO_NO_RELEASE_NOTICE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


_LISTING_ROW = re.compile(r"^ {4}(\S+)(?: {2,}|\s*$)")


def _flat(text: str) -> str:
    """Collapse whitespace so a wrapped help line still matches its sentence.

    argparse formats the epilog for the terminal width, so a long sentence is
    reflowed. Asserting the un-wrapped string would be asserting the width of
    the terminal that happened to run the test.
    """
    return " ".join(str(text).split())


def _listing_commands(help_text: str) -> List[str]:
    """The commands ``neo --help`` presents as top-level commands.

    Only the ``positional arguments:`` block counts. The ``{...}`` usage
    metavar is deliberately excluded: it is the DISPATCH inventory (it must
    stay complete so ``cli.capability``'s registry cross-check stays exact),
    while the listing is the VOCABULARY a reader is shown. Asserting on the
    metavar would assert the dispatch set, which this round deliberately did
    not shrink.
    """
    lines = help_text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip() == "positional arguments:":
            start = index
            break
    else:  # pragma: no cover - argparse always emits the block
        return []
    names: List[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        match = _LISTING_ROW.match(line)
        if match:
            names.append(match.group(1))
    return names


def _dispatchable_commands() -> List[str]:
    """Every top-level name the parser will actually dispatch."""
    parser = _main.build_parser()
    sub = _main._subparsers_of(parser)
    return sorted(str(name) for name in (getattr(sub, "choices", {}) or {}))


def _receipt_identity(record: Dict[str, Any]) -> Dict[str, Any]:
    """The fields two surfaces must agree on, byte for byte.

    Deliberately excludes ``surface`` (the provenance label, which MUST
    differ - a record a surface did not author is a record nobody can trust
    about which surface did what), ``message``/``text`` (captured display,
    never parsed for state), and ``timestamp`` (wall clock).
    """
    keys = (
        "command",
        "args",
        "status",
        "exit_code",
        "verdict",
        "verified",
        "state_before",
        "state_after",
        "task_id",
        "verification_state",
        "presentation",
        "recovery",
    )
    return {key: record.get(key) for key in keys}


def _event_shape(record: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    """The event sequence without the provenance label or the clock."""
    shape = []
    for event in record.get("events") or []:
        shape.append(
            (
                event.get("event"),
                event.get("command"),
                event.get("status"),
                event.get("exit_code"),
                event.get("state"),
                tuple(event.get("recovery") or ()),
            )
        )
    return shape


# ===========================================================================
# 1. The reduced surface is what --help shows
# ===========================================================================


class TestTheAutomationSurfaceIsWhatHelpShows:
    """`neo --help` presents the automation surface, labelled as one."""

    def test_the_advertised_roster_is_exactly_the_automation_surface(
        self,
    ) -> None:
        """The delivered reduction, pinned by name so it cannot evaporate."""
        surface = _main.automation_surface()
        assert sorted(surface["advertised"]) == list(ADVERTISED_ROSTER)
        assert sorted(surface["script_forms"]) == list(SCRIPT_FORM_ROSTER)
        assert len(ADVERTISED_ROSTER) == 16
        assert len(SCRIPT_FORM_ROSTER) == 13
        # 29 dispatchable public names -> 16 advertised + 13 script forms.
        assert len(ADVERTISED_ROSTER) + len(SCRIPT_FORM_ROSTER) == len(
            [n for n in _dispatchable_commands() if not n.startswith("_")]
        )

    def test_help_lists_the_advertised_commands_and_nothing_else(self) -> None:
        """The listing is the automation surface, not every dispatchable name."""
        help_text = _help_text()
        listed = _listing_commands(help_text)
        assert listed == _main.automation_surface()["advertised"], listed
        assert "serve" in listed and "run" in listed and "doctor" in listed

    def test_no_removed_command_is_presented_as_a_top_level_command(self) -> None:
        """A script form is not a peer of `/help` in the listing."""
        listed = _listing_commands(_help_text())
        for name in _main.SCRIPT_FORM_COMMANDS:
            assert name not in listed, f"{name} is still a top-level listing row"

    def test_every_removed_command_is_still_dispatchable(self) -> None:
        """Nothing was deleted - the door moved, the command did not."""
        dispatchable = _dispatchable_commands()
        for name in _main.SCRIPT_FORM_COMMANDS:
            assert name in dispatchable, f"{name} stopped dispatching"

    def test_help_reads_as_an_automation_tool(self) -> None:
        """The one-word label the round exists for."""
        assert "AUTOMATION surface" in _help_text()

    def test_help_names_the_slash_surface_as_the_product_in_one_line(
        self,
    ) -> None:
        """One line, and it names the door and the non-interactive runner."""
        note = _main.PRODUCT_SURFACE_NOTE
        assert _flat(note) in _flat(_help_text())
        assert "`/help`" in note
        assert 'neo run "/<command>' in note
        assert "\n" not in note, "a second paragraph of framing is a wall"

    def test_the_script_form_note_is_derived_and_names_only_hidden_commands(
        self,
    ) -> None:
        """The sentence cannot advertise a command the table does not hide."""
        note = _main.script_form_note()
        assert _flat(note) in _flat(_help_text())
        for name in _main.SCRIPT_FORM_COMMANDS:
            assert name in note
        for name in _main.automation_surface()["advertised"]:
            assert name not in note, f"{name} is advertised, not a script form"

    def test_the_usage_metavar_still_lists_every_dispatchable_command(
        self,
    ) -> None:
        """Pin the deliberate choice, so a later round cannot undo it blind.

        ``cli/capability.py`` derives the metavar from the registry AND
        cross-checks that registry against the live parser, failing on any
        difference. A command can therefore be hidden from the LISTING without
        breaking that gate, but it cannot be removed from the metavar without
        either editing another owner's file or silencing the cross-check. This
        test states which of the two happened.
        """
        usage = _help_text().split("positional arguments:")[0]
        for name in _dispatchable_commands():
            if name.startswith("_"):
                continue
            assert name in usage, f"{name} left the usage metavar"

    def test_a_script_form_the_parser_lacks_fails_the_build(self) -> None:
        """A table naming a door that does not exist is a build failure.

        Silently keeping the name would leave a registry row pointing at a
        command nobody can run, which is a lie a user acts on.
        """
        real = _main.SCRIPT_FORM_COMMANDS
        try:
            _main.SCRIPT_FORM_COMMANDS = dict(real)
            _main.SCRIPT_FORM_COMMANDS["a-door-that-does-not-exist"] = "/nope"
            parser = (
                _main.build_parser.__wrapped__
                if hasattr(_main.build_parser, "__wrapped__")
                else None
            )
            assert parser is None  # build_parser is not wrapped; use the helper
            with pytest.raises(RuntimeError) as excinfo:
                _main._apply_automation_surface(
                    _main._subparsers_of(_main.build_parser())
                )
            assert "a-door-that-does-not-exist" in str(excinfo.value)
        finally:
            _main.SCRIPT_FORM_COMMANDS = real

    def test_the_help_text_survives_a_markup_console_without_losing_a_line(
        self,
    ) -> None:
        """Rendered through a REAL rich console, every name is still VISIBLE.

        A substring assertion passes while a message is being eaten, so this
        renders the whole help screen through a markup-parsing ``Console`` and
        requires every advertised command, every script form, and the product
        note to be present in the RENDERED output.
        """
        from rich.console import Console

        console = Console(
            file=io.StringIO(), width=200, markup=True, force_terminal=False
        )
        console.print(_help_text())
        rendered = console.file.getvalue()
        for name in _main.automation_surface()["advertised"]:
            assert name in rendered, f"{name} did not survive rendering"
        for name in _main.SCRIPT_FORM_COMMANDS:
            assert name in rendered, f"{name} did not survive rendering"
        assert "the interactive product surface is the slash commands" in rendered


# ===========================================================================
# 2. Every removed capability is reachable through `neo run "/..."`
# ===========================================================================


class TestRemovedCapabilitiesAreReachableThroughNeoRun:
    """A hidden command is still reachable, and names its own door."""

    def test_every_flag_only_command_names_the_script_line_that_does_the_work(
        self, tmp_path: Path
    ) -> None:
        """`neo run "/<x>"` refuses and prints the exact line to run instead.

        This is the reachability proof for the removed half of the surface: a
        refusal that names the command is a DOOR, and a silent refusal would
        be a hole.
        """
        dispatchable = set(_dispatchable_commands())
        rows = dict(_commands.HEADLESS_FLAG_EQUIVALENTS)

        def _command_named(value: str) -> str:
            """The ``neo <name>`` a refusal points at, or "".

            One row is an ENV VAR, not a subcommand
            (``/effort`` -> ``NEO_EFFORT=<level> neo fix ...``), which is
            Terminal-07's recorded cross-owner request to either add a real
            flag or move the docstring. It is honoured here rather than
            special-cased away: the refusal must still resolve to a command
            this parser can run.
            """
            tokens = value.split()
            for index, token in enumerate(tokens[:-1]):
                if token == "neo":
                    return tokens[index + 1]
            return ""

        for name in sorted(rows):
            slash = name if name.startswith("/") else "/" + name
            result = _exec.run_command_line(
                slash, log_root=tmp_path / "logs", repo=tmp_path
            )
            assert result.status == "flag", (slash, result.status, result.text)
            assert result.exit_code == EXIT_CODES["usage_error"]
            required = result.requires.strip()
            assert "neo " in required, (slash, required)
            target = _command_named(required)
            assert target, f"{slash} names no command: {required!r}"
            assert target in dispatchable, (
                f"{slash} names `{required}`, which this parser cannot run"
            )
            assert required == rows[slash], (
                f"{slash} refusal names `{required}`; the registry row says "
                f"`{rows[slash]}` - the two must agree or a script copies the "
                f"wrong line"
            )

    def test_a_mapped_command_is_dispatched_rather_than_refused(
        self, tmp_path: Path
    ) -> None:
        """A mapped verb runs the handler instead of naming a flag."""
        for slash in ("/diagnostics", "/history", "/relevant"):
            result = _exec.run_command_line(
                slash, log_root=tmp_path / "logs", repo=tmp_path
            )
            assert result.status == "ok", (slash, result.status, result.text)
            assert result.exit_code == EXIT_CODES["success"]

    def test_the_run_surface_refuses_a_bare_line_honestly(self, tmp_path: Path) -> None:
        """No command line is a usage error, not a hang and not a pass."""
        result = _exec.run_command_line("", log_root=tmp_path / "logs", repo=tmp_path)
        assert result.exit_code == EXIT_CODES["usage_error"]
        assert result.status == "refused"
        assert "command line" in result.text

    def test_a_name_with_no_command_is_unknown_not_silently_accepted(
        self, tmp_path: Path
    ) -> None:
        """`unknown` must mean unknown, and must name the help surface."""
        result = _exec.run_command_line(
            "/nope-not-a-command", log_root=tmp_path / "logs", repo=tmp_path
        )
        assert result.status == "unknown"
        assert result.exit_code == EXIT_CODES["usage_error"]
        assert "/help" in result.text


# ===========================================================================
# 3 + 4. One implementation: the script door and the session door agree
# ===========================================================================


class TestOneImplementation:
    """A duplicate without a declared relationship is the defect."""

    def test_no_top_level_command_duplicates_a_slash_command_undeclared(
        self,
    ) -> None:
        """Compute the duplicates; require a row for every one.

        The duplicates are derived two ways because either one alone misses
        cases: a command whose NAME matches a slash command (``status`` /
        ``/status``), and a command a ``HEADLESS_FLAG_EQUIVALENTS`` row points
        a session user at (``config`` is named by three rows and by no
        slash command).
        """
        registry_names = {spec.name for spec in _commands.COMMAND_SPECS}
        rows = dict(_commands.HEADLESS_FLAG_EQUIVALENTS)
        declared = set(_main.ONE_IMPLEMENTATION)
        undeclared: List[str] = []
        for name in _dispatchable_commands():
            if name.startswith("_"):
                continue
            duplicate = f"/{name}" in registry_names or any(
                value.split()[1:2] == [name]
                for value in rows.values()
                if value.split()[:1] == ["neo"]
            )
            if duplicate and name not in declared:
                undeclared.append(name)
        assert undeclared == [], (
            "top-level commands that duplicate a slash command with no "
            f"declared one-implementation relationship: {undeclared}"
        )

    def test_every_declared_kind_is_from_the_closed_vocabulary(self) -> None:
        """A new word is an edit somebody makes, not a stronger claim."""
        for command, (_slash, how) in sorted(_main.ONE_IMPLEMENTATION.items()):
            assert how in _main.ONE_IMPLEMENTATION_KINDS, (command, how)

    def test_every_declared_slash_command_is_a_real_registry_row(self) -> None:
        """A declared relationship naming a command that does not exist is a lie."""
        names = {spec.name for spec in _commands.COMMAND_SPECS}
        for command, (slash_commands, _how) in sorted(_main.ONE_IMPLEMENTATION.items()):
            for slash in slash_commands:
                assert slash in names, (command, slash)

    def test_a_hidden_command_is_either_named_by_a_registry_row_or_declared(
        self,
    ) -> None:
        """Mechanically justify every command this round removed from help.

        Two accepted justifications and no third: a session command names it
        verbatim (a registry row's value starts with ``neo <name>``), or the
        one-implementation table declares it another script name for a
        capability whose door is a session command.
        """
        rows = dict(_commands.HEADLESS_FLAG_EQUIVALENTS)
        for name in sorted(_main.SCRIPT_FORM_COMMANDS):
            named = any(value.split()[:2] == ["neo", name] for value in rows.values())
            declared = _main.ONE_IMPLEMENTATION.get(name, ((), ""))[1]
            assert named or declared == "same-capability", (
                f"{name} is hidden from the top-level help with no reason a "
                f"test can check"
            )

    def test_the_headless_run_surface_calls_the_session_dispatcher(
        self,
    ) -> None:
        """One implementation, pinned in the SOURCE rather than by a receipt.

        A receipt can be made identical by two functions that happen to agree
        today. This reads ``cli/command_exec.py`` and requires the ONE call
        that runs a slash command to be the session's own dispatcher, so a
        second implementation cannot be added next to it unnoticed.
        """
        source = _exec.__file__ or ""
        text = Path(source).read_text(encoding="utf-8")
        calls = re.findall(r"_interactive\.(\w+)\(", text)
        dispatchers = [
            name for name in calls if "slash" in name or "subcommand" in name
        ]
        assert dispatchers, (
            "cli/command_exec.py no longer calls the session dispatcher - the "
            "headless surface has grown its own implementation"
        )
        assert text.count("_interactive._slash_command(") == 1

    @pytest.mark.parametrize(
        "verb", ["/diagnostics", "/history", "/relevant", "/doctor"]
    )
    def test_the_script_and_session_receipts_are_identical(
        self, verb: str, tmp_path: Path
    ) -> None:
        """`neo run "<verb>"` and the in-session dispatch agree, byte for byte.

        Everything except the provenance label and the clock must be equal:
        command, args, status, exit code, verdict, verification state, both
        states, presentation, recovery, and the whole event sequence.
        """
        log_root = tmp_path / "logs"
        state: Dict[str, Any] = {"repo": str(tmp_path), "file_config": {}}
        _interactive._slash_command(verb, verb, {}, log_root, state)
        repl = dict(state.get("last_command") or {})
        assert repl, f"{verb} recorded no session receipt"

        headless = _exec.run_command_line(
            verb, log_root=log_root, repo=tmp_path
        ).to_dict()

        assert _receipt_identity(repl) == _receipt_identity(headless), verb
        assert _event_shape(repl) == _event_shape(headless), verb
        assert repl.get("surface") == "repl"
        assert headless.get("status") == "ok"

    def test_a_verdict_can_never_be_promoted_by_either_surface(
        self, tmp_path: Path
    ) -> None:
        """`completed_unverified` stays unverified on the automation surface.

        Rule 6 in this round's own terms: a receipt that reported a run as
        verified without clean evidence would be the worst thing this suite
        could pass over, and the reduction is shared, so the check is cheap.
        """
        from cli import runview

        assert runview.run_verdict("completed_unverified") != "verified"
        assert runview.run_verdict("completed_verified", verification_state="") != (
            "verified"
        )
        result = _exec.run_command_line(
            "/status", log_root=tmp_path / "logs", repo=tmp_path
        )
        assert result.verdict != "verified"


# ===========================================================================
# 5. Script mode degrades honestly
# ===========================================================================


class TestScriptModeDegradesHonestly:
    """A dialog-needing verb says so in a non-TTY; it never opens one."""

    @pytest.mark.parametrize("argv", [["login"], ["connect"]])
    def test_a_dialog_verb_in_a_non_tty_exits_non_zero_without_hanging(
        self, argv: List[str], tmp_path: Path
    ) -> None:
        """Standard input at EOF: a clean refusal, a non-zero code, no hang.

        Driven as a real child process because the failure this catches is a
        child process blocking on ``input()``, which no in-process assertion can
        see. The wait is bounded and a timeout FAILS rather than passes.
        """
        stdin_file = tmp_path / "empty-stdin"
        stdin_file.write_text("", encoding="utf-8")
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "cli", *argv],
            cwd=str(REPO_ROOT),
            stdin=stdin_file.open("rb"),
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "NEO_NO_RELEASE_NOTICE": "1",
                "NEO_HOME": str(tmp_path / "neo-home"),
            },
        )
        elapsed = time.monotonic() - started
        assert elapsed < CHILD_TIMEOUT_S
        assert proc.returncode != 0, (argv, proc.returncode, proc.stdout)
        combined = _flat(f"{proc.stdout}\n{proc.stderr}")
        assert "interactive terminal" in combined, combined[:400]
        assert "Traceback" not in combined

    def test_the_run_surface_names_the_flag_instead_of_opening_a_dialog(
        self, tmp_path: Path
    ) -> None:
        """`/login` and `/connect` headlessly name the script line and stop."""
        for slash, expected in (("/login", "neo login"), ("/connect", "neo connect")):
            result = _exec.run_command_line(
                slash, log_root=tmp_path / "logs", repo=tmp_path
            )
            assert result.status == "flag", (slash, result.status)
            assert result.exit_code == EXIT_CODES["usage_error"]
            assert result.requires.strip() == expected
            assert expected in result.text


# ===========================================================================
# 6. Exit codes are the automation contract
# ===========================================================================


class TestTheSixExitCodesArePinned:
    """`cli/exit_codes.py` is the single numeric source of truth."""

    def test_all_six_exit_codes_are_the_declared_numbers(self) -> None:
        """0 / 1 / 2 / 3 / 4 / 130, by value and by name."""
        assert EXIT_CODES == {
            "success": 0,
            "task_failure": 1,
            "usage_error": 2,
            "environment_error": 3,
            "model_error": 4,
            "interrupted": 130,
        }

    def test_the_headless_surface_reports_those_codes(self) -> None:
        """A usage refusal is 2 and a dispatch is 0 - never a new number."""
        refused = _exec.run_command_line(
            "/nope-not-a-command", log_root=Path("logs"), repo=Path(".")
        )
        assert refused.exit_code == EXIT_CODES["usage_error"]

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["--no-such-flag"], 2),
            (["nope-not-a-command"], 2),
            (["fix"], 2),
        ],
    )
    def test_a_real_process_still_uses_two_for_usage(
        self, argv: List[str], expected: int
    ) -> None:
        """argparse usage errors keep their own exit code through ``main``."""
        proc = subprocess.run(
            [sys.executable, "-m", "cli", *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "NEO_NO_RELEASE_NOTICE": "1",
            },
        )
        assert proc.returncode == expected, (argv, proc.returncode, proc.stderr)
        assert "Traceback" not in proc.stderr

    def test_an_interrupted_session_still_reports_130(self) -> None:
        """The interactive branch maps Ctrl+C to 130 and nothing else."""
        source = Path(_main.__file__).read_text(encoding="utf-8")
        body = textwrap.dedent(
            source[
                source.index("def main(") : source.index(
                    "def _cleanup_after_interrupt("
                )
            ]
        )
        assert "return 130" in body
        assert "classify_exit_code(exc)" in body

    def test_classification_still_splits_environment_and_model(
        self,
    ) -> None:
        """3 and 4 are not new spellings of 1."""
        from execution.sandbox import SandboxUnavailableError

        status, code = _commands.command_failure(
            SandboxUnavailableError("docker daemon not reachable")
        )
        assert code == EXIT_CODES["environment_error"]
        status, code = _commands.command_failure(KeyboardInterrupt())
        assert code == EXIT_CODES["interrupted"]
        assert status == "cancelled"


# ===========================================================================
# 7. Documentation teaches the slash surface as primary
# ===========================================================================


class TestTheDocsTeachTheSlashSurface:
    """Documentation that teaches the wrong vocabulary is a defect."""

    def test_the_command_reference_does_not_present_a_script_form_as_top_level(
        self,
    ) -> None:
        """`docs/commands.md`'s top-level table lists only the automation surface."""
        text = (REPO_ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
        start = text.index("## Top-level commands")
        # Only the AUTOMATION table. The "Script forms" subsection below it is
        # where a removed command belongs, so the section boundary has to
        # stop at the next heading rather than at the next `##`.
        end = len(text)
        for marker in ("\n### ", "\n## "):
            found = text.find(marker, start + 5)
            if found != -1:
                end = min(end, found)
        table = text[start:end]
        for name in _main.SCRIPT_FORM_COMMANDS:
            row = re.compile(rf"^\|\s*`neo {re.escape(name)}`", re.MULTILINE)
            assert not row.search(table), (
                f"docs/commands.md still teaches `neo {name}` as a top-level "
                f"command; the session command is the door"
            )

    def test_the_command_reference_names_the_slash_surface_as_primary(
        self,
    ) -> None:
        text = (REPO_ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
        assert 'neo run "/' in text
        assert "/help" in text

    def test_the_readme_names_the_automation_surface(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert 'neo run "/' in text
        assert "AUTOMATION" in text or "automation surface" in text


# ===========================================================================
# 8. The surface receipt itself is well formed
# ===========================================================================


class TestTheSurfaceReceipt:
    """`automation_surface()` is the machine-readable form of the decision."""

    def test_the_receipt_names_every_dispatchable_command_exactly_once(
        self,
    ) -> None:
        receipt = _main.automation_surface()
        advertised = receipt["advertised"]
        hidden = sorted(receipt["script_forms"])
        every = advertised + hidden
        assert len(every) == len(set(every)), "a command is both advertised and hidden"
        dispatchable = [n for n in _dispatchable_commands() if not n.startswith("_")]
        assert sorted(every) == sorted(dispatchable), (
            "the receipt and the parser disagree; one of them is stale"
        )

    def test_the_receipt_serializes(self) -> None:
        """A receipt a script cannot parse is a comment."""
        payload = json.dumps(_main.automation_surface(), sort_keys=True)
        assert json.loads(payload)["schema"] == "neo.automation_surface/1"
