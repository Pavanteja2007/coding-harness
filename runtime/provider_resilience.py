"""Provider resilience: per-provider circuit breakers, bounded fallback,
and idempotency-aware retry.

Three failure modes kill an otherwise recoverable task when nothing handles
them, and this module exists to handle all three explicitly:

1. **A provider is down.** A transient 5xx / connection error is retried a
   couple of times and then the exception propagates — a planner call dies,
   the task dies, and a run that a different healthy provider could have
   completed is lost. A *circuit breaker* makes the failure a decision
   (``open`` -> stop dialing this endpoint) instead of an exception, and a
   *bounded fallback chain* turns it into a different target.
2. **The same provider keeps being dialed.** Without a breaker, N concurrent
   tasks each burn their own retry budget against a dead endpoint. The
   breaker is process-global (per provider identity) with a half-open probe,
   so the first task to recover pays the probe and the rest ride the
   closed state.
3. **A non-idempotent request is replayed.** Replaying a chat completion is
   safe; replaying an operation that creates provider-side state (a batch
   job, a fine-tune, an upload) is not. :func:`classify_retry` is the single
   classifier, and :func:`should_retry` refuses a replay of a caller-declared
   non-idempotent operation on the SAME endpoint *and* on the fallback chain.

Honest limits encoded here:

- The breaker state is per-process. A worker subprocess (runtime/worker.py)
  gets its own registry, so a 50-way fan-out does not share one breaker's
  state; the *scheduler* process is the natural place for a cross-process
  breaker and does not own provider calls. This is stated rather than
  pretended away, and :func:`snapshot` makes the per-process view explicit.
- The breaker never decides that a *task* should fail. It only decides
  whether to dial a target; the router's fallback chain owns the outcome.
- ``half_open`` permits exactly one probe. A second concurrent caller is
  refused until the probe resolves, which is the point of the state: a
  half-open breaker admitting 50 probes is just an open breaker.

Nothing here opens a network connection or imports a provider SDK.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from runtime import budget_governor

__all__ = [
    "CIRCUIT_OPEN_REASON",
    "CLOSED",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_MAX_FALLBACKS",
    "DEFAULT_RESET_SECONDS",
    "HALF_OPEN",
    "OPEN",
    "PROBE_INFLIGHT_REASON",
    "QUOTA_MARKERS",
    "BreakerRegistry",
    "BreakerSnapshot",
    "CircuitBreaker",
    "ProviderAttempt",
    "RetryDecision",
    "bound_fallbacks",
    "classify_retry",
    "default_registry",
    "field_map",
    "provider_identity",
    "re_split_reason",
    "reset_registry",
    "should_retry",
    "snapshot",
]

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: Stable reason slug returned by :meth:`CircuitBreaker.allow` when the
#: breaker is open. Named here so a caller never has to spell the literal.
CIRCUIT_OPEN_REASON = "circuit_open"
PROBE_INFLIGHT_REASON = "probe_inflight"

#: Failure threshold before a provider stops being dialed. One is the
#: default because the classifier below only counts *provider* failures
#: (connection/5xx/timeout/gateway flake) — a 4xx from a bad request is not
#: an outage and must not take the endpoint out of rotation.
DEFAULT_FAILURE_THRESHOLD = 2

#: How long an open breaker refuses calls before a single probe is admitted.
DEFAULT_RESET_SECONDS = 30.0

#: Ceiling on the number of providers one logical call may try. The
#: fallback chain is deliberately bounded: an unbounded chain turns one
#: outage into a request storm.
DEFAULT_MAX_FALLBACKS = 3


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def provider_identity(
    provider: Optional[str], api_base: Optional[str], model: Optional[str] = None
) -> str:
    """Return a stable, non-secret identity for one provider target.

    The endpoint is stored as a SHA-256 fingerprint, never the URL: a base
    URL can embed credentials (``https://user:pass@host``) and this string
    ends up in ledgers, traces, and breaker snapshots. The model is part of
    the identity because "openai is down" and "this specific model on
    openai is not served" are different facts.
    """
    name = str(provider or "").strip().lower() or "default"
    endpoint = ""
    if api_base:
        endpoint = hashlib.sha256(str(api_base).encode("utf-8", "replace")).hexdigest()[
            :12
        ]
    model_name = str(model or "").strip()
    return f"{name}|{endpoint}|{model_name}" if model_name else f"{name}|{endpoint}"


@dataclass(frozen=True)
class BreakerSnapshot:
    """An immutable view of one breaker, safe to serialize."""

    identity: str
    state: str
    failures: int
    successes: int
    opened_at: Optional[float]
    cooldown_remaining_s: float
    last_error: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict. ``last_error`` is already redacted by
        the caller; this module never stores raw exception text."""
        return {
            "identity": self.identity,
            "state": self.state,
            "failures": self.failures,
            "successes": self.successes,
            "opened_at": self.opened_at,
            "cooldown_remaining_s": round(self.cooldown_remaining_s, 3),
            "last_error": self.last_error,
        }


class CircuitBreaker:
    """A single-provider breaker with a closed / open / half-open cycle.

    Assumes a single ``lock`` guards the instance; :class:`BreakerRegistry`
    supplies it. A caller must go through :meth:`allow` before dialing and
    must report the outcome with :meth:`record_success` / :meth:`record_failure`.
    """

    def __init__(
        self,
        identity: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_seconds: float = DEFAULT_RESET_SECONDS,
        now: Optional[Any] = None,
    ) -> None:
        self.identity = identity
        self.failure_threshold = max(
            1, _as_int(failure_threshold, DEFAULT_FAILURE_THRESHOLD)
        )
        self.reset_seconds = max(0.0, _as_float(reset_seconds, DEFAULT_RESET_SECONDS))
        self._now = now or time.monotonic
        self._state = CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: Optional[float] = None
        self._half_open_inflight = False
        self._last_error: Optional[str] = None

    # -- state ---------------------------------------------------------------

    def state(self) -> str:
        """Return the live state, applying the open -> half-open transition.

        The transition is computed on read rather than by a background timer:
        no thread is woken for it, and a process that never calls the
        provider again pays nothing.
        """
        if self._state == OPEN and self._cooldown_elapsed():
            self._state = HALF_OPEN
            self._half_open_inflight = False
        return self._state

    def _cooldown_elapsed(self) -> bool:
        if self._opened_at is None:
            return True
        return (self._now() - self._opened_at) >= self.reset_seconds

    def _cooldown_remaining(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_seconds - (self._now() - self._opened_at))

    def allow(self) -> Tuple[bool, str]:
        """Return ``(permitted, reason)`` for dialing this provider now.

        Reasons are stable slugs: ``allowed``, ``circuit_open``,
        ``probe_inflight``.
        """
        state = self.state()
        if state == CLOSED:
            return True, "allowed"
        if state == OPEN:
            return False, CIRCUIT_OPEN_REASON
        if self._half_open_inflight:
            return False, PROBE_INFLIGHT_REASON
        self._half_open_inflight = True
        return True, "probe_admitted"

    def release_probe(self) -> None:
        """Give back an un-admitted probe slot (the caller gave up early)."""
        self._half_open_inflight = False

    # -- outcomes ------------------------------------------------------------

    def record_success(self) -> None:
        """Close the breaker and reset the failure run."""
        self._state = CLOSED
        self._failures = 0
        self._successes += 1
        self._opened_at = None
        self._half_open_inflight = False
        self._last_error = None

    def record_failure(self, error: Optional[str] = None) -> None:
        """Count one provider failure, opening the breaker at the threshold.

        A half-open probe that fails re-opens immediately instead of waiting
        for another threshold hit: the endpoint already failed once, went
        open, and has now failed its recovery probe.
        """
        self._successes = 0
        self._last_error = error
        if self._state == HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = OPEN
        self._opened_at = self._now()
        self._half_open_inflight = False

    def snapshot(self) -> BreakerSnapshot:
        """Return a serializable view of the breaker's current state."""
        return BreakerSnapshot(
            identity=self.identity,
            state=self.state(),
            failures=self._failures,
            successes=self._successes,
            opened_at=self._opened_at,
            cooldown_remaining_s=self._cooldown_remaining(),
            last_error=self._last_error,
        )

    def reset(self) -> None:
        """Force the breaker closed (tests and operator recovery)."""
        self._state = CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at = None
        self._half_open_inflight = False
        self._last_error = None


class BreakerRegistry:
    """A process-global set of per-provider breakers, keyed by identity.

    One registry per process keeps the breaker state next to the provider
    calls it protects. It is safe under concurrency: a single re-entrant
    lock guards creation and every state transition, so 50 concurrent
    workers in one process cannot admit 50 half-open probes.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_seconds: float = DEFAULT_RESET_SECONDS,
        now: Optional[Any] = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._now = now or time.monotonic
        self._lock = threading.RLock()
        self._breakers: Dict[str, CircuitBreaker] = {}

    def get(self, identity: str) -> CircuitBreaker:
        """Return (creating if needed) the breaker for ``identity``."""
        with self._lock:
            breaker = self._breakers.get(identity)
            if breaker is None:
                breaker = CircuitBreaker(
                    identity,
                    failure_threshold=self.failure_threshold,
                    reset_seconds=self.reset_seconds,
                    now=self._now,
                )
                self._breakers[identity] = breaker
            return breaker

    def allow(self, identity: str) -> Tuple[bool, str]:
        """Return ``(permitted, reason)`` for one target identity."""
        with self._lock:
            return self.get(identity).allow()

    def record_success(self, identity: str) -> None:
        """Report a successful call for ``identity``."""
        with self._lock:
            self.get(identity).record_success()

    def record_failure(self, identity: str, error: Optional[str] = None) -> None:
        """Report a provider failure for ``identity``."""
        with self._lock:
            self.get(identity).record_failure(error)

    def release_probe(self, identity: str) -> None:
        """Return an un-admitted half-open probe slot."""
        with self._lock:
            breaker = self._breakers.get(identity)
            if breaker is not None:
                breaker.release_probe()

    def state(self, identity: str) -> str:
        """Return the live state slug for ``identity``."""
        with self._lock:
            return self.get(identity).state()

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return every breaker's state as a sorted list of dicts."""
        with self._lock:
            return [breaker.snapshot().as_dict() for breaker in self._breakers.values()]

    def reset(self) -> None:
        """Drop every breaker (tests, and an explicit operator recovery)."""
        with self._lock:
            self._breakers.clear()


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: Optional[BreakerRegistry] = None


def default_registry() -> BreakerRegistry:
    """Return the process-global breaker registry, creating it once.

    Overridable with ``NEO_CIRCUIT_BREAKER_THRESHOLD`` /
    ``NEO_CIRCUIT_BREAKER_RESET_S`` so an operator can widen the breaker
    for a known-flaky free-tier endpoint without a code change.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = BreakerRegistry(
                failure_threshold=_as_int(
                    os.environ.get("NEO_CIRCUIT_BREAKER_THRESHOLD"),
                    DEFAULT_FAILURE_THRESHOLD,
                ),
                reset_seconds=_as_float(
                    os.environ.get("NEO_CIRCUIT_BREAKER_RESET_S"),
                    DEFAULT_RESET_SECONDS,
                ),
            )
        return _REGISTRY


def reset_registry() -> None:
    """Forget the process-global registry entirely (test isolation)."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def snapshot() -> List[Dict[str, Any]]:
    """Return the process-global breaker snapshot (for status/JSON output)."""
    return default_registry().snapshot()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecision:
    """Why a failed provider call may (or may not) be replayed.

    ``retryable`` is the provider-transient question. ``replayable`` is the
    operation question and is only meaningful when ``retryable`` is true.
    They are separate because a non-idempotent operation should still be
    able to *fail fast* on a 5xx (no point sleeping) while never being
    replayed anywhere.
    """

    retryable: bool
    replayable: bool
    kind: str
    provider_fault: bool
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for the ledger/trace."""
        return {
            "retryable": self.retryable,
            "replayable": self.replayable,
            "kind": self.kind,
            "provider_fault": self.provider_fault,
            "reason": self.reason,
        }


_RATE_LIMIT_MARKERS = (
    "ratelimit",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "request limit",
)
# R2-14: quota markers used to live INSIDE _RATE_LIMIT_MARKERS, so
# `insufficient_quota` was served rate-limit recovery (backoff + retry) when
# the correct action is to stop and tell the human their credit is gone.
# They are a separate set now and `classify_retry` tests them FIRST. The
# tuples are re-exported from runtime.budget_governor so there is one
# vocabulary rather than two marker tables that can drift again.
QUOTA_MARKERS = budget_governor.QUOTA_MARKERS
_RATE_LIMIT_MARKERS = budget_governor.RATE_LIMIT_MARKERS
_TRANSIENT_MARKERS = (
    "internalservererror",
    "apiconnectionerror",
    "serviceunavailable",
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "overloaded",
    "bad gateway",
    "gateway timeout",
)
_AUTH_MARKERS = (
    "authenticationerror",
    "permissiondenied",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "401",
    "403",
)
_BAD_REQUEST_MARKERS = (
    "badrequesterror",
    "invalidrequesterror",
    "contentpolicyviolation",
    "400",
    "404",
    "422",
)
_SERVER_CODES = ("500", "502", "503", "504", "529")


def _chain_text(exc: BaseException, limit: int = 12) -> str:
    """Return the lowercased text of an exception chain, bounded.

    litellm wraps the real cause (``litellm.APIConnectionError`` around a
    ``httpx.ConnectError``), so the classification must walk the chain. The
    walk is bounded and identity-guarded: a self-referential ``__context__``
    is a real thing (re-raise inside ``except``) and an unbounded walk
    would hang the classifier.
    """
    parts: List[str] = []
    seen: set = set()
    current: Optional[BaseException] = exc
    while current is not None and len(parts) < limit and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__} {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(parts).lower()


def _looks_like(text: str, markers: Sequence[str]) -> bool:
    padded = f" {text} "
    return any(
        marker in padded or padded.lstrip().startswith(marker) for marker in markers
    )


def classify_retry(
    exc: BaseException,
    *,
    idempotent: bool = True,
    is_transient: Optional[bool] = None,
    is_rate_limit: Optional[bool] = None,
) -> RetryDecision:
    """Classify a provider failure into retry / replay semantics.

    ``is_transient`` / ``is_rate_limit`` let the router inject its own
    already-implemented classifiers (``_is_transient_error`` /
    ``_is_rate_limit_error``) so there is exactly ONE notion of "this looks
    like a 5xx" in the repository. When they are not supplied the
    heuristics here are used, which is what makes this module independently
    testable.

    ``idempotent=False`` means the caller declared the operation unsafe to
    replay (a provider-side state change, a batch job, an upload). Such an
    operation is never replayed on the same endpoint and never handed to a
    fallback provider; it fails immediately with the reason recorded.
    """
    text = _chain_text(exc)
    # R2-14: an exhausted quota is tested BEFORE anything else and is NOT a
    # provider fault. The endpoint is healthy and the account is empty:
    # failing over to a second target on the same billing account spends the
    # same (absent) money, and the only real recovery is a human adding
    # credit. `runtime.budget_governor.classify_provider_failure` is the
    # richer classifier; this is the same split at this module's own
    # altitude so the two cannot disagree about the ACTION.
    if _looks_like(text, QUOTA_MARKERS) or getattr(exc, "status_code", None) == 402:
        return RetryDecision(
            retryable=False,
            replayable=False,
            kind=budget_governor.QUOTA_EXHAUSTED,
            provider_fault=False,
            reason="provider quota/credit exhausted; a retry cannot create credit",
        )
    if is_rate_limit is None:
        is_rate_limit = _looks_like(text, _RATE_LIMIT_MARKERS)
    if is_transient is None:
        is_transient = _looks_like(text, _TRANSIENT_MARKERS) or _looks_like(
            text, _SERVER_CODES
        )
    # An empty-bodied 400 is the router's documented gateway flake, not a
    # real bad request: reuse that rule rather than growing a second one.
    reason = re_split_reason(exc)
    gateway_flake = "badrequesterror" in text and not reason
    is_auth = _looks_like(text, _AUTH_MARKERS)
    is_bad_request = _looks_like(text, _BAD_REQUEST_MARKERS) and not gateway_flake

    if is_auth:
        # Credentials are the operator's problem, not a transient blip, but
        # the endpoint may still be usable by a DIFFERENT provider — so this
        # is "provider fault, do not retry here, eligible for fallback".
        return RetryDecision(
            retryable=False,
            replayable=False,
            kind="auth",
            provider_fault=True,
            reason="provider authentication/authorization failed",
        )
    if is_bad_request and not gateway_flake:
        return RetryDecision(
            retryable=False,
            replayable=False,
            kind="bad_request",
            provider_fault=False,
            reason="request rejected as invalid; a retry would send the same request",
        )
    if is_rate_limit:
        return RetryDecision(
            retryable=True,
            replayable=bool(idempotent),
            kind="rate_limit",
            provider_fault=True,
            reason="provider rate limit" + ("" if idempotent else "; non-idempotent"),
        )
    if is_transient or gateway_flake:
        return RetryDecision(
            retryable=True,
            replayable=bool(idempotent),
            kind="transient",
            provider_fault=True,
            reason="transient provider failure"
            + ("" if idempotent else "; non-idempotent"),
        )
    return RetryDecision(
        retryable=False,
        replayable=False,
        kind="unknown",
        provider_fault=False,
        reason="unclassified provider error",
    )


def re_split_reason(exc: BaseException) -> str:
    """Return the message tail of the first chain link, split on ``:``/``-``.

    ``litellm`` formats exceptions as ``"ClassName: message"``; a gateway
    flake carries nothing after the separator. The router already relies on
    that rule, so it lives here next to the classifier that needs it.
    """
    text = f"{exc}".strip()
    if not text:
        return ""
    for separator in (":", "\u2013", "-"):
        if separator in text:
            tail = text.split(separator, 1)[-1].strip()
            if tail:
                return tail
    return text


def should_retry(
    exc: BaseException,
    *,
    idempotent: bool = True,
    attempts_used: int,
    max_retries: int,
    backoff_exponent: int,
    is_transient: Optional[bool] = None,
    is_rate_limit: Optional[bool] = None,
) -> RetryDecision:
    """Return the decision for one retry of a provider call.

    Bounded by ``attempts_used``/``max_retries`` exactly like the router's
    existing budget, and refuses a replay outright when the operation is
    not idempotent. The caller sleeps; this function never does.
    """
    decision = classify_retry(
        exc,
        idempotent=idempotent,
        is_transient=is_transient,
        is_rate_limit=is_rate_limit,
    )
    if not idempotent:
        return RetryDecision(
            retryable=False,
            replayable=False,
            kind=decision.kind,
            provider_fault=decision.provider_fault,
            reason="non-idempotent operation is never replayed",
        )
    if not decision.retryable:
        return decision
    if attempts_used >= max_retries:
        return RetryDecision(
            retryable=False,
            replayable=False,
            kind=decision.kind,
            provider_fault=decision.provider_fault,
            reason=f"retry budget exhausted ({attempts_used}/{max_retries})",
        )
    return decision


def bound_fallbacks(
    targets: Iterable[Mapping[str, Any]],
    *,
    max_fallbacks: int = DEFAULT_MAX_FALLBACKS,
) -> List[Dict[str, Any]]:
    """Return a bounded, de-duplicated candidate list for one logical call.

    Order is preserved (the primary target is first). Duplicates are removed
    by provider identity, because "try provider A again" is not a fallback.
    The primary target is never dropped even when ``max_fallbacks`` is 0 —
    a bound of zero means "no alternates", not "no call".
    """
    limit = max(0, _as_int(max_fallbacks, DEFAULT_MAX_FALLBACKS))
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for target in targets or ():
        if not isinstance(target, Mapping):
            continue
        candidate = dict(target)
        identity = provider_identity(
            candidate.get("provider"), candidate.get("api_base"), candidate.get("model")
        )
        if identity in seen:
            continue
        seen.add(identity)
        out.append(candidate)
        if len(out) > limit:
            break
    return out


def field_map(source: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a plain dict copy of a mapping (defensive, never raises)."""
    if not isinstance(source, Mapping):
        return {}
    return {str(key): value for key, value in source.items()}


@dataclass
class ProviderAttempt:
    """One candidate's outcome, recorded for the ledger and the trace."""

    identity: str
    provider: Optional[str] = None
    model: Optional[str] = None
    breaker_state: str = CLOSED
    outcome: str = "pending"
    index: int = 0
    error: Optional[str] = None
    retry: Optional[Dict[str, Any]] = None
    skipped_reason: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe attempt record (never carries a raw traceback)."""
        return {
            "index": self.index,
            "identity": self.identity,
            "provider": self.provider,
            "model": self.model,
            "breaker_state": self.breaker_state,
            "outcome": self.outcome,
            "error": self.error,
            "retry": self.retry,
            "skipped_reason": self.skipped_reason,
            **({"extra": dict(self.extra)} if self.extra else {}),
        }
