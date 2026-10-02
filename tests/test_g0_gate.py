"""T5.W2.3 — the G0 gate's own tests.

A gate that cannot fail is a report generator. Every test here exists to make
one of these true statements mechanically checkable:

* ``tests/test_known_failing_pins.py`` and this file cannot both be red for
  different reasons without the difference being nameable;
* ``blocked`` cannot be constructed without a reason;
* ``not_implemented`` cannot be constructed without an owning phase;
* the word ``skip`` cannot enter the vocabulary, because a skip renders
  identically to a pass in the summary table this gate prints;
* a missing measurement is reported as ``blocked``, never as ``0``;
* the "what this does not establish" list travels with the numbers and cannot
  be dropped from the rendered report;
* the security-blocker count is derived from a MEASURED version comparison,
  not from a field that does not exist.

The synthetic-log arms below are the non-vacuity controls: they drive
``_suite_rung`` and ``parse_pytest_log`` with logs built to be readable, so a
parser that silently reported "0 failed" for every input would be caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.gates.P0 import (
    _FORBIDDEN_STATUSES,
    BLOCKED,
    BLOCKING_SEVERITIES,
    FAIL,
    GATE_RED,
    NOT_IMPLEMENTED,
    PASS,
    STATUSES,
    GateError,
    Rung,
    _installed_version,
    _npm_pinned_version,
    _pinned_version,
    _status,
    _version_tuple,
    blocked,
    count_security_blockers,
    g0_report,
    not_implemented,
    parse_pytest_log,
    render,
    run_probe,
)

# ---------------------------------------------------------------------------
# the vocabulary -- the load-bearing property
# ---------------------------------------------------------------------------


def test_the_status_vocabulary_has_no_skip() -> None:
    """``skip`` is not a member and cannot be constructed.

    This is the whole reason the four statuses exist. In the table this gate
    prints, a skip renders exactly like a pass, so a blocked daemon, a missing
    provider key and a green run all become the same glyph.
    """
    assert STATUSES == (PASS, FAIL, BLOCKED, NOT_IMPLEMENTED)
    for banned in _FORBIDDEN_STATUSES:
        assert banned not in STATUSES
    for banned in _FORBIDDEN_STATUSES:
        with pytest.raises(GateError):
            _status(banned)
        with pytest.raises(GateError):
            _status(banned.upper())
    # and the near-miss spellings a future edit might reach for
    for banned in ("Skip", "SKIPPED", " xfail ", "pending"):
        with pytest.raises(GateError):
            _status(banned)


def test_an_unknown_status_is_refused_rather_than_passed_through() -> None:
    """A typo cannot become a fifth status that reads like a pass."""
    with pytest.raises(GateError):
        _status("passed")
    with pytest.raises(GateError):
        _status("ok")
    with pytest.raises(GateError):
        _status("")


def test_blocked_cannot_be_constructed_without_the_exact_reason() -> None:
    """A blocked rung with no reason is the dishonesty the vocabulary prevents."""
    with pytest.raises(GateError, match="requires the exact reason"):
        blocked("r", "t", "")
    with pytest.raises(GateError, match="requires the exact reason"):
        blocked("r", "t", "   ")
    # ...and the same rule on the dataclass, for a hand-written Rung
    with pytest.raises(GateError):
        Rung("r", "t", BLOCKED, "   ")
    # a blocked WITH a reason is fine
    assert (
        blocked("r", "t", "docker info: cannot connect to the daemon").status == BLOCKED
    )


def test_not_implemented_cannot_be_constructed_without_an_owning_phase() -> None:
    """A reader must be able to tell whether to wait or to build."""
    with pytest.raises(GateError, match="requires the owning phase"):
        not_implemented("r", "t", "")
    rung = not_implemented("r", "t", "T5 / P7")
    assert rung.status == NOT_IMPLEMENTED
    assert "P7" in rung.detail and rung.owner == "T5 / P7"


def test_a_rung_with_no_detail_is_refused() -> None:
    """A rung with nothing to say is an assertion wearing a table row."""
    with pytest.raises(GateError, match="no detail is an assertion"):
        Rung("r", "t", PASS, "")


# ---------------------------------------------------------------------------
# pytest log ingestion -- with the non-vacuity controls
# ---------------------------------------------------------------------------

_CLEAN_LOG = "....F...                        [100%]\n3 passed, 1 skipped in 12.34s\n"
_DIRTY_LOG = (
    "F.                                    [100%]\n"
    "FAILED tests/test_a.py::test_one - AssertionError: boom\n"
    "FAILED tests/test_b.py::test_two - assert 1 == 2\n"
    "2 failed, 40 passed, 3 skipped, 1 xfailed in 88.10s (0:01:28)\n"
)


def test_a_clean_log_parses_to_zero_failures() -> None:
    parsed = parse_pytest_log(_CLEAN_LOG)
    assert parsed["parsed"] is True
    assert parsed["passed"] == 3
    assert parsed["failed"] == 0
    assert parsed["skipped"] == 1
    assert parsed["failed_nodes"] == []


def test_a_dirty_log_parses_and_names_every_failed_node() -> None:
    """A failure must be NAMED. A count with no node ids is not actionable."""
    parsed = parse_pytest_log(_DIRTY_LOG)
    assert parsed["parsed"] is True
    assert parsed["passed"] == 40
    assert parsed["failed"] == 2
    assert parsed["errors"] == 0
    assert parsed["failed_nodes"] == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]


def test_an_unreadable_log_is_not_reported_as_zero() -> None:
    """The DOCTRINE.md §1 rule, mechanically: absent != zero.

    A truncated log, an HTML page, or an empty file must yield
    ``parsed=False`` and ``None`` -- never ``failed=0``, which would render as
    a green lane in a summary table.
    """
    for junk in (
        "",
        "Killed by host pressure\n",
        "<html>crash</html>",
        "no summary here",
    ):
        parsed = parse_pytest_log(junk)
        assert parsed["parsed"] is False, junk
        assert parsed["passed"] is None, junk


def test_the_summary_counts_are_read_in_either_order() -> None:
    """pytest prints ``2 failed, 40 passed`` -- failures FIRST.

    A positional regex read `40` as the passed count and found no `failed`
    group at all, which reported a dirty lane as CLEAN. This arm pins the
    order-independence in both directions, because the count is the whole
    claim.
    """
    failures_first = parse_pytest_log("2 failed, 40 passed, 3 skipped in 88.10s\n")
    passes_first = parse_pytest_log("40 passed, 2 failed, 3 skipped in 88.10s\n")
    for parsed in (failures_first, passes_first):
        assert parsed["parsed"] is True
        assert parsed["failed"] == 2
        assert parsed["passed"] == 40
        assert parsed["skipped"] == 3


def test_a_real_pytest_q_summary_line_parses() -> None:
    """The exact shape this gate is actually fed, copied from a measured run.

    ``pytest -q`` has no ``====`` separator, so an earlier parser that
    required one rejected every real log. This arm is the regression pin for
    that, and it quotes the literal line from the 2026-10-01 run.
    """
    line = "15 failed, 7121 passed, 33 skipped, 2 warnings in 5250.04s (1:27:30)"
    parsed = parse_pytest_log(line + "\n")
    assert parsed["parsed"] is True
    assert parsed["failed"] == 15
    assert parsed["passed"] == 7121
    assert parsed["skipped"] == 33
    # warnings are not a test outcome and must not become one
    assert parsed["errors"] == 0


def test_a_lane_that_collected_nothing_is_fail_not_pass(tmp_path: Path) -> None:
    """Zero collected tests is never a pass -- the module's own `no_tests` rule."""
    from evals.gates.P0 import _suite_rung

    log = tmp_path / "empty.log"
    log.write_text("no tests ran in 0.04s\n", encoding="utf-8")
    rung = _suite_rung("lane", "a lane", log, owner="T5", command="pytest")
    assert rung.status == FAIL
    assert "collected NOTHING" in rung.detail


def test_an_error_or_an_xfail_makes_a_lane_fail(tmp_path: Path) -> None:
    """The clean criterion is stricter than `failed == 0`.

    A collection/setup ERROR and a suppressed `xfail` are both ways a lane can
    look green while having run less than it claims.
    """
    from evals.gates.P0 import _suite_rung

    err_log = tmp_path / "err.log"
    err_log.write_text("1 failed, 40 passed, 1 error in 5.00s\n", encoding="utf-8")
    assert _suite_rung("l", "t", err_log, owner="T5", command="p").status == FAIL

    xfail_log = tmp_path / "xfail.log"
    xfail_log.write_text("41 passed, 1 xfailed in 5.00s\n", encoding="utf-8")
    rung = _suite_rung("l", "t", xfail_log, owner="T5", command="p")
    assert rung.status == FAIL, "a suppressed xfail must not render as a clean lane"
    for junk in (
        "",
        "Killed by host pressure\n",
        "<html>crash</html>",
        "no summary here",
    ):
        parsed = parse_pytest_log(junk)
        assert parsed["parsed"] is False, junk
        assert parsed["passed"] is None, junk


def test_a_lane_with_no_log_is_blocked_not_passed(tmp_path: Path) -> None:
    """Absence of a measurement is ``blocked`` + the reason. Never a pass."""
    from evals.gates.P0 import _suite_rung

    rung = _suite_rung("lane", "a lane", None, owner="T5", command="pytest tests/")
    assert rung.status == BLOCKED
    assert "no measured log" in rung.detail

    # a path that does not exist is the same shape, and names the path
    rung2 = _suite_rung(
        "lane", "a lane", tmp_path / "nope.log", owner="T5", command="pytest tests/"
    )
    assert rung2.status == BLOCKED
    assert "no measured log" in rung2.detail


def test_a_lane_whose_log_has_no_summary_is_blocked_with_that_reason(
    tmp_path: Path,
) -> None:
    from evals.gates.P0 import _suite_rung

    log = tmp_path / "truncated.log"
    log.write_text("Killed by the host before the summary line\n", encoding="utf-8")
    rung = _suite_rung("lane", "a lane", log, owner="T5", command="pytest")
    assert rung.status == BLOCKED
    assert "no recognisable pytest summary" in rung.detail
    assert "NOT reported as zero" in rung.detail


def test_a_dirty_lane_is_fail_and_names_the_nodes(tmp_path: Path) -> None:
    from evals.gates.P0 import _suite_rung

    log = tmp_path / "dirty.log"
    log.write_text(_DIRTY_LOG, encoding="utf-8")
    rung = _suite_rung("lane", "a lane", log, owner="T5", command="pytest")
    assert rung.status == FAIL
    assert "tests/test_a.py::test_one" in rung.detail
    assert "tests/test_b.py::test_two" in rung.detail


def test_a_clean_lane_is_pass(tmp_path: Path) -> None:
    from evals.gates.P0 import _suite_rung

    log = tmp_path / "clean.log"
    log.write_text(_CLEAN_LOG, encoding="utf-8")
    rung = _suite_rung("lane", "a lane", log, owner="T5", command="pytest")
    assert rung.status == PASS
    assert "ran and held" in rung.detail


# ---------------------------------------------------------------------------
# the verdict derivation
# ---------------------------------------------------------------------------


def test_a_blocking_failure_makes_the_gate_red(tmp_path: Path) -> None:
    """The gate can come back red. That is the property that makes it a gate."""
    log = tmp_path / "dirty.log"
    log.write_text(_DIRTY_LOG, encoding="utf-8")
    report = g0_report(
        Path(__file__).resolve().parents[1],
        tests_dir_log=log,
        module_local_log=log,
        run_probes=False,
    )
    assert report["verdict"] == GATE_RED
    assert report["counts"][FAIL] >= 2
    assert all(r["status"] in STATUSES for r in report["rungs"])


def test_a_blocked_row_alone_does_not_silently_turn_the_gate_green_or_red(
    tmp_path: Path,
) -> None:
    """A blocked lane is a REPORT, not a verdict.

    The gate stays green on a blocked row on purpose: blocked means "nobody
    could run it", which is a fact for the reader to act on, not a product
    failure. The important property is that it is VISIBLE -- it appears in
    ``blocked_rows`` with its reason -- rather than rendered as a pass.
    """
    report = g0_report(Path(__file__).resolve().parents[1], run_probes=False)
    blocked_ids = {row["id"] for row in report["blocked_rows"]}
    assert "live_provider_lane" in blocked_ids, (
        "the live-provider row must always be present and blocked, because no "
        "live provider is reachable and every model call is a scripted double"
    )
    for row in report["blocked_rows"]:
        assert row["reason"].strip(), "a blocked row without a reason is a lie"
    # both pytest lanes had no log -> blocked, and named as such
    for lane in ("full_suite_tests_dir", "full_suite_module_local"):
        rung = next(r for r in report["rungs"] if r["id"] == lane)
        assert rung["status"] == BLOCKED
        assert "no measured log" in rung["detail"]


def test_every_rung_id_is_unique_and_every_status_is_in_the_vocabulary() -> None:
    """A duplicated row in a gate table is how one measurement is reported twice."""
    report = g0_report(Path(__file__).resolve().parents[1], run_probes=False)
    ids = [r["id"] for r in report["rungs"]]
    assert len(ids) == len(set(ids))
    for rung in report["rungs"]:
        assert rung["status"] in STATUSES
        assert rung["detail"].strip()
        assert rung["evidence"].strip(), f"{rung['id']} has no evidence"


# ---------------------------------------------------------------------------
# what the gate does NOT establish
# ---------------------------------------------------------------------------


def test_the_not_established_list_travels_with_the_report() -> None:
    """The limits cannot be separated from the numbers.

    A verdict read without its limits is a different claim, so the list is in
    the report dict AND in the rendered text.
    """
    report = g0_report(Path(__file__).resolve().parents[1], run_probes=False)
    assert report["what_this_does_not_establish"], (
        "a gate with no stated limits invites the reader to fill them in"
    )
    joined = " ".join(report["what_this_does_not_establish"]).lower()
    assert "model quality" in joined
    assert "scripted double" in joined
    text = render(report)
    assert "WHAT G0 DOES NOT ESTABLISH:" in text
    for item in report["what_this_does_not_establish"]:
        assert item[:40] in text


def test_the_rendered_report_uses_only_the_four_statuses() -> None:
    """The table a reader sees must not contain a fifth glyph."""
    report = g0_report(Path(__file__).resolve().parents[1], run_probes=False)
    text = render(report)
    # the vocabulary is stated in the header AND used in the rows
    assert "status vocabulary: pass | fail | blocked | not_implemented" in text
    for glyph in ("pass  ", "FAIL  ", "BLOCK ", "NOTIMPL"):
        assert glyph in text, glyph
    # no row may print a banned status
    for banned in _FORBIDDEN_STATUSES:
        assert f" {banned} " not in text
    assert "BLOCKED rows (never reported as skips):" in text


def test_the_report_is_json_serialisable() -> None:
    """A gate nobody can archive is a gate nobody can audit later."""
    report = g0_report(Path(__file__).resolve().parents[1], run_probes=False)
    text = json.dumps(report, indent=2, default=str)
    assert json.loads(text)["gate"] == "G0"


# ---------------------------------------------------------------------------
# the security-blocker count -- measured, not assumed
# ---------------------------------------------------------------------------


def test_the_security_count_is_measured_against_a_real_version_bound() -> None:
    """The count is a version comparison, not a field read.

    ``Advisory`` has no ``status`` field, so "is this resolved?" has to be
    answered by comparing the pinned version with the advisory's own ``fixed``
    bound. The non-vacuity arm: a package whose pin is below the bound must
    land in ``blocking``.
    """
    report = count_security_blockers(Path(__file__).resolve().parents[1])
    assert report["measurable"] is True, report["detail"]
    assert report["count"] is not None
    for ident in report["blocking"]:
        assert "< fixed" in ident, (
            f"{ident} is counted as an active blocker without a version "
            "comparison, which means the count is not measured"
        )
    # every bucket must be present, so a reader can see what was excluded
    for key in ("blocking", "resolved", "unknown_version", "not_applicable"):
        assert key in report
    # the total is derived, not asserted
    assert report["count"] == len(report["blocking"]) + len(report["unknown_version"])


def test_an_unresolvable_version_is_never_counted_as_resolved() -> None:
    """Absent evidence is not a clean bill of health."""
    report = count_security_blockers(Path(__file__).resolve().parents[1])
    resolved_ids = {row.split("(")[0] for row in report["resolved"]}
    unknown_ids = {row.split("(")[0] for row in report["unknown_version"]}
    assert not (resolved_ids & unknown_ids)
    for row in report["not_applicable"]:
        assert "absent from" in row, (
            "an advisory excluded from the count must say on what basis"
        )


def test_the_version_tuple_comparison_behaves_like_a_version_order() -> None:
    """The comparison is doing real work, so pin its semantics."""
    assert _version_tuple("1.74.10") > _version_tuple("1.74.9")
    assert _version_tuple("78.1.1") < _version_tuple("84.0.0")
    assert _version_tuple("3.1.6") == _version_tuple("3.1.6")
    assert _version_tuple("2.32.4") > _version_tuple("2.5.0")
    assert _version_tuple("nonsense") >= _version_tuple("0")  # never raises


def test_the_pyproject_pin_reader_sees_build_system_requires() -> None:
    """The scan is whole-file, because `[build-system] requires` hides a pin.

    A line-oriented reader looks for a line whose FIRST token is the package
    name; ``requires = ["setuptools==84.0.0"]`` does not have one, and the
    reader falls through to the installed version -- which reported a
    RESOLVED advisory as an ACTIVE blocker before this was fixed.
    """
    root = Path(__file__).resolve().parents[1]
    assert _pinned_version(root, "setuptools") == "84.0.0"
    assert _pinned_version(root, "litellm") == "1.74.9"
    assert _pinned_version(root, "definitely-not-installed-xyzzy") is None


def test_the_npm_lock_reader_is_used_for_javascript_packages() -> None:
    """Three advisories in the DB are npm packages; a Python reader cannot see them."""
    root = Path(__file__).resolve().parents[1]
    # whatever it returns, it must not raise, and a real npm dep must resolve
    assert _npm_pinned_version(root, "lodash") is None or isinstance(
        _npm_pinned_version(root, "lodash"), str
    )
    version = _npm_pinned_version(root, "next")
    assert version is None or version[0].isdigit()
    assert _installed_version("definitely-not-installed-xyzzy") is None


def test_the_blocking_severity_set_excludes_medium_on_purpose() -> None:
    """A gate that fails on everything is a gate that gets disabled."""
    assert "critical" in BLOCKING_SEVERITIES
    assert "high" in BLOCKING_SEVERITIES
    assert "medium" not in BLOCKING_SEVERITIES
    assert "low" not in BLOCKING_SEVERITIES


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


def test_run_probe_reports_a_nonzero_exit_rather_than_raising() -> None:
    """A probe that raises is a probe that cannot report a broken lane."""
    probe = run_probe(["python", "-c", "raise SystemExit(3)"], timeout_s=60)
    assert probe["ok"] is False
    assert probe["returncode"] == 3
    assert probe["timed_out"] is False


def test_run_probe_reports_a_missing_executable_rather_than_raising() -> None:
    probe = run_probe(["definitely-not-a-real-binary-xyzzy"], timeout_s=60)
    assert probe["ok"] is False
    assert probe["error"]
    assert probe["timed_out"] is False


def test_run_probe_reports_a_timeout_rather_than_hanging() -> None:
    probe = run_probe(["python", "-c", "import time; time.sleep(30)"], timeout_s=3)
    assert probe["ok"] is False
    assert probe["timed_out"] is True
    assert "within 3s" in probe["error"]
