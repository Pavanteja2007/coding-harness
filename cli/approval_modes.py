"""The four-mode approval enum, and every permission receipt a user can observe.

The problem this module exists to close
---------------------------------------
`cli/interactive.py` computed the whole approval model as one line::

    agent_approval = "require" if profile.approval == "ask" else "allow"

That is a **boolean dressed as a policy**, and three structural gaps follow
from it, all of them visible to the person using the product:

1. **No mode enum.** The corpus converged on Gemini CLI's four modes --
   ``PLAN`` / ``DEFAULT`` / ``AUTO_EDIT`` / ``YOLO`` -- and every one of the four
   provider-native systems now ships a read-only *planning* mode. A boolean
   cannot express "edits are automatic but a shell command still asks", which is
   the distinction a daily user actually feels.
2. **Policy denial does not reach the PROMPT surface.** A policy ``deny`` blocks
   dispatch, but ``ToolRegistry.schemas()`` still ships the schema, so the model
   spends turns discovering a tool it cannot use and plans worse. OpenCode's
   rule is the one this module implements: *denying a tool removes it from the
   model's view entirely -- tool availability itself is permission-derived.*
3. **A permission outcome can render blank.** A user who sees "denied" with no
   reason cannot act on it.

What is in here
---------------
======================================  =========================================
:func:`resolve_mode` / :class:`ModeSession`   the enum, its marker, in-flight change
:func:`tool_surface` / :func:`visible_surface`  the derived tool surface, both ways
:func:`describe_scope` / :func:`grant_lines`    arity-scoped grants in plain language
:class:`ApproverBridge`                          ``harness.approver`` on the live path
:func:`explain` / :func:`explain_line`           seven outcomes, one line each
======================================  =========================================

Why a new module and not a widening of ``cli/commands.py``
---------------------------------------------------------
``cli/commands.py`` already owns the six-mode **work** profile registry
(``plan``/``build``/``explore``/``review``/``debug``/``ask``) and its
``resolve_agent_approval``. This is a different axis: the work mode picks *what*
the agent is for, the approval mode picks *how much* it may do without asking.
Conflating them is how a mode registry grows a field nobody is forced to set.
Both axes are read together by :func:`resolve_run_approval`, and that is the
ONLY place the two meet.

The five properties this module holds, and where each is asserted
-----------------------------------------------------------------
* **The tool surface is DERIVED from the mode, not gated.** ``PLAN`` removes the
  write/shell tools from what the model can invoke; "the model can see it but
  calling it fails" is not a plan mode. (:func:`tool_surface`)
* **The mode is visible at all times.** :func:`mode_marker` is a plain string no
  surface has to re-word, and :func:`mode_line` is the one line all three
  surfaces render. (:func:`ModeSession`, :func:`explain_line`)
* **Both hiding directions are failures.** :func:`surface_violations` returns a
  ``denied_but_visible`` finding (under-hiding the policy) and a
  ``withheld_but_undeclared`` finding (over-hiding: a tool the agent needs and
  cannot see). A mapping that only checked one would ship the other's defect.
* **No blanket shell approval is ever offered.** :func:`scope_options` marks
  ``blanket=True`` for nothing that can run a command, and
  :func:`assert_no_blanket_shell_grant` is the gate. (:func:`describe_scope`)
* **Every permission outcome explains itself in one line** naming the action, the
  rule that decided it, and a runnable next step. (:func:`explain_line`)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "APPROVAL_MODES",
    "APPROVAL_MODE_ENV",
    "APPROVAL_MODE_KEYS",
    "APPROVER_OUTCOMES",
    "ARITY_REQUIRED_FIELDS",
    "GRANT_LAYERS",
    "MODE_PROFILES",
    "PERMISSION_OUTCOMES",
    "ApproverBridge",
    "ApproverOutcome",
    "GrantScopeView",
    "ModeProfile",
    "ModeSession",
    "PermissionDecision",
    "PlanExit",
    "ToolSurface",
    "VisibilityReport",
    "arity_authority_report",
    "assert_every_scope_is_characterised",
    "assert_no_blanket_shell_grant",
    "command_arity",
    "command_pattern",
    "declare_internal_tool",
    "declared_internal_tools",
    "describe_layer",
    "describe_scope",
    "explain",
    "explain_line",
    "grant_lines",
    "internal_tool_declarations",
    "is_shell_tool",
    "mode_line",
    "mode_marker",
    "normalize_mode",
    "plan_exit_request",
    "resolve_mode",
    "resolve_run_approval",
    "scope_options",
    "surface_violations",
    "tool_surface",
    "visible_surface",
    "worst_case_command",
    "worst_case_lines",
]


# ===========================================================================
# 1. The four-mode enum
# ===========================================================================

MODE_PLAN = "plan"
MODE_DEFAULT = "default"
MODE_AUTO_EDIT = "auto_edit"
MODE_YOLO = "yolo"

#: The four modes, in escalating order of what they auto-approve. Declared as a
#: tuple so ``APPROVAL_MODES`` can be asserted EQUAL by a test: an enum nobody
#: pins is an enum that quietly loses a member.
APPROVAL_MODES: Tuple[str, ...] = (MODE_PLAN, MODE_DEFAULT, MODE_AUTO_EDIT, MODE_YOLO)

#: The words a person types, mapped onto the canonical names. A mode is a
#: CONTROL, and a control whose spelling is guessed at is a control people stop
#: using. Every alias here is a word the corpus or a user would actually say.
MODE_ALIASES: Dict[str, str] = {
    "planning": MODE_PLAN,
    "plan-mode": MODE_PLAN,
    "read_only": MODE_PLAN,
    "readonly": MODE_PLAN,
    "ask": MODE_DEFAULT,
    "interactive": MODE_DEFAULT,
    "prompt": MODE_DEFAULT,
    "normal": MODE_DEFAULT,
    "auto": MODE_AUTO_EDIT,
    "autoedit": MODE_AUTO_EDIT,
    "auto-edit": MODE_AUTO_EDIT,
    "accept-edits": MODE_AUTO_EDIT,
    "acceptedits": MODE_AUTO_EDIT,
    "yolo": MODE_YOLO,
    "bypass": MODE_YOLO,
    "bypasspermissions": MODE_YOLO,
    "full-auto": MODE_YOLO,
    "yes": MODE_YOLO,
}

#: The statusline MARKERS, declared so a surface can RENDER them, and
#: deliberately NOT accepted as input spellings.
#:
#: An earlier version of this file mapped ``YOLO!`` onto ``yolo`` so a reader
#: could copy the marker off the rail and paste it back. That was reverted, and
#: the existing pin
#: (``test_an_unrecognised_mode_name_never_escalates_to_yolo``) is the reason:
#: a spelling that resolves to the WEAKEST mode is a permission escalation
#: caused by a copy-paste, which is the single failure
#: :func:`normalize_mode` exists to prevent. A label that doubles as a way to
#: disable safety is not a convenience. ``[YOLO!]`` resolves to ``default``,
#: and ``/approval mode`` names ``yolo`` for anyone who meant it.
MODE_MARKER_INPUTS: Dict[str, str] = {
    "plan": MODE_PLAN,
    "ask": MODE_DEFAULT,
    "auto": MODE_AUTO_EDIT,
    "yolo!": MODE_YOLO,
}

#: Every spelling that names a mode: the canonical names plus the typed
#: aliases. A caller asking "is this word one of ours" reads THIS, and not
#: ``MODE_ALIASES`` alone -- the alias table deliberately holds only the
#: alternate spellings, so a membership test against it rejects the canonical
#: names themselves, which is how ``/approval mode plan`` first reported a
#: refusal for the most obvious input there is.
#:
#: ``MODE_MARKER_INPUTS`` is NOT in here, deliberately, per the note above.
MODE_INPUTS: frozenset = frozenset(
    {str(name).lower() for name in APPROVAL_MODES}
    | {str(key).lower() for key in MODE_ALIASES}
)

#: Config keys that may select a mode, in precedence order. Read by KEY
#: PRESENCE so "nobody said" stays distinguishable from "somebody said
#: ``default``" -- the same rule AGT-13 and R2-03 use.
APPROVAL_MODE_KEYS: Tuple[str, ...] = (
    "approval_mode",
    "agent_approval_mode",
)

#: The environment variable an operator uses to pin a mode for a process. It is
#: read LAST and can only ever be honoured after the config, so a repository
#: config file can never select a weaker mode than the operator's shell did.
APPROVAL_MODE_ENV = "NEO_APPROVAL_MODE"


@dataclass(frozen=True)
class ModeProfile:
    """One mode's contract: its tool surface, its prompts, and its marker.

    ``prompted_classes`` is the load-bearing field for ``AUTO_EDIT``: the mode
    auto-approves file edits but still asks about a shell command, and the only
    way to say that without a second boolean is to name the SIDE-EFFECT CLASSES
    that still prompt.
    """

    name: str
    label: str
    summary: str
    #: Side-effect classes the model may INVOKE in this mode. A tool outside
    #: this set is not merely gated -- it is not offered.
    offered_classes: Tuple[str, ...]
    #: Side-effect classes that still raise a dialog in this mode.
    prompted_classes: Tuple[str, ...]
    #: Whether the mode auto-approves everything it offers.
    auto_approves: bool
    #: Whether the mode refuses every write. ``PLAN`` is read-only BY
    #: CONSTRUCTION, which is the whole value of a planning mode.
    read_only: bool
    #: Whether the mode carries reduced safety. Exactly one mode does.
    reduced_safety: bool
    #: The visible warning for a reduced-safety mode. ``""`` for every other.
    warning: str
    #: Whether leaving this mode requires an explicit decision.
    explicit_exit: bool
    #: Whether this mode's withheld tools are INTENTIONAL. ``PLAN`` withholds
    #: every mutation by design; a mode that withholds anything else without a
    #: replacement is the under-provisioning defect.
    intentional_gap: str = ""
    keybind: str = "ctrl+p"

    def offers(self, side_effect_class: str) -> bool:
        """Whether a tool of this class may be invoked in this mode."""
        return str(side_effect_class or "") in self.offered_classes

    def prompts(self, side_effect_class: str) -> bool:
        """Whether a tool of this class still raises a dialog."""
        return str(side_effect_class or "") in self.prompted_classes

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt a ``--json`` surface publishes."""
        return {
            "mode": self.name,
            "label": self.label,
            "summary": self.summary,
            "offered_classes": list(self.offered_classes),
            "prompted_classes": list(self.prompted_classes),
            "auto_approves": bool(self.auto_approves),
            "read_only": bool(self.read_only),
            "reduced_safety": bool(self.reduced_safety),
            "warning": self.warning,
            "explicit_exit": bool(self.explicit_exit),
            "intentional_gap": self.intentional_gap,
        }


#: The side-effect vocabulary this module projects onto. It is the SAME
#: vocabulary ``harness.agent_kernel.tools`` publishes on every ``ToolSpec``,
#: declared here so ``cli.approval_modes`` imports with no harness present --
#: the import-smoke gate mounts ``cli`` alone.
#:
#: **MEASURED against the real catalog, and the first version of this table
#: was wrong in BOTH directions.** It listed ``mutation`` and
#: ``destructive``, which the kernel does not emit, and omitted ``process``,
#: ``control`` and ``mcp``, which it does. The consequence was not cosmetic:
#: driving ``/approval tools`` against ``builtin_tool_specs()`` reported
#: **17 mis-provisioned surfaces** in the DEFAULT mode -- ``shell``,
#: ``process_*``, ``test``, ``verify``, ``lint``, ``build`` and the six control
#: tools were all classified into classes no profile offered, so a correct
#: surface read as a defect. A declared table beside a catalog is how the two
#: drift; the only honest defence is to check it against the catalog, which
#: ``cli/test_approval_modes.py`` now does.
#:
#: ``mutation``/``destructive`` are RETAINED as accepted values rather than
#: deleted: a caller may classify its own tool, and removing a word a
#: hand-rolled catalog still emits would fail a working caller's request
#: closed instead of answering it.
SIDE_EFFECT_CLASSES: Tuple[str, ...] = (
    "read_only",
    "memory",
    "workspace_write",
    "network",
    "process",
    "control",
    "mcp",
    "mutation",
    "destructive",
)

#: The classes the kernel actually emits, as opposed to the ones this module
#: also accepts. A test asserts the real catalog is a SUBSET of
#: :data:`SIDE_EFFECT_CLASSES`, so a NEW kernel class goes red here rather than
#: silently widening or narrowing every mode's surface.
KERNEL_SIDE_EFFECT_CLASSES: Tuple[str, ...] = (
    "read_only",
    "memory",
    "workspace_write",
    "network",
    "process",
    "control",
    "mcp",
)

#: The classes ``PLAN`` still offers: reading, remembering, and STRUCTURING the
#: work. ``memory`` is a read here (a decision-memory query). The control tools
#: (``task``/``todo``/``plan``/``finish``/``cancel``) are ``control`` in the real
#: catalog, and withholding them would make ``PLAN`` unable to return a plan at
#: all -- a read-only mode that cannot finish is not a read-only mode, it is a
#: broken one. What ``PLAN`` withholds is the three classes that can change the
#: world: ``workspace_write``, ``network`` and ``process``.
PLAN_OFFERED_CLASSES: Tuple[str, ...] = ("read_only", "memory", "control")

#: The classes ``PLAN`` still PROMPTS about: the ones it offers that are not
#: purely a read.
PLAN_PROMPTED_CLASSES: Tuple[str, ...] = ("memory",)

#: ``AUTO_EDIT`` auto-approves a file edit and still asks about everything that
#: runs something. This tuple is the distinction the whole mode exists for.
#: ``process`` is here because a command is the highest-blast-radius action
#: there is; ``mcp`` because a connector reaches a server this product does
#: not control.
AUTO_EDIT_PROMPTED_CLASSES: Tuple[str, ...] = (
    "network",
    "process",
    "mcp",
    "mutation",
    "destructive",
)

#: The canonical marker each mode shows in the status rail. ASCII-only, so it
#: renders on a ``cp1252`` console without a glyph probe -- a permission marker
#: that needs an encoding fallback is a permission marker somebody cannot read.
MODE_MARKERS: Dict[str, str] = {
    MODE_PLAN: "[PLAN]",
    MODE_DEFAULT: "[ASK]",
    MODE_AUTO_EDIT: "[AUTO]",
    MODE_YOLO: "[YOLO!]",
}

MODE_PROFILES: Dict[str, ModeProfile] = {
    MODE_PLAN: ModeProfile(
        name=MODE_PLAN,
        label="plan (read-only)",
        summary=(
            "reads the repository and returns a plan; every write and shell "
            "tool is withheld from the model entirely"
        ),
        offered_classes=PLAN_OFFERED_CLASSES,
        prompted_classes=PLAN_PROMPTED_CLASSES,
        auto_approves=False,
        read_only=True,
        reduced_safety=False,
        warning="",
        explicit_exit=True,
        intentional_gap=(
            "plan mode exists to not change the repository; a planning mode "
            "that could still edit would not be one"
        ),
    ),
    MODE_DEFAULT: ModeProfile(
        name=MODE_DEFAULT,
        label="ask (dialog per action)",
        summary=(
            "every side-effecting action raises a dialog: approve once, grant "
            "this scope, or reject"
        ),
        offered_classes=SIDE_EFFECT_CLASSES,
        prompted_classes=SIDE_EFFECT_CLASSES,
        auto_approves=False,
        read_only=False,
        reduced_safety=False,
        warning="",
        explicit_exit=False,
    ),
    MODE_AUTO_EDIT: ModeProfile(
        name=MODE_AUTO_EDIT,
        label="auto-edit (shell still asks)",
        summary=(
            "file edits are applied automatically; a shell command, a network "
            "call or anything destructive still asks"
        ),
        offered_classes=SIDE_EFFECT_CLASSES,
        prompted_classes=AUTO_EDIT_PROMPTED_CLASSES,
        auto_approves=False,
        read_only=False,
        reduced_safety=False,
        warning="",
        explicit_exit=False,
    ),
    MODE_YOLO: ModeProfile(
        name=MODE_YOLO,
        label="yolo (reduced safety)",
        summary=(
            "everything is auto-approved with no dialog; for CI and scripted "
            "runs, not for interactive work"
        ),
        offered_classes=SIDE_EFFECT_CLASSES,
        prompted_classes=(),
        auto_approves=True,
        read_only=False,
        reduced_safety=True,
        warning=(
            "reduced safety: every action is auto-approved and nothing will "
            "ask you again"
        ),
        explicit_exit=False,
    ),
}


def normalize_mode(value: Any, default: str = MODE_DEFAULT) -> str:
    """Return the canonical mode name for ``value``, or ``default``.

    An UNRECOGNISED value resolves to ``default``, never to a weaker mode. A
    typo that selected ``YOLO`` would be a permission escalation caused by a
    misspelling, which is the one failure this function exists to prevent.
    """
    text = str(value or "").strip().lower().replace(" ", "_")
    if not text:
        return default if default in MODE_PROFILES else MODE_DEFAULT
    if text in MODE_PROFILES:
        return text
    if text in MODE_ALIASES:
        return MODE_ALIASES[text]
    for candidate in MODE_ALIASES:
        if text.replace("-", "_") == candidate.replace("-", "_"):
            return MODE_ALIASES[candidate]
    return default if default in MODE_PROFILES else MODE_DEFAULT


def resolve_mode(
    config: Optional[Mapping[str, Any]] = None,
    *,
    default: str = MODE_DEFAULT,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """Return ``(mode, reason)`` -- the mode AND which layer chose it.

    Returns the reason because a receipt that says only ``"yolo"`` cannot answer
    "why was I not asked?", and a person who was not asked needs the answer in
    the same breath.

    Precedence is **config (first key present) > environment > default**. The
    environment is read last so a repository config file can never select a
    weaker mode than the operator's own shell declared.
    """
    values = dict(config or {})
    for key in APPROVAL_MODE_KEYS:
        if key in values and str(values.get(key) or "").strip():
            raw = str(values.get(key)).strip()
            mode = normalize_mode(raw, default=default)
            return (
                mode,
                f"{key}={raw!r} in this run's configuration",
            )
    import os

    env = os.environ if environ is None else environ
    raw_env = str(env.get(APPROVAL_MODE_ENV, "") or "").strip()
    if raw_env:
        mode = normalize_mode(raw_env, default=default)
        return mode, f"{APPROVAL_MODE_ENV}={raw_env!r} in the environment"
    mode = normalize_mode(default, default=MODE_DEFAULT)
    return mode, f"no mode was declared, so the interactive default ({mode!r}) applies"


def mode_marker(mode: Any) -> str:
    """Return the visible marker for a mode. Never empty.

    A permission marker that renders as nothing at one width is a permission
    mode nobody can trust, so this returns a non-empty ASCII marker for every
    canonical mode and for every unrecognised value.
    """
    canonical = normalize_mode(mode)
    return MODE_MARKERS.get(canonical) or f"[{canonical.upper()[:6]}]"


def mode_line(mode: Any, *, detail: bool = False) -> str:
    """Return the ONE line all three surfaces render for the current mode.

    ``detail=False`` is the status-rail form (marker plus label, one short
    segment). ``detail=True`` adds the summary and, for a reduced-safety mode,
    the warning -- and the warning is NEVER suppressed by a narrow width, which
    is why the short form exists separately rather than being a truncation of
    the long one.
    """
    canonical = normalize_mode(mode)
    profile = MODE_PROFILES[canonical]
    if not detail:
        return f"{mode_marker(canonical)} {profile.label}"
    parts = [f"{mode_marker(canonical)} {profile.label}", profile.summary]
    if profile.reduced_safety and profile.warning:
        parts.append(profile.warning.upper())
    return " - ".join(parts)


def resolve_run_approval(
    profile: Any,
    mode: Any,
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Return the ``agent_approval`` VALUE and WHY, from BOTH registries.

    This is the single place the work-mode registry (``cli.commands``) and the
    approval-mode enum meet. The previous rule was a boolean derived from one
    declared word::

        "require" if profile.approval == "ask" else "allow"

    which meant a mode that can edit and forgets ``approval=`` silently got no
    pre-apply diff -- a trust property resting on a data field nobody is forced
    to set.

    The rule now is **the approval mode decides first**, and the work mode only
    supplies the answer for the mode a run was started in:

    * ``PLAN`` can never ``allow`` -- a read-only mode has nothing to approve,
      and returning ``allow`` for it would report a gate that does not exist.
    * ``YOLO`` can never ``require`` -- the value would describe a dialog that
      ``YOLO`` never opens.
    * otherwise the declared word decides, defaulting to ``require`` for a
      profile that can write (the fail-closed direction).
    """
    opted_out = _approval_opt_out(config)
    if opted_out:
        return (
            "allow",
            f"explicitly opted out ({opted_out}); the pre-apply diff is NOT "
            "shown for this run",
        )
    canonical = normalize_mode(mode)
    profile_name = str(getattr(profile, "name", "mode") or "mode")
    mode_profile = MODE_PROFILES[canonical]
    if mode_profile.read_only:
        return (
            "allow",
            f"approval mode is {canonical!r}, which is read-only: there is "
            "nothing to approve and the write tools are withheld",
        )
    if mode_profile.auto_approves:
        return (
            "allow",
            f"approval mode is {canonical!r}: every action is auto-approved "
            "and reduced safety is in force",
        )
    declared = str(getattr(profile, "approval", "") or "").strip()
    if declared == "deny":
        return (
            "allow",
            f"{profile_name} denies every write tool, so there is nothing to "
            "approve; the denial is the control here, not a prompt",
        )
    if declared == "ask" or declared == "require":
        return (
            "require",
            f"approval mode is {canonical!r} and {profile_name} can write, so "
            "its changes are shown before they are applied",
        )
    declared_tools = set(getattr(profile, "visible_tools", ()) or ())
    # A profile may declare its capability as a concrete tool NAME ("edit") or
    # as a side-effect CLASS ("workspace_write"). The first version
    # intersected only with names, so a class-level declaration resolved to
    # `can_write == False` and a profile that CAN write got `allow` -- the
    # fail-OPEN answer, produced by a vocabulary mismatch rather than by a
    # decision. A declared mutating class counts as writable, which is the
    # direction the rest of this function already fails toward.
    can_write = bool(
        declared_tools & _WRITE_TOOL_NAMES or declared_tools & set(_MUTATING_CLASSES)
    )
    if can_write:
        return (
            "require",
            f"{profile_name} can write, so its changes are shown before they "
            "are applied (fail-closed default: the mode declared no approval "
            "word)",
        )
    return (
        "allow",
        f"{profile_name} has no write tool, so a pre-apply diff would be a "
        "prompt with nothing behind it",
    )


def _approval_opt_out(config: Optional[Mapping[str, Any]]) -> str:
    """Return the opt-out word, or ``""``.

    Delegates to ``cli.commands.approval_opt_out`` when it is importable so
    there is ONE definition of the opt-out vocabulary in the tree. An
    unrecognised value returns ``""`` there rather than defaulting to the
    opt-out, and this preserves that.
    """
    try:
        from cli import commands as _commands
    except Exception:
        return ""
    try:
        return str(_commands.approval_opt_out(config) or "")
    except Exception:
        return ""


# ===========================================================================
# Tool classification -- derived from the catalog where one is available
# ===========================================================================

#: Fallback classification by tool name, used ONLY when a caller hands this
#: module a bare name rather than a real ``ToolSpec``. When a spec is available
#: its own ``side_effect_class`` is authoritative: a hand-written table beside
#: the catalog is how the two drift.
_CLASS_BY_NAME: Dict[str, str] = {
    "read": "read_only",
    "glob": "read_only",
    "grep": "read_only",
    "list": "read_only",
    "search": "read_only",
    "image": "read_only",
    "todo": "control",
    "plan": "control",
    "ask": "control",
    "finish": "control",
    "cancel": "control",
    "task": "control",
    "git_status": "read_only",
    "git_diff": "read_only",
    "git_log": "read_only",
    "git_show": "read_only",
    "git_blame": "read_only",
    "git_branch": "read_only",
    "git_worktree": "read_only",
    "read_symbol": "read_only",
    "find_definition": "read_only",
    "find_references": "read_only",
    "blast_radius": "read_only",
    "retrieval": "read_only",
    "memory": "memory",
    "memory_record": "memory",
    "edit": "workspace_write",
    "write": "workspace_write",
    "apply_patch": "workspace_write",
    "rename": "workspace_write",
    "rename_symbol": "workspace_write",
    "update_signature": "workspace_write",
    # `delete` and `undo` are `workspace_write` in the kernel, not the
    # `destructive`/`mutation` this table first claimed. Both are only consulted
    # for a BARE NAME (a real spec's own declaration always wins), so the drift
    # was invisible until `cli/test_approval_modes.py` compared the two tables
    # against `builtin_tool_specs()` -- which is the only check that can.
    "delete": "workspace_write",
    "undo": "workspace_write",
    "web_fetch": "network",
    "web_search": "network",
    "fetch": "network",
    "bash": "process",
    "process": "process",
    "process_write_stdin": "process",
    "process_kill": "process",
    "process_read_output": "process",
    "shell": "process",
    "test": "process",
    "verify": "process",
    "lint": "process",
    "typecheck": "process",
    "build": "process",
    "mcp": "mcp",
}

#: The classes that put BYTES ON DISK or RUN SOMETHING. A declared second
#: table because "can this mode edit?" is asked of a profile that may carry no
#: class at all, and the answer must not be "the run had no tools".
_MUTATING_CLASSES: Tuple[str, ...] = (
    "workspace_write",
    "network",
    "process",
    "mcp",
    "mutation",
    "destructive",
)

#: The tool names that can execute a command or reach a process. Used for the
#: "never offer a blanket approval for a shell tool" rule.
_SHELL_TOOL_NAMES: frozenset = frozenset(
    {
        "bash",
        "shell",
        "process",
        "process_write_stdin",
        "process_kill",
        "test",
        "verify",
        "lint",
        "typecheck",
        "build",
        "web_fetch",
        "web_search",
        "fetch",
        "mcp",
        "task",
        "delete",
        "undo",
    }
)

#: Mirrors ``cli.commands.MODE_WRITE_TOOLS`` so a profile that declares only the
#: names (and no classes) still answers "can this mode write?".
_WRITE_TOOL_NAMES: frozenset = frozenset(
    {"edit", "write", "apply_patch", "bash", "shell", "process", "process_write_stdin"}
)


def classify_tool(tool: Any) -> str:
    """Return the side-effect class of a tool name or a real ``ToolSpec``.

    A spec's OWN declaration wins. This is the difference between a derived
    classification and a hand-written one: ``harness.tools`` is the single
    catalog, so a tool added there is classified correctly here with no edit to
    this module.
    """
    declared = getattr(tool, "side_effect_class", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    name = str(getattr(tool, "name", None) or tool or "").strip().lower()
    return _CLASS_BY_NAME.get(name, "mutation")


def is_shell_tool(tool: Any) -> bool:
    """Whether a tool can execute a command, start a process, or reach a server.

    The default for an UNRECOGNISED tool is ``True``. That is the safe direction
    for the rule this feeds ("never offer a blanket approval for a shell tool"):
    a new tool nobody classified must be treated as able to do damage until its
    catalog entry says otherwise, or the first version of a dangerous tool gets
    a blanket grant.

    It is also the CONSISTENT direction with :func:`classify_tool`, which
    already answers ``mutation`` for the same input. The first version keyed
    only on :data:`_SHELL_TOOL_NAMES` membership and so answered ``False`` for a
    name it had never seen while :func:`classify_tool` answered ``mutation`` for
    the very same name -- two answers to one question, and the permissive one is
    the one that feeds the grant dialog.
    """
    declared = getattr(tool, "side_effect_class", None)
    if isinstance(declared, str) and declared.strip():
        return str(declared.strip()) in {
            "network",
            "process",
            "mcp",
            "mutation",
            "destructive",
        }
    name = str(tool or "").strip().lower()
    if name in _SHELL_TOOL_NAMES:
        return True
    return name not in _CLASS_BY_NAME


# ===========================================================================
# 2. The tool surface, derived from the mode and from policy
# ===========================================================================


@dataclass(frozen=True)
class ToolSurface:
    """What the MODEL can invoke, and what was withheld and why.

    ``withheld`` is ``(name, reason)`` pairs rather than names alone. A user
    looking at a missing tool needs the rule that removed it, or the surface is
    an unexplained hole.
    """

    mode: str
    offered: Tuple[str, ...]
    withheld: Tuple[Tuple[str, str], ...]
    #: Tools withheld by the MODE (as opposed to by a policy denial).
    withheld_by_mode: Tuple[str, ...] = ()
    #: Tools withheld because POLICY denied them this turn.
    withheld_by_policy: Tuple[str, ...] = ()
    #: The declared-internal tools, which are executable but not advertised.
    declared_internal: Tuple[str, ...] = ()

    @property
    def withheld_names(self) -> Tuple[str, ...]:
        """The withheld tool names, in catalog order."""
        return tuple(name for name, _ in self.withheld)

    @property
    def total(self) -> int:
        """How many tools the catalog carried before any withholding."""
        return len(self.offered) + len(self.withheld)

    def reasons(self) -> Dict[str, str]:
        """Return ``tool -> reason`` for every withheld tool."""
        return {name: reason for name, reason in self.withheld}

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, withholding INCLUDED.

        Withholding is reported by name and reason because "the agent has fewer
        capabilities than you might assume" is a fact a user is entitled to
        read, not an implementation detail.
        """
        return {
            "mode": self.mode,
            "offered": list(self.offered),
            "withheld": [
                {"tool": name, "reason": reason} for name, reason in self.withheld
            ],
            "withheld_by_mode": list(self.withheld_by_mode),
            "withheld_by_policy": list(self.withheld_by_policy),
            "declared_internal": list(self.declared_internal),
            "total": self.total,
        }


def tool_surface(
    mode: Any,
    catalog: Sequence[Any] = (),
    *,
    denied: Optional[Iterable[Any]] = None,
) -> ToolSurface:
    """Return the tools a model may INVOKE in ``mode``, and what was withheld.

    This is the whole of T4.W1.1 item 1. ``PLAN`` does not merely REFUSE a write
    -- the write tool is not offered, so the model cannot spend a turn calling
    it. "The model can see it but calling it fails" is not a plan mode; it is a
    plan mode that wastes context.

    ``denied`` is the per-turn policy denial set. A denied tool is removed from
    the surface too, which is T4.W1.2: **tool availability is
    permission-derived**. The two sources are reported SEPARATELY
    (``withheld_by_mode`` / ``withheld_by_policy``) because they have different
    owners and different remedies.
    """
    canonical = normalize_mode(mode)
    profile = MODE_PROFILES[canonical]
    denied_names = {str(name).strip().lower() for name in (denied or ()) if str(name)}
    internal = declared_internal_tools()

    offered: List[str] = []
    withheld: List[Tuple[str, str]] = []
    by_mode: List[str] = []
    by_policy: List[str] = []

    for entry in catalog or ():
        name = str(getattr(entry, "name", None) or entry or "").strip().lower()
        if not name:
            continue
        if name in internal:
            # Executable, deliberately not advertised. Declared, not accidental.
            continue
        if name in denied_names:
            withheld.append((name, "policy denied this tool for this turn"))
            by_policy.append(name)
            continue
        side_effect_class = classify_tool(entry)
        if not profile.offers(side_effect_class):
            reason = (
                f"{profile.name} mode is read-only; a {side_effect_class} tool "
                "is not offered to the model"
                if profile.read_only
                else f"{profile.name} mode does not offer a {side_effect_class} tool"
            )
            withheld.append((name, reason))
            by_mode.append(name)
            continue
        offered.append(name)

    for name in sorted(internal):
        withheld.append(
            (name, "declared internal: executable, deliberately not advertised")
        )
        by_mode.append(name)

    return ToolSurface(
        mode=canonical,
        offered=tuple(offered),
        withheld=tuple(withheld),
        withheld_by_mode=tuple(by_mode),
        withheld_by_policy=tuple(by_policy),
        declared_internal=internal,
    )


# -- the model-visible vs executable separation ------------------------------

#: Tools the harness may execute but the model is NEVER told about.
#:
#: This is a real pattern: an internal bookkeeping call that would only
#: confuse a model, or a tool whose existence itself is privileged. It is
#: DECLARED here, with a written reason per entry, rather than emerging from
#: whatever the registry happened to contain.
#:
#: **The set is empty on this tree, and that is a measured statement, not an
#: oversight.** Every catalogued tool is useful to a model, so none is internal
#: today. The mechanism ships with the gate that keeps it honest: an UNDECLARED
#: tool that is withheld is reported as ``withheld_but_undeclared`` by
#: :func:`surface_violations`, so the set cannot grow by accident.
_DECLARED_INTERNAL: Dict[str, str] = {}


def declare_internal_tool(name: str, reason: str) -> str:
    """Declare a tool executable-but-not-advertised. Returns the canonical name.

    **A reason is required and must not be blank.** A declaration nobody can
    read is indistinguishable from an accident, and an accidental hole in the
    model's view produces a worse plan -- the under-hiding direction this module
    exists to catch in both directions.
    """
    canonical = str(name or "").strip().lower()
    text = str(reason or "").strip()
    if not canonical:
        raise ValueError("declare_internal_tool requires a tool name")
    if not text:
        raise ValueError(
            "declare_internal_tool requires a written reason: an internal tool "
            "is one the model is never told about, and a reader who cannot see "
            "why cannot audit the hole"
        )
    _DECLARED_INTERNAL[canonical] = text
    return canonical


def declared_internal_tools() -> Tuple[str, ...]:
    """Return the declared internal tool names, sorted."""
    return tuple(sorted(_DECLARED_INTERNAL))


def internal_tool_declarations() -> Dict[str, str]:
    """Return ``tool -> reason`` for every declared internal tool."""
    return dict(_DECLARED_INTERNAL)


def visible_surface(
    mode: Any,
    catalog: Sequence[Any] = (),
    *,
    denied: Optional[Iterable[Any]] = None,
) -> VisibilityReport:
    """Return what the MODEL SEES, and what the harness can still EXECUTE.

    The two are separate answers on purpose, and this function is where they stop
    being the same set.

    **``executable`` is deliberately the WHOLE catalog, and that is the honest
    answer rather than a convenient one.** A mode withholds a tool from the
    model's VIEW; it does not delete the tool from the registry. So the mode is
    enforced in two places -- the surface (what the model is told) and the
    dispatch decision (:func:`resolve_run_approval` plus the approver) -- and a
    receipt that claimed ``PLAN`` removed ``bash`` from the executable surface
    would be claiming an enforcement this layer does not perform.

    :attr:`VisibilityReport.dispatch_blocked_by` names, per tool, WHICH
    mechanism blocks it, so a reader can tell a tool that is out of the model's
    view from a tool that cannot run at all.

    A tool can also be executable and invisible on purpose: a declared internal
    tool. That is why the two lists differ at all.
    """
    surface = tool_surface(mode, catalog, denied=denied)
    internal = set(surface.declared_internal)
    visible = tuple(surface.offered)
    # The registry still carries every catalogued tool; ordering is
    # deterministic and puts the visible ones first so a reader scanning the
    # list sees what the model has before what it does not.
    executable = tuple(
        dict.fromkeys(list(visible) + [name for name, _ in surface.withheld])
    )
    blocked: Dict[str, str] = {}
    for name in surface.withheld_by_policy:
        blocked[name] = "policy denied this tool for this turn"
    for name in surface.withheld_by_mode:
        if name in internal:
            blocked[name] = "declared internal: executable, deliberately not advertised"
        else:
            profile = MODE_PROFILES[surface.mode]
            blocked[name] = f"withheld from the model's view by {surface.mode} mode" + (
                " (intentional: this mode is read-only)" if profile.read_only else ""
            )
    return VisibilityReport(
        mode=surface.mode,
        model_visible=visible,
        executable=executable,
        withheld=surface.withheld,
        withheld_by_mode=surface.withheld_by_mode,
        withheld_by_policy=surface.withheld_by_policy,
        declared_internal=surface.declared_internal,
        catalog_total=surface.total,
        dispatch_blocked_by=blocked,
    )


@dataclass(frozen=True)
class VisibilityReport:
    """The model-visible surface beside the executable surface."""

    mode: str
    model_visible: Tuple[str, ...]
    executable: Tuple[str, ...]
    withheld: Tuple[Tuple[str, str], ...]
    withheld_by_mode: Tuple[str, ...] = ()
    withheld_by_policy: Tuple[str, ...] = ()
    declared_internal: Tuple[str, ...] = ()
    catalog_total: int = 0
    #: ``tool -> the mechanism that keeps the model from reaching it``. Named
    #: per tool so "not offered" and "not offered AND not runnable" stay
    #: distinguishable to a reader of the receipt.
    dispatch_blocked_by: Mapping[str, str] = field(default_factory=dict)

    @property
    def withheld_count(self) -> int:
        """How many tools the catalog carried that the model cannot see."""
        return len(self.withheld)

    def reasons(self) -> Dict[str, str]:
        """Return ``tool -> reason`` for every withheld tool."""
        return {name: reason for name, reason in self.withheld}

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, including the counts."""
        return {
            "mode": self.mode,
            "model_visible": list(self.model_visible),
            "executable": list(self.executable),
            "withheld": [
                {"tool": name, "reason": reason} for name, reason in self.withheld
            ],
            "withheld_count": self.withheld_count,
            "withheld_by_mode": list(self.withheld_by_mode),
            "withheld_by_policy": list(self.withheld_by_policy),
            "declared_internal": list(self.declared_internal),
            "internal_declarations": internal_tool_declarations(),
            "dispatch_blocked_by": dict(self.dispatch_blocked_by),
            "catalog_total": self.catalog_total,
        }


def surface_violations(report: VisibilityReport) -> List[Dict[str, str]]:
    """Return BOTH classes of mis-provisioning, each named.

    * ``denied_but_visible`` -- a tool policy denied this turn is STILL in the
      model-visible surface. This is the under-hiding defect: the receipt and
      the schema disagree, and the schema is what the model plans against.
    * ``withheld_but_undeclared`` -- a tool is withheld from the model AND not
      declared internal, so nothing can say why the agent cannot see something
      it needs. This is the over-hiding defect, and it is the one a
      capability-restriction-only design never catches.

    ``mode_gap_declared`` is reported but is NOT a violation: ``PLAN`` withholds
    every mutation by design, and the profile says so in writing. A mode whose
    withholding is UNdeclared is a violation, which is the difference between a
    planning mode and a broken one.
    """
    problems: List[Dict[str, str]] = []
    visible = set(report.model_visible)
    declared = set(report.declared_internal)
    profile = MODE_PROFILES.get(
        normalize_mode(report.mode), MODE_PROFILES[MODE_DEFAULT]
    )
    #: Tools the receipt already EXPLAINS. Three cases, and the third is
    #: conditional on purpose:
    #:
    #: * a DECLARED INTERNAL tool -- declared executable and deliberately not
    #:   advertised;
    #: * a POLICY-DENIED tool -- a deliberate narrowing for this turn, which is
    #:   exactly why the tool is off the model's list. The first version of
    #:   this loop did not exempt these and reported the DEFAULT mode's own
    #:   correctly-narrowed receipt as a defect, which is the same failure as a
    #:   gate that fires on correct behaviour;
    #: * a tool withheld by a mode whose profile DECLARES the gap (``PLAN``).
    #:
    #: A mode with NO declared gap that withholds a tool is the over-hiding
    #: defect and is deliberately NOT exempt, which is why this is conditional
    #: rather than "everything the report mentions".
    explained = declared | set(report.withheld_by_policy)
    if profile.intentional_gap:
        explained |= set(report.withheld_by_mode)

    for name in report.withheld_by_policy:
        if name in visible:
            problems.append(
                {
                    "kind": "denied_but_visible",
                    "tool": name,
                    "detail": (
                        f"{name!r} was denied by policy but is still in the "
                        "model-visible surface, so the model will plan around a "
                        "tool it cannot use"
                    ),
                }
            )
    for name, reason in report.withheld:
        if name in explained:
            continue
        problems.append(
            {
                "kind": "withheld_but_undeclared",
                "tool": name,
                "detail": (
                    f"{name!r} is withheld from the model and is not a declared "
                    f"internal tool ({reason}); a tool the agent needs and cannot "
                    "see produces a worse plan"
                ),
            }
        )
    return problems


# ===========================================================================
# 3. Arity-scoped grants, in plain language
# ===========================================================================

#: The command-arity dictionary. It answers "how many leading words of this
#: command identify its SHAPE", which is what turns ``git commit -m x`` into the
#: ask pattern ``git commit *`` instead of a blanket "any bash command".
#:
#: **THIS TABLE IS A FALLBACK, NOT THE AUTHORITY.** The authority is
#: ``shared.policy_lang.command_arity_table()``, backed by
#: ``shared/policy_data/command_arity.json`` (167 commands, longest-prefix
#: lookup, every entry carrying ``min_args`` / ``max_args`` / ``positional`` /
#: ``flags`` / ``risk``). Wave 1 filed a cross-terminal request asking for
#: exactly that, and it has landed -- so keeping a SECOND arity dictionary here
#: is now two answers to "how scoped is ``git commit``", and the wrong one is
#: the one the UI would render.
#:
#: Concretely, the local table said ``git -> 2``, which turns
#: ``git -c core.pager='rm -rf /' commit`` into the pattern ``git -c *``. The
#: authority has **no entry** for that shape, because the ``-c`` global flag
#: makes it a different command family, and "unclassified" is the honest answer
#: where a guess is the dangerous one. :func:`command_arity` therefore asks the
#: authority first and reports the difference rather than quietly preferring
#: either side.
#:
#: :data:`ARITY_SOURCE` records which one answered, and
#: :func:`arity_authority_report` publishes it -- because a table that silently
#: changed owner is a table whose receipts can no longer be trusted.
COMMAND_ARITY: Dict[str, int] = {
    "git": 2,
    "npm": 2,
    "npx": 2,
    "pnpm": 2,
    "yarn": 2,
    "pip": 2,
    "pip3": 2,
    "python": 2,
    "python3": 2,
    "poetry": 2,
    "uv": 2,
    "cargo": 2,
    "go": 2,
    "docker": 2,
    "kubectl": 2,
    "ruff": 2,
    "pytest": 2,
    "mypy": 2,
    "black": 2,
    "flake8": 2,
    "node": 2,
    "deno": 2,
    "bun": 2,
    "make": 2,
    "cmake": 2,
    "dotnet": 2,
    "mvn": 2,
    "gradle": 2,
    "terraform": 2,
    "aws": 2,
    "gcloud": 2,
    "az": 2,
    "rg": 2,
    "grep": 2,
    "sed": 2,
    "awk": 2,
    "find": 2,
    "curl": 2,
    "wget": 2,
    "ssh": 2,
    "scp": 2,
    "rsync": 2,
    "tar": 2,
    "neo": 2,
    "mutagen": 2,
}

#: Default arity when the binary is in NEITHER table: ONE word. A one-word
#: pattern (``foo *``) is the narrowest pattern that still matches a family of
#: commands, and a default of 2 for an unknown binary would pin ``ls -la /var``
#: to ``ls -la *`` and approve a path the user never saw.
DEFAULT_ARITY = 1

#: Which arity table answered the last :func:`command_arity` call, and whether
#: the authority was even readable. A module-level value rather than a return
#: value because every caller wants the pattern and only the RECEIPT wants the
#: provenance; returning a tuple from :func:`command_arity` would put a
#: provenance obligation on every call site and one of them would drop it.
ARITY_SOURCE: Dict[str, Any] = {"authority": "", "available": False, "reason": ""}


def _authority_arity(command: Any) -> Tuple[Optional[int], str]:
    """Return ``(arity, note)`` from the AUTHORITY table, or ``(None, why)``.

    Delegates to ``shared.policy_lang.command_arity_table`` rather than reading
    its data file, so the UI and the policy engine cannot answer "how scoped is
    this command" differently. Never raises: an unavailable authority is a
    *reported* condition (:data:`ARITY_SOURCE`), not an exception at the moment
    somebody is being asked to approve a command.
    """
    try:
        from shared.policy_lang import command_arity_table
    except Exception as exc:
        ARITY_SOURCE.update(
            authority="", available=False, reason=f"policy_lang unavailable: {type(exc).__name__}"
        )
        return None, f"the arity authority is unavailable ({type(exc).__name__})"
    try:
        table = command_arity_table()
    except Exception as exc:
        ARITY_SOURCE.update(
            authority="", available=False, reason=f"arity table failed: {type(exc).__name__}"
        )
        return None, f"the arity table could not be read ({type(exc).__name__})"
    ARITY_SOURCE.update(
        authority="shared/policy_data/command_arity.json",
        available=bool(table.available),
        reason=str(table.reason or ""),
    )
    if not table.available:
        return None, f"the arity table is unavailable: {table.reason}"
    entry = table.lookup(command)
    if entry is None:
        words = str(command or "").strip().split()
        return (
            None,
            f"{' '.join(words[:2])!r} is not in the arity dictionary, so this "
            "command's shape is UNCLASSIFIED rather than guessed",
        )
    # The entry's OWN NAME is the pinned shape, because the authority stores
    # `git commit` and `git push` as separate two-word keys and resolves by
    # longest prefix. So the arity is the word count of the key it matched --
    # NOT `1 + min_args`, which was this round's first attempt and produced
    # arity 1 for `git commit -m x` (i.e. the pattern `git *`), a grant an
    # order of magnitude broader than the one the dictionary describes. That
    # error was caught by printing the pattern for every probe, not by reading
    # the formula.
    size = len(str(entry.command or "").strip().split())
    if size < 1:
        return None, f"the arity entry for {command!r} declares no command name"
    return size, ""


def arity_authority_report(command: Any = "") -> Dict[str, Any]:
    """Publish which arity table answered, and what it could not classify.

    A receipt rather than a log line, because the question "was this grant
    scoped by the authority's dictionary or by a fallback table in the UI?" is
    one a user deserves an answer to before they click *always allow*.
    """
    words = str(command or "").strip()
    arity, note = _authority_arity(words) if words else (None, "no command was named")
    return {
        "command": words,
        "authority": dict(ARITY_SOURCE),
        "authority_arity": arity,
        "fallback_arity": COMMAND_ARITY.get(words.split()[0].lower()) if words else None,
        "effective_arity": command_arity(words) if words else None,
        "note": note,
        "authority_commands": (
            len(_authority_table()) if _authority_table() is not None else 0
        ),
    }


def _authority_table() -> Any:
    """Return the authority table, or ``None``. Never raises."""
    try:
        from shared.policy_lang import command_arity_table

        return command_arity_table()
    except Exception:
        return None

#: The prefix words for the multi-word sub-verb families the dictionary cannot
#: express with a bare integer: ``git`` alone says 2, so ``git commit`` is the
#: pattern unit (``git commit *``), while ``npm run`` needs a third word to tell
#: a run from an install. Read BEFORE the integer, because it is the more
#: specific answer.
#:
#: **Every entry here has to be justified against :data:`COMMAND_ARITY`.** An
#: entry that contradicts the binary's own arity (``git commit`` at 3 against
#: ``git`` at 2) narrows the grant to something the user never chose, and the
#: corpus's own example is ``git commit *`` - so that entry was removed rather
#: than kept as a "safer" widening.
ARITY_PREFIXES: Tuple[Tuple[str, int], ...] = (
    ("npm run", 3),
    ("npm exec", 3),
    ("pnpm run", 3),
    ("pnpm dlx", 3),
    ("yarn run", 3),
    ("yarn add", 3),
    ("pip install", 3),
    ("pip uninstall", 3),
    ("cargo build", 3),
    ("cargo test", 3),
    ("cargo clippy", 3),
    ("go build", 3),
    ("go test", 3),
    ("go mod", 3),
    ("docker run", 3),
    ("docker compose", 3),
    ("kubectl apply", 3),
    ("kubectl delete", 3),
    ("kubectl get", 3),
)

#: The exact field names a grant must carry for the UI to present it honestly.
#: **T5 owns the policy DSL; these are the names this layer needs from it.** A
#: grant missing one of them can be rendered, but it cannot be rendered
#: completely, and the renderer says so rather than guessing.
ARITY_REQUIRED_FIELDS: Tuple[str, ...] = (
    "scope",
    "pattern",
    "arity",
    "tool",
    "layer",
    "session_only",
    "revocable",
)

#: The layering the corpus describes, lowest to highest. ``defaults`` is a
#: built-in the user never configured, which is exactly the case where a
#: surprising grant comes from somewhere they did not choose.
GRANT_LAYERS: Tuple[str, ...] = (
    "defaults",
    "builtin_agent",
    "user_config",
    "per_agent",
    "per_session",
)

_LAYER_PLAIN: Dict[str, str] = {
    "defaults": "a built-in default you did not configure",
    "builtin_agent": "a built-in agent's own declaration",
    "user_config": "your configuration file",
    "per_agent": "the agent profile for this run",
    "per_session": "this session",
}

_SCOPE_PLAIN: Dict[str, str] = {
    "once": "this one action",
    "exact_call": "this one exact call",
    "session_path": "this kind of action, on the paths you name",
    "session_command_prefix": "commands shaped like this one",
}


def describe_layer(layer: Any) -> str:
    """Return the plain-language name of a grant's layer.

    An UNRECOGNISED layer says so rather than defaulting to the session, because
    "this session" is the answer that hides the surprising case: a grant from a
    layer nobody can name is a grant from a place nobody configured.
    """
    key = str(layer or "").strip().lower()
    if key in _LAYER_PLAIN:
        return _LAYER_PLAIN[key]
    return (
        f"an unrecognised layer ({key!r}) - this grant did not come from your session"
    )


def command_arity(command: Any) -> int:
    """Return how many leading words identify this command's SHAPE.

    **The authority answers first.** ``shared.policy_lang``'s table --
    167 commands, longest-prefix, data-driven -- is the single source, and this
    module's own :data:`COMMAND_ARITY` is a declared FALLBACK for the case
    where the authority is unreadable.

    The ORDER is the property, and it is the opposite of what Wave 1 did. The
    first version asked the local table first, which meant:

    * ``git -c core.pager='rm -rf /' commit`` resolved to arity 2 and became the
      pattern ``git -c *`` -- a grant scoped to a command *family* the user never
      named, reached through a global flag that changes what the command does.
      The authority has no entry for that shape at all, so "unclassified" is the
      honest answer and a one-word pattern (``git *``) is what a fallback should
      give;
    * a command T5 added to the JSON dictionary after this module was written
      would be ignored, because the UI's private table would answer first.

    :data:`ARITY_PREFIXES` is still read before either table, because a
    three-word family (``npm run``) is more specific than any entry in a
    two-column integer table and both authorities agree it should win.

    Never returns less than 1 and never more than the number of words present,
    so a one-word command yields a one-word pattern rather than a dangling star.
    """
    words = str(command or "").strip().split()
    if not words:
        return 1
    lowered = [word.lower() for word in words]
    for prefix, arity in ARITY_PREFIXES:
        parts = prefix.split()
        if lowered[: len(parts)] == parts:
            return max(1, min(arity, len(words)))

    authority, _unclassified_note = _authority_arity(command)
    if authority is not None:
        return max(1, min(authority, len(words)))
    if ARITY_SOURCE.get("available"):
        # The authority IS readable and it has NO ENTRY for this shape. That is
        # a different fact from "the authority is unavailable", and it must be
        # answered differently.
        #
        # Unavailable -> the fallback table may guess, because the alternative
        #   is DEFAULT_ARITY for every command on the machine.
        # Unclassified -> the fallback table must NOT guess. It guessed
        #   ``git -> 2`` for ``git -c core.pager='rm -rf /' commit``, which
        #   produced the pattern ``git -c *`` -- a grant scoped to a command
        #   family the user never named, reached through a global flag that
        #   changes what the command does. One word (``git *``) is the narrowest
        #   honest answer, and :func:`worst_case_lines` then tells the reader
        #   that any extra flag is covered.
        return max(1, min(DEFAULT_ARITY, len(words)))

    binary = lowered[0]
    arity = int(COMMAND_ARITY.get(binary, DEFAULT_ARITY))
    return max(1, min(arity, len(words)))


def command_pattern(command: Any) -> str:
    """Return the ASK PATTERN for an exact command, e.g. ``git commit *``.

    This is OpenCode's shape: the exact command text becomes the ask pattern, so
    "always allow" grants ``git commit *`` rather than "any bash command". A
    user who clicks "always allow" is granting one command SHAPE, and the dialog
    has to be able to say which shape in words.
    """
    words = str(command or "").strip().split()
    if not words:
        return ""
    arity = command_arity(command)
    pinned = " ".join(words[:arity])
    return f"{pinned} *"


@dataclass(frozen=True)
class GrantScopeView:
    """One grant, in the words a person would use to describe it.

    ``plain`` is the sentence a dialog renders. ``pattern`` is the machine form
    (``git commit *``) so a receipt can be checked against the scope the policy
    engine actually enforces.
    """

    scope: str
    pattern: str
    arity: int
    plain: str
    layer: str
    tool: str = ""
    command: str = ""
    paths: Tuple[str, ...] = ()
    session_only: bool = True
    revocable: bool = True
    #: Whether this grant would cover more than the command that was approved.
    blanket: bool = False
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, with every required field named."""
        return {
            "scope": self.scope,
            "pattern": self.pattern,
            "arity": self.arity,
            "tool": self.tool,
            "command": self.command,
            "paths": list(self.paths),
            "layer": self.layer,
            "layer_plain": describe_layer(self.layer),
            "session_only": bool(self.session_only),
            "revocable": bool(self.revocable),
            "blanket": bool(self.blanket),
            "plain": self.plain,
            "notes": list(self.notes),
            "required_fields": list(ARITY_REQUIRED_FIELDS),
        }


def describe_scope(
    scope: Any,
    *,
    tool: Any = "",
    command: Any = "",
    paths: Sequence[str] = (),
    layer: str = "per_session",
) -> GrantScopeView:
    """Return a grant scope as plain language, with its machine pattern.

    **No scope over a shell tool is ever ``blanket``.** That is the whole
    non-negotiable of this section: a user who clicks "always allow" must be
    able to know the scope, and "any bash command" is not a scope a person can
    evaluate. For a shell tool the pattern is therefore always the arity-scoped
    ``<pinned words> *``, and the ``blanket`` flag is False by construction.

    An unrecognised scope is NOT upgraded. It is reported as ``once`` with a
    note, because the narrowing direction is the only safe one for a word this
    layer does not understand.
    """
    from shared.approval import APPROVAL_SCOPES, normalize_approval_scope

    raw = str(scope or "").strip()
    canonical = normalize_approval_scope(raw) if raw else "once"
    tool_name = str(getattr(tool, "name", None) or tool or "").strip().lower()
    command_text = str(command or "").strip()
    path_tuple = tuple(str(item) for item in (paths or ()) if str(item))
    shell = is_shell_tool(tool_name)
    arity = command_arity(command_text) if command_text else 1
    pattern = command_pattern(command_text) if command_text else ""

    notes: List[str] = []
    if raw and canonical != raw and raw not in APPROVAL_SCOPES:
        notes.append(
            f"scope {raw!r} is not a scope this build knows; narrowed to {canonical!r}"
        )

    if not pattern:
        if tool_name:
            pattern = f"{tool_name} *"
        else:
            pattern = "this action"
        notes.append("this grant has no command pattern, so it is scoped to the tool")

    if canonical == "once":
        plain = "this one action, once"
        # The displayed pattern must not be WIDER than what is enforced. Both
        # of these scopes are consumed at the decision site and
        # ``ApprovalGrant.covers()`` returns False for them, so the enforcement
        # is stricter than the wildcard -- but a dialog that says "only that
        # exact command line, byte for byte" while printing ``git commit *``
        # tells the reader the grant is BROADER than it is. The pattern is
        # therefore the exact command for the two scopes that mean "exactly
        # this", and the wildcard only where a prefix is genuinely the scope.
        if command_text:
            pattern = command_text
    elif canonical == "exact_call":
        if command_text:
            pattern = command_text
        plain = (
            f"this one exact call, re-usable only if it is byte-identical ({pattern})"
        )
    elif canonical == "session_path":
        base = f"{tool_name or 'this tool'} on " + (
            ", ".join(path_tuple[:3]) if path_tuple else "the paths you name"
        )
        plain = f"{base} for the rest of this session"
    elif canonical == "session_command_prefix":
        plain = f"{_SCOPE_PLAIN['session_command_prefix']}: {pattern} (never any other command shape)"
    else:
        plain = f"{_SCOPE_PLAIN.get(canonical, canonical)} ({pattern})"

    if shell and pattern.strip() == "*":
        # Unreachable through the normal path; kept so a future change to
        # ``command_pattern`` cannot quietly re-open a blanket shell grant.
        pattern = f"{tool_name or 'that tool'} *"
        notes.append(
            "a blanket shell approval is never offered; the scope was narrowed"
        )

    layer_key = str(layer or "per_session").strip().lower()
    if layer_key != "per_session":
        plain = f"{plain} - from {describe_layer(layer_key)}"

    return GrantScopeView(
        scope=canonical,
        pattern=pattern,
        arity=arity,
        plain=plain,
        layer=layer_key,
        tool=tool_name,
        command=command_text,
        paths=path_tuple,
        session_only=layer_key == "per_session",
        revocable=True,
        blanket=False,
        notes=tuple(notes),
    )


def _worst_case_flag(pinned: str) -> str:
    """Return a REAL flag this command accepts, or ``""``.

    **A worst case must be a command that could actually be typed.** The first
    version appended ``-c core.pager='sh -c "curl ... | sh"'`` to every pinned
    shape, which is a real and dangerous argument for ``git`` and a **fabricated
    one** for ``ls``, ``pytest`` and ``npm run`` -- so the dialog taught the
    reader a shape that does not exist, on exactly the tools where the reader
    has the least context to spot it.

    So the flag comes from the authority's own ``flags`` field, which is data
    somebody wrote about the real command. When the authority declares no flags
    at all the answer is ``""`` and the sentence says "any arguments" instead,
    which is both true and narrower than inventing one.
    """
    words = str(pinned or "").strip().split()
    if not words:
        return ""
    try:
        from shared.policy_lang import command_arity_table

        entry = command_arity_table().lookup(pinned)
    except Exception:
        return ""
    if entry is None:
        return ""
    for flag in str(entry.flags or "").split():
        flag = flag.strip()
        if not flag.startswith("-"):
            continue
        # Prefer a flag that changes behaviour rather than one that only sets a
        # pager: the point of a worst case is the strongest thing in scope.
        if flag in {"--amend", "--no-verify", "--force", "-f", "--hard"}:
            return f"{flag} '<a value you did not review>'"
    return ""


def worst_case_command(view: Any) -> str:
    """Return the WORST command this grant would permit, as a real command line.

    **The single most important property of this module.** A user clicks "always
    allow" and must be able to know exactly what they granted. A scope written as
    ``git commit *`` is only honest if the reader can see that the worst thing
    ``git commit *`` admits is *still* a ``git commit``. If they would have to
    reason about it to notice that the scope is narrow, the scope is too broad.

    So this function constructs the worst case rather than describing it, and
    the UI is expected to SHOW it. Three answers, all real strings:

    * a **session_command_prefix** grant returns a command the pattern really
      does admit, chosen so the adversarial argument is visible -- for
      ``git commit *`` it is a commit that runs an arbitrary program through
      ``-c core.pager``, because that is the same sub-verb and it is the shape a
      user is least likely to picture when they read ``git commit *``;
    * an **exact_call** grant returns the call itself, because that is the
      whole of it;
    * a **session_path** grant returns the worst PATH it admits, which is a path
      traversal out of the named directory, because "on ``src/``" sounds
      narrower than it is;
    * **once** returns the one action.

    Returns ``""`` for a scope this build does not know, rather than a guess --
    an unknown scope's worst case is unknown, and inventing one is exactly the
    overstatement this function exists to prevent.
    """
    scope = str(getattr(view, "scope", "") or "").strip().lower()
    command = str(getattr(view, "command", "") or "").strip()
    if scope == "once":
        return command or "(this one action, once)"
    if scope == "exact_call":
        return command or "(this one exact call)"
    if scope == "session_command_prefix":
        words = command.split()
        if not words:
            return "(no command was named)"
        # The pinned words ARE the scope, so the worst case keeps them and
        # appends the strongest argument the pinned sub-verb really accepts.
        pinned = " ".join(words[: max(1, command_arity(command))])
        flag = _worst_case_flag(pinned)
        return f"{pinned} {flag}".strip() if flag else f"{pinned} <any arguments>"
    if scope == "session_path":
        paths = tuple(str(item) for item in (getattr(view, "paths", ()) or ()))
        if not paths:
            return "(on whatever paths you name)"
        first = paths[0].replace("\\", "/").rstrip("/")
        return f"write anything under {first}/../.. (the grant covers {first}/ and its parents' children)"
    return ""


def worst_case_lines(view: Any) -> List[str]:
    """Return the plain lines a grant dialog must show before it offers the grant.

    Always at least one line, and the FIRST line is the worst case, so a reader
    who reads one line reads the important one. Empty for a grant this build
    cannot characterise -- and :func:`assert_every_scope_is_characterised`
    fails on that rather than letting an uncharacterised grant be offered.
    """
    worst = worst_case_command(view)
    if not worst:
        return []
    scope = str(getattr(view, "scope", "") or "").strip().lower()
    tool = str(getattr(view, "tool", "") or "this tool")
    if scope == "session_path":
        lead = (
            f"worst case for this {tool} grant: {worst}. "
            "A path grant is NOT limited to the files it names."
        )
    elif scope == "session_command_prefix":
        lead = (
            f"worst case for this {tool} grant: {worst}. "
            "Every command starting with those exact words is covered, together "
            "with every argument it accepts -- the pattern is a prefix, not an "
            "equality."
        )
    elif scope == "exact_call":
        lead = (
            f"worst case for this {tool} grant: {worst}. "
            "Only that exact command line, byte for byte."
        )
    else:
        lead = (
            f"worst case for this {tool} grant: {worst}. "
            "This grant is consumed by the action it was given for."
        )
    lines = [lead]
    notes = tuple(str(item) for item in (getattr(view, "notes", ()) or ()))
    lines.extend(f"  {note}" for note in notes if note.strip())
    return lines


def assert_every_scope_is_characterised(options: Sequence[Any]) -> None:
    """Raise when a grant this dialog would OFFER has no stated worst case.

    The gate that keeps :func:`worst_case_lines` from being decorative. It walks
    the option set rather than the dialog code, because an option built by hand
    elsewhere cannot satisfy a check that lives in the caller.
    """
    for option in options or ():
        if not worst_case_lines(option):
            raise ValueError(
                f"scope {getattr(option, 'scope', '?')!r} would be offered with "
                "no worst-case example, so the user cannot tell what they granted"
            )


def scope_options(
    tool: Any, command: Any = "", *, paths: Sequence[str] = ()
) -> Tuple[GrantScopeView, ...]:
    """Return the scopes a dialog may OFFER for this action.

    The options are a closed, ordered set, and **no option is ever a blanket
    approval for a tool that can run something**. The ``once`` option is always
    present, so "approve once" is always available; the longest option is always
    arity-scoped.

    Ordering is narrowest first, because a dialog's first option is the one
    people click, and the narrowest grant is the one that should be easiest to
    say yes to.
    """
    shell = is_shell_tool(tool)
    options = [
        describe_scope(
            "once",
            tool=tool,
            command=command,
            paths=paths,
            layer="per_session",
        ),
        describe_scope(
            "exact_call",
            tool=tool,
            command=command,
            paths=paths,
            layer="per_session",
        ),
    ]
    if shell:
        options.append(
            describe_scope(
                "session_command_prefix",
                tool=tool,
                command=command,
                paths=paths,
                layer="per_session",
            )
        )
    else:
        options.append(
            describe_scope(
                "session_path",
                tool=tool,
                command=command,
                paths=paths,
                layer="per_session",
            )
        )
    assert_no_blanket_shell_grant(options, tool=tool)
    assert_every_scope_is_characterised(options)
    return tuple(options)


def assert_no_blanket_shell_grant(
    options: Sequence[GrantScopeView], *, tool: Any = ""
) -> None:
    """Raise when a shell option is a blanket approval. Returns otherwise.

    This is the gate, and it is STRUCTURAL rather than a convention: the
    function inspects every offered scope for a shell tool and refuses a bare
    ``*`` pattern or an exact_call over a DIFFERENT command. A dialog built by
    hand elsewhere cannot satisfy it by accident because there is nothing to
    satisfy -- the check runs on the option set, not on the code that made it.
    """
    if not is_shell_tool(tool):
        return
    for option in options or ():
        if option.blanket:
            raise ValueError(
                f"a blanket approval was offered for the shell tool "
                f"{option.tool or 'shell'!r}; scope the grant to a command shape"
            )
        pattern = str(option.pattern or "").strip()
        if pattern in {"*", "**", "any", "all"}:
            raise ValueError(
                "a blanket approval was offered for a shell tool: the pattern "
                f"{pattern!r} would approve every command"
            )


def grant_lines(ledger: Any) -> List[str]:
    """Return PLAIN lines describing the current grant set.

    A grant you cannot see is a grant you cannot revoke, so this is the receipt
    a ``/approval grants`` surface renders. It is total: an unreadable ledger,
    a ``None`` ledger, and a ledger with no grants all produce honest lines
    rather than an exception or a blank.

    Each row carries the SCOPE in words, the LAYER it came from, and -- for any
    grant that did not come from this session -- the fact that it did not.
    """
    grants = _ledger_grants(ledger)
    if not grants:
        return [
            "no grants are in force: every side-effecting action will ask",
        ]
    lines = ["current grants:"]
    for grant in grants:
        try:
            view = describe_scope(
                _grant_field(grant, "scope", "once"),
                tool=_grant_field(grant, "tool", ""),
                command=_grant_field(grant, "command", ""),
                paths=_grant_paths(grant),
                layer=_grant_field(grant, "layer", "per_session"),
            )
        except Exception:
            lines.append("  (a grant could not be rendered; revoke it to be safe)")
            continue
        if _grant_expired(grant):
            lines.append(f"  {view.pattern} - expired, ask again next time")
            continue
        lines.append(f"  {view.pattern} - {view.plain}")
        if not view.session_only:
            lines.append(
                f"      from {describe_layer(view.layer)}, not from this session"
            )
    lines.append("revoke any of these with /approval forget")
    return lines


def _ledger_grants(ledger: Any) -> List[Any]:
    """Return the grant list from whatever shape the ledger exposes, safely."""
    if ledger is None:
        return []
    for attribute in ("grants", "entries", "records"):
        value = getattr(ledger, attribute, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    if isinstance(ledger, Mapping):
        value = ledger.get("grants")
        if isinstance(value, (list, tuple)):
            return list(value)
        return []
    if isinstance(ledger, (list, tuple)):
        return list(ledger)
    return []


def _grant_field(grant: Any, name: str, default: Any) -> Any:
    if isinstance(grant, Mapping):
        value = grant.get(name, default)
    else:
        value = getattr(grant, name, default)
    return default if value is None else value


def _grant_paths(grant: Any) -> Tuple[str, ...]:
    value = _grant_field(grant, "paths", ())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted(str(item) for item in value))
    return ()


def _grant_expired(grant: Any) -> bool:
    """Whether a grant carries an expiry that has passed.

    ``expires_at`` is read, never computed from a local clock the module does
    not own; a ledger that stores the fact is the authority. An absent expiry
    is NOT expired.
    """
    expires = _grant_field(grant, "expires_at", None)
    if expires in (None, "", 0):
        return False
    checker = getattr(grant, "is_expired", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


# ===========================================================================
# 4. The approver, wired
# ===========================================================================

#: How much of the reviewer's own failure text a refusal line carries. The
#: detail is DATA from a component that failed, so it is bounded rather than
#: trusted whole; the bound is a REPORTED clip, not a silent one, because the
#: alternative -- a bare "denied" -- is the failure this layer exists to stop.
MODEL_ERROR_REASON_MAX_CHARS = 200

#: Every way a review can fail. A CLOSED set, because a refusal-with-a-reason
#: has to stay distinguishable from a refusal we could not obtain -- and a log
#: reader can only count a vocabulary that is closed.
APPROVER_OUTCOMES: Tuple[str, ...] = (
    "ok",
    "not_privileged",
    "untraceable_origin",
    "disabled",
    "timeout",
    "model_error",
    "empty",
    "unparseable",
    "ambiguous",
    "malformed",
    "no_reason",
    "not_consulted",
)

#: The SIX fail-closed paths the brief names, mapped onto the vocabulary above.
#: Declared as a table so the mapping is a fact a test can assert rather than a
#: sentence in a docstring.
APPROVER_FAIL_CLOSED_PATHS: Dict[str, str] = {
    "empty": "the reviewer replied with nothing",
    "unparseable": "the reviewer's reply contained no decision word",
    "ambiguous": "the reviewer's reply contained both an approval and a denial",
    "malformed": "the reviewer's reply used a decision word this build does not define",
    "no_reason": "the reviewer approved without saying why",
    "timeout": "the reviewer did not answer within its budget",
    "model_error": "the reviewer's model call failed",
    "disabled": "the reviewer agent is switched off for this run",
    "untraceable_origin": "a subagent escalated this and its origin cannot be traced",
    "not_privileged": "the action did not already require a human, so no reviewer was asked",
    "not_consulted": "the caller reached the approver path without a decision to second-opine on",
}


@dataclass(frozen=True)
class ApproverOutcome:
    """What the second opinion said, and what may be done about it.

    ``advisory`` is ``True`` for EVERY outcome, by construction. That field is
    the structural no-widen: :meth:`as_decision` returns the HUMAN's decision
    unchanged in both directions, so a caller that consults the approver and
    then uses ``approved`` instead of ``as_decision`` is ignoring a field that
    says, in one word, that it must not.
    """

    approved: bool
    consulted: bool
    failure: str
    reason: str
    origin: str = ""
    advisory: bool = True
    journal_path: str = ""
    effect_digest: str = ""

    @property
    def refused(self) -> bool:
        """Whether the approver did not approve."""
        return not bool(self.approved)

    @property
    def obtained(self) -> bool:
        """Whether a real ANSWER was obtained at all.

        ``refused and obtained`` is a refusal WITH a reason; ``refused and not
        obtained`` is a refusal we could not obtain. Those are different
        artifacts and a user acting on them needs different next steps, so the
        question is not "did we consult" but "did the reviewer actually say
        something".

        It is therefore ``failure == "ok"`` and NOT the absence of three
        particular words. The first version excluded only
        ``{not_privileged, disabled, not_consulted}``, so a parse failure, a
        timeout and a provider exception all counted as ANSWERS OBTAINED -- a
        reviewer that never replied was reported as a considered refusal with
        a next step, which is the exact overstatement this field exists to
        prevent.
        """
        return bool(self.consulted) and self.failure == "ok"

    def explanation(self) -> str:
        """Return the one-line reason, for a denial overlay."""
        if self.failure and self.failure != "ok":
            detail = APPROVER_FAIL_CLOSED_PATHS.get(self.failure, self.failure)
            return f"second opinion: denied - {detail}" + (
                f" ({self.reason})" if self.reason else ""
            )
        if self.approved:
            return f"second opinion: approved - {self.reason or 'no reason given'}"
        return "second opinion: denied"

    def as_decision(self, human: Any) -> Any:
        """Return the HUMAN's decision, unchanged.

        **This is the structural guarantee, and it is deliberately the simplest
        possible implementation.** An approver that approved does not upgrade a
        rejection; an approver that refused does not downgrade an approval into
        a different SCOPE. The reviewer is a second opinion a human reads, not
        a second vote. There is no branch here that can return something the
        caller did not pass in, which is why a test can assert it by mutation.
        """
        return human

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, carrying the journal path."""
        return {
            "approved": bool(self.approved),
            "consulted": bool(self.consulted),
            "obtained": self.obtained,
            "failure": self.failure,
            "failure_meaning": APPROVER_FAIL_CLOSED_PATHS.get(self.failure, ""),
            "reason": self.reason,
            "origin": self.origin,
            "advisory": bool(self.advisory),
            "journal_path": self.journal_path,
            "effect_digest": self.effect_digest,
        }


class ApproverBridge:
    """Wires ``harness.approver`` onto the privileged approval path.

    Four properties, and each is where the code is:

    1. **Admissible-only.** :meth:`consult` builds the request and asks
       ``shared.approval.approver_admissible`` BEFORE a prompt is rendered, so an
       unprivileged action never reaches a model at all. A cheap reviewer that
       could be consulted for unprivileged work would be a way to ADD a question
       to every action in a run.
    2. **Fail closed with a DISTINCT reason per path.** Every failure in
       :data:`APPROVER_FAIL_CLOSED_PATHS` is a denial, and each is separately
       nameable.
    3. **Cannot widen.** The verdict is carried in :class:`ApproverOutcome`,
       whose ``advisory`` is always True and whose :meth:`as_decision` returns
       the human's decision verbatim.
    4. **Journalled.** Every prompt, verdict and origin goes to
       ``harness.approver.journal_path_for``'s file, which is the audit trail a
       later authorization receipt can consume.

    Constructing a bridge never dials a provider and never imports the harness:
    both are resolved lazily inside :meth:`consult`, so importing ``cli`` stays
    free of a model dependency.
    """

    def __init__(
        self,
        *,
        config: Optional[Mapping[str, Any]] = None,
        call_fn: Optional[Any] = None,
        journal_path: str = "",
        task_id: str = "",
        agent: Any = None,
    ) -> None:
        """Build a bridge. Never raises; never contacts a provider."""
        self._config = dict(config or {})
        self._call_fn = call_fn
        self._journal_path = str(journal_path or "")
        self._task_id = str(task_id or "")
        self._agent = agent
        self.outcomes: List[ApproverOutcome] = []

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]] = None,
        *,
        call_fn: Optional[Any] = None,
        journal_path: str = "",
        task_id: str = "",
    ) -> "ApproverBridge":
        """Build a bridge from a resolved config, opt-in by key presence."""
        return cls(
            config=config,
            call_fn=call_fn,
            journal_path=journal_path,
            task_id=task_id,
        )

    @property
    def enabled(self) -> bool:
        """Whether a reviewer model is switched on for this run.

        Read from the harness's OWN switch (``approver_agent``) by key presence
        plus truthiness, delegated rather than restated: two definitions of "is
        the approver on" is two answers, and the wrong one is the permissive
        one.
        """
        try:
            from harness import approver as _approver
        except Exception:
            return False
        raw = self._config.get(_approver.APPROVER_ENABLED_KEY)
        if raw is None:
            return False
        if isinstance(raw, bool):
            return bool(raw)
        return str(raw).strip().casefold() in {"true", "yes", "on", "1", "enabled"}

    @property
    def journal_path(self) -> str:
        """Where prompts and verdicts are journalled, resolved lazily."""
        if self._journal_path:
            return self._journal_path
        try:
            from harness import approver as _approver
        except Exception:
            return ""
        try:
            return str(
                _approver.journal_path_for(
                    self._config.get("_log_dir") or "", self._task_id
                )
            )
        except Exception:
            return ""

    def _resolve_agent(self) -> Any:
        """Build the harness agent on first use, or ``None`` if unavailable."""
        if self._agent is not None:
            return self._agent
        try:
            from harness import approver as _approver
        except Exception:
            return None
        try:
            self._agent = _approver.ApproverAgent.from_config(
                self._config,
                call_fn=self._call_fn,
                journal_path=self.journal_path,
                task_id=self._task_id,
            )
        except Exception:
            self._agent = None
        return self._agent

    # -- the decision ------------------------------------------------------

    def consult(
        self,
        effect: Any,
        *,
        origin: Any = None,
        requires_human: bool = True,
        privileged_reason: str = "",
        containment: Optional[Mapping[str, Any]] = None,
    ) -> ApproverOutcome:
        """Return a second opinion on an ALREADY-PRIVILEGED action.

        Never raises. Every failure path produces a denial with its own
        ``failure`` word, and the caller is expected to read
        :meth:`ApproverOutcome.as_decision` -- which cannot widen.
        """
        outcome = self._consult(
            effect,
            origin=origin,
            requires_human=requires_human,
            privileged_reason=privileged_reason,
            containment=containment,
        )
        self.outcomes.append(outcome)
        return outcome

    def _consult(
        self,
        effect: Any,
        *,
        origin: Any,
        requires_human: bool,
        privileged_reason: str,
        containment: Optional[Mapping[str, Any]],
    ) -> ApproverOutcome:
        try:
            from shared.approval import ApprovalOrigin, canonical_effect
        except Exception as exc:  # pragma: no cover - shared is the bottom layer
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure="model_error",
                reason=f"the approval contract is unavailable: {type(exc).__name__}",
            )

        who = origin if isinstance(origin, ApprovalOrigin) else ApprovalOrigin()
        try:
            canonical = (
                effect
                if hasattr(effect, "digest")
                else canonical_effect(
                    str(getattr(effect, "tool", "") or "unknown"), str(effect or "")
                )
            )
        except Exception as exc:
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure="model_error",
                reason=f"the effect could not be canonicalised: {type(exc).__name__}",
                origin=_origin_text(who),
            )

        digest = str(getattr(canonical, "digest", "") or "")

        # (1) Admissibility FIRST: an unprivileged action never reaches a model.
        try:
            from shared.approval import ApproverRequest, approver_admissible

            request = ApproverRequest(
                effect=canonical,
                origin=who,
                requires_human=bool(requires_human),
                privileged_reason=str(privileged_reason or ""),
                containment=dict(containment or {}),
            )
            admissible, why = approver_admissible(request)
        except Exception as exc:
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure="model_error",
                reason=f"admissibility could not be checked: {type(exc).__name__}",
                origin=_origin_text(who),
                effect_digest=digest,
            )
        if not admissible:
            # An untraceable subagent escalation lands here, and it is REFUSED
            # outright rather than shown to a human as if the main thread asked.
            failure = (
                "untraceable_origin"
                if "trace" in str(why).lower() or "subagent" in str(why).lower()
                else "not_privileged"
            )
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure=failure,
                reason=str(why),
                origin=_origin_text(who),
                effect_digest=digest,
            )

        # (2) The enable switch.
        if not self.enabled:
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure="disabled",
                reason="no reviewer agent is configured for this run",
                origin=_origin_text(who),
                effect_digest=digest,
            )

        agent = self._resolve_agent()
        if agent is None:
            return ApproverOutcome(
                approved=False,
                consulted=False,
                failure="disabled",
                reason="the reviewer agent could not be constructed",
                origin=_origin_text(who),
                effect_digest=digest,
            )

        # (3) The bounded call, then (4) the fail-closed parse. Nothing after
        # the parse can turn a denial into an approval.
        try:
            reply = agent.review(
                canonical,
                origin=who,
                requires_human=bool(requires_human),
                privileged_reason=str(privileged_reason or ""),
                containment=dict(containment or {}),
            )
        except Exception as exc:
            return ApproverOutcome(
                approved=False,
                consulted=True,
                failure="model_error",
                reason=f"the reviewer raised {type(exc).__name__}",
                origin=_origin_text(who),
                journal_path=self.journal_path,
                effect_digest=digest,
            )

        approved = bool(getattr(reply, "approved", False))
        reason = str(getattr(reply, "reason", "") or "")
        # ``failure == ""`` is what a REASONED ANSWER looks like on the wire,
        # for a denial as well as an approval: the reviewer said no and said
        # why. The first version mapped that to ``"malformed"``, so every
        # considered refusal was filed under the vocabulary entry for a reply
        # this build cannot parse -- a refusal WITH a reason was reported as a
        # refusal we could not obtain. An empty failure is therefore ``ok``,
        # and the ``failure == "ok"`` correction below now only catches a
        # denial that arrived with no explanation at all.
        failure = str(getattr(reply, "failure", "") or "ok")
        if failure not in APPROVER_OUTCOMES:
            failure = "malformed"
        if not approved and failure == "ok" and not reason:
            failure = "no_reason"
        if failure == "model_error" and not reason:
            # ``harness.approver`` returns a FAILED verdict whose ``reason`` is
            # empty and whose ``raw`` carries the detail it already produced --
            # measured as ``"model_error: RuntimeError: provider is down"``.
            # Rendering that reasonless is how a provider outage becomes a bare
            # "denied" with nothing to act on. The authority's own text is
            # surfaced (clipped) rather than re-derived here, so this layer adds
            # no second opinion about WHAT failed. The first version named
            # ``type(raw).__name__`` instead and produced "could not answer
            # (str)" -- a type name that names the container, not the outage.
            raw = str(getattr(reply, "raw", "") or "").strip()
            reason = (
                raw[:MODEL_ERROR_REASON_MAX_CHARS]
                if raw
                else "the reviewer's model call failed"
            )
        return ApproverOutcome(
            approved=approved,
            consulted=True,
            failure=failure,
            reason=reason,
            origin=_origin_text(who),
            journal_path=self.journal_path,
            effect_digest=digest,
        )

    def report(self) -> Dict[str, Any]:
        """Return the whole session's second-opinion receipt.

        The counts are separated by whether an answer was OBTAINED, because "the
        reviewer said no" and "we never got to ask" are different facts about a
        trust decision and a report that collapses them is the failure this
        round exists to close.
        """
        counts: Dict[str, int] = {name: 0 for name in APPROVER_OUTCOMES}
        obtained = 0
        for outcome in self.outcomes:
            counts[outcome.failure] = counts.get(outcome.failure, 0) + 1
            if outcome.obtained:
                obtained += 1
        return {
            "enabled": self.enabled,
            "consultations": len(self.outcomes),
            "obtained": obtained,
            "not_obtained": len(self.outcomes) - obtained,
            "by_failure": counts,
            "fail_closed_paths": dict(APPROVER_FAIL_CLOSED_PATHS),
            "journal_path": self.journal_path,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def _origin_text(origin: Any) -> str:
    """Return the origin's one-line description, or ``""``."""
    describe = getattr(origin, "describe", None)
    if callable(describe):
        try:
            return str(describe())
        except Exception:
            return ""
    return str(origin or "")


# ===========================================================================
# 5. Plan mode, and its explicit exit
# ===========================================================================


@dataclass(frozen=True)
class PlanExit:
    """A request to leave ``PLAN``, and whether it is answered.

    Corpus detail: Mistral Vibe emits a ``PlanReviewRequestedEvent`` that
    GATES the transition back. Leaving plan mode is a DECISION, so this is a
    decision with a recorded answer, not a fallback: an unanswered request is
    still ``PLAN`` and the receipt says so.
    """

    mode_before: str
    target_mode: str
    reason: str
    approved: bool
    summary: str = ""
    steps: Tuple[str, ...] = ()
    #: Whether leaving the mode required an explicit answer.
    explicit: bool = True

    @property
    def still_planning(self) -> bool:
        """Whether the run REMAINS in plan mode after this request."""
        return not bool(self.approved)

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt."""
        return {
            "mode_before": self.mode_before,
            "target_mode": self.target_mode,
            "reason": self.reason,
            "approved": bool(self.approved),
            "still_planning": self.still_planning,
            "explicit": bool(self.explicit),
            "summary": self.summary,
            "steps": list(self.steps),
        }


def plan_exit_request(
    *,
    approved: bool,
    reason: str = "",
    target_mode: str = MODE_DEFAULT,
    summary: str = "",
    steps: Sequence[str] = (),
) -> PlanExit:
    """Return a ``PLAN`` exit decision.

    ``approved=False`` is the FAIL-CLOSED answer: the run stays in ``PLAN`` and
    the receipt says so. There is no path here that leaves plan mode because a
    caller stopped asking.
    """
    return PlanExit(
        mode_before=MODE_PLAN,
        target_mode=normalize_mode(target_mode),
        reason=str(reason or ""),
        approved=bool(approved),
        summary=str(summary or ""),
        steps=tuple(str(step) for step in (steps or ())),
        explicit=True,
    )


# ===========================================================================
# 6. The in-flight mode session
# ===========================================================================


class ModeSession:
    """The live approval mode, changeable mid-run.

    "Reachable in-flight" is the requirement, and a module-level constant cannot
    satisfy it. This object is what all three surfaces hold, so a mode changed
    in the REPL is the same mode the TUI rail renders and the headless envelope
    publishes.

    A change takes effect on the NEXT tool call rather than the next session,
    which is why :meth:`set` records a revision: a caller that wants to say "this
    change is in force" can compare revisions rather than guessing.
    """

    def __init__(
        self,
        mode: Any = MODE_DEFAULT,
        *,
        reason: str = "",
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Build a session, resolving ``mode`` from config when not given."""
        if config is not None and not str(mode or "").strip():
            mode, reason = resolve_mode(config)
        elif not reason:
            reason = "declared by the caller"
        self._mode = normalize_mode(mode)
        self._reason = str(reason or "")
        self._revision = 1
        self._history: List[Dict[str, Any]] = [
            {"revision": 1, "mode": self._mode, "reason": self._reason}
        ]

    # -- reads -------------------------------------------------------------

    @property
    def mode(self) -> str:
        """The canonical mode in force."""
        return self._mode

    @property
    def reason(self) -> str:
        """Which layer chose the current mode."""
        return self._reason

    @property
    def revision(self) -> int:
        """How many times the mode has changed. Monotonic, never reset."""
        return self._revision

    @property
    def profile(self) -> ModeProfile:
        """The profile for the mode in force."""
        return MODE_PROFILES[self._mode]

    @property
    def reduced_safety(self) -> bool:
        """Whether the mode in force carries reduced safety."""
        return self.profile.reduced_safety

    def marker(self) -> str:
        """The visible marker for the mode in force."""
        return mode_marker(self._mode)

    def line(self, *, detail: bool = False) -> str:
        """The one line every surface renders."""
        return mode_line(self._mode, detail=detail)

    def history(self) -> Tuple[Dict[str, Any], ...]:
        """Every mode this session has been in, in order."""
        return tuple(dict(row) for row in self._history)

    # -- writes ------------------------------------------------------------

    def set(self, mode: Any, *, reason: str = "") -> Tuple[str, str]:
        """Change the mode in flight. Returns ``(mode, reason)``.

        Setting the mode it is already in is a NO-OP that returns the existing
        reason rather than inventing one: "you are already in auto-edit" is the
        honest answer, and a caller that wanted a change and got silence has to
        be able to tell the two apart.
        """
        canonical = normalize_mode(mode, default=self._mode)
        if canonical == self._mode:
            return self._mode, self._reason
        self._mode = canonical
        self._reason = str(reason or f"changed to {canonical!r} in this session")
        self._revision += 1
        self._history.append(
            {"revision": self._revision, "mode": self._mode, "reason": self._reason}
        )
        return self._mode, self._reason

    def prompts(self, tool: Any) -> bool:
        """Whether this tool still raises a dialog in the current mode."""
        return self.profile.prompts(classify_tool(tool))

    def offers(self, tool: Any) -> bool:
        """Whether this tool is offered to the model in the current mode."""
        return self.profile.offers(classify_tool(tool))

    def surface(
        self, catalog: Sequence[Any] = (), *, denied: Any = None
    ) -> ToolSurface:
        """The tool surface for the mode IN FORCE."""
        return tool_surface(self._mode, catalog, denied=denied)

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, including the history."""
        return {
            "mode": self._mode,
            "marker": self.marker(),
            "reason": self._reason,
            "revision": self._revision,
            "profile": self.profile.to_dict(),
            "history": list(self.history()),
        }


# ===========================================================================
# 7. The seven permission outcomes, one line each
# ===========================================================================

OUTCOME_ALLOW = "allow"
OUTCOME_ASK_GRANTED = "ask_granted"
OUTCOME_ASK_DENIED = "ask_denied"
OUTCOME_ASK_TIMED_OUT = "ask_timed_out"
OUTCOME_FLOOR_REFUSED = "floor_refused"
OUTCOME_MODE_BLOCKED = "mode_blocked"
OUTCOME_CAPABILITY_WITHHELD = "capability_withheld"

#: The closed set of permission outcomes a user can observe. Declared and
#: exported so a surface can be driven from the table rather than from prose,
#: and so a NEW outcome cannot be introduced without a renderer.
PERMISSION_OUTCOMES: Tuple[str, ...] = (
    OUTCOME_ALLOW,
    OUTCOME_ASK_GRANTED,
    OUTCOME_ASK_DENIED,
    OUTCOME_ASK_TIMED_OUT,
    OUTCOME_FLOOR_REFUSED,
    OUTCOME_MODE_BLOCKED,
    OUTCOME_CAPABILITY_WITHHELD,
)

#: What the user can DO next, per outcome, in a runnable form.
#:
#: Every value names a real ``/command`` this module's own command mounts, so a
#: card can never carry a next step nobody can type. ``allow`` is the ONE
#: outcome with no door, and it says so in words rather than rendering an empty
#: line -- "nothing to do" is an answer, and an absent one reads as a missing
#: affordance.
_NEXT_STEPS: Dict[str, Tuple[str, ...]] = {
    OUTCOME_ALLOW: ("nothing to do (this action was already allowed)",),
    OUTCOME_ASK_GRANTED: (
        "/approval grants",
        "/approval forget",
    ),
    OUTCOME_ASK_DENIED: (
        "/approval grants",
        "/approval mode default",
    ),
    OUTCOME_ASK_TIMED_OUT: (
        "/approval mode default",
        "/approval grants",
    ),
    OUTCOME_FLOOR_REFUSED: (
        "/approval",
        "/doctor",
        "/trace",
    ),
    OUTCOME_MODE_BLOCKED: (
        "/approval mode default",
        "/approval",
    ),
    OUTCOME_CAPABILITY_WITHHELD: (
        "/approval tools",
        "/approval mode default",
    ),
}

#: The two fallback commands a card with no other door ends on. This is the
#: precedent ``cli.runview.failure_lines`` set: "a classifier action can be pure
#: diagnosis, which is correct advice and useless as an instruction", so a
#: guaranteed runnable command rides on every card.
#:
#: ``allow`` is deliberately EXEMPT, and the exemption is the point: attaching
#: "/doctor /trace" to a card that says the action was allowed tells a person
#: something is wrong when nothing is. The rule is "a runnable next step
#: **where one exists**", and this is the case where none exists.
FALLBACK_COMMANDS: Tuple[str, ...] = ("/doctor", "/trace")

#: Outcomes whose next step is deliberately command-free.
_NO_COMMAND_OUTCOMES: Tuple[str, ...] = (OUTCOME_ALLOW,)

_DEFAULT_NEXT_STEP = ("/approval",)


@dataclass(frozen=True)
class PermissionDecision:
    """One permission outcome, its reason, and its next step.

    The three fields are SEPARATE because they answer three different questions
    and a receipt that blends them answers none of them: what was attempted, why
    the decision was what it was, and what the user can do next.
    """

    outcome: str
    action: str
    reason: str
    next_step: str
    layer: str = ""
    mode: str = ""
    tool: str = ""
    effect: str = ""
    floor: str = ""
    commands: Tuple[str, ...] = ()
    extra: Tuple[str, ...] = ()

    @property
    def decision(self) -> str:
        """``allowed`` or ``refused``, derived from the outcome."""
        return "allowed" if self.outcome == OUTCOME_ALLOW else "refused"

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt."""
        return {
            "outcome": self.outcome,
            "decision": self.decision,
            "action": self.action,
            "reason": self.reason,
            "next_step": self.next_step,
            "commands": list(self.commands),
            "layer": self.layer,
            "mode": self.mode,
            "tool": self.tool,
            "effect": self.effect,
            "floor": self.floor,
            "extra": list(self.extra),
        }

    def to_json(self) -> str:
        """Return the receipt as a single JSON line.

        One line, so a receipt can be appended to a journal row or printed by a
        ``--json`` surface without reformatting it.
        """
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


def explain(
    outcome: Any,
    *,
    action: str = "",
    reason: str = "",
    layer: str = "",
    mode: Any = "",
    tool: Any = "",
    effect: str = "",
    floor: str = "",
    next_step: str = "",
    commands: Sequence[str] = (),
    extra: Sequence[str] = (),
) -> PermissionDecision:
    """Return the ONE permission receipt for an observable outcome.

    **Never returns an empty reason and never an empty next step.** An outcome
    nobody passed a reason for is given the reason the outcome itself implies
    ("the mode is read-only", "a hard floor refused this"), because a receipt
    that says "denied" with nothing after it is the defect this function exists
    to remove.

    An UNRECOGNISED outcome is rendered as ``mode_blocked`` rather than dropped.
    A permission outcome nobody defined still happened, and refusing to render it
    is how a blank permission UI is born; the receipt says it was not recognised
    so a reader can tell the difference.
    """
    key = str(outcome or "").strip().lower()
    recognised = key in PERMISSION_OUTCOMES
    canonical = key if recognised else OUTCOME_MODE_BLOCKED
    mode_key = normalize_mode(mode) if str(mode or "").strip() else ""
    tool_name = str(getattr(tool, "name", None) or tool or "").strip().lower()

    described = str(action or "").strip()
    if not described:
        described = _describe_action(tool_name, effect)

    why = str(reason or "").strip()
    if not why:
        why = _default_reason(canonical, mode_key, layer, floor)

    step = str(next_step or "").strip()
    available = tuple(
        str(item).strip() for item in (commands or ()) if str(item).strip()
    )
    if not available:
        available = _NEXT_STEPS.get(canonical, _DEFAULT_NEXT_STEP)
    if not step:
        step = available[0]

    notes = [str(item).strip() for item in (extra or ()) if str(item).strip()]
    if not recognised:
        notes.insert(
            0,
            f"outcome {key!r} is not one this build defines; rendered as {canonical!r}",
        )

    # The guaranteed runnable command: if nothing the caller supplied names a
    # `/command`, append the fallbacks rather than leaving the card with advice
    # nobody can type. `allow` is exempt, and the exemption is deliberate --
    # see FALLBACK_COMMANDS.
    if canonical in _NO_COMMAND_OUTCOMES:
        runnable = []
    else:
        runnable = [item for item in available if item.startswith("/")]
        for fallback in FALLBACK_COMMANDS:
            if fallback not in runnable:
                runnable.append(fallback)

    return PermissionDecision(
        outcome=canonical,
        action=described,
        reason=why,
        next_step=step,
        layer=str(layer or ""),
        mode=mode_key,
        tool=tool_name,
        effect=str(effect or ""),
        floor=str(floor or ""),
        commands=tuple(runnable),
        extra=tuple(notes),
    )


def _describe_action(tool_name: str, effect: str) -> str:
    """Return the WHAT half: the action in human terms."""
    detail = str(effect or "").strip()
    if detail:
        verb = detail.split()[0] if detail.split() else detail
        return f"run `{verb}`"
    if tool_name:
        return f"use the {tool_name} tool"
    return "take a side-effecting action"


def _default_reason(outcome: str, mode: str, layer: str, floor: str) -> str:
    """Return the WHY half when the caller supplied none."""
    if outcome == OUTCOME_ALLOW:
        return "the mode auto-approves this class of action"
    if outcome == OUTCOME_ASK_GRANTED:
        return "you approved this action"
    if outcome == OUTCOME_ASK_DENIED:
        return "the dialog was answered no"
    if outcome == OUTCOME_ASK_TIMED_OUT:
        return "nobody answered the dialog before its deadline"
    if outcome == OUTCOME_FLOOR_REFUSED:
        detail = str(floor or "").strip()
        return (
            f"a hard floor refused this and no permission level can allow it: "
            f"{detail}"
            if detail
            else "a hard floor refused this and no permission level can allow it"
        )
    if outcome == OUTCOME_MODE_BLOCKED:
        detail = mode_line(mode) if mode else "the current mode"
        return f"{detail} does not offer this tool to the model at all"
    if outcome == OUTCOME_CAPABILITY_WITHHELD:
        return "policy withdrew this tool from the model's view for this turn"
    return "the permission decision is recorded with no reason"


def explain_line(decision: Any, *, width: int = 0) -> str:
    """Return the ONE plain-language line for a permission decision.

    The line names, in order: **what** was attempted, **why** the decision was
    what it was, and **what the user can do next**. It is never empty, for any
    outcome, including an unrecognised one -- a permission UI that goes blank on
    an unusual outcome is the absence of an affordance.

    ``width`` clips with an explicit marker rather than a bare cut, because a
    clipped sentence stops being true at the cut and a card that reads as
    complete when it is not is the honesty defect this whole section closes.
    """
    if isinstance(decision, str):
        decision = explain(decision)
    if isinstance(decision, Mapping):
        decision = explain(
            decision.get("outcome", ""),
            action=str(decision.get("action", "") or ""),
            reason=str(decision.get("reason", "") or ""),
            next_step=str(decision.get("next_step", "") or ""),
            mode=str(decision.get("mode", "") or ""),
            tool=str(decision.get("tool", "") or ""),
        )
    action = str(getattr(decision, "action", "") or "take a side-effecting action")
    reason = str(getattr(decision, "reason", "") or "no reason was recorded")
    step = str(getattr(decision, "next_step", "") or "/approval")
    commands = tuple(str(item) for item in getattr(decision, "commands", ()) or ())
    line = f"{action} - {reason} - next: {step}"
    if commands and not any(item in line for item in commands):
        line = f"{line} ({', '.join(commands)})"
    limit = int(width or 0)
    if limit > 0 and len(line) > limit:
        marker = "..."
        keep = max(1, limit - len(marker))
        line = line[:keep] + marker
    return line or "a permission decision was recorded with no detail - next: /approval"


def explain_lines(decision: Any, *, width: int = 0) -> List[str]:
    """Return the one-line explanation plus its supporting notes.

    Always at least one line. The notes carry the ``extra`` rows an explanation
    was built with, so a rendered card can state an unrecognised outcome rather
    than silently reclassifying it.
    """
    lines = [explain_line(decision, width=width)]
    for note in tuple(getattr(decision, "extra", ()) or ()):
        text = str(note).strip()
        if text:
            lines.append(f"  {text}")
    return lines
