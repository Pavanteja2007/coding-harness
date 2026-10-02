"""Typed errors raised by the transport-neutral agent SDK."""

from __future__ import annotations

from typing import Any, Mapping

from shared.security import redact_text

__all__ = [
    "AgentError",
    "AgentSDKError",
    "AuthenticationError",
    "ClosedError",
    "ConflictError",
    "EventReplayError",
    "HTTPError",
    "InvalidRequestError",
    "MissingVersionError",
    "NotFoundError",
    "ProtocolError",
    "RemoteError",
    "RunNotFoundError",
    "SDKError",
    "SerializationError",
    "ToolCatalogError",
    "ToolNotFoundError",
    "ToolResolutionError",
    "TransportError",
    "UnsupportedVersionError",
    "VersionError",
    "VersionMismatchError",
    "WorkspaceActiveError",
    "WorkspaceError",
    "WorkspaceNotFoundError",
]


class AgentError(Exception):
    """Base class for errors exposed by the public SDK."""


class AgentSDKError(AgentError):
    """Base class for SDK errors that are safe to show to callers."""


SDKError = AgentSDKError


class InvalidRequestError(AgentSDKError, ValueError):
    """Raised when a request is missing or contains invalid public data."""


class ProtocolError(AgentSDKError):
    """Raised when a peer violates the SDK wire protocol."""


class SerializationError(ProtocolError):
    """Raised when a response cannot be decoded as a public contract."""


class UnsupportedVersionError(AgentSDKError):
    """Raised when no deterministic compatible protocol version exists."""

    def __init__(
        self,
        message: str = "unsupported agent protocol version",
        *,
        supported: Any = (),
        requested: Any = (),
    ) -> None:
        """Create a version failure with optional supported-version metadata."""
        self.supported = tuple(supported or ())
        self.requested = tuple(requested or ())
        suffix = ""
        if self.supported:
            suffix = f"; supported={list(self.supported)}"
        super().__init__(redact_text(str(message) + suffix))


class MissingVersionError(UnsupportedVersionError):
    """Raised when a required protocol or schema version is absent."""


VersionError = UnsupportedVersionError
VersionMismatchError = UnsupportedVersionError


class TransportError(AgentSDKError):
    """Raised when a local or remote transport cannot complete an operation."""


class RemoteError(TransportError):
    """Raised when a remote server returns a structured failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "remote_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Create a redacted remote failure with HTTP and protocol metadata."""
        self.status_code = int(status_code or 0)
        self.code = str(code or "remote_error")
        self.details = dict(details or {})
        super().__init__(redact_text(message))


class HTTPError(RemoteError):
    """Raised for an unsuccessful HTTP response."""


class AuthenticationError(HTTPError):
    """Raised when a server rejects bearer authentication."""


class NotFoundError(RemoteError):
    """Raised when a requested public resource does not exist."""


class RunNotFoundError(NotFoundError):
    """Raised when a run identifier is unknown."""


class ToolCatalogError(RemoteError):
    """Raised when an injected tool catalog cannot complete an operation."""


class ToolNotFoundError(NotFoundError):
    """Raised when an exact tool name is absent from a catalog."""


class ToolResolutionError(ToolCatalogError):
    """Raised when a deferred tool schema cannot be resolved."""


class WorkspaceNotFoundError(NotFoundError):
    """Raised when a managed workspace identifier is unknown."""


class WorkspaceError(AgentSDKError):
    """Raised when a managed workspace lifecycle operation fails."""


class ConflictError(RemoteError):
    """Raised when a resource is in an incompatible lifecycle state."""


class WorkspaceActiveError(ConflictError):
    """Raised when an active managed workspace cannot be deleted."""


class EventReplayError(AgentSDKError):
    """Raised when an event journal fails public replay validation."""


class ClosedError(AgentSDKError):
    """Raised when a closed SDK object is used again."""
