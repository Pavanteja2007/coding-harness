"""Internal async/sync invocation helpers shared by ACP adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from typing import Any, Callable, Iterable


class ImmediateAwaitable:
    """An already-computed value that can also be awaited by async callers."""

    def __init__(self, value: Any = None) -> None:
        self.value = value

    def __await__(self):
        if inspect.isawaitable(self.value):
            result = yield from self.value.__await__()
            return result
        if False:
            yield
        return self.value

    def __repr__(self) -> str:
        return f"ImmediateAwaitable({self.value!r})"


def decode_wire_message(value: Any) -> Mapping[str, Any] | None:
    """Decode a mapping, typed ACP model, or one JSONL text message."""
    if value is None:
        return None
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ACP transport emitted invalid JSON") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
        raise ValueError("ACP transport message must be a JSON object")
    return None


async def call_maybe_async(
    owner: Any,
    names: str | Iterable[str],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a named method whether it is synchronous or asynchronous.

    Synchronous methods run in a worker thread when an event loop is active,
    preventing a blocking transport or agent call from stalling that loop.
    """
    candidates = (names,) if isinstance(names, str) else tuple(names)
    method: Callable[..., Any] | None = None
    for name in candidates:
        candidate = getattr(owner, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        raise AttributeError("required ACP transport method is unavailable")
    if inspect.iscoroutinefunction(method):
        result = method(*args, **kwargs)
    else:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = method(*args, **kwargs)
        else:
            result = await asyncio.to_thread(method, *args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def call_optional(
    owner: Any,
    names: str | Iterable[str],
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    """Call the first available optional method and return a default if absent."""
    candidates = (names,) if isinstance(names, str) else tuple(names)
    for name in candidates:
        if callable(getattr(owner, name, None)):
            return await call_maybe_async(owner, name, *args, **kwargs)
    return default


async def maybe_await(value: Any) -> Any:
    """Await an awaitable value, leaving ordinary values unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


def run_sync(value: Any) -> Any:
    """Resolve an async cleanup value when no event loop is running."""
    if isinstance(value, ImmediateAwaitable):
        value = value.value
    if inspect.isawaitable(value):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if inspect.iscoroutine(value):
                return asyncio.run(value)
            return value
        return value
    return value


__all__ = [
    "ImmediateAwaitable",
    "call_maybe_async",
    "call_optional",
    "decode_wire_message",
    "maybe_await",
    "run_sync",
]
