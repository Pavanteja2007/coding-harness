"""Persistent, returnable, steerable subagents - the corpus's best feature.

The esveo OpenCode review, quoted in the Phase 4 brief: *"OpenCode's most
impressive feature is the **subagents**. OpenCode's subagents are **persistent**,
which means the agent you're working with can **go back to the same subagent**
multiple times and ask it additional questions."* The example that makes it
concrete is a reviewer that hallucinated a bug: the main agent tried to
reproduce it, could not, and **went back to the same subagent** for more detail.

Our `task` tool spawns one-shot children. This module is the durable side: an
identity, a transcript, a result, and a **steering** channel - plus the two
properties that make the feature safe rather than merely pleasant:

* **isolation survives steering.** A steer is delivered to the child's own
  message list. It must NOT pull the child's transcript into the parent, which
  is the one property Phase 4's isolation work (T1) built and the one most
  likely to be broken by adding a human-in-the-loop channel. Every steer
  returns a receipt that carries the parent's transcript size before and after,
  and :func:`assert_isolation_after_steering` is the assertion.
* **the trust boundary is visible.** A subagent inherits only its parent's
  **deny** rules. An allow rule does not propagate downward, because "the parent
  may do X" is not "the child may do X". :func:`trust_boundary` publishes both
  halves, so a user can see what a subagent can and cannot do.

Transcript integrity is the third honesty property: a compacted transcript must
**never render as complete**. T1 compacts subagent context, so a transcript
whose own record says it was compacted renders with the compaction stated and
the dropped count named.

    python -m cli.subagent_view --store logs/_subagents --session SID list
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cli.ui import sanitize_text

__all__ = [
    "AGENT_STATES",
    "BOUND_EXPLANATIONS",
    "BOUND_KINDS",
    "INHERITANCE_RULES",
    "IsolationBroken",
    "SteerReceipt",
    "SubagentRecord",
    "SubagentStore",
    "assert_isolation_after_steering",
    "assert_trust_columns_disjoint",
    "list_marker",
    "new_agent_id",
    "normalize_bound",
    "steering_history_lines",
    "trust_boundary",
    "trust_boundary_lines",
]

#: The closed vocabulary of a subagent's lifecycle. ``compacted`` is a STATE and
#: not a modifier because the whole point is that a compacted transcript and a
#: complete one must not read the same.
AGENT_STATES: Tuple[str, ...] = (
    "running",
    "finished",
    "compacted",
    "refused",
    "failed",
)

#: The closed vocabulary of the BOUND that stopped a subagent.
#:
#: Read from ``harness.agent_kernel.subagents.PlanResearchResult.stopped_because``
#: - which exists, and which P4/W2 measured as **never reaching the user**: a
#: three-session run produced a subagent whose record carried ``state: failed``
#: and whose transcript said nothing about whether it ran out of turns, out of
#: budget, or hit a model error. Those are three different problems with three
#: different fixes, and a subagent that reports only "failed" sends a reader to
#: all three.
#:
#: Kept as a LOCAL vocabulary rather than an import, because this module renders
#: and must not fail because a sibling module moved; :func:`normalize_bound`
#: narrows anything unrecognised to ``"unknown"`` and the renderer says so. An
#: unrecognised bound is named, never dropped and never guessed at.
BOUND_KINDS: Tuple[str, ...] = (
    "turns",
    "budget",
    "tool_calls",
    "chars",
    "cost",
    "model_error",
    "parse_error",
    "no_tool_call",
    "refused",
    "failed",
    "unknown",
)

#: What each bound MEANS, so the renderer can say it in words rather than echoing
#: a token. A reader who is told "turns" learns nothing; a reader who is told
#: "the subagent ran out of turns before finishing" knows what happened.
BOUND_EXPLANATIONS: Dict[str, str] = {
    "turns": "it ran out of turns before it finished",
    "budget": "it ran out of budget before it finished",
    "tool_calls": "it ran out of tool calls before it finished",
    "chars": "its answer exceeded the maximum length and was cut",
    "cost": "it exceeded the cost ceiling before it finished",
    "model_error": "the provider failed; nothing about the task was decided",
    "parse_error": "its output could not be parsed, so no step could be taken",
    "no_tool_call": "it answered without calling a tool, so it did nothing",
    "refused": "it declined the work it was given",
    "failed": "it failed, and the failure is recorded on the transcript",
    "unknown": (
        "it stopped for a reason this build does not recognise, so the reason "
        "itself is UNKNOWN rather than absent"
    ),
}

#: What a child inherits from its parent, stated as a rule rather than as an
#: accident of configuration. Deny-only is OpenCode's model and it is the safe
#: direction: a missing allow propagates as a refusal, which is recoverable; a
#: wrongly inherited allow propagates as a capability nobody granted.
INHERITANCE_RULES: Tuple[Tuple[str, str], ...] = (
    ("deny", "INHERITED. A parent's refusal binds every child it spawns."),
    (
        "allow",
        "NOT INHERITED. 'The parent may do X' is not 'the child may do X'; an "
        "allow rule must be granted to the child explicitly.",
    ),
    ("approval", "NOT INHERITED. A standing approval in the parent does not approve the child."),
    ("budget", "INHERITED as a CEILING only. A child may spend less, never more."),
    ("network", "INHERITED as a denial only. No parent allowance widens a child."),
)


class IsolationBroken(RuntimeError):
    """Raised when a steer changed the parent's transcript."""


def _line(value: Any) -> str:
    """Return one display line through the ONE sanitise entry point.

    Subagent output is model-generated from repository content, so it can carry
    injected instructions. Every rendered value here is sanitised, and
    :func:`SubagentStore.steer` records the sanitised text it actually wrote so
    what the user saw is what the child received.
    """
    return sanitize_text(value)


def new_agent_id(prefix: str = "sub") -> str:
    """Return a filesystem-safe, collision-resistant subagent identity."""
    stamp = int(time.time() * 1000)
    suffix = os.urandom(3).hex()
    return f"{prefix}-{stamp:013x}-{suffix}"


def normalize_bound(value: Any) -> str:
    """Return which bound stopped a subagent, in words, or ``""`` if it finished.

    A bound the harness records as ``"model_error: <text>"`` is narrowed to the
    ``model_error`` half and the text is kept in the record's own ``error``
    field, so the receipt says the KIND and the detail stays retrievable. An
    unrecognised word becomes ``"unknown"`` - which the renderer states as an
    unknown reason - because dropping it would render a bounded subagent as one
    that simply ended.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip().lower()
    if head in BOUND_KINDS:
        return head
    return "unknown"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass
class SubagentRecord:
    """One subagent's durable identity, transcript, and result."""

    agent_id: str = ""
    agent: str = ""
    role: str = ""
    description: str = ""
    parent_session_id: str = ""
    node_id: str = ""
    spawned_at: float = 0.0
    state: str = "running"
    turns: int = 0
    result: str = ""
    result_truncated: bool = False
    follow_ups: Tuple[str, ...] = ()
    steers: Tuple[str, ...] = ()
    transcript_messages: int = 0
    transcript_chars: int = 0
    #: Messages the child's own context COMPACTION dropped. Non-zero means the
    #: transcript on disk is NOT the whole conversation, and the renderer says so.
    compacted_messages: int = 0
    compacted_at: float = 0.0
    trust: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: WHICH BOUND stopped this subagent, from :data:`BOUND_KINDS`. Empty means
    #: it finished, or it is still running - never "unknown", which is a value
    #: a producer may set explicitly when it cannot name the bound.
    stopped_because: str = ""
    #: The detail half of a bound the producer wrote as ``"kind: text"``.
    bound_detail: str = ""
    #: WHAT THE PARENT ACTUALLY RECEIVED, and the size of what the child did.
    #: P4/W2 added these because the isolation receipt measured only message
    #: COUNTS: a parent that received a three-line summary and a child that ran
    #: ninety turns both read as "1 -> 1 message(s)".
    parent_received: str = ""
    parent_received_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection (additive keys only)."""
        return {
            "agent_id": self.agent_id,
            "agent": self.agent,
            "role": self.role,
            "description": self.description,
            "parent_session_id": self.parent_session_id,
            "node_id": self.node_id,
            "spawned_at": float(self.spawned_at),
            "state": self.state,
            "turns": int(self.turns),
            "result": self.result,
            "result_truncated": bool(self.result_truncated),
            "follow_ups": list(self.follow_ups),
            "steers": list(self.steers),
            "transcript_messages": int(self.transcript_messages),
            "transcript_chars": int(self.transcript_chars),
            "compacted_messages": int(self.compacted_messages),
            "compacted_at": float(self.compacted_at),
            "stopped_because": self.stopped_because,
            "bound_detail": self.bound_detail,
            "bounded": self.bounded,
            "bound_line": self.bound_line,
            "complete": self.complete,
            "completeness_note": self.completeness_note,
            "parent_received": self.parent_received,
            "parent_received_chars": int(self.parent_received_chars),
            "trust": dict(self.trust),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubagentRecord":
        """Rebuild a record from persisted JSON, tolerating an unknown shape."""
        data = dict(value or {})
        trust = data.get("trust")
        return cls(
            agent_id=str(data.get("agent_id") or ""),
            agent=str(data.get("agent") or ""),
            role=str(data.get("role") or ""),
            description=str(data.get("description") or ""),
            parent_session_id=str(data.get("parent_session_id") or ""),
            node_id=str(data.get("node_id") or ""),
            spawned_at=float(data.get("spawned_at") or 0.0),
            state=str(data.get("state") or "running"),
            turns=int(data.get("turns") or 0),
            result=str(data.get("result") or ""),
            result_truncated=bool(data.get("result_truncated")),
            follow_ups=tuple(str(item) for item in data.get("follow_ups") or ()),
            steers=tuple(str(item) for item in data.get("steers") or ()),
            transcript_messages=int(data.get("transcript_messages") or 0),
            transcript_chars=int(data.get("transcript_chars") or 0),
            compacted_messages=int(data.get("compacted_messages") or 0),
            compacted_at=float(data.get("compacted_at") or 0.0),
            stopped_because=normalize_bound(data.get("stopped_because")),
            bound_detail=str(data.get("bound_detail") or ""),
            parent_received=_line(data.get("parent_received") or ""),
            parent_received_chars=int(data.get("parent_received_chars") or 0),
            trust=dict(trust) if isinstance(trust, Mapping) else {},
            error=str(data.get("error") or ""),
        )

    @property
    def bounded(self) -> bool:
        """Return whether this subagent stopped against a bound rather than finishing."""
        return bool(self.stopped_because)

    @property
    def bound_line(self) -> str:
        """Return the one sentence that says WHICH bound stopped it.

        Empty when nothing stopped it. Never a bare token: ``stopped_because:
        turns`` teaches a reader nothing, and the whole point of carrying the
        field is that the reason reaches them.
        """
        if not self.stopped_because:
            return ""
        kind = self.stopped_because
        text = BOUND_EXPLANATIONS.get(kind, BOUND_EXPLANATIONS["unknown"])
        detail = f" ({self.bound_detail})" if self.bound_detail else ""
        return f"{kind} - {text}{detail}"

    @property
    def complete(self) -> bool:
        """Return whether the transcript can be presented as the whole thing.

        False once compaction has dropped anything, and false while the child is
        still running. This is the property the renderer reads, and it is a
        PROPERTY rather than a rendering decision so a caller cannot forget it.
        """
        return (
            self.state == "finished"
            and self.compacted_messages <= 0
            and not self.result_truncated
            and not self.bounded
            and not self.error
        )

    @property
    def completeness_note(self) -> str:
        """Return the sentence a reader needs when the transcript is partial."""
        if self.state == "running":
            return (
                "this subagent is still running, so its transcript is the "
                "prefix of a conversation, not the conversation"
            )
        if self.compacted_messages > 0:
            return (
                f"this subagent's context was COMPACTED and {self.compacted_messages} "
                "message(s) were dropped. What follows is NOT the whole "
                "conversation; the dropped turns are summarised in the child's "
                "own handoff."
            )
        if self.result_truncated:
            return (
                "the child's RESULT was truncated to fit the parent's budget, "
                "so it is a summary of an answer, not the answer"
            )
        if self.bounded:
            return (
                "this subagent stopped against a bound rather than finishing, so "
                "its transcript is a prefix of an attempt that did not complete: "
                + self.bound_line
            )
        if self.error:
            return (
                "this subagent recorded a failure, so the transcript below is the "
                "record of an attempt that broke rather than an answer"
            )
        return ""


# ---------------------------------------------------------------------------
# Trust boundary
# ---------------------------------------------------------------------------


def trust_boundary(
    parent: Optional[Mapping[str, Any]] = None,
    child: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return what a child inherits from its parent, in both directions.

    ``parent`` may carry ``deny`` / ``allow`` / ``approval`` / ``budget`` /
    ``network`` rule lists. The projection is deliberately asymmetric: denies
    are inherited, allowances are not, and the receipt says so per rule so a
    user reading it can see WHY the child can do less than the parent.
    """
    parent_rules = dict(parent or {})
    child_rules = dict(child or {})
    inherited: Dict[str, Tuple[str, ...]] = {}
    not_inherited: Dict[str, Tuple[str, ...]] = {}
    for key in ("deny", "allow", "approval", "budget", "network"):
        items = tuple(str(item) for item in (parent_rules.get(key) or ()) if str(item))
        if key == "deny":
            inherited[key] = items
            continue
        if key == "budget":
            # A ceiling is inherited as a ceiling: the child cannot spend more,
            # and may spend less. It is "inherited" for the narrower direction.
            inherited[key] = items
            continue
        if items:
            not_inherited[key] = items
    denied_by_child_only = tuple(
        str(item) for item in (child_rules.get("deny") or ()) if str(item)
    )
    # ``can`` may only ever be derived from groups that are GRANTS. Deriving it
    # from every inherited group put a parent's DENIED rule and a budget
    # ceiling into the same column as a permission, so one rule appeared in
    # BOTH columns of one receipt. Under deny_rules_only no allowance is
    # inherited, so ``can`` is empty BY CONSTRUCTION and the note says so --
    # an empty column that names its reason is not the same as a missing one.
    inherited_grants: Dict[str, Tuple[str, ...]] = {
        key: items for key, items in inherited.items() if key not in ("deny", "budget")
    }
    withheld = (
        set(inherited.get("deny", ()))
        | set(inherited.get("budget", ()))
        | set(denied_by_child_only)
    )
    can = sorted(
        {item for items in inherited_grants.values() for item in items} - withheld - {"*"}
    )
    if not can:
        can_note = (
            "nothing is inherited as a grant: this model inherits denies and "
            "ceilings only, so a child holds no inherited permission"
        )
    else:
        can_note = "inherited as a grant, and not denied or capped by the parent"
    return {
        "model": "deny_rules_only",
        "inherited": {key: list(value) for key, value in inherited.items()},
        "not_inherited": {key: list(value) for key, value in not_inherited.items()},
        "child_only_denies": list(denied_by_child_only),
        "rules": [{"rule": rule, "effect": effect} for rule, effect in INHERITANCE_RULES],
        "can": can,
        "can_note": can_note,
        "cannot": sorted(
            {item for items in not_inherited.values() for item in items}
            | set(denied_by_child_only)
            | set(inherited.get("deny", ()))
        ),
    }


def assert_trust_columns_disjoint(boundary: Mapping[str, Any]) -> None:
    """Fail if one rule is reported as both permitted and refused.

    A receipt that lists the same string under ``can`` and ``cannot`` has no
    answer for the reader who asks what the child may do, which is the only
    question the two columns exist to answer.
    """
    can = {str(item) for item in (boundary.get("can") or ())}
    cannot = {str(item) for item in (boundary.get("cannot") or ())}
    # The deny check runs FIRST and is the more specific finding: an inherited
    # deny is always also in ``cannot``, so with the overlap checked first this
    # branch was unreachable -- an arm nobody can fire is not coverage.
    denied = {str(item) for item in (boundary.get("inherited", {}) or {}).get("deny", ())}
    leaked = sorted(can & denied)
    if leaked:
        raise AssertionError(
            "trust boundary reports a DENIED rule as permitted: "
            + ", ".join(leaked)
        )
    both = sorted(can & cannot)
    if both:
        raise AssertionError(
            "trust boundary reports the same rule in both directions: "
            + ", ".join(both)
            + " - a reader cannot act on a rule that is both allowed and refused"
        )


def trust_boundary_lines(boundary: Mapping[str, Any]) -> List[str]:
    """Render a subagent's trust boundary in both directions."""
    out: List[str] = [
        f"TRUST BOUNDARY ({_line(boundary.get('model'))}): the parent may do "
        "less than it allowed itself, and that is the safe direction"
    ]
    inherited = boundary.get("inherited") or {}
    for key in sorted(inherited):
        items = inherited.get(key) or []
        out.append(f"  INHERITED {key}: " + (", ".join(_line(i) for i in items) or "none"))
    not_inherited = boundary.get("not_inherited") or {}
    for key in sorted(not_inherited):
        items = not_inherited.get(key) or []
        out.append(
            f"  NOT INHERITED {key}: " + (", ".join(_line(i) for i in items) or "none")
            + "  <- the parent holding this does NOT give it to the child"
        )
    for item in boundary.get("rules") or ():
        out.append(f"  rule {_line(item.get('rule'))}: {_line(item.get('effect'))}")
    can = list(boundary.get("can") or ())
    out.append(
        "  MAY DO: " + (", ".join(_line(i) for i in can) or "nothing")
        + f"  <- {_line(boundary.get('can_note'))}"
    )
    cannot = list(boundary.get("cannot") or ())
    out.append(
        "  MAY NOT: " + (", ".join(_line(i) for i in cannot) or "nothing recorded")
    )
    return out


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def assert_isolation_after_steering(
    *,
    parent_messages_before: int,
    parent_messages_after: int,
    parent_chars_before: int,
    parent_chars_after: int,
    child_messages_before: int,
    child_messages_after: int,
) -> None:
    """Raise if a steer pulled the child's transcript into the parent.

    Both directions are checked, and both are required:

    * the parent must not GROW (steering must not leak the child upward), and
    * the child must GROW (a steer that changed nothing is a dead channel, and
      a dead channel is worse than no channel because a user believes their
      instruction arrived).

    A test that only asserted the first would pass on a steering implementation
    that silently discards the message, which is the failure mode this receipt
    exists to make visible.
    """
    if int(parent_messages_after) != int(parent_messages_before) or int(
        parent_chars_after
    ) != int(parent_chars_before):
        raise IsolationBroken(
            "isolation broken: the parent transcript changed across a steer "
            f"(messages {parent_messages_before}->{parent_messages_after}, "
            f"chars {parent_chars_before}->{parent_chars_after}). Steering must "
            "deliver downward only."
        )
    if int(child_messages_after) <= int(child_messages_before):
        raise IsolationBroken(
            "the steer reached the child but added nothing to its transcript "
            f"(messages {child_messages_before}->{child_messages_after}); a "
            "steering channel that silently discards a message is a lie"
        )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class SubagentStore:
    """Durable per-session subagents: identity, transcript, result, steering.

    Layout, under ``<root>/<session_id>/``:

    * ``<agent_id>.json``      - the record, atomically rewritten
    * ``<agent_id>.transcript`` - append-only JSONL, one message per line

    The transcript is append-only and separate from the record for the same
    reason ``harness/trace.jsonl`` is separate from ``state.json``: a
    concurrent reader of a rewritten record must never see a half-written one.
    """

    def __init__(self, root: Any, session_id: str) -> None:
        self.root = Path(root or "logs/_subagents")
        self.session_id = str(session_id or "unscoped")
        self.dir = self.root / self.session_id
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # a read-only store is a named condition
            raise IsolationBroken(f"subagent store is not writable: {exc}") from exc

    # -- paths ---------------------------------------------------------
    def record_path(self, agent_id: str) -> Path:
        """Return the record path for one subagent."""
        return self.dir / f"{_safe(agent_id)}.json"

    def transcript_path(self, agent_id: str) -> Path:
        """Return the transcript path for one subagent."""
        return self.dir / f"{_safe(agent_id)}.transcript"

    # -- reads ---------------------------------------------------------
    def ids(self) -> Tuple[str, ...]:
        """Return every subagent id in this session, oldest first."""
        found: List[Tuple[float, str]] = []
        for path in self.dir.glob("*.json"):
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            agent_id = str(data.get("agent_id") or path.stem)
            found.append((float(data.get("spawned_at") or 0.0), agent_id))
        found.sort()
        return tuple(agent_id for _, agent_id in found)

    def get(self, agent_id: str) -> Optional[SubagentRecord]:
        """Return one subagent, or ``None`` when there is no such subagent.

The transcript counters are RE-DERIVED from the bytes on disk rather
        than read back from the record. A record written by a process that has
        since exited carries whatever counters it had at write time, so reading
        them back would give a second answer to "how much is in this
        transcript" - the drift this module's own docstring warns about, and the
        reason the very first draft of this method returned a record whose
        ``transcript_messages`` was 0 while two messages were on disk.

        ``bound_line`` and ``completeness_note`` were added in P4/W2 because a
        ``--json`` document that omitted them was, for a script, a DIFFERENT
        answer than the text a person reads: the text said ``STOPPED ON A BOUND``
        and the document said nothing. A machine-readable surface that carries
        less than the human one cannot be used to check it.
        """
        data = _read_json(self.record_path(agent_id))
        if not isinstance(data, dict):
            return None
        return self._refresh(SubagentRecord.from_dict(data))

    def all(self) -> Tuple[SubagentRecord, ...]:
        """Return every subagent in this session, oldest first."""
        records = [self.get(agent_id) for agent_id in self.ids()]
        return tuple(item for item in records if item is not None)

    def transcript(self, agent_id: str) -> List[Dict[str, Any]]:
        """Return one subagent's transcript messages, in order.

        A torn final line (a writer killed mid-append) is reported as a message
        with ``torn: True`` rather than dropped, because a silently shorter
        transcript is indistinguishable from a transcript with less in it.
        """
        path = self.transcript_path(agent_id)
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                out.append({"role": "system", "content": stripped, "torn": True})
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    # -- writes --------------------------------------------------------
    def spawn(
        self,
        *,
        description: str,
        agent: str = "",
        role: str = "",
        parent_rules: Optional[Mapping[str, Any]] = None,
        node_id: str = "",
    ) -> SubagentRecord:
        """Create a subagent with an identity, a transcript, and no result yet."""
        record = SubagentRecord(
            agent_id=new_agent_id(),
            agent=str(agent or "inline"),
            role=str(role or ""),
            description=_line(description),
            parent_session_id=self.session_id,
            node_id=str(node_id or ""),
            spawned_at=time.time(),
            state="running",
            trust=trust_boundary(parent_rules),
        )
        self._save(record)
        self._append(
            record.agent_id,
            {
                "role": "system",
                "content": (
                    f"subagent {record.agent_id} opened for: {record.description}"
                ),
                "at": record.spawned_at,
            },
        )
        record = self._refresh(record)
        return record

    def append_turn(
        self,
        agent_id: str,
        *,
        role: str,
        text: str,
        tool: str = "",
        compacted: bool = False,
    ) -> SubagentRecord:
        """Append one turn to a subagent's transcript.

        ``compacted=True`` marks a compaction event: the message is kept and
        the dropped count is accumulated, which is what makes the transcript
        render as partial rather than as complete.
        """
        record = self.get(agent_id)
        if record is None:
            raise IsolationBroken(f"no such subagent: {agent_id}")
        self._append(
            agent_id,
            {
                "role": str(role or "assistant"),
                "content": _line(text),
                "tool": str(tool or ""),
                "at": time.time(),
            },
        )
        if compacted:
            record.compacted_messages += 1
            record.compacted_at = time.time()
            if record.state == "running":
                record.state = "compacted"
        record.turns += 1
        self._save(record)
        return self._refresh(record)

    def finish(
        self,
        agent_id: str,
        *,
        result: str,
        follow_ups: Sequence[str] = (),
        truncated: bool = False,
        state: str = "finished",
        stopped_because: Any = "",
        bound_detail: str = "",
        parent_received: Any = "",
    ) -> SubagentRecord:
        """Record a subagent's RESULT, which is what makes it returnable.

        A follow-up on a finished subagent is what the esveo workflow needs:
        *"went back to the same subagent to get more details"*. ``follow_ups``
        names what it asked for, so the reason for the return is on the record.

        ``stopped_because`` is the field that makes a bounded subagent legible.
        P4/W2 added it because ``harness.agent_kernel.subagents`` already records
        a bound on every bounded run and nothing carried it here: the receipt said
        ``failed`` and left a reader to guess between turns, budget, and a model
        error. It is narrowed by :func:`normalize_bound`, so a producer may pass
        the harness's own ``"model_error: <text>"`` form and get the kind plus a
        retrievable detail.

        ``parent_received`` records WHAT THE PARENT GOT, which is not the child's
        transcript. A parent that received a one-paragraph summary of ninety
        turns is the normal case, and a receipt that showed both sides is how a
        reader tells a summary from a transcript.
        """
        record = self.get(agent_id)
        if record is None:
            raise IsolationBroken(f"no such subagent: {agent_id}")
        if state not in AGENT_STATES:
            raise ValueError(f"unknown subagent state {state!r}; expected one of {AGENT_STATES}")
        bound = normalize_bound(stopped_because)
        record.result = _line(result)
        record.result_truncated = bool(truncated)
        record.follow_ups = tuple(str(item) for item in follow_ups)
        record.state = str(state)
        record.stopped_because = bound
        if bound:
            raw = str(stopped_because or "")
            record.bound_detail = str(bound_detail or "").strip() or (
                raw.split(":", 1)[1].strip() if ":" in raw else ""
            )
            # A bounded run is never rendered as a completed one, whatever state
            # the caller passed. Wave 1 read `state` and a bounded subagent whose
            # producer said `finished` listed as finished.
            record.state = "failed" if state == "finished" else str(state)
        if parent_received:
            text = _line(parent_received)
            record.parent_received = text
            record.parent_received_chars = len(text)
        if record.compacted_messages <= 0:
            record.state = record.state if bound else str(state)
        self._append(
            agent_id,
            {"role": "assistant", "content": record.result, "at": time.time(), "result": True},
        )
        self._save(record)
        return self._refresh(record)

    def steer(self, agent_id: str, text: str, *, parent_session: Any = None) -> "SteerReceipt":
        """Send a human instruction INTO a subagent, downward only.

        The receipt carries the parent's transcript size before and after, so the
        isolation property is checkable by the caller rather than asserted in a
        docstring. This is the orchestrator workflow: *"you can go in and fix
        things when a subagent gets stuck"* - and the fix must not cost the
        parent its context.
        """
        record = self.get(agent_id)
        if record is None:
            raise IsolationBroken(f"no such subagent: {agent_id}")
        parent_before = _transcript_size(parent_session)
        child_before = self._refresh(record)
        child_messages_before = int(child_before.transcript_messages)
        self._append(
            agent_id,
            {
                "role": "user",
                "content": _line(text),
                "at": time.time(),
                "steering": True,
            },
        )
        updated = self._refresh(record)
        updated.steers = (*updated.steers, _line(text))
        self._save(updated)
        after = self._refresh(updated)
        parent_after = _transcript_size(parent_session)
        assert_isolation_after_steering(
            parent_messages_before=parent_before[0],
            parent_messages_after=parent_after[0],
            parent_chars_before=parent_before[1],
            parent_chars_after=parent_after[1],
            child_messages_before=child_messages_before,
            child_messages_after=int(after.transcript_messages),
        )
        return SteerReceipt(
            agent_id=agent_id,
            text=_line(text),
            parent_messages_before=parent_before[0],
            parent_messages_after=parent_after[0],
            child_messages_before=child_messages_before,
            child_messages_after=int(after.transcript_messages),
            isolation="intact",
            parent_measured=_parent_session_measured(parent_session),
            parent_received=after.parent_received,
            parent_received_chars=int(after.parent_received_chars),
        )

    # -- internals -----------------------------------------------------
    def _append(self, agent_id: str, message: Mapping[str, Any]) -> None:
        """Append one message to a subagent's transcript, flushed."""
        path = self.transcript_path(agent_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(message), ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise IsolationBroken(
                f"could not append to the subagent transcript: {exc}"
            ) from exc

    def _save(self, record: SubagentRecord) -> None:
        """Write a record atomically."""
        path = self.record_path(record.agent_id)
        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True, default=str)
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(str(temporary), str(path))
        except OSError as exc:
            raise IsolationBroken(f"could not write the subagent record: {exc}") from exc

    def _refresh(self, record: SubagentRecord) -> SubagentRecord:
        """Recompute the transcript counters FROM the transcript on disk.

        The counters are derived rather than accumulated, so a transcript that
        was appended to by another process still reports the truth. A counter
        kept incrementally is a second answer to "how much is in this
        transcript", which is the drift this repository keeps paying for.
        """
        messages = self.transcript(record.agent_id)
        record.transcript_messages = len(messages)
        record.transcript_chars = sum(
            len(str(item.get("content") or "")) for item in messages
        )
        return record


def _safe(value: Any) -> str:
    """Return a filesystem-safe segment for a subagent identity."""
    text = "".join(
        char if (char.isalnum() or char in "-_") else "-" for char in str(value or "sub")
    )
    return text.strip("-") or "sub"


def _read_json(path: Path) -> Any:
    """Read one JSON file, returning ``None`` on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _parent_session_measured(session: Any) -> bool:
    """Return whether a parent transcript was actually MEASURED.

    Wave 1's receipt rendered ``parent transcript: 0 -> 0 message(s)`` for a
    steer called with no parent, which is indistinguishable from a parent that
    genuinely has an empty transcript - and the empty case is the one that would
    hide a leak. A measurement that did not happen says so; "0" is a number
    nobody took.
    """
    return isinstance(session, (Mapping, list, tuple))


def _transcript_size(session: Any) -> Tuple[int, int]:
    """Return ``(messages, chars)`` for a parent transcript of any shape."""
    if isinstance(session, Mapping):
        turns = list(session.get("turns") or ())
    elif isinstance(session, (list, tuple)):
        turns = list(session)
    else:
        turns = []
    chars = 0
    for turn in turns:
        if isinstance(turn, Mapping):
            chars += len(str(turn.get("text") or turn.get("content") or ""))
        else:
            chars += len(str(turn))
    return len(turns), chars


@dataclass(frozen=True)
class SteerReceipt:
    """What one steer did, with the isolation property made checkable."""

    agent_id: str
    text: str
    parent_messages_before: int
    parent_messages_after: int
    child_messages_before: int
    child_messages_after: int
    isolation: str = "intact"
    #: Whether the parent transcript was MEASURED at all. P4/W2 added this
    #: because ``0 -> 0`` reads as "the parent is empty", and an unmeasured
    #: parent is the case in which a leak would be invisible.
    parent_measured: bool = True
    #: WHAT THE PARENT GOT from this subagent, if the caller recorded it. The
    #: child's work is the child's; the parent sees a summary, and saying which
    #: summary is what makes the two sides comparable.
    parent_received: str = ""
    parent_received_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "agent_id": self.agent_id,
            "text": self.text,
            "parent_messages_before": int(self.parent_messages_before),
            "parent_messages_after": int(self.parent_messages_after),
            "child_messages_before": int(self.child_messages_before),
            "child_messages_after": int(self.child_messages_after),
            "isolation": self.isolation,
            "parent_measured": bool(self.parent_measured),
            "parent_received": self.parent_received,
            "parent_received_chars": int(self.parent_received_chars),
        }

    def lines(self) -> List[str]:
        """Render the receipt, including the isolation numbers.

        The isolation property is only meaningful when the parent was MEASURED,
        so the two are rendered as one statement rather than as a number and a
        claim the number cannot support.
        """
        out = [
            f"STEERED {self.agent_id}: {_line(self.text)}",
            f"  child transcript: {self.child_messages_before} -> "
            f"{self.child_messages_after} message(s) (the steer landed)",
        ]
        if self.parent_measured:
            out.append(
                f"  parent transcript: {self.parent_messages_before} -> "
                f"{self.parent_messages_after} message(s) (unchanged: isolation "
                f"{self.isolation})"
            )
        else:
            out.append(
                "  parent transcript: NOT MEASURED - no parent transcript was "
                "supplied, so isolation was NOT checked here. '0 -> 0' would "
                "have been a number nobody took."
            )
        if self.parent_received:
            out.append(
                f"  what the parent has from this subagent: "
                f"{self.parent_received_chars} char(s) of summary - the child's "
                f"{self.child_messages_after} message(s) stayed in the child"
            )
        else:
            out.append(
                "  what the parent has from this subagent: NOT RECORDED by this "
                "steer, so the parent's view of the child cannot be stated here"
            )
        return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def subagent_list_lines(store: SubagentStore) -> List[str]:
    """Render every subagent in the session, including the zero case.

    The marker column is the whole point of this surface and Wave 1 got it
    wrong. It rendered ``"finished"`` whenever ``record.complete`` held and the
    raw state otherwise, so a subagent whose context had been COMPACTED listed
    as ``finished`` - the same word as a subagent that ran to completion - and
    the only place the loss was visible was the transcript detail, one command
    deeper. The three-session run measured it:

        -> sub-001a10f944d69-0d9253 [reviewer/reviewer] finished

    for a child that had dropped a compaction event and had its result
    truncated. Four states are now distinguished in one word each, so a list of
    subagents cannot be read as a list of successes.
    """
    records = store.all()
    out: List[str] = [f"SUBAGENTS in session {store.session_id} ({len(records)}):"]
    if not records:
        out.append(
            "  no subagent has been spawned in this session. That is NONE, not "
            "'none finished' - a subagent list with no rows is not a green list."
        )
        return out
    for record in records:
        out.append(f"  -> {record.agent_id} [{list_marker(record)}]")
        out.append(
            f"       {_line(record.agent or 'inline')}/{_line(record.role or 'unspecified')}"
            f"  -  {_line(record.description or 'no description recorded')}"
        )
        out.append(
            f"       {record.turns} turn(s), {record.transcript_messages} "
            f"message(s), {len(record.steers)} steer(s)"
        )
        if record.bounded:
            out.append(f"       STOPPED ON A BOUND: {_line(record.bound_line)}")
        if record.compacted_messages > 0:
            out.append(
                f"       CONTEXT COMPACTED: {record.compacted_messages} message(s) "
                "dropped from this child's own window, so its transcript is NOT "
                "the whole conversation"
            )
        if record.result_truncated:
            out.append(
                "       RESULT TRUNCATED: the parent's copy is a summary of an "
                "answer, not the answer"
            )
        if record.error:
            out.append(f"       FAILURE RECORDED: {_line(record.error)}")
        if record.parent_received:
            out.append(
                f"       the PARENT received {record.parent_received_chars} "
                f"char(s) - a summary, not this child's "
                f"{record.transcript_chars} char(s) of transcript"
            )
        if record.follow_ups:
            out.append(
                f"       RETURNED TO after finishing, asking: "
                f"{_line('; '.join(record.follow_ups))}"
            )
    return out


def list_marker(record: SubagentRecord) -> str:
    """Return the one word a subagent's row carries.

    The order is most-specific first, because the specific states are the ones a
    reader must not miss: a bounded run and a compacted transcript both render
    as ``finished`` under Wave 1's rule, and ``finished`` is the word that means
    "do not worry about this one".
    """
    if record.bounded:
        return f"stopped on a bound ({record.stopped_because})"
    if record.error:
        return f"failed ({record.state})"
    if record.compacted_messages > 0:
        return f"finished, CONTEXT COMPACTED (-{record.compacted_messages} message(s))"
    if record.result_truncated:
        return "finished, RESULT TRUNCATED"
    if record.complete:
        return "finished"
    return str(record.state or "unknown")


def subagent_transcript_lines(store: SubagentStore, agent_id: str) -> List[str]:
    """Render one subagent's transcript, and say whether it is the whole thing."""
    record = store.get(agent_id)
    if record is None:
        return [
            f"SUBAGENT {agent_id}: no such subagent in session {store.session_id}"
        ]
    out: List[str] = [
        f"SUBAGENT {record.agent_id} transcript ({_line(record.agent or 'inline')})"
    ]
    out.append(f"  {_line(record.description or 'no description recorded')}")
    out.append(
        f"  turns: {record.turns}   messages: {record.transcript_messages}   "
        f"chars: {record.transcript_chars}   steers: {len(record.steers)}"
    )
    out.append(
        f"  COMPLETE: {record.complete}  (state={_line(record.state)}, "
        f"compacted_messages={record.compacted_messages}, "
        f"result_truncated={record.result_truncated}, "
        f"stopped_because={_line(record.stopped_because or 'none')}, "
        f"error={'recorded' if record.error else 'none'})"
    )
    if record.bounded:
        out.append(f"  !! STOPPED ON A BOUND: {_line(record.bound_line)}")
        out.append(
            "     what follows is the record of an attempt that did not complete. "
            "The bound is a limit of the run, not a conclusion about the task."
        )
    if record.error:
        out.append(f"  !! FAILURE RECORDED: {_line(record.error)}")
    note = record.completeness_note
    if note:
        out.append(f"  !! {_line(note)}")
    if record.compacted_messages > 0:
        out.append(
            f"  HOW TO RETRIEVE WHAT WAS COMPACTED: the child's own transcript "
            f"file {_line(store.transcript_path(record.agent_id))} holds every "
            "message that was written; what compaction dropped is the child's "
            "CONTEXT WINDOW, not the record. Re-running the child with a larger "
            "window is the only way to see the dropped turns as they were."
        )
    messages = store.transcript(record.agent_id)
    if not messages:
        if record.error or record.bounded:
            out.append(
                "  (no transcript on disk. This subagent FAILED or STOPPED, which "
                "is NOT a finding of nothing: the failure above is the finding, "
                "and an empty transcript is not evidence the child was quiet.)"
            )
        else:
            out.append(
                "  (no transcript on disk - this subagent was spawned but recorded "
                "no messages, which is NOT the same as an empty conversation)"
            )
    for item in messages:
        torn = " [TORN LINE]" if item.get("torn") else ""
        steering = " [steering]" if item.get("steering") else ""
        result = " [result]" if item.get("result") else ""
        tool = f" [tool={_line(item.get('tool'))}]" if item.get("tool") else ""
        out.append(
            f"  <{_line(item.get('role') or 'unknown')}>{tool}{steering}{result}{torn} "
            f"{_line(item.get('content') or '')}"
        )
    if record.error:
        out.append(f"  error recorded: {_line(record.error)}")
    return out


def subagent_detail_lines(store: SubagentStore, agent_id: str) -> List[str]:
    """Render one subagent's record plus its trust boundary."""
    record = store.get(agent_id)
    if record is None:
        return [f"SUBAGENT {agent_id}: no such subagent in session {store.session_id}"]
    out = [f"SUBAGENT {record.agent_id}"]
    out.append(f"  agent: {_line(record.agent or 'inline')}   role: {_line(record.role or 'unspecified')}")
    out.append(
        f"  state: {_line(record.state)}   complete transcript: {record.complete}   "
        f"list marker: {_line(list_marker(record))}"
    )
    out.append(f"  node id: {_line(record.node_id or 'none recorded')}")
    if record.bounded:
        out.append(f"  !! STOPPED ON A BOUND: {_line(record.bound_line)}")
    if record.error:
        out.append(f"  !! FAILURE RECORDED: {_line(record.error)}")
    out.append(
        f"  transcript: {record.transcript_messages} message(s), "
        f"{record.transcript_chars} char(s)"
    )
    if record.parent_received:
        out.append(
            f"  what the PARENT received: {record.parent_received_chars} char(s) - "
            "a summary of the child's work, not the child's transcript"
        )
    else:
        out.append(
            "  what the PARENT received: NOT RECORDED - this surface does not know "
            "whether the parent got a summary or nothing at all"
        )
    out.append(f"  result: {_line(record.result or 'no result recorded yet')}")
    for item in steering_history_lines(store, record.agent_id):
        out.append(f"  {item}")
    for item in trust_boundary_lines(record.trust or trust_boundary()):
        out.append(f"  {item}")
    return out


def steering_history_lines(store: "SubagentStore", agent_id: str) -> List[str]:
    """Render who steered this subagent, when, and with what.

    Attribution, not just a count. A steer is a human interrupting a machine, so
    a transcript showing ``3 steer(s)`` and no attribution cannot answer the only
    question a reader has about one - *who changed the answer, and what did they
    say* - and a steer message is content that arrived from outside the child, so
    it is the one place a subagent's transcript carries a third party's words.

    The messages are read back from the TRANSCRIPT, not from the record's
    ``steers`` tuple, because the transcript is what a reader can follow and the
    tuple is what the record remembers.
    """
    if store.get(agent_id) is None:
        return []
    steers = [item for item in store.transcript(agent_id) if item.get("steering")]
    if not steers:
        return [
            "STEERING HISTORY: none recorded - nobody has redirected this "
            "subagent, which is not the same as nobody having wanted to"
        ]
    out: List[str] = [
        f"STEERING HISTORY: {len(steers)} human instruction(s) delivered INTO "
        "this subagent, downward only:"
    ]
    for index, item in enumerate(steers, start=1):
        stamp = item.get("at")
        when = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(stamp)))
            if isinstance(stamp, (int, float)) and float(stamp) > 0
            else "UNKNOWN TIME"
        )
        # The author is the OPERATOR, by construction: only ``steer`` writes a
        # message carrying this marker, and steer is the human-facing channel.
        # Saying so is what makes the row an attribution rather than a guess.
        out.append(
            f"    {index}. by: the operator (a human, through the steering "
            f"channel)   at: {when}"
        )
        out.append(f"       said: {_line(item.get('content') or '')}")
    out.append(
        f"    these {len(steers)} message(s) came FROM the operator INTO the child; "
        "none of them pulled child content back up into the parent"
    )
    return out


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Drive the store from the shell so the surface is reachable by hand."""
    args = list(sys.argv[1:] if argv is None else argv)
    store_root = "logs/_subagents"
    session = "manual"
    positional: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--store" and index + 1 < len(args):
            store_root = args[index + 1]
            index += 2
            continue
        if token == "--session" and index + 1 < len(args):
            session = args[index + 1]
            index += 2
            continue
        positional.append(token)
        index += 1
    store = SubagentStore(store_root, session)
    verb = (positional[0] if positional else "list").lower()
    if verb == "spawn":
        record = store.spawn(
            description=" ".join(positional[1:]) or "manual spawn",
            agent="manual",
        )
        for line in subagent_detail_lines(store, record.agent_id):
            print(line)
        return 0
    if verb == "steer" and len(positional) >= 2:
        receipt = store.steer(positional[1], " ".join(positional[2:]))
        for line in receipt.lines():
            print(line)
        return 0
    if verb == "transcript" and len(positional) >= 2:
        for line in subagent_transcript_lines(store, positional[1]):
            print(line)
        return 0
    if verb == "show" and len(positional) >= 2:
        for line in subagent_detail_lines(store, positional[1]):
            print(line)
        return 0
    for line in subagent_list_lines(store):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_cli())
