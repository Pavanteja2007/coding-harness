"""OAuth 2.0 authorization-code with PKCE support for MCP HTTP adapters."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
)
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from .mcp import MCPAuthenticationError

__all__ = [
    "PKCE",
    "AtomicFileTokenStorage",
    "AuthenticationRequiredError",
    "FileTokenStorage",
    "InMemoryOAuthStateStore",
    "InMemoryTokenStorage",
    "MemoryTokenStorage",
    "OAuthAuthenticationRequiredError",
    "OAuthAuthorization",
    "OAuthCallback",
    "OAuthClient",
    "OAuthConfig",
    "OAuthConfigurationError",
    "OAuthError",
    "OAuthManager",
    "OAuthPKCEClient",
    "OAuthPKCEProvider",
    "OAuthState",
    "OAuthStateError",
    "OAuthStateExpiredError",
    "OAuthStateMismatchError",
    "OAuthStateReplayError",
    "OAuthStateStore",
    "OAuthStorageError",
    "OAuthToken",
    "OAuthTokenError",
    "OAuthTokenProvider",
    "OAuthTokenRequest",
    "OAuthTokenStorage",
    "PKCEPair",
    "TokenStorage",
    "TokenStore",
    "generate_pkce",
    "pkce_challenge",
]

_MAX_ERROR_CHARS = 512
_MAX_URL_CHARS = 4_096
_MAX_TOKEN_CHARS = 16_384
_MAX_AUTHORIZATION_RECORDS = 128
_SECRET_MARKERS = (
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "credential",
)
_SECRET_PATTERN = r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|secret|credential)\s*[=:]\s*[^\s,;]+|sk-[A-Za-z0-9_-]{6,})"


def _redact(value: Any, limit: int = _MAX_ERROR_CHARS) -> str:
    """Return bounded text with common credential forms removed."""
    import re

    text = str(value or "")
    text = re.sub(_SECRET_PATTERN, "[REDACTED_SECRET]", text)
    text = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|secret|credential)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+",
        r"\1[REDACTED_SECRET]",
        text,
    )
    marker = "...[truncated]"
    if len(text) > limit:
        if limit <= len(marker):
            return text[:limit]
        text = text[: limit - len(marker)] + marker
    return text


def _finite_positive(value: Any, default: float, maximum: float = 86_400.0) -> float:
    """Normalize a positive finite duration with a hard upper bound."""
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError):
        result = float(default)
    if result != result or result in (float("inf"), float("-inf")):
        result = float(default)
    return max(0.001, min(result, maximum))


def _scope_tuple(value: Any) -> tuple[str, ...]:
    """Normalize an OAuth scope string or sequence into a bounded tuple."""
    if value is None:
        return ()
    values = value.split() if isinstance(value, str) else value
    if not isinstance(values, Sequence):
        values = (values,)
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text[:256])
    return tuple(result[:64])


def _safe_url(value: str, name: str, *, allow_custom: bool = False) -> str:
    """Validate an absolute endpoint or redirect URI without echoing secrets."""
    text = str(value or "").strip()
    parsed = urlparse(text)
    if len(text) > _MAX_URL_CHARS or not parsed.scheme:
        raise ValueError(f"{name} must be an absolute URI")
    if parsed.fragment:
        raise ValueError(f"{name} must not contain a fragment")
    scheme = parsed.scheme.lower()
    if allow_custom and scheme in {"javascript", "data", "file", "vbscript"}:
        raise ValueError(f"{name} uses a forbidden URI scheme")
    if not allow_custom and scheme not in {"http", "https"}:
        raise ValueError(f"{name} must use http or https")
    if not allow_custom and not parsed.netloc:
        raise ValueError(f"{name} must contain a host")
    if (not allow_custom and parsed.username) or (not allow_custom and parsed.password):
        raise ValueError(f"{name} must not contain URL credentials")
    if any(
        marker in key.lower()
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        for marker in ("token", "secret", "password", "credential", "api_key")
    ):
        raise ValueError(f"{name} must not contain secret query values")
    return text


def _safe_headers(headers: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """Copy bounded HTTP headers while preventing control characters."""
    if headers is None:
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key)
        text = str(value)
        if not name or any(ord(char) < 32 for char in name + text):
            raise ValueError("OAuth headers contain invalid control characters")
        if len(name) > 256 or len(text) > 8_192:
            raise ValueError("OAuth header is too large")
        result[name] = text
    return result


class OAuthError(Exception):
    """Base class for safe OAuth failures."""


class OAuthConfigurationError(OAuthError, ValueError):
    """Report invalid OAuth client or endpoint configuration."""


class OAuthStateError(OAuthError):
    """Base class for authorization-state validation failures."""


class OAuthStateMismatchError(OAuthStateError):
    """Report an authorization callback with an unknown or mismatched state."""


class OAuthStateExpiredError(OAuthStateError):
    """Report an authorization callback after its state TTL."""


class OAuthStateReplayError(OAuthStateError):
    """Report reuse of an already consumed authorization state."""


class OAuthCallbackError(OAuthError):
    """Report a malformed, unbound, or rejected authorization callback."""


class OAuthTokenError(OAuthError):
    """Report a bounded token exchange or refresh failure."""


class OAuthStorageError(OAuthError):
    """Report a bounded token-storage failure."""


class OAuthAuthenticationRequiredError(MCPAuthenticationError, OAuthError):
    """Require user authorization while retaining a safe authorization URL."""

    def __init__(
        self,
        authorization_url: Optional[str] = None,
        message: str = "OAuth authorization is required",
    ) -> None:
        """Create an error whose normal representation never contains URL secrets."""
        MCPAuthenticationError.__init__(
            self,
            message,
            authorization_url=authorization_url,
        )

    def __repr__(self) -> str:
        """Return a representation that omits the authorization URL."""
        return "OAuthAuthenticationRequiredError()"


AuthenticationRequiredError = OAuthAuthenticationRequiredError


@dataclass(frozen=True, init=False)
class OAuthConfig:
    """Immutable OAuth client configuration for an MCP HTTP resource."""

    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scope: tuple[str, ...] = ()
    state_ttl_s: float = 600.0
    token_ttl_s: float = 3_600.0
    refresh_skew_s: float = 30.0
    max_pending_authorizations: int = _MAX_AUTHORIZATION_RECORDS
    max_tokens: int = _MAX_AUTHORIZATION_RECORDS
    resource: Optional[str] = None
    issuer: Optional[str] = None
    client_secret: Optional[str] = field(default=None, repr=False)
    token_endpoint_auth_method: str = "none"
    extra_authorization_params: Mapping[str, str] = field(
        default_factory=dict, repr=False
    )
    extra_token_params: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __init__(
        self,
        authorization_endpoint: str = "",
        token_endpoint: str = "",
        client_id: str = "",
        redirect_uri: str = "",
        scope: Any = (),
        state_ttl_s: float = 600.0,
        token_ttl_s: float = 3_600.0,
        refresh_skew_s: float = 30.0,
        max_pending_authorizations: int = _MAX_AUTHORIZATION_RECORDS,
        max_tokens: int = _MAX_AUTHORIZATION_RECORDS,
        resource: Optional[str] = None,
        issuer: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_endpoint_auth_method: str = "none",
        extra_authorization_params: Optional[Mapping[str, Any]] = None,
        extra_token_params: Optional[Mapping[str, Any]] = None,
        *,
        authorization_url: Optional[str] = None,
        token_url: Optional[str] = None,
        auth_endpoint: Optional[str] = None,
        client: Optional[str] = None,
        redirect: Optional[str] = None,
        scopes: Any = None,
        state_ttl: Optional[float] = None,
    ) -> None:
        """Create a validated immutable OAuth configuration."""
        endpoint = authorization_endpoint or authorization_url or auth_endpoint
        token = token_endpoint or token_url
        identifier = client_id or client or ""
        callback = redirect_uri or redirect or ""
        try:
            endpoint_value = _safe_url(endpoint, "authorization_endpoint")
            token_value = _safe_url(token, "token_endpoint")
            callback_value = _safe_url(callback, "redirect_uri", allow_custom=True)
        except ValueError as exc:
            raise OAuthConfigurationError(_redact(exc)) from None
        identifier_text = str(identifier).strip()
        if not identifier_text:
            raise OAuthConfigurationError("client_id must not be empty")
        if len(identifier_text) > 512:
            raise OAuthConfigurationError("client_id exceeds the supported bound")
        method = str(token_endpoint_auth_method or "none").strip()
        if method not in {"none", "client_secret_basic", "client_secret_post"}:
            raise OAuthConfigurationError("unsupported token_endpoint_auth_method")
        auth_params = _safe_headers(extra_authorization_params)
        token_params = _safe_headers(extra_token_params)
        reserved_auth = {
            "state",
            "code_challenge",
            "code_challenge_method",
            "client_id",
            "redirect_uri",
            "response_type",
        }
        if reserved_auth.intersection(auth_params):
            raise OAuthConfigurationError(
                "reserved authorization parameters cannot be overridden"
            )
        if any(
            marker in key.lower()
            for key in auth_params
            for marker in ("token", "secret", "password", "credential", "api_key")
        ):
            raise OAuthConfigurationError(
                "secret authorization parameters are not allowed"
            )
        object.__setattr__(self, "authorization_endpoint", endpoint_value)
        object.__setattr__(self, "token_endpoint", token_value)
        object.__setattr__(self, "client_id", identifier_text)
        object.__setattr__(self, "redirect_uri", callback_value)
        object.__setattr__(
            self, "scope", _scope_tuple(scopes if scopes is not None else scope)
        )
        object.__setattr__(
            self,
            "state_ttl_s",
            _finite_positive(
                state_ttl if state_ttl is not None else state_ttl_s, 600.0
            ),
        )
        object.__setattr__(
            self, "token_ttl_s", _finite_positive(token_ttl_s, 3_600.0, 31_536_000.0)
        )
        object.__setattr__(
            self, "refresh_skew_s", _finite_positive(refresh_skew_s, 30.0, 86_400.0)
        )
        object.__setattr__(
            self,
            "max_pending_authorizations",
            max(1, min(int(max_pending_authorizations), 1_024)),
        )
        object.__setattr__(self, "max_tokens", max(1, min(int(max_tokens), 1_024)))
        object.__setattr__(
            self, "resource", _safe_url(resource, "resource") if resource else None
        )
        object.__setattr__(
            self, "issuer", _safe_url(issuer, "issuer") if issuer else None
        )
        object.__setattr__(
            self, "client_secret", str(client_secret) if client_secret else None
        )
        object.__setattr__(self, "token_endpoint_auth_method", method)
        object.__setattr__(
            self, "extra_authorization_params", MappingProxyType(auth_params)
        )
        object.__setattr__(self, "extra_token_params", MappingProxyType(token_params))

    def __repr__(self) -> str:
        """Return a representation with the client secret and parameters omitted."""
        return (
            f"OAuthConfig(client_id={self.client_id!r}, redirect_uri={_redact(self.redirect_uri)!r}, "
            f"scope={self.scope!r}, token_endpoint={_redact(self.token_endpoint)!r})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a diagnostic projection without secrets or token values."""
        return {
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": list(self.scope),
            "state_ttl_s": self.state_ttl_s,
            "token_ttl_s": self.token_ttl_s,
            "resource": self.resource,
            "issuer": self.issuer,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
        }


@dataclass(frozen=True, init=False)
class OAuthToken:
    """An immutable OAuth token record with redacted public representations."""

    access_token: str = field(repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    token_type: str = "Bearer"
    expires_at: Optional[float] = None
    scope: tuple[str, ...] = ()
    issued_at: float = field(default_factory=time.time)

    def __init__(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        token_type: str = "Bearer",
        expires_in: Optional[float] = None,
        expires_at: Optional[float] = None,
        scope: Any = (),
        issued_at: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Create a token record and derive an absolute expiry when possible."""
        access = str(access_token or "")
        if not access:
            raise ValueError("access_token must not be empty")
        if len(access) > _MAX_TOKEN_CHARS:
            raise ValueError("access token exceeds the supported bound")
        now = float((clock or time.time)())
        expiry = float(expires_at) if expires_at is not None else None
        if expiry is None and expires_in is not None:
            try:
                expiry = now + max(0.0, float(expires_in))
            except (TypeError, ValueError):
                expiry = None
        refresh = str(refresh_token) if refresh_token else None
        if refresh is not None and len(refresh) > _MAX_TOKEN_CHARS:
            raise ValueError("refresh token exceeds the supported bound")
        object.__setattr__(self, "access_token", access)
        object.__setattr__(self, "refresh_token", refresh)
        object.__setattr__(self, "token_type", str(token_type or "Bearer")[:64])
        object.__setattr__(self, "expires_at", expiry)
        object.__setattr__(self, "scope", _scope_tuple(scope))
        object.__setattr__(
            self, "issued_at", now if issued_at is None else float(issued_at)
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, clock: Optional[Callable[[], float]] = None
    ) -> "OAuthToken":
        """Build a token from a standards-style snake_case or camelCase response."""
        access = value.get("access_token", value.get("accessToken"))
        refresh = value.get("refresh_token", value.get("refreshToken"))
        token_type = value.get("token_type", value.get("tokenType", "Bearer"))
        expires_in = value.get("expires_in", value.get("expiresIn"))
        return cls(
            str(access or ""),
            str(refresh) if refresh else None,
            str(token_type or "Bearer"),
            expires_in,
            scope=value.get("scope", ()),
            clock=clock,
        )

    def is_expired(self, now: Optional[float] = None, skew_s: float = 0.0) -> bool:
        """Return whether the access token is expired at the supplied wall time."""
        if self.expires_at is None:
            return False
        current = float(time.time() if now is None else now)
        return current >= self.expires_at - max(0.0, float(skew_s))

    def to_storage_dict(self) -> dict[str, Any]:
        """Return the private persistence representation containing token values."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": list(self.scope),
            "issued_at": self.issued_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return a diagnostic representation with all token values removed."""
        return {
            "token_type": self.token_type,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "expires_at": self.expires_at,
            "scope": list(self.scope),
        }

    def __repr__(self) -> str:
        """Return a safe representation that never prints token values."""
        return f"OAuthToken(token_type={self.token_type!r}, has_refresh_token={bool(self.refresh_token)!r})"


@dataclass(frozen=True, init=False)
class PKCEPair:
    """A PKCE verifier and its S256 challenge."""

    code_verifier: str = field(repr=False)
    code_challenge: str = field(repr=False)

    def __init__(
        self, code_verifier: str, code_challenge: Optional[str] = None
    ) -> None:
        """Create a standards-length PKCE pair."""
        verifier = str(code_verifier or "")
        if not 43 <= len(verifier) <= 128:
            raise ValueError("PKCE code verifier must contain 43 to 128 characters")
        challenge = code_challenge or pkce_challenge(verifier)
        if not 43 <= len(challenge) <= 128:
            raise ValueError("PKCE code challenge must contain 43 to 128 characters")
        object.__setattr__(self, "code_verifier", verifier)
        object.__setattr__(self, "code_challenge", challenge)

    def __repr__(self) -> str:
        """Return a safe representation without verifier material."""
        return "PKCEPair(code_challenge='[REDACTED_SECRET]')"


PKCE = PKCEPair


def pkce_challenge(code_verifier: str) -> str:
    """Return the unpadded base64url SHA-256 challenge for a verifier."""
    verifier = str(code_verifier or "")
    if not 43 <= len(verifier) <= 128:
        raise ValueError("PKCE code verifier must contain 43 to 128 characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_pkce() -> PKCEPair:
    """Generate a cryptographically random RFC 7636 S256 PKCE pair."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(128))
    return PKCEPair(verifier, pkce_challenge(verifier))


def _safe_authorization_url(value: Optional[str]) -> Optional[str]:
    """Validate, bound, and redact secret-shaped authorization query values."""
    if value is None:
        return None
    text = str(value)
    parsed = urlparse(text)
    if len(text) > _MAX_URL_CHARS or not parsed.scheme or parsed.fragment:
        raise OAuthConfigurationError("authorization URL is invalid")
    query = []
    redacted = False
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if any(
            marker in key.lower()
            for marker in ("token", "secret", "password", "credential", "api_key")
        ):
            item = "[REDACTED_SECRET]"
            redacted = True
        query.append((key, item))
    if not redacted:
        return text
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), "")
    )


@dataclass(frozen=True, init=False)
class OAuthAuthorization:
    """The operational result of starting an authorization request."""

    authorization_url: str = field(repr=False)
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    state_digest: str
    session_id: str
    client_id: str
    expires_at: float

    def __init__(
        self,
        authorization_url: str,
        state: str,
        code_verifier: str,
        state_digest: str,
        session_id: str,
        client_id: str,
        expires_at: float,
    ) -> None:
        """Create an authorization result with private transient PKCE fields."""
        safe_url = _safe_authorization_url(str(authorization_url))
        if safe_url is None:
            raise OAuthConfigurationError("authorization URL is invalid")
        state_text = str(state)
        verifier_text = str(code_verifier)
        if not state_text or len(state_text) > 1_024:
            raise OAuthConfigurationError("authorization state is invalid")
        if not 43 <= len(verifier_text) <= 128:
            raise OAuthConfigurationError("PKCE verifier is invalid")
        object.__setattr__(self, "authorization_url", safe_url)
        object.__setattr__(self, "state", state_text)
        object.__setattr__(self, "code_verifier", verifier_text)
        object.__setattr__(self, "state_digest", str(state_digest))
        object.__setattr__(self, "session_id", str(session_id))
        object.__setattr__(self, "client_id", str(client_id))
        object.__setattr__(self, "expires_at", float(expires_at))

    @property
    def code_challenge(self) -> str:
        """Return the S256 challenge associated with the private verifier."""
        return pkce_challenge(self.code_verifier)

    @property
    def challenge(self) -> str:
        """Return an alias for :attr:`code_challenge`."""
        return self.code_challenge

    def to_diagnostic_dict(self) -> dict[str, Any]:
        """Return diagnostics containing only a state fingerprint."""
        return {
            "state_digest": self.state_digest,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "expires_at": self.expires_at,
        }

    def __repr__(self) -> str:
        """Return a safe representation without URL state or PKCE material."""
        return f"OAuthAuthorization(state_digest={self.state_digest!r}, session_id={self.session_id!r})"


OAuthState = OAuthAuthorization


@dataclass(frozen=True, init=False)
class OAuthCallback:
    """A validated authorization callback and its one-time PKCE verifier."""

    code: str = field(repr=False)
    code_verifier: str = field(repr=False)
    state_digest: str
    session_id: str
    client_id: str
    redirect_uri: str

    def __init__(
        self,
        code: str,
        code_verifier: str,
        state_digest: str,
        session_id: str,
        client_id: str,
        redirect_uri: str,
    ) -> None:
        """Create a validated callback without including it in diagnostics."""
        if not code:
            raise OAuthCallbackError("authorization code is missing")
        code_text = str(code)
        if len(code_text) > _MAX_TOKEN_CHARS:
            raise OAuthCallbackError("authorization code exceeds the supported bound")
        object.__setattr__(self, "code", code_text)
        object.__setattr__(self, "code_verifier", str(code_verifier))
        object.__setattr__(self, "state_digest", str(state_digest))
        object.__setattr__(self, "session_id", str(session_id))
        object.__setattr__(self, "client_id", str(client_id))
        object.__setattr__(self, "redirect_uri", str(redirect_uri))

    def to_diagnostic_dict(self) -> dict[str, Any]:
        """Return callback diagnostics without code, state, or verifier."""
        return {
            "state_digest": self.state_digest,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }

    def __repr__(self) -> str:
        """Return a safe representation without authorization material."""
        return f"OAuthCallback(state_digest={self.state_digest!r}, session_id={self.session_id!r})"


@dataclass(frozen=True, init=False)
class OAuthTokenRequest:
    """A redacted request description passed to an injected token exchanger."""

    grant_type: str
    token_endpoint: str
    client_id: str
    redirect_uri: Optional[str] = None
    code: Optional[str] = field(default=None, repr=False)
    code_verifier: Optional[str] = field(default=None, repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    scope: tuple[str, ...] = ()
    resource: Optional[str] = None
    timeout_s: float = 30.0

    def __init__(
        self,
        grant_type: str,
        token_endpoint: str,
        client_id: str,
        redirect_uri: Optional[str] = None,
        code: Optional[str] = None,
        code_verifier: Optional[str] = None,
        refresh_token: Optional[str] = None,
        scope: Any = (),
        resource: Optional[str] = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Create a token request with private secret-bearing fields."""
        object.__setattr__(self, "grant_type", str(grant_type))
        object.__setattr__(self, "token_endpoint", str(token_endpoint))
        object.__setattr__(self, "client_id", str(client_id))
        object.__setattr__(
            self, "redirect_uri", str(redirect_uri) if redirect_uri else None
        )
        object.__setattr__(self, "code", str(code) if code else None)
        object.__setattr__(
            self, "code_verifier", str(code_verifier) if code_verifier else None
        )
        object.__setattr__(
            self, "refresh_token", str(refresh_token) if refresh_token else None
        )
        object.__setattr__(self, "scope", _scope_tuple(scope))
        object.__setattr__(self, "resource", str(resource) if resource else None)
        object.__setattr__(self, "timeout_s", _finite_positive(timeout_s, 30.0))

    def form_values(
        self, client_secret: Optional[str] = None, auth_method: str = "none"
    ) -> dict[str, str]:
        """Return form values for a real token endpoint without logging them."""
        values: dict[str, str] = {
            "grant_type": self.grant_type,
            "client_id": self.client_id,
        }
        if self.redirect_uri:
            values["redirect_uri"] = self.redirect_uri
        if self.code:
            values["code"] = self.code
        if self.code_verifier:
            values["code_verifier"] = self.code_verifier
        if self.refresh_token:
            values["refresh_token"] = self.refresh_token
        if self.scope:
            values["scope"] = " ".join(self.scope)
        if self.resource:
            values["resource"] = self.resource
        if client_secret and auth_method == "client_secret_post":
            values["client_secret"] = client_secret
        return values

    def __repr__(self) -> str:
        """Return a safe request representation without secret fields."""
        return f"OAuthTokenRequest(grant_type={self.grant_type!r}, client_id={self.client_id!r}, token_endpoint={_redact(self.token_endpoint)!r})"


class TokenStorage(Protocol):
    """Async token storage contract used by :class:`OAuthManager`."""

    async def get(self, owner: str = "default") -> Optional[OAuthToken]:
        """Return a token for an owner, or ``None`` when absent."""
        ...

    async def set(self, token: OAuthToken, owner: str = "default") -> None:
        """Persist a token for an owner."""
        ...

    async def delete(self, owner: str = "default") -> None:
        """Delete a token for an owner without exposing its value."""
        ...


TokenStore = TokenStorage


class InMemoryTokenStorage:
    """Bounded TTL-aware in-memory OAuth token storage."""

    def __init__(
        self, max_entries: int = _MAX_AUTHORIZATION_RECORDS, ttl_s: float = 86_400.0
    ) -> None:
        """Create a bounded token store keyed by a non-secret owner digest."""
        self.max_entries = max(1, min(int(max_entries), 1_024))
        self.ttl_s = _finite_positive(ttl_s, 86_400.0, 31_536_000.0)
        self._values: dict[str, tuple[OAuthToken, float]] = {}
        self._lock = threading.RLock()

    def _key(self, owner: str) -> str:
        """Return a bounded non-secret storage key for an owner."""
        return hashlib.sha256(str(owner or "default").encode("utf-8")).hexdigest()

    def _purge(self, now: float) -> None:
        """Remove expired and excess entries while preserving newest values."""
        expired = [key for key, (_, expiry) in self._values.items() if expiry <= now]
        for key in expired:
            self._values.pop(key, None)
        while len(self._values) > self.max_entries:
            oldest = min(self._values, key=lambda key: self._values[key][1])
            self._values.pop(oldest, None)

    async def get(self, owner: str = "default") -> Optional[OAuthToken]:
        """Return a live token for an owner."""
        with self._lock:
            now = time.time()
            self._purge(now)
            value = self._values.get(self._key(owner))
            return value[0] if value else None

    async def set(self, token: OAuthToken, owner: str = "default") -> None:
        """Store a token with a bounded TTL and evict the oldest entries."""
        if not isinstance(token, OAuthToken):
            raise TypeError("token must be OAuthToken")
        with self._lock:
            self._values[self._key(owner)] = (token, time.time() + self.ttl_s)
            self._purge(time.time())

    async def delete(self, owner: str = "default") -> None:
        """Remove a token without returning or logging its value."""
        with self._lock:
            self._values.pop(self._key(owner), None)

    async def load(self, owner: str = "default") -> Optional[OAuthToken]:
        """Alias for :meth:`get`."""
        return await self.get(owner)

    async def store(self, token: OAuthToken, owner: str = "default") -> None:
        """Alias for :meth:`set`."""
        await self.set(token, owner)

    async def clear(self) -> None:
        """Remove all in-memory token records."""
        with self._lock:
            self._values.clear()

    def __repr__(self) -> str:
        """Return storage metadata without token values or owner identifiers."""
        with self._lock:
            count = len(self._values)
        return f"InMemoryTokenStorage(entries={count}, max_entries={self.max_entries})"


class FileTokenStorage:
    """Atomic, size-bounded file token storage with restrictive permissions."""

    def __init__(
        self,
        path: Union[str, Path],
        max_bytes: int = 65_536,
        ttl_s: float = 86_400.0,
        max_entries: int = _MAX_AUTHORIZATION_RECORDS,
    ) -> None:
        """Create a file store whose parent and file are private by default."""
        self.path = Path(path).expanduser()
        self.max_bytes = max(1_024, min(int(max_bytes), 1_048_576))
        self.max_entries = max(1, min(int(max_entries), 1_024))
        self.ttl_s = _finite_positive(ttl_s, 86_400.0, 31_536_000.0)
        self._lock = threading.RLock()

    def _ensure_parent(self) -> None:
        """Create the private parent directory and reject symlink targets."""
        parent = self.path.parent
        parent_existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if os.name != "nt":
                mode = stat.S_IMODE(parent.stat().st_mode)
                if parent_existed and mode & 0o077:
                    raise OAuthStorageError(
                        "token storage parent permissions are too broad"
                    )
                if not parent_existed:
                    os.chmod(parent, 0o700)
        except OAuthStorageError:
            raise
        except OSError as exc:
            raise OAuthStorageError("token storage parent is unavailable") from exc
        if self.path.exists() and self.path.is_symlink():
            raise OAuthStorageError("token storage path must not be a symlink")

    def _read_unlocked(self) -> dict[str, Any]:
        """Read and validate the bounded JSON token file."""
        if self.path.is_symlink():
            raise OAuthStorageError("token storage path must not be a symlink")
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > self.max_bytes:
                raise OAuthStorageError("token storage file exceeds its bound")
            raw = self.path.read_text(encoding="utf-8")
            value = json.loads(raw)
        except OAuthStorageError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise OAuthStorageError("token storage file is unreadable") from None
        if not isinstance(value, dict) or not isinstance(value.get("tokens", {}), dict):
            raise OAuthStorageError("token storage file has an invalid shape")
        return value

    def _write_unlocked(self, value: Mapping[str, Any]) -> None:
        """Atomically replace the token file with private permissions."""
        self._ensure_parent()
        payload = json.dumps(dict(value), separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        if len(payload) > self.max_bytes:
            raise OAuthStorageError("token storage payload exceeds its bound")
        temporary: Optional[str] = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OAuthStorageError:
            raise
        except (OSError, ValueError):
            raise OAuthStorageError("token storage write failed") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _key(self, owner: str) -> str:
        """Return a non-secret owner digest for file records."""
        return hashlib.sha256(str(owner or "default").encode("utf-8")).hexdigest()

    async def get(self, owner: str = "default") -> Optional[OAuthToken]:
        """Return a token from the atomic file store."""
        with self._lock:
            data = self._read_unlocked()
            records = data.get("tokens", {})
            record = records.get(self._key(owner))
            if not isinstance(record, Mapping):
                return None
            try:
                created = float(record.get("stored_at", 0.0))
            except (TypeError, ValueError):
                raise OAuthStorageError("token storage record is invalid") from None
            if created and time.time() - created > self.ttl_s:
                return None
            token_record = record.get("token", {})
            if not isinstance(token_record, Mapping):
                raise OAuthStorageError("token storage record is invalid")
            try:
                return OAuthToken.from_mapping(token_record, clock=time.time)
            except (AttributeError, TypeError, ValueError):
                raise OAuthStorageError("token storage record is invalid") from None

    async def set(self, token: OAuthToken, owner: str = "default") -> None:
        """Atomically persist a token with a bounded TTL."""
        if not isinstance(token, OAuthToken):
            raise TypeError("token must be OAuthToken")
        with self._lock:
            data = self._read_unlocked()
            records = dict(data.get("tokens", {}))
            records[self._key(owner)] = {
                "stored_at": time.time(),
                "token": token.to_storage_dict(),
            }
            while len(records) > self.max_entries:
                oldest_key = min(
                    records,
                    key=lambda key: (
                        float(records[key].get("stored_at", 0.0))
                        if isinstance(records[key], Mapping)
                        else 0.0
                    ),
                )
                records.pop(oldest_key, None)
            self._write_unlocked({"version": 1, "tokens": records})

    async def delete(self, owner: str = "default") -> None:
        """Atomically remove one owner token."""
        with self._lock:
            data = self._read_unlocked()
            records = dict(data.get("tokens", {}))
            records.pop(self._key(owner), None)
            self._write_unlocked({"version": 1, "tokens": records})

    async def load(self, owner: str = "default") -> Optional[OAuthToken]:
        """Alias for :meth:`get`."""
        return await self.get(owner)

    async def store(self, token: OAuthToken, owner: str = "default") -> None:
        """Alias for :meth:`set`."""
        await self.set(token, owner)

    async def clear(self) -> None:
        """Atomically remove all stored tokens."""
        with self._lock:
            self._write_unlocked({"version": 1, "tokens": {}})

    def __repr__(self) -> str:
        """Return a safe storage representation without path contents."""
        return f"FileTokenStorage(path={_redact(self.path.name)!r}, max_bytes={self.max_bytes})"


@dataclass(frozen=True)
class _PendingAuthorization:
    """Private state metadata keyed by a one-way state digest."""

    state_digest: str
    code_verifier: str = field(repr=False)
    client_id: str
    session_id: str
    created_at: float
    expires_at: float


class InMemoryOAuthStateStore:
    """Bounded TTL state storage that never retains plaintext OAuth state."""

    def __init__(
        self,
        max_entries: int = _MAX_AUTHORIZATION_RECORDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Create a bounded state store with one-time consumption tracking."""
        self.max_entries = max(1, min(int(max_entries), 1_024))
        self._clock = clock or time.monotonic
        self._pending: dict[str, _PendingAuthorization] = {}
        self._consumed: dict[str, float] = {}
        self._lock = threading.RLock()

    def _purge(self, now: float) -> None:
        """Purge expired pending and consumed state digests."""
        for key, pending in tuple(self._pending.items()):
            if pending.expires_at <= now:
                self._pending.pop(key, None)
                self._consumed[key] = now + max(1.0, pending.expires_at - now)
        for key, expiry in tuple(self._consumed.items()):
            if expiry <= now:
                self._consumed.pop(key, None)
        while len(self._pending) > self.max_entries:
            oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
            self._pending.pop(oldest, None)

    def _purge_consumed(self, now: float) -> None:
        """Purge expired consumed-state markers without hiding pending expiry."""
        for key, expiry in tuple(self._consumed.items()):
            if expiry <= now:
                self._consumed.pop(key, None)
        while len(self._consumed) > self.max_entries:
            oldest = min(self._consumed, key=lambda item: self._consumed[item])
            self._consumed.pop(oldest, None)

    def create(self, pending: _PendingAuthorization) -> None:
        """Store a pending authorization by digest with bounded eviction."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            self._pending[pending.state_digest] = pending
            self._consumed.pop(pending.state_digest, None)
            while len(self._pending) > self.max_entries:
                oldest = min(
                    self._pending, key=lambda key: self._pending[key].created_at
                )
                self._pending.pop(oldest, None)

    def lookup(self, state_digest: str) -> Optional[_PendingAuthorization]:
        """Find pending state using a constant-time digest comparison."""
        with self._lock:
            self._purge_consumed(self._clock())
            for key, pending in self._pending.items():
                if hmac.compare_digest(key, str(state_digest)):
                    return pending
            return None

    def is_consumed(self, state_digest: str) -> bool:
        """Return whether a state digest was already consumed."""
        with self._lock:
            self._purge_consumed(self._clock())
            return any(
                hmac.compare_digest(key, str(state_digest)) for key in self._consumed
            )

    def consume(self, state_digest: str) -> Optional[_PendingAuthorization]:
        """Atomically consume a matching state digest exactly once."""
        with self._lock:
            now = self._clock()
            self._purge_consumed(now)
            for key, pending in tuple(self._pending.items()):
                if not hmac.compare_digest(key, str(state_digest)):
                    continue
                if pending.expires_at <= now:
                    self._pending.pop(key, None)
                    self._consumed[key] = now + max(1.0, pending.expires_at - now)
                    raise OAuthStateExpiredError("authorization state has expired")
                self._pending.pop(key, None)
                self._consumed[key] = now + max(1.0, pending.expires_at - now)
                while len(self._consumed) > self.max_entries:
                    oldest = min(self._consumed, key=lambda item: self._consumed[item])
                    self._consumed.pop(oldest, None)
                return pending
            return None

    def clear(self) -> None:
        """Remove all pending and consumed state digests."""
        with self._lock:
            self._pending.clear()
            self._consumed.clear()

    def __len__(self) -> int:
        """Return the number of currently pending state records."""
        with self._lock:
            self._purge(self._clock())
            return len(self._pending)

    def __repr__(self) -> str:
        """Return only bounded state-store counts."""
        with self._lock:
            return f"InMemoryOAuthStateStore(pending={len(self._pending)}, max_entries={self.max_entries})"


class OAuthManager:
    """Manage PKCE authorization callbacks and bounded OAuth token lifecycle."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        state_store: Optional[InMemoryOAuthStateStore] = None,
        token_storage: Optional[TokenStorage] = None,
        token_exchanger: Optional[Callable[..., Any]] = None,
        storage: Optional[TokenStorage] = None,
        exchange: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        monotonic_clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Create an OAuth manager with injectable offline exchange and clocks."""
        if not isinstance(config, OAuthConfig):
            raise TypeError("config must be OAuthConfig")
        self.config = config
        self.state_store = (
            state_store
            if state_store is not None
            else InMemoryOAuthStateStore(
                config.max_pending_authorizations,
                clock=monotonic_clock or time.monotonic,
            )
        )
        self.token_storage = (
            token_storage
            if token_storage is not None
            else storage
            if storage is not None
            else InMemoryTokenStorage(config.max_tokens)
        )
        self.token_exchanger = token_exchanger or exchange
        self._clock = clock or time.time
        self._monotonic_clock = monotonic_clock or time.monotonic

    def _now(self) -> float:
        """Return the configured wall clock."""
        return float(self._clock())

    def _monotonic(self) -> float:
        """Return the configured monotonic clock."""
        return float(self._monotonic_clock())

    async def _storage_get(self, owner: str) -> Optional[OAuthToken]:
        """Read a token from an async or injected synchronous storage."""
        try:
            result = self.token_storage.get(owner)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OAuthStorageError("token storage read failed") from None
        if result is None or isinstance(result, OAuthToken):
            return result
        if isinstance(result, Mapping):
            return self._coerce_token(result)
        raise OAuthStorageError("token storage returned an invalid token")

    async def _storage_set(self, token: OAuthToken, owner: str) -> None:
        """Write a token to an async or injected synchronous storage."""
        try:
            result = self.token_storage.set(token, owner)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OAuthStorageError("token storage write failed") from None

    def begin_authorization(
        self,
        session_id: str,
        client_id: Optional[str] = None,
        *,
        redirect_uri: Optional[str] = None,
        scope: Any = None,
        resource: Optional[str] = None,
        initiating_client: Optional[str] = None,
    ) -> OAuthAuthorization:
        """Start a state-bound S256 authorization request for a client session."""
        session = str(session_id or "").strip()
        if not session or len(session) > 512:
            raise OAuthCallbackError("initiating session is missing or too long")
        expected_client = str(client_id or initiating_client or self.config.client_id)
        if expected_client != self.config.client_id:
            raise OAuthCallbackError(
                "authorization client binding does not match configuration"
            )
        callback = str(redirect_uri or self.config.redirect_uri)
        if callback != self.config.redirect_uri:
            raise OAuthCallbackError(
                "authorization redirect binding does not match configuration"
            )
        state = secrets.token_urlsafe(32)
        verifier = "".join(
            secrets.choice(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            )
            for _ in range(128)
        )
        challenge = pkce_challenge(verifier)
        digest = hashlib.sha256(state.encode("ascii")).hexdigest()
        now = self._monotonic()
        pending = _PendingAuthorization(
            digest,
            verifier,
            self.config.client_id,
            session,
            now,
            now + self.config.state_ttl_s,
        )
        self.state_store.create(pending)
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        selected_scope = self.config.scope if scope is None else _scope_tuple(scope)
        if selected_scope:
            params["scope"] = " ".join(selected_scope)
        selected_resource = resource or self.config.resource
        if selected_resource:
            params["resource"] = selected_resource
        params.update(
            {
                str(key): str(value)
                for key, value in self.config.extra_authorization_params.items()
            }
        )
        parsed = urlparse(self.config.authorization_endpoint)
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key, value in params.items():
            query[key] = [value]
        authorization_url = urlunparse(
            parsed._replace(query=urlencode(query, doseq=True))
        )
        return OAuthAuthorization(
            authorization_url,
            state,
            verifier,
            digest,
            session,
            self.config.client_id,
            now + self.config.state_ttl_s,
        )

    def authorization_url(
        self, session_id: str, client_id: Optional[str] = None, **kwargs: Any
    ) -> OAuthAuthorization:
        """Alias for :meth:`begin_authorization`."""
        return self.begin_authorization(session_id, client_id, **kwargs)

    def initiate(
        self, session_id: str, client_id: Optional[str] = None, **kwargs: Any
    ) -> OAuthAuthorization:
        """Alias for :meth:`begin_authorization` for adapter hooks."""
        return self.begin_authorization(session_id, client_id, **kwargs)

    def start_authorization(
        self, session_id: str, client_id: Optional[str] = None, **kwargs: Any
    ) -> OAuthAuthorization:
        """Alias for :meth:`begin_authorization`."""
        return self.begin_authorization(session_id, client_id, **kwargs)

    def build_authorization_url(
        self, session_id: str, client_id: Optional[str] = None, **kwargs: Any
    ) -> OAuthAuthorization:
        """Return the authorization request and its transient PKCE material."""
        return self.begin_authorization(session_id, client_id, **kwargs)

    def get_authorization_url(
        self, session_id: str, client_id: Optional[str] = None, **kwargs: Any
    ) -> str:
        """Return only the operational authorization URL for a new request."""
        return self.begin_authorization(
            session_id, client_id, **kwargs
        ).authorization_url

    def validate_state(
        self,
        state: str,
        *,
        session_id: str,
        client_id: Optional[str] = None,
    ) -> bool:
        """Check a state digest and binding without consuming the authorization."""
        digest = hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()
        pending = self.state_store.lookup(digest)
        if pending is None or pending.expires_at <= self._monotonic():
            return False
        expected_client = str(client_id or self.config.client_id)
        return bool(
            pending.client_id == self.config.client_id
            and pending.client_id == expected_client
            and hmac.compare_digest(pending.session_id, str(session_id or ""))
        )

    def check_state(
        self, state: str, *, session_id: str, client_id: Optional[str] = None
    ) -> bool:
        """Alias for :meth:`validate_state`."""
        return self.validate_state(state, session_id=session_id, client_id=client_id)

    def _parse_callback(
        self, callback: Union[str, Mapping[str, Any]]
    ) -> tuple[dict[str, str], Optional[str]]:
        """Parse a callback URL or mapping into single-valued parameters."""
        if isinstance(callback, Mapping):
            values: dict[str, str] = {}
            for key, value in callback.items():
                if isinstance(value, (list, tuple)):
                    if len(value) != 1:
                        raise OAuthCallbackError("callback parameter is repeated")
                    value = value[0]
                values[str(key)] = str(value)
            return values, values.get("redirect_uri")
        text = str(callback or "")
        parsed = urlparse(text)
        if parsed.fragment:
            raise OAuthCallbackError("authorization callback must not use a fragment")
        raw_values = parse_qs(parsed.query, keep_blank_values=True)
        values = {}
        for key, entries in raw_values.items():
            if len(entries) != 1:
                raise OAuthCallbackError("callback parameter is repeated")
            values[key] = entries[0]
        callback_keys = {
            "code",
            "state",
            "error",
            "error_description",
            "error_uri",
            "iss",
            "client_id",
        }
        redirect_query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in callback_keys
        ]
        base = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                urlencode(redirect_query),
                "",
            )
        )
        return values, base

    def handle_callback(
        self,
        callback: Union[str, Mapping[str, Any]],
        session_id: str,
        client_id: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ) -> OAuthCallback:
        """Validate and consume one callback bound to its initiating session/client."""
        values, callback_redirect = self._parse_callback(callback)
        expected_client = str(client_id or self.config.client_id)
        if expected_client != self.config.client_id:
            raise OAuthCallbackError(
                "callback client binding does not match configuration"
            )
        callback_client = values.get("client_id")
        if callback_client is not None and callback_client != self.config.client_id:
            raise OAuthCallbackError(
                "callback client binding does not match configuration"
            )
        callback_state = values.get("state", "")
        if not callback_state or len(callback_state) > 1024:
            raise OAuthStateMismatchError("authorization state is missing or invalid")
        digest = hashlib.sha256(callback_state.encode("utf-8")).hexdigest()
        if self.state_store.is_consumed(digest):
            raise OAuthStateReplayError("authorization state was already consumed")
        pending = self.state_store.consume(digest)
        if pending is None:
            raise OAuthStateMismatchError(
                "authorization state does not match an active request"
            )
        if pending.expires_at <= self._monotonic():
            raise OAuthStateExpiredError("authorization state has expired")
        if not hmac.compare_digest(pending.state_digest, digest):
            raise OAuthStateMismatchError(
                "authorization state does not match an active request"
            )
        if (
            pending.client_id != self.config.client_id
            or pending.client_id != expected_client
        ):
            raise OAuthCallbackError("authorization client binding failed")
        if not hmac.compare_digest(pending.session_id, str(session_id or "")):
            raise OAuthCallbackError("authorization session binding failed")
        if isinstance(callback, Mapping):
            supplied_redirect = redirect_uri or callback_redirect
        else:
            supplied_redirect = callback_redirect
            if redirect_uri is not None and str(redirect_uri) != str(callback_redirect):
                raise OAuthCallbackError("authorization redirect binding failed")
        if (
            supplied_redirect is not None
            and str(supplied_redirect) != self.config.redirect_uri
        ):
            raise OAuthCallbackError("authorization redirect binding failed")
        if "error" in values:
            raise OAuthCallbackError("authorization server returned an error")
        issuer = values.get("iss")
        if self.config.issuer is not None and issuer != self.config.issuer:
            raise OAuthCallbackError("authorization issuer binding failed")
        code = values.get("code", "")
        if not code:
            raise OAuthCallbackError("authorization code is missing")
        return OAuthCallback(
            code,
            pending.code_verifier,
            digest,
            pending.session_id,
            pending.client_id,
            self.config.redirect_uri,
        )

    def consume_callback(
        self, callback: Union[str, Mapping[str, Any]], **kwargs: Any
    ) -> OAuthCallback:
        """Alias for :meth:`handle_callback`."""
        return self.handle_callback(callback, **kwargs)

    def validate_callback(
        self, callback: Union[str, Mapping[str, Any]], **kwargs: Any
    ) -> OAuthCallback:
        """Alias for :meth:`handle_callback` with the same one-time semantics."""
        return self.handle_callback(callback, **kwargs)

    async def _exchange(self, request: OAuthTokenRequest) -> OAuthToken:
        """Exchange or refresh a token through an injected or HTTP exchanger."""
        if self.token_exchanger is not None:
            try:
                result = self._call_exchanger(self.token_exchanger, request)
                if inspect.isawaitable(result):
                    result = await result
            except asyncio.CancelledError:
                raise
            except Exception:
                raise OAuthTokenError("token exchange failed") from None
            return self._coerce_token(result)
        try:
            import httpx
        except ImportError:
            try:
                import httpx2 as httpx
            except ImportError:
                raise OAuthTokenError(
                    "OAuth token exchange requires an HTTP client"
                ) from None
        values = request.form_values(
            self.config.client_secret, self.config.token_endpoint_auth_method
        )
        for key, value in self.config.extra_token_params.items():
            if key not in values:
                values[key] = value
        headers = {"Accept": "application/json"}
        auth = None
        if (
            self.config.token_endpoint_auth_method == "client_secret_basic"
            and self.config.client_secret
        ):
            auth = httpx.BasicAuth(self.config.client_id, self.config.client_secret)
            values.pop("client_id", None)
        try:
            async with httpx.AsyncClient(timeout=request.timeout_s) as client:
                response = await client.post(
                    self.config.token_endpoint, data=values, headers=headers, auth=auth
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OAuthTokenError("token endpoint request failed") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise OAuthTokenError(
                f"token endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception:
            raise OAuthTokenError("token endpoint returned invalid JSON") from None
        return self._coerce_token(payload)

    def _call_exchanger(
        self, exchanger: Callable[..., Any], request: OAuthTokenRequest
    ) -> Any:
        """Call an injected exchanger using common request and keyword forms."""
        candidates = (
            (request,),
            (),
        )
        try:
            signature = inspect.signature(exchanger)
        except (TypeError, ValueError):
            return exchanger(request)
        for arguments in candidates:
            try:
                signature.bind(*arguments)
            except TypeError:
                continue
            if arguments:
                return exchanger(request)
            return exchanger(
                grant_type=request.grant_type,
                token_endpoint=request.token_endpoint,
                client_id=request.client_id,
                redirect_uri=request.redirect_uri,
                code=request.code,
                code_verifier=request.code_verifier,
                refresh_token=request.refresh_token,
                scope=request.scope,
                resource=request.resource,
            )
        return exchanger(request)

    def _coerce_token(self, value: Any) -> OAuthToken:
        """Normalize an injected or HTTP token response into an OAuthToken."""
        if isinstance(value, OAuthToken):
            return value
        if not isinstance(value, Mapping):
            raise OAuthTokenError("token endpoint returned an invalid response")
        try:
            return OAuthToken.from_mapping(value, clock=self._clock)
        except (TypeError, ValueError):
            raise OAuthTokenError(
                "token endpoint response did not include an access token"
            ) from None

    async def exchange_authorization_code(
        self, callback: OAuthCallback, owner: Optional[str] = None
    ) -> OAuthToken:
        """Exchange a validated callback using its one-time PKCE verifier."""
        if not isinstance(callback, OAuthCallback):
            raise TypeError("callback must be OAuthCallback")
        if callback.client_id != self.config.client_id:
            raise OAuthCallbackError("callback client binding failed")
        request = OAuthTokenRequest(
            "authorization_code",
            self.config.token_endpoint,
            self.config.client_id,
            callback.redirect_uri,
            callback.code,
            callback.code_verifier,
            scope=self.config.scope,
            resource=self.config.resource,
        )
        token = await self._exchange(request)
        await self._storage_set(token, owner or callback.session_id)
        return token

    async def exchange_callback(
        self, callback: OAuthCallback, owner: Optional[str] = None
    ) -> OAuthToken:
        """Exchange a validated callback using the authorization-code grant."""
        return await self.exchange_authorization_code(callback, owner)

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        *,
        session_id: str = "default",
        owner: Optional[str] = None,
    ) -> OAuthToken:
        """Exchange a caller-provided code and verifier after external validation."""
        if not code or not code_verifier:
            raise OAuthCallbackError("authorization code and verifier are required")
        request = OAuthTokenRequest(
            "authorization_code",
            self.config.token_endpoint,
            self.config.client_id,
            self.config.redirect_uri,
            str(code),
            str(code_verifier),
            scope=self.config.scope,
            resource=self.config.resource,
        )
        token = await self._exchange(request)
        await self._storage_set(token, owner or session_id)
        return token

    async def refresh(
        self, owner: str = "default", *, force: bool = False
    ) -> OAuthToken:
        """Return a live token or refresh it once using the refresh token grant."""
        token = await self._storage_get(owner)
        if token is None:
            raise OAuthTokenError("no OAuth token is available")
        if not force and not token.is_expired(self._now(), self.config.refresh_skew_s):
            return token
        if not token.refresh_token:
            raise OAuthTokenError("OAuth token is expired and cannot be refreshed")
        request = OAuthTokenRequest(
            "refresh_token",
            self.config.token_endpoint,
            self.config.client_id,
            refresh_token=token.refresh_token,
            scope=self.config.scope,
            resource=self.config.resource,
        )
        refreshed = await self._exchange(request)
        if not refreshed.refresh_token:
            refreshed = OAuthToken(
                refreshed.access_token,
                token.refresh_token,
                refreshed.token_type,
                expires_at=refreshed.expires_at,
                scope=refreshed.scope or token.scope,
                clock=self._clock,
            )
        await self._storage_set(refreshed, owner)
        return refreshed

    async def refresh_token(
        self, owner: str = "default", *, force: bool = False
    ) -> OAuthToken:
        """Alias for :meth:`refresh` using the refresh-token grant."""
        return await self.refresh(owner, force=force)

    async def get_valid_token(
        self, owner: str = "default", *, force_refresh: bool = False
    ) -> OAuthToken:
        """Return a valid token, refreshing it when expired."""
        return await self.refresh(owner, force=force_refresh)

    async def get_token(
        self, owner: str = "default", *, force_refresh: bool = False
    ) -> OAuthToken:
        """Alias for :meth:`get_valid_token`."""
        return await self.get_valid_token(owner, force=force_refresh)

    async def get_access_token(self, owner: str = "default") -> Optional[str]:
        """Return a bearer token for an adapter, or ``None`` when unavailable."""
        try:
            token = await self.get_valid_token(owner)
        except OAuthTokenError:
            return None
        return token.access_token

    def token_provider(self, owner: str = "default") -> "OAuthTokenProvider":
        """Return a provider-shaped adapter hook for this manager."""
        return OAuthTokenProvider(self, owner=owner)

    def authorization_error(
        self, authorization_url: Optional[str] = None
    ) -> OAuthAuthenticationRequiredError:
        """Build the typed adapter-facing authentication-required error."""
        return OAuthAuthenticationRequiredError(authorization_url)

    async def complete(
        self,
        authorization: OAuthAuthorization,
        callback: Union[str, Mapping[str, Any]],
        *,
        owner: Optional[str] = None,
    ) -> OAuthToken:
        """Validate a callback for an authorization and exchange its code."""
        validated = self.handle_callback(
            callback,
            session_id=authorization.session_id,
            client_id=authorization.client_id,
        )
        return await self.exchange_authorization_code(validated, owner)

    def __repr__(self) -> str:
        """Return manager metadata without token or state values."""
        return f"OAuthManager(client_id={self.config.client_id!r}, pending_state={len(self.state_store)})"


OAuthPKCEProvider = OAuthManager
OAuthPKCEClient = OAuthManager
OAuthClient = OAuthManager
OAuthStateStore = InMemoryOAuthStateStore
OAuthTokenStorage = TokenStorage
AtomicFileTokenStorage = FileTokenStorage
MemoryTokenStorage = InMemoryTokenStorage


class OAuthTokenProvider:
    """Adapter hook exposing token lookup and authorization initiation."""

    def __init__(self, manager: OAuthManager, owner: str = "default") -> None:
        """Bind a provider to one manager and non-secret owner key."""
        self.manager = manager
        self.owner = str(owner or "default")

    async def get_access_token(self) -> Optional[str]:
        """Return a current bearer token without exposing refresh material."""
        return await self.manager.get_access_token(self.owner)

    async def get_token(self) -> Optional[str]:
        """Alias for :meth:`get_access_token`."""
        return await self.get_access_token()

    async def refresh(self) -> OAuthToken:
        """Force a refresh-token grant for the bound owner."""
        return await self.manager.refresh(self.owner, force=True)

    def begin_authorization(
        self,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        **kwargs: Any,
    ) -> OAuthAuthorization:
        """Initiate a fresh PKCE authorization for the adapter."""
        return self.manager.begin_authorization(
            session_id or self.owner, client_id, **kwargs
        )

    async def authorize(
        self,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        **kwargs: Any,
    ) -> OAuthAuthorization:
        """Async-compatible authorization initiation hook."""
        return self.begin_authorization(session_id, client_id, **kwargs)

    def __repr__(self) -> str:
        """Return a safe provider representation."""
        return f"OAuthTokenProvider(owner={_redact(self.owner)!r})"
