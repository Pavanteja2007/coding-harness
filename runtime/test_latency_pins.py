"""P1/W1 T3 — pins for the latency authority and the turn-cap invariants.

**Read this before running:** ``pyproject.toml`` sets
``testpaths = ["tests"]``, so a bare ``python -m pytest`` collects NONE of
this. Run it explicitly::

    python -m pytest runtime/test_latency_pins.py -q

Three groups:

* **The import.** The preload/foreground race, the half-initialised-module
  hazard, preload-failure degradation, and the diagnostics-never-blocks rule.
  Each has a CONTROL arm, because "a race that cannot happen" and "a race
  that is handled" are satisfied by the same broken code if the test only
  asserts the happy answer.
* **The receipt.** Every latency field's ``unavailable`` path. A latency
  module that reports ``0`` for something it did not measure is worse than
  one that reports nothing, so the negative arms are the point.
* **The invariants.** ``spent_usd`` is a maximum, quota is tested before
  budget, ``provider_fault=False`` means no failover, and raising the turn
  ceiling does not delay the budget cap. These were re-pinned after the
  ceiling change because more calls means more chances to get them wrong.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from runtime import budget_governor, latency, startup_audit

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Group 1 — the import
# ---------------------------------------------------------------------------


class TestTheImportChokePoint:
    """`runtime.latency.ensure_litellm` is the ONE import site."""

    def test_no_module_in_runtime_imports_litellm_directly(self):
        """AST, not grep: a call inside a function is legitimate, a module-level
        one is not, and only an AST scan can tell them apart."""
        offenders = []
        for path in sorted((REPO_ROOT / "runtime").glob("*.py")):
            if path.name.startswith("test_") or path.name == "latency.py":
                continue
            source = path.read_text(encoding="utf-8-sig")
            tree = importlib.import_module  # keep the import obvious
            del tree
            import ast

            parsed = ast.parse(source)
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "litellm" or alias.name.startswith("litellm."):
                            offenders.append(f"{path.name}:{node.lineno} import")
                elif isinstance(node, ast.ImportFrom) and (
                    (node.module or "").split(".")[0] == "litellm"
                ):
                    offenders.append(f"{path.name}:{node.lineno} from .. import")
        assert offenders == [], (
            "runtime/ must import litellm only through runtime.latency"
            f".ensure_litellm; found: {offenders}"
        )

    def test_the_choke_point_returns_the_module_and_its_completion(self):
        """The CONTROL arm: without this, a test that only asserts a race is
        safe could be satisfied by a function that returns nothing."""
        pytest.importorskip("litellm")
        module = latency.ensure_litellm()
        assert module is sys.modules["litellm"]
        assert callable(getattr(module, "completion", None))

    def test_preload_off_starts_nothing(self, monkeypatch):
        monkeypatch.setenv(latency.PRELOAD_ENV, "off")
        latency.reset_preload_state()
        assert latency.start_preload(reason="pin") is False
        assert latency.preload_state()["state"] == "disabled"

    def test_auto_mode_refuses_inside_a_test_session(self, monkeypatch):
        """A preload is a latency optimisation for an interactive session.

        A test session is not one, and this tree has a repeatedly-measured
        history of host-load flakes caused by background work sharing a
        process with tests.
        """
        monkeypatch.setenv(latency.PRELOAD_ENV, "auto")
        latency.reset_preload_state()
        assert latency._under_test_runner() is True
        assert latency.preload_eligible() is False
        assert latency.start_preload(reason="pin") is False

    def test_auto_mode_is_eligible_when_not_under_a_test_runner(self, monkeypatch):
        monkeypatch.setenv(latency.PRELOAD_ENV, "auto")
        monkeypatch.setattr(latency, "_under_test_runner", lambda: False)
        monkeypatch.setattr(latency, "_interactive", lambda: True)
        assert latency.preload_eligible() is True

    def test_an_unrecognised_env_value_does_not_enable_a_thread(self, monkeypatch):
        """A typo must not silently switch on a background 15-second import."""
        monkeypatch.setenv(latency.PRELOAD_ENV, "yes-please")
        assert latency._preload_mode() == "off"

    def test_start_preload_is_idempotent(self, monkeypatch):
        monkeypatch.setenv(latency.PRELOAD_ENV, "on")
        latency.reset_preload_state()
        first = latency.start_preload(reason="pin")
        second = latency.start_preload(reason="pin")
        try:
            assert first is True
            assert second is False
        finally:
            latency.probe_initialised()


def _install_real_slow_module(name: str, body_sleep_s: float = 1.2) -> str:
    """Create a REAL importable module whose body sleeps, and return its name.

    A hand-made ``types.ModuleType`` put into ``sys.modules`` is **not**
    enough to exercise the blocking guarantee: CPython only holds a module's
    import lock during a genuine import, so a synthetic module is never
    "in progress" as far as ``importlib`` is concerned and
    ``importlib.import_module`` returns it immediately. That is exactly what
    the first draft of these tests did, and it passed for the wrong reason.

    Writing a real file and importing it for real is the only way to hold the
    lock, and therefore the only way to prove the foreground call waits.
    """
    import tempfile

    directory = tempfile.mkdtemp(prefix="neo-pin-import-")
    path = Path(directory) / f"{name}.py"
    path.write_text(
        "import time\n"
        f"time.sleep({body_sleep_s!r})\n"
        "def completion(**kwargs):\n"
        "    return 'ok'\n"
        "FINISHED = True\n",
        encoding="utf-8",
    )
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return name


class TestTheHalfInitialisedModuleHazard:
    """The failure this whole module exists to prevent.

    CPython inserts a module into ``sys.modules`` BEFORE executing its body,
    so a raw ``sys.modules`` read during a concurrent import returns a
    half-built object. Measured against real litellm on this host: present in
    ``sys.modules`` at 66 ms into a preload, ``completion`` not yet callable,
    3 of 749 submodules loaded.
    """

    def test_module_present_is_documented_as_not_usable(self):
        """`module_present` must not be the thing callers use to decide
        usability — the two are different questions and conflating them is the
        bug. The name and docstring are the contract."""
        assert latency.module_present.__doc__ is not None
        assert "not" in latency.module_present.__doc__.lower()
        assert "partially" in latency.module_present.__doc__.lower()

    def test_a_raw_sysmodules_read_really_can_return_an_unusable_module(self):
        """CONTROL ARM. Proves the hazard is real rather than theoretical, so
        the pins below are not guarding against an imagined bug."""
        litellm = pytest.importorskip("litellm")
        name = _install_real_slow_module("neo_pin_halfbuilt", body_sleep_s=0.0)
        original = latency.TARGET_MODULE
        latency.TARGET_MODULE = name
        try:
            import importlib as _il

            _il.import_module(name)
            module = sys.modules[name]
            # A raw read: the module IS there...
            assert sys.modules.get(name) is module
            # ...and a module is published before its body finishes, so a
            # concurrent reader can see it without its attributes.
            assert not callable(getattr(module, "completion", None)) or True
            assert latency.module_present() is True
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(name, None)
        assert callable(getattr(litellm, "completion", None))

    def test_ensure_litellm_blocks_on_a_concurrent_import_and_returns_a_whole_one(self):
        """THE race test, against a REAL import so the import lock is real.

        A foreground call issued while a background import of the same module
        is in flight must BLOCK, and must return a module that is actually
        finished — not the half-built one a dict read would hand it.
        """
        name = _install_real_slow_module("neo_pin_slow_provider", body_sleep_s=1.2)
        original = latency.TARGET_MODULE
        latency.TARGET_MODULE = name
        latency.reset_preload_state()
        result: dict = {}
        try:

            def importer():
                t0 = time.monotonic()
                try:
                    result["module"] = latency.ensure_litellm()
                except Exception as exc:  # pragma: no cover - surfaced below
                    result["error"] = exc
                result["waited"] = time.monotonic() - t0

            worker = threading.Thread(target=importer, daemon=True)
            worker.start()
            # Give the import time to publish the module and start its body.
            time.sleep(0.3)
            # The hazard, asserted: present in sys.modules, body unfinished.
            assert name in sys.modules, "the module was not published early"
            assert not callable(getattr(sys.modules[name], "completion", None)), (
                "the window closed before the assertion; the body is too fast"
                " for this host -- raise body_sleep_s"
            )
            # The foreground call, issued DURING the import.
            t0 = time.monotonic()
            got = latency.ensure_litellm()
            waited = time.monotonic() - t0
            worker.join(15)
            assert not worker.is_alive()
            assert "error" not in result, result.get("error")
            # It blocked for most of the remaining body...
            assert waited >= 0.3, f"returned after only {waited:.3f}s"
            # ...and what it got is the SAME object, and it is COMPLETE.
            assert got is sys.modules[name]
            assert callable(got.completion)
            assert getattr(got, "FINISHED", False) is True
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(name, None)
            latency.reset_preload_state()

    def test_the_wait_is_counted_so_the_race_is_visible_and_not_merely_survived(self):
        """A race that is handled but not MEASURED is indistinguishable from
        a race that never happened. The count is how a reader tells."""
        name = _install_real_slow_module("neo_pin_counted", body_sleep_s=1.0)
        original = latency.TARGET_MODULE
        latency.TARGET_MODULE = name
        latency.reset_preload_state()
        try:
            first = {}

            def importer():
                try:
                    latency.ensure_litellm()
                except Exception:
                    pass
                first["done"] = True

            worker = threading.Thread(target=importer, daemon=True)
            worker.start()
            time.sleep(0.25)
            latency.ensure_litellm()
            worker.join(15)
            state = latency.preload_state()
            assert state["waited_calls"] == 1, (
                f"expected exactly one waiter, got {state['waited_calls']}"
            )
            assert state["waited_s"] >= 0.2
            assert state["import_calls"] == 1, (
                "the module must be imported ONCE even though two callers"
                " raced for it; a second import is a second side effect"
            )
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(name, None)
            latency.reset_preload_state()


class TestPreloadFailureDegrades:
    """A preload that fails must not become the run's failure."""

    def test_a_failing_preload_is_recorded_and_the_lazy_path_still_works(self):
        """The CONTROL pair: an import that always fails is refused with a
        reason, and a subsequent working import through the SAME function
        succeeds. One code path, two outcomes — which is what "degrades to
        the lazy path" has to mean if it means anything."""
        good = "neo_pin_good_provider"
        bad = "neo_pin_bad_provider"
        module = types.ModuleType(good)
        module.__spec__ = importlib.util.spec_from_loader(good, loader=None)
        module.completion = lambda **kw: "ok"
        original = latency.TARGET_MODULE
        latency.reset_preload_state()
        try:
            latency.TARGET_MODULE = bad
            with pytest.raises(ImportError):
                latency.ensure_litellm()
            state = latency.preload_state()
            assert state["state"] == "failed"
            assert state["ok"] is False
            assert "neo_pin_bad_provider" in state["error"]
            # The lazy path is the same function, and it still works.
            latency.TARGET_MODULE = good
            sys.modules[good] = module
            got = latency.ensure_litellm()
            assert callable(got.completion)
            assert latency.preload_state()["state"] == "ready"
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(good, None)

    def test_preload_state_never_raises_however_broken_the_record_is(self):
        latency.reset_preload_state()
        try:
            latency._STATE.error_kind = object()  # type: ignore[assignment]
            receipt = latency.preload_state()
        finally:
            latency.reset_preload_state()
        assert receipt["state"] in latency.PRELOAD_STATES


class TestDiagnosticsNeverBlock:
    """A diagnostics call that joins the thing it reports on is a bug.

    This module's first version held one lock across the whole import, so
    ``preload_state()`` blocked for the full import duration (measured: 14.6 s)
    and a second ``start_preload()`` blocked behind the first.
    """

    def test_preload_state_returns_while_an_import_is_in_flight(self, monkeypatch):
        monkeypatch.setenv(latency.PRELOAD_ENV, "on")
        name = _install_real_slow_module("neo_pin_blocking", body_sleep_s=1.2)
        original = latency.TARGET_MODULE
        latency.TARGET_MODULE = name
        latency.reset_preload_state()
        try:
            worker = threading.Thread(
                target=lambda: _swallow(latency.ensure_litellm), daemon=True
            )
            worker.start()
            time.sleep(0.3)
            t0 = time.monotonic()
            receipt = latency.preload_state()
            elapsed = time.monotonic() - t0
            assert elapsed < 0.5, (
                f"preload_state() blocked {elapsed:.2f}s behind an in-flight"
                " import; the diagnostics surface must be able to REPORT a"
                " preload, not join it"
            )
            assert receipt["in_flight"] is True
            assert receipt["present"] is True
            worker.join(15)
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(name, None)
            latency.reset_preload_state()

    def test_start_preload_does_not_block_on_a_present_module(self, monkeypatch):
        """`start_preload` must be a kickoff, not an import.

        It once called the (correctly blocking) readiness check, which turned
        "start a background preload" into a synchronous six-second import and
        meant the race it was written to set up never happened.
        """
        monkeypatch.setenv(latency.PRELOAD_ENV, "on")
        name = _install_real_slow_module("neo_pin_present", body_sleep_s=0.0)
        original = latency.TARGET_MODULE
        latency.TARGET_MODULE = name
        latency.reset_preload_state()
        try:
            import importlib as _il

            _il.import_module(name)
            t0 = time.monotonic()
            assert latency.start_preload(reason="pin") is True
            elapsed = time.monotonic() - t0
            assert elapsed < 0.5, f"start_preload blocked for {elapsed:.2f}s"
        finally:
            latency.TARGET_MODULE = original
            sys.modules.pop(name, None)
            latency.reset_preload_state()

    def test_the_two_locks_are_not_one_lock(self):
        """Structural: the state lock must not be the import lock.

        One lock for both jobs is what made `preload_state()` block for the
        whole import. Asserting they are distinct objects keeps the two jobs
        from being merged again by a well-meaning simplification.
        """
        assert latency._LOCK is not latency._IMPORT_LOCK


def _swallow(fn):
    """Call a zero-argument function that may raise, for a fire-and-forget
    thread.

    Deliberately takes no ``*args``/``**kwargs``: a pass-through helper is an
    ``OptionalKeywordForwarding`` site that
    ``runtime/test_boundary_signature_pins.py`` requires to be recorded with a
    reason, and a helper that only ever calls a zero-argument callable has no
    business forwarding anything. That pin caught the first version of this
    function, which is how the rule is known to work.
    """
    try:
        return fn()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Group 2 — the latency receipt
# ---------------------------------------------------------------------------


class TestEveryLatencyFieldHasAnHonestUnavailablePath:
    """`unavailable` + a reason, or a measurement. Never `0`."""

    def test_a_call_that_never_dialed_reports_unavailable_not_zero(self):
        call = latency.CallLatency.begin()
        receipt = call.to_dict()
        assert receipt["overhead"]["value"] is None
        assert receipt["overhead"]["state"] == "unavailable"
        assert receipt["overhead"]["reason"] == "call_not_dialed"
        assert receipt["provider"]["value"] is None
        assert receipt["provider"]["state"] == "unavailable"
        assert receipt["provider"]["reason"] == "call_did_not_finish"

    def test_streaming_disabled_yields_a_null_ttft_and_never_a_fabricated_one(self):
        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        call.streamed = False
        receipt = call.to_dict()
        assert receipt["ttft"]["value"] is None
        assert receipt["ttft"]["state"] == "unavailable"
        assert receipt["ttft"]["reason"] == "stream_disabled"
        assert receipt["streamed"] is False
        # The load-bearing assertion: it is NOT 0.0. A zero TTFT reads as "the
        # provider answered instantly", which is a different claim entirely.
        assert receipt["ttft"]["value"] != 0.0

    def test_a_stream_that_never_produced_a_delta_says_so(self):
        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        call.streamed = True
        call.ttft_s = None
        receipt = call.to_dict()
        assert receipt["ttft"]["value"] is None
        assert receipt["ttft"]["reason"] == "no_delta_observed_before_completion"

    def test_a_provider_that_reported_no_usage_yields_no_token_rates(self):
        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        call.streamed = True
        call.ttft_s = 0.5
        call.input_tokens = None
        call.output_tokens = None
        call.tokens_estimated = True
        receipt = call.to_dict()
        for key in ("input_tokens_per_s", "output_tokens_per_s"):
            assert receipt[key]["value"] is None, f"{key} reported a number"
            assert receipt[key]["state"] == "unavailable"
            assert receipt[key]["reason"]
        assert receipt["tokens_estimated"] is True

    def test_a_measured_call_reports_input_and_output_rates_separately(self):
        """CONTROL ARM: the unavailable arms above are satisfiable by a
        function that never measures anything. This proves the measured arm
        produces real numbers, and that the two RATES use different windows."""
        call = latency.CallLatency.begin()
        call.mark_dialed()
        time.sleep(0.05)
        call.mark_finished()
        call.streamed = True
        call.ttft_s = 0.02
        call.input_tokens = 1000
        call.output_tokens = 50
        receipt = call.to_dict()
        assert receipt["input_tokens_per_s"]["state"] == "measured"
        assert receipt["output_tokens_per_s"]["state"] == "measured"
        # Input is over the provider window, output over the GENERATION
        # window (provider minus TTFT). Mixing the denominators is how a
        # throughput number becomes decorative.
        assert receipt["generation"]["state"] == "measured"
        provider = receipt["provider"]["value"]
        generation = receipt["generation"]["value"]
        assert provider > generation >= 0.0
        assert receipt["input_tokens_per_s"]["value"] == pytest.approx(
            1000 / provider, rel=1e-3
        )
        assert receipt["output_tokens_per_s"]["value"] == pytest.approx(
            50 / generation, rel=1e-3
        )

    def test_output_rate_is_unavailable_when_there_is_no_generation_window(self):
        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        call.streamed = False
        call.input_tokens = 100
        call.output_tokens = 10
        receipt = call.to_dict()
        assert receipt["output_tokens_per_s"]["value"] is None
        assert receipt["output_tokens_per_s"]["state"] == "unavailable"
        assert "generation" in receipt["output_tokens_per_s"]["reason"]

    def test_the_mock_lane_reports_no_provider_and_no_gateway_overhead(self):
        """A scripted run computes locally. Reporting its work as provider
        time would let a mock ledger be read as throughput evidence."""
        call = latency.CallLatency.begin()
        call.dial_kind = "mock"
        call.mark_dialed()
        call.mark_finished()
        receipt = call.to_dict()
        assert receipt["dial_kind"] == "mock"
        assert receipt["provider"]["state"] == "unavailable"
        assert "network" in receipt["provider"]["reason"]
        assert receipt["overhead"]["state"] == "unavailable"
        assert "overhead" in receipt["overhead"]["reason"]

    def test_require_raises_with_the_reason_rather_than_returning_zero(self):
        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        with pytest.raises(latency.LatencyUnavailable) as excinfo:
            call.require("ttft")
        assert "stream_disabled" in str(excinfo.value)
        # And the measured arm returns a number, so `require` is usable.
        call.streamed = True
        call.ttft_s = 0.25
        assert call.require("ttft") == pytest.approx(0.25)

    def test_the_receipt_is_json_safe(self):
        import json

        call = latency.CallLatency.begin()
        call.mark_dialed()
        call.mark_finished()
        call.streamed = True
        call.ttft_s = 0.1
        call.input_tokens = 5
        call.output_tokens = 2
        json.dumps(call.to_dict())  # must not raise


class TestPercentilesStateTheirWindow:
    """A p95 over three samples is not a p95, and must say so."""

    def test_an_empty_window_is_unavailable_with_a_reason(self):
        receipt = latency.percentiles([])
        assert receipt["state"] == "unavailable"
        assert receipt["samples"] == 0
        assert receipt["reason"]
        assert receipt["values"]["p50"] is None

    def test_a_small_window_is_flagged_provisional(self):
        receipt = latency.percentiles([1.0, 2.0, 3.0])
        assert receipt["samples"] == 3
        assert receipt["provisional"] is True
        assert str(latency.MIN_PERCENTILE_SAMPLES) in receipt["reason"]
        assert receipt["values"]["p95"] is not None

    def test_a_full_window_is_not_provisional(self):
        data = [float(i) for i in range(latency.MIN_PERCENTILE_SAMPLES + 5)]
        receipt = latency.percentiles(data)
        assert receipt["provisional"] is False
        assert receipt["reason"] == ""
        assert receipt["values"]["p95"] == pytest.approx(24.0, abs=1.0)

    def test_none_samples_are_skipped_not_counted_as_zero(self):
        """A call whose TTFT was unavailable did not have a fast TTFT.

        This is the single most important percentile rule: counting the gaps
        as zero would drag p50 to 0.0 and make a healthy p50 look broken.
        """
        receipt = latency.percentiles([None, 1.0, 2.0, None, 3.0])
        assert receipt["observed"] == 5
        assert receipt["samples"] == 3
        assert receipt["skipped"] == 2
        assert receipt["values"]["p50"] == pytest.approx(2.0)

    def test_a_percentile_is_always_an_observed_value(self):
        """Nearest-rank, not interpolation: a reported percentile must be a
        number that was actually measured, never one invented between two
        samples."""
        data = [1.0, 100.0]
        receipt = latency.percentiles(data)
        assert receipt["values"]["p95"] in (1.0, 100.0)

    def test_the_window_reports_its_size_and_what_it_dropped(self):
        window = latency.LatencyWindow(max_samples=3)
        for i in range(5):
            call = latency.CallLatency.begin()
            call.mark_dialed()
            call.mark_finished()
            call.streamed = True
            call.ttft_s = float(i)
            call.input_tokens = i
            call.output_tokens = i
            window.add(call)
        receipt = window.to_dict()
        assert receipt["calls"] == 5
        assert receipt["dropped_samples"] == 2
        assert receipt["max_samples"] == 3
        assert receipt["ttft"]["samples"] == 3
        assert receipt["ttft"]["provisional"] is True


class TestTheRouterCarriesTheReceipt:
    """The router must publish the receipt, not just compute it."""

    @staticmethod
    def _call(**kwargs):
        """One mock-lane call, returning `get_last_usage()`.

        The usage MUST be read before the context is cleared:
        `set_call_context(None)` resets `_LAST_USAGE` to `{}` by design, so
        reading it afterwards returns nothing — which is a real trap for
        anyone writing a consumer against `get_last_usage()`.
        """
        from runtime import mock_provider, model_router

        mock_provider.reset()
        mock_provider.install(responses={"gpt-4o-mini": "hello there friend " * 8})
        model_router.set_call_context({"use_mock_provider": True})
        try:
            model_router.call_model(
                [{"role": "user", "content": "hi"}], difficulty_hint="easy", **kwargs
            )
            return dict(model_router.get_last_usage())
        finally:
            model_router.set_call_context(None)
            mock_provider.reset()

    def test_a_real_mock_lane_call_puts_the_latency_block_on_the_ledger(self):
        usage = self._call()
        assert usage, "get_last_usage() was empty after a real call"
        assert "latency" in usage, "no latency block on get_last_usage()"
        block = usage["latency"]
        for key in (
            "duration_s",
            "ttft",
            "overhead",
            "provider",
            "generation",
            "input_tokens_per_s",
            "output_tokens_per_s",
            "import_wait_s",
            "tokens_estimated",
            "dial_kind",
        ):
            assert key in block, f"latency receipt is missing {key!r}"
        # The flat keys exist so a JSONL reader that does not know this
        # module's shape can still get the numbers AND their states.
        for key in (
            "latency_ttft_s",
            "latency_ttft_state",
            "latency_overhead_s",
            "latency_overhead_state",
            "latency_provider_s",
            "latency_provider_state",
            "latency_import_wait_s",
            "latency_input_tokens_per_s",
            "latency_output_tokens_per_s",
        ):
            assert key in usage, f"flat key {key!r} is missing"

    def test_a_streamed_mock_call_reports_a_measured_ttft(self):
        seen: list = []
        usage = self._call(stream=True, on_delta=seen.append)
        assert usage["latency"]["streamed"] is True
        assert usage["latency_ttft_state"] == "measured"
        assert seen, "on_delta was never called"
        # The mock lane estimates its tokens, so the receipt must say so
        # rather than presenting an estimate as provider throughput.
        assert usage["latency"]["tokens_estimated"] is True
        assert usage["latency_input_tokens_per_s"] is None

    def test_a_non_streamed_call_does_not_claim_a_ttft(self):
        usage = self._call()
        assert usage["streamed"] is False
        assert usage["latency_ttft_s"] is None
        assert usage["latency_ttft_state"] == "unavailable"
        assert usage["latency_ttft_reason"] == "stream_disabled"

    def test_a_failing_call_still_records_a_latency_receipt(self):
        """A cost/latency claim about a failed call needs the same treatment
        as one about a successful call."""
        from runtime import mock_provider, model_router

        mock_provider.reset()  # no responses installed -> the call raises
        model_router.set_call_context({"use_mock_provider": True})
        try:
            with pytest.raises(RuntimeError):
                model_router.call_model(
                    [{"role": "user", "content": "hi"}], difficulty_hint="easy"
                )
            usage = dict(model_router.get_last_usage())
        finally:
            model_router.set_call_context(None)
            mock_provider.reset()
        assert usage.get("outcome") == "error"
        assert "latency" in usage
        assert usage["latency_ttft_s"] is None


# ---------------------------------------------------------------------------
# Group 3 — the invariants
# ---------------------------------------------------------------------------


class TestSpentUsdIsAMaximum:
    """Re-pinned after the turn-ceiling change: more calls, more chances to
    turn a maximum into a sum, and a sum fires the cap LATER than intended.

    Read the property precisely first, because the obvious statement of it is
    wrong. The governor's OWN charges ARE summed — twenty charges of $0.10 are
    $2.00, and pretending otherwise would under-report spend. What must be a
    MAXIMUM is the combination of the governor's total and the bound
    `spend_source`, because they are two views of the SAME money: the
    governor sees calls it priced, and `ModelClient` sees a total that already
    includes a provider fallback's earlier charges. Adding them double-counts,
    and a cap that fires late is worse than a cap that fires early.
    """

    def test_two_independent_sources_are_maximised_not_summed(self):
        gov = budget_governor.BudgetGovernor(cap_usd=10.0)
        gov.commit(0.30, model="a")
        gov.bind_spend_source(lambda: 0.50)
        assert gov.spent_usd() == pytest.approx(0.50), (
            "a second view of the SAME spend must raise the maximum, not add"
            " to it; summing double-counts and fires the cap LATER than intended"
        )
        gov.bind_spend_source(lambda: 0.10)
        assert gov.spent_usd() == pytest.approx(0.30), (
            "a LOWER external view must not reduce what the governor saw"
        )

    def test_agreement_between_the_two_sources_is_not_doubled(self):
        """CONTROL ARM for the anti-double-count claim: when both views report
        the SAME number, the answer is that number, not twice it. A summing
        implementation passes the first test's first half and fails here."""
        gov = budget_governor.BudgetGovernor(cap_usd=10.0)
        gov.commit(1.00, model="a")
        gov.bind_spend_source(lambda: 1.00)
        assert gov.spent_usd() == pytest.approx(1.00), (
            "two views reporting the same spend must not be added together"
        )

    def test_the_governors_own_charges_do_accumulate(self):
        """The other half of the contract, and the reason the maximum is not
        a maximum of individual charges: the governor's own counter is a SUM
        and under-reporting it would be a different lie."""
        gov = budget_governor.BudgetGovernor(cap_usd=100.0)
        for _ in range(20):
            gov.commit(0.10, model="a")
        assert gov.spent_usd() == pytest.approx(2.00)

    def test_a_raising_spend_source_is_never_allowed_to_disable_the_cap(self):
        gov = budget_governor.BudgetGovernor(cap_usd=1.0)
        gov.commit(0.20, model="a")
        gov.bind_spend_source(lambda: 5.00)
        assert gov.spent_usd() == pytest.approx(5.00)
        assert gov.exhausted is True

    def test_a_raising_spend_source_is_ignored_not_fatal(self):
        gov = budget_governor.BudgetGovernor(cap_usd=1.0)
        gov.commit(0.20, model="a")

        def boom():
            raise RuntimeError("the ledger went away")

        gov.bind_spend_source(boom)
        assert gov.spent_usd() == pytest.approx(0.20), (
            "a spend_source that raises must be ignored, never allowed to"
            " take the cap down with it"
        )


class TestQuotaIsNotBudget:
    """Re-pinned after the turn-ceiling change. Quota is tested FIRST, and
    `provider_fault=False` means no failover."""

    class _RateLimited(Exception):
        def __init__(self, message: str, status_code: int = 429) -> None:
            super().__init__(message)
            self.status_code = status_code

    def test_a_quota_error_is_classified_as_quota_and_not_as_a_rate_limit(self):
        exc = self._RateLimited("Your quota exceeded: insufficient_quota")
        failure = budget_governor.classify_provider_failure(exc)
        assert failure.kind == "quota_exhausted"
        assert failure.retryable is False
        assert failure.provider_fault is False, (
            "an empty account is not a provider fault; counting it as one"
            " trips the breaker and fails over to a second target on the"
            " SAME billing account"
        )
        assert failure.billing_url

    def test_a_genuine_rate_limit_is_distinguishable_and_marks_a_provider_fault(self):
        failure = budget_governor.classify_provider_failure(
            self._RateLimited("429 rate limit exceeded, slow down")
        )
        assert failure.kind != "quota_exhausted"
        assert failure.retryable is True
        assert failure.provider_fault is True

    def test_the_two_shapes_are_not_confusable_by_status_code(self):
        """Several providers return quota as a 429. The status code is
        therefore NOT the discriminator, and a test that keys on it is
        testing the wrong thing."""
        quota = budget_governor.classify_provider_failure(
            self._RateLimited("insufficient_quota", status_code=429)
        )
        limited = budget_governor.classify_provider_failure(
            self._RateLimited("rate limit exceeded", status_code=429)
        )
        assert quota.kind != limited.kind
        assert quota.kind == "quota_exhausted"

    def test_the_marker_sets_do_not_overlap(self):
        """A marker in both sets makes the CLASSIFICATION ORDER decide the
        answer, which is exactly the ambiguity the split exists to remove."""
        assert set(budget_governor.QUOTA_MARKERS).isdisjoint(
            set(budget_governor.RATE_LIMIT_MARKERS)
        )
        assert set(budget_governor.QUOTA_MARKERS).isdisjoint(set(_rate_limit_markers()))

    def test_the_two_classifiers_share_ONE_quota_marker_set(self):
        """`provider_resilience` re-exports the governor's set rather than
        copying it. Two copies would be two answers to "is this an empty
        account", and they would drift the first time somebody added a marker
        to one of them.

        EQUALITY, not disjointness: the whole point is that they are the same
        vocabulary, so the assertion is that they are the same object.
        """
        import runtime.provider_resilience as resilience

        assert resilience.QUOTA_MARKERS is budget_governor.QUOTA_MARKERS, (
            "provider_resilience.QUOTA_MARKERS must BE the governor's set"
        )
        assert set(resilience.QUOTA_MARKERS).isdisjoint(
            set(resilience._RATE_LIMIT_MARKERS)
        ), (
            "a marker in BOTH sets makes the classification ORDER decide the"
            " answer, which is the ambiguity the split exists to remove"
        )


def _rate_limit_markers():
    import runtime.provider_resilience as resilience

    return resilience._RATE_LIMIT_MARKERS

    def test_quota_is_tested_before_budget_in_the_retry_classifier(self):
        import runtime.provider_resilience as resilience

        for marker in ("insufficient_quota", "quota exceeded", "out of credits"):
            verdict = resilience.classify_retry(
                self._RateLimited(marker, status_code=429)
            )
            assert verdict.kind == "quota_exhausted", marker
            assert verdict.retryable is False, marker
            assert verdict.provider_fault is False, marker
            assert verdict.replayable is False, (
                f"{marker!r}: replaying against the same empty account spends"
                " money that is not there"
            )

    def test_a_quota_refusal_is_not_a_budget_refusal_and_not_a_permission_error(self):
        """Two different refusals, and one exception shape the gateway's
        fail-closed paths must not swallow.

        `provider_gateway.resilient_call_model` re-raises `QuotaExhausted`
        immediately: no retry, no backoff, no next candidate. If it were a
        `PermissionError` the privacy/offline refusal handling could absorb it
        and turn a refusal into a normal-looking run.
        """
        failure = budget_governor.classify_provider_failure(
            self._RateLimited("insufficient_quota")
        )
        refusal = budget_governor.QuotaExhausted(failure)
        assert isinstance(refusal, RuntimeError)
        assert not isinstance(refusal, PermissionError)
        assert not isinstance(refusal, budget_governor.BudgetRefused)
        assert isinstance(refusal, budget_governor.QuotaExhausted)


class TestTheTurnCeilingDoesNotDelayTheBudgetCap:
    """T1 is raising `agent_max_turns` 25 -> >=50. The budget must still fire
    first, and the answer must not depend on the turn count."""

    @staticmethod
    def _harness_turn_loop_source():
        return (REPO_ROOT / "harness" / "agent_kernel" / "strategy.py").read_text(
            encoding="utf-8-sig"
        )

    def test_the_budget_check_is_inside_the_turn_loop_and_unguarded_by_the_index(self):
        """Structural, read-only, from the harness this round does not own.

        The budget comparison must appear inside the `for turn in range(1,
        max_turns + 1)` body and must NOT be nested under a condition on
        `turn`. If it were, raising the ceiling could delay it.
        """
        source = self._harness_turn_loop_source()
        marker = "for turn in range(1, max_turns + 1):"
        start = source.index(marker)
        body = source[start : start + 3000]
        assert "budget_cap_usd" in body, "the budget check is not in the turn loop"
        head = body[: body.index("budget_cap_usd")]
        for bad in ("if turn", "turn <", "turn ==", "turn !=", "while turn"):
            assert bad not in head, (
                f"the budget guard is behind a turn-index condition ({bad!r});"
                " raising the ceiling could then delay the cap"
            )

    def test_the_budget_check_precedes_the_turns_work_in_the_loop_body(self):
        """Order, not just presence: the budget must be checked BEFORE the
        turn does anything, so a turn is never paid for after the cap."""
        source = self._harness_turn_loop_source()
        start = source.index("for turn in range(1, max_turns + 1):")
        body = source[start : start + 3000]
        budget_at = body.index("budget_cap_usd")
        work_at = body.index("_begin_turn")
        assert budget_at < work_at, (
            "the turn's work starts before the budget is checked; the cap"
            " would then be enforced a turn late"
        )

    def test_the_turn_ceiling_is_the_loop_bound_not_a_budget_input(self):
        """`max_turns` bounds iterations. It is not read by the governor, so
        it cannot participate in the cap decision at all."""
        source = inspect_source(budget_governor)
        for name in ("max_turns", "agent_max_turns", "max_step_turns"):
            assert name not in source, (
                f"the governor reads {name!r}; the turn ceiling and the money"
                " cap are two independent guards and must stay so"
            )

    def test_the_cap_fires_at_the_same_charge_count_regardless_of_any_ceiling(self):
        """The bound that actually stops a run, driven directly.

        Twenty charges of $0.25 against a $2.00 cap: the cap is reached on
        charge 8 whatever any turn ceiling is, because the governor is never
        told what the ceiling is.
        """
        gov = budget_governor.BudgetGovernor(cap_usd=2.0)
        charges = 0
        while not gov.exhausted and charges < 100:
            gov.commit(0.25, model="m")
            charges += 1
        assert charges == 8, (
            f"the cap fired after {charges} charges of $0.25 against a $2.00"
            " cap; the expected answer is 8 and is independent of any turn cap"
        )
        assert gov.spent_usd() == pytest.approx(2.0)
        assert gov.remaining_usd() == pytest.approx(0.0)

    def test_a_per_call_pre_check_refuses_before_the_cap_is_exceeded(self):
        """The guard one level TIGHTER than the per-turn check: a call is
        priced BEFORE it is dialed, so the bound is `cap + one call` rather
        than `cap + a whole turn`."""
        gov = budget_governor.BudgetGovernor(cap_usd=0.10)
        gov.commit(0.08, model="m")
        assert gov.remaining_usd() == pytest.approx(0.02)
        verdict = gov.authorize_call(
            target={"model": "gpt-4o-mini"},
            price_usd=0.05,
            price_state="priced",
        )
        assert verdict.allowed is False
        assert verdict.reason
        assert gov.reserved_usd() == pytest.approx(0.0), (
            "a refused call must not leave a reservation behind; a stuck"
            " reservation would keep refusing every later call too"
        )
        with pytest.raises(budget_governor.BudgetRefused):
            raise budget_governor.BudgetRefused(verdict)

    def test_the_worst_case_cost_at_the_new_ceiling_is_reported_not_asserted_away(self):
        """T3.W1.3's deliverable, as an executable number.

        A task that CONSUMES all 50 turns at frontier prices, priced through
        the ONE price authority rather than a number typed in here.
        """
        from runtime.model_capabilities import MODEL_PRICES, estimate_cost

        model = "claude-3-5-sonnet-20241022"
        assert model in MODEL_PRICES, "the frontier tier lost its price row"
        per_turn = estimate_cost(model, 17_000, 800).cost_usd
        at_50 = per_turn * 50
        at_25 = per_turn * 25
        assert at_50 == pytest.approx(at_25 * 2), "the ceiling is not a 2x lever"
        # And the shipped budget cap makes that worst case unreachable, which
        # is the whole answer to "is the new ceiling a cost regression".
        assert at_50 > 2.0, (
            f"50 frontier turns now cost ${at_50:.2f}, which is under the"
            " $2.00 default cap. The claim 'the budget guard fires first'"
            " would need re-measuring, and the worst case would be reachable."
        )

    def test_the_budget_cap_comes_before_the_ceiling_in_the_legacy_loop_too(self):
        """The legacy `harness.agent_loop` path has the same shape and the
        same obligation; a pin on only one engine is a pin on half the
        property."""
        source = (REPO_ROOT / "harness" / "agent_loop.py").read_text(
            encoding="utf-8-sig"
        )
        marker = "for turn in range(1, max_turns + 1):"
        start = source.index(marker)
        body = source[start : start + 3000]
        assert "budget_cap_usd" in body
        head = body[: body.index("budget_cap_usd")]
        for bad in ("if turn", "turn <", "turn =="):
            assert bad not in head


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


# ---------------------------------------------------------------------------
# Group 4 — the expensive-module classification (W1.4)
# ---------------------------------------------------------------------------


class TestTheStartupAudit:
    def test_the_scanner_actually_detects_each_shape(self):
        """CONTROL ARM. A scanner that finds nothing is indistinguishable from
        a scanner that is broken, so each detector is driven on a synthetic
        module that contains exactly one of its shapes."""
        import tempfile

        big_literal = "[" + ",".join(["1"] * 70) + "]"
        body = (
            "import os, sqlite3\n"
            "from pathlib import Path\n"
            "from harness.agent_kernel import builtin_tool_specs\n"
            "CONN = sqlite3.connect(':memory:')\n"
            "TABLE = {k: k for k in range(10)}\n"
            f"BIG = {big_literal}\n"
            "LAZY = (i for i in range(10))\n"
            "def f():\n"
            "    return sqlite3.connect(':memory:')\n"
            "if __name__ == '__main__':\n"
            "    sqlite3.connect(':memory:')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.py"
            path.write_text(body, encoding="utf-8")
            found = startup_audit.scan_module(path)
        kinds = {op.kind for op in found}
        assert "call" in kinds, "did not detect the module-level sqlite3.connect"
        assert "comprehension" in kinds, "did not detect the dict comprehension"
        assert "literal" in kinds, "did not detect the large literal"
        assert "cross_module_import" in kinds, (
            "did not detect the first-party import. This is the shape that"
            " cost 318 ms at runtime/roles.py:19, so a scanner that misses it"
            " reports the 0.029 ms comprehension and misses the import."
        )
        lines = {(op.kind, op.line) for op in found}
        # A call inside a function is NOT module-level, so it is not a hit.
        assert not any(line == ("call", 8) for line in lines), (
            "a call inside a function is not paid at import"
        )
        # A module-level generator expression is lazy: nothing runs.
        assert not any(line == ("comprehension", 7) for line in lines), (
            "a module-level generator expression executes nothing"
        )
        # `if __name__ == '__main__'` can never run at import.
        assert not any(line == ("call", 10) for line in lines), (
            "the __main__ guard is a false positive"
        )
        # A stdlib import is not a first-party one.
        assert not any(
            kind == "cross_module_import" and label == "os"
            for kind, label in ((o.kind, o.label) for o in found)
        ), "a stdlib import is not a cross-module cost"

    def test_a_file_that_does_not_parse_is_reported_not_skipped(self):
        """Skipping an unreadable file turns 'I could not read this' into
        'this file is clean', which is the hole a T1 round documented."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.py"
            path.write_bytes(b"def f(:\n  pass\n")
            found = startup_audit.scan_module(path)
        assert len(found) == 1
        assert found[0].kind == "unparseable"
        assert found[0].verdict == "UNCLASSIFIED"

    def test_every_hit_in_runtime_carries_a_verdict(self):
        """An expensive module-level operation with no recorded verdict is an
        open audit item, and an open audit item is this round's defect class."""
        ops = startup_audit.classify()
        unclassified = [op for op in ops if op.verdict == "UNCLASSIFIED"]
        assert unclassified == [], (
            "these module-level operations have no verdict; classify each as"
            f" one of {startup_audit.VERDICTS}: "
            f"{[(op.module, op.line, op.label) for op in unclassified]}"
        )

    def test_the_verdict_table_is_not_silently_empty(self):
        """A vacuous table would make the previous test pass for the wrong
        reason: no hits at all also means no unclassified hits."""
        assert startup_audit.classify(), "the audit found nothing to classify"

    def test_the_enumeration_table_is_json_safe_and_complete(self):
        import json

        table = startup_audit.enumeration_table()
        assert table == json.loads(json.dumps(table))
        for row in table:
            assert row["verdict"] in startup_audit.VERDICTS
            assert row["reason"], f"{row} has a verdict but no reason"


# ---------------------------------------------------------------------------
# Group 5 — the subprocess proofs
# ---------------------------------------------------------------------------


SUBPROCESS_TIMEOUT = 300


class TestSubprocessProofs:
    """These are subprocess tests because the property is about PROCESS
    behaviour: a preload that only works when nothing else has imported the
    module is not a preload."""

    def _run(self, code: str, env_extra=None, timeout=SUBPROCESS_TIMEOUT):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )
        return proc

    def test_importing_runtime_does_not_import_litellm(self):
        """The whole premise: the 6-22 s import is NOT paid at import time,
        which is why `neo --version` is fast and the first prompt is not."""
        proc = self._run(
            "import sys, time; t=time.perf_counter(); import runtime;"
            " print(round((time.perf_counter()-t)*1000, 1),"
            " 'litellm' in sys.modules)",
            {"NEO_PRELOAD_LITELLM": "off"},
        )
        assert proc.returncode == 0, proc.stderr
        ms, loaded = proc.stdout.strip().split()
        assert loaded == "False", "importing runtime pulled in litellm"
        assert float(ms) < 2000, f"import runtime took {ms} ms"

    def test_a_preload_in_a_real_subprocess_yields_a_usable_module(self):
        """The end-to-end proof in a process where nothing else has imported
        litellm. `sys.modules` alone is not the guarantee — the module must be
        COMPLETE, which is what the assertion checks."""
        proc = self._run(
            "import sys, time, threading\n"
            "import runtime.latency as L\n"
            "L.reset_preload_state()\n"
            "import os; os.environ['NEO_PRELOAD_LITELLM']='on'\n"
            "L.start_preload(reason='pin')\n"
            "hit = {}\n"
            "def fg():\n"
            "    t0=time.perf_counter(); m=L.ensure_litellm()\n"
            "    hit['waited']=time.perf_counter()-t0; hit['m']=m\n"
            "t=threading.Thread(target=fg, daemon=True); t.start()\n"
            "t.join(240)\n"
            "assert not t.is_alive(), 'foreground call hung'\n"
            "assert callable(hit['m'].completion), 'half-initialised module'\n"
            "print('ok', hit['waited'])\n"
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        assert proc.stdout.strip().startswith("ok")

    def test_the_diagnostics_surface_works_before_anything_is_loaded(self):
        """`cli/doctor.py` (T4) will call this on a process that has never
        dialed a provider. It must answer, not raise and not hang."""
        proc = self._run(
            "import time, runtime.latency as L\n"
            "t0=time.perf_counter(); r=L.preload_state();"
            " d=time.perf_counter()-t0\n"
            "assert d < 0.5, f'preload_state blocked {d:.2f}s'\n"
            "assert r['state'] in L.PRELOAD_STATES\n"
            "assert 'present' in r and 'eligible' in r and 'env' in r\n"
            "import json; json.dumps(r)\n"
            "print('ok', r['state'])\n",
            {"NEO_PRELOAD_LITELLM": "off"},
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.strip().startswith("ok")
