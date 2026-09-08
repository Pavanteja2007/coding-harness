"""Rationale log: one human-readable paragraph per task (spec item 29).

Given a task's structured trace (harness's trace.jsonl + state.json), build
a one-paragraph explanation: WHAT was wrong, WHAT changed, and WHY —
grounded strictly in the recorded trace, never invented. Deterministic by
design (no model call): the trace already contains every fact needed. A
model-polished variant can layer on later (harness could call
runtime.call_model with this paragraph as draft) — but the source of truth
stays the trace.

Assumes Terminal 1's trace schema (harness/trace.py):
- trace.jsonl events: {ts, kind, data} with kinds including task_start,
  baseline_verify, plan, tool_call, tool_result, verify, step_end,
  final_verify, attempt_end, task_end, result.
- state.json: {task_id, plan, completed_steps, files_touched, decisions,
  remaining_plan} (INTERFACES.md Boundary 4 schema).

Output shape (what() / why() / changed() feed git_output's PR description):
    One fix paragraph, 3-6 sentences.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_trace(log_dir: str) -> List[Dict[str, Any]]:
    """Read logs/{task_id}/trace.jsonl into a list of event dicts.

    Assumes log_dir contains trace.jsonl as written by harness.trace
    TraceLogger (one JSON object per line). Skips blank/corrupt lines
    rather than failing — a rationale is best-effort by nature.
    """
    path = Path(log_dir, "trace.jsonl")
    if not path.is_file():
        return []
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def load_state(log_dir: str) -> Dict[str, Any]:
    """Read logs/{task_id}/state.json (Boundary 4 schema); {} if absent."""
    path = Path(log_dir, "state.json")
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _first_failed_assertion(raw_output: str) -> Optional[str]:
    """Extract the first failed test name + short reason from pytest output.

    Assumes raw_output is pytest console output (the 'verify' events' raw
    field). Returns e.g. "test_pop_empty_raises_stackemptyerror" or None.
    """
    m = re.search(r"_{3,}\s*(\w*test\w*)\s*_{3,}", raw_output)
    if m:
        return m.group(1)
    m = re.search(r"FAILED\s+([\w/.\[\]::]+)", raw_output)
    if m:
        return m.group(1)
    return None


def _error_excerpt(raw_output: str, limit: int = 160) -> str:
    """Short human excerpt of the first error line in pytest output."""
    m = re.search(r"^E\s+(\w[^\\\n]*)", raw_output, re.MULTILINE)
    if m:
        return m.group(1).strip()[:limit]
    m = re.search(r"AssertionError:\s*(.+)", raw_output)
    if m:
        return m.group(1).strip()[:limit]
    m = re.search(r"(\w+Error):\s*(.+)", raw_output)
    if m:
        return f"{m.group(1)}: {m.group(2)}"[:limit]
    return ""


def build_rationale(
    log_dir: str,
    issue_text: Optional[str] = None,
) -> str:
    """Compose the one-paragraph rationale from a task's trace directory.

    Assumes log_dir is logs/{task_id}/ written by harness.core.run_task
    (trace.jsonl + state.json). issue_text may be passed explicitly (the
    Task object has it; the trace logs it too — explicit wins if given).
    Returns "" when the trace is missing/empty (caller omits the section).
    """
    events = load_trace(log_dir)
    if not events:
        return ""
    state = load_state(log_dir)

    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        by_kind.setdefault(ev.get("kind", "?"), []).append(ev)

    start = by_kind.get("task_start", [{}])[0].get("data", {})
    if issue_text is None:
        issue_text = start.get("issue_text") or ""
    task_end = by_kind.get("task_end", [{}])
    final_status = (task_end[-1].get("data") or {}).get("status") if task_end else None

    files = list(state.get("files_touched") or [])
    decisions = list(state.get("decisions") or [])
    attempts = len(by_kind.get("attempt_start", []))

    # What was wrong: first failing verification (baseline or earliest
    # verify) carries the pre-fix failure signal.
    wrong_bits: List[str] = []
    first_verify = by_kind.get("baseline_verify", [None])
    if first_verify and first_verify[0]:
        raw = (first_verify[0].get("data") or {}).get("raw", "")
        failed_test = _first_failed_assertion(raw)
        excerpt = _error_excerpt(raw)
        if failed_test:
            wrong_bits.append(f"{failed_test} was failing")
            if excerpt:
                wrong_bits.append(f"({excerpt})")

    # What changed: files touched (state.json) are the concrete record.
    changed_bits: List[str] = []
    if files:
        shown = ", ".join(f"`{f}`" for f in files[:5])
        more = f" (+{len(files) - 5} more)" if len(files) > 5 else ""
        changed_bits.append(f"changed {shown}{more}")

    # Why: recorded decisions are the harness's own account of reasoning.
    why_bits: List[str] = []
    for d in decisions[:3]:
        text = str(d).strip()
        if text:
            why_bits.append(text.rstrip("."))

    verdict = {
        "success": "The fix was verified: the target test now passes and "
                   "the full suite shows no regressions",
        "failed": "The task ended without a verified fix",
        "error": "The task ended with an internal error",
        "timeout": "The task hit its wall-clock limit",
    }.get(str(final_status), "The task ended")

    sentences: List[str] = []
    head = "The issue was that"
    if wrong_bits:
        sentences.append(f"{head} {' '.join(wrong_bits)}.")
    elif issue_text:
        first = _first_sentence(issue_text)
        if first:
            sentences.append(f"The reported problem: {first}.")
    if changed_bits:
        sentences.append(f"The fix {'; '.join(changed_bits)}.")
    if why_bits:
        sentences.append("Reasoning: " + "; ".join(why_bits) + ".")
    sentences.append(f"{verdict} (after {max(attempts, 1)} attempt(s)).")
    return " ".join(s.strip() for s in sentences if s.strip())


def _first_sentence(text: str) -> str:
    """First sentence (or first 120 chars) of a text block."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.match(r"(.{0,120}?[.!?])\s", text + " ")
    if m:
        return m.group(1)
    return text.splitlines()[0][:120]
