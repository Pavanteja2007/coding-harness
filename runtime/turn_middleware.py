"""Turn-level policies as composable middleware, in a declared order.

The corpus's rule R1 says: *"Start with a linear `while` loop; graduate to a
middleware pipeline only when orthogonal turn-level policies emerge. Upgrade
path: once you need three or more independent turn-level policies (turn limits,
cost caps, auto-compaction, context-budget warnings, read-only mode), adopt
Mistral Vibe's middleware pipeline pattern — each policy becomes a composable
middleware, not a new branch in the loop body."*

``runtime.loop_complexity`` measures that threshold rather than asserting it:
**31** in-loop turn-level policy branches across the two agent turn loops, an
order of magnitude past three. Those branches were the thing that made each
loop body unreadable, because a reader had to reconstruct the relative order of
nine independent guards by reading top to bottom and holding it in their head.
"This reads before that, that precedes the other" is a *design property*, and a
design property that lives only in the vertical order of a loop body cannot be
asserted — it can only be re-read and hoped over.

So this module makes the order **data**. :data:`SEMANTIC_ORDERINGS` declares
which policy must be consulted before which, and with what reason, and
:class:`TurnPolicyPipeline` refuses to be constructed if the composed order
violates one. A reordering that still "works" is a bug waiting for a load spike;
this is the check that says so at import time rather than in production.

Four things this is built to guarantee, each for a reason:

**A refusal and a termination are different things, and the types say so.**
:meth:`Decision.refuse` and :meth:`Decision.terminate` are separate
constructors over separate frozen dataclasses (:class:`Refusal` vs
:class:`Termination`), not one class with an ``is_free`` flag a caller can set
wrongly. A policy refusal means "a human or a cap said no; the run stops asking
and reports it"; a termination means "the run cannot continue". Collapsing them
into one status is how a user saying *no* becomes a failed task — the exact
defect AGT-11 found and had to undo.

**A refusal is FREE.** The repository rule is *"a non-retryable failure is
FREE"*: it must not consume a recovery budget, because a policy that a user
repeatedly declines would otherwise grind a run to death through the recovery
ladder rather than through the decision itself. :class:`Refusal` carries
``charged=False`` and ``retryable=False`` as *fields with no way to set them
otherwise* — ``Refusal.__init__`` does not accept them, and
:meth:`Refusal.__post_init__` refuses to construct one that claims to be
charged. A test asserts the charged arm too, so a suite that asserts "the
refusal is free" cannot pass by never charging anything.

**Middleware state is per-run, never shared.** :class:`TurnRun` is the
per-run state holder and the pipeline never holds one itself; a caller
constructs one per run and passes it in. A module-level pipeline shared across
50 concurrent runs is a data race, and the latches that policies need (the
one-shot turn-cap warning) are exactly the kind of mutable state that races.
:meth:`TurnRun.state` is keyed by ``(run_id, middleware)`` so a stray
cross-run key is detectable rather than invisible.

**Adding a middleware does not touch the loop.** :meth:`TurnPolicyPipeline.add`
appends to the composed chain and returns ``self``; the loop calls
``pipeline.check(turn, run)`` and contains no reference to any middleware's
name. That is asserted *structurally* by reading the loop AST — a loop body
that names a middleware is a violation, not a style note, because the whole
claim is that the loop has no per-policy knowledge left in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

#: The closed phase vocabulary. Phases run in this order and a pipeline is
#: constructed by name, so "which phase is this policy in" is a field rather
#: than an accident of where somebody appended the middleware.
#:
#: * ``pre_turn`` — per turn, before anything is spent. Turn ceiling, wall
#:   clock, cost cap, cancellation. Anything here must be cheap and must not
#:   perform I/O beyond a boundary read.
#: * ``pre_call`` — before the model request is built and dialled. Per-call
#:   budget authorization, context compaction, context-budget warnings. This is
#:   where "check the money, then dial" is enforced.
#: * ``pre_dispatch`` — before any individual tool call reaches a backend.
#:   Read-only gating, approval, doom-loop counting, protected-path refusals.
#:   One call may dispatch several tools, so this phase runs per call.
#: * ``post_turn`` — after the turn produced its result. Recovery-budget
#:   accounting and the summary a run hands to the next turn.
PHASES: tuple[str, ...] = ("pre_turn", "pre_call", "pre_dispatch", "post_turn")

#: Phases in which a ``refuse`` is a legal outcome. A refusal in ``post_turn``
#: has nothing left to protect — the turn's work already happened — so it is
#: refused by the type rather than converted into a status the caller would
#: misread.
REFUSABLE_PHASES: frozenset[str] = frozenset({"pre_turn", "pre_call", "pre_dispatch"})

#: Statuses a policy may produce. A subset of
#: ``shared.agent_contracts.RUN_STATUSES`` — deliberately *not* the whole set,
#: because a middleware cannot mint a completion and a vocabulary that includes
#: a completion word is a vocabulary where a typo can.
POLICY_STATUSES: tuple[str, ...] = (
    "needs_input",
    "blocked",
    "failed",
    "timeout",
    "cancelled",
)

#: The terminal vocabulary a policy may produce. A ``refuse`` may not appear
#: here — that is the whole distinction — and neither may any completion.
POLICY_TERMINAL_STATUSES: tuple[str, ...] = ("blocked", "failed", "timeout", "cancelled")

#: The status a ``refuse`` always produces. One value, so "the policy said no"
#: reads identically wherever it is reported.
REFUSAL_STATUS = "needs_input"


class MiddlewareOrderError(ValueError):
    """A composed pipeline violates a declared :data:`SEMANTIC_ORDERINGS` rule.

    Raised at **construction**, not at first use. A wrong order that only fails
    when the cap is hit is a wrong order that ships.
    """


# --------------------------------------------------------------------------
# The turn view
# --------------------------------------------------------------------------


@dataclass
class Turn:
    """What a middleware may read and mutate about the turn being considered.

    A plain mutable dataclass rather than a protocol so that a policy can read
    a fact without the loop having to hand it a dozen callables — the boundary
    is the *values*, and every one of them is a number or a closed-vocabulary
    word that a middleware can decide on without knowing the loop exists.

    ``calls`` is the list of tool calls this turn proposes to dispatch, which is
    what makes "read-only must gate before a write is dispatched" checkable
    inside the pipeline rather than at the dispatch site.
    """

    number: int
    max_turns: int
    elapsed_s: float = 0.0
    max_wallclock_s: float = 0.0
    spent_usd: float = 0.0
    budget_cap_usd: float = 0.0
    reserved_usd: float = 0.0
    cancelled: bool = False
    read_only: bool = False
    steering: str = ""
    context_tokens: int = 0
    context_window: int = 0
    context_compact_fraction: float = 0.6
    context_compacted: bool = False
    model: str = ""
    model_price_state: str = ""
    calls: tuple["ToolCall", ...] = ()
    #: Free-form per-turn scratch a middleware may use to hand a fact to a
    #: later middleware in the same phase. Never read outside one run.
    scratch: dict[str, Any] = field(default_factory=dict)

    def call_named(self, *names: str) -> tuple["ToolCall", ...]:
        """The proposed calls whose verb is any of ``names``."""
        wanted = {n.lower() for n in names}
        return tuple(c for c in self.calls if c.verb.lower() in wanted)


@dataclass(frozen=True)
class ToolCall:
    """The minimum a policy needs to judge one proposed tool call.

    ``verb`` is the typed catalog's canonical name. ``writes`` is the
    capability class rather than a hand-kept verb list, so a new mutating tool
    is gated by virtue of being declared mutating rather than by somebody
    remembering to add it to a second table.
    """

    verb: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: ``True``/``False`` when the producer declares the capability, and ``None``
    #: when it does not. Tri-state on purpose: a two-state field cannot tell
    #: "this tool is read-only" from "nobody said", and collapsing those makes
    #: an undeclared call indistinguishable from a proven-safe one. The first
    #: version of :func:`call_writes` did exactly that and answered ``True`` for
    #: a ``read`` of a test file, which made the test-removal floor refuse an
    #: ordinary inspection.
    writes: bool | None = None
    path: str = ""
    requires_approval: bool = False


#: Mutating capability classes, from the canonical catalog's vocabulary. A call
#: whose class is absent here is treated as NON-mutating, which is the safe
#: direction only because the catalog defaults unknown tools to
#: read_only is **false** — see :func:`call_writes`.
WRITE_CLASSES: frozenset[str] = frozenset(
    {"workspace_write", "process", "network", "memory", "control", "vcs"}
)

#: Shell commands that cannot mutate. A ``bash`` call is *capable* of writing, so
#: without an exemption here every shell call is mutating and a read-only mode
#: refuses ``git diff -- tests/`` and ``sed -n '1,20p' tests/test_x.py``.
#:
#: This table lives HERE rather than in ``runtime.test_protection`` because
#: "does this call mutate" is a capability question asked by two modules, and two
#: tables answering it is how ``git diff -- tests/`` ends up refused by one guard
#: and allowed by another. ``test_protection`` imports it from here.
READ_ONLY_HEADS: frozenset[str] = frozenset(
    {
        "cat", "head", "tail", "ls", "dir", "find", "grep", "egrep", "rg", "wc",
        "file", "stat", "pwd", "which", "type", "echo", "printf", "true", "false",
        "test", "[", "awk", "jq", "sort", "uniq", "diff", "cmp", "du", "df",
        "basename", "dirname", "realpath", "readlink", "env", "python", "pytest",
    }
)
READ_ONLY_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"diff", "log", "show", "status", "blame", "ls-files", "branch", "describe",
     "rev-parse", "shortlog"}
)
#: Shell metacharacters whose presence means the command is a composition rather
#: than a single verb. A read verb inside a composition is not a read verb.
COMPOSITION_CHARS: tuple[str, ...] = ("|", ">", "<", "&", ";", "`", "$(", "\n")
#: A single- or double-quoted span, so a metacharacter inside a string is not
#: read as a composition. Compiled once because this runs on every proposed call.
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def shell_command_of(arguments: Any) -> str:
    """The command string a shell-shaped call carries, if any."""
    if not isinstance(arguments, Mapping):
        return ""
    for key in ("command", "cmd", "shell", "script", "argv"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(arguments.get("argv"), (list, tuple)):
        return " ".join(str(part) for part in arguments["argv"])
    return ""


def _strip_quotes(text: str) -> str:
    """Remove quoted spans so a metacharacter inside a string is not a
    composition. ``sed -n '1,20p'`` has no composition once the quotes go."""
    return _QUOTED_SPAN_RE.sub(" ", text or "")


def is_read_only_command(command: str) -> bool:
    """Whether a shell command provably cannot mutate.

    Conservative in **both** directions, and both halves matter:

    * a known read verb at the head is necessary but not sufficient — a
      ``python`` one-liner or a ``find -delete`` can act;
    * no recognised mutating shape anywhere, and **no shell composition outside
      quotes**. ``cat a.py > b.py`` starts with a read verb and ends in a write,
      which is the exact case a head-only check misses — and it is what this
      function's own pin exists to catch.

    A full ``pytest`` run is **not** treated as read-only for the purposes of the
    deletion floor even though it is listed as a read verb: it executes the test
    module, whose fixtures write, and it is the shape the doom-loop guard wants
    to see. The two callers disagree deliberately and say so where they do.
    """
    if not command:
        return False
    body = _strip_quotes(command)
    for char in COMPOSITION_CHARS:
        if char in body:
            return False
    lowered = body.strip().lower()
    if not lowered:
        return False
    for marker in ("-delete", "-exec", "rm ", "mv ", "cp ", "chmod", "dd ", "tee ", "patch ", ">"):
        if marker in lowered:
            return False
    words = re.split(r"\s+", lowered)
    head = words[0].rsplit("/", 1)[-1] if words else ""
    if head == "git":
        return len(words) >= 2 and words[1] in READ_ONLY_GIT_SUBCOMMANDS
    if head == "sed":
        # `sed -n` prints; `sed -i` rewrites. Decided on the flag, not the verb.
        if any(flag in words for flag in ("-i", "--in-place")):
            return False
        return any(flag in words for flag in ("-n", "--quiet", "-p"))
    return head in READ_ONLY_HEADS


def call_writes(call: ToolCall) -> bool:
    """Whether a proposed call may mutate something.

    Four inputs, most certain first:

    * a call that DECLARES ``writes=True`` is mutating;
    * a call whose verb names a class in :data:`WRITE_CLASSES` is mutating even
      if it declares otherwise — an explicit "this does not write" on a tool whose
      own catalog class says it does is a contradiction, and the conservative
      reading of a contradiction is the safe one;
    * a call carrying a shell command that :func:`is_read_only_command` can
      **prove** is read-only is not mutating. The proof is what makes the
      exemption safe: a shell call is otherwise treated as capable of anything;
    * otherwise the call's own ``writes`` declaration decides, and a call that
      declares **nothing** is mutating — because the catalog's default for an
      unrecognised tool is to assume it can act.
    """
    if call.writes:
        return True
    if call.verb.lower() in WRITE_CLASSES:
        return True
    command = shell_command_of(call.arguments)
    if command:
        # A shell command present and NOT provably read-only is mutating, and
        # this OVERRIDES a declared ``writes=False``. The declaration is a claim
        # by the caller; the command text is evidence. A caller that says "this
        # bash call does not write" about ``cat a.py > b.py`` is wrong, and
        # honouring the claim would let the read-only guard be talked out of its
        # own finding by the very argument it is judging. The shell proof is the
        # only exemption, and it is checked FIRST for that reason.
        return not is_read_only_command(command)
    return call.writes is not False


# --------------------------------------------------------------------------
# Outcomes: refusal and termination are separate types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """A middleware saw something and the turn continues.

    ``kind`` is a closed vocabulary so an event surface can render it without
    inventing a column per policy. ``data`` carries the receipt.
    """

    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Mutation:
    """A middleware changed the turn and the turn continues.

    Only *declared* fields may change: :meth:`Turn.with_changes` rejects a key
    that is not a :class:`Turn` field, so a middleware cannot smuggle an
    undeclared attribute into the view that a later middleware would then trust.
    """

    changes: Mapping[str, Any]
    kind: str = "mutation"
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Refusal:
    """A policy declined. **Free**, non-retryable, and not a failure.

    ``charged`` and ``retryable`` are fields with exactly one legal value and
    no constructor parameter, so a policy cannot declare a refusal that spends
    budget. ``__post_init__`` re-checks them anyway: a frozen dataclass can
    still be force-mutated through ``object.__setattr__``, and a guard that
    only lives in the signature is a guard a determined caller walks around.
    """

    reason: str
    policy: str = ""
    turn: int = 0
    #: What the run should do next. ``stop`` ends the turn and reports;
    #: ``continue`` notes it and lets the turn finish. Both are free.
    action: str = "stop"
    detail: Mapping[str, Any] = field(default_factory=dict)
    charged: bool = False
    retryable: bool = False

    kind: str = "refuse"

    def __post_init__(self) -> None:
        if self.action not in ("stop", "continue"):
            raise ValueError(f"Refusal.action must be 'stop' or 'continue', not {self.action!r}")
        if self.charged:
            raise ValueError(
                "a policy refusal is FREE by the repository rule "
                "('a non-retryable failure is FREE'); charged=True is not constructible"
            )
        if self.retryable:
            raise ValueError(
                "a policy refusal is not retryable: retrying a declined action "
                "spends the recovery budget on the user's decision"
            )

    @property
    def status(self) -> str:
        """The one status a refusal reports."""
        return REFUSAL_STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "policy": self.policy,
            "turn": self.turn,
            "action": self.action,
            "detail": dict(self.detail),
            "charged": self.charged,
            "retryable": self.retryable,
            "free": True,
        }


@dataclass(frozen=True)
class Termination:
    """A policy stopped the run. This one **is** charged and is terminal.

    Constructed only through :meth:`Decision.terminate`, which validates the
    status against :data:`POLICY_TERMINAL_STATUSES`. ``needs_input`` is not in
    that set — a "termination" that asks a human for input is a refusal wearing
    the wrong name, and letting it through is how the two shapes drift back
    together.
    """

    status: str
    reason: str
    policy: str = ""
    turn: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)
    charged: bool = True
    retryable: bool = False

    kind: str = "terminate"

    def __post_init__(self) -> None:
        if self.status not in POLICY_TERMINAL_STATUSES:
            raise ValueError(
                f"Termination.status must be one of {POLICY_TERMINAL_STATUSES}; "
                f"{self.status!r} is not a terminal policy status "
                f"(a human-decision stop is a Refusal, not a Termination)"
            )

    @property
    def free(self) -> bool:
        """Always ``False``. Present so a reader does not have to know that only
        one of the two outcome types is free."""
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "policy": self.policy,
            "turn": self.turn,
            "detail": dict(self.detail),
            "charged": self.charged,
            "retryable": self.retryable,
            "free": self.free,
        }


@dataclass(frozen=True)
class Decision:
    """The constructors. A middleware calls one of these four and nothing else.

    Four classmethods rather than a free union so that "can this policy stop the
    run?" is answered by the *name it called*, and a reviewer reading a policy
    can see whether it can refuse, terminate, or only observe — which is the
    property that makes composing them safe. A policy that only ever observes is
    safe to run before the money check; one that can terminate is not.
    """

    @staticmethod
    def observe(kind: str, _payload: Mapping[str, Any] | None = None, **data: Any) -> Observation:
        """Record something; the turn continues unchanged.

        ``_payload`` is merged without letting its keys shadow ``kind``. That is
        not defensive decoration: the first draft took only ``**data``, and the
        first real payload it met — ``harness.turn_caps.approach_observation`` —
        carries its own ``kind`` key, so a policy forwarding a receipt verbatim
        raised ``TypeError: got multiple values for argument 'kind'`` and the
        run ended ``failed`` with a middleware's arity in the reason. A policy
        that forwards somebody else's receipt must not have to know the shape of
        that receipt, so the collision is resolved here, once.
        """
        merged: dict[str, Any] = dict(data)
        if _payload:
            for key, value in _payload.items():
                merged.setdefault(key, value)
        return Observation(kind=kind, data=merged)

    @staticmethod
    def mutate(**changes: Any) -> Mutation:
        """Change declared fields of the turn; the turn continues."""
        return Mutation(changes=dict(changes))

    @staticmethod
    def refuse(reason: str, *, policy: str = "", turn: int = 0, action: str = "stop", **detail: Any) -> Refusal:
        """Decline. Free, non-retryable, status ``needs_input``."""
        return Refusal(reason=reason, policy=policy, turn=turn, action=action, detail=dict(detail))

    @staticmethod
    def terminate(status: str, reason: str, *, policy: str = "", turn: int = 0, **detail: Any) -> Termination:
        """Stop the run. Charged, terminal, status from the closed set."""
        return Termination(status=status, reason=reason, policy=policy, turn=turn, detail=dict(detail))


#: What a middleware's ``evaluate`` may return. Distinct from :class:`Decision`,
#: which is the CONSTRUCTOR surface -- ``Decision.observe(...)`` and friends.
#: Naming the union separately is deliberate: an earlier version called this
#: alias ``Decision`` and the class immediately shadowed it, so every annotation
#: spelled ``-> Decision`` while meaning the class, and the union a caller
#: actually wanted was unreachable under its own name.
AnyDecision = Observation | Mutation | Refusal | Termination

#: The four kinds, for an exhaustive check.
DECISION_KINDS: tuple[type, ...] = (Observation, Mutation, Refusal, Termination)


# --------------------------------------------------------------------------
# Per-run state
# --------------------------------------------------------------------------


class RunStateError(RuntimeError):
    """Middleware state was read or written under the wrong run identity.

    Raised rather than tolerated. Two concurrent runs sharing one policy chain
    is the failure mode W1.5 exists to prevent, and a *detectable* leak is worth
    more than a silently merged one.
    """


@dataclass
class TurnRun:
    """Per-run middleware state. **One per run**, never shared.

    The latches policies need — the one-shot turn-cap announcement, the
    compaction counter, the per-run refusal tally — are mutable, so they live
    here and nowhere else. ``keyed`` namespaces every write by ``run_id``, and a
    read under a foreign run id raises :class:`RunStateError` instead of
    returning another run's value.

    ``middleware_seen`` records every ``(middleware, decision-kind)`` pair, which
    is what lets a receipt prove that a policy actually ran rather than merely
    existing in the chain.
    """

    run_id: str
    _state: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    middleware_seen: list[tuple[str, str]] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    terminations: list[Termination] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    #: Monotonic per-run sequence, so a receipt can order events across
    #: middlewares without relying on wall-clock time.
    sequence: int = 0

    def _bucket(self, middleware: str) -> dict[str, Any]:
        return self._state.setdefault(middleware, {})

    def note(self, middleware: str, kind: str) -> int:
        """Record that ``middleware`` returned a ``kind`` decision.

        Returns the run's next sequence number, so a caller can sort receipts
        across middlewares without a clock.
        """
        self.middleware_seen.append((middleware, kind))
        self.sequence += 1
        return self.sequence

    def get(self, middleware: str, key: str, default: Any = None, *, run_id: str | None = None) -> Any:
        """Read one per-run value. ``run_id`` mismatch raises."""
        self._assert_owner(run_id)
        return self._bucket(middleware).get(key, default)

    def set(self, middleware: str, key: str, value: Any, *, run_id: str | None = None) -> None:
        """Write one per-run value. ``run_id`` mismatch raises."""
        self._assert_owner(run_id)
        self._bucket(middleware)[key] = value

    def bump(self, middleware: str, key: str, *, run_id: str | None = None) -> int:
        """Increment and return a per-run counter. Missing counter starts at 0."""
        self._assert_owner(run_id)
        bucket = self._bucket(middleware)
        current = int(bucket.get(key, 0))
        current += 1
        bucket[key] = current
        return current

    def _assert_owner(self, run_id: str | None) -> None:
        if run_id is not None and run_id != self.run_id:
            raise RunStateError(
                f"middleware state belongs to run {self.run_id!r}; "
                f"a read or write arrived under {run_id!r}. A shared policy chain "
                "across concurrent runs is a data race, not a shared cache."
            )

    def receipt(self) -> dict[str, Any]:
        """A run-scoped receipt. ``shared: False`` is the field that matters."""
        return {
            "run_id": self.run_id,
            "middleware_seen": [[name, kind] for name, kind in self.middleware_seen],
            "refusals": [r.to_dict() for r in self.refusals],
            "terminations": [t.to_dict() for t in self.terminations],
            "observations": [
                {"kind": o.kind, "data": dict(o.data)} for o in self.observations
            ],
            "sequence": self.sequence,
            "shared": False,
        }


# --------------------------------------------------------------------------
# The middleware protocol
# --------------------------------------------------------------------------


@runtime_checkable
class TurnMiddleware(Protocol):
    """What the pipeline requires of a policy.

    Three attributes and one method. Deliberately small: a protocol a policy has
    to satisfy by writing adapters is a protocol that grows a second
    representation of every policy, and two representations of one policy is how
    they drift.

    ``phase`` is a name from :data:`PHASES`, not an integer, so a reordering of
    the phase tuple is a visible edit rather than a silent reshuffle of
    priorities.
    """

    name: str
    phase: str

    def evaluate(self, turn: Turn, run: TurnRun) -> Decision:
        """Decide about ``turn``.

        Must not raise for a policy condition it recognises; return a
        :class:`Refusal` or :class:`Termination` instead. An exception escaping
        a policy is a harness bug, and :class:`TurnPolicyPipeline` turns one
        into a termination rather than letting it unwind the loop.
        """
        ...


@dataclass(frozen=True)
class Policy:
    """A middleware built from a callable, with its phase and name as data.

    The callable receives ``(turn, run)`` and returns a :class:`Decision`. This
    is the whole adapter: there is no abstract base class to inherit, so a policy
    can be a closure over its own config and its docstring can be the receipt.

    ``trigger`` is the fixture that makes this policy do the thing it claims to
    do. It is required whenever ``can_stop`` is ``True``, and
    :meth:`stop_actually_reachable` drives it — so "this policy can refuse" is
    checked by refusing, not by reading a flag. Deriving that from a bare probe
    turn was the first draft's mistake: with every field at its default no guard
    fires, so every policy reported ``can_stop=False`` and the receipt claimed a
    chain of twelve policies of which none could stop anything.
    """

    name: str
    phase: str
    fn: Any
    can_stop: bool = False
    trigger: Any = None

    def evaluate(self, turn: Turn, run: TurnRun) -> Decision:
        if self.phase not in PHASES:
            raise MiddlewareOrderError(
                f"middleware {self.name!r} declares phase {self.phase!r}, "
                f"which is not one of {PHASES}"
            )
        return self.fn(turn, run)

    def stop_actually_reachable(self) -> bool:
        """Whether driving ``trigger`` really does produce a stop.

        Three outcomes, deliberately not two:

        * a Refusal or Termination — the declaration is TRUE;
        * anything else — the declaration is **FALSE**, i.e. a policy claiming it
          can stop but never doing so is a policy that cannot;
        * a trigger-less policy declaring ``can_stop=True`` — reported as not
          reachable, because an unverifiable claim is not a capability.

        The caller is expected to turn a ``True`` declaration with an
        unreachable stop into a finding rather than a receipt row.
        """
        if callable(self.trigger):
            try:
                turn = self.trigger()
            except Exception:
                return False
            state = TurnRun(run_id=f"__trigger__:{self.name}")
            try:
                decision = self.evaluate(turn, state)
            except Exception:
                return False
            return isinstance(decision, (Refusal, Termination))
        return False


# --------------------------------------------------------------------------
# Declared ordering
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ordering:
    """One rule of the form "``before`` must be consulted before ``after``".

    ``phase`` is the phase both live in when they share one; ``cross_phase=True``
    means the rule is about the phase order rather than the within-phase order.
    """

    before: str
    after: str
    reason: str
    same_phase: bool = True

    def describe(self) -> str:
        where = "within" if self.same_phase else "across"
        return f"{self.before} before {self.after} ({where} phases): {self.reason}"


#: The declared semantic orderings. **Each of these is asserted behaviourally**,
#: by constructing the situation the rule is about and driving the real
#: pipeline — not by comparing indices. An index comparison proves the table is
#: internally consistent; only driving the pipeline proves the pipeline obeys it.
SEMANTIC_ORDERINGS: tuple[Ordering, ...] = (
    Ordering(
        before="budget_cap",
        after="per_call_budget",
        reason=(
            "a session cap and a per-call cap are the same money seen at two "
            "granularities; asking the fine-grained one first means the "
            "authorization is computed against the session total the coarse "
            "check would have refused, so the reason a call is refused names the "
            "call rather than the session"
        ),
        same_phase=False,
    ),
    Ordering(
        before="per_call_budget",
        after="model_dial",
        reason=(
            "the cost of the NEXT call is knowable before the call is made, so a "
            "call is priced BEFORE it is dialled. This is what bounds the "
            "overshoot to one call's price rather than a whole turn's"
        ),
    ),
    Ordering(
        before="auto_compact",
        after="context_exceeded",
        reason=(
            "auto-compaction is the remedy and context_exceeded is the symptom. "
            "A policy that reports the symptom before the remedy has had a "
            "chance to run turns a recoverable context overflow into a failed "
            "turn on every run that needed compaction"
        ),
    ),
    Ordering(
        before="read_only_gate",
        after="write_dispatch",
        reason=(
            "read-only mode must gate BEFORE a mutating tool is dispatched, not "
            "after it has run and been rolled back. A post-hoc gate is a "
            "detector; a pre-dispatch gate is a guarantee"
        ),
    ),
    Ordering(
        before="wall_clock",
        after="model_dial",
        reason=(
            "the clock is checked before the dial so a turn is never paid for "
            "after the wall-clock bound has already passed"
        ),
        same_phase=False,
    ),
    Ordering(
        before="cancellation",
        after="model_dial",
        reason=(
            "a cancel is checked before the dial so a cancelled run cannot spend "
            "a model call on a turn it was never allowed to finish"
        ),
        same_phase=False,
    ),
)


def ordering_declaration_errors(chain: Sequence[str] = ()) -> list[str]:
    """Declared orderings that cannot be checked, and why.

    A rule whose two policies do not live where it says they do is a rule that
    quietly never fires — and one of the six in :data:`SEMANTIC_ORDERINGS` was
    declared ``same_phase=True`` for a pair that lives in two different phases,
    so five of six reordering attempts passed a check that looked like it was
    passing all six. The checker was right and the *declaration* was wrong, which
    is exactly the case a reader cannot see by reading the checker.

    Three findings, all reported rather than raised:

    * a ``same_phase=True`` rule whose two policies are in different phases —
      the rule can never be violated, because the phase walk already separates
      them;
    * a name that is not in the chain at all;
    * an unknown phase.
    """
    from runtime.turn_policies import POLICY_BUILDERS

    names = tuple(chain) or tuple(POLICY_BUILDERS)
    phase_of: dict[str, str] = {}
    for name in names:
        builder = POLICY_BUILDERS.get(name)
        if builder is None:
            return [f"{name!r} is not a declared policy name"]
        phase_of[name] = builder().phase

    problems: list[str] = []
    phase_rank = {phase: index for index, phase in enumerate(PHASES)}
    for rule in SEMANTIC_ORDERINGS:
        for side in (rule.before, rule.after):
            if side not in phase_of:
                problems.append(
                    f"{rule.before}/{rule.after}: {side!r} is not in the chain, "
                    "so the rule cannot be checked here"
                )
        if rule.before in phase_of and rule.after in phase_of:
            left, right = phase_of[rule.before], phase_of[rule.after]
            if rule.same_phase and left != right:
                problems.append(
                    f"{rule.before}/{rule.after}: declared same_phase=True but the "
                    f"policies are in {left!r} and {right!r}; the rule can never "
                    "fire, and reordering the chain would look like it passed"
                )
            if not rule.same_phase:
                if left == right:
                    problems.append(
                        f"{rule.before}/{rule.after}: declared same_phase=False but "
                        f"both policies are in {left!r}; the rule is checked as a "
                        "chain order instead of a phase order, which is the wrong "
                        "check for it"
                    )
                elif phase_rank.get(left, 99) >= phase_rank.get(right, 99):
                    problems.append(
                        f"{rule.before}/{rule.after}: {left!r} does not precede "
                        f"{right!r} in PHASES, so the rule is violated by the "
                        "phase table itself"
                    )
    return problems


def ordering_violations(names: Sequence[str]) -> list[Ordering]:
    """Orderings violated by a composed middleware-name sequence.

    A rule whose two policies are both absent from the chain is **not** a
    violation: a chain is allowed to omit a policy, and a rule about two absent
    policies cannot be broken. A rule with only one present also cannot be
    broken, and saying so is what stops the checker from demanding that every
    deployment install all six orderings' policies.
    """
    index = {name: position for position, name in enumerate(names)}
    violations: list[Ordering] = []
    for rule in SEMANTIC_ORDERINGS:
        left, right = index.get(rule.before), index.get(rule.after)
        if left is None or right is None:
            continue
        if left >= right:
            violations.append(rule)
    return violations


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What one phase produced.

    ``turn`` is the possibly-mutated turn, so a caller that continues picks up
    the mutations. ``considered`` names every middleware that ran before the
    decision, which is what makes an ordering bug diagnosable from a receipt
    instead of only by reading the chain.
    """

    decision: Decision | None
    phase: str
    turn: Turn
    considered: tuple[str, ...] = ()
    stopped_at: str = ""

    @property
    def continues(self) -> bool:
        return self.decided_is_continue

    @property
    def decided_is_continue(self) -> bool:
        """Whether the run should keep going after this outcome.

        Written as three explicit cases rather than the one-liner ruff's SIM103
        suggests, because **the per-case reading is the contract**: an undecided
        turn continues, a ``Refusal`` continues only when it says
        ``action="continue"``, and a ``Termination`` never does. Folding the last
        two into ``not isinstance(self.decision, Termination)`` is equivalent
        today and stops being equivalent the moment a fifth decision kind is
        added -- at which point the collapsed form silently continues and the
        enumerated form forces the new kind to be placed. The three-branch shape
        is the thing that makes a new decision kind a compile-and-test question
        rather than a guess.
        """
        if self.decision is None:
            return True
        if isinstance(self.decision, Refusal):
            return self.decision.action == "continue"
        if isinstance(self.decision, Termination):  # noqa: SIM103 - see docstring
            return False
        # Observation / Mutation: the turn is unaffected by design. This last
        # `return True` is the Observation/Mutation branch, named here because
        # ruff reads it as a foldable tail of the Termination case above; it is
        # the branch that makes a FIFTH decision kind a question rather than a
        # silent `continue`.
        return True

    @property
    def is_refusal(self) -> bool:
        return isinstance(self.decision, Refusal)

    @property
    def is_termination(self) -> bool:
        return isinstance(self.decision, Termination)

    @property
    def free(self) -> bool:
        """True only for a refusal. A ``None``/observe/mutate outcome is not a
        stop at all, so it is not "free" — it simply did not stop anything."""
        return self.is_refusal

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "decided": self.decision is not None,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "considered": list(self.considered),
            "stopped_at": self.stopped_at,
            "is_refusal": self.is_refusal,
            "is_termination": self.is_termination,
            "free": self.free,
            "turn": {
                "number": self.turn.number,
                "read_only": self.turn.read_only,
                "spent_usd": self.turn.spent_usd,
                "context_tokens": self.turn.context_tokens,
            },
        }


class TurnPolicyPipeline:
    """An ordered, composable chain of turn-level policies.

    The loop's entire remaining knowledge of turn policy is one call to
    :meth:`check`. Construction validates the declared orderings, so a chain
    assembled in the wrong order fails where it was assembled.
    """

    def __init__(
        self,
        middlewares: Iterable[Any] = (),
        *,
        phases: Sequence[str] = PHASES,
        validate_order: bool = True,
    ) -> None:
        self._phases: tuple[str, ...] = tuple(phases)
        for phase in self._phases:
            if phase not in PHASES:
                raise MiddlewareOrderError(f"unknown phase {phase!r}; expected one of {PHASES}")
        self._middlewares: list[Any] = []
        for middleware in middlewares:
            self._append(middleware)
        if validate_order:
            self.assert_order()

    # -- composition ------------------------------------------------------

    def add(self, middleware: Any) -> "TurnPolicyPipeline":
        """Append a middleware. Returns ``self`` so composition reads as a chain.

        This is the API the "adding a policy must not touch the loop" claim
        rests on: a caller composes by calling this, and the loop is untouched
        because the loop only ever calls :meth:`check`.
        """
        self._append(middleware)
        self.assert_order()
        return self

    def _append(self, middleware: Any) -> None:
        name = getattr(middleware, "name", None)
        phase = getattr(middleware, "phase", None)
        if not isinstance(name, str) or not name:
            raise MiddlewareOrderError(
                f"middleware {middleware!r} has no string `name`; an unnamed "
                "policy cannot appear in a receipt, and a receipt is how a "
                "reviewer learns which policy fired"
            )
        if phase not in self._phases:
            raise MiddlewareOrderError(
                f"middleware {name!r} declares phase {phase!r}, which is not in "
                f"this pipeline's phases {self._phases}"
            )
        if any(getattr(other, "name", None) == name for other in self._middlewares):
            raise MiddlewareOrderError(
                f"two middlewares are both named {name!r}; a receipt naming a "
                "policy would not say which one ran"
            )
        self._middlewares.append(middleware)

    @property
    def middlewares(self) -> tuple[Any, ...]:
        return tuple(self._middlewares)

    @property
    def names(self) -> tuple[str, ...]:
        """Composed order, flattened across phases."""
        return tuple(m.name for m in self._middlewares)

    def names_in_phase(self, phase: str) -> tuple[str, ...]:
        return tuple(m.name for m in self._middlewares if m.phase == phase)

    def assert_order(self) -> None:
        """Raise if a declared :data:`SEMANTIC_ORDERINGS` rule is violated.

        Two kinds of rule, checked two different ways, because they are two
        different facts:

        * ``same_phase=True`` — the two policies share a phase, so the order is a
          property of the **chain order inside that phase**. This is the kind a
          reordering can break.
        * ``same_phase=False`` — the two live in different phases, so the order
          is a property of the **phase table**, and no reordering of the chain
          can break it, because :meth:`check` walks phases first. The check is
          therefore that the ``before`` policy's phase precedes the ``after``
          policy's, and the honest way to violate it is to declare a policy in
          the wrong phase.

        Compared against the order the chain is actually **visited** in, not the
        order the middlewares were appended in. Those differ: a chain that
        appends a ``pre_call`` policy before a ``pre_turn`` one is visited in the
        opposite order, because :meth:`check` walks phase by phase. The first
        version of this method compared the *within-phase* enumeration index,
        which made five of the six declared orderings silently unenforced — an
        index that is meaningless across two phases.
        """
        positions = self._effective_positions()
        violations: list[str] = []
        for rule in SEMANTIC_ORDERINGS:
            left = positions.get(rule.before)
            right = positions.get(rule.after)
            if left is None or right is None:
                continue
            if rule.same_phase:
                if left[1] != right[1]:
                    continue
                if left[0] >= right[0]:
                    violations.append(
                        f"{rule.describe()} -- composed order is {list(self.names)}"
                    )
                continue
            left_phase = self._phases.index(left[1])
            right_phase = self._phases.index(right[1])
            if left_phase >= right_phase:
                violations.append(
                    f"{rule.describe()} -- {rule.before!r} is in phase "
                    f"{left[1]!r} and {rule.after!r} is in phase {right[1]!r}, "
                    f"and the phase table walks {list(self._phases)}"
                )
        if violations:
            raise MiddlewareOrderError(
                "the composed middleware order violates a declared semantic "
                "ordering:\n  " + "\n  ".join(violations)
            )

    def _effective_positions(self) -> dict[str, tuple[int, str]]:
        """``name -> (visit_index, phase)`` in the order :meth:`check` visits.

        Built by replaying the same phase-then-chain walk :meth:`check` performs,
        so the number compared here is the number that governs execution. A
        chain whose appended order and visited order disagree is precisely the
        case a phase table exists to catch.
        """
        positions: dict[str, tuple[int, str]] = {}
        index = 0
        for phase in self._phases:
            for middleware in self._middlewares:
                if middleware.phase == phase:
                    positions[middleware.name] = (index, phase)
                    index += 1
        return positions

    # -- evaluation -------------------------------------------------------

    def check(self, turn: Turn, run: TurnRun, *, phase: str | None = None) -> Outcome:
        """Run every phase (or one named phase) and return the first decision.

        A ``Mutation`` does not stop the chain — it is applied and the next
        middleware sees the mutated turn, which is what lets auto-compaction
        reduce ``context_tokens`` and the context-exceeded policy downstream
        then agree that the turn is fine. A ``Refusal`` stops the chain: a later
        policy must not be able to overrule an earlier refusal by deciding the
        turn is acceptable, because the earlier one had the more specific
        reason.
        """
        phases = self._phases if phase is None else (phase,)
        current = turn
        considered: list[str] = []
        for phase_name in phases:
            for middleware in self._middlewares:
                if middleware.phase != phase_name:
                    continue
                try:
                    decision = middleware.evaluate(current, run)
                except Exception as exc:
                    # A policy that raises must not unwind the run into an
                    # unlabelled traceback. It becomes a charged termination
                    # that names the policy, which is the honest reading of
                    # "we asked this policy and it could not answer".
                    run.note(middleware.name, "raised")
                    return Outcome(
                        decision=Decision.terminate(
                            "failed",
                            f"policy {middleware.name!r} raised "
                            f"{type(exc).__name__}: {exc}",
                            policy=middleware.name,
                            turn=current.number,
                        ),
                        phase=phase_name,
                        turn=current,
                        considered=tuple(considered),
                        stopped_at=middleware.name,
                    )
                considered.append(middleware.name)
                if isinstance(decision, Mutation):
                    current = self._apply(current, decision)
                    run.note(middleware.name, "mutate")
                    continue
                if isinstance(decision, Observation):
                    run.observations.append(decision)
                    run.note(middleware.name, "observe")
                    continue
                if isinstance(decision, Refusal):
                    run.refusals.append(decision)
                    run.note(middleware.name, "refuse")
                    return Outcome(
                        decision=decision,
                        phase=phase_name,
                        turn=current,
                        considered=tuple(considered),
                        stopped_at=middleware.name,
                    )
                if isinstance(decision, Termination):
                    run.terminations.append(decision)
                    run.note(middleware.name, "terminate")
                    return Outcome(
                        decision=decision,
                        phase=phase_name,
                        turn=current,
                        considered=tuple(considered),
                        stopped_at=middleware.name,
                    )
                # An unrecognised return is a policy bug. Fail closed and loud
                # rather than reading it as "no opinion", which would let a
                # policy silently gate nothing.
                run.note(middleware.name, "invalid")
                return Outcome(
                    decision=Decision.terminate(
                        "failed",
                        f"policy {middleware.name!r} returned "
                        f"{type(decision).__name__}, which is not a Decision",
                        policy=middleware.name,
                        turn=current.number,
                    ),
                    phase=phase_name,
                    turn=current,
                    considered=tuple(considered),
                    stopped_at=middleware.name,
                )
        return Outcome(decision=None, phase=phases[-1] if phases else "", turn=current, considered=tuple(considered))

    def _apply(self, turn: Turn, mutation: Mutation) -> Turn:
        """Apply a mutation, rejecting undeclared fields.

        Rejecting an unknown key is the point: a policy that writes
        ``turn.some_flag = True`` through the scratch dict it does not own would
        otherwise create a field a later middleware reads and trusts.
        """
        allowed = set(Turn.__dataclass_fields__)
        unknown = sorted(set(mutation.changes) - allowed)
        if unknown:
            raise MiddlewareOrderError(
                f"a middleware tried to mutate undeclared turn fields {unknown}; "
                f"declared fields are {sorted(allowed)}"
            )
        return replace(turn, **dict(mutation.changes))

    # -- receipts ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """A receipt of the composed chain and the order it asserts."""
        return {
            "phases": list(self._phases),
            "names": list(self.names),
            "by_phase": {phase: list(self.names_in_phase(phase)) for phase in self._phases},
            "can_stop": {m.name: bool(getattr(m, "can_stop", False)) for m in self._middlewares},
            "semantic_orderings": [rule.describe() for rule in SEMANTIC_ORDERINGS],
            "policy_count": len(self._middlewares),
        }

    def __len__(self) -> int:
        return len(self._middlewares)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"TurnPolicyPipeline({list(self.names)!r})"


def pipeline_report(pipeline: TurnPolicyPipeline, run: TurnRun | None = None) -> dict[str, Any]:
    """The chain receipt plus, when supplied, the run's own receipt."""
    payload = pipeline.describe()
    payload["run"] = run.receipt() if run is not None else None
    return payload
