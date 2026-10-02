"""WHAT KIND of command this is - the axis the registry did not have.

`cli.commands.CommandSpec` says HOW a result is shown
(`result_presentation`), when a command may run (`idle_policy` /
`in_flight_policy`) and what it needs (`required_permissions`). It never said
WHAT KIND of command it is, so nothing downstream could answer three questions
that have nothing to do with presentation:

* does running this spend tokens,
* may this open a modal,
* does this finish immediately while a run is live.

Without them ``/ask`` (a real model call) and ``/quiet`` (a boolean flip) are
indistinguishable to any budget, and "which commands cost money" is a question
the product cannot answer before the user presses enter.

THE FOUR TYPES
--------------

``LOCAL``
    Runs a plain function. No model call, no interactive component, no
    confirmation gate. ``/quiet``, ``/theme``, ``/status``, ``/cost``,
    ``/clear``, ``/copy-diff``, ``/help``.
``LOCAL_UI``
    Renders an interactive component - a browser, a picker, a stepped wizard.
    This is the ONLY type that may open a modal.
``PROMPT``
    Injects a prompt into the conversation and delegates to the model, so it is
    MODEL-CONSUMING. ``/ask``, ``/plan``, ``/build``, ``/resume``, ``/steer``.
``SKILL``
    Runs a file-backed workflow (a ``SKILL.md`` / command template) as a task,
    optionally in a subagent. Also model-consuming. ``/review`` is the one
    command whose entire purpose is a reusable, on-disk workflow - it is the
    row the registry already marked with ``template_resolvable=True``.

WHY A MODAL AND A GATE ARE NOT THE SAME THING
---------------------------------------------
``may_open_modal`` is a property of the TYPE and is true for ``LOCAL_UI`` only.
A confirmation gate - "run this plan?", "approve this effect?" - is a different
mechanism with a different owner: it is raised by a model workflow that is
about to change your files, which is precisely why ``PROMPT`` and ``SKILL`` are
allowed to raise one and ``LOCAL`` is not. ``/plan`` is the live case: its
``result_presentation`` is ``modal`` because its plan preview is a gate, and
``may_open_modal("/plan")`` is still ``False``. Both facts are pinned by tests
and neither is inferred from the other.

THE DORMANT-FIELD QUESTION, ANSWERED
------------------------------------
``CommandSpec.palette_behavior`` is NOT dormant and is NOT the tier axis, and
this module does not ask anyone to retire it. It is the ENTER-KEY axis: ``run``
means choosing the row executes the command, ``prefill`` means it fills the
composer so you finish the sentence. On the live tree 44 rows declare ``run``
and 8 declare ``prefill`` (``/mode``, ``/plan``, ``/build``, ``/ask``, ``/undo``,
``/resume``, ``/trace``, ``/steer``). The type axis and the palette axis are
independent - ``/ask`` is a PROMPT that prefills, ``/diff`` is a LOCAL_UI that
runs, ``/trace`` is a LOCAL_UI that prefills, ``/quiet`` is a LOCAL that runs -
so retiring one because the other exists would delete a behaviour the product
relies on. See ``cli/AGENTS.md`` for the cross-tab that proves it.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not dispatch anything, it does not read a config default, and it never
decides whether a run is verified. It is a pure projection over a spec's name
and two registry fields, so it can be imported by ``cli.commands`` without a
cycle and by a surface without importing the registry at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "BACKGROUND_POLICIES",
    "COMMAND_TYPES",
    "COST_CLASSES",
    "DEFAULT_COMMAND_TYPE",
    "GATE_COMMANDS",
    "MIGRATION_REPORT",
    "TYPES",
    "CommandType",
    "background_policy",
    "command_type",
    "context_budget_names",
    "cost_class",
    "cost_projection",
    "is_model_consuming",
    "may_fork_subagent",
    "may_gate",
    "may_open_modal",
    "migration_report_document",
    "migration_report_rows",
    "runs_instantly_while_busy",
    "subagent_agent",
    "type_label",
    "type_marker",
    "type_markers",
]

#: Schema version for every JSON document this module produces. Additive keys
#: bump nothing; a REMOVED or REPURPOSED key bumps this.
SCHEMA_VERSION = 1


class CommandType:
    """The four command types, and the four capabilities each one grants.

    A plain class rather than an ``Enum`` so the members are usable as dict
    keys in a frozen dataclass without an import-order surprise, and so a
    consumer can read ``CommandType.LOCAL.model_consuming`` without calling.
    Every capability here is a CLAIM about what a command of this kind may do;
    the registry's existing policies stay the authority (see
    ``runs_instantly_while_busy`` for the one place they meet).
    """

    __slots__ = (
        "label",
        "marker",
        "may_fork_subagent",
        "may_gate",
        "may_open_modal",
        "model_consuming",
        "name",
        "summary",
    )

    def __init__(
        self,
        name: str,
        label: str,
        marker: str,
        summary: str,
        *,
        model_consuming: bool,
        may_open_modal: bool,
        may_gate: bool,
        may_fork_subagent: bool,
    ) -> None:
        self.name = name
        self.label = label
        self.marker = marker
        self.summary = summary
        self.model_consuming = model_consuming
        self.may_open_modal = may_open_modal
        self.may_gate = may_gate
        self.may_fork_subagent = may_fork_subagent

    @property
    def cost_class(self) -> str:
        """Return ``tokens`` for a model-consuming type, else ``free``."""
        return "tokens" if self.model_consuming else "free"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly capability row for one type."""
        return {
            "name": self.name,
            "label": self.label,
            "marker": self.marker,
            "summary": self.summary,
            "model_consuming": self.model_consuming,
            "cost": self.cost_class,
            "may_open_modal": self.may_open_modal,
            "may_gate": self.may_gate,
            "may_fork_subagent": self.may_fork_subagent,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        """Return the type name, so a repr in a failure reads as a word."""
        return f"<CommandType {self.name}>"

    def __eq__(self, other: Any) -> bool:
        """Compare by type NAME, so a rebuilt instance is still equal."""
        return isinstance(other, CommandType) and other.name == self.name

    def __hash__(self) -> int:
        """Hash by name so it is usable as a mapping key."""
        return hash(("CommandType", self.name))


#: The four types, in the order a reader meets them.
LOCAL = CommandType(
    "local",
    "local",
    ".",
    "runs a plain function: no model call, no component, no gate",
    model_consuming=False,
    may_open_modal=False,
    may_gate=False,
    may_fork_subagent=False,
)
LOCAL_UI = CommandType(
    "local_ui",
    "local ui",
    "#",
    "renders an interactive component (browser, picker, stepped wizard)",
    model_consuming=False,
    may_open_modal=True,
    may_gate=False,
    may_fork_subagent=False,
)
PROMPT = CommandType(
    "prompt",
    "prompt",
    "~",
    "injects a prompt into the conversation and delegates to the model",
    model_consuming=True,
    may_open_modal=False,
    may_gate=True,
    may_fork_subagent=False,
)
SKILL = CommandType(
    "skill",
    "skill",
    "*",
    "runs a file-backed workflow as a task, optionally in a subagent",
    model_consuming=True,
    may_open_modal=False,
    may_gate=True,
    may_fork_subagent=True,
)

#: The closed vocabulary, in declaration order. A closed set is what makes
#: "every command carries a type" a countable claim instead of a vibe.
TYPES: Tuple[CommandType, ...] = (LOCAL, LOCAL_UI, PROMPT, SKILL)

_BY_NAME: Mapping[str, CommandType] = {t.name: t for t in TYPES}


def _type(name: str) -> CommandType:
    """Return the type member for a declared name, or raise on a typo."""
    found = _BY_NAME.get(str(name))
    if found is None:
        raise KeyError(f"unknown command type: {name!r} (known: {', '.join(_BY_NAME)})")
    return found


#: The type a command gets when it declares none.
#:
#: Chosen so an UNDECLARED command keeps whatever the registry already says.
#: ``LOCAL`` is the only type that grants nothing: not model-consuming, not
#: permitted a modal, not permitted a gate, not permitted a subagent. So a
#: command nobody classified cannot accidentally spend tokens or take over the
#: screen - the failure this default exists to prevent. Every one of the 56
#: registry rows declares a type, so this default is reachable only by a
#: third-party or future spec.
DEFAULT_COMMAND_TYPE: CommandType = LOCAL


#: Every registry command's type, keyed by canonical command name.
#:
#: This is DATA, not a derivation, because the honest answer is not derivable:
#: ``/plan`` and ``/trace`` share a presentation, ``/help`` and ``/history``
#: share a shape, and only reading the handler says whether a provider is
#: called. Each entry below is justified in ``MIGRATION_REPORT``.
COMMAND_TYPES: Mapping[str, CommandType] = {
    # ---- the registry reads and writes -------------------------------
    "/help": LOCAL,
    "/status": LOCAL,
    "/cost": LOCAL,
    "/effort": LOCAL,
    "/context": LOCAL_UI,
    "/trace": LOCAL_UI,
    "/feed": LOCAL_UI,
    "/diff": LOCAL_UI,
    "/checkpoints": LOCAL_UI,
    "/sessions": LOCAL_UI,
    "/files": LOCAL_UI,
    "/relevant": LOCAL_UI,
    "/diagnostics": LOCAL_UI,
    "/history": LOCAL,
    "/copy-diff": LOCAL,
    "/undo": LOCAL,
    "/redo": LOCAL,
    "/mcp": LOCAL,
    "/skills": LOCAL,
    "/plugins": LOCAL,
    # ---- settings and credentials ------------------------------------
    "/connect": LOCAL_UI,
    "/login": LOCAL_UI,
    "/logout": LOCAL,
    "/model": LOCAL,
    "/theme": LOCAL,
    "/settings": LOCAL,
    "/init": LOCAL,
    "/repo": LOCAL,
    "/open": LOCAL,
    "/doctor": LOCAL,
    # ---- conversation lifecycle --------------------------------------
    "/mode": LOCAL,
    "/compact": LOCAL,
    "/clear": LOCAL,
    "/quiet": LOCAL,
    "/fork": LOCAL,
    "/import": LOCAL,
    "/recover": LOCAL,
    "/export": LOCAL,
    "/share": LOCAL,
    # ---- run control (they stop or steer a run, they never start one) --
    "/cancel": LOCAL,
    "/detach": LOCAL,
    "/attach": LOCAL,
    "/watch": LOCAL,
    "/approve": LOCAL,
    "/reject": LOCAL,
    "/quit": LOCAL,
    # ---- model-consuming ---------------------------------------------
    "/ask": PROMPT,
    "/plan": PROMPT,
    "/build": PROMPT,
    "/resume": PROMPT,
    "/steer": PROMPT,
    "/review": SKILL,
    # ---- the four doors VEX-CS-01 added ------------------------------
    "/worktree": LOCAL,
    "/hooks": LOCAL,
    "/migrate": LOCAL,
    "/support-bundle": LOCAL,
}


#: Cost classes, derived from the type rather than declared per command, so a
#: new command cannot claim to be free when its type is not.
COST_CLASSES: Tuple[str, ...] = ("free", "tokens")


#: What a SKILL command declares when it runs as a task. Read by
#: ``background_policy`` / ``subagent_agent`` and validated at import, so a
#: SKILL command cannot be added without saying how it runs.
#:
#: These are DECLARATIONS of capability, not a description of today's
#: behaviour: ``/review`` currently runs in the foreground of the session that
#: typed it. Declaring them costs nothing and is inert until a surface mounts
#: them, which is why adding this data is not a behaviour change.
BACKGROUND_POLICIES: Tuple[str, ...] = ("foreground", "subagent")

_SKILL_EXECUTION: Mapping[str, Tuple[str, str]] = {
    "/review": ("subagent", "reviewer"),
}


def _validate_skill_execution() -> None:
    """Reject a SKILL command with no declared policy or agent."""
    for name, command_type in COMMAND_TYPES.items():
        if not command_type.may_fork_subagent:
            continue
        declared = _SKILL_EXECUTION.get(name)
        if declared is None:
            raise ValueError(
                f"skill command declares no background policy or agent: {name}"
            )
        policy, agent = declared
        if policy not in BACKGROUND_POLICIES:
            raise ValueError(
                f"{name} declares an unsupported background policy: {policy}"
            )
        if not agent:
            raise ValueError(f"{name} declares no subagent agent")


_validate_skill_execution()


def _name_of(spec: Any) -> str:
    """Return the canonical command name for a spec, a name, or nothing.

    Duck-typed on purpose: this module must not import ``cli.commands`` (that
    module imports this one), so the only contract is "has a ``.name``".
    """
    if spec is None:
        return ""
    if isinstance(spec, str):
        return str(spec).strip()
    name = getattr(spec, "name", None)
    return str(name or "").strip()


def command_type(spec: Any) -> CommandType:
    """Return the declared type of one command, or the default.

    ``spec`` may be a ``CommandSpec``, a command name, or ``None``. A name the
    table does not carry falls back to :data:`DEFAULT_COMMAND_TYPE` rather than
    raising, because a surface asking "what kind is this?" about a project or
    plugin command is asking a question with a real answer - it is a plain
    function until somebody says otherwise.
    """
    return COMMAND_TYPES.get(_name_of(spec), DEFAULT_COMMAND_TYPE)


def is_model_consuming(spec: Any) -> bool:
    """Return whether running this command can spend provider tokens.

    DERIVED from the type, never declared per command, so a command cannot be
    marked free while its type says it delegates to the model.
    """
    return command_type(spec).model_consuming


def cost_class(spec: Any) -> str:
    """Return ``tokens`` or ``free`` for one command."""
    return command_type(spec).cost_class


def may_open_modal(spec: Any) -> bool:
    """Return whether this command's TYPE may open a modal component.

    Only ``LOCAL_UI``. A PROMPT or SKILL command that needs to ask permission
    raises a confirmation GATE (:func:`may_gate`), which is a different
    mechanism with a different owner.
    """
    return command_type(spec).may_open_modal


def may_gate(spec: Any) -> bool:
    """Return whether this command's type may raise a confirmation gate.

    ``PROMPT`` and ``SKILL`` only. A gate asks "about to change your files,
    proceed?" - a question only a workflow that changes files has to ask.
    """
    return command_type(spec).may_gate


def runs_instantly_while_busy(spec: Any) -> bool:
    """Return whether this command finishes immediately while a run is live.

    BOTH conditions must hold, and the split matters:

    * the TYPE must be ``LOCAL`` - only a plain function is guaranteed to
      finish without a provider round trip or a component. This is the
      necessity the brief asks to be pinned: no PROMPT, SKILL or LOCAL_UI
      command is ever reported instant.
    * the REGISTRY must still allow it in flight (``in_flight_policy ==
      "allow"``). The registry stays the authority, so adding a type cannot
      weaken or strengthen an existing in-flight gate.

    ``/steer`` is the case that makes the first clause load-bearing: it is
    ``in_flight_policy="allow"`` (it is only meaningful during a run) and it is
    a PROMPT, so it must NOT be reported instant.
    """
    if command_type(spec) is not LOCAL:
        return False
    policy = getattr(spec, "in_flight_policy", None)
    if policy is None:
        # A bare NAME carries no policy. Answer with the type's own claim and
        # let the caller that has the spec apply the registry.
        return True
    return str(policy) == "allow"


def may_fork_subagent(spec: Any) -> bool:
    """Return whether this command's type may run in a subagent."""
    return command_type(spec).may_fork_subagent


def background_policy(spec: Any) -> str:
    """Return the declared background policy, or ``""`` for a non-SKILL."""
    return _SKILL_EXECUTION.get(_name_of(spec), ("", ""))[0]


def subagent_agent(spec: Any) -> str:
    """Return the declared subagent agent name, or ``""`` for a non-SKILL."""
    return _SKILL_EXECUTION.get(_name_of(spec), ("", ""))[1]


def type_marker(spec: Any) -> str:
    """Return the one-glyph type marker for a menu row."""
    return command_type(spec).marker


def type_label(spec: Any) -> str:
    """Return the human label for a command's type."""
    return command_type(spec).label


def type_markers() -> Dict[str, str]:
    """Return every type marker keyed by type name (the menu legend)."""
    return {t.name: t.marker for t in TYPES}


def type_rows() -> List[Dict[str, Any]]:
    """Return one JSON-friendly row per type, for ``--json`` and for docs."""
    return [t.to_dict() for t in TYPES]


def cost_projection(
    names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return the model-consumption projection for the command surface.

    This is the receipt a ``--json`` document needs for a cost column and the
    set a context-budget projection counts. ``names`` defaults to every
    classified command; passing an iterable projects a SUBSET (a session's
    commands, one eval arm's commands) without copying the table.

    Deliberately a PROJECTION: it reads the classification and writes nothing,
    and it names the axis it was derived from so a reader knows a cost column
    here is a claim about the TYPE, not a measurement of a run.
    """
    if names is None:
        selected: List[str] = list(COMMAND_TYPES)
    else:
        selected = [str(n).strip() for n in names]
    model_consuming = [n for n in selected if is_model_consuming(n)]
    free = [n for n in selected if not is_model_consuming(n)]
    by_type: Dict[str, List[str]] = {}
    for name in selected:
        by_type.setdefault(command_type(name).name, []).append(name)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "cli.command_types.COMMAND_TYPES",
        "axis": "command type",
        "total": len(selected),
        "model_consuming": model_consuming,
        "model_consuming_count": len(model_consuming),
        "free": free,
        "free_count": len(free),
        "by_type": {key: sorted(value) for key, value in sorted(by_type.items())},
        "note": (
            "model_consuming is derived from the command TYPE, which says "
            "whether the handler can reach a provider. It is not a measurement "
            "of any run; cli.runview owns the measured number."
        ),
    }


def context_budget_names() -> Tuple[str, ...]:
    """Return the commands a context-budget projection should count.

    The same set as the model-consuming half of :func:`cost_projection`,
    returned under its own name because a context budget and a cost receipt are
    different consumers with different downstream owners, and one of them is
    this module and the other is not.
    """
    return tuple(sorted(n for n in COMMAND_TYPES if is_model_consuming(n)))


#: Commands that raise a confirmation gate, with the owner of each. Declared
#: rather than inferred from ``result_presentation == "modal"`` because the
#: brief's rule ("only LOCAL_UI may open a modal") and the registry's
#: vocabulary ("modal") disagree for exactly one row, and the disagreement is
#: the point rather than an exception to be hidden.
GATE_COMMANDS: Mapping[str, str] = {
    "/plan": "the plan-preview confirm raised before the run starts",
}


@dataclass(frozen=True)
class MigrationRow:
    """One command's old behaviour, its new type, and what changed."""

    name: str
    command_type: CommandType
    old_behaviour: str
    reason: str
    behaviour_change: str = ""
    test: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly migration row."""
        return {
            "name": self.name,
            "type": self.command_type.name,
            "model_consuming": self.command_type.model_consuming,
            "may_open_modal": self.command_type.may_open_modal,
            "may_gate": self.command_type.may_gate,
            "may_fork_subagent": self.command_type.may_fork_subagent,
            "old_behaviour": self.old_behaviour,
            "reason": self.reason,
            "behaviour_change": self.behaviour_change,
            "test": self.test,
        }


def _row(
    name: str,
    old_behaviour: str,
    reason: str,
    behaviour_change: str = "",
    test: str = "",
) -> MigrationRow:
    """Build one row from the classification table, so they cannot diverge."""
    return MigrationRow(
        name=name,
        command_type=COMMAND_TYPES[name],
        old_behaviour=old_behaviour,
        reason=reason,
        behaviour_change=behaviour_change,
        test=test,
    )


MIGRATION_REPORT: Tuple[MigrationRow, ...] = (
    _row(
        "/help",
        "prints the grouped/ranked help index to the transcript",
        "renders text; no component is mounted and no provider is reached",
        "none: the palette gains a type marker and a cost class, both additive "
        "keys on an existing row",
        "test_help_is_local_and_the_menu_says_so",
    ),
    _row(
        "/status",
        "prints live status lines or delegates to `cli.main.cmd_status`",
        "reads the journal and formats it; no component, no provider",
    ),
    _row(
        "/cost",
        "prints run and session spend from the router ledger",
        "a READ of the ledger - it is how you find out what a model call cost, "
        "so calling it model-consuming would be circular",
    ),
    _row(
        "/effort",
        "sets/reads the effort rung and writes NEO_EFFORT",
        "writes a configuration value; it changes no conversation and reaches "
        "no provider",
    ),
    _row(
        "/context",
        "opens `_ContextSourcesScreen` (TUI) / prints cited sources (REPL)",
        "mounts an interactive panel a person browses",
    ),
    _row(
        "/trace",
        "opens `_TraceDetailScreen` with a journal entry",
        "mounts an interactive panel",
    ),
    _row(
        "/feed",
        "opens `_FeedBrowserScreen` over the action feed",
        "mounts a searchable interactive panel",
    ),
    _row(
        "/diff",
        "pushes `_DiffBrowserScreen` / `_DiffFileScreen`; verbs accept, reject, revert",
        "mounts an interactive diff browser; the mutating verbs are file "
        "operations and stay model-free",
    ),
    _row(
        "/checkpoints",
        "opens `_CheckpointsScreen` and `_CheckpointActionsScreen`",
        "mounts an interactive browser with a per-checkpoint action modal",
    ),
    _row(
        "/sessions",
        "opens `_SessionsScreen` over the session index",
        "mounts a searchable interactive browser",
    ),
    _row(
        "/files",
        "opens `_FilesScreen` over the repository tree and symbols",
        "mounts a searchable interactive browser",
    ),
    _row(
        "/relevant",
        "opens `_RelevantFilesScreen` over the ranked file projection",
        "mounts a searchable interactive browser",
    ),
    _row(
        "/diagnostics",
        "opens `_DiagnosticsScreen` and follows a row into the composer",
        "mounts an interactive list whose selection is acted on",
    ),
    _row(
        "/history",
        "prints matched input-history lines to the transcript",
        "DECISION: LOCAL, not LOCAL_UI. The `ctrl+r` history SCREEN is a "
        "different action (`action_input_history`) and a different affordance; "
        "the slash verb only prints. Recorded because it is the one row where "
        "the obvious reading (result_presentation == browser) is wrong",
        "none: the type changes no dispatch",
        "test_history_prints_and_is_local",
    ),
    _row(
        "/copy-diff",
        "copies the current diff to the clipboard",
        "a plain function over the diff projection",
    ),
    _row(
        "/undo",
        "stages a revert range; `commit` applies it",
        "file operations plus a receipt; no provider",
    ),
    _row(
        "/redo",
        "applies the task-local redo receipt",
        "a plain function over a receipt",
    ),
    _row(
        "/mcp",
        "lists connectors and their tools (VEX-CS-01 verbs route to `neo mcp ...`)",
        "DECISION: LOCAL, not LOCAL_UI. The connector listing is printed to the "
        "transcript by `_mcp_list_done`; no screen is mounted",
        "none: the type changes no dispatch",
        "test_mcp_prints_and_is_local",
    ),
    _row(
        "/skills",
        "prints discovered skills with their origins",
        "DECISION: LOCAL, not LOCAL_UI. `_render_skills` prints; it does not "
        "run a skill and does not mount a component",
        "none: the type changes no dispatch",
        "test_skills_prints_and_is_local",
    ),
    _row(
        "/plugins",
        "prints installed plugins and their enablement",
        "DECISION: LOCAL, not LOCAL_UI. The REPL handler prints a table; the "
        "VEX-CS-01 verbs are flag-only routes to `neo plugin ...`",
        "none: the type changes no dispatch",
        "test_plugins_prints_and_is_local",
    ),
    _row(
        "/connect",
        "runs the interactive credential provider picker",
        "renders a stepped interactive component - it asks and reads input",
    ),
    _row(
        "/login",
        "pushes `_OnboardScreen`",
        "renders a stepped interactive wizard",
    ),
    _row(
        "/logout",
        "removes the stored credential and reports what remains",
        "a settings write plus a receipt",
    ),
    _row(
        "/model",
        "shows the effective model, or pins a model name",
        "reads and writes configuration; no provider is called to show a name",
    ),
    _row(
        "/theme",
        "shows or selects a terminal theme and installs its tokens",
        "a settings write plus a render pass",
    ),
    _row(
        "/settings",
        "prints effective settings and their source tiers",
        "a read of the settings chain",
    ),
    _row(
        "/init",
        "scaffolds the project's `.neo/` config",
        "file creation plus a receipt",
    ),
    _row(
        "/repo",
        "switches the session repository and reloads project settings",
        "settings and cache reload; no provider",
    ),
    _row(
        "/open",
        "launches the configured editor at a path/line",
        "spawns an editor process; no provider",
    ),
    _row(
        "/doctor",
        "runs read-only health checks and reports each one",
        "runs probes and reports; it never repairs and never calls a provider",
    ),
    _row(
        "/mode",
        "sets the session mode (plan/build/explore/review/debug/ask)",
        "DECISION, AND THE ONE PLACE THIS CLASSIFICATION DEVIATES FROM THE "
        "BRIEF: the brief lists /mode as model-consuming, but `_mode_command` "
        "writes `state['mode']` and prints one line. It reaches no provider, so "
        "typing it PROMPT would put a free command in a cost column and spend a "
        "user's trust in that column. Typed LOCAL; the mode it selects is what "
        "the NEXT run is built from",
        "none: the type changes no dispatch",
        "test_mode_is_local_because_it_reaches_no_provider",
    ),
    _row(
        "/compact",
        "compacts the conversation into a deterministic summary",
        "DECISION: LOCAL. The summary is derived from the existing journal and "
        "recall primitive with no model call, so marking it PROMPT would "
        "invent a cost that does not exist",
        "none: the type changes no dispatch",
        "test_compact_is_local_and_costs_nothing",
    ),
    _row(
        "/clear",
        "starts a fresh conversation, keeping the old journal",
        "a file write plus a handle swap",
    ),
    _row(
        "/quiet",
        "toggles live-feed verbosity",
        "a boolean flip",
    ),
    _row(
        "/fork",
        "forks the conversation at a turn boundary",
        "session bookkeeping",
    ),
    _row(
        "/import",
        "imports a session export as a new conversation",
        "session bookkeeping",
    ),
    _row(
        "/recover",
        "reports or quarantines a corrupt conversation",
        "session bookkeeping",
    ),
    _row(
        "/export",
        "writes a privacy-filtered session artifact",
        "a file write",
    ),
    _row(
        "/share",
        "writes a metadata-only shareable artifact",
        "a file write",
    ),
    _row(
        "/cancel",
        "cancels the live run, keeping checkpoints",
        "DECISION: LOCAL. It STOPS a model workflow; the tokens were already "
        "spent by the run and this command spends none",
        "none: the type changes no dispatch",
        "test_cancel_is_local_because_it_spends_nothing",
    ),
    _row(
        "/detach",
        "leaves the run alive and stops projecting it",
        "run control; no provider",
    ),
    _row(
        "/attach",
        "rebinds to a detached run by replaying its journal",
        "a read of the journal",
    ),
    _row(
        "/watch",
        "follows a run's journal with a byte offset",
        "a read of the journal",
    ),
    _row(
        "/approve",
        "records a decision on a pending request",
        "DECISION: LOCAL. Its `result_presentation` is `approval`, but the "
        "PROMPT is raised by the worker's approval callback, not by this "
        "command - this command writes the answer",
        "none: the type changes no dispatch",
        "test_approve_records_a_decision_and_is_local",
    ),
    _row(
        "/reject",
        "records a rejection on a pending request",
        "as /approve: the prompt belongs to the worker, the answer is local",
    ),
    _row(
        "/quit",
        "quits, handling any active run safely",
        "session control",
    ),
    _row(
        "/ask",
        "runs a read-only question against the repository",
        "a real model call through `_run_one_question`",
    ),
    _row(
        "/plan",
        "previews steps and runs the request with plan guidance",
        "a real model call; and the case that separates a GATE from a MODAL - "
        "its plan preview is a confirmation raised before the run, which is "
        "why `may_open_modal` is False and `may_gate` is True",
        "none: the type changes no dispatch; the gate set is declared data",
        "test_plan_is_a_prompt_with_a_gate_and_not_a_modal",
    ),
    _row(
        "/build",
        "runs an explicit build request",
        "a real model call through `_run_one_build`",
    ),
    _row(
        "/resume",
        "restarts a resumable run from its journal",
        "DECISION: PROMPT, though the brief did not list it. Resuming RE-RUNS "
        "the harness against a provider, so calling it free would understate "
        "the cost of the most natural way to continue work",
        "none: the type changes no dispatch",
        "test_resume_is_model_consuming",
    ),
    _row(
        "/steer",
        "delivers an instruction into the live run's steering journal",
        "DECISION: PROMPT. It reaches no provider itself, but the run consumes "
        "the text and calls the model, so calling it free would understate a "
        "command whose whole purpose is to cause a model call",
        "none: the type changes no dispatch",
        "test_steer_is_a_prompt_and_never_instant",
    ),
    _row(
        "/review",
        "renders the last diff + rationale, or runs a resolvable review "
        "template as a fix request",
        "SKILL: the one command whose purpose is a reusable ON-DISK workflow "
        "(the row the registry already marked `template_resolvable=True`), run "
        "as a task. Model-consuming like a PROMPT, and the only type allowed to "
        "fork a subagent",
        "none: the declared background policy and agent are inert data until a "
        "surface mounts them",
        "test_review_is_a_skill_and_declares_its_execution",
    ),
    _row(
        "/worktree",
        "lists, creates, removes and prints isolated git worktrees",
        "git subprocesses and file operations",
    ),
    _row(
        "/hooks",
        "lists configured user hooks and the fail policy in force",
        "a read of the hook configuration",
    ),
    _row(
        "/migrate",
        "reports the pending migrations without writing",
        "a read plus a rendered plan; the declared verb is `plan`",
    ),
    _row(
        "/support-bundle",
        "writes one redacted diagnostic bundle",
        "a file write with redaction on every value",
    ),
)


def migration_report_rows() -> List[Dict[str, Any]]:
    """Return every migration row as a JSON-friendly dict, in registry order."""
    return [row.to_dict() for row in MIGRATION_REPORT]


def migration_report_document() -> Dict[str, Any]:
    """Return the whole migration report, including what it does NOT change.

    The ``behaviour_changes`` list is the important part: it is empty unless a
    decision below says otherwise, and a test fails if a row grows an entry
    without naming the test that pins it.
    """
    changes = [
        row
        for row in MIGRATION_REPORT
        if row.behaviour_change not in ("", "none")
        and not row.behaviour_change.startswith("none:")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "round": "VEX-CS-02-command-types",
        "commands": migration_report_rows(),
        "total": len(MIGRATION_REPORT),
        "behaviour_changes": [row.name for row in changes],
        "gate_commands": dict(GATE_COMMANDS),
        "default_type": DEFAULT_COMMAND_TYPE.name,
        "types": type_rows(),
        "note": (
            "Classifying a command changed NO dispatch, NO in-flight gate, NO "
            "exit code and NO verifier path. The additive changes are: "
            "`command_type`/`model_consuming`/`cost`/`type_marker` on a "
            "palette entry, `command_type` on a spec's `to_dict()`, and the "
            "SKILL execution declarations."
        ),
    }
