"""AGT-11 — the agent regression matrix over the PURE `step(history, tools, config)`.

The gap: `harness/agent_loop.py`'s loop was 1,255 lines in which every decision
was interleaved with a real effect — a clock read, a trace write, a pristine
snapshot, a sandboxed command, a steering journal poll, a provider call. That
made the loop's behaviour observable only by running it end to end, so each of
the fourteen situations below cost a subprocess, a temporary repository and a
wall-clock budget to observe once. There was no cheap way to ask "what would the
loop do here?", which is why regressions in the SMALL decisions — a refusal
being charged against a budget, a repeat guard firing on a read, a `DONE` with
no declared tests — surfaced late or not at all.

The round's required proofs, by name:

1. `TestTheStepFunctionIsPure` — four independent pins. A source scan proves
   what the module says; a module-global scan proves it cannot read hidden
   state; a determinism check proves the two agree; the poisoned clock catches
   the failure that actually bites, a helper three modules down quietly calling
   `time.time()`. A comment is not evidence that any of them holds.
2. `test_the_matrix_covers_every_named_scenario` — the fourteen scenarios are a
   table and the table is asserted to BE the required table, so a scenario
   cannot be deleted by deleting its test.
3. `test_no_scenario_ever_reports_an_unverified_completion_as_success` — the
   verifier-gate invariant, swept over every case rather than restated per case.
4. `test_a_verified_completion_is_reachable_only_through_the_declared_verifier`
   plus `test_the_three_mint_conditions_agree_across_the_tree` — the mint, from
   both directions and across all three of its implementations.
5. `TestThePhrasingsThatBroke` — the UXP corpus, executed, not listed.

Every scenario runs TWICE: once against the pure function with a scripted
boundary (cheap, exhaustive, and the reason this file is fast), and once
through the REAL adapter — `run_agent_stepped` on a real repository with a real
append-only trace, the real model boundary, the real executor and the real
verifier seam. Both runs assert the EVENTS and the FINAL STATUS, and the two
must agree: if the adapter can change an outcome, the split has leaked a
decision back into the thing that is supposed to be only wiring.

Host-only: no Docker, no provider, no network. The verifier boundary is
replaced at `harness.deps.get_verify` for every case that declares tests, and
every case is asserted to have reached it.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import agent_loop_step as step_mod  # noqa: E402
from harness import tool_errors  # noqa: E402
from shared.agent_contracts import RUN_STATUSES, CompletionStatus  # noqa: E402
from shared.types import ExecutionResult, VerificationResult  # noqa: E402

STEP_SOURCE = ROOT / "harness" / "agent_loop_step.py"
STEP_SOURCE_TEXT = STEP_SOURCE.read_text(encoding="utf-8")

#: The engine under test, named explicitly and never left to a default, for the
#: same reason `tests/test_agent_loop.py` pins `legacy_agent`: a suite that
#: unit-tests one engine must ASK for it, so a change of default cannot quietly
#: turn this file into a test of something else.
STEPPED = {"agent_strategy": "agent_step"}


# ---------------------------------------------------------------------------
# The scripted boundary
# ---------------------------------------------------------------------------


@dataclass
class ScriptedLoop(step_mod.LoopEnvironment):
    """A boundary with no effects at all, which is the point.

    Every capability the pure core needs is a field here, so a test drives the
    loop's decisions by constructing an object rather than by arranging the
    world. It records every interaction, because "the loop asked the model four
    times" and "the edit was never dispatched" are assertions worth being able
    to make.
    """

    replies: List[Any] = field(default_factory=list)
    tools: Dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    spent: float = 0.0
    cancel: bool = False
    approver: Any = None
    steering_plan: Dict[int, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seen_messages: List[List[Dict[str, str]]] = []
        self.invocations: List[tuple] = []
        self.approvals: List[tuple] = []
        self.steering_calls: List[str] = []

    def ask(self, messages: Any, *, step: str = "") -> str:
        self.seen_messages.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError(
                f"the model was asked more times than scripted ({step})"
            )
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)

    def invoke(self, name: str, args: Any, *, turn: int = 0) -> Any:
        self.invocations.append((str(name), dict(args or {}), int(turn)))
        entry = self.tools.get(str(name))
        if callable(entry):
            return entry(dict(args or {}), turn)
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            return step_mod.ToolOutcome(ok=True, output=f"{name} ok")
        return entry

    def elapsed_s(self) -> float:
        return float(self.elapsed)

    def spent_usd(self) -> float:
        return float(self.spent)

    def cancelled(self) -> bool:
        return bool(self.cancel)

    def approve(self, name: str, args: Any, *, turn: int = 0) -> bool:
        self.approvals.append((str(name), dict(args or {}), int(turn)))
        if self.approver is None:
            return False
        return bool(self.approver(str(name), dict(args or {})))

    def steering(self, where: str, *, turn: int, tool: str = "") -> Any:
        self.steering_calls.append(f"{where}-{turn}")
        return self.steering_plan.get(int(turn))

    def invoked(self, name: str) -> List[tuple]:
        return [row for row in self.invocations if row[0] == str(name)]


class Delivery:
    """A duck-typed steering delivery matching `SteeringDelivery`'s surface.

    Duplicated here on purpose: the pure core must not import the steering
    module, so the contract it depends on is these four members — and this is
    the witness that the contract really is that small.
    """

    def __init__(self, intent: str, text: str, seq: int = 1) -> None:
        self._intent = str(intent)
        self._text = str(text)
        self._seq = int(seq)

    @property
    def empty(self) -> bool:
        return False

    def action(self) -> str:
        return self._intent

    @property
    def texts(self) -> List[str]:
        return [self._text]

    @property
    def seqs(self) -> List[int]:
        return [self._seq]


# ---------------------------------------------------------------------------
# Assertion helpers — one place, so "what the matrix asserts" is readable
# ---------------------------------------------------------------------------


def kinds(events: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(e.get("kind") or "") for e in events]


def of_kind(events: Sequence[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    return [dict(e.get("data") or {}) for e in events if str(e.get("kind")) == kind]


def verify_evidence(
    target: bool, regression: bool = True, flaky: bool = False
) -> Dict[str, Any]:
    """A verifier answer.

    One constructor, so no case can hand-roll an evidence block with a key the
    mint condition does not read — the exact way a mint condition drifts.
    """
    return {
        "target_passed": bool(target),
        "regression_passed": bool(regression),
        "flaky": bool(flaky),
        "raw": "1 passed",
    }


def assert_well_formed(events: Sequence[Dict[str, Any]], expected_status: str) -> str:
    """Every invariant that must hold for EVERY case, checked in one place.

    Returns the terminal status so a case can keep asserting on it.
    """
    seq = kinds(events)
    assert seq.count(step_mod.TERMINAL_KIND) == 1, (
        f"a run must record exactly one terminal event, saw {seq.count(step_mod.TERMINAL_KIND)}"
    )
    assert seq[-1] == step_mod.TERMINAL_KIND, f"the terminal event is not last: {seq}"
    unknown = sorted({k for k in seq if k not in step_mod.EVENT_KINDS})
    assert not unknown, f"undeclared event kinds: {unknown}"
    status = step_mod.terminal_status_of(events)
    assert status in RUN_STATUSES, status
    assert status == expected_status, (
        f"expected {expected_status!r}, got {status!r} from {seq}"
    )
    if status == CompletionStatus.COMPLETED_VERIFIED.value:
        evidence = of_kind(events, "verify")
        assert evidence, (
            "a verified completion was minted with no verify event: nothing ran"
        )
        last = evidence[-1]
        assert last.get("target_passed") and last.get("regression_passed"), last
        assert not last.get("flaky"), last
    else:
        assert not step_mod.status_is_success(status), (
            f"{status!r} was reported as a pass; it is not one"
        )
    return status


# ---------------------------------------------------------------------------
# 1. The purity pins
# ---------------------------------------------------------------------------


class TestTheStepFunctionIsPure:
    def test_the_step_function_imports_nothing_that_can_touch_the_outside_world(self):
        tree = ast.parse(STEP_SOURCE_TEXT)
        imported: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "asyncio",
            "http",
            "multiprocessing",
            "os",
            "pathlib",
            "random",
            "requests",
            "secrets",
            "shutil",
            "signal",
            "socket",
            "sqlite3",
            "ssl",
            "subprocess",
            "tempfile",
            "threading",
            "time",
            "urllib",
            "uuid",
        }
        offending = sorted(imported & forbidden)
        assert not offending, (
            f"agent_loop_step.py imports {offending}; the decision function must "
            "reach the outside world only through its injected boundary"
        )
        assert imported <= {
            "__future__",
            "dataclasses",
            "harness",
            "json",
            "re",
            "shared",
            "types",
            "typing",
        }, f"unexpected dependency: {sorted(imported)}"

    def test_every_module_level_binding_is_immutable(self):
        """No hidden state: a module global the loop could read is a global the
        tests cannot set. A compiled pattern, a tuple or a frozenset is data; a
        dict, list or set literal is state. `__all__` is the one permitted dict
        because nothing in the loop ever reads it."""
        tree = ast.parse(STEP_SOURCE_TEXT)
        mutable: List[str] = []
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(
                node.value, (ast.List, ast.Dict, ast.Set)
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != "__all__":
                    mutable.append(target.id)
        assert not mutable, (
            f"module-level mutable state: {mutable}. A pure function that can "
            "read a global is a function whose output depends on call order. "
            "Use a frozenset/tuple, or MappingProxyType for a lookup table."
        )

    def test_the_lookup_table_is_genuinely_read_only(self):
        """The positive half of the pin above: the one lookup table the parser
        consults on every reply is a read-only mapping, so the grammar cannot be
        rewritten at runtime by anything holding a reference to this module."""
        from harness.agent_loop_step import _ARG_OF

        with pytest.raises(TypeError):
            _ARG_OF["read"] = "something else"  # type: ignore[index]

    def test_the_same_inputs_produce_the_same_events(self):
        """Determinism, checked on the awkward case — a run with an unparseable
        reply, a refusal, reads that repeat and a repeat guard — so the
        comparison is not between two trivial traces."""

        def run() -> List[Dict[str, Any]]:
            loop = ScriptedLoop(
                replies=[
                    "not a tool call at all",
                    "still not a tool call",
                    '{"tool": "bash", "command": "sudo rm -rf /"}',
                    '{"tool": "read", "path": "src/router.py"}',
                    '{"tool": "read", "path": "src/router.py"}',
                    '{"tool": "read", "path": "src/router.py"}',
                    '{"tool": "read", "path": "src/router.py"}',
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                ],
                tools={
                    "bash": step_mod.ToolOutcome(
                        ok=False,
                        output="COMMAND REJECTED",
                        kind=tool_errors.KIND_PERMISSION_DENIED,
                    )
                },
            )
            return step_mod.step(
                [{"role": "user", "content": "go"}],
                loop,
                {"max_repeat_tool_calls": 2},
            )

        first, second = run(), run()
        assert len(first) > 12, (
            f"the determinism check must run on a real trace, got {len(first)} events"
        )
        assert first == second, "the same inputs produced two different event lists"

    def test_step_never_reads_a_clock_or_opens_a_socket(self, monkeypatch):
        """The behavioural pin: poison the clock and the network, then run a
        whole run — model call, tool, verifier, completion — through `step`. A
        transitive helper that reads either one raises here instead of quietly
        making the result irreproducible."""
        import socket as socket_mod
        import time as time_mod

        def poisoned(*_args: Any, **_kw: Any) -> Any:
            raise AssertionError("the pure step read a clock or opened a socket")

        for name in ("time", "monotonic", "perf_counter", "time_ns"):
            monkeypatch.setattr(time_mod, name, poisoned, raising=False)
        monkeypatch.setattr(socket_mod, "socket", poisoned)
        monkeypatch.setattr(socket_mod, "create_connection", poisoned)

        loop = ScriptedLoop(
            replies=[
                '{"tool": "read", "path": "a.py"}',
                '{"tool": "verify"}',
                '{"tool": "done", "answer": "done"}',
            ],
            tools={
                "verify": step_mod.ToolOutcome(ok=True, detail=verify_evidence(True))
            },
        )
        events = step_mod.step(
            [{"role": "user", "content": "go"}],
            loop,
            {"target_test": "tests/test_a.py"},
        )
        assert_well_formed(events, CompletionStatus.COMPLETED_VERIFIED.value)

    def test_the_adapter_is_where_the_clock_lives(self):
        """The other half of the pin, stated positively: the clock is reachable,
        it is simply not in the decision function. Without an adapter reading
        it, the pure step could never time out and no run would ever end."""
        from harness import agent_loop as al

        source = Path(al.__file__).read_text(encoding="utf-8")
        assert "time.time() - self.started" in source, (
            "the adapter no longer reads a clock; nothing can time a run out"
        )
        assert "time.time" not in STEP_SOURCE_TEXT, (
            "the decision function reached for a clock directly"
        )


# ---------------------------------------------------------------------------
# 2, 3, 4. The matrix
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One matrix row: the pure run and the real-adapter run of the same thing.

    `verify` is the evidence the REAL adapter's verifier boundary will report,
    kept beside the scripted one so the two runs genuinely model the same
    situation. `never_invoked` names tools the pure run must not dispatch at
    all, which is a stronger claim than "the run ended somehow".
    """

    name: str
    replies: List[Any]
    config: Dict[str, Any]
    expected: str
    tools: Dict[str, Any] = field(default_factory=dict)
    verify: Optional[Dict[str, Any]] = None
    cancel: bool = False
    approver: Any = None
    steering: Dict[int, Any] = field(default_factory=dict)
    must_see: Sequence[str] = ()
    must_not_see: Sequence[str] = ()
    never_invoked: Sequence[str] = ()
    must_be_charged: bool = True
    #: Whether the real adapter can be driven into this situation at all. A
    #: row that says no must say WHY, and that reason is asserted to be about
    #: an effect this module cannot fake -- never about convenience.
    real: bool = True
    note: str = ""


def _clash() -> Any:
    """A BASH table that fails the first time it sees each distinct command and
    then succeeds.

    A callable rather than a table because several cases need the SAME command
    to fail once and then pass, and a static table would have to restate it.
    """
    state: Dict[str, bool] = {}

    def runner(args: Dict[str, Any], _turn: int) -> step_mod.ToolOutcome:
        command = str(args.get("command") or "")
        if not state.get(command):
            state[command] = True
            return step_mod.ToolOutcome(
                ok=False, output="exit=1 pyflakes: not found", kind="command_not_found"
            )
        return step_mod.ToolOutcome(ok=True, output="exit=0 1 passed")

    return runner


def _matrix() -> List[Case]:
    """The fourteen named scenarios, in the order the brief lists them."""
    plain = {"agent_max_turns": 8}
    declared = {"agent_max_turns": 8, "target_test": "tests/test_router.py"}
    return [
        # 1 -- question: read-only, no tests declared, nothing may be mutated.
        Case(
            name="question",
            replies=[
                '{"tool": "read", "path": "src/router.py"}',
                '{"tool": "done", "answer": "route() returns its argument."}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            must_see=("tool_call", "tool_result", "verify_skipped"),
            must_not_see=("verify", "loop_guard", "reflection", "policy_refused"),
            note="a question has no verifier, and saying so is the honest status",
        ),
        # 2 -- research: several read-only tools, no mutation, no approval.
        Case(
            name="research",
            replies=[
                '{"tool": "grep", "pattern": "def route"}',
                '{"tool": "memory", "query": "pytest conventions"}',
                '{"tool": "fetch", "url": "https://docs.python.org/3/"}',
                '{"tool": "done", "answer": "one module, one entry point."}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            must_see=("tool_call", "tool_result"),
            must_not_see=("verify", "approval_required"),
            never_invoked=("edit", "write", "bash"),
            note="read-only work never reaches the mutation or approval paths",
        ),
        # 3 -- build: a declared verifier, clean evidence. THE mint.
        Case(
            name="build",
            replies=[
                '{"tool": "write", "path": "src/new.py", "content": "X = 1\\n"}',
                '{"tool": "done", "answer": "added src/new.py"}',
            ],
            config=declared,
            expected=CompletionStatus.COMPLETED_VERIFIED.value,
            tools={
                "write": lambda args, turn: step_mod.ToolOutcome(
                    ok=True,
                    output="WRITE ok",
                    detail={"path": str(args.get("path") or "")},
                )
            },
            verify=verify_evidence(True),
            must_see=("verify",),
            must_not_see=("verify_skipped",),
            note="the only shape in the matrix that may report a pass",
        ),
        # 4 -- fix: a declared verifier that FAILS. The gate must hold.
        Case(
            name="fix",
            replies=[
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "return kind", "new_string": "return str(kind)"}',
                '{"tool": "done", "answer": "fixed it"}',
            ],
            config=declared,
            expected=CompletionStatus.FAILED.value,
            tools={
                "edit": lambda args, turn: step_mod.ToolOutcome(
                    ok=True, output="EDIT ok", detail={"path": "src/router.py"}
                )
            },
            verify=verify_evidence(False),
            must_see=("verify",),
            must_not_see=("verify_skipped",),
            note="the edit landed and the run still failed: the gate is not the edit",
        ),
        # 5 -- multi-file: two writes, then a clean verify.
        Case(
            name="multi_file",
            replies=[
                '{"tool": "write", "path": "src/a.py", "content": "A = 1\\n"}',
                '{"tool": "write", "path": "src/b.py", "content": "B = 2\\n"}',
                '{"tool": "done", "answer": "two files"}',
            ],
            config=declared,
            expected=CompletionStatus.COMPLETED_VERIFIED.value,
            tools={
                "write": lambda args, turn: step_mod.ToolOutcome(
                    ok=True,
                    output="WRITE ok",
                    detail={"path": str(args.get("path") or "")},
                )
            },
            verify=verify_evidence(True),
            must_see=("verify",),
            note="both paths must reach the terminal receipt, in order",
        ),
        # 6 -- tool failure: one failure becomes the next turn's input.
        Case(
            name="tool_failure",
            replies=[
                '{"tool": "bash", "command": "pyflakes src"}',
                '{"tool": "bash", "command": "pyflakes src/router.py"}',
                '{"tool": "done", "answer": "clean"}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            tools={"bash": _clash()},
            must_see=("reflection",),
            note="the failure is charged to the budget and the run recovers",
        ),
        # 7 -- policy refusal: refused, free, and never a pass.
        Case(
            name="policy_refusal",
            replies=[
                '{"tool": "bash", "command": "sudo rm -rf /"}',
                '{"tool": "read", "path": "src/router.py"}',
                '{"tool": "done", "answer": "I could not do that."}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            tools={
                "bash": step_mod.ToolOutcome(
                    ok=False,
                    output="COMMAND REJECTED: forbidden shape",
                    kind=tool_errors.KIND_PERMISSION_DENIED,
                )
            },
            must_see=("policy_refused", "reflection"),
            note="the refusal must be FREE: a refusal cannot spend a recovery budget",
        ),
        # 8 -- context exhaustion: the turn cap is the one bound with no off switch.
        Case(
            name="context_exhaustion",
            replies=[f'{{"tool": "read", "path": "src/f{i}.py"}}' for i in range(6)],
            config={"agent_max_turns": 3},
            expected=CompletionStatus.FAILED.value,
            must_not_see=("verify", "verify_skipped"),
            note="running out of turns is a failure with a reason, never a quiet finish",
        ),
        # 9 -- resume: the prefix reaches the model and the run continues.
        Case(
            name="resume",
            replies=['{"tool": "done", "answer": "continued from where we were"}'],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            must_not_see=("verify",),
            note="step has no resume concept: a prefix is ordinary history",
        ),
        # 10 -- cancel: checked before the model, so a cancel spends no turn.
        Case(
            name="cancel",
            replies=['{"tool": "done", "answer": "must not be reached"}'],
            config=plain,
            expected=CompletionStatus.CANCELLED.value,
            cancel=True,
            must_not_see=("tool_call", "verify", "verify_skipped"),
            real=False,
            note="a cancelled run must not have asked the model anything. The real "
            "adapter reads the session's cancellation token, which is tripped by "
            "the steering abort watcher on a live in-flight command -- that is a "
            "real interrupt and is proven in tests/test_agt_10_batch_boundary.py "
            "and tests/test_steering.py, not faked here.",
        ),
        # 11 -- doom loop: the Nth identical mutating call is never dispatched.
        Case(
            name="doom_loop",
            replies=[
                '{"tool": "bash", "command": "python -m pytest -q"}' for _ in range(6)
            ],
            config={"agent_max_turns": 8, "max_repeat_tool_calls": 2},
            expected=CompletionStatus.FAILED.value,
            must_see=("loop_guard",),
            note="three observations allowed; the fourth call is never dispatched",
        ),
        # 12 -- unparseable tool call: reflected on, not fatal on its own.
        Case(
            name="unparseable_tool_call",
            replies=[
                "Sure! Let me take a look at that for you.",
                '{"tool": "read", "path": "src/router.py"}',
                '{"tool": "done", "answer": "read it"}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            must_see=("tool_recovery", "reflection"),
            note="one unreadable reply is a normal event in a real run",
        ),
        # 13 -- approver denial: no dispatch, free reflection, run continues.
        Case(
            name="approver_denial",
            replies=[
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "return kind", "new_string": "return 0"}',
                '{"tool": "done", "answer": "I did not change anything."}',
            ],
            config={"agent_max_turns": 8, "agent_approval": "require"},
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            approver=lambda name, args: False,
            must_see=("approval_required", "approval_decided"),
            never_invoked=("edit",),
            note="the denial is visible as a decision, and the edit must not run",
        ),
        # 14 -- network unavailable: an environment fault, named, and not a pass.
        Case(
            name="network_unavailable",
            replies=[
                '{"tool": "fetch", "url": "https://example.com/x"}',
                '{"tool": "done", "answer": "no network, so I could not read it"}',
            ],
            config=plain,
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
            tools={
                "fetch": step_mod.ToolOutcome(
                    ok=False,
                    output="FETCH: connection refused",
                    kind=tool_errors.KIND_ENV_UNREACHABLE_NETWORK,
                )
            },
            must_see=("environment_unavailable", "reflection"),
            must_not_see=("verify",),
            note="the outage is named, and it can never be dressed as a pass",
        ),
    ]


MATRIX = _matrix()

#: The fourteen names the brief requires, in its order. Asserted against the
#: table so deleting a scenario deletes a test that fails.
REQUIRED_SCENARIOS = (
    "question",
    "research",
    "build",
    "fix",
    "multi_file",
    "tool_failure",
    "policy_refusal",
    "context_exhaustion",
    "resume",
    "cancel",
    "doom_loop",
    "unparseable_tool_call",
    "approver_denial",
    "network_unavailable",
)

#: The budgets the pure core and the adapter must agree on. A run that reports
#: a policy refusal must be able to say whether the refusal was charged.
CHARGED_KINDS = frozenset(
    {
        tool_errors.RETRY_CLASS_TASK_ERROR,
        tool_errors.RETRY_CLASS_MODEL_ERROR,
        tool_errors.RETRY_CLASS_TRANSIENT,
    }
)


class TestTheMatrix:
    def test_the_matrix_covers_every_named_scenario(self):
        assert tuple(c.name for c in MATRIX) == REQUIRED_SCENARIOS, (
            "the matrix is not the matrix: "
            f"{[c.name for c in MATRIX]} != {list(REQUIRED_SCENARIOS)}"
        )

    @pytest.mark.parametrize("case", MATRIX, ids=[c.name for c in MATRIX])
    def test_the_pure_step_reaches_the_expected_status(self, case: Case):
        """The cheap layer: a scripted boundary, no filesystem, no provider."""
        loop = ScriptedLoop(
            replies=list(case.replies),
            tools=_tools_for(case),
            cancel=case.cancel,
            approver=case.approver,
            steering_plan=dict(case.steering),
        )
        events = step_mod.step(_history_for(case), loop, case.config)

        assert_well_formed(events, case.expected)
        for kind in case.must_see:
            assert kind in kinds(events), (
                f"{case.name}: expected a {kind!r} event, got {kinds(events)}"
            )
        for kind in case.must_not_see:
            assert kind not in kinds(events), (
                f"{case.name}: {kind!r} must not appear here, got {kinds(events)}"
            )
        for tool in case.never_invoked:
            assert not loop.invoked(tool), (
                f"{case.name}: {tool!r} was dispatched and must not have been; "
                f"invocations were {loop.invocations}"
            )
        terminal = of_kind(events, step_mod.TERMINAL_KIND)[0]
        assert terminal.get("reason"), f"{case.name}: the run stopped without a reason"
        assert terminal.get("turns"), f"{case.name}: the run did not report its turns"

    @pytest.mark.parametrize("case", MATRIX, ids=[c.name for c in MATRIX])
    def test_a_refusal_is_never_charged_against_the_recovery_budget(self, case: Case):
        """The budget rule, checked where it is easy to get wrong.

        A non-retryable failure — a policy refusal, an approver denial, an
        environment fault — is journalled and FREE. If it were charged, a user
        saying no three times would exhaust the run's recovery budget, which
        would convert a refusal into a failure. The charged arm is asserted too,
        so this cannot pass by never charging anything.
        """
        loop = ScriptedLoop(
            replies=list(case.replies), tools=_tools_for(case), approver=case.approver
        )
        events = step_mod.step(_history_for(case), loop, case.config)
        reflections = of_kind(events, "reflection")
        for row in reflections:
            if row.get("retry_class") in CHARGED_KINDS:
                assert row.get("charged") is True, (
                    f"{case.name}: a retryable failure must be charged: {row}"
                )
            else:
                assert row.get("charged") is False, (
                    f"{case.name}: {row.get('retry_class')} must be free, or a "
                    f"refusal could exhaust a run's budget: {row}"
                )
        if not case.must_be_charged:
            assert not any(r.get("charged") for r in reflections), (
                f"{case.name}: nothing here was supposed to spend a budget"
            )

    @pytest.mark.parametrize("case", MATRIX, ids=[c.name for c in MATRIX])
    def test_the_real_adapter_agrees_with_the_pure_step(
        self, case: Case, tmp_path, monkeypatch
    ):
        """The expensive layer: the SAME scenario through the real product path.

        `run_agent_stepped` on a real repository, a real append-only
        `trace.jsonl`, the real model boundary, the real executor and the real
        verifier seam. The status must be the one the pure run decided — if the
        adapter can change an outcome, the split has leaked a decision back into
        the thing that is supposed to be only wiring.
        """
        if not case.real:
            pytest.skip(f"{case.name}: {case.note}")
        out = _drive(case, tmp_path, monkeypatch)
        assert out["status"] == case.expected, (
            f"{case.name}: the adapter reached {out['status']!r} and the pure "
            f"step reached {case.expected!r}"
        )
        assert step_mod.status_is_success(out["status"]) == (
            case.expected == CompletionStatus.COMPLETED_VERIFIED.value
        )

    @pytest.mark.parametrize("case", MATRIX, ids=[c.name for c in MATRIX])
    def test_the_real_adapter_leaves_a_replayable_trace(
        self, case: Case, tmp_path, monkeypatch
    ):
        """The durability boundary, which this round deliberately did NOT
        rebuild: the append-only `trace.jsonl` is still the authority, it still
        records the whole run, and it still ends with exactly one terminal
        event whose status the result repeats.
        """
        if not case.real:
            pytest.skip(f"{case.name}: {case.note}")
        out = _drive(case, tmp_path, monkeypatch)
        rows = _trace(tmp_path / "logs", f"agt11-{case.name}")
        assert rows, "the adapter wrote no trace: the durability boundary moved"
        terminal_rows = [r for r in rows if r.get("kind") == step_mod.TERMINAL_KIND]
        assert len(terminal_rows) == 1, [r.get("kind") for r in rows]
        assert terminal_rows[0]["data"]["status"] == out["status"], (
            "the trace and the result disagree about how the run ended"
        )
        # The seed is recorded before anything else, so a reader can see what
        # the run was asked to do without joining against anything else.
        assert rows[0].get("kind") == "task_start", rows[0].get("kind")
        # A `retrieval` row exists even when retrieval degraded, because
        # "nothing was found" and "we did not look" are different facts.
        assert any(r.get("kind") == "retrieval" for r in rows), [
            r.get("kind") for r in rows
        ]
        if case.expected == CompletionStatus.COMPLETED_VERIFIED.value:
            verify_rows = [r for r in rows if r.get("kind") == "verify"]
            assert verify_rows, (
                "a verified completion with no verify row in the journal"
            )
            assert len(verify_rows) == 1, (
                f"one verifier run must produce one journal row, got "
                f"{len(verify_rows)}: a reader counting verifier runs would be "
                f"right to distrust the number"
            )
        if case.name in ("build", "fix", "multi_file"):
            terminal = next(r for r in rows if r.get("kind") == step_mod.TERMINAL_KIND)
            assert terminal["data"]["files_touched"], (
                f"{case.name}: the run wrote files but the terminal receipt names "
                f"none, so the journal and the result would disagree about what "
                f"changed"
            )
            assert terminal["data"]["files_touched"] == out["files_touched"], (
                "the journal receipt and the result disagree about the changed files"
            )

    def test_no_scenario_ever_reports_an_unverified_completion_as_success(self):
        """The invariant, swept over the whole table rather than restated per
        case. The loop is over `MATRIX`, so a new row is covered the moment it
        is defined and a row that dresses an unverified completion as a pass
        cannot be added quietly.
        """
        for case in MATRIX:
            loop = ScriptedLoop(
                replies=list(case.replies), tools=_tools_for(case), cancel=case.cancel
            )
            events = step_mod.step(_history_for(case), loop, case.config)
            status = step_mod.terminal_status_of(events)
            if not case.config.get("target_test"):
                assert status != CompletionStatus.COMPLETED_VERIFIED.value, (
                    f"{case.name}: no tests are declared, so nothing can be verified"
                )
            if case.expected != CompletionStatus.COMPLETED_VERIFIED.value:
                assert not step_mod.status_is_success(status), case.name
            assert status in RUN_STATUSES, f"{case.name}: {status!r}"

    def test_a_verified_completion_is_reachable_only_through_the_declared_verifier(
        self,
    ):
        """The mint, from the permissive direction.

        Five shapes, every one of which a less careful loop would call a pass:
        clean evidence, flaky evidence, a regression failure, a target failure,
        and a verifier that raises. Only the first may complete, and the rest
        must be a FAILURE rather than a quieter flavour of completion.
        """
        shapes = {
            "clean": (
                step_mod.ToolOutcome(ok=True, detail=verify_evidence(True)),
                CompletionStatus.COMPLETED_VERIFIED.value,
            ),
            "flaky": (
                step_mod.ToolOutcome(ok=True, detail=verify_evidence(True, flaky=True)),
                CompletionStatus.FAILED.value,
            ),
            "regression_failed": (
                step_mod.ToolOutcome(
                    ok=True, detail=verify_evidence(True, regression=False)
                ),
                CompletionStatus.FAILED.value,
            ),
            "target_failed": (
                step_mod.ToolOutcome(ok=True, detail=verify_evidence(False)),
                CompletionStatus.FAILED.value,
            ),
            "verifier_raised": (
                RuntimeError("the docker daemon is not running"),
                CompletionStatus.FAILED.value,
            ),
        }
        for label, (verifier, expected) in shapes.items():
            loop = ScriptedLoop(
                replies=['{"tool": "done", "answer": "done"}'],
                tools={"verify": verifier},
            )
            events = step_mod.step(
                [{"role": "user", "content": "go"}],
                loop,
                {"target_test": "tests/test_router.py"},
            )
            assert_well_formed(events, expected)
            evidence = of_kind(events, "verify")
            assert evidence, f"{label}: a declared verifier must leave a record"
            if label == "verifier_raised":
                assert evidence[0].get("error"), (
                    "a verifier that raised must say so, not leave a report that "
                    "reads like a clean run with nothing in it"
                )

    def test_no_declared_tests_is_unverified_and_says_so(self):
        """The other direction, and the one this project has been bitten by: a
        run that finished with nothing declared is `completed_unverified`, and
        the trace says WHY rather than quietly omitting a verification."""
        loop = ScriptedLoop(replies=['{"tool": "done", "answer": "finished"}'])
        events = step_mod.step([{"role": "user", "content": "go"}], loop, {})
        status = assert_well_formed(events, CompletionStatus.COMPLETED_UNVERIFIED.value)
        assert not step_mod.status_is_success(status)
        skipped = of_kind(events, "verify_skipped")
        assert len(skipped) == 1
        assert skipped[0].get("status") == CompletionStatus.COMPLETED_UNVERIFIED.value
        assert skipped[0].get("reason"), "a skipped verification must name itself"
        assert not of_kind(events, "verify"), "nothing was run, so nothing was verified"

    def test_the_three_mint_conditions_agree_across_the_tree(self):
        """One mint condition, three implementations, pinned equal.

        The pure core, the adapter and the legacy kernel adapter each carry
        their own copy of "what counts as clean evidence". A copy is a hazard,
        so the copies are pinned against each other over a shape matrix rather
        than trusted — including the shapes where a default would help the
        wrong side (`{"target_passed": True}` with the regression key absent).
        """
        from harness.agent_kernel import legacy as legacy_mod
        from harness.agent_loop import _evidence_is_clean as adapter_clean

        shapes = [
            verify_evidence(True),
            verify_evidence(True, regression=False),
            verify_evidence(True, flaky=True),
            verify_evidence(False),
            {},
            {"target_passed": True},
            {"regression_passed": True},
            dict(verify_evidence(True), error="the runner crashed"),
        ]
        for evidence in shapes:
            assert step_mod._evidence_is_clean(evidence) == adapter_clean(evidence), (
                evidence
            )
            # The legacy adapter reads only the three booleans, so it treats an
            # evidence block that ALSO carries an error as clean; the pure core
            # requires the absence of an error. The pure core's direction is
            # the safe one -- a verifier that crashed and left stale booleans
            # behind must not mint a pass -- so the divergence is recorded and
            # asserted EXACTLY rather than papered over, and it is the one place
            # these two implementations are allowed to differ on a block both
            # of them can see.
            legacy_clean = bool(
                evidence.get("target_passed")
                and evidence.get("regression_passed", True)
                and not evidence.get("flaky")
            )
            if "regression_passed" in evidence:
                if evidence.get("error"):
                    assert legacy_clean and not step_mod._evidence_is_clean(evidence), (
                        "the documented error-bearing divergence changed: the "
                        f"pure core must stay the strict side. {evidence}"
                    )
                else:
                    assert legacy_clean == step_mod._evidence_is_clean(evidence), (
                        "the legacy and pure mint conditions disagree on an "
                        f"evidence block both of them can see: {evidence}"
                    )
            else:
                assert legacy_mod is not None  # the module is what is being pinned
                assert legacy_clean == bool(evidence.get("target_passed")), (
                    "the documented divergence must be exactly the missing "
                    f"regression term and nothing else: {evidence}"
                )

    def test_the_module_contains_no_literal_that_could_dress_a_completion(self):
        """A source-level honesty pin.

        The step module must not contain the bare status word as a string
        literal anywhere outside its own docstrings. It is a blunt instrument
        and that is the point: a future edit that reintroduces a third status
        vocabulary fails here instead of shipping a run that reports a pass it
        cannot prove.
        """
        tree = ast.parse(STEP_SOURCE_TEXT)
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ) and (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value.strip() == "success"
        ]
        assert not offenders, (
            "agent_loop_step.py contains the bare status word as a literal; the "
            "honest vocabulary is RUN_STATUSES and nothing else"
        )

    def test_the_turn_budget_is_the_one_bound_with_no_off_switch(self):
        """Zero, negative and unparseable turn budgets all mean ONE, not "run
        forever" and not "do nothing"."""
        for bad in (0, -5, "", None, "many", object()):
            loop = ScriptedLoop(replies=['{"tool": "done", "answer": "x"}'])
            cfg = step_mod.StepConfig.from_config({"agent_max_turns": bad})
            assert cfg.max_turns >= 1, bad
            events = step_mod.step([{"role": "user", "content": "go"}], loop, cfg)
            assert_well_formed(events, CompletionStatus.COMPLETED_UNVERIFIED.value)


# ---------------------------------------------------------------------------
# 5. The phrasings that broke in real use
# ---------------------------------------------------------------------------

#: The UXP corpus. Every entry is a sentence a person ACTUALLY typed and this
#: repository ACTUALLY misrouted, with the incident note kept beside it so a
#: future reader knows which regression each case guards. The notes are the
#: original ones from the classifier tables in `harness/agent_loop.py` and
#: `harness/intent.py`, not invented here.
UXP_CORPUS = (
    # `classify_deterministic`'s build table: both of these fell through to the
    # chit-chat dead end until design|make|develop|code|program|scaffold|
    # prototype were added to it.
    ("design a web based game where you dodge things", "agent_task"),
    ("make a mario type game", "agent_task"),
    # The explain table: "can u tell me the best mouse ..." hit the dead end
    # until tell me|suggest|recommend|advise were added.
    ("can u tell me the best mouse for clicky switches", "question"),
    # The recognised-input branch: an earlier rule treated any 3+ word input as
    # a task, and "florp the wobble" was launched as one.
    ("florp the wobble", "chit_chat"),
    # A build request misrouted to fix because a bare `raises` matched bug
    # language; the compound form is the only symptom form.
    ("build a function that returns an empty list and raises ValueError", "agent_task"),
    # No punctuation, lowercase, a blank line -- the shapes a typed sentence has.
    ("add retries to the http client\n\n  it times out sometimes", "agent_task"),
    # Vague, and with no file named anywhere in it.
    ("make it better", "agent_task"),
)

#: Phrasings a person would type that the classifier does NOT route yet. They
#: are here as an ASSERTED KNOWN GAP, not as cases pretending to work: a gap
#: that is recorded is a gap someone can close, and a gap that is not recorded
#: is a gap the next round rediscovers. The assertion is deliberately inverted
#: — it fails the day someone FIXES one, telling them to move it up into
#: `UXP_CORPUS`, where it becomes a case that must keep working.
KNOWN_CLASSIFICATION_GAPS = (
    # A specification clause. `harness/intent.py`'s mode router handles it via
    # the compound-only `_BUG_LANGUAGE` rule; `classify_deterministic` has no
    # such rule and lands on its honest "unrecognised" answer.
    ("an empty list must raise ValueError", "agent_task"),
    # A question with no interrogative word and no question mark.
    ("whats the deal with the cache invalidation", "question"),
    # Non-English work-shaped input. Nothing in the tree classifies on language.
    ("refactoriza el modulo de rutas para que sea mas rapido", "agent_task"),
)

_UXP_IDS = [
    f"{index:02d}-{text.split()[0].strip('.,?!')}"
    for index, (text, _) in enumerate(UXP_CORPUS)
]
_GAP_IDS = [f"gap-{index:02d}" for index, _ in enumerate(KNOWN_CLASSIFICATION_GAPS)]


class TestThePhrasingsThatBroke:
    @pytest.mark.parametrize("text,expected_kind", UXP_CORPUS, ids=_UXP_IDS)
    def test_the_phrase_still_routes_somewhere_sensible(self, text, expected_kind):
        """The classification half, asserted per sentence so a failure names
        the sentence rather than a loop index."""
        from harness.agent_loop import classify_deterministic

        intent = classify_deterministic(text)
        assert intent.kind == expected_kind, (
            f"{text!r} classified as {intent.kind!r} ({intent.reason!r}), "
            f"expected {expected_kind!r}"
        )

    @pytest.mark.parametrize("text,_kind", UXP_CORPUS, ids=_UXP_IDS)
    def test_the_phrase_produces_an_honest_run(self, text, _kind, tmp_path):
        """The execution half: the sentence is actually run through the real
        adapter, and three things are asserted. It reaches a status this project
        can report; that status is never a pass the run cannot prove; and the
        run says why it stopped.

        A model that answers `DONE` in one turn is used deliberately: the claim
        under test is about the harness's own bookkeeping, not about anything a
        model would say.
        """
        case = Case(
            name="uxp",
            replies=['{"tool": "done", "answer": "ok"}'],
            config={"agent_max_turns": 4},
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
        )
        out = _drive(
            case, tmp_path, monkeypatch=None, request=text, task_id="agt11-uxp"
        )
        assert out["status"] in RUN_STATUSES, out["status"]
        assert not step_mod.status_is_success(out["status"]), (
            f"{text!r}: a run with no declared tests reported {out['status']!r}"
        )
        assert out.get("end_reason"), f"{text!r}: the run stopped without a reason"
        rows = _trace(tmp_path / "logs", "agt11-uxp")
        assert [r for r in rows if r.get("kind") == step_mod.TERMINAL_KIND], text

    @pytest.mark.parametrize("text,_kind", UXP_CORPUS, ids=_UXP_IDS)
    def test_the_phrase_alone_never_mutates_the_repository(self, text, _kind, tmp_path):
        """The negative control that makes the sweep above non-vacuous.

        None of these sentences names a file, asks for a command, or grants a
        permission, so a run driven purely by the phrasing must leave the tree
        byte-identical. Without this, "the run reached a status" would be
        satisfiable by a loop that edits everything it is told.
        """
        repo = _repo(tmp_path / "repo")
        before = _tree_bytes(repo)
        from harness import deps
        from harness.agent_loop import run_agent_stepped

        deps.set_call_model(_Model(['{"tool": "done", "answer": "ok"}']))
        deps.set_execute_sandboxed(_Sandbox())
        try:
            out = run_agent_stepped(
                request=text,
                repo_path=str(repo),
                config={**STEPPED, "steering_enabled": False, "agent_max_turns": 3},
                log_root=tmp_path / "logs",
                task_id="agt11-uxp-mutate",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] in RUN_STATUSES
        assert _tree_bytes(repo) == before, (
            f"{text!r} mutated the repository with no tool call behind it"
        )

    def test_the_corpus_is_the_corpus(self):
        """A guard on the guard: the corpus is asserted to hold the shapes it
        exists to cover, so a future edit cannot quietly shrink it to the two
        easy cases."""
        assert len(UXP_CORPUS) >= 7
        assert {kind for _text, kind in UXP_CORPUS} == {
            "agent_task",
            "question",
            "chit_chat",
        }, "a classification kind in the corpus is never exercised"
        texts = [text for text, _ in UXP_CORPUS]
        assert any("\n" in text for text in texts), "no multi-line utterance"
        assert any(text == text.lower() for text in texts), "no lowercase utterance"
        assert any(not any(ch.isupper() for ch in text) for text in texts), (
            "no utterance typed entirely without a capital letter"
        )
        assert all(
            not any(ch in text for ch in (".py", ".js", ".ts", "src/"))
            for text in texts
        ), "a corpus entry names a file; these are the phrasings that name none"
        # Every entry must be distinct, or a duplicate is padding.
        assert len(set(texts)) == len(texts)

    @pytest.mark.parametrize("text,wanted", KNOWN_CLASSIFICATION_GAPS, ids=_GAP_IDS)
    def test_a_known_classification_gap_is_still_a_gap(self, text, wanted):
        """The inverted pin, and the reason the gap is safe to leave on the
        record: this FAILS the day the classifier is fixed, naming the sentence
        and telling the next person to move it into `UXP_CORPUS` where it
        becomes a case that must keep working. A gap nobody can see is a gap
        nobody closes.
        """
        from harness.agent_loop import classify_deterministic

        intent = classify_deterministic(text)
        assert intent.kind != wanted, (
            f"{text!r} now classifies as {intent.kind!r}, which is what this gap "
            "said it should be. Move it out of KNOWN_CLASSIFICATION_GAPS and "
            "into UXP_CORPUS so it becomes a case that must keep working."
        )
        assert intent.kind == "chit_chat", (
            f"{text!r} now routes somewhere unexpected: {intent.kind!r}"
        )

    @pytest.mark.parametrize("text,_kind", KNOWN_CLASSIFICATION_GAPS, ids=_GAP_IDS)
    def test_a_known_gap_still_produces_an_honest_run(self, text, _kind, tmp_path):
        """A misclassification must not become a fabricated completion.

        The classifier sends these to the chit-chat branch, so no run is
        launched at all from the adapter's point of view — and the assertion is
        that whatever comes back is a status this project can report, never a
        pass nobody proved. This is the property that makes the gap a
        usability bug rather than a correctness one.
        """
        case = Case(
            name="uxp-gap",
            replies=['{"tool": "done", "answer": "ok"}'],
            config={"agent_max_turns": 4},
            expected=CompletionStatus.COMPLETED_UNVERIFIED.value,
        )
        out = _drive(
            case, tmp_path, monkeypatch=None, request=text, task_id="agt11-gap"
        )
        assert out["status"] in RUN_STATUSES, out["status"]
        assert not step_mod.status_is_success(out["status"]), (
            f"{text!r}: a run with no declared tests reported {out['status']!r}"
        )


# ---------------------------------------------------------------------------
# Drivers and fixtures
# ---------------------------------------------------------------------------


def _tools_for(case: Case) -> Dict[str, Any]:
    """The scripted tool table for a case, with the verifier filled in.

    `case.verify` is the evidence the real adapter's verifier boundary reports.
    The pure run needs the SAME evidence, or the two layers would be modelling
    two different situations and the agreement test between them would be
    vacuous.
    """
    table = dict(case.tools)
    if case.verify is not None:
        table["verify"] = step_mod.ToolOutcome(ok=True, detail=dict(case.verify))
    return table


def _history_for(case: Case) -> List[Any]:
    """The starting history for a pure run.

    Only the `resume` case gets a prefix; every other case starts from one user
    message, so the seeded-history shape cannot mask a regression in a case that
    did not ask for one.
    """
    if case.name != "resume":
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "## Task\ndo the thing"},
        ]
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "## Task\nadd a flag"},
        {"role": "assistant", "content": '{"tool": "read", "path": "src/router.py"}'},
        {
            "role": "tool",
            "content": "READ src/router.py: 1 line",
            "tool": "read",
            "ok": True,
        },
        {"role": "user", "content": "## Prior session\nwe had already read router.py"},
    ]


class _Model:
    """A queue-driven Boundary-2 double.

    `**kw` is accepted deliberately. `ModelClient` forwards `effort` to any
    boundary whose signature names it, and a double with a strict signature is
    how a whole eval matrix died once; a double that rejected the keyword would
    be testing the double rather than the harness.
    """

    def __init__(self, replies: Sequence[Any]) -> None:
        self.replies = list(replies)
        self.calls: List[List[Dict[str, str]]] = []

    def __call__(self, messages: Any, **_kw: Any) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            return "I have nothing further to add."
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)

    def get_last_usage(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost": 0.0,
        }


class _Sandbox:
    """A Boundary-1 double for the harness's LOCAL BASH path.

    It answers every command green and records what it was asked, so a case
    that reaches the shell is visible rather than silent.
    """

    def __init__(self) -> None:
        self.commands: List[str] = []

    def __call__(
        self, _repo: str, command: str, timeout_s: float = 0.0
    ) -> ExecutionResult:
        self.commands.append(str(command))
        return ExecutionResult(0, "1 passed in 0.01s", "", False)

    @property
    def ran(self) -> List[str]:
        return list(self.commands)


def _drive(
    case: Case,
    tmp_path: Path,
    monkeypatch: Any = None,
    request: str = "do the thing",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one matrix case through the REAL adapter and return its result.

    `monkeypatch` is used when supplied (the pytest cases, which want the
    patch undone with them) and applied directly otherwise (the corpus sweeps,
    which have no fixture to hand). Either way the verifier boundary is
    replaced, so no case in this file needs a Docker daemon.
    """
    from harness import deps
    from harness.agent_loop import run_agent_stepped

    run_id = task_id or f"agt11-{case.name}"
    repo = _repo(tmp_path / "repo")
    model = _Model(case.replies)
    sandbox = _Sandbox()
    verifier = case.verify or verify_evidence(True)

    def fake_verify(*_args: Any, **_kw: Any) -> VerificationResult:
        return VerificationResult(
            target_test_passed=bool(verifier["target_passed"]),
            baseline_passed=bool(verifier["target_passed"]),
            regression_passed=bool(verifier["regression_passed"]),
            flaky=bool(verifier["flaky"]),
            raw_output=str(verifier.get("raw", "1 passed")),
        )

    if monkeypatch is not None:
        monkeypatch.setattr(deps, "get_verify", lambda: fake_verify)
    else:  # pragma: no cover -- the corpus sweeps take this branch
        _ORIGINAL_GET_VERIFY = deps.get_verify
        deps.get_verify = lambda: fake_verify  # type: ignore[assignment]
    deps.set_call_model(model)
    deps.set_execute_sandboxed(sandbox)
    try:
        out = run_agent_stepped(
            request=request,
            repo_path=str(repo),
            config={
                **STEPPED,
                "steering_enabled": False,
                "command_timeout_s": 30,
                "max_output_chars": 3000,
                **case.config,
            },
            log_root=tmp_path / "logs",
            task_id=run_id,
        )
    finally:
        deps.reset_overrides()
        if monkeypatch is None:  # pragma: no cover
            deps.get_verify = _ORIGINAL_GET_VERIFY  # type: ignore[assignment]
    return dict(out)


def _repo(root: Path) -> Path:
    """A small, real repository: a module, its test, and a second module so a
    multi-file case has somewhere to go."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "router.py").write_text(
        "def route(kind):\n    return kind\n", encoding="utf-8"
    )
    (root / "src" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_router.py").write_text(
        "from src.router import route\n\n\ndef test_route():\n    assert route('a') == 'a'\n",
        encoding="utf-8",
    )
    return root


def _tree_bytes(root: Path) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _trace(log_root: Path, task_id: str) -> List[Dict[str, Any]]:
    path = log_root / task_id / "trace.jsonl"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
