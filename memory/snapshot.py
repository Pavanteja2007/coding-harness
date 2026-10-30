"""Bounded, FROZEN memory injection -- the caps and the cache argument.

THE CACHE POINT, WHICH IS THE ACTUAL POINT
------------------------------------------
The corpus's bounded-memory design injects two small Markdown files as a
**frozen snapshot** so that "mid-session writes hit disk without invalidating
the prompt cache". That mechanism is the reason to do this rather than simply
"cap the memory block", and this module exists to make it a property.

The property is arithmetic. A provider prompt cache is keyed on the leading
messages of a request. ``runtime.prompt_cache.frozen_prefix()`` returns
``messages[:1]`` when the first message is a system message -- so exactly ONE
message forms the cacheable prefix. Therefore:

* memory injected INTO that first message, once, and never re-read, costs
  nothing when the store changes mid-session, because the bytes did not
  change;
* memory injected from LIVE file state costs the ENTIRE prefix on every write,
  because the prefix changed.

A mid-session ``memory_record`` write is a normal event on this agent, and the
whole extraction pipeline adds more of them. Under live injection every one of
them would be a cache invalidation. This module's job is to make sure none of
them is.

RECONCILING THE TWO CAPS (they are different questions)
------------------------------------------------------
There are TWO caps in this system and conflating them is how a system silently
truncates:

``STORED_TEXT_CHARS`` (:data:`memory.decision_store._MAX_TEXT_CHARS`, 16,384)
    A STORAGE bound. "How large may one row be on disk?" It is about disk,
    about the row fitting a bound that also bounds redaction cost and
    metadata normalisation.

``INJECTED_SNAPSHOT_CHARS`` (this module)
    a CONTEXT bound. "How large may what the MODEL SEES be?" It is about the
    request, about the model, about what competes with code context.

A row may legitimately be 16,384 characters of storage and still deserve a
fraction of one line of injected context. Answering "which is the cap?" with
one number is the error; the answer is "storage 16,384, injection 4,096, and
the reconciliation is that the injected snapshot is assembled UNDER a total
cap after per-row rendering, so one huge row cannot consume the window."
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from memory.ranking import RankingPolicy, RankingReceipt, UsageIndex, rank_memories

__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "INJECTED_SNAPSHOT_CHARS",
    "INJECTED_USER_CHARS",
    "MAX_RENDERED_ROW_CHARS",
    "STORED_TEXT_CHARS",
    "USER_TRUSTED_SOURCES",
    "MemorySnapshot",
    "SnapshotRow",
    "caps_table",
    "capture_snapshot",
    "render_snapshot",
]

# ---------------------------------------------------------------------------
# THE CAPS, named, measured, and justified.
#
# Every number below is derived from a measurement taken on this repository,
# not copied from the corpus system it is modelled on. Where the corpus number
# is deliberately NOT reused, the reason is recorded, because "we copied
# Hermes's 2,200" without a reason is how a cap becomes folklore.
# ---------------------------------------------------------------------------

#: The decision store's per-row STORAGE cap. Quoted, not redefined: it is
#: ``memory.decision_store._MAX_TEXT_CHARS`` and it belongs to the store. It
#: appears here so the two caps sit in one table and the difference between
#: them is legible; nothing in this module enforces it.
STORED_TEXT_CHARS = 16_384

#: Total characters of memory the model may SEE in one injected snapshot.
#:
#: DERIVATION, from measurements on this repository:
#:
#: 1. It must not starve code context. The compiled-code block this agent
#:    already injects is ``knowledge_block_max_chars`` = 24,000 chars
#:    (``harness/config.py``). The measured usable input budget is
#:    ``(context_window_tokens 32768 - context_reserved_output_tokens 4096) *
#:    context_compaction_fraction 0.6`` = 17,203 tokens, and at
#:    ``context_chars_per_token`` = 4 that is ~68,800 chars. At 4,096 chars
#:    memory takes **6.0% of the input budget** and **17% of the code-context
#:    block** -- enough to matter, small enough that a memory flood cannot
#:    displace the repository the model is trying to read.
#:
#: 2. It must hold a useful number of rows. The 6,317 real rows in this
#:    repository's own decision DB measure p50 = 67 chars, p90 = 72, max = 429.
#:    At the measured p90, 4,096 chars holds ~56 rows.
#:
#: 3. Therefore the cap is NOT the binding constraint -- the row LIMIT is. At
#:    the default 6 rows, the snapshot uses ~430 chars and the cap cannot fire.
#:    That is the honest shape of this number: it is a blast-radius bound, not
#:    a routine one, which is exactly why a truncation must be REPORTED
#:    (below) rather than applied quietly.
#:
#: WHY NOT HERMES'S 2,200. The corpus system injects 2,200 + 1,375 = 3,575
#: chars against ITS context budget. Ours is ~68,800 chars of input against a
#: 24,000-char code block, so the same absolute number would be a much larger
#: share of what actually competes with memory here. Copying the figure would
#: import a budget we do not have.
INJECTED_SNAPSHOT_CHARS = 4_096

#: Characters reserved for USER-STATED facts inside the snapshot.
#:
#: DERIVATION: the corpus system splits its budget 2,200 project / 1,375 user,
#: which is **38.4% user**. The RATIO is the design -- a user's own stated
#: preferences outrank facts the agent inferred about the project -- so the
#: ratio is reproduced (1,536 / 4,096 = **37.5%**) and the absolute number
#: follows ours. A memory that cites an untrusted source lands in neither
#: bucket's trust tier; see :data:`USER_TRUSTED_SOURCES`.
INJECTED_USER_CHARS = 1_536

#: Sources that count as USER-STATED for the split above. A memory whose
#: provenance names anything else is project context, however useful it is.
USER_TRUSTED_SOURCES: Tuple[str, ...] = ("user", "operator", "manual")

#: Bound on ONE rendered row inside the snapshot. A single 16,384-char stored
#: row must not be able to consume the whole 4,096-char window, which is the
#: whole reconciliation between the storage cap and the injection cap.
MAX_RENDERED_ROW_CHARS = 512

#: Default number of memories injected. Matches the existing
#: ``KnowledgeContext.recalled`` default and ``DEFAULTS["memory_query_limit"]``.
DEFAULT_MEMORY_LIMIT = 6

_CAP_TABLE: Tuple[Dict[str, Any], ...] = (
    {
        "name": "STORED_TEXT_CHARS",
        "value": STORED_TEXT_CHARS,
        "question": "how large may one memory ROW be on disk?",
        "owner": "memory.decision_store",
        "justification": (
            "the store's own per-row storage bound, quoted not redefined; it "
            "also bounds redaction and metadata-normalisation cost per row"
        ),
    },
    {
        "name": "MAX_RENDERED_ROW_CHARS",
        "value": MAX_RENDERED_ROW_CHARS,
        "question": "how large may one row be as rendered INTO the snapshot?",
        "owner": "memory.snapshot",
        "justification": (
            "the reconciliation between the two caps: a 16,384-char stored row "
            "must not be able to consume the whole 4,096-char window, so the "
            "rendered form is bounded independently and the bound is reported "
            "per row"
        ),
    },
    {
        "name": "INJECTED_USER_CHARS",
        "value": INJECTED_USER_CHARS,
        "question": "how many chars of USER-STATED memory may the model see?",
        "owner": "memory.snapshot",
        "justification": (
            "37.5% of the snapshot, reproducing the corpus system's 38.4% "
            "user/project ratio because the ratio is the design; the absolute "
            "figure follows our own 4,096-char budget rather than its 3,575"
        ),
    },
    {
        "name": "INJECTED_SNAPSHOT_CHARS",
        "value": INJECTED_SNAPSHOT_CHARS,
        "question": "how many chars of memory may the model see in total?",
        "owner": "memory.snapshot",
        "justification": (
            "6.0% of the measured ~68,800-char input budget and 17% of the "
            "24,000-char code-context block, and ~56 rows at the measured p90 "
            "row length; at the default 6 rows it cannot fire, so it is a "
            "blast-radius bound rather than a routine one. WHY NOT the corpus "
            "system's 2,200+1,375=3,575: those are sized against ITS context "
            "budget, and ours is ~68,800 chars of input against a 24,000-char "
            "code block, so the same absolute number would be a much larger "
            "share of what actually competes with memory here"
        ),
    },
    {
        "name": "DEFAULT_MEMORY_LIMIT",
        "value": DEFAULT_MEMORY_LIMIT,
        "question": "how many memories are injected?",
        "owner": "memory.snapshot",
        "justification": (
            "matches KnowledgeContext.recalled(limit=6) and the shipped "
            "memory_query_limit, so this module does not move a number another "
            "terminal already depends on"
        ),
    },
)


def caps_table() -> List[Dict[str, Any]]:
    """Return the cap table, for a report, a doc, and a test.

    Exposed as DATA rather than as prose so the justification travels with the
    number. A cap whose justification lives only in a docstring is a cap whose
    justification is lost the first time somebody edits the constant.
    """
    return [dict(row) for row in _CAP_TABLE]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _text_of(row: Any) -> str:
    """Return a row's text under either a mapping or an object shape."""
    if isinstance(row, MappingABC):
        for key in ("text", "body", "title"):
            if row.get(key):
                return str(row[key])
        return ""
    for key in ("text", "body", "title"):
        value = getattr(row, key, None)
        if value:
            return str(value)
    return ""


def _id_of(row: Any) -> Any:
    for key in ("id", "memory_id", "decision_id", "doc_id"):
        if isinstance(row, MappingABC):
            value = row.get(key)
        else:
            value = getattr(row, key, None)
        if value not in (None, ""):
            return value
    return None


def _field(row: Any, key: str, default: str = "") -> str:
    if isinstance(row, MappingABC):
        return str(row.get(key) or default)
    return str(getattr(row, key, default) or default)


def _provenance(row: Any) -> Mapping[str, Any]:
    raw = row.get("provenance") if isinstance(row, MappingABC) else getattr(
        row, "provenance", None
    )
    return raw if isinstance(raw, MappingABC) else {}


def _is_user_stated(row: Any) -> bool:
    """Whether a memory counts as user-stated for the trust-tier split.

    Precedence is deliberate. An explicit ``trusted_source`` marker in the
    provenance WINS over the channel it arrived on: the user can say "this fact
    came out of an issue tracker but I checked it and it is true", and that
    assertion is exactly the promotion this system is supposed to make possible.
    Without such a marker, only the three :data:`USER_TRUSTED_SOURCES` count.
    """
    provenance = _provenance(row)
    if provenance.get("trusted_source") is True:
        return True
    if provenance.get("untrusted_source") is True:
        return False
    source = _field(row, "source") or str(provenance.get("source") or "")
    return source.strip().casefold() in USER_TRUSTED_SOURCES


def _is_untrusted(row: Any) -> bool:
    provenance = _provenance(row)
    if provenance.get("untrusted_source") is True:
        return True
    if provenance.get("trusted_source") is True:
        return False
    if isinstance(row, MappingABC) and row.get("untrusted_source") is True:
        return True
    source = _field(row, "source") or str(provenance.get("source") or "")
    return source.strip().casefold() in {
        "issue",
        "repository_instructions",
        "web",
        "skill",
        "plugin",
        "mcp",
        "memory",
    }


@dataclass(frozen=True)
class SnapshotRow:
    """One rendered row inside a frozen snapshot."""

    memory_id: Any
    text: str
    category: str
    source: str
    rank: int
    score: float
    user_stated: bool
    untrusted_source: bool
    truncated: bool
    original_chars: int
    reasons: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "memory_id": self.memory_id,
            "text": self.text,
            "category": self.category,
            "source": self.source,
            "rank": self.rank,
            "score": self.score,
            "user_stated": self.user_stated,
            "untrusted_source": self.untrusted_source,
            "truncated": self.truncated,
            "original_chars": self.original_chars,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable, frozen view of memory for injection into a prompt.

    FROZEN is the load-bearing word. :meth:`text` is computed ONCE at capture
    and never re-derived, so a memory written mid-session -- by
    ``memory_record``, by the extraction pipeline, by another process, by the
    user in a second terminal -- does NOT change what is in the prompt window.
    The next snapshot is a different object with a different digest.

    ``digest`` is the SHA-256 of ``text``. It is published so a caller can put
    the memory block into a cached prompt prefix and prove the prefix is
    unchanged by asserting the digest before and after a write.
    """

    text: str
    digest: str
    captured_at: str
    rows: Tuple[SnapshotRow, ...]
    chars: int
    cap_chars: int
    truncated: bool
    truncated_rows: Tuple[SnapshotRow, ...]
    dropped_rows: Tuple[Any, ...]
    dropped_chars: int
    ranking: Optional[RankingReceipt] = None
    store_digest: str = ""
    session_id: str = ""

    @property
    def empty(self) -> bool:
        """True when the snapshot carries no memory at all."""
        return not self.rows

    @property
    def untrusted_rows(self) -> Tuple[SnapshotRow, ...]:
        """Rows whose provenance names an untrusted source."""
        return tuple(row for row in self.rows if row.untrusted_source)

    def stale_against(self, other: "MemorySnapshot") -> bool:
        """Whether ``other`` (a newer capture) differs from this one.

        The refresh boundary, stated as a comparison rather than as a promise:
        two snapshots over an unchanged store are byte-identical, and the first
        write that changes the store makes them differ.
        """
        return other.digest != self.digest

    def receipt(self) -> Dict[str, Any]:
        """Return the full injection receipt.

        Truncation is reported in four separate fields because "a cap that
        fired" has three distinguishable shapes and a single boolean would
        conflate them:
        ``rows_truncated`` (a row was clipped),
        ``rows_dropped`` (a row did not fit at all), and
        ``truncated`` (either happened).
        """
        return {
            "digest": self.digest,
            "captured_at": self.captured_at,
            "chars": self.chars,
            "cap_chars": self.cap_chars,
            "rows": len(self.rows),
            "truncated": self.truncated,
            "rows_truncated": len(self.truncated_rows),
            "rows_dropped": len(self.dropped_rows),
            "dropped_chars": self.dropped_chars,
            "dropped_memory_ids": [
                value if not hasattr(value, "memory_id") else value.memory_id
                for value in self.dropped_rows
            ],
            "truncated_memory_ids": [row.memory_id for row in self.truncated_rows],
            "user_stated_rows": sum(1 for row in self.rows if row.user_stated),
            "untrusted_rows": len(self.untrusted_rows),
            "store_digest": self.store_digest,
            "session_id": self.session_id,
            "frozen": True,
            "refresh_boundary": (
                "this text is immutable; a memory written after this capture is "
                "invisible to it and appears only in the NEXT capture"
            ),
            "caps": caps_table(),
            "ranking": (self.ranking.as_dict() if self.ranking is not None else None),
        }

    def as_dict(self) -> Dict[str, Any]:
        """Alias for :meth:`receipt`."""
        return self.receipt()


def _render_row(row: Any) -> Tuple[str, bool, int]:
    """Render one memory row to at most :data:`MAX_RENDERED_ROW_CHARS`.

    Returns ``(text, was_truncated, original_chars)``. The clip is REPORTED by
    the caller -- a row that reads as complete because its tail was silently
    removed is exactly the failure this repository's honesty rules are about.
    """
    body = _text_of(row).replace("\r\n", " ").replace("\n", " ").strip()
    original = len(body)
    marker = " …[row truncated]"
    if original <= MAX_RENDERED_ROW_CHARS:
        return body, False, original
    keep = max(0, MAX_RENDERED_ROW_CHARS - len(marker))
    return body[:keep] + marker, True, original


def capture_snapshot(
    store: Any,
    *,
    repo_path: Any = None,
    query: Any = "",
    limit: Optional[int] = None,
    usage: Optional[UsageIndex] = None,
    policy: Optional[RankingPolicy] = None,
    snapshot_chars: Optional[int] = None,
    user_chars: Optional[int] = None,
    session_id: Any = "",
    at: Optional[float] = None,
    report_ranking: bool = True,
) -> MemorySnapshot:
    """Capture ONE frozen snapshot of this repository's memory.

    This is the only function in the module that reads the store. Calling it
    again produces a NEW snapshot; it never mutates an existing one. That is
    the entire cache argument, expressed as an API shape: a caller that keeps
    the returned object has a prompt prefix that a mid-session write cannot
    invalidate.

    The snapshot is assembled in three passes, each with its own reported cap:

    1. retrieve + rank (the ranking receipt explains the order);
    2. render each row under :data:`MAX_RENDERED_ROW_CHARS`, counting what was
       clipped;
    3. admit rows under :data:`INJECTED_SNAPSHOT_CHARS` in total, with
       :data:`INJECTED_USER_CHARS` reserved for user-stated facts -- a
       user-stated row is admitted ahead of a project row, and an untrusted
       one only into what remains.

    A row that does not fit is DROPPED and named by id. It is not silently
    omitted, because a snapshot that reads as complete while hiding three
    memories is the exact dishonesty this project exists to prevent.
    """
    stamp = _now_iso() if at is None else time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(float(at))
    ) + "Z"
    total_cap = (
        INJECTED_SNAPSHOT_CHARS if snapshot_chars is None else int(snapshot_chars)
    )
    user_cap = INJECTED_USER_CHARS if user_chars is None else int(user_chars)
    total_cap = max(0, total_cap)
    user_cap = max(0, min(user_cap, total_cap))
    try:
        row_limit = max(1, int(limit)) if limit else DEFAULT_MEMORY_LIMIT
    except (TypeError, ValueError):
        row_limit = DEFAULT_MEMORY_LIMIT

    header = "## Project memory (frozen snapshot)"
    rows: Tuple[SnapshotRow, ...] = ()
    ranking: Optional[RankingReceipt] = None
    store_digest = ""

    if store is None:
        return MemorySnapshot(
            text="",
            digest=hashlib.sha256(b"").hexdigest(),
            captured_at=stamp,
            rows=(),
            chars=0,
            cap_chars=total_cap,
            truncated=False,
            truncated_rows=(),
            dropped_rows=(),
            dropped_chars=0,
            ranking=None,
            store_digest="",
            session_id=str(session_id or ""),
        )

    try:
        retrieved = store.search(str(query or ""), limit=row_limit, repo_path=repo_path)
    except TypeError:
        # A store whose `search` does not accept `repo_path` must degrade to
        # an unscoped read, never to an exception -- the same tolerance
        # mcp_server._find_duplicate applies.
        retrieved = store.search(str(query or ""), limit=row_limit)
    except Exception:
        retrieved = []

    ranking = rank_memories(
        list(retrieved or []),
        usage=usage,
        policy=policy,
        at=at,
        report_ranking=report_ranking,
    )
    ranked_rows = list(ranking.ranked)

    # A content digest of exactly what was selected, so "the store changed"
    # is a fact the receipt can state rather than something a reader infers
    # from a differing row count.
    store_digest = hashlib.sha256(
        "\x1f".join(f"{row.memory_id}:{row.score}" for row in ranked_rows).encode(
            "utf-8", "replace"
        )
    ).hexdigest()

    if usage is not None:
        usage.mark_ranked([row.memory_id for row in ranked_rows])

    body_parts: List[str] = []
    truncated_rows: List[SnapshotRow] = []
    dropped_rows: List[Any] = []
    dropped_chars = 0
    # The header is INSIDE the budget from the first row. Starting the counter
    # at zero let the header be added on top, so the budget was exceeded by the
    # header's length and the final defensive clip cut a row mid-sentence --
    # honest (it was marked) but it produced a half-row where a clean drop was
    # available and correct.
    spent = len(header) + 1
    spent_user = 0
    overflowed = False

    for entry in ranked_rows:
        rendered, was_truncated, original = _render_row(entry)
        user_stated = _is_user_stated(entry)
        untrusted = _is_untrusted(entry)
        snapshot_row = SnapshotRow(
            memory_id=entry.memory_id,
            text=rendered,
            category=entry.category,
            source=_field(entry, "source"),
            rank=entry.rank,
            score=entry.score,
            user_stated=user_stated,
            untrusted_source=untrusted,
            truncated=was_truncated,
            original_chars=original,
            reasons=entry.reasons,
        )
        if was_truncated:
            truncated_rows.append(snapshot_row)

        marker = f"- #{entry.memory_id} ({snapshot_row.category}) {rendered}"
        line_cost = len(marker) + 1
        if spent + line_cost > total_cap:
            dropped_rows.append(entry.memory_id)
            dropped_chars += original
            overflowed = True
            continue
        if user_stated and spent_user + line_cost > user_cap:
            # A user-stated row that would exceed its own tier's reservation is
            # NOT silently demoted into the project budget: it is dropped and
            # named, because quietly reclassifying the user's own statement is
            # how trust tiers stop meaning anything.
            dropped_rows.append(entry.memory_id)
            dropped_chars += original
            overflowed = True
            continue
        body_parts.append(marker)
        spent += line_cost
        if user_stated:
            spent_user += line_cost
        rows = (*rows, snapshot_row)

    text = ""
    if body_parts:
        text = header + "\n" + "\n".join(body_parts)
        if overflowed:
            text += (
                f"\n- …[{len(dropped_rows)} memory/memories did not fit the "
                f"{total_cap}-char snapshot and were dropped; their ids are in "
                "the injection receipt]"
            )
    chars = len(text)
    if chars > total_cap:
        # The header plus a full window can exceed the cap by the header's
        # length. Clip the WHOLE rendered text -- and the marker must still say
        # that rows were dropped, because a model-facing block that was clipped
        # at the final pass and does not say so is indistinguishable from a
        # complete one. The gate's `r8_truncation_reported` rung caught exactly
        # that: the receipt named the dropped rows while the text did not.
        marker = (
            f"\n…[snapshot truncated to {total_cap} chars; "
            f"{len(dropped_rows)} memory/memories did not fit]"
        )
        text = text[: max(0, total_cap - len(marker))] + marker
        chars = len(text)
        overflowed = True

    return MemorySnapshot(
        text=text,
        digest=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        captured_at=stamp,
        rows=rows,
        chars=chars,
        cap_chars=total_cap,
        truncated=bool(overflowed),
        truncated_rows=tuple(truncated_rows),
        dropped_rows=tuple(dropped_rows),
        dropped_chars=dropped_chars,
        ranking=ranking,
        store_digest=store_digest,
        session_id=str(session_id or ""),
    )


def render_snapshot(snapshot: MemorySnapshot, *, title: Optional[str] = None) -> str:
    """Render the model-facing block for a frozen snapshot.

    Returns ``""`` for an empty snapshot -- an absent memory block is correct
    behaviour, and rendering "no memory" as a heading would spend tokens to say
    nothing.
    """
    if snapshot is None or not snapshot.text:
        return ""
    text = snapshot.text
    if title:
        lines = text.splitlines()
        if lines and lines[0].startswith("## "):
            lines[0] = f"## {title}"
            text = "\n".join(lines)
    return text
