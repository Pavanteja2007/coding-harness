"""Router unit tests: routing tiers, usage ledger, mock provider, hints.

Fast, offline (mock provider only) — no network, no API keys needed.
"""

import json
import threading
import types
from typing import ClassVar

import pytest

from runtime import mock_provider
from runtime.model_router import (
    call_model,
    get_last_usage,
    set_call_context,
)


@pytest.fixture(autouse=True)
def _clean_context():
    set_call_context(None)
    mock_provider.reset()
    yield
    set_call_context(None)
    mock_provider.reset()


def _enable_mock(tiers=None, adaptive=False, **ctx):
    mock_provider.install(
        {
            "gpt-4o-mini": "cheap reply",
            "claude-3-5-sonnet-20241022": "expensive reply",
        }
    )
    set_call_context(
        {
            "use_mock_provider": True,
            "adaptive_routing": adaptive,
            **({"model_tiers": tiers} if tiers else {}),
            **ctx,
        }
    )


class TestExplicitOverrides:
    def test_explicit_model_wins(self):
        _enable_mock(adaptive=True)
        out = call_model(
            [{"role": "user", "content": "hi"}],
            model="claude-3-5-sonnet-20241022",
        )
        assert out == "expensive reply"
        usage = get_last_usage()
        assert usage["model"] == "claude-3-5-sonnet-20241022"
        assert usage["routed_via_hint"] is None

    def test_explicit_model_beats_routing(self):
        _enable_mock(adaptive=True)
        out = call_model(
            [{"role": "user", "content": "very hard"}],
            difficulty_hint="hard",
            model="gpt-4o-mini",
        )
        assert out == "cheap reply"  # explicit beats the tier


class TestAdaptiveRouting:
    TIERS: ClassVar[dict[str, dict[str, str]]] = {
        "easy": {"provider": "openai", "model": "gpt-4o-mini"},
        "medium": {"provider": "openai", "model": "gpt-4o-mini"},
        "hard": {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"},
    }

    def test_easy_routes_cheap(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "x"}], difficulty_hint="easy")
        assert get_last_usage()["model"] == "gpt-4o-mini"
        assert get_last_usage()["routed_via_hint"] == "easy"

    def test_hard_routes_expensive(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "x"}], difficulty_hint="hard")
        assert get_last_usage()["model"] == "claude-3-5-sonnet-20241022"

    def test_toggle_off_ignores_hint(self):
        _enable_mock(tiers=self.TIERS, adaptive=False)
        call_model([{"role": "user", "content": "x"}], difficulty_hint="hard")
        # routing off: falls to the medium default, not the hard tier
        assert get_last_usage()["routed_via_hint"] == "medium-default"
        assert get_last_usage()["model"] == "gpt-4o-mini"

    def test_hint_none_predicts_from_content(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        # loaded content: long, stack-tracey, file mentions, complexity words
        long_hard = (
            "Crash with Traceback and ValueError: ... "
            * 4
            + "src/mod/parser.py fails; race condition, deadlock, "
            "flaky intermittent timing. " * 2
        )
        call_model([{"role": "user", "content": long_hard}])
        usage = get_last_usage()
        assert usage["model"] == "claude-3-5-sonnet-20241022"
        assert usage["routed_via_hint"] == "hard"

    def test_hint_none_easy_content_routes_cheap(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "fix typo"}])
        usage = get_last_usage()
        assert usage["model"] == "gpt-4o-mini"
        assert usage["routed_via_hint"] == "easy"
        assert usage["difficulty_prediction"]["source"] == "predicted"

    def test_medium_hint_routes_medium_tier(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "x"}], difficulty_hint="medium")
        usage = get_last_usage()
        assert usage["model"] == "gpt-4o-mini"
        assert usage["routed_via_hint"] == "medium"
        assert usage["difficulty_hint"] == "medium"

    def test_unknown_hint_predicts_instead_of_defaulting(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "x"}], difficulty_hint="bogus")
        usage = get_last_usage()
        assert usage["model"] == "gpt-4o-mini"
        assert usage["routed_via_hint"] == "easy"
        assert usage["difficulty_hint"] == "easy"

    def test_explicit_none_predicts_instead_of_defaulting(self):
        _enable_mock(tiers=self.TIERS, adaptive=True)
        call_model([{"role": "user", "content": "x"}], difficulty_hint=None)
        usage = get_last_usage()
        assert usage["routed_via_hint"] == "easy"
        assert usage["difficulty_prediction"]["source"] == "predicted"


class TestLedger:
    def test_usage_recorded_per_call(self, tmp_path):
        mock_provider.install({"gpt-4o-mini": "abc"})
        set_call_context(
            {"use_mock_provider": True}, ledger_dir=str(tmp_path / "ledger.jsonl")
        )
        call_model([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        call_model([{"role": "user", "content": "hi again"}], model="gpt-4o-mini")
        lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        rec = json.loads(lines[0])
        for key in (
            "model",
            "provider",
            "prompt_tokens",
            "completion_tokens",
            "tokens",
            "cost_usd",
            "routed_via_hint",
        ):
            assert key in rec

    def test_cost_fallback_matches_price_table(self):
        mock_provider.install({"gpt-4o-mini": "x" * 4})
        set_call_context({"use_mock_provider": True})
        call_model([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        u = get_last_usage()
        assert u["cost_usd"] > 0  # price table applies to mock too
        assert u["tokens"] == u["prompt_tokens"] + u["completion_tokens"]

    def test_get_last_usage_empty_before_any_call(self):
        assert get_last_usage() == {}

    def test_configured_price_supports_byo_model_cost(self, tmp_path):
        mock_provider.install({"custom-model": "ok"})
        set_call_context(
            {
                "use_mock_provider": True,
                "model_prices": {
                    "custom-model": {
                        "input_cost_per_million": 2.0,
                        "output_cost_per_million": 4.0,
                    }
                },
            },
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        call_model([{"role": "user", "content": "hello"}], model="custom-model")
        usage = get_last_usage()
        assert usage["cost_source"] == "configured"
        assert usage["cost_usd"] > 0
        row = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8"))
        assert row["cost_source"] == "configured"


class TestConcurrentContext:
    def test_router_context_usage_and_ledgers_are_thread_local(
        self, tmp_path, monkeypatch
    ):
        barrier = threading.Barrier(2)
        seen = {}
        usages = {}

        def completion(**kwargs):
            name = threading.current_thread().name
            seen[name] = dict(kwargs)
            barrier.wait(timeout=5)
            message = types.SimpleNamespace(content=f"reply-{kwargs['model']}")
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=2),
                _hidden_params={},
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )

        def invoke(index):
            name = f"router-{index}"
            thread = threading.current_thread()
            thread.name = name
            model = f"model-{index}"
            ledger = tmp_path / f"ledger-{index}.jsonl"
            set_call_context(
                {
                    "task_id": f"task-{index}",
                    "adaptive_routing": True,
                    "model_tiers": {
                        "easy": {
                            "provider": f"provider-{index}",
                            "model": model,
                            "api_key": f"key-{index}",
                            "api_base": f"https://endpoint-{index}.example/v1",
                        }
                    },
                },
                ledger_dir=ledger,
            )
            try:
                assert (
                    call_model(
                        [{"role": "user", "content": "x"}],
                        difficulty_hint="easy",
                    )
                    == f"reply-provider-{index}/{model}"
                )
                usages[index] = get_last_usage()
                row = json.loads(ledger.read_text(encoding="utf-8"))
                assert row["model"] == model
                assert row["provider"] == f"provider-{index}"
            finally:
                set_call_context(None)

        threads = [
            threading.Thread(target=invoke, args=(index,), name=f"router-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert get_last_usage() == {}
        for index in range(2):
            name = f"router-{index}"
            assert seen[name]["api_key"] == f"key-{index}"
            assert seen[name]["api_base"] == f"https://endpoint-{index}.example/v1"
            assert usages[index]["model"] == f"model-{index}"
            assert usages[index]["difficulty_hint"] == "easy"


class TestFailureLedger:
    def test_provider_failure_is_recorded_without_secret(self, tmp_path, monkeypatch):
        sentinel = "sentinel-provider-key"

        def completion(**_kwargs):
            raise RuntimeError(f"AuthenticationError: key={sentinel}")

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        ledger = tmp_path / "ledger.jsonl"
        set_call_context({"api_key": sentinel}, ledger_dir=ledger)
        with pytest.raises(RuntimeError, match="AuthenticationError"):
            call_model([{"role": "user", "content": "x"}], model="custom")
        usage = get_last_usage()
        row = json.loads(ledger.read_text(encoding="utf-8"))
        assert usage["outcome"] == "error"
        assert row["outcome"] == "error"
        assert row["model"] == "custom"
        assert row["prompt_tokens"] == row["completion_tokens"] == 0
        assert sentinel not in json.dumps(row)

    def test_retry_attempts_share_call_id_and_are_both_recoverable(
        self, tmp_path, monkeypatch
    ):
        state = {"calls": 0}

        def completion(**_kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("RateLimitError: 429")
            message = types.SimpleNamespace(content="recovered")
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=1),
                _hidden_params={},
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        monkeypatch.setattr("runtime.model_router.time.sleep", lambda _seconds: None)
        ledger = tmp_path / "ledger.jsonl"
        set_call_context({"rate_limit_retries": 1}, ledger_dir=ledger)
        assert (
            call_model([{"role": "user", "content": "x"}], model="custom")
            == "recovered"
        )
        rows = [
            json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        assert [row["outcome"] for row in rows] == ["error", "success"]
        assert rows[0]["call_id"] == rows[1]["call_id"]
        assert [row["attempt"] for row in rows] == [1, 2]

    def test_ledger_write_failure_is_not_silent(self, tmp_path, monkeypatch):
        from runtime import model_router

        mock_provider.install({"gpt-4o-mini": "ok"})
        set_call_context({"use_mock_provider": True}, ledger_dir=tmp_path / "ledger")
        monkeypatch.setattr(
            model_router,
            "append_jsonl",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            call_model([{"role": "user", "content": "x"}], model="gpt-4o-mini")


class TestMockProviderGuards:
    def test_missing_mock_response_raises(self):
        set_call_context({"use_mock_provider": True})
        with pytest.raises(RuntimeError, match="no response"):
            call_model([{"role": "user", "content": "hi"}])

    def test_dynamic_mock_failure_is_not_silently_replaced(self):
        def broken(*_args):
            raise ValueError("broken script")

        mock_provider.install({"gpt-4o-mini": "canned"}, dynamic=broken)
        set_call_context({"use_mock_provider": True})
        with pytest.raises(ValueError, match="broken script"):
            call_model([{"role": "user", "content": "hi"}], model="gpt-4o-mini")


class TestHints:
    def test_hint_normalization(self):
        # normalization happens inside _norm_hint; exercise via call paths
        _enable_mock()
        call_model([{"role": "user", "content": "x"}], difficulty_hint="EASY")
        # "EASY" -> "easy" (normalized), routing off so default tier used
        assert get_last_usage()["difficulty_hint"] == "easy"


class TestTierEndpointWiring:
    """A tier may carry its own api_key/api_base (multi-provider routing:
    each tier hits a different gateway). A fake litellm captures the
    kwargs _resolve_target -> call_model actually sends."""

    @staticmethod
    def _fake_litellm(monkeypatch, capture):
        import types

        def completion(**kwargs):
            capture.update(kwargs)
            msg = types.SimpleNamespace(content="fake")
            choice = types.SimpleNamespace(message=msg)
            usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=2)
            return types.SimpleNamespace(
                choices=[choice],
                usage=usage,
                _hidden_params={},
            )

        fake = types.SimpleNamespace(completion=completion)
        monkeypatch.setitem(__import__("sys").modules, "litellm", fake)

    def test_tier_api_key_and_base_reach_litellm(self, tmp_path, monkeypatch):
        from runtime import model_router

        capture: dict = {}
        self._fake_litellm(monkeypatch, capture)
        set_call_context(
            {
                "adaptive_routing": True,
                "model_tiers": {
                    "hard": {
                        "provider": "openai",
                        "model": "glm",
                        "api_key": "sk-tier",
                        "api_base": "https://tier.example/v1",
                    },
                },
            },
            ledger_dir=str(tmp_path / "ledger.jsonl"),
        )
        out = model_router.call_model(
            [{"role": "user", "content": "x"}], difficulty_hint="hard"
        )
        assert out == "fake"
        assert capture["model"] == "openai/glm"
        assert capture["api_key"] == "sk-tier"
        assert capture["api_base"] == "https://tier.example/v1"
        assert get_last_usage()["routed_via_hint"] == "hard"

    def test_context_api_base_applies_without_tier(self, monkeypatch):
        from runtime import model_router

        capture: dict = {}
        self._fake_litellm(monkeypatch, capture)
        set_call_context({"api_base": "https://ctx.example/v1", "api_key": "sk-ctx"})
        model_router.call_model([{"role": "user", "content": "x"}])
        assert capture["api_base"] == "https://ctx.example/v1"
        assert capture["api_key"] == "sk-ctx"

    def test_call_arg_api_key_beats_tier_key(self, monkeypatch):
        from runtime import model_router

        capture: dict = {}
        self._fake_litellm(monkeypatch, capture)
        set_call_context(
            {
                "adaptive_routing": True,
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "m", "api_key": "sk-tier"}
                },
            }
        )
        model_router.call_model(
            [{"role": "user", "content": "x"}],
            difficulty_hint="easy",
            api_key="sk-call",
        )
        assert capture["api_key"] == "sk-call"


class TestRateLimitRetry:
    """Rate-limit errors (429s) must back off and retry, not kill the
    task; non-rate-limit errors must propagate immediately."""

    @staticmethod
    def _flaky_litellm(monkeypatch, n_failures: int, err_text: str):
        import types

        state = {"calls": 0}

        def completion(**kwargs):
            state["calls"] += 1
            if state["calls"] <= n_failures:
                raise RuntimeError(f"litellm.RateLimitError: {err_text}")
            msg = types.SimpleNamespace(content="recovered")
            choice = types.SimpleNamespace(message=msg)
            usage = types.SimpleNamespace(prompt_tokens=3, completion_tokens=1)
            return types.SimpleNamespace(
                choices=[choice],
                usage=usage,
                _hidden_params={},
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        return state

    def test_rate_limit_retries_then_succeeds(self, monkeypatch):
        from runtime import model_router

        self._flaky_litellm(monkeypatch, 2, "Maximum 8 requests within 1 minutes")
        monkeypatch.setattr(model_router.time, "sleep", lambda s: None)
        set_call_context({"rate_limit_retries": 4})
        out = model_router.call_model([{"role": "user", "content": "x"}])
        assert out == "recovered"

    def test_rate_limit_gives_up_after_budget(self, monkeypatch):
        from runtime import model_router

        state = self._flaky_litellm(monkeypatch, 99, "429 Too Many Requests")
        monkeypatch.setattr(model_router.time, "sleep", lambda s: None)
        set_call_context({"rate_limit_retries": 3})
        with pytest.raises(RuntimeError, match="429"):
            model_router.call_model([{"role": "user", "content": "x"}])
        # 1 initial + 3 retries
        assert state["calls"] == 4

    def test_other_errors_propagate_without_retry(self, monkeypatch):
        import types

        from runtime import model_router

        state = {"calls": 0}

        def completion(**kwargs):
            state["calls"] += 1
            raise RuntimeError("litellm.AuthenticationError: invalid key")

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        monkeypatch.setattr(model_router.time, "sleep", lambda s: None)
        set_call_context({"rate_limit_retries": 4})
        with pytest.raises(RuntimeError, match="AuthenticationError"):
            model_router.call_model([{"role": "user", "content": "x"}])
        assert state["calls"] == 1  # no retries burned


class TestProviderProfilePlumbing:
    def test_named_router_profile_is_normalized_for_openai_compatible_calls(
        self, monkeypatch
    ):
        from runtime import model_router

        capture = {}

        def completion(**kwargs):
            capture.update(kwargs)
            message = types.SimpleNamespace(content="profile-ok")
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                _hidden_params={},
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        set_call_context(
            {
                "use_mock_provider": False,
                "provider_profile": {
                    "provider": "agentrouter",
                    "model": "router-model",
                    "base_url": "https://router.example/v1",
                    "api_key": "sk-profile",
                },
            }
        )
        assert (
            model_router.call_model([{"role": "user", "content": "x"}]) == "profile-ok"
        )
        assert capture["model"] == "openai/router-model"
        assert capture["api_base"] == "https://router.example/v1"
        assert capture["api_key"] == "sk-profile"

    def test_private_router_helpers_keep_legacy_call_shapes(self, monkeypatch):
        from runtime import model_router

        target = model_router._resolve_target("easy", None, None, {})
        assert target["model"]
        assert model_router._cost_fallback(target, 10, 5)[0] >= 0
        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=lambda **_kwargs: object()),
        )
        assert model_router._completion_with_retry({}, 0, 0) is not None

    def test_transient_gateway_flake_retried(self, monkeypatch):
        """The observed flake: BadRequestError with an EMPTY message
        (gateway degradation, not a real 400) must retry and recover."""
        import types

        from runtime import model_router

        state = {"calls": 0}

        def completion(**kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("litellm.BadRequestError: ")
            msg = types.SimpleNamespace(content="recovered")
            choice = types.SimpleNamespace(message=msg)
            usage = types.SimpleNamespace(prompt_tokens=3, completion_tokens=1)
            return types.SimpleNamespace(
                choices=[choice],
                usage=usage,
                _hidden_params={},
            )

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        monkeypatch.setattr(model_router.time, "sleep", lambda s: None)
        set_call_context({"rate_limit_retries": 4})
        out = model_router.call_model([{"role": "user", "content": "x"}])
        assert out == "recovered"
        assert state["calls"] == 2

    def test_real_badrequest_with_message_propagates(self, monkeypatch):
        import types

        from runtime import model_router

        state = {"calls": 0}

        def completion(**kwargs):
            state["calls"] += 1
            raise RuntimeError("litellm.BadRequestError: field messages is required")

        monkeypatch.setitem(
            __import__("sys").modules,
            "litellm",
            types.SimpleNamespace(completion=completion),
        )
        monkeypatch.setattr(model_router.time, "sleep", lambda s: None)
        set_call_context({"rate_limit_retries": 4})
        with pytest.raises(RuntimeError, match="messages is required"):
            model_router.call_model([{"role": "user", "content": "x"}])
        assert state["calls"] == 1
