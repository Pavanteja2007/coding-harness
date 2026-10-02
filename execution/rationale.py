"""Build a grounded rationale paragraph from a task trace and state."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read_text(path: Path) -> str:
    """Read a text file without allowing encoding errors to escape."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def load_trace(log_dir: str) -> List[Dict[str, Any]]:
    """Read valid JSON object events from a task trace directory."""
    path = Path(log_dir, "trace.jsonl")
    try:
        if not path.is_file():
            return []
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    try:
        lines = _read_text(path).splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def load_state(log_dir: str) -> Dict[str, Any]:
    """Read a JSON object state file, returning an empty object on error."""
    path = Path(log_dir, "state.json")
    try:
        if not path.is_file():
            return {}
    except OSError:
        return {}
    try:
        value = json.loads(_read_text(path))
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    """Convert a value to one bounded single-line string."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:4000]


def _first_failed_assertion(raw_output: Any) -> Optional[str]:
    """Extract a failed test name from pytest-style output."""
    raw = _text(raw_output)
    match = re.search(r"_{3,}\s*(\w*test\w*)\s*_{3,}", raw)
    if match:
        return match.group(1)
    match = re.search(r"FAILED\s+([\w/.\[\]:-]+)", raw)
    if match:
        return match.group(1)
    return None


def _error_excerpt(raw_output: Any, limit: int = 160) -> str:
    """Extract a short error excerpt from pytest-style output."""
    raw = _text(raw_output)
    match = re.search(r"^E\s+(\w[^\\\n]*)", raw, re.MULTILINE)
    if match:
        return match.group(1).strip()[:limit]
    match = re.search(r"AssertionError:\s*(.+)", raw)
    if match:
        return match.group(1).strip()[:limit]
    match = re.search(r"(\w+Error):\s*(.+)", raw)
    if match:
        return f"{match.group(1)}: {match.group(2)}"[:limit]
    return ""


def _event_data(event: Any) -> Dict[str, Any]:
    """Return an event's data object, or an empty object for malformed data."""
    if not isinstance(event, dict):
        return {}
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _latest_verification(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the latest final verification record, if one exists."""
    for kind in ("final_verify", "verify"):
        for event in reversed(events):
            if event.get("kind") == kind:
                return _event_data(event)
    return None


def _verification_verdict(events: List[Dict[str, Any]], final_status: Any) -> str:
    """Derive a success sentence only from explicit verification evidence."""
    record = _latest_verification(events)
    if record:
        target = record.get("target_passed", record.get("target_test_passed"))
        regression = record.get("regression_passed")
        flaky = record.get("flaky")
        if target is True and regression is True and flaky is False:
            return "The fix was verified: the target test passed and the full suite showed no regressions"
        if target is False or regression is False or flaky is True:
            return "The task ended without a verified fix"
    status = _text(final_status)
    if status == "success":
        return "The task ended without a complete verification record"
    return {
        "failed": "The task ended without a verified fix",
        "error": "The task ended with an internal error",
        "timeout": "The task hit its wall-clock limit",
    }.get(status, "The task ended")


def build_rationale(log_dir: str, issue_text: Optional[str] = None) -> str:
    """Compose a single grounded paragraph from a task trace directory.

    Missing, malformed, or non-object trace records are ignored. A success
    sentence requires an explicit final verification record whose target and
    regression gates passed and whose flake flag is false.
    """
    events = load_trace(log_dir)
    if not events:
        return ""
    state = load_state(log_dir)
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        kind = event.get("kind")
        if isinstance(kind, str):
            by_kind.setdefault(kind, []).append(event)

    start_data = _event_data(by_kind.get("task_start", [{}])[0])
    issue = _text(
        issue_text if issue_text is not None else start_data.get("issue_text")
    )
    task_end = by_kind.get("task_end", [])
    final_data = _event_data(task_end[-1]) if task_end else {}
    final_status = final_data.get("status")

    raw_values = [
        _event_data(event).get("raw") for event in by_kind.get("baseline_verify", [])
    ]
    raw_values.extend(
        _event_data(event).get("raw") for event in by_kind.get("verify", [])
    )
    wrong_bits: List[str] = []
    for raw in raw_values:
        failed_test = _first_failed_assertion(raw)
        excerpt = _error_excerpt(raw)
        if failed_test:
            wrong_bits.append(f"{failed_test} was failing")
            if excerpt:
                wrong_bits.append(f"({excerpt})")
            break

    files_value = state.get("files_touched")
    files = (
        [_text(item) for item in files_value if _text(item)]
        if isinstance(files_value, list)
        else []
    )
    decisions_value = state.get("decisions")
    decisions = (
        [_text(item) for item in decisions_value if _text(item)]
        if isinstance(decisions_value, list)
        else []
    )
    attempts = len(by_kind.get("attempt_start", []))

    sentences: List[str] = []
    if wrong_bits:
        sentences.append(f"The issue was that {' '.join(wrong_bits)}.")
    elif issue:
        first = _first_sentence(issue)
        if first:
            sentences.append(f"The reported problem: {first}.")
    if files:
        shown = ", ".join(f"`{item}`" for item in files[:5])
        more = f" (+{len(files) - 5} more)" if len(files) > 5 else ""
        sentences.append(f"The fix changed {shown}{more}.")
    if decisions:
        sentences.append("Reasoning: " + "; ".join(decisions[:3]) + ".")
    verdict = _verification_verdict(events, final_status)
    if attempts:
        verdict += f" (after {attempts} attempt(s))"
    sentences.append(verdict + ".")
    return " ".join(sentence.strip() for sentence in sentences if sentence.strip())


def _first_sentence(text: Any) -> str:
    """Return the first sentence or first 120 characters of text."""
    value = _text(text)
    if not value:
        return ""
    match = re.match(r"(.{0,120}?[.!?])\s", value + " ")
    if match:
        return match.group(1)
    return value.splitlines()[0][:120]
