"""Unit tests for the RECALL feature: reversible compaction with on-demand
reinjection (spec item 13).

A step session can output "RECALL <terms>" instead of a bash command; the
harness greps this run's trace.jsonl (TraceLogger.find_events) and re-injects
matching entries into the session (prompts.render_recall_result). These tests
cover, in-process only (no network, no Docker):

- harness.trace.TraceLogger.find_events: matching (data + kind field),
  case-insensitivity, most-recent-N limit, kinds filter, empty/no-match
  queries, per-event truncation, malformed-line tolerance, 1-based line
  numbers, missing-file safety.
- harness.tools.parse_recall / is_submit: the RECALL escape's grammar and its
  distinctness from SUBMIT.
- harness.prompts.render_recall_result: the reinjection message shape, and
  render_step_system's model-facing RECALL documentation.
- harness.config.get_config: the recall config defaults and override
  precedence.
"""
import json
from pathlib import Path
from typing import Dict, List

import pytest

from harness.trace import TraceLogger
from harness.tools import parse_recall, is_submit
from harness.prompts import render_recall_result, render_step_system
from harness.config import get_config


# ---------------------------------------------------------------------------
# TraceLogger.find_events
# ---------------------------------------------------------------------------

def test_find_events_basic_match(tmp_path: Path) -> None:
    """A query term appearing in an event's data finds that event."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("task_start", {"issue": "numbers are summed wrong"})
    t.log("tool_result", {"output": "AssertionError at numlib/mathutil.py"})
    hits = t.find_events("mathutil")
    assert len(hits) == 1
    assert hits[0]["kind"] == "tool_result"
    assert "mathutil" in hits[0]["data"]


def test_find_events_matches_event_kind_field(tmp_path: Path) -> None:
    """The query matches the kind string itself, even if absent from data."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("verify", {"target_passed": True, "regression_passed": True})
    t.log("tool_call", {"command": "cat foo.py"})
    hits = t.find_events("verify")
    assert len(hits) == 1
    assert hits[0]["kind"] == "verify"
    assert hits[0]["line"] == 1


def test_find_events_case_insensitive(tmp_path: Path) -> None:
    """Query and trace content match regardless of case, both directions."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_result", {"output": "FlakyGate detected instability"})
    assert len(t.find_events("flakygate")) == 1  # query lower, data mixed
    t2 = TraceLogger(tmp_path / "logs" / "t2")
    t2.log("tool_result", {"output": "flaky rerun needed"})
    assert len(t2.find_events("FLAKY")) == 1  # query upper, data lower


def test_find_events_limit_keeps_most_recent(tmp_path: Path) -> None:
    """limit keeps the LAST N matches chronologically, not the first N."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_call", {"command": "grep needle a.txt"})   # match (A)
    t.log("tool_call", {"command": "grep needle b.txt"})   # match (B)
    t.log("tool_call", {"command": "grep needle c.txt"})   # match (C)
    t.log("tool_call", {"command": "ls"})                  # non-match
    hits = t.find_events("needle", limit=2)
    assert len(hits) == 2
    assert hits[0]["line"] == 2  # B
    assert hits[1]["line"] == 3  # C


def test_find_events_kinds_filter(tmp_path: Path) -> None:
    """kinds restricts matches to the listed event kinds only."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_call", {"command": "grep needle a.txt"})
    t.log("verify", {"raw": "needle not found"})
    t.log("tool_result", {"output": "needle: 3 hits"})
    hits = t.find_events("needle", kinds=["tool_call", "verify"])
    assert [h["kind"] for h in hits] == ["tool_call", "verify"]
    assert all(h["kind"] != "tool_result" for h in hits)


def test_find_events_empty_query_returns_empty(tmp_path: Path) -> None:
    """Empty or whitespace-only queries never match anything."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_call", {"command": "grep needle a.txt"})
    assert t.find_events("") == []
    assert t.find_events("   ") == []
    assert t.find_events(None) == []  # type: ignore[arg-type]


def test_find_events_no_match_returns_empty(tmp_path: Path) -> None:
    """A query hitting nothing returns an empty list, not an error."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_call", {"command": "ls"})
    assert t.find_events("zebra-unicorn-term") == []


def test_find_events_truncates_long_data(tmp_path: Path) -> None:
    """Per-event data dumps are capped with a visible truncation marker.

    With limit=1 and max_chars=4000 the cap is max(4000 // 1, 200) = 4000
    chars of JSON dump, then "…[truncated]" is appended.
    """
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_result", {"blob": "x" * 5000})
    hits = t.find_events("blob", limit=1, max_chars=4000)
    assert len(hits) == 1
    assert "…[truncated]" in hits[0]["data"]
    assert len(hits[0]["data"]) <= 4000 + len("…[truncated]")


def test_find_events_truncation_cap_floors_at_200(tmp_path: Path) -> None:
    """Small max_chars still leaves each event at least 200 chars of dump."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_result", {"blob": "y" * 5000})
    hits = t.find_events("blob", limit=10, max_chars=100)  # 100//10 = 10 -> floor 200
    assert len(hits) == 1
    assert "…[truncated]" in hits[0]["data"]
    assert len(hits[0]["data"]) <= 200 + len("…[truncated]")


def test_find_events_skips_malformed_lines(tmp_path: Path) -> None:
    """A garbage JSONL line is skipped, never raised, never matched."""
    t = TraceLogger(tmp_path / "logs" / "t1")
    t.log("tool_call", {"command": "grep needle a.txt"})
    with open(tmp_path / "logs" / "t1" / "trace.jsonl", "a", encoding="utf-8") as fh:
        fh.write("not json{\n")
    t.log("tool_call", {"command": "grep needle b.txt"})
    hits = t.find_events("needle")  # must not crash
    assert len(hits) == 2
    assert "not json{" not in json.dumps(hits)


def test_find_events_line_numbers_one_based_and_correct(tmp_path: Path) -> None:
    """Returned "line" values are 1-based and point at the matching lines."""
    log_dir = tmp_path / "logs" / "t1"
    t = TraceLogger(log_dir)
    # A file with a blank first line, then events, then a malformed line:
    # exercises that line numbers count physical lines (enumerate(fh, 1)).
    with open(log_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(json.dumps({"kind": "tool_call", "data": {"command": "ls"}}) + "\n")
        fh.write("not json{\n")
        fh.write(json.dumps({"kind": "verify", "data": {"raw": "needle: pass"}}) + "\n")
    hits = t.find_events("needle")
    assert [h["line"] for h in hits] == [4]


def test_find_events_missing_file_returns_empty(tmp_path: Path) -> None:
    """find_events never raises when trace.jsonl doesn't exist yet."""
    t = TraceLogger(tmp_path / "logs" / "t1")  # dir created, file never written
    assert t.find_events("anything") == []


# ---------------------------------------------------------------------------
# parse_recall / is_submit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("RECALL pytest failures step 1", "pytest failures step 1"),
    ("recall mixed case", "mixed case"),
    ("  RECALL   spaced   ", "spaced"),
])
def test_parse_recall_accepts_recall_forms(text: str, expected: str) -> None:
    """Anchored RECALL forms yield the captured terms."""
    assert parse_recall(text) == expected


@pytest.mark.parametrize("text", [
    "RECALL",             # no terms
    "SUBMIT",             # the other escape — distinct signal
    "cat foo.py",         # a normal bash command
    "let us recall the output",  # prose: recall mid-sentence must not match
])
def test_parse_recall_rejects_non_recall(text: str) -> None:
    """Non-RECALL messages (and bare RECALL) return None."""
    assert parse_recall(text) is None


def test_parse_recall_raw_fenced_form_not_recognized() -> None:
    # CHARACTERIZATION (documented behavior): a raw fenced block like
    # "```bash\nRECALL x\n```" is NOT recognized by parse_recall — the
    # regex is anchored at the start of the (stripped) message, so the
    # fence markers defeat it. core.py strips fences only in
    # _extract_command, AFTER the recall check, so a fenced RECALL falls
    # through to command extraction in the live loop. Pinned here as-is.
    assert parse_recall("```bash\nRECALL x\n```") is None


def test_recall_is_not_submit() -> None:
    """RECALL and SUBMIT are distinct escapes; neither triggers the other."""
    assert is_submit("RECALL foo") is False
    assert is_submit("SUBMIT") is True


# ---------------------------------------------------------------------------
# render_recall_result / render_step_system
# ---------------------------------------------------------------------------

def test_render_recall_result_empty(tmp_path: Path) -> None:
    """No matches: a helpful message naming the query and mentioning SUBMIT."""
    msg = render_recall_result("flaky tail", [])
    assert "No earlier trace entries match" in msg
    assert "flaky tail" in msg


def test_render_recall_result_entries(tmp_path: Path) -> None:
    """Matches: header, per-entry trace-line headers, data, closing line."""
    entries: List[Dict] = [
        {"line": 3, "kind": "tool_call", "data": '{"command": "pytest -x"}'},
        {"line": 7, "kind": "verify", "data": '{"raw": "2 passed"}'},
    ]
    msg = render_recall_result("pytest", entries)
    assert "RECALL results for 'pytest'" in msg
    assert "--- trace line 3 [tool_call] ---" in msg
    assert "--- trace line 7 [verify] ---" in msg
    assert '{"command": "pytest -x"}' in msg
    assert '{"raw": "2 passed"}' in msg
    last_line = msg.strip().splitlines()[-1]
    assert "End of RECALL results" in last_line
    assert "SUBMIT" in last_line


def test_step_system_prompt_documents_recall() -> None:
    """The step system prompt pins the model-facing RECALL documentation."""
    plan: List[Dict] = [
        {"id": 1, "description": "locate the bug",
         "checkpoint": "test fails for the right reason", "files_hint": []},
        {"id": 2, "description": "fix the bug",
         "checkpoint": "target test passes", "files_hint": []},
    ]
    prompt = render_step_system(
        issue_text="sums are wrong",
        plan=plan,
        step_id=2,
        total_steps=2,
        completed_block="(none yet)",
        context_block="(none)",
        max_output_chars=3000,
    )
    assert "Recovering compacted-away context (RECALL)" in prompt
    assert "RECALL <search terms>" in prompt


# ---------------------------------------------------------------------------
# get_config recall keys
# ---------------------------------------------------------------------------

def test_config_recall_defaults() -> None:
    """get_config({}) ships the spec'd recall defaults, all ints."""
    cfg = get_config({})
    assert cfg["max_recalls_per_step"] == 3
    assert cfg["recall_results_cap"] == 5
    assert cfg["recall_max_chars"] == 4000
    assert isinstance(cfg["max_recalls_per_step"], int)
    assert isinstance(cfg["recall_results_cap"], int)
    assert isinstance(cfg["recall_max_chars"], int)


def test_config_task_overrides_win() -> None:
    """task.config values override the defaults for recall knobs."""
    cfg = get_config({"max_recalls_per_step": 1})
    assert cfg["max_recalls_per_step"] == 1
    assert cfg["recall_results_cap"] == 5  # untouched default
