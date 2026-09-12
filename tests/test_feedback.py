"""Tests for execution.feedback — the FeedbackObject contract (Boundary 7).

Unit tests parse REAL pytest failure output lifted verbatim from this
harness's own logs/ traces (assert-mismatch, exception-in-source, list-diff,
deep where-chains — the actual shapes verify() produces). Docker-gated e2e
tests run the real verify() end-to-end and assert structured_feedback is
populated on failing runs and [] on passing runs.
"""

import subprocess
from pathlib import Path

import pytest

import execution.verify as vf
from execution.feedback import (
    FeedbackObject,
    parse_pytest_failures,
    to_objects,
    format_objects,
    feedback_from_result,
    FAILURE_TYPES,
)
from shared.types import VerificationResult

# ---------------------------------------------------------------------------
# REAL fixtures — verbatim failure output from logs/ traces
# ---------------------------------------------------------------------------

# logs/fix-0c656636/trace.jsonl baseline_verify: plain assert mismatch with
# where-decorations (mean() returned the sum, not the mean).
RAW_ASSERT_WHERE = """$ python -m pytest -q tests/test_mathutil.py::test_mean
exit=1
F                                                                        [100%]
=================================== FAILURES ===================================
__________________________________ test_mean ___________________________________

    def test_mean():
>       assert mathutil.mean([1, 2, 3, 4]) == 2.5
E       assert 10 == 2.5
E        +  where 10 = <function mean at 0x7a10ae5476d0>([1, 2, 3, 4])
E        +    where <function mean at 0x7a10ae5476d0> = mathutil.mean

tests/test_mathutil.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_mathutil.py::test_mean - assert 10 == 2.5
1 failed in 0.29s
"""

# logs/bench-real-bug02/trace.jsonl suite run: exception raised in SOURCE
# code (not the test) — ZeroDivisionError inside mean().
RAW_EXCEPTION_SOURCE = """$ python -m pytest -q
exit=1
FF...F                                                                   [100%]
=================================== FAILURES ===================================
___________________________ test_mean_single_element ___________________________

    def test_mean_single_element():
        # len-1 == 0 here — buggy code even hits ZeroDivisionError.
>       assert mean([10]) == 10.0

tests/test_mathutil.py:13:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

values = [10]

    def mean(values: List[float]) -> float:
        \"\"\"Arithmetic mean of a non-empty list.

        >>> mean([1, 2, 3, 4])
        2.5
        \"\"\"
        if not values:
            raise ValueError("mean() of empty list")
>       return sum(values) / (len(values) - 1)
E       ZeroDivisionError: division by zero

numlib/mathutil.py:13: ZeroDivisionError
=========================== short test summary info ============================
FAILED tests/test_mathutil.py::test_mean_single_element - ZeroDivisionErr...
2 failed, 1 passed in 0.59s
"""

# logs/ablations/v4 abl-off-drop-negatives: list-diff assertion (diff
# decoration lines after the core E-line).
RAW_LIST_DIFF = """$ python -m pytest -q
exit=1
.FF                                                                      [100%]
=================================== FAILURES ===================================
___________________________ test_adjacent_negatives ____________________________

    def test_adjacent_negatives():
>       assert drop_negatives([1, -2, -3, 4]) == [1, 4]
E       assert [1, -3, 4] == [1, 4]
E         At index 1 diff: -3 != 4
E         Left contains one more item: 4
E         Use -v to get more diff

tests/test_filtering.py:9: AssertionError
=========================== short test summary info ============================
FAILED tests/test_filtering.py::test_adjacent_negatives - assert [1, -3, 4] =...
FAILED tests/test_filtering.py::test_all_negative - assert [-2] == []
2 failed, 1 passed in 0.59s
"""

# logs/ablations/v6-multirepo abl-on-arrow-weekday-boundary: datetime
# values with a long where-chain.
RAW_DATETIME_WHERE = """$ python -m pytest -q -o addopts= tests/test_regression_arrow.py tests/test_util.py
exit=1
FF....                                                                   [100%]
=================================== FAILURES ===================================
________________________ test_next_weekday_correct_day _________________________

    def test_next_weekday_correct_day():
        # epoch 1970-01-01 is a Thursday
>       assert next_weekday(datetime(1970, 1, 1), 0) == datetime(1970, 1, 5)
E       assert datetime.datetime(1970, 1, 6, 0, 0) == datetime.datetime(1970, 1, 5, 0, 0)
E        +  where datetime.datetime(1970, 1, 6, 0, 0) = next_weekday(datetime.datetime(1970, 1, 1, 0, 0), 0)
E        +    where datetime.datetime(1970, 1, 1, 0, 0) = datetime(1970, 1, 1)
E        +  and   datetime.datetime(1970, 1, 5, 0, 0) = datetime(1970, 1, 5)

tests/test_regression_arrow.py:6: AssertionError
=========================== short test summary info ============================
FAILED tests/test_regression_arrow.py::test_next_weekday_correct_day - assert...
2 failed, 4 passed in 0.93s
"""

# Class-based test (tests/test_util.py::TestUtil::test_next_weekday from the
# same arrow run) — section header + summary carry class-qualified names.
RAW_CLASS_TEST = """$ python -m pytest -q -o addopts= tests/test_regression_arrow.py tests/test_util.py
exit=1
FF....                                                                   [100%]
=================================== FAILURES ===================================
_______________________ TestUtil.test_next_weekday ____________________________

self = <tests.test_util.TestUtil object at 0x7376fda85f90>

    def test_next_weekday(self):
        # Get first Monday after epoch
>       assert util.next_weekday(datetime(1970, 1, 1), 0) == datetime(1970, 1, 5)
E       assert datetime.datetime(1970, 1, 6, 0, 0) == datetime.datetime(1970, 1, 5, 0, 0)
E        +  where datetime.datetime(1970, 1, 6, 0, 0) = <function next_weekday at 0x7376fca1d7e0>(datetime.datetime(1970, 1, 1, 0, 0), 0)
E        +    where <function next_weekday at 0x7376fca1d7e0> = util.next_weekday

tests/test_util.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_util.py::TestUtil::test_next_weekday - assert datetime.date...
2 failed, 4 passed in 0.67s
"""

# verify() joining TWO runs (target + suite) that both show the same failure —
# the dedup contract.
RAW_TWO_RUNS_SAME_FAILURE = RAW_ASSERT_WHERE + "\n\n" + RAW_ASSERT_WHERE


class TestRealTraceFixtures:
    """The parser against VERBATIM output this harness has really produced."""

    def test_plain_assert_mismatch(self):
        objs = parse_pytest_failures(RAW_ASSERT_WHERE)
        assert len(objs) == 1
        o = objs[0]
        assert o.failure_type == "assertion_mismatch"
        assert o.test_id == "tests/test_mathutil.py::test_mean"
        assert o.expected == "2.5"
        assert o.actual == "10"
        assert o.file == "tests/test_mathutil.py"
        assert o.line == 5
        assert "expected 2.5, got 10" in o.summary

    def test_exception_in_source_points_at_source_file(self):
        objs = parse_pytest_failures(RAW_EXCEPTION_SOURCE)
        assert len(objs) == 1
        o = objs[0]
        assert o.failure_type == "exception"
        # The whole POINT: file points at the SOURCE location of the raise,
        # not the test file.
        assert o.file == "numlib/mathutil.py"
        assert o.line == 13
        assert "ZeroDivisionError" in o.summary

    def test_list_diff_values(self):
        objs = parse_pytest_failures(RAW_LIST_DIFF)
        assert len(objs) == 1
        o = objs[0]
        assert o.failure_type == "assertion_mismatch"
        assert o.expected == "[1, 4]"
        assert o.actual == "[1, -3, 4]"
        assert o.file == "tests/test_filtering.py"
        assert o.line == 9

    def test_datetime_where_chain(self):
        objs = parse_pytest_failures(RAW_DATETIME_WHERE)
        assert len(objs) == 1
        o = objs[0]
        assert o.expected == "datetime.datetime(1970, 1, 5, 0, 0)"
        assert o.actual == "datetime.datetime(1970, 1, 6, 0, 0)"
        assert o.file == "tests/test_regression_arrow.py"
        assert o.line == 6

    def test_class_qualified_test_id(self):
        objs = parse_pytest_failures(RAW_CLASS_TEST)
        assert len(objs) == 1
        o = objs[0]
        # Node id from the short summary, not the truncated section header.
        assert o.test_id == "tests/test_util.py::TestUtil::test_next_weekday"
        assert o.file == "tests/test_util.py"
        assert o.line == 12

    def test_dedup_across_target_and_suite_runs(self):
        objs = parse_pytest_failures(RAW_TWO_RUNS_SAME_FAILURE)
        assert len(objs) == 1  # NOT 2 — same failure in both runs collapses


class TestFallbacks:
    """to_objects is total: every failing-run shape yields >= 1 object."""

    def test_timeout_arg_short_circuits(self):
        objs = to_objects(
            "partial output that means nothing",
            target_test="tests/x.py::test_y",
            timed_out=True,
        )
        assert len(objs) == 1
        assert objs[0].failure_type == "timeout"
        assert objs[0].test_id == "tests/x.py::test_y"

    def test_exit_124_detected_from_chunk_markers(self):
        raw = "$ python -m pytest -q tests/x.py::test_y\nexit=124 TIMEOUT\n"
        objs = to_objects(raw)
        assert len(objs) == 1
        assert objs[0].failure_type == "timeout"

    def test_collection_error(self):
        raw = (
            "$ python -m pytest -q\nexit=2\n"
            "============================= ERRORS =============================\n"
            "ERROR collecting tests/test_broken.py\n"
            "ImportError: cannot import name 'missing' from 'mod'\n"
        )
        objs = to_objects(raw)
        assert len(objs) == 1
        assert objs[0].failure_type == "collection_error"
        assert "ImportError" in objs[0].traceback_summary

    def test_unparseable_output_never_empty(self):
        objs = to_objects("some completely foreign tool output\nexit=3\n")
        assert len(objs) == 1
        assert objs[0].failure_type == "unparseable"
        assert objs[0].traceback_summary  # carries the raw tail

    def test_empty_output_never_empty(self):
        objs = to_objects("")
        assert len(objs) == 1
        assert objs[0].failure_type == "unparseable"

    def test_passing_output_no_failures(self):
        # to_objects on green output: nothing failed; parse-only returns [].
        # (Contract: the [] case only exists for runs with no failure —
        # callers gate on the verification booleans, not on the list.)
        objs = parse_pytest_failures("1 passed in 0.01s\n")
        assert objs == []


class TestContractShape:
    """Field-name stability — other terminals build against these."""

    def test_to_dict_exact_keys(self):
        o = FeedbackObject(
            test_id="a::b",
            failure_type="assertion_mismatch",
            summary="s",
            expected="1",
            actual="2",
            file="f.py",
            line=3,
            traceback_summary="tb",
            raw_tail="SECRET",
        )
        d = o.to_dict()
        assert set(d) == {
            "test_id",
            "failure_type",
            "summary",
            "expected",
            "actual",
            "file",
            "line",
            "traceback_summary",
        }
        assert "raw_tail" not in d  # diagnostic-only, never in the contract

    def test_failure_types_vocabulary(self):
        assert FAILURE_TYPES == (
            "assertion_mismatch",
            "exception",
            "collection_error",
            "timeout",
            "unparseable",
        )

    def test_feedback_from_result_replay(self):
        # A result serialized WITHOUT structured_feedback (historical trace)
        # still parses via raw_output alone.
        v = VerificationResult(
            target_test_passed=False,
            baseline_passed=False,
            regression_passed=False,
            flaky=False,
            raw_output=RAW_ASSERT_WHERE,
        )
        objs = feedback_from_result(v, target_test="tests/test_mathutil.py::test_mean")
        assert len(objs) == 1
        assert objs[0].failure_type == "assertion_mismatch"
        assert objs[0].actual == "10"

    def test_values_never_misrepresented(self):
        # hostile-length value is capped with a marker, never silently altered
        long_val = "x" * 500
        raw = (
            "=================================== FAILURES ===================================\n"
            "___________________________________ test_big ___________________________________\n\n"
            "    def test_big():\n"
            ">       assert make() == '" + long_val + "'\n"
            "E       assert '" + long_val + "' == 'y'\n"
            "\n"
            "tests/test_big.py:5: AssertionError\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_big.py::test_big - assert...\n"
            "1 failed in 0.01s\n"
        )
        objs = parse_pytest_failures(raw)
        assert len(objs) == 1
        assert objs[0].expected == "'y'"
        assert objs[0].actual is not None
        assert objs[0].actual.endswith("...(truncated)")

    def test_never_raises_on_hostile_input(self):
        for hostile in [
            "",
            "===FAILURES===",
            "____ ____",
            "$ ",
            "\x00\nE ",
            "___" * 40,
            "FAILED",
            "E assert == == ==",
        ]:
            parse_pytest_failures(hostile)  # must not raise
            to_objects(hostile)


class TestFormatting:
    """format_objects is the raw-tail replacement — compact and bounded."""

    def test_renders_compact_lines(self):
        objs = to_objects(
            RAW_ASSERT_WHERE, target_test="tests/test_mathutil.py::test_mean"
        )
        text = format_objects(objs)
        assert "FAILED tests/test_mathutil.py::test_mean" in text
        assert "assertion_mismatch" in text
        assert "expected 2.5, got 10" in text
        assert "(tests/test_mathutil.py:5)" in text
        assert len(text) < 400  # vs the 1500-char raw tail it replaces

    def test_caps_failures_with_more_marker(self):
        objs = [
            FeedbackObject(test_id=f"t{i}", failure_type="exception", summary=f"s{i}")
            for i in range(5)
        ]
        text = format_objects(objs, max_objects=3)
        assert "(+2 more failure(s))" in text
        assert "t4" not in text

    def test_empty_means_passed(self):
        assert format_objects([]) == "All checks passed."

    def test_ascii_only(self):
        # cp1252 consoles (a documented project hazard) — no smart quotes,
        # em dashes, or other non-ASCII in model-facing text.
        objs = to_objects(RAW_ASSERT_WHERE)
        format_objects(objs).encode("cp1252")  # raises if non-encodable


# ---------------------------------------------------------------------------
# Docker-gated e2e — the real verify() populates structured_feedback
# ---------------------------------------------------------------------------


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    __import__("os").environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


@requires_docker
class TestVerifyIntegration:
    def _mk_broken(self, tmp_path: Path) -> Path:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
        )
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a - b  # bug\n", encoding="utf-8"
        )
        (tmp_path / "test_mymod.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_failing_verify_populates_structured_feedback(self, tmp_path):
        repo = self._mk_broken(tmp_path)
        v = vf.verify(str(repo), "test_mymod.py::test_add", 1, verify_timeout_s=120)
        assert v.target_test_passed is False
        fb = v.structured_feedback
        assert len(fb) >= 1
        o = fb[0]
        assert set(o) == {
            "test_id",
            "failure_type",
            "summary",
            "expected",
            "actual",
            "file",
            "line",
            "traceback_summary",
        }
        assert o["failure_type"] == "assertion_mismatch"
        assert o["expected"] == "5"
        assert o["actual"] == "-1"
        assert o["file"] == "test_mymod.py"
        assert o["line"] == 4  # the '>' assertion line, verified against real output

    def test_passing_verify_leaves_structured_empty(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
        )
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (tmp_path / "test_mymod.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        v = vf.verify(str(tmp_path), "test_mymod.py::test_add", 1, verify_timeout_s=120)
        assert v.target_test_passed is True
        assert v.structured_feedback == []

    def test_regression_run_failures_included(self, tmp_path):
        # Target passes but the suite has ANOTHER failing test — the suite's
        # failure must appear in structured feedback too.
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
        )
        (tmp_path / "mymod.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n"
            "    return 0  # bug: breaks the OTHER test\n",
            encoding="utf-8",
        )
        (tmp_path / "test_target.py").write_text(
            "from mymod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (tmp_path / "test_other.py").write_text(
            "from mymod import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
            encoding="utf-8",
        )
        v = vf.verify(
            str(tmp_path), "test_target.py::test_add", 1, verify_timeout_s=120
        )
        assert v.target_test_passed is True
        assert v.regression_passed is False
        fb = v.structured_feedback
        assert len(fb) >= 1
        assert fb[0]["test_id"] == "test_other.py::test_mul"
        assert fb[0]["expected"] == "6"
        assert fb[0]["actual"] == "0"

    def test_timeout_run_classified(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
        )
        (tmp_path / "test_hang.py").write_text(
            "import time\n\ndef test_hangs():\n    time.sleep(300)\n", encoding="utf-8"
        )
        v = vf.verify(str(tmp_path), "test_hang.py::test_hangs", 1, verify_timeout_s=15)
        assert v.target_test_passed is False
        fb = v.structured_feedback
        assert len(fb) == 1
        assert fb[0]["failure_type"] == "timeout"
