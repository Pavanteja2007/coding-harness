"""Render-loop safety for the streaming TUI (VEX-CEILING-10, part 2).

A terminal agent that streams has exactly one dangerous property: the
frame path is the thing that must never get slow. If a repaint does
I/O, waits on a lock the writer holds, or scales with the token rate, the
UI becomes the bottleneck and the run looks hung.

This module is the pure, testable core of that guarantee. It is a
**projection over the event journal**, never over captured stdout: it
consumes the same ``model_delta`` / ``model_request`` / ``model_response``
/ ``tool_call`` rows the harness writes, and it writes nothing.

The four guarantees, each independently checkable:

1. **Phase-typed state.** :class:`RunPhase` distinguishes *waiting for
   first token* from *thinking* from *streaming* from *executing a tool*.
   A slow endpoint is visible as one specific state, not as a spinner
   that means everything.
2. **Coalescing.** :class:`StreamCoalescer` folds N deltas into at most
   one render per window (40-500ms). Frame count is a function of wall
   time, not token rate — ``frame_cost_receipt()`` reports the measured
   ratio so "1 event/token and 1 event/100 tokens cost the same" is an
   assertion, not a claim.
3. **Bounded text.** Live text is capped (:data:`DEFAULT_MAX_LIVE_CHARS`)
   with an explicit tail marker, so a 200k-token answer cannot make the
   run-line repaint a 200k-character string every frame.
4. **Control bypass.** Cancellation, phase changes, and errors do not
   queue behind content: they land in a separate lane that
   :meth:`StreamCoalescer.poll` always returns first, so an urgent message
   is never one coalescing window behind a flood of text.

Nothing in this module touches the filesystem, the network, or a
subprocess. The frame path calls :meth:`StreamCoalescer.poll` and
renders; that is the whole contract.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

#: Coalescing window bounds in milliseconds. Matches
#: ``runtime.streaming``'s producer window; duplicated as literals so the
#: projection stays importable with no runtime package present.
MIN_WINDOW_MS = 40
MAX_WINDOW_MS = 500

#: Default window: fast enough that text feels live, slow enough that a
#: token storm costs one repaint instead of hundreds.
DEFAULT_WINDOW_MS = 60

#: Bounded live text. Past this the tail is kept and the head is replaced
#: by an explicit marker, so the run-line stays a fixed cost no matter how
#: long the answer gets.
DEFAULT_MAX_LIVE_CHARS = 4000

#: Live text is rendered as at most this many trailing lines.
DEFAULT_MAX_LIVE_LINES = 12

#: Head marker emitted when live text is trimmed.
TRIM_MARKER = "... (earlier output trimmed) "

#: How long a "waiting for first token" state stays interesting before it
#: escalates its own wording. A slow endpoint and a hung endpoint look
#: identical until you say how long you have been waiting.
SLOW_FIRST_TOKEN_S = 8.0

#: How long a tool has been running before the label says so.
SLOW_TOOL_S = 20.0


class RunPhase(str, Enum):
    """The phase a run is in, as the frame path must distinguish it.

    ``str``-valued so a renderer can interpolate it directly and a test
    can compare against the literal the UI shows.
    """

    IDLE = "idle"
    STARTING = "starting"
    #: A model request is in flight and no text has come back yet. This is
    #: the state that distinguishes "the provider is slow" from "the
    #: process is wedged" — and it is the state a slow endpoint must show.
    AWAITING_FIRST_TOKEN = "waiting for first token"
    #: The provider answered with reasoning but no visible text yet.
    THINKING = "thinking"
    #: Text is arriving.
    STREAMING = "streaming"
    #: A tool is running.
    TOOL = "executing tool"
    CANCELLING = "cancel requested"
    DONE = "done"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class PhaseState:
    """One immutable frame-state fact."""

    phase: RunPhase
    detail: str = ""
    elapsed_s: float = 0.0
    slow: bool = False

    def label(self) -> str:
        """The exact string a renderer should show for this state."""
        if not self.detail:
            return self.phase.value
        if self.slow:
            return f"{self.phase.value} · {self.detail} · still working"
        return f"{self.phase.value} · {self.detail}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.value,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 3),
            "slow": self.slow,
        }


@dataclass
class _Control:
    """A message that must not wait behind content."""

    text: str
    level: str
    at: float


@dataclass
class StreamCoalescer:
    """Fold journal stream events into at most one render per window.

    ``poll(now)`` is the ONLY method the frame path calls. It is pure with
    respect to I/O: no filesystem, no network, no subprocess, and no sleep.
    It returns the control lane first (so an urgent message is never one
    window late), then at most one content payload.
    """

    window_ms: int = DEFAULT_WINDOW_MS
    max_live_chars: int = DEFAULT_MAX_LIVE_CHARS
    max_live_lines: int = DEFAULT_MAX_LIVE_LINES
    clock: Callable[[], float] = time.monotonic

    _text_parts: List[str] = field(default_factory=list, init=False)
    _live_chars: int = field(default=0, init=False)
    _trimmed: bool = field(default=False, init=False)
    _window_started: Optional[float] = field(default=None, init=False)
    _dirty: bool = field(default=False, init=False)
    _control: Deque[_Control] = field(default_factory=deque, init=False)
    _events: int = field(default=0, init=False)
    _frames: int = field(default=0, init=False)
    _frames_with_content: int = field(default=0, init=False)
    _last_rendered: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.window_ms = max(
            MIN_WINDOW_MS, min(int(self.window_ms or DEFAULT_WINDOW_MS), MAX_WINDOW_MS)
        )
        self.max_live_chars = max(
            200, int(self.max_live_chars or DEFAULT_MAX_LIVE_CHARS)
        )
        self.max_live_lines = max(1, int(self.max_live_lines or DEFAULT_MAX_LIVE_LINES))

    # -- ingest (called from the journal tail thread, not the frame path)

    def push_delta(self, delta: str) -> None:
        """Record one ``model_delta``. Never raises, never blocks."""
        if not delta:
            return
        self._events += 1
        self._dirty = True
        self._text_parts.append(str(delta))
        self._live_chars += len(str(delta))
        while self._live_chars > self.max_live_chars and len(self._text_parts) > 1:
            dropped = self._text_parts.pop(0)
            self._live_chars -= len(dropped)
            self._trimmed = True

    def push_control(self, text: str, level: str = "info") -> None:
        """Record a control message (cancel, phase change, error).

        Control messages bypass the content queue entirely: they are held
        in their own lane and always returned ahead of pending text, so a
        cancellation is never stuck behind a thousand tokens.
        """
        if not text:
            return
        self._control.append(_Control(str(text), str(level), self.clock()))
        while len(self._control) > 32:
            self._control.popleft()

    def mark_frame(self) -> None:
        """Record that a frame was rendered. Used by the cost receipt."""
        self._frames += 1
        if self._dirty:
            self._frames_with_content += 1
            self._dirty = False
            self._window_started = self.clock()

    def reset(self) -> None:
        """Clear content but keep the control lane.

        A trace rotation or a new model call must not drop a pending
        cancellation: control is a different class of message.
        """
        self._text_parts.clear()
        self._live_chars = 0
        self._trimmed = False
        self._dirty = False
        self._window_started = None
        self._last_rendered = ""

    def reset_all(self) -> None:
        """Clear content AND control — a genuinely new run."""
        self.reset()
        self._control.clear()
        self._events = 0
        self._frames = 0
        self._frames_with_content = 0

    # -- frame path (pure, no I/O)

    def due(self, now: Optional[float] = None) -> bool:
        """Whether a content frame is due at ``now``."""
        if not self._dirty:
            return False
        if self._window_started is None:
            return True
        moment = self.clock() if now is None else now
        return (moment - self._window_started) * 1000.0 >= self.window_ms

    def poll(self, now: Optional[float] = None) -> Optional[str]:
        """Return the payload to render, or ``None`` for a no-op frame.

        A control message wins outright. Content is returned only when a
        coalescing window has closed, which is what makes frame cost
        independent of token rate.
        """
        self._frames += 1
        if self._control:
            return self._control.popleft().text
        if not self.due(now):
            return None
        self._frames_with_content += 1
        self._dirty = False
        self._window_started = self.clock()
        payload = self.live_text()
        if payload == self._last_rendered:
            return None
        self._last_rendered = payload
        return payload

    def live_text(self) -> str:
        """The bounded live text, as a renderer should show it."""
        body = "".join(self._text_parts)
        if self._trimmed:
            body = TRIM_MARKER + body[-self.max_live_chars :]
        lines = body.splitlines() or [""]
        if len(lines) > self.max_live_lines:
            body = "\n".join(lines[-self.max_live_lines :])
        return body

    def pending_control(self) -> List[str]:
        """Drain the control lane (used by a repaint that is not a poll)."""
        drained: List[str] = []
        while self._control:
            drained.append(self._control.popleft().text)
        return drained

    # -- measured receipt

    @property
    def events(self) -> int:
        """Delta events observed."""
        return self._events

    @property
    def frames(self) -> int:
        """``poll()`` calls, content or not."""
        return self._frames

    def frame_cost_receipt(self) -> Dict[str, Any]:
        """The measurement behind "frame cost is token-rate independent".

        ``events_per_frame`` is the ratio that must stay roughly constant
        as the token rate changes. ``events_per_content_frame`` is the
        coalescing win. A zero event count reports ``None`` rather than a
        fabricated ``0.0``, so a vacuous measurement cannot be read as a
        pass.
        """
        content_frames = max(1, self._frames_with_content)
        return {
            "events": self._events,
            "frames": self._frames,
            "content_frames": self._frames_with_content,
            "window_ms": self.window_ms,
            "events_per_frame": (
                round(self._events / float(self._frames), 4) if self._frames else None
            ),
            "events_per_content_frame": (
                round(self._events / float(content_frames), 4) if self._events else None
            ),
            "control_pending": len(self._control),
            "live_chars": self._live_chars,
            "trimmed": self._trimmed,
        }


# ---------------------------------------------------------------------------
# Phase projection over the journal
# ---------------------------------------------------------------------------


@dataclass
class PhaseProjector:
    """Fold journal rows into a :class:`PhaseState`.

    This is the "a slow endpoint shows a phase-specific state" mechanism.
    It is deliberately separate from :class:`StreamCoalescer`: the
    coalescer owns the *text* lane, the projector owns the *state* lane,
    and a control message that changes the phase is delivered even while
    the text lane is mid-window.
    """

    slow_first_token_s: float = SLOW_FIRST_TOKEN_S
    slow_tool_s: float = SLOW_TOOL_S
    clock: Callable[[], float] = time.monotonic

    _phase: RunPhase = field(default=RunPhase.IDLE, init=False)
    _detail: str = field(default="", init=False)
    _entered_at: float = field(default_factory=time.monotonic, init=False)
    _request_started: Optional[float] = field(default=None, init=False)
    _tool_started: Optional[float] = field(default=None, init=False)
    _has_text: bool = field(default=False, init=False)

    def consume(
        self, kind: str, data: Optional[Dict[str, Any]] = None
    ) -> Optional[PhaseState]:
        """Fold one journal row. Returns a state only when it changed."""
        data = data or {}
        before = (self._phase, self._detail)
        self._apply(kind, data)
        if (self._phase, self._detail) == before:
            return None
        return self.state()

    def _transition(self, phase: RunPhase, detail: str = "") -> None:
        if phase is not self._phase or detail != self._detail:
            self._entered_at = self.clock()
        self._phase = phase
        self._detail = detail

    def _apply(self, kind: str, data: Dict[str, Any]) -> None:
        if kind in ("task_start", "run_started"):
            self._request_started = None
            self._tool_started = None
            self._has_text = False
            self._transition(RunPhase.STARTING, str(data.get("mode") or ""))
        elif kind == "model_request":
            now = self.clock()
            self._request_started = now
            self._tool_started = None
            self._has_text = False
            self._transition(RunPhase.AWAITING_FIRST_TOKEN, str(data.get("step") or ""))
        elif kind in ("model_delta", "response_delta", "text_delta"):
            now = self.clock()
            if self._has_text:
                self._request_started = self._request_started or now
            else:
                self._has_text = True
            self._transition(RunPhase.STREAMING, str(data.get("step") or ""))
        elif kind == "model_response":
            self._request_started = None
            self._has_text = False
            if self._phase is not RunPhase.CANCELLING:
                self._transition(RunPhase.THINKING, "")
        elif kind in ("tool_call", "tool_started"):
            now = self.clock()
            self._tool_started = now
            command = str(data.get("command") or "").strip()
            self._transition(RunPhase.TOOL, command[:80])
        elif kind in ("tool_result", "tool_completed", "tool_error"):
            self._tool_started = None
            if self._phase is not RunPhase.CANCELLING:
                self._transition(RunPhase.THINKING, "")
        elif kind in ("cancellation_requested", "cancel_requested"):
            self._transition(RunPhase.CANCELLING, "")
        elif kind in ("task_end", "run_finished"):
            status = str(data.get("status") or "")
            failed = status in ("failed", "error", "timeout", "cancelled")
            self._transition(RunPhase.FAILED if failed else RunPhase.DONE, status)

    def state(self) -> PhaseState:
        """The current state, with the slow-path wording applied."""
        now = self.clock()
        elapsed = max(0.0, now - self._entered_at)
        if self._phase is RunPhase.AWAITING_FIRST_TOKEN and self._request_started:
            waiting = now - self._request_started
            if waiting >= self.slow_first_token_s:
                return PhaseState(
                    phase=RunPhase.AWAITING_FIRST_TOKEN,
                    detail=f"no token after {int(waiting)}s",
                    elapsed_s=elapsed,
                    slow=True,
                )
        if self._phase is RunPhase.TOOL and self._tool_started:
            running = now - self._tool_started
            if running >= self.slow_tool_s:
                return PhaseState(
                    phase=RunPhase.TOOL,
                    detail=f"{self._detail} · {int(running)}s".strip(" ·"),
                    elapsed_s=elapsed,
                    slow=True,
                )
        return PhaseState(
            phase=self._phase, detail=self._detail, elapsed_s=elapsed, slow=False
        )

    def cancel(self) -> PhaseState:
        """Force the cancelling phase (a `/cancel` that has not landed yet)."""
        self._transition(RunPhase.CANCELLING, "")
        return self.state()

    def reset(self) -> None:
        """Return to idle (trace rotation / reconnect)."""
        self._request_started = None
        self._tool_started = None
        self._has_text = False
        self._transition(RunPhase.IDLE, "")


def data_step_safe(projector: "PhaseProjector") -> str:
    """Return the projector's current detail without raising.

    Exists so a state builder that reads its own fields can never crash
    the frame path — a state builder that can raise is worse than no
    state.
    """
    try:
        return projector._detail or ""
    except Exception:  # pragma: no cover - defensive
        return ""


# ---------------------------------------------------------------------------
# Frame-cost equivalence measurement
# ---------------------------------------------------------------------------


def frame_cost_receipt(
    coalescer: StreamCoalescer, now: Optional[float] = None
) -> Dict[str, Any]:
    """Convenience wrapper: the receipt plus a forced window close.

    Used by the equivalence check so both arms are measured at the same
    point (end of stream) rather than at whatever frame the clock
    happened to land on.
    """
    coalescer.mark_frame()
    payload = coalescer.poll(now)
    receipt = coalescer.frame_cost_receipt()
    receipt["last_payload_chars"] = len(payload or "")
    return receipt


def frames_allowed(span_s: float, window_ms: int) -> int:
    """The most content frames a stream of ``span_s`` seconds can cost.

    One frame per coalescing window plus one for the trailing window. This
    is the ceiling the frame path must respect, and it is derived from
    WALL CLOCK only — which is exactly why it is the right thing to
    compare across token rates.
    """
    window_s = max(1, int(window_ms)) / 1000.0
    if span_s <= 0:
        return 1
    return int(span_s / window_s) + 1


def equivalent_frame_cost(
    dense: Dict[str, Any],
    sparse: Dict[str, Any],
    *,
    span_s: float = 1.0,
    tolerance: int = 1,
) -> Dict[str, Any]:
    """Compare two measured receipts for token-rate-independent frame cost.

    ``dense`` is one event per token, ``sparse`` one event per 100 tokens,
    over the same wall-clock span. The claim under test is **not** that the
    two arms produce the same number of frames — a genuinely slow stream
    legitimately produces fewer, and that is the coalescing working. The
    claim is that *neither* arm's frame count scales with its event count:
    both stay under the wall-clock bound. When either arm exceeds the
    bound, the coalescing is not doing its job and this returns
    ``equal: False``.
    """
    dense_frames = int(dense.get("content_frames") or 0)
    sparse_frames = int(sparse.get("content_frames") or 0)
    dense_events = int(dense.get("events") or 0)
    sparse_events = int(sparse.get("events") or 0)
    window = int(dense.get("window_ms") or DEFAULT_WINDOW_MS)
    allowed = frames_allowed(span_s, window) + tolerance
    dense_ok = 0 < dense_frames <= allowed
    sparse_ok = 0 < sparse_frames <= allowed
    dense_ratio = round(dense_frames / float(dense_events), 4) if dense_events else None
    sparse_ratio = (
        round(sparse_frames / float(sparse_events), 4) if sparse_events else None
    )
    return {
        "equal": dense_ok and sparse_ok,
        "frames_allowed": allowed,
        "dense_within_bound": dense_ok,
        "sparse_within_bound": sparse_ok,
        "frames_per_event_dense": dense_ratio,
        "frames_per_event_sparse": sparse_ratio,
        "coalescing_ratio": (
            round(dense_ratio / sparse_ratio, 4)
            if dense_ratio and sparse_ratio and sparse_ratio
            else None
        ),
        "dense": dense,
        "sparse": sparse,
        "reason": (
            ""
            if dense_ok and sparse_ok
            else "a content-frame count exceeded the wall-clock bound: frame cost "
            "is tracking the token rate"
        ),
    }


class _SimClock:
    """A monotonic clock a simulation advances explicitly.

    The loop below owns time; the coalescer only reads it. That is what
    makes the frame-cost equivalence measurement deterministic: the same
    wall-clock span yields the same frame count no matter how many deltas
    were pushed into it.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def simulate_stream(
    *,
    tokens: int,
    window_ms: int = DEFAULT_WINDOW_MS,
    span_s: float = 1.0,
    max_live_chars: int = DEFAULT_MAX_LIVE_CHARS,
) -> StreamCoalescer:
    """Drive a coalescer with ``tokens`` deltas over ``span_s`` seconds.

    The frame path is polled once per simulated frame tick, so the number
    of frames a stream costs is a property of the WALL CLOCK and the
    coalescing window, never of the token count. This is the mechanism the
    equivalence measurement checks, and it performs no I/O.
    """
    clock = _SimClock()
    coalescer = StreamCoalescer(
        window_ms=window_ms, max_live_chars=max_live_chars, clock=clock
    )
    if tokens <= 0:
        coalescer.poll(clock.now)
        return coalescer
    step = span_s / float(tokens)
    for _ in range(tokens):
        clock.advance(step)
        coalescer.push_delta("x")
        coalescer.poll(clock.now)
    # A final poll at the end of the span closes the trailing window.
    coalescer.poll(clock.now)
    return coalescer


# ===========================================================================
# VEX-PF-04 — role hierarchy, inline thinking, inline undo
# ===========================================================================
#
# Everything below turns a journal row into something a person can read at a
# glance. Four rules shape it, and each of them exists because the thing it
# prevents actually shipped once:
#
# 1. A ROLE, not an event name. The product leaked the literal string
#    "unknown event: model_delta" past a user's eyes, because a renderer
#    fell back to printing the event kind when it did not recognise a row.
#    :func:`role_for_event` is total and its table is closed, so there is no
#    code path that can reach an event name. An unrecognised row becomes a
#    ``SYSTEM`` block with a human label, and the receipt counts it.
#
# 2. STRUCTURE, not hue. Colour-blind readers, ``NO_COLOR`` terminals and a
#    16-colour palette all collapse hue. Indentation, a glyph, weight and
#    slant do not: :attr:`RoleSpec.silhouette` is the structural signature and
#    every pair of roles differs in it, which is the assertion the suite
#    makes.
#
# 3. FRAME COST IS NOT A FUNCTION OF TOKEN COUNT. The thinking lane reuses
#    the coalescer this module already owns, so the existing
#    :func:`frame_cost_receipt` / :func:`equivalent_frame_cost` /
#    :func:`frames_allowed` are the gate rather than a second set of
#    numbers nobody compares.
#
# 4. NOTHING IS SILENTLY DROPPED. A bound is MARKED ("+3 more files",
#    "... (earlier output trimmed)", "cancelled - kept partial text"), and a
#    measurement that observed nothing says so instead of reporting a
#    healthy zero.


class Role(str, Enum):
    """What a block IS, independent of what colour it wears.

    Six roles a reader must tell apart, plus ``SYSTEM`` for lifecycle and
    receipt rows. The seventh is not decoration: it is where a run's
    structural rows go, so they stop competing with content for the eye.
    """

    THINKING = "thinking"
    ACTION = "action"
    RESULT = "result"
    EDIT = "edit"
    NEEDS_YOU = "needs you"
    FAILURE = "failure"
    SYSTEM = "system"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: The role vocabulary, declared so "add a role" is an edit somebody makes.
ROLE_NAMES: tuple = tuple(role.value for role in Role)


@dataclass(frozen=True)
class RoleSpec:
    """The structural silhouette of one role.

    ``silhouette`` is the part that survives ``NO_COLOR``, an 8-colour
    terminal and colour-blind vision: indentation, the glyph, the weight
    and the slant. It is declared here and asserted pairwise-distinct by the
    suite, so "every role looks different" is a check rather than a hope.
    """

    role: Role
    label: str
    glyph: str
    ascii_glyph: str
    indent: int
    bold: bool
    italic: bool
    muted: bool
    #: Whether a reader can fold this role's detail away and get it back.
    expandable: bool
    #: Whether a reader can fold the whole role away while it is live.
    collapsible: bool
    #: Whether the role is its own horizontal band rather than a line.
    band: bool
    #: The token NAME a renderer resolves to a real style. A string, not a
    #: style object: this module stays free of any presentation import.
    style_token: str
    #: What a screen reader hears. Never empty, so a role is never silent.
    spoken: str

    @property
    def silhouette(self) -> tuple:
        """The colour-free signature of this role."""
        return (self.indent, self.glyph, self.bold, self.italic)

    def prefix(self, *, ascii_only: bool = False) -> str:
        """The leading columns: indent, then the glyph. Plain text."""
        mark = self.ascii_glyph if ascii_only else self.glyph
        return ("  " * self.indent) + f"{mark} "


ROLE_SPECS: Dict[str, RoleSpec] = {
    spec.role.value: spec
    for spec in (
        RoleSpec(
            role=Role.THINKING,
            label="thinking",
            glyph="~",
            ascii_glyph="~",
            indent=1,
            bold=False,
            italic=True,
            muted=True,
            expandable=True,
            collapsible=True,
            band=False,
            style_token="text_secondary",
            spoken="the model is thinking",
        ),
        RoleSpec(
            role=Role.ACTION,
            label="action",
            glyph=">",
            ascii_glyph=">",
            indent=0,
            bold=True,
            italic=False,
            muted=False,
            expandable=True,
            collapsible=False,
            band=False,
            style_token="accent_text",
            spoken="action taken",
        ),
        RoleSpec(
            role=Role.RESULT,
            label="result",
            glyph="=",
            ascii_glyph="=",
            indent=1,
            bold=False,
            italic=False,
            muted=True,
            expandable=True,
            collapsible=False,
            band=False,
            style_token="text_secondary",
            spoken="result",
        ),
        RoleSpec(
            role=Role.EDIT,
            label="edit",
            glyph="~",
            ascii_glyph="~",
            indent=0,
            bold=True,
            italic=False,
            muted=False,
            expandable=True,
            collapsible=False,
            band=True,
            style_token="text_primary",
            spoken="a file was edited",
        ),
        RoleSpec(
            role=Role.NEEDS_YOU,
            label="needs you",
            glyph="?",
            ascii_glyph="?",
            indent=0,
            bold=True,
            italic=False,
            muted=False,
            expandable=True,
            collapsible=False,
            band=True,
            style_token="warning",
            spoken="waiting for your decision",
        ),
        RoleSpec(
            role=Role.FAILURE,
            label="failure",
            glyph="x",
            ascii_glyph="x",
            indent=0,
            bold=True,
            italic=False,
            muted=False,
            expandable=True,
            collapsible=False,
            band=True,
            style_token="error",
            spoken="something failed",
        ),
        RoleSpec(
            role=Role.SYSTEM,
            label="",
            glyph="-",
            ascii_glyph="-",
            indent=0,
            bold=False,
            italic=False,
            muted=True,
            expandable=False,
            collapsible=False,
            band=False,
            style_token="text_disabled",
            spoken="progress",
        ),
    )
}

#: The one authority for "what is this row". Closed, total, and keyed on the
#: same journal names ``cli.runview.EVENT_VOCABULARY`` already declares, so a
#: surface cannot classify one row two ways. An entry missing from this table
#: is a ``SYSTEM`` row, not a printed event name - the failure mode this
#: table exists to delete.
_EVENT_ROLES: Dict[str, Role] = {
    # thinking: the model explaining itself, live
    "model_delta": Role.THINKING,
    "response_delta": Role.THINKING,
    "text_delta": Role.THINKING,
    "reasoning_summary": Role.THINKING,
    "reasoning": Role.THINKING,
    "thinking": Role.THINKING,
    "model_response": Role.THINKING,
    "model_completed": Role.THINKING,
    "model_request": Role.THINKING,
    "model_started": Role.THINKING,
    "turn_started": Role.THINKING,
    "plan": Role.THINKING,
    "project_plan_generated": Role.THINKING,
    "project_plan_saved": Role.THINKING,
    # action: something was done
    "tool_call": Role.ACTION,
    "batch_call": Role.ACTION,
    "tool_started": Role.ACTION,
    "file_read": Role.ACTION,
    "read_file": Role.ACTION,
    "qa_read": Role.ACTION,
    "recall": Role.ACTION,
    "docs_lookup": Role.ACTION,
    "file_search": Role.ACTION,
    "search_files": Role.ACTION,
    "read_symbol": Role.ACTION,
    "web_fetch": Role.ACTION,
    "subagent_started": Role.ACTION,
    "subagent_start": Role.ACTION,
    "child_started": Role.ACTION,
    "spawn": Role.ACTION,
    "project_sub_task_start": Role.ACTION,
    "skills": Role.ACTION,
    "skill_model_content": Role.ACTION,
    "retrieval": Role.ACTION,
    "retrieval_truncated": Role.ACTION,
    "decision_memory": Role.ACTION,
    "mcp_catalog": Role.ACTION,
    "session_context": Role.ACTION,
    "checkpoint_saved": Role.ACTION,
    "checkpoint": Role.ACTION,
    "project_checkpoint": Role.ACTION,
    "checkpoint_warning": Role.ACTION,
    # result: what came back
    "tool_result": Role.RESULT,
    "tool_completed": Role.RESULT,
    "tool_result_detail": Role.RESULT,
    "command_output": Role.RESULT,
    "process_output": Role.RESULT,
    "sandbox_result": Role.RESULT,
    "subagent_finished": Role.RESULT,
    "subagent_end": Role.RESULT,
    "child_finished": Role.RESULT,
    "spawn_error": Role.RESULT,
    "project_sub_task_end": Role.RESULT,
    "project_sub_task_already_passing": Role.RESULT,
    "context_built": Role.RESULT,
    "context_compiled": Role.RESULT,
    "context_compaction_restored": Role.RESULT,
    "knowledge_bound": Role.RESULT,
    "knowledge_close": Role.RESULT,
    "turn_recorded": Role.RESULT,
    # edit: a file changed
    "edit_applied": Role.EDIT,
    "file_changed": Role.EDIT,
    "project_file_changed": Role.EDIT,
    # needs you: the run cannot continue without a decision
    "approval_required": Role.NEEDS_YOU,
    "input_requested": Role.NEEDS_YOU,
    "permission_decision": Role.NEEDS_YOU,
    "approval_decided": Role.NEEDS_YOU,
    "approval_denied": Role.NEEDS_YOU,
    "question_asked": Role.NEEDS_YOU,
    "question_pending": Role.NEEDS_YOU,
    "unresolved_question": Role.NEEDS_YOU,
    # failure: something went wrong
    "error": Role.FAILURE,
    "run_error": Role.FAILURE,
    "worker_error": Role.FAILURE,
    "child_error": Role.FAILURE,
    "retry": Role.FAILURE,
    "model_retry": Role.FAILURE,
    "model_recovery": Role.FAILURE,
    "attempt_retry": Role.FAILURE,
    "crash_retry": Role.FAILURE,
    "model_error": Role.FAILURE,
    "hang_timeout": Role.FAILURE,
    "kill_requeue": Role.FAILURE,
    "kill_exhausted": Role.FAILURE,
    "approval_error": Role.FAILURE,
    "wallclock_timeout": Role.FAILURE,
    "context_warning": Role.FAILURE,
    "context_rewind": Role.FAILURE,
    "context_rewind_warning": Role.FAILURE,
    "turn_ledger_warning": Role.FAILURE,
    "knowledge_close_failed": Role.FAILURE,
    "tool_error": Role.FAILURE,
    # system: lifecycle, and anything this table has not named
    "task_start": Role.SYSTEM,
    "run_started": Role.SYSTEM,
    "run_start": Role.SYSTEM,
    "project_start": Role.SYSTEM,
    "phase_changed": Role.SYSTEM,
    "phase_change": Role.SYSTEM,
    "state_change": Role.SYSTEM,
    "attempt_start": Role.SYSTEM,
    "step_end": Role.SYSTEM,
    "step_skipped_resume": Role.SYSTEM,
    "task_end": Role.SYSTEM,
    "run_finished": Role.SYSTEM,
    "run_finish": Role.SYSTEM,
    "project_end": Role.SYSTEM,
    "result": Role.SYSTEM,
    "stop": Role.SYSTEM,
    "finish": Role.SYSTEM,
    "completion_decision": Role.SYSTEM,
    "cancellation_requested": Role.SYSTEM,
    "cancel_requested": Role.SYSTEM,
    "steering": Role.SYSTEM,
    "steering_abort": Role.SYSTEM,
    "steering_replan": Role.SYSTEM,
    "steering_step_yield": Role.SYSTEM,
    "execution_backend_ready": Role.SYSTEM,
    "project_criteria_extracted": Role.SYSTEM,
}


def role_for_event(kind: Any, data: Optional[Dict[str, Any]] = None) -> Role:
    """The role of one journal row. Total, and never returns a name.

    A row this table has not seen becomes :attr:`Role.SYSTEM` rather than
    being dropped, because a silently dropped row is a lie about what the
    run did and a printed event name is the defect this module exists to
    remove. The payload override is small and explicit: a receipt that
    says it FAILED is a failure even when its kind is a receipt.
    """
    name = str(kind or "").strip().lower()
    role = _EVENT_ROLES.get(name)
    if role is not None:
        return role
    payload = data if isinstance(data, Mapping) else {}
    if str(payload.get("status") or "").lower() in ("failed", "error", "timeout"):
        return Role.FAILURE
    return Role.SYSTEM


def role_spec(role: Any) -> RoleSpec:
    """The structural spec for a role, defaulting to ``system``."""
    key = role.value if isinstance(role, Role) else str(role or "").strip().lower()
    return ROLE_SPECS.get(key, ROLE_SPECS[Role.SYSTEM.value])


def role_silhouettes() -> Dict[str, tuple]:
    """``{role: silhouette}`` for every role. The NO_COLOR contract."""
    return {name: spec.silhouette for name, spec in ROLE_SPECS.items()}


# ---------------------------------------------------------------------------
# Role blocks
# ---------------------------------------------------------------------------


@dataclass
class RoleBlock:
    """One readable unit in the transcript.

    Carries a role, a human LABEL and a DETAIL, and never an event kind.
    ``collapsed`` and the omission counts exist so a bound is visible: a
    reader must never have to guess whether a row is short or cut.
    """

    role: Role
    label: str
    detail: str = ""
    body: List[str] = field(default_factory=list)
    meta: List[str] = field(default_factory=list)
    timestamp: str = ""
    username: str = ""
    collapsed: bool = False
    hidden_lines: int = 0
    truncated: bool = False
    step: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            self.role = Role(str(self.role or "system"))
        self.label = str(self.label or role_spec(self.role).spoken)
        self.detail = str(self.detail or "")
        self.body = [str(line) for line in (self.body or [])]
        self.meta = [str(line) for line in (self.meta or [])]
        self.timestamp = str(self.timestamp or "")
        self.username = str(self.username or "")

    @property
    def spec(self) -> RoleSpec:
        return role_spec(self.role)

    @property
    def empty(self) -> bool:
        """Whether this block says nothing a reader could read."""
        return not (self.label or self.detail or self.body or self.meta)

    def plain_lines(self, *, ascii_only: bool = False) -> List[str]:
        """The block as plain text lines. Never markup, by construction.

        Every string in here came from a journal row, a model reply or a
        filesystem path, so a caller may hand the result to a renderer
        that escapes it and must never hand it to one that does not.
        """
        spec = self.spec
        head = spec.prefix(ascii_only=ascii_only) + self.label
        if self.detail:
            head = f"{head} - {self.detail}"
        lines = [head]
        if self.username:
            lines.append("  from " + self.username)
        if self.collapsed:
            shown = self.body[:1]
            lines.extend("  " + line for line in shown)
            if len(self.body) > 1:
                lines.append(f"  +{len(self.body) - 1} more lines (folded)")
            return lines
        for line in self.body:
            lines.append("  " + line)
        for line in self.meta:
            lines.append("  " + line)
        if self.truncated:
            lines.append("  ... output cut here")
        return lines

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role.value,
            "label": self.label,
            "detail": self.detail,
            "lines": len(self.body),
            "meta": list(self.meta),
            "collapsed": self.collapsed,
            "hidden_lines": self.hidden_lines,
            "truncated": self.truncated,
            "silhouette": list(self.spec.silhouette),
        }


#: Hard bounds on a block's body. Module constants, not config values: a
#: value in a table merged into every task is a value whoever edits the
#: table can make arbitrarily large.
MAX_BODY_LINES = 40
MAX_DETAIL_CHARS = 240
MAX_FILES_LISTED = 12


def _clip(text: Any, limit: int = MAX_DETAIL_CHARS) -> Tuple[str, bool]:
    """Bound a detail string, returning ``(text, was_cut)``."""
    raw = "" if text is None else (text if isinstance(text, str) else str(text))
    clean = " ".join(raw.split())
    if len(clean) <= limit:
        return (clean, False)
    return (clean[: max(0, limit - 1)].rstrip() + "…", True)


def _action_label(data: Mapping[str, Any]) -> Tuple[str, str]:
    """A human phrase for an action row, and its subject.

    The wording is a fixed vocabulary, never the tool's own name, so a
    journal row cannot choose how it reads in the transcript. An
    unrecognised tool becomes "using a tool" rather than echoing whatever
    the producer called it.
    """
    tool = str(data.get("tool") or data.get("name") or "").strip().lower()
    args = data.get("arguments")
    if not isinstance(args, Mapping):
        args = data.get("args") if isinstance(data.get("args"), Mapping) else {}
    args = args if isinstance(args, Mapping) else {}
    target = str(
        args.get("path")
        or args.get("pattern")
        or args.get("query")
        or args.get("command")
        or data.get("command")
        or data.get("target")
        or ""
    )
    verbs = {
        "read": "reading",
        "glob": "searching",
        "grep": "searching",
        "search": "searching",
        "bash": "running",
        "shell": "running",
        "test": "running",
        "edit": "editing",
        "write": "editing",
        "apply_patch": "editing",
        "mcp_call": "calling an MCP tool",
        "memory": "recalling project memory",
        "verify": "verifying",
        "fetch": "fetching reference",
        "plan": "planning",
        "done": "finishing",
        "finish": "finishing",
    }
    if tool == "mcp":
        return ("calling an MCP tool", _clip(target)[0])
    verb = verbs.get(tool, "using a tool")
    if verb in ("using a tool", "verifying", "planning", "finishing"):
        return (verb, _clip(target)[0])
    return (verb if target else f"{verb} a file", _clip(target)[0])


def _failure_reason(data: Mapping[str, Any]) -> str:
    """Why something failed, in words, with a reason when one exists."""
    for key in ("reason", "error", "message", "summary", "detail"):
        value = data.get(key)
        if value:
            return _clip(value)[0]
    return "no reason reported"


def _result_body(data: Mapping[str, Any]) -> Tuple[List[str], bool]:
    """The result's text, bounded, with the truncation flag set honestly."""
    raw = data.get("stdout") or data.get("output") or data.get("raw") or ""
    if raw is None:
        raw = ""
    text = raw if isinstance(raw, str) else str(raw)
    lines = [line for line in text.splitlines() if line.strip()]
    cut = len(lines) > MAX_BODY_LINES
    return (lines[-MAX_BODY_LINES:] if cut else lines, cut)


# ---------------------------------------------------------------------------
# The thinking lane
# ---------------------------------------------------------------------------


def slow_endpoint_sentence(state: Any) -> str:
    """The exact sentence a slow endpoint shows.

    ``waiting for first token - no token after 9s - still working``. The
    middle clause is the whole point: a slow provider and a wedged
    process look identical until you say how long you have waited.
    """
    phase = str(getattr(state, "phase", "") or "")
    detail = str(getattr(state, "detail", "") or "")
    slow = bool(getattr(state, "slow", False))
    if not phase:
        return ""
    if slow and detail:
        return f"{phase} - {detail} - still working"
    if detail:
        return f"{phase} - {detail}"
    return str(phase)


@dataclass
class ThinkingStream:
    """The model's reasoning, inline, streaming, and bounded.

    It owns a :class:`StreamCoalescer` rather than reimplementing one, so
    the first-token-immediate rule, the coalescing window, the bounded
    live text and the control lane are the SAME mechanisms the run line
    already uses. What is new here is only the receipt: a thinking lane
    reports whether its frame cost tracked its token count, and a lane
    that observed nothing says so instead of reporting a healthy zero.
    """

    coalescer: StreamCoalescer = field(default_factory=StreamCoalescer)
    projector: "PhaseProjector" = field(default_factory=PhaseProjector)
    step: str = ""
    live: bool = False
    cancelled: bool = False
    ended: bool = False
    tokens: int = 0
    first_token_at: Optional[float] = None
    started_at: Optional[float] = None

    def begin(self, step: Any = "", *, now: Optional[float] = None) -> None:
        """A model request is in flight. Resets content, keeps control."""
        self.step = str(step or self.step or "")
        self.live = True
        self.cancelled = False
        self.ended = False
        self.tokens = 0
        self.first_token_at = None
        self.started_at = self.projector.clock() if now is None else now
        self.coalescer.reset()
        self.projector.consume("model_request", {"step": self.step})

    def push(self, delta: Any) -> None:
        """One reasoning delta. Never raises, never blocks."""
        text = (
            "" if delta is None else (delta if isinstance(delta, str) else str(delta))
        )
        if not text:
            return
        if not self.live:
            self.begin(self.step)
        self.tokens += 1
        if self.first_token_at is None:
            self.first_token_at = self.projector.clock()
        self.coalescer.push_delta(text)
        self.projector.consume("model_delta", {"step": self.step, "delta": text})

    def note(self, text: Any, level: str = "info") -> None:
        """A control-lane message (cancel, phase change, error)."""
        self.coalescer.push_control(text, level)

    def poll(self, now: Optional[float] = None) -> Optional[str]:
        """The text to paint this frame, or ``None`` for a no-op frame."""
        self.projector.consume("model_response", {"step": self.step})
        return self.coalescer.poll(now)

    def status(self, now: Optional[float] = None) -> str:
        """The phase sentence, including the slow-endpoint wording."""
        return slow_endpoint_sentence(self.projector.state())

    def end(self) -> None:
        """The model finished. Live text is kept for the transcript."""
        self.live = False
        self.ended = True
        self.projector.consume("model_response", {"step": self.step})

    def cancel(self) -> str:
        """The run was cancelled. PARTIAL TEXT IS KEPT, and says so.

        Discarding a half-written explanation because the run stopped is
        how a user loses the only account of what the model was about to
        do. The kept text is returned; the transcript block carries the
        note.
        """
        self.live = False
        self.cancelled = True
        self.ended = True
        self.projector.cancel()
        return self.text()

    def text(self) -> str:
        """The bounded reasoning text as a renderer should show it."""
        return self.coalescer.live_text()

    def _tail(self, max_lines: int = 6) -> Tuple[List[str], int]:
        parts = [line for line in self.text().splitlines() if line.strip()]
        if len(parts) <= max_lines:
            return (parts, 0)
        return (parts[-max_lines:], len(parts) - max_lines)

    def block(self, *, collapsed: bool = False) -> RoleBlock:
        """A THINKING block for the transcript, cancellation marked."""
        state = self.projector.state()
        detail = self.step
        if self.cancelled:
            detail = (detail + " - cancelled, kept partial text").strip(" -")
        lines, dropped = self._tail()
        block = RoleBlock(
            role=Role.THINKING,
            label="thinking" if not self.live else str(state.phase),
            detail=detail,
            body=lines,
            collapsed=collapsed,
            hidden_lines=dropped if collapsed else 0,
            truncated=bool(dropped and not collapsed),
            step=self.step,
        )
        return block

    def receipt(self, *, span_s: float = 1.0) -> Dict[str, Any]:
        """The measurement behind "thinking does not cost a frame a token".

        A lane that saw ZERO deltas reports ``observed: False`` and
        ``vacuous: True``: a frame-cost ratio computed over no events is
        not a pass, and reporting it as one is the defect this receipt
        exists to prevent.
        """
        cost = self.coalescer.frame_cost_receipt()
        window = int(cost.get("window_ms") or DEFAULT_WINDOW_MS)
        allowed = frames_allowed(span_s, window)
        observed = int(cost.get("events") or 0) > 0
        frames = int(cost.get("content_frames") or 0)
        return {
            "events": int(cost.get("events") or 0),
            "content_frames": frames,
            "frames_allowed": allowed,
            "within_bound": bool(observed and 0 < frames <= allowed),
            "events_per_frame": cost.get("events_per_frame") if observed else None,
            "window_ms": window,
            "trimmed": bool(cost.get("trimmed")),
            "live_chars": int(cost.get("live_chars") or 0),
            "observed": observed,
            "vacuous": not observed,
            "tokens": self.tokens,
            "cancelled": self.cancelled,
            "partial_text_kept": self.cancelled and bool(self.text().strip()),
        }

    def cost_vs(
        self, other: "ThinkingStream", *, span_s: float = 1.0
    ) -> Dict[str, Any]:
        """Compare this lane's frame cost against another lane's.

        Delegates to :func:`equivalent_frame_cost`, the measurement the
        coalescer already ships, so there is exactly one place in the
        product that decides whether frame cost tracked the token rate.
        """
        return equivalent_frame_cost(
            self.coalescer.frame_cost_receipt(),
            other.coalescer.frame_cost_receipt(),
            span_s=span_s,
        )


def thinking_frame_cost(
    tokens: int, *, window_ms: int = DEFAULT_WINDOW_MS, span_s: float = 1.0
) -> Dict[str, Any]:
    """Simulate a thinking lane of ``tokens`` deltas and measure it.

    Pure and I/O-free. A caller (or a test) gets the same receipt the
    live lane would produce for that shape, which is what makes "a dense
    and a sparse thinking lane cost the same" a measurement rather than
    an assertion about two different code paths.
    """
    simulated = simulate_stream(tokens=tokens, window_ms=window_ms, span_s=span_s)
    cost = simulated.frame_cost_receipt()
    allowed = frames_allowed(span_s, window_ms)
    observed = int(cost.get("events") or 0) > 0
    frames = int(cost.get("content_frames") or 0)
    return {
        "events": int(cost.get("events") or 0),
        "content_frames": frames,
        "frames_allowed": allowed,
        "within_bound": bool(observed and 0 < frames <= allowed),
        "observed": observed,
        "vacuous": not observed,
        # A ratio over no events is NOT zero, it is unmeasured. Returning
        # 0.0 would read as "the lane was free", which is a number
        # nobody measured.
        "events_per_frame": cost.get("events_per_frame") if observed else None,
    }


# ---------------------------------------------------------------------------
# The composer gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposerState:
    """Whether the composer accepts input, and why.

    The rule is one sentence: while a permission or a question is
    pending, the composer is DISABLED. A user who can type into a box
    whose contents will be ignored has been told, by the interface, that
    their input counts - and the decision still has to be made. The
    disabled state therefore carries the reason AND what to do about it.
    """

    disabled: bool
    reason: str = ""
    action: str = ""
    kind: str = ""

    def hint(self) -> str:
        """The one line a composer should show. Empty when enabled."""
        if not self.disabled:
            return ""
        if self.reason and self.action:
            return f"{self.reason} - {self.action}"
        return self.reason or "a decision is pending"


#: What a pending decision is called. Declared so a caller cannot invent a
#: fourth word for the same thing.
PENDING_DECISIONS: Tuple[str, ...] = (
    "permission",
    "question",
    "approval",
    "needs_input",
)


def composer_state(
    *, approval: Any = None, question: Any = None, steerable: bool = False
) -> ComposerState:
    """Resolve the composer's state from what is pending.

    ``approval`` / ``question`` are the pending records, or ``None`` for
    "nothing is pending". Presence - not truthiness - is the signal: a
    pending permission whose record happens to carry no fields is still
    a pending permission, and a record that reads as empty because the
    producer filled in nothing must not silently re-enable the box.

    A pending permission is checked FIRST: while a gate is open a
    steering message would also be ignored, and the honest thing to say
    is the gate. ``steerable=True`` is the one escape hatch, and it is
    an explicit argument rather than an inferred flag - a caller that
    believes a run really is steerable has to say so.
    """
    if steerable:
        return ComposerState(False)
    if approval is not None:
        return ComposerState(
            True,
            "a permission is waiting for your decision",
            "answer the permission prompt first",
            "permission",
        )
    if question is not None and str(question).strip():
        text, _cut = _clip(str(question))
        action = f"answer it first: {text}" if text else "answer it first"
        return ComposerState(
            True, "a question is waiting for your answer", action, "question"
        )
    return ComposerState(False)


# ---------------------------------------------------------------------------
# Inline undo / redo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileCounts:
    """One reverted file and its line counts."""

    path: str
    added: int = 0
    removed: int = 0

    def plain(self) -> str:
        """``path +12 -3`` - the shape a reader scans for."""
        return f"{self.path} +{self.added} -{self.removed}"


@dataclass
class UndoNotice:
    """A revert, rendered WHERE IT HAPPENED rather than in a modal.

    A modal is a fine place to ask "do you want to undo?" and a terrible
    place to find out what was undone ten minutes later. This block is
    the receipt: how many messages went back, how to put them back, and
    every file with its counts - bounded, with the omission stated.
    """

    messages_reverted: int
    files: List[FileCounts] = field(default_factory=list)
    restore_key: str = "ctrl+z"
    redo_command: str = "/redo"
    undone: bool = True
    hidden_files: int = 0

    @property
    def headline(self) -> str:
        """``N messages reverted`` - the sentence the brief names."""
        count = max(0, int(self.messages_reverted or 0))
        noun = "message" if count == 1 else "messages"
        return f"{count} {noun} reverted"

    def lines(self, *, max_files: int = MAX_FILES_LISTED) -> List[str]:
        """The whole block as plain text. Never markup."""
        out: List[str] = [self.headline]
        out.append(
            f"  restore: {self.restore_key}  ·  {self.redo_command} to apply again"
        )
        cap = max(0, int(max_files or 0))
        for item in self.files[:cap]:
            out.append("  " + item.plain())
        extra = max(0, len(self.files) - cap) + max(0, int(self.hidden_files or 0))
        if extra > 0:
            out.append(f"  +{extra} more file(s) reverted (not listed)")
        if self.undone and not self.files:
            out.append("  no file changes in this revert")
        return out

    def to_dict(self) -> Dict[str, Any]:
        listed = min(len(self.files), MAX_FILES_LISTED)
        return {
            "messages_reverted": int(self.messages_reverted or 0),
            "files": [
                {"path": item.path, "added": item.added, "removed": item.removed}
                for item in self.files[:listed]
            ],
            "files_listed": listed,
            "files_total": listed + max(0, int(self.hidden_files or 0)),
            "restore_key": self.restore_key,
            "redo_command": self.redo_command,
            "undone": self.undone,
        }


def diff_counts(diff: Any) -> Tuple[int, int]:
    """Count added and removed lines in a unified diff. Total."""
    text = "" if diff is None else (diff if isinstance(diff, str) else str(diff))
    added = removed = 0
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return (added, removed)


def undo_notice_from_facts(
    facts: Optional[Mapping[str, Any]] = None, *, max_files: int = MAX_FILES_LISTED
) -> UndoNotice:
    """Build an :class:`UndoNotice` from a receipt or a journal payload.

    Accepts both shapes a caller has on hand: the kernel's own undo
    receipt (``messages_reverted`` / ``restored_files``) and the staged
    ``cli.fileview`` range, which reports turns and per-file counts.
    Counts are integers read from the receipt and never guessed; an
    absent count renders as zero rather than as a plausible invention.
    """
    data: Mapping[str, Any] = facts if isinstance(facts, Mapping) else {}
    messages = 0
    for key in ("messages_reverted", "turns", "messages", "reverted_messages"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            messages = int(value)
            break
        if isinstance(value, (list, tuple)):
            messages = len(value)
            break
    raw_files = (
        data.get("files") or data.get("restored_files") or data.get("changes") or []
    )
    files: List[FileCounts] = []
    for item in raw_files if isinstance(raw_files, (list, tuple)) else []:
        if isinstance(item, FileCounts):
            files.append(item)
        elif isinstance(item, Mapping):
            path = str(item.get("path") or item.get("file") or "")
            if not path:
                continue
            added = item.get("added")
            removed = item.get("removed")
            if added is None and removed is None:
                added, removed = diff_counts(
                    item.get("diff") or item.get("patch") or ""
                )
            files.append(
                FileCounts(
                    path=path,
                    added=int(added) if isinstance(added, (int, float)) else 0,
                    removed=int(removed) if isinstance(removed, (int, float)) else 0,
                )
            )
        elif isinstance(item, str) and item.strip():
            files.append(FileCounts(path=item.strip()))
    cap = max(0, int(max_files or 0))
    return UndoNotice(
        messages_reverted=messages,
        files=files,
        restore_key=str(data.get("restore_key") or "ctrl+z"),
        redo_command=str(data.get("redo_command") or "/redo"),
        undone=bool(data.get("undone", True)),
        hidden_files=max(0, len(files) - cap),
    )


# ---------------------------------------------------------------------------
# The surface: journal rows in, role blocks out
# ---------------------------------------------------------------------------


def _timestamp(raw: Any) -> str:
    """A row's time as ``HH:MM:SS``, or empty when it has none."""
    if raw in (None, ""):
        return ""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(raw)))
    except Exception:
        return ""


class TranscriptSurface:
    """Journal rows in, role blocks out. Pure, total, and I/O-free.

    This is the seam a renderer mounts. It holds a
    :class:`ThinkingStream` for the live lane, folds every other row into
    a :class:`RoleBlock`, and reports a receipt that counts the rows it
    saw and the event names it emitted - which is always zero, because
    :func:`role_for_event` has no path that returns one.
    """

    def __init__(self, *, thinking: Optional[ThinkingStream] = None) -> None:
        self.thinking = thinking if thinking is not None else ThinkingStream()
        self._blocks: List[RoleBlock] = []
        self._rows = 0
        self._unclassified = 0
        self._dropped_events = 0
        self._live_delta_count = 0
        #: Every event name this surface was fed, kept only so the leak
        #: check can look for it in the rendered text.
        self._names: List[str] = []

    # -- ingest

    def feed(
        self, kind: Any, data: Optional[Mapping[str, Any]] = None
    ) -> Optional[RoleBlock]:
        """Fold one journal row. Returns the block it produced, if any.

        The row's own name is RECORDED here and never used again except
        by the leak check, which scans the rendered text for it. That is
        the whole mechanism behind "no raw event name may reach the
        user": there is no code path from a journal name to a rendered
        string, and the receipt measures the absence rather than
        promising it.
        """
        payload: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        self._rows += 1
        name = str(kind or "").strip().lower()
        self._names.append(name)
        role = role_for_event(name, payload)
        if name in ("model_delta", "response_delta", "text_delta"):
            self._live_delta_count += 1
            self.thinking.push(payload.get("delta"))
            self._dropped_events += 1
            return None
        if name in ("model_request", "model_started", "turn_started"):
            self.thinking.begin(payload.get("step"))
            self._dropped_events += 1
            return None
        if name in ("model_response", "model_completed"):
            self.thinking.end()
            block = self.thinking.block()
            self._blocks.append(block)
            self._dropped_events += 1
            return block
        if name not in _EVENT_ROLES:
            self._unclassified += 1
        block = self._block_for(role, name, payload)
        self._blocks.append(block)
        return block

    def _block_for(self, role: Role, name: str, data: Mapping[str, Any]) -> RoleBlock:
        stamp = _timestamp(data.get("ts") or data.get("timestamp"))
        step = str(data.get("step") or "")
        if role is Role.THINKING:
            body, cut = _result_body(data)
            return RoleBlock(
                role=Role.THINKING,
                label="thinking",
                detail=step,
                body=body,
                timestamp=stamp,
                truncated=cut,
                step=step,
            )
        if role is Role.ACTION:
            label, target = _action_label(data)
            return RoleBlock(
                role=Role.ACTION,
                label=label,
                detail=target,
                timestamp=stamp,
                step=step,
            )
        if role is Role.RESULT:
            body, cut = _result_body(data)
            exit_code = data.get("exit_code")
            detail = ""
            if exit_code is not None and str(exit_code) not in ("0", ""):
                detail = f"exit {exit_code}"
            return RoleBlock(
                role=Role.RESULT,
                label="result",
                detail=detail,
                body=body,
                timestamp=stamp,
                truncated=cut,
                step=step,
            )
        if role is Role.EDIT:
            path = str(data.get("path") or data.get("file") or "")
            added, removed = diff_counts(data.get("diff") or data.get("patch") or "")
            return RoleBlock(
                role=Role.EDIT,
                label="edited",
                detail=path,
                body=[f"+{added} -{removed}"] if (added or removed) else [],
                timestamp=stamp,
                step=step,
            )
        if role is Role.NEEDS_YOU:
            return RoleBlock(
                role=Role.NEEDS_YOU,
                label="needs you",
                detail=_clip(
                    str(
                        data.get("exact_effect")
                        or data.get("effect")
                        or data.get("question")
                        or ""
                    )
                )[0],
                timestamp=stamp,
                step=step,
            )
        if role is Role.FAILURE:
            return RoleBlock(
                role=Role.FAILURE,
                label="failed",
                detail=_failure_reason(data),
                timestamp=stamp,
                step=step,
            )
        # SYSTEM. The label is a fixed vocabulary of lifecycle words, so
        # even here the event's own name is unreachable.
        verbs = {
            "task_start": "run started",
            "run_started": "run started",
            "run_start": "run started",
            "project_start": "run started",
            "phase_changed": "phase changed",
            "phase_change": "phase changed",
            "state_change": "phase changed",
            "attempt_start": "attempt started",
            "step_end": "step finished",
            "step_skipped_resume": "step skipped, resumed",
            "task_end": "run finished",
            "run_finished": "run finished",
            "run_finish": "run finished",
            "project_end": "run finished",
            "result": "run finished",
            "stop": "run stopped",
            "finish": "run finished",
            "completion_decision": "run finished",
            "cancellation_requested": "cancel requested",
            "cancel_requested": "cancel requested",
            "steering": "steered",
            "steering_abort": "steer: abort",
            "steering_replan": "steer: re-plan",
            "steering_step_yield": "steer: yielding",
            "execution_backend_ready": "execution backend ready",
            "project_criteria_extracted": "criteria extracted",
        }
        label = verbs.get(name, "progress")
        detail, _cut = _clip(str(data.get("status") or data.get("mode") or ""))
        return RoleBlock(
            role=Role.SYSTEM, label=label, detail=detail, timestamp=stamp, step=step
        )

    # -- reads

    @property
    def blocks(self) -> List[RoleBlock]:
        """Every block so far, in order."""
        return list(self._blocks)

    def role_counts(self) -> Dict[str, int]:
        """How many blocks of each role. A receipt, not a render."""
        counts = {name: 0 for name in ROLE_NAMES}
        for block in self._blocks:
            counts[block.role.value] = counts.get(block.role.value, 0) + 1
        return counts

    def receipt(self) -> Dict[str, Any]:
        """The machine answer to "did any raw event name reach the user?".

        ``emitted_event_names`` is built by scanning the rendered text of
        every block against the names that were fed in. It is measured,
        not asserted, so a row that somehow reached a label still shows
        up as a number instead of a comment.
        """
        rendered = "\n".join(
            line for block in self._blocks for line in block.plain_lines()
        )
        leaked = sorted({name for name in self._names if name and name in rendered})
        return {
            "rows": self._rows,
            "blocks": len(self._blocks),
            "roles": self.role_counts(),
            "unclassified_rows": self._unclassified,
            "live_deltas": self._live_delta_count,
            "held_for_lane": self._dropped_events,
            "rendered_chars": len(rendered),
            "emitted_event_names": leaked,
            "clean": not leaked,
        }

    def plain_lines(self) -> List[str]:
        """Every block as plain text. Never markup."""
        lines: List[str] = []
        for block in self._blocks:
            lines.extend(block.plain_lines())
        return lines
