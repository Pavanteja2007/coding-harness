"""Deferred MCP schemas, server-contributed commands, and per-run tool budget.

``mcp_server/namespace.py`` owns the SECURITY boundary: namespaced
identifiers, least privilege, approval-time digests, and untrusted results.
It is deliberately read-only over what a server published, and it never
decides *what the model is shown*.

This module owns the three questions that sit on the other side of that
boundary and that a client actually pays for:

**1. DEFERRED SCHEMAS.** An external MCP server publishes one JSON schema per
tool, and a model pays for every one of those schemas on every turn. The
reference MCP client solved this by advertising a single *search* tool and
loading a tool's real schema only when the model asked for it. This module
implements that shape for Neo's connector surface:
:func:`deferred_catalog` returns the whole roster as ``{name, description,
side_effect_class, definition_digest}`` — the cheapest form that still lets a
model decide *whether* it wants a tool — and the ``inputSchema`` is withheld
until :func:`load_schema` is called for that one tool. The digest is computed
from the schema, so a deferred catalog is still pin-compatible: a pin is taken
against the full descriptor and re-verified against the full descriptor, and
the schema is loaded exactly for the tool being verified rather than for every
tool the server offers.

:func:`measure_schema_deferral` is the receipt. It reports the token cost of
BOTH arms — the eager catalog and the deferred catalog plus the one loaded
schema — so the saving is a number somebody measured rather than a claim in a
docstring. The estimator is a character heuristic with the divisor reported,
the same disclosure :mod:`cli.session` makes; it is not a tokenizer.

**2. SERVER-CONTRIBUTED COMMANDS.** An MCP server can expose *prompts*. A
prompt is not a tool and a tool is not a prompt, but a user thinks of both as
"something this server can do for me", and the reference MCP clients expose
server prompts as slash commands. :func:`prompt_commands` maps a server's
discovered prompts to ``/mcp__<server>__<prompt>`` command rows, reusing
``namespace.namespaced_tool_name`` so the separator, the segment normalization
and the hostile-label collapse are the SAME implementation a tool name goes
through — a server cannot inject the separator through a prompt name any more
than it can through a tool name. Discovery is over the real protocol
(``prompts/list``), not over a config file.

**3. PER-RUN TOOL BUDGET.** One fat server can expose forty tools and
quietly dominate the window. :class:`ToolBudget` records what was exposed,
what was called, and the projected context cost, and refuses the call that
would cross a declared ceiling. A budget that is recorded after the fact is a
report; a budget that refuses is a gate, and only the second one is worth
having.

Every function here is pure except :func:`load_schema`'s caller
(:func:`mcp_client_shaped_descriptors` is a projection of descriptors the
client already fetched) — the module performs no I/O, spawns nothing, and
never raises for a hostile or malformed descriptor. That is deliberate: it is
reached from the pre-call boundary in ``cli.connectors``, where a raise would
be a crash on the call path.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp_server.namespace import (
    DEFAULT_SIDE_EFFECT_CLASS,
    SIDE_EFFECT_CLASSES,
    MCPNamespaceError,
    ToolDefinition,
    ToolPinSet,
    namespaced_tool_name,
    tools_for_server,
)

__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_MAX_TOOLS_EXPOSED",
    "DEFAULT_MAX_TOOL_CALLS",
    "ToolBudget",
    "ToolBudgetDecision",
    "ToolSummary",
    "budget_from_config",
    "deferred_catalog",
    "describe_prompt_command",
    "estimate_tokens",
    "load_schema",
    "mcp_client_shaped_descriptors",
    "measure_schema_deferral",
    "prompt_command_name",
    "prompt_commands",
    "strict_undeclared_reason",
    "summary_for",
    "tools_outside_the_pin",
]

#: Characters per token used by the estimator. Reported by every measurement so
#: a reader can convert the figure themselves rather than trusting it. Chosen to
#: match ``cli.session``'s own disclosed divisor, so the two receipts are
#: comparable instead of quietly using different ones.
DEFAULT_CHARS_PER_TOKEN = 4

#: Default ceilings for :class:`ToolBudget`. Deliberately module constants
#: rather than ``harness.config.DEFAULTS`` entries: a DEFAULTS value merges into
#: every task and every eval arm, and "how many MCP calls may this run make" is
#: a property of a connector session, not a fact about every run in the
#: project. :func:`budget_from_config` reads them by key presence instead.
DEFAULT_MAX_TOOLS_EXPOSED = 64
DEFAULT_MAX_TOOL_CALLS = 32

_WHITESPACE = re.compile(r"\s+")


def estimate_tokens(
    text: Any, *, chars_per_token: int = DEFAULT_CHARS_PER_TOKEN
) -> int:
    """Return a disclosed heuristic token estimate for ``text``.

    A character heuristic, not a tokenizer, and it says so in its own receipt
    (:func:`measure_schema_deferral` reports ``chars_per_token``). A schema is
    JSON, which tokenizes worse than prose, so this figure is deliberately
    CONSERVATIVE in the direction that matters: it under-counts JSON tokens, so
    a reported saving is a floor rather than a best case.

    Never raises: a non-string is coerced, and a hostile control character is
    counted rather than executed.
    """
    try:
        divisor = max(1, int(chars_per_token))
    except Exception:
        divisor = DEFAULT_CHARS_PER_TOKEN
    raw = text if isinstance(text, str) else _json_text(text)
    return math.ceil(len(raw) / divisor)


def _json_text(value: Any) -> str:
    """Return a deterministic JSON projection of ``value`` (never raises)."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _first_line(text: Any, limit: int = 400) -> str:
    """Return a bounded, whitespace-collapsed first line of ``text``."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    head = raw.splitlines()[0] if raw.splitlines() else raw
    return _WHITESPACE.sub(" ", head)[:limit]


@dataclass(frozen=True)
class ToolSummary:
    """One tool as the model sees it BEFORE its schema is loaded.

    This is the deferred form: enough to decide whether the tool is worth a
    schema fetch, and nothing more. ``definition_digest`` is present because it
    is 64 hex characters and an approval ticket needs it; ``input_schema`` is
    empty precisely because that is the deferral.
    """

    name: str
    server: str
    tool: str
    description: str = ""
    side_effect_class: str = DEFAULT_SIDE_EFFECT_CLASS
    definition_digest: str = ""
    deferred: bool = True

    def as_model_dict(self) -> dict[str, Any]:
        """Return the row the MODEL is shown: name, description, class. Nothing else.

        This is the deferral, and it is deliberately smaller than
        :meth:`as_dict`. Measured on this tree's own five-tool server, adding
        the redundant ``server``/``tool`` pair and the 64-hex
        ``definition_digest`` to every row cost MORE tokens than the entire
        ``inputSchema`` payload the deferral removed — the digest is 16 tokens
        a tool under this divisor, and a small server's schema can be under
        that. ``name`` already carries both segments, so ``server``/``tool`` are
        derivable, and the digest is a RECEIPT field (an approval ticket needs
        it) rather than a field a model reads every turn.

        A first draft shipped one merged row and measured a NEGATIVE saving on
        a small catalog. That is what caught it.
        """
        return {
            "name": self.name,
            "description": self.description,
            "side_effect_class": self.side_effect_class,
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible deferred catalog ROW (the receipt form)."""
        return {
            "name": self.name,
            "server": self.server,
            "tool": self.tool,
            "description": self.description,
            "side_effect_class": self.side_effect_class,
            "definition_digest": self.definition_digest,
            "deferred": True,
            "schema_loaded": False,
        }


def summary_for(definition: ToolDefinition) -> ToolSummary:
    """Project one full definition onto its deferred summary."""
    return ToolSummary(
        name=definition.namespaced,
        server=definition.server,
        tool=definition.tool,
        description=_first_line(definition.description),
        side_effect_class=definition.side_effect_class,
        definition_digest=definition.digest,
    )


def deferred_catalog(server: Any, tools: Iterable[Any]) -> tuple[ToolSummary, ...]:
    """Return one server's tools as deferred summaries, in stable name order.

    Accepts the same inputs as :func:`mcp_server.namespace.tools_for_server`, so
    a caller that already holds a catalog does not need a second fetch. The
    returned rows carry NO ``inputSchema``; that is the entire point and the
    thing :func:`measure_schema_deferral` quantifies.
    """
    return tuple(
        summary_for(definition) for definition in tools_for_server(server, tools)
    )


def load_schema(server: Any, tool: Any, tools: Iterable[Any]) -> dict[str, Any]:
    """Return the FULL descriptor for one tool, loading its schema on demand.

    This is the deferred half's payoff: the ``inputSchema`` for exactly the tool
    the caller asked about, and for no other. Raises
    :class:`KeyError` when the server does not offer that tool, and
    :class:`mcp_server.namespace.MCPNamespaceError` when the name is not
    addressable — an unaddressable tool is a refusal, never a best-effort parse.
    """
    resolved = ToolDefinition.from_tool(server, tool)
    for item in tools_for_server(server, tools):
        if item.server == resolved.server and item.tool == resolved.tool:
            return item.as_dict()
    raise KeyError(
        f"{resolved.namespaced} is not offered by server {resolved.server!r}"
    )


def mcp_client_shaped_descriptors(
    tools: Iterable[Any],
) -> list[dict[str, Any]]:
    """Return raw descriptors in the shape ``memory.mcp_client`` would hand back.

    The client normalizes a listed tool to ``{name, description}``, which is
    precisely why :mod:`cli.connectors` has never seen an ``inputSchema``. This
    projection rebuilds the full descriptor mapping from whatever descriptors a
    caller holds — including ones carrying a schema — so the deferral machinery
    can be exercised and measured against real published schemas without
    editing another owner's file. See the request in
    ``mcp_server/AGENTS.md`` for the upstream change that would make this
    unnecessary.
    """
    out: list[dict[str, Any]] = []
    for item in tools or ():
        if isinstance(item, ToolDefinition):
            out.append(
                {
                    "name": item.tool,
                    "description": item.description,
                    "inputSchema": dict(item.input_schema or {}),
                    "sideEffectClass": item.side_effect_class,
                }
            )
        elif isinstance(item, Mapping):
            schema = item.get("inputSchema") or item.get("input_schema") or {}
            out.append(
                {
                    "name": str(item.get("name") or ""),
                    "description": str(item.get("description") or ""),
                    "inputSchema": dict(schema) if isinstance(schema, Mapping) else {},
                    "sideEffectClass": str(
                        item.get("sideEffectClass")
                        or item.get("side_effect_class")
                        or ""
                    ),
                }
            )
        else:
            out.append({"name": str(item), "description": "", "inputSchema": {}})
    return out


def measure_schema_deferral(
    server: Any,
    tools: Iterable[Any],
    *,
    requested: Optional[Sequence[str]] = None,
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
) -> dict[str, Any]:
    """Return the before/after token receipt for deferred schema loading.

    Three arms, all measured on the SAME descriptors:

    ``eager``
        every tool advertised with its full ``inputSchema`` — what a client that
        loads everything pays every turn;
    ``deferred``
        every tool advertised as a summary and NO schema loaded;
    ``deferred_with_requested``
        the deferred catalog plus the schemas for the tools in ``requested``.

    ``saving_tokens`` is ``eager - deferred_with_requested``: the figure the
    feature actually delivers for one request. ``saving_ratio`` is the same
    number as a fraction of the eager arm. Both are reported for the
    ``deferred`` arm too, because "what does deferral cost if the model asks
    for nothing" and "what does it save if it asks for one tool" are different
    questions and a receipt that answers only one of them is half a receipt.

    ``chars_per_token`` is carried in the output. This is a heuristic; the
    honest claim is a ratio measured under a stated divisor, not a token count
    from a tokenizer.
    """
    descriptors = mcp_client_shaped_descriptors(tools)
    definitions = tools_for_server(server, descriptors)
    summaries = [summary_for(definition) for definition in definitions]

    # The EAGER arm is what a client that loads everything shows a model:
    # name, description and the full inputSchema. The DEFERRED arm is
    # :meth:`ToolSummary.as_model_dict` — the same name and description with no
    # schema. Both are model-facing; neither carries a receipt field, because a
    # receipt field on every turn is a cost this feature exists to remove.
    eager_rows = [
        {
            "name": summary.name,
            "description": summary.description,
            "side_effect_class": summary.side_effect_class,
            "inputSchema": dict(definition.input_schema or {}),
        }
        for summary, definition in zip(summaries, definitions, strict=False)
    ]
    deferred_rows = [summary.as_model_dict() for summary in summaries]

    wanted: list[dict[str, Any]] = []
    names: list[str] = []
    unloaded: list[str] = []
    for name in list(requested or []):
        try:
            row = load_schema(server, name, descriptors)
        except (KeyError, MCPNamespaceError, Exception):
            unloaded.append(str(name))
            continue
        wanted.append(row)
        names.append(str(row.get("name") or ""))

    eager_tokens = estimate_tokens(eager_rows, chars_per_token=chars_per_token)
    deferred_tokens = estimate_tokens(deferred_rows, chars_per_token=chars_per_token)
    model_only_tokens = estimate_tokens(
        [summary.as_model_dict() for summary in summaries],
        chars_per_token=chars_per_token,
    )
    loaded_tokens = estimate_tokens(wanted, chars_per_token=chars_per_token)
    with_requested = deferred_tokens + loaded_tokens
    return {
        "server": str(server or ""),
        "tool_count": len(summaries),
        "requested": list(requested or []),
        "loaded": names,
        "not_offered": unloaded,
        "chars_per_token": max(1, int(chars_per_token or DEFAULT_CHARS_PER_TOKEN)),
        "estimator": "chars/divisor heuristic; not a tokenizer",
        "eager_tokens": eager_tokens,
        "deferred_tokens": deferred_tokens,
        "model_only_tokens": model_only_tokens,
        "loaded_schema_tokens": loaded_tokens,
        "deferred_with_requested_tokens": with_requested,
        # The SHIPPED row form keeps the digest (several suites assert it), so
        # this is the pessimistic saving. `model_only_*` is what a client that
        # dropped the receipt fields would pay. Both are reported because a
        # receipt that quotes only the flattering one is a receipt shaped to
        # sell the feature.
        "saving_tokens": eager_tokens - with_requested,
        "saving_ratio": _ratio(eager_tokens - with_requested, eager_tokens),
        "saving_tokens_no_request": eager_tokens - deferred_tokens,
        "saving_ratio_no_request": _ratio(eager_tokens - deferred_tokens, eager_tokens),
        "saving_tokens_model_only": eager_tokens - model_only_tokens,
        "saving_ratio_model_only": _ratio(
            eager_tokens - model_only_tokens, eager_tokens
        ),
        "eager_row": eager_rows,
        "deferred_row": deferred_rows,
    }


def _ratio(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator`` as a rounded float (0.0 when 0/0)."""
    try:
        if int(denominator) <= 0:
            return 0.0
        return round(int(numerator) / int(denominator), 6)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Server-contributed commands
# ---------------------------------------------------------------------------

#: The slash prefix a server-contributed prompt command carries. It is
#: ``"/"`` in front of the SAME ``mcp__<server>__<tool>`` namespace a tool uses,
#: so a user who already types ``mcp__`` for tools finds the prompts in the same
#: place, and a prompt command is never mistakable for a built-in Neo verb. It
#: is NOT ``"/mcp__"``: a first draft doubled the prefix and produced
#: ``/mcp__mcp__memory__summarize``, which is exactly the "two spellings of one
#: thing" failure the namespace layer exists to prevent.
PROMPT_COMMAND_PREFIX = "/"


def prompt_command_name(server: Any, prompt: Any) -> str:
    """Return the slash command a server prompt is exposed as.

    ``/mcp__<server>__<prompt>`` — the same separator and the same segment
    normalization a namespaced tool name goes through, because it is the same
    :func:`mcp_server.namespace.namespaced_tool_name` call with a different
    prefix. A hostile prompt name therefore cannot inject the separator, and a
    hostile SERVER name is normalized identically whether it arrived as a tool
    or as a prompt.
    """
    return f"{PROMPT_COMMAND_PREFIX}{namespaced_tool_name(server, prompt)}"


def describe_prompt_command(
    server: Any, prompt: Any, payload: Any = None
) -> dict[str, Any]:
    """Return one prompt-command row as JSON-compatible data.

    ``payload`` is the raw descriptor a client received for the prompt. A
    prompt's ARGUMENTS are part of its interface, so unlike a tool's schema
    they are summarized rather than withheld — a prompt with arguments cannot
    be invoked without knowing their names, and the argument NAMES are a few
    characters each. The argument DESCRIPTIONS are not carried, which is the
    deferral this module performs on the prompt axis.
    """
    data = dict(payload or {}) if isinstance(payload, Mapping) else {}
    raw_name = str(prompt or data.get("name") or "")
    description = _first_line(data.get("description"), limit=240)
    arguments: list[str] = []
    for item in data.get("arguments") or ():
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            arguments.append(name[:64])
    return {
        "command": prompt_command_name(server, raw_name),
        "server": str(server or ""),
        "prompt": raw_name,
        "description": description,
        "arguments": sorted(dict.fromkeys(arguments)),
        "source": "mcp_prompt",
    }


def prompt_commands(server: Any, prompts: Iterable[Any]) -> tuple[dict[str, Any], ...]:
    """Return every discovered prompt for one server as slash-command rows.

    ``prompts`` accepts raw protocol descriptors (mappings with ``name``),
    plain names, or already-built command rows. A prompt with no name is
    SKIPPED rather than rendered as a nameless command: an unaddressable
    prompt cannot be invoked, and a command nobody can call is a lie in the
    menu.

    Sorted by command name so two clients that list the same prompts in
    different orders produce the same rows.
    """
    rows: list[dict[str, Any]] = []
    for item in prompts or ():
        if isinstance(item, Mapping) and "command" in item and "source" in item:
            rows.append(dict(item))
            continue
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                rows.append(describe_prompt_command(server, name, item))
            except MCPNamespaceError:
                continue
            continue
        name = str(item or "").strip()
        if not name:
            continue
        try:
            rows.append(describe_prompt_command(server, name))
        except MCPNamespaceError:
            continue
    return tuple(sorted(rows, key=lambda row: row["command"]))


# ---------------------------------------------------------------------------
# Per-run tool budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolBudgetDecision:
    """The verdict for one would-be tool call.

    ``allowed=False`` names the ceiling that refused it, because "refused" with
    no reason is indistinguishable from a broken gate.
    """

    allowed: bool
    reason: str = ""
    calls_made: int = 0
    max_calls: int = DEFAULT_MAX_TOOL_CALLS
    tools_exposed: int = 0
    max_tools_exposed: int = DEFAULT_MAX_TOOLS_EXPOSED

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible verdict."""
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "calls_made": int(self.calls_made),
            "max_calls": int(self.max_calls),
            "tools_exposed": int(self.tools_exposed),
            "max_tools_exposed": int(self.max_tools_exposed),
        }


@dataclass
class ToolBudget:
    """What one run exposed, called, and is projected to spend on MCP context.

    Three numbers, because "one server quietly dominated the window" is three
    different failures:

    ``tools_exposed``
        how many tools the run advertised — the catalog a model pays for on
        every turn;
    ``calls_made``
        how many MCP calls the run dispatched;
    ``projected_context_tokens``
        the estimated per-turn cost of the catalog under the SAME disclosed
        divisor :func:`estimate_tokens` uses, so the number is convertible.

    The class is a GATE, not a report: :meth:`would_allow` refuses the call that
    would cross a declared ceiling, and :meth:`record_call` refuses to count a
    call that was not allowed. A budget that is only read after the fact is a
    dashboard, and a dashboard is not what stops one server owning the window.
    """

    max_tools_exposed: int = DEFAULT_MAX_TOOLS_EXPOSED
    max_calls: int = DEFAULT_MAX_TOOL_CALLS
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN
    calls_made: int = 0
    projected_context_tokens: int = 0
    tools_by_server: dict[str, int] = field(default_factory=dict)
    tokens_by_server: dict[str, int] = field(default_factory=dict)
    calls_by_server: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def servers_seen(self) -> tuple[str, ...]:
        """Return the servers whose catalogs were exposed, in stable order."""
        return tuple(sorted(self.tools_by_server))

    @property
    def tools_exposed(self) -> int:
        """Return the total advertised tool count across every exposed server."""
        return sum(self.tools_by_server.values())

    def record_exposure(
        self, server: Any, tools: Iterable[Any], *, catalog_tokens: Optional[int] = None
    ) -> int:
        """Record one server's advertised catalog. Returns its token estimate.

        Idempotent per server: a second exposure of the SAME server REPLACES its
        entry rather than double-counting, because a re-listed server is one
        catalog the model pays for once, not two. A budget that double-counted
        a re-list would report a server as more dominant than it is, which is
        the failure this module exists to prevent.
        """
        label = str(server or "")
        rows = deferred_catalog(label, tools)
        tokens = (
            int(catalog_tokens)
            if catalog_tokens is not None
            else estimate_tokens(
                [row.as_model_dict() for row in rows],
                chars_per_token=self.chars_per_token,
            )
        )
        self.tools_by_server[label] = len(rows)
        self.tokens_by_server[label] = tokens
        self.projected_context_tokens = sum(self.tokens_by_server.values())
        return tokens

    def would_allow(
        self,
        *,
        server: Any = "",
        tool: Any = "",
        tool_count: Optional[int] = None,
    ) -> ToolBudgetDecision:
        """Return whether one call fits inside the budget, without recording it.

        ``tool_count`` lets a caller that just exposed a larger catalog ask the
        question for the NEW total. Pure: nothing is mutated, so a caller may
        ask before committing.
        """
        exposed = int(self.tools_exposed if tool_count is None else tool_count)
        if self.max_tools_exposed >= 0 and exposed > self.max_tools_exposed:
            return ToolBudgetDecision(
                allowed=False,
                reason=(
                    f"exposing {exposed} MCP tools exceeds the per-run ceiling of "
                    f"{self.max_tools_exposed}"
                ),
                calls_made=self.calls_made,
                max_calls=self.max_calls,
                tools_exposed=exposed,
                max_tools_exposed=self.max_tools_exposed,
            )
        if self.max_calls >= 0 and self.calls_made >= self.max_calls:
            namespaced = namespaced_tool_name(server or "inline", tool or "?")
            return ToolBudgetDecision(
                allowed=False,
                reason=(
                    f"MCP call budget exhausted ({self.calls_made}/{self.max_calls}); "
                    f"{namespaced} was not called"
                ),
                calls_made=self.calls_made,
                max_calls=self.max_calls,
                tools_exposed=exposed,
                max_tools_exposed=self.max_tools_exposed,
            )
        return ToolBudgetDecision(
            allowed=True,
            reason="within the per-run MCP tool budget",
            calls_made=self.calls_made,
            max_calls=self.max_calls,
            tools_exposed=exposed,
            max_tools_exposed=self.max_tools_exposed,
        )

    def record_call(self, server: Any = "", tool: Any = "") -> ToolBudgetDecision:
        """Record one dispatched call, refusing it if the budget is exhausted.

        The refusal and the record are the SAME call, so a caller cannot record
        a call it was not allowed to make. A refused call returns the decision
        and leaves the counters untouched — a budget that counted a refused
        call would report its own refusal as consumption.
        """
        decision = self.would_allow(server=server, tool=tool)
        if not decision.allowed:
            return decision
        self.calls_made += 1
        label = str(server or "")
        if label:
            self.calls_by_server[label] = self.calls_by_server.get(label, 0) + 1
        return decision

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible per-run budget receipt.

        ``within_budget`` is the AND of both ceilings and is the field a
        surface should render. ``share_by_server`` is the dominance signal:
        a server holding most of the window is the thing the budget exists to
        make visible, and a number that only says "3 of 64 calls" does not.
        """
        total = max(1, self.calls_made)
        return {
            "tools_exposed": int(self.tools_exposed),
            "max_tools_exposed": int(self.max_tools_exposed),
            "calls_made": int(self.calls_made),
            "max_calls": int(self.max_calls),
            "projected_context_tokens": int(self.projected_context_tokens),
            "chars_per_token": max(
                1, int(self.chars_per_token or DEFAULT_CHARS_PER_TOKEN)
            ),
            "estimator": "chars/divisor heuristic; not a tokenizer",
            "servers": list(self.servers_seen),
            "tools_by_server": {
                name: int(count) for name, count in sorted(self.tools_by_server.items())
            },
            "tokens_by_server": {
                name: int(count)
                for name, count in sorted(self.tokens_by_server.items())
            },
            "calls_by_server": {
                name: int(count) for name, count in sorted(self.calls_by_server.items())
            },
            "share_by_server": {
                name: _ratio(count, total)
                for name, count in sorted(self.calls_by_server.items())
            },
            "within_budget": self._within_budget(),
            "notes": list(self.notes),
        }

    def _within_budget(self) -> bool:
        """Return whether both ceilings hold right now."""
        if self.max_tools_exposed >= 0 and self.tools_exposed > self.max_tools_exposed:
            return False
        return not (self.max_calls >= 0 and self.calls_made > self.max_calls)


def budget_from_config(
    config: Optional[Mapping[str, Any]] = None,
    *,
    max_tools_exposed: Optional[int] = None,
    max_calls: Optional[int] = None,
) -> ToolBudget:
    """Build a :class:`ToolBudget` from a task config, by KEY PRESENCE.

    Both keys are read by presence, never by a truthy default: an absent key
    means "the operator said nothing", and a value of ``0`` is a deliberate
    "no MCP calls at all", which a ``config.get(k, 1) or 1`` idiom would
    silently turn back into the default. Neither key is in
    ``harness.config.DEFAULTS``, because a DEFAULTS value merges into every
    task and every eval arm and this is a per-connector-session ceiling.

    An unusable value (a string, a negative number, a bool) is REPORTED in
    ``notes`` and the declared default is used — a typo must not be able to
    produce a budget nobody asked for.
    """
    data = dict(config or {})
    notes: list[str] = []

    def read(key: str, explicit: Optional[int], fallback: int) -> int:
        if explicit is not None:
            try:
                return int(explicit)
            except Exception:
                notes.append(f"{key}: supplied value {explicit!r} is not an integer")
                return fallback
        if key not in data:
            return fallback
        raw = data[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            notes.append(f"{key}: value {raw!r} is not a ceiling")
            return fallback
        try:
            value = int(str(raw).strip())
        except Exception:
            notes.append(f"{key}: value {raw!r} is not an integer")
            return fallback
        if value < 0:
            notes.append(f"{key}: negative ceiling {value} ignored")
            return fallback
        return value

    budget = ToolBudget(
        max_tools_exposed=read(
            "mcp_max_tools_exposed", max_tools_exposed, DEFAULT_MAX_TOOLS_EXPOSED
        ),
        max_calls=read("mcp_max_tool_calls", max_calls, DEFAULT_MAX_TOOL_CALLS),
        chars_per_token=read("mcp_chars_per_token", None, DEFAULT_CHARS_PER_TOKEN),
    )
    budget.notes = tuple(notes)
    return budget


# ---------------------------------------------------------------------------
# Undeclared connectors: the surfaced default and the opt-in strict mode
# ---------------------------------------------------------------------------

#: The config key an operator sets to refuse an undeclared connector outright.
#: Absent by default, and read by key presence. The DEFAULT is the surfaced
#: opt-in boundary R2-16 shipped — an undeclared connector runs with
#: ``enforced: false`` and a reason saying the call was NOT gated — because
#: enforcing it by default would break every connector that exists on upgrade.
STRICT_UNDECLARED_KEY = "mcp_require_declared_connector"


def strict_undeclared_reason(
    config: Optional[Mapping[str, Any]] = None,
    *,
    strict: Optional[bool] = None,
    label: str = "",
) -> Optional[str]:
    """Return a REFUSAL reason when strict mode refuses, else ``None``.

    Two axes, and they are not the same question:

    * the **default** answer is ``None`` — the call proceeds, undeclared, and the
      receipt carries ``enforced: false`` with a reason that says the call was
      NOT gated. That is the shipped contract and this function does not
      change it.
    * **strict mode** (``mcp_require_declared_connector`` present and true, or
      an explicit ``strict=True``) refuses an undeclared connector outright and
      names the remediation, because "an operator who wants this" is a real
      person and the fix is one command.

    A present-but-unusable value is treated as ``False`` and reported by the
    caller, rather than being coerced to a refusal by a truthy string: a typo
    in a security setting must not be the thing that silently turns a gate on
    OR off without saying so.
    """
    if strict is None:
        data = dict(config or {})
        if STRICT_UNDECLARED_KEY not in data:
            return None
        raw = data[STRICT_UNDECLARED_KEY]
        if isinstance(raw, bool):
            strict = raw
        elif isinstance(raw, str):
            strict = raw.strip().casefold() in ("1", "true", "yes", "require", "strict")
        else:
            strict = bool(raw)
    if not strict:
        return None
    name = str(label or "").strip() or "(inline command)"
    return (
        f"connector {name!r} has no permission declaration and strict mode is on "
        f"({STRICT_UNDECLARED_KEY}); declare it with "
        f"`neo mcp permissions {name} --tool <name> ...` before calling it"
    )


# ---------------------------------------------------------------------------
# Pin visibility
# ---------------------------------------------------------------------------


def tools_outside_the_pin(
    catalog: Iterable[Any],
    pins: Optional[ToolPinSet] = None,
    policy: Any = None,
) -> tuple[str, ...]:
    """Return the namespaced names this session may NOT call, with the reason split.

    This is the read-side twin of :meth:`ToolPinSet.verify`. ``verify`` is a
    GATE — it raises when a pinned definition moved. A catalog surface also
    needs to answer "which tools am I not showing, and why", and answering that
    from the same two authorities means a tool hidden from the catalog and a
    tool refused at the call cannot drift apart into different lists.

    Two reasons, kept distinguishable because they are different fixes:

    ``not_in_policy``
        least privilege refused it — fix the connector's declaration;
    ``outside_pin``
        the connector pinned some tools and this is not one of them — fix the
        declaration's ``tools`` list or pin it.

    A tool refused by BOTH appears under both reasons rather than being
    silently assigned one.
    """
    unauthorized: list[str] = []
    unpinned: list[str] = []
    pinned_names: set[str] = set()
    if pins is not None:
        pinned_names = {pin.namespaced for pin in pins.pins}
    for item in catalog or ():
        definition = (
            item
            if isinstance(item, ToolDefinition)
            else ToolDefinition.from_tool(item.get("server"), item.get("tool"), item)
        )
        if policy is not None:
            try:
                policy.authorize(definition)
            except Exception:
                unauthorized.append(definition.namespaced)
        if pinned_names and definition.namespaced not in pinned_names:
            unpinned.append(definition.namespaced)
    out: list[str] = []
    for name in sorted(set(unauthorized) | set(unpinned)):
        reasons = []
        if name in unauthorized:
            reasons.append("not_in_policy")
        if name in unpinned:
            reasons.append("outside_pin")
        out.append(f"{name}:{','.join(reasons)}")
    return tuple(out)


def side_effect_rank(name: Any) -> int:
    """Return the ordering rank of a side-effect class (unknown = most permissive).

    Re-exported through this module so a caller comparing a declared ceiling
    against a budget does not import two vocabularies. An unrecognized class
    lands on :data:`mcp_server.namespace.DEFAULT_SIDE_EFFECT_CLASS`, which is
    the conservative direction.
    """
    text = str(name or "").strip().casefold()
    if text not in SIDE_EFFECT_CLASSES:
        text = DEFAULT_SIDE_EFFECT_CLASS
    return SIDE_EFFECT_CLASSES.index(text)
