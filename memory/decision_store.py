"""Decision/pattern memory: a persistent store of learned facts.

Architecture decisions, conventions, past bugs, harness learnings —
anything worth remembering across tasks and sessions. Backed by SQLite
(one file, stdlib, zero extra deps). Three ingestion paths:

1. Manual / MCP: ``record(text, ...)`` — the ``record_decision`` MCP tool.
2. Boundary 4 ingestion: ``ingest_state_file`` / ``poll`` — reads the
   ``decisions`` field from Terminal 1's structured state files at
   ``logs/{task_id}/state.json`` (schema owned by harness/context.py).
3. ``watch`` — background polling loop that ingests new state files as
   they appear (for long-running processes; the MCP server instead polls
   lazily on each query, which is self-healing and thread-free).

Promotion: ``promote_decision`` is the explicit, provenance-bearing path for
turning text into durable memory. Legacy ``record`` remains available for
manual, MCP, agent, and state-file callers; session, conversation,
transcript, and chat sources are refused unless they carry provenance.

Deduplication: ingestion checks (task_id, normalized repo_path, text) so
re-reading a state.json that grew more decisions is a no-op for the ones
already stored in that repo. Manual/MCP records default to dedupe disabled
(a human may intend to repeat one).

Search: query words are matched case-insensitively against the text
(LIKE-based, no FTS dependency); rows are ranked by how many query words
they match, then by recency. An empty query returns the most recent
decisions.

Assumes: one store file per project; concurrent access is serialized by
an in-process lock (WAL mode keeps cross-process readers non-blocking
for the common read-heavy MCP workload).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple

from memory.paths import safe_state_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'manual',
    task_id TEXT,
    repo_path TEXT,
    created_at TEXT NOT NULL,
    provenance TEXT,
    metadata TEXT,
    durable INTEGER NOT NULL DEFAULT 1,
    promoted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_repo ON decisions(repo_path);
CREATE INDEX IF NOT EXISTS idx_decisions_task_repo_text
    ON decisions(task_id, repo_path, text);
"""

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|passwd|client[_-]?secret|private[_-]?key)\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]{0,31}://[^/\s:@]+:)[^@\s/]+@"),
)
_QUOTED_SECRET_PATTERN = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|passwd|client[_-]?secret|private[_-]?key|authorization|cookie)[\"']?\s*:\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
_MAX_TEXT_CHARS = 16_384
_MAX_FIELD_CHARS = 512
_MAX_REPO_PATH_CHARS = 4_096
_MAX_PROVENANCE_CHARS = 4_096
_MAX_METADATA_CHARS = 8_192
_MAX_METADATA_ITEMS = 64
_MAX_METADATA_DEPTH = 6
_MAX_STATE_FILE_BYTES = 8 * 1024 * 1024
_MAX_STATE_DECISIONS = 10_000
_DURABLE_SOURCES = frozenset({"manual", "mcp", "state-file", "agent", "harness"})
_SESSION_SOURCES = frozenset(
    {
        "session",
        "conversation",
        "transcript",
        "chat",
        "user-message",
        "user",
        "user-input",
        "assistant",
        "assistant-message",
        "conversation-summary",
        "message",
        "model",
    }
)


class DecisionStoreHealthStatus(str, Enum):
    """Stable health states reported by a decision store."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CORRUPT = "corrupt"
    CLOSED = "closed"
    ERROR = "error"

    def __str__(self) -> str:
        """Return the serialized health state."""
        return self.value


class DecisionStoreCorruptionStatus(str, Enum):
    """Stable corruption states reported by a decision store."""

    CLEAN = "clean"
    REPAIRABLE = "repairable"
    PRESENT = "present"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        """Return the serialized corruption state."""
        return self.value


class DecisionStoreRecoveryStatus(str, Enum):
    """Stable outcomes reported by a decision-store recovery attempt."""

    NOT_NEEDED = "not_needed"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        """Return the serialized recovery state."""
        return self.value


class DecisionStoreCorruptError(RuntimeError):
    """Raised by strict callers when the decision store cannot be trusted."""


DecisionStoreCorruptionError = DecisionStoreCorruptError


@dataclass(frozen=True)
class DecisionStoreHealth:
    """A typed, secret-free health projection for one decision store."""

    status: DecisionStoreHealthStatus
    healthy: bool
    corrupt: bool
    row_count: int = 0
    error: Optional[str] = None
    details: MappingABC[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize enum and scalar fields supplied by callers."""
        status = (
            self.status
            if isinstance(self.status, DecisionStoreHealthStatus)
            else DecisionStoreHealthStatus(self.status)
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "healthy", bool(self.healthy))
        object.__setattr__(self, "corrupt", bool(self.corrupt))
        object.__setattr__(self, "row_count", max(0, int(self.row_count)))
        object.__setattr__(self, "error", _bounded_error(self.error))
        details = self.details if isinstance(self.details, MappingABC) else {}
        object.__setattr__(
            self,
            "details",
            MappingProxyType(_redact_metadata_mapping(details)),
        )

    @property
    def ok(self) -> bool:
        """Return whether the store is healthy."""
        return self.healthy

    @property
    def state(self) -> str:
        """Return the serialized health state."""
        return self.status.value

    @property
    def closed(self) -> bool:
        """Return whether the store has been closed."""
        return self.status is DecisionStoreHealthStatus.CLOSED

    @property
    def corruption(self) -> bool:
        """Return whether health checks found database corruption."""
        return self.corrupt

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible health projection."""
        return {
            "status": self.status.value,
            "state": self.status.value,
            "health_status": self.status.value,
            "ok": self.healthy,
            "healthy": self.healthy,
            "corrupt": self.corrupt,
            "row_count": self.row_count,
            "error": self.error,
            "details": dict(self.details),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()

    def __getitem__(self, key: str) -> Any:
        """Expose health fields through mapping syntax."""
        return self.as_dict()[key]

    def __iter__(self):
        """Iterate over stable health field names."""
        return iter(self.as_dict())

    def __len__(self) -> int:
        """Return the number of projected health fields."""
        return len(self.as_dict())

    def __bool__(self) -> bool:
        """Use the health projection as its boolean status."""
        return self.healthy


@dataclass(frozen=True)
class DecisionStoreRecovery:
    """A typed result from a non-destructive decision-store recovery pass."""

    status: DecisionStoreRecoveryStatus
    recovered: bool
    repaired_rows: int = 0
    row_count: int = 0
    error: Optional[str] = None
    details: MappingABC[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize enum and scalar fields supplied by callers."""
        status = (
            self.status
            if isinstance(self.status, DecisionStoreRecoveryStatus)
            else DecisionStoreRecoveryStatus(self.status)
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "recovered", bool(self.recovered))
        object.__setattr__(self, "repaired_rows", max(0, int(self.repaired_rows)))
        object.__setattr__(self, "row_count", max(0, int(self.row_count)))
        object.__setattr__(self, "error", _bounded_error(self.error))
        details = self.details if isinstance(self.details, MappingABC) else {}
        object.__setattr__(
            self,
            "details",
            MappingProxyType(_redact_metadata_mapping(details)),
        )

    @property
    def ok(self) -> bool:
        """Return whether recovery completed without an error."""
        return self.status in {
            DecisionStoreRecoveryStatus.NOT_NEEDED,
            DecisionStoreRecoveryStatus.RECOVERED,
        }

    @property
    def state(self) -> str:
        """Return the serialized recovery state."""
        return self.status.value

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible recovery projection."""
        return {
            "status": self.status.value,
            "state": self.status.value,
            "ok": self.ok,
            "recovered": self.recovered,
            "repaired_rows": self.repaired_rows,
            "row_count": self.row_count,
            "error": self.error,
            "details": dict(self.details),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Alias for :meth:`as_dict`."""
        return self.as_dict()

    def __getitem__(self, key: str) -> Any:
        """Expose recovery fields through mapping syntax."""
        return self.as_dict()[key]

    def __iter__(self):
        """Iterate over stable recovery field names."""
        return iter(self.as_dict())

    def __len__(self) -> int:
        """Return the number of projected recovery fields."""
        return len(self.as_dict())

    def __bool__(self) -> bool:
        """Use the recovery projection as its boolean status."""
        return self.recovered or self.status is DecisionStoreRecoveryStatus.NOT_NEEDED


DecisionHealth = DecisionStoreHealth
DecisionRecovery = DecisionStoreRecovery
DecisionHealthStatus = DecisionStoreHealthStatus
DecisionRecoveryStatus = DecisionStoreRecoveryStatus
DecisionStoreHealthReport = DecisionStoreHealth
DecisionStoreRecoveryReport = DecisionStoreRecovery
DecisionStoreCorruption = DecisionStoreCorruptionStatus


@dataclass(frozen=True)
class DecisionProvenance:
    """Explicit provenance receipt attached to a promoted decision."""

    kind: str
    source: str = ""
    reference: str = ""
    explicit: bool = True
    metadata: Optional[MappingABC[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        """Return a bounded-friendly provenance mapping."""
        result: Dict[str, Any] = {
            "kind": self.kind,
            "explicit": bool(self.explicit),
        }
        if self.source:
            result["source"] = self.source
        if self.reference:
            result["reference"] = self.reference
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        return result


Provenance = DecisionProvenance


def contains_secret(text: Any) -> bool:
    """Return whether text contains a high-confidence credential pattern."""
    try:
        value = str(text or "")
    except Exception:
        return False
    return _QUOTED_SECRET_PATTERN.search(value) is not None or any(
        pattern.search(value) for pattern in _SECRET_PATTERNS
    )


def redact_secrets(text: Any) -> str:
    """Replace high-confidence credentials in text with a safe placeholder."""
    try:
        value = str(text or "")
    except Exception:
        return ""
    value = _QUOTED_SECRET_PATTERN.sub(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + _SECRET_PLACEHOLDER
            + match.group("quote")
        ),
        value,
    )
    for index, pattern in enumerate(_SECRET_PATTERNS):
        if index in (5, 7):
            value = pattern.sub(r"\1" + _SECRET_PLACEHOLDER, value)
        else:
            value = pattern.sub(_SECRET_PLACEHOLDER, value)
    return value


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "access_token",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)


def _bounded_error(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        text = redact_secrets(value)
    except Exception:
        return "decision store error"
    text = _CONTROL_RE.sub(" ", text).strip()
    return text[:500] or None


def _redact_metadata_mapping(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_METADATA_DEPTH:
        return _SECRET_PLACEHOLDER
    if isinstance(value, MappingABC):
        result: Dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_METADATA_ITEMS]:
            key_text = str(key)
            if _sensitive_metadata_key(key_text):
                result[key_text[:_MAX_FIELD_CHARS]] = _SECRET_PLACEHOLDER
            else:
                result[key_text[:_MAX_FIELD_CHARS]] = _redact_metadata_mapping(
                    item, depth + 1
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _redact_metadata_mapping(item, depth + 1)
            for item in list(value)[:_MAX_METADATA_ITEMS]
        ]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return redact_secrets(value)[:_MAX_FIELD_CHARS]
        return value
    return _SECRET_PLACEHOLDER


def _sensitive_metadata_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return any(part in normalized for part in _SENSITIVE_METADATA_KEYS)


def _normalize_metadata_value(
    value: Any,
    depth: int = 0,
    seen: Optional[set[int]] = None,
    counter: Optional[List[int]] = None,
) -> Tuple[bool, Any]:
    if depth > _MAX_METADATA_DEPTH:
        return False, None
    if seen is None:
        seen = set()
    if counter is None:
        counter = [0]
    if value is None or isinstance(value, (bool, int)):
        counter[0] += 1
        return (counter[0] <= _MAX_METADATA_ITEMS), value
    if isinstance(value, float):
        if not math.isfinite(value):
            return False, None
        counter[0] += 1
        return (counter[0] <= _MAX_METADATA_ITEMS), value
    if isinstance(value, str):
        counter[0] += 1
        if counter[0] > _MAX_METADATA_ITEMS or len(value) > _MAX_TEXT_CHARS:
            return False, None
        if _CONTROL_RE.search(value):
            return False, None
        return True, redact_secrets(value)
    if isinstance(value, MappingABC):
        identity = id(value)
        if identity in seen:
            return False, None
        seen.add(identity)
        result: Dict[str, Any] = {}
        try:
            items = list(value.items())
        except Exception:
            seen.discard(identity)
            return False, None
        if len(items) > _MAX_METADATA_ITEMS:
            seen.discard(identity)
            return False, None
        for key, item in items:
            counter[0] += 1
            if counter[0] > _MAX_METADATA_ITEMS:
                seen.discard(identity)
                return False, None
            if not isinstance(key, str):
                seen.discard(identity)
                return False, None
            key = redact_secrets(key).strip()
            if not key or len(key) > _MAX_FIELD_CHARS or _CONTROL_RE.search(key):
                seen.discard(identity)
                return False, None
            if _sensitive_metadata_key(key):
                result[key] = _SECRET_PLACEHOLDER
                continue
            ok, normalized = _normalize_metadata_value(item, depth + 1, seen, counter)
            if not ok:
                seen.discard(identity)
                return False, None
            result[key] = normalized
        seen.discard(identity)
        return True, result
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return False, None
        seen.add(identity)
        if len(value) > _MAX_METADATA_ITEMS:
            seen.discard(identity)
            return False, None
        result_list: List[Any] = []
        for item in value:
            counter[0] += 1
            if counter[0] > _MAX_METADATA_ITEMS:
                seen.discard(identity)
                return False, None
            ok, normalized = _normalize_metadata_value(item, depth + 1, seen, counter)
            if not ok:
                seen.discard(identity)
                return False, None
            result_list.append(normalized)
        seen.discard(identity)
        return True, result_list
    return False, None


def _metadata_json(value: Any) -> Tuple[bool, Optional[str]]:
    if value is None:
        return True, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, None
    if not isinstance(value, MappingABC):
        return False, None
    ok, normalized = _normalize_metadata_value(value)
    if not ok:
        return False, None
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False, None
    if len(encoded) > _MAX_METADATA_CHARS:
        return False, None
    return True, encoded


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """Validate, bound, and redact a metadata mapping for durable storage."""
    if value is None:
        return {}
    ok, normalized = _normalize_metadata_value(value)
    if not ok or not isinstance(normalized, dict):
        raise ValueError("metadata must be a bounded JSON object")
    return normalized


def validate_metadata(value: Any) -> bool:
    """Return whether metadata is a bounded, JSON-compatible mapping."""
    try:
        normalize_metadata(value)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_provenance(value: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    if value is None:
        return True, None
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > _MAX_PROVENANCE_CHARS:
            return False, None
        value = {"kind": value}
    elif not isinstance(value, MappingABC):
        as_dict = getattr(value, "as_dict", None)
        if callable(as_dict):
            try:
                value = as_dict()
            except Exception:
                return False, None
        elif hasattr(value, "__dict__"):
            value = {
                str(key): item
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
    ok, normalized = _normalize_metadata_value(value)
    if not ok or not isinstance(normalized, dict) or not normalized:
        return False, None
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False, None
    if len(encoded) > _MAX_PROVENANCE_CHARS:
        return False, None
    return True, normalized


def _prepare_text(value: Any) -> Optional[str]:
    try:
        text = str(value or "").strip()
    except Exception:
        return None
    if not text or len(text) > _MAX_TEXT_CHARS or _CONTROL_RE.search(text):
        return None
    if contains_secret(text):
        return None
    return redact_secrets(text)


def _prepare_field(value: Any, default: str) -> Optional[str]:
    if value is None:
        value = default
    if isinstance(value, (MappingABC, list, tuple, set, bytes, bytearray)):
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or len(text) > _MAX_FIELD_CHARS or _CONTROL_RE.search(text):
        return None
    return redact_secrets(text)


def _prepare_task_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (MappingABC, list, tuple, set, bytes, bytearray)):
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or len(text) > _MAX_FIELD_CHARS or _CONTROL_RE.search(text):
        return None
    return redact_secrets(text)


def _prepare_repo_scope(value: Any) -> Tuple[bool, Optional[str]]:
    if value is None or value == "":
        return True, None
    if isinstance(value, (MappingABC, list, tuple, set, bytes, bytearray)):
        return False, None
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str):
            raw = str(raw)
        raw = raw.strip()
    except Exception:
        return False, None
    if not raw or len(raw) > _MAX_REPO_PATH_CHARS or _CONTROL_RE.search(raw):
        return False, None
    if contains_secret(raw):
        return False, None
    key = _repo_key(raw)
    if not key or len(key) > _MAX_REPO_PATH_CHARS or _CONTROL_RE.search(key):
        return False, None
    return True, key


def _truncate_recovered_text(value: Any) -> str:
    text = redact_secrets(value)
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    marker = "...[TRUNCATED]"
    return text[: max(0, _MAX_TEXT_CHARS - len(marker))] + marker


def _durable_value(value: Any) -> bool:
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False


@dataclass
class Decision:
    """One stored fact."""

    id: int
    text: str
    category: str
    source: str
    task_id: Optional[str]
    repo_path: Optional[str]
    created_at: str
    provenance: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    durable: bool = True
    promoted_at: Optional[str] = None

    @property
    def provenance_kind(self) -> Optional[str]:
        """Return the normalized provenance kind when one is present."""
        if not isinstance(self.provenance, MappingABC):
            return None
        value = self.provenance.get("kind")
        return str(value) if value else None

    @property
    def provenance_source(self) -> Optional[str]:
        """Return the normalized provenance source when one is present."""
        if not isinstance(self.provenance, MappingABC):
            return None
        value = self.provenance.get("source")
        return str(value) if value else None

    @property
    def is_promoted(self) -> bool:
        """Return whether this row carries a durable promotion receipt."""
        return bool(self.durable and self.provenance is not None)

    def as_dict(self) -> Dict[str, Any]:
        """Return a redacted, JSON-compatible decision projection."""
        return {
            "id": self.id,
            "text": redact_secrets(self.text),
            "category": redact_secrets(self.category),
            "source": redact_secrets(self.source),
            "task_id": (
                redact_secrets(self.task_id) if self.task_id is not None else None
            ),
            "repo_path": (
                redact_secrets(self.repo_path) if self.repo_path is not None else None
            ),
            "created_at": redact_secrets(self.created_at),
            "provenance": _redact_metadata_mapping(self.provenance)
            if self.provenance is not None
            else None,
            "provenance_kind": self.provenance_kind,
            "provenance_source": self.provenance_source,
            "metadata": _redact_metadata_mapping(self.metadata)
            if self.metadata is not None
            else None,
            "durable": bool(self.durable),
            "is_promoted": self.is_promoted,
            "promoted_at": (
                redact_secrets(self.promoted_at)
                if self.promoted_at is not None
                else None
            ),
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class DecisionStore:
    """Persistent decision/pattern memory (one SQLite file).

    Thread-safe: every method takes an internal lock (the MCP server may
    serve requests from different threads). Assumes the DB file's parent
    directory is writable.
    """

    def __init__(self, db_path: str, *, strict: bool = False) -> None:
        """Open a store; strict mode re-raises database-open failures."""
        self.db_path = str(Path(db_path).resolve())
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False
        self._open_error: Optional[str] = None
        self._last_repair_count = 0
        try:
            self._conn = _connect(self.db_path)
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._ensure_schema()
                for index_name in ("idx_task_text", "idx_task_repo_text"):
                    exists = self._conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (index_name,),
                    ).fetchone()
                    if exists is not None:
                        self._conn.execute(f"DROP INDEX {index_name}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_decisions_durable_id "
                    "ON decisions(durable, id)"
                )
                self._conn.commit()
                self._last_repair_count = self._scrub_existing()
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
            self._open_error = _bounded_error(exc)
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None
            if strict:
                raise

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(decisions)").fetchall()
        }
        additions = (
            ("provenance", "TEXT"),
            ("metadata", "TEXT"),
            ("durable", "INTEGER NOT NULL DEFAULT 1"),
            ("promoted_at", "TEXT"),
        )
        for name, definition in additions:
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE decisions ADD COLUMN {name} {definition}"
                )

    def _scrub_existing(self) -> int:
        if self._conn is None:
            return 0
        repaired = 0
        rows = self._conn.execute("SELECT * FROM decisions").fetchall()
        for row in rows:
            values = {
                "text": _truncate_recovered_text(row["text"]),
                "category": _prepare_field(row["category"], "general") or "general",
                "source": _prepare_field(row["source"], "manual") or "manual",
                "task_id": _prepare_task_id(row["task_id"]),
                "repo_path": _repo_key(row["repo_path"]) if row["repo_path"] else None,
                "created_at": _prepare_field(row["created_at"], _utc_now())
                or _utc_now(),
                "provenance": _decode_json_object(row["provenance"]),
                "metadata": _decode_json_object(row["metadata"]),
                "durable": 1 if _durable_value(row["durable"]) else 0,
                "promoted_at": _prepare_field(row["promoted_at"], "") or None,
            }
            if (
                values["source"].casefold() in _SESSION_SOURCES
                and values["provenance"] is None
            ):
                values["durable"] = 0
            encoded_provenance = _encode_json_object(values["provenance"])
            encoded_metadata = _encode_json_object(values["metadata"])
            original = {
                "text": row["text"],
                "category": row["category"],
                "source": row["source"],
                "task_id": row["task_id"],
                "repo_path": row["repo_path"],
                "created_at": row["created_at"],
                "provenance": row["provenance"],
                "metadata": row["metadata"],
                "durable": row["durable"],
                "promoted_at": row["promoted_at"],
            }
            normalized = {
                "text": values["text"],
                "category": values["category"],
                "source": values["source"],
                "task_id": values["task_id"],
                "repo_path": values["repo_path"],
                "created_at": values["created_at"],
                "provenance": encoded_provenance,
                "metadata": encoded_metadata,
                "durable": values["durable"],
                "promoted_at": values["promoted_at"],
            }
            if original != normalized:
                self._conn.execute(
                    "UPDATE decisions SET text = ?, category = ?, source = ?, "
                    "task_id = ?, repo_path = ?, created_at = ?, provenance = ?, "
                    "metadata = ?, durable = ?, promoted_at = ? WHERE id = ?",
                    (
                        values["text"],
                        values["category"],
                        values["source"],
                        values["task_id"],
                        values["repo_path"],
                        values["created_at"],
                        _encode_json_object(values["provenance"]),
                        _encode_json_object(values["metadata"]),
                        values["durable"],
                        values["promoted_at"],
                        int(row["id"]),
                    ),
                )
                repaired += 1
        self._conn.commit()
        return repaired

    def _insert_decision(
        self,
        text: str,
        category: str,
        source: str,
        task_id: Optional[str],
        repo_scope: Optional[str],
        provenance_json: Optional[str],
        metadata_json: Optional[str],
        promoted_at: Optional[str],
        dedupe: bool,
    ) -> Optional[int]:
        if self._conn is None or self._closed:
            return None
        now = _utc_now()
        with self._lock:
            if self._conn is None or self._closed:
                return None
            if dedupe and task_id is not None:
                if self._conn.in_transaction:
                    self._conn.commit()
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    duplicate = self._conn.execute(
                        "SELECT id FROM decisions "
                        "WHERE task_id = ? AND repo_path = ? AND text = ? LIMIT 1",
                        (task_id, repo_scope, text),
                    ).fetchone()
                    if duplicate is None:
                        for row in self._conn.execute(
                            "SELECT repo_path FROM decisions WHERE task_id = ? AND text = ?",
                            (task_id, text),
                        ):
                            if _repo_key(row["repo_path"]) == repo_scope:
                                duplicate = row
                                break
                    if duplicate is not None:
                        self._conn.rollback()
                        return None
                    cur = self._conn.execute(
                        "INSERT INTO decisions (text, category, source, task_id, "
                        "repo_path, created_at, provenance, metadata, durable, promoted_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (
                            text,
                            category,
                            source,
                            task_id,
                            repo_scope,
                            now,
                            provenance_json,
                            metadata_json,
                            promoted_at,
                        ),
                    )
                    self._conn.commit()
                    return int(cur.lastrowid)
                except Exception:
                    self._conn.rollback()
                    raise
            cur = self._conn.execute(
                "INSERT INTO decisions (text, category, source, task_id, repo_path, "
                "created_at, provenance, metadata, durable, promoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    text,
                    category,
                    source,
                    task_id,
                    repo_scope,
                    now,
                    provenance_json,
                    metadata_json,
                    promoted_at,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record(
        self,
        text: str,
        category: str = "general",
        source: str = "manual",
        task_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        dedupe: bool = False,
        provenance: Any = None,
        metadata: Any = None,
        promote: bool = False,
        explicit: bool = False,
    ) -> Optional[int]:
        """Insert a bounded decision or return ``None`` when it is unsafe.

        Existing manual, MCP, agent, and state-file calls retain their
        historical behavior. Session, conversation, transcript, and chat
        sources require explicit provenance or an explicit promotion call.
        With ``dedupe=True`` and a task id, an identical normalized
        ``(task_id, repo_path, text)`` row is a no-op.
        """
        prepared_text = _prepare_text(text)
        prepared_category = _prepare_field(category, "general")
        prepared_source = _prepare_field(source, "manual")
        prepared_task_id = _prepare_task_id(task_id)
        if (
            prepared_text is None
            or prepared_category is None
            or prepared_source is None
            or (task_id is not None and prepared_task_id is None)
        ):
            return None
        repo_ok, repo_scope = _prepare_repo_scope(repo_path)
        if not repo_ok:
            return None
        provenance_ok, normalized_provenance = _normalize_provenance(provenance)
        metadata_ok, metadata_json = _metadata_json(metadata)
        if not provenance_ok or not metadata_ok:
            return None
        source_key = prepared_source.casefold()
        explicit_requested = bool(promote or explicit)
        provenance_explicit = bool(
            isinstance(normalized_provenance, MappingABC)
            and normalized_provenance.get("explicit") is True
        )
        if source_key in _SESSION_SOURCES and not (
            explicit_requested or provenance_explicit
        ):
            return None
        if explicit_requested and normalized_provenance is None:
            if source_key == "state-file":
                normalized_provenance = {
                    "kind": "state-file",
                    "source": "state-file",
                    "explicit": True,
                }
            else:
                return None
        if source_key == "state-file" and normalized_provenance is None:
            normalized_provenance = {
                "kind": "state-file",
                "source": "state-file",
                "explicit": True,
            }
        if normalized_provenance is not None and "source" not in normalized_provenance:
            normalized_provenance = {
                **normalized_provenance,
                "source": source_key,
            }
        provenance_json = _encode_json_object(normalized_provenance)
        if provenance_json is not None and len(provenance_json) > _MAX_PROVENANCE_CHARS:
            return None
        promoted_at = _utc_now() if normalized_provenance is not None else None
        return self._insert_decision(
            text=prepared_text,
            category=prepared_category,
            source=prepared_source,
            task_id=prepared_task_id,
            repo_scope=repo_scope,
            provenance_json=provenance_json,
            metadata_json=metadata_json,
            promoted_at=promoted_at,
            dedupe=bool(dedupe),
        )

    def promote_decision(
        self,
        text: str,
        category: str = "general",
        source: str = "manual",
        task_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        provenance: Any = None,
        metadata: Any = None,
        dedupe: bool = False,
        explicit: bool = True,
    ) -> Optional[int]:
        """Promote a decision only when bounded provenance is supplied.

        The call is intentionally separate from legacy ``record``. State-file
        decisions use this path because the harness state schema is an
        explicit decision channel; arbitrary session text has no implicit
        promotion path.
        """
        if not explicit:
            return None
        if isinstance(provenance, MappingABC) and provenance.get("explicit") is False:
            return None
        return self.record(
            text=text,
            category=category,
            source=source,
            task_id=task_id,
            repo_path=repo_path,
            dedupe=dedupe,
            provenance=provenance,
            metadata=metadata,
            promote=bool(explicit),
        )

    def promote(
        self,
        text: str,
        category: str = "general",
        source: str = "manual",
        task_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        provenance: Any = None,
        metadata: Any = None,
        dedupe: bool = False,
        explicit: bool = True,
    ) -> Optional[int]:
        """Alias for :meth:`promote_decision`."""
        return self.promote_decision(
            text=text,
            category=category,
            source=source,
            task_id=task_id,
            repo_path=repo_path,
            provenance=provenance,
            metadata=metadata,
            dedupe=dedupe,
            explicit=explicit,
        )

    def record_decision(
        self,
        text: str,
        category: str = "general",
        source: str = "manual",
        task_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        provenance: Any = None,
        metadata: Any = None,
        dedupe: bool = False,
        explicit: bool = True,
    ) -> Optional[int]:
        """Explicit provenance-gated alias for decision promotion."""
        return self.promote_decision(
            text=text,
            category=category,
            source=source,
            task_id=task_id,
            repo_path=repo_path,
            provenance=provenance,
            metadata=metadata,
            dedupe=dedupe,
            explicit=explicit,
        )

    # -- read ----------------------------------------------------------

    def search(
        self,
        query: str = "",
        limit: int = 20,
        repo_path: Optional[str] = None,
        source: Optional[str] = None,
        category: Optional[str] = None,
        durable_only: bool = True,
    ) -> List[Decision]:
        """Return ranked decisions with optional repository and metadata filters.

        Empty queries return recent rows. Text matching remains the historical
        case-insensitive per-word substring search. ``repo_path`` continues
        to exclude unscoped rows and compares canonical repository keys.
        """
        if self._conn is None or self._closed:
            return []
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            bounded_limit = 20
        words = _query_words(query)
        repo_ok, repo_key = _prepare_repo_scope(repo_path)
        if not repo_ok:
            return []
        source_key = None
        if source is not None:
            source_key = _prepare_field(source, "")
            if source_key is None:
                return []
        category_key = None
        if category is not None:
            category_key = _prepare_field(category, "")
            if category_key is None:
                return []
        with self._lock:
            if self._conn is None or self._closed:
                return []
            try:
                rows = self._conn.execute("SELECT * FROM decisions").fetchall()
            except sqlite3.DatabaseError:
                return []
            filtered: List[sqlite3.Row] = []
            for row in rows:
                if durable_only and not bool(row["durable"]):
                    continue
                if repo_key and _repo_key(row["repo_path"]) != repo_key:
                    continue
                if source_key is not None and row["source"] != source_key:
                    continue
                if category_key is not None and row["category"] != category_key:
                    continue
                filtered.append(row)
            if not words:
                filtered.sort(key=lambda row: int(row["id"]), reverse=True)
                return [_row_to_decision(row) for row in filtered[:bounded_limit]]
            scores: Dict[int, tuple] = {}
            for row in filtered:
                text_l = " " + (row["text"] or "").lower() + " "
                matched = sum(1 for word in words if word in text_l)
                if matched:
                    scores[int(row["id"])] = (matched, int(row["id"]))
            return [
                _row_to_decision(row)
                for row in sorted(
                    (row for row in filtered if int(row["id"]) in scores),
                    key=lambda row: (
                        -scores[int(row["id"])][0],
                        -int(row["id"]),
                    ),
                )[:bounded_limit]
            ]

    def count(
        self,
        source: Optional[str] = None,
        repo_path: Optional[str] = None,
        durable_only: bool = True,
    ) -> int:
        """Count stored decisions with optional source and repository filters."""
        if self._conn is None or self._closed:
            return 0
        repo_ok, repo_key = _prepare_repo_scope(repo_path)
        if not repo_ok:
            return 0
        source_key = None
        if source is not None:
            source_key = _prepare_field(source, "")
            if source_key is None:
                return 0
        with self._lock:
            if self._conn is None or self._closed:
                return 0
            try:
                rows = self._conn.execute("SELECT * FROM decisions").fetchall()
            except sqlite3.DatabaseError:
                return 0
            total = 0
            for row in rows:
                if durable_only and not bool(row["durable"]):
                    continue
                if source_key is not None and row["source"] != source_key:
                    continue
                if repo_key and _repo_key(row["repo_path"]) != repo_key:
                    continue
                total += 1
            return total

    def get(
        self,
        decision_id: int,
        include_undurable: bool = False,
    ) -> Optional[Decision]:
        """Return one durable decision by id, or ``None`` when it is absent."""
        if self._conn is None or self._closed:
            return None
        try:
            row_id = int(decision_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            if self._conn is None or self._closed:
                return None
            try:
                row = self._conn.execute(
                    "SELECT * FROM decisions WHERE id = ?", (row_id,)
                ).fetchone()
            except sqlite3.DatabaseError:
                return None
        if row is None:
            return None
        if not include_undurable and not bool(row["durable"]):
            return None
        return _row_to_decision(row)

    # -- Boundary 4 ingestion -------------------------------------------

    def ingest_state_file(self, path: str) -> int:
        """Ingest the explicit decisions list from one state file.

        The Boundary 4 state file is itself the provenance channel, so its
        string decisions are promoted with a state-file receipt. Malformed,
        oversized, or unreadable files contribute zero rows.
        """
        try:
            state_path = Path(path).expanduser().resolve()
            if state_path.stat().st_size > _MAX_STATE_FILE_BYTES:
                return 0
            raw = state_path.read_text(encoding="utf-8-sig")
            if len(raw) > _MAX_STATE_FILE_BYTES:
                return 0
            data = json.loads(raw)
        except (OSError, RuntimeError, ValueError, UnicodeError):
            return 0
        if not isinstance(data, dict):
            return 0
        raw_task_id = data.get("task_id")
        task_id = _prepare_task_id(raw_task_id) or state_path.parent.name
        task_id = _prepare_task_id(task_id)
        if task_id is None:
            return 0
        decisions = data.get("decisions")
        if not isinstance(decisions, list) or len(decisions) > _MAX_STATE_DECISIONS:
            return 0
        repo_path = data.get("repo_path")
        repo_ok, repo_scope = _prepare_repo_scope(repo_path)
        if not repo_ok:
            return 0
        new = 0
        for entry in decisions:
            if not isinstance(entry, str) or not entry.strip():
                continue
            rid = self.promote_decision(
                text=entry,
                category="task",
                source="state-file",
                task_id=task_id,
                repo_path=repo_scope,
                provenance={
                    "kind": "state-file",
                    "source": "state-file",
                    "explicit": True,
                    "state_file": state_path.name,
                    "task_id": task_id,
                },
                dedupe=True,
            )
            if rid is not None:
                new += 1
        return new

    def poll(self, logs_dir: str) -> int:
        """Ingest every state.json under `logs_dir` once (recursive).

        Idempotent per task, normalized repo, and text. Returns count of
        NEW rows ingested. Assumes the harness layout: one subdirectory per
        task holding state.json — but real runs nest deeper (e.g.
        benchmark drivers that stage task logs under
        ``<logs>/ablations/<run>/tasklogs/<task_id>/state.json``), so any
        depth is scanned. Re-ingestion of archived ``{task_id}.old-*``
        dirs is a no-op for texts already stored.
        """
        try:
            root = Path(logs_dir).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return 0
        if not root.is_dir():
            return 0
        new = 0
        for candidate in sorted(root.rglob("state.json")):
            state = safe_state_file(candidate, root)
            if state is not None:
                new += self.ingest_state_file(str(state))
        return new

    def watch(self, logs_dir: str, interval_s: float = 2.0, stop_event=None) -> None:
        """Poll `logs_dir` forever (until stop_event is set) — for
        long-running processes that want live ingestion.

        Assumes interval_s > 0.2; a polling loop is deliberate (watchdog
        would be a dependency for cross-platform inotify on Windows).
        """
        interval_s = max(float(interval_s), 0.2)
        while stop_event is None or not stop_event.is_set():
            try:
                self.poll(logs_dir)
            except Exception:
                pass  # a bad scan must never kill the watcher
            if stop_event is None:
                time.sleep(interval_s)
            else:
                stop_event.wait(interval_s)

    def health(self, check_rows: bool = True) -> DecisionStoreHealth:
        """Inspect database integrity, schema, and row safety without raising."""
        with self._lock:
            if self._closed:
                return DecisionStoreHealth(
                    status=DecisionStoreHealthStatus.CLOSED,
                    healthy=False,
                    corrupt=False,
                    error="decision store is closed",
                )
            if self._conn is None:
                return DecisionStoreHealth(
                    status=DecisionStoreHealthStatus.CORRUPT,
                    healthy=False,
                    corrupt=True,
                    error=self._open_error or "database unavailable",
                )
            details: Dict[str, Any] = {
                "integrity": "unknown",
                "schema": "unknown",
                "invalid_rows": 0,
            }
            try:
                integrity_rows = [
                    str(item[0])
                    for item in self._conn.execute("PRAGMA integrity_check")
                ]
                integrity = integrity_rows[0] if integrity_rows else "unknown"
                details["integrity"] = redact_secrets(integrity)[:200]
                tables = {
                    str(item[0])
                    for item in self._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "decisions" not in tables:
                    return DecisionStoreHealth(
                        status=DecisionStoreHealthStatus.CORRUPT,
                        healthy=False,
                        corrupt=True,
                        error="decisions table is missing",
                        details=details,
                    )
                columns = {
                    str(item[1])
                    for item in self._conn.execute("PRAGMA table_info(decisions)")
                }
                required = {
                    "id",
                    "text",
                    "category",
                    "source",
                    "task_id",
                    "repo_path",
                    "created_at",
                }
                missing = required - columns
                details["schema"] = (
                    "ok" if not missing else "missing:" + ",".join(sorted(missing))
                )
                if integrity.strip().lower() != "ok" or missing:
                    return DecisionStoreHealth(
                        status=DecisionStoreHealthStatus.CORRUPT,
                        healthy=False,
                        corrupt=True,
                        error=integrity
                        if integrity.strip().lower() != "ok"
                        else "schema incomplete",
                        details=details,
                    )
                if not check_rows:
                    row_count = int(
                        self._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[
                            0
                        ]
                    )
                    details["row_count"] = row_count
                    return DecisionStoreHealth(
                        status=DecisionStoreHealthStatus.HEALTHY,
                        healthy=True,
                        corrupt=False,
                        row_count=row_count,
                        details=details,
                    )
                rows = self._conn.execute("SELECT * FROM decisions").fetchall()
                details["row_count"] = len(rows)
                invalid = 0
                for row in rows:
                    raw_text = row["text"]
                    if (
                        not isinstance(raw_text, str)
                        or not raw_text.strip()
                        or len(raw_text) > _MAX_TEXT_CHARS
                        or bool(_CONTROL_RE.search(raw_text))
                        or contains_secret(raw_text)
                    ):
                        invalid += 1
                        continue
                    for name in ("provenance", "metadata"):
                        raw_value = row[name]
                        if (
                            raw_value is not None
                            and _decode_json_object(raw_value) is None
                        ):
                            invalid += 1
                            break
                    else:
                        if not isinstance(row["durable"], int) or row[
                            "durable"
                        ] not in (0, 1):
                            invalid += 1
                            continue
                        if row["repo_path"] is not None:
                            repo_ok, _ = _prepare_repo_scope(row["repo_path"])
                            if not repo_ok:
                                invalid += 1
                                continue
                        if _prepare_field(row["category"], "general") is None:
                            invalid += 1
                            continue
                        if _prepare_field(row["source"], "manual") is None:
                            invalid += 1
                details["invalid_rows"] = invalid
                if invalid:
                    return DecisionStoreHealth(
                        status=DecisionStoreHealthStatus.DEGRADED,
                        healthy=False,
                        corrupt=False,
                        row_count=len(rows),
                        details=details,
                    )
                return DecisionStoreHealth(
                    status=DecisionStoreHealthStatus.HEALTHY,
                    healthy=True,
                    corrupt=False,
                    row_count=len(rows),
                    details=details,
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.DatabaseError,
            ) as exc:
                return DecisionStoreHealth(
                    status=DecisionStoreHealthStatus.ERROR,
                    healthy=False,
                    corrupt=True,
                    error=_bounded_error(exc),
                    details=details,
                )

    def health_status(self) -> DecisionStoreHealthStatus:
        """Return the stable serialized health state."""
        return self.health().status

    def corruption_status(self) -> DecisionStoreCorruptionStatus:
        """Return whether the store is clean, repairable, or corrupt."""
        status = self.health().status
        if status is DecisionStoreHealthStatus.HEALTHY:
            return DecisionStoreCorruptionStatus.CLEAN
        if status is DecisionStoreHealthStatus.DEGRADED:
            return DecisionStoreCorruptionStatus.REPAIRABLE
        if status is DecisionStoreHealthStatus.CORRUPT:
            return DecisionStoreCorruptionStatus.PRESENT
        return DecisionStoreCorruptionStatus.UNKNOWN

    def check_health(self, check_rows: bool = True) -> DecisionStoreHealth:
        """Alias for :meth:`health`."""
        return self.health(check_rows=check_rows)

    def get_health(self, check_rows: bool = True) -> DecisionStoreHealth:
        """Alias for :meth:`health`."""
        return self.health(check_rows=check_rows)

    def health_check(self, check_rows: bool = True) -> DecisionStoreHealth:
        """Alias for :meth:`health`."""
        return self.health(check_rows=check_rows)

    def is_healthy(self, check_rows: bool = True) -> bool:
        """Return whether the store passes its health checks."""
        return bool(self.health(check_rows=check_rows))

    def recover(self, vacuum: bool = False) -> DecisionStoreRecovery:
        """Repair bounded rows and report recovery without destructive resets."""
        with self._lock:
            if self._closed:
                return DecisionStoreRecovery(
                    status=DecisionStoreRecoveryStatus.UNAVAILABLE,
                    recovered=False,
                    error="decision store is closed",
                )
            if self._conn is None:
                return DecisionStoreRecovery(
                    status=DecisionStoreRecoveryStatus.UNAVAILABLE,
                    recovered=False,
                    error=self._open_error or "database unavailable",
                )
            before = self.health()
            if before.status in {
                DecisionStoreHealthStatus.CORRUPT,
                DecisionStoreHealthStatus.ERROR,
            }:
                return DecisionStoreRecovery(
                    status=DecisionStoreRecoveryStatus.FAILED,
                    recovered=False,
                    error=before.error or "database integrity check failed",
                    details=dict(before.details),
                )
            try:
                self._ensure_schema()
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_decisions_durable_id "
                    "ON decisions(durable, id)"
                )
                repaired = self._scrub_existing()
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                self._conn.commit()
                if vacuum:
                    self._conn.execute("VACUUM")
                after = self.health()
                row_count = after.row_count
                if after.status is DecisionStoreHealthStatus.HEALTHY:
                    return DecisionStoreRecovery(
                        status=(
                            DecisionStoreRecoveryStatus.RECOVERED
                            if repaired
                            else DecisionStoreRecoveryStatus.NOT_NEEDED
                        ),
                        recovered=bool(repaired),
                        repaired_rows=repaired,
                        row_count=row_count,
                        details={"health": after.status.value},
                    )
                return DecisionStoreRecovery(
                    status=DecisionStoreRecoveryStatus.DEGRADED,
                    recovered=False,
                    repaired_rows=repaired,
                    row_count=row_count,
                    error=after.error,
                    details=dict(after.details),
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.DatabaseError,
            ) as exc:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                return DecisionStoreRecovery(
                    status=DecisionStoreRecoveryStatus.FAILED,
                    recovered=False,
                    error=_bounded_error(exc),
                )

    def repair(self, vacuum: bool = False) -> DecisionStoreRecovery:
        """Alias for :meth:`recover`."""
        return self.recover(vacuum=vacuum)

    def recovery_report(self, vacuum: bool = False) -> DecisionStoreRecovery:
        """Alias for :meth:`recover`."""
        return self.recover(vacuum=vacuum)

    def recovery_status(self) -> DecisionStoreRecoveryStatus:
        """Return the current corruption state as a recovery-oriented enum."""
        health_status = self.health().status
        if health_status is DecisionStoreHealthStatus.HEALTHY:
            return DecisionStoreRecoveryStatus.NOT_NEEDED
        if health_status is DecisionStoreHealthStatus.DEGRADED:
            return DecisionStoreRecoveryStatus.DEGRADED
        return DecisionStoreRecoveryStatus.FAILED

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Close the DB connection; repeated close calls are harmless."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            self._closed = True

    def __enter__(self) -> "DecisionStore":
        """Return this store as a context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Close the store when leaving a context-manager block."""
        self.close()


def _decode_json_object(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, MappingABC):
        parsed: Any = dict(value)
    else:
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except UnicodeError:
                return None
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, MappingABC):
        return None
    ok, normalized = _normalize_metadata_value(parsed)
    if not ok or not isinstance(normalized, dict):
        return None
    return normalized


def _encode_json_object(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, MappingABC):
        return None
    try:
        encoded = json.dumps(
            _redact_metadata_mapping(dict(value)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_METADATA_CHARS:
        return None
    return encoded


def _repo_key(repo_path: Any) -> Optional[str]:
    """Return the canonical, OS-normalized repository comparison key."""
    if repo_path is None or repo_path == "":
        return None
    try:
        raw = os.fspath(repo_path)
        if not isinstance(raw, str):
            raw = str(raw)
    except Exception:
        return None
    if not raw or len(raw) > _MAX_REPO_PATH_CHARS or _CONTROL_RE.search(raw):
        return None
    if contains_secret(raw):
        return None
    try:
        return os.path.normcase(str(Path(raw).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(raw)


def open_default_store() -> "DecisionStore":
    """The shared decision store at memory.paths.decisions_db_path().

    The single opener every consumer (harness planner queries, MCP server,
    CLI) should use so they all read/write ONE database. Assumes the
    process has the usual filesystem permissions for HARNESS_HOME.
    """
    from memory.paths import decisions_db_path

    return DecisionStore(str(decisions_db_path()))


def inspect_decision_store(db_path: str) -> DecisionStoreHealth:
    """Open a decision store, return its health projection, and close it."""
    store = DecisionStore(str(db_path))
    try:
        return store.health()
    finally:
        store.close()


def recover_decision_store(
    db_path: str,
    vacuum: bool = False,
) -> DecisionStoreRecovery:
    """Open a decision store, run safe recovery, and close it."""
    store = DecisionStore(str(db_path))
    try:
        return store.recover(vacuum=vacuum)
    finally:
        store.close()


def decision_store_health(db_path: str) -> DecisionStoreHealth:
    """Alias for :func:`inspect_decision_store`."""
    return inspect_decision_store(db_path)


def health_snapshot(db_path: str) -> DecisionStoreHealth:
    """Return a one-shot health snapshot for a decision database."""
    return inspect_decision_store(db_path)


def decision_store_recovery(
    db_path: str,
    vacuum: bool = False,
) -> DecisionStoreRecovery:
    """Alias for :func:`recover_decision_store`."""
    return recover_decision_store(db_path, vacuum=vacuum)


def _row_to_decision(row: sqlite3.Row) -> Decision:
    def value(name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    durable_value = value("durable", 1)
    try:
        durable = bool(int(durable_value))
    except (TypeError, ValueError):
        durable = True
    return Decision(
        id=int(value("id", 0)),
        text=redact_secrets(value("text", "")),
        category=redact_secrets(value("category", "general")),
        source=redact_secrets(value("source", "manual")),
        task_id=(
            redact_secrets(value("task_id")) or None
            if value("task_id") is not None
            else None
        ),
        repo_path=(
            redact_secrets(value("repo_path")) or None
            if value("repo_path") is not None
            else None
        ),
        created_at=redact_secrets(value("created_at", "")),
        provenance=_decode_json_object(value("provenance")),
        metadata=_decode_json_object(value("metadata")),
        durable=durable,
        promoted_at=(
            redact_secrets(value("promoted_at")) or None
            if value("promoted_at") is not None
            else None
        ),
    )


def _query_words(query: Any) -> List[str]:
    """Return lowercased query words after removing broad stop words."""
    stop = {
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "what",
        "which",
        "how",
        "why",
        "and",
        "or",
        "did",
        "do",
        "does",
        "was",
        "were",
        "that",
        "this",
        "it",
        "we",
        "i",
        "me",
        "my",
    }
    try:
        value = str(query or "").lower()
    except Exception:
        value = ""
    words = re.findall(r"[a-z0-9_]+", value[:_MAX_TEXT_CHARS])
    return [w for w in words if len(w) >= 2 and w not in stop] or words


def format_decisions(decisions: List[Decision], query: str = "") -> str:
    """Render decisions as the human-readable string returned by MCP /
    CLI. Assumes decisions are already ranked; caps at a sane length."""
    if not decisions:
        safe_query = redact_secrets(query)
        return "no matching decisions" + (f" for {safe_query!r}" if safe_query else "")
    lines: List[str] = []
    for d in decisions:
        source = redact_secrets(d.source)
        task_id = redact_secrets(d.task_id)
        origin = f" [{source}]" if source != "manual" else ""
        task = f" task:{task_id}" if task_id else ""
        lines.append(f"- {redact_secrets(d.text)}{origin}{task}")
    return "\n".join(lines)
