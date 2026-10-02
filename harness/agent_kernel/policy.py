"""Fine-grained, auditable permissions for typed agent tool calls.

**AGT-07 (2026-09-28):** this module now answers the OTHER axis as well, and
keeps the two apart by construction. :meth:`PolicyEngine.evaluate_axes`
returns a :class:`SafetyReceipt` carrying (a) *technical containment* -- what
the sandbox permits, reported by ``execution.sandbox`` -- beside (b) the
*decision* this engine has always made. The two used to be one sentence, so
"it did not ask" and "it could do anything" were the same claim.

The separation is mechanical, not aspirational:

* :meth:`SafetyReceipt.from_decision_only` is the ONLY way to build a
  receipt without a sandbox receipt, and it produces
  ``ContainmentAxis.unknown()``. A caller holding a decision can therefore
  never obtain a containment claim -- not by forgetting, not by passing
  ``None``.
* :attr:`ContainmentAxis.contained` is False while the axis is unknown, so
  "we don't know" can never be rendered as "it was fine".
* :meth:`SafetyReceipt.axes_confused` reports a receipt whose containment
  half carries a prompting field (or whose decision half carries a sandbox
  field). Such a receipt is refused rather than rendered, which is how a
  future edit that merges the axes fails a test instead of shipping a lie.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from shared.approval import (
    APPROVAL_SCOPES,
    CONTAINMENT_AXIS,
    DECISION_AXIS,
    command_prefix_matches,
    describe_axes,
    empty_prefix_refusal,
    normalize_approval_scope,
    retired_scope_note,
)

from .contracts import PERMISSION_ACTIONS, PermissionDecision, ToolCall

# The scope vocabulary and the prefix matcher are owned by shared.approval --
# ONE implementation, imported not forked, so the kernel policy and every
# terminal surface cannot drift on what a grant covers. `global` was removed
# there (R2-15): a scope that matches every call is not a scope.
SCOPE_ONCE = "once"
SCOPE_EXACT_CALL = "exact_call"
SCOPE_SESSION_PATH = "session_path"
SCOPE_SESSION_COMMAND_PREFIX = "session_command_prefix"

_DEFAULT_PROTECTED_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "_darcs",
        "logs",
        "harness",
        "execution",
        "runtime",
        "memory",
        "mcp_server",
        "shared",
        "secrets",
        "credentials",
        ".ssh",
    }
)
_DEFAULT_PROTECTED_PATTERNS = (
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials*",
    "secrets*",
    ".pypirc",
    ".netrc",
    "auth.json",
    "service-account*.json",
)
_PROTECTED_COMMAND_RE = re.compile(
    r"(?:^|[\s\"'=:/])(?:\.git|\.hg|\.svn|\.bzr|_darcs|logs|harness|execution|runtime|memory|mcp_server|shared|\.env(?:\.[\w.-]+)?|\.pypirc|\.netrc|credentials[\w.-]*|secrets?[\w.-]*|[\w.-]+\.(?:pem|key|p12|pfx)|id_(?:rsa|ed25519|ecdsa)[\w.-]*)(?:$|[\\/:\s\"'])",
    re.IGNORECASE,
)


def _normalized_path_parts(value: Any) -> tuple[str, ...]:
    text = str(value or "").replace("\\", "/").strip().strip("\"'`")
    if not text:
        return ()
    parts: List[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part.lower())
    return tuple(parts)


def _protected_path_reason(path: Any, patterns: Iterable[str]) -> str:
    parts = _normalized_path_parts(path)
    if not parts:
        return ""
    for component in parts:
        if component in _DEFAULT_PROTECTED_COMPONENTS:
            return f"protected path component refused: {component}"
    basename = parts[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(basename, str(pattern).replace("\\", "/").lower()):
            return f"secret path refused: {basename}"
    return ""


def protected_call_reason(call: ToolCall, extra_patterns: Iterable[str] = ()) -> str:
    """Return a terminal refusal reason for infrastructure or secret targets."""
    configured = tuple(
        str(item).replace("\\", "/").lower() for item in extra_patterns or ()
    )
    patterns = tuple(_DEFAULT_PROTECTED_PATTERNS) + configured
    dimensions = call_dimensions(call)
    for path in (dimensions["path"], call.target):
        reason = _protected_path_reason(path, patterns)
        if reason:
            return reason
        normalized = "/".join(_normalized_path_parts(path))
        for pattern in configured:
            if fnmatch.fnmatch(normalized, pattern) or any(
                fnmatch.fnmatch(part, pattern) for part in _normalized_path_parts(path)
            ):
                return f"configured protected path refused: {path}"
    command = str(dimensions["command"] or "")
    if command and _PROTECTED_COMMAND_RE.search(command):
        return "protected path or harness directory referenced by command"
    return ""


@dataclass
class PolicyRule:
    """One allow, ask, or deny matcher across permission dimensions."""

    action: str
    tool: Optional[str] = None
    path: Optional[str] = None
    command_prefix: Optional[str] = None
    mcp_server: Optional[str] = None
    network_domain: Optional[str] = None
    side_effect_class: Optional[str] = None
    scope: str = "once"
    actor: str = "*"
    name: str = ""

    def __post_init__(self) -> None:
        self.action = str(self.action or "deny").lower()
        if self.action not in PERMISSION_ACTIONS:
            raise ValueError(f"unsupported permission action: {self.action}")
        self.tool = _optional_lower(self.tool)
        self.mcp_server = _optional_string(self.mcp_server)
        self.network_domain = _optional_lower(self.network_domain)
        self.side_effect_class = _optional_lower(self.side_effect_class)
        self.scope = normalize_scope(self.scope)
        self.actor = str(self.actor or "*")
        self.name = str(self.name or "")
        # A PRESENT-BUT-EMPTY command prefix used to be a blanket allow: the
        # matcher tested `if self.command_prefix and ...`, so "" was skipped and
        # the rule matched every command. It is a configuration error, refused
        # with a message that says so (R2-15). An ABSENT prefix (None) is
        # still "this rule does not constrain commands".
        refusal = empty_prefix_refusal(self.command_prefix)
        if refusal:
            raise ValueError(refusal)

    @classmethod
    def from_value(cls, value: "PolicyRule | Mapping[str, Any]") -> "PolicyRule":
        """Build a rule from a rule object or mapping."""
        if isinstance(value, cls):
            return value
        data = dict(value or {})
        return cls(
            action=data.get("action", "deny"),
            tool=data.get("tool"),
            path=data.get("path"),
            command_prefix=data.get("command_prefix", data.get("command")),
            mcp_server=data.get("mcp_server", data.get("server")),
            network_domain=data.get("network_domain", data.get("domain")),
            side_effect_class=data.get("side_effect_class", data.get("side_effect")),
            scope=data.get("scope", "once"),
            actor=data.get("actor", "*"),
            name=data.get("name", ""),
        )

    def matches(
        self, call: ToolCall, context: Optional[Mapping[str, Any]] = None
    ) -> bool:
        """Return whether every configured matcher matches the call."""
        details = call_dimensions(call)
        if self.tool and self.tool != details["tool"]:
            return False
        if self.path and not _path_matches(details["path"], self.path):
            return False
        if self.command_prefix and not (
            _prefix_matches(details["command"], self.command_prefix)
            or _prefix_matches(details["command_prefix"], self.command_prefix)
        ):
            return False
        if self.mcp_server and self.mcp_server != details["mcp_server"]:
            return False
        if self.network_domain and not _domain_matches(
            details["network_domain"], self.network_domain
        ):
            return False
        if self.side_effect_class and self.side_effect_class != call.side_effect_class:
            return False
        actor = str((context or {}).get("actor") or "agent")
        return self.actor in ("*", actor)

    def specificity(self) -> int:
        """Return a stable specificity score for precedence."""
        return sum(
            bool(value)
            for value in (
                self.tool,
                self.path,
                self.command_prefix,
                self.mcp_server,
                self.network_domain,
                self.side_effect_class,
            )
        )


@dataclass
class ApprovalGrant:
    """A previously approved scope for future calls.

    R2-15: there is no ``global`` grant. A grant that matched every call
    regardless of tool, effect, repository, or command was an unconditional
    bypass -- one answer defeated the whole policy -- and it is deleted rather
    than narrowed, because "approve everything" is not a decision an operator
    can meaningfully have made about one request. A caller that asks for it is
    given the narrowest scope plus the reason (see :func:`normalize_scope` and
    :func:`retired_scope_note`).
    """

    scope: str
    call_id: str = ""
    tool: str = ""
    path: str = ""
    command_prefix: str = ""
    mcp_server: str = ""
    network_domain: str = ""
    side_effect_class: str = ""
    actor: str = "agent"

    def __post_init__(self) -> None:
        self.scope = normalize_scope(self.scope)
        self.call_id = str(self.call_id or "")
        self.tool = str(self.tool or "")
        self.path = str(self.path or "")
        self.command_prefix = str(self.command_prefix or "")
        self.mcp_server = str(self.mcp_server or "")
        self.network_domain = str(self.network_domain or "")
        self.side_effect_class = str(self.side_effect_class or "")
        # An empty prefix under a prefix scope would cover every command
        # (`"anything".startswith("")`). Refuse it as the configuration error it
        # is, rather than keeping a grant that silently means "yes to all".
        refusal = empty_prefix_refusal(self.command_prefix)
        if refusal and self.scope == SCOPE_SESSION_COMMAND_PREFIX:
            raise ValueError(refusal)

    def matches(self, call: ToolCall) -> bool:
        """Return whether this grant covers a call.

        Refuses in the refusal direction twice over: a retired scope is already
        normalized to ``once`` (which never covers anything), and a prefix grant
        with no prefix returns ``False`` instead of matching every command.
        """
        details = call_dimensions(call)
        if self.scope == SCOPE_ONCE:
            return False
        if self.scope == SCOPE_EXACT_CALL:
            return self.call_id == call.call_id
        if self.scope == SCOPE_SESSION_PATH:
            return self.tool == details["tool"] and _path_matches(
                details["path"], self.path
            )
        if self.scope == SCOPE_SESSION_COMMAND_PREFIX:
            if not self.command_prefix:
                return False
            return self.tool == details["tool"] and (
                _prefix_matches(details["command"], self.command_prefix)
                or _prefix_matches(details["command_prefix"], self.command_prefix)
            )
        return False


class PolicyEngine:
    """Evaluate typed calls and retain explicit approval scopes."""

    def __init__(
        self,
        rules: Optional[Iterable[PolicyRule | Mapping[str, Any]]] = None,
        *,
        session_id: str = "",
        actor: str = "agent",
        default_action: str = "auto",
        grants: Optional[Iterable[ApprovalGrant | Mapping[str, Any]]] = None,
        protected_paths: Optional[Iterable[str]] = None,
    ) -> None:
        self.session_id = str(session_id or "")
        self.actor = str(actor or "agent")
        self.default_action = str(default_action or "auto").lower()
        self.protected_paths = tuple(
            str(item).replace("\\", "/").lower() for item in (protected_paths or ())
        )
        if isinstance(rules, Mapping):
            if "action" in rules:
                rule_values = [rules]
            else:
                expanded: List[PolicyRule | Mapping[str, Any]] = []
                for tool, value in rules.items():
                    if isinstance(value, Mapping):
                        expanded.append({"tool": tool, **dict(value)})
                    else:
                        expanded.append({"tool": tool, "action": value})
                rule_values = expanded
        else:
            rule_values = rules or []
        self.rules = [PolicyRule.from_value(rule) for rule in rule_values]
        self.grants: List[ApprovalGrant] = [
            self._grant_from_value(grant) for grant in (grants or [])
        ]
        self.decisions: List[PermissionDecision] = []

    @property
    def audit_log(self) -> List[PermissionDecision]:
        """Return all decisions made by this engine."""
        return list(self.decisions)

    def evaluate(
        self,
        call: ToolCall | Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> PermissionDecision:
        """Evaluate a validated call, with deny taking precedence over asks."""
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        hard_reason = protected_call_reason(normalized, self.protected_paths)
        matches = [
            rule
            for rule in self.rules
            if rule.matches(normalized, {**(context or {}), "actor": self.actor})
        ]
        if hard_reason:
            action = "deny"
            matched_rule = "hard_deny_protected_path"
            scope = "once"
        elif matches:
            action_priority = {"deny": 3, "ask": 2, "allow": 1}
            selected = sorted(
                matches,
                key=lambda rule: (
                    action_priority.get(rule.action, 0),
                    rule.specificity(),
                ),
                reverse=True,
            )[0]
            action = selected.action
            matched_rule = selected.name or _rule_label(selected)
            scope = selected.scope
        else:
            action = self._default_for(normalized)
            matched_rule = "default"
            scope = "once"
        if action != "deny":
            grant = self._covering_grant(normalized)
            if grant is not None:
                action = "allow"
                matched_rule = matched_rule + "+approval"
                scope = grant.scope
        decision = PermissionDecision(
            matched_rule=matched_rule,
            action=action,
            scope=scope,
            actor=self.actor,
            call_id=normalized.call_id,
            exact_effect=exact_effect(normalized),
            reason=hard_reason or ("matched policy" if matches else "default policy"),
        )
        self.decisions.append(decision)
        return decision

    decide = evaluate

    def evaluate_axes(
        self,
        call: ToolCall | Mapping[str, Any],
        *,
        containment: Optional[Mapping[str, Any]] = None,
        containment_known: bool = True,
        context: Optional[Mapping[str, Any]] = None,
    ) -> "SafetyReceipt":
        """Evaluate a call and report BOTH axes, never one in place of the other.

        The decision is exactly what :meth:`evaluate` would have returned --
        this method delegates to it, so nothing about the existing policy
        changes. What it adds is the containment half, read from the
        sandbox's own receipt mapping
        (``execution.sandbox.containment_receipt``) and never invented here.

        ``containment=None`` does NOT mean "not contained" and does not mean
        "networkless": it means the axis is UNKNOWN, and the receipt says so.
        A caller that genuinely has no sandbox in the path should use
        :meth:`SafetyReceipt.from_decision_only`, which is the same thing with
        the intent stated in the call.

        A hard deny stays a deny whatever the containment is. Containment is
        not a mitigation channel: a contained run cannot make a protected-path
        deny disappear, and a deny is still reported with the containment it
        would have run under.
        """
        decision = self.evaluate(call, context)
        axis = ContainmentAxis.from_receipt(
            containment, known=containment_known and containment is not None
        )
        return SafetyReceipt(containment=axis, decision=decision)

    def privileged_receipt(
        self,
        call: ToolCall | Mapping[str, Any],
        *,
        containment: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> "SafetyReceipt":
        """Return a receipt, and mark whether the call ALREADY needs a human.

        The flag is derived, never set by the caller: it is True exactly when
        the decision is ``ask`` (an ``allow`` from a covering grant is not a
        pending escalation, and a ``deny`` is not a question for anyone).
        This is the field the approver agent gates on, which is what makes
        "the approver only sees already-privileged actions" a structural
        property rather than a convention at the call site.
        """
        return self.evaluate_axes(call, containment=containment, context=context)

    def record_approval(
        self,
        call: ToolCall | Mapping[str, Any],
        decision: PermissionDecision,
        approved: bool,
        scope: Optional[str] = None,
    ) -> PermissionDecision:
        """Record an approval outcome and optionally install its grant.

        A grant that could not be safely installed produces a **deny**, not an
        exception: this is called from an approval prompt, so a caller must be
        able to keep running after a bad scope answer. The reason travels on the
        returned decision (and therefore into the audit log), naming whether the
        scope was retired (``global``), unknown, or whether the command prefix
        was empty -- a config slip must not read as a quiet allow.
        """
        normalized = call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
        requested_scope = scope or decision.scope or SCOPE_ONCE
        selected_scope = normalize_scope(requested_scope)
        refusal = ""
        narrowing = retired_scope_note(requested_scope)
        if approved and selected_scope == SCOPE_SESSION_COMMAND_PREFIX:
            # Only a COMMAND-prefix grant is a configuration error when the
            # command is empty. A path grant legitimately has none.
            refusal = empty_prefix_refusal(normalized.arguments.get("command") or "")
        if approved and not refusal and selected_scope != SCOPE_ONCE:
            # `once` is consumed at the decision site, so it is NOT retained. The
            # pre-existing code appended an `once` grant that could never match
            # anything, which made the grant list disagree with what was
            # actually remembered.
            self.grants.append(
                ApprovalGrant(
                    scope=selected_scope,
                    call_id=normalized.call_id,
                    tool=normalized.tool,
                    path=str(normalized.arguments.get("path") or normalized.target),
                    command_prefix=str(normalized.arguments.get("command") or ""),
                    mcp_server=str(normalized.arguments.get("server") or ""),
                    network_domain=call_dimensions(normalized)["network_domain"],
                    side_effect_class=normalized.side_effect_class,
                    actor=self.actor,
                )
            )
        reason = "approval accepted" if approved else "approval denied"
        if refusal:
            reason = refusal
        elif narrowing:
            # The operator's yes is honoured for THIS call and nothing is
            # retained. Say so in the audit row: "approval accepted" on its own
            # would hide that the scope they asked for does not exist.
            reason = f"approval accepted once only; {narrowing}"
        updated = PermissionDecision(
            matched_rule=decision.matched_rule,
            action="allow" if (approved and not refusal) else "deny",
            scope=selected_scope if (approved and not refusal) else decision.scope,
            actor=decision.actor,
            call_id=decision.call_id,
            exact_effect=decision.exact_effect,
            reason=reason,
        )
        self.decisions[-1:] = [updated]
        return updated

    def grant(
        self,
        call: ToolCall | Mapping[str, Any],
        decision: PermissionDecision,
        scope: str = "once",
    ) -> ApprovalGrant:
        """Install a grant for a call without invoking an approver."""
        self.record_approval(call, decision, True, scope)
        return self.grants[-1]

    def _default_for(self, call: ToolCall) -> str:
        if self.default_action in PERMISSION_ACTIONS:
            return self.default_action
        if call.side_effect_class == "read_only":
            return "allow"
        return "ask"

    def _covering_grant(self, call: ToolCall) -> Optional[ApprovalGrant]:
        for grant in reversed(self.grants):
            if grant.matches(call):
                return grant
        return None

    @staticmethod
    def _grant_from_value(value: ApprovalGrant | Mapping[str, Any]) -> ApprovalGrant:
        if isinstance(value, ApprovalGrant):
            return value
        data = dict(value or {})
        return ApprovalGrant(
            scope=data.get("scope", "once"),
            call_id=data.get("call_id", ""),
            tool=data.get("tool", ""),
            path=data.get("path", ""),
            command_prefix=data.get("command_prefix", data.get("command", "")),
            mcp_server=data.get("mcp_server", data.get("server", "")),
            network_domain=data.get("network_domain", data.get("domain", "")),
            side_effect_class=data.get("side_effect_class", ""),
            actor=data.get("actor", "agent"),
        )


def call_dimensions(call: ToolCall) -> Dict[str, str]:
    """Extract normalized path, command, server, and domain dimensions."""
    args = call.arguments or {}
    path = str(args.get("path") or call.target or "").replace("\\", "/")
    command = str(args.get("command") or "")
    try:
        command_prefix = shlex.split(command)[0] if command else ""
    except ValueError:
        command_prefix = command.split()[0] if command.split() else ""
    server = str(args.get("server") or "")
    url = str(args.get("url") or "")
    domain = ""
    if url:
        try:
            domain = (urlparse(url).hostname or "").lower()
        except ValueError:
            domain = ""
    return {
        "tool": call.tool,
        "path": path,
        "command": command,
        "command_prefix": command_prefix,
        "mcp_server": server,
        "network_domain": domain,
    }


def exact_effect(call: ToolCall) -> str:
    """Return a stable, reviewable description of a call's effect."""
    dimensions = call_dimensions(call)
    return json.dumps(
        {
            "tool": call.tool,
            "side_effect_class": call.side_effect_class,
            "path": dimensions["path"],
            "command": dimensions["command"],
            "mcp_server": dimensions["mcp_server"],
            "network_domain": dimensions["network_domain"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def normalize_scope(value: Any) -> str:
    """Normalize approval scope aliases onto the closed shared scope set.

    R2-15: ``global`` is no longer a scope. It is normalized to ``once`` (the
    narrowest thing that can still be an answer) rather than to something
    convenient, and :func:`retired_scope_note` -- re-exported here so a caller
    already holding this module can report WHY. An unconditional bypass is not a
    scope: it matched every call regardless of tool, effect, repository, or
    command, so one answer defeated the entire policy.

    The keyword aliases are kept because they are a caller convenience
    (``"path"`` -> ``session_path``) and are not a widening.
    """
    requested = str(value or SCOPE_ONCE).strip().lower().replace("-", "_")
    aliases = {
        "call": SCOPE_EXACT_CALL,
        "exact": SCOPE_EXACT_CALL,
        "exact_call_id": SCOPE_EXACT_CALL,
        "path": SCOPE_SESSION_PATH,
        "command": SCOPE_SESSION_COMMAND_PREFIX,
        "command_prefix": SCOPE_SESSION_COMMAND_PREFIX,
        "session_command": SCOPE_SESSION_COMMAND_PREFIX,
    }
    if requested in APPROVAL_SCOPES:
        return requested
    return aliases.get(requested, normalize_approval_scope(requested))


def _optional_lower(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_matches(value: str, pattern: str) -> bool:
    return fnmatch.fnmatch(
        str(value or "").replace("\\", "/"), str(pattern or "").replace("\\", "/")
    )


def _prefix_matches(value: str, prefix: str) -> bool:
    """Return whether a command is covered by an approved prefix, with a boundary.

    Delegates to ``shared.approval.command_prefix_matches`` -- the ONE matcher in
    the tree. The previous local implementation was a bare ``str.startswith``,
    so a grant for ``git sta`` also covered ``git stash`` and an empty prefix
    covered every command. Both were live on this path.
    """
    return command_prefix_matches(value, prefix)


def _domain_matches(value: str, pattern: str) -> bool:
    left = str(value or "").lower()
    right = str(pattern or "").lower()
    return left == right or left.endswith("." + right.lstrip("."))


def _rule_label(rule: PolicyRule) -> str:
    """Return a readable stable identifier for an unnamed rule."""
    fields = [
        rule.tool,
        rule.path,
        rule.command_prefix,
        rule.mcp_server,
        rule.network_domain,
        rule.side_effect_class,
    ]
    matched = [str(value) for value in fields if value]
    return "rule:" + ("|".join(matched) if matched else rule.action)


# ---------------------------------------------------------------------------
# AGT-07 axis (b) reporting beside axis (a)
# ---------------------------------------------------------------------------

#: Fields that belong to the DECISION half. Finding one of these inside a
#: containment receipt is the conflation this round exists to prevent, so it
#: is detected rather than rendered.
_DECISION_ONLY_KEYS = frozenset(
    {
        "action",
        "ask",
        "approve",
        "approved",
        "approval",
        "approver",
        "decision",
        "permission",
        "prompt",
        "prompted",
        "requires_human",
        "scope",
    }
)

#: Fields that belong to the CONTAINMENT half.
_CONTAINMENT_ONLY_KEYS = frozenset(
    {
        "cap_drop",
        "mem_limit",
        "mounts",
        "network_enabled",
        "pids_limit",
        "readonly_applied",
        "rootfs_readonly",
        "sandboxed",
        "writable_roots",
    }
)


@dataclass(frozen=True)
class ContainmentAxis:
    """Axis (a): what the sandbox permits, as reported by the sandbox.

    ``known`` is the load-bearing field. An axis nobody reported is NOT
    "unrestricted" and NOT "fine" -- it is unknown, and
    :attr:`contained` is False until a real receipt arrives. That is the
    whole reason this type exists instead of a bare ``sandboxed: bool``:
    ``False`` for an unreported axis and ``False`` for a reported
    unsandboxed run are different facts, and only one of them is a problem.
    """

    known: bool = False
    sandboxed: bool = False
    network_enabled: bool = False
    readonly_applied: tuple[str, ...] = ()
    writable_roots: tuple[str, ...] = ()
    detail: str = ""
    extra: Mapping[str, Any] = None  # type: ignore[assignment]

    @classmethod
    def unknown(cls) -> "ContainmentAxis":
        """Return the axis a caller has no sandbox receipt for."""
        return cls(known=False, detail="not reported by the sandbox")

    @classmethod
    def from_receipt(
        cls, receipt: Optional[Mapping[str, Any]], *, known: bool = True
    ) -> "ContainmentAxis":
        """Build the axis from a sandbox receipt, or an UNKNOWN axis.

        Assumes ``receipt`` is ``execution.sandbox.containment_receipt``
        output (or anything with the same keys). An absent receipt, an empty
        mapping, or ``known=False`` all produce the unknown axis -- a missing
        receipt must never be read as a permissive one, and must never be
        read as a safe one either.
        """
        if not known or not isinstance(receipt, Mapping) or not receipt:
            return cls.unknown()
        applied = tuple(str(item) for item in (receipt.get("readonly_applied") or ()))
        writable = tuple(str(item) for item in (receipt.get("writable_roots") or ()))
        known_fields = {
            key: value
            for key, value in receipt.items()
            if key not in ("axis", "notes", "detail")
        }
        return cls(
            known=True,
            sandboxed=bool(
                receipt.get("sandboxed", receipt.get("rootfs_readonly", True))
            ),
            network_enabled=bool(receipt.get("network_enabled")),
            readonly_applied=applied,
            writable_roots=writable,
            detail=str(receipt.get("network_declaration") or ""),
            extra=known_fields,
        )

    @property
    def contained(self) -> bool:
        """Return whether a CONTAINED run is confirmed. Unknown is False."""
        return bool(self.known and self.sandboxed)

    def to_dict(self) -> Dict[str, Any]:
        """Return the containment half of the receipt, and nothing else."""
        record: Dict[str, Any] = {
            "axis": CONTAINMENT_AXIS,
            "known": self.known,
            "sandboxed": self.sandboxed,
            "network_enabled": self.network_enabled,
            "readonly_applied": list(self.readonly_applied),
            "writable_roots": list(self.writable_roots),
            "detail": self.detail,
        }
        if isinstance(self.extra, Mapping):
            record.update({str(k): v for k, v in self.extra.items() if k not in record})
        return record

    def describe(self) -> str:
        """Return the one line describing this axis alone."""
        if not self.known:
            return "containment: UNKNOWN (not reported by the sandbox)"
        readonly = ", ".join(self.readonly_applied) or "none applied"
        writable = ", ".join(self.writable_roots) or "whole workspace"
        network = "ON (declared)" if self.network_enabled else "off"
        return (
            f"containment: network {network}; read-only inside the writable "
            f"root: {readonly}; writable roots: {writable}"
        )


@dataclass(frozen=True)
class SafetyReceipt:
    """Both axes, side by side, with the confusion check attached.

    ``requires_human`` is DERIVED from the decision and only from the
    decision; :attr:`ContainmentAxis.contained` is derived from the
    containment and only from the containment. There is no constructor
    argument that can make one answer the other's question, which is the
    property the whole round is for.
    """

    containment: ContainmentAxis
    decision: PermissionDecision

    @classmethod
    def from_decision_only(cls, decision: PermissionDecision) -> "SafetyReceipt":
        """Build a receipt from a decision ALONE, with the axis UNKNOWN.

        This is the only constructor that omits a containment receipt, and it
        is deliberately explicit. A caller that has no sandbox receipt must
        say so here rather than passing ``None`` somewhere and inheriting a
        permissive default.
        """
        return cls(containment=ContainmentAxis.unknown(), decision=decision)

    @property
    def requires_human(self) -> bool:
        """Return whether a human is being asked about this call.

        True only for an ``ask``: an ``allow`` (including one produced by a
        covering grant) is not a pending escalation, and a ``deny`` is not a
        question for anyone. Containment is not consulted, ever.
        """
        return str(getattr(self.decision, "action", "")).strip().lower() == "ask"

    @property
    def denied(self) -> bool:
        """Return whether the decision axis refuses the call outright."""
        return str(getattr(self.decision, "action", "")).strip().lower() == "deny"

    def axes_confused(self) -> List[str]:
        """Return the reasons this receipt merges the two axes (empty is good).

        Three checks, all mechanical:

        * a decision field inside the containment half;
        * a containment field inside the decision's effect;
        * a containment axis that claims ``sandboxed`` while being unknown.

        A non-empty result means this receipt must not be rendered or acted
        on as a safety claim. It is a value, not an exception, so a caller
        can log it and keep running.
        """
        problems: List[str] = []
        if not self.containment.known and getattr(self.containment, "sandboxed", False):
            problems.append("an unreported containment axis claims a sandboxed run")
        if isinstance(self.containment.extra, Mapping):
            leaked = sorted(
                str(key)
                for key in self.containment.extra
                if str(key).casefold() in _DECISION_ONLY_KEYS
            )
            if leaked:
                problems.append(
                    "the containment half carries decision fields: " + ", ".join(leaked)
                )
        effect = str(getattr(self.decision, "exact_effect", ""))
        if effect:
            try:
                payload = json.loads(effect)
            except ValueError:
                payload = {}
            if isinstance(payload, Mapping):
                leaked = sorted(
                    str(key)
                    for key in payload
                    if str(key).casefold() in _CONTAINMENT_ONLY_KEYS
                )
                if leaked:
                    problems.append(
                        "the decision half carries containment fields: "
                        + ", ".join(leaked)
                    )
        return problems

    def to_dict(self) -> Dict[str, Any]:
        """Return the two-axis receipt: the decision verbatim, the axis apart."""
        return {
            "axes": [CONTAINMENT_AXIS, DECISION_AXIS],
            "containment": self.containment.to_dict(),
            "decision": {
                "action": getattr(self.decision, "action", ""),
                "matched_rule": getattr(self.decision, "matched_rule", ""),
                "scope": getattr(self.decision, "scope", ""),
                "actor": getattr(self.decision, "actor", ""),
                "call_id": getattr(self.decision, "call_id", ""),
                "reason": getattr(self.decision, "reason", ""),
                "requires_human": self.requires_human,
            },
            "axes_confused": self.axes_confused(),
        }

    def summary(self) -> str:
        """Return the one quotable line naming both axes independently."""
        problems = self.axes_confused()
        line = describe_axes(
            containment=self.containment.to_dict() if self.containment.known else None,
            containment_known=self.containment.known,
            decision=str(getattr(self.decision, "action", "")),
        )
        if problems:
            return f"{line} [AXES CONFLATED: {'; '.join(problems)}]"
        return line

    def approver_prompt_context(self) -> Dict[str, Any]:
        """Return the containment facts an approver model may be shown.

        The approver judges an effect a human must already approve. It is
        told the containment so it can weigh it, and it is told ONLY this:
        containment is not a reason to approve, so nothing here could turn a
        contained action into an approved one on its own.
        """
        return {
            "sandboxed": self.containment.contained,
            "containment_known": self.containment.known,
            "network_enabled": self.containment.network_enabled,
            "readonly_applied": list(self.containment.readonly_applied),
            "writable_roots": list(self.containment.writable_roots),
            "requires_human": self.requires_human,
        }
