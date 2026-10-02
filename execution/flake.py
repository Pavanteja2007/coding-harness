"""Flake-aware failure confirmation (Ceiling 08 §4).

A failing test is not yet a defect. Turning an unconfirmed failure into an edit
instruction is how a repair loop spends its budget chasing a test that was
already failing, or that failed because of ordering, and how a *real* failure
gets filed as "flaky" and silently tolerated.

The rule this module enforces:

    A failure becomes an EDIT INSTRUCTION only after it is CONFIRMED by
    rerunning it in a CLEAN environment. An unconfirmed failure is reported,
    never converted into work for the agent.

Confirmation means: a fresh copy of the repository (no build/run artifacts, no
``__pycache__``, no marker files left by the previous run), a scrubbed
environment (no ``PYTEST_*``/``PYTHONHASHSEED``-style leakage, no caller
``PYTHONPATH`` pointing back at a previous tree), and a fresh runner process per
attempt. Reusing the same tree and the same environment is not a rerun, it is a
re-read of the previous result.

Classification, from the observed outcome vector:

- ``confirmed_regression``   failed in the original run AND in every clean rerun
- ``pre_existing_flake``     outcomes differ across clean reruns
- ``transient_failure``      passed in every clean rerun (the original failure did
                             not reproduce)
- ``unconfirmed``            there were not enough reruns to decide (or the
                             rerun itself could not be executed)

Only ``confirmed_regression`` is actionable. ``confirmation_rate`` is the
fraction of assessments that reached a decision at all, which is the honest
denominator: a run that skipped every confirmation has a rate of 0, not 100.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

from execution.result_parsing import (
    OUTCOME_ERROR,
    OUTCOME_FAIL,
    OUTCOME_NO_TESTS,
    OUTCOME_PASS,
    OUTCOME_TIMEOUT,
    TestRunReport,
    parse_test_run,
)

CLASSIFICATION_CONFIRMED = "confirmed_regression"
CLASSIFICATION_PRE_EXISTING_FLAKE = "pre_existing_flake"
CLASSIFICATION_TRANSIENT = "transient_failure"
CLASSIFICATION_UNCONFIRMED = "unconfirmed"

#: Outcomes that count as "the test did not pass" when confirming a failure.
_NOT_PASSING: Tuple[str, ...] = (
    OUTCOME_FAIL,
    OUTCOME_TIMEOUT,
    OUTCOME_ERROR,
    OUTCOME_NO_TESTS,
)

#: Artifacts a clean environment must not carry over from a previous run.
_ARTIFACT_DIRS: Tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    "node_modules/.cache",
    ".coverage",
)
_ARTIFACT_FILES: Tuple[str, ...] = (
    ".coverage",
    ".coverage.host",
    ".flaky_marker",
    ".hang_marker",
    ".hang2_marker",
)

#: Environment variables that leak prior-run state into a rerun.
_SCRUB_PREFIXES: Tuple[str, ...] = ("PYTEST_", "NEO_", "HARNESS_")


@dataclass(frozen=True)
class FlakeAssessment:
    """The verdict for one failing test after clean-environment reruns."""

    classification: str
    original_outcome: str
    rerun_outcomes: Tuple[str, ...] = ()
    clean: bool = True
    actionable: bool = False
    edit_instruction: str = ""
    reason: str = ""
    rerun_error: str = ""

    @property
    def confirmed(self) -> bool:
        """Return whether the failure reproduced in every clean rerun."""
        return self.classification == CLASSIFICATION_CONFIRMED

    @property
    def decided(self) -> bool:
        """Return whether the assessment reached a conclusion."""
        return self.classification != CLASSIFICATION_UNCONFIRMED

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view for trace/evidence records."""
        return {
            "classification": self.classification,
            "original_outcome": self.original_outcome,
            "rerun_outcomes": list(self.rerun_outcomes),
            "clean": bool(self.clean),
            "actionable": bool(self.actionable),
            "edit_instruction": self.edit_instruction,
            "reason": self.reason,
            "rerun_error": self.rerun_error,
            "confirmed": self.confirmed,
            "decided": self.decided,
        }


@dataclass
class FlakeLedger:
    """Accumulates assessments and reports a confirmation rate.

    The rate is ``decided / total`` (assessments that reached a conclusion over
    assessments attempted), plus ``confirmed / decided`` as the strict reading.
    Both are reported so a caller can be honest about which claim it makes.
    """

    assessments: List[FlakeAssessment] = field(default_factory=list)

    def add(self, assessment: FlakeAssessment) -> FlakeAssessment:
        """Record one assessment and return it."""
        self.assessments.append(assessment)
        return assessment

    @property
    def total(self) -> int:
        """Return the number of assessments recorded."""
        return len(self.assessments)

    @property
    def decided(self) -> int:
        """Return the number of assessments that reached a conclusion."""
        return sum(1 for item in self.assessments if item.decided)

    @property
    def confirmed(self) -> int:
        """Return the number of failures confirmed as real regressions."""
        return sum(1 for item in self.assessments if item.confirmed)

    @property
    def confirmation_rate(self) -> Optional[float]:
        """Return decided/total as a fraction, or None when nothing was tried."""
        if not self.assessments:
            return None
        return round(self.decided / float(len(self.assessments)), 6)

    @property
    def confirmed_rate(self) -> Optional[float]:
        """Return confirmed/decided, or None when nothing was decided."""
        if not self.decided:
            return None
        return round(self.confirmed / float(self.decided), 6)

    def by_classification(self) -> Dict[str, int]:
        """Return the count per classification label."""
        counts: Dict[str, int] = {}
        for item in self.assessments:
            counts[item.classification] = counts.get(item.classification, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible summary for trace/evidence records."""
        return {
            "total": self.total,
            "decided": self.decided,
            "confirmed": self.confirmed,
            "confirmation_rate": self.confirmation_rate,
            "confirmed_rate": self.confirmed_rate,
            "by_classification": self.by_classification(),
        }


def assess_failure(
    report: TestRunReport,
    *,
    run: Optional[Callable[[str], Any]] = None,
    repo_path: str = "",
    attempts: int = 2,
    clean: bool = True,
    label: str = "",
) -> FlakeAssessment:
    """Classify one failing run, rerunning it in a clean environment.

    ``run`` is called with a repo path (the clean copy when ``clean`` is True,
    otherwise the original) and must return anything
    :func:`execution.result_parsing.parse_test_run` accepts. It is required
    when ``attempts`` is non-zero: without a way to rerun, an assessment is
    honestly ``unconfirmed`` and therefore NOT actionable, which is the whole
    point of the module.

    With ``attempts=0`` the function performs no rerun and returns an
    ``unconfirmed`` assessment; this is the explicit "do not confirm" arm, not
    an accident.
    """
    name = label or "failing test"
    if attempts <= 0 or run is None:
        return FlakeAssessment(
            classification=CLASSIFICATION_UNCONFIRMED,
            original_outcome=report.outcome,
            clean=bool(clean),
            actionable=False,
            reason="no clean rerun was performed; the failure stays unconfirmed",
        )
    if report.outcome == OUTCOME_PASS:
        return FlakeAssessment(
            classification=CLASSIFICATION_UNCONFIRMED,
            original_outcome=report.outcome,
            clean=bool(clean),
            actionable=False,
            reason="the run passed; there is no failure to confirm",
        )

    outcomes: List[str] = []
    error = ""
    if clean and repo_path:
        try:
            with clean_environment(repo_path) as clean_dir:
                for _ in range(int(attempts)):
                    outcomes.append(_outcome_of(run(clean_dir)))
        except (OSError, shutil.Error) as exc:
            error = f"clean environment could not be prepared: {exc}"
    else:
        for _ in range(int(attempts)):
            try:
                outcomes.append(_outcome_of(run(repo_path)))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break

    if error and not outcomes:
        return FlakeAssessment(
            classification=CLASSIFICATION_UNCONFIRMED,
            original_outcome=report.outcome,
            rerun_outcomes=tuple(outcomes),
            clean=bool(clean),
            actionable=False,
            reason=error,
            rerun_error=error,
        )

    return classify_outcomes(
        original_outcome=report.outcome,
        rerun_outcomes=tuple(outcomes),
        clean=bool(clean),
        label=name,
        note=error,
    )


def classify_outcomes(
    *,
    original_outcome: str,
    rerun_outcomes: Sequence[str],
    clean: bool = True,
    label: str = "",
    note: str = "",
) -> FlakeAssessment:
    """Classify an observed outcome vector without running anything.

    Exposed separately so a caller that already has clean rerun reports (for
    example from a replayed log) gets exactly the same classification the live
    path would produce.
    """
    name = label or "failing test"
    outcomes = tuple(str(value) for value in rerun_outcomes)
    if not outcomes:
        return FlakeAssessment(
            classification=CLASSIFICATION_UNCONFIRMED,
            original_outcome=original_outcome,
            clean=bool(clean),
            actionable=False,
            reason=note or "no clean rerun produced an outcome",
            rerun_error=note,
        )
    not_passing = [value for value in outcomes if value in _NOT_PASSING]
    if len(set(outcomes)) > 1:
        return FlakeAssessment(
            classification=CLASSIFICATION_PRE_EXISTING_FLAKE,
            original_outcome=original_outcome,
            rerun_outcomes=outcomes,
            clean=bool(clean),
            actionable=False,
            reason=(
                f"{name} produced different outcomes across clean reruns "
                f"({', '.join(outcomes)}); it is flaky, not a regression"
            ),
            rerun_error=note,
        )
    if not not_passing:
        return FlakeAssessment(
            classification=CLASSIFICATION_TRANSIENT,
            original_outcome=original_outcome,
            rerun_outcomes=outcomes,
            clean=bool(clean),
            actionable=False,
            reason=(
                f"{name} passed in every clean rerun; the original failure did "
                "not reproduce and is not an edit instruction"
            ),
            rerun_error=note,
        )
    return FlakeAssessment(
        classification=CLASSIFICATION_CONFIRMED,
        original_outcome=original_outcome,
        rerun_outcomes=outcomes,
        clean=bool(clean),
        actionable=True,
        edit_instruction=f"fix the confirmed failure in {name}",
        reason=(
            f"{name} failed in every clean rerun ({', '.join(outcomes)}); "
            "treated as a real regression"
        ),
        rerun_error=note,
    )


@contextmanager
def clean_environment(
    repo_path: str, *, keep_artifacts: Sequence[str] = ()
) -> Iterator[str]:
    """Yield a pristine copy of ``repo_path`` suitable for a rerun.

    The copy is made with symlinks dereferenced into real files and the usual
    run-artifact directories removed, so an order-dependent marker, a stale
    ``__pycache__``, or a coverage database from the failing run cannot make the
    rerun agree with the failure for the wrong reason. The original tree is
    never modified.
    """
    keep = {str(value) for value in keep_artifacts}
    source = str(repo_path)
    staging = tempfile.mkdtemp(prefix="neo-clean-")
    target = os.path.join(staging, "repo")
    try:
        shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)
        _purge_artifacts(target, keep)
        yield target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def scrub_run_environment(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return a run environment with prior-run leakage removed.

    Drops harness/pytest control variables and any ``PYTHONPATH`` that points
    at a previous tree, keeps the rest of the process environment (PATH and
    friends are load-bearing), and never echoes values back to the caller.
    """
    base = dict(os.environ if env is None else env)
    cleaned: Dict[str, str] = {}
    for key, value in base.items():
        upper = key.upper()
        if any(upper.startswith(prefix) for prefix in _SCRUB_PREFIXES):
            continue
        if upper == "PYTHONPATH":
            continue
        cleaned[key] = value
    return cleaned


def run_local_command(
    command: Sequence[str], *, cwd: str, timeout_s: int = 120
) -> TestRunReport:
    """Run a command on the HOST and parse it as a test run.

    This exists for the local, non-Docker measurement lanes (selection timing,
    flake confirmation on a fixture repo). It is deliberately NOT wired into
    :func:`execution.verify.verify`: production verification keeps going through
    the Docker sandbox, and nothing here can be mistaken for a sandbox pass
    because the report it produces records ``source`` from real output but the
    caller can see it came from a host process.

    THE SUBPROCESS-OUTPUT INGRESS for this lane. Two things were wrong here
    and both are real, not theoretical:

    1. **It was completely uncapped.** ``capture_output=True`` with no byte
       bound, while every sandboxed and local-workspace path caps at 1 MB. A
       test that printed its own input in a loop could exhaust host memory
       through a "measurement" helper.
    2. **It was unredacted**, like everything else before this round.

    The cap is the ingress's VERIFICATION cap, not the default: this function
    runs a test suite, and its output is exactly the pytest transcript the
    diagnosis is read from. The raw text never leaves this function (only a
    :class:`TestRunReport` does), so this is defence in depth — but a
    `notes` tuple on that report is a caller-visible string, and a test whose
    failure message quotes a fixture's token would have carried it.
    """
    from execution.ingress import PURPOSE_VERIFICATION, seal_streams
    from shared.types import ExecutionResult

    env = scrub_run_environment()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    timed_out = False
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(timeout_s),
            shell=False,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
    except OSError as exc:
        return TestRunReport(
            outcome=OUTCOME_ERROR,
            source="exit_code",
            confidence="low",
            notes=(f"host command could not run: {type(exc).__name__}: {exc}",),
        )
    result, _reports = seal_streams(
        ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        ),
        purpose=PURPOSE_VERIFICATION,
    )
    return parse_test_run(result)


def _as_text(value: Any) -> str:
    """Coerce subprocess output that may be bytes, str, or None."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _outcome_of(raw: Any) -> str:
    """Coerce whatever a rerun returned into an outcome string."""
    if isinstance(raw, TestRunReport):
        return raw.outcome
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return OUTCOME_PASS if raw else OUTCOME_FAIL
    outcome = getattr(raw, "outcome", None)
    if isinstance(outcome, str):
        return outcome
    return parse_test_run(raw).outcome


def _purge_artifacts(root: str, keep: Iterable[str]) -> None:
    """Remove run artifacts from a clean copy of the repository."""
    keep_set = set(keep)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        for name in list(dirnames):
            if name in _ARTIFACT_DIRS and name not in keep_set:
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
        for name in filenames:
            if name in _ARTIFACT_FILES and name not in keep_set:
                try:
                    os.unlink(os.path.join(dirpath, name))
                except OSError:
                    pass
