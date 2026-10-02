"""Runtime-local pins for supervision exemptions and quota/budget (P0/W2 T3).

Two coupled questions this module keeps honest.

**The supervision exemption.** A worker that is waiting on a human, or that is
sleeping through a provider backoff, is legitimately not writing ``state.json``.
The state-stale hang check would otherwise kill it for doing exactly the right
thing. The exemption is ONE mechanism with TWO reasons — ``approval_gate`` and
``provider_backoff`` — and three properties are load-bearing:

1. the WINDOW is derived from the same ``backoff_seconds(attempt, ...)`` the
   retry loop is about to sleep, so a 225 s backoff can no longer meet a 30 s
   kill window;
2. the WAIT and the LICENCE are two different numbers (``seconds`` versus
   ``seconds + grace_s``), and ``grace_s`` IS the watchdog's own window — one
   constant, two readers — so a worker whose backoff just expired still has
   time to land the retried call and write state;
3. an EXPIRED exemption is not an exemption, and neither the heartbeat kill nor
   the wall-clock cap is ever exempted.

**Quota is not a rate limit.** An empty account told to "back off and retry"
cannot be helped by retrying and spends what is left. Quota is classified FIRST,
is terminal, is not retryable, and reports ``provider_fault=False`` so neither a
circuit breaker nor a failover is attempted on the same billing account. And a
budget refusal is a different thing entirely: ``QuotaRefused`` does not exist,
because an empty account and a full budget are two different answers.

Requires no Docker, no provider, no network.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

import pytest

from runtime import budget_governor as bg
from runtime import config as rc
from runtime import provider_resilience as pr


def _governor(**kwargs: Any) -> bg.BudgetGovernor:
    now = [1000.0]
    defaults: Dict[str, Any] = {"cap_usd": 5.0, "clock": lambda: now[0]}
    defaults.update(kwargs)
    return bg.BudgetGovernor(**defaults)


# -- the exemption vocabulary --------------------------------------------


def test_there_are_exactly_two_exemption_reasons_and_both_are_reachable() -> None:
    """One mechanism, two reasons — and neither reason may go missing."""
    assert bg.EXEMPTION_APPROVAL == "approval_gate"
    assert bg.EXEMPTION_BACKOFF == "provider_backoff"
    reasons = {
        value
        for name, value in vars(bg).items()
        if name.startswith("EXEMPTION_") and isinstance(value, str)
    }
    assert reasons == {bg.EXEMPTION_APPROVAL, bg.EXEMPTION_BACKOFF}


def test_the_worker_writes_both_markers_and_clears_them_atomically() -> None:
    """`awaiting_approval` is the historical reader; the marker is the new one.

    The clear must be in the FINAL checkpoint write, atomically with
    ``status="finished"``. A separate finally-clear reopens the race the
    scheduler can observe: marker cleared + status running + stale state.json +
    a not-yet-exited process.
    """
    import runtime.worker as worker

    source = inspect.getsource(worker)
    assert "supervision_exemption" in source
    assert "awaiting_approval=True" in source
    assert 'awaiting_approval": False' in source or "awaiting_approval=False" in source
    clear_at = (
        source.index('"awaiting_approval": False')
        if ('"awaiting_approval": False' in source)
        else source.index("awaiting_approval=False")
    )
    assert "finished" in source[clear_at : clear_at + 400], (
        "the approval marker is cleared away from the status='finished' write, "
        "which reopens the state_stale kill during teardown"
    )


def test_the_scheduler_reads_the_shared_exemption_authority_not_its_own() -> None:
    """Two readers of the marker is how they drift."""
    import runtime.scheduler as scheduler

    source = inspect.getsource(scheduler)
    assert "budget_governor.state_stale_exempt" in source
    assert "hang_backoff_exempt" in source


# -- the window is derived from the same backoff value --------------------


def test_the_exemption_window_is_derived_from_the_same_backoff_value() -> None:
    """Property 1: the wait and the kill window cannot be two inventions."""
    for attempt in (1, 2, 3, 4, 5):
        expected = bg.backoff_seconds(attempt, base_s=15.0)
        window = _governor(backoff_base_s=15.0, backoff_grace_s=30.0).begin_backoff(
            attempt
        )
        assert window.seconds == expected
        assert window.until_epoch - window.started_epoch == expected + window.grace_s


def test_the_wait_and_the_licence_are_two_different_numbers() -> None:
    """Property 2: sleeping the licence turns every backoff into grace + backoff."""
    slept: List[float] = []
    governor = _governor(backoff_base_s=15.0, backoff_grace_s=30.0, sleep=slept.append)
    window = governor.begin_backoff(4)
    assert window.seconds == 120.0
    assert window.grace_s == 30.0
    assert window.until_epoch - window.started_epoch == 150.0
    governor.sleep_in_backoff(window)
    assert slept == [120.0], (
        f"the worker slept {slept} — it slept the LICENCE rather than the WAIT, "
        "so every backoff silently grew by the grace"
    )
    assert window.backoff_remaining_s(1000.0) == 120.0
    assert window.remaining_s(1000.0) == 150.0
    assert window.backoff_remaining_s(1120.0) == 0.0
    assert window.remaining_s(1120.0) == 30.0


def test_the_grace_is_the_watchdogs_own_window_one_constant_two_readers() -> None:
    """Property 2 again, at the wiring: the worker passes the watchdog window."""
    import runtime.worker as worker

    source = inspect.getsource(worker)
    assert "backoff_grace_s" in source
    assert "hang_heartbeat_stale_s" in source or "DEFAULT_HANG_STALE_S" in source
    assert rc.DEFAULT_HANG_STALE_S == 30.0
    assert "DEFAULT_HANG_STALE_S" in inspect.getsource(rc)


# -- an expired exemption is not an exemption -----------------------------


def test_a_live_exemption_suppresses_the_state_stale_kill() -> None:
    """The whole reason the mechanism exists."""
    window = _governor(backoff_grace_s=30.0).begin_backoff(4)
    checkpoint = {"supervision_exemption": window.as_dict(), "status": "running"}
    exempt, reason = bg.state_stale_exempt(checkpoint, now=1000.0)
    assert exempt is True
    assert "provider_backoff" in reason


def test_an_expired_exemption_is_not_an_exemption() -> None:
    """A worker that stops making progress after its wait is killed as before."""
    window = _governor(backoff_grace_s=30.0).begin_backoff(4)
    checkpoint = {"supervision_exemption": window.as_dict(), "status": "running"}
    exempt, reason = bg.state_stale_exempt(checkpoint, now=1600.0)
    assert exempt is False
    assert "expired" in reason


def test_the_approval_exemption_honours_a_missing_deadline_as_running() -> None:
    """The approval park is bounded by the wall-clock cap, not by a marker."""
    checkpoint = {
        "supervision_exemption": {
            "reason": bg.EXEMPTION_APPROVAL,
            "until_epoch": None,
            "started_epoch": 1000.0,
        },
        "status": "running",
    }
    exempt, reason = bg.state_stale_exempt(checkpoint, now=9999.0)
    assert exempt is True
    assert bg.EXEMPTION_APPROVAL in reason


def test_the_finished_status_exemption_belongs_to_the_scheduler_not_the_marker() -> (
    None
):
    """Teardown must not reopen the historical kill.

    `state_stale_exempt` reads ONE thing — the marker. The
    ``status == "finished"`` arm is the scheduler's, because it is a different
    question ("is this process still doing work?"), and folding it in here
    would make the marker reader answer two unrelated things.
    """
    import runtime.scheduler as scheduler

    assert bg.state_stale_exempt({"status": "finished"}) == (False, "")
    source = inspect.getsource(scheduler)
    assert "finished" in source
    assert "state_stale_exempt" in source


def test_no_exemption_at_all_means_no_exemption() -> None:
    """The negative arm: an ordinary running worker is killed as before."""
    exempt, reason = bg.state_stale_exempt({"status": "running"})
    assert exempt is False
    assert reason == ""


def test_the_supervision_exemption_reader_falls_back_to_the_historical_marker() -> None:
    """The historical `awaiting_approval` boolean must still work byte-for-byte."""
    marker = bg.supervision_exemption({"awaiting_approval": True})
    assert marker is not None
    assert marker.get("reason") == bg.EXEMPTION_APPROVAL
    assert bg.supervision_exemption({"awaiting_approval": False}) is None
    assert bg.supervision_exemption(None) is None
    assert bg.supervision_exemption({}) is None


# -- quota is not a rate limit -------------------------------------------


class _Quota(Exception):
    """A constructed provider-shaped failure; no provider is contacted."""


def test_a_quota_error_is_terminal_not_retryable_and_not_a_provider_fault() -> None:
    """Directional: retrying an empty account spends what is left."""
    failure = bg.classify_provider_failure(
        _Quota("Your account has insufficient_quota. Add credit at the billing page.")
    )
    assert failure.kind == bg.QUOTA_EXHAUSTED
    assert failure.retryable is False
    assert failure.terminal is True
    assert failure.provider_fault is False
    assert failure.billing_url


def test_the_status_code_is_not_the_discriminator() -> None:
    """Several providers return quota as a 429; both shapes must differ."""

    class _429(Exception):
        status_code = 429

    quota = bg.classify_provider_failure(
        _Quota(
            "You exceeded your current quota, please check your plan and billing details"
        )
    )
    rate = bg.classify_provider_failure(_429("rate limit exceeded, slow down"))
    assert quota.kind == bg.QUOTA_EXHAUSTED
    assert rate.kind == bg.RATE_LIMITED
    assert rate.retryable is True


def test_the_two_classifiers_agree_on_the_action() -> None:
    """`provider_resilience` and the governor must reach the same decision."""
    error = RuntimeError("Error code: 429 - insufficient_quota for this organization")
    governed = bg.classify_provider_failure(error)
    resilient = pr.classify_retry(error)
    assert governed.kind == resilient.kind == bg.QUOTA_EXHAUSTED
    assert resilient.retryable is False
    assert resilient.provider_fault is False


def test_a_quota_failure_is_recorded_as_a_terminal_outcome_on_the_governor() -> None:
    """A quota block nobody can see is not a quota block."""
    governor = _governor()
    governor.note_quota(
        bg.classify_provider_failure(_Quota("insufficient_quota on this key"))
    )
    failure = governor.quota_failure
    assert failure is not None
    assert failure.kind == bg.QUOTA_EXHAUSTED
    assert governor.report()["quota"]["kind"] == bg.QUOTA_EXHAUSTED
    assert governor.report()["quota"]["billing_url"]


def test_a_quota_block_is_raised_not_retried_by_the_governed_loop() -> None:
    """The pipeline must not spend a second charge discovering the same answer."""
    attempts = {"n": 0}

    def dial() -> str:
        attempts["n"] += 1
        raise _Quota("insufficient_quota")

    with pytest.raises(bg.QuotaExhausted):
        bg.governed_completion(
            dial,
            max_retries=3,
            base_backoff_s=0.0,
            sleep=lambda _s: None,
            started=1000.0,
        )
    assert attempts["n"] == 1


def test_a_rate_limited_dial_does_retry_because_a_retry_can_help() -> None:
    """The control: the same shape must not be treated as terminal."""

    class _429(Exception):
        status_code = 429

    attempts = {"n": 0}

    def dial() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _429("rate limit exceeded")
        return "ok"

    governor = _governor(backoff_base_s=0.0, sleep=lambda _s: None)
    assert (
        bg.governed_completion(
            dial, max_retries=3, governor=governor, sleep=lambda _s: None
        )
        == "ok"
    )
    assert attempts["n"] == 3
    assert governor.report()["backoff"]["count"] >= 1


# -- quota and budget are different things -------------------------------


def test_quota_exhausted_and_budget_refused_are_distinct_types() -> None:
    """An empty account and a full budget are two different answers."""
    assert not issubclass(bg.QuotaExhausted, bg.BudgetRefused)
    assert not issubclass(bg.BudgetRefused, bg.QuotaExhausted)
    assert "quota" in bg.QuotaExhausted.__name__.lower()
    assert "refused" in bg.BudgetRefused.__name__.lower()


def test_a_quota_block_carries_no_budget_verdict_and_a_refusal_carries_no_billing_url() -> (
    None
):
    """Mixing the two would let an operator 'fix' a budget by topping up credit."""
    failure = bg.classify_provider_failure(_Quota("insufficient_quota"))
    quota = bg.QuotaExhausted(failure)
    assert quota.failure.billing_url
    assert quota.failure.kind == bg.QUOTA_EXHAUSTED
    assert not hasattr(quota, "remaining_usd")
    assert failure.billing_url in str(quota), (
        "the exception text must carry the billing pointer, because that text is "
        "one of the five places the terminal outcome is recorded"
    )
    governor = _governor(cap_usd=0.01, reserve_per_call_usd=1.0)
    verdict = governor.authorize_call(price_usd=1.0, price_state="priced")
    assert verdict.allowed is False
    refused = bg.BudgetRefused(verdict)
    assert not hasattr(refused, "billing_url")
    assert not hasattr(refused, "verdict_missing_reason")
    assert "refused by the budget governor" in str(refused)


def test_a_budget_refusal_does_not_trip_the_circuit_breaker() -> None:
    """The governor's answer to "too expensive" is never a provider fault."""
    governor = _governor(cap_usd=0.01, reserve_per_call_usd=1.0)
    governor.authorize_call(price_usd=1.0, price_state="priced")
    report = governor.report()
    assert report["quota"]["state"] == "available"
    assert report["quota"]["kind"] is None
    assert report["backoff"]["count"] == 0


def test_the_terminal_vocabulary_separates_the_two_answers() -> None:
    """`quota_exhausted` is terminal; `rate_limited` is not."""
    assert bg.QUOTA_EXHAUSTED in bg.TERMINAL_KINDS
    assert bg.RATE_LIMITED not in bg.TERMINAL_KINDS
    assert bg.AUTH_FAILED in bg.TERMINAL_KINDS
    assert bg.BAD_REQUEST in bg.TERMINAL_KINDS


def test_the_backoff_schedule_is_capped_and_monotone() -> None:
    """An uncapped exponential wait is a different failure, not a smaller one."""
    seconds = [bg.backoff_seconds(n, base_s=15.0) for n in range(1, 12)]
    assert seconds == sorted(seconds)
    assert max(seconds) <= bg.MAX_BACKOFF_S
    assert bg.backoff_seconds(1, base_s=15.0) == 15.0
    assert bg.backoff_seconds(4, base_s=15.0) == 120.0
    assert bg.backoff_seconds(99, base_s=15.0) == bg.MAX_BACKOFF_S


def test_the_governor_clock_is_the_shared_epoch_clock() -> None:
    """Two clocks means two windows."""
    from runtime import fsutil

    assert bg.now_epoch is fsutil.now_epoch
    assert _governor().report()["clock"] == "runtime.fsutil.now_epoch"
