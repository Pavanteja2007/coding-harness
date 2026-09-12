"""Task B measurement: wall-clock time saved by BATCH on a multi-file
read-heavy step, through the REAL Docker sandbox (the production shape).

Scenario (a common diagnosis shape): the step needs to read 6 files and
run 2 searches — 8 independent read-only operations. Serial = 8 turns,
one command per turn (the pre-BATCH behavior). Batch = 1 turn with 8
entries. Same fixture repo, same operations, same sandbox; the only
variable is grouping. The task legitimately ends "failed" (the scripted
models only READ — nobody fixes the bug); what is measured is the
elapsed time of the identical read workload.

Honest scope: serial also pays a model-call per turn; with the scripted
model that cost is ~0, so this isolates the EXECUTION saving. A real
model adds per-turn latency that BATCH also saves (fewer round-trips),
so the real-world saving is strictly larger.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from harness.deps import reset_overrides, set_call_model  # noqa: E402
from shared.types import Task  # noqa: E402

FIXTURES = (REPO_ROOT / "tests" / "fixtures").resolve()
ONE_STEP_PLAN = [
    {
        "id": 1,
        "description": "read the relevant files",
        "checkpoint": "files understood",
    }
]

# 8 independent read-only ops a diagnosis step really issues
READS = [
    "cat numlib/mathutil.py",
    "cat tests/test_mathutil.py",
    "cat pyproject.toml",
    "ls numlib",
    "ls tests",
    "grep -n def numlib/mathutil.py",
    "grep -n assert tests/test_mathutil.py",
    "find . -name '*.py'",
]


class SerialModel:
    """Pre-BATCH behavior: one read per turn, 8 turns."""

    def __init__(self):
        self.queue = list(READS)

    def get_last_usage(self):
        return {"model": "serial", "provider": "fake", "tokens": 20, "cost_usd": 0.0001}

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        if self.queue:
            return self.queue.pop(0)
        return "SUBMIT"


class BatchModel:
    """BATCH behavior: all 8 reads in one turn."""

    def __init__(self):
        self.done = False

    def get_last_usage(self):
        return {"model": "batch", "provider": "fake", "tokens": 20, "cost_usd": 0.0001}

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": ONE_STEP_PLAN})
        if not self.done:
            self.done = True
            return "BATCH " + " ;;; ".join(READS)
        return "SUBMIT"


def run_one(label, model):
    from harness.core import run_task

    td = Path(tempfile.mkdtemp(prefix=f"measure-{label}-"))
    set_call_model(model)
    task = Task(
        task_id=f"measure-{label}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() wrong denominator",
        config={
            "test_command": "python -m pytest -q",
            "verify_timeout_s": 180,
            "command_timeout_s": 60,
            "max_step_turns": 12,
            "max_retries": 1,
        },
    )
    started = time.time()
    result = run_task(task, log_root=td / "logs")
    elapsed = time.time() - started
    reset_overrides()
    return elapsed, result


serial_t, serial_r = run_one("serial", SerialModel())
batch_t, batch_r = run_one("batch", BatchModel())

if __name__ == "__main__":
    print(
        json.dumps(
            {
                "serial_s": round(serial_t, 2),
                "batch_s": round(batch_t, 2),
                "saved_s": round(serial_t - batch_t, 2),
                "speedup": round(serial_t / batch_t, 2),
                "serial_status": serial_r.status,
                "batch_status": batch_r.status,
                "operations": len(READS),
            },
            indent=2,
        )
    )
