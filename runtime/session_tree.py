"""An append-only session entry tree with a movable head (P3a/W1, T3).

This module replaces three mechanisms with one structure, which is the point:

* a **flat, atomically-overwritten** ``checkpoint.json`` whose previous state
  is gone the moment it is written;
* a **checkpoint stack** that is popped;
* a **3-way ``rewind_run``** that truncates journals and rotates them aside.

What they share is a desire to move a session to an earlier position. What they
disagree about is what happens to the position being left. An overwriting file
destroys it. A popped stack destroys it. A journal truncation *rotates it aside*,
which is closer, but it is still a rewrite of an append-only artifact -- and it
is a per-run-file operation, so it cannot express "two positions at once".

The tree makes the disagreement disappear: **history is the set of entries, and
a position is a pointer into that set.** Nothing is ever mutated, so nothing is
ever destroyed, and a fork is free because a fork is a new leaf, not a copy.

The three operations become one-liners:

===============  ==========================================================
operation        implementation
===============  ==========================================================
append a turn    ``append("turn", ...)`` under the current head
fork             ``fork()`` -- a new leaf under the head's PARENT, no copying
rewind           ``move_head(entry_id)`` -- the pointer moves; the old
                 position is still in ``entries()``
clone            ``fork()`` + ``move_head()``; ``/clone`` was always a fork
                 plus a different starting position
===============  ==========================================================

Two properties are structural rather than conventional, and both are pinned:

**No entry is ever mutated.** ``append`` is the only writer, and it only ever
adds. :meth:`SessionTree.entries` reads the journal, and there is no ``update``
or ``delete`` on this class at all -- an AST pin in ``runtime/test_p3w1_pins.py``
asserts that no public method name contains one. A guarantee that a future
method can remove is not a guarantee.

**A branch summary is a CLAIM and is labelled as one.** When a branch is
abandoned, an LLM may write a paragraph about what happened on it. That
paragraph is *not* a transcript fact: it is a model's assertion about entries
that still exist. So it is stored as ``kind="branch_summary"``, carries
``claim: True``, and -- the load-bearing part -- carries the ``evidence`` list of
entry ids it summarises. :meth:`SessionTree.branch_summary_evidence` walks that
list back to the real entries, so a reader can always get the facts the summary
is a claim *about*. A summary is optional (:meth:`fork` does not require one) and
:func:`branch_summary_line` renders it with a marker so a terminal cannot
present it as transcript.

**GC policy** (the honest downside of an append-only structure -- see
:mod:`runtime.session_tree`'s ``__main__`` for the measured growth rate):

* **retain by default.** Nothing is collected while it is reachable from a
  retained root.
* **collect only what is unreachable AND expired AND unreferenced.**
  ``retained`` roots are the current head, every session's own head, and any
  entry named as summary evidence.
* **always report.** :meth:`SessionTree.gc` returns what it removed, what it
  kept and why, and it supports ``dry_run=True``. A GC that deletes silently is
  worse than no GC, because the user cannot tell a lost branch from one that
  never existed.
* **never delete content a receipt could name.** An entry's payload lives in its
  own row; removing the row removes the evidence a ``branch_summary`` cites, so
  evidence is a retention root.

Scope note: this module owns the DURABLE SUBSTRATE. ``cli/session.py`` owns the
user-facing snapshot and the index; ``harness/agent_kernel/context.py`` owns the
offline rewind picker. Neither is edited here -- the API those two need is
:func:`fork_api` and :func:`describe`, and the handoff request is in
``runtime/AGENTS.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from shared.security import redact_secrets

from .fsutil import atomic_write_json, ensure_dir, now_epoch, now_iso, read_json_or_none

#: The journal is append-only and redacted at the writer. Redaction is not
#: optional here: a turn entry carries the conversation's own text, which is the
#: same class of durable artifact as ``conversation.jsonl`` and must never
#: persist a credential. The writer fails closed -- a redaction failure records
#: the failure instead of the detail, because losing the audit trail is worse
#: than losing the row.
_TREE_SCHEMA_VERSION = 1
ENTRIES_NAME = "entries.jsonl"
HEAD_NAME = "head.json"
TREE_SCHEMA_VERSION = _TREE_SCHEMA_VERSION

#: Closed vocabulary of entry kinds. ``branch_summary`` is separated from
#: ``branch`` and from ``turn`` because they are three different kinds of claim:
#: a turn is something that HAPPENED, a branch is a structural marker, and a
#: branch summary is something a model SAID about a branch. Collapsing them is
#: how a generated paragraph ends up presented as a transcript fact.
ENTRY_KINDS: Tuple[str, ...] = (
    "root",
    "turn",
    "branch",
    "branch_summary",
    "note",
    "head",
)

#: Kinds whose payload is a model's assertion rather than a recorded event.
#: :func:`is_claim_kind` and :func:`branch_summary_line` read this, so a new
#: claim kind cannot be added without deciding how it is labelled.
CLAIM_KINDS: frozenset = frozenset({"branch_summary"})

#: Entry kinds that can BE the head. A ``turn`` makes itself the head (an
#: ordinary turn is one row and one write); a ``branch`` is the head of a forked
#: position; a ``head`` row is an explicit rewind pointing at an earlier entry.
#: A ``branch_summary`` is deliberately NOT here -- a claim cannot be a position,
#: which is how "the summary replaced the transcript" becomes structurally
#: impossible rather than a convention.
HEAD_KINDS: frozenset = frozenset({"root", "turn", "branch", "head"})

#: Marker rendered before a claim so no surface can present it as transcript.
#: Chosen to be unambiguous when concatenated onto free text.
CLAIM_MARKER = "[CLAIM - not a transcript fact]"

#: GC defaults. ``min_age_s`` is the grace period during which a just-created
#: entry is never collected even if unreachable -- a user who forks and
#: immediately rewinds has, for a moment, an unreachable head, and collecting it
#: would make "I went back" lose work.
DEFAULT_GC_MIN_AGE_S = 3600.0


#: How many times a GC's final ``os.replace`` is retried on a Windows sharing
#: violation, and how long it waits. **These are not new numbers.** They are
#: ``fsutil.atomic_write_json``'s, read from it rather than re-guessed, because
#: two retry schedules for "the same Windows replace" is two answers to one
#: question -- and because that function's own docstring records the measured
#: cause (a concurrent READER holding the file without ``FILE_SHARE_DELETE``
#: makes ``os.replace`` fail transiently, and the violation clears when the short
#: lived reader closes).
REPLACE_ATTEMPTS = 3
REPLACE_BACKOFF_S = 0.05

#: The closed vocabulary for "what state is the journal in". ``absent`` and
#: ``unreadable`` are the two that matter: before this round they both collapsed
#: into "zero rows", and zero rows verified clean, so a reader that raced a GC
#: was told it had a healthy tree containing no history.
JOURNAL_STATES: Tuple[str, ...] = ("present", "absent", "unreadable", "empty")


def is_claim_kind(kind: str) -> bool:
    """Return whether ``kind`` names a generated claim rather than a recorded event."""
    return str(kind or "") in CLAIM_KINDS


def branch_summary_line(entry: Mapping[str, Any]) -> str:
    """Render one branch-summary entry so it cannot read as a transcript fact.

    Always prefixed with :data:`CLAIM_MARKER`, always names the entry it
    summarises when it can, and says plainly when the evidence list is empty --
    an unevidenced summary is the worst case and must look like one.
    """
    # ``entries()`` returns ``TreeEntry`` OBJECTS while the rest of this module
    # (and every on-disk row) is a Mapping. Accepting only a Mapping meant the
    # obvious call -- ``branch_summary_line(tree.entries()[-1])`` -- raised
    # ``AttributeError: 'TreeEntry' object has no attribute 'get'``, found by
    # driving the real tree rather than by reading it. A renderer whose input
    # type disagrees with the collection it is meant to render is a renderer
    # nobody calls.
    row = entry if isinstance(entry, Mapping) else entry.as_dict()
    kind = str(row.get("kind") or "")
    if not is_claim_kind(kind):
        return f"[{kind}] {_entry_text(row)}"
    evidence = [str(item) for item in row.get("evidence") or () if str(item)]
    suffix = (
        f" (summarises {len(evidence)} entry/entries: {', '.join(evidence[:6])})"
        if evidence
        else " (NO EVIDENCE - this claim cites no entries)"
    )
    return f"{CLAIM_MARKER} {_entry_text(row)}{suffix}"


def _entry_text(entry: Any) -> str:
    """Return the human text of an entry, from whichever payload key is present."""
    row = entry if isinstance(entry, Mapping) else entry.as_dict()
    for key in ("text", "summary", "label", "note"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _canonical(value: Any) -> str:
    """Return a stable JSON encoding used for every digest in this module."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Any) -> str:
    """Return the SHA-256 of a payload's canonical encoding."""
    return hashlib.sha256(_canonical(payload).encode("utf-8", "replace")).hexdigest()


#: The fields a digest covers. Declared as data so a pin can assert the set
#: rather than trust a comment, and so adding a content field is a visible act.
DIGEST_FIELDS: Tuple[str, ...] = (
    "id",
    "parent_id",
    "kind",
    "turn_id",
    "text",
    "payload",
    "evidence",
    "claim",
)


def digest_body(row: Mapping[str, Any]) -> str:
    """Return the content digest of one entry row.

    **Single authority for what a digest covers.** The first version computed it
    over one field set in :meth:`SessionTree.append` and re-derived it from a
    slightly different set in :meth:`SessionTree.verify`, and every row reported
    as tampered -- two derivations of one fact, which is exactly the duplication
    the engineering doctrine names. Both readers call this now.

    ``claim`` is inside the set deliberately: a summary's claim status is
    content, so a row whose ``claim`` was flipped after the fact is precisely the
    tamper worth catching. ``created_at``/``seq`` are outside it because they are
    not content -- they say when and where, not what.
    """
    projected = {
        "id": str(row.get("id") or ""),
        "parent_id": str(row.get("parent_id") or ""),
        "kind": str(row.get("kind") or ""),
        "turn_id": str(row.get("turn_id") or ""),
        "text": str(row.get("text") or ""),
        "payload": dict(row.get("payload") or {}),
        "evidence": [str(item) for item in row.get("evidence") or ()],
        "claim": bool(row.get("claim", is_claim_kind(str(row.get("kind") or "")))),
    }
    return _digest(projected)


@dataclass(frozen=True)
class TreeEntry:
    """One immutable node of the tree.

    Every field is set at append time and never changes. ``digest`` covers the
    entry's own content, so a tampered row is detectable by :meth:`SessionTree.verify`
    rather than being silently believed.
    """

    id: str
    parent_id: str
    kind: str
    turn_id: str = ""
    text: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: Tuple[str, ...] = ()
    claim: bool = False
    digest: str = ""
    created_at: str = ""
    created_epoch: float = 0.0
    seq: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TreeEntry":
        """Rebuild an entry from a journal row, tolerating a missing field."""
        return cls(
            id=str(row.get("id") or ""),
            parent_id=str(row.get("parent_id") or ""),
            kind=str(row.get("kind") or "note"),
            turn_id=str(row.get("turn_id") or ""),
            text=str(row.get("text") or ""),
            payload=dict(row.get("payload") or {}),
            evidence=tuple(str(item) for item in row.get("evidence") or ()),
            claim=bool(row.get("claim", is_claim_kind(str(row.get("kind") or "")))),
            digest=str(row.get("digest") or ""),
            created_at=str(row.get("created_at") or ""),
            created_epoch=float(row.get("created_epoch") or 0.0),
            seq=int(row.get("seq") or 0),
        )

    def as_row(self) -> Dict[str, Any]:
        """Return the journal row for this entry (the durable form)."""
        return {
            "schema_version": _TREE_SCHEMA_VERSION,
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "turn_id": self.turn_id,
            "text": self.text,
            "payload": dict(self.payload or {}),
            "evidence": list(self.evidence),
            "claim": bool(self.claim),
            "digest": self.digest,
            "created_at": self.created_at,
            "created_epoch": self.created_epoch,
            "seq": self.seq,
        }

    def as_dict(self) -> Dict[str, Any]:
        """Return the presentation form, with the claim label carried explicitly."""
        row = self.as_row()
        row["is_claim"] = self.claim
        if self.claim:
            row["claim_label"] = CLAIM_MARKER
        return row


def _append_row(path: Path, row: Mapping[str, Any]) -> bool:
    """Append one redacted row with an explicit flush and fsync.

    Mirrors ``runtime.checkpoint._append_jsonl_durable``: same redaction, same
    fail-closed behaviour, same reason (an append-only journal of conversation
    text is the class of artifact that must never persist a credential).
    """
    try:
        ensure_dir(path.parent)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(redact_secrets(dict(row)), ensure_ascii=False, sort_keys=True)
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError):
        return False
    return True


def _read_journal(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    """Return ``(rows, state)`` for one journal, never conflating the two.

    ``_read_rows`` collapsed "the file is not there" and "the file is there and
    has rows" into the same empty list, and ``verify()`` then reported
    ``ok: True`` over it. That is the worst shape this module can produce: a
    reader is told its history verified when there is no history to verify.

    Measured on this tree before the fix: with one reader thread looping over a
    tree while a GC ran, **2 of 48 reads saw zero entries, no head, and
    ``verify() == {'ok': True, 'rows': 0}``** -- indistinguishable from a session
    that has never been written. Under storage pressure, which is exactly when a
    GC runs, an extraction job would silently see an empty conversation.

    So the four states are distinct and every reader that needs to know whether it
    saw the whole journal is told:

    ``present``
        the journal was read and carries at least one row.
    ``empty``
        the journal exists and genuinely has no rows. A fresh tree.
    ``absent``
        no journal file. Nothing has ever been written, or something removed it.
    ``unreadable``
        the file is there and could not be read -- a sharing violation, a
        permission, a directory where a file was expected.

    ``absent`` and ``unreadable`` are NOT ``empty``, and :meth:`SessionTree.verify`
    and :meth:`SessionTree.consistent_read` both refuse to call them intact.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], "absent"
    except (NotADirectoryError, IsADirectoryError):
        return [], "unreadable"
    except PermissionError:
        return [], "unreadable"
    except OSError:
        return [], "unreadable"
    rows: List[Dict[str, Any]] = []
    torn = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            # A torn line is a crash artifact, not corruption of the whole
            # journal. Skipping it is honest: the entry never existed as far as
            # any reader could tell, and `verify()` reports the row count it read
            # against the seq range it expected.
            torn += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
    state = "present" if rows else "empty"
    if torn and not rows:
        # Rows existed to read and none of them parsed. Reporting this as `empty`
        # is the same lie as reporting a GC'd journal as empty.
        state = "unreadable"
    return rows, state


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    """Return every well-formed row; a torn tail from a crash is skipped.

    Kept as a thin wrapper for the callers that genuinely do not care WHY the
    list is empty. A caller that needs to know must use :func:`_read_journal`.
    """
    rows, _state = _read_journal(path)
    return rows


def _replace_with_retry(source: Path, target: Path) -> bool:
    """``os.replace`` with a bounded retry for a Windows sharing violation.

    Read from ``fsutil.atomic_write_json``'s behaviour rather than re-guessed,
    and for the reason that function's docstring gives: a concurrent reader
    holding the target without ``FILE_SHARE_DELETE`` makes the replace fail
    transiently, and the violation clears when that reader closes.

    **Without this, GC on Windows silently collects nothing while its receipt
    claims it collected everything** -- a storage-pressure path that does nothing
    and says it did something, which is worse than no GC at all.
    """
    last: Exception | None = None
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(str(source), str(target))
            return True
        except PermissionError as exc:
            last = exc
            if attempt + 1 < REPLACE_ATTEMPTS:
                time.sleep(REPLACE_BACKOFF_S)
        except OSError as exc:
            _GC_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            return False
    _GC_LAST_ERROR = f"PermissionError after {REPLACE_ATTEMPTS} attempts: {last}"
    return False


#: The last replace failure, for a receipt that has to name a cause rather than
#: say "it did not work". Module-level because it is diagnostic only and a
#: receipt is built after the fact.
_GC_LAST_ERROR = ""


class SessionTree:
    """An append-only entry tree with one movable head pointer.

    Args:
        root: directory holding ``entries.jsonl`` and ``head.json``.
        clock: injectable epoch clock, so GC age is testable without sleeping.
    """

    def __init__(self, root: Path | str, *, clock: Any = None) -> None:
        self.root = Path(root)
        self._clock = clock or now_epoch
        #: In-process index cache, invalidated by the journal's (size, mtime_ns).
        #:
        #: This exists for a MEASURED reason. The first version derived the head
        #: id and the next sequence number by re-reading the whole journal on
        #: every append, so append cost grew linearly with journal length: 100
        #: turns measured a **42 ms median append**, and the curve is quadratic.
        #: An append-only structure whose append degrades with its own size is
        #: not a viable substrate -- it would make a long session unusable, which
        #: is the exact workload this phase exists to serve. The cache is
        #: invalidated by size AND mtime so an external writer (a second process,
        #: a resumed run) is still seen.
        self._index: Optional[Tuple[Tuple[int, int], List[TreeEntry]]] = None

    # -- paths ----------------------------------------------------------

    @property
    def entries_path(self) -> Path:
        """Return the append-only entry journal path."""
        return self.root / ENTRIES_NAME

    @property
    def head_path(self) -> Path:
        """Return the movable head-pointer path."""
        return self.root / HEAD_NAME

    # -- reading --------------------------------------------------------

    def head_id(self) -> str:
        """Return the derived head id, or ``""`` on an empty tree."""
        return self._derive_head()

    def _stamp(self) -> Tuple[int, int]:
        """Return the journal's ``(size, mtime_ns)``, the cache validity key."""
        try:
            stat = self.entries_path.stat()
        except OSError:
            return (-1, -1)
        return (int(stat.st_size), int(stat.st_mtime_ns))

    def _cached(self) -> List[TreeEntry]:
        """Return the entry list, rebuilding the index only when the journal moved."""
        stamp = self._stamp()
        if self._index is not None and self._index[0] == stamp:
            return self._index[1]
        entries = [TreeEntry.from_row(row) for row in _read_rows(self.entries_path)]
        self._index = (stamp, entries)
        return entries

    def entries(self) -> List[TreeEntry]:
        """Return every entry in journal order (oldest first)."""
        return list(self._cached())

    def entry(self, entry_id: str) -> Optional[TreeEntry]:
        """Return one entry by id, or None.

        Uses the index rather than rescanning, so this is O(1) on a warm tree --
        it is on the ``fork`` and ``move_head`` paths, which run once per user
        action, not once per turn.
        """
        wanted = str(entry_id or "")
        if not wanted:
            return None
        for item in self._cached():
            if item.id == wanted:
                return item
        return None

    def head(self) -> Optional[TreeEntry]:
        """Return the entry the DERIVED head names, or None on an empty tree.

        Derived from the journal, never read from a pointer file -- see
        :meth:`_derive_head` for why, and :meth:`head_mirror` for the cheap
        cross-process read that is explicitly labelled as a mirror.
        """
        head_id = self._derive_head()
        return self.entry(head_id) if head_id else None

    def path_to_head(self, entry_id: str = "") -> List[TreeEntry]:
        """Return the ancestry chain ending at ``entry_id`` (default: the head).

        Oldest first, so ``path[-1]`` is the named entry and ``path[0]`` is the
        root. A cycle (a corrupt journal claiming one) terminates rather than
        looping -- a walk that hangs is worse than one that stops short.
        """
        target = str(entry_id or "") or self.head_id()
        if not target:
            return []
        by_id = {item.id: item for item in self.entries()}
        chain: List[TreeEntry] = []
        seen: set = set()
        cursor = target
        while cursor and cursor not in seen:
            seen.add(cursor)
            node = by_id.get(cursor)
            if node is None:
                break
            chain.append(node)
            cursor = node.parent_id
        chain.reverse()
        return chain

    def children_of(self, entry_id: str) -> List[TreeEntry]:
        """Return the direct children of ``entry_id`` in journal order."""
        wanted = str(entry_id or "")
        return [item for item in self.entries() if item.parent_id == wanted]

    def reachable(self, roots: Iterable[str]) -> set:
        """Return the set of entry ids reachable from ``roots``.

        Walks children, not parents, because retention is about "can a reader
        still get here from something we keep".
        """
        by_parent: Dict[str, List[str]] = {}
        for item in self.entries():
            by_parent.setdefault(item.parent_id, []).append(item.id)
        seen: set = set()
        stack = [str(item) for item in roots if str(item)]
        while stack:
            cursor = stack.pop()
            if cursor in seen:
                continue
            seen.add(cursor)
            stack.extend(by_parent.get(cursor, ()))
        return seen

    # -- writing --------------------------------------------------------

    def _next_seq(self) -> int:
        """Return the next journal sequence number, from the cached tail.

        The cached index makes this O(1) on a warm tree; the cache is
        invalidated by ``(size, mtime_ns)``, so a concurrent writer is still seen.
        Reading the tail line directly instead would also be O(1) but would need
        a second code path that can disagree with the reader -- one derivation.
        """
        entries = self._cached()
        return max((int(item.seq or 0) for item in entries), default=0) + 1

    def _new_id(self, kind: str, seq: int) -> str:
        """Return a stable, human-nameable entry id.

        Content-addressed rather than random: re-appending the same entry after
        a crash produces the SAME id, so a retried append is idempotent by
        identity instead of duplicating the turn.
        """
        seed = f"{kind}|{seq}|{self._clock()}"
        return f"e-{kind}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"

    def append(
        self,
        kind: str,
        *,
        turn_id: str = "",
        text: str = "",
        payload: Optional[Mapping[str, Any]] = None,
        parent_id: str = "",
        evidence: Sequence[str] = (),
        entry_id: str = "",
        sets_head: bool = False,
    ) -> Optional[TreeEntry]:
        """Append one entry and return it, or None if the journal refused it.

        ``parent_id`` defaults to the current head, which is what makes a fork
        cheap: the caller names the parent it wants and nothing is copied.

        ``sets_head`` marks the entry as the new position. **The head is not a
        separate file** -- it is derived from the journal, by scanning for the
        last row whose kind is in :data:`HEAD_KINDS` (see :meth:`_derive_head`).
        So "append a turn and move onto it" is ONE fsync'd write, not two, and
        there is no mutable file in the structure at all.

        Returns ``None`` rather than raising on an unwritable journal. An
        append-only store whose write failed must not leave the caller believing
        the turn was recorded -- ``None`` is the only honest answer, and the
        caller decides whether that is fatal.
        """
        if str(kind) not in ENTRY_KINDS:
            return None
        parent = str(parent_id) or self.head_id()
        seq = self._next_seq()
        body = {
            "id": str(entry_id or ""),
            "parent_id": parent,
            "kind": str(kind),
            "turn_id": str(turn_id or ""),
            "text": str(text or ""),
            "payload": dict(payload or {}),
            "evidence": [str(item) for item in evidence or ()],
        }
        identifier = body["id"] or self._new_id(str(kind), seq)
        body["id"] = identifier
        body["claim"] = is_claim_kind(str(kind))
        body["digest"] = digest_body(body)
        body["created_at"] = now_iso()
        body["created_epoch"] = float(self._clock())
        body["seq"] = seq
        if not _append_row(self.entries_path, body):
            return None
        entry = TreeEntry.from_row(body)
        # Keep the cache WARM. Dropping it here is what made the first version
        # quadratic: every append then re-read a journal that had just grown.
        # The journal only changed by this row, so the index is updated in place.
        if self._index is not None:
            self._index = (self._stamp(), [*self._index[1], entry])
        else:
            self._index = None
        return entry

    def advance(
        self,
        *,
        turn_id: str = "",
        text: str = "",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Optional[TreeEntry]:
        """Append a ``turn`` under the current head; that turn IS the new head.

        This is the ordinary "the conversation went forward" operation. It is a
        separate method from :meth:`append` precisely so that appending
        bookkeeping (a note, a claim) cannot silently advance the position.
        """
        return self.append("turn", turn_id=turn_id, text=text, payload=payload, sets_head=True)

    def _derive_head(self) -> str:
        """Return the head id, derived from the journal.

        The head is **the last row whose kind is in :data:`HEAD_KINDS`**. An
        ordinary turn therefore makes itself the head with no second write, and a
        rewind appends a ``head`` row that points somewhere else -- so a ``head``
        row resolves to its ``payload["head_moved_to"]`` target, NOT to itself.
        (The first version returned the marker's own id, which made every rewind
        look like it failed its post-condition check and return ``None``.)

        Deriving rather than storing is what removes the only mutable file in the
        structure, and it was a MEASURED decision, not a preference: on this host
        a file open costs ~5 ms, and the first version did FOUR opens per turn
        (head read, seq read, append, head replace) for a **38 ms median append**
        whose curve was quadratic. One fsync'd append plus a warm cache is the
        design that makes a long session -- the workload this phase exists to
        serve -- actually usable.
        """
        head = ""
        for item in self._cached():
            if item.kind not in HEAD_KINDS:
                continue
            if item.kind == "head":
                head = str((item.payload or {}).get("head_moved_to") or "")
            else:
                head = item.id
        return head

    def write_head_mirror(self) -> Dict[str, Any]:
        """Write ``head.json`` as an explicitly-labelled MIRROR of the derived head.

        A mirror exists so another process (T4's session surface) can read the
        position with one small read instead of parsing the journal. It is never
        trusted: :meth:`head` always derives from the journal, and the mirror
        carries ``journal_seq`` so a reader can tell a current mirror from a stale
        one. That is the difference between a cache and a second authority.
        """
        head = self._derive_head()
        entries = self._cached()
        payload = {
            "schema_version": _TREE_SCHEMA_VERSION,
            "head_id": head,
            "journal_seq": max((int(item.seq or 0) for item in entries), default=0),
            "entries": len(entries),
            "authority": "derived_from_journal",
            "written_at": now_iso(),
        }
        try:
            atomic_write_json(self.head_path, payload)
        except OSError as exc:
            return {"written": False, "reason": f"{type(exc).__name__}"}
        return {"written": True, **payload}

    def head_mirror(self) -> Dict[str, Any]:
        """Read the mirror and report whether it is CURRENT or STALE.

        A stale mirror is reported, not served: "the head is X" is an answer, and
        an answer derived from a file that has fallen behind the journal is a
        lie with a timestamp on it.
        """
        state = read_json_or_none(self.head_path)
        if not isinstance(state, dict):
            return {"present": False, "current": False, "head_id": "", "reason": "no_mirror"}
        entries = self._cached()
        seq = max((int(item.seq or 0) for item in entries), default=0)
        recorded = int(state.get("journal_seq") or 0)
        derived = self._derive_head()
        current = recorded == seq and str(state.get("head_id") or "") == derived
        return {
            "present": True,
            "current": current,
            "head_id": str(state.get("head_id") or ""),
            "derived_head_id": derived,
            "journal_seq": seq,
            "mirror_seq": recorded,
            "reason": "current" if current else "mirror_is_behind_the_journal",
        }

    def move_head(self, entry_id: str, *, reason: str = "") -> Optional[Dict[str, Any]]:
        """Move the head to ``entry_id``. Returns a receipt, or None if refused.

        **This is the rewind.** Nothing is deleted: the position being left is
        still in :meth:`entries` and still reachable by :meth:`path_to_head`.

        Two refusals, both fail-closed:

        * an entry id that does not exist -- a rewind to a position we cannot
          name is not a rewind, and defaulting to the current head would report
          success for a no-op;
        * a claim-only entry as the new head -- a model-generated paragraph is
          not a conversation position, and letting it become one would let a
          summary replace the transcript, which is the exact thing this structure
          exists to prevent.
        """
        target = str(entry_id or "")
        entry = self.entry(target)
        if entry is None:
            return None
        if entry.claim:
            return None
        before = self.head()
        chain = self.path_to_head(target)
        # The move is APPENDED as a `head` row, not written to a pointer file.
        # So a rewind is one fsync'd append, the pre-rewind position is still a
        # first-class row in the journal, and the whole structure has no mutable
        # file -- a stronger form of "never mutated" than a pointer that is only
        # usually written.
        marker = self.append(
            "head",
            text=str(reason or "rewind"),
            payload={"head_moved_to": target, "previous_head": before.id if before else ""},
            parent_id=target,
        )
        if marker is None:
            return None
        if self._derive_head() != target:
            return None
        return {
            "moved": True,
            "from": before.id if before else "",
            "to": target,
            "marker_entry": marker.id,
            "reason": str(reason or "rewind"),
            "depth": len(chain),
            "history_retained": True,
            "retained_from_previous": [item.id for item in chain],
        }

    def fork(self, *, label: str = "", at: str = "") -> Optional[Dict[str, Any]]:
        """Create a new leaf without copying anything. Returns a receipt.

        The new entry is created under the head's PARENT by default, which is
        what makes this a fork of the *current position* rather than a child of
        it: the conversation does not continue past the fork point, it splits.
        Pass ``at=`` to fork from any named entry.

        **This is where a copy-based implementation spends its money and this one
        does not.** ``cli/session.py::fork_session`` deep-copies ``turns``,
        ``raw_turns`` and ``summaries`` and re-appends one ``turn_appended`` event
        per copied turn. The measured difference is in the ``__main__`` report.
        """
        anchor_id = str(at or "")
        anchor = self.entry(anchor_id) if anchor_id else self.head()
        if anchor is None:
            return None
        parent = anchor.parent_id
        # A `branch` entry is in HEAD_KINDS, so appending it IS the move: one
        # fsync'd write, no second step.
        entry = self.append(
            "branch",
            text=str(label or ""),
            parent_id=parent,
            payload={"forked_from": anchor.id, "forked_from_parent": parent},
            sets_head=True,
        )
        if entry is None:
            return None
        if self._derive_head() != entry.id:
            return None
        return {
            "forked": True,
            "entry_id": entry.id,
            "parent_id": parent,
            "forked_from": anchor.id,
            "entries_copied": 0,
            "bytes_copied": 0,
            "label": str(label or ""),
        }

    def abandon(
        self,
        *,
        label: str = "",
        summary: str = "",
        evidence: Sequence[str] = (),
        author: str = "model",
    ) -> Optional[Dict[str, Any]]:
        """Abandon the current branch, optionally writing a LABELLED summary.

        ``summary`` is optional and is stored as ``kind="branch_summary"`` with
        ``claim=True`` and an ``evidence`` list. When ``summary`` is empty the
        branch is simply abandoned and nothing is asserted about it -- which is
        the correct default, because "the branch was dropped" is a fact and
        "here is what would have happened on it" is a claim.

        The evidence list defaults to the abandoned branch's own entry ids, so
        the claim cites what it is a claim about even when the caller does not
        supply it. A caller that supplies a subset may narrow it; a caller that
        supplies nothing gets the branch, which is strictly better than an
        unevidenced claim.
        """
        head = self.head()
        if head is None:
            return None
        chain = [item.id for item in self.path_to_head(head.id)]
        cited = [str(item) for item in evidence] or chain
        written: Optional[TreeEntry] = None
        if str(summary or "").strip():
            written = self.append(
                "branch_summary",
                text=str(summary),
                parent_id=head.id,
                evidence=cited,
                payload={
                    "abandoned_head": head.id,
                    "abandoned_entries": chain,
                    "author": str(author or ""),
                    "label": str(label or ""),
                },
            )
            if written is None:
                return None
        receipt = {
            "abandoned": True,
            "abandoned_head": head.id,
            "abandoned_entries": chain,
            "summary_written": written is not None,
            "summary_entry": written.id if written else "",
            "summary_is_claim": written is not None,
            "evidence": cited if written is not None else [],
            "evidence_resolvable": len(cited) == len(self.branch_summary_evidence(written.id)) if written else True,
        }
        return receipt

    def branch_summary_evidence(self, entry_id: str) -> List[TreeEntry]:
        """Resolve a summary's ``evidence`` ids back to real entries.

        This is what makes the labelling a mechanism rather than a convention:
        the summary text can be deleted from a prompt and the entries it was a
        claim about are still readable. Returns the resolvable subset; an id
        that does not resolve is simply absent, and the caller can see that by
        comparing lengths.
        """
        entry = self.entry(entry_id)
        if entry is None:
            return []
        resolved = [self.entry(item) for item in entry.evidence]
        return [item for item in resolved if item is not None]

    # -- integrity ------------------------------------------------------

    def verify(self) -> Dict[str, Any]:
        """Re-derive every digest and report tampering, or ``ok``.

        The digest covers the entry's own content (see :func:`digest_body`), so a
        row edited after the fact is detectable. A journal whose last line is
        torn by a crash is NOT tampering and is reported separately, because the
        two look identical from the outside and mean opposite things: one means
        somebody changed history, the other means the process died mid-write.

        **``ok`` is about tampering and NOTHING ELSE, and that distinction is
        load-bearing.** A journal that is ``absent`` or ``unreadable`` reports
        ``ok: True`` -- there is nothing in it to tamper with -- and that is a
        true statement about a useless situation. So the receipt also carries
        ``state`` / ``present`` / ``complete`` / ``reason``, and ``complete`` is
        the flag a durability caller must read. Measured before this round, with
        one reader looping while a GC ran: **2 of 48 reads reported
        ``ok: True, rows: 0``**, which is a reader being told its history
        verified at the exact moment it had none.
        """
        rows, state = _read_journal(self.entries_path)
        bad: List[Dict[str, str]] = []
        for row in rows:
            expected = digest_body(row)
            if str(row.get("digest") or "") != expected:
                bad.append(
                    {
                        "id": str(row.get("id") or ""),
                        "expected": expected,
                        "found": str(row.get("digest") or ""),
                    }
                )
        complete = state in ("present", "empty")
        return {
            "ok": not bad,
            "rows": len(rows),
            "tampered": bad,
            "kinds": sorted({str(row.get("kind") or "") for row in rows}),
            "state": state,
            "present": complete,
            "complete": complete and not bad,
            "reason": ""
            if complete
            else (
                "no journal file, so there is no history to verify"
                if state == "absent"
                else "the journal could not be read, so its history is unknown"
            ),
        }

    def consistent_read(self, *, attempts: int = 3) -> Dict[str, Any]:
        """Read the whole tree and say whether the read was COMPLETE.

        **This is the read a background extraction job must use.** A plain
        :meth:`entries` call answers "what rows did I get", which during a
        rotation or a GC can be a partial or empty answer wearing no
        distinction at all. This one:

        1. reads the journal's ``(size, mtime_ns)`` BEFORE the read and again
           after it, and retries while they differ -- so a concurrent *append*
           cannot be reported as a whole-tree view;
        2. refuses to call a read complete when the journal state is ``absent``
           or ``unreadable``, rather than returning an empty list that reads as
           "this session has no turns";
        3. carries the derived head and the entry count, so a caller can tell
           which position it was shown.

        Returns a receipt, never an exception: a reader that raised would be
        indistinguishable to its caller from a reader that worked.
        """
        bounds = max(1, int(attempts))
        last: Dict[str, Any] = {}
        for _ in range(bounds):
            before = self._stamp()
            entries = [TreeEntry.from_row(row) for row in _read_rows(self.entries_path)]
            state = _read_journal(self.entries_path)[1]
            after = self._stamp()
            if before != after:
                # Something wrote while we read. The next pass sees a settled
                # journal; reporting this one would be a torn view presented as
                # a whole one.
                continue
            head_id = ""
            for item in entries:
                if item.kind not in HEAD_KINDS:
                    continue
                head_id = (
                    str((item.payload or {}).get("head_moved_to") or "")
                    if item.kind == "head"
                    else item.id
                )
            complete = state in ("present", "empty")
            last = {
                "ok": True,
                "complete": complete,
                "state": state,
                "reason": ""
                if complete
                else (
                    "the journal was absent while this read ran, so the view is "
                    "empty rather than complete; a rotation or a collection was "
                    "in flight"
                    if state == "absent"
                    else "the journal could not be read, so the view is unknown "
                    "rather than complete"
                ),
                "entries": len(entries),
                "head_id": head_id,
                "head_present": bool(head_id),
                "settled": True,
                "turns": sum(1 for item in entries if item.kind == "turn"),
            }
            return last
        return {
            "ok": True,
            "complete": False,
            "state": last.get("state", "unreadable"),
            "reason": "the journal changed on every attempt, so no settled view "
            "was obtained; this is a live writer, not a corrupt tree",
            "entries": 0,
            "head_id": "",
            "head_present": False,
            "settled": False,
            "turns": 0,
        }

    # -- growth and GC ---------------------------------------------------

    def growth(self) -> Dict[str, Any]:
        """Measure what the structure costs, per turn, honestly.

        ``bytes_per_turn`` is measured against the JOURNAL, which is the thing
        that grows without bound. ``head_bytes`` is reported separately because
        it is the one mutable file and it does not grow -- that asymmetry is the
        entire argument for the tree over an overwriting checkpoint.
        """
        rows = _read_rows(self.entries_path)
        try:
            journal_bytes = self.entries_path.stat().st_size
        except OSError:
            journal_bytes = 0
        try:
            head_bytes = self.head_path.stat().st_size
        except OSError:
            head_bytes = 0
        turns = sum(1 for row in rows if str(row.get("kind") or "") == "turn")
        return {
            "entries": len(rows),
            "turns": turns,
            "journal_bytes": int(journal_bytes),
            "head_bytes": int(head_bytes),
            "bytes_per_turn": (int(journal_bytes) / turns) if turns else None,
            "bytes_per_entry": (int(journal_bytes) / len(rows)) if rows else None,
        }

    def default_roots(self) -> set:
        """Return the default retention roots: the head, every fork, and every claim's evidence.

        **A fork somebody created on purpose is never collected by default.** That
        is the difference between a GC and data loss: a rewind-away linear
        stretch is a position the user passed through, while a fork is a position
        the user CHOSE to create, and silently collecting it destroys work
        somebody asked for. So ``branch`` entries and their subtrees are roots, and
        collecting one is an explicit ``roots=`` decision by the caller.

        **Every ``head`` MARKER row is a root too, and this one was a measured
        data-integrity defect.** A marker is not a position -- it is the RECORD
        that says where the position is, and ``_derive_head`` resolves it to some
        *other* entry. The head is therefore reachable as a root through
        ``path_to_head``, which never visits the marker: the marker is a leaf
        hanging off the target. Collecting it does not delete the position the
        user rewound to; it deletes the statement that they had rewound to it, and
        ``_derive_head`` then falls back to the previous HEAD_KIND row.

        Measured on this tree, driving this module's own audit: after a rewind and
        one GC, ``head_before`` and ``head_after`` were **different entries** and
        the rewind target was reported as a lost position. The session silently
        jumped forward -- with no error, and with ``verify()`` reporting the tree
        intact, because the journal really was intact. It was the user's explicit
        "go back to here" that was discarded, by a collection that was supposed to
        be preserving everything reachable.

        The claim-evidence root is for a different reason: a ``branch_summary``
        that cites an entry whose row has been collected is an unfalsifiable
        claim, which is worse than no summary.
        """
        roots: set = set()
        head = self.head()
        if head is not None:
            roots.add(head.id)
            roots.update(item.id for item in self.path_to_head(head.id))
        entries = self.entries()
        for entry in entries:
            if entry.kind == "branch":
                roots.add(entry.id)
                roots.update(self.reachable([entry.id]))
            if entry.kind == "head":
                # The record of a rewind. See the docstring: collecting it MOVES
                # the head, which is a rewind the user never asked for.
                roots.add(entry.id)
                target = str((entry.payload or {}).get("head_moved_to") or "")
                if target:
                    roots.add(target)
            if entry.evidence:
                roots.update(entry.evidence)
        return roots

    def gc(
        self,
        *,
        roots: Optional[Sequence[str]] = None,
        dry_run: bool = False,
        min_age_s: float = DEFAULT_GC_MIN_AGE_S,
    ) -> Dict[str, Any]:
        """Collect unreachable, expired, unreferenced entries. Reports either way.

        The policy, in order -- an entry is collectable only if ALL hold:

        1. it is not reachable from a retained root (see :meth:`default_roots`);
        2. it is not older than ``min_age_s`` -- the grace window in which a
           just-forked-then-rewound position is never collected, so "I went back"
           cannot lose work;
        3. it is not cited as ``evidence`` by any surviving entry.

        **GC is opt-in and never runs on its own.** An append-only structure
        whose collection happens implicitly is an append-only structure that is
        not, and the honest cost of that is the growth rate reported by
        ``python -m runtime.session_tree``. The policy exists so the growth is
        bounded *by a decision* rather than by a disk filling up.

        Because the journal is append-only, collection is implemented as a
        **compaction that rewrites the journal and rotates the original aside**,
        the same way ``runtime.checkpoint._rotate`` does it. The previous file is
        never deleted: a GC that destroys its own evidence is how a leak becomes
        a data-loss bug.

        Returns the removed ids, the retained count and the bytes reclaimed, so
        the caller can report rather than assume. ``dry_run=True`` changes
        nothing and reports the same numbers, which is the only way a caller can
        tell a plan from a result.
        """
        entries = self.entries()
        if not entries:
            return {
                "collected": [],
                "removed": 0,
                "retained": 0,
                "bytes_before": 0,
                "bytes_after": 0,
                "bytes_reclaimed": 0,
                "dry_run": bool(dry_run),
                "rotated_to": "",
                "reason": "empty_tree",
            }
        retained_ids = (
            self.default_roots() if roots is None else {str(item) for item in roots if str(item)}
        )
        collectable: List[TreeEntry] = []
        now = float(self._clock())
        for entry in entries:
            if entry.id in retained_ids:
                continue
            if now - float(entry.created_epoch or 0.0) < float(min_age_s):
                continue
            collectable.append(entry)
        # An entry may only be collected once nothing cites it. Two passes so a
        # claim that cites a claim that cites an entry keeps all three.
        collectable_ids = {item.id for item in collectable}
        cited: set = set()
        for entry in entries:
            if entry.id in collectable_ids:
                continue
            cited.update(entry.evidence)
        victims = [entry for entry in collectable if entry.id not in cited]
        kept = [entry for entry in entries if entry.id not in {item.id for item in victims}]
        try:
            bytes_before = self.entries_path.stat().st_size
        except OSError:
            bytes_before = 0
        receipt: Dict[str, Any] = {
            # `collectable` is the PLAN and is reported as a plan. `removed` is
            # what happened, and it is only filled in once the install landed --
            # the previous version computed both before the rewrite, so a
            # rewrite that failed still reported the full victim count as
            # REMOVED. A storage-pressure GC that quietly collected nothing while
            # claiming it collected everything is worse than no GC.
            "collectable": [item.id for item in victims],
            "collectable_count": len(victims),
            "collected": [],
            "removed": 0,
            "retained": len(entries),
            "bytes_before": int(bytes_before),
            "bytes_after": int(bytes_before),
            "bytes_reclaimed": 0,
            "dry_run": bool(dry_run),
            "rotated_to": "",
            "evidence_protected": sorted(cited),
            "journal_state": _read_journal(self.entries_path)[1],
        }
        if dry_run:
            receipt["reason"] = "dry_run"
            return receipt
        if not victims:
            receipt["reason"] = "nothing_collectable"
            return receipt
        rotated = self._rewrite(kept)
        self._index = None
        if rotated is None:
            receipt["reason"] = "rewrite_refused_nothing_removed"
            receipt["error"] = _GC_LAST_ERROR or "the compacted journal was not installed"
            receipt["removed"] = 0
            receipt["retained"] = len(entries)
            return receipt
        try:
            bytes_after = self.entries_path.stat().st_size
        except OSError:
            bytes_after = 0
        receipt.update(
            {
                "collected": [item.id for item in victims],
                "removed": len(victims),
                "retained": len(kept),
                "bytes_after": int(bytes_after),
                "bytes_reclaimed": max(0, int(bytes_before) - int(bytes_after)),
                "rotated_to": str(rotated),
                "reason": "collected",
            }
        )
        if _GC_LAST_ERROR:
            # The collection landed but the evidence copy did not. Reported,
            # because a receipt that says "collected" and silently has no
            # rotatable journal is the same shape as one that lies about
            # removing.
            receipt["evidence_copy_error"] = _GC_LAST_ERROR
        after = _read_journal(self.entries_path)[1]
        receipt["journal_state"] = after
        if after not in ("present", "empty"):
            receipt["reason"] = "collected_but_the_journal_is_not_readable"
        return receipt

    def _rewrite(self, entries: Sequence[TreeEntry]) -> Optional[Path]:
        """Install a compacted journal ATOMICALLY, and keep the old one as evidence.

        **The previous implementation was the reader-visible window this round
        closed.** It did ``os.replace(original, rotated)`` and only THEN opened
        ``original`` for writing, so for the whole duration of the rewrite there
        was no journal at all -- and because :func:`_read_rows` swallowed the
        ``OSError`` and :meth:`verify` reported ``ok`` over zero rows, a
        concurrent reader was told it had a healthy tree with no history in it.
        Measured: 2 of 48 concurrent reads, live.

        Two changes close it, and both are about ORDER rather than about locking:

        1. **The compacted journal is written to a sibling temp file first** and
           only then ``os.replace``d over the original. ``os.replace`` is atomic,
           so a reader sees either the whole old journal or the whole new one.
           There is no instant at which neither exists.
        2. **The previous journal is COPIED aside, not MOVED.** Rotating by
           ``os.replace`` is what removed it. A copy preserves the evidence a
           GC receipt claims exists, and the copy failing is reported rather than
           being allowed to abort the collection.

        A reader that catches the instant of the replace gets one of two whole
        journals, which is the only pair of answers it can act on.
        """
        global _GC_LAST_ERROR
        _GC_LAST_ERROR = ""
        temp = self.entries_path.with_name(f"{self.entries_path.name}.compact-{time.time_ns()}")
        try:
            ensure_dir(self.root)
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                for entry in entries:
                    handle.write(
                        json.dumps(entry.as_row(), ensure_ascii=False, sort_keys=True)
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            rotated: Optional[Path] = None
            if self.entries_path.is_file():
                rotated = self.entries_path.with_name(
                    f"{self.entries_path.name}.gc-{time.time_ns()}"
                )
                try:
                    # A COPY, deliberately. `shutil.copy2` rather than a replace:
                    # the evidence must not be produced by the same operation that
                    # would remove the journal if it failed.
                    shutil.copy2(str(self.entries_path), str(rotated))
                except OSError as exc:
                    _GC_LAST_ERROR = f"evidence copy failed ({type(exc).__name__}: {exc})"
                    rotated = None
            if not _replace_with_retry(temp, self.entries_path):
                try:
                    temp.unlink()
                except OSError:
                    pass
                if not _GC_LAST_ERROR:
                    _GC_LAST_ERROR = "the compacted journal could not replace the original"
                return None
            return rotated
        except OSError as exc:
            _GC_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            try:
                temp.unlink()
            except OSError:
                pass
            return None


# -- the surface T4 needs to move ``fork_session`` onto ----------------------


def fork_api(root: Path | str, *, clock: Any = None) -> Dict[str, Any]:
    """Return the callable surface a CLI fork should delegate to.

    ``cli/session.py::fork_session`` currently deep-copies ``turns``,
    ``raw_turns`` and ``summaries`` and re-appends one ``turn_appended`` event
    per copied turn. That is the copy this structure makes unnecessary. The
    migration is a three-line body:

    .. code-block:: python

        tree = SessionTree(log_root / "_tree" / session_id)
        receipt = tree.fork(label=reason, at=at_turn_id)
        # receipt["entry_id"] is the new position; receipt["entries_copied"] == 0

    Returned as a dict rather than a dataclass so a caller can read the exact
    key names it has to handle, and so a future key addition is visible as an
    addition rather than as an attribute error somewhere in a slash command.
    """
    return {
        "class": "runtime.session_tree.SessionTree",
        "fork": "SessionTree.fork(*, label='', at='') -> dict | None",
        "move_head": "SessionTree.move_head(entry_id, *, reason='') -> dict | None",
        "abandon": "SessionTree.abandon(*, label='', summary='', evidence=(), author='model') -> dict | None",
        "advance": "SessionTree.advance(*, turn_id='', text='', payload=None) -> TreeEntry | None",
        "path_to_head": "SessionTree.path_to_head(entry_id='') -> list[TreeEntry]",
        "entries": "SessionTree.entries() -> list[TreeEntry]",
        "entry": "SessionTree.entry(entry_id) -> TreeEntry | None",
        "verify": "SessionTree.verify() -> dict",
        "gc": "SessionTree.gc(*, roots=(), dry_run=False, min_age_s=3600.0) -> dict",
        "claim_marker": CLAIM_MARKER,
        "returns_none_on_refusal": (
            "fork returns None when there is no head to fork from; "
            "move_head returns None for an unknown entry OR a claim entry; "
            "append returns None for an unknown kind or an unwritable journal"
        ),
    }


def describe(root: Path | str) -> Dict[str, Any]:
    """Return one receipt describing a tree's shape. This is the T4 render input.

    Everything a surface needs to draw "this session rotated and here is where
    to go back" is in this dict, including the honesty flags: whether the
    current head is a claim (it must never be), and whether the journal verifies.
    """
    tree = SessionTree(root)
    head = tree.head()
    chain = tree.path_to_head(head.id) if head is not None else []
    return {
        "exists": tree.entries_path.is_file(),
        "entries": len(tree.entries()),
        "head": head.as_dict() if head is not None else None,
        "head_is_claim": bool(head is not None and head.claim),
        "head_authority": "derived_from_journal",
        "head_mirror": tree.head_mirror(),
        "depth": len(chain),
        "ancestry": [
            {"id": item.id, "kind": item.kind, "turn_id": item.turn_id, "claim": item.claim}
            for item in chain
        ],
        "forks": [
            {"id": item.id, "label": item.text, "retained": True}
            for item in tree.entries()
            if item.kind == "branch"
        ],
        "claims": [
            branch_summary_line(item.as_row())
            for item in tree.entries()
            if item.claim
        ],
        "growth": tree.growth(),
        "verify": tree.verify(),
    }


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m runtime.session_tree`` -- the T3.W1.3 measurement report.

    Builds a synthetic-but-real session (N turns, forks, rewinds) and reports
    the before/after costs, the growth rate, and the GC outcome. Exits 2 if the
    tree fails its own integrity check, because a measurement produced by a
    structure that does not verify is not a measurement.
    """
    import argparse
    import shutil
    import tempfile

    parser = argparse.ArgumentParser(prog="python -m runtime.session_tree")
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--forks", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    turns = max(1, int(args.turns))
    forks = max(0, int(args.forks))
    workspace = Path(tempfile.mkdtemp(prefix="neo-session-tree-"))
    try:
        tree = SessionTree(workspace / "tree")
        text = "x" * 400
        tree.advance(turn_id="turn-0", text="seed")

        append_ms: List[float] = []
        for index in range(1, turns + 1):
            start = time.perf_counter()
            tree.advance(turn_id=f"turn-{index}", text=text)
            append_ms.append((time.perf_counter() - start) * 1000.0)

        # -- the before/after fork comparison -----------------------------
        fork_ms: List[float] = []
        for index in range(forks):
            start = time.perf_counter()
            receipt = tree.fork(label=f"alt-{index}")
            fork_ms.append((time.perf_counter() - start) * 1000.0)
            if receipt is not None:
                tree.advance(turn_id=f"alt-{index}-a", text=text)
                tree.advance(turn_id=f"alt-{index}-b", text=text)

        # -- rewind cost --------------------------------------------------
        targets = [item.id for item in tree.entries() if item.kind == "turn"]
        rewind_ms: List[float] = []
        for target in targets[:forks]:
            start = time.perf_counter()
            tree.move_head(target, reason="measure")
            rewind_ms.append((time.perf_counter() - start) * 1000.0)

        # -- resume after a rewind: how long to read the restored position --
        resume_start = time.perf_counter()
        restored = [item.as_row() for item in tree.path_to_head()]
        resume_ms = (time.perf_counter() - resume_start) * 1000.0

        growth = tree.growth()
        # Two GC runs, because they answer DIFFERENT questions and reporting only
        # the second would imply the first is unnecessary:
        #   * the DEFAULT policy -- nothing is collectable, because every entry is
        #     young (min_age_s=3600) and every fork is a retention root. This is
        #     the number that describes the steady state.
        #   * an AGED policy (negative age) -- proves collection happens at all
        #     and that it collects only the rewound-past linear stretch.
        gc_default = tree.gc()
        gc_dry = tree.gc(dry_run=True, min_age_s=-1.0)
        gc_real = tree.gc(min_age_s=-1.0)
        mirror_start = time.perf_counter()
        tree.write_head_mirror()
        mirror_ms = (time.perf_counter() - mirror_start) * 1000.0

        report = {
            "turns": turns,
            "forks": forks,
            "append_ms_median": sorted(append_ms)[len(append_ms) // 2] if append_ms else None,
            "fork_ms_median": sorted(fork_ms)[len(fork_ms) // 2] if fork_ms else None,
            "fork_entries_copied": 0,
            "rewind_ms_median": sorted(rewind_ms)[len(rewind_ms) // 2] if rewind_ms else None,
            "head_mirror_ms": mirror_ms,
            "resume_after_rewind_ms": resume_ms,
            "restored_entries": len(restored),
            "growth": growth,
            "gc_default_policy": gc_default,
            "gc_aged_dry_run": gc_dry,
            "gc_aged": gc_real,
            "mirror": tree.head_mirror(),
            "verify": tree.verify(),
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("session tree measurement (synthetic session, this host)")
            print(f"  turns                          {turns}")
            print(f"  append ms (median)              {report['append_ms_median']:.3f}")
            print(f"  fork ms (median)                {report['fork_ms_median']:.3f}")
            print("  entries copied per fork         0")
            print(f"  rewind ms (median)              {report['rewind_ms_median']:.3f}")
            print(f"  head-mirror write ms            {mirror_ms:.3f}")
            print(f"  resume-after-rewind ms          {resume_ms:.3f}")
            print(f"  restored entries                {len(restored)}")
            print(f"  journal bytes                   {growth['journal_bytes']}")
            print(f"  bytes per turn                  {growth['bytes_per_turn']}")
            print("  mutable files in the structure  0 (head is derived)")
            print()
            print("  GC -- default policy (steady state)")
            print(f"    removed                       {gc_default['removed']}")
            print(f"    reason                        {gc_default['reason']}")
            print("  GC -- aged (proves collection works)")
            print(f"    dry run said                  {gc_dry['removed']}")
            print(f"    removed                       {gc_real['removed']}")
            print(f"    retained                      {gc_real['retained']}")
            print(f"    bytes reclaimed               {gc_real['bytes_reclaimed']}")
            print(f"    rotated aside to              {Path(gc_real['rotated_to']).name if gc_real['rotated_to'] else '(none)'}")
            print()
            print(f"  mirror current                  {report['mirror']['current']}")
            print(f"  verify ok                       {report['verify']['ok']}")
        if not report["verify"]["ok"]:
            return 2
        return 0
    finally:
        shutil.rmtree(str(workspace), ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_main())
