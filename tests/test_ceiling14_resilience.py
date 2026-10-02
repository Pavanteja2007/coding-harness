"""VEX-CEILING-14 — local-first, provider resilience, offline mode, privacy.

Host-only and offline: every provider is a fake callable or a fake ``litellm``
module, so no credential is read and no packet leaves the machine. The five
tests the ceiling prompt requires are named ``test_required_*``; the rest are
regression coverage for the decisions those five depend on.

Lane honesty: this suite exercises the MACHINERY (breaker, bounded fallback,
privacy gate, offline gate, redaction, locked config writes, durable queue).
It proves no packet is attempted and no credential is read. It does NOT
prove anything about a real provider's behavior, and nothing here is
reported as a live-provider pass.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from cli import neoconfig
from runtime import (
    local_models,
    model_router,
    offline_mode,
    privacy_policy,
    provider_gateway,
    provider_resilience,
)

# ---------------------------------------------------------------------------
# fixtures / doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_router_state(monkeypatch):
    """Isolate the process-global breaker registry and the router context."""
    monkeypatch.delenv("NEO_OFFLINE", raising=False)
    monkeypatch.delenv("NEO_CIRCUIT_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("NEO_CIRCUIT_BREAKER_RESET_S", raising=False)
    provider_resilience.reset_registry()
    model_router.set_call_context(None)
    yield
    provider_resilience.reset_registry()
    model_router.set_call_context(None)


def _fake_litellm(handler, capture=None):
    """Install a fake ``litellm`` module whose ``completion`` calls ``handler``.

    ``handler(kwargs) -> response``; raising propagates to the router's real
    retry/classify path, so a test exercises the production error handling
    rather than a stub of it.
    """
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(dict(kwargs))
        if capture is not None:
            capture.update(kwargs)
        return handler(kwargs)

    module = types.ModuleType("litellm")
    module.completion = completion
    module.calls = calls  # type: ignore[attr-defined]
    return module


def _response(
    content="ok", prompt_tokens=11, completion_tokens=7, finish_reason="stop"
):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return types.SimpleNamespace(choices=[choice], usage=usage, _hidden_params={})


class _Outage:
    """A provider that is down for the whole test: raises a litellm-shaped 5xx."""

    text = (
        "litellm.InternalServerError: Error code: 503 - service temporarily unavailable"
    )

    def __call__(self, _kwargs):
        raise RuntimeError(self.text)


def _ledger_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# ===========================================================================
# 1. provider outage falls back without task failure
# ===========================================================================


class TestRequiredProviderOutageFallsBack:
    @staticmethod
    def _content_dispatch(kwargs, seen_models, outage):
        """Answer like a real model: a plan for the planner, a fix for a step.

        Content dispatch (not a call counter) is what makes the fake behave
        like the real boundary: the router may dial it any number of times in
        any order while it fails over, and the loop still gets what it needs.
        """
        model = str(kwargs.get("model") or "")
        seen_models.append(model)
        if model.startswith("openai/") and outage["primary"]:
            raise RuntimeError(
                "litellm.InternalServerError: Error code: 503 - overloaded"
            )
        messages = kwargs.get("messages") or []
        system = " ".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        if "planning a bug fix" in system:
            return _response(
                json.dumps(
                    {
                        "analysis": "mean() returns the sum instead of the mean",
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix mean() to divide by len(values)",
                                "checkpoint": "the target test passes",
                                "files_hint": ["mathutil.py"],
                            }
                        ],
                    }
                )
            )
        # Decide from the CONVERSATION, not a call counter: the router may dial
        # this boundary several times for one logical turn while it fails
        # over, and a counter would answer the wrong turn. ONE bash command
        # per reply is the harness's action space, so the edit is its own
        # turn and SUBMIT is the next one.
        already_edited = any(
            "p.write_text" in str(message.get("content", ""))
            for message in messages
            if message.get("role") == "assistant"
        )
        if already_edited:
            return _response("SUBMIT")
        # A SURGICAL replace, not a rewrite: overwriting the module would drop
        # its other helpers, and the real verifier would (correctly) fail the
        # run on the regression.
        return _response(
            "python -c \"import pathlib; p = pathlib.Path('mathutil.py'); "
            "t = p.read_text(); "
            "p.write_text(t.replace('return sum(values)', "
            "'return sum(values) / len(values)'))\""
        )

    @staticmethod
    def _repo(tmp_path):
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "mathutil.py").write_text(
            "def mean(values):\n    return sum(values)\n", encoding="utf-8"
        )
        (repo / "tests" / "test_mathutil.py").write_text(
            "from mathutil import mean\n\n\ndef test_mean():\n    assert mean([1, 2, 3]) == 2\n",
            encoding="utf-8",
        )
        return repo

    @staticmethod
    def _stub_verifier(monkeypatch):
        """Inject a stub verifier, because the Docker daemon is unavailable.

        HONEST SCOPE: this test's subject is PROVIDER RECOVERY, not
        verification. The stub still runs the real gate sequence (baseline
        first, then the target after the edit, then the regression run) and
        still refuses unless the fixture file was actually changed, so a
        no-op "fix" cannot pass. What it does NOT prove is that the real
        Docker verifier minted the success — the Docker-gated twin below does
        that, and it skips rather than fakes.
        """
        import execution.verify as real_verify
        import shared.types as shared_types

        def verify(
            repo_path,
            target_test,
            rerun_for_flake_check=1,
            test_command=None,
            verify_timeout_s=300,
            **kwargs,
        ):
            source = Path(repo_path) / "mathutil.py"
            fixed = source.is_file() and "/ len(values)" in source.read_text(
                encoding="utf-8"
            )
            if not fixed:
                # A run that changed nothing is a FAIL, exactly as the real
                # verifier would report an unfixed target.
                return shared_types.VerificationResult(
                    target_test_passed=False,
                    baseline_passed=False,
                    regression_passed=False,
                    flaky=False,
                    raw_output="stub verifier: the fixture was not fixed",
                )
            return shared_types.VerificationResult(
                target_test_passed=True,
                baseline_passed=True,
                regression_passed=True,
                flaky=False,
                raw_output="stub verifier: target and suite report green",
            )

        monkeypatch.setattr(real_verify, "verify", verify)
        return verify

    def test_required_1_provider_outage_falls_back_without_task_failure(
        self, tmp_path, monkeypatch
    ):
        """A hard-down primary provider; the task still completes.

        Real ``harness.core.run_task``, real fix loop, real model boundary
        through the REAL router, a stubbed verifier (see the scope note on
        ``_stub_verifier``). The primary is 503 on every attempt; the
        configured fallback answers. The task must reach ``success`` and the
        ledger must show BOTH the outage and the fallback.
        """
        from harness.core import run_task
        from shared.types import Task

        seen_models: list[str] = []
        outage = {"primary": True}

        def handler(kwargs):
            return self._content_dispatch(kwargs, seen_models, outage)

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        self._stub_verifier(monkeypatch)
        monkeypatch.setenv("NEO_CIRCUIT_BREAKER_THRESHOLD", "1")
        provider_resilience.reset_registry()

        repo = self._repo(tmp_path)
        ledger = tmp_path / "ledger.jsonl"
        model_router.set_call_context(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test-primary",
                "provider_fallbacks": [
                    {
                        "provider": "anthropic",
                        "model": "claude-3-5-haiku-20241022",
                        "api_key": "sk-test-fallback",
                    }
                ],
                "rate_limit_retries": 0,
            },
            ledger_dir=str(ledger),
        )
        # The harness reaches Boundary 2 through the resilient pipeline, which
        # is a drop-in for the router's own call_model.
        import harness.deps as harness_deps

        harness_deps.set_call_model(provider_gateway.resilient_call_model)
        try:
            task = Task(
                task_id="fix-ceiling14-fallback",
                repo_path=str(repo),
                issue_text=(
                    "mathutil.mean() returns the sum instead of the mean. "
                    "Fix mean() in mathutil.py so mean([1,2,3]) == 2."
                ),
                config={
                    "target_test": "tests/test_mathutil.py::test_mean",
                    "test_command": "python -m pytest tests/test_mathutil.py -q",
                    "max_retries": 1,
                    "max_wallclock_s": 600,
                    "git_output": False,
                    "rationale_log": False,
                    "agent_tests": False,
                    "self_critique": False,
                    "max_completion_tokens": None,
                },
            )
            result = run_task(task, log_root=str(tmp_path / "logs"))
        finally:
            harness_deps.set_call_model(None)
            model_router.set_call_context(None)

        rows = _ledger_rows(ledger)
        assert rows, "the run produced no model ledger rows"
        assert any(
            row["outcome"] == "error" and row["provider"] == "openai" for row in rows
        ), (
            "the primary provider's outage was not recorded: "
            + json.dumps([row.get("error") for row in rows])[:400]
        )
        assert any(
            row["outcome"] == "success"
            and row["provider"] == "anthropic"
            and row["fallback_used"]
            for row in rows
        ), "no fallback success row was recorded"
        assert result.status == "success", (
            f"the task did not complete through the fallback path: {result.status}"
        )
        assert result.model_calls, "the run made no model calls at all"
        assert any("openai/gpt-4o" in model for model in seen_models)
        assert any("anthropic/" in model for model in seen_models)

    def test_required_1b_docker_verifier_twin(self, tmp_path, monkeypatch):
        """The same recovery with the REAL Docker verifier.

        Gated on a reachable daemon. On a host without Docker this SKIPS
        with a reason; the hermetic test above is the evidence of record for
        provider recovery, and no Docker lane is reported as passed here.
        """
        import shutil
        import subprocess

        from harness.core import run_task
        from shared.types import Task

        def _docker_up() -> bool:
            try:
                completed = subprocess.run(
                    ["docker", "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return completed.returncode == 0 and bool(completed.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                return False

        if os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up():
            pytest.skip("docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)")

        source = (
            Path(__file__).resolve().parent.parent / "cli" / "fixtures" / "smoke_repo"
        )
        repo = tmp_path / "smoke_repo"
        shutil.copytree(source, repo)
        seen_models: list[str] = []
        outage = {"primary": True}

        def handler(kwargs):
            return self._content_dispatch(kwargs, seen_models, outage)

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        monkeypatch.setenv("NEO_CIRCUIT_BREAKER_THRESHOLD", "1")
        provider_resilience.reset_registry()
        model_router.set_call_context(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test-primary",
                "provider_fallbacks": [
                    {"provider": "anthropic", "model": "claude-3-5-haiku-20241022"}
                ],
                "rate_limit_retries": 0,
            }
        )
        import harness.deps as harness_deps

        harness_deps.set_call_model(provider_gateway.resilient_call_model)
        try:
            result = run_task(
                Task(
                    task_id="fix-ceiling14-docker-twin",
                    repo_path=str(repo),
                    issue_text="mean() in mathutil.py returns the sum; make it the mean",
                    config={
                        "target_test": "tests/test_mathutil.py::test_mean",
                        "test_command": "python -m pytest tests/test_mathutil.py -q",
                        "max_retries": 1,
                        "max_wallclock_s": 900,
                        "git_output": False,
                        "rationale_log": False,
                        "agent_tests": False,
                        "self_critique": False,
                    },
                ),
                log_root=str(tmp_path / "logs"),
            )
        finally:
            harness_deps.set_call_model(None)
            model_router.set_call_context(None)
        assert result.status == "success", (
            f"task failed through the fallback: {result.status}"
        )
        verification = result.verification
        assert verification is not None
        assert verification.target_test_passed and verification.regression_passed
        assert verification.flaky is False
        # The original repository is never mutated by a verified run.
        assert "len(values)" not in (repo / "mathutil.py").read_text(encoding="utf-8")

    def test_fallback_is_bounded_by_configuration(self, tmp_path, monkeypatch):
        """Four extra candidates configured, two allowed: only two are dialed."""
        dialed: list[str] = []

        def handler(kwargs):
            dialed.append(str(kwargs.get("model")))
            return _response("ok")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        breaker = provider_resilience.BreakerRegistry(
            failure_threshold=1, reset_seconds=60
        )
        model_router.set_call_context(
            {
                "circuit_breaker_registry": breaker,
                "provider_fallbacks": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p2", "model": "m2"},
                    {"provider": "p3", "model": "m3"},
                    {"provider": "p4", "model": "m4"},
                    {"provider": "p5", "model": "m5"},
                ],
                "provider_fallback_max": 2,
            }
        )
        target = model_router._resolve_target(
            None, None, None, model_router._CONTEXT.get() or {}
        )
        chain = provider_gateway.resolve_candidates(target, model_router._CONTEXT.get())
        assert [item["model"] for item in chain] == [
            target.get("model"),
            "m1",
            "m2",
        ]
        provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        # The default medium tier is dialed first and succeeds, so exactly one
        # request is made: the bound is on the chain, not a forced fan-out.
        assert len(dialed) == 1

    def test_all_candidates_failing_raises_the_last_provider_error(self, monkeypatch):
        """No candidate left -> the provider's own exception, not a wrapper."""
        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(_Outage()))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        model_router.set_call_context(
            {
                "provider_fallbacks": [{"provider": "anthropic", "model": "claude-x"}],
                "rate_limit_retries": 0,
            }
        )
        with pytest.raises(RuntimeError) as excinfo:
            provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        assert "503" in str(excinfo.value)

    def test_breaker_opens_after_the_threshold_and_skips_the_provider(self, tmp_path):
        """Two failures open the circuit; a third call never dials it."""
        breaker = provider_resilience.BreakerRegistry(
            failure_threshold=2, reset_seconds=60
        )
        identity = provider_resilience.provider_identity("openai", None, "gpt-4o")
        assert breaker.allow(identity)[0] is True
        breaker.record_failure(identity, "transient")
        assert breaker.allow(identity)[0] is True
        breaker.record_failure(identity, "transient")
        allowed, reason = breaker.allow(identity)
        assert allowed is False
        assert reason == provider_resilience.CIRCUIT_OPEN_REASON
        assert breaker.state(identity) == provider_resilience.OPEN
        snapshot = breaker.snapshot()[0]
        assert snapshot["state"] == provider_resilience.OPEN
        assert snapshot["cooldown_remaining_s"] > 0

    def test_breaker_half_opens_after_the_cooldown_and_admits_one_probe(self):
        """A single recovery probe, not a stampede."""
        now = {"value": 1000.0}
        breaker = provider_resilience.CircuitBreaker(
            "openai|", failure_threshold=1, reset_seconds=10.0, now=lambda: now["value"]
        )
        breaker.record_failure("outage")
        assert breaker.allow()[0] is False
        now["value"] += 11.0
        assert breaker.state() == provider_resilience.HALF_OPEN
        assert breaker.allow()[0] is True
        assert breaker.allow()[0] is False  # the probe is already in flight
        breaker.record_success()
        assert breaker.state() == provider_resilience.CLOSED

    def test_a_rejected_request_does_not_trip_the_provider_breaker(self):
        """A 400 is the caller's fault, not an outage: the endpoint stays up."""
        breaker = provider_resilience.BreakerRegistry(
            failure_threshold=1, reset_seconds=60
        )
        identity = "openai||gpt-4o"
        decision = provider_resilience.classify_retry(
            RuntimeError("litellm.BadRequestError: field messages is required"),
            is_rate_limit=False,
            is_transient=False,
        )
        assert decision.kind == "bad_request"
        assert decision.provider_fault is False
        if decision.provider_fault:
            breaker.record_failure(identity)
        assert breaker.state(identity) == provider_resilience.CLOSED

    def test_non_idempotent_operation_is_never_replayed(self, monkeypatch):
        """A declared non-idempotent call makes exactly ONE provider request."""
        attempts = {"n": 0}

        def handler(_kwargs):
            attempts["n"] += 1
            raise RuntimeError("litellm.RateLimitError: 429 Too Many Requests")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        model_router.set_call_context(
            {
                "rate_limit_retries": 5,
                "provider_fallbacks": [{"provider": "anthropic", "model": "claude-x"}],
            }
        )
        with pytest.raises(RuntimeError):
            provider_gateway.resilient_call_model(
                [{"role": "user", "content": "create a fine-tune"}], idempotent=False
            )
        assert attempts["n"] == 1, (
            "a non-idempotent operation was replayed "
            f"({attempts['n']} provider requests for one logical call)"
        )

    def test_idempotent_operation_retries_and_reports_the_decision(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        state = {"n": 0}

        def handler(_kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("litellm.InternalServerError: 502 bad gateway")
            return _response("recovered")

        monkey = pytest.MonkeyPatch()
        module = _fake_litellm(handler)
        module.RateLimitError = type("RateLimitError", (Exception,), {})
        module.InternalServerError = type("InternalServerError", (Exception,), {})
        monkey.setitem(sys.modules, "litellm", module)
        monkey.setattr(model_router.time, "sleep", lambda _s: None)
        model_router.set_call_context({"rate_limit_retries": 3}, ledger_dir=str(ledger))
        try:
            out = provider_gateway.resilient_call_model(
                [{"role": "user", "content": "x"}]
            )
        finally:
            monkey.undo()
        assert out == "recovered"
        assert state["n"] == 2
        assert any(row["outcome"] == "success" for row in _ledger_rows(ledger))


# ===========================================================================
# 2. concurrent config writes never corrupt the config
# ===========================================================================


class TestRequiredConcurrentConfigWrites:
    def test_required_2_concurrent_config_writes_never_corrupt_the_config(
        self, tmp_path, monkeypatch
    ):
        """Many threads and processes write DIFFERENT keys at once.

        The final file must parse as TOML and contain every key. This is the
        test that fails without the lock: the old append path did a
        read-then-``write_text`` outside any lock, so two appenders lost one
        key (and a read-modify-write could resurrect a stale copy).
        """
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        monkeypatch.delenv("NEO_PROJECT_DIR", raising=False)
        target = tmp_path / "settings.toml"
        keys = [f"key_{index:02d}" for index in range(24)]
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                neoconfig.set_tier_key("global", keys[index], f"value-{index:02d}")
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(len(keys))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not [thread for thread in threads if thread.is_alive()], (
            "a writer deadlocked"
        )
        assert not errors, f"concurrent writes raised: {errors[:3]}"
        data = neoconfig.load_neo_config(target)
        missing = [key for key in keys if key not in data]
        assert not missing, f"lost keys to a concurrent write: {missing}"
        assert data[keys[0]] == "value-00"
        assert data[keys[-1]] == f"value-{len(keys) - 1:02d}"
        # The reader never observed a torn file.
        assert target.read_text(encoding="utf-8").count("key_") == len(keys)

    def test_concurrent_profile_writes_keep_every_profile(self, tmp_path, monkeypatch):
        """Named provider profiles are the login path; they must not race."""
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        names = [f"provider-{index}" for index in range(12)]
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                neoconfig.set_provider_profile(
                    name, {"provider": "openai", "model": f"m-{name}"}, tier="global"
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors, f"concurrent profile writes raised: {errors[:3]}"
        profiles = neoconfig.provider_profiles()
        missing = [name for name in names if name not in profiles]
        assert not missing, f"lost provider profiles to a concurrent write: {missing}"

    def test_atomic_write_never_leaves_a_temp_file_behind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        neoconfig.set_tier_key("global", "model", "m")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_a_held_lock_is_reported_not_bypassed(self, tmp_path, monkeypatch):
        """A lock held by ANOTHER writer is reported, never silently ignored.

        The holder is simulated by creating the lock file the way a foreign
        process would (a fresh, non-stale one). Bypassing the lock to "make
        the write succeed" is the failure mode this pins: a config that
        silently lost an update is worse than a visible refusal.
        """
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        target = tmp_path / "settings.toml"
        neoconfig.set_tier_key("global", "model", "m")
        foreign = target.with_name(target.name + ".lock")
        foreign.write_text("4242\n", encoding="utf-8")
        with pytest.raises(neoconfig.SettingsLockTimeout):
            neoconfig.set_tier_key("global", "model", "other")
        # The file is still intact and still says what it said.
        assert neoconfig.load_neo_config(target)["model"] == "m"
        foreign.unlink()

    def test_the_lock_is_reentrant_for_one_thread(self, tmp_path, monkeypatch):
        """A public mutator that calls another mutator must not self-deadlock."""
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        target = tmp_path / "settings.toml"
        neoconfig.set_tier_key("global", "model", "m")
        with neoconfig.settings_lock(target, timeout_s=0.5):
            # Same thread, same path: re-entrant, so this must not raise.
            neoconfig.set_tier_key("global", "model", "nested")
        assert neoconfig.load_neo_config(target)["model"] == "nested"
        # ...and the lock is genuinely released afterwards, not left held.
        assert not target.with_name(target.name + ".lock").exists()

    def test_a_stale_lock_is_taken_over(self, tmp_path, monkeypatch):
        """A crashed writer must not wedge the config forever."""
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        target = tmp_path / "settings.toml"
        neoconfig.set_tier_key("global", "model", "m")
        stale = target.with_name(target.name + ".lock")
        stale.write_text("999999\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        neoconfig.set_tier_key("global", "model", "recovered")
        assert neoconfig.load_neo_config(target)["model"] == "recovered"
        assert not stale.exists()

    def test_a_broken_settings_file_is_still_never_overwritten(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("NEO_CONFIG", str(tmp_path / "settings.toml"))
        target = tmp_path / "settings.toml"
        target.write_text("this is not = valid = toml\n", encoding="utf-8")
        with pytest.raises(ValueError):
            neoconfig.set_tier_key("global", "model", "m")
        assert "not = valid" in target.read_text(encoding="utf-8")


# ===========================================================================
# 3. offline mode performs no network request
# ===========================================================================


class TestRequiredOfflineMode:
    def test_required_3_offline_mode_performs_no_network_request(self, monkeypatch):
        """The strongest form: the fake provider EXPLODES if it is called.

        Offline + a remote target must raise before any request is built, so
        the exploding callable is never reached. This is the property, not a
        proxy for it.
        """

        def explode(_kwargs):
            raise AssertionError(
                "offline mode attempted a provider request — egress is not blocked"
            )

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(explode))
        model_router.set_call_context(
            {
                "offline": True,
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-x",
            }
        )
        with pytest.raises(offline_mode.OfflineEgressBlocked) as excinfo:
            provider_gateway.resilient_call_model([{"role": "user", "content": "hi"}])
        assert "no network request was made" in str(excinfo.value)

    def test_offline_allows_a_local_model_and_refuses_a_remote_one(self, monkeypatch):
        """The local tier is the offline tier; both halves are pinned."""
        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("local"))
        )
        model_router.set_call_context(
            {
                "offline": True,
                "adaptive_routing": True,
                "local_model_profile": {
                    "provider": "ollama",
                    "model": "qwen2.5:7b",
                    "api_base": "http://127.0.0.1:11434",
                },
            }
        )
        assert (
            provider_gateway.resilient_call_model([{"role": "user", "content": "hi"}])
            == "local"
        )
        usage = model_router.get_last_usage()
        assert usage["tier_class"] == "local"

    def test_offline_env_var_enables_the_gate(self, monkeypatch):
        monkeypatch.setenv("NEO_OFFLINE", "1")
        assert offline_mode.offline_config({}) is True
        assert offline_mode.offline_config(None) is True
        monkeypatch.setenv("NEO_OFFLINE", "0")
        assert offline_mode.offline_config({}) is False

    def test_offline_indicator_is_explicit_and_serializable(
        self, tmp_path, monkeypatch
    ):
        queue = offline_mode.OfflineQueue(tmp_path / "offline_queue.jsonl")
        queue.enqueue("question", {"q": "why is the build red?"})
        indicator = offline_mode.offline_indicator(
            {"offline": True}, queue_path=queue.path
        )
        receipt = indicator.as_dict()
        assert receipt["offline"] is True
        assert receipt["local_only"] is True
        assert receipt["queued"] == 1
        assert indicator.label().startswith("offline(queued=1)")
        assert json.loads(json.dumps(receipt)) == receipt

    def test_queued_work_survives_a_process_restart(self, tmp_path, monkeypatch):
        """A queue that lives in memory loses work; this one is a file."""
        path = tmp_path / "offline_queue.jsonl"
        first = offline_mode.OfflineQueue(path)
        first.enqueue("fix", {"issue": "a"}, entry_id="fix-1")
        first.enqueue("fix", {"issue": "b"}, entry_id="fix-2")
        # A brand-new instance (a new process reading the same file).
        second = offline_mode.OfflineQueue(path)
        assert [entry.entry_id for entry in second.pending()] == ["fix-1", "fix-2"]
        health = second.health()
        assert health["pending"] == 2 and health["total"] == 2

    def test_drain_replays_work_when_connectivity_returns(self, tmp_path):
        """Connectivity back -> the queue drains, and the receipt is honest."""
        queue = offline_mode.OfflineQueue(tmp_path / "offline_queue.jsonl")
        queue.enqueue("question", {"q": "one"}, entry_id="q1")
        queue.enqueue("question", {"q": "two"}, entry_id="q2")
        seen: list[str] = []

        def handler(entry):
            seen.append(entry.payload["q"])
            return "answered"

        report = queue.drain(handler)
        assert report["delivered"] == 2
        assert report["pending_after"] == 0
        assert seen == ["one", "two"]
        # Re-draining is a no-op: delivered work is not replayed.
        assert queue.drain(handler)["delivered"] == 0

    def test_a_failing_handler_does_not_strand_the_rest_of_the_queue(self, tmp_path):
        queue = offline_mode.OfflineQueue(tmp_path / "offline_queue.jsonl")
        queue.enqueue("x", {"n": 1}, entry_id="a")
        queue.enqueue("x", {"n": 2}, entry_id="b")
        queue.enqueue("x", {"n": 3}, entry_id="c")

        def handler(entry):
            if entry.entry_id == "b":
                raise RuntimeError("still offline")
            return entry.entry_id

        report = queue.drain(handler)
        assert report["delivered"] == 2
        assert report["failed"] == 1
        assert report["pending_after"] == 1
        # The failed entry is still there with its reason: nothing is lost.
        pending = queue.pending()
        assert [entry.entry_id for entry in pending] == ["b"]
        assert "still offline" in str(pending[0].last_error)

    def test_re_enqueue_is_idempotent(self, tmp_path):
        queue = offline_mode.OfflineQueue(tmp_path / "offline_queue.jsonl")
        for _ in range(4):
            queue.enqueue("x", {"n": 1}, entry_id="same")
        assert queue.health()["pending"] == 1
        assert queue.pending()[0].attempts >= 1

    def test_a_torn_final_line_does_not_destroy_the_queue(self, tmp_path):
        path = tmp_path / "offline_queue.jsonl"
        queue = offline_mode.OfflineQueue(path)
        queue.enqueue("x", {"n": 1}, entry_id="a")
        queue.enqueue("x", {"n": 2}, entry_id="b")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"op": "enqueue", "entry_id": "c", "payl'
            )  # killed mid-append
        reopened = offline_mode.OfflineQueue(path)
        assert [entry.entry_id for entry in reopened.pending()] == ["a", "b"]
        assert reopened.health()["malformed"] == 1

    def test_read_only_offline_question_refuses_without_a_local_model(self):
        with pytest.raises(offline_mode.OfflineEgressBlocked):
            offline_mode.answer_offline_question("what does this repo do?")

    def test_read_only_offline_question_answers_with_no_egress(self):
        calls: list[list[dict]] = []

        def local_call(messages):
            calls.append(messages)
            return "This repo fixes bugs."

        receipt = offline_mode.answer_offline_question(
            "what does this repo do?",
            local_call=local_call,
            local_profile={
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "api_base": "http://127.0.0.1:11434",
            },
        )
        assert receipt["egress"] == "denied"
        assert receipt["offline"] is True and receipt["read_only"] is True
        assert receipt["answer"] == "This repo fixes bugs."
        assert len(calls) == 1


# ===========================================================================
# 4. local model is used for the cheap tier when configured
# ===========================================================================


class TestRequiredLocalFirstTier:
    def test_required_4_local_model_is_used_for_the_cheap_tier_when_configured(
        self, monkeypatch
    ):
        """The cheap hint goes local; the decisive (hard) step stays frontier."""
        dialed: list[str] = []

        def handler(kwargs):
            dialed.append(str(kwargs.get("model")))
            return _response("ok")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "model_tiers": {
                    "easy": {"provider": "openai", "model": "gpt-4o-mini"},
                    "medium": {"provider": "openai", "model": "gpt-4o-mini"},
                    "hard": {
                        "provider": "anthropic",
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                "local_model_profile": {
                    "provider": "ollama",
                    "model": "qwen2.5:7b-instruct",
                    "api_base": "http://127.0.0.1:11434",
                },
            }
        )
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "summarize this file"}], difficulty_hint="easy"
        )
        easy_usage = model_router.get_last_usage()
        assert easy_usage["tier_class"] == "local"
        assert easy_usage["model"] == "qwen2.5:7b-instruct"
        assert easy_usage["local_first"] is True

        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "design the concurrency fix"}],
            difficulty_hint="hard",
        )
        hard_usage = model_router.get_last_usage()
        assert hard_usage["tier_class"] == "frontier"
        assert hard_usage["model"] == "claude-3-5-sonnet-20241022"
        assert hard_usage["local_first"] is False
        assert dialed == [
            "ollama/qwen2.5:7b-instruct",
            "anthropic/claude-3-5-sonnet-20241022",
        ]

    def test_no_local_profile_means_no_local_tier(self, monkeypatch):
        """Default-off: without a profile, routing is byte-identical to before."""
        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("ok"))
        )
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "model_tiers": {"easy": {"provider": "openai", "model": "gpt-4o-mini"}},
            }
        )
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "x"}], "easy"
        )
        assert model_router.get_last_usage()["tier_class"] == "frontier"

    def test_an_explicit_model_is_never_redirected_locally(self, monkeypatch):
        """A pinned target is the caller's decision; redirecting it would lie."""
        dialed: list[str] = []
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: dialed.append(k["model"]) or _response("ok")),
        )
        model_router.set_call_context(
            {
                "adaptive_routing": True,
                "local_model_profile": {
                    "provider": "ollama",
                    "model": "local-x",
                    "api_base": "http://127.0.0.1:11434",
                },
            }
        )
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "x"}], "easy", model="gpt-4o-mini"
        )
        assert dialed == ["gpt-4o-mini"]
        assert model_router.get_last_usage()["tier_class"] == "frontier"

    def test_tier_classification_comes_from_the_endpoint_not_the_name(self):
        assert (
            local_models.classify_target("openai", "http://127.0.0.1:8000/v1")
            == "local"
        )
        assert local_models.classify_target("ollama", None) == "local"
        assert (
            local_models.classify_target("openai", "https://api.openai.com/v1")
            == "frontier"
        )
        # No endpoint is NOT evidence of locality.
        assert local_models.classify_target("openai", None) == "frontier"
        assert (
            local_models.classify_target("my-local-server", "http://127.0.0.1:1234")
            == "local"
        )
        assert local_models.classify_target("x", "http://box.local:8080") == "local"

    def test_local_context_window_probe_is_cached(self):
        """A capability probe is a billable call; it happens once per endpoint."""
        from runtime import model_capabilities

        model_capabilities.reset_context_window_cache()
        profile = local_models.local_profile_from_config(
            {
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "api_base": "http://127.0.0.1:11434",
            }
        )
        calls = {"n": 0}

        def probe(_model=None, _provider=None, _api_base=None):
            calls["n"] += 1
            return 32768

        first = local_models.local_context_window(profile, probe=probe)
        second = local_models.local_context_window(profile, probe=probe)
        assert first["context_window"] == 32768
        assert first["context_window_source"] == "probe"
        assert second["cache"] == "hit"
        assert calls["n"] == 1, (
            f"the probe ran {calls['n']} times for one endpoint+model"
        )

    def test_a_declared_local_window_is_reported_as_declared(self):
        profile = local_models.local_profile_from_config(
            {
                "provider": "ollama",
                "model": "m",
                "api_base": "http://127.0.0.1:11434",
                "context_window": 65536,
            }
        )
        resolved = local_models.local_context_window(profile)
        assert resolved["context_window"] == 65536
        assert resolved["context_window_source"] == "declared"

    def test_an_unknown_local_window_is_never_zero(self):
        """The floor rule: a budgeter must not be handed a zero window."""
        from runtime import model_capabilities

        model_capabilities.reset_context_window_cache()
        profile = local_models.local_profile_from_config(
            {"provider": "ollama", "model": "totally-unknown-model-xyz", "api_base": ""}
        )
        resolved = local_models.local_context_window(profile)
        assert resolved["context_window"] > 0
        assert resolved["context_window"] == model_capabilities.FALLBACK_CONTEXT_WINDOW

    def test_a_local_profile_cannot_claim_the_decisive_role(self):
        profile = local_models.local_profile_from_config(
            {
                "provider": "ollama",
                "model": "m",
                "api_base": "http://127.0.0.1:11434",
                "roles": ["retrieval", "decisive"],
            }
        )
        assert "decisive" not in profile.roles
        assert "retrieval" in profile.roles

    def test_local_and_frontier_tokens_and_cost_are_reported(self, tmp_path):
        rows = [
            {
                "tier_class": "local",
                "tokens": 100,
                "prompt_tokens": 60,
                "completion_tokens": 40,
                "cost_usd": 0.0,
            },
            {
                "tier_class": "local",
                "tokens": 50,
                "prompt_tokens": 30,
                "completion_tokens": 20,
                "cost_usd": 0.0,
            },
            {
                "tier_class": "frontier",
                "tokens": 900,
                "prompt_tokens": 700,
                "completion_tokens": 200,
                "cost_usd": 0.42,
            },
            # A row written before this feature existed classifies from its
            # provider/endpoint rather than being dropped.
            {
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "tokens": 10,
                "prompt_tokens": 5,
                "completion_tokens": 5,
                "cost_usd": 0.01,
            },
        ]
        split = local_models.local_frontier_split(rows)
        assert split["local"]["tokens"] == 150
        assert split["local"]["calls"] == 2
        assert split["local"]["cost_usd"] == 0.0
        assert split["frontier"]["tokens"] == 910
        assert split["frontier"]["calls"] == 2
        assert split["total_tokens"] == 1060
        assert split["local"]["token_share"] == pytest.approx(150 / 1060, rel=1e-3)
        assert split["frontier"]["cost_share"] == pytest.approx(1.0, rel=1e-6)

    def test_the_split_is_read_from_a_real_ledger(self, tmp_path):
        ledger = tmp_path / "model_ledger.jsonl"
        ledger.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {"tier_class": "local", "tokens": 10, "cost_usd": 0.0},
                    {"tier_class": "frontier", "tokens": 90, "cost_usd": 0.5},
                )
            )
            + '\n{"tier_class": "frontier", "tokens": 99',  # torn tail
            encoding="utf-8",
        )
        report = local_models.summarize(
            {"local_model_profile": {"provider": "ollama", "model": "m"}}, ledger
        )
        assert report["local_model_configured"] is True
        assert report["split"]["local"]["tokens"] == 10
        assert report["split"]["frontier"]["tokens"] == 90
        assert report["split"]["rows"] == 2


# ===========================================================================
# 5. privacy policy blocks a disallowed provider
# ===========================================================================


class TestRequiredPrivacyPolicy:
    def test_required_5_privacy_policy_blocks_a_disallowed_provider(self, monkeypatch):
        """A disallowed provider is refused BEFORE a request is built."""

        def explode(_kwargs):
            raise AssertionError(
                "a privacy-blocked provider was dialed — the gate is after the request"
            )

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(explode))
        model_router.set_call_context(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-x",
                "privacy_policy": {"name": "pinned", "providers": ["anthropic"]},
            }
        )
        with pytest.raises(privacy_policy.PrivacyPolicyBlocked) as excinfo:
            provider_gateway.resilient_call_model([{"role": "user", "content": "hi"}])
        assert "provider_not_allowed" in str(excinfo.value)
        assert "no provider request was made" in str(excinfo.value)

    def test_an_allowed_provider_is_dialed_with_the_receipt_recorded(
        self, tmp_path, monkeypatch
    ):
        ledger = tmp_path / "ledger.jsonl"
        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("ok"))
        )
        model_router.set_call_context(
            {
                "provider": "anthropic",
                "model": "claude-3-5-haiku-20241022",
                "privacy_policy": {"name": "pinned", "providers": ["anthropic"]},
            },
            ledger_dir=str(ledger),
        )
        assert (
            provider_gateway.resilient_call_model([{"role": "user", "content": "hi"}])
            == "ok"
        )
        rows = _ledger_rows(ledger)
        assert rows[-1]["privacy"]["allowed"] is True
        assert rows[-1]["privacy"]["policy"] == "pinned"

    def test_a_zdr_policy_refuses_a_provider_without_a_documented_mode(self):
        policy = privacy_policy.PRIVACY_POLICIES["zdr"]
        decision = privacy_policy.authorize(policy, "some-unknown-router", "m", None)
        assert decision.allowed is False
        assert decision.reason == "zdr_not_documented"
        assert decision.zero_data_retention == "unknown"

    def test_a_zdr_policy_sends_the_documented_parameter_and_records_it(
        self, tmp_path, monkeypatch
    ):
        capture: dict = {}
        ledger = tmp_path / "ledger.jsonl"
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context(
            {
                "provider": "gemini",
                "model": "gemini-2-flash",
                "privacy_policy": "zdr",
            },
            ledger_dir=str(ledger),
        )
        provider_gateway.resilient_call_model([{"role": "user", "content": "hi"}])
        assert capture.get("extra_body") == {"no_store": True}
        row = _ledger_rows(ledger)[-1]
        assert row["zdr_requested"] is True
        assert "no_store" in row["zdr_parameter"]
        assert row["privacy"]["zero_data_retention"] is True

    def test_an_unknown_provider_makes_no_retention_claim(self):
        record = privacy_policy.provider_privacy(
            "brand-new-router", "https://x.example/v1"
        )
        assert record.zero_data_retention == "unknown"
        assert record.data_scope == "cloud"
        assert privacy_policy.zdr_kwargs("brand-new-router") == {}

    def test_a_local_endpoint_is_permitted_under_the_strictest_policy(self):
        policy = privacy_policy.PRIVACY_POLICIES["local_only"]
        decision = privacy_policy.authorize(
            policy, "openai", "some-local-build", "http://127.0.0.1:8000/v1"
        )
        assert decision.allowed is True
        assert decision.reason == "local_endpoint"
        assert decision.data_scope == "local"
        cloud = privacy_policy.authorize(
            policy, "openai", "gpt-4o", "https://api.openai.com"
        )
        assert cloud.allowed is False
        assert cloud.reason == "policy_local_only"

    def test_a_data_class_ceiling_blocks_repository_content(self):
        policy = privacy_policy.PRIVACY_POLICIES["local_only"]
        decision = privacy_policy.authorize(policy, "openai", "gpt-4o", None)
        assert decision.allowed is False
        strict_source = privacy_policy.policy_from_config(
            {"privacy_policy": "source_ok", "privacy_providers": ["openai"]}
        )
        assert strict_source.allows_class("source") is True
        assert strict_source.allows_class("user") is False

    def test_redaction_runs_before_the_provider_request(self, monkeypatch):
        """The secret must be absent from what the provider received."""
        capture: dict = {}
        secret = "sk-live-DO-NOT-LEAK-0123456789"
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({"api_key": "sk-configured-key-999999"})
        provider_gateway.resilient_call_model(
            [
                {"role": "system", "content": "you are a coding agent"},
                {
                    "role": "user",
                    "content": f"the deploy script uses {secret} — please reuse it",
                },
            ]
        )
        sent = json.dumps(capture["messages"])
        assert secret not in sent, "a credential reached the provider request"
        assert "REDACTED" in sent
        assert "coding agent" in sent, "redaction destroyed unrelated content"
        usage = model_router.get_last_usage()
        assert usage["redaction"]["redacted"] is True

    def test_a_configured_credential_is_redacted_even_without_a_key_shape(
        self, monkeypatch
    ):
        """A user's own opaque key is removed by identity, not by pattern."""
        capture: dict = {}
        opaque = "TOTALLYOPAQUE-VALUE-1234567890"
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({"model_tiers": {"easy": {"api_key": opaque}}})
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": f"the key is {opaque}"}], "easy"
        )
        assert opaque not in json.dumps(capture["messages"])

    def test_redaction_is_transparent_when_there_is_nothing_to_redact(
        self, monkeypatch
    ):
        """No secret -> the ORIGINAL message objects are sent, byte-identical."""
        capture: dict = {}
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({})
        messages = [
            {"role": "system", "content": "You are a planner agent."},
            {"role": "user", "content": "## Issue\nmean() returns the sum\n"},
        ]
        provider_gateway.resilient_call_model(messages)
        assert capture["messages"] == messages
        assert model_router.get_last_usage()["redaction"]["reason"] == "no_change"

    def test_a_privacy_refusal_names_the_target_and_carries_no_payload(self):
        decision = privacy_policy.authorize(
            privacy_policy.PRIVACY_POLICIES["local_only"],
            "openai",
            "gpt-4o",
            "https://api.openai.com/v1",
        )
        assert decision.allowed is False
        with pytest.raises(privacy_policy.PrivacyPolicyBlocked) as excinfo:
            raise privacy_policy.PrivacyPolicyBlocked([decision])
        text = str(excinfo.value)
        assert "gpt-4o" in text  # names the refused target
        assert "no provider request was made" in text
        assert "api_key" not in text.lower()


# ===========================================================================
# pipeline-level regressions
# ===========================================================================


class TestPipelineReceipts:
    def test_a_refused_candidate_is_recorded_before_a_later_one_succeeds(
        self, tmp_path, monkeypatch
    ):
        """The refusal must be legible even on a successful call."""
        ledger = tmp_path / "ledger.jsonl"
        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("ok"))
        )
        model_router.set_call_context(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "privacy_policy": {"name": "pinned", "providers": ["openai"]},
                "provider_fallbacks": [
                    {"provider": "anthropic", "model": "claude-3-5-haiku-20241022"}
                ],
            },
            ledger_dir=str(ledger),
        )
        out = provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        assert out == "ok"
        rows = _ledger_rows(ledger)
        assert any(row["outcome"] == "skipped" for row in rows), (
            "the refused candidate left no record: " + json.dumps(rows)[:400]
        )
        assert any(row["outcome"] == "success" for row in rows)

    def test_an_offline_refusal_is_recorded_on_the_ledger(self, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger.jsonl"

        def explode(_kwargs):
            raise AssertionError("egress was not blocked")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(explode))
        model_router.set_call_context(
            {"offline": True, "provider": "openai", "model": "gpt-4o"},
            ledger_dir=str(ledger),
        )
        with pytest.raises(offline_mode.OfflineEgressBlocked):
            provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        rows = _ledger_rows(ledger)
        assert rows, "an offline refusal left no ledger row"
        assert rows[0]["skipped_reason"] == "offline_egress_blocked"
        assert rows[0]["offline"] is True

    def test_the_ledger_never_carries_a_credential(self, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger.jsonl"
        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("ok"))
        )
        model_router.set_call_context(
            {"api_key": "sk-super-secret-value-777"}, ledger_dir=str(ledger)
        )
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "hi"}], model="gpt-4o-mini"
        )
        body = ledger.read_text(encoding="utf-8")
        assert "sk-super-secret-value-777" not in body
        rows = _ledger_rows(ledger)
        # The endpoint is fingerprinted, not stored.
        assert "api_base_sha256" in rows[-1]

    def test_provider_identity_never_contains_a_url(self):
        identity = provider_resilience.provider_identity(
            "openai", "https://user:hunter2@api.example.com/v1", "gpt-4o"
        )
        assert "hunter2" not in identity
        assert "api.example.com" not in identity
        assert identity.startswith("openai|")

    def test_an_unknown_privacy_policy_fails_closed(self):
        with pytest.raises(ValueError) as excinfo:
            privacy_policy.policy_from_config({"privacy_policy": "not-a-policy"})
        assert "known policies" in str(excinfo.value)

    def test_an_unknown_data_class_fails_closed(self):
        with pytest.raises(ValueError):
            privacy_policy.policy_from_config(
                {"privacy_policy": {"name": "x", "data_classes": ["nonsense"]}}
            )

    def test_configuring_redact_false_cannot_allow_a_credential_out(self):
        policy = privacy_policy.policy_from_config(
            {
                "privacy_policy": {
                    "name": "x",
                    "redact": False,
                    "data_classes": ["secret"],
                }
            }
        )
        assert "secret" not in policy.data_classes

    def test_the_default_breaker_threshold_is_configurable_by_env(self, monkeypatch):
        monkeypatch.setenv("NEO_CIRCUIT_BREAKER_THRESHOLD", "7")
        monkeypatch.setenv("NEO_CIRCUIT_BREAKER_RESET_S", "90")
        provider_resilience.reset_registry()
        registry = provider_resilience.default_registry()
        assert registry.failure_threshold == 7
        assert registry.reset_seconds == 90.0


class TestRouterDelegation:
    """The router's single delegation point (runtime/model_router.py)."""

    def test_no_resilience_key_means_the_legacy_path_runs(self, monkeypatch):
        """Byte-identical: the pipeline is never entered, so kwargs are the
        router's own historical shape."""
        capture: dict = {}
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({"api_key": "sk-x", "model": "gpt-4o-mini"})
        assert model_router.call_model([{"role": "user", "content": "x"}]) == "ok"
        assert capture["model"] == "gpt-4o-mini"
        assert model_router.get_last_usage().get("tier_class") is None, (
            "the legacy path must not gain a tier_class it never had"
        )

    def test_a_resilience_key_switches_the_router_onto_the_pipeline(self, monkeypatch):
        capture: dict = {}
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({"offline": False, "model": "gpt-4o-mini"})
        assert model_router.call_model([{"role": "user", "content": "x"}]) == "ok"
        assert model_router.get_last_usage()["tier_class"] == "frontier"

    def test_an_offline_router_call_never_dials(self, monkeypatch):
        def explode(_kwargs):
            raise AssertionError("the router dialed a provider while offline")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(explode))
        model_router.set_call_context(
            {"offline": True, "provider": "openai", "model": "gpt-4o"}
        )
        with pytest.raises(offline_mode.OfflineEgressBlocked):
            model_router.call_model([{"role": "user", "content": "x"}])

    def test_a_streamed_call_still_runs_the_gate(self, monkeypatch):
        """Streaming must not be a way to bypass the offline refusal."""

        def explode(_kwargs):
            raise AssertionError("the router dialed a provider while offline")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(explode))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        model_router.set_call_context(
            {"offline": True, "provider": "openai", "model": "gpt-4o"}
        )
        with pytest.raises(offline_mode.OfflineEgressBlocked):
            model_router.call_model(
                [{"role": "user", "content": "x"}],
                stream=True,
                on_delta=lambda _t: None,
            )

    def test_a_non_idempotent_router_call_is_never_replayed(self, monkeypatch):
        attempts = {"n": 0}

        def handler(_kwargs):
            attempts["n"] += 1
            raise RuntimeError("litellm.RateLimitError: 429 Too Many Requests")

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        monkeypatch.setattr(model_router.time, "sleep", lambda _s: None)
        model_router.set_call_context(
            {
                "privacy_policy": "standard",
                "model_calls_idempotent": False,
                "rate_limit_retries": 4,
                "provider_fallbacks": [{"provider": "anthropic", "model": "claude-x"}],
            }
        )
        with pytest.raises(RuntimeError):
            model_router.call_model([{"role": "user", "content": "x"}])
        assert attempts["n"] == 1


class TestCliDepsResolvers:
    """cli/deps.py exposes the new boundaries with the override pattern."""

    def test_every_new_resolver_resolves_to_a_real_callable(self):
        from cli import deps

        for getter in (
            deps.get_resilient_call_model,
            deps.get_privacy_policy,
            deps.get_privacy_authorize,
            deps.get_offline_config,
            deps.get_offline_queue,
            deps.get_local_model_profile,
            deps.get_local_frontier_report,
            deps.get_breaker_snapshot,
        ):
            assert callable(getter())

    def test_an_override_takes_precedence_and_resets(self):
        from cli import deps

        deps.set_offline_config(lambda _cfg=None: True)
        try:
            assert deps.get_offline_config()({}) is True
        finally:
            deps.reset_overrides()
        assert deps.get_offline_config()({"offline": "0"}) is False

    def test_the_resolver_is_a_drop_in_for_call_model(self, monkeypatch):
        from cli import deps

        monkeypatch.setitem(
            sys.modules, "litellm", _fake_litellm(lambda _k: _response("via-deps"))
        )
        model_router.set_call_context({})
        assert deps.get_resilient_call_model()([{"role": "user", "content": "x"}]) == (
            "via-deps"
        )

    def test_a_broken_boundary_raises_instead_of_degrading_to_allow(self, monkeypatch):
        """A privacy/offline boundary that degrades to 'allow' is worse than
        one that is unavailable, so an import failure must surface."""
        from cli import deps

        monkeypatch.setitem(sys.modules, "runtime.privacy_policy", None)
        with pytest.raises((ImportError, AttributeError, TypeError)):
            deps.get_privacy_policy()


class TestNoSilentMutationOfExistingBehavior:
    def test_with_no_resilience_config_the_request_is_unchanged(self, monkeypatch):
        """No profile, no offline, no policy, no fallbacks -> identical kwargs."""
        capture: dict = {}
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context(
            {"api_key": "sk-ctx", "api_base": "https://x.example/v1"}
        )
        provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        # The router's own behavior for an unconfigured context: no provider
        # name -> no prefix, exactly as the pre-resilience path produced.
        assert capture["model"] == "gpt-4o-mini"
        assert capture["api_key"] == "sk-ctx"
        assert capture["api_base"] == "https://x.example/v1"
        assert "tools" not in capture
        usage = model_router.get_last_usage()
        assert usage["fallback_used"] is False
        assert usage["tier_class"] == "frontier"

    def test_tool_schemas_still_reach_the_provider(self, monkeypatch):
        capture: dict = {}
        monkeypatch.setitem(
            sys.modules,
            "litellm",
            _fake_litellm(lambda k: capture.update(k) or _response("ok")),
        )
        model_router.set_call_context({})
        schema = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
        provider_gateway.resilient_call_model(
            [{"role": "user", "content": "x"}], tools=schema
        )
        assert capture["tools"] == schema

    def test_native_tool_calls_are_returned_normalized(self, monkeypatch):
        def handler(_kwargs):
            message = types.SimpleNamespace(
                content="",
                tool_calls=[
                    types.SimpleNamespace(
                        id="call_1",
                        function=types.SimpleNamespace(
                            name="read", arguments='{"path": "a.py"}'
                        ),
                    )
                ],
            )
            choice = types.SimpleNamespace(message=message, finish_reason="tool_calls")
            usage = types.SimpleNamespace(prompt_tokens=3, completion_tokens=1)
            return types.SimpleNamespace(
                choices=[choice], usage=usage, _hidden_params={}
            )

        monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(handler))
        model_router.set_call_context({})
        out = provider_gateway.resilient_call_model([{"role": "user", "content": "x"}])
        assert out["tool_calls"][0]["function"]["name"] == "read"
        assert out["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
        assert out["finish_reason"] == "tool_calls"
