"""Authoritative lifecycle and strategy registry for the Neo agent kernel."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.config import get_config

from .checkpoints import CheckpointStore
from .completion import CompletionPolicy
from .context import ContextBuilder, SessionStore
from .contracts import CompletionStatus, RunResult, RunSpec, SessionState
from .events import RunEventJournal
from .gateway import ModelGateway
from .legacy import LegacyAgentStrategy
from .policy import PolicyEngine
from .strategy import (
    AgentStrategy,
    DailyCodingStrategy,
    PlanningStrategy,
    QuestionStrategy,
    ResearchStrategy,
    build_default_handlers,
)
from .tools import ToolRegistry, builtin_tool_specs
from .verified import VerifiedFixStrategy
from .workspace import WorkspaceJournal

# ---------------------------------------------------------------------------
# AGT-01 - ONE capability surface
# ---------------------------------------------------------------------------
#
# Before this, each strategy declared its OWN tool list (``_DAILY_TOOLS`` 45,
# ``_QUESTION_TOOLS`` 9, ``_PLANNING_TOOLS`` 11, ``_RESEARCH_TOOLS`` 10) and
# three of the four had no retrieval at all. A user could not know that asking
# a question was a degraded mode, and the degradation was invisible in every
# receipt. This is why "what is the best mouse under 1000 rupees" could not be
# answered: the strategy that answers questions had no way to reach the world.
#
# The surface below is the ONE list, and it is DERIVED from the one canonical
# catalog (``harness.tools``) rather than typed out again. A tool added to the
# catalog therefore lands in a capability automatically; a strategy can no
# longer "forget" it. A strategy states only what it may NOT do - the surface
# it inherits is everything else.
#
# The capability axis is the catalog's own ``side_effect_class`` vocabulary,
# so the grouping is a renaming of what the catalog already says rather than a
# second opinion about it. Two of those classes are refined, because they carry
# two genuinely different capabilities under one name:
#
#   * ``process`` covers both arbitrary command execution and the repository's
#     own declared quality commands, and the kernel implements them through
#     two different handlers (``shell`` -> the safe execution backend,
#     ``test``/``verify`` -> the bounded completion verifier). Running a plan's
#     tests is not running a shell, so ``test``/``verify`` get their own
#     ``verify`` capability. ``lint``/``typecheck``/``build`` stay in
#     ``shell``: they have no bounded handler here and reach the backend.
#   * ``control`` carries the loop-control verbs plus the bounded subagent
#     spawn. A read-only strategy has no business escalating into a child
#     agent, so ``task`` gets its own ``subagent`` capability.
#
# A tool whose effect class this table does not know falls into
# ``UNCLASSIFIED_CAPABILITY``, which NO strategy withholds. That is the whole
# point: an unfamiliar capability is granted and REPORTED, never silently
# dropped. ``test_every_catalog_tool_belongs_to_exactly_one_capability`` and
# ``test_an_unclassified_tool_is_granted_to_every_strategy`` pin both halves.

_EFFECT_CAPABILITY: Dict[str, str] = {
    "read_only": "read",
    "workspace_write": "mutate",
    "process": "shell",
    "network": "network",
    "memory": "memory",
    "mcp": "mcp",
    "control": "control",
}

#: Canonical tool names that are a different capability from their effect
#: class default. Keys are resolved through the catalog, so an alias spelling
#: lands on the same capability.
_CAPABILITY_OVERRIDES: Dict[str, str] = {
    "test": "verify",
    "verify": "verify",
    "task": "subagent",
}

#: Where a catalog tool lands when its effect class is not in the table above.
#: Deliberately withheld by nobody: an unrecognised capability must be
#: visible and granted, never quietly absent.
UNCLASSIFIED_CAPABILITY = "unclassified"

#: Human reasons for the two narrowings the read-only strategies make. A
#: withheld capability without a stated reason is exactly the silent
#: degradation this round exists to remove, so a reason is required for every
#: entry rather than defaulted at render time.
_WITHHELD_REASONS: Dict[str, str] = {
    "mutate": "read-only strategy: this run may not change the repository",
    "shell": "read-only strategy: this run may not run commands",
    "subagent": (
        "read-only strategy: a child agent could reach capabilities this run "
        "withholds, so this run may not escalate into one"
    ),
}

#: What each strategy may NOT do. Everything else is inherited. An empty tuple
#: is the widest surface, which is why ``daily`` needs no entry beyond the
#: documented default.
STRATEGY_WITHHELD: Dict[str, Tuple[str, ...]] = {
    "daily": (),
    "verified_fix": (),
    "legacy_agent": (),
    "planning": ("mutate", "shell"),
    "question": ("mutate", "shell", "subagent"),
    "research": ("mutate", "shell", "subagent"),
}


def _canonical_tool(word: str) -> str:
    """Return the catalog's canonical name for a tool word or its alias.

    The canonical catalog renames tools between rounds (``ask``/``question``,
    ``fetch``/``web_fetch``) and keeps the old word as an alias. Resolving
    through the catalog means a role's tool allowlist always names the tool
    that actually exists, in either direction.
    """
    wanted = str(word or "").strip().lower()
    for spec in builtin_tool_specs():
        if spec.name == wanted or wanted in spec.aliases:
            return spec.name
    return wanted


def _capability_of(spec: Any) -> str:
    """Return the capability a catalog tool belongs to."""
    override = _CAPABILITY_OVERRIDES.get(spec.name)
    if override:
        return override
    return _EFFECT_CAPABILITY.get(spec.side_effect_class, UNCLASSIFIED_CAPABILITY)


def capability_surface() -> Dict[str, List[str]]:
    """Return the one capability surface: capability -> canonical tool names.

    Derived from ``harness.tools`` (the single canonical catalog) on every
    call, so it cannot drift from what the kernel can actually dispatch. The
    mapping is total: every catalog tool appears under exactly one capability,
    and an effect class this module does not know lands in
    :data:`UNCLASSIFIED_CAPABILITY` rather than disappearing.
    """
    grouped: Dict[str, List[str]] = {}
    for spec in builtin_tool_specs():
        grouped.setdefault(_capability_of(spec), []).append(spec.name)
    return {name: sorted(tools) for name, tools in sorted(grouped.items())}


def _operator_withheld(
    config: Optional[Mapping[str, Any]], already: Mapping[str, str]
) -> Dict[str, str]:
    """Return operator-requested narrowing, resolved against the surface.

    ``capability_withheld`` accepts capability names (``"network"``) or tool
    names / aliases (``"search"``, ``"build"``), each optionally paired with a
    reason. It can only ever SUBTRACT: an entry that names nothing in the
    surface is reported as an operator warning rather than ignored, because a
    typo in a narrowing key must not read as "nothing was withheld". ``already``
    is the strategy's own narrowing, so a capability narrowed by both keeps
    both reasons instead of one silently replacing the other.
    """
    values = dict(config or {})
    raw = values.get("capability_withheld")
    if raw is None:
        return {}
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    if isinstance(raw, Mapping):
        raw = [{"capability": key, "reason": value} for key, value in raw.items()]
    surface = capability_surface()
    tool_to_capability = {
        tool: capability for capability, tools in surface.items() for tool in tools
    }
    tool_to_capability.update(
        {
            alias: tool_to_capability[spec.name]
            for spec in builtin_tool_specs()
            for alias in spec.aliases
            if spec.name in tool_to_capability
        }
    )
    reasons: Dict[str, str] = {}
    for entry in list(raw or []):
        if isinstance(entry, Mapping):
            name = str(entry.get("capability") or entry.get("tool") or "").strip()
            reason = str(entry.get("reason") or "").strip()
        else:
            name = str(entry or "").strip()
            reason = ""
        if not name:
            continue
        resolved = (
            name if name in surface else tool_to_capability.get(_canonical_tool(name))
        )
        if resolved is None:
            reasons.setdefault(
                f"unknown:{name}",
                f"operator requested withholding {name!r}, which is not in the "
                "capability surface; nothing was withheld for it",
            )
            continue
        if not reason:
            reason = f"withheld by the operator's capability_withheld ({resolved})"
        if resolved in already:
            reason = f"{reason}; the strategy already withheld it: {already[resolved]}"
        reasons[resolved] = reason
    return reasons


def capability_receipt(
    strategy: str, config: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Return the effective capability surface for one strategy, as a receipt.

    The receipt is the whole answer to "what could this run actually do, and
    what did it not get?" - the effective tool list, the capabilities granted,
    and every withheld capability WITH its reason. It deliberately carries no
    completion vocabulary: a capability receipt must not be able to imply that
    anything was verified.
    """
    name = str(strategy or "daily")
    surface = capability_surface()
    withheld: Dict[str, str] = {
        capability: _WITHHELD_REASONS.get(capability, "")
        for capability in STRATEGY_WITHHELD.get(name, STRATEGY_WITHHELD["daily"])
    }
    withheld.update(_operator_withheld(config, withheld))
    granted = {key: value for key, value in surface.items() if key not in withheld}
    effective = sorted({tool for tools in granted.values() for tool in tools})
    withheld_tools = sorted(
        {tool for key, tools in surface.items() if key in withheld for tool in tools}
    )
    return {
        "schema_version": 1,
        "strategy": name,
        "capabilities": sorted(granted),
        "withheld": {key: withheld[key] for key in sorted(withheld)},
        "unclassified_present": UNCLASSIFIED_CAPABILITY in surface,
        "surface_tool_count": sum(len(tools) for tools in surface.values()),
        "allowed_tools": effective,
        "allowed_tool_count": len(effective),
        "withheld_tools": withheld_tools,
        "surface_by_capability": surface,
    }


def render_capability_note(receipt: Mapping[str, Any]) -> str:
    """Render one model-facing line naming what this run may not do.

    Only the WITHHELD capabilities are stated. A run that inherited everything
    gets no line at all, so the widest surface's prompt is byte-identical to
    what it was before this round - which is also what keeps the prompt-
    regression matrix comparable.
    """
    withheld = dict(receipt.get("withheld") or {})
    if not withheld:
        return ""
    lines = [
        f"- {capability}: {reason or 'withheld by this strategy'}"
        for capability, reason in sorted(withheld.items())
    ]
    return (
        "This run's capabilities were narrowed. It may NOT do the following, "
        "and a tool call asking for one will be refused rather than executed:\n"
        + "\n".join(lines)
    )


#: Strategies the kernel is the single authority for. ``agent_strategy`` in
#: the task/session config selects one of these; an unknown value is a hard
#: error before any run directory is created, never a silent fallback.
STRATEGY_NAMES = (
    "daily",
    "verified_fix",
    "planning",
    "question",
    "research",
    "legacy_agent",
)


class AgentKernel:
    """Coordinate one session, strategy, context loop, tools, and event journal."""

    def __init__(
        self,
        *,
        repo_path: str = "",
        log_root: Optional[Path | str] = None,
        config: Optional[Mapping[str, Any]] = None,
        context_builder: Optional[ContextBuilder] = None,
        model_gateway: Optional[ModelGateway] = None,
        policy_engine: Optional[PolicyEngine] = None,
        tool_registry: Optional[ToolRegistry] = None,
        workspace_journal: Optional[WorkspaceJournal] = None,
        completion_policy: Optional[CompletionPolicy] = None,
        verifier: Any = None,
        approval_callback: Optional[Callable[..., Any]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        strategy_factory: Optional[Callable[..., Any]] = None,
        strategy_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.repo_path = str(repo_path or "")
        self.log_root = Path(log_root).expanduser().resolve() if log_root else None
        self.config = get_config(dict(config or {}))
        self.context_builder = context_builder
        self.model_gateway = model_gateway
        self.policy_engine = policy_engine
        self.tool_registry = tool_registry
        self.workspace_journal = workspace_journal
        self.completion_policy = completion_policy
        self.verifier = verifier
        self.approval_callback = approval_callback
        self.on_event = on_event
        self.strategy_factory = strategy_factory
        self.strategy_options = dict(strategy_options or {})
        self.last_result: Optional[RunResult] = None
        self.last_legacy_result: Any = None
        self.active_strategy: Optional[AgentStrategy] = None

    def run(
        self,
        spec: RunSpec,
        *,
        strategy: Optional[str] = None,
        resume: bool = False,
    ) -> RunResult:
        """Run one registered strategy with one authoritative event journal.

        Strategy selection is the kernel's single authority. Precedence is an
        explicit ``strategy`` argument, then ``agent_strategy`` from the
        config, then the run specification. An unknown name fails before a run
        directory is created; it is never silently ignored.
        """
        spec.validate()
        selected, source = resolve_agent_strategy(
            self.config, explicit=strategy, spec=spec
        )
        log_root = (
            self.log_root
            or Path(self.config.get("work_subdir", "logs")).expanduser().resolve()
        )
        self.log_root = log_root
        run_dir = log_root / safe_segment(spec.run_id)
        if not resume and selected != "legacy_agent":
            _archive_fresh_run(run_dir, spec.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        events = RunEventJournal(
            run_dir / "trace.jsonl",
            session_id=spec.session_id,
            run_id=spec.run_id,
            turn_id=spec.turn_id,
        )
        if self.on_event is not None:
            events.subscribe(self._on_event)
        events.append(
            "run_started",
            {
                "task_id": spec.run_id,
                "run_id": spec.run_id,
                "session_id": spec.session_id,
                "mode": selected,
                "strategy": selected,
                "strategy_source": source,
                "request": spec.request,
                "repo_path": spec.repository_identity,
                "issue_text": spec.request,
                "resumed": bool(resume),
                "config": dict(self.config),
                "run_spec": _contract_payload(spec),
            },
        )
        events.append(
            "strategy_selected",
            {"strategy": selected, "source": source, "request": spec.request},
        )
        # AGT-01: the effective capability surface is recorded BEFORE the run
        # starts, next to the strategy that selected it, so "it did not search"
        # is answerable from the trace alone. The same receipt rides the result
        # (below) so a ``--json`` consumer never has to re-read the journal.
        capabilities = capability_receipt(selected, self.config)
        events.append("capability_surface", dict(capabilities))
        self.last_legacy_result = None
        try:
            if selected == "verified_fix":
                result = self._run_verified(spec, events, log_root, resume)
            elif selected == "legacy_agent":
                result = self._run_legacy_agent(spec, events, resume)
            else:
                result = self._run_daily(spec, events, run_dir, resume, selected)
        except Exception as exc:
            result = RunResult(
                status=CompletionStatus.FAILED,
                run_id=spec.run_id,
                session_id=spec.session_id,
                trace_path=str(events.path),
                error=str(exc),
                follow_up_needs=[str(exc)],
            )
        if not result.trace_path:
            result.trace_path = str(events.path)
        if not result.run_id:
            result.run_id = spec.run_id
        if not result.session_id:
            result.session_id = spec.session_id
        result.metadata = {
            **dict(result.metadata or {}),
            "capability_surface": dict(capabilities),
        }
        events.append(
            "run_finished",
            {
                "status": result.status,
                "turn": result.attempts,
                "error": result.error,
                "result": result.to_dict(),
            },
        )
        self.last_result = result
        return result

    def cancel(self, run_id: Optional[str] = None) -> bool:
        """Request cancellation of the active strategy for this kernel."""
        strategy = self.active_strategy
        if strategy is None:
            return False
        active_spec = getattr(strategy, "_spec", None)
        active_run_id = str(getattr(active_spec, "run_id", ""))
        if run_id and active_run_id != str(run_id):
            return False
        cancel = getattr(strategy, "cancel", None)
        if not callable(cancel):
            return False
        cancel()
        return True

    def _run_daily(
        self,
        spec: RunSpec,
        events: RunEventJournal,
        run_dir: Path,
        resume: bool,
        selected: str,
    ) -> RunResult:
        session_path = (
            self.log_root / safe_segment(spec.session_id) / "session.json"
            if self.log_root
            else run_dir.parent / safe_segment(spec.session_id) / "session.json"
        )
        store = SessionStore(session_path)
        builder = self.context_builder or ContextBuilder(
            store,
            max_turns=int(self.config.get("session_max_turns", 24)),
            max_summary_chars=int(self.config.get("session_summary_max_chars", 6000)),
            max_diff_chars=int(self.config.get("session_diff_max_chars", 12000)),
        )
        workspace = self.workspace_journal or WorkspaceJournal(
            spec.repository_identity,
            run_dir / "pristine",
            protected_paths=self.config.get("protected_paths", ()),
        )
        workspace.ensure_snapshot()
        gateway = self.model_gateway or ModelGateway(config=self.config)
        rules = self.config.get(
            "permission_rules", self.config.get("agent_permission_rules", [])
        )
        default_action = str(self.config.get("permission_default", "") or "").lower()
        if not default_action:
            default_action = (
                "allow"
                if str(self.config.get("agent_approval", "auto")).lower() == "auto"
                else "ask"
            )
        policy = self.policy_engine or PolicyEngine(
            rules,
            session_id=spec.session_id,
            default_action=default_action,
            protected_paths=self.config.get("protected_paths", ()),
        )
        registry = self.tool_registry or ToolRegistry()
        if self.tool_registry is None:
            registry.restrict(_allowed_tools(selected, self.config))
        completion = self.completion_policy or CompletionPolicy(
            self.verifier,
            config=self.config,
        )
        execution_backend = None
        execution_workspace = None
        if selected == "daily" and self.config.get("safe_tool_backend", True):
            try:
                from execution.workspace import SafeToolBackend, Workspace

                protected = self.config.get("protected_paths", ())
                if isinstance(protected, str):
                    protected = (protected,)
                execution_workspace = Workspace(
                    spec.repository_identity,
                    state_dir=run_dir / "safe-workspace",
                    protected_paths=tuple(protected or ()),
                )
                execution_backend = SafeToolBackend(
                    execution_workspace,
                    approve=lambda tool, arguments: True,
                )
                events.append(
                    "execution_backend_ready",
                    {"backend": "SafeToolBackend", "cancellable_processes": True},
                )
            except Exception as exc:
                events.append(
                    "execution_backend_warning",
                    {"error": str(exc), "fallback": "typed local handler"},
                )
        if self.tool_registry is None:
            build_default_handlers(
                registry,
                repo_path=spec.repository_identity,
                config=self.config,
                completion=completion,
            )
        checkpoints = CheckpointStore(
            run_dir / "checkpoint.json",
            session_id=spec.session_id,
            run_id=spec.run_id,
        )
        strategy_class = {
            "daily": DailyCodingStrategy,
            "planning": PlanningStrategy,
            "question": QuestionStrategy,
            "research": ResearchStrategy,
        }[selected]
        if self.strategy_factory is not None:
            strategy = self.strategy_factory(
                strategy_class=strategy_class,
                context_builder=builder,
                model_gateway=gateway,
                policy=policy,
                tools=registry,
                workspace=workspace,
                completion=completion,
                events=events,
                checkpoints=checkpoints,
                config=self.config,
                approval_callback=self.approval_callback,
            )
        else:
            strategy = strategy_class(
                context_builder=builder,
                model_gateway=gateway,
                policy=policy,
                tools=registry,
                workspace=workspace,
                completion=completion,
                events=events,
                checkpoints=checkpoints,
                config=self.config,
                approval_callback=self.approval_callback,
                execution_backend=execution_backend,
            )
        self.active_strategy = strategy
        try:
            return strategy.run(spec, resume=resume)
        finally:
            if execution_backend is not None:
                try:
                    execution_backend.cancel_side_effects()
                except Exception:
                    pass
                try:
                    from execution.workspace import cleanup_active_processes

                    cleanup_active_processes(timeout_s=5.0)
                except Exception:
                    pass
            if self.active_strategy is strategy:
                self.active_strategy = None
            if execution_workspace is not None:
                try:
                    execution_workspace.close()
                except Exception as exc:
                    events.append(
                        "execution_backend_close_warning", {"error": str(exc)}
                    )

    def _run_legacy_agent(
        self,
        spec: RunSpec,
        events: RunEventJournal,
        resume: bool,
    ) -> RunResult:
        strategy = LegacyAgentStrategy(
            events,
            approval_callback=self.approval_callback,
            plan_guidance=str(self.strategy_options.get("plan_guidance", "")),
            resume_history=str(self.strategy_options.get("resume_history", "")),
            session_context=self.strategy_options.get("session_context"),
        )
        self.active_strategy = strategy
        try:
            result = strategy.run(spec, resume=resume)
        finally:
            self.last_legacy_result = strategy.legacy_result
            if self.active_strategy is strategy:
                self.active_strategy = None
        return result

    def _run_verified(
        self,
        spec: RunSpec,
        events: RunEventJournal,
        log_root: Path,
        resume: bool,
    ) -> RunResult:
        strategy = VerifiedFixStrategy(log_root=log_root, events=events)
        self.active_strategy = strategy
        try:
            result = strategy.run(spec, resume=resume)
        finally:
            self.last_legacy_result = strategy.legacy_result
            if self.active_strategy is strategy:
                self.active_strategy = None
        return result

    def _on_event(self, event: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event.to_dict())
        except Exception:
            return


class SessionController:
    """Serialized session facade over AgentKernel without global live-run state."""

    def __init__(
        self,
        repo_path: str,
        *,
        log_root: Optional[Path | str] = None,
        config: Optional[Mapping[str, Any]] = None,
        kernel_factory: Optional[Callable[..., AgentKernel]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.repo_path = str(repo_path)
        self.log_root = Path(log_root).expanduser().resolve() if log_root else None
        self.config = dict(config or {})
        self.kernel_factory = kernel_factory
        self.on_event = on_event
        self.session_id = str(session_id or new_session_id())
        self.kernels: Dict[str, AgentKernel] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._active_run_id = ""
        self._store: Optional[SessionStore] = None

    def start(self) -> SessionState:
        """Start or resume the durable session lifecycle."""
        with self._lock:
            if self._closed:
                self._closed = False
            root = self.log_root or Path(self.config.get("work_subdir", "logs"))
            self._store = SessionStore(
                root / safe_segment(self.session_id) / "session.json"
            )
            state, _ = self._store.load_with_warnings(self.session_id)
            if state is None:
                state = SessionState(
                    session_id=self.session_id,
                    repository_identity=self.repo_path,
                )
            state.repository_identity = self.repo_path
            self._store.save(state)
            return state

    def run_turn(
        self,
        request: str,
        *,
        run_id: Optional[str] = None,
        strategy: Optional[str] = None,
        resume: bool = False,
        resume_token: Optional[str] = None,
        config: Optional[Mapping[str, Any]] = None,
        strategy_options: Optional[Mapping[str, Any]] = None,
    ) -> RunResult:
        """Run one serialized turn and retain its kernel for cancellation.

        ``strategy`` is optional on purpose: when it is omitted the kernel
        resolves the authoritative strategy from ``agent_strategy`` in the
        merged config, so a caller that configures a mode gets that mode.
        """
        with self._lock:
            self.start()
            merged = dict(self.config)
            merged.update(config or {})
            selected_run = run_id or new_run_id()
            requested = (
                strategy if strategy not in (None, "") else merged.get("agent_strategy")
            )
            selected = _strategy_name(requested or "daily")
            spec = RunSpec(
                session_id=self.session_id,
                run_id=selected_run,
                request=request,
                repository_identity=self.repo_path,
                strategy=selected,
                workspace_policy=dict(merged.get("workspace_policy", {})),
                verification_policy=dict(merged.get("verification_policy", {})),
                resume_token=resume_token,
                metadata={"config": _json_safe(merged)},
            )
            self._active_run_id = selected_run
            kernel = self._kernel(
                spec,
                config=merged,
                strategy_options=dict(strategy_options or {}),
            )
            try:
                return kernel.run(
                    spec,
                    strategy=strategy,
                    resume=resume,
                )
            finally:
                self._active_run_id = ""

    run = run_turn

    def cancel(self, run_id: Optional[str] = None) -> bool:
        """Request cancellation for one retained run in this session."""
        with self._lock:
            selected = run_id or self._active_run_id
            kernel = self.kernels.get(selected)
            return bool(kernel.cancel(selected)) if kernel is not None else False

    def resume(self, run_id: str, request: str = "", **kwargs: Any) -> RunResult:
        """Resume a run in this session using its stored checkpoint."""
        return self.run_turn(
            request or "continue the active task",
            run_id=run_id,
            resume=True,
            **kwargs,
        )

    def close(self) -> SessionState:
        """Close the session after clearing its active-run pointer."""
        with self._lock:
            if self._store is None:
                self.start()
            state, _ = self._store.load_with_warnings(self.session_id)
            if state is None:
                state = SessionState(session_id=self.session_id)
            state.active_run_id = ""
            self._store.save(state)
            self._closed = True
            return state

    def events(self, run_id: str) -> List[Dict[str, Any]]:
        """Read the authoritative event stream for a run."""
        root = self.log_root or Path(self.config.get("work_subdir", "logs"))
        journal = RunEventJournal(root / safe_segment(run_id) / "trace.jsonl")
        return journal.to_records()

    def _kernel(
        self,
        spec: RunSpec,
        config: Optional[Mapping[str, Any]] = None,
        strategy_options: Optional[Mapping[str, Any]] = None,
    ) -> AgentKernel:
        if spec.run_id in self.kernels:
            return self.kernels[spec.run_id]
        merged = dict(self.config)
        merged.update(config or {})
        if self.kernel_factory is not None:
            kernel = self.kernel_factory(
                repo_path=self.repo_path,
                log_root=self.log_root,
                config=merged,
                on_event=self.on_event,
            )
        else:
            kernel = AgentKernel(
                repo_path=self.repo_path,
                log_root=self.log_root,
                config=merged,
                approval_callback=(strategy_options or {}).get("approval_callback"),
                on_event=self.on_event,
                strategy_options=dict(strategy_options or {}),
            )
        self.kernels[spec.run_id] = kernel
        return kernel


def new_session_id() -> str:
    """Return a unique session identifier."""
    return f"session-{uuid.uuid4().hex}"


def new_run_id() -> str:
    """Return a unique run identifier."""
    return f"run-{uuid.uuid4().hex}"


def safe_segment(value: Any) -> str:
    """Return a safe single path segment for logs and session state."""
    text = str(value or "")
    if (
        text
        and text == text.strip()
        and "\x00" not in text
        and not any(char in text for char in '/\\:*?"<>|')
        and text.rstrip(". ") not in {"", ".", ".."}
    ):
        return text
    return (
        "invalid-"
        + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    )


def _strategy_name(value: Any) -> str:
    name = str(value or "daily").strip().lower()
    aliases = {
        "fix": "verified_fix",
        "verified": "verified_fix",
        "daily_coding": "daily",
        "question_research": "question",
        "build": "daily",
        "explore": "research",
        "review": "question",
        "ask": "question",
    }
    name = aliases.get(name, name)
    if name not in STRATEGY_NAMES:
        raise ValueError(f"unknown agent strategy: {name}")
    return name


def resolve_agent_strategy(
    config: Optional[Mapping[str, Any]] = None,
    *,
    explicit: Any = None,
    spec: Any = None,
) -> tuple[str, str]:
    """Return the one authoritative strategy name and where it came from.

    Precedence: an explicit ``strategy`` argument, then the ``agent_strategy``
    config key, then the run specification's own strategy. The
    ``agent_strategy`` key is therefore honored rather than ignored: a caller
    that sets it selects a different strategy, and an unknown value raises
    instead of falling back to a default the caller did not ask for.
    """
    if explicit not in (None, ""):
        return _strategy_name(explicit), "explicit"
    configured = (config or {}).get("agent_strategy")
    if configured not in (None, ""):
        return _strategy_name(configured), "config"
    if spec is not None and getattr(spec, "strategy", None):
        return _strategy_name(spec.strategy), "spec"
    return "daily", "default"


def _allowed_tools(
    strategy: str, config: Optional[Mapping[str, Any]] = None
) -> set[str]:
    """Return the effective tool set for one strategy.

    The strategy inherits the whole capability surface and subtracts the
    capabilities it declared it may not do. There is no per-strategy list to
    forget, so a tool added to the canonical catalog is reachable everywhere
    unless a strategy explicitly withholds its capability.
    """
    return set(capability_receipt(strategy, config)["allowed_tools"])


def _archive_fresh_run(run_dir: Path, run_id: str) -> None:
    if not run_dir.exists():
        return
    archive = run_dir.with_name(
        f"{run_id}.old-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    suffix = 1
    while archive.exists():
        archive = run_dir.with_name(f"{archive.name}-{suffix}")
        suffix += 1
    run_dir.rename(archive)


def _contract_payload(spec: RunSpec) -> Dict[str, Any]:
    try:
        return spec.to_dict()
    except ValueError as exc:
        return {
            "schema_version": spec.schema_version,
            "session_id": spec.session_id,
            "run_id": spec.run_id,
            "request": spec.request,
            "repository_identity": spec.repository_identity,
            "strategy": spec.strategy,
            "contract_error": str(exc),
        }


def _json_safe(value: Mapping[str, Any]) -> Dict[str, Any]:
    import json

    result: Dict[str, Any] = {}
    for key, item in value.items():
        try:
            json.dumps(item)
        except (TypeError, ValueError):
            continue
        result[str(key)] = item
    return result
