"""Shared data types — the contract types defined in INTERFACES.md.

Every module imports these from here. The dataclass definitions are the
verbatim contract from INTERFACES.md; do not change field names or shapes
without updating INTERFACES.md first (and its Change Log).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from shared.security import defang_untrusted_delimiters, review_issue_text, taint_wrap

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shared.security import UntrustedReview


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

    **The issue body is untrusted content, and this is where it is reviewed.**
    `issue_text` arrives from an issue tracker, so it is attacker-controllable
    by anyone who can file an issue, and it arrives at turn one — there is no
    earlier turn in which it could have been caught. The four contract fields
    are unchanged; the review lives in three ADDED members so nothing about
    the serialized shape moves.

    What happens eagerly, at construction, and why only this much:

    * `defanged_issue_text` is computed in `__post_init__`. Defanging is the
      load-bearing half of the boundary — it is what makes an issue body
      unable to close its own delimiter — and it costs 0.075 ms on a 4 kB
      issue, so every `Task` in the tree pays it whether or not anybody reads
      the property.
    * `issue_review` (the injection scan, the redaction, the policy) is a
      MEMOISED property, not eager. Measured on this host: the scan is 1.96 ms
      and the redaction 1.85 ms on a 4 kB issue, 24 ms and 20 ms on 40 kB, and
      `Task` is constructed in loops (`cli/fileview.py` per snapshot,
      `cli/interactive.py` per command). Charging every construction 44 ms to
      produce a value almost nobody reads would be a performance defect
      bought for a security property the eager half already delivers.

    A consumer that interpolates the issue into a prompt MUST read
    `safe_issue_text` and not `issue_text`. That is the whole wiring, and
    `harness/prompts.py` is not this terminal's file — see the pinned handoff
    in `INTERFACES.md`.
    """

    task_id: str
    repo_path: str
    issue_text: str
    config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Set through __dict__ rather than as dataclass fields on purpose:
        # `dataclasses.fields()`, `asdict()` and `__eq__` must not see them,
        # because Task's field set is a published contract.
        self.__dict__["_defanged_issue_text"] = defang_untrusted_delimiters(
            self.issue_text
        )
        self.__dict__["_issue_review"] = None

    @property
    def defanged_issue_text(self) -> str:
        """Return the issue body with every boundary-forging construct broken.

        This is the EAGER half of the boundary and it is not a review: nothing
        has been scanned for injection and no policy has been applied. It is
        the property that makes "the issue cannot close its own delimiter"
        true for every Task, at construction, for 0.075 ms.
        """
        return self.__dict__.get("_defanged_issue_text", "")

    @property
    def issue_review(self) -> "UntrustedReview":
        """Return the full untrusted-content verdict for `issue_text`.

        Memoised, so a consumer that asks twice pays for one scan. The policy
        is :data:`shared.security.ISSUE_TEXT_MODES` — `flag`, escalating to
        `quarantine` at `high` — unless `config["untrusted_source_modes"]`
        pins it otherwise.
        """
        cached = self.__dict__.get("_issue_review")
        if cached is None:
            cached = review_issue_text(self.issue_text, config=self.config)
            self.__dict__["_issue_review"] = cached
        return cached

    @property
    def safe_issue_text(self) -> str:
        """Return the issue body DELIMITED and defanged, for prompt use.

        This is the string a caller interpolates instead of `issue_text`. It
        carries the `[UNTRUSTED SOURCE: issue]` banner, the boundary lines and
        the `[[tainted:...]]` marker, and for a body that reached the
        escalation threshold it is the quarantine marker rather than the body.
        """
        return taint_wrap(self.issue_review)

    @property
    def issue_trust_receipt(self) -> Dict[str, Any]:
        """Return a payload-free receipt proving the issue was reviewed.

        Suitable for a trace row: it names the source, the mode, the severity,
        the finding categories and the digest, and never carries the body.
        """
        return self.issue_review.as_dict()
