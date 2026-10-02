"""Provider-neutral model gateway with native and text tool-call support.

The gateway is the single place a model reply is turned into something the
kernel can act on. Its contract:

* ``tools`` is passed through to the provider request (never silently
  dropped, and never sent when the caller has no catalog);
* a provider-native tool call is normalized into a typed call, and a
  text-protocol reply is the documented fallback - which fallback was used
  is recorded explicitly on the response and in the trace;
* every returned call is JSON-safe, so persisting an assistant turn can never
  wedge the journal on reload;
* retried or duplicated provider events are deduplicated by event id, so a
  duplicated stream frame executes a tool exactly once;
* an incomplete or missing stop reason is inferred and recorded instead of
  the turn ending silently.
"""

from __future__ import annotations

import inspect
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from harness.tool_errors import ModelFailure, ModelRecovery, classify_model_failure

from .tools import dedupe_tool_calls, json_safe, parse_model_response

#: Inferred stop reasons for a turn that produced no usable content.
STOP_EMPTY = "empty_response"
STOP_TRUNCATED = "incomplete_truncated"
STOP_REFUSED = "incomplete_refusal"
STOP_PROVIDER = "provider"

#: The bounded model-call retry reads the SAME config keys the legacy step
#: path reads, so one knob governs both loops and a run stays reproducible
#: from its merged config. There is deliberately no second set of names.
RECOVERY_MAX_ATTEMPTS_KEY = "max_model_attempts"
RECOVERY_BASE_BACKOFF_KEY = "model_retry_base_s"
RECOVERY_CAP_BACKOFF_KEY = "model_retry_cap_s"

#: Label recorded on every ``model_recovery`` event the gateway itself emits.
#: A call-level retry and the strategy's turn-level one share one event name
#: and one vocabulary; the label is what tells them apart.
GATEWAY_RECOVERY_LABEL = "kernel-model-call"


class ModelGatewayError(RuntimeError):
    """Raised when a model request cannot be completed."""


@dataclass
class ModelResponse:
    """Normalized model output plus provider usage metadata."""

    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    elapsed_s: float = 0.0
    error: str = ""
    #: ``native`` | ``text`` | ``none`` - which tool protocol produced the
    #: calls. A text-fallback turn is never silently presented as native.
    tool_protocol: str = "none"
    stop_reason: str = ""
    #: Protocol-level problems (malformed JSON, unknown tool, bad schema)
    #: kept separate from task failures so a run can distinguish them.
    protocol_errors: List[Dict[str, Any]] = field(default_factory=list)
    #: Provider event ids dropped as duplicates.
    duplicate_events: int = 0
    #: The classified failure behind ``error``, in ``harness.tool_errors``'s
    #: own ``ModelFailure`` vocabulary (``kind`` / ``detail`` / ``status_code``
    #: / ``retryable`` / ``terminal`` / ``backoff_s``) as a plain mapping so a
    #: response stays JSON-serializable. ``None`` on a success.
    model_failure: Optional[Dict[str, Any]] = None

    @property
    def failed(self) -> bool:
        """Return whether the provider request failed."""
        return bool(self.error)

    def to_dict(self) -> Dict[str, Any]:
        """Return a serializable response summary."""
        return {
            "text": self.text,
            "tool_calls": list(self.tool_calls),
            "usage": dict(self.usage),
            "elapsed_s": self.elapsed_s,
            "error": self.error,
            "tool_protocol": self.tool_protocol,
            "stop_reason": self.stop_reason,
            "protocol_errors": list(self.protocol_errors),
            "duplicate_events": self.duplicate_events,
            "model_failure": dict(self.model_failure) if self.model_failure else None,
        }


class ModelGateway:
    """Call a model through the existing boundary and normalize its output."""

    def __init__(
        self,
        call_fn: Optional[Callable[..., Any]] = None,
        *,
        config: Optional[Mapping[str, Any]] = None,
        model_client: Any = None,
        trace: Any = None,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        recovery: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.config = dict(config or {})
        self._call_fn = call_fn
        self._last_call_fn: Optional[Callable[..., Any]] = None
        self.model_client = model_client
        self.trace = trace
        self.tool_schemas = list(tool_schemas or [])
        self.calls: List[Dict[str, Any]] = []
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        # Bounded model-call retry. One instance per gateway so its statistics
        # span the run; a caller may inject its own (the daily strategy does,
        # to write the `model_recovery` events into the run's event journal).
        self._recovery = recovery
        self._sleep = sleep
        # Provider attempts the CURRENT logical call cost. Reset per call, so a
        # call that recovered after two 502s reports 3, not a running total.
        self._last_call_attempts = 0
        # Provider event ids already converted into tool calls, so a retried
        # or duplicated stream frame never executes a tool twice.
        self._seen_event_ids: set[str] = set()
        # Index of the trace record awaiting its tool-protocol receipt.
        self._last_record: Optional[int] = None

    # -- bounded model-call recovery -------------------------------------
    #
    # The kernel's model-call path used to retry nothing while the legacy step
    # path retried three times: a transient 502 could end a healthy run. This
    # is the same `harness.tool_errors.ModelRecovery` the legacy path uses -
    # same slugs, same classes, same terminal-vs-retryable split - so the two
    # paths cannot drift into different retry vocabularies.

    def bind_recovery(self, recovery: Any) -> "ModelGateway":
        """Attach this run's bounded model-call recovery and return ``self``.

        Assumes ``recovery`` is a ``harness.tool_errors.ModelRecovery`` (or
        anything with the same ``call(fn, *, step)`` / ``report()`` shape). A
        gateway with a bound recovery never builds its own, so an injected
        policy is never silently replaced.
        """
        self._recovery = recovery
        return self

    @property
    def has_bound_recovery(self) -> bool:
        """Whether a recovery was bound explicitly (builds nothing)."""
        return self._recovery is not None

    def _effective_recovery(self) -> Any:
        """Return the bound recovery, else one built from this run's config.

        A gateway constructed with no recovery still gets the bounded retry,
        because the retry budget is a property of the RUN (its config), not of
        the caller. ``max_model_attempts: 1`` is the honest way to turn it off.
        """
        if self._recovery is None:
            self._recovery = ModelRecovery(
                trace=self.trace,
                max_attempts=self._recovery_attempts(),
                base_backoff_s=self._recovery_backoff("base"),
                cap_backoff_s=self._recovery_backoff("cap"),
                sleep=self._sleep or time.sleep,
                label=GATEWAY_RECOVERY_LABEL,
            )
        return self._recovery

    def _recovery_attempts(self) -> int:
        """The configured per-call attempt budget (>= 1; 1 disables retry)."""
        try:
            return max(1, int(self.config.get(RECOVERY_MAX_ATTEMPTS_KEY, 3) or 1))
        except (TypeError, ValueError):
            return 3

    def _recovery_backoff(self, which: str) -> float:
        """The configured bounded-backoff base or cap, in seconds."""
        key = RECOVERY_BASE_BACKOFF_KEY if which == "base" else RECOVERY_CAP_BACKOFF_KEY
        default = 0.5 if which == "base" else 8.0
        try:
            return max(0.0, float(self.config.get(key, default) or 0.0))
        except (TypeError, ValueError):
            return default

    def recovery_report(self) -> Dict[str, Any]:
        """Return machine-readable counters for this gateway's model recovery.

        Zeros when no recovery has been resolved yet, so a caller can report a
        run's recovery honestly without having to know whether the loop ever
        reached the model boundary.
        """
        recovery = self._recovery
        if recovery is None:
            return {
                "attempts": 0,
                "recoveries": 0,
                "max_attempts": self._recovery_attempts(),
                "by_kind": {},
                "last_kind": None,
                "label": GATEWAY_RECOVERY_LABEL,
            }
        report = dict(recovery.report())
        report.setdefault("label", str(getattr(recovery, "label", "") or ""))
        return report

    def note_provider_event(self, event_id: str) -> bool:
        """Return whether a provider stream event id is new, marking it seen."""
        key = str(event_id or "").strip()
        if not key:
            return True
        if key in self._seen_event_ids:
            return False
        self._seen_event_ids.add(key)
        return True

    def call(
        self,
        messages: List[Dict[str, str]],
        *,
        step: str = "agent-turn",
        difficulty_hint: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ModelResponse:
        """Request one model response and normalize native/text formats.

        A retryable provider failure is retried on the SAME request with the
        run's bounded, deterministic backoff (see :meth:`_invoke_with_recovery`);
        a terminal one, and a harness-side exception, fail immediately. Either
        way the failure is returned as a ``ModelResponse`` with ``error`` set
        and ``model_failure`` carrying the classified kind, so the caller never
        has to re-parse an exception message.
        """
        started = time.time()
        effective_tools = list(tools) if tools is not None else list(self.tool_schemas)
        self._last_call_attempts = 0
        try:
            raw = self._invoke_with_recovery(
                messages,
                step=step,
                difficulty_hint=difficulty_hint,
                tools=effective_tools or None,
            )
        except (KeyboardInterrupt, SystemExit):
            # A cancel is not a provider fault and must never be retried or
            # reclassified; it leaves the kernel exactly as it was.
            raise
        except Exception as exc:
            response = ModelResponse(
                error=f"model failure: {exc}",
                stop_reason=STOP_PROVIDER,
                tool_protocol="none",
                model_failure=self._classify(exc),
            )
            self._record(response, step, effective_tools)
            return response
        response = self._normalize(raw)
        if not response.usage and self._last_call_fn is not None:
            response.usage = self._read_usage(self._last_call_fn)
        if response.usage:
            response.usage.setdefault("cost", response.usage.get("cost_usd", 0.0))
            response.usage.setdefault("cost_usd", response.usage.get("cost", 0.0))
        response.elapsed_s = round(time.time() - started, 3)
        self._record(response, step, effective_tools)
        return response

    complete = call

    @property
    def call_fn(self) -> Optional[Callable[..., Any]]:
        """Return the injected model boundary, or ``None`` for the default.

        A second gateway may be needed for the same run - a summarizer pinned to
        a cheaper model, for example - and it must reach the SAME boundary the
        run already resolved, or a run that injected a model (tests, the
        scripted offline path) would silently dial the real provider instead.
        """
        return self._call_fn or self._last_call_fn

    def parse(
        self, response: ModelResponse, known_tools: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Parse a normalized response into raw typed tool-call mappings.

        A native call is returned as-is. A reply with no native call falls
        back to the text protocol, and the fallback is marked on the response
        so the trace says which protocol actually ran. Malformed payloads are
        returned as explicit markers, never raised: the caller turns them into
        model-facing feedback instead of ending the session.
        """
        if response.failed:
            return [{"malformed": response.error, "recovery": "retry_model"}]
        if response.tool_calls:
            response.tool_protocol = response.tool_protocol or "native"
            calls = dedupe_tool_calls(
                [dict(item) for item in response.tool_calls],
                seen_event_ids=self._seen_event_ids,
            )
            self._note_protocol(response, "native", len(calls))
            return calls
        if not str(response.text or "").strip():
            return [
                {"malformed": "empty model response", "recovery": "emit_one_tool_call"}
            ]
        response.tool_protocol = "text"
        calls = dedupe_tool_calls(
            parse_model_response(response.text, known_tools),
            seen_event_ids=self._seen_event_ids,
        )
        response.protocol_errors = [
            {"reason": item["malformed"], "recovery": item.get("recovery", "")}
            for item in calls
            if "malformed" in item
        ]
        self._note_protocol(response, "text", len(calls))
        return calls

    def _note_protocol(
        self, response: ModelResponse, protocol: str, count: int
    ) -> None:
        """Record which protocol actually produced this turn's calls.

        A text-protocol reply must be traceable as a fallback: the recorded
        receipt is updated in place after parsing so the trace never claims a
        native call the provider never made.
        """
        index = self._last_record
        if index is None or index >= len(self.calls):
            return
        record = self.calls[index]
        record["tool_protocol"] = protocol
        record["tool_call_count"] = count
        record["protocol_errors"] = len(response.protocol_errors)
        if record.get("step") == response.usage.get("step"):
            return
        record["step"] = record.get("step", "")

    def snapshot_usage(self) -> Dict[str, Any]:
        """Return cumulative spend and token usage."""
        return {
            "cost_usd": round(self.total_cost_usd, 6),
            "tokens": self.total_tokens,
            "calls": len(self.calls),
        }

    def _attempts_for(self, response: ModelResponse) -> int:
        """Return how many provider attempts this call cost (>= 1).

        A retried call is ONE logical call that cost several attempts; the
        receipt says so instead of reporting one and losing the retries. The
        counter is per logical call, so a recovered call reports its retries and
        the next clean call reports 1.
        """
        return max(1, int(self._last_call_attempts or 1))

    def _invoke(
        self,
        messages: List[Dict[str, str]],
        *,
        step: str,
        difficulty_hint: Optional[str],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        if self.model_client is not None:
            kwargs: Dict[str, Any] = {"step": step, "difficulty_hint": difficulty_hint}
            if tools:
                kwargs["tools"] = list(tools)
            return self._invoke_callable(self.model_client.call, messages, kwargs)
        fn = self._call_fn
        if fn is None:
            from harness.deps import get_call_model

            fn = get_call_model()
        self._last_call_fn = fn
        kwargs = {
            "difficulty_hint": difficulty_hint,
            "provider": self.config.get("provider"),
            "model": self.config.get("model"),
            "api_key": self.config.get("api_key"),
            # The step label is forwarded so a boundary that accepts it can tell
            # an ordinary turn from a maintenance call (a context summarizer).
            # `_invoke_callable` drops it for every boundary that does not
            # declare it, so the real router is unaffected.
            "step": step,
        }
        selected_tools = tools if tools is not None else self.tool_schemas
        if selected_tools:
            kwargs["tools"] = list(selected_tools)
        return self._invoke_callable(fn, messages, kwargs)

    def _invoke_with_recovery(
        self,
        messages: List[Dict[str, str]],
        *,
        step: str,
        difficulty_hint: Optional[str],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """Invoke the boundary under the run's bounded retry, on the SAME call.

        A model request is read-only with respect to the workspace, so
        repeating it is safe. `harness.tool_errors.classify_model_failure` - the
        ONE classifier, the one the legacy step path uses - decides what may be
        repeated: a provider fault (``model_rate_limited``, ``model_unavailable``,
        ``model_timeout``) is retried with deterministic bounded backoff, and a
        TERMINAL class is not.

        A non-provider exception is never a provider fault. A ``TypeError`` in
        our own code classifies as ``model_internal`` and is raised on the first
        attempt however provider-flavoured its message is: retrying our own bug
        three times is noise, and labelling it a timeout would blame the
        provider for a coding error on this side of the boundary.
        """
        attempts = 0

        def once() -> Any:
            nonlocal attempts
            attempts += 1
            return self._invoke(
                messages, step=step, difficulty_hint=difficulty_hint, tools=tools
            )

        recovery = self._effective_recovery()
        try:
            if int(getattr(recovery, "max_attempts", 1) or 1) <= 1:
                return once()
            # `ModelRecovery.call` re-raises the last exception once the class is
            # terminal or the budget is spent, so the caller's `except` is the
            # single place a failure becomes a `ModelResponse` - one path.
            return recovery.call(once, step=step)
        finally:
            self._last_call_attempts = max(1, attempts)

    def _classify(self, exc: BaseException) -> Dict[str, Any]:
        """Classify a failed call into the serialized ``ModelFailure`` shape.

        Uses the same function the retry itself used, with the attempt number
        the call actually reached, so the recorded backoff is the one that was
        applied rather than a recomputed guess. Never raises.
        """
        try:
            failure: ModelFailure = classify_model_failure(
                exc,
                attempt=max(1, int(self._last_call_attempts)),
                base_backoff_s=self._recovery_backoff("base"),
                cap_backoff_s=self._recovery_backoff("cap"),
            )
        except Exception:  # pragma: no cover - the classifier is never-raising
            return {
                "kind": "model_internal",
                "detail": type(exc).__name__,
                "retryable": False,
                "terminal": True,
                "status_code": None,
                "backoff_s": 0.0,
            }
        return dict(failure._asdict())

    @staticmethod
    def _invoke_callable(
        fn: Callable[..., Any], messages: List[Dict[str, str]], kwargs: Dict[str, Any]
    ) -> Any:
        """Call a boundary with only the parameters its signature accepts.

        A boundary that predates the tool protocol - the stub router, a test
        double, or an older adapter - keeps working because an unaccepted
        ``tools`` keyword is dropped rather than raising.
        """
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(messages, **kwargs)
        parameters = signature.parameters.values()
        if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
            return fn(messages, **kwargs)
        accepted = {item.name for item in parameters}
        return fn(
            messages, **{key: value for key, value in kwargs.items() if key in accepted}
        )

    @classmethod
    def _normalize(cls, raw: Any) -> ModelResponse:
        """Normalize any provider shape into one validated response.

        Accepts the router's native mapping, an OpenAI-shaped ``choices``
        list, a bare tool-call list, or plain text. Tool-call arguments are
        JSON-projected so the journal can never receive an unserializable
        value, and a payload whose calls are all malformed becomes a protocol
        error rather than an empty, silently-ignored turn.
        """
        usage: Dict[str, Any] = {}
        text = ""
        raw_calls: Any = None
        finish_reason: Any = None
        if isinstance(raw, Mapping):
            text = str(raw.get("content", raw.get("text", "")) or "")
            raw_calls = raw.get("tool_calls")
            usage = dict(raw.get("usage") or {})
            finish_reason = raw.get("finish_reason")
            choices = raw.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], Mapping) else {}
                message = (
                    choice.get("message")
                    if isinstance(choice.get("message"), Mapping)
                    else {}
                )
                if message:
                    text = str(message.get("content", text) or "")
                    raw_calls = message.get("tool_calls", raw_calls)
                finish_reason = choice.get("finish_reason", finish_reason)
            if raw_calls is None and (raw.get("tool") or raw.get("name")):
                raw_calls = [raw]
        elif isinstance(raw, list):
            raw_calls = raw
        elif raw is not None and not isinstance(raw, str):
            # A litellm response object (or any other attribute carrier).
            text = str(getattr(raw, "content", "") or "")
            raw_calls = getattr(raw, "tool_calls", None)
            usage = (
                dict(getattr(raw, "usage", None) or {})
                if isinstance(getattr(raw, "usage", None), Mapping)
                else {}
            )
            choices = getattr(raw, "choices", None) or []
            if choices:
                choice = choices[0]
                message = getattr(choice, "message", None)
                text = str(getattr(message, "content", text) or "")
                raw_calls = getattr(message, "tool_calls", None) or raw_calls
                finish_reason = getattr(choice, "finish_reason", None)
        else:
            text = str(raw or "")
        protocol_errors: List[Dict[str, Any]] = []
        if raw_calls is not None:
            parsed = parse_model_response({"tool_calls": raw_calls})
            tool_calls = [json_safe(item) for item in parsed if "malformed" not in item]
            malformed = [item for item in parsed if "malformed" in item]
            protocol_errors = [
                {"reason": str(item["malformed"]), "recovery": item.get("recovery", "")}
                for item in malformed
            ]
            if malformed and not tool_calls:
                return ModelResponse(
                    text=text,
                    tool_calls=[],
                    usage=usage,
                    raw=json_safe(raw),
                    error=str(malformed[0]["malformed"]),
                    tool_protocol="native",
                    protocol_errors=protocol_errors,
                    stop_reason=cls._infer_stop_reason(
                        finish_reason, text, bool(malformed)
                    ),
                )
        else:
            tool_calls = []
        stop_reason = cls._infer_stop_reason(finish_reason, text, bool(protocol_errors))
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            raw=raw if isinstance(raw, (str, type(None))) else json_safe(raw),
            tool_protocol="native" if tool_calls else "none",
            stop_reason=stop_reason,
            protocol_errors=protocol_errors,
        )

    @staticmethod
    def _infer_stop_reason(
        finish_reason: Any, text: str, had_protocol_errors: bool
    ) -> str:
        """Infer and record why a turn ended, instead of ending silently."""
        reason = str(finish_reason or "").strip().lower()
        if reason in {"length", "max_tokens", "incomplete"}:
            return STOP_TRUNCATED
        if reason in {"content_filter", "refusal", "safety"}:
            return STOP_REFUSED
        if reason:
            return reason
        if had_protocol_errors:
            return "protocol_error"
        return "" if str(text or "").strip() else STOP_EMPTY

    @staticmethod
    def _read_usage(call_fn: Callable[..., Any]) -> Dict[str, Any]:
        """Read usage from a callable or its defining router module."""
        readers = []
        reader = getattr(call_fn, "get_last_usage", None)
        if callable(reader):
            readers.append(reader)
        module = sys.modules.get(getattr(call_fn, "__module__", ""))
        module_reader = getattr(module, "get_last_usage", None)
        if callable(module_reader):
            readers.append(module_reader)
        for usage_reader in readers:
            try:
                usage = usage_reader()
            except Exception:
                continue
            if isinstance(usage, Mapping) and usage:
                return dict(usage)
        return {}

    def _record(
        self,
        response: ModelResponse,
        step: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        cost = float(
            response.usage.get("cost_usd", response.usage.get("cost", 0.0)) or 0.0
        )
        tokens = int(
            response.usage.get("tokens", response.usage.get("total_tokens", 0)) or 0
        )
        self.total_cost_usd += cost
        self.total_tokens += tokens
        record = {
            "step": step,
            "tokens": tokens,
            "cost": cost,
            "elapsed_s": response.elapsed_s,
            "error": response.error,
            # Explicit tool-protocol receipt: a text-fallback turn is never
            # recorded as a native one, and the schema actually sent is
            # auditable from the trace alone.
            "tool_protocol": response.tool_protocol,
            "tools_sent": len(tools or []),
            "tool_call_count": len(response.tool_calls),
            "stop_reason": response.stop_reason,
            "protocol_errors": len(response.protocol_errors),
            # How many provider attempts this call actually cost, so a run's
            # ledger shows the retries rather than hiding them behind one row.
            "attempts": self._attempts_for(response),
            "model_failure_kind": str((response.model_failure or {}).get("kind") or ""),
        }
        self.calls.append(record)
        self._last_record = len(self.calls) - 1
        if self.trace is not None:
            payload = {"step": step, "content": response.text, "usage": record}
            if response.tool_calls:
                payload["tool_calls"] = list(response.tool_calls)
            if response.stop_reason:
                payload["stop_reason"] = response.stop_reason
            if response.protocol_errors:
                payload["protocol_errors"] = list(response.protocol_errors)
            try:
                self.trace.log("model_response", payload)
            except Exception:
                pass
