"""Offline coverage for portable recipe loading and execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recipes import (
    ExecutionPlan,
    Recipe,
    RecipeCache,
    RecipeCacheCorruptionError,
    RecipeCycleError,
    RecipeDepthError,
    RecipeError,
    RecipeParameterError,
    RecipeParseError,
    RecipeResolver,
    RecipeRunner,
    RecipeStep,
    RecipeSubrecipeError,
    RecipeTemplateError,
    RecipeToolError,
    RecipeValidator,
    load_builtin_recipe,
    load_recipe,
    parse_recipe,
    parse_yaml_subset,
)

VALID_RECIPE = """
schema_version: 1
name: demo
description: A portable demo.
parameters:
  target:
    type: string
    required: true
  retries:
    type: integer
    required: false
    default: 2
    min: 1
    max: 4
  labels:
    type: list
    required: false
    default: []
required_tools:
  - read
steps:
  - type: task
    task: "Inspect ${target} with ${retries} retries."
    strategy: review
    parameters:
      target: ${target}
      retries: ${retries}
  - type: tool
    tool: read
    parameters:
      path: "${target}"
"""


def test_valid_recipe_parses_without_pyyaml() -> None:
    recipe = parse_recipe(VALID_RECIPE)
    assert recipe.name == "demo"
    assert recipe.parameter_map()["retries"].default == 2
    assert recipe.steps[0].parameters["target"] == "${target}"
    assert recipe.steps[1].parameters["path"] == "${target}"


@pytest.mark.parametrize(
    "text",
    [
        "name: demo\nname: duplicate\n",
        "name: !!str demo\n",
        "name: &anchor demo\n",
        "name: *anchor\n",
        "name: demo\n<<: {}\n",
        'name: {"x": 1, "x": 2}\n',
    ],
)
def test_strict_yaml_rejects_unsafe_constructs(text: str) -> None:
    with pytest.raises(RecipeParseError):
        parse_yaml_subset(text)


def test_yaml_subset_supports_json_collections_comments_and_block_scalars() -> None:
    value = parse_yaml_subset(
        "items: [1, true, null]\ntext: |-\n  first\n  second\n# comment\n"
    )
    assert value == {"items": [1, True, None], "text": "first\nsecond"}


def test_loader_rejects_size_and_depth(tmp_path: Path) -> None:
    with pytest.raises(RecipeParseError):
        parse_yaml_subset("a: " + "x" * 100, max_bytes=32)
    with pytest.raises(RecipeParseError):
        parse_yaml_subset('a: {"b": {"c": 1}}', max_nesting=1)
    path = tmp_path / "recipe.yaml"
    path.write_text(VALID_RECIPE, encoding="utf-8")
    assert load_recipe(path, tool_catalog={"read": {}}).name == "demo"


def test_parameter_coercion_choices_bounds_and_unknown_values() -> None:
    recipe = parse_recipe(VALID_RECIPE)
    validator = RecipeResolver().validator
    values = validator.resolve_parameters(
        recipe, {"target": "src", "retries": "3", "labels": '["a"]'}
    )
    assert values == {"target": "src", "retries": 3, "labels": ["a"]}
    with pytest.raises(RecipeParameterError):
        validator.resolve_parameters(recipe, {"retries": 2})
    with pytest.raises(RecipeParameterError):
        validator.resolve_parameters(recipe, {"target": "src", "retries": 9})
    with pytest.raises(RecipeParameterError):
        validator.resolve_parameters(recipe, {"target": "src", "unknown": True})


def test_interpolation_is_strict_and_resolves_typed_values() -> None:
    recipe = Recipe(
        name="typed",
        parameters={
            "count": {"type": "integer", "required": True},
            "label": {"type": "string", "required": False, "default": "item-${count}"},
        },
        steps=[RecipeStep("task", task="Run ${label}", strategy="run")],
    )
    validator = RecipeValidator()
    values = validator.resolve_parameters(recipe, {"count": "2"})
    assert values == {"count": 2, "label": "item-2"}
    assert validator.interpolate({"value": "${count}"}, values) == {"value": 2}
    bad = Recipe(
        name="bad",
        steps=[RecipeStep("task", task="${unknown}", strategy="run")],
    )
    with pytest.raises(RecipeTemplateError):
        validator.validate(bad)


def test_invalid_defaults_and_secret_defaults_are_rejected_without_values() -> None:
    with pytest.raises(RecipeParameterError):
        RecipeValidator().validate(
            Recipe(
                name="bad-default",
                parameters={"count": {"type": "integer", "default": "3"}},
                steps=[RecipeStep("task", task="x", strategy="run")],
            )
        )
    secret = "sk-test-123456789"
    with pytest.raises(RecipeError) as caught:
        Recipe(
            name="secret-default",
            parameters={"api_key": {"type": "string", "default": f"api_key={secret}"}},
            steps=[RecipeStep("task", task="x", strategy="run")],
        )
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_tool_catalog_validation_reports_missing_and_undeclared_tools() -> None:
    recipe = Recipe(
        name="tools",
        required_tools=["read"],
        steps=[RecipeStep("tool", tool="write")],
    )
    report = RecipeValidator(tool_catalog={"read": {}}).check(recipe)
    assert not report.valid
    assert report.missing_tools == ()
    assert report.extra_tools == ("write",)
    with pytest.raises(RecipeToolError):
        RecipeValidator(tool_catalog={"write": {}}).validate(recipe)


def test_subrecipe_expansion_is_ordered_and_cycle_safe() -> None:
    child = Recipe(
        name="child",
        parameters={"value": {"type": "integer", "required": True}},
        steps=[RecipeStep("task", task="child ${value}", strategy="run")],
    )
    root = Recipe(
        name="root",
        parameters={"value": {"type": "integer", "default": 1}},
        steps=[
            RecipeStep("task", task="root", strategy="run"),
            RecipeStep("subrecipe", recipe="child", parameters={"value": "${value}"}),
        ],
    )
    resolution = RecipeResolver({"child": child}).resolve(root)
    assert [step.name for step in resolution] == ["root", "child 1"]
    assert [step.depth for step in resolution] == [0, 1]
    cyclic = Recipe(
        name="cycle",
        steps=[RecipeStep("subrecipe", recipe="cycle", parameters={})],
    )
    with pytest.raises(RecipeCycleError):
        RecipeResolver({"cycle": cyclic}).resolve(cyclic)
    with pytest.raises(RecipeDepthError):
        RecipeResolver({"child": child}, max_depth=0).resolve(root)


def test_dry_run_resolves_without_agent_execution() -> None:
    class Agent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, task: str) -> None:
            self.calls += 1

        def wait(self) -> None:
            self.calls += 1

    agent = Agent()
    recipe = parse_recipe(VALID_RECIPE)
    plan = RecipeRunner(agent=agent, tool_catalog={"read": {}}).dry_run(
        recipe, {"target": "src"}
    )
    assert isinstance(plan, ExecutionPlan)
    assert [step.kind for step in plan] == ["task", "tool"]
    assert agent.calls == 0
    assert "src" in plan.steps[0].name


def test_execute_uses_public_run_wait_and_stops_on_failure() -> None:
    class Agent:
        def __init__(self) -> None:
            self.runs: list[str] = []
            self.waits = 0

        def run(self, task: str) -> str:
            self.runs.append(task)
            return task

        def wait(self) -> dict[str, str]:
            self.waits += 1
            if len(self.runs) == 1:
                return {"status": "failed", "detail": "api_key=supersecret"}
            return {"status": "success"}

    agent = Agent()
    recipe = Recipe(
        name="execute",
        steps=[
            RecipeStep("task", task="first", strategy="run"),
            RecipeStep("task", task="second", strategy="run"),
            RecipeStep("task", task="third", strategy="run"),
        ],
    )
    result = RecipeRunner(agent=agent).execute(recipe)
    assert result.status == "failed"
    assert result.failure_index == 0
    assert agent.runs == ["first"]
    assert "supersecret" not in json.dumps(result.as_dict())


def test_cache_hit_ttl_corruption_and_eviction(tmp_path: Path) -> None:
    now = [100.0]
    cache = RecipeCache(
        tmp_path, max_entries=1, max_bytes=4096, ttl_seconds=5, clock=lambda: now[0]
    )
    recipe = Recipe("cached", steps=[RecipeStep("task", task="x", strategy="run")])
    key = cache.key_for(recipe, tool_schema_digest="schema-1")
    assert cache.get(key) is None
    assert cache.set(key, {"value": 1}, metadata={"api_key": "secret"}) is not None
    assert cache.get(key) == {"value": 1}
    assert cache.get(key, as_entry=True).metadata["api_key"] == "[REDACTED_SECRET]"
    second_key = "b" * 64
    cache.set(second_key, {"value": 2}, created_at=101)
    assert cache.get(key) is None
    assert cache.get(second_key) == {"value": 2}
    now[0] = 107
    assert cache.get(second_key) is None
    cache.set(second_key, {"value": 3}, created_at=200)
    path = cache.entry_path(second_key)
    path.write_text(
        path.read_text(encoding="utf-8").replace("value", "other"), encoding="utf-8"
    )
    with pytest.raises(RecipeCacheCorruptionError):
        cache.get(second_key)


def test_cache_key_changes_with_closure_and_tool_digest(tmp_path: Path) -> None:
    cache = RecipeCache(tmp_path)
    root = Recipe(
        "root", steps=[RecipeStep("subrecipe", recipe="child", parameters={})]
    )
    child = Recipe("child", steps=[RecipeStep("task", task="x", strategy="run")])
    first = cache.key_for(root, {"child": child}, "one")
    second = cache.key_for(root, {"child": child}, "two")
    assert first != second
    assert len(first) == 64


def test_builtin_recipe_expands_a_subrecipe() -> None:
    recipe = load_builtin_recipe("portable_code_review", validate=False)
    plan = RecipeRunner().dry_run(recipe, {"paths": ["src"]})
    assert [step.recipe_name for step in plan] == [
        "portable_code_review",
        "summarize_findings",
    ]
    assert "secret" not in repr(recipe)


def test_recipe_references_reject_traversal_and_missing_children() -> None:
    with pytest.raises(RecipeError):
        Recipe(
            name="unsafe",
            steps=[RecipeStep("subrecipe", recipe="../outside", parameters={})],
        )
    root = Recipe(
        name="root",
        steps=[RecipeStep("subrecipe", recipe="missing", parameters={})],
    )
    with pytest.raises(RecipeSubrecipeError):
        RecipeResolver({}).resolve(root)


def test_resolution_count_limit_is_enforced() -> None:
    recipe = Recipe(
        name="limited",
        steps=[
            RecipeStep("task", task="one", strategy="run"),
            RecipeStep("task", task="two", strategy="run"),
        ],
    )
    with pytest.raises(RecipeError):
        RecipeResolver(max_steps=1).resolve(recipe)


def test_cache_byte_limit_and_tool_digest_are_honored(tmp_path: Path) -> None:
    cache = RecipeCache(tmp_path, max_bytes=128)
    recipe = Recipe("small", steps=[RecipeStep("task", task="x", strategy="run")])
    assert cache.set(recipe, "x" * 1000) is None
    first = cache.key_for(recipe, tool_catalog={"read": {"version": 1}})
    second = cache.key_for(recipe, tool_catalog={"read": {"version": 2}})
    assert first != second


def test_reprs_redact_secret_shaped_step_values() -> None:
    secret = "api_key=supersecret"
    recipe = Recipe(
        name="redacted",
        steps=[RecipeStep("task", task=secret, strategy="run")],
    )
    plan = RecipeRunner().dry_run(recipe)
    assert secret not in repr(recipe)
    assert secret not in repr(plan)
    assert secret not in json.dumps(plan.as_dict())


def test_execute_uses_public_agent_sdk_typed_request(tmp_path: Path) -> None:
    """Recipe execution remains compatible with the real public SDK facade."""
    from agent_sdk import Agent

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")

    def model(messages, **kwargs):
        return json.dumps({"tool": "finish", "answer": "reviewed"})

    agent = Agent(
        repo,
        log_root=tmp_path / "logs",
        model=model,
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    recipe = Recipe(
        name="sdk-review",
        steps=[RecipeStep("task", task="Review app.py", strategy="question")],
    )
    try:
        result = RecipeRunner(agent=agent).execute(recipe)
    finally:
        agent.close()
    assert result.status == "success"
    assert result.steps[0].output["answer"] == "reviewed"
