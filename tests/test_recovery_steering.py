"""VEX-CEILING-07 — recovery policy, model-failure tolerance, steering abort,
and doom-loop prevention.

The gap this suite pins (from NEO_CEILING_MASTER_PROMPTS.md G13-G16):

  G13  error classification changed presentation, not behavior
  G14  steering was only checked at turn boundaries, so an in-flight
       long-running command could not be interrupted
  G15  one transient model error became FATAL for the whole run
  G16  tool output truncation could throw away the answer

Each of the prompt's six REQUIRED tests maps to a test named after it, and
each asserts a BEHAVIORAL outcome rather than a rendered string:

  1. `sleep 300` is aborted in <5 seconds              (real process kill)
  2. two provider 502s recover without losing the run   (bounded backoff)
  3. a timeout produces a narrower next command         (real narrowing)
  4. a 20k-character failing test output includes the assertion
  5. repeated identical calls trigger loop protection   (turn is stopped)
  6. steering arrives at the next boundary without corrupting the journal

Lanes are honest about what ran:
  * the `sleep 300` abort is measured against a REAL killed process — the
    Docker container lane when the daemon is up, and the local process-tree
    lane always (both are real kills, and both assert a wall-clock bound);
  * a Docker-gated selection self-skips with the daemon's reason, so a
    skipped Docker test is never reported as a pass;
  * no live model/provider lane is used anywhere. Provider failures are
    constructed exception objects, and every model call in the loop tests is
    the in-repo scripted fake.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import harness.core as core_mod
import harness.steering as steering_mod
import harness.tool_errors as te
from harness.config import get_config
from harness.deps import reset_overrides, set_call_model, set_execute_sandboxed
from harness.tools import BashSession
from shared.types import Task

REPO_ROOT = Path(__file__).resolve().parents[1]

# The prompt's hard bound for a hard abort of a running child process.
ABORT_BOUND_S = 5.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Sink:
    """A trace-shaped event sink that records every event for assertions."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def log(self, kind: str, data: Dict[str, Any]) -> None:
        self.events.append({"kind": kind, "data": dict(data)})

    def kinds(self) -> List[str]:
        return [event["kind"] for event in self.events]

    def first(self, kind: str) -> Dict[str, Any]:
        for event in self.events:
            if event["kind"] == kind:
                return event["data"]
        raise AssertionError(f"no {kind!r} event in {self.kinds()}")


def _events_of_kind(trace: Any, kind: str) -> List[Dict[str, Any]]:
    """Every trace record of one kind, oldest first.

    `TraceLogger.find_events` is a TEXT search, not a kind filter, so a
    kind-precise assertion must not go through it — a substring match would
    quietly pass on the wrong event.
    """
    out: List[Dict[str, Any]] = []
    for record in trace.read_all():
        if record.get("kind") == kind:
            out.append(dict(record.get("data") or {}))
    return out


def _docker_available() -> bool:
    try:
        from execution.sandbox import docker_available

        return bool(docker_available())
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_available(),
    reason="docker daemon not reachable; the real container kill lane is BLOCKED",
)


class _ProviderError(Exception):
    """A stand-in provider error carrying an explicit HTTP status.

    Models the shape litellm raises (`status_code` on the exception), which
    is what `classify_model_failure` reads first.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _FlakyModel:
    """A scripted model that fails N times, then returns the script.

    `failures` is a list of exceptions to raise (in order) before the first
    scripted reply is returned. It records the real user-visible messages so a
    test can prove the run kept its conversation across the recovery.
    """

    def __init__(self, failures: List[BaseException], replies: List[str]) -> None:
        self.failures = list(failures)
        self.replies = list(replies)
        self.calls = 0
        self.seen: List[List[Dict[str, str]]] = []

    def __call__(self, messages, *_args, **_kwargs):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.failures:
            raise self.failures.pop(0)
        return self.replies.pop(0) if self.replies else "SUBMIT"

    def get_last_usage(self) -> Dict[str, object]:
        return {
            "model": "scripted-flaky",
            "provider": "fake",
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "tokens": 20,
            "cost_usd": 0.0001,
        }


def _make_workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "readme.txt").write_text("hello\n", encoding="utf-8")
    return root


def _ok_verify(*_args, **_kwargs):
    """A verifier that reports clean evidence.

    The step loop requires a COMPLETE VerificationResult, not None: the
    post-SUBMIT path is where verifier-gated completion is decided, so a
    placeholder that returns nothing is refused (correctly) as incomplete
    evidence. Recovery must not be able to relax that.
    """
    from shared.types import VerificationResult

    return VerificationResult(True, True, True, False, "1 passed")


def _paths(log_root: Path, task_id: str, repo: Path):
    """A real `TaskPaths` whose pristine AND work point at the test repo.

    The layout object is the production one (so the log dir it computes is
    the one the test asserts against); only the tree pointers are aimed at the
    temp repo, which is what the fix loop would otherwise have copied there.
    """
    paths = core_mod.TaskPaths(log_root, task_id)
    paths.pristine = repo
    paths.work = repo
    return paths


def _failing_pytest_output(chars: int = 20000) -> str:
    """A pytest-shaped failing run whose assertion is recoverable only by a
    TAIL-PREFERRING truncation.

    Two properties make this input discriminate rather than merely pass:

      * the run is huge (a long collection log), so head+tail truncation is
        guaranteed to engage; and
      * the FINAL failure block is deliberately WIDER than an even 50/50
        split's tail, and the assertion sits at the START of that block. A
        50/50 tail therefore keeps only the trailing "short test summary
        info" footer and THROWS THE ASSERTION AWAY — which is exactly the bug
        G16 describes, reproduced on demand.

    Built in linear time; the filler grows in the middle and the failure block
    stays at the very end.
    """
    preamble = "collecting... " + "\n".join(f"collected item {i}" for i in range(1200))
    filler = "\n".join(
        f"log line {i} ................................" for i in range(400)
    )
    # A realistic long failure body: a wide traceback frame listing between
    # the assertion and the footer. Its width is chosen so the assertion's
    # SOURCE LINE sits between an even split's 1500-char tail and the
    # pytest-shaped 2550-char tail — which is what makes the head_ratio the
    # difference between keeping and losing the answer. The helper asserts
    # that property rather than leaving it to a comment.
    traceback_frame = "\n".join(
        f"E         {index:>3}|     result = compute(value)" for index in range(1, 41)
    )
    body = (
        "\n=================================== FAILURES "
        "==================================\n"
        "________________________________ test_value ___________________________________\n"
        "\n"
        "    def test_value():\n>       assert compute(2) == 5\n"
        "E       assert 4 == 5\n\n"
        "E        +  where 4 = compute(2)\n"
        f"{traceback_frame}\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_mod.py::test_value - assert 4 == 5\n"
        "============================= 1 failed in 9.42s ==============================\n"
    )
    # The required test is about a 20k output; assert the shape is what we
    # think it is rather than silently testing a smaller string.
    source_line = ">       assert compute(2) == 5"
    distance = len(body) - body.index(source_line)
    assert 1500 < distance < 2550, (
        "the assertion's source line must be out of reach for a 50/50 tail "
        f"(1500) and in reach for a pytest tail (2550); it is at {distance}"
    )
    head = preamble + "\n" + filler + "\n"
    need = max(0, chars - len(head) - len(body)) + 1
    unit = "." * 60 + "\n"
    block = "\n" + unit * (need // len(unit) + 4)
    text = head + block + body
    assert len(text) > chars
    return text


# ===========================================================================
# 1. Required: `sleep 300` is aborted in <5 seconds
# ===========================================================================


@requires_docker
def test_required_sleep_300_is_aborted_in_under_5_seconds_docker(tmp_path):
    """REQUIRED TEST 1 (real Docker container kill).

    A real `sleep 300` runs in a real sandboxed container. A cancellation
    token — the same token a steering abort signals — must kill the container
    and return well inside the five-second bound, with the documented exit 130
    (not a timeout 124). This is a real process kill, not a simulated one.
    """
    from execution.sandbox import execute_sandboxed
    from execution.workspace import CancellationToken

    root = _make_workdir(tmp_path)
    token = CancellationToken()
    box: Dict[str, Any] = {}

    def _run() -> None:
        box["result"] = execute_sandboxed(
            str(root), "sleep 300", 600, cancellation_token=token
        )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    time.sleep(1.0)  # the container is genuinely running `sleep 300`

    started = time.monotonic()
    token.cancel()
    worker.join(30)

    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "the sandboxed sleep 300 was never stopped"
    assert elapsed < ABORT_BOUND_S, (
        f"abort took {elapsed:.2f}s (bound {ABORT_BOUND_S}s)"
    )
    result = box["result"]
    assert result.exit_code == 130
    assert result.timed_out is False


def test_required_sleep_300_is_aborted_in_under_5_seconds_local(tmp_path):
    """REQUIRED TEST 1, local lane (no Docker required).

    Same contract against a REAL host process tree: a child sleeping 300s is
    killed by the cancellation token inside the five-second bound, and the
    result is reported as cancelled (exit 130) rather than as a timeout.
    """
    from execution.workspace import CancellationToken, start_local_execution

    root = _make_workdir(tmp_path)
    token = CancellationToken()
    handle = start_local_execution(
        root,
        f'{sys.executable} -c "import time; time.sleep(300)"',
        cancellation_token=token,
        timeout_s=600,
    )
    time.sleep(1.0)  # the child is genuinely running

    started = time.monotonic()
    handle.cancel()
    result = handle.wait(30)
    elapsed = time.monotonic() - started

    assert elapsed < ABORT_BOUND_S, (
        f"abort took {elapsed:.2f}s (bound {ABORT_BOUND_S}s)"
    )
    assert result.cancelled is True
    assert result.exit_code == 130
    assert result.timed_out is False


def test_bash_session_forwards_the_token_and_its_cancel_stops_a_blocking_call(tmp_path):
    """The harness half of REQUIRED TEST 1: the token actually reaches the
    sandbox, so the loop's `session.cancel()` is what stops the process.

    The injected sandbox blocks until the token reports cancellation, which is
    exactly the contract the real sandbox implements (50ms poll -> kill).
    """
    root = _make_workdir(tmp_path)
    seen: Dict[str, Any] = {}

    def _sandbox(repo_path, command, timeout_s, cancellation_token=None):
        seen["token"] = cancellation_token
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cancellation_token is not None and cancellation_token.is_cancelled():
                return _result(130, "cancelled")
            time.sleep(0.02)
        return _result(124, "", timed_out=True)

    def _result(exit_code, stdout, stderr="", timed_out=False):
        from shared.types import ExecutionResult

        return ExecutionResult(exit_code, stdout, stderr, timed_out)

    set_execute_sandboxed(_sandbox)
    try:
        from execution.workspace import CancellationToken

        session = BashSession(
            str(root), 600, 3000, cancellation_token=CancellationToken()
        )
        box: Dict[str, Any] = {}
        worker = threading.Thread(
            target=lambda: box.setdefault("out", session.run("sleep 300")), daemon=True
        )
        worker.start()
        time.sleep(0.3)

        started = time.monotonic()
        session.cancel()
        worker.join(20)
        elapsed = time.monotonic() - started

        assert not worker.is_alive()
        assert elapsed < ABORT_BOUND_S
        assert seen["token"] is not None, "the sandbox never received the token"
        assert session.cancelled is True
        assert "exit=130" in box["out"]
    finally:
        reset_overrides()


def test_bash_session_still_works_with_a_legacy_three_arg_sandbox(tmp_path):
    """The compatibility guarantee: a sandbox that only accepts the Boundary-1
    three positional args must keep working unchanged (no token is forwarded,
    no TypeError is raised)."""
    root = _make_workdir(tmp_path)
    calls: List[tuple] = []

    def _legacy(repo_path, command, timeout_s):
        calls.append((repo_path, command, timeout_s))
        from shared.types import ExecutionResult

        return ExecutionResult(0, "ok", "", False)

    set_execute_sandboxed(_legacy)
    try:
        from execution.workspace import CancellationToken

        session = BashSession(
            str(root), 30, 3000, cancellation_token=CancellationToken()
        )
        assert "exit=0" in session.run("echo hi")
        assert len(calls) == 1
    finally:
        reset_overrides()


# ===========================================================================
# 2. Required: two provider 502s recover without losing the run
# ===========================================================================


def test_required_two_provider_502s_recover_without_losing_the_run(tmp_path):
    """REQUIRED TEST 2: two 502s are retried, and the run completes.

    Two things are asserted, and both matter:
      * the run is NOT lost — the model eventually answers and the task
        reaches a real outcome;
      * every retry decision is a `model_recovery` event, and the two failed
        attempts are recorded as retried rather than as the end of the run.
    """
    sink = _Sink()
    replies = ["python -m pytest -x tests -q", "SUBMIT"]
    model = _FlakyModel(
        [
            _ProviderError("upstream returned 502 bad gateway", 502),
            _ProviderError("upstream returned 502 bad gateway", 502),
        ],
        replies,
    )
    recovery = te.ModelRecovery(
        trace=sink, max_attempts=3, base_backoff_s=0.0, cap_backoff_s=0.0, label="t"
    )

    out = recovery.call(
        lambda: model(
            [],
        ),
        step="step-1",
    )

    assert out == replies[0], "the recovered reply was not returned to the loop"
    assert model.calls == 3, "expected 1 failed + 2 retried calls"
    events = [e["data"] for e in sink.events if e["kind"] == "model_recovery"]
    assert len(sink.events) == 2, f"expected one event per decision, got {sink.kinds()}"
    assert all(e["data"]["kind"] == te.KIND_MODEL_UNAVAILABLE for e in sink.events)
    assert [e["data"]["action"] for e in sink.events] == ["retry", "retry"]
    assert [e["data"]["attempt"] for e in sink.events] == [1, 2]
    assert all(e["data"]["status_code"] == 502 for e in sink.events)
    assert all(e["data"]["retryable"] is True for e in sink.events)
    assert events[0]["backoff_s"] >= 0.0
    assert recovery.report()["recoveries"] == 2
    assert recovery.last_failure.kind == te.KIND_MODEL_UNAVAILABLE


def test_two_502s_recover_through_the_real_fix_loop(tmp_path):
    """REQUIRED TEST 2 through the REAL loop (`core.run_step`).

    Proves the wiring, not just the helper: the step session calls the model
    through ModelRecovery, two 502s happen mid-step, and the step still
    finishes on the third call with its work verified-shaped output intact.
    """
    root = _make_workdir(tmp_path)
    log_dir = tmp_path / "logs" / "t1"
    log_dir.mkdir(parents=True, exist_ok=True)
    from harness.context import TaskState
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    trace = TraceLogger(log_dir)
    trace.log("task_start", {"task_id": "t1"})

    def _verify(*_args, **_kwargs):
        from shared.types import VerificationResult

        return VerificationResult(True, True, True, False, "ok")

    model = _FlakyModel(
        [
            _ProviderError("upstream returned 502 bad gateway", 502),
            _ProviderError("upstream returned 502 bad gateway", 502),
        ],
        ['python -c "print(1)"', "SUBMIT"],
    )
    set_call_model(model)
    try:
        ok, note, _ = core_mod.run_step(
            task=Task(task_id="t1", issue_text="do the thing", repo_path=str(root)),
            step={"id": 1, "description": "print 1", "files_hint": []},
            plan=[{"id": 1, "description": "print 1", "files_hint": []}],
            cfg=get_config({}),
            paths=_paths(tmp_path / "logs", "t1", root),
            state=TaskState(log_dir, "t1"),
            trace=trace,
            model=ModelClient(trace, get_config({}), None),
            step_files=[],
            completed=[],
            feedback="",
            verify=_verify,
            deadline=time.time() + 120,
            # Bounded, non-sleeping backoff: the RECOVERY is under test, not
            # the wall clock of a real provider.
            policy=te.recovery_policy_from_config(get_config({}), root=str(root)),
            model_recovery=te.ModelRecovery(
                trace=trace, max_attempts=3, base_backoff_s=0.0, cap_backoff_s=0.0
            ),
        )
    finally:
        reset_overrides()

    assert ok is True, f"the step did not survive two 502s: {note}"
    assert "FATAL" not in note
    assert model.calls == 4, f"expected 2 failures + 2 replies, got {model.calls}"
    assert trace.read_all() is not None


def test_terminal_model_failures_are_classified_separately_and_not_retried():
    """G15's other half: auth and bad-request are TERMINAL and are reported
    with their own kind, so a run says WHY it stopped instead of collapsing
    every provider problem into "the model failed"."""
    for exc, expected_kind in (
        (_ProviderError("invalid api key", 401), "model_auth"),
        (
            _ProviderError("invalid_request_error: bad request", 400),
            "model_bad_request",
        ),
        (ValueError("a harness bug, not a provider problem"), "model_internal"),
    ):
        failure = te.classify_model_failure(exc)
        assert failure.kind == expected_kind, (exc, failure)
        assert failure.retryable is False
        assert failure.terminal is True
        assert failure.backoff_s == 0.0, (
            "a terminal failure must not schedule a backoff"
        )


def test_harness_internal_exception_is_not_mistaken_for_a_provider_outage():
    """A harness TypeError that merely mentions 'timeout' must not be retried
    as a provider outage — retrying a coding bug three times is noise."""
    failure = te.classify_model_failure(TypeError("unsupported operand for timeout_ms"))
    assert failure.kind == "model_internal"
    assert failure.retryable is False


def test_backoff_is_bounded_on_both_ends():
    delays = [te.backoff_s(n, base_s=0.5, cap_s=8.0) for n in range(1, 9)]
    assert delays[:4] == [0.5, 1.0, 2.0, 4.0]
    assert max(delays) == 8.0, "backoff must be capped"
    assert all(d <= 8.0 for d in delays)


def test_rate_limit_is_retryable_and_a_slow_provider_is_tolerated():
    assert (
        te.classify_model_failure(_ProviderError("rate limit reached", 429)).kind
        == "model_rate_limited"
    )

    class APITimeoutError(Exception):
        """The shape production actually raises: a provider-named class with
        no status code. Classification must read the class name, not only the
        HTTP status."""

    slow = te.classify_model_failure(APITimeoutError("Read timed out."))
    assert slow.kind == "model_timeout"
    assert slow.retryable is True


def test_model_recovery_gives_up_after_the_bounded_budget():
    """The bound is real: `max_attempts` is a ceiling, not a suggestion."""
    sink = _Sink()
    calls = {"n": 0}

    def _always_502():
        calls["n"] += 1
        raise _ProviderError("502 bad gateway", 502)

    recovery = te.ModelRecovery(
        trace=sink, max_attempts=3, base_backoff_s=0.0, cap_backoff_s=0.0
    )
    with pytest.raises(_ProviderError):
        recovery.call(_always_502, step="s")

    assert calls["n"] == 3, "the retry budget was not respected"
    assert [e["data"]["action"] for e in sink.events] == ["retry", "retry", "give_up"]
    assert recovery.report()["recoveries"] == 2


# ===========================================================================
# 3. Required: a timeout produces a narrower next command
# ===========================================================================


def test_required_a_timeout_produces_a_narrower_next_command(tmp_path):
    """REQUIRED TEST 3: a timeout changes the NEXT command, not just the prose.

    Two coupled effects are asserted: the command is genuinely narrower (a
    real subset of the work), and the bounded per-command budget was raised.
    """
    root = _make_workdir(tmp_path)
    policy = te.recovery_policy_from_config(
        {"command_timeout_s": 120, "recovery_max_timeout_s": 600}, root=str(root)
    )
    wide = "python -m pytest tests -q"

    action = policy.on_tool_error(
        te.ToolError("timeout", "command timed out"), command=wide
    )

    assert action.kind == "timeout"
    assert action.action == "narrow_and_extend"
    assert action.next_command, "no narrower command was produced"
    assert action.next_command != wide
    # Genuinely narrower: it stops at the first failure and shrinks the
    # traceback volume, which is exactly the evidence a repair turn needs.
    assert "-x" in action.next_command
    assert "--tb=short" in action.next_command
    assert "tests" in action.next_command, "narrowing dropped the target scope"
    # The bounded budget grew, and is bounded.
    assert action.timeout_s == 180
    assert policy.next_timeout_s() == 180
    assert action.timeout_s < 600

    # A second timeout keeps escalating but never past the cap.
    second = policy.on_tool_error(
        te.ToolError("timeout", "command timed out"), command=wide
    )
    assert second.timeout_s == 270
    for _ in range(20):
        capped = policy.on_tool_error(
            te.ToolError("timeout", "command timed out"), command=wide
        )
    assert capped.timeout_s == 600, "the timeout escalation is not bounded"


@pytest.mark.parametrize(
    ("wide", "narrower", "must_contain"),
    [
        ("python -m pytest tests -q", None, "-x"),
        ("pytest tests/test_a.py", None, "-x"),
        ("find . -name '*.pyc'", None, "-maxdepth"),
        ("grep -rn TODO .", None, "--include=*.py"),
        ("rg TODO", None, "-g"),
        # Already bounded on failure count: `-x` would conflict with
        # --maxfail, so only the traceback volume is narrowed.
        ("python -m pytest --maxfail=2 tests", None, "--tb=short"),
        # `-x` already present: only the traceback volume is narrowed.
        ("python -m pytest -x tests -q", None, "--tb=short"),
        # `-v` bounds the OUTPUT shape, not the run: stopping at the first
        # failure is still strictly less work.
        ("python -m pytest -v --tb=long tests", None, "-x"),
    ],
)
def test_narrower_command_rewrites_the_shapes_it_can_bound(
    wide, narrower, must_contain
):
    result = te.narrower_command(wide)
    assert result, f"no narrowing produced for {wide!r}"
    assert must_contain in result
    assert result != wide


@pytest.mark.parametrize(
    "command",
    [
        "",
        "cat notes.txt",
        "python -m pytest -x --tb=short tests -q",  # nothing left to narrow
        "find . -maxdepth 3 -name '*.pyc'",
        "grep TODO notes.txt",  # not recursive; nothing to bound
        "grep -rn --include='*.md' TODO .",  # already filtered
    ],
)
def test_narrower_command_returns_none_rather_than_inventing_a_rewrite(command):
    """No safe narrowing => None, so the loop falls back to 'run a smaller
    slice' rather than corrupting a command it does not understand."""
    assert te.narrower_command(command) is None


def test_narrower_command_preserves_a_shell_tail():
    """A redirect/pipe tail must survive narrowing, or the narrowed command
    would silently change behaviour."""
    result = te.narrower_command("python -m pytest tests -q | tee out.log")
    assert result is not None
    assert "tee out.log" in result, result
    assert "-x" in result


def test_timeout_recovery_reaches_the_real_session_budget(tmp_path):
    """End-to-end inside one step: after a real timeout, the session's
    per-command budget is genuinely raised for the next command."""
    root = _make_workdir(tmp_path)
    timeouts = {"n": 0}

    def _sandbox(repo_path, command, timeout_s):
        timeouts.setdefault("budgets", []).append(timeout_s)
        from shared.types import ExecutionResult

        timeouts["n"] += 1
        if timeouts["n"] == 1:
            return ExecutionResult(124, "", "", True)
        return ExecutionResult(0, "ok", "", False)

    set_execute_sandboxed(_sandbox)
    try:
        session = BashSession(str(root), 120, 3000)
        first = session.run("python -m pytest tests -q")
        assert "TOOL ERROR [timeout]" in first
        assert session.last_error is not None
        assert session.last_error.kind == "timeout"

        policy = te.recovery_policy_from_config(
            {"command_timeout_s": 120}, root=str(root)
        )
        action = policy.on_tool_error(
            session.last_error, command="python -m pytest tests -q"
        )
        session.set_timeout(action.timeout_s)
        session.run(action.next_command)
    finally:
        reset_overrides()

    assert timeouts["budgets"] == [120, 180], timeouts["budgets"]


# ===========================================================================
# 4. Required: a 20k-character failing test output includes the assertion
# ===========================================================================


def test_required_20k_failing_test_output_includes_the_assertion():
    """REQUIRED TEST 4: a truncated 20k failing pytest run still carries its
    assertion — and the tail-preferring shape is what buys that, which is
    asserted by comparing against the even split the old code used."""
    output = _failing_pytest_output(20000)
    assert len(output) > 20000

    shaped = te.shape_tool_output(output, 3000)

    assert len(shaped) < len(output)
    # The explicit omission marker, with a true count — a cap is never silent.
    marker = te.OMISSION_RE.search(shaped)
    assert marker, "truncation did not state how much was omitted"
    assert int(marker.group(1)) > 0
    # The answer survived: the failure's own source line, the rendered
    # assertion, the test name and the summary footer.
    source_line = ">       assert compute(2) == 5"
    assert source_line in shaped
    assert "E       assert 4 == 5" in shaped
    assert "test_value" in shaped
    assert "short test summary info" in shaped

    # ... and the tail-preferring shape is load-bearing: the historical even
    # split DROPS the assertion's source line on this same input.
    even = te.shape_tool_output(output, 3000, head_ratio=0.5)
    assert source_line not in even, (
        "the even split kept the assertion, so this test no longer "
        "discriminates the pytest-tail policy"
    )


def test_shape_tool_output_contract():
    short = "x" * 100
    assert te.shape_tool_output(short, 3000) == short, "short output must be untouched"
    assert te.shape_tool_output(
        short, 10
    ) == short or "omitted" in te.shape_tool_output(short, 10)
    assert te.shape_tool_output("abc", 0) == ""
    assert te.shape_tool_output(None, 100) == "None"  # never raises
    long_text = "A" * 500
    shaped = te.shape_tool_output(long_text, 100, head_ratio=0.5)
    assert shaped.startswith("A") and shaped.rstrip().endswith("A")
    assert "omitted" in shaped


def test_pytest_detection_is_a_cheap_marker_vote():
    assert te.is_pytest_shaped("= FAILURES =\nE   assert 1 == 2\n=== 1 failed ===")
    assert not te.is_pytest_shaped("hello world\nsome plain output\n")
    assert te.is_pytest_shaped("") is False


def test_pytest_detection_samples_the_tail_not_just_the_head():
    """The regression: a long pytest run opens with a collection log that
    contains no marker, so a head-only check missed it and fell back to the
    even split that throws the assertion away."""
    long_collect_log = "collecting... " + "\n".join(
        f"collected item {i}" for i in range(2000)
    )
    assert len(long_collect_log) > 20000
    assert not te.is_pytest_shaped(long_collect_log), "a bare log is not pytest"
    pytest_after_a_long_log = (
        long_collect_log
        + "\n= FAILURES =\nE       assert 4 == 5\n"
        + "=========================== short test summary info ============================\n"
        + "FAILED tests/test_mod.py::test_value - assert 4 == 5\n"
        + "============================= 1 failed in 9.42s ==============================\n"
    )
    assert te.is_pytest_shaped(pytest_after_a_long_log)
    assert "E       assert 4 == 5" in te.shape_tool_output(
        pytest_after_a_long_log, 3000
    )


def test_the_bash_session_shapes_output_with_the_pytest_tail_preference(tmp_path):
    """The preference is on the LIVE path, not just in the helper."""
    root = _make_workdir(tmp_path)
    big = _failing_pytest_output(20000)

    def _sandbox(repo_path, command, timeout_s):
        from shared.types import ExecutionResult

        return ExecutionResult(1, big, "", False)

    set_execute_sandboxed(_sandbox)
    try:
        session = BashSession(str(root), 60, 3000)
        out = session.run("python -m pytest tests -q")
    finally:
        reset_overrides()

    assert "assert 4 == 5" in out
    assert "omitted" in out
    assert len(out) < len(big)


# ===========================================================================
# 5. Required: repeated identical calls trigger loop protection
# ===========================================================================


def test_required_repeated_identical_calls_trigger_loop_protection():
    """REQUIRED TEST 5: the third identical command is NOT dispatched and the
    turn is stopped, with the refusal recorded.

    The command is a BUILD, not a read — a repeated read is exempt by default
    (see the exemption tests below), and a repeated build is the actual doom
    loop this policy exists to stop.
    """
    policy = te.RecoveryPolicy(max_repeat_command=2)
    command = "python -m pytest tests/test_a.py -q"

    assert policy.note_command(command) is None, "call 1 must be allowed"
    assert policy.note_command(command) is None, "call 2 must be allowed"
    action = policy.note_command(command)

    assert action is not None, "loop protection did not trip on the third call"
    assert action.kind == "loop_detected"
    assert action.action == "stop_and_ask"
    assert action.stop is True
    assert action.attempts == 3
    assert "REPEATED CALL #3" in action.evidence
    # The refused command is released for the caller's refusal message and
    # then disarmed, so the guard is one-shot per offending command.
    assert policy.release_blocked_command() == command
    assert policy.release_blocked_command() is None


def test_loop_protection_exempts_repeated_reads_by_default():
    """A model re-reading a file after its own edit is legitimate, and the
    existing loop contracts depend on it: this is a real regression the Docker
    e2e lane caught, so it is pinned here as a contract.
    """
    policy = te.RecoveryPolicy(max_repeat_command=1)
    for _ in range(8):
        assert policy.note_command("cat numlib/mathutil.py") is None
    # A repeated MUTATING command is never exempt.
    assert policy.note_command("sed -i s/a/b/ f.py") is None
    assert policy.note_command("sed -i s/a/b/ f.py") is not None


def test_read_only_classification_is_conservative_in_both_directions():
    assert te.is_read_only_command("cat a.py")
    assert te.is_read_only_command("git status")
    # pytest is NOT read-only: it writes caches and runs arbitrary test code,
    # and a repeated full run is exactly the doom loop worth stopping.
    assert not te.is_read_only_command("python -m pytest tests -q")
    assert not te.is_read_only_command('python -c \'open("f","w").write(1)\'')
    assert not te.is_read_only_command("cat a.py > b.py"), "a redirect is a write"
    assert not te.is_read_only_command("sed -i s/a/b/ f.py")
    assert not te.is_read_only_command("")
    # The guard can be configured to include reads.
    strict = te.RecoveryPolicy(max_repeat_command=1, loop_guard_read_only=True)
    assert strict.note_command("cat a.py") is None
    assert strict.note_command("cat a.py") is not None


def test_loop_protection_distinguishes_different_commands():
    policy = te.RecoveryPolicy(max_repeat_command=1)
    assert policy.note_command("python -m build") is None
    assert policy.note_command("python -m build") is not None
    assert policy.note_command("python -m install .") is None, (
        "a different command must be allowed"
    )


def test_loop_protection_normalizes_whitespace():
    policy = te.RecoveryPolicy(max_repeat_command=1)
    assert policy.note_command("python   -m build") is None
    assert policy.note_command("python -m build") is not None


def test_repeated_identical_bash_calls_stop_the_real_step(tmp_path):
    """REQUIRED TEST 5 through the REAL loop: a model that keeps issuing the
    same BUILDING command loses the remaining turns instead of burning them,
    and the refusal is in the trace."""
    root = _make_workdir(tmp_path)
    log_dir = tmp_path / "logs" / "t2"
    log_dir.mkdir(parents=True, exist_ok=True)
    from harness.context import TaskState
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    trace = TraceLogger(log_dir)
    trace.log("task_start", {"task_id": "t2"})
    ran: List[str] = []

    def _sandbox(repo_path, command, timeout_s):
        ran.append(command)
        from shared.types import ExecutionResult

        return ExecutionResult(1, "still failing", "", False)

    repeated = "python -m pytest tests -q"
    set_execute_sandboxed(_sandbox)
    set_call_model(lambda *a, **k: repeated)
    try:
        ok, note, _ = core_mod.run_step(
            task=Task(task_id="t2", issue_text="fix it", repo_path=str(root)),
            step={"id": 1, "description": "fix", "files_hint": []},
            plan=[{"id": 1, "description": "fix", "files_hint": []}],
            cfg=get_config({"max_step_turns": 8, "recovery_max_repeat_command": 2}),
            paths=_paths(tmp_path / "logs", "t2", root),
            state=TaskState(log_dir, "t2"),
            trace=trace,
            model=ModelClient(trace, get_config({}), None),
            step_files=[],
            completed=[],
            feedback="",
            verify=_ok_verify,
            deadline=time.time() + 120,
        )
    finally:
        reset_overrides()

    assert ok is False
    assert note.startswith("LOOP-GUARD"), note
    # Two identical executions were allowed; the third was REFUSED, not run.
    assert len(ran) == 2, f"the refused call was still dispatched: {ran}"
    assert _events_of_kind(trace, "loop_guard"), "no loop_guard event was recorded"


def test_repeated_reads_do_not_stop_the_real_step(tmp_path):
    """The exemption, end to end through the REAL loop: a model re-reading a
    file must still be allowed to finish its turn budget.

    This is a real regression the Docker e2e lane caught
    (`test_verified_success_state_complete_after_exhausted_turns` scripted three
    identical `cat` calls and expected the exhausted-turns path), so the
    exemption is pinned as a contract on both the policy and the loop.
    """
    root = _make_workdir(tmp_path)
    log_dir = tmp_path / "logs" / "t8"
    log_dir.mkdir(parents=True, exist_ok=True)
    from harness.context import TaskState
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    trace = TraceLogger(log_dir)
    trace.log("task_start", {"task_id": "t8"})
    ran: List[str] = []

    def _sandbox(repo_path, command, timeout_s):
        ran.append(command)
        from shared.types import ExecutionResult

        return ExecutionResult(0, "VALUE = 1", "", False)

    replies = ["cat pkg/mod.py"] * 4 + ["SUBMIT"]
    set_execute_sandboxed(_sandbox)
    set_call_model(lambda *a, **k: replies.pop(0))
    try:
        ok, note, _ = core_mod.run_step(
            task=Task(task_id="t8", issue_text="look at it", repo_path=str(root)),
            step={"id": 1, "description": "look", "files_hint": []},
            plan=[{"id": 1, "description": "look", "files_hint": []}],
            cfg=get_config({"max_step_turns": 6, "recovery_max_repeat_command": 2}),
            paths=_paths(tmp_path / "logs", "t8", root),
            state=TaskState(log_dir, "t8"),
            trace=trace,
            model=ModelClient(trace, get_config({}), None),
            step_files=[],
            completed=[],
            feedback="",
            verify=_ok_verify,
            deadline=time.time() + 120,
        )
    finally:
        reset_overrides()

    assert ok is True, note
    assert len(ran) == 4, f"a repeated read was refused: {ran}"
    assert not _events_of_kind(trace, "loop_guard")


def test_loop_guard_stops_the_agent_loop_too(tmp_path):
    """The same guard on the legacy agent path."""
    from harness.agent_loop import run_agent

    root = _make_workdir(tmp_path)
    ran: List[str] = []

    def _sandbox(repo_path, command, timeout_s):
        ran.append(command)
        from shared.types import ExecutionResult

        return ExecutionResult(1, "still failing", "", False)

    replies = ['{"tool": "BASH", "command": "python -m pytest tests -q"}'] * 8
    set_execute_sandboxed(_sandbox)
    set_call_model(lambda *a, **k: replies.pop(0) if replies else "DONE")
    try:
        result = run_agent(
            request="fix the failing test",
            repo_path=str(root),
            # R2-04: the default engine is now `daily`; this test drives the
            # LEGACY agent path (its own docstring says so) via the legacy
            # `BASH` verb and the `set_execute_sandboxed` boundary, so it asks
            # for the compatibility engine by name. The assertions below --
            # the guard's repeats count, the `loop_guard` event, and the
            # recovery_stats receipt -- are unchanged.
            config=get_config(
                {
                    "agent_max_turns": 8,
                    "recovery_max_repeat_command": 2,
                    "agent_strategy": "legacy_agent",
                }
            ),
            log_root=tmp_path / "logs",
            task_id="t3",
        )
    finally:
        reset_overrides()

    assert result["status"] == "failed"
    assert len(ran) == 2, f"the refused call was still dispatched: {ran}"
    events = _read_trace(result["trace_path"])
    guards = [e for e in events if e.get("kind") == "loop_guard"]
    assert guards, "no loop_guard event reached the session journal"
    assert guards[0]["data"]["repeats"] == 3
    stats = [e for e in events if e.get("kind") == "recovery_stats"]
    assert stats, "no recovery_stats receipt was emitted"
    assert "loop_detected" in stats[-1]["data"]["policy"]["by_kind"]


def _read_trace(path: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ===========================================================================
# 6. Required: steering arrives at the next boundary, journal intact
# ===========================================================================


def test_required_steering_arrives_at_the_next_boundary_without_corrupting_the_journal(
    tmp_path,
):
    """REQUIRED TEST 6: a guide injected while the model is thinking is
    delivered at the next tool boundary, and the journal stays a valid
    append-only log of inject/consume pairs."""
    root = _make_workdir(tmp_path)
    log_dir = tmp_path / "logs" / "t4"
    log_dir.mkdir(parents=True, exist_ok=True)
    from harness.context import TaskState
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    trace = TraceLogger(log_dir)
    trace.log("task_start", {"task_id": "t4"})
    injected: Dict[str, Any] = {}

    class _InjectingModel:
        """Injects steering right after the FIRST reply is produced — i.e.
        while the loop is between the model call and the tool boundary."""

        def __init__(self) -> None:
            self.calls = 0
            self.seen: List[List[Dict[str, str]]] = []

        def __call__(self, messages, *_args, **_kwargs):
            self.calls += 1
            self.seen.append([dict(m) for m in messages])
            if self.calls == 1:
                from harness.steering import SteeringBuffer

                buffer = SteeringBuffer(log_dir, "t4")
                event = buffer.inject("only touch the parser file", intent="guide")
                injected["event"] = event
                return 'python -c "print(1)"'
            return "SUBMIT"

        def get_last_usage(self):
            return {
                "model": "scripted",
                "provider": "fake",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "tokens": 2,
                "cost_usd": 0.0,
            }

    model = _InjectingModel()
    steer = steering_mod.SteeringBuffer(log_dir, "t4")

    def _sandbox(repo_path, command, timeout_s):
        from shared.types import ExecutionResult

        return ExecutionResult(0, "1", "", False)

    set_execute_sandboxed(_sandbox)
    set_call_model(lambda *a, **k: model(*a, **k))
    try:
        ok, note, _ = core_mod.run_step(
            task=Task(task_id="t4", issue_text="x", repo_path=str(root)),
            step={"id": 1, "description": "s", "files_hint": []},
            plan=[{"id": 1, "description": "s", "files_hint": []}],
            cfg=get_config({"max_step_turns": 4}),
            paths=_paths(tmp_path / "logs", "t4", root),
            state=TaskState(log_dir, "t4"),
            trace=trace,
            model=ModelClient(trace, get_config({}), None),
            step_files=[],
            completed=[],
            feedback="",
            verify=_ok_verify,
            deadline=time.time() + 60,
            steer=steer,
        )
    finally:
        reset_overrides()

    assert injected["event"] is not None, "the steering inject was refused"
    assert ok is True, note

    # Delivered: the instruction is in a LATER model request, not the first.
    delivered = [
        m
        for turn_messages in model.seen[1:]
        for m in turn_messages
        if "only touch the parser file" in m["content"]
    ]
    assert delivered, "the steering text never reached the model"

    # The journal is intact: every line parses, and the inject has a matching
    # consume recorded by the loop's single consume point.
    lines = [
        json.loads(line)
        for line in (log_dir / "steering.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert all(isinstance(row, dict) for row in lines)
    injects = [row for row in lines if row.get("op") == "inject"]
    consumes = [row for row in lines if row.get("op") == "consume"]
    assert len(injects) == 1
    assert len(consumes) == 1
    assert consumes[0]["seq"] == injects[0]["seq"]
    assert not steer.pending(), "the event was consumed twice"
    assert _events_of_kind(trace, "steering"), "no steering event reached the trace"


def test_hard_abort_of_an_in_flight_command_terminates_it_and_keeps_the_result(
    tmp_path,
):
    """G14 end-to-end: a hard abort injected while a command runs kills the
    command, preserves its output, and leaves a resumable trace."""
    root = _make_workdir(tmp_path)
    log_dir = tmp_path / "logs" / "t5"
    log_dir.mkdir(parents=True, exist_ok=True)
    from harness.context import TaskState
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    trace = TraceLogger(log_dir)
    trace.log("task_start", {"task_id": "t5"})

    def _sandbox(repo_path, command, timeout_s, cancellation_token=None):
        # A long command that the cancellation token really does stop, and
        # that leaves partial output behind — the shape of an aborted build.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cancellation_token is not None and cancellation_token.is_cancelled():
                from shared.types import ExecutionResult

                return ExecutionResult(130, "partial build output", "", False)
            time.sleep(0.02)
        from shared.types import ExecutionResult

        return ExecutionResult(0, "done", "", False)

    class _AbortAfterFirstReply:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, messages, *_args, **_kwargs):
            self.calls += 1
            if self.calls >= 2:
                return "SUBMIT"
            return 'python -c "build_everything()"'

        def get_last_usage(self):
            return {
                "model": "scripted",
                "provider": "fake",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "tokens": 2,
                "cost_usd": 0.0,
            }

    model = _AbortAfterFirstReply()
    steer = steering_mod.SteeringBuffer(log_dir, "t5")

    # Inject the hard abort a beat after the command starts.
    def _inject_soon() -> None:
        time.sleep(0.4)
        steer.inject("stop, that is the wrong approach", intent="abort")

    injector = threading.Thread(target=_inject_soon, daemon=True)

    set_execute_sandboxed(_sandbox)
    set_call_model(lambda *a, **k: model(*a, **k))
    try:
        injector.start()
        started = time.monotonic()
        ok, note, _ = core_mod.run_step(
            task=Task(task_id="t5", issue_text="x", repo_path=str(root)),
            step={"id": 1, "description": "s", "files_hint": []},
            plan=[{"id": 1, "description": "s", "files_hint": []}],
            cfg=get_config({"max_step_turns": 4, "command_timeout_s": 600}),
            paths=_paths(tmp_path / "logs", "t5", root),
            state=TaskState(log_dir, "t5"),
            trace=trace,
            model=ModelClient(trace, get_config({}), None),
            step_files=[],
            completed=[],
            feedback="",
            verify=_ok_verify,
            deadline=time.time() + 120,
            steer=steer,
        )
        elapsed = time.monotonic() - started
    finally:
        reset_overrides()
        injector.join(10)

    assert ok is False
    assert note.startswith("STEER-ABORT"), note
    assert elapsed < ABORT_BOUND_S, (
        f"the in-flight command was not stopped inside the bound: {elapsed:.2f}s"
    )
    event = _events_of_kind(trace, "steering_abort_in_flight")
    assert event, "the in-flight abort was not recorded"
    # The in-flight result is PRESERVED, not silently discarded.
    assert event[0]["result_preserved_chars"] > 0
    assert event[0]["elapsed_s"] < ABORT_BOUND_S
    assert event[0]["watcher_joined"] is True
    assert not _events_of_kind(trace, "steering_abort_watcher_error"), (
        "the interrupt itself failed; the abort must not read as successful"
    )
    # The abort is still PENDING after the step: `run_step` never consumes a
    # hard intent, because the task loop's step-boundary handler owns the
    # single consume point. Consuming it exactly once is what follows.
    assert [e.intent for e in steer.pending()] == ["abort"]
    consumed = steer.take("abort-step-boundary")
    assert len(consumed) == 1
    assert not steer.pending(), "the event was consumed twice"
    rows = [
        json.loads(line)
        for line in (log_dir / "steering.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert sum(1 for row in rows if row.get("op") == "inject") == 1
    assert sum(1 for row in rows if row.get("op") == "consume") == 1


def test_hard_abort_watcher_never_consumes_the_event_it_observes(tmp_path):
    """The watcher observes; the loop's single consume point consumes. If the
    watcher consumed, a resumed run would lose the abort."""
    log_dir = tmp_path / "logs" / "t6"
    log_dir.mkdir(parents=True, exist_ok=True)
    buffer = steering_mod.SteeringBuffer(log_dir, "t6")
    fired: List[str] = []
    # The hook is a KILL hook: zero-argument by contract. This test pins that
    # contract, because a hook whose signature does not match fails silently
    # at the one moment it must not.
    watcher = steering_mod.HardAbortWatcher(buffer, lambda: fired.append("killed"))
    watcher.start()
    try:
        buffer.inject("abort this", intent="abort")
        deadline = time.monotonic() + 5
        while not watcher.aborted and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        assert watcher.stop(join_timeout_s=5) is True

    assert watcher.aborted is True
    assert fired == ["killed"]
    assert "abort this" in watcher.abort_reason
    assert buffer.pending(), "the watcher consumed the event it was watching for"
    assert watcher.elapsed_to_abort_s() is not None
    assert watcher.elapsed_to_abort_s() < ABORT_BOUND_S


def test_hard_abort_watcher_is_inert_without_a_buffer():
    watcher = steering_mod.HardAbortWatcher(None, lambda: None)
    assert watcher.start() is watcher
    assert watcher.stop() is True
    assert watcher.aborted is False
    assert watcher.elapsed_to_abort_s() is None
    assert watcher.abort_reason == "abort"


def test_hard_abort_watcher_records_a_failing_kill_hook_instead_of_hiding_it(
    tmp_path,
):
    """A raising kill hook must be RECORDED. The integration path writes
    `watcher.errors` to the trace, so a failed interrupt can never read as a
    successful one."""
    log_dir = tmp_path / "logs" / "t7"
    log_dir.mkdir(parents=True, exist_ok=True)
    buffer = steering_mod.SteeringBuffer(log_dir, "t7")

    def _boom() -> None:
        raise RuntimeError("kill hook failed")

    watcher = steering_mod.HardAbortWatcher(buffer, _boom)
    watcher.start()
    try:
        buffer.inject("abort", intent="abort")
        deadline = time.monotonic() + 5
        while not watcher.aborted and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        assert watcher.stop(join_timeout_s=5) is True
    assert watcher.aborted is True
    assert any("on_abort hook failed" in e for e in watcher.errors), watcher.errors
    # The event is STILL pending, so the loop's consume point still owns it.
    assert buffer.pending()


# ===========================================================================
# per-kind recovery policy: the other five kinds each do something real
# ===========================================================================


def test_file_not_found_attaches_a_real_directory_listing(tmp_path):
    """`file_not_found` -> attach a directory listing (not just a hint)."""
    root = _make_workdir(tmp_path)
    (root / "parser.py").write_text("x = 1\n", encoding="utf-8")
    policy = te.recovery_policy_from_config({}, root=str(root))
    action = policy.on_tool_error(
        te.ToolError("file_not_found", "file not found: parserx.py"),
        command="cat parserx.py",
    )
    assert action.kind == "file_not_found"
    assert action.action == "attach_listing"
    assert "parserx.py" in action.evidence
    assert "parser.py" in action.evidence, action.evidence
    assert "Contents of" in action.evidence
    assert policy.forbidden == (), "a missing file is not a forbidden path"


def test_permission_denied_forbids_the_path_and_refuses_it_next_time(tmp_path):
    """`permission_denied` -> stop retrying THAT path (a real guard, not prose)."""
    root = _make_workdir(tmp_path)
    (root / "etc").mkdir()
    (root / "etc" / "shadow").write_text("secret", encoding="utf-8")
    policy = te.recovery_policy_from_config({}, root=str(root))
    action = policy.on_tool_error(
        te.ToolError("permission_denied", "permission denied on path etc/shadow"),
        command="cat etc/shadow",
    )
    assert action.kind == "permission_denied"
    assert action.action == "forbid_path"
    assert "etc/shadow" in policy.forbidden
    assert policy.rejects("cat etc/shadow"), "the forbidden path was not refused"
    # A command whose ONLY paths are forbidden is refused without running.
    policy.on_tool_error(
        te.ToolError("permission_denied", "permission denied on path etc/passwd"),
        command="cat etc/passwd",
    )
    assert policy.rejects("cat etc/shadow etc/passwd")
    # ... but a command that ALSO touches an allowed path still runs: the
    # policy stops retrying the path, it does not stop the agent working
    # around it.
    assert policy.rejects("cat etc/shadow pkg/mod.py") is None
    assert policy.rejects("cat pkg/mod.py") is None
    assert policy.rejects("echo hello") is None


def test_malformed_patch_returns_the_exact_current_file_with_line_numbers(tmp_path):
    """`malformed_patch` -> the exact current file, numbered."""
    root = _make_workdir(tmp_path)
    target = root / "mod.py"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    (root / "fix.patch").write_text("--- a/mod.py\n+++ b/mod.py\n", encoding="utf-8")
    policy = te.recovery_policy_from_config({}, root=str(root))
    action = policy.on_tool_error(
        te.ToolError(
            "malformed_patch", "malformed patch: hunk doesn't apply at line 2"
        ),
        command="patch -p1 mod.py < fix.patch",
    )
    assert action.kind == "malformed_patch"
    assert action.action == "attach_numbered_file"
    assert "line one" in action.evidence
    assert "line three" in action.evidence
    # Numbered, and the numbers are the file's real line numbers.
    assert "1 | line one" in action.evidence
    assert "3 | line three" in action.evidence
    # The PATCH FILE is never presented as the file that failed to patch.
    assert "--- a/mod.py" not in action.evidence


def test_malformed_patch_never_returns_the_patch_file_as_the_target(tmp_path):
    """A command that names ONLY the patch must say so honestly rather than
    attaching the numbered contents of the diff as if it were the source."""
    root = _make_workdir(tmp_path)
    (root / "fix.patch").write_text("--- a/mod.py\n+++ b/mod.py\n", encoding="utf-8")
    policy = te.recovery_policy_from_config({}, root=str(root))
    action = policy.on_tool_error(
        te.ToolError("malformed_patch", "malformed patch: hunk doesn't apply"),
        command="patch -p1 < fix.patch",
    )
    assert "no target file" in action.evidence
    assert "--- a/mod.py" not in action.evidence


def test_malformed_patch_falls_back_to_the_last_touched_file(tmp_path):
    """When the command does not name a target, the session's own write
    history identifies it — the evidence must still be real."""
    root = _make_workdir(tmp_path)
    (root / "touched.py").write_text("alpha\nbeta\n", encoding="utf-8")
    policy = te.recovery_policy_from_config({}, root=str(root))
    action = policy.on_tool_error(
        te.ToolError("malformed_patch", "malformed patch: hunk doesn't apply"),
        command="patch -p1 < /dev/stdin",
        files_touched=["pkg/mod.py", "touched.py"],
    )
    assert "alpha" in action.evidence
    assert "touched.py" in action.evidence


def test_evidence_builders_degrade_honestly_instead_of_raising(tmp_path):
    """Recovery machinery must never be the reason a run dies."""
    root = _make_workdir(tmp_path)
    policy = te.recovery_policy_from_config({}, root=str(root))
    listing = policy.directory_listing("does/not/exist/at/all.py")
    assert "could not list" in listing or "Contents of" in listing
    numbered = policy.numbered_file("nope.py")
    assert "could not read" in numbered or "no target file" in numbered
    assert policy.numbered_file("") != ""
    assert policy.directory_listing("") != ""


def test_malformed_tool_call_restates_the_schema_and_gives_one_example():
    """`malformed_tool_call` -> restate the schema AND one worked example."""
    action = te.RecoveryPolicy().on_malformed_tool_call(
        "missing required argument 'path'",
        tool_schema='{"tool": "READ", "path": "<repo-relative path>"}',
        tool_example='{"tool": "READ", "path": "harness/core.py"}',
    )
    assert action.kind == "malformed_tool_call"
    assert action.action == "restate_schema"
    assert "Required schema" in action.evidence
    assert "harness/core.py" in action.evidence
    assert "missing required argument" in action.evidence


def test_model_unavailable_names_a_bounded_fallback():
    """`model_unavailable` -> a bounded fallback, never a dead run."""
    policy = te.recovery_policy_from_config(
        {"recovery_fallback_model": "cheap/local-tier"}, root=None
    )
    action = policy.on_tool_error(
        te.ToolError("model_unavailable", "provider 503 overloaded")
    )
    assert action.kind == "model_unavailable"
    assert action.action == "bounded_fallback"
    assert "cheap/local-tier" in action.evidence
    assert (
        "next configured provider"
        in te.RecoveryPolicy()
        .on_tool_error(te.ToolError("model_unavailable", "503"))
        .evidence
    )


def test_command_not_found_forbids_the_binary():
    policy = te.RecoveryPolicy()
    action = policy.on_tool_error(
        te.ToolError("command_not_found", "command not found: ripgrepx"),
        command="ripgrepx TODO",
    )
    assert action.action == "avoid_binary"
    assert "ripgrepx" in policy.forbidden


def test_command_rejected_says_repeating_is_a_no_op():
    action = te.RecoveryPolicy().on_tool_error(
        te.ToolError("command_rejected", "denied by harness safety pattern")
    )
    assert action.action == "avoid_shape"
    assert "no-op" in action.evidence


def test_verification_failed_preserves_evidence_and_signals_replan():
    """`verification_failed` -> preserve the evidence and replan."""
    from shared.types import VerificationResult

    verification = VerificationResult(
        target_test_passed=False,
        baseline_passed=True,
        regression_passed=True,
        flaky=False,
        raw_output=_failing_pytest_output(8000),
    )
    policy = te.RecoveryPolicy()
    action = policy.on_verification_failure(verification, attempt=2)
    assert action.kind == "verification_failed"
    assert action.action == "preserve_and_replan"
    assert action.replan is True
    assert "assert 4 == 5" in action.evidence, "the verifier evidence was not preserved"
    assert "omitted" in action.evidence, "the preserved evidence was silently truncated"
    assert action.attempts == 2


def test_verification_evidence_is_bounded_even_for_a_huge_raw_output():
    from shared.types import VerificationResult

    verification = VerificationResult(
        False, True, True, False, _failing_pytest_output(200000)
    )
    action = te.RecoveryPolicy().on_verification_failure(verification)
    assert len(action.evidence) < 4000, "the preserved evidence is not bounded"


def test_every_policy_kind_has_a_distinct_action():
    """A table where two kinds share an action is a table that is not a
    policy; every kind must map to a real, distinct response."""
    actions = {kind: value[0] for kind, value in te.POLICY.items()}
    assert len(set(actions.values())) == len(actions), actions
    for slug in actions.values():
        assert slug, "an empty action slug is not a policy"


# ===========================================================================
# mean turns-to-recovery by error kind
# ===========================================================================


def test_mean_turns_to_recovery_is_measured_per_kind():
    """The measured statistic the prompt asks for, and it is a real
    measurement: turns are counted from the error to the next error-free turn."""
    policy = te.RecoveryPolicy()
    policy.set_turn(0)
    policy.on_tool_error(te.ToolError("timeout", "timed out"), command="x")
    policy.set_turn(1)  # recovered after 1 turn
    policy.on_tool_error(te.ToolError("timeout", "timed out"), command="x")
    policy.set_turn(2)  # recovered after 1 turn
    policy.set_turn(3)
    policy.on_tool_error(te.ToolError("permission_denied", "denied"), command="y")
    policy.set_turn(6)  # permission_denied recovered after 3 turns

    stats = policy.stats()
    assert stats["by_kind"]["timeout"]["occurrences"] == 2
    assert stats["by_kind"]["timeout"]["recoveries"] == 2
    assert stats["by_kind"]["timeout"]["mean_turns_to_recovery"] == 1.0
    assert stats["by_kind"]["permission_denied"]["mean_turns_to_recovery"] == 3.0
    assert stats["mean_turns_to_recovery"] == round((1 + 1 + 3) / 3, 3)
    assert stats["total_occurrences"] == 3


def test_an_unrecovered_error_is_never_reported_as_recovered():
    """An error still pending at report time must not be counted — an
    unrecovered failure reported as a fast recovery is exactly the kind of
    flattering measurement this work must not produce."""
    policy = te.RecoveryPolicy()
    policy.set_turn(0)
    policy.on_tool_error(te.ToolError("timeout", "timed out"), command="x")
    stats = policy.stats()
    assert stats["by_kind"]["timeout"]["recoveries"] == 0
    assert stats["by_kind"]["timeout"]["pending"] is True
    assert stats["by_kind"]["timeout"]["mean_turns_to_recovery"] is None
    assert stats["mean_turns_to_recovery"] is None


def test_the_recovery_receipt_reaches_the_trace_on_both_outcomes(tmp_path):
    """The statistics are observable on the run's own event stream, for a
    successful run and a failed one."""
    from harness.core import _log_recovery_stats

    sink = _Sink()
    policy = te.RecoveryPolicy()
    policy.set_turn(0)
    policy.on_tool_error(te.ToolError("timeout", "timed out"), command="x")
    policy.set_turn(1)
    payload = _log_recovery_stats(sink, policy, te.ModelRecovery(trace=sink))
    assert "recovery_stats" in sink.kinds()
    assert payload["policy"]["by_kind"]["timeout"]["mean_turns_to_recovery"] == 1.0
    assert payload["model"]["attempts"] == 0
    # A broken sink must not raise.
    assert _log_recovery_stats(object(), None, None)["policy"] == {}


def test_policy_stats_record_forbidden_paths_and_the_final_budget(tmp_path):
    root = _make_workdir(tmp_path)
    policy = te.recovery_policy_from_config(
        {"command_timeout_s": 100, "recovery_max_timeout_s": 150}, root=str(root)
    )
    policy.on_tool_error(
        te.ToolError("permission_denied", "permission denied on path locked.py"),
        command="cat locked.py",
    )
    for _ in range(5):
        policy.on_tool_error(te.ToolError("timeout", "t"), command="x")
    stats = policy.stats()
    assert stats["forbidden_paths"] == ["locked.py"]
    assert stats["final_timeout_s"] == 150
    assert stats["actions"] == 6


# ===========================================================================
# config + contract
# ===========================================================================


def test_every_recovery_config_key_has_a_documented_default():
    config = get_config({})
    for key, expected in (
        ("recovery_timeout_backoff", 1.5),
        ("recovery_max_timeout_s", 600),
        ("recovery_max_repeat_command", 2),
        ("recovery_listing_limit", 40),
        ("recovery_numbered_file_lines", 160),
        ("recovery_numbered_file_chars", 6000),
        ("recovery_fallback_model", None),
        ("max_model_attempts", 3),
        ("model_retry_base_s", 0.5),
        ("model_retry_cap_s", 8.0),
        ("abort_kill_deadline_s", 5),
    ):
        assert key in config, f"{key} is missing from the config defaults"
        assert config[key] == expected, key


def test_recovery_policy_is_config_driven_end_to_end():
    config = get_config(
        {
            "command_timeout_s": 10,
            "recovery_max_timeout_s": 20,
            "recovery_timeout_backoff": 2.0,
            "recovery_max_repeat_command": 1,
        }
    )
    policy = te.recovery_policy_from_config(config)
    assert policy.base_timeout_s == 10
    assert policy.max_timeout_s == 20
    action = policy.on_tool_error(te.ToolError("timeout", "t"), command="pytest x")
    assert action.timeout_s == 20
    # max_repeat_command=1: the first call is allowed, the second trips.
    assert policy.note_command("pytest x") is None
    assert policy.note_command("pytest x") is not None


def test_command_paths_extraction_is_defensive():
    assert "a/b.py" in te.command_paths("cat a/b.py")
    assert te.command_paths("") == []
    assert te.command_paths(None) == []
    assert te.command_paths("echo hello") == []


def test_the_verifier_contract_is_untouched_by_recovery():
    """Ceiling invariant: recovery may change the loop's NEXT move, never what
    counts as success."""
    source = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")
    # The success mint is still keyed on the verifier's own three-way
    # evidence, and the recovery machinery is nowhere in that condition.
    mint = source.index("if (\n            final_v.target_test_passed")
    window = source[mint : mint + 400]
    assert "final_v.regression_passed" in window
    assert "not final_v.flaky" in window
    for leak in ("_recovery", "recovery_policy", "model_recovery"):
        assert leak not in window, f"{leak} leaked into the success mint"
    # tool_errors must not import a verifier, and must not name a completion
    # status at all — recovery is downstream of the tool boundary only.
    tool_errors_source = (REPO_ROOT / "harness" / "tool_errors.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "execution.verify",
        "completed_verified",
        "completed_unverified",
        "harness.context",
    ):
        assert forbidden not in tool_errors_source, forbidden
