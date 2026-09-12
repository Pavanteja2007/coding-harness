"""Shared data types — the contract types defined in INTERFACES.md.

Every module imports these from here. The dataclass definitions are the
verbatim contract from INTERFACES.md; do not change field names or shapes
without updating INTERFACES.md first (and its Change Log).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class ExecutionResult:
    """Result of running one shell command via the sandbox layer.

    Assumes: exit_code is the process exit status (0 = success), stdout and
    stderr are the captured outputs (possibly truncated by the caller), and
    timed_out is True iff the command exceeded its timeout.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass
class VerificationResult:
    """Result of the verifier (test-based) check of a repo state.

    Assumes:
    - target_test_passed: the target test passed in the most recent run.
    - baseline_passed: the target test passed BEFORE any edit was made
      (i.e. on the pristine repo state — see execution/verify semantics).
    - regression_passed: the full test suite still passes after the edits.
    - flaky: the target test produced different outcomes across reruns.
    - raw_output: combined captured output of the verification runs.
    - structured_feedback: OPTIONAL parsed-failure objects (INTERFACES.md
      Boundary 7; list of FeedbackObject-shaped dicts — test_id,
      failure_type, summary, expected, actual, file, line,
      traceback_summary). Producers (execution/verify) fill it on failing
      runs when parseable; consumers must treat absent/[] as "raw_output
      only" (graceful adoption — the stub and historical serializations
      never carry it). Field default keeps every existing constructor
      call signature-valid.
    """

    target_test_passed: bool
    baseline_passed: bool
    regression_passed: bool
    flaky: bool
    raw_output: str
    structured_feedback: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class TaskResult:
    """Final outcome of one task, as returned by harness.core.run_task.

    Assumes: status is one of "success" | "failed" | "error" | "timeout";
    "success" is only ever set when the verifier confirmed the target test
    passes (verifier-gated completion — never the model's own claim).
    model_calls is a list of {step, model, provider, tokens, cost} dicts.
    """

    task_id: str
    status: Literal["success", "failed", "error", "timeout"]
    attempts: int
    diff: Optional[str]
    verification: Optional[VerificationResult]
    cost_usd: float
    model_calls: list
    log_path: str


@dataclass
class Task:
    """One unit of work for the harness: fix the bug described in issue_text.

    Assumes: repo_path points at a readable directory (a git work tree is
    preferred but not required); config carries all tunables (retry limits,
    budget caps, target test, model choices) so runs are reproducible.
    """

    task_id: str
    repo_path: str
    issue_text: str
    config: Dict[str, Any] = field(default_factory=dict)
