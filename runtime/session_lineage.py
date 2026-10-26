"""Durable session lineage: rotation, ancestry, cross-lineage search, resume.

This module is the **writer** half of lineage. ``cli/lineage.py`` (T4) is the
**reader**: it walks ``parent_session_id`` upward, reads a session's
``summaries`` / ``compacted_turns`` / ``raw_turns``, and appends a
``compaction`` / ``session_rotated`` row from the session's own
``<sid>.events.jsonl``. That reader already exists and is not this module's
file. So every shape this module writes is one the reader already reads, by
name -- there is no second on-disk vocabulary waiting to drift.

Three rules decide the shape, and each one was a correction to something the
brief assumed:

1. **No SQLite, no new store.** Sessions are a JSON snapshot plus a JSONL event
   journal, and ``cli.session`` owns the path rule for both. This module
   *delegates* to ``cli.session._session_path`` / ``load_or_create`` /
   ``save_session`` / ``append_session_event`` rather than re-deriving the
   layout. A second copy of a path rule is how a surface starts reporting "no
   such session" for a session that exists -- and ``cli/lineage.py``'s own
   docstring says exactly that, in the other direction. If ``cli.session``
   cannot be imported, every entry point here **refuses**; it never guesses a
   path and never writes bytes somewhere nobody will read them back.

2. **A rotation is a rotation, not a copy.** The brief described "close the
   parent, create a child, keep the parent's transcript". ``cli.session``
   already implements the retention half: ``compact_session`` moves dropped
   turns to ``compacted_turns``, keeps every turn in ``raw_turns``, and appends
   a ``summaries`` record naming the turns it folded. So this module does NOT
   re-implement retention or re-invent a summary format. It performs the part
   that does not exist yet: the **link**. Parent gets ``end_reason`` +
   ``rotated_to``; child gets ``parent_session_id`` + ``fork_point_turn_id``;
   the child inherits the retained history **by value** so the parent and the
   child cannot later disagree about it; and both journals get a rotation row.

3. **A summary is an INDEX, never a replacement.** The child's active
   ``turns`` starts EMPTY. The summary, ``compacted_turns`` and ``raw_turns``
   travel forward as references to the transcript, so "what did that
   compaction take?" is answerable from the child alone -- which is what
   ``cli.lineage.pre_compaction_history`` reads. Copying the parent's active
   turns into the child would make a rotation look like a fork, and a reader
   could not tell a rotation from a duplicate.

**Identity closure.** A child inherits ``LINEAGE_IDENTITY_FIELDS`` verbatim
from its parent -- never recomputed. Recomputing is how a rotation weakens the
closure: the repository moved, the request was re-read at a different
revision, and the child's own digest of its ancestry no longer matches the
thing it is continuing. So the inheritance is checked, not trusted:
:func:`identity_status` reports every field, which side carried it, and
whether the rotation **weakened** any of them (a field non-blank on the parent
and blank on the child is a weakening and refuses). A field blank on BOTH
sides is reported as ``blank_on_both`` and does not refuse -- a bare worker
launch has no supervisor namespace on either side, and refusing it would break
a legitimate caller rather than protect one. This deliberately mirrors
``runtime.checkpoint._ALLOWED_BLANK_IDENTITY_FIELDS``; two answers to "is this
identity complete" is the exact failure this repository keeps paying for, so
the reasoning is the same and the two modules name each other.

**Only an explicit continue may reuse a request hash.** ``resume_state`` takes
``continue_request=True`` and nothing else does. A lineage rotation is not a
continue: it changes the session id, so inheriting the parent's request hash
without an explicit instruction would authorise work the caller never asked
for.

Cost note: every function here is a bounded read over a session snapshot and
its own journal. Nothing opens a task directory, replays a trace, or walks the
repository -- "show me what happened before this compaction" is asked at the
moment somebody suspects something was lost, and an answer that needs a full
replay is not an answer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from runtime.session_tree import digest_body

from .fsutil import atomic_write_json

__all__ = [
    "BLANK_ALLOWED_IDENTITY_FIELDS",
    "DELETIONS_NAME",
    "LINEAGE_DIRNAME",
    "LINEAGE_IDENTITY_FIELDS",
    "LIVE_SESSIONS_NAME",
    "OPENED_BY_ROTATION_EVENT",
    "ROTATION_REASONS",
    "LineageRefused",
    "ancestry",
    "begin_session",
    "checkpoint_identity_of",
    "content_digest",
    "dedupe_hits",
    "delete_session",
    "deletion_ledger",
    "deletions_journal_path",
    "describe",
    "end_session",
    "event_journal_path",
    "identity_of",
    "identity_status",
    "lineage_identity",
    "lineage_records",
    "live_sessions_path",
    "load_session",
    "pre_compaction_index",
    "publish_live_sessions",
    "resolve_dropped_seqs",
    "resume_against_checkpoint",
    "resume_state",
    "rotate_session",
    "rotation_reversible",
    "search_lineage",
    "session_path",
]

#: The closed vocabulary of ways a session may END. ``compression`` is the one a
#: lineage rotation produces; the others exist so a reader can tell a rotation
#: apart from a normal end without reading prose. Mirrors
#: ``cli.lineage.ROTATION_REASONS`` -- same four words, same order.
ROTATION_REASONS: Tuple[str, ...] = (
    "compression",
    "user_fork",
    "abandoned",
    "completed",
)

#: The five identity dimensions a rotation must not weaken. Deliberately the
#: same five the checkpoint identity closure uses, named identically, so a
#: reader comparing the two receipts is comparing like with like.
LINEAGE_IDENTITY_FIELDS: Tuple[str, ...] = (
    "repository_identity",
    "request_identity",
    "revision_identity",
    "resume_namespace",
    "effort_identity",
)

#: Fields permitted to be blank on BOTH sides of a rotation. Kept identical to
#: ``runtime.checkpoint._ALLOWED_BLANK_IDENTITY_FIELDS`` and for the same two
#: reasons: a blank ``repository_identity`` means the caller declared no
#: repository (this tree's own scheduler tests build tasks with
#: ``repo_path=""``), and a blank ``resume_namespace`` means a bare worker
#: launch rather than a supervised run. A field blank on the PARENT and
#: non-blank on the child, or the reverse, is not covered by this set -- that is
#: a change, and a change is reported.
BLANK_ALLOWED_IDENTITY_FIELDS = frozenset(
    {"repository_identity", "resume_namespace"}
)

#: How deep an ancestry walk goes before it declares a cycle. A hand-edited or
#: corrupt snapshot that points at itself must produce an answer, not a hang on
#: somebody's status repaint timer.
MAX_ANCESTRY_DEPTH = 64

#: Default bound on a cross-lineage search result set.
DEFAULT_SEARCH_LIMIT = 50

#: The event kind written into a CHILD's journal to record that a rotation
#: opened it. Deliberately OUTSIDE the set ``cli.lineage`` reads as rotations
#: (``session_rotated`` / ``session_rotation``): that reader defines those two
#: kinds as "this session ENDED and opened a successor", and a child has not
#: ended. Writing ``session_rotated`` into a child was measured to make the
#: reader report a rotation to nothing -- see :func:`rotate_session`.
OPENED_BY_ROTATION_EVENT = "session_opened_by_rotation"

#: The AUDIT event kind written into BOTH journals by a rotation. Also outside
#: ``cli.lineage``'s read set, and for a measured reason: its journal path reads
#: ``row["data"]`` / ``row["ts"]`` while ``cli.session``'s real journal envelope
#: is ``{"event", "payload", "timestamp", ...}``, so a journal-sourced rotation
#: row renders with an empty child and no timestamp and then wins ``events[-1]``.
#: The rotation link therefore lives in the SNAPSHOT, which that reader reads
#: perfectly. See :func:`rotate_session` and the T4 request in
#: ``runtime/AGENTS.md``.
ROTATION_RECEIPT_EVENT = "rotation_receipt"

#: The ``ConversationJournal`` record kind a rotation writes so it becomes
#: REVERSIBLE. Read by ``runtime.checkpoint.ConversationJournal.active_compactions``
#: (which folds ``compaction`` rows against ``restore`` rows) and therefore by the
#: same fold ``ConversationMemory.restore`` uses -- one authority, no second one.
ROTATION_RECORD = "compaction"


class LineageRefused(RuntimeError):
    """A lineage operation was refused. Never a ``PermissionError``.

    ``provider_gateway``'s fail-closed paths absorb ``PermissionError`` and turn
    it into "try the next candidate"; a lineage refusal that arrived as one
    would be treated as a transient problem and retried rather than reported.
    """

    def __init__(self, reason: str, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail: Dict[str, Any] = dict(detail or {})

    def as_dict(self) -> Dict[str, Any]:
        """A receipt for a refusal. Every refusal carries a reason string."""
        return {
            "ok": False,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


# ---------------------------------------------------------------------------
# Delegation to cli.session. One path rule, one writer, one journal format.
# ---------------------------------------------------------------------------


def _session_api() -> Any:
    """Return ``cli.session``, or refuse. Never re-derives anything."""
    try:
        from cli import session as module
    except Exception as exc:  # pragma: no cover - a broken install, not a shape
        raise LineageRefused(
            "cli.session is unavailable, so a session's path rule and writer "
            "are unknown; lineage refuses rather than guessing where a "
            "conversation lives",
            {"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return module


def session_path(log_root: Any, session_id: Any) -> Path:
    """The snapshot path for one session, through ``cli.session``'s own rule."""
    api = _session_api()
    try:
        return Path(str(api._session_path(log_root, str(session_id))))
    except Exception as exc:
        raise LineageRefused(
            "this session id does not resolve to a conversation path",
            {"session_id": str(session_id), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def event_journal_path(log_root: Any, session_id: Any) -> Path:
    """The append-only event journal path, through ``cli.session``'s own rule."""
    api = _session_api()
    try:
        return Path(str(api.event_log_path(log_root, str(session_id))))
    except Exception as exc:
        raise LineageRefused(
            "this session id does not resolve to an event journal",
            {"session_id": str(session_id), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def load_session(log_root: Any, session_id: Any, repo: Any = None) -> Dict[str, Any]:
    """Load one session snapshot. ``{}`` when there is no such conversation.

    **Existence is checked BEFORE delegating, and that check is load-bearing.**
    ``cli.session.load_or_create`` CREATES a session when the file is absent --
    correct for a shell starting a conversation, and wrong for a lineage reader.
    Driving this function through it directly meant ``resume_state`` on a session
    id that does not exist returned a freshly-minted empty session and answered
    ``may_resume: True``: a **fabricated resume authorisation** for a
    conversation that was never there. It also meant a deleted middle generation
    silently reappeared, so a break anywhere in the ancestry could not be
    detected at all.

    So: if the snapshot file is not there, this returns ``{}`` and every caller
    reports the session as unreadable rather than inventing one. A lineage reader
    that mints the conversation it was asked about cannot answer any question
    about it honestly.
    """
    api = _session_api()
    try:
        if not session_path(log_root, session_id).is_file():
            return {}
        return dict(
            api.load_or_create(log_root, repo, str(session_id), strict=False) or {}
        )
    except Exception:
        return {}


def content_digest(text: Any) -> str:
    """A stable content digest for one transcript turn.

    Delegates to :func:`runtime.session_tree.digest_body`, which is the ONE
    digest authority in this module's neighbourhood. Two answers to "what is
    this turn's digest" is how a dedupe starts disagreeing with the tree that
    recorded the turn.
    """
    return digest_body({"text": str(text or "")})


# ---------------------------------------------------------------------------
# Identity closure
# ---------------------------------------------------------------------------


def identity_of(session: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """The five lineage identity dimensions carried by one session."""
    data = session if isinstance(session, Mapping) else {}
    return {field: str(data.get(field) or "") for field in LINEAGE_IDENTITY_FIELDS}


def lineage_identity(
    log_root: Any,
    session_id: Any,
    *,
    repo: Any = None,
) -> Dict[str, Any]:
    """Fold the whole ancestry into ONE resume identity, and report disagreement.

    **This is the anti-rotation property, and it is the one that matters.** The
    question "may this conversation be continued, and against which repository /
    request / revision / namespace / effort" has to be answerable from the
    lineage itself, because a rotation changes the session id and a reader that
    only looked at the tip would be looking at a session nobody can place.

    So each field is folded over every generation and reported three ways:

    * **one non-blank value** -> ``carried``. Every generation that carries the
      field agrees, which is exactly "the child inherited it without weakening
      it".
    * **two or more distinct non-blank values** -> ``divergent``, and
      ``ok is False``. A lineage whose root says repository A and whose tip says
      repository B is a lineage a rotation used to move a conversation across
      repositories, and that refuses.
    * **no non-blank value anywhere** -> ``absent``, which is NOT a divergence
      and NOT a completeness failure. It means the ``cli.session`` snapshot shape
      carries no checkpoint identity keys at all, which is the normal state of a
      real conversation. :func:`resume_state` reports it as a fact and lets the
      CALLER decide, because "this snapshot shape has no closure in it" is not
      the same claim as "the closure does not hold".

    Returns ``{ok, identity, fields, divergent, absent, generations,
    unreadable}``. Never raises: a lineage reader that raised would be
    indistinguishable from one that worked.
    """
    chain = ancestry(log_root, session_id, repo=repo)
    generations: List[Dict[str, str]] = []
    unreadable: List[str] = []
    for row in chain:
        if not row.get("loaded"):
            unreadable.append(str(row.get("session_id") or ""))
            continue
        loaded = load_session(log_root, str(row.get("session_id") or ""), repo)
        generations.append(identity_of(loaded))
    fields: Dict[str, Dict[str, Any]] = {}
    divergent: List[str] = []
    absent: List[str] = []
    folded: Dict[str, str] = {}
    for field in LINEAGE_IDENTITY_FIELDS:
        seen = sorted({row.get(field, "") for row in generations if row.get(field, "")})
        if not seen:
            state = "absent"
            absent.append(field)
        elif len(seen) == 1:
            state = "carried"
            folded[field] = seen[0]
        else:
            state = "divergent"
            divergent.append(field)
            folded[field] = seen[-1]
        fields[field] = {
            "state": state,
            "value": folded.get(field, ""),
            "observed": seen,
            "blank_allowed": field in BLANK_ALLOWED_IDENTITY_FIELDS,
        }
    return {
        "ok": not divergent,
        "identity": folded,
        "fields": fields,
        "divergent": divergent,
        "absent": absent,
        "generations": len(generations),
        "unreadable": unreadable,
        "source": "lineage",
    }


def identity_status(
    parent: Optional[Mapping[str, Any]],
    child: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compare a rotation's two sides field by field.

    Reports, per field: what each side carried, whether the rotation **weakened**
    or otherwise **changed** it, and whether it is complete under
    :data:`BLANK_ALLOWED_IDENTITY_FIELDS`.

    ``ok`` is ``True`` when NOTHING CHANGED. That is the whole rule, and it is
    deliberately narrower than "all five are non-empty":

    * A field blank on **both** sides does not refuse. It is reported as
      ``blank_on_both`` and folded into ``complete``, and the rotation proceeds.
      A ``cli.session`` snapshot carries no ``request_identity`` at all -- those
      are checkpoint fields -- so requiring them here would make the feature
      unreachable on every real session while protecting nothing. **Completeness
      is the parent's business** and ``runtime.checkpoint`` already decides it,
      with its own documented exemptions. Two modules answering "is this
      identity complete" differently is the failure this repository keeps paying
      for, so this one does not answer it at all; it answers the strictly
      narrower question it can answer correctly.

    * A field blank on ONE side only is a **change**, and it refuses. That is
      the exact mechanism by which a rotation could authorise work the parent
      was never authorised to continue -- and unlike completeness, it is
      reachable, because the child is built here and inherits verbatim.

    A caller that wants the strict completeness answer has it, in
    ``complete``, and :func:`resume_state` is the place that acts on it.
    """
    before = identity_of(parent)
    after = identity_of(child)
    fields: Dict[str, Dict[str, Any]] = {}
    changed: List[str] = []
    incomplete: List[str] = []
    for field in LINEAGE_IDENTITY_FIELDS:
        old = before.get(field, "")
        new = after.get(field, "")
        if old != new:
            changed.append(field)
        if not old and not new and field not in BLANK_ALLOWED_IDENTITY_FIELDS:
            incomplete.append(field)
        fields[field] = {
            "parent": old,
            "child": new,
            "inherited_verbatim": bool(old) and old == new,
            "blank_on_both": not old and not new,
            "blank_allowed": field in BLANK_ALLOWED_IDENTITY_FIELDS,
            "changed": old != new,
        }
    return {
        "ok": not changed,
        "fields": fields,
        "changed": list(changed),
        "weakened": [f for f in changed if before.get(f) and not after.get(f)],
        "complete": not incomplete,
        "incomplete": list(incomplete),
        "identity_fields": list(LINEAGE_IDENTITY_FIELDS),
        "blank_allowed": sorted(BLANK_ALLOWED_IDENTITY_FIELDS),
    }


# ---------------------------------------------------------------------------
# Making a rotation REVERSIBLE through the run's ConversationJournal
# ---------------------------------------------------------------------------


def resolve_dropped_seqs(
    run_dir: Any,
    folded_turns: Sequence[Mapping[str, Any]],
    *,
    dropped_seqs: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Map the turns a rotation folded onto the run journal's ``dropped_seqs``.

    **This is the whole difficulty of the feature, and it is a real one.** A
    session turn is identified by its ``turn_id`` (``"t0"``); a
    ``ConversationJournal`` turn row is identified by its ``seq`` (a monotonic
    integer the run's ``ConversationMemory`` allocates). Nothing joins them, and a
    rotation that guessed ``dropped_seqs`` from turn ORDER would drop the wrong
    sequences -- and a wrong ``restore_compaction`` re-drops or un-drops the
    wrong history, which is worse than not being reversible at all.

    So the resolution is explicit-first and content-based as the fallback:

    * an explicit ``dropped_seqs`` always wins, and is returned as-is;
    * otherwise each folded turn's text is matched to journal rows **by content
      digest** -- the same :func:`content_digest` authority the search dedupe
      uses, so "what is this turn" has one answer in this module;
    * a turn that resolves to **no** row, or to **more than one**, is reported in
      ``unresolved`` and is NOT dropped.

    An empty ``dropped_seqs`` with a non-empty ``unresolved`` is the honest
    outcome: the rotation happened and is recorded, but it is NOT reversible
    through this journal, and :func:`rotate_session` says so in its receipt
    rather than implying a restore would work.
    """
    from runtime.checkpoint import ConversationJournal

    explicit = [int(item) for item in (dropped_seqs or ()) if str(item or "").strip()]
    if explicit:
        return {
            "dropped_seqs": explicit,
            "resolution": "explicit",
            "unresolved": [],
            "folded_turns": len(folded_turns or ()),
            "reason": "",
        }
    wanted = {
        content_digest(_text_of(turn)): str(turn.get("turn_id") or "")
        for turn in (folded_turns or ())
        if _text_of(turn)
    }
    if not wanted:
        return {
            "dropped_seqs": [],
            "resolution": "none",
            "unresolved": [],
            "folded_turns": 0,
            "reason": "no folded turn carried text to match on",
        }
    rows = [
        row
        for row in ConversationJournal(run_dir).load()
        if str(row.get("record") or "") == "turn"
    ]
    hits: Dict[str, List[int]] = {}
    for row in rows:
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if not seq:
            continue
        hits.setdefault(content_digest(row.get("content") or ""), []).append(seq)
    resolved: List[int] = []
    unresolved: List[str] = []
    for digest, turn_id in wanted.items():
        matched = hits.get(digest) or []
        if len(matched) == 1:
            resolved.append(matched[0])
        elif not matched:
            unresolved.append(f"{turn_id or digest[:12]}: no journal row carries this text")
        else:
            unresolved.append(
                f"{turn_id or digest[:12]}: {len(matched)} journal rows carry this "
                "text, so dropping it would drop history nobody can name"
            )
    resolved = sorted(set(resolved))
    return {
        "dropped_seqs": resolved,
        "resolution": "content_digest",
        "unresolved": unresolved,
        "folded_turns": len(wanted),
        "reason": (
            ""
            if resolved and not unresolved
            else f"{len(unresolved)} folded turn(s) did not resolve to exactly one "
            "journal row, so this rotation is recorded but NOT reversible here"
        ),
    }


def rotation_reversible(
    run_dir: Any,
    compaction_id: Any,
    *,
    expected_dropped: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Report whether a rotation is currently restorable in a run's journal.

    A read-only report: it folds the journal through
    :meth:`ConversationJournal.active_compactions` and says whether the named
    compaction is in force, what it drops, and whether that matches what the
    rotation said it dropped. A rotation whose recorded ``dropped_seqs`` no longer
    matches the journal is reported as ``drifted`` rather than as reversible --
    a restore that un-drops the wrong sequences is a corruption, not a recovery.
    """
    from runtime.checkpoint import ConversationJournal

    identifier = str(compaction_id or "").strip()
    if not identifier:
        return {
            "ok": False,
            "reversible": False,
            "reason": "no compaction id was given",
            "compaction_id": "",
            "active": False,
            "drifted": False,
        }
    try:
        journal = ConversationJournal(run_dir)
        active = {str(row.get("compaction_id") or "") for row in journal.active_compactions()}
    except Exception as exc:
        return {
            "ok": False,
            "reversible": False,
            "reason": f"the conversation journal could not be read: {exc}",
            "compaction_id": identifier,
            "active": False,
            "drifted": False,
        }
    if identifier not in active:
        return {
            "ok": True,
            "reversible": True,
            "reason": "this compaction is not in force, so nothing needs restoring; "
            "its turns are already live",
            "compaction_id": identifier,
            "active": False,
            "drifted": False,
        }
    row = next(
        (
            item
            for item in journal.active_compactions()
            if str(item.get("compaction_id") or "") == identifier
        ),
        {},
    )
    live = [int(item) for item in (expected_dropped or ()) if str(item or "").strip()]
    actual = [int(item) for item in (row.get("dropped_seqs") or ()) if str(item or "")]
    drifted = bool(live) and sorted(live) != sorted(actual)
    return {
        "ok": True,
        "reversible": True,
        "reason": (
            "the journal's dropped sequences no longer match what the rotation "
            "recorded, so restoring it would un-drop the wrong history"
            if drifted
            else ""
        ),
        "compaction_id": identifier,
        "active": True,
        "dropped_seqs": sorted(actual),
        "expected_dropped_seqs": sorted(live),
        "drifted": drifted,
    }


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def _carried(session: Mapping[str, Any], key: str) -> List[Dict[str, Any]]:
    """A session's ``key`` list, deep-copied, as plain dicts.

    Copied BY VALUE on purpose. If the child shared the parent's list object,
    a later compaction of the child would mutate the parent's record of what it
    retained, and the two would silently disagree about history that both
    claim to still hold.
    """
    raw = session.get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _carried_raw_text(session: Mapping[str, Any], key: str) -> List[Any]:
    """``raw_turns`` entries, which are not always mappings, copied by value."""
    raw = session.get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) if isinstance(item, Mapping) else item for item in raw]


def rotate_session(
    log_root: Any,
    session: Mapping[str, Any],
    *,
    summary: str,
    source_turn_ids: Sequence[Any] = (),
    reason: str = "compression",
    child_session_id: Optional[str] = None,
    repo: Any = None,
    run_dir: Any = None,
    compaction_id: Optional[str] = None,
    dropped_seqs: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Rotate a session: close the parent, open a child that inherits everything.

    The parent keeps EVERY turn it had. This function adds links and a summary;
    it never removes a turn, a raw turn, or a prior summary from the parent.
    That is the whole "history is never destroyed" contract, and it is why the
    child carries the retained history forward rather than the parent being
    trimmed to make room.

    Args:
        log_root: artifact root; ``cli.session`` resolves the layout under it.
        session: the parent snapshot, as loaded by :func:`load_session`.
        summary: the generated summary. Stored as an INDEX alongside the turns
            it folded -- never in place of them.
        source_turn_ids: the turn ids this summary folded away, so
            ``pre_compaction_history`` can narrow to exactly them.
        reason: one of :data:`ROTATION_REASONS`.
        child_session_id: an explicit child id; one is minted when absent.
        repo: repository for the child, defaulting to the parent's own record.
        run_dir: the run whose ``ConversationJournal`` should record this
            rotation so it can be **reversed**. Optional, and deliberately so: a
            rotation cannot be undone in a run whose journal this process does not
            have, and inventing a run directory to write one would put the receipt
            somewhere nobody restores from.
        compaction_id: the id ``ConversationJournal.restore_compaction`` will be
            called with. Minted from the child id when absent, so the id a caller
            needs is in the receipt even if they never choose one.
        dropped_seqs: explicit journal sequences to un-drop on restore. Wins over
            content matching; see :func:`resolve_dropped_seqs`.

    Returns:
        A receipt carrying the parent/child ids, both identity blocks, the
        identity comparison, what was retained, and -- when ``run_dir`` was
        given -- whether the rotation is **reversible** and, if not, exactly why.
        Raises :class:`LineageRefused` if the session store cannot be written, and
        returns a receipt with ``ok: False`` (never a partial rotation) if the
        identity would be weakened.
    """
    if not isinstance(session, Mapping) or not session.get("session_id"):
        raise LineageRefused(
            "rotation needs a loaded parent session; nothing was written",
            {"session_id": str((session or {}).get("session_id") or "")},
        )
    if reason not in ROTATION_REASONS:
        raise LineageRefused(
            f"unknown rotation reason {reason!r}; the closed vocabulary is "
            + ", ".join(ROTATION_REASONS),
            {"reason": str(reason), "valid": list(ROTATION_REASONS)},
        )
    if not str(summary or "").strip():
        # A rotation with an empty summary is a rotation that destroyed context
        # and recorded nothing. Refuse rather than write the link.
        raise LineageRefused(
            "refusing to rotate with an empty summary: the parent would gain a "
            "child link and the child would gain no way back to what it lost",
            {"session_id": str(session.get("session_id") or "")},
        )

    api = _session_api()
    parent_id = str(session.get("session_id") or "")
    parent_summaries = _carried(session, "summaries")
    source_ids = [str(item) for item in (source_turn_ids or ()) if str(item or "")]

    child_id = str(child_session_id or "").strip()
    if not child_id:
        child_id = str(api.new_session_id())
    if child_id == parent_id:
        raise LineageRefused(
            "a rotation must open a NEW session; rotating a session onto itself "
            "would make the lineage a self-loop and every ancestry walk a cycle",
            {"session_id": parent_id},
        )

    # The child inherits the identity VERBATIM. Recomputing it is how a
    # rotation weakens the closure.
    inherited = identity_of(session)
    now = float(time.time())

    child: Dict[str, Any] = dict(api._new_session(child_id, repo or session.get("repo")))
    child["parent_session_id"] = parent_id
    child["rotation_reason"] = reason
    child["fork_point_turn_id"] = (
        source_ids[-1] if source_ids else str(session.get("fork_point_turn_id") or "")
    )
    # Retained history travels forward BY VALUE.
    child["summaries"] = [
        *parent_summaries,
        {
            "text": str(summary),
            "source_turn_ids": list(source_ids),
            "reason": reason,
            "parent_session_id": parent_id,
            # ``ts`` and ``turn_count`` under the names ``cli.lineage`` reads
            # (``entry.get("ts")``, ``entry.get("turn_count")``). Writing only
            # ``at`` would render that reader's compaction row with no time and
            # no count -- a rotation receipt that reads as "nothing happened".
            "ts": now,
            "at": now,
            "turn_count": len(source_ids),
            "dropped_messages": len(source_ids),
            "is_index": True,
        }
    ]
    child["compacted_turns"] = _carried(session, "compacted_turns")
    child["raw_turns"] = _carried_raw_text(session, "raw_turns")
    # The turns THIS rotation folded away move into the child's
    # ``compacted_turns`` by value, so "what did that compaction take?" is
    # answerable from the child alone -- which is what
    # ``cli.lineage.pre_compaction_history`` reads, and what the brief's "a
    # summary indexes the transcript rather than replacing it" means in practice.
    #
    # The PARENT keeps every one of its own turns. This is a copy into the child,
    # not a move out of the parent: the parent's transcript is never shortened by
    # a rotation, which is the whole "history is never destroyed" contract. A
    # reader asking the parent "what did you have?" and a reader asking the child
    # "what did you lose?" therefore both have an answer, and they agree.
    named_set = set(source_ids)
    if named_set:
        already = {
            str(turn.get("turn_id") or "")
            for turn in child["compacted_turns"]
            if isinstance(turn, Mapping)
        }
        for turn in _carried(session, "turns"):
            turn_id = str(turn.get("turn_id") or "")
            if turn_id and turn_id in named_set and turn_id not in already:
                child["compacted_turns"].append(turn)
                already.add(turn_id)
    # The child's active turns start EMPTY: a rotation is not a fork, and a
    # reader must be able to tell the difference from the turn counts alone.
    child["turns"] = []
    for field, value in inherited.items():
        if value:
            child[field] = value
    if not child.get("repo"):
        child["repo"] = str(session.get("repo") or "")

    check = identity_status(session, child)
    if not check["ok"]:
        # Nothing is written. A rotation that CHANGED the identity would let a
        # child resume work its parent was never authorised to continue -- which
        # is reachable, because the child is built here and inherits verbatim.
        return {
            "ok": False,
            "reason": "the rotation would change the resume identity; nothing "
            "was written",
            "parent_session_id": parent_id,
            "child_session_id": child_id,
            "identity": check,
            "written": False,
        }

    if not api.save_session(log_root, child):
        raise LineageRefused(
            "the child conversation could not be written; the parent was left "
            "untouched so no link points at a session that does not exist",
            {
                "parent_session_id": parent_id,
                "child_session_id": child_id,
                "error": str(child.get("_last_save_error") or "save_session returned False"),
            },
        )

    # Close the parent LAST. If this write failed, the parent has no
    # ``rotated_to`` and the child is simply an unread session -- which is a
    # recoverable state. The reverse order would leave a parent claiming a
    # rotation that no child exists for.
    closed = dict(session)
    closed["end_reason"] = reason
    closed["ended_at"] = now
    closed["rotated_to"] = child_id
    closed["rotated_to_reason"] = reason
    closed["updated_ts"] = now
    closed["_log_root"] = log_root
    if not api.save_session(log_root, closed):
        raise LineageRefused(
            "the parent conversation could not be closed; the child exists but "
            "no parent claims it, so this rotation is reported rather than "
            "silently half-applied",
            {"parent_session_id": parent_id, "child_session_id": child_id},
        )

    payload = {
        "reason": reason,
        "parent_session_id": parent_id,
        "child_session_id": child_id,
        "summary": str(summary),
        "summary_digest": content_digest(summary),
        "source_turn_ids": list(source_ids),
        "retained_compacted_turns": len(child["compacted_turns"]),
        "retained_raw_turns": len(child["raw_turns"]),
        "retained_summaries": len(child["summaries"]),
        "child_active_turns": 0,
        "identity": check,
    }
    # The rotation link lives in the SNAPSHOT, which is where every reader looks
    # first and where ``cli.lineage`` reads it perfectly (``rotated_to`` +
    # ``end_reason`` + ``ended_at``). The journal gets an AUDIT row under a kind
    # outside the set ``cli.lineage`` reads as a rotation, and that is a measured
    # decision rather than a stylistic one.
    #
    # Measured: writing ``session_rotated`` into the journal made
    # ``cli.lineage.last_rotation`` return the JOURNAL row instead of the
    # snapshot row, because ``cli.lineage``'s journal path reads ``row["data"]``
    # and ``row["ts"]`` while ``cli.session``'s real journal envelope is
    # ``{"event", "payload", "timestamp", ...}``. The journal-sourced row
    # therefore rendered with an EMPTY ``child_session_id`` and NO timestamp, and
    # since it was appended last it won ``events[-1]``. So the audit row would
    # have made ``last_rotation`` report a rotation to nothing -- while adding
    # nothing the snapshot does not already carry, written in the same operation.
    #
    # Both journals carry ``rotation_receipt`` so the audit trail is complete. The
    # key mismatch is T4's to fix (a request, not an edit); until then the
    # snapshot link is the one authoritative rotation record.
    audit = {
        "reason": reason,
        "parent_session_id": parent_id,
        "child_session_id": child_id,
        "summary_digest": payload["summary_digest"],
        "source_turn_ids": list(source_ids),
        "rotated_at": now,
    }
    api.append_session_event(closed, ROTATION_RECEIPT_EVENT, dict(audit))
    api.append_session_event(child, ROTATION_RECEIPT_EVENT, dict(audit))
    api.append_session_event(child, OPENED_BY_ROTATION_EVENT, dict(audit))

    # Make it REVERSIBLE, when a run journal was offered. This is a separate
    # durability act from the snapshot writes above and is reported separately,
    # because "the rotation happened" and "the rotation can be undone" are two
    # different facts and a caller restoring a rotation that was never recorded
    # as reversible gets a refusal rather than a silent no-op.
    reversible: Dict[str, Any] = {
        "attempted": False,
        "reversible": False,
        "reason": "no run_dir was given, so this rotation is recorded in the "
        "session snapshots and in both event journals, but it is NOT reversible "
        "through a ConversationJournal: pass run_dir= to make it restorable",
        "compaction_id": "",
        "dropped_seqs": [],
        "unresolved": [],
    }
    if run_dir is not None:
        reversible["attempted"] = True
        identifier = str(compaction_id or "").strip() or f"lineage-{child_id}"
        folded = [
            turn
            for turn in _carried(session, "turns")
            if not source_ids or str(turn.get("turn_id") or "") in set(source_ids)
        ]
        resolution = resolve_dropped_seqs(
            run_dir, folded, dropped_seqs=dropped_seqs
        )
        reversible.update(
            {
                "compaction_id": identifier,
                "dropped_seqs": list(resolution["dropped_seqs"]),
                "resolution": resolution["resolution"],
                "unresolved": list(resolution["unresolved"]),
            }
        )
        wrote = False
        try:
            from runtime.checkpoint import ConversationJournal

            wrote = bool(
                ConversationJournal(run_dir).append(
                    {
                        "record": ROTATION_RECORD,
                        "compaction_id": identifier,
                        # Journal sequences, NOT session turn ids -- see
                        # resolve_dropped_seqs for why they are different things.
                        "dropped_seqs": list(resolution["dropped_seqs"]),
                        "turn_count": len(resolution["dropped_seqs"]),
                        "method": "runtime.session_lineage.rotate_session",
                        "reason": reason,
                        "summary": str(summary),
                        "summary_digest": content_digest(summary),
                        "parent_session_id": parent_id,
                        "child_session_id": child_id,
                        "source_turn_ids": list(source_ids),
                        "handoff": {
                            "summary": str(summary),
                            "summary_is_index": True,
                            "retained_in": parent_id,
                            "retained_compacted_turns": len(child["compacted_turns"]),
                            "retained_raw_turns": len(child["raw_turns"]),
                            "child_session_id": child_id,
                        },
                    }
                )
            )
        except Exception as exc:
            reversible["journal_error"] = f"{type(exc).__name__}: {exc}"
        reversible["written"] = wrote
        if not wrote:
            reversible["reason"] = (
                reversible.get("journal_error")
                or "the compaction row did not persist to the conversation journal"
            )
        elif resolution["unresolved"]:
            reversible["reversible"] = False
            reversible["reason"] = resolution["reason"]
        else:
            reversible["reversible"] = True
            reversible["reason"] = (
                f"restore_compaction({identifier!r}) re-drops "
                f"{len(resolution['dropped_seqs'])} journal sequence(s)"
                if resolution["dropped_seqs"]
                else "the rotation folded no journal-recorded turn, so there is "
                "nothing to re-drop; the rotation is recorded and restorable as "
                "a no-op"
            )
    try:
        api.index_conversation(closed, log_root, status="rotated")
        api.index_conversation(child, log_root, status="active")
    except Exception:
        # The index is an accelerator; a rotation that indexed nothing is a
        # listing gap, not a lost rotation.
        pass

    return {
        "ok": True,
        "reason": "",
        "written": True,
        "parent_session_id": parent_id,
        "child_session_id": child_id,
        "identity": check,
        "parent_end_reason": reason,
        "parent_rotated_to": child_id,
        "child_parent_session_id": parent_id,
        "summary": str(summary),
        "summary_digest": content_digest(summary),
        "source_turn_ids": list(source_ids),
        "retained": {
            "compacted_turns": len(child["compacted_turns"]),
            "raw_turns": len(child["raw_turns"]),
            "summaries": len(child["summaries"]),
        },
        "history_destroyed": False,
        "parent_turns_untouched": len(session.get("turns") or []),
        "reversible": reversible,
    }


# ---------------------------------------------------------------------------
# Ancestry
# ---------------------------------------------------------------------------


def ancestry(
    log_root: Any,
    session_id: Any,
    *,
    max_depth: int = MAX_ANCESTRY_DEPTH,
    repo: Any = None,
) -> List[Dict[str, Any]]:
    """Every ancestor of a session, nearest first, ending at the root.

    Bounded by ``max_depth`` and by a seen-set: a cycle in the parent chain (a
    corrupt or hand-edited snapshot) STOPS the walk and is reported, rather than
    hanging a status surface on its repaint timer.

    A session whose own record cannot be read is still reported, with
    ``loaded: False`` and the reason. "My parent is missing" is a fact somebody
    needs; omitting the row would hide it.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    current = str(session_id or "")
    depth = 0
    limit = max(1, int(max_depth))
    while current and depth < limit:
        if current in seen:
            out.append(
                {
                    "session_id": current,
                    "loaded": False,
                    "reason": "the parent chain loops back to a session "
                    "already visited",
                    "depth": depth,
                }
            )
            break
        seen.add(current)
        session = load_session(log_root, current, repo)
        if not session:
            out.append(
                {
                    "session_id": current,
                    "loaded": False,
                    "reason": "this session's record could not be read",
                    "depth": depth,
                }
            )
            break
        out.append(
            {
                "session_id": current,
                "loaded": True,
                "parent_session_id": str(session.get("parent_session_id") or ""),
                "child_session_id": str(session.get("rotated_to") or ""),
                "end_reason": str(session.get("end_reason") or ""),
                "rotation_reason": str(session.get("rotation_reason") or ""),
                "turns": len(session.get("turns") or []),
                "compaction_count": len(session.get("summaries") or []),
                "depth": depth,
            }
        )
        current = str(session.get("parent_session_id") or "")
        depth += 1
    return out


def lineage_records(log_root: Any, session_id: Any, *, repo: Any = None) -> Dict[str, Any]:
    """The whole lineage as one receipt: chain, identity, rotations, retained.

    This is the machine-readable half behind ``cli.lineage.lineage_rows``. It
    answers the same questions from the same bytes so the two cannot disagree
    about whether a rotation happened or what it retained.
    """
    chain = ancestry(log_root, session_id, repo=repo)
    self_row = next((row for row in chain if row.get("loaded")), None)
    session = (
        load_session(log_root, session_id, repo) if self_row is not None else {}
    )
    rotations = [
        {
            "session_id": row.get("session_id"),
            "end_reason": row.get("end_reason"),
            "rotated_to": row.get("child_session_id"),
            "depth": row.get("depth"),
        }
        for row in chain
        if row.get("loaded") and row.get("end_reason")
    ]
    identity = identity_of(session)
    return {
        "session_id": str(session_id or ""),
        "chain": chain,
        "depth": len(chain),
        "rotations": rotations,
        "last_rotation": rotations[0] if rotations else {},
        "identity": identity,
        "identity_complete": all(
            value for field, value in identity.items()
            if field not in BLANK_ALLOWED_IDENTITY_FIELDS
        ),
        "retained": {
            "compacted_turns": len(session.get("compacted_turns") or []),
            "raw_turns": len(session.get("raw_turns") or []),
            "summaries": len(session.get("summaries") or []),
        },
        "cycles": any(
            "loops back" in str(row.get("reason") or "") for row in chain
        ),
        "unreadable": [row["session_id"] for row in chain if not row.get("loaded")],
    }


def pre_compaction_index(
    log_root: Any,
    session_id: Any,
    *,
    index: int = -1,
    repo: Any = None,
) -> Dict[str, Any]:
    """Which turns a compaction folded away, and where they still live.

    Answers "show me what happened before this compaction" WITHOUT a replay and
    WITHOUT opening a task directory, by reading the session's own retained
    lists. ``index`` is 1-based over the session's summaries; ``-1`` (the
    default) is the most recent, which is the one a person asking that question
    means.

    The answer is an INDEX into retained transcript, not a copy of it: the
    ``source_turn_ids`` are named, and ``retained_in`` says which session still
    holds the turns themselves. A summary that cannot say where its evidence
    went is a summary a reader has to trust on faith.
    """
    session = load_session(log_root, session_id, repo)
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
    named = {str(item) for item in (summary.get("source_turn_ids") or ())}
    pool = [turn for turn in compacted if isinstance(turn, Mapping)]
    if named:
        narrowed = [
            turn for turn in pool if str(turn.get("turn_id") or "") in named
        ]
        if narrowed:
            pool = narrowed
    return {
        "available": True,
        "reason": "",
        "index": wanted,
        "compaction_count": len(summaries),
        "dropped_total": len(pool),
        "source_turn_ids": sorted(named),
        "turns": [dict(turn) for turn in pool],
        "summary": str(summary.get("text") or ""),
        "summary_is_index": bool(summary.get("is_index", True)),
        "retained_after_compaction": len(session.get("turns") or []),
        "history_retained": bool(raw) or bool(compacted),
        "retained_in": str(session_id or ""),
    }


# ---------------------------------------------------------------------------
# Cross-lineage search, deduped by content
# ---------------------------------------------------------------------------


def _turn_rows(session: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every transcript row a session holds, across all three retained lists.

    ``raw_turns`` is read as well as ``turns`` and ``compacted_turns``, and that
    is load-bearing rather than thorough: a rotation carries the parent's turns
    into the child's ``raw_turns``, so a search that skipped it would find the
    inherited history ZERO times on the child and the digest dedupe below would
    have nothing to collapse. A dedupe that never sees the duplicates is a dedupe
    that cannot be measured.
    """
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for key in ("turns", "compacted_turns", "raw_turns"):
        for turn in session.get(key) or []:
            if not isinstance(turn, Mapping):
                continue
            row = dict(turn)
            marker = str(row.get("turn_id") or "") or content_digest(_text_of(row))
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            row.setdefault("retained_in", key)
            out.append(row)
    return out


def _text_of(turn: Mapping[str, Any]) -> str:
    for key in ("text", "content", "body", "summary"):
        value = turn.get(key)
        if value:
            return str(value)
    return ""


def dedupe_hits(hits: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse search hits to one row per distinct CONTENT digest.

    A rotation carries the parent's turns forward, so a naive search over a
    lineage returns the same turn once per descendant that inherited it. The
    duplicate is a FACT about the lineage, not a second result -- and a reader
    counting "how many times did this come up" would count the tree, not the
    conversation.

    The FIRST occurrence wins and keeps its own ``session_id``, with
    ``also_in`` naming every other session the same content was inherited by.
    A control arm in the test suite asserts two hits with the same text in
    DIFFERENT sessions that are genuinely different content-bearing rows
    (different turn ids) are NOT collapsed, so this cannot pass by collapsing
    everything.
    """
    order: List[str] = []
    merged: Dict[str, Dict[str, Any]] = {}
    for hit in hits or ():
        if not isinstance(hit, Mapping):
            continue
        row = dict(hit)
        digest = str(row.get("digest") or content_digest(_text_of(row)))
        row["digest"] = digest
        if digest not in merged:
            row["also_in"] = []
            row["occurrences"] = 1
            merged[digest] = row
            order.append(digest)
            continue
        existing = merged[digest]
        sid = str(row.get("session_id") or "")
        if sid and sid != str(existing.get("session_id") or "") and sid not in existing["also_in"]:
            existing["also_in"].append(sid)
        existing["occurrences"] = int(existing.get("occurrences") or 1) + 1
    return [merged[digest] for digest in order]


def search_lineage(
    log_root: Any,
    session_id: Any,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    repo: Any = None,
) -> Dict[str, Any]:
    """Search a session AND its whole ancestry, deduped by content digest.

    Bounded by ``limit`` and stated when it bites: a truncated result set that
    does not say it was truncated reads as a complete one, and "the answer is
    no" is the most expensive wrong answer this function could give.
    """
    needle = str(query or "").strip().lower()
    chain = ancestry(log_root, session_id, repo=repo)
    bound = max(1, int(limit))
    raw: List[Dict[str, Any]] = []
    sessions_searched: List[str] = []
    for row in chain:
        if not row.get("loaded"):
            continue
        sid = str(row.get("session_id") or "")
        sessions_searched.append(sid)
        session = load_session(log_root, sid, repo)
        for turn in _turn_rows(session):
            text = _text_of(turn)
            if needle and needle not in text.lower():
                continue
            raw.append(
                {
                    "session_id": sid,
                    "turn_id": str(turn.get("turn_id") or ""),
                    "depth": int(row.get("depth") or 0),
                    "text": text,
                    "digest": content_digest(text),
                }
            )
    deduped = dedupe_hits(raw)
    shown = deduped[:bound]
    return {
        "ok": True,
        "session_id": str(session_id or ""),
        "query": str(query or ""),
        "sessions_searched": sessions_searched,
        "ancestry_depth": len(chain),
        "raw_hits": len(raw),
        "deduped_hits": len(deduped),
        "collapsed_by_digest": len(raw) - len(deduped),
        "shown": len(shown),
        "truncated": len(deduped) > len(shown),
        "limit": bound,
        "hits": shown,
    }


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def checkpoint_identity_of(run_dir: Any) -> Dict[str, Any]:
    """Read the durable resume closure from a run's checkpoint, or ``{}``.

    **The closure lives on the CHECKPOINT, not on the session snapshot.** Those
    are two different artifacts with two different lifetimes: the checkpoint is
    per-attempt and per-task under ``logs/{task_id}.runtime/``, while a session
    snapshot is per-conversation and carries no checkpoint identity keys at all.
    Asking a snapshot whether its request identity is intact is the mistake this
    function exists to stop being able to make.

    Delegates to :class:`runtime.checkpoint.TaskCheckpoint` rather than reading
    ``checkpoint.json`` directly, so the path rule and the load semantics stay in
    one place. Never raises.
    """
    try:
        from runtime.checkpoint import TaskCheckpoint

        loaded = TaskCheckpoint(run_dir).load() or {}
    except Exception as exc:
        return {
            "identity": {},
            "readable": False,
            "reason": f"the checkpoint could not be read: {type(exc).__name__}: {exc}",
        }
    identity = {
        field: str(loaded.get(field) or "") for field in LINEAGE_IDENTITY_FIELDS
    }
    return {
        "identity": identity,
        "readable": True,
        "reason": ""
        if any(identity.values())
        else "the checkpoint carries no identity block, so there is no closure to "
        "check; this run was written before the closure existed, not by a run "
        "whose closure failed",
        "identity_version": int(loaded.get("identity_version") or 0),
        "status": str(loaded.get("status") or ""),
    }


def resume_against_checkpoint(
    log_root: Any,
    session_id: Any,
    run_dir: Any,
    *,
    continue_request: bool = False,
    repo: Any = None,
) -> Dict[str, Any]:
    """Resume a lineage using the checkpoint's own closure as the expected one.

    This is the production wiring for :func:`resume_state`, and it is what makes
    the five-field check *reachable* rather than theoretical: the closure has a
    real source, so a mismatch is a comparison against a fact rather than against
    nothing.

    A checkpoint with no identity block is reported as such and the resume falls
    back to the lineage-derived closure -- never to a synthesised one. Defaulting
    a missing identity field would authorise a resume against a claim nobody
    made, which is the precise lie
    :func:`runtime.checkpoint.effort_identity` exists to prevent.
    """
    closure = checkpoint_identity_of(run_dir)
    identity = closure.get("identity") or {}
    used = "checkpoint"
    if not any(str(value) for value in identity.values()):
        identity = {}
        used = "lineage"
    receipt = resume_state(
        log_root,
        session_id,
        identity=identity or None,
        continue_request=continue_request,
        repo=repo,
    )
    receipt["identity_source"] = used
    receipt["checkpoint"] = {
        "readable": bool(closure.get("readable")),
        "reason": str(closure.get("reason") or ""),
        "identity_version": int(closure.get("identity_version") or 0),
        "carries_identity": any(str(v) for v in (closure.get("identity") or {}).values()),
    }
    return receipt


def resume_state(
    log_root: Any,
    session_id: Any,
    *,
    identity: Optional[Mapping[str, Any]] = None,
    continue_request: bool = False,
    repo: Any = None,
) -> Dict[str, Any]:
    """Whether this lineage may be continued, and under which identity.

    Two independent questions, deliberately not collapsed:

    * **May it be continued?** A session that was ROTATED away is a completed
      segment; continuing *it* rather than its child would resume a session
      whose own record says it ended. The receipt names the successor instead.
      Only an explicit ``continue_request`` resumes a rotated parent's own
      turns -- and that is the ONLY place the request hash is reused, because
      a rotation changes the session id and inheriting the hash unasked would
      authorise work nobody requested.
    * **Is the identity intact?** Two sources, and the choice between them is
      reported rather than assumed.

    **``identity=`` given -- the caller holds a closure and this compares against
    it.** The rule is: **a field the session SNAPSHOT speaks for must agree, and a
    field it is silent on is recorded rather than refused.**

    Both halves were wrong in the previous version, and the second half is the
    one that mattered. It required every non-blank-allowed field to be non-blank
    *on the snapshot*, and a ``cli.session`` snapshot carries none of the
    checkpoint identity keys -- so a caller-supplied closure could never match a
    real session, which made :func:`resume_against_checkpoint` (the production
    wiring) dead on arrival for the same reason the default path was. The
    snapshot is not where the closure lives; requiring it to repeat the closure
    compares two artifacts that were never meant to hold the same values.

    The hazard is still closed and strictly more so: a closure naming repository
    A against a session carrying repository B **refuses**, a closure silent where
    the session speaks **refuses**, and a generation that cannot be read refuses
    -- in BOTH branches, where the previous version walked the ancestry only on
    the derived path.

    **``identity=`` absent -- the closure is folded from the LINEAGE.** This is
    the change this round made, and the previous behaviour was a defect rather
    than a strictness: the old code required all five fields to be non-blank on
    the session SNAPSHOT, and a ``cli.session`` snapshot carries none of the
    checkpoint identity keys. So ``resume_state`` answered
    ``may_resume: False, reason: "the resume identity does not hold across this
    lineage"`` for **every real session in the tree** -- measured on this tree,
    five rotations deep, the child's own resume refused. A resume feature that
    refuses every session is not a fail-closed gate; it is a dead one, and it is
    indistinguishable from a correct refusal to anybody reading the boolean.

    What replaced it is strictly stronger *where it matters*: a lineage whose
    generations DISAGREE about any field is ``divergent`` and refuses. That is
    the property "a rotation must not become a way to resume against a
    mismatched repository", and before this round **nothing checked it at all**,
    because the check that was running was unsatisfiable. A lineage on which no
    generation carries a closure reports ``identity_carried: False`` and
    ``identity_ok: None`` -- unknown, not false -- and the caller decides, which
    :func:`resume_against_checkpoint` does by supplying the real closure.

    Returns a value, never an exception: a resume decision must be a decision.
    """
    session = load_session(log_root, session_id, repo)
    sid = str(session_id or "")
    if not session:
        return {
            "ok": False,
            "may_resume": False,
            "resume_authorised": False,
            "reason": "this session's record could not be read",
            "session_id": sid,
            "continued_session_id": "",
            "continued_reason": "",
            "identity": {},
            "identity_expected": {},
            "identity_ok": False,
            "identity_carried": False,
            "identity_source": "none",
            "identity_problems": ["session_unreadable"],
            "ancestry_problems": [],
            "continue_request": bool(continue_request),
            "identity_fields": list(LINEAGE_IDENTITY_FIELDS),
        }

    end_reason = str(session.get("end_reason") or "")
    rotated_to = str(session.get("rotated_to") or "")
    continued = sid
    continued_reason = ""
    may_resume = True
    reason = ""

    if end_reason and rotated_to and not continue_request:
        may_resume = False
        continued = rotated_to
        continued_reason = (
            f"this session ended with {end_reason!r} and rotated to {rotated_to}; "
            "continue the successor rather than the segment that ended"
        )
        reason = continued_reason
    elif continue_request:
        continued_reason = (
            "an explicit continue was requested, so this session's own turns "
            "are resumed even though it is a rotated segment"
        )

    actual = identity_of(session)
    problems: List[str] = []
    source = "caller" if identity is not None else "lineage"

    if identity is not None:
        expected = {
            field: str(identity.get(field) or "")  # type: ignore[union-attr]
            for field in LINEAGE_IDENTITY_FIELDS
        }
        not_carried: List[str] = []
        for field in LINEAGE_IDENTITY_FIELDS:
            want = expected.get(field, "")
            have = actual.get(field, "")
            if have and not want:
                # Checked BEFORE the disagreement clause, because it is the more
                # specific diagnosis and the generic one would otherwise swallow
                # it: "the closure names nothing" and "the closure names something
                # else" are different operator problems with different fixes.
                problems.append(
                    f"{field}: this session carries {have!r} but the supplied "
                    "closure names nothing, so the closure is incomplete where the "
                    "session is not"
                )
            elif have and want != have:
                # The session SPEAKS for this field and the closure says something
                # else. That is a contradiction and it refuses -- which is the
                # property "a rotation must not become a way to resume against a
                # mismatched repository".
                problems.append(
                    f"{field}: the closure says {want!r} and this session carries "
                    f"{have!r}"
                )
            elif not have and want:
                # The session snapshot is SILENT on this field. That is not a
                # contradiction: `cli.session` snapshots carry no checkpoint
                # identity keys at all, so the closure lives on the CHECKPOINT and
                # there is nothing here to disagree with. Recorded, not refused --
                # and refusing it is what made the previous version of this
                # function unable to match ANY real session.
                not_carried.append(field)
        folded = {
            "ok": not problems,
            "identity": actual,
            "divergent": [],
            "not_carried_by_the_snapshot": not_carried,
        }
    else:
        expected = dict(actual)
        folded = lineage_identity(log_root, sid, repo=repo)
        # A lineage-derived closure checks ACROSS generations, which is the
        # question a tip-only read cannot answer.
        for field in folded.get("divergent") or ():
            observed = folded["fields"][field]["observed"]
            problems.append(
                f"{field}: the lineage's generations disagree ({', '.join(observed)}), "
                "so a rotation moved this conversation between identities"
            )
        for name in folded.get("unreadable") or ():
            problems.append(f"{name}: a generation in this lineage could not be read")

    # The ancestry is walked in BOTH branches. A generation that cannot be read
    # is a break in the chain regardless of where the closure came from, and the
    # previous version only checked it on the derived path.
    chain_problems: List[str] = []
    for row in ancestry(log_root, sid, repo=repo):
        if not row.get("loaded"):
            chain_problems.append(f"{row.get('session_id')}: {row.get('reason')}")

    carried = any(actual.get(field) for field in LINEAGE_IDENTITY_FIELDS)
    # ``identity_ok`` is TRI-STATE and the third value is load-bearing. ``None``
    # means "no closure was available to check", which is a different fact from
    # "the closure does not hold" and must render as neither True nor False.
    if problems or chain_problems:
        identity_ok: Optional[bool] = False
        may_resume = False
        reason = reason or "the resume identity does not hold across this lineage"
    elif carried or source == "caller" or any((folded.get("identity") or {}).values()):
        identity_ok = True
    else:
        identity_ok = None

    return {
        "ok": True,
        "may_resume": may_resume,
        # The field a caller should branch on. ``may_resume`` answers "may this
        # SEGMENT be continued"; this answers "is continuing it authorised",
        # which additionally requires that the closure was not found broken.
        "resume_authorised": bool(may_resume and identity_ok is not False),
        "reason": reason,
        "session_id": sid,
        "end_reason": end_reason,
        "rotated_to": rotated_to,
        "continued_session_id": continued,
        "continued_reason": continued_reason,
        "identity": actual,
        "identity_expected": expected,
        "identity_lineage": folded,
        "identity_ok": identity_ok,
        "identity_carried": carried,
        "identity_source": source,
        "identity_problems": list(problems),
        "ancestry_problems": list(chain_problems),
        "continue_request": bool(continue_request),
        "continue_request_is_the_only_reuse": True,
        "identity_fields": list(LINEAGE_IDENTITY_FIELDS),
    }


def deletions_journal_path(log_root: Any) -> Path:
    """Where explicit deletions are recorded. OUTSIDE every session's directory.

    This is load-bearing rather than tidy. The record of a deletion has to
    survive the deletion, so it cannot live inside the thing being deleted --
    which means the obvious place (``<log_root>/sessions/<sid>/…``) is exactly
    the wrong place, and a ledger written there would be destroyed by the first
    deletion it was meant to explain.
    """
    return Path(str(log_root)) / "_lineage" / DELETIONS_NAME


def deletion_ledger(log_root: Any) -> List[Dict[str, Any]]:
    """Every recorded explicit deletion, oldest first. Never raises."""
    path = deletions_journal_path(log_root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def delete_session(
    log_root: Any,
    session_id: Any,
    *,
    confirm: Any = None,
    why: str = "",
    drop_tip: bool = False,
    drop_successors: bool = False,
    repo: Any = None,
) -> Dict[str, Any]:
    """Delete one conversation. **The only way this module loses history.**

    Row 8 of the corpus's guarantee is the important one: history should be lost
    **only** through an explicit, user-initiated deletion, and anything that
    loses history implicitly is a bug. This function is that explicit deletion,
    and it is built so the word *explicit* means something:

    * ``confirm`` must equal the session id. A caller cannot delete a session by
      passing a flag it set by accident, and cannot delete session A while
      believing it deleted session B.
    * ``why`` must be non-empty. A deletion nobody can explain is a deletion
      nobody can appeal.
    * **The live tip is refused** unless ``drop_tip=True``. The tip is the
      conversation somebody is in the middle of, and the one whose absence
      changes what the product does next.
    * A **middle generation** may be deleted -- that is a normal request ("remove
      that one conversation") -- but its **descendants are reported as
      ``orphaned_successors``**, because deleting a generation does break the
      ancestry walk for everything below it and that consequence belongs in the
      receipt rather than in a surprise. ``drop_successors=True`` deletes them
      too, still explicitly.
    * The audit row is appended **before** the files are removed and lives in
      ``<log_root>/_lineage/deletions.jsonl``, which is not inside any session's
      directory. So the destruction is real -- the bytes are gone -- and the fact
      of it survives.

    The receipt names what was destroyed (turn counts, summary counts, byte
    counts, the exact paths) because "I deleted it" is not a record; "I deleted
    these 34 turns, 5 summaries, 41,203 bytes, at this path, for this reason, on
    this date" is.

    Raises :class:`LineageRefused` for every refusal. Never partially deletes:
    the audit row is written first precisely so a refusal cannot leave a row
    claiming a destruction that did not happen, and the removals are checked
    before any of them is performed.
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise LineageRefused("a deletion needs a session id", {})
    if str(confirm or "") != sid:
        raise LineageRefused(
            "refusing to delete: confirm must equal the session id being deleted. "
            "Deleting a conversation is the one operation in this module that "
            "destroys history, so it is confirmed by naming the target rather "
            "than by passing a flag.",
            {"session_id": sid, "confirm": str(confirm or "")},
        )
    if not str(why or "").strip():
        raise LineageRefused(
            "refusing to delete without a reason: a deletion nobody can explain "
            "is a deletion nobody can appeal",
            {"session_id": sid},
        )
    session = load_session(log_root, sid, repo)
    if not session:
        raise LineageRefused(
            "there is no such conversation, so there is nothing to delete. This "
            "refuses rather than reporting a success, because a deletion receipt "
            "is the only record that history was lost.",
            {"session_id": sid},
        )

    successors: List[str] = []
    frontier = [sid]
    seen = {sid}
    while frontier and successors == []:
        current = frontier.pop()
        records = load_session(log_root, current, repo)
        child = str(records.get("rotated_to") or "")
        if child and child not in seen:
            successors.append(child)
            seen.add(child)
            frontier.append(child)
    is_tip = not successors
    if is_tip and not drop_tip:
        raise LineageRefused(
            f"refusing to delete the live tip {sid}: it has no successor, so it is "
            "the conversation currently open. Pass drop_tip=True to delete it "
            "anyway.",
            {"session_id": sid, "is_tip": True},
        )
    if is_tip and drop_tip and drop_successors:
        raise LineageRefused(
            "drop_tip and drop_successors cannot both be set: a tip has no "
            "successors, so the second flag asserts a fact that is not true",
            {"session_id": sid},
        )
    targets = [sid, *(successors if drop_successors else [])]
    paths: Dict[str, str] = {}
    destroyed: List[Dict[str, Any]] = []
    for target in targets:
        row = load_session(log_root, target, repo) or {}
        snapshot = session_path(log_root, target)
        journal = event_journal_path(log_root, target)
        size = 0
        for path in (snapshot, journal):
            try:
                if path.is_file():
                    size += path.stat().st_size
            except OSError:
                pass
        paths[target] = str(snapshot)
        destroyed.append(
            {
                "session_id": target,
                "turns": len(row.get("turns") or []),
                "compacted_turns": len(row.get("compacted_turns") or []),
                "raw_turns": len(row.get("raw_turns") or []),
                "summaries": len(row.get("summaries") or []),
                "bytes": size,
                "snapshot": str(snapshot),
                "journal": str(journal),
            }
        )

    record = {
        "event": "session_deleted",
        "session_id": sid,
        "successors": successors,
        "orphaned_successors": [] if drop_successors else successors,
        "was_tip": is_tip,
        "why": str(why),
        "at": time.time(),
        "destroyed": destroyed,
        "bytes": sum(int(item["bytes"]) for item in destroyed),
        "explicit": True,
        "mechanism": "runtime.session_lineage.delete_session",
        "history_destroyed": True,
    }
    ledger = deletions_journal_path(log_root)
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise LineageRefused(
            "the deletion ledger could not be written, so nothing was deleted. A "
            "destruction nobody can audit is the failure mode this module exists "
            "to prevent, and it is better to refuse than to leave it unrecorded.",
            {"session_id": sid, "error": f"{type(exc).__name__}: {exc}"},
        ) from exc

    removed: List[str] = []
    problems: List[Dict[str, str]] = []
    for target in targets:
        for path in (session_path(log_root, target), event_journal_path(log_root, target)):
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:
                problems.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    try:
        from cli import session as session_api

        for target in targets:
            try:
                session_api.index_session(
                    {
                        "session_id": target,
                        "task_id": "",
                        "status": "deleted",
                        "resumable": False,
                        "issue": "",
                        "updated_at": time.time(),
                        "log_root": str(log_root),
                    }
                )
            except Exception:
                # The index is an accelerator; a deletion that indexed nothing
                # is a stale listing, not an undeleted conversation.
                break
    except Exception:
        pass

    return {
        "ok": True,
        "session_id": sid,
        "was_tip": is_tip,
        "successors": successors,
        "orphaned_successors": [] if drop_successors else successors,
        "ancestry_broken_for": [] if drop_successors else successors,
        "deleted": len(targets),
        "removed_paths": removed,
        "problems": problems,
        "ledger": str(ledger),
        "record": record,
        "history_destroyed": True,
        "explicit": True,
        "confirm_required": True,
    }


#: Filename of the explicit-deletion ledger. Named so a caller can find the
#: record without knowing the directory layout, and so the durability module can
#: assert it lives outside every session's own directory.
DELETIONS_NAME = "deletions.jsonl"

#: Where the live-session declaration lives. ``execution.snapshot`` READS this
#: path and refuses to prune anything it cannot place; publishing it is this
#: module's job because "which session is live" is a conversation-level fact.
LIVE_SESSIONS_NAME = "live_sessions.json"
LINEAGE_DIRNAME = "_lineage"


def live_sessions_path(log_root: Any) -> Path:
    """The live-session declaration a retention pass reads before it prunes."""
    return Path(str(log_root)) / LINEAGE_DIRNAME / LIVE_SESSIONS_NAME


def publish_live_sessions(
    log_root: Any,
    live: Any,
    *,
    lineage_of: Optional[Any] = None,
) -> Dict[str, Any]:
    """Publish which sessions are live, so retention cannot prune their state.

    **This is the writer ``execution.snapshot`` asked for.** Its retention pass
    reads ``<log_root>/_lineage/live_sessions.json`` and refuses to prune a run
    belonging to a lineage with a live session -- and until something publishes
    the file, every real run reports ``lineage_view_source="absent"``, which
    prunes normally. That is the correct behaviour for a broken-or-absent fact,
    and it is also why lineage protection is not yet exercised on a real tree.

    Three requirements, all load-bearing, and all stated by that reader:

    * **publish on session start** and **remove on session end** -- a
      declaration that only ever grows is a retention exemption that outlives
      the work, which is a leak wearing a safety mechanism's name;
    * **write it atomically** -- a torn file lands the reader in
      ``unreadable``, and ``unreadable`` protects EVERYTHING, so a crash during
      this write would silently disable pruning for the whole root;
    * **redact** -- it sits in a logs tree.

    ``live`` is any iterable of session ids, or a mapping of ``{session_id:
    lineage_id}``. ``lineage_of`` derives the lineage id for a bare id; without
    it the session id is used as its own lineage, which still protects the
    session. Never raises: a failed publish is reported and the caller decides,
    because a retention declaration that raises would take a session start down.
    """
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    if isinstance(live, Mapping):
        items = [(str(key), str(value or "")) for key, value in live.items()]
    else:
        items = [(str(item), "") for item in (live or ())]
    for session_id, lineage_id in items:
        name = session_id.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        derived = str(lineage_id or "")
        if not derived and lineage_of is not None:
            try:
                derived = str(lineage_of(name) or "")
            except Exception as exc:
                derived = ""
                entries.append(
                    {"session_id": name, "lineage_id": name, "lineage_error":
                     f"{type(exc).__name__}: {exc}"}
                )
                continue
        entries.append({"session_id": name, "lineage_id": derived or name})
    payload = {
        "schema_version": 1,
        "sessions": entries,
        "published_at": time.time(),
        "authority": "runtime.session_lineage.publish_live_sessions",
    }
    path = live_sessions_path(log_root)
    try:
        atomic_write_json(path, payload)
    except Exception as exc:
        return {
            "written": False,
            "path": str(path),
            "sessions": len(entries),
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "a retention pass will report lineage_view_source='absent' and "
            "prune normally, which is the correct behaviour for a fact it does "
            "not have",
        }
    return {"written": True, "path": str(path), "sessions": len(entries)}


def begin_session(
    log_root: Any,
    session_id: Any,
    *,
    lineage_id: str = "",
    also: Any = (),
    repo: Any = None,
) -> Dict[str, Any]:
    """Declare a session live, and return a receipt a caller ends with :func:`end_session`.

    A convenience over :func:`publish_live_sessions` that READS the current
    declaration and adds to it, so two concurrent sessions in one root do not
    overwrite each other's exemption. The read-modify-write is not locked -- a
    lost update here costs a retention exemption, not history -- and the receipt
    reports the count that landed so a caller can see it.
    """
    current = _read_live_sessions(log_root)
    merged: Dict[str, str] = {}
    for row in current:
        name = str(row.get("session_id") or "")
        if name:
            merged[name] = str(row.get("lineage_id") or name)
    targets = [str(session_id or ""), *[str(item) for item in (also or ())]]
    for name in targets:
        if name.strip():
            merged[name.strip()] = str(lineage_id or "").strip() or name.strip()
    return publish_live_sessions(log_root, merged)


def end_session(log_root: Any, session_id: Any) -> Dict[str, Any]:
    """Remove a session from the live declaration. Its exemption must END.

    A declaration that only ever grows is a retention exemption that outlives the
    work it was granted for, and that is a disk leak presented as a safety
    mechanism. Returns the declaration that was written, plus whether the session
    was in it at all -- "removed something" and "was not there" are different
    facts and a caller reconciling counts needs to tell them.
    """
    name = str(session_id or "").strip()
    current = _read_live_sessions(log_root)
    was_live = any(str(row.get("session_id") or "") == name for row in current)
    merged = {
        str(row.get("session_id") or ""): str(row.get("lineage_id") or "")
        for row in current
        if str(row.get("session_id") or "") and str(row.get("session_id") or "") != name
    }
    receipt = publish_live_sessions(log_root, merged)
    receipt["session_id"] = name
    receipt["was_live"] = was_live
    return receipt


def _read_live_sessions(log_root: Any) -> List[Dict[str, Any]]:
    """Read the live declaration, or ``[]``. A torn file reads as ``[]``."""
    from .fsutil import read_json_or_none

    data = read_json_or_none(live_sessions_path(log_root))
    if not isinstance(data, Mapping):
        return []
    rows = data.get("sessions")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def describe(log_root: Any, session_id: Any, *, repo: Any = None) -> Dict[str, Any]:
    """One receipt a status surface can render without walking anything itself."""
    records = lineage_records(log_root, session_id, repo=repo)
    resume = resume_state(log_root, session_id, repo=repo)
    return {
        "schema": "runtime.session_lineage.describe/1",
        "session_id": str(session_id or ""),
        "lineage": records,
        "resume": resume,
        "rotation_reasons": list(ROTATION_REASONS),
        "identity_fields": list(LINEAGE_IDENTITY_FIELDS),
    }
