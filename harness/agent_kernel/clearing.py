"""Tool-result CLEARING — the lossless lever that runs before compaction.

**Three levers, routinely conflated. They are not the same operation and they
cost wildly different amounts.**

============  ==============================  ==============  ==============
lever         operation                       loss            cost
============  ==============================  ==============  ==============
compaction    whole transcript -> summary     lossy BY DESIGN one model call
clearing      old ``tool_result`` -> placeholder LOSSLESS      NO model call
memory        information leaves the window   none            none
============  ==============================  ==============  ==============

Anthropic's own framing: *"Once a tool has been called deep in the message
history, the agent rarely needs to see the raw result again ... If the agent does
need the data, it just calls the tool again."* Clearing is *"one of the safest
lightest-touch forms of compaction."*

**Why this module exists rather than being folded into compaction.** Compaction
fires at ``context_compaction_fraction`` (0.6) of the window, which is LATE — by
then the context is two thirds full of bytes that re-fetching would replace for
free. Clearing fires EARLIER and CHEAPER, and because it is lossless it lets the
run compact *less often*. Running clearing first means you lose less, and you
pay no model call to do it. The ordering is the whole argument, and it is
asserted in :mod:`harness.agent_kernel.strategy`.

**The correctness crux is the classification.** A tool result that CANNOT be
re-fetched must never be cleared:

* a **test verdict** (``test``, ``build``, ``lint``, ``typecheck``) is a
  one-off computation — re-running it may give a different answer, and it is
  the only record of what the verifier said;
* a **web** result (``web_fetch``, ``web_search``) is not reproducible now;
* ``verify`` and ``git_*`` reads are point-in-time facts;
* anything this run **authored** (``memory_record``) is not a re-fetch of
  anything.

Only results whose source is a **pure read of durable state** are cleared:
``read``, ``list``, ``grep``, ``glob``, and the read-only ``git_*`` reads
(``git_log``/``git_show``/``git_blame``/``git_branch``/``git_status``/
``git_worktree``). An **unrecognised** tool is PROTECTED. The asymmetry is the
point: a tool this module has never heard of might compute something, and
clearing a non-re-fetchable result is silent data loss that no receipt can undo
for the reader.

**Clearing is a VIEW TRANSFORM, never a deletion.** The bytes stay in the
conversation journal; :attr:`ClearingReceipt.reversible` names the resolver, and
:meth:`ConversationMemory.reconstruct_prior_view` is the proof that the dropped
text is still readable by journal sequence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The closed vocabulary of what a clearing did. ``skipped`` is a real outcome,
#: not an error: a run whose window is not full clears nothing.
CLEARING_METHODS: Tuple[str, ...] = (
    "cleared",  # at least one re-fetchable result was replaced
    "skipped",  # nothing was eligible (window fine, or nothing re-fetchable)
    "disabled",  # the operator turned it off
)

#: Why one tool was NOT cleared. A receipt that says "not cleared" with no
#: reason is indistinguishable from a bug, so every protected tool names itself.
PROTECTED_REASONS: Tuple[str, ...] = (
    "newest",  # the newest tool result is always live
    "not_refetchable",  # a one-off computation, a verdict, or point-in-time
    "unclassified_tool",  # this module has never heard of it -> protect
    "already_placeholder",  # cleared on an earlier turn
    "too_small",  # below the byte floor; clearing would not pay for itself
    "no_tool_name",  # a turn with no tool name cannot be classified
    # P3a/W2: the turn carries image/video attachment blocks. The transform
    # replaces `content` only, so clearing the text of such a turn would leave
    # the attachment in the request and produce a receipt that claims bytes it
    # did not reclaim. Checked STRUCTURALLY on the turn rather than inferred
    # from the tool name, so widening the allow-list cannot arm the trap.
    "carries_attachments",
)

#: Tools whose result is a PURE READ OF DURABLE STATE: the same call tomorrow
#: returns the same bytes, so the agent can re-fetch instead of carrying them.
#:
#: This is the allow-list. Everything else is protected, including anything
#: unrecognised. Read it as "these are the only tools I can prove are
#: re-fetchable", NOT as "everything not listed is refetchable".
REFETCHABLE_TOOLS: frozenset = frozenset(
    {
        "read",
        "list",
        "grep",
        "glob",
        "find_definition",
        "find_references",
        "read_symbol",
        "blast_radius",
        "git_log",
        "git_show",
        "git_blame",
        "git_branch",
        "git_status",
        "git_worktree",
        "git_ls_files",
    }
)

#: Tools whose result must never be cleared, named explicitly so a reader can
#: see the reasoning rather than infer it from the absence. These are the
#: subclasses of "cannot be re-fetched": a computation, a verdict, a point-in-time
#: observation, a network result, or something this run authored.
#:
#: **P3a/W2: this table is CATALOG-COMPLETE.** It previously named 33 tools
#: while the shipped catalog carries 45, so 12 real tools — including the three
#: the phase brief names explicitly (``process_read_output``, ``memory``,
#: ``mcp``) — were protected only by the ``unclassified_tool`` fallback. That
#: fallback is the safe direction, so nothing was lost; but a table that leans
#: on its own fallback for a third of the catalog is not a classification a
#: reviewer can check, and it silently becomes a weaker one the moment somebody
#: widens the allow-list. :func:`catalog_coverage` in
#: :mod:`harness.clearing_lossless` now asserts 45/45 and names any gap, so a
#: new catalog tool cannot ship unclassified.
NON_REFETCHABLE_TOOLS: Dict[str, str] = {
    # A verifier verdict. Re-running may produce a DIFFERENT answer, and this is
    # the only record of what the verifier said on this tree.
    "test": "test_verdict",
    "build": "build_verdict",
    "lint": "check_verdict",
    "typecheck": "check_verdict",
    "verify": "verification_verdict",
    # Point-in-time repository state.
    "git_commit": "vcs_write",
    "git_diff": "point_in_time",
    # Network results are not reproducible now.
    "web_fetch": "network_result",
    "web_search": "network_result",
    # Authored by this run. There is nothing to re-fetch; the write is the fact.
    "memory_record": "run_authored",
    "write": "run_authored",
    "edit": "run_authored",
    "apply_patch": "run_authored",
    "rename": "run_authored",
    "delete": "run_authored",
    "undo": "run_authored",
    "todo": "run_authored",
    "rename_symbol": "run_authored",
    "update_signature": "run_authored",
    # ---- P3a/W2 additions: every one of these was previously unnamed. ----
    # A command that ran. Its output is a verdict about the tree at the moment
    # it ran, and re-running it re-runs whatever it did.
    "shell": "command_verdict",
    # Process control. The whole point of these is to CHANGE a live process, so
    # there is no read to repeat.
    "process": "process_started",
    "process_kill": "process_side_effect",
    "process_write_stdin": "process_side_effect",
    # A stream that has moved on. This is the brief's own example: the bytes
    # after this call are not the bytes this call returned, and the buffer that
    # held them has already advanced.
    "process_read_output": "stream_already_advanced",
    # A memory QUERY. The store is mutable and shared across sessions, so the
    # same query tomorrow is not the same answer. `memory_record` is the write
    # side and is protected as `run_authored`; this is the read side and is
    # protected because "durable" is a property of the ROW, not of the query.
    "memory": "memory_query_not_a_pure_read",
    # An MCP call. Its effect class is decided by the SERVER, not by this
    # catalog, so a call that reads here may mutate in another deployment.
    "mcp": "mcp_effect_server_decided",
    # An image. `ConversationTurn.attachments` is NOT touched by the clearing
    # transform (`dataclasses.replace(..., content=...)`), so clearing an image
    # turn's text would replace the caption with a placeholder while the image
    # block SURVIVED — a receipt claiming reclaimed bytes it did not reclaim.
    # Protecting it also costs nothing: attachments are deliberately not charged
    # to `ConversationTurn.chars`, so there were no text bytes to win.
    "image": "attachment_survives_the_transform",
    # Control-plane results. A human's answer, a spawn, or a cancellation is a
    # fact about what happened, not a read anybody can repeat.
    "ask": "human_answer_point_in_time",
    "task": "spawn_side_effect",
    "cancel": "process_side_effect",
    "plan": "control_plane_record",
    "finish": "control_plane_record",
}

#: Below this many characters a clearing costs more in placeholder text than it
#: reclaims. Measured against the placeholder's own length: a 40-character result
#: replaced by a 90-character notice is a net LOSS of context.
DEFAULT_MIN_CHARS_TO_CLEAR = 160

#: The placeholder shape. Short, self-describing, and it names the TOOL so the
#: model knows what it could re-run. It carries no bytes of the original, which
#: is what makes the transform a placeholder rather than a truncation.
PLACEHOLDER_TEMPLATE = (
    "[tool result cleared: {tool} returned {chars} characters on turn {turn}. "
    "Re-call {tool} if you need it again.]"
)

#: A message already carrying a placeholder is left alone, so a render loop
#: cannot re-clear a cleared result and inflate the receipt.
_PLACEHOLDER_RE = re.compile(r"^\[tool result cleared:")

_CLEARED_MAX_CHARS = 2000


def is_refetchable(tool: str) -> bool:
    """Whether ``tool``'s result can be re-fetched rather than carried.

    Unknown tools answer ``False``. That is the safe direction and it is
    deliberate: a tool added to the catalog next month has an unknown cost, and
    clearing its output on the strength of not recognising it would be silent
    data loss with a receipt that looks like a success.
    """
    return str(tool or "").strip().lower() in REFETCHABLE_TOOLS


def classify_tool(tool: str) -> Tuple[bool, str]:
    """Return ``(refetchable, reason)`` for one tool name.

    ``reason`` is empty when the tool is re-fetchable, and otherwise one of
    :data:`PROTECTED_REASONS` — so every protected result in a receipt can say
    why it survived.
    """
    name = str(tool or "").strip().lower()
    if not name:
        return False, "no_tool_name"
    if name in NON_REFETCHABLE_TOOLS:
        return False, "not_refetchable"
    if name in REFETCHABLE_TOOLS:
        return True, ""
    return False, "unclassified_tool"


def placeholder_for(tool: str, chars: int, turn: Any) -> str:
    """Return the placeholder that replaces a cleared result."""
    return PLACEHOLDER_TEMPLATE.format(
        tool=str(tool or "the tool"),
        chars=int(chars),
        turn=int(turn or 0),
    )


@dataclass(frozen=True)
class ClearedResult:
    """One cleared result: what it was, and what it cost."""

    seq: int
    tool: str
    turn: int
    ok: Optional[bool]
    target: str
    chars_before: int
    chars_after: int
    placeholder: str

    @property
    def reclaimed_chars(self) -> int:
        """Characters this clearing gave back to the window."""
        return max(0, int(self.chars_before) - int(self.chars_after))

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe record. Carries NO content, only digests of it."""
        return {
            "seq": int(self.seq),
            "tool": str(self.tool),
            "turn": int(self.turn),
            "ok": None if self.ok is None else bool(self.ok),
            "target": str(self.target),
            "chars_before": int(self.chars_before),
            "chars_after": int(self.chars_after),
            "reclaimed_chars": int(self.reclaimed_chars),
        }


@dataclass(frozen=True)
class ClearingReceipt:
    """What one clearing pass did, in enough detail to be audited.

    Every field a reader needs to answer "was context reclaimed, and what was
    dropped" is here: how many results, how many bytes, which tools, which
    results were protected and why, and where the text still lives.
    """

    method: str = "skipped"
    cleared: Tuple[ClearedResult, ...] = ()
    protected: Tuple[Tuple[int, str, str], ...] = ()  # (seq, tool, reason)
    chars_before: int = 0
    chars_after: int = 0
    min_chars: int = DEFAULT_MIN_CHARS_TO_CLEAR
    #: Why the pass did nothing when it did nothing. Never empty for a
    #: ``skipped`` receipt — "nothing happened" and "we were not asked" are
    #: different facts.
    reason: str = ""
    reversible: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cleared_count(self) -> int:
        """How many results were replaced by a placeholder."""
        return len(self.cleared)

    @property
    def protected_count(self) -> int:
        """How many eligible-looking results were deliberately kept."""
        return len(self.protected)

    @property
    def reclaimed_chars(self) -> int:
        """Total characters returned to the context window."""
        return max(0, int(self.chars_before) - int(self.chars_after))

    @property
    def tools(self) -> Tuple[str, ...]:
        """The distinct tools whose results were cleared, sorted."""
        return tuple(sorted({item.tool for item in self.cleared if item.tool}))

    @property
    def protected_tools(self) -> Tuple[str, ...]:
        """The distinct tools whose results were kept, sorted."""
        return tuple(sorted({tool for _, tool, _ in self.protected if tool}))

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe receipt that rides the trace and the meter."""
        return {
            "method": str(self.method),
            "cleared_count": int(self.cleared_count),
            "protected_count": int(self.protected_count),
            "tools": list(self.tools),
            "protected_tools": list(self.protected_tools),
            "chars_before": int(self.chars_before),
            "chars_after": int(self.chars_after),
            "reclaimed_chars": int(self.reclaimed_chars),
            "min_chars": int(self.min_chars),
            "reason": str(self.reason),
            "cleared": [item.as_dict() for item in self.cleared],
            "protected": [
                {"seq": int(seq), "tool": str(tool), "reason": str(reason)}
                for seq, tool, reason in self.protected
            ],
            "reversible": dict(self.reversible or {}),
        }

    def summary_line(self) -> str:
        """Return one PLAIN line naming the count, the bytes and the tools."""
        if self.method != "cleared":
            return (
                f"[context clearing: {self.method}] {self.reason or 'nothing to clear'}"
            )
        return (
            f"[context clearing: {self.cleared_count} tool result(s) cleared, "
            f"{self.reclaimed_chars} chars reclaimed, tools: "
            f"{', '.join(self.tools) or '(none)'}; "
            f"{self.protected_count} protected]"
        )


def clear_tool_results(
    turns: Sequence[Any],
    *,
    protect_newest: int = 1,
    min_chars: int = DEFAULT_MIN_CHARS_TO_CLEAR,
    enabled: bool = True,
    reversible: Optional[Mapping[str, Any]] = None,
) -> Tuple[List[Any], ClearingReceipt]:
    """Return ``(cleared_turns, receipt)`` for one conversation's turns.

    ``turns`` are :class:`~harness.agent_kernel.conversation.ConversationTurn`
    objects. The transform is a **view**: the returned turns are copies with the
    content replaced, the input list is not mutated, and the durable journal is
    untouched. That is what makes clearing lossless — the bytes are still in the
    journal and still reachable by ``seq``.

    ``protect_newest`` is the newest-N protection the repository already applied
    in ``conversation.py``; it is honoured here and defaulted to 1. The newest
    tool result is the one the model is reasoning about right now, so clearing it
    would trade a live fact for a notice.

    The turn's own identity fields (``seq``, ``turn``, ``kind``, ``tool``, ``ok``,
    ``target``) are all PRESERVED, so the ``tool_use`` record survives: the model
    still knows it made the call, what it called, and what it targeted. Only the
    RESULT BYTES go.
    """
    items = list(turns or ())
    if not enabled:
        return items, ClearingReceipt(
            method="disabled",
            reason="clearing disabled by configuration",
            min_chars=int(min_chars),
            reversible=dict(reversible or {}),
        )

    result_positions = [
        index
        for index, item in enumerate(items)
        if str(getattr(item, "kind", "")) == "tool_result"
    ]
    if not result_positions:
        return items, ClearingReceipt(
            method="skipped",
            reason="no tool results retained to clear",
            min_chars=int(min_chars),
            reversible=dict(reversible or {}),
        )

    keep_newest = _newest_positions(result_positions, protect_newest)
    chars_before = sum(int(getattr(item, "chars", 0) or 0) for item in items)

    cleared: List[ClearedResult] = []
    protected: List[Tuple[int, str, str]] = []
    out: List[Any] = []
    floor = max(0, int(min_chars))

    for index, item in enumerate(items):
        seq = int(getattr(item, "seq", 0) or 0)
        tool = str(getattr(item, "tool", "") or "")
        turn = int(getattr(item, "turn", 0) or 0)
        if str(getattr(item, "kind", "")) != "tool_result":
            out.append(item)
            continue
        content = str(getattr(item, "content", "") or "")
        if index in keep_newest:
            protected.append((seq, tool, "newest"))
            out.append(item)
            continue
        if _PLACEHOLDER_RE.match(content.strip()):
            protected.append((seq, tool, "already_placeholder"))
            out.append(item)
            continue
        refetchable, reason = classify_tool(tool)
        if not refetchable:
            protected.append((seq, tool, reason))
            out.append(item)
            continue
        if _carries_attachments(item):
            # Structural, not name-based: the transform replaces `content` and
            # leaves `attachments` alone, so a turn with an attachment would be
            # half-cleared — placeholder text in the request, image block still
            # present, and a receipt counting bytes nobody got back.
            protected.append((seq, tool, "carries_attachments"))
            out.append(item)
            continue
        if len(content) < floor:
            protected.append((seq, tool, "too_small"))
            out.append(item)
            continue

        note = placeholder_for(tool, len(content), turn)
        copy = _replace_content(item, note)
        out.append(copy)
        cleared.append(
            ClearedResult(
                seq=seq,
                tool=tool,
                turn=turn,
                ok=getattr(item, "ok", None),
                target=str(getattr(item, "target", "") or ""),
                chars_before=len(content),
                chars_after=len(note),
                placeholder=note,
            )
        )

    chars_after = sum(int(getattr(item, "chars", 0) or 0) for item in out)
    receipt = ClearingReceipt(
        method="cleared" if cleared else "skipped",
        cleared=tuple(cleared),
        protected=tuple(protected),
        chars_before=chars_before,
        chars_after=chars_after,
        min_chars=floor,
        reason="" if cleared else "no re-fetchable tool result was old enough to clear",
        reversible=dict(reversible or {}),
    )
    return out, receipt


def _newest_positions(positions: Sequence[int], protect_newest: int) -> set:
    """Return the positions the newest-N protection covers.

    **The ``-0`` slice is a real trap and this helper exists because of it.**
    ``positions[-0:]`` is ``positions[0:]`` — the WHOLE list — so the obvious
    spelling of "protect nothing" protects everything, silently, with no error
    and a receipt that says ``skipped``. The two shipped call sites both
    accepted ``protect_newest=0`` and neither could express "protect none".

    Negative values clamp to zero rather than slicing from the end, because
    ``positions[-(-3):]`` would protect three items for a caller who asked for
    none — the same class of surprise in the opposite direction.
    """
    count = max(0, int(protect_newest))
    if count == 0 or not positions:
        return set()
    return set(positions[-count:])


def _carries_attachments(turn: Any) -> bool:
    """Whether ``turn`` holds attachment blocks the transform would not remove.

    ``ConversationTurn.attachments`` arrived in P1/W2 (vision). It is a separate
    field from ``content``, and :func:`_replace_content` replaces ``content``
    only — so an attachment-bearing turn cannot be half-cleared without the
    receipt lying about what it reclaimed. This is checked on the VALUE rather
    than on the tool name so that adding a new image-bearing tool cannot arm
    the trap by omission.
    """
    attachments = getattr(turn, "attachments", None)
    try:
        return bool(attachments)
    except Exception:
        return False


def _replace_content(turn: Any, content: str) -> Any:
    """Return a copy of ``turn`` carrying ``content``, with everything else kept.

    Uses ``dataclasses.replace`` so a future field on ``ConversationTurn`` is
    carried rather than silently defaulted — the same reason the repo's other
    view transforms do not rebuild an object field by field.
    """
    import dataclasses

    if dataclasses.is_dataclass(turn):
        return dataclasses.replace(turn, content=str(content))
    copied = dict(turn) if isinstance(turn, Mapping) else None
    if copied is not None:
        copied["content"] = str(content)
        return copied
    raise TypeError(
        "clear_tool_results needs a dataclass or mapping turn; got "
        f"{type(turn).__name__}"
    )


def clearing_report(
    turns: Sequence[Any],
    *,
    min_chars: int = DEFAULT_MIN_CHARS_TO_CLEAR,
    protect_newest: int = 1,
) -> Dict[str, Any]:
    """Return what clearing WOULD reclaim, without mutating anything.

    Read-only, and it takes the conversation's **turns** rather than a rendered
    message list. That is deliberate and it was the first thing this function got
    wrong: a rendered ``{"role", "content"}`` message carries no tool name, so a
    profiler reading one has to either guess the tool or refuse to. Guessing
    would publish a number no receipt could defend; refusing makes the function
    useless for the Phase-3b question it exists to answer. The turns DO carry
    ``kind``, ``tool``, ``chars`` and ``seq``, so the answer is exact.

    ``apply`` is not a parameter. Running the transform to measure it would make
    a read-only reporter a mutation, and the journal-versus-view distinction is
    the property :func:`clear_tool_results` exists to preserve.
    """
    floor = max(0, int(min_chars))
    results = [
        (index, item)
        for index, item in enumerate(turns or ())
        if str(getattr(item, "kind", "")) == "tool_result"
    ]
    keep_newest = _newest_positions([index for index, _ in results], protect_newest)
    eligible: Dict[str, int] = {}
    protected: Dict[str, int] = {}
    reclaimable = 0
    total_result_chars = 0
    for index, item in results:
        content = str(getattr(item, "content", "") or "")
        tool = str(getattr(item, "tool", "") or "") or "(no tool)"
        total_result_chars += len(content)
        if index in keep_newest:
            protected[tool] = protected.get(tool, 0) + len(content)
            continue
        refetchable, _reason = classify_tool(tool)
        if not refetchable or len(content) < floor:
            protected[tool] = protected.get(tool, 0) + len(content)
            continue
        eligible[tool] = eligible.get(tool, 0) + len(content)
        reclaimable += len(content)
    return {
        "tool_results": len(results),
        "tool_result_chars": int(total_result_chars),
        "reclaimable_chars": int(reclaimable),
        "protected_chars": int(sum(protected.values())),
        "by_tool": dict(eligible),
        "protected_by_tool": dict(protected),
        "min_chars": floor,
        "protect_newest": int(protect_newest),
        "note": (
            "reclaimable_chars is the CONTENT that clearing would replace; the "
            "placeholder costs characters back, so the net figure is lower and "
            "is measured, not estimated"
        ),
    }


def classification_table() -> List[Tuple[str, str, str]]:
    """Return ``(tool, refetchable, reason)`` for every classified tool.

    Published so the classification is a thing a reader can READ rather than
    infer from an allow-list's silence.
    """
    rows: List[Tuple[str, str, str]] = []
    for name in sorted(REFETCHABLE_TOOLS):
        rows.append((name, "yes", "pure read of durable state"))
    for name, reason in sorted(NON_REFETCHABLE_TOOLS.items()):
        rows.append((name, "no", reason))
    return rows
