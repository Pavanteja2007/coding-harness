# recipes/ AGENTS.md

## Purpose

`recipes/` is a self-contained, portable recipe package. It loads a deliberately
small YAML subset without requiring PyYAML, validates typed parameters and
required tools, expands nested subrecipes deterministically, provides a bounded
content-addressed cache, and offers side-effect-free dry-run plus execution
through a duck-typed public agent.

The package imports only Python standard-library modules and
`shared.security`. It does not import private harness, execution, runtime, or
agent modules.

## Public API

`recipes/__init__.py` exports:

- `Recipe`, `RecipeParameter`, and `RecipeStep`
- `RecipeCache` and `RecipeCacheEntry`
- `RecipeValidator`, `RecipeResolver`, and `RecipeRunner`
- `RecipeError` plus typed parse, schema, parameter, template, tool, subrecipe,
  cycle, depth, limit, security, cache, and execution errors
- `ExecutionPlan`, `ResolvedStep`, `RecipeRunResult`, and `ToolCatalog`
- `load_recipe`, `parse_recipe`, `parse_yaml_subset`, and built-in loaders

Compatibility facades are available in `recipe.py`, `core.py`, `loader.py`,
`validation.py`, `resolve.py`, `runner.py`, and `yaml_loader.py`.

## Schema version 1

A recipe document is a mapping with these required fields:

```yaml
schema_version: 1
name: example
description: A short description.
parameters: {}
required_tools: []
steps: []
```

`name` is a safe portable identifier. `description` is text. `parameters` maps
names to parameter definitions. `required_tools` is a unique sequence of safe
tool names. `steps` is a non-empty ordered sequence.

Parameter definitions support:

- `type`: `string`, `integer`, `number`, `boolean`, `list`, or `object`
- `required`: boolean
- `default`: a correctly typed default
- `choices`: a sequence of correctly typed allowed values
- `min` and `max`: numeric bounds
- `description`: optional text

A parameter may be omitted from a call when it is optional and has no default.
Supplied CLI-style strings are coerced for integer, number, boolean, list, and
object parameters. Defaults are type-checked strictly. Unknown supplied
parameters, missing required parameters, invalid choices, invalid bounds,
unknown template variables, and missing template values fail closed.

Templates use only `${name}` or `${name}` embedded in a larger string. Variable
names are portable identifiers. Malformed, empty, nested, or ambiguous
expressions are rejected. Exact placeholders preserve a typed value; embedded
placeholders use a deterministic string conversion.

Each step has `type: task | tool | subrecipe` and a `parameters` mapping:

- `task` requires `task` and an explicit safe `strategy`.
- `tool` requires `tool` and should be listed in `required_tools`.
- `subrecipe` requires a safe relative `recipe` reference and may provide
  parameters for the child.

Recipe references reject absolute paths, backslashes, traversal components,
encoded traversal, control characters, and secret-shaped values. Resolution
uses a stable mapping/closure order, detects cycles by recipe name, and enforces
maximum depth and expanded-step count.

## Supported YAML subset

The stdlib loader accepts only:

- block mappings and block sequences
- plain, single-quoted, and double-quoted scalars
- inline collections that are valid JSON
- booleans (`true`/`false`), null (`null`/`~`), integers, and finite numbers
- comments outside quoted/flow/block-scalar content
- literal and folded block scalars with common `+`/`-` chomping indicators

It rejects tags, anchors, aliases, merge keys, duplicate mapping keys,
multiple documents, tabs used for indentation, malformed JSON, unsupported
YAML directives, non-finite numbers, and ambiguous flow syntax. It does not
claim to implement arbitrary YAML. Files are bounded by UTF-8 byte size,
recursion depth, collection nesting, and node count. `yaml.safe_load` is not
required and is not used by the implementation, so an undeclared PyYAML
installation cannot change behavior or construct objects.

## Built-ins

`recipes/builtin/portable_code_review.yaml` demonstrates required and typed
parameters, `read`/`grep` required tools, interpolation, and a nested
`summarize_findings` subrecipe. `recipes/builtin/summarize_findings.yaml` is its portable
child. Neither built-in depends on private project modules.

Use `load_builtin_recipe("portable_code_review")` in source checkouts. A
wheel/sdist packaging handoff is still required: `pyproject.toml` was outside
this change's permitted scope and its explicit setuptools package list does not
yet include `recipes` or package its YAML data. The coordinator must add the
package/data declaration and run a wheel-installed smoke test before release.

## Cache and execution

`RecipeCache` uses SHA-256 over normalized raw recipe data, the resolved subrecipe
closure, supplied parameters, and a tool-schema digest. It writes atomically below a contained root,
rejects symlinked entries and corrupt records, redacts metadata and payloads before persistence,
honors TTL, and prunes by count and bytes.

`RecipeRunner.dry_run()` validates and resolves everything, returns an ordered
`ExecutionPlan`, and never imports, constructs, or calls an agent/model/tool.
`RecipeRunner.execute()` calls only public `run`/`wait` methods on an injected
agent (or an available `agent_sdk.Agent`), preserves resolved step parameters
in typed request metadata, records one redacted result per expanded step, and
stops at the first failure. Deferred, blocked, or schema-incompatible tool steps
fail validation before any step executes.

## Tests and verification

`tests/test_recipes.py` is fully offline and covers parsing, malformed and
unsafe YAML, parameter coercion and interpolation, tool catalogs, subrecipe
expansion/cycles/depth, cache hit/miss/TTL/corruption/eviction, dry-run
non-execution, fake-agent execution, built-ins, and secret redaction.

Run from the repository root:

```text
pytest -q tests/test_recipes.py
24 passed
ruff check recipes tests/test_recipes.py
ruff format --check recipes tests/test_recipes.py
```

## Handoffs and deliberate boundaries

- The release coordinator owns the `pyproject.toml` package-data change noted
  above; this task intentionally did not edit it.
- A future caller may provide a real duck-typed `ToolCatalog`; no private tool
  registry is coupled here.
- A future agent integration should preserve the public `run`/`wait` contract
  and pass the existing shared security redactor over all persisted output.
- Recipe execution is a sequencing adapter, not a model, shell, sandbox, or
  persistence implementation. Those boundaries remain owned by their existing
  modules.
