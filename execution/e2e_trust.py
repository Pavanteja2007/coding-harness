"""P4/W2 T2.W2.1 — can an end-to-end check be FOOLED? Ten attacks, and a fifth state.

Wave 1 built end-to-end verification: the capability that makes a feature ledger
mean anything. **An e2e check that can be fooled is worse than no e2e check**,
because it converts an honest gap into a false pass — the exact failure this
project exists to prevent.

This module is the answer to the one question Wave 1 could not ask, because it
had no name for the answer: *is this check capable of deciding the question it
was declared to decide?*

It asks it three ways, each corresponding to a group of rows in
:mod:`execution.e2e_attack`'s table:

===========================  ==========================================
OWNERSHIP (rows 1, 2, 3)    is this check **the repository's**, or did
                            the agent it verifies author it, rewrite it,
                            or neutralise it?
STRENGTH (row 4)            a check that cannot fail, or whose failure
                            would say nothing, is not a passing check —
                            it is an **untrustworthy** one.
TIMING (row 6)             a check that finishes in less time than the
                            boundary that ran it costs did not run.
===========================  ==========================================

--------------------------------------------------------------------------
THE FIFTH STATE, AND WHY IT IS NOT "FAILED"
--------------------------------------------------------------------------

``execution/e2e_verify.py``'s :data:`~execution.e2e_verify.E2E_STATES` is a
closed FIVE-value vocabulary. Four of them describe a *measurement*; the fifth
describes the ABSENCE of a usable one:

============  =============================================================
``passed``    ran, and the feature behaved
``failed``    ran, and the feature did not
``blocked``   could not run, with a mandatory reason
``not_run``   was not attempted
``untrustworthy``  **this module's contribution.** ran (or could have), and
              the verdict is void in BOTH directions
============  =============================================================

The argument for the fifth state is one sentence: **the check ran, exited 0,
and asserts nothing.** That is not ``passed``. It is also not ``failed``,
because nothing was shown to be broken and a ledger that renders it ``failed``
sends a repair loop after a working tree. It is a third fact, and a report
containing one green row and one void row must not read ``passed`` — which is
why ``untrustworthy`` **dominates** :attr:`E2EReport.verdict`.

Every ``untrustworthy`` row REQUIRES a reason and ``E2EResult`` REFUSES a trust
receipt on any other state. Both rules are refusals in the permissive direction,
and the second one matters more than it looks: a trust field that appears on a
green row teaches every reader to skip it.

--------------------------------------------------------------------------
A WEAK CHECK IS NOT A FAILING CHECK
--------------------------------------------------------------------------

:func:`assess_strength` publishes *why* it concluded, in a closed vocabulary of
weak signals (:data:`WEAK_SIGNALS`) and a closed vocabulary of strong ones
(:data:`STRONG_SIGNALS`). Two rules keep it from being a rubber stamp:

1. **No weak signal is compensable.** Nothing makes ``true`` into a check, and
   nothing makes ``pytest -q tests`` into an end-to-end check — it is
   :mod:`execution.verify`'s question, and
   :func:`execution.e2e_verify.neither_substitutes` already says the two
   questions are different. A design with a "compensating strong signal" escape
   hatch would have exactly one entry, ``compensates_everything``.
2. **Zero weak AND zero strong is ``unmeasurable``, not ``strong``.** "We found
   nothing wrong" is a finding only if something was looked at. This is the
   non-vacuity control, and it is what stops the assessment from being a
   tautology that approves every command it has never seen.

--------------------------------------------------------------------------
SUSPICIOUSLY FAST, MEASURED RATHER THAN GUESSED
--------------------------------------------------------------------------

A floor that is a round number chosen for looking reasonable is not a
measurement. :data:`FLOOR_TABLE` carries three floors, each the **measured
minimum** on this host minus a **named margin**, with the raw samples:

=========================  =========  ==============================
floor                       value      measured minimum on this host
=========================  =========  ==============================
:data:`CONTAINER_MIN_PLAUSIBLE_S`   **1.00 s**  **1.4382 s** (5 samples,
``true`` through the real boundary)
:data:`SERVER_MIN_PLAUSIBLE_S`      **1.25 s**  **1.7245 s** (5 samples, a
real bind + listen + readiness wait)
:data:`RUNNER_MIN_PLAUSIBLE_S`      **2.00 s**  **3.8333 s** (3 samples, a
real ``pytest -q`` collection)
=========================  =========  ==============================

F1 is the load-bearing one: it is the cost of ``docker run`` **itself**, so no
check that ran through the boundary can finish faster than the boundary. A
stubbed check that returns without spawning anything costs ~0.01 s — a
hundredth of the floor — which is what makes the detection a measurement rather
than a taste.

The ratio rule (:data:`FAST_PASS_RATIO`) is host-independent and is the second
arm: if this content digest's check took 3.2 s last time and 0.01 s now, that is
suspicious on any machine.

And the **structural** arm needs no timing at all: a result claiming
``ran=True`` with no ``container_name`` never crossed the boundary, whatever
the clock says.

--------------------------------------------------------------------------
A REUSED RESULT NAMES WHAT IT WAS REUSED FROM
--------------------------------------------------------------------------

:func:`reuse_of` produces the mark a cached row must carry, and
:func:`execution.e2e_evidence.assert_same_commit` is the structural refusal
that makes a result from a **different commit** unusable as evidence. A
measurement of commit A cannot speak about commit B, and the receipt says which
commit it measured.
"""

from __future__ import annotations

import re
import shlex
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from execution.e2e_verify import (
    E2E_UNTRUSTWORTHY,
    E2ECheck,
    E2EResult,
    untrustworthy_result,
)

__all__ = [
    "CONTAINER_MIN_PLAUSIBLE_S",
    "FAST_PASS_RATIO",
    "FLOOR_MARGIN_NOTE",
    "FLOOR_TABLE",
    "NO_CONTAINER_REASON",
    "RUNNER_MIN_PLAUSIBLE_S",
    "SERVER_MIN_PLAUSIBLE_S",
    "SIGNAL_DESCRIPTIONS",
    "STRENGTH_STRONG",
    "STRENGTH_UNMEASURABLE",
    "STRENGTH_VALUES",
    "STRENGTH_WEAK",
    "STRONG_SIGNALS",
    "TRUST_STATES",
    "TRUST_TRUSTED",
    "TRUST_UNTRUSTWORTHY",
    "UNTRUSTWORTHY_SIGNALS",
    "WEAK_SIGNALS",
    "CommitAnchor",
    "FastPassVerdict",
    "OwnershipVerdict",
    "StrengthVerdict",
    "TrustVerdict",
    "assert_five_states_distinct",
    "assess_check",
    "assess_fast_pass",
    "assess_ownership",
    "assess_strength",
    "command_shape",
    "floor_for",
    "gate_e2e_result",
    "guard_e2e_check",
    "read_commit",
    "reuse_of",
    "self_neutralised_spans",
    "signals_of",
    "unit_runner_command",
]


# ==========================================================================
# The floors — measured, with their bases committed in source
# ==========================================================================

#: The margin between a measured minimum and the published floor. Stated so a
#: reader can compute the floor rather than trusting it, and so a faster host
#: does not produce false positives: the floor is the measured minimum LESS this
#: fraction, not a round number.
FLOOR_MARGIN_NOTE = (
    "each published floor is this host's MEASURED MINIMUM multiplied by 0.70, "
    "so a machine faster than this one cannot make a legitimate check look "
    "suspicious. Re-measure with `python -m execution.e2e_attack --floors` "
    "before quoting these against another host."
)

CONTAINER_MIN_PLAUSIBLE_S = 1.00
SERVER_MIN_PLAUSIBLE_S = 1.25
RUNNER_MIN_PLAUSIBLE_S = 2.00

#: A run below this fraction of its own prior median is suspicious regardless
#: of host speed. This is the host-independent arm of the timing check: a check
#: that took 3.2 s for this content digest and 0.01 s now did not run the same
#: thing.
FAST_PASS_RATIO = 0.05

FLOOR_TABLE: Dict[str, Dict[str, Any]] = {
    "any_sandboxed_command": {
        "floor_s": CONTAINER_MIN_PLAUSIBLE_S,
        "measured_min_s": 1.4382,
        "measured_median_s": 1.948,
        "samples": 5,
        "measured": "MEASURED 2026-10-06 on this host, real Docker 28.5.1, warm image",
        "basis": (
            "the cheapest possible sandboxed command (`true`) through "
            "execution.sandbox.execute_sandboxed. This is the cost of `docker "
            "run` ITSELF, so no check that ran through the boundary can finish "
            "faster than the boundary that ran it"
        ),
        "samples_s": [1.948, 1.5127, 1.4382, 2.0239, 2.0756],
    },
    "server_starting_command": {
        "floor_s": SERVER_MIN_PLAUSIBLE_S,
        "measured_min_s": 1.7245,
        "measured_median_s": 2.2053,
        "samples": 5,
        "measured": "MEASURED 2026-10-06 on this host, real Docker 28.5.1, warm image",
        "basis": (
            "a real bind + listen + readiness wait inside the container. A "
            "check declaring `publish_port` or `server_start` is below this row "
            "because the port never bound"
        ),
        "samples_s": [2.3095, 1.8955, 1.7245, 2.7269, 2.2053],
    },
    "real_unit_runner": {
        "floor_s": RUNNER_MIN_PLAUSIBLE_S,
        "measured_min_s": 3.8333,
        "measured_median_s": 4.2564,
        "samples": 3,
        "measured": "MEASURED 2026-10-06 on this host, real Docker 28.5.1, warm image",
        "basis": (
            "a real `python -m pytest -q` collection over a 1-test fixture. "
            "Wider margin than the other two rows because collection time is far "
            "more variable than `docker run`"
        ),
        "samples_s": [3.8333, 4.4237, 4.2564],
    },
}

NO_CONTAINER_REASON = (
    "the result claims it ran but carries no container name, so no container "
    "was ever created: the boundary was stubbed. A stub that answers 'passed' "
    "without a container is the shape this state exists for, and it is "
    "detectable with no timing at all"
)


def floor_for(check: E2ECheck) -> Tuple[str, Optional[float]]:
    """Which floor applies to this check, and its value. ``None`` = no floor.

    The ORDER matters and is the policy: a server check pays for a container AND
    a bind, so it takes the higher floor; a runner check pays for a container,
    a bind AND a collection. A check with neither declared and no runner in its
    command gets the container floor, which is the floor for having run at all.
    """
    if check.has_server or check.publish_port is not None:
        return "server_starting_command", SERVER_MIN_PLAUSIBLE_S
    if _RUNNER_TOKENS.search(check.command or ""):
        return "real_unit_runner", RUNNER_MIN_PLAUSIBLE_S
    return "any_sandboxed_command", CONTAINER_MIN_PLAUSIBLE_S


# ==========================================================================
# The signal vocabulary — closed in both directions
# ==========================================================================

#: Signals that make a check UNTRUSTWORTHY. A closed tuple so a reader can
#: enumerate every way this module can refuse, and so a new signal has to be
#: added deliberately.
WEAK_SIGNALS: Tuple[str, ...] = (
    "command_is_inert",
    "command_self_neutralised",
    "command_is_the_unit_runner",
    "command_skips_its_own_cases",
    "command_forges_its_own_output",
    "command_asserts_nothing",
    "command_runs_a_test_path",
    "command_exercises_nothing",
    "no_feature_named",
    "output_carries_no_result",
    "referenced_path_absent",
    "declaration_digest_moved",
)

#: Signals that a check DOES carry evidence. Published so ``strong`` is not
#: merely "we found nothing wrong", which is a tautology over an unseen command.
STRONG_SIGNALS: Tuple[str, ...] = (
    "invokes_a_runner",
    "output_carries_a_result_summary",
    "names_an_existing_repo_path",
    "reaches_an_external_surface",
    "declares_a_feature",
    "can_fail",
    "boundary_was_crossed",
    "declaration_digest_vouched",
)

#: The union, which is what :func:`assess_check` publishes as ``signals``.
UNTRUSTWORTHY_SIGNALS: Tuple[str, ...] = WEAK_SIGNALS

#: Trust itself has two states and no third. A check is either capable of
#: deciding its question or it is not; "partially trustworthy" is not an answer
#: anybody can act on.
TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTWORTHY = "untrustworthy"
TRUST_STATES: Tuple[str, str] = (TRUST_TRUSTED, TRUST_UNTRUSTWORTHY)

#: Published so a surface can render a signal without re-deriving the argument.
SIGNAL_DESCRIPTIONS: Dict[str, str] = {
    "command_is_inert": (
        "the declared command cannot fail: its exit code is 0 whatever the "
        "feature does, so a green row carries no information about the feature"
    ),
    "command_self_neutralised": (
        "the command forces its own success (`|| true`, `; exit 0`, "
        "`&& true`), which is the shell-level equivalent of a skip marker"
    ),
    "command_is_the_unit_runner": (
        "the command runs the unit suite. That is execution/verify.py's "
        "question, not an end-to-end one, and neither_substitutes() already "
        "refuses the substitution in both directions"
    ),
    "command_skips_its_own_cases": (
        "the command deselects or ignores cases (`-k`, `--deselect`, `-m 'not "
        "...'`, `--ignore`), so it cannot observe the failure it would report"
    ),
    "command_forges_its_own_output": (
        "the command PRINTS a runner's result line instead of producing one "
        "(`echo ... passed ...`). This check can report '1 passed' with nothing "
        "having run at all, which is the most direct forgery available"
    ),
    "command_exercises_nothing": (
        "the command names no runner and nothing in the repository, so it has "
        "no subject: an end-to-end check that touches no feature and no code "
        "cannot be an end-to-end check. This is attack row 4 — the weak check "
        "that passes while the feature is broken — detected structurally rather "
        "than by reading the command and judging it"
    ),
    "command_runs_a_test_path": (
        "the command runs a TEST FILE as its end-to-end check. Two facts make "
        "that the wrong answer rather than a merely weak one: a unit test is "
        "execution/verify.py's question (neither_substitutes refuses the "
        "substitution in both directions), and a test file is a test the agent "
        "can write — so the check is not the repository's own. Row 1"
    ),
    "command_asserts_nothing": (
        "the command's referenced source file(s) contain NO assertion at all. "
        "This is row 4's hard case: a script that names an existing file, names "
        "a feature, and prints a passing result line while containing nothing "
        "that could fail. It looks exactly like a real check, and the only "
        "mechanical difference is that one of them asserts"
    ),
    "reaches_an_external_surface": (
        "the command reaches an external surface (a published host/port, an "
        "HTTP URL, or a client for one), which is what makes a check end-to-end "
        "rather than end-of-a-file"
    ),
    "no_feature_named": (
        "the check names no feature, so nothing ties its verdict to a behaviour "
        "a user touches"
    ),
    "output_carries_no_result": (
        "the run produced no output, so there is no result line for a reader to "
        "read and nothing that distinguishes 'worked' from 'did nothing'"
    ),
    "referenced_path_absent": (
        "the command names a repository path that does not exist, so it cannot "
        "have exercised the thing it names"
    ),
    "declaration_digest_moved": (
        "the declaration file changed since it was vouched for. An agent that "
        "edits the check it is verified by has made the check its own"
    ),
    "invokes_a_runner": (
        "the command names a known test runner (pytest, jest, vitest, go test, "
        "cargo test, and the rest of the published token set), so the verdict "
        "rests on a runner's own exit code rather than on the command's"
    ),
    "output_carries_a_result_summary": (
        "the output carries a runner's own summary line, so the verdict rests "
        "on a structured result rather than on an exit code alone"
    ),
    "names_an_existing_repo_path": (
        "the command names a path that exists in the repository under test"
    ),
    "declares_a_feature": "the declaration names the feature this check speaks for",
    "can_fail": (
        "nothing in the command forces success, so a non-zero exit is "
        "reachable and the check can actually report a failure"
    ),
    "boundary_was_crossed": (
        "a container name was observed, so the run really went through "
        "execution.sandbox.execute_sandboxed"
    ),
    "declaration_digest_vouched": (
        "the declaration's bytes match the digest the caller vouched for, so "
        "this check is the repository's own"
    ),
}

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"
STRENGTH_UNMEASURABLE = "unmeasurable"
STRENGTH_VALUES: Tuple[str, str, str] = (
    STRENGTH_STRONG,
    STRENGTH_WEAK,
    STRENGTH_UNMEASURABLE,
)


# ==========================================================================
# Command classification — delegated to this package's own authorities
# ==========================================================================

#: Tokens that mean "a real test runner is being invoked". Deliberately a
#: substring scan over the command rather than a parse: the question is "did
#: somebody name a runner", and a command that mentions ``pytest`` is claiming
#: runner evidence whether or not it parses.
_RUNNER_TOKENS = re.compile(
    r"\b(pytest|py\.test|jest|vitest|mocha|go\s+test|cargo\s+test|mvn\s+test|"
    r"gradle\s+test|dotnet\s+test|bundle\s+exec\s+r?spec|rspec|phpunit|tox|nox|"
    r"npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|unittest|node\s+--test)\b"
)

#: A command whose ONLY job is to exit zero. Matched after the shell metachar
#: acters are stripped, so ``true && real-thing`` is not inert.
_INERT_TOKENS = frozenset(
    {
        "true",
        ":",
        "exit 0",
        "exit+0",
        "/bin/true",
        "echo",
        "printf",
        "pass",
        "noop",
        "nop",
        "yes",
    }
)

#: Shell shapes that force the exit code to success. ``&& true`` is included
#: because it is the mirror image of ``|| true`` and just as common.
#:
#: These are matched against the WHOLE command, never against a split segment.
#: That is not a detail: splitting on ``||`` first would remove the ``|| true``
#: from the segment it lives in, and the detector would then report every
#: stubbed check as an honest one. The segment split exists to decide whether
#: every segment is inert; the neutraliser scan needs the original text.
_NEUTRALISERS: Tuple[Tuple[str, str], ...] = (
    (r"\|\|\s*(?:/bin/)?true\b", "|| true"),
    (r"\|\|\s*(?:/bin/)?echo\b", "|| echo"),
    (r"\|\|\s*exit\s+0\b", "|| exit 0"),
    (r";\s*exit\s+0\b", "; exit 0"),
    (r"&&\s*(?:/bin/)?true\b", "&& true"),
    (r"&&\s*exit\s+0\b", "&& exit 0"),
    (r"^\s*:\s*$", ": (a no-op)"),
)

#: Flags that remove cases from a runner's own denominator.
_SKIP_TOKENS: Tuple[Tuple[str, str], ...] = (
    (r"(?:^|\s)-k\b", "-k (an expression filter)"),
    (r"(?:^|\s)--deselect\b", "--deselect"),
    (r"(?:^|\s)--ignore\b", "--ignore"),
    (r"(?:^|\s)-m\s+[\"']?not\b", "-m 'not ...' (a marker filter)"),
    (r"(?:^|\s)--collect-only\b", "--collect-only (collects, never runs)"),
    (r"(?:^|\s)--co\b", "--co (collects, never runs)"),
)

#: Runner summary lines. A result carrier, not a claim: the shape is what makes
#: a verdict readable rather than an exit code.
_SUMMARY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\b\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed)\b", "pytest summary"),
    (r"^=+\s*(?:FAILURES|ERRORS|short test summary)", "pytest section header"),
    (r"^\s*(?:ok|FAIL|ERROR)\s+\S+", "tAP line"),
    (r"Tests:\s+\d+", "jest/vitest summary"),
    (r"^Test Suites?:.*\b(?:passed|failed)\b", "jest suite summary"),
    (r"^\s*PASS\b", "go test PASS line"),
    (r"^\s*FAIL\b", "go test FAIL line"),
    (r"^test result:", "cargo test summary"),
    (r"^OK \(", "go/cargo OK line"),
    (r"BUILD SUCCESS", "gradle/maven success"),
    (r"\d+ examples?, \d+ failures?", "rspec summary"),
)

_COMPILED_NEUTRALISERS: Tuple[Tuple[Any, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _NEUTRALISERS
)
_COMPILED_SKIPS: Tuple[Tuple[Any, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _SKIP_TOKENS
)
_COMPILED_SUMMARY: Tuple[Tuple[Any, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE | re.MULTILINE), name)
    for pattern, name in _SUMMARY_PATTERNS
)

#: ``echo``/``printf`` alongside a runner-summary token. Printing a result is
#: not producing one, and this is the shape that forges a green row without a
#: sandbox, a runner, or a single line of the feature's code.
_FORGES_OUTPUT = re.compile(
    r"\b(?:echo|printf)\b[^\n]*"
    r"(?:\d+\s+(?:passed|failed|errors?)|tests?:|PASS\b|FAIL\b|ok\s+\d+)",
    re.IGNORECASE,
)

#: Ways a command reaches something OUTSIDE the repository under test. This is
#: what makes a check end-to-end: it crosses a boundary (a socket, a port, a
#: published URL) rather than reading a file.
#:
#: It exists because of a REAL false positive. Without it, the perfectly
#: legitimate ``curl -sS {host}:{port}/health`` — a real end-to-end check
#: against a real published port — named nothing in the repository and no
#: runner, so ``command_exercises_nothing`` refused it. A weak signal that fires
#: on working code is a weak signal that gets deleted rather than satisfied, and
#: it hides the signals that matter behind its own noise.
_REACHES_EXTERNAL = re.compile(
    r"\{(?:host|port|base_url|url)\}"
    r"|https?://"
    r"|\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bhttpie?\b"
    r"|\burllib\b|\brequests\.[a-z]|\bsocket\.\w|\bhttp\.client\b",
    re.IGNORECASE,
)

#: Source/test suffixes used to decide whether a token names a repository file.
_PATH_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".rs",
    ".java",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".html",
    ".txt",
    ".md",
)

#: Tokens that are interpreters or tools, never repository paths.
_NOT_A_PATH = frozenset(
    {
        "python",
        "python3",
        "node",
        "npm",
        "yarn",
        "pnpm",
        "go",
        "cargo",
        "bash",
        "sh",
        "-c",
        "&&",
        "||",
        ";",
        "|",
        ">",
        ">>",
        "<",
        "env",
        "echo",
        "true",
        "cd",
        "test",
    }
)


def unit_runner_command(repo_path: str) -> str:
    """The repository's own unit-suite command, from the package's authority.

    Delegates to :func:`execution.verify._autodetect_test_command` — the same
    intra-family private seam ``execution/verification_gate.py`` already uses —
    so "is this command the unit suite?" is answered by ONE function rather
    than by a second token table that can drift from the real command.
    """
    try:
        from execution import verify as ver

        detected = ver._autodetect_test_command(str(repo_path))
    except Exception:
        detected = None
    return str(detected or "")


def _tokens(command: str) -> Tuple[str, ...]:
    text = str(command or "")
    try:
        return tuple(shlex.split(text, posix=False))
    except ValueError:
        # An unbalanced quote is itself worth seeing, so fall back to a split
        # rather than discarding the command.
        return tuple(text.split())


def _significant_segments(command: str) -> Tuple[str, ...]:
    """The command split on shell separators, so a segment is judged alone."""
    parts = re.split(r"&&|\|\||;|\||\n", str(command or ""))
    return tuple(part.strip() for part in parts if part.strip())


def self_neutralised_spans(command: str) -> Tuple[str, ...]:
    """Every shell span in ``command`` that forces its own success.

    Scanned over the WHOLE command text, never per split segment — see the note
    on :data:`_NEUTRALISERS`. An earlier version split on ``||`` first and then
    scanned each piece, which meant ``... || true`` was split into two
    innocent-looking segments and the detector reported every stubbed check as
    an honest one. The signature is unchanged; the scanned text is not.
    """
    text = str(command or "")
    if not text.strip():
        return ()
    found: List[str] = []
    for pattern, name in _COMPILED_NEUTRALISERS:
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            found.append(f"{name} at offset {match.start()} (line {line})")
    return tuple(dict.fromkeys(found))


def _skip_spans(command: str) -> Tuple[str, ...]:
    found: List[str] = []
    for pattern, name in _COMPILED_SKIPS:
        if pattern.search(str(command or "")):
            found.append(name)
    return tuple(dict.fromkeys(found))


def command_is_inert(command: str) -> bool:
    """Whether every segment of the command is a no-op.

    ALL segments, not any. ``true && pytest -q tests`` is not inert, and a check
    that only looked at the first token would call it inert — which would be a
    refusal that fires on working code, the kind that gets disabled.
    """
    segments = _significant_segments(command)
    if not segments:
        return True
    for segment in segments:
        tokens = [tok.strip("\"'`()[]{}") for tok in _tokens(segment)]
        tokens = [tok for tok in tokens if tok]
        if not tokens:
            return True
        if not all(tok.lower() in _INERT_TOKENS for tok in tokens):
            return False
    return True


def command_shape(
    command: str,
    *,
    repo_path: str = "",
) -> Tuple[str, Tuple[str, ...]]:
    """Classify a declared command into a CLOSED shape vocabulary.

    ``inert``        every segment is a no-op
    ``unit_runner``  it runs the repository's unit suite
    ``skips``        it deselects or ignores cases
    ``runner``       it names a test runner
    ``probe``        anything else — the only shape that can carry evidence
    ``empty``        no command at all

    The order is the policy and it is what makes row 1 answerable: an agent that
    writes a test asserting nothing and points the e2e check at it produces
    ``unit_runner``, which is refused as an end-to-end check because
    :func:`execution.e2e_verify.neither_substitutes` already refuses the
    substitution in the other direction too.
    """
    text = str(command or "").strip()
    if not text:
        return "empty", ("the declaration carries no command",)
    notes: List[str] = []
    if command_is_inert(text):
        return "inert", ("every segment of the command is a no-op",)
    neutralised = self_neutralised_spans(text)
    if neutralised:
        notes.extend(neutralised)
    skips = _skip_spans(text)
    if skips:
        return "skips", tuple(f"deselects cases: {', '.join(skips)}" for s in skips)
    unit = unit_runner_command(repo_path) if repo_path else ""
    if unit and text.strip() == unit.strip():
        return "unit_runner", (f"this is the repository's own unit-suite command ({unit!r})",)
    if _RUNNER_TOKENS.search(text):
        return "runner", (f"invokes a runner: {_RUNNER_TOKENS.search(text).group(0)!r}",)
    return "probe", tuple(notes)


def _repo_relative_paths(command: str) -> Tuple[str, ...]:
    """Repository-relative path tokens named by the command.

    Conservative in the direction that matters: an unrecognised token is
    dropped rather than guessed at, because a wrong "this path is absent" fires
    a refusal on a working check.
    """
    found: List[str] = []
    for token in _tokens(command):
        cleaned = token.strip("\"'`(),;")
        if not cleaned or cleaned.startswith("-") or cleaned in _NOT_A_PATH:
            continue
        if cleaned.startswith(("/", "~", "$", "{", "<", "=")) or "://" in cleaned:
            continue
        if ":" in cleaned and not re.match(r"^[A-Za-z0-9_./-]+$", cleaned):
            continue
        if cleaned.startswith("-"):
            continue
        has_suffix = cleaned.lower().endswith(_PATH_SUFFIXES)
        looks_pathed = "/" in cleaned or has_suffix
        if not looks_pathed:
            continue
        cleaned = cleaned.replace("\\", "/").lstrip("./")
        if not cleaned or cleaned in _NOT_A_PATH:
            continue
        found.append(cleaned)
    return tuple(dict.fromkeys(found))


def _result_summary_carriers(output: str) -> Tuple[str, ...]:
    """Which runner-summary shapes the output carries, if any."""
    text = str(output or "")
    if not text.strip():
        return ()
    return tuple(
        name for pattern, name in _COMPILED_SUMMARY if pattern.search(text)
    )


def _has_subject(
    command: str,
    *,
    shape: str,
    referenced: Sequence[str],
    test_paths: Sequence[str],
) -> Tuple[bool, str]:
    """Whether a command has a SUBJECT: a runner, a test file, repository code,
    or an external surface to reach. **One decision, one function.**

    It is factored out because the ownership verdict and the strength verdict
    both need it and they must not be able to disagree. The first version
    computed it twice: the strength half learned about external surfaces and
    the ownership half did not, so a legitimate ``curl {host}:{port}/health``
    check was reported ``strong`` by one assessment and ``command_exercises_
    nothing`` by the other. Two answers to one question is the defect this
    repository has now paid for several times over — ``verify_cache``'s second
    skip table, ``test_guard`'s second path matcher, ``verification_evidence``'s
    second ``not_run`` — so the rule here is structural rather than careful.
    """
    text = str(command or "")
    if shape in ("inert", "empty", "unit_runner", "skips"):
        # These are already decisive signals in their own right; adding "and it
        # has no subject" to every one of them would bury the specific finding
        # under a generic one.
        return True, ""
    if _RUNNER_TOKENS.search(text):
        return True, ""
    if tuple(test_paths):
        return True, ""
    if tuple(referenced):
        return True, ""
    if _REACHES_EXTERNAL.search(text):
        return True, ""
    return False, (
        "the command names no runner, no test file, nothing in the repository "
        "under test and no external surface, so the check has no subject: there "
        "is no feature and no code for it to exercise, whatever exit code it "
        "reports"
    )


def _has_subject_signal(
    command: str,
    *,
    shape: str,
    referenced: Sequence[str],
    test_paths: Sequence[str],
) -> Optional[str]:
    """``"command_exercises_nothing"`` or ``None``. Same decision, as a signal."""
    has, _reason = _has_subject(
        command, shape=shape, referenced=referenced, test_paths=test_paths
    )
    return None if has else "command_exercises_nothing"


def assertions_in(path: Path) -> int:
    """How many assertions a repository source file actually contains.

    An ``assert`` statement, a raise of ``AssertionError``, or an explicit
    non-zero exit all count. Deliberately **syntactic and shallow**: the question
    is not "is this assertion correct" but "is there anything here that could
    fail", and a count answers that without evaluating the program.

    **The limit is stated rather than hidden.** An assertion that is itself
    wrong — ``assert True``, or an ``assert`` guarded by a condition the feature
    never satisfies — returns a count of 1 and is not caught by this function.
    Detecting that needs the check's own coverage, which is a different
    mechanism (and :func:`assess_fast_pass`'s ratio arm catches the *shape* of
    it when the run suddenly becomes instant).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    count = len(re.findall(r"^\s*assert\b", text, re.MULTILINE))
    count += len(re.findall(r"\braise\s+AssertionError\b", text))
    count += len(re.findall(r"\bsys\.exit\((?!0\)|\s*0\s*\))", text))
    count += len(re.findall(r"\bshutil\.which\([^)]*\)\s*is\s+None\b", text))
    return count


def assertion_count_for(command: str, *, repo_path: str = "") -> Tuple[int, Tuple[str, ...]]:
    """Total assertions across the existing SOURCE files a command names.

    Returns ``(count, files)``. ``files`` is empty when nothing readable was
    found — which is the ``unmeasurable`` case and never zero-is-a-pass: the
    caller checks ``files`` before concluding anything.
    """
    root = Path(str(repo_path or "."))
    scanned: List[str] = []
    total = 0
    for relative in _repo_relative_paths(command):
        if not relative.lower().endswith((".py",)):
            continue
        candidate = root / relative
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        scanned.append(relative)
        total += assertions_in(candidate)
    return total, tuple(scanned)


def referenced_paths(command: str) -> Tuple[str, ...]:
    """The public name for :func:`_repo_relative_paths`.

    Exposed because two assessments need it and a private cross-function call
    inside one module is the shape that later grows a second implementation.
    """
    return _repo_relative_paths(command)


def test_paths_in(command: str, *, repo_path: str = "") -> Tuple[str, ...]:
    """Which referenced paths the package's own authority calls a TEST path.

    Delegates to :func:`execution.test_guard.test_path_matcher`, which is
    itself a delegation to ``execution.ecosystems.is_test_path``. Two hops, one
    answer: this module does not own a path matcher, because a second one in
    ``execution/`` is a second answer to "is this a test" — and attack row 1 is
    entirely about that question.

    The lookup is done ONCE per command rather than per path, because the
    matcher probes the ecosystem registry and doing that per token is how a
    cheap check becomes a slow one.
    """
    referenced = _repo_relative_paths(command)
    if not referenced:
        return ()
    try:
        from execution.test_guard import test_path_matcher

        matcher = test_path_matcher()
    except Exception:  # pragma: no cover - defensive
        return ()
    hits: List[str] = []
    for relative in referenced:
        try:
            if matcher(relative):
                hits.append(relative)
        except Exception:  # pragma: no cover - a hostile row must not crash
            continue
    return tuple(hits)


# ==========================================================================
# The three verdicts
# ==========================================================================


@dataclass(frozen=True)
class OwnershipVerdict:
    """Is this check the repository's, or did the agent it verifies write it?"""

    trusted: bool
    shape: str
    signals: Tuple[str, ...]
    reasons: Tuple[str, ...]
    declaration_digest: str = ""
    vouched_digest: str = ""
    agent_authored: Optional[bool] = None

    @property
    def may_report_passed(self) -> bool:
        return bool(self.trusted)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trusted": bool(self.trusted),
            "shape": self.shape,
            "signals": list(self.signals),
            "reasons": list(self.reasons),
            "declaration_digest": self.declaration_digest,
            "vouched_digest": self.vouched_digest,
            "agent_authored": self.agent_authored,
            "may_report_passed": bool(self.trusted),
        }


@dataclass(frozen=True)
class StrengthVerdict:
    """Whether the check is strong enough to decide its own question."""

    strength: str
    can_fail: bool
    weak_signals: Tuple[str, ...]
    strong_signals: Tuple[str, ...]
    reasons: Tuple[str, ...]
    shape: str = ""

    @property
    def may_report_passed(self) -> bool:
        """Only a ``strong`` check may report a pass.

        ``unmeasurable`` is NOT a soft ``weak``. It means nothing was looked
        at, and treating "we could not assess this" as "it is fine" is the exact
        inversion this whole module exists to prevent.
        """
        return self.strength == STRENGTH_STRONG

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strength": self.strength,
            "can_fail": bool(self.can_fail),
            "weak_signals": list(self.weak_signals),
            "strong_signals": list(self.strong_signals),
            "reasons": list(self.reasons),
            "shape": self.shape,
            "may_report_passed": bool(self.may_report_passed),
            "signal_descriptions": {
                name: SIGNAL_DESCRIPTIONS[name]
                for name in tuple(self.weak_signals) + tuple(self.strong_signals)
                if name in SIGNAL_DESCRIPTIONS
            },
        }


@dataclass(frozen=True)
class FastPassVerdict:
    """Whether the run finished faster than the boundary that ran it."""

    suspicious: bool
    duration_s: float
    floor_name: str
    floor_s: Optional[float]
    reasons: Tuple[str, ...] = ()
    ratio_to_prior: Optional[float] = None
    prior_median_s: Optional[float] = None
    observed: bool = True

    @property
    def may_report_passed(self) -> bool:
        return not bool(self.suspicious)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suspicious": bool(self.suspicious),
            "duration_s": round(float(self.duration_s), 4),
            "floor_name": self.floor_name,
            "floor_s": self.floor_s,
            "reasons": list(self.reasons),
            "ratio_to_prior": (
                None if self.ratio_to_prior is None else round(float(self.ratio_to_prior), 6)
            ),
            "prior_median_s": (
                None if self.prior_median_s is None else round(float(self.prior_median_s), 4)
            ),
            "observed": bool(self.observed),
            "fast_pass_ratio_bar": FAST_PASS_RATIO,
            "may_report_passed": bool(self.may_report_passed),
        }


@dataclass(frozen=True)
class TrustVerdict:
    """The three verdicts folded into one answer, with everything published."""

    state: str
    may_report_passed: bool
    reasons: Tuple[str, ...]
    signals: Tuple[str, ...]
    ownership: OwnershipVerdict
    strength: StrengthVerdict
    fast_pass: FastPassVerdict

    def __post_init__(self) -> None:
        if self.state not in TRUST_STATES:
            raise ValueError(
                f"unknown trust state {self.state!r}; the vocabulary is closed: "
                f"{TRUST_STATES}"
            )
        if bool(self.may_report_passed) != (self.state == TRUST_TRUSTED):
            raise ValueError(
                "may_report_passed must agree with the trust state: a check is "
                "either capable of deciding its question and may report a pass, "
                "or it is not and may not. Two answers to one question is the "
                "defect this module exists to remove."
            )

    @property
    def untrustworthy(self) -> bool:
        return self.state == TRUST_UNTRUSTWORTHY

    def receipt(self) -> Dict[str, Any]:
        """The payload an :class:`~execution.e2e_verify.E2EResult` carries."""
        return {
            "state": self.state,
            "may_report_passed": bool(self.may_report_passed),
            "signals": list(self.signals),
            "reason": "; ".join(self.reasons) if self.reasons else "trusted",
            "all_reasons": list(self.reasons),
            "ownership": self.ownership.to_dict(),
            "strength": self.strength.to_dict(),
            "fast_pass": self.fast_pass.to_dict(),
            "e2e_state": E2E_UNTRUSTWORTHY,
        }

    def render(self) -> List[str]:
        lines = [f"e2e check trust: {self.state.upper()}"]
        lines.append(f"  may report passed : {self.may_report_passed}")
        lines.append(f"  command shape    : {self.ownership.shape}")
        lines.append(f"  strength         : {self.strength.strength} (can_fail={self.strength.can_fail})")
        lines.append(
            f"  timing           : {self.fast_pass.duration_s:.4f}s vs "
            f"{self.fast_pass.floor_name} floor "
            f"{'(none)' if self.fast_pass.floor_s is None else f'{self.fast_pass.floor_s}s'}"
            f"  suspicious={self.fast_pass.suspicious}"
        )
        for signal in self.signals:
            lines.append(f"    - {signal}: {SIGNAL_DESCRIPTIONS.get(signal, '?')}")
        for reason in self.reasons:
            lines.append(f"    because {reason}")
        return lines


# ==========================================================================
# Commit anchoring — "which tree is this?"
# ==========================================================================


@dataclass(frozen=True)
class CommitAnchor:
    """Which commit a measurement is about, and how confident that answer is.

    ``status`` is a closed vocabulary of four, and ``unknown`` is one of them:
    a repository with no HEAD, a detached worktree, or an unreadable ``.git``
    must NOT report a commit, because a fabricated one is worse than none — it
    makes evidence from two different trees look like evidence about one.
    """

    commit: str
    status: str
    ref: str = ""
    source: str = ""
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.status == "resolved"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit": self.commit,
            "status": self.status,
            "known": bool(self.known),
            "ref": self.ref,
            "source": self.source,
            "reason": self.reason,
        }


COMMIT_RESOLVED = "resolved"
COMMIT_UNBORN = "unborn"
COMMIT_DETACHED = "detached"
COMMIT_UNKNOWN = "unknown"
COMMIT_STATUSES: Tuple[str, str, str, str] = (
    COMMIT_RESOLVED,
    COMMIT_UNBORN,
    COMMIT_DETACHED,
    COMMIT_UNKNOWN,
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _resolve_git_dir(repo_path: Path) -> Tuple[Optional[Path], str]:
    """Find the real git directory, following a worktree's ``gitdir:`` file."""
    candidate = repo_path / ".git"
    if candidate.is_dir():
        return candidate, ""
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            return None, f"the .git FILE could not be read: {exc}"
        if not text.lower().startswith("gitdir:"):
            return None, f"the .git file does not name a git directory: {text!r}"
        target = Path(text.split(":", 1)[1].strip())
        if not target.is_absolute():
            target = (repo_path / target).resolve()
        if not target.is_dir():
            return None, f"the .git file names a directory that does not exist: {target}"
        return target, ""
    return None, "no .git directory or file: this is not a git working tree"


def read_commit(repo_path: str) -> CommitAnchor:
    """Read the commit a working tree is on, by FILESYSTEM alone.

    Deliberately does not spawn ``git``. Three reasons, and the first is the
    one that matters: a subprocess reader would need a row in
    ``execution/test_p2w1_trust_pins.py::SUBPROCESS_CLASSIFICATION``, and a new
    host spawn in this package is a reviewable cost. Reading ``.git/HEAD`` and
    the ref (or ``packed-refs``) needs no authority at all and cannot be
    steered by a hostile ``PATH``.

    Four statuses, and ``unknown`` is one of them. A repository with no commit
    yet, a detached HEAD, a missing ``.git``, or an unreadable file all report
    what they are. **None of them invents a commit**, because a fabricated
    anchor is worse than a missing one: it makes evidence from two different
    trees look like evidence about one.
    """
    root = Path(str(repo_path or ""))
    git_dir, why = _resolve_git_dir(root)
    if git_dir is None:
        status = COMMIT_UNKNOWN
        if "not a git working tree" in why:
            status = COMMIT_UNKNOWN
        return CommitAnchor(commit="", status=status, source="", reason=why)

    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return CommitAnchor(
            commit="",
            status=COMMIT_UNKNOWN,
            source=str(head_path),
            reason="HEAD is absent from the git directory",
        )
    except OSError as exc:
        return CommitAnchor(
            commit="",
            status=COMMIT_UNKNOWN,
            source=str(head_path),
            reason=f"HEAD could not be read: {exc}",
        )

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        if not ref:
            return CommitAnchor(
                commit="",
                status=COMMIT_UNKNOWN,
                ref=ref,
                source=str(head_path),
                reason="HEAD names an empty ref",
            )
        loose = git_dir / Path(*ref.split("/"))
        try:
            if loose.is_file():
                value = loose.read_text(encoding="utf-8", errors="replace").strip()
                if _SHA1.match(value):
                    return CommitAnchor(
                        commit=value, status=COMMIT_RESOLVED, ref=ref, source=str(loose)
                    )
                return CommitAnchor(
                    commit="",
                    status=COMMIT_UNKNOWN,
                    ref=ref,
                    source=str(loose),
                    reason=f"the loose ref holds {value!r}, which is not a 40-hex sha",
                )
        except OSError as exc:
            return CommitAnchor(
                commit="",
                status=COMMIT_UNKNOWN,
                ref=ref,
                source=str(loose),
                reason=f"the loose ref could not be read: {exc}",
            )
        packed = git_dir / "packed-refs"
        try:
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2 and parts[1].strip() == ref and _SHA1.match(parts[0]):
                        return CommitAnchor(
                            commit=parts[0],
                            status=COMMIT_RESOLVED,
                            ref=ref,
                            source=str(packed),
                        )
        except OSError as exc:
            return CommitAnchor(
                commit="",
                status=COMMIT_UNKNOWN,
                ref=ref,
                source=str(packed),
                reason=f"packed-refs could not be read: {exc}",
            )
        return CommitAnchor(
            commit="",
            status=COMMIT_UNBORN,
            ref=ref,
            source=str(head_path),
            reason=(
                f"HEAD points at {ref!r} and neither a loose ref nor "
                f"packed-refs holds it: this repository has no commit yet, so "
                f"there is no commit for a measurement to be about"
            ),
        )

    if _SHA1.match(head):
        return CommitAnchor(
            commit=head, status=COMMIT_DETACHED, ref="", source=str(head_path)
        )
    return CommitAnchor(
        commit="",
        status=COMMIT_UNKNOWN,
        source=str(head_path),
        reason=f"HEAD holds {head!r}, which is neither a ref nor a 40-hex sha",
    )


def assert_five_states_distinct() -> Tuple[str, ...]:
    """Require the five end-to-end states to be five DISTINCT values.

    Checkable in one line and worth checking, because a duplicated member makes
    a closed vocabulary silently smaller: ``E2E_STATES`` with
    ``E2E_UNTRUSTWORTHY == E2E_PASSED`` would still be a "closed five-tuple" to
    every test that only counted it.

    Returns the five values, and refuses when any two are equal or when the set
    is not exactly the published vocabulary. A caller that only wants the
    assertion can ignore the return value.
    """
    from execution import e2e_verify

    published = (
        e2e_verify.E2E_PASSED,
        e2e_verify.E2E_FAILED,
        e2e_verify.E2E_BLOCKED,
        e2e_verify.E2E_NOT_RUN,
        e2e_verify.E2E_UNTRUSTWORTHY,
    )
    if tuple(e2e_verify.E2E_STATES) != published:
        raise AssertionError(
            f"E2E_STATES is {e2e_verify.E2E_STATES!r}, not the published "
            f"five-value vocabulary {published!r}"
        )
    if len(set(published)) != len(published):
        raise AssertionError(
            f"the end-to-end state vocabulary has a duplicate member: {published!r}"
        )
    return published


# ==========================================================================
# The three assessments
# ==========================================================================


def assess_ownership(
    check: E2ECheck,
    *,
    repo_path: str = "",
    declaration_digest: str = "",
    vouched_digest: str = "",
) -> OwnershipVerdict:
    """Is this check the repository's own? (attack rows 1, 2 and 3)

    The check is the repository's when its DECLARATION is the repository's and
    its COMMAND is a feature probe rather than a unit test or a no-op. Two
    independent grounds, because they fail differently:

    * a **moved declaration digest** means the agent edited the check it is
      verified by (rows 2 and 3). This needs a caller to have vouched for a
      digest; with no vouched digest this signal cannot fire and the verdict
      says so rather than pretending to have checked.
    * a **unit-runner or inert command** means the check is answering a
      different question (row 1). This needs no vouched digest and is therefore
      the arm that works on a repository nobody has pinned.
    """
    signals: List[str] = []
    reasons: List[str] = []

    shape, shape_notes = command_shape(check.command, repo_path=repo_path)
    test_paths = test_paths_in(check.command, repo_path=repo_path)
    if shape == "inert":
        signals.append("command_is_inert")
        reasons.append("the declared command is a no-op, so it cannot fail and it decides nothing")
    elif shape == "unit_runner":
        signals.append("command_is_the_unit_runner")
        reasons.append(
            "the declared command is this repository's own unit-suite command. "
            "That is execution/verify.py's question; running it here would "
            "report the unit result as end-to-end evidence, and "
            "neither_substitutes() refuses exactly that substitution"
        )
    elif shape == "skips":
        signals.append("command_skips_its_own_cases")
        reasons.extend(shape_notes)

    if test_paths:
        signals.append("command_runs_a_test_path")
        reasons.append(
            "the declared command runs test file(s) "
            + ", ".join(test_paths[:5])
            + ", and a test file is a file an agent can write. The check is not "
            "the repository's own"
        )

    if _FORGES_OUTPUT.search(str(check.command or "")):
        signals.append("command_forges_its_own_output")
        reasons.append(
            "the declared command PRINTS a runner's result line instead of "
            "producing one"
        )

    # One decision, shared with assess_strength — see _has_subject.
    _ref = referenced_paths(check.command)
    _shape_now, _ = command_shape(check.command, repo_path=repo_path)
    _no_subject = _has_subject_signal(
        check.command, shape=_shape_now, referenced=_ref, test_paths=test_paths
    )
    if _no_subject:
        signals.append(_no_subject)
        reasons.append(
            "the declared command names nothing in the repository under test, "
            "no runner and no external surface, so it has no subject"
        )

    neutralised = self_neutralised_spans(check.command)
    if neutralised:
        signals.append("command_self_neutralised")
        reasons.append(
            "the command forces its own success ("
            + "; ".join(neutralised)
            + "), which is the shell-level equivalent of a skip marker"
        )

    if not str(check.feature or "").strip():
        signals.append("no_feature_named")
        reasons.append(
            "the check names no feature, so its verdict is attached to no "
            "behaviour a user touches"
        )

    if not str(check.description or "").strip() and shape != "inert":
        # A description is not evidence on its own — it is a signal that the
        # repository stated what the check is FOR, which is the difference
        # between a check and a command someone ran once.
        pass

    current = str(declaration_digest or "")
    vouched = str(vouched_digest or "")
    if current and vouched and current != vouched:
        signals.append("declaration_digest_moved")
        reasons.append(
            f"the declaration's bytes changed: vouched {vouched[:12]}, now "
            f"{current[:12]}. An agent that edits the check it is verified by "
            f"has made the check its own, and a check the agent owns verifies "
            f"nothing"
        )

    agent_authored: Optional[bool]
    if "declaration_digest_moved" in signals:
        agent_authored = True
    elif vouched and current and current == vouched:
        agent_authored = False
    else:
        agent_authored = None

    trusted = not signals
    return OwnershipVerdict(
        trusted=trusted,
        shape=shape,
        signals=tuple(dict.fromkeys(signals)),
        reasons=tuple(reasons),
        declaration_digest=current,
        vouched_digest=vouched,
        agent_authored=agent_authored,
    )


def assess_strength(
    check: E2ECheck,
    *,
    repo_path: str = "",
    exit_code: Optional[int] = None,
    raw_output: str = "",
    container_name: str = "",
    ran: Optional[bool] = None,
) -> StrengthVerdict:
    """Is the check strong enough to decide its own question? (attack row 4)

    **``can_fail`` is the sharpest single answer and it is decided first.** A
    check that cannot fail cannot be evidence in either direction, whatever it
    reports. Two mechanical ways to be unable to fail: the command is inert, or
    the command forces its own success. Neither is compensable by anything the
    rest of the declaration says.

    The other half is observable: a run whose output carries **no result line**
    has given a reader nothing to read, and "the exit code was 0" is not a
    result. That is `output_carries_no_result`, and it is what makes the
    ``strong`` arm a measurement rather than an absence of complaint.

    ``unmeasurable`` — zero weak AND zero strong signals — is the non-vacuity
    control. Without it this function would approve any command it had never
    seen, which is a rubber stamp with a vocabulary.
    """
    shape, _shape_notes = command_shape(check.command, repo_path=repo_path)
    weak: List[str] = []
    strong: List[str] = []
    reasons: List[str] = []

    if shape in ("inert", "empty"):
        weak.append("command_is_inert")
        reasons.append("the command cannot fail: every segment is a no-op")
    elif shape == "unit_runner":
        weak.append("command_is_the_unit_runner")
        reasons.append("this is the unit-suite command, which is a different question")
    elif shape == "skips":
        weak.append("command_skips_its_own_cases")
        reasons.append("the command deselects cases, so it cannot observe the failure")
    elif shape == "runner":
        strong.append("invokes_a_runner")
        reasons.append("the command names a real test runner")

    if self_neutralised_spans(check.command):
        weak.append("command_self_neutralised")
        reasons.append("the command forces its own success")

    if _FORGES_OUTPUT.search(str(check.command or "")):
        weak.append("command_forges_its_own_output")
        reasons.append(
            "the command PRINTS a runner's result line rather than producing "
            "one, so it can report 'passed' with nothing having run"
        )

    if str(check.feature or "").strip():
        strong.append("declares_a_feature")
    else:
        weak.append("no_feature_named")
        reasons.append("the check names no feature")

    # -- referenced paths ---------------------------------------------------
    referenced = _repo_relative_paths(check.command)
    root = Path(str(repo_path or "."))
    absent: List[str] = []
    present: List[str] = []
    for relative in referenced:
        try:
            exists = (root / relative).exists()
        except OSError:
            exists = False
        (present if exists else absent).append(relative)
    if present:
        strong.append("names_an_existing_repo_path")
    if absent:
        weak.append("referenced_path_absent")
        reasons.append(
            "the command names repository path(s) that do not exist: "
            + ", ".join(absent[:5])
        )

    # -- does the check have a SUBJECT at all? (attack rows 1 and 4) --------
    test_paths = test_paths_in(check.command, repo_path=repo_path)
    reaches_external = bool(_REACHES_EXTERNAL.search(str(check.command or "")))

    # -- does the referenced source ASSERT anything? (attack row 4, hard) ---
    # Placed after `test_paths` is known: a unit test file is already refused as
    # an end-to-end check by its own signal, and counting assertions inside one
    # would add a generic reason under a specific finding.
    if present and not test_paths:
        asserted, scanned = assertion_count_for(check.command, repo_path=repo_path)
        if scanned and asserted == 0:
            weak.append("command_asserts_nothing")
            reasons.append(
                "the source file(s) this check runs contain NO assertion at all: "
                + ", ".join(scanned[:5])
                + ". A check that cannot fail is not a passing check, whatever "
                "exit code it reports"
            )
        elif asserted:
            reasons.append(
                f"the source file(s) this check runs contain {asserted} "
                f"assertion(s), so the run can actually fail"
            )

    if test_paths:
        weak.append("command_runs_a_test_path")
        reasons.append(
            "the command runs test file(s) "
            + ", ".join(test_paths[:5])
            + " as its end-to-end check. A unit test is execution/verify.py's "
            "question — neither_substitutes() refuses the substitution in both "
            "directions — and a test file is a file an agent can write, so the "
            "check is not the repository's own"
        )
    if reaches_external:
        strong.append("reaches_an_external_surface")

    # Row 4's real detection, and it is structural: `python -c "import sys;
    # sys.exit(0)"` is not readable as weak by eye, because it looks like a
    # program. The decision is `_has_subject`, shared with assess_ownership.
    _has, _why = _has_subject(
        check.command, shape=shape, referenced=referenced, test_paths=test_paths
    )
    if not _has:
        weak.append("command_exercises_nothing")
        reasons.append(_why)

    # -- the observed half --------------------------------------------------
    did_run = bool(ran) if ran is not None else (exit_code is not None)
    if did_run:
        carriers = _result_summary_carriers(raw_output)
        if carriers:
            strong.append("output_carries_a_result_summary")
            reasons.append(
                "the output carries a runner's own result line(s): "
                + ", ".join(carriers)
            )
        elif not _REACHES_EXTERNAL.search(str(check.command or "")):
            # NOT fired when the check REACHES AN EXTERNAL SURFACE. A health
            # endpoint's JSON is the result and carries no runner summary, so
            # firing here refused every legitimate `curl {host}:{port}/health`
            # check. This is the same false-positive class this round already
            # fixed twice: a weak signal that fires on working code gets
            # disabled, and it hides the signals that matter behind its noise.
            weak.append("output_carries_no_result")
            reasons.append(
                "the run produced no result line for a reader: a bare exit code "
                "distinguishes 'worked' from 'did nothing' not at all"
            )
        if str(container_name or "").strip():
            strong.append("boundary_was_crossed")
            reasons.append(f"a container was created ({container_name}), so the run really crossed the boundary")
        else:
            weak.append("output_carries_no_result")
            reasons.append(NO_CONTAINER_REASON)

    # -- can_fail, decided last and summarised -----------------------------
    blocking = (
        "command_is_inert",
        "command_self_neutralised",
        "command_skips_its_own_cases",
    )
    can_fail = not any(signal in weak for signal in blocking)
    if can_fail:
        strong.append("can_fail")

    weak_u = tuple(dict.fromkeys(weak))
    strong_u = tuple(dict.fromkeys(strong))
    if weak_u:
        strength = STRENGTH_WEAK
    elif strong_u:
        strength = STRENGTH_STRONG
    else:
        strength = STRENGTH_UNMEASURABLE
        reasons.append(
            "nothing was looked at: the command matched no weak shape and no "
            "strong one either. An unassessed check is not a trusted check, so "
            "this reports `unmeasurable` rather than `strong`"
        )
    return StrengthVerdict(
        strength=strength,
        can_fail=bool(can_fail),
        weak_signals=weak_u,
        strong_signals=strong_u,
        reasons=tuple(reasons),
        shape=shape,
    )


def assess_fast_pass(
    check: E2ECheck,
    *,
    duration_s: float,
    container_name: str = "",
    ran: Optional[bool] = None,
    prior_durations_s: Sequence[float] = (),
    declared_min_duration_s: Optional[float] = None,
) -> FastPassVerdict:
    """Did this run finish faster than the boundary that ran it? (row 6)

    Three independent arms, and any one of them fires:

    1. **the container floor** — measured (see :data:`FLOOR_TABLE`), and it is
       the cost of ``docker run`` itself, so it holds on any host;
    2. **the ratio** — under :data:`FAST_PASS_RATIO` of this check's own prior
       median for the same content. Host-independent, and it is the arm that
       catches "it was real last run and it is a stub now";
    3. **the structural arm** — a result claiming ``ran=True`` with no
       container name. Needs no timing whatsoever, which is why a driver that
       stubs the boundary cannot hide from it.

    ``observed=False`` when there is no duration to judge, which is reported as
    UNOBSERVED rather than as "not suspicious" — the rule this repository keeps
    re-learning: an axis that could not be measured is not a held guarantee.
    """
    reasons: List[str] = []
    wall = float(duration_s or 0.0)
    floor_name, floor_s = floor_for(check)
    did_run = bool(ran) if ran is not None else True

    prior = [float(item) for item in (prior_durations_s or ()) if float(item) > 0.0]
    prior_median: Optional[float] = None
    ratio: Optional[float] = None
    if prior:
        prior_median = float(statistics.median(prior))
        if prior_median > 0:
            ratio = wall / prior_median
            if ratio < FAST_PASS_RATIO:
                reasons.append(
                    f"this run took {wall:.4f}s against a prior median of "
                    f"{prior_median:.4f}s for the same check on the same "
                    f"content ({ratio * 100:.2f}% of it, bar is "
                    f"{FAST_PASS_RATIO * 100:.0f}%)"
                )

    if declared_min_duration_s is not None and wall < float(declared_min_duration_s):
        reasons.append(
            f"the repository declared a floor of {float(declared_min_duration_s)}s "
            f"for this check and the run took {wall:.4f}s"
        )

    if did_run and not str(container_name or "").strip():
        reasons.append(NO_CONTAINER_REASON)

    if floor_s is not None and wall < float(floor_s):
        reasons.append(
            f"the run finished in {wall:.4f}s, below the {floor_name} floor of "
            f"{float(floor_s)}s. That floor is the MEASURED cost of reaching a "
            f"sandboxed command on this host "
            f"(measured minimum {FLOOR_TABLE[floor_name]['measured_min_s']}s), so "
            f"a run below it did not reach a sandbox at all"
        )

    return FastPassVerdict(
        suspicious=bool(reasons),
        duration_s=wall,
        floor_name=floor_name,
        floor_s=floor_s,
        reasons=tuple(reasons),
        ratio_to_prior=ratio,
        prior_median_s=prior_median,
        observed=bool(did_run and (wall > 0.0 or prior)),
    )


# ==========================================================================
# The gate
# ==========================================================================


def signals_of(verdict: TrustVerdict) -> Tuple[str, ...]:
    """Every signal across the three assessments, de-duplicated, sorted."""
    merged: List[str] = []
    for group in (verdict.ownership.signals, verdict.strength.weak_signals):
        merged.extend(group)
    return tuple(sorted(set(merged)))


def assess_check(
    check: E2ECheck,
    *,
    repo_path: str = "",
    exit_code: Optional[int] = None,
    raw_output: str = "",
    container_name: str = "",
    ran: Optional[bool] = None,
    duration_s: float = 0.0,
    declaration_digest: str = "",
    vouched_digest: str = "",
    prior_durations_s: Sequence[float] = (),
    declared_min_duration_s: Optional[float] = None,
    assess_timing: bool = True,
) -> TrustVerdict:
    """Fold the three assessments into one answer, with everything published.

    ``assess_timing=False`` is for a driver that wants to assess a check it did
    NOT run (rows 2 and 3, where the refusal is pre-run). Timing a run that
    never happened would compare 0.0 s against the container floor and report
    "suspiciously fast" for a check that was never attempted — a true statement
    about the wrong thing.
    """
    assert_five_states_distinct()
    ownership = assess_ownership(
        check,
        repo_path=repo_path,
        declaration_digest=declaration_digest,
        vouched_digest=vouched_digest,
    )
    strength = assess_strength(
        check,
        repo_path=repo_path,
        exit_code=exit_code,
        raw_output=raw_output,
        container_name=container_name,
        ran=ran,
    )
    if assess_timing:
        fast = assess_fast_pass(
            check,
            duration_s=float(duration_s or 0.0),
            container_name=container_name,
            ran=ran,
            prior_durations_s=prior_durations_s,
            declared_min_duration_s=declared_min_duration_s,
        )
    else:
        _, floor_s = floor_for(check)
        fast = FastPassVerdict(
            suspicious=False,
            duration_s=0.0,
            floor_name="not_assessed",
            floor_s=floor_s,
            reasons=(
                "timing was not assessed: this check was not run, so there is "
                "no duration to judge and comparing an absent run against the "
                "container floor would be a true statement about the wrong thing",
            ),
            observed=False,
        )

    reasons: List[str] = []
    reasons.extend(ownership.reasons)
    reasons.extend(strength.reasons)
    reasons.extend(fast.reasons)
    signals = signals_of(
        TrustVerdict(
            state=TRUST_TRUSTED,
            may_report_passed=True,
            reasons=(),
            signals=(),
            ownership=ownership,
            strength=strength,
            fast_pass=fast,
        )
    )

    may_pass = (
        ownership.may_report_passed and strength.may_report_passed and fast.may_report_passed
    )
    verdict = TrustVerdict(
        state=TRUST_TRUSTED if may_pass else TRUST_UNTRUSTWORTHY,
        may_report_passed=may_pass,
        reasons=tuple(reasons),
        signals=signals,
        ownership=ownership,
        strength=strength,
        fast_pass=fast,
    )
    return verdict


def guard_e2e_check(
    check: E2ECheck,
    *,
    repo_path: str = "",
    declaration_digest: str = "",
    vouched_digest: str = "",
) -> Tuple[bool, TrustVerdict]:
    """Refuse BEFORE running: is this check safe to run at all? (rows 2, 3)

    Returns ``(may_run, verdict)``. Deliberately pre-run, and deliberately a
    tuple rather than a result object: a check refused here produced no
    measurement, and a caller that wants the fifth-state row builds it with
    :func:`execution.e2e_verify.untrustworthy_result` and ``ran=False``.

    The refusal covers ownership and strength but **not** timing, for the reason
    in :func:`assess_check`: there is no duration yet.
    """
    verdict = assess_check(
        check,
        repo_path=repo_path,
        declaration_digest=declaration_digest,
        vouched_digest=vouched_digest,
        assess_timing=False,
    )
    return verdict.may_report_passed, verdict


def gate_e2e_result(
    result: E2EResult,
    check: E2ECheck,
    *,
    repo_path: str = "",
    declaration_digest: str = "",
    vouched_digest: str = "",
    prior_durations_s: Sequence[float] = (),
    declared_min_duration_s: Optional[float] = None,
) -> E2EResult:
    """Convert a run into the fifth state when the run cannot be trusted.

    The rule is one line and it is a refusal: **a ``passed`` result from a check
    that is not ``trusted`` never survives.** Everything else is passed through
    untouched — including a ``failed`` result from a weak check, because a
    failing weak check is still evidence that *something* is wrong and turning
    it into ``untrustworthy`` would hide a real signal behind a meta-problem.

    The asymmetry is the point. Over-refusing costs a re-run; under-refusing
    mints a false pass, and this project exists to prevent the second.
    """
    assert_five_states_distinct()
    verdict = assess_check(
        check,
        repo_path=repo_path,
        exit_code=result.exit_code,
        raw_output=result.raw_output,
        container_name=result.container_name,
        ran=result.ran,
        duration_s=result.duration_s,
        declaration_digest=declaration_digest,
        vouched_digest=vouched_digest,
        prior_durations_s=prior_durations_s,
        declared_min_duration_s=declared_min_duration_s,
    )
    if verdict.may_report_passed or result.state != "passed":
        return result
    return untrustworthy_result(
        check,
        verdict.receipt(),
        reason=(
            "the check ran and exited "
            f"{result.exit_code}, but it cannot decide the question it was "
            "declared to decide"
        ),
        ran=result.ran,
        observations=result.observations,
        duration_s=result.duration_s,
        exit_code=result.exit_code,
        raw_output=result.raw_output,
        container_name=result.container_name,
        flaky_check=result.flaky_check,
        environment=result.environment,
    )


# ==========================================================================
# Reuse — a reused result names what it was reused from
# ==========================================================================


def reuse_of(
    *,
    content_digest: str,
    stored_at: Any = None,
    matched_by: str = "",
    commit: str = "",
    feature_id: str = "",
    state: str = "",
) -> Dict[str, Any]:
    """The mark a REUSED result must carry, distinct from a fresh run.

    Four fields, all required, and none of them optional:

    * ``reused`` — the single boolean a surface can branch on;
    * ``fresh_evidence`` — **hard-coded ``False``**, the only value this
      function can produce, which is what makes "reuse may save a re-run but
      can never mark a feature passing" structural rather than a convention;
    * ``reused_from`` — the CONTENT ADDRESS it came from, so a reader can
      establish for themselves that the tree state matched;
    * ``distinct_from_fresh`` — an explicit statement that this row is NOT a
      fresh run, because "the same object shape" is exactly what makes a reused
      row readable as a fresh one.

    ``commit`` is deliberately here and deliberately allowed to be empty: an
    unanchored measurement says so rather than inheriting the caller's commit by
    default, and :func:`execution.e2e_evidence.assert_same_commit` refuses it.
    """
    when = ""
    if stored_at:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(stored_at)))
        except (OverflowError, OSError, ValueError, TypeError):
            when = str(stored_at)
    digest = str(content_digest or "")
    source = f"reused from content digest {digest[:12] or '(none)'}"
    if when:
        source += f", measured {when}"
    if commit:
        source += f", on commit {commit[:12]}"
    return {
        "reused": True,
        "fresh_evidence": False,
        "distinct_from_fresh": True,
        "reused_from": digest,
        "stored_at": stored_at,
        "stored_at_rendered": when,
        "matched_by": str(matched_by or "exact content digest of every file the run read"),
        "commit": str(commit or ""),
        "feature_id": str(feature_id or ""),
        "state": str(state or ""),
        "summary": source,
        "may_mark_feature_passing": False,
        "note": (
            "NOTHING RAN. This is a stored measurement of an EARLIER tree state, "
            "served because the content digest matched exactly. It may be shown "
            "to a human to save a re-run; it can never mark a feature passing, "
            "because the feature did not exist when it was written, and "
            "fresh_evidence=False is the only value this function can emit."
        ),
    }


# ==========================================================================
# The self-test the CLI runs
# ==========================================================================


def _floor_receipt() -> Dict[str, Any]:
    return {
        "floors": {name: dict(row) for name, row in FLOOR_TABLE.items()},
        "fast_pass_ratio": FAST_PASS_RATIO,
        "margin_note": FLOOR_MARGIN_NOTE,
        "reproduce": "python -m execution.e2e_attack --floors",
    }
