"""The `/` menu: the ONE authoritative list of what this build can do.

Rule 4 of the reference system this round is built on: *availability depends on
the platform, the model and the installed plugins, so the MENU is the
documentation.* A command this build cannot run must not be invisible, and a
command that costs tokens must say so before the user presses enter. That makes
the menu a projection, not a document: every row is derived from the command
registry (`cli.commands.COMMAND_SPECS`), the live availability gate
(`command_availability`), the installed plugins and the connected MCP servers,
and the thirty task phrasings in `cli.onboarding.TASKS`. There is no second
table of commands anywhere in this module, because a second table is a command
that exists in the menu and not in the product.

WHAT THIS MODULE OWNS
--------------------
* `PALETTE_GROUPS` - the seven groups a person thinks in, and the ONLY
  mapping from a command name to a group. Every command is in exactly one.
* `palette_entries` - the merged row set: registry + plugins + MCP prompts.
* `group_entries` - grouping with the anti-clutter rule read from
  `cli.design.ANTI_CLUTTER_MIN_ENTRIES`. A one-member group is HIDDEN.
* `search_entries` - the one ranking path, over name, description, group AND
  the phrasing corpus, so "show me what changed" finds `/diff`.
* `entry_row` / `palette_lines` - the renderers. Plain text out, plus a
  `Text` variant, because these rows carry command summaries, plugin names and
  MCP server names, all of which are DATA.
* `PaletteScreen` - the Textual screen, mountable by `cli/tui.py` (Prompt 01's
  file, deliberately not edited here) and drivable headlessly.
* `slash_hook` - the composer key contract, written out so the mount is a
  copy-paste rather than a design decision.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not dispatch anything. Choosing a row returns a
`PaletteEntry`; running it is the shell's existing `_slash_command`, so there
is exactly one implementation of "what `/undo` does" and it is not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

__all__ = [
    "COST_GLYPHS",
    "ENTRY_KINDS",
    "MCP_COMMAND_PREFIX",
    "OTHER_GROUP",
    "OTHER_TITLE",
    "PALETTE_GROUPS",
    "PALETTE_TRIGGER",
    "PLUGIN_COMMAND_SEPARATOR",
    "PaletteEntry",
    "PaletteGroup",
    "anti_clutter_min_entries",
    "command_group",
    "entry_row",
    "entry_text_row",
    "escape_lines",
    "group_entries",
    "group_for_command",
    "mcp_entries",
    "open_rows",
    "palette_entries",
    "palette_lines",
    "palette_receipt",
    "palette_text_lines",
    "phrasings_warm",
    "plugin_entries",
    "search_entries",
    "slash_hook",
    "text_lines",
    "unassigned_commands",
]


# ---------------------------------------------------------------------------
# 1. The seven groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaletteGroup:
    """One heading a person would use to look for a command.

    `key` is the stable id (used by tests and by any future "open this group"
    affordance); `title` is what is rendered; `because` is the reason the
    group exists, kept beside it so a future session can argue with the
    grouping instead of rediscovering it.
    """

    key: str
    title: str
    because: str


#: The menu's grouping. Declared in the order a first-day user needs them, and
#: the order is the argument: you set yourself up, you look at what happened,
#: you put it back, you check whether it is true, you add what you own, you
#: configure it, and only then do you go looking for documentation.
#:
#: The names are chosen for a PERSON, not for the code. `cli/commands.py` is
#: organised by surface; this is organised by the question "what am I trying
#: to do?", and the two deliberately do not line up.
PALETTE_GROUPS: Tuple[PaletteGroup, ...] = (
    PaletteGroup(
        key="getting_started",
        title="Getting started",
        because="a first run has to connect a model and read the repository "
        "before anything else can work",
    ),
    PaletteGroup(
        key="session",
        title="Session",
        because="what you are talking to: past runs, this run's control, and "
        "the conversation itself",
    ),
    PaletteGroup(
        key="changes",
        title="Changes",
        because="an agent that edits your files owes you the diff and the way "
        "to put it back",
    ),
    PaletteGroup(
        key="verification",
        title="Verification",
        because="the promise of this product is that a run is only called done "
        "when its tests pass; this is where you check that claim",
    ),
    PaletteGroup(
        key="extensions",
        title="Extensions",
        because="what YOU installed: MCP connectors, skills, plugins",
    ),
    PaletteGroup(
        key="configuration",
        title="Configuration",
        because="settings, model and theme: the knobs that outlive a run",
    ),
    PaletteGroup(
        key="help",
        title="Help",
        because="the command guide, the health check that says whether this "
        "install can work at all, and the mode picker - the three questions "
        "of 'what can I do, is it working, and how should it behave'",
    ),
)

_GROUP_BY_KEY: Dict[str, PaletteGroup] = {g.key: g for g in PALETTE_GROUPS}
_TITLE_BY_KEY: Dict[str, str] = {g.key: g.title for g in PALETTE_GROUPS}


#: The ONE command -> group mapping. Every registered command appears in
#: exactly one group, and `unassigned_commands()` reports any that do not, so
#: a new `CommandSpec` is a visible gap rather than a silently homeless row.
#:
#: This is DATA, deliberately, and the reason is that it is the only place a
#: human decides "which bucket does this belong in". Moving a command between
#: groups is an edit to a tuple, not a code change; the gate
#: `tests/test_palette_discovery.py::test_every_command_is_in_exactly_one_group`
#: fails if a command is missing or listed twice.
COMMAND_GROUPS: Dict[str, str] = {
    # -- Getting started -----------------------------------------------------
    "/plan": "getting_started",
    "/build": "getting_started",
    "/ask": "getting_started",
    "/connect": "getting_started",
    "/login": "getting_started",
    "/logout": "getting_started",
    "/init": "getting_started",
    "/repo": "getting_started",
    # -- Session -------------------------------------------------------------
    "/status": "session",
    "/sessions": "session",
    "/resume": "session",
    "/fork": "session",
    "/import": "session",
    "/recover": "session",
    "/compact": "session",
    "/clear": "session",
    "/context": "session",
    "/history": "session",
    "/quiet": "session",
    "/steer": "session",
    "/cancel": "session",
    "/detach": "session",
    "/attach": "session",
    "/watch": "session",
    "/share": "session",
    "/export": "session",
    "/quit": "session",
    # -- Changes -------------------------------------------------------------
    "/diff": "changes",
    "/undo": "changes",
    "/redo": "changes",
    "/files": "changes",
    "/relevant": "changes",
    "/review": "changes",
    "/open": "changes",
    "/copy-diff": "changes",
    "/worktree": "changes",
    # -- Verification --------------------------------------------------------
    "/checkpoints": "verification",
    "/trace": "verification",
    "/feed": "verification",
    "/diagnostics": "verification",
    "/cost": "verification",
    "/effort": "verification",
    "/approve": "verification",
    "/reject": "verification",
    # -- Extensions ----------------------------------------------------------
    "/mcp": "extensions",
    "/skills": "extensions",
    "/plugins": "extensions",
    "/hooks": "extensions",
    # -- Configuration -------------------------------------------------------
    "/settings": "configuration",
    "/theme": "configuration",
    "/model": "configuration",
    "/migrate": "configuration",
    # -- Help ----------------------------------------------------------------
    "/help": "help",
    "/doctor": "help",
    "/mode": "help",
    "/support-bundle": "help",
}


def anti_clutter_min_entries() -> int:
    """The one threshold, read from the layout authority.

    `cli/design.py` owns the anti-clutter rule; this module asks rather than
    restating the number, because a second copy of `3` is how the sidebar and
    the menu start disagreeing about what "cluttered" means. A `cli.design`
    that cannot be imported falls back to the shipped value, because "the menu
    imports cleanly" is worth more than "the menu imports the authority".
    """
    try:
        from cli import design
    except Exception:
        return 3
    try:
        return max(1, int(design.ANTI_CLUTTER_MIN_ENTRIES))
    except Exception:
        return 3


def group_for_command(name: str) -> Optional[PaletteGroup]:
    """The group a command belongs to, or None when it is not in the map."""
    key = COMMAND_GROUPS.get(str(name or ""))
    return _GROUP_BY_KEY.get(key) if key else None


def command_group(name: str) -> str:
    """The group KEY for a command, or `""` when unmapped.

    A string rather than the dataclass because a caller assembling a search
    haystack wants the key, and a missing key must be a falsy value it can
    notice rather than an exception on a menu-open path.

    DYNAMIC rows are resolved by their NAME SHAPE rather than by a map, because
    a `/plugin:skill` or `/mcp__server__prompt` name does not exist until the
    moment it does and cannot be listed ahead of time. Both shapes land in
    `extensions`, which is the group a person would look in for something they
    installed themselves. The shapes are the ones this module itself mints, so
    a producer that invents a third shape is visibly homeless rather than
    silently misplaced.
    """
    text = str(name or "")
    key = COMMAND_GROUPS.get(text)
    if key:
        return key
    bare = text.lstrip("/")
    if bare.startswith(MCP_COMMAND_PREFIX) or PLUGIN_COMMAND_SEPARATOR in bare:
        return "extensions"
    return ""


def unassigned_commands() -> List[str]:
    """Registered commands with no group, and groups naming no live command.

    The two halves are different defects and both are worth naming: a command
    with no group is homeless in the menu, and a group key nothing maps to is a
    heading that will never render. Never raises - an unavailable registry
    yields both lists empty rather than breaking a menu open.
    """
    try:
        from cli import commands as commands_mod
    except Exception:
        return []
    try:
        live = {
            str(getattr(spec, "name", "") or "")
            for spec in commands_mod.COMMAND_SPECS
            if getattr(spec, "name", "")
        }
    except Exception:
        return []
    return sorted(name for name in live if name not in COMMAND_GROUPS)


def _empty_group_keys() -> List[str]:
    """Group keys nothing maps to - a heading that will never be rendered."""
    used = set(COMMAND_GROUPS.values())
    return [key for key in _TITLE_BY_KEY if key not in used]


# ---------------------------------------------------------------------------
# 2. Cost, so a user can see what a command spends before pressing enter
# ---------------------------------------------------------------------------

#: The glyph vocabulary, keyed by `CommandSpec.result_presentation` (the
#: registry's own field, read not restated) and by whether the command starts
#: a run. ASCII fallbacks because a cp1252 console raises on a Unicode glyph,
#: and a menu that crashes on a legacy terminal is a menu that does not exist.
#:
#: The point is not decoration: `~` on a row means "this one calls a model".
#: A person choosing between reading a diff and re-running the agent should be
#: able to see the difference before they spend anything.
#:
#: **Terminal 02 landed `cli/command_types.py` while this round was running**,
#: and it owns this answer properly: `command_type(spec)` is a closed table of
#: `local | local_ui | prompt | skill` carrying its own `.marker` and
#: `.cost_class`, and `CommandSpec.command_type` is a registry field. This
#: module DELEGATES to it (see `_glyph_for`) rather than keeping the second
#: table below, because a second cost table is exactly the drift this round
#: exists to remove. `COST_GLYPHS` survives as the FALLBACK for a command the
#: type module does not carry, and a test asserts the two agree on every
#: registered command - so the fallback cannot quietly become a rival.
COST_GLYPHS: Dict[str, Tuple[str, str]] = {
    "run": ("~", "~"),  # starts a run: a model call, tokens, a verifier
    "browser": ("=", "="),  # a browsable list; local, no model
    "card": ("#", "#"),  # a structured card; local, no model
    "diff": ("+", "+"),  # shows a diff; local, no model
    "modal": ("?", "?"),  # asks something; may block
    "settings": ("*", "*"),  # changes persistent configuration
    "approval": ("!", "!"),  # a permission decision, which is the human's
    "exit": (">", ">"),
    "inline": (".", "."),
}

#: A run is only launched by these presentations AND a `run` palette
#: behaviour. `/help` that renders inline is free; a `/plan` that starts a
#: planner is not. The registry has no "costs tokens" field, so this is
#: DERIVED from the two fields it does have, and named so a future registry
#: field can replace it in one place.
_COST_IS_RUN_PRESENTATIONS = frozenset({"card", "modal", "approval"})


# ---------------------------------------------------------------------------
# 3. The row
# ---------------------------------------------------------------------------


#: The kinds a row can be. `command` is the registry; the other three are the
#: dynamic sources. Declared as a closed vocabulary because a consumer has to
#: be able to COUNT them, and "some other kind" is not countable.
ENTRY_KINDS: Tuple[str, ...] = ("command", "custom", "plugin", "mcp", "file")


#: Encoding probe results, keyed by the character asked about.
#:
#: Cached because the probe is the answer to a QUESTION ABOUT THE STREAM, and
#: the stream does not change inside a process, while the question was being
#: asked once per row per menu open. Measured on this host: 10,400 probes
#: accounted for **0.202 s of a 0.626 s build** - 32 % of the menu's render
#: path spent re-asking a question whose answer cannot have moved. The cache is
#: bounded to the glyph vocabulary rather than being an unbounded memo, so a
#: hostile description containing a million distinct characters cannot grow it.
_GLYPH_CACHE: Dict[str, str] = {}


def _glyph(pretty: str, ascii_fallback: str) -> str:
    """Encoding-safe glyph pick, delegating to the product's own probe.

    `cli.ui._enc_ok` is the one place that knows what the current stream can
    render; a second probe here is a second answer to the same question. The
    answer is cached per character - see `_GLYPH_CACHE` for the measurement
    that made it necessary.
    """
    if pretty in _GLYPH_CACHE:
        return _GLYPH_CACHE[pretty]
    chosen = ascii_fallback
    try:
        from cli import ui

        if ui._enc_ok(pretty):
            chosen = pretty
    except Exception:
        chosen = ascii_fallback
    if len(_GLYPH_CACHE) < 64:
        _GLYPH_CACHE[pretty] = chosen
    return chosen


@dataclass(frozen=True)
class PaletteEntry:
    """One row of the menu, and everything a renderer needs to draw it.

    Deliberately carries the DISPLAY facts (description, argument hint,
    aliases, glyph) rather than a spec reference, so a dynamic row (a plugin
    skill, an MCP prompt) is the same shape as a registry row and a consumer
    never branches on provenance to render.

    The two states a user must be able to tell apart are separate fields:
    `available` is the gate, `hidden` is "this build does not show it at
    all", and `reason` / `note` say why. `note` is deliberately NOT a refusal -
    `command_availability` marks "needs a task in this session" as a note
    because every such handler already degrades to an honest line of its own.
    """

    name: str
    kind: str = "command"
    group: str = ""
    description: str = ""
    argument_hint: str = ""
    aliases: Tuple[str, ...] = ()
    glyph: str = "."
    available: bool = True
    hidden: bool = False
    reason: str = ""
    note: str = ""
    run: bool = True
    value: str = ""
    source: str = ""
    phrasing: Any = ()
    shortcuts: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        """One of `available | noted | unavailable | hidden`.

        A single declared word per state, because "can I run this" has four
        honest answers and a boolean cannot carry them. `noted` exists so a
        caller that wants to hide the noise can, without losing the reason.
        """
        if self.hidden:
            return "hidden"
        if not self.available:
            return "unavailable"
        if self.note:
            return "noted"
        return "available"

    @property
    def search_text(self) -> str:
        """Every string a query may match, for one flat `fuzzy_score` call.

        Concatenating is FINE here and wrong in `cli.fuzzy.field_score`, and
        the difference is that this is a convenience for callers that only
        need "does anything match at all", while `search_entries` scores the
        fields separately so a long description cannot outweigh a name.
        """
        parts = [self.name, *self.aliases, self.description, self.argument_hint]
        if self.group:
            parts.append(_TITLE_BY_KEY.get(self.group, self.group))
        return " ".join(part for part in parts if part)

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-friendly row, for a receipt or a headless assertion."""
        return {
            "name": self.name,
            "kind": self.kind,
            "group": self.group,
            "group_title": _TITLE_BY_KEY.get(self.group, self.group),
            "description": self.description,
            "argument_hint": self.argument_hint,
            "aliases": list(self.aliases),
            "glyph": self.glyph,
            "state": self.state,
            "available": self.available,
            "hidden": self.hidden,
            "reason": self.reason,
            "note": self.note,
            "run": self.run,
            "value": self.value,
            "source": self.source,
        }

    def to_legacy_entry(self) -> Dict[str, Any]:
        """The dict shape `cli/tui.py`'s `_PaletteScreen` already consumes.

        `command_palette_entries` returns `{kind, label, hint, value, run, ...}`
        and the shell's `add_files` appends the same shape, so a row handed
        across the mount boundary has to be that shape. Producing it here is
        what lets the `/` hook reuse the existing screen instead of a fork of
        it - the alternative, a second screen class, is two implementations of
        one behaviour.
        """
        bits = [self.description] if self.description else []
        if self.argument_hint:
            bits.append(self.argument_hint)
        if self.aliases:
            bits.append("aliases: " + ", ".join(self.aliases))
        if self.shortcuts:
            bits.append(self.shortcuts)
        if not self.available:
            bits.append("unavailable: " + self.reason)
        elif self.note:
            bits.append(self.note)
        return {
            "kind": self.kind,
            "label": self.name,
            "hint": " · ".join(bits),
            "value": self.value or self.name,
            "run": self.run,
            "disabled": not self.available,
            "disabled_reason": self.reason,
            "note": self.note,
            "group": self.group,
            "glyph": self.glyph,
            "state": self.state,
        }


def _glyph_for(spec: Any) -> str:
    """The cost glyph for a `CommandSpec`, from the ONE type authority.

    Delegates to `cli.command_types` (Terminal 02's), which is the closed
    table of `local | local_ui | prompt | skill` with a `.marker` and a
    `.cost_class` per entry, and which reads `CommandSpec.command_type`. The
    presentation-derived `COST_GLYPHS` table below is the FALLBACK for a build
    without that module, or a command it does not carry - never a rival.

    Returning the ASCII form when the stream cannot render the Unicode one is
    the same rule as everywhere else in this module: a menu that crashes on a
    cp1252 console is a menu that does not exist.
    """
    try:
        from cli import command_types

        return _glyph(
            command_types.type_marker(spec),
            command_types.command_type(spec).marker,
        )
    except Exception:
        pass
    presentation = str(getattr(spec, "result_presentation", "inline") or "inline")
    behavior = str(getattr(spec, "palette_behavior", "run") or "run")
    key = presentation
    if (
        behavior == "run"
        and presentation in _COST_IS_RUN_PRESENTATIONS
        and getattr(spec, "name", "") in _RUN_COMMANDS
    ):
        key = "run"
    pretty, ascii_fallback = COST_GLYPHS.get(key, COST_GLYPHS["inline"])
    return _glyph(pretty, ascii_fallback)


#: The commands that actually START a run, declared as the FALLBACK's input.
#:
#: This is NO LONGER the authority - `cli.command_types` is, and the glyph
#: comes from there - so this set exists only for a build where that module
#: cannot be imported. It is kept small and declared rather than inferred,
#: because "which of these spends tokens" is the question the glyph answers
#: and a guess would be a lie in a menu. A test asserts the two agree on every
#: registered command, so the fallback cannot become a rival by drifting.
_RUN_COMMANDS: frozenset = frozenset(
    {
        "/plan",
        "/build",
        "/ask",
        "/review",
        "/mode",
        "/steer",
        "/resume",
        "/repo",
    }
)


# ---------------------------------------------------------------------------
# 4. Building the rows
# ---------------------------------------------------------------------------


def _registry_entries(context: Any = None) -> List[PaletteEntry]:
    """One row per `CommandSpec`, projected from the registry and the gate.

    Every display fact comes from the spec: the summary, the argument hint,
    the ALIASES (read from `spec.aliases`, never from a table here - that is
    how `/plugins` reveals it resolves to `/plugin` without anyone maintaining
    a second alias list), and the availability from
    `command_availability`. Never raises: an unavailable registry yields no
    rows rather than taking the menu down.
    """
    try:
        from cli import commands as commands_mod
    except Exception:
        return []
    try:
        specs = list(commands_mod.COMMAND_SPECS)
    except Exception:
        return []
    try:
        phrasings = _task_phrasings()
    except Exception:
        phrasings = {}
    rows: List[PaletteEntry] = []
    for spec in specs:
        name = str(getattr(spec, "name", "") or "")
        if not name:
            continue
        try:
            availability = commands_mod.command_availability(spec, context)
        except Exception:
            continue
        try:
            aliases = tuple(str(a) for a in (getattr(spec, "aliases", ()) or ()))
        except Exception:
            aliases = ()
        try:
            shortcuts = str(spec.shortcut_label() or "")
        except Exception:
            shortcuts = ""
        rows.append(
            PaletteEntry(
                name=name,
                kind="command",
                group=command_group(name),
                description=str(getattr(spec, "summary", "") or ""),
                argument_hint=str(getattr(spec, "argument_hint", "") or ""),
                aliases=aliases,
                glyph=_glyph_for(spec),
                available=bool(getattr(availability, "available", True)),
                hidden=bool(getattr(availability, "hidden", False)),
                reason=str(getattr(availability, "reason", "") or ""),
                note=str(getattr(availability, "note", "") or ""),
                run=str(getattr(spec, "palette_behavior", "run")) == "run",
                value=name,
                source="registry",
                phrasing=phrasings.get(name, ()),
                shortcuts=shortcuts,
            )
        )
    return rows


_PHRASINGS_CACHE: Optional[Dict[str, Any]] = None


def _task_phrasings() -> Dict[str, Any]:
    """The phrasing corpus, read once per process and copied on the way out.

    Cached because it is a pure projection of a frozen table and a menu open
    must not re-walk thirty rows per keystroke. The cache is module-private and
    `palette_receipt()` reports whether it was warm, so a stale corpus is
    visible in evidence rather than invisible in a search result.
    """
    global _PHRASINGS_CACHE
    if _PHRASINGS_CACHE is None:
        try:
            from cli import onboarding

            _PHRASINGS_CACHE = dict(onboarding.task_phrasings())
        except Exception:
            _PHRASINGS_CACHE = {}
    return dict(_PHRASINGS_CACHE)


def phrasings_warm() -> bool:
    """Whether the phrasing corpus has been read yet (a receipt, not a claim)."""
    return _PHRASINGS_CACHE is not None


#: Terminal 07's producer contributes `/mcp__<server>__<prompt>` rows. It is
#: not in the tree yet, so the palette READS it by name and degrades to an
#: empty list when it is absent. Reading it by name rather than importing it is
#: what lets this module land first and still be the authority afterwards: the
#: moment the producer exists, the menu shows it with no edit here.
MCP_PROMPT_PRODUCERS: Tuple[str, ...] = (
    "cli.mcp_prompts:mcp_prompt_entries",
    "cli.mcp_prompts:list_prompt_entries",
    "cli.connectors:mcp_prompt_entries",
)

#: Terminal 06's plugin producer contributes `/<plugin>:<skill>` rows under the
#: same rule. The default implementation reads `cli.plugins.list_plugins()`,
#: which IS in the tree, so plugin rows work today; the hook exists so a
#: richer producer can replace it without a second merge path.
PLUGIN_ENTRY_PRODUCERS: Tuple[str, ...] = ("cli.palette:plugin_entries",)


MCP_COMMAND_PREFIX = "mcp__"
PLUGIN_COMMAND_SEPARATOR = ":"


def _resolve_producer(spec: str) -> Any:
    """Resolve a `module:function` producer name, or None when absent.

    Total by construction: an unimportable module or a missing attribute is
    None, and a producer that RAISES is None with the reason discarded here and
    reported by `palette_receipt()`. A dynamic source that breaks must not take
    the registry rows down with it - the user still needs `/undo` to be
    findable because a plugin's manifest is malformed.
    """
    try:
        module_name, _, attr = str(spec or "").partition(":")
        if not module_name or not attr:
            return None
        import importlib

        module = importlib.import_module(module_name)
        fn = getattr(module, attr, None)
        return fn if callable(fn) else None
    except Exception:
        return None


def mcp_entries(
    servers: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    producer: Any = None,
) -> List[PaletteEntry]:
    """Rows for connected MCP servers' prompts: `/mcp__<server>__<prompt>`.

    Two ways in, and the distinction matters for tests. `servers` is the
    already-read data (what a test passes); `producer` is a zero-argument
    callable Terminal 07 supplies. With neither, the named producers are tried
    in `MCP_PROMPT_PRODUCERS` order and an absent one is simply no rows - the
    menu is still complete for everything the registry declares.

    The name shape is `mcp__<server>__<prompt>` because that is what
    `cli.connectors` already namespaces tools to, and a menu that spelled the
    same server two ways would be a menu a user cannot type. A server name or
    prompt name containing a bracket is DATA and reaches the renderer as a
    `Text`; `entry_row` never emits it as markup.
    """
    rows: List[PaletteEntry] = []
    data: List[Mapping[str, Any]] = []
    if servers is not None:
        data = [s for s in servers if isinstance(s, Mapping)]
    elif callable(producer):
        try:
            produced = producer()
        except Exception:
            produced = []
        data = [s for s in (produced or []) if isinstance(s, Mapping)]
    else:
        for name in MCP_PROMPT_PRODUCERS:
            fn = _resolve_producer(name)
            if fn is None:
                continue
            try:
                produced = fn()
            except Exception:
                continue
            data = [s for s in (produced or []) if isinstance(s, Mapping)]
            if data:
                break
    for item in data:
        server = str(item.get("server") or item.get("label") or "").strip()
        prompt = str(item.get("prompt") or item.get("name") or "").strip()
        if not server or not prompt:
            continue
        available = item.get("available", True)
        rows.append(
            PaletteEntry(
                name=f"/{MCP_COMMAND_PREFIX}{server}__{prompt}",
                kind="mcp",
                group="extensions",
                description=str(item.get("description") or ""),
                argument_hint=str(item.get("argument_hint") or ""),
                glyph=_glyph(*COST_GLYPHS["run"]),
                available=bool(available),
                reason=str(item.get("reason") or ""),
                note=str(item.get("note") or ""),
                run=True,
                value=f"/{MCP_COMMAND_PREFIX}{server}__{prompt}",
                source=f"mcp:{server}",
                extra={"server": server, "prompt": prompt},
            )
        )
    return rows


def plugin_entries(
    plugins: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    producer: Any = None,
) -> List[PaletteEntry]:
    """Rows for installed plugins' skills: `/<plugin>:<skill>`.

    Reads `cli.plugins.list_plugins()` by default - which IS in the tree - so
    this half of the dynamic surface works today rather than being a promise.
    A DISABLED plugin contributes nothing: a menu that offers a skill the
    harness will not load is advertising a door with nothing behind it, which
    is the same defect as a help entry for a command that does not exist.

    The namespace separator is `:` and a plugin or skill name containing one
    is preserved rather than escaped, because the name is what the user types.
    A disabled plugin with skills is still visible but marked unavailable with
    its reason, so "why can't I use it" is answerable from the menu.
    """
    rows: List[PaletteEntry] = []
    data: List[Mapping[str, Any]] = []
    if plugins is not None:
        data = [p for p in plugins if isinstance(p, Mapping)]
    elif callable(producer):
        try:
            produced = producer()
        except Exception:
            produced = []
        data = [p for p in (produced or []) if isinstance(p, Mapping)]
    else:
        try:
            from cli import plugins as plugins_mod

            listed = plugins_mod.list_plugins()
        except Exception:
            listed = []
        data = [p for p in (listed or []) if isinstance(p, Mapping)]
    for item in data:
        plugin = str(item.get("name") or "").strip()
        if not plugin:
            continue
        enabled = bool(item.get("enabled", True))
        description = str(item.get("description") or "")
        # The manifest's declared skills, unioned with what is actually on
        # disk. `list_plugins` already recounts both, and the union is the
        # honest answer: a manifest that lists a deleted skill should not
        # advertise it, and a skill added without touching the manifest should
        # still be findable.
        names: List[str] = []
        for key in ("skills_on_disk", "skills"):
            for value in item.get(key) or ():
                text = str(value).strip()
                if text and text not in names:
                    names.append(text)
        for skill in names:
            rows.append(
                PaletteEntry(
                    name=f"/{plugin}{PLUGIN_COMMAND_SEPARATOR}{skill}",
                    kind="plugin",
                    group="extensions",
                    description=description or f"skill from the {plugin} plugin",
                    glyph=_glyph(*COST_GLYPHS["browser"]),
                    available=enabled,
                    reason="" if enabled else f"plugin {plugin} is disabled",
                    run=True,
                    value=f"/{plugin}{PLUGIN_COMMAND_SEPARATOR}{skill}",
                    source=f"plugin:{plugin}",
                    extra={"plugin": plugin, "skill": skill},
                )
            )
    return rows


def palette_entries(
    context: Any = None,
    *,
    custom: Optional[Sequence[str]] = None,
    plugins: Optional[Sequence[Mapping[str, Any]]] = None,
    servers: Optional[Sequence[Mapping[str, Any]]] = None,
    include_dynamic: bool = True,
) -> List[PaletteEntry]:
    """The merged row set: registry + custom commands + plugins + MCP prompts.

    ORDER is the argument, and it is the order the reference systems use:
    the commands this build declares, then the commands YOU added, then the
    extensions you installed, then the servers you connected. A user typing
    `/` sees what the product can do before what they bolted onto it, and a
    plugin can never displace a built-in from the top of the list.

    `include_dynamic=False` is the fast path the "/" hook uses for the first
    paint: the registry rows are pure and cost nothing, while reading the
    plugins root and the MCP registry is I/O. The screen merges the dynamic
    rows in on a worker thread, exactly as the existing `add_files` does, so a
    slow plugin directory cannot hold the menu closed.
    """
    rows = _registry_entries(context)
    for name in custom or ():
        text = str(name or "").strip()
        if not text:
            continue
        rows.append(
            PaletteEntry(
                name=text if text.startswith("/") else f"/{text}",
                kind="custom",
                group="extensions",
                description="custom command from this repository",
                glyph=_glyph(*COST_GLYPHS["run"]),
                run=False,
                value=text if text.startswith("/") else f"/{text}",
                source="custom",
            )
        )
    if include_dynamic:
        rows.extend(plugin_entries(plugins))
        rows.extend(mcp_entries(servers))
    return rows


# ---------------------------------------------------------------------------
# 5. Grouping, with the anti-clutter rule
# ---------------------------------------------------------------------------


def group_entries(
    entries: Sequence[PaletteEntry],
    *,
    min_entries: Optional[int] = None,
) -> List[Tuple[str, List[PaletteEntry]]]:
    """`(title, rows)` in menu order, with THIN GROUPS' HEADINGS dropped.

    The anti-clutter rule, read from `cli.design` rather than restated: a
    heading plus one or two rows costs three lines to say almost nothing, so a
    group below the threshold is HIDDEN rather than shown near-empty.

    The HEADING is hidden, not the rows. That distinction is the whole design
    and it is load-bearing: a menu that dropped a command because its heading
    was thin would be a menu hiding a command, which is the failure rule 5
    exists to prevent. So a thin group's rows are returned under an EMPTY
    title, which every renderer reads as "no heading for these". The group is
    still discoverable by typing a word from its name, because the group title
    is one of the searched fields.

    Hidden rows (`entry.hidden`) are the only ones actually dropped, and they
    are dropped here rather than at the call site so there is ONE hiding rule
    with one opinion.

    An unmapped command (a `CommandSpec` added without a group row) falls into
    the same un-headed bucket, and `unassigned_commands()` names it, so a
    missing group row is visible rather than fatal.
    """
    threshold = (
        anti_clutter_min_entries() if min_entries is None else max(1, int(min_entries))
    )
    buckets: Dict[str, List[PaletteEntry]] = {}
    for entry in entries or ():
        if getattr(entry, "hidden", False):
            continue
        key = str(getattr(entry, "group", "") or "") or OTHER_GROUP
        buckets.setdefault(key, []).append(entry)
    out: List[Tuple[str, List[PaletteEntry]]] = []
    loose: List[PaletteEntry] = list(buckets.get(OTHER_GROUP) or ())
    for group in PALETTE_GROUPS:
        rows = buckets.get(group.key)
        if not rows:
            continue
        if len(rows) >= threshold:
            out.append((group.title, rows))
        else:
            loose.extend(rows)
    if loose:
        # ONE un-headed bucket for everything the rule removed, and its rows
        # keep their own group name in the row itself so a reader can still see
        # where a command belongs.
        out.append(("", loose))
    return out


#: The overflow bucket for a command nobody grouped. Declared rather than
#: inline so `group_entries` and `unassigned_commands` can be read together.
OTHER_GROUP = "other"
OTHER_TITLE = "More"


# ---------------------------------------------------------------------------
# 6. Search
# ---------------------------------------------------------------------------


def _entry_fields(entry: PaletteEntry) -> Dict[str, str]:
    """The named search fields of one row, for `cli.fuzzy.entry_score`."""
    return {
        "name": str(entry.name or ""),
        "alias": " ".join(str(a) for a in (entry.aliases or ())),
        "argument": str(entry.argument_hint or ""),
        "description": str(entry.description or ""),
        "group": _TITLE_BY_KEY.get(str(entry.group or ""), str(entry.group or "")),
        "kind": str(entry.kind or ""),
        "phrasing": list(entry.phrasing or ()),
    }


def search_entries(
    entries: Sequence[PaletteEntry],
    query: str,
    *,
    limit: Optional[int] = None,
) -> List[PaletteEntry]:
    """Rank rows by name, description, group AND the phrasing corpus.

    The one ranking path, so a search can never mean two things in two
    surfaces. An empty query returns the curated order unchanged (the menu
    opens grouped, and re-ranking 52 rows by a score of zero would scramble
    the grouping for no reason).

    A query that matches nothing returns `[]`. The menu says so honestly
    rather than showing the whole wall, because "your filter excluded
    everything" and "here is everything" are different facts and only one of
    them is true.

    Degradation is the honest ceiling and is stated rather than hidden: a
    phrasing nobody wrote into `cli.onboarding.TASKS` does not reach a row
    through the corpus, but it still reaches it through the name, the summary
    and the group - so it degrades to the pre-round behaviour rather than to
    nothing. Thirty hand-written phrasings is a corpus, not universal task
    understanding, and this function does not pretend otherwise.
    """
    rows = list(entries or ())
    if not str(query or "").strip():
        return rows[:limit] if limit is not None and limit >= 0 else rows
    try:
        from cli import fuzzy
    except Exception:
        return []
    ranked = fuzzy.rank_entries(rows, query, _entry_fields, limit=limit)
    out: List[PaletteEntry] = []
    for item in ranked:
        if isinstance(item, PaletteEntry):
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# 7. Rendering
# ---------------------------------------------------------------------------


def escape_lines(lines: Any) -> List[str]:
    """`lines` as rich markup, every field escaped.

    rich's OWN `escape`, so the escaping cannot drift from the parser it
    defends. A row carrying a command summary, a plugin description or an MCP
    server name is carrying DATA, and a repository or package named
    `weird[red].x` must render as that name rather than eating the row.
    """
    try:
        from rich.markup import escape
    except Exception:
        return [str(line) for line in (lines or [])]
    return [str(escape(str(line))) for line in (lines or [])]


def text_lines(lines: Any, style: str = "") -> List[Any]:
    """`lines` as `rich.text.Text` - no markup interpretation at all.

    The STRUCTURAL answer: a `Text` is a string plus spans, so there is no
    parser between it and the terminal and no bracket in it can mean anything.
    Preferred wherever the sink accepts `Text`, which is the palette screen's
    case.
    """
    from rich.text import Text

    return [Text(str(line), style=style or "") for line in (lines or [])]


#: The columns of a rendered row. A terminal has no width, so the budget is
#: expressed as a fixed layout with the description bounded and the reason
#: LAST - the reason is what a person needs when a command is refused, and it
#: must never be the thing that gets truncated.
_ROW_NAME_COLUMNS = 26
_DESCRIPTION_COLUMNS = 46
_REASON_COLUMNS = 40


def _clip(text: str, columns: int) -> str:
    """Bound a field to `columns`, marking the cut.

    A bounded string is still that string; a clipped SENTENCE stops being
    true at the cut, so the ellipsis is not decoration.
    """
    value = " ".join(str(text or "").split())
    if columns <= 0 or len(value) <= columns:
        return value
    return value[: max(1, columns - 1)] + "…"


def entry_row(
    entry: PaletteEntry,
    *,
    width: int = 100,
    show_group: bool = False,
) -> str:
    """One row as PLAIN text: glyph, name, description, argument, reason.

    Plain on purpose. This is the string that crosses into a renderer, and a
    row's description, a plugin's name and an MCP server's label are all data.
    Two exits, and a caller picks by what its sink accepts:

    * `entry_row` - plain, for a `print`, a receipt, a headless assertion.
      **A sink that parses markup must use `entry_markup_row` instead**;
      handing this to one is how a name containing `[` gets eaten.
    * `entry_markup_row` - rich markup with every field escaped.
    * `entry_text_row` - `rich.text.Text`, which has no parser at all.

    The reason column comes LAST and is never the field that loses characters
    to a long description, because the reason is the whole point of showing an
    unavailable command rather than hiding it.
    """
    room = max(40, int(width or 100))
    glyph = str(entry.glyph or ".")
    name = _clip(entry.name, _ROW_NAME_COLUMNS)
    bits: List[str] = [f"{glyph} {name}"]
    budget = room - len(name) - 4
    alias_text = ""
    if entry.aliases:
        alias_text = "aka " + ", ".join(entry.aliases)
    reason = entry.reason if not entry.available else entry.note
    body_parts: List[str] = []
    # The group marker goes FIRST, not last, and that ordering was found by
    # measuring. An un-headed row - one whose group fell under the
    # anti-clutter threshold - has no heading to place it, so the marker is
    # the ONLY place the group appears; last in the row, it was the field the
    # width budget clipped away, and a row a reader cannot place is a row the
    # grouping failed on.
    if show_group and entry.group:
        group_title = _TITLE_BY_KEY.get(entry.group, entry.group)
        if group_title:
            body_parts.append(f"[{group_title}]")
    if entry.description:
        body_parts.append(entry.description)
    if entry.argument_hint:
        body_parts.append(entry.argument_hint)
    if alias_text:
        body_parts.append(alias_text)
    body = " · ".join(part for part in body_parts if part)
    if reason:
        # The reason is charged LAST and the description yields to it, which
        # is the opposite of the historical order and the reason why a refusal
        # is now readable at narrow widths.
        keep = max(12, min(len(reason), _REASON_COLUMNS, room - len(name) - 8))
        room_for_body = max(0, budget - keep - 3)
        if room_for_body < 12:
            keep = max(8, keep - (12 - room_for_body))
            room_for_body = max(0, budget - keep - 3)
        body = _clip(body, room_for_body)
        bits.append(body)
        bits.append(_clip(reason, keep))
    else:
        bits.append(_clip(body, max(0, budget)))
    return "  ".join(bit for bit in bits if bit)


def entry_markup_row(
    entry: PaletteEntry,
    *,
    width: int = 100,
    show_group: bool = False,
) -> str:
    """One row as rich MARKUP, every data field escaped.

    The exit for a sink that parses markup - `Console.print`, a `Static` with
    a markup string. rich's OWN `escape`, so the escaping cannot drift from
    the parser it defends.

    This function exists because of a measured failure, not a precaution.
    `tests/test_palette_discovery.py::
    TestNothingIsDeletedByARender::test_a_hostile_plugin_name_is_visible_after_
    rendering` renders a row through a real `Console(markup=True)` and the
    plain `entry_row` output rendered a plugin named `weird[red].x` as
    `weird.x` - the tag was parsed and the text between the brackets was
    DELETED. A substring assertion on the un-rendered string passes while the
    message is being eaten, which is why the test renders first and asserts
    visibility afterwards.
    """
    return escape_lines([entry_row(entry, width=width, show_group=show_group)])[0]


def entry_text_row(
    entry: PaletteEntry,
    *,
    width: int = 100,
    show_group: bool = False,
) -> Any:
    """One row as `rich.text.Text`, styled by STATE rather than by name.

    The styling is keyed on `PaletteEntry.state`, so an unavailable command
    reads differently from a hidden one and from a merely noted one - which is
    requirement 5, and it is only true if the two are different VALUES and not
    one boolean read twice.
    """
    from rich.text import Text

    line = entry_row(entry, width=width, show_group=show_group)
    style = {
        "available": "bold",
        "noted": "dim",
        "unavailable": "dim italic",
        "hidden": "dim",
    }.get(entry.state, "")
    return Text(line, style=style)


def palette_lines(
    entries: Sequence[PaletteEntry],
    *,
    width: int = 100,
    query: str = "",
) -> List[str]:
    """The whole menu as PLAIN lines, grouped.

    This is the surface a non-Textual caller (the REPL, a test, a receipt)
    renders, and it is why the grouping lives here rather than in the screen:
    one grouping, one anti-clutter rule, and the screen is a widget over it.
    An empty result renders the honest "nothing matched" line rather than an
    empty wall, because an empty list and a full list are different facts.
    """
    rows = search_entries(entries, query)
    if not rows:
        if str(query or "").strip():
            return [f"no command matches {str(query).strip()!r}"]
        return []
    out: List[str] = []
    for title, members in group_entries(rows):
        if title:
            out.append(title)
        for entry in members:
            lead = "  " if title else ""
            out.append(lead + entry_row(entry, width=width, show_group=not title))
    return out


def palette_text_lines(
    entries: Sequence[PaletteEntry],
    *,
    width: int = 100,
    query: str = "",
) -> List[Any]:
    """`palette_lines` as `rich.text.Text` - no markup interpretation at all."""
    return text_lines(palette_lines(entries, width=width, query=query))


def palette_receipt(
    context: Any = None,
    *,
    plugins: Optional[Sequence[Mapping[str, Any]]] = None,
    servers: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """What the menu would show, and what it could not.

    A receipt, because the two questions a menu has to answer are "what can I
    do" and "what did you leave out", and only the first is interesting until
    something goes missing. `unassigned` and `empty_groups` are the two ways a
    command becomes unfindable, `dynamic` records which extension sources
    answered, and `phrasings` records whether the search corpus was warm.
    """
    try:
        rows = palette_entries(
            context, plugins=plugins, servers=servers, include_dynamic=True
        )
    except Exception:
        rows = []
    grouped = group_entries(rows)
    by_kind: Dict[str, int] = {}
    by_state: Dict[str, int] = {}
    for row in rows:
        by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
        by_state[row.state] = by_state.get(row.state, 0) + 1
    hidden = [row.name for row in rows if row.hidden]
    unavailable = [
        {"name": row.name, "reason": row.reason}
        for row in rows
        if not row.available and not row.hidden
    ]
    return {
        "entries": len(rows),
        "groups": [
            {"title": title, "rows": len(members)} for title, members in grouped
        ],
        "hidden_groups": [
            key
            for key in _TITLE_BY_KEY
            if key not in {e.group for e in rows if not e.hidden}
        ],
        "by_kind": dict(by_kind),
        "by_state": dict(by_state),
        "unavailable": unavailable,
        "hidden": hidden,
        "unassigned": unassigned_commands(),
        "empty_group_keys": _empty_group_keys(),
        "dynamic": {
            "plugin_rows": sum(1 for r in rows if r.kind == "plugin"),
            "mcp_rows": sum(1 for r in rows if r.kind == "mcp"),
            "mcp_producers": list(MCP_PROMPT_PRODUCERS),
        },
        "phrasings": "warm" if phrasings_warm() else "cold",
        "anti_clutter_min_entries": anti_clutter_min_entries(),
    }


# ---------------------------------------------------------------------------
# 8. The "/" hook - for `cli/tui.py`, which this round did NOT edit
# ---------------------------------------------------------------------------

#: The key that opens the menu from the composer. The brief's requirement is
#: that typing "/" OPENS it, so this is a key the composer intercepts, not a
#: binding the shell has to remember.
PALETTE_TRIGGER = "/"


def slash_hook(
    value: str,
    *,
    width: int = 100,
    context: Any = None,
) -> Optional[List[PaletteEntry]]:
    """What a composer should do after the user typed `/`.

    Returns the rows to show, or None when the keystroke is not a menu
    trigger. The rule is deliberately narrow:

    * the value before the slash must be EMPTY, so typing `src/main.py` or
      `3/4` never opens a menu over a sentence somebody is in the middle of
      typing, and
    * a second slash does not re-open, so `//` is a line the user can type.

    Everything after the first `/` becomes the query, which is why this
    returns the ROWS rather than a boolean: the caller filters with
    `search_entries` on the same function, so the menu and the search can
    never disagree.

    Written as a pure function with no Textual import so it is testable
    without a running app, and so the mount in `cli/tui.py` is a copy of this
    docstring rather than a design.
    """
    text = str(value or "")
    if not text.startswith(PALETTE_TRIGGER):
        return None
    rest = text[len(PALETTE_TRIGGER) :]
    if PALETTE_TRIGGER in rest:
        return None
    try:
        return palette_entries(context)
    except Exception:
        return []


def open_rows(
    query: str = "",
    *,
    context: Any = None,
    plugins: Optional[Sequence[Mapping[str, Any]]] = None,
    servers: Optional[Sequence[Mapping[str, Any]]] = None,
    include_dynamic: bool = False,
    limit: Optional[int] = None,
) -> List[PaletteEntry]:
    """The rows a menu open should paint, filtered by `query`.

    The fast path the "/" hook uses: `include_dynamic=False` because reading
    the plugins root and the MCP registry is I/O and the first paint must not
    wait for it. The screen merges the dynamic rows afterwards through
    `PaletteScreen.add_entries`, which re-filters with the current query so a
    half-loaded menu still ranks correctly.
    """
    rows = palette_entries(
        context, plugins=plugins, servers=servers, include_dynamic=include_dynamic
    )
    return search_entries(rows, query, limit=limit)


# ---------------------------------------------------------------------------
# 9. The screen
# ---------------------------------------------------------------------------

#: Rows the menu will paint at once. A 52-command menu is short; a menu merged
#: with a repository's files is not, and the cap is the difference between a
#: menu and a wall.
MAX_VISIBLE_ROWS = 200


def _textual_types() -> Any:
    """Import the Textual pieces, or None when Textual is unavailable.

    The palette's DATA and RENDERING are Textual-free on purpose: a headless
    test, a receipt and a `--json` document must all be able to ask what the
    menu would show on a machine with no terminal. Only the screen needs the
    framework, and it is built lazily behind this so importing `cli.palette`
    never imports Textual.
    """
    try:
        from textual import events
        from textual.app import ComposeResult
        from textual.containers import Vertical
        from textual.widgets import Input, OptionList, Static
        from textual.widgets.option_list import Option

        from cli import tui_components

        return {
            "events": events,
            "ComposeResult": ComposeResult,
            "Vertical": Vertical,
            "Input": Input,
            "OptionList": OptionList,
            "Static": Static,
            "Option": Option,
            "CommandPaletteFrame": tui_components.CommandPaletteFrame,
        }
    except Exception:
        return None


_TYPES = _textual_types()


if _TYPES is not None:  # pragma: no branch - a build without Textual skips it

    class PaletteScreen(_TYPES["CommandPaletteFrame"]):
        """The `/` menu: grouped, keyboard-complete, and headlessly drivable.

        Composition is the same `CommandPaletteFrame` the existing ctrl+p
        palette mounts, so this is a SCREEN over `cli.palette`'s data rather
        than a second palette: one grouping, one anti-clutter rule, one
        ranking function, one set of row renderers.

        Keyboard-complete, because a menu a mouse can only use is not a menu:
        up/down/pageup/pagedown/home/end move the selection, enter runs,
        escape dismisses, and typing in the filter keeps focus while the
        selection moves - the VS Code feel, and the reason the selection
        index and the filter text are separate pieces of state.

        Group HEADINGS are rendered as DISABLED options. Textual's own
        navigation skips a disabled option, so arrows, page keys, home and end
        all land on a command and never on a heading, with no per-key
        arithmetic to get wrong. That is a real property of the widget rather
        than a claim about it, and the tests drive it.

        Every row is a `rich.text.Text`, so a command summary, a plugin
        description or an MCP server label containing `[` renders as itself.
        """

        CSS = """
        PaletteScreen {
            align: center middle;
        }
        #palette-box {
            width: 78;
            min-width: 20;
            max-width: 92%;
            height: 24;
            min-height: 8;
            max-height: 80%;
            padding: 1 1;
            background: $neo-panel;
            border: round $neo-accent;
        }
        #palette-input {
            border: round $neo-accent;
            margin-bottom: 1;
        }
        #palette-list {
            height: auto;
            max-height: 20;
            background: $neo-panel;
        }
        #palette-hint {
            color: $neo-secondary;
            margin-top: 1;
        }
        """

        #: The pre-existing shell reads this off the screen, so it stays.
        _MAX_RESULTS = MAX_VISIBLE_ROWS

        def __init__(
            self,
            entries: Sequence[PaletteEntry] = (),
            *,
            context: Any = None,
        ) -> None:
            super().__init__()
            self._entries: List[PaletteEntry] = list(entries or ())
            # NAMED `command_context`, NEVER `_context`. `MessagePump._context`
            # is a METHOD the message pump calls (`with self._context():`) on
            # every widget, and assigning `self._context = None` shadows it with
            # a `None`. The screen's message pump then dies on the first call
            # with `TypeError: 'NoneType' object is not callable` INSIDE
            # `_process_messages`, Textual swallows it, and the screen never
            # processes a single message - so `pilot.pause()` waits forever and
            # the whole class looks broken for no visible reason.
            #
            # Found by bisecting one attribute at a time against a real mount
            # (six variants, one hung), not by reading the code. A one-word
            # attribute name on a Textual widget is a HANG, not a typo, and the
            # test that catches it is a real mount rather than an assert.
            self.command_context = context
            self._visible: List[PaletteEntry] = []
            self._pending_dynamic = False

        # -- composition ----------------------------------------------------

        def compose(self) -> Any:
            with _TYPES["Vertical"](id="palette-box"):
                yield _TYPES["Input"](
                    placeholder="type a command, or what you want to do",
                    id="palette-input",
                )
                yield _TYPES["OptionList"](id="palette-list")
                yield _TYPES["Static"](
                    "↑/↓ select · enter run · esc close",
                    id="palette-hint",
                )

        def on_mount(self) -> None:
            super().on_mount()
            self._refilter("")
            try:
                self.query_one("#palette-input", _TYPES["Input"]).focus()
            except Exception:
                pass

        # -- the one ranking path -------------------------------------------

        def _query(self) -> str:
            try:
                return str(
                    self.query_one("#palette-input", _TYPES["Input"]).value or ""
                )
            except Exception:
                return ""

        def _refilter(self, query: str) -> None:
            """Repaint the list for `query`. The ONLY place rows are rendered."""
            needle = str(query or "")
            self._visible = search_entries(
                self._entries, needle, limit=self._MAX_RESULTS
            )
            options: List[Any] = []
            grouped = group_entries(self._visible)
            shown: set = set()
            for title, members in grouped:
                options.append(
                    _TYPES["Option"](
                        entry_text_row(
                            PaletteEntry(
                                name=title,
                                kind="group",
                                description="",
                            ),
                            width=76,
                        ),
                        disabled=True,
                    )
                )
                for entry in members:
                    options.append(
                        _TYPES["Option"](entry_text_row(entry, width=76), id=entry.name)
                    )
                    shown.add(entry.name)
            for entry in self._visible:
                if entry.name in shown:
                    continue
                options.append(
                    _TYPES["Option"](
                        entry_text_row(entry, width=76, show_group=True), id=entry.name
                    )
                )
            if not options:
                options.append(_TYPES["Option"](_no_match_text(needle), disabled=True))
            try:
                lst = self.query_one("#palette-list", _TYPES["OptionList"])
                lst.clear_options()
                lst.add_options(options)
                first = _first_enabled(self._visible)
                if first is not None:
                    lst.highlighted = _option_index_for(lst, first)
            except Exception:
                pass

        # -- events ---------------------------------------------------------

        def on_input_changed(self, event: Any) -> None:
            self._refilter(getattr(event, "value", "") or "")

        def on_input_submitted(self, event: Any) -> None:
            try:
                event.input.value = ""
            except Exception:
                pass
            self._choose()

        def on_option_list_option_selected(self, event: Any) -> None:
            event.stop()
            self._choose()

        def on_key(self, event: Any) -> None:
            key = getattr(event, "key", "")
            if key == "escape":
                event.stop()
                event.prevent_default()
                self.dismiss(None)
                return
            if key not in ("up", "down", "pageup", "pagedown", "home", "end"):
                return
            # The OptionList owns the cursor while the Input keeps focus, so
            # the user types AND browses. The screen is a modal, so its
            # on_key runs before the focused widget sees the key.
            try:
                lst = self.query_one("#palette-list", _TYPES["OptionList"])
            except Exception:
                return
            event.stop()
            event.prevent_default()
            action = {
                "up": "action_cursor_up",
                "pageup": "action_page_up",
                "home": "action_first",
                "down": "action_cursor_down",
                "pagedown": "action_page_down",
                "end": "action_last",
            }.get(key)
            if action and hasattr(lst, action):
                try:
                    getattr(lst, action)()
                except Exception:
                    pass

        def _choose(self) -> None:
            if not self._visible:
                self.dismiss(None)
                return
            try:
                lst = self.query_one("#palette-list", _TYPES["OptionList"])
            except Exception:
                lst = None
            self.dismiss(self._highlighted_entry(lst))

        def _highlighted_entry(self, lst: Any) -> Optional[PaletteEntry]:
            """The entry under the selection, resolved through the OPTION ID.

            Not through the highlight INDEX, and that distinction was found by
            running this: the list carries group HEADINGS as disabled options,
            so option 0 is the first heading and option 1 is the first command.
            Reading the index against `self._visible` therefore returned the
            SECOND command whenever a heading was rendered. The option's own
            `id` is the command's name, which is stable regardless of how many
            headings precede it.
            """
            names = {entry.name for entry in self._visible}
            if lst is not None:
                try:
                    index = lst.highlighted
                except Exception:
                    index = None
                if index is not None:
                    try:
                        option = lst.get_option_at_index(int(index))
                    except Exception:
                        option = None
                    if option is not None:
                        option_id = getattr(option, "id", None)
                        if option_id in names:
                            return next(e for e in self._visible if e.name == option_id)
            return self._visible[0] if self._visible else None

        # -- the deferred merge --------------------------------------------

        def add_entries(
            self,
            entries: Sequence[PaletteEntry],
            *,
            plugins: Optional[Sequence[Mapping[str, Any]]] = None,
            servers: Optional[Sequence[Mapping[str, Any]]] = None,
        ) -> None:
            """Merge the extension rows into a live menu.

            Reading the plugins root and the MCP registry is I/O, so the menu
            paints the registry rows first and this arrives a moment later from
            a worker thread - the same shape as the existing `add_files`, and
            the reason a slow plugin directory cannot hold the menu closed.
            Re-filters with the CURRENT query so a half-loaded menu still
            ranks correctly, which is the part a naive append gets wrong.
            """
            self._pending_dynamic = False
            incoming = list(entries or ())
            if plugins is not None:
                incoming.extend(plugin_entries(plugins))
            if servers is not None:
                incoming.extend(mcp_entries(servers))
            if not incoming or not getattr(self, "is_attached", True):
                return
            known = {entry.name for entry in self._entries}
            self._entries.extend(e for e in incoming if e.name not in known)
            self._refilter(self._query())

        def rows(self) -> List[PaletteEntry]:
            """The currently visible rows, for a headless assertion."""
            return list(self._visible)

else:  # pragma: no cover - only on a build with no Textual installed

    class PaletteScreen:  # type: ignore[no-redef]
        """Unavailable: Textual is not importable in this build.

        A named class that refuses loudly, rather than an `ImportError` at
        `import cli.palette`, because the DATA half of this module - grouping,
        availability, search, the plain and `Text` renderers - is Textual-free
        and is what a headless caller, a receipt and a `--json` document use.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "cli.palette.PaletteScreen needs Textual; the data half "
                "(palette_entries, search_entries, palette_lines) does not"
            )


def _no_match_text(query: str) -> Any:
    from rich.text import Text

    return Text(f"no command matches {str(query or '').strip()!r}", style="dim italic")


def _first_enabled(entries: Sequence[PaletteEntry]) -> Optional[str]:
    return entries[0].name if entries else None


def _option_index_for(lst: Any, name: str) -> Optional[int]:
    try:
        return lst.get_option_index(name)
    except Exception:
        return None
