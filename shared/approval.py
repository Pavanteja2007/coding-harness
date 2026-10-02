"""Approval integrity: canonical effects, hash binding, and stale rejection.

**R2-15 (trust) additions are in section 4 below**: the ONE
boundary-respecting command-prefix matcher, the approval-scope table with the
``global`` bypass deleted, the trust ledger that stops a session re-asking for
the same fact, and :func:`resolve_daily_trust` -- the daily path's sandbox /
approval boundary as a *receipt*, so a run cannot report a containment it is
not actually in.

The ceiling invariant is *the canonical command/effect shown for approval must
equal the effect actually executed*. That needs three mechanical pieces, and
this module owns all three so no caller re-implements a private version:

1. **Canonicalization** â€” build a stable, order-independent description of
   what an effect does (argv, working directory, environment overrides, tool
   name, target) plus a human-renderable argv line that is derived from the
   same structure the executor consumes. The render is never assembled by
   hand at a prompt site, so the thing an operator reads and the thing that
   runs cannot drift.
2. **Hash binding** â€” an approval ticket stores the digest of the canonical
   effect, not a copy of the effect. Anything the executor would treat as
   material (argv, cwd, env, tool, target) is inside the digest.
3. **Pre-execution re-check** â€” :func:`verify_before_execution` reconstructs
   the canonical effect from *live* state immediately before execution and
   compares digests. A material change yields ``stale`` and the caller must
   obtain a fresh approval; the ticket is never silently upgraded.

Assumes inputs are already validated by the caller's own policy layer; this
module is the integrity check, not the authorization decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from shared.security import redact_secrets, redact_text, safe_segment

__all__ = [
    "APPROVAL_SCOPES",
    "APPROVER_DECISIONS",
    "APPROVER_DECISION_ALIASES",
    "APPROVER_DECISION_KEYS",
    "APPROVER_FAILURES",
    "APPROVER_REASON_KEYS",
    "CONTAINMENT_AXIS",
    "DAILY_APPROVAL_SCOPE",
    "DAILY_SANDBOX_KEY",
    "DECISION_AXIS",
    "KERNEL_SANDBOX_KEY",
    "MAX_APPROVAL_SCOPE",
    "RETIRED_APPROVAL_SCOPES",
    "SAFETY_AXES",
    "ApprovalCheck",
    "ApprovalOrigin",
    "ApprovalStaleError",
    "ApprovalTicket",
    "ApproverReply",
    "ApproverRequest",
    "CanonicalEffect",
    "DailyTrust",
    "EmptyCommandPrefixError",
    "TrustGrant",
    "TrustLedger",
    "approver_admissible",
    "assert_fresh",
    "canonical_effect",
    "command_prefix_matches",
    "describe_axes",
    "effect_digest",
    "empty_prefix_refusal",
    "load_trust_ledger",
    "normalize_approval_scope",
    "parse_approver_reply",
    "read_ticket",
    "render_argv",
    "resolve_daily_trust",
    "retired_scope_note",
    "safe_ticket_name",
    "save_trust_ledger",
    "scope_is_supported",
    "tissue_ticket",
    "trust_ledger_path",
    "verify_before_execution",
    "verify_trust_applied",
    "write_ticket",
]

PathLike = Union[str, os.PathLike[str]]

# Digest domain separator. Without it, an argv list and a JSON blob that
# happen to serialize the same would produce the same approval hash.
_DIGEST_DOMAIN = "neo/effect/v1"


class ApprovalStaleError(RuntimeError):
    """Raised when an approval no longer matches the effect to be executed."""


def _canonical_value(value: Any) -> Any:
    """Return a JSON-safe, redacted, deterministically ordered projection."""

    return redact_secrets(value)


def render_argv(argv: Sequence[Any]) -> str:
    """Render an argv sequence as one copy-pasteable, quoted command line.

    Assumes ``argv`` is already the exact list the executor will spawn. The
    render is shell-quoted per argument so the displayed command and the
    executed argv describe the same thing; nothing is joined unquoted.
    """
    parts: list[str] = []
    for item in argv:
        text = "" if item is None else str(item)
        if text and not re_has_shell_meta(text):
            parts.append(text)
        else:
            parts.append("'" + text.replace("'", "'\\''") + "'")
    return " ".join(parts)


def re_has_shell_meta(text: str) -> bool:
    """Return whether an argv element needs shell quoting when displayed."""
    return any(char in text for char in " \t\n\r\"'`$&|;<>()*?!#~")


@dataclass(frozen=True)
class CanonicalEffect:
    """The single, hashable description of one unit of work.

    ``argv`` is what runs; ``render`` is derived from it by
    :func:`render_argv` so the two cannot disagree. ``digest`` covers every
    field, which is what makes a material change detectable.
    """

    tool: str
    argv: tuple[str, ...]
    working_directory: str = ""
    environment: tuple[tuple[str, str], ...] = ()
    target: str = ""
    side_effect_class: str = "read_only"
    render: str = ""
    digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible effect record."""
        return {
            "tool": self.tool,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "environment": {key: value for key, value in self.environment},
            "target": self.target,
            "side_effect_class": self.side_effect_class,
            "render": self.render,
            "digest": self.digest,
        }


def effect_digest(effect: Union[CanonicalEffect, Mapping[str, Any]]) -> str:
    """Return the stable SHA-256 digest of a canonical effect.

    Accepts either a :class:`CanonicalEffect` or its mapping form so a
    persisted ticket can be re-hashed without reconstructing the object. The
    value is computed from redacted inputs, so a ticket can be compared
    without any caller having to sanitize first.
    """
    if isinstance(effect, CanonicalEffect):
        payload = {
            "tool": effect.tool,
            "argv": list(effect.argv),
            "working_directory": effect.working_directory,
            "environment": {key: value for key, value in effect.environment},
            "target": effect.target,
            "side_effect_class": effect.side_effect_class,
        }
    else:
        payload = {
            "tool": str(effect.get("tool", "")),
            "argv": [str(item) for item in effect.get("argv", []) or []],
            "working_directory": str(effect.get("working_directory", "")),
            "environment": {
                str(key): str(value)
                for key, value in sorted(
                    dict(effect.get("environment", {}) or {}).items()
                )
            },
            "target": str(effect.get("target", "")),
            "side_effect_class": str(effect.get("side_effect_class", "")),
        }
    encoded = json.dumps(
        _canonical_value(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        f"{_DIGEST_DOMAIN}\0".encode("utf-8") + encoded.encode("utf-8")
    ).hexdigest()


def _as_argv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, os.PathLike)):
        text = str(value)
        return (text,) if text else ()
    if isinstance(value, Mapping):
        return (
            json.dumps(_canonical_value(value), sort_keys=True, ensure_ascii=False),
        )
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return (str(value),)


def _as_env(value: Any) -> tuple[tuple[str, str], ...]:
    if not value:
        return ()
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), str(item)) for key, item in value.items()))
    return ()


def canonical_effect(
    tool: str,
    argv: Any,
    *,
    working_directory: PathLike = "",
    environment: Optional[Mapping[str, Any]] = None,
    target: str = "",
    side_effect_class: str = "read_only",
) -> CanonicalEffect:
    """Build the canonical, hashable effect for one call.

    Assumes ``argv`` is the literal list the executor will use â€” the caller
    must pass the executor's own value, never a re-typed approximation. The
    returned render is produced from the same argv, so an operator approving
    the render approves the argv.
    """
    parts = _as_argv(argv)
    redacted_argv = tuple(redact_text(item) for item in parts)
    cwd = str(working_directory or "")
    payload = {
        "tool": re.sub(r"[^A-Za-z0-9_.-]+", "", str(tool or "")) or "tool",
        "argv": list(redacted_argv),
        "working_directory": cwd,
        "environment": _as_env(environment),
        "target": redact_text(str(target or "")),
        "side_effect_class": str(side_effect_class or "read_only"),
    }
    return CanonicalEffect(
        tool=payload["tool"],
        argv=redacted_argv,
        working_directory=cwd,
        environment=payload["environment"],
        target=payload["target"],
        side_effect_class=payload["side_effect_class"],
        render=render_argv(redacted_argv),
        digest=effect_digest(payload),
    )


@dataclass(frozen=True)
class ApprovalTicket:
    """A redacted approval bound to one canonical effect digest.

    ``scope``/``actor``/``issued_at``/``expires_at`` are the operator-facing
    metadata; ``effect_digest`` is the integrity binding. A ticket with no
    digest can never be created (see ``__post_init__``) â€” an unbound approval
    is a programming error, not a lenient default.
    """

    effect_digest: str
    tool: str = ""
    render: str = ""
    scope: str = "once"
    actor: str = ""
    decision: str = "approved"
    issued_at: float = 0.0
    expires_at: Optional[float] = None
    ticket_id: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.effect_digest or "").strip():
            raise ValueError("approval ticket requires an effect digest")
        if str(self.decision or "").strip().casefold() not in {
            "approved",
            "allow",
        }:
            raise ValueError("only an approved decision can produce a ticket")

    @property
    def expired(self) -> bool:
        """Return whether the ticket's expiry has passed."""
        return self.expires_at is not None and time.time() > float(self.expires_at)

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible ticket."""
        return {
            "ticket_id": self.ticket_id or "",
            "effect_digest": self.effect_digest,
            "tool": redact_text(self.tool),
            "render": redact_text(self.render),
            "scope": str(self.scope or "once"),
            "actor": redact_text(self.actor),
            "decision": str(self.decision or "approved"),
            "issued_at": round(float(self.issued_at or 0.0), 3),
            "expires_at": None
            if self.expires_at is None
            else round(float(self.expires_at), 3),
            "reason": redact_text(self.reason),
            "metadata": _canonical_value(dict(self.metadata)),
        }


def tissue_ticket(
    effect: CanonicalEffect, approved: bool = True, **fields: Any
) -> ApprovalTicket:
    """Bind a decision to a canonical effect, returning an approval ticket.

    ``effect`` must already be built by :func:`canonical_effect` so the digest
    covers exactly what will run. Refusing a non-approved decision is the
    point: there is no "ticket" for a rejection, so a rejected approval can
    never be replayed into an execution path.
    """
    if not approved:
        raise ValueError("a rejected decision does not produce an approval ticket")
    return ApprovalTicket(
        effect_digest=effect.digest,
        tool=effect.tool,
        render=effect.render,
        scope=str(fields.pop("scope", "once") or "once"),
        actor=fields.pop("actor", "") or "",
        decision="approved",
        issued_at=float(fields.pop("issued_at", time.time())),
        expires_at=fields.pop("expires_at", None),
        ticket_id=fields.pop("ticket_id", "") or f"tkt-{uuid.uuid4().hex[:16]}",
        reason=fields.pop("reason", "") or "",
        metadata=dict(fields.pop("metadata", {}) or {}),
    )


@dataclass(frozen=True)
class ApprovalCheck:
    """The result of a pre-execution integrity re-check."""

    ok: bool
    stale: bool
    reason: str
    expected_digest: str = ""
    actual_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible check record."""
        return {
            "ok": self.ok,
            "stale": self.stale,
            "reason": self.reason,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
        }


def verify_before_execution(
    ticket: Union[ApprovalTicket, Mapping[str, Any]],
    effect: Union[CanonicalEffect, Mapping[str, Any]],
    *,
    now: Optional[float] = None,
) -> ApprovalCheck:
    """Re-check an approval against the live effect immediately before running.

    This is the fail-closed half of approval integrity: the caller passes the
    effect it is *about* to execute (reconstructed from live state, not from
    whatever was shown at approval time) and gets back an explicit verdict.

    A mismatch, an expired ticket, a non-approved decision, or a missing
    digest is ``ok=False`` and ``stale=True`` â€” the caller must obtain a fresh
    approval. Never raises for a mere mismatch, so the caller can decide
    whether to re-approve or to fail the call.
    """
    if isinstance(ticket, Mapping):
        record = dict(ticket)
        expected = str(record.get("effect_digest", "") or "")
        decision = str(record.get("decision", "approved") or "approved").casefold()
        expires_at = record.get("expires_at")
    else:
        expected = str(ticket.effect_digest or "")
        decision = str(ticket.decision or "approved").casefold()
        expires_at = ticket.expires_at

    actual = effect_digest(effect)
    if decision not in {"approved", "allow"}:
        return ApprovalCheck(
            False, True, "approval decision is not approved", expected, actual
        )
    if not expected:
        return ApprovalCheck(
            False, True, "approval is not bound to an effect digest", expected, actual
        )
    if expires_at is not None and (now if now is not None else time.time()) > float(
        expires_at
    ):
        return ApprovalCheck(
            False, True, "approval expired before execution", expected, actual
        )
    if expected != actual:
        return ApprovalCheck(
            False,
            True,
            "effect changed after approval; re-approval required",
            expected,
            actual,
        )
    return ApprovalCheck(True, False, "", expected, actual)


def assert_fresh(ticket: Union[ApprovalTicket, Mapping[str, Any]], effect: Any) -> None:
    """Raise :class:`ApprovalStaleError` unless the approval still matches."""
    check = verify_before_execution(ticket, effect)
    if not check.ok:
        raise ApprovalStaleError(
            f"{check.reason} (expected={check.expected_digest[:12]} actual={check.actual_digest[:12]})"
        )


def write_ticket(path: PathLike, ticket: ApprovalTicket) -> Path:
    """Persist a redacted ticket atomically and return the path."""
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(ticket.as_dict(), sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_ticket(path: PathLike) -> Optional[dict[str, Any]]:
    """Read a persisted ticket, or None when absent/unreadable."""
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def safe_ticket_name(task_id: str, run_id: str = "") -> str:
    """Return a filesystem-safe ticket name for a task/run pair."""
    if task_id and safe_segment(task_id):
        return f"{task_id}.approval-ticket.json"
    if run_id and safe_segment(run_id):
        return f"_run-{run_id}.approval-ticket.json"
    return "approval-ticket.json"


# ---------------------------------------------------------------------------
# 4. R2-15 -- the daily path's trust boundary
#
# Everything below is ADDITIVE. Sections 1-3 above (canonicalization, hash
# binding, pre-execution re-check) are unchanged, and the historical helpers
# keep their exact signatures and output.
#
# The finding this answers: the trust asymmetry. ``neo fix`` runs sandboxed
# behind a verifier gate; the daily interactive path defaulted to
# ``approval=auto`` on LIVE HOST BASH with no sandbox, held no grant ledger, and
# its ``global`` approval scope was an UNCONDITIONAL bypass -- one grant
# defeated the entire policy. This module owns the three shared facts so no
# shell re-derives them:
#
#   * which scopes exist (``global`` is not one of them),
#   * what a command prefix covers (a boundary, and never "everything"),
#   * what containment a run is actually in (``resolve_daily_trust``).
# ---------------------------------------------------------------------------

#: The closed set of approval scopes the daily path may retain. ``global`` is
#: deliberately ABSENT: a scope that matches every call regardless of tool,
#: effect, repository, or command is not a scope, it is a switch that deletes
#: the policy. It is listed in :data:`RETIRED_APPROVAL_SCOPES` instead, and a
#: caller that asks for it is narrowed to ``once`` and told why.
APPROVAL_SCOPES: tuple[str, ...] = (
    "once",
    "exact_call",
    "session_path",
    "session_command_prefix",
)

#: Scopes that used to exist and are refused. Kept as data so a receipt, an
#: audit row, or a UI can NAME the refusal instead of silently substituting a
#: different scope.
RETIRED_APPROVAL_SCOPES: tuple[str, ...] = ("global",)

#: The widest scope the daily path will retain, and therefore the ceiling a
#: configured scope is clamped to. Retained grants are always session- or
#: repo-scoped; nothing here outlives the session unless an operator opts into
#: persistence explicitly.
MAX_APPROVAL_SCOPE = "session_command_prefix"

#: The scope a session grant learns to reuse when an operator approves a
#: command-prefix grant more than once for the same repository.
DAILY_APPROVAL_SCOPE = "session_command_prefix"

#: Operator-facing switch for the daily path's shell containment.
DAILY_SANDBOX_KEY = "daily_sandbox"

#: The key the agent kernel actually reads when it dispatches a shell call
#: (``harness/agent_kernel/strategy.py`` -> ``SafeToolBackend.execute(...,
#: sandboxed=...)``). :func:`resolve_daily_trust` reconciles the two so the
#: receipt cannot describe a containment the kernel is not in.
KERNEL_SANDBOX_KEY = "agent_process_sandboxed"

#: The refusal text an empty command prefix earns. An empty prefix matches every
#: string under a plain ``str.startswith``, so a config slip
#: (``{"command_prefix": ""}``) silently becomes a blanket grant. It is a
#: configuration error, refused with a message that says exactly that.
EMPTY_PREFIX_REFUSAL = (
    "refused: an empty command prefix would approve every command; "
    "name the command (for example 'pytest tests/') or use the 'once' scope"
)


class EmptyCommandPrefixError(ValueError):
    """Raised when a command prefix is present but empty (a config error)."""


def command_prefix_matches(command: str, prefix: str) -> bool:
    """Return whether ``command`` is covered by an approved ``prefix``.

    This is the ONE prefix matcher in the tree. It is a plain string prefix
    with a **boundary** check, so ``pytest tests/`` covers
    ``pytest tests/test_a.py`` (the prefix names a directory) while ``git sta``
    never covers ``git stash`` (the prefix stops mid-token).

    Two properties are load-bearing and both are refusal-direction:

    * **An empty prefix matches nothing.** ``"".startswith("")`` is ``True``,
      so a plain implementation turns a blank prefix into a blanket grant. Here
      a blank prefix or a blank command is ``False``, and
      :func:`empty_prefix_refusal` explains why.
    * **Matching is not authorization.** This answers "is this command inside a
      grant the operator already gave", never "should this command run". The
      caller's policy engine still decides, and a hard deny still wins.

    Assumes ``prefix`` is the operator-approved text as typed; no shell
    tokenization is performed, so a prefix that ends inside a quoted argument is
    the operator's own doing and is reported as such by
    :func:`empty_prefix_refusal`/the grant's ``command`` field.
    """
    left = str(command or "").strip()
    right = str(prefix or "").strip()
    if not left or not right:
        return False
    if not left.startswith(right):
        return False
    if len(left) == len(right):
        return True
    if right[-1].isspace() or right[-1] in "/\\":
        return True
    return left[len(right) : len(right) + 1].isspace()


def empty_prefix_refusal(value: Any) -> str:
    """Return the refusal text for a blank command prefix, else ``""``.

    Assumes ``value`` is the raw configured prefix. ``None`` means "no prefix
    was configured" and is NOT a refusal; ``""`` or whitespace means "a prefix
    was configured and it is empty", which is a configuration error.
    """
    if value is None:
        return ""
    if str(value).strip():
        return ""
    return EMPTY_PREFIX_REFUSAL


def normalize_approval_scope(value: Any) -> str:
    """Normalize an approval scope name to the closed :data:`APPROVAL_SCOPES` set.

    Unrecognised and retired names (including ``global``) narrow to ``once``,
    the narrowest scope, rather than to something convenient. Assumes the caller
    treats the result as an authorization decision input; a caller that needs to
    *report* the narrowing should pair this with :func:`retired_scope_note`.
    """
    text = str(value or "once").strip().lower().replace("-", "_")
    aliases = {
        "y": "once",
        "yes": "once",
        "call": "exact_call",
        "exact": "exact_call",
        "exact_call_id": "exact_call",
        "path": "session_path",
        "command": "session_command_prefix",
        "command_prefix": "session_command_prefix",
        "session_command": "session_command_prefix",
    }
    if text in APPROVAL_SCOPES:
        return text
    return aliases.get(text, "once")


def scope_is_supported(value: Any) -> bool:
    """Return whether a scope name resolves to a supported, non-retired scope."""
    return str(value or "").strip().lower().replace("-", "_") in set(
        APPROVAL_SCOPES
    ) | {
        "y",
        "yes",
        "call",
        "exact",
        "exact_call_id",
        "path",
        "command",
        "command_prefix",
        "session_command",
    }


def retired_scope_note(value: Any) -> str:
    """Return why a requested scope is unsupported, or ``""`` when it is fine.

    Assumes the caller wants a human-readable reason for a refusal or a
    narrowing. Never raises.
    """
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return ""
    if text in RETIRED_APPROVAL_SCOPES:
        return (
            f"scope '{text}' is not a scope: it matched every call regardless of "
            "tool, effect, repository, or command. Use once, session_path, or "
            "session_command_prefix."
        )
    if not scope_is_supported(text):
        return f"unknown approval scope '{text}'; narrowed to 'once'"
    return ""


def _clamp_scope(value: Any) -> tuple[str, str]:
    """Return ``(effective_scope, note)`` for a requested approval scope."""
    note = retired_scope_note(value)
    requested = normalize_approval_scope(value)
    order = {
        "once": 0,
        "exact_call": 1,
        "session_path": 2,
        "session_command_prefix": 3,
    }
    ceiling = order[MAX_APPROVAL_SCOPE]
    if order[requested] > ceiling:
        return MAX_APPROVAL_SCOPE, (
            note + f" widened scope '{requested}' clamped to the daily ceiling "
            f"'{MAX_APPROVAL_SCOPE}'"
        ).strip()
    return requested, note


@dataclass(frozen=True)
class TrustGrant:
    """One remembered approval: what was allowed, where, and until when.

    A grant is *evidence that an operator already said yes to this class of
    effect*, never a decision. :meth:`covers` answers the narrow question "is
    this the same fact the operator already approved"; the caller's policy engine
    still decides whether to run it, and a hard deny still wins.

    ``command_prefix`` MUST be non-empty for a ``session_command_prefix``
    grant -- an empty prefix would cover every command. A blank one is refused
    at construction (:class:`EmptyCommandPrefixError`) and additionally can
    never match (:meth:`covers`).
    """

    scope: str
    tool: str = ""
    command_prefix: str = ""
    paths: tuple[str, ...] = ()
    repo_key: str = ""
    effect_digest: str = ""
    granted_at: float = 0.0
    expires_at: Optional[float] = None
    source: str = "operator"
    grant_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", normalize_approval_scope(self.scope))
        object.__setattr__(self, "tool", str(self.tool or "").strip().lower())
        object.__setattr__(
            self, "command_prefix", str(self.command_prefix or "").strip()
        )
        object.__setattr__(
            self, "paths", tuple(str(item) for item in (self.paths or ()))
        )
        refusal = empty_prefix_refusal(self.command_prefix)
        if refusal and self.scope == "session_command_prefix":
            raise EmptyCommandPrefixError(refusal)

    @property
    def expired(self) -> bool:
        """Return whether the grant's expiry has already passed."""
        return self.expires_at is not None and time.time() > float(self.expires_at)

    def valid(self, *, now: Optional[float] = None) -> bool:
        """Return whether the grant may still be relied on at ``now``."""
        if self.expired:
            return False
        return bool(self.scope in APPROVAL_SCOPES)

    def covers(
        self,
        *,
        tool: str = "",
        command: str = "",
        paths: Sequence[str] = (),
        repo_key: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """Return whether this grant already covers the described effect.

        Assumes the caller passes the LIVE effect dimensions, not the ones shown
        at approval time -- the same discipline as :func:`verify_before_execution`.
        A grant never covers a different repository, a different tool, a path it
        does not name, or a command outside its (boundary-checked) prefix.
        """
        if not self.valid(now=now):
            return False
        if self.repo_key and repo_key and self.repo_key != repo_key:
            return False
        if self.tool and tool and self.tool != str(tool or "").strip().lower():
            return False
        if self.scope == "session_command_prefix":
            if not self.command_prefix:
                return False
            return command_prefix_matches(command, self.command_prefix)
        if self.scope == "session_path":
            wanted = {
                str(item).replace("\\", "/").lstrip("./") for item in (paths or ())
            }
            granted = {str(item).replace("\\", "/").lstrip("./") for item in self.paths}
            return bool(wanted) and wanted.issubset(granted)
        # `once` and `exact_call` are deliberately NOT retained as covering
        # grants: `once` is consumed at the decision site, and an exact-call
        # grant is bound to a call id this record does not carry. Returning
        # False keeps "remembered" distinct from "pre-approved".
        return False

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible grant record."""
        return {
            "grant_id": self.grant_id or "",
            "scope": self.scope,
            "tool": self.tool,
            "command_prefix": self.command_prefix,
            "paths": list(self.paths),
            "repo_key": self.repo_key,
            "effect_digest": self.effect_digest,
            "granted_at": round(float(self.granted_at or 0.0), 3),
            "expires_at": (
                None if self.expires_at is None else round(float(self.expires_at), 3)
            ),
            "source": self.source,
            "reason": redact_text(self.reason),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Optional["TrustGrant"]:
        """Rebuild a grant from its record form, or ``None`` when unusable.

        A record that carries a blank command prefix for a prefix-scoped grant is
        refused here rather than imported as a blanket grant: a corrupt or
        hand-edited journal must not be able to widen the policy.
        """
        data = dict(value or {})
        try:
            return cls(
                scope=data.get("scope", "once"),
                tool=data.get("tool", ""),
                command_prefix=data.get("command_prefix", ""),
                paths=tuple(data.get("paths") or ()),
                repo_key=data.get("repo_key", ""),
                effect_digest=data.get("effect_digest", ""),
                granted_at=float(data.get("granted_at") or 0.0),
                expires_at=(
                    float(data["expires_at"])
                    if data.get("expires_at") is not None
                    else None
                ),
                source=data.get("source", "operator"),
                grant_id=data.get("grant_id", ""),
                reason=data.get("reason", ""),
            )
        except (TypeError, ValueError):
            return None


class TrustLedger:
    """Session- (optionally repo-) scoped memory of approvals already given.

    The point is calibration: a user should not re-approve the same fact forever.
    Every entry stays **revocable** -- :meth:`revoke`, :meth:`revoke_tool`, and
    :meth:`forget` all remove it -- and every entry expires on its own terms.

    This object holds no policy. It records what was approved and answers
    "have I already been told yes to this?"; the caller's policy engine still
    decides, so a ledger can never turn a deny into an allow by itself.
    """

    def __init__(
        self,
        grants: Optional[Iterable[TrustGrant | Mapping[str, Any]]] = None,
        *,
        repo_key: str = "",
        session_id: str = "",
        max_grants: int = 200,
        enabled: bool = True,
    ) -> None:
        self.repo_key = str(repo_key or "")
        self.session_id = str(session_id or "")
        try:
            self.max_grants = max(1, int(max_grants))
        except (TypeError, ValueError):
            self.max_grants = 200
        self.enabled = bool(enabled)
        self.grants: list[TrustGrant] = []
        self.refusals: list[str] = []
        for grant in grants or ():
            resolved = (
                grant if isinstance(grant, TrustGrant) else TrustGrant.from_dict(grant)  # type: ignore[arg-type]
            )
            if resolved is not None:
                self.grants.append(resolved)
        self._enforce_bound()

    def _enforce_bound(self) -> None:
        """Drop the oldest entries so the ledger stays bounded."""
        overflow = len(self.grants) - self.max_grants
        if overflow > 0:
            self.grants = self.grants[overflow:]

    def record(
        self,
        *,
        scope: str,
        tool: str = "",
        command_prefix: str = "",
        paths: Sequence[str] = (),
        effect_digest: str = "",
        expires_at: Optional[float] = None,
        source: str = "operator",
        reason: str = "",
    ) -> Optional[TrustGrant]:
        """Remember one approval and return the grant, or ``None`` if refused.

        Refuses (and records the reason in :attr:`refusals`) rather than raising,
        because this is called from an approval prompt: a caller must be able to
        keep running after a bad scope answer. A refused grant returns ``None``
        so the caller can tell "remembered" from "not remembered".
        """
        if not self.enabled:
            return None
        effective, note = _clamp_scope(scope)
        if note:
            self.refusals.append(note)
        # A blank prefix is only a problem for a COMMAND-prefix grant. A path
        # grant legitimately carries no command, so applying the refusal there
        # would make every `session_path` answer silently unrememberable.
        if effective == "session_command_prefix":
            refusal = empty_prefix_refusal(command_prefix)
            if refusal:
                self.refusals.append(refusal)
                return None
        if effective == "once":
            return None
        grant = TrustGrant(
            scope=effective,
            tool=tool,
            command_prefix=command_prefix,
            paths=tuple(paths),
            repo_key=self.repo_key,
            effect_digest=effect_digest,
            granted_at=time.time(),
            expires_at=expires_at,
            source=source,
            grant_id=f"grant-{uuid.uuid4().hex[:12]}",
            reason=reason,
        )
        self.grants.append(grant)
        self._enforce_bound()
        return grant

    def covering(
        self,
        *,
        tool: str = "",
        command: str = "",
        paths: Sequence[str] = (),
        repo_key: str = "",
        now: Optional[float] = None,
    ) -> Optional[TrustGrant]:
        """Return the newest still-valid grant covering this effect, or ``None``.

        This is the calibration read: a caller that would otherwise prompt asks
        here first, and only prompts when this returns ``None``.
        """
        if not self.enabled:
            return None
        for grant in reversed(self.grants):
            if grant.covers(
                tool=tool, command=command, paths=paths, repo_key=repo_key, now=now
            ):
                return grant
        return None

    def learned_default(self, *, tool: str = "", now: Optional[float] = None) -> str:
        """Return the scope this ledger has most often learned to reuse.

        A user who answers ``c`` three times for ``git status`` has taught this
        session a default. It is only ever a *proposal* for what to pre-select
        in a prompt, never an authorization.
        """
        if not self.enabled:
            return "once"
        counts: dict[str, int] = {}
        for grant in self.grants:
            if not grant.valid(now=now):
                continue
            if tool and grant.tool and grant.tool != str(tool).strip().lower():
                continue
            counts[grant.scope] = counts.get(grant.scope, 0) + 1
        if not counts:
            return "once"
        return max(sorted(counts), key=lambda scope: counts[scope])

    def revoke(self, grant_id: str) -> bool:
        """Forget one grant by id. Returns whether anything was removed."""
        before = len(self.grants)
        self.grants = [item for item in self.grants if item.grant_id != grant_id]
        return len(self.grants) != before

    def revoke_tool(self, tool: str) -> int:
        """Forget every grant for one tool. Returns how many were removed."""
        name = str(tool or "").strip().lower()
        before = len(self.grants)
        self.grants = [item for item in self.grants if item.tool != name]
        return before - len(self.grants)

    def forget(self) -> None:
        """Forget every grant. The grant journal is the user's to take back."""
        self.grants.clear()
        self.refusals.clear()

    def prune(self, *, now: Optional[float] = None) -> int:
        """Drop expired grants and return how many were removed."""
        before = len(self.grants)
        self.grants = [item for item in self.grants if item.valid(now=now)]
        return before - len(self.grants)

    def summary(self) -> str:
        """Return one quotable line describing what this ledger remembers."""
        if not self.enabled:
            return "trust calibration: off (every approval is asked again)"
        if not self.grants:
            return "trust calibration: on, nothing remembered yet"
        scopes: dict[str, int] = {}
        for grant in self.grants:
            scopes[grant.scope] = scopes.get(grant.scope, 0) + 1
        detail = ", ".join(
            f"{count}x {scope}" for scope, count in sorted(scopes.items())
        )
        return (
            f"trust calibration: on, {len(self.grants)} grant(s) remembered ({detail})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible ledger record."""
        return {
            "schema_version": 1,
            "repo_key": self.repo_key,
            "session_id": self.session_id,
            "enabled": self.enabled,
            "max_grants": self.max_grants,
            "grants": [grant.as_dict() for grant in self.grants],
            "refusals": list(self.refusals),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, max_grants: Optional[int] = None
    ) -> "TrustLedger":
        """Rebuild a ledger from its record form, tolerating a damaged file.

        A record that is not a mapping, or a grant row that will not rebuild,
        yields an empty ledger rather than an exception: losing calibration is
        recoverable, taking down the shell is not.
        """
        data = dict(value or {}) if isinstance(value, Mapping) else {}
        grants = [
            grant
            for grant in (
                TrustGrant.from_dict(row) for row in (data.get("grants") or ())
            )
            if grant is not None
        ]
        ledger = cls(
            grants,
            repo_key=str(data.get("repo_key") or ""),
            session_id=str(data.get("session_id") or ""),
            max_grants=int(data.get("max_grants") or 200)
            if max_grants is None
            else max_grants,
            enabled=bool(data.get("enabled", True)),
        )
        ledger.refusals = [str(item) for item in (data.get("refusals") or ())]
        return ledger


def trust_ledger_path(log_root: Any, repo_key: str) -> str:
    """Return the on-disk grant-journal path for one repository.

    Assumes ``log_root`` is the session's artifact root and ``repo_key`` is the
    repository's stable key (``memory.paths.repo_key``). The path is
    harness-owned, inside the artifact root, and never inside the repository.
    """
    safe = "".join(
        char if (char.isalnum() or char in "-_") else "-"
        for char in str(repo_key or "")
    ).strip("-")
    base = Path(log_root or ".").expanduser()
    return str(base / "_trust" / f"{safe or 'repo'}.json")


def load_trust_ledger(path: Any) -> TrustLedger:
    """Load a persisted grant journal, or an empty ledger when absent/corrupt.

    Never raises: an unreadable or hand-edited journal must not be able to widen
    the policy, and it must not be able to take down a session either.
    """
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TrustLedger()
    if not isinstance(payload, dict):
        return TrustLedger()
    return TrustLedger.from_dict(payload)


def save_trust_ledger(path: Any, ledger: TrustLedger) -> Optional[str]:
    """Persist a grant journal atomically; return the path, or ``None`` on failure.

    A failure is a note, never a crash: forgetting that calibration is on disk
    costs one extra prompt, while raising would end the user's run.
    """
    target = Path(path).expanduser()
    temporary = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(ledger.to_dict(), sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return None
    return str(target)


def _truthy_opt_in(value: Any) -> Optional[bool]:
    """Return a tri-state read of a boolean-ish config value.

    ``None`` means "no opinion" (key absent or explicitly ``None``), which is
    what lets an opt-OUT switch be told apart from a merged default.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    return None


@dataclass(frozen=True)
class DailyTrust:
    """The containment a daily run is ACTUALLY in, as a receipt.

    ``sandboxed`` is what the kernel will do, not what the operator hoped for:
    when the two disagree the receipt reports the weaker boundary and names the
    conflict. That is deliberate -- a receipt that overstates containment is
    worse than no receipt, because it is the thing a user reads to decide
    whether to trust the run.
    """

    sandboxed: bool
    sandbox_opt_out: bool
    sandbox_reason: str
    approval_scope: str
    approval_scope_requested: str
    approval_notes: tuple[str, ...] = ()
    calibration: bool = True
    calibration_persist: bool = False
    repo_key: str = ""
    repo_path: str = ""
    paths_in_reach: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def unsandboxed(self) -> bool:
        """Return whether this run is executing on the live host."""
        return not self.sandboxed

    def config_patch(self) -> dict[str, Any]:
        """Return the config keys the caller MUST apply for this receipt to be true."""
        return {KERNEL_SANDBOX_KEY: self.sandboxed, DAILY_SANDBOX_KEY: self.sandboxed}

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible receipt."""
        return {
            "sandboxed": self.sandboxed,
            "sandbox_opt_out": self.sandbox_opt_out,
            "sandbox_reason": self.sandbox_reason,
            "approval_scope": self.approval_scope,
            "approval_scope_requested": self.approval_scope_requested,
            "approval_notes": list(self.approval_notes),
            "calibration": self.calibration,
            "calibration_persist": self.calibration_persist,
            "repo_key": self.repo_key,
            "repo_path": self.repo_path,
            "paths_in_reach": list(self.paths_in_reach),
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """Return the one line a human reads at the moment a run starts."""
        boundary = "sandboxed" if self.sandboxed else "UNSANDBOXED (live host)"
        return (
            f"{boundary} Â· approvals: {self.approval_scope} Â· "
            f"paths in reach: {', '.join(self.paths_in_reach) or 'none named'}"
        )

    def banner_lines(self) -> list[str]:
        """Return the receipt as display lines, loudest line first.

        The unsandboxed case leads with a warning-shaped line because it is the
        one a user must not be able to miss.
        """
        lines: list[str] = []
        if self.sandboxed:
            lines.append("boundary: sandboxed (commands run in a container)")
        else:
            lines.append(
                "boundary: UNSANDBOXED - commands run directly on this machine"
            )
            lines.append(f"reason: {self.sandbox_reason}")
            lines.append("opt out of this with daily_sandbox = true")
        lines.append(f"approvals: {self.approval_scope} (widest the session retains)")
        lines.append(
            f"paths in reach: {', '.join(self.paths_in_reach) or 'none named'}"
        )
        lines.append(
            "calibration: on - approved facts are remembered for this session"
            if self.calibration
            else "calibration: off - you will be asked for every action"
        )
        for note in self.notes:
            lines.append(f"note: {note}")
        return lines


def resolve_daily_trust(
    config: Optional[Mapping[str, Any]] = None,
    *,
    repo_path: str = "",
    repo_key: str = "",
    session_id: str = "",
) -> DailyTrust:
    """Resolve the daily path's containment from config into a receipt.

    Precedence, and it is the only precedence:

    1. ``daily_sandbox`` present and explicitly false -> UNSANDBOXED. The
       operator asked for the live host and gets it, loudly.
    2. otherwise the receipt is sandboxed UNLESS the key the kernel actually
       reads (``agent_process_sandboxed``) is present and false -- which is also
       an explicit opt-out, because a caller that sets that key to false is
       asking for the host.
    3. when the two keys disagree, the receipt reports the WEAKER boundary and
       names the conflict. A receipt must never describe a containment the
       kernel is not in.

    Absent, ``None``, and unparseable values all mean "no opinion", and "no
    opinion" means the safe default (sandboxed). An unparseable value can
    therefore never *disable* a boundary by accident.

    Assumes ``config`` is the run's resolved config -- typically
    ``harness.config.get_config`` output, which already merges ``DEFAULTS``. That
    is why the opt-out is a value check on an explicitly-written ``False`` and
    not a truthiness check: a merged default and a deliberate choice must be
    distinguishable. The caller MUST apply :meth:`DailyTrust.config_patch` to the
    config it hands the run; :func:`verify_trust_applied` exists so it can prove
    it did.
    """
    values = dict(config or {})
    notes: list[str] = []
    requested_daily = values.get(DAILY_SANDBOX_KEY)
    kernel_value = values.get(KERNEL_SANDBOX_KEY)
    daily = _truthy_opt_in(requested_daily)
    kernel = _truthy_opt_in(kernel_value)
    if requested_daily is not None and daily is None:
        notes.append(
            f"unrecognised {DAILY_SANDBOX_KEY}={requested_daily!r}; "
            "treated as no opinion, so the sandbox stays on"
        )
    if kernel_value is not None and kernel is None:
        notes.append(
            f"unrecognised {KERNEL_SANDBOX_KEY}={kernel_value!r}; "
            "treated as no opinion, so the sandbox stays on"
        )

    opt_out = False
    reason = ""
    if daily is False:
        opt_out = True
        reason = f"{DAILY_SANDBOX_KEY}=false was set explicitly"
    elif daily is True and kernel is False:
        opt_out = True
        reason = (
            f"{DAILY_SANDBOX_KEY}=true but {KERNEL_SANDBOX_KEY}=false; the "
            "weaker boundary is reported, not the requested one"
        )
    elif daily is None and kernel is False:
        opt_out = True
        reason = f"{KERNEL_SANDBOX_KEY}=false was set explicitly"
    sandboxed = not opt_out

    requested_scope = values.get("approval_scope", MAX_APPROVAL_SCOPE)
    scope, scope_note = _clamp_scope(requested_scope)
    approval_notes = tuple(item for item in (scope_note,) if item)
    calibration = _truthy_opt_in(values.get("approval_calibration"))
    persist = _truthy_opt_in(values.get("approval_calibration_persist"))
    if calibration is None:
        calibration = True
    if persist is None:
        persist = False
    reach = (str(repo_path),) if repo_path else ()
    return DailyTrust(
        sandboxed=sandboxed,
        sandbox_opt_out=opt_out,
        sandbox_reason=reason,
        approval_scope=scope,
        approval_scope_requested=str(requested_scope or ""),
        approval_notes=approval_notes,
        calibration=calibration,
        calibration_persist=bool(persist),
        repo_key=str(repo_key or ""),
        repo_path=str(repo_path or ""),
        paths_in_reach=reach,
        notes=tuple(notes),
    )


def verify_trust_applied(
    trust: DailyTrust, config: Optional[Mapping[str, Any]] = None
) -> str:
    """Return ``""`` when the config really is in the boundary the receipt claims.

    Assumes ``trust`` came from :func:`resolve_daily_trust` and ``config`` is
    the mapping that will actually be handed to the run. A non-empty return names
    the mismatch, and a caller that renders the receipt should render this too:
    a receipt printed beside a config that disagrees is a lie with a timestamp.
    """
    if not isinstance(trust, DailyTrust):
        return "no trust receipt was resolved"
    values = dict(config or {})
    kernel = _truthy_opt_in(values.get(KERNEL_SANDBOX_KEY))
    if trust.sandboxed and kernel is False:
        return (
            f"receipt claims a sandbox but {KERNEL_SANDBOX_KEY}=false will run "
            "commands on the host"
        )
    if trust.unsandboxed and kernel is not False:
        return (
            f"receipt reports an unsandboxed run but {KERNEL_SANDBOX_KEY}="
            f"{values.get(KERNEL_SANDBOX_KEY)!r} will sandbox it"
        )
    return ""


# ---------------------------------------------------------------------------
# 5. AGT-07 -- TWO AXES, AND AN APPROVER THAT FAILS CLOSED
#
# Everything above is one axis. Section 4 made the DECISION axis honest
# (no `global` bypass, no blank prefix, a receipt for the run's boundary).
# It could not make the other axis honest, because there was no shape for it:
# "it did not ask" and "it could do anything" were the same sentence, so a
# reader of any receipt had to guess whether a run was contained.
#
# This section adds the three shared facts, and nothing else:
#
#   * the two AXIS NAMES, so a receipt can name both and never let one
#     stand in for the other (:data:`CONTAINMENT_AXIS` /
#     :data:`DECISION_AXIS` / :func:`describe_axes`);
#   * the ORIGIN of a request (:class:`ApprovalOrigin`) -- a subagent must
#     not be able to escalate past the human watching the main thread, so
#     the escalation carries the thread it came from;
#   * the approver's REPLY CONTRACT (:func:`parse_approver_reply`), which
#     is fail-closed in the refusal direction for every shape that is not
#     an unambiguous, reasoned approval.
#
# The model call itself is NOT here. This module is the bottom layer: it owns
# what a decision MEANS and refuses to invent one. `harness/approver.py` owns
# asking the question.
# ---------------------------------------------------------------------------

#: Axis (a): what the sandbox PERMITS. Technical containment -- no network
#: unless declared, `.git` and config directories read-only inside a writable
#: root, writable roots declared. A value the sandbox itself reports.
CONTAINMENT_AXIS = "containment"

#: Axis (b): whether to PROMPT. A policy decision -- allow / ask / deny. A
#: value the policy engine reports.
DECISION_AXIS = "decision"

#: The closed set of axes a safety receipt may carry. A receipt that grows a
#: third axis has to say so here first: a second axis is how "it asked" starts
#: looking like "it was contained".
SAFETY_AXES: tuple[str, ...] = (CONTAINMENT_AXIS, DECISION_AXIS)

#: The only two answers an approver may return. Everything else is a FAILURE,
#: and every failure denies.
APPROVER_DECISIONS: tuple[str, ...] = ("approve", "deny")

#: Accepted spellings for each canonical decision. The approve set is
#: deliberately small and every member is an explicit word a reviewer could
#: have typed; there is no "looks fine", no "probably ok", and no empty string.
APPROVER_DECISION_ALIASES: dict[str, str] = {
    "approve": "approve",
    "approved": "approve",
    "allow": "approve",
    "allowed": "approve",
    "permit": "approve",
    "yes": "approve",
    "y": "approve",
    "deny": "deny",
    "denied": "deny",
    "deny_it": "deny",
    "refuse": "deny",
    "refused": "deny",
    "reject": "deny",
    "rejected": "deny",
    "block": "deny",
    "blocked": "deny",
    "no": "deny",
    "n": "deny",
}

#: JSON keys read for the decision, in priority order. A reviewer model is
#: asked for ``decision``; the others are read so a differently-shaped
#: scripted reply is still understood rather than silently denied.
APPROVER_DECISION_KEYS: tuple[str, ...] = ("decision", "verdict", "action", "result")

#: JSON keys read for the reasoning. The brief requires a reason WITH the
#: verdict, and :func:`parse_approver_reply` enforces it: an approval with no
#: stated reason is malformed, and malformed denies.
APPROVER_REASON_KEYS: tuple[str, ...] = (
    "reason",
    "rationale",
    "justification",
    "explanation",
    "why",
)

#: Every way a review can FAIL. All of them deny. The set is closed so a
#: receipt can carry the reason and a consumer can refuse an unknown one
#: instead of defaulting to "approved".
APPROVER_FAILURES: tuple[str, ...] = (
    "",  # "" means the reply parsed; the absence of a failure IS the proof
    "empty",
    "unparseable",
    "ambiguous",
    "malformed",
    "no_reason",
    "timeout",
    "model_error",
    "not_privileged",
    "disabled",
)


@dataclass(frozen=True)
class ApprovalOrigin:
    """WHO is asking for approval, including the thread it descends from.

    This is the field that stops an escalation from laundering itself. A
    subagent's privileged request is shown to the human (and to the approver
    model) with the main thread named, so "a subagent decided to do this" can
    never be read as "a human decided to do this".

    ``thread_id`` is the run's root thread; ``subagent_id`` is the spawned
    worker, if any; ``parent_id`` is whatever spawned it. An origin with a
    ``subagent_id`` and no ``thread_id`` names an escalation whose origin
    cannot be established -- :attr:`provenance_complete` is False, and
    :func:`approver_admissible` denies on it.
    """

    thread_id: str = ""
    subagent_id: str = ""
    parent_id: str = ""
    label: str = ""

    @property
    def is_subagent(self) -> bool:
        """Return whether this request came from a spawned subagent."""
        return bool(str(self.subagent_id or "").strip())

    @property
    def provenance_complete(self) -> bool:
        """Return whether the escalation can be traced to a watching thread."""
        if not self.is_subagent:
            return bool(str(self.thread_id or "").strip())
        return bool(str(self.thread_id or "").strip()) and bool(
            str(self.parent_id or "").strip()
        )

    def describe(self) -> str:
        """Return the one line an approval overlay shows as WHO is asking.

        Never collapses a subagent into "the agent": an unnamed or
        untraceable subagent says so in the text rather than reading like the
        main thread.
        """
        thread = str(self.thread_id or "").strip() or "unattributed"
        sub = str(self.subagent_id or "").strip()
        parent = str(self.parent_id or "").strip()
        label = str(self.label or "").strip()
        if not sub:
            named = f"main thread {thread}"
            return f"{named} ({label})" if label else named
        detail = f"subagent {sub}"
        if parent:
            detail += f" spawned by {parent}"
        detail += f", escalating to main thread {thread}"
        if label:
            detail += f" ({label})"
        if not self.provenance_complete:
            detail += " [origin not fully traceable]"
        return detail

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible origin record."""
        return {
            "thread_id": redact_text(self.thread_id),
            "subagent_id": redact_text(self.subagent_id),
            "parent_id": redact_text(self.parent_id),
            "label": redact_text(self.label),
            "is_subagent": self.is_subagent,
            "provenance_complete": self.provenance_complete,
        }


@dataclass(frozen=True)
class ApproverRequest:
    """The exact thing an approver is shown, and nothing else.

    ``effect`` is the canonical effect the executor will run, so the approver
    reads the same argv/cwd/env the operator approves -- not a summary
    written by a prompt site. ``requires_human`` records that the action
    ALREADY needs a human: an approver model is a second opinion on a
    privileged action, never a way to widen what reaches a human, and
    :func:`approver_admissible` refuses a request that is not already
    privileged.

    ``containment`` is the OTHER axis, carried as data so the approver can
    reason about what the sandbox would allow. It is informational: an
    approver's approval never widens containment, and containment never
    decides whether to prompt.
    """

    effect: CanonicalEffect
    origin: ApprovalOrigin = field(default_factory=ApprovalOrigin)
    requires_human: bool = False
    privileged_reason: str = ""
    containment: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible request record."""
        return {
            "effect": self.effect.as_dict(),
            "effect_digest": self.effect.digest,
            "origin": self.origin.to_dict(),
            "requires_human": bool(self.requires_human),
            "privileged_reason": redact_text(self.privileged_reason),
            "containment": _canonical_value(dict(self.containment or {})),
        }

    def summary(self) -> str:
        """Return the one-line description a human reads in the overlay."""
        return (
            f"{self.origin.describe()} wants to {self.effect.tool}: "
            f"{self.effect.render or ' '.join(self.effect.argv) or '(no command)'}"
        )


def approver_admissible(request: ApproverRequest) -> tuple[bool, str]:
    """Return whether an approver model may be asked about this request.

    Two refusals, both in the narrowing direction:

    * the action does not already require a human. An approver agent that
      could be consulted for unprivileged work would be a way to ADD a
      gate, and a cheap model gate is not a human gate; the reviewer decides
      what a human is asked about, never the reviewer.
    * the request is a subagent escalation whose origin cannot be traced to
      a thread. An untraceable escalation is exactly the case where a human
      watching the main thread is being asked to approve something that did
      not come from where the overlay says it came from.

    Assumes ``request`` is already built from a canonical effect; this
    function judges admissibility only, never the effect itself.
    """
    if not isinstance(request, ApproverRequest):
        return False, "no approver request was supplied"
    if not request.requires_human:
        return (
            False,
            "the approver agent only judges actions that already require a "
            "human; this one does not",
        )
    if request.origin.is_subagent and not request.origin.provenance_complete:
        return (
            False,
            "refused: a subagent escalation whose origin cannot be traced to "
            "a watching thread; the overlay would misreport who asked",
        )
    return True, ""


@dataclass(frozen=True)
class ApproverReply:
    """One approver verdict, with the failure that produced it (if any).

    ``approved`` is the only field a caller may branch on, and it is True only
    for a parsed, reasoned approval. A verdict that merely *failed* is never
    approved, so a caller that ignores ``failure`` entirely still denies --
    the failure mode of a missing check is the safe direction by construction.
    """

    decision: str = "deny"
    reason: str = ""
    failure: str = ""
    raw: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    origin: ApprovalOrigin = field(default_factory=ApprovalOrigin)

    def __post_init__(self) -> None:
        decision = str(self.decision or "deny").strip().casefold()
        failure = str(self.failure or "").strip()
        if failure not in set(APPROVER_FAILURES):
            raise ValueError(f"unknown approver failure: {failure!r}")
        if not failure and decision not in APPROVER_DECISIONS:
            raise ValueError(
                f"an approver decision must be one of {APPROVER_DECISIONS!r}"
            )
        if not failure and not str(self.reason or "").strip():
            raise ValueError("an approver approval must state a reason")
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "failure", failure)

    @property
    def approved(self) -> bool:
        """Return whether this reply is an approval. A failure is never one."""
        return self.decision == "approve" and not self.failure

    @property
    def failed(self) -> bool:
        """Return whether the reply failed rather than decided."""
        return bool(self.failure)

    @classmethod
    def failed_verdict(
        cls, failure: str, raw: str = "", **fields: Any
    ) -> "ApproverReply":
        """Return a DENY carrying ``failure``. This cannot approve.

        A named constructor rather than a bare call, because a refusal with a
        reason and a refusal that could not be OBTAINED are different
        artifacts, and a receipt that collapses them hides exactly the case
        this round exists for.
        """
        if failure not in set(APPROVER_FAILURES) or not failure:
            raise ValueError(f"refusing to build a failure verdict for {failure!r}")
        return cls(
            decision="deny",
            reason="",
            failure=failure,
            raw=redact_text(str(raw or "")),
            **fields,
        )

    def with_origin(self, origin: ApprovalOrigin) -> "ApproverReply":
        """Return the same verdict carrying ``origin`` (the escalation label)."""
        return ApproverReply(
            decision=self.decision,
            reason=self.reason,
            failure=self.failure,
            raw=self.raw,
            model=self.model,
            elapsed_s=self.elapsed_s,
            origin=origin,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted, JSON-compatible verdict record.

        Safe to journal: both the raw reply and the reasoning are redacted, so
        an auditor can reconstruct WHY a privileged action was refused without
        the journal re-publishing a secret the command happened to contain.
        """
        return {
            "approved": self.approved,
            "decision": self.decision,
            "reason": redact_text(self.reason),
            "failure": self.failure,
            "raw": redact_text(self.raw),
            "model": redact_text(self.model),
            "elapsed_s": round(float(self.elapsed_s or 0.0), 3),
            "origin": self.origin.to_dict(),
        }

    def summary(self) -> str:
        """Return the one line a human reads under the approval overlay."""
        who = self.origin.describe()
        if self.approved:
            return (
                f"approver agent ({self.model or 'unset model'}) APPROVED for "
                f"{who}: {self.reason}"
            )
        if self.failure:
            return (
                f"approver agent DENIED for {who}: the reply could not be used "
                f"({self.failure}); a reply that cannot be read is a refusal"
            )
        return f"approver agent DENIED for {who}: {self.reason}"


def _decision_token(value: Any) -> str:
    """Normalize one decision word, or return "" when it is not one."""
    text = str(value or "").strip().casefold()
    text = text.strip("*_`'\"[]{}<>~ \t\r\n")
    text = text.rstrip(".!?:;, ")
    return APPROVER_DECISION_ALIASES.get(text, "")


def _json_object_from_text(text: str) -> Optional[dict[str, Any]]:
    """Return the first balanced JSON object in ``text``, else ``None``.

    A reviewer model is asked for JSON and will sometimes wrap it in prose or
    a fence; extracting the first *balanced* object keeps that usable. Only a
    dict is accepted -- ``"approve"`` on its own is a string, and the caller
    falls through to the text form where a bare reason-less word is refused
    for having no reason.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        end = -1
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end != -1:
            try:
                payload = json.loads(text[start : end + 1])
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return payload
        start = text.find("{", start + 1)
    return None


def _strip_fences(text: str) -> str:
    """Remove a surrounding markdown fence, if the whole reply is one."""
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _reason_from_payload(payload: Mapping[str, Any]) -> str:
    for key in APPROVER_REASON_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return redact_text(value.strip())
    return ""


def parse_approver_reply(text: Any) -> ApproverReply:
    """Parse an approver model's reply into an explicit verdict, fail-closed.

    The load-bearing property is the refusal direction: **anything that is not
    an unambiguous, reasoned approval denies.** Concretely, a reply is an
    approval only when it names a decision in the approve set AND states a
    non-empty reason. Every other shape returns ``decision="deny"`` with a
    ``failure`` from :data:`APPROVER_FAILURES`:

    * nothing, or only whitespace -> ``empty``;
    * no recognisable decision word -> ``unparseable``;
    * approve-ish and deny-ish words together, or two JSON keys that
      disagree -> ``ambiguous``;
    * a decision word that is not in either set -> ``malformed``;
    * an approval with no reason -> ``no_reason``.

    This is the whole reason a malformed reply is a refusal rather than an
    absence: an approver that fails open is not an approver, and a parser
    that returned "no decision" would have to be treated as "yes" by every
    caller that forgot to check.

    Never raises. ``text`` may be any object; a non-string is reported as
    unparseable rather than coerced.
    """
    raw = "" if text is None else (text if isinstance(text, str) else str(text))
    cleaned = _strip_fences(raw)
    if not cleaned.strip():
        return ApproverReply.failed_verdict("empty", raw)

    payload = _json_object_from_text(cleaned)
    if payload is not None:
        found: list[str] = []
        for key in APPROVER_DECISION_KEYS:
            if key not in payload:
                continue
            token = _decision_token(payload.get(key))
            if token:
                found.append(token)
        if not found:
            return ApproverReply.failed_verdict("malformed", raw)
        if len(set(found)) > 1:
            return ApproverReply.failed_verdict("ambiguous", raw)
        reason = _reason_from_payload(payload)
        if not reason:
            return ApproverReply.failed_verdict("no_reason", raw)
        return ApproverReply(
            decision=found[0], reason=reason, failure="", raw=redact_text(cleaned)
        )

    # Text form. The decision must be the FIRST word of the first non-empty
    # line, so a reply that merely mentions "deny" somewhere while agreeing
    # ("looks fine, I would not deny this") cannot be read as a refusal, and
    # a reply that never commits is unparseable rather than approved.
    words: set[str] = set()
    for token in re.findall(r"[A-Za-z_]+", cleaned):
        resolved = _decision_token(token)
        if resolved:
            words.add(resolved)
    if len(words) > 1:
        return ApproverReply.failed_verdict("ambiguous", raw)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ApproverReply.failed_verdict("empty", raw)
    head = lines[0]
    # Zero or more leading NON-word characters (a quote, an emphasis marker, a
    # bullet) then the first word. A first word that is not a decision word is
    # a reply that never committed, which is `unparseable` -- not a denial
    # carried by a later "deny" somewhere in the prose.
    first = re.match(r"^[^A-Za-z0-9_]*([A-Za-z_]+)", head)
    decision = _decision_token(first.group(1)) if first else ""
    if not decision:
        return ApproverReply.failed_verdict("unparseable", raw)
    if not words:
        return ApproverReply.failed_verdict("malformed", raw)
    remainder = head[first.end() :] if first else ""
    remainder = remainder.lstrip(" \t:,-—–.")
    reason = " ".join([remainder, *lines[1:]]).strip()
    if not reason:
        return ApproverReply.failed_verdict("no_reason", raw)
    return ApproverReply(
        decision=decision,
        reason=redact_text(reason),
        failure="",
        raw=redact_text(cleaned),
    )


def describe_axes(
    *,
    containment: Optional[Mapping[str, Any]] = None,
    decision: str = "",
    containment_known: bool = True,
) -> str:
    """Return one line that names BOTH axes and never lets one imply the other.

    This is the sentence a receipt, a banner, or a CLI line uses, so the
    phrasing is owned here rather than written per call site. Three rules it
    enforces in the text itself:

    * containment is described from CONTAINMENT data, never from the
      decision -- so "no prompt was needed" can never be rendered as
      "nothing could have gone wrong";
    * an unknown containment is said to be unknown, and is never replaced by
      the decision's wording;
    * a decision is described as a DECISION, never as evidence of
      containment.

    Assumes ``containment`` is the sandbox's own receipt mapping. A missing
    or empty mapping with ``containment_known=True`` is still rendered as
    unknown, because a claim nobody can check is not a claim.
    """
    if not containment_known or not containment:
        left = "containment: UNKNOWN (not reported by the sandbox)"
    else:
        enabled = bool(containment.get("network_enabled"))
        readonly = tuple(containment.get("readonly_applied") or ())
        writable = tuple(containment.get("writable_roots") or ())
        left = (
            "containment: "
            f"{'network ON (declared)' if enabled else 'network off'}; "
            f"read-only inside the writable root: "
            f"{', '.join(readonly) if readonly else 'none applied'}; "
            f"writable roots: {', '.join(writable) if writable else 'whole workspace'}"
        )
    action = str(decision or "").strip().casefold()
    right = (
        f"decision: {action} (whether to prompt -- says nothing about containment)"
        if action
        else "decision: not evaluated (says nothing about containment)"
    )
    return f"{left}; {right}"
