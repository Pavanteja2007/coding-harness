"""Supervision and reconnect for :class:`agent_sdk.server.AgentServer`.

THE RECORDED GAP (``agent_sdk/AGENTS.md``): *"no supervision/reconnect story
for ``AgentServer``: a crashed server is restarted by whoever launched it."*
Two consequences follow, and both are addressed here:

1. **A server that dies takes the session with it.** A ``serve_forever``
   thread that raises leaves the socket dead while the process looks alive.
   :class:`ServerSupervisor` watches the serving thread and restarts a
   restartable server with bounded backoff and a restart cap.
2. **A client that cannot reconnect cannot tell that failure from a genuine
   one.** :func:`wait_for_health` gives the client a bounded, honest answer:
   the server is back, or it is not, within a stated budget.

COORDINATION NOTE — do not build two
-------------------------------------
``runtime/scheduler.py`` supervises WORKER PROCESSES (spawn / reap / kill /
requeue) — a process-level concern with checkpoints and heartbeats, owned by
T3. This module supervises an IN-PROCESS HTTP server THREAD. They are two
layers of one product, not two implementations of one mechanism: nothing here
spawns a process, and nothing in the scheduler watches a thread.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

__all__ = [
    "SUPERVISION_STATES",
    "ServerSupervisor",
    "SupervisionEvent",
    "SupervisionReport",
    "wait_for_health",
]

#: The closed supervision state vocabulary. A state nobody defined is a state
#: a receipt cannot name.
SUPERVISION_STATES = ("running", "stopped", "exhausted")

#: Backoff between a detected death and a restart attempt, indexed by attempt.
DEFAULT_BACKOFF_S = (0.05, 0.25, 1.0)


@dataclass(frozen=True)
class SupervisionEvent:
    """One thing the supervisor did, with when and why."""

    kind: str
    detail: str
    at: float

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "at": self.at}


@dataclass(frozen=True)
class SupervisionReport:
    """The supervisor's receipt: restarts so far, the event log, the state."""

    restarts: int
    events: Tuple[SupervisionEvent, ...]
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "restarts": self.restarts,
            "state": self.state,
            "events": [event.as_dict() for event in self.events],
        }


class ServerSupervisor:
    """Watch an ``AgentServer``'s serving thread and restart it when it dies.

    Assumes the server is restartable: it owns its transport (the default).
    An injected transport cannot be restarted — ``AgentServer.start`` refuses
    it — so supervising one is refused up front rather than discovered after
    the first crash.

    The watchdog never restarts a server somebody stopped deliberately, and
    it gives up after ``max_restarts`` with state ``exhausted``: a server
    that cannot stay up is a fact, not a retry loop.
    """

    def __init__(
        self,
        server: Any,
        *,
        max_restarts: int = 3,
        backoff_s: Tuple[float, ...] = DEFAULT_BACKOFF_S,
        poll_s: float = 0.1,
        on_event: Optional[Callable[[SupervisionEvent], Any]] = None,
    ) -> None:
        if getattr(server, "_owns_transport", False) is not True:
            raise ValueError(
                "only a server that owns its transport is restartable; an "
                "injected transport is not, by the server's own contract"
            )
        self.server = server
        self.max_restarts = max(0, int(max_restarts))
        self.backoff_s = tuple(float(item) for item in backoff_s) or DEFAULT_BACKOFF_S
        self.poll_s = max(0.01, float(poll_s))
        self.on_event = on_event
        self._events: list[SupervisionEvent] = []
        self._restarts = 0
        self._state = "stopped"
        self._stopping = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        """The current supervision state, from the closed vocabulary."""
        return self._state

    @property
    def restarts(self) -> int:
        """How many times the server has been restarted."""
        return self._restarts

    def report(self) -> SupervisionReport:
        """A snapshot receipt of the supervisor's state."""
        return SupervisionReport(self._restarts, tuple(self._events), self._state)

    def _note(self, kind: str, detail: str) -> None:
        event = SupervisionEvent(kind, detail, time.time())
        self._events.append(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass  # an observability callback must never kill the watchdog

    def start(self) -> "ServerSupervisor":
        """Start the server (if not already serving) and the watchdog."""
        with self._lock:
            if getattr(self.server, "_thread", None) is None:
                self.server.start()
            self._stopping.clear()
            self._state = "running"
            self._note("started", "supervision engaged")
            self._watchdog = threading.Thread(
                target=self._watch, name="neo-agent-server-supervisor", daemon=True
            )
            self._watchdog.start()
            return self

    def stop(self) -> None:
        """Stop the watchdog and the server, in that order."""
        self._stopping.set()
        watchdog = self._watchdog
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=max(1.0, self.poll_s * 4))
        try:
            self.server.stop()
        finally:
            self._state = "stopped"
            self._note("stopped", "supervision disengaged")

    def close(self) -> None:
        """Context-manager-compatible alias for :meth:`stop`."""
        self.stop()

    def __enter__(self) -> "ServerSupervisor":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    # -- the watchdog -------------------------------------------------------

    def _server_thread_dead(self) -> bool:
        thread = getattr(self.server, "_thread", None)
        if thread is None:
            return False
        return not thread.is_alive()

    def _watch(self) -> None:
        while not self._stopping.wait(self.poll_s):
            if getattr(self.server, "_stopped", False):
                self._note("watchdog_exit", "the server was stopped elsewhere")
                return
            if not self._server_thread_dead():
                continue
            if self._restarts >= self.max_restarts:
                self._state = "exhausted"
                self._note(
                    "exhausted",
                    f"the server died {self._restarts + 1} time(s); giving up",
                )
                return
            delay = self.backoff_s[min(self._restarts, len(self.backoff_s) - 1)]
            self._restarts += 1
            self._note("restart", f"serving thread died; restart {self._restarts} in {delay}s")
            if self._stopping.wait(delay):
                return
            try:
                self.server.stop()
            except Exception as exc:
                self._note("stop_failed", f"{type(exc).__name__}: {exc}")
            try:
                self.server.start()
                self._state = "running"
                self._note("restarted", "the server is serving again")
            except Exception as exc:
                self._note("restart_failed", f"{type(exc).__name__}: {exc}")


def wait_for_health(
    url: str,
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.1,
    opener: Any = None,
) -> bool:
    """Bounded reconnect answer: is the server back within the budget?

    Polls ``<url>/health`` until it answers HTTP 200 or the budget is spent.
    Returns a boolean — never raises — because the whole point is that "we
    could not reconnect" must be distinguishable from a genuine failure, and
    an exception says neither. ``opener`` is injectable for tests.
    """
    base = str(url or "").rstrip("/")
    if not base:
        return False
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.01, float(interval_s))

    def _open(request: Any, timeout: float) -> Any:
        if opener is not None:
            return opener(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            request = urllib.request.Request(base + "/health")
            with _open(request, timeout=min(remaining, 5.0)) as response:
                if getattr(response, "status", 200) == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
