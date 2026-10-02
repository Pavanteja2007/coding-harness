"""Role profiles for bounded runtime workflows.

Profiles are policy bundles, not autonomous agents. They select a public
agent-kernel strategy, a closed tool registry, and a default-deny policy so
an orchestration child cannot grow capabilities at runtime.

The ``planner`` profile's tool list is NOT hand-written. It is derived from
``harness.agent_kernel.subagents.plan_mode_tools()`` - the same capability gate
the plan-phase research subagent runs under - so a planner role and a plan
subagent cannot disagree about what "read-only" means. Adding a read tool to the
catalog widens both; adding a mutating one widens neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from harness.agent_kernel import PolicyEngine, ToolRegistry, builtin_tool_specs
from harness.agent_kernel.subagents import (
    PLAN_MODE_REFUSALS,
    plan_mode_tools,
    plan_mode_withheld,
)

ROLE_NAMES = (
    "planner",
    "implementer",
    "explorer",
    "reviewer",
    "debugger",
    "verifier",
    "release_operator",
)

ROLE_ALIASES = {
    "architect": "planner",
    "researcher": "explorer",
    "tester": "debugger",
    "release": "release_operator",
}

# One canonical catalog (harness.tools.typed_tool_specs, re-derived by the
# kernel) is the only tool list, so a role may name a tool by any documented
# alias. Resolve every alias to its canonical spec here; the kernel
# canonicalizes a call's tool name during validation, so permission rules must
# be keyed by the canonical name.
_TOOL_SPECS: Dict[str, Any] = {
    name: entry
    for entry in builtin_tool_specs()
    for name in (entry.name, *entry.aliases)
}


def _planner_tools() -> Tuple[str, ...]:
    """Return the planner role's tool list, derived from the plan-mode gate.

    This is the AGT-05 requirement made structural at the role layer too: a
    planning role that could be handed a mutating tool would be a plan phase
    that can act, which is precisely what a plan is not. The derivation is the
    SAME function the research subagent's registry is restricted with, so the
    two cannot drift.
    """
    return tuple(plan_mode_tools())


def planner_withheld_reason(capability: str) -> str:
    """Return the refusal text a planner role would journal for a capability."""
    return PLAN_MODE_REFUSALS.get(
        str(capability or ""), f"plan mode withholds the {capability} capability"
    )


def planner_capability_gate() -> Dict[str, Any]:
    """Return the planner role's capability gate as a receipt.

    Exposed so a caller (and a test) can ask "what may a planner not do, and
    why" without reconstructing the surface by hand. It is the same receipt the
    research subagent reports.
    """
    return {
        "tools": list(plan_mode_tools()),
        "withheld": dict(plan_mode_withheld()),
    }


def _canonical_role_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    return ROLE_ALIASES.get(name, name)


@dataclass(frozen=True)
class RoleProfile:
    """Immutable defaults for one bounded workflow role."""

    name: str
    strategy: str
    visible_tools: tuple[str, ...]
    denied_side_effects: tuple[str, ...] = ()
    permission_default: str = "deny"
    mutates: bool = False
    accepts_unverified: bool = True
    estimated_cost_usd: float = 0.0
    allow_network: bool = False
    description: str = ""
    permission_rules: tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        name = _canonical_role_name(self.name)
        if name not in ROLE_NAMES:
            raise ValueError(f"unknown workflow role: {self.name}")
        strategy = str(self.strategy or "").strip().lower()
        if strategy not in {"planning", "daily", "question", "research"}:
            raise ValueError(f"unsupported role strategy: {self.strategy}")
        requested = tuple(
            str(tool or "").strip().lower()
            for tool in self.visible_tools
            if str(tool or "").strip()
        )
        unknown = sorted(set(requested) - set(_TOOL_SPECS))
        if unknown:
            raise ValueError(
                f"role {name} references unknown tools: {', '.join(unknown)}"
            )
        tools = tuple(dict.fromkeys(_TOOL_SPECS[tool].name for tool in requested))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "visible_tools", tools)
        object.__setattr__(
            self,
            "denied_side_effects",
            tuple(
                dict.fromkeys(
                    str(item or "").strip().lower()
                    for item in self.denied_side_effects
                    if str(item or "").strip()
                )
            ),
        )
        default = str(self.permission_default or "deny").strip().lower()
        if default not in {"allow", "ask", "deny"}:
            raise ValueError(f"invalid permission default: {self.permission_default}")
        object.__setattr__(self, "permission_default", default)
        object.__setattr__(self, "mutates", bool(self.mutates))
        object.__setattr__(self, "accepts_unverified", bool(self.accepts_unverified))
        object.__setattr__(
            self, "estimated_cost_usd", max(0.0, float(self.estimated_cost_usd))
        )
        object.__setattr__(self, "allow_network", bool(self.allow_network))
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(
            self,
            "permission_rules",
            tuple(
                dict(item)
                for item in self.permission_rules
                if isinstance(item, Mapping)
            ),
        )

    @property
    def requires_spawn_approval(self) -> bool:
        """Return whether the role normally needs approval before spawning."""
        return self.mutates

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible profile description."""
        return {
            "name": self.name,
            "strategy": self.strategy,
            "visible_tools": list(self.visible_tools),
            "denied_side_effects": list(self.denied_side_effects),
            "permission_default": self.permission_default,
            "mutates": self.mutates,
            "accepts_unverified": self.accepts_unverified,
            "estimated_cost_usd": self.estimated_cost_usd,
            "allow_network": self.allow_network,
            "requires_spawn_approval": self.requires_spawn_approval,
            "description": self.description,
            "permission_rules": [dict(item) for item in self.permission_rules],
        }

    def effective_rules(self) -> tuple[Dict[str, Any], ...]:
        """Return default-deny allow rules plus explicit profile overrides."""
        rules: list[Dict[str, Any]] = []
        for tool in self.visible_tools:
            spec = _TOOL_SPECS[tool]
            if spec.side_effect_class in self.denied_side_effects:
                continue
            if spec.side_effect_class == "network" and not self.allow_network:
                continue
            rules.append(
                {
                    "action": "allow",
                    "tool": tool,
                    "name": f"role:{self.name}:allow:{tool}",
                    "scope": "once",
                }
            )
        for rule in self.permission_rules:
            rules.append(dict(rule))
        return tuple(rules)


_ROLES: Dict[str, RoleProfile] = {
    "planner": RoleProfile(
        name="planner",
        strategy="planning",
        # AGT-05: derived from the plan-mode capability gate, not a second
        # hand-written list. The historical hand list was a subset that could
        # silently drift from the gate (a new read tool was invisible to a
        # planner; a renamed tool broke it). This one cannot.
        visible_tools=_planner_tools(),
        description=(
            "Decomposes work and produces a bounded plan. Read-only by "
            "capability, not by instruction: the mutating, shell, network, "
            "memory, mcp and subagent capabilities are unreachable."
        ),
    ),
    "implementer": RoleProfile(
        name="implementer",
        strategy="daily",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "edit",
            "write",
            "apply_patch",
            "git_status",
            "git_diff",
            "shell",
            "test",
            "verify",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        mutates=True,
        accepts_unverified=False,
        estimated_cost_usd=0.25,
        description="Edits only its claimed worktree and must produce verification evidence.",
    ),
    "explorer": RoleProfile(
        name="explorer",
        strategy="research",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "memory",
            "fetch",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        allow_network=True,
        description="Performs bounded read-only repository and web research.",
    ),
    "reviewer": RoleProfile(
        name="reviewer",
        strategy="question",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "memory",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        description="Reviews a dependency's isolated tree without mutation.",
    ),
    "debugger": RoleProfile(
        name="debugger",
        strategy="daily",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "shell",
            "test",
            "verify",
            "memory",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        denied_side_effects=("workspace_write", "network", "external"),
        accepts_unverified=False,
        estimated_cost_usd=0.15,
        description="Runs bounded diagnostics and reports evidence without editing.",
    ),
    "verifier": RoleProfile(
        name="verifier",
        strategy="daily",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "shell",
            "test",
            "verify",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        denied_side_effects=("workspace_write", "network", "external"),
        accepts_unverified=False,
        estimated_cost_usd=0.2,
        description="Runs the declared verification gate and records evidence.",
    ),
    "release_operator": RoleProfile(
        name="release_operator",
        strategy="planning",
        visible_tools=(
            "read",
            "glob",
            "grep",
            "git_status",
            "git_diff",
            "memory",
            "todo",
            "plan",
            "ask",
            "finish",
            "cancel",
        ),
        estimated_cost_usd=0.3,
        description="Prepares a release review; it never mutates or publishes.",
    ),
}


def role_profiles() -> Dict[str, RoleProfile]:
    """Return a copy of the complete role profile registry."""
    return dict(_ROLES)


def get_role_profile(name: str) -> RoleProfile:
    """Return one registered role profile or fail closed."""
    key = _canonical_role_name(name)
    try:
        return _ROLES[key]
    except KeyError as exc:
        raise ValueError(f"unknown workflow role: {name}") from exc


def build_role_registry(profile: RoleProfile | str) -> ToolRegistry:
    """Build a handler-free registry containing only profile-visible tools."""
    selected = get_role_profile(profile) if isinstance(profile, str) else profile
    registry = ToolRegistry()
    registry.restrict(selected.visible_tools)
    return registry


def build_role_policy(
    profile: RoleProfile | str,
    *,
    session_id: str = "",
    protected_paths: Optional[Iterable[str]] = None,
) -> PolicyEngine:
    """Build a default-deny policy for one role and session."""
    selected = get_role_profile(profile) if isinstance(profile, str) else profile
    return PolicyEngine(
        selected.effective_rules(),
        session_id=session_id,
        default_action=selected.permission_default,
        protected_paths=protected_paths or (),
    )


def validate_role_names(names: Iterable[str]) -> tuple[str, ...]:
    """Validate and normalize a sequence of role names."""
    selected = tuple(_canonical_role_name(name) for name in names)
    unknown = sorted(set(selected) - set(ROLE_NAMES))
    if unknown:
        raise ValueError("unknown workflow roles: " + ", ".join(unknown))
    return selected


__all__ = [
    "ROLE_ALIASES",
    "ROLE_NAMES",
    "RoleProfile",
    "build_role_policy",
    "build_role_registry",
    "get_role_profile",
    "planner_capability_gate",
    "planner_withheld_reason",
    "role_profiles",
    "validate_role_names",
]
