"""Regression tests for VEX-CEILING-10 part 1: real streaming.

Covers the producer primitives in ``runtime.streaming``, the
``stream=True`` end-to-end path through ``runtime.model_router``, and the
``model_delta`` journal rows ``harness.model_client`` emits.

These are unit/regression tests. The real-PTY, real-provider, and Docker
lanes are NOT exercised here and are reported as not selected in the
handoff, never as passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

import runtime.streaming as streaming
from harness.model_client import ModelClient
from harness.trace import TraceLogger
from runtime import model_router
from tests.fake_model import ScriptedModel

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _chunk(text: str = "", *, finish: str | None = None, tool=None, usage=None):
    """One provider stream frame, shaped like litellm's."""
    frame: dict = {"choices": [{"delta": {}, "finish_reason": finish}]}
    if text:
        frame["choices"][0]["delta"]["content"] = text
    if tool is not None:
        frame["choices"][0]["delta"]["tool_calls"] = tool
    if usage is not None:
        frame["usage"] = usage
    return frame


def _word_chunks(text: str) -> list[dict]:
    """Split text into the word-ish frames a live provider would emit."""
    parts = text.split(" ")
    return [
        _chunk(part if index == len(parts) - 1 else part + " ")
        for index, part in enumerate(parts)
    ]


class _Clock:
    """Monotonic clock that advances only when told to.

    Coalescing is a wall-clock contract, so a deterministic test must move
    the clock rather than ask for an impossible 0ms window.
    """

    def __init__(self, step: float = 0.001) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def routed(tmp_path, monkeypatch):
    """A router context with a fresh ledger and a scripted provider."""
    ledger = tmp_path / "ledger.jsonl"
    recorded: list[dict] = []

    def configure(frames, *, usage=None, ctx_extra=None):
        payloads = list(frames) + ([_chunk(usage=usage)] if usage else [])
        recorded.clear()

        class _Message:
            content = "".join(
                frame["choices"][0]["delta"].get("content", "")
                for frame in payloads
                if frame.get("choices")
            )
            tool_calls = None

        class _Choice:
            message = _Message()
            finish_reason = "stop"

        class _Response:
            choices: ClassVar[list] = [_Choice()]
            usage = None

        def completion(**kwargs):
            recorded.append(kwargs)
            if kwargs.get("stream"):
                return iter(payloads)
            return _Response()

        fake = type("_LitellmStub", (), {"completion": staticmethod(completion)})
        monkeypatch.setitem(sys.modules, "litellm", fake)
        ctx = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "adaptive_routing": False,
            "use_mock_provider": False,
        }
        ctx.update(ctx_extra or {})
        model_router.set_call_context(ctx)
        model_router._LEDGER_PATH.set(ledger)
        return recorded

    yield configure
    model_router.set_call_context({})
    model_router._LEDGER_PATH.set(None)


def _rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    text = ledger.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# runtime.streaming — the producer primitive
# ---------------------------------------------------------------------------


class TestStreamAssembler:
    def test_assembles_text_from_chunks(self):
        clock = _Clock()
        got: list[str] = []
        assembler = streaming.StreamAssembler(on_delta=got.append, clock=clock)
        assembler.begin()
        for frame in _word_chunks("hello brave world"):
            assembler.feed(frame)
        assembler.flush()
        assert assembler.text == "hello brave world"
        assert "".join(got) == "hello brave world"
        assert assembler.stats.chunks_seen == 3

    def test_window_bounds_delivery_count(self):
        """One delivery per window, not one per chunk.

        500 chunks must not become 500 callbacks — that is the whole
        point of coalescing, and it is what makes frame cost independent
        of token rate.
        """
        clock = _Clock(step=0.0)
        got: list[str] = []
        assembler = streaming.StreamAssembler(
            on_delta=got.append, window_ms=500, clock=clock
        )
        assembler.begin()
        for _ in range(500):
            assembler.feed(_chunk("x"))
        assert assembler.stats.chunks_seen == 500
        assembler.flush()
        assert assembler.stats.deliveries <= 1
        assert len(got) <= 1

    def test_a_long_stream_delivers_once_per_window(self):
        """A stream that spans several windows delivers once per window."""
        clock = _Clock(step=0.0)
        got: list[str] = []
        assembler = streaming.StreamAssembler(
            on_delta=got.append, window_ms=100, clock=clock
        )
        assembler.begin()
        for index in range(300):
            assembler.feed(_chunk("y"))
            if index % 30 == 0:
                clock.advance(0.2)  # 200ms — closes the window
        assembler.flush()
        assert assembler.stats.chunks_seen == 300
        # ~10 windows plus the trailing flush: nowhere near 300 callbacks.
        assert 1 < assembler.stats.deliveries <= 14
        assert assembler.stats.events_per_delivery > 1.0
        assert "".join(got) == "y" * 300

    def test_window_ms_is_clamped_to_the_documented_band(self):
        assert streaming.MIN_WINDOW_MS == 40
        assert streaming.MAX_WINDOW_MS == 500
        assert (
            streaming.StreamAssembler(window_ms=1).window_ms == streaming.MIN_WINDOW_MS
        )
        assert (
            streaming.StreamAssembler(window_ms=99_999).window_ms
            == streaming.MAX_WINDOW_MS
        )
        assert streaming.StreamAssembler(window_ms=120).window_ms == 120

    def test_malformed_chunk_never_raises(self):
        clock = _Clock()
        got: list[str] = []
        assembler = streaming.StreamAssembler(on_delta=got.append, clock=clock)
        assembler.begin()
        assembler.feed({"choices": "not-a-list"})
        assembler.feed(object())
        assembler.feed(_chunk("survivor"))
        assembler.flush()
        assert assembler.text == "survivor"
        assert assembler.stats.malformed_chunks >= 1

    def test_usage_only_tail_frame_is_not_malformed(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.feed(_chunk("a"))
        assembler.feed(_chunk(usage={"prompt_tokens": 3}))
        assembler.flush()
        assert assembler.stats.malformed_chunks == 0
        assert assembler.usage == {"prompt_tokens": 3}

    def test_delta_callback_failure_does_not_lose_text(self):
        def boom(_delta):
            raise RuntimeError("ui died")

        assembler = streaming.StreamAssembler(on_delta=boom, clock=_Clock())
        assembler.begin()
        assembler.feed(_chunk("kept"))
        assembler.flush()
        assert assembler.text == "kept"
        assert assembler.stats.malformed_chunks >= 1

    def test_text_is_bounded_with_an_explicit_marker(self):
        assembler = streaming.StreamAssembler(max_chars=1024, clock=_Clock())
        assembler.begin()
        for _ in range(200):
            assembler.feed(_chunk("abcdefghij"))
        assembler.flush()
        assert assembler.stats.truncated is True
        assert len(assembler.text) < 4096
        assert streaming.TRUNCATION_MARKER in assembler.text

    def test_streamed_tool_call_fragments_become_one_call(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.feed(
            _chunk(
                tool=[
                    {
                        "index": 0,
                        "id": "call_abc",
                        "function": {"name": "read", "arguments": '{"pa'},
                    }
                ]
            )
        )
        assembler.feed(
            _chunk(tool=[{"index": 0, "function": {"arguments": 'th": "a.py"}'}}])
        )
        assembler.flush()
        calls = assembler.tool_calls
        assert len(calls) == 1
        assert calls[0]["id"] == "call_abc"
        assert calls[0]["function"]["name"] == "read"
        assert calls[0]["function"]["arguments"] == {"path": "a.py"}

    def test_two_parallel_streamed_tool_calls_stay_separate(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        for index, name in enumerate(("read", "grep")):
            assembler.feed(
                _chunk(
                    tool=[
                        {
                            "index": index,
                            "id": f"c{index}",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ]
                )
            )
        assembler.flush()
        names = sorted(call["function"]["name"] for call in assembler.tool_calls)
        assert names == ["grep", "read"]

    def test_unparseable_tool_arguments_degrade_to_empty_dict(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.feed(
            _chunk(
                tool=[{"index": 0, "function": {"name": "x", "arguments": "{not json"}}]
            )
        )
        assembler.flush()
        assert assembler.tool_calls[0]["function"]["arguments"] == {}

    def test_finish_reason_and_usage_are_captured(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.feed(_chunk("x"))
        assembler.feed(_chunk(finish="stop"))
        assembler.flush()
        assert assembler.finish_reason == "stop"

    def test_first_token_latency_is_measured(self):
        clock = _Clock()
        assembler = streaming.StreamAssembler(clock=clock)
        assembler.begin()
        clock.advance(1.5)
        assembler.feed(_chunk("a"))
        assembler.flush()
        assert assembler.stats.first_token_s is not None
        assert assembler.stats.first_token_s >= 1.4

    def test_first_token_is_unset_before_anything_arrives(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assert assembler.stats.first_token_s is None

    def test_receipt_is_json_safe(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.feed(_chunk("a"))
        assembler.flush()
        payload = assembler.stats.to_dict()
        assert json.loads(json.dumps(payload))["chunks_seen"] == 1
        assert "events_per_delivery" in payload

    def test_events_per_delivery_is_zero_when_nothing_was_observed(self):
        assembler = streaming.StreamAssembler(clock=_Clock())
        assembler.begin()
        assembler.flush()
        assert assembler.stats.events_per_delivery == 0.0

    def test_iter_stream_handles_list_single_and_none(self):
        assert list(streaming.iter_stream([_chunk("a"), _chunk("b")])) == [
            _chunk("a"),
            _chunk("b"),
        ]
        assert list(streaming.iter_stream(None)) == []
        assert list(streaming.iter_stream(_chunk("solo"))) == [_chunk("solo")]

    def test_estimate_prompt_tokens_is_stable_and_nonzero(self):
        messages = [{"content": "x" * 400}, {"content": "y" * 40}]
        first = streaming.estimate_prompt_tokens(messages)
        assert first == streaming.estimate_prompt_tokens(messages)
        assert first >= 10
        assert streaming.estimate_prompt_tokens([]) == 10

    def test_stream_call_never_retries(self):
        """A stream must not be replayed as a second charge."""
        attempts = []

        def completion(**kwargs):
            attempts.append(kwargs)
            raise ConnectionError("stream died")

        with pytest.raises(ConnectionError):
            streaming.stream_call(completion, {"model": "m"}, on_delta=lambda _d: None)
        assert len(attempts) == 1
        assert attempts[0]["stream"] is True

    def test_stream_call_returns_the_full_tuple(self):
        def completion(**kwargs):
            return iter(
                [
                    *_word_chunks("hi there"),
                    _chunk(finish="stop"),
                    _chunk(usage={"prompt_tokens": 5}),
                ]
            )

        text, calls, reason, stats, usage = streaming.stream_call(
            completion, {"model": "m"}, on_delta=lambda _d: None, window_ms=40
        )
        assert text == "hi there"
        assert calls == []
        assert reason == "stop"
        assert stats.chunks_seen == 4
        assert usage == {"prompt_tokens": 5}


# ---------------------------------------------------------------------------
# runtime.model_router — stream=True reaches the provider
# ---------------------------------------------------------------------------


class TestRouterStreaming:
    def test_stream_true_reaches_the_provider(self, routed):
        calls = routed(_word_chunks("streamed answer"))
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        assert out == "streamed answer"
        assert calls, "provider was never dialed"
        assert calls[0]["stream"] is True

    def test_deltas_reach_the_callback(self, routed):
        got: list[str] = []
        clock = _Clock()
        routed(_word_chunks("one two three"), ctx_extra={"stream_window_ms": 40})
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=got.append,
        )
        assert out == "one two three"
        assert "".join(got) == "one two three"
        del clock

    def test_ledger_records_the_streaming_receipt(self, routed, tmp_path):
        routed(_word_chunks("alpha beta"))
        model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        rows = [
            row
            for row in _rows(tmp_path / "ledger.jsonl")
            if row.get("outcome") == "success"
        ]
        assert rows, "no successful ledger row"
        assert rows[-1]["streamed"] is True
        assert rows[-1]["stream"]["chunks_seen"] >= 1
        assert rows[-1]["stream"]["deliveries"] >= 1
        assert rows[-1]["stream"]["window_ms"] == 40

    def test_non_streaming_is_the_historical_request(self, routed):
        calls = routed(_word_chunks("plain"))
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}], model="gpt-4o-mini"
        )
        assert out == "plain"
        assert "stream" not in calls[0]

    def test_stream_without_a_callback_is_not_streamed(self, routed, tmp_path):
        """`stream=True` with no sink must not pay the streaming tax and
        must not be recorded as streamed."""
        calls = routed(_word_chunks("plain"))
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}], model="gpt-4o-mini", stream=True
        )
        assert out == "plain"
        assert "stream" not in calls[0]
        rows = [
            row
            for row in _rows(tmp_path / "ledger.jsonl")
            if row["outcome"] == "success"
        ]
        assert rows[-1]["streamed"] is False

    def test_usage_frame_pricing_is_labeled_provider(self, routed, tmp_path):
        routed(
            _word_chunks("a b"),
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.5},
        )
        model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        rows = [
            row
            for row in _rows(tmp_path / "ledger.jsonl")
            if row.get("outcome") == "success"
        ]
        assert rows[-1]["cost_source"] == "provider"
        assert rows[-1]["tokens"] == 120

    def test_missing_usage_is_estimated_and_labeled(self, routed, tmp_path):
        routed(_word_chunks("a b"))
        model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        rows = [
            row
            for row in _rows(tmp_path / "ledger.jsonl")
            if row.get("outcome") == "success"
        ]
        assert rows[-1]["cost_source"] != "provider"
        assert rows[-1]["prompt_tokens"] > 0

    def test_streamed_tool_call_returns_the_normalized_shape(self, routed):
        routed(
            [
                _chunk(
                    tool=[
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ]
                ),
                _chunk(finish="tool_calls"),
            ]
        )
        out = model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        assert out["tool_calls"][0]["function"]["name"] == "read"
        assert out["finish_reason"] == "tool_calls"

    def test_empty_stream_is_an_error_not_a_blank_success(self, routed, tmp_path):
        routed([])
        with pytest.raises(RuntimeError, match="empty assistant content"):
            model_router.call_model(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o-mini",
                stream=True,
                on_delta=lambda _d: None,
            )
        rows = _rows(tmp_path / "ledger.jsonl")
        assert any(row.get("outcome") == "error" for row in rows)

    def test_streamed_stop_reason_is_recorded(self, routed, tmp_path):
        routed([_chunk("x"), _chunk(finish="length")])
        model_router.call_model(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            stream=True,
            on_delta=lambda _d: None,
        )
        rows = [
            row
            for row in _rows(tmp_path / "ledger.jsonl")
            if row.get("outcome") == "success"
        ]
        assert rows[-1]["stop_reason"] == "length"

    def test_provider_failure_is_recorded(self, routed, tmp_path, monkeypatch):
        def explode(**kwargs):
            raise ConnectionError("stream died")

        routed([])
        fake = type("_LitellmStub", (), {"completion": staticmethod(explode)})
        monkeypatch.setitem(sys.modules, "litellm", fake)
        with pytest.raises(ConnectionError):
            model_router.call_model(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o-mini",
                stream=True,
                on_delta=lambda _d: None,
            )
        assert any(
            row.get("outcome") == "error" for row in _rows(tmp_path / "ledger.jsonl")
        )

    def test_mock_lane_streams_through_the_same_assembler(self):
        """The offline lane must exercise the real delta path, or every
        streaming regression would be invisible to the eval matrix."""
        from runtime import mock_provider

        mock_provider.install({"m": "offline streamed reply"})
        got: list[str] = []
        model_router.set_call_context(
            {
                "use_mock_provider": True,
                "adaptive_routing": False,
                "model": "m",
                "stream_window_ms": 40,
            }
        )
        try:
            out = model_router.call_model(
                [{"role": "user", "content": "hi"}], stream=True, on_delta=got.append
            )
            usage = model_router.get_last_usage()
        finally:
            model_router.set_call_context({})
        assert out == "offline streamed reply"
        assert "".join(got) == "offline streamed reply"
        assert usage.get("streamed") is True
        assert usage["stream"]["chunks_seen"] >= 1

    def test_mock_lane_without_a_sink_is_unchanged(self):
        from runtime import mock_provider

        mock_provider.install({"m": "plain reply"})
        model_router.set_call_context(
            {"use_mock_provider": True, "adaptive_routing": False, "model": "m"}
        )
        try:
            out = model_router.call_model([{"role": "user", "content": "hi"}])
            usage = model_router.get_last_usage()
        finally:
            model_router.set_call_context({})
        assert out == "plain reply"
        assert usage.get("streamed") is False


# ---------------------------------------------------------------------------
# harness.model_client — model_delta journal rows
# ---------------------------------------------------------------------------


class _RecordingTrace(TraceLogger):
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def log(self, kind: str, data: dict) -> None:  # type: ignore[override]
        self.rows.append((kind, dict(data)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.rows]


class _BrokenDeltaTrace(_RecordingTrace):
    def log(self, kind: str, data: dict) -> None:  # type: ignore[override]
        if kind == "model_delta":
            raise OSError("journal gone")
        self.rows.append((kind, dict(data)))


def _streaming_boundary(text: str, log: list | None = None):
    """A Boundary-2 double that speaks the streaming contract."""

    def call_model(
        messages,
        difficulty_hint=None,
        provider=None,
        model=None,
        api_key=None,
        tools=None,
        tool_choice=None,
        stream=False,
        on_delta=None,
    ):
        if log is not None:
            log.append({"stream": stream, "has_sink": on_delta is not None})
        assembler = streaming.StreamAssembler(on_delta=on_delta, clock=_Clock())
        assembler.begin()
        for frame in _word_chunks(text):
            assembler.feed(frame)
        assembler.flush()
        return assembler.text

    return call_model


def _legacy_boundary(
    messages, difficulty_hint=None, provider=None, model=None, api_key=None
):
    """A pre-streaming Boundary-2 double."""
    return "legacy reply"


class TestModelClientStreaming:
    def test_emits_model_delta_rows(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a b c"))
        try:
            client = ModelClient(trace, {"stream_enabled": True})
            out = client.call([{"role": "user", "content": "hi"}], step="step-1")
        finally:
            deps.set_call_model(None)
        assert out == "a b c"
        assert "model_delta" in trace.kinds()
        deltas = [data for kind, data in trace.rows if kind == "model_delta"]
        assert "".join(str(row["delta"]) for row in deltas) == "a b c"
        assert all(row["chars"] > 0 for row in deltas)
        assert all(row["step"] == "step-1" for row in deltas)
        assert [row["window"] for row in deltas] == list(range(1, len(deltas) + 1))
        assert client.stream_deltas == len(deltas)
        assert client.stream_chars == 5

    def test_model_request_precedes_the_deltas(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("x y"))
        try:
            ModelClient(trace, {"stream_enabled": True}).call(
                [{"role": "user", "content": "hi"}], step="step-1"
            )
        finally:
            deps.set_call_model(None)
        kinds = trace.kinds()
        assert kinds[0] == "model_request"
        assert kinds[-1] == "model_response"
        assert kinds.index("model_delta") > 0

    def test_stream_is_actually_requested_of_the_boundary(self):
        import harness.deps as deps

        seen: list[dict] = []
        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a", log=seen))
        try:
            ModelClient(trace, {"stream_enabled": True}).call(
                [{"role": "user", "content": "hi"}], step="s"
            )
        finally:
            deps.set_call_model(None)
        assert seen and seen[0]["stream"] is True and seen[0]["has_sink"] is True

    def test_off_arm_emits_no_deltas(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a b"))
        try:
            client = ModelClient(trace, {"stream_enabled": False})
            out = client.call([{"role": "user", "content": "hi"}], step="step-1")
        finally:
            deps.set_call_model(None)
        assert out == "a b"
        assert "model_delta" not in trace.kinds()
        assert client.model_calls[-1]["streamed"] is False

    def test_per_call_override_beats_the_client_default(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a b"))
        try:
            client = ModelClient(trace, {"stream_enabled": False})
            out = client.call(
                [{"role": "user", "content": "hi"}], step="step-1", stream=True
            )
        finally:
            deps.set_call_model(None)
        assert out == "a b"
        assert "model_delta" in trace.kinds()

    def test_boundary_without_stream_support_degrades_honestly(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_legacy_boundary)
        try:
            client = ModelClient(trace, {"stream_enabled": True})
            out = client.call([{"role": "user", "content": "hi"}], step="step-1")
        finally:
            deps.set_call_model(None)
        assert out == "legacy reply"
        assert "model_delta" not in trace.kinds()
        assert client.model_calls[-1]["streamed"] is False

    def test_journal_write_failure_does_not_abort_the_call(self):
        import harness.deps as deps

        trace = _BrokenDeltaTrace()
        deps.set_call_model(_streaming_boundary("a b"))
        try:
            out = ModelClient(trace, {"stream_enabled": True}).call(
                [{"role": "user", "content": "hi"}], step="step-1"
            )
        finally:
            deps.set_call_model(None)
        assert out == "a b"
        assert "model_response" in trace.kinds()

    def test_stream_defaults_on_without_config(self):
        client = ModelClient(_RecordingTrace(), {})
        assert client.stream is True

    def test_streamed_flag_lands_in_the_call_record(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a"))
        try:
            client = ModelClient(trace, {"stream_enabled": True})
            client.call([{"role": "user", "content": "hi"}], step="step-1")
        finally:
            deps.set_call_model(None)
        assert client.model_calls[-1]["streamed"] is True
        assert client.model_calls[-1]["step"] == "step-1"
        assert "model_delta" in trace.kinds()

    def test_call_structured_streams(self):
        import harness.deps as deps

        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a b"))
        try:
            out = ModelClient(trace, {"stream_enabled": True}).call_structured(
                [{"role": "user", "content": "hi"}]
            )
        finally:
            deps.set_call_model(None)
        assert out == "a b"
        assert "model_delta" in trace.kinds()

    def test_a_kwargs_boundary_is_not_assumed_to_stream(self):
        """A `**kwargs` double must not be handed the new keyword.

        A boundary that accepts **kwargs and forwards them would accept
        `stream=True` and then pass it to something that rejects it,
        turning a liveness feature into a run-killing TypeError. So
        streaming requires an EXPLICIT declaration, not a permissive one.
        """
        import harness.deps as deps

        def forwarding(messages, **kwargs):
            return ScriptedModel(plan=[], scripts={})(messages, **kwargs)

        trace = _RecordingTrace()
        deps.set_call_model(forwarding)
        try:
            client = ModelClient(trace, {"stream_enabled": True})
            out = client.call([{"role": "user", "content": "hi"}], step="step-1")
        finally:
            deps.set_call_model(None)
        assert isinstance(out, str)
        assert "model_delta" not in trace.kinds()
        assert client.model_calls[-1]["streamed"] is False

    def test_a_declaring_boundary_still_streams(self):
        import harness.deps as deps

        seen: list[dict] = []
        trace = _RecordingTrace()
        deps.set_call_model(_streaming_boundary("a b", log=seen))
        try:
            ModelClient(trace, {"stream_enabled": True}).call(
                [{"role": "user", "content": "hi"}], step="step-1"
            )
        finally:
            deps.set_call_model(None)
        assert seen[0]["stream"] is True
        assert "model_delta" in trace.kinds()

    def test_the_capability_check_reads_the_signature(self):
        from tests.fake_model import ScriptedModel

        client = ModelClient(_RecordingTrace(), {})

        def declares(messages, stream=False, on_delta=None, **rest):
            return ""

        def permissive(messages, **kwargs):
            return ""

        assert client._boundary_streams(ScriptedModel(plan=[], scripts={})) is False
        assert client._boundary_streams(permissive) is False
        assert client._boundary_streams(declares) is True

    def test_default_window_matches_the_runtime_constant(self):
        import harness.model_client as mc

        assert mc.DEFAULT_STREAM_WINDOW_MS == streaming.MIN_WINDOW_MS

    def test_config_defaults_expose_the_stream_knobs(self):
        from harness.config import DEFAULTS

        assert DEFAULTS["stream_enabled"] is True
        assert DEFAULTS["stream_window_ms"] == 40
