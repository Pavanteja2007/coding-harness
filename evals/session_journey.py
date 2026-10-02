"""The human session harness: drive the REAL shell through a real session.

Every defect ever reported in this project was found by a PERSON, not by a
test. This module is the harness that makes a person's session the test input.
It drives the real `cli.tui.NeoApp` through Textual's own `Pilot` -- the same
compositor a terminal drives -- and records what the user SEES.

Three things make it evidence rather than a screenshot:

1. **The corpus is the intent router's regression net.** `PHRASE_CORPUS` is 69
   utterances a person would really type (typos, no punctuation, all lowercase,
   run-on sentences, vague requests, requests naming no file). Each carries the
   routing the deterministic tier produced ON THIS TREE -- measured by running
   the router, not predicted. Thirteen of them route somewhere a person would
   not want; those are in `RECORDED_ROUTING_GAPS` with a reason each, and the
   test carries an INVERTED pin that fails the day somebody fixes one. A
   recorded gap is a gap somebody can close; an unrecorded one just gets
   rediscovered.

2. **The assertions are on the visible surface.** Rows rendered, messages that
   did not vanish, no raw journal event name, no library traceback, the
   product's own status vocabulary, and honest verification. Nothing here
   asserts against an internal: it reads the mounted widget tree and the
   compositor's own output.

3. **A run leaves a machine-readable transcript and an SVG, and two runs
   DIFF.** `diff_runs` is the visual-regression gate: a change in what a person
   sees becomes a diff in a document rather than a hunch.

Offline and deterministic by construction: no Docker, no provider, no network,
no credential. The session backends are installed as doubles through
`cli.interactive`'s documented module attributes and the model boundary through
`harness.deps.set_call_model`; every journal row is written by the driver, so
the shell's own projection is the real one.

Standalone entry point -- its own module rather than an `evals.run --suite`
row, because `run.py` and `__main__.py` are other terminals' in-flight files:

    python -m evals.session_journey --out-root logs/product-round/session-journey
    python -m evals.session_journey --quick
    python -m evals.session_journey --json            # exit 2 on a finding
    python -m evals.session_journey --diff <previous-run-dir>

Config discipline: this module adds NO key to `harness/config.py::DEFAULTS`. A
value there merges into every Task and every eval arm; the bounds here belong
to an evidence driver, not to a run, so they are module-level constants.

Anti-clutter: the transcript document groups its findings and renders a group
only when it carries at least `cli.design.ANTI_CLUTTER_MIN_ENTRIES` entries,
through the product's own `design.section_is_rendered`. A two-entry section is
not written down at all. Reading the threshold rather than restating it is the
point -- this document is a panel too, and a rule that lives in one place is a
rule the panels cannot disagree about.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The viewport every journey receipt is captured at. 120x36 is the smallest
#: size at which the plan rail AND the context rail both render, so a receipt
#: shows a real composition rather than the collapsed one.
VIEWPORT: Tuple[int, int] = (120, 36)

#: How long one typed line may occupy before the driver stops waiting on the
#: worker. A step that is cancelled deliberately parks longer, so the cancel
#: scenario uses its own bound.
SETTLE_TIMEOUT_S = 12.0

#: How long the driver waits for a parked fake worker to actually start.
PARK_TIMEOUT_S = 10.0

#: The idle poll between worker checks. Short enough that a fast fake looks
#: instant; long enough not to spin a core on a four-terminal host.
POLL_S = 0.02

#: Cap on a captured surface. A bound that is REPORTED is not a silent
#: truncation, and the transcript carries `truncated: true` when it bites --
#: a silent truncation turns a wall into evidence of a clean surface.
MAX_CAPTURE_CHARS = 24000

#: How much of a step's transcript the receipt keeps. The full text lives in
#: the frame and the live document; the per-step row keeps the tail, which is
#: where a completion card and a failure card land. The tail is a SLIDING
#: WINDOW and is deliberately NOT the diff's signal -- see `delta` below.
STEP_TRANSCRIPT_TAIL_CHARS = 4000

#: How much of a step's DELTA the receipt keeps -- the text the step actually
#: added, whitespace-normalised. This is the diff's primary signal.
#:
#: Two capture artefacts had to be removed first, and both are properties of
#: the READER rather than of the product:
#:
#: * A `RichLog`'s stored segment stream can drop a space at a wrap boundary
#:   (`...code` + `in this repo...`) that the rendered frame keeps. Comparing
#:   two runs of the same tree reported 18 "changes" that were that one space.
#: * The 4 000-character TAIL slides on every turn of a sixty-turn session, so
#:   it moves even when nothing did. 93 of 111 diff hits were the window
#:   moving, not the product.
#:
#: So the signal is the whitespace-normalised delta, and the note in the diff
#: document says the tail is context rather than signal.
STEP_DELTA_CHARS = 2000

#: How long the driver lets the shell SETTLE before capturing a step's
#: receipt. The run line is repainted by a 0.125 s timer and the journal tail
#: is a thread, so a capture taken the instant a step returns can miss rows
#: that arrived a frame later -- which is not a product change but a
#: measurement of the scheduler. Bounded, and the number is in the metrics.
FRAME_SETTLE_S = 0.20


# ---------------------------------------------------------------------------
# 1. The phrase corpus -- measured, with the gaps recorded rather than hidden
# ---------------------------------------------------------------------------

#: The three kinds the deterministic tier can return. Duplicated on purpose:
#: this is a TRANSCRIPT-level check, and a corpus that imported the router's
#: own vocabulary could not detect the router growing a fourth kind.
ROUTER_KINDS: Tuple[str, ...] = ("agent_task", "question", "chit_chat")

#: What each kind means to the SHELL, as a dispatch. A `chit_chat` line
#: launches nothing at all -- the shell answers it inline -- so a corpus that
#: asserted "the shell dispatched chit_chat" would be asserting a dispatch the
#: product deliberately does not make. This map is the contract the routing net
#: is written against, and it is the reason the net says anything at all: it
#: compares the tier's VERDICT against what the shell actually DID.
EXPECTED_DISPATCH: Mapping[str, str] = {
    "agent_task": "agent_task",
    "question": "question",
    "chit_chat": "",
}


def expected_dispatch(kind: str) -> str:
    """The dispatch the shell makes for a router verdict of `kind`.

    An unrecognised kind returns `"?"`, which no dispatch equals -- so a
    fourth kind fails the net instead of passing it by accident.
    """
    return str(EXPECTED_DISPATCH.get(str(kind), "?"))


@dataclass(frozen=True)
class Phrase:
    """One utterance a person would really type, and how it routed.

    `kind` is the deterministic tier's answer on this tree. `sensible` is the
    SEPARATE judgement of whether that answer is what a person meant: a phrase
    can route reproducibly and still be wrong, and conflating the two is how a
    corpus stops being a net.
    """

    text: str
    kind: str
    sensible: bool = True
    group: str = "misc"
    note: str = ""


#: 69 utterances, grouped so a reader can see the shapes the brief names:
#: typos, no punctuation, all lowercase, run-on sentences, vague requests, and
#: requests naming no file.
PHRASE_CORPUS: Tuple[Phrase, ...] = (
    # -- greetings, thanks, meta ------------------------------------------
    Phrase("hi", "chit_chat", group="greeting"),
    Phrase("hey", "chit_chat", group="greeting"),
    Phrase("hello there", "chit_chat", group="greeting"),
    Phrase("good morning", "chit_chat", group="greeting"),
    Phrase("sup", "chit_chat", group="greeting"),
    Phrase("thanks", "chit_chat", group="greeting"),
    Phrase("ty", "chit_chat", group="greeting"),
    Phrase("ok", "chit_chat", group="greeting"),
    Phrase("ok cool", "chit_chat", group="greeting"),
    Phrase("you there?", "question", group="greeting"),
    Phrase("how's it going", "chit_chat", group="greeting"),
    Phrase("what can you do", "chit_chat", group="meta"),
    Phrase("who are you", "chit_chat", group="meta"),
    Phrase("what model are you using", "chit_chat", group="meta"),
    Phrase("list your commands", "chit_chat", group="meta"),
    Phrase("how do i quit", "chit_chat", group="meta"),
    Phrase("help", "chit_chat", group="meta"),
    Phrase("help me", "chit_chat", group="meta"),
    # -- the canonical bug sentences ---------------------------------------
    Phrase(
        "mean() in mathutil.py returns the sum, not the mean",
        "agent_task",
        group="bug_report",
    ),
    Phrase(
        "fix the failing test in tests/test_mathutil.py",
        "agent_task",
        group="bug_report",
    ),
    Phrase(
        "parse_config crashes on an empty file",
        "chit_chat",
        sensible=False,
        group="bug_report",
        note="a crash is the strongest bug signal there is; the router has no crash verb",
    ),
    Phrase(
        "the login page throws a 500 when you submit twice",
        "chit_chat",
        sensible=False,
        group="bug_report",
        note="'throws' is not in the fix-verb table either",
    ),
    Phrase("why is wrap() dropping the last line?", "question", group="bug_report"),
    Phrase("tests/test_mathutil.py::test_mean fails", "agent_task", group="bug_report"),
    # -- typos, no punctuation, all lowercase ------------------------------
    Phrase("plese fix teh lgin bug", "agent_task", group="typo"),
    Phrase("mean() retuns the sum not teh mean", "agent_task", group="typo"),
    Phrase("the thing is broken somewere idk", "chit_chat", group="typo"),
    Phrase(
        "cant login after redeploy",
        "chit_chat",
        sensible=False,
        group="typo",
        note="'cant' is a contraction, not the table's 'can't'",
    ),
    Phrase("add loggin to the auth handler", "agent_task", group="typo"),
    Phrase("why does the parser blow up on empty input", "question", group="typo"),
    Phrase(
        "its not commiting my changes",
        "chit_chat",
        sensible=False,
        group="typo",
        note="'not <verb-ing>' is a bug report and nothing in the tables reads it as one",
    ),
    Phrase("make the tests pass", "agent_task", group="typo"),
    Phrase("run the tests and fix failures", "agent_task", group="typo"),
    Phrase(
        "exec pytest tests -q",
        "chit_chat",
        sensible=False,
        group="typo",
        note="the run-verb table spells run/execute, not the common `exec` abbreviation",
    ),
    # -- vague, naming no file --------------------------------------------
    Phrase("its broken", "chit_chat", group="vague"),
    Phrase("something is off in here", "chit_chat", group="vague"),
    Phrase("this is not working", "chit_chat", group="vague"),
    Phrase("can you look at this", "chit_chat", group="vague"),
    Phrase("why doesnt it work", "question", group="vague"),
    Phrase("whats wrong", "chit_chat", group="vague"),
    Phrase("do the thing", "chit_chat", group="vague"),
    Phrase("make it better", "agent_task", group="vague"),
    Phrase(
        "clean this up",
        "chit_chat",
        sensible=False,
        group="vague",
        note="the table has 'clean up' but not the two words a person actually types",
    ),
    Phrase("sort it out", "chit_chat", group="vague"),
    Phrase("its too slow", "chit_chat", group="vague"),
    Phrase("the numbers are wrong", "chit_chat", group="vague"),
    Phrase("fix it", "agent_task", group="vague"),
    Phrase("change the behaviour", "agent_task", group="vague"),
    Phrase("support windows", "agent_task", group="vague"),
    # -- questions ---------------------------------------------------------
    Phrase("explain how routing works", "question", group="question"),
    Phrase("what does mean() do", "question", group="question"),
    Phrase("where is the retry logic", "question", group="question"),
    Phrase("which library parses the config", "question", group="question"),
    Phrase("how does the sandbox mount the repo", "question", group="question"),
    Phrase("walk me through the verify step", "question", group="question"),
    Phrase("what would break if i changed this", "question", group="question"),
    Phrase(
        "is the login handler thread safe",
        "chit_chat",
        sensible=False,
        group="question",
        note="a question with no interrogative word and no named artifact",
    ),
    # -- acknowledgement-shaped, but a real request -------------------------
    Phrase(
        "ok run the tests",
        "chit_chat",
        sensible=False,
        group="ack_request",
        note="the greeting branch fires before the run branch, so the leading 'ok' swallows the request",
    ),
    Phrase("ok fix the parser", "agent_task", group="ack_request"),
    Phrase(
        "undo that",
        "chit_chat",
        sensible=False,
        group="undo_request",
        note="'undo' is not a fix verb; the sentence falls through to the clarifying question",
    ),
    Phrase(
        "put that back",
        "chit_chat",
        sensible=False,
        group="undo_request",
        note="the same shape as 'undo that'",
    ),
    Phrase(
        "revert everything",
        "chit_chat",
        sensible=False,
        group="undo_request",
        note="'revert' is not in the fix-verb table; remove and delete are",
    ),
    # -- long / run-on ------------------------------------------------------
    Phrase(
        "hey so the thing is the parser is throwing on empty files again and i think it is "
        "because the guard was removed last week can you have a look",
        "chit_chat",
        sensible=False,
        group="run_on",
        note="a 30-word run-on bug report with a stated cause",
    ),
    Phrase(
        "add a flag to the run command that lets me pick the model and also print the cost "
        "at the end please",
        "agent_task",
        group="run_on",
    ),
    Phrase(
        "when i run the tests on windows the path separator breaks the config loader can you "
        "fix that",
        "agent_task",
        group="run_on",
    ),
    Phrase(
        "i think the mean function is wrong but im not 100 sure can you check and tell me what "
        "you find",
        "chit_chat",
        sensible=False,
        group="run_on",
        note="asks for an investigation and reads as unrecognised",
    ),
    # -- hostile: markup that must never be interpreted ---------------------
    Phrase(
        "fix [bold]app.py[/] the [weird]name[/] parser", "agent_task", group="hostile"
    ),
    Phrase("what is [red]this[/] tool", "question", group="hostile"),
    Phrase("[/] unbalanced closing tag", "chit_chat", group="hostile"),
)

#: The phrases whose measured routing is NOT what a person meant, with the
#: reason. Kept beside the corpus rather than in a comment so the reason
#: travels with the phrase and a fixer reads the reason, not just the word
#: "failing".
RECORDED_ROUTING_GAPS: Dict[str, str] = {
    phrase.text: phrase.note for phrase in PHRASE_CORPUS if not phrase.sensible
}


def corpus_gap_texts() -> Tuple[str, ...]:
    """The corpus phrases currently recorded as mis-routed, in corpus order."""
    return tuple(phrase.text for phrase in PHRASE_CORPUS if not phrase.sensible)


def sensible_phrases() -> Tuple[Phrase, ...]:
    """The corpus phrases that currently route the way a person meant."""
    return tuple(phrase for phrase in PHRASE_CORPUS if phrase.sensible)


# ---------------------------------------------------------------------------
# 2. Scenarios -- the shape of a real session, step by step
# ---------------------------------------------------------------------------

#: What a step EXPECTS to have happened, as a claim about the visible
#: surface. `ask` is a first-class outcome: the product's deliberate cost
#: asymmetry makes a clarifying question the right answer to a vague line, so
#: a gate that only accepted work and answers would be testing the wrong
#: product.
STEP_EXPECTATIONS: Tuple[str, ...] = (
    "ask",
    "answer",
    "work",
    "command",
    "cancel",
    "resume",
    "routed",
)


@dataclass(frozen=True)
class JourneyStep:
    """One thing a person does, and what should be visible afterwards."""

    label: str
    line: str
    expect: str = "work"
    #: Journal rows the fake session backend writes for this step, so the
    #: shell's own projection is the real one rather than a hand-drawn card.
    events: Tuple[Mapping[str, Any], ...] = ()
    #: The terminal status the fake backend publishes. `""` means
    #: `completed_unverified`, the honest default for a run with no verifier.
    status: str = ""
    #: Whether the fake backend appends a terminal `run_finished` row. A
    #: CANCELLED run must NOT have one: the shell's own sentence is "the run is
    #: not finished until the journal says so", so a journal that says it
    #: finished after an interrupt is a lie, and the resume path would (rightly)
    #: refuse a session the product considers completed.
    writes_terminal_row: bool = True
    #: Whether this step's `delta` may be compared between two runs. False for
    #: a step whose visible output is a RACE rather than a behaviour: the
    #: cancel step is typed and cancelled the instant the worker parks, so
    #: whether the journal tail got to render the run's feed lines before the
    #: cancel landed is a scheduling fact, and a diff that reported it would be
    #: reporting a change nobody made. The step's real claims -- that the
    #: worker parked, that the cancel landed, and how long it took -- are
    #: measurements, not deltas, and they are asserted directly.
    diffable: bool = True


@dataclass(frozen=True)
class JourneyScenario:
    """One scripted session, plus the repository shape it runs against."""

    slug: str
    title: str
    steps: Tuple[JourneyStep, ...]
    viewport: Tuple[int, int] = VIEWPORT
    #: A repository that is NOT a fixture. The brief names this shape, and the
    #: reason it matters is that a fixture's names are safe: nothing under
    #: `tests/fixtures/` contains a bracket.
    hostile_repo: bool = False


_DAILY_SESSION = JourneyScenario(
    slug="daily_session",
    title="several turns, a tool call, a diff, a failure, and a recovery",
    steps=(
        JourneyStep("greeting", "hi", expect="ask"),
        JourneyStep("question", "what does mean() do", expect="answer"),
        JourneyStep(
            "failing fix",
            "mean() in mathutil.py returns the sum, not the mean",
            expect="work",
            status="failed",
            events=(
                {
                    "kind": "plan",
                    "data": {"plan": [{"id": 1, "description": "read mathutil.py"}]},
                },
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "read",
                        "arguments": {"path": "mathutil.py"},
                        "command": "cat mathutil.py",
                    },
                },
                {
                    "kind": "tool_result",
                    "data": {
                        "tool": "read",
                        "path": "mathutil.py",
                        "ok": True,
                        "output": "def mean(values):\n    return sum(values)\n",
                    },
                },
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "edit",
                        "arguments": {"path": "mathutil.py"},
                        "command": "apply edit",
                    },
                },
                {
                    "kind": "tool_error",
                    "data": {
                        "kind": "syntax_error",
                        "message": "invalid syntax (line 2)",
                    },
                },
                {
                    "kind": "verify",
                    "data": {
                        "target_passed": False,
                        "regression_passed": True,
                        "raw": "1 failed",
                    },
                },
            ),
        ),
        JourneyStep(
            "recovery",
            "fix it properly this time",
            expect="work",
            status="completed_verified",
            events=(
                {
                    "kind": "plan",
                    "data": {
                        "plan": [{"id": 1, "description": "divide by the length"}]
                    },
                },
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "read",
                        "arguments": {"path": "mathutil.py"},
                        "command": "cat mathutil.py",
                    },
                },
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "edit",
                        "arguments": {"path": "mathutil.py"},
                        "command": "apply edit",
                    },
                },
                {
                    "kind": "tool_result",
                    "data": {
                        "tool": "edit",
                        "path": "mathutil.py",
                        "ok": True,
                        "output": "1 file changed",
                    },
                },
                {
                    "kind": "verify",
                    "data": {
                        "target_passed": True,
                        "regression_passed": True,
                        "raw": "3 passed",
                    },
                },
                {
                    "kind": "result",
                    "data": {
                        "status": "completed_verified",
                        "attempts": 2,
                        "files": ["mathutil.py"],
                        "diff": (
                            "--- a/mathutil.py\n+++ b/mathutil.py\n@@\n"
                            "-    return sum(values)\n+    return sum(values) / len(values)\n"
                        ),
                    },
                },
            ),
        ),
        JourneyStep("the diff", "/diff", expect="command"),
        JourneyStep("what it cost", "/cost", expect="command"),
    ),
)

#: A mid-run cancel and then a resume, in one shell. The cancel is REAL: the
#: fake backend parks until the product's own interrupt arrives, so what gets
#: cancelled is a live worker rather than a flag somebody set.
_CANCEL_AND_RESUME = JourneyScenario(
    slug="cancel_and_resume",
    title="a mid-run cancel and a resume, in the same shell",
    steps=(
        JourneyStep(
            "a run that parks",
            "refactor the auth handler so the token check is in one place",
            expect="cancel",
            status="",
            writes_terminal_row=False,
            diffable=False,
            events=(
                {
                    "kind": "task_start",
                    "data": {
                        "issue_text": "refactor the auth handler",
                        "mode": "agent_task",
                    },
                },
                {
                    "kind": "plan",
                    "data": {"plan": [{"id": 1, "description": "read the handler"}]},
                },
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "read",
                        "arguments": {"path": "auth.py"},
                        "command": "cat auth.py",
                    },
                },
                {
                    "kind": "tool_result",
                    "data": {
                        "tool": "read",
                        "path": "auth.py",
                        "ok": True,
                        "output": "def check_token(t):\n    return bool(t)\n",
                    },
                },
            ),
        ),
        JourneyStep("the resume", "resume", expect="resume"),
    ),
)

#: A run that finishes with no verifier behind it. The assertion is that the
#: shell says UNVERIFIED and that nothing in the frame reads as success.
_UNVERIFIED_SESSION = JourneyScenario(
    slug="unverified_is_not_success",
    title="a completed run with no verifier behind it",
    steps=(
        JourneyStep(
            "a run with no declared verifier",
            "add a docstring to mathutil.py",
            expect="work",
            status="completed_unverified",
            events=(
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "edit",
                        "arguments": {"path": "mathutil.py"},
                        "command": "apply edit",
                    },
                },
                {"kind": "verify_skipped", "data": {"reason": "no declared verifier"}},
            ),
        ),
    ),
)

#: A repository that is not a fixture, carrying a filename with a bracket in it
#: and a request carrying markup. A render failure must never DELETE a message,
#: so a control marker is typed first and must still be on screen at the end.
_HOSTILE_SESSION = JourneyScenario(
    slug="hostile_repo",
    title="a repository that is not a fixture, with hostile names and markup",
    hostile_repo=True,
    steps=(
        JourneyStep("greeting", "hi", expect="ask"),
        JourneyStep(
            "hostile request",
            "fix [bold]weird[name].py[/] so the [red]parser[/] stops dropping lines",
            expect="work",
            status="completed_unverified",
            events=(
                {
                    "kind": "tool_call",
                    "data": {
                        "tool": "read",
                        "arguments": {"path": "weird[name].py"},
                        "command": "cat 'weird[name].py'",
                    },
                },
                {
                    "kind": "tool_result",
                    "data": {
                        "tool": "read",
                        "path": "weird[name].py",
                        "ok": True,
                        "output": "MARKER_IN_FILE_CONTENT",
                    },
                },
            ),
        ),
        JourneyStep("the diff", "/diff", expect="command"),
    ),
)

#: Sixty turns. The point is not that sixty works; it is that the transcript
#: stays BOUNDED and every turn still produced a visible outcome.
LONG_TURN_COUNT = 60
_LONG_SESSION = JourneyScenario(
    slug="long_session",
    title=f"a {LONG_TURN_COUNT}-turn session",
    steps=tuple(
        JourneyStep(
            f"turn {index:02d}",
            (
                f"explain step {index} of the fix loop"
                if index % 3
                else f"fix the rounding in round_{index}.py"
            ),
            expect="answer" if index % 3 else "work",
            status="completed_unverified",
            events=(
                {
                    "kind": "tool_call",
                    "data": {"tool": "read", "command": "cat mathutil.py"},
                },
            ),
        )
        for index in range(1, LONG_TURN_COUNT + 1)
    ),
)

#: The corpus, driven through the real composer in one mounted shell.
_PHRASE_CORPUS_SCENARIO = JourneyScenario(
    slug="phrase_corpus",
    title=f"the {len(PHRASE_CORPUS)}-phrase corpus, typed into the real composer",
    steps=tuple(
        JourneyStep(f"phrase {index:02d}", phrase.text, expect="routed")
        for index, phrase in enumerate(PHRASE_CORPUS, start=1)
    ),
)

SCENARIOS: Tuple[JourneyScenario, ...] = (
    _DAILY_SESSION,
    _CANCEL_AND_RESUME,
    _UNVERIFIED_SESSION,
    _HOSTILE_SESSION,
    _LONG_SESSION,
    _PHRASE_CORPUS_SCENARIO,
)


#: Defects this harness FOUND, recorded rather than asserted away.
#:
#: The gate passes *because these are registered*, not because they are fixed.
#: Each row is a surface a person would complain about, measured on this tree,
#: with the file that owns it and what a fix looks like. The test pins the
#: register to the OBSERVED codes, so the day somebody fixes one the gate goes
#: red and says which row to delete -- an unregistered gap is one nobody is
#: looking at, and an over-broad register is a shrug with a table.
@dataclass(frozen=True)
class SurfaceGap:
    """One surface defect, observed, attributed, and still open."""

    code: str
    slug: str
    symptom: str
    why_it_matters: str
    owner: str
    fix: str


REGISTERED_SURFACE_GAPS: Tuple[SurfaceGap, ...] = (
    SurfaceGap(
        code="resume_produced_no_visible_output",
        slug=_CANCEL_AND_RESUME.slug,
        symptom=(
            "`/resume <id>` on a run whose completion card is already on screen "
            "grows the transcript by ZERO characters: no line, no card, no "
            "acknowledgement. The resumed worker's result goes through "
            "`NeoApp._note_result`, which is presentation-only and writes "
            "nothing, and `_finish_run` -> `_render_card` returns early because "
            "the task id is already in `_completion_rendered`."
        ),
        why_it_matters=(
            "A person who types `/resume` and sees nothing cannot tell a "
            "finished resume from a dropped keystroke, and a resume is exactly "
            "the control someone reaches for when a run went wrong. Silence on "
            "the recovery control is the worst place for silence."
        ),
        owner="cli/tui.py (`_resume_worker` / `_note_result` / `_finish_run`)",
        fix=(
            "Have the resume path publish its OWN line -- a "
            "`resumed <task-id>` transcript row before the worker starts, plus a "
            "terminal card whose task id is distinguishable from the cancelled "
            "one, or drop the id from `_completion_rendered` when a resume "
            "begins. `AGENT-09` (staged undo) already threads a `resumed` flag "
            "through the same surfaces, so the vocabulary exists."
        ),
    ),
    SurfaceGap(
        code="rail_calls_a_mapped_row_unreadable",
        slug=_CANCEL_AND_RESUME.slug,
        symptom=(
            "The plan rail publishes `journal 1 unreadable event(s): plan` for "
            "a journal row the transcript feed renders happily as "
            "'planned 1 sub-step(s)'. `cli/tracelog.FeedBuilder` maps `plan`; "
            "`cli/runview.RunProjection`'s `EVENT_VOCABULARY` does not."
        ),
        why_it_matters=(
            "Two projections of the SAME journal row disagree about whether the "
            "product understood it, and the one the person reads second says the "
            "product did not. `cli/runview.py:3302` calls naming it 'better "
            "than reporting a clean-looking run we only partially understood' -- "
            "which is true, and also wrong here, because the row WAS understood."
        ),
        owner="cli/runview.py (`EVENT_VOCABULARY`)",
        fix=(
            "Add `plan` (and audit the rest of `cli/tracelog.FeedBuilder`'s "
            "`_on_*` handlers against `EVENT_VOCABULARY`) so the rail and the "
            "feed agree. This is the same one-line class AGT-02, AGT-10, AGT-11 "
            "and VEX-PF-10 each filed for a different kind."
        ),
    ),
)

#: The scenario each registered gap was observed on, so a test can require the
#: register and the observation to stay in step.
REGISTERED_GAP_CODES: Tuple[str, ...] = tuple(
    gap.code for gap in REGISTERED_SURFACE_GAPS
)

#: The scenarios a fast lane may select. The corpus is deliberately NOT among
#: them: the corpus is the intent router's net, and a fast lane that skipped it
#: would skip the net.
QUICK_SLUGS: Tuple[str, ...] = (
    _DAILY_SESSION.slug,
    _CANCEL_AND_RESUME.slug,
    _UNVERIFIED_SESSION.slug,
    _HOSTILE_SESSION.slug,
)


def scenario_map() -> Dict[str, JourneyScenario]:
    """Every scenario keyed by slug. The one lookup a caller needs."""
    return {scenario.slug: scenario for scenario in SCENARIOS}


def routed_steps() -> Tuple[Tuple[Phrase, int], ...]:
    """The corpus phrase paired with its step index in the corpus scenario.

    A test asserts each phrase's MEASURED routing against the dispatch the
    shell actually performed for the step that typed it, which is what makes
    the corpus a net over the router rather than over itself.
    """
    scenario = scenario_map()[_PHRASE_CORPUS_SCENARIO.slug]
    if len(scenario.steps) != len(PHRASE_CORPUS):  # pragma: no cover - a build error
        raise AssertionError(
            f"the corpus scenario types {len(scenario.steps)} lines for "
            f"{len(PHRASE_CORPUS)} phrases; the pairing would silently mis-align"
        )
    return tuple((phrase, offset + 1) for offset, phrase in enumerate(PHRASE_CORPUS))


# ---------------------------------------------------------------------------
# 3. What "visible" means here -- the closed vocabularies
# ---------------------------------------------------------------------------

#: Raw journal event names. None of these may reach a person: they are the
#: product's internal transport vocabulary, and a surface that prints one is
#: printing its own plumbing. `task_start`/`task_end`/`result` are absent on
#: purpose -- the shell legitimately renders the SENTENCES built from them.
FORBIDDEN_VISIBLE: Tuple[str, ...] = (
    "model_delta",
    "model_request",
    "model_response",
    "tool_call",
    "tool_result",
    "tool_error",
    "run_started",
    "run_finished",
    "attempt_start",
    "attempt_resume",
    "step_end",
    "step_skipped_resume",
    "plan_parse_error",
    "baseline_verify",
    "final_verify",
    "agent_tests_verify",
    "verification_rung",
    "steering_replan",
    "steering_abort",
    "steering_batch_boundary",
    "steering_queue",
    "loop_detected",
    "loop_guard",
    "model_recovery",
    "tool_recovery",
    "policy_refused",
    "command_recovery",
    "context_compacted",
    "turn_started",
    "unknown event",
)

#: The one DECLARED place a raw event name may appear, in each of the two
#: forms the product renders it:
#:
#: 1. `cli.tracelog.FeedBuilder._unknown_entry` (tracelog.py:677) prints
#:    `unknown event: <kind>` ONCE per unrecognised kind and then counts. Its
#:    docstring and the pinned wording in `tests/test_cli_tracelog.py` make
#:    the disclosure a contract: a producer that is not wired to the feed must
#:    not look identical to one that emits nothing.
#: 2. `cli/tui.NeoApp._render_side` (tui.py:3311) prints
#:    `journal N unreadable event(s): <names>` for the same reason.
#:
#: Both are carved out BEFORE the scan, and the carve-out is deliberately
#: narrow: it matches the whole declared sentence and nothing else. A wider
#: exemption is a place for the next defect to hide.
DECLARED_RECEIPTS: Tuple[Any, ...] = (
    re.compile(r"unknown event: [^\s\n]+", re.IGNORECASE),
    re.compile(r"\(\s*incomplete row", re.IGNORECASE),
    re.compile(r"journal \d+ unreadable event\(s\):[^\n]*", re.IGNORECASE),
)

#: Shapes a LIBRARY leaves behind when it raises through a UI. Any of these on
#: screen is a traceback a person was shown.
#:
#: `"closing tag"` was in this list and was REMOVED by the gate firing on
#: itself: the corpus contains the utterance `[/] unbalanced closing tag`, and
#: rich's error text is `closing tag '[/]' at position N has nothing to close`.
#: A marker that a person can TYPE is a marker that cries wolf, and a gate that
#: cries wolf is a gate people stop reading. The error is still caught by
#: `MarkupError` and by the unambiguous `has nothing to close`.
FORBIDDEN_TRACEBACK: Tuple[str, ...] = (
    "Traceback (most recent call last)",
    '  File "',
    "MarkupError",
    "has nothing to close",
    "auto closing tag",
    "NoMatches",
    "textual.app",
    "textual.css.errors",
    "textual.widgets",
    "rich.errors",
    "AttributeError:",
    "TypeError:",
    "KeyError:",
    "IndexError:",
    "ValueError:",
    "UnboundLocalError:",
    "RecursionError",
    "StaleWorker",
    "asyncio.exceptions",
)

#: The alternative order matters. Python's alternation is LEFTMOST-FIRST, so
#: putting UNVERIFIED first is what stops the shorter word matching inside the
#: longer one -- and that is why this is a regex and not an `in`.
_VERDICT_RE = re.compile(r"UNVERIFIED|VERIFIED|SUCCESS|PASSED|FAILED")

#: Verdict words that may NOT be the last one a reader's eye lands on when the
#: run was not verified. The LAST mention is the right assertion rather than
#: the only one, because the scrollback is the run's history and a verified
#: run earlier in the session legitimately appears above a later unverified
#: one. This is the same reading-order rule the terminal-UX round measured.
_TAIL_FORBIDDEN: Tuple[str, ...] = ("VERIFIED", "SUCCESS")


def verdict_mentions(text: str) -> Tuple[str, ...]:
    """Every verdict word in `text`, in reading order.

    Assumes `text` is the plain text a person sees with markup resolved. Never
    raises; text it cannot read yields `()`, which a caller must read as "no
    verdict was said" rather than "all clear".
    """
    try:
        return tuple(match.group(0) for match in _VERDICT_RE.finditer(str(text or "")))
    except Exception:
        return ()


def last_verdict(text: str) -> str:
    """The verdict word a reader's eye lands on LAST, or `""` for none."""
    mentions = verdict_mentions(text)
    return mentions[-1] if mentions else ""


def unverified_tail_is_honest(text: str) -> bool:
    """Whether the last verdict in `text` is an unverified or failed one.

    True for text that never says a verdict, because a run that has not
    finished has not earned one. False the moment the last word a reader would
    land on is `VERIFIED` or `SUCCESS`.
    """
    tail = last_verdict(text).upper()
    return tail in ("", "UNVERIFIED", "FAILED")


def _bounded(text: str, *, limit: int = MAX_CAPTURE_CHARS) -> Tuple[str, bool]:
    """Bound a captured surface and REPORT that the bound bit."""
    value = str(text or "")
    if len(value) <= limit:
        return value, False
    return value[:limit], True


#: An absolute path, on either platform. Used only to make a captured receipt
#: REPRODUCIBLE: the shell's wordmark prints the repository and log roots, so
#: two runs of the same tree differ by a random temp directory name. A
#: receipt that differs between two identical runs cannot be diffed, and a
#: diff nobody can read is not a visual-regression gate.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)(?:[^\s:;\"'<>|]+[\\/])*[^\s:;\"'<>|]*"
)


def normalise_paths(text: str, root: Optional[Path] = None) -> str:
    """Replace the run's own root -- and any other absolute path -- with a label.

    Assumes nothing about `text`. Never raises.

    Two replacements, in this order, and the order is the whole thing. The
    shell's wordmark PRINTS the repository and log roots but ELIDES the middle
    of them, so the full root string is not present in the captured text; only
    the leaf is. The leaf is also the part that carries a random token
    (`mkdtemp` names the directory after the scenario and a random suffix), so
    replacing the leaf FIRST is what makes two runs of the same tree produce
    the same bytes. Replacing only the full path would leave the random token
    in and the receipt would differ every run -- a receipt that differs between
    two identical runs cannot be diffed.

    Anything else absolute becomes `.../<two last segments>`, which keeps the
    file names a reader needs and drops the host layout they do not. Declared
    in the manifest as `paths_normalised`, because a receipt nobody knows was
    normalised is a receipt nobody trusts.
    """
    value = str(text or "")
    if root is not None:
        replacements = [
            str(root),
            str(root).replace("\\", "/"),
            str(root).replace("/", "\\"),
        ]
        # The leaf FIRST: the wordmark elides the middle of a path, so the full
        # string is absent while the random leaf is present.
        replacements.insert(0, str(root.name))
        for spelling in replacements:
            if spelling:
                value = value.replace(spelling, "<root>")
    try:
        return _ABSOLUTE_PATH_RE.sub(
            lambda match: (
                ".../"
                + "/".join(match.group(0).replace("\\", "/").strip("/").split("/")[-2:])
            ),
            value,
        )
    except Exception:  # pragma: no cover - the regex is a module constant
        return value


# ---------------------------------------------------------------------------
# 4. Reading what the shell actually showed
# ---------------------------------------------------------------------------


def transcript_plain(app: Any) -> str:
    """The `#neo-body` RichLog's plain text, segments concatenated in order.

    Assumes the app is mounted. A widget that is missing or unreadable yields
    `""` rather than raising, so a render failure cannot turn a finding in the
    evidence driver into a crash.
    """
    try:
        from rich.text import Text

        out = Text()
        for line in app.query_one("#neo-body").lines:
            for segment in line._segments:
                out.append(segment.text, style=segment.style)
        return out.plain
    except Exception:
        return ""


def transcript_rows(app: Any) -> int:
    """How many rows the transcript RichLog is holding."""
    try:
        return len(app.query_one("#neo-body").lines)
    except Exception:
        return 0


def frame_rows(app: Any) -> List[str]:
    """The RENDERED screen, one string per terminal row.

    The compositor is the only place a layout claim can be checked for real.
    `widget.visual` proves what a widget CONTAINS and says nothing about
    whether the row was ON SCREEN -- which is the whole difference between
    "rendered" and "visible".
    """
    try:
        return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]
    except Exception:
        return []


def frame_text(app: Any) -> str:
    """`frame_rows` joined: the text a terminal would show."""
    return "\n".join(frame_rows(app))


def surface_text(app: Any, widget_id: str) -> str:
    """One mounted surface's plain text, through the product's own reader."""
    try:
        from cli import tui_components as components

        return components.surface_text(app, widget_id)
    except Exception:
        return ""


def strip_declared_receipts(text: str) -> str:
    """Remove the product's DECLARED honesty receipts from a captured surface.

    Assumes nothing about the surface beyond it being text. Never raises; a
    value it cannot read yields `""`, which a caller reads as "nothing to scan"
    rather than "nothing wrong".
    """
    value = str(text or "")
    try:
        for pattern in DECLARED_RECEIPTS:
            value = pattern.sub(" ", value)
    except Exception:  # pragma: no cover - the patterns are module constants
        return ""
    return value


def visible_findings(text: str, *, label: str) -> List[Dict[str, str]]:
    """Findings about ONE captured surface. Pure; never raises.

    The declared honesty receipts are removed first, so a rail that says "3
    unreadable event(s): verification_rung" and a feed that says "unknown
    event: verify_skipped" are product behaviour rather than leaked names,
    while any OTHER occurrence of a transport name is a finding.
    """
    findings: List[Dict[str, str]] = []
    for name in FORBIDDEN_TRACEBACK:
        if name in text:
            findings.append(
                {
                    "code": "library_traceback_visible",
                    "detail": f"{label}: a library shape reached the screen: {name}",
                }
            )
            break
    scannable = strip_declared_receipts(text)
    for name in FORBIDDEN_VISIBLE:
        if name in scannable:
            findings.append(
                {
                    "code": "raw_event_name_visible",
                    "detail": f"{label}: the internal event name {name!r} reached the screen",
                }
            )
    return findings


def group_notes(notes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Group findings by code, applying the product's OWN anti-clutter rule.

    A section with two or fewer entries is not rendered at all. Reading the
    threshold from `cli.design` rather than restating it is the point: this
    document is a panel too, and a rule that lives in one place is a rule the
    panels cannot disagree about.
    """
    try:
        from cli import design

        threshold = int(design.ANTI_CLUTTER_MIN_ENTRIES)
        rendered = design.section_is_rendered
    except Exception:  # pragma: no cover - design is a hard dependency
        threshold = 3

        def rendered(count: Any) -> bool:  # type: ignore[misc]
            return int(count) >= 3

    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for note in notes:
        grouped.setdefault(str(note.get("code") or "unknown"), []).append(note)
    out: List[Dict[str, Any]] = []
    for code in sorted(grouped):
        rows = grouped[code]
        if not rendered(len(rows)):
            continue
        out.append(
            {
                "code": code,
                "count": len(rows),
                "min_entries": threshold,
                "first": str(rows[0].get("detail") or "")[:400],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 5. The driver -- the real app, the real composer, the real compositor
# ---------------------------------------------------------------------------


@dataclass
class _Backend:
    """The fake session backends the driver installs.

    One object, so the driver can ask it afterwards exactly what the shell
    dispatched. The list GROWS only when something dispatched, so a step's
    kind is the SLICE between the counts before and after it -- indexing the
    list by step number would report a later step's kind for every greeting,
    which is the kind of wrong answer that makes a routing net worthless.
    """

    dispatched: List[Dict[str, str]] = field(default_factory=list)
    released: threading.Event = field(default_factory=threading.Event)
    #: When set, the fake agent backend parks until `released` or a
    #: KeyboardInterrupt. That is what makes a cancel real.
    park: bool = False
    parked: threading.Event = field(default_factory=threading.Event)

    def record(self, kind: str, payload: str) -> None:
        self.dispatched.append({"kind": kind, "payload": payload})

    def mark(self) -> int:
        """The current dispatch count, to be taken before a step."""
        return len(self.dispatched)

    def since(self, mark: int) -> List[str]:
        """The kinds dispatched since `mark`."""
        return [str(row.get("kind") or "") for row in self.dispatched[mark:]]

    @staticmethod
    def primary(kinds: Sequence[str]) -> str:
        """One word for a step's dispatch: the kind, or `""` for none.

        `multiple` rather than the first kind when a step dispatched more than
        one thing, because a step that launched two runs is a different fact
        from a step that launched one.
        """
        if not kinds:
            return ""
        return kinds[0] if len(kinds) == 1 else "multiple"


def _write_journal(
    log_root: Path, task_id: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Append journal rows the way the harness writes them.

    `sequence` is allocated here so the file is a contiguous journal and the
    shell's own `EventCursor` accepts it. A caller is expected to hand over
    rows already in terminal order; the `run_finished` row is appended by
    `_events_for`, never by a scenario, so it is always last.
    """
    task_dir = Path(log_root) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    with (task_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
        for index, row in enumerate(rows, start=1):
            record = {
                "sequence": index,
                "ts": time.time(),
                "kind": str(row.get("kind") or "info"),
                "data": dict(row.get("data") or {}),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def _events_for(state: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The journal rows a fake backend should write for the current turn.

    The step's own `events` ride on the session state, which is how the driver
    hands a scenario's scripted journal to a backend that only receives the
    ordinary session arguments. The terminal row is appended HERE rather than
    in a scenario, so it is always last -- and only when the step asks for one,
    because a cancelled run has no terminal row to publish.
    """
    rows: List[Mapping[str, Any]] = [
        dict(row) for row in (state.get("_journey_events") or ())
    ]
    if state.get("_journey_terminal") is False:
        return rows
    status = str(state.get("_journey_status") or "") or "completed_unverified"
    rows.append({"kind": "run_finished", "data": {"status": status, "attempts": 1}})
    return rows


def _events_status(rows: Sequence[Mapping[str, Any]]) -> str:
    for row in reversed(list(rows)):
        if str(row.get("kind")) in ("run_finished", "result"):
            status = str((row.get("data") or {}).get("status") or "")
            if status:
                return status
    return ""


def _events_diff(rows: Sequence[Mapping[str, Any]]) -> str:
    for row in reversed(list(rows)):
        if str(row.get("kind")) == "result":
            return str((row.get("data") or {}).get("diff") or "")
    return ""


def _events_files(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    files: List[str] = []
    for row in rows:
        if str(row.get("kind")) not in ("tool_call", "tool_result", "edit_applied"):
            continue
        data = row.get("data") or {}
        for key in ("path", "file"):
            value = str(data.get(key) or "")
            if value and value not in files:
                files.append(value)
    return files


def _make_repo(root: Path, *, hostile: bool) -> Path:
    """A repository for the shell to run against.

    With `hostile=True` this is deliberately NOT a fixture: the name carries a
    bracket (`weird[name].py`), because nothing under `tests/fixtures/` does
    and a fixture's safe names are the reason this defect class survived.
    """
    repo = Path(root) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    files = {
        "mathutil.py": "def mean(values):\n    return sum(values)\n",
        "auth.py": "def check_token(token):\n    return bool(token)\n",
        "tests/test_mathutil.py": (
            "from mathutil import mean\n\n\ndef test_mean():\n"
            "    assert mean([1, 2, 3]) == 2\n"
        ),
    }
    if hostile:
        files["weird[name].py"] = "def parse(text):\n    return text.split()\n"
    for name, body in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    return repo


def _scripted_model(messages: Any, **kwargs: Any) -> str:
    """The model boundary, installed through `harness.deps.set_call_model`.

    It ECHOES the deterministic tier's own verdict back as strict JSON. That
    is the honest way to make a corpus deterministic: the deterministic tier
    is the part a test can own, and an echo keeps the second tier from
    becoming a second, unpinned router. The gray-zone tier's own behaviour --
    including its fail-closed degradation to `chit_chat` -- is covered by
    `tests/test_agent_loop.py`, which is not this round's file.
    """
    from harness.agent_loop import classify_deterministic

    text = ""
    for message in list(messages or [])[::-1]:
        content = str((message or {}).get("content") or "")
        if "Reply with the JSON verdict now" in content:
            text = content.split("Message:", 1)[-1]
            text = text.split("Reply with the JSON verdict now.", 1)[0]
            break
    kind = classify_deterministic(text.strip()).kind
    return json.dumps({"kind": kind, "reason": "session_journey scripted echo"})


class _Shell:
    """One mounted `NeoApp` with scripted backends installed.

    Owns the global-state discipline this repository's TUI suites share: the
    `cli.interactive` module attributes, the live-run registration and the
    process env are all process-global and four terminals edit this tree at
    once. Everything is restored on exit, and a restore that FAILS is reported
    in `restore_failures` rather than swallowed. The field is named for what
    it HOLDS -- failures -- because a receipt reading
    `global_state_restore_failures: []` is unambiguous, while the earlier
    `global_state_restored: []` invited a reader to take an empty list as a
    list of things restored rather than a list of things that failed to be.
    """

    ENV_NAMES: Tuple[str, ...] = (
        "NO_COLOR",
        "NEO_NO_COLOR",
        "NEO_HOME",
        "NEO_NOTIFY",
        "NEO_GLOBAL_ROOT",
        "NEO_PLUGINS_DIR",
    )

    def __init__(self, root: Path, *, viewport: Tuple[int, int], hostile: bool) -> None:
        self.root = Path(root)
        self.viewport = viewport
        self.hostile = hostile
        self.backend = _Backend()
        self.app: Any = None
        self.restore_failures: List[str] = []
        self._patches: List[Tuple[Any, str, Any]] = []
        self._env_backup: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "_Shell":
        import cli.interactive as interactive
        import harness.deps as hdeps
        from cli import tui as tui_module

        self._env_backup = {name: os.environ.get(name) for name in self.ENV_NAMES}
        # A receipt is captured WITH colour so the SVG shows the real
        # composition; `NO_COLOR` would strip it and the receipt would be of a
        # terminal nobody uses.
        for name in ("NO_COLOR", "NEO_NO_COLOR", "NEO_NOTIFY"):
            os.environ.pop(name, None)

        repo = _make_repo(self.root, hostile=self.hostile)
        log_root = self.root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        self.repo = repo
        self.log_root = log_root
        os.environ["NEO_HOME"] = str(self.root / "neo-home")

        hdeps.set_call_model(_scripted_model)

        def fake_agent(
            request: str,
            repo: Any,
            state: Any,
            log_root: Any,
            file_config: Any = None,
            **kwargs: Any,
        ) -> Dict[str, Any]:
            self.backend.record("agent_task", str(request))
            task_id = f"journey-agent-{len(self.backend.dispatched):03d}"
            interactive._fire_task_start(task_id)
            interactive._set_live_run(task_id, log_root)
            try:
                rows = _events_for(state or {})
                _write_journal(Path(log_root), task_id, rows)
                if self.backend.park:
                    self.backend.parked.set()
                    self._park()
                    return {
                        "task_id": task_id,
                        "log_root": str(log_root),
                        "status": "cancelled",
                    }
                status = _events_status(rows) or "completed_unverified"
                return {
                    "task_id": task_id,
                    "log_root": str(log_root),
                    "status": status,
                    "diff": _events_diff(rows),
                    "error": "model unavailable" if status == "failed" else "",
                    "cost_usd": 0.0012,
                    "model_calls": [
                        {"model": "scripted", "tokens": 120, "cost": 0.0012}
                    ],
                    "files_touched": _events_files(rows),
                }
            finally:
                interactive._clear_live_run()

        def fake_question(
            question: str,
            repo: Any,
            state: Any,
            log_root: Any,
            file_config: Any = None,
            **kwargs: Any,
        ) -> Dict[str, Any]:
            self.backend.record("question", str(question))
            task_id = f"journey-question-{len(self.backend.dispatched):03d}"
            interactive._fire_task_start(task_id)
            try:
                _write_journal(
                    Path(log_root),
                    task_id,
                    [
                        {
                            "kind": "run_finished",
                            "data": {"status": "completed_unverified"},
                        }
                    ],
                )
                return {
                    "task_id": task_id,
                    "log_root": str(log_root),
                    "status": "completed_unverified",
                    "answer": "mean() divides the sum by the number of values.",
                }
            finally:
                interactive._clear_live_run()

        def fake_resume(
            task_id: str, log_root: Any, state: Any = None, **kwargs: Any
        ) -> Any:
            self.backend.record("resume", str(task_id))
            return (
                {
                    "task_id": str(task_id),
                    "log_root": str(log_root),
                    "status": "completed_verified",
                    "diff": "--- a/auth.py\n+++ b/auth.py\n@@\n+def check_token(t):\n",
                },
                ["resumed"],
            )

        for name, fn in (
            ("_run_one_agent", fake_agent),
            ("_run_one_fix", fake_agent),
            ("_run_one_build", fake_agent),
            ("_run_one_question", fake_question),
            ("_run_one_research", fake_question),
            ("_resume_task", fake_resume),
        ):
            self._patch(interactive, name, fn)

        self.app = tui_module.NeoApp(
            repo=repo,
            log_root=log_root,
            state={
                "repo": str(repo),
                "file_config": {},
                "quiet": False,
                "model": "scripted-model",
            },
            file_config={},
            version="0.0.0-journey",
        )
        return self

    def _park(self) -> None:
        """Park until released, or until the product interrupts the worker.

        POLLS with `time.sleep` rather than `Event.wait(timeout)`: a C-level
        timed acquire ignores a pending async exception until it expires, which
        is the product's own note on this exact problem and exactly what would
        make the cancel flaky.
        """
        deadline = time.monotonic() + 30.0
        while not self.backend.released.is_set() and time.monotonic() < deadline:
            time.sleep(POLL_S)

    def __exit__(self, *exc: Any) -> bool:
        import cli.interactive as interactive
        import harness.deps as hdeps

        for holder, name, original in self._patches:
            try:
                setattr(holder, name, original)
            except Exception as restore_exc:  # pragma: no cover - a real failure
                self.restore_failures.append(
                    f"{getattr(holder, '__name__', holder)}.{name}: {restore_exc}"
                )
        self._patches = []
        try:
            hdeps.reset_overrides()
        except Exception as deps_exc:  # pragma: no cover
            self.restore_failures.append(f"harness.deps.reset_overrides: {deps_exc}")
        try:
            interactive._clear_live_run()
        except Exception as live_exc:  # pragma: no cover
            self.restore_failures.append(f"cli.interactive._clear_live_run: {live_exc}")
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False

    def _patch(self, holder: Any, name: str, value: Any) -> None:
        self._patches.append((holder, name, getattr(holder, name)))
        setattr(holder, name, value)


async def _type_line(
    shell: _Shell,
    pilot: Any,
    line: str,
    *,
    timeout_s: float,
    wait_worker: bool = True,
) -> bool:
    """Type one line into the real composer and let the real dispatch run.

    Goes through the widget and the `enter` key, NOT through
    `NeoApp._handle_line`, so the submit echo, the history write and the whole
    command path are the product's own.

    `wait_worker=False` is for a step that is MEANT to leave a worker alive --
    the cancel scenario. Waiting there would report a `False` that means
    "the run is still going", which is the point, as a defect. Returns whether
    the worker finished inside the bound; a `False` is a REPORTED number, not
    a silent pass.
    """
    from textual.widgets import Input

    app = shell.app
    app.query_one("#neo-input", Input).value = line
    await pilot.press("enter")
    settled = await _wait_worker(app, timeout_s, pilot) if wait_worker else False
    await pilot.pause()
    return settled


async def _wait_worker(app: Any, timeout_s: float, pilot: Any) -> bool:
    """Wait for the app's worker thread, yielding to the loop between polls."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        worker = getattr(app, "_worker_thread", None)
        if worker is None or not worker.is_alive():
            return True
        await asyncio.sleep(POLL_S)
        await pilot.pause()
    return False


async def _wait_event(event: threading.Event, timeout_s: float, pilot: Any) -> bool:
    """Wait on a threading.Event from the UI thread, yielding between polls."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not event.is_set():
        await asyncio.sleep(POLL_S)
        await pilot.pause()
    return event.is_set()


def _last_task_id(app: Any) -> str:
    run = getattr(app, "_run", None)
    if run is not None and getattr(run, "task_id", ""):
        return str(run.task_id)
    return ""


#: Box-drawing runs and card borders. The completion card lays itself out in
#: COLUMNS whose widths depend on how wide the content region was when the
#: card was measured -- which moves with the rail's teardown -- so the same
#: run draws a different rule length and wraps the task id at a different
#: column in two runs of the same tree. That is chrome and layout, not
#: content, so the delta strips it. The verdict words are text, not box
#: drawing, and they survive; this runs AFTER the verdict extraction.
_BOX_CHROME_RE = re.compile(r"[─-╿╱╲╳]+")


def normalise_box_chrome(text: str) -> str:
    """Drop box-drawing rules and the card's column separators.

    Assumes nothing; never raises. Strips `│` as well as rules, because in the
    card the border and the field separators are the same character and both
    are chrome -- the words between them are the content.
    """
    try:
        return _BOX_CHROME_RE.sub(" ", str(text or "")).replace("│", " ")
    except Exception:  # pragma: no cover - the regex is a module constant
        return str(text or "")


def _repair_wrap_spaces(delta: str, frame: str) -> str:
    """Restore spaces the segment stream lost at a WRAP boundary, using the frame.

    A `RichLog`'s stored segment stream can drop a space at a wrap boundary:
    the text reads `change codein this repo` where the terminal shows
    `change code in this repo`. The FRAME is the rendered truth -- it is what a
    person saw -- so the frame is consulted, not guessed at.

    Only ever ADDS a space, and only when the frame proves the split exists:
    for each token the frame does not contain, a single split point is tried,
    and it is accepted only if BOTH halves are tokens the frame does contain.
    A token with no frame-backed split is left alone, so a genuinely new word
    is never mangled and a content change is still reported.
    """
    if not delta or not frame:
        return delta
    frame_tokens = set(normalise_box_chrome(frame).split())
    if not frame_tokens:
        return delta
    repaired: List[str] = []
    for token in delta.split(" "):
        if token in frame_tokens or len(token) < 4:
            repaired.append(token)
            continue
        for cut in range(2, len(token) - 1):
            head, tail = token[:cut], token[cut:]
            if head in frame_tokens and tail in frame_tokens:
                token = f"{head} {tail}"
                break
        repaired.append(token)
    return " ".join(repaired)


def _step_delta(before: str, after: str, frame: str = "") -> str:
    """What one step ADDED to the transcript, whitespace-normalised.

    Assumes the transcript only ever appends, which is what a `RichLog` does.
    When it does not -- the transcript hit its own cap and the window slid --
    the whole after-text is returned, and the caller can tell the difference
    because `grew` is still the length comparison. A diff that silently
    reported a sliding window as a step's output would be a gate measuring the
    wrong thing.

    Whitespace runs are collapsed to one space and the result is stripped. The
    reason is a capture artefact, not a product change: a `RichLog`'s stored
    segment stream can lose a space at a wrap boundary that the rendered frame
    keeps, so an un-normalised delta reported a difference between two runs of
    the same tree that no person could have seen.
    """
    raw = after[len(before) :] if after.startswith(before) else after
    return " ".join(_repair_wrap_spaces(normalise_box_chrome(raw), frame).split())


async def _settle(pilot: Any) -> None:
    """Let the shell's own repaint timers and journal tail catch up.

    Bounded and short. The point is not to make the run slow; it is to capture
    the frame a person would see a moment after the step, rather than the one
    the scheduler happened to be in when the step returned.
    """
    await asyncio.sleep(FRAME_SETTLE_S)
    await pilot.pause()
    await pilot.pause()


def _step_row(
    scenario: JourneyScenario,
    step: JourneyStep,
    index: int,
    app: Any,
    dispatched: Sequence[str],
    *,
    elapsed_ms: float,
    note: str = "",
    grew: bool = True,
    root: Optional[Path] = None,
    delta: str = "",
) -> Dict[str, Any]:
    """One step's MEASURED receipt, asserted on what the screen showed.

    `dispatched` is the slice of dispatches this step caused, not a lookup by
    step number -- a greeting dispatches nothing, and a lookup would hand it a
    later step's answer.

    `grew` is whether the transcript gained characters across this step. A step
    that dispatched work and showed nothing is recorded as a REGISTERED gap
    rather than a hard finding when the register already names it, and as a
    finding when it does not -- so a new silence cannot hide behind an old
    registration.
    """
    text, truncated = _bounded(transcript_plain(app))
    frame = _bounded(frame_text(app))[0]
    text = normalise_paths(text, root)
    frame = normalise_paths(frame, root)
    findings = visible_findings(text, label=f"{scenario.slug}#{index} transcript")
    findings += visible_findings(frame, label=f"{scenario.slug}#{index} frame")
    if step.expect == "routed" and not text.strip():
        findings.append(
            {
                "code": "no_visible_outcome",
                "detail": f"{scenario.slug}#{index}: nothing at all was shown for a typed line",
            }
        )
    if step.expect == "resume" and not grew:
        findings.append(
            {
                "code": "resume_produced_no_visible_output",
                "detail": (
                    f"{scenario.slug}#{index}: /resume dispatched a run and the transcript "
                    "grew by zero characters"
                ),
            }
        )
    surfaces = {
        "runline": normalise_paths(surface_text(app, "neo-runline"), root),
        "announce": normalise_paths(surface_text(app, "neo-announce"), root),
        "statusline": normalise_paths(surface_text(app, "neo-statusline"), root),
    }
    for name, value in surfaces.items():
        findings += visible_findings(value, label=f"{scenario.slug}#{index} {name}")
    return {
        "index": index,
        "label": step.label,
        "line": step.line,
        "expect": step.expect,
        "diffable": step.diffable,
        "status": str(getattr(app, "_status", "")),
        "dispatched": _Backend.primary(dispatched),
        "dispatched_kinds": list(dispatched),
        "grew": grew,
        "rows": len(frame_rows(app)),
        "transcript_rows": transcript_rows(app),
        "transcript_chars": len(text),
        "truncated": truncated,
        "elapsed_ms": round(elapsed_ms, 1),
        "note": note,
        "surfaces": surfaces,
        "verdicts": list(verdict_mentions(text)),
        "last_verdict": last_verdict(text),
        "delta": delta[-STEP_DELTA_CHARS:],
        "delta_chars": len(delta),
        "transcript": text[-STEP_TRANSCRIPT_TAIL_CHARS:],
        "findings": findings,
    }


async def _drive(scenario: JourneyScenario) -> Dict[str, Any]:
    """Mount the real shell once and replay one scenario through it."""
    shell = _Shell(
        Path(tempfile.mkdtemp(prefix=f"neo-journey-{scenario.slug}-")),
        viewport=scenario.viewport,
        hostile=scenario.hostile_repo,
    )
    transcript: Dict[str, Any] = {
        "schema_version": 1,
        "slug": scenario.slug,
        "title": scenario.title,
        "viewport": list(scenario.viewport),
        "hostile_repo": scenario.hostile_repo,
        "steps": [],
        "findings": [],
        "registered": [],
    }
    started = time.perf_counter()
    cancel_scenario = scenario.slug == _CANCEL_AND_RESUME.slug
    root = shell.root
    with shell:
        app = shell.app
        app.state["_journey_events"] = []
        app.state["_journey_status"] = ""
        # The control marker is typed FIRST, as its own line, so it cannot
        # change the intent of the first real phrase. A render failure that
        # DELETES a message is invisible to every other assertion here, so
        # this is the one that can see it.
        marker = f"MARKER-{scenario.slug}-must-survive"
        async with app.run_test(size=scenario.viewport) as pilot:
            await pilot.pause()
            shell.backend.park = cancel_scenario
            await _type_line(shell, pilot, marker, timeout_s=SETTLE_TIMEOUT_S)

            for index, step in enumerate(scenario.steps, start=1):
                step_started = time.perf_counter()
                app.state["_journey_events"] = list(step.events)
                app.state["_journey_status"] = step.status
                app.state["_journey_terminal"] = step.writes_terminal_row
                note = ""
                settled = True
                mark = shell.backend.mark()
                before_raw = transcript_plain(app)
                before_chars = len(before_raw)

                if cancel_scenario and step.expect == "cancel":
                    # The line is typed and the worker is deliberately left
                    # running: this is what makes the cancel a real cancel.
                    await _type_line(
                        shell,
                        pilot,
                        step.line,
                        timeout_s=SETTLE_TIMEOUT_S,
                        wait_worker=False,
                    )
                    parked = await _wait_event(
                        shell.backend.parked, PARK_TIMEOUT_S, pilot
                    )
                    cancel_started = time.perf_counter()
                    app._slash_command("/cancel", "/cancel")
                    settled = await _wait_worker(app, PARK_TIMEOUT_S, pilot)
                    await pilot.pause()
                    note = (
                        f"parked={parked} cancel_to_idle_ms="
                        f"{(time.perf_counter() - cancel_started) * 1000.0:.0f}"
                    )
                    if not parked:
                        transcript["findings"].append(
                            {
                                "code": "worker_never_started",
                                "detail": (
                                    f"{scenario.slug}#{index}: the worker never parked, so the "
                                    "cancel did not cancel anything"
                                ),
                            }
                        )
                    if not settled:
                        transcript["findings"].append(
                            {
                                "code": "cancel_did_not_land",
                                "detail": (
                                    f"{scenario.slug}#{index}: the worker was still alive "
                                    f"{PARK_TIMEOUT_S}s after /cancel"
                                ),
                            }
                        )
                elif cancel_scenario and step.expect == "resume":
                    resume_id = _last_task_id(app) or "journey-agent-001"
                    app._slash_command(f"/resume {resume_id}", f"/resume {resume_id}")
                    settled = await _wait_worker(app, SETTLE_TIMEOUT_S, pilot)
                    await pilot.pause()
                    note = f"resume_id={resume_id}"
                else:
                    settled = await _type_line(
                        shell, pilot, step.line, timeout_s=SETTLE_TIMEOUT_S
                    )

                elapsed_ms = (time.perf_counter() - step_started) * 1000.0
                await _settle(pilot)
                after_raw = transcript_plain(app)
                delta = _step_delta(before_raw, after_raw, frame_text(app))
                row = _step_row(
                    scenario,
                    step,
                    index,
                    app,
                    shell.backend.since(mark),
                    elapsed_ms=elapsed_ms,
                    note=note,
                    grew=len(after_raw) > before_chars,
                    root=root,
                    delta=normalise_paths(delta, root),
                )
                if not settled:
                    row["findings"].append(
                        {
                            "code": "worker_did_not_settle",
                            "detail": (
                                f"{scenario.slug}#{index}: the worker was still alive after "
                                f"{SETTLE_TIMEOUT_S}s"
                            ),
                        }
                    )
                transcript["steps"].append(row)
                transcript["findings"].extend(row["findings"])

            final_text = normalise_paths(transcript_plain(app), root)
            final_frame = normalise_paths(frame_text(app), root)
            rail = normalise_paths(surface_text(app, "neo-side"), root)
            if marker not in final_text:
                transcript["findings"].append(
                    {
                        "code": "message_vanished",
                        "detail": (
                            f"{scenario.slug}: the first message {marker!r} is no longer on "
                            "screen -- a render failure deleted it"
                        ),
                    }
                )
            # The rail's own honesty receipt, read as a MEASUREMENT: a row the
            # rail calls unreadable while the transcript rendered a sentence for
            # it is two projections of one journal disagreeing. It is recorded
            # as a finding here and, where the register already names it, moved
            # to `registered` below.
            if "unreadable event" in rail:
                receipts = [
                    line.strip()
                    for line in rail.splitlines()
                    if "unreadable event" in line
                ]
                transcript["findings"].append(
                    {
                        "code": "rail_calls_a_mapped_row_unreadable",
                        "detail": (
                            f"{scenario.slug}: the rail says "
                            f"{'; '.join(receipts) or 'unreadable event(s)'}"
                        ),
                    }
                )
            transcript["metrics"] = {
                "wall_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "steps": len(scenario.steps) + 1,
                "dispatched": list(shell.backend.dispatched),
                "transcript_chars": len(final_text),
                "transcript_rows": transcript_rows(app),
                "frame_rows": len(frame_rows(app)),
                "status": str(getattr(app, "_status", "")),
                "last_verdict": last_verdict(final_text),
                "unverified_tail_honest": unverified_tail_is_honest(final_text),
                "frame_honest": unverified_tail_is_honest(final_frame),
                "surfaces": {
                    "transcript": _bounded(final_text)[0],
                    "runline": normalise_paths(surface_text(app, "neo-runline"), root),
                    "announce": normalise_paths(
                        surface_text(app, "neo-announce"), root
                    ),
                    "statusline": normalise_paths(
                        surface_text(app, "neo-statusline"), root
                    ),
                    "sidebar": rail,
                    "context": normalise_paths(surface_text(app, "neo-context"), root),
                    "header": normalise_paths(surface_text(app, "neo-header"), root),
                },
            }
            transcript["frame_text"] = _bounded(final_frame)[0]
            # The SVG is normalised for the same reason the text is: the
            # wordmark prints the repository and log roots, so an un-normalised
            # frame differs between two identical runs and its digest says
            # nothing about whether the pixels changed.
            transcript["svg"] = normalise_paths(
                app.export_screenshot(simplify=True), root
            )
            transcript["paths_normalised"] = True
        transcript["global_state_restore_failures"] = list(shell.restore_failures)
    observed = _dedupe(transcript["findings"])
    transcript["findings"] = [
        note for note in observed if note["code"] not in REGISTERED_GAP_CODES
    ]
    transcript["registered"] = [
        note for note in observed if note["code"] in REGISTERED_GAP_CODES
    ]
    transcript["verdict"] = "FINDINGS" if transcript["findings"] else "CLEAN"
    transcript["sections"] = group_notes(transcript["findings"])
    return transcript


def _dedupe(notes: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    seen: Dict[Tuple[str, str], Dict[str, str]] = {}
    for note in notes:
        key = (str(note.get("code")), str(note.get("detail")))
        seen.setdefault(key, {"code": key[0], "detail": key[1]})
    return [seen[key] for key in sorted(seen)]


def run_scenario(scenario: JourneyScenario) -> Dict[str, Any]:
    """Drive one scenario and return its transcript document.

    Blocking on purpose: this is the seam both a test and the CLI call, so the
    corpus's 69 submissions can be one ordinary synchronous test function
    rather than an async one nobody can call from `pytest -q`.
    """
    return asyncio.run(_drive(scenario))


# ---------------------------------------------------------------------------
# 6. Receipts, diffing, and the run document
# ---------------------------------------------------------------------------


def _write_receipts(run_dir: Path, transcript: Mapping[str, Any]) -> Dict[str, Any]:
    """One SVG plus one transcript file per scenario, each with a SHA-256.

    A receipt that is not written down is a claim. Each artifact's digest is
    recorded so a later run can PROVE the frame changed rather than assert
    that it probably did.
    """
    slug = str(transcript.get("slug") or "scenario")
    written: Dict[str, Any] = {}
    svg = str(transcript.get("svg") or "")
    if svg:
        path = run_dir / f"{slug}.svg"
        # BYTES, not text: a text write on Windows translates "\n" to "\r\n",
        # so the digest taken here would not match the file on disk. A receipt
        # that cannot prove what it describes is not evidence.
        payload = svg.encode("utf-8")
        path.write_bytes(payload)
        written["svg"] = {
            "path": path.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "paths_normalised": bool(transcript.get("paths_normalised")),
        }
    body = {
        "schema_version": 1,
        "slug": slug,
        "title": transcript.get("title"),
        "viewport": transcript.get("viewport"),
        "steps": transcript.get("steps"),
        "findings": transcript.get("findings"),
        "registered": transcript.get("registered"),
        "sections": transcript.get("sections"),
        "metrics": transcript.get("metrics"),
        "verdict": transcript.get("verdict"),
        "paths_normalised": transcript.get("paths_normalised"),
    }
    payload = (
        json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        + "\n"
    )
    path = run_dir / f"{slug}.transcript.json"
    path.write_bytes(payload.encode("utf-8"))
    written["transcript"] = {
        "path": path.name,
        "bytes": len(payload.encode("utf-8")),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    return written


def run_journeys(
    out_root: Path,
    *,
    slugs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Drive the selected scenarios and write every receipt under `out_root`.

    Returns the run document, the same shape `diff_runs` consumes, so a later
    run can be compared against this one mechanically.
    """
    wanted = set(slugs or ())
    known = {scenario.slug for scenario in SCENARIOS}
    unknown = sorted(wanted - known)
    scenarios = [
        scenario for scenario in SCENARIOS if not wanted or scenario.slug in wanted
    ]
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid():06d}"
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    transcripts: List[Dict[str, Any]] = []
    for scenario in scenarios:
        transcript = run_scenario(scenario)
        transcripts.append(transcript)

    document: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "neo-session-journey",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "out_root": str(out_root),
        "unknown_slugs": unknown,
        "scenarios": [transcript["slug"] for transcript in transcripts],
        "corpus_size": len(PHRASE_CORPUS),
        "recorded_routing_gaps": sorted(RECORDED_ROUTING_GAPS),
        "recorded_routing_gap_count": len(RECORDED_ROUTING_GAPS),
        "verdict": (
            "ERROR"
            if unknown
            else ("FINDINGS" if any(t["findings"] for t in transcripts) else "CLEAN")
        ),
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "transcripts": transcripts,
    }
    artifacts: Dict[str, Any] = {}
    for transcript in transcripts:
        artifacts[str(transcript["slug"])] = _write_receipts(run_dir, transcript)
    manifest = {
        "schema_version": 1,
        "kind": "neo-session-journey-manifest",
        "run_id": run_id,
        "paths_normalised": True,
        "path_normalisation": (
            "each artifact's absolute paths are replaced with `<root>` for the "
            "run's own directory and `.../<two last segments>` for anything "
            "else, because the shell's wordmark prints the repository and log "
            "roots and two identical runs would otherwise differ by a random "
            "temp directory name"
        ),
        "artifacts": artifacts,
    }
    (run_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    (run_dir / "journey.json").write_bytes(
        (
            json.dumps(
                document, indent=2, sort_keys=True, ensure_ascii=False, default=str
            )
            + "\n"
        ).encode("utf-8")
    )
    return document


def load_run(run_dir: Path) -> Dict[str, Any]:
    """Read a run document back off disk. Raises on an unreadable one."""
    return json.loads((Path(run_dir) / "journey.json").read_text(encoding="utf-8"))


def diff_runs(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    """DIFF two run documents, so a visual regression is a diff and not a hunch.

    Compares three things a person would actually notice: which scenarios ran,
    what each step's visible status and verdict words were, and whether the
    step's transcript text changed. `elapsed_ms` is deliberately NOT compared:
    a wall-clock number is a measurement, not a behaviour, and diffing it would
    make every loaded run a regression.
    """
    before_index = {
        str(t.get("slug")): dict(t) for t in before.get("transcripts") or ()
    }
    after_index = {str(t.get("slug")): dict(t) for t in after.get("transcripts") or ()}
    added = sorted(set(after_index) - set(before_index))
    removed = sorted(set(before_index) - set(after_index))

    changed: List[Dict[str, Any]] = []
    for slug in sorted(set(before_index) & set(after_index)):
        old_steps = {
            int(s.get("index") or 0): dict(s)
            for s in before_index[slug].get("steps") or ()
        }
        new_steps = {
            int(s.get("index") or 0): dict(s)
            for s in after_index[slug].get("steps") or ()
        }
        step_changes: List[Dict[str, Any]] = []
        for key in sorted(set(old_steps) | set(new_steps)):
            old_step, new_step = old_steps.get(key), new_steps.get(key)
            if old_step is None or new_step is None:
                step_changes.append(
                    {
                        "index": key,
                        "change": "added" if old_step is None else "removed",
                        "line": (new_step or old_step or {}).get("line"),
                    }
                )
                continue
            for name in ("line", "expect", "status", "dispatched", "verdicts", "rows"):
                if old_step.get(name) != new_step.get(name):
                    step_changes.append(
                        {
                            "index": key,
                            "change": "field",
                            "field": name,
                            "before": old_step.get(name),
                            "after": new_step.get(name),
                        }
                    )
            if old_step.get("delta") != new_step.get("delta"):
                step_changes.append(
                    {
                        "index": key,
                        "change": "delta",
                        "field": "delta",
                        "skipped_reason": None
                        if bool(new_step.get("diffable", True))
                        else "this step's visible output is a race, not a behaviour",
                    }
                )
            if old_step.get("surfaces") != new_step.get("surfaces"):
                step_changes.append(
                    {"index": key, "change": "surfaces", "field": "surfaces"}
                )
            if old_step.get("grew") != new_step.get("grew"):
                step_changes.append({"index": key, "change": "grew", "field": "grew"})
        old_findings = {
            str(f.get("code")) for f in before_index[slug].get("findings") or ()
        }
        new_findings = {
            str(f.get("code")) for f in after_index[slug].get("findings") or ()
        }
        old_registered = {
            str(f.get("code")) for f in before_index[slug].get("registered") or ()
        }
        new_registered = {
            str(f.get("code")) for f in after_index[slug].get("registered") or ()
        }
        if (
            step_changes
            or old_findings != new_findings
            or old_registered != new_registered
        ):
            changed.append(
                {
                    "slug": slug,
                    "steps": step_changes,
                    "findings_before": sorted(old_findings),
                    "findings_after": sorted(new_findings),
                    "registered_before": sorted(old_registered),
                    "registered_after": sorted(new_registered),
                }
            )

    return {
        "schema_version": 1,
        "kind": "neo-session-journey-diff",
        "before_run_id": before.get("run_id"),
        "after_run_id": after.get("run_id"),
        "scenarios_added": added,
        "scenarios_removed": removed,
        "scenarios_changed": changed,
        "identical": not (added or removed or changed),
        "racy_steps": [
            f"{scenario.slug}#{offset + 1}"
            for scenario in SCENARIOS
            for offset, step in enumerate(scenario.steps)
            if not step.diffable
        ],
        "note": (
            "elapsed_ms is deliberately NOT compared: a wall-clock number is a measurement, "
            "not a behaviour, and diffing it would turn every loaded run into a regression. "
            "`delta` is the per-step visible OUTPUT and is the primary signal; `transcript` is "
            "a sliding 4000-character window kept for a reader, not for the diff. A step listed "
            "in `racy_steps` has a DECLARED non-deterministic output and its delta carries a "
            "`skipped_reason`; see JourneyStep.diffable."
        ),
    }


# ---------------------------------------------------------------------------
# 7. The CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Run the journeys, write the receipts, and report the verdict.

    Exit codes: 0 clean, 2 a finding, 3 the harness could not mount the shell
    at all. `3` is deliberately distinct from `2` -- a harness that cannot
    reach its subject must never be reported as a clean product.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.session_journey",
        description="Drive the real Neo shell through scripted human sessions.",
    )
    parser.add_argument(
        "--out-root",
        default=str(REPO_ROOT / "logs" / "product-round" / "session-journey"),
        help="receipt root; a per-run directory is created inside it",
    )
    parser.add_argument(
        "--scenario", action="append", default=[], help="one scenario slug; repeatable"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="the fast subset; EXCLUDES the phrase corpus, which is the intent router's net",
    )
    parser.add_argument(
        "--diff", default="", help="a previous run directory to diff against"
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout"
    )
    args = parser.parse_args(argv)

    slugs = list(args.scenario) or (list(QUICK_SLUGS) if args.quick else [])
    try:
        document = run_journeys(Path(args.out_root), slugs=slugs or None)
    except Exception as exc:  # a harness that cannot mount is not a clean run
        payload = {
            "schema_version": 1,
            "kind": "neo-session-journey",
            "verdict": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(
            json.dumps(payload, indent=2, sort_keys=True)
            if args.json
            else f"session-journey could not mount the shell: {payload['error']}"
        )
        return 3

    if args.diff:
        try:
            before = load_run(Path(args.diff))
        except Exception as exc:
            document["diff_error"] = f"{type(exc).__name__}: {exc}"
        else:
            document["diff"] = diff_runs(before, document)

    findings = sum(len(t.get("findings") or ()) for t in document["transcripts"])
    if args.json:
        print(
            json.dumps(
                document, indent=2, sort_keys=True, ensure_ascii=False, default=str
            )
        )
    else:
        print(f"run {document['run_id']} -> {document['run_dir']}")
        for transcript in document["transcripts"]:
            metrics = transcript.get("metrics") or {}
            print(
                f"  {transcript['slug']:26s} {transcript['verdict']:8s} "
                f"steps={metrics.get('steps')} findings={len(transcript.get('findings') or ())} "
                f"wall={metrics.get('wall_ms')}ms"
            )
            for note in transcript.get("findings") or ():
                print(f"      - {note.get('code')}: {note.get('detail')}")
            for note in transcript.get("registered") or ():
                print(f"      ~ REGISTERED {note.get('code')}: {note.get('detail')}")
        if "diff" in document:
            print(f"  diff: identical={document['diff']['identical']}")
        print(f"  verdict: {document['verdict']} ({findings} findings)")
    return 0 if not findings and document["verdict"] != "ERROR" else 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
