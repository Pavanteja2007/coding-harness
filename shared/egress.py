"""Deny-by-default egress policy for every outbound network path.

This module is the single authority for "may this process open a connection
to this host?". It is intentionally dependency-free and sits at the bottom of
the project (like :mod:`shared.security`): the web fetcher, the sandbox, the
automation webhook ingress, and any future connector all resolve their answer
here instead of keeping private blocklists.

Design rules (Prompt 13, "Egress and sandbox"):

* **Deny by default.** An empty allowlist denies *every* host. There is no
  implicit "anything not obviously private is fine" mode, because that is the
  exact blocklist failure the ceiling prompt calls out.
* **Explicit allowlist.** Only exact hosts and ``*.suffix`` wildcards that an
  operator wrote down are reachable. The shipped default is a deliberately
  tiny, documented set: the Python documentation/PyPI endpoints the FETCH
  workflow exists to reach, plus the RFC 2606 reserved ``example.com``.
* **Blocklist still applies first.** A host on the allowlist that resolves to
  loopback/private/link-local space is still denied; the allowlist can never
  re-open a blocked address.
* **Every decision is a value, not a boolean.** The reason slug is stable and
  safe to record in traces and receipts, so a denied egress is auditable
  without exposing the URL's secret-bearing parts.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "EGRESS_ENV_ALLOWLIST",
    "EgressDecision",
    "EgressPolicy",
    "egress_decision",
    "load_egress_policy",
    "normalize_host",
    "save_egress_policy",
]

# Hosts the product actually needs for its documented read-only workflows:
#   * pypi.org / files.pythonhosted.org — package metadata and documentation
#     (the FETCH/DOCS lookup path, covered by the eval scenario and the
#     num2words docs test);
#   * docs.python.org — the canonical stdlib/library reference;
#   * example.com — RFC 2606 reserved name that can never resolve to a real
#     endpoint; used by the harness's own fixtures and offline tests.
# Anything else is denied until an operator adds it.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "docs.python.org",
    "example.com",
)

# Operators can extend the allowlist through the environment without editing
# code. The value is a comma/space separated host list using the same
# ``*.suffix`` wildcard syntax as :class:`EgressPolicy`.
EGRESS_ENV_ALLOWLIST = "NEO_EGRESS_ALLOWED_HOSTS"

# Stable reason slugs. These are recorded in traces/receipts.
_REASON_ALLOWED = "allowed"
_REASON_EMPTY_ALLOWLIST = "denied_empty_allowlist"
_REASON_NOT_ALLOWED = "denied_host_not_allowlisted"
_REASON_BAD_SCHEME = "denied_scheme"
_REASON_MALFORMED = "denied_malformed_target"
_REASON_PRIVATE = "denied_private_address"
_REASON_CREDENTIALS = "denied_embedded_credentials"
_REASON_PORT = "denied_port"

_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = (80, 443, "")


def normalize_host(value: Any) -> str:
    """Return a lowercase, trailing-dot-free host name ("" when unusable).

    Accepts a bare host, a ``host:port`` pair, or a full URL. Bracketed IPv6
    literals are unwrapped. This is the canonical form the allowlist and the
    decision records use, so two spellings of one host cannot diverge.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return ""
        text = parts.hostname or ""
    text = text.strip().strip(".").casefold()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if text.count(":") == 1:
        head, _, tail = text.partition(":")
        if tail.isdigit():
            text = head
    return text


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _resolved_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every address a host resolves to, or [] when unresolvable."""
    if _is_ip_literal(host):
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            return []
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError, ValueError):
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    return addresses


def _address_is_blocked(address: Any) -> bool:
    try:
        parsed = (
            address
            if isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(str(address))
        )
    except ValueError:
        return False
    return bool(
        parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_private
        or parsed.is_reserved
        or parsed.is_unspecified
        or parsed.is_multicast
    )


@dataclass(frozen=True)
class EgressDecision:
    """One resolved egress judgement.

    ``allowed`` is the only field a caller acts on. ``reason`` is a stable
    slug safe for traces; ``host`` and ``port`` are the normalized target.
    """

    allowed: bool
    reason: str
    host: str = ""
    port: str = ""
    scheme: str = ""
    policy_source: str = "default"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible decision record."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
            "policy_source": self.policy_source,
        }

    def __bool__(self) -> bool:
        return bool(self.allowed)


@dataclass(frozen=True)
class EgressPolicy:
    """A deny-by-default host allowlist with an auditable provenance label.

    ``allowed_hosts`` entries are exact host names or ``*.suffix`` wildcards.
    ``source`` records where the policy came from ("default", "config",
    "env", or a file path) so a decision can be traced back to a policy the
    operator can actually see.
    """

    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    source: str = "default"
    allow_private: bool = False

    @classmethod
    def build(
        cls,
        hosts: Optional[Iterable[Any]] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        allow_private: bool = False,
        source: str = "default",
        use_environment: bool = True,
    ) -> "EgressPolicy":
        """Return a policy from explicit hosts, the environment, or defaults.

        Precedence is explicit ``hosts`` > ``NEO_EGRESS_ALLOWED_HOSTS`` >
        :data:`DEFAULT_ALLOWED_HOSTS`. An explicitly empty ``hosts`` sequence
        is honoured (that is the deny-everything policy) and does not fall
        back to the shipped default.
        """
        resolved: tuple[str, ...]
        resolved_source = source
        if hosts is not None:
            resolved = tuple(normalize_host(item) for item in hosts)
            resolved = tuple(item for item in resolved if item)
            resolved_source = source if source != "default" else "explicit"
        else:
            env = os.environ if environ is None else environ
            raw = str(env.get(EGRESS_ENV_ALLOWLIST, "") or "")
            entries = [item for item in raw.replace(",", " ").split() if item]
            if entries:
                resolved = tuple(
                    host for host in (normalize_host(item) for item in entries) if host
                )
                resolved_source = "env"
            else:
                resolved = DEFAULT_ALLOWED_HOSTS
                resolved_source = "default"
        return cls(
            allowed_hosts=resolved,
            source=resolved_source,
            allow_private=bool(allow_private),
        )

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]] = None) -> "EgressPolicy":
        """Return the policy described by a ``Task.config``-style mapping.

        Recognised keys: ``egress_allowed_hosts`` (explicit list, may be an
        empty list meaning deny-all) and ``egress_allow_private`` (opt-in only
        for a deliberately isolated test environment).
        """
        values = dict(config or {})
        hosts = values.get("egress_allowed_hosts")
        if hosts is None:
            return cls.build(
                allow_private=bool(values.get("egress_allow_private", False))
            )
        if isinstance(hosts, str):
            hosts = [item for item in hosts.replace(",", " ").split() if item]
        return cls.build(
            hosts=list(hosts),
            allow_private=bool(values.get("egress_allow_private", False)),
            source="config",
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible policy record."""
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "source": self.source,
            "allow_private": bool(self.allow_private),
        }

    def permits(self, host: str) -> bool:
        """Return whether a normalized host is on the allowlist."""
        target = normalize_host(host)
        if not target:
            return False
        for entry in self.allowed_hosts:
            candidate = normalize_host(entry)
            if not candidate:
                continue
            if candidate.startswith("*."):
                suffix = candidate[1:]  # keeps the leading dot
                if target == candidate[2:] or target.endswith(suffix):
                    return True
                continue
            if target == candidate:
                return True
        return False


def egress_decision(
    target: Any,
    policy: Optional[EgressPolicy] = None,
    *,
    scheme: str = "",
    port: Union[int, str, None] = None,
) -> EgressDecision:
    """Resolve one outbound target against the deny-by-default policy.

    ``target`` may be a full URL or a bare host. Assumes the caller wants a
    value, never an exception: a malformed or denied target is a decision with
    a reason, so callers can surface it without inventing a new error type.

    Order of checks is load-bearing: scheme, embedded credentials, port, then
    the SSRF blocklist (which runs even for allowlisted hosts), then the
    allowlist itself. An allowlist entry can therefore never re-open a private
    or loopback address.
    """
    active = policy or EgressPolicy.build()
    text = str(target or "").strip()
    resolved_scheme = str(scheme or "").casefold()
    resolved_port = "" if port is None else str(port)
    host = ""

    if text:
        if "://" in text:
            try:
                parts = urlsplit(text)
            except ValueError:
                return EgressDecision(
                    False, _REASON_MALFORMED, policy_source=active.source
                )
            if parts.username or parts.password:
                return EgressDecision(
                    False, _REASON_CREDENTIALS, policy_source=active.source
                )
            resolved_scheme = resolved_scheme or parts.scheme.casefold()
            if parts.port:
                resolved_port = resolved_port or str(parts.port)
            host = normalize_host(parts.hostname or "")
        else:
            host = normalize_host(text)
            if not resolved_port and text.count(":") == 1:
                _, _, tail = text.partition(":")
                if tail.isdigit():
                    resolved_port = tail

    if not host:
        return EgressDecision(False, _REASON_MALFORMED, policy_source=active.source)
    if not resolved_scheme:
        return EgressDecision(
            False, _REASON_MALFORMED, host, resolved_port, "", active.source
        )
    if resolved_scheme not in _ALLOWED_SCHEMES:
        return EgressDecision(
            False,
            _REASON_BAD_SCHEME,
            host,
            resolved_port,
            resolved_scheme,
            active.source,
        )
    if resolved_port and resolved_port not in [str(item) for item in _ALLOWED_PORTS]:
        return EgressDecision(
            False, _REASON_PORT, host, resolved_port, resolved_scheme, active.source
        )

    if not active.allow_private:
        for address in _resolved_addresses(host):
            if _address_is_blocked(address):
                return EgressDecision(
                    False,
                    _REASON_PRIVATE,
                    host,
                    resolved_port,
                    resolved_scheme,
                    active.source,
                )

    if not active.allowed_hosts:
        return EgressDecision(
            False,
            _REASON_EMPTY_ALLOWLIST,
            host,
            resolved_port,
            resolved_scheme,
            active.source,
        )
    if not active.permits(host):
        return EgressDecision(
            False,
            _REASON_NOT_ALLOWED,
            host,
            resolved_port,
            resolved_scheme,
            active.source,
        )
    return EgressDecision(
        True, _REASON_ALLOWED, host, resolved_port, resolved_scheme, active.source
    )


def load_egress_policy(path: Union[str, os.PathLike[str]]) -> EgressPolicy:
    """Load a policy from a JSON file written by :func:`save_egress_policy`.

    A missing file yields the deny-by-default empty policy rather than the
    shipped default: an operator-supplied policy file is authoritative, and
    silently widening it to PyPI when it is absent would be the wrong default
    for a security control.
    """
    candidate = Path(path).expanduser()
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"egress policy file is missing: {candidate}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"egress policy file is unreadable: {candidate}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("egress policy must be a JSON object")
    hosts = payload.get("allowed_hosts")
    if hosts is None:
        raise ValueError("egress policy must declare allowed_hosts")
    if isinstance(hosts, str):
        hosts = [item for item in hosts.replace(",", " ").split() if item]
    if not isinstance(hosts, Sequence):
        raise ValueError("egress policy allowed_hosts must be a list")
    return EgressPolicy.build(
        hosts=list(hosts),
        allow_private=bool(payload.get("allow_private", False)),
        source=str(candidate),
    )


def save_egress_policy(
    path: Union[str, os.PathLike[str]], policy: EgressPolicy
) -> Path:
    """Write a policy to ``path`` atomically and return the resolved path."""
    from shared.security import require_contained

    target = Path(path).expanduser().absolute()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    contained = require_contained(parent, target.name)
    payload = json.dumps(policy.as_dict(), indent=2, sort_keys=True) + "\n"
    temporary = contained.with_name(contained.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, contained)
    return contained
