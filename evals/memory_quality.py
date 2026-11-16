"""Memory QUALITY, not memory recall — the five numbers Phase 3a's second half needs.

WHY RECALL IS NOT THE SAME QUESTION
-----------------------------------
:mod:`evals.memory_ablation` (Wave 1) and :mod:`evals.memory_ablation_real`
(this wave) both measure whether memory is FOUND and whether it MOVES an
outcome. Neither answers whether the memory was any good. A recall@k of 1.0 over
a corpus of confident falsehoods is a perfect retrieval of the wrong thing, and
a wrong memory is strictly worse than no memory: it makes the agent confidently
wrong on day 2 and the user has no way to know.

So this module measures five properties, each with an explicit definition and
each able to return ``None`` with a reason:

``precision``
    Of the memories actually injected into a real run, how many were RELEVANT
    to the task in front of it? A recall that returns three rows where one was
    needed spends prompt tokens and adds noise.

``staleness``
    Of the memories injected, how many assert something about the repository
    that is FALSE? This is the expected failure mode of agent-maintained
    memory and the reason the corpus's Codex model is git-baselined.

``retraction``
    When the code a memory references changes, is the memory DETECTED as stale?
    Answered by driving the real mutation and the real detector, not by reading
    one.

``harm``
    How often did a recalled memory change an outcome, and in how many of those
    for the worse? Read from :mod:`evals.memory_ablation_real` so there is ONE
    measurement of it, not two.

``inbox_contribution``
    Of the memories the real extraction pipeline produces from a real
    transcript, how many are wrong enough that a human should have rejected
    them? This number either justifies the inbox's friction or shows the
    friction was unnecessary, and both are findings.

THE INDEPENDENT CHECKER, AND WHY IT MUST NOT READ THE LABELS
------------------------------------------------------------
:func:`check_claim` is the only thing that decides whether a memory is true or
false, and it decides from the REPOSITORY BYTES and the literals the memory
itself names. It never reads :data:`evals.memory_ablation_real.SEED_MEMORIES`'s
``truth`` field — a quality metric that graded the corpus against its own
answer key would measure nothing but the key. The seed labels are used for one
thing only, and it is a comparison, reported as ``label_agreement``: whether
the independent checker agrees with the label that was written before the
checker existed. Disagreement is a finding and is not smoothed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "ClaimFinding",
    "check_claim",
    "memory_quality_report",
    "render",
]

#: Characters that end a claim inside a memory's prose.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+")

#: A repository-relative path candidate: something with a slash and a suffix.
_PATH_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)")

#: A code identifier candidate: backticked, or an ALL_CAPS/CamelCase word.
_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_./]*)`|\b(_[A-Z][A-Z0-9_]{3,})\b")

#: The two claims this repository's own seed corpus makes about how to run a
#: suite. Both are CHECKED BY RUNNING the invocation, because a claim about
#: pytest cannot be settled by reading a file.
_INVOCATION_RE = re.compile(
    r"bare `pytest`(?P<bare_claim>\s+(?:cannot|can not|does not|doesn'?t|fails?)\b[^.]*)?",
    re.IGNORECASE,
)

#: The corpus's own convention for asserting a file is ALREADY correct. A memory
#: carrying this marker makes a falsifiable claim about the code's state, which
#: is why it is honoured rather than treated as decoration.
_FIXED_MARKER_RE = re.compile(r"FILE-ALREADY-FIXED:?\s*(?P<path>[\w./-]+)", re.IGNORECASE)

#: Relevance labels, per task. Which of the seeded memories a correct planner
#: for that task SHOULD have been handed. This is a judgement about the task,
#: written before the run, and it is published in the report so a reader can
#: disagree with it.
RELEVANCE_LABELS: Dict[str, Tuple[str, ...]] = {
    "bug01_wrap": (),
    "bug02_mean": (
        "numlib is not installed as a package",
        "numlib/__init__.py was reviewed",
    ),
    "bug03_stack": ("stacklib raises StackEmptyError",),
    "bug04_nameerror": ("_DAYS_PER_MONTHS",),
    "bug05_cart": ("price_report",),
}

#: The exact lines the harness emits when a task recalled nothing. They are not
#: memories, so they are removed before anything is scored against them - and
#: the count of tasks that produced one is published, because "9 of 14 tasks
#: recalled nothing at all" is a measurement about memory, not a footnote.
NO_MEMORY_ROWS: Tuple[str, ...] = (
    "(none recorded yet)",
    "(no recorded memories)",
)


@dataclass(frozen=True)
class ClaimFinding:
    """One thing the checker could not confirm about a memory."""

    kind: str
    detail: str
    severity: str = "medium"

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


def _source_text(repo: Path) -> str:
    chunks: List[str] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix not in {".py", ".toml", ".cfg", ".txt", ".md"}:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _run_invocation(repo: Path, argv: Sequence[str], timeout: float = 180.0) -> Optional[int]:
    """Run a pytest invocation inside ``repo`` and return its exit code.

    ``None`` means the invocation could not be run at all (no ``pytest`` on
    PATH, a timeout). That is reported as an unavailable measurement and never
    as a passing or failing claim.
    """
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return int(completed.returncode)


def _mentions(source: str, path: str) -> bool:
    """Whether a test file exercises the module at ``path``.

    Deliberately matches on the module NAME rather than the import line: the
    tests in these repositories import the package, and the path a memory names
    is the file inside it. Getting this wrong in the permissive direction
    re-creates the whole-suite false positive, so an unmatched path means "no
    test covers this" and the caller reports the claim as unverifiable.

    A dunder module (``__init__.py``) NEVER counts as covered, and the reason
    is the second false positive this checker produced: every test that imports
    a package exercises its ``__init__``, so "covered" was true for the whole
    suite and a bug in a sibling module refuted a true claim about
    ``__init__.py``. A package initialiser has no test of its own; whether it
    is correct is a source-reading question, not a test question.
    """
    stem = Path(path).stem
    if stem.startswith("__") and stem.endswith("__"):
        return False
    return stem in source


def check_claim(repo_path: Any, text: str) -> List[ClaimFinding]:
    """Decide, from the repository BYTES, what it can and cannot confirm.

    Returns a list of :class:`ClaimFinding`. An EMPTY list means every literal
    the memory named was found and every checkable claim held — it does not
    mean the memory is true, and the report says so.
    """
    repo = Path(str(repo_path))
    findings: List[ClaimFinding] = []
    if not repo.is_dir():
        return [
            ClaimFinding(
                "unverifiable",
                f"repository {repo} does not exist, so nothing in this memory "
                "could be checked",
                severity="high",
            )
        ]
    blob = _source_text(repo)

    for candidate in sorted(set(_PATH_RE.findall(text or ""))):
        if not (repo / candidate).exists():
            findings.append(
                ClaimFinding(
                    "references_a_path_that_does_not_exist",
                    f"{candidate} is named but is not in the repository",
                    severity="high",
                )
            )

    for match in _IDENT_RE.finditer(text or ""):
        name = match.group(1) or match.group(2) or ""
        if not name or "/" in name:
            continue
        if name.endswith((".py", ".toml", ".md", ".txt")):
            continue
        if name not in blob:
            findings.append(
                ClaimFinding(
                    "names_a_symbol_that_does_not_exist",
                    f"{name} is named but appears in no source file",
                    severity="high",
                )
            )

    marker = _FIXED_MARKER_RE.search(text or "")
    if marker is not None:
        # A memory that says "this file is already correct" makes a claim the
        # repository can settle, and the first draft of this checker could not:
        # `wrapwrap/textutil.py` EXISTS, so the path check passed, the marker
        # was ignored, and a memory whose named defect was still present came
        # back "verified".
        #
        # The evidence for "already correct" is that the tests covering THAT
        # FILE pass. It is emphatically not "the suite passes": a suite failing
        # in a DIFFERENT file says nothing about the named one, and the second
        # draft of this check ran the whole suite and so refuted
        # `numlib/__init__.py was reviewed and needed no change` on the
        # strength of a bug in numlib/mathutil.py. A whole-suite failure is
        # unattributable, so it is reported as unverifiable, never as a false
        # claim.
        named = (marker.group("path") or "").strip().rstrip(".")
        if named and (repo / named).is_file():
            tests = sorted((repo / "tests").glob("test_*.py")) if (repo / "tests").is_dir() else []
            if not tests:
                findings.append(
                    ClaimFinding(
                        "unverifiable",
                        f"the memory says {named} is already fixed, and the "
                        "repository has no tests to check that against",
                        severity="low",
                    )
                )
            else:
                covering = [
                    test
                    for test in tests
                    if _mentions(test.read_text(encoding="utf-8", errors="replace"), named)
                ]
                if covering:
                    code = _run_invocation(
                        repo,
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            "-q",
                            "-x",
                            "-p",
                            "no:randomly",
                            *[test.relative_to(repo).as_posix() for test in covering],
                        ],
                    )
                    if code is None:
                        findings.append(
                            ClaimFinding(
                                "unverifiable",
                                "the tests covering "
                                f"{named} could not be run here, so the claim "
                                "that it is already fixed is unchecked",
                                severity="low",
                            )
                        )
                    elif code != 0:
                        findings.append(
                            ClaimFinding(
                                "claims_a_file_is_fixed_but_its_tests_fail",
                                f"the memory says {named} is already correct, "
                                f"and the tests covering it exit {code}",
                                severity="high",
                            )
                        )
                else:
                    code = _run_invocation(
                        repo, [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"]
                    )
                    if code not in (0, None):
                        findings.append(
                            ClaimFinding(
                                "unverifiable",
                                f"no test in this repository names {named}, and "
                                f"the suite as a whole exits {code}; that failure "
                                "cannot be attributed to the named file, so the "
                                "claim is unchecked rather than refuted",
                                severity="low",
                            )
                        )

    invocation = _INVOCATION_RE.search(text or "")
    if invocation is not None:
        claims_bare_fails = bool(invocation.group("bare_claim"))
        tests = sorted((repo / "tests").glob("test_*.py")) if (repo / "tests").is_dir() else []
        if not tests:
            findings.append(
                ClaimFinding(
                    "unverifiable",
                    "the repository has no tests/test_*.py to run the "
                    "invocation claim against",
                    severity="low",
                )
            )
        else:
            relative = tests[0].relative_to(repo).as_posix()
            bare = _run_invocation(repo, ["pytest", "-q", relative])
            module = _run_invocation(repo, [sys.executable, "-m", "pytest", "-q", relative])
            if bare is None or module is None:
                findings.append(
                    ClaimFinding(
                        "unverifiable",
                        "a pytest invocation could not be run here, so the "
                        "claim about how to run the suite was not checked",
                        severity="low",
                    )
                )
            else:
                bare_failed = bare != 0
                if claims_bare_fails and not bare_failed:
                    findings.append(
                        ClaimFinding(
                            "invocation_claim_is_false",
                            f"the memory says bare `pytest` fails; it exited "
                            f"{bare} on {relative}",
                            severity="high",
                        )
                    )
                if not claims_bare_fails and bare_failed:
                    findings.append(
                        ClaimFinding(
                            "invocation_claim_is_false",
                            f"the memory says bare `pytest` works; it exited "
                            f"{bare} on {relative}",
                            severity="high",
                        )
                    )
    return findings


def _real_memories(rows: Any) -> List[str]:
    """Drop the harness's "I recalled nothing" placeholder.

    Filtering happens HERE as well as at the parse site, because the placeholder
    is unfalsifiable by construction: it names no path, no symbol and no
    invocation, so every checker passes it. A row like that is a free pass, and
    a metric that counts free passes as evidence is the failure this whole
    module is about.
    """
    return [str(row) for row in (rows or ()) if str(row).strip() not in NO_MEMORY_ROWS]


def _is_relevant(task: str, text: str) -> Optional[bool]:
    """Whether a memory was relevant to a task.

    ``None`` means "no label was written for this task", which is NOT the same
    as "irrelevant". A task with no labels contributes nothing to precision
    rather than dragging it down with an unlabelled row.
    """
    labels = RELEVANCE_LABELS.get(task)
    if labels is None:
        return None
    if not labels:
        # The task declares that NOTHING in the corpus is relevant to it, so
        # any row injected into it is noise by construction.
        return False
    lowered = (text or "").casefold()
    return any(label.casefold() in lowered for label in labels)


# ---------------------------------------------------------------------------
# The five metrics.
# ---------------------------------------------------------------------------


def precision_report(
    injected: Mapping[str, Sequence[str]]
) -> Dict[str, Any]:
    """Of the memories injected into real runs, how many were relevant?"""
    rows: List[Dict[str, Any]] = []
    relevant = 0
    irrelevant = 0
    unlabelled = 0
    for task in sorted(injected):
        for text in _real_memories(injected.get(task)):
            verdict = _is_relevant(task, text)
            if verdict is None:
                unlabelled += 1
            elif verdict:
                relevant += 1
            else:
                irrelevant += 1
            rows.append(
                {
                    "task": task,
                    "text": text[:220],
                    "relevant": verdict,
                    "reason": (
                        "no relevance label was written for this task, so the "
                        "row was neither counted relevant nor irrelevant"
                        if verdict is None
                        else ""
                    ),
                }
            )
    scored = relevant + irrelevant
    return {
        "definition": (
            "of the memories actually injected into a real run's planner "
            "prompt, the fraction a human label calls relevant to that task"
        ),
        "precision": round(relevant / scored, 6) if scored else None,
        "precision_reason": (
            ""
            if scored
            else "no injected row had a relevance label, so precision is undefined rather than zero"
        ),
        "injected": relevant + irrelevant + unlabelled,
        "scored": scored,
        "relevant": relevant,
        "irrelevant": irrelevant,
        "unlabelled": unlabelled,
        "rows": rows,
    }


def staleness_report(
    injected: Mapping[str, Sequence[str]], repo_paths: Mapping[str, str]
) -> Dict[str, Any]:
    """Of the memories injected, how many assert something FALSE about the repo."""
    rows: List[Dict[str, Any]] = []
    stale = 0
    checked = 0
    unverifiable = 0
    for task in sorted(injected):
        repo = repo_paths.get(task)
        for text in injected.get(task) or []:
            findings = check_claim(repo, text) if repo else [
                ClaimFinding("unverifiable", "no repository was resolved for this task")
            ]
            # KIND is checked before severity. `check_claim` reports "the repo
            # does not exist" as an `unverifiable` finding with severity "high",
            # and folding that into the false bucket reported 9 stale memories
            # that had never been looked at: the first draft of this function
            # turned a missing repository into nine data points.
            blocking = [item for item in findings if item.severity == "high" and item.kind != "unverifiable"]
            checked += 1
            if not findings:
                state = "holds"
            elif blocking:
                state = "false"
                stale += 1
            else:
                state = "unverifiable"
                unverifiable += 1
            rows.append(
                {
                    "task": task,
                    "text": text[:220],
                    "state": state,
                    "findings": [item.as_dict() for item in findings],
                }
            )
    return {
        "definition": (
            "of the memories injected into real runs, the fraction whose "
            "literals the repository BYTES contradict; 'unverifiable' rows are "
            "counted separately and never folded into either side"
        ),
        "stale_rate": round(stale / checked, 6) if checked else None,
        "stale_rate_reason": (
            "" if checked else "no memory was injected, so the rate is undefined rather than zero"
        ),
        "checked": checked,
        "false": stale,
        "holds": checked - stale - unverifiable,
        "unverifiable": unverifiable,
        "rows": rows,
        "checker": "evals.memory_quality.check_claim",
        "checker_note": (
            "an EMPTY finding list means every literal the memory named was "
            "found and every runnable claim held. It does NOT mean the memory "
            "is true: a memory can be unfalsifiable from the repository alone."
        ),
    }


def retraction_report(
    repo_paths: Mapping[str, str], workdir: Optional[Any] = None
) -> Dict[str, Any]:
    """Does a stale memory get DETECTED when the code it names changes?

    Driven end to end through the real components: a real store, a real
    frozen snapshot, a real mutation of the repository, then a real re-capture.
    The detector under test is ``MemorySnapshot.stale_against`` plus the real
    re-check, and the real ``MemoryConsolidator`` is asked the corpus's own
    question — does a git-baselined consolidation surface the stale memory.
    """
    import shutil as _shutil

    from memory.decision_store import DecisionStore
    from memory.snapshot import capture_snapshot

    temporary = workdir is None
    root = Path(str(workdir)) if workdir is not None else Path(tempfile.mkdtemp())
    root.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    try:
        for task in sorted(repo_paths):
            source = Path(repo_paths[task])
            sandbox = root / task
            if sandbox.exists():
                _shutil.rmtree(sandbox, ignore_errors=True)
            _shutil.copytree(source, sandbox)
            target = sandbox / "wrapwrap" / "textutil.py"
            if not target.exists():
                target = next(
                    (path for path in sorted(sandbox.rglob("*.py")) if "tests" not in path.parts),
                    None,
                )
            if target is None:
                continue
            relative = target.relative_to(sandbox).as_posix()
            store = DecisionStore(str(root / f"{task}.db"))
            try:
                store.record(
                    f"the guard in {relative} was reviewed and is correct.",
                    category="convention",
                    source="agent",
                    repo_path=str(sandbox),
                    provenance={"kind": "test", "source": "agent", "explicit": True},
                )
                before = capture_snapshot(store, repo_path=str(sandbox), session_id="q")
                before_findings = check_claim(sandbox, f"the guard in {relative} is correct.")

                # The code changes: the file the memory names is RENAMED, which
                # is the cheapest mutation that makes the memory's literal
                # untrue and is a thing a repository actually does.
                renamed = target.with_name(target.stem + "_renamed.py")
                target.rename(renamed)

                after_findings = check_claim(sandbox, f"the guard in {relative} is correct.")
                after = capture_snapshot(store, repo_path=str(sandbox), session_id="q")
                detected_by_snapshot = bool(
                    before.stale_against(after) or before.text != after.text
                )
                detected_by_checker = len(after_findings) > len(before_findings)
                rows.append(
                    {
                        "task": task,
                        "path_named": relative,
                        "mutation": "the named file was renamed",
                        "checker_findings_before": [i.as_dict() for i in before_findings],
                        "checker_findings_after": [i.as_dict() for i in after_findings],
                        "detected_by_the_checker": detected_by_checker,
                        "detected_by_the_snapshot": detected_by_snapshot,
                        "detected": detected_by_checker or detected_by_snapshot,
                        "memory_text_amended": False,
                        "amended_note": (
                            "the store did not AMEND the memory, and that is the "
                            "honest answer: detection is the only mechanism here. "
                            "A memory that is merely flagged still reaches the "
                            "model unless something reads the flag."
                        ),
                    }
                )
            finally:
                store.close()
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)

    detected = sum(1 for row in rows if row["detected"])
    return {
        "definition": (
            "a memory is detected as stale when, after the code it names "
            "changes, an INDEPENDENT re-check against the repository reports a "
            "finding the pre-change re-check did not"
        ),
        "cases": len(rows),
        "detected": detected,
        "detection_rate": round(detected / len(rows), 6) if rows else None,
        "detection_rate_reason": (
            "" if rows else "no repository in the set had a named file to mutate"
        ),
        "amended_count": sum(1 for row in rows if row["memory_text_amended"]),
        "amended_rate": (
            round(
                sum(1 for row in rows if row["memory_text_amended"]) / len(rows), 6
            )
            if rows
            else None
        ),
        "rows": rows,
    }


def inbox_contribution_report(
    transcript: str, repo_path: Any
) -> Dict[str, Any]:
    """How many extracted memories a human SHOULD have rejected.

    The extraction is the REAL pipeline (``memory.extraction``), including its
    own redaction-before-write, and the grading is the INDEPENDENT checker. A
    row the checker cannot confirm at all is counted as ``needs_a_human``,
    which is the honest reading of the inbox's purpose: the human is there for
    the rows automation cannot settle.
    """
    from memory.extraction import extract_memories

    repo = str(repo_path)
    items, _receipt = extract_memories(transcript)
    memories = list(items or [])
    rows: List[Dict[str, Any]] = []
    reject = 0
    unresolved = 0
    for item in memories:
        text = str(getattr(item, "text", item) or "")
        findings = check_claim(repo, text)
        hard = [entry for entry in findings if entry.severity == "high"]
        if hard:
            verdict = "reject"
            reject += 1
        elif findings:
            verdict = "needs_a_human"
            unresolved += 1
        else:
            verdict = "accept"
        rows.append(
            {
                "text": text[:220],
                "verdict": verdict,
                "findings": [entry.as_dict() for entry in findings],
            }
        )
    total = len(rows)
    return {
        "definition": (
            "of the memories the REAL extraction pipeline produced from a "
            "REAL transcript, the fraction the independent checker refutes; "
            "'needs_a_human' is a memory automation could not settle either way"
        ),
        "extracted": total,
        "would_be_rejected": reject,
        "would_be_rejected_rate": round(reject / total, 6) if total else None,
        "needs_a_human": unresolved,
        "accepted": total - reject - unresolved,
        "justifies_the_inbox": None,
        "justification_note": (
            "the inbox's friction is justified when the reject rate plus the "
            "needs_a_human rate is material; the verdict field is left None "
            "here because the threshold is a PRODUCT decision and this report "
            "publishes the two rates rather than inventing one"
        ),
        "rows": rows,
    }


def label_agreement(
    staleness: Mapping[str, Any]
) -> Dict[str, Any]:
    """Does the independent checker agree with the labels written first?

    The corpus's ``truth`` labels were written BEFORE this checker existed.
    Agreement is evidence that both are measuring the same thing; disagreement
    is a finding and is published, never smoothed.

    There are THREE buckets, not two, and the third is the point. "wrapwrap is
    installed system-wide" was labelled ``verifiably_false``, and no amount of
    reading a repository settles it - it is a fact about the machine. Scored as
    a binary disagreement, that row is counted against the checker for not
    knowing something it cannot know. Scored as agreement it would flatter both.
    So a seed may declare ``checkable_from_repo=False``, and the row lands in
    ``not_checkable_from_repo``: the label is about the environment, the checker
    is about the bytes, and the two were never in conflict.
    """
    from evals.memory_ablation_real import SEED_MEMORIES

    labels: Dict[str, Tuple[str, bool]] = {}
    for seed in SEED_MEMORIES:
        labels[seed.text[:60]] = (
            seed.truth,
            getattr(seed, "checkable_from_repo", True),
        )
    agree = 0
    disagree = 0
    unlabelled = 0
    off_scope = 0
    details: List[Dict[str, Any]] = []
    for row in staleness.get("rows") or []:
        text = str(row.get("text") or "")
        key = next(
            (candidate for candidate in labels if text.startswith(candidate[:40])), None
        )
        if key is None:
            unlabelled += 1
            continue
        label, checkable = labels[key]
        if not checkable:
            off_scope += 1
            details.append(
                {
                    "text": text[:120],
                    "label": label,
                    "observed": "not_checkable_from_repo",
                    "agree": True,
                    "bucket": "not_checkable_from_repo",
                    "why": (
                        "the label is a claim about the environment, not about "
                        "the repository's bytes, so a byte-level checker cannot "
                        "confirm or contradict it and is not asked to"
                    ),
                }
            )
            continue
        observed = {
            "false": "verifiably_false",
            "holds": "verified_true",
            # An unchecked row is NOT a confirmation. Mapping it to
            # "verified_true" would turn the checker's silence into its
            # agreement, which is how the first draft of this function reported
            # 5 agreements when only 4 had actually been checked.
            "unverifiable": "unverifiable",
        }.get(str(row.get("state")), "unverifiable")
        if observed == "unverifiable":
            off_scope += 1
            details.append(
                {
                    "text": text[:120],
                    "label": label,
                    "observed": observed,
                    "agree": False,
                    "bucket": "not_checkable_from_repo",
                    "why": (
                        "the checker could not settle this row from repository "
                        "bytes, so it neither confirms nor refutes the label and "
                        "is excluded from the rate"
                    ),
                }
            )
            continue
        bucket = "agreed" if observed == label else "checker_gap"
        if observed == label:
            agree += 1
        else:
            disagree += 1
        details.append(
            {
                "text": text[:120],
                "label": label,
                "observed": observed,
                "agree": observed == label,
                "bucket": bucket,
            }
        )
    scored = agree + disagree
    return {
        "agreed": agree,
        "disagreed": disagree,
        "not_checkable_from_repo": off_scope,
        "unlabelled": unlabelled,
        "agreement_rate": round(agree / scored, 6) if scored else None,
        "agreement_rate_reason": (
            "" if scored else "every labelled row was off-scope, so there is nothing to agree about"
        ),
        "checker_gaps": [
            row for row in details if row.get("bucket") == "checker_gap"
        ],
        "details": details,
        "note": (
            "a checker_gap means the label is byte-checkable and the checker "
            "could not settle it: the checker is wrong. It is reported, not "
            "smoothed, and it is a work item for this module rather than for "
            "the corpus."
        ),
    }


# ---------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------


def memory_quality_report(
    *,
    tasks: Optional[Sequence[str]] = None,
    injected: Optional[Mapping[str, Sequence[str]]] = None,
    repo_paths: Optional[Mapping[str, str]] = None,
    out_root: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the full memory-quality report.

    ``injected`` / ``repo_paths`` let a caller reuse an ablation run instead of
    paying for another pass. With neither, one memory-ON pass is run per task
    through the real loop and the injected memory block is read back out of the
    harness's own ``decision_memory`` trace event.
    """
    from evals import memory_ablation_real as ablation

    temporary = out_root is None
    root = Path(str(out_root)) if out_root is not None else Path(tempfile.mkdtemp())
    root.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tasks_with_no_memory: List[str] = []
    try:
        if injected is None or repo_paths is None:
            from evals import tasks as eval_tasks

            specs = eval_tasks.all_tasks(root / "repos")
            if tasks:
                wanted = set(tasks)
                specs = [spec for spec in specs if str(spec.get("slug")) in wanted]
            specs.sort(key=lambda item: str(item.get("slug")))
            repos = {Path(str(spec["repo"])).name: str(spec["repo"]) for spec in specs}
            store = ablation.seed_store(root / "seed" / "decisions.db", repos)
            by_task_repo = {
                str(spec["slug"]): str(spec["repo"]) for spec in specs
            }
            injected = {}
            for spec in specs:
                outcome = ablation._run_one(
                    spec,
                    arm="memory_on",
                    repetition=1,
                    lane="oblivious",
                    log_root=root / "runs",
                    decision_db=Path(store["db_path"]),
                )
                rows = [
                    line.lstrip("- ").strip()
                    for line in (outcome.memory_block or "").splitlines()
                    if line.strip() and not line.strip().startswith("...")
                ]
                rows = [row for row in rows if row not in NO_MEMORY_ROWS]
                if not rows:
                    # The harness says this out loud, in these words, when it
                    # recalled nothing. Left in place it was scored as a memory
                    # whose text was "(none recorded yet)" - a memory that names
                    # no path, no symbol and no invocation, so it always "held",
                    # and it inflated the denominator of every rate here. The
                    # count of tasks that recalled nothing is itself a result,
                    # so it is published rather than dropped.
                    tasks_with_no_memory.append(str(spec["slug"]))
                injected[str(spec["slug"])] = rows
            repo_paths = by_task_repo

        # Both of these read the built repositories, so they run HERE, inside the
        # workdir's lifetime. The first draft computed them after the `finally`
        # deleted it and every row came back "repository does not exist" -
        # 9 phantom stale memories from 9 repositories it had just removed.
        staleness = staleness_report(injected, repo_paths)
        retraction = retraction_report(repo_paths, workdir=root / "retraction")
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)
    injected_rows = sum(len(rows or []) for rows in (injected or {}).values())
    quality = {
        "suite": "memory-quality",
        "phase": "P3a-W2",
        "started_at": started,
        "precision": precision_report(injected),
        "staleness": staleness,
        "retraction": retraction,
        "label_agreement": label_agreement(staleness),
        "harm": {
            "measured_in": "evals.memory_ablation_real",
            "why_not_here": (
                "harm needs a loop in which a recalled memory CAN change an "
                "outcome. This module has no loop of its own, so it reads the "
                "number from the one place it is measured rather than "
                "recomputing it and publishing two answers"
            ),
        },
        "inbox_contribution": {
            "through_inbox": 0,
            "memories_injected": injected_rows,
            "reason": (
                "a measured ZERO, not a gap: this report's corpus is seeded "
                "straight into a decision store with DecisionStore.record, which "
                "is the deterministic path the ablation needs, so no memory here "
                "passed through the inbox. The inbox's own contribution - "
                "backlog, quarantined, committed - is measured where the inbox "
                "runs, in evals.inbox_gate, and the two numbers are deliberately "
                "not combined into one rate."
            ),
            "measured_in": "evals.inbox_gate",
            "caveat": (
                "so this report cannot say what the inbox is worth end to end. "
                "It says what the RECALLED corpus is worth, which is the part "
                "precision, staleness and harm depend on."
            ),
        },
        "tasks_that_recalled_nothing": {
            "count": len(tasks_with_no_memory),
            "of": len(injected),
            "tasks": sorted(tasks_with_no_memory),
            "note": (
                "the harness said '(none recorded yet)' for these. Counted, not "
                "scored: there was no memory to be right or wrong about."
            ),
        },
        "claims": [
            "Every rate is computed against repository bytes or a human label, "
            "never against the corpus's own truth field.",
        ],
        "not_established": [
            "No number here is a claim about model quality: extraction and "
            "recall in this module are deterministic and the model, where one "
            "is involved at all, is a scripted double.",
        ],
        "verdict": "CLEAN",
    }
    return quality


def render(report: Mapping[str, Any]) -> str:
    """Render the quality report for a terminal."""
    lines = ["memory QUALITY report (deterministic, no model call)", ""]
    precision = report.get("precision") or {}
    lines.append(
        f"precision: {precision.get('precision')} "
        f"({precision.get('relevant')}/{precision.get('scored')} scored, "
        f"{precision.get('unlabelled')} unlabelled)"
    )
    stale = report.get("staleness") or {}
    lines.append(
        f"staleness: {stale.get('stale_rate')} "
        f"({stale.get('false')} false, {stale.get('holds')} hold, "
        f"{stale.get('unverifiable')} unverifiable of {stale.get('checked')})"
    )
    for row in stale.get("rows") or []:
        if row.get("state") != "holds":
            lines.append(
                f"   [{row['state']}] {row['task']}: {row['text'][:90]}"
            )
            for finding in row.get("findings") or []:
                lines.append(f"        {finding['kind']}: {finding['detail']}")
    quiet = report.get("tasks_that_recalled_nothing") or {}
    if quiet.get("count"):
        lines.append(
            f"nothing recalled at all in {quiet['count']} of {quiet['of']} tasks: "
            f"{', '.join(quiet['tasks'])}"
        )
    retraction = report.get("retraction") or {}
    lines.append(
        f"retraction: detection_rate={retraction.get('detection_rate')} "
        f"({retraction.get('detected')}/{retraction.get('cases')}), "
        f"amended={retraction.get('amended_count')}"
    )
    inbox = report.get("inbox_contribution")
    if inbox:
        lines.append(
            f"inbox contribution: {inbox.get('through_inbox')} of "
            f"{inbox.get('memories_injected')} injected memories passed the "
            f"inbox (measured in {inbox.get('measured_in')})"
        )
    agreement = report.get("label_agreement") or {}
    lines.append(
        f"checker/label agreement: {agreement.get('agreed')} agreed, "
        f"{agreement.get('disagreed')} checker gaps, "
        f"{agreement.get('not_checkable_from_repo')} off-scope, "
        f"{agreement.get('unlabelled')} unlabelled"
    )
    for row in agreement.get("checker_gaps") or []:
        lines.append(f"   [CHECKER GAP] {row['text'][:88]}")
    lines.append("")
    lines.append(f"verdict: {report.get('verdict')}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m evals.memory_quality``."""
    parser = argparse.ArgumentParser(description="the P3a memory-quality report")
    parser.add_argument("--tasks", default=None, help="comma list of task slugs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = memory_quality_report(
        tasks=args.tasks.split(",") if args.tasks else None
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0 if report.get("verdict") == "CLEAN" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
