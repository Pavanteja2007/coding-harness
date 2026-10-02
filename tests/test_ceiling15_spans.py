"""GenAI span semantics: emit, correlate, and reconstruct a whole run.

Covers the Terminal 15 requirement that ``traceview`` can reconstruct a
complete lifecycle from correlated spans, and that an incomplete run is
reported as incomplete rather than quietly short.
"""

from __future__ import annotations

import json

import pytest

from shared import traceview, tracing

ALL_KINDS = ("model", "tool", "retrieval", "verify", "routing", "cost")


@pytest.fixture()
def trace_root(tmp_path, monkeypatch):
    """A private unified trace root, with the module cache reset around it."""
    root = tmp_path / "logs"
    root.mkdir()
    monkeypatch.setenv(tracing.TRACE_ENV, str(root))
    tracing._reset_cache()
    yield root
    tracing._reset_cache()


def test_every_required_span_kind_reconstructs_a_run(trace_root):
    for kind in ALL_KINDS:
        with tracing.span(
            kind,
            f"{kind}.op",
            task_id="t-1",
            run_id="r-1",
            session_id="s-1",
            model="m-1",
        ):
            pass
    spans = tracing.read_genai_spans("t-1")
    kinds = {span["kind"] for span in spans}
    assert kinds == set(ALL_KINDS)

    lifecycle = traceview.span_lifecycle(
        traceview.reconstruct_spans("t-1", logs_root=trace_root)
    )
    assert lifecycle["missing_kinds"] == []
    assert lifecycle["open_span_count"] == 0
    assert lifecycle["explicit_span_count"] == len(ALL_KINDS)
    assert lifecycle["reconstructable"] is True


def test_every_span_correlates_run_task_session_and_model(trace_root):
    with tracing.span(
        "model", "chat", task_id="t-2", run_id="r-2", session_id="s-2", model="m-2"
    ):
        pass
    spans = tracing.read_genai_spans("t-2")
    assert spans
    for span in spans:
        assert span["task_id"] == "t-2"
        assert span["run_id"] == "r-2"
        assert span["session_id"] == "s-2"
        assert span["model"] == "m-2"
        assert len(span["trace_id"]) == 32
        assert len(span["span_id"]) == 16
    assert len({span["trace_id"] for span in spans}) == 1


def test_a_span_without_an_end_is_reported_open_not_dropped(trace_root):
    tracing.emit_span_start(
        "tool", "read", task_id="t-3", session_id="s-3", model="m-3"
    )
    spans = tracing.read_genai_spans("t-3")
    assert len(spans) == 1
    assert spans[0]["open"] is True
    assert spans[0]["end_ts"] is None
    lifecycle = traceview.span_lifecycle(
        traceview.reconstruct_spans("t-3", logs_root=trace_root)
    )
    assert lifecycle["open_span_count"] == 1
    assert lifecycle["reconstructable"] is False


def test_a_failing_block_is_recorded_as_an_error_span_and_still_raises(trace_root):
    with pytest.raises(RuntimeError, match="boom"):
        with tracing.span(
            "verify", "final_verify", task_id="t-4", session_id="s", model="m"
        ):
            raise RuntimeError("boom")
    spans = tracing.read_genai_spans("t-4")
    assert len(spans) == 1
    assert spans[0]["status"] == "error"
    assert spans[0]["attributes"].get("error_class") == "RuntimeError"


def test_an_unknown_span_kind_is_normalized_not_silently_dropped(trace_root):
    tracing.emit_span_start("not-a-kind", "x", task_id="t-5", session_id="s", model="m")
    tracing.emit_span_end("not-a-kind", "x", task_id="t-5", session_id="s", model="m")
    spans = tracing.read_genai_spans("t-5")
    assert len(spans) == 1
    assert spans[0]["kind"] in tracing.GENAI_SPAN_KINDS


def test_legacy_trace_events_still_reconstruct_as_labelled_derived_spans(trace_root):
    task_dir = trace_root / "t-6"
    task_dir.mkdir(parents=True)
    (task_dir / "trace.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"kind": "task_start", "ts": 1.0, "data": {"session_id": "s-6"}},
                {
                    "kind": "model_request",
                    "ts": 1.1,
                    "data": {"step": 1, "model": "m-6"},
                },
                {"kind": "tool_call", "ts": 1.2, "data": {"command": "pytest"}},
                {"kind": "final_verify", "ts": 1.3, "data": {"target_passed": True}},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    tracing._set_fallback_dir(trace_root)
    spans = traceview.reconstruct_spans("t-6", logs_root=trace_root)
    derived = [span for span in spans if span["origin"] == "derived"]
    assert {span["kind"] for span in derived} == {"model", "tool", "verify"}
    lifecycle = traceview.span_lifecycle(spans)
    assert lifecycle["explicit_span_count"] == 0
    assert lifecycle["derived_span_count"] == len(derived)
    # A legacy-only run is honestly not a correlated span reconstruction.
    assert lifecycle["reconstructable"] is False


def test_traceview_cli_renders_the_span_lifecycle_and_otlp(trace_root, capsys):
    for kind in ALL_KINDS:
        with tracing.span(kind, kind, task_id="t-7", session_id="s-7", model="m-7"):
            pass
    assert traceview.main(["t-7", "--spans", "--logs-root", str(trace_root)]) == 0
    assert "lifecycle:" in capsys.readouterr().out

    assert traceview.main(["t-7", "--lifecycle", "--logs-root", str(trace_root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reconstructable"] is True

    assert traceview.main(["t-7", "--otlp", "--logs-root", str(trace_root)]) == 0
    otlp = json.loads(capsys.readouterr().out)
    assert otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_span_emission_never_raises_into_the_caller(trace_root, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tracing, "_write", boom)
    with tracing.span("model", "chat", task_id="t-8", session_id="s", model="m"):
        pass
    assert tracing.read_genai_spans("t-8") == []


def test_spans_are_inert_when_tracing_is_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(tracing.TRACE_ENV, raising=False)
    tracing._reset_cache()
    with tracing.span("model", "chat", task_id="t-9", session_id="s", model="m"):
        pass
    assert tracing.read_genai_spans("t-9") == []
    assert traceview.span_lifecycle([])["reconstructable"] is False
