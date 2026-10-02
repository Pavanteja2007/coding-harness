"""User-facing Agent and Conversation facades over local or remote transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .errors import InvalidRequestError
from .events import Events
from .local import LocalClient, LocalTransport
from .models import Result, RunRequest, Tool
from .remote import RemoteClient, RemoteTransport
from .transport import RunHandle
from .workspace import Workspace

__all__ = [
    "Agent",
    "Conversation",
    "LocalAgent",
    "LocalClient",
    "RemoteAgent",
    "RemoteClient",
]


class Conversation:
    """A session-scoped facade with the same operations on either transport."""

    def __init__(
        self,
        transport: Any,
        session_id: str = "",
        *,
        agent: "Agent | None" = None,
        hooks: Any = None,
        hook_manager: Any = None,
        tool_catalog: Any = None,
        catalog: Any = None,
    ) -> None:
        """Bind a conversation to a transport and stable session identifier."""
        if hooks is not None and hook_manager is not None and hooks is not hook_manager:
            raise InvalidRequestError(
                "hooks and hook_manager must identify the same manager"
            )
        if (
            tool_catalog is not None
            and catalog is not None
            and tool_catalog is not catalog
        ):
            raise InvalidRequestError(
                "tool_catalog and catalog must identify the same catalog"
            )
        if isinstance(transport, Agent):
            agent = transport
            transport = agent.transport
        if not hasattr(transport, "run"):
            raise InvalidRequestError("conversation requires an agent transport")
        selected_hooks = hook_manager if hook_manager is not None else hooks
        if (
            selected_hooks is not None
            and getattr(transport, "supports_local_hooks", True) is False
        ):
            raise InvalidRequestError(
                "remote transports do not execute client-local hooks"
            )
        if selected_hooks is not None and hasattr(transport, "hooks"):
            transport.hooks = selected_hooks
            transport.hook_manager = selected_hooks
        self.hooks = (
            selected_hooks
            if selected_hooks is not None
            else getattr(transport, "hooks", None)
        )
        self.hook_manager = self.hooks
        selected_catalog = tool_catalog if tool_catalog is not None else catalog
        if selected_catalog is not None and hasattr(transport, "tool_catalog"):
            transport.tool_catalog = selected_catalog
            transport.catalog = selected_catalog
        self.tool_catalog = (
            selected_catalog
            if selected_catalog is not None
            else getattr(transport, "tool_catalog", None)
        )
        self.catalog = self.tool_catalog
        self.transport = transport
        self.agent = agent
        self.session_id = str(session_id or getattr(transport, "session_id", ""))
        if not self.session_id:
            self.session_id = self._new_session_id()
        self.id = self.session_id

    @staticmethod
    def _new_session_id() -> str:
        """Generate a session id for a custom transport that does not provide one."""
        from .models import new_session_id

        return new_session_id()

    def _request(self, value: Any, **kwargs: Any) -> RunRequest:
        request = RunRequest.from_value(value, **kwargs)
        request.session_id = self.session_id
        if not request.repo_path:
            request.repo_path = str(getattr(self.agent, "repo_path", "") or "")
        return request

    def query(self, request: Any = "", **kwargs: Any) -> Result:
        """Run a synchronous query in this conversation."""
        value = self._request(request, **kwargs)
        return self.transport.query(value)

    def run(
        self, request: Any = "", *, wait: bool | None = None, **kwargs: Any
    ) -> Result | RunHandle:
        """Start a run in this conversation."""
        value = self._request(request, **kwargs)
        return self.transport.run(value, wait=wait)

    def stream(
        self,
        request: Any = "",
        *,
        run_id: str = "",
        after_sequence: int = 0,
        **kwargs: Any,
    ) -> Events:
        """Start or select a run and return its event stream."""
        if isinstance(request, RunHandle):
            return request.events(after_sequence)
        if run_id:
            return self.transport.events(
                run_id, session_id=self.session_id, after_sequence=after_sequence
            )
        text = str(request or "")
        checker = getattr(self.transport, "has_run", None)
        if text and callable(checker):
            try:
                if checker(text):
                    return self.transport.events(
                        text, session_id=self.session_id, after_sequence=after_sequence
                    )
            except Exception:
                pass
        value = self._request(text or "continue", **kwargs)
        handle = self.transport.run(value, wait=False)
        if not isinstance(handle, RunHandle):
            result = handle if isinstance(handle, Result) else Result(handle)
            return Events(
                self.transport,
                result.run_result.run_id,
                session_id=self.session_id,
                after_sequence=after_sequence,
            )
        return handle.events(after_sequence)

    def cancel(self, run_id: str = "") -> bool:
        """Cancel a selected run, or the latest active local conversation run."""
        if run_id:
            return self.transport.cancel(run_id)
        lister = getattr(self.transport, "list_runs", None)
        if callable(lister):
            values = lister()
            candidates = [
                value
                for value in values
                if isinstance(value, Mapping)
                and value.get("session_id") == self.session_id
            ]
            active = [
                value
                for value in candidates
                if not value.get("terminal") and not value.get("done")
            ]
            for value in reversed(active or candidates):
                return self.transport.cancel(str(value.get("run_id", "")))
        return False

    def replay(self, run_id: str) -> Any:
        """Replay a selected run through the public kernel projection."""
        return self.transport.replay(run_id)

    def events(self, run_id: str, *, after_sequence: int = 0) -> Events:
        """Return an event stream for a selected run."""
        return self.transport.events(
            run_id, session_id=self.session_id, after_sequence=after_sequence
        )

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return bounded public conversation turns."""
        reader = getattr(self.transport, "history", None)
        if not callable(reader):
            return []
        return list(reader(self.session_id, limit=limit))

    def list_tools(self) -> list[Tool]:
        """List catalog tools through the bound transport."""
        return self.transport.list_tools()

    def search_tools(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search catalog tools through the bound transport."""
        if limit is None:
            return self.transport.search_tools(query, max_results)
        return self.transport.search_tools(query, limit=limit)

    def get_tool(self, name: str) -> Tool:
        """Return one exact catalog tool through the bound transport."""
        return self.transport.get_tool(name)

    def search(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search catalog metadata under the short alias."""
        return self.search_tools(query, max_results, limit=limit)

    def get(self, name: str) -> Tool:
        """Return one exact catalog descriptor under the short alias."""
        return self.get_tool(name)

    def resolve_tool_schema(self, name: str) -> Any:
        """Resolve one catalog schema through the bound transport."""
        return self.transport.resolve_tool_schema(name)

    def resolve_schema(self, name: str) -> Any:
        """Resolve one catalog schema under the short compatibility name."""
        return self.transport.resolve_tool_schema(name)

    def resolve_tool(self, name: str) -> Any:
        """Resolve one catalog schema under the resolve-tool alias."""
        return self.transport.resolve_tool_schema(name)

    def resolve(self, name: str) -> Any:
        """Resolve one catalog schema under the short resolve alias."""
        return self.transport.resolve_tool_schema(name)

    def close(self) -> None:
        """Close this conversation without closing unrelated agent transports."""
        closer = getattr(self.transport, "close_conversation", None)
        if callable(closer):
            closer(self.session_id)

    def __enter__(self) -> "Conversation":
        """Enter a conversation context."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Close the conversation on context exit."""
        self.close()


class Agent:
    """Transport-neutral coding agent facade with identical local/remote semantics."""

    def __init__(
        self,
        repo_path: str | Path | Workspace = "",
        *,
        transport: Any = None,
        client: Any = None,
        base_url: str = "",
        token: str = "",
        api_key: str = "",
        log_root: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
        model: Any = None,
        call_fn: Any = None,
        model_callable: Any = None,
        model_fn: Any = None,
        verifier: Any = None,
        session_id: str = "",
        workspace: Workspace | str | None = None,
        workspace_root: str | Path | None = None,
        on_event: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        hooks: Any = None,
        hook_manager: Any = None,
        tool_catalog: Any = None,
        catalog: Any = None,
    ) -> None:
        """Create a local agent by default or a remote agent when a base URL is supplied."""
        if hooks is not None and hook_manager is not None and hooks is not hook_manager:
            raise InvalidRequestError(
                "hooks and hook_manager must identify the same manager"
            )
        if (
            tool_catalog is not None
            and catalog is not None
            and tool_catalog is not catalog
        ):
            raise InvalidRequestError(
                "tool_catalog and catalog must identify the same catalog"
            )
        selected_hooks = hook_manager if hook_manager is not None else hooks
        selected_catalog = tool_catalog if tool_catalog is not None else catalog
        selected_transport = transport or client
        if selected_transport is None and base_url:
            selected_transport = RemoteTransport(
                base_url,
                token=token or api_key,
                timeout=timeout,
                session_id=session_id,
                headers=headers,
                hooks=selected_hooks,
                tool_catalog=selected_catalog,
            )
        if selected_transport is None:
            selected_repo = workspace if workspace else repo_path
            selected_transport = LocalTransport(
                selected_repo,
                log_root=log_root,
                config=config,
                model=model,
                call_fn=call_fn,
                model_callable=model_callable,
                model_fn=model_fn,
                verifier=verifier,
                workspace_root=workspace_root,
                session_id=session_id,
                on_event=on_event,
                hooks=selected_hooks,
                tool_catalog=selected_catalog,
            )
        if not hasattr(selected_transport, "run"):
            raise InvalidRequestError(
                "transport must implement the public agent protocol"
            )
        if (
            selected_hooks is not None
            and getattr(selected_transport, "supports_local_hooks", True) is False
        ):
            raise InvalidRequestError(
                "remote transports do not execute client-local hooks"
            )
        if selected_hooks is not None and hasattr(selected_transport, "hooks"):
            selected_transport.hooks = selected_hooks
            selected_transport.hook_manager = selected_hooks
        if selected_catalog is not None and hasattr(selected_transport, "tool_catalog"):
            selected_transport.tool_catalog = selected_catalog
            selected_transport.catalog = selected_catalog
        self.hooks = (
            selected_hooks
            if selected_hooks is not None
            else getattr(selected_transport, "hooks", None)
        )
        self.hook_manager = self.hooks
        self.tool_catalog = (
            selected_catalog
            if selected_catalog is not None
            else getattr(selected_transport, "tool_catalog", None)
        )
        self.catalog = self.tool_catalog
        self.transport = selected_transport
        self.client = selected_transport
        selected_workspace = workspace if isinstance(workspace, Workspace) else None
        self.repo_path = str(
            getattr(selected_transport, "repo_path", "")
            or getattr(selected_workspace, "path", "")
            or (
                repo_path
                if isinstance(repo_path, (str, Path))
                else getattr(repo_path, "path", "")
            )
        )
        self.workspace = selected_workspace
        if selected_workspace is not None:
            manager = getattr(selected_workspace, "manager", None)
            if manager is not None and hasattr(selected_transport, "workspace_manager"):
                selected_transport.workspace_manager = manager
        self._conversation = Conversation(
            self.transport, session_id, agent=self, hooks=self.hooks
        )
        self.session_id = self._conversation.session_id

    @property
    def conversation(self) -> Conversation:
        """Return this agent's stable conversation facade."""
        return self._conversation

    @property
    def conversation_id(self) -> str:
        """Return the agent conversation identifier."""
        return self._conversation.id

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Return public schemas for the kernel's built-in typed tools."""
        from harness.agent_kernel import builtin_tool_specs

        descriptors = []
        for spec in builtin_tool_specs():
            properties = {}
            for name in (*spec.required, *spec.optional):
                expected = spec.types.get(name, str)
                field_type = "string"
                if expected is int:
                    field_type = "integer"
                elif expected is bool:
                    field_type = "boolean"
                elif expected is dict:
                    field_type = "object"
                elif expected is list:
                    field_type = "array"
                properties[name] = {"type": field_type}
            descriptors.append(
                Tool(
                    name=spec.name,
                    description=f"{spec.name} ({spec.side_effect_class})",
                    parameters={
                        "type": "object",
                        "properties": properties,
                        "required": list(spec.required),
                        "additionalProperties": False,
                    },
                    side_effect_class=spec.side_effect_class,
                )
            )
        return tuple(descriptors)

    def list_tools(self) -> list[Tool]:
        """List injected or remote catalog tools without resolving schemas."""
        return self.transport.list_tools()

    def search_tools(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search injected or remote catalog metadata without resolving schemas."""
        if limit is None:
            return self.transport.search_tools(query, max_results)
        return self.transport.search_tools(query, limit=limit)

    def get_tool(self, name: str) -> Tool:
        """Return one exact injected or remote catalog descriptor."""
        return self.transport.get_tool(name)

    def search(
        self, query: str = "", max_results: int = 10, *, limit: int | None = None
    ) -> list[Tool]:
        """Search catalog metadata under the short alias."""
        return self.search_tools(query, max_results, limit=limit)

    def get(self, name: str) -> Tool:
        """Return one exact catalog descriptor under the short alias."""
        return self.get_tool(name)

    def resolve_tool_schema(self, name: str) -> Any:
        """Resolve one exact injected or remote catalog schema on demand."""
        return self.transport.resolve_tool_schema(name)

    def resolve_schema(self, name: str) -> Any:
        """Return one exact catalog schema under the short compatibility name."""
        return self.transport.resolve_tool_schema(name)

    def resolve_tool(self, name: str) -> Any:
        """Resolve one catalog schema under the resolve-tool alias."""
        return self.transport.resolve_tool_schema(name)

    def resolve(self, name: str) -> Any:
        """Resolve one catalog schema under the short resolve alias."""
        return self.transport.resolve_tool_schema(name)

    def _request(self, value: Any, **kwargs: Any) -> RunRequest:

        request = RunRequest.from_value(value, **kwargs)
        if not request.repo_path:
            request.repo_path = self.repo_path
        if not request.session_id:
            request.session_id = self.session_id
        if self.workspace is not None and not request.workspace_id:
            request.workspace_id = self.workspace.id
        return request

    def query(self, request: Any = "", **kwargs: Any) -> Result:
        """Run a synchronous headless query."""
        value = self._request(request, **kwargs)
        return self.transport.query(value)

    def run(
        self, request: Any = "", *, wait: bool | None = None, **kwargs: Any
    ) -> Result | RunHandle:
        """Start a run; synchronous wait returns Result and async wait returns RunHandle."""
        value = self._request(request, **kwargs)
        return self.transport.run(value, wait=wait)

    def stream(
        self,
        request: Any = "",
        *,
        run_id: str = "",
        after_sequence: int = 0,
        **kwargs: Any,
    ) -> Events:
        """Return events for an existing run or start an asynchronous run."""
        if isinstance(request, RunHandle):
            return request.events(after_sequence)
        if run_id:
            return self.transport.events(
                run_id, session_id=self.session_id, after_sequence=after_sequence
            )
        text = str(request or "")
        checker = getattr(self.transport, "has_run", None)
        if text and callable(checker):
            try:
                if checker(text):
                    return self.transport.events(
                        text, session_id=self.session_id, after_sequence=after_sequence
                    )
            except Exception:
                pass
        value = self._request(text or "continue", **kwargs)
        handle = self.transport.run(value, wait=False)
        if isinstance(handle, RunHandle):
            return handle.events(after_sequence)
        result = handle if isinstance(handle, Result) else Result(handle)
        return self.transport.events(
            result.run_result.run_id,
            session_id=self.session_id,
            after_sequence=after_sequence,
        )

    def events(self, run_id: str, *, after_sequence: int = 0) -> Events:
        """Return a public event stream for a selected run."""
        return self.transport.events(
            run_id, session_id=self.session_id, after_sequence=after_sequence
        )

    def cancel(self, run_id: str) -> bool:
        """Cancel a run by its stable identifier."""
        return self.transport.cancel(run_id)

    def replay(self, run_id: str) -> Any:
        """Return the deterministic public replay projection for a run."""
        return self.transport.replay(run_id)

    def resume(
        self, run_id: str, request: str = "", *, wait: bool = True, **kwargs: Any
    ) -> Result | RunHandle:
        """Resume a run through its public checkpoint contract."""
        return self.transport.resume(run_id, request, wait=wait, **kwargs)

    def list_runs(self) -> list[dict[str, Any]]:
        """List known runs using the public status projection."""
        return self.transport.list_runs()

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return this agent conversation's bounded history."""
        return self.conversation.history(limit)

    def close(self) -> None:
        """Close the transport and release active local kernels."""
        self.transport.close()

    def __enter__(self) -> "Agent":
        """Enter an agent context."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Close the agent on context exit."""
        self.close()


class LocalAgent(Agent):
    """Explicit local-only spelling of :class:`Agent`."""

    def __init__(self, repo_path: str | Path | Workspace = "", **kwargs: Any) -> None:
        """Create an Agent backed by LocalTransport."""
        kwargs.pop("base_url", None)
        kwargs.pop("client", None)
        transport_kwargs = {
            key: kwargs.pop(key)
            for key in (
                "log_root",
                "config",
                "model",
                "call_fn",
                "model_callable",
                "model_fn",
                "verifier",
                "workspace_root",
                "session_id",
                "on_event",
                "hooks",
                "hook_manager",
                "tool_catalog",
                "catalog",
            )
            if key in kwargs
        }
        workspace_value = kwargs.get("workspace")
        selected_repo = repo_path or getattr(
            workspace_value, "path", workspace_value or ""
        )
        super().__init__(
            selected_repo,
            transport=LocalTransport(selected_repo, **transport_kwargs),
            **kwargs,
        )


class RemoteAgent(Agent):
    """Explicit remote spelling of :class:`Agent`."""

    def __init__(
        self,
        base_url: str = "",
        repo_path: str | Path = "",
        *,
        token: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        """Create an Agent backed by RemoteTransport."""
        kwargs.pop("client", None)
        transport = kwargs.pop("transport", None)
        remote_hooks = kwargs.get("hook_manager", kwargs.get("hooks"))
        remote_catalog = kwargs.get("tool_catalog", kwargs.get("catalog"))
        super().__init__(
            repo_path,
            transport=transport
            or RemoteTransport(
                base_url,
                token=token or api_key,
                timeout=timeout,
                session_id=str(kwargs.get("session_id", "") or ""),
                hooks=remote_hooks,
                tool_catalog=remote_catalog,
            ),
            token=token,
            **kwargs,
        )
