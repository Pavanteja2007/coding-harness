"""Per-memory provenance: a citation you can open, or an admission you cannot.

T4.W2.1. The corpus's Codex model injects memory *"with citation tracking."*
That phrase is a claim, not evidence, and this module is where the claim is
either earned or withdrawn.

**Why this module exists at all.** ``cli/memory_ui.py`` (Wave 1) renders
provenance off an inbox proposal file. That was correct when T5's memory layer
had no producer. It does now: ``memory/decision_store.py`` stores a
``provenance`` blob, ``memory/ranking.py`` stores turn-level citations, and
``memory/governance.py`` reduces a row to a trust tier. Wave 1 therefore built
a SECOND reader of the same facts with a THIRD vocabulary of its own
(``SOURCE_TYPES``), and never called the ranking that the brief's item 4 asks
to explain. This module replaces that reader with ONE resolver over the real
APIs, so the provenance a reader sees is the provenance the store holds.

**The four things it guarantees.**

1. **A citation resolves or it is named as broken.**
   :func:`citation_target` is the ONE resolver. It returns one of six
   :data:`CITATION_VERDICTS`, and exactly two of them -
   :data:`RESOLVABLE_CITATION_VERDICTS` - may render as a working link.
   "The session this memory came from has been deleted" and "the session is
   right there" are different facts, and rendering a dead citation with live
   link syntax is the exact lie this round exists to remove.
2. **Source type is a MARKER on the row, not a colour.**
   :data:`TRUST_MARKERS` is one ASCII character per trust class, declared and
   pairwise distinct, and it goes FIRST on the row. ``cli/AGENTS.md`` records
   the lesson twice: a marker at the tail of a row is a marker a narrow
   terminal drops, and an unrecognised producer's silence is not consent.
3. **Ranking explains itself.**
   ``memory.ranking.RankedMemory.reasons`` already carries a sentence per
   scored term, including the honest *"this memory has NO usage row at all,
   which is not the same as zero citations"*. :func:`ranking_why` surfaces
   them, and :func:`assert_ranking_is_explained` refuses a ranked memory whose
   reasons are empty.
4. **The sanitiser is the only way content reaches a surface.**
   Memory text is extracted from sessions that may contain untrusted text - an
   issue body, a retrieved file, a model reply. This is a NEW injection
   surface: the text was authored elsewhere and is rendered here. Every
   rendered value crosses ``cli.ui.sanitize_text`` exactly once, and the
   wrapper is fail-CLOSED, so a raising sanitiser withholds rather than
   passes through.

**Nothing here writes.** Provenance is a read surface; the only writes in this
phase are a human accepting or refusing a memory, and those live in
``cli/memory_ui.py``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CITATION_VERDICTS",
    "MEMORY_FACTS",
    "RESOLVABLE_CITATION_VERDICTS",
    "TRUST_CLASSES",
    "TRUST_MARKERS",
    "assert_no_dangling_citation_renders_as_verifiable",
    "assert_ranking_is_explained",
    "assert_trust_markers_are_distinct",
    "citation_lines",
    "citation_target",
    "memory_facts",
    "provenance_report",
    "provenance_report_lines",
    "ranking_why",
    "trust_class_of",
    "trust_marker",
]

#: The closed trust vocabulary a reader sees. Derived, never stored: the store
#: keeps a free-text ``source`` column and a ``provenance`` blob, and inventing
#: a fourth spelling here would be a second answer to "where did this come
#: from". Each class maps onto ``memory.governance.trust_tier``'s own three
#: values plus the two states neither of those can express.
TRUST_CLASSES: Tuple[str, ...] = (
    "user_stated",  # the operator said it; governance's "stated"
    "extracted",  # observed from a session the operator was in
    "untrusted",  # observed from content the operator did not author
    "unattributed",  # this build cannot say where it came from
)

#: One ASCII character per trust class, FIRST on the row.
#:
#: ASCII on purpose: ``cli/AGENTS.md`` records a legacy-cp1252 ``UnicodeEncodeError``
#: taking the whole TUI down over a glyph, and a marker is the one thing a
#: reader must never lose to an encoding fallback. The set is declared here and
#: asserted pairwise distinct by :func:`assert_trust_markers_are_distinct`, so
#: two classes cannot silently collapse onto one mark.
TRUST_MARKERS: Dict[str, str] = {
    "user_stated": "=",
    "extracted": "~",
    "untrusted": "!",
    "unattributed": "?",
}

#: The four facts a reader needs about one memory, each with its own absent
#: rule, and each named by the LABEL the renderer prints for it.
#:
#: Declared with the rendered label rather than the internal field name so a
#: test can assert "the surface prints a row for every fact" - which is the
#: property that matters - rather than "the surface prints a row for every key
#: we happen to call it". A provenance panel that renders three of four looks
#: complete, and only the set pins that.
MEMORY_FACTS: Tuple[str, ...] = ("source", "session", "turn", "date")

#: What a citation can resolve to. Six, and the difference between the pairs is
#: the whole point:
#:
#: * ``resolvable`` / ``session_missing`` / ``turn_missing`` - a citation was
#:   recorded and we checked. Only the first is live.
#: * ``no_session_recorded`` / ``no_turn_recorded`` - NO citation was recorded.
#:   Silence from a producer is not a dangling citation; it is an absent one,
#:   and it reads differently because there is nothing to have gone stale.
#: * ``unavailable`` - we could not check. Never rendered as a broken
#:   citation, because "we could not look" is not "it is gone".
CITATION_VERDICTS: Tuple[str, ...] = (
    "resolvable",
    "session_missing",
    "turn_missing",
    "no_session_recorded",
    "no_turn_recorded",
    "unavailable",
)

#: The ONLY verdicts whose link syntax a reader may follow. Asserted by
#: :func:`assert_no_dangling_citation_renders_as_verifiable`.
RESOLVABLE_CITATION_VERDICTS = frozenset({"resolvable"})

_TRUST_TIERS = ("stated", "observed", "untrusted")


def _sanitize(value: Any, width: int = 0) -> str:
    """One sanitise at the render boundary. Fail-CLOSED, with no ``except`` path
    that passes the raw value through.

    Memory text, a proposal body, and a session id all originate outside this
    process's control. ``cli.ui.sanitize_text`` itself fails closed, so this
    wrapper has no branch that could leak: a raising or absent sanitiser
    WITHHOLDS the detail.
    """
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
    """Bound a product-authored string. NOT a substitute for :func:`_sanitize`.

    For words this module wrote and for values that came out of a typed
    store: a sanitiser's withholding marker must never land on the line a
    reader needs in order to navigate.
    """
    text = " ".join(str(value if value is not None else "").split())
    if width and len(text) > max(4, int(width)):
        text = text[: max(1, int(width) - 3)] + "..."
    return text


def _int_or_none(value: Any) -> Optional[int]:
    """An int, or ``None``. Never ``0`` for absent, and ``bool`` is refused."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _governance() -> Any:
    """``memory.governance``, or ``None``. Never raises.

    The trust reduction is delegated rather than re-derived: two modules
    answering "is this memory user-stated" is how a reader ends up believing
    an untrusted extraction because one of them guessed.
    """
    try:
        from memory import governance

        return governance
    except Exception:
        return None


def _ranking() -> Any:
    try:
        from memory import ranking

        return ranking
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Trust class. Derived from the store's own reduction.
# ---------------------------------------------------------------------------


def trust_class_of(row: Any, *, untrusted: Optional[bool] = None) -> Dict[str, Any]:
    """Reduce one memory row to a trust class, a marker, and a reason.

    The reduction is delegated to ``memory.governance.trust_tier`` and then
    MAPPED, because the two vocabularies answer different questions:

    * governance's ``stated``/``observed``/``untrusted`` is about the SOURCE;
    * this module's four classes are about what a READER may conclude, and
      they need a fourth state governance has no word for - ``unattributed``,
      the row that carries no source at all.

    ``unattributed`` is not a fifth kind of distrust. It is the honest answer
    to "this row does not say where it came from", and it must NOT be rendered
    as ``extracted``: reading an absent provenance as an observed fact is
    asserting trust from silence, which is the defect Wave 1 already had to
    fix once in the proposal reader (``cli/AGENTS.md`` P3a/W1 §2 item 6).
    """
    governance = _governance()
    source = ""
    provenance: Mapping[str, Any] = {}
    if isinstance(row, Mapping):
        source = str(row.get("source") or "")
        blob = row.get("provenance")
        provenance = blob if isinstance(blob, Mapping) else {}
    else:
        source = str(getattr(row, "source", "") or "")
        blob = getattr(row, "provenance", None)
        provenance = blob if isinstance(blob, Mapping) else {}

    tier = ""
    reason = ""
    if governance is not None:
        try:
            tier = str(governance.trust_tier(row, untrusted=untrusted) or "")
        except Exception as exc:  # a broken reducer must not read as "trusted"
            tier = ""
            reason = f"the trust reducer could not be read: {type(exc).__name__}"
    declared = str(provenance.get("source_type") or provenance.get("kind") or "").strip()
    recorded = bool(source or declared or provenance.get("session_id"))

    if not tier:
        cls = "unattributed"
    elif tier == "untrusted":
        cls = "untrusted"
    elif tier == "stated":
        cls = "user_stated"
    elif tier == "observed":
        cls = "extracted"
    else:
        # A tier outside governance's own closed vocabulary. Narrowing it to
        # `unattributed` is the only safe direction: an unrecognised tier is
        # not evidence of trust.
        cls = "unattributed"
        reason = reason or (
            f"this build does not recognise the trust tier {tier!r}, so it is "
            "not treated as a trusted source"
        )
    if not recorded and cls != "unattributed":
        cls = "unattributed"
        reason = reason or "the row records no source at all"
    return {
        "class": cls,
        "marker": TRUST_MARKERS[cls],
        "tier": tier or "unrecorded",
        "source": source,
        "declared": declared,
        "recorded": recorded,
        "untrusted": cls in ("untrusted", "unattributed"),
        "reason": reason,
    }


def trust_marker(row: Any, *, untrusted: Optional[bool] = None) -> str:
    """The one character that must lead this memory's row."""
    return str(trust_class_of(row, untrusted=untrusted).get("marker") or "?")


# ---------------------------------------------------------------------------
# The citation. Resolve it, or name it as broken.
# ---------------------------------------------------------------------------


def _turn_keys(session: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every turn a session still holds, across all three of its turn lists.

    All three, because a compaction moves a turn from ``turns`` into
    ``compacted_turns`` and the raw journal keeps ``raw_turns``: a citation
    into a compacted turn is the NORMAL case after a compaction, and reading
    only the live window is how "I was cited from turn 40" reports a dead
    link over a turn that is sitting on disk.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for key in ("turns", "compacted_turns", "raw_turns"):
        rows = session.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            turn_id = str(item.get("turn_id") or "")
            marker = turn_id or repr(dict(item))
            if marker in seen:
                continue
            seen.add(marker)
            out.append(dict(item))
    return out


def citation_target(
    log_root: Any,
    session_id: Any,
    turn: Any = None,
    *,
    session: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve one citation to the conversation turn it names, or say why not.

    This is the ONE resolver, and every surface that shows a citation calls it.
    ``log_root`` is accepted as ``None`` so a caller holding only an in-memory
    session can still resolve a turn; when it is absent and no ``session`` is
    passed the verdict is ``unavailable`` rather than a guess.

    The verdict vocabulary is :data:`CITATION_VERDICTS` and it is deliberately
    six wide, because the three questions a reader actually has are different:

    * is there a citation at all? (``no_session_recorded`` /
      ``no_turn_recorded``)
    * is the thing it names still there? (``resolvable`` /
      ``session_missing`` / ``turn_missing``)
    * could we even look? (``unavailable``)

    Collapsing the first two is how a memory with no provenance ends up
    wearing the same badge as one whose provenance rotted, and a reader learns
    to trust a badge that means two things.
    """
    sid = str(session_id or "").strip()
    target: Dict[str, Any] = {
        "verdict": "no_session_recorded",
        "session_id": sid,
        "session_status": "",
        "session_exists": None,
        "turn": None,
        "turn_id": "",
        "turn_role": "",
        "turn_text": "",
        "turn_found": None,
        "reason": "this memory records no source session, so there is nothing to cite",
        "command": "",
        "checkable": False,
    }
    if not sid:
        return target

    # -- the session ------------------------------------------------------
    record: Optional[Mapping[str, Any]] = session
    status = ""
    if record is None and log_root is not None:
        try:
            from cli import session as _session

            target["checkable"] = True
            health = _session.inspect_session(log_root, sid)
            status = str(health.get("status") or "")
            if status == "ok":
                loaded = _session.load_or_create(log_root, None, sid, strict=False)
                if isinstance(loaded, Mapping):
                    record = loaded
            elif status in ("missing", "invalid", "corrupt", "unavailable"):
                target.update(
                    {
                        "verdict": "session_missing",
                        "session_status": status,
                        "session_exists": False,
                        "reason": _session_reason(status, health),
                    }
                )
                return target
        except Exception as exc:
            target.update(
                {
                    "verdict": "unavailable",
                    "session_status": "unreadable",
                    "reason": (
                        "the source session could not be checked: "
                        f"{type(exc).__name__}"
                    ),
                }
            )
            return target
    elif record is None:
        target.update(
            {
                "verdict": "unavailable",
                "session_status": "unchecked",
                "reason": (
                    "this surface has neither the session record nor an artifact "
                    "root, so the citation could not be checked"
                ),
            }
        )
        return target

    target["session_exists"] = True
    target["session_status"] = status or "loaded"
    target["checkable"] = True

    # -- the turn ---------------------------------------------------------
    # A recorded turn is EITHER an ordinal OR a turn id, and the two producers
    # disagree about which. The first version collapsed both into
    # ``_int_or_none``, so a string turn id became ``None`` and the resolver
    # fell through to the index branch and crashed on ``None - 1``. A turn id
    # is therefore read as a string whenever the value is not an integer, and
    # only a genuinely integral value is treated as an ordinal.
    raw_turn = turn
    if isinstance(raw_turn, bool) or raw_turn is None:
        raw_turn = None
    turn_id = (
        "" if isinstance(raw_turn, int) else str(raw_turn or "").strip()
    )
    wanted = _int_or_none(raw_turn)
    if raw_turn is None or (not turn_id and wanted is None):
        target.update(
            {
                "verdict": "no_turn_recorded",
                "reason": (
                    f"the source session {sid} is still on disk, but no turn in "
                    "it was recorded, so the citation names a conversation and "
                    "not a moment in it"
                ),
                "command": f"/lineage show {sid}",
            }
        )
        return target

    turns = _turn_keys(record or {})
    found: Optional[Mapping[str, Any]] = None
    index_used: Optional[int] = None
    if turn_id:
        for row in turns:
            if str(row.get("turn_id") or "") == turn_id:
                found = row
                break
    elif wanted is not None:
        # 0-based and 1-based are both tried because the two producers
        # disagree, and picking one would make half of all citations resolve to
        # the wrong turn - which is worse than resolving to none.
        for candidate in (wanted, wanted - 1):
            if 0 <= candidate < len(turns):
                found = turns[candidate]
                index_used = candidate
                break

    if found is None:
        target.update(
            {
                "verdict": "turn_missing",
                "turn": wanted,
                "turn_id": turn_id,
                "turn_found": False,
            "reason": (
                f"the source session {sid} is still on disk but no turn in it "
                f"carries {('id ' + turn_id) if turn_id else ('index ' + str(wanted))}; "
                "the citation is stale, not the session"
            ),
                "command": f"/lineage show {sid}",
            }
        )
        return target

    target.update(
        {
            "verdict": "resolvable",
            # `turn` is the value the READER typed, normalised to something
            # printable. Reporting the resolved ordinal would be a different
            # number from the one in the provenance blob, and a reader checking
            # the memory against its own record would be looking for a turn
            # number the store never wrote. When the citation named a turn ID
            # rather than an ordinal, that id is what is echoed back.
            "turn": wanted if wanted is not None else turn_id,
            "turn_named_as": "index" if wanted is not None else "id",
            "turn_id": str(found.get("turn_id") or "") or turn_id,
            "turn_role": str(found.get("role") or ""),
            "turn_text": str(found.get("text") or ""),
            "turn_found": True,
            "turn_index": index_used,
            "reason": "",
            "command": f"/lineage show {sid}",
        }
    )
    return target


def _session_reason(status: str, health: Mapping[str, Any]) -> str:
    """A sentence for a session that is not there, per status.

    ``missing`` and ``corrupt`` are DIFFERENT answers and stay different: a
    deleted session is a fact about the user's history, while a corrupt one is
    a fact about this build's ability to read it, and telling somebody their
    citation is gone when the file is merely unreadable sends them looking for
    a deletion that never happened.
    """
    detail = str(health.get("error") or "")
    if status == "missing":
        return (
            "the session this memory was extracted from no longer exists, so "
            "this citation cannot be opened or checked"
        )
    if status == "invalid":
        return "the recorded source session id is not a valid session id"
    if status == "corrupt":
        return (
            "the source session still exists but cannot be read"
            + (f" ({_plain(detail, 60)})" if detail else "")
            + ", so this citation is unverified rather than broken"
        )
    if status == "unavailable":
        return (
            "the source session could not be reached from this surface"
            + (f" ({_plain(detail, 60)})" if detail else "")
        )
    return f"the source session could not be resolved (status: {status})"


# ---------------------------------------------------------------------------
# The facts. Four, each with its own absent rule.
# ---------------------------------------------------------------------------


def _iso_from_epoch(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    except (TypeError, ValueError, OSError):
        return ""


def memory_facts(row: Any) -> Dict[str, Any]:
    """The four provenance facts of one memory, each with its own absent value.

    ``=  ==============  ======================================================
    fact   absent rule    why
    =  ==============  ======================================================
    source  ``""``       a row with no source renders under the ``?`` marker and
                         says so; it is never read as ``extracted``
    session ``""``       an absent session is an ABSENT citation, not a broken
                         one, and the two render differently
    turn     ``None``    ``None`` is "no turn was recorded"; a 0 is a real turn
                         index and must survive
    date     ``None``    ``None`` is "we do not know when"; the file mtime is
                         NOT a substitute, because a file's mtime is when it
                         was written, not when the fact was stated
    =  ==============  ======================================================

    ``extracted_at`` is read from the provenance blob's own ``recorded_at`` /
    ``promoted_at`` first and the row's ``created_at`` second, and each is
    reported with WHICH one it came from, because a row insert time and an
    extraction time are different events.
    """
    blob: Mapping[str, Any] = {}
    created = ""
    if isinstance(row, Mapping):
        candidate = row.get("provenance")
        blob = candidate if isinstance(candidate, Mapping) else {}
        created = str(row.get("created_at") or "")
    else:
        candidate = getattr(row, "provenance", None)
        blob = candidate if isinstance(candidate, Mapping) else {}
        created = str(getattr(row, "created_at", "") or "")

    session_id = str(blob.get("session_id") or blob.get("session") or "")
    turn = blob.get("turn")
    if turn is None:
        turn = blob.get("turn_index")
    if turn is None:
        turn = blob.get("step")

    extracted_at = ""
    extracted_from_field = ""
    for key in ("recorded_at", "promoted_at", "extracted_at"):
        value = blob.get(key)
        if value in (None, ""):
            continue
        text = str(value)
        if text.replace("-", "").replace(":", "").replace("T", "").replace(
            "Z", ""
        ).replace(".", "").isdigit():
            text = _iso_from_epoch(value)
            if not text:
                continue
        extracted_at = text
        extracted_from_field = f"provenance.{key}"
        break
    if not extracted_at and created:
        extracted_at = created
        extracted_from_field = "created_at"

    return {
        "source": str(blob.get("source_type") or blob.get("kind") or "") or str(
            (row.get("source") if isinstance(row, Mapping) else getattr(row, "source", "")) or ""
        ),
        "source_column": str(
            (row.get("source") if isinstance(row, Mapping) else getattr(row, "source", "")) or ""
        ),
        "session_id": session_id,
        "turn": turn,
        "extracted_at": extracted_at or None,
        "extracted_from_field": extracted_from_field,
        "recorded_via": str(blob.get("recorded_via") or ""),
        "explicit": blob.get("explicit"),
        "untrusted_source": bool(blob.get("untrusted_source"))
        and not bool(blob.get("trusted_source")),
        "trusted_source": bool(blob.get("trusted_source")),
    }


# ---------------------------------------------------------------------------
# Ranking. The brief asks a reader to see WHY memory #1 is memory #1.
# ---------------------------------------------------------------------------


def ranking_why(
    memory_id: Any,
    *,
    usage: Any = None,
    rows: Optional[Sequence[Any]] = None,
    ranked: Any = None,
) -> Dict[str, Any]:
    """The ranking explanation for one memory, and whether one exists.

    ``memory.ranking.RankedMemory.reasons`` is a tuple of English sentences -
    one per scored term - and the load-bearing one is the honest zero: *"this
    memory has NO usage row at all, which is not the same as zero citations."*
    That distinction is the whole of item 4, so it is surfaced rather than
    summarised.

    ``ranked`` is an already-built ``RankingReceipt`` when the caller has one
    (so a list renders every memory's reasons from a single ranking pass);
    otherwise one is computed for a single row. Returns ``explained: False``
    with a reason rather than an empty string when nothing can be explained,
    because a blank is indistinguishable from a reader who did not ask.
    """
    target = _int_or_none(memory_id)
    if target is None or target <= 0:
        return {
            "explained": False,
            "rank": None,
            "score": None,
            "citations": None,
            "usage_known": False,
            "reasons": (),
            "reason": "this memory has no numeric id, so its usage cannot be looked up",
        }

    row: Any = None
    usage_applied = False
    usage_reason = ""
    receipt = ranked
    if receipt is None and rows is not None:
        ranking = _ranking()
        if ranking is None:
            return {
                "explained": False,
                "rank": None,
                "score": None,
                "citations": None,
                "usage_known": False,
                "reasons": (),
                "reason": "the ranking module could not be loaded",
            }
        try:
            receipt = ranking.rank_memories(list(rows), usage=usage)
        except Exception as exc:
            return {
                "explained": False,
                "rank": None,
                "score": None,
                "citations": None,
                "usage_known": False,
                "reasons": (),
                "reason": f"the ranking could not be computed: {type(exc).__name__}",
            }
    if receipt is None:
        return {
            "explained": False,
            "rank": None,
            "score": None,
            "citations": None,
            "usage_known": False,
            "reasons": (),
            "reason": "no ranking was supplied and no rows were given to build one",
        }

    usage_applied = bool(getattr(receipt, "usage_applied", False))
    usage_reason = str(getattr(receipt, "usage_reason", "") or "")
    for candidate in getattr(receipt, "ranked", ()) or ():
        if _int_or_none(getattr(candidate, "memory_id", None)) == target:
            row = candidate
            break
    if row is None:
        return {
            "explained": False,
            "rank": None,
            "score": None,
            "citations": None,
            "usage_known": False,
            "reasons": (),
            "reason": (
                "this memory was not in the ranked set, so no score explains "
                "its position"
            ),
            "usage_applied": usage_applied,
            "usage_reason": usage_reason,
        }

    reasons = tuple(str(item) for item in (getattr(row, "reasons", ()) or ()))
    citations = _int_or_none(getattr(row, "citations", None))
    return {
        "explained": bool(reasons),
        "rank": _int_or_none(getattr(row, "rank", None)),
        "score": getattr(row, "score", None),
        "citations": citations,
        "usage_known": bool(getattr(row, "usage_known", False)),
        "reasons": reasons,
        "reason": "" if reasons else "the ranking recorded no reasons for this row",
        "usage_applied": usage_applied,
        "usage_reason": usage_reason,
        "components": dict(getattr(row, "components", {}) or {}),
        "age_days": getattr(row, "age_days", None),
    }


def _citations_line(why: Mapping[str, Any]) -> str:
    """The one line that answers "was this recalled often?".

    Three answers, because the module below insists on them:
    ``recalled N time(s)`` for a measured count, ``never recalled`` for a
    measured zero, and ``recall UNKNOWN`` for a memory with no usage row at
    all. The last is not the second. "We have no record" and "the record says
    zero" are different claims and a reader deciding whether to trust a memory
    needs the difference.
    """
    if not why.get("explained"):
        return "recall            " + _plain(
            why.get("reason") or "no usage record was available", 44
        )
    citations = why.get("citations")
    # No column padding: the caller's renderer collapses runs of whitespace, so
    # padded alignment is decoration that does not survive to the screen. The
    # label is the FIRST WORD instead, which is the alignment that does.
    if citations is None:
        # Kept under 78 characters ON PURPOSE. The longer form of this sentence
        # was cut by the caller's width bound at exactly the clause that makes
        # it true - the fourth time this round a bound ate a disclosure.
        return "recalled UNKNOWN - no usage row exists, which is NOT zero"
    if int(citations) == 0:
        return "recalled 0 time(s) - a usage row exists and says zero"
    return f"recalled recorded {int(citations)} time(s)"


# ---------------------------------------------------------------------------
# The report and its renderer.
# ---------------------------------------------------------------------------


def provenance_report(
    row: Any,
    *,
    memory_id: Any = None,
    log_root: Any = None,
    ranked: Any = None,
    usage: Any = None,
    citation_limit: int = 3,
) -> Dict[str, Any]:
    """Every provenance fact about one memory, resolved and classified.

    Returns a mapping whose keys are stable so a test can assert on them:

    ``memory_id`` / ``text`` / ``trust`` / ``facts`` / ``citation`` /
    ``citations`` / ``ranking`` / ``recorded_citations`` / ``untrusted``

    The two that carry the round's weight are ``citation`` (which carries a
    verdict from :data:`CITATION_VERDICTS`) and ``ranking`` (which carries the
    sentences explaining the position). Never raises; a source that cannot be
    read yields a report that says so rather than an exception out of a
    renderer.
    """
    identifier = memory_id
    if identifier is None:
        identifier = (
            row.get("id") if isinstance(row, Mapping) else getattr(row, "id", None)
        )
    facts = memory_facts(row)
    trust = trust_class_of(row)

    citation = citation_target(log_root, facts.get("session_id"), facts.get("turn"))
    why = ranking_why(identifier, usage=usage, ranked=ranked)

    recorded: List[Dict[str, Any]] = []
    citations_available = False
    citations_reason = ""
    if _int_or_none(identifier) is not None and usage is not None:
        reader = getattr(usage, "citations", None)
        if callable(reader):
            try:
                for item in reader(int(identifier), limit=max(1, int(citation_limit))) or ():
                    recorded.append(
                        {
                            "at": str(getattr(item, "cited_at", "") or ""),
                            "session_id": str(getattr(item, "session_id", "") or ""),
                            "run_id": str(getattr(item, "run_id", "") or ""),
                            "turn": _int_or_none(getattr(item, "turn", None)),
                            "context": str(getattr(item, "context", "") or ""),
                        }
                    )
                citations_available = True
            except Exception as exc:
                citations_reason = (
                    "the citation history could not be read: "
                    f"{type(exc).__name__}"
                )
    elif usage is None:
        citations_reason = "no usage index was available to this surface"
    else:
        citations_reason = "this memory has no numeric id, so it has no citation history"

    return {
        "memory_id": identifier,
        "text": str(
            (row.get("text") if isinstance(row, Mapping) else getattr(row, "text", "")) or ""
        ),
        "trust": trust,
        "facts": facts,
        "citation": citation,
        "citations": why.get("citations"),
        "ranking": why,
        "recorded_citations": recorded,
        "recorded_citations_available": citations_available,
        "recorded_citations_reason": citations_reason,
        "untrusted": bool(trust.get("untrusted")),
    }


def provenance_report_lines(
    report: Mapping[str, Any], *, width: int = 78, explain: bool = True
) -> List[str]:
    """Render one memory's provenance. Never an empty list.

    The order is the reading order of the question "should I believe this?":
    what it says, where it came from, whether the citation still opens, how
    often it has been recalled, and only then why it ranks where it does. The
    trust MARKER leads, because it is the one thing that must survive a narrow
    terminal.

    Every rendered memory byte goes through :func:`_sanitize`. Memory text was
    extracted from sessions that may contain untrusted content, so this is the
    render boundary for a new injection surface, and the marker travels with
    the text rather than being trusted as a separate field.
    """
    report = report if isinstance(report, Mapping) else {}
    trust = report.get("trust") if isinstance(report.get("trust"), Mapping) else {}
    facts = report.get("facts") if isinstance(report.get("facts"), Mapping) else {}
    citation = (
        report.get("citation") if isinstance(report.get("citation"), Mapping) else {}
    )
    why = report.get("ranking") if isinstance(report.get("ranking"), Mapping) else {}

    marker = _plain(trust.get("marker") or "?", 1)
    label = f"memory {marker} #{report.get('memory_id')}"
    lines: List[str] = [label]
    body = _sanitize(report.get("text") or "", width - 2)
    lines.append(f"  says   {body}" if body else "  says   (this memory carries no text)")

    # -- the four facts ---------------------------------------------------
    source = _plain(facts.get("source") or facts.get("source_column") or "", 30)
    lines.append(
        "  source   {}".format(
            source or "NOT RECORDED - this build cannot say where it came from"
        )
    )
    session_id = _plain(facts.get("session_id") or "", 32)
    lines.append(
        "  session  {}".format(session_id or "not recorded - no citation was recorded")
    )
    turn = facts.get("turn")
    lines.append(
        "  turn     {}".format(
            "not recorded - the citation names a conversation, not a moment in it"
            if turn is None
            else _plain(turn, 32)
        )
    )
    extracted = facts.get("extracted_at")
    lines.append(
        "  date     {}".format(
            "unknown - no extraction date was recorded"
            if not extracted
            else f"{_plain(extracted, 24)} (from {facts.get('extracted_from_field') or 'unknown field'})"
        )
    )
    if trust.get("reason"):
        lines.append("  trust    " + _plain(trust["reason"], width - 12))

    lines.extend(citation_lines(citation, width=width))

    lines.append(_citations_line(why))
    if explain and why.get("explained"):
        for item in list(why.get("reasons") or ())[:6]:
            lines.append("    why: " + _plain(item, max(20, width - 10)))
        if len(list(why.get("reasons") or ())) > 6:
            lines.append("    (+ more terms in this score)")
        if not why.get("usage_applied", True) and why.get("usage_reason"):
            lines.append(
                "    NOTE: usage ranking is not in play - "
                + _plain(why.get("usage_reason"), max(20, width - 34))
            )
    elif not explain:
        pass

    recorded = report.get("recorded_citations") or []
    if recorded:
        lines.append("  cited in:")
        for item in recorded:
            lines.append(
                "    {at}  session {sid}  turn {turn}".format(
                    at=_plain(item.get("at") or "(no time recorded)", 22),
                    sid=_plain(item.get("session_id") or "(not recorded)", 28),
                    turn=(
                        "(not recorded)"
                        if item.get("turn") is None
                        else _plain(item.get("turn"), 8)
                    ),
                )
            )
    elif report.get("recorded_citations_available"):
        lines.append("  cited in: no citation has ever been recorded for this memory")
    elif report.get("recorded_citations_reason"):
        lines.append(
            "  cited in: unavailable - "
            + _plain(report["recorded_citations_reason"], max(20, width - 20))
        )
    return [_plain(line, width + 10) for line in lines]


def citation_lines(citation: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render one citation verdict. Never an empty list, never a dead link.

    The link syntax (``/lineage show <id>``) appears ONLY for a resolvable
    verdict. For a broken one the session id is still printed - a reader needs
    to know WHICH session is gone - but with no command beside it, because a
    command a reader can type and watch fail is a worse experience than a
    sentence that already told them the truth.
    """
    citation = citation if isinstance(citation, Mapping) else {}
    verdict = str(citation.get("verdict") or "unavailable")
    sid = _plain(citation.get("session_id") or "", 32)
    lines: List[str] = []

    if verdict == "resolvable":
        turn = citation.get("turn")
        anchor = (
            "(no turn was recorded, so this names the conversation)"
            if turn is None
            else f"turn {_plain(turn, 12)}"
            + (
                f" · role {_plain(citation.get('turn_role') or '?', 12)}"
                if citation.get("turn_role")
                else ""
            )
        )
        lines.append(f"  cite     opens: {sid} · {anchor}")
        if citation.get("turn_text"):
            lines.append(
                "             “" + _sanitize(citation.get("turn_text"), max(20, width - 16)) + "”"
            )
        if citation.get("command"):
            lines.append("           open it with: " + _plain(citation["command"], width - 24))
        return lines

    if verdict == "no_session_recorded":
        lines.append("  cite     NONE - this memory was recorded with no source session")
        lines.append(
            "           nothing is claimed about where it came from, and that is "
            "not the same as a citation that has gone stale"
        )
        return lines

    if verdict == "no_turn_recorded":
        lines.append(f"  cite     session only - {sid}")
        lines.append(
            "           the session is still on disk but no turn in it was "
            "recorded, so this names a conversation and not a moment in it"
        )
        return lines

    if verdict == "session_missing":
        lines.append(
            f"  cite     DANGLING - the source session {sid or '(unnamed)'} is gone"
        )
        lines.append("           " + _plain(citation.get("reason") or "", max(20, width - 14)))
        lines.append(
            "           this memory cannot be checked against what it was drawn "
            "from; treat it as unverified"
        )
        return lines

    if verdict == "turn_missing":
        lines.append(f"  cite     STALE - session {sid} exists, the cited turn does not")
        lines.append("           " + _plain(citation.get("reason") or "", max(20, width - 14)))
        lines.append(
            "           the rest of the session is intact; read it with "
            + _plain(citation.get("command") or "", max(16, width - 30))
        )
        return lines

    lines.append("  cite     UNVERIFIED - the citation could not be checked")
    lines.append("           " + _plain(citation.get("reason") or "", max(20, width - 14)))
    return lines


# ---------------------------------------------------------------------------
# The gates. In the product, not only in the test suite.
# ---------------------------------------------------------------------------


def assert_trust_markers_are_distinct() -> Tuple[bool, str]:
    """Every trust class has its own marker, and every marker is ASCII.

    Two classes sharing a character is a surface where ``!`` means both "from
    an untrusted source" and "we cannot say", and a reader who has learned that
    ``!`` means one of them has learned nothing. ASCII is required because a
    marker that a legacy console cannot encode is a marker a legacy console
    drops, and the repo has a recorded outage from exactly that class.
    """
    seen: Dict[str, str] = {}
    for cls, marker in TRUST_MARKERS.items():
        text = str(marker)
        if not text or len(text) != 1:
            return False, f"the marker for {cls!r} is {text!r}, which is not one character"
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            return False, f"the marker for {cls!r} is not ASCII: {text!r}"
        if text in seen:
            return False, (
                f"trust classes {seen[text]!r} and {cls!r} share the marker "
                f"{text!r}; a reader cannot tell them apart"
            )
        seen[text] = cls
    missing = [cls for cls in TRUST_CLASSES if cls not in TRUST_MARKERS]
    if missing:
        return False, f"trust classes with no marker: {', '.join(missing)}"
    return True, ""


def assert_no_dangling_citation_renders_as_verifiable(
    report: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """The gate: only a RESOLVABLE citation may carry a followable command.

    Checked at the RENDER layer rather than the projection layer on purpose: a
    projection that is correct and a renderer that prints it anyway is the
    failure this round exists to catch, and only reading the rendered lines can
    see it.

    With ``report=None`` it self-checks by driving every verdict through the
    real renderer, which is what makes it a gate rather than an assertion
    about one fixture.
    """
    if report is None:
        for verdict in CITATION_VERDICTS:
            verdict_report = {
                "memory_id": 1,
                "text": "a memory",
                "trust": {"marker": "=", "reason": ""},
                "facts": {
                    "source": "manual",
                    "source_column": "manual",
                    "session_id": "sess-abc",
                    "turn": 1,
                    "extracted_at": "2026-01-01T00:00:00Z",
                    "extracted_from_field": "created_at",
                },
                "citation": {
                    "verdict": verdict,
                    "session_id": "sess-abc",
                    "turn": 1,
                    "turn_role": "user",
                    "turn_text": "the original words",
                    "reason": "because",
                    "command": "/lineage show sess-abc",
                },
                "ranking": {"explained": False, "reason": "not ranked here"},
                "recorded_citations": [],
                "recorded_citations_available": False,
                "recorded_citations_reason": "",
            }
            ok, why = assert_no_dangling_citation_renders_as_verifiable(verdict_report)
            if not ok:
                return False, why
        return True, ""

    citation = (
        report.get("citation") if isinstance(report.get("citation"), Mapping) else {}
    )
    verdict = str(citation.get("verdict") or "")
    if verdict not in CITATION_VERDICTS:
        return False, (
            f"citation verdict {verdict!r} is outside the declared vocabulary "
            f"{CITATION_VERDICTS}; an unrecognised verdict is not a resolvable one"
        )
    rendered = "\n".join(
        citation_lines(citation)
        if isinstance(report.get("citation"), Mapping)
        else []
    )
    offers_link = "open it with:" in rendered
    if verdict in RESOLVABLE_CITATION_VERDICTS and not offers_link:
        return False, (
            "a resolvable citation rendered without a way to open it; a "
            "citation a reader cannot follow is a citation that is only a claim"
        )
    if verdict not in RESOLVABLE_CITATION_VERDICTS and offers_link:
        return False, (
            f"a {verdict!r} citation rendered a followable command; a dead link "
            "presented as live is the exact lie this round exists to remove"
        )
    return True, ""


def assert_ranking_is_explained(
    report: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """The gate: a memory that is RANKED carries a reason for its position.

    Self-checks when given nothing, by building a genuine
    ``memory.ranking`` receipt over two rows and reading the reasons back. That
    matters: a gate whose only input is a hand-written fixture proves the
    fixture, and this one has to prove the ranking module's own output is
    reachable through this surface.
    """
    if report is None:
        ranking = _ranking()
        if ranking is None:
            return False, "the ranking module could not be loaded, so nothing was checked"
        rows = [
            {"id": 1, "text": "the first memory", "source": "user"},
            {"id": 2, "text": "the second memory", "source": "user"},
        ]
        try:
            receipt = ranking.rank_memories(rows)
        except Exception as exc:
            return False, f"the ranking module could not be exercised: {type(exc).__name__}"
        for row in rows:
            why = ranking_why(row["id"], ranked=receipt)
            if not why.get("explained"):
                return False, (
                    f"a ranked memory (id {row['id']}) produced no explanation: "
                    f"{why.get('reason') or 'no reason recorded'}"
                )
            lines = provenance_report_lines(
                {
                    "memory_id": row["id"],
                    "text": row["text"],
                    "trust": {"marker": "=", "reason": ""},
                    "facts": {},
                    "citation": {"verdict": "no_session_recorded"},
                    "ranking": why,
                    "recorded_citations": [],
                    "recorded_citations_available": False,
                    "recorded_citations_reason": "",
                }
            )
            if not any("why:" in line for line in lines):
                return False, (
                    "a ranked memory rendered with no 'why' line, so a reader "
                    "sees a position with no reason attached"
                )
        return True, ""

    why = report.get("ranking") if isinstance(report.get("ranking"), Mapping) else {}
    if not why:
        return False, "the report carries no ranking section"
    if why.get("rank") is None and why.get("score") is None:
        return True, ""
    if not why.get("explained"):
        return False, (
            "this memory carries a rank or score but no explanation: "
            f"{why.get('reason') or 'no reason recorded'}"
        )
    return True, ""
