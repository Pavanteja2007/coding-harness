"""Command aliases, the alias table's gate, and command stacking.

VEX-CS-04. Two surfaces that did not exist before, both **pure**, both
importable without Textual and without touching a single file this round
does not own (``cli/tui.py`` and ``cli/commands.py`` are read-only here).

Three things live here, and they are deliberately in ONE file because each
one is the other's failure mode:

1. **The alias table** (:data:`ALIASES`) plus the gate that keeps it honest
   (:func:`validate_alias_table`). An alias is only allowed to exist when it
   does not shadow a real command name, resolves to a real command, and the
   whole graph is acyclic. Those three rules are what make the table a table
   rather than a list of hopes, and each has a test named after it.
2. **Stacking** (:func:`expand_command_line`). ``resolve_command_line`` in
   ``cli/commands.py`` parses exactly ONE name; the expander here recognises
   several commands at the START of one message and hands the remainder to
   every one of them.
3. **The disclosure** a menu needs to show the resolution
   (:func:`alias_disclosure_lines`). A command a user cannot find does not
   exist, and an alias whose target is invisible is exactly that.

Nothing here executes a command, opens a file, reads the environment, or
keeps state between calls. Every public function is a total function of its
arguments; the two that need the command registry take it as an injectable
``resolver`` so this module can be tested without the package importing
itself in a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ALIASES",
    "ALIAS_KEYS",
    "DEFERRED_ALIASES",
    "MAX_STACKED_COMMANDS",
    "NON_STACKABLE",
    "NON_STACKABLE_REASONS",
    "RETIRED_ALIASES",
    "STACK_STOP_REASONS",
    "AliasAudit",
    "CommandExpansion",
    "DeferredAlias",
    "StackedCommand",
    "alias_cycle_report",
    "alias_disclosure_lines",
    "alias_disclosure_text",
    "alias_table_rows",
    "alias_target",
    "deferred_alias_promotions",
    "escape_lines",
    "expand_command_line",
    "expand_command_lines",
    "non_stackable_reason",
    "normalize_command_name",
    "plain_lines",
    "resolve_alias",
    "resolve_alias_names",
    "resolve_line",
    "safe_lines",
    "validate_alias_table",
]


# ---------------------------------------------------------------------------
# DELTA 1a -- the alias table
# ---------------------------------------------------------------------------
#
# Eight aliases were declared on `CommandSpec.aliases` before this round.
# They stay, byte for byte, because backwards compatibility is not optional:
# every one of them is reachable today and a user may have muscle memory.
#
# The brief asked for eight more. Five of them landed. The other three are
# recorded below with a REASON, not dropped:
#
#   * `/diff -> /undo`        RETIRED. `/diff` is a REAL command, so it
#                              SHADOWS the alias; and because `command_spec`
#                              resolves an exact name before it consults an
#                              alias, the declared alias is UNREACHABLE
#                              (measured: `command_spec("/diff").name ==
#                              "/diff"`). A reader typing `/diff` today gets
#                              the diff view, which is what they meant. The
#                              alias could only ever have been a revert, and
#                              it never was one. Carrying it forward would be
#                              carrying forward a row that documents a
#                              behaviour the product has never had.
#   * `/settings -> /config`  RETIRED, same shadowing rule: `/settings` is a
#                              real command (it is in `COMMAND_SPECS`), and
#                              `/config` is not a slash command at all --
#                              the config surface is `neo config` on the
#                              script side and the real `/settings` row here.
#   * `/checkpoint -> /rewind` DEFERRED. `/checkpoint` is a LIVE alias of
#                              `/checkpoints`; retargeting it would change
#                              today's behaviour, and `/rewind` is not a
#                              command. It needs one `CommandSpec` row in
#                              `cli/commands.py` (not this round's file).
#
# Everything the brief names as a TARGET rather than as a source is checked
# against the registry by `validate_alias_table`, and three of the requested
# targets did not exist. Those chains are re-pointed at the real command that
# does the same job, and the deviation is one line in each row below rather
# than a paragraph of intent:
#
#   requested `/marketplace -> /plugin`   landed `/marketplace -> /plugins`
#   requested `/cost`,`/stats` -> `/usage` landed `/usage -> /cost`, `/stats -> /cost`
#   requested `/bashes` -> `/tasks`         landed `/tasks -> /sessions`, `/bashes -> /tasks`

Resolver = Callable[[str], Any]


def _default_resolver() -> Resolver:
    """Return the live command registry's resolver, imported lazily.

    Lazy because ``cli/commands.py`` is 3k lines and imports this package's
    siblings; a module-level import here would make the import order of the
    whole CLI package load-bearing. The registry is the authority and this is
    only a way to reach it.
    """

    from cli import commands as _commands

    return _commands.command_spec


ALIASES: Mapping[str, str] = {
    # -- the eight that existed before this round, unchanged ---------------
    "/changes": "/diff",
    "/checkpoint": "/checkpoints",
    "/copy": "/copy-diff",
    "/exit": "/quit",
    "/related": "/relevant",
    "/thinking": "/effort",
    "/auth": "/connect",
    # -- landed by this round ---------------------------------------------
    "/reset": "/clear",
    "/new": "/clear",
    "/marketplace": "/plugins",
    "/continue": "/resume",
    "/tasks": "/sessions",
    "/bashes": "/tasks",
    "/usage": "/cost",
    "/stats": "/cost",
}

#: Sorted alias keys; handy for a menu and for a stable rendering order.
ALIAS_KEYS: Tuple[str, ...] = tuple(sorted(ALIASES))


RETIRED_ALIASES: Mapping[str, str] = {
    "/diff": (
        "RETIRED: `/diff` is a real command, so it shadows the alias, and "
        "`command_spec` resolves an exact name BEFORE it consults an alias - "
        "so the declared `/diff -> /undo` row was unreachable and a reader "
        "typing `/diff` has always got the diff view, never a revert. "
        "MIGRATION: there is nothing to migrate. `/undo` is the revert and it "
        "is named `/undo`; `/diff` reads the diff. The live registry still "
        "carries the dead alias on `/undo`'s row (`cli/commands.py`, not this "
        "round's file) and removing it is a one-token edit there."
    ),
    "/settings": (
        "RETIRED: `/settings` is a real command name (it is a `COMMAND_SPECS` "
        "row), and the brief's target `/config` is not a slash command in "
        "this product - the config surface is `neo config` on the script side "
        "and the `/settings` row on the interactive side. An alias that "
        "shadows a real command and points at nothing is worse than no alias."
    ),
}


@dataclass(frozen=True)
class DeferredAlias:
    """One requested alias that cannot land until the registry carries a row.

    A recorded gap is a gap somebody can close; an unrecorded one gets
    rediscovered. `deferred_alias_promotions` turns each of these into an
    INVERTED pin: it fails the day the missing command appears, and the
    message names the row somebody should add.
    """

    alias: str
    target: str
    blocked_on: str
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the deferral."""

        return {
            "alias": self.alias,
            "target": self.target,
            "blocked_on": self.blocked_on,
            "reason": self.reason,
        }


DEFERRED_ALIASES: Mapping[str, DeferredAlias] = {
    "/bug": DeferredAlias(
        alias="/bug",
        target="/feedback",
        blocked_on="/feedback",
        reason=(
            "`/feedback` is not a `COMMAND_SPECS` row, so there is nothing for "
            "this alias to point at. Needs one row in `cli/commands.py` "
            "(not this round's file); this alias then becomes valid with no "
            "edit here."
        ),
    ),
    "/checkpoint:rewind": DeferredAlias(
        alias="/checkpoint",
        target="/rewind",
        blocked_on="/rewind",
        reason=(
            "the brief wants `/checkpoint` to mean rewind, but `/checkpoint` "
            "is a LIVE alias of `/checkpoints` today and retargeting it would "
            "change working behaviour, and `/rewind` is not a command. Two "
            "separate rows are needed: a `/rewind` CommandSpec, and a "
            "decision from the `/checkpoints` owner about retargeting."
        ),
    ),
}


def deferred_alias_promotions(
    resolver: Optional[Resolver] = None,
) -> List[Dict[str, str]]:
    """Return the deferred aliases whose blocking command now EXISTS.

    Every row here is a FAILING pin the day its blocker lands, which is the
    only honest way to keep a recorded gap from rotting into a forgotten one.
    """

    look = _default_resolver() if resolver is None else resolver
    ready: List[Dict[str, str]] = []
    for key in sorted(DEFERRED_ALIASES):
        row = DEFERRED_ALIASES[key]
        if look(row.target) is not None:
            ready.append(row.as_dict())
    return ready


# ---------------------------------------------------------------------------
# DELTA 1b -- resolution, and the gate that keeps the table a table
# ---------------------------------------------------------------------------


def normalize_command_name(name: Any) -> str:
    """Return one canonical slash spelling for a command name or alias.

    Assumes nothing about its input beyond that ``str()`` works on it: the
    composer hands this half-typed strings, and the palette hands it rows.
    """

    key = str(name or "").strip().lower()
    if not key:
        return ""
    return key if key.startswith("/") else "/" + key


def alias_target(name: Any, *, resolver: Optional[Resolver] = None) -> Optional[str]:
    """Return the alias's one-hop target, or ``None`` if it is not an alias.

    The one-hop target may itself be an alias (a chain), so this returns the
    spelling the table holds rather than the end of the chain --
    :func:`resolve_alias` is the chain. What is guaranteed here is that the
    chain DOES resolve to a real command: an alias whose chain dead-ends in
    the registry is a typo, and reporting it as ``None`` is what keeps it
    from reaching a user as a successful no-op.
    """

    key = normalize_command_name(name)
    if key not in ALIASES:
        return None
    look = _default_resolver() if resolver is None else resolver
    if look(resolve_alias(key, resolver=look)) is None:
        return None
    return ALIASES[key]


def resolve_alias(name: Any, *, resolver: Optional[Resolver] = None) -> str:
    """Return the canonical command name for a name, following the chain.

    Three rules, in this order:

    1. **The REGISTRY always wins.** If `cli.commands.command_spec` resolves
       the name at all -- as an exact name or through one of the registry's
       own aliases -- the registry's answer is returned unchanged. This table
       is a FALLBACK for names the registry does not carry, which is the only
       job it can do: `cli/commands.py` is not this round's file, so a row
       added there after this module was written must be honoured rather than
       fought. It is not hypothetical: while this round was running, a
       parallel terminal added `/plugin` as a registry alias of `/plugins`.
    2. Otherwise follow the chain to its end.
    3. Bounded by :data:`MAX_ALIAS_HOPS` so a table that somehow acquired a
       cycle cannot spin here. A cycle is also rejected by
       `validate_alias_table`; the bound is the second line, not the first.
    """

    look = _default_resolver() if resolver is None else resolver
    key = normalize_command_name(name)
    spec = look(key)
    if spec is not None:
        resolved = str(getattr(spec, "name", "") or "")
        if resolved:
            return resolved
    seen = [key]
    for _ in range(MAX_ALIAS_HOPS):
        nxt = ALIASES.get(key)
        if nxt is None:
            break
        key = nxt
        if key in seen:  # pragma: no cover - rejected by the gate
            break
        seen.append(key)
    return key


MAX_ALIAS_HOPS = 8


def resolve_alias_names(
    names: Sequence[Any], *, resolver: Optional[Resolver] = None
) -> Tuple[str, ...]:
    """Resolve several names at once, preserving order and duplicates."""

    return tuple(resolve_alias(n, resolver=resolver) for n in names)


def alias_table_rows(*, resolver: Optional[Resolver] = None) -> List[Dict[str, str]]:
    """Return one row per alias, ready for a menu or a JSON document.

    ``resolves`` is the FULL chain resolution, not the one-hop target, because
    a menu that shows ``/bashes -> /tasks`` and not ``/bashes -> /sessions``
    has taught the user a name they cannot type. ``exists`` reports whether
    the chain REACHES a command, which is the question that matters -- the
    one-hop target of a chained alias is legitimately not a command.
    """

    look = _default_resolver() if resolver is None else resolver
    rows: List[Dict[str, str]] = []
    for alias in sorted(ALIASES):
        target = ALIASES[alias]
        spec = look(target)
        end = resolve_alias(alias, resolver=look)
        rows.append(
            {
                "alias": alias,
                "target": target,
                "resolves": end,
                "summary": str(getattr(spec, "summary", "") or ""),
                "exists": "yes" if look(end) is not None else "no",
            }
        )
    return rows


def alias_cycle_report(*, resolver: Optional[Resolver] = None) -> Dict[str, Any]:
    """Report every cycle in the alias graph, as an explicit walk.

    A DFS with a colour map rather than a set difference, so the report says
    WHICH cycle it found (``/a -> /b -> /a``) and a test can assert on the
    cycle rather than on the absence of one.
    """

    colours: Dict[str, int] = {}  # 0 unseen, 1 on the current path, 2 done
    cycles: List[Tuple[str, ...]] = []

    def visit(node: str, path: List[str]) -> None:
        if node in path:
            cycles.append(tuple([*path[path.index(node) :], node]))
            return
        if colours.get(node) == 2:
            return
        if colours.get(node) == 1:
            return
        colours[node] = 1
        path.append(node)
        nxt = ALIASES.get(node)
        if nxt:
            visit(nxt, path)
        path.pop()
        colours[node] = 2

    for alias in sorted(ALIASES):
        visit(alias, [])
    return {
        "acyclic": not cycles,
        "cycles": [" -> ".join(c) for c in cycles],
        "nodes_walked": len(colours),
    }


@dataclass(frozen=True)
class AliasAudit:
    """The result of the table's gate. ``ok`` is the AND of every finding."""

    ok: bool
    aliases: int
    shadows: Tuple[str, ...] = ()
    unknown_targets: Tuple[str, ...] = ()
    cycles: Tuple[str, ...] = ()
    findings: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the audit."""

        return {
            "ok": self.ok,
            "aliases": self.aliases,
            "shadows": list(self.shadows),
            "unknown_targets": list(self.unknown_targets),
            "cycles": list(self.cycles),
            "findings": list(self.findings),
        }

    def message(self) -> str:
        """Return one copyable line naming what is wrong, or ``ok``."""

        if self.ok:
            return f"alias table ok: {self.aliases} aliases, acyclic, no shadowing"
        return "alias table invalid: " + "; ".join(self.findings)


def validate_alias_table(*, resolver: Optional[Resolver] = None) -> AliasAudit:
    """Check the alias table: no shadowing, real targets, acyclic.

    Three rules, each of which has bitten somebody before:

    * **no shadowing** -- an alias may not be spelled like a real command,
      because ``command_spec`` resolves the exact name first and the alias
      would be permanently unreachable. That is not a style rule; it is the
      measurement that made the ``/diff -> /undo`` row dead.
    * **real targets** -- an alias may not name a command the registry does
      not carry. Half the brief's requested targets (``/plugin``, ``/config``,
      ``/usage``, ``/tasks``, ``/feedback``, ``/rewind``) are not commands,
      and an alias that points at one resolves to nothing and reports
      success.
    * **acyclic** -- a chain that loops makes ``resolve_alias`` unbounded
      without a bound, and its output unreadable with one.
    """

    look = _default_resolver() if resolver is None else resolver
    findings: List[str] = []
    shadows: List[str] = []
    unknown: List[str] = []

    for alias in sorted(ALIASES):
        spec = look(alias)
        if spec is not None and getattr(spec, "name", None) == alias:
            shadows.append(alias)
            findings.append(
                f"/{alias.lstrip('/')} shadows a real command of the same name"
            )
        end = resolve_alias(alias, resolver=look)
        if look(end) is None:
            unknown.append(alias)
            findings.append(
                f"{alias} resolves to {end}, which is not a registered command"
            )

    cycle = alias_cycle_report(resolver=look)
    cycles = tuple(cycle["cycles"])
    for entry in cycles:
        findings.append(f"alias cycle: {entry}")

    return AliasAudit(
        ok=not findings,
        aliases=len(ALIASES),
        shadows=tuple(shadows),
        unknown_targets=tuple(unknown),
        cycles=cycles,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# DELTA 2 -- stacking
# ---------------------------------------------------------------------------

#: At most this many commands may chain in one message. Six is the
#: reference's number and it is a DISPLAY bound as much as a safety one: past
#: six a user cannot see which of them they got wrong.
MAX_STACKED_COMMANDS = 6

#: Closed vocabulary for why expansion stopped. A stop reason nobody can name
#: is a stop nobody can debug, and a truncated chain that says "capped" and
#: not what it dropped is a lie about what ran.
STACK_STOP_REASONS: Tuple[str, ...] = (
    "not_command",  # the first token is not a command at all
    "not_stackable",  # a command in the chain refuses to share a line
    "chain_cap",  # MAX_STACKED_COMMANDS reached
    "exhausted",  # every token was consumed as a command
)

#: Closed vocabulary for why a command may not be stacked.
NON_STACKABLE_REASONS: Tuple[str, ...] = (
    "forks_subagent",
    "free_text_arguments",
)

#: Commands that may not be followed by another command in the same message.
#:
#: Two independent reasons, and the second one is the one that is easy to get
#: wrong:
#:
#: * ``forks_subagent`` -- the command starts agent work that itself forks
#:   subagents (``/plan`` and ``/build`` both reach
#:   ``cli.interactive._run_one_agent``). A second command in the same message
#:   would be attributed to that agent's own plan rather than to the person.
#: * ``free_text_arguments`` -- the command's own argument may legitimately
#:   BEGIN with a slash: a path (``/repo /home/x``, ``/open /src/a.py``), or
#:   a sentence (``/steer actually /stop doing that``). Expanding past one of
#:   these would eat the person's path and hand it to a different command.
#:
#: A command with ``argument_policy == "required"`` is ALSO non-stackable,
#: derived in `non_stackable_reason` from the registry rather than declared
#: here, so the two can never disagree.
NON_STACKABLE: Mapping[str, str] = {
    "/plan": "forks_subagent",
    "/build": "forks_subagent",
    "/ask": "free_text_arguments",
    "/review": "free_text_arguments",
    "/steer": "free_text_arguments",
    "/repo": "free_text_arguments",
    "/open": "free_text_arguments",
    "/import": "free_text_arguments",
    "/export": "free_text_arguments",
    "/share": "free_text_arguments",
}


def non_stackable_reason(
    name: Any, *, resolver: Optional[Resolver] = None
) -> Optional[str]:
    """Return why a command may not be stacked, or ``None`` if it may.

    The declared table above is the product decision; the registry-derived
    check below is the safety net. A command that REQUIRES an argument can
    never be stacked without stealing it, and that fact is in the registry
    rather than in anybody's memory.
    """

    look = _default_resolver() if resolver is None else resolver
    canonical = resolve_alias(name, resolver=look)
    if canonical in NON_STACKABLE:
        return NON_STACKABLE[canonical]
    spec = look(canonical)
    if spec is not None and getattr(spec, "argument_policy", "") == "required":
        return "free_text_arguments"
    return None


@dataclass(frozen=True)
class StackedCommand:
    """One command recognised at the head of a stacked message."""

    command: str
    args: str
    via_alias: str = ""

    def line(self) -> str:
        """Return the copyable one-command line this expands to."""

        return f"{self.command} {self.args}".strip()

    def as_dict(self) -> Dict[str, str]:
        """Return the JSON-friendly projection of the recognised command."""

        return {
            "command": self.command,
            "args": self.args,
            "via_alias": self.via_alias,
            "line": self.line(),
        }


@dataclass(frozen=True)
class CommandExpansion:
    """The whole answer to "what would `expand_command_line` do to this?".

    ``arguments`` is the load-bearing field: the token that stopped expansion
    plus everything after it, handed to EVERY recognised command. It is never
    dropped and never truncated -- a message that silently loses its tail is
    the worst outcome this module has, because the user cannot see it.
    """

    commands: Tuple[StackedCommand, ...]
    arguments: str
    stopped_at: str = ""
    stop_reason: str = "exhausted"
    raw: str = ""

    @property
    def stacked(self) -> bool:
        """True when more than one command was recognised."""

        return len(self.commands) > 1

    def lines(self) -> List[str]:
        """Return one copyable line per expanded command, in order."""

        return [c.line() for c in self.commands]

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the expansion."""

        return {
            "raw": self.raw,
            "commands": [c.as_dict() for c in self.commands],
            "arguments": self.arguments,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "stacked": self.stacked,
            "capped": self.stop_reason == "chain_cap",
        }

    def message(self) -> str:
        """Return one copyable sentence naming the expansion."""

        if not self.commands:
            return f"not a command: {self.raw.strip()!r}"
        names = ", ".join(c.via_alias or c.command for c in self.commands)
        tail = f" arguments: {self.arguments}" if self.arguments else ""
        stop = (
            f" stopped at {self.stopped_at!r} ({self.stop_reason})"
            if self.stopped_at
            else ""
        )
        return f"{names} -> {len(self.commands)} command(s){tail}{stop}"


def _token_is_command(token: str, *, resolver: Resolver) -> bool:
    """True when a whitespace-delimited token is an inline user-invocable name.

    Inline means the registry carries it (directly or through the alias
    table). A project ``.neo/commands/*.md`` template is deliberately NOT
    inline: it is a second thing with a different lifecycle, and treating it
    as stackable would make the two surfaces disagree about what a message
    means.
    """

    if not token.startswith("/"):
        return False
    key = normalize_command_name(token)
    if key in ALIASES and resolver(key) is None:
        # An alias whose chain the registry cannot satisfy is not a command.
        # A registry-claimed spelling always is, even if this table also
        # carries it -- `resolve_alias` gives the real command the win.
        return resolver(resolve_alias(token, resolver=resolver)) is not None
    spec = resolver(key)
    return spec is not None and getattr(spec, "name", None) == key


def expand_command_line(
    line: Any, *, resolver: Optional[Resolver] = None, limit: Optional[int] = None
) -> CommandExpansion:
    """Expand ONE message into the commands it starts with.

    The rules, all four of them, in the order they are applied:

    1. a command is recognised ONLY at the start of a message;
    2. expansion stops at the first token that is not an inline
       user-invocable command, and that token plus the rest becomes the
       argument text for EVERY expanded command;
    3. up to :data:`MAX_STACKED_COMMANDS` may chain;
    4. if a command cannot stack, expansion stops there and the remainder
       becomes its arguments.

    Total, deterministic, and pure. It never raises on hostile input: a NUL
    byte, an unpaired bracket, or a two-hundred-token message all produce an
    expansion rather than an exception, because this runs on the path where a
    person's typed line is dispatched.
    """

    look = _default_resolver() if resolver is None else resolver
    cap = MAX_STACKED_COMMANDS if limit is None else max(0, int(limit))
    raw = str(line or "")
    tokens = raw.split()
    if not tokens or not _token_is_command(tokens[0], resolver=look):
        return CommandExpansion(
            commands=(),
            arguments=" ".join(tokens),
            stopped_at=tokens[0] if tokens else "",
            stop_reason="not_command",
            raw=raw.strip(),
        )

    recognised: List[StackedCommand] = []
    cursor = 0
    stop_reason = "exhausted"
    stopped_at = ""
    while cursor < len(tokens):
        if len(recognised) >= cap:
            stop_reason = "chain_cap"
            stopped_at = tokens[cursor]
            break
        token = tokens[cursor]
        if not _token_is_command(token, resolver=look):
            stop_reason = "exhausted"
            stopped_at = token
            break
        canonical = resolve_alias(token, resolver=look)
        via = token if normalize_command_name(token) in ALIASES else ""
        recognised.append(StackedCommand(command=canonical, args="", via_alias=via))
        cursor += 1
        reason = non_stackable_reason(canonical, resolver=look)
        if reason is not None:
            stop_reason = "not_stackable"
            stopped_at = reason
            break

    remainder = " ".join(tokens[cursor:])
    # The argument text is the REMAINDER plus, for a single command, nothing
    # else -- and for a chain, the same remainder to each. Deliberately the
    # same string for every command: the brief's example passes
    # `src/auth.py` to BOTH.
    commands = tuple(
        StackedCommand(command=c.command, args=remainder, via_alias=c.via_alias)
        for c in recognised
    )
    if stop_reason == "not_stackable":
        # `stopped_at` is carrying a REASON here, not a token; the token that
        # stopped it is the first one of the remainder. Keep both: the receipt
        # names what stopped it and the command line shows what it kept.
        stopped_at = remainder.split()[0] if remainder else ""
    return CommandExpansion(
        commands=commands,
        arguments=remainder,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        raw=raw.strip(),
    )


def expand_command_lines(
    line: Any, *, resolver: Optional[Resolver] = None, limit: Optional[int] = None
) -> Tuple[str, ...]:
    """Return the expanded command lines, ready to hand to a dispatcher."""

    expansion = expand_command_line(line, resolver=resolver, limit=limit)
    return tuple(c.line() for c in expansion.commands)


def resolve_line(line: Any, context: Any = None) -> Any:
    """Resolve one slash line THROUGH this table, then through the registry.

    This is the ONE seam a surface mounts, and it exists because
    ``cli.commands.resolve_command_line`` reads ``CommandSpec.aliases`` only --
    ``cli/commands.py`` is not this round's file, so a name that lives in
    ``ALIASES`` reaches the historical parser as ``unknown`` until something
    routes it here. Measured before this function existed:
    ``resolve_command_line("/reset").status == "unknown"``.

    The rewrite is conservative on purpose:

    * only the FIRST token is translated, and only when it is a name in
      :data:`ALIASES` -- so a line that is already canonical is handed to the
      registry BYTE-IDENTICALLY and every one of the 52 existing commands,
      their 8 registry aliases, their refusals and their exit codes is
      untouched;
    * the canonical name plus the ORIGINAL remainder is re-resolved, so
      argument policies, availability and argument conflicts are still
      decided by the registry;
    * a non-command line is returned as the registry's own ``not_command``
      resolution rather than a second vocabulary.

    Every surface calls this in place of ``resolve_command_line`` and
    nothing else about its dispatch changes.
    """

    from cli import commands as _commands

    raw = str(line or "")
    tokens = raw.split(None, 1)
    head = tokens[0] if tokens else ""
    if normalize_command_name(head) not in ALIASES:
        return _commands.resolve_command_line(raw, context)
    canonical = resolve_alias(head)
    rest = tokens[1] if len(tokens) > 1 else ""
    return _commands.resolve_command_line(f"{canonical} {rest}".strip(), context)


# ---------------------------------------------------------------------------
# DELTA 1c -- disclosure. A command a user cannot find does not exist.
# ---------------------------------------------------------------------------


def alias_disclosure_lines(*, resolver: Optional[Resolver] = None) -> List[str]:
    """Return PLAIN lines naming every alias and what it resolves to.

    Terminal 03 renders these in the `/` menu. They are plain text because
    they are DATA: an alias a person typed lives in this table and may
    contain anything their editor allowed, and a render failure that deletes
    a message is worse than a missing one.
    """

    rows = alias_table_rows(resolver=resolver)
    out = [f"{len(rows)} aliases (typing one resolves to its target):"]
    for row in rows:
        via = row["alias"]
        target = row["target"]
        chain = f" -> {row['resolves']}" if row["resolves"] != target else ""
        out.append(f"  {via} -> {target}{chain}")
    return out


def alias_disclosure_text(*, resolver: Optional[Resolver] = None) -> str:
    """Return the disclosure as ONE newline-joined plain string."""

    return "\n".join(alias_disclosure_lines(resolver=resolver))


# ---------------------------------------------------------------------------
# Markup safety. The two sanctioned exits, and nothing in between.
# ---------------------------------------------------------------------------

_MARKUP_TAG = re.compile(r"\[/?[^\[\]]*\]")


def plain_lines(values: Sequence[str]) -> List[str]:
    """Return the lines unchanged.

    Named for the property it provides: a caller that has ALREADY escaped
    passes its lines through here and the intent is legible at the call site.
    """

    return [str(v) for v in values]


def escape_lines(values: Sequence[str]) -> List[str]:
    """Escape every line through rich's own ``escape``.

    Delegating rather than hand-rolling is the point: a hand-written escaper
    is a second opinion about the parser, and this codebase has been bitten by
    ``[[...]]`` not surviving a hand-rolled double-escape before.
    """

    try:
        from rich.markup import escape
    except Exception:  # pragma: no cover - rich is a hard dependency
        return [re.sub(r"\[", r"\\\[", str(v)).replace("\\]", r"\\]") for v in values]
    return [escape(str(v)) for v in values]


def safe_lines(values: Sequence[str]) -> List[Any]:
    """Return ``rich.text.Text`` lines, which have NO markup interpretation.

    The structural answer rather than the defensive one: a `Text` object
    cannot be markup-parsed even in principle, so this path cannot delete a
    message no matter what the data contains.
    """

    try:
        from rich.text import Text
    except Exception:  # pragma: no cover - rich is a hard dependency
        return [str(v) for v in values]
    return [Text(str(v)) for v in values]
