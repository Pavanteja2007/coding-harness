"""Schema, parameter, tool, and subrecipe validation for recipes."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .models import (
    MISSING,
    PARAMETER_TYPES,
    Recipe,
    RecipeError,
    RecipeParameter,
    RecipeParameterError,
    RecipeSchemaError,
    RecipeStep,
    RecipeTemplateError,
    RecipeToolError,
    RecipeValidationError,
    ToolCatalog,
    ValidationIssue,
    ValidationReport,
    _canonical,
    _jsonable,
    _validate_template_syntax,
    _validate_tool_name,
)

_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})
RecipeValidationFailure = RecipeValidationError


class RecipeValidator:
    """Validate recipe documents and resolve typed parameter values."""

    def __init__(
        self,
        tool_catalog: Any = None,
        *,
        catalog: Any = None,
        max_depth: int = 32,
        max_steps: int = 4096,
        max_nesting: int = 64,
        max_count: Optional[int] = None,
        max_recipe_depth: Optional[int] = None,
    ) -> None:
        """Create a validator with optional duck-typed tool access."""
        if catalog is not None:
            if tool_catalog is not None:
                raise RecipeValidationError("tool_catalog and catalog disagree")
            tool_catalog = catalog
        if max_count is not None:
            max_steps = max_count
        if max_recipe_depth is not None:
            max_depth = max_recipe_depth
        if max_depth <= 0 or max_steps <= 0 or max_nesting <= 0:
            raise RecipeValidationError("recipe validator limits must be positive")
        self.tool_catalog = tool_catalog
        self.max_depth = max_depth
        self.max_steps = max_steps
        self.max_nesting = max_nesting
        self.max_count = max_steps
        self.max_recipe_depth = max_depth

    def validate(
        self,
        value: Any,
        *,
        tool_catalog: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
        raise_on_error: bool = True,
    ) -> Recipe | ValidationReport:
        """Validate a recipe or strict mapping and return the typed recipe."""
        catalog = self.tool_catalog if tool_catalog is None else tool_catalog
        issues: list[ValidationIssue] = []
        recipe: Optional[Recipe] = None
        missing_tools: tuple[str, ...] = ()
        extra_tools: tuple[str, ...] = ()
        unused_required_tools: tuple[str, ...] = ()
        catalog_extra_tools: tuple[str, ...] = ()
        try:
            recipe = self._coerce_recipe(value)
            self._validate_recipe_shape(recipe, issues)
            self._validate_templates(recipe, issues)
            (
                missing_tools,
                extra_tools,
                unused_required_tools,
                catalog_extra_tools,
            ) = self._validate_tools(
                recipe,
                catalog,
                issues,
            )
            if parameters is not None:
                self.resolve_parameters(recipe, parameters)
        except RecipeError as exc:
            issues.append(ValidationIssue(exc.code, str(exc)))
            if raise_on_error:
                raise
        report = ValidationReport(
            issues=issues,
            recipe=recipe,
            missing_tools=missing_tools,
            extra_tools=extra_tools,
            unused_required_tools=unused_required_tools,
            catalog_extra_tools=catalog_extra_tools,
        )
        if not report.valid and raise_on_error:
            report.raise_for_errors()
        return recipe if report.valid else report

    def check(
        self,
        value: Any,
        *,
        tool_catalog: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> ValidationReport:
        """Return a non-raising validation report."""
        result = self.validate(
            value,
            tool_catalog=tool_catalog,
            parameters=parameters,
            raise_on_error=False,
        )
        if isinstance(result, ValidationReport):
            return result
        issues: list[ValidationIssue] = []
        catalog = self.tool_catalog if tool_catalog is None else tool_catalog
        missing, extra, unused, catalog_extra = self._validate_tools(
            result,
            catalog,
            issues,
        )
        return ValidationReport(
            issues=issues,
            recipe=result,
            missing_tools=missing,
            extra_tools=extra,
            unused_required_tools=unused,
            catalog_extra_tools=catalog_extra,
        )

    def validate_or_raise(
        self,
        value: Any,
        *,
        tool_catalog: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> Recipe:
        """Validate a recipe and return it or raise its typed failure."""
        result = self.validate(
            value,
            tool_catalog=tool_catalog,
            parameters=parameters,
            raise_on_error=True,
        )
        if isinstance(result, ValidationReport):
            result.raise_for_errors()
            raise RecipeValidationError("recipe validation failed")
        return result

    def is_valid(
        self,
        value: Any,
        *,
        tool_catalog: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Return whether a recipe and optional parameters validate."""
        return self.check(
            value,
            tool_catalog=tool_catalog,
            parameters=parameters,
        ).valid

    def validate_recipe(
        self,
        value: Any,
        *,
        tool_catalog: Any = None,
        parameters: Optional[Mapping[str, Any]] = None,
        raise_on_error: bool = True,
    ) -> Recipe | ValidationReport:
        """Alias for :meth:`validate` with an explicit recipe-oriented name."""
        return self.validate(
            value,
            tool_catalog=tool_catalog,
            parameters=parameters,
            raise_on_error=raise_on_error,
        )

    def resolve_parameters(
        self,
        recipe: Recipe | Mapping[str, Any],
        supplied: Optional[Mapping[str, Any]] = None,
        *,
        include_optional: bool = False,
    ) -> dict[str, Any]:
        """Resolve defaults, coerce supplied values, and interpolate all templates."""
        actual_recipe = self._coerce_recipe(recipe)
        return self._resolve_parameters(
            actual_recipe, supplied or {}, include_optional=include_optional
        )

    def validate_parameters(
        self,
        recipe: Recipe | Mapping[str, Any],
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        include_optional: bool = False,
    ) -> dict[str, Any]:
        """Validate and coerce a parameter mapping for a recipe."""
        if isinstance(recipe, Mapping) and isinstance(parameters, Recipe):
            recipe, parameters = parameters, recipe
        return self.resolve_parameters(
            recipe,
            parameters,
            include_optional=include_optional,
        )

    def interpolate(
        self,
        value: Any,
        parameters: Mapping[str, Any],
        *,
        known_names: Optional[Sequence[str]] = None,
    ) -> Any:
        """Interpolate a JSON-like value with strict variable-name checks."""
        names = set(known_names if known_names is not None else parameters.keys())
        return self._interpolate(value, dict(parameters), names, "value")

    def tool_schema_digest(self, tool_catalog: Any = None) -> str:
        """Return a stable digest for an injected tool catalog."""
        catalog = self.tool_catalog if tool_catalog is None else tool_catalog
        if catalog is None:
            return ""
        adapter = catalog if isinstance(catalog, ToolCatalog) else ToolCatalog(catalog)
        for method_name in ("schema_digest", "digest"):
            method = getattr(catalog, method_name, None)
            if isinstance(method, str):
                return method
            if callable(method):
                try:
                    return str(method())
                except Exception as exc:
                    raise RecipeToolError(
                        "tool schema digest could not be computed"
                    ) from exc
        return adapter.digest()

    def validate_tools(
        self, recipe: Recipe, tool_catalog: Any = None
    ) -> ValidationReport:
        """Validate a recipe's required and referenced tools without other checks."""
        issues: list[ValidationIssue] = []
        catalog = self.tool_catalog if tool_catalog is None else tool_catalog
        missing, extra, unused, catalog_extra = self._validate_tools(
            recipe, catalog, issues
        )
        return ValidationReport(
            issues=issues,
            recipe=recipe if not issues else None,
            missing_tools=missing,
            extra_tools=extra,
            unused_required_tools=unused,
            catalog_extra_tools=catalog_extra,
        )

    def _coerce_recipe(self, value: Any) -> Recipe:
        if isinstance(value, Recipe):
            return value
        if isinstance(value, Mapping):
            return Recipe.from_dict(value)
        if isinstance(value, (str, bytes)):
            from ._yaml import load_recipe

            return load_recipe(value, validate=False)
        raise RecipeSchemaError("recipe must be a Recipe, mapping, or YAML string")

    def _validate_recipe_shape(
        self, recipe: Recipe, issues: list[ValidationIssue]
    ) -> None:
        if len(recipe.steps) > self.max_steps:
            issues.append(
                ValidationIssue("max_steps", "recipe has too many steps", "steps")
            )
        for parameter in recipe.parameters:
            if parameter.type not in PARAMETER_TYPES:
                issues.append(
                    ValidationIssue(
                        "parameter_type",
                        "parameter has an unsupported type",
                        parameter.name,
                    )
                )
            if (
                parameter.minimum is not None or parameter.maximum is not None
            ) and parameter.type not in {
                "integer",
                "number",
            }:
                issues.append(
                    ValidationIssue(
                        "invalid_bound",
                        "minimum and maximum require a numeric parameter",
                        parameter.name,
                    )
                )
            if parameter.choices is not None:
                for choice in parameter.choices:
                    try:
                        self._coerce_value(
                            choice, parameter, strict=True, path=parameter.name
                        )
                    except RecipeError as exc:
                        issues.append(
                            ValidationIssue("invalid_choice", str(exc), parameter.name)
                        )
            if parameter.has_default:
                try:
                    self._coerce_value(
                        parameter.default,
                        parameter,
                        strict=True,
                        path=parameter.name,
                    )
                except RecipeError as exc:
                    issues.append(ValidationIssue(exc.code, str(exc), parameter.name))
                self._check_choices_and_bounds(
                    parameter.default, parameter, issues, parameter.name
                )
        for index, step in enumerate(recipe.steps):
            path = f"steps[{index}]"
            try:
                self._validate_step(step, path)
            except RecipeError as exc:
                issues.append(ValidationIssue(exc.code, str(exc), path))

    def _validate_step(self, step: RecipeStep, path: str) -> None:
        if step.kind == "task":
            if not step.task.strip():
                raise RecipeSchemaError("task step requires a task")
            if not step.strategy.strip() or not _TEMPLATE_NAME_RE.fullmatch(
                step.strategy
            ):
                raise RecipeSchemaError("task step requires an explicit safe strategy")
            _validate_template_syntax(step.task)
        elif step.kind == "tool":
            _validate_tool_name(step.tool)
            if step.strategy and not _TEMPLATE_NAME_RE.fullmatch(step.strategy):
                raise RecipeSchemaError("tool strategy is not a safe identifier")
        else:
            from .models import _validate_reference

            _validate_reference(step.recipe)
            if "${" in step.recipe:
                raise RecipeTemplateError("subrecipe references cannot be templated")
            if step.strategy and not _TEMPLATE_NAME_RE.fullmatch(step.strategy):
                raise RecipeSchemaError("subrecipe strategy is not a safe identifier")
        self._validate_value(step.parameters, path=path, depth=0)

    def _validate_value(self, value: Any, *, path: str, depth: int) -> None:
        if depth > self.max_nesting:
            raise RecipeSchemaError("recipe value nesting exceeds the configured limit")
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise RecipeSchemaError("recipe mapping keys must be strings")
                _validate_template_syntax(key)
                self._validate_value(item, path=f"{path}.{key}", depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self._validate_value(item, path=f"{path}[{index}]", depth=depth + 1)
            return
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise RecipeSchemaError("non-finite numbers are not supported")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise RecipeSchemaError("recipe values must be JSON-like")

    def _validate_templates(
        self, recipe: Recipe, issues: list[ValidationIssue]
    ) -> None:
        names = set(recipe.parameter_map())
        try:
            self._check_template_names(recipe.description, names, "description")
        except RecipeError as exc:
            issues.append(ValidationIssue(exc.code, str(exc), "description"))
        for parameter in recipe.parameters:
            try:
                self._check_template_names(parameter.description, names, parameter.name)
            except RecipeError as exc:
                issues.append(ValidationIssue(exc.code, str(exc), parameter.name))
            if parameter.has_default:
                try:
                    self._check_template_names(parameter.default, names, parameter.name)
                except RecipeError as exc:
                    issues.append(ValidationIssue(exc.code, str(exc), parameter.name))
        for index, step in enumerate(recipe.steps):
            path = f"steps[{index}]"
            if step.kind == "task":
                try:
                    self._check_template_names(step.task, names, path)
                except RecipeError as exc:
                    issues.append(ValidationIssue(exc.code, str(exc), path))
            if step.kind == "subrecipe":
                try:
                    self._check_template_names(step.recipe, names, path)
                except RecipeError as exc:
                    issues.append(ValidationIssue(exc.code, str(exc), path))
            try:
                self._check_template_names(step.parameters, names, path)
            except RecipeError as exc:
                issues.append(ValidationIssue(exc.code, str(exc), path))

    def _check_template_names(self, value: Any, names: set[str], path: str) -> None:
        if isinstance(value, str):
            _validate_template_syntax(value)
            for name in _template_names(value):
                if name not in names:
                    raise RecipeTemplateError(
                        "template references an unknown parameter"
                    )
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                self._check_template_names(key, names, path)
                self._check_template_names(item, names, path)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self._check_template_names(item, names, path)

    def _validate_tools(
        self,
        recipe: Recipe,
        catalog: Any,
        issues: list[ValidationIssue],
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        required = set(recipe.required_tools)
        referenced = {step.tool for step in recipe.steps if step.kind == "tool"}
        undeclared = tuple(sorted(referenced - required))
        for tool in undeclared:
            issues.append(
                ValidationIssue(
                    "undeclared_tool",
                    "extra tool step is not declared in required_tools",
                    tool,
                )
            )
        if catalog is not None:
            for index, step in enumerate(recipe.steps):
                if step.kind != "tool":
                    continue
                issue = _validate_tool_step_schema(catalog, step)
                if issue is not None:
                    issues.append(ValidationIssue(*issue, f"steps[{index}].tool"))
        unused = tuple(sorted(required - referenced))
        if catalog is None:
            return (), undeclared, unused, ()
        available = set(_catalog_names(catalog))
        if not available and _catalog_supports_probe(catalog):
            missing = tuple(
                sorted(name for name in required if not _catalog_has(catalog, name))
            )
        else:
            missing = tuple(sorted(required - available))
        for tool in missing:
            issues.append(
                ValidationIssue("missing_tool", "required tool is not available", tool)
            )
        catalog_extra = tuple(sorted(available - required))
        return missing, undeclared, unused, catalog_extra

    def _resolve_parameters(
        self,
        recipe: Recipe,
        supplied: Mapping[str, Any],
        *,
        include_optional: bool,
        stack: Optional[set[str]] = None,
        resolved: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not isinstance(supplied, Mapping):
            raise RecipeParameterError("recipe parameters must be a mapping")
        active_stack = set() if stack is None else set(stack)
        active_resolved = {} if resolved is None else resolved
        definitions = recipe.parameter_map()
        names = set(definitions)
        unknown = set(supplied) - names
        if unknown:
            raise RecipeParameterError("recipe received an unknown parameter")

        def resolve_one(name: str) -> None:
            if name in active_resolved:
                return
            if name in active_stack:
                raise RecipeTemplateError("parameter defaults contain a template cycle")
            parameter = definitions[name]
            active_stack.add(name)
            raw = supplied.get(name, MISSING)
            if raw is MISSING:
                if parameter.has_default:
                    raw = parameter.default
                elif parameter.required:
                    active_stack.remove(name)
                    raise RecipeParameterError("required recipe parameter is missing")
                else:
                    active_stack.remove(name)
                    if include_optional:
                        active_resolved[name] = None
                    return
            for referenced in _collect_template_names(raw):
                self._require_template_name(referenced, names)
                if referenced != name and referenced not in active_resolved:
                    resolve_one(referenced)
            try:
                interpolated = self._interpolate(raw, active_resolved, names, name)
                value = self._coerce_value(
                    interpolated,
                    parameter,
                    strict=False,
                    path=name,
                )
                self._check_choices_and_bounds(value, parameter, [], name)
            except RecipeError:
                active_stack.remove(name)
                raise
            except (TypeError, ValueError, OverflowError) as exc:
                active_stack.remove(name)
                raise RecipeParameterError(
                    "recipe parameter could not be resolved"
                ) from exc
            active_resolved[name] = value
            active_stack.remove(name)

        for parameter_name in definitions:
            resolve_one(parameter_name)
        return dict(active_resolved)

    def _interpolate(
        self,
        value: Any,
        parameters: Mapping[str, Any],
        names: set[str],
        path: str,
    ) -> Any:
        if isinstance(value, str):
            _validate_template_syntax(value)
            exact = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}", value)
            if exact:
                name = exact.group(1)
                self._require_template_name(name, names)
                if name not in parameters:
                    raise RecipeTemplateError("template references a missing parameter")
                return parameters[name]
            return self._interpolate_text(value, parameters, names)
        if isinstance(value, Mapping):
            return {
                str(self._interpolate(key, parameters, names, path)): self._interpolate(
                    item,
                    parameters,
                    names,
                    path,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._interpolate(item, parameters, names, path) for item in value]
        if isinstance(value, tuple):
            return tuple(
                self._interpolate(item, parameters, names, path) for item in value
            )
        return value

    def _interpolate_text(
        self,
        value: str,
        parameters: Mapping[str, Any],
        names: set[str],
    ) -> str:
        pieces: list[str] = []
        index = 0
        while index < len(value):
            start = value.find("${", index)
            if start < 0:
                pieces.append(value[index:])
                break
            pieces.append(value[index:start])
            end = value.find("}", start + 2)
            if end < 0:
                raise RecipeTemplateError("malformed template expression")
            name = value[start + 2 : end]
            self._require_template_name(name, names)
            if name not in parameters:
                raise RecipeTemplateError("template references a missing parameter")
            pieces.append(_stringify(parameters[name]))
            index = end + 1
        return "".join(pieces)

    def _require_template_name(self, name: str, names: set[str]) -> None:
        if not _TEMPLATE_NAME_RE.fullmatch(name):
            raise RecipeTemplateError("template variable name is not unambiguous")
        if name not in names:
            raise RecipeTemplateError("template references an unknown parameter")

    def _coerce_value(
        self,
        value: Any,
        parameter: RecipeParameter,
        *,
        strict: bool,
        path: str,
    ) -> Any:
        kind = parameter.type
        if kind == "string":
            if isinstance(value, str):
                return value
            if strict:
                raise RecipeParameterError("default has the wrong parameter type")
            if isinstance(value, (int, float, bool)) or value is None:
                if value is None:
                    raise RecipeParameterError("null is not a valid string parameter")
                return _stringify(value)
            if isinstance(value, (list, dict)):
                if strict:
                    raise RecipeParameterError("default has the wrong parameter type")
                return json.dumps(
                    _jsonable(value), sort_keys=True, separators=(",", ":")
                )
            raise RecipeParameterError("value has the wrong parameter type")
        if kind == "integer":
            if isinstance(value, bool):
                raise RecipeParameterError("boolean is not an integer parameter")
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                if strict:
                    raise RecipeParameterError("default has the wrong parameter type")
                if value.is_integer():
                    return int(value)
                raise RecipeParameterError("number is not an integer parameter")
            if (
                isinstance(value, str)
                and not strict
                and re.fullmatch(r"[+-]?[0-9]+", value.strip())
            ):
                return int(value.strip())
            raise RecipeParameterError("value has the wrong parameter type")
        if kind == "number":
            if isinstance(value, bool):
                raise RecipeParameterError("boolean is not a number parameter")
            if isinstance(value, (int, float)):
                result = float(value)
                if not math.isfinite(result):
                    raise RecipeParameterError("number parameter must be finite")
                return result
            if isinstance(value, str) and not strict:
                try:
                    result = float(value.strip())
                except ValueError as exc:
                    raise RecipeParameterError(
                        "value has the wrong parameter type"
                    ) from exc
                if not math.isfinite(result):
                    raise RecipeParameterError("number parameter must be finite")
                return result
            raise RecipeParameterError("value has the wrong parameter type")
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and not strict:
                folded = value.strip().casefold()
                if folded in _TRUE_STRINGS:
                    return True
                if folded in _FALSE_STRINGS:
                    return False
            raise RecipeParameterError("value has the wrong parameter type")
        if kind == "list":
            if isinstance(value, list):
                return list(value)
            if isinstance(value, tuple):
                return list(value)
            if isinstance(value, str) and not strict:
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RecipeParameterError(
                        "value has the wrong parameter type"
                    ) from exc
                if isinstance(parsed, list):
                    return parsed
            raise RecipeParameterError("value has the wrong parameter type")
        if kind == "object":
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, str) and not strict:
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RecipeParameterError(
                        "value has the wrong parameter type"
                    ) from exc
                if isinstance(parsed, Mapping):
                    return dict(parsed)
            raise RecipeParameterError("value has the wrong parameter type")
        raise RecipeParameterError("unsupported parameter type")

    def _check_choices_and_bounds(
        self,
        value: Any,
        parameter: RecipeParameter,
        issues: list[ValidationIssue],
        path: str,
    ) -> None:
        errors: list[ValidationIssue] = []
        if parameter.choices is not None and not any(
            _same_value(value, choice) for choice in parameter.choices
        ):
            errors.append(
                ValidationIssue(
                    "invalid_choice", "value is not an allowed choice", path
                )
            )
        if parameter.minimum is not None and not _compare_bound(
            value,
            parameter.minimum,
            minimum=True,
        ):
            errors.append(
                ValidationIssue("below_minimum", "value is below the minimum", path)
            )
        if parameter.maximum is not None and not _compare_bound(
            value,
            parameter.maximum,
            minimum=False,
        ):
            errors.append(
                ValidationIssue("above_maximum", "value is above the maximum", path)
            )
        if not errors:
            return
        if issues:
            issues.extend(errors)
            return
        raise RecipeParameterError(
            errors[0].message, details={"path": path, "code": errors[0].code}
        )


def _template_names(value: str) -> list[str]:
    return re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}", value)


def _collect_template_names(value: Any) -> list[str]:
    """Collect template names recursively in deterministic encounter order."""
    if isinstance(value, str):
        _validate_template_syntax(value)
        return _template_names(value)
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            result.extend(_collect_template_names(key))
            result.extend(_collect_template_names(item))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_collect_template_names(item))
        return result
    return []


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return _canonical(left) == _canonical(right)


def _compare_bound(value: Any, bound: float, *, minimum: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    if minimum:
        return value >= bound
    return value <= bound


def _catalog_descriptor(catalog: Any, name: str) -> Any:
    """Return one public tool descriptor without resolving deferred schemas."""
    if isinstance(catalog, Mapping):
        return catalog.get(name)
    for method_name in ("get_tool", "get", "resolve", "resolve_tool"):
        method = getattr(catalog, method_name, None)
        if not callable(method):
            continue
        try:
            value = method(name)
            if hasattr(value, "__await__"):
                return None
            if value is not None:
                return value
        except Exception as exc:
            raise RecipeToolError("tool descriptor could not be read") from exc
    return None


def _validate_tool_step_schema(catalog: Any, step: Any) -> tuple[str, str] | None:
    """Reject unavailable or incompatible tool schemas before execution."""
    descriptor = _catalog_descriptor(catalog, step.tool)
    if descriptor is None:
        return None
    if isinstance(descriptor, Mapping):
        metadata = descriptor.get("metadata", {})
        blocked = bool(
            descriptor.get("blocked")
            or descriptor.get("enabled") is False
            or (isinstance(metadata, Mapping) and metadata.get("blocked"))
        )
        deferred = bool(
            descriptor.get("deferred", descriptor.get("is_deferred", False))
        )
        schema = descriptor.get(
            "input_schema", descriptor.get("inputSchema", descriptor.get("schema"))
        )
    else:
        metadata = getattr(descriptor, "metadata", {})
        blocked = bool(
            getattr(descriptor, "blocked", False)
            or getattr(descriptor, "enabled", True) is False
            or (isinstance(metadata, Mapping) and metadata.get("blocked"))
        )
        deferred = bool(getattr(descriptor, "deferred", False))
        schema = getattr(
            descriptor,
            "input_schema",
            getattr(descriptor, "inputSchema", getattr(descriptor, "schema", None)),
        )
    if blocked:
        return "blocked_tool", "tool is blocked by the catalog"
    if deferred:
        return (
            "missing_tool_schema",
            "deferred tool schema must be resolved before execution",
        )
    if schema is None:
        return None
    if hasattr(schema, "__await__"):
        return "missing_tool_schema", "tool schema is not synchronously available"
    if not isinstance(schema, Mapping):
        return "missing_tool_schema", "tool schema must be an object"
    if any(
        isinstance(value, str) and "${" in value for value in step.parameters.values()
    ):
        return None
    required = schema.get("required", ())
    if isinstance(required, str):
        required = (required,)
    if not isinstance(required, (list, tuple, set)):
        return "missing_tool_schema", "tool schema required field is invalid"
    missing = sorted(str(name) for name in required if str(name) not in step.parameters)
    if missing:
        return "missing_tool_parameter", "tool parameters are missing required fields"
    properties = schema.get("properties", {})
    if properties and not isinstance(properties, Mapping):
        return "missing_tool_schema", "tool schema properties are invalid"
    for name, value in step.parameters.items():
        definition = properties.get(name) if isinstance(properties, Mapping) else None
        if not isinstance(definition, Mapping):
            continue
        expected = definition.get("type")
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, Mapping),
        }.get(str(expected), True)
        if not valid:
            return "invalid_tool_parameter", "tool parameter has the wrong type"
    return None


def _catalog_names(catalog: Any) -> tuple[str, ...]:
    if isinstance(catalog, Mapping):
        return tuple(sorted(str(item) for item in catalog))
    if isinstance(catalog, ToolCatalog):
        return catalog.names()
    if isinstance(catalog, (str, bytes)):
        raise RecipeToolError("tool catalog must expose a name collection")
    if isinstance(catalog, (list, tuple, set, frozenset)):
        return tuple(sorted(str(item) for item in catalog))
    for method_name in (
        "names",
        "list_tools",
        "available_tools",
        "tool_names",
        "get_tool_names",
    ):
        method = getattr(catalog, method_name, None)
        if callable(method):
            try:
                values = method()
                if isinstance(values, Mapping):
                    return tuple(sorted(str(item) for item in values))
                return tuple(sorted(str(item) for item in values))
            except Exception as exc:
                raise RecipeToolError("tool catalog names could not be read") from exc
        if isinstance(method, (list, tuple, set, frozenset)):
            return tuple(sorted(str(item) for item in method))
    for attr in ("tools", "available_tools", "catalog", "entries"):
        value = getattr(catalog, attr, None)
        if isinstance(value, Mapping):
            return tuple(sorted(str(item) for item in value))
        if isinstance(value, (list, tuple, set, frozenset)):
            names: list[str] = []
            for item in value:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, Mapping) and item.get("name"):
                    names.append(str(item["name"]))
            if names:
                return tuple(sorted(names))
    return ()


def _catalog_supports_probe(catalog: Any) -> bool:
    return any(
        callable(getattr(catalog, name, None))
        for name in ("has_tool", "has", "contains", "validate", "supports")
    )


def _catalog_has(catalog: Any, name: str) -> bool:
    if isinstance(catalog, Mapping):
        return name in catalog
    if isinstance(catalog, ToolCatalog):
        return catalog.has(name)
    for method_name in ("has_tool", "has", "contains", "validate", "supports"):
        method = getattr(catalog, method_name, None)
        if callable(method):
            try:
                return bool(method(name))
            except Exception as exc:
                raise RecipeToolError(
                    "tool catalog availability could not be read"
                ) from exc
    return False


def validate_recipe(
    value: Any,
    *,
    tool_catalog: Any = None,
    validator: Optional[RecipeValidator] = None,
    parameters: Optional[Mapping[str, Any]] = None,
    raise_on_error: bool = True,
) -> Recipe | ValidationReport:
    """Validate one recipe using an optional injected validator."""
    active = validator or RecipeValidator(tool_catalog=tool_catalog)
    return active.validate(
        value,
        tool_catalog=tool_catalog,
        parameters=parameters,
        raise_on_error=raise_on_error,
    )


def resolve_parameters(
    recipe: Recipe | Mapping[str, Any],
    parameters: Optional[Mapping[str, Any]] = None,
    *,
    validator: Optional[RecipeValidator] = None,
) -> dict[str, Any]:
    """Resolve recipe parameters through a validator."""
    active = validator or RecipeValidator()
    return active.resolve_parameters(recipe, parameters)


__all__ = [
    "RecipeValidationFailure",
    "RecipeValidator",
    "resolve_parameters",
    "validate_recipe",
]
