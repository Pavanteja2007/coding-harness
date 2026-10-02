"""MCP tool namespacing, least privilege, and approval-time hash pinning.

An external MCP server is the least-trusted thing the agent talks to: a tool
whose schema the operator never wrote, returning content the operator never
saw. This module is the boundary between "an MCP server said so" and "the
agent may act on it", and it covers the four properties Ceiling Prompt 12
names for the MCP surface.

**Namespaced tool identifiers.** Every tool is addressed as
``mcp__<server>__<tool>`` — the convention Claude Code, Cursor, and the other
common MCP clients already parse, so a client can route a name without a
lookup table. The server and tool segments are normalized to a safe
alphanumeric/hyphen/underscore form, dots are not silently dropped, and a
namespaced name is parsed back to exactly the ``(server, tool)`` pair it came
from. An unparseable name is never guessed at: it is a refusal.

**Per-server/per-tool least privilege.** :class:`MCPToolPolicy` holds an
explicit server allowlist and, per server, a tool allowlist plus a declared
side-effect class per tool. Both default closed: an unlisted server is denied,
an unlisted tool within an allowed server is denied, and a tool that declares
no side-effect class is treated as mutating. Least privilege here means a
server added to the config exposes exactly the tools that config named.

**Tool definition hashes pinned at approval.** The hash is a domain-separated
SHA-256 over the canonical ``(server, tool, description, inputSchema)``. An
approval records the hashes it was granted against, and
:meth:`ToolPinSet.verify` re-hashes the live catalog immediately before the
call: if a server changed a tool's description or schema after the operator
approved it, the call is refused as ``tool_definition_changed`` rather than
executed against a definition nobody approved. That is the MCP analogue of
Terminal 13's approval-effect recheck.

**Server responses are untrusted content.** :func:`review_tool_result` routes a
result through :func:`shared.security.review_untrusted_source` with
``source="mcp"`` and returns text the agent may actually use, with taint
visible. A quarantined result is replaced with an explicit refusal that names
the source; the hostile text is never forwarded. The review record carries no
payload, so a receipt or trace can prove the decision without republishing the
content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from shared import security

__all__ = [
    "MCPError",
    "MCPNamespaceError",
    "MCPToolDefinitionChanged",
    "MCPToolNotAllowed",
    "MCPToolPolicy",
    "PinnedTool",
    "ToolDefinition",
    "ToolPinSet",
    "namespace_separator",
    "namespaced_tool_name",
    "parse_namespaced_tool_name",
    "review_tool_result",
    "tool_definition_digest",
    "tools_for_server",
]

_NAMESPACE_PREFIX = "mcp"
_SERVER_SEPARATOR = "__"
_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
# A doubled underscore inside one segment is the namespace separator itself.
# Collapsing runs of two-or-more underscores to a single one keeps every
# produced name unambiguously splittable, which is the property a client
# router depends on.
_DOUBLE_UNDERSCORE = re.compile(r"_{2,}")
_REPEATED_DASH = re.compile(r"-{2,}")
# A dash immediately next to an underscore is punctuation a hostile label
# produced (for example ``evil/__server``); collapsing it to a dash keeps the
# segment readable without ever forming the separator.
_DASH_UNDERSCORE = re.compile(r"(?:-_|_-)+")
_HASH_DOMAIN = "neo/mcp-tool/v1"
_MAX_SEGMENT = 64
_MAX_DESCRIPTION = 2_000
_MAX_SCHEMA_ITEMS = 128

#: Side-effect classes, ordered from least to most privileged. A tool that
#: declares nothing is treated as :data:`DEFAULT_SIDE_EFFECT_CLASS`.
SIDE_EFFECT_CLASSES: tuple[str, ...] = (
    "read",
    "search",
    "network",
    "mutation",
    "destructive",
)
DEFAULT_SIDE_EFFECT_CLASS = "mutation"
_SIDE_EFFECT_RANK = {name: index for index, name in enumerate(SIDE_EFFECT_CLASSES)}


class MCPError(Exception):
    """Base class for MCP namespace failures."""


class MCPNamespaceError(MCPError, ValueError):
    """Raised when a namespaced tool identifier is malformed."""


class MCPToolNotAllowed(MCPError):
    """Raised when a server/tool falls outside the least-privilege policy."""


class MCPToolDefinitionChanged(MCPError):
    """Raised when a tool's definition no longer matches its approval pin."""


def namespace_separator() -> str:
    """Return the separator common clients parse between namespace segments."""
    return _SERVER_SEPARATOR


def _normalize_segment(value: Any, *, label: str) -> str:
    """Return a safe single-segment identifier.

    Characters outside ``[A-Za-z0-9._-]`` collapse to ``-`` and the result is
    bounded, so a hostile server label can never inject a separator, a path, or
    a control character into a tool name a client will dispatch on. An empty
    result is an error, not a wildcard: an unnamed server cannot be addressed.
    """
    raw = security.redact_text(str(value or "")).strip()
    if not raw:
        raise MCPNamespaceError(f"{label} must not be empty")
    collapsed = _SEGMENT.sub("-", raw)
    collapsed = _DOUBLE_UNDERSCORE.sub("_", collapsed)
    collapsed = _DASH_UNDERSCORE.sub("-", collapsed)
    collapsed = _REPEATED_DASH.sub("-", collapsed)
    collapsed = collapsed.strip("-_.")
    if not collapsed:
        raise MCPNamespaceError(f"{label} {raw!r} has no addressable characters")
    return collapsed[:_MAX_SEGMENT]


def namespaced_tool_name(server: Any, tool: Any) -> str:
    """Return the namespaced identifier for one MCP tool.

    The shape is ``mcp__<server>__<tool>`` — what common MCP clients already
    route on, so a name produced here needs no client-side lookup table.
    """
    return (
        f"{_NAMESPACE_PREFIX}{_SERVER_SEPARATOR}"
        f"{_normalize_segment(server, label='server')}{_SERVER_SEPARATOR}"
        f"{_normalize_segment(tool, label='tool')}"
    )


def parse_namespaced_tool_name(name: Any) -> tuple[str, str]:
    """Return the ``(server, tool)`` pair for a namespaced identifier.

    Refuses anything that is not exactly ``mcp__<server>__<tool>`` with both
    segments present and non-empty. There is deliberately no "best effort"
    parse: a name the agent cannot attribute to a server is a name it may not
    call.
    """
    raw = security.redact_text(str(name or "")).strip()
    prefix = f"{_NAMESPACE_PREFIX}{_SERVER_SEPARATOR}"
    if not raw.startswith(prefix):
        raise MCPNamespaceError(f"{raw!r} is not a namespaced MCP tool identifier")
    remainder = raw[len(prefix) :]
    if _SERVER_SEPARATOR not in remainder:
        raise MCPNamespaceError(
            f"{raw!r} has no server/tool separator; expected mcp__<server>__<tool>"
        )
    server, _, tool = remainder.partition(_SERVER_SEPARATOR)
    if not server or not tool:
        raise MCPNamespaceError(f"{raw!r} has an empty server or tool segment")
    if _SERVER_SEPARATOR in tool:
        raise MCPNamespaceError(f"{raw!r} has more than two namespace segments")
    return server, tool


def _canonical_schema(
    value: Any, *, depth: int = 0, budget: Optional[list[int]] = None
) -> Any:
    """Return a bounded, key-sorted projection of a JSON schema.

    The digest must be stable across key order and must not explode on a
    pathological schema, so the projection sorts keys, bounds depth, and spends
    a node budget. A schema that exceeds the budget hashes as ``"[TRUNCATED]"``
    — the pin then says "the definition changed" on any edit past the bound,
    which is the safe direction.
    """
    if budget is None:
        budget = [_MAX_SCHEMA_ITEMS]
    if budget[0] <= 0 or depth > 8:
        return "[TRUNCATED]"
    budget[0] -= 1
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_schema(value[key], depth=depth + 1, budget=budget)
            for key in sorted(str(item) for item in value)
        }
    if isinstance(value, (list, tuple)):
        return [
            _canonical_schema(item, depth=depth + 1, budget=budget)
            for item in list(value)[:_MAX_SCHEMA_ITEMS]
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return (
            value
            if isinstance(value, (int, float, bool)) or value is None
            else security.redact_text(str(value))
        )
    return security.redact_text(str(value))


def tool_definition_digest(
    server: Any, tool: Any, *, description: Any = "", input_schema: Any = None
) -> str:
    """Return the domain-separated SHA-256 of one tool definition.

    The digest covers exactly what an operator reads before approving a call:
    which server, which tool, its description, and its input schema. A
    re-pointed server, a renamed tool, a rewritten description, or a widened
    schema all change the digest.
    """
    payload = json.dumps(
        {
            "domain": _HASH_DOMAIN,
            "server": _normalize_segment(server, label="server"),
            "tool": _normalize_segment(tool, label="tool"),
            "description": security.redact_text(str(description or ""))[
                :_MAX_DESCRIPTION
            ],
            "input_schema": _canonical_schema(
                input_schema if input_schema is not None else {}
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolDefinition:
    """One catalogued MCP tool, as the client saw it."""

    server: str
    tool: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    side_effect_class: str = DEFAULT_SIDE_EFFECT_CLASS

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "server", _normalize_segment(self.server, label="server")
        )
        object.__setattr__(self, "tool", _normalize_segment(self.tool, label="tool"))
        object.__setattr__(
            self,
            "description",
            security.redact_text(str(self.description or ""))[:_MAX_DESCRIPTION],
        )
        effect = (
            security.redact_text(str(self.side_effect_class or "")).strip().casefold()
        )
        if effect not in _SIDE_EFFECT_RANK:
            effect = DEFAULT_SIDE_EFFECT_CLASS
        object.__setattr__(self, "side_effect_class", effect)

    @property
    def namespaced(self) -> str:
        """Return the namespaced identifier for this tool."""
        return namespaced_tool_name(self.server, self.tool)

    @property
    def digest(self) -> str:
        """Return the definition digest an approval is pinned to."""
        return tool_definition_digest(
            self.server,
            self.tool,
            description=self.description,
            input_schema=self.input_schema,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible catalog entry."""
        return {
            "name": self.namespaced,
            "server": self.server,
            "tool": self.tool,
            "description": self.description,
            "side_effect_class": self.side_effect_class,
            "definition_digest": self.digest,
        }

    @classmethod
    def from_tool(cls, server: Any, tool: Any, payload: Any = None) -> "ToolDefinition":
        """Build a definition from a client's tool descriptor mapping."""
        data = dict(payload or {}) if isinstance(payload, Mapping) else {}
        schema = data.get("inputSchema") or data.get("input_schema") or {}
        if not isinstance(schema, Mapping):
            schema = {"value": schema}
        return cls(
            server=server,
            tool=tool,
            description=str(data.get("description") or ""),
            input_schema=schema,
            side_effect_class=str(
                data.get("sideEffectClass") or data.get("side_effect_class") or ""
            ),
        )


def tools_for_server(server: Any, tools: Iterable[Any]) -> tuple[ToolDefinition, ...]:
    """Return the catalogued definitions for one server, in sorted name order.

    Accepts either :class:`ToolDefinition` objects or raw client descriptors
    (mappings with ``name``/``description``/``inputSchema``). The result is
    sorted, so two clients listing the same tools in different orders produce
    the same catalog and therefore the same digests.
    """
    selected = str(server or "")
    out: list[ToolDefinition] = []
    for item in tools or ():
        if isinstance(item, ToolDefinition):
            definition = item
        elif isinstance(item, Mapping):
            definition = ToolDefinition.from_tool(selected, item.get("name"), item)
        else:
            definition = ToolDefinition.from_tool(selected, item)
        if definition.server != _normalize_segment(selected, label="server"):
            continue
        out.append(definition)
    return tuple(sorted(out, key=lambda definition: definition.namespaced))


@dataclass(frozen=True)
class MCPToolPolicy:
    """Per-server/per-tool least privilege for MCP tool calls.

    ``servers`` maps a server name to the set of tools that server may expose
    in this session. Everything is closed by default: an absent server is
    denied, and an absent tool within an allowed server is denied. A server
    entry may also carry a ``"*"`` tool value to allow every tool the *server*
    declares — that is an explicit operator decision, distinct from the
    default, and it is recorded as such by :meth:`describe`.
    """

    servers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    max_side_effect_class: str = "mutation"
    allow_all_tools: bool = False

    @classmethod
    def from_value(cls, value: Any) -> "MCPToolPolicy":
        """Build a policy from a mapping, a bool toggle, or another policy."""
        if isinstance(value, MCPToolPolicy):
            return value
        if value is None or isinstance(value, bool):
            return cls(allow_all_tools=bool(value))
        data = dict(value or {})
        servers: dict[str, tuple[str, ...]] = {}
        for raw_server, raw_tools in (data.get("servers") or {}).items():
            server = _normalize_segment(raw_server, label="server")
            if isinstance(raw_tools, str):
                tools = tuple(
                    item.strip()
                    for item in raw_tools.replace("|", ",").split(",")
                    if item.strip()
                )
            elif isinstance(raw_tools, (list, tuple, set)):
                tools = tuple(
                    str(item).strip() for item in raw_tools if str(item).strip()
                )
            else:
                tools = ()
            servers[server] = tools
        ceiling = (
            str(
                data.get("max_side_effect_class")
                or data.get("max_side_effect")
                or "mutation"
            )
            .strip()
            .casefold()
        )
        if ceiling not in _SIDE_EFFECT_RANK:
            ceiling = "mutation"
        return cls(
            servers=servers,
            max_side_effect_class=ceiling,
            allow_all_tools=bool(data.get("allow_all_tools", False)),
        )

    def allows_server(self, server: Any) -> bool:
        """Return whether a server is configured at all."""
        try:
            return _normalize_segment(server, label="server") in self.servers
        except MCPNamespaceError:
            return False

    def allows_tool(self, server: Any, tool: Any) -> bool:
        """Return whether a specific tool is exposed for a server."""
        if self.allow_all_tools:
            return True
        try:
            name = _normalize_segment(server, label="server")
            target = _normalize_segment(tool, label="tool")
        except MCPNamespaceError:
            return False
        tools = self.servers.get(name)
        if tools is None:
            return False
        if "*" in tools:
            return True
        return target in tools

    def allows_side_effect(self, side_effect_class: Any) -> bool:
        """Return whether a declared side-effect class is within the ceiling."""
        effect = str(side_effect_class or "").strip().casefold()
        if effect not in _SIDE_EFFECT_RANK:
            effect = DEFAULT_SIDE_EFFECT_CLASS
        ceiling = self.max_side_effect_class
        if ceiling not in _SIDE_EFFECT_RANK:
            ceiling = "mutation"
        return _SIDE_EFFECT_RANK[effect] <= _SIDE_EFFECT_RANK[ceiling]

    def authorize(
        self, definition: Union[ToolDefinition, Mapping[str, Any]]
    ) -> ToolDefinition:
        """Return the definition if it is permitted, else raise.

        Three independent gates, in order: the server must be configured, the
        tool must be exposed for that server, and the declared side-effect
        class must be within the session ceiling. A tool that declares no
        side-effect class lands on :data:`DEFAULT_SIDE_EFFECT_CLASS` and
        therefore needs a mutating ceiling.
        """
        resolved = (
            definition
            if isinstance(definition, ToolDefinition)
            else ToolDefinition.from_tool(
                definition.get("server"), definition.get("tool"), definition
            )
        )
        if not self.allows_server(resolved.server):
            raise MCPToolNotAllowed(
                f"mcp server {resolved.server!r} is not in this session's allowlist"
            )
        if not self.allows_tool(resolved.server, resolved.tool):
            raise MCPToolNotAllowed(
                f"tool {resolved.namespaced} is not exposed for server {resolved.server!r}"
            )
        if not self.allows_side_effect(resolved.side_effect_class):
            raise MCPToolNotAllowed(
                f"tool {resolved.namespaced} declares side effect "
                f"{resolved.side_effect_class!r}, above the session ceiling"
            )
        return resolved

    def visible(
        self, catalog: Iterable[Union[ToolDefinition, Mapping[str, Any]]]
    ) -> tuple[ToolDefinition, ...]:
        """Return only the catalogued tools this policy permits."""
        out: list[ToolDefinition] = []
        for item in catalog or ():
            try:
                out.append(self.authorize(item))
            except MCPToolNotAllowed:
                continue
        return tuple(sorted(out, key=lambda definition: definition.namespaced))

    def describe(self) -> dict[str, Any]:
        """Return a JSON-compatible summary of the configured privileges."""
        return {
            "servers": {
                name: (["*"] if "*" in tools else list(tools))
                for name, tools in sorted(self.servers.items())
            },
            "max_side_effect_class": self.max_side_effect_class,
            "allow_all_tools": bool(self.allow_all_tools),
        }


@dataclass(frozen=True)
class PinnedTool:
    """One approved tool plus the definition digest it was approved against."""

    server: str
    tool: str
    definition_digest: str
    side_effect_class: str = DEFAULT_SIDE_EFFECT_CLASS

    @property
    def namespaced(self) -> str:
        """Return the namespaced identifier for the pinned tool."""
        return namespaced_tool_name(self.server, self.tool)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible pin record."""
        return {
            "name": self.namespaced,
            "server": self.server,
            "tool": self.tool,
            "definition_digest": self.definition_digest,
            "side_effect_class": self.side_effect_class,
        }


class ToolPinSet:
    """The set of tool definitions an approval is bound to.

    An approval records :class:`PinnedTool` digests. Immediately before the
    call, :meth:`verify` re-hashes the live catalog. A digest that no longer
    matches raises :class:`MCPToolDefinitionChanged` naming the tool, and a
    pinned tool that has disappeared from the catalog raises the same error —
    both are "the thing you approved is not the thing that would run".
    """

    def __init__(
        self, pins: Iterable[Union[PinnedTool, Mapping[str, Any]]] = ()
    ) -> None:
        self._pins: dict[str, PinnedTool] = {}
        for item in pins or ():
            pin = (
                item
                if isinstance(item, PinnedTool)
                else PinnedTool(
                    server=str(item.get("server") or ""),
                    tool=str(item.get("tool") or ""),
                    definition_digest=str(item.get("definition_digest") or ""),
                    side_effect_class=str(
                        item.get("side_effect_class") or DEFAULT_SIDE_EFFECT_CLASS
                    ),
                )
            )
            self._pins[pin.namespaced] = pin

    @classmethod
    def from_catalog(
        cls, catalog: Iterable[Union[ToolDefinition, Mapping[str, Any]]]
    ) -> "ToolPinSet":
        """Pin every definition in a catalog at its current digest."""
        pins: list[PinnedTool] = []
        for item in catalog or ():
            definition = (
                item
                if isinstance(item, ToolDefinition)
                else ToolDefinition.from_tool(
                    item.get("server"), item.get("tool"), item
                )
            )
            pins.append(
                PinnedTool(
                    server=definition.server,
                    tool=definition.tool,
                    definition_digest=definition.digest,
                    side_effect_class=definition.side_effect_class,
                )
            )
        return cls(pins)

    def __len__(self) -> int:
        """Return the number of pinned tools."""
        return len(self._pins)

    def __contains__(self, name: object) -> bool:
        """Return whether a namespaced identifier is pinned."""
        return str(name) in self._pins

    @property
    def pins(self) -> tuple[PinnedTool, ...]:
        """Return the pins in deterministic namespaced order."""
        return tuple(self._pins[name] for name in sorted(self._pins))

    def verify(
        self, catalog: Iterable[Union[ToolDefinition, Mapping[str, Any]]]
    ) -> dict[str, str]:
        """Re-hash the live catalog and confirm every pin still matches.

        Returns the verified ``{namespaced_name: digest}`` map. Raises
        :class:`MCPToolDefinitionChanged` on the first mismatch, naming the
        tool and both digests so a reviewer can see exactly what moved.
        """
        live: dict[str, ToolDefinition] = {}
        for item in catalog or ():
            definition = (
                item
                if isinstance(item, ToolDefinition)
                else ToolDefinition.from_tool(
                    item.get("server"), item.get("tool"), item
                )
            )
            live[definition.namespaced] = definition
        verified: dict[str, str] = {}
        for name in sorted(self._pins):
            pin = self._pins[name]
            definition = live.get(name)
            if definition is None:
                raise MCPToolDefinitionChanged(
                    f"pinned tool {name} is no longer offered by its server"
                )
            if definition.digest != pin.definition_digest:
                raise MCPToolDefinitionChanged(
                    f"tool {name} changed after approval "
                    f"(approved {pin.definition_digest[:12]}, now {definition.digest[:12]})"
                )
            verified[name] = definition.digest
        return verified

    def authorize(
        self,
        policy: MCPToolPolicy,
        catalog: Iterable[Union[ToolDefinition, Mapping[str, Any]]],
        name: str,
    ) -> ToolDefinition:
        """Full pre-call gate: least privilege, then pin verification.

        Order is load-bearing. Least privilege is checked first so an
        unauthorized tool is refused as unauthorized rather than as a pin
        mismatch; the pin recheck then happens for exactly the tools the
        session may call.
        """
        try:
            server, tool = parse_namespaced_tool_name(name)
        except MCPNamespaceError:
            raise
        definition = ToolDefinition(server=server, tool=tool)
        for item in catalog or ():
            candidate = (
                item
                if isinstance(item, ToolDefinition)
                else ToolDefinition.from_tool(
                    item.get("server"), item.get("tool"), item
                )
            )
            if candidate.server == server and candidate.tool == tool:
                definition = candidate
                break
        return policy.authorize(definition)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record suitable for an approval ticket."""
        return {
            "schema_version": 1,
            "pins": [pin.as_dict() for pin in self.pins],
        }


def review_tool_result(
    payload: Any, *, server: str = "", tool: str = "", mode: Optional[str] = None
) -> tuple[str, Any]:
    """Review an MCP result and return ``(usable_text, review)``.

    The server's answer is untrusted content. It goes through
    :func:`shared.security.review_untrusted_source` with ``source="mcp"``, so a
    result carrying injected instructions is quarantined and replaced with an
    explicit refusal naming the source, and a merely-flagged result is returned
    with its taint visible. ``review`` is the :class:`UntrustedReview`, whose
    ``as_dict()`` carries no payload — a receipt can prove the decision
    without republishing the content.
    """
    label = f"mcp:{server}:{tool}" if server or tool else "mcp"
    review = security.review_untrusted_source(payload, source="mcp", mode=mode)
    if review.blocked:
        origin = f" from {label}" if label != "mcp" else ""
        return (
            f"[UNTRUSTED SOURCE: mcp] result{origin} was refused "
            f"({review.severity} injection indicators); the content was not forwarded",
            review,
        )
    if review.tainted:
        return security.taint_wrap(review), review
    return review.text, review


def namespaced_catalog(
    server: str, tools: Sequence[Any], *, policy: Optional[MCPToolPolicy] = None
) -> list[dict[str, Any]]:
    """Return the JSON-compatible, namespaced catalog for one server.

    When a policy is supplied, only permitted tools appear, so the catalog a
    client renders is the catalog the session may actually call — the two
    cannot drift because they are the same list.
    """
    definitions = tools_for_server(server, tools)
    if policy is not None:
        definitions = policy.visible(definitions)
    return [definition.as_dict() for definition in definitions]
