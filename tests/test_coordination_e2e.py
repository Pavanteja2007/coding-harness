"""End-to-end tests of COORDINATED multi-file changes (Improvement
Round 2, Tasks A+B+C) through the REAL loop controller, REAL Docker
sandbox + verifier, on a fixture whose fix GENUINELY requires 4
coordinated file changes (a method rename + signature change rippling
from the model to every consumer).

The fixture (tests/fixtures/bug06_coord): Invoice.invoice_total()
ignores its include_tax flag (always returns the pre-tax subtotal).
The correct fix — dropping the misleading flag and renaming the
method to amount_due() — REQUIRES coordinated edits in invlib/model.py
(the definition), invlib/serializers.py (two call sites),
invlib/reports.py (one call site), and invlib/api.py (one call site):
four files that must change together or the suite breaks (and a
partial rename is the classic half-updated state this round exists to
prevent).

These tests exercise:
- planning-side fan-out detection (real memory.code_graph over the
  real repo — the coordination trace event + change_group prompt),
- the ATOMIC group gate (a partial coordinated change is rejected
  before the verifier sees it as if it were complete),
- GROUP rollback (the rejected/broken group's files revert together;
  non-group work survives),
- end-to-end success on the genuine 4-file scenario.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from harness.core import run_task
from harness.deps import reset_overrides, set_call_model
from shared.types import Task
from tests.fake_model import ScriptedModel

FIXTURES = Path(__file__).parent / "fixtures"
COORD_FIXTURE = FIXTURES / "bug06_coord"


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


@pytest.fixture(autouse=True)
def _clean_overrides():
    reset_overrides()
    yield
    reset_overrides()


def _coord_task(tmp_path, issue, config=None, task_id="coord-e2e") -> Task:
    cfg = {
        "test_command": "python -m pytest -q",
        "command_timeout_s": 60,
        "verify_timeout_s": 180,
        "max_step_turns": 10,
        "agent_tests": False,  # isolate the coordination gates
    }
    cfg.update(config or {})
    return Task(
        task_id=f"{task_id}-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(COORD_FIXTURE),
        issue_text=issue,
        config=cfg,
    )


def _read_state(logs, task_id):
    return json.loads((logs / task_id / "state.json").read_text(encoding="utf-8"))


def _kinds(logs, task_id):
    return [
        json.loads(l)["kind"]
        for l in (logs / task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    ]


def _events(logs, task_id, kind):
    return [
        json.loads(l)
        for l in (logs / task_id / "trace.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
        if json.loads(l)["kind"] == kind
    ]


ISSUE = (
    "Invoice.invoice_total() ignores its include_tax parameter and always "
    "returns the pre-tax subtotal; every consumer sees the wrong total. "
    "Fix the model and update every call site in the same change: drop "
    "the include_tax parameter from the signature, rename the method "
    "to amount_due() (it now returns subtotal + tax), and update all "
    "callers — serializers.py, reports.py and api.py each call it and "
    "must be updated together with the model, or nothing should land."
)

# The genuine 4-file coordinated fix, as one scripted step (real bash):
# rename + de-flag the method, then update each call site file.
# NOTE: each heredoc script avoids triple-quote collisions by using a
# __-prefixed docstring marker rendered from escapes.

MODEL_OLD_DOC = '        __doc__ "Final amount the customer owes."'
_FULL_FIX = [
    # 1) model.py: rename + drop the flag (the definition)
    """python - <<'EOF'
p = "invlib/model.py"
s = open(p).read()
old = (
    "    def invoice_total(self, include_tax: bool = False) -> Decimal:\\n"
    "        \\\"\\\"\\\"Final amount the customer owes.\\\"\\\"\\\"\\n"
    "        return self.subtotal\\n"
)
new = (
    "    def amount_due(self) -> Decimal:\\n"
    "        \\\"\\\"\\\"Final amount the customer owes (subtotal + assessed tax).\\\"\\\"\\\"\\n"
    "        return self.subtotal + self.tax\\n"
)
assert old in s, "model.py shape changed"
open(p, "w").write(s.replace(old, new))
EOF""",
    # 2) serializers.py: both call sites
    """python - <<'EOF'
p = "invlib/serializers.py"
s = open(p).read()
assert "inv.invoice_total(include_tax=True)" in s
s = s.replace("inv.invoice_total(include_tax=True)", "inv.amount_due()")
open(p, "w").write(s)
EOF""",
    # 3) reports.py: its call site
    """python - <<'EOF'
p = "invlib/reports.py"
s = open(p).read()
assert "inv.invoice_total(include_tax=True)" in s
s = s.replace("inv.invoice_total(include_tax=True)", "inv.amount_due()")
open(p, "w").write(s)
EOF""",
    # 4) api.py: its call site (the flag-less call still breaks post-rename)
    """python - <<'EOF'
p = "invlib/api.py"
s = open(p).read()
assert "inv.invoice_total()" in s
s = s.replace("inv.invoice_total()", "inv.amount_due()")
open(p, "w").write(s)
EOF""",
    "SUBMIT",
]
FULL_FIX = _FULL_FIX

# Attempt-1 PARTIAL change: renames the model but never updates the three
# call-site files (the classic half-updated coordinated change).
PARTIAL_FIX = [
    FULL_FIX[0],  # model.py only
    "SUBMIT",
]

PLAN_ONE_STEP = [
    {
        "id": 1,
        "description": "fix model + all call sites as one coordinated change",
        "checkpoint": "target test passes",
        "files_hint": [
            "invlib/model.py",
            "invlib/serializers.py",
            "invlib/reports.py",
            "invlib/api.py",
        ],
        "change_group": "amount-due-rename",
    }
]


# ---------------------------------------------------------------------------
# Task C.1 — the genuine 4-file coordinated fix succeeds end-to-end
# ---------------------------------------------------------------------------


@requires_docker
def test_genuine_4file_coordinated_fix_succeeds(tmp_path):
    """The Definition-of-Done for this round: a real coordinated change
    across model + 3 consumers lands as ONE verified unit — detection
    fired at planning time (structural fan-out), the plan declared a
    change_group over all four files, the gate saw every member changed,
    and the verifier confirmed the fix."""
    model = ScriptedModel(plan=PLAN_ONE_STEP, scripts={1: [FULL_FIX]})
    set_call_model(model)
    task = _coord_task(
        tmp_path,
        ISSUE,
        config={"target_test": "tests/test_invoice.py::test_amount_due_includes_tax"},
    )
    logs = tmp_path / "logs"
    result = run_task(task, log_root=logs)
    assert result.status == "success", f"attempts={result.attempts}"
    assert result.attempts == 1
    assert result.diff is not None
    for rel in (
        "invlib/model.py",
        "invlib/serializers.py",
        "invlib/reports.py",
        "invlib/api.py",
    ):
        assert f"b/{rel}" in result.diff, f"{rel} missing from the fix diff"

    state = _read_state(logs, task.task_id)
    # the four-file group is declared in state.json (additive key)
    assert state["change_groups"] == {
        "amount-due-rename": [
            "invlib/api.py",
            "invlib/model.py",
            "invlib/reports.py",
            "invlib/serializers.py",
        ]
    }
    # detection ran structurally and flagged the coordinated shape
    coord_events = _events(logs, task.task_id, "coordination")
    assert coord_events, "planning-side coordination event missing"
    det = coord_events[0]["data"]
    assert det["detected"] is True
    # detection ran from retrieval's picks (the plan doesn't exist yet);
    # the graph fan-out found call/import dependents of the changed core
    assert det["dependent_files"], "structural fan-out found no dependents"
    assert det["kind"] in ("signature", "rename")
    # the plan's DECLARED group (the enforcement surface) carries all
    # four files — informed by the detection section in the prompt
    state_groups = state["change_groups"]
    assert state_groups["amount-due-rename"] == [
        "invlib/api.py",
        "invlib/model.py",
        "invlib/reports.py",
        "invlib/serializers.py",
    ]
    # every group member is in files_touched
    for rel in state_groups["amount-due-rename"]:
        assert rel in state["files_touched"]
    # the gate did NOT reject (all members changed)
    assert "coordination_gate_rejected" not in _kinds(logs, task.task_id)


# ---------------------------------------------------------------------------
# Task C.2 — partial coordinated change: gate rejects + ATOMIC rollback
# ---------------------------------------------------------------------------


@requires_docker
def test_partial_coordinated_change_rejected_and_rolled_back_together(tmp_path):
    """The rollback test the round demands: attempt 1 lands a PARTIAL
    coordinated change (model renamed, three call sites missed). The
    coordination gate must poison the attempt BEFORE the verifier can
    mistake it for complete, name the missing members in feedback, and
    roll the GROUP back together — model.py returns to pristine even
    though it was the one file that DID change. Attempt 2 completes all
    four files and succeeds."""
    model = ScriptedModel(
        plan=PLAN_ONE_STEP,
        scripts={
            1: [PARTIAL_FIX, FULL_FIX],
        },
    )
    set_call_model(model)
    task = _coord_task(
        tmp_path,
        ISSUE,
        config={
            "target_test": "tests/test_invoice.py::test_amount_due_includes_tax",
            "max_retries": 3,
        },
    )
    logs = tmp_path / "logs"
    result = run_task(task, log_root=logs)
    assert result.status == "success", (
        f"attempt 2 must complete the group; got {result.status}"
    )
    assert result.attempts == 2, "partial group must poison attempt 1"

    kinds = _kinds(logs, task.task_id)
    # the gate fired on attempt 1...
    rejected = _events(logs, task.task_id, "coordination_gate_rejected")
    assert len(rejected) == 1
    assert rejected[0]["data"]["attempt"] == 1
    assert set(rejected[0]["data"]["groups"]["amount-due-rename"]) == {
        "invlib/serializers.py",
        "invlib/reports.py",
        "invlib/api.py",
    }
    # ...and the group rolled back ATOMICALLY (model.py too, though it
    # was the file attempt 1 actually edited)
    rollbacks = _events(logs, task.task_id, "coordination_rollback")
    assert len(rollbacks) == 1
    assert rollbacks[0]["data"]["attempt"] == 1
    assert set(rollbacks[0]["data"]["restored"]) == {
        "invlib/api.py",
        "invlib/model.py",
        "invlib/reports.py",
        "invlib/serializers.py",
    }
    # the final diff is ONLY attempt 2's complete change
    assert result.diff is not None
    assert result.diff.count("+++ b/invlib/") == 4
    # no final_verify ever ran on the partial state (poisoned pre-verifier)
    final_verifies = _events(logs, task.task_id, "final_verify")
    assert len(final_verifies) == 1  # only attempt 2's
    assert "coordination_gate_rejected" in kinds
    # state decision explains the rollback (memory ingestion surface)
    state = _read_state(logs, task.task_id)
    assert any("coordinated change rolled back" in d for d in state["decisions"])
    # the ORIGINAL fixture repo is untouched throughout (never-mutate
    # guarantee holds for coordinated flows too)
    model_src = (COORD_FIXTURE / "invlib" / "model.py").read_text(encoding="utf-8")
    assert "def invoice_total(self, include_tax: bool = False)" in model_src
    assert "amount_due" not in model_src
    assert "amount_due" in (COORD_FIXTURE / "tests" / "test_invoice.py").read_text(
        encoding="utf-8"
    )  # (the test was written for the FIXED api)


# ---------------------------------------------------------------------------
# Task C.3 — group rollback when the COMPLETE change still fails verify
# ---------------------------------------------------------------------------


@requires_docker
def test_complete_but_broken_group_rolls_back_together(tmp_path):
    """All four files change (group complete — gate passes) but the
    change is BROKEN: the rename lands with the wrong arithmetic, so
    the target test fails. The failed verified attempt must roll the
    group back TOGETHER (atomic on failure, not just on partial), and
    attempt 2's clean fix succeeds. Proves rollback isn't tied to the
    partial-detection path alone."""
    broken = list(FULL_FIX)
    # model.py edit with the WRONG arithmetic (renamed, de-flagged, but
    # returns tax twice) — all 4 files still change
    broken[0] = """python - <<'EOF'
p = "invlib/model.py"
s = open(p).read()
old = (
    "    def invoice_total(self, include_tax: bool = False) -> Decimal:\\n"
    "        \\\"\\\"\\\"Final amount the customer owes.\\\"\\\"\\\"\\n"
    "        return self.subtotal\\n"
)
new = (
    "    def amount_due(self) -> Decimal:\\n"
    "        \\\"\\\"\\\"Final amount the customer owes (subtotal + assessed tax).\\\"\\\"\\\"\\n"
    "        return self.subtotal + self.tax + self.tax\\n"
)
assert old in s, "model.py shape changed"
open(p, "w").write(s.replace(old, new))
EOF"""
    model = ScriptedModel(
        plan=PLAN_ONE_STEP,
        scripts={
            1: [broken, FULL_FIX],
        },
    )
    set_call_model(model)
    task = _coord_task(
        tmp_path,
        ISSUE,
        config={
            "target_test": "tests/test_invoice.py::test_amount_due_includes_tax",
            "max_retries": 3,
        },
    )
    logs = tmp_path / "logs"
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    assert result.attempts == 2

    # attempt 1 got all the way to final verify (gate passed — complete
    # group) and failed on the broken arithmetic...
    final_verifies = _events(logs, task.task_id, "final_verify")
    assert len(final_verifies) == 2
    assert final_verifies[0]["data"]["target_passed"] is False
    # ...then the whole group rolled back together
    rollbacks = _events(logs, task.task_id, "coordination_rollback")
    assert len(rollbacks) == 1
    assert rollbacks[0]["data"]["attempt"] == 1
    assert set(rollbacks[0]["data"]["restored"]) == {
        "invlib/api.py",
        "invlib/model.py",
        "invlib/reports.py",
        "invlib/serializers.py",
    }
    # ...and attempt 2's complete correct change is the final diff
    assert result.diff is not None
    assert result.diff.count("+++ b/invlib/") == 4
    assert "self.tax + self.tax" not in result.diff


# ---------------------------------------------------------------------------
# Task C.4 — detection off / plain tasks unaffected (regression guards)
# ---------------------------------------------------------------------------


@requires_docker
def test_single_file_flow_unchanged_with_detection_enabled(tmp_path):
    """A plain single-file fix (bug02 fixture) with coordination
    detection ON must behave exactly as before: no group declared, no
    gate, no rollback, first-attempt success."""
    plan = [
        {
            "id": 1,
            "description": "fix the bug in the target file",
            "checkpoint": "target test passes",
            "files_hint": [],
        }
    ]
    model = ScriptedModel(
        plan=plan,
        scripts={
            1: [
                [
                    """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
                    "SUBMIT",
                ]
            ],
        },
    )
    set_call_model(model)
    task = Task(
        task_id=f"coord-plain-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "command_timeout_s": 60,
            "verify_timeout_s": 180,
            "max_step_turns": 8,
            "agent_tests": False,
        },
    )
    logs = tmp_path / "logs"
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    assert result.attempts == 1
    kinds = _kinds(logs, task.task_id)
    # coordination detection RAN (event present, nothing detected)...
    assert "coordination" in kinds
    coord = _events(logs, task.task_id, "coordination")[0]["data"]
    assert coord["detected"] is False
    # ...but no gate/rollback/groups ever fired
    assert "coordination_gate_rejected" not in kinds
    assert "coordination_rollback" not in kinds
    assert "change_groups" not in _read_state(logs, task.task_id)


@requires_docker
def test_coordination_detection_can_be_disabled(tmp_path):
    """coordination_detect=False is a real switch: no coordination
    event at all (the ablation OFF arm)."""
    plan = [
        {
            "id": 1,
            "description": "fix the bug in the target file",
            "checkpoint": "target test passes",
            "files_hint": [],
        }
    ]
    model = ScriptedModel(
        plan=plan,
        scripts={
            1: [
                [
                    """sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py""",
                    "SUBMIT",
                ]
            ],
        },
    )
    set_call_model(model)
    task = Task(
        task_id=f"coord-off-{abs(hash(str(tmp_path))) % 10000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "command_timeout_s": 60,
            "verify_timeout_s": 180,
            "max_step_turns": 8,
            "agent_tests": False,
            "coordination_detect": False,
        },
    )
    logs = tmp_path / "logs"
    result = run_task(task, log_root=logs)
    assert result.status == "success"
    assert "coordination" not in _kinds(logs, task.task_id)
