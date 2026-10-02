"""Baseline failure set and environment triage (VEX-CEILING Round 2, R2-05).

**The verifier answers "did the target pass". It never answered "was this test
already broken before I touched anything", and it treated every failure as a
code failure.** Both are the same defect seen from two sides:

1. With no baseline failure SET, a run cannot tell a *regression* (this fix
   broke something) from a *pre-existing* failure (it was broken before). It
   reports `regression_passed=False` for both, so a repository that was already
   red reads exactly like one the agent damaged. The honest triple is
   `target_passed`, `new_failures`, `preexisting_failures` — and only the
   second of those is something this run is responsible for.

2. An environment fault (missing interpreter, unresolvable import of a
   DECLARED dependency, unreachable network, absent Docker daemon, permission
   error on the repository itself) is presented to the model as a defect to
   fix. The loop then "repairs" the machine by editing the repository, which
   both fails and destroys the evidence of what was there before.

This module owns the data and the two decisions; it owns NO verdict of its own.

- `BaselineSet` is the recorded, named set of tests that already failed on the
  PRISTINE tree, with the counts that say whether that observation completed.
- `BaselineVerdict` is the honest triple plus the flags that make it
  non-vacuous, and `blocks_success` is fail-CLOSED: it is True whenever the
  verifier's own evidence says the run is not green, so this module can only
  ever ADD a reason to refuse success, never remove one.
- `EnvironmentVerdict` names the environment class and the end state. The
  classification itself is delegated to `harness.tool_errors.classify_environment`
  (the existing classifier, EXTENDED for this prompt) rather than forked here,
  so there is exactly one table of what counts as an environment fault.

Design rules that are load-bearing, not decoration:

- **An unobserved baseline is not an empty baseline.** A baseline run that
  timed out or crashed yields `baseline_observed=False` and an empty failure
  set, and the verdict then reports every post-fix failure as NEW. Guessing
  "pre-existing" for evidence that was never collected would excuse real
  regressions.
- **An undeclared import that fails is a code defect.** Only a module the
  repository DECLARES can be attributed to the environment; otherwise the
  existing `import_error` classification stands and the run keeps repairing.
- **A harness policy refusal is never an environment fault.** The delegation
  handles that exclusion; this module only records the outcome.
- **"0 tests collected" is never a pass.** `zero_tests_collected` is set from
  the parsed evidence (exit-code shape, zero-count marker, or an explicit
  report outcome) and it blocks success, because a renamed or moved collector
  is exactly what a vacuous green looks like.
- **Never raises.** An unreadable manifest, an unparseable capture, a missing
  file, and an unavailable classifier all degrade to a recorded reason. A run
  must not die because its diagnosis was hard.

Configuration (project rule 5: every knob through `Task.config`; **key
presence** for opt-in, because a value placed in `DEFAULTS` is merged into
every task and every eval arm at once):

  `baseline_set_enabled`          present -> the failure set is recorded
  `environment_triage_enabled`    present -> environment triage runs
  `baseline_set_max_failures`     cap on how many failures are named (default 200)
  `baseline_set_max_summary_chars` cap on one failure's summary (default 300)

Both enable keys default to ABSENT (off) precisely so nothing switches
silently; `harness/config.py` is not this module's file, so adding them to
`DEFAULTS` is a filed request, not an edit. See `execution/AGENTS.md`
("Cross-terminal requests").
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from execution.feedback import FeedbackObject, to_objects
from execution.result_parsing import (
    OUTCOME_ERROR,
    OUTCOME_NO_TESTS,
    OUTCOME_TIMEOUT,
    TestRunReport,
    parse_prose_counts,
)

__all__ = [
    "BASELINE_FILE",
    "ENV_CLASSIFICATION_NONE",
    "SCHEMA_VERSION",
    "BaselineSet",
    "BaselineVerdict",
    "EnvironmentVerdict",
    "FailureRecord",
    "baseline_set_from_verification",
    "classify_run",
    "declared_dependencies",
    "default_baseline_path",
    "load_baseline",
    "record_baseline",
    "save_baseline",
    "should_stop_for_environment",
    "triage_environment",
]

#: Bumped when the persisted record changes shape. An unknown version is
#: refused by the reader rather than half-understood.
SCHEMA_VERSION = 1

#: The on-disk name beside a run's other artifacts.
BASELINE_FILE = "baseline_set.json"

#: What `EnvironmentVerdict.classification` says when triage found nothing.
ENV_CLASSIFICATION_NONE = "none"

#: The run status an environment fault must end in. `TaskResult.status` is a
#: closed Literal over success/failed/error/timeout, so an environment fault is
#: honestly an `error` — distinguished by the classification, the operator
#: action, and `repairable_by_edit=False` in the payload, never by a new
#: status word and never by anything resembling success.
ENV_RUN_STATUS = "error"

#: Reported when the classifier itself cannot be imported. NOT "not an
#: environment fault" and NOT "an environment fault": the honest answer is that
#: the question could not be asked, and it is recorded as such.
ENV_CLASSIFICATION_UNAVAILABLE = "env_classifier_unavailable"

_MAX_FAILURES_DEFAULT = 200
_MAX_SUMMARY_CHARS_DEFAULT = 300


# ---------------------------------------------------------------------------
# configuration (key presence for opt-in; value keys for caps)
# ---------------------------------------------------------------------------


def _config_enabled(cfg: Optional[Mapping[str, Any]], key: str) -> bool:
    """True when an OPT-IN key is PRESENT in the resolved config.

    Presence, not truthiness of a default: a value in `DEFAULTS` is merged into
    every task and every eval arm, so an opt-in feature gated on a value
    switches every run the moment it is added. Presence is the only safe gate
    for behaviour that must be requested.
    """
    try:
        return key in (cfg or {})
    except Exception:  # pragma: no cover — defensive
        return False


def _config_int(cfg: Optional[Mapping[str, Any]], key: str, default: int) -> int:
    """Read a bounded integer cap from the config, tolerating garbage."""
    try:
        value = (cfg or {}).get(key, default)
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


# ---------------------------------------------------------------------------
# declared dependencies (the evidence an import error needs)
# ---------------------------------------------------------------------------

_PYPROJECT_DEP_RE = re.compile(
    r"^\s*dependencies\s*=\s*\[(?P<body>[^\]]*)\]", re.MULTILINE | re.DOTALL
)
_PEP508_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_SETUP_CFG_RE = re.compile(
    r"^\s*install_requires\s*=\s*(?P<body>.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL
)


def _names_from_text(body: str) -> List[str]:
    """Pull distribution names out of a manifest fragment.

    Deliberately tolerant: the goal is to know whether a module name is one the
    project DECLARED, and a name read imperfectly is still evidence. It is
    never used to invent a version or a requirement.
    """
    names: List[str] = []
    for chunk in re.split(r"[,\n]", body or ""):
        text = chunk.strip().strip("\"'").lstrip("-")
        if not text or text.startswith("#"):
            continue
        match = _PEP508_NAME_RE.match(text)
        if match:
            names.append(match.group(0))
    return names


def _read_requirements(root: str) -> List[str]:
    """Names from every ``requirements*.txt`` at the repository root."""
    out: List[str] = []
    try:
        for filename in sorted(os.listdir(root)):
            if not filename.lower().startswith("requirements"):
                continue
            path = os.path.join(root, filename)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                out.extend(_names_from_text(handle.read()))
    except OSError:
        return []
    return out


def _read_pyproject(root: str) -> Tuple[List[str], Optional[str]]:
    """Names from ``pyproject.toml`` plus a note when parsing was degraded.

    Uses ``tomllib`` (3.11+) then ``tomli``; when neither is importable a
    bounded regex reads the ``dependencies = [ ... ]`` array. The third case is
    reported as a note rather than being silently treated as authoritative,
    because a degraded parse is weaker evidence.
    """
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return [], None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return [], "pyproject.toml could not be read"
    loader = None
    try:
        import tomllib as loader  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover — Python 3.10 hosts
        try:
            import tomli as loader  # type: ignore[no-redef]
        except ImportError:
            loader = None
    if loader is not None:
        try:
            document = loader.loads(text)
            project = document.get("project") if isinstance(document, Mapping) else None
            deps = project.get("dependencies") if isinstance(project, Mapping) else None
            if isinstance(deps, list):
                return _names_from_text("\n".join(str(item) for item in deps)), None
            return [], None
        except Exception:
            return [], "pyproject.toml was unparseable"
    match = _PYPROJECT_DEP_RE.search(text)
    if not match:
        return [], None
    return (
        _names_from_text(match.group("body")),
        "no TOML parser available; dependencies read by bounded text match",
    )


def _read_package_json(root: str) -> List[str]:
    """Names from a JS/TS manifest's dependencies and devDependencies."""
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            document = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(document, Mapping):
        return []
    out: List[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = document.get(key)
        if isinstance(block, Mapping):
            out.extend(str(name) for name in block)
    return out


def _read_setup_cfg(root: str) -> List[str]:
    """Names from ``setup.cfg``'s ``install_requires``."""
    path = os.path.join(root, "setup.cfg")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    match = _SETUP_CFG_RE.search(text)
    if not match:
        return []
    return _names_from_text(
        "\n".join(line.split("#", 1)[0] for line in match.group("body").splitlines())
    )


def declared_dependencies(
    repo_path: Optional[str],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return ``(names, notes)`` for every dependency the repository DECLARES.

    This is the evidence an import error needs before it may be called an
    environment fault: without it, a missing module is indistinguishable from a
    genuine code defect and the run must keep treating it as one.

    Assumes ``repo_path`` is a directory; a missing/unreadable directory yields
    an empty tuple and a note, never an exception. Names are raw manifest
    strings, deduplicated case-insensitively, in first-seen order. ``notes``
    records every degraded read (unparseable manifest, missing TOML parser) so
    a caller can tell "the project declares nothing" from "we could not read
    what the project declares".
    """
    names: List[str] = []
    notes: List[str] = []
    root = str(repo_path or "")
    if not root:
        # No repository supplied is the caller's own choice, not a failed scan,
        # so it produces no note: a caller that never had a repo should not
        # read "unreadable directory" in its verdict.
        return (), ()
    if not os.path.isdir(root):
        return (), (f"repository is not a readable directory: {root}",)
    try:
        names.extend(_read_requirements(root))
        py_names, py_note = _read_pyproject(root)
        names.extend(py_names)
        if py_note:
            notes.append(py_note)
        names.extend(_read_package_json(root))
        names.extend(_read_setup_cfg(root))
    except Exception as exc:  # a diagnostic must not raise into a run
        notes.append(f"dependency scan degraded: {type(exc).__name__}: {exc}")
    seen: Dict[str, str] = {}
    for name in names:
        key = str(name).strip().lower()
        if key and key not in seen:
            seen[key] = str(name).strip()
    return tuple(seen.values()), tuple(notes)


# ---------------------------------------------------------------------------
# the failure record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureRecord:
    """One named failing test, as observed on ONE side of the run.

    Assumes it was built from `execution.feedback` objects (or by hand in a
    test), so ``test_id`` is a pytest node id when the capture carried one and
    None otherwise; ``file``/``line`` are the repo-relative location when the
    capture carried them. The record is evidence, not a verdict: it says a test
    failed, never why the run should or should not succeed.
    """

    test_id: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    failure_type: str = "unparseable"
    summary: str = ""

    @property
    def key(self) -> str:
        """A stable identity for cross-run comparison.

        Node id first (the only identity that survives a moved line), then
        file+line, then a normalized summary. Returns a non-empty string for
        every record, so a set of these can never contain a silently
        unidentifiable member.
        """
        if self.test_id:
            return str(self.test_id).strip()
        if self.file and self.line is not None:
            return f"{self.file}:{self.line}"
        if self.file:
            return str(self.file)
        return " ".join(str(self.summary or "").split())[:120] or "unknown_failure"

    def matches(self, other: "FailureRecord") -> bool:
        """True when two records describe the SAME failure.

        Prefers an exact node-id comparison and falls back to file+line, so a
        capture that upgraded a bare test name to a full node id on one side
        only still matches. Never returns True for two records with no shared
        evidence.
        """
        try:
            if self.key and self.key == other.key:
                return True
            if self.test_id and other.test_id:
                return str(self.test_id).strip() == str(other.test_id).strip()
            if self.file and other.file and self.line is not None:
                return str(self.file) == str(other.file) and int(self.line) == int(
                    other.line or -1
                )
            return False
        except Exception:  # pragma: no cover — defensive
            return False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready projection for the run's artifacts and the trace."""
        return {
            "test_id": self.test_id,
            "file": self.file,
            "line": self.line,
            "failure_type": self.failure_type,
            "summary": self.summary,
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FailureRecord":
        """Rebuild a record from its dict projection, tolerating junk."""
        try:
            line = data.get("line")
            return cls(
                test_id=data.get("test_id"),
                file=data.get("file"),
                line=int(line) if isinstance(line, (int, float)) else None,
                failure_type=str(data.get("failure_type") or "unparseable"),
                summary=str(data.get("summary") or ""),
            )
        except Exception:
            return cls()

    @classmethod
    def from_feedback(
        cls,
        obj: FeedbackObject,
        *,
        max_summary_chars: int = _MAX_SUMMARY_CHARS_DEFAULT,
    ) -> "FailureRecord":
        """Adapt one `execution.feedback.FeedbackObject`."""
        summary = " ".join(str(obj.summary or "").split())[
            : max(1, int(max_summary_chars))
        ]
        return cls(
            test_id=obj.test_id,
            file=obj.file,
            line=obj.line,
            failure_type=str(obj.failure_type or "unparseable"),
            summary=summary,
        )


# ---------------------------------------------------------------------------
# environment verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentVerdict:
    """Whether the failure is a machine fault, and what the run must do.

    ``environment`` is the boolean the loop asks; ``classification`` is the
    stable slug a report keys off (one of
    `harness.tool_errors.ENVIRONMENT_KINDS`, or ``"none"``); ``action`` is the
    POLICY slug that was selected. ``repairable_by_edit`` is False for EVERY
    environment class — that is the property the run's end state is built on,
    and it is asserted rather than assumed.
    """

    environment: bool = False
    classification: str = ENV_CLASSIFICATION_NONE
    action: str = ""
    detail: str = ""
    hint: str = ""
    operator_action: str = ""
    repairable_by_edit: bool = True
    run_status: str = ""
    declared_dependencies: Tuple[str, ...] = ()
    matched_dependencies: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready projection. Contains no completion status word."""
        return {
            "environment": bool(self.environment),
            "classification": self.classification,
            "action": self.action,
            "detail": self.detail,
            "hint": self.hint,
            "operator_action": self.operator_action,
            "repairable_by_edit": bool(self.repairable_by_edit),
            "run_status": self.run_status,
            "declared_dependencies": list(self.declared_dependencies),
            "matched_dependencies": list(self.matched_dependencies),
            "notes": list(self.notes),
        }

    def report(self) -> str:
        """Operator-facing explanation of why the run stopped."""
        if not self.environment:
            return "not an environment failure"
        lines = [
            f"ENVIRONMENT FAILURE [{self.classification}]",
            f"cause: {self.detail}",
            "repairable by editing the repository: NO",
        ]
        if self.operator_action or self.hint:
            lines.append(f"operator action: {self.operator_action or self.hint}")
        if self.matched_dependencies:
            lines.append(
                "declared dependency implicated: "
                + ", ".join(self.matched_dependencies)
            )
        for note in self.notes:
            lines.append(f"note: {note}")
        lines.append(
            "The run stopped instead of editing the repository: an environment "
            "fault has no repository-side fix."
        )
        return "\n".join(lines)


def _no_environment(notes: Sequence[str] = ()) -> EnvironmentVerdict:
    """The explicit "not an environment fault" verdict."""
    return EnvironmentVerdict(notes=tuple(notes))


def _declared_dependencies_impl(
    repo_path: Optional[str],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Module-level alias so `triage_environment`'s parameter may shadow the
    public name without the function calling itself."""
    return declared_dependencies(repo_path)


def triage_environment(
    result: Any = None,
    *,
    exc: Optional[BaseException] = None,
    output: str = "",
    repo_path: Optional[str] = None,
    declared_dependencies: Any = None,
    command: str = "",
) -> EnvironmentVerdict:
    """Classify a failure as an environment fault, and say what ends the run.

    Delegates the classification to `harness.tool_errors.classify_environment`
    (or `environment_from_result` for an `ExecutionResult`), which is the ONE
    table in the tree for this question — this prompt EXTENDED that classifier
    instead of forking a second one.

    Assumes ``result`` is an `ExecutionResult`-shaped object (or None), ``exc``
    an exception raised around the run (or None), and ``declared_dependencies``
    either an explicit iterable or None, in which case the repository's own
    manifests are read via :func:`declared_dependencies`. Never raises.

    Every environment class produces ``repairable_by_edit=False`` and
    ``run_status="error"``. An unavailable classifier produces
    ``classification="env_classifier_unavailable"`` with ``environment=False``
    and a note — the question could not be asked, which is neither a pass nor a
    verdict, and is recorded as exactly that.
    """
    notes: List[str] = []
    try:
        try:
            from harness import tool_errors
        except Exception as exc_import:  # pragma: no cover — defensive
            return EnvironmentVerdict(
                classification=ENV_CLASSIFICATION_UNAVAILABLE,
                notes=(
                    f"environment classifier unavailable: {exc_import}",
                    "no environment classification was attempted",
                ),
            )

        names: Tuple[str, ...]
        if declared_dependencies is None:
            names, scan_notes = _declared_dependencies_impl(repo_path)
            notes.extend(scan_notes)
        else:
            names = tuple(str(item) for item in declared_dependencies or ())

        if result is not None and getattr(result, "exit_code", None) not in (0, None):
            err = tool_errors.environment_from_result(
                result,
                command=command,
                declared_dependencies=names,
                repo_path=repo_path,
            )
        else:
            err = tool_errors.classify_environment(
                exc,
                output=output
                or (
                    f"{getattr(result, 'stdout', '') or ''}\n"
                    f"{getattr(result, 'stderr', '') or ''}"
                    if result is not None
                    else ""
                ),
                command=command,
                declared_dependencies=names,
                repo_path=repo_path,
            )
        if err is None:
            return _no_environment(notes)

        classification = str(err.kind)
        normalized = tool_errors._normalize_dependencies(names)
        # Which DECLARED dependency the classification actually implicates: the
        # quoted module names in its own detail, intersected with the manifest.
        # Intersecting (rather than listing everything) is what makes the
        # receipt evidence instead of an inventory.
        detail_text = str(err.detail or "")
        matched = tuple(
            sorted(
                {
                    token
                    for quoted in re.findall(r"'([^']+)'", detail_text)
                    for part in re.split(r"[-_.\s]+", quoted)
                    for token in (part,)
                    if part and part.lower() in normalized
                }
            )
        )
        return EnvironmentVerdict(
            environment=True,
            classification=classification,
            action=tool_errors.environment_action(classification) or "",
            detail=str(err.detail or ""),
            hint=str(err.hint or ""),
            operator_action=str(err.hint or ""),
            repairable_by_edit=bool(tool_errors.is_repairable_by_edit(classification)),
            run_status=ENV_RUN_STATUS,
            declared_dependencies=tuple(sorted(names)),
            matched_dependencies=matched,
            notes=tuple(notes),
        )
    except Exception as exc:  # a diagnosis must never kill a run
        return EnvironmentVerdict(
            classification=ENV_CLASSIFICATION_UNAVAILABLE,
            notes=(f"environment triage degraded: {type(exc).__name__}: {exc}",),
        )


# ---------------------------------------------------------------------------
# the baseline failure set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineSet:
    """The tests that already failed on the PRISTINE tree, named and counted.

    ``failures`` is the set; ``baseline_observed`` says whether the run that
    produced it COMPLETED. Those are different claims and the distinction is the
    whole point of this record: a baseline that timed out leaves an empty set
    that means "unknown", never "clean".
    """

    target_test: Optional[str] = None
    failures: Tuple[FailureRecord, ...] = ()
    collected: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    skipped: Optional[int] = None
    outcome: str = ""
    source: str = "exit_code"
    baseline_observed: bool = False
    environment: EnvironmentVerdict = field(default_factory=_no_environment)
    notes: Tuple[str, ...] = ()

    @property
    def failing_ids(self) -> Tuple[str, ...]:
        """Stable keys of the recorded failures, in record order."""
        return tuple(record.key for record in self.failures)

    @property
    def preexisting_count(self) -> int:
        """How many distinct pre-existing failures were recorded."""
        seen: set = set()
        for record in self.failures:
            seen.add(record.key)
        return len(seen)

    def contains(self, record: "FailureRecord") -> bool:
        """True when `record` is one of the recorded pre-existing failures."""
        try:
            return any(record.matches(known) for known in self.failures)
        except Exception:  # pragma: no cover — defensive
            return False

    def includes_target(self) -> bool:
        """True when the run's own target test is in the pre-existing set.

        The answer to the question the verifier never asked: "was this test
        already broken before I touched anything?"
        """
        target = str(self.target_test or "").strip()
        if not target:
            return False
        for record in self.failures:
            if record.test_id and str(record.test_id).strip() == target:
                return True
            if target in record.key:
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready projection for `logs/{task_id}/baseline_set.json`."""
        return {
            "schema_version": SCHEMA_VERSION,
            "target_test": self.target_test,
            "failures": [record.to_dict() for record in self.failures],
            "collected": self.collected,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "outcome": self.outcome,
            "source": self.source,
            "baseline_observed": bool(self.baseline_observed),
            "environment": self.environment.to_dict(),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BaselineSet":
        """Rebuild from :meth:`to_dict`; an unknown schema version is refused.

        Refusing rather than half-reading is deliberate: a misread baseline
        would attribute pre-existing status to the wrong tests, which is
        precisely the confusion this record exists to remove.
        """
        if not isinstance(data, Mapping):
            raise ValueError("baseline set must be a mapping")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported baseline_set schema_version {version!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        raw = data.get("failures")
        failures = tuple(
            FailureRecord.from_dict(item)
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping)
        )
        env_raw = data.get("environment")
        environment = _no_environment()
        if isinstance(env_raw, Mapping):
            environment = EnvironmentVerdict(
                environment=bool(env_raw.get("environment")),
                classification=str(
                    env_raw.get("classification") or ENV_CLASSIFICATION_NONE
                ),
                action=str(env_raw.get("action") or ""),
                detail=str(env_raw.get("detail") or ""),
                hint=str(env_raw.get("hint") or ""),
                operator_action=str(env_raw.get("operator_action") or ""),
                repairable_by_edit=bool(env_raw.get("repairable_by_edit", True)),
                run_status=str(env_raw.get("run_status") or ""),
                declared_dependencies=tuple(
                    str(item) for item in (env_raw.get("declared_dependencies") or [])
                ),
                matched_dependencies=tuple(
                    str(item) for item in (env_raw.get("matched_dependencies") or [])
                ),
                notes=tuple(str(item) for item in (env_raw.get("notes") or [])),
            )
        notes = tuple(str(item) for item in (data.get("notes") or []))

        def _opt_int(key: str) -> Optional[int]:
            value = data.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        return cls(
            target_test=data.get("target_test"),
            failures=failures,
            collected=_opt_int("collected"),
            passed=_opt_int("passed"),
            failed=_opt_int("failed"),
            skipped=_opt_int("skipped"),
            outcome=str(data.get("outcome") or ""),
            source=str(data.get("source") or "exit_code"),
            baseline_observed=bool(data.get("baseline_observed")),
            environment=environment,
            notes=notes,
        )


def _zero_tests_observed(result: Any, report: Optional[TestRunReport]) -> bool:
    """True when the evidence says NOTHING was collected.

    A report wins (it is machine-readable). Without one, the prose parser's
    digit-bounded zero marker and no-tests markers are used, and an explicit
    failure with named failing tests is trusted over the marker (a report that
    names failures did collect them).
    """
    try:
        if report is not None:
            if report.outcome == OUTCOME_NO_TESTS:
                return True
            # An `error` outcome with no counts is a BROKEN run, not a
            # zero-test run: `execution.result_parsing` keeps those distinct on
            # purpose, and collapsing them here would invent a "0 tests
            # collected" claim the evidence does not make. Only a report that
            # positively says "zero collected" counts here.
            return report.tests_collected == 0
        raw = str(getattr(result, "raw_output", "") or "")
        if not raw.strip():
            return False
        counts = parse_prose_counts(raw)
        if counts.get("failed"):
            return False
        return bool(counts.get("zero_marker")) or (
            counts.get("collected") == 0 and not (counts.get("passed") or 0)
        )
    except Exception:  # pragma: no cover — defensive
        return False


def _outcome_for(
    result: Any,
    report: Optional[TestRunReport],
    *,
    zero_tests: bool,
) -> str:
    """The run's outcome string, reusing `execution.result_parsing`' vocabulary.

    A report wins. Without one the verdict is derived from the result's own
    three-way evidence plus the zero-test check, and an unknown/broken shape
    reports `error` rather than a pass.
    """
    if report is not None:
        return report.outcome
    raw = str(getattr(result, "raw_output", "") or "")
    if zero_tests:
        return OUTCOME_NO_TESTS
    if _looks_timed_out(result, raw):
        return OUTCOME_TIMEOUT
    if not raw.strip() and not any(
        bool(getattr(result, name, False))
        for name in ("target_test_passed", "regression_passed", "flaky")
    ):
        return OUTCOME_ERROR
    passed = bool(getattr(result, "target_test_passed", False)) and bool(
        getattr(result, "regression_passed", False)
    )
    if passed and not bool(getattr(result, "flaky", False)):
        return "pass"
    return "fail"


def _looks_timed_out(result: Any, raw: str) -> bool:
    """True when the capture was killed by the run's time budget."""
    try:
        for line in str(raw or "").splitlines():
            text = line.strip()
            if text.startswith("exit=") and (
                text.endswith("TIMEOUT") or text.split()[0] == "exit=124"
            ):
                return True
        return False
    except Exception:  # pragma: no cover — defensive
        return False


def baseline_set_from_verification(
    result: Any,
    *,
    target_test: Optional[str] = None,
    report: Optional[TestRunReport] = None,
    declared: Any = None,
    repo_path: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    max_failures: Optional[int] = None,
    max_summary_chars: Optional[int] = None,
) -> BaselineSet:
    """Build the baseline failure set from ONE pristine-tree verification.

    Assumes ``result`` is a `shared.types.VerificationResult` (duck typed: it
    needs ``raw_output`` and the three booleans). ``report`` is an optional
    `execution.result_parsing.TestRunReport` for the same run; when supplied its
    counts and outcome are authoritative. ``declared`` is the repository's
    declared dependencies (see :func:`declared_dependencies`); when None they
    are read from ``repo_path``.

    ``baseline_observed`` is False when the run did not produce a usable
    verdict — it timed out, the runner broke, or nothing was collected. In that
    case the failure set is empty AND the empty set means "unknown", which
    :func:`classify_run` then reports as unattributed rather than clean.

    Never raises. Never returns a pass for a zero-test run.
    """
    notes: List[str] = []
    try:
        if result is None:
            return BaselineSet(
                target_test=target_test,
                baseline_observed=False,
                outcome=OUTCOME_ERROR,
                notes=("no verification result was supplied",),
            )
        cap = int(
            max_failures
            if max_failures is not None
            else _config_int(config, "baseline_set_max_failures", _MAX_FAILURES_DEFAULT)
        )
        summary_cap = int(
            max_summary_chars
            if max_summary_chars is not None
            else _config_int(
                config, "baseline_set_max_summary_chars", _MAX_SUMMARY_CHARS_DEFAULT
            )
        )
        raw = str(getattr(result, "raw_output", "") or "")
        zero_tests = _zero_tests_observed(result, report)
        outcome = _outcome_for(result, report, zero_tests=zero_tests)

        records: List[FailureRecord] = []
        seen: set = set()
        structured = list(getattr(result, "structured_feedback", None) or [])
        objects: Sequence[FeedbackObject]
        if outcome == "pass":
            # A GREEN run has no failing tests. `to_objects` deliberately returns
            # a catch-all object for a run whose failures it cannot parse, so
            # parsing a passing capture would manufacture a failure out of its
            # own success line. Nothing is extracted, and the empty set is
            # correct rather than unknown (the run completed).
            objects = []
        elif structured:
            # Boundary-7 objects are the higher-fidelity evidence (the
            # verifier already parsed them); reuse rather than re-parse.
            objects = []
            for item in structured:
                if isinstance(item, Mapping):
                    objects.append(
                        FeedbackObject(
                            test_id=item.get("test_id"),
                            failure_type=str(item.get("failure_type") or "unparseable"),
                            summary=str(item.get("summary") or ""),
                            expected=item.get("expected"),
                            actual=item.get("actual"),
                            file=item.get("file"),
                            line=item.get("line"),
                            traceback_summary=str(item.get("traceback_summary") or ""),
                        )
                    )
        else:
            objects = to_objects(
                raw, target_test=target_test, timed_out=outcome == OUTCOME_TIMEOUT
            )
        for obj in objects:
            if getattr(obj, "failure_type", "") == "timeout":
                # A killed run names no failures. Recording it as one would put
                # "the run was killed" into the pre-existing set, where it would
                # later excuse a real regression.
                continue
            if zero_tests and getattr(obj, "failure_type", "") == "collection_error":
                # Nothing was collected, so there is no per-test failure set to
                # record. A collection error is a fact about the RUN, and it is
                # already carried by `outcome=no_tests` plus a note; naming it
                # as a failing test would inflate the set with a test that
                # never ran.
                continue
            record = FailureRecord.from_feedback(obj, max_summary_chars=summary_cap)
            if record.key in seen:
                continue
            seen.add(record.key)
            records.append(record)
            if len(records) >= max(1, cap):
                notes.append(
                    f"failure set truncated at {cap} records; the set is a "
                    "lower bound, not a complete list"
                )
                break

        observed = outcome in ("pass", "fail") and not zero_tests
        if outcome == OUTCOME_TIMEOUT:
            notes.append(
                "baseline run timed out: the pre-existing failure set is "
                "UNKNOWN, not empty"
            )
        elif outcome == OUTCOME_ERROR:
            notes.append(
                "baseline run broke (runner/environment error): the "
                "pre-existing failure set is UNKNOWN, not empty"
            )
        elif zero_tests:
            notes.append(
                "baseline collected zero tests: this is never a clean baseline"
            )

        env_names = declared
        env_notes: Tuple[str, ...] = ()
        if env_names is None:
            env_names, env_notes = declared_dependencies(repo_path)
        notes.extend(env_notes)
        environment = triage_environment(
            output=raw,
            repo_path=repo_path,
            declared_dependencies=env_names,
        )

        return BaselineSet(
            target_test=target_test,
            failures=tuple(records),
            collected=getattr(report, "tests_collected", None),
            passed=getattr(report, "tests_passed", None),
            failed=getattr(report, "tests_failed", None),
            skipped=getattr(report, "tests_skipped", None),
            outcome=outcome,
            source=str(getattr(report, "source", "") or "exit_code"),
            baseline_observed=observed,
            environment=environment,
            notes=tuple(notes),
        )
    except Exception as exc:  # a diagnosis must never kill a run
        return BaselineSet(
            target_test=target_test,
            baseline_observed=False,
            outcome=OUTCOME_ERROR,
            notes=(f"baseline set construction degraded: {type(exc).__name__}: {exc}",),
        )


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def default_baseline_path(run_dir: Optional[str]) -> str:
    """The baseline record's path beside a run's other artifacts.

    Assumes ``run_dir`` is a directory string (or Path). Returns "" for an
    empty input so a caller that has no run directory never writes to a
    relative path by accident.
    """
    try:
        root = str(run_dir or "").strip()
        if not root:
            return ""
        return os.path.join(root, BASELINE_FILE)
    except Exception:  # pragma: no cover — defensive
        return ""


def save_baseline(baseline: BaselineSet, path: str) -> bool:
    """Write the baseline record atomically; return True when it landed.

    Persistence is observability, not gating: an unwritable run directory must
    never fail a verification, so a write error returns False rather than
    raising. tmp+replace so a crash cannot leave a half-written record that a
    later reader would half-believe.
    """
    tmp = ""
    try:
        target = str(path or "")
        if not target:
            return False
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(baseline.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
        return True
    except (OSError, TypeError, ValueError):
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def load_baseline(run_dir: Optional[str]) -> Optional[BaselineSet]:
    """Load the recorded baseline, or None when there is none / it is unusable.

    Assumes ``run_dir`` is the same directory :func:`default_baseline_path`
    names. A missing file, unreadable file, invalid JSON, or unknown schema
    version all return None with no exception: "no usable baseline" is the
    honest answer, and :func:`classify_run` then treats every post-fix failure
    as new rather than excusing it.
    """
    path = default_baseline_path(run_dir)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return BaselineSet.from_dict(data)
    except (OSError, ValueError, TypeError):
        return None


def record_baseline(
    result: Any,
    *,
    run_dir: Optional[str] = None,
    target_test: Optional[str] = None,
    report: Optional[TestRunReport] = None,
    declared: Any = None,
    repo_path: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> BaselineSet:
    """Build AND persist the baseline set for a pristine-tree verification.

    The single call the pristine-verify call site needs. Assumes everything
    :func:`baseline_set_from_verification` assumes, plus ``run_dir`` when the
    record should be persisted (omit it to compute without writing). Returns
    the set either way; when persistence fails, a note says so, because a lost
    baseline is a real (if minor) loss of the run's audit trail.
    """
    baseline = baseline_set_from_verification(
        result,
        target_test=target_test,
        report=report,
        declared=declared,
        repo_path=repo_path,
        config=config,
    )
    path = default_baseline_path(run_dir)
    if path and not save_baseline(baseline, path):
        baseline = BaselineSet(
            target_test=baseline.target_test,
            failures=baseline.failures,
            collected=baseline.collected,
            passed=baseline.passed,
            failed=baseline.failed,
            skipped=baseline.skipped,
            outcome=baseline.outcome,
            source=baseline.source,
            baseline_observed=baseline.baseline_observed,
            environment=baseline.environment,
            notes=(
                *baseline.notes,
                f"baseline record could not be written to {path}",
            ),
        )
    return baseline


# ---------------------------------------------------------------------------
# the honest triple
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineVerdict:
    """``target_passed`` + ``new_failures`` + ``preexisting_failures``.

    ``target_passed`` alone is insufficient and slightly misleading: it cannot
    distinguish "the fix worked" from "the test was already broken either way",
    and it cannot name a repository that was already red. This record is the
    honest triple, with every pre-existing failure NAMED and attributed to the
    baseline.

    ``blocks_success`` is fail-closed by construction: it is True whenever the
    verifier's own evidence is not green, whenever a NEW failure appeared,
    whenever zero tests were collected, and whenever the failure is
    environment-class. This module can therefore only ADD a reason to refuse a
    success claim; it can never remove one.
    """

    target_test: Optional[str] = None
    target_passed: bool = False
    new_failures: Tuple[FailureRecord, ...] = ()
    preexisting_failures: Tuple[FailureRecord, ...] = ()
    preexisting_still_failing: Tuple[FailureRecord, ...] = ()
    regression_passed: Optional[bool] = None
    flaky: Optional[bool] = None
    zero_tests_collected: bool = False
    baseline_known: bool = False
    target_preexisting: bool = False
    environment: EnvironmentVerdict = field(default_factory=_no_environment)
    notes: Tuple[str, ...] = ()

    @property
    def new_failure_count(self) -> int:
        """How many failures this run is responsible for (must be 0 to pass)."""
        return len(self.new_failures)

    @property
    def preexisting_count(self) -> int:
        """How many failures were already failing before any edit."""
        return len(self.preexisting_failures)

    @property
    def preexisting_still_failing_count(self) -> int:
        """How many of the pre-existing failures are STILL failing.

        The intersection is the proof of attribution: these failures are
        observed in the post-fix run AND were already recorded at baseline, so
        the run inherited them rather than causing them.
        """
        return len(self.preexisting_still_failing)

    @property
    def blocks_success(self) -> bool:
        """True when this evidence must NOT produce a verified success."""
        if not self.target_passed:
            return True
        if self.regression_passed is not True:
            # Unknown regression evidence is treated as not-passed on purpose:
            # an absent signal is not a green light.
            return True
        if self.flaky:
            return True
        if self.new_failures:
            return True
        if self.zero_tests_collected:
            return True
        return bool(
            self.environment.environment and not self.environment.repairable_by_edit
        )

    def summary_line(self) -> str:
        """The honest triple, rendered as ONE line a report can quote.

        Names the pre-existing failures (up to a bound) so the sentence cannot
        read as a bare, contextless "preexisting: 3", and names the
        intersection separately so a reader can tell an INHERITED failure from
        one this run caused.
        """
        preexisting = (
            ", ".join(record.key for record in self.preexisting_failures[:5]) or "none"
        )
        more = len(self.preexisting_failures) - 5
        if more > 0:
            preexisting += f" (+{more} more)"
        new = ", ".join(record.key for record in self.new_failures[:5]) or "none"
        if len(self.new_failures) > 5:
            new += f" (+{len(self.new_failures) - 5} more)"
        parts = [
            f"target_passed={self.target_passed}",
            f"new_failures={self.new_failure_count} [{new}]",
            f"preexisting_failures={self.preexisting_count} [{preexisting}]",
        ]
        if self.preexisting_still_failing:
            parts.append(
                f"preexisting_still_failing={self.preexisting_still_failing_count} "
                f"(inherited, not caused by this run)"
            )
        if self.zero_tests_collected:
            parts.append("zero_tests_collected=True (never a pass)")
        if not self.baseline_known:
            parts.append("baseline_unknown=True (failures unattributed)")
        if self.environment.environment:
            parts.append(
                f"environment={self.environment.classification} "
                f"(repairable_by_edit="
                f"{self.environment.repairable_by_edit})"
            )
        return "; ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready projection for a trace row or run artifact."""
        return {
            "target_test": self.target_test,
            "target_passed": bool(self.target_passed),
            "new_failures": [record.to_dict() for record in self.new_failures],
            "new_failure_count": self.new_failure_count,
            "preexisting_failures": [
                record.to_dict() for record in self.preexisting_failures
            ],
            "preexisting_failure_count": self.preexisting_count,
            "preexisting_still_failing": [
                record.to_dict() for record in self.preexisting_still_failing
            ],
            "preexisting_still_failing_count": self.preexisting_still_failing_count,
            "regression_passed": self.regression_passed,
            "flaky": self.flaky,
            "zero_tests_collected": bool(self.zero_tests_collected),
            "baseline_known": bool(self.baseline_known),
            "target_preexisting": bool(self.target_preexisting),
            "environment": self.environment.to_dict(),
            "blocks_success": bool(self.blocks_success),
            "summary": self.summary_line(),
            "notes": list(self.notes),
        }


def classify_run(
    result: Any,
    *,
    baseline: Optional[BaselineSet] = None,
    target_test: Optional[str] = None,
    run_dir: Optional[str] = None,
    report: Optional[TestRunReport] = None,
    declared: Any = None,
    repo_path: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> BaselineVerdict:
    """Split a post-fix verification's failures into new vs pre-existing.

    The single call a result-assembly site needs. Assumes ``result`` is the
    post-fix `shared.types.VerificationResult`, ``baseline`` the
    :class:`BaselineSet` recorded from the pristine tree (loaded from
    ``run_dir`` when not supplied), and ``report`` an optional machine-readable
    report for the same post-fix run.

    Attribution rules, in order:

    - ``new_failures`` is every post-fix failure the recorded baseline did NOT
      already contain. Only those are this run's responsibility, and only they
      (besides the verifier's own evidence) block success.
    - ``preexisting_failures`` is the BASELINE's recorded set, named and
      counted, whether or not it still fails: "this repository arrived with
      these broken" is part of the honest report even when the fix worked.
    - ``preexisting_still_failing`` is the intersection - observed post-fix AND
      already known at baseline. Those are the failures the run INHERITED, and
      their presence is what makes "pre-existing, not a regression" a checkable
      claim rather than an assertion.
    - With no usable baseline (``None``, or one that was never observed), every
      failure is reported as `new` and the verdict says the baseline is unknown.
      Excusing failures against a baseline that was never collected is how a
      real regression becomes invisible.
    - An environment-class failure is reported in ``environment`` and always
      blocks success, whatever the baseline says: a broken machine is not a
      repository condition.

    Never raises; a degraded input produces a verdict with a note.
    """
    notes: List[str] = []
    try:
        if target_test is None and baseline is not None:
            target_test = baseline.target_test
        if baseline is None and run_dir:
            baseline = load_baseline(run_dir)
            if baseline is None:
                notes.append("no baseline record was found beside the run")
        post = baseline_set_from_verification(
            result,
            target_test=target_test,
            report=report,
            declared=declared,
            repo_path=repo_path,
            config=config,
        )
        environment = post.environment
        if (
            baseline is not None
            and baseline.environment.environment
            and not environment.environment
        ):
            # A pristine-tree environment fault outranks the post-fix reading
            # ONLY when the post-fix side has no classification of its own: the
            # machine was already wrong before any edit, so an unparseable
            # post-fix capture must not hide that. The post-fix reading is never
            # discarded — it is the closer evidence when it has something to say.
            environment = baseline.environment

        baseline_known = bool(baseline is not None and baseline.baseline_observed)
        if baseline is not None and not baseline.baseline_observed:
            notes.append(
                "the baseline run did not complete, so its failure set is "
                "unknown; every post-fix failure is reported as new"
            )

        still_failing: List[FailureRecord] = []
        new: List[FailureRecord] = []
        for record in post.failures:
            if baseline_known and baseline is not None and baseline.contains(record):
                still_failing.append(record)
            else:
                new.append(record)
        if not baseline_known and post.failures:
            notes.append(
                f"{len(post.failures)} post-fix failure(s) reported as new "
                "because no completed baseline exists to attribute them to"
            )

        target_preexisting = bool(
            target_test
            and baseline is not None
            and baseline_observed_target(baseline, target_test)
            and not bool(getattr(result, "target_test_passed", False))
        )
        if target_preexisting:
            notes.append(
                f"the target test {target_test} was ALREADY failing before any "
                "edit: its failure is pre-existing, not a regression this run "
                "caused"
            )
        zero_tests = post.outcome == OUTCOME_NO_TESTS
        if zero_tests:
            notes.append("zero tests collected: this is never a pass")
        if baseline is not None and baseline.outcome == OUTCOME_NO_TESTS:
            notes.append(
                "the baseline collected zero tests, so it could not establish "
                "a pre-existing failure set"
            )

        return BaselineVerdict(
            target_test=target_test,
            target_passed=bool(getattr(result, "target_test_passed", False)),
            new_failures=tuple(new),
            preexisting_failures=tuple(baseline.failures)
            if baseline is not None
            else (),
            preexisting_still_failing=tuple(still_failing),
            regression_passed=getattr(result, "regression_passed", None),
            flaky=getattr(result, "flaky", None),
            zero_tests_collected=bool(zero_tests),
            baseline_known=baseline_known,
            target_preexisting=target_preexisting,
            environment=environment,
            notes=tuple(notes) + tuple(post.notes),
        )
    except Exception as exc:  # a diagnosis must never kill a run
        return BaselineVerdict(
            target_test=target_test,
            target_passed=bool(getattr(result, "target_test_passed", False)),
            regression_passed=getattr(result, "regression_passed", None),
            flaky=getattr(result, "flaky", None),
            baseline_known=False,
            notes=(f"run classification degraded: {type(exc).__name__}: {exc}",),
        )


def baseline_observed_target(baseline: BaselineSet, target_test: Optional[str]) -> bool:
    """True when the recorded baseline saw this target FAIL.

    Separate from :meth:`BaselineSet.includes_target` (which asks whether the
    target appears among the failures) only in that it is the named call site
    for the "was it already broken?" question, and it additionally requires the
    baseline to have COMPLETED. An unobserved baseline cannot answer it.
    """
    try:
        if not baseline or not baseline.baseline_observed or not target_test:
            return False
        return baseline.includes_target()
    except Exception:  # pragma: no cover — defensive
        return False


def should_stop_for_environment(
    verdict: BaselineVerdict,
) -> Optional[Dict[str, Any]]:
    """The run's end state when an environment fault must stop it, else None.

    Returns ``None`` for every run that may continue (including one whose
    failures are all pre-existing), and a dict carrying ``status``, ``reason``,
    the classification, the operator action, and ``repairable_by_edit=False``
    for a run that must stop.

    The returned ``status`` is ``"error"``, the honest member of
    `TaskResult.status`'s closed set: the machine could not run the tests. The
    classification and the operator action are what make that distinguishable
    from a crash, and the payload never claims any form of success. A caller
    that ignored this dict and fell through to the retry loop would still not
    mint success, because `BaselineVerdict.blocks_success` is independently
    True for the same evidence — the stop is belt, the gate is braces.
    """
    try:
        environment = verdict.environment
        if not environment.environment or environment.repairable_by_edit:
            return None
        return {
            "status": environment.run_status or ENV_RUN_STATUS,
            "reason": (
                f"environment failure ({environment.classification}); not "
                "repairable by editing the repository"
            ),
            "classification": environment.classification,
            "action": environment.action,
            "operator_action": environment.operator_action or environment.hint,
            "repairable_by_edit": False,
            "report": environment.report(),
            "summary": verdict.summary_line(),
        }
    except Exception:  # pragma: no cover — defensive
        return None


def enabled_for(cfg: Optional[Mapping[str, Any]], key: str) -> bool:
    """Public key-presence gate for the two opt-in features.

    Exposed so a call site reads the SAME gate this module uses rather than
    re-implementing "is it on?" and risking a value check that switches every
    task at once.
    """
    return _config_enabled(cfg, key)
