"""P5/W1: vision and LSP diagnostics reach the model, honestly."""

from __future__ import annotations

import json
from types import SimpleNamespace

from harness import vision


def _plan_record(**over):
    rec = {
        "path": "docs/shot.png",
        "media_type": "image/png",
        "data_base64": (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l0k2AAAAAABJRU5ErkJggg=="
        ),
    }
    rec.update(over)
    return rec


def test_the_model_receives_a_real_multimodal_block():
    from harness.agent_kernel.conversation import ConversationMemory

    mem = ConversationMemory()
    blocks = vision.vision_blocks([_plan_record()], model="gpt-5", provider="openai")
    mem.record_image("[image] 1 attached", blocks, turn=1)
    messages = mem.render()
    image_msgs = [m for m in messages if isinstance(m.get("content"), list)]
    assert image_msgs, "expected a multimodal-message turn"
    content = image_msgs[0]["content"]
    assert isinstance(content[0], str)
    kinds = {b.get("type") for b in content[1:]}
    assert kinds == {"image_url"}, kinds


def test_supports_vision_false_sends_nothing(monkeypatch):
    import runtime.model_capabilities as mc

    monkeypatch.setattr(mc, "vision_capability_of", lambda p, m: (False, "test"))
    plan = vision.vision_plan_for([_plan_record()], model="gpt-5", provider="openai")
    assert plan.status == vision.VISION_UNSUPPORTED_MODEL
    assert plan.blocks == ()
    assert plan.blocks_payload == []


def _strategy_instance():
    from harness.agent_kernel.strategy import DailyCodingStrategy

    strat = DailyCodingStrategy.__new__(DailyCodingStrategy)
    strat.tools = SimpleNamespace(canonical_name=lambda w: w)
    strat._events = []

    def _event(kind, data, turn_id=""):
        strat._events.append((kind, data))

    strat._event = _event
    return strat


def test_lsp_unavailable_is_distinct_from_no_findings():
    # knowledge bound and lsp enabled, but no server ever opened the document.
    strat = _strategy_instance()
    strat.knowledge = lambda spec: SimpleNamespace(
        note_edit_enabled=True,
        sync_document=lambda p: False,
        collect_diagnostics=lambda p: [],
        diagnostics_note=lambda p, clear=False: "(none reported)",
        lsp_status=lambda: {
            "configured": True,
            "started": False,
            "last_error": "pylsp not found",
        },
        repo_path=".",
    )
    call = SimpleNamespace(tool="edit", arguments={"path": "a.py"}, call_id="c1")
    out = strat._observe_lsp_after_mutation(call, None)
    assert "UNAVAILABLE" in out
    assert "pylsp not found" in out
    assert "no findings" not in out


def test_the_disabled_off_arm_is_silent_and_byte_identical():
    # An explicitly disabled gate is a choice, not blindness: it must not
    # change an edit result, or a default run stops being byte-identical.
    strat = _strategy_instance()
    strat.knowledge = lambda spec: SimpleNamespace(
        note_edit_enabled=False, lsp_status=lambda: {}
    )
    call = SimpleNamespace(tool="edit", arguments={"path": "a.py"}, call_id="c2")
    assert strat._observe_lsp_after_mutation(call, None) == ""


def test_a_connected_server_reports_clean_distinctly():
    strat = _strategy_instance()
    strat.knowledge = lambda spec: SimpleNamespace(
        note_edit_enabled=True,
        sync_document=lambda p: True,
        collect_diagnostics=lambda p: [],
        diagnostics_note=lambda p, clear=False: "(none reported)",
        lsp_status=lambda: {"configured": True, "started": True},
        repo_path=".",
    )
    call = SimpleNamespace(tool="edit", arguments={"path": "a.py"}, call_id="c3")
    out = strat._observe_lsp_after_mutation(call, None)
    assert "now reports no findings" in out
    assert "UNAVAILABLE" not in out


def test_retract_preview_and_markers(tmp_path):
    import harness.retract as r

    run = tmp_path / "run"
    run.mkdir()
    (run / "trace.jsonl").write_text(
        '{"tool":"edit","input":{},"output":{}}\n{"kind":"task_end"}\n',
        encoding="utf-8",
    )
    (run / "turns.jsonl").write_text(
        '{"turn":1,"changed_files":["a.py"]}\n{"turn":2,"changed_files":["b.py"]}\n',
        encoding="utf-8",
    )
    (run / "conversation.jsonl").write_text(
        '{"turn":1,"kind":"turn","role":"assistant"}\n'
        '{"turn":2,"kind":"turn","role":"assistant"}\n',
        encoding="utf-8",
    )
    preview = r.preview_retraction(run, turn=2)
    assert preview["reverted_file_count"] == 1
    assert not r.is_retracted(run)
    assert r.may_resume(run)


def test_retract_marks_visible_preserves_history_and_blocks_resume(
    tmp_path, monkeypatch
):
    import harness.retract as r

    run = tmp_path / "run"
    run.mkdir()
    original_trace = '{"kind":"tool_call","tool":"edit"}\n{"kind":"task_end"}\n'
    (run / "trace.jsonl").write_text(original_trace, encoding="utf-8")
    # Stand in for the real rewind (which needs a live workspace) so the test
    # exercises the marker/history/resumability contract, not the file I/O.
    monkeypatch.setattr(r, "rewind_run", lambda **kw: {"restored": ["a.py"]})

    receipt = r.retract(run, turn=1, scope="both")
    assert receipt["retracted"] is True
    # the action is visible and the run cannot silently resume
    assert (run / r.RETRACTED_NAME).is_file()
    assert r.is_retracted(run) and not r.may_resume(run)
    # history was preserved, not deleted, and the retract is journalled
    rows = r.redacted_trace_rows(run)
    assert rows[0]["kind"] == "tool_call"  # original evidence still first
    assert any(row["kind"] == "retract" for row in rows)
    assert len(rows) == 3  # 2 original + 1 retract marker


def test_edit_message_records_the_new_text_for_a_rerun(tmp_path, monkeypatch):
    import harness.retract as r

    run = tmp_path / "run"
    run.mkdir()
    (run / "trace.jsonl").write_text('{"kind":"task_end"}\n', encoding="utf-8")
    (run / "conversation.jsonl").write_text(
        '{"turn":1,"kind":"turn","role":"user"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(r, "rewind_run", lambda **kw: {"restored": []})
    receipt = r.edit_message(run, user_turn=1, new_text="do the OTHER thing")
    assert receipt["edited"] is True
    stored = json.loads((run / "EDIT_MESSAGE.json").read_text(encoding="utf-8"))
    assert stored["new_text"] == "do the OTHER thing"
