"""AGT-11 — the agent loop as ONE pure function: ``step(history, tools, config)``.

The problem this module exists to solve is not a bug. It is that the loop's
*decisions* and its *effects* were interleaved in a single 1,255-line function,
so the only way to ask "what would the loop do here?" was to run it against a
real repository, a real sandbox, a real provider and a real clock. Every
behaviour in the matrix below — a doom loop, an unparseable reply, an approver
refusal, a network that is down — cost a subprocess to observe.

OpenHands' insight is worth taking and its infrastructure is not: extract the
decision function, inject every effect, and leave the durable record alone.

**The shape.** One function:

    step(history, tools, config) -> events

* ``history`` is the append-only conversation so far. It is read, never
  written: every turn appends to a *local* list and the caller's object is
  untouched, so a caller can replay the same prefix for free.
* ``tools`` is the entire injected boundary. It carries the model, every tool,
  and the environment readings the loop needs (elapsed seconds, spend so far,
  cancellation, pending steering, the approver). **Nothing is read from a
  global, a module, or the environment.** The clock arrives as a number
  through ``tools.elapsed_s()``; that is what "no clock" means here.
* ``config`` is a frozen :class:`StepConfig`, read by key meaning, never by
  truthiness of an absent key.
* ``events`` is a list of ``{"kind": str, "data": dict}`` — the same shape
  ``harness.trace.TraceLogger.log`` already writes, so the adapter's whole
  persistence job is ``for e in events: trace.log(e["kind"], e["data"])``.
  Exactly ONE of them, ``task_end``, is terminal, and it is always last.

**What this function will not do.** It does not read a clock, open a socket,
touch the filesystem, spawn a process, consult the clock implicitly through a
helper, or mutate anything it was handed. :func:`step` is a total function of
its three arguments: the same triple always produces the same list. That is
pinned four ways in ``tests/test_agent_loop_matrix.py`` — an import pin, a
module-global pin, a determinism pin, and a poisoned-clock pin — because "it
looks pure" is not evidence that it is.

**The verifier gate is not this module's to weaken, and it is enforced here
structurally.** The terminal status vocabulary is
:data:`shared.agent_contracts.RUN_STATUSES`, which contains no bare "the run
worked" word at all. :func:`status_is_success` is true for exactly one value,
``completed_verified``, and that value is reachable from exactly one place: a
declared verifier returned clean evidence. A run that finished with no
declared tests is ``completed_unverified`` — a real, reportable, non-fabricated
outcome — and this module contains no string literal that could dress it as
anything else. The purity of the function is what makes that checkable: a
verifier gate whose inputs are all injected is a gate a test can drive.

**Durability is not rebuilt here.** The append-only ``logs/{task_id}/
trace.jsonl`` remains the boundary and remains the authority for resume; this
module produces the events that get appended to it, and reads back nothing. A
persistence rewrite is a different round.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness import tool_errors, turn_caps
from shared.agent_contracts import RUN_STATUSES, CompletionStatus

__all__ = [
    "APPROVAL_TOOLS",
    "DONE_TOOL",
    "EVENT_KINDS",
    "READ_ONLY_TOOLS",
    "TERMINAL_KIND",
    "VERIFIER_TOOL",
    "HistoryEntry",
    "LoopEnvironment",
    "StepConfig",
    "ToolOutcome",
    "completion_status",
    "is_completed",
    "normalize_history",
    "parse_tool_call",
    "status_is_success",
    "step",
    "terminal_status_of",
]


# ---------------------------------------------------------------------------
# The reply protocol (pure; moved here from harness.agent_loop so the decision
# function owns its own grammar and the adapter can only re-export it)
# ---------------------------------------------------------------------------

TOOL_NAMES = (
    "read",
    "glob",
    "grep",
    "bash",
    "edit",
    "write",
    "memory",
    "fetch",
    "mcp",
    "mcp_call",
    "verify",
    "done",
)

PLAIN_PATTERNS = (
    ("read", re.compile(r"^\s*READ\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("glob", re.compile(r"^\s*GLOB\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("grep", re.compile(r"^\s*GREP\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("bash", re.compile(r"^\s*BASH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("memory", re.compile(r"^\s*MEMORY\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("fetch", re.compile(r"^\s*FETCH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("verify", re.compile(r"^\s*VERIFY\s*$", re.IGNORECASE)),
    ("done", re.compile(r"^\s*DONE(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)),
)

#: Which plain-text argument name each single-argument verb carries. Derived
#: from the grammar above so the pattern table and this one cannot disagree.
#: A read-only mapping, not a dict: this table is consulted on every reply, and
#: a dict a caller could mutate is a way to change the grammar at runtime.
_ARG_OF = MappingProxyType(
    {
        "read": "path",
        "glob": "pattern",
        "memory": "query",
        "fetch": "url",
    }
)

#: The control verb. It ends the run; it is never dispatched as a tool.
DONE_TOOL = "done"

#: The verifier verb. It is a real dispatch, and its result is the only thing
#: that can mint a verified completion.
VERIFIER_TOOL = "verify"

#: Verbs that need a human before they run. Identical to the legacy loop's set
#: — a second, narrower list here would be a gate that silently misses a verb.
APPROVAL_TOOLS = frozenset({"bash", "edit", "write", "mcp", "mcp_call"})

#: Verbs that cannot change anything. Exempt from the repeat guard, because
#: re-reading a file after your own edit is exploration, not a doom loop.
READ_ONLY_TOOLS = frozenset({"read", "glob", "grep", "memory", "fetch"})

#: The one terminal event kind. It is always last and always appears once.
TERMINAL_KIND = "task_end"

#: Every event kind :func:`step` can emit. A closed set, so a test can assert
#: the vocabulary and a new kind has to be added on purpose.
EVENT_KINDS = frozenset(
    {
        "approval_decided",
        "approval_required",
        "environment_unavailable",
        "loop_guard",
        "model_failure",
        "model_recovery",
        "policy_refused",
        "reflection",
        "steering_delivered",
        "steering_abort",
        "stop",
        TERMINAL_KIND,
        "tool_call",
        "tool_recovery",
        "tool_result",
        "verify",
        "verify_skipped",
    }
)


def strip_fences(text: str) -> str:
    """Return one model reply with a wrapping code fence removed.

    Assumes nothing about the caller: a non-string, a ``None``, and text with
    no fence all return text. Never raises.
    """
    try:
        body = (text or "").strip()
        match = re.match(
            r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", body, re.DOTALL | re.IGNORECASE
        )
        return match.group(1).strip() if match else body
    except Exception:  # pragma: no cover — defensive
        return ""


def parse_tool_call(
    text: str, known_verbs: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """Parse ONE model reply into ``{"tool": name, **args}``.

    Accepts the JSON form (bare or fenced) and the plain-text one-line form.
    ``known_verbs`` (installed plugin verbs, from the session config) lets
    ``{"tool": "<verb>", ...}`` through; routing and effect checks happen at
    execution, not here. Returns ``None`` when the reply is not a recognizable
    tool call. Never raises and never performs I/O.

    A reply that is not a tool call is not an error condition in itself — the
    loop reflects on it — so this returns a value rather than raising.
    """
    try:
        body = strip_fences(text)
        if not body:
            return None
        if body.lstrip().startswith("{"):
            obj = None
            try:
                obj = json.loads(re.search(r"\{.*\}", body, re.DOTALL).group(0))
            except (ValueError, AttributeError, TypeError):
                obj = None
            if isinstance(obj, dict):
                tool = str(obj.get("tool") or "").strip().lower()
                extra = {str(v).lower() for v in (known_verbs or []) if str(v).strip()}
                if tool in TOOL_NAMES or tool in extra:
                    out: Dict[str, Any] = {"tool": tool}
                    for key, value in obj.items():
                        if key != "tool":
                            out[key] = value
                    return out
        for name, pattern in PLAIN_PATTERNS:
            match = pattern.match(body)
            if not match:
                continue
            if name == "grep":
                rest = match.group(1).strip()
                parts = rest.split(None, 1)
                if len(parts) == 2 and not parts[1].startswith("-"):
                    return {
                        "tool": "grep",
                        "pattern": parts[0].strip("\"'"),
                        "path": parts[1].strip(),
                    }
                return {"tool": "grep", "pattern": rest.strip("\"'")}
            if name in ("read", "glob", "memory", "fetch"):
                return {
                    "tool": name,
                    _ARG_OF[name]: match.group(1).strip().strip("`\"'"),
                }
            if name == "bash":
                return {"tool": "bash", "command": match.group(1).strip()}
            if name == "verify":
                return {"tool": "verify"}
            return {"tool": "done", "answer": (match.group(1) or "").strip()}
        return None
    except Exception:  # pragma: no cover — defensive
        return None


# ---------------------------------------------------------------------------
# Status vocabulary — the honest one, re-exported rather than re-declared
# ---------------------------------------------------------------------------


def status_is_success(status: Any) -> bool:
    """Whether ``status`` is the one value this project calls a clean pass.

    True for exactly ``completed_verified`` and nothing else. It is a function
    rather than a set membership test so a caller cannot accidentally build a
    broader truth set from the surrounding vocabulary, and it is the function
    the CLI, the matrix and the honesty probes are expected to share.
    """
    return str(status) == CompletionStatus.COMPLETED_VERIFIED.value


def is_completed(status: Any) -> bool:
    """Whether the run reached a completed state, verified or not.

    Deliberately separate from :func:`status_is_success`: "the run finished" and
    "the run was proven correct" are different questions and this repository
    has been bitten by conflating them.
    """
    return str(status) in {
        CompletionStatus.COMPLETED_VERIFIED.value,
        CompletionStatus.COMPLETED_UNVERIFIED.value,
    }


def completion_status(status: Any) -> str:
    """Return ``status`` as a canonical :data:`RUN_STATUSES` value.

    Narrows anything unrecognised to ``failed`` — a status nobody can read is
    not a status, and it is certainly not a pass.
    """
    text = str(status or "").strip()
    return text if text in RUN_STATUSES else CompletionStatus.FAILED.value


def terminal_status_of(events: Sequence[Mapping[str, Any]]) -> str:
    """Return the terminal status recorded in ``events``.

    Falls back to ``failed`` when the list carries no ``task_end``, because a
    run that stopped without saying why has not been verified to have done
    anything.
    """
    for event in reversed(list(events or [])):
        if str(event.get("kind") or "") == TERMINAL_KIND:
            data = event.get("data")
            return completion_status(
                (data or {}).get("status") if isinstance(data, Mapping) else None
            )
    return CompletionStatus.FAILED.value


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryEntry:
    """One immutable turn of the conversation.

    ``role`` is the model's vocabulary (``system``/``user``/``assistant``/
    ``tool``) because that is what the model boundary is handed. ``tool``,
    ``ok`` and ``turn`` are bookkeeping a trace reader wants; none of them is
    required, and none of them changes what the model sees.
    """

    role: str
    content: str
    turn: int = 0
    tool: str = ""
    ok: Optional[bool] = None

    def as_message(self) -> Dict[str, str]:
        """The exact mapping handed to the model boundary."""
        return {"role": self.role, "content": self.content}


def normalize_history(history: Any) -> List[HistoryEntry]:
    """Coerce anything history-shaped into a list of :class:`HistoryEntry`.

    Accepts entries, mappings, or a bare string (treated as user text, which
    is how a first request arrives). Never raises, never mutates the input,
    and never returns a shared object — the caller owns the result. A
    resume prefix read back from ``trace.jsonl`` is therefore an ordinary
    history, with no special case anywhere in :func:`step`.
    """
    out: List[HistoryEntry] = []
    for item in list(history or []):
        if isinstance(item, HistoryEntry):
            out.append(item)
            continue
        if isinstance(item, Mapping):
            out.append(
                HistoryEntry(
                    role=str(item.get("role") or "user"),
                    content=str(item.get("content") or ""),
                    turn=int(item.get("turn") or 0),
                    tool=str(item.get("tool") or ""),
                    ok=item.get("ok"),
                )
            )
            continue
        text = str(item or "")
        if text:
            out.append(HistoryEntry(role="user", content=text))
    return out


# ---------------------------------------------------------------------------
# The injected boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOutcome:
    """What one injected tool call returned.

    ``kind`` is the already-classified failure slug when the caller has one
    (the real adapter gets it from the executor); when it is empty, :func:`step`
    classifies the output itself through the one shared classifier. ``exit_code``
    and ``timed_out`` exist so a caller that only has an execution result does
    not have to pre-classify. ``detail`` is free-form structured evidence for
    the trace — the file a verifier ran against, a URL, an error slug.
    """

    ok: bool = True
    output: str = ""
    kind: str = ""
    exit_code: int = 0
    timed_out: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """The JSON-safe projection carried on the ``tool_result`` event."""
        data: Dict[str, Any] = {
            "ok": bool(self.ok),
            "output": str(self.output or ""),
        }
        if self.kind:
            data["kind"] = str(self.kind)
        if self.detail:
            data["detail"] = dict(self.detail)
        return data


class LoopEnvironment:
    """Base for the injected boundary: the two required capabilities, and
    honest defaults for everything else.

    Subclass and override what your run actually has. The defaults are chosen
    in the safe direction, and each one says why:

    * ``elapsed_s`` → ``0.0``: no wall clock, so no timeout. A run that has no
      clock cannot time out, and pretending otherwise would be the loop
      inventing a limit it was never given.
    * ``spent_usd`` → ``0.0``: no meter, so no budget stop.
    * ``cancelled`` → ``False``: a cancel nobody asked for must not fire.
    * ``approve`` → ``False``: **fail closed.** No approver is a refusal, never
      a silent yes. This is the one default that could have been convenient in
      the other direction and is not.
    * ``steering`` → an empty delivery: no inbox, nothing typed.

    ``ask`` and ``invoke`` have no default and must be supplied.
    """

    def ask(self, messages: Sequence[Mapping[str, str]], *, step: str) -> str:
        """Return ONE model reply for ``messages``. Required."""
        raise NotImplementedError("the injected boundary must implement ask()")

    def invoke(self, name: str, args: Mapping[str, Any], *, turn: int) -> ToolOutcome:
        """Run ONE tool and return its outcome. Required."""
        raise NotImplementedError("the injected boundary must implement invoke()")

    def elapsed_s(self) -> float:
        """Seconds since the run started. Injected: this module reads no clock."""
        return 0.0

    def spent_usd(self) -> float:
        """Spend so far, in dollars. Injected: this module reads no meter."""
        return 0.0

    def cancelled(self) -> bool:
        """Whether a human asked this run to stop. Injected."""
        return False

    def approve(self, name: str, args: Mapping[str, Any], *, turn: int) -> bool:
        """Whether a human approved this call. Fail-closed by default."""
        return False

    def steering(self, where: str, *, turn: int, tool: str = "") -> Any:
        """Consume and return the pending steering at this checkpoint.

        The return value is duck-typed against ``harness.steering.
        SteeringDelivery`` (``.empty``, ``.action()``, ``.texts``, ``.seqs``),
        so this module does not import the steering module and a caller may
        return any object with that shape. ``None`` means "nothing arrived".
        """
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int, floor: int = 0) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > floor else floor


def _as_float(value: Any, default: float, floor: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > floor else floor


def _as_tuple(value: Any) -> Tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(v) for v in value if str(v).strip())
    except TypeError:
        return ()


@dataclass(frozen=True)
class StepConfig:
    """Every bound and switch :func:`step` reads. Frozen, and read by meaning.

    The names are the harness's own config keys, because a bound that lives
    under a different name in the loop and in the config table is a bound
    nobody can change from a settings file.

    ``max_turns`` is the one bound with no off switch: a loop that cannot be
    asked to stop is the failure this whole round is about, and ``0`` is
    coerced to ``1`` rather than honoured.
    """

    max_turns: int = 25
    max_wallclock_s: float = 900.0
    budget_cap_usd: float = 2.0
    verifier_declared: bool = False
    target_test: str = ""
    test_command: str = ""
    approval_mode: str = "auto"
    max_reflection_per_step: int = 3
    max_reflection_per_run: int = 12
    max_repeat_tool_calls: int = 3
    loop_guard_read_only: bool = False
    max_output_chars: int = 4000
    plugin_verbs: Tuple[str, ...] = ()
    known_verbs: Tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]] = None) -> "StepConfig":
        """Build a config from a merged task config, by key meaning.

        An absent key takes this class's default; an unusable value degrades to
        the default rather than to infinity, because an unbounded bound is not
        a bound. ``max_wallclock_s=0`` and ``budget_cap_usd=0`` are NOT
        "unbounded" — they clamp to the floor — so the only way to run without
        those two limits is to not check them, and this class always checks
        them. Never raises.
        """
        cfg: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        declared = bool(str(cfg.get("target_test") or "").strip()) or bool(
            str(cfg.get("test_command") or "").strip()
        )
        approval = str(cfg.get("agent_approval") or cfg.get("approval") or "auto")
        return cls(
            max_turns=turn_caps.resolve_caps(cfg).per_task or 1,
            max_wallclock_s=_as_float(cfg.get("max_wallclock_s"), 900.0),
            budget_cap_usd=_as_float(cfg.get("budget_cap_usd"), 2.0),
            verifier_declared=declared,
            target_test=str(cfg.get("target_test") or ""),
            test_command=str(cfg.get("test_command") or ""),
            approval_mode=approval if approval in ("auto", "require") else "auto",
            max_reflection_per_step=_as_int(
                cfg.get("reflection_max_per_step"), 3, floor=0
            )
            or 3,
            max_reflection_per_run=_as_int(
                cfg.get("reflection_max_per_run"), 12, floor=0
            )
            or 12,
            max_repeat_tool_calls=_as_int(cfg.get("max_repeat_tool_calls"), 3, floor=0)
            or 3,
            loop_guard_read_only=bool(cfg.get("loop_guard_read_only", False)),
            max_output_chars=_as_int(cfg.get("agent_max_output_chars"), 4000, 200),
            plugin_verbs=_as_tuple(cfg.get("plugin_verbs")),
            known_verbs=_as_tuple(cfg.get("known_verbs")),
        )

    @classmethod
    def coerce(cls, config: Any) -> "StepConfig":
        """Accept a :class:`StepConfig`, a mapping, or ``None``."""
        if isinstance(config, cls):
            return config
        if isinstance(config, Mapping):
            return cls.from_config(config)
        return cls()


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------


def _event(event_kind: str, /, **data: Any) -> Dict[str, Any]:
    """One event in the shape ``harness.trace.TraceLogger.log`` already takes.

    The kind is POSITIONAL-ONLY on purpose. Several event payloads legitimately
    carry a `kind` of their own — a classified failure slug, a verification
    answer — and a signature that let `kind` land in `**data` would make those
    calls a `TypeError` at runtime rather than a reviewable line of code.
    """
    return {"kind": str(event_kind), "data": dict(data)}


def _bounded(text: Any, limit: int) -> str:
    body = str(text or "")
    if limit <= 0 or len(body) <= limit:
        return body
    return tool_errors.shape_tool_output(body, limit)


def _fingerprint(name: str, args: Mapping[str, Any]) -> str:
    """A stable identity for one call, for the repeat guard.

    JSON with sorted keys, so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` are the
    same call. Deterministic by construction — no hashing, no clock, nothing to
    make two identical calls look different.
    """
    try:
        payload = json.dumps(
            {str(k): args[k] for k in sorted(args, key=str)},
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
    except (TypeError, ValueError):  # pragma: no cover — default=str covers it
        payload = repr(sorted((str(k), str(v)) for k, v in args.items()))
    return f"{name}:{payload}"


def _read_only(name: str, args: Mapping[str, Any]) -> bool:
    """Whether a call can be repeated without changing anything.

    Read verbs are exempt. A shell command is judged by the ONE shared
    predicate rather than by a list here, because a second command-safety
    table is a second opinion about the same command — and the legacy round
    already paid for the list version of this rule by refusing a legitimate
    re-read.
    """
    if name in READ_ONLY_TOOLS:
        return True
    if name == "bash":
        return tool_errors.is_read_only_command(str(args.get("command") or ""))
    return False


def _describe(args: Mapping[str, Any]) -> str:
    """The one-line rendering of a call's arguments, for the event and the
    reflection signature. Never a summary of the RESULT — the result is
    carried verbatim elsewhere."""
    for key in ("path", "command", "pattern", "query", "url", "name"):
        if args.get(key):
            return f"{key}={str(args[key])[:200]}"
    return " ".join(f"{k}={str(v)[:60]}" for k, v in sorted(args.items()))[:200]


def step(
    history: Any,
    tools: Any,
    config: Any = None,
) -> List[Dict[str, Any]]:
    """Run the agent loop to its terminal event and return every event.

    This is the whole decision function. It is a total function of its three
    arguments — the same ``(history, tools, config)`` always yields the same
    list — and it performs no I/O of its own: the model, the tools, the clock,
    the meter, the cancel flag, the approver and the steering inbox all arrive
    through ``tools``.

    Assumes about its inputs:

    * ``history`` may be entries, mappings, or strings (see
      :func:`normalize_history`); it is read and never written.
    * ``tools`` implements :class:`LoopEnvironment` — ``ask`` and ``invoke``
      are required, the rest have safe defaults. An exception from either is
      treated as a FAILURE of the run, never as a reason to fabricate an
      outcome.
    * ``config`` may be a :class:`StepConfig`, a merged task config mapping, or
      ``None``.

    Guarantees about its output:

    * The last event is ``task_end`` and it appears exactly once.
    * Every kind is in :data:`EVENT_KINDS`.
    * ``data["status"]`` on the terminal event is a member of
      :data:`shared.agent_contracts.RUN_STATUSES`.
    * ``completed_verified`` appears only when a declared verifier returned
      clean evidence, and :func:`status_is_success` is false for every other
      terminal status — including a run that finished with no declared tests.

    Never raises. A failure inside the loop is a terminal status with a reason,
    because a crash is the one thing that must not be indistinguishable from a
    decision.
    """
    cfg = StepConfig.coerce(config)
    events: List[Dict[str, Any]] = []
    entries: List[HistoryEntry] = normalize_history(history)

    # A local reflection budget. The CLASSIFICATION is the shared one
    # (`tool_errors.retry_class_of`); only the counter is local, because the
    # shared budget object is a mutable accumulator and this function must not
    # reach outside its arguments for one.
    reflections = tool_errors.ReflectionBudget(
        per_step=cfg.max_reflection_per_step,
        per_run=cfg.max_reflection_per_run,
        max_evidence_chars=cfg.max_output_chars,
        trace=None,
        label="agent-step",
    )

    repeats: Dict[str, int] = {}
    status = CompletionStatus.FAILED.value
    end_reason = ""
    answer = ""
    verification: Optional[Dict[str, Any]] = None
    files: List[str] = []
    turn = 0

    def stop(new_status: str, reason: str) -> None:
        """Record the terminal status and the reason that produced it.

        The loop breaks at every call site, so the terminal status has exactly
        one writer and the `task_end` event below has exactly one source. A
        status this function cannot read is narrowed to ``failed``: an
        unrecognised outcome is not a pass.
        """
        nonlocal status, end_reason
        status = completion_status(new_status)
        end_reason = str(reason or "")

    def bounded_message(text: str, role: str = "user", **extra: Any) -> None:
        entries.append(HistoryEntry(role=role, content=text, turn=turn, **extra))

    def reflect(
        kind: str, evidence: str, signature: str
    ) -> Tuple[Dict[str, Any], bool]:
        """Record ONE failure and answer two questions about it.

        Returns ``(reflection, may_continue)``. The second value is False ONLY
        when a CAP bound the decision, which is the single case where the run
        must stop and name the reason.

        It is deliberately NOT ``reflection.allowed``. That flag answers a
        different question -- "may this failure be handed back to the model as
        an invitation to try again?" -- and it is False for a non-retryable
        failure, which is correct: a refusal must not read as a retry
        suggestion. But a refusal still has to be REPORTED and the loop still
        has to continue, because the correct next move after being told no is a
        DIFFERENT one. Conflating the two flags ends the run on the first
        policy refusal, which turns a user saying no into a failed task.
        """
        step_id = f"agent-{turn}"
        decision = reflections.note(
            kind, evidence=evidence, signature=signature, step_id=step_id
        )
        events.append(_event("reflection", **decision.to_dict()))
        return decision.to_dict(), not bool(decision.exhausted)

    # -- the loop ----------------------------------------------------------

    while turn < cfg.max_turns:
        turn += 1

        # 1. Wall clock. Injected: a number in, a comparison out. Read once —
        #    asking the boundary twice could return two different answers and
        #    make the receipt disagree with the decision.
        elapsed = float(tools.elapsed_s() or 0.0)
        if cfg.max_wallclock_s and elapsed >= cfg.max_wallclock_s:
            events.append(
                _event("stop", reason="wall clock", elapsed_s=round(elapsed, 3))
            )
            stop(CompletionStatus.TIMEOUT, "wall-clock limit reached")
            break

        # 2. Spend. Same one-read rule, for the same reason.
        spent = float(tools.spent_usd() or 0.0)
        if cfg.budget_cap_usd and spent >= cfg.budget_cap_usd:
            events.append(
                _event("stop", reason="budget cap", spent_usd=round(spent, 6))
            )
            stop(CompletionStatus.FAILED, "budget cap exceeded")
            break

        # 3. Cancellation. Checked before the model so a cancel cannot spend a
        #    turn it was never going to be allowed to finish.
        if bool(tools.cancelled()):
            stop(CompletionStatus.CANCELLED, "cancelled before the model call")
            break

        # 4. Steering checkpoint. The strongest intent wins; a replan spends no
        #    model call, because the turn it would have been spent on is now
        #    known to be the wrong turn.
        delivery = tools.steering("agent-turn", turn=turn)
        action = _steering_action(delivery)
        if action and action != "none":
            events.append(
                _event(
                    "steering_delivered",
                    at=f"agent-turn-{turn}",
                    action=action,
                    seqs=list(_steering_field(delivery, "seqs")),
                    texts=list(_steering_field(delivery, "texts")),
                )
            )
            if action == "abort":
                events.append(
                    _event("steering_abort", turn=turn, at=f"agent-turn-{turn}")
                )
                stop(CompletionStatus.CANCELLED, f"aborted by steering at turn {turn}")
                break
            bounded_message(
                f"USER STEERING ({action}) before turn {turn}: "
                + " | ".join(_steering_texts(delivery))
            )
            if action == "replan":
                continue

        # 5. The model call.
        try:
            reply = str(tools.ask(_as_messages(entries), step=f"agent-{turn}") or "")
        except (KeyboardInterrupt, SystemExit):
            # A cancel is not a provider fault. It is re-raised so the caller's
            # own interrupt handling owns it, exactly as the legacy loop does.
            raise
        except Exception as exc:
            failure = tool_errors.classify_model_failure(exc)
            events.append(
                _event(
                    "model_failure",
                    turn=turn,
                    kind=failure.kind,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_class=tool_errors.retry_class_of_model_failure(failure),
                )
            )
            if not failure.retryable:
                stop(
                    CompletionStatus.FAILED,
                    f"terminal model failure ({failure.kind}) at turn {turn}",
                )
                break
            events.append(
                _event("model_recovery", turn=turn, action="retry", kind=failure.kind)
            )
            _recorded, may_continue = reflect(
                failure.kind, f"{type(exc).__name__}: {exc}", "model"
            )
            if not may_continue:
                stop(
                    CompletionStatus.FAILED,
                    "the model call exhausted its reflection budget",
                )
                break
            bounded_message(
                f"MODEL CALL FAILED [{failure.kind}]: {type(exc).__name__}: {exc}"
            )
            continue
        bounded_message(reply, role="assistant")

        # 6. Parse. A reply the parser cannot read is reflected on, not fatal
        #    on its own: one unreadable reply is a normal event in a real run.
        call = parse_tool_call(reply, list(cfg.known_verbs) + list(cfg.plugin_verbs))
        if call is None:
            events.append(
                _event(
                    "tool_recovery",
                    turn=turn,
                    kind=tool_errors.KIND_MALFORMED_TOOL_CALL,
                    action=tool_errors.POLICY.get(
                        tool_errors.KIND_MALFORMED_TOOL_CALL, ("", "")
                    )[0],
                )
            )
            _recorded, may_continue = reflect(
                tool_errors.KIND_MALFORMED_TOOL_CALL,
                _bounded(reply, cfg.max_output_chars),
                "parse",
            )
            if not may_continue:
                stop(
                    CompletionStatus.FAILED,
                    "the tool-call parse exhausted its reflection budget",
                )
                break
            bounded_message(
                tool_errors.render_reflection(
                    reflections.last,
                    header="Your reply was not a tool call.",
                )
            )
            continue

        name = str(call.get("tool") or "")
        args: Dict[str, Any] = {k: v for k, v in call.items() if k != "tool"}
        events.append(
            _event("tool_call", turn=turn, tool=name, command=_describe(args))
        )

        # 7. DONE. A control verb, not a tool: it ends the run and is never
        #    dispatched. The verifier gate is entirely below this branch, and
        #    it is the ONLY place a run can become verified.
        if name == DONE_TOOL:
            answer = str(args.get("answer") or "").strip()
            if not cfg.verifier_declared:
                events.append(
                    _event(
                        "verify_skipped",
                        reason="no tests are declared for this task, so nothing was verified",
                        status=CompletionStatus.COMPLETED_UNVERIFIED.value,
                    )
                )
                status = CompletionStatus.COMPLETED_UNVERIFIED.value
                end_reason = "the model finished; no verifier was declared"
            else:
                evidence = _run_verifier(tools, cfg, turn, events)
                verification = evidence
                clean = _evidence_is_clean(evidence)
                status = (
                    CompletionStatus.COMPLETED_VERIFIED.value
                    if clean
                    else CompletionStatus.FAILED.value
                )
                end_reason = (
                    "the model finished and the declared verifier was clean"
                    if clean
                    else f"the declared verifier was not clean: {evidence.get('error') or 'see the verify event'}"
                )
            break

        # 8. Approval. Required only in `require` mode, and only for the verbs
        #    that can change something. A denial is NOT a dispatch and NOT a
        #    completion: the call does not run, and the model is told why.
        if cfg.approval_mode == "require" and name in APPROVAL_TOOLS:
            events.append(
                _event(
                    "approval_required", turn=turn, tool=name, command=_describe(args)
                )
            )
            approved = bool(tools.approve(name, args, turn=turn))
            events.append(
                _event(
                    "approval_decided",
                    turn=turn,
                    tool=name,
                    approved=approved,
                    reason="" if approved else "the approver refused this call",
                )
            )
            if not approved:
                # A refusal is non-retryable and therefore FREE: a user saying
                # no must never be usable to exhaust the run's recovery budget.
                _recorded, may_continue = reflect(
                    tool_errors.KIND_COMMAND_REJECTED,
                    f"the approver refused {name} {_describe(args)}",
                    f"approve:{name}",
                )
                if not may_continue:
                    stop(
                        CompletionStatus.FAILED,
                        "the approval refusals exhausted the budget",
                    )
                    break
                bounded_message(
                    tool_errors.render_reflection(
                        reflections.last, header="The approver refused that call."
                    )
                )
                continue

        # 9. Repeat guard. Runs BEFORE dispatch, so the Nth identical call is
        #    never executed. Read-only calls are exempt by configuration.
        exempt = _read_only(name, args) and not cfg.loop_guard_read_only
        if not exempt:
            mark = _fingerprint(name, args)
            repeats[mark] = repeats.get(mark, 0) + 1
            if repeats[mark] > cfg.max_repeat_tool_calls:
                events.append(
                    _event(
                        "loop_guard",
                        turn=turn,
                        tool=name,
                        command=_describe(args),
                        repeats=repeats[mark],
                        threshold=cfg.max_repeat_tool_calls,
                    )
                )
                stop(
                    CompletionStatus.FAILED,
                    f"the same {name} call repeated {repeats[mark]} times at turn {turn}",
                )
                break

        # 10. Dispatch. Anything the tool raises is a failed CALL, never a
        #     failed process: the loop is the thing that survives.
        try:
            outcome = tools.invoke(name, args, turn=turn)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            outcome = ToolOutcome(
                ok=False,
                output=f"tool raised {type(exc).__name__}: {exc}",
                kind=tool_errors.KIND_INTERNAL_ERROR,
            )
        if not isinstance(outcome, ToolOutcome):
            outcome = ToolOutcome(ok=bool(outcome), output=str(outcome))
        events.append(
            _event(
                "tool_result",
                turn=turn,
                tool=name,
                ok=outcome.ok,
                output=_bounded(outcome.output, cfg.max_output_chars),
                **({"kind": outcome.kind} if outcome.kind else {}),
            )
        )
        for path in _paths_touched(outcome, args):
            if path not in files:
                files.append(path)

        if outcome.ok:
            reflections.note_success()
            bounded_message(
                f"{name.upper()} ok: {_bounded(outcome.output, cfg.max_output_chars)}",
                tool=name,
                ok=True,
            )
            continue

        # 11. A failed call becomes the next turn's input, classified by the
        #     one shared classifier, and capped by the shared budget.
        kind = _failure_kind(outcome, name, args)
        if tool_errors.is_environment_kind(kind):
            events.append(
                _event(
                    "environment_unavailable",
                    turn=turn,
                    tool=name,
                    kind=kind,
                    repairable=tool_errors.is_repairable_by_edit(kind),
                )
            )
        elif kind in (
            tool_errors.KIND_PERMISSION_DENIED,
            tool_errors.KIND_COMMAND_REJECTED,
        ):
            events.append(_event("policy_refused", turn=turn, tool=name, kind=kind))
        _recorded, may_continue = reflect(
            kind,
            _bounded(outcome.output, cfg.max_output_chars),
            f"{name}:{_describe(args)}",
        )
        if not may_continue:
            stop(
                CompletionStatus.FAILED,
                f"the {name} call exhausted its reflection budget at turn {turn}",
            )
            break
        bounded_message(
            tool_errors.render_reflection(
                reflections.last, header=f"The {name} call failed."
            ),
            tool=name,
            ok=False,
        )
    else:
        # The turn budget is the one cap with no off switch. Running out of it
        # is a failure with a named reason, never a quiet completion.
        stop(
            CompletionStatus.FAILED,
            f"the run used all {cfg.max_turns} turns without finishing",
        )

    # -- close -------------------------------------------------------------

    events.append(
        _event(
            TERMINAL_KIND,
            status=status,
            turns=turn,
            reason=end_reason,
            answer=answer,
            files_touched=files,
            verification=verification or {},
            reflection=reflections.report(),
        )
    )
    return events


# ---------------------------------------------------------------------------
# Helpers used by `step`; module-private, pure, and never re-entered
# ---------------------------------------------------------------------------


def _as_messages(entries: Sequence[HistoryEntry]) -> List[Dict[str, str]]:
    """The exact list handed to the model boundary. A fresh list every call, so
    a boundary that mutates what it was given cannot corrupt the run."""
    return [entry.as_message() for entry in entries]


def _run_verifier(
    tools: Any, cfg: StepConfig, turn: int, events: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Ask the injected verifier, and shape its answer into evidence.

    The one call in this module whose answer can mint a verified completion,
    so it is the one call whose failure mode matters: an exception is NOT a
    pass. It becomes evidence that is explicitly not clean, and the run ends
    ``failed`` — the narrow direction.
    """
    try:
        outcome = tools.invoke(VERIFIER_TOOL, {}, turn=turn)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        evidence = {
            "target_passed": False,
            "regression_passed": False,
            "flaky": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        events.append(_event("verify", turn=turn, **evidence))
        return evidence
    if not isinstance(outcome, ToolOutcome):
        outcome = ToolOutcome(ok=bool(outcome), output=str(outcome))
    detail = dict(outcome.detail or {})
    evidence = {
        "target_passed": bool(detail.get("target_passed", False)),
        "regression_passed": bool(detail.get("regression_passed", False)),
        "flaky": bool(detail.get("flaky", False)),
        "error": str(detail.get("error") or ""),
        "target_test": cfg.target_test,
        "test_command": cfg.test_command,
    }
    events.append(
        _event(
            "verify",
            turn=turn,
            raw=_bounded(outcome.output, cfg.max_output_chars),
            **evidence,
        )
    )
    return evidence


def _evidence_is_clean(evidence: Mapping[str, Any]) -> bool:
    """Whether verifier evidence proves a clean target AND regression pass.

    The same three-term conjunction the rest of this project mints
    `completed_verified` on, re-derived here rather than imported so this
    module has no dependency on a completion policy. An absent key is False,
    not a default: an evidence block that omits a term cannot be used to claim
    it.
    """
    if not evidence or evidence.get("error"):
        return False
    return bool(
        evidence.get("target_passed")
        and evidence.get("regression_passed")
        and not evidence.get("flaky")
    )


def _failure_kind(outcome: ToolOutcome, name: str, args: Mapping[str, Any]) -> str:
    """Classify one failed call with the ONE shared classifier.

    A caller that already classified the failure wins; otherwise the output is
    classified from the execution shape. A failed ``verify`` call is a
    verification failure regardless of what the runner printed, because that
    is the fact the loop needs to report.
    """
    if outcome.kind:
        return str(outcome.kind)
    if name == VERIFIER_TOOL:
        return tool_errors.KIND_VERIFICATION_FAILED
    return tool_errors.classify(
        int(outcome.exit_code or 0),
        str(outcome.output or ""),
        "",
        bool(outcome.timed_out),
        str(args.get("command") or ""),
    ).kind


def _paths_touched(outcome: ToolOutcome, args: Mapping[str, Any]) -> List[str]:
    """Repo-relative paths this call changed, from the outcome's own detail.

    Read from the injected outcome rather than re-derived, because the executor
    is the only thing that knows what it actually wrote.
    """
    out: List[str] = []
    for key in ("path", "paths", "files_touched"):
        value = (outcome.detail or {}).get(key)
        if isinstance(value, str) and value:
            out.append(value)
        elif isinstance(value, Sequence):
            out.extend(str(v) for v in value if str(v).strip())
    return out


def _steering_action(delivery: Any) -> str:
    """``abort``/``replan``/``guide``/``none`` from a duck-typed delivery."""
    if delivery is None or bool(getattr(delivery, "empty", True)):
        return "none"
    action = getattr(delivery, "action", None)
    if callable(action):
        try:
            return str(action() or "none")
        except Exception:  # pragma: no cover — defensive
            return "none"
    return str(action or "none")


def _steering_texts(delivery: Any) -> List[str]:
    return [str(t) for t in _steering_field(delivery, "texts")]


def _steering_field(delivery: Any, name: str) -> List[Any]:
    if delivery is None:
        return []
    value = getattr(delivery, name, None)
    if callable(value):
        try:
            value = value()
        except Exception:  # pragma: no cover — defensive
            return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value or [])
    except TypeError:  # pragma: no cover — defensive
        return []
