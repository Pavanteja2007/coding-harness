"""Deterministic, cycle-safe subrecipe resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from shared.security import require_contained

from ._yaml import load_recipe
from .models import (
    ExecutionPlan,
    Recipe,
    RecipeCycleError,
    RecipeDepthError,
    RecipeError,
    RecipeLimitError,
    RecipeStep,
    RecipeSubrecipeError,
    RecipeValidationError,
    ResolvedStep,
)
from .validator import RecipeValidator


@dataclass
class RecipeResolution:
    """The immutable-by-convention result of expanding a recipe."""

    recipe: Recipe
    parameters: dict[str, Any]
    steps: list[ResolvedStep]
    closure: dict[str, Recipe] = field(default_factory=dict)
    tool_schema_digest: str = ""

    def __iter__(self) -> Iterator[ResolvedStep]:
        """Iterate over expanded steps in execution order."""
        return iter(self.steps)

    def __len__(self) -> int:
        """Return the number of expanded steps."""
        return len(self.steps)

    def __getitem__(self, key: int | str) -> Any:
        """Index steps or access named resolution fields."""
        if isinstance(key, str):
            if key == "steps":
                return self.steps
            if key == "parameters":
                return self.parameters
            if key == "closure":
                return self.closure
            if key == "recipe":
                return self.recipe
            if key == "tool_schema_digest":
                return self.tool_schema_digest
            if key == "recipe_name":
                return self.recipe.name
            raise KeyError(key)
        return self.steps[key]

    @property
    def recipe_name(self) -> str:
        """Return the root recipe name."""
        return self.recipe.name

    @property
    def recipes(self) -> dict[str, Recipe]:
        """Return the resolved recipe closure keyed by recipe name."""
        return self.closure

    @property
    def plan(self) -> ExecutionPlan:
        """Return this resolution as a side-effect-free execution plan."""
        return ExecutionPlan(
            recipe_name=self.recipe.name,
            parameters=self.parameters,
            steps=self.steps,
            closure=self.closure,
            tool_schema_digest=self.tool_schema_digest,
            recipe=self.recipe,
        )

    execution_plan = plan

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a redacted JSON-compatible resolution summary."""
        return {
            "recipe": self.recipe.name,
            "parameters": self.parameters
            if not redact
            else self._redacted_parameters(),
            "steps": [step.to_dict(redact=redact) for step in self.steps],
            "closure": sorted(self.closure),
            "tool_schema_digest": self.tool_schema_digest,
        }

    def _redacted_parameters(self) -> dict[str, Any]:
        from shared.security import redact_secrets

        return dict(redact_secrets(self.parameters))

    def cache_key(self, tool_catalog: Any = None) -> str:
        """Return the content key for this resolved recipe closure."""
        from .cache import RecipeCache

        return RecipeCache.key_for(
            self.recipe,
            self.closure,
            self.tool_schema_digest,
            tool_catalog=tool_catalog,
            parameters=self.parameters,
        )

    to_dict = as_dict

    def __eq__(self, other: object) -> bool:
        """Compare resolutions by expanded steps for list-like callers."""
        if isinstance(other, (list, tuple)):
            return self.steps == list(other)
        if isinstance(other, RecipeResolution):
            return (
                self.recipe_name == other.recipe_name
                and self.parameters == other.parameters
                and self.steps == other.steps
            )
        return NotImplemented

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return f"RecipeResolution({self.as_dict(redact=True)!r})"


class RecipeResolver:
    """Expand subrecipe references with deterministic limits and provenance."""

    def __init__(
        self,
        recipes: Any = None,
        tool_catalog: Any = None,
        *,
        subrecipes: Any = None,
        catalog: Any = None,
        validator: Optional[RecipeValidator] = None,
        recipe_root: Any = None,
        loader: Optional[Callable[[Any], Recipe]] = None,
        max_depth: int = 16,
        max_steps: int = 4096,
        max_count: Optional[int] = None,
        max_recipe_depth: Optional[int] = None,
    ) -> None:
        """Create a resolver over a mapping, callback, directory, or built-ins."""
        if max_count is not None:
            max_steps = max_count
        if max_recipe_depth is not None:
            max_depth = max_recipe_depth
        if max_depth < 0 or max_steps <= 0:
            raise RecipeValidationError("resolver limits are invalid")
        if subrecipes is not None:
            if recipes is not None:
                raise RecipeValidationError("recipes and subrecipes disagree")
            recipes = subrecipes
        if catalog is not None:
            if tool_catalog is not None:
                raise RecipeValidationError("tool_catalog and catalog disagree")
            tool_catalog = catalog
        if recipe_root is None and isinstance(recipes, (str, Path)):
            candidate = Path(recipes)
            if candidate.is_dir():
                recipe_root = candidate
                recipes = None
        self.recipes = recipes
        self.tool_catalog = tool_catalog
        self.validator = validator or RecipeValidator(
            tool_catalog=tool_catalog,
            max_depth=max_depth + 1,
            max_steps=max_steps,
        )
        self.recipe_root = Path(recipe_root) if recipe_root is not None else None
        self.loader = loader
        self.max_depth = max_depth
        self.max_steps = max_steps

    def resolve(
        self,
        recipe: Recipe | Mapping[str, Any] | str | Path,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        tool_catalog: Any = None,
        max_depth: Optional[int] = None,
        max_steps: Optional[int] = None,
        max_count: Optional[int] = None,
        max_recipe_depth: Optional[int] = None,
    ) -> RecipeResolution:
        """Resolve a recipe and all nested subrecipes into ordered steps."""
        root = self._coerce_root(recipe)
        catalog = self.tool_catalog if tool_catalog is None else tool_catalog
        depth_limit = self.max_depth if max_depth is None else max_depth
        if max_recipe_depth is not None:
            depth_limit = max_recipe_depth
        step_limit = self.max_steps if max_steps is None else max_steps
        if max_count is not None:
            step_limit = max_count
        if depth_limit < 0 or step_limit <= 0:
            raise RecipeValidationError("resolver limits are invalid")
        root_parameters = self.validator.resolve_parameters(root, parameters or {})
        steps: list[ResolvedStep] = []
        closure: dict[str, Recipe] = {}
        tools: set[str] = set(root.required_tools)
        self._expand(
            root,
            root_parameters,
            depth=0,
            stack=(),
            steps=steps,
            closure=closure,
            tools=tools,
            step_limit=step_limit,
            depth_limit=depth_limit,
            catalog=catalog,
        )
        self._validate_aggregate_tools(tools, catalog)
        return RecipeResolution(
            recipe=root,
            parameters=root_parameters,
            steps=steps,
            closure=closure,
            tool_schema_digest=self.validator.tool_schema_digest(catalog),
        )

    def expand(
        self,
        recipe: Recipe | Mapping[str, Any] | str | Path,
        parameters: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> list[ResolvedStep]:
        """Return only the ordered expanded step list."""
        return self.resolve(recipe, parameters, **kwargs).steps

    resolve_steps = expand
    resolve_recipe = resolve
    expand_recipe = expand

    def plan(
        self,
        recipe: Recipe | Mapping[str, Any] | str | Path,
        parameters: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> ExecutionPlan:
        """Return a side-effect-free execution plan for a resolved recipe."""
        resolution = self.resolve(recipe, parameters, **kwargs)
        return ExecutionPlan(
            recipe_name=resolution.recipe.name,
            parameters=resolution.parameters,
            steps=resolution.steps,
            closure=resolution.closure,
            tool_schema_digest=resolution.tool_schema_digest,
            recipe=resolution.recipe,
        )

    dry_run = plan

    def _coerce_root(self, recipe: Any) -> Recipe:
        if isinstance(recipe, Recipe):
            return recipe
        if isinstance(recipe, Mapping):
            return Recipe.from_dict(recipe)
        return load_recipe(recipe, validate=False)

    def _expand(
        self,
        recipe: Recipe,
        parameters: Mapping[str, Any],
        *,
        depth: int,
        stack: tuple[str, ...],
        steps: list[ResolvedStep],
        closure: dict[str, Recipe],
        tools: set[str],
        step_limit: int,
        depth_limit: int,
        catalog: Any,
    ) -> None:
        self.validator.validate(recipe, tool_catalog=catalog)
        if depth > depth_limit:
            raise RecipeDepthError("subrecipe expansion exceeds the maximum depth")
        if recipe.name in stack:
            chain = " -> ".join((*stack, recipe.name))
            raise RecipeCycleError(f"subrecipe cycle detected: {chain}")
        if len(steps) > step_limit:
            raise RecipeLimitError("subrecipe expansion exceeds the maximum step count")
        previous = closure.get(recipe.name)
        if previous is not None and previous.normalized() != recipe.normalized():
            raise RecipeSubrecipeError(
                "the same recipe name resolved to different definitions"
            )
        closure.setdefault(recipe.name, recipe)
        next_stack = (*stack, recipe.name)
        for step in recipe.steps:
            if len(steps) >= step_limit:
                raise RecipeLimitError(
                    "subrecipe expansion exceeds the maximum step count"
                )
            if step.kind == "subrecipe":
                child = self._lookup_subrecipe(step.recipe)
                child_parameters = self.validator.interpolate(
                    step.parameters,
                    parameters,
                    known_names=tuple(recipe.parameter_map()),
                )
                resolved_child = self.validator.resolve_parameters(
                    child, child_parameters
                )
                tools.update(child.required_tools)
                self._expand(
                    child,
                    resolved_child,
                    depth=depth + 1,
                    stack=next_stack,
                    steps=steps,
                    closure=closure,
                    tools=tools,
                    step_limit=step_limit,
                    depth_limit=depth_limit,
                    catalog=catalog,
                )
                continue
            interpolated_task = (
                self.validator.interpolate(
                    step.task,
                    parameters,
                    known_names=tuple(recipe.parameter_map()),
                )
                if step.kind == "task"
                else step.tool
            )
            interpolated_parameters = self.validator.interpolate(
                step.parameters,
                parameters,
                known_names=tuple(recipe.parameter_map()),
            )
            if not isinstance(interpolated_parameters, Mapping):
                raise RecipeValidationError("step parameters must remain a mapping")
            resolved_step = RecipeStep(
                step.kind,
                task=interpolated_task if step.kind == "task" else "",
                tool=step.tool,
                recipe=step.recipe,
                strategy=step.strategy,
                parameters=dict(interpolated_parameters),
                step_id=step.step_id,
                description=step.description,
            )
            resolved = ResolvedStep(
                step=resolved_step,
                recipe_name=recipe.name,
                depth=depth,
                position=len(steps),
                parameters=dict(interpolated_parameters),
            )
            steps.append(resolved)
            if step.kind == "tool":
                tools.add(step.tool)

    def _lookup_subrecipe(self, reference: str) -> Recipe:
        from .models import _validate_reference

        safe_reference = _validate_reference(reference)
        source = self.recipes
        if isinstance(source, Mapping):
            value = source.get(safe_reference)
            reference_stem = Path(safe_reference).stem
            if value is None and reference_stem != safe_reference:
                value = source.get(reference_stem)
            if value is None:
                for _key, candidate in source.items():
                    if isinstance(candidate, Recipe) and candidate.name in {
                        safe_reference,
                        reference_stem,
                    }:
                        value = candidate
                        break
            if value is not None:
                return self._coerce_recipe(value)
        if callable(source):
            try:
                value = source(safe_reference)
            except RecipeError:
                raise
            except Exception as exc:
                raise RecipeSubrecipeError("subrecipe loader failed") from exc
            if value is not None:
                return self._coerce_recipe(value)
        if self.loader is not None:
            try:
                value = self.loader(safe_reference)
            except RecipeError:
                raise
            except Exception as exc:
                raise RecipeSubrecipeError("subrecipe loader failed") from exc
            if value is not None:
                return self._coerce_recipe(value)
        if self.recipe_root is not None:
            root = self.recipe_root
            candidates = [
                root / safe_reference,
                root / f"{safe_reference}.yaml",
                root / f"{safe_reference}.yml",
            ]
            for candidate in candidates:
                try:
                    contained = require_contained(root, candidate)
                except Exception:
                    continue
                if contained.is_file():
                    return load_recipe(contained, validate=False)
        from ._yaml import load_builtin_recipe

        try:
            return load_builtin_recipe(safe_reference, validate=False)
        except RecipeError as exc:
            raise RecipeSubrecipeError(
                f"subrecipe is not available: {safe_reference}"
            ) from exc

    def _coerce_recipe(self, value: Any) -> Recipe:
        if isinstance(value, Recipe):
            return value
        if isinstance(value, Mapping):
            return Recipe.from_dict(value)
        if isinstance(value, (str, Path)):
            return load_recipe(value, validate=False)
        raise RecipeSubrecipeError("subrecipe value is not a recipe")

    def _validate_aggregate_tools(self, tools: set[str], catalog: Any) -> None:
        if catalog is None:
            return
        from .validator import _catalog_has, _catalog_names, _catalog_supports_probe

        available = set(_catalog_names(catalog))
        if not available and _catalog_supports_probe(catalog):
            missing = sorted(name for name in tools if not _catalog_has(catalog, name))
        else:
            missing = sorted(tools - available)
        if missing:
            raise RecipeValidationError("one or more required tools are not available")


class RecipeStepWithInterpolation:
    """Internal immutable view of a step after parameter interpolation."""

    def __init__(
        self, *, original: Any, task: str, parameters: Mapping[str, Any]
    ) -> None:
        """Create an interpolated step without exposing mutable input state."""
        self.kind = original.kind
        self.tool = original.tool
        self.recipe = original.recipe
        self.strategy = original.strategy
        self.description = original.description
        self.step_id = original.step_id
        self.task = task
        self.parameters = dict(parameters)

    @property
    def name(self) -> str:
        """Return the underlying task or tool name."""
        return self.task if self.kind == "task" else self.tool

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a redacted mapping for diagnostics."""
        from shared.security import redact_secrets

        return {
            "type": self.kind,
            "task": self.task,
            "tool": self.tool,
            "recipe": self.recipe,
            "strategy": self.strategy,
            "parameters": dict(redact_secrets(self.parameters))
            if redact
            else dict(self.parameters),
        }


Resolution = RecipeResolution

__all__ = ["RecipeResolution", "RecipeResolver", "Resolution"]
