"""Report-first, exit-code-second test-result parsing (Ceiling 08 §5).

The previous verifier decided pass/fail from the process exit code plus a hand
rolled set of prose substrings. That is two failure modes waiting to happen:

1. A run that collected ZERO tests exits 0 for several runners, and "no tests"
   prose varies by runner, locale, and version.
2. A TIMEOUT is not a failure and not a pass. It is a third outcome, because a
   test that sometimes hangs is flaky by definition.

This module makes the decision machine-checkable and states where the evidence
came from, so a caller can tell a high-confidence report-backed verdict from a
prose guess:

- ``source="report"``  a machine-readable report (JUnit XML / pytest JSON)
  was parsed; counts are authoritative.
- ``source="exit_code"`` no report was available, the process exited 0, and
  the collector produced at least one test.
- ``source="prose"``  only human-readable output was available and it was
  parsed with the conservative digit-boundary rules. Lowest confidence.

:func:`parse_test_run` never raises. A malformed report degrades to the
exit-code path rather than failing the run, and every degradation is recorded
in ``notes`` so the trace shows that the report was unreadable instead of
silently pretending it was read.

Zero collected tests is ``no_tests``, never ``pass`` — including when
``expected_tests`` is 0 and the output is completely empty, because a
collector that was renamed out of the way produces exactly that shape.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Timeout exit codes. 124 is GNU `timeout`/the sandbox convention; 137 is
#: SIGKILL (OOM or a hard kill); -9/-15 appear when a shell reports a signalled
#: child as a negative status.
TIMEOUT_EXIT_CODES: Tuple[int, ...] = (124, 137, -9, -15, 152)

#: Runner exit codes that mean "the run itself was broken", not "tests failed".
RUNNER_ERROR_EXIT_CODES: Tuple[int, ...] = (2, 3, 4, 5)

#: pytest's documented "no tests collected" exit code.
NO_TESTS_COLLECTED_EXIT = 5

OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NO_TESTS = "no_tests"
OUTCOME_ERROR = "error"

#: Only these outcomes are ever acceptable as a passing verification.
PASSING_OUTCOMES: Tuple[str, ...] = (OUTCOME_PASS,)

_NO_TEST_MARKERS: Tuple[str, ...] = (
    "no tests ran",
    "no tests were collected",
    "no tests found",
    "no test files found",
    "no tests matched",
    "no tests collected",
)

_PYTEST_EXIT_DOC = (
    "0=passed 1=failures 2=interrupted 3=internal-error 4=usage-error "
    "5=no-tests-collected"
)

#: Shapes that mean the RUN ITSELF broke rather than a test failing. A
#: traceback, pytest's INTERNALERROR banner, or an unhandled OSError with no
#: evidence that any test ran is an evaluator failure, and reading it as "1
#: failed" invents a regression that never happened.
_CRASH_MARKERS: Tuple[str, ...] = (
    "traceback (most recent call last)",
    "internalerror",
    "pluggy.diag",
    "oserror",
    "input/output error",
    "error collecting",
)


@dataclass(frozen=True)
class TestRunReport:
    """A machine-checkable verdict for one test run.

    ``outcome`` is one of ``pass`` / ``fail`` / ``timeout`` / ``no_tests`` /
    ``error``. ``source`` records which evidence produced it and ``confidence``
    is derived from that, so a caller can refuse to accept a low-confidence
    pass where a report was expected.
    """

    outcome: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    tests_collected: Optional[int] = None
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    tests_skipped: Optional[int] = None
    source: str = "exit_code"
    confidence: str = "low"
    notes: Tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Return True only for an evidence-backed pass."""
        return self.outcome in PASSING_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view for trace/evidence records."""
        return {
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "timed_out": bool(self.timed_out),
            "tests_collected": self.tests_collected,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tests_skipped": self.tests_skipped,
            "source": self.source,
            "confidence": self.confidence,
            "passed": self.passed,
            "notes": list(self.notes),
        }


@dataclass
class _ProseCounts:
    """Counts scraped from human-readable runner output."""

    passed: Optional[int] = None
    failed: Optional[int] = None
    skipped: Optional[int] = None
    errors: Optional[int] = None
    collected: Optional[int] = None
    zero_marker: bool = False


def parse_test_run(
    result: Any,
    *,
    junit_xml: Optional[str] = None,
    json_report: Optional[str] = None,
    expected_tests: Optional[int] = None,
    extra_output: Optional[str] = None,
) -> TestRunReport:
    """Return a :class:`TestRunReport` for one sandbox run.

    ``result`` is an ``ExecutionResult``-shaped object (``exit_code``,
    ``timed_out``, ``stdout``, ``stderr``); duck typing keeps the parser usable
    from tests without constructing the shared dataclass. Evidence order is
    machine-readable report -> exit code -> prose, and the winning source is
    recorded on the report.

    A timeout is detected FIRST and is always its own outcome, so a timeout can
    never be read as a clean pass (a hang that exits 0 is a broken runner, not
    a passing test).
    """
    notes: List[str] = []
    exit_code = _coerce_int(getattr(result, "exit_code", None))
    timed_out = bool(getattr(result, "timed_out", False)) or exit_code in (
        TIMEOUT_EXIT_CODES
    )
    output = _output_text(result, extra_output)

    if timed_out:
        notes.append("timeout is a distinct outcome, not a pass or a fail")
        return TestRunReport(
            outcome=OUTCOME_TIMEOUT,
            exit_code=exit_code,
            timed_out=True,
            source="exit_code",
            confidence="high",
            notes=tuple(notes),
        )

    report_counts = _report_counts(junit_xml, json_report, notes)
    prose = _parse_prose_counts(output)

    if report_counts is not None:
        return _verdict_from_counts(
            report_counts,
            exit_code=exit_code,
            source="report",
            expected_tests=expected_tests,
            notes=notes,
        )

    if exit_code in RUNNER_ERROR_EXIT_CODES and exit_code != NO_TESTS_COLLECTED_EXIT:
        return TestRunReport(
            outcome=OUTCOME_ERROR,
            exit_code=exit_code,
            source="exit_code",
            confidence="high",
            notes=(*notes, f"runner error exit {exit_code}: {_PYTEST_EXIT_DOC}"),
        )

    if exit_code == NO_TESTS_COLLECTED_EXIT:
        notes.append("runner reported 'no tests collected' (exit 5)")
        return TestRunReport(
            outcome=OUTCOME_NO_TESTS,
            exit_code=exit_code,
            tests_collected=0,
            source="exit_code",
            confidence="high",
            notes=tuple(notes),
        )

    if exit_code == 0:
        collected = prose.collected
        if _zero_tests_in_prose(prose, output):
            notes.append("output reported zero collected tests")
            return TestRunReport(
                outcome=OUTCOME_NO_TESTS,
                exit_code=exit_code,
                tests_collected=0,
                tests_passed=0,
                source="exit_code",
                confidence="high",
                notes=tuple(notes),
            )
        expect = _coerce_int(expected_tests)
        if not output.strip() and collected is None:
            # An empty capture plus a zero exit is exactly what a renamed,
            # moved, or deleted collector looks like. Refusing it is the point:
            # a zero-test run is not a pass.
            notes.append(
                "zero exit with an empty capture and no collected-test evidence; "
                "treated as no_tests"
            )
            return TestRunReport(
                outcome=OUTCOME_NO_TESTS,
                exit_code=exit_code,
                tests_collected=0,
                source="exit_code",
                confidence="medium",
                notes=tuple(notes),
            )
        if expect is not None and expect >= 1 and collected in (None, 0):
            notes.append(f"expected at least {expect} test(s) but none were collected")
            return TestRunReport(
                outcome=OUTCOME_NO_TESTS,
                exit_code=exit_code,
                tests_collected=0,
                source="prose",
                confidence="medium",
                notes=tuple(notes),
            )
        return TestRunReport(
            outcome=OUTCOME_PASS,
            exit_code=0,
            tests_collected=collected,
            tests_passed=prose.passed if prose.passed is not None else collected,
            tests_failed=0,
            tests_skipped=prose.skipped,
            source="prose" if collected is not None else "exit_code",
            confidence="medium" if collected is not None else "low",
            notes=tuple(notes),
        )

    if exit_code == 1:
        collected = prose.collected
        failed = prose.failed if prose.failed is not None else 1
        skipped = prose.skipped or 0
        passed = prose.passed
        if passed is None and collected is not None:
            passed = max(0, collected - failed - skipped)
        if prose.failed is None and _looks_crashed(output):
            # A traceback / INTERNALERROR / unhandled OSError with no evidence
            # that a single test ran is a broken run, not a failing test. This
            # distinction decides whether the failure becomes an edit
            # instruction, so it is made explicitly instead of by defaulting to
            # "1 failed".
            notes.append("capture is crash-shaped with no test counts; not a failure")
            return TestRunReport(
                outcome=OUTCOME_ERROR,
                exit_code=1,
                source="exit_code",
                confidence="medium",
                notes=tuple(notes),
            )
        return TestRunReport(
            outcome=OUTCOME_FAIL,
            exit_code=1,
            tests_collected=collected,
            tests_passed=passed,
            tests_failed=failed,
            tests_skipped=prose.skipped,
            source="prose" if prose.failed is not None else "exit_code",
            confidence="medium" if prose.failed is not None else "high",
            notes=tuple(notes),
        )

    notes.append(f"unclassified exit code {exit_code!r}; treated as error")
    return TestRunReport(
        outcome=OUTCOME_ERROR,
        exit_code=exit_code,
        source="exit_code",
        confidence="medium",
        notes=tuple(notes),
    )


def parse_junit_xml(text: str) -> Optional[Dict[str, int]]:
    """Return test counts from a JUnit XML document, or None when unusable.

    Handles both a ``<testsuites>`` wrapper and a bare ``<testsuite>``, which
    are the two shapes pytest's ``--junitxml`` has used. Root-level attributes
    are preferred; a missing attribute falls back to summing child suites.
    """
    if not text or not text.strip():
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    suites: List[ET.Element]
    if root.tag == "testsuite":
        suites = [root]
    else:
        suites = [child for child in root.iter() if child.tag == "testsuite"]
    if not suites:
        return None

    def _attr(suite: ET.Element, name: str) -> Optional[int]:
        value = _coerce_int(suite.get(name))
        if value is None:
            return None
        return max(0, value)

    def _sum(name: str) -> int:
        total = 0
        seen = False
        for suite in suites:
            value = _attr(suite, name)
            if value is not None:
                total += value
                seen = True
        return total if seen else 0

    root_values = {
        "tests": _attr(root, "tests"),
        "failures": _attr(root, "failures"),
        "errors": _attr(root, "errors"),
        "skipped": _attr(root, "skipped"),
    }
    has_root_totals = any(value is not None for value in root_values.values())
    collected = (
        root_values["tests"] if root_values["tests"] is not None else _sum("tests")
    )
    if has_root_totals:
        failed = (root_values["failures"] or 0) + (root_values["errors"] or 0)
        skipped = root_values["skipped"] or 0
    else:
        failed = _sum("failures") + _sum("errors")
        skipped = _sum("skipped")
    counts = {
        "collected": collected,
        "passed": None,
        "failed": failed,
        "skipped": skipped,
    }
    if counts["collected"] == 0 and not _has_child_cases(suites):
        return None
    counts["passed"] = max(0, counts["collected"] - failed - skipped)
    return counts


def parse_pytest_json(text: str) -> Optional[Dict[str, int]]:
    """Return test counts from a pytest JSON report, or None when unusable.

    Supports the common ``{"summary": {...}}`` and flat ``{"collected": ...}``
    shapes. An unrecognized shape returns None so the caller degrades to the
    exit-code path rather than inventing counts.
    """
    if not text or not text.strip():
        return None
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, Mapping):
        return None
    summary = document.get("summary")
    source: Mapping[str, Any] = summary if isinstance(summary, Mapping) else document
    collected = _first_int(
        source, ("collected", "collected_count", "numcollected", "total")
    )
    if (
        collected is None
        and "tests" in source
        and isinstance(source["tests"], Sequence)
    ):
        tests = [entry for entry in source["tests"] if isinstance(entry, Mapping)]
        collected = len(tests) or None
    if collected is None:
        return None
    failed = _first_int(source, ("failed", "failures", "numfailures")) or 0
    passed = _first_int(source, ("passed", "numpassed"))
    skipped = _first_int(source, ("skipped", "numskipped")) or 0
    errors = _first_int(source, ("errors", "error")) or 0
    if passed is None:
        passed = max(0, collected - failed - skipped - errors)
    return {
        "collected": max(0, collected),
        "passed": max(0, passed),
        "failed": max(0, failed + errors),
        "skipped": max(0, skipped),
    }


def parse_prose_counts(output: str) -> Dict[str, Optional[int]]:
    """Return runner counts scraped from human-readable output.

    The count regexes are digit-boundary aware on purpose: ``10 passed`` must
    not match a ``0 passed`` no-tests pattern, and a count must be on the same
    output line as its unit. Exposed for callers that want the raw scrape
    without the full verdict.
    """
    counts = _parse_prose_counts(output or "")
    return {
        "passed": counts.passed,
        "failed": counts.failed,
        "skipped": counts.skipped,
        "errors": counts.errors,
        "collected": counts.collected,
        "zero_marker": counts.zero_marker,
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _verdict_from_counts(
    counts: Mapping[str, int],
    *,
    exit_code: Optional[int],
    source: str,
    expected_tests: Optional[int],
    notes: List[str],
) -> TestRunReport:
    """Turn authoritative report counts into a verdict."""
    collected = int(counts.get("collected") or 0)
    failed = int(counts.get("failed") or 0)
    skipped = int(counts.get("skipped") or 0)
    passed = counts.get("passed")
    passed_int = (
        int(passed) if passed is not None else max(0, collected - failed - skipped)
    )
    if collected == 0:
        notes.append("machine-readable report recorded zero collected tests")
        return TestRunReport(
            outcome=OUTCOME_NO_TESTS,
            exit_code=exit_code,
            tests_collected=0,
            tests_passed=0,
            source=source,
            confidence="high",
            notes=tuple(notes),
        )
    if expected_tests is not None and collected < max(0, int(expected_tests)):
        notes.append(
            f"collected {collected} tests, fewer than the {int(expected_tests)} expected"
        )
    if failed or passed_int <= 0:
        outcome = OUTCOME_FAIL
    elif exit_code not in (0, None, 1):
        outcome = OUTCOME_ERROR
    else:
        outcome = OUTCOME_PASS
    return TestRunReport(
        outcome=outcome,
        exit_code=exit_code,
        tests_collected=collected,
        tests_passed=passed_int,
        tests_failed=failed,
        tests_skipped=skipped,
        source=source,
        confidence="high",
        notes=tuple(notes),
    )


def _report_counts(
    junit_xml: Optional[str], json_report: Optional[str], notes: List[str]
) -> Optional[Dict[str, int]]:
    """Return counts from whichever machine-readable report is usable."""
    if junit_xml:
        counts = parse_junit_xml(junit_xml)
        if counts is not None:
            return counts
        notes.append("JUnit XML report was unparseable; falling back to exit code")
    if json_report:
        counts = parse_pytest_json(json_report)
        if counts is not None:
            return counts
        notes.append("JSON test report was unparseable; falling back to exit code")
    return None


def _parse_prose_counts(output: str) -> _ProseCounts:
    """Scrape counts out of runner prose with digit-boundary-safe patterns."""
    lowered = output.lower()
    counts = _ProseCounts()
    counts.failed = _count(output, ("failed", "failure"))
    counts.passed = _count(output, ("passed", "passing"))
    counts.skipped = _count(output, ("skipped", "skip"))
    counts.errors = _count(output, ("errors", "error"))
    counts.collected = _count(output, ("collected",))
    if counts.collected is None:
        total = (
            (counts.passed or 0)
            + (counts.failed or 0)
            + (counts.skipped or 0)
            + (counts.errors or 0)
        )
        counts.collected = total or None
    counts.zero_marker = bool(_ZERO_COUNT_RE.search(lowered)) or any(
        marker in lowered for marker in _NO_TEST_MARKERS
    )
    return counts


def _count(output: str, words: Sequence[str]) -> Optional[int]:
    """Return the count attached to the first matching word on one line."""
    if not output:
        return None
    for word in words:
        pattern = re.compile(r"(?<![\w.])(\d{1,9})\s+" + re.escape(word) + r"(?![\w])")
        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                return int(match.group(1))
    return None


def _looks_crashed(output: str) -> bool:
    """Return whether a capture shows a broken run rather than a test result.

    Deliberately conservative: any real count line (``N passed`` / ``N failed`` /
    ``N errors``) means the runner got far enough to enumerate tests, so the
    capture is a RESULT even when a traceback is also present.
    """
    lowered = (output or "").lower()
    if not any(marker in lowered for marker in _CRASH_MARKERS):
        return False
    return not any(
        _count(output, word) is not None
        for word in ("passed", "failed", "failure", "errors", "error", "collected")
    )


# Only a standalone, digit-bounded zero count means "nothing ran". `10 passed`
# contains "0 passed" as a substring, which is why the lookbehind/lookahead and
# the same-line requirement both exist.
_ZERO_COUNT_RE = re.compile(r"(?<![\d.])0[ \t]+(?:passed|tests?)(?![\w])")


def _zero_tests_in_prose(counts: _ProseCounts, output: str) -> bool:
    """Return whether the output positively states that no test ran."""
    lowered = output.lower()
    if any(marker in lowered for marker in _NO_TEST_MARKERS):
        return True
    return bool(_ZERO_COUNT_RE.search(lowered))


def _has_child_cases(suites: Sequence[ET.Element]) -> bool:
    """Return whether any suite element lists explicit testcase children."""
    for suite in suites:
        for child in suite:
            if child.tag == "testcase":
                return True
    return False


def _first_int(source: Mapping[str, Any], keys: Sequence[str]) -> Optional[int]:
    """Return the first coercible integer among ``keys``."""
    for key in keys:
        if key in source:
            value = _coerce_int(source.get(key))
            if value is not None:
                return value
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """Return ``value`` as an int, or None when it is not integral."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _output_text(result: Any, extra_output: Optional[str]) -> str:
    """Return the combined stdout/stderr (plus any extra) of a run.

    DECLARED SAFE, WITH THE REASON — this is the entry on the T2.W1.3
    untrusted-content table that is neither fenced nor a request, and saying
    why is the point of the table.

    The input is untrusted (a test run's own output). It is declared safe
    here because this function is a VIEW, not an egress: the string it returns
    is consumed only by ``_parse_prose_counts`` / ``_zero_tests_in_prose`` /
    ``_looks_crashed``, which extract integers, booleans and a crash verdict
    and never pass the text onward. Nothing downstream of here renders a
    character of it to a model, a journal or a terminal — the transcript
    reaches those through ``execution.ingress`` on the way out of
    ``execute_sandboxed`` instead. So a hostile test output cannot speak to
    the model from this path, and adding redaction here would buy nothing
    while costing a second pass over a megabyte.

    WHAT THIS VIEW IS NOT, stated because it is the real risk in the class: a
    test that PRINTS "10 passed" is counted as 10 passing tests. That is an
    untrusted-INPUT-INTEGRITY problem (a forgeable measurement), not an
    injection problem, and it is a different boundary with a different
    answer. ``execution/ecosystems.py`` already answers it in the right
    direction — the machine-readable report is consulted FIRST and a prose
    count is only a fallback with a recorded ``confidence`` — and the
    fail-closed ``no_tests_collected`` gate refuses the vacuous green. The
    remaining gap is that the report channel is only wired for ecosystems that
    declare one; for a Python repository the prose count is still the
    authority. That gap is recorded in ``execution/AGENTS.md`` and is a
    verifier-policy question, not a boundary question.
    """
    parts: List[str] = []
    for name in ("stdout", "stderr"):
        value = getattr(result, name, None)
        if isinstance(value, str):
            parts.append(value)
    if extra_output:
        parts.append(extra_output)
    return "\n".join(parts)
