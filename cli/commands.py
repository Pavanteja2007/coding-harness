"""Custom commands (Plugins round, Task B): reusable slash-command
instruction templates, modeled on Claude Code's custom slash commands.

A command is a single markdown file:

    .neo/commands/<name>.md          (project — committed, shared)
    ~/.config/neo/commands/<name>.md (global — personal)
    ~/.config/neo/plugins/<plugin>/commands/<name>.md (from a plugin)

The file's content is an instruction template the user invokes
directly in the interactive session:

    neo › /review the auth module

`$ARGUMENTS` in the template is replaced by everything after the
command name (empty string when absent); `{{arg1}}`-style positional
placeholders from some other systems are deliberately NOT supported —
one substitution slot keeps templates simple and predictable.

Dispatch (cli/interactive.py::_slash_command): when the user types
`/<name> ...` and `<name>` is not a BUILT-IN session command
(/help /status /diff /sessions /resume /approve /reject /cancel
 /quiet /plan /compact /copy-diff /files /context /checkpoints, plus /trace /feed /steer /history

/init /model /login /logout /mcp /skills /cost /undo /clear — see
BUILTIN_SLASH_COMMANDS; /review stays custom-resolvable by design),
the custom-command loader resolves the template and the
session renders it — echo back first so the user sees exactly what
will run, then run it as a fix request whose issue text IS the filled
template (a custom command is a reusable instruction for the harness,
which is exactly what an issue is).

Discovery precedence on name collision: project > global > plugin
(same ordering rationale as skills — the specific beats the general).

All file IO is best-effort: a missing/unreadable/malformed command
file degrades to a plain "unknown command" hint, never a traceback.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "ARGUMENT_POLICIES",
    "BEHAVIOR_POLICIES",
    "BUILTIN_SLASH_COMMANDS",
    "COMMAND_EVENT_TYPES",
    "COMMAND_SPECS",
    "COMMAND_STATUSES",
    "INTERACTIVE_DISPATCH",
    "HEADLESS_FLAG_EQUIVALENTS",
    "HANDED_OFF_COMMANDS",
    "MODE_NAMES",
    "NAVIGATION_KEYS",
    "NO_RUN_VERDICT",
    "PERMISSION_SCOPES",
    "RECOVERY_ACTIONS",
    "REQUIRED_COMMANDS",
    "RESULT_PRESENTATIONS",
    "SHORTCUT_KEYS",
    "SHORTCUT_PURPOSE",
    "SUBCOMMANDS",
    "SUBRESULT_PRESENTATIONS",
    "SURFACES",
    "SURFACE_SHORTCUTS",
    "SURFACE_STATES",
    "VERB_HINT_COMMANDS",
    "WORKTREE_ACTIONS",
    "ApprovalGrant",
    "ApprovalPolicy",
    "ApprovalRequestView",
    "CommandAvailability",
    "CommandContext",
    "CommandEvent",
    "CommandOutcome",
    "CommandResolution",
    "CommandSpec",
    "ModeSpec",
    "SubcommandResolution",
    "SubcommandSpec",
    "WorktreeCommandError",
    "approval_from_answer",
    "approval_gate_outcome",
    "approval_request_view",
    "argument_hint",
    "cmd_worktree",
    "command_availability",
    "command_failure",
    "command_names",
    "command_outcome",
    "command_palette_entries",
    "command_record",
    "command_recovery_hint",
    "command_spec",
    "command_specs",
    "command_usage",
    "command_verdict",
    "contextual_command_hints",
    "default_subcommand",
    "fill_template",
    "headless_equivalent",
    "headless_policy",
    "interactive_dispatch_refusal",
    "is_custom_command_line",
    "keyboard_shortcuts",
    "list_commands",
    "load_command",
    "approval_opt_out",
    "mode_config",
    "mode_permission",
    "mode_spec",
    "mode_specs",
    "mode_tool_visible",
    "mode_writes",
    "normalize_approval_scope",
    "normalize_mode",
    "normalize_terminal_state",
    "reset_session_approval_policy",
    "resolve_agent_approval",
    "resolve_command_line",
    "resolve_subcommand",
    "session_approval_policy",
    "subcommand_names",
    "subcommand_specs",
    "subcommand_verb",
    "surface_command_context",
    "unknown_command_line",
    "worktree_list",
    "worktree_new",
    "worktree_path",
    "worktree_remove",
    "worktree_root",
    "worktree_run_config",
]

ARGUMENT_POLICIES = frozenset({"none", "optional", "required"})
BEHAVIOR_POLICIES = frozenset({"allow", "refuse", "queue"})
RESULT_PRESENTATIONS = frozenset(
    {
        "inline",
        "browser",
        "modal",
        "card",
        "diff",
        "settings",
        "approval",
        "exit",
    }
)

#: What a SUBCOMMAND may return. Narrower than ``RESULT_PRESENTATIONS`` on
#: purpose: a verb that ACTS must never end in a dialog. The full set carries
#: ``browser``/``modal`` for the no-argument case, which is the only case that
#: opens a menu. Keeping the two vocabularies separate is what makes "a verb
#: that ends in a dialog is not a verb" a checkable property of the registry
#: rather than a convention somebody remembers.
SUBRESULT_PRESENTATIONS = frozenset(
    {"inline", "card", "diff", "settings", "receipt", "exit"}
)

#: Whether an interactive surface dispatches a command's own handler, or
#: refuses with the flag that owns the work. See
#: ``CommandSpec.interactive_dispatch`` for why this is a field and not a
#: branch in one shell.
INTERACTIVE_DISPATCH = frozenset({"handled", "flag-only"})


SURFACE_STATES = (
    "idle",
    "running",
    "waiting_for_approval",
    "cancelled",
    "failed",
    "completed_verified",
    "completed_unverified",
    "blocked",
    "resumed",
)
COMMAND_EVENT_TYPES = ("command_started", "state_changed", "command_finished")

#: The three terminal surfaces, declared. A surface that is not in this
#: tuple cannot be recorded by :func:`command_record`, so "add a fourth
#: surface" is an explicit edit here rather than a silently accepted
#: misspelling in a ``surface="tui"`` argument.
SURFACES = ("repl", "tui", "headless")

#: Every status word a :class:`CommandOutcome` may carry, declared. The
#: closed set is what makes ``status`` comparable across surfaces: a
#: script can branch on it, and a new word cannot appear in one shell
#: without being added here (and therefore reviewed) first.
COMMAND_STATUSES = frozenset(
    {
        "ok",
        "unknown",
        "not_command",
        "invalid",
        "disabled",
        "hidden",
        "refused",
        "queued",
        "flag",
        "failed",
        "error",
        "cancelled",
    }
)

#: The verdict a command with NO run behind it reports. A run vocabulary
#: value would be a fabrication in either direction: ``verified`` claims
#: a verification nobody ran, and any failure value reads as a failed run
#: that does not exist. The command's own success is its ``exit_code``,
#: which is already in the document. Shared by every surface (headless
#: used to define its own copy).
NO_RUN_VERDICT = "no_run"

RECOVERY_ACTIONS = frozenset(
    {
        "retry",
        "edit-input",
        "cancel-command",
        "resume",
        "undo",
        "inspect-trace",
        "return-safe-state",
    }
)

# Headless surface contract. "mapped" runs the SAME handler the REPL uses
# with its output captured; "flag-only" means a first-class CLI flag already
# owns the work (the adapter points at it instead of duplicating it);
# "refuse" means the command needs a live run or interactive input, which a
# non-TTY session must never fake.
HEADLESS_POLICIES = frozenset({"mapped", "flag-only", "refuse"})

HEADLESS_COMMAND_POLICIES: Mapping[str, str] = {
    "/approve": "mapped",
    "/ask": "refuse",
    "/attach": "mapped",
    "/build": "refuse",
    "/cancel": "refuse",
    "/checkpoints": "mapped",
    "/clear": "refuse",
    "/compact": "refuse",
    "/connect": "flag-only",
    "/context": "mapped",
    "/copy-diff": "refuse",
    "/cost": "flag-only",
    "/detach": "refuse",
    "/diagnostics": "mapped",
    "/diff": "mapped",
    "/doctor": "mapped",
    "/effort": "flag-only",
    "/export": "refuse",
    "/feed": "mapped",
    "/files": "mapped",
    "/fork": "mapped",
    "/help": "mapped",
    "/history": "mapped",
    "/hooks": "flag-only",
    "/import": "mapped",
    "/init": "flag-only",
    "/login": "flag-only",
    "/logout": "flag-only",
    "/mcp": "flag-only",
    "/migrate": "flag-only",
    "/model": "flag-only",
    "/mode": "refuse",
    "/open": "mapped",
    "/plan": "refuse",
    "/plugins": "flag-only",
    "/quiet": "mapped",
    "/quit": "refuse",
    "/recover": "mapped",
    "/redo": "mapped",
    "/relevant": "mapped",
    "/reject": "mapped",
    "/repo": "mapped",
    "/resume": "mapped",
    "/review": "refuse",
    "/sessions": "mapped",
    "/settings": "flag-only",
    "/share": "refuse",
    "/skills": "flag-only",
    "/status": "flag-only",
    "/steer": "refuse",
    "/support-bundle": "flag-only",
    "/theme": "flag-only",
    "/trace": "mapped",
    "/undo": "mapped",
    "/watch": "flag-only",
    "/worktree": "flag-only",
}

HEADLESS_FLAG_EQUIVALENTS: Mapping[str, str] = {
    "/connect": "neo connect",
    "/cost": "neo status --task-id <task-id>",
    "/effort": "NEO_EFFORT=<level> neo fix ...",
    "/hooks": "neo hooks list",
    "/init": "neo config init-project",
    "/login": "neo login",
    "/logout": "neo logout",
    "/mcp": "neo mcp list",
    "/migrate": "neo migrate",
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
PERMISSION_SCOPES = frozenset(
    {
        "session:read",
        "journal:read",
        "workspace:read",
        "workspace:write",
        "filesystem:write",
        "settings:read",
        "settings:write",
        "session:write",
        "run:control",
        "agent:run",
        "approval:decide",
        "extension:read",
        "external:read",
        "session:control",
    }
)

#: The CLOSED vocabulary of keys a command may declare. Prompt 04 lists
#: "keyboard shortcuts" beside the palette and the footer hints, which is only
#: meaningful if the command specification carries them: before this, the
#: shortcuts existed solely as raw Textual ``BINDINGS`` rows, so the palette
#: could not print a key, ``/help`` could not teach one, and rebinding a key
#: could not fail anything. A closed set means a typo is rejected at
#: construction instead of rendering as a key that does nothing.
SHORTCUT_KEYS = frozenset(
    {
        "ctrl+b",
        "ctrl+c",
        "ctrl+g",
        "ctrl+p",
        "ctrl+q",
        "ctrl+r",
        "ctrl+x",
        "ctrl+y",
    }
)

#: What each declared key actually DOES, in the user's words. Two keys can
#: reach the same command (ctrl+g and ctrl+b are both `/steer`, one at the
#: next safe boundary and one queued), so the key - not the command - is the
#: unit that needs a purpose.
SHORTCUT_PURPOSE: Mapping[str, str] = {
    "ctrl+5": "show, hide, or auto-size the sidebar",
    "ctrl+b": "queue steering without interrupting the run",
    "ctrl+c": "cancel the run (checkpoints kept), or quit when idle",
    "ctrl+g": "steer at the next safe boundary",
    "ctrl+o": "switch between comfortable and compact density",
    "ctrl+p": "open the command palette",
    "ctrl+q": "quit the session",
    "ctrl+r": "search the input history",
    "ctrl+x": "cancel the run (checkpoints kept)",
    "ctrl+y": "copy the selected text",
}

#: Keys that open a SURFACE rather than run a command. They are still part of
#: the one command system - a user looking for a key asks the same place - so
#: they are declared here instead of living only in the TUI's binding table.
SURFACE_SHORTCUTS: Tuple[Tuple[str, str], ...] = (
    ("ctrl+p", "palette"),
    ("ctrl+r", "history"),
    ("ctrl+y", "copy"),
    # The layout shell (cli/design.py + cli/tui.py) added two layout toggles.
    # They reach a SURFACE control rather than a command, so they belong in
    # this group; the two tables are pinned against the TUI's BINDINGS in both
    # directions, and a bound key nothing declares is a key nobody can find.
    ("ctrl+5", "sidebar"),
    ("ctrl+o", "density"),
)

#: Keys that move around what is already on screen. They are neither a
#: command nor a surface, so declaring them is what lets the drift gate say
#: "every bound key is declared" with NO exemption list to keep in sync -
#: an exemption list is just a place for the next unbound key to hide.
NAVIGATION_KEYS: Tuple[Tuple[str, str], ...] = (
    ("ctrl+space", "complete an @path mention under the cursor"),
    ("shift+up", "scroll the transcript back"),
    ("shift+down", "scroll the transcript forward"),
    ("shift+pageup", "scroll the transcript back a page"),
    ("shift+pagedown", "scroll the transcript forward a page"),
    ("shift+end", "jump back to the newest output"),
)

REQUIRED_COMMANDS = (
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


def _command_type_names() -> frozenset:
    """Return the closed command-type vocabulary.

    A one-line delegation to the module that owns the enum, imported lazily so
    this module and `cli.command_types` cannot form an import cycle, and typed
    as a ``frozenset`` because the caller only ever membership-tests it.
    """
    from cli import command_types

    return frozenset(t.name for t in command_types.TYPES)


@dataclass(frozen=True)
class CommandSpec:
    """Canonical behavior and presentation metadata for one command."""

    name: str
    summary: str
    aliases: Tuple[str, ...]
    argument_policy: str
    idle_policy: str
    in_flight_policy: str
    palette_behavior: str
    template_resolvable: bool = False
    argument_hint: str = ""
    required_permissions: Tuple[str, ...] = ()
    result_presentation: str = "inline"
    failure_recovery: Tuple[str, ...] = (
        "retry",
        "edit-input",
        "inspect-trace",
        "return-safe-state",
    )
    shortcuts: Tuple[str, ...] = ()
    hidden: bool = False
    hidden_reason: str = ""
    headless_policy: str = "mapped"
    #: The verbs this command dispatches on, in hint order. EMPTY for a
    #: command with no actions. This is a PROJECTION of :data:`SUBCOMMANDS`,
    #: not a second declaration: :func:`_with_subcommands` fills it from the
    #: registry after the specs are built, so a verb that exists in one table
    #: and not the other is impossible to express.
    subcommands: Tuple[str, ...] = ()
    #: The subset of :attr:`subcommands` that MUTATES state. Read from the
    #: subcommand registry (each ``SubcommandSpec.mutating``) and never
    #: restated, which is the R2-15 §11.1 request: before this field a
    #: mutating verb acted under the command's read-only permission tuple,
    #: because the registry had no per-verb axis at all.
    mutating_verbs: Tuple[str, ...] = ()
    #: Commands whose subcommand registry has an ``enabled``/``disable`` pair
    #: or a browser-shaped no-argument case. Set by the same projection, and
    #: read by the tests that assert a verb never opens a modal.
    opens_browser_without_argument: bool = False
    #: Does an INTERACTIVE surface dispatch this command's own handler?
    #:
    #: ``"handled"`` (the default) means both shells run it. ``"flag-only"``
    #: means the registry row is a DOOR whose handler another surface or
    #: terminal owns, and an interactive call is refused with the flag that
    #: does the work rather than falling through to "unknown command" after
    #: the preflight already said yes.
    #:
    #: It exists because the two shells MUST dispatch the same set of commands
    #: (``test_cli_terminal_parity.py`` reads both dispatchers with ``ast`` and
    #: asserts set equality), and this round does not own ``cli/tui.py``. A REPL
    #: branch for a command the TUI cannot run would break that gate for
    #: every future command; a data flag keeps the surface honest on both and
    #: reduces the handoff to one field. The rows that carry it name their
    #: owner in the docstring below.
    interactive_dispatch: str = "handled"
    #: WHAT KIND of command this is - ``local`` | ``local_ui`` | ``prompt`` |
    #: ``skill``. The axis :attr:`result_presentation` cannot express: that one
    #: says HOW a result is shown, this one says whether the handler can reach
    #: a provider, whether it may open a modal, and whether it finishes
    #: instantly while a run is live.
    #:
    #: Declared as a STRING here and resolved through
    #: :func:`cli.command_types.command_type`, so this module keeps exactly one
    #: classification table and the enum stays in the module that owns it
    #: (VEX-CS-02). An undeclared value resolves to that module's
    #: ``DEFAULT_COMMAND_TYPE`` rather than raising, because a row added without
    #: a type must keep behaving exactly as it did before - and the default
    #: (``LOCAL``) grants no model call, no modal and no gate, so an undeclared
    #: command cannot silently start spending tokens.
    #:
    #: Validated in ``__post_init__`` against the closed vocabulary, because a
    #: typo here would otherwise read as "a command that costs nothing".
    command_type: str = ""

    def __post_init__(self) -> None:
        """Reject malformed command metadata before a UI can consume it."""
        if self.argument_policy not in ARGUMENT_POLICIES:
            raise ValueError(f"unsupported argument policy: {self.argument_policy}")
        if self.idle_policy not in BEHAVIOR_POLICIES - {"queue"}:
            raise ValueError(f"unsupported idle behavior: {self.idle_policy}")
        if self.in_flight_policy not in BEHAVIOR_POLICIES:
            raise ValueError(f"unsupported in-flight behavior: {self.in_flight_policy}")
        if self.palette_behavior not in {"run", "prefill"}:
            raise ValueError(f"unsupported palette behavior: {self.palette_behavior}")
        if self.result_presentation not in RESULT_PRESENTATIONS:
            raise ValueError(
                f"unsupported result presentation: {self.result_presentation}"
            )
        unknown_permissions = set(self.required_permissions) - PERMISSION_SCOPES
        if unknown_permissions:
            raise ValueError(
                "unknown command permissions: " + ", ".join(sorted(unknown_permissions))
            )
        unknown_recovery = set(self.failure_recovery) - RECOVERY_ACTIONS
        if unknown_recovery:
            raise ValueError(
                "unknown recovery actions: " + ", ".join(sorted(unknown_recovery))
            )
        unknown_shortcuts = set(self.shortcuts) - SHORTCUT_KEYS
        if unknown_shortcuts:
            raise ValueError(
                "unknown keyboard shortcuts: "
                + ", ".join(sorted(unknown_shortcuts))
                + f" (known: {', '.join(sorted(SHORTCUT_KEYS))})"
            )
        if self.interactive_dispatch not in INTERACTIVE_DISPATCH:
            raise ValueError(
                f"unsupported interactive dispatch: {self.interactive_dispatch}"
            )
        if self.command_type and self.command_type not in _command_type_names():
            raise ValueError(
                f"unsupported command type: {self.command_type} "
                f"(known: {', '.join(sorted(_command_type_names()))})"
            )

    @property
    def type_name(self) -> str:
        """Return the RESOLVED command type, filling in the default.

        Read through the enum rather than restated, so a row that declares no
        type and a row that declares the default type are indistinguishable -
        which is the point of having a default at all.
        """
        from cli import command_types

        return command_types.command_type(self).name

    @property
    def model_consuming(self) -> bool:
        """Return whether running this command can spend provider tokens."""
        from cli import command_types

        return command_types.is_model_consuming(self)

    @property
    def may_open_modal(self) -> bool:
        """Return whether this command's TYPE may open a modal component."""
        from cli import command_types

        return command_types.may_open_modal(self)

    @property
    def may_gate(self) -> bool:
        """Return whether this command's TYPE may raise a confirmation gate.

        A gate is not a modal: it is the "about to change your files, proceed?"
        prompt a model workflow raises before it runs, and only a model-consuming
        type ever has to ask.
        """
        from cli import command_types

        return command_types.may_gate(self)

    @property
    def runs_instantly_while_busy(self) -> bool:
        """Return whether this command finishes instantly during a live run."""
        from cli import command_types

        return command_types.runs_instantly_while_busy(self)

    @property
    def idle_behavior(self) -> str:
        """Return the Prompt 04 idle-behavior spelling."""
        return self.idle_policy

    @property
    def in_flight_behavior(self) -> str:
        """Return the Prompt 04 in-flight-behavior spelling."""
        return self.in_flight_policy

    @property
    def permissions(self) -> Tuple[str, ...]:
        """Return the required permission scopes."""
        return self.required_permissions

    def shortcut_label(self) -> str:
        """Return the keys that reach this command, as one copyable string."""
        return " / ".join(self.shortcuts)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly command specification."""
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "summary": self.summary,
            "argument_policy": self.argument_policy,
            "argument_hint": self.argument_hint,
            "idle_behavior": self.idle_policy,
            "in_flight_behavior": self.in_flight_policy,
            "required_permissions": list(self.required_permissions),
            "result_presentation": self.result_presentation,
            "failure_recovery": list(self.failure_recovery),
            "shortcuts": list(self.shortcuts),
            "palette_behavior": self.palette_behavior,
            "template_resolvable": self.template_resolvable,
            "hidden": self.hidden,
            "hidden_reason": self.hidden_reason,
            "headless_policy": self.headless_policy,
            "subcommands": list(self.subcommands),
            "mutating_verbs": list(self.mutating_verbs),
            # VEX-CS-02. Additive keys only: an existing consumer reading
            # `result_presentation` sees exactly what it saw before, and a new
            # consumer can ask what KIND of command this is without reaching
            # into `cli.command_types` itself.
            "command_type": self.type_name,
            "model_consuming": self.model_consuming,
        }


@dataclass(frozen=True)
class SubcommandResolution:
    """One resolved verb, and why it is not runnable when it is not.

    ``verb`` is ``None`` for a command with no subcommand registry at all and
    for a bare call (the no-argument case is the DEFAULT verb, which the
    caller resolves through :func:`default_subcommand`). ``ok`` distinguishes
    "this is not a verb of this command" from "this verb cannot run here", and
    the second carries the reason and the recovery.
    """

    command: str
    verb: str
    spec: Optional[SubcommandSpec]
    rest: str
    ok: bool = True
    message: str = ""
    known: bool = True
    recovery: Tuple[str, ...] = ()
    exit_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly verb-resolution record."""
        return {
            "command": self.command,
            "verb": self.verb,
            "rest": self.rest,
            "ok": self.ok,
            "known": self.known,
            "message": self.message,
            "recovery": list(self.recovery),
            "exit_code": self.exit_code,
            "mutating": bool(self.spec is not None and self.spec.mutating),
            "result_presentation": (
                self.spec.result_presentation if self.spec is not None else ""
            ),
        }


def _free_text_verb(spec: CommandSpec) -> Optional[SubcommandSpec]:
    """Return the verb that absorbs an unrecognised first word, if any.

    The single place the "was this always accepted?" question is answered, so
    the resolver has no command-specific branch and a new command has to
    declare the behaviour rather than inherit it.
    """
    for verb in subcommand_specs(spec.name):
        if verb.free_text_argument:
            return verb
    return None


def default_subcommand(name: str) -> Optional[SubcommandSpec]:
    """Return the verb a no-argument call means, when one is declared.

    The interactive menu lives on this one row and nowhere else, which is what
    makes "a verb that ends in a dialog is not a verb" true by construction
    rather than by review.
    """
    for verb in subcommand_specs(name):
        if verb.default:
            return verb
    verbs = subcommand_specs(name)
    return verbs[0] if verbs else None


def _usage_refusal(spec: CommandSpec, verb: str) -> str:
    """The ONE sentence for an unknown subcommand.

    Reuses :func:`command_usage` so the valid set is listed from the same
    place the composer hint reads it, rather than a second sentence written
    here. A user who types ``/plugin instal`` is told what is accepted, not
    shown a menu.
    """
    return f"unknown subcommand: {spec.name} {verb} — {command_usage(spec)}"


def resolve_subcommand(
    spec: Optional[CommandSpec], args: str
) -> Optional[SubcommandResolution]:
    """Resolve the first word of one command's arguments to a declared verb.

    Returns ``None`` for a command with no verb registry - the resolution
    path then behaves exactly as it did before this round, which is what keeps
    all 52 pre-existing commands byte-identical.

    A command WITH a registry rejects an unrecognised first word as a usage
    error. That is the deliberate change: a REPL filters and an agent takes
    verbs, and a verb registry whose typo silently falls through to the
    browser is a browser with extra typing.
    """
    if spec is None or not spec.subcommands:
        return None
    text = str(args or "").strip()
    if not text:
        verb = default_subcommand(spec.name)
        return SubcommandResolution(
            command=spec.name,
            verb=verb.name if verb is not None else "",
            spec=verb,
            rest="",
            recovery=verb.failure_recovery if verb is not None else (),
        )
    head, _, rest = text.partition(" ")
    first = head.strip().lower()
    verb = subcommand_verb(spec.name, first)
    if verb is None:
        fallback = _free_text_verb(spec)
        if fallback is not None:
            # A historical free-text first argument (`/mcp <label>`,
            # `/skills <filter>`). The WHOLE argument string is the verb's
            # argument, not just the head, so a multi-word filter still works.
            return SubcommandResolution(
                command=spec.name,
                verb=fallback.name,
                spec=fallback,
                rest=text,
                recovery=fallback.failure_recovery,
            )
        return SubcommandResolution(
            command=spec.name,
            verb=first,
            spec=None,
            rest=rest.strip(),
            ok=False,
            known=False,
            message=_usage_refusal(spec, first),
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    remaining = rest.strip()
    if verb.argument_policy == "none" and remaining:
        return SubcommandResolution(
            command=spec.name,
            verb=verb.name,
            spec=verb,
            rest=remaining,
            ok=False,
            message=verb.usage(spec.name),
            recovery=verb.failure_recovery,
            exit_code=2,
        )
    if verb.argument_policy == "required" and not remaining:
        return SubcommandResolution(
            command=spec.name,
            verb=verb.name,
            spec=verb,
            rest="",
            ok=False,
            message=verb.usage(spec.name),
            recovery=verb.failure_recovery,
            exit_code=2,
        )
    return SubcommandResolution(
        command=spec.name,
        verb=verb.name,
        spec=verb,
        rest=remaining,
        recovery=verb.failure_recovery,
    )


@dataclass(frozen=True)
class CommandContext:
    """Surface state used to resolve command availability consistently."""

    surface: str = "interactive"
    in_flight: bool = False
    waiting_for_approval: bool = False
    has_task: Optional[bool] = None
    pending_approval: Optional[bool] = None
    denied_permissions: Tuple[str, ...] = ()
    disabled_reasons: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CommandAvailability:
    """Whether a command can execute and why it cannot."""

    available: bool
    hidden: bool = False
    reason: str = ""
    note: str = ""


@dataclass(frozen=True)
class CommandResolution:
    """One validated command invocation before a surface handler runs."""

    raw: str
    spec: Optional[CommandSpec]
    args: str
    status: str
    message: str = ""
    recovery: Tuple[str, ...] = ()
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        """Return whether the surface handler may execute."""
        return self.status == "ok"

    @property
    def command(self) -> str:
        """Return the canonical command name or the raw first token."""
        return (
            self.spec.name if self.spec is not None else self.raw.split(maxsplit=1)[0]
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly command-resolution record."""
        return {
            "raw": self.raw,
            "command": self.command,
            "args": self.args,
            "status": self.status,
            "message": self.message,
            "recovery": list(self.recovery),
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class CommandEvent:
    """One normalized command lifecycle event shared by every surface."""

    event: str
    surface: str
    command: str
    state: str
    status: str = "accepted"
    exit_code: int = 0
    message: str = ""
    task_id: str = ""
    verification_state: str = "not_run"
    recovery: Tuple[str, ...] = ()
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly normalized event row."""
        return {
            "schema_version": 1,
            "event": self.event,
            "surface": self.surface,
            "command": self.command,
            "state": self.state,
            "status": self.status,
            "exit_code": self.exit_code,
            "message": self.message,
            "task_id": self.task_id,
            "verification_state": self.verification_state,
            "recovery": list(self.recovery),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CommandOutcome:
    """One surface-independent command result and its event sequence."""

    command: str
    args: str
    surface: str
    status: str
    state_before: str
    state_after: str
    exit_code: int
    events: Tuple[CommandEvent, ...]
    message: str = ""
    presentation: str = "inline"
    recovery: Tuple[str, ...] = ()
    task_id: str = ""
    verification_state: str = "not_run"
    #: The fail-closed RUN verdict this command observed, reduced at
    #: construction time from the run's own status and evidence. A field
    #: and not a property on purpose: the property version reduced the
    #: COMMAND's lifecycle word ("ok") and therefore reported
    #: ``unverified`` for a run whose journal said ``completed_verified``
    #: with clean evidence — a verified run that the machine record
    #: insists is unverified. Present on EVERY surface, which is the
    #: point: the headless adapter used to be the only one carrying a
    #: verdict, so a script could ask ``verified`` of a headless run and
    #: a TUI user could not.
    verdict: str = NO_RUN_VERDICT

    @property
    def ok(self) -> bool:
        """Return whether the command completed with exit code zero."""
        return self.exit_code == 0

    @property
    def state(self) -> str:
        """Return the state after command handling."""
        return self.state_after

    @property
    def verified(self) -> bool:
        """Whether the observed run is verified on clean evidence alone."""
        return self.verdict == "verified"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly outcome with normalized event rows."""
        return {
            "schema_version": 1,
            "command": self.command,
            "args": self.args,
            "surface": self.surface,
            "status": self.status,
            "state": self.state_after,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "exit_code": self.exit_code,
            "message": self.message,
            "presentation": self.presentation,
            "recovery": list(self.recovery),
            "task_id": self.task_id,
            "verification_state": self.verification_state,
            "verdict": self.verdict,
            "verified": self.verified,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class ApprovalRequestView:
    """Redacted, exact-effect approval data shared by every terminal surface."""

    request_id: str
    fingerprint: str
    task_id: str
    repo_path: str
    paths: Tuple[str, ...]
    command: str
    server: str
    side_effect: str
    diff: str
    issue_text: str
    summary: str
    timeout_s: Optional[float] = None

    def effect_signature(self, scope: str = "session") -> Tuple[str, ...]:
        """Return the dimensions a grant of ``scope`` must match.

        A session grant is deliberately narrow: the SAME repository, side
        effect, command, server, and paths. Path and command grants relax
        exactly the dimension they generalize — a path grant still pins the
        repository and side effect, a command grant still pins the
        repository and server — so widening a scope never silently widens
        the others. Mirrors the kernel's once / exact-call / session-path /
        session-command-prefix split in harness/agent_kernel/policy.py.
        """
        repo = self.repo_path.replace("\\", "/").rstrip("/").lower()
        effect = self.side_effect.strip().lower()
        server = self.server.strip().lower()
        if scope == "command":
            return (repo, effect, server)
        if scope == "path":
            return (repo, effect, self.command.strip(), server)
        return (
            repo,
            effect,
            self.command.strip(),
            server,
            *tuple(path.replace("\\", "/").lstrip("/") for path in self.paths),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly exact-effect view without the diff body."""
        return {
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "task_id": self.task_id,
            "repo_path": self.repo_path,
            "paths": list(self.paths),
            "command": self.command,
            "server": self.server,
            "side_effect": self.side_effect,
            "issue_text": self.issue_text,
            "summary": self.summary,
            "timeout_s": self.timeout_s,
            "effect": self.effect_summary(),
        }

    def effect_summary(self) -> str:
        """Return the exact path, command, server, or diff being approved."""
        details: List[str] = []
        if self.command:
            details.append(f"command: {self.command}")
        if self.server:
            details.append(f"MCP server: {self.server}")
        if self.paths:
            details.append("paths: " + ", ".join(self.paths))
        if self.side_effect:
            details.append(f"side effect: {self.side_effect}")
        if not details and self.diff:
            details.append("side effect: apply the exact proposed diff")
        if not details:
            details.append("side effect: approve the pending request exactly as shown")
        return " · ".join(details)


@dataclass(frozen=True)
class ApprovalGrant:
    """One session-scoped approval grant retained by an interactive shell."""

    scope: str
    effect_signature: Tuple[str, ...]
    paths: Tuple[str, ...] = ()
    command: str = ""
    server: str = ""
    request_id: str = ""
    fingerprint: str = ""

    def matches(self, request: ApprovalRequestView) -> bool:
        """Return whether this grant covers a later request."""
        if self.scope == "session":
            return self.effect_signature == request.effect_signature("session")
        repo = request.repo_path.replace("\\", "/").rstrip("/").lower()
        if self.scope in ("path", "command") and repo not in self.effect_signature:
            return False
        if self.scope == "path":
            granted = {path.replace("\\", "/").lstrip("/") for path in self.paths}
            requested = {path.replace("\\", "/").lstrip("/") for path in request.paths}
            return bool(requested) and requested.issubset(granted)
        if self.scope == "command":
            return _command_matches(request.command, self.command)
        return False


@dataclass
class ApprovalPolicy:
    """Session-only grants for once/session/path/command approval UX."""

    grants: List[ApprovalGrant] = field(default_factory=list)

    def reset(self) -> None:
        """Forget every session approval grant."""
        self.grants.clear()

    def matching(self, request: ApprovalRequestView) -> Optional[ApprovalGrant]:
        """Return the newest grant covering a request, if any."""
        for grant in reversed(self.grants):
            if grant.matches(request):
                return grant
        return None

    def record(
        self, request: ApprovalRequestView, scope: str = "once"
    ) -> Optional[ApprovalGrant]:
        """Record a non-once grant and return it."""
        normalized = normalize_approval_scope(scope)
        if normalized == "once":
            return None
        grant = ApprovalGrant(
            scope=normalized,
            effect_signature=request.effect_signature(normalized),
            paths=tuple(path.replace("\\", "/").lstrip("/") for path in request.paths),
            command=request.command,
            server=request.server,
            request_id=request.request_id,
            fingerprint=request.fingerprint,
        )
        self.grants.append(grant)
        return grant


@dataclass(frozen=True)
class ModeSpec:
    """A product mode with a bounded tool and permission profile."""

    name: str
    label: str
    summary: str
    strategy: str
    visible_tools: Tuple[str, ...]
    denied_side_effects: Tuple[str, ...] = ()
    approval: str = "allow"
    read_only: bool = True
    allow_network: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly mode profile."""
        return {
            "name": self.name,
            "label": self.label,
            "summary": self.summary,
            "strategy": self.strategy,
            "visible_tools": list(self.visible_tools),
            "denied_side_effects": list(self.denied_side_effects),
            "approval": self.approval,
            "permission": self.approval,
            "read_only": self.read_only,
            "allow_network": self.allow_network,
            "network": self.allow_network,
        }


MODE_NAMES: Tuple[str, ...] = ("plan", "build", "explore", "review", "debug", "ask")

#: The tools that can put BYTES on disk. This is the axis the approval gate
#: is derived from, and it is declared ONCE here so a new editing mode
#: cannot forget to be gated.
#:
#: `bash` is in the set because a mode allowed to run commands can write a
#: file through them; leaving it out would let a mode that shells out
#: escape the very gate this exists to apply. A mode that denies
#: `workspace_write` is not helped by this set - its own denial is the
#: stronger control and it is applied independently.
MODE_WRITE_TOOLS: frozenset = frozenset(
    {"edit", "write", "apply_patch", "bash", "process", "process_write_stdin"}
)

#: The environment variable a person uses to turn the gate OFF for a run.
#: Read by KEY PRESENCE and only in this direction - it can WEAKEN the gate
#: and never strengthen it - because an operator who types this has decided
#: something about their own machine, and no environment variable in a
#: repository should be able to put a gate back on.
APPROVAL_OPT_OUT_ENV = "NEO_AGENT_APPROVAL"


def mode_writes(spec: Any) -> bool:
    """Whether this mode profile can put SOURCE bytes on disk.

    A NECESSARY condition, derived from the tool list rather than declared:
    a mode whose visible tools include none of :data:`MODE_WRITE_TOOLS` has
    nothing to approve, so asking would be a prompt with no diff behind it -
    and a prompt a person learns to dismiss without reading is worse than no
    prompt.

    **A declared ``workspace_write`` denial wins over the tool list.** A
    mode that shells out but explicitly denies workspace writes (the `debug`
    profile runs tests, and a test run writes caches) is already controlled
    by a STRONGER mechanism than a prompt, and gating it would mean asking a
    person to approve every ``pytest`` invocation. The denial is applied
    independently of this function; this only declines to double it.
    """
    denied = set(getattr(spec, "denied_side_effects", ()) or ())
    if "workspace_write" in denied:
        return False
    tools = set(getattr(spec, "visible_tools", ()) or ())
    return bool(tools & MODE_WRITE_TOOLS)


def approval_opt_out(config: Optional[Mapping[str, Any]] = None) -> str:
    """Return the opt-out word, or ``""``.

    Two sources, both read by key presence: the ``agent_approval`` /
    ``mode_approval`` config key, then :data:`APPROVAL_OPT_OUT_ENV`. An
    unrecognised value returns ``""`` rather than defaulting to the opt-out:
    a typo must not silently disable a trust gate.
    """
    values = dict(config or {})
    for key in ("mode_approval", "agent_approval"):
        if key in values:
            word = str(values.get(key) or "").strip().casefold()
            if word in {"allow", "never", "off", "no"}:
                return word
            if word in {"require", "ask", "always", "on", "yes"}:
                return ""
    import os

    word = str(os.environ.get(APPROVAL_OPT_OUT_ENV, "") or "").strip().casefold()
    return word if word in {"allow", "never", "off", "no"} else ""


def resolve_agent_approval(
    spec: Any, config: Optional[Mapping[str, Any]] = None
) -> Tuple[str, str]:
    """The ``agent_approval`` value for a run, and WHY it is that value.

    Returns ``(value, reason)`` because a receipt that says only
    ``"require"`` cannot answer "why was I asked?", and a person who is
    asked a question they did not expect needs the answer in the same
    breath.

    **The gate is derived from CAPABILITY, not from a declared word.** The
    previous rule was ``"require" if profile.approval == "ask" else
    "allow"``, which means a mode that can edit and forgets ``approval=``
    silently gets NO pre-apply diff - a trust property resting on a data
    field nobody is forced to set. Deriving it from the tool list makes the
    omission impossible, and the reason string names the capability so the
    derivation is auditable rather than magic.
    """
    opted_out = approval_opt_out(config)
    if opted_out:
        return (
            "allow",
            f"explicitly opted out via {APPROVAL_OPT_OUT_ENV}={opted_out} or a "
            "config key; the pre-apply diff is NOT shown for this run",
        )
    if mode_writes(spec):
        if str(getattr(spec, "approval", "")) == "deny":
            return (
                "allow",
                "the mode denies every write tool, so there is nothing to approve; "
                "the denial is the control here, not a prompt",
            )
        return (
            "require",
            f"this mode can write ({getattr(spec, 'name', 'mode')}), so its changes "
            "are shown before they are applied",
        )
    return (
        "allow",
        "this mode has no write tool, so a pre-apply diff would be a prompt with "
        "nothing behind it",
    )


_MODE_SPECS: Dict[str, ModeSpec] = {
    "plan": ModeSpec(
        "plan",
        "Plan",
        "Decompose work without changing the repository",
        "planning",
        ("read", "glob", "grep", "memory", "todo", "plan", "ask", "finish", "cancel"),
        approval="allow",
    ),
    "build": ModeSpec(
        "build",
        "Build",
        "Implement and verify a feature on the live repository",
        "daily",
        (
            "read",
            "glob",
            "grep",
            "edit",
            "write",
            "bash",
            "apply_patch",
            "mcp",
            "git_status",
            "git_diff",
            "memory",
            "verify",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        approval="ask",
        read_only=False,
    ),
    "explore": ModeSpec(
        "explore",
        "Explore",
        "Investigate code and bounded reference material read-only",
        "research",
        ("read", "glob", "grep", "memory", "fetch", "todo", "ask", "finish", "cancel"),
        approval="allow",
        allow_network=True,
    ),
    "review": ModeSpec(
        "review",
        "Review",
        "Inspect a change and report evidence without editing",
        "question",
        (
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "memory",
            "todo",
            "ask",
            "finish",
            "cancel",
        ),
        denied_side_effects=("workspace_write", "process", "external"),
        approval="deny",
    ),
    "debug": ModeSpec(
        "debug",
        "Debug",
        "Run diagnostics and tests without changing source files",
        "daily",
        (
            "read",
            "glob",
            "grep",
            "bash",
            "memory",
            "verify",
            "todo",
            "ask",
            "finish",
            "cancel",
        ),
        denied_side_effects=("workspace_write", "network", "external"),
        approval="allow",
        read_only=False,
    ),
    "ask": ModeSpec(
        "ask",
        "Ask",
        "Answer a repository question with read-only context",
        "question",
        ("read", "glob", "grep", "memory", "todo", "ask", "finish", "cancel"),
        approval="allow",
    ),
}
_MODE_ALIASES = {
    "question": "ask",
    "research": "explore",
    "fix": "build",
    "agent": "build",
    "agent_task": "build",
    "planning": "plan",
    "implementation": "build",
    "implementer": "build",
    "reviewer": "review",
    "debugger": "debug",
    "researcher": "explore",
}


def mode_specs() -> Tuple[ModeSpec, ...]:
    """Return the immutable six-mode registry."""
    return tuple(_MODE_SPECS[name] for name in MODE_NAMES)


def mode_spec(name: str) -> Optional[ModeSpec]:
    """Resolve a mode name or compatibility alias to its profile."""
    key = str(name or "").strip().lower()
    key = _MODE_ALIASES.get(key, key)
    return _MODE_SPECS.get(key)


def normalize_mode(name: str, default: str = "ask") -> str:
    """Normalize a mode alias, falling back to a valid default."""
    selected = mode_spec(name)
    if selected is not None:
        return selected.name
    fallback = mode_spec(default)
    return fallback.name if fallback is not None else "ask"


def mode_config(name: str) -> Dict[str, Any]:
    """Return additive config keys for a mode profile."""
    selected = mode_spec(name) or mode_spec("ask")
    assert selected is not None
    return {
        "agent_mode": selected.name,
        "agent_strategy": selected.strategy,
        "agent_visible_tools": list(selected.visible_tools),
        "agent_denied_side_effects": list(selected.denied_side_effects),
        "agent_approval": selected.approval,
        "agent_permission": selected.approval,
        "agent_read_only": selected.read_only,
        "agent_allow_network": selected.allow_network,
        "agent_network": selected.allow_network,
    }


def mode_tool_visible(name: str, tool: str) -> bool:
    """Return whether a mode exposes a tool to the agent."""
    selected = mode_spec(name) or mode_spec("ask")
    if selected is None:
        return False
    tool_name = str(tool or "").strip().lower()
    if tool_name in selected.visible_tools:
        return True
    aliases = {
        "shell": "bash",
        "process": "bash",
        "test": "verify",
        "git": "git_status",
        "finish": "done",
    }
    return aliases.get(tool_name, tool_name) in selected.visible_tools


def mode_permission(name: str, tool: str) -> str:
    """Return the default permission action for a mode/tool pair."""
    selected = mode_spec(name) or mode_spec("ask")
    if selected is None:
        return "deny"
    if not mode_tool_visible(selected.name, tool):
        return "deny"
    if str(tool or "").lower() in {
        "edit",
        "write",
        "bash",
        "shell",
        "process",
        "apply_patch",
    }:
        return selected.approval
    return "allow"


# ---------------------------------------------------------------------------
# The SUBCOMMAND registry (VEX-CS-01)
# ---------------------------------------------------------------------------
#
# A REPL filters; an agent takes verbs. `/plugins`, `/mcp` and `/skills` were
# three browsers with no verbs at all: a user could LOOK at a plugin, a
# connector and a skill, and could not install one. The pattern that already
# worked for `/diff` is lifted here, and lifted as a REGISTRY feature rather
# than a `/plugin` feature: every verb's policy, argument shape, presentation
# and permission requirement is DATA on a `SubcommandSpec`, so the argument
# hint, the availability gate and the dispatcher all read ONE table.
#
# The shape the reference established, kept exactly:
#
#     /plugin                          -> opens the interactive menu
#     /plugin list                     -> prints the table, RETURNS
#     /plugin install <ref>            -> installs, returns a receipt
#
# `default=True` is the no-argument case (the menu). Every other verb RETURNS
# a result and never opens a dialog, which is why the subcommand result
# vocabulary is narrower than the command one.


@dataclass(frozen=True)
class SubcommandSpec:
    """One verb under a command, with its own policy and presentation.

    The per-verb axis the command registry did not have. A command that both
    reads (``/plugin list``) and writes (``/plugin install``) cannot express
    that difference with one ``result_presentation`` and one
    ``required_permissions`` tuple, so it declares the difference here.
    """

    name: str
    summary: str
    argument_policy: str = "optional"
    argument_hint: str = ""
    idle_policy: str = "allow"
    in_flight_policy: str = "allow"
    required_permissions: Tuple[str, ...] = ()
    result_presentation: str = "inline"
    failure_recovery: Tuple[str, ...] = ("retry", "inspect-trace")
    #: The no-argument case. Exactly one per command, and the ONLY verb whose
    #: result may be a browser or a modal.
    default: bool = False
    #: Does this verb change state? The in-flight gate refuses these and
    #: permits the read-only ones, which is the per-verb generalization of
    #: the gate `/diff` grew in VEX-PF-05.
    mutating: bool = False
    #: Other spellings that reach this same verb. Declared rather than
    #: normalized at the call site so the registry is the one place that
    #: knows ``apply`` and ``commit`` are one action.
    aliases: Tuple[str, ...] = ()
    #: A first word the HISTORICAL engine already owned, before this registry
    #: existed. Declared in the same table so the in-flight gate reads ONE
    #: list: a mutating word the gate does not know about is a word the gate
    #: permits. ``/diff undo`` and a bare ``/diff all`` are the two that
    #: exist - neither has a verb this round added.
    legacy: bool = False
    #: Does an UNRECOGNISED first word reach this verb as its argument?
    #:
    #: True only where the command historically took a free-text first
    #: argument that is not a verb: ``/mcp <label>`` resolved a connector and
    #: ``/skills <filter>`` filtered the roster. Those forms are load-bearing
    #: and rule 8 (backward compatibility) outranks this round's tidiness, so
    #: they are DECLARED here rather than deleted.
    #:
    #: False for ``/plugin``, where the historical behaviour was to print a
    #: usage line for any argument - so ``/plugin instal`` being a usage error
    #: costs nothing and is what the brief asks for. This flag is the whole
    #: difference between the two, and it is data rather than a special case
    #: in the resolver.
    free_text_argument: bool = False
    #: Is this verb a WORD a person types, or a label for what a bare call
    #: does? ``/undo`` STAGES on a bare call (AGT-09) and the word "stage" was
    #: never something the user typed, and the hint is pinned byte-for-byte by
    #: that round's suite. The row still has to exist - it is the default, and
    #: it carries the permissions and the presentation of the bare call - but
    #: declaring it untyped is what keeps "the hint names every verb the
    #: dispatcher answers for" an assertion rather than an aspiration.
    typed: bool = True

    def __post_init__(self) -> None:
        """Reject a malformed verb before any surface can consume it."""
        if self.argument_policy not in ARGUMENT_POLICIES:
            raise ValueError(f"unsupported argument policy: {self.argument_policy}")
        if self.idle_policy not in BEHAVIOR_POLICIES:
            raise ValueError(f"unsupported idle behavior: {self.idle_policy}")
        if self.in_flight_policy not in BEHAVIOR_POLICIES:
            raise ValueError(f"unsupported in-flight behavior: {self.in_flight_policy}")
        if self.result_presentation not in SUBRESULT_PRESENTATIONS:
            raise ValueError(
                "unsupported subcommand result presentation: "
                f"{self.result_presentation} (a verb returns; only the "
                "no-argument case may open a dialog)"
            )
        unknown_permissions = set(self.required_permissions) - PERMISSION_SCOPES
        if unknown_permissions:
            raise ValueError(
                "unknown subcommand permissions: "
                + ", ".join(sorted(unknown_permissions))
            )
        unknown_recovery = set(self.failure_recovery) - RECOVERY_ACTIONS
        if unknown_recovery:
            raise ValueError(
                "unknown subcommand recovery actions: "
                + ", ".join(sorted(unknown_recovery))
            )
        if self.name in self.aliases:
            raise ValueError(f"subcommand {self.name} lists itself as an alias")

    @property
    def verb(self) -> str:
        """Return the verb as it is typed (no leading slash)."""
        return self.name

    def usage(self, command: str) -> str:
        """Return one copyable usage line for this verb."""
        arguments = f" {self.argument_hint}" if self.argument_hint else ""
        return f"usage: {command} {self.name}{arguments}"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly verb specification."""
        return {
            "name": self.name,
            "summary": self.summary,
            "argument_policy": self.argument_policy,
            "argument_hint": self.argument_hint,
            "idle_policy": self.idle_policy,
            "in_flight_policy": self.in_flight_policy,
            "required_permissions": list(self.required_permissions),
            "result_presentation": self.result_presentation,
            "failure_recovery": list(self.failure_recovery),
            "default": self.default,
            "mutating": self.mutating,
            "aliases": list(self.aliases),
            "legacy": self.legacy,
            "free_text_argument": self.free_text_argument,
            "typed": self.typed,
        }


def _verb(
    name: str,
    summary: str,
    *,
    argument_policy: str = "optional",
    argument_hint: str = "",
    in_flight_policy: str = "allow",
    required_permissions: Tuple[str, ...] = (),
    result_presentation: str = "inline",
    failure_recovery: Tuple[str, ...] = ("retry", "inspect-trace"),
    default: bool = False,
    mutating: bool = False,
    aliases: Tuple[str, ...] = (),
    legacy: bool = False,
    free_text_argument: bool = False,
    typed: bool = True,
) -> SubcommandSpec:
    """Build one verb; the terse spelling keeps the table readable."""
    return SubcommandSpec(
        name=name,
        summary=summary,
        argument_policy=argument_policy,
        argument_hint=argument_hint,
        in_flight_policy=in_flight_policy,
        required_permissions=required_permissions,
        result_presentation=result_presentation,
        failure_recovery=failure_recovery,
        default=default,
        mutating=mutating,
        aliases=aliases,
        legacy=legacy,
        free_text_argument=free_text_argument,
        typed=typed,
    )


#: Every verb, keyed by its command. An EMPTY tuple means "this command has no
#: verbs", which is the majority and is the correct answer for `/help` and
#: `/cost` — a verb set they do not have would be a lie in the hint.
#:
#: The read-only verb of every mutating command is declared FIRST so the
#: in-flight refusal can name it as the alternative without a second search.
SUBCOMMANDS: Mapping[str, Tuple[SubcommandSpec, ...]] = {
    # /plugins is a browser today. The verbs route to `cli.plugins`, which is
    # the implementation `/plugins` and `neo plugin` already share.
    "/plugins": (
        _verb(
            "list",
            "list installed plugins and their enablement",
            default=True,
            result_presentation="inline",
            required_permissions=("extension:read",),
            failure_recovery=("retry", "edit-input"),
        ),
        _verb(
            "inspect",
            "print one plugin's manifest, skills, commands and tools",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read",),
        ),
        _verb(
            "install",
            "install a plugin from a local path or a git URL",
            argument_policy="required",
            argument_hint="<ref>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "enable",
            "enable an installed plugin",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "disable",
            "disable an installed plugin without removing it",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "remove",
            "uninstall a plugin and everything it installed",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "reload",
            "rescan the plugin root and report what changed",
            required_permissions=("extension:read",),
            mutating=True,
            failure_recovery=("retry", "inspect-trace", "return-safe-state"),
        ),
        _verb(
            "marketplace",
            "add a marketplace source and list the ones configured",
            argument_policy="optional",
            argument_hint="add <src>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
    ),
    "/mcp": (
        _verb(
            "list",
            "list configured MCP connectors and their sources",
            default=True,
            # `/mcp <label>` has always listed one connector's tools, and
            # `test_cli_slash2.py` pins it. The free-text first word reaches
            # THIS verb rather than becoming a usage error.
            free_text_argument=True,
            required_permissions=("external:read",),
            failure_recovery=("retry", "edit-input"),
        ),
        _verb(
            "add",
            "declare a new MCP connector",
            argument_policy="required",
            argument_hint="<label> <command...>",
            required_permissions=("external:read", "settings:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "remove",
            "remove a declared MCP connector",
            argument_policy="required",
            argument_hint="<label>",
            required_permissions=("external:read", "settings:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "health",
            "probe every configured connector and report each result",
            required_permissions=("external:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
        _verb(
            "call",
            "call one tool on a configured connector",
            argument_policy="required",
            argument_hint="<label> <tool> [args-json]",
            required_permissions=("external:read",),
            failure_recovery=("retry", "edit-input", "inspect-trace"),
        ),
        _verb(
            "pin",
            "pin one connector tool to an approved digest",
            argument_policy="required",
            argument_hint="<label> <tool> <digest>",
            required_permissions=("external:read", "settings:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "reconnect",
            "re-probe a connector and report its live state",
            argument_policy="required",
            argument_hint="<label>",
            required_permissions=("external:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
        _verb(
            "enable",
            "re-enable a connector whose entry is disabled",
            argument_policy="required",
            argument_hint="<label>",
            required_permissions=("external:read", "settings:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "disable",
            "disable a connector without removing its declaration",
            argument_policy="required",
            argument_hint="<label>",
            required_permissions=("external:read", "settings:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
    ),
    "/skills": (
        _verb(
            "list",
            "list project, global and plugin skills with their origins",
            default=True,
            argument_hint="[filter]",
            # `/skills <filter>` has always filtered the roster, and
            # `test_cli_slash2.py` pins both the match and the no-match line.
            free_text_argument=True,
            required_permissions=("extension:read",),
            failure_recovery=("retry", "edit-input"),
        ),
        _verb(
            "inspect",
            "print one skill's declaration and body",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read",),
        ),
        _verb(
            "enable",
            "enable a disabled skill",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "disable",
            "disable a skill without deleting its body",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "create",
            "write a new skill scaffold under the project skills root",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("extension:read", "filesystem:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
    ),
    # VEX-CS-01 item 7: the seven worktree capabilities were written, tested
    # and reachable ONLY from argparse. These verbs are the REPL door onto the
    # same `worktree_*` functions - a routing edit, not a re-implementation.
    "/worktree": (
        _verb(
            "list",
            "list managed worktrees reconciled against disk",
            default=True,
            required_permissions=("workspace:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
        _verb(
            "create",
            "create a detached worktree from the current HEAD",
            argument_policy="required",
            argument_hint="<name> [--base <commit>]",
            required_permissions=("workspace:read", "workspace:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "remove",
            "remove a managed worktree, refusing a dirty one",
            argument_policy="required",
            argument_hint="<name> [--force]",
            required_permissions=("workspace:read", "workspace:write"),
            mutating=True,
            failure_recovery=("retry", "edit-input", "return-safe-state"),
        ),
        _verb(
            "checkout",
            "print the absolute path of a managed worktree",
            argument_policy="required",
            argument_hint="<name>",
            required_permissions=("workspace:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
    ),
    # Terminals 09 and 10 own the behaviour of the last two; the rows and the
    # registry shape are declared here and the handlers are handed off. An
    # honest refusal that names the owner beats a verb that silently does
    # nothing, and the parity test pins that a row with no handler says so.
    "/hooks": (
        _verb(
            "list",
            "list configured user hooks and the fail policy in force",
            default=True,
            required_permissions=("settings:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
    ),
    "/migrate": (
        _verb(
            "plan",
            "report the pending migrations without writing anything",
            default=True,
            required_permissions=("settings:read",),
            failure_recovery=("retry", "inspect-trace"),
        ),
    ),
    "/support-bundle": (
        _verb(
            "create",
            "write one redacted diagnostic bundle",
            default=True,
            required_permissions=("settings:read", "session:read"),
            mutating=True,
            failure_recovery=("retry", "inspect-trace", "return-safe-state"),
        ),
    ),
}


def subcommand_specs(name: str) -> Tuple[SubcommandSpec, ...]:
    """Return the declared verbs for one command, in hint order.

    An empty tuple is the correct answer for a command with no actions, so
    callers must treat "no verbs" as normal rather than as missing data.

    For ``/diff`` and ``/undo`` the verbs are READ from the module that
    dispatches them (``cli.review`` / ``cli.fileview``) rather than restated
    here. That is the whole point of the generalization: a gate that lists
    words the dispatcher does not speak refuses a command nobody can run and
    permits one everybody can, and the /diff row of this registry is exactly
    the shape that was hand-written before.
    """
    spec = command_spec(name)
    if spec is None:
        return ()
    if spec.name in _VERB_SOURCE_COMMANDS:
        return _verbs_from_dispatcher(spec.name)
    return SUBCOMMANDS.get(spec.name, ())


#: Commands whose verb vocabulary is owned by ANOTHER module and must be read
#: from it. Keyed by command name to the accessor that returns the vocabulary.
#:
#: ``/diff``: ``cli.review.DIFF_REVIEW_VERBS`` is the closed vocabulary the
#: review dispatcher answers for. ``/undo``: ``cli.fileview.UNDO_VERBS`` is the
#: staged-undo dispatcher's own set. Both are imported lazily inside the
#: accessor because ``cli.review`` imports ``cli.fileview``, which imports this
#: module - a module-level import here would be a cycle.
_VERB_SOURCE_COMMANDS: Tuple[str, ...] = ("/diff", "/undo")

#: ``/diff`` also has first words the HISTORICAL engine owns: ``undo`` restores
#: a pre-image and a bare ``all`` reverts everything, neither with a verb this
#: registry added. They are in the mutating set for the same reason the review
#: gate listed them, and they are declared HERE rather than in a second list so
#: a gate cannot disagree with a dispatcher about the same word.
_DIFF_LEGACY_MUTATING: Tuple[str, ...] = ("undo", "all")

_VERB_CACHE: Dict[str, Tuple[SubcommandSpec, ...]] = {}


def _verbs_from_dispatcher(command: str) -> Tuple[SubcommandSpec, ...]:
    """Return one command's verbs, read from the module that dispatches them.

    Cached, because the vocabulary is a module constant on the far side and
    the resolution walks a tuple on every availability check. A read that
    raised would be the wrong answer, so the fallback is the same literal the
    pre-registry code used rather than an empty set: an empty set would make
    every ``/diff`` verb read as read-only.
    """
    cached = _VERB_CACHE.get(command)
    if cached is not None:
        return cached
    if command == "/diff":
        verbs = _diff_verbs()
    elif command == "/undo":
        verbs = _undo_verbs()
    else:  # pragma: no cover - guarded by _VERB_SOURCE_COMMANDS
        verbs = ()
    _VERB_CACHE[command] = verbs
    return verbs


def _diff_verbs() -> Tuple[SubcommandSpec, ...]:
    """Build the ``/diff`` verb rows from ``cli.review``'s own vocabulary.

    The review verbs come from ``cli.review`` (the module that dispatches
    them). The two HISTORICAL first words the historical engine owns -
    ``undo`` restores a pre-image and a bare ``all`` reverts everything -
    are appended here with ``legacy=True``, so the in-flight gate reads ONE
    list instead of the review set plus a private set literal beside it. That
    private set is exactly the shape that let the two drift.
    """
    names: Tuple[str, ...] = ("show", "accept", "reject", "revert")
    try:
        from cli import review as _review

        names = tuple(_review.diff_review_verbs()) or names
    except Exception:
        pass
    out: List[SubcommandSpec] = []
    for verb in names:
        out.append(
            _verb(
                verb,
                f"/diff {verb}",
                argument_hint="<file>" if verb == "show" else "",
                required_permissions=(
                    ("journal:read", "workspace:read")
                    if verb == "show"
                    else ("journal:read", "workspace:read", "workspace:write")
                ),
                result_presentation="diff" if verb == "show" else "receipt",
                mutating=verb != "show",
                default=verb == "show",
                # `/diff <file>` has always been the per-file diff view, so an
                # unrecognised first word reaches the READ-ONLY verb rather
                # than becoming a usage error - and, because that verb is not
                # mutating, `/diff <file>` stays available mid-run exactly as
                # it was. Declared here rather than special-cased in the
                # resolver so the two commands that need it say so.
                free_text_argument=verb == "show",
                failure_recovery=("retry", "inspect-trace", "return-safe-state"),
            )
        )
    for legacy_word in _DIFF_LEGACY_MUTATING:
        out.append(
            _verb(
                legacy_word,
                f"/diff {legacy_word} (historical engine)",
                argument_hint="[file|all]",
                required_permissions=(
                    "journal:read",
                    "workspace:read",
                    "workspace:write",
                ),
                result_presentation="receipt",
                mutating=True,
                legacy=True,
                failure_recovery=("retry", "inspect-trace", "return-safe-state"),
            )
        )
    return tuple(out)


def _undo_verbs() -> Tuple[SubcommandSpec, ...]:
    """Build the ``/undo`` verb rows from ``cli.fileview``'s own vocabulary.

    ``/undo`` STAGES on a bare call (that is the AGT-09 surface), so the
    default verb is the staging one rather than a named word. It is mutating
    in the sense that matters here - it holds a range that a later ``commit``
    writes to disk - and the whole command already refuses in flight, so the
    gate is unchanged.
    """
    try:
        from cli import fileview as _fv

        names = tuple(_fv.UNDO_VERBS) or ("commit", "discard", "plan", "force")
    except Exception:
        names = ("commit", "discard", "plan", "force")
    out: List[SubcommandSpec] = [
        _verb(
            "stage",
            "stage the newest turn's changes for revert",
            argument_hint="[code|task|all|<file>]",
            required_permissions=("workspace:write",),
            result_presentation="receipt",
            mutating=True,
            default=True,
            # The word "stage" is a LABEL for what a bare `/undo` does, not
            # a spelling a person types: AGT-09 made the bare call stage and
            # that round's suite pins the hint byte-for-byte. The row still
            # has to exist - it is the default and it carries the bare call's
            # permissions - but it is not advertised as a word.
            typed=False,
            # `/undo <file>` has always reached the historical per-file revert
            # when the staged range is empty, and `test_agt_09` pins that
            # fall-through. An unrecognised first word therefore reaches THIS
            # verb, which keeps the historical path reachable.
            free_text_argument=True,
            failure_recovery=("retry", "undo", "return-safe-state"),
        )
    ]
    for verb in names:
        # `cli.fileview.UNDO_VERBS` names SIX words for what a person sees as
        # four actions: `apply` is `commit` and `cancel` is `discard`. Both
        # spellings are accepted (the dispatcher answers them), and the hint
        # this round did not change names only the four. Declaring the two
        # untyped is the honest reading - they are ACCEPTED, not ADVERTISED -
        # and it keeps "the hint names every verb a person can type" true
        # without editing a hint three other suites pin byte-for-byte.
        secondary = verb in {"apply", "cancel"}
        out.append(
            _verb(
                verb,
                f"/undo {verb}",
                argument_hint="<file>" if verb in {"commit", "apply"} else "",
                required_permissions=("workspace:write",),
                result_presentation="receipt" if verb != "plan" else "inline",
                mutating=verb != "plan",
                typed=not secondary,
                # NO alias table: `UNDO_VERBS` already carries both
                # spellings, and restating the pairing here would be a second
                # source for a fact the dispatcher already owns.
                failure_recovery=("retry", "undo", "return-safe-state"),
            )
        )
    return tuple(out)


def subcommand_names(name: str) -> Tuple[str, ...]:
    """Return one command's TYPED verb names, for a hint or a menu.

    A row with ``typed=False`` describes what a bare call does rather than a
    word a person types, so it is not part of the advertised vocabulary. It
    stays in :func:`subcommand_specs` because the dispatcher resolves through
    it - a menu that listed a word the resolver rejects, or a resolver that
    answered for a word the menu omits, would be the same lie in two
    directions.
    """
    return tuple(verb.name for verb in subcommand_specs(name) if verb.typed)


def subcommand_verb(name: str, verb: str) -> Optional[SubcommandSpec]:
    """Resolve one command plus one typed verb to its specification.

    Accepts an alias as readily as a canonical name, because the vocabulary
    the user types and the vocabulary the dispatcher implements are allowed to
    differ (``apply`` and ``commit`` are one action) and the registry is the
    one place that knows.
    """
    wanted = str(verb or "").strip().lower().lstrip("/")
    if not wanted:
        return None
    for candidate in subcommand_specs(name):
        if wanted == candidate.name or wanted in candidate.aliases:
            return candidate
    return None


#: Commands whose ``argument_hint`` IS the verb list, and is therefore
#: RENDERED from the registry rather than hand-written. `/diff` and `/undo`
#: are deliberately absent: their hints carry argument words the verb list
#: cannot express (`[file|all]`, `code|task|all|<file>`), and both are pinned
#: byte-for-byte by other terminals' suites. The verbs are still read from the
#: registry for the GATE; only the rendered hint is left alone.
VERB_HINT_COMMANDS: Tuple[str, ...] = (
    "/plugins",
    "/mcp",
    "/skills",
    "/worktree",
    "/hooks",
    "/migrate",
    "/support-bundle",
)


def _subcommand_argument_hint(
    spec: CommandSpec, verbs: Tuple[SubcommandSpec, ...]
) -> str:
    """Render one command's declared verbs as the reference does.

    The exact shape the ``/diff`` row established: ``[a|b|c]``, in the order
    the registry declares, with every verb named so the hint teaches the whole
    vocabulary rather than the first two words of it.
    """
    if spec.name not in VERB_HINT_COMMANDS:
        return spec.argument_hint
    return "[" + "|".join(verb.name for verb in verbs if verb.typed) + "]"


COMMAND_SPECS: Tuple[CommandSpec, ...] = (
    CommandSpec(
        "/help",
        "show the command guide",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[filter]",
        required_permissions=("session:read",),
        result_presentation="browser",
    ),
    CommandSpec(
        "/status",
        "show the live or last task",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id]",
        required_permissions=("journal:read", "session:read"),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "resume"),
    ),
    CommandSpec(
        "/mode",
        "select a Plan, Build, Explore, Review, Debug, or Ask mode",
        (),
        "optional",
        "allow",
        "refuse",
        "prefill",
        argument_hint="[plan|build|explore|review|debug|ask]",
        required_permissions=("session:write",),
    ),
    CommandSpec(
        "/plan",
        "preview or start a plan",
        (),
        "optional",
        "allow",
        "queue",
        "prefill",
        argument_hint="[request]",
        required_permissions=("agent:run",),
        result_presentation="modal",
        failure_recovery=("edit-input", "cancel-command", "return-safe-state"),
    ),
    CommandSpec(
        "/build",
        "run an explicit Build request",
        (),
        "required",
        "allow",
        "queue",
        "prefill",
        argument_hint="<request>",
        required_permissions=("agent:run", "workspace:write"),
        result_presentation="card",
        failure_recovery=("retry", "edit-input", "inspect-trace", "undo"),
    ),
    CommandSpec(
        "/ask",
        "run an explicit read-only Ask request",
        (),
        "required",
        "allow",
        "queue",
        "prefill",
        argument_hint="<question>",
        required_permissions=("agent:run", "workspace:read"),
        result_presentation="card",
        failure_recovery=("retry", "edit-input", "inspect-trace"),
    ),
    CommandSpec(
        "/review",
        "review the current diff or start a review request",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        True,
        argument_hint="[request]",
        required_permissions=("journal:read",),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/diff",
        "review what the run did to your files, then accept, reject or revert it",
        ("/changes",),
        "optional",
        "allow",
        "allow",
        "run",
        # VEX-PF-05. The read-only verbs are available mid-run; the
        # MUTATING ones are refused by `_in_flight_argument_conflict` below,
        # which is why this policy stays `allow` rather than `refuse` - a
        # `/diff` that cannot be read while a run is live is the one moment
        # a person most wants to read it. `undo` stays in the hint because
        # it is still the historical engine's verb and removing the word
        # would be a help string lying about what the command accepts.
        argument_hint="[show|accept|reject|revert|undo] [file|all]",
        required_permissions=("journal:read", "workspace:read"),
        result_presentation="diff",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/checkpoints",
        "browse durable run checkpoints",
        ("/checkpoint",),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id]",
        required_permissions=("journal:read", "session:read"),
        result_presentation="browser",
        failure_recovery=("retry", "resume", "inspect-trace"),
    ),
    CommandSpec(
        "/undo",
        "stage a revert of the last turn, or commit the staged one",
        ("/diff",),
        "optional",
        "allow",
        "refuse",
        "prefill",
        # AGT-09: a bare `/undo` STAGES (and a second one WIDENS the range);
        # a new prompt commits it. The verbs and the three granularities are
        # the whole surface, and a bare file path still reaches the
        # historical per-file revert.
        argument_hint=("[commit|discard|plan|force|code|task|all|<file>]"),
        required_permissions=("workspace:write",),
        result_presentation="diff",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/redo",
        "redo the last undone agent edit",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[file|all]",
        required_permissions=("workspace:write",),
        result_presentation="diff",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/sessions",
        "browse previous sessions",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[query]",
        required_permissions=("session:read",),
        result_presentation="browser",
        failure_recovery=("retry", "resume", "inspect-trace"),
    ),
    CommandSpec(
        "/resume",
        "continue a resumable task",
        (),
        "optional",
        "allow",
        "refuse",
        "prefill",
        argument_hint="[task-id]",
        required_permissions=("run:control", "journal:read"),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/share",
        "write a metadata-oriented shareable artifact",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[path]",
        required_permissions=("session:read", "filesystem:write"),
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/export",
        "export a redacted session artifact",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[path]",
        required_permissions=("session:read", "filesystem:write"),
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/fork",
        "fork this conversation at a turn boundary",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[turn-id]",
        required_permissions=("session:read", "session:write"),
        result_presentation="card",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/import",
        "import a session export as a new conversation",
        (),
        "required",
        "allow",
        "refuse",
        "run",
        argument_hint="<path> [--overwrite]",
        required_permissions=("session:write",),
        result_presentation="card",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/recover",
        "report or quarantine a corrupt session",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[session-id] [--fresh|--backup]",
        required_permissions=("session:read", "session:write"),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/cost",
        "show run and session spend",
        (),
        "none",
        "allow",
        "allow",
        "run",
        required_permissions=("journal:read",),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace"),
    ),
    CommandSpec(
        "/effort",
        "show or set how hard the model thinks",
        ("/thinking",),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[auto|low|medium|high|xhigh|max]",
        required_permissions=("session:write",),
        result_presentation="inline",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/context",
        "show the compiled session and repository context receipt",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[filter]",
        required_permissions=("session:read", "workspace:read"),
        result_presentation="browser",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/trace",
        "inspect journal-backed trace details",
        (),
        "optional",
        "allow",
        "allow",
        "prefill",
        argument_hint="[entry-number|diagnostic]",
        required_permissions=("journal:read",),
        result_presentation="browser",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/feed",
        "browse the journal-backed action feed",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[query]",
        required_permissions=("journal:read",),
        result_presentation="browser",
        failure_recovery=("retry", "inspect-trace"),
    ),
    CommandSpec(
        "/mcp",
        "browse MCP connectors and tools",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[server-label]",
        required_permissions=("external:read",),
        result_presentation="browser",
        failure_recovery=("retry", "edit-input", "inspect-trace"),
    ),
    CommandSpec(
        "/skills",
        "list project, global, and plugin skills",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[filter]",
        required_permissions=("extension:read",),
        result_presentation="browser",
        failure_recovery=("retry", "edit-input"),
    ),
    CommandSpec(
        "/plugins",
        "list, inspect and manage installed plugins",
        # VEX-CS-01: `/plugin` is the verb-shaped spelling this round makes
        # the product surface, and `/plugins` stays byte-identical as the
        # historical one. An alias rather than a rename, so every existing
        # dispatch, hint, pin and headless row keeps resolving.
        ("/plugin",),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[list|inspect|install|enable|disable|remove|reload|marketplace]",
        required_permissions=("extension:read",),
        result_presentation="browser",
        failure_recovery=("retry", "edit-input"),
    ),
    CommandSpec(
        "/init",
        "scaffold the project config",
        (),
        "none",
        "allow",
        "refuse",
        "run",
        required_permissions=("settings:write",),
        result_presentation="settings",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/connect",
        "add or replace a model provider credential",
        ("/auth",),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[provider] — or nothing to pick from the list",
        required_permissions=("settings:write",),
        result_presentation="modal",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/login",
        "configure a model provider",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[global|project|local]",
        required_permissions=("settings:write",),
        result_presentation="modal",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/logout",
        "remove the stored API key",
        (),
        "none",
        "allow",
        "refuse",
        "run",
        required_permissions=("settings:write",),
        result_presentation="settings",
        failure_recovery=("retry", "return-safe-state"),
    ),
    CommandSpec(
        "/model",
        "show or pin the model",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[model-name]",
        required_permissions=("settings:read", "settings:write"),
        result_presentation="settings",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/compact",
        "compact this conversation",
        (),
        "none",
        "allow",
        "refuse",
        "run",
        required_permissions=("session:write",),
        failure_recovery=("retry", "resume", "return-safe-state"),
    ),
    CommandSpec(
        "/steer",
        "steer the running task",
        (),
        "required",
        "refuse",
        "allow",
        "prefill",
        argument_hint="<instruction>",
        required_permissions=("run:control",),
        result_presentation="inline",
        failure_recovery=("edit-input", "cancel-command", "return-safe-state"),
        shortcuts=("ctrl+g", "ctrl+b"),
    ),
    CommandSpec(
        "/cancel",
        "cancel the current task safely",
        (),
        "none",
        "allow",
        "allow",
        "run",
        required_permissions=("run:control",),
        result_presentation="inline",
        failure_recovery=("resume", "inspect-trace", "return-safe-state"),
        shortcuts=("ctrl+x", "ctrl+c"),
    ),
    # VEX-CEILING-10 background runs. /detach leaves the run ALIVE and
    # stops projecting it, so it is only meaningful in flight; /attach is
    # the inverse and is useful at any time (it replays the journal of a
    # run whose process is gone).
    CommandSpec(
        "/detach",
        "leave the run running and stop watching it",
        (),
        "none",
        "refuse",
        "allow",
        "run",
        required_permissions=("run:control",),
        result_presentation="inline",
        failure_recovery=("resume", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/attach",
        "rebind to a detached run from its event journal",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id]",
        required_permissions=("journal:read", "session:read"),
        result_presentation="card",
        failure_recovery=("inspect-trace", "resume", "return-safe-state"),
    ),
    CommandSpec(
        "/watch",
        "follow a run's journal from the current session",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id]",
        required_permissions=("journal:read", "session:read"),
        result_presentation="card",
        failure_recovery=("inspect-trace", "resume", "return-safe-state"),
    ),
    CommandSpec(
        "/approve",
        "approve a pending request",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id] [once|session|path|command]",
        required_permissions=("approval:decide",),
        result_presentation="approval",
        failure_recovery=("edit-input", "cancel-command", "return-safe-state"),
    ),
    CommandSpec(
        "/reject",
        "reject a pending request",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[task-id]",
        required_permissions=("approval:decide",),
        result_presentation="approval",
        failure_recovery=("edit-input", "cancel-command", "return-safe-state"),
    ),
    CommandSpec(
        "/theme",
        "show or select a terminal theme",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[default|high-contrast|reduced-motion]",
        required_permissions=("settings:read", "settings:write"),
        result_presentation="settings",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/settings",
        "show effective settings and source tiers",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[status|path|filter]",
        required_permissions=("settings:read",),
        result_presentation="settings",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/open",
        "open a file or line in the configured editor",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[path[:line]]",
        required_permissions=("workspace:read",),
        result_presentation="inline",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/doctor",
        "run read-only health checks for the daily path",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[--json]",
        required_permissions=("settings:read", "session:read"),
        result_presentation="inline",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/repo",
        "switch the session repository and reload project settings",
        (),
        "required",
        "allow",
        "queue",
        "run",
        argument_hint="<path>",
        required_permissions=("settings:read", "settings:write"),
        result_presentation="inline",
        failure_recovery=("edit-input", "return-safe-state"),
    ),
    CommandSpec(
        "/quit",
        "quit after safely handling any active run",
        ("/exit",),
        "none",
        "allow",
        "allow",
        "run",
        required_permissions=("session:control",),
        result_presentation="exit",
        failure_recovery=("cancel-command", "return-safe-state"),
        shortcuts=("ctrl+q",),
    ),
    CommandSpec(
        "/files",
        "browse the repository file tree",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[query]",
        required_permissions=("workspace:read",),
        result_presentation="browser",
    ),
    CommandSpec(
        "/relevant",
        "list the files that matter for this run, with the reason for each",
        ("/related",),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[query]",
        required_permissions=("journal:read", "workspace:read"),
        result_presentation="browser",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
    ),
    CommandSpec(
        "/diagnostics",
        "show language-server diagnostics",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[filter]",
        required_permissions=("workspace:read",),
        result_presentation="browser",
    ),
    CommandSpec(
        "/copy-diff",
        "copy the current diff",
        ("/copy",),
        "none",
        "allow",
        "allow",
        "run",
        required_permissions=("journal:read",),
        result_presentation="diff",
        failure_recovery=("retry", "inspect-trace"),
    ),
    CommandSpec(
        "/history",
        "search input history",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[query]",
        required_permissions=("session:read",),
        result_presentation="browser",
    ),
    CommandSpec(
        "/quiet",
        "toggle live feed verbosity",
        (),
        "none",
        "allow",
        "allow",
        "run",
        required_permissions=("session:write",),
    ),
    CommandSpec(
        "/clear",
        "start a fresh conversation",
        (),
        "none",
        "allow",
        "refuse",
        "run",
        required_permissions=("session:write",),
        failure_recovery=("retry", "resume", "return-safe-state"),
    ),
    # VEX-CS-01 item 7. These four capabilities were written, tested and
    # reachable ONLY from argparse - `cmd_worktree`, `worktree_list`,
    # `worktree_new`, `worktree_remove`, `worktree_path`, `worktree_root`,
    # `worktree_run_config` and `WorktreeCommandError` - which is why the
    # command felt like a REPL: you could look at a plugin, a connector and a
    # skill, and you could not install one. A registry row is the DOOR; the
    # verb registry above is the ROOM. The argparse surface is untouched and
    # still dispatches to the same functions, so there is one implementation
    # of each verb and two doors onto it.
    CommandSpec(
        "/worktree",
        "list, create and remove isolated git worktrees",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[list|create|remove|checkout]",
        required_permissions=("workspace:read",),
        result_presentation="card",
        failure_recovery=("retry", "edit-input", "return-safe-state"),
        # VEX-CS-01 item 7. The implementation is in THIS file
        # (`worktree_list` / `worktree_new` / `worktree_remove` /
        # `worktree_path`), so a shell branch would be three lines. The REPL
        # HAS one (`cli/interactive.py::run_subcommand`); the TUI does not,
        # and `cli/tui.py` is not this round's file. Declaring the row
        # `flag-only` for now keeps the two shells' dispatch sets equal -
        # which `test_cli_terminal_parity.py` asserts with an AST read - and
        # tells an interactive caller to run `neo worktree`. The handoff to
        # drop this to `handled` is in `cli/AGENTS.md`, section "Handoff to
        # 01": add `/worktree` to the TUI's `/plugins` delegation tuple.
        interactive_dispatch="flag-only",
    ),
    CommandSpec(
        "/hooks",
        "inspect the user-hook layer and the fail policy in force",
        (),
        "optional",
        "allow",
        "allow",
        "run",
        argument_hint="[list]",
        required_permissions=("settings:read",),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
        # Terminal 09 owns the behaviour. Declared, not built.
        interactive_dispatch="flag-only",
    ),
    CommandSpec(
        "/migrate",
        "report the pending install migrations, and apply them on request",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[plan]",
        required_permissions=("settings:read",),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
        # Terminal 09 owns the behaviour. Declared, not built.
        interactive_dispatch="flag-only",
    ),
    CommandSpec(
        "/support-bundle",
        "write one redacted diagnostic bundle to attach to an issue",
        (),
        "optional",
        "allow",
        "refuse",
        "run",
        argument_hint="[create]",
        required_permissions=("settings:read", "session:read"),
        result_presentation="card",
        failure_recovery=("retry", "inspect-trace", "return-safe-state"),
        # Terminal 10 owns the behaviour. Declared, not built.
        interactive_dispatch="flag-only",
    ),
)


def _with_subcommands(
    specs: Tuple[CommandSpec, ...],
) -> Tuple[CommandSpec, ...]:
    """Project the subcommand registry onto every command's declared verbs.

    Run once at import, right after ``COMMAND_SPECS`` is built, so
    ``spec.subcommands`` / ``spec.mutating_verbs`` / ``argument_hint`` are
    DERIVED from :data:`SUBCOMMANDS` and cannot be restated in a spec row. A
    verb that exists in one table and not the other is not expressible, which
    is the whole reason the /diff gate could previously drift from the
    dispatcher that had to obey it.
    """
    out: List[CommandSpec] = []
    for spec in specs:
        verbs = (
            _verbs_from_dispatcher(spec.name)
            if spec.name in _VERB_SOURCE_COMMANDS
            else SUBCOMMANDS.get(spec.name, ())
        )
        if not verbs:
            out.append(spec)
            continue
        out.append(
            replace(
                spec,
                argument_hint=_subcommand_argument_hint(spec, verbs),
                subcommands=tuple(verb.name for verb in verbs),
                mutating_verbs=tuple(verb.name for verb in verbs if verb.mutating),
                opens_browser_without_argument=True,
            )
        )
    return tuple(out)


COMMAND_SPECS = _with_subcommands(COMMAND_SPECS)

#: Commands whose INTERACTIVE dispatch is missing on BOTH shells, so an
#: interactive caller gets the flag instead of falling through to the
#: dispatcher's "unknown command" path. The row is declared (it is in the
#: palette, in `/help` and in the headless table) and the capability is
#: reachable through the named CLI command, but neither shell has a branch
#: for it, and `cli/tui.py` is not this round's file to edit.
#:
#: Distinct from ``CommandSpec.interactive_dispatch``, which is a per-row
#: DECLARATION a future round flips to ``"handled"`` when it mounts the
#: branch. This table is the cross-terminal handoff RECORD, and it is what
#: :func:`interactive_dispatch_refusal` reads for a row nobody here
#: dispatches.
#:
#: ``/connect`` is a PRE-EXISTING orphan: VEX-PF-02 added the row, the
#: ``flag-only`` policy and `cli/auth.py::connect_interactive`, and both
#: handoffs in that round's notes ask for a dispatch branch that has not
#: landed. It is recorded here rather than exempted from the orphan gate,
#: because an exemption list is a place for the next orphan to hide.
HANDED_OFF_COMMANDS: Mapping[str, str] = {
    "/connect": "VEX-PF-02 - the auth flow is built; neither shell has a branch",
    "/hooks": "Terminal 09 (`neo hooks list|run`)",
    "/migrate": "Terminal 09 (`neo migrate`)",
    "/support-bundle": "Terminal 10 (`neo support-bundle`)",
}


def _validate_headless_tables() -> None:
    """Fail closed if the headless tables name unknown or malformed commands.

    Runs at import so a typo in the surface contract is a hard error rather
    than a headless command that silently behaves differently from the REPL.

    Four directions are checked, and BOTH directions of the flag-equivalent
    relationship are required. The original check only ran
    "has an equivalent => is flag-only", so a command marked ``flag-only``
    with NO equivalent sailed through and the headless adapter rendered
    ``"/effort has a dedicated flag in this surface: "`` — a sentence with
    nothing after the colon, naming a flag that does not exist. A
    one-directional gate is not a gate.
    """
    known = {spec.name for spec in COMMAND_SPECS}
    missing = known.difference(HEADLESS_COMMAND_POLICIES)
    if missing:
        raise ValueError(
            "headless policy missing for commands: " + ", ".join(sorted(missing))
        )
    for name, policy in HEADLESS_COMMAND_POLICIES.items():
        if name not in known:
            raise ValueError(f"headless policy for unknown command: {name}")
        if policy not in HEADLESS_POLICIES:
            raise ValueError(f"unsupported headless policy for {name}: {policy}")
    for name in HEADLESS_FLAG_EQUIVALENTS:
        if name not in known:
            raise ValueError(f"headless equivalent for unknown command: {name}")
        if HEADLESS_COMMAND_POLICIES.get(name) != "flag-only":
            raise ValueError(
                f"{name} has a flag equivalent but is not marked flag-only"
            )
    for name, policy in HEADLESS_COMMAND_POLICIES.items():
        if policy != "flag-only":
            continue
        if not str(HEADLESS_FLAG_EQUIVALENTS.get(name) or "").strip():
            raise ValueError(
                f"{name} is flag-only but names no headless equivalent; a "
                "refusal that cannot say what to run instead is not an "
                "actionable refusal"
            )
    absent_required = sorted(set(REQUIRED_COMMANDS).difference(known))
    if absent_required:
        raise ValueError(
            "required commands without a registry row: " + ", ".join(absent_required)
        )
    bad_surfaces = sorted(
        {
            str(spec.name)
            for spec in COMMAND_SPECS
            if spec.in_flight_policy not in BEHAVIOR_POLICIES
        }
    )
    if bad_surfaces:
        raise ValueError("unsupported in-flight policy: " + ", ".join(bad_surfaces))
    _validate_subcommand_registry()


def _validate_subcommand_registry() -> None:
    """Fail closed if the verb registry and the command registry disagree.

    Five directions, all of them load-bearing:

    1. every ``SUBCOMMANDS`` key is a real ``CommandSpec`` name, so a verb
       cannot exist for a command nobody can type;
    2. every spec that declares verbs has a registry row, so
       ``spec.subcommands`` cannot be empty while the table is not (the
       projection in :func:`_with_subcommands` makes that impossible, and
       this is the belt);
    3. each command has at most ONE ``default`` verb, because two
       no-argument cases means the bare call is ambiguous;
    4. each command has AT LEAST ONE verb whenever the registry row exists,
       because an empty row is a command that renders a hint teaching nothing;
    5. a MUTATING verb never carries a dialog-shaped presentation, because a
       verb that ends in a dialog is not a verb.

    Runs at import for the same reason the headless tables do: a typo in the
    surface contract should be a hard error, not a surface that quietly
    behaves differently from the one the user read in ``/help``.
    """
    known = {spec.name for spec in COMMAND_SPECS}
    orphan_verbs = sorted(
        set(SUBCOMMANDS)
        .difference(known)
        .union({name for name in _VERB_SOURCE_COMMANDS if name not in known})
    )
    if orphan_verbs:
        raise ValueError(
            "subcommands declared for unknown commands: " + ", ".join(orphan_verbs)
        )
    for name in sorted(set(SUBCOMMANDS).union(_VERB_SOURCE_COMMANDS)):
        verbs = (
            _verbs_from_dispatcher(name)
            if name in _VERB_SOURCE_COMMANDS
            else SUBCOMMANDS.get(name, ())
        )
        if not verbs:
            raise ValueError(f"{name} declares an empty subcommand list")
        defaults = [verb.name for verb in verbs if verb.default]
        if len(defaults) > 1:
            raise ValueError(
                f"{name} declares more than one default subcommand: "
                + ", ".join(defaults)
            )
        spellings: Dict[str, str] = {}
        for verb in verbs:
            if verb.result_presentation in {"browser", "modal", "approval"}:
                raise ValueError(
                    f"{name} {verb.name} returns a dialog "
                    f"({verb.result_presentation}); only the no-argument case "
                    "may open one"
                )
            for spelling in (verb.name,) + tuple(verb.aliases):
                owner = spellings.get(spelling)
                if owner is not None:
                    raise ValueError(
                        f"{name} verb {spelling!r} is claimed by both "
                        f"{owner} and {verb.name}"
                    )
                spellings[spelling] = verb.name
        spec = next(item for item in COMMAND_SPECS if item.name == name)
        if not spec.opens_browser_without_argument:
            raise ValueError(f"{name} declares subcommands but not the browser case")
        if spec.subcommands != tuple(verb.name for verb in verbs):
            raise ValueError(
                f"{name} subcommand projection drifted from the registry: "
                f"{list(spec.subcommands)} != {[v.name for v in verbs]}"
            )
        if spec.mutating_verbs != tuple(v.name for v in verbs if v.mutating):
            raise ValueError(
                f"{name} mutating-verb projection drifted from the registry"
            )
        if name not in VERB_HINT_COMMANDS:
            continue
        typed = [verb.name for verb in verbs if verb.typed]
        if "[" + "|".join(typed) + "]" not in spec.argument_hint:
            raise ValueError(
                f"{name} argument_hint does not teach its own verbs: "
                f"{spec.argument_hint!r}"
            )


_validate_headless_tables()


def command_specs() -> Tuple[CommandSpec, ...]:
    """Return the immutable built-in command metadata registry."""
    return COMMAND_SPECS


def command_spec(name: str) -> Optional[CommandSpec]:
    """Resolve a slash name or alias to its canonical command spec."""
    key = str(name or "").strip().lower()
    if not key:
        return None
    if not key.startswith("/"):
        key = "/" + key
    for spec in COMMAND_SPECS:
        if key == spec.name:
            return spec
    for spec in COMMAND_SPECS:
        if key in spec.aliases:
            return spec
    return None


def command_usage(spec: CommandSpec) -> str:
    """Return one copyable usage line for a command specification."""
    arguments = f" {spec.argument_hint}" if spec.argument_hint else ""
    aliases = f" (aliases: {', '.join(spec.aliases)})" if spec.aliases else ""
    return f"usage: {spec.name}{arguments}{aliases}"


def command_recovery_hint(spec: Optional[CommandSpec]) -> str:
    """Return a concise, actionable recovery footer for a command failure."""
    actions = (
        spec.failure_recovery
        if spec is not None
        else (
            "retry",
            "edit-input",
            "inspect-trace",
            "return-safe-state",
        )
    )
    labels = {
        "retry": "retry",
        "edit-input": "edit input",
        "cancel-command": "cancel command",
        "resume": "resume",
        "undo": "undo",
        "inspect-trace": "inspect /trace",
        "return-safe-state": "return to safe state",
    }
    return "next: " + " · ".join(labels.get(action, action) for action in actions)


def unknown_command_line(resolution: CommandResolution) -> str:
    """The one-sentence refusal for a name the registry does not carry.

    Shared so the two shells that print it say the same thing, and so the
    ``/help`` affordance lives in the contract rather than in whichever
    dispatcher happened to answer first. A refusal that does not say what
    to do next is only half a refusal.
    """
    message = str(resolution.message or "unknown command")
    if not message.rstrip().endswith("/help"):
        message = f"{message} — try /help"
    return message


def headless_policy(name: str) -> str:
    """Return how one command behaves in a non-TTY headless session."""
    spec = command_spec(name)
    if spec is None:
        return "refuse"
    return HEADLESS_COMMAND_POLICIES.get(spec.name, "mapped")


def headless_equivalent(name: str) -> str:
    """Return the CLI flag that owns a flag-only headless command."""
    spec = command_spec(name)
    if spec is None:
        return ""
    return HEADLESS_FLAG_EQUIVALENTS.get(spec.name, "")


def command_availability(
    spec: CommandSpec,
    context: Optional[CommandContext] = None,
    verb: Optional[SubcommandSpec] = None,
) -> CommandAvailability:
    """Resolve whether a command (or one of its verbs) is runnable.

    ``verb`` narrows the answer to one declared subcommand. The permission
    check is the union of the command's and the verb's, not either alone:
    ``/plugin list`` needs only ``extension:read`` and ``/plugin install``
    additionally needs ``filesystem:write``, and a gate that consulted only
    the command tuple would let a write happen under read permissions. This
    is the R2-15 §11.1 per-verb permission gap, closed.
    """
    active = CommandContext() if context is None else context
    if spec.hidden:
        return CommandAvailability(False, True, spec.hidden_reason or "hidden command")
    required = set(spec.required_permissions)
    if verb is not None:
        required |= set(verb.required_permissions)
    denied = set(active.denied_permissions).intersection(required)
    if denied:
        return CommandAvailability(
            False,
            reason="missing permission: " + ", ".join(sorted(denied)),
        )
    for name, reason in active.disabled_reasons:
        if command_spec(name) is spec:
            return CommandAvailability(False, reason=reason)
    if active.surface == "headless" and headless_policy(spec.name) == "refuse":
        return CommandAvailability(
            False,
            reason="needs an interactive session (no live run or prompt in headless mode)",
        )
    if active.has_task is False and spec.name in {
        "/status",
        "/diff",
        "/checkpoints",
        "/undo",
        "/redo",
        "/resume",
        "/review",
        "/cost",
        "/context",
        "/trace",
        "/feed",
    }:
        # Not a refusal: every one of these handlers already degrades to an
        # honest "no run in this session" line. The gate only annotates the
        # palette so the hint is honest before the user presses enter.
        note = "needs a task in this session"
    elif active.pending_approval is False and spec.name in {"/approve", "/reject"}:
        note = "no pending approval request"
    else:
        note = ""
    if not active.in_flight and spec.idle_policy == "refuse":
        return CommandAvailability(False, reason="no active run", note=note)
    if active.in_flight and spec.in_flight_policy == "refuse":
        return CommandAvailability(
            False, reason="unavailable while a run is active", note=note
        )
    if verb is not None and not active.in_flight and verb.idle_policy == "refuse":
        return CommandAvailability(
            False, reason=f"{spec.name} {verb.name}: no active run", note=note
        )
    if verb is not None and active.in_flight and verb.in_flight_policy == "refuse":
        alternative = _read_only_alternative(spec, verb)
        tail = (
            f"; {spec.name} {alternative} is available"
            if alternative
            else f"; {spec.name} with no argument is available"
        )
        return CommandAvailability(
            False,
            reason=(
                f"{spec.name} {verb.name} is unavailable while a run is active{tail}"
            ),
            note=note,
        )
    return CommandAvailability(True, note=note)


def normalize_terminal_state(
    value: Any = None,
    *,
    in_flight: bool = False,
    waiting_for_approval: bool = False,
    has_task: bool = False,
    resumed: bool = False,
    verification_evidence: Any = None,
) -> str:
    """Normalize surface or journal state to the nine-state terminal matrix.

    ``value`` may be a raw status or a journal projection mapping. A live
    approval gate takes precedence over run activity, an explicitly resumed
    run precedes ordinary running, and terminal success is upgraded only
    when clean verifier evidence is present.
    """
    snapshot: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    raw = (
        str(
            snapshot.get("display_status")
            or snapshot.get("status")
            or ("" if isinstance(value, Mapping) else value)
            or ""
        )
        .strip()
        .lower()
    )
    task_id = str(snapshot.get("task_id") or "")
    has_run = bool(has_task or task_id or raw not in {"", "idle", "queued", "unknown"})
    evidence = verification_evidence
    if evidence is None:
        evidence = snapshot.get("verification_evidence") or snapshot.get(
            "latest_verification"
        )
    if not resumed:
        result = snapshot.get("result")
        resumed = bool(
            snapshot.get("resumed")
            or snapshot.get("resume_started")
            or (isinstance(result, Mapping) and result.get("resumed"))
        )
    if not waiting_for_approval:
        waiting_for_approval = bool(
            raw
            in {"approval_required", "needs_input", "waiting_for_approval", "waiting"}
            or str(snapshot.get("approval") or "").lower() == "waiting"
        )
    if waiting_for_approval:
        return "waiting_for_approval"
    if in_flight or raw in {"running", "starting", "thinking", "verifying"}:
        return "resumed" if resumed else "running"
    if resumed and has_run:
        return "resumed"
    aliases = {
        "": "idle",
        "queued": "idle",
        "pending": "idle",
        "success": "completed_verified",
        "passed": "completed_verified",
        "verified": "completed_verified",
        "completed": "completed_unverified",
        "complete": "completed_unverified",
        "already_exists": "completed_unverified",
        "error": "failed",
        "timeout": "failed",
        "timed_out": "failed",
        "approval_required": "waiting_for_approval",
        "needs_input": "waiting_for_approval",
        "waiting": "waiting_for_approval",
        "aborted": "cancelled",
        "canceled": "cancelled",
        "interrupted": "cancelled",
    }
    normalized = aliases.get(raw, raw)
    if normalized in {"success", "completed_verified", "completed_unverified"}:
        from cli.runview import effective_terminal_status

        normalized = effective_terminal_status(normalized, evidence)
    if normalized == "resumed" and not has_run:
        return "idle"
    if normalized in SURFACE_STATES:
        return normalized
    return "running" if has_run else "idle"


def surface_command_context(
    surface: str,
    *,
    in_flight: bool = False,
    snapshot: Optional[Mapping[str, Any]] = None,
    task_id: str = "",
    waiting_for_approval: bool = False,
    pending_approval: Optional[bool] = None,
    denied_permissions: Tuple[str, ...] = (),
) -> CommandContext:
    """Build one command context from the shared terminal-state projection."""
    data = dict(snapshot or {})
    target = str(task_id or data.get("task_id") or "")
    waiting = bool(
        waiting_for_approval
        or str(data.get("approval") or "").lower() == "waiting"
        or str(data.get("status") or "").lower() in {"approval_required", "needs_input"}
    )
    if pending_approval is not None:
        waiting = waiting or bool(pending_approval)
    state = normalize_terminal_state(
        data,
        in_flight=in_flight,
        waiting_for_approval=waiting,
        has_task=bool(target),
    )
    active = bool(in_flight) or state in {
        "running",
        "waiting_for_approval",
        "resumed",
    }
    return CommandContext(
        surface=str(surface or "interactive"),
        in_flight=active,
        waiting_for_approval=waiting,
        has_task=bool(target) if target else state != "idle",
        pending_approval=waiting,
        denied_permissions=tuple(denied_permissions),
    )


def is_custom_command_line(line: str, repo_path: Optional[str] = None) -> bool:
    """Whether a slash line names a project/global custom command template.

    A name absent from :data:`COMMAND_SPECS` is NOT automatically a
    mistake: ``.neo/commands/<name>.md`` and
    ``~/.config/neo/commands/<name>.md`` are real, documented commands
    that live outside the built-in registry. A preflight that declared
    every unregistered name "unknown" before the dispatcher had a chance
    to load a template would delete that feature while still reporting a
    correct refusal for genuine typos — the two cases are
    indistinguishable from the name alone, so the rule has to ask.

    Never raises. A built-in name is never custom (a template cannot
    shadow one), which is :func:`load_command`'s own rule, so this does
    not weaken the shadowing guard.
    """
    raw = str(line or "").strip()
    if not raw.startswith("/"):
        return False
    name = raw.split(None, 1)[0].lower().lstrip("/")
    try:
        return load_command(name, repo_path=repo_path) is not None
    except Exception:
        return False


def command_outcome(
    *,
    command: str,
    args: str,
    surface: str,
    status: str,
    state_before: Any,
    state_after: Any = None,
    exit_code: int = 0,
    message: str = "",
    presentation: str = "inline",
    recovery: Tuple[str, ...] = (),
    task_id: str = "",
    verification_state: str = "not_run",
    run_status: str = "",
    evidence: Any = None,
) -> CommandOutcome:
    """Build one normalized command outcome and its lifecycle events.

    ``run_status`` and ``evidence`` are the RUN's facts, distinct from
    ``status``, which is the COMMAND's lifecycle word. They are what the
    ``verdict`` is reduced from, and a caller that has a journal
    projection in hand must pass them: reducing the command's own word
    reports ``unverified`` for a verified run, which is the same class of
    lie as the reverse.

    ``state_before`` / ``state_after`` are reduced by the SAME evidence the
    verdict is. They used to be promoted by the ``verification_state`` WORD
    alone, which is how a record could publish ``state_after="completed_verified"``
    beside ``verdict="unverified", verified=false``: the state answered to one
    input and the verdict to another, so a dirty-evidence run reported itself
    verified in the state column while the honesty column refused it. (VEX-TERM-UX-09
    blocker 4.) A word is a claim; only evidence promotes a state.
    """
    before = normalize_terminal_state(state_before, verification_evidence=evidence)
    after = normalize_terminal_state(
        state_after if state_after is not None else state_before,
        verification_evidence=evidence,
    )
    shared = {
        "surface": str(surface or "interactive"),
        "command": str(command or ""),
        "task_id": str(task_id or ""),
        "verification_state": str(verification_state or "not_run"),
        "recovery": tuple(recovery or ()),
    }
    events = [
        CommandEvent(
            event="command_started",
            state=before,
            status="accepted",
            **shared,
        )
    ]
    if after != before:
        events.append(
            CommandEvent(
                event="state_changed",
                state=after,
                status=str(status or "accepted"),
                exit_code=int(exit_code),
                message=str(message or ""),
                **shared,
            )
        )
    events.append(
        CommandEvent(
            event="command_finished",
            state=after,
            status=str(status or "accepted"),
            exit_code=int(exit_code),
            message=str(message or ""),
            **shared,
        )
    )
    return CommandOutcome(
        command=str(command or ""),
        args=str(args or ""),
        surface=str(surface or "interactive"),
        status=str(status or "accepted"),
        state_before=before,
        state_after=after,
        exit_code=int(exit_code),
        events=tuple(events),
        message=str(message or ""),
        presentation=str(presentation or "inline"),
        recovery=tuple(recovery or ()),
        task_id=str(task_id or ""),
        verification_state=str(verification_state or "not_run"),
        verdict=command_verdict(
            str(run_status or "").strip() or str(status or ""),
            task_id=task_id,
            verification_state=verification_state,
            evidence=evidence,
        ),
    )


def command_verdict(
    status: str,
    *,
    task_id: str = "",
    verification_state: str = "",
    evidence: Any = None,
) -> str:
    """Reduce one command record to the run verdict it observed.

    The one fail-closed reduction, shared by all three surfaces. It
    delegates to ``cli.runview.run_verdict``, which is the authority
    (R2-17) — a second reduction here would be a second way to call an
    unverified run verified, and would have to be kept honest separately.

    ``status`` is the RUN's status, never the command's lifecycle word.
    ``evidence`` is the journal's own verification record; without it a
    ``completed_verified`` word is not evidence and reduces to
    ``unverified``, which is the correct refusal.

    A command with NO run behind it reports :data:`NO_RUN_VERDICT` rather
    than a run verdict at all. Its own success is its ``exit_code``, which
    is already in the record; inventing a run verdict for ``/help`` would
    either claim a verification nobody ran or read as a failed run, and
    both are lies. Never raises — a broken projection reports ``unknown``.
    """
    if not str(task_id or "").strip():
        return NO_RUN_VERDICT
    try:
        from cli.runview import run_verdict
    except Exception:
        return "unknown"
    try:
        return run_verdict(
            status,
            verification_state=str(verification_state or ""),
            evidence=evidence,
        )
    except Exception:
        return "unknown"


def command_failure(exc: BaseException) -> Tuple[str, int]:
    """Return the ONE ``(status, exit_code)`` an escaping handler means.

    Three surfaces each used to invent their own answer and they did not
    agree:

    | exception | REPL | TUI | headless |
    |---|---|---|---|
    | ``KeyboardInterrupt`` | ``error`` / 1 | ``error`` / 1 | ``cancelled`` / 130 |
    | ``SandboxUnavailableError`` | ``error`` / 1 | ``error`` / 1 | ``error`` / 3 |

    So the same `Ctrl+C` was a *task failure* in two shells and an
    *interruption* in a script, and a broken Docker daemon was a
    "retry the run" in two shells and a "fix the machine" in a script.
    Every one of those is a lie about which door the user is standing in.

    This is the one place that decides. Never raises.
    """
    if isinstance(exc, KeyboardInterrupt):
        return "cancelled", 130
    from cli.exit_codes import EXIT_CODES, classify_exit_code

    try:
        code = int(classify_exit_code(exc))
    except Exception:
        code = int(EXIT_CODES["task_failure"])
    return "error", code


def command_record(
    *,
    command: str,
    args: str,
    surface: str,
    state_before: Any,
    state_after: Any = None,
    spec: Optional[CommandSpec] = None,
    status: Optional[str] = None,
    exit_code: Optional[int] = None,
    message: str = "",
    recovery: Optional[Tuple[str, ...]] = None,
    task_id: str = "",
    verification_state: str = "not_run",
    run_status: str = "",
    evidence: Any = None,
    exc: Optional[BaseException] = None,
) -> CommandOutcome:
    """Build one surface-independent command record.

    The single entry point every surface uses to finish a command, so the
    three shells cannot disagree about the envelope, the event rows, the
    presentation, or the exit code. It is a strict superset of
    :func:`command_outcome`: it resolves the spec-derived fields (so a
    caller cannot forget ``presentation``) and routes an escaping handler
    through :func:`command_failure` instead of three hand-written
    policies.

    ``run_status``/``evidence`` are the run's facts and decide the
    ``verdict``; see :func:`command_outcome` for why the command's own
    lifecycle word must not be used for that.

    ``exc`` wins over ``status``/``exit_code`` — a surface that caught an
    exception has no honest status of its own to report. Fails closed:
    an unknown surface is recorded as ``repl`` rather than accepted as a
    new vocabulary member.
    """
    name = str(surface or "").strip().lower()
    if name not in SURFACES:
        name = "repl"
    if exc is not None:
        status, exit_code = command_failure(exc)
        if not message:
            message = f"{type(exc).__name__}: {exc}"
    if status is None:
        status = "ok"
    if exit_code is None:
        exit_code = 0
    return command_outcome(
        command=command,
        args=args,
        surface=name,
        status=str(status or "ok"),
        state_before=state_before,
        state_after=state_after,
        exit_code=int(exit_code),
        message=message,
        presentation=spec.result_presentation if spec is not None else "inline",
        recovery=(
            tuple(recovery)
            if recovery is not None
            else (spec.failure_recovery if spec is not None else ())
        ),
        task_id=task_id,
        verification_state=verification_state,
        run_status=run_status,
        evidence=evidence,
    )


def _headless_answers_first(spec: CommandSpec, context: CommandContext) -> bool:
    """Does a non-TTY surface answer "run the flag" better than "bad verb"?

    On a headless surface a ``flag-only`` command is refused with the flag that
    owns the work, and a ``refuse`` command is refused as needing an
    interactive session. Both sentences are true and both are more useful than
    listing a verb vocabulary the caller cannot reach anyway - and a script
    author who typed ``/plugins probe`` needs to be told ``neo plugin list``,
    not that ``probe`` is not one of the eight verbs.

    Interactively the opposite is true, which is why this is a branch and not
    a rule: on the REPL and the TUI an unknown verb IS the user's mistake and
    the valid set is exactly what they need to see.
    """
    if str(getattr(context, "surface", "") or "") != "headless":
        return False
    return headless_policy(spec.name) in {"flag-only", "refuse"}


def _in_flight_argument_conflict(spec: CommandSpec, args: str) -> str:
    """Return why one argument form is unavailable during a live run.

    Generalized in VEX-CS-01. The per-verb decision is now read from the
    subcommand registry: a verb that declares ``mutating`` is refused while a
    run is live, a verb that does not is permitted, and the refusal NAMES the
    read-only alternative so a person is not told only that they cannot do the
    thing they asked for.

    The registry is the authority. Before this, the mutating set was a private
    module-level literal that a test pinned against ``review.diff_review_verbs``
    - which is a gate that can be updated without the dispatcher changing, and
    a dispatcher can change without the gate noticing. The set is now derived
    from the same table the dispatcher reads, and ``test_command_routing.py``
    asserts the derivation rather than the two agreeing by coincidence.
    """
    values = str(args or "").strip().split()
    if not values:
        return ""
    first = values[0].lower()
    if spec.subcommands:
        verb = subcommand_verb(spec.name, first)
        if verb is not None:
            if verb.mutating:
                alternative = _read_only_alternative(spec, verb)
                tail = (
                    f"; {spec.name} {alternative} is available"
                    if alternative
                    else f"; {spec.name} with no argument is available"
                )
                return (
                    f"{spec.name} {verb.name} changes state and is unavailable "
                    f"while a run is active{tail}"
                )
            return ""
    # The historical per-command argument rules that predate the registry and
    # are still correct: they are about a SHAPE of argument, not a verb.
    if spec.name == "/checkpoints" and first == "restore":
        return "checkpoint restore is unavailable while a run is active"
    if spec.name == "/settings" and len(values) >= 2:
        return "settings writes are unavailable while a run is active"
    if spec.name == "/mcp" and values and not subcommand_verb("/mcp", first):
        return "connector tool discovery is unavailable while a run is active"
    if spec.name == "/review" and values:
        return "a review request must wait for the active run to finish"
    return ""


def _read_only_alternative(spec: CommandSpec, refused: SubcommandSpec) -> str:
    """Name the read-only verb a person can use instead of the refused one.

    Read from the registry rather than composed from a template, so a command
    that has no read-only alternative says so rather than naming a verb that
    does not exist. The default (no-argument) verb is preferred because it is
    by construction the one that never mutates.
    """
    for candidate in subcommand_specs(spec.name):
        if candidate.mutating or candidate.name == refused.name:
            continue
        if candidate.default:
            return candidate.name
    for candidate in subcommand_specs(spec.name):
        if not candidate.mutating and candidate.name != refused.name:
            return candidate.name
    return ""


def _diff_mutating_verbs() -> Tuple[str, ...]:
    """The `/diff` first words that mutate the working tree.

    KEPT as a name because other modules and suites import it, but it now
    READS the subcommand registry rather than restating a set: the gate and
    the dispatcher are the same table, so this cannot drift from either.
    """
    return tuple(verb.name for verb in subcommand_specs("/diff") if verb.mutating)


def resolve_command_line(
    line: str, context: Optional[CommandContext] = None
) -> CommandResolution:
    """Parse and validate one slash command without executing it."""
    raw = str(line or "").strip()
    if not raw.startswith("/"):
        return CommandResolution(
            raw=raw,
            spec=None,
            args="",
            status="not_command",
            message="not a slash command",
            recovery=("edit-input",),
            exit_code=2,
        )
    pieces = raw.split(None, 1)
    name = pieces[0].lower()
    args = pieces[1].strip() if len(pieces) > 1 else ""
    spec = command_spec(name)
    if spec is None:
        return CommandResolution(
            raw=raw,
            spec=None,
            args=args,
            status="unknown",
            message=f"unknown command: {name}",
            recovery=("edit-input", "return-safe-state"),
            exit_code=2,
        )
    if spec.argument_policy == "none" and args:
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status="invalid",
            message=command_usage(spec),
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    if spec.argument_policy == "required" and not args:
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status="invalid",
            message=command_usage(spec),
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    active = CommandContext() if context is None else context
    verb = resolve_subcommand(spec, args)
    if verb is not None and not verb.ok and not _headless_answers_first(spec, active):
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status="invalid",
            message=verb.message,
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    argument_conflict = (
        _in_flight_argument_conflict(spec, args) if active.in_flight else ""
    )
    if argument_conflict:
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status="disabled",
            message=argument_conflict,
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    availability = command_availability(spec, active, verb.spec if verb else None)
    if not availability.available:
        status = "hidden" if availability.hidden else "disabled"
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status=status,
            message=availability.reason,
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    if active.in_flight and spec.in_flight_policy == "queue":
        if active.surface == "tui":
            return CommandResolution(
                raw=raw,
                spec=spec,
                args=args,
                status="queued",
                message="queued until the active run finishes",
                recovery=("cancel-command", "return-safe-state"),
            )
        return CommandResolution(
            raw=raw,
            spec=spec,
            args=args,
            status="refused",
            message="unavailable while a run is active",
            recovery=spec.failure_recovery,
            exit_code=2,
        )
    return CommandResolution(raw=raw, spec=spec, args=args, status="ok")


def argument_hint(line: str) -> str:
    """Return the composer hint for a partial slash command.

    A half-typed name (``/dif``) resolves to the single command it can
    become, so the hint teaches the argument shape while the user is still
    typing. Ambiguous prefixes fall back to the help pointer rather than
    guessing between commands.
    """
    raw = str(line or "").strip()
    if not raw or not raw.startswith("/"):
        return "Ask, change, run, or debug this repo"
    name = raw.split(None, 1)[0]
    spec = command_spec(name)
    if spec is None:
        token = name.lstrip("/")
        if token and " " not in token:
            candidates = [
                candidate
                for candidate in COMMAND_SPECS
                if candidate.name.lstrip("/").startswith(token)
            ]
            if len(candidates) == 1:
                spec = candidates[0]
            elif len(candidates) > 1:
                return (
                    f"{len(candidates)} commands match {name}: "
                    f"{', '.join(c.name for c in candidates[:6])}"
                )
        if spec is None:
            return "Unknown command · /help for available commands"
    if not spec.argument_hint:
        return f"{spec.summary} · no arguments"
    return f"{spec.name} {spec.argument_hint} · {spec.summary}"


# ---------------------------------------------------------------------------
# Effort (AGT-08) - the `/effort` surface and its receipts
# ---------------------------------------------------------------------------
#
# The ladder is defined once, in `runtime.model_capabilities`. This module
# names the vocabulary (so the composer hint, the palette and the parser all
# read ONE list) and holds the pure resolve/apply helpers every surface calls,
# because the REPL, the TUI and the headless runner each own their own
# dispatch loop and none of them should re-implement the decision.

#: The closed ladder as the PRODUCT sees it, including `auto`. Duplicated as
#: literals (rather than imported at module scope) so the command registry
#: stays importable on a path with no runtime package present; a test pins
#: this tuple equal to the authority's own `EFFORT_CHOICES`.
EFFORT_LADDER: Tuple[str, ...] = (
    "auto",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: A one-line cost/speed trade per rung, so `/effort` can answer the question
#: a user actually has ("which one should I pick?") instead of only echoing a
#: word. The claims are ORDINAL, not measured: they describe the direction the
#: parameter moves, never a number this tree has not measured.
EFFORT_TRADE: Dict[str, str] = {
    "auto": "provider default - nothing is sent",
    "low": "cheapest and fastest",
    "medium": "balanced",
    "high": "slower and dearer, thinks harder",
    "xhigh": "much slower; only some providers accept it",
    "max": "most expensive; only some providers accept it",
}


def effort_levels() -> Tuple[str, ...]:
    """Return the closed effort ladder, preferring the runtime authority.

    The literal tuple is the fallback for a path with no runtime package; a
    test asserts the two agree so the two can never drift.
    """
    try:
        from runtime.model_capabilities import EFFORT_CHOICES

        return tuple(EFFORT_CHOICES)
    except Exception:
        return EFFORT_LADDER


def _effort_alias(value: Any) -> str:
    """Return the canonical rung for a typed value, or ``""`` when invalid.

    Delegates to the ONE authority so a CLI alias (``hi``) and a config value
    (``high``) resolve identically - two vocabularies is how a setting ends up
    "set" in one place and ignored in another.
    """
    try:
        from runtime.model_capabilities import normalize_effort

        return normalize_effort(value)
    except Exception:
        text = str(value or "").strip().lower().replace("_", "-")
        return text if text in EFFORT_LADDER else ""


def effort_receipt(
    state: Optional[Mapping[str, Any]] = None,
    config: Optional[Mapping[str, Any]] = None,
    arguments: str = "",
) -> Dict[str, Any]:
    """Resolve `/effort` and return the receipt every surface renders.

    Bare `/effort` reports; `/effort <level>` also reports what WOULD be sent
    for the current model. The receipt never mutates anything: a caller
    applies it with :func:`apply_effort` after deciding the surface may.

    The `plan` is the honest part: a provider with no effort knob comes back
    as `unsupported_model` with a sentence saying so, and `sent` is claimed
    only when a real parameter is named. A level the operator can type but
    the provider cannot accept is reported as `unsupported_level`, never
    quietly rounded down to the nearest rung it does accept.
    """
    values: Dict[str, Any] = {}
    for source in (config or {}, state or {}):
        if isinstance(source, Mapping):
            values.update(source)
    requested = str(arguments or "").strip()
    if requested:
        level = _effort_alias(requested)
        if not level:
            return {
                "command": "/effort",
                "ok": False,
                "requested": requested,
                "level": _effort_alias(values.get("effort")) or "auto",
                "source": "session",
                "changed": False,
                "levels": list(effort_levels()),
                "error": (
                    f"{requested!r} is not an effort level; expected one of "
                    f"{', '.join(effort_levels())}"
                ),
                "plan": None,
            }
        values = {**values, "effort": level}
    try:
        from runtime.model_capabilities import resolve_effort

        level, source = resolve_effort(values)
    except Exception:
        level, source = _effort_alias(values.get("effort")) or "auto", "session"
    model = str(values.get("model") or "")
    provider = str(values.get("provider") or "")
    plan: Optional[Dict[str, Any]] = None
    if requested:
        try:
            from runtime.model_capabilities import map_effort

            plan = map_effort(
                level,
                model,
                provider=provider,
                parameter=values.get("effort_parameter"),
            ).to_dict()
        except Exception as exc:  # a broken authority must not break /effort
            plan = {
                "effort": level,
                "effort_status": "unreported",
                "effort_sent": False,
                "effort_detail": f"the effort authority is unavailable: {exc}",
            }
    return {
        "command": "/effort",
        "ok": True,
        "requested": requested,
        "level": level,
        "source": source,
        "changed": bool(requested),
        "model": model,
        "provider": provider,
        "levels": list(effort_levels()),
        "trade": EFFORT_TRADE.get(level, ""),
        "error": "",
        "plan": plan,
    }


def apply_effort(state: Optional[Dict[str, Any]], receipt: Mapping[str, Any]) -> bool:
    """Apply a `/effort <level>` receipt. Returns whether anything changed.

    Two writes, and both are needed. `state["effort"]` is what a session
    renders and what the next run's config is built from; the environment
    variable is what the ROUTER resolves when a run's config is assembled by a
    path this command does not own (a worker subprocess, a mode module, the
    headless runner). Setting only the session key would leave the router on
    `auto` for every run the command did not personally build, which is exactly
    the "set to high that silently does nothing" failure the ladder exists to
    remove.
    """
    if not isinstance(receipt, Mapping) or not receipt.get("changed"):
        return False
    if not receipt.get("ok"):
        return False
    level = str(receipt.get("level") or "").strip().lower()
    if not level:
        return False
    if isinstance(state, dict):
        state["effort"] = level
    try:
        from harness.config import EFFORT_ENV_VAR

        os.environ[EFFORT_ENV_VAR] = level
    except Exception:  # pragma: no cover - a read-only environ is not fatal
        return isinstance(state, dict)
    return True


def render_effort(receipt: Mapping[str, Any]) -> List[str]:
    """Render an :func:`effort_receipt` as plain, markup-free lines.

    Plain text on purpose: this crosses into a Textual/Rich renderer that
    parses brackets, and a level name or a provider detail is DATA, so the
    caller must never hand it over as markup.
    """
    if not isinstance(receipt, Mapping):
        return ["(effort unavailable)"]
    levels = ", ".join(str(item) for item in receipt.get("levels") or ())
    if not receipt.get("ok"):
        return [
            f"effort unchanged at {receipt.get('level') or 'auto'}",
            str(receipt.get("error") or "unusable level"),
            f"levels: {levels}",
        ]
    level = str(receipt.get("level") or "auto")
    lines = [f"effort: {level} ({receipt.get('source') or 'default'})"]
    trade = str(receipt.get("trade") or "")
    if trade:
        lines.append(f"  {trade}")
    model = str(receipt.get("model") or "")
    if model:
        lines.append(f"  model: {model}")
    plan = receipt.get("plan")
    if isinstance(plan, Mapping):
        status = str(plan.get("effort_status") or "")
        parameter = str(plan.get("effort_parameter") or "")
        if plan.get("effort_sent"):
            lines.append(
                f"  sends: {parameter} = {plan.get('effort_value')!r} "
                f"({plan.get('effort_family') or 'provider'})"
            )
        else:
            lines.append(f"  nothing sent: {status}")
        detail = str(plan.get("effort_detail") or "")
        if detail:
            lines.append(f"  {detail}")
    elif not receipt.get("changed"):
        lines.append(f"  levels: {levels}")
        lines.append("  set one with /effort <level>")
    return lines


def interactive_dispatch_refusal(spec: Optional[CommandSpec]) -> str:
    """Return why an INTERACTIVE surface must not run this command, or ``""``.

    A SEPARATE axis from :func:`command_availability`, and deliberately so.
    Availability answers "may this run in this STATE"; this answers "does any
    shell have a handler for it at all". Folding the second into the first
    would make an honest ``disabled`` indistinguishable from a state-policy
    refusal, and would break the determinism pin in
    ``test_cli_command_system.py`` that walks every command through all nine
    states expecting the declared policies to be the only thing that varies.

    Two outcomes, both honest refusals:

    * a row whose handler another terminal owns is told the flag that does the
      work - ``/hooks``, ``/migrate``, ``/support-bundle``;
    * a row with ``interactive_dispatch="flag-only"`` is told the same thing
      even when an implementation exists in this tree, which is how a row that
      BOTH shells can run the moment the other is mounted stays honest in the
      meantime.

    Either way a row that is listed in ``/help`` and cannot be run from a
    session names the door. Silence would leave a person believing a command
    does not exist.
    """
    if spec is None:
        return ""
    if spec.interactive_dispatch != "flag-only" and spec.name not in (
        HANDED_OFF_COMMANDS
    ):
        return ""
    equivalent = headless_equivalent(spec.name) or f"neo {spec.name.lstrip('/')}"
    return f"{spec.name} runs from the command line in this build: run `{equivalent}`"


def command_palette_entries(
    context: Optional[CommandContext] = None,
) -> List[Dict[str, Any]]:
    """Return built-in palette entries with argument and disabled-state hints.

    Every row also carries its ``command_type``, ``model_consuming`` and
    ``cost`` (VEX-CS-02), which is what makes "which of these spends tokens?"
    answerable from the MENU rather than from a source file. The keys are
    additive: a consumer reading ``hint``, ``run`` or ``result_presentation``
    sees byte-identical values to before.
    """
    from cli import command_types

    entries: List[Dict[str, Any]] = []
    for spec in COMMAND_SPECS:
        availability = command_availability(spec, context)
        # VEX-CS-01: a row whose handler no shell owns is disabled in the MENU
        # with the flag that does the work. Read from
        # `interactive_dispatch_refusal` rather than re-derived here, so the
        # palette and the REPL refusal cannot say different things.
        handed_off = interactive_dispatch_refusal(spec)
        if handed_off:
            availability = CommandAvailability(False, reason=handed_off)
        hint = spec.summary
        if spec.argument_hint:
            hint += f" · {spec.argument_hint}"
        if spec.shortcuts:
            hint += f" · {spec.shortcut_label()}"
        if not availability.available:
            hint += f" · unavailable: {availability.reason}"
        elif availability.note:
            hint += f" · {availability.note}"
        entries.append(
            {
                "kind": "command",
                "label": spec.name,
                "hint": hint,
                "value": spec.name,
                "run": spec.palette_behavior == "run",
                "disabled": not availability.available,
                "disabled_reason": availability.reason,
                "note": availability.note,
                "hidden": availability.hidden,
                "hidden_reason": availability.reason if availability.hidden else "",
                "argument_hint": spec.argument_hint,
                "shortcut": spec.shortcut_label(),
                "result_presentation": spec.result_presentation,
                "failure_recovery": list(spec.failure_recovery),
                "command_type": command_types.command_type(spec).name,
                "type_marker": command_types.type_marker(spec),
                "model_consuming": command_types.is_model_consuming(spec),
                "cost": command_types.cost_class(spec),
            }
        )
    return entries


def contextual_command_hints(
    *, active: bool = False, waiting: bool = False, width: int = 100
) -> str:
    """Return data-driven footer hints for idle, active, or waiting states."""
    if waiting:
        return "modal open · esc reject/cancel · enter confirm"
    if active:
        if width < 72:
            return "/steer · /status · /cancel"
        if width < 120:
            return "/steer · /status · /diff · /review · /cancel"
        return "/steer · /status · /diff · /review · /cost · /cancel"
    if width < 72:
        return "/help · ctrl+p · ctrl+c · ctrl+q"
    if width < 120:
        return "/help · ctrl+p palette · ctrl+r history · /sessions · /feed · ctrl+c · ctrl+q"
    return "/help · ctrl+p palette · ctrl+r history · /sessions search · /feed history · ctrl+c · ctrl+q quit"


_session_policy: Optional["ApprovalPolicy"] = None


def session_approval_policy() -> "ApprovalPolicy":
    """The ONE approval policy for a non-TUI session.

    A `session`/`path`/`command` grant is only useful if it survives to the
    NEXT request, so the policy holding it has to outlive one prompt. The TUI
    keeps its own per-app policy (`NeoApp.state["approval_policy"]`, which is
    a session by construction); the REPL has no session object, so its policy
    is process-scoped, which is exactly the session's lifetime.

    Both are reset by `reset_session_approval_policy()`, so a test (or a
    `/trust forget`, when that command lands) can revoke without a restart.
    """
    global _session_policy
    if _session_policy is None:
        _session_policy = ApprovalPolicy()
    return _session_policy


def reset_session_approval_policy() -> None:
    """Forget every retained grant. The revocation door for a session scope."""
    global _session_policy
    _session_policy = None


def keyboard_shortcuts() -> Dict[str, List[Dict[str, Any]]]:
    """Return every declared keyboard shortcut, grouped by what it reaches.

    Two groups, one authority:

    ``commands``
        keys a :class:`CommandSpec` declares, with the command they run and
        the key's purpose.
    ``surface``
        keys that open a surface (palette, history, copy) rather than run a
        command. They are declared too, because a user looking for "what does
        ctrl+p do" asks the same question about both kinds.
    ``navigation``
        keys that only move around what is already on screen.

    The TUI's ``BINDINGS`` table is the OTHER half of the same contract and
    the two are pinned against each other by
    ``tests/test_cli_command_system.py``, in both directions and with no
    exemption list: a declared key that nothing binds would render in the
    palette as a key that does nothing, and a bound key nothing declares is a
    key nobody can find.
    """
    commands: List[Dict[str, Any]] = []
    for spec in COMMAND_SPECS:
        if not spec.shortcuts:
            continue
        commands.append(
            {
                "command": spec.name,
                "keys": list(spec.shortcuts),
                "summary": spec.summary,
                "label": spec.shortcut_label(),
                "purposes": [SHORTCUT_PURPOSE.get(key, "") for key in spec.shortcuts],
            }
        )
    surface = [
        {
            "key": key,
            "surface": surface_name,
            "purpose": SHORTCUT_PURPOSE.get(key, ""),
        }
        for key, surface_name in SURFACE_SHORTCUTS
    ]
    navigation = [{"key": key, "purpose": purpose} for key, purpose in NAVIGATION_KEYS]
    return {
        "commands": commands,
        "surface": surface,
        "navigation": navigation,
    }


def approval_gate_outcome(gate_dir: "Path | str") -> Dict[str, Any]:
    """Summarize one approval gate's OWN records: what was asked and decided.

    Reads only the gate's `review.log` — the structured record the runtime
    writes — and never infers approval from a run's status or from captured
    output. That is the whole point: a surface must be able to say "the gate
    timed out" instead of "the run is fine", and a rejected or expired gate
    can never be presented as a verified result.

    `gate_dir` is the gate directory itself (`<log_root>/<id>.runtime/approval`).
    Total: a missing, unreadable, or malformed log reads as `not_requested`
    rather than raising, because a diagnostic that crashes the surface it is
    diagnosing is worse than a coarse answer.
    """
    import json

    review = Path(gate_dir) / "review.log"
    events: List[str] = []
    if review.is_file():
        try:
            raw = review.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event"):
                events.append(str(row["event"]))
    state: Dict[str, Any] = {
        "required": bool(events),
        "requested": bool(events),
        "decision": "not_requested",
        "reason": "no approval request was made",
        "events": events,
    }
    if not events:
        return state
    if "approved" in events:
        state["decision"] = "approved"
        state["reason"] = "the human approved the proposed diff"
    elif "rejected" in events:
        state["decision"] = "rejected"
        state["reason"] = "the human rejected the proposed diff"
    elif "timeout" in events:
        state["decision"] = "timeout"
        state["reason"] = "no approval decision arrived before the deadline"
    else:
        state["decision"] = "pending"
        state["reason"] = "the run ended before a decision was recorded"
    return state


def _redacted(value: Any) -> str:
    try:
        from shared.security import redact_text

        return str(redact_text(str(value or "")))
    except Exception:
        return str(value or "")


def _paths_from_diff(diff: str) -> Tuple[str, ...]:
    paths: List[str] = []
    for line in str(diff or "").splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip().split("\t", 1)[0]
        if path == "/dev/null":
            continue
        if path.startswith(("a/", "b/")):
            path = path[2:]
        path = path.replace("\\", "/").lstrip("./")
        if path and path not in paths and ".." not in Path(path).parts:
            paths.append(path)
    return tuple(paths[:20])


def approval_request_view(
    request: Mapping[str, Any], timeout_s: Optional[float] = None
) -> ApprovalRequestView:
    """Normalize and redact one exact approval request for any terminal surface."""
    data = dict(request or {})
    diff = _redacted(data.get("diff") or "")
    raw_paths = data.get("paths")
    if isinstance(raw_paths, str):
        paths = (raw_paths,)
    elif isinstance(raw_paths, (list, tuple)):
        paths = tuple(str(item) for item in raw_paths if str(item))
    else:
        paths = _paths_from_diff(diff)
    path_value = data.get("path")
    if path_value:
        normalized = str(path_value).replace("\\", "/")
        if normalized not in paths:
            paths = (*paths, normalized)
    timeout = data.get("timeout_s", timeout_s)
    try:
        timeout_value = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_value = None
    return ApprovalRequestView(
        request_id=_redacted(data.get("request_id")),
        fingerprint=_redacted(data.get("fingerprint")),
        task_id=_redacted(data.get("task_id")),
        repo_path=_redacted(data.get("repo_path")),
        paths=tuple(_redacted(path) for path in paths),
        command=_redacted(data.get("command") or ""),
        server=_redacted(data.get("server") or ""),
        side_effect=_redacted(
            data.get("side_effect") or ("workspace_write" if diff else "control")
        ),
        diff=diff,
        issue_text=_redacted(data.get("issue_text") or data.get("issue") or ""),
        summary=_redacted(data.get("summary") or ""),
        timeout_s=timeout_value,
    )


def normalize_approval_scope(value: Any) -> str:
    """Normalize once/session/path/command approval scope names."""
    text = str(value or "once").strip().lower().replace("-", "_")
    return {
        "y": "once",
        "yes": "once",
        "once": "once",
        "s": "session",
        "session": "session",
        "p": "path",
        "path": "path",
        "c": "command",
        "command": "command",
    }.get(text, "once")


def approval_from_answer(answer: Any, *, scoped: bool = False) -> Tuple[bool, str]:
    """Convert a terminal approval answer into a safe decision and scope."""
    text = str(answer or "").strip().lower()
    if not scoped:
        return text in {"y", "yes", "approve", "approved"}, "once"
    if text in {"n", "no", "reject", "rejected", "escape", "esc"}:
        return False, "once"
    if text in {"y", "yes", "once"}:
        return True, "once"
    if text in {"s", "session"}:
        return True, "session"
    if text in {"p", "path"}:
        return True, "path"
    if text in {"c", "command"}:
        return True, "command"
    return False, "once"


def _command_matches(command: str, approved_prefix: str) -> bool:
    """Return whether a command starts with an approved command prefix.

    Mirrors the kernel's session-command-prefix semantics
    (harness/agent_kernel/policy.py::_prefix_matches): a plain string
    prefix, so "pytest tests/" covers "pytest tests/test_a.py" without
    tokenizing the command line. The one addition is a boundary check, so
    a grant for "git sta" can never silently cover "git stash" while a
    grant ending in a path separator ("pytest tests/") still covers the
    whole subtree it names.
    """
    left = str(command or "").strip()
    right = str(approved_prefix or "").strip()
    if not left or not right:
        return False
    if not left.startswith(right):
        return False
    if len(left) == len(right):
        return True
    if right[-1].isspace() or right[-1] in "/\\":
        return True
    return left[len(right) : len(right) + 1].isspace()


def approval_scope_from_args(arguments: str) -> str:
    """Extract an optional scope from manual ``/approve`` arguments."""
    values = str(arguments or "").split()
    for value in reversed(values):
        normalized = normalize_approval_scope(value)
        if normalized != "once" or value.lower() in {"once", "y", "yes"}:
            return normalized
    return "once"


BUILTIN_SLASH_COMMANDS = frozenset(
    name
    for spec in COMMAND_SPECS
    if not spec.template_resolvable
    for name in (spec.name, *spec.aliases)
)

_ARG_SUB = re.compile(r"\$ARGUMENTS\b")

# Safety cap on one command template read (a pathological template must
# not blow the session).
_MAX_TEMPLATE_CHARS = 20_000


def _global_config_root() -> Path:
    """Return the platform-aware Neo global data root."""
    override = os.environ.get("NEO_GLOBAL_ROOT")
    if override:
        return Path(override).expanduser()
    config = os.environ.get("NEO_CONFIG")
    if config:
        return Path(config).expanduser().parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "neo"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "neo"
    return Path.home() / ".config" / "neo"


def _roots(repo_path: Optional[str]) -> List[Path]:
    """Command search roots in precedence order (project > global >
    plugins). Assumes repo_path is the session's repo; None = skip
    project root (list-only global/plugin commands)."""
    roots: List[Path] = []
    if repo_path:
        roots.append(Path(repo_path) / ".neo" / "commands")
    global_root = _global_config_root()
    roots.append(global_root / "commands")
    plugins = global_root / "plugins"
    if plugins.is_dir():
        try:
            for p in sorted(plugins.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    # Disabled plugins (`<name>.disabled` marker beside the
                    # directory) stay installed but undiscovered.
                    try:
                        if (p.parent / f"{p.name}.disabled").is_file():
                            continue
                    except OSError:
                        pass
                    roots.append(p / "commands")
        except OSError:
            pass
    return roots


def load_command(name: str, repo_path: Optional[str] = None) -> Optional[str]:
    """Resolve /<name> to its filled-ready template text (no $ARGUMENTS
    substitution — the caller owns that, it has the arguments).

    Assumes name is the bare command word WITHOUT the leading slash.
    Returns None for built-in names (never shadowable), unknown names,
    or unreadable files — the caller degrades to its unknown-command
    hint. Never raises.
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return None
    if f"/{name}" in BUILTIN_SLASH_COMMANDS:
        return None
    if name.startswith("."):
        return None  # hidden files never load
    for root in _roots(repo_path):
        path = root / f"{name}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()
        if text:
            return text[:_MAX_TEMPLATE_CHARS]
    return None


def list_commands(repo_path: Optional[str] = None) -> Dict[str, str]:
    """All available custom commands as {name: description-ish first line}.

    Project > global > plugin on collision (first root wins, same as
    skills). The "description" is the template's first non-heading line
    (command files have no frontmatter contract — keeping the format
    dead simple), truncated for display. Never raises.
    """
    out: Dict[str, str] = {}
    for root in _roots(repo_path):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            name = path.stem
            if not name or name.startswith(".") or f"/{name}" in BUILTIN_SLASH_COMMANDS:
                continue
            if name in out:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            desc = ""
            for ln in lines:
                if ln.startswith("#"):
                    continue  # skip title/heading lines
                desc = ln
                break
            if not lines:
                continue
            out[name] = desc[:120]
    return out


def command_names(repo_path: Optional[str] = None) -> List[str]:
    """Sorted names of available custom commands (for /help)."""
    return sorted(list_commands(repo_path))


def fill_template(template: str, arguments: str) -> str:
    """Substitute $ARGUMENTS in a template with the user's arguments.

    Assumes arguments is the raw text after the command name (may be
    empty); an empty substitution replaces the slot with "" and the
    harness's issue handling treats a template that still makes sense
    without arguments normally. Templates without a slot return
    unchanged.
    """
    return _ARG_SUB.sub(arguments or "", template or "")


# ---------------------------------------------------------------------------
# `neo worktree` — Git worktree isolation for agent runs
# ---------------------------------------------------------------------------
#
# Worktrees live in one managed root (default `<logs_root>/worktrees`) so a
# run's isolated checkouts are discoverable and removable from one place. The
# manager is the runtime's public `WorktreeManager`; this module only maps CLI
# arguments onto it and renders the result. Every failure is a usage error
# (exit 2), never a traceback: a worktree refusal is a decision, not a crash.

WORKTREE_ACTIONS = ("new", "list", "go", "rm")
_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreeCommandError(ValueError):
    """Raised when a `neo worktree` invocation cannot be satisfied."""


def _worktree_error_type() -> Any:
    """Return the runtime's worktree error type for local translation."""
    from runtime.worktrees import WorktreeError

    return WorktreeError


def worktree_root(
    log_root: Optional[str] = None, repo_path: Optional[str] = None
) -> Path:
    """Return the managed worktree root for a repository.

    Default: ``<log_root>/worktrees/<repo-name>``, so two repositories never
    share a worktree namespace and a run's isolated checkouts are all under
    the logs root the operator already knows.
    """
    base = Path(log_root) if log_root else Path("logs")
    name = Path(repo_path).resolve().name if repo_path else "repo"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "repo")[:48] or "repo"
    return base.expanduser() / "worktrees" / safe


def _worktree_manager(
    repo: str,
    *,
    log_root: Optional[str] = None,
    git_timeout_s: float = 30.0,
) -> Any:
    from runtime.worktrees import WorktreeManager

    path = Path(repo).expanduser()
    if not path.is_dir():
        raise WorktreeCommandError(f"--repo is not a directory: {repo}")
    try:
        return WorktreeManager(
            path, worktree_root(log_root, repo), git_timeout_s=git_timeout_s
        )
    except _worktree_error_type() as exc:
        raise WorktreeCommandError(str(exc)) from exc


def _validate_worktree_name(name: str) -> str:
    if not _WORKTREE_NAME.match(str(name or "")):
        raise WorktreeCommandError(
            "worktree name must start with a letter or digit and use only "
            "letters, digits, dot, underscore, or dash"
        )
    return str(name)


def worktree_new(
    repo: str,
    name: str,
    *,
    base: str = "",
    log_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one detached worktree and return its record.

    A dirty source checkout fails closed: worktrees pin a base commit, so
    silently ignoring uncommitted work would produce an isolation guarantee
    that does not hold.
    """
    manager = _worktree_manager(repo, log_root=log_root)
    try:
        manager.ensure_clean()
        record = manager.create(
            _validate_worktree_name(name),
            base_commit=base.strip() or None,
        )
    except _worktree_error_type() as exc:
        raise WorktreeCommandError(str(exc)) from exc
    return record.to_dict()


def worktree_list(
    repo: str,
    *,
    log_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return every managed worktree record, reconciled against disk."""
    manager = _worktree_manager(repo, log_root=log_root)
    return [record.to_dict() for record in manager.recover().values()]


def worktree_path(
    repo: str,
    name: str,
    *,
    log_root: Optional[str] = None,
) -> str:
    """Return the absolute path of one managed worktree."""
    manager = _worktree_manager(repo, log_root=log_root)
    record = manager.list().get(_validate_worktree_name(name))
    if record is None:
        raise WorktreeCommandError(f"unknown worktree: {name}")
    if not Path(record.path).exists():
        raise WorktreeCommandError(f"worktree path is missing: {record.path}")
    return str(Path(record.path).resolve())


def worktree_remove(
    repo: str,
    name: str,
    *,
    force: bool = False,
    log_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove one managed worktree, refusing a dirty removal unless forced."""
    manager = _worktree_manager(repo, log_root=log_root)
    key = _validate_worktree_name(name)
    record = manager.list().get(key)
    if record is None:
        raise WorktreeCommandError(f"unknown worktree: {name}")
    try:
        manager.remove(record, force=bool(force))
    except _worktree_error_type() as exc:
        raise WorktreeCommandError(str(exc)) from exc
    return {"removed": key, "path": record.path, "forced": bool(force)}


def worktree_run_config(
    repo: str,
    *,
    worktree: str = "",
    base: str = "",
    log_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the ``Task.config`` fragment for an isolated (``--worktree``) run.

    The run happens inside the worktree instead of the original checkout, so
    the original repository is never mutated by the run. The fragment also
    pins the plan and subagent bounds, which is what makes an isolated
    multi-unit run reproducible.
    """
    name = _validate_worktree_name(worktree or "run")
    record = worktree_new(repo, name, base=base, log_root=log_root)
    return {
        "worktree_isolation": True,
        "worktree_path": record["path"],
        "worktree_base_commit": record["base_commit"],
        "worktree_source_repo": record["source_repo"],
        "max_plan_steps": 12,
        "subagent_max_depth": 2,
        "subagent_max_children_per_parent": 4,
        "subagent_max_concurrent": 4,
    }


def cmd_worktree(args: Any) -> int:
    """Dispatch ``neo worktree new|list|go|rm``.

    Exit codes follow the module contract: 0 success, 2 usage error. A
    refusal from Git (dirty checkout, unapplied patch, unknown worktree) is
    reported as a clean message, never a traceback.
    """
    from cli import ui

    action = str(
        getattr(args, "worktree_action", "") or getattr(args, "action", "") or ""
    )
    if action not in WORKTREE_ACTIONS:
        ui.err_console().print(
            f"[neo.error]error: worktree action must be one of "
            f"{', '.join(WORKTREE_ACTIONS)}[/]"
        )
        return 2
    repo = str(getattr(args, "repo", "") or ".")
    name = str(getattr(args, "name", "") or "")
    log_root = getattr(args, "log_root", None)
    as_json = bool(getattr(args, "json", False))
    try:
        if action == "new":
            if not name:
                raise WorktreeCommandError("worktree new requires a name")
            payload: Any = worktree_new(
                repo, name, base=str(getattr(args, "base", "") or ""), log_root=log_root
            )
        elif action == "list":
            payload = worktree_list(repo, log_root=log_root)
        elif action == "go":
            if not name:
                raise WorktreeCommandError("worktree go requires a name")
            payload = {
                "name": name,
                "path": worktree_path(repo, name, log_root=log_root),
            }
        else:
            if not name:
                raise WorktreeCommandError("worktree rm requires a name")
            payload = worktree_remove(
                repo, name, force=bool(getattr(args, "force", False)), log_root=log_root
            )
    except WorktreeCommandError as exc:
        ui.err_console().print(f"[neo.error]error: {exc}[/]")
        return 2
    except Exception as exc:
        ui.err_console().print(f"[neo.error]error: worktree {action} failed: {exc}[/]")
        return 2
    if as_json:
        import json as _json

        print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    con = ui.console()
    if action == "list":
        if not payload:
            con.print("[neo.muted]no managed worktrees[/]")
            return 0
        for record in payload:
            con.print(
                f"[neo.accent]{record.get('node_id', '?')}[/] "
                f"[neo.muted]{record.get('state', '?')}[/] "
                f"{record.get('path', '')}"
            )
        return 0
    if action == "go":
        # soft_wrap keeps a long absolute path on one line: a wrapped path is
        # not a usable path.
        con.print(str(payload.get("path", "")), soft_wrap=True)
        return 0
    if action == "rm":
        con.print(f"[neo.muted]removed[/] {payload.get('path', '')}")
        return 0
    con.print(
        f"[neo.accent]{payload.get('node_id', '?')}[/] [neo.muted]created[/] "
        f"{payload.get('path', '')}"
    )
    con.print(f"[neo.muted]base:   {payload.get('base_commit', '')}[/]")
    return 0
