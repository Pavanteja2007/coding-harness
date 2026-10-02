"""Local-first model tier: the cheap work stays on the machine.

The cost story in ``project-spec.md`` is "cheap/open model as the default
tier, frontier model only as the escalation tier". A remote cheap tier still
sends the prompt to somebody else's machine and still bills per token. This
module is the local version of the same idea, and it owns four things:

1. **A local model profile.** Provider, model, endpoint, optional window,
   and the ROLES it is trusted for (``retrieval``, ``summarize``,
   ``boilerplate``, ``decisive``). A role is not decoration: ``decisive`` is
   the only role allowed to be the step a frontier model must take.

2. **Tier classification.** Every resolved target is classified
   ``local`` or ``frontier`` from the endpoint, not from the model NAME. A
   model called ``qwen3.8-27b`` served by a remote router is frontier spend;
   the same name on ``127.0.0.1`` is local. Classification drives the
   routing decision, the ledger fields, and the cost report, so getting it
   wrong would misreport spend.

3. **A local context-window probe with a cache.** A local server's window
   is whatever the model was built with, and the honest answer differs per
   model on the same server. :func:`local_context_window` delegates to
   ``runtime.model_capabilities`` — the one cache in the repository keyed
   per ``(endpoint, model)``, with a positive floor and a recorded source —
   and adds the local-specific piece: an explicit profile window wins, and a
   probe that fails degrades to the floor with ``source`` saying so. It
   never returns zero, and it never opens a socket on its own.

4. **Local vs frontier accounting.** :func:`local_frontier_split` folds
   ledger rows into the two buckets with tokens, cost, and share, so ``/cost``
   and ``--json`` can report the split instead of one undifferentiated
   number.

Design constraints:

- Nothing here imports a provider SDK or opens a connection. The probe is
  caller-supplied or opt-in through the profile, because a capability probe
  is a billable/observable call.
- Local is a DEFAULT OFF. With no profile configured the router's behavior
  is byte-identical to before this module existed.
- ``local`` never means "free" in a report: a hosted endpoint billed at a
  token rate is not local, and a genuinely local model reports a real $0.00
  with ``price_source`` saying why, rather than an unpriced blank.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

__all__ = [
    "CHEAP_ROLES",
    "DECISIVE_ROLE",
    "LOCAL_HOSTNAMES",
    "LOCAL_PROVIDER_NAMES",
    "LocalProfile",
    "classify_target",
    "describe_roles",
    "endpoint_fingerprint",
    "is_local_endpoint",
    "local_context_window",
    "local_frontier_split",
    "local_profile_from_config",
    "read_ledger_rows",
    "resolve_local_profile",
    "role_for_hint",
    "should_use_local",
    "summarize",
]

#: Roles the local model is trusted for. These are the three the ceiling
#: prompt names: retrieval, summarization, boilerplate. None of them is the
#: step a task's correctness turns on.
CHEAP_ROLES = ("retrieval", "summarize", "boilerplate")

#: The one role that is NOT local-first. A frontier model keeps the decisive
#: step; "decisive" is what makes that explicit rather than implicit.
DECISIVE_ROLE = "decisive"

#: Provider names that mean "runs on my machine" regardless of endpoint.
#: ``localhost``/``local``/``lmstudio`` are self-declared; ``ollama``,
#: ``llamacpp``, ``vllm`` are the local-serving ecosystems.
LOCAL_PROVIDER_NAMES = frozenset(
    {
        "ollama",
        "llamacpp",
        "llama_cpp",
        "llama.cpp",
        "lmstudio",
        "lm_studio",
        "vllm",
        "local",
        "localhost",
        "localai",
    }
)

#: Hostnames that cannot name a remote machine. ``0.0.0.0`` is included
#: because it is the address a local server binds to.
LOCAL_HOSTNAMES = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal"}
)


def is_local_endpoint(provider: Optional[str], api_base: Optional[str]) -> bool:
    """Return True when this target is served from the local machine.

    Two independent signals, either sufficient:

    - the **provider name** is a known local-serving ecosystem, or
    - the **endpoint host** is loopback / a link-local local name.

    An absent endpoint is NOT local evidence: a provider name with no
    ``api_base`` is litellm's own hosted default unless the provider itself
    is a local one. Guessing "no base URL means local" is exactly the lie
    that makes a cost report wrong.
    """
    name = str(provider or "").strip().lower()
    if name in LOCAL_PROVIDER_NAMES:
        return True
    if not api_base:
        return False
    try:
        parts = urlsplit(str(api_base))
    except ValueError:
        return False
    host = (parts.hostname or "").strip().lower()
    if not host:
        return False
    if host in LOCAL_HOSTNAMES:
        return True
    # A bare host with no scheme is common in a hand-written config
    # ("127.0.0.1:11434"); urlsplit puts it in .path.
    if not parts.scheme and "/" not in str(api_base):
        host = str(api_base).split(":", 1)[0].strip().lower()
        if host in LOCAL_HOSTNAMES:
            return True
    return host.endswith(".local") or host.endswith(".localhost")


def classify_target(provider: Optional[str], api_base: Optional[str]) -> str:
    """Return ``"local"`` or ``"frontier"`` for one resolved target."""
    return "local" if is_local_endpoint(provider, api_base) else "frontier"


@dataclass(frozen=True)
class LocalProfile:
    """One local model the user has told Neo it may use for cheap work.

    ``context_window`` is optional on purpose: a real answer requires a
    probe, and a guessed number is a lie a context budgeter acts on. When it
    is absent the resolved window carries its source so a reader can tell a
    declared window from a floor.
    """

    provider: str
    model: str
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    label: Optional[str] = None
    context_window: Optional[int] = None
    roles: tuple = CHEAP_ROLES
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    probe: Optional[Callable[..., Any]] = field(default=None, compare=False, repr=False)

    @property
    def tier_class(self) -> str:
        """Always ``"local"`` — the profile's own classification."""
        return "local"

    def as_dict(self) -> Dict[str, Any]:
        """Return a receipt with no credential in it.

        The API key is deliberately excluded rather than masked: this dict
        is written to ledgers and traces, and a masked-but-present key is
        still a value a reader learns to expect there.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base_sha256": endpoint_fingerprint(self.api_base),
            "label": self.label,
            "context_window": self.context_window,
            "roles": list(self.roles),
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "tier_class": "local",
        }

    def as_target(self) -> Dict[str, Any]:
        """Return a router-shaped target dict for this profile."""
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "display_label": self.label,
            "source_tier": "local_model_profile",
            "input_cost_per_million": self.input_cost_per_million,
            "output_cost_per_million": self.output_cost_per_million,
            "tier_class": "local",
            "source_tier_class": "local",
        }


def endpoint_fingerprint(api_base: Optional[str]) -> Optional[str]:
    """Return a short, non-secret fingerprint of an endpoint URL."""
    if not api_base:
        return None
    import hashlib

    return hashlib.sha256(str(api_base).encode("utf-8", "replace")).hexdigest()[:12]


def _positive_int(value: Any) -> Optional[int]:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def local_profile_from_config(raw: Any) -> Optional[LocalProfile]:
    """Build a :class:`LocalProfile` from a config mapping, or None.

    A malformed or incomplete profile is ``None``, never a half-profile: a
    local tier with no model name cannot be dialed, and silently dialing the
    frontier default instead would defeat the feature silently.
    """
    if not isinstance(raw, Mapping):
        return None
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not provider or not model:
        return None
    roles = raw.get("roles")
    if isinstance(roles, str):
        roles = (roles,)
    if not isinstance(roles, (list, tuple)) or not roles:
        roles = CHEAP_ROLES
    normalized = tuple(str(role).strip().lower() for role in roles if str(role).strip())
    if DECISIVE_ROLE in normalized:
        # A local model may not claim the decisive role: that is the whole
        # separation between "cheap work" and "the step correctness turns
        # on". Dropping it here keeps the refusal at the boundary instead of
        # discovering it at routing time.
        normalized = (
            tuple(role for role in normalized if role != DECISIVE_ROLE) or CHEAP_ROLES
        )
    return LocalProfile(
        provider=provider,
        model=model,
        api_base=str(raw.get("api_base") or raw.get("base_url") or "").strip() or None,
        api_key=str(raw.get("api_key") or "").strip() or None,
        label=str(raw.get("label") or raw.get("display_label") or "").strip() or None,
        context_window=_positive_int(raw.get("context_window")),
        roles=normalized,
        input_cost_per_million=_as_float(raw.get("input_cost_per_million"), 0.0),
        output_cost_per_million=_as_float(raw.get("output_cost_per_million"), 0.0),
        probe=raw.get("context_window_probe")
        if callable(raw.get("context_window_probe"))
        else None,
    )


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if result != result else result


def resolve_local_profile(
    config: Optional[Mapping[str, Any]],
) -> Optional[LocalProfile]:
    """Return the local profile for a task/router config, or None.

    Accepted shapes (first match wins), all additive Task.config keys:

    - ``local_model_profile`` — the documented shape (a mapping).
    - ``local_model`` + ``local_api_base`` (+ ``local_provider``) — the
      flat shape a user is likelier to type into a settings file.
    - a ``model_tiers`` entry that itself resolves to a local endpoint and
      carries ``local: true`` or is the ``easy``/``medium`` tier.
    """
    if not isinstance(config, Mapping):
        return None
    profile = local_profile_from_config(config.get("local_model_profile"))
    if profile is not None:
        return profile
    model = str(config.get("local_model") or "").strip()
    if model:
        built = local_profile_from_config(
            {
                "provider": config.get("local_provider") or "ollama",
                "model": model,
                "api_base": config.get("local_api_base")
                or config.get("local_base_url"),
                "api_key": config.get("local_api_key"),
                "label": config.get("local_label"),
                "context_window": config.get("local_context_window"),
            }
        )
        if built is not None:
            return built
    tiers = config.get("model_tiers")
    if isinstance(tiers, Mapping):
        for hint in ("easy", "medium", "local"):
            tier = tiers.get(hint)
            if not isinstance(tier, Mapping):
                continue
            if not is_local_endpoint(
                tier.get("provider"), tier.get("api_base") or tier.get("base_url")
            ):
                continue
            built = local_profile_from_config(
                {
                    **dict(tier),
                    "roles": tier.get("roles") or CHEAP_ROLES,
                }
            )
            if built is not None:
                return built
    return None


def role_for_hint(difficulty_hint: Optional[str]) -> str:
    """Map a difficulty hint to the work role it represents.

    ``hard`` is the decisive step: a frontier model keeps it. Everything
    else is cheap work a local model is trusted for.
    """
    return (
        DECISIVE_ROLE
        if str(difficulty_hint or "").strip().lower() == "hard"
        else "boilerplate"
    )


def should_use_local(
    config: Optional[Mapping[str, Any]],
    difficulty_hint: Optional[str],
    *,
    explicit_model: bool = False,
    explicit_provider: bool = False,
    adaptive_routing: bool = True,
) -> bool:
    """Decide whether this call should use the local profile.

    Four things veto the local tier, each for a stated reason:

    1. no local profile configured (the default-off rule);
    2. an **explicit** model or provider from the caller — an explicit
       target means the caller knows best and silently redirecting a pinned
       model would make the ledger lie;
    3. the decisive step (``hard``) — frontier escalation is the feature,
       not a bug to route around;
    4. adaptive routing off — that flag is the ablation switch for tier
       selection, and a local redirect under a pinned OFF arm would
       invalidate an ablation run.

    A ``local_first`` config key can force the cheap tier on (roles are still
    respected, so the decisive step stays frontier) or off it.
    """
    if not isinstance(config, Mapping):
        return False
    profile = resolve_local_profile(config)
    if profile is None:
        return False
    if explicit_model or explicit_provider:
        return False
    if not adaptive_routing and not config.get("local_first"):
        return False
    role = role_for_hint(difficulty_hint)
    if role == DECISIVE_ROLE and not config.get("local_first_decisive"):
        return False
    if config.get("local_first") is False:
        return False
    if config.get("local_first_roles"):
        wanted = config.get("local_first_roles")
        if isinstance(wanted, str):
            wanted = [wanted]
        allowed = {str(item).strip().lower() for item in wanted or ()}
        if allowed and role not in allowed:
            return False
    return True


def local_context_window(
    profile: LocalProfile,
    *,
    probe: Optional[Callable[..., Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Resolve a local model's context window through the shared cache.

    Precedence: a declared ``profile.context_window`` -> a caller/probe
    answer -> the local table -> the documented positive floor. The result
    always carries ``context_window_source`` and ``tier_class="local"`` and
    is always strictly positive. A declared window is reported as
    ``declared`` so a reader can tell a user's claim from a measurement.
    """
    from . import model_capabilities

    declared = _positive_int(profile.context_window)
    if declared is not None:
        return {
            "context_window": declared,
            "context_window_source": "declared",
            "model": profile.model,
            "api_base_sha256": endpoint_fingerprint(profile.api_base),
            "tier_class": "local",
            "cache": "declared",
            "probe_calls": 0,
        }
    resolved = model_capabilities.resolve_context_window(
        profile.model,
        provider=profile.provider,
        api_base=profile.api_base,
        probe=probe or profile.probe,
        use_cache=use_cache,
    )
    resolved["tier_class"] = "local"
    return resolved


def _empty_bucket() -> Dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tokens": 0,
        "cost_usd": 0.0,
    }


def local_frontier_split(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Fold model-ledger rows into local vs frontier tokens and cost.

    Rows without a ``tier_class`` are classified from
    ``provider``/``api_base`` so a ledger written before this feature landed
    still splits correctly. Malformed rows are skipped, never fatal: a cost
    report must not be the thing that breaks a run.
    """
    split: Dict[str, Any] = {
        "local": _empty_bucket(),
        "frontier": _empty_bucket(),
        "unclassified": 0,
        "rows": 0,
    }
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        split["rows"] += 1
        declared = str(row.get("tier_class") or "").strip().lower()
        if declared not in ("local", "frontier"):
            declared = classify_target(row.get("provider"), row.get("api_base"))
        bucket = split[declared]
        bucket["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "tokens"):
            try:
                bucket[key] += int(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            bucket["cost_usd"] += float(row.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    total_tokens = split["local"]["tokens"] + split["frontier"]["tokens"]
    total_cost = split["local"]["cost_usd"] + split["frontier"]["cost_usd"]
    for name in ("local", "frontier"):
        bucket = split[name]
        bucket["cost_usd"] = round(bucket["cost_usd"], 8)
        bucket["token_share"] = (
            round(bucket["tokens"] / total_tokens, 6) if total_tokens else 0.0
        )
        bucket["cost_share"] = (
            round(bucket["cost_usd"] / total_cost, 6) if total_cost else 0.0
        )
    split["total_tokens"] = total_tokens
    split["total_cost_usd"] = round(total_cost, 8)
    return split


def read_ledger_rows(path: Any, limit: int = 100_000) -> List[Dict[str, Any]]:
    """Read a JSONL model ledger, tolerating a torn or partial last line.

    A ledger is append-only and can be read while a run is live, so a
    half-written final line is expected rather than exceptional.
    """
    from pathlib import Path

    target = Path(path)
    if not target.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= limit:
                    break
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return rows
    return rows


def summarize(
    config: Optional[Mapping[str, Any]] = None, ledger_path: Any = None
) -> Dict[str, Any]:
    """Return the local/frontier report for ``/cost`` and ``--json``.

    The report names the configured local profile (no credential) and, when
    a ledger path is given, the measured split from that ledger. A report
    with no ledger still states the CONFIGURATION, which is the part a user
    needs to answer "is my local tier actually being used".
    """
    profile = resolve_local_profile(config)
    report: Dict[str, Any] = {
        "local_model_configured": profile is not None,
        "local_model": profile.as_dict() if profile is not None else None,
        "local_first": bool(isinstance(config, Mapping) and config.get("local_first")),
        "local_first_decisive": bool(
            isinstance(config, Mapping) and config.get("local_first_decisive")
        ),
        "split": None,
        "ledger": str(ledger_path) if ledger_path else None,
    }
    if ledger_path is not None:
        report["split"] = local_frontier_split(read_ledger_rows(ledger_path))
    return report


def describe_roles(profile: Optional[LocalProfile]) -> Sequence[str]:
    """Return the roles a local profile is trusted for (empty when absent)."""
    return tuple(profile.roles) if profile is not None else ()
