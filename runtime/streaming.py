"""Streaming primitives for the model boundary (VEX-CEILING-10).

A provider that answers incrementally is the difference between "the
terminal is alive" and "the terminal has hung". This module owns the
two ends of that path and nothing else:

* **Producer** — :class:`StreamAssembler` folds a litellm stream into the
  same shape a non-streamed response has, so every consumer above
  :func:`runtime.model_router.call_model` (text-protocol parsing, native
  tool-call normalization, usage/cost accounting, the ledger) works
  unchanged whether or not the provider streamed. It also carries the
  coalescing window: a delta is *observed* immediately and *delivered*
  to the callback at most once per window, so frame cost is a function
  of wall time, never of token rate.
* **Transport** — :class:`DeltaSink` is the harness-side collector that
  turns provider deltas into `model_delta` journal rows. It is the only
  place that decides what a delta record looks like.

Design constraints this module holds to:

* **Never raise into a run.** A provider chunk that cannot be understood
  is counted, not raised: a malformed stream degrades to "fewer
  characters" and the assembled text is still returned.
* **Bounded.** Text accumulation is capped (default 256 KiB) so a runaway
  stream cannot exhaust memory before the provider's own limit fires.
* **Honest.** ``streamed`` is recorded from what actually happened, and a
  provider that does not support streaming falls back to a single
  non-streamed call with ``streamed=False`` — never claimed as streamed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: Coalescing window bounds (ms). The producer never delivers more often
#: than ``MIN_WINDOW_MS`` and never holds a delta longer than
#: ``MAX_WINDOW_MS`` once the first delta of a window has arrived, so the
#: first token after an idle period is visible promptly while a fast
#: token storm costs one callback per window rather than one per token.
MIN_WINDOW_MS = 40
MAX_WINDOW_MS = 500

#: Hard cap on the text a single call will assemble. A provider stream
#: that exceeds this is truncated with an explicit marker rather than
#: growing without bound.
DEFAULT_MAX_CHARS = 256 * 1024

#: The truncation marker appended when ``max_chars`` is reached. It is a
#: constant so a consumer can assert on it.
TRUNCATION_MARKER = "\n[stream truncated]"


def _text_of(value: Any) -> str:
    """Coerce a provider delta to text without ever raising."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return ""
    if isinstance(value, (list, tuple)):
        return "".join(_text_of(item) for item in value)
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return ""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a mapping or an attribute holder, tolerantly."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _has_choices(chunk: Any) -> bool:
    """Whether a frame is shaped like a provider chunk at all.

    A frame that carries a non-list ``choices`` (or none at all) is
    tolerated — that is how a usage-only tail frame and a bare sentinel
    look — but it is *counted* as malformed, so a provider that starts
    sending garbage is visible in the receipt instead of silently
    producing an empty answer.
    """
    choices = _get(chunk, "choices")
    if choices is None:
        return False
    return isinstance(choices, (list, tuple))


@dataclass
class StreamStats:
    """Measured receipt for one streamed call.

    Every field is a measurement, not an intention: ``chunks_seen`` counts
    provider chunks actually iterated, ``deliveries`` counts callbacks
    actually made. ``events_per_delivery`` is the coalescing ratio the
    ceiling prompt asks to be token-rate independent.
    """

    chunks_seen: int = 0
    malformed_chunks: int = 0
    deliveries: int = 0
    chars: int = 0
    first_token_s: Optional[float] = None
    total_s: float = 0.0
    window_ms: int = MIN_WINDOW_MS
    truncated: bool = False
    tool_calls: int = 0
    finish_reason: str = ""

    @property
    def events_per_delivery(self) -> float:
        """Deltas observed per delivered callback.

        ``0.0`` when nothing was observed — a vacuous ratio must not be
        reported as a coalescing win.
        """
        if not self.deliveries:
            return 0.0
        return round(self.chunks_seen / float(self.deliveries), 4)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe receipt for the ledger / trace."""
        return {
            "chunks_seen": int(self.chunks_seen),
            "malformed_chunks": int(self.malformed_chunks),
            "deliveries": int(self.deliveries),
            "chars": int(self.chars),
            "events_per_delivery": self.events_per_delivery,
            "first_token_s": (
                None if self.first_token_s is None else round(self.first_token_s, 4)
            ),
            "total_s": round(self.total_s, 4),
            "window_ms": int(self.window_ms),
            "truncated": bool(self.truncated),
            "tool_calls": int(self.tool_calls),
            "finish_reason": self.finish_reason,
        }


@dataclass
class StreamAssembler:
    """Fold provider stream chunks into a single response-shaped result.

    ``on_delta`` is invoked at most once per coalescing window with the
    text observed in that window. It is invoked from the *consumer's*
    thread — the thread iterating the provider stream — and must not
    block: a slow callback slows the stream, which is why
    :mod:`cli.streamview` re-coalesces for the frame path.
    """

    on_delta: Optional[Callable[[str], None]] = None
    window_ms: int = MIN_WINDOW_MS
    max_chars: int = DEFAULT_MAX_CHARS
    clock: Callable[[], float] = time.monotonic

    _pending: str = field(default="", init=False, repr=False)
    _window_started: Optional[float] = field(default=None, init=False, repr=False)
    _started: Optional[float] = field(default=None, init=False, repr=False)
    _parts: List[str] = field(default_factory=list, init=False, repr=False)
    _tool_parts: Dict[int, Dict[str, Any]] = field(default_factory=dict, init=False)
    _usage: Optional[Dict[str, Any]] = field(default=None, init=False, repr=False)
    _stats: StreamStats = field(default_factory=StreamStats, init=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # The window is a CONTRACT, not a preference: below 40ms a fast
        # token storm degenerates back into one callback per token, above
        # 500ms the UI feels dead. A caller that asks for less gets the
        # floor. Tests that need determinism inject ``clock`` instead of
        # asking for an impossible window.
        self.window_ms = max(
            MIN_WINDOW_MS, min(int(self.window_ms or MIN_WINDOW_MS), MAX_WINDOW_MS)
        )
        self.max_chars = max(1024, int(self.max_chars or DEFAULT_MAX_CHARS))

    # -- lifecycle ---------------------------------------------------------

    def begin(self) -> None:
        """Mark the request as sent. Called immediately before iterating."""
        with self._lock:
            self._started = self.clock()

    # -- producer side -----------------------------------------------------

    def feed(self, chunk: Any) -> None:
        """Consume one provider chunk. Never raises."""
        try:
            with self._lock:
                self._stats.chunks_seen += 1
                if self._started is None:
                    self._started = self.clock()
            self._absorb_finish_reason(chunk)
            self._absorb_usage(chunk)
            delta = self._absorb_text(chunk)
            calls = self._absorb_tool_calls(chunk)
            if calls:
                with self._lock:
                    self._stats.tool_calls = max(self._stats.tool_calls, len(calls))
            if not delta:
                if self._stats.chunks_seen and self._stats.first_token_s is None:
                    # A provider frame that carried no text is still the
                    # first thing that came back — the UI must be able to
                    # leave "waiting for first token" on it.
                    with self._lock:
                        self._stats.first_token_s = self.clock() - (
                            self._started or 0.0
                        )
                if (
                    chunk is not None
                    and not _has_choices(chunk)
                    and _get(chunk, "usage") is None
                ):
                    with self._lock:
                        self._stats.malformed_chunks += 1
                return
            with self._lock:
                if self._stats.first_token_s is None:
                    self._stats.first_token_s = self.clock() - (self._started or 0.0)
                room = self.max_chars - sum(len(p) for p in self._parts) - len(delta)
                if room <= 0:
                    self._stats.truncated = True
                    return
                if room < len(delta):
                    delta = delta[:room]
                    self._stats.truncated = True
                self._parts.append(delta)
                self._pending += delta
                self._stats.chars += len(delta)
            self._maybe_deliver(force=False)
        except Exception:
            with self._lock:
                self._stats.malformed_chunks += 1

    def _absorb_text(self, chunk: Any) -> str:
        if not _has_choices(chunk):
            return ""
        choices = _get(chunk, "choices") or []
        choice = choices[0]
        delta = _get(choice, "delta")
        if delta is None:
            return ""
        return _text_of(_get(delta, "content"))

    def _absorb_finish_reason(self, chunk: Any) -> None:
        if not _has_choices(chunk):
            return
        choices = _get(chunk, "choices") or []
        reason = _get(choices[0], "finish_reason")
        if reason:
            with self._lock:
                self._stats.finish_reason = str(reason)

    def _absorb_usage(self, chunk: Any) -> None:
        """Capture a provider usage frame if one arrives.

        Streamed calls only report usage when the provider volunteers it
        (OpenAI's ``stream_options.include_usage``, Anthropic's
        ``message_start``/``message_delta`` pair). We never force the
        option — a gateway that rejects an unknown parameter would fail
        the whole call for a nicety — so a stream without usage is
        estimated by the caller and labeled as an estimate.
        """
        usage = _get(chunk, "usage")
        if usage is None:
            return
        if isinstance(usage, dict):
            payload: Any = usage
        else:
            payload = {
                key: getattr(usage, key, None)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cost",
                )
                if getattr(usage, key, None) is not None
            }
        if not payload:
            return
        with self._lock:
            self._usage = payload

    def _absorb_tool_calls(self, chunk: Any) -> List[Dict[str, Any]]:
        """Merge streamed tool-call fragments into call indices.

        Providers stream a tool call in fragments: the first frame carries
        the id/name, later frames carry argument substrings. Accumulating
        by index is what makes a streamed native tool call equivalent to a
        non-streamed one.
        """
        if not _has_choices(chunk):
            return []
        choices = _get(chunk, "choices") or []
        if not choices:
            return []
        delta = _get(choices[0], "delta")
        raw = _get(delta, "tool_calls") if delta is not None else None
        if not raw:
            return []
        with self._lock:
            for item in raw:
                try:
                    index = int(_get(item, "index", len(self._tool_parts)) or 0)
                except (TypeError, ValueError):
                    index = len(self._tool_parts)
                slot = self._tool_parts.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                identifier = _get(item, "id")
                if identifier:
                    slot["id"] = str(identifier)
                function = _get(item, "function")
                if function is not None:
                    name = _get(function, "name")
                    if name:
                        slot["name"] = str(name)
                    arguments = _get(function, "arguments")
                    if isinstance(arguments, str) and arguments:
                        slot["arguments"] += arguments
            return [dict(slot) for slot in self._tool_parts.values()]

    def _maybe_deliver(self, force: bool) -> None:
        """Deliver the pending window if the window has closed."""
        now = self.clock()
        with self._lock:
            if not self._pending:
                return
            if self._window_started is None:
                self._window_started = now
                return
            elapsed_ms = (now - self._window_started) * 1000.0
            if not force and elapsed_ms < self.window_ms:
                return
            payload = self._pending
            self._pending = ""
            self._window_started = None
            self._stats.deliveries += 1
        callback = self.on_delta
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            # A UI that raises must not abort the provider stream; the
            # text still lands in the assembled result below.
            with self._lock:
                self._stats.malformed_chunks += 1

    def flush(self) -> None:
        """Deliver any pending window and finalize the stats."""
        self._maybe_deliver(force=True)
        with self._lock:
            if self._started is not None and self._stats.total_s == 0.0:
                self._stats.total_s = max(0.0, self.clock() - self._started)

    # -- consumer side -----------------------------------------------------

    @property
    def text(self) -> str:
        """The complete assistant text observed so far."""
        with self._lock:
            body = "".join(self._parts)
            if self._stats.truncated:
                body += TRUNCATION_MARKER
            return body

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        """Normalized provider-native tool calls assembled from fragments."""
        with self._lock:
            return [
                {
                    "id": slot["id"] or f"call_{index + 1}",
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": _parse_arguments(slot["arguments"]),
                    },
                }
                for index, slot in sorted(self._tool_parts.items())
                if slot["name"] or slot["arguments"]
            ]

    @property
    def stats(self) -> StreamStats:
        """The measured receipt for this stream."""
        with self._lock:
            return StreamStats(
                chunks_seen=self._stats.chunks_seen,
                malformed_chunks=self._stats.malformed_chunks,
                deliveries=self._stats.deliveries,
                chars=self._stats.chars,
                first_token_s=self._stats.first_token_s,
                total_s=self._stats.total_s or 0.0,
                window_ms=self._stats.window_ms,
                truncated=self._stats.truncated,
                tool_calls=self._stats.tool_calls,
                finish_reason=self._stats.finish_reason,
            )

    @property
    def finish_reason(self) -> str:
        """The provider's stop reason, or "" if it never sent one."""
        with self._lock:
            return self._stats.finish_reason

    @property
    def usage(self) -> Optional[Dict[str, Any]]:
        """The provider's usage frame, or ``None`` when none was sent."""
        with self._lock:
            return dict(self._usage) if self._usage else None


def estimate_prompt_tokens(messages: Iterable[Any]) -> int:
    """Estimate prompt tokens for a streamed call that reported no usage.

    Same shape as the mock path in :func:`runtime.model_router.call_model`
    (10 tokens of framing plus a quarter of the characters), so a streamed
    call and a non-streamed one of the same request estimate alike. The
    caller records ``cost_source="estimate"`` so this is never reported as
    a provider number.
    """
    total = 10
    for message in messages or []:
        try:
            total += len(str(_get(message, "content", "") or "")) // 4
        except Exception:  # pragma: no cover - defensive
            continue
    return total


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    """Parse streamed tool-call arguments, degrading to ``{}``.

    A streamed argument fragment is frequently not valid JSON on its own
    and a *completed* stream can still be unparseable. The caller's
    schema validation is where that is reported to the model; this layer
    only guarantees the type.
    """
    import json

    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def iter_stream(chunks: Iterable[Any]) -> Iterable[Any]:
    """Yield provider chunks, skipping a non-iterable provider result.

    ``litellm.completion(stream=True)`` returns a generator, but a test
    double or an OpenAI-compatible gateway may return a list or a single
    response object. Treating the single object as a one-chunk stream is
    the honest degradation: ``streamed`` stays true, ``chunks_seen`` is 1.
    """
    if chunks is None:
        return []
    if isinstance(chunks, (list, tuple)):
        return list(chunks)
    choices = _get(chunks, "choices")
    if choices is not None and not hasattr(chunks, "__next__"):
        return [chunks]
    return chunks


def stream_call(
    completion: Callable[..., Any],
    kwargs: Dict[str, Any],
    *,
    on_delta: Optional[Callable[[str], None]] = None,
    window_ms: int = MIN_WINDOW_MS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Tuple[str, List[Dict[str, Any]], str, StreamStats, Optional[Dict[str, Any]]]:
    """Run one streamed provider call and return the assembled result.

    Returns ``(text, tool_calls, finish_reason, stats, usage)``. ``usage``
    is ``None`` when the provider volunteered none — the caller then
    estimates and labels the cost as an estimate. ``completion`` is called
    with ``stream=True``; a provider that raises is *not* retried here,
    because the caller's bounded-retry wrapper owns retry and a partially
    consumed stream must not be replayed as a second charge.
    """
    assembler = StreamAssembler(
        on_delta=on_delta, window_ms=window_ms, max_chars=max_chars
    )
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    assembler.begin()
    response = completion(**stream_kwargs)
    for chunk in iter_stream(response):
        assembler.feed(chunk)
    assembler.flush()
    return (
        assembler.text,
        assembler.tool_calls,
        assembler.finish_reason,
        assembler.stats,
        assembler.usage,
    )
