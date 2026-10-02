"""Portable recipe data models and redacted failures."""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike
from typing import Any, Iterator, Optional

from shared.security import (
    contains_secret,
    is_sensitive_key,
    redact_secrets,
    redact_text,
)

SCHEMA_VERSION = 1
PARAMETER_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "list", "object"}
)
STEP_KINDS = frozenset({"task", "tool", "subrecipe"})
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_TOOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")


class _Missing:
    """Sentinel used to distinguish an omitted default from ``None``."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


class RecipeError(Exception):
    """Base class for recipe failures whose messages are always redacted."""

    code = "recipe_error"

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        safe_message = redact_text(message)
        safe_details = redact_secrets(dict(details or {}))
        self.message = safe_message
        self.details = safe_details
        super().__init__(safe_message)

    def __repr__(self) -> str:
        """Return a representation that cannot disclose credentials."""
        return f"{type(self).__name__}({self.message!r})"


class RecipeParseError(RecipeError):
    """Raised when recipe text is outside the supported safe YAML subset."""

    code = "recipe_parse_error"


class RecipeValidationError(RecipeError):
    """Raised when a recipe or supplied value violates its schema."""

    code = "recipe_validation_error"


class RecipeSchemaError(RecipeValidationError):
    """Raised for an unsupported or structurally invalid recipe schema."""

    code = "recipe_schema_error"


class RecipeParameterError(RecipeValidationError):
    """Raised for invalid parameters, defaults, or interpolation."""

    code = "recipe_parameter_error"


class RecipeTemplateError(RecipeParameterError):
    """Raised for malformed, missing, or unknown template variables."""

    code = "recipe_template_error"


class RecipeToolError(RecipeValidationError):
    """Raised when required or referenced tools are unavailable."""

    code = "recipe_tool_error"


class RecipeSubrecipeError(RecipeValidationError):
    """Raised when a subrecipe cannot be resolved."""

    code = "recipe_subrecipe_error"


class RecipeCycleError(RecipeSubrecipeError):
    """Raised when subrecipe expansion encounters a cycle."""

    code = "recipe_cycle_error"


class RecipeDepthError(RecipeSubrecipeError):
    """Raised when subrecipe expansion exceeds its configured depth."""

    code = "recipe_depth_error"


class RecipeLimitError(RecipeValidationError):
    """Raised when a recipe expansion exceeds a configured count limit."""

    code = "recipe_limit_error"


class RecipeSecurityError(RecipeValidationError):
    """Raised when recipe input crosses a containment or redaction boundary."""

    code = "recipe_security_error"


class RecipeCacheError(RecipeError):
    """Base class for local recipe cache failures."""

    code = "recipe_cache_error"


class RecipeCacheCorruptionError(RecipeCacheError):
    """Raised when a cache entry fails integrity or schema validation."""

    code = "recipe_cache_corruption"


class RecipeExecutionError(RecipeError):
    """Raised when recipe execution cannot be started or completed."""

    code = "recipe_execution_error"


ParseError = RecipeParseError
ValidationError = RecipeValidationError
CacheError = RecipeCacheError
ExecutionError = RecipeExecutionError
RecipeYAMLError = RecipeParseError
RecipeValidationFailure = RecipeValidationError
RecipeParameterValidationError = RecipeParameterError
RecipeSchemaValidationError = RecipeSchemaError
RecipeToolValidationError = RecipeToolError
RecipeSubrecipeValidationError = RecipeSubrecipeError
RecipeTraversalError = RecipeSecurityError


def _safe_copy(value: Any) -> Any:
    """Copy a JSON-like value without retaining caller-owned containers."""
    if value is MISSING:
        return MISSING
    return copy.deepcopy(value)


def _redacted(value: Any) -> Any:
    """Return the shared security redacted representation of a value."""
    return redact_secrets(value)


def _jsonable(value: Any) -> Any:
    """Convert supported recipe values to deterministic JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, PathLike):
        return str(value)
    return str(value)


def _canonical(value: Any) -> str:
    """Serialize a value canonically for stable digests."""
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_name(value: Any, *, label: str = "name") -> str:
    """Validate a portable recipe, parameter, or strategy identifier."""
    if not isinstance(value, str) or not value or not _NAME_RE.fullmatch(value):
        raise RecipeSchemaError(f"{label} is not a safe identifier")
    if ".." in value or contains_secret(value):
        raise RecipeSecurityError(f"{label} has an unsafe value")
    return value


def _validate_tool_name(value: Any) -> str:
    """Validate a portable tool name without accepting path syntax."""
    if not isinstance(value, str) or not value or not _TOOL_RE.fullmatch(value):
        raise RecipeToolError("tool name is not a safe identifier")
    if ".." in value or contains_secret(value):
        raise RecipeSecurityError("tool name has an unsafe value")
    return value


def _validate_reference(value: Any) -> str:
    """Validate a relative recipe reference and reject traversal syntax."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RecipeSubrecipeError("subrecipe reference is not a safe relative name")
    if "\x00" in value or any(ord(char) < 32 for char in value):
        raise RecipeSecurityError("subrecipe reference contains control characters")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise RecipeSecurityError("absolute subrecipe reference is not allowed")
    if (
        "\\" in value
        or ".." in value
        or any(token in value.casefold() for token in ("%2e", "%2f", "%5c"))
    ):
        raise RecipeSecurityError("subrecipe reference traversal is not allowed")
    if any(part in ("", ".") for part in value.split("/")):
        raise RecipeSecurityError("subrecipe reference is not a safe relative path")
    if contains_secret(value):
        raise RecipeSecurityError("subrecipe reference has an unsafe value")
    return value


class RecipeParameter:
    """One typed recipe parameter and its validation policy.

    ``default`` uses a private sentinel so ``None`` remains a meaningful YAML
    value. ``min`` and ``max`` are accepted as constructor aliases for the
    serialized ``minimum`` and ``maximum`` fields.
    """

    def __init__(
        self,
        name: str,
        type: str = "string",
        required: bool = True,
        default: Any = MISSING,
        description: str = "",
        choices: Optional[Sequence[Any]] = None,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
        *,
        min: Optional[float] = None,
        max: Optional[float] = None,
        param_type: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> None:
        """Create a parameter from already parsed values."""
        selected_type = param_type or kind or type
        if not isinstance(selected_type, str):
            raise RecipeSchemaError("parameter type must be a string")
        selected_type = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "dict": "object",
        }.get(selected_type, selected_type.casefold())
        if selected_type not in PARAMETER_TYPES:
            raise RecipeSchemaError(f"unsupported parameter type: {selected_type}")
        self.name = _validate_name(name, label="parameter name")
        self.type = selected_type
        if not isinstance(required, bool):
            raise RecipeSchemaError("parameter required must be boolean")
        self.required = required
        self.default = _safe_copy(default)
        if not isinstance(description, str):
            raise RecipeSchemaError("parameter description must be a string")
        self.description = description
        if choices is not None and (
            isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence)
        ):
            raise RecipeSchemaError("parameter choices must be a sequence")
        self.choices = (
            None if choices is None else tuple(_safe_copy(item) for item in choices)
        )
        if min is not None:
            if minimum is not None and minimum != min:
                raise RecipeSchemaError("minimum and min disagree")
            minimum = min
        if max is not None:
            if maximum is not None and maximum != max:
                raise RecipeSchemaError("maximum and max disagree")
            maximum = max
        if minimum is not None and (
            isinstance(minimum, bool) or not isinstance(minimum, (int, float))
        ):
            raise RecipeSchemaError("parameter minimum must be numeric")
        if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, (int, float))
        ):
            raise RecipeSchemaError("parameter maximum must be numeric")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise RecipeSchemaError("parameter minimum cannot exceed maximum")
        self.minimum = minimum
        self.maximum = maximum
        if self.default is not MISSING:
            _reject_secret_default(self.name, self.default)
        if self.choices is not None:
            for choice in self.choices:
                if contains_secret(choice):
                    raise RecipeSecurityError(
                        "parameter choices contain secret-shaped data"
                    )

    @property
    def kind(self) -> str:
        """Return the normalized parameter type."""
        return self.type

    @property
    def has_default(self) -> bool:
        """Return whether a default was explicitly supplied."""
        return self.default is not MISSING

    @property
    def min(self) -> Optional[float]:
        """Return the lower bound using the concise constructor spelling."""
        return self.minimum

    @property
    def max(self) -> Optional[float]:
        """Return the upper bound using the concise constructor spelling."""
        return self.maximum

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a YAML/JSON-compatible parameter mapping."""
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }
        if self.has_default:
            result["default"] = (
                _redacted(self.default) if redact else _safe_copy(self.default)
            )
        if self.choices is not None:
            result["choices"] = (
                _redacted(list(self.choices)) if redact else list(self.choices)
            )
        if self.minimum is not None:
            result["min"] = self.minimum
        if self.maximum is not None:
            result["max"] = self.maximum
        return result

    def normalized(self) -> dict[str, Any]:
        """Return raw deterministic data for content-addressed cache keys."""
        return self.to_dict(redact=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, name: str = "") -> "RecipeParameter":
        """Build a parameter from a strict mapping representation."""
        if not isinstance(data, Mapping):
            raise RecipeSchemaError("parameter definition must be a mapping")
        allowed = {
            "name",
            "type",
            "required",
            "default",
            "description",
            "choices",
            "min",
            "max",
            "minimum",
            "maximum",
        }
        unknown = set(data) - allowed
        if unknown:
            raise RecipeSchemaError("parameter definition contains unknown fields")
        actual_name = data.get("name", name)
        if not isinstance(actual_name, str) or not actual_name:
            raise RecipeSchemaError("parameter definition requires a name")
        if "type" in data and not isinstance(data["type"], str):
            raise RecipeSchemaError("parameter type must be a string")
        if "required" in data and not isinstance(data["required"], bool):
            raise RecipeSchemaError("parameter required must be boolean")
        if "description" in data and not isinstance(data["description"], str):
            raise RecipeSchemaError("parameter description must be a string")
        if (
            "choices" in data
            and data["choices"] is not None
            and (
                isinstance(data["choices"], (str, bytes))
                or not isinstance(data["choices"], Sequence)
            )
        ):
            raise RecipeSchemaError("parameter choices must be a sequence")
        return cls(
            actual_name,
            type=data.get("type", "string"),
            required=data.get("required", True),
            default=data.get("default", MISSING),
            description=data.get("description", ""),
            choices=data.get("choices"),
            minimum=data.get("minimum", data.get("min")),
            maximum=data.get("maximum", data.get("max")),
        )

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeParameter({self.to_dict(redact=True)!r})"


class RecipeStep:
    """One ordered task, tool, or subrecipe step."""

    def __init__(
        self,
        kind: str = "",
        task: str = "",
        tool: str = "",
        recipe: str = "",
        strategy: str = "",
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        type: Optional[str] = None,
        step_type: Optional[str] = None,
        subrecipe: Optional[str] = None,
        name: Optional[str] = None,
        action: Optional[str] = None,
        args: Optional[Mapping[str, Any]] = None,
        step_id: Optional[str] = None,
        description: str = "",
    ) -> None:
        """Create a step from normalized fields."""
        selected_kind = type or step_type or kind
        if not isinstance(selected_kind, str):
            raise RecipeSchemaError("step type must be a string")
        selected_kind = selected_kind.casefold()
        selected_kind = {
            "sub_recipe": "subrecipe",
            "sub-recipe": "subrecipe",
        }.get(selected_kind, selected_kind)
        if selected_kind not in STEP_KINDS:
            raise RecipeSchemaError("step type must be task, tool, or subrecipe")
        if parameters is None and args is not None:
            parameters = args
        if action is not None:
            if selected_kind == "tool" and not tool:
                tool = action
            elif selected_kind == "task" and not task:
                task = action
        if subrecipe is not None:
            if recipe and recipe != subrecipe:
                raise RecipeSchemaError("step recipe and subrecipe disagree")
            recipe = subrecipe
        if selected_kind == "task" and not task and name:
            task = name
        if selected_kind == "tool" and not tool and name:
            tool = name
        if selected_kind == "subrecipe" and not recipe and name:
            recipe = name
        if (
            not isinstance(task, str)
            or not isinstance(tool, str)
            or not isinstance(recipe, str)
        ):
            raise RecipeSchemaError("step fields must be strings")
        if not isinstance(strategy, str):
            raise RecipeSchemaError("step strategy must be a string")
        if not isinstance(description, str):
            raise RecipeSchemaError("step description must be a string")
        if step_id is not None and not isinstance(step_id, str):
            raise RecipeSchemaError("step id must be a string")
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, Mapping):
            raise RecipeSchemaError("step parameters must be a mapping")
        self.kind = selected_kind
        self.task = task
        self.tool = tool
        self.recipe = recipe
        self.strategy = strategy
        self.parameters = _safe_copy(dict(parameters))
        self.step_id = step_id or ""
        self.description = description

    @property
    def type(self) -> str:
        """Return the serialized step type."""
        return self.kind

    @property
    def action(self) -> str:
        """Return the task text or tool name represented by this step."""
        return self.name

    @property
    def name(self) -> str:
        """Return the task or subrecipe reference represented by the step."""
        if self.kind == "task":
            return self.task
        if self.kind == "subrecipe":
            return self.recipe
        return self.tool

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a strict YAML/JSON-compatible step mapping."""
        result: dict[str, Any] = {"type": self.kind}
        if self.kind == "task":
            result["task"] = _redacted(self.task) if redact else self.task
            if self.strategy:
                result["strategy"] = self.strategy
        elif self.kind == "tool":
            result["tool"] = _redacted(self.tool) if redact else self.tool
        else:
            result["recipe"] = _redacted(self.recipe) if redact else self.recipe
            if self.strategy:
                result["strategy"] = self.strategy
        result["parameters"] = (
            _redacted(self.parameters) if redact else _safe_copy(self.parameters)
        )
        if self.description:
            result["description"] = self.description
        if self.step_id:
            result["id"] = self.step_id
        return result

    def normalized(self) -> dict[str, Any]:
        """Return raw deterministic data for cache and closure digests."""
        return self.to_dict(redact=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecipeStep":
        """Build a step from a strict mapping representation."""
        if not isinstance(data, Mapping):
            raise RecipeSchemaError("recipe step must be a mapping")
        allowed = {
            "type",
            "kind",
            "step_type",
            "task",
            "tool",
            "recipe",
            "subrecipe",
            "name",
            "strategy",
            "parameters",
            "args",
            "action",
            "description",
            "id",
        }
        unknown = set(data) - allowed
        if unknown:
            raise RecipeSchemaError("recipe step contains unknown fields")
        if "parameters" not in data and "args" not in data:
            raise RecipeSchemaError(
                "recipe step requires an explicit parameters mapping"
            )
        first = data.get("type")
        second = data.get("kind")
        third = data.get("step_type")
        if first is not None and second is not None and first != second:
            raise RecipeSchemaError("recipe step type and kind disagree")
        if first is not None and third is not None and first != third:
            raise RecipeSchemaError("recipe step type fields disagree")
        kind = first if first is not None else (second if second is not None else third)
        if kind is None:
            raise RecipeSchemaError("recipe step requires a type")
        for field_name in (
            "kind",
            "type",
            "step_type",
            "task",
            "tool",
            "recipe",
            "subrecipe",
            "name",
            "strategy",
            "action",
            "id",
            "description",
        ):
            if (
                field_name in data
                and data[field_name] is not None
                and not isinstance(data[field_name], str)
            ):
                raise RecipeSchemaError("recipe step fields must be strings")
        return cls(
            kind,
            task=data.get("task", ""),
            tool=data.get("tool", ""),
            recipe=data.get("recipe", data.get("subrecipe", "")),
            strategy=data.get("strategy", ""),
            parameters=data.get("parameters", data.get("args", {})),
            name=data.get("name"),
            action=data.get("action"),
            step_id=data.get("id"),
            description=data.get("description", ""),
        )

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeStep({self.to_dict(redact=True)!r})"


class Recipe:
    """A portable, versioned recipe with ordered steps."""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(
        self,
        name: str,
        description: str = "",
        parameters: Optional[Mapping[str, Any] | Sequence[RecipeParameter]] = None,
        required_tools: Optional[Sequence[str]] = None,
        steps: Optional[Sequence[RecipeStep | Mapping[str, Any]]] = None,
        schema_version: int = SCHEMA_VERSION,
        *,
        version: Optional[int] = None,
        tools: Optional[Sequence[str]] = None,
        source: str = "",
    ) -> None:
        """Create a recipe from typed fields or parsed mappings."""
        if version is not None:
            if schema_version != SCHEMA_VERSION and schema_version != version:
                raise RecipeSchemaError("schema version fields disagree")
            schema_version = version
        if tools is not None:
            if required_tools is not None and tuple(required_tools) != tuple(tools):
                raise RecipeSchemaError("required_tools and tools disagree")
            required_tools = tools
        if isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
            raise RecipeSchemaError("unsupported recipe schema version")
        self.schema_version = SCHEMA_VERSION
        self.name = _validate_name(name, label="recipe name")
        self.version = self.schema_version
        if not isinstance(description, str):
            raise RecipeSchemaError("recipe description must be a string")
        self.description = description
        parsed_parameters: list[RecipeParameter] = []
        if isinstance(parameters, Mapping):
            for parameter_name, definition in parameters.items():
                if not isinstance(parameter_name, str):
                    raise RecipeSchemaError("parameter mapping keys must be strings")
                if isinstance(definition, RecipeParameter):
                    if definition.name != parameter_name:
                        raise RecipeSchemaError("parameter name does not match its key")
                    parsed_parameters.append(definition)
                else:
                    parsed_parameters.append(
                        RecipeParameter.from_dict(definition, name=str(parameter_name))
                    )
        elif parameters is None:
            parsed_parameters = []
        elif isinstance(parameters, (str, bytes)) or not isinstance(
            parameters, Sequence
        ):
            raise RecipeSchemaError("recipe parameters must be a mapping or sequence")
        else:
            for definition in parameters:
                if isinstance(definition, RecipeParameter):
                    parsed_parameters.append(definition)
                else:
                    parsed_parameters.append(RecipeParameter.from_dict(definition))
        names = [parameter.name for parameter in parsed_parameters]
        if len(names) != len(set(names)):
            raise RecipeSchemaError("recipe parameters must have unique names")
        self.parameters = tuple(parsed_parameters)
        if required_tools is None:
            tool_values: list[str] = []
        elif isinstance(required_tools, str):
            raise RecipeSchemaError("required_tools must be a sequence")
        else:
            tool_values = []
            for item in required_tools:
                if not isinstance(item, str):
                    raise RecipeSchemaError("required_tools must contain strings")
                tool_values.append(item)
        self.required_tools = tuple(_validate_tool_name(item) for item in tool_values)
        self.tools = self.required_tools
        if len(self.required_tools) != len(set(self.required_tools)):
            raise RecipeSchemaError("required_tools must be unique")
        if steps is None:
            step_values: list[RecipeStep] = []
        elif isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
            raise RecipeSchemaError("recipe steps must be a sequence")
        else:
            step_values = [
                item if isinstance(item, RecipeStep) else RecipeStep.from_dict(item)
                for item in steps
            ]
        if not step_values:
            raise RecipeSchemaError("recipe must contain at least one step")
        self.steps = tuple(step_values)
        self.source = str(source or "")
        self._validate_step_shapes()

    def _validate_step_shapes(self) -> None:
        for step in self.steps:
            if step.kind == "task":
                if not step.task.strip():
                    raise RecipeSchemaError("task step requires a task")
                if not step.strategy.strip():
                    raise RecipeSchemaError("task step requires an explicit strategy")
                _validate_template_syntax(step.task)
            elif step.kind == "tool":
                _validate_tool_name(step.tool)
                _validate_template_syntax(step.tool, allow_empty=False)
            else:
                _validate_reference(step.recipe)
                if step.strategy:
                    _validate_name(step.strategy, label="step strategy")
            _validate_value(step.parameters)

    def parameter_map(self) -> dict[str, RecipeParameter]:
        """Return parameters keyed by their declared names."""
        return {parameter.name: parameter for parameter in self.parameters}

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a strict recipe mapping suitable for serialization."""
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "parameters": {
                parameter.name: _parameter_definition(parameter, redact=redact)
                for parameter in self.parameters
            },
            "required_tools": list(self.required_tools),
            "steps": [step.to_dict(redact=redact) for step in self.steps],
        }
        return _redacted(result) if redact else result

    def normalized(self) -> dict[str, Any]:
        """Return a deterministic raw mapping for cache key construction."""
        result = self.to_dict(redact=False)
        result["required_tools"] = sorted(self.required_tools)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "") -> "Recipe":
        """Build a recipe from a validated-looking mapping."""
        if not isinstance(data, Mapping):
            raise RecipeSchemaError("recipe document must be a mapping")
        allowed = {
            "schema_version",
            "version",
            "name",
            "description",
            "parameters",
            "required_tools",
            "steps",
        }
        unknown = set(data) - allowed
        if unknown:
            raise RecipeSchemaError("recipe document contains unknown fields")
        required = {"name", "description", "parameters", "required_tools", "steps"}
        if "schema_version" not in data and "version" not in data:
            required.add("schema_version")
        missing = required - set(data)
        if missing:
            raise RecipeSchemaError("recipe document is missing required fields")
        version = data.get("schema_version", data.get("version", SCHEMA_VERSION))
        if "name" in data and not isinstance(data["name"], str):
            raise RecipeSchemaError("recipe name must be a string")
        if "description" in data and not isinstance(data["description"], str):
            raise RecipeSchemaError("recipe description must be a string")
        if "parameters" in data and not isinstance(data["parameters"], Mapping):
            raise RecipeSchemaError("recipe parameters must be a mapping")
        if "required_tools" in data and (
            isinstance(data["required_tools"], (str, bytes))
            or not isinstance(data["required_tools"], Sequence)
        ):
            raise RecipeSchemaError("recipe required_tools must be a sequence")
        if "steps" in data and (
            isinstance(data["steps"], (str, bytes))
            or not isinstance(data["steps"], Sequence)
        ):
            raise RecipeSchemaError("recipe steps must be a sequence")
        return cls(
            data.get("name", ""),
            description=data.get("description", ""),
            parameters=data.get("parameters", {}),
            required_tools=data.get("required_tools", []),
            steps=data.get("steps", []),
            schema_version=version,
            source=source,
        )

    @classmethod
    def from_yaml(cls, text: str, **kwargs: Any) -> "Recipe":
        """Load a recipe from supported YAML text."""
        from ._yaml import parse_recipe

        return parse_recipe(text, **kwargs)

    @classmethod
    def load(cls, source: Any, **kwargs: Any) -> "Recipe":
        """Load a recipe from a path, stream, or supported YAML string."""
        from ._yaml import load_recipe

        return load_recipe(source, **kwargs)

    def validate(
        self,
        *,
        tool_catalog: Any = None,
        validator: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
        raise_on_error: bool = True,
    ) -> "Recipe | ValidationReport":
        """Validate this recipe through the public validator."""
        from .validator import RecipeValidator

        active = validator or RecipeValidator(tool_catalog=tool_catalog)
        return active.validate(
            self,
            tool_catalog=tool_catalog,
            parameters=parameters,
            raise_on_error=raise_on_error,
        )

    def resolve_parameters(
        self,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        validator: Any = None,
    ) -> dict[str, Any]:
        """Resolve and interpolate this recipe's parameters."""
        from .validator import RecipeValidator

        active = validator or RecipeValidator()
        return active.resolve_parameters(self, parameters)

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"Recipe({self.to_dict(redact=True)!r})"


def _parameter_definition(
    parameter: RecipeParameter, *, redact: bool
) -> dict[str, Any]:
    """Return the compact mapping used inside a recipe document."""
    result: dict[str, Any] = {
        "type": parameter.type,
        "required": parameter.required,
    }
    if parameter.description:
        result["description"] = parameter.description
    if parameter.has_default:
        result["default"] = (
            _redacted(parameter.default) if redact else _safe_copy(parameter.default)
        )
    if parameter.choices is not None:
        result["choices"] = (
            _redacted(list(parameter.choices)) if redact else list(parameter.choices)
        )
    if parameter.minimum is not None:
        result["min"] = parameter.minimum
    if parameter.maximum is not None:
        result["max"] = parameter.maximum
    return result


def _reject_secret_default(name: str, value: Any) -> None:
    """Reject secret-shaped defaults without including their value in errors."""
    if is_sensitive_key(name) or contains_secret(value):
        raise RecipeSecurityError("secret-shaped parameter defaults are not allowed")


def _validate_value(value: Any, *, depth: int = 0, max_depth: int = 64) -> None:
    """Validate that a value is composed only of supported JSON-like data."""
    if depth > max_depth:
        raise RecipeSchemaError("recipe value nesting is too deep")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecipeSchemaError("recipe mapping keys must be strings")
            _validate_template_syntax(key)
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise RecipeSchemaError("non-finite numbers are not supported")
        if isinstance(value, str):
            _validate_template_syntax(value)
        return
    raise RecipeSchemaError("recipe values must be JSON-like YAML scalars")


def _validate_template_syntax(value: str, *, allow_empty: bool = True) -> None:
    """Reject malformed or ambiguous ``${...}`` template expressions."""
    if not isinstance(value, str):
        raise RecipeTemplateError("template expression must be a string")
    index = 0
    while index < len(value):
        if value[index] != "$":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] != "{":
            index += 1
            continue
        if index > 0 and value[index - 1] == "$":
            raise RecipeTemplateError("ambiguous template expression")
        end = value.find("}", index + 2)
        if end < 0:
            raise RecipeTemplateError("malformed template expression")
        body = value[index + 2 : end]
        if not body or "{" in body or "$" in body:
            raise RecipeTemplateError("malformed template expression")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", body):
            raise RecipeTemplateError("template variable name is not unambiguous")
        index = end + 1


def _require_sequence(value: Any, *, label: str) -> list[Any]:
    """Return a list copy while rejecting strings as sequences."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RecipeSchemaError(f"{label} must be a sequence")
    return list(value)


@dataclass
class ValidationIssue:
    """One safe, machine-readable validation issue."""

    code: str
    message: str
    path: str = ""

    def __post_init__(self) -> None:
        """Normalize issue text through the shared redactor."""
        self.message = redact_text(self.message)
        self.path = redact_text(self.path)

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-compatible issue mapping."""
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass
class ValidationReport:
    """A non-raising validation result with optional typed recipe data."""

    issues: list[ValidationIssue] = field(default_factory=list)
    recipe: Optional[Recipe] = None
    missing_tools: tuple[str, ...] = ()
    extra_tools: tuple[str, ...] = ()
    unused_required_tools: tuple[str, ...] = ()
    catalog_extra_tools: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Return whether no validation issues were found."""
        return not self.issues and self.recipe is not None

    @property
    def errors(self) -> list[ValidationIssue]:
        """Return validation issues under a conventional alias."""
        return self.issues

    def __bool__(self) -> bool:
        """Return the valid state."""
        return self.valid

    def __iter__(self) -> Iterator[ValidationIssue]:
        """Iterate over issues for callers that treat reports as collections."""
        return iter(self.issues)

    def raise_for_errors(self) -> "ValidationReport":
        """Raise a typed validation error when the report is invalid."""
        if not self.valid:
            first = (
                self.issues[0]
                if self.issues
                else ValidationIssue("invalid", "recipe is invalid")
            )
            error_types = {
                "recipe_parameter_error": RecipeParameterError,
                "recipe_template_error": RecipeTemplateError,
                "recipe_tool_error": RecipeToolError,
                "recipe_subrecipe_error": RecipeSubrecipeError,
                "recipe_cycle_error": RecipeCycleError,
                "recipe_depth_error": RecipeDepthError,
                "recipe_schema_error": RecipeSchemaError,
                "recipe_security_error": RecipeSecurityError,
                "missing_tool": RecipeToolError,
                "extra_tool": RecipeToolError,
                "undeclared_tool": RecipeToolError,
                "invalid_choice": RecipeParameterError,
                "below_minimum": RecipeParameterError,
                "above_maximum": RecipeParameterError,
                "invalid_bound": RecipeParameterError,
                "max_steps": RecipeLimitError,
            }
            error_type = error_types.get(first.code, RecipeValidationError)
            raise error_type(
                first.message,
                details={"issues": [item.as_dict() for item in self.issues]},
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted validation report."""
        return {
            "valid": self.valid,
            "issues": [item.as_dict() for item in self.issues],
            "missing_tools": list(self.missing_tools),
            "extra_tools": list(self.extra_tools),
            "unused_required_tools": list(self.unused_required_tools),
            "catalog_extra_tools": list(self.catalog_extra_tools),
        }


@dataclass
class ResolvedStep:
    """A fully interpolated step with deterministic expansion provenance."""

    step: RecipeStep
    recipe_name: str
    depth: int
    position: int
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Return the underlying step kind."""
        return self.step.kind

    @property
    def type(self) -> str:
        """Return the serialized step kind."""
        return self.step.kind

    @property
    def name(self) -> str:
        """Return the task, tool, or subrecipe name."""
        return self.step.name

    @property
    def task(self) -> str:
        """Return the interpolated task text when this is a task step."""
        return self.step.task

    @property
    def tool(self) -> str:
        """Return the tool name when this is a tool step."""
        return self.step.tool

    @property
    def recipe(self) -> str:
        """Return the subrecipe reference when this is a subrecipe step."""
        return self.step.recipe

    @property
    def strategy(self) -> str:
        """Return the explicit task or subrecipe strategy."""
        return self.step.strategy

    def __getitem__(self, key: int | str) -> Any:
        """Index a resolved step or access a named serialized field."""
        if isinstance(key, str):
            return self.to_dict(redact=True)[key]
        return self.parameters[key]

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a redacted execution-plan step mapping."""
        value = {
            "kind": self.step.kind,
            "name": _redacted(self.name) if redact else self.name,
            "strategy": self.step.strategy,
            "parameters": _redacted(self.parameters)
            if redact
            else _safe_copy(self.parameters),
            "recipe": self.recipe_name,
            "depth": self.depth,
            "position": self.position,
        }
        if self.step.task:
            value["task"] = _redacted(self.step.task) if redact else self.step.task
        if self.step.tool:
            value["tool"] = _redacted(self.step.tool) if redact else self.step.tool
        return value

    def __eq__(self, other: object) -> bool:
        """Compare resolved steps with mappings using the public shape."""
        if isinstance(other, Mapping):
            return self.to_dict(redact=True) == other
        return super().__eq__(other)

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"ResolvedStep({self.to_dict(redact=True)!r})"


@dataclass
class ExecutionPlan:
    """An ordered, side-effect-free plan produced by dry-run."""

    recipe_name: str
    parameters: dict[str, Any]
    steps: list[ResolvedStep]
    closure: dict[str, Recipe] = field(default_factory=dict)
    tool_schema_digest: str = ""
    recipe: Optional[Recipe] = None

    def __post_init__(self) -> None:
        """Ensure callers cannot mutate the public parameter map accidentally."""
        self.parameters = dict(self.parameters)
        self.steps = list(self.steps)
        self.closure = dict(self.closure)

    @property
    def dry_run(self) -> bool:
        """Return that this plan is side-effect-free."""
        return True

    def __iter__(self) -> Iterator[ResolvedStep]:
        """Iterate over ordered plan steps."""
        return iter(self.steps)

    def __len__(self) -> int:
        """Return the number of expanded steps."""
        return len(self.steps)

    def __getitem__(self, key: int | str) -> Any:
        """Index a step by position or access a named plan field."""
        if isinstance(key, str):
            if key == "steps":
                return self.steps
            if key == "parameters":
                return self.parameters
            if key == "closure":
                return self.closure
            if key == "recipe_name":
                return self.recipe_name
            if key == "recipe":
                return self.recipe
            if key == "tool_schema_digest":
                return self.tool_schema_digest
            if key == "dry_run":
                return True
            raise KeyError(key)
        return self.steps[key]

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a JSON-compatible redacted plan mapping."""
        return {
            "recipe": self.recipe_name,
            "parameters": _redacted(self.parameters)
            if redact
            else _safe_copy(self.parameters),
            "steps": [step.to_dict(redact=redact) for step in self.steps],
            "closure": sorted(self.closure),
            "tool_schema_digest": self.tool_schema_digest,
            "dry_run": True,
        }

    def cache_key(self, tool_catalog: Any = None) -> str:
        """Return the content key for this plan and its resolved closure."""
        from .cache import RecipeCache

        recipe = self.recipe or self.closure.get(self.recipe_name)
        if recipe is None:
            recipe = (
                Recipe(self.recipe_name, steps=[self.steps[0].step])
                if self.steps
                else None
            )
        if recipe is None:
            raise RecipeValidationError("execution plan has no recipe closure")
        return RecipeCache.key_for(
            recipe,
            self.closure,
            self.tool_schema_digest,
            tool_catalog=tool_catalog,
            parameters=self.parameters,
        )

    to_dict = as_dict

    def __eq__(self, other: object) -> bool:
        """Compare plans by expanded steps for list-like callers."""
        if isinstance(other, (list, tuple)):
            return self.steps == list(other)
        if isinstance(other, ExecutionPlan):
            return self.recipe_name == other.recipe_name and self.steps == other.steps
        return NotImplemented

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"ExecutionPlan({self.as_dict(redact=True)!r})"


@dataclass
class RecipeStepResult:
    """The redacted outcome of one executed recipe step."""

    index: int
    kind: str
    name: str
    status: str
    output: Any = None
    error: Optional[str] = None
    depth: int = 0

    def __getitem__(self, key: int | str) -> Any:
        """Index a result or access a named serialized field."""
        if isinstance(key, str):
            return self.as_dict()[key]
        return self.output[key]

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-compatible result mapping."""
        return {
            "index": self.index,
            "kind": self.kind,
            "name": _redacted(self.name),
            "status": self.status,
            "output": _redacted(self.output),
            "error": redact_text(self.error or ""),
            "depth": self.depth,
        }

    to_dict = as_dict

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeStepResult({self.as_dict()!r})"


@dataclass
class RecipeRunResult:
    """A sequence-like aggregate of per-step execution results."""

    status: str
    steps: list[RecipeStepResult]
    failure_index: Optional[int] = None

    @property
    def success(self) -> bool:
        """Return whether every planned step succeeded."""
        return self.status == "success" and self.failure_index is None

    @property
    def results(self) -> list[RecipeStepResult]:
        """Return per-step results under a conventional alias."""
        return self.steps

    @property
    def completed(self) -> bool:
        """Return whether the aggregate execution completed successfully."""
        return self.success

    @property
    def ok(self) -> bool:
        """Return the success state under a concise alias."""
        return self.success

    @property
    def error(self) -> Optional[str]:
        """Return the first redacted step error, if execution stopped."""
        for step in self.steps:
            if step.error:
                return step.error
        return None

    def __iter__(self) -> Iterator[RecipeStepResult]:
        """Iterate over per-step results."""
        return iter(self.steps)

    def __len__(self) -> int:
        """Return the number of executed steps."""
        return len(self.steps)

    def __getitem__(self, key: int | str) -> Any:
        """Index results or access named aggregate fields."""
        if isinstance(key, str):
            if key == "steps":
                return self.steps
            if key == "results":
                return self.steps
            if key == "status":
                return self.status
            if key == "success":
                return self.success
            if key == "failure_index":
                return self.failure_index
            if key == "error":
                return self.error
            if key == "completed":
                return self.completed
            if key == "ok":
                return self.ok
            raise KeyError(key)
        return self.steps[key]

    def as_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-compatible aggregate result."""
        return {
            "status": self.status,
            "success": self.success,
            "failure_index": self.failure_index,
            "error": self.error or "",
            "steps": [item.as_dict() for item in self.steps],
        }

    to_dict = as_dict

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeRunResult({self.as_dict()!r})"


class ToolCatalog:
    """Small adapter for common duck-typed tool catalog shapes."""

    def __init__(self, tools: Any = None) -> None:
        """Wrap a mapping, iterable, or existing catalog object."""
        self._source = tools if tools is not None else {}

    def register(self, name: str, schema: Any = None) -> None:
        """Register a tool schema in a mutable catalog adapter."""
        if not isinstance(self._source, dict):
            self._source = {}
        self._source[_validate_tool_name(name)] = schema

    add = register

    def _mapping(self) -> dict[str, Any]:
        source = self._source
        if isinstance(source, ToolCatalog):
            return source._mapping()
        if isinstance(source, Mapping):
            return {str(key): value for key, value in source.items()}
        if isinstance(source, (Sequence, set, frozenset)) and not isinstance(
            source,
            (str, bytes),
        ):
            return {str(item): {} for item in source}
        for attr in ("tools", "available_tools", "catalog", "entries"):
            candidate = getattr(source, attr, None)
            if isinstance(candidate, Mapping):
                return {str(key): value for key, value in candidate.items()}
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes)
            ):
                result: dict[str, Any] = {}
                for item in candidate:
                    if isinstance(item, str):
                        result[item] = {}
                    elif isinstance(item, Mapping):
                        name = item.get("name")
                        if name:
                            result[str(name)] = item
                if result:
                    return result
        for method_name in ("names", "list_tools", "tool_names", "get_tool_names"):
            method = getattr(source, method_name, None)
            if callable(method):
                try:
                    values = method()
                except Exception:
                    continue
                if isinstance(values, Mapping):
                    return {str(key): value for key, value in values.items()}
                if isinstance(values, (list, tuple, set, frozenset)):
                    return {str(item): {} for item in values}
        return {}

    def names(self) -> tuple[str, ...]:
        """Return available tool names in deterministic sorted order."""
        return tuple(sorted(self._mapping()))

    def has(self, name: str) -> bool:
        """Return whether a tool name is available."""
        if name in self._mapping():
            return True
        for method_name in ("has_tool", "contains", "__contains__"):
            method = getattr(self._source, method_name, None)
            if callable(method):
                try:
                    return bool(method(name))
                except Exception:
                    return False
        return False

    def get(self, name: str) -> Any:
        """Return a tool schema or descriptor when available."""
        mapping = self._mapping()
        if name in mapping:
            return mapping[name]
        for method_name in ("get_tool", "get", "schema_for", "get_schema"):
            method = getattr(self._source, method_name, None)
            if callable(method):
                try:
                    return method(name)
                except (KeyError, TypeError, AttributeError):
                    return None
        return None

    def schema(self, name: str) -> Any:
        """Return a tool schema through a public schema method when present."""
        for method_name in ("schema", "get_schema", "schema_for"):
            method = getattr(self._source, method_name, None)
            if callable(method):
                try:
                    return method(name)
                except Exception:
                    continue
        return self.get(name)

    def digest(self) -> str:
        """Return a stable digest of the available tool schemas."""
        import hashlib

        payload = {}
        for name in self.names():
            schema = self.schema(name)
            if schema is None:
                schema = self.get(name)
            try:
                payload[name] = _jsonable(redact_secrets(schema))
            except Exception:
                payload[name] = repr(redact_secrets(schema))
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

    schema_digest = digest

    def __contains__(self, name: object) -> bool:
        """Support ``name in catalog`` checks."""
        return isinstance(name, str) and self.has(name)

    def __len__(self) -> int:
        """Return the number of enumerable tools."""
        return len(self.names())

    def __iter__(self) -> Iterator[str]:
        """Iterate over available names."""
        return iter(self.names())


__all__ = [
    "MISSING",
    "PARAMETER_TYPES",
    "SCHEMA_VERSION",
    "STEP_KINDS",
    "CacheError",
    "ExecutionError",
    "ExecutionPlan",
    "ParseError",
    "Recipe",
    "RecipeCacheCorruptionError",
    "RecipeCacheError",
    "RecipeCycleError",
    "RecipeDepthError",
    "RecipeError",
    "RecipeExecutionError",
    "RecipeLimitError",
    "RecipeParameter",
    "RecipeParameterError",
    "RecipeParameterValidationError",
    "RecipeParseError",
    "RecipeRunResult",
    "RecipeSchemaError",
    "RecipeSchemaValidationError",
    "RecipeSecurityError",
    "RecipeStep",
    "RecipeStepResult",
    "RecipeSubrecipeError",
    "RecipeSubrecipeValidationError",
    "RecipeTemplateError",
    "RecipeToolError",
    "RecipeToolValidationError",
    "RecipeTraversalError",
    "RecipeValidationError",
    "RecipeValidationFailure",
    "RecipeYAMLError",
    "ResolvedStep",
    "ToolCatalog",
    "ValidationError",
    "ValidationIssue",
    "ValidationReport",
]
