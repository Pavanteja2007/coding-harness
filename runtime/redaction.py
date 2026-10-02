"""The ONE runtime boundary where provider or network text becomes a product value.

**This module exists because ``runtime/**`` is the layer that touches the
network and spends money.** A provider error body is the single most likely
real-world path for a credential to reach a log: a gateway answers a bad key
with a body that echoes the key prefix, that body becomes an exception
string, and the string reaches a ledger row, a journal row, a checkpoint, and
the TUI. Downstream redaction cannot make the on-disk artifact trustworthy if
the payload that created it was unsafe, so redaction happens HERE, at the
boundary, before the value is stored.

Four rules, in the order they are applied. The order is the design:

1. **Strip ANSI escapes FIRST.** An escape can split a secret into
   visually-contiguous bytes: ``sk-abc\\x1b[0mdef`` matches no credential
   pattern, and a strip that runs *after* redaction reassembles the
   credential from its own fragments. This is the ordering the engineering
   doctrine names, and ``cli/ui.py::strip_ansi`` is the shipped example of
   the wrong order.
2. **Redact through ``shared.security``.** This module implements NO pattern
   of its own. It is a boundary, not a second redactor: two redactors is two
   answers to "what is a secret".
3. **Cap the length.** A multi-KB HTML error page is its own denial of
   service. The cap is applied LAST, after redaction, so a truncation can
   never be what hides a secret's tail.
4. **Fail CLOSED.** If redaction is unavailable or raises, the value is
   REPLACED with ``(withheld: <reason>)``. Never the raw value, and never
   ``None`` -- ``None`` in a receipt reads as "no key was involved", which
   is the opposite of the truth.

**The class is never redacted.** ``classify_provider_failure`` owns *what
happened* (auth / rate-limit / quota / bad request / provider fault /
unknown) and ``provider_resilience.classify_retry`` owns *what to do next*.
Those are vocabulary, not provider text, and a withheld detail with no class
is not an affordance. Every call site therefore classifies FIRST and redacts
the detail SECOND; :func:`provider_failure_detail` is the seam that keeps
that order honest by refusing to accept a detail without the classification
it belongs to.

Invariants a caller gets, stated so a test can pin them:

* no provider exception text reaches a product value unredacted;
* a withheld detail ALWAYS carries the class beside it;
* an absent value is never rendered as ``None`` or ``""``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from shared import security

__all__ = [
    "ANSI_ESCAPE_RE",
    "DEFAULT_DETAIL_CHARS",
    "DETAIL_TRUNCATION_NOTE",
    "WITHHELD_PREFIX",
    "class_always_present",
    "detail_receipt",
    "provider_failure_detail",
    "redact_payload",
    "redact_provider_text",
    "safe_secret_values",
    "strip_ansi",
]

#: Ceiling on any provider-derived text that becomes a stored value. Chosen to
#: fit a JSONL row and a terminal card, not to be "generous". The doctrine
#: calls out an unbounded error body as a denial of service in its own right.
DEFAULT_DETAIL_CHARS = 500

#: Appended when a value is cut. A bounded value must say it was bounded --
#: silently showing a prefix is how a receipt stops matching reality.
DETAIL_TRUNCATION_NOTE = " [truncated]"

#: What a withheld value looks like. Deliberately NOT ``None`` and NOT ``""``:
#: both read as "nothing was here", and something WAS here -- we just could
#: not prove it was safe.
WITHHELD_PREFIX = "(withheld:"

#: CSI/OSC/other ANSI escape sequences. Removed before redaction so a secret
#: cannot hide inside one. Compiled once; this runs on every provider error.
ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]          # CSI ... final byte
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?  # OSC ... BEL or ST
    | \x1b[@-Z\\-_]                   # two-character escapes
    | [\x00-\x08\x0b\x0c\x0e-\x1f]     # bare C0 controls (keep \t \n \r)
    """,
    re.VERBOSE,
)


def strip_ansi(value: Any) -> str:
    """Return ``value`` as text with ANSI escapes removed.

    Runs BEFORE redaction, never after. Accepts any value so it is safe to
    call on a field whose type is not known at the call site.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return ANSI_ESCAPE_RE.sub("", text)


def safe_secret_values(secrets: Optional[Iterable[Any]]) -> tuple[str, ...]:
    """Normalise a ``secrets`` argument into the tuple the redactor accepts.

    Blank and non-string entries are dropped rather than stringified: a
    ``None`` in the secrets list would otherwise redact the literal text
    "None" out of every message, which is a redactor that lies about what it
    found. Order is preserved and duplicates are collapsed so the longest-known
    secret is not masked by a shorter one that shares its prefix.
    """
    if not secrets:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for secret in secrets:
        if secret is None:
            continue
        text = secret if isinstance(secret, str) else str(secret)
        text = text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    # Longest first: a short secret that is a prefix of a longer one must not
    # consume the longer one's head and leave a readable tail behind.
    out.sort(key=len, reverse=True)
    return tuple(out)


def redact_provider_text(
    value: Any,
    *,
    secrets: Optional[Iterable[Any]] = (),
    limit: int = DEFAULT_DETAIL_CHARS,
    label: str = "provider detail",
) -> str:
    """Return provider text that is safe to store, display, or publish.

    Assumes ``value`` came from a provider, a gateway, or a network error
    body -- i.e. text this process did not author and cannot trust to omit
    its own credentials. Applies, in this order: strip escapes, redact,
    cap.

    **Never returns ``None`` and never returns the input unchanged on
    failure.** If ``shared.security`` is unavailable or raises, the result is
    ``(withheld: <reason>)``: a withheld detail is honest, a leaked one is
    not.
    """
    if value is None:
        return f"{WITHHELD_PREFIX} {label} was absent)"
    text = strip_ansi(value)
    if not text.strip():
        return f"{WITHHELD_PREFIX} {label} was empty)"

    known = safe_secret_values(secrets)
    try:
        text = security.redact_text(text, secrets=known)
    except Exception as exc:  # fail closed: a broken redactor must not disclose
        return (
            f"{WITHHELD_PREFIX} redaction unavailable for {label}: "
            f"{type(exc).__name__})"
        )

    # Strip again: a redactor is allowed to introduce its own placeholder, and
    # a placeholder must not carry escapes into a terminal.
    text = strip_ansi(text)

    cap = DEFAULT_DETAIL_CHARS if limit is None else max(0, int(limit))
    if cap and len(text) > cap:
        text = text[:cap].rstrip() + DETAIL_TRUNCATION_NOTE
    return text


def redact_payload(value: Any, *, secrets: Optional[Iterable[Any]] = ()) -> Any:
    """Return a deep-redacted copy of a receipt/journal payload.

    Delegates the recursion to ``shared.security`` so this module never
    becomes the second answer to "which key names a credential". Keys that
    name a credential are masked by the key; strings that merely CONTAIN one
    are masked by shape. ``limit`` is deliberately not applied here: a payload
    is a structured record, and a per-field cap on it would make a receipt
    disagree with the value it describes. Use :func:`redact_provider_text` for
    free text.
    """
    known = safe_secret_values(secrets)
    try:
        return security.redact_secrets(value, secrets=known)
    except Exception as exc:  # fail closed, and say why in the artifact itself
        return {
            WITHHELD_PREFIX.strip("("): f"redaction unavailable: {type(exc).__name__}"
        }


def class_always_present(failure: Any) -> bool:
    """Whether ``failure`` carries a non-empty classification.

    The cheap assertion a call site can make without importing this module's
    vocabulary: a withheld detail is only an affordance if the class beside
    it survives. A record with an empty kind is one this project should never
    publish.
    """
    if failure is None:
        return False
    kind = getattr(failure, "kind", None)
    if kind is None and isinstance(failure, dict):
        kind = failure.get("kind")
    return bool(str(kind or "").strip())


def detail_receipt(
    *,
    kind: str,
    reason: str,
    detail: str,
    status_code: Optional[int] = None,
    billing_url: Optional[str] = None,
    detail_withheld: bool = False,
    redacted: bool = False,
    truncated: bool = False,
) -> dict:
    """Build the JSON-safe receipt for one classified provider failure.

    Every flag is a FACT about what happened to the detail, so a reader can
    tell "the provider said nothing" from "we redacted it" from "we cut it"
    -- three outcomes that used to be the same empty string.

    ``kind`` and ``reason`` are passed in rather than derived here: they are
    owned by :mod:`runtime.budget_governor` and
    :mod:`runtime.provider_resilience`, and this module is not a second
    classifier.
    """
    return {
        "kind": str(kind or "unknown_failure"),
        "reason": str(reason or "unclassified failure"),
        "detail": str(detail or ""),
        "detail_withheld": bool(detail_withheld),
        "detail_redacted": bool(redacted),
        "detail_truncated": bool(truncated),
        "status_code": status_code,
        "billing_url": billing_url,
    }


def provider_failure_detail(
    exc: BaseException,
    *,
    kind: str,
    secrets: Optional[Iterable[Any]] = (),
    status_code: Optional[int] = None,
    provider: str = "",
    limit: int = DEFAULT_DETAIL_CHARS,
) -> dict:
    """Classify-and-redact the ONE seam for a provider failure's detail.

    Call this instead of ``str(exc)[:500]``. It exists to make the ordering
    impossible to get wrong: the classification is computed here and
    ``kind``/``status_code``/``provider`` are REQUIRED keyword arguments, so
    a caller cannot reach for this helper and then lose the class.

    The returned dict is the full honest receipt -- ``kind``, ``reason``,
    the redacted detail, and the three flags describing what happened to the
    detail. ``reason`` is composed from the classification rather than from
    the provider's own text, so it survives any redaction.
    """
    from runtime import budget_governor

    # CLASSIFY FIRST. ``classify_provider_failure`` reads the exception's own
    # text, so it must run before the text is shortened or redacted -- the
    # markers it matches on live in the body.
    failure = budget_governor.classify_provider_failure(exc)

    raw = str(exc) if exc is not None else ""
    detail = redact_provider_text(
        raw, secrets=secrets, limit=limit, label="provider error detail"
    )
    withheld = detail.startswith(WITHHELD_PREFIX)
    receipt = detail_receipt(
        kind=failure.kind,
        reason=failure.reason,
        detail=detail,
        status_code=status_code if status_code is not None else failure.status_code,
        billing_url=failure.billing_url,
        detail_withheld=withheld,
        redacted=(not withheld) and detail != strip_ansi(raw),
        truncated=DETAIL_TRUNCATION_NOTE in detail,
    )
    return receipt
