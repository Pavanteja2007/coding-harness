"""Bounded, content-addressed local cache for resolved recipes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from shared.security import (
    REDACTED_SECRET,
    contains_secret,
    is_sensitive_key,
    redact_secrets,
    require_contained,
)

from .models import (
    SCHEMA_VERSION,
    Recipe,
    RecipeCacheCorruptionError,
    RecipeCacheError,
    _jsonable,
)

DEFAULT_MAX_ENTRIES = 128
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60


def _raw_key_value(value: Any) -> Any:
    """Return deterministic JSON data for hashing without redaction."""
    return _jsonable(value)


@dataclass
class RecipeCacheEntry:
    """One validated local cache entry."""

    key: str
    payload: Any
    created_at: float
    expires_at: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    size_bytes: int = 0

    @property
    def schema_version(self) -> int:
        """Return the cache entry schema version."""
        return SCHEMA_VERSION

    @property
    def cache_key(self) -> str:
        """Return the content-addressed key under a descriptive alias."""
        return self.key

    @property
    def value(self) -> Any:
        """Return the cached payload under a concise alias."""
        return self.payload

    @property
    def payload_hash(self) -> str:
        """Return the integrity digest of the redacted payload."""
        return _payload_digest(self.payload)

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Return whether this entry is expired at the supplied epoch time."""
        timestamp = time.time() if now is None else float(now)
        return self.expires_at is not None and timestamp >= self.expires_at

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a JSON-compatible redacted entry envelope."""
        payload = redact_secrets(self.payload) if redact else self.payload
        metadata = redact_secrets(self.metadata) if redact else self.metadata
        result = {
            "schema_version": SCHEMA_VERSION,
            "key": self.key,
            "payload": payload,
            "payload_digest": _payload_digest(payload),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "metadata": metadata,
            "size_bytes": self.size_bytes,
        }
        result["record_digest"] = _record_digest(result)
        return result

    to_dict = as_dict

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecipeCacheEntry":
        """Parse and validate one serialized cache entry."""
        if not isinstance(data, Mapping):
            raise RecipeCacheCorruptionError("cache entry is not an object")
        required = {
            "schema_version",
            "key",
            "payload",
            "payload_digest",
            "record_digest",
            "created_at",
            "expires_at",
            "metadata",
            "size_bytes",
        }
        if set(data) != required:
            if not required.issubset(data):
                raise RecipeCacheCorruptionError(
                    "cache entry is missing required fields"
                )
            raise RecipeCacheCorruptionError("cache entry contains unknown fields")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RecipeCacheCorruptionError(
                "cache entry has an unsupported schema version"
            )
        key = data.get("key")
        if not isinstance(key, str) or len(key) != 64:
            raise RecipeCacheCorruptionError("cache entry has an invalid key")
        try:
            int(key, 16)
        except ValueError as exc:
            raise RecipeCacheCorruptionError("cache entry has an invalid key") from exc
        payload_digest = data.get("payload_digest")
        if not isinstance(payload_digest, str) or len(payload_digest) != 64:
            raise RecipeCacheCorruptionError(
                "cache entry has an invalid payload digest"
            )
        try:
            int(payload_digest, 16)
        except ValueError as exc:
            raise RecipeCacheCorruptionError(
                "cache entry has an invalid payload digest"
            ) from exc
        try:
            actual_payload_digest = _payload_digest(data.get("payload"))
            actual_record_digest = _record_digest(data)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RecipeCacheCorruptionError(
                "recipe cache entry contains non-JSON data"
            ) from exc
        if actual_payload_digest != payload_digest:
            raise RecipeCacheCorruptionError(
                "recipe cache payload failed integrity validation"
            )
        record_digest = data.get("record_digest")
        if not isinstance(record_digest, str) or actual_record_digest != record_digest:
            raise RecipeCacheCorruptionError(
                "recipe cache metadata failed integrity validation"
            )
        created = data.get("created_at")
        if (
            isinstance(created, bool)
            or not isinstance(created, (int, float))
            or not math.isfinite(float(created))
        ):
            raise RecipeCacheCorruptionError("cache entry has an invalid timestamp")
        expires = data.get("expires_at")
        if expires is not None and (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(float(expires))
        ):
            raise RecipeCacheCorruptionError("cache entry has an invalid expiry")
        size = data.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RecipeCacheCorruptionError("cache entry has an invalid size")
        if not isinstance(data.get("metadata"), Mapping):
            raise RecipeCacheCorruptionError("cache entry metadata is invalid")
        return cls(
            key=key,
            payload=redact_secrets(data.get("payload")),
            created_at=float(created),
            expires_at=None if expires is None else float(expires),
            metadata=dict(redact_secrets(dict(data["metadata"]))),
            size_bytes=size,
        )

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeCacheEntry({self.as_dict(redact=True)!r})"


class RecipeCache:
    """Atomic local recipe cache with count, byte, and TTL bounds."""

    def __init__(
        self,
        root: Any = None,
        *,
        cache_dir: Any = None,
        cache_root: Any = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_seconds: Optional[float] = DEFAULT_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        max_items: Optional[int] = None,
        max_count: Optional[int] = None,
        max_size_bytes: Optional[int] = None,
        max_size: Optional[int] = None,
        ttl: Optional[float] = None,
    ) -> None:
        """Create a cache below a contained local root."""
        selected_root = root if root is not None else cache_dir
        if selected_root is None:
            selected_root = cache_root
        if selected_root is None:
            raise RecipeCacheError("recipe cache root is required")
        if max_items is not None:
            max_entries = max_items
        if max_count is not None:
            max_entries = max_count
        if max_size_bytes is not None:
            max_bytes = max_size_bytes
        if max_size is not None:
            max_bytes = max_size
        if ttl is not None:
            ttl_seconds = ttl
        if max_entries <= 0 or max_bytes <= 0:
            raise RecipeCacheError("recipe cache bounds must be positive")
        if ttl_seconds is not None and (
            ttl_seconds < 0 or not math.isfinite(float(ttl_seconds))
        ):
            raise RecipeCacheError("recipe cache TTL must be finite and non-negative")
        self.root = Path(selected_root).expanduser()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            require_contained(self.root, self.root / ".recipe-cache-probe")
        except Exception as exc:
            raise RecipeCacheError("recipe cache root is not contained") from exc
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self.max_count = self.max_entries
        self.max_size = self.max_bytes
        self.ttl_seconds = None if ttl_seconds is None else float(ttl_seconds)
        self.ttl = self.ttl_seconds
        self.clock = clock or (lambda: time.time())

    @staticmethod
    def key_for(
        recipe: Recipe | Mapping[str, Any],
        closure: Any = None,
        tool_schema_digest: Any = "",
        *,
        tool_catalog: Any = None,
        parameters: Any = None,
        resolved_parameters: Any = None,
    ) -> str:
        """Return a raw-content SHA-256 key including parameters and tools."""
        resolution_parameters = None
        if hasattr(recipe, "recipe") and hasattr(recipe, "closure"):
            resolution = recipe
            recipe = resolution.recipe
            if closure is None:
                closure = resolution.closure
            if not tool_schema_digest:
                tool_schema_digest = getattr(resolution, "tool_schema_digest", "")
            if parameters is None and resolved_parameters is None:
                resolution_parameters = getattr(resolution, "parameters", None)
        if (
            parameters is not None
            and resolved_parameters is not None
            and _raw_key_value(parameters) != _raw_key_value(resolved_parameters)
        ):
            raise RecipeCacheError("recipe cache parameter aliases disagree")
        if parameters is None:
            parameters = resolved_parameters
        if parameters is None:
            parameters = resolution_parameters
        actual_recipe = (
            recipe if isinstance(recipe, Recipe) else Recipe.from_dict(recipe)
        )
        if closure is None:
            closure = {}
        if hasattr(closure, "closure"):
            closure = closure.closure
        if isinstance(closure, (list, tuple, set, frozenset)):
            closure = {
                getattr(item, "name", str(index)): item
                for index, item in enumerate(closure)
            }
        if not isinstance(closure, Mapping):
            raise RecipeCacheError("recipe cache closure must be a mapping")
        if tool_catalog is not None:
            from .validator import RecipeValidator

            tool_schema_digest = RecipeValidator().tool_schema_digest(tool_catalog)
        elif callable(tool_schema_digest):
            tool_schema_digest = str(tool_schema_digest())
        elif tool_schema_digest is not None and not isinstance(tool_schema_digest, str):
            from .models import ToolCatalog

            if isinstance(tool_schema_digest, ToolCatalog):
                tool_schema_digest = tool_schema_digest.digest()
            else:
                tool_schema_digest = str(tool_schema_digest)
        closure_value: dict[str, Any] = {}
        for name, child in sorted(closure.items(), key=lambda item: str(item[0])):
            actual_child = (
                child if isinstance(child, Recipe) else Recipe.from_dict(child)
            )
            closure_value[str(name)] = _raw_key_value(actual_child.normalized())
        payload = {
            "schema_version": SCHEMA_VERSION,
            "recipe": _raw_key_value(actual_recipe.normalized()),
            "closure": closure_value,
            "parameters": _raw_key_value(parameters),
            "tool_schema_digest": str(tool_schema_digest or ""),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    make_key = key_for
    content_key = key_for
    compute_key = key_for
    key = key_for

    @staticmethod
    def cache_key(
        recipe: Recipe | Mapping[str, Any],
        closure: Any = None,
        tool_schema_digest: Any = "",
        *,
        tool_catalog: Any = None,
        parameters: Any = None,
        resolved_parameters: Any = None,
    ) -> str:
        """Return a cache key without constructing a cache instance."""
        return RecipeCache.key_for(
            recipe,
            closure,
            tool_schema_digest,
            tool_catalog=tool_catalog,
            parameters=parameters,
            resolved_parameters=resolved_parameters,
        )

    def get_entry(
        self,
        key: str | Recipe | Mapping[str, Any],
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
        now: Optional[float] = None,
    ) -> Optional[RecipeCacheEntry]:
        """Return a live entry, rejecting malformed or expired cache data."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        path = self._path_for_key(actual_key)
        if path.is_symlink():
            raise RecipeCacheCorruptionError("recipe cache entry must not be a symlink")
        try:
            raw = _read_bounded(path, self.max_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecipeCacheError("recipe cache entry could not be read") from exc
        if len(raw) > self.max_bytes:
            raise RecipeCacheCorruptionError(
                "recipe cache entry exceeds the byte bound"
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise RecipeCacheCorruptionError(
                "recipe cache entry contains invalid JSON"
            ) from exc
        entry = RecipeCacheEntry.from_dict(data)
        if entry.key != actual_key:
            raise RecipeCacheCorruptionError(
                "recipe cache key does not match its filename"
            )
        canonical_payload = json.dumps(
            entry.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        if data.get("payload_digest") != digest:
            raise RecipeCacheCorruptionError(
                "recipe cache payload failed integrity validation"
            )
        if entry.size_bytes != len(raw):
            raise RecipeCacheCorruptionError(
                "recipe cache size metadata is inconsistent"
            )
        timestamp = self.clock() if now is None else float(now)
        if entry.expires_at is not None and timestamp >= entry.expires_at:
            self.delete(actual_key)
            return None
        return entry

    def get(
        self,
        key: str | Recipe | Mapping[str, Any],
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
        now: Optional[float] = None,
        as_entry: bool = False,
        tolerate_corrupt: bool = False,
    ) -> Any:
        """Return a cached payload or a typed entry when requested."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        try:
            entry = self.get_entry(actual_key, now=now)
        except RecipeCacheCorruptionError:
            if not tolerate_corrupt:
                raise
            self._discard_corrupt(actual_key)
            return None
        if entry is None:
            return None
        return entry if as_entry else entry.payload

    def set(
        self,
        key: str | Recipe | Mapping[str, Any],
        payload: Any,
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_at: Optional[float] = None,
    ) -> Optional[RecipeCacheEntry]:
        """Atomically write one entry and prune older entries to the bounds."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        self._validate_key(actual_key)
        try:
            safe_payload = _cache_payload(payload)
            metadata_value = dict(metadata or {})
            if not isinstance(key, str) and isinstance(key, Recipe):
                metadata_value.setdefault("recipe", key.name)
            safe_metadata = _redact_cache_metadata(metadata_value)
            payload_digest = _payload_digest(safe_payload)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RecipeCacheError(
                "recipe cache payload is not JSON-compatible"
            ) from exc
        timestamp = self.clock() if created_at is None else float(created_at)
        if not math.isfinite(timestamp):
            raise RecipeCacheError("recipe cache timestamp must be finite")
        expires = None if self.ttl_seconds is None else timestamp + self.ttl_seconds
        if expires is not None and not math.isfinite(expires):
            raise RecipeCacheError("recipe cache expiry must be finite")
        envelope: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "key": actual_key,
            "payload": safe_payload,
            "payload_digest": payload_digest,
            "record_digest": "",
            "created_at": timestamp,
            "expires_at": expires,
            "metadata": safe_metadata,
        }
        raw = b""
        try:
            envelope["size_bytes"] = 0
            for _ in range(8):
                envelope["record_digest"] = _record_digest(envelope)
                raw = json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                envelope["size_bytes"] = len(raw)
                final = json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                if final == raw:
                    raw = final
                    break
                raw = final
        except (TypeError, ValueError, OverflowError) as exc:
            raise RecipeCacheError("recipe cache entry is not JSON-compatible") from exc
        if len(raw) > self.max_bytes:
            return None
        path = self._path_for_key(actual_key)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{actual_key}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise RecipeCacheError("recipe cache entry could not be written") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._prune()
        return self.get_entry(actual_key, now=timestamp)

    put = set
    store = set
    read = get
    write = set

    def get_or_set(
        self,
        key: str | Recipe | Mapping[str, Any],
        factory: Callable[[], Any],
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_at: Optional[float] = None,
    ) -> Any:
        """Return a cached payload or recover a corrupt entry as a miss."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        try:
            cached_entry = self.get_entry(actual_key)
        except RecipeCacheCorruptionError:
            self._discard_corrupt(actual_key)
            cached_entry = None
        if cached_entry is not None:
            return cached_entry.payload
        return self.set(
            actual_key,
            factory(),
            metadata=metadata,
            created_at=created_at,
        )

    def delete(
        self,
        key: str | Recipe | Mapping[str, Any],
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
    ) -> bool:
        """Delete one contained cache entry."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        path = self._path_for_key(actual_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RecipeCacheError("recipe cache entry could not be deleted") from exc
        return True

    def entry_path(
        self,
        key: str | Recipe | Mapping[str, Any],
        *,
        closure: Any = None,
        tool_schema_digest: str = "",
        parameters: Any = None,
        resolved_parameters: Any = None,
    ) -> Path:
        """Return the contained path for one cache key."""
        actual_key = (
            key
            if isinstance(key, str)
            else self.key_for(
                key,
                closure,
                tool_schema_digest,
                parameters=parameters,
                resolved_parameters=resolved_parameters,
            )
        )
        return self._path_for_key(actual_key)

    path_for_key = entry_path

    def clear(self) -> None:
        """Delete all cache entry files below the configured root."""
        for path in self._entry_paths():
            try:
                path.unlink()
            except OSError as exc:
                raise RecipeCacheError("recipe cache could not be cleared") from exc

    def stats(self) -> dict[str, int]:
        """Return current entry count and serialized byte count."""
        paths = self._entry_paths()
        count = len(paths)
        byte_count = sum(_safe_size(path) for path in paths)
        return {
            "entries": count,
            "count": count,
            "bytes": byte_count,
            "size_bytes": byte_count,
        }

    def _path_for_key(self, key: str) -> Path:
        self._validate_key(key)
        try:
            return require_contained(self.root, Path(f"{key}.json"))
        except Exception as exc:
            raise RecipeCacheCorruptionError(
                "recipe cache key is outside the cache root"
            ) from exc

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or len(key) != 64:
            raise RecipeCacheError("recipe cache key must be a SHA-256 hex digest")
        try:
            int(key, 16)
        except ValueError as exc:
            raise RecipeCacheError(
                "recipe cache key must be a SHA-256 hex digest"
            ) from exc

    def _entry_paths(self) -> list[Path]:
        result: list[Path] = []
        try:
            candidates = self.root.glob("*.json")
            for path in candidates:
                try:
                    contained = require_contained(self.root, path)
                except Exception:
                    continue
                if contained.is_file() and not contained.is_symlink():
                    result.append(contained)
        except OSError as exc:
            raise RecipeCacheError(
                "recipe cache directory could not be inspected"
            ) from exc
        return sorted(result, key=lambda item: item.name)

    def _discard_corrupt(self, key: str) -> None:
        """Remove one unreadable cache entry without masking the recovery path."""
        try:
            self._path_for_key(key).unlink(missing_ok=True)
        except (OSError, RecipeCacheError):
            return

    def _prune(self) -> None:
        records: list[tuple[float, int, Path]] = []
        timestamp = self.clock()
        for index, path in enumerate(self._entry_paths()):
            try:
                raw = _read_bounded(path, self.max_bytes)
                data = json.loads(raw.decode("utf-8"))
                created = float(data.get("created_at", 0))
                expires = data.get("expires_at")
                if expires is not None and timestamp >= float(expires):
                    path.unlink()
                    continue
            except (
                OSError,
                ValueError,
                TypeError,
                RecipeCacheError,
                json.JSONDecodeError,
            ):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            records.append((created, index, path))
        records.sort(key=lambda item: (item[0], item[2].name))
        total = sum(_safe_size(item[2]) for item in records)
        while len(records) > self.max_entries or total > self.max_bytes:
            _, _, path = records.pop(0)
            try:
                total -= _safe_size(path)
                path.unlink()
            except OSError:
                continue


def recipe_cache_key(
    recipe: Recipe | Mapping[str, Any],
    closure: Any = None,
    tool_schema_digest: Any = "",
    *,
    tool_catalog: Any = None,
    parameters: Any = None,
    resolved_parameters: Any = None,
) -> str:
    """Return a content key without requiring a cache instance."""
    return RecipeCache.cache_key(
        recipe,
        closure,
        tool_schema_digest,
        tool_catalog=tool_catalog,
        parameters=parameters,
        resolved_parameters=resolved_parameters,
    )


cache_key = recipe_cache_key


def _redact_cache_metadata(value: Any, *, parameter_context: bool = False) -> Any:
    """Redact cache metadata and force sensitive recipe parameters out of files."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            child_context = parameter_context or key_text.casefold() in {
                "arguments",
                "args",
                "parameters",
            }
            if is_sensitive_key(key_text) or (
                child_context and contains_secret(item, key_text)
            ):
                result[key_text] = REDACTED_SECRET
            else:
                result[key_text] = _redact_cache_metadata(
                    item, parameter_context=child_context
                )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_cache_metadata(item, parameter_context=parameter_context)
            for item in value
        ]
    return redact_secrets(value)


def _read_bounded(path: Path, maximum: int) -> bytes:
    """Read at most the configured cache byte bound, including a race guard."""
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise RecipeCacheCorruptionError("recipe cache entry exceeds the byte bound")
    return raw


def _cache_payload(value: Any) -> Any:
    """Return a JSON-safe redacted cache payload."""
    if isinstance(value, Recipe):
        return value.to_dict(redact=True)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return redact_secrets(as_dict())
    return redact_secrets(value)


def _record_digest(data: Mapping[str, Any]) -> str:
    """Return an integrity digest over one complete cache envelope."""
    selected = {
        key: data.get(key)
        for key in (
            "schema_version",
            "key",
            "payload",
            "payload_digest",
            "created_at",
            "expires_at",
            "metadata",
        )
    }
    canonical = json.dumps(
        selected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_digest(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "RecipeCache",
    "RecipeCacheEntry",
]
