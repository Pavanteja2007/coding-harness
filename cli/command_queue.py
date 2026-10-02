"""Command queueing: the named exempt set, the shared policy, and the queue.

VEX-CS-04. ``cli/tui.py`` has queued commands since the AGT-10 round; the REPL
refuses them and the headless adapter refuses them. That is three surfaces
answering the same question three ways, and the measured pre-round table is
in ``logs/command-surface/terminal-04.json``:

    ``/plan ship it`` while busy ->
        tui       status=queued
        repl      status=refused   (exit 2)
        headless  status=disabled  (exit 2)

This module is the ONE answer, as a pure function, plus the queue data
structure that goes with it. It is importable without Textual and it does not
read a terminal, a session, or a journal: the caller supplies the busy
signal and the dispatch function, which is what makes all three surfaces
testable against one implementation.

**Delivery reuses the AGT-10 boundary-injection seam and does not build a
second transport.** :meth:`CommandQueue.drain` calls the real
``harness.tools.run_tool_batch`` -- the same function whose docstring says
``dispatch`` is invoked exactly once per call, that a call is never split,
and that ``seam`` fires only BETWEEN calls with a ``False`` return naming the
untouched remainder in ``not_executed``. A queued command has exactly the
same guarantee an AGT-10 queued steering message has, because it is the same
mechanism and not a lookalike.

The one thing this module cannot do on its own is be reached: the three call
sites live in ``cli/tui.py``, ``cli/interactive.py`` and
``cli/command_exec.py``, none of which is this round's file. The exact mount
points are in ``cli/AGENTS.md`` under "Handoff to <terminal>" and in
``logs/command-surface/terminal-04.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from cli import command_aliases as _aliases

__all__ = [
    "QUEUED_STATUS",
    "QUEUE_ACTIONS",
    "QUEUE_EXEMPT_COMMANDS",
    "QUEUE_EXEMPT_CONFIG_KEY",
    "QUEUE_POLICIES",
    "QUEUE_POLICY_CONFIG_KEY",
    "CommandQueue",
    "QueueConfigReport",
    "QueueDecision",
    "QueueDrain",
    "QueuedCommand",
    "decide",
    "escape_lines",
    "exempt_commands",
    "is_exempt",
    "queue_depth",
    "queue_lines",
    "resolve_queue_config",
    "safe_lines",
    "statusline_facts",
]


# ---------------------------------------------------------------------------
# The closed vocabularies. A decision nobody can name is a decision nobody
# can debug, and a surface that can emit a fourth action is a script that
# works in a terminal and fails in CI.
# ---------------------------------------------------------------------------

#: What a surface does with one command. ``run_now`` executes it immediately
#: (including every exempt command while busy), ``queue`` holds it for the
#: next boundary, ``refuse`` says no and exits 2.
QUEUE_ACTIONS: Tuple[str, ...] = ("run_now", "queue", "refuse")

#: The ``CommandResolution.status`` word a queued command carries. It is the
#: word ``cli/commands.py`` already emits on the TUI, reused verbatim so a
#: status string cannot mean two things depending on the surface.
QUEUED_STATUS = "queued"

#: The commands that run IMMEDIATELY while a run is in flight.
#:
#: Named, not inferred. An exempt set derived from a heuristic ("anything
#: read-only") changes meaning the day somebody adds a read-only command that
#: takes nine seconds, and the next reader has no way to know which commands
#: the product decided were safe to interrupt a run for. This is the set the
#: brief names, expressed as the spellings a user actually types:
#:
#: * ``/status``  -- what is happening right now
#: * ``/tasks``   -- what runs exist (an alias of ``/sessions``)
#: * ``/usage``   -- what it has cost so far (an alias of ``/cost``)
#: * ``/cost``    -- the same, spelled directly
#: * ``/cancel``  -- the door out; refusing it while busy would be a trap
#:
#: Every one of the five is a fact about the present or the way to stop. None
#: of them mutates the repository, starts a run, or forges a subagent, which
#: is the property the set is really asserting.
QUEUE_EXEMPT_COMMANDS: Tuple[str, ...] = (
    "/status",
    "/tasks",
    "/usage",
    "/cost",
    "/cancel",
)

#: The ``Task.config`` key that overrides the exempt set. Read by KEY
#: PRESENCE, never by truthiness: an absent key means "nobody said", which is
#: different from "somebody said empty", and a value in
#: ``harness.config.DEFAULTS`` merges into every task and every eval arm.
QUEUE_EXEMPT_CONFIG_KEY = "queue_exempt_commands"

#: The ``Task.config`` key that switches the whole policy off. ``None`` in
#: ``DEFAULTS``, opt-in by presence, and its OFF arm is "refuse while busy",
#: which is today's REPL behaviour and therefore the safe default for a run
#: nobody asked to be interruptible.
QUEUE_POLICY_CONFIG_KEY = "queue_commands_while_busy"

#: The declared default for :data:`QUEUE_POLICY_CONFIG_KEY`.
QUEUE_POLICY_DEFAULT = "queue"

QUEUE_POLICIES: Tuple[str, ...] = ("queue", "refuse")


# ---------------------------------------------------------------------------
# The exempt set, and its one honesty problem
# ---------------------------------------------------------------------------


def exempt_commands(config: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    """Return the exempt command names, CANONICALISED and de-duplicated.

    The brief names ``/tasks`` and ``/usage``, which are aliases in this
    product rather than commands (``command_spec("/tasks") is None`` measured).
    They are canonicalised through the one alias authority so the exempt set
    contains command names a surface can actually resolve -- and a config
    value naming an unknown command is dropped rather than honoured, because
    an exempt entry nothing can resolve is a promise the product cannot keep.
    """

    names: Sequence[str]
    if isinstance(config, Mapping) and QUEUE_EXEMPT_CONFIG_KEY in config:
        raw = config.get(QUEUE_EXEMPT_CONFIG_KEY)
        if isinstance(raw, (str, list, tuple, set, frozenset)):
            names = (
                [part for part in raw.replace(",", " ").split() if part]
                if isinstance(raw, str)
                else [str(part) for part in raw if str(part).strip()]
            )
        else:
            names = QUEUE_EXEMPT_COMMANDS
    else:
        names = QUEUE_EXEMPT_COMMANDS

    from cli import commands as _commands

    out: List[str] = []
    for name in names:
        canonical = _aliases.resolve_alias(name)
        if canonical in out:
            continue
        if _commands.command_spec(canonical) is None:
            continue
        out.append(canonical)
    return tuple(out)


def is_exempt(name: Any, config: Optional[Mapping[str, Any]] = None) -> bool:
    """True when a command runs immediately while a run is in flight."""

    return _aliases.resolve_alias(name) in exempt_commands(config)


def resolve_queue_config(
    config: Optional[Mapping[str, Any]] = None,
) -> QueueConfigReport:
    """Read the two queue keys out of a task config and report what happened.

    Fail-closed about itself: an unusable value is REPORTED and the declared
    default is used, so a typo in a settings file cannot silently turn
    queueing off (or, worse, silently turn an unknown set into a blank one).
    """

    source = config if isinstance(config, Mapping) else {}
    notes: List[str] = []

    policy = QUEUE_POLICY_DEFAULT
    if QUEUE_POLICY_CONFIG_KEY in source:
        raw = source.get(QUEUE_POLICY_CONFIG_KEY)
        if isinstance(raw, bool):
            policy = "queue" if raw else "refuse"
        elif raw in QUEUE_POLICIES:
            policy = str(raw)
        else:
            notes.append(
                f"{QUEUE_POLICY_CONFIG_KEY}={raw!r} is not one of "
                f"{QUEUE_POLICIES}; using {QUEUE_POLICY_DEFAULT!r}"
            )

    exempt = exempt_commands(source)
    if QUEUE_EXEMPT_CONFIG_KEY in source:
        raw = source.get(QUEUE_EXEMPT_CONFIG_KEY)
        usable = isinstance(raw, (str, list, tuple, set, frozenset))
        if not usable:
            notes.append(
                f"{QUEUE_EXEMPT_CONFIG_KEY}={raw!r} is neither a string nor a "
                f"list; using the declared set"
            )
        elif not exempt:
            notes.append(
                f"{QUEUE_EXEMPT_CONFIG_KEY} named no command this build can "
                "resolve; using the declared set"
            )
    if not exempt:
        exempt = exempt_commands(None)
        notes.append("declared exempt set carried forward")

    return QueueConfigReport(
        policy=policy,
        exempt=exempt,
        exempt_source="config" if QUEUE_EXEMPT_CONFIG_KEY in source else "declared",
        policy_source=("config" if QUEUE_POLICY_CONFIG_KEY in source else "declared"),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class QueueConfigReport:
    """What the queue keys resolved to, and every deviation on the way."""

    policy: str
    exempt: Tuple[str, ...]
    exempt_source: str
    policy_source: str
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the report."""

        return {
            "policy": self.policy,
            "exempt": list(self.exempt),
            "exempt_source": self.exempt_source,
            "policy_source": self.policy_source,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The one policy. All three surfaces call this and none of them decides.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueDecision:
    """What one surface should do with one command, and why."""

    action: str
    status: str
    reason: str
    command: str = ""
    exempt: bool = False
    in_flight: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.action == "run_now"

    @property
    def queued(self) -> bool:
        """True when the command belongs in the queue."""

        return self.action == "queue"

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the decision."""

        return {
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "command": self.command,
            "exempt": self.exempt,
            "in_flight": self.in_flight,
        }


def decide(
    resolution: Any,
    *,
    in_flight: bool,
    config: Optional[Mapping[str, Any]] = None,
    surface: str = "repl",
) -> QueueDecision:
    """Decide what ONE surface does with ONE resolved command line.

    ``resolution`` is a ``cli.commands.CommandResolution`` (duck-typed: it
    needs ``spec``, ``status`` and ``args``), so this function does not import
    ``cli.commands`` at module scope and cannot create an import cycle.

    The order of the rules is the order a person would state them:

    1. not busy -> run now, whatever the command is;
    2. an EXEMPT command -> run now, and say so;
    3. a command whose own registry row says ``in_flight_policy == "allow"``
       -> run now, because the registry already made that decision and this
       function does not second-guess it;
    4. a command whose row says ``"refuse"`` -> refuse;
    5. everything else (a ``"queue"`` row, or the whole policy switched off)
       -> queue.

    Rule 3 before rule 4 matters: the registry is the authority on what each
    command may do mid-run, and a surface that invented its own answer would
    be a second opinion about a decision somebody already made.
    """

    from cli import commands as _commands

    raw_status = str(getattr(resolution, "status", "") or "")
    raw_line = str(getattr(resolution, "raw", "") or "")
    head = raw_line.split()[0] if raw_line.split() else ""
    canonical = _aliases.resolve_alias(head) if head else ""

    # `resolve_command_line` reads `CommandSpec.aliases` only, so a name that
    # lives in THIS module's table (`/tasks`, `/usage`, `/stats`, `/bashes`)
    # reaches it as `unknown`. Re-resolving the canonical name is what makes
    # the exempt set meaningful for the spellings a user actually types, and
    # it is a registry lookup rather than a string comparison.
    spec = getattr(resolution, "spec", None)
    if spec is None and canonical:
        spec = _commands.command_spec(canonical)

    if spec is None:
        return QueueDecision(
            action="refuse",
            status=raw_status or "unknown",
            reason=(
                f"the registry refused this line ({raw_status or 'unknown'}) "
                "before the queue was consulted"
            ),
            command=canonical,
            in_flight=bool(in_flight),
        )

    name = str(getattr(spec, "name", "") or "")
    canonical = canonical or name
    policy = str(getattr(spec, "in_flight_policy", "") or "queue")

    # `resolve_command_line` collapses the queue policy into ONE status word
    # per SURFACE -- `queued` on the TUI, `refused` on the REPL, `disabled`
    # headless -- and that divergence is exactly what this module exists to
    # remove, so the status word is NOT what the policy is read from: the
    # registry ROW is. The two queue-collapsed words are therefore accepted
    # here and decided from `policy` below; every OTHER status
    # (`unknown` / `not_command` / `invalid` / `hidden` / `disabled`) is a
    # refusal the registry made for a reason of its own and is honoured
    # as-is. An ALIAS-only spelling arrives with `status == "unknown"` and a
    # spec recovered above, which is why `unknown` is in the accepted set.
    registry_refusal = raw_status not in {
        "ok",
        QUEUED_STATUS,
        "refused",
        "unknown",
    }
    if registry_refusal:
        return QueueDecision(
            action="refuse",
            status=raw_status or "refuse",
            reason=(
                f"the registry refused this line ({raw_status or 'unknown'}) "
                "before the queue was consulted"
            ),
            command=canonical,
            in_flight=bool(in_flight),
        )

    if not in_flight:
        return QueueDecision(
            action="run_now",
            status="ok",
            reason="no run is in flight",
            command=canonical,
            in_flight=False,
        )

    if canonical and is_exempt(canonical, config):
        return QueueDecision(
            action="run_now",
            status="ok",
            reason=(
                f"{canonical} is a named exempt command: it reports the present "
                "state, or is the way to stop, and it does not need the run to "
                "finish first"
            ),
            command=canonical,
            exempt=True,
            in_flight=True,
        )

    report = resolve_queue_config(config)
    if policy == "allow":
        return QueueDecision(
            action="run_now",
            status="ok",
            reason=f"the registry allows {canonical} while a run is in flight",
            command=canonical,
            in_flight=True,
        )
    if policy == "refuse" or report.policy == "refuse":
        return QueueDecision(
            action="refuse",
            status="refused",
            reason=(
                f"{canonical} is unavailable while a run is in flight "
                "(wait for it to finish, or /cancel it)"
            ),
            command=canonical,
            in_flight=True,
        )
    return QueueDecision(
        action="queue",
        status=QUEUED_STATUS,
        reason="held until the current task finishes",
        command=canonical,
        in_flight=True,
    )


# ---------------------------------------------------------------------------
# The queue itself: visible, removable, editable, and consumed exactly once.
# ---------------------------------------------------------------------------


@dataclass
class QueuedCommand:
    """One held command line. ``seq`` is the identity a receipt reports."""

    seq: int
    text: str
    command: str
    args: str = ""
    surface: str = ""
    delivered: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the held command."""

        return {
            "seq": self.seq,
            "text": self.text,
            "command": self.command,
            "args": self.args,
            "surface": self.surface,
            "delivered": self.delivered,
        }


@dataclass
class CommandQueue:
    """An ordered, visible, editable queue of held command lines.

    Deliberately a plain list of small records rather than the TUI's bare
    ``list[str]``: a queued prompt a user cannot see, cannot edit, and cannot
    remove before delivery is a promise the product made and did not keep.
    The TUI's existing ``self._queue`` holds canonical LINES and its
    ``_after_run`` pops one; this is the same lifecycle with the receipt and
    the edit/remove doors, and :meth:`drain` is the same boundary.
    """

    surface: str = "repl"
    _items: List[QueuedCommand] = field(default_factory=list)
    _next_seq: int = 1
    _delivered: List[QueuedCommand] = field(default_factory=list)

    # -- depth / inspection ------------------------------------------------
    def depth(self) -> int:
        """Return how many commands are waiting."""

        return len(self._items)

    def __len__(self) -> int:  # pragma: no cover - convenience only
        return len(self._items)

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return bool(self._items)

    def peek(self) -> Tuple[QueuedCommand, ...]:
        """Return the waiting commands in delivery order, without consuming."""

        return tuple(self._items)

    def delivered(self) -> Tuple[QueuedCommand, ...]:
        """Return everything this queue has already consumed, in order."""

        return tuple(self._delivered)

    # -- mutation ----------------------------------------------------------
    def enqueue(self, text: str, *, surface: str = "") -> QueuedCommand:
        """Hold one command line and return the record that was held.

        The line is canonicalised here (``f"{spec.name} {args}".rstrip()``,
        the exact spelling ``cli/tui.py`` already appends) so the queue never
        stores a line whose resolution could differ from what was queued.
        """

        raw = str(text or "").strip()
        canonical, command, args = _canonicalise(raw)
        item = QueuedCommand(
            seq=self._next_seq,
            text=canonical,
            command=command,
            args=args,
            surface=surface or self.surface,
        )
        self._next_seq += 1
        self._items.append(item)
        return item

    def remove(self, seq: int) -> Optional[QueuedCommand]:
        """Drop the queued command with this sequence number.

        Returns the removed record, or ``None`` when the number is not in the
        queue. A removal that reported success for a sequence nobody queued
        would be a receipt that lied, so the miss is reported as ``None``.
        """

        for index, item in enumerate(self._items):
            if item.seq == int(seq):
                return self._items.pop(index)
        return None

    def edit(self, seq: int, text: str) -> Optional[QueuedCommand]:
        """Replace a queued command's line in place, keeping its position.

        Position matters: a person editing the second of three queued
        commands does not mean "run it next", they mean "run the thing I
        fixed, in the order I queued it".
        """

        for item in self._items:
            if item.seq == int(seq):
                canonical, command, args = _canonicalise(str(text or "").strip())
                item.text = canonical
                item.command = command
                item.args = args
                return item
        return None

    def clear(self) -> List[QueuedCommand]:
        """Drop every waiting command and return what was dropped."""

        dropped = list(self._items)
        self._items = []
        return dropped

    # -- delivery ----------------------------------------------------------
    def drain(
        self,
        dispatch: Callable[[QueuedCommand], Any],
        *,
        seam: Optional[Callable[[Any], bool]] = None,
    ) -> "QueueDrain":
        """Deliver waiting commands through the AGT-10 boundary seam.

        ``dispatch`` is the surface's own line handler, invoked EXACTLY ONCE
        per queued command and never re-entered -- the same guarantee
        ``harness.tools.run_tool_batch`` gives a tool call, because this IS
        ``run_tool_batch``. ``seam`` is called only BETWEEN commands and after
        the last one; a ``seam`` that returns ``False`` or raises stops the
        drain and the untouched remainder is named in ``not_delivered``, so a
        delivery that failed can never let the rest run as though it had
        happened.

        A queued command is REMOVED BEFORE its dispatch runs. That ordering
        is the whole answer to "must never execute twice": there is no window
        in which a line is both delivered and still waiting, and a dispatch
        that raises mid-flight cannot re-deliver on the next drain.
        """

        from harness import tools as _tools

        pending = list(self._items)
        self._items = []
        drained: List[QueuedCommand] = []
        failed: List[Tuple[int, str]] = []

        def _dispatch(item: QueuedCommand) -> Any:
            # The entry is counted as consumed BEFORE the handler runs. That is
            # the "consumed exactly once" guarantee: the line is removed from
            # the queue before dispatch (so it can never be re-delivered) and
            # it stays consumed even if the handler raises, because it WAS
            # handed to exactly one handler. Silently retrying a line whose
            # handler threw is how a `/plan` runs twice.
            drained.append(item)
            # The step shape is `harness.tools.ToolBatchStep`'s own: a queued
            # command is dispatched through the SAME seam as a tool call, so
            # the receipt it produces has the same three fields a tool step
            # has. Building it here rather than inventing a record is what
            # makes "the queue reuses the AGT-10 seam" a fact rather than a
            # resemblance.
            try:
                step = _tools.ToolBatchStep(ok=True, output="", detail=item.text)
            except Exception:  # pragma: no cover - an older field set
                step = _tools.ToolBatchStep(True, "", item.text)
            try:
                dispatch(item)
                return step
            except Exception as exc:
                # Recorded, not propagated, and not retried. The command ran
                # (one handler saw it); the rest of the queue still has a
                # boundary to arrive at. `failed` names the seq and the reason
                # so the loss is on the record instead of in a traceback.
                failed.append((item.seq, f"{type(exc).__name__}: {exc}"))
                try:
                    return _tools.ToolBatchStep(ok=False, output="", detail=item.text)
                except Exception:  # pragma: no cover
                    return step

        batch = None
        undelivered: Tuple[QueuedCommand, ...] = ()
        try:
            batch = _tools.run_tool_batch(pending, _dispatch, seam=seam)
        finally:
            # A `dispatch` that RAISES propagates out of `run_tool_batch`, and
            # without this the commands it never reached would simply be gone:
            # popped, undelivered, and unreported. Everything the drain did not
            # hand to a handler goes back at the FRONT, in order, because it
            # was never run -- which is also exactly what a seam that returned
            # False put in `not_executed`. That is the whole answer to "never
            # silently drop the remainder", including the case where the
            # handler itself is what failed.
            seen = {item.seq for item in drained}
            undelivered = tuple(i for i in pending if i.seq not in seen)
            if undelivered:
                self._items = list(undelivered) + self._items
        delivered = tuple(drained)
        self._delivered.extend(delivered)
        return QueueDrain(
            delivered=delivered,
            not_delivered=undelivered,
            seams=int(getattr(batch, "seams", 0) or 0) if batch is not None else 0,
            surface=self.surface,
            failed=tuple(failed),
        )

    # -- receipts ----------------------------------------------------------
    def receipt(self) -> Dict[str, Any]:
        """Return the auditable state of the queue.

        ``undelivered`` is the load-bearing key: the sequence numbers the
        queue still holds. A receipt that reported only the depth would look
        identical before and after a command was lost.
        """

        return {
            "surface": self.surface,
            "depth": self.depth(),
            "waiting": [item.as_dict() for item in self._items],
            "delivered": [item.seq for item in self._delivered],
            "undelivered": [item.seq for item in self._items],
            "executed": len(self._delivered),
        }


@dataclass
class QueueDrain:
    """What one drain delivered, and what it deliberately did not."""

    delivered: Tuple[QueuedCommand, ...] = ()
    not_delivered: Tuple[Any] = ()
    seams: int = 0
    surface: str = ""
    failed: Tuple[Tuple[int, str], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly projection of the drain.

        `failed` is `(seq, reason)` pairs. A handler that raises is recorded
        here rather than propagated: the command WAS handed to exactly one
        handler, so it is consumed and must not be retried, and the rest of
        the queue still has a boundary to arrive at. Hiding it would be the
        opposite failure -- a silent drop.
        """

        return {
            "surface": self.surface,
            "delivered": [item.as_dict() for item in self.delivered],
            "not_delivered": [
                getattr(item, "text", str(item)) for item in self.not_delivered
            ],
            "seams": self.seams,
            "failed": [{"seq": seq, "reason": reason} for seq, reason in self.failed],
        }


def _canonicalise(raw: str) -> Tuple[str, str, str]:
    """Return ``(line, command, args)`` for one queued command line.

    Same shape ``cli/tui.py`` already appends -- ``f"{spec.name}
    {args}".rstrip()`` -- so a queue built here and a queue built there hold
    byte-identical strings and a test comparing them is comparing like with
    like. An unresolvable line is kept VERBATIM: dropping it would be losing
    what the person typed.
    """

    stripped = str(raw or "").strip()
    if not stripped:
        return ("", "", "")
    from cli import commands as _commands

    resolution = _commands.resolve_command_line(stripped)
    if resolution.spec is None:
        head, _, rest = stripped.partition(" ")
        return (stripped, _aliases.normalize_command_name(head), rest.strip())
    return (
        f"{resolution.spec.name} {resolution.args}".strip(),
        resolution.spec.name,
        resolution.args,
    )


# ---------------------------------------------------------------------------
# Presentation. Plain producers; the two sanctioned exits.
# ---------------------------------------------------------------------------


def queue_depth(queue: Any) -> int:
    """Return the depth of a queue, from THIS queue or a bare ``list``.

    The TUI keeps ``self._queue`` as a ``list[str]`` in 14 call sites and this
    round does not own that file, so a statusline fact that only accepted a
    :class:`CommandQueue` would refuse to describe the product's real queue.
    Both shapes answer the same question.
    """

    depth = getattr(queue, "depth", None)
    if callable(depth):
        try:
            return int(depth())
        except Exception:  # pragma: no cover - defensive
            return 0
    try:
        return len(queue)
    except Exception:  # pragma: no cover - defensive
        return 0


def statusline_facts(queue: Any) -> Dict[str, str]:
    """Return the statusline's queue fact, or nothing when the queue is empty.

    The STRING is produced by ``cli.design.STATUSLINE_SECTIONS`` -- the same
    table the TUI renders its ``3 queued (ctrl+g)`` from -- so there is one
    renderer of that sentence in the product and this function cannot print a
    second wording.

    ``0 queued`` is a claim about the queue and an absent entry is the honest
    form of it, which is that table's own rule: a statusline that reports
    ``0 queued`` forever is a row that costs a row and states nothing.
    """

    count = queue_depth(queue)
    if count <= 0:
        return {}
    try:
        from cli import design as _design
    except Exception:  # pragma: no cover - design is CLI-internal
        return {"queue": f"{count} queued (ctrl+g)"}
    for item in _design.STATUSLINE_SECTIONS:
        if getattr(item, "key", "") != "queue":
            continue
        rendered = item.render(count, "")
        if rendered:
            return {"queue": rendered}
    return {"queue": f"{count} queued (ctrl+g)"}


def queue_lines(queue: Any, *, width: int = 0) -> List[str]:
    """Return PLAIN lines a surface can render to show what is waiting.

    Every entry is DATA a person typed, and a queued line may contain a file
    path with a bracket in it. The lines are plain; the caller escapes them or
    renders them as ``rich.text.Text`` (see :func:`escape_lines` and
    :func:`safe_lines`).
    """

    items: Iterable[Any]
    peek = getattr(queue, "peek", None)
    if callable(peek):
        items = peek()
    else:
        items = list(queue or [])
    out: List[str] = []
    for item in items:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        seq = getattr(item, "seq", "")
        prefix = f"{seq}. " if seq != "" else ""
        out.append(f"{prefix}{text}")
    if not out:
        return ["nothing queued"]
    if width and width > 8:
        bound = width - 2
        out = [
            (line if len(line) <= bound else line[: bound - 1] + "\u2026")
            for line in out
        ]
    return out


def escape_lines(values: Sequence[str]) -> List[str]:
    """Escape every line through rich's own ``escape``.

    Rich is the parser's own implementation, so this cannot drift from it.
    """

    try:
        from rich.markup import escape
    except Exception:  # pragma: no cover
        return [str(v) for v in values]
    return [escape(str(v)) for v in values]


def safe_lines(values: Sequence[str]) -> List[Any]:
    """Return ``rich.text.Text`` lines, which have NO markup interpretation.

    The structural answer: a ``Text`` object cannot be markup-parsed even in
    principle, so this path cannot delete a message.
    """

    try:
        from rich.text import Text
    except Exception:  # pragma: no cover
        return [str(v) for v in values]
    return [Text(str(v)) for v in values]
