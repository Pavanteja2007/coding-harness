"""Typed, versioned models and policy helpers for the Agent Client Protocol.

The adapter speaks ACP protocol version 1 over newline-delimited JSON-RPC.  This
module deliberately has no dependency on an ACP SDK.  It contains the wire
models, status-to-stop-reason policy, prompt/update normalization, and the
small amount of verification policy needed to avoid treating an unverified
agent result as a successful verified run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Mapping, Sequence

from shared.security import redact_secrets, redact_text

PROTOCOL_VERSION = 1
ACP_PROTOCOL_VERSION = PROTOCOL_VERSION
ACP_VERSION = PROTOCOL_VERSION
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION})
SUPPORTED_VERSIONS = SUPPORTED_PROTOCOL_VERSIONS
JSONRPC_VERSION = "2.0"

ACP_ERROR_PARSE = -32700
ACP_ERROR_INVALID_REQUEST = -32600
ACP_ERROR_METHOD_NOT_FOUND = -32601
ACP_ERROR_INVALID_PARAMS = -32602
ACP_ERROR_INTERNAL = -32603
ACP_ERROR_AUTH_REQUIRED = -32001
ACP_ERROR_NOT_INITIALIZED = -32002
ACP_ERROR_TIMEOUT = -32003
ACP_ERROR_REQUEST_CANCELLED = -32800
ACP_ERROR_CANCELLED = ACP_ERROR_REQUEST_CANCELLED

STOP_REASON_END_TURN = "end_turn"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_MAX_TURN_REQUESTS = "max_turn_requests"
STOP_REASON_REFUSAL = "refusal"
STOP_REASON_CANCELLED = "cancelled"
VALID_STOP_REASONS = frozenset(
    {
        STOP_REASON_END_TURN,
        STOP_REASON_MAX_TOKENS,
        STOP_REASON_MAX_TURN_REQUESTS,
        STOP_REASON_REFUSAL,
        STOP_REASON_CANCELLED,
    }
)

CANONICAL_STATUSES = (
    "completed_verified",
    "completed_unverified",
    "needs_input",
    "blocked",
    "failed",
    "cancelled",
    "timeout",
)

_STATUS_ALIASES = {
    "completed_verified": "completed_verified",
    "verified": "completed_verified",
    "verified_complete": "completed_verified",
    "success": "completed_verified",
    "succeeded": "completed_verified",
    "completed_unverified": "completed_unverified",
    "unverified": "completed_unverified",
    "completed": "completed_verified",
    "complete": "completed_verified",
    "done": "completed_verified",
    "ok": "completed_verified",
    "needs_input": "needs_input",
    "input_required": "needs_input",
    "awaiting_input": "needs_input",
    "blocked": "blocked",
    "refused": "blocked",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "timeout": "timeout",
    "timed_out": "timeout",
    "timedout": "timeout",
}


class ACPError(Exception):
    """A redacted JSON-RPC or adapter error suitable for public handling."""

    def __init__(
        self,
        code: int | str = -32603,
        message: str = "",
        data: Any = None,
    ) -> None:
        if isinstance(code, str) and not message:
            message = code
            code = -32603
        try:
            self.code = int(code)
        except (TypeError, ValueError):
            self.code = -32603
        self.message = redact_text(message or "ACP operation failed")
        self.data = redact_secrets(data)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-RPC error object."""
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            value["data"] = self.data
        return value

    def __repr__(self) -> str:
        """Return a diagnostic representation without raw secret material."""
        return f"ACPError(code={self.code!r}, message={self.message!r})"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPError":
        """Parse a redacted JSON-RPC error mapping."""
        data = dict(value or {})
        return cls(
            data.get("code", -32603),
            data.get("message", "ACP operation failed"),
            data.get("data"),
        )


class ACPProtocolError(ACPError):
    """An invalid JSON-RPC envelope or unsupported ACP operation."""


class ACPUnsupportedVersionError(ACPProtocolError):
    """Raised when protocol negotiation cannot select a supported version."""


class ACPTransportError(ACPError):
    """Raised when the underlying transport cannot complete an operation."""


class ACPTimeoutError(ACPTransportError, TimeoutError):
    """Raised when an ACP request or transport read exceeds its deadline."""


class ACPTransportClosed(ACPTransportError):
    """Raised when an operation is attempted after transport shutdown."""


class ACPAuthenticationError(ACPError):
    """Raised when an operation needs authentication that was not completed."""


def _public(value: Any) -> Any:
    """Return a recursively redacted JSON-compatible public value."""
    return redact_secrets(value)


def _validate_id(value: Any) -> int | str | None:
    """Validate a JSON-RPC identifier while preserving integer zero."""
    if value is None or isinstance(value, (int, str)):
        if isinstance(value, bool):
            raise ACPProtocolError(-32600, "JSON-RPC ids cannot be booleans")
        return value
    raise ACPProtocolError(-32600, "JSON-RPC id must be an integer, string, or null")


def validate_jsonrpc_id(value: Any, *, allow_none: bool = True) -> int | str | None:
    """Validate an ID at a protocol boundary with explicit null policy."""
    if value is None and not allow_none:
        raise ACPProtocolError(-32600, "JSON-RPC request id is required")
    return _validate_id(value)


def validate_params(value: Any, *, allow_missing: bool = False) -> dict[str, Any]:
    """Validate an ACP JSON-RPC params object and reject null/array values."""
    if value is None and allow_missing:
        return {}
    if not isinstance(value, Mapping):
        raise ACPProtocolError(-32602, "JSON-RPC params must be an object")
    return dict(value)


def _mapping(value: Any) -> dict[str, Any]:
    """Copy a mapping or return an empty mapping for absent values."""
    return dict(value or {}) if isinstance(value, Mapping) else {}


def validate_session_id(value: Any, *, allow_empty: bool = False) -> str:
    """Validate an opaque ACP session identifier without path assumptions."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ACPProtocolError(-32602, "ACP session id must be a string")
    if not value and not allow_empty:
        raise ACPProtocolError(-32602, "ACP session id is required")
    if len(value) > 512 or value.strip() != value:
        raise ACPProtocolError(-32602, "ACP session id is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ACPProtocolError(-32602, "ACP session id contains control characters")
    return value


def _validate_prompt_block(value: Any) -> dict[str, Any]:
    """Validate one stable ACP v1 content block."""
    if not isinstance(value, Mapping):
        raise ACPProtocolError(-32602, "ACP prompt content must be an object")
    block = dict(value)
    kind = block.get("type")
    if not isinstance(kind, str) or not kind:
        raise ACPProtocolError(-32602, "ACP prompt content type is required")
    if kind == "text":
        if not isinstance(block.get("text"), str):
            raise ACPProtocolError(-32602, "ACP text content requires text")
    elif kind in {"image", "audio"}:
        if not isinstance(block.get("data"), str) or not isinstance(
            block.get("mimeType"), str
        ):
            raise ACPProtocolError(
                -32602, f"ACP {kind} content requires data and mimeType"
            )
    elif kind == "resource_link":
        if not isinstance(block.get("uri"), str):
            raise ACPProtocolError(-32602, "ACP resource_link requires uri")
    elif kind == "resource":
        resource = block.get("resource")
        if not isinstance(resource, Mapping) or not isinstance(
            resource.get("uri"), str
        ):
            raise ACPProtocolError(-32602, "ACP resource content requires resource.uri")
    elif not kind.startswith("_"):
        raise ACPProtocolError(-32602, f"unsupported ACP prompt content type: {kind}")
    return block


def _validate_prompt(value: Any) -> list[dict[str, Any]]:
    """Validate a wire prompt sequence using stable ACP v1 content rules."""
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ACPProtocolError(-32602, "ACP prompt must be a content block array")
    if not value:
        raise ACPProtocolError(-32602, "ACP prompt must not be empty")
    return [_validate_prompt_block(item) for item in value]


_KNOWN_REQUIRED_CAPABILITIES = frozenset(
    {
        "auth",
        "auth.logout",
        "auth.terminal",
        "fs",
        "fs.readTextFile",
        "fs.writeTextFile",
        "terminal",
        "elicitation",
        "session.configOptions.boolean",
        "promptCapabilities.image",
        "promptCapabilities.audio",
        "promptCapabilities.embeddedContext",
        "mcpCapabilities",
        "mcpCapabilities.http",
        "mcpCapabilities.sse",
        "loadSession",
        "sessionCapabilities",
        "sessionCapabilities.close",
        "sessionCapabilities.resume",
        "sessionCapabilities.delete",
        "sessionCapabilities.list",
    }
)


def _validate_required_capability_names(value: Any) -> None:
    """Reject unknown required capability names in an initialize result."""
    if value is None:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ACPProtocolError(-32602, "requiredCapabilities must be an array")
    for item in value:
        if str(item) not in _KNOWN_REQUIRED_CAPABILITIES:
            raise ACPProtocolError(
                -32602,
                "unknown required ACP capability",
                {"capability": str(item)},
            )


def _auth_method_type(value: Mapping[str, Any]) -> str:
    """Return a normalized v1 authentication method type."""
    raw = value.get("type", "agent")
    return str(raw or "agent").strip().lower()


def auth_method_is_terminal(value: Mapping[str, Any] | str) -> bool:
    """Return whether an advertised v1 method uses terminal authentication."""
    data = {"id": value, "type": "terminal"} if isinstance(value, str) else dict(value)
    return (
        _auth_method_type(data) == "terminal"
        or str(data.get("id", "")).lower() == "terminal"
    )


def auth_method_requires_protocol_auth(value: Mapping[str, Any] | str) -> bool:
    """Return whether v1 requires an ``authenticate`` request for a method."""
    data = {"id": value, "type": "agent"} if isinstance(value, str) else dict(value)
    method_id = str(data.get("id", "")).strip().lower()
    method_type = _auth_method_type(data)
    if method_id in {
        "never",
        "none",
        "no-auth",
        "no_auth",
        "terminal",
        "host",
        "bearer",
    }:
        return False
    return method_type not in {"terminal", "host", "bearer", "never", "none", "no-auth"}


def negotiate_protocol_version(
    requested: Any,
    supported: Iterable[int] = SUPPORTED_PROTOCOL_VERSIONS,
) -> int:
    """Select the highest deterministic intersection of protocol versions."""
    if isinstance(requested, bool):
        raise ACPUnsupportedVersionError(
            -32000, "ACP protocol version must be an integer"
        )
    if isinstance(requested, int):
        requested_versions = {requested}
    elif isinstance(requested, Sequence) and not isinstance(
        requested, (str, bytes, bytearray)
    ):
        requested_versions = {
            int(item)
            for item in requested
            if isinstance(item, int) and not isinstance(item, bool)
        }
    else:
        raise ACPUnsupportedVersionError(
            -32000, "ACP protocol version must be an integer"
        )
    supported_versions = {
        int(item)
        for item in supported
        if isinstance(item, int) and not isinstance(item, bool)
    }
    intersection = requested_versions.intersection(supported_versions)
    if not intersection:
        raise ACPUnsupportedVersionError(
            -32000,
            "no supported ACP protocol version is shared by the client and agent",
        )
    return max(intersection)


def _record_mapping(value: Any) -> dict[str, Any] | None:
    """Return a public mapping/dataclass view without private attribute access."""
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return asdict(value)
        except (TypeError, ValueError):
            return None
    return None


_MISSING = object()


@dataclass(init=False)
class ACPRequest:
    """One JSON-RPC request or notification with an optional protocol version."""

    id: int | str | None
    method: str
    params: dict[str, Any]
    jsonrpc: str
    protocol_version: int | None
    id_present: bool
    params_present: bool

    def __init__(
        self,
        method: Any = "",
        params: Any = _MISSING,
        id: Any = None,
        *,
        request_id: Any = None,
        jsonrpc: str = JSONRPC_VERSION,
        protocol_version: int | None = None,
        version: int | None = None,
        id_present: bool | None = None,
        params_present: bool | None = None,
    ) -> None:
        """Build a request; ``id=None`` creates a JSON-RPC notification."""
        if version is not None:
            protocol_version = version
        if isinstance(method, int) and isinstance(params, str) and id is None:
            method, params, id = params, {}, method
        if isinstance(method, Mapping) and params is _MISSING:
            raw = dict(method)
            method = raw.get("method", "")
            params = raw.get("params", {})
            id = raw.get("id") if id is None else id
            jsonrpc = raw.get("jsonrpc", jsonrpc)
            protocol_version = raw.get(
                "protocolVersion", raw.get("protocol_version", protocol_version)
            )
            if id_present is None:
                id_present = "id" in raw
            if params_present is None:
                params_present = "params" in raw
        params_was_missing = params is _MISSING
        if params_was_missing:
            params = {}
        if params_present is None:
            params_present = not params_was_missing
        if id_present is None:
            id_present = id is not None or request_id is not None
        if not isinstance(method, str) or not method:
            raise ACPProtocolError(-32600, "JSON-RPC method is required")
        self.method = method
        self.params = validate_params(params, allow_missing=False)
        selected_id = request_id if request_id is not None else id
        if id_present and selected_id is None:
            raise ACPProtocolError(-32600, "JSON-RPC request id cannot be null")
        self.id = _validate_id(selected_id)
        if not isinstance(jsonrpc, str) or jsonrpc != JSONRPC_VERSION:
            raise ACPProtocolError(-32600, "unsupported JSON-RPC version")
        self.jsonrpc = jsonrpc
        if protocol_version is not None and (
            isinstance(protocol_version, bool) or not isinstance(protocol_version, int)
        ):
            raise ACPProtocolError(-32600, "ACP protocol version must be an integer")
        self.protocol_version = protocol_version
        self.id_present = bool(id_present)
        self.params_present = bool(params_present)

    @property
    def version(self) -> int | None:
        """Return the optional protocol version alias."""
        return self.protocol_version

    @property
    def schema_version(self) -> int | None:
        """Return the protocol version under a schema-style alias."""
        return self.protocol_version

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-RPC request object without diagnostic redaction."""
        value: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "method": self.method,
        }
        if self.id is not None:
            value["id"] = self.id
        value["params"] = dict(self.params)
        if self.protocol_version is not None:
            value["protocolVersion"] = self.protocol_version
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPRequest":
        """Parse and validate one JSON-RPC request mapping."""
        if not isinstance(value, Mapping):
            raise ACPProtocolError(-32600, "JSON-RPC request must be an object")
        if "params" in value and value["params"] is None:
            raise ACPProtocolError(-32602, "JSON-RPC params cannot be null")
        if "id" in value and value["id"] is None:
            raise ACPProtocolError(-32600, "JSON-RPC request id cannot be null")
        return cls(
            method=value.get("method", ""),
            params=value.get("params", {}),
            id=value.get("id"),
            jsonrpc=value.get("jsonrpc", JSONRPC_VERSION),
            protocol_version=value.get(
                "protocolVersion", value.get("protocol_version")
            ),
            id_present="id" in value,
            params_present="params" in value,
        )

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access for small transport integrations."""
        return self.to_dict()[key]


@dataclass(init=False)
class ACPResponse:
    """One JSON-RPC success or error response."""

    id: int | str | None
    result: Any
    error: ACPError | None
    jsonrpc: str

    def __init__(
        self,
        result: Any = None,
        error: ACPError | Mapping[str, Any] | None = None,
        id: Any = None,
        *,
        response_id: Any = None,
        jsonrpc: str = JSONRPC_VERSION,
    ) -> None:
        """Build a response with exactly one result or error member."""
        if (
            isinstance(result, Mapping)
            and error is None
            and id is None
            and any(key in result for key in ("id", "jsonrpc"))
        ):
            raw = dict(result)
            result = raw.get("result")
            error = raw.get("error")
            id = raw.get("id")
            jsonrpc = raw.get("jsonrpc", jsonrpc)
        self.id = _validate_id(response_id if response_id is not None else id)
        self.result = result
        if isinstance(error, ACPError):
            self.error = error
        elif isinstance(error, Mapping):
            self.error = ACPError(
                error.get("code", -32603),
                error.get("message", "ACP operation failed"),
                error.get("data"),
            )
        else:
            self.error = None
        self.jsonrpc = str(jsonrpc or JSONRPC_VERSION)
        if self.jsonrpc != JSONRPC_VERSION:
            raise ACPProtocolError(-32600, "unsupported JSON-RPC version")
        if self.error is None and result is None and id is None:
            self.result = {}

    @property
    def is_error(self) -> bool:
        """Return whether this response carries a JSON-RPC error."""
        return self.error is not None

    @property
    def ok(self) -> bool:
        """Return whether this response carries a successful result."""
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-RPC response object without diagnostic redaction."""
        value: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            value["error"] = self.error.to_dict()
        else:
            value["result"] = self.result
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPResponse":
        """Parse and validate one JSON-RPC response mapping."""
        if not isinstance(value, Mapping):
            raise ACPProtocolError(-32602, "JSON-RPC response must be an object")
        if "id" not in value:
            raise ACPProtocolError(-32602, "JSON-RPC response id is required")
        if "error" in value and (
            value["error"] is None or not isinstance(value["error"], Mapping)
        ):
            raise ACPProtocolError(-32602, "JSON-RPC error must be an object")
        if "result" in value and "error" in value:
            raise ACPProtocolError(
                -32602, "JSON-RPC response cannot contain both result and error"
            )
        if "result" not in value and "error" not in value:
            raise ACPProtocolError(
                -32602, "JSON-RPC response must contain result or error"
            )
        return cls(
            result=value.get("result"),
            error=value.get("error"),
            id=value.get("id"),
            jsonrpc=value.get("jsonrpc", JSONRPC_VERSION),
        )

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access for small transport integrations."""
        return self.to_dict()[key]


@dataclass(init=False)
class ACPCapabilities:
    """The negotiated ACP version and advertised agent capabilities."""

    protocol_version: int
    agent_capabilities: dict[str, Any]
    auth_methods: list[dict[str, Any]]
    agent_info: dict[str, Any]
    auth_required: bool | None
    raw: dict[str, Any]

    def __init__(
        self,
        protocol_version: int = PROTOCOL_VERSION,
        agent_capabilities: Mapping[str, Any] | None = None,
        auth_methods: Sequence[Mapping[str, Any] | str] | str | None = None,
        agent_info: Mapping[str, Any] | None = None,
        raw: Mapping[str, Any] | None = None,
        auth_required: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Build capabilities from an initialize result or explicit fields."""
        if "version" in kwargs and protocol_version == PROTOCOL_VERSION:
            protocol_version = kwargs["version"]
        if isinstance(protocol_version, Mapping):
            source = dict(protocol_version)
            agent_capabilities = source.get(
                "agentCapabilities", source.get("agent_capabilities", {})
            )
            auth_methods = source.get("authMethods", source.get("auth_methods", []))
            agent_info = source.get("agentInfo", source.get("agent_info", {}))
            auth_required = source.get(
                "authRequired", source.get("auth_required", auth_required)
            )
            raw = source
            protocol_version = source.get(
                "protocolVersion", source.get("protocol_version", 1)
            )
        self.protocol_version = normalize_protocol_version(protocol_version)
        if agent_capabilities is not None and not isinstance(
            agent_capabilities, Mapping
        ):
            raise ACPProtocolError(-32602, "ACP agent capabilities must be an object")
        self.agent_capabilities = _mapping(agent_capabilities)
        if isinstance(auth_methods, str):
            auth_methods = [auth_methods]
        if auth_methods is not None and (
            not isinstance(auth_methods, Sequence)
            or isinstance(auth_methods, (bytes, bytearray))
        ):
            raise ACPProtocolError(-32602, "ACP auth methods must be an array")
        self.auth_methods = [_auth_method(item) for item in (auth_methods or [])]
        if agent_info is not None and not isinstance(agent_info, Mapping):
            raise ACPProtocolError(-32602, "ACP agent info must be an object")
        self.agent_info = _mapping(agent_info)
        if auth_required is not None and not isinstance(auth_required, bool):
            raise ACPProtocolError(-32602, "ACP authRequired must be a boolean")
        if auth_required is None:
            self.auth_required = any(
                auth_method_requires_protocol_auth(item) for item in self.auth_methods
            )
        else:
            self.auth_required = auth_required
        self.raw = _mapping(raw)
        if not self.raw:
            self.raw = self.to_dict()

    @property
    def protocolVersion(self) -> int:
        """Return the ACP camel-case protocol version alias."""
        return self.protocol_version

    @property
    def agentCapabilities(self) -> dict[str, Any]:
        """Return the ACP camel-case agent capability mapping."""
        return self.agent_capabilities

    @property
    def authMethods(self) -> list[dict[str, Any]]:
        """Return advertised authentication methods."""
        return self.auth_methods

    @property
    def authRequired(self) -> bool | None:
        """Return whether the agent explicitly requires authentication."""
        return self.auth_required

    @property
    def load_session(self) -> bool:
        """Return whether the agent advertises session loading."""
        return bool(self.agent_capabilities.get("loadSession", False))

    @property
    def supports_authentication(self) -> bool:
        """Return whether at least one authentication method was declared."""
        return bool(self.auth_methods)

    @property
    def requires_authentication(self) -> bool:
        """Return whether a v1 protocol-driven authenticate call is required."""
        return bool(self.auth_required)

    @property
    def protocol_auth_methods(self) -> list[dict[str, Any]]:
        """Return advertised methods that support the v1 authenticate flow."""
        return [
            item
            for item in self.auth_methods
            if auth_method_requires_protocol_auth(item)
        ]

    def auth_method(self, method_id: str) -> dict[str, Any] | None:
        """Return one advertised auth method by its v1 identifier."""
        wanted = str(method_id or "")
        return next(
            (item for item in self.auth_methods if str(item.get("id", "")) == wanted),
            None,
        )

    def supports(self, path: str, default: bool = False) -> bool:
        """Return a nested advertised capability value."""
        value: Any = self.agent_capabilities
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return bool(value)

    def to_dict(self) -> dict[str, Any]:
        """Return the initialize result in ACP wire form."""
        value: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": dict(self.agent_capabilities),
            "authMethods": [dict(item) for item in self.auth_methods],
            "agentInfo": dict(self.agent_info),
        }
        if self.auth_required is not None:
            value["authRequired"] = self.auth_required
        return value

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access to the initialize result."""
        return self.to_dict()[key]

    def __contains__(self, key: str) -> bool:
        """Return whether an initialize result key is present."""
        return key in self.to_dict()

    @classmethod
    def from_initialize_result(cls, value: Mapping[str, Any]) -> "ACPCapabilities":
        """Parse an initialize result and fail closed on an invalid version."""
        data = dict(value or {})
        if "authMethods" in data and (
            not isinstance(data["authMethods"], Sequence)
            or isinstance(data["authMethods"], (str, bytes, bytearray))
        ):
            raise ACPProtocolError(-32602, "ACP authMethods must be an array")
        if "auth_methods" in data and (
            not isinstance(data["auth_methods"], Sequence)
            or isinstance(data["auth_methods"], (str, bytes, bytearray))
        ):
            raise ACPProtocolError(-32602, "ACP authMethods must be an array")
        _validate_required_capability_names(
            data.get("requiredCapabilities", data.get("required_capabilities"))
        )
        capability_data = data.get(
            "agentCapabilities", data.get("agent_capabilities", {})
        )
        if isinstance(capability_data, Mapping):
            _validate_required_capability_names(
                capability_data.get(
                    "requiredCapabilities", capability_data.get("required_capabilities")
                )
            )
        version = data.get("protocolVersion", data.get("protocol_version"))
        try:
            version = normalize_protocol_version(version)
        except ACPUnsupportedVersionError as exc:
            raise ACPUnsupportedVersionError(-32000, str(exc)) from exc
        return cls(
            protocol_version=version,
            agent_capabilities=data.get(
                "agentCapabilities", data.get("agent_capabilities", {})
            ),
            auth_methods=data.get("authMethods", data.get("auth_methods", [])),
            agent_info=data.get("agentInfo", data.get("agent_info", {})),
            auth_required=data.get("authRequired", data.get("auth_required")),
            raw=data,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPCapabilities":
        """Parse an initialize result from a mapping."""
        return cls.from_initialize_result(value)


def _auth_method(value: Mapping[str, Any] | str) -> dict[str, Any]:
    """Normalize one stable ACP v1 authentication method descriptor."""
    if isinstance(value, str):
        if not value.strip():
            raise ACPProtocolError(-32602, "ACP authentication method id is required")
        shorthand_type = "terminal" if value.strip().lower() == "terminal" else "agent"
        return {"id": value, "name": value, "type": shorthand_type}
    if not isinstance(value, Mapping):
        raise ACPProtocolError(-32602, "ACP authentication method must be an object")
    data = dict(value)
    method_id = data.get("id", data.get("methodId", data.get("name", "")))
    if not isinstance(method_id, str) or not method_id.strip():
        raise ACPProtocolError(-32602, "ACP authentication method id is required")
    method_type = data.get("type", "agent")
    if not isinstance(method_type, str) or not method_type:
        raise ACPProtocolError(-32602, "ACP authentication method type is invalid")
    normalized_type = method_type.strip().lower()
    if normalized_type not in {
        "agent",
        "terminal",
        "host",
        "bearer",
        "never",
    } and not normalized_type.startswith("_"):
        raise ACPProtocolError(-32602, "unsupported ACP authentication method type")
    data["id"] = method_id
    data.setdefault("name", method_id)
    data["type"] = method_type
    return data


@dataclass(init=False)
class ACPSession:
    """A typed ACP session returned by ``session/new``."""

    session_id: str
    cwd: str = ""
    modes: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    config_options: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    def __init__(
        self,
        session_id: str = "",
        cwd: str = "",
        modes: Mapping[str, Any] | None = None,
        models: Mapping[str, Any] | None = None,
        config_options: Sequence[Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        protocol_version: int = PROTOCOL_VERSION,
        **kwargs: Any,
    ) -> None:
        """Build a session model from explicit fields or a wire result."""
        if isinstance(session_id, Mapping):
            raw = dict(session_id)
            session_id = raw.get("sessionId", raw.get("session_id", ""))
            cwd = raw.get("cwd", cwd)
            modes = raw.get("modes", modes)
            models = raw.get("models", models)
            config_options = raw.get(
                "configOptions", raw.get("config_options", config_options)
            )
            metadata = raw.get("metadata", metadata)
            protocol_version = raw.get("protocolVersion", protocol_version)
        self.session_id = validate_session_id(session_id)
        self.cwd = str(cwd or "")
        self.modes = _mapping(modes)
        self.models = _mapping(models)
        self.config_options = [_mapping(item) for item in (config_options or [])]
        self.metadata = _mapping(metadata)
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
            raise ACPProtocolError(-32602, "ACP protocol version must be an integer")
        self.protocol_version = protocol_version

    @property
    def id(self) -> str:
        """Return the short session identifier alias."""
        return self.session_id

    @property
    def version(self) -> int:
        """Return the negotiated protocol version."""
        return self.protocol_version

    @property
    def sessionId(self) -> str:
        """Return the ACP camel-case session identifier alias."""
        return self.session_id

    @property
    def mode_id(self) -> str:
        """Return the current mode id, if the agent supplied one."""
        return str(
            self.modes.get("currentModeId", self.modes.get("current_mode_id", ""))
        )

    @property
    def available_modes(self) -> list[dict[str, Any]]:
        """Return advertised available modes."""
        return [_mapping(item) for item in self.modes.get("availableModes", [])]

    def to_dict(self) -> dict[str, Any]:
        """Return the ACP session result in wire form."""
        value: dict[str, Any] = {"sessionId": self.session_id}
        if self.cwd:
            value["cwd"] = self.cwd
        if self.modes:
            value["modes"] = dict(self.modes)
        if self.models:
            value["models"] = dict(self.models)
        if self.config_options:
            value["configOptions"] = [dict(item) for item in self.config_options]
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access for session results."""
        return self.to_dict()[key]

    @classmethod
    def from_new_session_result(
        cls,
        value: Mapping[str, Any],
        *,
        cwd: str = "",
        protocol_version: int = PROTOCOL_VERSION,
    ) -> "ACPSession":
        """Parse a ``session/new`` result while requiring a session id."""
        data = dict(value or {})
        session_id = data.get("sessionId", data.get("session_id"))
        validate_session_id(session_id)
        return cls(
            session_id=session_id,
            cwd=cwd,
            modes=data.get("modes", {}),
            models=data.get("models", {}),
            config_options=data.get("configOptions", data.get("config_options", [])),
            metadata=data.get("metadata", {}),
            protocol_version=protocol_version,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPSession":
        """Parse a session result from a mapping."""
        data = dict(value or {})
        session_id = data.get("sessionId", data.get("session_id", ""))
        validate_session_id(session_id)
        version = data.get("protocolVersion", PROTOCOL_VERSION)
        return cls(
            session_id=session_id,
            cwd=str(data.get("cwd", "")),
            modes=data.get("modes", {}),
            models=data.get("models", {}),
            config_options=data.get("configOptions", data.get("config_options", [])),
            metadata=data.get("metadata", {}),
            protocol_version=version,
        )


@dataclass(init=False)
class ACPPromptResult:
    """A terminal prompt response plus the ordered updates observed for it."""

    stop_reason: str
    status: str
    verified: bool
    updates: list[dict[str, Any]]
    result: Any
    error: str
    text_value: str
    session_id: str

    def __init__(
        self,
        stop_reason: str = STOP_REASON_END_TURN,
        status: str = "completed_unverified",
        verified: bool = False,
        updates: Sequence[Mapping[str, Any]] | None = None,
        result: Any = None,
        error: str = "",
        text: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Build a prompt result, downgrading unsupported verification claims."""
        if isinstance(stop_reason, Mapping):
            raw = dict(stop_reason)
            stop_reason = raw.get(
                "stopReason", raw.get("stop_reason", STOP_REASON_END_TURN)
            )
            status = raw.get("status", status)
            verified = bool(raw.get("verified", raw.get("completed_verified", False)))
            updates = raw.get("updates", updates)
            result = raw.get("result", result)
            error = raw.get("error", error)
            text = raw.get("text", text)
            session_id = raw.get("sessionId", raw.get("session_id", session_id))
        self.stop_reason = str(stop_reason or STOP_REASON_END_TURN)
        self.status = normalize_status(status, verified=verified, result=result)
        evidence = clean_verification_evidence(result)
        self.verified = bool(
            verified and self.status == "completed_verified" and evidence
        )
        self.updates = [_mapping(item) for item in (updates or [])]
        self.result = result
        self.error = redact_text(error)
        self.text_value = redact_text(
            text or _text_from_updates(self.updates) or _answer_from_result(result)
        )
        self.session_id = str(session_id or "")

    @property
    def stopReason(self) -> str:
        """Return the ACP camel-case stop reason alias."""
        return self.stop_reason

    @property
    def text(self) -> str:
        """Return concatenated visible text from updates and the result."""
        return self.text_value

    @property
    def answer(self) -> str:
        """Return the result answer alias."""
        return self.text_value

    @property
    def completed_verified(self) -> bool:
        """Return whether clean verifier evidence supports completion."""
        return self.verified

    @property
    def successful(self) -> bool:
        """Return whether the prompt ended in either completion state."""
        return self.status in {"completed_verified", "completed_unverified"}

    @property
    def cancelled(self) -> bool:
        """Return whether the prompt ended by client cancellation."""
        return self.status == "cancelled"

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted local model representation."""
        return _public(
            {
                "stopReason": self.stop_reason,
                "status": self.status,
                "verified": self.verified,
                "updates": self.updates,
                "result": self.result,
                "error": self.error,
                "text": self.text_value,
                "sessionId": self.session_id,
            }
        )

    def to_wire_result(self) -> dict[str, Any]:
        """Return the terminal ``session/prompt`` result mapping."""
        value: dict[str, Any] = {
            "stopReason": self.stop_reason,
            "status": self.status,
            "verified": self.verified,
        }
        if self.result is not None:
            value["result"] = self.result
        if self.error:
            value["error"] = self.error
        return value

    def __getitem__(self, key: str) -> Any:
        """Provide mapping-style access to the local prompt result."""
        return self.to_dict()[key]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ACPPromptResult":
        """Parse a terminal prompt mapping into the typed local model."""
        data = dict(value or {})
        return cls(
            stop_reason=data.get(
                "stopReason", data.get("stop_reason", STOP_REASON_END_TURN)
            ),
            status=data.get("status", "completed_unverified"),
            verified=bool(data.get("verified", data.get("completed_verified", False))),
            updates=data.get("updates", []),
            result=data.get("result"),
            error=data.get("error", ""),
            text=data.get("text", ""),
            session_id=data.get("sessionId", data.get("session_id", "")),
        )


def _get(value: Any, name: str, default: Any = None) -> Any:
    """Read a public mapping key or object attribute without private access."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _status_text(value: Any) -> str:
    """Return a lower-case status token from a public result value."""
    raw = _get(
        value,
        "status",
        _get(value, "state", _get(value, "completion_status", "")),
    )
    if not raw:
        payload = _get(value, "payload", None)
        if payload is not None and payload is not value:
            payload_text = _status_text(payload)
            if payload_text:
                return payload_text
        raw = _get(value, "event_type", _get(value, "event", ""))
    if hasattr(raw, "value"):
        raw = raw.value
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text


def clean_verification_evidence(value: Any) -> bool:
    """Return whether a result contains explicit clean verifier evidence.

    A status label or a model's ``done`` claim is not evidence.  The function
    therefore requires a verification record proving both the target and
    regression checks, with no flaky or error marker.
    """
    records: list[Any] = []
    for name in ("verification_evidence", "verification", "evidence"):
        candidate = _get(value, name, None)
        record = _record_mapping(candidate)
        if record is not None:
            records.append(record)
        elif isinstance(candidate, Sequence) and not isinstance(
            candidate, (str, bytes)
        ):
            records.extend(
                item
                for value in candidate
                if (item := _record_mapping(value)) is not None
            )
    if not records:
        payload = _get(value, "payload", None)
        if payload is not None and payload is not value:
            return clean_verification_evidence(payload)
        return False
    for record in records:
        kind = str(_get(record, "kind", "verification")).lower()
        target = _get(
            record, "target_test_passed", _get(record, "target_passed", False)
        )
        regression = _get(record, "regression_passed", False)
        flaky = _get(record, "flaky", False)
        error = _get(record, "error", "")
        passed = _get(record, "passed", None)
        if kind == "verification" or target is not None:
            if passed is None:
                passed = bool(target) and bool(regression)
            if (
                bool(passed)
                and bool(target)
                and bool(regression)
                and not bool(flaky)
                and not error
            ):
                return True
    return False


def normalize_status(
    value: Any,
    *,
    verified: bool = False,
    result: Any = None,
) -> str:
    """Map a public agent status to one of the seven canonical Neo statuses."""
    token = _status_text(value)
    if not token and isinstance(value, str):
        token = value.strip().lower().replace("-", "_").replace(" ", "_")
    status = _STATUS_ALIASES.get(token, "")
    if not status:
        if _get(value, "ok", False) is True:
            status = "completed_unverified"
        else:
            status = "failed"
    if status == "completed_verified" and (
        not verified or not clean_verification_evidence(result)
    ):
        status = "completed_unverified"
    if status in {"completed_verified", "completed_unverified"} and not verified:
        return "completed_unverified"
    return status


def stop_reason_for_status(status: Any) -> str:
    """Map a canonical Neo status to the stable ACP v1 stop-reason vocabulary."""
    normalized = normalize_status(status)
    if normalized in {"completed_verified", "completed_unverified", "needs_input"}:
        return STOP_REASON_END_TURN
    if normalized == "cancelled":
        return STOP_REASON_CANCELLED
    if normalized == "timeout":
        return STOP_REASON_MAX_TURN_REQUESTS
    return STOP_REASON_REFUSAL


status_to_stop_reason = stop_reason_for_status
map_status_to_stop_reason = stop_reason_for_status


def normalize_prompt(prompt: Any, *, allow_text: bool = True) -> list[dict[str, Any]]:
    """Normalize a prompt string or a validated ACP content-block sequence."""
    if isinstance(prompt, str):
        if not allow_text:
            raise ACPProtocolError(
                -32602, "ACP wire prompt must be a content block array"
            )
        return [{"type": "text", "text": prompt}]
    if isinstance(prompt, Mapping):
        return [_validate_prompt_block(prompt)]
    if isinstance(prompt, Sequence) and not isinstance(prompt, (bytes, bytearray)):
        return _validate_prompt(prompt)
    raise ACPProtocolError(-32602, "prompt must be text or ACP content blocks")


def _text_from_value(value: Any) -> str:
    """Extract visible text from a public content block or result shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get("type") == "text" and "text" in value:
            return str(value.get("text") or "")
        for key in ("text", "delta", "chunk", "answer", "output", "content", "data"):
            if key in value:
                extracted = _text_from_value(value[key])
                if extracted:
                    return extracted
        if isinstance(value.get("payload"), Mapping):
            return _text_from_value(value["payload"])
        return ""
    for key in (
        "text",
        "delta",
        "chunk",
        "answer",
        "output",
        "content",
        "data",
        "payload",
    ):
        if hasattr(value, key):
            extracted = _text_from_value(getattr(value, key))
            if extracted:
                return extracted
    return ""


def _answer_from_result(value: Any) -> str:
    """Extract an answer from common public result field names."""
    for name in ("answer", "output", "text", "response", "content"):
        extracted = _text_from_value(_get(value, name, ""))
        if extracted:
            return extracted
    payload = _get(value, "payload", None)
    if payload is not None and payload is not value:
        return _answer_from_result(payload)
    return ""


def _text_from_updates(updates: Iterable[Mapping[str, Any]]) -> str:
    """Concatenate text content from ordered ACP update mappings."""
    pieces: list[str] = []
    for update in updates:
        if isinstance(update, Mapping) and isinstance(update.get("update"), Mapping):
            update = update["update"]
        session_update = str(_get(update, "sessionUpdate", ""))
        content = _get(update, "content", None)
        if session_update in {
            "",
            "agent_message_chunk",
            "agent_message",
            "user_message_chunk",
        }:
            text = _text_from_value(content)
            if text:
                pieces.append(text)
        else:
            text = _text_from_value(update)
            if text and session_update in {"message", "text", "output"}:
                pieces.append(text)
    return "".join(pieces)


def normalize_update(chunk: Any) -> dict[str, Any] | None:
    """Map a public agent stream chunk to an ACP ``session/update`` object."""
    if chunk is None:
        return None
    if isinstance(chunk, str):
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": chunk},
        }
    if isinstance(chunk, Mapping):
        if chunk.get("sessionUpdate") is not None:
            return dict(chunk)
        kind = str(chunk.get("type", chunk.get("kind", chunk.get("event", "")))).lower()
        if kind in {"result", "run_result", "terminal", "done", "completion"}:
            return None
        content = chunk.get("content")
        if isinstance(content, Mapping) and content.get("type") == "text":
            return {
                "sessionUpdate": "agent_message_chunk",
                "content": dict(content),
            }
        text = _text_from_value(chunk)
        if text:
            return {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }
        return dict(chunk) or None
    if _status_text(chunk) and _status_text(chunk) in {
        "completed_verified",
        "completed_unverified",
        "needs_input",
        "blocked",
        "failed",
        "cancelled",
        "timeout",
    }:
        return None
    text = _text_from_value(chunk)
    if text:
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    return None


def text_from_updates(updates: Iterable[Mapping[str, Any]]) -> str:
    """Return visible text from an ordered public update iterable."""
    return _text_from_updates(updates)


def result_answer(value: Any) -> str:
    """Return visible answer text from a public agent result."""
    return _answer_from_result(value)


def is_terminal_result(value: Any) -> bool:
    """Return whether a stream item looks like a terminal result object."""
    if isinstance(value, str):
        return False
    status = _status_text(value)
    kind = str(
        _get(
            value,
            "type",
            _get(value, "kind", _get(value, "event", _get(value, "event_type", ""))),
        )
    ).lower()
    if status in set(CANONICAL_STATUSES) or status in {"result", "completed", "done"}:
        return True
    if kind in {"result", "run_result", "terminal", "completion"}:
        return True
    if _get(value, "result", None) is not None and not isinstance(value, (str, bytes)):
        return (
            _get(value, "answer", None) is not None
            or _get(value, "status", None) is not None
        )
    return False


def normalize_protocol_version(value: Any) -> int:
    """Validate a protocol version and fail closed for unsupported values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ACPUnsupportedVersionError(
            -32000, "ACP protocol version must be an integer"
        )
    version = value
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ACPUnsupportedVersionError(
            -32000,
            f"unsupported ACP protocol version {version}; supported version is {PROTOCOL_VERSION}",
        )
    return version


def json_compatible(value: Any) -> str:
    """Serialize a public value as compact JSON after recursive redaction."""
    return json.dumps(_public(value), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ACP_ERROR_AUTH_REQUIRED",
    "ACP_ERROR_CANCELLED",
    "ACP_ERROR_INTERNAL",
    "ACP_ERROR_INVALID_PARAMS",
    "ACP_ERROR_INVALID_REQUEST",
    "ACP_ERROR_METHOD_NOT_FOUND",
    "ACP_ERROR_NOT_INITIALIZED",
    "ACP_ERROR_PARSE",
    "ACP_ERROR_REQUEST_CANCELLED",
    "ACP_ERROR_TIMEOUT",
    "ACP_PROTOCOL_VERSION",
    "ACP_VERSION",
    "CANONICAL_STATUSES",
    "JSONRPC_VERSION",
    "PROTOCOL_VERSION",
    "STOP_REASON_CANCELLED",
    "STOP_REASON_END_TURN",
    "STOP_REASON_MAX_TOKENS",
    "STOP_REASON_MAX_TURN_REQUESTS",
    "STOP_REASON_REFUSAL",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SUPPORTED_VERSIONS",
    "VALID_STOP_REASONS",
    "ACPAuthenticationError",
    "ACPCapabilities",
    "ACPError",
    "ACPPromptResult",
    "ACPProtocolError",
    "ACPRequest",
    "ACPResponse",
    "ACPSession",
    "ACPTimeoutError",
    "ACPTransportClosed",
    "ACPTransportError",
    "ACPUnsupportedVersionError",
    "auth_method_is_terminal",
    "auth_method_requires_protocol_auth",
    "clean_verification_evidence",
    "is_terminal_result",
    "json_compatible",
    "map_status_to_stop_reason",
    "negotiate_protocol_version",
    "normalize_prompt",
    "normalize_protocol_version",
    "normalize_status",
    "normalize_update",
    "result_answer",
    "status_to_stop_reason",
    "stop_reason_for_status",
    "text_from_updates",
    "validate_jsonrpc_id",
    "validate_params",
    "validate_session_id",
]
