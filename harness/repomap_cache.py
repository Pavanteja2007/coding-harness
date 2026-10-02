# SPDX-License-Identifier: Apache-2.0
#
# Vendored from Aider (https://github.com/Aider-AI/aider), file
# `aider/repomap.py` and its SQLite cache, commit pin
# `1cc4199c1b7bd7a2e0d94bd47b7b0d0b4d34d5b1` (v0.86.0 tag line).
#
# WHY IT BEATS OURS - the mandatory field, and the reason this file exists.
# `harness/retrieval.py` already has weighted PageRank over the tree-sitter
# index, and it is a good ranker. What it did NOT have was a cache whose
# invalidation is per-FILE. `memory.code_graph.load_or_build` is
# content-digest-keyed but rebuilds the WHOLE index when any file changes, so on
# a working repository - where something changes on almost every turn - the
# cache's hit rate on the thing people actually do is close to zero. That is
# the defect this module is for, and it is a *latency* defect rather than a
# *ranking* defect: Aider's repo map is the corpus's only ranked repository map
# with a production disk cache, and its `RepoCache` is the part we were missing.
#
# WHAT WAS TAKEN AND WHAT WAS WRITTEN. Nothing is copied verbatim. This module
# is an independent implementation of Aider's CACHE DESIGN - a SQLite
# diskcache keyed by content digest, versioned, with garbage collection -
# written against this repository's own `harness/retrieval.py` symbol records
# and this repository's own honesty rules. The attribution above is for the
# design lineage, which is what a reader auditing provenance needs; there is no
# third-party code in this file to license.
#
# THE DESIGN POINTS BORROWED, and the one that mattered most:
#   * SQLite rather than a pickle-per-file store, so a cache is one file with
#     transactional writes instead of thousands that can each be half-written.
#   * A SCHEMA VERSION, so a cache written by an older layout is REFUSED and
#     rebuilt rather than misread. Aider's is v4; ours is too, for the same
#     reason, and the refusal is a refusal rather than a best-effort parse.
#   * Content digest as the key, so an edit invalidates exactly one entry.
#
# THE DESIGN POINT THAT WAS NOT BORROWED, deliberately: Aider trusts its own
# cache unconditionally. This repository does not, because
# `phases/DOCTRINE.md` §1 forbids rendering an unknown measurement as a
# measured one. Hence `UNAVAILABLE` as a first-class hit value, which must
# never be rendered as `miss` and never as `0%`. A cache that cannot be read is
# a different fact from a cache that has no entry, and a latency bar that
# cannot see the difference will be reported as green forever.

"""Per-FILE content-addressed disk cache for the ranked repository map.

What this fixes
---------------
``memory.code_graph`` already persists an index and already keys it on content
digests, but it rebuilds the **whole** index when **any** file changes. On a
repository under active edit - which is the only kind this harness runs on -
that is a cache whose hit rate on the access pattern that matters is
approximately zero. This module caches the per-file symbol extraction
individually, so editing one file re-derives one file.

The four properties, in the order they matter
---------------------------------------------
1. **Per-content-digest invalidation.** One row per ``(relative path, sha256)``.
   A file whose bytes are unchanged is never re-extracted, whatever happened
   anywhere else in the tree. A cache that invalidates wholesale is not a
   cache; it is a latency spike with extra steps.
2. **Bounded, with GC that REPORTS.** ``max_bytes`` is a ceiling, eviction is
   least-recently-used, and every collection returns what it removed. A store
   with no GC grows without limit, and the repo's own doctrine flags that class.
3. **An honest hit vocabulary.** :data:`HIT`, :data:`PARTIAL`, :data:`MISS`,
   :data:`UNAVAILABLE` are four different facts. ``UNAVAILABLE`` is never
   ``MISS`` and never ``0%``.
4. **Cold and warm are measured separately.** :class:`CacheReport` separates
   ``cold_s`` from the warm figures, because quoting a warm number as "the
   retrieval cost" is the dishonest measurement this round exists to prevent.

Public surface: :class:`RepoMapCache`, :class:`CacheReport`, :func:`open_cache`,
:func:`cache_path_for`, the four hit constants, and :func:`render_hit`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROWS",
    "HIT",
    "HIT_VALUES",
    "MISS",
    "PARTIAL",
    "SCHEMA_VERSION",
    "UNAVAILABLE",
    "CacheReport",
    "RepoMapCache",
    "cache_path_for",
    "file_digest",
    "open_cache",
    "render_hit",
]

#: v4, matching Aider's diskcache generation. A cache written by a DIFFERENT
#: layout is refused and rebuilt, never best-effort parsed: a misread cache
#: returns wrong symbols, and wrong symbols are worse than no symbols.
SCHEMA_VERSION = 4

#: Every requested entry was served from the cache.
HIT = "hit"
#: Some entries were served and some had to be re-derived. A distinct value, not
#: a fractional ``hit``: after a one-file edit, almost every search is PARTIAL,
#: and reporting that as 0.99 hit would be a rounding lie.
PARTIAL = "partial"
#: The cache is readable and has no entry for what was asked.
MISS = "miss"
#: The cache could not be read, written, or is the wrong schema. NEVER
#: rendered as ``miss`` and never as ``0%`` - "we could not look" and "there
#: was nothing there" are different answers and only one of them is a fact.
UNAVAILABLE = "unavailable"

HIT_VALUES: Tuple[str, ...] = (HIT, PARTIAL, MISS, UNAVAILABLE)

#: 256 MiB. Large enough that a real repository's whole map fits; small enough
#: that a runaway symbol extractor cannot fill a user's disk.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
#: A row cap as well as a byte cap, so a repository of many tiny files cannot
#: evade the byte ceiling with per-row overhead.
DEFAULT_MAX_ROWS = 400_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    rel      TEXT NOT NULL,
    digest   TEXT NOT NULL,
    payload  TEXT NOT NULL,
    nbytes   INTEGER NOT NULL,
    used_at  REAL NOT NULL,
    PRIMARY KEY (rel, digest)
);
CREATE INDEX IF NOT EXISTS entries_used_at ON entries (used_at);
CREATE INDEX IF NOT EXISTS entries_rel ON entries (rel);
"""


def render_hit(value: str) -> str:
    """Render a hit value for a human, refusing to flatten the vocabulary.

    ``UNAVAILABLE`` renders as a statement about the CACHE and never as a
    percentage, because a percentage would be a number nobody measured.
    """
    text = str(value or "").strip().lower()
    if text == HIT:
        return "cache hit: every entry was already derived"
    if text == PARTIAL:
        return "cache partial: some entries were re-derived"
    if text == MISS:
        return "cache miss: no entry for what was asked"
    if text == UNAVAILABLE:
        return "cache unavailable: the cache could not be read or written"
    return f"cache status unknown: {value!r}"


def file_digest(path: Path) -> str:
    """SHA-256 of a file's BYTES, or ``""`` when it cannot be read.

    Content, not ``(size, mtime)``: ``memory/AGENTS.md`` already documents the
    stat-reuse blind spot - a same-size edit that preserves ``mtime_ns`` is
    invisible to a stat check - and a cache that misses that edit returns stale
    symbols, which is a wrong answer rather than a slow one.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def cache_path_for(repo_path: str, cache_root: Optional[str] = None) -> Path:
    """The cache file for a repository, OUTSIDE the repository.

    The never-mutate guarantee depends on this: a cache inside the tree would
    be a file the agent could read, edit, or be diffed against. Keyed on the
    canonical absolute path so two checkouts do not share an index.
    """
    try:
        canonical = os.path.normcase(os.path.abspath(str(repo_path)))
    except (OSError, ValueError):
        canonical = str(repo_path or ".")
    token = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]
    if cache_root:
        root = Path(cache_root)
    else:
        configured = (
            os.environ.get("NEO_REPOMAP_CACHE_DIR")
            or os.environ.get("NEO_HOME")
            or os.environ.get("HARNESS_HOME")
        )
        root = (
            Path(configured) / "repomap"
            if configured
            else Path.home() / ".config" / "neo" / "repomap"
        )
    return root / f"repo-{token}.sqlite3"


@dataclass
class CacheReport:
    """What one cache interaction did, and what it could not do.

    ``hit`` is one of :data:`HIT_VALUES`. ``cold_s`` is separated from
    ``warm_s`` on purpose: a cold build and a warm read are different
    measurements, and averaging them (or quoting the warm one as "the cost")
    is the dishonest number this round exists to prevent.
    """

    hit: str = MISS
    requested: int = 0
    served: int = 0
    derived: int = 0
    evicted_rows: int = 0
    evicted_bytes: int = 0
    gc_ran: bool = False
    bytes_used: int = 0
    rows: int = 0
    cold_s: float = 0.0
    warm_s: float = 0.0
    reason: str = ""
    path: str = ""

    @property
    def complete(self) -> bool:
        """Whether every requested entry came from the cache."""
        return self.hit == HIT

    @property
    def honest_hit_rate(self) -> Optional[float]:
        """The served fraction, or ``None`` when nothing was requested.

        ``None`` and ``0.0`` are different answers: a ratio with a zero
        denominator is not a percentage anybody should act on. An UNAVAILABLE
        cache also reports ``None`` rather than ``0.0``, because it served
        nothing for a reason that has nothing to do with the entries.
        """
        if self.hit == UNAVAILABLE or self.requested <= 0:
            return None
        return round(self.served / float(self.requested), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hit": self.hit,
            "requested": int(self.requested),
            "served": int(self.served),
            "derived": int(self.derived),
            "hit_rate": self.honest_hit_rate,
            "hit_rate_is_measured": self.hit != UNAVAILABLE,
            "evicted_rows": int(self.evicted_rows),
            "evicted_bytes": int(self.evicted_bytes),
            "gc_ran": bool(self.gc_ran),
            "bytes_used": int(self.bytes_used),
            "rows": int(self.rows),
            "cold_s": round(float(self.cold_s), 4),
            "warm_s": round(float(self.warm_s), 4),
            "reason": self.reason,
            "path": self.path,
            "rendered": render_hit(self.hit),
        }


class RepoMapCache:
    """A bounded, per-content-digest SQLite cache for symbol extraction.

    Assumes nothing about the repository. Every public method degrades to a
    typed report and never raises: a cache that can end a run is a cache whose
    failure mode is worse than having no cache at all.

    Args:
        repo_path: the repository the entries belong to.
        cache_path: where the SQLite file lives. Defaults outside the tree.
        max_bytes: byte ceiling; GC runs when a WRITE pushes past it.
        max_rows: row ceiling, so per-row overhead cannot evade the byte cap.
    """

    def __init__(
        self,
        repo_path: str = ".",
        *,
        cache_path: Optional[str] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.repo_path = str(repo_path or ".")
        self.path = Path(cache_path) if cache_path else cache_path_for(self.repo_path)
        self.max_bytes = max(1, int(max_bytes or DEFAULT_MAX_BYTES))
        self.max_rows = max(1, int(max_rows or DEFAULT_MAX_ROWS))
        self._conn: Optional[sqlite3.Connection] = None
        self._unavailable = ""
        #: Reported once, so a consumer can say "GC ran" without a second call.
        self.last_gc: Dict[str, Any] = {"ran": False, "rows": 0, "bytes": 0}

    # -- lifecycle --------------------------------------------------------
    def _connect(self) -> Optional[sqlite3.Connection]:
        """Open and migrate, or record WHY it is unavailable and return None."""
        if self._conn is not None:
            return self._conn
        if self._unavailable:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if row is None:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.commit()
            else:
                found = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                version = int(found[0]) if found else 0
                if version != SCHEMA_VERSION:
                    # A different layout is REFUSED, not parsed. A best-effort
                    # read of a v3 table would return wrong symbols, and wrong
                    # symbols are a wrong answer rather than a slow one.
                    conn.close()
                    self._unavailable = (
                        f"the cache at {self.path} is schema v{version}; this "
                        f"build speaks v{SCHEMA_VERSION}. Rebuild it by deleting "
                        f"the file."
                    )
                    return None
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._unavailable = (
                f"the cache at {self.path} is not usable: {type(exc).__name__}: {exc}"
            )
            return None
        self._conn = conn
        return conn

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def __enter__(self) -> "RepoMapCache":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- reads ------------------------------------------------------------
    def lookup(self, rel: str, digest: str) -> Optional[Dict[str, Any]]:
        """Return the cached payload for ``(rel, digest)``, or ``None``.

        ``None`` means "no entry" OR "cache unavailable"; the caller cannot
        tell them apart from this alone, which is exactly why every caller in
        this module goes through :meth:`get_many`, which reports the
        difference.
        """
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT payload FROM entries WHERE rel = ? AND digest = ?",
                (str(rel), str(digest)),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            # A corrupt row is dropped rather than served. A cache that
            # returns half-parsed symbols is worse than one that misses.
            return None

    def get_many(
        self, wanted: Sequence[Tuple[str, str]]
    ) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], CacheReport]:
        """Look up many ``(rel, digest)`` pairs, and report the hit honestly.

        The report is the point. A caller that only receives the dict cannot
        distinguish a full hit from a partial one from a cache that was never
        readable, and those three lead to three different correct actions.
        """
        started = time.perf_counter()
        report = CacheReport(path=str(self.path), requested=len(wanted))
        if self._connect() is None:
            report.hit = UNAVAILABLE
            report.reason = self._unavailable
            report.cold_s = time.perf_counter() - started
            return {}, report
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for rel, digest in wanted:
            payload = self.lookup(rel, digest)
            if payload is not None:
                out[(rel, digest)] = payload
        report.served = len(out)
        report.derived = max(0, len(wanted) - len(out))
        if report.served == 0:
            report.hit = MISS
        elif report.derived == 0:
            report.hit = HIT
        else:
            report.hit = PARTIAL
        self._touch([key[0] for key in out])
        report.warm_s = time.perf_counter() - started
        # A READ does not collect. See `_measure`.
        self._measure(report, collect=False)
        return out, report

    # -- writes -----------------------------------------------------------
    def put(self, rel: str, digest: str, payload: Any) -> bool:
        """Store one entry. Returns whether it was written.

        A write failure is reported, never raised: losing a cache entry costs
        one re-derivation, and a run must not die for it.
        """
        conn = self._connect()
        if conn is None:
            return False
        try:
            blob = json.dumps(payload, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO entries "
                "(rel, digest, payload, nbytes, used_at) VALUES (?,?,?,?,?)",
                (str(rel), str(digest), blob, len(blob), time.time()),
            )
            conn.commit()
        except sqlite3.Error:
            return False
        self._measure(CacheReport(), collect=True)
        return True

    def put_many(self, items: Iterable[Tuple[str, str, Any]]) -> int:
        """Store many entries in one transaction. Returns how many landed."""
        conn = self._connect()
        if conn is None:
            return 0
        rows = []
        for rel, digest, payload in items:
            try:
                blob = json.dumps(payload, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                continue
            rows.append((str(rel), str(digest), blob, len(blob), time.time()))
        if not rows:
            return 0
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO entries "
                "(rel, digest, payload, nbytes, used_at) VALUES (?,?,?,?,?)",
                rows,
            )
            conn.commit()
        except sqlite3.Error:
            return 0
        self._measure(CacheReport(), collect=True)
        return len(rows)

    def _touch(self, rels: Iterable[str]) -> None:
        conn = self._conn
        if conn is None:
            return
        now = time.time()
        try:
            conn.executemany(
                "UPDATE entries SET used_at = ? WHERE rel = ?",
                [(now, str(rel)) for rel in rels],
            )
            conn.commit()
        except sqlite3.Error:
            pass

    # -- bounded GC -------------------------------------------------------
    def collect(self, *, dry_run: bool = False) -> Dict[str, Any]:
        """Evict least-recently-used rows until both ceilings are met.

        Returns what it removed. A GC whose result nobody can read is a GC
        nobody can trust to have run, so this is a VALUE and not a log line.

        The keys are named so that ``rows``/``bytes`` mean ONE thing - what was
        REMOVED - and the store's size is always ``rows_in_store`` /
        ``bytes_in_store``. An earlier version reused ``rows`` for both, so a
        dry run reported the row count it had *not* removed, and the receipt
        read as though six rows had been evicted when none had. A dry run that
        claims an eviction is worse than no dry run.
        """
        conn = self._connect()
        out: Dict[str, Any] = {
            "ran": False,
            "rows": 0,
            "bytes": 0,
            "rows_in_store": 0,
            "bytes_in_store": 0,
            "dry_run": bool(dry_run),
            "path": str(self.path),
            "reason": self._unavailable,
        }
        if conn is None:
            self.last_gc = dict(out)
            return out
        try:
            used, rows = conn.execute(
                "SELECT COALESCE(SUM(nbytes),0), COUNT(*) FROM entries"
            ).fetchone()
        except sqlite3.Error as exc:
            out["reason"] = f"could not measure the cache: {exc}"
            self.last_gc = dict(out)
            return out
        used = int(used or 0)
        rows = int(rows or 0)
        out["bytes_in_store"] = used
        out["rows_in_store"] = rows
        if not (used > self.max_bytes or rows > self.max_rows):
            out["reason"] = "already under both ceilings; nothing to collect"
            self.last_gc = dict(out)
            return out
        if dry_run:
            out.update(
                {
                    "ran": True,
                    "reason": (
                        f"over by bytes={used - self.max_bytes:+d} "
                        f"rows={rows - self.max_rows:+d}; nothing removed "
                        f"(dry run)"
                    ),
                }
            )
            self.last_gc = dict(out)
            return out
        # Evict a generous LRU batch, then stop as soon as BOTH ceilings are
        # met - the loop condition is the bound, not a guess at a row count.
        try:
            victims = conn.execute(
                "SELECT rel, digest, nbytes FROM entries ORDER BY used_at ASC",
            ).fetchall()
        except sqlite3.Error as exc:
            out["reason"] = f"could not select victims: {exc}"
            self.last_gc = dict(out)
            return out
        removed_rows = 0
        removed_bytes = 0
        for rel, digest, nbytes in victims:
            if used - removed_bytes <= self.max_bytes and (
                rows - removed_rows <= self.max_rows
            ):
                break
            try:
                conn.execute(
                    "DELETE FROM entries WHERE rel = ? AND digest = ?",
                    (rel, digest),
                )
            except sqlite3.Error:
                continue
            removed_rows += 1
            removed_bytes += int(nbytes or 0)
        try:
            conn.commit()
        except sqlite3.Error:
            pass
        out.update(
            {
                "ran": True,
                "rows": removed_rows,
                "bytes": removed_bytes,
                "bytes_in_store": max(0, used - removed_bytes),
                "rows_in_store": max(0, rows - removed_rows),
                "reason": (
                    f"evicted {removed_rows} row(s) / {removed_bytes} byte(s) "
                    f"(least recently used) to return under {self.max_bytes} "
                    f"bytes / {self.max_rows} rows"
                ),
            }
        )
        self.last_gc = dict(out)
        return out

    def _measure(self, report: CacheReport, *, collect: bool = False) -> None:
        """Fill in the size figures, and collect ONLY when asked.

        ``collect=False`` on the read path is a design decision, not an
        oversight: **a read must not delete.** A cache whose lookup can evict is
        a cache where reading is a mutation, so a reader timing a warm hit is
        also silently changing what the next reader will get. Collection happens
        where the store actually grows - ``put`` / ``put_many`` / ``stats`` -
        and :meth:`collect` is public for an operator.
        """
        conn = self._conn
        if conn is None:
            return
        try:
            used, rows = conn.execute(
                "SELECT COALESCE(SUM(nbytes),0), COUNT(*) FROM entries"
            ).fetchone()
        except sqlite3.Error:
            return
        report.bytes_used = int(used or 0)
        report.rows = int(rows or 0)
        if not collect:
            return
        if report.bytes_used > self.max_bytes or report.rows > self.max_rows:
            collected = self.collect()
            report.gc_ran = bool(collected.get("ran"))
            report.evicted_rows = int(collected.get("rows") or 0)
            report.evicted_bytes = int(collected.get("bytes") or 0)
            # RE-MEASURE after eviction. Reporting the pre-GC row count next to
            # "evicted 7 rows" is a receipt that contradicts itself, and a
            # reader checking `rows <= max_rows` would see a violation the
            # collection had already fixed.
            try:
                used, rows = conn.execute(
                    "SELECT COALESCE(SUM(nbytes),0), COUNT(*) FROM entries"
                ).fetchone()
            except sqlite3.Error:
                return
            report.bytes_used = int(used or 0)
            report.rows = int(rows or 0)
            if collected.get("reason"):
                report.reason = str(collected["reason"])

    def stats(self) -> Dict[str, Any]:
        """Cache size and hit statistics, for a trace row or a status line."""
        report = CacheReport(path=str(self.path))
        if self._connect() is None:
            report.hit = UNAVAILABLE
            report.reason = self._unavailable
            return report.to_dict()
        self._measure(report, collect=True)
        report.hit = HIT
        return report.to_dict()

    def clear(self) -> bool:
        """Remove every entry. Used by a test and by a deliberate rebuild."""
        conn = self._connect()
        if conn is None:
            return False
        try:
            conn.execute("DELETE FROM entries")
            conn.commit()
        except sqlite3.Error:
            return False
        return True


def open_cache(
    repo_path: str,
    *,
    cache_path: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> RepoMapCache:
    """Open (or construct) the cache for a repository. Never raises."""
    return RepoMapCache(
        repo_path,
        cache_path=cache_path,
        max_bytes=max_bytes,
        max_rows=max_rows,
    )
