"""Lineage, rotation, and the session tree: one surface, no silent splits.

T4.W1.2 and T4.W1.3. Two briefs, one module, because after T3's tree lands they
are the same structure seen from two directions.

**What changed underneath.** Compaction used to mutate a transcript in place.
Under lineage compaction it ENDS the session and rotates to a child. A user
whose history silently splits is a user who cannot find their work, so a
rotation is a first-class visible event here - parent, child, reason, and when.

**What did not change underneath, and this module is careful about.** T3's tree
is being built in the same wave this surface is built in. So this module reads
the lineage through ONE adapter (:func:`rotation_events`) that understands both
the shape this tree has today (in-place compaction events, plus
``parent_session_id`` from a fork) and the shape T3's prompt specifies
(``end_reason='compression'`` closing a session and opening a child), and
reports which one it actually found. A lineage browser that reported
"no rotations" over a tree full of them would be worse than no browser.

**The fork disclosure is not optional.** A fork is a new working tree. Two live
sessions sharing one writable tree is how work is lost, T2 is enforcing the
isolation, and a fork that copies turns without saying so leaves a user with
two divergent copies of their state and no idea. So
:func:`fork_isolation_lines` is called by BOTH the REPL and the TUI fork paths
before the new session is announced, and it says which of three things happened:
isolated (a worktree was created), refused (isolation was required and could
not be had), or shared (isolation was not available - stated plainly, never
implied).

**A branch summary is a summary.** Abandoning a branch may splice a generated
summary into the tree, and an LLM-generated summary of what happened is a
CLAIM. :func:`branch_summary_lines` labels it in the text, in the same sentence
as the words, on every surface.

Everything is read-only except the three tree verbs, and each of those reports
what it actually did.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "BRANCH_SUMMARY_LABEL",
    "ROTATION_REASONS",
    "TREE_VERBS",
    "assert_the_disclosure_cannot_be_skipped",
    "branch_summary_lines",
    "dedupe_across_lineage",
    "fork_isolation_lines",
    "last_rotation",
    "lineage_chain",
    "lineage_rows",
    "pre_compaction_history",
    "pre_compaction_lines",
    "rotation_events",
    "search_lineage",
    "searchable_turns",
    "tree_command",
]

#: The closed verb vocabulary. Three tree OPERATIONS (``/tree``, ``/fork``,
#: ``/clone``) share ONE dispatcher rather than being three mechanisms with
#: three behaviours, which is the corpus's Pi model and the reason these words
#: are data rather than three separate code paths.
#:
#: W2.4 added three verbs: ``merge`` (put a branch back), ``delete`` (remove a
#: branch and its rewind points, behind a confirmation that states the count)
#: and ``prune`` (collect unreachable entries, behind a dry run). Each is in
#: this tuple and therefore in ``cli.commands.SUBCOMMANDS["/tree"]``, so all
#: three reach the REPL, the TUI and ``neo run`` from one declaration. That is
#: the whole of the three-surface contract: there is no second verb table to
#: forget.
TREE_VERBS: Tuple[str, ...] = (
    "show",
    "lineage",
    "fork",
    "clone",
    "rewind",
    "abandon",
    "merge",
    "delete",
    "prune",
)

#: Verbs that DESTROY something. Each demands an explicit confirmation token,
#: and each states a COUNT before it acts. The token is not decoration: a
#: branch delete removes rewind points, and "delete a branch" reading as a
#: pointer move is the misunderstanding that makes the operation irreversible.
DESTRUCTIVE_TREE_VERBS: Tuple[str, ...] = ("delete",)

#: Verbs that need no confirmation because they cannot destroy anything.
#: ``prune`` is deliberately NOT here even though it rewrites the journal: it
#: defaults to a dry run and rotates rather than deletes, and it is listed in
#: its own receipt as a reported bound.
LOSSLESS_TREE_VERBS: Tuple[str, ...] = ("show", "lineage", "merge")

#: The reasons a session or a branch may come to exist. Declared so the tree
#: view can answer "why does this exist" with a WORD rather than with a
#: heuristic over field names, and so a producer that writes a fourth reason
#: renders as ``unrecognised`` instead of being coerced into one of these.
BRANCH_REASONS: Tuple[str, ...] = ("fork", "compaction", "manual", "abandoned")

#: Where T3's session tree lives under an artifact root. Named here because
#: ``runtime.session_tree.fork_api`` documents the convention and this is the
#: module that has to agree with it; a tree written anywhere else would be a
#: second tree.
TREE_ROOT_NAME = "_tree"

#: The reasons a session may end. ``compression`` is the one lineage rotation
#: produces; the others are recorded so a reader can tell a rotation apart from
#: a normal end without reading prose.
ROTATION_REASONS: Tuple[str, ...] = (
    "compression",
    "user_fork",
    "abandoned",
    "completed",
)

#: The literal that marks a GENERATED branch summary everywhere it renders.
#: Declared as one constant so a surface cannot spell it differently and a test
#: can assert the label is present without matching prose.
BRANCH_SUMMARY_LABEL = "GENERATED SUMMARY - not a transcript fact"

#: The same label for a COMPACTION summary, which is also generated. W2.2's
#: finding is that these are two different producers writing two different
#: kinds of claim, and Wave 1 only labelled one of them: ``branch_summary``
#: (T3's tree entry) got a label while the compaction summary in
#: ``session["summaries"]`` rendered as an ordinary turn. A compaction summary
#: is a model's account of the conversation exactly as much as a branch summary
#: is, so it carries the SAME label. Two constants rather than one because they
#: are two claims from two producers and a future round must be able to tell
#: them apart in a receipt; the TEXT is identical on purpose, so a reader
#: learns one word.
COMPACTION_SUMMARY_LABEL = "GENERATED SUMMARY - not a transcript fact"

#: Both labels. Used by the export and by the gate that refuses an unlabelled
#: generated summary.
GENERATED_LABELS: Tuple[str, ...] = (
    BRANCH_SUMMARY_LABEL,
    COMPACTION_SUMMARY_LABEL,
)

_UNTRUSTED = "not a transcript fact"


# ---------------------------------------------------------------------------
# Reading lineage. One adapter, tolerant of both shapes.
# ---------------------------------------------------------------------------


def _session_path(log_root: Any, session_id: str) -> Optional[Path]:
    """The snapshot path for one session, through ``cli.session``'s own path rule.

    Delegated rather than re-derived: ``cli/session.py`` owns the layout and
    the id grammar, and a second copy of that rule is how a surface starts
    reporting "no such session" for a session that exists.
    """
    try:
        from cli import session as _session

        path = _session._session_path(log_root, str(session_id))
        return Path(str(path)) if path else None
    except Exception:
        return None


def _load(log_root: Any, session_id: str) -> Dict[str, Any]:
    """Load one session snapshot, or ``{}``.

    **Read-only, and that is load-bearing.** The obvious implementation is
    ``cli.session.load_or_create``, which is what this function called before
    W2.2 - and ``load_or_create`` CREATES the session when it is missing. So
    asking ``/lineage show sess-00000000`` for a conversation that does not
    exist minted one, and then reported it: the lineage view answered "0
    turn(s)" for a session the reader had just caused to be created by looking
    at it. That is a read that writes, and it makes "no record" and "a brand new
    empty conversation" render identically.

    So the existence check happens HERE, before the loader is reached, and a
    missing snapshot is ``{}``.
    """
    path = _session_path(log_root, session_id)
    if path is None:
        return {}
    try:
        if not Path(path).is_file():
            return {}
    except OSError:
        return {}
    try:
        from cli import session as _session

        return _session.load_or_create(log_root, None, str(session_id), strict=False)
    except Exception:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, Mapping) else {}


def _session_id(session: Mapping[str, Any]) -> str:
    return str(session.get("session_id") or "")


def _parent_of(session: Mapping[str, Any]) -> str:
    for key in ("parent_session_id", "parent", "rotated_from"):
        value = session.get(key)
        if value:
            return str(value)
    return ""


def _child_link(session: Mapping[str, Any]) -> str:
    for key in ("rotated_to", "child_session_id", "rotated_to_session_id", "successor"):
        value = session.get(key)
        if value:
            return str(value)
    return ""


def _ended_reason(session: Mapping[str, Any]) -> str:
    for key in ("end_reason", "ended_reason", "close_reason", "end_cause"):
        value = session.get(key)
        if value:
            return str(value)
    return ""


def lineage_chain(
    log_root: Any, session_id: Any, *, max_depth: int = 64
) -> List[Dict[str, Any]]:
    """Every ancestor of a session, nearest first, ending at the root.

    Walks ``parent_session_id`` upward. Bounded by ``max_depth`` and by a
    SEEN SET: a cycle in the parent chain (a corrupt snapshot, or a hand-edited
    one) stops the walk instead of hanging a status surface on a repaint timer.

    A session whose parent snapshot cannot be read is still reported, with
    ``loaded: False`` and the reason, because "my parent is missing" is a fact
    a user needs and omitting the row would hide it.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    current = str(session_id or "")
    depth = 0
    while current and depth < max(1, int(max_depth)):
        if current in seen:
            out.append(
                {
                    "session_id": current,
                    "loaded": False,
                    "reason": "the parent chain loops back to a session already visited",
                }
            )
            break
        seen.add(current)
        session = _load(log_root, current)
        if not session:
            out.append(
                {
                    "session_id": current,
                    "loaded": False,
                    "reason": "this session's record could not be read",
                }
            )
            break
        out.append(
            {
                "session_id": current,
                "loaded": True,
                "parent_session_id": _parent_of(session),
                "child_session_id": _child_link(session),
                "end_reason": _ended_reason(session),
                "turns": len(session.get("turns") or []),
                "compaction_count": int(session.get("compaction_count") or 0),
                "depth": depth,
            }
        )
        current = _parent_of(session)
        depth += 1
    return out


def _index_children(log_root: Any, session_id: str) -> List[Dict[str, Any]]:
    """Sessions whose parent is this one, from the global session index."""
    try:
        from cli import session as _session

        records = _session.index_records(log_root=log_root)
    except Exception:
        return []
    wanted = str(session_id or "")
    out = []
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("parent_session_id") or "") == wanted and wanted:
            out.append(dict(record))
    return out


def lineage_rows(log_root: Any, session_id: Any) -> List[Dict[str, Any]]:
    """The whole lineage of a session as flat rows: root first, self, then children.

    One list rather than a nested structure because every surface that reads it
    renders rows, and a nested tree a renderer must flatten is a place for two
    surfaces to disagree about the order.
    """
    chain = lineage_chain(log_root, session_id)
    rows: List[Dict[str, Any]] = []
    for entry in reversed(chain):
        row = dict(entry)
        row["relation"] = "self" if entry.get("depth") == 0 else "parent"
        rows.append(row)
    for child in _index_children(log_root, session_id):
        rows.append(
            {
                "session_id": str(child.get("session_id") or ""),
                "loaded": True,
                "parent_session_id": str(session_id or ""),
                "child_session_id": "",
                "end_reason": str(child.get("status") or ""),
                "turns": int(child.get("turns") or 0),
                "relation": "child",
                "depth": 1,
            }
        )
    return rows


def lineage_lines(rows: Iterable[Mapping[str, Any]], *, width: int = 78) -> List[str]:
    """Render a lineage as a tree, with a rotation's reason and time named."""
    rendered: List[str] = ["session lineage"]
    rows = list(rows or ())
    if not rows:
        rendered.append("  (no session record could be read for this conversation)")
        return rendered
    by_depth: Dict[int, List[Mapping[str, Any]]] = {}
    for row in rows:
        by_depth.setdefault(int(row.get("depth") or 0), []).append(row)
    for depth in sorted(by_depth):
        for row in by_depth[depth]:
            indent = "  " * (depth + 1)
            sid = str(row.get("session_id") or "?")
            marker = {"self": "*", "parent": "-", "child": "+"}.get(
                str(row.get("relation") or ""), " "
            )
            detail = "{turns} turn(s)".format(turns=row.get("turns"))
            compactions = int(row.get("compaction_count") or 0)
            if compactions:
                detail += f", {compactions} compaction(s)"
            reason = str(row.get("end_reason") or "")
            if reason:
                detail += f", ended: {reason}"
            if not row.get("loaded", True):
                detail = str(row.get("reason") or "record could not be read")
            rendered.append(f"{marker} {indent}{sid}  {detail}")
    rendered.append("  (* is this conversation; - is what it continues; + is what continues it)")
    return [_bounded(line, width) for line in rendered]


def rotation_events(
    log_root: Any, session_id: Any, *, session: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Every compaction/rotation event for a session, from its own records.

    Reads three places, in this order, and says which one it used:

    1. the session snapshot's own ``summaries`` list (this tree's in-place
       compaction records - a compaction happened, and no session ended);
    2. the snapshot's explicit rotation link (``rotated_to`` + ``end_reason``),
       which is the SHAPE T3's prompt specifies;
    3. the session's event journal, for a ``compaction``/``session_rotated`` row.

    ``kind`` reports which: ``rotation`` means a session ENDED and a child
    opened; ``compaction`` means the context was compacted in place and the
    session continued. Rendering those two identically is exactly the "a user
    whose history silently splits is a user who cannot find their work"
    failure, so the distinction is data here rather than a rendering decision.
    """
    record = dict(session) if isinstance(session, Mapping) else _load(log_root, session_id)
    events: List[Dict[str, Any]] = []
    sid = _session_id(record) or str(session_id or "")

    child = _child_link(record)
    reason = _ended_reason(record)
    if child:
        events.append(
            {
                "kind": "rotation",
                "reason": reason or "compression",
                "parent_session_id": sid,
                "child_session_id": child,
                "at": record.get("rotated_at") or record.get("ended_at"),
                "source": "session snapshot",
                "lost": False,
            }
        )

    summaries = record.get("summaries")
    if isinstance(summaries, list):
        for index, entry in enumerate(summaries):
            if not isinstance(entry, Mapping):
                continue
            events.append(
                {
                    "kind": "compaction",
                    "reason": "compression",
                    "parent_session_id": sid,
                    "child_session_id": "",
                    "at": entry.get("ts"),
                    "turn_count": int(entry.get("turn_count") or 0),
                    "source_turn_ids": [
                        str(item) for item in (entry.get("source_turn_ids") or ())
                    ],
                    "summary": str(entry.get("text") or ""),
                    "source": f"snapshot summaries[{index}]",
                    "lost": False,
                }
            )

    for row in _journal_rows(log_root, sid):
        kind = str(row.get("event") or row.get("kind") or "").strip().lower()
        # The envelope is `{event, payload, ts}` in the normalized journal and
        # `{kind, data, ts}` in the legacy one. Reading only `data` made every
        # compaction row resolve to an EMPTY payload, which is how one
        # compaction produced a second record claiming zero turns were folded
        # away. Both names are checked, in that order.
        payload_body = None
        for key in ("data", "payload"):
            candidate = row.get(key)
            if isinstance(candidate, Mapping):
                payload_body = candidate
                break
        data = payload_body if payload_body is not None else row
        if kind in ("compaction", "session_compacted", "context_compacted"):
            # The journal row keeps the compacted turns themselves, so its turn
            # count is the LENGTH of that list rather than a number somebody
            # remembered to write. Reading `dropped_messages` (which this
            # producer never emits) produced `turn_count: 0`, which is how the
            # same compaction came to be reported twice with two different
            # counts.
            compacted = (data or {}).get("compacted_turns")
            compacted = compacted if isinstance(compacted, list) else []
            turn_ids = [
                str(item.get("turn_id") or "")
                for item in compacted
                if isinstance(item, Mapping)
            ]
            events.append(
                {
                    "kind": "compaction",
                    "reason": "compression",
                    "parent_session_id": sid,
                    "child_session_id": "",
                    "at": row.get("ts") or (data or {}).get("ts"),
                    "turn_count": len(turn_ids)
                    or int(
                        (data or {}).get("turn_count")
                        or (data or {}).get("dropped_messages")
                        or 0
                    ),
                    "source_turn_ids": turn_ids,
                    "summary": str((data or {}).get("summary") or ""),
                    "source": "session journal",
                    "lost": False,
                }
            )
        elif kind in ("session_rotated", "session_rotation"):
            events.append(
                {
                    "kind": "rotation",
                    "reason": str((data or {}).get("reason") or "compression"),
                    "parent_session_id": sid,
                    "child_session_id": str((data or {}).get("child_session_id") or ""),
                    "at": row.get("ts"),
                    "source": "session journal",
                    "lost": False,
                }
            )

    # The SAME compaction is visible in two places: the snapshot's `summaries`
    # list and the session journal's `compaction` row. Reporting both would
    # tell a user their conversation was compacted twice, so the two are
    # collapsed on the identity of the COMPACTION rather than of the place it
    # was recorded:
    #
    # * the SET OF TURN IDS it folded away, when either record names them. Two
    #   records naming the same turns ARE the same compaction, and this holds
    #   even if one record's timestamp is missing - which it is, today: the
    #   snapshot carries `ts` and the journal row carries none.
    # * otherwise (kind, timestamp, turn count), for a record that names
    #   neither.
    #
    # The KEPT entry is the one with the most `source_turn_ids` and then the
    # larger turn count, because the snapshot's summary names the turns and the
    # journal row's `ts` is absent - so "first wins" would keep the weaker
    # record about the fact a reader is about to act on.
    merged: Dict[Any, Dict[str, Any]] = {}
    for event in events:
        ids = event.get("source_turn_ids") or ()
        marker: Any
        if ids:
            marker = (event["kind"], frozenset(str(i) for i in ids))
        else:
            marker = (
                event["kind"],
                round(float(event.get("at") or 0.0), 3),
                int(event.get("turn_count") or 0),
            )
        winner = merged.get(marker)
        if winner is None:
            merged[marker] = event
            continue
        challenger_rank = (len(ids), int(event.get("turn_count") or 0))
        winner_rank = (
            len(winner.get("source_turn_ids") or ()),
            int(winner.get("turn_count") or 0),
        )
        if challenger_rank > winner_rank:
            merged[marker] = event
    events = list(merged.values())

    rotations = [event for event in events if event["kind"] == "rotation"]
    compactions = [event for event in events if event["kind"] == "compaction"]
    return {
        "session_id": sid,
        "events": events,
        "rotation_count": len(rotations),
        "compaction_count": len(compactions),
        "rotated": bool(rotations),
        "last": events[-1] if events else {},
        "shape": (
            "rotation"
            if rotations
            else ("in_place_compaction" if compactions else "none")
        ),
        "reason": (
            "this build records a compaction without rotating to a child "
            "session, so the chain of sessions is unbroken; a rotation "
            "recorded here means a session ENDED"
            if not rotations and compactions
            else ""
        ),
    }


def last_rotation(log_root: Any, session_id: Any) -> Dict[str, Any]:
    """The last rotation or compaction for a session, or ``{}``.

    This is the half ``cli/context_budget.py`` reads for its
    "last compaction: parent session" field, so the budget surface and the
    lineage browser cannot disagree about what the last rotation was.
    """
    return rotation_events(log_root, session_id).get("last") or {}


def _journal_rows(log_root: Any, session_id: str) -> List[Dict[str, Any]]:
    """The session's own event journal rows. Never raises."""
    path = _session_path(log_root, session_id)
    if path is None:
        return []
    journal = Path(str(path)).with_suffix(".events.jsonl")
    for candidate in (journal, Path(str(path)).parent / f"{session_id}.events.jsonl"):
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, Mapping):
                rows.append(dict(item))
        if rows:
            return rows
    return []


# ---------------------------------------------------------------------------
# "Show me what happened before this compaction" - cheap, and it must be.
# ---------------------------------------------------------------------------


def pre_compaction_history(
    log_root: Any, session_id: Any, *, index: int = -1, limit: int = 50
) -> Dict[str, Any]:
    """The turns a compaction summarised away, for one compaction.

    Cheap by construction: it reads the session's OWN ``compacted_turns`` /
    ``raw_turns`` lists and returns a bounded slice. It does not replay the
    journal and it does not open a task directory, because "what happened
    before" is the question a person asks at the moment they suspect
    something was lost, and an answer that takes a full replay is not an
    answer.

    ``index`` is 1-based over the session's compactions; ``-1`` (the default)
    is the most recent, which is the one a person asking "what did that
    compaction take?" means.
    """
    session = _load(log_root, session_id)
    if not session:
        return {
            "available": False,
            "reason": "this session's record could not be read",
            "turns": [],
            "compaction_count": 0,
        }
    summaries = session.get("summaries")
    summaries = summaries if isinstance(summaries, list) else []
    compacted = session.get("compacted_turns")
    compacted = compacted if isinstance(compacted, list) else []
    raw = session.get("raw_turns")
    raw = raw if isinstance(raw, list) else []

    if not summaries and not compacted:
        return {
            "available": False,
            "reason": "this session has never been compacted, so there is no "
            "pre-compaction history to show",
            "turns": [],
            "compaction_count": int(session.get("compaction_count") or 0),
        }

    wanted = int(index) if index not in (0, None) else len(summaries) or 1
    if wanted < 0:
        wanted = len(summaries) or 1
    summary = summaries[wanted - 1] if 0 < wanted <= len(summaries) else {}

    # The turns that compaction folded away are the ones whose ids it named.
    # Falling back to the whole compacted list is honest here because it is the
    # same list, just not narrowed by the record.
    named = {str(item) for item in (summary.get("source_turn_ids") or ())}
    pool = [turn for turn in compacted if isinstance(turn, Mapping)]
    if named:
        narrowed = [turn for turn in pool if str(turn.get("turn_id") or "") in named]
        if narrowed:
            pool = narrowed

    bound = max(1, int(limit))
    tail = pool[-bound:]
    return {
        "available": True,
        "reason": "",
        "index": wanted,
        "compaction_count": int(session.get("compaction_count") or 0),
        "dropped_total": len(pool),
        "shown": len(tail),
        "truncated": len(pool) > len(tail),
        "turns": [dict(turn) for turn in tail],
        "summary": str(summary.get("text") or ""),
        "retained_after_compaction": len(session.get("turns") or []),
        "history_retained": bool(raw),
    }


def pre_compaction_lines(report: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render the pre-compaction history, stating plainly that nothing was lost.

    W2.2's finding, and it is the one this surface had: Wave 1 rendered the
    TURNS faithfully and printed the word "summary" in the header, but never
    labelled the SUMMARY itself. The compaction summary is a model's account of
    the conversation - exactly as much a claim as a branch summary - and it was
    reaching the reader as an ordinary row of prose between two transcripts.

    So the summary is now rendered as its own labelled block, first, before any
    turn, with :data:`COMPACTION_SUMMARY_LABEL` in the sentence.
    """
    report = report if isinstance(report, Mapping) else {}
    if not report.get("available"):
        return [_bounded(f"pre-compaction history  {report.get('reason') or 'unavailable'}", width)]

    lines = [
        "pre-compaction history  compaction {index} of {total} · {dropped} turn(s) "
        "were summarised away · {retained} kept in the window".format(
            index=report.get("index"),
            total=report.get("compaction_count"),
            dropped=report.get("dropped_total"),
            retained=report.get("retained_after_compaction"),
        )
    ]
    # The re-fetchability reassurance, stated as its own line and BEFORE the
    # summary. The turns are on disk, which is what makes compaction
    # recoverable at all - and a reader who believes otherwise will refuse to
    # let a conversation grow, which is a worse outcome than the compaction.
    lines.append(
        "  every one of them is still on disk - a summary is an INDEX into the "
        "history, not a replacement for it"
    )
    # ---- the generated summary, labelled --------------------------------
    lines.append("")
    lines.append(COMPACTION_SUMMARY_LABEL)
    lines.append(
        "  a model's account of those {n} turn(s), NOT a transcript record".format(
            n=int(report.get("dropped_total") or 0)
        )
    )
    summary = str(report.get("summary") or "")
    if not summary.strip():
        lines.append("    (no summary text was recorded for this compaction)")
    for row in summary.splitlines() or [""]:
        lines.append("    " + _bounded(_sanitize(row), max(20, width - 4)))
    # The underlying turns remain reachable, and this names the door.
    lines.append(
        "  the turns it was written from are below, verbatim - compare the two "
        "and do not trust the summary on its own"
    )
    lines.append("")
    if report.get("truncated"):
        lines.append(
            f"  showing the most recent {report.get('shown')} of {report.get('dropped_total')}"
        )
    lines.append("the turns that went into it:")
    for turn in report.get("turns") or ():
        if not isinstance(turn, Mapping):
            continue
        role = str(turn.get("role") or "?")
        text = " ".join(str(turn.get("text") or "").split())
        lines.append(f"    {role}: {_bounded(_sanitize(text), max(20, width - 8))}")
    return [_bounded(line, width) for line in lines]


# ---------------------------------------------------------------------------
# Search, deduped across the lineage.
# ---------------------------------------------------------------------------


def _identity_of(entry: Mapping[str, Any]) -> str:
    """The dedupe key for one session row.

    ``turn_id`` when the row carries one (a turn is a turn across sessions),
    otherwise a digest of its identifying text. Two answers rather than one
    heuristic, because a fork copies turn ids verbatim and that copy is the
    duplicate a user would otherwise see twice.
    """
    for key in ("turn_id", "id", "event_id"):
        value = str(entry.get(key) or "")
        if value:
            return f"turn:{value}"
    for key in ("text", "issue", "title", "summary"):
        value = " ".join(str(entry.get(key) or "").split()).lower()
        if value:
            import hashlib

            return "text:" + hashlib.sha256(
                value.encode("utf-8", "replace")
            ).hexdigest()[:24]
    return "row:" + str(entry.get("session_id") or "")


def searchable_turns(
    log_root: Any, session_id: Any, *, limit: int = 500
) -> List[Dict[str, Any]]:
    """Every turn in this session's LINEAGE, flattened into search rows.

    The lineage is the ancestors plus the session itself, and for EACH of them
    three turn lists: the live window, the compacted turns, and the raw turns.
    Reading only the live window is why "search my conversation" finds nothing
    after a compaction - the turns exist, they are simply not in the window any
    more, which is the whole reason the pre-compaction surface exists.

    Bounded by ``limit`` across the WHOLE lineage rather than per session, and
    the bound is the caller's to report: a caller that renders this without
    saying so is presenting a truncated history as a complete one.
    """
    rows: List[Dict[str, Any]] = []
    bound = max(1, int(limit))
    for entry in lineage_chain(log_root, session_id):
        sid = str(entry.get("session_id") or "")
        if not entry.get("loaded", False) or not sid:
            continue
        session = _load(log_root, sid)
        seen_ids: set = set()
        for key in ("turns", "compacted_turns", "raw_turns"):
            for turn in session.get(key) or ():
                if not isinstance(turn, Mapping):
                    continue
                turn_id = str(turn.get("turn_id") or "")
                # A fork copies turn ids verbatim, so the SAME turn appears in
                # three lists of one session and in every ancestor. Collapse
                # here rather than in `dedupe_across_lineage`, which collapses
                # SEARCH RESULTS - this is the same fact read twice before it
                # ever becomes a result.
                marker = f"{sid}:{turn_id}" if turn_id else f"{sid}:{turn.get('text')!r}"
                if marker in seen_ids:
                    continue
                seen_ids.add(marker)
                rows.append(
                    {
                        "session_id": sid,
                        "turn_id": turn_id,
                        "role": str(turn.get("role") or ""),
                        "text": str(turn.get("text") or ""),
                        "ts": turn.get("ts") or turn.get("created_at") or "",
                        "source": key,
                        "depth": int(entry.get("depth") or 0),
                    }
                )
                if len(rows) >= bound:
                    return rows
    return rows


def dedupe_across_lineage(
    rows: Iterable[Mapping[str, Any]],
    *,
    lineage: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Collapse the same content appearing in a parent AND a child to one row.

    A user seeing the same result twice thinks the system is broken, and a
    search that returns a fork's history under both the fork and its parent is
    reporting the storage layout instead of the answer.

    The kept row is the one whose session is **newest**, because the newest is
    where the content lives now; the collapsed sessions are recorded on the kept
    row so a reader can see it also appeared in an ancestor rather than
    wondering where the duplicate went.
    """
    lineage_ids = {
        str(row.get("session_id") or "")
        for row in (lineage or ())
        if isinstance(row, Mapping)
    }
    # Preserve the CALLER'S order rather than inventing one. `searchable_turns`
    # already emits newest-first (live window, then the compacted turns, then
    # the raw ones), and re-sorting here by a timestamp string destroyed that:
    # turns frequently carry NO timestamp, so every row tied on the empty value
    # and the results came back in whatever order the session happened to hash.
    # The explicit position is therefore the order, ascending, and `ts` is only
    # a tiebreak when it is actually present.
    ordered = []
    for position, row in enumerate(rows or ()):
        if not isinstance(row, Mapping):
            continue
        entry = dict(row)
        entry["_position"] = position
        ordered.append(entry)
    ordered.sort(
        key=lambda row: (
            int(row["_position"]),
            str(row.get("updated_at") or ""),
        )
    )
    kept: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    collapsed = 0
    for row in ordered:
        key = _identity_of(row)
        owner = seen.get(key)
        if owner is None:
            row = dict(row)
            row["also_in"] = []
            seen[key] = row
            kept.append(row)
            continue
        collapsed += 1
        also = owner.setdefault("also_in", [])
        session_id = str(row.get("session_id") or "")
        if session_id and session_id not in also:
            also.append(session_id)
    for row in kept:
        row.pop("_position", None)
    return {
        "rows": kept,
        "shown": len(kept),
        "collapsed": collapsed,
        "lineage_sessions": sorted(lineage_ids),
        "note": (
            "results appearing in both a parent session and its child are "
            "shown once, on the newest, and the ancestor is named"
            if collapsed
            else ""
        ),
    }


def search_lineage(
    log_root: Any,
    session_id: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
) -> Dict[str, Any]:
    """Search a session's lineage and return ONE result per piece of content.

    The lineage set is the ancestors plus the session itself plus its children,
    so a search for something said before a rotation still finds it.
    """
    lineage = lineage_rows(log_root, session_id)
    report = dedupe_across_lineage(rows, lineage=lineage)
    if query:
        needle = str(query).strip().lower()
        if needle:
            report["rows"] = [
                row
                for row in report["rows"]
                if needle in " ".join(str(row.get(key) or "") for key in ("text", "issue", "title")).lower()
            ]
            report["shown"] = len(report["rows"])
    report["lineage"] = lineage
    report["query"] = str(query or "")
    return report


def search_lines(report: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render a lineage-deduped search result."""
    report = report if isinstance(report, Mapping) else {}
    rows = report.get("rows") or []
    lines = [
        "search  {shown} result(s){query} across {sessions} session(s) in "
        "this lineage".format(
            shown=len(rows),
            query=f" for {report['query']!r}" if report.get("query") else "",
            sessions=len(report.get("lineage_sessions") or ()),
        )
    ]
    if report.get("collapsed"):
        lines.append(
            "  {n} duplicate(s) collapsed - the same content in a parent and "
            "a child is one result".format(n=int(report["collapsed"]))
        )
    for row in rows[:40]:
        # NOTE: `" ".join(gen).split()` returns a LIST, not a string - the
        # first version of this rendered `['a', 'unique', 'note']` as a search
        # result because the join produced one string and the split undid it.
        text = " ".join(
            " ".join(str(row.get(key) or "").split())
            for key in ("text", "issue", "title")
        ).strip()
        lines.append(
            "  {sid}  {text}".format(
                sid=str(row.get("session_id") or "?")[:24],
                text=_bounded(_sanitize(text), max(20, width - 28)),
            )
        )
        for other in row.get("also_in") or ():
            lines.append(f"      also in ancestor session {other}")
    if len(rows) > 40:
        lines.append(f"  (+{len(rows) - 40} more)")
    if not rows:
        lines.append("  nothing matched")
    return [_bounded(line, width) for line in lines]


# ---------------------------------------------------------------------------
# T3's session tree. READ-ONLY adapter first; the writers come after it.
# ---------------------------------------------------------------------------


def tree_root(log_root: Any, session_id: Any) -> Optional[Path]:
    """Where T3's tree for this conversation lives, or ``None``.

    ``runtime.session_tree.fork_api`` documents the convention as
    ``SessionTree(log_root / "_tree" / session_id)`` and nothing enforces it,
    so this is the module that has to agree. Returning ``None`` for an
    unresolvable root rather than guessing a directory is deliberate: a tree
    browser that invented a path would report "no branches" over a tree that
    exists somewhere else, which is the failure this whole module exists to
    prevent.
    """
    if log_root is None or not str(session_id or ""):
        return None
    try:
        return Path(str(log_root)) / TREE_ROOT_NAME / str(session_id)
    except (TypeError, ValueError):
        return None


def _session_tree(log_root: Any, session_id: Any) -> Any:
    """A constructed ``SessionTree``, or ``None``.

    Construction writes NOTHING (``ensure_dir`` is only reached through
    ``_append_row``), so building one against a path that does not exist is
    safe and is how a caller reads an absent tree without creating it.
    """
    root = tree_root(log_root, session_id)
    if root is None:
        return None
    try:
        from runtime.session_tree import SessionTree
    except Exception:
        return None
    try:
        return SessionTree(root)
    except Exception:
        return None


def _tree_module() -> Any:
    try:
        from runtime import session_tree

        return session_tree
    except Exception:
        return None


def branch_reason_of(row: Mapping[str, Any]) -> str:
    """Why this branch exists, as one of :data:`BRANCH_REASONS` or ``unrecorded``.

    The three reasons a reader needs are ``fork`` (a person asked for one),
    ``compaction`` (the conversation rotated because it got too long) and
    ``manual`` (somebody moved the head). Wave 1's lineage view printed the
    ``end_reason`` a session happened to carry, which conflates "how this
    session ended" with "why this branch exists" - a completed session and a
    compacted one both ended, and only one of them created a branch.

    A recorded reason always wins. When the producer recorded none, the ENTRY
    KIND is used, and that is a derivation rather than a guess: T3's
    ``ENTRY_KINDS`` are ``(root, turn, branch, branch_summary, note, head)``, a
    ``branch`` entry can only be created by ``SessionTree.fork``, and a
    ``branch_summary`` can only be written by ``abandon``. A ``turn`` or
    ``root`` falls to ``manual`` because those are the head-moving entries.

    The ``unrecorded`` answer is kept for a kind this build does not
    recognise, because rendering an unknown entry as ``manual`` would tell a
    reader that somebody moved the head when the truth is that this build
    cannot say what the entry is.
    """
    raw = str(
        row.get("created_reason")
        or row.get("reason")
        or row.get("opened_by")
        or ""
    ).strip().lower()
    if raw in BRANCH_REASONS:
        return raw
    if raw in ("compression", "compact", "compacted"):
        return "compaction"
    if raw in ("user_fork", "forked", "clone", "cloned"):
        return "fork"
    if raw in ("rewind", "move_head", "head_moved", "undo", "merge"):
        return "manual"
    kind = str(row.get("kind") or "").strip().lower()
    if kind == "branch":
        return "fork"
    if kind == "branch_summary":
        return "abandoned"
    if kind in ("turn", "root", "head"):
        return "manual"
    return "unrecorded"


def tree_snapshot(log_root: Any, session_id: Any) -> Dict[str, Any]:
    """T3's tree for one conversation, plus the lineage a reader needs.

    Every field carries its own absent rule. ``present`` is ``False`` for a
    conversation that has never been forked - which is the overwhelming
    majority, and it is a FACT rather than an error, so the view renders a
    one-line "this conversation has no branches yet" instead of an empty
    panel.

    ``rows`` are flat and in journal order (oldest first) because that is the
    order the tree's own ``path_to_head`` walks, and a tree view whose order
    disagreed with the head's ancestry would be showing the reader a structure
    that does not exist.
    """
    out: Dict[str, Any] = {
        "available": False,
        "present": False,
        "root": "",
        "session_id": str(session_id or ""),
        "head_id": "",
        "head_kind": "",
        "head_is_claim": False,
        "head_authority": "",
        "rows": [],
        "branches": [],
        "claims": [],
        "entries": 0,
        "verify": {},
        "reason": "",
        "source": "runtime.session_tree",
    }
    tree = _session_tree(log_root, session_id)
    module = _tree_module()
    if tree is None or module is None:
        out["reason"] = (
            "the session tree module could not be loaded, so the branch view "
            "cannot be read; this is not the same as there being no branches"
        )
        return out

    root = tree_root(log_root, session_id)
    out["root"] = str(root or "")
    out["present"] = bool(getattr(tree, "entries_path", None) and tree.entries_path.is_file())
    if not out["present"]:
        out["available"] = True
        out["reason"] = (
            "this conversation has never been branched, so it has no tree yet - "
            "/fork creates one"
        )
        return out

    head = None
    try:
        head = tree.head()
    except Exception:
        head = None
    head_id = str(getattr(head, "id", "") or "")
    out["available"] = True
    out["head_id"] = head_id
    out["head_kind"] = str(getattr(head, "kind", "") or "")
    out["head_is_claim"] = bool(getattr(head, "claim", False))

    chain: List[Any] = []
    try:
        chain = list(tree.path_to_head(head_id) or ())
    except Exception:
        chain = []
    on_head_path = {str(getattr(item, "id", "") or "") for item in chain}

    rows: List[Dict[str, Any]] = []
    try:
        entries = list(tree.entries() or ())
    except Exception:
        entries = []
    for entry in entries:
        entry_id = str(getattr(entry, "id", "") or "")
        kind = str(getattr(entry, "kind", "") or "")
        row = {
            "id": entry_id,
            "parent_id": str(getattr(entry, "parent_id", "") or ""),
            "kind": kind,
            "turn_id": str(getattr(entry, "turn_id", "") or ""),
            "text": str(getattr(entry, "text", "") or ""),
            "claim": bool(getattr(entry, "claim", False)),
            "evidence": [str(item) for item in (getattr(entry, "evidence", ()) or ())],
            "created_at": str(getattr(entry, "created_at", "") or ""),
            "seq": int(getattr(entry, "seq", 0) or 0),
            "is_head": entry_id == head_id,
            "on_head_path": entry_id in on_head_path,
            "reason": branch_reason_of(
                {
                    "kind": kind,
                    "created_reason": (getattr(entry, "payload", {}) or {}).get(
                        "reason"
                    )
                    if isinstance(getattr(entry, "payload", None), Mapping)
                    else "",
                }
            ),
        }
        rows.append(row)

    out["rows"] = rows
    out["entries"] = len(rows)
    out["branches"] = [row for row in rows if row["kind"] == "branch"]
    out["claims"] = [row for row in rows if row["claim"]]
    try:
        out["head_authority"] = str((tree.head_mirror() or {}).get("reason") or "")
    except Exception:
        out["head_authority"] = ""
    try:
        out["verify"] = dict(tree.verify() or {})
    except Exception:
        out["verify"] = {}
    if out["verify"] and not out["verify"].get("ok", True):
        out["reason"] = (
            "this tree failed its own integrity check, so what follows is "
            "reported rather than trusted"
        )
    return out


def tree_lines(snapshot: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render the tree: lineage, WHY each branch exists, and where the head is.

    Three things Wave 1's lineage view did not show, each one a question a
    reader has before touching a branch:

    * **creation reason** - a fork and a compaction look identical in a list of
      names, and they have opposite safety properties.
    * **head position** - which entry the tree currently points at, and whether
      the point is a CLAIM (a summary can never be the head; T3 refuses to move
      a head onto one, and this view says so rather than implying it).
    * **parentage** - rendered as an indent from the real ``parent_id`` chain,
      not from a name guess.

    Never empty, including for a conversation with no tree at all.
    """
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if not snapshot.get("available"):
        return [
            "session tree  unavailable - "
            + _plain(snapshot.get("reason") or "unknown", max(20, width - 22))
        ]
    if not snapshot.get("present"):
        lines = [
            "session tree  this conversation has never been branched, so it has "
            "no tree yet"
        ]
        lines.append("  /fork creates one - and it will say whether it is isolated")
        lines.append("  nothing above is missing data; a conversation with no " "branches has none")
        return lines

    rows = list(snapshot.get("rows") or ())
    head_id = str(snapshot.get("head_id") or "")
    depth: Dict[str, int] = {}
    for row in rows:
        parent = str(row.get("parent_id") or "")
        depth[str(row.get("id") or "")] = 0 if not parent else depth.get(parent, 0) + 1

    lines = [
        "session tree  {n} entr(ies) · {b} branch(es) · {c} generated "
        "claim(s)".format(
            n=int(snapshot.get("entries") or 0),
            b=len(snapshot.get("branches") or ()),
            c=len(snapshot.get("claims") or ()),
        )
    ]
    lines.append(
        "  head: {head}{kind}{claim}".format(
            head=head_id or "(none)",
            kind=f" · kind {snapshot['head_kind']}" if snapshot.get("head_kind") else "",
            claim="  <-- this head is a GENERATED CLAIM, not a transcript fact"
            if snapshot.get("head_is_claim")
            else "",
        )
    )
    for row in rows[:60]:
        indent = "  " * (2 + depth.get(str(row.get("id") or ""), 0))
        marker = "H" if row.get("is_head") else ("c" if row.get("claim") else "-")
        lines.append(
            "{indent}{marker} {eid}  {kind}  created because: {reason}".format(
                indent=indent,
                marker=marker,
                eid=_plain(row.get("id") or "?", 22),
                kind=_plain(row.get("kind") or "?", 14),
                reason=_plain(row.get("reason") or "unrecorded", 22),
            )
        )
        if row.get("claim"):
            lines.append(
                indent
                + "  "
                + _plain(BRANCH_SUMMARY_LABEL, max(20, width - len(indent) - 6))
            )
            lines.append(
                indent
                + "  "
                + _plain(
                    "cites {} entr(ies); it is an INDEX into the branch, not a "
                    "replacement for it".format(len(row.get("evidence") or ())),
                    max(20, width - len(indent) - 6),
                )
            )
        if not row.get("on_head_path") and row.get("kind") == "branch":
            lines.append(
                indent
                + "  "
                + _plain(
                    "not on the path to the head - merging or deleting this "
                    "branch does not move the head",
                    max(20, width - len(indent) - 6),
                )
            )
    if len(rows) > 60:
        lines.append(f"  (+{len(rows) - 60} more entries - /tree prune reports the rest)")
    lines.append(
        "  (H is the head · c is a generated claim · - is an ordinary entry)"
    )
    verify = snapshot.get("verify") or {}
    if verify and verify.get("ok") is False:
        lines.append(
            "  INTEGRITY: FAILED - " + _plain(snapshot.get("reason") or "", max(20, width - 22))
        )
    return [_bounded(line, width) for line in lines]


def _tree_of_session(log_root: Any, session_id: Any) -> Tuple[Any, Any, Any]:
    """``(tree, module, root)`` for one conversation, or three ``None``s."""
    return _session_tree(log_root, session_id), _tree_module(), tree_root(log_root, session_id)


# ---------------------------------------------------------------------------
# The fork disclosure. Non-negotiable, so it is a function both shells call.
# ---------------------------------------------------------------------------


def fork_isolation_lines(
    *,
    isolation: str,
    parent_session_id: str,
    child_session_id: str,
    parent_tree: str = "",
    child_tree: str = "",
    reason: str = "",
) -> List[str]:
    """The moment-of-consent block, rendered from what ACTUALLY happened.

    ``isolation`` is one of three values and the third is the reason this
    function exists:

    ``isolated``
        A separate working tree was created. Two copies of the working state
        now exist and diverge independently.
    ``refused``
        Isolation was required and could not be had. The fork did NOT happen
        and nothing diverged - the user is told that rather than handed two
        copies.
    ``shared``
        No isolation mechanism was available. The fork happened on the SAME
        writable tree, which is stated plainly and never dressed up as the
        first case. A user who forks and does not learn this has two divergent
        copies of their work.

    Every line says which of the three it is. There is no branch here that can
    render a fork without saying.
    """
    lines: List[str] = []
    if isolation == "isolated":
        lines.append("WORKING TREE ISOLATED")
        lines.append(
            f"  this conversation now runs in its own working tree, separate "
            f"from {parent_tree or parent_session_id or 'the parent'}"
        )
        lines.append(
            "  you now have TWO COPIES of your working state and they will "
            "diverge - changes in one do not appear in the other"
        )
    elif isolation == "shared":
        lines.append("WORKING TREE NOT ISOLATED")
        lines.append(
            "  this fork shares the SAME writable working tree as its parent: "
            "no worktree mechanism was available in this build"
        )
        lines.append(
            "  you now have two conversations over one set of files - edits "
            "made through either are visible to both"
        )
        lines.append(
            "  if you need them independent, run them in separate checkouts"
        )
    elif isolation == "refused":
        # Leads with the SAME marker the other two verdicts use, so a reader
        # scanning for the working-tree state finds it whichever of the three
        # happened. The first draft led with "FORK REFUSED" and spelled
        # "working tree" in lower case, which meant the one verdict that
        # touched NO files was the one a scan could not pick up.
        lines.append("WORKING TREE NOT ISOLATED - the fork was REFUSED")
        lines.append(
            "  a fork means a new working tree, and one could not be created"
            + (f": {reason}" if reason else "")
        )
        lines.append(
            "  nothing was forked; this conversation is unchanged and your "
            "files were not touched"
        )
    else:
        lines.append("WORKING TREE STATE UNKNOWN")
        lines.append(
            "  this build could not determine whether a fork isolates the "
            "working tree, so it is reporting that rather than implying it does"
        )
    if parent_session_id or child_session_id:
        lines.append(
            f"  {child_session_id or '(no child)'} continues "
            f"{parent_session_id or '(no parent)'}"
        )
    lines.append(
        "  the parent conversation is untouched; "
        f"`/resume {parent_session_id}` returns there" if parent_session_id
        else "  the parent conversation is untouched"
    )
    return lines


def assert_the_disclosure_cannot_be_skipped(
    fork: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """The gate: a fork receipt cannot claim success without an isolation verdict.

    A ``ForkReceipt`` that omits ``isolation`` is a receipt a caller could
    render as a clean fork, which is the exact non-negotiable ("never let a
    fork proceed without disclosing the working-tree isolation"). So the check
    is on the receipt, not on the renderer - a renderer that forgot the block
    cannot fix it, and this can.
    """
    if not isinstance(fork, Mapping):
        return True, ""  # no fork happened; nothing to disclose
    isolation = str(fork.get("isolation") or "")
    if isolation not in ("isolated", "shared", "refused", "unknown"):
        return False, (
            "the fork receipt carries no isolation verdict "
            f"({isolation!r}); a fork that does not say whether the working "
            "tree was isolated is a fork nobody consented to"
        )
    if isolation != "refused" and not str(fork.get("child_session_id") or ""):
        return False, "the fork receipt reports success with no child session"
    if isolation == "refused" and str(fork.get("child_session_id") or ""):
        return False, (
            "the fork receipt reports a refusal and also names a child "
            "session; a refusal must not have forked anything"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Branch summaries. A claim, labelled as one.
# ---------------------------------------------------------------------------


def branch_summary_lines(
    summary: Any,
    *,
    branch_session_id: str = "",
    generated_by: str = "",
    width: int = 78,
) -> List[str]:
    """Render a GENERATED branch summary with the label in the sentence.

    Three lines are load-bearing and none is optional:

    * the label, first, before any of the content;
    * what it is a summary OF (the branch it replaces);
    * that the branch's own transcript is still on disk.

    An LLM-generated summary of a discarded branch is a CLAIM about what
    happened. A user who cannot tell it apart from a record has been handed a
    fabricated history, and the fabrication is indistinguishable from the truth
    precisely because it is usually right.
    """
    lines = [BRANCH_SUMMARY_LABEL]
    if branch_session_id:
        lines.append(f"  a summary OF branch {branch_session_id}")
    if generated_by:
        lines.append(f"  written by: {generated_by}")
    # These two sentences are the whole point of the label and they are kept
    # SHORT on purpose. The first draft read "it is a model's account of that
    # branch, not something the conversation recorded" - 82 characters, which
    # the 78-column bound clipped at exactly the word "not", so the load-bearing
    # claim was the part a real terminal never showed. A disclosure that
    # survives only in the source is a disclosure nobody receives.
    lines.append("  a model's account, NOT a transcript record")
    lines.append(
        f"  the branch's own turns are still on disk: `/lineage show "
        f"{branch_session_id or '<id>'}`"
    )
    lines.append("  summary:")
    text = str(summary if summary is not None else "").strip()
    if not text:
        lines.append("    (no summary was produced)")
    for row in text.splitlines() or [""]:
        lines.append("    " + _bounded(_sanitize(row), max(20, width - 4)))
    # W2.4 item 4: abandoning a branch has to say what happens to the FILES.
    # Wave 1 said "the branch is NOT deleted - every turn in it is still on
    # disk", which is true about the conversation and silent about the working
    # tree - so a reader could reasonably conclude that abandoning a branch
    # also set its file changes aside. It does not: the files are wherever the
    # last edit left them, and only a rewind moves them.
    lines.append("")
    lines.append("  YOUR FILES ARE UNTOUCHED by abandoning a branch:")
    lines.append(
        "    the working tree still holds whatever the branch last wrote to it. "
        "Abandoning moves a conversation pointer; it does not put files back."
    )
    lines.append(
        "    to put the files back too, rewind to a checkpoint: /tree rewind <id>"
    )
    return [_bounded(line, width) for line in lines]


# ---------------------------------------------------------------------------
# The unified tree dispatcher. /tree, /fork, /clone are ONE mechanism.
# ---------------------------------------------------------------------------


def _receipt(
    verb: str, ok: bool, lines: Iterable[str], payload: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "verb": verb,
        "ok": bool(ok),
        "lines": [str(line) for line in lines],
        "payload": dict(payload or {}),
    }


def _fail(verb: str, message: str) -> Dict[str, Any]:
    return _receipt(verb, False, [message], {})


def _sanitize(value: Any, width: int = 0) -> str:
    try:
        from cli import ui as _ui

        text = str(_ui.sanitize_text(str(value if value is not None else "")))
    except Exception:
        return "(detail withheld: sanitiser unavailable)"
    text = " ".join(text.split())
    if width and len(text) > max(4, int(width)):
        text = text[: max(1, int(width) - 3)] + "..."
    return text


def _plain(value: Any, width: int = 0) -> str:
    """Bound a PRODUCT-AUTHORED string. NOT a substitute for :func:`_sanitize`.

    For words this module wrote and for values that came out of an identifier
    grammar. A sanitiser's withholding marker must never land on the line a
    reader needs in order to navigate - so ids, reasons and counts go through
    here and untrusted CONTENT goes through :func:`_sanitize`.
    """
    text = " ".join(str(value if value is not None else "").split())
    if width and len(text) > max(4, int(width)):
        text = text[: max(1, int(width) - 3)] + "..."
    return text


def _bounded(line: str, width: int) -> str:
    text = str(line)
    limit = int(width or 0)
    if limit <= 3 or len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."


def _isolate(repo: Any) -> Dict[str, Any]:
    """Try to give a fork its own working tree, and report what actually happened.

    Delegates to ``runtime.worktrees`` when it is importable, because T2/T3 own
    the worktree mechanism and a second copy here would be a second answer to
    "how do two sessions stop sharing a tree".

    Every failure mode is reported, not raised, and none of them is reported as
    ``isolated``.
    """
    try:
        from runtime import worktrees as _wt
    except Exception as exc:
        return {
            "isolation": "shared",
            "reason": f"the worktree mechanism could not be loaded: {exc}",
            "tree": "",
        }
    try:
        from cli import session as _session

        resolved = _session.resolve_artifact_root(None, repo)
        root = (resolved or {}).get("log_root")
    except Exception as exc:
        return {
            "isolation": "shared",
            "reason": f"the artifact root could not be resolved: {exc}",
            "tree": "",
        }
    if not root:
        return {
            "isolation": "shared",
            "reason": "no artifact root is configured, so no worktree root "
            "could be resolved",
            "tree": "",
        }
    try:
        manager = _wt.WorktreeManager(repo, Path(str(root)) / "worktrees")
    except Exception as exc:
        return {
            "isolation": "shared",
            "reason": f"the worktree manager refused to build: {exc}",
            "tree": "",
        }
    try:
        record = manager.create(_fork_worktree_name())
    except Exception as exc:
        return {
            "isolation": "shared",
            "reason": f"creating a worktree failed: {exc}",
            "tree": "",
        }
    # `create()` can RETURN a record whose state is a failure rather than
    # raising. Reporting that record as `isolated` would be the worst version
    # of this function: a fork announcing a second working tree that does not
    # exist. So the state is checked, and a non-ready state is `shared`.
    state = str(getattr(record, "state", "") or "").strip().lower()
    if state and state not in ("ready", "active", "ok", "created"):
        return {
            "isolation": "shared",
            "reason": (
                f"a worktree record was created but its state is {state!r}"
                + (
                    f": {getattr(record, 'last_error', '')}"
                    if getattr(record, "last_error", "")
                    else ""
                )
            ),
            "tree": str(getattr(record, "path", "") or ""),
            "state": state,
        }
    return {
        "isolation": "isolated",
        "reason": "",
        "tree": str(getattr(record, "path", "") or ""),
        "state": state or "ready",
        "node_id": str(getattr(record, "node_id", "") or ""),
    }


_WORKTREE_SEQ = {"n": 0}


def _fork_worktree_name() -> str:
    """A deterministic-per-process worktree name. Bounded, and never a path."""
    _WORKTREE_SEQ["n"] += 1
    return f"fork-{int(time.time())}-{_WORKTREE_SEQ['n']}"


def _do_fork(
    verb: str,
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
) -> Dict[str, Any]:
    """The ONE fork implementation, behind both ``/fork`` and ``/clone``.

    ``/clone`` is the same operation with a different name because the corpus
    unifies them: one structure, one set of semantics, three names. The
    difference between them is what the receipt says, not what happens.
    """
    if not session_id:
        return _fail(verb, f"there is no conversation to {verb} yet")
    try:
        from cli import session as _session

        base = _session.load_or_create(log_root, repo, session_id, strict=False)
    except Exception as exc:
        return _fail(verb, f"this conversation could not be read: {exc}")
    if not base:
        return _fail(verb, f"no conversation is loaded for session {session_id!r}")

    isolated = _isolate(repo)
    try:
        from cli import session as _session

        fork = _session.fork_session(
            log_root, session_id, repo, at_turn_id=token or None
        )
    except Exception as exc:
        fork = {}
        error = str(exc)
    else:
        error = ""

    if error:
        # The fork did NOT happen. Isolation state is irrelevant here, and
        # reporting it would imply something changed.
        lines = [f"{verb} failed: {_sanitize(error)}"]
        lines.append("nothing was forked; this conversation and your files are unchanged")
        return _receipt(
            verb,
            False,
            lines,
            {
                "isolation": "refused",
                "reason": error,
                "child_session_id": "",
                "parent_session_id": session_id,
            },
        )

    child = str(fork.get("session_id") or "")
    lines = fork_isolation_lines(
        isolation=str(isolated.get("isolation") or "shared"),
        parent_session_id=session_id,
        child_session_id=child,
        parent_tree=str(repo) if repo else "",
        child_tree=str(isolated.get("tree") or ""),
    )
    receipt = {
        "isolation": isolated.get("isolation"),
        "reason": isolated.get("reason", ""),
        "parent_session_id": session_id,
        "child_session_id": child,
        "worktree": isolated.get("tree", ""),
        "turns_copied": len(fork.get("turns") or []),
        "verb": verb,
        "shared_implementation": True,
    }
    ok, why = assert_the_disclosure_cannot_be_skipped(receipt)
    if not ok:
        lines.append(f"DISCLOSURE CHECK FAILED: {why}")
    lines.append(
        f"  {len(fork.get('turns') or [])} turn(s) carried into {child}"
    )
    lines.append(f"  read the lineage with: /lineage show {child}")
    return _receipt(verb, ok, lines, {"fork": receipt, "conversation": fork})


def _do_rewind(
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
) -> Dict[str, Any]:
    """Rewind, and REPORT WHAT WAS RESTORED - files included.

    Delegates the work to ``memory.checkpoints``, which is T3's/T5's authority
    and which owns the conflict-safe restore. What this owns is the receipt: a
    rewind that moved a conversation pointer and silently left the files alone
    is the failure a user cannot detect, so ``files_restored``,
    ``files_not_restored`` and ``scope`` are all rendered, and "nothing was
    restored" is stated rather than presented as a clean run.
    """
    if not token:
        return _fail("rewind", "rewind needs a checkpoint or turn id: /tree rewind <id>")
    if not repo:
        # Without a repository the checkpoint authority cannot even resolve a
        # workspace identity, and letting it try produced a raw
        # `TypeError: expected str, bytes or os.PathLike object, not NoneType`
        # as a user-facing refusal - a Python error, not a sentence anybody
        # can act on. Nothing was restored either way.
        return _fail(
            "rewind",
            "rewind needs a repository: this session has none, so there is no "
            "working tree to put back (open one with /repo <path>)",
        )
    try:
        from memory import checkpoints as _cp
    except Exception as exc:
        return _fail("rewind", f"the checkpoint authority could not be loaded: {exc}")
    try:
        result = _cp.restore_checkpoint(
            repo,
            log_root,
            str(token),
            session_id=str(session_id or ""),
            restore_files=True,
            restore_conversation=True,
            strict=False,
        )
    except Exception as exc:
        return _fail("rewind", f"the rewind was refused: {exc}")
    payload = dict(result or {})

    status = str(payload.get("status") or "unknown")
    ok = bool(payload.get("ok"))
    # `restored_files` is the real key; `conflicts` carries {path, reason} for
    # every path that still holds NEWER content and was therefore left alone.
    restored = [str(item) for item in (payload.get("restored_files") or [])]
    conflicts = [dict(item) for item in (payload.get("conflicts") or []) if isinstance(item, Mapping)]

    lines = [
        "rewind  checkpoint {token} · {status}".format(
            token=str(payload.get("checkpoint_id") or token),
            status=(
                "restored" if status == "restored" else f"NOT restored ({status})"
            ),
        )
    ]
    if restored:
        lines.append(f"  FILES RESTORED ({len(restored)}) - your working tree now holds this state:")
        for path in restored[:20]:
            lines.append(f"    + {_sanitize(path)}")
        if len(restored) > 20:
            lines.append(f"    (+{len(restored) - 20} more)")
    else:
        lines.append(
            "  FILES RESTORED: none - no file was changed by this rewind"
        )
    if conflicts:
        lines.append(
            f"  {len(conflicts)} path(s) were NOT restored and still hold the "
            "NEWER content:"
        )
        for item in conflicts[:20]:
            lines.append(
                "    ! {path}  ({reason})".format(
                    path=_sanitize(item.get("path")),
                    reason=_sanitize(item.get("reason") or "conflicts with the checkpoint"),
                )
            )
        if len(conflicts) > 20:
            lines.append(f"    (+{len(conflicts) - 20} more)")
        lines.append(
            "  nothing was overwritten: a rewind will not discard work that "
            "happened after the checkpoint"
        )
    conversation = payload.get("conversation")
    conversation_status = (
        str((conversation or {}).get("status") or "") if isinstance(conversation, Mapping) else ""
    )
    if conversation_status and conversation_status != "restored":
        lines.append(
            f"  the conversation itself was NOT rolled back ({conversation_status}); "
            "the files and the transcript are restored independently"
        )
    lines.append(
        "  the newer state was rotated aside, not deleted - nothing was destroyed"
    )
    return _receipt(
        "rewind",
        ok,
        lines,
        {
            "rewind": payload,
            "status": status,
            "files_restored": restored,
            "files_not_restored": [
                str(item.get("path") or "") for item in conflicts
            ],
            "conversation_status": conversation_status,
        },
    )


def _confirm_flags(rest: str) -> Tuple[str, Dict[str, str]]:
    """Split a destructive verb's argument into its id and its flags.

    Delegates to :func:`memory_ui._resolve_flags` rather than re-deriving the
    split, because that one runs through ``shlex`` and therefore survives the
    quoted reason this surface teaches. Two flag parsers is how a refusal ends
    up refusing the very example its own help line prints.
    """
    try:
        from cli import memory_ui as _memory

        return _memory._resolve_flags(rest)
    except Exception:
        text = str(rest or "").strip().split()
        return (text[0] if text else ""), {"confirm": ""}


def _require_confirmation(
    verb: str, flags: Mapping[str, str], *, what: str, count: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Refuse a destructive verb without an explicit confirmation, or ``None``.

    The refusal STATES THE COUNT, because that is the fact the brief asks for
    and because a confirmation prompt without a number is a prompt about
    something the reader has to go and count themselves.
    """
    if "confirm" in flags or "yes" in flags:
        return None
    counted = f"{int(count)}" if isinstance(count, int) else "an unknown number of"
    return _fail(
        verb,
        f"/tree {verb} will remove {counted} {what}. Nothing about that is "
        f"undoable from this surface, so it needs the explicit token: "
        f"/tree {verb} <id> --confirm",
    )


def _do_merge(
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
    force: bool = False,
) -> Dict[str, Any]:
    """Merge a branch back into its parent, refusing a divergent one.

    W2.4 item 3. A merge that silently overwrites divergent work is worse than
    no merge at all, so the preflight is the whole of this function's honesty:

    * the branch must EXIST in T3's tree (a session with no tree has no branch
      to merge, and reporting one anyway would be inventing structure);
    * it must not BE the head (merging the head into its own parent is a
      rewind wearing a merge's name);
    * its parent must still exist;
    * **it must not have diverged** from the parent - and "diverged" is decided
      by the same question ``cli/review.py`` asks before it reverts a file,
      which is whether the branch's own recorded content is what the parent
      would be overwritten with. ``force=True`` converts the refusal into a
      RECORDED overwrite that still names what it is about to destroy, because
      a forced merge that reads like a clean one afterwards is a gate that
      cannot fail.

    The tree has no ``merge`` method of its own - ``runtime/AGENTS.md`` says so
    - so the merge is expressed with the primitives that DO exist: a turn entry
    carrying the branch's text, then a head move onto the parent. That is why
    the receipt reports which primitives it used rather than claiming an atomic
    tree operation.
    """
    if not token:
        return _fail("merge", "merge needs a branch id: /tree merge <branch-id>")
    tree, _module, _root = _tree_of_session(log_root, session_id)
    if tree is None or _tree_module() is None:
        return _fail(
            "merge",
            "the session tree could not be read, so nothing was merged; this "
            "is not the same as there being nothing to merge",
        )
    entry = None
    try:
        entry = tree.entry(str(token))
    except Exception:
        entry = None
    if entry is None:
        return _fail(
            "merge", f"no branch {token!r} in this conversation's tree"
        )
    if str(getattr(entry, "kind", "")) != "branch":
        return _fail(
            "merge",
            f"{token} is a {getattr(entry, 'kind', '?')!r} entry, not a branch; "
            "only a branch can be merged back",
        )
    head = None
    try:
        head = tree.head()
    except Exception:
        head = None
    if head is not None and str(getattr(head, "id", "")) == str(token):
        return _fail(
            "merge",
            f"{token} IS the head, so merging it into its parent is a rewind "
            "wearing a merge's name; use /tree rewind <checkpoint-id>",
        )
    parent_id = str(getattr(entry, "parent_id", "") or "")
    parent = None
    try:
        parent = tree.entry(parent_id)
    except Exception:
        parent = None
    if parent is None:
        return _fail(
            "merge",
            f"the parent of {token} is not in the tree, so there is nowhere to "
            "merge it back to; nothing was changed",
        )

    branch_text = str(getattr(entry, "text", "") or "")
    evidence = [str(item) for item in (getattr(entry, "evidence", ()) or ())]
    diverged = bool(evidence) and parent_id not in evidence

    lines = [
        "merge    branch {branch} -> parent {parent}".format(
            branch=_plain(token, 24), parent=_plain(parent_id, 24)
        ),
        "  the branch's own content follows the merge, so it is NOT lost",
    ]
    if diverged and not force:
        lines.append(
            "  REFUSED: this branch diverged from its parent, so merging it "
            "would overwrite work that happened on both sides"
        )
        lines.append(
            "    the branch cites {n} entr(ies) and its parent is not among "
            "them - that is what divergence means here".format(n=len(evidence))
        )
        lines.append("    nothing was written. If you mean it:")
        lines.append(f"      /tree merge {token} --confirm --force")
        lines.append(
            "    or read both sides first: /tree show, then decide which to keep"
        )
        return _receipt(
            "merge",
            False,
            lines,
            {
                "branch": str(token),
                "parent": parent_id,
                "diverged": True,
                "forced": False,
                "evidence": evidence,
            },
        )

    appended = None
    try:
        appended = tree.append(
            "turn",
            text=branch_text,
            payload={
                "merged_from": str(token),
                "forced": bool(diverged and force),
                "reason": "merge",
            },
            evidence=evidence or (),
        )
    except Exception as exc:
        return _fail("merge", f"the merge could not be recorded: {exc}")
    if appended is None:
        return _fail(
            "merge",
            "the merge entry could not be written to the tree journal; nothing "
            "was changed",
        )
    moved = None
    try:
        moved = tree.move_head(parent_id, reason="merge")
    except Exception as exc:
        return _fail("merge", f"the head could not be moved: {exc}")
    if moved is None:
        return _fail(
            "merge",
            "the merge entry was written but the head could not be moved onto "
            "the parent; the branch is still reachable and nothing was lost",
        )
    if diverged and force:
        lines.append(
            "  FORCED: this branch had diverged, and you overwrote the "
            "divergence anyway. The forced branch is still on disk."
        )
    lines.append(
        "  used the tree's primitives (a turn entry + a head move), because "
        "this build's tree has no atomic merge"
    )
    lines.append(f"  head is now: {_plain(parent_id, 24)}")
    lines.append("  the branch is still on disk: /tree show")
    return _receipt(
        "merge",
        True,
        lines,
        {
            "branch": str(token),
            "parent": parent_id,
            "diverged": diverged,
            "forced": bool(diverged and force),
            "entry_id": str(getattr(appended, "id", "") or ""),
            "history_retained": True,
        },
    )


def _do_delete(
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Delete a branch, behind a confirmation that states how much goes with it.

    W2.4 item 5. Deleting a branch deletes its REWIND POINTS: every entry on
    that branch is a place the conversation could be put back to, and removing
    them removes the ability to. So the receipt counts them BEFORE it acts and
    prints the count in the refusal, because "delete a branch" reading as a
    pointer move is the misunderstanding that makes this irreversible.

    T3's tree has no hard delete. ``gc`` ROTATES ``entries.jsonl`` aside to
    ``entries.jsonl.gc-<ns>`` and never removes it, so this verb reports what
    actually happened in those terms rather than claiming a deletion it did not
    perform.
    """
    if not token:
        return _fail("delete", "delete needs a branch id: /tree delete <branch-id>")
    tree, module, _root = _tree_of_session(log_root, session_id)
    if tree is None or module is None:
        return _fail("delete", "the session tree could not be read; nothing was deleted")
    entry = None
    try:
        entry = tree.entry(str(token))
    except Exception:
        entry = None
    if entry is None:
        return _fail("delete", f"no branch {token!r} in this conversation's tree")

    branch_id = str(getattr(entry, "id", "") or "")
    head = None
    try:
        head = tree.head()
    except Exception:
        head = None
    head_id = str(getattr(head, "id", "") or "")
    if branch_id == head_id:
        return _fail(
            "delete",
            f"{token} IS the head. Deleting it would leave the tree pointing at "
            "nothing; move the head first with /tree rewind <checkpoint-id>",
        )

    # Count what goes with it: the branch itself plus every entry beneath it.
    descendants: List[str] = []
    pending = [branch_id]
    seen = {branch_id}
    while pending:
        current = pending.pop()
        try:
            children = tree.children_of(current)
        except Exception:
            children = []
        for child in children or ():
            child_id = str(getattr(child, "id", "") or "")
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            descendants.append(child_id)
            pending.append(child_id)
    rewind_points = len(descendants)

    refusal = _require_confirmation("delete", {"confirm": ""} if confirm else {}, what="rewind point(s)", count=rewind_points)
    if refusal is not None:
        lines = list(refusal["lines"])
        lines.append(
            "  {n} rewind point(s) live on or below this branch: {ids}".format(
                n=rewind_points,
                ids=", ".join(descendants[:6]) + (" ..." if len(descendants) > 6 else ""),
            )
            if descendants
            else "  no rewind points live on or below this branch"
        )
        lines.append("  nothing was deleted. The branch is still on disk.")
        return _receipt(
            "delete",
            False,
            lines,
            {"branch": branch_id, "rewind_points": rewind_points, "deleted": False},
        )

    collected: List[str] = []
    reason = ""
    try:
        # T3's `gc(roots=...)` names the entries to RETAIN, not the ones to
        # collect - so passing the branch id would have retained exactly the
        # branch being deleted. The retained set is therefore T3's own default
        # roots MINUS this branch, which is also why the first version of this
        # function reported "nothing was collectable" for a branch that plainly
        # existed: it was asking to keep the thing it meant to remove.
        retained = sorted(set(tree.default_roots() or ()) - {branch_id})
        # `min_age_s=0` deliberately overrides T3's one-hour grace window, and
        # the receipt says so. The window exists so "I went back" cannot lose
        # work; an explicit `--confirm` has already served that purpose, and
        # silently collecting under the default window would report a delete
        # that quietly did nothing.
        outcome = tree.gc(roots=retained, dry_run=False, min_age_s=0.0)
    except Exception as exc:
        return _fail("delete", f"the delete could not be performed: {exc}")
    if isinstance(outcome, Mapping):
        collected = [str(item) for item in (outcome.get("collected") or ())]
        reason = str(outcome.get("reason") or "")
    if not collected:
        return _fail(
            "delete",
            "nothing was collectable on this branch, so nothing was removed. "
            "A surviving claim may cite one of its entries, and a cited entry is "
            "kept by design - which is reported here rather than dressed up as a "
            "deletion" + (f" (reason: {reason})" if reason else ""),
        )
    lines = [
        "deleted  branch {branch}".format(branch=_plain(branch_id, 24)),
        "  {n} entr(ies) collected: {ids}".format(
            n=len(collected), ids=", ".join(collected[:6])
        ),
        "  NOTHING WAS ERASED: this build rotates the journal aside rather "
        "than deleting it, so every collected entry is still on disk",
        "  the rotated original is beside the journal as entries.jsonl.gc-<ns>",
        "  the one-hour rewind grace window was overridden by your --confirm; "
        "entries cited by a surviving claim are still kept",
    ]
    return _receipt(
        "delete",
        True,
        lines,
        {
            "branch": branch_id,
            "deleted": True,
            "collected": collected,
            "rewind_points": rewind_points,
            "descendants": descendants,
            "rotated_not_erased": True,
            "grace_window_overridden": True,
        },
    )


def _do_prune(
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
    confirm: bool = False,
    min_age_s: float = 0.0,
) -> Dict[str, Any]:
    """Report what pruning would collect, and collect it only when confirmed.

    W2.5's prune-notice surface. The dry run is the DEFAULT and the receipt
    leads with the bound, because a prune that reports its own ceiling after
    the fact is a receipt about a decision the reader did not get to make.
    """
    tree, module, _root = _tree_of_session(log_root, session_id)
    if tree is None or module is None:
        return _fail("prune", "the session tree could not be read; nothing was pruned")
    try:
        preview = tree.gc(roots=None, dry_run=True, min_age_s=float(min_age_s or 0.0))
    except Exception as exc:
        return _fail("prune", f"the prune could not be measured: {exc}")
    preview = dict(preview or {})
    collectable = [str(item) for item in (preview.get("collected") or ())]

    if not confirm:
        lines = [
            "prune    DRY RUN - nothing was collected",
            "  {n} entr(ies) are collectable right now".format(n=len(collectable)),
            "  retained: {kept} · the bound is min_age_s={age:g}s".format(
                kept=int(preview.get("retained") or 0), age=float(min_age_s or 0.0)
            ),
        ]
        if collectable:
            lines.append("  would collect: " + ", ".join(collectable[:8]))
        else:
            lines.append(
                "  nothing is collectable: every branch is a retention root by "
                "default, so a prune only reclaims entries no branch can reach"
            )
        lines.append("  to actually collect: /tree prune --confirm")
        return _receipt(
            "prune",
            True,
            lines,
            {"dry_run": True, "collectable": collectable, "preview": preview},
        )

    try:
        outcome = tree.gc(roots=None, dry_run=False, min_age_s=float(min_age_s or 0.0))
    except Exception as exc:
        return _fail("prune", f"the prune could not be performed: {exc}")
    outcome = dict(outcome or {})
    removed = int(outcome.get("removed") or 0)
    lines = [
        "prune    {n} entr(ies) collected, {kept} retained".format(
            n=removed, kept=int(outcome.get("retained") or 0)
        ),
        "  {before:,} -> {after:,} bytes ({reclaimed:,} reclaimed)".format(
            before=int(outcome.get("bytes_before") or 0),
            after=int(outcome.get("bytes_after") or 0),
            reclaimed=int(outcome.get("bytes_reclaimed") or 0),
        ),
        "  NOTHING WAS ERASED: the original journal was rotated aside, not "
        "deleted, so every entry is still on disk",
    ]
    if outcome.get("evidence_protected"):
        lines.append(
            "  {n} entr(ies) are cited by a surviving claim and were kept: "
            "{ids}".format(
                n=len(outcome["evidence_protected"]),
                ids=", ".join(str(item) for item in outcome["evidence_protected"][:6]),
            )
        )
    return _receipt(
        "prune",
        True,
        lines,
        {"dry_run": False, "removed": removed, "outcome": outcome, "rotated_not_erased": True},
    )


def _do_abandon(
    token: str,
    *,
    repo: Any,
    log_root: Any,
    session_id: str,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Abandon a branch, and report what the user gets back.

    W2.4: this now acts on T3's TREE, because that is where branches live.
    Wave 1 read ``cli.session`` snapshots for the branch and its parent, so
    ``abandon`` reported "this has no parent to return to" for a branch the
    tree could see perfectly well - the two halves of the product disagreed
    about what a branch even was, and the half the user types into was the one
    that could not find it.

    Two outcomes, and the receipt distinguishes them because they are not the
    same: the head moves to the branch's PARENT (a restore, whose position is
    still there), or a generated summary is spliced in (a CLAIM, labelled as
    one by :func:`branch_summary_lines`).

    Nothing is ever deleted: abandoning moves a pointer. And the FILES are not
    touched either - :func:`branch_summary_lines` says so in its own words,
    because "the branch is not deleted" is true about a conversation and silent
    about a working tree.
    """
    if not token:
        return _fail("abandon", "abandon needs a session id: /tree abandon <session-id>")

    tree, _module, _root = _tree_of_session(log_root, session_id)
    entry = None
    if tree is not None:
        try:
            entry = tree.entry(str(token))
        except Exception:
            entry = None
    if entry is None:
        # No tree, or no such entry: fall back to the session lineage, which is
        # what Wave 1 did and is still the right answer for a ROTATED
        # conversation - a rotation creates a child SESSION, and abandoning one
        # of those returns the head to its parent session.
        target = _load(log_root, token)
        if not target:
            return _fail(
                "abandon",
                f"no branch or session {token!r} in this conversation. If it "
                "belongs to another conversation, name that one: /tree abandon "
                "<session-id> is scoped to the conversation you are in",
            )
        parent = _parent_of(target)
        if not parent:
            return _fail(
                "abandon",
                f"{token} has no parent to return to - abandoning it would "
                "discard the root of a lineage, which this surface will not do",
            )
        lines = [
            f"abandoned  branch {token} · the lineage head returns to {parent}",
            "  the branch is NOT deleted - every turn in it is still on disk",
            "  YOUR FILES ARE UNTOUCHED - abandoning moves a conversation "
            "pointer, it does not put files back",
            f"  read it later with: /lineage show {token}",
        ]
        if summary:
            lines.append("")
            lines.extend(
                branch_summary_lines(
                    summary, branch_session_id=token, generated_by="the caller"
                )
            )
        return _receipt(
            "abandon",
            True,
            lines,
            {"abandoned": token, "head": parent, "summary_generated": bool(summary)},
        )

    branch_id = str(getattr(entry, "id", "") or "")
    if str(getattr(entry, "kind", "")) != "branch":
        return _fail(
            "abandon",
            f"{token} is a {getattr(entry, 'kind', '?')!r} entry, not a branch",
        )
    parent_id = str(getattr(entry, "parent_id", "") or "")
    if not parent_id:
        return _fail(
            "abandon",
            f"{branch_id} has no parent entry to return to - it is the root of "
            "this tree, and abandoning a root would leave the head pointing at "
            "nothing",
        )
    parent = None
    try:
        parent = tree.entry(parent_id)
    except Exception:
        parent = None
    if parent is None:
        return _fail(
            "abandon",
            f"the parent of {branch_id} is not in the tree, so there is nowhere "
            "to return to; nothing was changed",
        )

    written = None
    try:
        written = tree.abandon(
            label=str(getattr(entry, "text", "") or ""),
            summary=str(summary or ""),
            evidence=tuple(str(item) for item in (getattr(entry, "evidence", ()) or ())),
        )
    except Exception as exc:
        return _fail("abandon", f"the branch could not be abandoned: {exc}")
    if written is None:
        return _fail(
            "abandon",
            "the tree refused to abandon this branch; nothing was changed and "
            "the branch is still reachable",
        )

    moved = None
    try:
        moved = tree.move_head(parent_id, reason="abandon")
    except Exception:
        moved = None

    lines = [
        f"abandoned  branch {branch_id} · the head returns to {_plain(parent_id, 24)}",
        "  the branch is NOT deleted - every entry on it is still on disk",
        "  YOUR FILES ARE UNTOUCHED - abandoning moves a conversation pointer, "
        "it does not put files back",
        "    to put the files back too: /tree rewind <checkpoint-id>",
    ]
    if moved is None:
        lines.append(
            "  the summary was written but the head could NOT be moved, so the "
            "branch is still on the head's path"
        )
    if written.get("summary_written"):
        # The summary TEXT lives on the new claim entry, so it is read back
        # from the tree rather than echoed from the argument - a receipt that
        # renders what the caller passed instead of what was recorded is a
        # receipt about an intention.
        claim = None
        try:
            claim = tree.entry(str(written.get("summary_entry") or ""))
        except Exception:
            claim = None
        lines.append("")
        lines.extend(
            branch_summary_lines(
                str(getattr(claim, "text", "") or "") if claim is not None else "",
                branch_session_id=branch_id,
                generated_by="the model that abandoned the branch",
            )
        )
        if claim is not None and not str(getattr(claim, "text", "") or "").strip():
            lines.append(
                "  (the branch was abandoned with NO summary text: the claim "
                "entry cites the branch but asserts nothing about it)"
            )
    else:
        lines.append(
            "  no branch summary was generated - abandoning restores to its "
            "parent without one unless a summary is asked for"
        )
    lines.append("  read it later with: /tree show")
    return _receipt(
        "abandon",
        True,
        lines,
        {
            "abandoned": branch_id,
            "head": parent_id,
            "head_moved": moved is not None,
            "summary_generated": bool(written.get("summary_written")),
            "summary_entry": str(written.get("summary_entry") or ""),
            "files_touched": False,
            "history_retained": True,
        },
    )


def tree_command(
    verb: str,
    rest: str = "",
    *,
    repo_path: Any = None,
    log_root: Any = None,
    session_id: str = "",
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch one tree verb. THE implementation for ``/tree``, ``/fork``, ``/clone``.

    ``verb`` outside :data:`TREE_VERBS` returns a refusal naming the valid set.
    A read-only verb (``show``/``lineage``) cannot write, and the two writers
    both delegate to :func:`fork_isolation_lines` before reporting success.
    """
    name = str(verb or "").strip().lower()
    if name not in TREE_VERBS:
        return _fail(
            name or "(none)", "unknown tree verb; use one of: " + ", ".join(TREE_VERBS)
        )
    token = str(rest or "").strip().split()[0] if str(rest or "").strip() else ""

    if name in ("show", "lineage"):
        rows = lineage_rows(log_root, session_id)
        lines = lineage_lines(rows)
        report = rotation_events(log_root, session_id)
        if report.get("events"):
            lines.append("")
            lines.append("rotations and compactions:")
            for event in report["events"]:
                if event["kind"] == "rotation":
                    lines.append(
                        "  rotation    {parent} -> {child} · reason: {reason}".format(
                            parent=event["parent_session_id"],
                            child=event["child_session_id"],
                            reason=event["reason"],
                        )
                    )
                else:
                    # W2.2: the compaction event names the summary it produced
                    # and marks it GENERATED. A compaction listed as "12 turns
                    # summarised away" reads as a description of the
                    # conversation; what actually happened is that a model wrote
                    # a paragraph about those turns, and that paragraph is now
                    # in the conversation.
                    lines.append(
                        "  compaction  {n} turn(s) summarised away · reason: "
                        "compression (this session continued)".format(
                            n=event.get("turn_count") or "?"
                        )
                    )
                    if event.get("summary"):
                        lines.append("    " + COMPACTION_SUMMARY_LABEL)
                        lines.append(
                            "    a model's account of those turns; the turns "
                            "themselves are still on disk: /lineage before"
                        )
        elif report.get("reason"):
            lines.append("")
            lines.append("  " + report["reason"])
        # W2.4 item 2: the tree view, with the reason and the head. Appended
        # rather than replacing the lineage rows, because they answer different
        # questions - the lineage rows are about SESSIONS and the tree rows are
        # about ENTRIES - and a reader asking "what is this conversation" wants
        # both.
        snapshot = tree_snapshot(log_root, session_id)
        lines.append("")
        lines.extend(tree_lines(snapshot))
        return _receipt(
            name,
            True,
            lines,
            {"lineage": rows, "rotation": report, "tree": snapshot},
        )

    if name in ("fork", "clone"):
        return _do_fork(name, token, repo=repo_path, log_root=log_root, session_id=session_id)

    if name == "rewind":
        return _do_rewind(
            token, repo=repo_path, log_root=log_root, session_id=session_id
        )

    if name == "merge":
        _token, flags = _confirm_flags(rest)
        return _do_merge(
            _token or token,
            repo=repo_path,
            log_root=log_root,
            session_id=session_id,
            force="force" in flags or "confirm_force" in flags,
        )

    if name == "delete":
        _token, flags = _confirm_flags(rest)
        return _do_delete(
            _token or token,
            repo=repo_path,
            log_root=log_root,
            session_id=session_id,
            confirm=("confirm" in flags or "yes" in flags),
        )

    if name == "prune":
        _token, flags = _confirm_flags(rest)
        age = 0.0
        for key in ("min_age_s", "min-age-s"):
            if key in flags:
                try:
                    age = float(flags[key])
                except (TypeError, ValueError):
                    age = 0.0
        return _do_prune(
            _token or token,
            repo=repo_path,
            log_root=log_root,
            session_id=session_id,
            confirm=("confirm" in flags or "yes" in flags),
            min_age_s=age,
        )

    return _do_abandon(
        token,
        repo=repo_path,
        log_root=log_root,
        session_id=session_id,
        summary=summary,
    )
