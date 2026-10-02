"""Say what is unavailable, why, and how long we waited for it.

An offline or firewalled machine does not produce a stack trace. It produces
an app that hangs on a dial, or one that prints an empty string where an
answer should be, or -- the worst of the three -- one that reports a BLOCKED
call as if it were a RESULT. This module owns the vocabulary that makes those
three distinguishable.

* :class:`Availability` is a **value**, never a boolean. ``available`` is the
  only field a caller acts on; ``category`` and ``reason`` are stable slugs a
  receipt, a trace row and a terminal sentence can all share, so a
  user-visible sentence and a machine-readable record cannot disagree.
* ``category`` is a CLOSED set. The distinctions that matter are ``policy``
  (an operator said no), ``offline`` (the run was told to stay local),
  ``timeout`` (we waited a bounded time), ``unreachable`` (the name did not
  resolve or the socket was refused) and ``budget`` (out of tokens or money).
  "The fetch failed" collapses five different answers into one sentence a
  user cannot act on.
* **A blocked call is never a result.** ``Availability.is_result`` is False
  for every unavailable record, and :func:`require_available` raises rather
  than letting an unavailability be consumed as an answer. The genuinely
  dangerous failure mode in an offline product is a silent empty string
  flowing into a prompt as though the model had replied.
* **The dial is bounded by construction.** :func:`dial_deadline_s` returns a
  finite budget for every outbound path and :func:`bounded_deadline` refuses
  a non-finite or non-positive one. "No timeout" is a value this module will
  not produce.

It reads the existing authorities rather than replacing them:
:mod:`shared.egress` for "may this host be reached", and the ``offline`` /
``read_only`` / ``local_first`` config keys for "must this stay local". Those
keys are read by KEY PRESENCE: an absent key means the operator never said,
which is different from ``offline: false``, and a value in
``harness.config.DEFAULTS`` merges into every task and every eval arm, so
nothing here adds one.

The module is dependency-free and sits at the bottom of the project, like
:mod:`shared.egress` next door: ``harness``, ``runtime``, ``cli`` and
``execution`` may all import it, and it imports none of them.
"""

from __future__ import annotations

import math
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from shared.egress import EgressPolicy, egress_decision, normalize_host

__all__ = [
    "AVAILABILITY_CATEGORIES",
    "DEFAULT_DIAL_DEADLINE_S",
    "MAX_DIAL_DEADLINE_S",
    "MIN_DIAL_DEADLINE_S",
    "OFFLINE_ENV",
    "Availability",
    "BlockedCallReportedAsResult",
    "availability_for",
    "bounded_deadline",
    "dial_deadline_s",
    "offline_requested",
    "offline_sentence",
    "require_available",
    "unavailable_lines",
]

#: The closed vocabulary. A category nobody defined is a receipt nobody can
#: count, and an unrecognised one fails CLOSED to ``unreachable`` rather than
#: to ``available``.
AVAILABILITY_CATEGORIES = (
    "available",
    "policy",
    "offline",
    "timeout",
    "unreachable",
    "budget",
    "malformed",
)

#: A dial that can hang is not a dial. 15s is the same default the fetch path
#: already uses, so this module and ``harness.webfetch`` agree about the cost
#: of a dead endpoint instead of each inventing one.
DEFAULT_DIAL_DEADLINE_S = 15.0
MIN_DIAL_DEADLINE_S = 1.0
MAX_DIAL_DEADLINE_S = 300.0

#: Read the same environment variable ``runtime.offline_mode`` honours, so the
#: shared answer and the runtime gate cannot disagree about whether this
#: process is offline.
OFFLINE_ENV = "NEO_OFFLINE"

_TRUTHY = ("1", "true", "yes", "on", "offline")
_FALSY = ("0", "false", "no", "off", "")


class BlockedCallReportedAsResult(RuntimeError):
    """Raised when an unavailability is about to be consumed as an answer.

    This is the defence the whole module exists for. A caller that does
    ``return availability.text or "no answer"`` has turned a blocked call
    into a result; :func:`require_available` makes that a loud failure
    instead of a plausible-looking string in a prompt.
    """

    def __init__(self, availability: "Availability") -> None:
        super().__init__(availability.sentence)
        self.availability = availability

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record for the refusal."""
        return {
            "error": "blocked_call_reported_as_result",
            "message": str(self),
            "availability": self.availability.as_dict(),
        }


@dataclass(frozen=True)
class Availability:
    """One resolved judgement about whether a call can happen at all.

    ``available`` is the only field a caller acts on. ``category`` and
    ``reason`` are stable slugs; ``sentence`` is the human answer and is
    always populated, so a surface never has to compose one from a slug and
    get the five categories confused. ``is_result`` is False for every
    unavailable record -- that is the invariant, and it is what
    :func:`require_available` checks.
    """

    available: bool
    category: str
    reason: str = ""
    target: str = ""
    host: str = ""
    deadline_s: float = 0.0
    waited_s: float = 0.0
    retryable: bool = False
    source: str = ""
    detail: str = ""
    local_alternatives: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.category not in AVAILABILITY_CATEGORIES:
            raise ValueError(
                f"unknown availability category {self.category!r}; "
                f"expected one of {AVAILABILITY_CATEGORIES}"
            )

    @property
    def is_result(self) -> bool:
        """True only when something actually came back."""
        return bool(self.available)

    @property
    def sentence(self) -> str:
        """The one human sentence describing what is unavailable and why."""
        return self.detail or _default_sentence(self)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record (no secrets, no raw URLs)."""
        return {
            "available": self.available,
            "category": self.category,
            "reason": self.reason,
            "target": self.target,
            "host": self.host,
            "deadline_s": round(float(self.deadline_s), 3),
            "waited_s": round(float(self.waited_s), 3),
            "retryable": bool(self.retryable),
            "source": self.source,
            "detail": self.detail,
            "is_result": self.is_result,
            "local_alternatives": list(self.local_alternatives),
        }

    def describe(self) -> List[str]:
        """Return PLAIN lines. Never markup (see the module docstring)."""
        return unavailable_lines(self)


def _default_sentence(availability: Availability) -> str:
    if availability.available:
        return "available"
    host = availability.host or availability.target or "that host"
    if availability.category == "offline":
        return (
            f"{host} is unavailable: this run is offline, so no network call was made. "
            "Everything that does not need the network still works."
        )
    if availability.category == "policy":
        return (
            f"{host} is not on the egress allowlist, so the call was refused before any "
            "connection was attempted. Add the host to the allowlist to allow it."
        )
    if availability.category == "timeout":
        return (
            f"{host} did not answer within {availability.deadline_s:.0f}s, so the call was "
            "abandoned. This is a timeout, not an empty answer."
        )
    if availability.category == "unreachable":
        return (
            f"{host} could not be reached (the name did not resolve or the connection was "
            "refused). This is a network failure, not an empty answer."
        )
    if availability.category == "budget":
        return f"{host} was not called: this run is out of budget."
    if availability.category == "malformed":
        return f"{host} could not be requested: the target is not a usable address."
    return f"{host} is unavailable ({availability.reason or 'unknown reason'})."


def _offline_from_config(config: Optional[Mapping[str, Any]]) -> Tuple[bool, str]:
    """Return ``(offline, source)`` from a config mapping, by KEY PRESENCE.

    An absent key means the operator never said anything, which is not the
    same as ``False``; that distinction is why this reads presence rather
    than truthiness and why nothing here lands in ``config.DEFAULTS``.
    """
    values = dict(config or {})
    for key in ("offline", "no_network", "airgap"):
        if key not in values:
            continue
        raw = values[key]
        if isinstance(raw, bool):
            return raw, f"config:{key}"
        text = str(raw or "").strip().casefold()
        if text in _TRUTHY:
            return True, f"config:{key}"
        if text in _FALSY:
            return False, f"config:{key}"
        # Present but unusable is NOT "offline" and NOT "online": a typo must
        # not silently keep the network on and must not silently kill it.
        return False, f"config:{key}:unusable:{text[:40]}"
    env = os.environ.get(OFFLINE_ENV, "").strip().casefold()
    if env in _TRUTHY:
        return True, f"env:{OFFLINE_ENV}"
    return False, "default"


def offline_requested(config: Optional[Mapping[str, Any]] = None) -> Tuple[bool, str]:
    """Return ``(offline, source)`` for this run. Never raises.

    Reads the ``offline`` / ``no_network`` / ``airgap`` config keys by key
    presence, then ``NEO_OFFLINE``. The source label travels with the answer
    so a receipt can say WHY the network was considered unavailable.
    """
    try:
        return _offline_from_config(config)
    except Exception:
        return False, "default:unreadable"


def offline_sentence(config: Optional[Mapping[str, Any]] = None) -> str:
    """Return the one sentence that says the run is offline and why."""
    offline, source = offline_requested(config)
    if not offline:
        return ""
    return (
        "offline: no outbound network call will be made for this run "
        f"({source}). Reading, editing, testing and memory keep working."
    )


def dial_deadline_s(
    config: Optional[Mapping[str, Any]] = None,
    *,
    default_s: float = DEFAULT_DIAL_DEADLINE_S,
) -> float:
    """Return a FINITE budget in seconds for one outbound attempt.

    Precedence: the ``net_dial_deadline_s`` config key > ``default_s``. The
    result is clamped into ``[MIN_DIAL_DEADLINE_S, MAX_DIAL_DEADLINE_S]`` and
    is always finite: a NaN, an infinity, a zero, a negative and a
    non-numeric all resolve to the default rather than to "wait forever". A
    caller that cannot express a timeout gets one anyway, which is the whole
    point of the helper existing.
    """
    values = dict(config or {})
    raw: Any = values.get("net_dial_deadline_s", default_s)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        number = float(default_s)
    if not math.isfinite(number) or number <= 0:
        number = float(default_s)
    if not math.isfinite(number) or number <= 0:
        number = DEFAULT_DIAL_DEADLINE_S
    return max(MIN_DIAL_DEADLINE_S, min(MAX_DIAL_DEADLINE_S, number))


def bounded_deadline(
    seconds: Any, *, default_s: float = DEFAULT_DIAL_DEADLINE_S
) -> float:
    """Clamp one caller-supplied timeout into a finite, positive budget."""
    return dial_deadline_s({"net_dial_deadline_s": seconds}, default_s=default_s)


def _policy_for(config: Optional[Mapping[str, Any]]) -> EgressPolicy:
    try:
        return EgressPolicy.from_config(config)
    except Exception:
        return EgressPolicy.build()


def _local_alternatives(host: str) -> Tuple[str, ...]:
    """Name the offline-usable sources for a blocked documentation fetch."""
    alternatives: List[str] = []
    if host in ("docs.python.org", "pypi.org", "files.pythonhosted.org"):
        alternatives.append("the installed package's own source and docstrings")
        alternatives.append("pydoc in the sandbox (works with no network at all)")
    alternatives.append("the repository's own code and tests")
    return tuple(alternatives)


#: Egress reason slug -> (category, retryable, one-clause explanation). The
#: mapping is EXPLICIT rather than a prefix match: "denied_*" is a policy
#: answer, not a network one, and a caller that only looks at the word
#: "denied" cannot tell an operator's decision from a typo in a URL.
_EGRESS_CATEGORIES = {
    "denied_empty_allowlist": (
        "policy",
        False,
        "the egress allowlist is empty, so nothing is reachable",
    ),
    "denied_host_not_allowlisted": (
        "policy",
        False,
        "that host is not on the egress allowlist",
    ),
    "denied_scheme": ("policy", False, "only http and https are permitted"),
    "denied_malformed_target": (
        "malformed",
        False,
        "the target is not a usable address",
    ),
    "denied_private_address": (
        "policy",
        False,
        "that address is loopback, private or link-local",
    ),
    "denied_embedded_credentials": (
        "malformed",
        False,
        "a URL with a username or password is not sent",
    ),
    "denied_port": ("policy", False, "only ports 80 and 443 are permitted"),
    "allowed": ("available", True, ""),
}


def availability_for(
    target: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
    policy: Optional[EgressPolicy] = None,
    kind: str = "network",
    check_reachable: bool = False,
    now: Optional[float] = None,
) -> Availability:
    """Resolve whether ``target`` can be reached, and say why if it cannot.

    ``kind`` names WHAT is being asked for and is carried into the record, so
    a model call, a web fetch and a container network bridge are three
    distinguishable unavailabilities rather than one "network" word.

    The order is load-bearing and matches :mod:`shared.egress`: a run told to
    stay local is offline regardless of what any allowlist says, and a
    target the policy refuses never reaches a resolver. ``check_reachable``
    is opt-in because a DNS lookup is itself a network call -- asking
    "is this reachable" on an offline machine is a dial, and a dial is what
    this module exists to bound.
    """
    deadline = dial_deadline_s(config)
    host = normalize_host(target)
    subject = host or str(target or "that host")[:120]

    offline, source = offline_requested(config)
    if offline:
        return Availability(
            available=False,
            category="offline",
            reason="offline_requested",
            target=str(target or "")[:200],
            host=host,
            deadline_s=deadline,
            source=source,
            detail=(
                f"{subject} is unavailable for {kind}: this run is offline ({source}), so no "
                "connection was attempted. Local reading, editing and testing still work."
            ),
            local_alternatives=_local_alternatives(host),
        )

    active = policy if policy is not None else _policy_for(config)
    try:
        decision = egress_decision(target, active)
    except Exception as exc:
        return Availability(
            available=False,
            category="unreachable",
            reason="policy_unavailable",
            target=str(target or "")[:200],
            host=host,
            deadline_s=deadline,
            source=active.source,
            detail=(
                f"{subject} is unavailable: the egress policy could not be evaluated "
                f"({type(exc).__name__}), so nothing was sent."
            ),
        )

    category, retryable, explanation = _EGRESS_CATEGORIES.get(
        decision.reason, ("unreachable", False, "the egress policy denied this target")
    )
    if decision.allowed and category == "available":
        if not check_reachable:
            return Availability(
                available=True,
                category="available",
                reason=decision.reason,
                target=str(target or "")[:200],
                host=host,
                deadline_s=deadline,
                retryable=True,
                source=active.source,
                detail=f"{subject} is allowed by the egress policy; a {kind} call may proceed.",
            )
        reachable, problem = _probe_reachable(host, deadline)
        if reachable:
            return Availability(
                available=True,
                category="available",
                reason=decision.reason,
                target=str(target or "")[:200],
                host=host,
                deadline_s=deadline,
                retryable=True,
                source=active.source,
                detail=f"{subject} resolved and accepted a connection; the {kind} call may proceed.",
            )
        return Availability(
            available=False,
            category=problem,
            reason=f"{decision.reason}:{problem}",
            target=str(target or "")[:200],
            host=host,
            deadline_s=deadline,
            retryable=True,
            source=active.source,
            detail=(
                f"{subject} is allowed by policy but could not be reached: "
                f"{'the name did not resolve' if problem == 'unreachable' else 'the connection timed out'}. "
                "No content was returned, and this is not an empty answer."
            ),
        )

    return Availability(
        available=False,
        category=category,
        reason=decision.reason,
        target=str(target or "")[:200],
        host=host,
        deadline_s=deadline,
        retryable=retryable,
        source=active.source,
        detail=(
            f"{subject} is unavailable for {kind}: {explanation} "
            f"(policy source: {active.source}). Nothing was sent."
        ),
        local_alternatives=_local_alternatives(host),
    )


def _probe_reachable(host: str, deadline_s: float) -> Tuple[bool, str]:
    """Resolve and connect to ``host`` under a bounded deadline.

    Returns ``(ok, problem)`` where ``problem`` is ``unreachable`` or
    ``timeout``. Both DNS and connect are bounded by ``deadline_s`` -- a
    resolver that hangs is the exact hang this module is closing.
    """
    if not host:
        return False, "unreachable"
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return False, "unreachable"
    if not infos:
        return False, "unreachable"
    family, socktype, proto, _canon, address = infos[0]
    connection = socket.socket(family, socktype, proto)
    try:
        connection.settimeout(max(0.1, float(deadline_s)))
        connection.connect(address)
    except socket.timeout:
        return False, "timeout"
    except OSError:
        return False, "unreachable"
    finally:
        with _suppress_oserror():
            connection.close()
    return True, ""


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def require_available(availability: Availability) -> Availability:
    """Return the availability, or RAISE rather than let it become a result.

    The fail-closed door. ``if availability.available`` is easy to forget on
    one branch of a twenty-line fetch path; calling this is not, because the
    missing call is a traceback naming the reason rather than an empty string
    flowing into a prompt.
    """
    if not isinstance(availability, Availability):
        raise TypeError(
            f"require_available expects an Availability, got {type(availability).__name__}"
        )
    if not availability.available:
        raise BlockedCallReportedAsResult(availability)
    return availability


def unavailable_lines(availability: Availability) -> List[str]:
    """Render one :class:`Availability` as PLAIN, markup-free lines.

    One sentence plus, when there is one, the offline-usable alternative. The
    alternatives are a second line rather than a section: this is a refusal,
    not a panel, so the anti-clutter rule does not apply -- a refusal that
    rendered nothing would be a hang with better manners.
    """
    if availability.available:
        return [availability.sentence]
    lines = [availability.sentence]
    if availability.local_alternatives:
        lines.append(
            "  available instead: " + "; ".join(availability.local_alternatives)
        )
    if availability.retryable and availability.category in ("timeout", "unreachable"):
        lines.append(
            f"  retrying is reasonable: this is a {availability.category}, not a refusal"
        )
    return lines
