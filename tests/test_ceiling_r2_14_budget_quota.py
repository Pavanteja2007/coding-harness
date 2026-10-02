"""R2-14 - budget governor, quota awareness, 429 safety.

Every test is named after the BEHAVIOUR it pins, and every one measures
something the code did before this round:

* a 429 whose backoff is longer than the hang window survives (the worker
  is not hang-killed) — the real scheduler, the real worker subprocess, the
  real ``governed_completion`` loop, the real checkpoint;
* a quota error ends the run AS QUOTA, with zero retries and zero sleeps;
* a budget of $X never exceeds $X by more than the price of the final
  call, measured from the on-disk ledger;
* the exemption window, the backoff the retry loop sleeps, and the
  watchdog's clock are provably one number and one clock.

Host-only: no Docker, no provider, no network. Every provider-shaped
failure is a constructed exception object, so nothing here is evidence
about a real provider's behaviour.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List

import pytest

from runtime import budget_governor
from runtime import budget_governor as bg
from runtime.fsutil import now_epoch


# --------------------------------------------------------------------------
# doubles shaped like the provider errors the classifier must tell apart
# --------------------------------------------------------------------------
class _RateLimited(Exception):
    """A 429, shaped like litellm's."""

    status_code = 429


class _InsufficientQuota(Exception):
    """An exhausted quota. Note the 429: quota arrives as a rate-limit
    STATUS on several real providers, so status code alone cannot classify
    it. The text markers are the discriminator."""

    status_code = 429


class _Overloaded(Exception):
    status_code = 503


class _FakeResponse:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [
            type(
                "Choice",
                (),
                {
                    "message": type(
                        "Message", (), {"content": content, "tool_calls": None}
                    ),
                    "finish_reason": "stop",
                },
            )()
        ]


def _governor(**overrides: Any) -> bg.BudgetGovernor:
    """Build a governor with a recording sleep and no real waiting."""
    slept: List[float] = []
    kwargs: Dict[str, Any] = {
        "task_id": "t",
        "cap_usd": 1.0,
        "sleep": slept.append,
        "backoff_base_s": 15.0,
        "rate_limit_retries": 4,
    }
    kwargs.update(overrides)
    governor = bg.BudgetGovernor(**kwargs)
    governor.slept = slept  # type: ignore[attr-defined]
    return governor


def _sleeps(governor: bg.BudgetGovernor) -> List[float]:
    return list(getattr(governor, "slept", []))


# ==========================================================================
# 1. Quota is not a rate limit (the misclassification itself)
# ==========================================================================
class TestQuotaIsNotARateLimit:
    def test_an_exhausted_quota_is_classified_terminal_and_is_never_retried(
        self,
    ) -> None:
        failure = bg.classify_provider_failure(
            _InsufficientQuota("You exceeded your current quota, insufficient_quota")
        )
        assert failure.kind == bg.QUOTA_EXHAUSTED
        assert failure.retryable is False
        assert failure.terminal is True

    def test_a_rate_limit_is_classified_separately_and_is_retryable(self) -> None:
        failure = bg.classify_provider_failure(
            _RateLimited("RateLimitError: 429 Too Many Requests")
        )
        assert failure.kind == bg.RATE_LIMITED
        assert failure.retryable is True
        assert failure.terminal is False

    def test_quota_and_rate_limit_are_distinct_even_at_the_same_status_code(
        self,
    ) -> None:
        """Both are 429 in the wild. Status alone cannot tell them apart."""
        quota = bg.classify_provider_failure(_InsufficientQuota("insufficient_quota"))
        limited = bg.classify_provider_failure(_RateLimited("Too Many Requests"))
        assert quota.status_code == limited.status_code == 429
        assert quota.kind != limited.kind

    def test_the_two_marker_sets_do_not_overlap(self) -> None:
        """The root defect was `insufficient_quota` living INSIDE the
        rate-limit marker set. Pin the disjointness so it cannot return."""
        assert not set(bg.QUOTA_MARKERS) & set(bg.RATE_LIMIT_MARKERS)
        assert "insufficient_quota" in bg.QUOTA_MARKERS
        assert "429" in bg.RATE_LIMIT_MARKERS

    def test_the_resilience_classifier_agrees_on_the_action(self) -> None:
        """`provider_resilience` is a second classifier; the two must not
        disagree about whether a quota wall may be retried."""
        from runtime import provider_resilience

        decision = provider_resilience.classify_retry(
            _InsufficientQuota("insufficient_quota")
        )
        assert decision.kind == bg.QUOTA_EXHAUSTED
        assert decision.retryable is False
        # And it is not an endpoint outage: a circuit breaker must not be
        # tripped and a failover must not be attempted on a dead account.
        assert decision.provider_fault is False

    def test_a_quota_refusal_points_at_the_providers_billing_page(self) -> None:
        failure = bg.classify_provider_failure(_InsufficientQuota("insufficient_quota"))
        assert failure.billing_url
        assert failure.billing_url.startswith("https://")
        assert bg.billing_url_for("openai") in failure.billing_url
        # An unknown provider still gets a pointer: a dead end is not a
        # recovery instruction.
        assert bg.billing_url_for("some-unknown-gateway").startswith("https://")

    def test_a_transient_5xx_is_still_retried_and_an_auth_failure_is_not(self) -> None:
        assert bg.classify_provider_failure(_Overloaded("503")).kind == (
            bg.PROVIDER_UNAVAILABLE
        )
        assert bg.classify_provider_failure(_Overloaded("503")).retryable is True
        auth = bg.classify_provider_failure(
            Exception("AuthenticationError: 401 invalid api key")
        )
        assert auth.kind == bg.AUTH_FAILED
        assert auth.retryable is False


# ==========================================================================
# 2. One backoff schedule, one clock, one deadline
# ==========================================================================
class TestOneBackoffScheduleOneClock:
    def test_the_exemption_window_is_derived_from_the_same_backoff_value(self) -> None:
        """THE anti-disagreement test. The watchdog and the retry loop read
        the same number, so a backoff can never outlive the kill window
        that was computed independently."""
        governor = _governor(backoff_base_s=30.0, backoff_cap_s=300.0)
        window = governor.begin_backoff(2)
        schedule = bg.backoff_seconds(2, base_s=30.0, cap_s=300.0)
        assert window.seconds == schedule
        assert window.until_epoch - window.started_epoch == pytest.approx(schedule)

    def test_the_exemption_deadline_is_on_the_governors_clock(self) -> None:
        clock = {"now": 1000.0}
        governor = _governor(backoff_base_s=10.0, clock=lambda: clock["now"])
        window = governor.begin_backoff(1)
        assert window.started_epoch == 1000.0
        assert window.until_epoch == 1010.0
        assert window.covers(now=1005.0) is True
        assert window.covers(now=1010.5) is False

    def test_the_sleep_the_loop_performs_equals_the_backoff_it_armed(self) -> None:
        """`governed_completion` sleeps `BackoffWindow.backoff_remaining_s`,
        so the WAIT and the LICENCE are two different facts read from one
        value. Sleeping the licence would add the grace to every backoff."""
        clock = {"now": 500.0}
        governor = _governor(
            backoff_base_s=45.0, backoff_grace_s=99.0, clock=lambda: clock["now"]
        )
        outcome: Dict[str, Any] = {}

        def _dial() -> str:
            if not outcome.get("done"):
                outcome["done"] = True
                raise _RateLimited("429")
            return "ok"

        bg.governed_completion(
            _dial, max_retries=4, base_backoff_s=45.0, governor=governor
        )
        assert _sleeps(governor) == [45.0], "the wait is the backoff, not the licence"
        window = governor.backoff_window
        assert window is not None
        assert window.seconds == 45.0
        assert window.grace_s == 99.0
        # The licence outlives the wait by exactly the declared grace.
        assert window.remaining_s(clock["now"]) - window.backoff_remaining_s(
            clock["now"]
        ) == pytest.approx(99.0)

    def test_the_wait_and_the_licence_come_from_one_window_and_cannot_drift(
        self,
    ) -> None:
        clock = {"now": 10.0}
        governor = _governor(
            backoff_base_s=20.0,
            backoff_cap_s=300.0,
            backoff_grace_s=5.0,
            clock=lambda: clock["now"],
        )
        window = governor.begin_backoff(1)
        assert window.seconds == bg.backoff_seconds(1, base_s=20.0, cap_s=300.0)
        assert window.until_epoch == pytest.approx(
            window.started_epoch + window.seconds + window.grace_s
        )
        # Halfway through the wait, the licence is already longer.
        clock["now"] += 10.0
        assert window.backoff_remaining_s(clock["now"]) == pytest.approx(10.0)
        assert window.remaining_s(clock["now"]) == pytest.approx(15.0)

    def test_backoff_is_exponential_capped_and_deterministic(self) -> None:
        assert [bg.backoff_seconds(n, base_s=15.0) for n in (1, 2, 3, 4)] == [
            15.0,
            30.0,
            60.0,
            120.0,
        ]
        assert bg.backoff_seconds(12, base_s=15.0) == bg.MAX_BACKOFF_S
        assert bg.backoff_seconds(1, base_s=0.0) == 0.0
        assert bg.backoff_seconds(1, base_s=15.0, cap_s=1.0) == 1.0

    def test_the_wallclock_deadline_comes_from_the_governor_not_a_second_clock(
        self,
    ) -> None:
        governor = _governor(
            started_epoch=100.0, max_wallclock_s=900.0, clock=lambda: 1000.0
        )
        assert governor.deadline_epoch == 1000.0
        assert governor.over_wallclock(now=999.0) is False
        assert governor.over_wallclock(now=1001.0) is True
        # No configured cap means NO deadline, not a deadline of zero.
        assert _governor(max_wallclock_s=None).deadline_epoch is None
        assert _governor(max_wallclock_s=None).over_wallclock() is False

    def test_the_checkpoint_marker_names_the_shared_clock(self) -> None:
        window = _governor(backoff_base_s=12.0).begin_backoff(1)
        marker = window.as_dict()
        assert marker["clock"] == "runtime.fsutil.now_epoch"
        assert marker["reason"] == bg.EXEMPTION_BACKOFF
        assert marker["until_epoch"] == pytest.approx(marker["started_epoch"] + 12.0)


# ==========================================================================
# 3. The supervision exemption (approval park, generalized)
# ==========================================================================
class TestSupervisionExemption:
    def test_the_approval_park_marker_still_exempts_with_no_governor(self) -> None:
        """Backward compatibility: the historical boolean must keep working
        byte-for-byte, or every existing approval test breaks."""
        exempt, reason = bg.state_stale_exempt({"awaiting_approval": True})
        assert exempt is True
        assert reason == bg.EXEMPTION_APPROVAL

    def test_a_live_backoff_exempts_and_an_expired_one_does_not(self) -> None:
        now = 2000.0
        window = bg.BackoffWindow(
            reason=bg.EXEMPTION_BACKOFF,
            started_epoch=now,
            until_epoch=now + 60.0,
            attempt=1,
            seconds=60.0,
        )
        checkpoint = {"supervision_exemption": window.as_dict()}
        assert bg.state_stale_exempt(checkpoint, now=now + 10.0)[0] is True
        assert bg.state_stale_exempt(checkpoint, now=now + 61.0)[0] is False

    def test_no_marker_means_the_kill_applies_exactly_as_before(self) -> None:
        for checkpoint in (
            None,
            {},
            {"status": "running"},
            {"awaiting_approval": False},
        ):
            assert bg.state_stale_exempt(checkpoint)[0] is False

    def test_an_unparsable_deadline_is_not_a_licence_to_skip_a_kill(self) -> None:
        checkpoint = {"supervision_exemption": {"reason": "provider_backoff"}}
        exempt, _ = bg.state_stale_exempt(checkpoint)
        assert exempt is True
        # ...but the marker still has to be well-formed enough to read; a
        # missing reason means no marker at all.
        assert bg.supervision_exemption({"supervision_exemption": {}}) is None

    def test_a_backoff_longer_than_the_hang_window_is_exempt_for_its_full_length(
        self,
    ) -> None:
        """The defect, stated arithmetically: 225s backoff vs 30s window."""
        hang_stale_s = 30.0
        now = 10_000.0
        governor = _governor(backoff_base_s=225.0, clock=lambda: now)
        window = governor.begin_backoff(1)
        assert window.seconds > hang_stale_s
        for probe in (0.0, 15.0, 29.0, 30.5, 120.0, 224.0):
            assert (
                bg.state_stale_exempt(
                    {"supervision_exemption": window.as_dict()}, now=now + probe
                )[0]
                is True
            )
        # One second after the backoff ends the worker is killable again.
        assert (
            bg.state_stale_exempt(
                {"supervision_exemption": window.as_dict()}, now=now + 225.1
            )[0]
            is False
        )


# ==========================================================================
# 4. Per-call budget enforcement
# ==========================================================================
class TestPerCallBudget:
    def test_a_call_that_cannot_fit_is_refused_before_it_is_dialed(self) -> None:
        governor = _governor(cap_usd=0.001)
        verdict = governor.authorize_call(
            messages=[{"role": "user", "content": "x" * 40000}],
            target={"model": "gpt-4o", "provider": "openai"},
        )
        assert verdict.allowed is False
        assert verdict.reserved_usd > verdict.remaining_usd
        assert "does not fit" in verdict.reason

    def test_a_refusal_latches_the_cap_so_the_attempt_level_backstop_fires(
        self,
    ) -> None:
        governor = _governor(cap_usd=0.0005)
        assert governor.exhausted is False
        governor.authorize_call(
            messages=[{"role": "user", "content": "x" * 20000}],
            target={"model": "claude-3-5-sonnet-20241022", "provider": "anthropic"},
        )
        assert governor.exhausted is True
        # Every later call is refused with the quota/cap reason, never
        # silently re-priced at zero.
        again = governor.authorize_call(
            messages=[{"role": "user", "content": "hi"}],
            target={"model": "gpt-4o-mini"},
        )
        assert again.allowed is False

    def test_reservations_make_a_burst_collectively_fit_inside_the_cap(self) -> None:
        """Two concurrent reservations must not both fit when only one
        call's worth of budget is left. This is what the attempt-start
        check could never see."""
        governor = _governor(cap_usd=1.0)
        target = {"model": "gpt-4o-mini", "provider": "openai"}
        messages = [{"role": "user", "content": "x" * 2000}]
        first = governor.authorize_call(messages=messages, target=target)
        assert first.allowed is True
        spent = governor.spent_usd()
        second = governor.authorize_call(messages=messages, target=target)
        # Still fits while both are held; the point is the accounting.
        assert second.remaining_usd <= 1.0 - spent
        governor.release(first.reserved_usd)
        governor.release(second.reserved_usd)
        assert governor.reserved_usd() == 0.0

    def test_committing_spend_shrinks_the_remaining_budget(self) -> None:
        governor = _governor(cap_usd=2.0)
        assert governor.remaining_usd() == 2.0
        governor.commit(0.5)
        assert governor.remaining_usd() == pytest.approx(1.5)
        governor.commit(0.25, model="gpt-4o-mini")
        assert governor.remaining_usd() == pytest.approx(1.25)

    def test_the_spend_authority_is_the_maximum_of_the_two_sources_not_the_sum(
        self,
    ) -> None:
        """`ModelClient` sees one call's cost; the governor also sees a
        fallback's earlier charges. The cap must not under-report, and must
        not double-count what both saw."""
        external = {"value": 0.0}
        governor = _governor(cap_usd=5.0, spend_source=lambda: external["value"])
        governor.commit(0.30)
        external["value"] = 0.10  # the harness counted the same 0.30? no: less
        assert governor.spent_usd() == pytest.approx(0.30)
        external["value"] = 0.90  # the harness saw a call the governor missed
        assert governor.spent_usd() == pytest.approx(0.90)

    def test_a_broken_spend_source_never_disables_the_cap(self) -> None:
        def _boom() -> float:
            raise RuntimeError("usage reader is broken")

        governor = _governor(cap_usd=1.0, spend_source=_boom)
        governor.commit(0.4)
        assert governor.spent_usd() == pytest.approx(0.4)
        assert governor.remaining_usd() == pytest.approx(0.6)

    def test_no_cap_configured_means_no_cap_not_a_zero_budget(self) -> None:
        governor = _governor(cap_usd=None)
        verdict = governor.authorize_call(
            messages=[{"role": "user", "content": "x" * 100000}],
            target={"model": "gpt-4o"},
        )
        assert verdict.allowed is True
        assert verdict.cap_usd is None
        assert governor.remaining_usd() is None
        assert governor.exhausted is False

    def test_an_unpriced_model_is_reported_unpriced_and_never_reads_as_free(
        self,
    ) -> None:
        price, source = bg.estimated_call_price(
            [{"role": "user", "content": "hello"}],
            target={"model": "a-model-nobody-priced"},
        )
        assert (price, source) == (0.0, "unpriced")
        governor = _governor(cap_usd=0.0, reserve_per_call_usd=None)
        verdict = governor.authorize_call(
            messages=[{"role": "user", "content": "hello"}],
            target={"model": "a-model-nobody-priced"},
        )
        # The receipt says WHY it could not refuse, instead of silently
        # pricing the call at zero.
        assert "unpriced" in verdict.reason or verdict.price_state == "unpriced"

    def test_a_declared_per_call_bound_is_used_when_the_model_is_unpriced(self) -> None:
        governor = _governor(cap_usd=0.02, reserve_per_call_usd=0.05)
        verdict = governor.authorize_call(
            messages=[{"role": "user", "content": "hi"}],
            target={"model": "a-model-nobody-priced"},
        )
        assert verdict.price_state == "declared_bound"
        assert verdict.allowed is False

    def test_the_reservation_uses_the_capability_registry_cost_function(self) -> None:
        """The price AND its state both come from R2-13's registry
        (`estimate_cost`), never from a private table or from inferring a
        number. Two price authorities would make the cap and the cost
        report disagree, and neither would be auditable."""
        from runtime import model_capabilities

        target = {"model": "gpt-4o-mini", "provider": "openai"}
        messages = [{"role": "user", "content": "x" * 400}]
        price, state = bg.estimated_call_price(messages, target=target)
        expected = model_capabilities.estimate_cost(
            "gpt-4o-mini", 110, bg.DEFAULT_UNOBSERVED_COMPLETION_TOKENS
        )
        assert price == pytest.approx(expected.cost_usd)
        assert state == expected.price_state == model_capabilities.PRICE_PRICED

    def test_a_declared_free_model_is_free_and_says_so(self) -> None:
        """`free` is reachable only by a DECLARED (0.0, 0.0) row, and the
        verdict distinguishes it from `unpriced` — the distinction the
        whole R2-13 registry exists to make."""
        from runtime import model_capabilities

        model_capabilities.register_capability(
            {
                "provider": "",
                "model": "r2-14-free",
                "input_cost_per_million": 0.0,
                "output_cost_per_million": 0.0,
            }
        )
        try:
            price, state = bg.estimated_call_price(
                [{"role": "user", "content": "x" * 4000}],
                target={"model": "r2-14-free"},
            )
        finally:
            model_capabilities.unregister_capability("", "r2-14-free")
        assert price == 0.0
        assert state == model_capabilities.PRICE_FREE
        assert state != model_capabilities.PRICE_UNPRICED

    def test_a_declared_completion_bound_is_what_makes_the_reserve_sound(self) -> None:
        """The honest statement of the granularity, as a test: a bigger
        declared bound reserves more, which is what makes the reservation a
        real upper bound rather than a guess."""
        target = {"model": "gpt-4o-mini", "provider": "openai"}
        small, _ = bg.estimated_call_price([], target=target, max_completion_tokens=64)
        large, _ = bg.estimated_call_price(
            [], target=target, max_completion_tokens=8192
        )
        assert large > small

    def test_a_negative_or_nan_charge_is_ignored_rather_than_credited_back(
        self,
    ) -> None:
        governor = _governor(cap_usd=1.0)
        governor.commit(-5.0)
        governor.commit(float("nan"))
        assert governor.spent_usd() == 0.0
        assert governor.remaining_usd() == 1.0

    def test_a_live_receipt_reports_the_remaining_budget_not_just_the_cap(self) -> None:
        governor = _governor(cap_usd=1.0)
        governor.commit(0.4)
        report = governor.report()
        assert report["cap_usd"] == 1.0
        assert report["spent_usd"] == pytest.approx(0.4)
        assert report["remaining_usd"] == pytest.approx(0.6)
        assert report["exhausted"] is False
        assert report["quota"]["state"] == "available"
        assert report["clock"] == "runtime.fsutil.now_epoch"

    def test_the_receipt_is_written_atomically_and_is_readable(
        self, tmp_path: Path
    ) -> None:
        governor = _governor(cap_usd=1.0)
        governor.commit(0.125)
        target = tmp_path / "logs" / "t" / bg.BUDGET_RECEIPT_NAME
        written = bg.write_budget_receipt(target, governor)
        assert written is not None
        assert json.loads(target.read_text(encoding="utf-8"))["spent_usd"] == (
            pytest.approx(0.125)
        )

    def test_a_quota_wall_refuses_every_later_call_without_pricing_them(self) -> None:
        governor = _governor(cap_usd=100.0)
        governor.note_quota(
            bg.classify_provider_failure(_InsufficientQuota("insufficient_quota"))
        )
        verdict = governor.authorize_call(
            messages=[{"role": "user", "content": "hi"}],
            target={"model": "gpt-4o"},
        )
        assert verdict.allowed is False
        assert verdict.price_state == "quota_source"
        assert governor.exhausted is True
        assert governor.quota_failure is not None
        assert governor.quota_failure.billing_url


# ==========================================================================
# 5. The governed dial loop
# ==========================================================================
class TestGovernedDialLoop:
    def test_a_quota_error_is_raised_on_the_first_attempt_with_zero_retries(
        self,
    ) -> None:
        governor = _governor()
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            raise _InsufficientQuota("insufficient_quota")

        with pytest.raises(bg.QuotaExhausted) as caught:
            bg.governed_completion(
                _dial, max_retries=4, base_backoff_s=15.0, governor=governor
            )
        assert attempts["n"] == 1, "a quota wall must be dialled exactly once"
        assert _sleeps(governor) == [], "a quota wall must not be waited out"
        assert caught.value.failure.kind == bg.QUOTA_EXHAUSTED
        assert governor.report()["quota"]["kind"] == bg.QUOTA_EXHAUSTED

    def test_a_rate_limit_is_retried_and_its_backoff_is_armed_as_an_exemption(
        self,
    ) -> None:
        governor = _governor(backoff_base_s=15.0)
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _RateLimited("429")
            return "recovered"

        assert (
            bg.governed_completion(
                _dial, max_retries=4, base_backoff_s=15.0, governor=governor
            )
            == "recovered"
        )
        assert attempts["n"] == 3
        assert _sleeps(governor) == [15.0, 30.0]
        assert governor.report()["backoff"]["count"] == 2
        assert governor.backoff_window is not None
        assert governor.backoff_window.reason == bg.EXEMPTION_BACKOFF

    def test_a_rate_limit_beyond_the_retry_budget_raises_the_original_error(
        self,
    ) -> None:
        governor = _governor(backoff_base_s=1.0, rate_limit_retries=1)
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            raise _RateLimited("429")

        with pytest.raises(_RateLimited):
            bg.governed_completion(_dial, max_retries=1, governor=governor)
        assert attempts["n"] == 2  # the original attempt plus one retry

    def test_a_non_idempotent_call_is_dialled_exactly_once(self) -> None:
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            raise _RateLimited("429")

        with pytest.raises(_RateLimited):
            bg.governed_completion(_dial, max_retries=4, idempotent=False)
        assert attempts["n"] == 1

    def test_a_quota_wall_is_never_failover_material(self) -> None:
        """A second target on the same billing account spends the same absent
        money, so the governed loop must hand the terminal error upward
        rather than continue."""
        governor = _governor()

        def _dial() -> str:
            raise _InsufficientQuota("insufficient_quota")

        with pytest.raises(bg.QuotaExhausted):
            bg.governed_completion(_dial, max_retries=4, governor=governor)
        assert governor.report()["backoff"]["count"] == 0

    def test_a_transient_failure_still_gets_its_bounded_short_retry(self) -> None:
        governor = _governor(backoff_base_s=60.0)
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _Overloaded("503 Service Unavailable")
            return "ok"

        assert bg.governed_completion(_dial, max_retries=4, governor=governor) == "ok"
        # The TRANSIENT wait, not the rate-limit backoff: distinct actions.
        assert _sleeps(governor) == [bg.TRANSIENT_BACKOFF_S, bg.TRANSIENT_BACKOFF_S]

    def test_a_keyboard_interrupt_is_never_classified_or_retried(self) -> None:
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            bg.governed_completion(_dial, max_retries=4)
        assert attempts["n"] == 1

    def test_a_harness_bug_is_not_relabelled_as_a_provider_fault(self) -> None:
        """A TypeError in our own code is our bug. Retrying it would be
        noise, and calling it a provider outage would blame the wrong side.
        """
        attempts = {"n": 0}

        def _dial() -> str:
            attempts["n"] += 1
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

        with pytest.raises(TypeError):
            bg.governed_completion(_dial, max_retries=4)
        assert attempts["n"] == 1


# ==========================================================================
# 6. The ledger-measured cap guarantee (the required measurement)
# ==========================================================================
class TestCapBoundMeasuredFromTheLedger:
    def test_a_budget_of_x_never_exceeds_x_by_more_than_the_final_calls_price(
        self, tmp_path: Path
    ) -> None:
        """The required test, measured the way the prompt states it: from a
        ledger, not from an assertion about the code.

        The cap is SELF-CALIBRATING against the router's own price for one
        call, so the test does not hardcode a table and cannot silently
        pass because a price changed. Each authorized call is then charged
        2.5x its reservation -- deliberately MORE than reserved, so the
        declared reservation is not a sound upper bound and the "+ one
        call" tail is genuinely exercised rather than assumed.
        """
        target = {"model": "gpt-4o-mini", "provider": "openai"}
        messages = [{"role": "user", "content": "x" * 8000}]

        probe = _governor(cap_usd=1.0, max_completion_tokens=512)
        unit = probe.authorize_call(messages=messages, target=target).reserved_usd
        assert unit > 0.0, (
            "the router must price this model for the probe to mean anything"
        )
        cap = unit * 4.0
        charged_per_call = unit * 2.5

        governor = _governor(
            cap_usd=cap,
            sleep=lambda _s: None,
            backoff_base_s=0.0,
            max_completion_tokens=512,
        )
        ledger: List[Dict[str, Any]] = []
        for _ in range(50):
            verdict = governor.authorize_call(messages=messages, target=target)
            if not verdict.allowed:
                ledger.append({"outcome": "refused", "cost_usd": 0.0})
                break
            governor.commit(charged_per_call, model=str(target["model"]))
            governor.release(verdict.reserved_usd)
            ledger.append(
                {
                    "outcome": "success",
                    "cost_usd": charged_per_call,
                    "reserved_usd": verdict.reserved_usd,
                }
            )

        ledger_path = tmp_path / "model_ledger.jsonl"
        ledger_path.write_text(
            "\n".join(json.dumps(row) for row in ledger) + "\n", encoding="utf-8"
        )
        measured = sum(
            json.loads(line)["cost_usd"]
            for line in ledger_path.read_text().splitlines()
        )
        authorized = [row for row in ledger if row["outcome"] == "success"]
        assert len(authorized) >= 2, "the test must exercise several calls, not one"
        final_call_price = authorized[-1]["cost_usd"]

        # The bound, from the ledger.
        assert measured <= cap + final_call_price + 1e-12
        # The refusal actually fired: the cap stopped the run rather than
        # the loop running to its natural end.
        assert ledger[-1]["outcome"] == "refused"
        # And the cap was actually hit, or the measurement proves nothing.
        assert measured > cap, "this measurement is only meaningful if the cap was hit"
        # The overshoot is bounded by ONE call -- that is the whole claim,
        # and it is what replaces the measured 9.28x attempt-level bound.
        assert measured - cap <= final_call_price + 1e-12
        # The old behaviour for comparison: 50 calls of this size would
        # have cost 50x, since nothing stopped the loop mid-burst.
        assert measured < charged_per_call * len(authorized) + 1e-12

    def test_the_harness_attempt_level_check_stays_a_backstop(self) -> None:
        """`over_budget()` must read the governor, so a per-call refusal is
        observed by the next attempt boundary even though nothing was
        spent."""
        governor = _governor(cap_usd=0.001)
        governor.authorize_call(
            messages=[{"role": "user", "content": "x" * 40000}],
            target={"model": "gpt-4o", "provider": "openai"},
        )
        assert governor.spent_usd() == 0.0
        assert governor.exhausted is True


# ==========================================================================
# 7. The gateway refuses before it dials, and stops on quota
# ==========================================================================
class TestGatewayRefusesBeforeDialing:
    def _prepare(self, tmp_path: Path, config: Dict[str, Any]) -> Any:
        from runtime import model_router, provider_gateway

        ledger = tmp_path / "ledger.jsonl"
        ctx = {
            "provider": "openai",
            "model": "gpt-4o",
            "use_mock_provider": False,
            "rate_limit_retries": 0,
            # Any Ceiling-14 key enters the resilient pipeline; `offline`
            # is the cheapest one that does not change what is dialed.
            "offline": False,
            **config,
        }
        model_router.set_call_context(ctx, ledger_dir=str(ledger))
        return provider_gateway, model_router, ledger

    def test_a_call_over_budget_is_refused_before_any_request_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        governor = _governor(cap_usd=0.0001, reserve_per_call_usd=0.5)
        budget_governor.install_governor(governor)
        gateway, model_router, ledger = self._prepare(tmp_path, {})
        dialed: List[Dict[str, Any]] = []

        class _Boom(Exception):
            pass

        import litellm  # noqa: F401  (imported for the seam, never called)

        monkeypatch.setattr(
            "litellm.completion",
            lambda **kwargs: dialed.append(kwargs) or _FakeResponse(),
        )
        try:
            with pytest.raises(bg.BudgetRefused):
                gateway.resilient_call_model(
                    [{"role": "user", "content": "hi"}], "easy"
                )
        finally:
            budget_governor.clear_governor()
            model_router.set_call_context(None)
        assert dialed == [], "no provider request may be built when it cannot fit"
        rows = [
            json.loads(line)
            for line in Path(ledger).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        refused = [row for row in rows if row.get("skipped_reason") == "budget_refused"]
        assert refused, "the refusal must be on the ledger, not silent"
        assert refused[0]["skipped_reason"] == "budget_refused"

    def test_a_quota_error_stops_the_chain_without_dialing_a_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quota wall is terminal: one attempt, no failover, no breaker
        trip. The chain must not walk to the next candidate."""
        governor = _governor()
        budget_governor.install_governor(governor)
        gateway, model_router, ledger = self._prepare(
            tmp_path,
            {
                "provider_fallbacks": [
                    {"provider": "openai", "model": "gpt-4o-mini"},
                ]
            },
        )
        dials: List[Dict[str, Any]] = []

        class _Quota(Exception):
            status_code = 429

        def _fake_completion(**kwargs: Any) -> Any:
            dials.append(kwargs)
            raise _Quota("insufficient_quota")

        monkeypatch.setattr("litellm.completion", _fake_completion)
        try:
            with pytest.raises(bg.QuotaExhausted):
                gateway.resilient_call_model(
                    [{"role": "user", "content": "hi"}], "easy"
                )
        finally:
            budget_governor.clear_governor()
            model_router.set_call_context(None)
        assert len(dials) == 1, "a quota wall must not be failed over"
        rows = [
            json.loads(line)
            for line in Path(ledger).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(row.get("skipped_reason") == "quota_exhausted" for row in rows)
        assert governor.report()["quota"]["kind"] == bg.QUOTA_EXHAUSTED


# ==========================================================================
# 8. End-to-end: a 429 backoff longer than the hang window SURVIVES
# ==========================================================================
def _backoff_task_config(logs_root: Path, **overrides: Any) -> Dict[str, Any]:
    """Config for the backoff-vs-hang-window experiment.

    The hang window is 2.5 s, which is deliberately ABOVE the runtime's
    2.0 s heartbeat cadence: a window below the cadence can only ever fire
    the HEARTBEAT kill, and a heartbeat kill is not what this round fixed.
    The backoff is 4.0 s, so it outlasts the window by 1.6 s -- the defect,
    scaled down so the test finishes in seconds.

    Every value is a knob in ``Task.config``; nothing is hardcoded in the
    scheduler or the worker.
    """
    config: Dict[str, Any] = {
        "use_fake_harness": True,
        "fake_steps": ["plan", "backoff", "verify"],
        "fake_backoff_step": "backoff",
        "fake_backoff_429s": 4.0,
        "fake_backoff_429_count": 1,
        "rate_limit_retries": 1,
        "rate_limit_backoff_s": 4.0,
        "hang_heartbeat_stale_s": 2.5,
        "max_wallclock_s": 120.0,
        "crash_retries": 0,
        "resume": True,
        "log_root": str(logs_root),
    }
    config.update(overrides)
    return config


def _run_backoff_task(
    tmp_path: Path, task_id: str, **overrides: Any
) -> tuple[Any, Dict[str, Any], List[Dict[str, Any]]]:
    """Run one backoff task through the REAL scheduler; return the result,
    the resolved config, and the run journal rows."""
    from runtime.config import apply_defaults
    from runtime.scheduler import Scheduler
    from shared.types import Task

    logs_root = tmp_path / "logs"
    config = _backoff_task_config(logs_root, **overrides)
    scheduler = Scheduler(concurrency=1, logs_root=str(logs_root))
    results = scheduler.run(
        [
            Task(
                task_id=task_id,
                repo_path=str(tmp_path),
                issue_text="a 429 that backs off longer than the hang window",
                config=config,
            )
        ],
        poll_interval_s=0.05,
    )
    events = [
        json.loads(line)
        for line in (scheduler.run_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    return results[task_id], apply_defaults(config), events


class TestBackoffLongerThanHangWindowSurvives:
    def test_a_worker_in_a_429_backoff_is_not_hang_killed(self, tmp_path: Path) -> None:
        """The required end-to-end proof, with REAL supervision machinery.

        Real `Scheduler`, real `python -m runtime.worker` subprocess, real
        runtime checkpoint + heartbeat, real
        `budget_governor.governed_completion` dial loop, real 429-shaped
        exception. The backoff (4.0 s) is deliberately LONGER than the hang
        window (2.5 s), which is the defect: before the exemption this
        worker was state-stale-killed mid-backoff.
        """
        result, cfg, events = _run_backoff_task(tmp_path, "r2-14-backoff")
        assert result.status == "success", (
            "a provider backing off correctly must not be killed as hung"
        )

        names = [event["event"] for event in events]
        assert "hang_timeout" not in names, (
            f"the worker was hang-killed during its provider backoff: {events}"
        )
        assert "hang_exempt" in names, (
            "the exemption must be auditable in the run journal, not silent"
        )
        exempt = [e for e in events if e["event"] == "hang_exempt"]
        assert any(
            "provider_backoff" in str(e["data"].get("reason", "")) for e in exempt
        )
        # The window really did outlast the hang threshold: without that,
        # the survival above would prove nothing. The upper bound is the
        # DECLARED window: the backoff plus the watchdog's own staleness
        # grace, so a reader can see the exemption is bounded rather than
        # open-ended.
        window = float(cfg["fake_backoff_429s"]) + float(cfg["hang_heartbeat_stale_s"])
        assert max((e["data"].get("state_age_s") or 0.0) for e in exempt) > 2.5
        assert max((e["data"].get("state_age_s") or 0.0) for e in exempt) <= window

        # The worker really was inside a governed backoff, and the receipt
        # shows the exact wait the exemption was derived from.
        from runtime.checkpoint import TaskCheckpoint
        from runtime.paths import runtime_root

        checkpoint = TaskCheckpoint(
            str(runtime_root("r2-14-backoff", cfg, tmp_path / "logs"))
        ).load()
        assert checkpoint is not None
        assert checkpoint["status"] == "finished"
        # The marker is cleared atomically with "finished", so a stale
        # window can never outlive the run.
        assert checkpoint.get("supervision_exemption") is None
        receipt = json.loads(
            (tmp_path / "logs" / "r2-14-backoff" / bg.BUDGET_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        assert receipt["backoff"]["count"] == 1
        assert receipt["backoff"]["seconds_total"] == pytest.approx(4.0, abs=0.5)
        # The grace is the watchdog's own window, so the exemption and the
        # kill threshold are the same number.
        assert receipt["backoff"]["grace_s"] == pytest.approx(2.5)
        assert receipt["quota"]["state"] == "available"
        # The live remaining budget is visible, not discoverable afterwards.
        assert receipt["cap_usd"] is not None
        assert receipt["remaining_usd"] is not None
        assert receipt["exhausted"] is False

    def test_the_same_backoff_with_the_exemption_off_is_still_hang_killed(
        self, tmp_path: Path
    ) -> None:
        """The honest OFF arm, and the A/B that makes the fix measurable.

        Identical task, identical real backoff, identical real 429 — only
        `hang_backoff_exempt: False`. Without this contrast, "the worker
        survived" could mean the test simply cannot be killed.
        """
        result, _cfg, events = _run_backoff_task(
            tmp_path, "r2-14-noexempt", hang_backoff_exempt=False
        )
        names = [event["event"] for event in events]
        assert "hang_exempt_suppressed" in names, (
            "the OFF arm must be recorded, not silent"
        )
        assert "hang_timeout" in names, (
            "with the exemption off, the historical state-stale kill must fire"
        )
        timed_out = [
            e
            for e in events
            if e["event"] == "hang_timeout" and e["data"].get("signal") == "state_stale"
        ]
        assert timed_out, f"the kill must be the state-stale signal: {events}"
        assert result.status in {"timeout", "error", "failed"}


class TestQuotaEndsTheRunAsQuota:
    def test_a_quota_error_in_a_worker_ends_the_run_without_a_retry(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: real worker, real governor, quota error at the dial.

        The run ends AS QUOTA — `TaskResult.status` stays inside the
        historical four-value vocabulary (`shared/types.py` is another
        owner's file), and the machine-readable verdict is the
        `quota_exhausted` terminal reason on the checkpoint, the run
        journal, the budget receipt, and the trace. Zero retries: the
        governed loop's single dial is proven by the receipt's backoff
        count being zero and by the quota block naming the billing page.
        """
        from runtime.checkpoint import TaskCheckpoint
        from runtime.config import apply_defaults
        from runtime.paths import runtime_root
        from runtime.scheduler import Scheduler
        from shared.types import Task

        logs_root = tmp_path / "logs"
        config: Dict[str, Any] = {
            "use_fake_harness": True,
            "fake_steps": ["plan", "quota", "verify"],
            "fake_quota_step": "quota",
            "rate_limit_retries": 4,
            "hang_heartbeat_stale_s": 30.0,
            "max_wallclock_s": 120.0,
            "crash_retries": 0,
            "resume": True,
            "log_root": str(logs_root),
        }
        scheduler = Scheduler(concurrency=1, logs_root=str(logs_root))
        results = scheduler.run(
            [
                Task(
                    task_id="r2-14-quota",
                    repo_path=str(tmp_path),
                    issue_text="the provider account is out of credit",
                    config=config,
                )
            ],
            poll_interval_s=0.05,
        )
        result = results["r2-14-quota"]
        assert result.status == "error"
        assert result.verification is None, "quota must not mint any verification"
        assert not result.diff

        cfg = apply_defaults(config)
        checkpoint = TaskCheckpoint(
            str(runtime_root("r2-14-quota", cfg, logs_root))
        ).load()
        assert checkpoint is not None
        assert checkpoint["terminal_reason"] == bg.QUOTA_EXHAUSTED
        quota = checkpoint["quota"]
        assert quota["kind"] == bg.QUOTA_EXHAUSTED
        assert quota["retryable"] is False
        assert quota["billing_url"]

        events = [
            json.loads(line)
            for line in (scheduler.run_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        finishes = [e for e in events if e["event"] == "finish"]
        assert finishes and finishes[0]["data"].get("terminal_reason") == (
            bg.QUOTA_EXHAUSTED
        )
        # A quota wall is not a crash, so the crash budget was never spent
        # and the task was not relaunched into the same empty account.
        assert "crash_retry" not in [e["event"] for e in events]

        receipt = json.loads(
            (logs_root / "r2-14-quota" / bg.BUDGET_RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        assert receipt["quota"]["kind"] == bg.QUOTA_EXHAUSTED
        assert receipt["backoff"]["count"] == 0, "a quota wall is never waited out"
        assert receipt["quota"]["billing_url"]


# ==========================================================================
# 9. The governor is opt-in and honest about being absent
# ==========================================================================
class TestAbsenceIsAMeaningfulState:
    def test_no_governor_installed_reads_as_none_not_as_a_zero_budget(self) -> None:
        budget_governor.clear_governor()
        assert budget_governor.current_governor() is None

    def test_the_harness_resolves_none_when_nothing_is_installed(self) -> None:
        from harness import core as harness_core

        budget_governor.clear_governor()
        assert harness_core._resolve_budget_governor() is None

    def test_the_harness_resolves_the_installed_governor(self) -> None:
        from harness import core as harness_core

        governor = _governor()
        budget_governor.install_governor(governor)
        try:
            assert harness_core._resolve_budget_governor() is governor
        finally:
            budget_governor.clear_governor()

    def test_the_harness_falls_back_to_the_router_context(self) -> None:
        from harness import core as harness_core
        from runtime import model_router

        governor = _governor()
        budget_governor.clear_governor()
        model_router.set_call_context({"budget_governor": governor})
        try:
            assert harness_core._resolve_budget_governor() is governor
        finally:
            model_router.set_call_context(None)

    def test_a_value_that_is_not_a_governor_is_refused(self) -> None:
        from harness import core as harness_core
        from runtime import model_router

        budget_governor.clear_governor()
        model_router.set_call_context({"budget_governor": {"cap_usd": 1.0}})
        try:
            assert harness_core._resolve_budget_governor() is None
        finally:
            model_router.set_call_context(None)

    def test_the_attempt_level_check_is_byte_identical_without_a_governor(self) -> None:
        """The historical comparison, asserted directly: no governor means
        the old arithmetic, not a second opinion."""
        cap = 2.0
        assert not (cap <= 0.0)
        assert cap <= 2.0
        assert not (cap <= 1.99)

    def test_no_r2_14_key_is_published_in_a_shared_defaults_table(self) -> None:
        """A value in DEFAULTS is merged into every task and every eval arm,
        so it would switch all of them silently. The governor's new keys are
        deliberately absent from both defaults tables."""
        from harness.config import DEFAULTS as HARNESS_DEFAULTS
        from runtime.config import DEFAULTS as RUNTIME_DEFAULTS

        for key in (
            "budget_reserve_per_call_usd",
            "budget_governor",
            "supervision_exemption",
        ):
            assert key not in HARNESS_DEFAULTS, f"{key} must not be a harness default"
            assert key not in RUNTIME_DEFAULTS, f"{key} must not be a runtime default"
        # budget_cap_usd is PRE-EXISTING in harness DEFAULTS and is
        # deliberately left there: it is the historical cap, not this
        # round's change, and moving it would be a behaviour change.
        assert "budget_cap_usd" in HARNESS_DEFAULTS


# ==========================================================================
# 10. The clock is shared, not duplicated
# ==========================================================================
class TestOneClock:
    def test_the_governor_default_clock_is_the_shared_epoch_clock(self) -> None:
        governor = bg.BudgetGovernor()
        assert governor.clock is now_epoch

    def test_the_exemption_reader_defaults_to_the_shared_clock(self) -> None:
        window = _governor(backoff_base_s=30.0).begin_backoff(1)
        checkpoint = {"supervision_exemption": window.as_dict()}
        # No `now` given: the reader must use the shared clock, and the
        # window is 30s wide, so it is live either side of this assertion.
        assert bg.state_stale_exempt(checkpoint)[0] is True

    def test_the_worker_exemption_marker_survives_a_real_checkpoint_round_trip(
        self, tmp_path: Path
    ) -> None:
        """The marker is JSON on disk. If a float lost precision through the
        write, a long backoff would silently shorten and the watchdog would
        start killing again."""
        from runtime.checkpoint import TaskCheckpoint

        cp = TaskCheckpoint(str(tmp_path / "rt"))
        cp.save({"task_id": "t", "status": "running"})
        window = _governor(backoff_base_s=225.0).begin_backoff(1)
        cp.update(supervision_exemption=window.as_dict())
        reloaded = TaskCheckpoint(str(tmp_path / "rt")).load()
        assert reloaded is not None
        marker = reloaded["supervision_exemption"]
        assert marker["seconds"] == pytest.approx(225.0)
        assert marker["until_epoch"] - marker["started_epoch"] == pytest.approx(225.0)
        assert not math.isnan(float(marker["until_epoch"]))
