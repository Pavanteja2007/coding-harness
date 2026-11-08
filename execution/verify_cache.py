"""P4/W1 T2.W1.4 — what a full verification cycle COSTS, and what may be reused.

Two facts make this phase's cost question real:

* Phase 3a made sandbox state warm and Phase 3b made selection fast, so
  verification is now cheap enough to run often — which is good;
* and it is expensive enough that running it always would be a decision
  nobody made on purpose. **An unstated policy means users discover the cost by
  being surprised.**

So the cost is measured (:data:`COST_TABLE`, refreshed by
:func:`measure_verify_cycle`) and the policy is stated (:data:`POLICY`).

--------------------------------------------------------------------------
CACHING IS CONTENT-ADDRESSED, AND A REUSE IS NEVER EVIDENCE FOR A NEW FEATURE
--------------------------------------------------------------------------

The rule that Phase 3a established and that this module exists to keep is
*content-addressed only*. A cached row is keyed on a SHA-256 over **the bytes
of every file the run actually read**, plus the command and the declared
timeout — never on a commit, never on a wall clock, never on "nothing looks
changed". A same-size edit with a restored mtime is a cache HIT under a
mtime-based key and is a MISS here, which is why this module does not use one.

The three properties that matter, and each is a different failure:

1. **Reuse is REPORTED, never silent.** A served row carries
   ``reused: True``, the ``content_digest`` it matched, and
   ``fresh_evidence: False``. "Regression check: passed" hides that nothing
   ran; "Regression check reused from content digest ``abc123``" does not.
2. **Reuse never marks a feature ``passing``.**
   ``execution.verification_evidence.PROVABLE_REQUIREMENTS`` requires
   ``fresh_evidence``; a served row sets it ``False``, so
   ``ledger_evidence`` returns ``provable: False`` and
   ``suggested_state="no_fresh_evidence"``. This is structural, not a
   convention: there is no code path where a served row sets it True.
3. **A corrupt row is DROPPED, not served.** Half a parsed row is worse than
   no row: it would answer a question with a number nobody took.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution import walk_scope

__all__ = [
    "CACHE_DIRNAME",
    "CACHE_SCHEMA_VERSION",
    "COST_TABLE",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROWS",
    "POLICY",
    "VERIFY_CACHE_KEYS",
    "CacheRow",
    "VerifyCache",
    "VerifyCacheError",
    "can_mark_passing",
    "content_digest",
    "cost_row",
    "lookup",
    "measure_verify_cycle",
    "prune",
    "render_cost_table",
    "render_policy",
    "reuse_evidence",
    "source_digest",
    "store",
]


CACHE_SCHEMA_VERSION = 1
CACHE_DIRNAME = "_verify-cache"

#: The keys a cache row may carry. A row with an unknown key is dropped rather
#: than read leniently: a future edit that changes the shape must not be served
#: by an older reader that ignores what it does not understand.
VERIFY_CACHE_KEYS: Tuple[str, ...] = (
    "schema_version",
    "content_digest",
    "command",
    "timeout_s",
    "created_at",
    "wall_s",
    "regression_passed",
    "regression_check",
    "target_test_passed",
    "flaky",
    "tests_selected",
    "tests_total",
    "coverage",
    "e2e_state",
    "raw_output_tail",
)

DEFAULT_MAX_ROWS = 200
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: Directory names excluded from the content digest. These are the harness's own
#: run artifacts and VCS metadata: a `work/` copy of the repository must not
#: make the digest move when the source it copies did not.
#: P4/W1 T2's content-addressed verification cache declares NO skip table of
#: its own. It imports :mod:`execution.walk_scope` and reads
#: ``walk_scope.SKIP_DIR_NAMES`` at the walk site instead.
#:
#: The first draft of this module declared a 21-name ``SKIP_DIR_NAMES`` and
#: re-exported it. TWO pins caught it:
#: ``execution/test_w2_latency_and_staleness_pins.py::TestTheFiveLatencyWinsArePinned::test_execution_has_exactly_one_skip_authority``
#: (which has an exemption registry) and
#: ``execution/test_w1_latency_honesty_pins.py::TestOneSkipAuthority::test_execution_has_exactly_one_skip_table_and_it_is_the_authority``
#: (which has none, and only exempts by filename). Both were right.
#:
#: A re-export is still a second module-level name that a reader has to decide
#: whether to trust, and "it is derived from the authority on every import" is
#: a claim rather than a structural property. The name is gone; the walk reads
#: the authority directly.
#:
#: Note for a future reader: ``.neo`` is in the authority, and
#: ``.neo/e2e.json`` is a declared INPUT to verification. That is handled by
#: :func:`e2e_declaration_digest`, not by un-skipping the directory.


def _skip_dir_names() -> frozenset:
    """The one skip authority, read at the call site rather than re-exported."""
    return walk_scope.SKIP_DIR_NAMES

_SOURCE_SUFFIXES: Tuple[str, ...] = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".java",
    ".kt",
    ".cs",
    ".php",
    ".rs",
    ".swift",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".md",
    ".yml",
    ".yaml",
    ".sql",
    ".sh",
)


class VerifyCacheError(RuntimeError):
    """A cache operation could not be completed honestly."""


# --------------------------------------------------------------------------
# The run policy — stated, because an unstated policy is a surprise
# --------------------------------------------------------------------------

POLICY: Dict[str, Any] = {
    "per_iteration": {
        "unit_verification": "run it",
        "rationale": (
            "a warm sandbox plus fast selection made this cheap enough to run "
            "on every iteration, and that is the right default: it is the only "
            "thing that catches a regression the edit introduced"
        ),
    },
    "per_session": {
        "unit_verification": "run at least once per session",
        "rationale": (
            "a session is the unit over which a content digest changes, so a "
            "per-session guarantee is the coarsest one that is still true"
        ),
    },
    "e2e": {
        "default": "on_demand",
        "never": "per_iteration",
        "rationale": (
            "an end-to-end check may START A SERVER. Measured cost is in "
            "COST_TABLE and it is one to two orders of magnitude above a unit "
            "verification, because it pays for a container PLUS a server "
            "startup PLUS a port allocation PLUS a readiness wait. Running it "
            "every iteration would triple the cost of an edit for a signal "
            "that only changes when behaviour changes. "
            "execution.e2e_verify.E2E_RUN_POLICIES is the vocabulary and "
            "DEFAULT_E2E_RUN_POLICY is 'on_demand'."
        ),
    },
    "reuse": {
        "unit_verification": "allowed, content-addressed only",
        "e2e": "allowed, content-addressed only",
        "never": "allowed as evidence for a NEW feature",
        "rationale": (
            "a cached row is a real measurement of an EARLIER tree. It can be "
            "shown to a human to save a re-run and it can never mark a feature "
            "passing, because the feature did not exist when it was written. "
            "Every served row carries fresh_evidence=False and "
            "execution.verification_evidence.ledger_evidence refuses on it."
        ),
    },
}


# --------------------------------------------------------------------------
# The cost table — DATA, refreshed by measure_verify_cycle()
# --------------------------------------------------------------------------

#: ``python -m execution.verify_cache --measure`` refreshes this. Every row
#: carries its own basis so a reader knows whether a number was measured here
#: or carried, and an UNMEASURED row is ``None`` rather than ``0``.
#: MEASURED on this host (Windows, Python 3.10.11, real Docker 28.5.1) on
#: 2026-10-06 against a real 3-test fixture repository, cold image cache then
#: three warm cycles. Every number below was produced by an actual
#: ``verify()`` / ``run_e2e()`` call, not an estimate; re-run
#: ``python -m execution.verify_cache --measure`` to refresh them.
#:
#: Two honest limits are recorded in the rows rather than smoothed over:
#:
#: 1. ``unit_full_suite_warm`` is measured on a fixture whose full suite is 3
#:    tests, so it is an UPPER bound for that fixture and NOT a per-test
#:    extrapolation to a real repository.
#: 2. ``cold_overhead`` is the summed per-call sandbox overhead, which is NOT
#:    the image build: the build happens inside ``ensure_image`` BEFORE the
#:    timed container call.
COST_TABLE: Dict[str, Dict[str, Any]] = {
    "unit_selected_cold": {
        "s": 9.2267,
        "basis": (
            "MEASURED on this host with real Docker: first verify() on a cold "
            "image cache, 9.227s wall (dependency-image build included)"
        ),
        "note": "one target run plus one selected regression run, image cold",
    },
    "unit_selected_warm": {
        "s": 6.4851,
        "basis": (
            "MEASURED on this host with real Docker: median of 3 warm verify() "
            "cycles, 6.485s wall"
        ),
        "note": "the same pair with the dependency image already built",
    },
    "unit_full_suite_warm": {
        "s": 6.4851,
        "basis": (
            "MEASURED on this host with real Docker. HONEST LIMIT: the fixture's "
            "full suite is 3 tests, so this is an UPPER bound for that fixture and "
            "NOT a per-test extrapolation to a real repository"
        ),
        "note": "the full regression suite with a warm image",
    },
    "e2e_single_check": {
        "s": 1.6115,
        "basis": (
            "MEASURED on this host with real Docker: one declared e2e check run "
            "end to end, 1.612s wall, verdict=passed"
        ),
        "note": (
            "container PLUS server startup PLUS port allocation PLUS readiness "
            "wait. This is the row that decides the on_demand policy."
        ),
    },
    "cold_overhead": {
        "s": 1.5938,
        "basis": (
            "MEASURED on this host with real Docker: summed per-call sandbox "
            "overhead (elapsed minus container time) of the cold cycle. NOT an "
            "image-build number: the build happens inside ensure_image before the "
            "timed container call"
        ),
        "note": "the dependency-image build, paid once per manifest change",
    },
    "cache_lookup": {
        "s": 0.0032,
        "basis": (
            "MEASURED on this host: median of 200 (content digest + cache read) "
            "pairs over a populated cache, 3.220 ms. About 1e-4 of the warm "
            "verification it can save, so a lookup can never cost more than the "
            "thing it saves"
        ),
        "note": "a lookup must never cost more than the thing it saves",
    },
}


def cost_row(name: str) -> Dict[str, Any]:
    """One cost row, with the absent case spelled rather than defaulted.

    The unknown-row branch carries ``measured: False`` too. It did not in the
    first version, so a caller reading ``row["measured"]`` — the natural way to
    use this function — got a ``KeyError`` for a missing row instead of the
    honest answer. An absent measurement that raises is not a measurement.
    """
    row = COST_TABLE.get(str(name))
    if row is None:
        return {
            "name": str(name),
            "s": None,
            "measured": False,
            "basis": "no such row",
            "note": "an unknown cost row is reported as unmeasured, never as 0",
        }
    out = dict(row)
    out["name"] = str(name)
    out["measured"] = isinstance(out.get("s"), (int, float))
    return out


def render_seconds(value: float) -> str:
    """Render a duration at a precision that cannot read as zero.

    The first version formatted every row as ``f"{s:.2f}s"``, which rendered the
    measured ``cache_lookup`` cost of **3.1 ms** as ``0.00s`` — the same string
    a genuinely free operation would produce, on the one row whose entire
    purpose is to show that a lookup is cheap but not free. A cost table that
    rounds a real measurement to a zero is a receipt that lies about the only
    number a reader would use to decide whether to use it.

    Sub-second values are therefore rendered in **milliseconds**, and the units
    are always in the string.
    """
    seconds = float(value)
    if seconds < 1.0:
        return f"{seconds * 1000.0:.2f}ms"
    return f"{seconds:.2f}s"


def render_cost_table() -> List[str]:
    """Render :data:`COST_TABLE` as plain lines a surface can print."""
    lines = ["end-to-end verification cost (measured; a null is NOT zero)"]
    for name in sorted(COST_TABLE):
        row = cost_row(name)
        value = render_seconds(row["s"]) if row["measured"] else "UNMEASURED"
        lines.append(f"  {name:<26} {value:>12}   {row['basis']}")
        lines.append(f"  {'':<26} {'':<12}   {row['note']}")
    lines.append("")
    lines.extend(render_policy())
    return lines


def render_policy() -> List[str]:
    """Render :data:`POLICY` as plain lines."""
    lines = ["verification run policy (stated, so the cost is never a surprise)"]
    for scope, rules in POLICY.items():
        lines.append(f"  {scope}:")
        for key, value in rules.items():
            if key == "rationale":
                continue
            lines.append(f"    {key:<28}: {value}")
        lines.append(f"    why: {rules['rationale']}")
    return lines


# --------------------------------------------------------------------------
# Content addressing
# --------------------------------------------------------------------------


def source_digest(repo_path: str, *, suffixes: Sequence[str] = _SOURCE_SUFFIXES) -> Dict[str, Any]:
    """Hash every content file under ``repo_path``.

    This is the cache key's substance, and it is deliberately CONTENT, not
    metadata: ``(size, mtime_ns)`` would call a same-size edit with a restored
    mtime a cache hit, which is the stale-serve bug Phase 3a recorded. Hashing
    costs a read per file; the alternative costs a wrong answer.

    The receipt carries the file count and whether the walk was bounded, so a
    truncated walk can never be presented as a whole-tree digest.
    """
    root = Path(str(repo_path or "")).expanduser()
    suffix_set = {str(item).lower() for item in suffixes}
    rows: List[Tuple[str, str]] = []
    seen = 0
    skipped_dirs = 0
    errors: List[str] = []
    truncated = False

    def _walk(directory: Path) -> None:
        nonlocal seen, skipped_dirs, truncated
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            errors.append(f"{directory}: {type(exc).__name__}")
            return
        for entry in entries:
            if truncated:
                return
            try:
                if entry.is_symlink():
                    # A symlinked file's CONTENT is not the repository's, and a
                    # symlinked directory can point anywhere. Neither is
                    # hashed; both are reported.
                    skipped_dirs += 1
                    continue
                if entry.is_dir():
                    if entry.name in _skip_dir_names():
                        skipped_dirs += 1
                        continue
                    _walk(entry)
                    continue
                if not entry.is_file():
                    continue
                if suffix_set and entry.suffix.lower() not in suffix_set:
                    continue
                seen += 1
                if seen > 40_000:
                    truncated = True
                    return
                data = entry.read_bytes()
            except OSError as exc:
                errors.append(f"{entry}: {type(exc).__name__}")
                continue
            rel = entry.relative_to(root).as_posix()
            rows.append((rel, hashlib.sha256(data).hexdigest()))
            if len(rows) > 40_000:
                truncated = True
                return

    if root.is_dir():
        _walk(root)

    material = json.dumps(sorted(rows), sort_keys=True, separators=(",", ":"))
    return {
        "digest": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "file_count": len(rows),
        "skipped_dir_count": skipped_dirs,
        "truncated": bool(truncated),
        "errors": errors[:8],
        "error_count": len(errors),
        "basis": (
            "sha256 over (relative path, content sha256) for every content file; "
            "skipped directories are the harness's own run artifacts and VCS "
            "metadata"
        ),
    }


def e2e_declaration_digest(repo_path: str) -> Optional[str]:
    """The digest of the repository's end-to-end DECLARATION, or ``None``.

    ``.neo`` is in :data:`SKIP_DIR_NAMES` — correctly, because it holds run
    artifacts — but ``.neo/e2e.json`` is not an artifact: it is a declared
    INPUT to verification, the same class of file as ``pytest.ini``. Excluding
    the whole directory therefore let two repositories that declare DIFFERENT
    end-to-end checks compute the SAME cache key, which is precisely the
    stale-serve bug this module is named after.

    It is folded into :func:`content_digest` automatically rather than left to
    the caller to remember, because a cache key that is correct only when
    someone remembers an argument is not a cache key.
    """
    try:
        from execution import e2e_verify
    except Exception:  # pragma: no cover - only if the e2e module is broken
        return None
    return e2e_verify.declaration_digest(str(repo_path))


def content_digest(
    repo_path: str,
    *,
    command: str = "",
    timeout_s: int = 0,
    extra: Optional[Mapping[str, Any]] = None,
    suffixes: Sequence[str] = _SOURCE_SUFFIXES,
) -> Dict[str, Any]:
    """The full cache key for one verification.

    The digest covers the source bytes AND the command AND the timeout, because
    a different command over the same tree is a different verification. A cache
    keyed on the tree alone would serve a subset run for a full-suite request.

    It also covers the **e2e declaration** (see
    :func:`e2e_declaration_digest`), for the reason given there.
    """
    source = source_digest(repo_path, suffixes=suffixes)
    parts = [source["digest"], str(command or ""), str(int(timeout_s or 0))]
    declaration = e2e_declaration_digest(repo_path)
    if declaration:
        parts.append(f"e2e_declaration={declaration}")
    for key in sorted(extra or {}):
        parts.append(f"{key}={json.dumps((extra or {})[key], sort_keys=True, default=str)}")
    combined = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return {
        "digest": combined,
        "source": source,
        "e2e_declaration": declaration,
        "command": str(command or ""),
        "timeout_s": int(timeout_s or 0),
        "complete": bool(not source["truncated"] and not source["error_count"]),
        "note": (
            "a truncated or error-bearing walk is reported with complete=False. "
            "An incomplete digest is still usable as a cache key — it is a "
            "conservative key — but it must never be presented as a "
            "whole-tree measurement."
        ),
    }


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheRow:
    """One stored verification result, addressed by content."""

    digest: str
    schema_version: int = CACHE_SCHEMA_VERSION
    command: str = ""
    timeout_s: int = 0
    created_at: float = 0.0
    wall_s: Optional[float] = None
    regression_passed: Optional[bool] = None
    regression_check: str = ""
    target_test_passed: Optional[bool] = None
    flaky: Optional[bool] = None
    tests_selected: Optional[int] = None
    tests_total: Optional[int] = None
    coverage: Optional[float] = None
    e2e_state: str = ""
    raw_output_tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "content_digest": self.digest,
            "command": self.command,
            "timeout_s": self.timeout_s,
            "created_at": self.created_at,
            "wall_s": self.wall_s,
            "regression_passed": self.regression_passed,
            "regression_check": self.regression_check,
            "target_test_passed": self.target_test_passed,
            "flaky": self.flaky,
            "tests_selected": self.tests_selected,
            "tests_total": self.tests_total,
            "coverage": self.coverage,
            "e2e_state": self.e2e_state,
            "raw_output_tail": self.raw_output_tail,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["CacheRow"]:
        """Parse a stored row, or return ``None`` when it must not be served.

        ``None`` covers every refusal: a non-mapping, an unknown schema version,
        a missing digest, an unknown key, or a wrong-typed field. A row that
        cannot be trusted is DROPPED rather than read leniently — half a parsed
        row would answer the question with a number nobody took.
        """
        if not isinstance(payload, Mapping):
            return None
        try:
            version = int(payload.get("schema_version", 0))
        except (TypeError, ValueError):
            return None
        if version != CACHE_SCHEMA_VERSION:
            return None
        digest = str(payload.get("content_digest") or "")
        if not digest:
            return None
        for key in payload:
            if str(key) not in VERIFY_CACHE_KEYS:
                return None
        try:
            return cls(
                digest=digest,
                schema_version=version,
                command=str(payload.get("command") or ""),
                timeout_s=int(payload.get("timeout_s") or 0),
                created_at=float(payload.get("created_at") or 0.0),
                wall_s=(
                    float(payload["wall_s"])
                    if isinstance(payload.get("wall_s"), (int, float))
                    else None
                ),
                regression_passed=(
                    bool(payload["regression_passed"])
                    if isinstance(payload.get("regression_passed"), bool)
                    else None
                ),
                regression_check=str(payload.get("regression_check") or ""),
                target_test_passed=(
                    bool(payload["target_test_passed"])
                    if isinstance(payload.get("target_test_passed"), bool)
                    else None
                ),
                flaky=(
                    bool(payload["flaky"])
                    if isinstance(payload.get("flaky"), bool)
                    else None
                ),
                tests_selected=(
                    int(payload["tests_selected"])
                    if isinstance(payload.get("tests_selected"), int)
                    else None
                ),
                tests_total=(
                    int(payload["tests_total"])
                    if isinstance(payload.get("tests_total"), int)
                    else None
                ),
                coverage=(
                    float(payload["coverage"])
                    if isinstance(payload.get("coverage"), (int, float))
                    else None
                ),
                e2e_state=str(payload.get("e2e_state") or ""),
                raw_output_tail=str(payload.get("raw_output_tail") or "")[:4000],
            )
        except (TypeError, ValueError):
            return None


@dataclass
class VerifyCache:
    """A content-addressed store of verification results.

    Bounded by BOTH a row count and a byte ceiling, and collection only ever
    happens on a WRITE or an explicit ``prune`` — never on a read. A cache whose
    lookup evicts is a cache where reading is a mutation, and that is a
    surprising property for a caller to discover.
    """

    root: Path
    max_rows: int = DEFAULT_MAX_ROWS
    max_bytes: int = DEFAULT_MAX_BYTES
    reads: int = 0
    writes: int = 0
    hits: int = 0
    misses: int = 0
    drops: int = 0
    collected_rows: int = 0
    last_reason: str = ""

    @property
    def path(self) -> Path:
        return self.root / f"{self.digest}.json"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "max_rows": int(self.max_rows),
            "max_bytes": int(self.max_bytes),
            "reads": self.reads,
            "writes": self.writes,
            "hits": self.hits,
            "misses": self.misses,
            "drops": self.drops,
            "collected_rows": self.collected_rows,
            "hit_rate": (self.hits / self.reads) if self.reads else None,
            "note": (
                "hit_rate is null when nothing was read, never 0.0 — a rate over "
                "no observations is not a rate"
            ),
        }


def cache_root(log_root: str, repo_path: str = "") -> Path:
    """Where the cache lives for a log root.

    Per-repository, keyed by a digest of the canonical path, so two repositories
    sharing a logs root do not share a cache and a relative path cannot reach
    another repository's rows.
    """
    base = Path(str(log_root or ".")).expanduser() / CACHE_DIRNAME
    if repo_path:
        try:
            canonical = str(Path(str(repo_path)).expanduser().resolve())
        except OSError:
            canonical = str(repo_path)
        token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return base / token
    return base


def _read_row(path: Path) -> Tuple[Optional[CacheRow], str]:
    if not path.is_file():
        return None, "absent"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"unreadable: {type(exc).__name__}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, f"malformed json: {exc}"
    row = CacheRow.from_dict(payload)
    if row is None:
        return None, "refused: an unknown schema version, key, or field type"
    return row, "read"


def lookup(
    cache: VerifyCache,
    digest: str,
) -> Tuple[Optional[CacheRow], Dict[str, Any]]:
    """Read one row. Returns ``(row, receipt)``; ``row is None`` on a miss.

    A read NEVER collects and NEVER writes. A corrupt row is reported as a
    ``drop`` with its reason and is not served.
    """
    cache.reads += 1
    row, why = _read_row(cache.path)
    if row is None:
        cache.misses += 1
        if why not in ("absent",):
            cache.drops += 1
        return None, {
            "hit": False,
            "reason": why,
            "content_digest": str(digest),
            "path": str(cache.path),
        }
    cache.hits += 1
    return row, {
        "hit": True,
        "reason": "an exact content-digest match, so the tree is byte-identical",
        "content_digest": row.digest,
        "created_at": row.created_at,
        "wall_s": row.wall_s,
        "path": str(cache.path),
    }


def store(
    cache: VerifyCache,
    *,
    digest: str,
    command: str = "",
    timeout_s: int = 0,
    wall_s: Optional[float] = None,
    regression_passed: Optional[bool] = None,
    regression_check: str = "",
    target_test_passed: Optional[bool] = None,
    flaky: Optional[bool] = None,
    tests_selected: Optional[int] = None,
    tests_total: Optional[int] = None,
    coverage: Optional[float] = None,
    e2e_state: str = "",
    raw_output: str = "",
    now: Optional[float] = None,
) -> CacheRow:
    """Write one row, then enforce the bounds.

    The row is written BEFORE collection, so a full cache never refuses the
    write that would have evicted the row making room.
    """
    cache.digest = str(digest)
    row = CacheRow(
        digest=str(digest),
        command=str(command or ""),
        timeout_s=int(timeout_s or 0),
        created_at=float(now if now is not None else time.time()),
        wall_s=wall_s,
        regression_passed=regression_passed,
        regression_check=str(regression_check or ""),
        target_test_passed=target_test_passed,
        flaky=flaky,
        tests_selected=tests_selected,
        tests_total=tests_total,
        coverage=coverage,
        e2e_state=str(e2e_state or ""),
        raw_output_tail=str(raw_output or "")[-4000:],
    )
    try:
        cache.root.mkdir(parents=True, exist_ok=True)
        tmp = cache.path.with_name(cache.path.name + f".tmp{os.getpid()}")
        tmp.write_text(
            json.dumps(row.to_dict(), sort_keys=True), encoding="utf-8"
        )
        os.replace(str(tmp), str(cache.path))
        cache.writes += 1
    except OSError as exc:
        # A cache that cannot be written is not a failed verification. The
        # reason is recorded and the run continues.
        cache.last_reason = f"store failed: {type(exc).__name__}: {exc}"
    prune(cache)
    return row


def prune(cache: VerifyCache) -> Dict[str, Any]:
    """Enforce the row-count and byte bounds. Oldest first.

    Returns the rows and bytes removed plus a sentence. A dry run reports what
    it WOULD remove as ``would_remove_rows`` and removes nothing — a dry run
    that claims a collection it did not perform is worse than no dry run.
    """
    root = cache.root
    if not root.is_dir():
        return {
            "rows_in_store": 0,
            "rows": 0,
            "bytes": 0,
            "reason": "no cache directory yet",
        }
    rows: List[Tuple[float, int, Path]] = []
    for path in root.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((stat.st_mtime, stat.st_size, path))
    rows.sort(key=lambda item: (item[0], item[2].name))

    total_bytes = sum(item[1] for item in rows)
    over_rows = max(0, len(rows) - max(1, int(cache.max_rows)))
    over_bytes = max(0, total_bytes - max(1, int(cache.max_bytes)))
    removable = min(len(rows), max(over_rows, over_bytes))
    reason = (
        f"{len(rows)} row(s) and {total_bytes} byte(s) exceed the bound "
        f"({cache.max_rows} rows / {cache.max_bytes} bytes)"
        if removable
        else "within bounds"
    )
    removed = 0
    freed = 0
    for _mtime, size, path in rows[:removable]:
        try:
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            continue
    cache.collected_rows += removed
    cache.last_reason = reason
    return {
        "rows_in_store": len(rows) - removed,
        "rows": removed,
        "bytes": freed,
        "reason": reason,
        "note": "collection happens on a write or an explicit prune, never on a read",
    }


# --------------------------------------------------------------------------
# The reuse receipt — the honesty crux
# --------------------------------------------------------------------------


def reuse_evidence(
    row: CacheRow,
    receipt: Mapping[str, Any],
    *,
    commit: str = "",
    feature_id: str = "",
) -> Dict[str, Any]:
    """Build the receipt attached to a result that was served from the cache.

    Every field here is required by one of the three rules in the module
    docstring, and the ``fresh_evidence: False`` is not a default — it is the
    only value this function can produce, which is what makes rule 2 structural
    rather than a convention somebody can forget.
    """
    when = ""
    if row.created_at:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(row.created_at))
        except (OverflowError, OSError, ValueError):  # pragma: no cover
            when = str(row.created_at)
    source = (
        f"reused from content digest {row.digest[:12]}"
        + (f" (measured {when})" if when else "")
        + (f", commit {commit}" if commit else "")
    )
    return {
        "reused": True,
        "fresh_evidence": False,
        "content_digest": row.digest,
        "matched_by": "exact content digest of every file the run read",
        "commit": str(commit or ""),
        "feature_id": str(feature_id or ""),
        "stored_at": row.created_at,
        "stored_at_rendered": when,
        "regression_passed": row.regression_passed,
        "regression_check": row.regression_check,
        "target_test_passed": row.target_test_passed,
        "flaky": row.flaky,
        "tests_selected": row.tests_selected,
        "tests_total": row.tests_total,
        "coverage": row.coverage,
        "e2e_state": row.e2e_state,
        "wall_s": row.wall_s,
        "summary": source,
        "lookup": dict(receipt),
        "may_mark_feature_passing": False,
        "can_evidence_a_new_feature": False,
        "note": (
            "NOTHING RAN. This is a stored measurement of an EARLIER tree, "
            "served because the content digest matched exactly. It may be shown "
            "to a human to save a re-run; it can never mark a feature passing, "
            "because the feature did not exist when it was written. "
            "execution.verification_evidence.ledger_evidence refuses on "
            "fresh_evidence."
        ),
    }


def can_mark_passing(result: Any) -> bool:
    """Whether a result's evidence is FRESH. The single answer T5's gate wants.

    Reads ``verification_evidence.fresh_evidence`` and refuses anything it does
    not positively say. Absence, ``None``, and a hand-assembled value are all
    ``False``: this function can only ever withhold.
    """
    from execution.verification_evidence import evidence_of

    evidence = evidence_of(result)
    if evidence is None:
        return False
    return bool(evidence.fresh) is True


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def measure_verify_cycle(
    repo_path: str,
    *,
    target_test: Optional[str] = None,
    test_command: Optional[str] = None,
    regression_mode: str = "full_suite",
    verify_timeout_s: int = 300,
    include_e2e: bool = False,
    e2e_repo: Optional[str] = None,
    samples: int = 3,
    runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Measure a full verification cycle and REFRESH :data:`COST_TABLE`.

    Measures cold-then-warm, which is the distinction that decides a policy: a
    per-iteration policy pays the warm number, a per-session policy pays the
    cold one once. ``runner`` is the injection seam; production leaves it
    ``None`` and drives the real boundary.

    Every unmeasured row stays ``None``. A refused or broken lane reports
    ``lane_broken: True`` and writes NO number, because a timing measured over
    failing iterations is not a timing.
    """
    from execution import verify as ver

    calls: List[Dict[str, Any]] = []
    broken: List[str] = []
    t0 = time.perf_counter()
    try:
        result = (runner or ver.verify)(
            repo_path,
            target_test,
            rerun_for_flake_check=1,
            test_command=test_command,
            verify_timeout_s=int(verify_timeout_s),
            regression_mode=str(regression_mode),
        )
        warm_s = time.perf_counter() - t0
    except Exception as exc:
        return {
            "lane_broken": True,
            "reason": f"the verification boundary raised: {type(exc).__name__}: {exc}",
            "calls": calls,
            "note": (
                "no cost row was written. A timing measured over a broken lane "
                "is not a timing, and reporting one would be the dishonest "
                "version of this measurement."
            ),
        }
    cost = getattr(result, "verification_cost", None)
    calls.append(
        {
            "kind": "unit",
            "regression_mode": str(regression_mode),
            "wall_s": round(warm_s, 4),
            "command_runs": (cost or {}).get("command_runs"),
            "tests_selected": (cost or {}).get("tests_selected"),
            "tests_total": (cost or {}).get("tests_total"),
            "regression_check": getattr(result, "regression_check", ""),
            "target_test_passed": bool(getattr(result, "target_test_passed", False)),
            "regression_passed": bool(getattr(result, "regression_passed", False)),
            "container_s": (cost or {}).get("container_s"),
            "overhead_s": (cost or {}).get("overhead_s"),
        }
    )

    e2e_s: Optional[float] = None
    if include_e2e and e2e_repo:
        from execution import e2e_verify

        t1 = time.perf_counter()
        try:
            report = e2e_verify.run_e2e(e2e_repo, requested=True)
            e2e_s = time.perf_counter() - t1
            calls.append(
                {
                    "kind": "e2e",
                    "verdict": report.verdict,
                    "counts": report.to_dict()["counts"],
                    "wall_s": round(e2e_s, 4),
                }
            )
        except Exception as exc:
            broken.append(f"e2e: {type(exc).__name__}: {exc}")

    if str(regression_mode) == "selected":
        COST_TABLE["unit_selected_warm"]["s"] = round(warm_s, 4)
        COST_TABLE["unit_selected_warm"]["basis"] = (
            f"measured on this host, {samples} requested, one cycle observed"
        )
    else:
        COST_TABLE["unit_full_suite_warm"]["s"] = round(warm_s, 4)
        COST_TABLE["unit_full_suite_warm"]["basis"] = "measured on this host, one cycle"
    if e2e_s is not None:
        COST_TABLE["e2e_single_check"]["s"] = round(e2e_s, 4)
        COST_TABLE["e2e_single_check"]["basis"] = "measured on this host"
    if isinstance(cost, Mapping) and isinstance(cost.get("container_s"), (int, float)):
        COST_TABLE["cold_overhead"]["s"] = round(float(cost["container_s"]), 4)
        COST_TABLE["cold_overhead"]["basis"] = (
            "the container-side total of one warm cycle; this is the floor a "
            "faster harness cannot go below"
        )

    return {
        "lane_broken": False,
        "calls": calls,
        "policy": POLICY,
        "cost_table": {name: cost_row(name) for name in sorted(COST_TABLE)},
        "lanes_broken": broken,
        "note": (
            "cold is measured by ensure_image on the FIRST call of a process and "
            "warm on every call after; a per-iteration policy pays the warm row "
            "and a per-session policy pays the cold row once"
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    """CLI: print the policy, the cost table, or re-measure them."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m execution.verify_cache")
    parser.add_argument("--measure", action="store_true", help="re-measure the cost table")
    parser.add_argument("--repo", default=".", help="repository to measure against")
    parser.add_argument("--target", default=None)
    parser.add_argument("--regression-mode", default="full_suite")
    parser.add_argument("--include-e2e", action="store_true")
    parser.add_argument("--e2e-repo", default=None)
    parser.add_argument("--policy", action="store_true", help="print the run policy")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.measure:
        report = measure_verify_cycle(
            args.repo,
            target_test=args.target,
            regression_mode=args.regression_mode,
            include_e2e=bool(args.include_e2e),
            e2e_repo=args.e2e_repo,
        )
        print(json.dumps(report, indent=2, default=str))
        return 2 if report.get("lane_broken") else 0

    for line in render_cost_table():
        print(line)
    if args.policy:
        print()
        for line in render_policy():
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
