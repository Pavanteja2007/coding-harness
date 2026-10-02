"""Transport interfaces and the public asynchronous run handle."""

from __future__ import annotations

from typing import Any, Iterator, Protocol

from .errors import AgentError, RunNotFoundError
from .events import Events
from .models import Result

__all__ = ["AgentTransport", "RunHandle", "Transport"]


class AgentTransport(Protocol):
    """Structural transport contract consumed by Agent and Conversation."""

    def query(self, request: Any, **kwargs: Any) -> Result:
        """Run a synchronous query and return a public result."""

    def run(self, request: Any, **kwargs: Any) -> Result | RunHandle:
        """Start a run synchronously or return an asynchronous handle."""

    def cancel(self, run_id: str) -> bool:
        """Request cancellation for one run."""

    def replay(self, run_id: str) -> Any:
        """Return the deterministic semantic replay projection."""

    def events(
        self, run_id: str, *, session_id: str = "", after_sequence: int = 0
    ) -> Events:
        """Return a replayable event view."""

    def list_runs(self) -> list[dict[str, Any]]:
        """List known public run summaries."""

    def close(self) -> None:
        """Release transport resources."""


class Transport:
    """Small shared base for local and remote implementations."""

    def query(self, request: Any, **kwargs: Any) -> Result:
        """Run a synchronous query."""
        raise NotImplementedError

    def run(self, request: Any, **kwargs: Any) -> Result | RunHandle:
        """Start a run."""
        raise NotImplementedError

    def cancel(self, run_id: str) -> bool:
        """Cancel a run."""
        raise NotImplementedError

    def replay(self, run_id: str) -> Any:
        """Replay a run."""
        raise NotImplementedError

    def events(
        self, run_id: str, *, session_id: str = "", after_sequence: int = 0
    ) -> Events:
        """Return an event view."""
        raise NotImplementedError

    def list_runs(self) -> list[dict[str, Any]]:
        """List run summaries."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the transport."""
        return None


class RunHandle:
    """A transport-neutral handle for an asynchronous run."""

    def __init__(
        self,
        transport: Any,
        run_id: str,
        *,
        session_id: str = "",
        trace_path: str = "",
    ) -> None:
        """Bind a handle to a transport and stable run identity."""
        self.transport = transport
        self.run_id = str(run_id or "")
        self.session_id = str(session_id or "")
        self.trace_path = str(trace_path or "")

    @property
    def id(self) -> str:
        """Return the run identifier under a short alias."""
        return self.run_id

    @property
    def done(self) -> bool:
        """Return whether the run has reached a terminal result."""
        return bool(self.is_terminal())

    @property
    def status(self) -> str:
        """Return the current public status, including running when active."""
        checker = getattr(self.transport, "status", None)
        if callable(checker):
            try:
                return str(checker(self.run_id).get("status", "running"))
            except Exception:
                pass
        return "completed" if self.done else "running"

    def is_terminal(self) -> bool:
        """Return whether the transport reports a terminal run."""
        checker = getattr(self.transport, "is_terminal", None)
        if callable(checker):
            try:
                return bool(checker(self.run_id))
            except Exception:
                return False
        result = self.result
        return result is not None

    def wait(self, timeout: float | None = None) -> Result:
        """Wait for completion and return the canonical result wrapper."""
        waiter = getattr(self.transport, "wait", None)
        if not callable(waiter):
            raise AgentError("transport does not support waiting")
        value = waiter(self.run_id, timeout=timeout)
        return value if isinstance(value, Result) else Result(value)

    def cancel(self) -> bool:
        """Request cancellation directly through the owning transport."""
        canceller = getattr(self.transport, "cancel", None)
        if not callable(canceller):
            return False
        return bool(canceller(self.run_id))

    def events(self, after_sequence: int = 0) -> Events:
        """Return an event view beginning after an exclusive sequence."""
        return Events(
            self.transport,
            self.run_id,
            session_id=self.session_id,
            after_sequence=after_sequence,
        )

    def stream(self, after_sequence: int = 0) -> Iterator[Any]:
        """Iterate over live events from an exclusive sequence."""
        return self.events(after_sequence).iter_after_sequence(wait=True)

    @property
    def result(self) -> Result | None:
        """Return the completed result, or ``None`` while active."""
        getter = getattr(self.transport, "get_result", None)
        if callable(getter):
            try:
                value = getter(self.run_id)
                return (
                    value
                    if isinstance(value, Result) or value is None
                    else Result(value)
                )
            except RunNotFoundError:
                return None
        return None

    def get_result(
        self, wait: bool = True, timeout: float | None = None
    ) -> Result | None:
        """Return a result explicitly, optionally waiting for completion."""
        if wait:
            return self.wait(timeout=timeout)
        return self.result

    def __iter__(self) -> Iterator[Any]:
        """Iterate over this handle's event stream."""
        return self.events().iter_after_sequence(wait=True)

    def __repr__(self) -> str:
        """Return a credential-free handle representation."""
        return f"RunHandle(run_id={self.run_id!r}, status={self.status!r})"
