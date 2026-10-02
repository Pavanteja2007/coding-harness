"""The lifecycle EVENT VOCABULARY the declarative hook layer targets.

This module exists because plugin authors target an event NAME, and the set of
names a plugin may target is a contract. It is therefore DATA, declared once,
here, with no dependency on any other module: ``extensions.user_hooks`` reads
it, the ``/hooks`` surface reads it, and a plugin author reads it.

Three things are decided here and nowhere else:

1. **The names.** :data:`LIFECYCLE_EVENTS` is the closed vocabulary.
2. **The class each event belongs to** — :data:`EVENT_CLASSES`. A class, not a
   per-event opinion, is what a fail policy is derived from, so a new event
   cannot inherit a policy by accident: it must be given a class, and the
   class decides.
3. **What happens when a hook for that event cannot run** —
   :data:`FAIL_POLICY_BY_CLASS` folded into :data:`FAIL_POLICIES`, with a
   written reason per event in :data:`FAIL_POLICY_REASONS`.

The rule behind the table, stated so a future event cannot argue with it: a
hook that raised, timed out, is missing its executable, or returned unusable
output **has not approved anything**. For an event whose whole purpose is to
gate a decision, treating that silence as consent is the one failure mode a
hook layer must not have, so a GATING class is fail-closed. For an event that
can only add context or suppress output, failing closed would turn a red
logger into a red run for no security benefit, so OBSERVATIONAL and
LIFECYCLE are fail-open.

``PreToolUse`` and ``Stop`` are the two fail-closed events and they are
fail-closed because they ARE the two gates. :data:`FAIL_CLOSED_EVENTS` names
them so a test can pin the property directly rather than re-deriving it.

Nothing here imports ``cli`` or ``harness``: a vocabulary module that could
reach into the product would stop being a contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

__all__ = [
    "EVENT_CLASSES",
    "EVENT_CLASS_NAMES",
    "EVENT_DESCRIPTIONS",
    "FAIL_CLOSED_EVENTS",
    "FAIL_POLICIES",
    "FAIL_POLICY_REASONS",
    "FAIL_POLICY_VALUES",
    "FAIL_POLICY_VALUES_BY_CLASS",
    "GATING",
    "LEGACY_EVENTS",
    "LIFECYCLE_EVENTS",
    "LIFECYCLE_EVENT_SET",
    "OBSERVATIONAL",
    "OBSERVATIONAL_EVENTS",
    "HookEventConfigError",
    "describe_event",
    "event_class",
    "fail_policy_for",
    "fail_policy_projection",
    "fail_policy_reason",
    "hook_digest",
    "is_observational",
    "normalize_event",
    "plugin_hook_document",
    "synthetic_subject",
]

#: The three classes. ``gating`` events may refuse a decision; the other two
#: can never change one no matter what a hook says.
GATING = "gating"
OBSERVATIONAL = "observational"
LIFECYCLE = "lifecycle"

EVENT_CLASS_NAMES: tuple[str, ...] = (GATING, OBSERVATIONAL, LIFECYCLE)

FAIL_POLICY_VALUES: tuple[str, ...] = ("fail_closed", "fail_open")

#: The only two policies a class may resolve to. A class absent from this table
#: is a bug, not a default — see :func:`fail_policy_for`.
FAIL_POLICY_VALUES_BY_CLASS: dict[str, str] = {
    GATING: "fail_closed",
    OBSERVATIONAL: "fail_open",
    LIFECYCLE: "fail_open",
}

#: Every event name the hook layer fires, in lifecycle order. This is the whole
#: surface a plugin author may target; an event that is not here cannot be
#: registered, fired, or listed.
LIFECYCLE_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
)

LIFECYCLE_EVENT_SET: frozenset[str] = frozenset(LIFECYCLE_EVENTS)

#: The events the ORIGINAL product vocabulary declared, kept as an exact tuple
#: because shipped code, the ``neo hooks`` JSON document and several existing
#: suites read ``extensions.user_hooks.HOOK_EVENTS`` and expect these seven and
#: only these. The four added names are the same kind of event with the same
#: grammar; they are reached through :data:`LIFECYCLE_EVENTS`.
LEGACY_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "PreCompact",
    "SessionEnd",
)

EVENT_CLASSES: dict[str, str] = {
    "SessionStart": LIFECYCLE,
    "UserPromptSubmit": GATING,
    "PreToolUse": GATING,
    "PostToolUse": OBSERVATIONAL,
    "PostToolUseFailure": OBSERVATIONAL,
    "Notification": OBSERVATIONAL,
    "Stop": GATING,
    "SubagentStart": LIFECYCLE,
    "SubagentStop": LIFECYCLE,
    "PreCompact": OBSERVATIONAL,
    "SessionEnd": LIFECYCLE,
}

OBSERVATIONAL_EVENTS: tuple[str, ...] = tuple(
    name for name in LIFECYCLE_EVENTS if EVENT_CLASSES[name] == OBSERVATIONAL
)

#: Named so a test can assert the security property without re-deriving it.
FAIL_CLOSED_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "Stop",
    "UserPromptSubmit",
)

#: One sentence per event, saying what firing it MEANS. Written for a plugin
#: author reading a config file, and shown by ``/hooks list`` so a user can
#: tell two similarly named events apart without reading source.
EVENT_DESCRIPTIONS: dict[str, str] = {
    "SessionStart": (
        "a session is beginning; fires once per session, before any tool runs"
    ),
    "UserPromptSubmit": (
        "the user submitted a message and the run has not started; it can add "
        "context or refuse the prompt"
    ),
    "PreToolUse": "a tool call is about to be dispatched; this is the gate on it",
    "PostToolUse": "a tool call succeeded; observational, can add context or suppress output",
    "PostToolUseFailure": "a tool call failed; observational, it reports and does not gate",
    "Notification": (
        "the shell is about to notify the user (bell, toast); observational, it "
        "can add context to the message"
    ),
    "Stop": (
        "a run is about to report a terminal status; this is the gate on completion"
    ),
    "SubagentStart": "a subagent is about to begin; lifecycle bookkeeping, nothing is gated",
    "SubagentStop": "a subagent has finished; lifecycle bookkeeping, nothing is gated",
    "PreCompact": "conversation history is about to be compacted; observational, harness bookkeeping",
    "SessionEnd": "a session is ending; lifecycle bookkeeping, teardown reports",
}

FAIL_POLICY_REASONS: dict[str, str] = {
    "SessionStart": (
        "lifecycle bookkeeping: nothing is gated, so a missing or broken "
        "setup hook must not stop the session from starting"
    ),
    "UserPromptSubmit": (
        "this event gates the user's own prompt before the run starts, so a "
        "hook that could not run has not cleared the prompt and the prompt is "
        "not accepted by an absent verdict"
    ),
    "PreToolUse": (
        "this event IS the gate on a tool call; a hook that could not run has "
        "not approved the call, so the call is refused rather than allowed by "
        "an absent verdict"
    ),
    "PostToolUse": (
        "observational: it can add context and suppress output but can never "
        "change a decision, so failing closed would only turn a red hook into "
        "a red run"
    ),
    "PostToolUseFailure": (
        "observational: the tool already failed; the hook reports, it does not gate"
    ),
    "Notification": (
        "observational: the message has already been composed, so a hook can "
        "add context to it but cannot refuse it"
    ),
    "Stop": (
        "this event gates completion, so a hook that could not run has not "
        "cleared the run and the run may not report completed_verified"
    ),
    "SubagentStart": (
        "lifecycle bookkeeping: the subagent is dispatched by the caller, and "
        "a broken announcement hook must not stop it from starting"
    ),
    "SubagentStop": (
        "lifecycle bookkeeping: the subagent has already finished, so a broken "
        "teardown hook can only report"
    ),
    "PreCompact": (
        "observational: compaction is the harness's own bookkeeping, not a gate"
    ),
    "SessionEnd": "lifecycle bookkeeping: teardown reports, it does not gate",
}

#: Derived once, at import, from :data:`EVENT_CLASSES` — so the table and the
#: class map cannot disagree, and a new event added to one without the other
#: fails a test rather than shipping a default.
FAIL_POLICIES: dict[str, str] = {
    name: FAIL_POLICY_VALUES_BY_CLASS[EVENT_CLASSES[name]] for name in LIFECYCLE_EVENTS
}

#: Spellings people and plugin manifests actually use. A plugin that writes
#: ``pre_tool_use`` gets ``PreToolUse`` rather than a config error, because a
#: hook layer that refuses a snake_case spelling teaches people that hooks are
#: untrustworthy.
_EVENT_ALIASES: dict[str, str] = {
    "sessionstart": "SessionStart",
    "session_start": "SessionStart",
    "start": "SessionStart",
    "session_started": "SessionStart",
    "begin": "SessionStart",
    "userpromptsubmit": "UserPromptSubmit",
    "user_prompt_submit": "UserPromptSubmit",
    "prompt_submit": "UserPromptSubmit",
    "user_prompt": "UserPromptSubmit",
    "before_prompt": "UserPromptSubmit",
    "pretooluse": "PreToolUse",
    "pre_tool_use": "PreToolUse",
    "before_tool": "PreToolUse",
    "tool_before": "PreToolUse",
    "pre_tool": "PreToolUse",
    "posttooluse": "PostToolUse",
    "post_tool_use": "PostToolUse",
    "after_tool": "PostToolUse",
    "tool_after": "PostToolUse",
    "post_tool": "PostToolUse",
    "posttoolusefailure": "PostToolUseFailure",
    "post_tool_use_failure": "PostToolUseFailure",
    "tool_failure": "PostToolUseFailure",
    "tool_error": "PostToolUseFailure",
    "post_tool_failure": "PostToolUseFailure",
    "notification": "Notification",
    "notify": "Notification",
    "before_notification": "Notification",
    "notify_user": "Notification",
    "stop": "Stop",
    "completion": "Stop",
    "pre_mint": "Stop",
    "before_completion": "Stop",
    "subagentstart": "SubagentStart",
    "subagent_start": "SubagentStart",
    "before_subagent": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "subagentstop": "SubagentStop",
    "after_subagent": "SubagentStop",
    "subagent_end": "SubagentStop",
    "precompact": "PreCompact",
    "pre_compact": "PreCompact",
    "before_compact": "PreCompact",
    "compact": "PreCompact",
    "sessionend": "SessionEnd",
    "session_end": "SessionEnd",
    "end": "SessionEnd",
    "session_ended": "SessionEnd",
}

_SUBJECT_DEFAULTS: dict[str, dict[str, str]] = {
    "SessionStart": {"tool": "session", "side_effect_class": "read"},
    "UserPromptSubmit": {"tool": "prompt", "path": "", "side_effect_class": "read"},
    "PreToolUse": {
        "tool": "edit",
        "path": "src/example.py",
        "command": "git status --short",
        "command_prefix": "git status",
        "side_effect_class": "mutation",
    },
    "PostToolUse": {
        "tool": "edit",
        "path": "src/example.py",
        "status": "ok",
        "side_effect_class": "mutation",
    },
    "PostToolUseFailure": {
        "tool": "edit",
        "path": "src/example.py",
        "status": "failed",
        "side_effect_class": "mutation",
    },
    "Notification": {
        "tool": "notify",
        "status": "waiting for you",
        "side_effect_class": "read",
    },
    "Stop": {
        "tool": "run",
        "status": "completed_unverified",
        "result_summary": "target test not run",
        "side_effect_class": "read",
    },
    "SubagentStart": {"tool": "subagent", "side_effect_class": "read"},
    "SubagentStop": {
        "tool": "subagent",
        "status": "completed_unverified",
        "side_effect_class": "read",
    },
    "PreCompact": {"tool": "compact", "side_effect_class": "read"},
    "SessionEnd": {"tool": "session", "side_effect_class": "read"},
}

_MAX_SUBJECT_FIELD = 512


class HookEventConfigError(ValueError):
    """Raised when a value cannot be resolved to a lifecycle event name.

    A hook layer that guesses an unknown event name is a hook layer that
    silently runs nothing, so an unresolvable name is an error and the message
    names the vocabulary.
    """


def normalize_event(value: Any) -> str:
    """Return the canonical event name for any spelling a person may type.

    Accepts a canonical name, the snake_case and kebab forms, and the short
    aliases a plugin author is likely to reach for. Raises
    :class:`HookEventConfigError` for anything else, naming the vocabulary —
    an unknown event must never read as "no hooks configured".
    """
    raw = str(value or "").strip()
    if raw in LIFECYCLE_EVENT_SET:
        return raw
    normalized = raw.replace("-", "_").replace(".", "_").replace(" ", "_").casefold()
    resolved = _EVENT_ALIASES.get(normalized)
    if resolved is not None:
        return resolved
    raise HookEventConfigError(
        f"unknown hook event {value!r}; the events are: {', '.join(LIFECYCLE_EVENTS)}"
    )


def event_class(name: Any) -> str:
    """Return the declared class for one event.

    Raises :class:`HookEventConfigError` for an event that is not in the
    vocabulary, and :class:`RuntimeError` for a name in the vocabulary with no
    declared class. The second case is a programming error in this module, and
    making it loud is the point: it is how "a new event inherited 'open' by
    accident" is prevented rather than merely discouraged.
    """
    resolved = normalize_event(name)
    declared = EVENT_CLASSES.get(resolved)
    if declared is None:
        raise RuntimeError(
            f"hook event {resolved!r} has no declared class; add it to "
            "EVENT_CLASSES (and its reason to FAIL_POLICY_REASONS) rather than "
            "letting it inherit a fail policy"
        )
    return declared


def is_observational(name: Any) -> bool:
    """Return whether an event can never change a decision.

    True exactly for the OBSERVATIONAL class, which is the structural
    "an observational event can never decide" rule expressed as data.
    """
    return event_class(name) == OBSERVATIONAL


def fail_policy_for(name: Any) -> str:
    """Return the declared fail policy for one event.

    Derived from :data:`EVENT_CLASSES` rather than restated, so the class map
    and the policy map cannot drift. An event whose class is missing raises
    rather than defaulting — that is the whole reason the class exists.
    """
    return FAIL_POLICY_VALUES_BY_CLASS[event_class(name)]


def fail_policy_reason(name: Any) -> str:
    """Return the written reason for one event's declared fail policy."""
    resolved = normalize_event(name)
    reason = FAIL_POLICY_REASONS.get(resolved, "")
    if not reason:
        raise RuntimeError(
            f"hook event {resolved!r} has no documented fail-policy reason; a "
            "policy without a reason is a policy nobody can argue with later"
        )
    return reason


def fail_policy_projection(names: tuple[str, ...]) -> dict[str, str]:
    """Return the fail policy for a SUBSET of events, in the order given.

    Used for the backward-compatible projections in
    ``extensions.user_hooks``, which must keep exposing a seven-event table
    while the vocabulary has eleven. Derived, never a second copy.
    """
    return {name: fail_policy_for(name) for name in names}


def describe_event(name: Any) -> dict[str, Any]:
    """Return the machine-readable description of one event.

    Every field a surface needs to teach the vocabulary without reading
    source: the name, its class, its fail policy, why, and what firing it
    means.
    """
    resolved = normalize_event(name)
    return {
        "event": resolved,
        "class": EVENT_CLASSES[resolved],
        "fail_policy": fail_policy_for(resolved),
        "fail_policy_reason": fail_policy_reason(resolved),
        "description": EVENT_DESCRIPTIONS.get(resolved, ""),
        "legacy": resolved in LEGACY_EVENTS,
    }


def synthetic_subject(event: Any, **overrides: Any) -> dict[str, Any]:
    """Return a synthetic :class:`~extensions.user_hooks.HookSubject` mapping.

    ``/hooks test`` runs ONE hook against a synthetic event so an operator can
    try it before trusting it, and a synthetic subject is what makes that
    possible without a real tool call. The defaults are per-event and declared
    here so the thing a user sees is the same thing on every host; any field
    may be overridden, and an unknown override key is refused rather than
    silently dropped (the same rule the matcher vocabulary applies).

    Returns a plain mapping. ``extensions.user_hooks.HookSubject`` is
    deliberately not imported: this module is the vocabulary, and the
    vocabulary does not depend on the dispatcher that consumes it.
    """
    resolved = normalize_event(event)
    subject: dict[str, Any] = dict(_SUBJECT_DEFAULTS.get(resolved, {}))
    allowed = set(subject) | {
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
        "metadata",
    }
    for key, value in overrides.items():
        name = str(key)
        if name not in allowed:
            raise HookEventConfigError(
                f"synthetic {resolved} subject has no field {name!r}; "
                "expected any of " + ", ".join(sorted(allowed))
            )
        subject[name] = value if name == "metadata" else str(value)[:_MAX_SUBJECT_FIELD]
    subject.setdefault("task_id", "hooks-test")
    subject.setdefault("session_id", "hooks-test-session")
    return subject


# ---------------------------------------------------------------------------
# Plugin hooks: a plugin's registrations join the user's, they never replace
# them
# ---------------------------------------------------------------------------


def plugin_hook_document(
    plugin_name: str, manifest: Any
) -> tuple[Optional[dict[str, Any]], tuple[dict[str, str], ...]]:
    """Return ``(document, diagnostics)`` for one plugin's declared hooks.

    A plugin declares hooks in its manifest under ``"hooks"`` using the SAME
    grammar as a hook-tier file::

        {"hooks": {"PreToolUse": [{"id": "no-secrets", "type": "command", ...}]}}

    Every id is **namespaced** to ``plugin:<plugin>:<id>``. That is the whole
    security property of the hooks stack: the tier merge replaces a
    registration whose id collides, so a plugin that declares the id of a
    user's hook would otherwise silently REPLACE it. Namespacing makes that
    collision unreachable rather than merely unlikely, and the namespaced id
    is what appears in every receipt, so a reader can tell whose hook ran.

    A malformed declaration is a **diagnostic, never an exception**: an
    installed plugin must not be able to break the session that merely loads
    it. The returned document is ``None`` when the plugin declared nothing
    usable.
    """
    diagnostics: list[dict[str, str]] = []
    label = str(plugin_name or "").strip()
    if not label:
        diagnostics.append(
            {
                "code": "plugin_unnamed",
                "detail": "a plugin hook declaration arrived without a plugin name",
            }
        )
        return None, tuple(diagnostics)
    if not isinstance(manifest, Mapping):
        diagnostics.append(
            {
                "code": "plugin_hooks_unreadable",
                "detail": f"plugin {label}: manifest is not an object",
            }
        )
        return None, tuple(diagnostics)
    declared = manifest.get("hooks")
    if declared is None:
        return None, tuple(diagnostics)
    if not isinstance(declared, Mapping):
        diagnostics.append(
            {
                "code": "plugin_hooks_unreadable",
                "detail": f"plugin {label}: 'hooks' must be an object keyed by event name",
            }
        )
        return None, tuple(diagnostics)

    out: dict[str, Any] = {}
    refused_policy_keys = [
        key
        for key in ("fail_policy", "default_fail_policy")
        if manifest.get(key) is not None
    ]
    if refused_policy_keys:
        # A plugin declares HOOKS. It does not get to declare the fail POLICY:
        # the policy is a security property of the EVENT, and a value a plugin
        # could write would let that plugin opt itself out of the gate it is
        # subject to. The keys are dropped here — before the document is built —
        # so nothing downstream can read them either, and the attempt is
        # recorded so it is visible rather than merely ignored.
        diagnostics.append(
            {
                "code": "plugin_fail_policy_refused",
                "detail": (
                    f"plugin {label} may not declare a fail policy; the event's "
                    "declared policy applies. ignored: "
                    + ", ".join(refused_policy_keys)
                ),
            }
        )
    for raw_event, entries in declared.items():
        try:
            event = normalize_event(raw_event)
        except HookEventConfigError as exc:
            diagnostics.append(
                {
                    "code": "plugin_hook_unknown_event",
                    "detail": f"plugin {label}: {exc}",
                }
            )
            continue
        if isinstance(entries, Mapping):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            diagnostics.append(
                {
                    "code": "plugin_hook_entries_unreadable",
                    "detail": f"plugin {label}: hooks.{event} must be a list of hook objects",
                }
            )
            continue
        kept: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                diagnostics.append(
                    {
                        "code": "plugin_hook_entry_unreadable",
                        "detail": f"plugin {label}: hooks.{event}[{index}] is not an object",
                    }
                )
                continue
            item = {str(key): value for key, value in entry.items()}
            raw_id = str(item.get("id") or f"{event.lower()}-{index}").strip()
            item["id"] = f"plugin:{label}:{raw_id}" if raw_id else ""
            kept.append(item)
        if kept:
            out.setdefault(event, []).extend(kept)
    if not out:
        return None, tuple(diagnostics)
    return {"schema_version": 1, "hooks": out}, tuple(diagnostics)


def hook_digest(payload: Any) -> str:
    """Return a stable digest of one hook registration.

    Used by ``/hooks trust``: a trust decision is about a specific
    declaration, so editing the declaration must invalidate it. The digest is
    over a canonical JSON rendering with sorted keys, so two hosts that loaded
    the same hook agree on its identity.
    """
    try:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(sorted(_stringify(payload)))
    return hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()


def _stringify(value: Any) -> list[str]:
    """Return a sorted list of printable strings for an unserializable value."""
    if isinstance(value, Mapping):
        return [f"{key}={_stringify(item)}" for key, item in sorted(value.items())]
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(_stringify(item) for item in value)
    return str(value)
