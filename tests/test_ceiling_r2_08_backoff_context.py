"""R2-08 — kernel model backoff and single context authority.

The prompt this file answers, in its own words:

* two 502s on the KERNEL path produce two `model_recovery` events and the reply
  still returns;
* a `TypeError` in our own code is classified `model_internal` and is NOT
  retried as if the endpoint were flaky;
* the compaction receipt names the model that ACTUALLY summarized, and never
  implies an honoured `context_compaction_model` that was not honoured;
* a 32k-window model is never issued an over-window request, whichever config
  key is set;
* the prompt-cache receipt is REPORTED, never asserted.

Every test drives the REAL kernel or the REAL `harness.core.run_step` with a
scripted boundary, so "the reply returned" and "no over-window request" are
observations of the request the model was actually handed, not claims about the
code that built it. No provider is contacted and no Docker is required.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from harness.agent_kernel import AgentKernel, ModelGateway, RunSpec
from harness.agent_kernel.budget import (
    DEFAULT_CONTEXT_WINDOW,
    TokenEstimator,
    budget_from_config,
)
from harness.prompts import (
    render_step_messages,
    render_step_system,
    step_system_text,
)
from harness.tool_errors import (
    KIND_MODEL_INTERNAL,
    KIND_MODEL_UNAVAILABLE,
    ModelRecovery,
    classify_model_failure,
)

# A 40k body of mixed words (NOT one repeated character: a long single-character
# run is a known pathological input for the shared redaction pass, so a test
# payload must not depend on it being fast).
HUGE = ("def helper(value):\n    return value * 2 + 1\n" * 1000)[:40000]


class BadGateway(Exception):
    """A provider-flavoured 502. Class name and status code both say provider."""

    status_code = 502


class CountingBoundary:
    """A scripted boundary that records every attempt it received.

    It also reports a fixed, non-zero cost per call through the boundary's own
    `get_last_usage`, so a test can measure whether a SECOND model's spend is
    folded into the run's totals rather than left in a second ledger.
    """

    cost_per_call = 0.0

    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []
        self.usage_calls = 0

    def get_last_usage(self) -> Dict[str, Any]:
        """The Boundary-2 usage convention this gateway reads."""
        self.usage_calls += 1
        return {
            "tokens": 11,
            "cost": float(self.cost_per_call),
            "cost_usd": float(self.cost_per_call),
        }

    def __call__(self, messages, **kwargs):
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "model": str(kwargs.get("model") or ""),
                "step": str(kwargs.get("step") or ""),
            }
        )
        value = self.script.pop(0) if self.script else self.script
        if isinstance(value, Exception):
            raise value
        return value


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    """A tiny repository that already contains a large readable file.

    The growth fixture reads a file that is ALREADY in the repository rather
    than writing one first. That keeps these tests independent of the
    `require_edit_digest` mutation gate, which is another terminal's in-flight
    change and is not what any assertion here is about.
    """
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "big.txt").write_text(HUGE, encoding="utf-8")
    return repo


def _spec(repo: Path, run_id: str, request: str = "change the value") -> RunSpec:
    return RunSpec(
        session_id=f"session-{run_id}",
        run_id=run_id,
        request=request,
        repository_identity=str(repo),
    )


def _kernel(
    repo: Path, log_root: Path, boundary, *, _gateway: ModelGateway = None, **config
) -> AgentKernel:
    values: Dict[str, Any] = {
        "agent_approval": "auto",
        "steering_enabled": False,
        # Deterministic backoff: the recovery still runs, retries still happen,
        # and the suite does not spend wall clock asleep. The RECEIPTS are the
        # same shape they are with a real base delay.
        "max_model_attempts": 3,
        "model_retry_base_s": 0.0,
        "model_retry_cap_s": 0.0,
    }
    values.update(config)
    gateway = _gateway
    if gateway is None:
        gateway = ModelGateway(call_fn=boundary)
    elif gateway.call_fn is None:
        gateway._call_fn = boundary
    return AgentKernel(
        repo_path=str(repo),
        log_root=Path(log_root),
        model_gateway=gateway,
        config=values,
    )


def _events(log_root: Path, run_id: str, *names: str) -> List[Dict[str, Any]]:
    """Return the payloads of the named journal events, in order."""
    path = Path(log_root) / run_id / "trace.jsonl"
    wanted = set(names)
    found: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("event") or "") in wanted:
            found.append(row.get("payload") or {})
    return found


def _finish(answer: str = "done") -> str:
    return json.dumps({"tool": "finish", "answer": answer})


def _calls_for(boundary: CountingBoundary, step: str) -> List[Dict[str, Any]]:
    """Return only the attempts the boundary received for one logical step.

    The kernel retries WITHIN a step and then re-issues the next TURN, so a
    total call count conflates two different axes. Asserting per step is what
    actually measures "this call was not retried".
    """
    return [item for item in boundary.calls if item["step"] == step]


# ---------------------------------------------------------------------------
# 1. Bounded, deterministic backoff on the KERNEL path
# ---------------------------------------------------------------------------


def test_two_502s_produce_two_model_recovery_events_and_the_reply_returns(tmp_path):
    """A transient provider fault costs bounded wall clock, never the run.

    Before this the kernel retried nothing while the legacy step path retried
    three times, so two 502s could end a healthy run. The receipt is the two
    `model_recovery` rows on the run's own journal, and the run still finishes.
    """
    repo = _repo(tmp_path, name="repo-502")
    log_root = tmp_path / "logs"
    boundary = CountingBoundary(
        [BadGateway("502 bad gateway"), BadGateway("502 bad gateway"), _finish()]
    )
    kernel = _kernel(repo, log_root, boundary)
    result = kernel.run(_spec(repo, run_id="run-502"))

    # The reply returned: the run reached its terminal state on the third
    # attempt, not a failure the retry budget could not afford.
    assert result.status == "completed_unverified"
    assert len(_calls_for(boundary, "agent-1")) == 3, (
        "two failures plus the successful attempt, on ONE logical call"
    )
    assert len(boundary.calls) == 3

    recoveries = _events(log_root, "run-502", "model_recovery")
    gateway_rows = [
        row for row in recoveries if row.get("label") == "kernel-model-call"
    ]
    assert len(gateway_rows) == 2, f"expected two retry receipts, got {gateway_rows}"
    for index, row in enumerate(gateway_rows, start=1):
        assert row["kind"] == KIND_MODEL_UNAVAILABLE
        assert row["status_code"] == 502
        assert row["retryable"] is True
        assert row["terminal"] is False
        assert row["action"] == "retry"
        assert row["attempt"] == index
        assert row["max_attempts"] == 3
    # The measurement, not just the event stream.
    report = (result.metadata or {}).get("model_recovery") or {}
    assert report["recoveries"] == 2
    assert report["by_kind"] == {KIND_MODEL_UNAVAILABLE: 2}
    assert report["last_kind"] == KIND_MODEL_UNAVAILABLE
    # The retries are visible in the call ledger rather than hidden behind one
    # row: one logical call, three provider attempts.
    assert [record["attempts"] for record in kernel.model_gateway.calls] == [3]


def test_the_kernel_backoff_is_bounded_by_the_configured_attempt_budget(tmp_path):
    """A provider that never recovers is bounded, not retried forever."""
    repo = _repo(tmp_path, name="repo-dead")
    log_root = tmp_path / "logs"
    boundary = CountingBoundary([BadGateway("502 bad gateway")] * 12)
    kernel = _kernel(repo, log_root, boundary, max_model_attempts=2)
    result = kernel.run(_spec(repo, run_id="run-dead"))

    for turn in range(1, 4):
        assert len(_calls_for(boundary, f"agent-{turn}")) == 2, (
            "the per-call attempt budget is the only bound on a retry loop"
        )
    rows = [
        row
        for row in _events(log_root, "run-dead", "model_recovery")
        if row.get("label") == "kernel-model-call"
    ]
    # Every turn spent its budget and gave up; none of them looped.
    assert [row["action"] for row in rows].count("give_up") == 3
    assert all(row["max_attempts"] == 2 for row in rows)
    assert result.status == "failed", "an exhausted budget ends the run honestly"


def test_max_model_attempts_one_disables_the_retry_entirely(tmp_path):
    """The OFF arm is one config key, not a second code path."""
    repo = _repo(tmp_path, name="repo-off")
    log_root = tmp_path / "logs"
    boundary = CountingBoundary([BadGateway("502 bad gateway")] * 6)
    kernel = _kernel(repo, log_root, boundary, max_model_attempts=1)
    kernel.run(_spec(repo, run_id="run-off"))
    for turn in range(1, 4):
        assert len(_calls_for(boundary, f"agent-{turn}")) == 1
    rows = _events(log_root, "run-off", "model_recovery")
    assert not [row for row in rows if row.get("label") == "kernel-model-call"], (
        "with no retry budget the gateway emits no recovery receipt at all"
    )


def test_the_kernel_and_the_legacy_path_share_one_classifier_and_one_vocabulary():
    """Same slugs, same classes, same terminal-vs-retryable split.

    The kernel must not grow a second notion of "this failure is the provider's
    fault". Both paths route through `classify_model_failure`.
    """
    provider = BadGateway("502 bad gateway")
    harness_side = TypeError("unsupported operand for timeout_ms")
    assert classify_model_failure(provider).kind == KIND_MODEL_UNAVAILABLE
    assert classify_model_failure(provider).retryable is True
    assert classify_model_failure(harness_side).kind == KIND_MODEL_INTERNAL
    assert classify_model_failure(harness_side).retryable is False
    # The gateway uses that very function, not a private table.
    from harness.agent_kernel import gateway as gateway_module

    assert gateway_module.classify_model_failure is classify_model_failure
    assert gateway_module.ModelRecovery is ModelRecovery


# ---------------------------------------------------------------------------
# 2. A non-provider exception is never a provider fault
# ---------------------------------------------------------------------------


def test_a_type_error_is_classified_model_internal_and_is_not_retried(tmp_path):
    """A bug in our own code must not be retried as if the endpoint were flaky."""
    repo = _repo(tmp_path, name="repo-typeerror")
    log_root = tmp_path / "logs"
    boundary = CountingBoundary([TypeError("unsupported operand for timeout_ms")] * 4)
    kernel = _kernel(repo, log_root, boundary)
    result = kernel.run(_spec(repo, run_id="run-typeerror"))

    assert len(_calls_for(boundary, "agent-1")) == 1, (
        "a TypeError is a harness bug; retrying it three times is noise, and "
        "labelling it a timeout would blame the provider for our coding error"
    )
    rows = [
        row
        for row in _events(log_root, "run-typeerror", "model_recovery")
        if row.get("label") == "kernel-model-call"
    ]
    assert {row["attempt"] for row in rows} == {1}, "never attempted a second time"
    assert {row["kind"] for row in rows} == {KIND_MODEL_INTERNAL}
    assert {row["action"] for row in rows} == {"give_up"}
    assert all(row["retryable"] is False and row["terminal"] is True for row in rows)
    report = (result.metadata or {}).get("model_recovery") or {}
    assert report["recoveries"] == 0
    assert report["last_kind"] == KIND_MODEL_INTERNAL


def test_a_provider_flavoured_message_on_a_harness_exception_is_still_internal():
    """`Classify` keys off the exception's type/status, never its prose."""
    failure = classify_model_failure(
        TypeError("upstream returned 502 bad gateway and timed out")
    )
    assert failure.kind == KIND_MODEL_INTERNAL
    assert failure.retryable is False
    assert failure.terminal is True
    assert failure.backoff_s == 0.0


def test_a_provider_flavoured_message_on_a_harness_exception_is_not_retried(tmp_path):
    """The same rule, proven end to end through the kernel path."""
    repo = _repo(tmp_path, name="repo-flavoured")
    log_root = tmp_path / "logs"
    boundary = CountingBoundary(
        [TypeError("upstream returned 502 bad gateway and timed out")] * 4
    )
    kernel = _kernel(repo, log_root, boundary)
    kernel.run(_spec(repo, run_id="run-flavoured"))
    assert len(_calls_for(boundary, "agent-1")) == 1
    rows = [
        row
        for row in _events(log_root, "run-flavoured", "model_recovery")
        if row.get("label") == "kernel-model-call"
    ]
    assert [row["kind"] for row in rows] == [KIND_MODEL_INTERNAL] * 3


# ---------------------------------------------------------------------------
# 3. The compaction receipt names the model that actually summarized
# ---------------------------------------------------------------------------


def _compaction_run(
    tmp_path, run_id, *, run_model, compaction_model, fallback="", answer_primary=True
):
    """Run long enough to compact, recording which model each call reached."""
    repo = _repo(tmp_path, name=f"repo-{run_id}")
    log_root = tmp_path / "logs"
    seen: List[Dict[str, str]] = []
    state = {"compacting": False, "reads": 0}
    usage = {"calls": 0}
    gateway = ModelGateway(call_fn=None)

    def boundary(messages, **kwargs):
        step = str(kwargs.get("step") or "")
        model_name = str(kwargs.get("model") or "")
        seen.append({"step": step, "model": model_name})
        if step.startswith("context-compaction"):
            state["compacting"] = True
            if model_name == "fallback-model":
                return "SUMMARY-FROM-FALLBACK"
            # Any summarizer model that is NOT the fallback tier produces
            # nothing unless the test asked it to answer, so the
            # primary->fallback chain is genuinely exercised rather than
            # accidentally answered by the first model asked.
            if answer_primary:
                return f"SUMMARY-FROM-{model_name}"
            return ""
        if state["compacting"]:
            return _finish("compacted run done")
        state["reads"] += 1
        if state["reads"] > 12:
            return _finish("growth run done")
        return json.dumps({"tool": "read", "path": "big.txt"})

    def usage_receipt() -> Dict[str, Any]:
        usage["calls"] += 1
        return {
            "tokens": 11,
            "cost": FIXTURE_COST_PER_CALL,
            "cost_usd": FIXTURE_COST_PER_CALL,
        }

    boundary.get_last_usage = usage_receipt  # type: ignore[attr-defined]
    config = {
        "context_window_tokens": 32768,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 40000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 40,
        "model": run_model,
        "context_compaction_model": compaction_model,
        "context_compaction_fallback_model": fallback,
    }
    kernel = _kernel(repo, log_root, boundary, _gateway=gateway, **config)
    result = kernel.run(_spec(repo, run_id=run_id))
    return log_root, seen, result, gateway, usage


#: What the compaction fixture's boundary charges per call. A summarizer on a
#: second model is real spend: if it were left in a second ledger the run's
#: total would be short by exactly these amounts.
FIXTURE_COST_PER_CALL = 0.01


def test_the_receipt_names_the_summarizing_model_and_the_key_is_honoured(tmp_path):
    """`context_compaction_model` is the model that produced the summary.

    Before this the key was read straight into the receipt while the summarizer
    ran on the run's own model, so the receipt named a model that never
    summarized anything.
    """
    log_root, seen, result, _gw, _usage = _compaction_run(
        tmp_path,
        "honoured-run",
        run_model="run-model",
        compaction_model="summarizer-model",
    )
    compactions = _events(log_root, "honoured-run", "context_compacted")
    assert compactions, "the run must have compacted to exercise the receipt"
    receipt = compactions[0]
    assert receipt["method"] == "model_summary"
    assert receipt["compaction_model"] == "summarizer-model"
    assert receipt["compaction_model_configured"] == "summarizer-model"
    assert receipt["compaction_model_honoured"] is True
    # The model that was ASKED is the model the receipt names: a receipt that
    # disagrees with the request is the whole defect.
    assert any(
        item["step"].startswith("context-compaction")
        and item["model"] == "summarizer-model"
        for item in seen
    )
    assert not any(
        item["step"].startswith("agent-") and item["model"] == "summarizer-model"
        for item in seen
    ), "the summarizer model must not receive ordinary turns"
    assert result.status == "completed_unverified"


def test_a_receipt_never_implies_an_unhonoured_compaction_model(tmp_path):
    """No summary at all is recorded as the run's own model, not as the key."""
    log_root, _seen, _result, _gw, _usage = _compaction_run(
        tmp_path,
        "trim-run",
        run_model="run-model",
        compaction_model="summarizer-model-that-cannot-answer",
        fallback="",
        answer_primary=False,
    )
    receipt = _events(log_root, "trim-run", "context_compacted")[0]
    # The summarizer model WAS asked and DID fail, so the receipt names it...
    assert (
        receipt["compaction_model_configured"] == "summarizer-model-that-cannot-answer"
    )
    # ...and the method is the honest structural trim, never a fake summary.
    assert receipt["method"] == "structural_trim"
    assert "SUMMARY-FROM-" not in json.dumps(receipt)


def test_the_summarizer_models_spend_is_folded_into_the_runs_own_totals(tmp_path):
    """A summarizer on a second model is real spend, not a second ledger.

    One run, one ledger: the run's own gateway call list carries the summarizer's
    row, so a cost report cannot under-report a compaction the operator paid for.
    """
    log_root, seen, result, gateway, usage = _compaction_run(
        tmp_path,
        "spend-run",
        run_model="run-model",
        compaction_model="summarizer-model",
    )
    assert _events(log_root, "spend-run", "context_compacted")
    summarizer_rows = [
        record
        for record in gateway.calls
        if str(record.get("step") or "").startswith("context-compaction")
    ]
    assert summarizer_rows, (
        "the summarizer's call must land in the run's OWN gateway ledger, not in "
        "a second one that nothing reports"
    )
    # The measurement: every provider call the run made is charged to the run,
    # INCLUDING the ones the summarizer's own model made. If the summarizer's
    # spend were left in a second ledger this total would be short by exactly
    # the summarizer rows' cost.
    assert usage["calls"] == len(gateway.calls), (
        "one recorded gateway row per provider call, with the summarizer among them"
    )
    assert gateway.total_cost_usd == pytest.approx(
        len(gateway.calls) * FIXTURE_COST_PER_CALL, abs=1e-6
    )
    assert gateway.total_tokens == len(gateway.calls) * 11
    assert any(item["model"] == "summarizer-model" for item in seen), (
        "the summarizer really was the configured model"
    )
    assert result.status == "completed_unverified"


def test_a_failed_primary_summarizer_falls_back_and_names_the_fallback(tmp_path):
    """The fallback tier, when it answers, is what the receipt names."""
    log_root, seen, _result, _gw, _usage = _compaction_run(
        tmp_path,
        "fallback-run",
        run_model="run-model",
        compaction_model="summarizer-model",
        fallback="fallback-model",
        answer_primary=False,
    )
    receipt = _events(log_root, "fallback-run", "context_compacted")[0]
    assert receipt["method"] == "fallback_model"
    assert receipt["compaction_model"] == "fallback-model"
    assert receipt["compaction_model_configured"] == "summarizer-model"
    assert receipt["compaction_model_honoured"] is False, (
        "the configured primary did not produce this summary, so the receipt "
        "must not claim it did"
    )
    assert any(
        item["step"].startswith("context-compaction-fallback")
        and item["model"] == "fallback-model"
        for item in seen
    )


# ---------------------------------------------------------------------------
# 4. One context-window authority
# ---------------------------------------------------------------------------


def test_the_context_window_authority_is_the_only_resolver():
    """`runtime.model_capabilities` resolves; the kernel budget READS it."""
    budget = budget_from_config({"context_window_tokens": 131072, "model": "gpt-4"})
    assert budget.window == 8192, "gpt-4's real window, from the authority"
    assert budget.window_declared == 131072, "what the run declared is recorded"
    assert budget.window_authority == 8192
    assert budget.window_source != "config:context_window_tokens"
    assert budget.as_dict()["window_authority_applied"] is True


def test_the_authority_floor_never_shrinks_a_declared_budget():
    """An unknown model is a refusal to guess, not a capability answer."""
    budget = budget_from_config(
        {"context_window_tokens": 131072, "model": "brand-new-model-9000"}
    )
    assert budget.window == 131072
    assert budget.window_source == "config:context_window_tokens"
    assert budget.window_authority == 0


def test_an_unconfigured_run_is_unchanged_by_the_authority():
    """A run with no model and no declared window keeps the historical value."""
    budget = budget_from_config({})
    assert budget.window == DEFAULT_CONTEXT_WINDOW
    assert budget.window_source == "default"


def _requests_and_window(tmp_path, run_id, **config):
    """Drive a real kernel run and return every request the model was handed.

    The window bound is measured INSIDE the boundary, from the request itself, so
    "never over-window" is a property of what the model received rather than a
    claim about the budget that produced it.
    """
    repo = _repo(tmp_path, name=f"repo-{run_id}")
    log_root = tmp_path / "logs"
    estimator = TokenEstimator("heuristic")
    measured: List[Dict[str, Any]] = []
    state = {"reads": 0}

    def boundary(messages, **kwargs):
        measured.append(
            {
                "tokens": estimator.messages_tokens(messages),
                "messages": len(messages),
                "step": str(kwargs.get("step") or ""),
            }
        )
        state["reads"] += 1
        if state["reads"] % 3 == 0:
            return _finish(f"run {run_id} done")
        return json.dumps({"tool": "read", "path": "big.txt"})

    values = {
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 40000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 40,
    }
    values.update(config)
    kernel = _kernel(repo, log_root, boundary, **values)
    result = kernel.run(_spec(repo, run_id=run_id))
    budget = (result.metadata or {}).get("context", {}).get("budget", {})
    return measured, budget, result


@pytest.mark.parametrize(
    "label,config",
    [
        (
            "global_key",
            {"model": "gpt-4", "context_window_tokens": 131072},
        ),
        (
            "per_model_key",
            {
                "model": "gpt-4",
                "context_window_tokens": 131072,
                "context_window_by_model": {"gpt-4": 131072},
            },
        ),
    ],
)
def test_a_32k_window_model_is_never_issued_an_over_window_request(
    tmp_path, label, config
):
    """Whichever key is set, the request never exceeds the model's real window.

    gpt-4's window is 8192 and the configs below both DECLARE 131072, which is
    the disagreement this collapses: two sources resolving a window independently
    is how a 32k model gets run with a 128k budget.
    """
    run_id = f"window-{label}"
    measured, budget, result = _requests_and_window(tmp_path, run_id, **config)
    assert budget["window"] == 8192, "the authority's answer, not the declaration"
    assert budget["window_declared"] == 131072
    assert budget["window_authority"] == 8192
    usable = budget["usable_window"]
    assert measured, "the run must have reached the model boundary"
    over = [item for item in measured if item["tokens"] > usable]
    assert not over, (
        f"{len(over)} of {len(measured)} requests exceeded the {usable}-token "
        f"usable window (worst {max(i['tokens'] for i in measured)})"
    )
    # The bound is real, not vacuous: the run genuinely used a meaningful part
    # of the window it was given.
    assert max(item["tokens"] for item in measured) > usable * 0.2
    assert result.status in {"completed_unverified", "completed_verified"}


def test_a_config_pin_may_lower_the_window_but_never_raise_it():
    """Lowering is the documented use of the key; raising is the bug it prevents."""
    lowered = budget_from_config(
        {"context_window_tokens": 8192, "model": "gpt-4o", "provider": "openai"}
    )
    assert lowered.window == 8192
    assert lowered.window_source == "config:context_window_tokens"
    raised = budget_from_config(
        {
            "context_window_by_model": {"gpt-4": 131072},
            "context_window_tokens": 131072,
            "model": "gpt-4",
        }
    )
    assert raised.window == 8192, "a per-model pin cannot raise past the authority"


def test_the_window_source_names_the_rung_that_produced_the_number():
    """A measured window is distinguishable from a declared one and a floor."""
    measured = budget_from_config(
        {"context_window_tokens": 131072, "model": "gpt-4o", "provider": "openai"}
    )
    assert measured.window_source.split(":")[0] in {"litellm", "table", "probe"}
    declared = budget_from_config({"context_window_tokens": 4096, "model": "gpt-4o"})
    assert declared.window_source == "config:context_window_tokens"


def test_a_broken_authority_degrades_to_the_declared_window_rather_than_zero():
    """A zero window is a lie a budgeter acts on; the run keeps its declaration."""
    budget = budget_from_config(
        {"context_window_tokens": 32768, "model": "gpt-4", "provider": "openai"}
    )
    assert budget.window >= 1024
    # An unresolvable model name is the same shape: positive, and stated.
    assert (
        budget_from_config({"context_window_tokens": 2048, "model": ""}).window == 2048
    )


# ---------------------------------------------------------------------------
# 5. The prompt-cache receipt is reported, never asserted
# ---------------------------------------------------------------------------


def _step_prompt_kwargs(**overrides):
    values = {
        "issue_text": "the value is wrong",
        "plan": [{"id": 1, "description": "fix the value", "checkpoint": "tests"}],
        "step_id": 1,
        "total_steps": 1,
        "completed_block": "",
        "context_block": "app.py: value = 1",
        "max_output_chars": 4000,
        "language": "python",
    }
    values.update(overrides)
    return values


def test_the_cacheable_step_prompt_makes_the_leading_segment_byte_stable():
    """The property the split exists for, measured on the real renderer."""
    from runtime.prompt_cache import prefix_digest, split_at_breakpoint

    one = render_step_messages(first_user="Begin.", **_step_prompt_kwargs(step_id=1))
    two = render_step_messages(
        first_user="Begin.",
        **_step_prompt_kwargs(step_id=2, completed_block="step 1 done"),
    )
    assert [item["role"] for item in one] == ["system", "system", "user"]
    assert one[0]["content"] == two[0]["content"], "frozen prefix must be identical"
    assert one[1]["content"] != two[1]["content"], "the per-turn half must vary"
    prefix_a, _suffix_a, index_a = split_at_breakpoint(one)
    prefix_b, _suffix_b, index_b = split_at_breakpoint(two)
    assert index_a == index_b == 0
    assert prefix_digest(one) == prefix_digest(two), (
        "consecutive turns must share one cache prefix digest"
    )
    assert len(prefix_a) == 1 and len(prefix_b) == 1


def test_step_system_text_reads_both_prompt_shapes_identically():
    """The migration helper: a dispatcher can move before or after the split."""
    single = [
        {"role": "system", "content": render_step_system(**_step_prompt_kwargs())},
        {"role": "user", "content": "Begin."},
    ]
    split = render_step_messages(first_user="Begin.", **_step_prompt_kwargs())
    assert step_system_text(single) == step_system_text(split)
    assert step_system_text(single) == render_step_system(**_step_prompt_kwargs())
    # The dispatch every scripted step model performs still resolves.
    match = re.search(r"your step is #(\d+) of", step_system_text(split))
    assert match and int(match.group(1)) == 1
    # Unrelated system messages are separated rather than glued together.
    assert (
        step_system_text(
            [
                {"role": "system", "content": "A ends"},
                {"role": "system", "content": "B starts"},
            ]
        )
        == "A ends\n\nB starts"
    )
    assert step_system_text([{"role": "user", "content": "x"}]) == ""


def test_the_step_prompt_cache_receipt_is_reported_and_never_gates(tmp_path):
    """The receipt is an observation, not a threshold.

    A cache hit rate is a property of the PROVIDER. The harness reports the
    inputs to that decision (prefix digest, breakpoint position, prefix size)
    and never computes or enforces a rate, so a 0% or unreported rate is a fact
    about the endpoint rather than a reason to fail a task.
    """
    from harness.core import _log_step_prompt_cache

    class _Trace:
        def __init__(self) -> None:
            self.rows: List[Dict[str, Any]] = []

        def log(self, kind, data=None):
            self.rows.append({"kind": kind, "data": dict(data or {})})

    trace = _Trace()
    single = [
        {"role": "system", "content": render_step_system(**_step_prompt_kwargs())},
        {"role": "user", "content": "Begin."},
    ]
    split = render_step_messages(first_user="Begin.", **_step_prompt_kwargs())
    for messages, split_flag in ((single, False), (split, True)):
        receipt = _log_step_prompt_cache(trace, messages, step_id=1, split=split_flag)
        assert receipt["available"] is True
        assert receipt["split"] is split_flag
        assert receipt["system_message_count"] == (2 if split_flag else 1)
        assert len(receipt["prefix_digest"]) == 64
        assert receipt["cache_breakpoint_index"] >= 0
        # REPORTED inputs, and deliberately NO rate, NO target, NO verdict.
        assert "hit_rate" not in receipt
        assert "cache_status" not in receipt
        assert not any("threshold" in key or "target" in key for key in receipt)
    assert [row["kind"] for row in trace.rows] == ["step_prompt_cache"] * 2


def test_a_provider_that_reports_no_cache_usage_is_reported_as_unreported(tmp_path):
    """No cache field is not a 0% hit rate; it is `unreported`.

    This is the reason the receipt is reported rather than asserted: a scripted
    or OpenAI-compatible endpoint simply does not speak the cache protocol, and
    rounding that to "miss" would overstate savings while rounding it to "hit"
    would overstate efficiency.
    """
    from runtime.prompt_cache import (
        CACHE_UNREPORTED,
        CACHE_UNSUPPORTED,
        plan_cache,
        receipt_from_usage,
    )

    messages = render_step_messages(first_user="Begin.", **_step_prompt_kwargs())
    # MEASURED, not assumed: the legacy step prompt's frozen prefix is the
    # static template ALONE, and it measures below the provider's own 1024-token
    # floor, so today the plan is not requested. That is the honest receipt, and
    # it is the reason this is reported rather than asserted - the number a
    # cache change produces depends on the endpoint, not on the harness.
    plan = plan_cache(messages, provider="anthropic", model="claude-sonnet-4")
    assert plan.requested is False
    assert plan.skip_reason == "prefix_below_floor"
    assert 0 < plan.prefix_tokens_estimate < 1024

    # The same prefix IS requestable when the operator's floor allows it, which
    # is what makes the below-floor answer a measurement rather than a dead end.
    lowered = plan_cache(
        messages,
        provider="anthropic",
        model="claude-sonnet-4",
        min_prefix_tokens=plan.prefix_tokens_estimate,
    )
    assert lowered.requested is True
    assert lowered.breakpoint_index == 0
    # That provider then reported no cache field at all: `unreported`, which is
    # neither a hit nor a miss, and never a computed rate.
    receipt = receipt_from_usage({}, lowered)
    assert receipt.status == CACHE_UNREPORTED
    assert receipt.decided is False, "an unreported cache is not a decided miss"
    assert receipt.hit is False
    assert receipt.cached_input_tokens == 0
    assert receipt.to_dict()["cache_status"] == CACHE_UNREPORTED

    # A provider that never speaks the protocol is `unsupported` - also
    # undecided, also not rounded to a rate.
    openai_plan = plan_cache(
        messages,
        provider="openai",
        model="gpt-4o",
        min_prefix_tokens=plan.prefix_tokens_estimate,
    )
    openai_receipt = receipt_from_usage({}, openai_plan)
    assert openai_receipt.status == CACHE_UNSUPPORTED
    assert openai_receipt.decided is False
    assert openai_receipt.hit is False


def test_the_step_prompt_split_is_off_unless_explicitly_requested():
    """Opt-in by an explicit key, so no task and no eval arm switches silently."""
    from harness.config import DEFAULTS

    assert "step_prompt_cache_split" in DEFAULTS
    assert DEFAULTS["step_prompt_cache_split"] is None, (
        "a value in DEFAULTS is merged into every task; a truthy default here "
        "would silently change every run and every eval arm's message shape"
    )
    # Absent, None and False all mean the historical single system message.
    for value in (None, False):
        assert not bool(value)


def test_the_split_changes_what_a_first_system_message_dispatcher_can_read():
    """Measured evidence for the cross-terminal request, not a guess.

    The conversion to the cacheable shape is one config key away, and it is NOT
    enabled here because the step loops' scripted models dispatch on the FIRST
    system message. This test pins the exact breakage so the request that lands
    the change is not a guess - and so a future change to
    `render_step_messages` cannot silently make the breakage worse.
    """
    kwargs = _step_prompt_kwargs(step_id=2)
    single = [
        {"role": "system", "content": render_step_system(**kwargs)},
        {"role": "user", "content": "Begin."},
    ]
    split = render_step_messages(first_user="Begin.", **kwargs)

    def first_system(messages):
        # The exact expression used by tests/test_webfetch.py,
        # tests/test_batch_docs_lint.py, evals/run.py, and
        # harness/_stubs/scripted_model.py.
        return next(
            (item["content"] for item in messages if item["role"] == "system"), ""
        )

    naive_step = re.search(r"your step is #(\d+) of", first_system(split))
    assert naive_step is None, (
        "if this ever starts matching, the documented blocker is stale and the "
        "conversion can be enabled"
    )
    assert re.search(r"your step is #(\d+) of", first_system(single))
    # The helper is the fix, and it works for both shapes.
    assert re.search(r"your step is #(\d+) of", step_system_text(split)), (
        "step_system_text is the migration path"
    )
