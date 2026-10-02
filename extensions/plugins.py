"""Plugin registration and lifecycle management for Neo extensions.

The manager is intentionally independent of the CLI's bundle installer.  It
accepts typed :class:`PluginSpec` objects, already-created plugins, injected
factories, and an explicitly contained local Python file.  Activation and
deactivation are isolated: failures are retained in redacted plugin metadata,
partial hook registrations are rolled back, and peers continue to operate.
There is no registry, installation, or marketplace behavior in this module.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import re
import stat
import sys
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from shared import security

from .hooks import (
    Callback,
    HookContext,
    HookDecision,
    HookDeniedError,
    HookDispatchError,
    HookError,
    HookManager,
    HookOutcome,
    HookPayloadError,
    HookPoint,
    HookRecord,
    HookRegistrationError,
    HookSecurityError,
    HookValidationError,
)


class PluginError(Exception):
    """Base class for typed plugin lifecycle and validation failures."""

    def __str__(self) -> str:
        """Return a redacted public error string."""
        return security.redact_text(super().__str__())[:4096]

    def __repr__(self) -> str:
        """Return a redacted public error repr."""
        return f"{type(self).__name__}({str(self)!r})"


class PluginManifestError(PluginError, ValueError):
    """Raised when a plugin manifest is malformed or unsafe."""


class PluginValidationError(PluginManifestError):
    """Raised when a plugin specification violates its public contract."""


class PluginRegistrationError(PluginError):
    """Raised when a plugin cannot be added to or removed from the registry."""


class PluginNotFoundError(PluginRegistrationError):
    """Raised when an operation names an unregistered plugin."""


class PluginStateError(PluginError):
    """Raised when a lifecycle operation is invalid for the current state."""


class PluginLifecycleError(PluginError):
    """Raised when an explicit lifecycle failure is requested by a caller."""


class PluginActivationError(PluginLifecycleError):
    """Raised when plugin activation cannot complete."""


class PluginDeactivationError(PluginLifecycleError):
    """Raised when plugin deactivation cannot complete cleanly."""


class PluginSecurityError(PluginError, security.SecurityViolation):
    """Raised when plugin loading or activation crosses a security boundary."""


class PluginLoaderError(PluginError):
    """Raised when an explicitly requested local plugin cannot be loaded."""


HookCallback = Callback
HookValue = Union[HookManager, Any]
PluginFactory = Callable[..., Any]


class PluginState(str, Enum):
    """Stable lifecycle states exposed by :class:`Plugin`."""

    REGISTERED = "registered"
    ENABLED = "enabled"
    ACTIVE = "active"
    INACTIVE = "deactivated"
    DEACTIVATED = "deactivated"
    DISABLED = "disabled"
    FAILED = "failed"
    ERROR = "failed"
    REMOVED = "removed"

    def __str__(self) -> str:
        """Return the stable serialized state value."""
        return self.value


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_ENTRYPOINT_PATTERN = re.compile(r"^[A-Za-z0-9_./\\-]+$")
_MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "entrypoint",
        "path",
        "module",
        "enabled",
        "metadata",
        "hooks",
        "schema_version",
        "manifest_version",
        "api_version",
    }
)
_PLUGIN_SPEC_FIELDS = frozenset(
    {
        "name",
        "manifest",
        "enabled",
        "priority",
        "metadata",
        "hooks",
        "source",
        "path",
        "factory",
        "plugin_factory",
        "activate",
        "deactivate",
        "register_hooks",
        "version",
        "description",
    }
)
_HOOK_DESCRIPTOR_FIELDS = frozenset(
    {"callback", "handler", "priority", "name", "metadata"}
)
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_PLUGIN_BYTES = 512 * 1024


def _safe_text(value: Any, limit: int = 512) -> str:
    """Return a bounded shared-security-redacted string."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = ""
    return security.redact_text(text)[:limit]


def _safe_error(error: BaseException) -> str:
    """Return a redacted and bounded exception message."""
    try:
        message = str(error)
    except Exception:
        message = ""
    return security.redact_text(message or type(error).__name__)[:4096]


def _reviewed_manifest_text(value: Any, field: str, limit: int = 4096) -> str:
    """Return manifest prose that passed the untrusted-content boundary.

    A plugin manifest is operator-installed but externally authored, and its
    free-text fields (description, metadata strings) are rendered into
    listings, receipts, and prompts. Redaction is not enough: a description
    that reads as an instruction is an injection channel. Text the shared
    boundary flags is replaced with the quarantine marker; text it refuses is
    dropped entirely, because a manifest that cannot describe itself safely
    has no business rendering.
    """
    review = security.review_untrusted_source(value, source="plugin")
    if review.blocked:
        raise PluginManifestError(
            f"plugin manifest field {field!r} is quarantined by the "
            f"untrusted-content policy ({review.severity})"
        )
    if review.tainted:
        return review.text[:limit]
    return review.text[:limit]


def _reviewed_manifest_value(value: Any) -> Any:
    """Return recursively redacted AND injection-reviewed manifest data.

    Keys keep the existing credential-safe projection. String leaves go
    through the same untrusted-content review as the description, so a
    manifest cannot smuggle an instruction into a metadata field that a
    listing or prompt renders.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return security.redact_secrets(value)
    if isinstance(value, str):
        return _reviewed_manifest_text(value, "metadata", 512)
    if isinstance(value, Mapping):
        return {
            _safe_text(key): (
                security.REDACTED_SECRET
                if security.is_sensitive_key(key)
                else _reviewed_manifest_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_reviewed_manifest_value(item) for item in value]
    return security.redact_secrets(value)


def _safe_value(value: Any) -> Any:
    """Return a recursively redacted public value for plugin metadata."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return security.redact_secrets(value)
    if isinstance(value, Mapping):
        return {
            _safe_text(key): security.REDACTED_SECRET
            if security.is_sensitive_key(key)
            else _safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    try:
        return security.redact_secrets(value)
    except Exception:
        try:
            return _safe_text(repr(value))
        except Exception:
            return "<unrepresentable>"


def _validate_name(name: Any) -> str:
    """Validate a stable plugin name and return its normalized value."""
    text = str(name or "").strip()
    if not _NAME_PATTERN.fullmatch(text) or security.contains_secret(text):
        raise PluginValidationError(f"invalid plugin name: {name!r}")
    return text


def _validate_version(version: Any) -> str:
    """Validate a bounded semantic-like plugin version string."""
    text = str(version or "0.0.0").strip()
    if not _VERSION_PATTERN.fullmatch(text) or security.contains_secret(text):
        raise PluginManifestError(f"invalid plugin version: {version!r}")
    return text


def _validate_entrypoint(entrypoint: Any) -> str:
    """Validate an explicitly relative plugin entrypoint path."""
    text = str(entrypoint or "").strip()
    if not text:
        return ""
    if (
        "\x00" in text
        or not _ENTRYPOINT_PATTERN.fullmatch(text)
        or Path(text).is_absolute()
        or ".." in Path(text).parts
        or not text.casefold().endswith(".py")
    ):
        raise PluginManifestError(
            f"plugin entrypoint must be a contained .py path: {entrypoint!r}"
        )
    return text.replace("\\", "/")


def _as_manifest(value: Any) -> Optional["PluginManifest"]:
    """Normalize a manifest object or mapping into a PluginManifest."""
    if value is None:
        return None
    if isinstance(value, PluginManifest):
        return value
    if isinstance(value, Mapping):
        return PluginManifest.from_dict(value)
    raise PluginManifestError("plugin manifest must be a PluginManifest or mapping")


def _coerce_hook_point(value: Any) -> HookPoint:
    """Normalize a manifest hook-point spelling."""
    try:
        return HookPoint(value)
    except (TypeError, ValueError):
        try:
            from .hooks import _coerce_point

            return _coerce_point(value)
        except Exception:
            raise PluginManifestError(f"invalid plugin hook point: {value!r}") from None


def _manifest_version(value: Any, *, field_name: str = "schema_version") -> int:
    """Validate a supported manifest schema version."""
    if isinstance(value, bool):
        raise PluginManifestError("plugin manifest version must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise PluginManifestError(
            "plugin manifest version must be an integer"
        ) from None
    if parsed != _MANIFEST_SCHEMA_VERSION:
        raise PluginManifestError(f"unsupported plugin manifest {field_name}: {parsed}")
    return parsed


def _validate_manifest_versions(data: Mapping[str, Any]) -> int:
    """Validate all manifest version aliases and reject drift."""
    values = [
        (key, data[key])
        for key in ("schema_version", "manifest_version", "api_version")
        if key in data
    ]
    if not values:
        return _MANIFEST_SCHEMA_VERSION
    parsed = [_manifest_version(value, field_name=key) for key, value in values]
    if len(set(parsed)) != 1:
        raise PluginManifestError("plugin manifest version fields disagree")
    return parsed[0]


def _normalize_hooks(
    value: Any,
    *,
    error_type: type[PluginError] = PluginValidationError,
) -> dict[str, list[Any]]:
    """Validate and copy declarative hook callbacks without stringifying them."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise error_type("plugin hooks must be a mapping")
    result: dict[str, list[Any]] = {}
    for point, declaration in value.items():
        normalized_point = _coerce_hook_point(point)
        values = (
            list(declaration)
            if isinstance(declaration, (list, tuple))
            else [declaration]
        )
        if not values:
            raise error_type("plugin hook declarations must not be empty")
        normalized_values: list[Any] = []
        for item in values:
            if callable(item):
                normalized_values.append(item)
                continue
            if not isinstance(item, Mapping):
                raise error_type("plugin hook declarations must contain callables")
            unknown = set(item) - _HOOK_DESCRIPTOR_FIELDS
            if unknown:
                raise error_type("plugin hook declaration contains unknown fields")
            callback = item.get("callback", item.get("handler"))
            if not callable(callback):
                raise error_type("plugin hook declaration requires a callable")
            priority = item.get("priority", 0)
            if isinstance(priority, bool):
                raise error_type("plugin hook priority must be an integer")
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                raise error_type("plugin hook priority must be an integer") from None
            name = item.get("name", "")
            if not isinstance(name, str):
                raise error_type("plugin hook name must be a string")
            metadata = item.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise error_type("plugin hook metadata must be a mapping")
            normalized_values.append(
                {
                    "callback": callback,
                    "priority": priority,
                    "name": name,
                    "metadata": _safe_value(metadata),
                }
            )
        result[normalized_point.value] = normalized_values
    return result


def _call_with_context(callback: Callable[..., Any], context: Any) -> Any:
    """Invoke a user callback with a tolerant, non-arity-confusing signature."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(context)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    has_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if not positional and not has_varargs:
        if has_kwargs:
            return callback(context=context)
        return callback()
    required = [
        parameter
        for parameter in positional
        if parameter.default is inspect.Parameter.empty
    ]
    if len(required) <= 1:
        return callback(context)
    if len(positional) >= 2:
        return callback(context, None)
    return callback(context)


@dataclass(init=False)
class PluginManifest:
    """Validated metadata describing one local or injected plugin."""

    name: str
    version: str
    description: str
    entrypoint: str
    enabled: bool
    metadata: dict[str, Any]
    hooks: dict[str, Any]
    schema_version: int
    extra: dict[str, Any]

    def __init__(
        self,
        name: str = "",
        version: str = "0.0.0",
        description: str = "",
        entrypoint: str = "",
        enabled: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
        hooks: Optional[Mapping[str, Any]] = None,
        *,
        path: str = "",
        module: str = "",
        schema_version: int = _MANIFEST_SCHEMA_VERSION,
        manifest_version: Optional[int] = None,
        api_version: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Validate manifest fields and retain redacted extension metadata."""
        versions = [schema_version]
        if manifest_version is not None:
            versions.append(manifest_version)
        if api_version is not None:
            versions.append(api_version)
        parsed_versions = {
            _manifest_version(item, field_name="schema_version") for item in versions
        }
        if len(parsed_versions) != 1:
            raise PluginManifestError("plugin manifest version fields disagree")
        self.schema_version = parsed_versions.pop()
        entrypoints = [item for item in (entrypoint, path, module) if item]
        if entrypoints and any(item != entrypoints[0] for item in entrypoints[1:]):
            raise PluginManifestError("plugin manifest entrypoint fields disagree")
        chosen_entrypoint = entrypoints[0] if entrypoints else ""
        if extra:
            raise PluginManifestError("plugin manifest contains unsupported fields")
        self.name = _validate_name(name)
        self.version = _validate_version(version)
        self.description = _reviewed_manifest_text(description, "description", 4096)
        self.entrypoint = _validate_entrypoint(chosen_entrypoint)
        if not isinstance(enabled, bool):
            raise PluginManifestError("plugin manifest enabled must be boolean")
        self.enabled = enabled
        self.metadata = _reviewed_manifest_value(
            metadata if isinstance(metadata, Mapping) else {}
        )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PluginManifestError("plugin manifest metadata must be a mapping")
        self.hooks = _normalize_hooks(hooks, error_type=PluginManifestError)
        self.extra = {}
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        if not isinstance(self.extra, dict):
            self.extra = {}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginManifest":
        """Build a validated manifest from a JSON-compatible mapping."""
        if not isinstance(value, Mapping):
            raise PluginManifestError("plugin manifest must be a mapping")
        data = dict(value)
        unknown = set(data) - _MANIFEST_FIELDS
        if unknown:
            raise PluginManifestError("plugin manifest contains unsupported fields")
        _validate_manifest_versions(data)
        entrypoint_values = [
            str(data[key])
            for key in ("entrypoint", "path", "module")
            if key in data and data[key] is not None
        ]
        if entrypoint_values and any(
            value != entrypoint_values[0] for value in entrypoint_values[1:]
        ):
            raise PluginManifestError("plugin manifest entrypoint fields disagree")
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "0.0.0")),
            description=str(data.get("description", "")),
            entrypoint=entrypoint_values[0] if entrypoint_values else "",
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {}),
            hooks=data.get("hooks", {}),
            schema_version=data.get(
                "schema_version",
                data.get("manifest_version", data.get("api_version", 1)),
            ),
            manifest_version=data.get("manifest_version"),
            api_version=data.get("api_version"),
        )

    @classmethod
    def from_json(cls, value: str) -> "PluginManifest":
        """Build a manifest from an explicitly supplied JSON string."""
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            raise PluginManifestError("plugin manifest is not valid JSON") from None
        return cls.from_dict(data)

    @classmethod
    def load(
        cls,
        path: Union[str, Path],
        root: Union[str, Path],
    ) -> "PluginManifest":
        """Load a manifest from an explicitly contained local JSON file."""
        return load_manifest(path, root)

    @classmethod
    def from_file(
        cls,
        path: Union[str, Path],
        root: Union[str, Path],
    ) -> "PluginManifest":
        """Load a manifest from an explicitly contained JSON file."""
        return load_manifest(path, root)

    def validate(
        self: Union["PluginManifest", Mapping[str, Any]],
    ) -> "PluginManifest":
        """Validate an instance or a manifest mapping and return the instance."""
        if isinstance(self, Mapping):
            return validate_manifest(self)
        _validate_name(self.name)
        _validate_version(self.version)
        _validate_entrypoint(self.entrypoint)
        _manifest_version(self.schema_version)
        if not isinstance(self.metadata, Mapping):
            raise PluginManifestError("plugin manifest metadata must be a mapping")
        _normalize_hooks(self.hooks, error_type=PluginManifestError)
        return self

    @property
    def path(self) -> str:
        """Return the contained entrypoint path alias."""
        return self.entrypoint

    @property
    def module(self) -> str:
        """Return the entrypoint/module compatibility alias."""
        return self.entrypoint

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-compatible manifest mapping."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "enabled": self.enabled,
            "metadata": _safe_value(self.metadata),
            "hooks": _safe_value(self.hooks),
            "schema_version": self.schema_version,
        }

    def __repr__(self) -> str:
        """Return a manifest repr without raw metadata or entrypoint secrets."""
        return (
            f"PluginManifest(name={self.name!r}, version={self.version!r}, "
            f"enabled={self.enabled!r}, entrypoint={self.entrypoint!r})"
        )


@dataclass(init=False)
class PluginSpec:
    """A factory-backed or instance-backed plugin registration description."""

    name: str
    factory: Optional[PluginFactory]
    plugin: Any
    manifest: Optional[PluginManifest]
    enabled: bool
    priority: int
    metadata: dict[str, Any]
    hooks: dict[str, Any]
    source: Optional[Path]
    version: str
    description: str
    activate_callback: Optional[Callable[..., Any]]
    deactivate_callback: Optional[Callable[..., Any]]
    register_hooks_callback: Optional[Callable[..., Any]]

    def __init__(
        self,
        name: str = "",
        factory: Optional[PluginFactory] = None,
        *,
        plugin: Any = None,
        manifest: Any = None,
        enabled: Optional[bool] = None,
        priority: int = 0,
        metadata: Optional[Mapping[str, Any]] = None,
        hooks: Optional[Mapping[str, Any]] = None,
        source: Optional[Union[str, Path]] = None,
        path: Optional[Union[str, Path]] = None,
        plugin_factory: Optional[PluginFactory] = None,
        activate: Optional[Callable[..., Any]] = None,
        deactivate: Optional[Callable[..., Any]] = None,
        register_hooks: Optional[Callable[..., Any]] = None,
        version: str = "",
        description: str = "",
    ) -> None:
        """Normalize a plugin specification and validate its identity."""
        parsed_manifest = _as_manifest(manifest)
        chosen_name = name or (parsed_manifest.name if parsed_manifest else "")
        self.name = _validate_name(chosen_name)
        if parsed_manifest is None and (version or description):
            parsed_manifest = PluginManifest(
                name=self.name,
                version=version or "0.0.0",
                description=description,
            )
        self.manifest = parsed_manifest
        if (
            parsed_manifest is not None
            and version
            and str(version) != parsed_manifest.version
        ):
            raise PluginValidationError("plugin spec and manifest versions disagree")
        self.version = self.manifest.version if self.manifest is not None else version
        self.description = (
            self.manifest.description if self.manifest is not None else description
        )
        self.factory = factory or plugin_factory
        if self.factory is not None and not callable(self.factory):
            raise PluginValidationError("plugin factory must be callable")
        self.activate_callback = activate
        self.deactivate_callback = deactivate
        self.register_hooks_callback = register_hooks
        self.plugin = plugin
        if enabled is None:
            enabled = parsed_manifest.enabled if parsed_manifest is not None else True
        if not isinstance(enabled, bool):
            raise PluginValidationError("plugin enabled must be boolean")
        self.enabled = enabled
        if isinstance(priority, bool):
            raise PluginValidationError("plugin priority must be an integer")
        try:
            self.priority = int(priority)
        except (TypeError, ValueError):
            raise PluginValidationError("plugin priority must be an integer") from None
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PluginValidationError("plugin metadata must be a mapping")
        self.metadata = _safe_value(metadata if isinstance(metadata, Mapping) else {})
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        selected_hooks = hooks
        if selected_hooks is None and parsed_manifest is not None:
            selected_hooks = parsed_manifest.hooks
        self.hooks = _normalize_hooks(selected_hooks)
        source_path = source if source is not None else path
        self.source = (
            Path(source_path).expanduser() if source_path is not None else None
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginSpec":
        """Build a typed specification from a mapping without importing code."""
        if not isinstance(value, Mapping):
            raise PluginValidationError("plugin specification must be a mapping")
        data = dict(value)
        unknown = set(data) - _PLUGIN_SPEC_FIELDS
        if unknown:
            raise PluginValidationError("plugin specification contains unknown fields")
        manifest_data = {
            key: item for key, item in data.items() if key in _MANIFEST_FIELDS
        }
        return cls(
            name=str(data.get("name", "")),
            manifest=manifest_data or None,
            enabled=data.get("enabled"),
            priority=data.get("priority", 0),
            metadata=data.get("metadata"),
            hooks=data.get("hooks"),
            source=data.get("source", data.get("path")),
            plugin_factory=data.get("factory", data.get("plugin_factory")),
            activate=data.get("activate"),
            deactivate=data.get("deactivate"),
            register_hooks=data.get("register_hooks"),
            version=str(data.get("version", "")),
            description=str(data.get("description", "")),
        )

    @property
    def path(self) -> str:
        """Return the local source path alias."""
        return str(self.source) if self.source is not None else ""

    def validate(self) -> "PluginSpec":
        """Validate this specification and return it."""
        if not self.name:
            raise PluginValidationError("plugin name must not be empty")
        if (
            self.factory is None
            and self.plugin is None
            and self.manifest is None
            and self.activate_callback is None
            and self.deactivate_callback is None
            and self.register_hooks_callback is None
        ):
            raise PluginValidationError(
                "plugin requires a factory, instance, or manifest"
            )
        if self.factory is not None and not callable(self.factory):
            raise PluginValidationError("plugin factory must be callable")
        if self.manifest is not None:
            self.manifest.validate()
        _normalize_hooks(self.hooks)
        return self

    def create(self, manager: Optional["PluginManager"] = None) -> Any:
        """Instantiate this specification using its injected factory."""
        if self.plugin is not None:
            return self.plugin
        if self.factory is None:
            raise PluginValidationError("plugin has no factory")
        try:
            signature = inspect.signature(self.factory)
        except (TypeError, ValueError):
            return self.factory()
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if not positional and not has_varargs:
            return self.factory()
        required = [
            parameter
            for parameter in positional
            if parameter.default is inspect.Parameter.empty
        ]
        if len(required) <= 1:
            return self.factory(self)
        if len(positional) >= 2:
            return self.factory(self, manager)
        return self.factory(self)

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted specification mapping without callables."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "enabled": self.enabled,
            "priority": self.priority,
            "metadata": _safe_value(self.metadata),
            "hooks": _safe_value(self.hooks),
            "manifest": self.manifest.to_dict() if self.manifest is not None else None,
            "source": str(self.source) if self.source is not None else "",
            "factory": getattr(self.factory, "__name__", "") if self.factory else "",
        }

    def __repr__(self) -> str:
        """Return a spec repr that excludes callable and secret-bearing values."""
        return f"PluginSpec(name={self.name!r}, enabled={self.enabled!r}, priority={self.priority!r})"


class Plugin:
    """A lifecycle-aware plugin instance with optional declarative hooks."""

    def __init__(
        self,
        name: str = "",
        activate: Optional[Callable[..., Any]] = None,
        deactivate: Optional[Callable[..., Any]] = None,
        *,
        register_hooks: Optional[Callable[..., Any]] = None,
        setup: Optional[Callable[..., Any]] = None,
        teardown: Optional[Callable[..., Any]] = None,
        cleanup: Optional[Callable[..., Any]] = None,
        start: Optional[Callable[..., Any]] = None,
        stop: Optional[Callable[..., Any]] = None,
        on_activate: Optional[Callable[..., Any]] = None,
        on_deactivate: Optional[Callable[..., Any]] = None,
        hooks: Optional[Mapping[str, Any]] = None,
        manifest: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
        instance: Any = None,
        enabled: bool = True,
        version: str = "",
        description: str = "",
    ) -> None:
        """Create a plugin wrapper around explicit lifecycle callbacks."""
        self.name = _validate_name(name or getattr(instance, "name", ""))
        self.activate_callback = (
            activate
            or setup
            or start
            or on_activate
            or getattr(instance, "activate", None)
            or getattr(instance, "setup", None)
            or getattr(instance, "start", None)
        )
        self.deactivate_callback = (
            deactivate
            or teardown
            or cleanup
            or stop
            or on_deactivate
            or getattr(instance, "deactivate", None)
            or getattr(instance, "teardown", None)
            or getattr(instance, "cleanup", None)
            or getattr(instance, "stop", None)
        )
        self.register_hooks_callback = register_hooks or getattr(
            instance, "register_hooks", None
        )
        selected_hooks = (
            hooks if hooks is not None else getattr(instance, "hooks", None)
        )
        self.manifest = _as_manifest(manifest)
        if selected_hooks is None and self.manifest is not None:
            selected_hooks = self.manifest.hooks
        self.hooks = _normalize_hooks(selected_hooks)
        if self.manifest is None and (version or description):
            self.manifest = PluginManifest(
                name=self.name,
                version=version or "0.0.0",
                description=description,
            )
        self.version = self.manifest.version if self.manifest is not None else version
        self.description = (
            self.manifest.description if self.manifest is not None else description
        )
        self.metadata = _safe_value(metadata if isinstance(metadata, Mapping) else {})
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.instance = instance
        self.spec: Optional[PluginSpec] = None
        self.enabled = bool(enabled)
        self.state = PluginState.REGISTERED if enabled else PluginState.DISABLED
        self.error: Optional[str] = None
        self.hook_ids: set[str] = set()
        self.activation_count = 0
        self.deactivation_count = 0
        self.history: list[dict[str, Any]] = [
            {"state": self.state.value, "error": None}
        ]
        self._lock = threading.RLock()

    def _adopt(self, other: "Plugin") -> None:
        """Adopt a freshly created factory wrapper into this stable handle."""
        with self._lock:
            self.activate_callback = other.activate_callback
            self.deactivate_callback = other.deactivate_callback
            self.register_hooks_callback = other.register_hooks_callback
            self.hooks = dict(other.hooks)
            self.manifest = other.manifest
            self.version = other.version
            self.description = other.description
            self.metadata.update(other.metadata)
            self.instance = other.instance
            self.enabled = other.enabled

    def activate(self, context: Any = None) -> Any:
        """Invoke the configured activation callback."""
        if self.activate_callback is None:
            return None
        return _call_with_context(self.activate_callback, context)

    def deactivate(self, context: Any = None) -> Any:
        """Invoke the configured deactivation callback."""
        if self.deactivate_callback is None:
            return None
        return _call_with_context(self.deactivate_callback, context)

    def start(self, context: Any = None) -> Any:
        """Invoke activation using the lifecycle compatibility name."""
        return self.activate(context)

    def stop(self, context: Any = None) -> Any:
        """Invoke deactivation using the lifecycle compatibility name."""
        return self.deactivate(context)

    def register_hooks(self, manager: HookManager) -> list[str]:
        """Register declarative hooks and any explicit registration callback."""
        declarations = _normalize_hooks(self.hooks)
        ids: list[str] = []
        for point, values in declarations.items():
            normalized = _coerce_hook_point(point)
            for item in values:
                if isinstance(item, Mapping):
                    callback = item.get("callback", item.get("handler"))
                    hook_id = manager.register(
                        normalized,
                        callback,
                        priority=item.get("priority", 0),
                        plugin_id=self.name,
                        name=str(item.get("name", "")),
                        metadata=item.get("metadata"),
                    )
                else:
                    hook_id = manager.register(normalized, item, plugin_id=self.name)
                ids.append(hook_id)
        if self.register_hooks_callback is not None:
            returned = _call_with_context(self.register_hooks_callback, manager)
            if isinstance(returned, str):
                ids.append(returned)
            elif isinstance(returned, (list, tuple, set)):
                ids.extend(str(item) for item in returned)
        self.hook_ids.update(ids)
        return ids

    def set_state(self, state: PluginState | str) -> None:
        """Set the lifecycle state under the plugin lock."""
        try:
            normalized = state if isinstance(state, PluginState) else PluginState(state)
        except (TypeError, ValueError):
            raise PluginStateError(f"invalid plugin state: {state!r}") from None
        with self._lock:
            if self.state != normalized:
                self.state = normalized
                self.history.append({"state": normalized.value, "error": self.error})

    def set_error(self, error: Any) -> str:
        """Store a redacted error string in plugin metadata and return it."""
        if isinstance(error, BaseException):
            message = _safe_error(error)
            error_type = type(error).__name__
        else:
            message = _safe_text(error)
            error_type = "PluginError"
        with self._lock:
            self.error = message
            self.metadata["last_error"] = message
            self.metadata["error"] = message
            self.metadata["last_error_type"] = error_type
            if self.history:
                self.history[-1]["error"] = message
        return message

    def clear_error(self) -> None:
        """Clear the last lifecycle error."""
        with self._lock:
            self.error = None
            self.metadata.pop("last_error", None)
            self.metadata.pop("error", None)
            self.metadata.pop("last_error_type", None)
            if self.history:
                self.history[-1]["error"] = None

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted snapshot of plugin lifecycle state."""
        with self._lock:
            return {
                "name": self.name,
                "spec": self.spec.to_dict() if self.spec is not None else None,
                "version": self.version,
                "description": self.description,
                "state": self.state.value,
                "enabled": self.enabled,
                "error": self.error,
                "metadata": _safe_value(self.metadata),
                "activation_count": self.activation_count,
                "deactivation_count": self.deactivation_count,
                "hook_ids": sorted(self.hook_ids),
                "history": _safe_value(self.history),
                "manifest": self.manifest.to_dict()
                if self.manifest is not None
                else None,
            }

    def __repr__(self) -> str:
        """Return a redacted plugin repr."""
        return f"Plugin(name={self.name!r}, state={self.state.value!r}, enabled={self.enabled!r})"


@dataclass
class _Entry:
    """Internal manager record for one registered plugin."""

    spec: PluginSpec
    plugin: Optional[Plugin]
    order: int
    state: PluginState
    hydrated: bool = True
    hook_ids: set[str] = field(default_factory=set)
    error: Optional[str] = None

    def __post_init__(self) -> None:
        """Keep the plugin wrapper and entry state synchronized initially."""
        if self.plugin is not None:
            self.plugin.state = self.state


def _validate_loader_imports(
    tree: ast.AST, root: Path, allowed_imports: Sequence[str]
) -> None:
    """Reject imports not explicitly allowed or contained beside the file."""
    allowed = {str(item) for item in allowed_imports or ()}
    allowed.add("__future__")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"__import__", "eval", "exec", "compile"}
        ):
            raise PluginSecurityError(
                f"dynamic plugin execution is not allowed: {node.func.id}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".", 1)[0]
                if root_name not in allowed:
                    raise PluginSecurityError(
                        f"plugin import is not explicitly allowed: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                relative_parts = [".."] * max(0, node.level - 1)
                if module:
                    relative_parts.extend(module.split("."))
                    relative = root.joinpath(*relative_parts).with_suffix(".py")
                else:
                    relative = root.joinpath(*relative_parts, "__init__.py")
                try:
                    security.require_contained(root, relative)
                except security.SecurityViolation:
                    raise PluginSecurityError(
                        "relative plugin import escapes its root"
                    ) from None
                continue
            root_name = module.split(".", 1)[0]
            if root_name not in allowed:
                raise PluginSecurityError(
                    f"plugin import is not explicitly allowed: {module}"
                )


def _read_contained_bytes(
    root: Path,
    candidate: Union[str, Path],
    *,
    maximum: int,
    error_type: type[PluginError],
    suffix: str,
) -> tuple[Path, bytes]:
    """Read one contained regular file without reopening it for execution."""
    try:
        contained = security.require_contained(root, candidate, must_exist=True)
    except security.SecurityViolation:
        raise PluginSecurityError("plugin file is outside the explicit root") from None
    if contained.suffix.casefold() != suffix or not contained.is_file():
        raise error_type(f"plugin path must name an existing {suffix} file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(str(contained), flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise error_type("plugin path must name a regular file")
        if before.st_size > maximum:
            raise error_type("plugin file is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
            after = os.fstat(descriptor)
        if len(raw) > maximum:
            raise error_type("plugin file is too large")
        identity_before = (
            getattr(before, "st_dev", 0),
            getattr(before, "st_ino", 0),
            getattr(before, "st_size", 0),
        )
        identity_after = (
            getattr(after, "st_dev", 0),
            getattr(after, "st_ino", 0),
            getattr(after, "st_size", 0),
        )
        if identity_before != identity_after:
            raise PluginSecurityError("plugin file changed while it was read")
    except PluginError:
        raise
    except (OSError, ValueError) as exc:
        raise error_type("plugin file cannot be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        security.require_contained(root, contained, must_exist=True)
    except security.SecurityViolation:
        raise PluginSecurityError(
            "plugin file changed containment during read"
        ) from None
    return contained, raw


def _load_module(
    path: Path,
    root: Path,
    factory_name: str,
    allowed_imports: Sequence[str],
    source: bytes,
) -> Any:
    """Execute an already-validated source snapshot in a trusted in-process module."""
    digest = hashlib.sha256(source + str(path).encode("utf-8")).hexdigest()[:20]
    module_name = f"_neo_extension_plugin_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None:
        raise PluginLoaderError("plugin file has no Python import loader")
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__loader__ = None
    module.__package__ = ""
    module.__spec__ = spec
    module.__cached__ = None
    sys.modules[module_name] = module
    try:
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except security.SecurityViolation:
        sys.modules.pop(module_name, None)
        raise PluginSecurityError(
            "plugin file violated the security boundary"
        ) from None
    except Exception:
        sys.modules.pop(module_name, None)
        raise PluginLoaderError("plugin file could not be imported") from None
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        sys.modules.pop(module_name, None)
        raise PluginLoaderError(
            f"plugin file does not define callable {factory_name!r}"
        )
    return factory


def load_plugin_file(
    path: Union[str, Path],
    root: Union[str, Path],
    *,
    factory_name: str = "create_plugin",
    name: str = "",
    enabled: bool = True,
    allowed_imports: Sequence[str] = (),
    imports: Optional[Sequence[str]] = None,
) -> PluginSpec:
    """Load a factory from an explicitly contained local Python file.

    Both ``path`` and ``root`` are mandatory.  The file must be a regular,
    non-symlinked ``.py`` file below ``root``; imports are limited to standard
    library modules or names explicitly supplied in ``allowed_imports``.  The
    validated source bytes are executed directly in this process, so loaded
    plugin code is trusted in-process code and runs with the host's authority.
    """
    if not isinstance(factory_name, str) or not factory_name.isidentifier():
        raise PluginLoaderError("plugin factory_name must be a Python identifier")
    root_path = Path(root).expanduser().absolute()
    try:
        contained, source_bytes = _read_contained_bytes(
            root_path,
            path,
            maximum=_MAX_PLUGIN_BYTES,
            error_type=PluginLoaderError,
            suffix=".py",
        )
        source = source_bytes.decode("utf-8")
        tree = ast.parse(source, filename=str(contained))
    except PluginError:
        raise
    except (UnicodeDecodeError, SyntaxError):
        raise PluginLoaderError("plugin file cannot be read or parsed") from None
    permitted_imports = tuple(allowed_imports) + tuple(imports or ())
    _validate_loader_imports(tree, root_path, permitted_imports)
    try:
        factory = _load_module(
            contained,
            root_path,
            factory_name,
            permitted_imports,
            source_bytes,
        )
        security.require_contained(root_path, contained, must_exist=True)
        current = _read_contained_bytes(
            root_path,
            contained,
            maximum=_MAX_PLUGIN_BYTES,
            error_type=PluginSecurityError,
            suffix=".py",
        )[1]
        if current != source_bytes:
            raise PluginSecurityError("plugin file changed during execution")
    except PluginError:
        raise
    except security.SecurityViolation:
        raise PluginSecurityError(
            "plugin file changed containment during execution"
        ) from None
    plugin_name = name or contained.stem
    return PluginSpec(
        name=plugin_name,
        factory=factory,
        manifest=PluginManifest(
            name=plugin_name, entrypoint=str(contained.relative_to(root_path))
        ),
        enabled=enabled,
        source=contained,
    )


def validate_manifest(value: Any) -> PluginManifest:
    """Validate a manifest mapping or object and return the typed manifest."""
    parsed = _as_manifest(value)
    if parsed is None:
        raise PluginManifestError("manifest must not be empty")
    return parsed.validate()


def load_manifest(
    path: Union[str, Path],
    root: Union[str, Path],
) -> PluginManifest:
    """Read and validate a JSON manifest from an explicitly contained file."""
    root_path = Path(root).expanduser().absolute()
    try:
        _contained, raw = _read_contained_bytes(
            root_path,
            path,
            maximum=_MAX_MANIFEST_BYTES,
            error_type=PluginManifestError,
            suffix=".json",
        )
        data = json.loads(raw.decode("utf-8"))
    except PluginError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise PluginManifestError("plugin manifest cannot be read") from None
    return PluginManifest.from_dict(data)


class PluginManager:
    """Thread-safe plugin registry with isolated lifecycle transitions."""

    def __init__(
        self,
        hook_manager: Optional[HookManager] = None,
        *,
        hooks: Optional[HookManager] = None,
        factories: Optional[Mapping[str, PluginFactory]] = None,
        plugin_root: Optional[Union[str, Path]] = None,
        raise_on_error: bool = False,
    ) -> None:
        """Create a manager over an injected or newly-created hook manager."""
        selected_hooks = hook_manager or hooks or HookManager()
        if not isinstance(selected_hooks, HookManager):
            raise PluginRegistrationError("hook_manager must be a HookManager")
        self.hooks = selected_hooks
        self.hook_manager = selected_hooks
        self.plugin_root = (
            Path(plugin_root).expanduser() if plugin_root is not None else None
        )
        self.raise_on_error = bool(raise_on_error)
        self._factories: dict[str, PluginFactory] = {}
        if factories is not None:
            if not isinstance(factories, Mapping):
                raise PluginRegistrationError("factories must be a mapping")
            for name, factory in factories.items():
                if not callable(factory):
                    raise PluginRegistrationError(
                        f"factory for {name!r} is not callable"
                    )
                self._factories[_validate_name(name)] = factory
        self._entries: dict[str, _Entry] = {}
        self._sequence = 0
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._owner_locks: dict[str, threading.RLock] = {}

    @property
    def plugins(self) -> tuple[Plugin, ...]:
        """Return active and registered plugin objects in deterministic order."""
        return tuple(
            entry.plugin
            for entry in self._ordered_entries()
            if entry.plugin is not None
        )

    @property
    def plugin_handles(self) -> tuple[Plugin, ...]:
        """Return the plugin-handle collection alias."""
        return self.plugins

    @property
    def registry(self) -> Mapping[str, Plugin]:
        """Return a snapshot mapping names to plugin objects."""
        return {
            entry.spec.name: entry.plugin
            for entry in self._ordered_entries()
            if entry.plugin is not None
        }

    @property
    def plugin_map(self) -> Mapping[str, Plugin]:
        """Return the deterministic registry mapping alias."""
        return self.registry

    @property
    def plugin_states(self) -> Mapping[str, PluginState]:
        """Return a redacted state mapping in deterministic order."""
        return {entry.spec.name: entry.state for entry in self._ordered_entries()}

    @property
    def states(self) -> Mapping[str, PluginState]:
        """Return the plugin state mapping alias."""
        return self.plugin_states

    def _ordered_entries(self) -> list[_Entry]:
        """Return registry entries sorted by priority, name, then order."""
        with self._lock:
            return sorted(
                self._entries.values(),
                key=lambda entry: (
                    entry.spec.priority,
                    entry.spec.name.casefold(),
                    entry.order,
                ),
            )

    def _entry(self, name: Any) -> _Entry:
        """Return a registered entry or raise a typed lookup error."""
        candidate = getattr(name, "name", name)
        normalized = _validate_name(candidate)
        with self._lock:
            entry = self._entries.get(normalized)
        if entry is None:
            raise PluginNotFoundError(f"plugin is not registered: {normalized}")
        return entry

    def _owner_lock(self, name: str) -> threading.RLock:
        """Return the hook manager lock shared with in-flight dispatches."""
        normalized = _validate_name(name)
        return self.hooks.owner_lock(normalized)

    def _new_plugin(self, spec: PluginSpec, instance: Any) -> Plugin:
        """Adapt a factory result to the public Plugin wrapper."""
        if isinstance(instance, Plugin):
            plugin = instance
            plugin.name = spec.name
            if not plugin.hooks and spec.hooks:
                plugin.hooks = _normalize_hooks(spec.hooks)
            return plugin
        if isinstance(instance, Mapping):
            try:
                instance_hooks = instance.get("hooks")
                if instance_hooks is None:
                    instance_hooks = spec.hooks
                return Plugin(
                    name=spec.name,
                    activate=instance.get("activate") or spec.activate_callback,
                    deactivate=instance.get("deactivate") or spec.deactivate_callback,
                    register_hooks=instance.get("register_hooks")
                    or spec.register_hooks_callback,
                    hooks=instance_hooks,
                    manifest=spec.manifest,
                    metadata=spec.metadata,
                    instance=instance,
                    enabled=spec.enabled,
                )
            except Exception:
                raise PluginActivationError(
                    "plugin factory returned an invalid mapping"
                ) from None
        try:
            instance_hooks = getattr(instance, "hooks", None)
            if instance_hooks is None:
                instance_hooks = spec.hooks
            return Plugin(
                name=spec.name,
                activate=getattr(instance, "activate", None) or spec.activate_callback,
                deactivate=getattr(instance, "deactivate", None)
                or spec.deactivate_callback,
                register_hooks=getattr(instance, "register_hooks", None)
                or spec.register_hooks_callback,
                hooks=instance_hooks,
                manifest=spec.manifest,
                metadata=spec.metadata,
                instance=instance,
                enabled=spec.enabled,
            )
        except Exception:
            raise PluginActivationError(
                "plugin factory returned an invalid object"
            ) from None

    def _entry_plugin(self, entry: _Entry) -> Plugin:
        """Return the stable wrapper, hydrating a factory exactly once."""
        if entry.plugin is not None and entry.hydrated:
            return entry.plugin
        try:
            instance = entry.spec.create(self)
            created = self._new_plugin(entry.spec, instance)
        except (security.SecurityViolation, PluginSecurityError) as exc:
            raise PluginSecurityError(_safe_error(exc)) from None
        except Exception as exc:
            raise PluginActivationError(_safe_error(exc)) from None
        if entry.plugin is None:
            entry.plugin = created
        else:
            entry.plugin._adopt(created)
        entry.hydrated = True
        entry.plugin.hook_ids.update(self.hooks.registration_ids(entry.plugin.name))
        return entry.plugin

    def _set_state(
        self, entry: _Entry, state: PluginState, error: Optional[str] = None
    ) -> None:
        """Update entry and plugin state under the manager lock."""
        with self._lock:
            entry.state = state
            entry.error = error
            if entry.plugin is not None:
                entry.plugin.set_state(state)
                if error:
                    entry.plugin.set_error(error)
                elif state != PluginState.FAILED:
                    entry.plugin.clear_error()

    def _record_activation_failure(self, entry: _Entry, error: BaseException) -> str:
        """Remove partial hooks and persist a redacted activation failure."""
        message = _safe_error(error)
        self.hooks.remove_plugin(entry.spec.name)
        with self._lock:
            entry.hook_ids.clear()
            if entry.plugin is not None:
                entry.plugin.hook_ids.clear()
        self._set_state(entry, PluginState.FAILED, message)
        return message

    def _register_plugin_hooks(self, entry: _Entry, plugin: Plugin) -> None:
        """Register declarative and explicit plugin hooks in an owner scope."""
        with self.hooks.plugin_scope(plugin.name):
            returned = plugin.register_hooks(self.hooks)
        if returned is False:
            raise PluginActivationError("plugin hook registration returned False")
        after = set(self.hooks.registration_ids(plugin.name))
        with self._lock:
            plugin.hook_ids.update(after)
            entry.hook_ids.update(after)
        if returned:
            with self._lock:
                plugin.hook_ids.update(str(item) for item in returned)
                entry.hook_ids.update(str(item) for item in returned)

    def register(
        self,
        plugin: Any = None,
        factory: Optional[PluginFactory] = None,
        *,
        manifest: Any = None,
        name: str = "",
        enabled: Optional[bool] = None,
        priority: int = 0,
        metadata: Optional[Mapping[str, Any]] = None,
        hooks: Optional[Mapping[str, Any]] = None,
        activate: bool = False,
        source: Optional[Union[str, Path]] = None,
        plugin_factory: Optional[PluginFactory] = None,
        activate_now: Optional[bool] = None,
        auto_activate: Optional[bool] = None,
    ) -> Plugin:
        """Register a typed spec, instance, manifest mapping, or named factory.

        Registration is lazy for factories and does not activate by default.
        Pass ``activate=True`` when registration should immediately activate.
        The returned :class:`Plugin` wrapper remains available even when a
        deferred factory later fails.
        """
        if factory is None:
            factory = plugin_factory
        if activate_now is not None:
            activate = bool(activate_now)
        if auto_activate is not None:
            activate = bool(auto_activate)
        if isinstance(plugin, PluginSpec):
            spec = plugin
            if manifest is not None:
                spec.manifest = _as_manifest(manifest)
            if enabled is not None:
                spec.enabled = enabled
            if metadata is not None:
                if not isinstance(metadata, Mapping):
                    raise PluginValidationError("plugin metadata must be a mapping")
                spec.metadata = _safe_value(metadata)
            if hooks is not None:
                spec.hooks = _normalize_hooks(hooks)
        elif isinstance(plugin, Plugin):
            spec = PluginSpec(
                name=plugin.name or name,
                plugin=plugin,
                manifest=manifest or plugin.manifest,
                enabled=plugin.enabled if enabled is None else enabled,
                priority=priority,
                metadata=metadata or plugin.metadata,
                hooks=hooks if hooks is not None else plugin.hooks,
                source=source,
            )
        elif isinstance(plugin, (PluginManifest, Mapping)):
            parsed = _as_manifest(plugin)
            spec = PluginSpec(
                name=name or parsed.name,
                factory=factory,
                manifest=parsed,
                enabled=parsed.enabled if enabled is None else enabled,
                priority=priority,
                metadata=metadata or parsed.metadata,
                hooks=hooks if hooks is not None else parsed.hooks,
                source=source,
            )
        elif isinstance(plugin, str) or plugin is None:
            chosen_name = name or plugin or getattr(factory, "__name__", "")
            spec = PluginSpec(
                name=chosen_name,
                factory=factory,
                manifest=manifest,
                enabled=enabled,
                priority=priority,
                metadata=metadata,
                hooks=hooks,
                source=source,
            )
        elif callable(plugin) and factory is None:
            spec = PluginSpec(
                name=name or getattr(plugin, "__name__", ""),
                factory=plugin,
                manifest=manifest,
                enabled=enabled,
                priority=priority,
                metadata=metadata,
                hooks=hooks,
                source=source,
            )
        elif (
            hasattr(plugin, "activate")
            or hasattr(plugin, "deactivate")
            or hasattr(plugin, "setup")
            or hasattr(plugin, "teardown")
            or hasattr(plugin, "cleanup")
            or hasattr(plugin, "start")
            or hasattr(plugin, "stop")
        ):
            spec = PluginSpec(
                name=name or getattr(plugin, "name", ""),
                plugin=plugin,
                manifest=manifest,
                enabled=enabled,
                priority=priority,
                metadata=metadata,
                hooks=(hooks if hooks is not None else getattr(plugin, "hooks", {})),
                source=source,
            )
        else:
            raise PluginRegistrationError(
                "register expects PluginSpec, Plugin, manifest mapping, name, or lifecycle object"
            )
        if factory is not None:
            spec.factory = factory
        if manifest is not None and spec.manifest is None:
            spec.manifest = _as_manifest(manifest)
        if source is not None and spec.source is None:
            spec.source = Path(source).expanduser()
        if not spec.name:
            spec.name = _validate_name(name or getattr(factory, "__name__", ""))
        if spec.factory is None and spec.plugin is None:
            spec.factory = self._factories.get(spec.name)
        spec.validate()
        with self._lock:
            if spec.name in self._entries:
                raise PluginRegistrationError(
                    f"plugin is already registered: {spec.name}"
                )
            self._sequence += 1
            order = self._sequence
            initial = PluginState.REGISTERED if spec.enabled else PluginState.DISABLED
            if isinstance(spec.plugin, Plugin):
                wrapper = spec.plugin
            elif spec.plugin is not None:
                wrapper = self._new_plugin(spec, spec.plugin)
            else:
                wrapper = Plugin(
                    name=spec.name,
                    activate=spec.activate_callback,
                    deactivate=spec.deactivate_callback,
                    register_hooks=spec.register_hooks_callback,
                    hooks=spec.hooks,
                    manifest=spec.manifest,
                    metadata=spec.metadata,
                    enabled=spec.enabled,
                )
            entry = _Entry(
                spec=spec,
                plugin=wrapper,
                order=order,
                state=initial,
                hydrated=spec.plugin is not None
                or (
                    spec.factory is None
                    and (
                        spec.activate_callback is not None
                        or spec.deactivate_callback is not None
                        or spec.register_hooks_callback is not None
                    )
                ),
            )
            self._entries[spec.name] = entry
            wrapper.spec = spec
            wrapper.enabled = spec.enabled
            wrapper.set_state(initial)
        if activate:
            return self.activate(spec.name)
        return wrapper

    def register_plugin(self, *args: Any, **kwargs: Any) -> Plugin:
        """Register a plugin using the explicit compatibility method name."""
        return self.register(*args, **kwargs)

    def register_hook(self, *args: Any, **kwargs: Any) -> str:
        """Register a hook through the manager's explicit convenience API."""
        return self.hooks.register(*args, **kwargs)

    def remove_hook(self, hook_id: str) -> bool:
        """Remove a hook through the manager's explicit convenience API."""
        return self.hooks.unregister(hook_id)

    def register_spec(self, spec: PluginSpec, **kwargs: Any) -> Plugin:
        """Register a PluginSpec using the explicit method name."""
        return self.register(spec, **kwargs)

    def _plugin_for_entry(self, entry: _Entry) -> Plugin:
        """Return an entry wrapper, preserving plugin state on errors."""
        if entry.plugin is not None:
            return entry.plugin
        try:
            return self._entry_plugin(entry)
        except Exception as exc:
            if entry.plugin is None:
                placeholder = Plugin(
                    name=entry.spec.name,
                    manifest=entry.spec.manifest,
                    metadata=entry.spec.metadata,
                )
                with self._lock:
                    entry.plugin = placeholder
            self._set_state(entry, PluginState.FAILED, _safe_error(exc))
            if self.raise_on_error:
                if isinstance(exc, PluginError):
                    raise
                raise PluginActivationError(_safe_error(exc)) from None
            return entry.plugin

    def _activate_entry(self, entry: _Entry) -> Plugin:
        """Serialize activation for one owner and roll back partial hooks."""
        with self._owner_lock(entry.spec.name):
            return self._activate_entry_locked(entry)

    def _activate_entry_locked(self, entry: _Entry) -> Plugin:
        """Activate one entry after its owner lock has been acquired."""
        if not entry.spec.enabled or entry.state == PluginState.DISABLED:
            raise PluginStateError(f"plugin is disabled: {entry.spec.name}")
        if entry.state == PluginState.ACTIVE and entry.plugin is not None:
            return entry.plugin
        try:
            plugin = self._entry_plugin(entry)
            with self.hooks.plugin_scope(entry.spec.name):
                returned = plugin.activate(self.hooks)
            if returned is False:
                raise PluginActivationError("plugin activation callback returned False")
            self._register_plugin_hooks(entry, plugin)
            with self._lock:
                plugin.activation_count += 1
                plugin.enabled = True
                plugin.clear_error()
            self._set_state(entry, PluginState.ACTIVE)
            return plugin
        except (security.SecurityViolation, PluginSecurityError) as exc:
            message = self._record_activation_failure(entry, exc)
            raise PluginSecurityError(message) from None
        except Exception as exc:
            message = self._record_activation_failure(entry, exc)
            if self.raise_on_error:
                raise PluginActivationError(message) from None
        return entry.plugin or Plugin(name=entry.spec.name)

    def activate(self, name: str, *, raise_on_error: Optional[bool] = None) -> Plugin:
        """Activate a registered plugin once per active lifecycle.

        Callback failures are isolated and recorded on the plugin.  Set
        ``raise_on_error`` or construct the manager with that option to turn
        the recorded failure into a typed lifecycle exception.
        """
        with self._lifecycle_lock:
            entry = self._entry(name)
            should_raise = (
                self.raise_on_error if raise_on_error is None else raise_on_error
            )
            if entry.state == PluginState.ACTIVE and entry.plugin is not None:
                return entry.plugin
            if entry.state == PluginState.DISABLED or not entry.spec.enabled:
                raise PluginStateError(f"plugin is disabled: {entry.spec.name}")
            try:
                result = self._activate_entry(entry)
                if entry.state == PluginState.FAILED and should_raise:
                    raise PluginActivationError(
                        entry.error or "plugin activation failed"
                    )
                return result
            except PluginSecurityError:
                raise
            except PluginError:
                if should_raise:
                    raise
                return entry.plugin or Plugin(name=entry.spec.name)
            except Exception as exc:
                if should_raise:
                    raise PluginActivationError(_safe_error(exc)) from None
                return entry.plugin or Plugin(name=entry.spec.name)

    def activate_all(self, *, raise_on_error: bool = False) -> list[Plugin]:
        """Activate all enabled plugins in deterministic registry order."""
        results: list[Plugin] = []
        for entry in self._ordered_entries():
            if not entry.spec.enabled or entry.state == PluginState.DISABLED:
                continue
            try:
                results.append(
                    self.activate(entry.spec.name, raise_on_error=raise_on_error)
                )
            except PluginSecurityError:
                raise
            except Exception:
                if raise_on_error:
                    raise
        return results

    def _deactivate_entry(self, entry: _Entry) -> Plugin:
        """Serialize deactivation and guarantee owner cleanup on every exit."""
        with self._owner_lock(entry.spec.name):
            return self._deactivate_entry_locked(entry)

    def _deactivate_entry_locked(self, entry: _Entry) -> Plugin:
        """Deactivate one entry after its owner lock has been acquired."""
        if entry.plugin is None:
            self._set_state(entry, PluginState.INACTIVE, entry.error)
            return Plugin(name=entry.spec.name)
        plugin = entry.plugin
        if entry.state != PluginState.ACTIVE and plugin.state != PluginState.ACTIVE:
            self.hooks.remove_plugin(entry.spec.name)
            entry.hook_ids.clear()
            plugin.hook_ids.clear()
            self._set_state(entry, PluginState.INACTIVE, entry.error)
            return plugin
        try:
            with self.hooks.plugin_scope(entry.spec.name):
                returned = plugin.deactivate(self.hooks)
            if returned is False:
                raise PluginDeactivationError(
                    "plugin deactivation callback returned False"
                )
            with self._lock:
                plugin.deactivation_count += 1
                plugin.clear_error()
            self.hooks.remove_plugin(entry.spec.name)
            entry.hook_ids.clear()
            plugin.hook_ids.clear()
            self._set_state(entry, PluginState.INACTIVE)
            return plugin
        except (security.SecurityViolation, PluginSecurityError) as exc:
            message = _safe_error(exc)
            self.hooks.remove_plugin(entry.spec.name)
            entry.hook_ids.clear()
            plugin.hook_ids.clear()
            self._set_state(entry, PluginState.FAILED, message)
            raise PluginSecurityError(message) from None
        except Exception as exc:
            message = _safe_error(exc)
            self.hooks.remove_plugin(entry.spec.name)
            entry.hook_ids.clear()
            plugin.hook_ids.clear()
            self._set_state(entry, PluginState.FAILED, message)
            if self.raise_on_error:
                raise PluginDeactivationError(message) from None
            return plugin
        finally:
            self.hooks.remove_plugin(entry.spec.name)
            with self._lock:
                entry.hook_ids.clear()
                plugin.hook_ids.clear()

    def deactivate(self, name: str, *, raise_on_error: Optional[bool] = None) -> Plugin:
        """Deactivate a plugin and remove every hook it owns."""
        with self._lifecycle_lock:
            entry = self._entry(name)
            should_raise = (
                self.raise_on_error if raise_on_error is None else raise_on_error
            )
            try:
                result = self._deactivate_entry(entry)
                if entry.state == PluginState.FAILED and should_raise:
                    raise PluginDeactivationError(
                        entry.error or "plugin deactivation failed"
                    )
                return result
            except PluginSecurityError:
                raise
            except PluginError:
                if should_raise:
                    raise
                return entry.plugin or Plugin(name=entry.spec.name)
            except Exception as exc:
                if should_raise:
                    raise PluginDeactivationError(_safe_error(exc)) from None
                return entry.plugin or Plugin(name=entry.spec.name)

    def start(self, **kwargs: Any) -> list[Plugin]:
        """Activate all enabled plugins using the lifecycle alias."""
        return self.activate_all(**kwargs)

    def stop(self, **kwargs: Any) -> list[Plugin]:
        """Deactivate all active plugins using the lifecycle alias."""
        return self.deactivate_all(**kwargs)

    def deactivate_all(self, *, raise_on_error: bool = False) -> list[Plugin]:
        """Deactivate all active plugins in deterministic order."""
        results: list[Plugin] = []
        for entry in reversed(self._ordered_entries()):
            if entry.state == PluginState.ACTIVE:
                try:
                    results.append(
                        self.deactivate(entry.spec.name, raise_on_error=raise_on_error)
                    )
                except PluginSecurityError:
                    raise
                except Exception:
                    if raise_on_error:
                        raise
        return results

    def enable(self, name: str, *, activate: bool = False) -> Plugin:
        """Enable a registered plugin without silently activating it."""
        with self._lifecycle_lock:
            return self._enable_locked(name, activate=activate)

    def _enable_locked(self, name: str, *, activate: bool = False) -> Plugin:
        """Enable a plugin while lifecycle serialization is held."""
        entry = self._entry(name)
        with self._lock:
            entry.spec.enabled = True
            if entry.plugin is not None:
                entry.plugin.enabled = True
        self._set_state(entry, PluginState.REGISTERED)
        if activate:
            return self.activate(name)
        return entry.plugin or self._plugin_for_entry(entry)

    def disable(self, name: str, *, raise_on_error: Optional[bool] = None) -> Plugin:
        """Disable a plugin, deactivate it, and remove its hooks."""
        with self._lifecycle_lock:
            return self._disable_locked(name, raise_on_error=raise_on_error)

    def _disable_locked(
        self, name: str, *, raise_on_error: Optional[bool] = None
    ) -> Plugin:
        """Disable a plugin while lifecycle serialization is held."""
        entry = self._entry(name)
        should_raise = self.raise_on_error if raise_on_error is None else raise_on_error
        error: Optional[str] = entry.error
        if entry.state == PluginState.ACTIVE:
            try:
                self._deactivate_entry(entry)
                if entry.state == PluginState.FAILED and error is None:
                    error = entry.error
            except PluginSecurityError:
                raise
            except PluginError as exc:
                error = _safe_error(exc)
            except Exception as exc:
                error = _safe_error(exc)
        self.hooks.remove_plugin(entry.spec.name)
        entry.hook_ids.clear()
        if entry.plugin is not None:
            entry.plugin.hook_ids.clear()
        with self._lock:
            entry.spec.enabled = False
            if entry.plugin is not None:
                entry.plugin.enabled = False
        self._set_state(entry, PluginState.DISABLED, error)
        if error and should_raise:
            raise PluginDeactivationError(error)
        return entry.plugin or Plugin(name=entry.spec.name)

    def remove(self, name: str, *, raise_on_error: Optional[bool] = None) -> Plugin:
        """Deactivate, unregister, and mark a plugin removed."""
        with self._lifecycle_lock:
            return self._remove_locked(name, raise_on_error=raise_on_error)

    def _remove_locked(
        self, name: str, *, raise_on_error: Optional[bool] = None
    ) -> Plugin:
        """Remove a plugin while lifecycle serialization is held."""
        entry = self._entry(name)
        should_raise = self.raise_on_error if raise_on_error is None else raise_on_error
        error: Optional[str] = entry.error
        if entry.state == PluginState.ACTIVE:
            try:
                self._deactivate_entry(entry)
                if entry.state == PluginState.FAILED and error is None:
                    error = entry.error
            except PluginSecurityError:
                raise
            except Exception as exc:
                error = _safe_error(exc)
        self.hooks.remove_plugin(entry.spec.name)
        entry.hook_ids.clear()
        if entry.plugin is not None:
            entry.plugin.hook_ids.clear()
            if error:
                entry.plugin.set_error(error)
            entry.plugin.set_state(PluginState.REMOVED)
        with self._lock:
            self._entries.pop(entry.spec.name, None)
        if error and should_raise:
            raise PluginDeactivationError(error)
        return entry.plugin or Plugin(name=entry.spec.name)

    def unregister(self, name: str, **kwargs: Any) -> Plugin:
        """Remove a plugin using the compatibility method name."""
        return self.remove(name, **kwargs)

    def remove_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Remove a plugin using the explicit plugin method name."""
        return self.remove(name, **kwargs)

    def activate_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Activate a plugin using the explicit plugin method name."""
        return self.activate(name, **kwargs)

    def deactivate_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Deactivate a plugin using the explicit plugin method name."""
        return self.deactivate(name, **kwargs)

    def enable_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Enable a plugin using the explicit plugin method name."""
        return self.enable(name, **kwargs)

    def disable_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Disable a plugin using the explicit plugin method name."""
        return self.disable(name, **kwargs)

    def reload_plugin(self, name: str, **kwargs: Any) -> Plugin:
        """Reload a plugin using the explicit plugin method name."""
        return self.reload(name, **kwargs)

    def reload(self, name: str, *, activate: Optional[bool] = None) -> Plugin:
        """Reload a plugin, preserving its enabled setting and cleaning hooks."""
        with self._lifecycle_lock:
            return self._reload_locked(name, activate=activate)

    def _reload_locked(self, name: str, *, activate: Optional[bool] = None) -> Plugin:
        """Reload a plugin while lifecycle serialization is held."""
        entry = self._entry(name)
        was_active = entry.state == PluginState.ACTIVE
        should_activate = was_active if activate is None else bool(activate)
        if was_active:
            self._deactivate_entry(entry)
        self.hooks.remove_plugin(entry.spec.name)
        with self._lock:
            entry.plugin = Plugin(
                name=entry.spec.name,
                manifest=entry.spec.manifest,
                metadata=entry.spec.metadata,
                enabled=entry.spec.enabled,
            )
            entry.plugin.spec = entry.spec
            entry.hydrated = False
            entry.hook_ids.clear()
        self._set_state(
            entry,
            PluginState.REGISTERED if entry.spec.enabled else PluginState.DISABLED,
        )
        result = entry.plugin
        if should_activate and entry.spec.enabled:
            return self.activate(name)
        return result

    def get(self, name: str) -> Plugin:
        """Return a registered plugin handle without forcing a lazy factory."""
        entry = self._entry(name)
        return entry.plugin or self._plugin_for_entry(entry)

    def get_plugin(self, name: str) -> Plugin:
        """Return a registered plugin wrapper using the explicit name."""
        return self.get(name)

    def state(self, name: str) -> PluginState:
        """Return a registered plugin's current lifecycle state."""
        return self._entry(name).state

    def status(self, name: str) -> dict[str, Any]:
        """Return a redacted plugin status mapping."""
        return self.get(name).to_dict()

    def get_state(self, name: str) -> PluginState:
        """Return a plugin state using the explicit lookup name."""
        return self.state(name)

    def get_error(self, name: str) -> Optional[str]:
        """Return a plugin's redacted error using the explicit lookup name."""
        return self.error(name)

    def error(self, name: str) -> Optional[str]:
        """Return a plugin's redacted last error, if any."""
        entry = self._entry(name)
        if entry.plugin is not None:
            return entry.plugin.error
        return entry.error

    def list_plugins(self) -> list[Plugin]:
        """Return registered plugin handles in deterministic order."""
        return [
            entry.plugin
            for entry in self._ordered_entries()
            if entry.plugin is not None
        ]

    def names(self) -> tuple[str, ...]:
        """Return registered names in deterministic registry order."""
        return tuple(entry.spec.name for entry in self._ordered_entries())

    def list(self) -> list[Plugin]:
        """Return registered plugin handles using the short list name."""
        return self.list_plugins()

    def register_manifest(
        self,
        manifest: Any,
        *,
        root: Optional[Union[str, Path]] = None,
        factory: Optional[PluginFactory] = None,
        activate: bool = False,
        name: str = "",
        enabled: Optional[bool] = None,
    ) -> Plugin:
        """Validate and register a manifest, optionally with a factory."""
        if isinstance(manifest, (str, Path)):
            selected_root = root if root is not None else self.plugin_root
            if selected_root is None:
                raise PluginLoaderError(
                    "an explicit plugin root is required for manifest loading"
                )
            manifest = load_manifest(manifest, selected_root)
        parsed = _as_manifest(manifest)
        if parsed is None:
            raise PluginManifestError("manifest must not be empty")
        return self.register(
            parsed,
            factory=factory,
            activate=activate,
            name=name or parsed.name,
            enabled=enabled,
        )

    def load_manifest(
        self,
        path: Union[str, Path],
        root: Optional[Union[str, Path]] = None,
        *,
        factory: Optional[PluginFactory] = None,
        activate: bool = False,
        enabled: Optional[bool] = None,
    ) -> Plugin:
        """Load and register a manifest from an explicitly supplied root."""
        selected_root = root if root is not None else self.plugin_root
        if selected_root is None:
            raise PluginLoaderError(
                "an explicit plugin root is required for manifest loading"
            )
        manifest = load_manifest(path, selected_root)
        return self.register_manifest(
            manifest,
            factory=factory,
            activate=activate,
            enabled=enabled,
        )

    def load_plugin(
        self,
        path: Union[str, Path],
        root: Optional[Union[str, Path]] = None,
        *,
        factory_name: str = "create_plugin",
        activate: bool = False,
        enabled: bool = True,
        name: str = "",
        allowed_imports: Sequence[str] = (),
        imports: Optional[Sequence[str]] = None,
    ) -> Plugin:
        """Register a factory from an explicitly supplied contained file."""
        selected_root = root if root is not None else self.plugin_root
        if selected_root is None:
            raise PluginLoaderError(
                "an explicit plugin root is required for file loading"
            )
        spec = load_plugin_file(
            path,
            selected_root,
            factory_name=factory_name,
            name=name,
            enabled=enabled,
            allowed_imports=allowed_imports,
            imports=imports,
        )
        return self.register(spec, activate=activate)

    def load(
        self,
        path: Union[str, Path],
        root: Optional[Union[str, Path]] = None,
        **kwargs: Any,
    ) -> Plugin:
        """Register a local plugin using the short loader method name."""
        return self.load_plugin(path, root, **kwargs)

    def __contains__(self, name: object) -> bool:
        """Return whether a normalized plugin name is registered."""
        try:
            candidate = getattr(name, "name", name)
            return _validate_name(candidate) in self._entries
        except PluginError:
            return False

    def __getitem__(self, name: str) -> Plugin:
        """Return a registered plugin by name."""
        return self.get(name)

    def clear(self, *, raise_on_error: bool = False) -> int:
        """Remove every registered plugin and return the removal count."""
        names = list(reversed(self.names()))
        for name in names:
            self.remove(name, raise_on_error=raise_on_error)
        return len(names)

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted deterministic registry snapshot."""
        return {
            entry.spec.name: entry.plugin.to_dict()
            for entry in self._ordered_entries()
            if entry.plugin is not None
        }

    def __repr__(self) -> str:
        """Return a callback-free manager repr."""
        with self._lock:
            return f"PluginManager(plugins={len(self._entries)}, active={sum(item.state == PluginState.ACTIVE for item in self._entries.values())})"


load_plugin = load_plugin_file


__all__ = [
    "Callback",
    "HookCallback",
    "HookContext",
    "HookDecision",
    "HookDeniedError",
    "HookDispatchError",
    "HookError",
    "HookManager",
    "HookOutcome",
    "HookPayloadError",
    "HookPoint",
    "HookRecord",
    "HookRegistrationError",
    "HookSecurityError",
    "HookValidationError",
    "Plugin",
    "PluginActivationError",
    "PluginDeactivationError",
    "PluginError",
    "PluginFactory",
    "PluginLifecycleError",
    "PluginLoaderError",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginNotFoundError",
    "PluginRegistrationError",
    "PluginSecurityError",
    "PluginSpec",
    "PluginState",
    "PluginStateError",
    "PluginValidationError",
    "load_manifest",
    "load_plugin",
    "load_plugin_file",
    "validate_manifest",
]
