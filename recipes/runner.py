"""Dry-run and public-agent execution for resolved recipes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any, Callable, Optional

from shared.security import redact_secrets, redact_text

from .models import (
    ExecutionPlan,
    Recipe,
    RecipeExecutionError,
    RecipeRunResult,
    RecipeStepResult,
    ResolvedStep,
)
from .resolver import RecipeResolver
from .validator import RecipeValidator


class RecipeRunner:
    """Resolve recipes safely and optionally execute plans through a public agent."""

    def __init__(
        self,
        agent: Any = None,
        *,
        agent_factory: Optional[Callable[[], Any]] = None,
        tool_catalog: Any = None,
        resolver: Optional[RecipeResolver] = None,
        validator: Optional[RecipeValidator] = None,
        cache: Any = None,
    ) -> None:
        """Create a runner without importing or starting an agent during dry-run."""
        self.agent = agent
        self.agent_factory = agent_factory
        self.tool_catalog = tool_catalog
        self.validator = validator or RecipeValidator(tool_catalog=tool_catalog)
        self.resolver = resolver or RecipeResolver(
            tool_catalog=tool_catalog, validator=self.validator
        )
        self.cache = cache

    def dry_run(
        self,
        recipe: Recipe | Mapping[str, Any] | str,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        resolver: Optional[RecipeResolver] = None,
        tool_catalog: Any = None,
    ) -> ExecutionPlan:
        """Resolve parameters and subrecipes without agent, model, or tool calls."""
        active_resolver = resolver or self.resolver
        return active_resolver.plan(
            recipe,
            parameters,
            tool_catalog=self.tool_catalog if tool_catalog is None else tool_catalog,
        )

    plan = dry_run

    def execute(
        self,
        recipe: Recipe | Mapping[str, Any] | str,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        agent: Any = None,
        resolver: Optional[RecipeResolver] = None,
        tool_catalog: Any = None,
    ) -> RecipeRunResult:
        """Execute each expanded step through ``agent.run`` and ``agent.wait``."""
        plan = self.dry_run(
            recipe,
            parameters,
            resolver=resolver,
            tool_catalog=tool_catalog,
        )
        active_agent = self._get_agent(agent)
        results: list[RecipeStepResult] = []
        for index, step in enumerate(plan.steps):
            try:
                run_value = self._invoke_run(active_agent, step)
                run_failed, run_error = _run_failure(run_value)
                wait_value = self._invoke_wait(active_agent, run_value)
                output = run_value if wait_value is None else wait_value
                wait_failed, wait_error = _failure(output)
                failed = run_failed or wait_failed
                error = wait_error or run_error
                if failed:
                    result = RecipeStepResult(
                        index=index,
                        kind=step.kind,
                        name=step.name,
                        status="failed",
                        output=redact_secrets(output),
                        error=redact_text(error or ""),
                        depth=step.depth,
                    )
                else:
                    result = RecipeStepResult(
                        index=index,
                        kind=step.kind,
                        name=step.name,
                        status="success",
                        output=redact_secrets(output),
                        depth=step.depth,
                    )
            except Exception as exc:
                result = RecipeStepResult(
                    index=index,
                    kind=step.kind,
                    name=step.name,
                    status="error",
                    output=None,
                    error=redact_text(str(exc)),
                    depth=step.depth,
                )
            results.append(result)
            if result.status != "success":
                aggregate_status = "error" if result.status == "error" else "failed"
                return RecipeRunResult(
                    status=aggregate_status,
                    steps=results,
                    failure_index=index,
                )
        return RecipeRunResult(status="success", steps=results)

    run = execute

    def _get_agent(self, supplied: Any = None) -> Any:
        agent = supplied if supplied is not None else self.agent
        if agent is None and self.agent_factory is not None:
            try:
                agent = self.agent_factory()
            except Exception as exc:
                raise RecipeExecutionError("agent factory failed") from exc
        if isinstance(agent, type):
            try:
                agent = agent()
            except Exception as exc:
                raise RecipeExecutionError("agent could not be created") from exc
        if agent is None:
            try:
                import agent_sdk
            except ImportError as exc:
                raise RecipeExecutionError("agent_sdk.Agent is unavailable") from exc
            factory = getattr(agent_sdk, "Agent", None)
            if factory is None:
                raise RecipeExecutionError("agent_sdk.Agent is unavailable")
            try:
                agent = factory()
            except Exception as exc:
                raise RecipeExecutionError("agent could not be created") from exc
        if not callable(getattr(agent, "run", None)):
            raise RecipeExecutionError("agent must expose a public run method")
        return agent

    @staticmethod
    def _invoke_run(agent: Any, step: ResolvedStep) -> Any:
        method = agent.run
        signature = _signature(method)
        task_text = str(step.step.task or "").strip()
        if not task_text:
            task_text = f"Execute recipe step {step.to_dict(redact=True)!r}"
        request = {
            "kind": step.kind,
            "task": task_text,
            "tool": step.step.tool,
            "recipe": step.recipe_name,
            "strategy": step.step.strategy,
            "parameters": step.parameters,
            "metadata": {
                "recipe_step": {
                    "kind": step.kind,
                    "tool": step.step.tool,
                    "recipe": step.recipe_name,
                    "parameters": dict(step.parameters),
                }
            },
        }
        if signature is None:
            return _resolve_awaitable(method(task_text))
        parameters = signature.parameters
        has_kwargs = any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        names = set(parameters)
        kwargs: dict[str, Any] = {}
        request_field = next(
            (name for name in ("task", "prompt", "request") if name in names), ""
        )
        if request_field:
            kwargs[request_field] = task_text
        elif "step" in names:
            kwargs["step"] = step.step
        elif "name" in names:
            kwargs["name"] = step.name
        elif "action" in names:
            kwargs["action"] = step.name
        for name in ("strategy", "parameters", "tool", "kind", "recipe", "metadata"):
            if name in names and request[name] not in (None, "", {}):
                kwargs[name] = request[name]
        if has_kwargs:
            if (
                not request_field
                and "step" not in names
                and "name" not in names
                and "action" not in names
            ):
                kwargs.setdefault("request", task_text)
            if request["strategy"]:
                kwargs.setdefault("strategy", request["strategy"])
            kwargs.setdefault("metadata", request["metadata"])
        if "arguments" in names:
            kwargs["arguments"] = request["parameters"]
        if "params" in names:
            kwargs["params"] = request["parameters"]
        if "options" in names:
            kwargs["options"] = request["parameters"]
        if "spec" in names:
            kwargs["spec"] = request
        positional = _first_positional(signature)
        if not kwargs and positional is not None:
            if positional.name in {"task", "prompt", "request"}:
                return _resolve_awaitable(method(task_text))
            if positional.name == "step":
                return _resolve_awaitable(method(step.step))
            return _resolve_awaitable(method(request))
        if not kwargs and not has_kwargs:
            return _resolve_awaitable(method())
        return _resolve_awaitable(method(**kwargs))

    @staticmethod
    def _invoke_wait(agent: Any, run_value: Any) -> Any:
        method = getattr(agent, "wait", None)
        if not callable(method):
            handle_wait = getattr(run_value, "wait", None)
            if callable(handle_wait):
                return _resolve_awaitable(handle_wait())
            return run_value
        signature = _signature(method)
        if signature is None:
            return _resolve_awaitable(method(run_value))
        positional = _first_positional(signature)
        if positional is not None and positional.default is inspect.Parameter.empty:
            wait_value = run_value
            if positional.name in {"run_id", "run", "handle"} and isinstance(
                run_value, Mapping
            ):
                wait_value = run_value.get("run_id", run_value.get("run", run_value))
            return _resolve_awaitable(method(wait_value))
        return _resolve_awaitable(method())

    def __repr__(self) -> str:
        """Return a redacted representation without exposing agent state."""
        return (
            f"RecipeRunner(agent={type(self.agent).__name__ if self.agent else None!r})"
        )


def _signature(method: Callable[..., Any]) -> Optional[inspect.Signature]:
    try:
        return inspect.signature(method)
    except (TypeError, ValueError):
        return None


def _first_positional(signature: inspect.Signature) -> Optional[inspect.Parameter]:
    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return parameter
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return parameter
    return None


def _resolve_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RecipeExecutionError(
        "asynchronous agent execution requires a synchronous caller"
    )


def _run_failure(value: Any) -> tuple[bool, Optional[str]]:
    failed, error = _failure(value)
    if not failed or not isinstance(value, Mapping):
        return failed, error
    status = str(
        getattr(value.get("status"), "value", value.get("status", ""))
    ).casefold()
    if status in {"pending", "queued", "started", "running"}:
        return False, None
    if not any(key in value for key in ("run_id", "run", "handle")):
        return failed, error
    if any(value.get(key) is False for key in ("success", "ok", "completed", "passed")):
        return failed, error
    if status in {
        "failed",
        "error",
        "cancelled",
        "timeout",
        "blocked",
        "needs_input",
    }:
        return failed, error
    return False, None


def _failure(value: Any) -> tuple[bool, Optional[str]]:
    if value is False:
        return True, "agent reported failure"
    if isinstance(value, Mapping):
        for key in ("success", "ok", "completed", "passed"):
            if key in value and value[key] is False:
                return True, f"agent reported {key}=false"
        status_value = value.get("status", "")
        status = str(getattr(status_value, "value", status_value)).casefold()
        if status and status not in {
            "success",
            "succeeded",
            "completed",
            "completed_verified",
            "completed_unverified",
            "ok",
            "done",
        }:
            return True, f"agent reported status {status}"
        exit_code = value.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return True, "agent reported a non-zero exit code"
        return False, None
    for attribute in ("success", "ok", "completed", "passed"):
        if hasattr(value, attribute) and getattr(value, attribute) is False:
            return True, f"agent reported {attribute}=false"
    status = getattr(value, "status", None)
    if status is not None:
        normalized = str(getattr(status, "value", status)).casefold()
        if normalized not in {
            "success",
            "succeeded",
            "completed",
            "completed_verified",
            "completed_unverified",
            "ok",
            "done",
        }:
            return True, f"agent reported status {normalized}"
    exit_code = getattr(value, "exit_code", None)
    if isinstance(exit_code, int) and exit_code != 0:
        return True, "agent reported a non-zero exit code"
    return False, None


__all__ = ["RecipeRunner"]
