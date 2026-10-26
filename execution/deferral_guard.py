"""Wave 2: the deferral boundary is ENFORCED here, and an attack is run at it.

Why this module exists at all
----------------------------
Wave 1 built the vocabulary (``execution/tool_deferral.py``) and asserted its
properties. Its own module docstring said, in those words::

    * It is not wired into ``execute()``. It is the vocabulary and the proof
      surface for a seam that T1 owns.

**That sentence is a finding, not a caveat.** Three of this round's ten
deferral attacks are escalation shapes, and every one of them walks straight
through a seam that does not exist:

============================  ==============================================
attack                        what a funnel with no gate does
============================  ==============================================
row 2, execute a WITHHELD     ``execute()`` reads ``policy_for_tool(name)``.
tool directly                 ``edit`` is a REAL catalog tool, so a
                              ``patch_only`` run's withheld ``edit``
                              executes the moment an approver says yes. The
                              only thing holding it is the operator
                              noticing, which is not a mechanism.
row 8, swap the model's       ``harness.tool_surface.surface_for`` derives
family mid-session            the withheld set from ``model=``. A swap is
                              therefore a *derivation*, and a derivation
                              that removes a withheld tool is a widening.
row 10, a search result       discovery returns a NAME. Nothing binds the
used without validation       name it was found under to a validation.
============================  ==============================================

So Wave 2's job is not another assertion. It is three mechanisms plus one
driver that attacks them:

1. :func:`pin_surface` produces an **immutable, session-scoped** surface and
   :func:`check_surface` refuses a withheld or capability-gated name at the
   dispatch. ``SafeToolBackend.execute`` consults it (additively, and only when
   a pin was supplied).
2. :func:`swap_surface` makes a mid-session re-derivation **narrow-only**. A
   model swap may make a session *less* capable; it may never make it more
   capable. This is the anti-escalation direction and it is stated as a rule
   rather than as an omission.
3. :class:`DescriptionPin` and :class:`SkillFilePin` pin content by digest and
   **report a change**. A tool whose description changes between the moment a
   user approved it and the moment it executes is the case nobody plans for,
   and "we re-read it" is exactly the bug: the user approved one thing and got
   another.

And :mod:`execution.deferral_attack` runs the three tables.

The asymmetry this module keeps
-------------------------------
**A bug in a surface decision must make a tool invisible or refused, never
visible and never permitted.** Every rule below is written in that direction,
and the reasons are asymmetric in their consequences: over-refusal costs a
turn, while a discoverable withheld tool costs the guarantee the whole phase
exists to establish.

What this module deliberately does NOT do
-----------------------------------------
* It does not decide what the model is SHOWN. ``harness.tool_surface`` owns the
  per-model mapping; ``mcp_server.tool_pinning`` owns the deferred MCP
  catalog. This module consumes both through :func:`execution.tool_deferral`.
* It does not grant, widen, or revoke a permission. :func:`validate_actual_call`
  checks a call against the run's own tool set and against nothing a skill
  declared.
* It does not re-derive a policy. ``policy_for_tool`` and
  ``ToolPolicyEngine`` remain the only authorities for what a call is allowed
  to do; this module decides only whether the tool exists for this session.
"""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from execution import tool_deferral as _vocab
from execution.workspace import (
    ToolPolicyEngine,
    ToolResult,
)

__all__ = [
    "CALL_STAGES",
    "DEFERRED_JOURNAL_KIND",
    "MID_RUN_ESCALATIONS",
    "SURFACE_REFUSALS",
    "SURFACE_SWAP_RULES",
    "CallValidation",
    "DescriptionChange",
    "DescriptionPin",
    "EscalationRefusal",
    "ExecutionPathTrace",
    "SkillFileChange",
    "SkillFilePin",
    "SurfacePin",
    "SurfaceRefusal",
    "SurfaceSwap",
    "approval_prompt_fields",
    "attach_surface_state",
    "check_description",
    "check_skill_file",
    "check_surface",
    "content_digest",
    "module_source",
    "nested_skill_tools",
    "pin_description",
    "pin_skill_file",
    "pin_surface",
    "refuses_mid_run_escalation",
    "surface_journal_row",
    "swap_surface",
    "trace_execution_path",
    "validate_actual_call",
    "withheld_refusal_sentence",
]


# ---------------------------------------------------------------------------
# 0. The one ordered funnel, named once
# ---------------------------------------------------------------------------

#: The stages a typed call passes through, in order. This is a CLOSED tuple and
#: it is the claim the round makes: a call's authorisation is a funnel, and a
#: funnel can be compared. ``surface_resolution`` appears for EVERY call,
#: visible or deferred, because a visible tool is also looked up - looking up
#: and finding "visible" is the same stage as looking up and finding
#: "deferred", and a funnel that skipped the lookup for the easy case would be
#: a funnel whose shape depends on the answer.
#:
#: ``skill_invocation`` is present for every call and reports whether a skill
#: asked for it. It is NOT a gate: :func:`validate_actual_call` runs against
#: the run's own tool set regardless of what any skill declared, which is the
#: whole point of the stage being present in both arms.
CALL_STAGES: Tuple[str, ...] = (
    "surface_resolution",
    "policy_lookup",
    "revision_binding",
    "policy_evaluation",
    "approval",
    "containment",
    "dispatch",
)

#: The journal row kind that records the surface state. Additive: it is a new
#: kind, not a new field on an existing row, so a reader that does not know it
#: is unaffected and a reader that wants a post-hoc audit can find it by name.
DEFERRED_JOURNAL_KIND = "tool_surface_state"

#: Refusal kinds. A CLOSED vocabulary, because a refusal a caller has to parse
#: as free text is a refusal the caller will parse wrongly.
SURFACE_REFUSALS: Tuple[str, ...] = (
    # The tool exists in the catalog and this session withheld it. This is the
    # escalation shape: withholding is not "hidden", it is absent.
    "withheld_tool",
    # The tool is deferred, and this session's capability set does not include
    # it. Deferred-but-denied is still absent as far as execution is concerned.
    "capability_gated",
    # The name carries no state in the pin. An unrecognised name must never be
    # treated as visible - that is the same fail-open this file exists to close.
    "unknown_surface_state",
    # The name in the pin has a state this build does not implement.
    "unrepresentable_surface_state",
)

#: What a mid-session surface re-derivation is allowed to do. ``"narrow_only"``
#: is the rule and it is asymmetric on purpose.
SURFACE_SWAP_RULES: Tuple[str, ...] = ("narrow_only", "reject_unknown_state")

#: Mid-run escalation attempts a skill or a model could make. Each is refused
#: for the same reason: a run's permissions are decided at resolution, not
#: renegotiated while a turn is in flight.
MID_RUN_ESCALATIONS: Tuple[str, ...] = (
    "skill_requests_new_tool_after_start",
    "skill_requests_new_permission_after_start",
    "model_swap_after_start",
    "capability_widened_after_start",
)


def content_digest(value: Any) -> str:
    """Return a SHA-256 hex digest of ``value``'s UTF-8 text.

    ``shared.security.stable_digest`` is MD5-length, which is the right answer
    for a cache key and the wrong answer for an INTEGRITY pin: this digest is
    what says "the text the user approved is the text that executed", and a
    pin whose collisions an attacker can seek is not a pin. Both digests are
    computed over ``str(value)``, so a non-string description and the string a
    reader would see pin identically.
    """
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# 1. The surface pin: an immutable session surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceRefusal:
    """Why a name is not executable in this session, in one value."""

    tool: str
    state: str
    kind: str
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "surface_state": self.state,
            "refusal_kind": self.kind,
            "refusal": self.detail,
        }


def withheld_refusal_sentence(tool: str, source: str) -> str:
    """The refusal text a caller and a user both read.

    It NAMES the state and the authority that decided it, because "refused"
    without either is the receipt a reader works around. It does NOT say the
    tool does not exist - that would be a lie the caller could not verify, and
    an attacker-supplied name would be indistinguishable from a real one.
    """
    return (
        f"tool {tool!r} is WITHHELD for this session by {source or 'the surface'} "
        f"and is refused at the call. Withheld is absent, not hidden: there is no "
        f"execution path, no approval path, and no discovery path for it. This is "
        f"not an error you can approve around."
    )


@dataclass(frozen=True)
class SurfacePin:
    """A session's surface, decided ONCE and then frozen.

    **Frozen is the security property.** :func:`harness.tool_surface.surface_for`
    takes a ``model`` and derives the withheld set from it, so a caller that
    re-derives mid-session can widen. A pin records the model, the provider and
    the capability that decided it and never consults them again.

    The pin carries NO method that can change it. ``withheld``, ``deferred`` and
    ``visible`` are tuples computed at construction; ``state_of`` is a lookup.
    """

    ledger: _vocab.SurfaceLedger
    model: str = ""
    provider: str = ""
    capability: str = ""
    session_id: str = ""
    surface_source: str = "deferral_guard"
    notes: Tuple[str, ...] = ()

    # -- state -------------------------------------------------------------
    @property
    def withheld(self) -> Tuple[str, ...]:
        return self.ledger.withheld

    @property
    def deferred(self) -> Tuple[str, ...]:
        return self.ledger.deferred

    @property
    def visible(self) -> Tuple[str, ...]:
        return self.ledger.visible

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(record.name for record in self.ledger.records)

    def state_of(self, name: str) -> str:
        """One tool's state, or ``""`` for a name this pin does not carry."""
        return self.ledger.state_of(name)

    @property
    def digest(self) -> str:
        """A digest over the DECISION, so a pin can be compared to another.

        Deliberately over the sorted state assignment rather than over the
        input sets: two pins that resolved the same states from different
        inputs are the same decision, and a reader comparing pins wants that
        answer.
        """
        payload = "\n".join(
            f"{record.name}={record.state}" for record in self.ledger.records
        )
        return content_digest(f"{payload}\ncapability={self.capability}")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "capability": self.capability,
            "session_id": self.session_id,
            "surface_source": self.surface_source,
            "states": {
                state: len(self.ledger.names(state))
                for state in _vocab.SURFACE_STATES
            },
            "visible": list(self.visible),
            "deferred": list(self.deferred),
            "withheld": list(self.withheld),
            "conflicts": list(self.ledger.conflicts),
            "digest": self.digest,
            "notes": list(self.ledger.notes) + list(self.notes),
        }


def pin_surface(
    *,
    catalog: Optional[Sequence[str]] = None,
    deferred: Optional[Sequence[str]] = None,
    withheld: Optional[Sequence[str]] = None,
    model: str = "",
    provider: str = "",
    config: Optional[Mapping[str, Any]] = None,
    session_id: str = "",
) -> SurfacePin:
    """Decide this session's surface, ONCE, and freeze it.

    Three inputs, in this order, and the order is the argument:

    1. ``catalog`` - the tools that exist. Read through
       ``execution.workspace._TOOL_POLICIES`` when omitted, so a tool added to
       the catalog is covered the day it lands rather than the day somebody
       remembers this list.
    2. ``withheld`` - the run's own declaration, PLUS the per-model withheld set
       from ``harness.tool_surface`` when the caller supplied ``model``/
       ``config``. The union is used deliberately: a caller that names its own
       withheld set must not be able to *drop* the model's set by omitting it.
    3. ``deferred`` - reachability, not permission.

    :func:`execution.tool_deferral.resolve_surface` does the resolution, so this
    module owns no second implementation of "a name in both sets is a
    conflict". That function resolves an overlap to ``withheld`` and publishes
    the conflict, which is the direction that cannot widen.
    """
    from execution import workspace as _ws

    names = _clean(catalog if catalog is not None else tuple(_ws._TOOL_POLICIES))
    declared_withheld = _clean(withheld)
    model_withheld: Tuple[str, ...] = ()
    capability = ""
    if model or config:
        model_withheld = _clean(
            _vocab.builtin_withheld_names(config=config, model=model, provider=provider)
        )
        capability = str((config or {}).get("tool_surface_capabilities") or "")
    ledger = _vocab.resolve_surface(
        catalog=names,
        deferred=deferred,
        withheld=tuple(declared_withheld) + model_withheld,
        withheld_source="run_declaration+tool_surface" if model_withheld else "run_declaration",
    )
    return SurfacePin(
        ledger=ledger,
        model=str(model or ""),
        provider=str(provider or ""),
        capability=capability,
        session_id=str(session_id or ""),
    )


def check_surface(pin: Optional[SurfacePin], tool: str) -> Optional[SurfaceRefusal]:
    """Return a refusal when ``tool`` may not be executed in this session.

    ``None`` means "the pin has no opinion" - either because there is no pin,
    which is the historical behaviour and is byte-identical to Wave 1, or
    because the name is in a state that IS executable. **Every other outcome is
    a refusal**, including the ones nobody has a name for yet: an unrecognised
    state and a name the pin does not carry both refuse, because the
    fail-open direction is the one this phase exists to close.

    A **deferred** tool is NOT refused. Deferred is a visibility change, and
    refusing it would make deferral a permission change - the exact confusion
    the exit gate exists to rule out. Its execution still goes through
    ``policy_for_tool``, ``ToolPolicyEngine`` and approval, identically to a
    visible tool's; see :func:`trace_execution_path`.
    """
    if pin is None:
        return None
    name = str(tool or "").strip().lower()
    if not name:
        return SurfaceRefusal(
            tool=name,
            state=_vocab.SURFACE_WITHHELD,
            kind="withheld_tool",
            detail=(
                "an empty tool name carries no state and is refused; a refusal "
                "that treated blank as visible would be a refusal a typo could "
                "walk through"
            ),
        )
    state = pin.state_of(name)
    if state == _vocab.SURFACE_WITHHELD:
        return SurfaceRefusal(
            tool=name,
            state=state,
            kind="withheld_tool",
            detail=withheld_refusal_sentence(name, pin.surface_source),
        )
    if state not in _vocab.SURFACE_STATES:
        return SurfaceRefusal(
            tool=name,
            state=state or "",
            kind="unknown_surface_state",
            detail=(
                f"tool {name!r} carries no surface state in this session's pin "
                f"(known states: {list(_vocab.SURFACE_STATES)}). An unrecognised "
                f"state is refused rather than treated as visible."
            ),
        )
    if state == _vocab.SURFACE_DEFERRED:
        # Deferred is reachable. There is no refusal here and there must not be
        # one: the whole claim of this round is that deferral changed WHEN the
        # model saw the tool, not WHAT it may do.
        return None
    return None


# ---------------------------------------------------------------------------
# 2. Row 8: a model swap may only NARROW
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceSwap:
    """The verdict on a mid-session re-derivation of the surface."""

    accepted: bool
    requested_model: str
    requested_provider: str
    pinned_model: str
    pinned_provider: str
    would_unhold: Tuple[str, ...] = ()
    would_hold: Tuple[str, ...] = ()
    rule: str = "narrow_only"
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "pinned_model": self.pinned_model,
            "pinned_provider": self.pinned_provider,
            "would_unhold": list(self.would_unhold),
            "would_hold": list(self.would_hold),
            "rule": self.rule,
            "reason": self.reason,
        }


def swap_surface(
    pin: SurfacePin,
    *,
    model: str = "",
    provider: str = "",
    config: Optional[Mapping[str, Any]] = None,
) -> SurfaceSwap:
    """Decide whether a mid-session model/provider swap may change the surface.

    **The rule is ``narrow_only`` and it is one-directional: a swap may make a
    session LESS capable and never more.** The comparison is over the two
    withheld SETS, not over the capability string, because the capability is
    the input and the set is the decision:

    * a new derivation that withholds a SUPERSET of the pin's set is
      **accepted** - the session got stricter, which is the recoverable
      direction;
    * a new derivation that withholds a SUBSET is **refused** and the
      difference is published in ``would_unhold``, because accepting it would
      be exactly the escalation this row exists to catch;
    * an identical set is accepted and changes nothing.

    The honest gap, which this function's docstring is not allowed to hide:
    this refuses a *widening the caller asked for*. It cannot refuse a
    widening the caller never announces, and it says so in the posture
    document rather than in a comment.
    """
    pinned_withheld = set(pin.withheld)
    derived = pin_surface(
        catalog=pin.names,
        model=model,
        provider=provider,
        config=config,
        session_id=pin.session_id,
    )
    derived_withheld = set(derived.withheld)
    would_unhold = tuple(sorted(pinned_withheld - derived_withheld))
    would_hold = tuple(sorted(derived_withheld - pinned_withheld))
    if would_unhold:
        return SurfaceSwap(
            accepted=False,
            requested_model=str(model or ""),
            requested_provider=str(provider or ""),
            pinned_model=pin.model,
            pinned_provider=pin.provider,
            would_unhold=would_unhold,
            would_hold=would_hold,
            reason=(
                f"the surface this swap would produce withholds "
                f"{list(would_unhold)}, which the pinned surface withholds too, "
                f"so the swap UNLOCKS it. A mid-session surface may only narrow "
                f"(rule 'narrow_only'); the pin resolved for model "
                f"{pin.model or 'unset'!r} and is not re-derived. Refused."
            ),
        )
    return SurfaceSwap(
        accepted=True,
        requested_model=str(model or ""),
        requested_provider=str(provider or ""),
        pinned_model=pin.model,
        pinned_provider=pin.provider,
        would_hold=would_hold,
        reason=(
            f"the swap withholds a superset of the pinned surface "
            f"(+{list(would_hold)}), which is the narrowing direction and is "
            f"accepted; the pin's own states are unchanged."
            if would_hold
            else (
                "the swap withholds exactly the pinned set; nothing changes and "
                "nothing is unlocked."
            )
        ),
    )


# ---------------------------------------------------------------------------
# 3. Row 10: a description is PINNED, and a change is REPORTED
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DescriptionPin:
    """One description's bytes at the moment a human could have approved them."""

    name: str
    digest: str
    chars: int
    source: str
    blocked: bool
    tainted: bool
    severity: str = "none"
    review_digest: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "digest": self.digest,
            "chars": int(self.chars),
            "source": self.source,
            "blocked": bool(self.blocked),
            "tainted": bool(self.tainted),
            "severity": self.severity,
            "review_digest": self.review_digest,
            "note": self.note,
        }


@dataclass(frozen=True)
class DescriptionChange:
    """Whether a description still says what it said when it was pinned.

    ``changed`` is the whole receipt. There is deliberately no
    ``accept_if_same``-style escape: a caller that wants to re-pin reads
    ``current_digest`` and re-runs whatever approval it ran before. **A pin
    that could be silently refreshed is not a pin.**
    """

    name: str
    changed: bool
    pinned_digest: str
    current_digest: str
    prior_chars: int
    current_chars: int
    verdict: str = "unchanged"
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "changed": bool(self.changed),
            "pinned_digest": self.pinned_digest,
            "current_digest": self.current_digest,
            "prior_chars": int(self.prior_chars),
            "current_chars": int(self.current_chars),
            "verdict": self.verdict,
            "note": self.note,
        }


def pin_description(
    name: str,
    description: Any,
    *,
    source: str = _vocab.DESCRIPTION_SOURCE,
    max_chars: int = _vocab.DEFERRED_DESCRIPTION_CHARS,
    policy: Optional[Any] = None,
    mode: Optional[str] = None,
) -> Tuple[DescriptionPin, Any]:
    """Fence a description and pin its bytes. Returns ``(pin, review)``.

    The review is the SAME call :func:`execution.tool_deferral.fence_description`
    makes, so a pinned description and a searched description are fenced
    identically - two fences would be two answers to "is this text safe".

    **The pin is over the RAW text, not the fenced text.** That is deliberate
    and it is the whole mechanism: the fence's output is a function of the raw
    text *and* of the shipped policy, so a policy edit would silently change
    what a pin means. Pinning the raw bytes means a change to the text is a
    change to the pin, and a change to the policy is visible as a change to
    ``severity``/``blocked`` on the review.
    """
    review = _vocab.fence_description(
        description, source=source, max_chars=max_chars, policy=policy, mode=mode
    )
    raw = "" if description is None else str(description)
    pin = DescriptionPin(
        name=str(name or ""),
        digest=content_digest(raw),
        chars=len(raw),
        source=str(source or ""),
        blocked=bool(review.blocked),
        tainted=bool(review.tainted),
        severity=str(review.severity),
        review_digest=str(review.digest or ""),
        note=(
            "pinned over the RAW description bytes; the review beside it is the "
            "fence verdict at pin time"
        ),
    )
    return pin, review


def check_description(pin: DescriptionPin, description: Any) -> DescriptionChange:
    """Return whether ``description`` is still the text the pin describes.

    The answer is a VALUE and never an exception, because the caller of this
    function is a dispatch path and a hostile description must not be able to
    turn a content check into a crash. :attr:`DescriptionChange.changed` is the
    property; the digests are published so a human can see WHICH text moved.
    """
    raw = "" if description is None else str(description)
    current = content_digest(raw)
    if current == pin.digest:
        return DescriptionChange(
            name=pin.name,
            changed=False,
            pinned_digest=pin.digest,
            current_digest=current,
            prior_chars=pin.chars,
            current_chars=len(raw),
            verdict="unchanged",
            note="the description still says what it said when it was pinned",
        )
    return DescriptionChange(
        name=pin.name,
        changed=True,
        pinned_digest=pin.digest,
        current_digest=current,
        prior_chars=pin.chars,
        current_chars=len(raw),
        verdict="changed_reported",
        note=(
            f"the description of {pin.name!r} CHANGED between the moment it was "
            f"pinned (digest {pin.digest[:16]}..., {pin.chars} chars, "
            f"severity {pin.severity}) and this fetch (digest {current[:16]}..., "
            f"{len(raw)} chars). Whatever was reviewed or approved was NOT this "
            f"text. Re-review before use; do not refresh the pin silently."
        ),
    )


# ---------------------------------------------------------------------------
# 4. A skill's own file is pinned, for the same reason
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillFilePin:
    """A skill document's bytes at the moment its declaration was resolved."""

    skill: str
    digest: str
    chars: int
    source: str = "text"
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "skill": self.skill,
            "digest": self.digest,
            "chars": int(self.chars),
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class SkillFileChange:
    """Whether a skill document still says what it said when it was pinned."""

    skill: str
    changed: bool
    pinned_digest: str
    current_digest: str
    verdict: str = "unchanged"
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "skill": self.skill,
            "changed": bool(self.changed),
            "pinned_digest": self.pinned_digest,
            "current_digest": self.current_digest,
            "verdict": self.verdict,
            "note": self.note,
        }


def pin_skill_file(
    skill: str, text: Any = None, *, path: Optional[str] = None
) -> SkillFilePin:
    """Pin a skill document. Either pass ``text`` or pass ``path`` to read.

    A path is read with ``errors="replace"`` and a byte cap, because a skill
    document is third-party content on disk and a hostile one should not be
    able to wedge the pin on an undecodable byte. An unreadable path pins the
    literal it was given, so a caller that cannot read the file still gets a
    pin - of nothing, which is honest, and which will read as a CHANGE the
    moment the file becomes readable.
    """
    source = "text"
    body = "" if text is None else str(text)
    if text is None and path:
        source = f"file:{path}"
        try:
            body = _read_bounded(path)
        except Exception as exc:  # pragma: no cover - defensive
            body = ""
            source = f"unreadable:{path}:{type(exc).__name__}"
    return SkillFilePin(
        skill=str(skill or ""),
        digest=content_digest(body),
        chars=len(body),
        source=source,
        note=(
            "a skill document is third-party content: pinning it means a run "
            "cannot have its declared tool set rewritten under it"
        ),
    )


def check_skill_file(pin: SkillFilePin, text: Any = None, *, path: Optional[str] = None) -> SkillFileChange:
    """Return whether the skill document still says what it said when pinned."""
    body = "" if text is None else str(text)
    if text is None and path:
        try:
            body = _read_bounded(path)
        except Exception:  # pragma: no cover - defensive
            body = ""
    current = content_digest(body)
    if current == pin.digest:
        return SkillFileChange(
            skill=pin.skill,
            changed=False,
            pinned_digest=pin.digest,
            current_digest=current,
            note="the skill document is byte-identical to the pinned one",
        )
    return SkillFileChange(
        skill=pin.skill,
        changed=True,
        pinned_digest=pin.digest,
        current_digest=current,
        verdict="changed_reported",
        note=(
            f"SKILL.md for {pin.skill!r} CHANGED mid-run (pinned digest "
            f"{pin.digest[:16]}..., now {current[:16]}...). A skill that edits "
            f"its own declaration is declaring something new; the pinned set "
            f"stands and the change is reported."
        ),
    )


# ---------------------------------------------------------------------------
# 5. A declaration is DATA. The run's tool set is the ENFORCEMENT.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallValidation:
    """The verdict on one ACTUAL call, judged against the run and nothing else."""

    tool: str
    allowed: bool
    reason: str = ""
    #: True when the call is inside what the SKILL DECLARED. Recorded, and it
    #: is explicitly NOT the enforcement: a call outside the declaration is
    #: judged on :attr:`run_allows` alone, and a call inside one that the run
    #: does not have is still refused.
    declared: bool = False
    run_allows: bool = False
    rule: str = "run_tools"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "declared": bool(self.declared),
            "run_allows": bool(self.run_allows),
            "rule": self.rule,
        }


def validate_actual_call(
    *,
    tool: str,
    run_tools: Sequence[str],
    declared_tools: Optional[Sequence[str]] = None,
) -> CallValidation:
    """Judge one real call against ``run_tools``, never against a declaration.

    **This is the row that matters and it is a one-line rule.** A skill that
    declares ``read`` and calls ``write`` is declaring a lie, and the only way
    to catch a lie is to check the CALL rather than the claim. So:

    * a tool in ``run_tools`` is allowed, **whether or not the skill declared
      it** - the run has it, and refusing it would be the declaration becoming
      a restriction the harness invented;
    * a tool the run does not have is refused, **whether or not the skill
      declared it** - and :attr:`declared` is recorded so the receipt shows the
      skill CLAIMED it, which is the evidence that matters;
    * ``declared_tools=None`` means "no declaration was supplied", which is a
      fact and not a permission. The verdict is unchanged.

    The ``rule`` field says which authority decided, so a reader can tell a
    declaration-free refusal from a declaration-free permission.
    """
    name = str(tool or "").strip()
    run = set(_clean(run_tools))
    declared = set(_clean(declared_tools)) if declared_tools is not None else None
    run_allows = name in run
    claimed = name in declared if declared is not None else False
    if run_allows:
        return CallValidation(
            tool=name,
            allowed=True,
            reason=(
                "this run exposes the tool, so the call is validated against the "
                "run's own surface"
                + (
                    "; the skill did NOT declare it, which is recorded because a "
                    "declaration that omits a real call is a declaration nobody "
                    "should be able to rely on"
                    if declared is not None and not claimed
                    else ""
                )
            ),
            declared=claimed,
            run_allows=True,
        )
    return CallValidation(
        tool=name,
        allowed=False,
        reason=(
            f"this run does not expose {name!r}"
            + (
                ", and the skill DECLARED it: the declaration is recorded as "
                "evidence that the skill claimed a tool the run does not have"
                if claimed
                else "; the skill did not declare it either, so this is the run's "
                "own surface refusing, not the declaration"
            )
        ),
        declared=claimed,
        run_allows=False,
    )


def nested_skill_tools(
    parent_effective: Sequence[str],
    child_declared: Sequence[str],
    run_tools: Optional[Sequence[str]] = None,
) -> Tuple[str, ...]:
    """Return ``min(parent, child declared)``, intersected with the run.

    A skill that runs another skill must not be a widening step, and the
    arithmetic that says so is a minimum. It is a MINIMUM and not a union
    because every rule above this one is a narrowing, and a union here would
    be the first widening in the file.

    ``run_tools`` is applied as a third intersection because the parent's
    effective set is already a subset of the run's; passing it explicitly makes
    the property true even if a caller supplies a parent set that is not.
    """
    parent = set(_clean(parent_effective))
    child = set(_clean(child_declared))
    effective = parent & child
    if run_tools is not None:
        effective &= set(_clean(run_tools))
    return tuple(sorted(effective))


@dataclass(frozen=True)
class EscalationRefusal:
    """A mid-run request for more than the run resolved."""

    kind: str
    requested: Tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "requested": list(self.requested),
            "reason": self.reason,
        }


def refuses_mid_run_escalation(
    kind: str,
    requested: Optional[Sequence[str]] = None,
    *,
    started: bool = True,
) -> EscalationRefusal:
    """Refuse a permission or tool request that arrives AFTER the run started.

    A run's tool surface is resolved once, before the first turn. That is not
    an implementation convenience - it is what makes the surface something a
    user can review, because "the tool set at the moment you approved this run"
    is a fact and "the tool set at whatever turn asked" is not.

    ``started=False`` is the honest OFF arm and it exists so the rule can be
    expressed as a decision rather than as an absence: before a run starts,
    resolving a wider set is how a run gets its surface, and refusing it would
    forbid the only legitimate moment.
    """
    names = _clean(requested)
    slug = str(kind or "").strip()
    if not started:
        return EscalationRefusal(
            kind=slug,
            requested=names,
            reason=(
                "the run has not started, so its surface is still being resolved; "
                "a request now is configuration, not escalation"
            ),
        )
    if slug not in MID_RUN_ESCALATIONS:
        return EscalationRefusal(
            kind=slug or "unclassified",
            requested=names,
            reason=(
                f"{slug!r} is not a class of mid-run escalation this guard knows, "
                f"and an unclassified request is refused rather than allowed by "
                f"default (known: {list(MID_RUN_ESCALATIONS)})"
            ),
        )
    return EscalationRefusal(
        kind=slug,
        requested=names,
        reason=(
            f"{slug!r} was refused: this run's tool surface and permissions were "
            f"resolved before its first turn and are not renegotiated mid-run"
            + (f". Requested: {list(names)}" if names else "")
            + ". A user who wants a wider surface starts a run with one - that is "
            "the moment the surface is reviewable."
        ),
    )


# ---------------------------------------------------------------------------
# 6. The identical-path trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPathTrace:
    """The ordered stages a call passes through, plus what it was decided as.

    **``stages`` carries no surface state and ``decision`` carries no surface
    state.** That is the claim of this round expressed as a type: the funnel and
    the verdict are functions of the call, not of how the model learned about
    it. The state a call had is recorded somewhere else - :func:`surface_journal_row`
    - because a post-hoc audit needs it and an approval prompt must not be able
    to be branched on.

    ``approval`` is the exact field set a human is asked about, and it is
    identical across the visible and deferred arms for the same reason.
    """

    stages: Tuple[str, ...] = CALL_STAGES
    decision: Tuple[Any, ...] = ()
    approval: Tuple[Tuple[str, Any], ...] = ()
    tool: str = ""
    invoked_via_skill: str = ""

    def stage_names(self) -> Tuple[str, ...]:
        return tuple(self.stages)

    def identical_to(self, other: "ExecutionPathTrace") -> bool:
        """Whether two calls took the same path and were decided the same way.

        Compares the stages, the decision and the approval field set, and
        deliberately does NOT compare ``tool`` - two calls of DIFFERENT tools
        legitimately take the same path, and a comparison that included the
        name would only prove the names matched.
        """
        return (
            tuple(self.stages) == tuple(other.stages)
            and tuple(self.decision) == tuple(other.decision)
            and tuple(self.approval) == tuple(other.approval)
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "stages": list(self.stages),
            "decision": list(self.decision),
            "approval": [[label, value] for label, value in self.approval],
            "invoked_via_skill": self.invoked_via_skill,
        }


def approval_prompt_fields(
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    policy_engine: Optional[ToolPolicyEngine] = None,
    approve: Optional[Callable[..., Any]] = None,
) -> Tuple[Tuple[str, Any], ...]:
    """The exact fields a human is asked about, as an ORDERED tuple.

    Derived from the SAME two authorities ``SafeToolBackend._authorize`` uses -
    ``policy_for_tool`` and ``ToolPolicyEngine.evaluate`` - and from nothing
    else. There is no surface argument, so there is nowhere for a visibility
    state to enter and no way for this function to grow one without a visible
    signature change.

    Order matters as much as membership: a prompt a surface re-renders in a
    different order is a different prompt, and "identical" has to mean
    identical.
    """
    fingerprint = _vocab.call_fingerprint(
        tool, args or {}, policy_engine=policy_engine, approve=approve
    )
    values = dict(args or {})
    digest = content_digest(
        "|".join(
            f"{key}={values[key]!r}" for key in sorted(values) if _is_promptable(values[key])
        )
    )
    return (
        ("tool", fingerprint.tool),
        ("effect", fingerprint.effect),
        ("backend", fingerprint.backend),
        ("sandboxed", fingerprint.sandboxed),
        ("process", fingerprint.process),
        ("network", fingerprint.network),
        ("mutating", fingerprint.mutating),
        ("requires_approval", fingerprint.requires_approval),
        ("policy_action", fingerprint.policy_action),
        ("effect_digest", digest),
    )


def trace_execution_path(
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    *,
    policy_engine: Optional[ToolPolicyEngine] = None,
    approve: Optional[Callable[..., Any]] = None,
    invoked_via_skill: str = "",
) -> ExecutionPathTrace:
    """Return the funnel and the decision for one call, with no surface input.

    ``stages`` is :data:`CALL_STAGES` unconditionally - there is no branch here
    that could return a different list, which is why a visible call and a
    deferred call of the same tool produce equal traces. The PROOF that the real
    dispatch agrees with this derivation is the pin module's job, because a
    derivation that agreed with itself would prove nothing; see
    ``execution/test_p3b_w2_attack_pins.py::TestThePathIsIdentical``.
    """
    fingerprint = _vocab.call_fingerprint(
        tool, args or {}, policy_engine=policy_engine, approve=approve
    )
    return ExecutionPathTrace(
        stages=CALL_STAGES,
        decision=fingerprint.decision_fields(),
        approval=approval_prompt_fields(
            tool, args, policy_engine=policy_engine, approve=approve
        ),
        tool=str(tool or ""),
        invoked_via_skill=str(invoked_via_skill or ""),
    )


# ---------------------------------------------------------------------------
# 7. The journal records the state, so an audit can tell the arms apart
# ---------------------------------------------------------------------------


def surface_journal_row(
    trace: ExecutionPathTrace,
    pin: Optional[SurfacePin],
    *,
    task_id: str = "",
    invoked_via_skill: str = "",
    result: Optional[ToolResult] = None,
) -> Dict[str, Any]:
    """The post-hoc audit row. **This** is where the surface state lives.

    The split is the answer to the obvious objection: an approval prompt that
    differed between a visible and a deferred tool would let an approver be
    steered by the phase's own mechanism, and an approval prompt that did
    differ would also be a branch in a security decision. So the prompt does
    NOT differ, and the audit does not lose the fact - it moves it here, into a
    row a reader can query after the fact rather than a prompt an attacker can
    influence before the fact.
    """
    name = str(trace.tool or "")
    state = pin.state_of(name) if pin is not None else ""
    row: Dict[str, Any] = {
        "kind": DEFERRED_JOURNAL_KIND,
        "tool": name,
        "surface_state": state or "unresolved",
        "deferred": state == _vocab.SURFACE_DEFERRED,
        "withheld": state == _vocab.SURFACE_WITHHELD,
        "visible": state == _vocab.SURFACE_VISIBLE,
        "invoked_via_skill": str(invoked_via_skill or ""),
        "stages": list(trace.stages),
        "policy_action": trace.approval[8][1] if len(trace.approval) > 8 else "",
        "requires_approval": trace.approval[7][1] if len(trace.approval) > 7 else None,
        "effect": trace.approval[1][1] if len(trace.approval) > 1 else "",
    }
    if pin is not None:
        row["surface_digest"] = pin.digest
        row["surface_capability"] = pin.capability
        row["surface_model"] = pin.model
    if task_id:
        row["task_id"] = str(task_id)
    if result is not None:
        row["ok"] = bool(result.ok)
        row["profile"] = result.profile
    return row


def attach_surface_state(result: ToolResult, state: str, kind: str = "") -> ToolResult:
    """Stamp the surface state onto a ``ToolResult`` as ADDITIVE attributes.

    An instance attribute and not a dataclass field, for the reason this
    repository has used four times now: ``shared/types.py`` is another owner's
    file and a field added there is a cross-terminal contract change. The
    ``execution/flake_gate.py::attach_evidence`` precedent is the same shape.

    The attribute is set on EVERY path - including the un-pinned one, where it
    is ``"unresolved"`` - because a field that exists only on the interesting
    arm is a field whose absence a reader has to interpret.
    """
    result.surface_state = str(state or "unresolved")
    result.surface_refusal_kind = str(kind or "")
    return result


# ---------------------------------------------------------------------------
# 8. The structural claim, re-asserted over THIS module
# ---------------------------------------------------------------------------


def module_source() -> str:
    """Return this module's own source, for the structural self-checks.

    Two checks run against it in the pin module, and both are the same shape as
    Wave 1's: "we were careful" is not evidence, "there is no expression for it"
    is. They are

    * :func:`execution.tool_deferral.forbidden_grant_expressions` - this module
      contains no assignment to a ``ToolPolicy`` field and no
      ``dataclasses.replace``/``setattr`` on a policy, so it cannot grant;
    * :func:`forbidden_rederivation_names` below - the surface is not
      re-derived from a ``model`` argument anywhere on a dispatch path.
    """
    import pathlib

    return pathlib.Path(__file__).read_text(encoding="utf-8")


def forbidden_rederivation_names(source: str) -> Tuple[str, ...]:
    """Return every site in ``source`` that could RE-DERIVE a session surface.

    The attack is row 8: ``harness.tool_surface.surface_for`` takes a ``model``
    and returns a withheld set, so a dispatch path that calls it with a
    caller-supplied model re-derives the surface on every call and a swap
    becomes a widening. This scans for exactly that shape - a call to
    ``surface_for`` outside the one resolver that is allowed to - and names the
    line so a failing assertion says where.

    The allowance is by NAME and it is narrow: ``pin_surface`` and
    ``swap_surface`` are the two functions whose entire job is deciding a
    surface, and nothing else in this file may call the derivation.
    """
    allowed = frozenset({"pin_surface", "swap_surface", "module_source"})
    hits: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return (f"source did not parse: {exc}",)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = ""
        if isinstance(node.func, ast.Name):
            label = node.func.id
        elif isinstance(node.func, ast.Attribute):
            label = node.func.attr
        if label != "surface_for":
            continue
        # Attribute the call to the enclosing function.
        owner = ""
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(child is node for child in ast.walk(parent)):
                owner = parent.name
                break
        if owner in allowed:
            continue
        hits.append(
            f"line {node.lineno}: calls surface_for() inside {owner or '<module>'!r}; "
            f"only {sorted(allowed)} may derive a surface, so a dispatch path "
            f"cannot re-derive one from a caller-supplied model"
        )
    return tuple(hits)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clean(names: Optional[Iterable[Any]]) -> Tuple[str, ...]:
    """Return the non-empty, de-duplicated, order-preserving names.

    A verbatim copy of :func:`execution.tool_deferral._clean` rather than an
    import of it, and that is a deliberate exception to this repo's one-rule-
    one-home discipline: that helper is module-private, and reaching into
    another module's underscore is the coupling this repository has paid for
    before. The duplicate is four lines, the rule it implements is "strip and
    de-duplicate", and it is pinned equal to the original by the pin module so
    the two cannot drift.
    """
    out: List[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


#: How much of a skill document a pin reads. 1 MiB: a SKILL.md is a markdown
#: document, and a file this size is not one. The point is to bound a pin, not
#: to accept a large one.
SKILL_FILE_MAX_BYTES = 1 << 20


def _read_bounded(path: str) -> str:
    """Read at most :data:`SKILL_FILE_MAX_BYTES` of a file as text."""
    with open(os.fspath(path), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(SKILL_FILE_MAX_BYTES)


def _is_promptable(value: Any) -> bool:
    """Whether an argument value is safe to summarise into an effect digest.

    A value that is not a plain scalar is EXCLUDED rather than repr'ed. A
    ``repr`` of an arbitrary object can execute a ``__repr__``, and a value an
    approval prompt does not need is not worth that: the prompt's job is to let
    a human recognise the call, and "this tool, these four scalar arguments" is
    what that takes.
    """
    return isinstance(value, (str, int, float, bool, type(None)))
