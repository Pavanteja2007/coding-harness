"""Unit tests for harness.state_machine (Round 8 Tasks C/D): explicit
states, the valid-transition table, rejection of invalid edges, and the
compact state-transition audit trail (transitions.jsonl).
"""

import json
from pathlib import Path

import pytest

from harness.state_machine import (
    ALL_STATES,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    TaskStateMachine,
    current_phase,
    initial_state,
    is_valid_transition,
    read_transitions,
)


def test_states_are_exactly_the_documented_set():
    assert ALL_STATES == frozenset(
        {
            "planning",
            "editing",
            "testing",
            "repairing",
            "awaiting_approval",
            "done",
            "failed",
        }
    )
    assert TERMINAL_STATES == frozenset({"done", "failed"})


def test_valid_transition_table_matches_docstring():
    """The table is the single source of truth for valid edges — pin the
    documented set so any change is a deliberate, reviewed edit.

    steering round: editing gained -> repairing (an attempt GATE
    (edit-validation/lint/coordination) rejects before any verify ran
    in the attempt, or a steering re-plan dismantles it for a new
    plan) — previously that edge forced a dishonest testing->repairing
    shape or skipped the audit entirely."""
    assert VALID_TRANSITIONS == {
        "planning": frozenset({"planning", "editing", "testing", "done", "failed"}),
        "editing": frozenset({"editing", "testing", "repairing", "done", "failed"}),
        "testing": frozenset(
            {
                "editing",
                "repairing",
                "planning",
                "testing",
                "awaiting_approval",
                "done",
                "failed",
            }
        ),
        "repairing": frozenset({"planning", "editing", "testing", "failed"}),
        "awaiting_approval": frozenset({"done", "failed"}),
        "done": frozenset(),
        "failed": frozenset(),
    }


def test_is_valid_transition_basics():
    assert is_valid_transition("planning", "editing")
    assert is_valid_transition("testing", "repairing")
    assert is_valid_transition("testing", "testing")
    assert is_valid_transition("awaiting_approval", "done")
    # invalid edges: terminal drift, backwards flow, unknown states
    assert not is_valid_transition("done", "editing")
    assert not is_valid_transition("failed", "planning")
    assert not is_valid_transition("editing", "planning")
    assert not is_valid_transition("awaiting_approval", "editing")
    assert not is_valid_transition("nonsense", "planning")
    assert not is_valid_transition("planning", "nonsense")


def test_initial_state_by_resume_flag():
    assert initial_state(resuming=False) == "planning"
    assert initial_state(resuming=True) == "repairing"


# ---------------------------------------------------------------------------
# machine + audit trail
# ---------------------------------------------------------------------------


def test_begin_and_happy_path_records_audit_trail(tmp_path):
    machine = TaskStateMachine(tmp_path)
    assert machine.phase is None
    machine.begin("run_task start", resuming=False)
    assert machine.phase == "planning"

    machine.transition("editing", "step 1 session begins")
    machine.transition("testing", "step 1 checkpoint verify")
    machine.transition("done", "fix verified")

    assert machine.phase == "done"
    assert machine.is_terminal()

    trail = read_transitions(tmp_path)
    assert [t["to_state"] for t in trail] == ["planning", "editing", "testing", "done"]
    assert trail[0]["from_state"] is None
    assert all(t["valid"] is True for t in trail)
    # compact record shape: ts + from + to + reason + valid
    assert set(trail[1].keys()) == {"ts", "from_state", "to_state", "reason", "valid"}
    assert trail[3]["reason"] == "fix verified"
    # chronological
    assert [t["ts"] for t in trail] == sorted(t["ts"] for t in trail)


def test_invalid_transition_rejected_and_recorded(tmp_path):
    machine = TaskStateMachine(tmp_path)
    machine.begin("run_task start")
    machine.transition("editing", "step 1 begins")
    with pytest.raises(InvalidTransition) as exc_info:
        machine.transition("planning", "drift attempt")
    assert exc_info.value.from_state == "editing"
    assert exc_info.value.to_state == "planning"
    # phase UNCHANGED — no silent drift
    assert machine.phase == "editing"
    # the violation is in the audit trail with valid: False
    trail = read_transitions(tmp_path)
    violations = [t for t in trail if t.get("valid") is False]
    assert len(violations) == 1
    assert violations[0]["from_state"] == "editing"
    assert violations[0]["to_state"] == "planning"
    assert machine.violations and machine.violations[0]["reason"] == "drift attempt"


def test_invalid_transition_on_terminal_state(tmp_path):
    machine = TaskStateMachine(tmp_path)
    machine.begin("start")
    machine.transition("failed", "budget cap")
    with pytest.raises(InvalidTransition):
        machine.transition("editing", "zombie revival")
    assert machine.phase == "failed"


def test_unknown_target_state_rejected(tmp_path):
    machine = TaskStateMachine(tmp_path)
    machine.begin("start")
    with pytest.raises(InvalidTransition):
        machine.transition("teleporting", "unknown state")


def test_transition_before_begin_rejected(tmp_path):
    machine = TaskStateMachine(tmp_path)
    with pytest.raises(InvalidTransition):
        machine.transition("editing", "never began")


def test_double_begin_rejected(tmp_path):
    machine = TaskStateMachine(tmp_path)
    machine.begin("start")
    with pytest.raises(InvalidTransition):
        machine.begin("again")


def test_on_transition_hook_fires_on_valid_only(tmp_path):
    seen = []

    def hook(from_state, to_state, reason):
        seen.append((from_state, to_state, reason))

    machine = TaskStateMachine(tmp_path, on_transition=hook)
    machine.begin("start")
    machine.transition("editing", "step 1")
    with pytest.raises(InvalidTransition):
        machine.transition("planning", "drift")
    # hook saw exactly the valid transitions (begin included, via record)
    assert seen == [("planning", "editing", "step 1")]

    # a crashing hook must not kill the run
    def bad_hook(f, t, r):
        raise RuntimeError("hook exploded")

    machine2 = TaskStateMachine(tmp_path / "m2", on_transition=bad_hook)
    machine2.begin("start")
    assert machine2.transition("failed", "hook crash tolerated") == "failed"


def test_resume_entry_is_repairing_and_trail_survives_relaunch(tmp_path):
    """A relaunched (resumed) run enters through `repairing` and APPENDS
    to the same audit trail — pre-crash transitions survive as history,
    exactly like trace.jsonl."""
    m1 = TaskStateMachine(tmp_path)
    m1.begin("run 1 start")
    m1.transition("editing", "step 1 begins")
    # hard crash here; relaunch resumes:
    m2 = TaskStateMachine(tmp_path)
    m2.begin("run 2 start (resumed)", resuming=True)
    assert m2.phase == "repairing"
    m2.transition("editing", "attempt continues its steps")
    m2.transition("testing", "final gate")
    m2.transition("done", "fix verified")

    trail = read_transitions(tmp_path)
    assert [t["to_state"] for t in trail] == [
        "planning",
        "editing",
        "repairing",
        "editing",
        "testing",
        "done",
    ]
    # both run-starts present (the second notes the resume)
    assert trail[2]["from_state"] is None


def test_current_phase_reads_last_valid_to_state(tmp_path):
    machine = TaskStateMachine(tmp_path)
    assert current_phase(tmp_path) is None  # nothing ran
    machine.begin("start")
    machine.transition("editing", "step 1")
    assert current_phase(tmp_path) == "editing"
    with pytest.raises(InvalidTransition):
        machine.transition("planning", "drift")
    # a violation does NOT change the reported phase
    assert current_phase(tmp_path) == "editing"


def test_current_phase_ignores_violation_records(tmp_path):
    """The live-status surface must show the last VALID state even when
    the trail holds a rejected edge."""
    machine = TaskStateMachine(tmp_path)
    machine.begin("start")
    machine.transition("editing", "step 1 session")
    with pytest.raises(InvalidTransition):
        machine.transition("planning", "drift: backwards from editing")
    assert current_phase(tmp_path) == "editing"


def test_read_transitions_tolerates_corruption_and_absence(tmp_path):
    empty = tmp_path / "never-ran"
    assert read_transitions(empty) == []
    assert current_phase(empty) is None
    d = tmp_path / "corrupt"
    d.mkdir()
    (d / "transitions.jsonl").write_text(
        "not json\n\n" + json.dumps({"to_state": "editing", "valid": True}) + "\n",
        encoding="utf-8",
    )
    trail = read_transitions(d)
    assert len(trail) == 1  # malformed line skipped, valid one kept
    assert current_phase(d) == "editing"


def test_machine_thread_safety_smoke(tmp_path):
    """Two threads transitioning; every append is a full line (the audit
    file is line-oriented — no torn records)."""
    import threading

    machine = TaskStateMachine(tmp_path)
    machine.begin("start")
    errors = []

    def worker(name):
        try:
            for i in range(20):
                machine.transition("editing", f"{name}-{i}")
                machine.transition("testing", f"{name}-{i} verify")
        except Exception as exc:  # some races legitimately raise Invalid
            if not isinstance(exc, InvalidTransition):
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    lines = (
        (tmp_path / "transitions.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert all(json.loads(l) for l in lines)  # every line parses
