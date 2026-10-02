"""Rolling conversation memory owned by the agent kernel's strategies.

The kernel's daily strategy must not rebuild a fresh ``[system, user]`` pair
on every turn: a fact discovered on turn 1 (a filename, a command's output,
a failing assertion) has to still be in the model's own message list on turn
5. This module owns that rolling list, the context budget that bounds it, and
the structured handoff that replaces turns dropped past the budget.

Contract:

- The base frame (system prompt + the opening user message) is seeded once per
  run and then never rebuilt. Live workspace facts that change mid-run are
  appended as additional conversation turns, not substituted into the base.
- Every retained turn keeps its role, its content, and the structured facts
  the handoff needs (tool name, outcome, target path).
- History is bounded by a message count and a character budget. When history
  exceeds the budget the OLDEST turns are dropped and folded into a
  :class:`ConversationHandoff`, which is rendered as one structured user
  message ahead of the retained turns. The most recent tool result is always
  retained verbatim, even if that alone would exceed the budget.
- History is *also* bounded by a token budget (see ``harness/agent_kernel/
  budget.py``). :meth:`ConversationMemory.compact_tokens` drops the oldest
  turns with a model-written handover summary when a request approaches the
  provider window, and returns a reversible receipt naming the dropped journal
  sequences.
- **Compaction never touches the cached prefix.** The base frame is the part of
  the request a provider can serve from cache and the only part nothing else
  restates, so a compaction that summarised it away would re-create the cache
  entry every turn. :meth:`cache_prefix_identity` measures it before and after,
  the receipt carries both digests, and a plan that reached into it is
  structurally impossible.
- **Every compaction is a replayable record.** :class:`CondensationRecord`
  names each dropped message (identity plus content digest, never the text - the
  journal holds that) plus the replacement summary verbatim, and
  :func:`reconstruct_prior_view` rebuilds the exact pre-compaction request from
  it. Nothing is destructively rewritten.
- **A compaction that reclaims nothing is refused, not retried.** Three
  consecutive no-progress compactions raise
  :class:`harness.agent_kernel.budget.CompactionThrashError`, which the kernel
  reports as a ``failed`` run carrying the reason.
- Every retained turn can be mirrored into a durable journal through the
  ``journal`` sink, and :meth:`ConversationMemory.restore` rebuilds an exact
  prior state from it. That is what makes a three-way rewind (conversation
  only / files only / both) exact instead of approximate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .budget import (
    DEFAULT_COMPACTION_THRASH_LIMIT,
    DEFAULT_MIN_RECLAIM_TOKENS,
    CompactionThrashError,
    CompactionThrashPolicy,
    CondensationRecord,
    DroppedMessage,
    compaction_thrash_policy_from_config,
    plan_drop,
    prefix_identity,
)

DEFAULT_MAX_MESSAGES = 48
DEFAULT_MAX_CHARS = 24000
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 4000
DEFAULT_HANDOFF_CHARS = 4000
DEFAULT_HANDOFF_ITEMS = 12


@dataclass
class ConversationTurn:
    """One retained conversation turn plus the facts a handoff summarizes."""

    role: str
    content: str
    turn: int = 0
    kind: str = "note"
    tool: str = ""
    ok: Optional[bool] = None
    target: str = ""
    #: Monotonic position in the durable journal. It is what makes a compaction
    #: reversible: the receipt names the dropped sequences, so restoring means
    #: re-reading exactly those records instead of replaying the whole session.
    seq: int = 0

    @property
    def chars(self) -> int:
        """Return the character cost of this turn."""
        return len(self.content) + len(self.tool) + len(self.target)

    def to_message(self) -> Dict[str, str]:
        """Return the provider-shaped message for this turn."""
        return {"role": self.role, "content": self.content}

    def to_record(self) -> Dict[str, Any]:
        """Return the full journal-shaped record, content included."""
        return {
            "seq": int(self.seq or 0),
            "role": self.role,
            "kind": self.kind,
            "turn": int(self.turn or 0),
            "tool": self.tool,
            "ok": self.ok,
            "target": self.target,
            "content": self.content,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ConversationTurn":
        """Rebuild one turn from a journal record, tolerating a missing field."""
        values = dict(record or {})
        ok = values.get("ok")
        return cls(
            role=str(values.get("role") or "user"),
            content=str(values.get("content") or ""),
            turn=int(values.get("turn") or 0),
            kind=str(values.get("kind") or "note"),
            tool=str(values.get("tool") or ""),
            ok=None if ok is None else bool(ok),
            target=str(values.get("target") or ""),
            seq=int(values.get("seq") or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a serializable projection used by the turn ledger."""
        return {
            "role": self.role,
            "kind": self.kind,
            "turn": self.turn,
            "tool": self.tool,
            "ok": self.ok,
            "target": self.target,
            "chars": self.chars,
            "digest": _digest(self.content),
        }


@dataclass
class ConversationHandoff:
    """Structured summary of the turns dropped past the context budget."""

    first_turn: int = 0
    last_turn: int = 0
    dropped: int = 0
    findings: List[str] = field(default_factory=list)
    files_touched: List[str] = field(default_factory=list)
    tool_counts: Dict[str, int] = field(default_factory=dict)
    failures: int = 0
    #: Model-written handover summaries, newest last. These are what actually
    #: replace the dropped turns' content; the structured fields above are the
    #: deterministic floor that survives even when no summary could be produced.
    summaries: List[str] = field(default_factory=list)

    def absorb(self, turn: ConversationTurn) -> None:
        """Fold one dropped turn into this handoff."""
        self.dropped += 1
        if self.first_turn == 0:
            self.first_turn = turn.turn
        self.last_turn = max(self.last_turn, turn.turn)
        if turn.tool:
            self.tool_counts[turn.tool] = self.tool_counts.get(turn.tool, 0) + 1
        if turn.ok is False:
            self.failures += 1
        if turn.target and turn.target not in self.files_touched:
            self.files_touched.append(turn.target)
        line = _finding_line(turn)
        if line and line not in self.findings:
            self.findings.append(line)

    def add_summary(self, text: str, *, max_chars: int = 4000) -> bool:
        """Record one model-written handover summary; return whether it landed."""
        body = str(text or "").strip()
        if not body:
            return False
        if body in self.summaries:
            return False
        self.summaries.append(_bound(body, max(200, int(max_chars))))
        return True

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "ConversationHandoff":
        """Rebuild a handoff from its serialized projection."""
        data = dict(values or {})

        def _int(key: str) -> int:
            try:
                return int(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            first_turn=_int("first_turn"),
            last_turn=_int("last_turn"),
            dropped=_int("dropped"),
            findings=[str(item) for item in data.get("findings") or ()],
            files_touched=[str(item) for item in data.get("files_touched") or ()],
            tool_counts={
                str(key): int(value or 0)
                for key, value in dict(data.get("tool_counts") or {}).items()
            },
            failures=_int("failures"),
            summaries=[str(item) for item in data.get("summaries") or ()],
        )

    def merge(self, other: "ConversationHandoff") -> "ConversationHandoff":
        """Merge a later handoff into this one, preserving order."""
        if other.dropped:
            if self.first_turn == 0:
                self.first_turn = other.first_turn
            self.last_turn = max(self.last_turn, other.last_turn)
            self.dropped += other.dropped
            self.failures += other.failures
            for name, count in other.tool_counts.items():
                self.tool_counts[name] = self.tool_counts.get(name, 0) + count
            for item in other.findings:
                if item not in self.findings:
                    self.findings.append(item)
            for item in other.files_touched:
                if item not in self.files_touched:
                    self.files_touched.append(item)
        for item in other.summaries:
            self.add_summary(item)
        return self

    def as_dict(self) -> Dict[str, Any]:
        """Return a serializable projection of this handoff."""
        return {
            "first_turn": self.first_turn,
            "last_turn": self.last_turn,
            "dropped": self.dropped,
            "findings": list(self.findings),
            "files_touched": list(self.files_touched),
            "tool_counts": dict(self.tool_counts),
            "failures": self.failures,
            "summaries": list(self.summaries),
        }

    def render(self, max_chars: int = DEFAULT_HANDOFF_CHARS) -> str:
        """Render the handoff as one structured user message body."""
        if self.dropped <= 0 and not self.summaries:
            return ""
        lines = [
            "## Compacted earlier turns (structured handoff)",
            f"Turns {self.first_turn}-{self.last_turn} were dropped from the "
            f"context budget ({self.dropped} messages, {self.failures} failed). "
            "Their findings are preserved here; the full records stay in this "
            "run's trace.jsonl and turns.jsonl.",
        ]
        if self.tool_counts:
            summary = ", ".join(
                f"{name} x{count}" for name, count in sorted(self.tool_counts.items())
            )
            lines.append(f"Tools invoked: {summary}")
        if self.files_touched:
            lines.append("Paths touched: " + ", ".join(self.files_touched[:20]))
        if self.summaries:
            lines.append("Compaction summary of the dropped turns:")
            lines.extend(f"- {item}" for item in self.summaries[-3:])
        if self.findings:
            lines.append("Findings:")
            lines.extend(f"- {item}" for item in self.findings[-DEFAULT_HANDOFF_ITEMS:])
        else:
            lines.append("Findings: (none recorded)")
        return _bound("\n".join(lines), max_chars)


class ConversationMemory:
    """Own one run's rolling message list, budget, and compaction handoff.

    Assumes ``seed`` is called once with the base ``[system, user]`` frame the
    context builder produced for turn 1. Every later call to
    :meth:`render` returns that same base plus the retained history, so a
    prior turn's content can never disappear simply because the run moved on.

    A durable ``journal`` sink, when supplied, receives one record per
    retained turn (``seq``, ``turn``, ``kind``, and the full content) plus the
    base frame. That journal - not this object - is what makes the rolling list
    and every compaction reversible: this object deliberately keeps only the
    bounded window, so a 5,000-turn session costs the same memory as a 5-turn
    one.
    """

    def __init__(
        self,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
        handoff_max_chars: int = DEFAULT_HANDOFF_CHARS,
        journal: Optional[Callable[[Dict[str, Any]], None]] = None,
        compaction_thrash_limit: int = DEFAULT_COMPACTION_THRASH_LIMIT,
        compaction_min_reclaim_tokens: int = DEFAULT_MIN_RECLAIM_TOKENS,
        contents: Optional[Callable[[int], Optional[str]]] = None,
    ) -> None:
        self.max_messages = max(4, int(max_messages))
        self.max_chars = max(1000, int(max_chars))
        self.max_tool_output_chars = max(200, int(max_tool_output_chars))
        self.handoff_max_chars = max(512, int(handoff_max_chars))
        self.journal = journal
        #: Optional resolver for a dropped message's content BY JOURNAL
        #: SEQUENCE. The content is not retained here on purpose - retaining it
        #: would make this object grow as fast as the history it bounds - so
        #: reconstructing a prior view needs a source for it. With no resolver
        #: :meth:`reconstruct_prior_view` answers ``None``, which is the honest
        #: "cannot reconstruct" rather than a partial view dressed as a whole
        #: one. The kernel's ``ConversationJournal`` is the intended resolver.
        self._contents = contents
        self._base: List[Dict[str, str]] = []
        self._history: List[ConversationTurn] = []
        self._handoff = ConversationHandoff()
        self._state_digest = ""
        self.total_recorded = 0
        self._seq = 0
        self.compactions: List[Dict[str, Any]] = []
        #: One inspectable record per TOKEN-budget compaction. Deliberately not
        #: populated by the character/message fold: that fold runs on every
        #: append once the budget is hit, and a per-append record would make this
        #: object grow exactly as fast as the history it bounds. The fold's
        #: condensation rides its journal row instead.
        self.condensations: List[CondensationRecord] = []
        self._thrash = CompactionThrashPolicy(
            limit=max(0, int(compaction_thrash_limit)),
            min_reclaim_tokens=max(0, int(compaction_min_reclaim_tokens)),
        )
        self._no_progress_streak = 0
        self.thrash_error: Optional[CompactionThrashError] = None

    def configure_compaction(self, config: Optional[Mapping[str, Any]] = None) -> None:
        """Adopt the run's compaction-thrash policy from its config mapping.

        The keys are read by KEY MEANING and absent means "use the documented
        default", which is why this is a separate call rather than a constructor
        argument the strategy has to remember: it is one line, it is additive,
        and a run that never calls it still gets the same bounded behaviour the
        constructor default provides.
        """
        policy = compaction_thrash_policy_from_config(config)
        self._thrash = policy
        self._no_progress_streak = 0
        self.thrash_error = None

    @property
    def thrash_policy(self) -> CompactionThrashPolicy:
        """Return the active thrash policy (its limit is the abort budget)."""
        return self._thrash

    @property
    def no_progress_streak(self) -> int:
        """Return consecutive compactions that reclaimed nothing."""
        return int(self._no_progress_streak)

    @property
    def seeded(self) -> bool:
        """Return whether the base frame has been established."""
        return bool(self._base)

    @property
    def handoff(self) -> ConversationHandoff:
        """Return the accumulated compaction handoff."""
        return self._handoff

    @property
    def dropped_messages(self) -> int:
        """Return how many history messages were folded into the handoff."""
        return self._handoff.dropped

    def seed(self, messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
        """Establish the base frame once; later calls are ignored.

        Returns the rendered message list so a caller can use this as a
        drop-in replacement for a direct ``context_builder.build`` result.
        """
        if not self.seeded:
            self._base = [
                {
                    "role": str(item.get("role") or "user"),
                    "content": str(item.get("content") or ""),
                }
                for item in messages
            ] or [
                {"role": "user", "content": ""},
            ]
            self._emit(
                {"record": "base", "seq": 0, "turn": 0, "messages": list(self._base)}
            )
        return self.render()

    def record_assistant(
        self,
        text: str,
        tool_calls: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        turn: int = 0,
    ) -> None:
        """Record the model's own reply for this turn."""
        calls = list(tool_calls or [])
        summary = str(text or "").strip()
        if calls:
            names = ", ".join(
                str(item.get("tool") or item.get("name") or "tool") for item in calls
            )
            summary = f"{summary}\n[typed tool calls: {names}]".strip()
        if not summary:
            return
        self._append(
            ConversationTurn(
                role="assistant",
                content=_bound(summary, self.max_tool_output_chars),
                turn=turn,
                kind="assistant",
            )
        )

    def record_tool_result(
        self,
        tool: str,
        ok: bool,
        output: Any,
        *,
        turn: int = 0,
        target: str = "",
    ) -> None:
        """Record one tool outcome, keeping the most recent output verbatim."""
        body = _stringify(output)
        content = (
            f"TOOL RESULT {tool} ({'ok' if ok else 'error'}):\n"
            f"{body}\nContinue with one typed tool call or finish."
        )
        self._append(
            ConversationTurn(
                role="user",
                content=_bound(content, self.max_tool_output_chars),
                turn=turn,
                kind="tool_result",
                tool=str(tool or ""),
                ok=bool(ok),
                target=str(target or ""),
            )
        )

    def record_note(self, text: str, *, turn: int = 0) -> None:
        """Record harness feedback (recovery, validation, steering) as a turn."""
        body = str(text or "").strip()
        if not body:
            return
        self._append(
            ConversationTurn(
                role="user",
                content=_bound(body, self.max_tool_output_chars),
                turn=turn,
                kind="note",
            )
        )

    def record_state(self, digest: str, text: str, *, turn: int = 0) -> bool:
        """Append live workspace state only when it materially changed."""
        value = str(digest or "")
        if value and value == self._state_digest:
            return False
        self._state_digest = value
        body = str(text or "").strip()
        if not body:
            return False
        self._append(
            ConversationTurn(
                role="user",
                content=_bound(body, self.max_tool_output_chars),
                turn=turn,
                kind="state",
            )
        )
        return True

    def history_digest(self) -> str:
        """Return a stable digest of the retained history for replay checks."""
        payload = "\n".join(
            f"{item.role}|{item.kind}|{item.tool}|{_digest(item.content)}"
            for item in self._history
        )
        return _digest(payload)

    def turn_facts(self) -> List[Dict[str, Any]]:
        """Return serializable facts for every retained turn."""
        return [item.to_dict() for item in self._history]

    def render(
        self, extra_messages: Sequence[Mapping[str, Any]] = ()
    ) -> List[Dict[str, str]]:
        """Return the bounded message list for the next model request.

        ``extra_messages`` are appended after the retained history without being
        stored, which is how the constraint re-injection rides the end of a long
        context without accumulating one block per turn.
        """
        self._compact()
        messages = [dict(item) for item in self._base]
        handoff_text = self._handoff.render(self.handoff_max_chars)
        if handoff_text:
            messages.append({"role": "user", "content": handoff_text})
        messages.extend(item.to_message() for item in self._history)
        for item in extra_messages or ():
            messages.append(
                {
                    "role": str(item.get("role") or "user"),
                    "content": str(item.get("content") or ""),
                }
            )
        return messages

    def protected_prefix(self) -> int:
        """Return how many leading rendered messages must never be compacted."""
        return len(self._base) + (
            1 if self._handoff.render(self.handoff_max_chars) else 0
        )

    def base_messages(self) -> List[Dict[str, str]]:
        """Return the seeded base frame - the request's cached, byte-stable head.

        This is the region a provider prompt cache can reuse and the only region
        nothing else in the request restates, so it is what "compact the growth,
        not the prefix" protects. It is returned as copies: a caller must not be
        able to mutate the head through this accessor.
        """
        return [dict(item) for item in self._base]

    def cache_prefix(self) -> List[Dict[str, str]]:
        """Alias of :meth:`base_messages` named for the cache it protects."""
        return self.base_messages()

    def cache_prefix_identity(self, *, tools: Optional[Sequence[Any]] = None) -> Any:
        """Return the measured identity of the cached prefix.

        Two measurements with the same ``sha256`` are the same bytes, so a
        receipt can state "the cached head was untouched" as a fact instead of a
        promise. ``tools`` may carry the request's tool schemas so the digest
        matches the one the provider's own cache accounting would compute.
        """
        return prefix_identity(self._base, tools=tools)

    def condensation(self, compaction_id: str) -> Optional[CondensationRecord]:
        """Return one recorded condensation by id, or ``None`` when absent."""
        wanted = str(compaction_id or "").strip()
        if not wanted:
            return None
        for record in self.condensations:
            if record.compaction_id == wanted:
                return record
        return None

    def reconstruct_prior_view(
        self, condensation: Any
    ) -> Optional[List[Dict[str, str]]]:
        """Rebuild the pre-compaction request from a record plus this journal.

        The dropped CONTENT is not held here (that is the point of the journal),
        so this needs the contents the journal recorded. When the dropped content
        is unavailable the answer is ``None`` - an honest "cannot reconstruct",
        never a partial view that reads as a whole one.
        """
        record = (
            condensation
            if isinstance(condensation, CondensationRecord)
            else CondensationRecord.from_dict(condensation or {})
        )
        contents = self._recorded_contents
        if contents is None:
            return None
        return reconstruct_prior_view(
            record,
            base_messages=self.base_messages(),
            live_messages=self.render(),
            contents=contents,
            live_handoff=self._handoff.as_dict(),
        )

    @property
    def _recorded_contents(self) -> Optional[Dict[int, str]]:
        """Return ``{seq: content}`` for every dropped message, or ``None``.

        Resolved lazily and ONLY for the sequences a condensation actually
        dropped, so a run that never compacts never pays for this and a run that
        compacts a hundred times holds a hundred short entries rather than the
        whole session. One unresolvable sequence fails the WHOLE reconstruction:
        a prior view with a hole in it is not a prior view.
        """
        if self._contents is None:
            return None
        wanted: Dict[int, str] = {}
        for record in self.condensations:
            for item in record.dropped:
                if int(item.seq) in wanted:
                    continue
                try:
                    value = self._contents(int(item.seq))
                except Exception:
                    value = None
                if value is None:
                    return None
                wanted[int(item.seq)] = str(value)
        return wanted

    def bind_contents(self, resolver: Optional[Callable[[int], Optional[str]]]) -> None:
        """Bind (or unbind) the by-sequence content resolver used for replay."""
        self._contents = resolver

    def snapshot(self) -> Dict[str, Any]:
        """Return the exact retained state needed to restore this memory.

        The snapshot is bounded (base frame + retained history + handoff), which
        is exactly the state ``restore`` must reproduce. The full history lives
        in the durable journal, not here.
        """
        return {
            "base": [dict(item) for item in self._base],
            "history": [item.to_record() for item in self._history],
            "handoff": self._handoff.as_dict(),
            "state_digest": self._state_digest,
            "total_recorded": int(self.total_recorded),
            "seq": int(self._seq),
            "condensations": [item.as_dict() for item in self.condensations],
            "thrash": {
                "limit": int(self._thrash.limit),
                "min_reclaim_tokens": int(self._thrash.min_reclaim_tokens),
                "no_progress_streak": int(self._no_progress_streak),
                "aborted": bool(self.thrash_error is not None),
                "reason": str(self.thrash_error.reason) if self.thrash_error else "",
            },
        }

    def restore(self, snapshot: Mapping[str, Any]) -> List[Dict[str, str]]:
        """Restore an exact prior state produced by :meth:`snapshot`.

        Rewind and compaction rollback both land here: the caller re-derives the
        snapshot from the durable journal and this rebuilds the in-memory list,
        so a restored run renders byte-identical requests.
        """
        values = dict(snapshot or {})
        self._base = [
            {
                "role": str(item.get("role") or "user"),
                "content": str(item.get("content") or ""),
            }
            for item in values.get("base") or ()
        ]
        self._history = [
            ConversationTurn.from_record(item) for item in values.get("history") or ()
        ]
        handoff = values.get("handoff")
        self._handoff = (
            ConversationHandoff.from_dict(handoff)
            if isinstance(handoff, Mapping)
            else ConversationHandoff()
        )
        self._state_digest = str(values.get("state_digest") or "")
        try:
            self.total_recorded = int(values.get("total_recorded") or 0)
        except (TypeError, ValueError):
            self.total_recorded = len(self._history)
        try:
            self._seq = max(int(values.get("seq") or 0), 0)
        except (TypeError, ValueError):
            self._seq = 0
        self.condensations = [
            CondensationRecord.from_dict(item)
            for item in values.get("condensations") or ()
            if isinstance(item, Mapping)
        ]
        thrash = values.get("thrash")
        if isinstance(thrash, Mapping):
            try:
                self._thrash = CompactionThrashPolicy(
                    limit=max(0, int(thrash.get("limit", self._thrash.limit))),
                    min_reclaim_tokens=max(
                        0,
                        int(
                            thrash.get(
                                "min_reclaim_tokens", self._thrash.min_reclaim_tokens
                            )
                        ),
                    ),
                )
                self._no_progress_streak = max(
                    0, int(thrash.get("no_progress_streak") or 0)
                )
            except (TypeError, ValueError):
                pass
        return self.render()

    def compact_tokens(
        self,
        *,
        limit_tokens: int,
        summarize: Callable[[str, Sequence[ConversationTurn]], Tuple[str, str]],
        estimator: Any,
        protect_tail: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Compact the oldest droppable turns when the token budget is exceeded.

        Returns ``None`` when no compaction was needed or nothing was droppable.
        Otherwise returns the reversible compaction receipt: what was dropped, by
        which journal sequence, how many tokens were reclaimed, which summarizer
        method produced the replacement, what survived, whether the cached prefix
        came through byte-identical, and the replayable
        :class:`~harness.agent_kernel.budget.CondensationRecord` that
        reconstructs the prior view.

        ``summarize`` receives the rendered transcript plus the dropped turns and
        returns ``(summary_text, method)``. It owns the primary/fallback model
        chain; this method never calls a model itself. An empty summary is not a
        failure: the structured handoff still carries the deterministic facts, and
        the receipt says which method actually ran.

        **Raises** :class:`~harness.agent_kernel.budget.CompactionThrashError`
        when the compaction reclaimed nothing and the streak of such
        compactions reaches the configured limit. The condensation and its
        journal row are written FIRST, so the abort leaves the evidence a reader
        needs, and the run ends ``failed`` with the reason rather than
        compacting again.
        """
        if self.thrash_error is not None:
            # Already aborted. Re-raise rather than compact again: a guard whose
            # answer changes between calls is not a guard.
            raise self.thrash_error
        if limit_tokens <= 0:
            return None
        messages = self.render()
        before = int(estimator.messages_tokens(messages))
        if before < int(limit_tokens):
            return None
        prefix = self.protected_prefix()
        cache_prefix = self.cache_prefix_identity()

        plan = plan_drop(
            messages,
            protect=range(prefix),
            reclaim_tokens=0,
            estimator=estimator,
            protect_tail=protect_tail,
        )
        if not plan.indexes:
            return self._note_blocked_attempt(
                before,
                limit_tokens=int(limit_tokens),
                reason=(
                    "the request is over its compaction threshold but every "
                    "message in it is protected (the base frame, the handoff, "
                    "and the newest turn), so no compaction can shrink it"
                ),
                cache_prefix=cache_prefix,
            )
        offsets = [index - prefix for index in plan.indexes]
        droppable = [
            item for offset, item in enumerate(self._history) if offset in set(offsets)
        ]
        if not droppable:
            return self._note_blocked_attempt(
                before,
                limit_tokens=int(limit_tokens),
                reason=(
                    "the drop plan selected no retained turn, so a compaction "
                    "would replace nothing and change nothing"
                ),
                cache_prefix=cache_prefix,
            )
        handoff_before = self._handoff.as_dict()
        transcript = render_dropped_transcript(droppable)
        method = "none"
        summary = ""
        error = ""
        try:
            summary, method = summarize(transcript, tuple(droppable))
        except Exception as exc:  # a summarizer must never end the run
            error = str(exc)
            summary, method = "", "summarizer_error"
        first_turn = min((item.turn for item in droppable), default=0)
        last_turn = max((item.turn for item in droppable), default=0)
        seqs = [item.seq for item in droppable]
        for item in droppable:
            self._handoff.absorb(item)
        if summary:
            self._handoff.add_summary(summary, max_chars=self.handoff_max_chars)
            method = method or "model_summary"
        self._history = [item for item in self._history if item.seq not in set(seqs)]
        after_messages = self.render()
        after = int(estimator.messages_tokens(after_messages))
        cache_prefix_after = self.cache_prefix_identity()
        compaction_id = f"compact-{len(self.compactions) + 1}"
        condensation = CondensationRecord(
            compaction_id=compaction_id,
            method=method or ("structural_trim" if not summary else "model_summary"),
            dropped=tuple(_dropped_message(item) for item in droppable),
            summary=str(summary or ""),
            summary_sha256=_digest(summary or ""),
            summary_chars=len(summary or ""),
            before_tokens=before,
            after_tokens=after,
            reclaimed_tokens=max(0, before - after),
            limit_tokens=int(limit_tokens),
            first_turn=first_turn,
            last_turn=last_turn,
            handoff_before=handoff_before,
            cache_prefix_messages=int(cache_prefix.messages or 0),
            cache_prefix_sha256_before=cache_prefix.sha256,
            cache_prefix_sha256_after=cache_prefix_after.sha256,
            cache_prefix_preserved=(
                bool(cache_prefix.sha256)
                and cache_prefix.sha256 == cache_prefix_after.sha256
            ),
            # The first index a compaction may touch: the base frame plus the
            # handoff. That is a DIFFERENT boundary from
            # ``cache_prefix_messages``, which is the provider's cache
            # breakpoint, and keeping the two distinct is the point - the handoff
            # is safe to replace precisely because nothing in the provider's
            # cache keys on it.
            body_from=int(prefix),
            protected=tuple(plan.protected),
            error=error,
        )
        receipt: Dict[str, Any] = {
            "compaction_id": compaction_id,
            "method": condensation.method,
            "before_tokens": before,
            "after_tokens": after,
            "reclaimed_tokens": condensation.reclaimed_tokens,
            "limit_tokens": int(limit_tokens),
            "dropped_messages": len(droppable),
            "dropped_seqs": seqs,
            "first_turn": first_turn,
            "last_turn": last_turn,
            "summary_chars": len(summary or ""),
            "summary_digest": condensation.summary_sha256,
            "error": error,
            # The cached head is measured, not asserted. A compaction that
            # changed it would invalidate a provider's cache on this turn AND
            # every turn after, which looks perfectly healthy in the cost
            # report until the hit rate is compared.
            "cache_prefix": {
                "before": cache_prefix.as_dict(),
                "after": cache_prefix_after.as_dict(),
                "preserved": condensation.cache_prefix_preserved,
                "body_from": condensation.body_from,
            },
            "condensation": condensation.as_dict(),
            "survived": {
                "handoff_dropped": self._handoff.dropped,
                "handoff_summaries": len(self._handoff.summaries),
                "handoff_files": list(self._handoff.files_touched[-20:]),
                "retained_turns": [item.turn for item in self._history],
                "retained_messages": len(self._history),
                "last_tool_result_retained": bool(
                    self._history and self._history[-1].kind == "tool_result"
                )
                or not any(item.kind == "tool_result" for item in droppable),
            },
        }
        self.compactions.append(receipt)
        self.condensations.append(condensation)
        self._emit(
            {
                "record": "compaction",
                "seq": self._seq,
                "turn": last_turn,
                "compaction_id": compaction_id,
                "method": condensation.method,
                "dropped_seqs": seqs,
                "handoff": self._handoff.as_dict(),
                "before_tokens": before,
                "after_tokens": after,
                "reclaimed_tokens": condensation.reclaimed_tokens,
                "summary_digest": condensation.summary_sha256,
                "cache_prefix_preserved": condensation.cache_prefix_preserved,
                "condensation": condensation.as_dict(),
                "survived": dict(receipt["survived"]),
            }
        )
        self._note_compaction_progress(condensation, receipt)
        return receipt

    def _note_blocked_attempt(
        self,
        before: int,
        *,
        limit_tokens: int,
        reason: str,
        cache_prefix: Any,
    ) -> None:
        """Count a compaction that COULD not run, and refuse a run of them.

        This is the other half of thrash, and the half a real session actually
        hits: a request that sits above its compaction threshold while every
        message in it is protected - typically because the compiled base frame
        alone is larger than the window. Compaction is then called once per turn
        and can never change anything, which spends a turn each time to produce
        the same request. The run is told, and an unbroken run of them aborts.

        A single blocked attempt is NOT an abort: it returns ``None`` so the
        caller reports an ordinary skip, because one turn whose only droppable
        message is the protected newest turn is ordinary, not a fault.
        """
        receipt = {
            "compaction_id": "",
            "method": "blocked",
            "before_tokens": int(before),
            "after_tokens": int(before),
            "reclaimed_tokens": 0,
            "limit_tokens": int(limit_tokens),
            "cache_prefix_messages": int(getattr(cache_prefix, "messages", 0) or 0),
        }
        self._note_compaction_progress(None, receipt, reason=reason)
        return None

    def _note_compaction_progress(
        self,
        condensation: Optional[CondensationRecord],
        receipt: Dict[str, Any],
        *,
        reason: str = "",
    ) -> None:
        """Count one compaction's progress and abort a thrashing run.

        The streak resets on the first compaction that reclaims anything, so
        only an unbroken run of compactions that change nothing can trip the
        guard. The abort is written to the journal before it is raised, so the
        evidence outlives the exception.
        """
        self._no_progress_streak = self._thrash.note(
            receipt, streak=self._no_progress_streak
        )
        decided = self._thrash.decide(self._no_progress_streak)
        if not decided:
            return
        detail = reason or decided
        error = CompactionThrashError(
            detail,
            streak=self._no_progress_streak,
            limit=int(self._thrash.limit),
            min_reclaim_tokens=int(self._thrash.min_reclaim_tokens),
            receipt=receipt,
        )
        self.thrash_error = error
        self._emit(
            {
                "record": "compaction_thrash",
                "seq": self._seq,
                "turn": int(
                    getattr(condensation, "last_turn", 0)
                    or receipt.get("last_turn")
                    or 0
                ),
                "compaction_id": str(receipt.get("compaction_id") or ""),
                **error.as_dict(),
            }
        )
        raise error

    def reset(self) -> None:
        """Clear the rolling memory, keeping the configured budgets."""
        self._base = []
        self._history = []
        self._handoff = ConversationHandoff()
        self._state_digest = ""
        self.total_recorded = 0
        self._seq = 0
        self.compactions = []
        self.condensations = []
        self._no_progress_streak = 0
        self.thrash_error = None

    # -- internals --------------------------------------------------------

    def _append(self, item: ConversationTurn) -> None:
        self._seq += 1
        item.seq = self._seq
        self._history.append(item)
        self.total_recorded += 1
        self._compact()
        self._emit(
            {
                "record": "turn",
                "seq": item.seq,
                "turn": item.turn,
                "kind": item.kind,
                "role": item.role,
                "tool": item.tool,
                "ok": item.ok,
                "target": item.target,
                "content": item.content,
            }
        )

    def _emit(self, record: Dict[str, Any]) -> None:
        if self.journal is None:
            return
        try:
            self.journal(dict(record))
        except Exception:
            # A journal is evidence, not the run's outcome. Losing a record must
            # never end the run, and it is reported by the journal's own
            # warning list rather than by an exception here.
            return

    def _compact(self) -> None:
        dropped: List[int] = []
        folded: List[ConversationTurn] = []
        handoff_before: Optional[Dict[str, Any]] = None
        first_turn = 0
        last_turn = 0

        def _absorb(item: ConversationTurn) -> None:
            # Captured once, immediately before the FIRST absorb, so the record
            # carries the handoff as it was before this fold. Capturing it at
            # the top of the call would make every append pay for a projection
            # of a handoff that is not being folded into.
            nonlocal handoff_before
            if handoff_before is None:
                handoff_before = self._handoff.as_dict()
            self._handoff.absorb(item)

        while len(self._history) > self.max_messages:
            item = self._history.pop(0)
            _absorb(item)
            dropped.append(item.seq)
            folded.append(item)
            first_turn = item.turn if not first_turn else min(first_turn, item.turn)
            last_turn = max(last_turn, item.turn)
        total = sum(item.chars for item in self._history)
        while total > self.max_chars and len(self._history) > 1:
            # The most recent turn is never dropped: a fact discovered on the
            # last turn is the one the model most needs to see next.
            item = self._history.pop(0)
            _absorb(item)
            dropped.append(item.seq)
            folded.append(item)
            total -= item.chars
            first_turn = item.turn if not first_turn else min(first_turn, item.turn)
            last_turn = max(last_turn, item.turn)
        if dropped:
            # A character/message fold is a compaction by another name, so it is
            # journaled the same way and is reversible by the same mechanism -
            # including the dropped-message record, so the two compaction paths
            # are inspectable in one shape. It is NOT appended to
            # ``self.condensations``: this runs on every append once the budget
            # is hit, and a per-append record would make the in-memory object
            # grow exactly as fast as the history it is bounding.
            cache_prefix = self.cache_prefix_identity()
            condensation = CondensationRecord(
                compaction_id=f"fold-{last_turn}-{dropped[0]}",
                method="budget_fold",
                dropped=tuple(_dropped_message(item) for item in folded),
                handoff_before=handoff_before or {},
                cache_prefix_messages=int(cache_prefix.messages or 0),
                cache_prefix_sha256_before=cache_prefix.sha256,
                cache_prefix_sha256_after=cache_prefix.sha256,
                cache_prefix_preserved=True,
                # Same meaning as the token path: the first index a compaction
                # may touch. Measured BEFORE the fold absorbed anything, so the
                # handoff is not yet counted against the protected region.
                body_from=len(self._base) + (1 if handoff_before is None else 0),
                first_turn=first_turn,
                last_turn=last_turn,
            )
            self._emit(
                {
                    "record": "compaction",
                    "seq": self._seq,
                    "turn": last_turn,
                    "compaction_id": condensation.compaction_id,
                    "method": "budget_fold",
                    "dropped_seqs": dropped,
                    "handoff": self._handoff.as_dict(),
                    "first_turn": first_turn,
                    "last_turn": last_turn,
                    "cache_prefix_preserved": True,
                    "condensation": condensation.as_dict(),
                }
            )


def _dropped_message(turn: ConversationTurn) -> DroppedMessage:
    """Project one dropped turn into the record's identity-only shape.

    The content is deliberately not projected. The record is held in memory for
    the life of the run and the content is what makes a session big; the journal
    is where the content lives, and the digest is what proves a reconstruction
    came back byte-exact.
    """
    return DroppedMessage(
        seq=int(turn.seq or 0),
        turn=int(turn.turn or 0),
        kind=str(turn.kind or "note"),
        role=str(turn.role or "user"),
        tool=str(turn.tool or ""),
        ok=turn.ok,
        target=str(turn.target or ""),
        chars=int(turn.chars),
        content_sha256=_digest(turn.content),
    )


def contents_by_seq(rows: Sequence[Mapping[str, Any]]) -> Dict[int, str]:
    """Return ``{seq: content}`` for a conversation journal's turn rows.

    This is the content half of a replayable condensation, read from the durable
    journal rather than from the in-memory conversation (which deliberately does
    not keep it). It is a pure function of the rows, so a test can reconstruct a
    prior view from a file on disk without a live run.
    """
    resolved: Dict[int, str] = {}
    for row in rows or ():
        if str(dict(row or {}).get("record") or "") != "turn":
            continue
        try:
            seq = int(dict(row).get("seq") or 0)
        except (TypeError, ValueError):
            continue
        resolved[seq] = str(dict(row).get("content") or "")
    return resolved


def reconstruct_prior_view(
    condensation: Any,
    *,
    base_messages: Sequence[Mapping[str, Any]],
    live_messages: Sequence[Mapping[str, Any]],
    contents: Mapping[int, str],
    live_handoff: Optional[Mapping[str, Any]] = None,
) -> Optional[List[Dict[str, str]]]:
    """Rebuild the request as it stood immediately before one compaction.

    Returns the base frame, the handoff as it was BEFORE this condensation (so
    the fold the compaction performed on it is undone, not compounded), the
    dropped messages in journal order, and the messages the live view still
    carries. The result is the exact prior view, which is what makes a
    condensation inspectable rather than merely recorded.

    ``live_handoff`` is the CURRENT handoff state, because the live view's copy
    of the handoff message is the POST-compaction one and has to be replaced
    rather than appended after. Passing it is what makes the ordering correct
    when the compaction was the first thing to create a handoff - the case where
    a naive slice would duplicate it.

    ``None`` when any dropped message's content is unavailable. A prior view
    with a hole in it is not a prior view, and a partial answer here would be
    indistinguishable from a whole one to a caller that never checked.
    """
    record = (
        condensation
        if isinstance(condensation, CondensationRecord)
        else CondensationRecord.from_dict(condensation or {})
    )
    rebuilt: List[Dict[str, str]] = [
        {
            "role": str(item.get("role") or "user"),
            "content": str(item.get("content") or ""),
        }
        for item in base_messages or ()
    ]
    handoff = ConversationHandoff.from_dict(record.handoff_before or {})
    handoff_text = handoff.render()
    if handoff_text:
        rebuilt.append({"role": "user", "content": handoff_text})
    for item in sorted(record.dropped, key=lambda entry: int(entry.seq or 0)):
        content = contents.get(int(item.seq))
        if content is None:
            return None
        rebuilt.append({"role": str(item.role or "user"), "content": str(content)})
    base_count = len(base_messages or ())
    live = list(live_messages or ())
    after_handoff = bool(
        ConversationHandoff.from_dict(dict(live_handoff or {})).render()
    )
    rebuilt.extend(
        dict(item) for item in live[base_count + (1 if after_handoff else 0) :]
    )
    return rebuilt


def _finding_line(turn: ConversationTurn) -> str:
    if turn.kind == "tool_result":
        state = "ok" if turn.ok else "error"
        where = f" on {turn.target}" if turn.target else ""
        return f"{turn.tool}{where} -> {state}"
    if turn.kind == "assistant":
        head = " ".join(turn.content.split())[:180]
        return f"model said: {head}" if head else ""
    if turn.kind == "state":
        head = " ".join(turn.content.split())[:180]
        return f"workspace state: {head}" if head else ""
    head = " ".join(turn.content.split())[:180]
    return f"note: {head}" if head else ""


def render_dropped_transcript(
    turns: Sequence[ConversationTurn], *, max_chars: int = 24000
) -> str:
    """Render the turns a compaction is about to drop, for the summarizer.

    Each line is prefixed with its origin so the summarizer can tell an assistant
    claim from a tool result, and the whole block is bounded by the caller so a
    compaction can never itself build an over-window request.
    """
    lines: List[str] = []
    for item in turns or ():
        head = " ".join(item.content.split())
        lines.append(
            f"[turn {item.turn} {item.kind}"
            + (f" {item.tool}" if item.tool else "")
            + f" {item.role}] {head}"
        )
    return _bound("\n".join(lines), max(400, int(max_chars)))


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}: {item}" for key, item in value.items()) + "}"
    return str(value)


def _bound(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[compacted]"


def _digest(value: str) -> str:
    return hashlib.sha256(
        str(value or "").encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def state_digest(changed_files: Sequence[str], diff: str) -> str:
    """Return a digest of the live workspace state for change detection."""
    files = ",".join(sorted({str(item) for item in changed_files if item}))
    return _digest(f"{files}\n{diff or ''}")


def render_workspace_state(
    changed_files: Sequence[str], diff: str, limit: int = 4000
) -> str:
    """Render the bounded live workspace state a turn should carry."""
    files = sorted({str(item) for item in changed_files if item})
    lines = ["## Live workspace state (updated mid-run)"]
    lines.append("Changed files so far: " + (", ".join(files) or "(none)"))
    if diff:
        lines.append("Diff so far (bounded):\n" + _bound(diff, max(200, limit - 200)))
    else:
        lines.append("Diff so far: (none)")
    return "\n".join(lines)


def split_roles(messages: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
    """Return the ``(system, user)`` base pair from a context bundle."""
    system = ""
    user = ""
    for item in messages:
        role = str(item.get("role") or "user")
        content = str(item.get("content") or "")
        if role == "system" and not system:
            system = content
        elif role == "user" and not user:
            user = content
    return system, user
