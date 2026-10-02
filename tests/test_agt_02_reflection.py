"""AGT-02 — the bounded reflection loop (`harness.agent_loop` + `tool_errors`).

A failure that does not become the NEXT turn's input is a failure the model
has to rediscover. This suite pins the loop that makes it the input, under a
cap, with honest retry classes:

  1. three failures, then a fourth is refused (per-step cap);
  2. a policy refusal does NOT consume the budget — and a transient provider
     fault DOES (the same budget, the two opposite answers);
  3. the cap is REPORTED, both caps, both counters, and which one bound the
     decision;
  4. every reflection is journalled with its class and its evidence, and the
     evidence is the failure VERBATIM;
  5. a reflection cannot launder a policy refusal into compliance;
  6. the retry class is DERIVED from the one existing classification — there
     is no second error table;
  7. the verifier gate is untouched: an exhausted run is `failed`, never
     `success`, and an unverified completion is still `completed_unverified`.

Every test drives the REAL loop with a scripted model through the documented
`harness.deps` seam (no Docker, no network, no provider).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import deps  # noqa: E402
from harness import tool_errors as te  # noqa: E402

# R2-04: an unqualified `run_agent` dispatches to the kernel resolver's own
# `daily` default. This suite unit-tests the LEGACY engine's loop body — the
# `{"tool": ...}` reply protocol, the approval gate and the live BASH session
# that the reflection loop is wired into — so it asks for the compatibility
# engine BY NAME. The unqualified default is pinned separately in
# tests/test_ceiling_r2_04_daily_default.py.
LEGACY = {"agent_strategy": "legacy_agent", "steering_enabled": False}

#: A reply `parse_tool_call` cannot read. The model said something, it was not
#: a tool call, and the failure is that reply — verbatim — in the next turn.
GARBAGE = "Let me think about this out loud instead of calling a tool."


class Scripted:
    """Queue-driven fake model that RECORDS the messages it was handed.

    The queue is INFINITE: the last scripted reply repeats. A reflection loop
    under test is allowed to keep asking, and a test must not be shaped by
    pytest's message cache telling the loop to stop.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list = []

    def __call__(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            return ""
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Provider502(Exception):
    """A provider-shaped transient failure (class name + status say so)."""

    status_code = 502


class AlwaysFails:
    """A model that never answers: every call is a provider fault."""

    def __init__(self, exc=None):
        self.exc = exc or Provider502("upstream returned 502 bad gateway")
        self.calls = 0

    def __call__(self, messages, **kw):
        self.calls += 1
        raise self.exc


def _failing_command(marker: str) -> str:
    """A command that genuinely runs, genuinely fails, and says why.

    The real live-BASH path (the repository's local subprocess boundary, not
    a double) is used on purpose: a test that injects a fake sandbox measures
    the test's own fiction rather than the loop.
    """
    return f"python -c \"raise SystemExit('boom: {marker}')\""


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "router.py").write_text("def route(k):\n    return k\n", "utf-8")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_router.py").write_text(
        "def test_route():\n    pass\n", "utf-8"
    )
    return root


def _run(
    request,
    repo,
    replies,
    *,
    config=None,
    sandbox=None,
    approve_fn=None,
    task_id="agt-02",
    log_root=None,
):
    from harness.agent_loop import run_agent

    model = Scripted(replies) if isinstance(replies, (list, tuple)) else replies
    deps.set_call_model(model)
    if sandbox is not None:
        deps.set_execute_sandboxed(sandbox)
    try:
        return run_agent(
            request=request,
            repo_path=str(repo),
            config={**LEGACY, **(config or {})},
            log_root=log_root or (repo.parent / "logs"),
            task_id=task_id,
            approve_fn=approve_fn,
        ), model
    finally:
        deps.reset_overrides()


def _events(out, kind):
    rows = []
    with open(str(out["trace_path"]), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == kind:
                rows.append(row.get("data") or {})
    return rows


def _reflections(out):
    return _events(out, "reflection")


def _stats(out):
    rows = _events(out, "recovery_stats")
    assert rows, "no recovery_stats receipt was emitted"
    return rows[-1]


@pytest.fixture
def repo(tmp_path):
    return _repo(tmp_path)


# ---------------------------------------------------------------------------
# 1. The cap: three failures, then a fourth is refused
# ---------------------------------------------------------------------------


def test_three_failures_then_a_fourth_is_refused(repo):
    """The loop's default cap is 3, and the FOURTH failure is refused.

    Every failure is the same unparseable reply, so the per-step cap is what
    binds — and the run ends with a reason that NAMES the cap and the last
    failure, rather than retrying silently or stopping without one.
    """
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE] * 6,
        config={"agent_max_turns": 8},
    )

    rows = _reflections(out)
    assert len(rows) == 4, f"expected 4 journalled failures, got {len(rows)}"
    assert [r["charged"] for r in rows] == [True, True, True, False]
    assert [r["allowed"] for r in rows] == [True, True, True, False]
    assert rows[-1]["exhausted"] == "per_step"
    assert "per-step reflection cap reached (3/3)" in rows[-1]["reason"]

    assert out["status"] == "failed"
    assert out["status"] != "success"
    reason = out["end_reason"]
    assert "reflection budget exhausted" in reason
    assert "per-step cap 3" in reason
    assert "malformed_tool_call" in reason, reason


def test_the_per_step_cap_is_configurable(repo):
    """A tighter cap refuses sooner; the default is 3 (the documented one)."""
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE] * 6,
        config={"agent_max_turns": 8, "reflection_max_per_step": 1},
    )
    rows = _reflections(out)
    assert len(rows) == 2, rows
    assert rows[-1]["exhausted"] == "per_step"
    assert rows[-1]["step_cap"] == 1


def test_the_per_run_cap_binds_across_separate_failure_streaks(repo):
    """The per-RUN cap is a real second bound, not a restatement of the first.

    Every failure here is separated by a SUCCESSFUL turn, so the per-step
    (consecutive-failure) counter never rises above 1 and the per-step cap
    cannot bind. The per-RUN cap is what refuses the fourth — and the receipt
    names which cap it was.
    """
    script: list = []
    for n in range(5):
        script.append(
            json.dumps(
                {
                    "tool": "edit",
                    "path": "src/router.py",
                    "old_string": f"# marker {n}",
                    "new_string": "# replaced",
                }
            )
        )
        script.append('{"tool": "read", "path": "src/util.py"}')
    script.append('{"tool": "done", "answer": "gave up"}')
    out, _ = _run(
        "tidy the source",
        repo,
        script,
        config={
            "agent_max_turns": 16,
            "reflection_max_per_step": 3,
            "reflection_max_per_run": 3,
        },
    )
    rows = _reflections(out)
    assert len(rows) == 4, rows
    assert [r["exhausted"] for r in rows] == ["", "", "", "per_run"]
    assert [r["charged"] for r in rows] == [True, True, True, False]
    assert [r["step_used"] for r in rows[:3]] == [1, 1, 1], (
        "the per-step counter must reset on every productive turn, so the "
        "consecutive-failure cap can never be what binds here"
    )
    assert rows[3]["step_used"] == 0, "a refused reflection spends nothing"
    assert out["status"] == "failed"
    assert "per_step" not in out["end_reason"]


# ---------------------------------------------------------------------------
# 2. Retry classes: a refusal is free, a transient fault is not
# ---------------------------------------------------------------------------


def test_a_policy_refusal_does_not_consume_the_budget(repo):
    """A refusal is reported and the loop continues — at zero budget cost.

    The run below spends a refusal AND three malformed replies. The refusal is
    journalled as a policy refusal with `charged=False`, the three malformed
    replies each get a full reflection, and only the FOURTH malformed reply
    hits the cap. If a refusal had cost budget, one of those three would have
    been refused.
    """
    out, _ = _run(
        "edit the source",
        repo,
        [
            '{"tool": "edit", "path": "src/router.py", "old_string": "    return k",'
            ' "new_string": "    return 42"}',
            GARBAGE,
            GARBAGE,
            GARBAGE,
            GARBAGE,
        ],
        config={"agent_max_turns": 8, "agent_approval": "require"},
    )
    rows = _reflections(out)
    kinds = [(r["kind"], r["retry_class"], r["charged"]) for r in rows]
    assert kinds[0] == ("command_rejected", "policy_refusal", False), kinds
    assert [k[2] for k in kinds[1:4]] == [True, True, True], kinds
    assert kinds[4][2] is False and rows[4]["exhausted"] == "per_step", kinds
    assert _stats(out)["reflection"]["run_used"] == 3, (
        "a policy refusal must not have spent a reflection"
    )
    # The refused edit never happened.
    assert "return 42" not in (repo / "src" / "router.py").read_text(encoding="utf-8")


def test_a_transient_provider_fault_does_consume_the_budget(repo):
    """A transient provider fault IS worth another attempt, and is charged."""
    out, model = _run(
        "do the thing",
        repo,
        AlwaysFails(),
        config={"agent_max_turns": 8, "max_model_attempts": 1},
    )
    rows = _reflections(out)
    assert len(rows) == 4, rows
    assert {r["retry_class"] for r in rows} == {"transient_provider"}
    assert [r["charged"] for r in rows] == [True, True, True, False]
    assert rows[-1]["exhausted"] == "per_step"
    assert _stats(out)["reflection"]["run_used"] == 3
    # Each attempt is still one model call, and the provider-retry receipt
    # still records every decision.
    assert model.calls == 4
    assert len(_events(out, "model_recovery")) == 4
    assert out["status"] == "error"
    assert "reflection budget exhausted" in out["end_reason"]


def test_a_transient_fault_recovers_the_run_within_the_budget(repo):
    """One transient blip, then a normal run: the reflection is the fix.

    This is the behaviour the cap exists to protect — a provider hiccup used to
    end a healthy run, and now becomes the next turn's input.
    """

    class Flaky:
        def __init__(self):
            self.calls = 0
            self.seen: list = []

        def __call__(self, messages, **kw):
            self.calls += 1
            self.seen.append([dict(m) for m in messages])
            if self.calls == 1:
                raise Provider502("upstream returned 502 bad gateway")
            return '{"tool": "done", "answer": "recovered"}'

    out, model = _run(
        "do the thing",
        repo,
        Flaky(),
        config={"agent_max_turns": 8, "max_model_attempts": 1},
    )
    assert model.calls == 2, model.calls
    assert len(_reflections(out)) == 1
    assert out["status"] in ("completed_unverified", "success")
    assert out["status"] != "success" or bool(out.get("verification"))
    # The failure reached the model verbatim.
    last = model.seen[-1][-1]["content"]
    assert "REFLECTION 1" in last
    assert "502 bad gateway" in last


def test_a_terminal_model_failure_is_not_reflected_on(repo):
    """Auth/bad-request/internal are not worth another attempt.

    A terminal failure must not spend reflection budget and must not be
    re-asked of the model — the run reports why it stopped, as it always did.
    """

    class AuthError(Exception):
        status_code = 401

    out, model = _run(
        "do the thing",
        repo,
        AlwaysFails(AuthError("invalid api key")),
        config={"agent_max_turns": 8},
    )
    assert model.calls == 1, "a terminal failure must not be re-asked"
    assert _reflections(out) == []
    assert out["status"] == "error"
    assert "model_auth" in out["end_reason"]
    assert _stats(out)["reflection"]["run_used"] == 0


# ---------------------------------------------------------------------------
# 3. The cap is reported
# ---------------------------------------------------------------------------


def test_the_cap_is_reported_in_the_receipt_and_the_result(repo):
    """Both caps, both counters, and the failure that stopped the loop."""
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE] * 6,
        config={
            "agent_max_turns": 8,
            "reflection_max_per_step": 2,
            "reflection_max_per_run": 7,
        },
    )
    report = _stats(out)["reflection"]
    assert report["per_step"] == 2
    assert report["per_run"] == 7
    assert report["run_used"] == 2
    assert report["step_used"] == 2
    assert report["exhausted"] == "per_step"
    assert report["step_cap_reached"] is True
    assert report["run_cap_reached"] is False
    assert report["by_class"]["model_error"] == 3
    assert report["last"]["kind"] == "malformed_tool_call"
    # The same receipt rides the public result, so a caller does not have to
    # read the journal to learn the cap.
    assert out["reflection"]["per_step"] == 2
    assert out["reflection"]["run_used"] == 2
    assert out["reflection"]["last"]["kind"] == "malformed_tool_call"


def test_the_reflection_caps_have_documented_defaults():
    from harness.config import DEFAULTS

    assert DEFAULTS["reflection_max_per_step"] == 3
    assert DEFAULTS["reflection_max_per_run"] == 12
    assert DEFAULTS["reflection_evidence_max_chars"] == 4000
    budget = te.reflection_budget_from_config(DEFAULTS)
    assert (budget.per_step, budget.per_run) == (3, 12)
    # An unusable value degrades to the documented default, never a crash.
    broken = te.reflection_budget_from_config(
        {"reflection_max_per_step": "three", "reflection_max_per_run": None}
    )
    assert (broken.per_step, broken.per_run) == (3, 12)


# ---------------------------------------------------------------------------
# 4. Every reflection is journalled, with its class and its evidence
# ---------------------------------------------------------------------------


def test_every_reflection_is_journalled_with_its_class_and_evidence(repo):
    """A run's struggle is reconstructable from the trace ALONE."""
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE] * 6,
        config={"agent_max_turns": 8},
    )
    rows = _reflections(out)
    assert rows, "no reflection reached the journal"
    for index, row in enumerate(rows, 1):
        assert row["index"] == index
        assert row["kind"] == "malformed_tool_call"
        assert row["retry_class"] in te.RETRY_CLASSES
        assert row["retryable"] is True
        assert row["step_id"]
        assert row["evidence"], "a reflection with no evidence is not a reflection"
        assert GARBAGE in row["evidence"], (
            "the failure must ride the reflection verbatim"
        )
        assert row["evidence_chars"] == len(row["evidence"])
        assert row["step_cap"] == 3 and row["run_cap"] == 12
        assert row["reason"]


def test_a_tool_failure_reaches_the_model_verbatim_with_its_evidence(repo):
    """A failing BASH command is the next user message, output and all.

    This is the class the cap has to stay honest about: a command that RAN and
    reported a non-zero status is a RESULT, not a failed call. The output must
    ride the next turn intact — with the classified evidence attached — and a
    run of healthy failing tests must not be reflected on at all.
    """
    commands = [
        json.dumps({"tool": "bash", "command": _failing_command("first")}),
        json.dumps({"tool": "bash", "command": _failing_command("second")}),
        '{"tool": "done", "answer": "stopping here"}',
    ]
    out, model = _run(
        "run the tests",
        repo,
        commands,
        config={"agent_max_turns": 8},
    )
    last = model.seen[-1][-1]["content"]
    assert "## Tool result (bash, ok)" in last
    assert "boom: second" in last, "the command's own output must ride verbatim"
    assert "exit=1" in last, "the historical exit shape is preserved"
    assert "REFLECTION" not in last, (
        "a command that ran and reported a failure is not a failed CALL; "
        "reflecting on it would spend the budget on a working test run"
    )
    assert _reflections(out) == []
    assert out["reflection"]["run_used"] == 0


def test_a_denied_command_is_reflected_on_with_its_classification(repo):
    """A command the DENY-GUARD refuses never ran, so it IS a failed call."""
    out, model = _run(
        "clean up the repository",
        repo,
        [
            json.dumps({"tool": "bash", "command": "rm -rf /"}),
            '{"tool": "done", "answer": "refused, as it should be"}',
        ],
        config={"agent_max_turns": 6},
    )
    rows = _reflections(out)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "command_rejected"
    assert rows[0]["retry_class"] == "policy_refusal"
    assert rows[0]["charged"] is False, "a refusal must not spend a reflection"
    last = model.seen[-1][-1]["content"]
    assert "REFUSED" in last
    assert "safety pattern" in last


# ---------------------------------------------------------------------------
# 5. A reflection cannot launder a policy refusal into compliance
# ---------------------------------------------------------------------------


def test_a_refusal_never_reads_as_an_invitation_to_try_again(repo):
    """The refused shape is a refusal, and the message says the decision stands."""
    out, model = _run(
        "edit the tests",
        repo,
        [
            '{"tool": "edit", "path": "tests/test_router.py", '
            '"old_string": "    pass", "new_string": "    assert True"}',
            '{"tool": "edit", "path": "tests/test_router.py", '
            '"old_string": "    pass", "new_string": "    assert 1"}',
            '{"tool": "done", "answer": "could not edit the protected test"}',
        ],
        config={"agent_max_turns": 6},
    )
    refusals = [r for r in _reflections(out) if r["retry_class"] == "policy_refusal"]
    assert len(refusals) == 2, _reflections(out)
    assert all(r["charged"] is False for r in refusals)
    assert all(r["retryable"] is False for r in refusals)
    assert _stats(out)["reflection"]["run_used"] == 0, (
        "two refused calls must not have spent a single reflection"
    )

    # The refused mutation never happened, twice.
    assert "assert" not in (repo / "tests" / "test_router.py").read_text(
        encoding="utf-8"
    )

    # And the message the model read cannot be read as "try again".
    seen = "\n".join(m["content"] for m in model.seen[-1])
    assert "not retryable" in seen
    assert "REFUSED" in seen
    low = seen.lower()
    for invitation in ("try again", "retry the", "try the same call again"):
        assert invitation not in low, (
            f"a refusal offered {invitation!r}; that is the laundering this class exists to prevent"
        )


def test_a_refused_call_is_still_refused_after_the_reflection(repo):
    """The reflection is a report, not a permission.

    A model that reads the refusal and immediately re-issues the same refused
    call gets refused again — the decision is not a one-turn speed bump.
    """
    payload = '{"tool": "write", "path": "tests/test_router.py", "content": "def test_route(): pass"}'
    out, _ = _run(
        "overwrite the test",
        repo,
        [payload, payload, '{"tool": "done", "answer": "stopped"}'],
        config={"agent_max_turns": 6},
    )
    refusals = [r for r in _reflections(out) if r["retry_class"] == "policy_refusal"]
    assert len(refusals) == 2, _reflections(out)
    assert (repo / "tests" / "test_router.py").read_text(encoding="utf-8") == (
        "def test_route():\n    pass\n"
    )
    assert out["reflection"]["run_used"] == 0


# ---------------------------------------------------------------------------
# 6. One classification: the retry class is derived, never a second table
# ---------------------------------------------------------------------------


def test_every_existing_kind_maps_to_exactly_one_retry_class():
    """Every kind the module already defines gets a class, and only one.

    This is the anti-second-table gate: the retry class is a projection of the
    EXISTING `POLICY` rows and the EXISTING model-failure kinds, so a new
    failure kind cannot be classified by a parallel vocabulary that drifts.
    """
    existing = set(te.POLICY) | {
        te.KIND_MODEL_RATE_LIMITED,
        te.KIND_MODEL_UNAVAILABLE,
        te.KIND_MODEL_TIMEOUT,
        te.KIND_MODEL_AUTH,
        te.KIND_MODEL_BAD_REQUEST,
        te.KIND_MODEL_INTERNAL,
    }
    for kind in sorted(existing):
        cls = te.retry_class_of(kind)
        assert cls in te.RETRY_CLASSES, (kind, cls)
        # Stable: a second call is the same answer.
        assert te.retry_class_of(kind) == cls

    # A refusal is read off the ONE table's own action, and the provider
    # classes are read off the provider classifier's own sets (which is why
    # `model_unavailable` is transient even though it also has a POLICY row:
    # a provider blip is worth another attempt).
    refusal_actions = {"avoid_shape", "forbid_path"}
    transient = {
        te.KIND_MODEL_RATE_LIMITED,
        te.KIND_MODEL_UNAVAILABLE,
        te.KIND_MODEL_TIMEOUT,
    }
    terminal = {te.KIND_MODEL_AUTH, te.KIND_MODEL_BAD_REQUEST, te.KIND_MODEL_INTERNAL}
    for kind in sorted(te.POLICY):
        action = te.POLICY[kind][0]
        expected = (
            te.RETRY_CLASS_POLICY_REFUSAL
            if action in refusal_actions
            else te.RETRY_CLASS_TASK_ERROR
        )
        if kind in transient:
            expected = te.RETRY_CLASS_TRANSIENT
        elif kind in terminal:
            expected = te.RETRY_CLASS_TERMINAL
        elif kind == "malformed_tool_call":
            expected = te.RETRY_CLASS_MODEL_ERROR
        elif kind == "loop_detected" or te.is_environment_kind(kind):
            expected = te.RETRY_CLASS_TERMINAL
        assert te.retry_class_of(kind) == expected, kind

    # An UNKNOWN kind is reflective, not a refusal: guessing a refusal would
    # silently disable recovery for a failure nobody has classified yet.
    assert te.retry_class_of("some_kind_nobody_wrote_yet") == te.RETRY_CLASS_TASK_ERROR
    assert te.retry_class_of("") == te.RETRY_CLASS_TASK_ERROR
    assert te.retry_class_of(None) == te.RETRY_CLASS_TASK_ERROR


def test_the_two_classes_agree_with_the_model_classifier():
    """`classify_model_failure` and `retry_class_of` cannot disagree."""
    for exc, expected in (
        (Provider502("upstream returned 502 bad gateway"), te.RETRY_CLASS_TRANSIENT),
    ):
        failure = te.classify_model_failure(exc)
        assert te.retry_class_of_model_failure(failure) == expected

    class RateLimit(Exception):
        status_code = 429

    class Auth(Exception):
        status_code = 401

    class BadRequest(Exception):
        status_code = 400

    for exc, expected in (
        (RateLimit("429 too many requests"), te.RETRY_CLASS_TRANSIENT),
        (Auth("invalid api key"), te.RETRY_CLASS_TERMINAL),
        (BadRequest("invalid_request_error"), te.RETRY_CLASS_TERMINAL),
    ):
        failure = te.classify_model_failure(exc)
        assert te.retry_class_of_model_failure(failure) == expected, (exc, failure)


def test_the_budget_counts_and_reports_without_a_trace_sink():
    """The budget is usable (and honest) with no observability attached.

    The two refusals here prove the ordering the honest answer depends on: a
    non-retryable failure is answered BEFORE either cap is consulted, so it is
    free even when the budget is already spent. A cap must never be able to
    reclassify a refusal as a budget problem.
    """
    budget = te.ReflectionBudget(per_step=2, per_run=2)
    for _ in range(3):
        budget.note("timeout", evidence="late", signature="pytest -q", step_id="s1")
    refusals = [
        budget.note(
            "command_rejected", evidence="no", signature="rm -rf /", step_id="s1"
        )
        for _ in range(5)
    ]
    report = budget.report()
    assert report["run_used"] == 2
    assert report["charged"] == 2
    assert report["free"] == 6
    assert report["step_cap_reached"] is True
    assert report["run_cap_reached"] is True
    assert report["exhausted"] in {"per_step", "per_run"}
    assert all(r.charged is False and r.retryable is False for r in refusals)
    assert all(r.allowed is False for r in refusals)
    assert all(r.exhausted == "" for r in refusals), (
        "a refusal is never a cap problem; the cap must not be what answers it"
    )
    assert all("not retryable" in r.reason for r in refusals)


def test_evidence_is_bounded_and_states_what_it_dropped():
    """A long failure is head+tail shaped, never silently truncated.

    The marker sits at the END of the evidence, which is where a failure's
    answer lives: a head-only cap would drop the very line the model needs.
    """
    budget = te.ReflectionBudget(per_step=3, max_evidence_chars=400)
    marker = "THE-ACTUAL-FAILURE"
    reflection = budget.note(
        "internal_error",
        evidence=("x" * 4000) + marker,
        signature="cmd",
    )
    text = reflection.evidence
    assert len(text) < 800
    assert marker in text
    assert "chars omitted" in text


def test_render_reflection_never_raises_on_a_hostile_record():
    assert te.render_reflection(None) == ""
    assert te.render_reflection(object()) == ""


# ---------------------------------------------------------------------------
# 7. The verifier gate is untouched
# ---------------------------------------------------------------------------


def test_an_exhausted_run_is_never_a_success(repo):
    """The reflection cap ends a run; it cannot complete one.

    A reflection is prompt shaping, and prompt shaping is not verification.
    """
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE] * 6,
        config={
            "agent_max_turns": 8,
            "target_test": "tests/test_router.py::test_route",
        },
    )
    assert out["status"] == "failed"
    assert out["status"] != "success"
    assert "success" not in json.dumps(out.get("reflection", {})).lower()


def test_a_reflection_loop_run_still_reports_unverified_completion(repo):
    """A recovered run with no verifier evidence is `completed_unverified`."""
    out, _ = _run(
        "do the thing",
        repo,
        [GARBAGE, '{"tool": "done", "answer": "all good"}'],
        config={"agent_max_turns": 8},
    )
    assert out["status"] == "completed_unverified"
    assert "verification" not in out
    assert len(_reflections(out)) == 1


def test_the_reflection_budget_never_appears_in_a_completion_decision():
    """Source-level pin: the cap decides when to STOP, never what counts as done.

    The same discipline VEX-CEILING-07 pinned for the recovery policy: the
    reflection machinery may change what the loop does NEXT, and must be absent
    from the DONE branch, which is where an outcome is decided. Pinned on the
    DONE branch itself rather than a character window, so an unrelated receipt
    written before the tail cannot be mistaken for the gate.
    """
    from pathlib import Path as _Path

    source = (_Path(ROOT) / "harness" / "agent_loop.py").read_text(encoding="utf-8")
    body = _function_body(source, "def _run_agent_legacy(")
    done_branch = body.split('if tool == "done":', 1)[-1].split(
        "if _needs_approval(", 1
    )[0]

    # The DONE branch still routes through the verifier when tests are declared.
    assert 'if cfg.get("target_test") or cfg.get("test_command")' in done_branch
    assert 'status = "success" if ok else "failed"' in done_branch
    assert "completed_unverified" not in body, (
        "the legacy loop must not mint the unverified word itself; that is the "
        "adapter's job (harness.agent_loop._compat_status)"
    )
    # And the reflection budget is nowhere in it.
    for leak in ("_reflections", "_reflect(", "_exhaust", "reflection"):
        assert leak not in done_branch, (
            f"the reflection budget reached the completion decision via {leak!r}"
        )


def _function_body(source: str, header: str) -> str:
    """The text of ONE top-level function, so a later docstring cannot be
    mistaken for its code."""
    start = source.index(header)
    nxt = source.find("\ndef ", start + len(header))
    return source[start:] if nxt < 0 else source[start:nxt]
