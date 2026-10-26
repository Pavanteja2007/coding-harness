"""The context budget a person is owed, and the honest form of every field.

**The problem this exists to solve.** A rail that says ``62% of window`` tells
a reader their context is fine. It does not tell them what the number is a
percentage OF, it does not distinguish the two ways a window shrinks, and it
renders exactly the same whether the agent cleared a stale tool output or
summarised away their own conversation. A user who cannot tell those apart
cannot tell whether their work is safe.

**Six rows, and each carries its own absent rule.**

1. ``tokens`` - what is in the window against what the window is. Omitted
   entirely when the window is unresolvable, because a percentage of an
   unknown denominator is a fabricated number.
2. ``clearing`` - tokens reclaimed by DISCARDING stale tool output. Distinct
   from row 3 because the turns are still on disk and the model chose to drop
   them; nothing the user said was summarised.
3. ``compaction`` - tokens reclaimed by SUMMARISING the conversation. Distinct
   from row 2 because the user's own words went into a summary, and that is
   the one a person needs to be told about.
4. ``summary`` - how the compacted turns were grouped into sections, so
   "summarised away" has a shape rather than being one undifferentiated
   number.
5. ``last_compaction`` - which PARENT session the last rotation came from.
   Reported as its own row because a rotation ends a session, and a session
   that silently becomes two is the failure ``cli/lineage.py`` exists to make
   visible.
6. ``memory`` - what memory is costing this session, and how many proposals
   are waiting. The count is here because an inbox nobody can see is an inbox
   nobody empties.

**Absent is not zero.** Every field is ``None`` when it cannot be measured,
never ``0``. ``0`` reads as a measured fact - "nothing was reclaimed", "no
memory" - and that is precisely the claim this module refuses to make without
evidence. The rendering rule is therefore mechanical: a ``None`` field renders
no row, and the module reports ``reason`` for each so a reader (or a test) can
tell "we did not measure" from "we measured zero".

**Clearing and compaction are separate because conflating them is a lie about
risk.** A user whose stale tool output was cleared has lost nothing they said.
A user whose conversation was compacted has lost the exact wording of their own
messages from the window. Same token count reclaimed, completely different
consequence, and a rail that prints one number for both is the surface this
round exists to correct.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BUDGET_ROWS",
    "CLEARING_POLICIES",
    "CLEARING_POLICY_KEY",
    "RECLAIM_KINDS",
    "UNAVAILABLE",
    "assert_absent_is_never_zero",
    "budget_lines",
    "budget_projection",
    "budget_rows",
    "clearing_policy",
    "clearing_receipt",
    "compaction_receipt",
    "reclaim_receipts",
]

#: The literal that renders wherever a field could not be measured. It is a
#: SENTENCE rather than a dash or a blank so a reader who scans the rail still
#: learns that something is missing rather than assuming the row is empty.
UNAVAILABLE = "unavailable"

#: The six rows, in the order they render. Declared so a test can pin the set
#: and so the rail cannot quietly lose one - a six-row projection that renders
#: five rows is a projection nobody checked.
BUDGET_ROWS: Tuple[str, ...] = (
    "tokens",
    "clearing",
    "compaction",
    "summary",
    "last_compaction",
    "memory",
)

#: How a reclaimed-token row names its cause. Two members, and the distinction
#: is the module's whole point (see the module docstring).
RECLAIM_KINDS: Tuple[str, ...] = ("clearing", "compaction")

#: The config key that turns CLEARING off. Declared here, read by key
#: PRESENCE, and deliberately NOT in ``harness/config.py::DEFAULTS``: a value
#: there is merged into every task and every eval arm, and "should this session
#: discard stale tool output" is a decision one person makes about one
#: conversation. Wave 2's item 6 is "a user must be able to turn clearing off",
#: and a knob nobody can reach is not that.
CLEARING_POLICY_KEY = "clearing_enabled"

#: The two policies, spelled out so the receipt can name which one is in force.
#: ``auto`` is the shipped default and means "clear stale tool output, never
#: touch the conversation" - which is the only setting that is lossless.
CLEARING_POLICIES: Tuple[str, ...] = ("auto", "off", "on")

#: What a CLEARING event dropped, by key. Producers spell these four ways, so
#: the spellings are enumerated here rather than guessed at each call site - a
#: call site that guesses reports ``None`` for a number that exists, and the
#: reader is left believing nothing was dropped.
_DROP_KEYS: Dict[str, Tuple[str, ...]] = {
    "messages": ("messages", "message_count", "dropped_messages", "count"),
    "bytes": ("bytes", "byte_size", "dropped_bytes", "chars"),
    "tools": ("tools", "tool_calls", "dropped_tools", "tool_count"),
}


def _drops(item: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    """What one reclaim event dropped: message count, bytes, tool calls.

    Each is ``None`` when the producer did not record it, and ``None`` is the
    only honest answer here. A tool-output clear that dropped 40 messages and
    300 KB of output did not drop "0 bytes", and a receipt that renders 0 for
    an unrecorded number tells a reader the tool output was free.
    """
    out: Dict[str, Optional[int]] = {}
    for name, keys in _DROP_KEYS.items():
        found: Optional[int] = None
        for key in keys:
            value = item.get(key)
            if name == "tools" and isinstance(value, (list, tuple, set)):
                found = len(value)
                break
            candidate = _int_or_none(value)
            if candidate is not None:
                found = candidate
                break
        out[name] = found
    return out


def _sum_drops(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[int]]:
    """Total the drops across events, keeping ``None`` for an unmeasured field.

    A sum that starts at ``0`` turns "nobody recorded the byte count" into "the
    byte count was zero", which is the exact conflation this module exists to
    prevent - one layer down from where Wave 1 was already fighting it.
    """
    totals = {"messages": None, "bytes": None, "tools": None}
    for item in rows:
        for name, value in _drops(item).items():
            if value is None:
                continue
            totals[name] = (totals[name] or 0) + int(value)
    return totals


def clearing_policy(config: Any = None) -> Dict[str, Any]:
    """Whether CLEARING runs for this session, read by key presence.

    ``on`` / ``off`` / ``auto``, defaulting to ``auto``. An unrecognised value
    is REPORTED and falls back to ``auto`` rather than resolving to ``off`` or
    ``on``: a typo in a safety-adjacent knob must not be able to switch it, and
    a typo that silently disabled clearing would look identical to a session
    where nothing needed clearing.

    The receipt says which key answered, because "clearing is off" and "nobody
    configured clearing" are different facts and a user deciding whether their
    context is safe needs to know which one they are looking at.
    """
    raw = None
    present = False
    if isinstance(config, Mapping):
        present = CLEARING_POLICY_KEY in config
        raw = config.get(CLEARING_POLICY_KEY)
    value = "auto"
    source = "default (no key configured)"
    reason = ""
    if present and raw is not None:
        text = str(raw).strip().lower()
        if text in ("", "auto", "default"):
            value, source = "auto", "configured as auto"
        elif text in ("off", "false", "no", "never", "disabled"):
            value, source = "off", "configured off by the user"
        elif text in ("on", "true", "yes", "always", "enabled"):
            value, source = "on", "configured on by the user"
        else:
            reason = (
                f"{CLEARING_POLICY_KEY}={raw!r} is not one of "
                f"{', '.join(CLEARING_POLICIES)}; clearing falls back to auto, "
                "which is the lossless setting"
            )
            source = "unrecognised value, falling back to auto"
    return {
        "policy": value,
        "enabled": value in ("auto", "on"),
        "source": source,
        "reason": reason,
        "key": CLEARING_POLICY_KEY,
        "controllable": True,
        "how": (
            f"set {CLEARING_POLICY_KEY} = off in this session's settings, or "
            "run /settings to change it"
        ),
    }


def _int_or_none(value: Any) -> Optional[int]:
    """An int, or ``None``. Never ``0`` for absent.

    ``bool`` is refused explicitly: ``True`` is an ``int`` in Python, and a
    caller passing a flag where a count belongs would otherwise get a
    confident ``1``.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tokens_from(record: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Read a token count out of whichever shape a producer used.

    The kernel's own budget rows and the decision-store receipts spell this
    four different ways. Four spellings is too many for a rail, so the four are
    enumerated HERE rather than at each call site - a call site that guesses is
    a call site that reports ``None`` for a number that exists.
    """
    if not isinstance(record, Mapping):
        return None
    for key in (
        "tokens_reclaimed",
        "reclaimed_tokens",
        "tokens_freed",
        "freed_tokens",
        "dropped_tokens",
        "tokens",
    ):
        found = _int_or_none(record.get(key))
        if found is not None:
            return found
    return None


def _session_id(conversation: Any) -> str:
    if isinstance(conversation, Mapping):
        return str(conversation.get("session_id") or "")
    return ""


def budget_projection(
    *,
    pulse: Optional[Mapping[str, Any]] = None,
    meter: Optional[Mapping[str, Any]] = None,
    conversation: Any = None,
    log_root: Any = None,
    repo_path: Any = None,
    tokens_used: Any = None,
    window_tokens: Any = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Project the six honest budget fields. Pure, total, never raises.

    Every source is OPTIONAL and every field degrades independently: a caller
    with only a session and no run still gets the lineage row, and a caller
    with a run and no session still gets the token rows. That independence is
    the point - a projection that returns ``{}`` because one input was missing
    throws away the five answers it could give.

    ``tokens_used`` / ``window_tokens`` are EXPLICIT rather than read out of
    ``pulse`` because the pulse's context block carries a ``fraction`` and not
    the two numbers a percentage needs. A surface that knows both passes them;
    a surface that only has the pulse gets the row omitted, which is honest
    and is the correct behaviour when the denominator really is unknown.

    Returns ``{"rows": {...}, "known": int, "absent": int, "reasons": {...}}``.
    ``known``/``absent`` count the six rows so a caller can tell "everything
    measured" from "nothing measured" without reading each field.
    """
    pulse = pulse if isinstance(pulse, Mapping) else {}
    meter = meter if isinstance(meter, Mapping) else {}
    rows: Dict[str, Any] = {}
    reasons: Dict[str, str] = {}

    # -- 1. tokens used / window -----------------------------------------
    context = pulse.get("context") if isinstance(pulse.get("context"), Mapping) else {}
    used = _int_or_none(tokens_used)
    if used is None:
        used = _int_or_none(context.get("tokens"))
    if used is None:
        used = _int_or_none(
            (meter.get("meter") if isinstance(meter.get("meter"), Mapping) else {}).get(
                "prompt_tokens"
            )
        )
    window = _int_or_none(window_tokens)
    if window is None:
        window = _int_or_none(context.get("window"))
    if window is None:
        window = _int_or_none(meter.get("window"))
    if used is not None and window:
        rows["tokens"] = {
            "used": used,
            "window": window,
            "percent": min(100, int(used * 100 / window)),
        }
    else:
        reasons["tokens"] = (
            "no context window is configured, so there is no denominator to "
            "report a percentage of"
        )

    # -- 2 & 3. reclaimed, by cause --------------------------------------
    # `context_rewind` is the kernel's CLEARING receipt (stale tool output
    # dropped); `context_compacted` is the COMPACTION one. They are read from
    # different lists precisely so they cannot be added together into one
    # number that says nothing about which happened.
    rewinds = meter.get("rewinds") if isinstance(meter.get("rewinds"), list) else []
    compactions = (
        meter.get("compactions") if isinstance(meter.get("compactions"), list) else []
    )
    rewinds = [item for item in rewinds if isinstance(item, Mapping)]
    compactions = [item for item in compactions if isinstance(item, Mapping)]
    clearing_total = 0
    clearing_seen = False
    for item in rewinds:
        count = _tokens_from(item)
        if count is not None:
            clearing_total += count
            clearing_seen = True
    policy = clearing_policy(config)
    if clearing_seen and policy["enabled"]:
        rows["clearing"] = {
            "tokens": clearing_total,
            "tokens_measured": True,
            "events": len(rewinds),
            "kept_on_disk": True,
            # W2.3: the drops, and the reassurance that is LOAD-BEARING. A
            # cleared tool output can be re-fetched from the journal, and saying
            # so is the reason clearing is safe; hiding it makes a lossless
            # operation feel lossy, which is how a user ends up refusing to let
            # the agent manage its own context at all.
            "refetchable": True,
            "refetch_note": (
                "nothing you said was discarded: the tool output is still in "
                "this run's journal and can be fetched again"
            ),
            "drops": _sum_drops(rewinds),
            "policy": policy["policy"],
        }
    elif clearing_seen and not policy["enabled"]:
        # Turning clearing OFF does not erase what already happened, so the row
        # is reported as SUPPRESSED rather than dropped. Omitting it would read
        # as "nothing was ever cleared", which is false and is the direction a
        # user disabling the policy needs to be able to check.
        rows["clearing"] = {
            "tokens": None,
            "events": len(rewinds),
            "kept_on_disk": True,
            "refetchable": True,
            "suppressed_by_policy": True,
            "reason": (
                "clearing is switched off for this session"
                f" ({policy['source']}), so no further stale tool output is "
                "discarded; the "
                f"{len(rewinds)} event(s) already recorded are still on disk"
            ),
            "drops": _sum_drops(rewinds),
            "policy": policy["policy"],
        }
    else:
        reasons["clearing"] = (
            "clearing is switched off for this session"
            f" ({policy['source']})"
            if not policy["enabled"]
            else "no clearing has been recorded for this session"
        )
        reasons["clearing_policy"] = policy["source"]

    compaction_total = 0
    compaction_seen = False
    compaction_turns = 0
    for item in compactions:
        count = _tokens_from(item)
        if count is not None:
            compaction_total += count
            compaction_seen = True
        found = _int_or_none(item.get("turn_count"))
        if found is not None:
            compaction_turns += found
    if compaction_seen or compaction_turns:
        rows["compaction"] = {
            "tokens": compaction_total if compaction_seen else None,
            "tokens_measured": compaction_seen,
            "turns": compaction_turns or None,
            "events": len(compactions),
            "kept_on_disk": True,
            # W2.3 item 3: compaction is the LOSSY half and the receipt has to
            # say so in its own words, with a door to the detail. The turns are
            # still on disk - but they are no longer in the window, and "on
            # disk" is not the same reassurance as "will be there next turn".
            "lossy": True,
            "lossy_note": (
                "your own words were SUMMARISED out of the window; the turns are "
                "still on disk but the model is no longer reading them verbatim"
            ),
            "detail_command": "/lineage before",
            "detail_note": "see exactly which turns went into a summary: /lineage before",
            "drops": _sum_drops(compactions),
        }
    else:
        reasons["compaction"] = "no compaction has been recorded for this session"

    # -- 4. summary shape ------------------------------------------------
    # Read off the SESSION's own compaction summary rather than the run's
    # meter: the summary is a property of the conversation, and a run-level
    # compaction would otherwise be reported as this conversation's history.
    sections = _summary_sections(conversation)
    if sections is not None:
        rows["summary"] = sections
    else:
        reasons["summary"] = "no compaction summary exists for this conversation"

    # -- 5. the last rotation --------------------------------------------
    session_id = _session_id(conversation)
    if session_id and log_root is not None:
        try:
            from cli import lineage as _lineage

            last = _lineage.last_rotation(log_root, session_id)
        except Exception:
            last = {}
        if isinstance(last, Mapping) and last:
            parent = str(last.get("parent_session_id") or "")
            child = str(last.get("child_session_id") or "")
            rows["last_compaction"] = {
                "kind": str(last.get("kind") or "compaction"),
                "reason": str(last.get("reason") or ""),
                "parent_session_id": parent,
                "child_session_id": child,
                "rotated": bool(last.get("child_session_id")),
            }
        else:
            reasons["last_compaction"] = (
                "this conversation has no recorded compaction or rotation"
            )
    else:
        reasons["last_compaction"] = "no session is loaded, so there is no lineage to read"

    # -- 6. memory in this session ---------------------------------------
    # PENDING and APPLIED are read independently and joined into one row.
    # Wave 1 built this row only when an INBOX was available, so a project
    # whose inbox lives outside the checkout by design - which is every project,
    # since VEX-CEILING-03 moved artifacts out of the tree - reported no memory
    # row at all and a reader could not see how much the agent had remembered.
    # "How many proposals are waiting" and "how much has been applied" are two
    # different questions and neither implies the other.
    try:
        from cli import memory_ui as _memory

        status = _memory.inbox_status(repo_path, log_root)
    except Exception:
        status = {}
    applied = 0
    applied_known = False
    applied_reason = ""
    try:
        from cli import memory_ui as _memory

        report = _memory.applied_memories(repo_path)
        # Wave 1's line here was `applied_memories(repo_path, log_root)`
        # followed by `len(rows_applied or ())`. Both halves were wrong and
        # they cancelled into a FABRICATED COUNT that rendered as a fact:
        # `log_root` landed in `applied_memories`'s `store` keyword, and
        # `len()` of the returned REPORT DICT is its key count - which is
        # exactly 4. So this rail said "4 remembered" for every project on
        # every machine, forever, and `assert_absent_is_never_zero` could not
        # see it because a confident wrong number is not an absent one.
        #
        # The count is now read from the report's own `count` field, and it is
        # `None` - not 0 - when the store could not be read.
        if isinstance(report, Mapping) and report.get("available"):
            applied = _int_or_none(report.get("count"))
            applied_known = applied is not None
        else:
            applied_reason = str(
                (report or {}).get("reason") or "the memory store could not be read"
            )
    except Exception as exc:
        applied_reason = f"the memory store could not be read: {type(exc).__name__}"
    rows["memory"] = {
        "pending": status.get("count") if status else None,
        "applied": applied if applied_known else None,
        "applied_known": applied_known,
        "applied_reason": applied_reason,
        "tokens": None,
        "untrusted": status.get("untrusted") if status else None,
        "inbox_available": bool(status.get("available")) if status else False,
        "inbox_reason": str(status.get("reason") or "") if status else "",
    }
    if not rows["memory"]["inbox_available"]:
        # The row exists, so the applied count is still shown; the absent half
        # carries its own reason rather than being dropped with the row.
        reasons["memory_inbox"] = str(
            (status or {}).get("reason") or "no memory inbox exists for this repository"
        )
    else:
        reasons["memory"] = str(
            (status or {}).get("reason") or "no memory inbox exists for this repository"
        )

    known = sum(1 for name in BUDGET_ROWS if name in rows)
    return {
        "rows": rows,
        "known": known,
        "absent": len(BUDGET_ROWS) - known,
        "total": len(BUDGET_ROWS),
        "reasons": reasons,
    }


def _summary_sections(conversation: Any) -> Optional[Dict[str, Any]]:
    """How the compacted turns were grouped, or ``None`` if never compacted.

    A count of sections is the smallest shape that makes "your conversation was
    summarised" legible: five sections and twenty sections are different
    compressions, and the rail should not say the same thing about both.
    """
    if not isinstance(conversation, Mapping):
        return None
    summaries = conversation.get("summaries")
    if not isinstance(summaries, list) or not summaries:
        return None
    compacted = conversation.get("compacted_turns")
    compacted = compacted if isinstance(compacted, list) else []
    text = str((summaries[-1] or {}).get("text") or "") if isinstance(
        summaries[-1], Mapping
    ) else ""
    sections = [row for row in text.splitlines() if row.strip()]
    return {
        "count": len(summaries),
        "sections": len(sections) or None,
        "turns": len(compacted) or None,
        "sections_text": sections[:6],
    }


def _drop_phrase(drops: Mapping[str, Any], *, width: int) -> str:
    """Render what a reclaim dropped: count, bytes, and tools.

    Absent fields are named as absent rather than rendered as zero, and the
    three are all reported because the brief asks for all three and because a
    reader who is told "1,200 tokens reclaimed" and nothing else cannot tell
    whether a file was dropped or a tool call was.

    The ``0`` / ``unavailable`` split is the load-bearing part of Wave 2's
    item 5: ``0 messages, 0 bytes, 0 tools`` is a measurement, and it only
    appears when every producer actually recorded the field.
    """
    bits: List[str] = []
    for key, noun in (("messages", "message"), ("bytes", "byte"), ("tools", "tool")):
        value = drops.get(key)
        if value is None:
            bits.append(f"{noun}s {UNAVAILABLE}")
            continue
        count = int(value)
        bits.append(f"{count} {noun}{'' if count == 1 else 's'}")
    return ", ".join(bits)


def clearing_receipt(projection: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """The CLEARING receipt: what was dropped, and why it is safe.

    **Re-fetchability is stated in the first content line**, not as a footnote.
    It is the reason clearing is lossless, and Wave 1 rendered both halves of
    the reclaim with the SAME three words - ``(kept on disk)`` - which is
    technically true of both and reassurance about neither. A cleared tool
    output can be fetched again from the journal; a summarised conversation
    cannot be un-summarised into the window. One phrase for both is how a
    lossless operation gets read as a lossy one.
    """
    rows = (projection or {}).get("rows") or {}
    clearing = rows.get("clearing")
    if not isinstance(clearing, Mapping):
        reason = str(
            ((projection or {}).get("reasons") or {}).get("clearing")
            or "no clearing has been recorded for this session"
        )
        return [f"clearing     not measured - {reason}"]

    lines: List[str] = []
    suppressed = bool(clearing.get("suppressed_by_policy"))
    tokens = clearing.get("tokens")
    head = "clearing     "
    if suppressed:
        lines.append(head + "switched OFF for this session")
        lines.append("  " + _wrap(clearing.get("reason") or "", width - 4))
    elif tokens is None:
        lines.append(head + "0 token(s) reclaimed from stale tool output")
    else:
        lines.append(
            head
            + "{tok:,} token(s) reclaimed from stale tool output ({n} event(s))".format(
                tok=int(tokens), n=int(clearing.get("events") or 0)
            )
        )
    lines.append(
        "  RE-FETCHABLE: "
        + _wrap(
            clearing.get("refetch_note")
            or "the tool output is still in this run's journal",
            width - 16,
        )
    )
    lines.append(
        "  dropped:     " + _drop_phrase(clearing.get("drops") or {}, width=width)
    )
    # The policy is read off the ROW, not recomputed with an empty config: the
    # projection already resolved it against the caller's settings, and asking
    # again with nothing would print "default" beside a row that was suppressed
    # BECAUSE the user had configured it off.
    in_force = str(clearing.get("policy") or "auto")
    source = clearing_policy(None)["source"] if in_force == "auto" else (
        "configured by the user"
        if in_force == "off"
        else "configured on by the user"
    )
    lines.append(
        "  policy:      {policy} - {source}; change it with {key}".format(
            policy=in_force,
            source=source,
            key=clearing_policy(None)["key"],
        )
    )
    return lines


def compaction_receipt(
    projection: Mapping[str, Any], *, width: int = 78, session_id: str = ""
) -> List[str]:
    """The COMPACTION receipt: what was dropped, and that it was summarised.

    Leads with the LOSS, because that is the difference from clearing and the
    thing a reader has a right to know before trusting the window. The turns are
    still on disk, so the receipt does NOT claim they are gone - it says the
    model is no longer reading them verbatim, which is the true and much more
    useful statement.

    The detail command is offered rather than assumed: ``/lineage before``
    answers "which turns went into that summary", and a receipt that says
    information was summarised away without saying how to look at it is an
    alarm with no door out of it.
    """
    rows = (projection or {}).get("rows") or {}
    compaction = rows.get("compaction")
    if not isinstance(compaction, Mapping):
        reason = str(
            ((projection or {}).get("reasons") or {}).get("compaction")
            or "no compaction has been recorded for this session"
        )
        return [f"compaction   not measured - {reason}"]

    parts: List[str] = []
    if compaction.get("tokens") is not None:
        parts.append(f"{int(compaction['tokens']):,} token(s)")
    if compaction.get("turns"):
        parts.append(f"{int(compaction['turns'])} turn(s)")
    parts.append(f"{int(compaction.get('events') or 0)} event(s)")
    lines = [
        "compaction   {parts} SUMMARISED - this conversation was compacted".format(
            parts=" ".join(parts)
        ),
        "  LOSSY:      "
        + _wrap(
            compaction.get("lossy_note") or "your words left the window verbatim",
            width - 14,
        ),
    ]
    lines.append("  dropped:     " + _drop_phrase(compaction.get("drops") or {}, width=width))
    detail = str(compaction.get("detail_command") or "/lineage before")
    if session_id:
        detail = f"{detail} {session_id}"
    lines.append("  detail:      " + _wrap(f"which turns went into it: {detail}", width - 14))
    return lines


def reclaim_receipts(
    projection: Mapping[str, Any], *, width: int = 78, session_id: str = ""
) -> Dict[str, List[str]]:
    """Both receipts, keyed by cause. NEVER one number for both.

    The two are returned as two SEPARATE line lists rather than one merged
    string because the brief's item 1 is about separation and the cheapest way
    to keep them separate is to never build the joined form at all. A caller
    that wants one block can interleave them; a caller that cannot tell them
    apart cannot have concatenated them.
    """
    return {
        "clearing": clearing_receipt(projection, width=width),
        "compaction": compaction_receipt(projection, width=width, session_id=session_id),
    }


def _wrap(text: Any, width: int) -> str:
    """Bound a sentence for one indented continuation line."""
    value = " ".join(str(text if text is not None else "").split())
    limit = max(20, int(width))
    if len(value) > limit:
        return value[: max(1, limit - 3)] + "..."
    return value


def budget_rows(projection: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """The six rows as ``(label, value)``, ABSENT rows omitted.

    Omission is the honest form: a rail that printed ``clearing: 0`` would be
    claiming a measurement nobody made. The omission is therefore silent-by-
    construction here and REPORTED in ``projection["reasons"]`` for a caller
    that wants to say why.
    """
    if not isinstance(projection, Mapping):
        return []
    rows = projection.get("rows")
    if not isinstance(rows, Mapping):
        return []
    out: List[Tuple[str, str]] = []
    tokens = rows.get("tokens")
    if isinstance(tokens, Mapping):
        out.append(
            (
                "context",
                "{used:,} of {window:,} tokens ({pct}% of window)".format(
                    used=int(tokens.get("used") or 0),
                    window=int(tokens.get("window") or 0),
                    pct=int(tokens.get("percent") or 0),
                ),
            )
        )
    clearing = rows.get("clearing")
    if isinstance(clearing, Mapping):
        if clearing.get("suppressed_by_policy"):
            out.append(("clearing", "OFF for this session - no stale tool output is discarded"))
        else:
            out.append(
                (
                    "clearing",
                    "{tok:,} token(s) reclaimed from stale tool output "
                    "({n} event(s)) - RE-FETCHABLE, nothing you said was "
                    "discarded".format(
                        tok=int(clearing.get("tokens") or 0),
                        n=int(clearing.get("events") or 0),
                    ),
                )
            )
    compaction = rows.get("compaction")
    if isinstance(compaction, Mapping):
        parts = []
        if compaction.get("tokens") is not None:
            parts.append(f"{int(compaction['tokens']):,} token(s)")
        if compaction.get("turns"):
            parts.append(f"{int(compaction['turns'])} turn(s)")
        parts.append("summarised")
        # The scope is the FIRST word, not the last. Two rows can both say
        # "N turn(s) summarised" with different N - the RUN's meter and the
        # SESSION's summary are different scopes and legitimately disagree -
        # and a rail row is truncated at the tail, so a trailing scope label is
        # exactly the word a narrow terminal drops. A reader left with two
        # differing numbers and no scope hunts for a bug that is not there.
        #
        # W2.3: the tail now says LOSSY rather than "(kept on disk)". Both
        # halves keep their bytes on disk and only one of them is lossless, so
        # the identical reassurance was reassurance about neither.
        out.append(("compaction (this run)", " ".join(parts) + " - LOSSY, see /lineage before"))
    summary = rows.get("summary")
    if isinstance(summary, Mapping):
        bits = ["this conversation"]
        if summary.get("turns"):
            bits.append(f"{summary['turns']} turn(s) summarised")
        if summary.get("sections"):
            bits.append(f"{summary['sections']} section(s)")
        bits.append(f"over {summary.get('count')} compaction(s)")
        bits.append("(kept on disk)")
        out.append(("summary", ", ".join(bits)))
    last = rows.get("last_compaction")
    if isinstance(last, Mapping):
        if last.get("rotated"):
            out.append(
                (
                    "rotation",
                    "{p} -> {c} (reason: {r})".format(
                        p=last.get("parent_session_id") or "?",
                        c=last.get("child_session_id") or "?",
                        r=last.get("reason") or "compression",
                    ),
                )
            )
        else:
            out.append(
                (
                    "last compaction",
                    "this session continued; no rotation was recorded",
                )
            )
    memory = rows.get("memory")
    if isinstance(memory, Mapping):
        bits = []
        if memory.get("pending") is not None:
            bits.append(f"{memory['pending']} proposal(s) waiting")
        if memory.get("applied") is not None:
            bits.append(f"{memory['applied']} remembered")
        if memory.get("untrusted"):
            bits.append(f"{memory['untrusted']} from untrusted sources")
        if bits:
            out.append(("memory", ", ".join(bits)))
    return out


def budget_lines(
    projection: Mapping[str, Any], *, width: int = 78, session_id: str = ""
) -> List[str]:
    """Plain-text lines for the rail, one per KNOWN row, plus a gap count.

    The two reclaim rows are rendered as their OWN receipt blocks rather than
    as two flat rows, because the brief's non-negotiable is *"never collapse
    clearing and compaction into one number"* and the cheapest way to keep that
    from regressing is to give each one a multi-line shape of its own. A flat
    pair of rows invites the next round to merge them into one.

    The trailing ``(N of 6 fields not measured)`` line is what keeps the
    omission honest: without it a reader sees three rows and cannot tell
    whether the other three are absent or merely uninteresting.
    """
    rows = budget_rows(projection)
    lines = [f"{label}: {value}" for label, value in rows]
    receipts = reclaim_receipts(projection, width=width, session_id=session_id)
    for cause in ("clearing", "compaction"):
        block = receipts.get(cause) or []
        # Skip the receipt when the rail row is absent: a receipt for a field
        # the projection never measured would be a second place claiming a
        # measurement exists.
        if any(label == ("clearing" if cause == "clearing" else "compaction (this run)") for label, _ in rows):
            lines.extend(block)
    absent = 0
    if isinstance(projection, Mapping):
        absent = int(projection.get("absent") or 0)
    if absent:
        lines.append(
            "{n} of {t} context fields not measured ({why})".format(
                n=absent,
                t=int(projection.get("total") or len(BUDGET_ROWS)),
                why=(projection.get("reasons") or {}).get("tokens")
                or "nothing was recorded for them",
            )
        )
    return lines


def assert_absent_is_never_zero(
    projection: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """The gate: an UNMEASURED field is ``None``, and ``0`` means measured.

    This is the module's load-bearing invariant and it is checked at the
    PROJECTION layer rather than at the render layer, because a renderer that
    forgot the rule would be one place to fix while the data itself still read
    as a measured zero to the next consumer.

    The control arm matters: a gate that passes because every field is ``None``
    measures nothing, so a field is only rejected when it is ``0`` AND carries
    no evidence of having been measured. This module cannot see the evidence,
    so the check is deliberately narrow - it fires on a row that claims a
    count of zero for something the projection could not read, which is the
    only shape that was actually observed going wrong.
    """
    if projection is None:
        return True, ""
    if not isinstance(projection, Mapping):
        return True, ""
    rows = projection.get("rows")
    if not isinstance(rows, Mapping):
        return True, ""
    for name in BUDGET_ROWS:
        if name not in rows:
            continue
        row = rows[name]
        if not isinstance(row, Mapping):
            continue
        for key in ("tokens", "turns", "sections", "events", "applied"):
            if key in row and row[key] == 0 and not row.get(f"{key}_measured"):
                return False, (
                    f"{name}.{key} is 0 with no measurement behind it; an "
                    "unmeasured field must be None, because 0 reads as a "
                    "measured fact"
                )
    return True, ""
