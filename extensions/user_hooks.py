"""Declarative, user-configurable lifecycle hooks (Neo Ceiling Prompt 12).

This is the *user-facing* hook layer. It is deliberately a different surface
from :mod:`extensions.hooks`, which is the in-process contract a Python plugin
registers callbacks against. Here an operator (or a repository) declares hooks
in JSON, and the harness runs them:

    <global config root>/hooks.json              tier "user"
    <repo>/.neo/hooks.json                      tier "project"
    <repo>/.neo/hooks.local.json                tier "local"

    {
      "schema_version": 1,
      "max_latency_s": 20,
      "hooks": {
        "PostToolUse": [
          {
            "id": "lint-after-edit",
            "matcher": {"tool": "edit|write", "path": "**/*.py"},
            "if": {"side_effect_class": "mutation"},
            "type": "command",
            "command": ["ruff", "check", "--quiet", "{path}"],
            "timeout_s": 10,
            "blocking": false
          }
        ],
        "Stop": [
          {
            "id": "gate-not-green",
            "type": "command",
            "command": ["./.neo/gate.sh"],
            "timeout_s": 60,
            "blocking": true
          }
        ]
      }
    }

Events (:class:`HookEvent`): ``SessionStart``, ``PreToolUse``,
``PostToolUse``, ``PostToolUseFailure``, ``Stop``, ``PreCompact``,
``SessionEnd``.

Matching syntax is the SAME permission-rule form the tool policy already uses
(:class:`harness.agent_kernel.policy.PolicyRule`): the dimension names are
``tool``, ``path``, ``command_prefix``, ``mcp_server``, ``network_domain``,
``side_effect_class``. A ``matcher`` and an ``if`` block are AND-combined, and
within one block every present dimension must match. ``*`` and a bare field
match everything, so a hook with no matcher fires for the event.

Handler types, in the order the ceiling prompt asks for them:

* ``command`` â€” a fixed argv (never a shell string), run with a scrubbed
  environment, a repo-pinned cwd, a per-hook timeout, and bounded stdout.
* ``http`` â€” a single bounded POST through :mod:`shared.egress`, so a hook
  cannot reach a host the operator did not allowlist.
* ``prompt`` â€” OPTIONAL and additive only. It renders bounded text into
  ``additional_context``. Without an injected ``prompt_renderer`` it is
  skipped and the skip is *recorded*, never silent.

The four properties this layer guarantees, each with a regression test:

1. **Ordering and merge are deterministic.** Registrations sort by
   ``(tier_rank, declared_order, id)``; the three tiers merge with explicit
   precedence ``local > project > user`` and a colliding ``id`` is replaced,
   not appended.
2. **Hook output is bounded, typed, and cannot silently mutate policy.** The
   only fields a hook can return are ``decision``, ``reason``,
   ``systemMessage``, ``additionalContext``, and ``suppressOutput``. Any
   policy-shaped key (``permission``, ``policy``, ``approval``, ``tools``,
   ``env``, ``config``) is dropped, and the drop is recorded as
   ``policy_mutation_refused``. Unparseable output degrades to ``continue``
   with a ``malformed_output`` diagnostic â€” a hook can never block or allow by
   accident.
3. **Latency is visible and budgeted.** Every hook records its own duration;
   a dispatch stops consuming budget when the per-dispatch total is spent and
   records the remainder as ``latency_budget_exhausted``.
4. **Failure is visible, bounded, and never silent.** A hook that raises,
   times out, or is missing is recorded on :attr:`HookOutcome.failures` and
   emitted to the unified trace. Observational events (``PostToolUse``,
   ``PostToolUseFailure``, ``PreCompact``) can add context and suppress
   output but can never change a decision; blocking events use restrictive
   precedence so a single ``block`` wins.

The completion gate (:class:`CompletionGate`) is the verifier integration: a
``Stop`` hook that blocks makes a run report ``completed_unverified``, never
``completed_verified``. It only ever *downgrades* a status â€” there is no path
in this module that mints a verified result.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse

from extensions import hook_events
from shared import security, tracing
from shared.approval import command_prefix_matches
from shared.egress import egress_decision

__all__ = [
    "COMPLETION",
    "FAIL_POLICIES",
    "FAIL_POLICY_VALUES",
    "HOOK_ACTIONS",
    "HOOK_EVENTS",
    "HOOK_FAIL_POLICIES",
    "HOOK_FAIL_POLICY_REASONS",
    "HOOK_LIFECYCLE_EVENTS",
    "HOOK_TIER_PRECEDENCE",
    "CompletionGate",
    "CompletionGateVerdict",
    "HookAction",
    "HookCommandResult",
    "HookConfig",
    "HookConfigError",
    "HookDecision",
    "HookEngine",
    "HookError",
    "HookEvent",
    "HookGate",
    "HookMatcher",
    "HookOutcome",
    "HookRecord",
    "HookRegistry",
    "HookReloadReport",
    "HookRewrite",
    "HookSpec",
    "HookSubject",
    "HookTrust",
    "PostEditGate",
    "PostEditGateReport",
    "hook_config_tier_paths",
    "hook_trust_path",
    "hooks_command",
    "load_hook_config",
    "load_hook_trust",
    "plain_hook_sentence",
    "record_hook_trust",
]

# --------------------------------------------------------------------------
# Bounds. Every value a hook can influence is capped here, once.
# --------------------------------------------------------------------------
_MAX_SPECS = 64
_MAX_OUTPUT_CHARS = 8_192
_MAX_REASON_CHARS = 512
_MAX_CONTEXT_CHARS = 2_048
_MAX_ARGV = 32
_MAX_ARGV_ITEM = 4_096
_MAX_CONFIG_BYTES = 256_000
_DEFAULT_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 300.0
_DEFAULT_DISPATCH_BUDGET_S = 20.0
_MAX_DISPATCH_BUDGET_S = 600.0
_MAX_RECORDS = 512
_RECORD_HISTORY = 256

# A hook may return exactly these keys. Everything else is either ignored
# (unknown advisory keys) or refused (policy-shaped keys), and both are
# recorded.
_DECISION_KEYS = ("decision", "continue", "block", "ask", "reason", "stopReason")
#: The keys that can carry a DECISION inside a JSON object. ``reason`` and
#: ``stopReason`` are excluded, and that exclusion is a fix rather than a
#: preference: they were in the list above because the bare-STRING output form
#: ("block") has to be readable, and in a mapping they were then read as the
#: decision — so the most natural gate output in the world,
#: ``{"reason": "never push from this repo"}``, raised
#: ``unsupported hook decision: 'never push from this repo'`` and was rejected
#: as unusable. Found by writing ``/hooks test`` against a real gate, not by
#: reading the code. A reason alone now always yields ``continue``, which is
#: also the safe direction: a hook must not block by accident.
_DECISION_ONLY_KEYS = ("decision", "continue", "block", "ask")
_REASON_KEYS = ("reason", "stopReason")
_CONTEXT_KEYS = (
    "systemMessage",
    "system_message",
    "additionalContext",
    "additional_context",
)
_FLAG_KEYS = ("suppressOutput", "suppress_output")

# The single key a hook uses to REWRITE the call it is gating. There is one
# spelling per field rather than a family of aliases so the accepted surface is
# a table a reader can hold in their head; both the camel and snake forms of the
# KEY itself are accepted because hooks are written by two audiences.
_REWRITE_KEY = "updatedInput"

#: The only call fields a rewrite may touch, and the ONLY thing a rewrite can do
#: is NARROW them. Each entry is ``(field, kind)``:
#:
#: * ``command_prefix`` â€” replaced with a prefix that must itself be a prefix of
#:   the original, checked through :func:`shared.approval.command_prefix_matches`
#:   (the ONE prefix matcher in the tree), so a rewrite can only ever narrow the
#:   set of commands a call is allowed to reach.
#: * ``path`` â€” replaced with a path INSIDE the original path's directory tree,
#:   so a rewrite can only ever narrow where a call may write.
#:
#: There is deliberately no key that adds capability: a rewrite removes a
#: command's reach, and it cannot add one. `HookRewrite` has no field for
#: anything else, so a hook asking for it is refused BY NAME rather than
#: partially honoured.
_REWRITE_FIELDS: dict[str, str] = {
    "command_prefix": "narrowing",
    "path": "narrowing",
}
_REWRITE_CAPABILITIES: tuple[str, ...] = tuple(_REWRITE_FIELDS)

_POLICY_KEYS = (
    "permission",
    "permissions",
    "policy",
    "permission_rules",
    "approval",
    "approvals",
    "allow",
    "allow_tools",
    "allowed_tools",
    "tools",
    "visible_tools",
    "env",
    "environment",
    "config",
    "settings",
    "sandbox",
    "egress",
    "model",
    "provider",
)


class HookError(Exception):
    """Base class for user-hook configuration and dispatch failures."""

    def __str__(self) -> str:
        """Return a redacted, bounded public message."""
        return security.redact_text(super().__str__())[:_MAX_REASON_CHARS]


class HookConfigError(HookError, ValueError):
    """Raised when a hook configuration file is unusable as written."""


class HookAction(str, Enum):
    """The only three things a hook may say about an event."""

    CONTINUE = "continue"
    BLOCK = "block"
    ASK = "ask"

    @classmethod
    def _missing_(cls, value: object) -> "HookAction":
        """Normalize common spellings; an unknown word fails closed to continue."""
        raw = str(value or "").strip().casefold()
        if raw in {"allow", "approved", "ok", "proceed", "true", "1"}:
            return cls.CONTINUE
        if raw in {"deny", "denied", "reject", "rejected", "false", "0"}:
            return cls.BLOCK
        if raw in {"confirm", "approval", "prompt", "question"}:
            return cls.ASK
        if raw in {"", "continue", "allow", "block", "ask"}:
            return cls(raw) if raw else cls.CONTINUE
        raise ValueError(f"unsupported hook decision: {value!r}")

    def __str__(self) -> str:
        """Return the stable serialized action value."""
        return self.value

    @property
    def rank(self) -> int:
        """Return the restrictive precedence rank (higher is more restrictive)."""
        return {HookAction.CONTINUE: 0, HookAction.ASK: 1, HookAction.BLOCK: 2}[self]


HOOK_ACTIONS: tuple[str, ...] = tuple(action.value for action in HookAction)


#: Every lifecycle event the hook layer can fire, as a ``str`` enum.
#:
#: The members are DERIVED from
#: :data:`extensions.hook_events.LIFECYCLE_EVENTS` rather than listed, because a
#: class body cannot synthesise members from a tuple it imports and, more
#: importantly, a restated list is a second place to forget an event. Deriving
#: them means an event that is not in the vocabulary cannot be registered, fired,
#: or listed, so the dispatcher and the vocabulary a plugin author targets cannot
#: drift.
#:
#: The behaviour is attached below rather than declared in a body: the functional
#: Enum API builds the class from the mapping alone, and these are genuinely
#: derived from the same authority the members come from.
#: ``SCREAMING_SNAKE`` is derived rather than hand-written so a new event name
#: cannot acquire a member whose attribute spelling somebody had to guess.
def _event_member_name(event: str) -> str:
    """Return the ``SCREAMING_SNAKE`` attribute spelling for one event name.

    Derived rather than hand-written so a new event name cannot acquire a member
    whose attribute spelling somebody had to guess, and so the eleven
    attribute names cannot drift from the eleven vocabulary names.
    """
    words: list[str] = []
    for index, character in enumerate(event):
        if character == "_":
            continue
        if character.isupper() and index:
            words.append("_")
        words.append(character.upper())
    return "".join(words)


_EVENT_MEMBER_NAMES = {
    _event_member_name(name): name for name in hook_events.LIFECYCLE_EVENTS
}

HookEvent = Enum(
    "HookEvent",
    _EVENT_MEMBER_NAMES,
    type=str,
    module=__name__,
    qualname="HookEvent",
)
HookEvent.__doc__ = (
    "Every lifecycle event the hook layer can fire.\n\n"
    "The members are DERIVED from "
    ":data:`extensions.hook_events.LIFECYCLE_EVENTS`, so an event outside that "
    "vocabulary cannot be registered, fired, or listed. "
    '``HookEvent("pre_tool_use")`` accepts the spellings people actually type, '
    "and every member answers ``observational`` and ``event_class`` from the "
    "declared class rather than from a second table."
)


def _hook_event_missing(cls, value: object):
    """Accept every spelling :func:`hook_events.normalize_event` accepts."""
    return cls(hook_events.normalize_event(value))


def _hook_event_str(self) -> str:
    """Return the stable serialized event name."""
    return str(self.value)


def _hook_event_observational(self) -> bool:
    """Return whether this event can never change a decision."""
    return hook_events.is_observational(self.value)


def _hook_event_class(self) -> str:
    """Return the declared class (``gating``/``observational``/``lifecycle``)."""
    return hook_events.event_class(self.value)


HookEvent._missing_ = classmethod(_hook_event_missing)  # type: ignore[attr-defined]
HookEvent.__str__ = _hook_event_str  # type: ignore[method-assign]
HookEvent.observational = property(  # type: ignore[attr-defined]
    _hook_event_observational
)
HookEvent.event_class = property(_hook_event_class)  # type: ignore[attr-defined]


_OBSERVATIONAL_EVENTS = frozenset(hook_events.OBSERVATIONAL_EVENTS)

#: The FULL lifecycle vocabulary (eleven events), the surface a plugin author
#: targets.
HOOK_LIFECYCLE_EVENTS: tuple[str, ...] = hook_events.LIFECYCLE_EVENTS

#: The SEVEN events the original product vocabulary declared. Kept exactly,
#: and derived rather than restated, because shipped code, the ``neo hooks``
#: JSON document and several existing suites read this name and expect these
#: seven. New events are reached through :data:`HOOK_LIFECYCLE_EVENTS`.
HOOK_EVENTS: tuple[str, ...] = hook_events.LEGACY_EVENTS

COMPLETION: HookEvent = HookEvent.STOP

HOOK_TIER_PRECEDENCE: tuple[str, ...] = ("user", "project", "local", "plugin")
_TIER_RANK = {name: index for index, name in enumerate(HOOK_TIER_PRECEDENCE)}

#: The tier a plugin's own registrations live in. It is APPENDED to the
#: precedence tuple rather than inserted, so the three shipped tiers keep the
#: exact relative order and ranks they have always had, and a plugin hook runs
#: after every operator-declared hook on the same event.
PLUGIN_TIER = "plugin"

_MATCHER_DIMENSIONS = (
    "tool",
    "path",
    "command_prefix",
    "mcp_server",
    "network_domain",
    "side_effect_class",
)

# --------------------------------------------------------------------------
# Fail policy: what happens when a hook for a GATING event cannot be run.
#
# A hook that raises, times out, is missing its executable, or returns
# unusable output has NOT approved anything. For an event whose whole purpose
# is to gate, treating that silence as consent is the one failure mode a hook
# layer must not have, so those events are FAIL-CLOSED. For an event that can
# only add context or suppress output, failing closed would mean a broken
# logger stops a run for no security benefit, so those are FAIL-OPEN.
#
# The AUTHORITY is `extensions.hook_events`: an event is given a CLASS and the
# class decides, so a new event cannot inherit a policy by accident â€” it has
# to be given a class, and the class has to be one of the three declared.
# The two tables below are PROJECTIONS of that authority over the seven events
# the original vocabulary declared, because shipped code and several existing
# suites read these exact names. They are derived, never restated: a change in
# the class map changes them, and a drift between them is unreachable rather
# than merely discouraged.
#
# Every value is reported on the gate receipt (`fail_policy`,
# `fail_policy_source`) and in `HookConfig.to_dict()`, so a reader can always
# tell which policy applied and why. An operator can override per event (a
# `fail_policy` table in any hook tier) or per hook (`"fail_policy": "..."` on
# one registration); an override is recorded as such and is never silent.
# --------------------------------------------------------------------------

FAIL_POLICY_VALUES: tuple[str, ...] = hook_events.FAIL_POLICY_VALUES
FAIL_POLICIES: dict[str, str] = {
    "fail_closed": "an unusable gate refuses",
    "fail_open": "an unusable gate is recorded and does not refuse",
}

HOOK_FAIL_POLICIES: dict[str, str] = hook_events.fail_policy_projection(
    hook_events.LEGACY_EVENTS
)

HOOK_FAIL_POLICY_REASONS: dict[str, str] = {
    name: hook_events.fail_policy_reason(name) for name in hook_events.LEGACY_EVENTS
}


def _normalize_command_prefix(value: Any) -> str:
    """Return a command prefix in one normalized form for comparison.

    Only collapses surrounding and internal whitespace. It deliberately does NOT
    tokenize: the ONE prefix matcher in this tree is
    :func:`shared.approval.command_prefix_matches`, and this helper exists only
    so a rewrite's candidate and the subject's original are compared as the same
    kind of string rather than as ``"git  status"`` versus ``"git status"``.
    """
    return " ".join(_text(value, 512).split())


def _narrowed_path(original: str, candidate: str) -> Optional[str]:
    """Return ``candidate`` when it stays inside ``original``'s tree, else None.

    The check is containment on NORMALIZED separators, because a rewrite that
    reached outside the original path's tree would be a WIDENING wearing a
    rewrite's syntax. A path with no original is refused outright: there is
    nothing to narrow, so any target would be a widening.
    """
    base = str(original or "").replace("\\", "/").strip().rstrip("/")
    target = str(candidate or "").replace("\\", "/").strip()
    if not base or not target:
        return None
    if base == target:
        return target
    if target.startswith(base + "/"):
        return target
    return None


def _fail_policy_value(value: Any, *, origin: str) -> str:
    """Validate one fail-policy spelling; return the closed-vocabulary value."""
    text = _text(value, 40).strip().casefold().replace("-", "_")
    if text in ("closed", "close", "deny", "strict", "true", "1"):
        return "fail_closed"
    if text in ("open", "allow", "lenient", "false", "0"):
        return "fail_open"
    if text in FAIL_POLICY_VALUES:
        return text
    raise HookConfigError(
        f"{origin}: fail_policy {value!r} is not one of {FAIL_POLICY_VALUES}"
    )


def _text(value: Any, limit: int = _MAX_REASON_CHARS) -> str:
    """Return a redacted, bounded string for any value."""
    try:
        raw = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - pathological __str__
        raw = ""
    return security.redact_text(raw)[:limit]


def _subject_field(value: Any, limit: int) -> str:
    """Return one subject field as text, and an ABSENT field as ``""``.

    Not ``_text``: ``str(None)`` is the four-character string ``"None"``, so an
    absent field used to arrive as the literal word ``None`` in every receipt and
    a matcher written against it would match a call that declared nothing at all.
    Measured on a real ``/hooks test`` receipt, which is where it showed up.
    """
    if value is None:
        return ""
    return _text(value, limit)


def _bounded_json(value: Any) -> str:
    """Return a redacted, size-bounded JSON rendering of a value."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return '"[unserializable]"'
    return security.redact_text(text)[:_MAX_OUTPUT_CHARS]


@dataclass(frozen=True)
class HookMatcher:
    """A permission-rule-form condition over one hook subject.

    The dimension names and their AND-within-block semantics are deliberately
    the same as :class:`harness.agent_kernel.policy.PolicyRule` so an operator
    who has already written permission rules for a tool writes a hook matcher
    in the same vocabulary. ``None``/empty means "this dimension is not
    constrained"; ``"*"`` is the explicit form of the same thing.
    """

    tool: Optional[str] = None
    path: Optional[str] = None
    command_prefix: Optional[str] = None
    mcp_server: Optional[str] = None
    network_domain: Optional[str] = None
    side_effect_class: Optional[str] = None

    _DIMENSIONS = _MATCHER_DIMENSIONS

    @classmethod
    def from_value(cls, value: Any) -> "HookMatcher":
        """Build a matcher from a mapping (or a list of such mappings)."""
        if value is None:
            return cls()
        if isinstance(value, HookMatcher):
            return value
        if isinstance(value, (list, tuple)):
            merged: dict[str, Any] = {}
            for item in value:
                merged.update(cls.from_value(item).as_dict())
            return cls.from_value(merged)
        if not isinstance(value, Mapping):
            raise HookConfigError(
                f"matcher must be a mapping, got {type(value).__name__}"
            )
        unknown = sorted(
            str(key) for key in value if str(key) not in _MATCHER_DIMENSIONS
        )
        if unknown:
            raise HookConfigError(
                "unsupported matcher dimension(s): " + ", ".join(unknown) + "; "
                f"expected any of {', '.join(_MATCHER_DIMENSIONS)}"
            )
        fields: dict[str, Optional[str]] = {}
        for name in _MATCHER_DIMENSIONS:
            raw = value.get(name)
            if raw is None:
                fields[name] = None
                continue
            rendered = _text(raw, 400).strip()
            fields[name] = None if rendered in ("", "*") else rendered
        return cls(**fields)

    def as_dict(self) -> dict[str, Any]:
        """Return the non-empty dimensions as a JSON-compatible mapping."""
        return {
            name: getattr(self, name)
            for name in _MATCHER_DIMENSIONS
            if getattr(self, name)
        }

    @property
    def constrained(self) -> bool:
        """Return whether this matcher constrains at least one dimension."""
        return bool(self.as_dict())

    def matches(self, subject: "HookSubject") -> bool:
        """Return whether every constrained dimension matches ``subject``."""
        if self.tool and not _alternatives(self.tool, subject.tool):
            return False
        if self.path and not _path_matches(subject.path, self.path):
            return False
        if self.command_prefix and not _alternatives(
            self.command_prefix, subject.command or subject.command_prefix
        ):
            return False
        if self.mcp_server and not _alternatives(self.mcp_server, subject.mcp_server):
            return False
        if self.network_domain and not _alternatives(
            self.network_domain, subject.network_domain
        ):
            return False
        return not (
            self.side_effect_class
            and not _alternatives(self.side_effect_class, subject.side_effect_class)
        )

    def describe(self) -> str:
        """Return a short human-readable rendering of the constrained dimensions."""
        parts = [
            f"{name}={getattr(self, name)}"
            for name in _MATCHER_DIMENSIONS
            if getattr(self, name)
        ]
        return ", ".join(parts) if parts else "(any)"


def _alternatives(pattern: str, value: str) -> bool:
    """Return whether ``value`` matches a ``|``-separated pattern list."""
    options = [item.strip() for item in str(pattern or "").split("|") if item.strip()]
    if not options:
        return True
    candidate = str(value or "").strip().casefold()
    if not candidate:
        return False
    for option in options:
        token = option.casefold()
        if candidate == token:
            return True
        if fnmatch.fnmatch(candidate, token):
            return True
    return False


def _path_matches(value: str, pattern: str) -> bool:
    """Return whether a subject path satisfies a hook path pattern."""
    candidate = str(value or "").replace("\\", "/").strip()
    if not candidate:
        return False
    options = [
        item.strip().replace("\\", "/")
        for item in str(pattern or "").split("|")
        if item.strip()
    ]
    if not options:
        return True
    for option in options:
        if fnmatch.fnmatch(candidate, option):
            return True
        # A directory-style pattern also covers everything beneath it.
        if not option.endswith("*") and candidate.startswith(option.rstrip("/") + "/"):
            return True
    return False


@dataclass(frozen=True)
class HookSubject:
    """The bounded, redacted facts one dispatch exposes to its hooks.

    A subject is a projection, never a live object: it carries only the
    dimensions the matcher vocabulary knows plus a small bounded metadata
    mapping. It cannot carry a ``ToolCall`` and therefore cannot be mutated by
    a hook into something the policy would then honour.
    """

    tool: str = ""
    path: str = ""
    command: str = ""
    command_prefix: str = ""
    mcp_server: str = ""
    network_domain: str = ""
    side_effect_class: str = ""
    task_id: str = ""
    session_id: str = ""
    status: str = ""
    result_summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "HookSubject":
        """Build a subject from a mapping, an existing subject, or ``None``."""
        if isinstance(value, HookSubject):
            return value
        data = dict(value or {}) if isinstance(value, Mapping) else {}
        known = {
            "tool",
            "path",
            "command",
            "command_prefix",
            "mcp_server",
            "network_domain",
            "side_effect_class",
            "task_id",
            "session_id",
            "status",
            "result_summary",
        }
        extra = {
            str(key): security.redact_secrets(item)
            for key, item in data.items()
            if str(key) not in known
        }
        return cls(
            tool=_subject_field(data.get("tool"), 200),
            path=_subject_field(data.get("path"), 512),
            command=_subject_field(data.get("command"), 1_024),
            command_prefix=_subject_field(data.get("command_prefix"), 512),
            mcp_server=_subject_field(data.get("mcp_server"), 200),
            network_domain=_subject_field(data.get("network_domain"), 200),
            side_effect_class=_subject_field(data.get("side_effect_class"), 100),
            task_id=_subject_field(data.get("task_id"), 200),
            session_id=_subject_field(data.get("session_id"), 200),
            status=_subject_field(data.get("status"), 100),
            result_summary=_subject_field(data.get("result_summary"), 1_024),
            metadata=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible projection sent to a command/http hook."""
        return {
            "tool": self.tool,
            "path": self.path,
            "command": self.command,
            "mcp_server": self.mcp_server,
            "network_domain": self.network_domain,
            "side_effect_class": self.side_effect_class,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "result_summary": self.result_summary,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class HookDecision:
    """A hook's typed, bounded answer.

    There is deliberately no field here that could express a policy change: a
    hook can continue, block, or ask, explain itself, contribute bounded
    context, and ask for its own output to be suppressed. Nothing else.
    """

    action: HookAction = HookAction.CONTINUE
    reason: str = ""
    system_message: str = ""
    additional_context: str = ""
    suppress_output: bool = False
    refused_policy_keys: tuple[str, ...] = ()
    # A ``PreToolUse`` hook may REWRITE the call it is gating. Only a rewriting
    # decision carries one, and :class:`HookRewrite` is structurally incapable of
    # expressing a grant: its two fields can only NARROW the call, and a candidate
    # that would widen it is refused by name in ``refused_rewrite_keys`` rather
    # than partially honoured.
    rewrite: Optional["HookRewrite"] = None
    # Keys a hook tried to rewrite that this layer refuses. Recorded so a silent
    # refusal is impossible, and surfaced by ``/hooks test`` so a plugin author
    # learns the accepted surface.
    refused_rewrite_keys: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether this decision blocks the event."""
        return self.action is HookAction.BLOCK

    @property
    def needs_approval(self) -> bool:
        """Whether this decision defers to the operator."""
        return self.action is HookAction.ASK

    @classmethod
    def from_payload(
        cls, payload: Any, *, subject: Optional["HookSubject"] = None
    ) -> "HookDecision":
        """Build a decision from a hook's parsed output.

        Policy-shaped keys are dropped and named in ``refused_policy_keys``
        instead of being applied, so a hook that tries to widen its own
        authority produces a visible refusal rather than a silent grant.

        ``subject`` is REQUIRED for a rewrite to be honoured, because a rewrite
        can only be checked for narrowing against the call it narrows. Without
        it every rewrite candidate is refused by name — the fail-closed
        direction, and the one that keeps this method usable from a place that
        genuinely has no subject.
        """
        if isinstance(payload, HookDecision):
            return payload
        if payload is None:
            return cls()
        if isinstance(payload, str):
            return cls(
                action=HookAction(payload), reason=_text(payload, _MAX_REASON_CHARS)
            )
        if not isinstance(payload, Mapping):
            raise HookConfigError(
                f"hook output must be an object, got {type(payload).__name__}"
            )
        data = dict(payload)
        refused = tuple(
            sorted(
                str(key) for key in data if str(key).strip().casefold() in _POLICY_KEYS
            )
        )
        raw_decision: Any = None
        for key in _DECISION_ONLY_KEYS:
            if key in data and data[key] not in (None, ""):
                raw_decision = data[key]
                break
        action = (
            HookAction(raw_decision)
            if raw_decision is not None
            else HookAction.CONTINUE
        )
        reason = ""
        for key in _REASON_KEYS:
            value = data.get(key)
            if (
                isinstance(value, str)
                and value.strip()
                and key in ("reason", "stopReason")
            ):
                reason = _text(value, _MAX_REASON_CHARS)
                break
        system_message = ""
        context = ""
        for key in _CONTEXT_KEYS:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if key in ("systemMessage", "system_message"):
                system_message = _text(value, 1_000)
            else:
                context = _text(value, _MAX_CONTEXT_CHARS)
                break
        suppress = False
        for key in _FLAG_KEYS:
            if key in data:
                suppress = bool(data[key])
                break
        rewrite: Optional[HookRewrite] = None
        refused_rewrite: tuple[str, ...] = ()
        if _REWRITE_KEY in data or "rewrite" in data:
            rewrite, refused_rewrite = HookRewrite.from_payload(
                data.get(_REWRITE_KEY, data.get("rewrite")),
                subject=subject,
                policy_keys=refused,
            )
        return cls(
            action=action,
            reason=reason,
            system_message=system_message,
            additional_context=context,
            suppress_output=suppress,
            refused_policy_keys=refused,
            rewrite=rewrite,
            refused_rewrite_keys=refused_rewrite,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record for a receipt or trace row."""
        return {
            "action": self.action.value,
            "reason": self.reason,
            "system_message": self.system_message,
            "additional_context_chars": len(self.additional_context),
            "suppress_output": self.suppress_output,
            "refused_policy_keys": list(self.refused_policy_keys),
            "rewrite": self.rewrite.to_dict() if self.rewrite else None,
            "refused_rewrite_keys": list(self.refused_rewrite_keys),
        }


@dataclass(frozen=True)
class HookRewrite:
    """A ``PreToolUse`` hook's NARROWING of the call it is gating.

    A hook can block a call outright, or it can make the call narrower than the
    model asked for: pin the command to a shorter prefix, or confine the write
    to a subdirectory. Both are real needs â€” "never run `git push` from this
    repo" and "never write above ``src/``" are not expressible as a refusal â€”
    and neither is expressible as an arbitrary edit of the call.

    **The class is structurally narrowing-only.** It has exactly two fields,
    both of which are checked against the original subject before they are
    accepted:

    * ``command_prefix`` â€” accepted only when
      :func:`shared.approval.command_prefix_matches` says the ORIGINAL command
      is still covered by the NEW prefix, i.e. the new prefix is a real prefix
      of the original. That is the same matcher the policy engine and the CLI
      use, so "what a rewrite may narrow to" and "what a grant may cover" can
      never disagree.
    * ``path`` â€” accepted only when the new path is the original path or lies
      inside its tree.

    A candidate that would WIDEN either is refused and NAMED in
    :attr:`refused`. There is no field that could add a capability, so a hook
    asking for one cannot express it, and :meth:`from_payload` reports the
    attempt instead of silently dropping it.
    """

    command_prefix: str = ""
    path: str = ""
    #: Refusal reasons, one per rejected candidate.
    refused: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything was actually narrowed."""
        return bool(self.command_prefix or self.path)

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        subject: Optional["HookSubject"] = None,
        policy_keys: tuple[str, ...] = (),
    ) -> tuple[Optional["HookRewrite"], tuple[str, ...]]:
        """Build a rewrite from a hook's output.

        Returns ``(rewrite, refused)``. A ``None`` rewrite with a non-empty
        ``refused`` means the hook asked for something this layer will not do,
        and the refusal is reported rather than silently ignored.

        Assumes ``payload`` is the hook's ``updatedInput`` object (or ``None``).
        A payload that is not an object, or a key outside
        :data:`_REWRITE_FIELDS`, is refused by name â€” including any key already
        refused as policy-shaped, so a key is never both refused and applied.
        """
        already = {str(key) for key in policy_keys}
        if payload is None:
            return None, ()
        if not isinstance(payload, Mapping):
            return None, ("updatedInput must be an object of narrowable fields",)
        refused: list[str] = []
        prefix = ""
        if "command_prefix" in payload:
            candidate = _normalize_command_prefix(payload.get("command_prefix"))
            if not candidate:
                refused.append(
                    "command_prefix: a rewrite may not empty it; a rewrite may "
                    "only narrow"
                )
            elif subject is None:
                refused.append(
                    "command_prefix: cannot verify this rewrite without the "
                    "subject it narrows"
                )
            else:
                original = _normalize_command_prefix(
                    subject.command_prefix or subject.command
                )
                if not original:
                    # The subject declared no prefix, so there is no narrower
                    # one to compute: naming a prefix here would be asserting
                    # the call is one this hook invented.
                    refused.append(
                        "command_prefix: the subject declares no command "
                        "prefix, so a rewrite has nothing to narrow"
                    )
                elif command_prefix_matches(original, candidate):
                    prefix = candidate
                elif command_prefix_matches(candidate, original):
                    # The candidate is BROADER than what the call was. This is
                    # the widening case, and it is the whole reason this class
                    # exists, so it says so in plain words.
                    refused.append(
                        f"command_prefix: {candidate!r} is broader than the "
                        f"call's own {original!r}; a rewrite may only narrow"
                    )
                else:
                    refused.append(
                        f"command_prefix: {candidate!r} is not a prefix of the "
                        f"call's own {original!r}; a rewrite may only narrow"
                    )
        new_path = ""
        if "path" in payload:
            candidate = _text(payload.get("path"), 512).strip()
            if subject is None:
                refused.append(
                    "path: cannot verify this rewrite without the subject it narrows"
                )
            else:
                narrowed = _narrowed_path(subject.path, candidate)
                if narrowed is None:
                    refused.append(
                        f"path: {candidate!r} is outside the call's own "
                        f"{subject.path!r}; a rewrite may only narrow"
                    )
                else:
                    new_path = narrowed
        unknown = sorted(
            str(key)
            for key in payload
            if str(key) not in _REWRITE_FIELDS and str(key) not in already
        )
        for key in unknown:
            refused.append(
                f"{key}: not a narrowable field; a rewrite may only set "
                + ", ".join(_REWRITE_CAPABILITIES)
            )
        if refused:
            # A refused candidate must never be PARTIALLY applied. If every
            # requested field was refused there is no rewrite; if some survived,
            # the surviving ones are applied and the refusals ride the receipt.
            rewrite = cls(command_prefix=prefix, path=new_path, refused=tuple(refused))
            return (rewrite if rewrite.changed else None), tuple(refused)
        return cls(command_prefix=prefix, path=new_path), ()

    def apply(self, subject: "HookSubject") -> "HookSubject":
        """Return the subject as it will ACTUALLY run.

        This is the object the caller must dispatch. Recording the rewritten
        subject is what makes "the transcript shows what actually ran" true
        rather than aspirational: the caller holds this, not the original.
        """
        if not self.changed:
            return subject
        return replace(
            subject,
            command_prefix=self.command_prefix or subject.command_prefix,
            path=self.path or subject.path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt for a trace row."""
        return {
            "command_prefix": self.command_prefix,
            "path": self.path,
            "refused": list(self.refused),
        }


@dataclass(frozen=True)
class HookSpec:
    """One merged, validated hook registration."""

    id: str
    event: HookEvent
    kind: str
    tier: str = "user"
    matcher: HookMatcher = field(default_factory=HookMatcher)
    condition: HookMatcher = field(default_factory=HookMatcher)
    command: tuple[str, ...] = ()
    url: str = ""
    prompt_text: str = ""
    timeout_s: float = _DEFAULT_TIMEOUT_S
    blocking: bool = True
    order: int = 0
    # Empty means "use the documented table for this event"; a non-empty value
    # is an explicit per-hook override and is reported as one.
    fail_policy: str = ""

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """Return the deterministic dispatch ordering key."""
        return (_TIER_RANK.get(self.tier, len(_TIER_RANK)), int(self.order), self.id)

    def matches(self, event: HookEvent, subject: HookSubject) -> bool:
        """Return whether this registration applies to one dispatch."""
        if self.event is not event:
            return False
        if not self.matcher.matches(subject):
            return False
        return self.condition.matches(subject)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record for a receipt or trace row."""
        return {
            "id": self.id,
            "event": self.event.value,
            "type": self.kind,
            "tier": self.tier,
            "matcher": self.matcher.as_dict(),
            "if": self.condition.as_dict(),
            "timeout_s": round(float(self.timeout_s), 3),
            "blocking": bool(self.blocking),
            "order": int(self.order),
            "fail_policy": self.fail_policy,
        }


@dataclass(frozen=True)
class HookRecord:
    """One audit row: what ran, what it decided, how long it took."""

    hook_id: str
    event: str
    kind: str
    tier: str
    outcome: str
    decision: str = HookAction.CONTINUE.value
    reason: str = ""
    duration_ms: float = 0.0
    exit_code: Optional[int] = None
    detail: str = ""
    refused_policy_keys: tuple[str, ...] = ()
    # True when the event is observational and this hook's decision was
    # rewritten to ``continue`` because the event may not change a decision.
    # The record keeps that fact; the rewritten decision is what a caller sees.
    suppressed_for_observational: bool = False
    # This hook's narrowing of the call, when it produced one. On the record so
    # the audit trail answers "what did the hooks change about this call"
    # without re-running anything, and so a rewrite is attributable to the hook
    # that asked for it rather than appearing as an anonymous diff.
    rewrite: Optional[dict[str, Any]] = None
    # The plain-language sentence a user reads. Diagnostic stderr and a Python
    # exception class stay on ``detail``; this is the one sentence that is
    # allowed in front of a person.
    plain: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record (no raw subprocess output)."""
        return {
            "hook_id": self.hook_id,
            "event": self.event,
            "kind": self.kind,
            "tier": self.tier,
            "outcome": self.outcome,
            "decision": self.decision,
            "reason": self.reason,
            "duration_ms": round(float(self.duration_ms), 3),
            "exit_code": self.exit_code,
            "detail": self.detail,
            "plain": self.plain,
            "rewrite": dict(self.rewrite) if self.rewrite else None,
            "refused_policy_keys": list(self.refused_policy_keys),
            "suppressed_for_observational": bool(self.suppressed_for_observational),
        }


@dataclass(frozen=True)
class HookOutcome:
    """The aggregate result of one dispatch, always complete and inspectable."""

    event: str
    action: HookAction = HookAction.CONTINUE
    records: tuple[HookRecord, ...] = ()
    system_messages: tuple[str, ...] = ()
    additional_context: str = ""
    suppress_output: bool = False
    blocked_by: str = ""
    reason: str = ""
    failures: tuple[HookRecord, ...] = ()
    duration_ms: float = 0.0
    budget_exhausted: bool = False
    skipped: int = 0

    @property
    def blocked(self) -> bool:
        """Whether any hook blocked (or asked) on a blocking event."""
        return self.action in (HookAction.BLOCK, HookAction.ASK)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt for a receipt file or trace row."""
        return {
            "event": self.event,
            "action": self.action.value,
            "blocked": self.blocked,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "records": [record.to_dict() for record in self.records],
            "failures": [record.to_dict() for record in self.failures],
            "system_messages": list(self.system_messages),
            "additional_context_chars": len(self.additional_context),
            "suppress_output": self.suppress_output,
            "duration_ms": round(float(self.duration_ms), 3),
            "budget_exhausted": self.budget_exhausted,
            "skipped": int(self.skipped),
        }


@dataclass(frozen=True)
class HookGate:
    """The answer a GATING caller needs: may the action proceed, and why.

    This is the only object a caller should branch on. ``allowed`` already has
    the event's fail policy folded in, so a caller cannot accidentally treat an
    absent verdict as consent: for a ``fail_closed`` event an unusable hook
    yields ``allowed=False`` with ``block_by="fail_policy"``, and for a
    ``fail_open`` event the same unusable hook yields ``allowed=True`` with the
    failure still on ``failures`` and named in ``reason``. Observational events
    always allow, because they cannot decide, but they still carry the record
    of what ran and what failed.
    """

    event: str
    allowed: bool
    action: str = HookAction.CONTINUE.value
    fail_policy: str = "fail_closed"
    fail_policy_source: str = "table"
    block_by: str = ""
    reason: str = ""
    records: tuple[dict[str, Any], ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    system_messages: tuple[str, ...] = ()
    additional_context: str = ""
    suppress_output: bool = False
    duration_ms: float = 0.0
    # One entry per hook that narrowed the call, each naming the hook so a
    # transcript shows WHICH hook rewrote WHAT. Empty means "no rewrite", which
    # is also what a caller gets when it ignores this field entirely.
    rewrite: tuple[dict[str, Any], ...] = ()
    # The narrowed call, as a field mapping. A caller must dispatch THIS rather
    # than the subject it passed in, and it is a field here rather than a
    # computed property so a receipt cannot describe a narrowing that was not
    # applied.
    rewritten_subject: tuple[tuple[str, str], ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether a hook deliberately blocked (or asked) the action."""
        return self.action in (HookAction.BLOCK.value, HookAction.ASK.value)

    @property
    def rewritten(self) -> bool:
        """Whether a ``PreToolUse`` hook narrowed this call."""
        return bool(self.rewrite)

    def subject_overrides(self) -> dict[str, str]:
        """Return the narrowed call as a plain mapping a caller can apply.

        Empty when nothing was rewritten. A method rather than a bare field so
        a caller cannot read :attr:`rewritten_subject` as a flat list of pairs
        and get the columns the wrong way round.
        """
        return {str(key): str(value) for key, value in self.rewritten_subject}

    def refusal(self) -> str:
        """Return the MODEL-FACING refusal text for a denied call, or ``""``.

        A ``PreToolUse`` refusal has to be legible to a model that is about to
        try something else, so it names the event, the hook that refused, and
        the reason in one sentence â€” and it is the same text for every caller,
        so a terminal, a script, and a model cannot receive three different
        accounts of one refusal. ``""`` when the call is allowed.
        """
        if self.allowed:
            return ""
        who = self.block_by or "a hook"
        return (
            f"{self.event} refused this call ({who}). "
            f"{self.reason or 'no reason was given'}"
        ).strip()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt for a trace row or run receipt."""
        return {
            "event": self.event,
            "allowed": bool(self.allowed),
            "action": self.action,
            "blocked": self.blocked,
            "fail_policy": self.fail_policy,
            "fail_policy_source": self.fail_policy_source,
            "block_by": self.block_by,
            "reason": self.reason,
            "records": [dict(item) for item in self.records],
            "failures": [dict(item) for item in self.failures],
            "system_messages": list(self.system_messages),
            "additional_context": self.additional_context,
            "suppress_output": bool(self.suppress_output),
            "duration_ms": round(float(self.duration_ms), 3),
            "rewritten": self.rewritten,
            "rewrite": [dict(item) for item in self.rewrite],
            "rewritten_subject": dict(self.rewritten_subject),
            "refusal": self.refusal(),
        }


# ---------------------------------------------------------------------------
# Configuration: three tiers, explicit precedence, deterministic merge
# ---------------------------------------------------------------------------


def hook_config_tier_paths(repo_path: Optional[str] = None) -> dict[str, Path]:
    """Return the three hook-config file paths, low precedence first.

    ``$NEO_HOOKS_DIR`` overrides the user tier (test isolation), and
    ``$NEO_REPO_DIR``/``repo_path`` choose the project directory. The project
    directory defaults to the nearest ``.neo`` ancestor of the CWD, matching
    the settings-tier convention without importing the CLI layer.
    """
    out: dict[str, Path] = {}
    override = os.environ.get("NEO_HOOKS_DIR")
    if override:
        out["user"] = Path(override).expanduser() / "hooks.json"
    else:
        out["user"] = _global_config_root() / "hooks.json"
    base = Path(repo_path).expanduser() if repo_path else _project_base()
    if base is not None:
        out["project"] = base / ".neo" / "hooks.json"
        out["local"] = base / ".neo" / "hooks.local.json"
    else:
        out["project"] = Path("__no_project__") / "hooks.json"
        out["local"] = Path("__no_project__") / "hooks.local.json"
    return out


def _global_config_root() -> Path:
    override = os.environ.get("NEO_GLOBAL_ROOT")
    if override:
        return Path(override).expanduser()
    config = os.environ.get("NEO_CONFIG")
    if config:
        return Path(config).expanduser().parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "neo"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "neo"
    return Path.home() / ".config" / "neo"


def _project_base() -> Optional[Path]:
    env = os.environ.get("NEO_PROJECT_DIR")
    if env:
        return Path(env).expanduser().parent
    current = Path.cwd()
    for candidate in (current, *current.parents):
        if (candidate / ".neo").is_dir():
            return candidate
    return None


def _read_config_file(path: Path) -> Optional[dict[str, Any]]:
    """Read and parse one hook config file; None when absent, raise when broken."""
    if not path.is_file() or path.is_symlink():
        return None
    if any(part.is_symlink() for part in [path, *path.parents]):
        return None
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise HookConfigError(
                f"{path}: hook config exceeds {_MAX_CONFIG_BYTES} bytes"
            )
        raw = path.read_text(encoding="utf-8-sig")
    except HookConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise HookConfigError(f"{path}: cannot read hook config: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HookConfigError(f"{path}: hook config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HookConfigError(f"{path}: hook config must be a JSON object")
    version = data.get("schema_version", 1)
    if not isinstance(version, int) or version != 1:
        raise HookConfigError(
            f"{path}: unsupported hook config schema_version {version!r}"
        )
    return data


def _spec_from_entry(
    entry: Mapping[str, Any], *, event: HookEvent, tier: str, order: int, origin: str
) -> HookSpec:
    """Validate and build one hook spec from a config entry."""
    if not isinstance(entry, Mapping):
        raise HookConfigError(f"{origin}: hook entry must be an object")
    hook_id = _text(entry.get("id"), 120).strip()
    if not hook_id:
        hook_id = f"{event.value.lower()}-{order}"
    if any(token in hook_id for token in ("/", "\\", "\x00")):
        raise HookConfigError(
            f"{origin}: hook id {hook_id!r} contains a path separator"
        )
    kind = _text(entry.get("type", entry.get("kind", "")), 40).strip().casefold()
    if kind not in ("command", "http", "prompt"):
        raise HookConfigError(
            f"{origin}: hook {hook_id!r} has unsupported type {kind!r}; "
            "expected command, http, or prompt"
        )
    command: list[str] = []
    url = ""
    prompt_text = ""
    if kind == "command":
        raw_command = entry.get("command")
        if isinstance(raw_command, str):
            raise HookConfigError(
                f"{origin}: hook {hook_id!r} command must be an argv list, not a shell string; "
                "a shell string would make the hook an arbitrary code path"
            )
        if not isinstance(raw_command, (list, tuple)) or not raw_command:
            raise HookConfigError(
                f"{origin}: hook {hook_id!r} command must be a non-empty list"
            )
        if len(raw_command) > _MAX_ARGV:
            raise HookConfigError(
                f"{origin}: hook {hook_id!r} command has {len(raw_command)} argv items "
                f"(max {_MAX_ARGV})"
            )
        for item in raw_command:
            rendered = _text(item, _MAX_ARGV_ITEM + 64)
            if "\x00" in rendered:
                raise HookConfigError(
                    f"{origin}: hook {hook_id!r} command has a null byte"
                )
            command.append(rendered[:_MAX_ARGV_ITEM])
    elif kind == "http":
        url = _text(entry.get("url"), 1_000).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HookConfigError(
                f"{origin}: hook {hook_id!r} url must be an absolute http(s) URL"
            )
    else:
        prompt_text = _text(entry.get("prompt", entry.get("text", "")), 4_000)
        if not prompt_text:
            raise HookConfigError(
                f"{origin}: hook {hook_id!r} prompt handler needs a prompt"
            )
    timeout = entry.get("timeout_s", _DEFAULT_TIMEOUT_S)
    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError) as exc:
        raise HookConfigError(
            f"{origin}: hook {hook_id!r} timeout_s must be a number"
        ) from exc
    timeout_s = min(max(0.1, timeout_s), _MAX_TIMEOUT_S)
    raw_fail_policy = entry.get("fail_policy")
    fail_policy = (
        ""
        if raw_fail_policy is None
        else _fail_policy_value(raw_fail_policy, origin=origin)
    )
    return HookSpec(
        id=hook_id,
        event=event,
        kind=kind,
        tier=tier,
        matcher=HookMatcher.from_value(entry.get("matcher")),
        condition=HookMatcher.from_value(entry.get("if")),
        command=tuple(command),
        url=url,
        prompt_text=prompt_text,
        timeout_s=timeout_s,
        blocking=bool(entry.get("blocking", True)),
        order=order,
        fail_policy=fail_policy,
    )


@dataclass(frozen=True)
class HookConfig:
    """The merged, deterministic view of all three configuration tiers."""

    specs: tuple[HookSpec, ...] = ()
    enabled: bool = True
    max_latency_s: float = _DEFAULT_DISPATCH_BUDGET_S
    tiers_present: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None
    egress_policy: Any = None
    # ``(event_name, policy)`` pairs from a tier's ``fail_policy`` table,
    # highest-precedence tier first, so a later tier overrides an earlier one.
    fail_policies: tuple[tuple[str, str], ...] = ()
    # Empty means "the documented table decides every event".
    default_fail_policy: str = ""

    def for_event(self, event: Union[HookEvent, str]) -> tuple[HookSpec, ...]:
        """Return the registrations for one event in dispatch order."""
        selected = HookEvent(event)
        return tuple(
            sorted(
                (spec for spec in self.specs if spec.event is selected),
                key=lambda s: s.sort_key,
            )
        )

    def fail_policy_for(
        self, event: Union[HookEvent, str], spec: Optional[HookSpec] = None
    ) -> tuple[str, str]:
        """Return ``(policy, source)`` for one event, honouring overrides.

        Precedence, highest first: the registration's own ``fail_policy``,
        the highest-precedence tier's ``fail_policy`` table entry for that
        event, the tier's ``default_fail_policy``, then
        :data:`HOOK_FAIL_POLICIES`. The source string names which of those
        decided, so a receipt never says only "fail_closed" without saying
        why.
        """
        name = HookEvent(event).value
        if spec is not None and spec.fail_policy:
            return spec.fail_policy, f"hook:{spec.id}"
        for configured, policy in self.fail_policies:
            if configured == name:
                return policy, "config:event"
        if self.default_fail_policy:
            return self.default_fail_policy, "config:default"
        # The documented table decides. It is resolved through the vocabulary
        # authority so an event this projection does not carry (one of the four
        # added names) still gets ITS OWN declared policy rather than a
        # last-ditch default â€” and so an event with no declared class raises
        # instead of quietly inheriting one.
        return hook_events.fail_policy_for(name), "table"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible summary (no command argv, no secrets)."""
        return {
            "enabled": bool(self.enabled),
            "max_latency_s": round(float(self.max_latency_s), 3),
            "tiers_present": list(self.tiers_present),
            "sources": list(self.sources),
            "fail_policies": {
                name: HOOK_FAIL_POLICIES.get(name, "fail_closed")
                for name in HOOK_EVENTS
            },
            "fail_policy_overrides": {
                name: policy for name, policy in self.fail_policies
            },
            "default_fail_policy": self.default_fail_policy,
            "fail_policy_reasons": {
                name: HOOK_FAIL_POLICY_REASONS.get(name, "") for name in HOOK_EVENTS
            },
            # The FULL eleven-event vocabulary. Additive: the three tables above
            # stay byte-identical for the seven shipped events, and a reader
            # that wants to know what `Notification` or `SubagentStart` does
            # reads these instead of the source.
            "lifecycle_events": list(HOOK_LIFECYCLE_EVENTS),
            "event_classes": dict(hook_events.EVENT_CLASSES),
            "lifecycle_fail_policies": dict(hook_events.FAIL_POLICIES),
            "lifecycle_fail_policy_reasons": dict(hook_events.FAIL_POLICY_REASONS),
            "spec_count": len(self.specs),
            "specs": [spec.to_dict() for spec in self.specs],
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


def load_hook_config(
    *,
    repo_path: Optional[str] = None,
    sources: Optional[Mapping[str, Any]] = None,
    prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
    egress_policy: Any = None,
    enabled: Optional[bool] = None,
    plugin_manifests: Optional[Mapping[str, Any]] = None,
) -> HookConfig:
    """Merge the user/project/local/plugin hook tiers into one config.

    Precedence is explicit and total: ``plugin`` -> ``local`` -> ``project``
    -> ``user``. A hook ``id`` that appears in more than one tier is
    *replaced* by the highest-precedence tier (so a project can neutralize an
    inherited personal hook by id), and every surviving registration keeps its
    own tier label so the trace says which file decided it. Registrations with
    distinct ids all run, ordered ``user`` -> ``project`` -> ``local`` ->
    ``plugin``, then by declared order, then by id â€” the same order on every
    run.

    ``plugin_manifests`` adds the HOOKS STACK: an installed plugin's declared
    hooks are merged as the ``plugin`` tier, so a plugin's ``PreToolUse`` hook
    and the user's own BOTH fire on every call and neither replaces the other.
    Ids are namespaced to ``plugin:<plugin>:<id>`` by
    :func:`hook_events.plugin_hook_document`, which is what makes a collision
    between a plugin's id and a user's id unreachable rather than merely
    unlikely â€” the merge replaces BY ID, so an un-namespaced plugin hook that
    reused a user's id would silently disable that user's gate.

    The stack's ORDER is user then plugin (with project and local between
    them), so the operator's own gate is evaluated first and a plugin's hook
    refines it rather than overriding its verdict. Every surviving
    registration keeps its tier, so a receipt names whose hook ran.

    ``sources`` lets a caller pass tier documents directly (tests, embedded
    hosts) instead of reading files. A file that is present but unparseable
    raises :class:`HookConfigError`: a typo in a hook config must be loud, not
    silently "no hooks". A malformed PLUGIN declaration is a diagnostic, never
    an exception â€” an installed plugin must not be able to break the session
    that merely loads it.
    """
    documents: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    sources_present: list[str] = []
    if sources is not None:
        for tier in HOOK_TIER_PRECEDENCE:
            if tier == PLUGIN_TIER:
                # Plugin registrations arrive as namespaced documents (below),
                # never as a raw tier, because an un-namespaced plugin hook
                # could reuse a user's id and silently replace their gate.
                continue
            value = sources.get(tier)
            if value is None:
                continue
            if not isinstance(value, Mapping):
                raise HookConfigError(f"{tier}: hook config must be a mapping")
            version = value.get("schema_version", 1)
            if version != 1:
                raise HookConfigError(
                    f"{tier}: unsupported hook config schema_version {version!r}"
                )
            documents[tier] = dict(value)
            sources_present.append(f"{tier}:inline")
    else:
        paths = hook_config_tier_paths(repo_path)
        for tier in HOOK_TIER_PRECEDENCE:
            if tier == PLUGIN_TIER:
                continue
            path = paths.get(tier)
            if path is None:
                continue
            data = _read_config_file(path)
            if data is None:
                continue
            documents[tier] = data
            sources_present.append(f"{tier}:{path}")

    if plugin_manifests:
        # Plugin order is the mapping's own insertion order, so a caller that
        # passes a deterministic list gets a deterministic dispatch order, and
        # every plugin's registrations are recorded by name.
        plugin_documents: list[dict[str, Any]] = []
        for raw_name, manifest in plugin_manifests.items():
            document, problems = hook_events.plugin_hook_document(raw_name, manifest)
            diagnostics.extend(dict(item) for item in problems)
            if document is None:
                continue
            plugin_documents.append(document)
            sources_present.append(f"plugin:{str(raw_name).strip()}")
        if plugin_documents:
            # All plugins share ONE tier. Their ids are already namespaced, so
            # two plugins cannot collide either, and they keep their declared
            # order within the tier.
            merged_hooks: dict[str, list[Any]] = {}
            for document in plugin_documents:
                for event, entries in dict(document.get("hooks") or {}).items():
                    merged_hooks.setdefault(str(event), []).extend(list(entries))
            documents[PLUGIN_TIER] = {"schema_version": 1, "hooks": merged_hooks}

    merged: dict[str, HookSpec] = {}
    tiers_present: list[str] = []
    max_latency = _DEFAULT_DISPATCH_BUDGET_S
    config_enabled = True
    # Collected low-precedence-first so a later tier's entry overrides an
    # earlier one by simple re-assignment, the same rule hooks use.
    fail_policies: dict[str, str] = {}
    default_fail_policy = ""
    for tier in HOOK_TIER_PRECEDENCE:
        document = documents.get(tier)
        if document is None:
            continue
        tiers_present.append(tier)
        if "enabled" in document:
            config_enabled = bool(document["enabled"])
        if "max_latency_s" in document:
            try:
                candidate = float(document["max_latency_s"])
            except (TypeError, ValueError):
                candidate = -1.0
            if candidate > 0:
                max_latency = min(candidate, _MAX_DISPATCH_BUDGET_S)
        raw_policies = document.get("fail_policy")
        if raw_policies is not None:
            if not isinstance(raw_policies, Mapping):
                raise HookConfigError(
                    f"{tier}: 'fail_policy' must be an object keyed by event name "
                    f"or the string {FAIL_POLICY_VALUES!r}"
                )
            for raw_event, raw_value in raw_policies.items():
                try:
                    event = HookEvent(raw_event)
                except ValueError as exc:
                    raise HookConfigError(f"{tier}: {exc}") from exc
                if raw_value is None:
                    fail_policies.pop(event.value, None)
                    continue
                fail_policies[event.value] = _fail_policy_value(
                    raw_value, origin=f"{tier}.fail_policy.{event.value}"
                )
        if "default_fail_policy" in document:
            default_fail_policy = _fail_policy_value(
                document.get("default_fail_policy"),
                origin=f"{tier}.default_fail_policy",
            )
        hooks = document.get("hooks", {})
        if not isinstance(hooks, Mapping):
            raise HookConfigError(
                f"{tier}: 'hooks' must be an object keyed by event name"
            )
        for raw_event, entries in hooks.items():
            try:
                event = HookEvent(raw_event)
            except ValueError as exc:
                raise HookConfigError(f"{tier}: {exc}") from exc
            if isinstance(entries, Mapping):
                entries = [entries]
            if not isinstance(entries, (list, tuple)):
                raise HookConfigError(
                    f"{tier}: hooks.{event.value} must be a list of hook objects"
                )
            for order, entry in enumerate(entries):
                spec = _spec_from_entry(
                    entry,
                    event=event,
                    tier=tier,
                    order=order,
                    origin=f"{tier}.{event.value}",
                )
                merged[spec.id] = spec  # a later (higher-precedence) tier replaces
    specs = tuple(sorted(merged.values(), key=lambda spec: spec.sort_key))
    if len(specs) > _MAX_SPECS:
        diagnostics.append(
            {
                "code": "spec_budget_exceeded",
                "detail": f"{len(specs)} registrations kept, first {_MAX_SPECS} in dispatch order",
            }
        )
        specs = specs[:_MAX_SPECS]
    return HookConfig(
        specs=specs,
        enabled=bool(config_enabled if enabled is None else enabled),
        max_latency_s=max_latency,
        tiers_present=tuple(tiers_present),
        sources=tuple(sources_present),
        diagnostics=tuple(diagnostics),
        prompt_renderer=prompt_renderer,
        egress_policy=egress_policy,
        fail_policies=tuple(sorted(fail_policies.items())),
        default_fail_policy=default_fail_policy,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_PLACEHOLDERS = ("{path}", "{tool}", "{task_id}", "{session_id}", "{event}")


def _substitute(
    argv: Sequence[str], subject: HookSubject, event: HookEvent
) -> list[str]:
    """Expand the allowlisted placeholders in a hook argv.

    Substitution is safe because the argv is never handed to a shell, and it is
    *bounded* because each replacement is capped and a value containing a null
    byte is refused rather than truncated into something surprising.
    """
    values = {
        "{path}": subject.path,
        "{tool}": subject.tool,
        "{task_id}": subject.task_id,
        "{session_id}": subject.session_id,
        "{event}": event.value,
    }
    out: list[str] = []
    for item in argv:
        rendered = item
        for placeholder in _PLACEHOLDERS:
            if placeholder in rendered:
                rendered = rendered.replace(placeholder, values[placeholder][:512])
        if "\x00" in rendered:
            raise HookConfigError(
                "hook argv placeholder expanded to a value with a null byte"
            )
        out.append(rendered)
    return out


def _run_command_handler(
    spec: HookSpec, subject: HookSubject, event: HookEvent, *, repo: Optional[Path]
) -> tuple[int, str, str]:
    """Run a command hook. Returns ``(exit_code, stdout, detail)``.

    The argv is fixed and never shell-interpolated, the child environment is
    scrubbed by :func:`shared.security.scrub_environment`, and the cwd is the
    resolved repository (never a hook-supplied path). A missing executable is
    an ordinary failure the caller records, not an exception.
    """
    argv = _substitute(spec.command, subject, event)
    env = security.scrub_environment(extra={"NEO_HOOK_EVENT": event.value})
    cwd = repo if repo is not None and repo.is_dir() else Path.cwd()
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            env=env,
            cwd=str(cwd),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The child is killed by `subprocess.run`'s timeout, so a hook that hangs
        # cannot hang the run; the dispatch resolves by the event's declared fail
        # policy and the row records exit 124.
        return 124, "", f"timed out after {spec.timeout_s}s", "timeout"
    except FileNotFoundError as exc:
        return 127, "", f"executable not found: {exc}", "missing_executable"
    except OSError as exc:
        return 126, "", f"spawn failed: {exc}", "spawn_failed"
    stdout = (completed.stdout or "")[:_MAX_OUTPUT_CHARS]
    if completed.stderr:
        # stderr is DIAGNOSTIC. It reaches the record's `detail` and the plain
        # sentence declines to render a traceback; it is never run state and
        # never a decision input.
        detail = (completed.stderr or "")[:_MAX_REASON_CHARS]
    else:
        detail = ""
    return int(completed.returncode), stdout, detail, ""


def _run_http_handler(
    spec: HookSpec,
    subject: HookSubject,
    event: HookEvent,
    *,
    policy: Any,
) -> tuple[int, str, str, str]:
    """POST the subject to a hook URL through the deny-by-default egress policy.

    Returns ``(status, body, detail, failure_category)``. The host must be
    allowedlisted by the operator; an unlisted host never opens a socket, which
    is the same guarantee ``harness.webfetch`` gives for FETCH.
    """
    if policy is None:
        from shared import egress

        policy = egress.EgressPolicy.build()
    decision = egress_decision(spec.url, policy)
    if not decision.allowed:
        # A refusal is not a success: it is reported with a non-zero status so
        # it lands in the failure list instead of reading as a hook that ran
        # and said nothing.
        return 403, "", f"egress refused: {decision.reason}", "egress_refused"
    payload = json.dumps(
        {"event": event.value, "hook_id": spec.id, "subject": subject.to_dict()},
        ensure_ascii=False,
    ).encode("utf-8")
    from urllib import request as _request

    req = _request.Request(
        spec.url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "neo-hooks/1"},
    )
    try:
        with _request.urlopen(req, timeout=spec.timeout_s) as response:
            body = response.read(_MAX_OUTPUT_CHARS).decode("utf-8", "replace")
            return int(getattr(response, "status", 200) or 200), body, "", ""
    except Exception as exc:  # urllib raises a family; the boundary is the record
        return 0, "", f"http hook failed: {type(exc).__name__}", "http_failed"


def _run_prompt_handler(
    spec: HookSpec,
    subject: HookSubject,
    renderer: Optional[Callable[[HookSpec, HookSubject], str]],
) -> tuple[int, str, str, str]:
    """Render the optional prompt handler.

    A prompt handler is *additive only*: it can produce bounded
    ``additional_context`` and nothing else. With no renderer bound the hook is
    skipped and the skip is returned as a detail string, so the caller records
    it rather than pretending the hook ran.
    """
    if renderer is None:
        return 0, "", "skipped: no prompt renderer bound", "no_prompt_renderer"
    try:
        rendered = renderer(spec, subject)
    except Exception as exc:
        return (
            1,
            "",
            f"prompt renderer raised {type(exc).__name__}",
            "handler_raised",
        )
    if not isinstance(rendered, str):
        return 1, "", "prompt renderer returned a non-string", "bad_output"
    return 0, json.dumps({"additionalContext": rendered[:_MAX_CONTEXT_CHARS]}), "", ""


#: What went wrong, in the vocabulary a person can read. Every failure path in
#: this module reduces to exactly one of these, and the reduction is what lets
#: a stderr dump or a Python exception class stay DIAGNOSTIC (on a record's
#: ``detail``) while the sentence a user reads names only the category and the
#: hook.
#:
#: The mapping is deliberately coarse. A user needs "it timed out" or "it is not
#: installed", not the subprocess's errno, and a finer vocabulary would tempt a
#: caller to interpolate raw output into a user-facing string.
_FAILURE_PLAIN: dict[str, str] = {
    "timeout": "took too long and was stopped",
    "missing_executable": "is not installed on this machine",
    "spawn_failed": "could not be started",
    "egress_refused": "was refused by this project's network allowlist",
    "http_failed": "could not reach its endpoint",
    "unparsable_output": "printed something that is not a hook decision",
    "bad_output": "printed something that is not a hook decision",
    "invalid_decision": "printed a decision word this layer does not know",
    "nonzero_exit": "exited with a failure code and printed no decision",
    "handler_raised": "raised an error",
    "no_prompt_renderer": "needs a prompt renderer, which this session does not have",
    "budget_exhausted": "did not run: this event's time budget was already spent",
    "blocked": "refused the call",
    "asked": "asked the user to decide",
}

_PLAIN_MAX_CHARS = 200

#: A traceback in the middle of user-facing text is the failure this module
#: exists to prevent, so the redaction is applied to the SENTENCE and the test
#: asserts the rendered message is still visible afterwards.
_PLAIN_TRACEBACK = re.compile(
    r"(Traceback \(most recent call last\)|\w+Error|\w+Exception|"
    r'File "[^"]*", line \d+|Traceback)',
    re.IGNORECASE,
)


def plain_hook_sentence(
    hook_id: str, category: str, detail: str = "", *, event: str = ""
) -> str:
    """Return ONE plain sentence describing a hook failure.

    Assumes ``category`` is a key of :data:`_FAILURE_PLAIN` (an unknown category
    degrades to a generic "did not run" rather than inventing a description).
    ``detail`` is consulted ONLY to disambiguate categories the caller has
    already reduced â€” it is never interpolated verbatim, and a detail carrying a
    traceback is dropped rather than rendered. The result is bounded, has no
    markup delimiters of its own, and is safe to print anywhere.

    This is the whole of requirement "a hook's stderr is diagnostic detail in the
    transcript, never run state, and never a traceback in the user's face": the
    sentence is built from a closed vocabulary, not from the failure's text.
    """
    name = str(hook_id or "hook").strip() or "hook"
    phrase = _FAILURE_PLAIN.get(str(category or "").strip().casefold(), "did not run")
    if event:
        sentence = f"{name} {phrase} during {event}"
    else:
        sentence = f"{name} {phrase}"
    detail_text = _text(detail, _MAX_REASON_CHARS)
    if detail_text and not _PLAIN_TRACEBACK.search(detail_text):
        # Only a short, plain, non-exception detail may qualify the sentence.
        # Anything longer or shaped like a trace is dropped: the record keeps it.
        clean = " ".join(detail_text.split())
        if 0 < len(clean) <= _PLAIN_MAX_CHARS and "\n" not in clean:
            sentence = f"{sentence}: {clean}"
    return sentence[: _PLAIN_MAX_CHARS + 200]


def _parse_hook_output(
    exit_code: int, stdout: str, *, subject: Optional["HookSubject"] = None
) -> tuple[Optional[HookDecision], str]:
    """Parse a handler's stdout into a typed decision.

    Returns ``(decision, detail)``. ``decision`` is None when the output was
    not usable, in which case ``detail`` says why and the caller degrades to
    ``continue`` with that detail recorded. A non-zero exit with no parsable
    JSON is still honoured as a block when the exit code is 2 (the conventional
    "I refuse" code), because a gate script's refusal is its whole point.

    ``subject`` is passed through so a rewrite inside the payload is checked
    against the call it claims to narrow at PARSE time rather than after the
    decision exists.
    """
    text = (stdout or "").strip()
    if not text:
        if exit_code == 2:
            return HookDecision(action=HookAction.BLOCK, reason="hook exited 2"), ""
        if exit_code == 0:
            return HookDecision(), ""
        return None, f"hook exited {exit_code} with no parsable output"
    try:
        payload = json.loads(text)
    except ValueError as exc:
        if exit_code == 2:
            return (
                HookDecision(
                    action=HookAction.BLOCK, reason=_text(text, _MAX_REASON_CHARS)
                ),
                f"output was not JSON ({exc}); exit code 2 honoured as a block",
            )
        return None, f"output was not valid JSON: {exc}"
    try:
        return HookDecision.from_payload(payload, subject=subject), ""
    except (ValueError, HookConfigError) as exc:
        return None, f"output could not be typed: {exc}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class HookEngine:
    """Deterministic, bounded, failure-isolated dispatch for user hooks.

    The engine is the only place a declarative hook can influence anything,
    and every path through it produces a :class:`HookOutcome` â€” including the
    paths where nothing matched, a hook failed, or the latency budget ran out.
    There is no silent branch.
    """

    def __init__(
        self,
        config: Optional[HookConfig] = None,
        *,
        repo_path: Optional[str] = None,
        emit: Optional[Callable[..., None]] = None,
        prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
        egress_policy: Any = None,
    ) -> None:
        if config is None:
            config = load_hook_config(
                repo_path=repo_path,
                prompt_renderer=prompt_renderer,
                egress_policy=egress_policy,
            )
        self.config = config
        self.repo = Path(repo_path).expanduser().resolve() if repo_path else None
        self._records: list[HookRecord] = []
        self._emit = emit

    # -- public dispatch surface -------------------------------------------

    def session_start(self, subject: Any = None) -> HookOutcome:
        """Fire ``SessionStart``."""
        return self._dispatch(HookEvent.SESSION_START, subject)

    def pre_tool_use(self, subject: Any = None) -> HookOutcome:
        """Fire ``PreToolUse`` before a tool call is dispatched."""
        return self._dispatch(HookEvent.PRE_TOOL_USE, subject)

    def post_tool_use(self, subject: Any = None) -> HookOutcome:
        """Fire ``PostToolUse`` after a successful tool call."""
        return self._dispatch(HookEvent.POST_TOOL_USE, subject)

    def post_tool_use_failure(self, subject: Any = None) -> HookOutcome:
        """Fire ``PostToolUseFailure`` after a failed tool call."""
        return self._dispatch(HookEvent.POST_TOOL_USE_FAILURE, subject)

    def pre_compact(self, subject: Any = None) -> HookOutcome:
        """Fire ``PreCompact`` before context compaction."""
        return self._dispatch(HookEvent.PRE_COMPACT, subject)

    def stop(self, subject: Any = None) -> HookOutcome:
        """Fire ``Stop``, the completion hook that can block a verified result."""
        return self._dispatch(HookEvent.STOP, subject)

    def session_end(self, subject: Any = None) -> HookOutcome:
        """Fire ``SessionEnd``."""
        return self._dispatch(HookEvent.SESSION_END, subject)

    def user_prompt_submit(self, subject: Any = None) -> HookOutcome:
        """Fire ``UserPromptSubmit``, the gate on the user's own prompt."""
        return self._dispatch(HookEvent.USER_PROMPT_SUBMIT, subject)

    def notification(self, subject: Any = None) -> HookOutcome:
        """Fire ``Notification``, before the shell notifies the user."""
        return self._dispatch(HookEvent.NOTIFICATION, subject)

    def subagent_start(self, subject: Any = None) -> HookOutcome:
        """Fire ``SubagentStart``."""
        return self._dispatch(HookEvent.SUBAGENT_START, subject)

    def subagent_stop(self, subject: Any = None) -> HookOutcome:
        """Fire ``SubagentStop``."""
        return self._dispatch(HookEvent.SUBAGENT_STOP, subject)

    def method_for(self, event: Union[HookEvent, str]) -> Callable[..., HookOutcome]:
        """Return the named dispatch method for one event.

        Lets a caller that already holds an event NAME (the connector surface, a
        config file, an integration loop) fire it without writing the seven-way
        dispatch again â€” which is how two surfaces end up disagreeing about
        which method an event maps to.
        """
        selected = HookEvent(event)
        table = {
            HookEvent.SESSION_START: self.session_start,
            HookEvent.USER_PROMPT_SUBMIT: self.user_prompt_submit,
            HookEvent.PRE_TOOL_USE: self.pre_tool_use,
            HookEvent.POST_TOOL_USE: self.post_tool_use,
            HookEvent.POST_TOOL_USE_FAILURE: self.post_tool_use_failure,
            HookEvent.NOTIFICATION: self.notification,
            HookEvent.STOP: self.stop,
            HookEvent.SUBAGENT_START: self.subagent_start,
            HookEvent.SUBAGENT_STOP: self.subagent_stop,
            HookEvent.PRE_COMPACT: self.pre_compact,
            HookEvent.SESSION_END: self.session_end,
        }
        return table[selected]

    def gate(self, event: Union[HookEvent, str], subject: Any = None) -> HookGate:
        """Fire one event and return the GATING answer, fail policy applied.

        Assumes the caller is about to do something the event is supposed to
        gate (dispatch a tool call, report a completion). The returned
        :class:`HookGate` is the only value a caller should branch on, because
        it has already resolved the three questions that are easy to get wrong:
        did a hook deliberately block, did a hook fail, and what does this
        event's documented fail policy say about a hook that could not run.

        An event with no matching registration is NOT a failure â€” there is
        nothing to be silent about. A disabled config is likewise not a
        failure, because the operator turned the layer off on purpose; it is
        reported through ``fail_policy_source`` staying ``table`` and no
        failure rows.
        """
        selected = HookEvent(event)
        policy, source = self.config.fail_policy_for(selected)
        outcome = self._dispatch(selected, subject)
        records = tuple(record.to_dict() for record in outcome.records)
        failures = tuple(record.to_dict() for record in outcome.failures)
        rewrite = tuple(
            {
                "hook_id": row.hook_id,
                "tier": row.tier,
                **{
                    key: value
                    for key, value in (row.rewrite or {}).items()
                    if key != "refused"
                },
                "refused": list((row.rewrite or {}).get("refused") or ()),
            }
            for row in outcome.records
            if row.rewrite
        )
        if outcome.blocked:
            return HookGate(
                event=outcome.event,
                allowed=False,
                action=outcome.action.value,
                fail_policy=policy,
                fail_policy_source=source,
                block_by=outcome.blocked_by or "hook",
                reason=outcome.reason or f"blocked by hook {outcome.blocked_by}",
                records=records,
                failures=failures,
                system_messages=tuple(outcome.system_messages),
                additional_context=outcome.additional_context,
                suppress_output=bool(outcome.suppress_output),
                duration_ms=float(outcome.duration_ms),
                rewrite=rewrite,
            )
        if failures:
            failing = ", ".join(str(row.get("hook_id") or "?") for row in failures)
            detail = "; ".join(
                str(row.get("reason") or row.get("detail") or "hook failed")
                for row in failures
            )
            # An OBSERVATIONAL event can never decide, so it can never refuse
            # either: the failure is still reported, on the reason and on
            # ``failures``, but the action proceeds. For a gating event the
            # fail policy is what decides, and a fail-closed one refuses.
            refuse = policy == "fail_closed" and not selected.observational
            if refuse:
                return HookGate(
                    event=outcome.event,
                    allowed=False,
                    action=HookAction.BLOCK.value,
                    fail_policy=policy,
                    fail_policy_source=source,
                    block_by="fail_policy",
                    reason=(
                        f"{selected.value} gate hook(s) {failing} could not run and the "
                        f"event's fail policy is fail_closed: {detail}"
                    ),
                    records=records,
                    failures=failures,
                    system_messages=tuple(outcome.system_messages),
                    additional_context=outcome.additional_context,
                    suppress_output=bool(outcome.suppress_output),
                    duration_ms=float(outcome.duration_ms),
                )
            return HookGate(
                event=outcome.event,
                allowed=True,
                action=HookAction.CONTINUE.value,
                fail_policy=policy,
                fail_policy_source=source,
                reason=(
                    f"{selected.value} hook(s) {failing} failed and the event's fail "
                    f"policy is fail_open: {detail}"
                ),
                records=records,
                failures=failures,
                system_messages=tuple(outcome.system_messages),
                additional_context=outcome.additional_context,
                suppress_output=bool(outcome.suppress_output),
                duration_ms=float(outcome.duration_ms),
                rewrite=rewrite,
            )
        # The ALLOWED tail. A ``PreToolUse`` hook may have narrowed the call, so
        # this is where the subject the caller must actually DISPATCH is
        # computed â€” from the subject the hooks saw, through the recorded
        # rewrites, in dispatch order. The rewrite is not a claim on a receipt:
        # `rewritten_subject` is the call, and a caller that ignores it dispatches
        # the original.
        original = HookSubject.from_value(subject)
        final = original
        for entry in rewrite:
            final = HookRewrite(
                command_prefix=str(entry.get("command_prefix") or ""),
                path=str(entry.get("path") or ""),
            ).apply(final)
        overrides = tuple(
            (field, str(getattr(final, field)))
            for field in _REWRITE_CAPABILITIES
            if getattr(final, field) != getattr(original, field)
        )
        return HookGate(
            event=outcome.event,
            allowed=True,
            action=outcome.action.value,
            fail_policy=policy,
            fail_policy_source=source,
            reason=outcome.reason,
            records=records,
            failures=failures,
            system_messages=tuple(outcome.system_messages),
            additional_context=outcome.additional_context,
            suppress_output=bool(outcome.suppress_output),
            duration_ms=float(outcome.duration_ms),
            rewrite=rewrite,
            rewritten_subject=overrides,
        )

    @property
    def records(self) -> tuple[HookRecord, ...]:
        """Return the bounded audit history of every dispatch."""
        return tuple(self._records)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible view of the engine and its config."""
        return {
            "config": self.config.to_dict(),
            "repo": str(self.repo) if self.repo else "",
            "record_count": len(self._records),
        }

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, event: HookEvent, subject: Any) -> HookOutcome:
        started = time.perf_counter()
        records: list[HookRecord] = []
        failures: list[HookRecord] = []
        messages: list[str] = []
        context_parts: list[str] = []
        action = HookAction.CONTINUE
        blocked_by = ""
        reason = ""
        suppress = False
        skipped = 0
        budget_exhausted = False
        projected = HookSubject.from_value(subject)
        if not self.config.enabled:
            return self._finish(
                HookOutcome(
                    event=event.value,
                    action=HookAction.CONTINUE,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    skipped=len(self.config.for_event(event)),
                ),
                event,
                [],
            )
        budget = float(self.config.max_latency_s)
        for spec in self.config.for_event(event):
            if not spec.matches(event, projected):
                skipped += 1
                continue
            if (time.perf_counter() - started) >= budget:
                record = HookRecord(
                    hook_id=spec.id,
                    event=event.value,
                    kind=spec.kind,
                    tier=spec.tier,
                    outcome="skipped",
                    detail="latency_budget_exhausted",
                )
                records.append(record)
                failures.append(record)
                skipped += 1
                budget_exhausted = True
                continue
            decision, record = self._run_one(spec, event, projected)
            records.append(record)
            if record.outcome not in ("ok", "skipped"):
                failures.append(record)
            if decision is None:
                continue
            if decision.suppress_output:
                suppress = True
            if decision.system_message:
                messages.append(decision.system_message)
            if decision.additional_context:
                context_parts.append(decision.additional_context)
            if event.observational:
                # Observational events may add context and suppress output but
                # can never change a decision. ``_run_one`` already rewrote a
                # non-continue decision to ``continue`` and recorded the fact on
                # the row, so simply not entering the precedence branch below
                # is the whole enforcement.
                continue
            if spec.blocking and decision.action.rank > action.rank:
                # Strictly-greater means equal-rank decisions are FIRST-wins,
                # so the recorded blocker is the earliest blocking hook in
                # dispatch order. Every hook keeps its own row and reason, so
                # the losing reason is still visible on the outcome.
                action = decision.action
                blocked_by = spec.id
                reason = decision.reason
        outcome = HookOutcome(
            event=event.value,
            action=action,
            records=tuple(records),
            system_messages=tuple(messages),
            additional_context="\n".join(context_parts)[:_MAX_CONTEXT_CHARS],
            suppress_output=suppress,
            blocked_by=blocked_by,
            reason=reason,
            failures=tuple(failures),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            budget_exhausted=budget_exhausted,
            skipped=skipped,
        )
        return self._finish(outcome, event, records, projected)

    def _run_one(
        self, spec: HookSpec, event: HookEvent, subject: HookSubject
    ) -> tuple[Optional[HookDecision], HookRecord]:
        """Run one hook and produce its decision plus its audit row."""
        started = time.perf_counter()
        exit_code: Optional[int]
        failure = ""
        try:
            if spec.kind == "command":
                exit_code, stdout, detail, failure = _run_command_handler(
                    spec, subject, event, repo=self.repo
                )
            elif spec.kind == "http":
                exit_code, stdout, detail, failure = _run_http_handler(
                    spec, subject, event, policy=self.config.egress_policy
                )
            else:
                exit_code, stdout, detail, failure = _run_prompt_handler(
                    spec, subject, self.config.prompt_renderer
                )
        except HookConfigError as exc:
            detail = _text(str(exc), _MAX_REASON_CHARS)
            record = HookRecord(
                hook_id=spec.id,
                event=event.value,
                kind=spec.kind,
                tier=spec.tier,
                outcome="failed",
                detail=detail,
                plain=plain_hook_sentence(
                    spec.id, "bad_output", detail, event=event.value
                ),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return None, _with_suppression(record, event, "hook could not be prepared")
        except Exception as exc:  # isolation: a hook can never take the run down
            record = HookRecord(
                hook_id=spec.id,
                event=event.value,
                kind=spec.kind,
                tier=spec.tier,
                outcome="failed",
                detail=f"handler raised {type(exc).__name__}",
                plain=plain_hook_sentence(
                    spec.id, "handler_raised", "", event=event.value
                ),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return None, _with_suppression(record, event, "hook handler raised")
        duration = (time.perf_counter() - started) * 1000.0
        if spec.kind == "prompt" and detail.startswith("skipped:"):
            record = HookRecord(
                hook_id=spec.id,
                event=event.value,
                kind=spec.kind,
                tier=spec.tier,
                outcome="skipped",
                detail=detail,
                plain=plain_hook_sentence(
                    spec.id, "no_prompt_renderer", "", event=event.value
                ),
                exit_code=exit_code,
                duration_ms=duration,
            )
            return None, _with_suppression(record, event, detail)
        decision, parse_detail = _parse_hook_output(exit_code, stdout, subject=subject)
        if decision is None:
            # The handler's own detail (a timeout, a refused egress, a missing
            # executable) is the actionable part; the parse note is the
            # secondary one. Both reach the record so the failure is
            # diagnosable, never just "the hook did not produce output".
            reason = "; ".join(part for part in (detail, parse_detail) if part)
            category = failure or (
                "unparsable_output" if parse_detail else "nonzero_exit"
            )
            record = HookRecord(
                hook_id=spec.id,
                event=event.value,
                kind=spec.kind,
                tier=spec.tier,
                outcome="failed",
                reason=reason or "hook output was unusable",
                plain=plain_hook_sentence(spec.id, category, detail, event=event.value),
                exit_code=exit_code,
                duration_ms=duration,
                detail=detail or parse_detail,
            )
            return None, _with_suppression(record, event, "hook output was unusable")
        if decision.refused_policy_keys:
            refused = ", ".join(decision.refused_policy_keys)
            detail = (
                f"{detail}; " if detail else ""
            ) + f"policy mutation refused: {refused}"
            detail = detail[:_MAX_REASON_CHARS]
        if decision.refused_rewrite_keys:
            refused = ", ".join(decision.refused_rewrite_keys)
            detail = (f"{detail}; " if detail else "") + f"rewrite refused: {refused}"
            detail = detail[:_MAX_REASON_CHARS]
        plain = ""
        if decision.blocked:
            plain = plain_hook_sentence(spec.id, "blocked", "", event=event.value)
        elif decision.needs_approval:
            plain = plain_hook_sentence(spec.id, "asked", "", event=event.value)
        elif decision.rewrite is not None:
            narrowed = ", ".join(
                f"{field} -> {getattr(decision.rewrite, field)}"
                for field in _REWRITE_CAPABILITIES
                if getattr(decision.rewrite, field)
            )
            plain = f"{spec.id} narrowed this call during {event.value}: {narrowed}"
        record = HookRecord(
            hook_id=spec.id,
            event=event.value,
            kind=spec.kind,
            tier=spec.tier,
            outcome="ok",
            decision=decision.action.value,
            reason=decision.reason or detail,
            plain=plain,
            exit_code=exit_code,
            duration_ms=duration,
            detail=detail,
            refused_policy_keys=decision.refused_policy_keys,
            rewrite=decision.rewrite.to_dict() if decision.rewrite else None,
        )
        if event.observational and (
            decision.action is not HookAction.CONTINUE or decision.rewrite is not None
        ):
            # An observational event cannot decide, and a REWRITE is a change to
            # the call â€” so it is as out of place there as a `block`. Both are
            # dropped and the row says which was ignored.
            what = (
                f"a {decision.action.value} decision"
                if decision.action is not HookAction.CONTINUE
                else "a rewrite"
            )
            record = HookRecord(
                hook_id=record.hook_id,
                event=record.event,
                kind=record.kind,
                tier=record.tier,
                outcome="ignored",
                decision=HookAction.CONTINUE.value,
                reason=f"observational event ignored {what}",
                plain=f"{spec.id} tried to change the call during "
                f"{event.value}, which cannot decide",
                exit_code=record.exit_code,
                duration_ms=record.duration_ms,
                detail=record.detail,
                refused_policy_keys=record.refused_policy_keys,
            )
            return (
                HookDecision(
                    action=HookAction.CONTINUE,
                    system_message=decision.system_message,
                    additional_context=decision.additional_context,
                    suppress_output=decision.suppress_output,
                    refused_policy_keys=decision.refused_policy_keys,
                ),
                _with_suppression(record, event, "observational decision ignored"),
            )
        return decision, _with_suppression(
            record,
            event,
            "policy_mutation_refused"
            if decision.refused_policy_keys
            else ("rewrite_refused" if decision.refused_rewrite_keys else ""),
        )

    def _finish(
        self,
        outcome: HookOutcome,
        event: HookEvent,
        records: Sequence[HookRecord],
        subject: Optional[HookSubject] = None,
    ) -> HookOutcome:
        """Record the dispatch and mirror it onto the unified trace stream."""
        self._records.extend(records)
        if len(self._records) > _RECORD_HISTORY:
            del self._records[:-_RECORD_HISTORY]
        emit = self._emit
        if emit is None:
            emit = tracing.emit
        try:
            emit(
                "extensions",
                "user_hook",
                event=outcome.event,
                action=outcome.action.value,
                blocked=outcome.blocked,
                blocked_by=outcome.blocked_by,
                reason=outcome.reason,
                records=len(outcome.records),
                failures=len(outcome.failures),
                duration_ms=round(outcome.duration_ms, 3),
                budget_exhausted=outcome.budget_exhausted,
                tool=(subject.tool if subject else ""),
                task_id=(subject.task_id if subject else ""),
            )
        except Exception:
            # Observability never changes a task outcome (shared.tracing contract).
            pass
        return outcome


def _with_suppression(record: HookRecord, event: HookEvent, detail: str) -> HookRecord:
    """Attach the internal suppression marker to a record.

    The marker is how the dispatcher knows a record's decision was rewritten to
    ``continue`` because the event is observational. It is a real field on the
    frozen record (assigned through ``object.__setattr__``) and is part of the
    public ``to_dict`` projection, so the audit trail says explicitly that a
    decision was ignored rather than leaving the reader to infer it.
    """
    object.__setattr__(
        record, "suppressed_for_observational", bool(event.observational)
    )
    if detail and not record.detail:
        object.__setattr__(record, "detail", detail)
    return record


# ---------------------------------------------------------------------------
# Verifier integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostEditGateReport:
    """The typed verdict of the post-edit gate (target test or lint subset)."""

    ran: bool
    passed: bool
    hook_id: str = ""
    command: tuple[str, ...] = ()
    exit_code: Optional[int] = None
    duration_ms: float = 0.0
    output: str = ""
    reason: str = ""
    skipped: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report (bounded output)."""
        return {
            "ran": bool(self.ran),
            "passed": bool(self.passed),
            "hook_id": self.hook_id,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "duration_ms": round(float(self.duration_ms), 3),
            "output_chars": len(self.output),
            "reason": self.reason,
            "skipped": self.skipped,
        }


class PostEditGate:
    """Run the declared post-edit gate after a mutation.

    The gate is an ordinary ``PostToolUse`` registration whose command is the
    project's target test or lint subset; this wrapper makes its verdict a
    typed value the caller can put in front of the verifier gate instead of a
    string it has to re-parse. It never decides a task's completion: it reports
    whether the declared subset was green, and a red subset is a signal the
    caller must honour.

    Assumes the configured hook command is a fixed argv (the config loader
    already refuses shell strings), so nothing here re-parses a command line.
    """

    def __init__(
        self, engine: HookEngine, *, event: HookEvent = HookEvent.POST_TOOL_USE
    ) -> None:
        self.engine = engine
        self.event = event

    def run(self, subject: Any) -> PostEditGateReport:
        """Run every matching post-edit gate hook and aggregate the verdicts."""
        projected = HookSubject.from_value(subject)
        specs = [
            spec
            for spec in self.engine.config.for_event(self.event)
            if spec.kind == "command" and spec.matches(self.event, projected)
        ]
        if not specs:
            return PostEditGateReport(
                ran=False, passed=True, skipped="no post-edit gate configured"
            )
        passed = True
        started = time.perf_counter()
        last = PostEditGateReport(
            ran=False, passed=True, skipped="no post-edit gate ran"
        )
        for spec in specs:
            decision, record = self.engine._run_one(spec, self.event, projected)
            duration = record.duration_ms
            ok = decision is not None and decision.action is HookAction.CONTINUE
            report = PostEditGateReport(
                ran=True,
                passed=ok,
                hook_id=spec.id,
                command=spec.command,
                exit_code=record.exit_code,
                duration_ms=duration,
                reason=record.reason or record.detail,
            )
            if not ok:
                passed = False
            last = report
        return PostEditGateReport(
            ran=True,
            passed=passed,
            hook_id=last.hook_id,
            command=last.command,
            exit_code=last.exit_code,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            reason=last.reason,
        )


@dataclass(frozen=True)
class CompletionGateVerdict:
    """Whether a run may report ``completed_verified``, and why not."""

    allowed: bool
    status: str
    original_status: str
    blocked_by: str = ""
    reason: str = ""
    failures: tuple[dict[str, Any], ...] = ()
    duration_ms: float = 0.0
    fail_policy: str = "fail_closed"
    fail_policy_source: str = "table"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible verdict."""
        return {
            "allowed": bool(self.allowed),
            "status": self.status,
            "original_status": self.original_status,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "failures": [dict(item) for item in self.failures],
            "duration_ms": round(float(self.duration_ms), 3),
            "fail_policy": self.fail_policy,
            "fail_policy_source": self.fail_policy_source,
        }


class CompletionGate:
    """Block completion when the ``Stop`` gate is not green.

    The invariant this class exists to protect: **a blocking completion hook
    prevents a false success.** The gate only ever *downgrades* a status â€”
    ``completed_verified`` becomes ``completed_unverified`` when a ``Stop``
    hook blocks, and no input can make it produce ``completed_verified``. A run
    that was already unverified, failed, timed out, or cancelled is returned
    unchanged, because the gate is not what makes a result honest.

    ``Stop`` is a ``fail_closed`` event, so a ``Stop`` hook that RAISES, times
    out, or returns unusable output also downgrades a verified status. That is
    the conservative direction on purpose: a gate that could not run has not
    cleared the run, and the run's own verifier evidence is the only other
    thing that can. An operator who genuinely wants a broken ``Stop`` hook to
    be advisory sets ``"fail_policy": {"Stop": "fail_open"}`` in a hook tier;
    that override is recorded on the verdict.
    """

    VERIFIED = "completed_verified"
    UNVERIFIED = "completed_unverified"

    def __init__(self, engine: HookEngine) -> None:
        self.engine = engine

    def evaluate(
        self,
        subject: Any,
        *,
        status: str,
        verification: Optional[Mapping[str, Any]] = None,
    ) -> CompletionGateVerdict:
        """Run the ``Stop`` hooks and return the status a run may report."""
        original = _text(status, 100) or self.UNVERIFIED
        gate = self.engine.gate(HookEvent.STOP, subject)
        blocked = not gate.allowed
        may_report_verified = original == self.VERIFIED and not blocked
        resolved = self.VERIFIED if may_report_verified else self.UNVERIFIED
        verdict = CompletionGateVerdict(
            allowed=not blocked,
            status=resolved,
            original_status=original,
            blocked_by=gate.block_by,
            reason=gate.reason
            or ("completion blocked by a Stop hook" if blocked else ""),
            failures=gate.failures,
            duration_ms=gate.duration_ms,
            fail_policy=gate.fail_policy,
            fail_policy_source=gate.fail_policy_source,
        )
        if verification is not None and may_report_verified:
            evidence = dict(verification)
            if not evidence.get("target_passed") or not evidence.get(
                "regression_passed"
            ):
                # Defence in depth: even a green Stop hook cannot promote a run
                # whose own verifier evidence is incomplete.
                return CompletionGateVerdict(
                    allowed=False,
                    status=self.UNVERIFIED,
                    original_status=original,
                    blocked_by="verifier_evidence",
                    reason="verifier evidence is incomplete",
                    failures=verdict.failures,
                    duration_ms=verdict.duration_ms,
                )
        return verdict


# ---------------------------------------------------------------------------
# Trust: a hook a person has decided about
# ---------------------------------------------------------------------------

_TRUST_FILE = "hook-trust.json"
_TRUST_VERSION = 1
_TRUST_DECISIONS: tuple[str, ...] = ("trusted", "untrusted")


@dataclass(frozen=True)
class HookTrust:
    """One recorded trust decision about one hook.

    A trust decision is about a SPECIFIC DECLARATION, so it carries the
    registration's digest: editing a hook after trusting it makes the digest
    stop matching, and :meth:`still_matches` turns that into ``changed`` rather
    than a decision that silently carries over to code the person never read.

    Only the user's own decision is recorded. There is deliberately no
    "trusted by plugin" value: a plugin cannot vouch for itself, and a hook that
    could grant its own trust would make the whole mechanism decorative.
    """

    hook_id: str
    decision: str
    digest: str
    noted: str = ""

    @property
    def trusted(self) -> bool:
        """Whether this hook is recorded as trusted."""
        return self.decision == "trusted"

    def still_matches(self, digest: str) -> str:
        """Return ``"current"``, ``"changed"`` or ``"new"`` for a digest.

        ``"changed"`` is the important answer: the hook's declaration differs
        from the one that was trusted, so the recorded decision does not apply
        to what is on disk now.
        """
        if not self.digest:
            return "new"
        if self.digest == str(digest or ""):
            return "current"
        return "changed"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record for a receipt."""
        return {
            "hook_id": self.hook_id,
            "decision": self.decision,
            "digest": self.digest,
            "noted": self.noted,
        }


def hook_trust_path(*, env: Optional[Mapping[str, str]] = None) -> Path:
    """Return the path of the hook-trust ledger.

    Lives beside the hook config's user tier so it inherits the same overrides
    (``$NEO_HOOKS_DIR``, ``$NEO_GLOBAL_ROOT``, ``$NEO_CONFIG``, XDG, the Windows
    roaming app-data dir) and a test that isolates the hooks directory also
    isolates the ledger. It is deliberately OUTSIDE every repository: a trust
    decision is a person's, not a project's, and a repo that shipped one would
    be shipping an authorisation.
    """
    environ = dict(env if env is not None else os.environ)
    override = environ.get("NEO_HOOK_TRUST_FILE")
    if override:
        return Path(override).expanduser()
    # Derived from the USER TIER's own directory rather than recomputed, so the
    # ledger can never land somewhere the hook config does not when
    # ``$NEO_HOOKS_DIR`` is set. A test that isolates the hooks directory
    # therefore isolates the ledger without a second variable to remember.
    hooks_override = environ.get("NEO_HOOKS_DIR")
    if hooks_override:
        return Path(hooks_override).expanduser() / _TRUST_FILE
    return _global_config_root() / _TRUST_FILE


def load_hook_trust(
    *, path: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> dict[str, HookTrust]:
    """Read the trust ledger; ``{}`` when absent or unreadable.

    Never raises. A ledger nobody can read is treated as no decisions recorded,
    which means every hook reads as ``new`` and the user is asked rather than
    assumed â€” the fail-closed direction for an authorisation store.
    """
    target = Path(path) if path is not None else hook_trust_path(env=env)
    try:
        if not target.is_file() or target.is_symlink():
            return {}
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping) or data.get("schema_version") != _TRUST_VERSION:
        return {}
    entries = data.get("hooks")
    if not isinstance(entries, Mapping):
        return {}
    out: dict[str, HookTrust] = {}
    for raw_id, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        decision = str(entry.get("decision") or "").strip().casefold()
        if decision not in _TRUST_DECISIONS:
            continue
        out[str(raw_id)] = HookTrust(
            hook_id=str(raw_id),
            decision=decision,
            digest=str(entry.get("digest") or ""),
            noted=_text(entry.get("note"), 200),
        )
    return out


def record_hook_trust(
    hook_id: str,
    decision: str,
    digest: str,
    *,
    note: str = "",
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> HookTrust:
    """Record the USER's trust decision about one hook and return it.

    ``decision`` must be ``"trusted"`` or ``"untrusted"``; anything else raises
    :class:`HookConfigError` rather than being coerced, because a silently
    coerced value here would be an authorisation nobody actually gave.

    The write goes through a sibling ``*.lock`` file created ``O_EXCL``, which is
    atomic on every filesystem this project supports, because two ``/hooks trust``
    invocations writing the same ledger read-modify-write would otherwise lose
    one decision â€” the exact failure mode that would make a trust ledger
    untrustworthy.
    """
    name = _text(hook_id, 120).strip()
    if not name:
        raise HookConfigError("a trust decision needs a hook id")
    verdict = _text(decision, 20).strip().casefold()
    if verdict not in _TRUST_DECISIONS:
        raise HookConfigError(
            f"trust decision must be one of {_TRUST_DECISIONS}, got {decision!r}"
        )
    target = Path(path) if path is not None else hook_trust_path(env=env)
    existing = load_hook_trust(path=target, env=env)
    existing[name] = HookTrust(
        hook_id=name,
        decision=verdict,
        digest=str(digest or ""),
        noted=_text(note, 200),
    )
    payload = {
        "schema_version": _TRUST_VERSION,
        "hooks": {key: item.to_dict() for key, item in sorted(existing.items())},
    }
    lock = target.with_suffix(target.suffix + ".lock")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # A stale lock from a crashed writer must not wedge trust forever.
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0.0
                if age < 30:
                    raise HookConfigError(
                        f"another hook-trust write holds {lock}; try again in a moment"
                    ) from None
            lock.unlink(missing_ok=True)
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        # The lock's EXISTENCE is the lock, so the descriptor is closed
        # immediately. Leaving it open holds a Windows handle on the file, which
        # makes the `unlink` below fail with a sharing violation and leaves the
        # ledger permanently locked - measured on this host, not assumed.
        os.close(descriptor)
        try:
            # The LOCK file only guards the read-modify-write; the payload goes
            # to a uniquely named temp file in the same directory and is then
            # `os.replace`d onto the ledger. A fixed temp name would be shared by
            # every writer, which is the same clobbering failure this lock exists
            # to prevent.
            staged = target.with_name(
                f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                with open(staged, "w", encoding="utf-8") as handle:
                    json.dump(
                        payload, handle, ensure_ascii=False, sort_keys=True, indent=2
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(staged), str(target))
            finally:
                staged.unlink(missing_ok=True)
        finally:
            lock.unlink(missing_ok=True)
        # Mode 0600 best effort: an authorisation ledger is not world readable on
        # a POSIX host. Windows has no equivalent bit and is left as-is.
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except HookConfigError:
        raise
    except OSError as exc:
        raise HookConfigError(f"cannot record hook trust: {exc}") from exc
    return existing[name]


# ---------------------------------------------------------------------------
# /hooks â€” the verbs, and the one implementation behind them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCommandResult:
    """What every ``/hooks`` verb and every ``neo hooks`` subcommand returns.

    ONE return type for every verb, because the product rule is that the slash
    verb is the implementation and the script surface delegates to it: a second
    return shape is how the two surfaces start disagreeing about what happened.

    ``lines`` are PLAIN text. Every string here originates in a config file, a
    plugin manifest, or a hook's own output, so it crosses into a markup parser
    as DATA; the renderers escape on the way out and the test renders a hostile
    hook id through a real rich ``Console`` and asserts the message is still
    VISIBLE, because a substring assertion passes while the message is eaten.

    ``document`` is the verb's PRIMARY machine-readable document — the merged
    config for ``list``, the gate receipt for ``run``, and so on. It is spread
    into the top level of :meth:`to_dict` so the ``--json`` shape a script has
    always parsed keeps its keys exactly where they were; the envelope and this
    round's additions ride alongside it. That is why a new verb can add a field
    without a script having to learn a new place to look for the old one.
    """

    command: str
    status: str
    exit_code: int
    lines: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    document: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the verb succeeded."""
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt.

        The verb's own document is at the TOP LEVEL, so every key the historical
        ``neo hooks --json`` output published (``allowed``, ``fail_policy``,
        ``specs``, …) is still at the top level. The envelope is additive.
        """
        return {
            **self.document,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "lines": list(self.lines),
            **({"payload": self.payload} if self.payload else {}),
        }


HOOK_VERBS: tuple[str, ...] = ("list", "run", "test", "trust", "reload")
_HOOK_VERB_ALIASES = {
    "ls": "list",
    "show": "list",
    "fire": "run",
    "try": "test",
    "check": "test",
    "refresh": "reload",
}
_HOOK_REASONS: dict[str, str] = {
    "block": "it refused this call",
    "ask": "it asked the user to decide",
}
_MAX_TEST_OUTPUT = 4_096
_MAX_LIST_ROWS = 200

_SUBJECT_FLAGS: dict[str, str] = {
    "--tool": "tool",
    "--path": "path",
    "--command": "command",
    "--command-prefix": "command_prefix",
    "--mcp-server": "mcp_server",
    "--side-effect": "side_effect_class",
    "--task-id": "task_id",
    "--session-id": "session_id",
    "--status": "status",
    "--event": "event",
    "--reason": "reason",
    "--note": "note",
    "--id": "hook_id",
    "--decision": "decision",
    "--repo": "repo",
}
#: Flags that configure the VERB rather than the hook subject.
_VERB_ONLY_FLAGS = frozenset({"event", "reason", "note", "hook_id", "decision", "repo"})


def hook_lines(lines: Sequence[str]) -> tuple[str, ...]:
    """Return ``lines`` escaped, one per entry, bounded count.

    The ONE exit every hook surface uses for human-readable output. Delegates to
    rich's own ``escape`` so this module cannot drift from the parser's
    understanding of what a tag is, and returns the plain string unchanged when
    rich is unimportable â€” an unescaped bracket is at worst interpreted, an
    over-escaped one at worst ugly.
    """
    try:
        from rich.markup import escape
    except Exception:  # pragma: no cover - an install without rich
        return tuple(str(line) for line in list(lines)[:_MAX_LIST_ROWS])
    return tuple(escape(str(line)) for line in list(lines)[:_MAX_LIST_ROWS])


def _spec_digest(spec: HookSpec) -> str:
    """Return the stable digest of one registration, for trust.

    The digest covers the HANDLER as well as the registration's metadata, because
    ``HookSpec.to_dict`` deliberately omits the argv (it is a receipt, and an
    argv is not a receipt). A digest built from the receipt alone therefore could
    not tell a hook whose command had been edited from one that had not — which
    would make a recorded trust decision carry over to code the user never read.
    Found by the first trust round-trip test, not by reading the code.
    """
    return hook_events.hook_digest(
        {
            **spec.to_dict(),
            "handler": {
                "kind": spec.kind,
                "command": list(spec.command),
                "url": spec.url,
                "prompt": spec.prompt_text,
            },
        }
    )


def _trust_rows(
    config: HookConfig, trust: Mapping[str, HookTrust]
) -> list[dict[str, Any]]:
    """Return the per-registration trust state for every spec, in order."""
    rows: list[dict[str, Any]] = []
    for spec in config.specs:
        recorded = trust.get(spec.id)
        digest = _spec_digest(spec)
        state = recorded.still_matches(digest) if recorded else "unrecorded"
        rows.append(
            {
                "hook_id": spec.id,
                "event": spec.event.value,
                "tier": spec.tier,
                "digest": digest,
                "trust": recorded.decision if recorded else "unrecorded",
                "trust_state": state,
                # A hook whose declaration CHANGED since it was trusted reads as
                # untrusted even though the ledger says otherwise. This is the
                # answer to "is this the code I approved".
                "trusted_now": bool(
                    recorded and recorded.trusted and state == "current"
                ),
            }
        )
    return rows


def _parse_subject_flags(tokens: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Return ``(fields, unrecognized)`` from a verb's flags.

    Accepts ``--tool X``, ``--tool=X`` and a bare first positional as an event
    name, because a person types ``/hooks run PreToolUse --tool bash`` far more
    often than the long form. An unknown flag is returned in the second value so
    the caller refuses it BY NAME instead of ignoring it â€” a silently ignored
    flag on a gate is how someone concludes a matcher does not work.
    """
    fields: dict[str, str] = {}
    unknown: list[str] = []
    items = [str(token) for token in tokens]
    index = 0
    while index < len(items):
        token = items[index]
        if token.startswith("--"):
            key = token.split("=", 1)[0]
            if key in _SUBJECT_FLAGS:
                field = _SUBJECT_FLAGS[key]
                if "=" in token:
                    fields[field] = token.split("=", 1)[1]
                elif index + 1 < len(items) and not items[index + 1].startswith("--"):
                    fields[field] = items[index + 1]
                    index += 1
                else:
                    unknown.append(token)
            else:
                unknown.append(token)
        elif "event" not in fields and not fields:
            fields["event"] = token
        elif "hook_id" not in fields:
            fields["hook_id"] = token
        else:
            unknown.append(token)
        index += 1
    return fields, unknown


def _subject_from_flags(fields: Mapping[str, str]) -> HookSubject:
    """Return a :class:`HookSubject` from the SUBJECT flags alone."""
    return HookSubject.from_value(
        {
            key: value
            for key, value in fields.items()
            if key not in _VERB_ONLY_FLAGS and key != "command_prefix"
        }
        | (
            {"command_prefix": fields["command_prefix"]}
            if fields.get("command_prefix")
            else {}
        )
    )


def _emitted(
    result: HookCommandResult, write: Optional[Callable[[str], None]]
) -> HookCommandResult:
    """Return ``result`` after writing its lines through ``write``.

    Every return path in every verb goes through this. A refusal that is built
    but never emitted is how a broken hook config becomes a command that prints
    NOTHING and exits 2 — indistinguishable from a crash to whoever is watching,
    and the exact failure this module's plain-language requirement exists to
    prevent. Centralising it means a new return path cannot forget.
    """
    emit = write if write is not None else (lambda line: print(line))
    for line in result.lines:
        emit(line)
    return result


def _usage_result(
    command: str,
    message: str,
    lines: Sequence[str],
    *,
    write: Optional[Callable[[str], None]] = None,
    **payload: Any,
) -> HookCommandResult:
    """Return the ONE refusal shape every unusable request resolves to.

    Emits its own lines: a usage error is the most common kind of message this
    surface produces and it must never be the kind that prints nothing.
    """
    return _emitted(
        HookCommandResult(
            command=command,
            status="usage_error",
            exit_code=2,
            lines=hook_lines([message, *lines]),
            payload={"error": message, **payload},
        ),
        write,
    )


def _refused_result(
    command: str,
    message: str,
    *,
    status: str = "refused",
    write: Optional[Callable[[str], None]] = None,
    extra: Sequence[str] = (),
    **payload: Any,
) -> HookCommandResult:
    """Return a REFUSAL (exit 2) with its lines emitted.

    Distinct from :func:`_usage_result` only in the status word, and it shares
    the emitting rule: nothing this surface decides is allowed to be silent.
    ``extra`` carries the follow-up lines (what to type next, where to look), so
    a refusal can still TEACH rather than only deny.
    """
    return _emitted(
        HookCommandResult(
            command=command,
            status=status,
            exit_code=2,
            lines=hook_lines([message, *extra]),
            payload={"error": message, **payload},
        ),
        write,
    )


def hooks_command(
    argv: Sequence[str],
    *,
    repo_path: Optional[str] = None,
    engine: Optional[HookEngine] = None,
    registry: Optional["HookRegistry"] = None,
    config: Optional[HookConfig] = None,
    trust: Optional[Mapping[str, HookTrust]] = None,
    prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
    egress_policy: Any = None,
    plugin_manifests: Optional[Mapping[str, Any]] = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Run one ``/hooks`` verb and return the ONE result shape.

    This is the primary implementation: the slash command and ``neo hooks`` both
    call it, so there is exactly one behaviour per verb rather than one per
    surface. ``argv`` is the verb followed by its arguments, the same words the
    user typed.

    Every failure path returns a :class:`HookCommandResult` whose ``lines`` are
    plain sentences: a hostile hook id, a broken config, and a Python traceback
    all reduce to something a person can read.
    """
    tokens = [str(token) for token in argv if str(token).strip()]
    raw = tokens[0].strip().casefold() if tokens else "list"
    verb = _HOOK_VERB_ALIASES.get(raw, raw)
    emit = write if write is not None else (lambda line: print(line))
    try:
        resolved = config
        if resolved is None:
            resolved = load_hook_config(
                repo_path=repo_path,
                prompt_renderer=prompt_renderer,
                egress_policy=egress_policy,
                plugin_manifests=plugin_manifests,
            )
    except HookConfigError as exc:
        return _refused_result(verb, f"hooks: {exc}", write=emit)
    if verb == "list":
        return hooks_list(resolved, repo_path=repo_path, trust=trust, write=emit)
    if verb == "run":
        return hooks_run(
            tokens[1:], resolved, repo_path=repo_path, engine=engine, write=emit
        )
    if verb == "test":
        return hooks_test(
            tokens[1:],
            resolved,
            repo_path=repo_path,
            trust=trust,
            prompt_renderer=prompt_renderer,
            egress_policy=egress_policy,
            write=emit,
        )
    if verb == "trust":
        return hooks_trust(tokens[1:], resolved, trust=trust, write=emit)
    if verb == "reload":
        return hooks_reload(
            tokens[1:],
            resolved,
            repo_path=repo_path,
            registry=registry,
            engine=engine,
            trust=trust,
            prompt_renderer=prompt_renderer,
            egress_policy=egress_policy,
            plugin_manifests=plugin_manifests,
            write=emit,
        )
    return _usage_result(
        verb,
        f"hooks: unknown verb {tokens[0] if tokens else ''!r}",
        ["  verbs: " + ", ".join(HOOK_VERBS)],
        write=emit,
        verbs=list(HOOK_VERBS),
    )


def hooks_list(
    config: HookConfig,
    *,
    repo_path: Optional[str] = None,
    trust: Optional[Mapping[str, HookTrust]] = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Print the merged config, the stack, the fail table and the trust state.

    The one verb a person runs before writing a hook, so it prints everything a
    hook author needs to get one right: which tiers are present and in what order
    they win, every registration with its tier / timeout / effective fail policy
    AND ITS SOURCE, the full eleven-event vocabulary with each event's declared
    class and reason, and whether each hook is trusted against the code on disk
    right now.
    """
    emit = write if write is not None else (lambda line: print(line))
    recorded = load_hook_trust() if trust is None else dict(trust)
    rows = _trust_rows(config, recorded)
    lines: list[str] = [
        f"{len(config.specs)} hook(s) Â· tiers present: "
        f"{', '.join(config.tiers_present) or 'none'} Â· layer "
        f"{'enabled' if config.enabled else 'DISABLED'}",
        "tier precedence (later wins): " + " > ".join(HOOK_TIER_PRECEDENCE),
        "",
        "hooks:",
    ]
    if not config.specs:
        lines.append("  (none declared)")
    # `rows` is built by iterating `config.specs` in the same order, so the
    # pairing is positional by construction; `strict=True` fails loudly if a
    # future edit ever builds the two independently.
    for row, spec in zip(rows, config.specs, strict=True):
        policy, source = config.fail_policy_for(spec.event, spec)
        if row["trusted_now"]:
            note = "TRUSTED"
        elif row["trust"] == "untrusted":
            note = "you marked this untrusted"
        elif row["trust_state"] == "changed":
            note = "CHANGED since you trusted it"
        else:
            note = "trust unrecorded"
        lines.append(
            f"  {spec.id} Â· {spec.event.value} Â· {spec.kind} Â· tier {spec.tier} Â· "
            f"timeout {spec.timeout_s}s Â· fail policy {policy} from {source} Â· {note}"
        )
        lines.append(f"    matcher {spec.matcher.describe()}")
    lines.append("")
    lines.append("lifecycle events (the whole vocabulary a hook may target):")
    for name in HOOK_LIFECYCLE_EVENTS:
        described = hook_events.describe_event(name)
        lines.append(
            f"  {name} Â· {described['class']} Â· {described['fail_policy']} Â· "
            f"{described['description']}"
        )
        lines.append(f"    why: {described['fail_policy_reason']}")
    if config.diagnostics:
        lines.append("")
        lines.append("diagnostics:")
        for item in config.diagnostics:
            lines.append(f"  {item.get('code', 'note')}: {item.get('detail', '')}")
    if config.sources:
        lines.append("")
        lines.append("config sources: " + "; ".join(config.sources))
    payload = {
        "config": config.to_dict(),
        "tier_precedence": list(HOOK_TIER_PRECEDENCE),
        "lifecycle_events": list(HOOK_LIFECYCLE_EVENTS),
        "event_classes": dict(hook_events.EVENT_CLASSES),
        "lifecycle_fail_policies": dict(hook_events.FAIL_POLICIES),
        "trust": rows,
        "repo": str(repo_path or ""),
    }
    rendered = hook_lines(lines)
    for line in rendered:
        emit(line)
    return HookCommandResult(
        command="list",
        status="ok",
        exit_code=0,
        lines=rendered,
        payload=payload,
        # The historical `neo hooks list --json` document was `config.to_dict()`
        # and nothing else, so that is the primary document here.
        document=config.to_dict(),
    )


def hooks_run(
    tokens: Sequence[str],
    config: HookConfig,
    *,
    repo_path: Optional[str] = None,
    engine: Optional[HookEngine] = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Fire one event through the real gate and report what it decided.

    This is ``neo hooks run``'s whole behaviour and it delegates to nothing: the
    same :meth:`HookEngine.gate` the connector surface uses, with the event's
    fail policy folded in. Exit 1 when the gate refuses, so a script can gate on
    it.
    """
    emit = write if write is not None else (lambda line: print(line))
    fields, unknown = _parse_subject_flags(tokens)
    if unknown:
        return _usage_result(
            "run",
            f"hooks run: unrecognized argument {unknown[0]!r}",
            ["  usage: hooks run <Event> [--tool T] [--path P]"],
            write=emit,
        )
    name = str(fields.get("event") or "").strip()
    if not name:
        return _usage_result(
            "run",
            "hooks run: name an event",
            ["  events: " + ", ".join(HOOK_LIFECYCLE_EVENTS)],
            write=emit,
            events=list(HOOK_LIFECYCLE_EVENTS),
        )
    try:
        event = HookEvent(name)
    except ValueError as exc:
        return _usage_result(
            "run",
            f"hooks run: {exc}",
            ["  events: " + ", ".join(HOOK_LIFECYCLE_EVENTS)],
            write=emit,
            events=list(HOOK_LIFECYCLE_EVENTS),
        )
    active = engine or HookEngine(config, repo_path=repo_path)
    gate = active.gate(event, _subject_from_flags(fields))
    verdict = "allow" if gate.allowed else "REFUSE"
    lines = [
        f"{verdict} Â· {gate.event} Â· class {event.event_class} Â· fail policy "
        f"{gate.fail_policy} from {gate.fail_policy_source} Â· "
        f"{len(gate.records)} record(s), {len(gate.failures)} failure(s)"
    ]
    if gate.reason:
        lines.append(f"  {gate.reason}")
    if gate.rewritten:
        for entry in gate.rewrite:
            parts = [
                f"{key} -> {value}"
                for key, value in sorted(entry.items())
                if key in ("command_prefix", "path") and value
            ]
            lines.append(
                f"  {entry.get('hook_id')} narrowed this call: " + ", ".join(parts)
            )
        if gate.rewritten_subject:
            lines.append(
                "  the call that will actually run: "
                + ", ".join(f"{k}={v}" for k, v in sorted(gate.rewritten_subject))
            )
    for row in gate.records:
        note = row.get("plain") or row.get("reason") or row.get("detail") or ""
        lines.append(
            f"  {row.get('hook_id')} Â· {row.get('outcome')} Â· "
            f"{row.get('decision')} Â· tier {row.get('tier')}"
            + (f" Â· {note}" if note else "")
        )
    rendered = hook_lines(lines)
    for line in rendered:
        emit(line)
    return HookCommandResult(
        command="run",
        status="ok" if gate.allowed else "refused",
        exit_code=0 if gate.allowed else 1,
        lines=rendered,
        payload=gate.to_dict(),
        # The historical `neo hooks run --json` document was the gate receipt
        # itself, at the top level. It stays there.
        document=gate.to_dict(),
    )


def hooks_test(
    tokens: Sequence[str],
    config: HookConfig,
    *,
    repo_path: Optional[str] = None,
    trust: Optional[Mapping[str, HookTrust]] = None,
    prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
    egress_policy: Any = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Run ONE hook against a synthetic event and show its REAL output.

    The verb exists because trusting a hook you have never run is the worst way
    to use one, and because "it works" is not the same question as "it does what
    I meant". It runs exactly ONE named registration â€” not the whole event â€” so
    the output on screen is that hook's own stdout, stderr, exit code and
    decision, together with the substituted argv the hook actually received.

    The synthetic subject is per-event and declared in
    :func:`hook_events.synthetic_subject`, overridable field by field, so what
    is tested is reproducible on every host. A hook whose matcher would NOT have
    selected the synthetic subject is still run, and the receipt says so,
    because "does my hook do the right thing" is a different question from
    "would it have fired" and conflating them hides both.

    Exit 0 when the hook ran, 1 when it failed, 2 when the request was
    unusable.
    """
    emit = write if write is not None else (lambda line: print(line))
    fields, unknown = _parse_subject_flags(tokens)
    if unknown:
        return _usage_result(
            "test",
            f"hooks test: unrecognized argument {unknown[0]!r}",
            ["  usage: hooks test --id <hook-id> [--event E]"],
            write=emit,
        )
    name = str(fields.get("event") or "").strip()
    try:
        event = HookEvent(name) if name else HookEvent.PRE_TOOL_USE
    except ValueError as exc:
        return _usage_result(
            "test",
            f"hooks test: {exc}",
            ["  events: " + ", ".join(HOOK_LIFECYCLE_EVENTS)],
            write=emit,
            events=list(HOOK_LIFECYCLE_EVENTS),
        )
    wanted = str(fields.get("hook_id") or "").strip()
    if wanted:
        matches = [spec for spec in config.specs if spec.id == wanted]
        if not matches:
            return _refused_result(
                "test",
                f"hooks test: no hook with id {wanted!r}",
                status="not_found",
                write=emit,
                extra=["  `hooks list` shows every declared id"],
                hook_id=wanted,
            )
        spec = matches[0]
        event = spec.event
    else:
        candidates = [item for item in config.for_event(event)]
        if not candidates:
            return _refused_result(
                "test",
                f"hooks test: no hook is registered for {event.value}",
                status="not_found",
                write=emit,
                extra=["  name one with --id, or declare one in a hooks file"],
                event=event.value,
            )
        if len(candidates) > 1:
            names = ", ".join(f"{item.id} (tier {item.tier})" for item in candidates)
            return _usage_result(
                "test",
                f"hooks test: {event.value} has {len(candidates)} hooks; name one",
                [f"  {names}"],
                write=emit,
                candidates=[item.id for item in candidates],
            )
        spec = candidates[0]
    subject = HookSubject.from_value(
        hook_events.synthetic_subject(event, **_subject_overrides(fields))
    )
    # A `test` run uses the config's OWN renderer and egress policy, because the
    # renderer and the policy are already the ones the live dispatch would use;
    # a caller that wants a different one passes a different config.
    engine = HookEngine(config, repo_path=repo_path)
    would_match = spec.matches(event, subject)
    decision, record = engine._run_one(spec, event, subject)
    recorded = load_hook_trust() if trust is None else dict(trust)
    digest = _spec_digest(spec)
    trust_state = (
        recorded[spec.id].still_matches(digest) if spec.id in recorded else "unrecorded"
    )
    ran = decision is not None
    lines = [
        f"{spec.id} Â· {event.value} Â· {spec.kind} Â· tier {spec.tier} Â· "
        f"{'ran' if ran else 'failed'}"
    ]
    argv = (
        list(_substitute(spec.command, subject, event))
        if spec.kind == "command"
        else [spec.kind]
    )
    lines.append("  argv it received: " + " ".join(argv))
    lines.append(
        "  matcher "
        + ("matches" if would_match else "does NOT match")
        + " this synthetic subject (it was run anyway)"
    )
    if decision is not None:
        lines.append(f"  decision: {decision.action.value}")
        if decision.reason:
            lines.append(f"  reason: {decision.reason}")
        if decision.additional_context:
            lines.append(
                "  additionalContext: " + decision.additional_context[:_MAX_TEST_OUTPUT]
            )
        if decision.system_message:
            lines.append(
                "  systemMessage: " + decision.system_message[:_MAX_TEST_OUTPUT]
            )
        if decision.rewrite is not None:
            lines.append(f"  rewrite: {decision.rewrite.to_dict()}")
    if record.plain:
        lines.append(f"  {record.plain}")
    if record.detail:
        lines.append(f"  detail: {record.detail[:_MAX_TEST_OUTPUT]}")
    lines.append(
        f"  exit code: {record.exit_code} Â· {record.duration_ms:.1f} ms Â· "
        f"trust: {trust_state}"
    )
    if decision is not None and decision.refused_policy_keys:
        lines.append(
            "  refused policy keys (a hook cannot widen its own authority): "
            + ", ".join(decision.refused_policy_keys)
        )
    if decision is not None and decision.refused_rewrite_keys:
        lines.append(
            "  refused rewrite (a rewrite may only narrow): "
            + "; ".join(decision.refused_rewrite_keys)
        )
    rendered = hook_lines(lines)
    for line in rendered:
        emit(line)
    return HookCommandResult(
        command="test",
        status="ok" if ran else "failed",
        exit_code=0 if ran else 1,
        lines=rendered,
        payload={
            "hook_id": spec.id,
            "event": event.value,
            "event_class": event.event_class,
            "tier": spec.tier,
            "kind": spec.kind,
            "ran": ran,
            "would_match": would_match,
            "record": record.to_dict(),
            "decision": decision.to_dict() if decision is not None else None,
            "trust": trust_state,
            "digest": digest,
            "subject": subject.to_dict(),
        },
        document={
            "hook_id": spec.id,
            "event": event.value,
            "event_class": event.event_class,
            "tier": spec.tier,
            "kind": spec.kind,
            "ran": ran,
            "would_match": would_match,
            "trust": trust_state,
            "digest": digest,
        },
    )


def _subject_overrides(fields: Mapping[str, str]) -> dict[str, str]:
    """Return the synthetic-subject overrides from the SUBJECT flags."""
    return {
        key: value
        for key, value in fields.items()
        if key
        in (
            "tool",
            "path",
            "command",
            "command_prefix",
            "mcp_server",
            "side_effect_class",
            "task_id",
            "session_id",
            "status",
        )
    }


def hooks_trust(
    tokens: Sequence[str],
    config: HookConfig,
    *,
    trust: Optional[Mapping[str, HookTrust]] = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Record or show the USER's trust decision about hooks.

    ``hooks trust`` with no arguments prints the current state of every hook
    against the digest of what is on disk, so "which of these am I actually
    running" has an answer. ``hooks trust --id X --decision trusted`` records the
    decision together with the registration's digest; editing the hook afterwards
    changes the digest and the state reads ``changed``, because a trust decision
    is about a DECLARATION and not about an id.

    A hook can DENY and ask; nothing here can ALLOW anything. Recording trust
    authorises a hook the operator could simply have declared, so this is a
    consent and bookkeeping surface and never a privilege grant.
    """
    emit = write if write is not None else (lambda line: print(line))
    fields, unknown = _parse_subject_flags(tokens)
    if unknown:
        return _usage_result(
            "trust",
            f"hooks trust: unrecognized argument {unknown[0]!r}",
            [
                "  usage: hooks trust [--id <hook-id> --decision "
                + "|".join(_TRUST_DECISIONS)
                + "]"
            ],
            write=emit,
        )
    rows = _trust_rows(config, load_hook_trust() if trust is None else dict(trust))
    wanted = str(fields.get("hook_id") or "").strip()
    if not wanted:
        lines = ["hook trust:"]
        if not rows:
            lines.append("  (no hooks declared)")
        for row in rows:
            lines.append(
                f"  {row['hook_id']} Â· {row['event']} Â· tier {row['tier']} Â· "
                f"{row['trust']} ({row['trust_state']}) Â· digest {row['digest'][:12]}"
            )
        rendered = hook_lines(lines)
        for line in rendered:
            emit(line)
        return HookCommandResult(
            command="trust",
            status="ok",
            exit_code=0,
            lines=rendered,
            payload={"trust": rows},
        )
    decision = str(fields.get("decision") or "").strip().casefold()
    if not decision:
        return _usage_result(
            "trust",
            f"hooks trust: say what you decided about {wanted!r}",
            [
                "  usage: hooks trust --id <hook-id> --decision "
                + "|".join(_TRUST_DECISIONS)
            ],
            write=emit,
        )
    known = {row["hook_id"]: row for row in rows}
    if wanted not in known:
        return _refused_result(
            "trust",
            f"hooks trust: no hook with id {wanted!r}",
            status="not_found",
            write=emit,
            extra=["  `hooks list` shows every declared id"],
            hook_id=wanted,
        )
    row = known[wanted]
    try:
        recorded = record_hook_trust(
            wanted,
            decision,
            str(row["digest"]),
            note=str(fields.get("note") or fields.get("reason") or ""),
        )
    except HookConfigError as exc:
        return _refused_result("trust", f"hooks trust: {exc}", write=emit)
    lines = [
        f"recorded: {recorded.hook_id} is {recorded.decision} "
        f"(digest {recorded.digest[:12]})"
    ]
    if not recorded.trusted:
        lines.append(
            "  an untrusted hook still runs if it is declared; this records your "
            "decision about it, it does not disable it"
        )
    if recorded.noted:
        lines.append(f"  note: {recorded.noted}")
    rendered = hook_lines(lines)
    for line in rendered:
        emit(line)
    return HookCommandResult(
        command="trust",
        status="ok",
        exit_code=0,
        lines=rendered,
        payload={"trust": recorded.to_dict()},
    )


@dataclass(frozen=True)
class HookReloadReport:
    """What changed between two reads of the hook configuration.

    The comparison is by registration digest, so an edit to a hook's argv or its
    matcher is a ``changed`` row rather than a silent replacement â€” a reload that
    only reported counts would say "2 hooks" whether or not either had been
    edited.
    """

    before: HookConfig
    after: HookConfig
    applied: bool = False

    @property
    def added(self) -> tuple[str, ...]:
        """Return the ids that appeared."""
        old = {spec.id for spec in self.before.specs}
        return tuple(spec.id for spec in self.after.specs if spec.id not in old)

    @property
    def removed(self) -> tuple[str, ...]:
        """Return the ids that disappeared."""
        new = {spec.id for spec in self.after.specs}
        return tuple(spec.id for spec in self.before.specs if spec.id not in new)

    @property
    def changed(self) -> tuple[str, ...]:
        """Return the ids whose declaration differs."""
        old = {spec.id: _spec_digest(spec) for spec in self.before.specs}
        return tuple(
            spec.id
            for spec in self.after.specs
            if spec.id in old and old[spec.id] != _spec_digest(spec)
        )

    @property
    def changed_any(self) -> bool:
        """Whether anything at all differs."""
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        """Return a one-line human summary of the delta."""
        return (
            f"{len(self.before.specs)} -> {len(self.after.specs)} registration(s); "
            f"{len(self.added)} added, {len(self.removed)} removed, "
            f"{len(self.changed)} changed"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "applied": self.applied,
            "before_count": len(self.before.specs),
            "after_count": len(self.after.specs),
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
            "summary": self.summary(),
            "tiers_present": list(self.after.tiers_present),
            "sources": list(self.after.sources),
            "diagnostics": [dict(item) for item in self.after.diagnostics],
        }


def hooks_reload(
    tokens: Sequence[str],
    config: HookConfig,
    *,
    repo_path: Optional[str] = None,
    registry: Optional["HookRegistry"] = None,
    engine: Optional[HookEngine] = None,
    trust: Optional[Mapping[str, HookTrust]] = None,
    prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
    egress_policy: Any = None,
    plugin_manifests: Optional[Mapping[str, Any]] = None,
    write: Optional[Callable[[str], None]] = None,
) -> HookCommandResult:
    """Re-register every hook without a restart, and report what changed.

    ``reload`` exists because the alternative to restarting a long session to
    pick up an edited hook is not editing hooks. It re-reads every tier, so a
    config that has become UNPARSEABLE is refused here with its reason rather
    than silently leaving the previous registrations live â€” a reload that quietly
    kept the old config would be a lie about what is loaded.

    With a :class:`HookRegistry` the registry is re-pointed at the new config,
    which is what makes the reload reach a running session; without one the verb
    still re-reads and reports, so a script gets the same answer.
    """
    emit = write if write is not None else (lambda line: print(line))
    fields, unknown = _parse_subject_flags(tokens)
    if unknown:
        return _usage_result(
            "reload",
            f"hooks reload: unrecognized argument {unknown[0]!r}",
            ["  usage: hooks reload [--repo R]"],
            write=emit,
        )
    if fields.get("repo"):
        repo_path = fields["repo"]
    try:
        fresh = load_hook_config(
            repo_path=repo_path,
            prompt_renderer=prompt_renderer,
            egress_policy=egress_policy,
            plugin_manifests=plugin_manifests,
        )
    except HookConfigError as exc:
        return _refused_result(
            "reload",
            f"hooks reload: {exc}",
            write=emit,
            extra=["  the previous registrations are still in force"],
            reloaded=False,
        )
    report = HookReloadReport(before=config, after=fresh)
    if registry is not None:
        registry.adopt(fresh)
        report = HookReloadReport(before=config, after=fresh, applied=True)
    elif engine is not None:
        engine.config = fresh
        report = HookReloadReport(before=config, after=fresh, applied=True)
    lines = [f"re-read the hook config: {report.summary()}"]
    if report.added:
        lines.append("  added: " + ", ".join(report.added))
    if report.removed:
        lines.append("  removed: " + ", ".join(report.removed))
    if report.changed:
        lines.append("  changed: " + ", ".join(report.changed))
    if not report.changed_any:
        lines.append("  nothing changed")
    lines.append(
        "  "
        + (
            "registry re-pointed at the new config"
            if report.applied
            else "no live registry to re-point; the next dispatch reads this"
        )
    )
    for item in fresh.diagnostics:
        lines.append(f"  {item.get('code', 'note')}: {item.get('detail', '')}")
    rendered = hook_lines(lines)
    for line in rendered:
        emit(line)
    return HookCommandResult(
        command="reload",
        status="ok",
        exit_code=0,
        lines=rendered,
        payload=report.to_dict(),
        document=report.to_dict(),
    )


class HookRegistry:
    """The live hook layer for one repository, re-registerable without a restart.

    A long-running session that wants hooks to take effect the moment the user
    edits them holds one of these instead of constructing a fresh
    :class:`HookEngine` per dispatch. It holds exactly one engine and one config,
    so "the hooks currently registered" is a single answer, and :meth:`reload` is
    the only thing that changes it.

    Deliberately not locking around the swap: :meth:`adopt` replaces a whole
    config reference, which is atomic under the GIL and cannot be observed
    half-applied by a concurrent dispatch. Two threads RELOADING at once is a
    caller bug and the loser simply wins, because the value is a whole config
    rather than an accumulation.
    """

    def __init__(
        self,
        *,
        repo_path: Optional[str] = None,
        config: Optional[HookConfig] = None,
        prompt_renderer: Optional[Callable[[HookSpec, HookSubject], str]] = None,
        egress_policy: Any = None,
        plugin_manifests: Optional[Mapping[str, Any]] = None,
        emit: Optional[Callable[..., None]] = None,
    ) -> None:
        self.repo_path = repo_path
        self.plugin_manifests = dict(plugin_manifests or {})
        self.prompt_renderer = prompt_renderer
        self.egress_policy = egress_policy
        self.config = (
            config
            if config is not None
            else load_hook_config(
                repo_path=repo_path,
                prompt_renderer=prompt_renderer,
                egress_policy=egress_policy,
                plugin_manifests=self.plugin_manifests,
            )
        )
        self.engine = HookEngine(
            self.config,
            repo_path=repo_path,
            prompt_renderer=prompt_renderer,
            egress_policy=egress_policy,
            emit=emit,
        )

    def adopt(self, config: HookConfig) -> HookConfig:
        """Point both the registry and its engine at a new config."""
        self.config = config
        self.engine.config = config
        return config

    def reload(self) -> HookReloadReport:
        """Re-read every tier and re-register, returning the delta.

        A config that no longer parses is REFUSED and the previous
        registrations stay in force, which is the only safe direction: the
        alternative is a broken edit silently disarming a gate mid-session.
        """
        try:
            fresh = load_hook_config(
                repo_path=self.repo_path,
                prompt_renderer=self.prompt_renderer,
                egress_policy=self.egress_policy,
                plugin_manifests=self.plugin_manifests,
            )
        except HookConfigError:
            return HookReloadReport(
                before=self.config, after=self.config, applied=False
            )
        previous = self.config
        self.adopt(fresh)
        return HookReloadReport(before=previous, after=fresh, applied=True)

    def gate(self, event: Union[HookEvent, str], subject: Any = None) -> HookGate:
        """Delegate one gate to the currently-registered engine."""
        return self.engine.gate(event, subject)
