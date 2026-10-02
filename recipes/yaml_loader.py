"""Compatibility exports for the strict recipe YAML loader."""

from ._yaml import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NESTING,
    DEFAULT_MAX_NODES,
    load_builtin_recipe,
    load_builtin_recipes,
    load_recipe,
    load_recipe_directory,
    load_recipe_file,
    load_recipe_yaml,
    load_yaml,
    loads_recipe,
    parse_recipe,
    parse_recipe_yaml,
    parse_yaml_subset,
    safe_load_recipe_yaml,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NESTING",
    "DEFAULT_MAX_NODES",
    "load_builtin_recipe",
    "load_builtin_recipes",
    "load_recipe",
    "load_recipe_directory",
    "load_recipe_file",
    "load_recipe_yaml",
    "load_yaml",
    "loads_recipe",
    "parse_recipe",
    "parse_recipe_yaml",
    "parse_yaml_subset",
    "safe_load_recipe_yaml",
]
