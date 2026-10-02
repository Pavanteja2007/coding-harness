"""Thin wrapper around the model boundary (call_model) that:
- logs every prompt/response pair to the trace,
- snapshots usage (tokens/cost) right after each call for budget checks,
- accumulates the TaskResult.model_calls records.

The actual provider call is harness.deps.get_call_model() — real router
(Terminal 3) or stub, injected or not, is decided there.
"""

import inspect
import time
from typing import Any, Callable, Dict, List, Optional

from harness.trace import TraceLogger

#: Coalescing window for provider deltas (ms). Matches
#: ``runtime.streaming.MIN_WINDOW_MS``; duplicated as a literal so this
#: module does not import the runtime package on a path that only needs the
#: harness (the harness must stay importable with no provider present).
DEFAULT_STREAM_WINDOW_MS = 40


class ModelClient:
    """One per task run; wraps the boundary call_model with logging and
    usage accounting. Assumes TraceLogger writes to logs/{task_id}/.

    ``stream=True`` (VEX-CEILING-10) turns the boundary into a streaming
    call and emits one ``model_delta`` journal row per coalesced window.
    That row is the *only* live-text channel the projection has, so the TUI
    stays a consumer of the event journal: a slow call is visible as
    "waiting for first token" and then as growing text, rather than as a
    silent spinner. The assembled response is byte-identical to the
    non-streamed one, so a run with ``stream=True`` produces the same
    transcript content and the same verification contract.
    """

    def __init__(
        self,
        trace: TraceLogger,
        config: Dict[str, Any],
        stream: Optional[bool] = None,
    ) -> None:
        self.trace = trace
        self.config = config
        self.model_calls: List[Dict[str, Any]] = []
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self._step_counter: int = 0
        # Streaming is opt-in and config-driven (``stream_enabled``,
        # default True) so the OFF arm is a config key rather than a second
        # code path, and an ablation can diff the two.
        if stream is None:
            stream = bool(config.get("stream_enabled", True))
        self.stream = bool(stream)
        self.stream_window_ms: int = int(
            config.get("stream_window_ms", DEFAULT_STREAM_WINDOW_MS)
            or DEFAULT_STREAM_WINDOW_MS
        )
        self.stream_deltas: int = 0
        self.stream_chars: int = 0
        self.first_token_s: Optional[float] = None

    def _boundary_streams(self, fn: Callable[..., Any]) -> bool:
        """Whether this boundary EXPLICITLY declares the streaming contract.

        Presence of the keywords is not enough. A boundary that accepts
        ``**kwargs`` and forwards them (a scripted test double, an
        adapter around a pre-streaming callable) would accept
        ``stream=True`` and then hand it to something that rejects it,
        turning a liveness feature into a run-killing ``TypeError``. So
        streaming requires both ``stream`` and ``on_delta`` to be named
        in the signature; anything else degrades to the historical
        single call and is recorded as ``streamed: false``.

        The real boundary, ``runtime.model_router.call_model``, declares
        both, so the production path streams. A boundary that could be
        extended to stream only needs to name them.
        """
        try:
            parameters = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False
        return "stream" in parameters and "on_delta" in parameters

    def _boundary_names(self, fn: Callable[..., Any], name: str) -> bool:
        """Whether this boundary EXPLICITLY declares ``name`` in its signature.

        Presence of ``**kwargs`` is NOT evidence. A scripted double, an older
        stub, or an adapter that forwards keywords would accept ``effort`` and
        then hand it to something that rejects it — which is exactly the
        failure ``_boundary_streams`` was written to prevent for ``stream``,
        and it is a run-killing ``TypeError`` rather than a degraded feature.
        So a keyword is forwarded only when the boundary NAMES it. The real
        boundary, ``runtime.model_router.call_model``, names both, so the
        production path is unaffected.
        """
        try:
            parameters = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False
        return name in parameters

    def call(
        self,
        messages: List[Dict[str, str]],
        step: str,
        difficulty_hint: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stream: Optional[bool] = None,
        effort: Optional[str] = None,
    ) -> Any:
        """Send one chat request and return text or native provider data.

        Existing callers receive the historical string unchanged. A router
        that returns a provider-native mapping is also accepted so the
        kernel gateway can consume structured tool calls without a second
        model request. ``tools`` is forwarded to the boundary only when the
        caller supplies it, so a call without tools is byte-identical to the
        pre-tool-protocol request.

        ``stream`` defaults to this client's configured value, and is
        further gated on the boundary declaring the streaming contract
        (see :meth:`_boundary_streams`). When streaming happens, one
        ``model_delta`` row is written per coalesced provider window; the
        row is additive and safe for a consumer that does not know about
        it.

        ``effort`` (AGT-08) is forwarded on the SAME terms as ``stream``: only
        when the boundary NAMES the parameter (see :meth:`_boundary_names`), so
        an older stub or a scripted double keeps working byte-identically. The
        per-call record always carries ``effort`` - the level that was in force
        - plus whatever the router reported about it, so a `--json` consumer can
        explain a cost claim from the run's own records. Effort selects a model
        parameter only; it is never consulted by a verification or completion
        decision.
        """
        fn: Callable[..., str] = self._get_fn()
        self._step_counter += 1
        self.trace.log("model_request", {"step": step, "messages": messages})
        started = time.time()
        self._call_started = started
        kwargs: Dict[str, Any] = {
            "difficulty_hint": difficulty_hint,
            "provider": self.config.get("provider"),
            "model": self.config.get("model"),
            "api_key": self.config.get("api_key"),
        }
        if tools:
            kwargs["tools"] = list(tools)
        # A mid-run `/effort` writes into the run's config, so an explicit
        # argument beats the config and the config beats the client's own
        # default. `None` means "whatever the config says", which is why the
        # key is only added when there is something to add AND the boundary
        # can accept it.
        if effort is None:
            effort = self.config.get("effort")
        if effort is not None and self._boundary_names(fn, "effort"):
            kwargs["effort"] = effort
        wants_stream = self.stream if stream is None else bool(stream)
        on_delta = (
            self._delta_sink(step)
            if wants_stream and self._boundary_streams(fn)
            else None
        )
        if on_delta is not None:
            kwargs["stream"] = True
            kwargs["on_delta"] = on_delta
        response = self._invoke(fn, messages, kwargs)
        elapsed = time.time() - started

        usage = self._read_usage(fn)
        tool_calls = self._native_tool_calls(response)
        record = {
            "step": step,
            "model": usage.get("model", self.config.get("model")),
            "provider": usage.get("provider", self.config.get("provider")),
            "tokens": usage.get("tokens", 0),
            "cost": usage.get("cost_usd", 0.0),
            "elapsed_s": round(elapsed, 2),
            "call_index": self._step_counter,
            "streamed": bool(on_delta is not None and self.stream_deltas),
            # AGT-08: the effort level in force for THIS call, plus whatever
            # the router reported about it. Both are recorded even when the
            # boundary is a stub that knows nothing about effort, because
            # "asked for high" and "sent high" are different claims.
            "effort": str(usage.get("effort") or effort or "auto"),
            "effort_status": str(usage.get("effort_status") or ""),
            "effort_sent": bool(usage.get("effort_sent")),
            "effort_parameter": str(usage.get("effort_parameter") or ""),
        }
        if tool_calls:
            record["tool_calls"] = len(tool_calls)
            record["tool_protocol"] = "native"
        elif tools:
            record["tool_protocol"] = "text"
        self.model_calls.append(record)
        self.total_cost_usd += float(record["cost"] or 0.0)
        self.total_tokens += int(record["tokens"] or 0)
        self.trace.log(
            "model_response",
            {
                "step": step,
                "content": response,
                "usage": record,
            },
        )
        return response

    def _delta_sink(self, step: str) -> Callable[[str], None]:
        """Build the per-call ``model_delta`` writer.

        Each row carries the window index and character count, so a
        consumer that coalesces again (the frame path) can tell a
        one-token-per-frame stream from a one-window-per-response one
        without counting rows.
        """

        state = {"window": 0, "first": True}

        def emit(delta: str) -> None:
            if not delta:
                return
            state["window"] += 1
            self.stream_deltas += 1
            self.stream_chars += len(delta)
            if state["first"]:
                state["first"] = False
                self.first_token_s = round(time.time() - self._last_started(), 4)
            try:
                self.trace.log(
                    "model_delta",
                    {
                        "step": step,
                        "delta": delta,
                        "chars": len(delta),
                        "window": state["window"],
                    },
                )
            except Exception:
                # A journal write failure must never abort a live model
                # call; the assembled response still lands below.
                pass

        return emit

    def _last_started(self) -> float:
        """The start time of the call currently in flight."""
        return getattr(self, "_call_started", time.time())

    @staticmethod
    def _invoke(
        fn: Callable[..., Any],
        messages: List[Dict[str, str]],
        kwargs: Dict[str, Any],
    ) -> Any:
        """Call the boundary with the arguments its signature accepts.

        A boundary that predates the tool protocol (or a test double) keeps
        working: only the parameters it declares are passed.
        """
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(messages, **kwargs)
        parameters = signature.parameters
        if any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        ):
            return fn(messages, **kwargs)
        accepted = set(parameters)
        return fn(messages, **{k: v for k, v in kwargs.items() if k in accepted})

    @staticmethod
    def _native_tool_calls(response: Any) -> List[Dict[str, Any]]:
        """Return the provider-native tool calls carried by a response."""
        if not isinstance(response, dict):
            return []
        calls = response.get("tool_calls")
        return (
            [dict(item) for item in calls if isinstance(item, dict)]
            if isinstance(calls, list)
            else []
        )

    def call_structured(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        step: str = "agent-turn",
        difficulty_hint: Optional[str] = None,
        stream: Optional[bool] = None,
        effort: Optional[str] = None,
    ) -> Any:
        """Request a response with native tools when the provider supports them.

        The response is passed back untouched - a native provider payload or
        the historical text - so the kernel gateway can normalize it once,
        with the text-protocol fallback applied only when no native call was
        returned. Streaming rides the same path, so a streamed native tool
        call is assembled before the gateway ever sees it.
        """
        return self.call(
            messages,
            step=step,
            difficulty_hint=difficulty_hint,
            tools=tools,
            stream=stream,
            effort=effort,
        )

    def _get_fn(self) -> Callable[..., Any]:
        from harness.deps import get_call_model

        return get_call_model()

    def _read_usage(self, fn: Callable[..., str]) -> Dict[str, Any]:
        """Pull usage from whatever module backs call_model. Convention: a
        model module may expose get_last_usage() (the stub does). Falls
        back to zeros so budget accounting never crashes the run."""
        try:
            getter = getattr(fn, "get_last_usage", None)
            if getter is None:
                mod = __import__(fn.__module__, fromlist=["get_last_usage"])
                getter = getattr(mod, "get_last_usage", None)
            if callable(getter):
                return dict(getter() or {})
        except Exception:
            pass
        return {}

    def snapshot_usage(self) -> Dict[str, Any]:
        """Current cumulative usage for budget/stop checks."""
        return {
            "cost_usd": round(self.total_cost_usd, 6),
            "tokens": self.total_tokens,
            "calls": len(self.model_calls),
        }
