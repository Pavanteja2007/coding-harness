"""`/connect` — one credential store, save-first, plain-language failures.

This module replaces the old login ordering. The measured defect it closes:
a user pasted an OpenRouter key, watched a 60-second live probe fail, saw a
provider banner and then ``Test failed: litellm.Timeout: APITimeoutError``,
and was told ``Nothing saved.`` The key they typed was thrown away and a
Python class name was shown to them instead.

The inversions that fix it, each enforced by a test rather than a comment:

1. **The first run never blocks.** Nothing in the session start path requires
   a wizard. :func:`first_run_hint` is the whole first-run surface: one line,
   no prompt, no wizard. ``cli.onboard.maybe_onboard_repl`` no longer runs a
   wizard.
2. **One store.** ``<neo_home>/auth.json``, mode 0600, keyed by provider id,
   every entry discriminated by its auth method. Writing provider N touches
   exactly one key, so providers 1..N-1 are byte-identical afterwards.
3. **The provider list is DATA.** :data:`PROVIDERS` is a table, and
   ``<neo_home>/providers.json`` is merged over it, so adding a provider is a
   data edit with no code change. Sorted by priority then name, at most
   :data:`MAX_PROVIDERS_SHOWN` rows, ``other`` always last.
4. **SAVE FIRST, TEST SECOND.** :func:`connect` persists the credential and
   returns a receipt; verification happens afterwards, in a CHILD PROCESS
   (:mod:`cli.auth` re-entered as ``python -m cli.auth``), so a hung provider
   can neither block the app nor print into its terminal. A failed
   verification never discards what the user typed.
5. **The auth-method step is conditional.** ``Select auth method`` is
   rendered only for a provider that declares more than one method.
6. **Plain sentences only.** :func:`classify_failure` turns any exception
   object, any exception string, or a provider banner into one sentence, and
   :func:`looks_like_library_leak` is the gate that keeps a class name out of
   it.
7. **Secrets are referenced, never inlined.** A config file gets
   ``{env:VAR}``; a literal key is written only into ``auth.json``.

Nothing here imports ``cli.tui``: this terminal does not own that file. The
mount points for the TUI and the REPL are in ``cli/AGENTS.md`` under
"Handoff to 01".
"""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

__all__ = [
    "AUTH_FILENAME",
    "AUTH_FILE_MODE",
    "AUTH_METHODS",
    "FAILURE_KINDS",
    "FORBIDDEN_SUBSTRINGS",
    "MAX_PROVIDERS_SHOWN",
    "SCHEMA_VERSION",
    "BackgroundCheck",
    "ConnectResult",
    "Credential",
    "Credentials",
    "Provider",
    "StoreCorrupt",
    "Verification",
    "active_credential",
    "apply_active_credential",
    "auth_path",
    "classify_failure",
    "cmd_auth_list",
    "cmd_auth_logout",
    "cmd_connect",
    "connect",
    "connect_interactive",
    "first_run_hint",
    "is_env_ref",
    "list_credentials",
    "load_credentials",
    "looks_like_library_leak",
    "markup_safe",
    "mask_key",
    "method_prompt",
    "needs_method_choice",
    "normalise_provider_id",
    "provider_by_id",
    "provider_id_for_endpoint",
    "provider_list",
    "provider_prompt",
    "read_store",
    "register_connect_parser",
    "remove_credential",
    "render_connect_result",
    "render_list",
    "render_method_prompt",
    "render_provider_menu",
    "render_status",
    "render_verification",
    "resolve_secret",
    "save_credential",
    "secret_source",
    "section_lines",
    "set_active",
    "status_line",
    "verify_credential",
    "write_store",
]

# ---------------------------------------------------------------------------
# Vocabulary. Declared so a typo is an error instead of a new status word.
# ---------------------------------------------------------------------------

#: Serialized-store version. A store written by a different version is
#: REPORTED, never silently reset: overwriting bytes nobody can read is how a
#: user loses every credential they had.
SCHEMA_VERSION = 1

AUTH_FILENAME = "auth.json"
PROVIDERS_FILENAME = "providers.json"

#: POSIX owner-only. On Windows ``os.chmod`` only toggles the read-only bit,
#: so the value is a best effort and the receipt reports the mode it asked for
#: and the mode the filesystem reported.
AUTH_FILE_MODE = 0o600

#: At most this many providers are offered. A menu of thirty is a menu nobody
#: scans, and the honest way to hide a provider is a smaller table the operator
#: controls, not a code edit.
MAX_PROVIDERS_SHOWN = 8

#: The provider id that always sorts LAST and always survives the cap. It is
#: the escape hatch: without it, "my company's gateway" would be unreachable
#: on a machine that already has eight named providers.
OTHER_PROVIDER_ID = "other"
OTHER_PROVIDER_NAME = "Other (any OpenAI-compatible endpoint)"
_OTHER_PRIORITY = 10_000

#: Every way a credential can be obtained. A stored entry carries exactly one
#: of these in its ``method`` field, and the field — not the presence of a key
#: string — is what the readers dispatch on. That is what makes
#: "no key needed" distinguishable from "the key is missing".
AUTH_METHODS = ("api_key", "api_base", "env", "none")

#: Every reason a verification can fail. A closed set means
#: ``Classification(kind=...)`` cannot become a thirteenth status word.
FAILURE_KINDS = (
    "timeout",
    "auth",
    "permission",
    "rate_limit",
    "model_not_found",
    "unavailable",
    "network",
    "not_installed",
    "empty_response",
    "cancelled",
    "unknown",
)

#: Substrings that may never appear in anything this module hands a surface.
#: Checked case-insensitively, and asserted by the suite over a corpus of real
#: provider failures.
FORBIDDEN_SUBSTRINGS = (
    "litellm",
    "traceback",
    "most recent call last",
    'file "',
    "site-packages",
    "api_key=",
    "authorization:",
    "bearer ",
)

#: A Python exception class name: ``Timeout``, ``APITimeoutError``,
#: ``AuthenticationError``, ``RateLimitError``, ... A sentence is prose and
#: never contains one, so a CamelCase class name in user-facing output is a
#: leak by construction rather than by spelling list.
_EXCEPTION_CLASS_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Timeout|Interrupt)\b"
)

#: ``{env:VAR}`` — the only shape a config file may use for a secret.
_ENV_REF_RE = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")

_PROBE_TIMEOUT_DEFAULT_S = 60
_PROBE_ENV = "NEO_CONNECT_PROBE_TIMEOUT_S"
_SETTINGS_KEY_TIMEOUT = "auth_verify_timeout_s"
_DETAIL_MAX_CHARS = 160

_counter = 0


def _next_id() -> int:
    """A per-process unique suffix for temp file names."""
    global _counter
    _counter += 1
    return _counter


class StoreCorrupt(RuntimeError):
    """The credential store could not be read.

    Raised instead of resetting. A reset is indistinguishable from data loss
    to the person who owned the keys, so the store is left exactly as it was
    and the caller reports a sentence.
    """


# ---------------------------------------------------------------------------
# Provider table. DATA, not code.
# ---------------------------------------------------------------------------


def _row(
    pid: str,
    name: str,
    *,
    methods: Sequence[str],
    priority: int,
    default_model: str = "",
    key_url: str = "",
    base_url: str = "",
    route: str = "",
    env_key: str = "",
    models: Sequence[str] = (),
    note: str = "",
) -> Dict[str, Any]:
    """One provider row. A row is a dict so a JSON file is the same shape."""
    return {
        "id": pid,
        "name": name,
        "methods": tuple(methods),
        "priority": int(priority),
        "default_model": default_model,
        "key_url": key_url,
        "base_url": base_url,
        "route": route,
        "env_key": env_key,
        "models": tuple(models),
        "note": note,
    }


#: The shipped provider table. Adding a provider is an entry HERE or a row in
#: ``<neo_home>/providers.json`` — never a branch in the flow.
PROVIDERS: Tuple[Dict[str, Any], ...] = (
    _row(
        "openai",
        "OpenAI",
        methods=("api_key",),
        priority=10,
        default_model="gpt-4o-mini",
        key_url="https://platform.openai.com/api-keys",
        route="openai",
        env_key="OPENAI_API_KEY",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    ),
    _row(
        "anthropic",
        "Anthropic",
        methods=("api_key",),
        priority=20,
        default_model="claude-3-5-haiku-20241022",
        key_url="https://console.anthropic.com/settings/keys",
        route="anthropic",
        env_key="ANTHROPIC_API_KEY",
        models=("claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"),
    ),
    _row(
        "gemini",
        "Google Gemini",
        methods=("api_key",),
        priority=30,
        default_model="gemini-2.0-flash",
        key_url="https://aistudio.google.com/app/apikey",
        route="gemini",
        env_key="GEMINI_API_KEY",
        models=("gemini-2.0-flash", "gemini-1.5-pro"),
    ),
    _row(
        "openrouter",
        "OpenRouter",
        methods=("api_key",),
        priority=40,
        default_model="openai/gpt-4o-mini",
        key_url="https://openrouter.ai/keys",
        base_url="https://openrouter.ai/api/v1",
        route="openai",
        env_key="OPENROUTER_API_KEY",
        models=("openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"),
        note="Hundreds of models on one key. Free tiers are often busy.",
    ),
    _row(
        "tokenrouter",
        "TokenRouter",
        methods=("api_key",),
        priority=50,
        default_model="z-ai/glm-5.3-free",
        key_url="https://tokenrouter.io",
        base_url="https://api.tokenrouter.com/v1",
        route="openai",
        env_key="TOKENROUTER_API_KEY",
        models=("z-ai/glm-5.3-free",),
    ),
    _row(
        "agentrouter",
        "AgentRouter",
        methods=("api_key",),
        priority=60,
        default_model="gpt-4o-mini",
        key_url="https://agentrouter.org",
        base_url="https://agentrouter.org/v1",
        route="openai",
        env_key="AGENTROUTER_API_KEY",
    ),
    _row(
        "groq",
        "Groq",
        methods=("api_key",),
        priority=65,
        default_model="llama-3.3-70b-versatile",
        key_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        route="openai",
        env_key="GROQ_API_KEY",
        note="Very fast free tier; good for a first working key.",
    ),
    _row(
        "local",
        "Ollama (on this machine)",
        methods=("none",),
        priority=70,
        default_model="qwen2.5",
        key_url="https://ollama.com/download",
        base_url="http://localhost:11434/v1",
        route="openai",
        models=("qwen2.5", "llama3.1", "mistral"),
        note="No key. Nothing you type leaves this machine.",
    ),
    _row(
        OTHER_PROVIDER_ID,
        OTHER_PROVIDER_NAME,
        methods=("api_base",),
        priority=_OTHER_PRIORITY,
        default_model="",
        key_url="",
        route="openai",
        note="Any endpoint that speaks the OpenAI chat API.",
    ),
)


@dataclass(frozen=True)
class Provider:
    """One offered provider. Built from a table row, never from a branch."""

    id: str
    name: str
    methods: Tuple[str, ...]
    priority: int
    default_model: str = ""
    key_url: str = ""
    base_url: str = ""
    route: str = ""
    env_key: str = ""
    models: Tuple[str, ...] = ()
    note: str = ""
    source: str = "built-in"

    @property
    def needs_key(self) -> bool:
        """True when this provider's declared method is not keyless."""
        return "none" not in self.methods

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "methods": list(self.methods),
            "priority": self.priority,
            "default_model": self.default_model,
            "key_url": self.key_url,
            "base_url": self.base_url,
            "route": self.route,
            "env_key": self.env_key,
            "models": list(self.models),
            "note": self.note,
            "source": self.source,
        }


def _provider_from_row(row: Mapping[str, Any], *, source: str) -> Optional[Provider]:
    """Build a :class:`Provider` from a table row, or ``None`` if unusable.

    A row that cannot be read is DROPPED with a reason rather than raising:
    a typo in somebody's providers.json must not take the whole menu away.
    """
    if not isinstance(row, Mapping):
        return None
    pid = str(row.get("id") or "").strip()
    if not pid:
        return None
    raw_methods = row.get("methods") or ("api_key",)
    if isinstance(raw_methods, str):
        raw_methods = [raw_methods]
    methods = tuple(
        m for m in (str(x).strip() for x in raw_methods) if m in AUTH_METHODS
    )
    if not methods:
        methods = ("api_key",)
    try:
        priority = int(row.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    models = row.get("models") or ()
    if isinstance(models, str):
        models = (models,)
    return Provider(
        id=normalise_provider_id(pid) or pid,
        name=str(row.get("name") or pid),
        methods=methods,
        priority=priority,
        default_model=str(row.get("default_model") or ""),
        key_url=str(row.get("key_url") or ""),
        base_url=str(row.get("base_url") or ""),
        route=str(row.get("route") or ""),
        env_key=str(row.get("env_key") or ""),
        models=tuple(str(m) for m in models),
        note=str(row.get("note") or ""),
        source=source,
    )


def _user_provider_rows_text(text: str) -> Tuple[List[Provider], List[str]]:
    """Parse a ``providers.json`` document. Returns ``(providers, problems)``.

    A malformed document yields a problem and no rows: the shipped table
    still loads, because "your provider list is broken" must not mean "you
    cannot connect to anything".
    """
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return [], [
            f"{PROVIDERS_FILENAME} could not be read ({type(exc).__name__}); "
            "using the built-in list"
        ]
    if isinstance(raw, Mapping):
        rows = raw.get("providers")
    else:
        rows = raw
    if not isinstance(rows, (list, tuple)):
        return [], [
            f"{PROVIDERS_FILENAME} must hold a list of providers, or an object "
            "with a 'providers' list"
        ]
    built: List[Provider] = []
    problems: List[str] = []
    for index, row in enumerate(rows, start=1):
        provider = _provider_from_row(row, source="user")
        if provider is None:
            problems.append(f"{PROVIDERS_FILENAME} row {index} has no id; row ignored")
            continue
        built.append(provider)
    return built, problems


def _user_provider_rows(
    home: Optional[Path] = None,
) -> Tuple[List[Provider], List[str]]:
    """Read ``<neo_home>/providers.json``, merged OVER the shipped table."""
    target = _providers_path(home)
    if not target.is_file():
        return [], []
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [
            f"{PROVIDERS_FILENAME} could not be read ({type(exc).__name__}); "
            "using the built-in list"
        ]
    return _user_provider_rows_text(text)


def provider_list(*, home: Optional[Path] = None) -> Tuple[Provider, ...]:
    """EVERY offered provider, sorted by priority then name. No cap.

    The cap belongs to :func:`provider_prompt`, the MENU, not to the table:
    a provider hidden from the picker must still be connectable by name, or
    the eight-row rule would quietly delete Groq for everyone whose machine
    already had seven others.
    """
    extra, _problems = _user_provider_rows(home)
    merged: Dict[str, Provider] = {}
    for row in PROVIDERS:
        provider = _provider_from_row(row, source="built-in")
        if provider is not None:
            merged[provider.id] = provider
    for provider in extra:
        merged[provider.id] = provider
    return tuple(
        sorted(
            merged.values(),
            key=lambda p: (1 if p.id == OTHER_PROVIDER_ID else 0, p.priority, p.name),
        )
    )


def provider_prompt(
    *, limit: int = MAX_PROVIDERS_SHOWN, home: Optional[Path] = None
) -> Tuple[Provider, ...]:
    """The rows a picker shows: at most ``limit``, and ``other`` always kept.

    The cap drops the LOWEST priority real provider, never ``other``: a table
    that can push the escape hatch off the menu is a table that can lock a
    user out of their own gateway.
    """
    ordered = list(provider_list(home=home))
    cap = max(1, int(limit))
    others = [p for p in ordered if p.id == OTHER_PROVIDER_ID]
    real = [p for p in ordered if p.id != OTHER_PROVIDER_ID]
    kept = real[: max(0, cap - len(others))] + others
    return tuple(kept[:cap])


def provider_by_id(
    provider_id: str, *, home: Optional[Path] = None
) -> Optional[Provider]:
    """Resolve a provider id or a display name to a row, or ``None``."""
    wanted = normalise_provider_id(provider_id)
    if not wanted:
        return None
    rows = provider_list(home=home)
    for provider in rows:
        if provider.id == wanted:
            return provider
    lowered = str(provider_id or "").strip().lower()
    for provider in rows:
        if provider.name.lower() == lowered:
            return provider
    return None


def needs_method_choice(provider: Optional[Provider]) -> bool:
    """True only when the provider declares MORE THAN ONE auth method.

    This is the whole of requirement 6: a provider with one method has no
    decision to make, and a step that asks a question with one answer is the
    clutter this round exists to remove.
    """
    if provider is None:
        return False
    return len(tuple(provider.methods)) > 1


def method_prompt(provider: Optional[Provider]) -> Optional[Tuple[str, ...]]:
    """The auth methods to offer, or ``None`` when the step must be skipped."""
    if not needs_method_choice(provider):
        return None
    assert provider is not None
    return tuple(provider.methods)


def provider_id_for_endpoint(base_url: str) -> str:
    """A stable provider id for an unlisted endpoint, from its host.

    Trailing slashes are removed here rather than at the call site, so
    ``https://gw.example/v1/`` and ``https://gw.example/v1`` are ONE key and
    writing the second cannot silently create a second credential.
    """
    raw = str(base_url or "").strip()
    if not raw:
        return OTHER_PROVIDER_ID
    try:
        host = (urlsplit(raw).hostname or "").strip().lower()
    except ValueError:
        host = ""
    host = host.rstrip(".")
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return "custom-local" if host else OTHER_PROVIDER_ID
    slug = re.sub(r"[^a-z0-9._-]+", "-", host).strip("-")
    return slug or OTHER_PROVIDER_ID


# ---------------------------------------------------------------------------
# Secrets: referenced, never inlined.
# ---------------------------------------------------------------------------


def is_env_ref(value: Any) -> bool:
    """True when ``value`` is exactly a ``{env:VAR}`` reference."""
    return bool(_ENV_REF_RE.match(str(value or "").strip()))


def secret_source(value: Any) -> str:
    """Where a stored secret comes from: ``env:NAME`` or ``literal``.

    Printed on the status line instead of the secret, so a user can tell a
    broken reference from a broken key without the key ever being displayed.
    """
    text = str(value or "").strip()
    match = _ENV_REF_RE.match(text)
    if match:
        return f"env:{match.group(1)}"
    return "literal"


def resolve_secret(value: Any, environ: Optional[Mapping[str, str]] = None) -> str:
    """Resolve a stored secret, following a ``{env:VAR}`` reference.

    A reference whose variable is unset resolves to ``""`` — never to the
    literal text ``{env:VAR}``, which would be sent to a provider as a key.
    """
    env = os.environ if environ is None else environ
    text = str(value or "").strip()
    match = _ENV_REF_RE.match(text)
    if match:
        return str(env.get(match.group(1), "") or "")
    return text


def mask_key(value: Any, *, keep: int = 4) -> str:
    """A masked form of a key for a status line. Never the whole secret.

    A ``{env:VAR}`` reference is displayed AS the reference: there is no
    secret to mask, and printing one would be inventing a fact.
    """
    text = str(value or "").strip()
    if not text:
        return "(not set)"
    if is_env_ref(text):
        return text
    if len(text) <= keep:
        return "*" * len(text)
    return f"{text[:3]}…{text[-keep:]}"


# ---------------------------------------------------------------------------
# The store.
# ---------------------------------------------------------------------------


def _neo_home() -> Path:
    """``<neo_home>`` via the shared resolver, with a local fallback."""
    try:
        from memory.paths import neo_home

        return Path(neo_home())
    except Exception:
        base = os.environ.get("NEO_HOME") or os.environ.get("HARNESS_HOME")
        if base:
            return Path(base)
        return Path.home() / ".neo"


def auth_path(home: Optional[Path] = None) -> Path:
    """``<neo_home>/auth.json`` — the ONE credential store."""
    return (
        Path(home) / AUTH_FILENAME if home is not None else _neo_home() / AUTH_FILENAME
    )


def _providers_path(home: Optional[Path] = None) -> Path:
    return (
        Path(home) / PROVIDERS_FILENAME
        if home is not None
        else _neo_home() / PROVIDERS_FILENAME
    )


def _chmod_private(path: Path) -> int:
    """Ask for owner-only and report the mode the filesystem gave back."""
    try:
        os.chmod(path, AUTH_FILE_MODE)
    except OSError:
        pass
    try:
        return int(path.stat().st_mode & 0o777)
    except OSError:
        return -1


@dataclass(frozen=True)
class Credential:
    """One stored credential. ``method`` is the discriminator, always."""

    provider: str
    method: str
    secret: str = ""
    base_url: str = ""
    model: str = ""
    route: str = ""
    label: str = ""
    saved_at: float = 0.0
    verified: Optional[bool] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "secret": self.secret,
            "base_url": self.base_url,
            "model": self.model,
            "route": self.route,
            "label": self.label,
            "saved_at": self.saved_at,
            "verified": self.verified,
        }

    @classmethod
    def from_json(cls, provider_id: str, raw: Any) -> "Credential":
        data = raw if isinstance(raw, Mapping) else {}
        method = str(data.get("method") or "api_key")
        if method not in AUTH_METHODS:
            method = "api_key"
        verified = data.get("verified")
        return cls(
            provider=normalise_provider_id(provider_id) or str(provider_id),
            method=method,
            secret=str(data.get("secret") or ""),
            base_url=str(data.get("base_url") or ""),
            model=str(data.get("model") or ""),
            route=str(data.get("route") or ""),
            label=str(data.get("label") or ""),
            saved_at=float(data.get("saved_at") or 0.0),
            verified=(None if verified is None else bool(verified)),
        )

    def to_dict(self) -> Dict[str, Any]:
        """A SAFE projection: the masked key and its source, never the secret.

        This is what every display and every ``--json`` document is built
        from. The literal exists only in :meth:`to_json`, which is what
        :func:`write_store` calls — so adding a machine-readable surface
        cannot become the place a key escapes.
        """
        return {
            "provider": self.provider,
            "method": self.method,
            "key": self.masked(),
            "secret_source": secret_source(self.secret),
            "base_url": self.base_url,
            "model": self.model,
            "route": self.route,
            "label": self.label,
            "saved_at": self.saved_at,
            "verified": self.verified,
        }

    def resolved_secret(self, environ: Optional[Mapping[str, str]] = None) -> str:
        return resolve_secret(self.secret, environ)

    def masked(self) -> str:
        return mask_key(self.secret)


@dataclass(frozen=True)
class Credentials:
    """The whole store, read once. ``corrupt`` means "report, do not reset"."""

    providers: Tuple[Credential, ...] = ()
    active: Optional[str] = None
    path: Path = field(default_factory=Path)
    ok: bool = True
    corrupt: bool = False
    error: str = ""
    problems: Tuple[str, ...] = ()
    mode: int = -1

    def by_id(self, provider_id: str) -> Optional[Credential]:
        wanted = normalise_provider_id(provider_id)
        for credential in self.providers:
            if credential.provider == wanted:
                return credential
        return None

    def ids(self) -> Tuple[str, ...]:
        return tuple(c.provider for c in self.providers)

    def active_credential(self) -> Optional[Credential]:
        if not self.active:
            return None
        return self.by_id(self.active)


def normalise_provider_id(provider_id: Any) -> str:
    """Normalise a provider KEY: trimmed, trailing slashes removed.

    Trailing slashes are removed because a base URL and a bare host both end
    up as provider keys, and ``gw.example/`` must not be a second credential
    beside ``gw.example``. Leading slashes are removed for the same reason.
    Case is PRESERVED: two ids differing only in case may be two different
    gateways, and merging them would drop a credential.
    """
    text = str(provider_id or "").strip()
    text = text.rstrip("/").lstrip("/")
    return text.strip()


def _empty_store() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "active": None, "providers": {}}


def read_store(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read and validate ``auth.json``. Raises :class:`StoreCorrupt`.

    A missing file is an empty store — that is first run, not corruption. A
    file that exists and cannot be read is corruption, and the bytes are left
    exactly where they are.
    """
    target = Path(path) if path is not None else auth_path()
    if not target.exists():
        return _empty_store()
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise StoreCorrupt(
            f"{AUTH_FILENAME} could not be read ({type(exc).__name__}); "
            "nothing was changed"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StoreCorrupt(
            f"{AUTH_FILENAME} is not valid JSON; nothing was changed. "
            "Move it aside to start over."
        ) from exc
    if not isinstance(data, dict):
        raise StoreCorrupt(
            f"{AUTH_FILENAME} does not hold a credential object; nothing was changed"
        )
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StoreCorrupt(
            f"{AUTH_FILENAME} was written by a different version "
            f"(found {version!r}, this build reads {SCHEMA_VERSION}); nothing was changed"
        )
    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise StoreCorrupt(
            f"{AUTH_FILENAME} has no 'providers' object; nothing was changed"
        )
    return data


def write_store(data: Mapping[str, Any], path: Optional[Path] = None) -> Path:
    """Write the store atomically, asking for owner-only permissions.

    A unique temp name plus ``os.replace`` means a crashed write leaves the
    previous store intact, and two concurrent writers cannot interleave bytes
    inside the file.
    """
    target = Path(path) if path is not None else auth_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(data), indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{_next_id()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _chmod_private(tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():  # pragma: no cover - only on a failed write
            try:
                tmp.unlink()
            except OSError:
                pass
    _chmod_private(target)
    return target


def load_credentials(
    *, path: Optional[Path] = None, home: Optional[Path] = None
) -> Credentials:
    """Read the store into a report. Never raises, never writes.

    ``ok=False`` with ``corrupt=True`` is the whole-file failure; ``problems``
    carries per-entry damage that was skipped so the rest of the store is
    still usable. Nothing here resets anything.
    """
    target = Path(path) if path is not None else auth_path(home)
    try:
        data = read_store(target)
    except StoreCorrupt as exc:
        return Credentials(
            providers=(),
            active=None,
            path=target,
            ok=False,
            corrupt=True,
            error=str(exc),
        )
    problems: List[str] = []
    entries: List[Credential] = []
    for raw_id, raw_entry in sorted((data.get("providers") or {}).items()):
        provider_id = normalise_provider_id(raw_id)
        if not provider_id:
            problems.append("a stored entry with an empty provider id was skipped")
            continue
        if not isinstance(raw_entry, Mapping):
            problems.append(f"{provider_id}: entry is not an object; entry skipped")
            continue
        method = str(raw_entry.get("method") or "")
        if method not in AUTH_METHODS:
            problems.append(
                f"{provider_id}: unknown auth method {method or '(missing)'!r}; entry skipped"
            )
            continue
        entries.append(Credential.from_json(provider_id, raw_entry))
    active = normalise_provider_id(data.get("active") or "")
    if active and active not in {entry.provider for entry in entries}:
        problems.append(
            f"the remembered active provider {active!r} is not in the store"
        )
        active = ""
    mode = -1
    if target.exists():
        mode = _chmod_private(target)
    return Credentials(
        providers=tuple(entries),
        active=active or None,
        path=target,
        ok=True,
        corrupt=False,
        problems=tuple(problems),
        mode=mode,
    )


def _store_lock(target: Path):
    """A cross-process exclusive lock over one store file.

    The credential store is read-modify-write PER KEY, so two writers can
    lose each other's provider. This reuses ``cli.neoconfig.settings_lock``
    rather than inventing a second lock implementation: it is the same
    sibling ``O_EXCL`` file, the same stale-owner takeover, and it is already
    the tree's tested answer to "two Neo processes, one config file".
    """
    from cli.neoconfig import settings_lock

    return settings_lock(target)


def save_credential(
    credential: Credential, *, path: Optional[Path] = None, activate: bool = True
) -> Credentials:
    """Persist ONE provider. Raises :class:`StoreCorrupt` rather than reset.

    Only ``providers[credential.provider]`` is assigned. Every other key in
    the document is carried through from the read, so writing provider N
    cannot disturb providers 1..N-1 — the property a second login used to
    have no way to guarantee. The whole read-modify-write runs under ONE
    cross-process lock, so a concurrent writer cannot lose a provider.
    """
    if credential.method not in AUTH_METHODS:
        raise ValueError(f"unknown auth method: {credential.method!r}")
    provider_id = normalise_provider_id(credential.provider)
    if not provider_id:
        raise ValueError("a credential needs a provider id")
    target = Path(path) if path is not None else auth_path()
    with _store_lock(target):
        data = read_store(target)  # raises StoreCorrupt; the file is untouched
        providers = dict(data.get("providers") or {})
        stored = replace(
            credential,
            provider=provider_id,
            saved_at=credential.saved_at or time.time(),
        )
        providers[provider_id] = stored.to_json()
        data["providers"] = providers
        if activate or not data.get("active"):
            data["active"] = provider_id
        data["schema_version"] = SCHEMA_VERSION
        write_store(data, target)
    return load_credentials(path=target)


def remove_credential(provider_id: str, *, path: Optional[Path] = None) -> Credentials:
    """Remove EXACTLY ONE provider and report what remains.

    The report is the point: a logout that says "logged out" without saying
    what is still connected is a claim the user has to check by hand.
    """
    target = Path(path) if path is not None else auth_path()
    with _store_lock(target):
        data = read_store(target)
        providers = dict(data.get("providers") or {})
        wanted = normalise_provider_id(provider_id)
        if wanted in providers:
            providers.pop(wanted, None)
            data["providers"] = providers
            if normalise_provider_id(data.get("active") or "") == wanted:
                data["active"] = next(iter(sorted(providers)), None)
            write_store(data, target)
    return load_credentials(path=target)


def list_credentials(
    *, path: Optional[Path] = None, home: Optional[Path] = None
) -> Credentials:
    """The whole store, as a report. ``auth list`` renders this."""
    return load_credentials(path=path, home=home)


def active_credential(*, path: Optional[Path] = None) -> Optional[Credential]:
    """The credential a run would use, or ``None``."""
    return load_credentials(path=path).active_credential()


def set_active(provider_id: str, *, path: Optional[Path] = None) -> Credentials:
    """Point the store at a provider that is already connected."""
    target = Path(path) if path is not None else auth_path()
    data = read_store(target)
    providers = data.get("providers") or {}
    wanted = normalise_provider_id(provider_id)
    if wanted not in providers:
        raise KeyError(wanted)
    data["active"] = wanted
    write_store(data, target)
    return load_credentials(path=target)


def apply_active_credential(
    config: Optional[Mapping[str, Any]], *, path: Optional[Path] = None
) -> Dict[str, Any]:
    """Overlay the active credential onto a runtime key mapping.

    Only keys the caller has NOT set are filled, and a NEW dict is returned:
    a credential store must never mutate a task's config in place, because
    the caller cannot see that it happened. This is how a key stored in
    ``auth.json`` reaches a run without ever being written into a config file.
    """
    out: Dict[str, Any] = dict(config or {})
    credential = active_credential(path=path)
    if credential is None:
        return out
    secret = credential.resolved_secret()
    fills = (
        ("api_key", secret),
        ("api_base", credential.base_url),
        ("model", credential.model),
        ("provider", credential.route),
    )
    for key, value in fills:
        if not out.get(key) and value:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Plain-language failures. This is the core of requirement 7.
# ---------------------------------------------------------------------------


def looks_like_library_leak(text: Any) -> bool:
    """True when ``text`` would show a person a library, not a sentence.

    Three screens, deliberately independent: a known library name, a
    traceback frame, and a CamelCase exception class name. A sentence is
    prose; ``APITimeoutError`` is not, whatever surrounds it.
    """
    raw = str(text or "")
    if not raw:
        return False
    lowered = raw.lower()
    for token in FORBIDDEN_SUBSTRINGS:
        if token in lowered:
            return True
    if _EXCEPTION_CLASS_RE.search(raw):
        return True
    return '\n  File "' in raw or raw.lstrip().startswith('File "')


def _display(provider_name: str) -> str:
    """A provider's name for a sentence, without the marketing suffix."""
    name = str(provider_name or "that provider").strip()
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    return name or "that provider"


def _kind_hint(text: str) -> List[Tuple[str, str]]:
    """Ordered (substring, kind) hints, so classification is deterministic.

    Order IS the policy: an authentication failure that happens to mention a
    timeout is an authentication failure, because telling a user to "try again"
    about a key that was rejected is the advice that wastes their afternoon.
    """
    return [
        ("401", "auth"),
        ("unauthorized", "auth"),
        ("invalid api key", "auth"),
        ("incorrect api key", "auth"),
        ("invalid_api_key", "auth"),
        ("authentication", "auth"),
        ("api key", "auth"),
        ("403", "permission"),
        ("forbidden", "permission"),
        ("permission", "permission"),
        ("not allowed", "permission"),
        ("429", "rate_limit"),
        ("rate limit", "rate_limit"),
        ("too many requests", "rate_limit"),
        ("quota", "rate_limit"),
        ("model_not_found", "model_not_found"),
        ("does not exist", "model_not_found"),
        ("not found", "model_not_found"),
        ("no available channel", "unavailable"),
        ("service unavailable", "unavailable"),
        ("503", "unavailable"),
        ("502", "unavailable"),
        ("500", "unavailable"),
        ("overloaded", "unavailable"),
        ("temporarily", "unavailable"),
        ("timed out", "timeout"),
        ("timeout", "timeout"),
        ("read timed", "timeout"),
        ("connection", "network"),
        ("dns", "network"),
        ("could not connect", "network"),
        ("failed to establish", "network"),
        ("no route to host", "network"),
        ("ssl", "network"),
        ("not installed", "not_installed"),
        ("modulenotfounderror", "not_installed"),
        ("no module named", "not_installed"),
    ]


def _class_name_chain(exc: BaseException) -> str:
    """Every class name in an exception's MRO, plus its message."""
    names = [cls.__name__ for cls in type(exc).__mro__ if cls is not object]
    return " ".join(names)


def _sentence(kind: str, provider: Provider, *, timeout_s: int, model: str = "") -> str:
    """The one sentence a person reads. No class name, ever.

    Every sentence here is written to fit an 80-column terminal AFTER its
    receipt prefix. A wrapped sentence does not read as a bug, but a
    sentence that wraps because a URL was appended to it reads as a bug, so
    the "where do I get a key" pointer is its own line in
    :func:`render_verification` instead.
    """
    name = _display(provider.name)
    if kind == "timeout":
        return (
            f"{name} didn't answer in {timeout_s}s. Free-tier models are often "
            "busy — try again or pick another model."
        )
    if kind == "auth":
        # Short on purpose: this sentence is prefixed with "  check failed: "
        # and an 80-column terminal wraps the rest at an arbitrary word. The
        # key URL is a separate row in the receipt, where it cannot push a
        # half-sentence onto a second line.
        return f"{name} rejected that key. Check you copied the whole key."
    if kind == "permission":
        return (
            f"{name} accepted the key but will not serve"
            f"{' ' + model if model else ' that model'} with it. Try a model your "
            "account can reach, or a different provider."
        )
    if kind == "rate_limit":
        return (
            f"{name} is rate-limiting this key. Wait a minute, or use a "
            "provider with a free tier."
        )
    if kind == "model_not_found":
        return (
            f"{name} doesn't serve the model"
            f"{' ' + model if model else ' name'} you gave. Check the model id on "
            "the provider's own model list."
        )
    if kind == "unavailable":
        return (
            f"{name} is having trouble right now. Nothing is wrong with your key "
            "— try again in a minute, or pick another provider."
        )
    if kind == "network":
        return (
            f"Could not reach {name}. Check your internet connection, or the "
            "address if you typed one."
        )
    if kind == "not_installed":
        return (
            "The model library is not installed, so no request was made. "
            "Install it with: pip install neo-agent-cli"
        )
    if kind == "empty_response":
        return f"{name} answered with no text. Try again, or pick another model."
    if kind == "cancelled":
        return f"The check against {name} was stopped. Your key is saved."
    return (
        f"{name} could not be checked right now. Your key is saved — run "
        "`neo connect --list` to see it, or try again later."
    )


@dataclass(frozen=True)
class Classification:
    """A failure reduced to a closed kind, one sentence, and a redaction."""

    kind: str
    sentence: str
    detail: str = ""


def _fallback_provider(name: str) -> Provider:
    """A nameless provider row, so a sentence can always be built."""
    return Provider(
        id=OTHER_PROVIDER_ID,
        name=_clean(name, limit=48) or "That provider",
        methods=("api_key",),
        priority=_OTHER_PRIORITY,
        route="openai",
        source="fallback",
    )


def classify_failure(
    value: Any,
    provider: Any,
    *,
    timeout_s: int = _PROBE_TIMEOUT_DEFAULT_S,
    model: str = "",
) -> Classification:
    """Reduce ANY failure — exception object, exception string, a closed
    kind, or a provider banner — to one plain sentence.

    This is the single mapper both surfaces read, so the REPL, the TUI and
    ``neo connect`` cannot word the same timeout three different ways. It
    never raises, and it never returns a string that names a library: the
    provider's own text is classified and then DROPPED rather than sanitised,
    because a sanitiser that misses one spelling is a leak wearing a test.
    """
    row: Provider
    if isinstance(provider, Provider):
        row = provider
    else:
        found = provider_by_id(str(provider or ""))
        row = found or _fallback_provider(str(provider or ""))
    budget = int(timeout_s or _PROBE_TIMEOUT_DEFAULT_S)
    probe = _sentence("unknown", row, timeout_s=budget, model=str(model or ""))

    if value is None:
        return Classification("unknown", probe)
    if isinstance(value, Classification):
        return value
    # A caller that already reduced the failure (the probe child) passes a
    # member of FAILURE_KINDS. Re-deriving it from the word "auth" would
    # miss every hint in the table and fall through to the catch-all.
    if isinstance(value, str) and value in FAILURE_KINDS:
        return Classification(
            value, _sentence(value, row, timeout_s=budget, model=model)
        )
    if isinstance(value, BaseException):
        text = f"{_class_name_chain(value)}: {value}"
    else:
        text = str(value)
    if not text.strip():
        return Classification("unknown", probe)

    lowered = text.lower()
    for needle, kind in _kind_hint(text):
        if needle in lowered:
            return Classification(
                kind, _sentence(kind, row, timeout_s=budget, model=model)
            )
    return Classification("unknown", probe)


# ---------------------------------------------------------------------------
# Verification. A child process, so it can never block or pollute the app.
# ---------------------------------------------------------------------------

_PROBE_MARKER = "--probe"


@dataclass(frozen=True)
class Verification:
    """The receipt of a background check. Carries NO provider text."""

    ok: bool
    checked: bool
    kind: str = ""
    sentence: str = ""
    elapsed_ms: float = 0.0
    provider: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "kind": self.kind,
            "sentence": self.sentence,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "provider": self.provider,
        }


def _in_process_probe(request: Mapping[str, Any]) -> Dict[str, Any]:
    """The one live request. Only ever runs inside the probe child.

    The provider's own stdout and stderr are captured and DISCARDED, and the
    receipt is written to the interpreter's ORIGINAL stdout. That is why a
    provider banner cannot reach a user: in the parent process this code does
    not run at all.
    """
    out = sys.__stdout__
    started = time.monotonic()
    receipt: Dict[str, Any] = {
        "ok": False,
        "checked": True,
        "kind": "unknown",
        "sentence": "",
    }
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            os.environ.setdefault("LITELLM_LOG", "ERROR")
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            import litellm  # lazy: the offline path must work without it
        for attribute, value in (("suppress_debug_info", True), ("set_verbose", False)):
            try:
                setattr(litellm, attribute, value)
            except Exception:
                pass
        model = str(request.get("model") or "")
        route = str(request.get("route") or "openai")
        base = str(request.get("base_url") or "") or None
        secret = str(request.get("secret") or "")
        timeout_s = int(request.get("timeout_s") or _PROBE_TIMEOUT_DEFAULT_S)
        if not secret and base and _is_local_base(base):
            secret = "ollama"
        target = model
        if route and not model.startswith(f"{route}/"):
            target = f"{route}/{model}" if model else route
        kwargs: Dict[str, Any] = {
            "model": target,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "max_tokens": 16,
            "timeout": timeout_s,
        }
        if secret:
            kwargs["api_key"] = secret
        if base:
            kwargs["api_base"] = base
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            response = litellm.completion(**kwargs)
        text = ""
        try:
            text = str(response.choices[0].message.content or "").strip()
        except Exception:
            text = ""
        receipt = {
            "ok": bool(text),
            "checked": True,
            "kind": "" if text else "empty_response",
            "sentence": "" if text else "The endpoint answered but sent no text.",
        }
    except BaseException as exc:
        receipt = {
            "ok": False,
            "checked": True,
            "kind": "error",
            "sentence": "",
            "error": exc,
        }
    finally:
        receipt["elapsed_ms"] = (time.monotonic() - started) * 1000.0
        try:
            out.write(json.dumps({"__probe__": True, **receipt}, default=repr) + "\n")
            out.flush()
        except Exception:
            pass
    return receipt


def _is_local_base(base: Any) -> bool:
    try:
        host = (urlsplit(str(base or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _probe_request(
    credential: Credential,
    *,
    timeout_s: int,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "model": credential.model,
        "route": credential.route or "openai",
        "base_url": credential.base_url,
        "secret": credential.resolved_secret(environ),
        "timeout_s": int(timeout_s),
    }


def _subprocess_probe(request: Mapping[str, Any], timeout_s: int) -> Dict[str, Any]:
    """Run :func:`_in_process_probe` in a child with a HARD deadline.

    A kill, not a request timeout: a provider that ignores its own timeout
    must still not be able to hold the app. The child's stdout is a pipe we
    own, so nothing it prints can reach a terminal.
    """
    started = time.monotonic()
    # NOT -I: isolated mode drops the repository from sys.path, and the child
    # has to import the very module that spawned it.
    argv = [sys.executable, "-m", "cli.auth", _PROBE_MARKER]
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_s)) + 5,
            env=env,
            cwd=root,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "checked": True,
            "kind": "timeout",
            "sentence": "",
            "elapsed_ms": (time.monotonic() - started) * 1000.0,
        }
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "checked": False,
            "kind": "unavailable",
            "sentence": "",
            "error": exc,
            "elapsed_ms": (time.monotonic() - started) * 1000.0,
        }
    for line in reversed((completed.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("__probe__"):
            payload.pop("__probe__", None)
            payload.setdefault("elapsed_ms", (time.monotonic() - started) * 1000.0)
            return payload
    return {
        "ok": False,
        "checked": False,
        "kind": "unavailable",
        "sentence": "",
        "error": RuntimeError(f"probe exited {completed.returncode}"),
        "elapsed_ms": (time.monotonic() - started) * 1000.0,
    }


def verify_credential(
    credential: Credential,
    *,
    probe: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
    timeout_s: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Verification:
    """Check a stored credential. Never raises, never blocks past its budget.

    A failed check is a SENTENCE about the provider, not about a class. The
    credential it describes is untouched: this function is not allowed to
    delete anything, and it has no reference to the store.
    """
    row = provider_by_id(credential.provider)
    budget = int(timeout_s or _resolve_timeout_s())
    request = _probe_request(credential, timeout_s=budget, environ=environ)
    if not credential.model:
        return Verification(
            ok=False,
            checked=False,
            kind="model_not_found",
            sentence=(
                f"{_display(row.name if row else credential.provider)} has no model "
                "selected yet. Pick one with /connect and try again."
            ),
            provider=credential.provider,
        )
    runner = probe or _subprocess_probe
    try:
        raw = runner(request, budget)
    except BaseException as exc:
        raw = {
            "ok": False,
            "checked": True,
            "kind": "error",
            "sentence": "",
            "error": exc,
        }
    if not isinstance(raw, Mapping):
        raw = {"ok": False, "checked": True, "kind": "unknown", "sentence": ""}
    if raw.get("ok"):
        return Verification(
            ok=True,
            checked=True,
            kind="",
            sentence="",
            elapsed_ms=float(raw.get("elapsed_ms") or 0.0),
            provider=credential.provider,
        )
    kind = str(raw.get("kind") or "unknown")
    if kind == "error":
        classified = classify_failure(
            raw.get("error"), row, timeout_s=budget, model=credential.model
        )
    else:
        classified = classify_failure(
            kind if kind in FAILURE_KINDS else (raw.get("sentence") or kind),
            row,
            timeout_s=budget,
            model=credential.model,
        )
    if kind == "error" and classified.kind == "unknown":
        # The child's own sentence is the honest one when it is already plain.
        sentence = str(raw.get("sentence") or "").strip()
        if sentence and not looks_like_library_leak(sentence):
            classified = Classification(classified.kind, sentence)
    return Verification(
        ok=False,
        checked=bool(raw.get("checked", True)),
        kind=classified.kind,
        sentence=classified.sentence,
        elapsed_ms=float(raw.get("elapsed_ms") or 0.0),
        provider=credential.provider,
    )


class BackgroundCheck:
    """A verification running off the caller's thread.

    The app must never wait on a provider. This handle exists so the flow can
    say "checking…" and continue, and so a caller that genuinely wants the
    answer can ask for it with a deadline of its own.
    """

    def __init__(
        self,
        credential: Credential,
        *,
        probe: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
        timeout_s: Optional[int] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._credential = credential
        self._probe = probe
        self._timeout_s = timeout_s
        self._environ = environ
        self._result: Optional[Verification] = None
        self._thread = threading.Thread(
            target=self._run, name="neo-connect-check", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        self._result = verify_credential(
            self._credential,
            probe=self._probe,
            timeout_s=self._timeout_s,
            environ=self._environ,
        )

    def done(self) -> bool:
        return self._result is not None

    def result(self, timeout: Optional[float] = None) -> Optional[Verification]:
        """The verification, or ``None`` if it has not finished in time.

        ``None`` is a real answer, not an error: "still checking" is what a
        slow free tier looks like, and a surface that cannot say so will
        either block or lie.
        """
        if self._result is not None:
            return self._result
        self._thread.join(timeout)
        return self._result


# ---------------------------------------------------------------------------
# Config. Every knob is read, never hardcoded, and never in DEFAULTS.
# ---------------------------------------------------------------------------


def _resolve_timeout_s() -> int:
    """The verification budget: argument > env > settings > module default.

    Not in ``harness/config.py::DEFAULTS``: a value there is merged into every
    task and every eval arm, and a 60-second network probe is not behaviour
    every run in this project should have.
    """
    raw = os.environ.get(_PROBE_ENV, "").strip()
    if raw:
        try:
            return max(1, min(600, int(raw)))
        except ValueError:
            pass
    try:
        from cli import neoconfig

        settings = neoconfig.merged_settings() or {}
        value = settings.get(_SETTINGS_KEY_TIMEOUT)
        if value is not None and str(value).strip():
            return max(1, min(600, int(float(value))))
    except Exception:
        pass
    return _PROBE_TIMEOUT_DEFAULT_S


# ---------------------------------------------------------------------------
# The flow: SAVE FIRST, TEST SECOND.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectResult:
    """What a connect attempt did. ``saved`` is the load-bearing field.

    A verification that failed does NOT clear ``saved``, does not clear the
    model, and does not undo the write. The receipt says so in words, because
    the receipt is what a user reads to decide whether to retype their key.
    """

    provider: str
    provider_name: str
    method: str
    saved: bool
    path: Path
    store_error: str = ""
    model: str = ""
    masked: str = ""
    base_url: str = ""
    secret_source: str = ""
    active: Optional[str] = None
    connected: Tuple[str, ...] = ()
    verification: Optional[Verification] = None
    check: Optional[BackgroundCheck] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_name": self.provider_name,
            "method": self.method,
            "saved": self.saved,
            "path": str(self.path),
            "store_error": self.store_error,
            "model": self.model,
            "masked": self.masked,
            "base_url": self.base_url,
            "secret_source": self.secret_source,
            "active": self.active,
            "connected": list(self.connected),
            "verification": self.verification.to_dict() if self.verification else None,
            "note": self.note,
        }


def _pick_method(provider: Provider, requested: Optional[str]) -> Optional[str]:
    """Resolve the auth method. One method means the step is skipped."""
    methods = tuple(provider.methods)
    if requested:
        want = str(requested).strip().lower()
        if want not in methods:
            raise ValueError(
                f"{provider.name} does not use {want!r} auth. "
                f"Available: {', '.join(methods)}"
            )
        return want
    if len(methods) == 1:
        return methods[0]
    return None


def _mirror_to_settings(
    provider: Provider, credential: Credential, *, tier: str = "global"
) -> str:
    """Write the NON-SECRET half of a connection into the settings chain.

    A literal key is never written into a config file. The model, the route
    and the endpoint go in; the key stays in ``auth.json`` and reaches a run
    through :func:`apply_active_credential`. Returns a short note for the
    receipt, and never raises — a settings file that cannot be written is a
    degraded store, not a lost credential.
    """
    if not credential.model and not credential.base_url and not credential.route:
        return ""
    try:
        from cli import neoconfig

        if credential.model:
            neoconfig.set_tier_key(tier, "model", credential.model)
        if credential.route:
            neoconfig.set_tier_key(tier, "provider", credential.route)
        if credential.base_url:
            neoconfig.set_tier_key(tier, "base_url", credential.base_url)
        return (
            f"model and endpoint recorded in the {tier} settings (no key written there)"
        )
    except Exception as exc:
        return f"could not update the settings file ({type(exc).__name__}); the key is still saved"


def connect(
    provider_id: str,
    secret: str = "",
    *,
    method: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    label: str = "",
    store: Optional[Path] = None,
    probe: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
    background: bool = True,
    verify: bool = True,
    timeout_s: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    activate: bool = True,
) -> ConnectResult:
    """Save a credential, THEN check it. The single backend both surfaces use.

    Ordering is the fix. The write happens before any provider call, so a
    timeout, a wrong key, a dead network or a killed app all leave the user's
    input in place — the state the old flow destroyed.

    The check runs on a background thread (or inline when
    ``background=False``) against a child process, so it can neither block
    the app nor print into its terminal. ``verify=False`` skips it entirely,
    which is the only way to connect with no network at all.
    """
    row = provider_by_id(provider_id)
    endpoint = str(
        base_url if base_url is not None else row.base_url if row else "" or ""
    ).strip()
    endpoint = endpoint.rstrip("/")
    if row is not None and row.id == OTHER_PROVIDER_ID and endpoint:
        # `other` is a MENU ENTRY, not a store key. Two gateways are two
        # credentials, so the key is derived from the endpoint's host.
        row = replace(
            row,
            id=provider_id_for_endpoint(endpoint),
            name=str(provider_id)
            if str(provider_id) not in ("", OTHER_PROVIDER_ID)
            else row.name,
            source="argument",
        )
    elif row is None and base_url:
        row = Provider(
            id=provider_id_for_endpoint(base_url),
            name=str(provider_id) or OTHER_PROVIDER_NAME,
            methods=("api_base",),
            priority=500,
            default_model="",
            base_url=endpoint,
            route="openai",
            source="argument",
        )
    if row is None:
        row = provider_by_id(OTHER_PROVIDER_ID) or Provider(
            id=OTHER_PROVIDER_ID,
            name=OTHER_PROVIDER_NAME,
            methods=("api_base",),
            priority=_OTHER_PRIORITY,
        )
    if row.id == OTHER_PROVIDER_ID and not endpoint:
        return ConnectResult(
            provider=row.id,
            provider_name=row.name,
            method="",
            saved=False,
            path=store or auth_path(),
            store_error=(
                "A custom endpoint needs its address. Nothing was changed — "
                "run /connect again and type the base_url."
            ),
        )
    try:
        chosen = _pick_method(row, method)
    except ValueError as exc:
        return ConnectResult(
            provider=row.id,
            provider_name=row.name,
            method="",
            saved=False,
            path=store or auth_path(),
            store_error=str(exc),
        )
    if chosen is None:
        return ConnectResult(
            provider=row.id,
            provider_name=row.name,
            method="",
            saved=False,
            path=store or auth_path(),
            store_error=(
                f"{row.name} has more than one way to authenticate. "
                f"Pick one: {', '.join(row.methods)}"
            ),
        )
    endpoint = str(base_url if base_url is not None else row.base_url or "").strip()
    endpoint = endpoint.rstrip("/")
    discarded_key = ""
    if chosen == "none":
        # A keyless endpoint keeps NO key. Saying so is the whole point: a
        # silently discarded paste is the same class of bug as a discarded
        # credential.
        if secret:
            discarded_key = (
                "  nothing was stored for the key: this endpoint runs on this machine"
            )
        secret_value = ""
    elif chosen == "env":
        secret_value = str(secret or "").strip()
        if secret_value and not is_env_ref(secret_value):
            secret_value = "{env:" + secret_value.lstrip("{env:").rstrip("}") + "}"
    else:
        secret_value = str(secret or "").strip()
        if (
            not secret_value
            and row.env_key
            and environ is None
            and os.environ.get(row.env_key)
        ):
            secret_value = "{env:" + row.env_key + "}"
    if chosen != "none" and not secret_value:
        env_value = resolve_secret(secret_value, environ)
        if not env_value and endpoint and _is_local_base(endpoint):
            chosen = "none"
            secret_value = ""
        elif not env_value and row.needs_key:
            return ConnectResult(
                provider=row.id,
                provider_name=row.name,
                method=chosen,
                saved=False,
                path=store or auth_path(),
                store_error=(
                    f"{_clean(row.name, limit=44)} needs a key before it can be "
                    "connected. Nothing was changed."
                ),
                model=str(
                    model if model is not None else row.default_model or ""
                ).strip(),
            )
    chosen_model = str(model if model is not None else row.default_model or "").strip()
    credential = Credential(
        provider=row.id,
        method=chosen,
        secret=secret_value,
        base_url=endpoint,
        model=chosen_model,
        route=str(row.route or "openai"),
        label=str(label or row.name or ""),
        saved_at=time.time(),
        verified=None,
    )
    target = Path(store) if store is not None else auth_path()
    try:
        report = save_credential(credential, path=target, activate=activate)
    except StoreCorrupt as exc:
        return ConnectResult(
            provider=row.id,
            provider_name=row.name,
            method=chosen,
            saved=False,
            path=target,
            store_error=str(exc),
            model=chosen_model,
            masked=mask_key(secret_value),
        )
    except (OSError, ValueError) as exc:
        return ConnectResult(
            provider=row.id,
            provider_name=row.name,
            method=chosen,
            saved=False,
            path=target,
            store_error=(
                f"the key could not be saved ({type(exc).__name__}); nothing else "
                "was changed"
            ),
            model=chosen_model,
            masked=mask_key(secret_value),
        )
    note = " ".join(
        part
        for part in (
            _mirror_to_settings(row, credential),
            discarded_key.strip(),
            (
                "  local endpoint: nothing to check over a network"
                if chosen == "none" and verify
                else ""
            ),
        )
        if part
    )
    verification: Optional[Verification] = None
    check: Optional[BackgroundCheck] = None
    if verify and chosen != "none" and chosen_model:
        if background:
            check = BackgroundCheck(
                credential, probe=probe, timeout_s=timeout_s, environ=environ
            )
        else:
            verification = verify_credential(
                credential, probe=probe, timeout_s=timeout_s, environ=environ
            )
            if verification.ok:
                try:
                    stored = report.by_id(row.id)
                    if stored is not None:
                        save_credential(
                            replace(stored, verified=True), path=target, activate=False
                        )
                        report = load_credentials(path=target)
                except (StoreCorrupt, OSError, ValueError):
                    pass
    return ConnectResult(
        provider=row.id,
        provider_name=row.name,
        method=chosen,
        saved=True,
        path=target,
        model=chosen_model,
        masked=mask_key(secret_value),
        base_url=endpoint,
        secret_source=secret_source(secret_value),
        active=report.active,
        connected=report.ids(),
        verification=verification,
        check=check,
        note=note,
    )


# ---------------------------------------------------------------------------
# Rendering. Plain lines only; escaping happens at the boundary.
# ---------------------------------------------------------------------------


def markup_safe(text: Any) -> str:
    """Escape a line for rich/Textual markup.

    Every string that reaches a markup parser from data this module did not
    author — a provider name from a user JSON file, a model id typed by a
    person, a masked key — goes through here. A render failure that DELETES a
    message is worse than a render failure that shows one literally, so the
    default is to show it literally.
    """
    raw = str(text or "")
    try:
        from rich.markup import escape

        return escape(raw)
    except Exception:
        return raw.replace("[", "\\[")


def _clean(value: Any, *, limit: int = 200) -> str:
    """Collapse a data string to one bounded, printable line."""
    raw = " ".join(str(value or "").split())
    if len(raw) > limit:
        raw = raw[: max(0, limit - 1)] + "…"
    return raw


def section_lines(
    rows: Sequence[str], *, min_entries: int = 3, title: str = "", indent: str = "  "
) -> List[str]:
    """Render a section, or NOTHING when it has ``min_entries`` or fewer rows.

    The anti-clutter rule: a panel with two entries is noise, and a section
    nobody needs is a section that trains people to stop reading. The
    omission is silent BY DESIGN here — a panel that rendered a heading and
    then nothing is the clutter this removes — so the callers that need the
    count state it in their own heading.
    """
    items = [row for row in rows if str(row).strip()]
    if len(items) < max(1, int(min_entries)):
        return []
    lines = [f"{title} ({len(items)})"] if title else []
    lines.extend(f"{indent}{item}" for item in items)
    return lines


def render_provider_menu(
    providers: Optional[Sequence[Provider]] = None,
    *,
    limit: int = MAX_PROVIDERS_SHOWN,
    home: Optional[Path] = None,
) -> List[str]:
    """The provider picker. At most ``limit`` rows, ``other`` always last."""
    rows = (
        list(providers)
        if providers is not None
        else list(provider_prompt(limit=limit, home=home))
    )
    if not rows:
        return ["No providers are configured. Add one to <neo_home>/providers.json."]
    lines = ["Connect a provider:"]
    for index, provider in enumerate(rows, start=1):
        bits = [f"  {index}. {_clean(provider.name, limit=44)}"]
        if provider.default_model:
            bits.append(f"model {provider.default_model}")
        if provider.note:
            bits.append(_clean(provider.note, limit=64))
        if provider.key_url:
            bits.append(f"key: {provider.key_url}")
        lines.append("  ".join(bits))
    lines.append("  Type a number, a provider name, or /skip to keep working offline.")
    return lines


def render_method_prompt(provider: Optional[Provider]) -> List[str]:
    """``Select auth method`` — and NOTHING when the provider has one method."""
    methods = method_prompt(provider)
    if not methods or provider is None:
        return []
    lines = [f"{_clean(provider.name, limit=44)} can authenticate more than one way:"]
    for index, method in enumerate(methods, start=1):
        if method == "none":
            detail = "nothing to type — it runs on this machine"
        elif method == "env":
            detail = f"read from an environment variable ({provider.env_key or 'any'})"
        elif method == "api_base":
            detail = "an endpoint address plus an optional key"
        else:
            detail = "a key you paste"
        lines.append(f"  {index}. {method} — {detail}")
    return lines


#: Failures whose advice is "get a different or valid key", so the
#: "where do I get one" pointer is worth a row of its own.
_KEY_ADVICE_KINDS = frozenset({"auth", "permission", "rate_limit", "model_not_found"})


def render_verification(verification: Optional[Verification]) -> List[str]:
    """Two or three lines, never the provider's own words.

    The key URL is a separate line rather than a clause in the sentence: a
    sentence that wraps because a URL was appended to it reads as a bug on an
    80-column terminal, and this is the surface a person reads when their key
    did not work.
    """
    if verification is None:
        return []
    if verification.ok:
        return [f"  checked: the endpoint answered ({verification.elapsed_ms:.0f} ms)"]
    if not verification.checked:
        return [f"  not checked: {verification.sentence}"]
    lines = [f"  check failed: {verification.sentence}"]
    if verification.kind in _KEY_ADVICE_KINDS:
        row = provider_by_id(verification.provider)
        if row is not None and row.key_url:
            lines.append(f"  get a key: {row.key_url}")
    return lines


def render_connect_result(result: ConnectResult) -> List[str]:
    """The receipt. It says SAVED first, and never says the key was lost."""
    if not result.saved:
        lines = [f"  not saved: {result.store_error}"]
        return lines
    lines = [
        f"  saved: {_clean(result.provider_name, limit=44)} "
        f"({result.method}) → {result.path.name}"
    ]
    if result.model:
        lines.append(f"  model: {result.model}")
    if result.base_url:
        lines.append(f"  endpoint: {result.base_url}")
    lines.append(f"  key: {result.masked} (from {result.secret_source})")
    if result.note:
        lines.append(f"  {result.note}")
    if result.verification is not None:
        lines.extend(render_verification(result.verification))
    elif result.check is not None:
        pending = "  checking in the background — your key is already saved"
        if result.method == "none":
            pending = "  local endpoint: nothing to check over a network"
        lines.append(pending)
    return lines


def render_status(credentials: Optional[Credentials] = None) -> List[str]:
    """The status block: what is connected, what is active, and the model.

    The per-provider list obeys the anti-clutter rule — two or fewer
    connections is not a list, it is a fact, and the fact is the first line.
    """
    report = credentials if credentials is not None else load_credentials()
    if report.corrupt:
        return [f"  credential store unreadable: {report.error}"]
    if not report.providers:
        return ["  no provider connected — type /connect when you want one"]
    active = report.active_credential()
    if active is not None:
        row = provider_by_id(active.provider)
        name = _clean(row.name if row else active.provider, limit=40)
        bits = [f"  connected: {name}"]
        if active.model:
            bits.append(f"model {active.model}")
        bits.append(f"key {active.masked()}")
        head = "  ".join(bits)
    else:
        head = "  connected: (none selected — /connect picks one)"
    lines = [head]
    others = [
        c for c in report.providers if active is None or c.provider != active.provider
    ]
    lines.extend(
        section_lines(
            [
                f"{_clean(c.provider)} · {c.model or 'no model'} · {c.masked()}"
                for c in others
            ],
            min_entries=3,
            title="also connected",
        )
    )
    lines.extend(f"  note: {problem}" for problem in report.problems[:3])
    return lines


def status_line(credentials: Optional[Credentials] = None) -> str:
    """One line naming the active credential and its model, key masked."""
    report = credentials if credentials is not None else load_credentials()
    if report.corrupt:
        return "provider: (credential store unreadable)"
    active = report.active_credential()
    if active is None:
        count = len(report.providers)
        if not count:
            return "provider: none connected — /connect"
        return f"provider: none selected ({count} connected) — /connect"
    row = provider_by_id(active.provider)
    name = _clean(row.name if row else active.provider, limit=40)
    return f"provider: {name} · {active.model or 'no model'} · {active.masked()}"


def render_list(credentials: Optional[Credentials] = None) -> List[str]:
    """``auth list`` — answers the question it was asked."""
    report = credentials if credentials is not None else load_credentials()
    if report.corrupt:
        return [
            f"credential store unreadable: {report.error}",
            "",
            "Nothing was reset.",
        ]
    if not report.providers:
        return ["No providers connected yet. Type /connect to add one."]
    lines = [status_line(report), ""]
    for credential in report.providers:
        marker = "*" if credential.provider == report.active else " "
        verified = (
            "checked"
            if credential.verified is True
            else ("check failed" if credential.verified is False else "not checked")
        )
        # One bounded line, no padded columns: a padded table wraps inside an
        # 80-column terminal and turns a status read into a two-line scroll.
        lines.append(
            f" {marker} "
            + " · ".join(
                (
                    _clean(credential.provider, limit=24),
                    credential.method,
                    _clean(credential.model or "(no model)", limit=36),
                    credential.masked(),
                    verified,
                )
            )
        )
    lines.append("* = the credential a run would use")
    lines.extend(f"note: {problem}" for problem in report.problems[:5])
    lines.append(f"store: {report.path}")
    return lines


def first_run_hint() -> str:
    """The entire first-run surface: one line, no prompt, no wizard.

    The app is fully usable with no credential at all — offline work, /help,
    /diff, session history. A first run that must answer a question before it
    can be used is a first run that some people never get past. One line, and
    it fits an 80-column terminal without wrapping, because a wrapped hint
    reads as two sentences and the second one is not a sentence.
    """
    return (
        "Get started: /connect adds a provider. Skipping is fine — neo works offline."
    )


# ---------------------------------------------------------------------------
# The interactive flow. One backend; the REPL and the TUI render its lines.
# ---------------------------------------------------------------------------


def _ask(
    prompt: str,
    *,
    default: str = "",
    input_fn: Optional[Callable[[str], str]] = None,
    getpass_fn: Optional[Callable[[str], str]] = None,
    mask: bool = False,
) -> Optional[str]:
    """One line of input.

    Returns ``None`` only for a skip word or EOF, so a caller can tell "the
    user pressed enter" (``""`` or ``default``) from "the user wants out"
    (``None``). Collapsing those two is how a flow either traps somebody in
    a prompt or silently discards what they typed.
    """
    fn = getpass_fn if mask else (input_fn or input)
    show = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        raw = str(fn(show) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw.lower() in ("/skip", "skip", "/q", "q", "quit", "exit"):
        return None
    return raw or default


def connect_interactive(
    *,
    provider_id: str = "",
    store: Optional[Path] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    getpass_fn: Optional[Callable[[str], str]] = None,
    say: Optional[Callable[[str], None]] = None,
    probe: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
    verify: bool = True,
    background: bool = False,
    timeout_s: Optional[int] = None,
    limit: int = MAX_PROVIDERS_SHOWN,
) -> Optional[ConnectResult]:
    """The whole picker-to-receipt flow, driven by injectable input.

    Returns the :class:`ConnectResult`, or ``None`` when the user skipped.
    Every string it emits is PLAIN; a surface prints them through
    :func:`markup_safe`.

    The flow is deliberately short — pick, method (only when there is a
    choice), secret, model — because the old one lost a user's key to a
    thirty-second probe and a question they had already answered correctly.
    """
    emit = say or (lambda line: print(line))
    rows = provider_prompt(limit=limit)
    for line in render_provider_menu(rows, limit=limit):
        emit(line)
    chosen_id = str(provider_id or "").strip()
    if not chosen_id:
        answer = _ask("Pick", input_fn=input_fn)
        if answer is None:
            emit("  skipped — nothing was changed. /connect runs any time.")
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(rows):
            chosen_id = rows[int(answer) - 1].id
        else:
            match = provider_by_id(answer)
            if match is None:
                emit(f"  {answer!r} is not on the list; using the custom entry.")
                chosen_id = OTHER_PROVIDER_ID
            else:
                chosen_id = match.id
    row = provider_by_id(chosen_id) or provider_by_id(OTHER_PROVIDER_ID)
    if row is None:
        emit("  no providers are configured; nothing was changed.")
        return None

    method = row.methods[0]
    for line in render_method_prompt(row):
        emit(line)
    if needs_method_choice(row):
        pick = _ask("Auth method", default=row.methods[0], input_fn=input_fn)
        if pick is None:
            emit("  skipped — nothing was changed.")
            return None
        method = pick.strip().lower() if pick.strip() else row.methods[0]
        if method not in row.methods:
            emit(
                f"  {method!r} is not one of {', '.join(row.methods)}; using {row.methods[0]}."
            )
            method = row.methods[0]

    endpoint = row.base_url
    if method == "api_base" or row.id == OTHER_PROVIDER_ID:
        endpoint = (
            _ask("Endpoint (base_url)", default=endpoint, input_fn=input_fn) or endpoint
        )
    secret = ""
    if method != "none":
        env_hint = row.env_key
        if env_hint and os.environ.get(env_hint):
            answer = _ask(
                f"Key — press Enter to reuse ${env_hint}, paste a key, or /skip",
                input_fn=input_fn,
            )
            if answer is None:
                emit("  skipped — nothing was changed.")
                return None
            secret = answer.strip() or "{env:" + env_hint + "}"
        else:
            secret = _ask(
                f"Key for {_clean(row.name, limit=40)}"
                + (f" ({row.key_url})" if row.key_url else ""),
                input_fn=input_fn,
                getpass_fn=getpass_fn,
                mask=True,
            )
            if secret is None:
                emit("  skipped — nothing was changed.")
                return None
    model = row.default_model
    if model:
        model = _ask("Model", default=model, input_fn=input_fn) or model
    result = connect(
        row.id,
        secret or "",
        method=method,
        base_url=(endpoint or "").rstrip("/") or None,
        model=model or "",
        store=store,
        probe=probe,
        verify=verify,
        background=background,
        timeout_s=timeout_s,
    )
    for line in render_connect_result(result):
        emit(line)
    return result


# ---------------------------------------------------------------------------
# The CLI surface. `neo connect`, `neo auth list`, `neo auth logout <p>`.
# ---------------------------------------------------------------------------


def _emit_json(document: Mapping[str, Any], console: Any) -> None:
    console.print_json(json.dumps(dict(document), default=str))


def _say(console: Any) -> Callable[[str], None]:
    def _emit(line: str) -> None:
        console.print(markup_safe(line))

    return _emit


def cmd_connect(args: Any) -> int:
    """`neo connect [provider]` — the CLI half of the same backend.

    Exit codes follow the module contract: 0 the credential is saved, 1 a
    usage problem, 2 the store could not be written, 4 a verification that
    failed. A failed check is 4 and NOT a lost key: the store is written
    before the check and the receipt says so.
    """
    from cli import ui

    console = ui.console()
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "list", False):
        return cmd_auth_list(args)
    provider_id = str(getattr(args, "provider", "") or "").strip()
    base_url = str(getattr(args, "base_url", "") or "").strip() or None
    try:
        interactive_stdin = bool(sys.stdin.isatty())
    except Exception:
        interactive_stdin = False
    if not provider_id and not base_url and not interactive_stdin:
        ui.err_console().print(
            "[neo.error]error: `neo connect` needs a provider name, "
            "--base-url, or an interactive terminal[/]"
        )
        return 2
    key = str(getattr(args, "api_key", "") or "").strip()
    if not key and str(getattr(args, "env", "") or "").strip():
        key = "{env:" + str(args.env).strip().lstrip("{env:").rstrip("}") + "}"
    row = provider_by_id(provider_id) if provider_id else None
    if not key and not as_json and not (row is not None and row.methods == ("none",)):
        asked = _ask(
            f"Key for {_clean(row.name if row else (provider_id or 'the endpoint'), limit=40)}",
            getpass_fn=getpass.getpass,
            mask=True,
        )
        if asked is None:
            ui.err_console().print("nothing was changed")
            return 1
        key = asked
    result = connect(
        provider_id or OTHER_PROVIDER_ID,
        key,
        method=str(getattr(args, "method", "") or "").strip() or None,
        base_url=base_url,
        model=str(getattr(args, "model", "") or "").strip() or None,
        store=None,
        verify=not bool(getattr(args, "no_verify", False)),
        background=False,
    )
    if as_json:
        _emit_json(result.to_dict(), console)
        if not result.saved:
            return 2
        return 0 if (result.verification is None or result.verification.ok) else 4
    for line in render_connect_result(result):
        _say(console)(line)
    if not result.saved:
        return 2
    if result.verification is not None and not result.verification.ok:
        return 4
    return 0


def cmd_auth_list(args: Any) -> int:
    """`neo auth list` — what is connected, with every key masked."""
    from cli import ui

    console = ui.console()
    report = load_credentials()
    if bool(getattr(args, "json", False)):
        _emit_json(
            {
                "path": str(report.path),
                "corrupt": report.corrupt,
                "error": report.error,
                "active": report.active,
                "problems": list(report.problems),
                "providers": [c.to_dict() for c in report.providers],
            },
            console,
        )
        return 0
    for line in render_list(report):
        _say(console)(line)
    return 1 if report.corrupt else 0


def cmd_auth_logout(args: Any) -> int:
    """`neo auth logout <provider>` — remove exactly one, report the rest.

    Presence is checked BEFORE the removal, because afterwards "was not
    connected" and "just removed it" produce the same empty store and only
    one of them is a refusal.
    """
    from cli import ui

    console = ui.console()
    provider_id = str(getattr(args, "provider", "") or "").strip()
    if not provider_id:
        ui.err_console().print("[neo.error]error: name the provider to log out of[/]")
        return 2
    wanted = normalise_provider_id(provider_id)
    try:
        before = load_credentials()
    except Exception:
        before = Credentials()
    if before.corrupt:
        ui.err_console().print(f"[neo.error]error: {markup_safe(before.error)}[/]")
        return 2
    if wanted not in before.ids():
        ui.err_console().print(
            f"[neo.error]error: {markup_safe(provider_id)} is not connected "
            f"(connected: {', '.join(before.ids()) or 'none'})[/]"
        )
        return 2
    try:
        report = remove_credential(provider_id)
    except StoreCorrupt as exc:
        ui.err_console().print(f"[neo.error]error: {markup_safe(str(exc))}[/]")
        return 2
    except OSError as exc:
        ui.err_console().print(
            f"[neo.error]error: the store could not be written ({type(exc).__name__})[/]"
        )
        return 2
    if bool(getattr(args, "json", False)):
        _emit_json(
            {
                "removed": wanted,
                "remaining": list(report.ids()),
                "active": report.active,
                "path": str(report.path),
            },
            console,
        )
        return 0
    _say(console)(f"  removed {markup_safe(provider_id)} from {report.path.name}")
    remaining = report.ids()
    if remaining:
        _say(console)("  still connected: " + ", ".join(remaining))
    else:
        _say(console)("  nothing is connected now — neo still works offline")
    return 0


def register_connect_parser(sub: Any) -> Any:
    """Register `neo connect` and `neo auth` on the shared subparser.

    Called from ``cli/main.py``'s parser body. It lives here so the command
    surface travels with the backend: a parser row and the handler it calls
    cannot drift apart if they are written in the same place.
    """

    connect_parser = sub.add_parser(
        "connect",
        help="add or replace a model provider credential (saves first, checks after)",
    )
    connect_parser.add_argument(
        "provider", nargs="?", default="", help="provider id or name"
    )
    connect_parser.add_argument(
        "--base-url", dest="base_url", default="", help="OpenAI-compatible endpoint"
    )
    connect_parser.add_argument(
        "--model", dest="model", default="", help="model id to use"
    )
    connect_parser.add_argument(
        "--method", dest="method", default="", help="api_key | api_base | env | none"
    )
    connect_parser.add_argument(
        "--api-key",
        dest="api_key",
        default="",
        help="the key (a literal is stored only in auth.json)",
    )
    connect_parser.add_argument(
        "--env",
        dest="env",
        default="",
        help="read the key from this environment variable",
    )
    connect_parser.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        help="save without checking",
    )
    connect_parser.add_argument(
        "--list", dest="list", action="store_true", help="list what is connected"
    )
    connect_parser.add_argument(
        "--json", dest="json", action="store_true", help="one JSON document"
    )
    connect_parser.set_defaults(func=cmd_connect)

    auth_parser = sub.add_parser(
        "auth", help="inspect or remove stored provider credentials"
    )
    auth_sub = auth_parser.add_subparsers(dest="auth_action")
    auth_list = auth_sub.add_parser(
        "list", help="list connected providers (keys masked)"
    )
    auth_list.add_argument(
        "--json", dest="json", action="store_true", help="one JSON document"
    )
    auth_list.set_defaults(func=cmd_auth_list)
    auth_logout = auth_sub.add_parser("logout", help="remove exactly one provider")
    auth_logout.add_argument("provider", help="the provider id to remove")
    auth_logout.add_argument(
        "--json", dest="json", action="store_true", help="one JSON document"
    )
    auth_logout.set_defaults(func=cmd_auth_logout)
    auth_parser.set_defaults(func=cmd_auth_list, json=False)
    return connect_parser


# ---------------------------------------------------------------------------
# Child-process entry point: `python -m cli.auth --probe`
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m cli.auth`` runs the probe child. Nothing else."""
    args = list(sys.argv[1:] if argv is None else argv)
    if _PROBE_MARKER in args:
        try:
            request = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            request = {}
        _in_process_probe(request if isinstance(request, dict) else {})
        return 0
    sys.stderr.write("cli.auth is an internal module; use `neo connect`.\n")
    return 2


if __name__ == "__main__":  # pragma: no cover - the child-process path
    raise SystemExit(main())
