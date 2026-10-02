"""The ONE authority for the two turn caps, and for saying so before the cliff.

What was actually wrong (measured, not read off the brief)
----------------------------------------------------------
The brief for this round described ``session_max_turns`` as declared in
``DEFAULTS`` with **zero readers**. That is not what the tree contains. It has a
reader - :func:`harness.agent_kernel.kernel.AgentKernel` passes it to
``ContextBuilder(max_turns=...)`` - and what that reader does is
``state.turns = state.turns[-self.max_turns:]``: it bounds how many prior
**context-summary** records the compiler keeps.

So the defect is not a dead key. The defect is a **name that lies about what the
key does**. ``session_max_turns`` reads as a cap on how long a conversation may
get; it is a cap on how many summarised turns fit in the compiled context
window. A reader who trusted the name would build a user-facing promise the
number does not keep. The repo's own doctrine puts it plainly: *"A cap that
does not cap is worse than either alternative"* - and a cap under the wrong name
is the same failure wearing a comment.

There is also a genuine gap underneath it: **there is no conversation-length
cap at all.** A session can run indefinitely; the only real bounds are
``budget_cap_usd`` and ``max_wallclock_s``, both of which are money and time,
not turns.

The decision, and why
---------------------
**Keep ``session_max_turns``, and give it an honest alias. Add a real
conversation cap under a name that says what it is.** Deleting the key was
rejected because it is live and load-bearing for the context window; renaming it
outright was rejected because it would silently change behaviour for any
existing configuration and for the kernel call site, and a rename is not a
performance change. What is rejected outright is leaving a cap named
``session_max_turns`` that bounds context records.

- ``session_max_turns`` - UNCHANGED behaviour. Still the context-summary bound.
  :data:`SESSION_MAX_TURNS_ALIAS` records the honest name, and
  :func:`resolve_caps` reports which key supplied the value so a receipt never
  implies a meaning the number does not have.
- ``session_max_conversation_turns`` - the REAL conversation cap. Default
  ``None``, meaning unbounded, and deliberately **not** a number: a default here
  is merged into every task and every eval arm, so shipping a real value would
  silently truncate conversations everywhere. ``None`` is also the honest
  statement of today's actual behaviour, which is what this key documents.

The approach is an OBSERVATION, not a refusal
----------------------------------------------
:func:`approach_observation` returns a receipt when a run comes within
``turn_cap_approach_warn_turns`` of a cap. It does not stop anything. The shape
is deliberately the same as the doom-loop path, which escalates to
``needs_input``: a cap the run is about to reach is a fact about the run, and a
fact should be reported, not converted into a failure. A cap that turned into a
refusal at N-1 turns would be a second, invisible cap - the exact thing this
round exists to remove.

Public surface
--------------
- :data:`DEFAULT_AGENT_MAX_TURNS` / :data:`DEFAULT_SESSION_MAX_TURNS` - the
  numbers, defined ONCE so the four hard-coded ``25`` fallbacks in the tree can
  be deleted rather than left to drift.
- :data:`TURN_CAP_KINDS` - the closed vocabulary.
- :class:`TurnCaps` / :func:`resolve_caps` - both caps, resolved, with
  provenance.
- :func:`approach_observation` - the visible-approach receipt.
- :func:`render_caps` / :func:`turn_caps_receipt` - the shapes T4's live rail
  and T5's Trust-Ladder rung-8 check read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "CAP_CONTEXT_SUMMARY",
    "CAP_CONVERSATION",
    "CAP_PER_TASK",
    "DEFAULT_AGENT_MAX_TURNS",
    "DEFAULT_APPROACH_WARN_TURNS",
    "DEFAULT_SESSION_MAX_TURNS",
    "TURN_CAP_KINDS",
    "TurnCaps",
    "approach_observation",
    "render_caps",
    "resolve_caps",
    "turn_caps_receipt",
]

#: Per-TASK tool-calling cap. Defined here so the four hard-coded ``25``
#: fallbacks scattered through ``agent_loop.py`` / ``agent_loop_step.py`` /
#: ``strategy.py`` can be deleted instead of left to drift away from DEFAULTS.
#: The justification for 60, and its spend implication, are in
#: ``harness/config.py`` next to the key itself - this constant is the
#: fallback, not the documentation.
DEFAULT_AGENT_MAX_TURNS = 60

#: How many prior CONTEXT-SUMMARY records the compiler keeps. Unchanged from the
#: value this key has always had.
DEFAULT_SESSION_MAX_TURNS = 24

#: How close to a cap a run gets before it announces itself.
DEFAULT_APPROACH_WARN_TURNS = 10

CAP_PER_TASK = "per_task"
CAP_CONTEXT_SUMMARY = "context_summary"
CAP_CONVERSATION = "conversation"

#: The three caps, and what each one actually bounds. Published so a consumer
#: cannot read a name and have to guess the scope.
TURN_CAP_KINDS: Tuple[str, ...] = (
    CAP_PER_TASK,
    CAP_CONTEXT_SUMMARY,
    CAP_CONVERSATION,
)

#: The honest name for what ``session_max_turns`` does. A configuration may use
#: either; the receipt reports which one supplied the value.
SESSION_MAX_TURNS_ALIAS = "session_context_summary_turns"


def _as_int(value: Any, fallback: int, floor: int = 0) -> int:
    """Coerce a config value to an int, degrading to ``fallback`` on nonsense.

    ``bool`` is refused rather than coerced: ``int(True) == 1`` would turn a
    typo into a one-turn run, and a one-turn run is a cap that does not cap.
    """
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return out if out >= floor else fallback


@dataclass(frozen=True)
class TurnCaps:
    """Both caps, resolved, each with the key that supplied its value."""

    per_task: int = DEFAULT_AGENT_MAX_TURNS
    per_task_source: str = "default"
    context_summary: int = DEFAULT_SESSION_MAX_TURNS
    context_summary_source: str = "default"
    conversation: Optional[int] = None
    conversation_source: str = "default"
    approach_warn_turns: int = DEFAULT_APPROACH_WARN_TURNS
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caps": {
                CAP_PER_TASK: {
                    "value": int(self.per_task),
                    "source": self.per_task_source,
                    "bounds": "tool-calling turns in one agent task",
                },
                CAP_CONTEXT_SUMMARY: {
                    "value": int(self.context_summary),
                    "source": self.context_summary_source,
                    "bounds": "prior context-summary records the compiler keeps",
                    "alias": SESSION_MAX_TURNS_ALIAS,
                },
                CAP_CONVERSATION: {
                    "value": self.conversation,
                    "source": self.conversation_source,
                    "bounds": "turns in one session; null means unbounded",
                },
            },
            "approach_warn_turns": int(self.approach_warn_turns),
            "notes": list(self.notes),
        }


def resolve_caps(config: Optional[Mapping[str, Any]] = None) -> TurnCaps:
    """Resolve both caps from a config mapping, with provenance for each.

    Read by KEY MEANING, never by truthiness: an absent ``per_task`` key means
    the default, an explicit ``0`` is a deliberate zero-turn run, and those are
    different facts a receipt must not collapse. A truthiness check would read
    ``0`` as absent and answer the wrong question.
    """
    cfg: Mapping[str, Any] = config or {}
    notes = []

    per_task_key = "agent_max_turns" if "agent_max_turns" in cfg else None
    per_task = _as_int(
        cfg.get("agent_max_turns") if per_task_key else None,
        DEFAULT_AGENT_MAX_TURNS,
        floor=0,
    )
    if per_task <= 0:
        notes.append(
            f"agent_max_turns={per_task!r} permits no turns; the loop will "
            f"produce an answer without ever calling the model"
        )

    summary_key = None
    for candidate in ("session_max_turns", SESSION_MAX_TURNS_ALIAS):
        if candidate in cfg:
            summary_key = candidate
            break
    context_summary = _as_int(
        cfg.get(summary_key) if summary_key else None,
        DEFAULT_SESSION_MAX_TURNS,
        floor=1,
    )
    if summary_key == SESSION_MAX_TURNS_ALIAS:
        notes.append(
            f"{SESSION_MAX_TURNS_ALIAS} is the honest alias and bounds context "
            f"SUMMARY records, not conversation length"
        )

    conversation_raw = (
        cfg.get("session_max_conversation_turns")
        if "session_max_conversation_turns" in cfg
        else None
    )
    conversation = (
        None
        if conversation_raw is None
        else _as_int(conversation_raw, DEFAULT_SESSION_MAX_TURNS, floor=1)
    )

    return TurnCaps(
        per_task=per_task,
        per_task_source=per_task_key or "default",
        context_summary=context_summary,
        context_summary_source=summary_key or "default",
        conversation=conversation,
        conversation_source=(
            "config" if "session_max_conversation_turns" in cfg else "default"
        ),
        approach_warn_turns=_as_int(
            cfg.get("turn_cap_approach_warn_turns")
            if "turn_cap_approach_warn_turns" in cfg
            else None,
            DEFAULT_APPROACH_WARN_TURNS,
            floor=0,
        ),
        notes=tuple(notes),
    )


def approach_observation(
    turn: int,
    *,
    config: Optional[Mapping[str, Any]] = None,
    cap: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return a receipt when ``turn`` is close to a cap, else ``None``.

    An OBSERVATION, never a stop. ``None`` means "nothing to say", which is
    distinct from "the cap was not approached" - a caller that wants to know
    whether the mechanism ran at all should compare the turn to
    ``warn + 1`` itself, and :func:`turn_caps_receipt` publishes the numbers
    that makes that possible.

    ``None`` for the conversation cap when it is unset, because there is no
    cliff to approach: announcing an approach to a cap that does not exist
    would be inventing a limit.
    """
    caps = resolve_caps(config)
    limit = caps.per_task if cap is None else max(0, int(cap))
    if limit <= 0:
        return None
    remaining = limit - int(turn)
    if remaining > caps.approach_warn_turns:
        return None
    if caps.conversation is not None:
        conv_remaining = caps.conversation - int(turn)
        if conv_remaining <= caps.approach_warn_turns:
            return {
                "kind": CAP_CONVERSATION,
                "turn": int(turn),
                "cap": int(caps.conversation),
                "remaining": int(conv_remaining),
                "observation": (
                    f"turn {turn} of at most {caps.conversation}: "
                    f"{max(0, conv_remaining)} conversation turn(s) left"
                ),
            }
    return {
        "kind": CAP_PER_TASK,
        "turn": int(turn),
        "cap": int(limit),
        "remaining": int(remaining),
        "observation": (
            f"turn {turn} of at most {limit}: {max(0, remaining)} turn(s) left. "
            f"The run continues; this is a heads-up, not a stop."
        ),
    }


def render_caps(config: Optional[Mapping[str, Any]] = None) -> str:
    """One line naming every cap, for a model-facing note or a status line.

    The conversation cap is rendered as UNBOUNDED rather than omitted: an absent
    cap read as "fine" is the same confusion this module exists to remove.
    """
    caps = resolve_caps(config)
    conversation = "unbounded" if caps.conversation is None else str(caps.conversation)
    return (
        f"turn caps: {caps.per_task} per task "
        f"(warn within {caps.approach_warn_turns}), "
        f"{caps.context_summary} context-summary records, "
        f"{conversation} conversation turns"
    )


def turn_caps_receipt(
    config: Optional[Mapping[str, Any]] = None,
    *,
    turn: Optional[int] = None,
) -> Dict[str, Any]:
    """The publishable receipt T4's rail and T5's rung-8 check consume.

    Carries the values, their provenance, the approach threshold, the current
    turn when the caller has one, and the observed approach - so a checker can
    assert "both caps are observable" without re-deriving any of it, and
    without the risk of a check that passes because the fields are absent.
    """
    caps = resolve_caps(config)
    receipt: Dict[str, Any] = caps.to_dict()
    receipt["rendered"] = render_caps(config)
    if turn is not None:
        observation = approach_observation(int(turn), config=config)
        receipt["turn"] = int(turn)
        receipt["approaching"] = observation is not None
        receipt["approach_observation"] = observation
    return receipt
