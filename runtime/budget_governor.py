"""Budget governor, quota classification, and 429-safe supervision (R2-14).

Three coupled defects shared one root cause: **nothing above the provider
dial knew about money, quota, or its own clock.** This module is the ONE
authority for all three, and everything else in the tree asks it rather than
re-deriving a second opinion.

* **Classification.** A rate limit and an exhausted quota are DIFFERENT
  events with different correct actions. A rate limit backs off; a quota
  wall is a billing fact, so it is terminal, never retried, and carries a
  pointer to the provider's billing page. :func:`classify_provider_failure`
  tests quota BEFORE rate limit, which is the whole fix: the two marker sets
  used to overlap, so ``insufficient_quota`` was served rate-limit recovery.

* **One clock, one deadline.** :func:`backoff_seconds` is the only place a
  provider backoff length is computed, and :func:`supervision_exemption`
  is the only place a supervision kill is skipped. A backoff window is
  turned into a supervision exemption by deriving it from the SAME
  ``backoff_seconds`` value (:meth:`BudgetGovernor.begin_backoff`), so the
  hang watchdog and the backoff cannot disagree about what "slow" means.
  Every comparison uses :func:`runtime.fsutil.now_epoch`.

* **Per-call budget.** :meth:`BudgetGovernor.authorize_call` prices the
  NEXT call before it is dialed and refuses one that cannot fit, which
  tightens ``budget_cap_usd`` from "cap + one whole attempt" to
  "cap + at most one call". The harness's attempt-level check stays in
  place as a backstop; both read the same governor.

The supervision exemption is the SAME mechanism the approval park already
used (``awaiting_approval`` in the runtime checkpoint), generalized to
``supervision_exemption = {reason, until_epoch, ...}``. It is written by the
worker and read by the scheduler; see :func:`supervision_exemption` and
:func:`state_stale_exempt`.

The dial loop itself lives in :func:`governed_completion`. It is reached
from ``runtime.provider_gateway`` (and therefore from
``runtime.model_router``'s single Ceiling-14 delegation point), so the
router file is not edited to obtain budget awareness.
"""

from __future__ import annotations

import contextvars
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from runtime.fsutil import atomic_write_json, now_epoch, now_iso
from runtime.redaction import WITHHELD_PREFIX, redact_provider_text

__all__ = [
    "AUTH_FAILED",
    "BAD_REQUEST",
    "BUDGET_RECEIPT_NAME",
    "EXEMPTION_APPROVAL",
    "EXEMPTION_BACKOFF",
    "PROVIDER_UNAVAILABLE",
    "QUOTA_BILLING_URLS",
    "QUOTA_EXHAUSTED",
    "QUOTA_MARKERS",
    "RATE_LIMITED",
    "SCHEMA_VERSION",
    "TERMINAL_KINDS",
    "UNKNOWN_FAILURE",
    "BackoffWindow",
    "BudgetGovernor",
    "BudgetRefused",
    "BudgetVerdict",
    "ProviderFailure",
    "QuotaExhausted",
    "backoff_seconds",
    "billing_url_for",
    "classify_provider_failure",
    "clear_governor",
    "current_governor",
    "estimated_call_price",
    "governed_completion",
    "install_governor",
    "state_stale_exempt",
    "supervision_exemption",
    "write_budget_receipt",
]

SCHEMA_VERSION = 1

#: Name of the live remaining-budget receipt written next to a task's logs.
BUDGET_RECEIPT_NAME = "budget.json"

# -- classification vocabulary (closed) ------------------------------------
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
PROVIDER_UNAVAILABLE = "provider_unavailable"
AUTH_FAILED = "auth_failed"
BAD_REQUEST = "bad_request"
UNKNOWN_FAILURE = "unknown_failure"

#: Kinds that can never be recovered by retrying. A quota wall is the
#: motivating member: waiting longer cannot create credit.
TERMINAL_KINDS = frozenset({QUOTA_EXHAUSTED, AUTH_FAILED, BAD_REQUEST})

#: Provider text that means "you are out of money", not "slow down".
#: Checked BEFORE the rate-limit markers; ``insufficient_quota`` and
#: ``quota exceeded`` used to sit inside the rate-limit set, which is the
#: misclassification this module exists to remove.
QUOTA_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "exceeded your current quota",
    "exceeded your quota",
    "quota_exceeded",
    "excessive quota",
    "billing hard limit",
    "spending limit",
    "credit balance is too low",
    "insufficient credits",
    "insufficient funds",
    "out of credits",
    "payment required",
    "usage limit reached",
    "monthly limit",
)

#: Provider text that means "slow down" and IS worth waiting out.
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "ratelimit",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "request limit",
    "requests per minute",
    "rpm limit",
    "tpm limit",
    "tokens per minute",
    "slow down",
)

_TRANSIENT_MARKERS: tuple[str, ...] = (
    "internalservererror",
    "apiconnectionerror",
    "serviceunavailable",
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "overloaded",
    "bad gateway",
    "gateway timeout",
)

_AUTH_MARKERS: tuple[str, ...] = (
    "authenticationerror",
    "permissiondeniederror",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "401",
    "403",
)

_BAD_REQUEST_MARKERS: tuple[str, ...] = (
    "badrequesterror",
    "invalidrequesterror",
    "contentpolicyviolation",
    "400",
    "404",
    "422",
)

_SERVER_CODES: tuple[str, ...] = ("500", "502", "503", "504", "529", "529 overloaded")

#: Where a user goes to add credit. A quota refusal with no pointer is a
#: dead end, so every quota verdict carries one even when the provider is
#: unknown (the generic entry is deliberately a real, generic page).
QUOTA_BILLING_URLS: Dict[str, str] = {
    "openai": "https://platform.openai.com/settings/organization/billing/overview",
    "anthropic": "https://console.anthropic.com/settings/billing",
    "default": "https://platform.openai.com/settings/organization/billing/overview",
}


def billing_url_for(provider: Optional[str]) -> str:
    """Return the billing page for ``provider`` (never empty).

    Assumes ``provider`` is a bare provider name (``"openai"``), not a
    ``provider/model`` litellm string. An unrecognised provider returns the
    generic entry rather than an empty string, so a quota refusal always
    points somewhere.
    """
    name = str(provider or "").strip().lower()
    if "/" in name:
        name = name.split("/", 1)[0]
    return QUOTA_BILLING_URLS.get(name, QUOTA_BILLING_URLS["default"])


@dataclass(frozen=True)
class ProviderFailure:
    """One classified provider failure, with the action it implies.

    ``retryable`` says whether waiting could help. ``provider_fault`` says
    whether this is the ENDPOINT's problem (it counts against a circuit
    breaker and may justify a failover) as opposed to the ACCOUNT's
    problem. A quota wall is deliberately not a provider fault: the
    endpoint is healthy, the credit is gone, and failing over to a second
    target on the same billing account just spends the same money twice.
    """

    kind: str
    retryable: bool
    terminal: bool
    provider_fault: bool
    reason: str
    detail: str = ""
    status_code: Optional[int] = None
    billing_url: Optional[str] = None
    #: What HAPPENED to ``detail``. Three outcomes that used to be the same
    #: empty string -- "the provider said nothing", "we redacted it", "we cut
    #: it" -- and a reader cannot tell them apart without these. See
    #: ``runtime.redaction`` for the boundary that produces them.
    detail_withheld: bool = False
    detail_redacted: bool = False
    detail_truncated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for a ledger row or trace event.

        Every value here is a FACT about what happened: ``detail`` is already
        redacted at source (``runtime.redaction``), the three flags say what
        was done to it, and ``reason`` is composed from the classification
        rather than from the provider's own text -- so it survives any
        redaction, and a withheld detail never costs the user the class.
        """
        return {
            "kind": self.kind,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "provider_fault": self.provider_fault,
            "reason": self.reason,
            "detail": self.detail,
            "detail_withheld": self.detail_withheld,
            "detail_redacted": self.detail_redacted,
            "detail_truncated": self.detail_truncated,
            "status_code": self.status_code,
            "billing_url": self.billing_url,
        }


class QuotaExhausted(RuntimeError):
    """Raised when a provider reports an exhausted quota or credit balance.

    Terminal by construction: the only correct recovery is for a human to
    add credit or raise the limit. ``failure`` carries the classified
    record, including the billing pointer.
    """

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(
            f"provider quota exhausted: {failure.reason}"
            + (
                f" (add credit or raise the limit at {failure.billing_url})"
                if failure.billing_url
                else ""
            )
        )
        self.failure = failure


class BudgetRefused(RuntimeError):
    """Raised when a model call cannot fit inside the remaining budget.

    Not a provider failure and not a task failure on its own: it is the
    per-call pre-check refusing to dial. The attempt-level budget check
    remains the backstop that turns this into a terminal run outcome.
    ``verdict`` carries the measured numbers.
    """

    def __init__(self, verdict: "BudgetVerdict") -> None:
        super().__init__(
            "model call refused by the budget governor: "
            f"remaining ${verdict.remaining_usd:.6f} cannot cover a reserved "
            f"call of ${verdict.reserved_usd:.6f} "
            f"(cap ${(verdict.cap_usd or 0.0):.6f}, spent ${verdict.spent_usd:.6f})"
        )
        self.verdict = verdict


@dataclass(frozen=True)
class BudgetVerdict:
    """The measured answer to "may this call be dialed?".

    ``price_state`` is R2-13's closed vocabulary where it applies --
    ``priced`` | ``free`` | ``unpriced`` from
    ``runtime.model_capabilities.price_of`` -- plus this module's own two
    rungs for the cases a price row cannot answer: ``declared_bound`` (the
    caller declared a per-call bound) and ``uncapped`` / ``cap_reached`` /
    ``quota_source`` for the non-price outcomes. So a reader can tell a
    real price from a declared bound from a guess, and ``"unpriced"`` is a
    REPORTED state, not a zero cost: a model with no price row cannot be
    reserved against, and saying so is the point.
    """

    allowed: bool
    cap_usd: Optional[float]
    spent_usd: float
    reserved_usd: float
    remaining_usd: float
    price_state: str
    model: str = ""
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for a ledger row or trace event."""
        return {
            "allowed": self.allowed,
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 8),
            "reserved_usd": round(self.reserved_usd, 8),
            # `None` means "no cap", which is a meaningful state and must
            # survive serialization rather than crashing on round().
            "remaining_usd": (
                None if self.remaining_usd is None else round(self.remaining_usd, 8)
            ),
            "price_state": self.price_state,
            "model": self.model,
            "reason": self.reason,
        }


def _chain_text(exc: BaseException, limit: int = 12) -> str:
    """Return the lowercased text of an exception chain, identity-guarded.

    ``litellm`` wraps the real cause, so a single-link scan misses it. The
    walk is bounded because a self-referential ``__context__`` (re-raise
    inside ``except``) is a real thing and an unbounded walk would hang.
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


def _status_code(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from an exception, or None."""
    for candidate in (exc, getattr(exc, "response", None)):
        code = getattr(candidate, "status_code", None)
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.isdigit():
            return int(code)
    match = re.search(r"\b([1-5]\d{2})\b", f"{type(exc).__name__} {exc}")
    return int(match.group(1)) if match else None


def _provider_of(exc: BaseException) -> str:
    """Best-effort provider name from an exception, or ''."""
    for attribute in ("provider", "llm_provider", "model_provider"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


#: Ceiling on the detail a classified provider failure carries. This is the
#: value that used to be ``str(exc)[:500]`` -- the slice is UNCHANGED, and
#: what changed is that it is redacted BEFORE it is stored.
DETAIL_CHARS = 500


def _safe_detail(exc: BaseException) -> tuple[str, bool, bool, bool]:
    """Return ``(detail, withheld, redacted, truncated)`` for ``exc``.

    The one place a provider exception's TEXT becomes a stored value. It used
    to be ``detail=str(exc)[:500]`` in six places, and every one of them wrote
    raw provider text into an artifact that is not redacted at the writer:
    ``budget.json``, ``checkpoint.json``, and the worker journal. A gateway
    answers a bad key with a body that echoes the key, so that is the most
    likely real path for a credential to reach disk.

    Redaction is at SOURCE (through ``runtime.redaction`` ->
    ``shared.security``), not at display time, so the artifact is safe by
    construction rather than by later scrubbing. The three flags are facts
    about what was done to the value, so a reader can tell "the provider said
    nothing" from "we redacted it" from "we cut it".

    The classification is NOT applied here -- it is computed first, by
    :func:`classify_provider_failure`, from the unshortened exception text,
    and it is what the reader is left with if the detail is withheld.
    """
    raw = "" if exc is None else str(exc)
    detail = redact_provider_text(
        raw, limit=DETAIL_CHARS, label="provider error detail"
    )
    withheld = detail.startswith(WITHHELD_PREFIX)
    return (
        detail,
        withheld,
        (not withheld) and detail != raw[:DETAIL_CHARS],
        detail.endswith(" [truncated]"),
    )


def classify_provider_failure(exc: BaseException) -> ProviderFailure:
    """Classify a provider failure into kind + action.

    Assumes ``exc`` is a provider-shaped exception (a litellm error or a
    test double shaped like one). A non-provider exception is NOT
    mislabelled as a provider fault: an unrecognised chain is
    ``unknown_failure`` and is not retried, so this classifier can never
    turn a bug in our own code into provider traffic.

    **Quota is tested FIRST.** ``insufficient_quota`` and ``quota exceeded``
    contain no rate-limit wording, but they used to be listed as rate-limit
    markers, so an exhausted account was told to back off and retry --
    which cannot help and burns the remaining credit. The order below is
    the fix and is asserted by a test.
    """
    if isinstance(exc, QuotaExhausted):
        return exc.failure
    text = _chain_text(exc)
    status = _status_code(exc)
    provider = _provider_of(exc)
    # Redacted ONCE, before any branch below, so no return path can store raw
    # provider text and no branch can be forgotten later. The classification
    # above it already read the unshortened `text`, which is why the order
    # here is safe and not a reason to worry about losing the markers.
    detail, withheld, redacted, truncated = _safe_detail(exc)

    if _looks_like(text, QUOTA_MARKERS) or status == 402:
        return ProviderFailure(
            kind=QUOTA_EXHAUSTED,
            retryable=False,
            terminal=True,
            provider_fault=False,
            reason=f"{provider or 'provider'} reports an exhausted quota or credit balance",
            detail=detail,
            detail_withheld=withheld,
            detail_redacted=redacted,
            detail_truncated=truncated,
            status_code=status,
            billing_url=billing_url_for(provider),
        )
    if _looks_like(text, _AUTH_MARKERS) or status in (401, 403):
        return ProviderFailure(
            kind=AUTH_FAILED,
            retryable=False,
            terminal=True,
            provider_fault=True,
            reason="provider rejected the credential; waiting cannot fix it",
            detail=detail,
            detail_withheld=withheld,
            detail_redacted=redacted,
            detail_truncated=truncated,
            status_code=status,
        )
    if _looks_like(text, RATE_LIMIT_MARKERS) or status == 429:
        return ProviderFailure(
            kind=RATE_LIMITED,
            retryable=True,
            terminal=False,
            provider_fault=True,
            reason="provider rate limit; a bounded wait is the correct recovery",
            detail=detail,
            detail_withheld=withheld,
            detail_redacted=redacted,
            detail_truncated=truncated,
            status_code=status,
        )
    if _looks_like(text, _BAD_REQUEST_MARKERS) and status not in (500, 502, 503, 504):
        return ProviderFailure(
            kind=BAD_REQUEST,
            retryable=False,
            terminal=True,
            provider_fault=False,
            reason="request rejected as invalid; a retry would send the same request",
            detail=detail,
            detail_withheld=withheld,
            detail_redacted=redacted,
            detail_truncated=truncated,
            status_code=status,
        )
    if _looks_like(text, _TRANSIENT_MARKERS) or (
        status is not None and str(status) in _SERVER_CODES
    ):
        return ProviderFailure(
            kind=PROVIDER_UNAVAILABLE,
            retryable=True,
            terminal=False,
            provider_fault=True,
            reason="transient provider failure",
            detail=detail,
            detail_withheld=withheld,
            detail_redacted=redacted,
            detail_truncated=truncated,
            status_code=status,
        )
    return ProviderFailure(
        kind=UNKNOWN_FAILURE,
        retryable=False,
        terminal=True,
        provider_fault=False,
        reason="unclassified failure; not retried rather than guessed at",
        detail=detail,
        detail_withheld=withheld,
        detail_redacted=redacted,
        detail_truncated=truncated,
        status_code=status,
    )


#: Ceiling on any single backoff wait (s). A wait longer than this is
#: indistinguishable from a hang to any operator, including the watchdog's
#: own wall-clock backstop.
MAX_BACKOFF_S = 300.0
#: Ceiling on the bounded transient-retry budget (attempts, not seconds).
MAX_TRANSIENT_ATTEMPTS = 2
#: The transient (non-rate-limit) wait, in seconds. Deliberately a
#: constant rather than a config key: it is a fixed property of the
#: existing transient path and changing it would change every task.
TRANSIENT_BACKOFF_S = 5.0


def backoff_seconds(
    attempt: int,
    *,
    base_s: float,
    cap_s: float = MAX_BACKOFF_S,
    multiplier: float = 2.0,
) -> float:
    """Return the wait before provider retry ``attempt`` (1-based).

    Assumes ``base_s >= 0``. The result is clamped into
    ``[0, min(cap_s, MAX_BACKOFF_S)]`` and is deterministic, so a test can
    assert that the supervision exemption is derived from exactly this
    number. **This is the only place a provider backoff length is
    computed**; the retry loop, the supervision exemption, and the
    watchdog all read it, which is what stops them disagreeing.
    """
    index = max(0, int(attempt) - 1)
    ceiling = max(0.0, min(float(cap_s), MAX_BACKOFF_S))
    try:
        base = max(0.0, float(base_s))
    except (TypeError, ValueError):
        base = 0.0
    try:
        factor = max(0.0, float(multiplier))
    except (TypeError, ValueError):
        factor = 0.0
    if base == 0.0 or factor == 0.0:
        return 0.0
    return min(ceiling, base * (factor**index))


# -- supervision exemption (the approval-park mechanism, generalized) ------
EXEMPTION_APPROVAL = "approval_gate"
EXEMPTION_BACKOFF = "provider_backoff"


@dataclass(frozen=True)
class BackoffWindow:
    """A live provider backoff, expressed on the supervision clock.

    ``until_epoch`` comes from :func:`backoff_seconds` and the SAME
    :func:`runtime.fsutil.now_epoch` the watchdog reads, so "the worker is
    inside a backoff" and "state.json went stale" are comparable facts
    rather than two systems' private arithmetic.

    ``grace_s`` is the DECLARED extra window beyond the backoff itself. It
    is not slack: a worker whose backoff has just expired still has to land
    the retried call and write state, and without it the state-stale kill
    fires the instant the backoff ends -- which is the same defect one
    moment later. The caller supplies it from the watchdog's own staleness
    threshold, so it is the watchdog's window reused rather than a second
    number that can drift.
    """

    reason: str
    started_epoch: float
    until_epoch: float
    attempt: int
    seconds: float
    kind: str = RATE_LIMITED
    grace_s: float = 0.0

    def remaining_s(self, now: Optional[float] = None) -> float:
        """Seconds of EXEMPTION left, on the shared clock. Never negative.

        This is the licence, and it deliberately includes ``grace_s``. It
        is NOT the wait: use :meth:`backoff_remaining_s` for that.
        """
        current = now_epoch() if now is None else float(now)
        return max(0.0, self.until_epoch - current)

    def backoff_remaining_s(self, now: Optional[float] = None) -> float:
        """Seconds of the BACKOFF itself left, on the shared clock.

        Separate from :meth:`remaining_s` because the sleep and the licence
        are different facts. Sleeping the licence would add the grace to
        every wait -- the worker would sit still for a window nobody asked
        it to wait for, and a 225 s backoff would become a 255 s one.
        """
        current = now_epoch() if now is None else float(now)
        return max(0.0, (self.started_epoch + self.seconds) - current)

    def covers(self, now: Optional[float] = None) -> bool:
        """Whether the exemption still holds at ``now``."""
        return self.remaining_s(now) > 0.0

    def as_dict(self) -> Dict[str, Any]:
        """Return the checkpoint-shaped supervision marker."""
        return {
            "reason": self.reason,
            "kind": self.kind,
            "attempt": self.attempt,
            "seconds": round(float(self.seconds), 3),
            "grace_s": round(float(self.grace_s), 3),
            "started_epoch": round(float(self.started_epoch), 3),
            "until_epoch": round(float(self.until_epoch), 3),
            "clock": "runtime.fsutil.now_epoch",
        }


def supervision_exemption(
    checkpoint: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the live supervision exemption recorded in a checkpoint.

    Accepts either the historical ``awaiting_approval`` boolean (which has
    no expiry -- an unbounded park is bounded by the wall-clock cap) or the
    generalized ``supervision_exemption`` mapping. Returns ``None`` when
    the worker claims no exemption, which is the fail-closed default: a
    missing marker means the state-stale kill applies exactly as before.
    """
    if not isinstance(checkpoint, Mapping):
        return None
    marker = checkpoint.get("supervision_exemption")
    if isinstance(marker, Mapping) and marker.get("reason"):
        out = dict(marker)
        try:
            out["until_epoch"] = float(out.get("until_epoch"))
        except (TypeError, ValueError):
            # An unparsable deadline is not a licence to skip a kill: the
            # marker degrades to a reason with no expiry, so the exemption
            # is honoured only through the boolean it also carries.
            out["until_epoch"] = None
        return out
    if bool(checkpoint.get("awaiting_approval")):
        # Backward compatibility with the approval-park marker, which is
        # cleared atomically with status="finished" and has no deadline.
        return {"reason": EXEMPTION_APPROVAL, "until_epoch": None}
    return None


def state_stale_exempt(
    checkpoint: Optional[Mapping[str, Any]],
    *,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """Decide whether the state-stale hang kill is skipped for a checkpoint.

    Returns ``(exempt, reason)``. A live exemption with a future
    ``until_epoch`` exempts the worker; an EXPIRED one does not, so a
    worker that stops making progress after its backoff ends is killed
    exactly as before. A heartbeat kill and the wall-clock cap are NOT
    exemptions and are never read here.
    """
    marker = supervision_exemption(checkpoint)
    if marker is None:
        return False, ""
    until = marker.get("until_epoch")
    if until is None:
        # No deadline recorded (the approval gate): honour it while the
        # checkpoint is still running, which the caller already checks.
        return True, str(marker.get("reason") or "unspecified")
    current = now_epoch() if now is None else float(now)
    if float(until) > current:
        return True, (
            f"{marker.get('reason')}: {float(until) - current:.1f}s of exemption left"
        )
    return False, f"{marker.get('reason')}: exemption expired"


# -- pricing ---------------------------------------------------------------
#: Chars-per-token for the prompt estimate. Identical to the heuristic the
#: router itself uses for a missing usage frame, so the reservation and
#: the charge are measured the same way.
CHARS_PER_TOKEN = 4
#: Completion bound used when the caller has declared NO completion budget.
#: Deliberately small, and deliberately documented as an ESTIMATE: the
#: enforced guarantee is "cap + the price of the final call", not "cap",
#: because the price of a call is not knowable before it is made. A
#: governance-chosen LARGE default would instead make the cap fire early
#: on cheap models, which is a different lie (it reports a budget the
#: user never spent). Declare `max_completion_tokens` and the reserve
#: becomes a sound upper bound instead.
DEFAULT_UNOBSERVED_COMPLETION_TOKENS = 512


def estimated_call_price(
    messages: Optional[Iterable[Mapping[str, Any]]],
    *,
    target: Optional[Mapping[str, Any]] = None,
    max_completion_tokens: Optional[int] = None,
    fallback_per_call_usd: Optional[float] = None,
) -> tuple[float, str]:
    """Return ``(price_usd, price_state)`` for ONE upcoming model call.

    **The price and the price STATE both come from R2-13's capability
    registry** via ``runtime.model_capabilities.estimate_cost``, which
    owns the closed ``(priced, free, unpriced)`` vocabulary. This module
    deliberately does NOT infer price state from a number and does NOT
    keep a second price table: `0.0` is ambiguous on its own, and a
    reservation that priced an unpriced model at zero would be the exact
    defect R2-13 exists to remove, reintroduced one layer up. If the two
    layers ever disagreed about a price, the cost report and the cap would
    disagree too, and neither would be auditable.

    Rungs, most trustworthy first:

    1. ``"priced"`` / ``"free"`` -- the registry priced the model, and the
       rate is applied to the router's own prompt/completion accounting
       (``CHARS_PER_TOKEN``, the heuristic the router already uses for a
       missing usage frame). ``"free"`` is reachable only by a DECLARED
       ``(0.0, 0.0)`` row and is a different answer from ``"unpriced"``.
    2. ``"declared_bound"`` -- the model is unpriced and the caller declared
       ``budget_reserve_per_call_usd``. A declared bound is honest; a guess
       is not.
    3. ``"unpriced"`` -- no price row and no declared bound. **Reported, not
       rounded to zero.** The pre-check cannot refuse on price here, and the
       receipt says exactly why.
    """
    prompt_tokens = 10
    for message in messages or ():
        if isinstance(message, Mapping):
            prompt_tokens += len(str(message.get("content", ""))) // CHARS_PER_TOKEN
    try:
        completion = int(max_completion_tokens or 0)
    except (TypeError, ValueError):
        completion = 0
    if completion <= 0:
        completion = DEFAULT_UNOBSERVED_COMPLETION_TOKENS

    model = str(target.get("model") or "") if isinstance(target, Mapping) else ""
    if model:
        try:
            from . import model_capabilities

            estimate = model_capabilities.estimate_cost(
                model, prompt_tokens, completion
            )
            state = str(estimate.price_state or model_capabilities.PRICE_UNPRICED)
            if state in (
                model_capabilities.PRICE_PRICED,
                model_capabilities.PRICE_FREE,
            ):
                return max(0.0, float(estimate.cost_usd)), state
        except Exception:
            # A broken registry must not become a broken cap: fall through
            # to the declared bound, then to a REPORTED unpriced.
            pass
    if fallback_per_call_usd is not None:
        try:
            bound = float(fallback_per_call_usd)
        except (TypeError, ValueError):
            bound = 0.0
        if bound > 0.0:
            return bound, "declared_bound"
    return 0.0, "unpriced"


# -- the governor ----------------------------------------------------------
_DEFAULT_RECEIPT_MAX_CALLS = 200


@dataclass
class _Counters:
    """Monotonic counters for the receipt (never a second ledger)."""

    calls_authorized: int = 0
    calls_refused: int = 0
    cost_committed_usd: float = 0.0
    peak_reserved_usd: float = 0.0
    backoffs: int = 0
    backoff_seconds_total: float = 0.0
    quota_failures: int = 0
    receipt_log: List[Dict[str, Any]] = field(default_factory=list)


class BudgetGovernor:
    """The one budget, quota, and deadline authority for a task.

    Assumes a single task runs in the context that installed it (the
    worker installs one per process; see :func:`install_governor`), and
    that ``Task.config`` supplies the cap. Every knob is read from the
    config or from an explicit constructor argument -- there is no
    hardcoded retry limit, budget, or timeout in this class.

    ``spend_source`` is how ONE spend authority is preserved: the harness's
    own ``ModelClient.total_cost_usd`` already includes calls the governor
    never sees (the difficulty classifier), while the governor sees
    provider-fallback attempts the harness does not. :meth:`spent_usd`
    therefore returns the MAXIMUM of the two, never the sum, so the cap can
    only fire earlier than either source alone would -- never later.
    """

    def __init__(
        self,
        *,
        cap_usd: Optional[float] = None,
        spend_source: Optional[Callable[[], float]] = None,
        on_exemption: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None,
        on_receipt: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_quota: Optional[Callable[[ProviderFailure], None]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        clock: Callable[[], float] = now_epoch,
        sleep: Callable[[float], None] = time.sleep,
        max_wallclock_s: Optional[float] = None,
        started_epoch: Optional[float] = None,
        backoff_base_s: float = 15.0,
        backoff_cap_s: float = MAX_BACKOFF_S,
        backoff_multiplier: float = 2.0,
        backoff_grace_s: float = 0.0,
        rate_limit_retries: int = 4,
        reserve_per_call_usd: Optional[float] = None,
        max_completion_tokens: Optional[int] = None,
        transient_backoff_s: float = TRANSIENT_BACKOFF_S,
        receipt_max_calls: int = _DEFAULT_RECEIPT_MAX_CALLS,
        task_id: str = "",
    ) -> None:
        self.task_id = str(task_id or "")
        self.cap_usd = None if cap_usd is None else max(0.0, float(cap_usd))
        self.spend_source = spend_source
        self.on_exemption = on_exemption
        self.on_receipt = on_receipt
        self.on_quota = on_quota
        self.on_event = on_event
        self.clock = clock
        self.sleep = sleep
        self.max_wallclock_s = (
            None if max_wallclock_s is None else float(max_wallclock_s)
        )
        self.started_epoch = (
            float(started_epoch) if started_epoch is not None else clock()
        )
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_cap_s = float(backoff_cap_s)
        self.backoff_multiplier = float(backoff_multiplier)
        self.backoff_grace_s = max(0.0, float(backoff_grace_s))
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.reserve_per_call_usd = reserve_per_call_usd
        self.max_completion_tokens = max_completion_tokens
        self.transient_backoff_s = float(transient_backoff_s)
        self.receipt_max_calls = max(0, int(receipt_max_calls))

        self._reserved_usd = 0.0
        self._quota: Optional[ProviderFailure] = None
        self._exhausted = False
        self._window: Optional[BackoffWindow] = None
        self._counters = _Counters()

    # -- lifecycle -------------------------------------------------------

    @property
    def deadline_epoch(self) -> Optional[float]:
        """The one wall-clock deadline, on the one clock.

        ``None`` when no wall-clock cap is configured, which reads as "no
        deadline" rather than as "deadline zero" -- the same refusal-to-
        guess rule the context-window authority uses.
        """
        if self.max_wallclock_s is None:
            return None
        return self.started_epoch + self.max_wallclock_s

    def over_wallclock(self, now: Optional[float] = None) -> bool:
        """Whether the one wall-clock deadline has passed."""
        deadline = self.deadline_epoch
        if deadline is None:
            return False
        return (now_epoch() if now is None else float(now)) >= deadline

    def bind_spend_source(self, source: Optional[Callable[[], float]]) -> None:
        """Bind (or clear) the external spend authority.

        Assumes ``source`` returns a non-negative float. A source that
        raises is IGNORED, and the governor falls back to its own
        accounting: a broken spend reader must not disable the cap.
        """
        self.spend_source = source

    def reset_window(self) -> None:
        """Clear the exemption marker (after a backoff ends or a run ends)."""
        self._window = None
        self._emit_exemption(None)
        self._publish()

    def publish(self) -> None:
        """Publish the receipt now, without a state change.

        The worker calls this once at construction so ``logs/{task_id}/
        budget.json`` exists from the START of a run. A live budget that
        only appears once something has been charged is a budget a user
        discovers afterwards, which is the thing this is meant to prevent.
        """
        self._publish()

    # -- money -----------------------------------------------------------

    def spent_usd(self) -> float:
        """Spend seen, as the MAXIMUM of the governor's own and the bound source."""
        observed = self._counters.cost_committed_usd
        if self.spend_source is None:
            return max(0.0, observed)
        try:
            external = float(self.spend_source())
        except Exception:
            return max(0.0, observed)
        return max(0.0, observed, external if external == external else 0.0)

    def reserved_usd(self) -> float:
        """Total currently reserved for in-flight authorized calls."""
        return max(0.0, float(self._reserved_usd))

    def remaining_usd(self) -> Optional[float]:
        """Budget left, or ``None`` when no cap is configured."""
        if self.cap_usd is None:
            return None
        return max(0.0, self.cap_usd - self.spent_usd() - self.reserved_usd())

    @property
    def exhausted(self) -> bool:
        """Whether the cap is reached or a call was already refused.

        Sticky once a per-call refusal has happened: the attempt-level
        check in the harness reads this, which is what makes it a genuine
        backstop rather than a second, independent number.
        """
        if self._exhausted:
            return True
        remaining = self.remaining_usd()
        return remaining is not None and remaining <= 0.0

    def commit(self, cost_usd: float, *, model: str = "") -> None:
        """Record spend that has actually happened.

        Assumes ``cost_usd >= 0``; a negative or non-finite value is
        ignored rather than credited back, because a negative charge is a
        provider bug and must not become budget.
        """
        try:
            value = float(cost_usd)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value <= 0.0:
            return
        self._counters.cost_committed_usd += value
        self._publish()

    def authorize_call(
        self,
        *,
        messages: Optional[Iterable[Mapping[str, Any]]] = None,
        target: Optional[Mapping[str, Any]] = None,
        price_usd: Optional[float] = None,
        price_state: str = "",
        max_completion_tokens: Optional[int] = None,
    ) -> BudgetVerdict:
        """Decide whether the NEXT model call fits inside the cap.

        This is the per-call pre-dial check. It reserves the call's price
        so a burst of calls cannot collectively overshoot the cap between
        two attempt-level checks: the historical behaviour was that the cap
        fired at attempt START, so one attempt could overshoot by a large
        multiple (a previous round measured 9.28x a deliberately tiny cap).

        ``max_completion_tokens`` is the caller's DECLARED completion
        bound. Supplying it makes the reservation a sound upper bound and
        the tail zero in practice; omitting it falls back to
        :data:`DEFAULT_UNOBSERVED_COMPLETION_TOKENS`, which makes the
        enforced guarantee "cap + the price of the final call" — the honest
        bound, since a call's price is not knowable before it is made.

        When no cap is configured the verdict is ``allowed=True`` with
        ``cap_usd=None``: absent is a meaningful state, not a zero budget.
        """
        spent = self.spent_usd()
        # The pre-check MUST read the reservation-aware remainder, not a second
        # formula of its own. It used to compute `cap - spent` here while
        # `remaining_usd()` computed `cap - spent - reserved`, so the two
        # disagreed by exactly the in-flight reservations: each of a burst of
        # calls was checked against the SAME `remaining`, and the reservation
        # that exists to stop the burst was recorded only after the call had
        # already been let through. A 0.10 cap with 0.05 reserved would then
        # authorize a further 0.06 call -- 1.1x the cap reserved at once --
        # and only flip `exhausted` afterwards.
        #
        # One authority for "what is left" is the fix, and it can only fire
        # EARLIER: subtracting what is already spoken for can never let a call
        # through that the cap could not cover.
        remaining = self.remaining_usd()
        model_name = str(target.get("model") if isinstance(target, Mapping) else "")
        if self._quota is not None:
            return self._verdict(
                False,
                spent,
                0.0,
                0.0,
                "quota_source",
                model_name,
                "provider quota is exhausted; no further call may be dialed",
            )
        if remaining is None:
            return self._verdict(
                True,
                spent,
                0.0,
                None,
                "uncapped",
                model_name,
                "no budget_cap_usd configured",
            )
        if self._exhausted:
            # A refusal is STICKY. The alternative -- let a later, smaller
            # call squeeze through -- would let a run dribble past a cap it
            # has already been told it reached, and the attempt-level
            # backstop would then read as a mid-run flicker. The cap is
            # fixed at construction, so nothing here can lower it back.
            return self._verdict(
                False,
                spent,
                0.0,
                remaining,
                "cap_reached",
                model_name,
                "the cap was already reached; no further call may be dialed",
            )
        if price_usd is None:
            price, source = estimated_call_price(
                messages,
                target=target,
                max_completion_tokens=(
                    max_completion_tokens
                    if max_completion_tokens is not None
                    else self.max_completion_tokens
                ),
                fallback_per_call_usd=self.reserve_per_call_usd,
            )
        else:
            price, source = max(0.0, float(price_usd)), price_state or "declared"
        allowed = price <= remaining
        reason = "" if allowed else "reserved call does not fit in the remaining budget"
        if allowed and source == "unpriced" and price <= 0.0:
            reason = "no price row and no declared bound; the pre-check cannot refuse"
        if allowed:
            self._reserved_usd += price
            self._counters.calls_authorized += 1
            self._counters.peak_reserved_usd = max(
                self._counters.peak_reserved_usd, self.reserved_usd()
            )
        else:
            self._counters.calls_refused += 1
            self._exhausted = True
        verdict = self._verdict(
            allowed,
            spent,
            price,
            remaining,
            source,
            model_name,
            reason,
        )
        self._publish(verdict)
        return verdict

    def release(self, reserved_usd: float) -> None:
        """Release a reservation once its call has been charged or failed.

        Callers that used :meth:`authorize_call` must call this exactly
        once per authorization, otherwise a reservation leaks and the cap
        fires early. A negative release is clamped at zero.
        """
        try:
            value = float(reserved_usd)
        except (TypeError, ValueError):
            return
        self._reserved_usd = max(0.0, self._reserved_usd - max(0.0, value))

    def note_quota(self, failure: ProviderFailure) -> None:
        """Record an exhausted quota and arm the terminal state.

        A quota wall is never retried and never reserved past: the first
        observation latches ``_quota``, so every later authorization is
        refused and :attr:`exhausted` becomes True.
        """
        self._quota = failure
        self._counters.quota_failures += 1
        self._exhausted = True
        self._publish()
        if self.on_quota is not None:
            try:
                self.on_quota(failure)
            except Exception:
                pass
        self._event(
            "quota_exhausted",
            {
                "kind": failure.kind,
                "reason": failure.reason,
                "billing_url": failure.billing_url,
                "status_code": failure.status_code,
                "detail": failure.detail,
            },
        )

    @property
    def quota_failure(self) -> Optional[ProviderFailure]:
        """The latched quota failure, or ``None``."""
        return self._quota

    # -- backoff + supervision exemption ---------------------------------

    def backoff_for(self, attempt: int) -> float:
        """The wait before retry ``attempt``, from the ONE backoff schedule."""
        return backoff_seconds(
            attempt,
            base_s=self.backoff_base_s,
            cap_s=self.backoff_cap_s,
            multiplier=self.backoff_multiplier,
        )

    def begin_backoff(self, attempt: int, *, kind: str = RATE_LIMITED) -> BackoffWindow:
        """Arm the supervision exemption for one backoff wait plus its grace.

        The window is ``backoff_for(attempt)`` seconds from now, which is
        the same value :func:`governed_completion` is about to sleep, plus
        ``backoff_grace_s``. The backoff part is the fix for a backoff
        outliving the hang window: the measured 429 backoff (~225 s) met a
        ~30 s kill window. The grace part is what makes the fix hold past
        the end of the wait -- a worker whose backoff expires still has to
        land the retried call and write state, and without the grace the
        state-stale kill fires the instant the exemption lapses, which is
        the same defect one moment later.
        """
        seconds = self.backoff_for(attempt)
        started = self.clock()
        window = BackoffWindow(
            reason=EXEMPTION_BACKOFF,
            started_epoch=started,
            until_epoch=started + seconds + self.backoff_grace_s,
            attempt=int(attempt),
            seconds=seconds,
            kind=kind,
            grace_s=self.backoff_grace_s,
        )
        self._window = window
        self._counters.backoffs += 1
        self._counters.backoff_seconds_total += seconds
        self._emit_exemption(window.as_dict())
        self._event(
            "provider_backoff",
            {
                "attempt": window.attempt,
                "backoff_s": round(seconds, 3),
                "grace_s": round(self.backoff_grace_s, 3),
                "exempt_s": round(seconds + self.backoff_grace_s, 3),
                "until_epoch": round(window.until_epoch, 3),
                "kind": kind,
                "exemption": "supervision_exemption",
            },
        )
        # The backoff is a moment a user watching a run most wants the
        # remaining budget, so the receipt is published here too.
        self._publish()
        return window

    @property
    def backoff_window(self) -> Optional[BackoffWindow]:
        """The live backoff window, or ``None`` when not backing off."""
        return self._window

    def sleep_in_backoff(self, window: BackoffWindow) -> None:
        """Sleep the BACKOFF remainder of ``window`` on the shared clock.

        Deliberately ``window.backoff_remaining_s`` and not
        ``window.remaining_s``: the wait and the licence are different
        facts, and sleeping the licence would silently add the grace to
        every backoff. Both numbers come from the same ``BackoffWindow``,
        so they cannot drift apart either.
        """
        self.sleep(max(0.0, window.backoff_remaining_s(self.clock())))

    # -- receipt ---------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """Return the live remaining-budget receipt.

        This is what a run's visibility surface reads, so a cap is seen
        approaching rather than discovered afterwards. It is a receipt, not
        a claim: ``price_state`` says which rung priced the reserve, and
        an unpriced model is reported as such.
        """
        spent = self.spent_usd()
        remaining = self.remaining_usd()
        return {
            "schema_version": SCHEMA_VERSION,
            "ts": now_iso(),
            "epoch": round(self.clock(), 3),
            "task_id": self.task_id,
            "cap_usd": self.cap_usd,
            "spent_usd": round(spent, 8),
            "reserved_usd": round(self.reserved_usd(), 8),
            "remaining_usd": None if remaining is None else round(remaining, 8),
            "exhausted": self.exhausted,
            "clock": "runtime.fsutil.now_epoch",
            "deadline_epoch": (
                None if self.deadline_epoch is None else round(self.deadline_epoch, 3)
            ),
            "max_wallclock_s": self.max_wallclock_s,
            "backoff": {
                "base_s": self.backoff_base_s,
                "cap_s": self.backoff_cap_s,
                "multiplier": self.backoff_multiplier,
                "grace_s": self.backoff_grace_s,
                "retries": self.rate_limit_retries,
                "count": self._counters.backoffs,
                "seconds_total": round(self._counters.backoff_seconds_total, 3),
                "window": self._window.as_dict() if self._window else None,
            },
            "quota": (
                self._quota.as_dict()
                if self._quota is not None
                else {"state": "available", "kind": None, "billing_url": None}
            ),
            "calls_authorized": self._counters.calls_authorized,
            "calls_refused": self._counters.calls_refused,
            "cost_committed_usd": round(self._counters.cost_committed_usd, 8),
            "peak_reserved_usd": round(self._counters.peak_reserved_usd, 8),
            "reserve_per_call_usd": self.reserve_per_call_usd,
            "max_completion_tokens": self.max_completion_tokens,
            "recent": list(self._counters.receipt_log),
        }

    # -- internals -------------------------------------------------------

    def _verdict(
        self,
        allowed: bool,
        spent: float,
        reserved: float,
        remaining: Optional[float],
        price_state: str,
        model: str,
        reason: str,
    ) -> BudgetVerdict:
        verdict = BudgetVerdict(
            allowed=allowed,
            cap_usd=self.cap_usd,
            spent_usd=spent,
            reserved_usd=reserved,
            remaining_usd=(
                remaining
                if remaining is None
                else max(0.0, remaining - (reserved if allowed else 0.0))
            ),
            price_state=price_state,
            model=model,
            reason=reason,
        )
        if self.receipt_max_calls:
            self._counters.receipt_log.append(verdict.as_dict())
            del self._counters.receipt_log[: -self.receipt_max_calls]
        return verdict

    def _publish(self, verdict: Optional[BudgetVerdict] = None) -> None:
        if self.on_receipt is None:
            return
        try:
            self.on_receipt(self.report())
        except Exception:
            # Observability must never change a task outcome.
            pass

    def _emit_exemption(self, marker: Optional[Dict[str, Any]]) -> None:
        if self.on_exemption is None:
            return
        try:
            self.on_exemption(marker)
        except Exception:
            pass

    def _event(self, name: str, payload: Dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, payload)
        except Exception:
            pass


# -- the governed dial loop ------------------------------------------------
def governed_completion(
    dial: Callable[[], Any],
    *,
    max_retries: int = 4,
    base_backoff_s: float = 15.0,
    cap_backoff_s: float = MAX_BACKOFF_S,
    multiplier: float = 2.0,
    governor: Optional[BudgetGovernor] = None,
    sleep: Optional[Callable[[float], None]] = None,
    on_attempt_failure: Optional[Callable[[int, float, BaseException], None]] = None,
    idempotent: bool = True,
    transient_backoff_s: float = TRANSIENT_BACKOFF_S,
    max_transient_attempts: int = MAX_TRANSIENT_ATTEMPTS,
    started: Optional[float] = None,
) -> Any:
    """Dial with the ONE bounded retry/backoff policy, quota-aware.

    Assumes ``dial`` performs exactly one provider attempt and raises the
    provider's own exception on failure. Replaces
    ``runtime.model_router._completion_with_retry`` (whose quota-as-rate-
    limit behaviour this exists to correct) with three distinct actions:

    * **quota exhausted** -> raise :class:`QuotaExhausted` on the FIRST
      attempt. Zero retries, zero backoff, zero failover. A billing wall is
      not a transient condition and retrying it only spends what is left.
    * **rate limited** -> arm the supervision exemption for exactly the
      wait that is about to be slept (:meth:`BudgetGovernor.begin_backoff`),
      then sleep it. This is the fix for a backoff outliving the hang
      window: the watchdog is told, from the same number, that the worker
      is inside a provider backoff.
    * **transient** -> a short fixed wait, bounded by
      ``max_transient_attempts``.

    A non-idempotent call is dialled exactly once: replaying a request that
    creates provider-side state costs twice. ``on_attempt_failure`` is
    called for EVERY failed provider attempt before any sleep, so a caller
    can record it. The caller owns releasing any budget reservation it took.
    """
    sleeper = sleep or (governor.sleep if governor is not None else time.sleep)
    base = started if started is not None else time.monotonic()
    notify = on_attempt_failure or (lambda _attempt, _elapsed, _exc: None)
    rate_limit_attempts = 0
    transient_attempts = 0
    provider_attempts = 0
    if idempotent:
        retries = max(0, min(int(max_retries), 20))
        transient_budget = max(0, min(int(max_transient_attempts), 20))
    else:
        retries = 0
        transient_budget = 0
    while True:
        try:
            return dial()
        except (KeyboardInterrupt, SystemExit):
            raise
        except QuotaExhausted as already:
            # A governor upstream already classified it. Still terminal and
            # still never retried; recorded as an attempt like any other.
            provider_attempts += 1
            notify(provider_attempts, time.monotonic() - base, already)
            raise
        except Exception as exc:
            provider_attempts += 1
            notify(provider_attempts, time.monotonic() - base, exc)
            failure = classify_provider_failure(exc)
            if failure.kind == QUOTA_EXHAUSTED:
                if governor is not None:
                    governor.note_quota(failure)
                raise QuotaExhausted(failure) from exc
            if not failure.retryable:
                raise
            if failure.kind == RATE_LIMITED:
                if rate_limit_attempts >= retries:
                    raise
                rate_limit_attempts += 1
                if governor is not None:
                    window = governor.begin_backoff(rate_limit_attempts)
                    governor.sleep_in_backoff(window)
                else:
                    sleeper(
                        backoff_seconds(
                            rate_limit_attempts,
                            base_s=base_backoff_s,
                            cap_s=cap_backoff_s,
                            multiplier=multiplier,
                        )
                    )
                continue
            if transient_attempts < transient_budget:
                transient_attempts += 1
                sleeper(max(0.0, float(transient_backoff_s)))
                continue
            raise


# -- process-local install -------------------------------------------------
_GOVERNOR: contextvars.ContextVar[Optional["BudgetGovernor"]] = contextvars.ContextVar(
    "neo_budget_governor", default=None
)


def install_governor(
    governor: Optional["BudgetGovernor"],
) -> Optional["BudgetGovernor"]:
    """Install ``governor`` for the current execution context.

    Assumes concurrent tasks each install their own before dialling (the
    worker does this once per process). Passing ``None`` clears only the
    current context, matching ``model_router.set_call_context``.
    """
    _GOVERNOR.set(governor)
    return governor


def current_governor() -> Optional["BudgetGovernor"]:
    """Return the governor for the current context, or ``None``."""
    return _GOVERNOR.get()


def clear_governor() -> None:
    """Clear the current context's governor."""
    _GOVERNOR.set(None)


def write_budget_receipt(
    path: str | Path, governor: BudgetGovernor
) -> Optional[Dict[str, Any]]:
    """Atomically write the live budget receipt to ``path``.

    Assumes the parent directory exists or is creatable. Returns the
    receipt that was written, or ``None`` when the write failed -- an
    unreadable receipt is reported, never raised, because observability
    must not fail a run.
    """
    receipt = governor.report()
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, receipt)
    except (OSError, TypeError, ValueError):
        return None
    return receipt
