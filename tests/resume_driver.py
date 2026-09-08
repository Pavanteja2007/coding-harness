"""Subprocess driver for the resume/crash tests (NOT a test file itself).

Runs the REAL harness loop (harness.core.run_task) in a child process so a
mid-run HARD KILL (os._exit inside the scripted model — no cleanup, no
exception, exactly a TerminateProcess-style crash) takes down only the
child, not pytest. This mirrors the runtime's worker-process model, which
is the real deployment shape for checkpoint/resume.

Usage:
  python tests/resume_driver.py <mode> <task.json> <log_root> <result.json>

  mode "kill"     — model completes step 1 (a PARTIAL fix that leaves the
                     target test failing), then hard-kills the process at
                     step 2's first model call: the canonical crash window
                     (step 1 recorded complete in state.json, step 2 never
                     ran, work/ holds the partial edit).
  mode "complete" — model finishes step 2 (turns the partial fix into the
                     real fix) so the resumed run must succeed WITHOUT
                     re-running step 1 or the planner.

The task.json carries the Task fields (task_id, repo_path, issue_text,
config) — config already contains resume=True and the test command.
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.core import run_task  # noqa: E402
from harness.deps import set_call_model  # noqa: E402
from shared.types import Task  # noqa: E402

# Two-step plan where step 1 deliberately does NOT fix the target test
# (it swaps the wrong denominator for a placeholder), so the loop has a
# genuine step 2 to crash inside / resume into.
TWO_STEP_PLAN = [
    {"id": 1, "description": "mark the wrong denominator in numlib",
     "checkpoint": "mathutil.py contains the marker",
     "files_hint": ["numlib/mathutil.py"]},
    {"id": 2, "description": "replace the marker with the correct len() call",
     "checkpoint": "target test passes",
     "files_hint": ["numlib/mathutil.py"]},
]

STEP1_CMD = """sed -i 's/len(values) - 1/__FIX_ME__(values)/' numlib/mathutil.py"""
STEP2_CMD = """sed -i 's/__FIX_ME__(values)/len(values)/' numlib/mathutil.py"""


class KillAtStep2Model:
    """Serves the plan + step 1, then os._exit(70) on step 2's first call."""

    def get_last_usage(self):
        return {"model": "kill-at-step2", "provider": "fake", "tokens": 20,
                "cost_usd": 0.0001}

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages
                       if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": TWO_STEP_PLAN})
        if "your step is #1 of" in system:
            # one command then SUBMIT (queue is per-run simple: 2 entries)
            if not getattr(self, "_s1", None):
                self._s1 = [STEP1_CMD, "SUBMIT"]
            return self._s1.pop(0)
        if "your step is #2 of" in system:
            os._exit(70)  # hard kill: the crash window this driver exists for
        return "SUBMIT"


class CompleteStep2Model:
    """Serves the plan (never requested on resume) + step 2's fix."""

    def get_last_usage(self):
        return {"model": "complete-step2", "provider": "fake", "tokens": 20,
                "cost_usd": 0.0001}

    def __call__(self, messages, **kwargs):
        system = next((m["content"] for m in messages
                       if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            # Should NOT happen on resume (plan is reused); if it does,
            # serve the same plan so the run stays deterministic.
            return json.dumps({"analysis": "scripted", "plan": TWO_STEP_PLAN})
        if "your step is #1 of" in system:
            # Should NOT happen on resume (step 1 is skipped); returning
            # SUBMIT would fail verify — make the misuse visible but finite.
            return "SUBMIT"
        if "your step is #2 of" in system:
            if not getattr(self, "_s2", None):
                self._s2 = [STEP2_CMD, "SUBMIT"]
            return self._s2.pop(0)
        return "SUBMIT"


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: resume_driver.py <kill|complete> <task.json> "
              "<log_root> <result.json>", file=sys.stderr)
        return 2
    mode, task_json, log_root, result_json = sys.argv[1:5]
    spec = json.loads(Path(task_json).read_text(encoding="utf-8"))
    task = Task(
        task_id=spec["task_id"],
        repo_path=spec["repo_path"],
        issue_text=spec["issue_text"],
        config=spec["config"],
    )
    set_call_model(KillAtStep2Model() if mode == "kill" else CompleteStep2Model())
    result = run_task(task, log_root=Path(log_root))

    import dataclasses
    Path(result_json).write_text(
        json.dumps(dataclasses.asdict(result), indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
