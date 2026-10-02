"""Independent evidence: held-out acceptance tests and a separate judge
context (Ceiling 08 §3).

A visible test suite is a claim the builder can satisfy. The gap this closes is
measured, not assumed: the visible-suite score and the held-out score are both
computed, and the DIFFERENCE is the reward-hacking gap.

Three mechanisms, each with a specific failure it closes:

1. **Held-out acceptance tests that are not readable during the loop.**
   :class:`HeldOutSuite` materializes randomized acceptance tests OUTSIDE the
   repository under test, records a content digest, and refuses to reveal its
   files while the build loop is running. A builder that wants to satisfy them
   has to satisfy the SPECIFICATION, not the test text, because the text is not
   there. Randomization (order, boundary values, per-run nonce) is seeded and
   the seed is recorded, so a held-out run is reproducible.

2. **Lucky-pass detection.** A test that passes for a wrong reason (a shared
   cache, an ordering accident, an over-broad ``assert True``) is caught by
   running the SAME change against a held-out test that the visible suite
   cannot satisfy. :meth:`Judgment.gap_points` is
   ``visible_pct - heldout_pct``; a positive gap beyond the threshold is
   reported as a lucky pass rather than quietly averaged away.

3. **Evaluator tampering detection.** The held-out directory is fingerprinted
   before the loop and re-fingerprinted before the judge runs. A changed digest,
   a moved file, or a vanished file is reported as tampering. The judge also
   runs against its OWN clean copy of the repository rather than the builder's
   working tree, so a leftover artifact in the work tree cannot decide the
   verdict.

The judge is a separate EVALUATOR CONTEXT: it receives the claim, the visible
report, and the held-out suite, and it has no way to promote a claim to a pass
on its own. ``verdict`` can only be ``"verified"`` when the held-out suite
passed, no tampering was detected, and the visible claim was not a lucky pass.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution.result_parsing import TestRunReport, parse_test_run

#: Default gap, in percentage points, above which a visible/held-out difference
#: is reported as a lucky pass rather than noise.
DEFAULT_GAP_THRESHOLD_POINTS = 5.0

VERDICT_VERIFIED = "verified"
VERDICT_REJECTED = "rejected"
VERDICT_UNAVAILABLE = "unavailable"

#: Name of the generated acceptance module inside a held-out suite directory.
HELD_OUT_TEST_NAME = "test_held_out_acceptance.py"

#: A pinned, empty pytest config shipped with the suite. Without it pytest
#: walks the repository's ancestors looking for an ini file, and on a Windows
#: bind mount that walk can fail with an OSError that has nothing to do with the
#: code under test. Pinning the config removes the walk entirely.
HELD_OUT_CONFIG_NAME = "pytest.ini"

#: The held-out suite carries its own conftest so the module under test
#: resolves in BOTH the builder's working tree and the judge's clean copy. It
#: uses the process working directory, which the judge sets to the copy it is
#: evaluating, so no path is baked into the protected text.
_CONFTEST_BODY = '''"""Held-out suite bootstrap. Part of the protected held-out evidence."""

import os
import sys

_ROOT = os.getcwd()
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
'''


@dataclass(frozen=True)
class HeldOutFile:
    """One held-out test file plus the digest that must not change."""

    name: str
    digest: str
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view."""
        return {"name": self.name, "digest": self.digest, "path": self.path}


@dataclass
class HeldOutSuite:
    """Randomized acceptance tests held out of the build loop.

    The suite owns a directory OUTSIDE the repository under test. While
    ``concealed`` is True the files are mode ``0o000`` (POSIX) and the
    directory is additionally marked, so an accidental read in the loop gets a
    permission error rather than the answer. On a platform without POSIX modes
    the concealment degrades to a recorded flag plus a refusal from
    :meth:`read`, which is honest rather than silently weaker.
    """

    root: str
    seed: int = 0
    files: Tuple[HeldOutFile, ...] = ()
    concealed: bool = False
    note: str = ""
    sealed_fingerprint: str = ""

    @property
    def file_count(self) -> int:
        """Return the number of held-out files."""
        return len(self.files)

    def command(self, suite_cmd: str) -> Optional[str]:
        """Return a command that runs the held-out suite, or None when empty.

        The suite's own ``pytest.ini`` is pinned with ``-c`` so the runner does
        not walk the repository's ancestors looking for a config file. That walk
        is a real failure mode on a Windows bind mount (an ``OSError`` on a
        non-existent ancestor config path) and it has nothing to do with the
        code under test.
        """
        if not self.files or not suite_cmd:
            return None
        return (
            f"{suite_cmd} -c {os.path.join(self.root, HELD_OUT_CONFIG_NAME)} "
            "--json-report --json-report-file=" + os.path.join(self.root, "report.json")
        )

    def read(self, name: str) -> str:
        """Read a held-out file, refusing while the suite is concealed."""
        if self.concealed:
            raise PermissionError(
                f"held-out acceptance tests are concealed during the build loop: {name}"
            )
        with open(os.path.join(self.root, name), encoding="utf-8") as handle:
            return handle.read()

    def fingerprint(self) -> str:
        """Return a digest over every held-out file's current content.

        While the suite is concealed the files are (on POSIX) unreadable, so the
        digest recorded at build time is returned rather than a read that would
        fail. Tampering is therefore detected by the judge AFTER it reveals the
        suite, which is the only point where reading is legitimate.
        """
        if self.concealed and self.sealed_fingerprint:
            return self.sealed_fingerprint
        digest = hashlib.sha256()
        for name in sorted(entry.name for entry in self.files):
            path = os.path.join(self.root, name)
            try:
                with open(path, "rb") as handle:
                    digest.update(name.encode("utf-8"))
                    digest.update(handle.read())
            except OSError:
                digest.update(b"<missing>")
                digest.update(name.encode("utf-8"))
        return digest.hexdigest()

    def conceal(self) -> None:
        """Make the held-out files unreadable for the build loop."""
        self.sealed_fingerprint = (
            self.fingerprint()
            if not self.concealed
            else (self.sealed_fingerprint or self.fingerprint())
        )
        self.concealed = True
        self.note = _apply_mode(self.root, 0)

    def reveal(self) -> None:
        """Make the held-out files readable for the judge."""
        self.concealed = False
        self.note = _apply_mode(self.root, 0o700)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view that NEVER includes the test text."""
        return {
            "root": self.root,
            "seed": int(self.seed),
            "file_count": self.file_count,
            "concealed": bool(self.concealed),
            "fingerprint": self.fingerprint(),
            "files": [entry.to_dict() for entry in self.files],
            "note": self.note,
        }


@dataclass(frozen=True)
class TamperingReport:
    """Whether the held-out evidence was altered during the build loop."""

    ok: bool
    expected_fingerprint: str = ""
    actual_fingerprint: str = ""
    changed: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    added: Tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view."""
        return {
            "ok": bool(self.ok),
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
            "changed": list(self.changed),
            "missing": list(self.missing),
            "added": list(self.added),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Judgment:
    """An independent evaluator's verdict on one builder claim."""

    verdict: str
    visible_score: Optional[float] = None
    heldout_score: Optional[float] = None
    gap_points: Optional[float] = None
    threshold_points: float = DEFAULT_GAP_THRESHOLD_POINTS
    lucky_pass: bool = False
    tampering: Optional[TamperingReport] = None
    reasons: Tuple[str, ...] = ()
    heldout_report: Optional[Dict[str, Any]] = None
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """Return whether the judge accepts the claim."""
        return self.verdict == VERDICT_VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible view for trace/evidence records."""
        return {
            "verdict": self.verdict,
            "visible_score": self.visible_score,
            "heldout_score": self.heldout_score,
            "gap_points": self.gap_points,
            "threshold_points": self.threshold_points,
            "lucky_pass": bool(self.lucky_pass),
            "tampering": self.tampering.to_dict() if self.tampering else None,
            "reasons": list(self.reasons),
            "heldout_report": self.heldout_report,
            "claims": dict(self.claims),
        }


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def build_held_out_suite(
    root: str,
    cases: Sequence[Mapping[str, Any]],
    *,
    seed: int = 0,
    repo_path: str = "",
    module: str = "",
) -> HeldOutSuite:
    """Materialize randomized acceptance tests under ``root``.

    Two case shapes are accepted:

    - literal:  ``{"id": "add", "call": "add(2, 3)", "expect": 5}``
    - templated: ``{"id": "add", "expr": "add({a}, {b})", "inputs": [...],
      "expect": "{a} + {b}"}`` — one test is generated per input, with BOTH the
      expression and the expectation formatted from the same values, so the
      expectation cannot drift out of correctness.

    Randomization is the seeded shuffling of the case order and of the input
    order within each case. Order randomization is what a held-out suite
    actually needs: a builder that satisfies acceptance by memorizing a
    sequence is broken by a permutation, and the permutation is reproducible
    from the recorded seed. Input VALUES are authored, never synthesized from
    the implementation, so a correct implementation always passes.

    A sibling ``conftest.py`` puts the process working directory on
    ``sys.path``. That is what lets the same suite run against the builder's
    tree and against the judge's independent copy without baking either path
    into the generated text.

    The suite is written OUTSIDE ``repo_path`` by construction and the check is
    enforced here rather than trusted.
    """
    target = str(root)
    os.makedirs(target, exist_ok=True)
    if repo_path and _is_within(target, str(repo_path)):
        raise ValueError(
            "held-out acceptance tests must live outside the repository under test"
        )
    rng = random.Random(int(seed))
    subject = module or _guess_module(repo_path)

    ordered = [dict(case) for case in cases]
    rng.shuffle(ordered)

    lines: List[str] = [
        '"""Held-out acceptance tests, generated by execution.independent_evidence.',
        "",
        "These tests are NOT visible to the build loop. Do not edit this file:",
        "its content digest is checked by the independent judge before it runs,",
        "and a changed digest is reported as tampering.",
        "",
        f"Randomization seed: {int(seed)}",
        '"""',
        "",
    ]
    if subject:
        lines.append(f"from {subject} import *  # noqa: F401,F403")
        lines.append("")

    for case in ordered:
        for index, (label, expression, expected) in enumerate(_expand_case(case)):
            if not expression:
                continue
            lines.append("")
            lines.append(f"def test_{_identifier(label)}{index}():")
            lines.append(f"    assert {expression} == {expected}, {label!r}")

    test_path = os.path.join(target, HELD_OUT_TEST_NAME)
    _write_text(test_path, "\n".join(lines) + "\n")
    _write_text(os.path.join(target, HELD_OUT_CONFIG_NAME), "[pytest]\n")
    conftest_path = os.path.join(target, "conftest.py")
    _write_text(
        conftest_path,
        _CONFTEST_BODY,
    )

    files = tuple(
        HeldOutFile(
            name=name,
            digest=_sha256_text(_read_text(os.path.join(target, name))),
            path=os.path.join(target, name),
        )
        for name in sorted((HELD_OUT_TEST_NAME, HELD_OUT_CONFIG_NAME, "conftest.py"))
    )
    suite = HeldOutSuite(
        root=target,
        seed=int(seed),
        files=files,
        concealed=False,
        note="",
    )
    suite.sealed_fingerprint = suite.fingerprint()
    _apply_mode(target, 0o700)
    return suite


def _expand_case(case: Mapping[str, Any]) -> List[Tuple[str, str, str]]:
    """Return (label, expression, expected) triples for one spec case."""
    case_id = str(case.get("id") or "held_out")
    literal = str(case.get("call") or "").strip()
    if literal:
        return [(case_id, literal, repr(case.get("expect")))]
    template = str(case.get("expr") or "").strip()
    inputs = case.get("inputs")
    if (
        not template
        or not isinstance(inputs, Sequence)
        or isinstance(inputs, (str, bytes))
    ):
        return []
    expect_template = case.get("expect")
    triples: List[Tuple[str, str, str]] = []
    for index, values in enumerate(inputs):
        if not isinstance(values, Mapping):
            continue
        try:
            expression = template.format(**dict(values))
        except (KeyError, IndexError, ValueError):
            continue
        if isinstance(expect_template, str) and "{" in expect_template:
            try:
                expected = expect_template.format(**dict(values))
            except (KeyError, IndexError, ValueError):
                continue
        else:
            expected = repr(expect_template)
        triples.append((f"{case_id}_{index}", expression, expected))
    return triples


def detect_tampering(
    suite: HeldOutSuite, *, expected_fingerprint: str
) -> TamperingReport:
    """Compare the held-out suite against a pre-loop fingerprint.

    ``expected_fingerprint`` empty means no fingerprint was recorded, which is
    reported as a failure: an unsealed held-out suite cannot prove it was not
    edited, and saying so is the honest answer.
    """
    if not expected_fingerprint:
        return TamperingReport(
            ok=False, reason="no held-out fingerprint was recorded before the loop"
        )
    if not suite.files:
        return TamperingReport(
            ok=False, reason="the held-out suite is empty; there is nothing to protect"
        )
    actual = suite.fingerprint()
    changed: List[str] = []
    missing: List[str] = []
    present = {entry.name for entry in suite.files}
    for entry in suite.files:
        path = entry.path or os.path.join(suite.root, entry.name)
        if not os.path.isfile(path):
            missing.append(entry.name)
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            if _sha256_text(handle.read()) != entry.digest:
                changed.append(entry.name)
    on_disk = (
        {name for name in os.listdir(suite.root) if name.endswith(".py")}
        if os.path.isdir(suite.root)
        else set()
    )
    added = sorted(on_disk - present)
    ok = not changed and not missing and not added and actual == expected_fingerprint
    reasons: List[str] = []
    if changed:
        reasons.append("held-out files changed: " + ", ".join(sorted(changed)))
    if missing:
        reasons.append("held-out files missing: " + ", ".join(sorted(missing)))
    if added:
        reasons.append(
            "unexpected files added to the held-out directory: " + ", ".join(added)
        )
    if actual != expected_fingerprint:
        reasons.append("held-out fingerprint changed")
    return TamperingReport(
        ok=ok,
        expected_fingerprint=expected_fingerprint,
        actual_fingerprint=actual,
        changed=tuple(sorted(changed)),
        missing=tuple(sorted(missing)),
        added=tuple(added),
        reason="; ".join(reasons),
    )


# ---------------------------------------------------------------------------
# judging
# ---------------------------------------------------------------------------


def compare_scores(
    visible: Optional[float], heldout: Optional[float]
) -> Optional[float]:
    """Return the reward-hacking gap in percentage points, or None.

    ``visible_pct - heldout_pct``. A negative gap (the held-out suite is easier)
    is still reported; rounding keeps the number stable for reports.
    """
    if visible is None or heldout is None:
        return None
    return round(float(visible) - float(heldout), 4)


def score_from_report(report: Optional[TestRunReport]) -> Optional[float]:
    """Return a 0-100 pass percentage from a report, or None when unusable.

    A missing ``tests_passed`` is derived from collected/failed/skipped rather
    than treated as unknown: a report that says "1 collected, 1 failed" is a
    complete 0% statement, and refusing to score it would hide exactly the
    lucky-pass case this module exists to find.
    """
    if report is None:
        return None
    collected = report.tests_collected
    if not collected:
        return None
    passed = report.tests_passed
    if passed is None:
        failed = report.tests_failed or 0
        skipped = report.tests_skipped or 0
        passed = max(0, collected - failed - skipped)
    return round(100.0 * float(passed) / float(collected), 4)


def judge(
    suite: HeldOutSuite,
    *,
    run: Callable[[str, HeldOutSuite], Any],
    visible_report: Optional[TestRunReport] = None,
    claims: Optional[Mapping[str, Any]] = None,
    expected_fingerprint: str = "",
    threshold_points: float = DEFAULT_GAP_THRESHOLD_POINTS,
    repo_path: str = "",
    clean_repo_path: str = "",
) -> Judgment:
    """Run the held-out suite in an independent context and judge the claim.

    ``run`` receives the repository path to evaluate and the suite, and returns
    anything :func:`execution.result_parsing.parse_test_run` accepts. The judge
    reveals the suite, evaluates ``clean_repo_path or repo_path``, re-conceals
    it, and refuses to return ``verified`` when tampering is detected.

    The judge cannot mint success: ``VERDICT_VERIFIED`` requires a held-out run
    that actually passed with at least one collected test.
    """
    reasons: List[str] = []
    if not suite.files:
        return Judgment(
            verdict=VERDICT_UNAVAILABLE,
            reasons=(
                *(reasons or ()),
                "no held-out acceptance tests were generated",
            ),
            threshold_points=threshold_points,
            claims=dict(claims or {}),
        )

    was_concealed = suite.concealed
    suite.reveal()
    try:
        tampering = detect_tampering(suite, expected_fingerprint=expected_fingerprint)
        if not tampering.ok:
            reasons.append(tampering.reason or "held-out evidence was not intact")
        try:
            raw = run(clean_repo_path or repo_path, suite)
        except Exception as exc:
            raw = None
            reasons.append(
                f"held-out run failed to execute: {type(exc).__name__}: {exc}"
            )
    finally:
        if was_concealed:
            suite.conceal()
    report = raw if isinstance(raw, TestRunReport) else parse_test_run(raw)

    visible_score = score_from_report(visible_report)
    heldout_score = score_from_report(report)
    gap = compare_scores(visible_score, heldout_score)
    lucky = bool(gap is not None and gap > float(threshold_points))

    if not report.passed:
        reasons.append(f"held-out acceptance did not pass (outcome={report.outcome})")
    elif not report.tests_collected:
        reasons.append("held-out run collected no tests; a lucky pass is possible")
    if lucky:
        reasons.append(
            f"visible {visible_score}% vs held-out {heldout_score}% exceeds the "
            f"{threshold_points} point gap threshold"
        )

    verdict = (
        VERDICT_VERIFIED
        if (report.passed and tampering.ok and not lucky)
        else VERDICT_REJECTED
    )
    return Judgment(
        verdict=verdict,
        visible_score=visible_score,
        heldout_score=heldout_score,
        gap_points=gap,
        threshold_points=float(threshold_points),
        lucky_pass=lucky,
        tampering=tampering,
        reasons=tuple(reasons),
        heldout_report=report.to_dict(),
        claims=dict(claims or {}),
    )


def premature_completion(
    *,
    claimed_complete: bool,
    final_gate_ran: bool,
    final_gate_passed: bool = False,
    spec_guard_ok: Optional[bool] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """Report whether a completion claim is premature.

    Premature means: the builder (or any caller) declared completion without a
    clean final gate. The check is deliberately dumb and mechanical, because
    this is the assertion that keeps "final success is never minted by the
    model" true at the boundary where a claim becomes a result.
    """
    premature = bool(claimed_complete and not (final_gate_ran and final_gate_passed))
    detail = reason or (
        "final gate did not pass"
        if claimed_complete and not (final_gate_ran and final_gate_passed)
        else "no premature completion detected"
    )
    return {
        "claimed_complete": bool(claimed_complete),
        "final_gate_ran": bool(final_gate_ran),
        "final_gate_passed": bool(final_gate_passed),
        "spec_guard_ok": spec_guard_ok,
        "premature": premature,
        "reason": detail,
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _identifier(value: str) -> str:
    """Return a Python identifier fragment for a case id."""
    out = "".join(char if char.isalnum() else "_" for char in str(value))
    return out.strip("_") or "case"


def _guess_module(repo_path: str) -> str:
    """Return the most likely single top-level module of a repository."""
    if not repo_path:
        return ""
    try:
        entries = sorted(os.listdir(repo_path))
    except OSError:
        return ""
    for entry in entries:
        path = os.path.join(repo_path, entry)
        if (
            os.path.isfile(path)
            and entry.endswith(".py")
            and not entry.startswith("test")
        ):
            return entry[: -len(".py")]
    for entry in entries:
        path = os.path.join(repo_path, entry)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "__init__.py")):
            return entry
    return ""


def _is_within(child: str, parent: str) -> bool:
    """Return whether ``child`` is inside ``parent`` (or equal to it)."""
    child_abs = os.path.abspath(child)
    parent_abs = os.path.abspath(parent)
    try:
        return os.path.commonpath([child_abs, parent_abs]) == parent_abs
    except ValueError:
        return False


def _apply_mode(root: str, mode: int) -> str:
    """Best-effort POSIX mode change; returns a note describing what happened."""
    if os.name == "nt":
        return "posix_modes_unavailable_on_windows"
    try:
        os.chmod(root, mode)
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), mode)
                except OSError:
                    continue
    except OSError:
        return "chmod_failed"
    return "applied"


def _sha256_text(text: str) -> str:
    """Return the SHA-256 of ``text``."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _read_text(path: str) -> str:
    """Read a text file, replacing undecodable bytes rather than raising."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _write_text(path: str, text: str) -> None:
    """Write text with UTF-8 encoding, creating parents as needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def copy_tree(source: str, target: str) -> str:
    """Copy a repository to ``target`` for an independent evaluation context.

    The judge's own copy is what makes the verdict independent of whatever
    happens to be lying around in the builder's working tree.
    """
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)
    return target
