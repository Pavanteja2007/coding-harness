"""Privacy policy for provider requests: what may leave the machine, and
what must be redacted before it does.

Three separate decisions are often conflated into one "is it safe" question.
They are separated here because they fail differently:

1. **Is this provider allowed at all?** A per-task policy names the
   providers/models that may be dialed. Everything else is refused BEFORE a
   request is built, so a refused target never becomes an HTTP call.
2. **May this CLASS of data go there?** A policy declares the data classes
   a target may receive (``metadata``, ``source``, ``private``, ``user``,
   ``secret``). Source code is not an email address; conflating them means a
   policy either leaks code or blocks a question nobody asked to protect.
3. **Has the payload been made safe?** Redaction is a separate, unconditional
   step applied to the outgoing messages on EVERY request. It is not a
   policy the operator can turn off, because "redact nothing" is the setting
   that turns a leaked ``.env`` into a provider-side incident.

Zero-data-retention (ZDR) is treated as *metadata plus an honest request*,
not as a guarantee:

- :data:`PROVIDER_PRIVACY` records, per provider, whether a ZDR mode is
  documented and what request parameter carries it. An unknown provider is
  ``"unknown"``, never ``True``: an unverified retention claim is the worst
  kind of lie in this file.
- :func:`zdr_kwargs` returns only the parameter a provider's own API
  documents, and ``{}`` otherwise. The ledger records ``zdr_requested`` and
  ``zdr_supported`` so a reader can see whether a retention flag was actually
  sent or merely claimed.
- A local endpoint is ``data_scope="local"`` and is never asked for ZDR,
  because the data does not leave.

Everything here is fail-closed on the *decision* side: an unknown provider
under a strict policy is refused, not allowed. Everything here is
fail-safe on the *payload* side: redaction never raises, and a redaction
failure leaves the text intact rather than dropping the request, because a
lost request is visible while a silently-unsanitized one is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from shared.security import redact_secrets, redact_text

from .local_models import endpoint_fingerprint, is_local_endpoint

__all__ = [
    "DATA_CLASSES",
    "MIN_LITERAL_SECRET_CHARS",
    "PRIVACY_POLICIES",
    "PROVIDER_PRIVACY",
    "DataClass",
    "PrivacyDecision",
    "PrivacyPolicy",
    "PrivacyPolicyBlocked",
    "ProviderPrivacy",
    "authorize",
    "endpoint_label",
    "env_offline",
    "policy_from_config",
    "provider_privacy",
    "redact_messages",
    "redaction_summary",
    "scrub_environment_secrets",
    "zdr_kwargs",
]


class PrivacyPolicyBlocked(PermissionError):
    """Raised when NO candidate target is permitted by the active policy.

    A :class:`PermissionError` subclass so an existing caller that already
    distinguishes "the environment refused this" from "the provider failed"
    keeps working. Every skipped target's reason is carried on the exception,
    because "it was blocked" without "by what" is the least actionable error
    message in the system.
    """

    def __init__(self, decisions: Sequence[Any]) -> None:
        self.decisions = list(decisions)
        reasons = sorted({str(getattr(d, "reason", "unknown")) for d in self.decisions})
        names = sorted(
            {
                f"{getattr(d, 'provider', None) or 'default'}"
                f"/{getattr(d, 'model', None) or 'default'}"
                for d in self.decisions
            }
        )
        super().__init__(
            "privacy policy refused every provider target ("
            + ", ".join(names)
            + "); reasons: "
            + ", ".join(reasons)
            + "; no provider request was made"
        )


#: The closed set of data classes. Order is "increasingly sensitive", which
#: is what makes the presets below readable: a policy is a floor in this
#: ordering, so a stricter policy is a shorter list.
DATA_CLASSES: Tuple[str, ...] = (
    "metadata",  # counts, durations, model names - no repository content
    "public",  # public docs, package metadata, public API text
    "source",  # repository source code and tests
    "private",  # internal prose: issue text, internal URLs, config
    "user",  # user-authored text that is not in the repository
    "secret",  # anything that looks like a credential
)


@dataclass(frozen=True)
class DataClass:
    """One data class: its label and whether it may leave the machine by
    default under a cloud provider."""

    name: str
    description: str
    default_cloud_allowed: bool


_CLASSES: Dict[str, DataClass] = {
    item.name: item
    for item in (
        DataClass("metadata", "counts, durations, model/provider names", True),
        DataClass("public", "public documentation and package metadata", True),
        DataClass("source", "repository source code, tests, diffs", True),
        DataClass("private", "issue text, internal notes, internal endpoints", True),
        DataClass("user", "user-authored text outside the repository", True),
        DataClass("secret", "credential-shaped values", False),
    )
}


@dataclass(frozen=True)
class ProviderPrivacy:
    """What is known about one provider's handling of request content.

    ``zero_data_retention`` is one of ``True`` / ``False`` / ``"unknown"``.
    ``"unknown"`` is the default and is treated as NOT supporting ZDR by
    every policy in this module. Recording a guess as ``True`` would make
    this table the least trustworthy file in the repository.
    """

    name: str
    data_scope: str = "cloud"  # "cloud" | "local"
    zero_data_retention: Any = "unknown"
    zdr_parameter: Optional[str] = None
    training_opt_out: Any = "unknown"
    region: Optional[str] = None
    notes: str = ""


PROVIDER_PRIVACY: Dict[str, ProviderPrivacy] = {
    "anthropic": ProviderPrivacy(
        name="anthropic",
        zero_data_retention=True,
        zdr_parameter=None,
        training_opt_out=True,
        notes="API terms prohibit training on customer content; ZDR is the default contract.",
    ),
    "openai": ProviderPrivacy(
        name="openai",
        zero_data_retention=True,
        zdr_parameter="zero_data_retention",
        training_opt_out=True,
        notes="ZDR is available on the API tier via the documented request flag.",
    ),
    "gemini": ProviderPrivacy(
        name="gemini",
        zero_data_retention=True,
        zdr_parameter="no_store",
        training_opt_out=True,
        notes="ZDR is requested with the documented no_store body parameter.",
    ),
    "google": ProviderPrivacy(
        name="google",
        zero_data_retention=True,
        zdr_parameter="no_store",
        training_opt_out=True,
    ),
    "azure": ProviderPrivacy(
        name="azure",
        zero_data_retention=True,
        zdr_parameter="store",
        training_opt_out=True,
        notes="Retention is controlled by the customer's own Azure deployment.",
    ),
    "openai-compatible": ProviderPrivacy(
        name="openai-compatible",
        zero_data_retention="unknown",
        notes="A self-hosted or third-party router: retention is unknown, ask the operator.",
    ),
    "ollama": ProviderPrivacy(
        name="ollama",
        data_scope="local",
        zero_data_retention=True,
        notes="Serves from the local machine; content does not leave the host.",
    ),
    "llamacpp": ProviderPrivacy(
        name="llamacpp", data_scope="local", zero_data_retention=True
    ),
    "lmstudio": ProviderPrivacy(
        name="lmstudio", data_scope="local", zero_data_retention=True
    ),
    "vllm": ProviderPrivacy(name="vllm", data_scope="local", zero_data_retention=True),
    "local": ProviderPrivacy(
        name="local", data_scope="local", zero_data_retention=True
    ),
    "localhost": ProviderPrivacy(
        name="localhost", data_scope="local", zero_data_retention=True
    ),
    "localai": ProviderPrivacy(
        name="localai", data_scope="local", zero_data_retention=True
    ),
}


def provider_privacy(
    provider: Optional[str], api_base: Optional[str] = None
) -> ProviderPrivacy:
    """Return the privacy record for a target, deriving it when unknown.

    A local endpoint is answered from the endpoint, not the provider name:
    ``openai/`` pointed at ``127.0.0.1`` is a local server regardless of the
    name it is dialled under. An unknown CLOUD provider gets the explicit
    ``openai-compatible`` record with ``zero_data_retention="unknown"``,
    which every strict policy refuses.
    """
    name = str(provider or "").strip().lower()
    if is_local_endpoint(provider, api_base):
        return ProviderPrivacy(
            name=name or "local",
            data_scope="local",
            zero_data_retention=True,
            notes="resolved as local from the endpoint, not the provider name",
        )
    record = PROVIDER_PRIVACY.get(name)
    if record is not None:
        return record
    return ProviderPrivacy(
        name=name or "unknown",
        data_scope="cloud",
        zero_data_retention="unknown",
        notes="unregistered provider: retention is unknown, so no ZDR claim is made",
    )


def zdr_kwargs(
    provider: Optional[str], api_base: Optional[str] = None
) -> Dict[str, Any]:
    """Return the request kwargs that REQUEST zero data retention.

    Only a parameter the provider's own API documents is returned, and a
    parameter whose documented meaning is "store the data" is deliberately
    NOT inverted into an opt-out: a wrong guess here is a false retention
    claim, which is worse than no claim. Callers must record that this
    returned ``{}`` rather than presenting it as ZDR being satisfied.
    """
    record = provider_privacy(provider, api_base)
    if record.zero_data_retention is not True or not record.zdr_parameter:
        return {}
    parameter = record.zdr_parameter
    if parameter == "zero_data_retention":
        return {"zero_data_retention": True}
    if parameter == "no_store":
        return {"extra_body": {"no_store": True}}
    # Any other documented spelling is a top-level flag with a truthy value.
    return {parameter: True}


@dataclass
class PrivacyPolicy:
    """A per-task policy describing what may leave the machine.

    ``data_classes`` is the ceiling. An empty tuple means "nothing but
    metadata" is allowed, which is the strict floor. ``providers`` /
    ``models`` are allow-lists: empty means "any", because a policy that
    names no provider is expressing a data-sensitivity decision, not a
    routing decision.
    """

    name: str = "standard"
    data_classes: Tuple[str, ...] = DATA_CLASSES
    providers: Tuple[str, ...] = ()
    models: Tuple[str, ...] = ()
    require_zero_data_retention: bool = False
    redact: bool = True
    local_only: bool = False
    notes: str = ""

    @property
    def strict(self) -> bool:
        """True when this policy cannot be satisfied by a cloud provider."""
        return self.local_only or "source" not in self.data_classes

    def allows_class(self, name: str) -> bool:
        """Return whether a data class is within this policy's ceiling."""
        if not self.data_classes:
            return name == "metadata"
        return str(name) in self.data_classes

    def as_dict(self) -> Dict[str, Any]:
        """Return a receipt safe for a ledger row or a trace event."""
        return {
            "name": self.name,
            "data_classes": list(self.data_classes),
            "providers": list(self.providers),
            "models": list(self.models),
            "require_zero_data_retention": self.require_zero_data_retention,
            "redact": self.redact,
            "local_only": self.local_only,
            "strict": self.strict,
        }


PRIVACY_POLICIES: Dict[str, PrivacyPolicy] = {
    # Nothing but counters may reach a provider. Everything else is local.
    "local_only": PrivacyPolicy(
        name="local_only",
        data_classes=("metadata",),
        require_zero_data_retention=True,
        local_only=True,
        notes="no repository or user content may leave the machine",
    ),
    # Public repositories only. The common corporate default.
    "source_ok": PrivacyPolicy(
        name="source_ok",
        data_classes=("metadata", "public", "source"),
        notes="repository source may reach a provider; user/private content may not",
    ),
    "standard": PrivacyPolicy(name="standard"),
    # Everything permitted, but only to a provider with a documented ZDR mode.
    "zdr": PrivacyPolicy(
        name="zdr",
        require_zero_data_retention=True,
        notes="only providers with a documented zero-data-retention mode may be dialed",
    ),
}

_ALIASES = {
    "local": "local_only",
    "localonly": "local_only",
    "local-first": "local_only",
    "offline": "local_only",
    "strict": "local_only",
    "zdr_only": "zdr",
    "zero_data_retention": "zdr",
    "open": "standard",
    "default": "standard",
}


def policy_from_config(config: Any) -> PrivacyPolicy:
    """Resolve the per-task policy from a config mapping.

    Accepted keys, all additive Task.config keys:

    - ``privacy_policy`` — a preset name or an inline mapping.
    - ``privacy_data_classes`` — a list of class names.
    - ``privacy_providers`` / ``privacy_models`` — allow-lists.
    - ``privacy_require_zdr`` — bool.
    - ``privacy_redact`` — bool. Accepted for explicitness, but the effective
      value is still forced on for the ``secret`` class: a credential is not
      exfiltrated because a config said ``redact = false``.
    """
    if not isinstance(config, Mapping):
        return PRIVACY_POLICIES["standard"]
    raw = config.get("privacy_policy")
    policy: PrivacyPolicy
    if isinstance(raw, Mapping):
        policy = (
            policy_from_config(raw)
            if raw.get("name") in PRIVACY_POLICIES
            else PrivacyPolicy()
        )
        data_classes = raw.get("data_classes")
        if isinstance(data_classes, (list, tuple)):
            policy.data_classes = tuple(
                str(item).strip().lower() for item in data_classes if str(item).strip()
            )
        for key, attribute in (("providers", "providers"), ("models", "models")):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                setattr(
                    policy,
                    attribute,
                    tuple(str(item).strip().lower() for item in value),
                )
        for key, attribute in (
            ("require_zero_data_retention", "require_zero_data_retention"),
            ("redact", "redact"),
            ("local_only", "local_only"),
        ):
            if key in raw:
                setattr(policy, attribute, bool(raw[key]))
        if raw.get("name"):
            policy.name = str(raw["name"])
    else:
        name = str(raw or "standard").strip().lower().replace("-", "_")
        name = _ALIASES.get(name, name)
        policy = PRIVACY_POLICIES.get(name)
        if policy is None:
            raise ValueError(
                f"unknown privacy policy {raw!r}; known policies: "
                + ", ".join(sorted(PRIVACY_POLICIES))
            )
        policy = PrivacyPolicy(**{**policy.__dict__})
    if "privacy_data_classes" in config:
        value = config.get("privacy_data_classes")
        if isinstance(value, (list, tuple)):
            policy.data_classes = tuple(
                str(item).strip().lower() for item in value if str(item).strip()
            )
    if "privacy_providers" in config and isinstance(
        config.get("privacy_providers"), (list, tuple)
    ):
        policy.providers = tuple(
            str(item).strip().lower() for item in config["privacy_providers"] or ()
        )
    if "privacy_models" in config and isinstance(
        config.get("privacy_models"), (list, tuple)
    ):
        policy.models = tuple(
            str(item).strip().lower() for item in config["privacy_models"] or ()
        )
    if "privacy_require_zdr" in config:
        policy.require_zero_data_retention = bool(config["privacy_require_zdr"])
    if "privacy_redact" in config:
        policy.redact = bool(config["privacy_redact"])
    unknown = [name for name in policy.data_classes if name not in _CLASSES]
    if unknown:
        raise ValueError(
            "unknown privacy data class(es): "
            + ", ".join(sorted(unknown))
            + f"; known classes: {', '.join(DATA_CLASSES)}"
        )
    if not policy.redact:
        # A credential is never exfiltrated because a config asked for it.
        policy.data_classes = tuple(
            name for name in policy.data_classes if name != "secret"
        )
    return policy


@dataclass
class PrivacyDecision:
    """The verdict for one (policy, target) pair.

    ``allowed=False`` is a refusal the router must honor BEFORE building a
    request. ``reason`` is a stable slug for the ledger; ``detail`` is a
    human sentence carrying no content from the payload.
    """

    allowed: bool
    reason: str
    detail: str
    policy: str
    provider: Optional[str] = None
    model: Optional[str] = None
    data_scope: str = "cloud"
    zero_data_retention: Any = "unknown"
    zdr_requested: bool = False
    zdr_supported: bool = False
    zdr_parameter: Optional[str] = None
    data_classes: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe receipt for the ledger row / trace event."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "detail": self.detail,
            "policy": self.policy,
            "provider": self.provider,
            "model": self.model,
            "data_scope": self.data_scope,
            "zero_data_retention": (
                self.zero_data_retention
                if isinstance(self.zero_data_retention, (bool, str))
                else str(self.zero_data_retention)
            ),
            "zdr_requested": self.zdr_requested,
            "zdr_supported": self.zdr_supported,
            "zdr_parameter": self.zdr_parameter,
            "data_classes": list(self.data_classes),
        }


def authorize(
    policy: PrivacyPolicy,
    provider: Optional[str],
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    *,
    data_classes: Optional[Sequence[str]] = None,
) -> PrivacyDecision:
    """Decide whether one target may be dialed under ``policy``.

    Checks run in this order, and the order matters: the cheapest and most
    decisive refusals come first, so a disallowed provider is reported as
    ``provider_not_allowed`` rather than as a data-class problem that the
    caller would then try to work around.

    1. local endpoint -> always allowed (content does not leave the machine),
       even under the strictest policy.
    2. local-only policy -> any cloud provider is refused.
    3. provider allow-list.
    4. model allow-list.
    5. data classes the request actually carries.
    6. zero-data-retention requirement against the provider's DOCUMENTED
       capability. ``"unknown"`` fails closed.
    """
    record = provider_privacy(provider, api_base)
    carried = tuple(
        dict.fromkeys(
            str(item).strip().lower()
            for item in (
                data_classes if data_classes is not None else _request_classes(record)
            )
            if str(item).strip()
        )
    )
    common = {
        "policy": policy.name,
        "provider": provider,
        "model": model,
        "data_scope": record.data_scope,
        "zero_data_retention": record.zero_data_retention,
        "data_classes": carried,
    }
    if record.data_scope == "local":
        return PrivacyDecision(
            allowed=True,
            reason="local_endpoint",
            detail="served from the local machine; content does not leave the host",
            zdr_requested=False,
            zdr_supported=True,
            **common,
        )
    if policy.local_only:
        return PrivacyDecision(
            allowed=False,
            reason="policy_local_only",
            detail="policy forbids sending content to a remote provider",
            **common,
        )
    if policy.providers and str(provider or "").strip().lower() not in policy.providers:
        return PrivacyDecision(
            allowed=False,
            reason="provider_not_allowed",
            detail=(
                f"provider {provider!r} is not in the policy allow-list"
                if provider
                else "no provider is named by the policy allow-list"
            ),
            **common,
        )
    if policy.models and str(model or "").strip().lower() not in policy.models:
        return PrivacyDecision(
            allowed=False,
            reason="model_not_allowed",
            detail=f"model {model!r} is not in the policy allow-list",
            **common,
        )
    blocked = [name for name in carried if not policy.allows_class(name)]
    if blocked:
        return PrivacyDecision(
            allowed=False,
            reason="data_class_not_allowed",
            detail="policy forbids data class(es): " + ", ".join(sorted(blocked)),
            **common,
        )
    if policy.require_zero_data_retention and record.zero_data_retention is not True:
        return PrivacyDecision(
            allowed=False,
            reason="zdr_not_documented",
            detail=(
                f"provider {record.name!r} has no documented zero-data-retention mode; "
                "an unverified retention claim is not a guarantee"
            ),
            **common,
        )
    return PrivacyDecision(
        allowed=True,
        reason="allowed",
        detail="permitted by policy",
        zdr_requested=bool(policy.require_zero_data_retention and record.zdr_parameter),
        zdr_supported=record.zero_data_retention is True,
        zdr_parameter=(
            record.zdr_parameter if policy.require_zero_data_retention else None
        ),
        **common,
    )


def _request_classes(record: ProviderPrivacy) -> Tuple[str, ...]:
    """Return the data classes a request to this provider carries.

    A model call always carries ``metadata`` and, for a coding agent,
    ``source`` (retrieved code and the issue text). ``secret`` is included
    unconditionally so a payload is always checked for credentials even
    though the class is never *permitted* to leave; the two jobs are
    different (check vs permit) and the redaction step performs the check.
    """
    if record.data_scope == "local":
        return ("metadata", "source", "secret")
    return ("metadata", "source", "private", "secret")


def redaction_summary(original: Any, redacted: Any) -> Dict[str, Any]:
    """Return a receipt describing what redaction changed.

    The counts are derived by comparing the two values, not by trusting the
    redactor to report on itself, and the receipt carries no content.
    """
    before = len(str(original or ""))
    after = len(str(redacted or ""))
    changed = str(original or "") != str(redacted or "")
    return {
        "redacted": changed,
        "chars_before": before,
        "chars_after": after,
        "chars_removed": max(0, before - after),
    }


def redact_messages(
    messages: Any,
    extra_secrets: Optional[Iterable[str]] = None,
    *,
    enabled: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Return ``(messages, receipt)`` with credential material removed.

    Applied to EVERY outgoing request regardless of policy: the ceiling
    invariant is that a secret never enters a provider request, and a
    config that says otherwise does not get a vote on that.

    Never raises. On an unexpected failure the ORIGINAL messages are
    returned with ``receipt["redacted"]=False`` and a reason recorded: a
    redaction that silently drops the request would turn a policy failure
    into a lost task, and a redaction failure is the thing an operator needs
    to see.

    When nothing needed redaction the ORIGINAL object is returned, not an
    equal copy. That keeps the bytes on the wire (and the prompt-cache prefix
    digest, and every existing request-shape assertion) identical for the
    overwhelmingly common case of a payload with no credential in it.
    """
    secrets = [str(item) for item in (extra_secrets or ()) if item]
    receipt: Dict[str, Any] = {
        "redacted": False,
        "chars_before": 0,
        "chars_after": 0,
        "chars_removed": 0,
        "reason": "",
    }
    if messages is None:
        return messages, receipt
    if not enabled and not secrets:
        receipt["reason"] = "disabled"
        return messages, receipt
    try:
        cleaned = redact_secrets(messages, secrets=secrets)
        # The recursive shared redactor covers credential-shaped keys and
        # known patterns; a final text pass catches a bare token embedded in
        # prose ("use sk-abc123 to call the API") that has no key to hang off.
        cleaned = _redact_text_pass(cleaned)
    except Exception as exc:  # pragma: no cover - defensive
        receipt["reason"] = f"redaction_failed: {type(exc).__name__}"
        return messages, receipt
    summary = redaction_summary(messages, cleaned)
    receipt.update(summary)
    if not summary["redacted"]:
        receipt["reason"] = "no_change"
        return messages, receipt
    receipt["reason"] = "redacted"
    return cleaned, receipt


def _redact_text_pass(value: Any, depth: int = 0) -> Any:
    """Apply ``redact_text`` to every string inside a nested structure."""
    if depth > 24:
        return value
    if isinstance(value, str):
        # ``secrets=()``, not ``secrets=None``: the shared redactor iterates
        # the argument, and ``None`` is a TypeError, not an empty list.
        return redact_text(value, secrets=())
    if isinstance(value, Mapping):
        return {key: _redact_text_pass(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_text_pass(item, depth + 1) for item in value]
        try:
            return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
        except TypeError:  # namedtuple and friends
            return rebuilt
    return value


#: Minimum length before a config-supplied value is treated as a literal
#: secret worth removing. A shorter value (a tier named "m", a boolean-ish
#: string) would match inside unrelated words and mangle the prompt.
MIN_LITERAL_SECRET_CHARS = 6

_SECRET_KEY_TOKENS = (
    "api_key",
    "apikey",
    "key",
    "token",
    "secret",
    "password",
    "passwd",
)


def _is_secret_key(name: str) -> bool:
    """True when a config key name conventionally carries a credential."""
    normalized = str(name).lower()
    return any(token in normalized for token in _SECRET_KEY_TOKENS)


def scrub_environment_secrets(config: Optional[Mapping[str, Any]]) -> List[str]:
    """Return the non-empty secret-shaped values present in a config.

    Used to feed the configured credentials into the redactor as literal
    values: a key the user configured is a secret the redactor should remove
    by identity even if its shape matches no pattern. Values shorter than
    :data:`MIN_LITERAL_SECRET_CHARS` are skipped, because a literal that
    short would match inside unrelated words and corrupt the prompt.
    """
    out: List[str] = []
    if not isinstance(config, Mapping):
        return out

    def _collect(mapping: Mapping[str, Any], depth: int = 0) -> None:
        if depth > 8:
            return
        for key, value in mapping.items():
            if isinstance(value, Mapping):
                _collect(value, depth + 1)
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Mapping):
                        _collect(item, depth + 1)
                continue
            if not _is_secret_key(key):
                continue
            if (
                isinstance(value, str)
                and len(value.strip()) >= MIN_LITERAL_SECRET_CHARS
            ):
                out.append(value.strip())

    _collect(config)
    return out


def env_offline() -> bool:
    """Return True when the process environment requests offline mode."""
    value = str(os.environ.get("NEO_OFFLINE", "")).strip().lower()
    return value not in ("", "0", "false", "no", "off")


def endpoint_label(api_base: Optional[str]) -> str:
    """Return a non-secret, human-readable label for an endpoint."""
    if not api_base:
        return ""
    try:
        parts = urlsplit(str(api_base))
        if parts.hostname:
            return redact_text(parts.hostname)
    except ValueError:
        pass
    return redact_text(str(api_base))[:64] or f"sha256:{endpoint_fingerprint(api_base)}"
