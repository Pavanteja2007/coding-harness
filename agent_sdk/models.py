"""Public SDK value objects for requests, results, tools, events, and versions."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
)

from shared.agent_contracts import SCHEMA_VERSION, CompletionStatus, RunSpec
from shared.agent_contracts import RunEvent as ContractRunEvent
from shared.agent_contracts import RunResult as ContractRunResult
from shared.agent_contracts import ToolCall as ContractToolCall
from shared.security import redact_event, redact_secrets, redact_text

from .errors import MissingVersionError, ToolResolutionError, UnsupportedVersionError

PROTOCOL_VERSION = 1
SDK_VERSION = "1"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)
ToolCall = ContractToolCall
RunEvent = ContractRunEvent
RunResult = ContractRunResult

__all__ = [
    "PROTOCOL_VERSION",
    "SDK_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "Capabilities",
    "Event",
    "EventEnvelope",
    "ProtocolCapabilities",
    "QueryRequest",
    "Result",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "RunSpec",
    "Tool",
    "ToolCall",
    "VersionNegotiation",
]


def _mapping(value: Any) -> Dict[str, Any]:
    """Return a shallow JSON-compatible mapping copy."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _version_values(value: Any, default: Iterable[int]) -> tuple[int, ...]:
    """Normalize one version or an iterable of versions into integers."""
    if value is None:
        source: Iterable[Any] = default
    elif isinstance(value, (str, int)):
        text = str(value).strip()
        if "," in text:
            source = [item.strip() for item in text.split(",") if item.strip()]
        else:
            source = [text]
    else:
        source = value
    values: list[int] = []
    for item in source:
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise UnsupportedVersionError(
                f"invalid protocol version: {item!r}", requested=(item,)
            ) from exc
        if number not in values:
            values.append(number)
    return tuple(values)


@dataclass(frozen=True)
class VersionNegotiation:
    """Deterministically negotiated protocol and event-schema versions."""

    protocol_version: int = PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION
    supported_protocol_versions: tuple[int, ...] = SUPPORTED_PROTOCOL_VERSIONS
    supported_schema_versions: tuple[int, ...] = SUPPORTED_SCHEMA_VERSIONS
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    server: str = "neo-agent-sdk"

    def __post_init__(self) -> None:
        """Validate the negotiated versions and freeze capability metadata."""
        if int(self.protocol_version) not in tuple(self.supported_protocol_versions):
            raise UnsupportedVersionError(
                "negotiated protocol version is not supported",
                supported=self.supported_protocol_versions,
                requested=(self.protocol_version,),
            )
        if int(self.schema_version) not in tuple(self.supported_schema_versions):
            raise UnsupportedVersionError(
                "negotiated schema version is not supported",
                supported=self.supported_schema_versions,
                requested=(self.schema_version,),
            )
        object.__setattr__(self, "protocol_version", int(self.protocol_version))
        object.__setattr__(self, "schema_version", int(self.schema_version))
        object.__setattr__(
            self,
            "supported_protocol_versions",
            tuple(int(item) for item in self.supported_protocol_versions),
        )
        object.__setattr__(
            self,
            "supported_schema_versions",
            tuple(int(item) for item in self.supported_schema_versions),
        )
        object.__setattr__(self, "capabilities", dict(self.capabilities or {}))

    @property
    def version(self) -> int:
        """Return the protocol version under a compact alias."""
        return self.protocol_version

    @property
    def protocol(self) -> int:
        """Return the protocol version under a short alias."""
        return self.protocol_version

    @property
    def schema(self) -> int:
        """Return the event schema version under a short alias."""
        return self.schema_version

    @property
    def supported(self) -> tuple[int, ...]:
        """Return the supported protocol versions."""
        return self.supported_protocol_versions

    @property
    def supported_versions(self) -> tuple[int, ...]:
        """Return the supported protocol versions under a generic alias."""
        return self.supported_protocol_versions

    @property
    def schema_versions(self) -> tuple[int, ...]:
        """Return the supported event schema versions."""
        return self.supported_schema_versions

    @property
    def headers(self) -> Dict[str, str]:
        """Return headers that advertise the negotiated versions."""
        return self.to_headers()

    @classmethod
    def negotiate(
        cls,
        requested_protocol: Any = PROTOCOL_VERSION,
        requested_schema: Any = SCHEMA_VERSION,
        *,
        protocol_versions: Iterable[int] = SUPPORTED_PROTOCOL_VERSIONS,
        schema_versions: Iterable[int] = SUPPORTED_SCHEMA_VERSIONS,
        protocol_version: Any = None,
        schema_version: Any = None,
        client_protocol_versions: Any = None,
        client_schema_versions: Any = None,
        server_protocol_versions: Any = None,
        server_schema_versions: Any = None,
        capabilities: Mapping[str, Any] | None = None,
        client: Mapping[str, Any] | None = None,
        server: Mapping[str, Any] | None = None,
    ) -> "VersionNegotiation":
        """Select the highest common protocol and schema versions deterministically."""
        if protocol_version is not None:
            requested_protocol = protocol_version
        if schema_version is not None:
            requested_schema = schema_version
        if client_protocol_versions is not None:
            requested_protocol = client_protocol_versions
        if client_schema_versions is not None:
            requested_schema = client_schema_versions
        if server_protocol_versions is not None:
            protocol_versions = server_protocol_versions
        if server_schema_versions is not None:
            schema_versions = server_schema_versions
        if requested_protocol is None or requested_schema is None:
            raise MissingVersionError("protocol and schema versions are required")
        if isinstance(requested_protocol, Mapping):
            client = requested_protocol
            requested_protocol = client.get(
                "protocol_version", client.get("protocol", PROTOCOL_VERSION)
            )
            requested_schema = client.get(
                "schema_version", client.get("schema", SCHEMA_VERSION)
            )
        if client is not None:
            requested_protocol = client.get(
                "protocol_version", client.get("protocol", requested_protocol)
            )
            requested_schema = client.get(
                "schema_version", client.get("schema", requested_schema)
            )
        server_protocols = tuple(protocol_versions)
        server_schemas = tuple(schema_versions)
        if server is not None:
            server_protocols = tuple(
                server.get("supported_protocol_versions", server_protocols)
            )
            server_schemas = tuple(
                server.get("supported_schema_versions", server_schemas)
            )
        requested_protocols = _version_values(
            requested_protocol, SUPPORTED_PROTOCOL_VERSIONS
        )
        requested_schemas = _version_values(requested_schema, SUPPORTED_SCHEMA_VERSIONS)
        common_protocols = sorted(
            set(requested_protocols) & set(int(item) for item in server_protocols),
            reverse=True,
        )
        common_schemas = sorted(
            set(requested_schemas) & set(int(item) for item in server_schemas),
            reverse=True,
        )
        if not common_protocols:
            raise UnsupportedVersionError(
                "no compatible protocol version",
                supported=server_protocols,
                requested=requested_protocols,
            )
        if not common_schemas:
            raise UnsupportedVersionError(
                "no compatible event schema version",
                supported=server_schemas,
                requested=requested_schemas,
            )
        return cls(
            protocol_version=common_protocols[0],
            schema_version=common_schemas[0],
            supported_protocol_versions=tuple(int(item) for item in server_protocols),
            supported_schema_versions=tuple(int(item) for item in server_schemas),
            capabilities=dict(capabilities or (server or {}).get("capabilities", {})),
            server=str((server or {}).get("server", "neo-agent-sdk")),
        )

    @classmethod
    def select(cls, *args: Any, **kwargs: Any) -> "VersionNegotiation":
        """Select a compatible version using the deterministic negotiation algorithm."""
        return cls.negotiate(*args, **kwargs)

    @classmethod
    def from_request(
        cls,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        *,
        required: bool = True,
    ) -> "VersionNegotiation":
        """Negotiate versions from HTTP headers, JSON fields, or query parameters."""
        sources: list[Mapping[str, Any]] = [
            dict(headers or {}),
            dict(body or {}),
            dict(query or {}),
        ]
        normalized: list[dict[str, Any]] = []
        for source in sources:
            normalized.append(
                {str(key).lower(): value for key, value in source.items()}
            )
        protocol_values: list[Any] = []
        schema_values: list[Any] = []
        protocol_sets: list[tuple[int, ...]] = []
        schema_sets: list[tuple[int, ...]] = []
        for source in normalized:
            source_protocols = [
                source[key]
                for key in ("x-neo-protocol-version", "protocol_version", "protocol")
                if key in source and source[key] not in (None, "")
            ]
            source_schemas = [
                source[key]
                for key in ("x-neo-schema-version", "schema_version", "schema")
                if key in source and source[key] not in (None, "")
            ]
            if source_protocols:
                protocol_values.extend(source_protocols)
                protocol_sets.append(_version_values(source_protocols, ()))
            if source_schemas:
                schema_values.extend(source_schemas)
                schema_sets.append(_version_values(source_schemas, ()))
        for values, label in (
            (protocol_sets, "protocol"),
            (schema_sets, "schema"),
        ):
            if values and any(item != values[0] for item in values[1:]):
                raise UnsupportedVersionError(
                    f"conflicting {label} versions in request",
                    supported=(values[0],),
                    requested=tuple(item for group in values for item in group),
                )
        if required and (not protocol_values or not schema_values):
            raise MissingVersionError(
                "protocol_version and schema_version are required for this endpoint"
            )
        if not protocol_values:
            protocol_values = [PROTOCOL_VERSION]
        if not schema_values:
            schema_values = [SCHEMA_VERSION]
        return cls.negotiate(protocol_values, schema_values)

    def to_headers(self) -> Dict[str, str]:
        """Return the negotiated version headers."""
        return {
            "X-Neo-Protocol-Version": str(self.protocol_version),
            "X-Neo-Schema-Version": str(self.schema_version),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted wire representation of the negotiation."""
        return {
            "server": self.server,
            "protocol_version": self.protocol_version,
            "protocol": self.protocol_version,
            "schema_version": self.schema_version,
            "schema": self.schema_version,
            "event_schema_version": self.schema_version,
            "version": self.protocol_version,
            "supported_protocol_versions": list(self.supported_protocol_versions),
            "supported_schema_versions": list(self.supported_schema_versions),
            "capabilities": redact_secrets(dict(self.capabilities)),
        }

    def __getitem__(self, key: str) -> Any:
        """Return a negotiation field using mapping access."""
        return self.to_dict()[str(key)]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a negotiation field or a supplied default."""
        return self.to_dict().get(str(key), default)


@dataclass(frozen=True)
class ProtocolCapabilities:
    """Versioned feature capabilities advertised during protocol negotiation."""

    protocol_version: int = PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION
    features: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize feature names and validate the advertised versions."""
        if int(self.protocol_version) != PROTOCOL_VERSION:
            raise UnsupportedVersionError(
                f"unsupported capability protocol version {self.protocol_version}",
                supported=SUPPORTED_PROTOCOL_VERSIONS,
                requested=(self.protocol_version,),
            )
        if int(self.schema_version) != SCHEMA_VERSION:
            raise UnsupportedVersionError(
                f"unsupported capability schema version {self.schema_version}",
                supported=SUPPORTED_SCHEMA_VERSIONS,
                requested=(self.schema_version,),
            )
        object.__setattr__(self, "protocol_version", PROTOCOL_VERSION)
        object.__setattr__(self, "schema_version", SCHEMA_VERSION)
        object.__setattr__(
            self, "features", tuple(sorted({str(item) for item in self.features}))
        )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted capability mapping."""
        return {
            "protocol_version": self.protocol_version,
            "protocol": self.protocol_version,
            "schema_version": self.schema_version,
            "schema": self.schema_version,
            "features": list(self.features),
            "metadata": dict(redact_secrets(self.metadata)),
        }


Capabilities = ProtocolCapabilities


@dataclass
class RunRequest:
    """A transport-neutral request to start or continue one agent run."""

    request: str
    repo_path: str = ""
    session_id: str = ""
    run_id: str = ""
    strategy: str = "daily"
    config: Dict[str, Any] = field(default_factory=dict)
    verification_policy: Dict[str, Any] = field(default_factory=dict)
    workspace_policy: Dict[str, Any] = field(default_factory=dict)
    workspace_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    wait: bool = True
    resume: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Normalize request fields without serializing runtime callbacks."""
        self.request = str(self.request or "")
        self.repo_path = str(self.repo_path or "")
        self.session_id = str(self.session_id or "")
        self.run_id = str(self.run_id or "")
        self.strategy = str(self.strategy or "daily").strip().lower()
        self.config = _mapping(self.config)
        self.verification_policy = _mapping(self.verification_policy)
        self.workspace_policy = _mapping(self.workspace_policy)
        self.workspace_id = str(
            self.workspace_id or self.workspace_policy.get("workspace_id", "")
        )
        self.metadata = _mapping(self.metadata)
        self.wait = bool(self.wait)
        self.resume = bool(self.resume)
        if int(self.schema_version) != SCHEMA_VERSION:
            raise UnsupportedVersionError(
                f"unsupported request schema version {self.schema_version}",
                supported=(SCHEMA_VERSION,),
                requested=(self.schema_version,),
            )
        self.schema_version = SCHEMA_VERSION

    @property
    def verification(self) -> Dict[str, Any]:
        """Return the verification policy under a compatibility alias."""
        return self.verification_policy

    @verification.setter
    def verification(self, value: Mapping[str, Any]) -> None:
        """Set the verification policy through its compatibility alias."""
        self.verification_policy = _mapping(value)

    @property
    def workspace(self) -> str:
        """Return the managed workspace identifier alias."""
        return self.workspace_id

    @workspace.setter
    def workspace(self, value: Any) -> None:
        """Set the managed workspace identifier through its alias."""
        self.workspace_id = str(value or "")

    @classmethod
    def from_value(
        cls,
        value: "RunRequest | QueryRequest | str | Mapping[str, Any]",
        **overrides: Any,
    ) -> "RunRequest":
        """Build a request from a string, mapping, or existing request object."""
        if isinstance(value, cls):
            data = value.to_dict()
        elif isinstance(value, str):
            data = {"request": value}
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            data = {"request": str(value or "")}
        data.update(overrides)
        if "verification" in data and "verification_policy" not in data:
            data["verification_policy"] = data.pop("verification")
        if "workspace" in data and "workspace_id" not in data:
            workspace = data.pop("workspace")
            data["workspace_id"] = getattr(workspace, "id", workspace)
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted JSON-compatible request representation."""
        return {
            "request": redact_secrets(self.request),
            "repo_path": self.repo_path,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "strategy": self.strategy,
            "config": redact_secrets(self.config),
            "verification_policy": redact_secrets(self.verification_policy),
            "workspace_policy": redact_secrets(self.workspace_policy),
            "workspace_id": self.workspace_id,
            "metadata": redact_secrets(self.metadata),
            "wait": self.wait,
            "resume": self.resume,
            "schema_version": self.schema_version,
        }

    def copy_with(self, **updates: Any) -> "RunRequest":
        """Return a copied request with selected fields replaced."""
        data = self.to_dict()
        data.update(updates)
        return type(self).from_value(data)

    def __getitem__(self, key: str) -> Any:
        """Return a request field using mapping access."""
        return self.to_dict()[str(key)]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a request field or a supplied default."""
        return self.to_dict().get(str(key), default)


@dataclass
class QueryRequest(RunRequest):
    """A synchronous, normally read-only question request."""

    strategy: str = "question"
    wait: bool = True


class Result(Mapping[str, Any]):
    """Mapping-friendly wrapper around a canonical :class:`RunResult`."""

    def __init__(self, value: ContractRunResult | Mapping[str, Any]) -> None:
        """Wrap a kernel result or a serialized result mapping."""
        if isinstance(value, ContractRunResult):
            self._result = value
        elif isinstance(value, Mapping):
            try:
                self._result = ContractRunResult.from_dict(value)
            except ValueError as exc:
                if "schema" in str(exc).casefold() or "status" in str(exc).casefold():
                    raise UnsupportedVersionError(str(exc)) from exc
                raise
        else:
            raise TypeError("Result requires RunResult or a mapping")
        if (
            self._result.status == CompletionStatus.COMPLETED_VERIFIED.value
            and not self._result.has_clean_verification
        ):
            self._result.status = CompletionStatus.COMPLETED_UNVERIFIED.value
            self._result.metadata["verification_downgraded"] = True

    @property
    def run_result(self) -> ContractRunResult:
        """Return the wrapped canonical result."""
        return self._result

    @property
    def raw(self) -> ContractRunResult:
        """Return the wrapped result under a short alias."""
        return self._result

    @property
    def status(self) -> str:
        """Return the canonical completion status string."""
        return str(self._result.status)

    @property
    def answer(self) -> str:
        """Return the redacted model answer."""
        return str(redact_secrets(self._result.answer))

    @property
    def changed_files(self) -> list[str]:
        """Return the changed-file list."""
        return list(self._result.changed_files)

    @property
    def verification_evidence(self) -> list[dict[str, Any]]:
        """Return the redacted verification evidence list."""
        return [
            dict(redact_secrets(item)) for item in self._result.verification_evidence
        ]

    @property
    def error(self) -> str:
        """Return the redacted result error."""
        return str(redact_secrets(self._result.error))

    @property
    def ok(self) -> bool:
        """Return whether the result is a completed state."""
        return bool(self._result.ok)

    @property
    def completed_verified(self) -> bool:
        """Return whether completion has clean verifier evidence."""
        return bool(self._result.completed_verified)

    @property
    def schema_version(self) -> int:
        """Return the canonical result schema version."""
        return int(self._result.schema_version)

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted JSON-compatible result."""
        return dict(redact_secrets(self._result.to_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Result":
        """Build a wrapper from a serialized canonical result."""
        return cls(ContractRunResult.from_dict(value))

    def __getitem__(self, key: str) -> Any:
        """Return a serialized result field by mapping key."""
        return self.to_dict()[str(key)]

    def __iter__(self) -> Iterator[str]:
        """Iterate over serialized result keys."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of serialized result fields."""
        return len(self.to_dict())

    def __getattr__(self, name: str) -> Any:
        """Expose compatible RunResult attributes without private lookups."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_result"), name)

    def __call__(self) -> "Result":
        """Return self so handle result access is convenient in both styles."""
        return self

    def __eq__(self, other: object) -> bool:
        """Compare public result wrappers by their canonical serialized form."""
        if isinstance(other, Result):
            return self.to_dict() == other.to_dict()
        if isinstance(other, ContractRunResult):
            return self.to_dict() == redact_secrets(other.to_dict())
        if isinstance(other, Mapping):
            return self.to_dict() == dict(redact_secrets(other))
        return NotImplemented


@dataclass
class Tool:
    """A public tool descriptor that can create typed kernel tool calls."""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    side_effect_class: str = "read_only"
    handler: Optional[Callable[..., Any]] = field(
        default=None, repr=False, compare=False
    )
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    tags: tuple[str, ...] = ()
    deferred: bool = False
    server: str = ""
    _catalog_descriptor: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize a tool descriptor and validate its contract version."""
        self.name = str(self.name or "").strip().lower()
        self.description = str(self.description or "")
        self.parameters = _mapping(self.parameters)
        self.side_effect_class = str(self.side_effect_class or "read_only")
        self.metadata = dict(redact_secrets(_mapping(self.metadata)))
        tag_values = (
            self.tags.split(",") if isinstance(self.tags, str) else (self.tags or ())
        )
        self.tags = tuple(
            dict.fromkeys(str(item).strip() for item in tag_values if str(item).strip())
        )
        if isinstance(self.deferred, str):
            self.deferred = self.deferred.strip().casefold() not in {
                "",
                "0",
                "false",
                "no",
            }
        else:
            self.deferred = bool(self.deferred)
        if self.deferred:
            self.parameters = {}
        self.server = str(self.server or "")
        self._catalog_descriptor = bool(
            self._catalog_descriptor or self.deferred or self.tags or self.server
        )
        if not self.name:
            raise ValueError("tool name is required")
        if int(self.schema_version) != SCHEMA_VERSION:
            raise UnsupportedVersionError(
                f"unsupported tool schema version {self.schema_version}",
                supported=(SCHEMA_VERSION,),
                requested=(self.schema_version,),
            )
        self.schema_version = SCHEMA_VERSION

    @property
    def schema(self) -> Dict[str, Any]:
        """Return the tool schema in a provider-neutral shape."""
        data = {
            "name": self.name,
            "description": self.description,
            "parameters": {} if self.deferred else dict(self.parameters),
            "side_effect_class": self.side_effect_class,
            "schema_version": self.schema_version,
        }
        if self._catalog_descriptor:
            data.update(
                {
                    "input_schema": dict(self.parameters),
                    "inputSchema": dict(self.parameters),
                    "tags": list(self.tags),
                    "deferred": self.deferred,
                    "server": self.server,
                    "metadata": dict(redact_secrets(self.metadata)),
                }
            )
        return data

    @property
    def input_schema(self) -> Dict[str, Any]:
        """Return the JSON input schema under the SDK's stable field name."""
        return dict(self.parameters)

    @property
    def inputSchema(self) -> Dict[str, Any]:
        """Return the JSON input schema under the MCP wire-field spelling."""
        return self.input_schema

    @property
    def is_deferred(self) -> bool:
        """Return whether the full input schema still needs resolution."""
        return self.deferred

    @property
    def model_schema(self) -> Optional[Dict[str, Any]]:
        """Return an eager schema or ``None`` for a deferred descriptor."""
        return None if self.deferred else self.input_schema

    def search_dict(self) -> Dict[str, Any]:
        """Return metadata-only discovery fields for a catalog result."""
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "deferred": self.deferred,
            "server": self.server,
        }

    def to_catalog_dict(self) -> Dict[str, Any]:
        """Return a redacted descriptor projection for a tool catalog."""
        parameters = {} if self.deferred else dict(self.parameters)
        data = dict(redact_secrets(self.schema))
        data.update(
            {
                "parameters": parameters,
                "input_schema": parameters,
                "inputSchema": parameters,
                "tags": list(self.tags),
                "deferred": self.deferred,
                "server": self.server,
                "metadata": dict(redact_secrets(self.metadata)),
            }
        )
        return dict(redact_secrets(data))

    def to_call(
        self,
        arguments: Mapping[str, Any] | None = None,
        *,
        call_id: str = "",
        target: str = "",
        status: str = "pending",
    ) -> ContractToolCall:
        """Create a typed Boundary-0 tool call for this descriptor."""
        if self.deferred:
            raise ToolResolutionError(
                f"tool schema must be resolved before calling {self.name}"
            )
        return ContractToolCall(
            call_id=call_id,
            tool=self.name,
            arguments=dict(arguments or {}),
            side_effect_class=self.side_effect_class,
            target=target,
            status=status,
            metadata=dict(self.metadata),
            schema_version=self.schema_version,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a redacted public tool descriptor."""
        if self._catalog_descriptor:
            return self.to_catalog_dict()
        return dict(redact_secrets(self.schema))

    def __getitem__(self, key: str) -> Any:
        """Return a tool descriptor field using mapping access."""
        return self.to_dict()[str(key)]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a tool descriptor field or a supplied default."""
        return self.to_dict().get(str(key), default)

    def __iter__(self) -> Iterator[str]:
        """Iterate over serialized tool descriptor fields."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of serialized tool descriptor fields."""
        return len(self.to_dict())

    def __repr__(self) -> str:
        """Return a bounded representation without schema or metadata values."""
        description = redact_text(self.description)[:120]
        return f"Tool(name={self.name!r}, description={description!r}, deferred={self.deferred!r})"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Tool":
        """Build a tool descriptor from a serialized mapping."""
        data = dict(value or {})
        return cls(
            name=data.get("name", data.get("tool", "")),
            description=data.get("description", ""),
            parameters=data.get(
                "parameters",
                data.get(
                    "input_schema", data.get("inputSchema", data.get("schema", {}))
                ),
            ),
            side_effect_class=data.get("side_effect_class", "read_only"),
            metadata=data.get("metadata", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            tags=data.get("tags", ()) or (),
            deferred=data.get("deferred", data.get("is_deferred", False)),
            server=str(data.get("server", "") or ""),
            _catalog_descriptor=any(
                key in data
                for key in (
                    "input_schema",
                    "inputSchema",
                    "tags",
                    "deferred",
                    "server",
                    "metadata",
                )
            ),
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the optional local handler for this tool."""
        if self.handler is None:
            raise TypeError(f"tool {self.name!r} has no local handler")
        return self.handler(*args, **kwargs)


@dataclass
class Event(ContractRunEvent):
    """A redacted, SSE-friendly public projection of a canonical RunEvent."""

    @property
    def event(self) -> str:
        """Return the event type using the wire-field name."""
        return self.event_type

    @property
    def type(self) -> str:
        """Return the event type under a short alias."""
        return self.event_type

    @property
    def name(self) -> str:
        """Return the event type under a name alias."""
        return self.event_type

    @property
    def id(self) -> str:
        """Return a stable event identity string."""
        return f"{self.run_id}:{self.sequence}"

    def to_dict(self) -> Dict[str, Any]:
        """Return the canonical redacted event row."""
        data = dict(redact_event(ContractRunEvent.to_dict(self)))
        data["event_type"] = self.event_type
        data["timestamp"] = self.timestamp
        return data

    def __getitem__(self, key: str) -> Any:
        """Return a canonical event field using mapping access."""
        return self.to_dict()[str(key)]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a canonical event field or a supplied default."""
        return self.to_dict().get(str(key), default)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Event":
        """Build a public event from a canonical or enveloped row."""
        data = dict(value or {})
        nested = data.get("event")
        if isinstance(nested, Mapping):
            merged = dict(nested)
            for key in ("protocol_version", "schema_version"):
                if key in data and key not in merged:
                    merged[key] = data[key]
            data = merged
        try:
            base = ContractRunEvent.from_dict(data)
        except ValueError as exc:
            raise UnsupportedVersionError(str(exc)) from exc
        return cls(
            sequence=base.sequence,
            timestamp=base.timestamp,
            session_id=base.session_id,
            run_id=base.run_id,
            turn_id=base.turn_id,
            event_type=base.event_type,
            payload=dict(redact_event(base.payload)),
            schema_version=base.schema_version,
        )


@dataclass
class EventEnvelope:
    """A versioned protocol envelope around one public event."""

    event: Event
    protocol_version: int = PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate the envelope and normalize a mapping event."""
        if isinstance(self.event, Mapping):
            self.event = Event.from_dict(self.event)
        if not isinstance(self.event, Event):
            self.event = Event.from_dict(self.event.to_dict())
        if int(self.protocol_version) != PROTOCOL_VERSION:
            raise UnsupportedVersionError(
                f"unsupported event protocol version {self.protocol_version}",
                supported=SUPPORTED_PROTOCOL_VERSIONS,
                requested=(self.protocol_version,),
            )
        if int(self.schema_version) != SCHEMA_VERSION:
            raise UnsupportedVersionError(
                f"unsupported event schema version {self.schema_version}",
                supported=SUPPORTED_SCHEMA_VERSIONS,
                requested=(self.schema_version,),
            )
        self.protocol_version = PROTOCOL_VERSION
        self.schema_version = SCHEMA_VERSION

    @property
    def sequence(self) -> int:
        """Return the wrapped event sequence."""
        return self.event.sequence

    @property
    def event_type(self) -> str:
        """Return the wrapped event type."""
        return self.event.event_type

    @property
    def type(self) -> str:
        """Return the wrapped event type under a short alias."""
        return self.event.event_type

    def to_dict(self) -> Dict[str, Any]:
        """Return a flat, SSE-friendly envelope with compatibility fields."""
        data = self.event.to_dict()
        data.update(
            {
                "protocol_version": self.protocol_version,
                "schema_version": self.schema_version,
                "event_type": self.event.event_type,
            }
        )
        return data

    def __getitem__(self, key: str) -> Any:
        """Return an envelope field using mapping access."""
        return self.to_dict()[str(key)]

    def get(self, key: str, default: Any = None) -> Any:
        """Return an envelope field or a supplied default."""
        return self.to_dict().get(str(key), default)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        """Build an envelope from a flat or nested event row."""
        data = dict(value or {})
        nested = data.get("event")
        if isinstance(nested, Mapping):
            event_data = dict(nested)
        else:
            event_data = data
        return cls(
            event=Event.from_dict(event_data),
            protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


def new_run_id() -> str:
    """Return a generated filesystem-safe run identifier."""
    return f"run-{uuid.uuid4().hex}"


def new_session_id() -> str:
    """Return a generated filesystem-safe session identifier."""
    return f"session-{uuid.uuid4().hex}"


def new_workspace_id() -> str:
    """Return a generated filesystem-safe workspace identifier."""
    return f"workspace-{uuid.uuid4().hex}"


def event_now() -> float:
    """Return the current event timestamp."""
    return time.time()
