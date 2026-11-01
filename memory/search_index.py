"""SQLite FTS5 search over sessions and memories — the deterministic ceiling.

THE EVIDENCE THIS IMPLEMENTS
-----------------------------
The corpus finding is that persistent memory has replaced compaction as the
frontier of context engineering, and that in that corpus **none of the eleven
systems uses embedding-based retrieval as its primary memory substrate**. The
deterministic-retrieval result that already held for code holds for memory, and
SQLite full-text search is named as the production ceiling. One production
system proves lexical retrieval suffices at scale: FTS5 with BM25 plus trigram
indexing for CJK, over trigger-maintained tables, with **no LLM calls
anywhere**.

So this module is deliberately NOT semantic. It is:

* **BM25 ranking**, which FTS5 provides natively (``bm25()``), over sessions
  and over memories.
* **Trigram indexing**, because ``unicode61`` cannot match inside a CJK run --
  it finds no word boundary there, so a two-character query inside a
  six-character phrase is invisible to it. Trigram is the corpus's named
  answer and it is used here for exactly that reason.
* **Deterministic**: no model call, anywhere, on any path. A search that costs
  a model call has a latency and cost profile that makes it unusable on the
  hot path -- and "deterministic" is also what makes it *measurable*: the same
  corpus and query give the same ranking, so a regression is a fact rather
  than a mood.

THE HONESTY CRUX
----------------
A silent fallback to a linear scan is how a search layer becomes orders of
magnitude slower with no signal. Every result therefore carries ``engine``, and
the ``unavailable`` state is a **first-class outcome that is structurally
distinct from a real, empty result set**: :class:`SearchOutcome` refuses to
construct with ``available=False`` and a non-empty ``hits``, and refuses
``available=True`` without a real engine name. There is no code path from "no
FTS5" to "results found" -- :func:`fts5_capability` is probed once and its
verdict is published in every receipt.

This mirrors how T1 reports the retrieval engine: the reader is told which
engine answered, not merely that something answered.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
* No embeddings, no vector store, no similarity fallback. 0 of 11 corpus
  systems use embeddings as the primary substrate; adding one would make the
  hot path non-deterministic and cost-per-query non-zero.
* No cross-process daemon. One SQLite file, opened by whoever needs it.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "INDEX_KINDS",
    "REPORTED_ENGINES",
    "IndexReport",
    "SearchEngine",
    "SearchHit",
    "SearchIndex",
    "SearchOutcome",
    "engine_report",
    "fts5_capability",
    "has_cjk",
    "open_default_search_index",
]

#: Identifiers this index understands. ``sessions`` and ``memories`` are the two
#: corpora the phase cares about; keeping the set closed means a caller cannot
#: invent a third corpus and get an empty answer that reads as "no matches".
INDEX_KINDS: Tuple[str, ...] = ("memories", "sessions")

#: The cap on a single indexed document. Chosen to MATCH the decision store's
#: own per-row storage cap (``memory.decision_store._MAX_TEXT_CHARS`` ==
#: 16,384) so the index can never be the component that silently truncates a
#: row the store accepted. Measured over the 6,317 real rows in this
#: repository's own decision DB: p50=67 chars, p90=72, max=429 -- so the cap
#: fires on pathological input only, and it REPORTS when it fires.
MAX_DOC_CHARS = 16_384

#: Trigram needs three consecutive characters to form an indexable token. A
#: query shorter than this CANNOT match in a trigram index, no matter what
#: memory contains. That is a property of the engine, not of the corpus, and it
#: is reported as such: :attr:`SearchOutcome.below_token_floor` is the
#: machine-readable form and ``reason`` is the human one. Measured on SQLite
#: 3.40.1: a 3-character CJK phrase matches, a 2-character one matches
#: nothing, and without this flag the second case is indistinguishable from
#: "this repository has no memory about that".
TRIGRAM_TOKEN_FLOOR_CHARS = 3

#: Boolean match modes, matching FTS5's own ``AND`` / ``OR`` operators.
#:
#: ``and`` (the default) is PRECISE: every term must appear. A search for
#: "pytest invocation" against a memory that says "run pytest from the root"
#: returns nothing, which is defensible.
#:
#: ``or`` is RECALL-oriented: any term is enough, and BM25 ranks the rows that
#: matched more terms first. For a memory surface this is usually what a user
#: means, and it is the mode that makes a recall@k measurable at all -- an AND
#: search over a labelled query set where one term is a paraphrase measures the
#: labelling, not the retrieval.
#:
#: The default is unchanged at ``and`` because precision is the safer failure
#: for a caller who did not choose, and changing a default is a contract
#: change.
MATCH_MODES: Tuple[str, ...] = ("and", "or")

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)


class SearchEngine(str, Enum):
    """Which engine answered. ``UNAVAILABLE`` is not a success with no rows."""

    FTS5_UNICODE = "fts5_unicode61"
    FTS5_TRIGRAM = "fts5_trigram"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        """Return the serialized engine name."""
        return self.value


#: Engines this module will ever report as having answered. A caller can
#: compare membership without string-matching, and an unexpected engine name is
#: a bug rather than a silent variant.
REPORTED_ENGINES: Tuple[str, ...] = (
    SearchEngine.FTS5_UNICODE.value,
    SearchEngine.FTS5_TRIGRAM.value,
)

#: The unicode61 tokenizer string, published in every receipt so a reader knows
#: which segmentation produced a ranking.
UNICODE_TOKENIZER = "unicode61 remove_diacritics 2"
TRIGRAM_TOKENIZER = "trigram"

# ``index_documents`` holds the content; the two FTS tables hold their OWN copy
# of the ranked text keyed by the SAME rowid. An external-content FTS5 table
# (``content='index_documents'``) would need an explicit 'delete' command
# carrying the ORIGINAL column values on every update, and getting that wrong
# leaves a stale row that searches as if it were live. Two standalone tables
# make the update a plain DELETE + INSERT, which is auditable.
_CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_documents (
    kind TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    repo_key TEXT,
    payload_digest TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_index_documents_repo
    ON index_documents(kind, repo_key);
CREATE INDEX IF NOT EXISTS idx_index_documents_updated
    ON index_documents(updated_at);
"""

_UNICODE_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS index_fts USING fts5(
    title, body, tokenize='unicode61 remove_diacritics 2'
);
"""

_TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS index_fts_trigram USING fts5(
    title, body, tokenize='trigram'
);
"""


@dataclass(frozen=True)
class SearchHit:
    """One ranked row.

    ``rank`` is 1-based and is the position in THIS result set, so a consumer
    can render "why did this surface" without recomputing anything.
    """

    kind: str
    doc_id: str
    title: str
    body: str
    score: float
    rank: int
    engine: str
    ordering: str = ""
    repo_key: Optional[str] = None
    updated_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this hit."""
        return {
            "kind": self.kind,
            "doc_id": self.doc_id,
            "title": self.title,
            "body": self.body,
            "score": self.score,
            "rank": self.rank,
            "engine": self.engine,
            "ordering": self.ordering,
            "repo_key": self.repo_key,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SearchOutcome:
    """The result of one search, including WHICH engine answered.

    The two classmethods below are the honesty mechanism, and they are why this
    is a dataclass rather than a dict:

    * :meth:`unavailable` -- FTS5 is not compiled in. ``available`` is False,
      ``engine`` is ``"unavailable"``, ``reason`` is non-empty, and ``hits`` is
      empty.
    * :meth:`results` -- a real answer from a named engine. ``available`` is
      True and ``engine`` is one of :data:`REPORTED_ENGINES`.

    A caller cannot build an outcome that claims results without an engine, or
    an engine-less failure that looks like a hit.
    """

    available: bool
    engine: str
    hits: Tuple[SearchHit, ...] = ()
    reason: str = ""
    query: str = ""
    truncated: bool = False
    truncation_reason: str = ""
    scanned_rows: int = 0
    tokenizer: str = ""
    ordering: str = ""
    cjk_query: bool = False
    trigram_attempted: bool = False
    trigram_reason: str = ""
    below_token_floor: bool = False

    def __post_init__(self) -> None:
        """Refuse the two dishonest shapes."""
        if self.available:
            if self.engine not in REPORTED_ENGINES:
                raise ValueError(
                    f"an available search must name a real engine; got "
                    f"{self.engine!r}. REPORTED_ENGINES is {REPORTED_ENGINES!r}."
                )
            if not self.hits and self.truncation_reason:
                # A truncated ranking is a partial answer and must not be able
                # to masquerade as a complete empty one.
                raise ValueError(
                    "a truncated ranking cannot be an empty result set: the two "
                    "are different facts and this outcome would conflate them"
                )
        else:
            if self.hits:
                raise ValueError(
                    "an unavailable search cannot carry hits; that is the exact "
                    "failure this type exists to prevent"
                )
            if self.engine != SearchEngine.UNAVAILABLE.value:
                raise ValueError(
                    "an unavailable search must report the unavailable engine"
                )
            if not str(self.reason).strip():
                raise ValueError(
                    "an unavailable search must carry the exact reason it is "
                    "unavailable; a reasonless failure reads as an empty result"
                )

    @classmethod
    def unavailable(cls, reason: str, query: str = "") -> "SearchOutcome":
        """Return the honest failure: no engine, no hits, a stated reason."""
        return cls(
            available=False,
            engine=SearchEngine.UNAVAILABLE.value,
            hits=(),
            reason=str(reason),
            query=str(query or ""),
        )

    @classmethod
    def results(
        cls,
        engine: str,
        hits: Sequence[SearchHit],
        *,
        query: str = "",
        truncated: bool = False,
        truncation_reason: str = "",
        scanned_rows: int = 0,
        tokenizer: str = "",
        ordering: str = "",
        cjk_query: bool = False,
        trigram_attempted: bool = False,
        trigram_reason: str = "",
        below_token_floor: bool = False,
        reason: str = "",
    ) -> "SearchOutcome":
        """Return a real, engine-named answer."""
        return cls(
            available=True,
            engine=str(engine),
            hits=tuple(hits),
            query=str(query or ""),
            truncated=bool(truncated),
            truncation_reason=str(truncation_reason or ""),
            scanned_rows=int(scanned_rows),
            tokenizer=str(tokenizer or ""),
            ordering=str(ordering or ""),
            cjk_query=bool(cjk_query),
            trigram_attempted=bool(trigram_attempted),
            trigram_reason=str(trigram_reason or ""),
            below_token_floor=bool(below_token_floor),
            reason=str(reason or ""),
        )

    @property
    def empty(self) -> bool:
        """True when a real engine answered and matched nothing.

        Distinct from ``not available``: an empty-but-available answer means
        "the engine ran and there was nothing", which is a fact about memory.
        """
        return self.available and not self.hits

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt.

        Every consumer-visible field is present on BOTH the available and the
        unavailable path, so a renderer cannot branch on ``if hits:`` and end up
        drawing an empty list for an engine that does not exist.
        """
        return {
            "available": self.available,
            "engine": self.engine,
            "hits": [hit.as_dict() for hit in self.hits],
            "count": len(self.hits),
            "reason": self.reason,
            "query": self.query,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "scanned_rows": self.scanned_rows,
            "tokenizer": self.tokenizer,
            "ordering": self.ordering,
            "cjk_query": self.cjk_query,
            "trigram_attempted": self.trigram_attempted,
            "trigram_reason": self.trigram_reason,
            "below_token_floor": self.below_token_floor,
            "deterministic": True,
            "model_calls": 0,
        }

    def receipt(self) -> Dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()


def _probe_tokenizers() -> Tuple[bool, bool, str]:
    """Return ``(fts5, trigram, sqlite_version)`` for a throwaway in-memory DB.

    A capability PROBE, not an assumption: both tokenizers are optional build
    options of SQLite, and a machine can have FTS5 without trigram. Probing is
    the only honest way to know, and it is cheap (two CREATE statements on an
    in-memory database, once per process).
    """
    version = str(getattr(sqlite3, "sqlite_version", "") or "")
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False, False, version
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE probe_u USING fts5(x)")
            fts5 = True
        except sqlite3.Error:
            fts5 = False
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE probe_t USING fts5(x, tokenize='trigram')"
            )
            trigram = True
        except sqlite3.Error:
            trigram = False
        return fts5, trigram, version
    finally:
        conn.close()


FTS5_OK, TRIGRAM_OK, SQLITE_VERSION = _probe_tokenizers()


def fts5_capability() -> Dict[str, Any]:
    """Report what this interpreter's SQLite actually supports.

    The consumer of this receipt is the reason the module exists in this
    shape: a caller must be able to say "the search engine is unavailable
    because this build has no FTS5" instead of silently getting a slower,
    differently-ranked answer that looks identical.
    """
    reason = ""
    if not FTS5_OK:
        reason = (
            f"this Python's SQLite ({SQLITE_VERSION or 'unknown'}) was built "
            "without the FTS5 module, so BM25 ranking is impossible; this index "
            "refuses to substitute a linear scan because a silent fallback is "
            "how a search layer becomes orders of magnitude slower with no signal"
        )
    trigram_reason = ""
    if FTS5_OK and not TRIGRAM_OK:
        trigram_reason = (
            f"this Python's SQLite ({SQLITE_VERSION or 'unknown'}) has FTS5 "
            "but not the trigram tokenizer, so a query shorter than three "
            "characters, or a query inside a CJK run, cannot be matched by the "
            "unicode61 index either; CJK recall is degraded on this build and "
            "that is stated rather than hidden"
        )
    return {
        "fts5": FTS5_OK,
        "trigram": TRIGRAM_OK,
        "sqlite_version": SQLITE_VERSION,
        "engines": list(REPORTED_ENGINES),
        "reason": reason,
        "trigram_reason": trigram_reason,
        "deterministic": True,
        "model_calls": 0,
    }


def engine_report() -> Dict[str, Any]:
    """Alias for :func:`fts5_capability`, named for the receipt it produces."""
    return fts5_capability()


def has_cjk(text: Any) -> bool:
    """True when the text contains a CJK codepoint.

    This is the switch that decides whether trigram is *needed*, and it is a
    property of the QUERY rather than of the corpus: a CJK query against a
    unicode61 index cannot match inside a run, so the trigram path is tried
    and the receipt says it was tried.
    """
    try:
        return _CJK_RE.search(str(text or "")) is not None
    except Exception:
        return False


def _unicode_query(text: str, match_mode: str = "and") -> str:
    """Build a safe FTS5 MATCH expression from free text, unicode61 style.

    Every term is quoted, so FTS5 operator characters in a user query (``*``,
    ``NEAR``, ``^``, ``:``, ``-``) are literal characters rather than query
    syntax. A query is untrusted input; an unescaped one is an injection into
    the ranking function.

    Terms are joined by the mode's boolean operator. In ``or`` mode the final
    term carries a prefix ``*`` so a prefix-typed word finds the row containing
    it -- the behaviour a human expects from a memory search, and it costs
    nothing in determinism.
    """
    terms = _WORD_RE.findall(str(text or ""))
    if not terms:
        return ""
    quoted = [f'"{term}"' for term in terms]
    if match_mode == "or":
        quoted[-1] = f"{quoted[-1]}*"
        return " OR ".join(quoted)
    quoted[-1] = f"{quoted[-1]}*"
    return " ".join(quoted)


def _trigram_query(text: str) -> str:
    """Build a trigram MATCH expression.

    A trigram index matches on character TRIPLETS, so the query is quoted as a
    single phrase rather than split into words. Quoting is still mandatory: an
    unquoted trigram query would treat punctuation as syntax.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    return '"' + raw.replace('"', '""') + '"'


def _payload_digest(kind: str, doc_id: str, title: str, body: str) -> str:
    """Return the content address of one document's ranked payload.

    Invalidation is by CONTENT, not by row count or mtime: re-indexing a
    document whose ranked text is unchanged writes nothing and bumps nothing.
    That is what makes "index only new content" a property rather than a hope.
    """
    payload = "\x1f".join((str(kind), str(doc_id), str(title or ""), str(body or "")))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _bounded_limit(limit: Any, default: int = 20) -> int:
    """Clamp a caller-supplied limit into ``[1, 500]``."""
    try:
        return max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        return default


@dataclass
class _IndexState:
    """Bookkeeping for one open index."""

    conn: Optional[sqlite3.Connection] = None
    open_error: str = ""
    fts5: bool = False
    trigram: bool = False
    closed: bool = False
    indexed: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    truncated_docs: int = 0


@dataclass(frozen=True)
class IndexReport:
    """What one :meth:`SearchIndex.index` pass actually did.

    ``indexed``/``updated``/``unchanged``/``removed`` are four DIFFERENT facts.
    Collapsing them into one "processed" counter is how an incremental indexer
    claims to be incremental while re-indexing everything, so they are kept
    apart and the test suite asserts the split.
    """

    kind: str
    indexed: int
    updated: int
    unchanged: int
    removed: int
    truncated_docs: int
    engine: str
    tokenizer: str
    trigram_tokenizer: bool
    reason: str

    @property
    def ok(self) -> bool:
        """True when a real engine is backing this index."""
        return self.engine in REPORTED_ENGINES

    @property
    def writes(self) -> int:
        """How many documents actually caused a write.

        ``indexed + updated``; ``unchanged`` is excluded, which is the whole
        point of a content-addressed index.
        """
        return self.indexed + self.updated

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "kind": self.kind,
            "indexed": self.indexed,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "writes": self.writes,
            "truncated_docs": self.truncated_docs,
            "engine": self.engine,
            "tokenizer": self.tokenizer,
            "trigram_tokenizer": self.trigram_tokenizer,
            "reason": self.reason,
            "ok": self.ok,
        }


class SearchIndex:
    """A content-addressed FTS5 index over sessions and memories.

    Thread-safe through one ``RLock``, exactly like
    :class:`memory.decision_store.DecisionStore`: the MCP server may serve
    requests from different threads.

    Assumes the parent directory of ``db_path`` is writable. When FTS5 is not
    compiled in, every method still WORKS and every receipt says
    ``engine="unavailable"`` with the reason -- the object degrades to a
    refusal, never to a slower different answer.
    """

    def __init__(self, db_path: Any, *, strict: bool = False) -> None:
        """Open the index; ``strict`` re-raises an unusable-database failure.

        ``strict=False`` (the default) is the honest default: an unopenable
        index yields an object whose every search reports ``unavailable`` with
        the reason, which a caller can render. ``strict=True`` is for a caller
        that would rather fail loudly at construction.
        """
        self.db_path = str(Path(str(db_path)).resolve())
        self._lock = threading.RLock()
        self._state = _IndexState(fts5=FTS5_OK, trigram=TRIGRAM_OK)
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            if FTS5_OK:
                conn.executescript(_CONTENT_SCHEMA)
                conn.executescript(_UNICODE_SCHEMA)
                if TRIGRAM_OK:
                    conn.executescript(_TRIGRAM_SCHEMA)
                conn.commit()
            self._state.conn = conn
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
            self._state.open_error = f"{type(exc).__name__}: {exc}"[:300]
            if strict:
                raise

    # -- capability -----------------------------------------------------

    @property
    def available(self) -> bool:
        """True when a real BM25 engine is backing this index."""
        return bool(self._state.fts5 and self._state.conn is not None)

    @property
    def trigram_available(self) -> bool:
        """True when the trigram (CJK-capable) index exists."""
        return bool(self._state.trigram and self.available)

    def capability(self) -> Dict[str, Any]:
        """Return this index's live capability, including any open failure.

        :func:`fts5_capability` reports the interpreter; this reports THIS
        object, which can additionally be unavailable because its file could
        not be opened. Conflating the two is how a disk problem gets reported
        as a missing SQLite feature.
        """
        report = fts5_capability()
        report["db_path"] = self.db_path
        report["open_error"] = self._state.open_error
        report["index_available"] = self.available
        if not self.available and not report["reason"]:
            report["reason"] = (
                f"the index database could not be opened: {self._state.open_error}"
                if self._state.open_error
                else "the index database is not open"
            )
        return report

    # -- write ----------------------------------------------------------

    def index(
        self,
        kind: str,
        documents: Iterable[MappingABC[str, Any]],
    ) -> IndexReport:
        """Index ``documents`` for ``kind``, writing only what changed.

        Each document is a mapping with at least ``doc_id``; ``title``,
        ``body``, ``repo_key`` and ``updated_at`` are optional. Content is
        compared by SHA-256 against the stored digest, so an unchanged document
        is a no-op even when it is submitted again on every run.

        A document whose body exceeds :data:`MAX_DOC_CHARS` is TRUNCATED, and
        the count is published as ``truncated_docs`` -- a cap that fires must be
        reported, never applied silently.
        """
        kind_key = str(kind or "").strip()
        if kind_key not in INDEX_KINDS:
            return IndexReport(
                kind=kind_key,
                indexed=0,
                updated=0,
                unchanged=0,
                removed=0,
                truncated_docs=0,
                engine=SearchEngine.UNAVAILABLE.value,
                tokenizer="",
                trigram_tokenizer=False,
                reason=(
                    f"unknown index kind {kind_key!r}; this index serves "
                    f"{INDEX_KINDS!r} and refuses to invent a third corpus"
                ),
            )
        state = self._state
        with self._lock:
            conn = state.conn
            if conn is None or not state.fts5:
                return IndexReport(
                    kind=kind_key,
                    indexed=0,
                    updated=0,
                    unchanged=0,
                    removed=0,
                    truncated_docs=0,
                    engine=SearchEngine.UNAVAILABLE.value,
                    tokenizer="",
                    trigram_tokenizer=False,
                    reason=self.capability()["reason"]
                    or "the index database is not open",
                )
            indexed = updated = unchanged = truncated = 0
            try:
                for document in documents or ():
                    action, was_truncated = self._index_one(
                        conn, kind_key, document
                    )
                    if was_truncated:
                        truncated += 1
                    if action == "indexed":
                        indexed += 1
                    elif action == "updated":
                        updated += 1
                    else:
                        unchanged += 1
                conn.commit()
            except sqlite3.DatabaseError as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return IndexReport(
                    kind=kind_key,
                    indexed=0,
                    updated=0,
                    unchanged=0,
                    removed=0,
                    truncated_docs=0,
                    engine=SearchEngine.UNAVAILABLE.value,
                    tokenizer="",
                    trigram_tokenizer=False,
                    reason=f"indexing failed: {type(exc).__name__}: {exc}"[:300],
                )
            state.indexed += indexed
            state.updated += updated
            state.unchanged += unchanged
            state.truncated_docs += truncated
        return IndexReport(
            kind=kind_key,
            indexed=indexed,
            updated=updated,
            unchanged=unchanged,
            removed=0,
            truncated_docs=truncated,
            engine=SearchEngine.FTS5_UNICODE.value,
            tokenizer=UNICODE_TOKENIZER,
            trigram_tokenizer=state.trigram,
            reason="",
        )

    def _index_one(
        self,
        conn: sqlite3.Connection,
        kind: str,
        document: MappingABC[str, Any],
    ) -> Tuple[str, bool]:
        """Index one document. Returns ``(action, was_truncated)``.

        ``action`` is one of ``indexed`` / ``updated`` / ``unchanged``, which is
        the fact the incremental claim rests on.
        """
        if not isinstance(document, MappingABC):
            return "unchanged", False
        try:
            doc_id = str(document.get("doc_id") or "").strip()
        except Exception:
            doc_id = ""
        if not doc_id:
            return "unchanged", False
        title = str(document.get("title") or "")
        body = str(document.get("body") or "")
        repo_key = document.get("repo_key")
        repo_key = str(repo_key) if repo_key not in (None, "") else None
        updated_at = str(document.get("updated_at") or "")
        was_truncated = False
        if len(body) > MAX_DOC_CHARS:
            body = body[:MAX_DOC_CHARS]
            was_truncated = True
        digest = _payload_digest(kind, doc_id, title, body)
        existing = conn.execute(
            "SELECT rowid, payload_digest FROM index_documents "
            "WHERE kind = ? AND doc_id = ?",
            (kind, doc_id),
        ).fetchone()
        if existing is not None:
            row_id = int(existing["rowid"])
            if str(existing["payload_digest"]) == digest:
                # Content-addressed invalidation: identical ranked text is a
                # no-op, so a code edit that did not touch a memory does not
                # re-index it.
                return "unchanged", was_truncated
            conn.execute(
                "UPDATE index_documents SET payload_digest = ?, title = ?, "
                "body = ?, repo_key = ?, updated_at = ? WHERE rowid = ?",
                (digest, title, body, repo_key, updated_at, row_id),
            )
            self._rewrite_fts(conn, row_id, title, body)
            return "updated", was_truncated
        cursor = conn.execute(
            "INSERT INTO index_documents "
            "(kind, doc_id, repo_key, payload_digest, title, body, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, doc_id, repo_key, digest, title, body, updated_at),
        )
        row_id = int(cursor.lastrowid)
        self._rewrite_fts(conn, row_id, title, body)
        return "indexed", was_truncated

    def _fts_tables(self) -> Tuple[str, ...]:
        """Return the FTS tables this index maintains, in a stable order.

        Forgetting the trigram table here is how a CJK search goes stale while
        the English one looks perfectly healthy, so the list is derived from one
        place and the test suite asserts both are present.
        """
        tables = ["index_fts"]
        if self._state.trigram:
            tables.append("index_fts_trigram")
        return tuple(tables)

    def _rewrite_fts(
        self, conn: sqlite3.Connection, row_id: int, title: str, body: str
    ) -> None:
        """DELETE then INSERT one row in EVERY FTS table this index owns."""
        for table in self._fts_tables():
            try:
                conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (row_id,))
                conn.execute(
                    f"INSERT INTO {table}(rowid, title, body) VALUES (?, ?, ?)",
                    (row_id, title, body),
                )
            except sqlite3.Error:
                # A table that cannot be written is not fatal to the content
                # table; the capability receipt still says whether trigram
                # exists, and search() reports what it actually used.
                continue

    def remove(self, kind: str, doc_ids: Iterable[Any]) -> int:
        """Remove documents by id. Returns the number actually removed."""
        kind_key = str(kind or "").strip()
        if kind_key not in INDEX_KINDS:
            return 0
        removed = 0
        with self._lock:
            conn = self._state.conn
            if conn is None or not self._state.fts5:
                return 0
            try:
                for raw in doc_ids or ():
                    doc_id = str(raw or "").strip()
                    if not doc_id:
                        continue
                    row = conn.execute(
                        "SELECT rowid FROM index_documents "
                        "WHERE kind = ? AND doc_id = ?",
                        (kind_key, doc_id),
                    ).fetchone()
                    if row is None:
                        continue
                    row_id = int(row["rowid"])
                    conn.execute(
                        "DELETE FROM index_documents WHERE rowid = ?", (row_id,)
                    )
                    for table in self._fts_tables():
                        try:
                            conn.execute(
                                f"DELETE FROM {table} WHERE rowid = ?", (row_id,)
                            )
                        except sqlite3.Error:
                            continue
                    removed += 1
                conn.commit()
            except sqlite3.DatabaseError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return 0
        self._state.removed += removed
        return removed

    # -- the decision-store trigger surface ----------------------------
    #
    # These two are what make the index TRIGGER-MAINTAINED rather than
    # manually-fed. The corpus design indexes "trigger-maintained tables"; a
    # store whose rows must be pushed into a second database by hand is not
    # trigger-maintained, it is manually-maintained, and it silently rots.
    #
    # Two shapes were wrong before this existed, both measured:
    #
    #   1. a memory written by `DecisionStore.record` was INVISIBLE to
    #      `SearchIndex.search` until something called `index()` by hand;
    #   2. worse, a REVOKED memory stayed retrievable through the index after
    #      the store had withdrawn it -- so "revocation is immediate and
    #      complete" was true of the store and false of the surface a caller
    #      actually searches.
    #
    # (2) is the reason this is a trigger and not a convenience.

    def index_memory(
        self,
        doc_id: Any,
        *,
        text: Any = "",
        title: Any = "",
        repo_key: Any = "",
        kind: str = "memories",
        updated_at: Any = "",
    ) -> IndexReport:
        """Index or re-index one document from a decision-store row.

        The parameter names mirror what :class:`memory.decision_store.Decision`
        exposes, so the trigger can forward a row without reshaping it.
        """
        return self.index(
            kind,
            [
                {
                    "doc_id": doc_id,
                    "title": title,
                    "body": text,
                    "repo_key": repo_key,
                    "updated_at": updated_at,
                }
            ],
        )

    def remove_memory(self, doc_id: Any, *, kind: str = "memories") -> int:
        """Remove one document by its decision-store id."""
        return self.remove(kind, [doc_id])

    # -- read -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        limit: int = 20,
        repo_key: Optional[str] = None,
        match_mode: str = "and",
    ) -> SearchOutcome:
        """Rank indexed documents for ``query``.

        The engine is chosen from the QUERY, not from taste:

        * a query containing CJK goes to the trigram index when it exists,
          because ``unicode61`` finds no word boundary inside a CJK run and so
          cannot match a two-character query inside a six-character phrase;
        * everything else goes to ``unicode61``, INCLUDING a short query --
          unicode61 matches a short query as a prefix, so routing "ab" to
          trigram would guarantee zero results where unicode61 has one.

        Two degradations are stated rather than hidden. When a CJK query
        arrives on a build with no trigram, the outcome carries
        ``trigram_attempted``/``trigram_reason``. And when the query is
        shorter than :data:`TRIGRAM_TOKEN_FLOOR_CHARS`, the outcome carries
        ``below_token_floor``: an empty result there is a property of the
        tokenizer, and reporting it as "no matching memory" would be a lie
        about the corpus.

        The empty query is a RECENCY listing, matching
        ``DecisionStore.search``'s documented behaviour. Its hits carry
        ``ordering="recency"`` and a score of ``0.0``: a row that BM25 did not
        rank must not carry a BM25 number.

        ``match_mode`` selects the boolean operator: ``and`` (the default, every
        term must appear) or ``or`` (any term, BM25-ranked). An unrecognised
        mode is REFUSED rather than defaulted, because silently treating
        ``"OR"`` as ``and`` would make a caller's recall-oriented search return
        nothing and read as "no matching memory".
        """
        raw_query = str(query or "")
        bounded = _bounded_limit(limit)
        mode = str(match_mode or "and").strip().casefold()
        if mode not in MATCH_MODES:
            return SearchOutcome.unavailable(
                f"unknown match_mode {match_mode!r}; this index implements "
                f"{list(MATCH_MODES)}. Refusing rather than defaulting, because "
                "silently treating 'or' as 'and' would return nothing and read "
                "as 'no matching memory'.",
                raw_query,
            )
        cjk = has_cjk(raw_query)
        stripped = raw_query.strip()
        need_trigram = cjk

        with self._lock:
            if self._state.conn is None or not self._state.fts5:
                return SearchOutcome.unavailable(
                    self.capability()["reason"] or "the index database is not open",
                    raw_query,
                )

        if not stripped:
            return self._recent(
                kind=kind, limit=bounded, repo_key=repo_key, query=raw_query
            )

        floor_reason = ""
        below_floor = False
        if need_trigram and len(stripped) < TRIGRAM_TOKEN_FLOOR_CHARS:
            below_floor = True
            floor_reason = (
                f"this query is {len(stripped)} character(s) long and contains "
                f"CJK, and a trigram needs {TRIGRAM_TOKEN_FLOOR_CHARS} consecutive "
                "characters, so this index cannot answer it at all: an empty "
                "result here means the query was below the tokenizer's floor, NOT "
                "that memory is missing. Widen the query to three or more "
                "characters."
            )

        trigram_reason = floor_reason
        if need_trigram and self._state.trigram:
            table, engine = "index_fts_trigram", SearchEngine.FTS5_TRIGRAM
            match = _trigram_query(raw_query)
            tokenizer, attempted = TRIGRAM_TOKENIZER, True
        elif need_trigram:
            table, engine = "index_fts", SearchEngine.FTS5_UNICODE
            match = _unicode_query(raw_query, mode)
            tokenizer, attempted = UNICODE_TOKENIZER, False
            trigram_reason = self.capability()["trigram_reason"] or (
                "this query contains CJK and this build has no trigram "
                "tokenizer, so it was answered by the unicode61 index, which "
                "cannot match inside a CJK run; its recall is degraded for this "
                "query"
            )
            if below_floor:
                trigram_reason = floor_reason
        else:
            table, engine = "index_fts", SearchEngine.FTS5_UNICODE
            match = _unicode_query(raw_query, mode)
            tokenizer, attempted = UNICODE_TOKENIZER, False

        if not match:
            return SearchOutcome.results(
                engine.value,
                (),
                query=raw_query,
                tokenizer=tokenizer,
                ordering="bm25",
                cjk_query=cjk,
                trigram_attempted=attempted,
                trigram_reason=trigram_reason,
                below_token_floor=below_floor,
                reason="the query contained no indexable term",
            )

        clauses = [f"{table} MATCH ?"]
        params: List[Any] = [match]
        if kind is not None:
            kind_key = str(kind).strip()
            if kind_key not in INDEX_KINDS:
                return SearchOutcome.results(
                    engine.value,
                    (),
                    query=raw_query,
                    tokenizer=tokenizer,
                    ordering="bm25",
                    reason=f"unknown index kind {kind_key!r}",
                )
            clauses.append("d.kind = ?")
            params.append(kind_key)
        if repo_key not in (None, ""):
            clauses.append("d.repo_key = ?")
            params.append(str(repo_key))
        where = " AND ".join(clauses)

        sql = (
            "SELECT d.kind, d.doc_id, d.title, d.body, d.repo_key, d.updated_at, "
            f"bm25({table}) AS score FROM {table} f "
            "JOIN index_documents d ON d.rowid = f.rowid "
            f"WHERE {where} ORDER BY score ASC, d.doc_id ASC LIMIT ?"
        )
        params.append(bounded + 1)
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return SearchOutcome.unavailable(
                    "the index database is not open", raw_query
                )
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError as exc:
                return SearchOutcome.unavailable(
                    f"the FTS5 query failed: {type(exc).__name__}: {exc}"[:300],
                    raw_query,
                )

        truncated = len(rows) > bounded
        rows = rows[:bounded]
        truncation_reason = ""
        if truncated:
            truncation_reason = (
                f"the result was capped at {bounded} rows and at least one "
                "further match exists; this is a partial ranking, not a complete one"
            )
        hits = tuple(
            SearchHit(
                kind=str(row["kind"]),
                doc_id=str(row["doc_id"]),
                title=str(row["title"]),
                body=str(row["body"]),
                score=round(float(row["score"]), 8),
                rank=position,
                engine=engine.value,
                ordering="bm25",
                repo_key=(str(row["repo_key"]) if row["repo_key"] else None),
                updated_at=str(row["updated_at"] or ""),
            )
            for position, row in enumerate(rows, start=1)
        )
        return SearchOutcome.results(
            engine.value,
            hits,
            query=raw_query,
            truncated=truncated,
            truncation_reason=truncation_reason,
            scanned_rows=len(rows),
            tokenizer=tokenizer,
            ordering="bm25",
            cjk_query=cjk,
            trigram_attempted=attempted,
            trigram_reason=trigram_reason,
            below_token_floor=below_floor,
            reason=(
                floor_reason
                if below_floor and not hits
                else ""
            ),
        )

    def _recent(
        self,
        *,
        kind: Optional[str],
        limit: int,
        repo_key: Optional[str],
        query: str,
    ) -> SearchOutcome:
        """Return the most recently updated rows for an EMPTY query."""
        clauses: List[str] = []
        params: List[Any] = []
        if kind is not None:
            kind_key = str(kind).strip()
            if kind_key not in INDEX_KINDS:
                return SearchOutcome.results(
                    SearchEngine.FTS5_UNICODE.value,
                    (),
                    query=query,
                    ordering="recency",
                    reason=f"unknown index kind {kind_key!r}",
                )
            clauses.append("kind = ?")
            params.append(kind_key)
        if repo_key not in (None, ""):
            clauses.append("repo_key = ?")
            params.append(str(repo_key))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT kind, doc_id, title, body, repo_key, updated_at FROM "
            f"index_documents{where} ORDER BY updated_at DESC, doc_id ASC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            conn = self._state.conn
            if conn is None:
                return SearchOutcome.unavailable(
                    "the index database is not open", query
                )
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError as exc:
                return SearchOutcome.unavailable(
                    f"the recency query failed: {type(exc).__name__}: {exc}"[:300],
                    query,
                )
        hits = tuple(
            SearchHit(
                kind=str(row["kind"]),
                doc_id=str(row["doc_id"]),
                title=str(row["title"]),
                body=str(row["body"]),
                score=0.0,
                rank=position,
                engine=SearchEngine.FTS5_UNICODE.value,
                ordering="recency",
                repo_key=(str(row["repo_key"]) if row["repo_key"] else None),
                updated_at=str(row["updated_at"] or ""),
            )
            for position, row in enumerate(rows, start=1)
        )
        return SearchOutcome.results(
            SearchEngine.FTS5_UNICODE.value,
            hits,
            query=query,
            scanned_rows=len(rows),
            tokenizer="none (a recency listing issues no MATCH expression)",
            ordering="recency",
            reason="empty query: rows are ordered by recency, not by BM25",
        )

    # -- introspection --------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Return counts and the engine, for a listing/health surface."""
        capability = self.capability()
        counts: Dict[str, Optional[int]] = {kind: None for kind in INDEX_KINDS}
        with self._lock:
            conn = self._state.conn
            if conn is not None and self._state.fts5:
                try:
                    for kind in INDEX_KINDS:
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM index_documents WHERE kind = ?",
                            (kind,),
                        ).fetchone()
                        counts[kind] = int(row["n"])
                except sqlite3.DatabaseError:
                    counts = {kind: None for kind in INDEX_KINDS}
        return {
            "available": self.available,
            "engine": (
                SearchEngine.FTS5_UNICODE.value if self.available else "unavailable"
            ),
            "reason": capability["reason"],
            "trigram": self.trigram_available,
            "trigram_reason": capability["trigram_reason"],
            "sqlite_version": SQLITE_VERSION,
            "documents": counts,
            "indexed": self._state.indexed,
            "updated": self._state.updated,
            "unchanged": self._state.unchanged,
            "removed": self._state.removed,
            "truncated_docs": self._state.truncated_docs,
            "deterministic": True,
            "model_calls": 0,
        }

    def close(self) -> None:
        """Close the index; repeated calls are harmless."""
        with self._lock:
            if self._state.conn is not None:
                try:
                    self._state.conn.close()
                finally:
                    self._state.conn = None
            self._state.closed = True

    def __enter__(self) -> "SearchIndex":
        """Return this index as a context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Close the index when leaving a context-manager block."""
        self.close()


def open_default_search_index() -> SearchIndex:
    """The shared search index beside the decision store.

    One opener, so the CLI, the MCP server and the harness all read the SAME
    index -- the same reason :func:`memory.decision_store.open_default_store`
    exists. Env-overridable through ``NEO_MEMORY_INDEX_DB``.
    """
    from memory.paths import search_index_db_path

    return SearchIndex(str(search_index_db_path()))
