"""Versioned skill and subagent declarations with least-privilege enforcement.

Ceiling Prompt 12 asks for three things this module provides and the ceiling
honesty rules demand be provable rather than asserted:

1. **Versioned skill/agent files with model tier and permission
   declarations.** A ``SKILL.md`` or an agent file may carry frontmatter:

       ---
       name: pytest-conventions
       version: 2
       model-tier: medium
       tools: [read, edit, test]
       permissions:
         - allow: read
         - allow: test
         - deny: "write:.git/*"
       ---

   The declaration is *data*. It never reaches the permission engine directly.

2. **Skill selection is explainable in the trace.** :func:`explain_selection`
   produces the exact record a trace row carries: which skills were
   considered, which matched, on which terms, at which version/tier, and —
   for every claim that was refused — the parent-session rule it exceeded.

3. **A skill cannot grant more permission than the parent session.**
   :func:`resolve_permissions` intersects the declaration with the parent
   session's own grants. The intersection is monotonic: a claim outside the
   parent's envelope is *refused and named*, never granted, and there is no
   configuration value that turns a refusal into a grant. The same holds for
   the model tier: a declared tier is clamped to the session's ceiling, so a
   skill cannot promote itself to a more expensive model than the operator
   allowed for the session.

The parent session's envelope is a :class:`SessionPermissions`. It is built
from the same vocabulary the policy engine already uses — allow/ask/deny over
``tool``/``path``/``command_prefix``/``mcp_server``/``network_domain``/
``side_effect_class`` — so there is one permission language in the product, not
two that can drift.

Subagent files (``agents/<name>.md``) are the second half of the surface. They
are loaded from a contained root, rejected when symlinked or when any path
component is a symlink, and validated the same way: an agent that declares a
tool or permission the session lacks is loadable but *reduced*, and the
reduction is on the record.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from shared import security

__all__ = [
    "AGENT_DECLARATION_KEYS",
    "PERMISSION_DIMENSIONS",
    "AgentSpec",
    "PermissionClaim",
    "PermissionResolution",
    "SessionPermissions",
    "SkillDeclaration",
    "declaration_from_object",
    "explain_agent",
    "explain_declaration_for_role",
    "explain_selection",
    "load_agent_specs",
    "parse_agent_markdown",
    "parse_declaration",
    "resolve_permissions",
    "resolve_tools_for_role",
    "role_tool_surface",
]

PERMISSION_DIMENSIONS: tuple[str, ...] = (
    "tool",
    "path",
    "command_prefix",
    "mcp_server",
    "network_domain",
    "side_effect_class",
)

AGENT_DECLARATION_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "version",
    "model-tier",
    "model_tier",
    "tools",
    "permissions",
    "skills",
)

MODEL_TIERS: tuple[str, ...] = ("cheap", "easy", "medium", "hard", "expensive")
_TIER_RANK = {name: index for index, name in enumerate(MODEL_TIERS)}

_MAX_NAME = 80
_MAX_DESCRIPTION = 1_000
_MAX_CLAIMS = 64
_MAX_TOOLS = 64
_MAX_AGENTS = 64
_MAX_AGENT_FILE_BYTES = 64_000
_FM_BOUNDARY = re.compile(r"^---\s*$")
_FM_LINE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")
_CLAIM_LINE = re.compile(r"^\s*-\s*(?P<body>.+?)\s*$")


def _text(value: Any, limit: int = _MAX_DESCRIPTION) -> str:
    """Return a redacted, bounded string for any value."""
    try:
        raw = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - pathological __str__
        raw = ""
    return security.redact_text(raw)[:limit]


def _dimension_text(value: Any) -> str:
    """Return one matcher-dimension value, normalizing absence to ``""``.

    A missing dimension must be *unconstrained*, and ``"None"`` is not a
    matcher — normalizing here keeps an absent field from becoming a literal
    string that matches nothing and reads as a configured rule.
    """
    if value is None:
        return ""
    rendered = _text(value, 400).strip()
    return "" if rendered.casefold() in ("", "none", "null") else rendered


def _declaration_tools_of(obj: Any) -> list[Any]:
    """Return the declared tool list of any declaration object.

    One accessor, because the shape differs across the two producers this
    module consumes: a `harness.skills.Skill` carries ``declared_tools``,
    an `AgentDefinition`-shaped object carries ``tools``, and a plain mapping
    carries whichever key it has. Reading only ``tools`` silently produced an
    EMPTY declared list for a skill - which then reads as "this skill asked
    for nothing" rather than as "the reader asked in the wrong place".
    """
    if isinstance(obj, Mapping):
        data = dict(obj)
        raw = data.get("tools", data.get("declared_tools"))
    else:
        raw = getattr(obj, "declared_tools", None)
        if raw is None:
            raw = getattr(obj, "tools", None)
    return _coerce_list(raw)


def _coerce_list(value: Any) -> list[str]:
    """Return a bounded list of non-empty strings from a list/CSV/scalar.

    Bracketed inline forms (``tools: [read, grep]`` — YAML flow style, which is
    not JSON) are handled by stripping the brackets from each item. Without
    that, ``read`` would arrive as ``[read`` and silently fail to match the
    session's tool surface, turning a declared tool into a refused one for no
    reason a reader could see.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = [part.strip() for part in text.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [_text(item, 200).strip() for item in value]
    else:
        items = [_text(value, 200).strip()]
    cleaned: list[str] = []
    for item in items:
        candidate = item.strip().strip("\"'").strip("[]").strip()
        if candidate and len(candidate) <= 200:
            cleaned.append(candidate)
    return cleaned[:_MAX_CLAIMS]


@dataclass(frozen=True)
class PermissionClaim:
    """One declared ``allow``/``ask``/``deny`` over permission dimensions.

    The shape is the permission-rule form used everywhere else in the product:
    an action plus any subset of the six matcher dimensions, all of which must
    match. ``deny`` claims are kept even though they cannot widen anything —
    a skill that *narrows* itself is honoured, and dropping the claim would
    lose the author's stated intent from the explanation record.
    """

    action: str
    tool: str = ""
    path: str = ""
    command_prefix: str = ""
    mcp_server: str = ""
    network_domain: str = ""
    side_effect_class: str = ""
    raw: str = ""

    _ACTIONS = ("allow", "ask", "deny")

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", str(self.action or "").strip().casefold())
        if self.action not in self._ACTIONS:
            raise ValueError(f"unsupported permission action: {self.action!r}")

    @classmethod
    def from_value(cls, value: Any) -> "PermissionClaim":
        """Build a claim from a mapping, a ``"allow: write"`` string, or a claim."""
        if isinstance(value, PermissionClaim):
            return value
        raw = ""
        if isinstance(value, str):
            raw = value.strip()
            body = raw
            if ":" in body:
                action, _, rest = body.partition(":")
                if action.strip().casefold() in cls._ACTIONS:
                    rest = rest.strip()
                    # `allow: write:.git/*` is a single dimension shorthand.
                    dimension, _, dimension_value = rest.partition(":")
                    if dimension.strip().casefold() in PERMISSION_DIMENSIONS:
                        return cls(
                            action=action.strip(),
                            **{dimension.strip().casefold(): dimension_value.strip()},
                            raw=raw,
                        )
                    return cls(action=action.strip(), tool=rest, raw=raw)
            return cls(action="deny", tool=body, raw=raw)
        if not isinstance(value, Mapping):
            raise ValueError(
                f"permission claim must be a mapping or string, got {type(value).__name__}"
            )
        data = dict(value)
        raw = _text(data, 400)
        action = _text(data.get("action", "allow"), 20).strip().casefold()
        unknown = sorted(
            str(key)
            for key in data
            if str(key) not in ("action", *PERMISSION_DIMENSIONS)
        )
        if unknown:
            raise ValueError(
                "unsupported permission claim field(s): " + ", ".join(unknown)
            )
        fields = {
            name: _dimension_text(data.get(name)) for name in PERMISSION_DIMENSIONS
        }
        return cls(action=action, raw=raw, **fields)

    def matches(self, subject: Mapping[str, Any]) -> bool:
        """Return whether every constrained dimension matches ``subject``."""
        for name in PERMISSION_DIMENSIONS:
            expected = getattr(self, name)
            if not expected or expected == "*":
                continue
            actual = _text(subject.get(name), 400).strip().casefold()
            options = [
                item.strip().casefold() for item in expected.split("|") if item.strip()
            ]
            if not options:
                continue
            if not actual:
                return False
            if not any(
                actual == option or fnmatch.fnmatch(actual, option)
                for option in options
            ):
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible projection (never the raw author text)."""
        return {
            "action": self.action,
            **{
                name: getattr(self, name)
                for name in PERMISSION_DIMENSIONS
                if getattr(self, name)
            },
        }

    def describe(self) -> str:
        """Return a short human-readable rendering."""
        parts = [self.action]
        parts.extend(
            f"{name}={getattr(self, name)}"
            for name in PERMISSION_DIMENSIONS
            if getattr(self, name)
        )
        return " ".join(parts)


@dataclass(frozen=True)
class PermissionResolution:
    """The effective permissions for a declaration inside one session.

    ``granted`` is the intersection. ``refused`` names every claim the parent
    session did not already allow, with the reason, so a reviewer can see the
    attempt rather than just the absence of the grant. ``escalation_attempted``
    is true whenever at least one claim exceeded the parent envelope — that is
    the signal a security review looks for.
    """

    granted: tuple[PermissionClaim, ...] = ()
    denied: tuple[PermissionClaim, ...] = ()
    refused: tuple[dict[str, Any], ...] = ()
    escalation_attempted: bool = False

    @property
    def granted_actions(self) -> tuple[str, ...]:
        """Return the distinct actions present in the effective set."""
        seen: list[str] = []
        for claim in self.granted:
            if claim.action not in seen:
                seen.append(claim.action)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record for a receipt or trace row."""
        return {
            "granted": [claim.as_dict() for claim in self.granted],
            "denied": [claim.as_dict() for claim in self.denied],
            "refused": [dict(item) for item in self.refused],
            "escalation_attempted": bool(self.escalation_attempted),
            "granted_actions": list(self.granted_actions),
        }


@dataclass(frozen=True)
class SessionPermissions:
    """The parent session's permission envelope — the ceiling for any child.

    ``allow`` claims are what the session may actually do; ``deny`` claims are
    refusals that survive every intersection. ``max_model_tier`` is the model's
    spend ceiling in the same ordinal vocabulary the declarations use.
    """

    allow: tuple[PermissionClaim, ...] = ()
    deny: tuple[PermissionClaim, ...] = ()
    allow_tools: tuple[str, ...] = ()
    max_model_tier: str = "hard"

    @classmethod
    def from_value(cls, value: Any) -> "SessionPermissions":
        """Build a session envelope from a mapping or another envelope."""
        if isinstance(value, SessionPermissions):
            return value
        data = dict(value or {}) if isinstance(value, Mapping) else {}
        return cls(
            allow=tuple(
                PermissionClaim.from_value(item) for item in data.get("allow", ()) or ()
            ),
            deny=tuple(
                PermissionClaim.from_value(item) for item in data.get("deny", ()) or ()
            ),
            allow_tools=tuple(_coerce_list(data.get("allow_tools"))),
            max_model_tier=normalize_tier(data.get("max_model_tier") or "hard"),
        )

    def permits(self, claim: PermissionClaim, subject: Mapping[str, Any]) -> bool:
        """Return whether the session already allows exactly this claim.

        A ``deny`` claim is checked first: the envelope's own refusals are
        terminal, so nothing downstream can re-grant them.
        """
        for refusal in self.deny:
            if refusal.matches(subject):
                return False
        if claim.action == "deny":
            # A declared self-denial is always admissible; it narrows.
            return True
        if not self.allow:
            # An envelope with no explicit allow grants nothing beyond
            # "deny" — least privilege, and the documented default.
            return False
        return any(entry.matches(subject) for entry in self.allow)

    def allows_tool(self, tool: str) -> bool:
        """Return whether the session exposes ``tool`` at all."""
        if not self.allow_tools:
            return False
        name = _text(tool, 200).strip().casefold()
        return any(
            name == entry.casefold() or fnmatch.fnmatch(name, entry.casefold())
            for entry in self.allow_tools
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record (no raw author text)."""
        return {
            "allow": [claim.as_dict() for claim in self.allow],
            "deny": [claim.as_dict() for claim in self.deny],
            "allow_tools": list(self.allow_tools),
            "max_model_tier": self.max_model_tier,
        }


def normalize_tier(value: Any) -> str:
    """Return a canonical model tier, defaulting to ``medium`` when unknown."""
    text = _text(value, 40).strip().casefold()
    if text in _TIER_RANK:
        return text
    aliases = {
        "small": "cheap",
        "fast": "cheap",
        "lite": "cheap",
        "simple": "easy",
        "standard": "medium",
        "default": "medium",
        "big": "expensive",
        "large": "expensive",
        "strong": "expensive",
    }
    return aliases.get(text, "medium")


def clamp_tier(requested: Any, ceiling: Any) -> tuple[str, str]:
    """Clamp a requested model tier to the session ceiling.

    Returns ``(effective_tier, reason)``. A request above the ceiling is
    clamped down and the reason says so; a request below it is honoured as
    written. There is no input that raises the ceiling, which is the property
    "a skill cannot grant more than its parent session" needs.
    """
    want = normalize_tier(requested)
    cap = normalize_tier(ceiling)
    if _TIER_RANK[want] <= _TIER_RANK[cap]:
        return want, ""
    return cap, (
        f"model tier {want!r} exceeds the session ceiling {cap!r}; clamped down"
    )


def resolve_permissions(
    claims: Iterable[Any],
    session: Union[SessionPermissions, Mapping[str, Any]],
    *,
    subject_defaults: Optional[Mapping[str, Any]] = None,
) -> PermissionResolution:
    """Intersect declared permission claims with the parent session envelope.

    The result is the only set a child may act on. A claim the session does not
    already permit is recorded in ``refused`` with the reason and excluded from
    ``granted``; the function never promotes a claim. ``deny`` claims always
    survive, so a skill that narrows itself is honoured even in an envelope
    that otherwise grants nothing.

    ``subject_defaults`` supplies the dimension values a claim is evaluated
    against when it names a dimension but not a target (for example a bare
    ``allow: write`` evaluated against the session's configured tool surface).
    """
    envelope = SessionPermissions.from_value(session)
    defaults = dict(subject_defaults or {})
    granted: list[PermissionClaim] = []
    denied: list[PermissionClaim] = []
    refused: list[dict[str, Any]] = []
    for raw in list(claims or ())[:_MAX_CLAIMS]:
        try:
            claim = PermissionClaim.from_value(raw)
        except ValueError as exc:
            refused.append(
                {"claim": _text(raw, 200), "reason": f"invalid claim: {exc}"}
            )
            continue
        subject = {
            name: defaults.get(name, "") or claim_value(claim, name)
            for name in PERMISSION_DIMENSIONS
        }
        if not envelope.permits(claim, subject):
            denied.append(claim)
            refused.append(
                {
                    "claim": claim.describe(),
                    "action": claim.action,
                    "reason": "not permitted by the parent session envelope",
                }
            )
            continue
        granted.append(claim)
    return PermissionResolution(
        granted=tuple(granted),
        denied=tuple(denied),
        refused=tuple(refused),
        escalation_attempted=bool(refused),
    )


def claim_value(claim: PermissionClaim, dimension: str) -> str:
    """Return one dimension value from a claim (empty when unconstrained)."""
    return _text(getattr(claim, dimension, ""), 400)


def resolve_tools(
    declared: Iterable[Any], session: Union[SessionPermissions, Mapping[str, Any]]
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Intersect a declared tool list with the session's visible tool surface.

    Returns ``(tools, refused)``. A session that exposes no tools grants none;
    that is the least-privilege default and it is the same rule a role
    profile in :mod:`runtime.roles` already follows.
    """
    envelope = SessionPermissions.from_value(session)
    tools: list[str] = []
    refused: list[dict[str, Any]] = []
    for raw in list(declared or ())[:_MAX_TOOLS]:
        name = _text(raw, 200).strip().casefold()
        if not name:
            continue
        if envelope.allows_tool(name):
            if name not in tools:
                tools.append(name)
        else:
            refused.append(
                {
                    "tool": name,
                    "reason": "tool is not exposed by the parent session",
                }
            )
    return tuple(tools), tuple(refused)


def role_tool_surface(role: Any) -> tuple[str, ...]:
    """Return the tools a role profile exposes, or ``()`` if unknown.

    Reads :mod:`runtime.roles` rather than restating a list: a hand-written
    copy is exactly how a renamed tool silently disappears from the policy
    layer while remaining visible in the catalog. An unknown role returns
    the EMPTY tuple, which means "exposes nothing" - the least-privilege
    direction, and the same answer a missing role profile gives.
    """
    name = str(role or "").strip()
    if not name:
        return ()
    try:
        from runtime.roles import ROLE_ALIASES, get_role_profile

        profile = get_role_profile(ROLE_ALIASES.get(name.lower(), name.lower()))
    except Exception:
        return ()
    return tuple(str(item) for item in getattr(profile, "visible_tools", ()) or ())


def resolve_tools_for_role(
    declared: Iterable[Any], role: Any
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Intersect a declared tool list with a ROLE PROFILE's visible surface.

    The distinction this closes: :func:`resolve_tools` intersects with the
    parent SESSION's tool surface, and a session's surface is whatever the
    operator configured. A role profile is a different, narrower ceiling -
    the set of tools the ROLE is built around. A skill that declares a tool
    the session happens to expose but the role does not is refused, and the
    refusal names both ceilings so a reader can see which one was hit.

    Returns the same ``(tools, refused)`` shape as :func:`resolve_tools`,
    with the refusal reason naming the ROLE and the tool. The refusal is
    still a refusal: nothing here can promote a declared tool into the
    effective set.
    """
    surface = role_tool_surface(role)
    allowed = {item.casefold() for item in surface}
    tools: list[str] = []
    refused: list[dict[str, Any]] = []
    label = str(role or "").strip() or "unspecified"
    for raw in list(declared or ())[:_MAX_TOOLS]:
        name = _text(raw, 200).strip().casefold()
        if not name:
            continue
        if name in allowed:
            if name not in tools:
                tools.append(name)
        else:
            refused.append(
                {
                    "tool": name,
                    "role": label,
                    "reason": (
                        f"tool is not exposed by the {label!r} role profile"
                        if surface
                        else (
                            f"role profile {label!r} is unknown, so it exposes no tools"
                        )
                    ),
                    "role_tools": list(surface),
                }
            )
    return tuple(tools), tuple(refused)


def explain_declaration_for_role(
    obj: Any,
    role: Any,
    session: Union[SessionPermissions, Mapping[str, Any], None] = None,
) -> dict[str, Any]:
    """Explain one declaration against a role profile AND a session envelope.

    The composed answer, because "which tools does this skill get" has two
    ceilings and reporting only one of them is how a person concludes a
    skill can write files when its role cannot. Both intersections are
    computed and both are reported; the EFFECTIVE tool set is the stricter
    of the two, so the record cannot claim a tool that either ceiling would
    refuse.

    ``session`` is the parent session's envelope (the existing
    :class:`SessionPermissions`); it is OPTIONAL, and omitting it computes the
    role half alone - which is what a definition-facing surface has, since a
    definition is validated against its role before any session exists.
    ``role`` is a role name or profile. The returned record keeps
    ``session_tools`` and ``role_tools`` apart, so a surface can say which
    ceiling did the work.
    """
    envelope = SessionPermissions.from_value(session)
    declaration = declaration_from_object(obj)
    if envelope.allow or envelope.deny or envelope.allow_tools:
        session_tools, session_refusals = resolve_tools(declaration.tools, envelope)
        permissions = resolve_permissions(declaration.claims, envelope)
        ceiling = envelope.max_model_tier
        session_present = True
    else:
        # No envelope was supplied. The session half is then UNCONSTRAINED -
        # reported as the declared set with an empty refusal list - so the
        # record says "the role refused this" rather than "everything was
        # refused twice", which would name a ceiling that was not in force.
        session_tools = tuple(declaration.tools)
        session_refusals = ()
        permissions = PermissionResolution()
        ceiling = MODEL_TIERS[-1]
        session_present = False
    role_tools, role_refusals = resolve_tools_for_role(declaration.tools, role)
    allowed_by_role = {item.casefold() for item in role_tools}
    # The effective set is the STRICTER of the two ceilings, ALWAYS. Gating
    # the role intersection on `session_present` meant an absent envelope -
    # which is the common case for a definition-facing surface, where no
    # session exists yet - returned the whole declared list. A record whose
    # `refused` names a tool while `effective_tools` still contains it is the
    # exact "it says no and reads yes" shape this composition exists to
    # prevent.
    effective = tuple(
        name for name in session_tools if name.casefold() in allowed_by_role
    )
    tier, tier_reason = clamp_tier(declaration.model_tier, ceiling)
    refused = (
        list(session_refusals)
        + list(role_refusals)
        + [dict(item) for item in permissions.refused]
    )
    if tier_reason:
        refused.append(
            {"claim": f"model-tier: {declaration.model_tier}", "reason": tier_reason}
        )
    return {
        "name": declaration.name,
        "kind": getattr(obj, "kind", None) or "declaration",
        "role": str(role or ""),
        "declared_tools": list(declaration.tools),
        "session_tools": list(session_tools),
        "role_tools": list(role_tools),
        "effective_tools": list(effective),
        "session_envelope_present": session_present,
        "declared_model_tier": declaration.model_tier,
        "effective_model_tier": tier,
        "permissions": permissions.to_dict(),
        "refused": refused,
        "escalation_attempted": bool(refused),
        "digest": declaration.digest,
    }


@dataclass(frozen=True)
class SkillDeclaration:
    """The version/tier/permission declaration carried by a skill file.

    ``version`` is an integer (a skill that declares a non-integer version is
    treated as version 1 with a recorded diagnostic rather than failing to
    load, because a skill body is untrusted content and must never be able to
    break discovery). ``model_tier`` is the *declared* tier; the effective tier
    is the clamp against the session ceiling computed by
    :func:`clamp_tier`.
    """

    name: str = ""
    version: int = 1
    model_tier: str = "medium"
    tools: tuple[str, ...] = ()
    claims: tuple[PermissionClaim, ...] = ()
    skills: tuple[str, ...] = ()
    digest: str = ""
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record (no body, no author text)."""
        return {
            "name": self.name,
            "version": int(self.version),
            "model_tier": self.model_tier,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "claims": [claim.as_dict() for claim in self.claims],
            "digest": self.digest,
            "diagnostics": list(self.diagnostics),
        }

    def explain(
        self, session: Union[SessionPermissions, Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Return the explainable record for one skill inside one session."""
        envelope = SessionPermissions.from_value(session)
        tools, tool_refusals = resolve_tools(self.tools, envelope)
        permissions = resolve_permissions(self.claims, envelope)
        tier, tier_reason = clamp_tier(self.model_tier, envelope.max_model_tier)
        refused = list(permissions.refused) + list(tool_refusals)
        if tier_reason:
            refused.append(
                {"claim": f"model-tier: {self.model_tier}", "reason": tier_reason}
            )
        return {
            "name": self.name,
            "version": int(self.version),
            "declared_model_tier": self.model_tier,
            "effective_model_tier": tier,
            "effective_tools": list(tools),
            "permissions": permissions.to_dict(),
            "refused": refused,
            "escalation_attempted": bool(refused),
            "digest": self.digest,
        }


def parse_declaration(frontmatter: Any, *, name: str = "") -> SkillDeclaration:
    """Build a :class:`SkillDeclaration` from parsed frontmatter.

    Accepts the frontmatter mapping ``harness.skills`` already extracts plus any
    extra keys a caller wants to carry. Unknown keys are ignored (a skill file
    is content, not a schema), and a malformed ``permissions`` block becomes a
    recorded diagnostic rather than an exception.
    """
    data = dict(frontmatter or {}) if isinstance(frontmatter, Mapping) else {}
    diagnostics: list[str] = []
    version_raw = data.get("version")
    version = 1
    if version_raw not in (None, ""):
        try:
            version = max(1, int(str(version_raw).strip()))
        except (TypeError, ValueError):
            diagnostics.append("version is not an integer; treated as 1")
    tier = normalize_tier(data.get("model-tier") or data.get("model_tier") or "medium")
    claims: list[PermissionClaim] = []
    for item in data.get("permissions", ()) or ():
        try:
            claims.append(PermissionClaim.from_value(item))
        except ValueError as exc:
            diagnostics.append(f"permission claim refused: {exc}")
    digest_source = json.dumps(
        {
            "name": _text(data.get("name") or name, _MAX_NAME),
            "version": version,
            "model_tier": tier,
            "tools": sorted(_coerce_list(data.get("tools"))),
            "claims": sorted(claim.describe() for claim in claims),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return SkillDeclaration(
        name=_text(data.get("name") or name, _MAX_NAME),
        version=version,
        model_tier=tier,
        tools=tuple(_coerce_list(data.get("tools"))),
        claims=tuple(claims),
        skills=tuple(_coerce_list(data.get("skills"))),
        digest=hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32],
        diagnostics=tuple(diagnostics),
    )


def declaration_from_object(obj: Any, *, name: str = "") -> SkillDeclaration:
    """Build a declaration from ANY object that carries the four fields.

    Accepts a ``harness.skills.Skill`` (the versioned fields Ceiling 12 added
    to it), a ``runtime.subagents.AgentDefinition`` (Terminal 06's agent
    authority), or a historical bare object. This is the single adapter the
    policy layer uses, so there is ONE permission-intersection implementation
    regardless of which loader produced the declaration - the anti-drift
    property that matters more than which file parses the markdown.

    A T06 agent's ``version`` is semver (``"2.1.0"``); this layer treats a
    non-integer version as version 1 with a recorded diagnostic rather than
    failing, because the declaration is untrusted data and the authoritative
    semver check already happened in the loader that produced it.
    """
    if isinstance(obj, Mapping):
        raw = dict(obj)
        name_value = raw.get("name") or ""
        version_value = raw.get("version")
        tier_value = raw.get("model-tier", raw.get("model_tier"))
        claims_value = raw.get("permissions", raw.get("claims"))
    else:
        name_value = getattr(obj, "name", "") or ""
        version_value = getattr(obj, "version", None)
        tier_value = getattr(obj, "model_tier", None)
        claims_value = getattr(obj, "permissions", None) or getattr(
            obj, "permission_claims", None
        )
    return parse_declaration(
        {
            "name": name_value,
            "version": version_value,
            "model-tier": tier_value,
            "tools": _declaration_tools_of(obj),
            "permissions": claims_value,
        },
        name=str(name or name_value),
    )


#: Backwards-compatible alias for the Skill-specific spelling.
declaration_from_skill = declaration_from_object


def explain_agent(
    agent: Any,
    session: Union[SessionPermissions, Mapping[str, Any]],
    *,
    kind: str = "agent",
) -> dict[str, Any]:
    """Explain one subagent (or skill) declaration inside one session.

    Works for a ``runtime.subagents.AgentDefinition``, a
    ``harness.skills.Skill``, or a plain mapping, because it goes through
    :func:`declaration_from_object`. That is the point: the *spawn* path
    (Terminal 06) and the *policy* path (this module) evaluate the same
    declaration against the same envelope, so a role-profile ceiling and a
    parent-session ceiling compose instead of competing.
    """
    if isinstance(agent, Mapping):
        base: dict[str, Any] = dict(agent)
    else:
        base = declaration_from_object(agent).to_dict()
    base["model_tier"] = base.get("model_tier") or getattr(agent, "model_tier", "")
    base["tools"] = base.get("tools") or list(getattr(agent, "tools", ()) or ())
    base["permissions"] = base.get("claims") or base.get("permissions") or []
    record = parse_declaration(base, name=str(base.get("name", ""))).explain(session)
    record["kind"] = kind
    record["source"] = _text(getattr(agent, "source_path", ""), 512)
    if getattr(agent, "role", ""):
        record["role"] = _text(agent.role, 60)
    return record


# ---------------------------------------------------------------------------
# Subagent files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One versioned subagent declaration loaded from an ``agents/*.md`` file."""

    name: str
    description: str = ""
    version: int = 1
    model_tier: str = "medium"
    tools: tuple[str, ...] = ()
    claims: tuple[PermissionClaim, ...] = ()
    skills: tuple[str, ...] = ()
    source: str = ""
    body: str = ""
    digest: str = ""
    diagnostics: tuple[str, ...] = field(default=())

    def explain(
        self, session: Union[SessionPermissions, Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Return the explainable record for this agent inside one session."""
        declaration = SkillDeclaration(
            name=self.name,
            version=self.version,
            model_tier=self.model_tier,
            tools=self.tools,
            claims=self.claims,
            skills=self.skills,
            digest=self.digest,
            diagnostics=self.diagnostics,
        )
        record = declaration.explain(session)
        record["kind"] = "agent"
        record["source"] = self.source
        record["body_chars"] = len(self.body)
        return record

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record (no body)."""
        return {
            "kind": "agent",
            "name": self.name,
            "description": self.description,
            "version": int(self.version),
            "model_tier": self.model_tier,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "claims": [claim.as_dict() for claim in self.claims],
            "source": self.source,
            "digest": self.digest,
            "diagnostics": list(self.diagnostics),
            "body_chars": len(self.body),
        }


def _split_frontmatter(text: str) -> tuple[dict[str, str], str, list[str]]:
    """Return ``(frontmatter, body, diagnostics)`` for a markdown file."""
    lines = text.splitlines()
    meta: dict[str, str] = {}
    diagnostics: list[str] = []
    body_start = 0
    if lines and _FM_BOUNDARY.match(lines[0]):
        closing = None
        for index in range(1, len(lines)):
            if _FM_BOUNDARY.match(lines[index]):
                closing = index
                break
            match = _FM_LINE.match(lines[index])
            if match:
                meta[match.group(1).strip().casefold()] = match.group(2).strip()
                continue
            item = _CLAIM_LINE.match(lines[index])
            if item and "permissions" in meta:
                meta.setdefault("__permissions_lines__", "")
                meta["__permissions_lines__"] += item.group("body") + "\n"
        if closing is None:
            diagnostics.append("frontmatter was not closed; treated as body-only")
            return {}, text, diagnostics
        body_start = closing + 1
    return meta, "\n".join(lines[body_start:]).strip(), diagnostics


def parse_agent_markdown(text: Any, *, source: str = "") -> AgentSpec:
    """Parse one agent markdown file into a versioned :class:`AgentSpec`.

    Never raises: a malformed file becomes an agent with the diagnostics
    recorded and no permissions, so a broken agent file is an auditable
    downgrade rather than a crash. The body is the untrusted-content boundary's
    business — callers pass the *reviewed* body text in (see
    ``harness.skills`` for the shared review entry point), and this function
    only structures it.
    """
    diagnostics: list[str] = []
    if not isinstance(text, str):
        return AgentSpec(
            name=_text(source, _MAX_NAME) or "agent",
            source=_text(source, 512),
            diagnostics=("agent file is not text",),
        )
    meta, body, parse_notes = _split_frontmatter(text)
    diagnostics.extend(parse_notes)
    raw_permissions = meta.pop("__permissions_lines__", "").strip()
    claims: list[PermissionClaim] = []
    if raw_permissions:
        parsed_items: list[Any] = []
        try:
            loaded = json.loads(raw_permissions)
            if isinstance(loaded, list):
                parsed_items = list(loaded)
            else:
                parsed_items = [loaded]
        except ValueError:
            parsed_items = [
                line.strip() for line in raw_permissions.splitlines() if line.strip()
            ]
        for item in parsed_items:
            try:
                claims.append(PermissionClaim.from_value(item))
            except ValueError as exc:
                diagnostics.append(f"permission claim refused: {exc}")
    declaration = parse_declaration(meta, name=meta.get("name", ""))
    name = declaration.name or Path(source).stem or "agent"
    return AgentSpec(
        name=_text(name, _MAX_NAME),
        description=_text(meta.get("description"), _MAX_DESCRIPTION),
        version=declaration.version,
        model_tier=declaration.model_tier,
        tools=declaration.tools,
        claims=tuple(claims) or declaration.claims,
        skills=declaration.skills,
        source=_text(source, 512),
        body=body[:_MAX_AGENT_FILE_BYTES],
        digest=declaration.digest,
        diagnostics=tuple(diagnostics + list(declaration.diagnostics)),
    )


def _has_symlink_component(path: Path) -> bool:
    """Return whether any component of ``path`` is a symlink."""
    try:
        current = Path(path)
        while True:
            if current.is_symlink():
                return True
            parent = current.parent
            if parent == current:
                return False
            current = parent
    except (OSError, RuntimeError, ValueError):
        return True


def load_agent_specs(
    root: Union[str, Path], *, diagnostics: Optional[list[dict[str, Any]]] = None
) -> list[AgentSpec]:
    """Load versioned subagent files from a contained ``agents`` directory.

    Assumes the caller supplies the root explicitly. The loader refuses a
    symlinked root, a symlinked entry, a non-``.md`` file, a file over
    :data:`_MAX_AGENT_FILE_BYTES`, and any name containing a path separator.
    Everything it skips is appended to ``diagnostics``, so an unreadable agent
    directory is a recorded skip and never a silent absence.
    """
    base = Path(root).expanduser()
    if _has_symlink_component(base) or not base.is_dir():
        if diagnostics is not None:
            diagnostics.append(
                {"root": str(base), "error": "agent root is not a contained directory"}
            )
        return []
    specs: list[AgentSpec] = []
    try:
        entries = sorted(base.iterdir())
    except OSError as exc:
        if diagnostics is not None:
            diagnostics.append(
                {"root": str(base), "error": f"cannot list agent root: {exc}"}
            )
        return []
    for entry in entries[:_MAX_AGENTS]:
        if entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.suffix.lower() != ".md":
            continue
        if _has_symlink_component(entry):
            if diagnostics is not None:
                diagnostics.append(
                    {"source": str(entry), "error": "symlinked agent file refused"}
                )
            continue
        try:
            if entry.stat().st_size > _MAX_AGENT_FILE_BYTES:
                if diagnostics is not None:
                    diagnostics.append(
                        {"source": str(entry), "error": "agent file too large"}
                    )
                continue
            text = entry.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            if diagnostics is not None:
                diagnostics.append(
                    {"source": str(entry), "error": f"cannot read agent file: {exc}"}
                )
            continue
        spec = parse_agent_markdown(text, source=str(entry))
        if not spec.name or any(token in spec.name for token in ("/", "\\", "..")):
            if diagnostics is not None:
                diagnostics.append(
                    {"source": str(entry), "error": "agent name is unsafe"}
                )
            continue
        specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------


def explain_selection(
    selected: Iterable[Any],
    *,
    session: Union[SessionPermissions, Mapping[str, Any]],
    considered: int = 0,
    skipped: Optional[str] = None,
    error: Optional[str] = None,
    receipts: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the trace-explained record for one skill-selection decision.

    This is the record a ``skills`` trace row carries. It answers, without a
    re-run: which skills were considered, which were selected, why each one
    matched, what version/tier/permissions each declared, and — for every
    claim the parent session would not permit — the refusal. An empty
    selection is a first-class record too: ``considered: 0`` with an explicit
    ``skipped`` reason is auditable, which is the whole point of the receipt
    discipline the rest of the harness already follows.
    """
    rows: list[dict[str, Any]] = []
    escalations: list[str] = []
    for skill in selected or ():
        declaration = declaration_from_skill(skill)
        record = declaration.explain(session)
        record["kind"] = "skill"
        record["origin"] = _text(getattr(skill, "origin", ""), 40)
        record["source"] = _text(getattr(skill, "source", ""), 512)
        record["tainted"] = bool(getattr(skill, "tainted", False))
        rows.append(record)
        if record["escalation_attempted"]:
            escalations.append(record["name"])
    by_name = {row["name"]: row for row in rows}
    for receipt in receipts or ():
        if not isinstance(receipt, Mapping):
            continue
        name = str(receipt.get("name") or "")
        row = by_name.get(name)
        if row is None:
            continue
        row["matched_terms"] = [str(item) for item in receipt.get("matched_terms", [])]
        row["match_reason"] = _text(receipt.get("reason"), 512)
    return {
        "considered": int(considered or len(rows)),
        "selected": [row["name"] for row in rows],
        "records": rows,
        "refused_claims": {
            row["name"]: [dict(item) for item in row["refused"]] for row in rows
        },
        "escalation_attempts": sorted(escalations),
        "skipped": skipped,
        "error": error,
    }
