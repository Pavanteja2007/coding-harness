"""Formal agent state machine: explicit states, defined valid
transitions, invalid transitions rejected/flagged (Round 8, Tasks C+D).

Before this module, the task's "phase" was only observable after the
fact by parsing trace.jsonl. Now it is PROVABLE: run_task drives every
phase change through TaskStateMachine.transition(), which validates the
edge against the table below, appends a compact audit record to
logs/{task_id}/transitions.jsonl (timestamp + from-state + to-state +
reason), and mirrors the live phase into state.json's "phase" field
(the additive Boundary-4 key that powers `harness status` live view and
the dashboard's "what phase is this task in right now").

STATES (what each means for a running task):

  planning           the run is set up (snapshot/baseline done or
                     resumed) and the planner is producing sub-steps
  editing            a sub-step bash session is executing commands
                     (RECALL turns included — they serve the edit)
  testing            a verification pass is evaluating a repo state:
                     baseline (pristine), per-step (post-SUBMIT), or
                     the final verifier gate
  repairing          an attempt failed verification and the next one is
                     being prepared (work/ rolled back, feedback seeded);
                     also the resume entry state (repairing an
                     interrupted attempt)
  awaiting_approval  the task's verified fix is parked in Terminal 3's
                     human-approval gate (worker-level; the harness
                     records the park so live status can show it)
  done              terminal: verifier-gated success (incl. the
                     pre-passing-pristine short-circuit)
  failed             terminal: retries/budget/wall-clock exhausted
                     without a verified fix, or the approval gate
                     rejected/timeout on a verified fix

VALID TRANSITIONS (from -> {to}):

  planning     -> {planning, editing, testing, done, failed}
                   (planning->planning: a plan-parse retry)
  editing      -> {editing, testing, repairing, done, failed}
                   (editing->editing: next turn/command in the session;
                    editing->repairing: an attempt GATE (edit-validation/
                    lint/coordination) rejected the attempt before any
                    verify ran in it, or a steering re-plan dismantles
                    the attempt for a new plan)
  testing      -> {editing, repairing, planning, testing,
                   awaiting_approval, done, failed}
                   (testing->editing: checkpoint failed, step continues
                    or the next step runs; testing->planning: baseline
                    verify on pristine; testing->testing: CONSECUTIVE
                    verifier passes — a step's checkpoint verify followed
                    directly by the attempt's final gate, no editing
                    between; testing->awaiting_approval: the final gate
                    verified the fix AND approval mode is on (the park is
                    recorded write-ahead, before run_task returns);
                    testing->repairing: step verify poisoned the
                    attempt / final verify failed / self-critique
                    rejected the diff)
  repairing    -> {planning, editing, testing, failed}
                   (repairing->editing: the next attempt's steps run;
                    repairing->planning: an aborted resume falls back to
                    a fresh start (baseline -> planner), or a steering
                    re-plan replaces the remaining plan; repairing->
                    testing: attempt reached final verify)
  awaiting_approval -> {done, failed}
                   (approve -> done; reject/timeout -> failed)
  done         -> {}    terminal
  failed       -> {}    terminal

ANY other edge is invalid. `transition` never applies it: it records the
violation (audit trail + trace event) and raises InvalidTransition so
the caller can decide (run_task treats an invalid transition as a task
error — loud, never silent drift; the state.json "phase" field then
still shows the last VALID state, which is the honest live view).

STEERING (steering round, Task B): a steering interrupt is NOT a state
transition — the machine stays in its current state and the loop
consumes the instruction at the next SAFE CHECKPOINT (a turn boundary,
step boundary, or the final gate). Two steering intents do move the
machine, through EXISTING forward edges only:
  - "replan" -> editing -> repairing -> planning (the attempt is
    dismantled for a new plan; work-in-progress is KEPT)
  - "abort"  -> <current> -> failed (clean stop; checkpoints stay)
  - "guide"  -> no machine movement at all (the instruction rides the
    live session / next attempt's feedback).
`record_event` appends a non-transition audit record ({"event": ...},
valid: true, no from/to change) so the trail shows WHEN steering was
consumed without ever corrupting a valid transition — the steering
round's Task B contract.

ERROR outcomes (planner crash, snapshot failure, fatal step error):
run_task deliberately does NOT transition — the on-disk phase stays at
the crash-point state, which is the honest "where it died" view; only
verified success (done) and exhausted/rejected failure (failed) are
terminal transitions.

The harness may be killed hard at any moment (scheduler hang check,
os._exit); the audit trail + phase field are therefore written BEFORE
the work each phase describes happens (write-ahead), so a relaunched
run (resume) re-enters through the recorded phase and a crash never
leaves a misleading "done" on disk for an unfinished task.

Awaiting-approval honesty note: the gate itself lives in Terminal 3's
worker (runtime/approval.py), which parks AFTER run_task returns — the
harness cannot see it from inside run_task. The worker's checkpoint
already carries `awaiting_approval`; the phase is recorded here for
live-status consumers that read state.json. run_task records the park
when task.config["approval"] == "require" and the fix is verified (the
exact condition the worker gates on), immediately before returning —
so the on-disk phase is correct during the entire park window.
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# State names (single source of truth; state.json "phase" uses these).
STATE_PLANNING = "planning"
STATE_EDITING = "editing"
STATE_TESTING = "testing"
STATE_REPAIRING = "repairing"
STATE_AWAITING_APPROVAL = "awaiting_approval"
STATE_DONE = "done"
STATE_FAILED = "failed"

ALL_STATES = frozenset(
    {
        STATE_PLANNING,
        STATE_EDITING,
        STATE_TESTING,
        STATE_REPAIRING,
        STATE_AWAITING_APPROVAL,
        STATE_DONE,
        STATE_FAILED,
    }
)

TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED})

# The valid-transition table (from-state -> allowed to-states).
VALID_TRANSITIONS: Dict[str, frozenset] = {
    STATE_PLANNING: frozenset(
        {
            STATE_PLANNING,
            STATE_EDITING,
            STATE_TESTING,
            STATE_DONE,
            STATE_FAILED,
        }
    ),
    STATE_EDITING: frozenset(
        {
            STATE_EDITING,
            STATE_TESTING,
            STATE_REPAIRING,
            STATE_DONE,
            STATE_FAILED,
        }
    ),
    STATE_TESTING: frozenset(
        {
            STATE_EDITING,
            STATE_REPAIRING,
            STATE_PLANNING,
            STATE_TESTING,
            STATE_AWAITING_APPROVAL,
            STATE_DONE,
            STATE_FAILED,
        }
    ),
    STATE_REPAIRING: frozenset(
        {
            STATE_PLANNING,
            STATE_EDITING,
            STATE_TESTING,
            STATE_FAILED,
        }
    ),
    STATE_AWAITING_APPROVAL: frozenset({STATE_DONE, STATE_FAILED}),
    STATE_DONE: frozenset(),
    STATE_FAILED: frozenset(),
}


class InvalidTransition(Exception):
    """Raised when a transition is not in VALID_TRANSITIONS.

    `from_state`, `to_state`, and `reason` carry the rejected edge for
    logging; the machine's phase is NOT changed when this raises.
    """

    def __init__(self, from_state: str, to_state: str, reason: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        super().__init__(
            f"invalid state transition: {from_state!r} -> {to_state!r} "
            f"(reason: {reason!r})"
        )


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """True when from_state -> to_state is an allowed edge.

    Assumes both arguments are state names from ALL_STATES; unknown
    states are invalid by definition (the machine must never drift into
    an undefined phase).
    """
    return to_state in VALID_TRANSITIONS.get(from_state, frozenset())


def initial_state(resuming: bool) -> str:
    """The state a run starts in.

    A fresh run enters through planning (setup -> baseline -> planner).
    A RESUMED run continues an interrupted attempt: its pre-crash work
    survives in work/ and the fix is mid-flight — that is `repairing`
    (repairing an interrupted attempt), the documented resume entry.
    Assumes the caller only sets resuming when the resume contract's
    preconditions hold (completed steps + plan.json + surviving dirs).
    """
    return STATE_REPAIRING if resuming else STATE_PLANNING


class TaskStateMachine:
    """One task's phase tracker + audit-trail writer.

    Assumes it is created once per run_task invocation, owns
    logs/{task_id}/transitions.jsonl (append-only, survives relaunches
    like trace.jsonl), and that the caller keeps state.json's "phase"
    field in sync via the on_transition hook (run_task wires that to
    TaskState.set_phase so the live-status surface never lags the
    audit trail).
    """

    def __init__(
        self,
        log_dir: Path,
        on_transition: Optional[Any] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self._path = self.log_dir / "transitions.jsonl"
        self._lock = threading.Lock()
        self._on_transition = on_transition
        self.state: Optional[str] = None
        self.violations: List[Dict[str, Any]] = []

    # -- queries ---------------------------------------------------------

    @property
    def phase(self) -> Optional[str]:
        """Current phase, or None before the first begin()."""
        return self.state

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    # -- driving ---------------------------------------------------------

    def begin(self, reason: str, resuming: bool = False) -> str:
        """Enter the machine's initial state (does NOT validate — the
        first entry has no from-state; records the run-start transition).

        Assumes the machine has not begun yet; a second begin() is an
        invalid transition (None -> state only once).
        """
        with self._lock:
            if self.state is not None:
                raise InvalidTransition(
                    self.state,
                    initial_state(resuming),
                    "begin() called on an already-started machine",
                )
            self.state = initial_state(resuming)
            self._append(self._record(None, self.state, reason))
            return self.state

    def transition(self, to_state: str, reason: str) -> str:
        """Attempt from-state -> to_state.

        Valid: appends the audit record, invokes on_transition(from, to,
        reason) (run_task's hook mirrors `to` into state.json), returns
        the new state. Invalid (or unknown state name): records a
        violation entry, leaves the phase UNCHANGED, and raises
        InvalidTransition — silent drift is exactly what this module
        exists to prevent. Assumes `reason` is a short human-readable
        cause for the audit trail.
        """
        if to_state not in ALL_STATES:
            to_state = str(to_state)
        with self._lock:
            if self.state is None:
                raise InvalidTransition(
                    "<unstarted>", to_state, "transition() called before begin()"
                )
            if not is_valid_transition(self.state, to_state):
                violation = {
                    "ts": round(time.time(), 3),
                    "from_state": self.state,
                    "to_state": to_state,
                    "reason": reason,
                    "valid": False,
                }
                self.violations.append(violation)
                self._append(violation)
                raise InvalidTransition(self.state, to_state, reason)
            from_state = self.state
            self.state = to_state
            self._append(self._record(from_state, to_state, reason))
        if self._on_transition is not None:
            try:
                self._on_transition(from_state, to_state, reason)
            except Exception:
                pass  # the hook mirrors state.json; never kill the run
        return to_state

    # -- audit trail ------------------------------------------------------

    def record_event(self, event: str, detail: Any = None) -> None:
        """Append a NON-TRANSITION audit record (steering round, Task B).

        Steering consumption must be visible in the trail (WHEN the
        user redirected the task) without ever corrupting a valid
        transition — this records {"event": name, "detail": ...} with
        the phase UNCHANGED. Assumes event is a short slug
        ("steering", "steering_replan", "steering_abort"); detail is
        any JSON-able context. Never raises; a write failure is
        swallowed (same contract as _append).
        """
        record: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "event": str(event),
            "phase": self.state,
            "valid": True,
            "detail": detail if detail is not None else {},
        }
        with self._lock:
            self._append(record)

    def history(self) -> List[Dict[str, Any]]:
        """All audit records on disk (this run + prior resumed runs),
        oldest first. Malformed lines are skipped; read errors -> []."""
        records: List[Dict[str, Any]] = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
        except OSError:
            return []
        return records

    def _record(
        self,
        from_state: Optional[str],
        to_state: str,
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "ts": round(time.time(), 3),
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "valid": True,
        }

    def _append(self, record: Dict[str, Any]) -> None:
        """Append one audit record; a write failure must never kill the
        run (the in-memory state stays authoritative for the caller)."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


# -- reading helpers for CLI / dashboard (live status) ------------------------


def read_transitions(log_dir: Path) -> List[Dict[str, Any]]:
    """Read a task's full state-transition history from disk.

    Assumes log_dir is logs/{task_id}/ (or an archived copy). Returns
    the parsed records, oldest first; missing/corrupt file -> [] (a
    pre-Round-8 run has no trail — readers must treat that as "unknown",
    not "planning").
    """
    return TaskStateMachine(Path(log_dir)).history()


def current_phase(log_dir: Path) -> Optional[str]:
    """The last VALID transition's to-state from a task's audit trail.

    The phase a live status surface should show; None when there is no
    readable trail (nothing ran, or a pre-Round-8 run).
    """
    for record in reversed(read_transitions(log_dir)):
        if record.get("valid") is not False:
            return str(record.get("to_state") or None)
    return None
