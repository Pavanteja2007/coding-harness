"""Bounded subagents for the agent kernel.

Two things live here, and they are the same idea at two scales.

**The bounded ``task`` tool: a real spawn into the runtime DAG.** Ceiling 04
made ``task`` a journaled intent so the kernel would never recurse into
unbounded model spawning. This module keeps that guarantee and makes the call
useful: the ``task`` tool now performs a *bounded* spawn through
:class:`runtime.subagents.SubagentSpawner`, which admits the request against
depth, fanout, node, concurrency, and cost limits and hands the resulting node
to the one existing :class:`runtime.orchestration.Orchestrator`.

There is no second orchestrator here. A child agent runs in its own process,
so its ``task`` call records a request in the workflow's durable
:class:`runtime.subagents.SpawnRequestStore`; the parent orchestrator admits it
at the next tool boundary. With no orchestrator bound at all the call is
refused honestly — never silently executed through an untyped fallback.

**The plan-phase research subagent (AGT-05).** Planning's value is that
exploration output never enters the main window, and that is a STRUCTURAL
property, not something a prompt can ask for. :class:`PlanResearchSubagent`
runs repository exploration in its own message list under a capability gate
derived from the one canonical catalog, spends a bounded budget, and returns
only a size-capped plan plus bounded citations. Its transcript is counted, not
returned: the caller receives the answer, never the working notes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.agent_contracts import ToolCall

from .tools import ToolResult

SPAWN_CONFIG_KEY = "orchestration_spawn_dir"
ORCHESTRATION_ID_KEY = "orchestration_id"
NODE_ID_KEY = "orchestration_node_id"
PARENT_NODE_ID_KEY = "orchestration_parent_node_id"
MAX_FILES_PER_SPAWN = 8

ERROR_NO_SUBAGENT_RUNTIME = "no_runtime"
ERROR_SUBAGENT_LIMIT = "handler_error"

# -- AGT-05: the plan-phase research subagent -------------------------------
#
# The one insight this section exists for: a plan phase is worth having only if
# the exploration it does never lands in the main context window. Prompting a
# model "don't paste the whole file back" does not work, because the mechanism
# that makes the paste easy is the same one that makes the answer possible. So
# the isolation is STRUCTURAL: the researcher has its own message list, a tool
# list that cannot express a write, a bounded budget, and a return value that is
# a size-capped plan plus bounded citations. Nothing else crosses the boundary.

#: The capabilities a plan-phase researcher may hold. ``read`` is the only one
#: that grants repository observation; ``control`` is granted because the
#: researcher must be able to END its own turn and ANSWER with a plan. It is
#: deliberately NOT a prompt: ``mutate``, ``shell``, ``network``, ``memory``
#: and ``mcp`` are unreachable, and so is the bounded ``task`` spawn, because a
#: researcher that can fork work has an unbounded budget by construction.
PLAN_RESEARCH_CAPABILITIES: Tuple[str, ...] = ("read", "control")

#: Tools inside a granted capability that the researcher may still not hold.
#: ``task`` is a ``control`` tool and is the only way to escalate, so it is
#: named explicitly rather than left to the capability table.
PLAN_RESEARCH_WITHHELD_TOOLS: Tuple[str, ...] = ("task",)

#: The closed vocabulary of a researcher's plan-mode refusals. A refusal names
#: the capability it came from, so a reader can tell "the gate said no" from
#: "the model asked for something that does not exist".
PLAN_MODE_REFUSALS: Dict[str, str] = {
    "mutate": "plan mode is read-only: this run may not change the repository",
    "shell": "plan mode may not run commands; exploration is observation only",
    "network": "plan mode may not reach the network",
    "memory": "plan mode may not record memory",
    "mcp": "plan mode may not call MCP servers",
    "subagent": (
        "plan mode may not spawn a child agent: a researcher's budget is "
        "bounded, and a child's is not"
    ),
}

#: Default bounds. Every one of them is overridable from the run's config, and
#: none of them may be raised to infinity by a config that omits the key: an
#: unbounded bound is not a bound.
DEFAULT_PLAN_MAX_TURNS = 6
DEFAULT_PLAN_MAX_TOOL_CALLS = 12
DEFAULT_PLAN_MAX_COST_USD = 0.25
DEFAULT_PLAN_MAX_CHARS = 4000
DEFAULT_PLAN_MAX_CITATIONS = 12

_PLAN_SYSTEM = (
    "You are the research phase of a coding agent. You explore the repository "
    "and return a PLAN. You cannot edit, run commands, or spawn agents - your "
    "tools are reads and searches only, and that is enforced rather than "
    "requested. Do not paste file contents into your answer. Cite the paths "
    "you relied on. End with the `finish` tool carrying the plan as its "
    "answer: a short ordered list of the changes to make and how to verify "
    "them."
)


def plan_mode_capabilities() -> Tuple[str, ...]:
    """Return the capabilities a plan-phase researcher holds, sorted."""
    return tuple(sorted(PLAN_RESEARCH_CAPABILITIES))


def plan_mode_withheld() -> Dict[str, str]:
    """Return the capabilities a plan-phase researcher does not hold, with why.

    The reason names the tools the capability carried, so the receipt answers
    "what exactly is unreachable" rather than "something in that area is".
    """
    from .kernel import capability_surface

    surface = capability_surface()
    withheld: Dict[str, str] = {}
    for capability, tools in sorted(surface.items()):
        if capability in PLAN_RESEARCH_CAPABILITIES:
            continue
        reason = PLAN_MODE_REFUSALS.get(
            capability,
            f"plan mode withholds the {capability} capability",
        )
        withheld[capability] = f"{reason} (withheld tools: {', '.join(tools)})"
    return withheld


def plan_mode_tools() -> Tuple[str, ...]:
    """Return the canonical tool names a plan-phase researcher may call.

    Derived from the ONE catalog through the kernel's capability surface, never
    from a hand-written list: a tool added to the catalog is either reachable
    by the researcher (because it is a read) or invisible to it (because it is
    not), and there is no third answer to forget to update.
    """
    from .kernel import capability_surface

    surface = capability_surface()
    names: List[str] = []
    for capability in PLAN_RESEARCH_CAPABILITIES:
        for tool in surface.get(capability, ()):
            if tool in PLAN_RESEARCH_WITHHELD_TOOLS:
                continue
            if tool not in names:
                names.append(tool)
    return tuple(sorted(names))


def plan_mode_is_read_only(tool: str) -> bool:
    """Return whether a tool name is inside the plan-mode gate.

    The answer is asked of the SAME capability surface the researcher's tool
    list is built from, so "the gate refused it" and "the tool list omitted
    it" cannot disagree.
    """
    return str(tool or "").strip().lower() in plan_mode_tools()


@dataclass(frozen=True)
class PlanCitation:
    """One bounded reference the researcher's plan relies on.

    A citation is a locator plus a one-line note, never a quotation. The
    distinction is the whole point: a path with a line number costs a dozen
    tokens and leaves the decision to whoever follows, while a pasted block
    costs a window and makes the decision for them.
    """

    target: str
    kind: str = "file"
    note: str = ""

    def render(self) -> str:
        """Return the model-facing one-line form of this citation."""
        label = f"{self.kind}:{self.target}" if self.kind else self.target
        note = f" - {self.note}" if self.note else ""
        return f"- {label}{note}"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this citation."""
        return {"target": self.target, "kind": self.kind, "note": self.note}


@dataclass(frozen=True)
class PlanResearchResult:
    """What the plan-phase researcher returns: a plan, citations, and a receipt.

    The receipt is the part that makes the isolation checkable rather than
    asserted. ``transcript_messages`` and ``transcript_chars`` are what the
    researcher's own window HELD; they are reported so a caller (and a test) can
    confirm that the exploration really happened, and they are the only trace
    of it that leaves. The plan and the citations are what the executor gets.
    """

    plan: str = ""
    citations: Tuple[PlanCitation, ...] = ()
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    truncated: bool = False
    finished: bool = False
    model: str = ""
    model_configured: str = ""
    model_honoured: bool = True
    stopped_because: str = ""
    transcript_messages: int = 0
    transcript_chars: int = 0
    transcript_digest: str = ""
    max_chars: int = DEFAULT_PLAN_MAX_CHARS
    max_turns: int = DEFAULT_PLAN_MAX_TURNS
    max_tool_calls: int = DEFAULT_PLAN_MAX_TOOL_CALLS
    max_cost_usd: float = DEFAULT_PLAN_MAX_COST_USD
    tools: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this result."""
        return {
            "plan": self.plan,
            "citations": [item.to_dict() for item in self.citations],
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "cost_usd": round(float(self.cost_usd or 0.0), 6),
            "truncated": bool(self.truncated),
            "finished": bool(self.finished),
            "model": self.model,
            "model_configured": self.model_configured,
            "model_honoured": bool(self.model_honoured),
            "stopped_because": self.stopped_because,
            "transcript_messages": int(self.transcript_messages),
            "transcript_chars": int(self.transcript_chars),
            "transcript_digest": self.transcript_digest,
            "max_chars": int(self.max_chars),
            "max_turns": int(self.max_turns),
            "max_tool_calls": int(self.max_tool_calls),
            "max_cost_usd": float(self.max_cost_usd),
            "tools": list(self.tools),
        }


class PlanResearchSubagent:
    """A read-only researcher with its own context window and a bounded budget.

    It is a small, explicit loop rather than a nested kernel run, because the
    three properties that matter are all properties of THIS object and would be
    harder to see through a general strategy:

    * **Own context.** ``self._messages`` is built here and never handed to the
      caller. The parent strategy receives :class:`PlanResearchResult`, which
      has no field capable of holding a transcript.
    * **Capability gate.** The registry is restricted to
      :func:`plan_mode_tools`, so a mutating call is not "refused by a policy"
      - it is not a tool the researcher has. A call naming one fails
      validation with ``unknown tool`` before any handler could run.
    * **Bounded budget.** Turns, tool calls, dollars, and the size of the
      returned text are each capped, and the cap that ended the run is named in
      the receipt.

    It never mutates the workspace, never shells out, and never spawns: the
    tools that could do any of those are not in its list.
    """

    def __init__(
        self,
        *,
        registry: Any,
        gateway: Any,
        policy: Any = None,
        context: Optional[Mapping[str, Any]] = None,
        max_turns: int = DEFAULT_PLAN_MAX_TURNS,
        max_tool_calls: int = DEFAULT_PLAN_MAX_TOOL_CALLS,
        max_cost_usd: float = DEFAULT_PLAN_MAX_COST_USD,
        max_chars: int = DEFAULT_PLAN_MAX_CHARS,
        max_citations: int = DEFAULT_PLAN_MAX_CITATIONS,
    ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.policy = policy
        self.context = dict(context or {})
        self.max_turns = max(1, int(max_turns))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.max_cost_usd = max(0.0, float(max_cost_usd))
        self.max_chars = max(0, int(max_chars))
        self.max_citations = max(0, int(max_citations))
        self._messages: List[Dict[str, str]] = []
        self._citations: List[PlanCitation] = []
        self._tool_calls = 0
        self._turns = 0
        self._stopped_because = ""
        self._finished = False

    # -- the bounded loop --------------------------------------------------

    def run(self, request: str, *, context_block: str = "") -> PlanResearchResult:
        """Explore for the request and return a plan plus bounded citations.

        Never raises. A researcher that cannot reach the model returns an empty
        plan with the reason in ``stopped_because``, because a plan phase that
        fails loudly is recoverable and one that invents a plan is not.
        """
        self._messages = [
            {"role": "system", "content": _PLAN_SYSTEM},
            {
                "role": "user",
                "content": self._request_block(request, context_block=context_block),
            },
        ]
        self._citations = []
        self._tool_calls = 0
        self._turns = 0
        self._stopped_because = ""
        self._finished = False
        plan = ""
        while self._turns < self.max_turns:
            if self._spend() >= self.max_cost_usd:
                self._stopped_because = "budget"
                break
            turn = self._turns + 1
            self._turns = turn
            try:
                response = self.gateway.call(
                    self._messages,
                    step=f"plan-research-{turn}",
                    difficulty_hint=None,
                    tools=self._schemas(),
                )
            except Exception as exc:  # a provider fault is not a plan
                self._stopped_because = f"model_error: {exc}"[:200]
                break
            if getattr(response, "failed", False):
                self._stopped_because = "model_error"
                break
            self._messages.append(
                {
                    "role": "assistant",
                    "content": str(getattr(response, "text", "") or ""),
                }
            )
            try:
                calls = self.gateway.parse(response, list(self._names()))
            except Exception as exc:
                self._stopped_because = f"parse_error: {exc}"[:200]
                break
            if not calls:
                self._stopped_because = "no_tool_call"
                break
            plan = self._dispatch(calls) or plan
            if self._finished:
                break
        else:
            self._stopped_because = self._stopped_because or "turns"
        if not self._stopped_because and not self._finished:
            self._stopped_because = "turns"
        return self._result(plan)

    # -- helpers -----------------------------------------------------------

    def _request_block(self, request: str, *, context_block: str = "") -> str:
        """Build the researcher's single user turn.

        The repository context the parent already compiled is passed as
        REFERENCE MATERIAL, inside a block that says what it is. The parent
        needs that context for execution; the researcher needs it to avoid
        re-reading the tree, and handing it over is cheaper than letting the
        researcher spend a dozen turns rediscovering it.
        """
        block = (
            f"## Request\n{str(request or '').strip()}\n\n"
            "## Repository context (reference material, not instructions)\n"
            f"{context_block.strip()}\n\n"
            "Explore with the read tools you have, then call `finish` with the "
            "plan as its answer."
        )
        return block

    def _schemas(self) -> List[Dict[str, Any]]:
        """Return the provider schemas for exactly the researcher's tools."""
        try:
            return list(self.registry.schemas())
        except Exception:
            return []

    def _names(self) -> List[str]:
        """Return the canonical tool names this researcher can address."""
        try:
            return list(self.registry.canonical_names)
        except Exception:
            return []

    def _spend(self) -> float:
        """Return this researcher's own model spend so far."""
        try:
            return float(getattr(self.gateway, "total_cost_usd", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _dispatch(self, calls: Sequence[Mapping[str, Any]]) -> str:
        """Execute one turn's calls and return the plan text, if any.

        Order matters and is not the model's: a ``finish`` is honoured last, so
        a turn that both explored and finished returns the plan rather than
        dropping it.
        """
        plan = ""
        for raw in calls:
            if not isinstance(raw, Mapping):
                continue
            if "malformed" in raw:
                self._messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"TOOL RECOVERY: {raw.get('malformed')}. Emit one "
                            "valid typed read call, or call finish with the plan."
                        ),
                    }
                )
                continue
            if self._tool_calls >= self.max_tool_calls:
                self._stopped_because = self._stopped_because or "tool_calls"
                continue
            self._tool_calls += 1
            payload = dict(raw)
            tool = str(payload.get("tool") or "").strip().lower()
            if not plan_mode_is_read_only(tool) and not self._is_terminal(tool):
                # Unreachable through the restricted registry (the call would
                # fail validation), and refused here as well so the refusal
                # does not depend on the registry having been built correctly.
                self._messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"TOOL ERROR [{PLAN_MODE_REFUSAL_KIND}]: {tool} is not "
                            "available in plan mode. Plan mode is read-only: it may "
                            "read, search, and answer, and may not change the "
                            "repository, run commands, or spawn agents."
                        ),
                    }
                )
                continue
            if self._is_terminal(tool):
                plan = self._terminal(payload)
                continue
            self._execute(payload, tool)
        return plan

    @staticmethod
    def _is_terminal(tool: str) -> bool:
        """Return whether a tool ends the researcher's turn with a plan."""
        return tool in {"finish", "done", "plan"}

    def _terminal(self, payload: Mapping[str, Any]) -> str:
        """Record the researcher's plan and stop the loop."""
        arguments = dict(payload.get("arguments") or {})
        text = str(arguments.get("answer") or "").strip()
        if not text:
            steps = arguments.get("steps") or []
            if isinstance(steps, (list, tuple)):
                text = "\n".join(
                    f"{index}. {str(step).strip()}"
                    for index, step in enumerate(steps, start=1)
                ).strip()
            notes = str(arguments.get("notes") or "").strip()
            if notes:
                text = f"{text}\n\n{notes}".strip()
        if not text:
            self._messages.append(
                {
                    "role": "user",
                    "content": (
                        "TOOL RECOVERY: the plan was empty. Call finish with the "
                        "plan as its answer, or cancel if there is nothing to plan."
                    ),
                }
            )
            return ""
        self._finished = True
        self._messages.append({"role": "assistant", "content": text})
        return text

    def _execute(self, payload: Mapping[str, Any], tool: str) -> None:
        """Run one read and append its bounded result to the researcher's window."""
        from .tools import ToolValidationError

        try:
            result = self.registry.execute(dict(payload), self.context)
        except ToolValidationError as exc:
            self._messages.append({"role": "user", "content": f"TOOL ERROR: {exc}"})
            return
        except Exception as exc:  # a handler fault is not a plan
            self._messages.append({"role": "user", "content": f"TOOL ERROR: {exc}"})
            return
        self._messages.append(
            {
                "role": "user",
                "content": f"TOOL RESULT {tool} ({'ok' if result.ok else 'error'}):\n"
                f"{str(result.output)[:4000]}",
            }
        )
        if result.ok:
            self._cite(payload, tool)

    def _cite(self, payload: Mapping[str, Any], tool: str) -> None:
        """Record one bounded citation for a successful read."""
        if self.max_citations <= 0:
            return
        arguments = dict(payload.get("arguments") or {})
        target = ""
        for key in ("path", "symbol", "pattern", "url", "command"):
            value = str(arguments.get(key) or "").strip()
            if value:
                target = value
                break
        if not target:
            target = tool
        kind = "symbol" if tool in {"read_symbol", "find_definition"} else "file"
        citation = PlanCitation(target=target[:200], kind=kind)
        if citation not in self._citations:
            self._citations.append(citation)

    def _result(self, plan: str) -> PlanResearchResult:
        """Project this researcher's state into the value the caller receives."""
        transcript_chars = sum(
            len(str(message.get("content") or "")) for message in self._messages
        )
        digest = hashlib.sha256(
            "\n".join(
                str(message.get("content") or "") for message in self._messages
            ).encode("utf-8", "replace")
        ).hexdigest()
        text, truncated = _cap_text(plan, self.max_chars)
        citations = tuple(self._citations[: self.max_citations])
        return PlanResearchResult(
            plan=text,
            citations=citations,
            turns=int(self._turns),
            tool_calls=int(self._tool_calls),
            cost_usd=self._spend(),
            truncated=bool(truncated),
            finished=bool(self._finished),
            model=str(self.gateway.config.get("model") or ""),
            model_configured=str(self.gateway.config.get("model") or ""),
            model_honoured=True,
            stopped_because=self._stopped_because,
            transcript_messages=len(self._messages),
            transcript_chars=int(transcript_chars),
            transcript_digest=digest,
            max_chars=int(self.max_chars),
            max_turns=int(self.max_turns),
            max_tool_calls=int(self.max_tool_calls),
            max_cost_usd=float(self.max_cost_usd),
            tools=tuple(self._names()),
        )


#: The error slug a plan-mode refusal carries. Stable, because a caller (and a
#: test) matches on it rather than parsing the prose.
PLAN_MODE_REFUSAL_KIND = "plan_mode_refused"


def _cap_text(text: str, limit: int) -> Tuple[str, bool]:
    """Return ``(text, truncated)`` with the limit a HARD ceiling.

    The cap is applied HERE, on the way out, rather than trusted to the
    researcher's own discipline: a subagent that returns an unbounded plan would
    otherwise spend the executor's window on material the executor never asked
    for. A cap of zero is an honest "return nothing", not "return everything".

    The marker is measured, not guessed. The first version reserved a fixed 32
    characters for it, and a six-figure omission count produced a marker longer
    than that - so the "capped" result was 16 characters OVER the limit, which
    is the failure mode a cap exists to prevent. The marker is therefore built
    first, from the real omission count, and the head and tail are then sized to
    fit what is left.
    """
    body = str(text or "")
    if limit <= 0:
        return "", bool(body)
    if len(body) <= limit:
        return body, False
    # Two passes: the first learns the marker's real length, the second fits the
    # body to whatever remains. The marker depends on the omitted count, which
    # depends on the split, so it is solved rather than approximated.
    head = max(limit // 2, 1)
    for _ in range(4):
        tail = max(limit - head - 1, 1)
        omitted = len(body) - head - tail
        marker = f"\n\n[... {omitted} chars omitted from the plan ...]\n\n"
        room = limit - len(marker)
        if room <= 0:
            # A limit smaller than the marker cannot carry both. The marker wins,
            # because a silent truncation is the lie this whole mechanism is
            # built to avoid.
            return (marker + body)[:limit], True
        head = max(min(head, room - 1), 1)
        tail = max(room - head, 1)
        omitted = len(body) - head - tail
        marker = f"\n\n[... {omitted} chars omitted from the plan ...]\n\n"
        if head + tail + len(marker) <= limit:
            return body[:head] + marker + body[-tail:], True
    return body[:limit], True


@dataclass(frozen=True)
class SubagentToolRuntime:
    """The runtime handle a run uses to service a ``task`` call.

    ``spawner`` is an in-process
    :class:`runtime.subagents.SubagentSpawner` (used when the run owns the
    orchestrator). ``request_store`` is the durable cross-process queue a child
    agent writes into. Exactly one of them is used; when neither is bound the
    tool refuses.
    """

    spawner: Any = None
    request_store: Any = None
    parent_node_id: str = "root"
    workflow_id: str = ""

    @property
    def available(self) -> bool:
        """Return whether a bounded subagent runtime is bound to this run."""
        return self.spawner is not None or self.request_store is not None

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "SubagentToolRuntime":
        """Resolve the bound subagent runtime from a run's dispatch context.

        Looks for an explicitly bound ``subagents`` handle first, then for the
        workflow's durable spawn-request directory in the run config (the path
        the orchestrator pins on every child packet).
        """
        spawner = context.get("subagents")
        spec = context.get("spec")
        session = context.get("session")
        config: Dict[str, Any] = {}
        for source in (getattr(spec, "config", None), getattr(session, "config", None)):
            if isinstance(source, Mapping):
                config.update(dict(source))
        runtime_config = context.get("config")
        if isinstance(runtime_config, Mapping):
            config.update(dict(runtime_config))
        parent = str(
            config.get(PARENT_NODE_ID_KEY)
            or config.get(NODE_ID_KEY)
            or getattr(spec, "run_id", "")
            or "root"
        )
        store = None
        spawn_dir = str(config.get(SPAWN_CONFIG_KEY, "") or "")
        if spawner is None and spawn_dir:
            store = _request_store(spawn_dir, config)
        return cls(
            spawner=spawner,
            request_store=store,
            parent_node_id=parent,
            workflow_id=str(config.get(ORCHESTRATION_ID_KEY, "") or ""),
        )


def dispatch_task_tool(call: ToolCall, context: Mapping[str, Any]) -> ToolResult:
    """Execute one ``task`` call as a bounded subagent spawn.

    The call always returns a bounded, model-facing receipt: the admission
    decision, the node id, the visible concurrency cap, and where the child's
    own summary will land. A refusal is a result too, so the parent keeps its
    turn budget and the model can adapt instead of retrying blindly.
    """
    runtime = SubagentToolRuntime.from_context(context)
    arguments = dict(call.arguments or {})
    description = str(arguments.get("description", "") or "").strip()
    if not description:
        return ToolResult(
            False,
            "TOOL ERROR [argument_error]: the task tool needs a description of the "
            "bounded work to hand to a child agent.",
            error_kind="validation_error",
        )
    files = [str(item) for item in (arguments.get("files") or ()) if str(item).strip()]
    symbols = [
        str(item) for item in (arguments.get("symbols") or ()) if str(item).strip()
    ]
    depends_on = [
        str(item) for item in (arguments.get("depends_on") or ()) if str(item).strip()
    ]
    agent = str(arguments.get("agent", "") or "")
    if not runtime.available:
        return ToolResult(
            False,
            "TOOL ERROR [no_runtime]: the task tool is a bounded spawn and no "
            "subagent runtime is bound to this run. Run the request through "
            "`neo run` (or an Orchestrator) to create child agents.",
            error_kind=ERROR_NO_SUBAGENT_RUNTIME,
        )
    if len(files) > MAX_FILES_PER_SPAWN:
        return ToolResult(
            False,
            f"TOOL ERROR [argument_error]: a child may claim at most "
            f"{MAX_FILES_PER_SPAWN} files, got {len(files)}.",
            error_kind="validation_error",
        )
    if runtime.spawner is not None:
        return _spawn_in_process(
            runtime, description, agent, files, symbols, depends_on
        )
    return _queue_request(runtime, description, agent, files, symbols, depends_on)


def _spawn_in_process(
    runtime: SubagentToolRuntime,
    description: str,
    agent: str,
    files: list[str],
    symbols: list[str],
    depends_on: list[str],
) -> ToolResult:
    spawner = runtime.spawner
    try:
        decision = spawner.spawn(
            description,
            runtime.parent_node_id,
            agent=agent,
            files=files,
            symbols=symbols,
            depends_on=depends_on,
        )
    except Exception as exc:
        return ToolResult(
            False,
            f"TOOL ERROR [{ERROR_SUBAGENT_LIMIT}]: child spawn refused: {exc}",
            error_kind=ERROR_SUBAGENT_LIMIT,
        )
    receipt = decision.render()
    if decision.admitted:
        try:
            status = spawner.describe()
            receipt = json.dumps(
                {
                    **json.loads(receipt),
                    "concurrency_cap": status.get("limits", {}).get(
                        "max_concurrent_children"
                    ),
                    "active_children": status.get("active_count", 0),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            pass
    return ToolResult(bool(decision.admitted), receipt, decision.node_id)


def _queue_request(
    runtime: SubagentToolRuntime,
    description: str,
    agent: str,
    files: list[str],
    symbols: list[str],
    depends_on: list[str],
) -> ToolResult:
    store = runtime.request_store
    try:
        request = store.submit(
            description,
            runtime.parent_node_id,
            agent=agent,
            files=files,
            symbols=symbols,
            depends_on=depends_on,
        )
    except Exception as exc:
        return ToolResult(
            False,
            f"TOOL ERROR [{ERROR_SUBAGENT_LIMIT}]: child spawn request refused: {exc}",
            error_kind=ERROR_SUBAGENT_LIMIT,
        )
    payload = {
        "queued": True,
        "request_id": request.request_id,
        "parent_node_id": request.parent_node_id,
        "agent": agent,
        "files": files,
        "workflow_id": runtime.workflow_id,
        "note": (
            "The orchestrator admits this request at its next tool boundary; the "
            "child's bounded summary arrives in the parent continuation handoff."
        ),
    }
    return ToolResult(
        True,
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        request.request_id,
    )


def _request_store(spawn_dir: str, config: Mapping[str, Any]) -> Optional[Any]:
    try:
        from runtime.subagents import SpawnRequestStore, SubagentLimits

        return SpawnRequestStore(spawn_dir, limits=SubagentLimits.from_config(config))
    except Exception:
        return None


__all__ = [
    "DEFAULT_PLAN_MAX_CHARS",
    "DEFAULT_PLAN_MAX_CITATIONS",
    "DEFAULT_PLAN_MAX_COST_USD",
    "DEFAULT_PLAN_MAX_TOOL_CALLS",
    "DEFAULT_PLAN_MAX_TURNS",
    "ERROR_NO_SUBAGENT_RUNTIME",
    "ERROR_SUBAGENT_LIMIT",
    "MAX_FILES_PER_SPAWN",
    "NODE_ID_KEY",
    "ORCHESTRATION_ID_KEY",
    "PARENT_NODE_ID_KEY",
    "PLAN_MODE_REFUSALS",
    "PLAN_MODE_REFUSAL_KIND",
    "PLAN_RESEARCH_CAPABILITIES",
    "PLAN_RESEARCH_WITHHELD_TOOLS",
    "SPAWN_CONFIG_KEY",
    "PlanCitation",
    "PlanResearchResult",
    "PlanResearchSubagent",
    "SubagentToolRuntime",
    "dispatch_task_tool",
    "plan_mode_capabilities",
    "plan_mode_is_read_only",
    "plan_mode_tools",
    "plan_mode_withheld",
]
