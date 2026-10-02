"""Duck-typed tool-catalog projections shared by local and remote SDK transports."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Mapping
from typing import Any, Optional

from shared.security import is_sensitive_key, redact_secrets, redact_text

from .errors import ToolCatalogError, ToolNotFoundError, ToolResolutionError
from .models import Tool

__all__ = [
    "catalog_get",
    "catalog_list",
    "catalog_schema",
    "catalog_search",
    "safe_catalog_json",
    "tool_from_wire",
    "tool_to_wire",
]

_MAX_JSON_STRING = 16_384
_MAX_JSON_ITEMS = 256
_MAX_JSON_DEPTH = 32
_MAX_JSON_BUDGET = 32_768


def _bounded_json(
    value: Any,
    *,
    depth: int = 0,
    budget: Optional[list[int]] = None,
) -> Any:
    """Return recursively redacted, size-bounded JSON-compatible data."""
    if budget is None:
        budget = [_MAX_JSON_BUDGET]
    if budget[0] <= 0 or depth >= _MAX_JSON_DEPTH:
        return "[TRUNCATED]"
    budget[0] -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)[:_MAX_JSON_STRING]
    if isinstance(value, bytes):
        return "[BINARY]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        try:
            items = list(value.items())[:_MAX_JSON_ITEMS]
        except Exception:
            items = []
        for key, item in items:
            if budget[0] <= 0:
                result["[TRUNCATED]"] = True
                break
            key_text = redact_text(str(key))[:_MAX_JSON_STRING]
            if is_sensitive_key(key_text):
                result[key_text] = "[REDACTED_SECRET]"
            else:
                result[key_text] = _bounded_json(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result_list: list[Any] = []
        try:
            values = list(value)[:_MAX_JSON_ITEMS]
        except Exception:
            values = []
        for item in values:
            if budget[0] <= 0:
                result_list.append("[TRUNCATED]")
                break
            result_list.append(_bounded_json(item, depth=depth + 1, budget=budget))
        return result_list
    try:
        safe = redact_secrets(value)
    except Exception:
        safe = str(type(value).__name__)
    if safe is value or isinstance(safe, (Mapping, list, tuple, set, frozenset)):
        return _bounded_json(safe, depth=depth, budget=budget)
    return redact_text(str(safe))[:_MAX_JSON_STRING]


def safe_catalog_json(value: Any) -> Any:
    """Return a bounded redacted value suitable for a catalog HTTP response."""
    return _bounded_json(value)


def _raw_descriptor(value: Any) -> Mapping[str, Any]:
    """Normalize a descriptor-like object to a mapping without executing tools."""
    if isinstance(value, Tool):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    name = getattr(value, "name", "")
    if not name:
        raise ToolCatalogError("tool catalog returned an unnamed descriptor")
    schema = getattr(value, "input_schema", None)
    if schema is None:
        schema = getattr(value, "inputSchema", None)
    if schema is None:
        schema = getattr(value, "schema", None)
    return {
        "name": name,
        "description": getattr(value, "description", ""),
        "input_schema": schema if isinstance(schema, Mapping) else {},
        "tags": getattr(value, "tags", ()),
        "deferred": bool(
            getattr(value, "deferred", getattr(value, "is_deferred", False))
        ),
        "server": getattr(value, "server", ""),
        "metadata": getattr(value, "metadata", {}),
    }


def tool_from_wire(value: Any) -> Tool:
    """Convert a catalog descriptor or wire mapping to the public Tool value."""
    if isinstance(value, Tool):
        return value
    return Tool.from_dict(_raw_descriptor(value))


def tool_to_wire(value: Any) -> dict[str, Any]:
    """Return a bounded redacted wire descriptor for one catalog tool."""
    return dict(safe_catalog_json(tool_from_wire(value).to_catalog_dict()))


def _invoke(value: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a catalog method and leave asynchronous values untouched."""
    if not callable(value):
        return value
    return value(*args, **kwargs)


def _await_if_needed(value: Any) -> Any:
    """Resolve an awaitable catalog result from synchronous SDK/server code."""
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_consume(value))

    output: list[Any] = []
    errors: list[BaseException] = []

    async def consume() -> Any:
        return await value

    def run() -> None:
        try:
            output.append(asyncio.run(consume()))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="neo-tool-schema-resolver")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return output[0] if output else None


async def _consume(value: Any) -> Any:
    """Await one catalog result while preserving cancellation and typed errors."""
    return await value


def _missing_tool(name: Any) -> ToolNotFoundError:
    """Return a bounded exact-tool miss with a redacted name."""
    return ToolNotFoundError(f"tool not found: {redact_text(str(name or ''))[:160]}")


def _catalog_error(exc: BaseException, *, operation: str) -> ToolCatalogError:
    """Translate a private catalog error into a bounded SDK error."""
    if isinstance(exc, (ToolNotFoundError, ToolResolutionError, ToolCatalogError)):
        return exc
    name = type(exc).__name__
    message = redact_text(str(exc)).casefold()
    if name in {"ToolNotFoundError", "KeyError"} or "not found" in message:
        return _missing_tool("")
    if name in {
        "ToolResolutionError",
        "ToolResolutionFailure",
        "SchemaValidationError",
    }:
        return ToolResolutionError("tool schema could not be resolved")
    return ToolCatalogError(f"tool catalog {operation} failed")


def _catalog_values(catalog: Any) -> list[Any]:
    """Return descriptor-like values from common catalog shapes."""
    if catalog is None:
        return []
    for attribute in ("all_descriptors", "descriptors"):
        try:
            value = getattr(catalog, attribute)
        except AttributeError:
            continue
        value = _invoke(value)
        value = _await_if_needed(value)
        if value is not None:
            if isinstance(value, Mapping):
                return [value]
            try:
                return list(value)
            except TypeError:
                continue
    values: list[Any] = []
    seen: set[str] = set()
    for attribute in (
        "tools",
        "deferred_descriptors",
        "deferred_tools",
        "list_tools",
        "list",
    ):
        try:
            value = getattr(catalog, attribute)
        except AttributeError:
            continue
        value = _invoke(value)
        value = _await_if_needed(value)
        if value is None:
            continue
        if isinstance(value, Mapping):
            nested = value.get("tools") if "tools" in value else None
            candidates = list(nested) if isinstance(nested, (list, tuple)) else [value]
        else:
            try:
                candidates = list(value)
            except TypeError:
                continue
        for candidate in candidates:
            name = str(getattr(candidate, "name", ""))
            if not name and isinstance(candidate, Mapping):
                name = str(candidate.get("name", ""))
            key = name or f"item-{len(values)}"
            if key in seen:
                continue
            seen.add(key)
            values.append(candidate)
    if values:
        return values
    try:
        return list(catalog)
    except TypeError:
        return []


def catalog_list(catalog: Any) -> list[Tool]:
    """List catalog tools without resolving deferred schemas."""
    try:
        values = _catalog_values(catalog)
        return [tool_from_wire(value) for value in values]
    except ToolCatalogError:
        raise
    except Exception as exc:
        raise _catalog_error(exc, operation="listing") from None


def catalog_search(catalog: Any, query: str = "", max_results: int = 10) -> list[Tool]:
    """Search catalog metadata without resolving deferred schemas."""
    try:
        limit = max(0, int(max_results))
    except (TypeError, ValueError):
        limit = 10
    limit = min(limit, 100)
    if catalog is None or limit == 0:
        return []
    for method_name in ("search_tools", "search"):
        try:
            method = getattr(catalog, method_name, None)
        except AttributeError:
            continue
        except Exception as exc:
            raise _catalog_error(exc, operation="search") from None
        if not callable(method):
            continue
        try:
            values = _await_if_needed(_invoke(method, query, limit))
            return [tool_from_wire(value) for value in (values or [])][:limit]
        except Exception as exc:
            raise _catalog_error(exc, operation="search") from None
    terms = [term for term in str(query or "").casefold().split() if term]
    values = catalog_list(catalog)
    ranked: list[tuple[int, str, Tool]] = []
    for tool in values:
        text = " ".join(
            [tool.name, tool.description, *tool.tags, tool.server]
        ).casefold()
        score = 0
        for term in terms:
            if term == tool.name.casefold():
                score += 100
            elif term in tool.name.casefold():
                score += 50
            elif term in text:
                score += 10
        if not terms or score:
            ranked.append((score, tool.name, tool))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def catalog_get(catalog: Any, name: str) -> Tool:
    """Resolve one exact catalog name without loading a deferred schema."""
    selected = str(name or "").strip()
    if catalog is None or not selected:
        raise _missing_tool(selected)
    for method_name in (
        "resolve",
        "resolve_tool",
        "resolve_exact",
        "get_tool",
        "get",
    ):
        try:
            method = getattr(catalog, method_name, None)
        except AttributeError:
            continue
        except Exception as exc:
            raise _catalog_error(exc, operation="lookup") from None
        if not callable(method):
            continue
        try:
            value = _await_if_needed(_invoke(method, selected))
        except Exception as exc:
            translated = _catalog_error(exc, operation="lookup")
            if isinstance(translated, ToolNotFoundError):
                raise _missing_tool(selected) from None
            raise translated from None
        if value is not None:
            return tool_from_wire(value)
    raise _missing_tool(selected)


def catalog_schema(catalog: Any, name: str) -> Any:
    """Resolve one exact tool schema, loading deferred schemas only on request."""
    selected = str(name or "").strip()
    descriptor = catalog_get(catalog, selected)
    resolver = None
    for method_name in (
        "resolve_schema",
        "resolve_tool_schema",
        "load_schema",
        "resolve",
    ):
        try:
            method = getattr(catalog, method_name, None)
        except AttributeError:
            continue
        except Exception as exc:
            raise _catalog_error(exc, operation="schema resolution") from None
        if callable(method):
            resolver = method
            break
    if resolver is not None:
        try:
            schema = _await_if_needed(_invoke(resolver, selected))
        except Exception as exc:
            raise _catalog_error(exc, operation="schema resolution") from None
    else:
        schema = descriptor.input_schema
    if isinstance(schema, Mapping) and set(schema) == {"schema"}:
        schema = schema["schema"]
    if not isinstance(schema, (Mapping, bool)):
        raise ToolResolutionError("tool schema could not be resolved")
    if descriptor.deferred and isinstance(schema, Mapping) and not schema:
        raise ToolResolutionError("tool schema could not be resolved")
    return safe_catalog_json(schema)
