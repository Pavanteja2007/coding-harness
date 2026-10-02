"""Typed MCP tool descriptors, schema validation, and deferred discovery."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Union,
)

__all__ = [
    "DeferredTool",
    "RequiredToolsError",
    "SchemaValidationError",
    "ToolCatalog",
    "ToolCatalogError",
    "ToolCatalogSearch",
    "ToolDescriptor",
    "ToolNotFoundError",
    "ToolResolutionError",
    "ToolResolutionFailure",
    "ToolSchemaError",
    "validate_json_schema",
    "validate_required_tools",
    "validate_schema_definition",
]

_JSONMapping = Mapping[str, Any]
_SchemaLoader = Callable[..., Union[_JSONMapping, Awaitable[_JSONMapping]]]
_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|password|passwd|secret|credential|token)\s*[=:]\s*[^\s,;]+|sk-[A-Za-z0-9_-]{6,})"
)
_MAX_ERROR_CHARS = 512
_MAX_DESCRIPTION_CHARS = 2_000
_MAX_SCHEMA_CHARS = 131_072
_MAX_TAGS = 32


class ToolCatalogError(Exception):
    """Base error for catalog construction and lookup failures."""


class SchemaValidationError(ToolCatalogError):
    """Report a bounded JSON Schema validation failure without echoing values."""

    def __init__(self, path: str, message: str) -> None:
        """Create a safe validation error for a JSON document location."""
        safe_path = _bounded_text(_redact_text(path or "$"), 160)
        safe_message = _bounded_text(_redact_text(message), _MAX_ERROR_CHARS)
        super().__init__(f"schema validation failed at {safe_path}: {safe_message}")
        self.path = safe_path
        self.reason = safe_message


class ToolNotFoundError(ToolCatalogError):
    """Report that an exact tool name is absent from a catalog."""

    def __init__(self, name: str) -> None:
        """Create a bounded missing-tool error for a requested name."""
        safe_name = _bounded_text(_redact_text(str(name)), 160)
        super().__init__(f"tool not found: {safe_name}")
        self.name = safe_name


class ToolResolutionError(ToolCatalogError):
    """Report a bounded deferred schema loading or validation failure."""

    def __init__(
        self, name: str, message: str = "tool schema could not be resolved"
    ) -> None:
        """Create a safe deferred-resolution error without including loader output."""
        safe_name = _bounded_text(_redact_text(str(name)), 160)
        safe_message = _bounded_text(_redact_text(message), _MAX_ERROR_CHARS)
        super().__init__(
            f"could not resolve schema for tool {safe_name}: {safe_message}"
        )
        self.name = safe_name
        self.reason = safe_message


class RequiredToolsError(ToolCatalogError):
    """Report missing tools required by a recipe or platform."""

    def __init__(self, missing: Iterable[str]) -> None:
        """Create an error listing only bounded missing tool names."""
        names = tuple(_bounded_text(_redact_text(str(item)), 160) for item in missing)
        rendered = ", ".join(names) if names else "none"
        super().__init__(f"required tools are unavailable: {rendered}")
        self.missing = names


ToolSchemaError = SchemaValidationError
ToolResolutionFailure = ToolResolutionError


class _FrozenList(list[Any]):
    """A JSON-compatible list that rejects in-place mutation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        """Reject mutation operations on a frozen JSON array."""
        raise TypeError("JSON schema arrays are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


class _CallableList(list[Any]):
    """A list projection that also supports legacy method-style access."""

    def __call__(self) -> list[Any]:
        """Return a shallow list copy for callers using method syntax."""
        return list(self)


def _redact_text(value: Any, limit: int = _MAX_ERROR_CHARS) -> str:
    """Redact common credential forms and bound arbitrary text."""
    text = str(value or "")
    text = _SECRET_PATTERN.sub("[REDACTED_SECRET]", text)
    text = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|secret|credential|token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+",
        r"\1[REDACTED_SECRET]",
        text,
    )
    return _bounded_text(text, limit)


def _bounded_text(value: Any, limit: int) -> str:
    """Return text with a hard character bound and a truncation marker."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    marker = "...[truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _bound_summary(value: Any) -> str:
    """Return a bounded redacted description for deferred discovery."""
    return _bounded_text(
        _redact_text(value, _MAX_DESCRIPTION_CHARS),
        _MAX_DESCRIPTION_CHARS,
    )


def _freeze_json(value: Any) -> Any:
    """Recursively copy JSON-like values into immutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_json(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _thaw_json(value: Any) -> Any:
    """Convert immutable internal JSON values back to ordinary containers."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _json_size(value: Any) -> int:
    """Return a bounded JSON serialization size for schema inputs."""
    try:
        return len(json.dumps(_thaw_json(value), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _MAX_SCHEMA_CHARS + 1


def _normal_tags(tags: Any) -> tuple[str, ...]:
    """Normalize bounded string tags while dropping empty and secret values."""
    if tags is None:
        return ()
    if isinstance(tags, str):
        values = tags.split(",")
    elif isinstance(tags, Sequence):
        values = tags
    else:
        values = (tags,)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _bounded_text(_redact_text(value), 120).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= _MAX_TAGS:
            break
    return tuple(result)


def _call_flexible(
    factory: Callable[..., Any], candidates: Sequence[tuple[Any, ...]]
) -> Any:
    """Call an injected factory with the first signature-compatible argument set."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(*candidates[0]) if candidates else factory()
    for arguments in candidates:
        try:
            signature.bind(*arguments)
        except TypeError:
            continue
        return factory(*arguments)
    if candidates:
        return factory(*candidates[0])
    return factory()


def _safe_metadata(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return metadata with secret-shaped keys and values redacted."""
    if depth > 8:
        return "[TRUNCATED]"
    if any(
        marker in str(key).lower()
        for marker in (
            "token",
            "secret",
            "password",
            "credential",
            "authorization",
            "api_key",
            "argument",
            "result",
            "output",
        )
    ):
        return "[REDACTED_SECRET]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_metadata(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, key=key, depth=depth + 1) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


@dataclass(frozen=True, init=False)
class ToolDescriptor(Mapping[str, Any]):
    """Immutable, JSON-shaped metadata for one MCP tool.

    ``input_schema`` is the eager model schema.  Deferred descriptors created by
    :class:`DeferredTool` intentionally expose an empty schema until resolution.
    """

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    deferred: bool = False
    server: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __init__(
        self,
        name: str,
        description: str = "",
        input_schema: Optional[Mapping[str, Any]] = None,
        tags: Any = (),
        deferred: bool = False,
        server: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        *,
        inputSchema: Optional[Mapping[str, Any]] = None,
        schema: Optional[Mapping[str, Any]] = None,
        is_deferred: Optional[bool] = None,
    ) -> None:
        """Create a descriptor while accepting common MCP schema field aliases."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("tool name must not be empty")
        chosen_schema = input_schema
        if chosen_schema is None:
            chosen_schema = inputSchema
        if chosen_schema is None:
            chosen_schema = schema
        if chosen_schema is None:
            chosen_schema = {}
        if not isinstance(chosen_schema, Mapping):
            raise TypeError("tool input schema must be a mapping")
        deferred_flag = bool(is_deferred if is_deferred is not None else deferred)
        if not deferred_flag and _json_size(chosen_schema) > _MAX_SCHEMA_CHARS:
            raise ValueError("tool input schema exceeds the supported bound")
        frozen_schema = {} if deferred_flag else _freeze_json(chosen_schema)
        frozen_metadata = _freeze_json(metadata or {})
        object.__setattr__(self, "name", clean_name)
        object.__setattr__(
            self,
            "description",
            _bounded_text(
                _redact_text(description, _MAX_DESCRIPTION_CHARS),
                _MAX_DESCRIPTION_CHARS,
            ),
        )
        object.__setattr__(self, "input_schema", MappingProxyType(frozen_schema))
        object.__setattr__(self, "tags", _normal_tags(tags))
        object.__setattr__(self, "deferred", deferred_flag)
        object.__setattr__(self, "server", _bounded_text(_redact_text(server), 200))
        object.__setattr__(self, "metadata", MappingProxyType(dict(frozen_metadata)))

    @property
    def inputSchema(self) -> Mapping[str, Any]:
        """Return the JSON Schema using the MCP wire-field spelling."""
        return self.input_schema

    @property
    def is_deferred(self) -> bool:
        """Return whether this descriptor still needs schema resolution."""
        return self.deferred

    @property
    def schema_available(self) -> bool:
        """Return whether an eager model schema is available."""
        return not self.deferred

    @property
    def model_schema(self) -> Optional[Mapping[str, Any]]:
        """Return the eager schema or ``None`` for a deferred descriptor."""
        if self.deferred:
            return None
        return self.input_schema

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible descriptor projection."""
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": _thaw_json(self.input_schema),
            "tags": list(self.tags),
            "deferred": self.deferred,
            "server": self.server,
        }
        if self.metadata:
            result["metadata"] = _safe_metadata(_thaw_json(self.metadata))
        return result

    def search_dict(self) -> dict[str, Any]:
        """Return only non-secret fields used by discovery search."""
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "deferred": self.deferred,
        }

    def __getitem__(self, key: str) -> Any:
        """Expose descriptor fields through mapping syntax."""
        if key == "inputSchema":
            return self.input_schema
        if key == "is_deferred":
            return self.deferred
        if key == "schema":
            return self.model_schema
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over stable descriptor field names."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of projected descriptor fields."""
        return len(self.to_dict())

    def __repr__(self) -> str:
        """Return a bounded representation that cannot expose schema secrets."""
        return (
            f"ToolDescriptor(name={self.name!r}, description={_redact_text(self.description)[:120]!r}, "
            f"tags={self.tags!r}, deferred={self.deferred!r})"
        )


class DeferredTool:
    """A discoverable tool whose full input schema is loaded only on demand."""

    def __init__(
        self,
        name: str,
        description: str = "",
        tags: Any = (),
        summary: str = "",
        loader: Optional[_SchemaLoader] = None,
        *,
        schema_loader: Optional[_SchemaLoader] = None,
        resolver: Optional[_SchemaLoader] = None,
        executor: Any = None,
        server: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Create a deferred tool with no schema or execution callback."""
        self.name = str(name or "").strip()
        if not self.name:
            raise ValueError("deferred tool name must not be empty")
        self.description = _bounded_text(
            _redact_text(description, _MAX_DESCRIPTION_CHARS),
            _MAX_DESCRIPTION_CHARS,
        )
        self.tags = _normal_tags(tags)
        self.summary = _bound_summary(summary or description)
        self.server = _bounded_text(_redact_text(server), 200)
        self.metadata = MappingProxyType(dict(_freeze_json(metadata or {})))
        self._loader = loader or schema_loader or resolver
        self._executor = executor
        self._schema: Optional[Mapping[str, Any]] = None
        self._lock = asyncio.Lock()

    @property
    def deferred(self) -> bool:
        """Return the stable deferred marker."""
        return True

    @property
    def is_resolved(self) -> bool:
        """Return whether the full schema has already loaded successfully."""
        return self._schema is not None

    def descriptor(self) -> ToolDescriptor:
        """Return a non-secret search descriptor without loading the schema."""
        return ToolDescriptor(
            self.name,
            self.description,
            input_schema={},
            tags=self.tags,
            deferred=True,
            server=self.server,
            metadata=None,
        )

    def search_summary(self) -> dict[str, Any]:
        """Return a bounded summary suitable for model tool discovery."""
        return {
            "name": self.name,
            "description": self.description,
            "summary": self.summary,
            "tags": list(self.tags),
            "deferred": True,
        }

    async def resolve_schema(self) -> Mapping[str, Any]:
        """Load and validate the full schema without executing the tool."""
        async with self._lock:
            if self._schema is not None:
                return self._schema
            loader = self._loader
            if loader is None:
                raise ToolResolutionError(self.name, "no schema loader is configured")
            try:
                loaded = _call_flexible(loader, ((self.name,), (self,), ()))
                if inspect.isawaitable(loaded):
                    loaded = await loaded
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ToolResolutionError(self.name, "schema loader failed") from None
            if not isinstance(loaded, Mapping):
                candidate = getattr(loaded, "input_schema", None)
                if candidate is None:
                    candidate = getattr(loaded, "inputSchema", None)
                if candidate is None:
                    candidate = getattr(loaded, "schema", None)
                loaded = candidate
            if not isinstance(loaded, Mapping):
                raise ToolResolutionError(
                    self.name, "schema loader returned a non-mapping"
                )
            if _json_size(loaded) > _MAX_SCHEMA_CHARS:
                raise ToolResolutionError(
                    self.name, "schema exceeds the supported bound"
                )
            frozen = _freeze_json(loaded)
            if not isinstance(frozen, Mapping):
                raise ToolResolutionError(self.name, "schema is not a mapping")
            try:
                validate_schema_definition(frozen)
            except SchemaValidationError:
                raise ToolResolutionError(
                    self.name, "schema is not valid JSON Schema"
                ) from None
            self._schema = frozen
            return self._schema

    async def load_schema(self) -> Mapping[str, Any]:
        """Alias for :meth:`resolve_schema` used by deferred-tool callers."""
        return await self.resolve_schema()

    async def resolve(self) -> Mapping[str, Any]:
        """Alias for :meth:`resolve_schema`."""
        return await self.resolve_schema()

    async def resolved_descriptor(self) -> ToolDescriptor:
        """Return an eager descriptor after loading the deferred schema."""
        schema = await self.resolve_schema()
        return ToolDescriptor(
            self.name,
            self.description,
            input_schema=schema,
            tags=self.tags,
            deferred=False,
            server=self.server,
            metadata=self.metadata,
        )

    def __repr__(self) -> str:
        """Return a safe representation without loader internals or schema data."""
        return f"DeferredTool(name={self.name!r}, resolved={self.is_resolved!r})"


class ToolCatalog:
    """A bounded searchable catalog combining eager and deferred MCP tools."""

    def __init__(
        self,
        tools: Optional[
            Iterable[Union[ToolDescriptor, Mapping[str, Any], DeferredTool]]
        ] = None,
        deferred: Optional[Iterable[Union[DeferredTool, Mapping[str, Any]]]] = None,
        *,
        schema_loader: Optional[_SchemaLoader] = None,
        schema_loaders: Optional[Mapping[str, _SchemaLoader]] = None,
        descriptors: Optional[
            Iterable[Union[ToolDescriptor, Mapping[str, Any], DeferredTool]]
        ] = None,
        deferred_tools: Optional[
            Iterable[Union[DeferredTool, Mapping[str, Any]]]
        ] = None,
        schema_resolver: Optional[_SchemaLoader] = None,
    ) -> None:
        """Create a catalog from eager descriptors and deferred tool entries."""
        self._tools: dict[str, ToolDescriptor] = {}
        self._deferred: dict[str, DeferredTool] = {}
        self._lock = RLock()
        self._schema_loader = schema_loader or schema_resolver
        self._schema_loaders = dict(schema_loaders or {})
        for item in (tools if tools is not None else descriptors) or ():
            self._add_item(item)
        for item in (deferred if deferred is not None else deferred_tools) or ():
            self._add_item(item)

    def _coerce_deferred(self, item: Any) -> DeferredTool:
        """Convert a mapping or descriptor-like value into a deferred entry."""
        if isinstance(item, DeferredTool):
            return item
        if isinstance(item, Mapping):
            loader = (
                item.get("loader") or item.get("schema_loader") or item.get("resolver")
            )
            if loader is None:
                loader = self._schema_loaders.get(str(item.get("name", "")))
            return DeferredTool(
                str(item.get("name", "")),
                str(item.get("description", "")),
                item.get("tags", ()),
                str(item.get("summary", "")),
                loader,
                server=str(item.get("server", "")),
                metadata=item.get("metadata")
                if isinstance(item.get("metadata"), Mapping)
                else None,
            )
        if isinstance(item, ToolDescriptor):
            return DeferredTool(
                item.name,
                item.description,
                item.tags,
                item.description,
                None,
                server=item.server,
                metadata=item.metadata,
            )
        raise TypeError("deferred tool entries must be DeferredTool or mapping values")

    def _add_item(self, item: Any) -> None:
        """Add one catalog item while rejecting duplicate names."""
        if isinstance(item, DeferredTool):
            deferred = item
        elif isinstance(item, Mapping) and bool(item.get("deferred", False)):
            deferred = self._coerce_deferred(item)
        else:
            if isinstance(item, Mapping):
                item = ToolDescriptor(
                    str(item.get("name", "")),
                    str(item.get("description", "")),
                    item.get(
                        "input_schema", item.get("inputSchema", item.get("schema", {}))
                    ),
                    item.get("tags", ()),
                    bool(item.get("deferred", False)),
                    str(item.get("server", "")),
                    item.get("metadata")
                    if isinstance(item.get("metadata"), Mapping)
                    else None,
                )
            if not isinstance(item, ToolDescriptor):
                raise TypeError(
                    "catalog tools must be ToolDescriptor, mapping, or DeferredTool values"
                )
            if not item.name:
                raise ValueError("catalog tool name must not be empty")
            with self._lock:
                if item.name in self._tools or item.name in self._deferred:
                    raise ToolCatalogError(f"duplicate tool name: {item.name}")
                self._tools[item.name] = item
            return
        if self._schema_loader is not None and deferred._loader is None:
            deferred._loader = self._schema_loader
        with self._lock:
            if deferred.name in self._tools or deferred.name in self._deferred:
                raise ToolCatalogError(f"duplicate tool name: {deferred.name}")
            self._deferred[deferred.name] = deferred

    def register(
        self, tool: Union[ToolDescriptor, Mapping[str, Any]]
    ) -> ToolDescriptor:
        """Register one eager descriptor and return its normalized form."""
        if isinstance(tool, Mapping):
            tool = ToolDescriptor(
                str(tool.get("name", "")),
                str(tool.get("description", "")),
                tool.get(
                    "input_schema", tool.get("inputSchema", tool.get("schema", {}))
                ),
                tool.get("tags", ()),
                False,
                str(tool.get("server", "")),
                tool.get("metadata")
                if isinstance(tool.get("metadata"), Mapping)
                else None,
            )
        if not isinstance(tool, ToolDescriptor):
            raise TypeError("register expects a ToolDescriptor or mapping")
        self._add_item(tool)
        return tool

    def register_deferred(
        self, tool: Union[DeferredTool, Mapping[str, Any]]
    ) -> DeferredTool:
        """Register one deferred tool and return it."""
        deferred = self._coerce_deferred(tool)
        self._add_item(deferred)
        return deferred

    @property
    def tools(self) -> tuple[ToolDescriptor, ...]:
        """Return eager descriptors in stable name order."""
        with self._lock:
            return tuple(self._tools[name] for name in sorted(self._tools))

    @property
    def deferred_tools(self) -> tuple[DeferredTool, ...]:
        """Return deferred entries in stable name order."""
        with self._lock:
            return tuple(self._deferred[name] for name in sorted(self._deferred))

    @property
    def names(self) -> tuple[str, ...]:
        """Return all exact tool names in stable order."""
        with self._lock:
            return tuple(sorted((*self._tools.keys(), *self._deferred.keys())))

    @property
    def all_descriptors(self) -> tuple[ToolDescriptor, ...]:
        """Return safe descriptors for both eager and deferred tools."""
        with self._lock:
            values: list[ToolDescriptor] = [
                self._tools[name] for name in sorted(self._tools)
            ]
            values.extend(
                self._deferred[name].descriptor() for name in sorted(self._deferred)
            )
        return tuple(values)

    @property
    def eager_model_schemas(self) -> _CallableList:
        """Return eager model schemas, callable for method-style consumers."""
        return _CallableList(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": _thaw_json(tool.input_schema),
                "tags": list(tool.tags),
            }
            for tool in self.tools
        )

    @property
    def model_schemas(self) -> _CallableList:
        """Return an alias for :attr:`eager_model_schemas`."""
        return self.eager_model_schemas

    @property
    def deferred_summaries(self) -> _CallableList:
        """Return bounded non-secret summaries for deferred tools."""
        return _CallableList(tool.search_summary() for tool in self.deferred_tools)

    @property
    def eager_schemas(self) -> _CallableList:
        """Return an alias for :attr:`eager_model_schemas`."""
        return self.eager_model_schemas

    @property
    def deferred_descriptors(self) -> _CallableList:
        """Return safe descriptors for deferred tools."""
        return _CallableList(tool.descriptor() for tool in self.deferred_tools)

    def __len__(self) -> int:
        """Return the total number of registered tool names."""
        with self._lock:
            return len(self._tools) + len(self._deferred)

    def __contains__(self, name: object) -> bool:
        """Return whether an exact tool name is registered."""
        with self._lock:
            return str(name) in self._tools or str(name) in self._deferred

    def __getitem__(self, name: str) -> ToolDescriptor:
        """Return an exact descriptor or raise a bounded missing-tool error."""
        return self.resolve(name)

    def get(
        self, name: str, default: Optional[ToolDescriptor] = None
    ) -> Optional[ToolDescriptor]:
        """Return an exact descriptor without raising when absent."""
        try:
            return self.resolve(name)
        except ToolNotFoundError:
            return default

    def resolve(self, name: str) -> ToolDescriptor:
        """Resolve an exact name to a safe descriptor without loading schemas."""
        clean_name = str(name or "")
        with self._lock:
            eager = self._tools.get(clean_name)
            deferred = self._deferred.get(clean_name)
        if eager is not None:
            return eager
        if deferred is not None:
            return deferred.descriptor()
        raise ToolNotFoundError(clean_name)

    def resolve_exact(self, name: str) -> ToolDescriptor:
        """Return the exact descriptor for a name."""
        return self.resolve(name)

    def resolve_tool(self, name: str) -> ToolDescriptor:
        """Alias for :meth:`resolve`."""
        return self.resolve(name)

    async def resolve_schema(self, name: str) -> Mapping[str, Any]:
        """Resolve an exact tool's schema, loading a deferred entry if needed."""
        clean_name = str(name or "")
        with self._lock:
            eager = self._tools.get(clean_name)
            deferred = self._deferred.get(clean_name)
        if eager is not None:
            return eager.input_schema
        if deferred is not None:
            try:
                return await deferred.resolve_schema()
            except ToolResolutionError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ToolResolutionError(
                    clean_name, "schema resolution failed"
                ) from None
        raise ToolNotFoundError(clean_name)

    async def load_schema(self, name: str) -> Mapping[str, Any]:
        """Alias for :meth:`resolve_schema`."""
        return await self.resolve_schema(name)

    async def validate_arguments(
        self, name: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Validate arguments against an exact tool schema and return a copy."""
        if arguments is None:
            values: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            values = dict(arguments)
        else:
            raise TypeError("tool arguments must be a mapping")
        schema = await self.resolve_schema(name)
        validate_json_schema(values, schema)
        return values

    def search(self, query: str = "", max_results: int = 10) -> list[ToolDescriptor]:
        """Rank name, description, and tags without exposing arguments or results."""
        try:
            limit = int(max_results)
        except (TypeError, ValueError):
            limit = 10
        if limit <= 0:
            return []
        limit = min(limit, 100)
        terms = [
            term for term in re.findall(r"[\w-]+", str(query or "").lower()) if term
        ]
        ranked: list[tuple[int, str, ToolDescriptor]] = []
        for descriptor in self.all_descriptors:
            name = descriptor.name.lower()
            description = descriptor.description.lower()
            tags = [tag.lower() for tag in descriptor.tags]
            score = 0
            if not terms:
                score = 1
            else:
                for term in terms:
                    if name == term:
                        score += 100
                    elif name.startswith(term):
                        score += 50
                    elif term in name:
                        score += 25
                    if term in description:
                        score += 6
                    if any(term in tag for tag in tags):
                        score += 12
            if score > 0 or not terms:
                ranked.append((score, descriptor.name, descriptor))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [ToolCatalog._safe_search_descriptor(item[2]) for item in ranked[:limit]]

    @staticmethod
    def _safe_search_descriptor(descriptor: ToolDescriptor) -> ToolDescriptor:
        """Return metadata-only search projection without private SDK metadata."""
        return ToolDescriptor(
            descriptor.name,
            descriptor.description,
            input_schema=descriptor.input_schema,
            tags=descriptor.tags,
            deferred=descriptor.deferred,
            server=descriptor.server,
            metadata=None,
        )

    def search_tools(
        self, query: str = "", max_results: int = 10
    ) -> list[ToolDescriptor]:
        """Alias for :meth:`search`."""
        return self.search(query, max_results)

    def validate_required_tools(
        self,
        required_tools: Iterable[str],
        *,
        recipe: Optional[Any] = None,
        platform: Optional[Any] = None,
    ) -> tuple[str, ...]:
        """Validate required names for an optional recipe or platform context."""
        if recipe is not None:
            required_tools = _required_from_recipe(recipe, required_tools)
        if platform is not None:
            platform_required = _required_from_platform(platform)
            required_tools = (*tuple(required_tools), *platform_required)
        required = tuple(
            dict.fromkeys(str(item) for item in required_tools if str(item))
        )
        missing = tuple(name for name in required if name not in self)
        if missing:
            raise RequiredToolsError(missing)
        return required

    def validate_recipe(self, recipe: Any) -> tuple[str, ...]:
        """Validate tools declared by a recipe mapping or object."""
        return self.validate_required_tools(_recipe_requirements(recipe), recipe=recipe)

    def require_tools(
        self, required_tools: Iterable[str], **kwargs: Any
    ) -> tuple[str, ...]:
        """Alias for :meth:`validate_required_tools`."""
        return self.validate_required_tools(required_tools, **kwargs)

    def validate_platform(
        self, platform: Any, required_tools: Iterable[str] = ()
    ) -> tuple[str, ...]:
        """Validate tools declared for a platform mapping or name."""
        return self.validate_required_tools(required_tools, platform=platform)


ToolCatalogSearch = ToolCatalog


def _recipe_requirements(recipe: Any) -> tuple[str, ...]:
    """Extract required tool names from common recipe shapes."""
    if isinstance(recipe, Mapping):
        value = recipe.get(
            "required_tools", recipe.get("required", recipe.get("tools", ()))
        )
    else:
        value = getattr(recipe, "required_tools", getattr(recipe, "required", ()))
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(str(key) for key in value)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


def _required_from_recipe(recipe: Any, fallback: Iterable[str]) -> Iterable[str]:
    """Return recipe requirements or the caller's fallback requirements."""
    requirements = _recipe_requirements(recipe)
    return requirements if requirements else fallback


def _required_from_platform(platform: Any) -> tuple[str, ...]:
    """Extract required tools from a platform mapping or object."""
    if isinstance(platform, Mapping):
        value = platform.get("required_tools", platform.get("required", ()))
    else:
        value = getattr(platform, "required_tools", getattr(platform, "required", ()))
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


def _json_type_matches(value: Any, expected: str) -> bool:
    """Return whether a Python value matches a JSON Schema primitive type."""
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _schema_error(path: str, message: str) -> SchemaValidationError:
    """Construct a bounded schema validation error."""
    return SchemaValidationError(path, message)


def _validate_schema_definition(schema: Any, path: str = "$", depth: int = 0) -> None:
    """Validate the bounded structural subset of a JSON Schema definition."""
    if depth > 64:
        raise _schema_error(path, "schema nesting exceeds the supported bound")
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise _schema_error(path, "schema must be an object or boolean")
    if "type" in schema:
        expected = schema["type"]
        values = expected if isinstance(expected, (list, tuple)) else (expected,)
        allowed = {"object", "array", "string", "integer", "number", "boolean", "null"}
        if not values or any(str(item) not in allowed for item in values):
            raise _schema_error(path, "schema contains an unsupported type")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, (list, tuple))
        or any(not isinstance(item, str) for item in required)
    ):
        raise _schema_error(path, "required must be an array of names")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise _schema_error(path, "properties must be an object")
        for name, child in properties.items():
            _validate_schema_definition(child, f"{path}.{name}", depth + 1)
    items = schema.get("items")
    if isinstance(items, (list, tuple)):
        for index, child in enumerate(items):
            _validate_schema_definition(child, f"{path}[{index}]", depth + 1)
    elif items is not None:
        _validate_schema_definition(items, f"{path}[]", depth + 1)
    for keyword in ("allOf", "anyOf", "oneOf"):
        children = schema.get(keyword)
        if children is not None:
            if not isinstance(children, (list, tuple)):
                raise _schema_error(path, f"{keyword} must be an array")
            for index, child in enumerate(children):
                _validate_schema_definition(child, f"{path}[{index}]", depth + 1)
    if "not" in schema:
        _validate_schema_definition(schema["not"], f"{path}.not", depth + 1)
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise _schema_error(path, "pattern must be a string")
        try:
            re.compile(pattern)
        except re.error:
            raise _schema_error(path, "schema pattern is invalid") from None


def validate_schema_definition(schema: Any) -> None:
    """Validate a JSON Schema definition without validating an instance."""
    _validate_schema_definition(schema)


def _validate_schema(value: Any, schema: Any, path: str, depth: int = 0) -> None:
    """Validate a JSON value against a practical JSON Schema subset."""
    if depth > 64:
        raise _schema_error(path, "schema nesting exceeds the supported bound")
    if schema is True or schema == {}:
        return
    if schema is False:
        raise _schema_error(path, "schema rejects all values")
    if not isinstance(schema, Mapping):
        raise _schema_error(path, "schema must be an object or boolean")
    if "type" in schema:
        expected = schema["type"]
        expected_values = expected if isinstance(expected, list) else [expected]
        if not any(_json_type_matches(value, str(item)) for item in expected_values):
            raise _schema_error(path, "value has the wrong JSON type")
    if "const" in schema and value != schema["const"]:
        raise _schema_error(path, "value does not match const")
    if "enum" in schema and value not in schema["enum"]:
        raise _schema_error(path, "value is not an allowed enum member")
    for keyword in ("allOf",):
        for subschema in schema.get(keyword, []) or []:
            _validate_schema(value, subschema, path, depth + 1)
    if "anyOf" in schema and not any(
        _schema_matches(value, item, depth + 1)
        for item in schema.get("anyOf", []) or []
    ):
        raise _schema_error(path, "value does not match anyOf")
    if "oneOf" in schema:
        matches = sum(
            1
            for item in schema.get("oneOf", []) or []
            if _schema_matches(value, item, depth + 1)
        )
        if matches != 1:
            raise _schema_error(path, "value must match exactly one oneOf branch")
    if "not" in schema and _schema_matches(value, schema["not"], depth + 1):
        raise _schema_error(path, "value matches a forbidden not schema")
    if isinstance(value, Mapping):
        _validate_object(value, schema, path, depth)
    elif isinstance(value, (list, tuple)):
        _validate_array(value, schema, path, depth)
    elif isinstance(value, str):
        _validate_string(value, schema, path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(value, schema, path)
    if schema.get("uniqueItems") and isinstance(value, (list, tuple)):
        serialized = [json.dumps(item, sort_keys=True, default=str) for item in value]
        if len(serialized) != len(set(serialized)):
            raise _schema_error(path, "array items must be unique")


def _schema_matches(value: Any, schema: Any, depth: int) -> bool:
    """Return whether a value matches a schema without exposing its value."""
    try:
        _validate_schema(value, schema, "$", depth)
    except SchemaValidationError:
        return False
    return True


def _validate_object(
    value: Mapping[str, Any], schema: Mapping[str, Any], path: str, depth: int
) -> None:
    """Validate object properties, required fields, and additional properties."""
    required = schema.get("required", ()) or ()
    if isinstance(required, str):
        required = (required,)
    for name in required:
        if str(name) not in value:
            raise _schema_error(f"{path}.{name}", "required property is missing")
    properties = schema.get("properties", {}) or {}
    if not isinstance(properties, Mapping):
        raise _schema_error(path, "properties must be an object")
    for name, item in value.items():
        if name in properties:
            _validate_schema(item, properties[name], f"{path}.{name}", depth + 1)
        elif schema.get("additionalProperties") is False:
            raise _schema_error(f"{path}.{name}", "additional property is not allowed")
        elif isinstance(schema.get("additionalProperties"), Mapping):
            _validate_schema(
                item, schema["additionalProperties"], f"{path}.{name}", depth + 1
            )


def _validate_array(
    value: Sequence[Any], schema: Mapping[str, Any], path: str, depth: int
) -> None:
    """Validate array length and item schemas."""
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(value) < minimum:
        raise _schema_error(path, "array is shorter than minItems")
    if isinstance(maximum, int) and len(value) > maximum:
        raise _schema_error(path, "array is longer than maxItems")
    items = schema.get("items")
    if items is not None:
        if isinstance(items, (list, tuple)):
            for index, item in enumerate(value[: len(items)]):
                _validate_schema(item, items[index], f"{path}[{index}]", depth + 1)
        else:
            for index, item in enumerate(value):
                _validate_schema(item, items, f"{path}[{index}]", depth + 1)


def _validate_string(value: str, schema: Mapping[str, Any], path: str) -> None:
    """Validate string length and regular-expression constraints."""
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise _schema_error(path, "string is shorter than minLength")
    if isinstance(maximum, int) and len(value) > maximum:
        raise _schema_error(path, "string is longer than maxLength")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            matches = re.search(pattern, value) is not None
        except re.error:
            raise _schema_error(path, "schema pattern is invalid") from None
        if not matches:
            raise _schema_error(path, "string does not match pattern")


def _validate_number(
    value: Union[int, float], schema: Mapping[str, Any], path: str
) -> None:
    """Validate finite numeric bounds and multipleOf constraints."""
    if isinstance(value, float) and not math.isfinite(value):
        raise _schema_error(path, "number must be finite")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        raise _schema_error(path, "number is below minimum")
    if isinstance(maximum, (int, float)) and value > maximum:
        raise _schema_error(path, "number is above maximum")
    if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
        raise _schema_error(path, "number is below exclusiveMinimum")
    if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
        raise _schema_error(path, "number is above exclusiveMaximum")
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        quotient = value / multiple
        if abs(quotient - round(quotient)) > 1e-9:
            raise _schema_error(path, "number does not satisfy multipleOf")


def validate_json_schema(instance: Any, schema: Mapping[str, Any]) -> None:
    """Validate a JSON-like instance against a bounded JSON Schema subset.

    The validator has no third-party dependency and supports the common MCP
    object, array, scalar, combinator, enum, and numeric/string constraints.
    """
    if not isinstance(schema, (Mapping, bool)):
        raise SchemaValidationError("$", "schema must be an object or boolean")
    _validate_schema_definition(schema)
    _validate_schema(instance, schema, "$")


def validate_required_tools(
    catalog: ToolCatalog,
    required_tools: Iterable[str],
    *,
    recipe: Optional[Any] = None,
    platform: Optional[Any] = None,
) -> tuple[str, ...]:
    """Validate recipe/platform requirements against a catalog."""
    if not isinstance(catalog, ToolCatalog):
        raise TypeError("catalog must be a ToolCatalog")
    return catalog.validate_required_tools(
        required_tools, recipe=recipe, platform=platform
    )
