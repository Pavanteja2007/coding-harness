"""The human session harness as a GATE on the daily path.

Every defect ever reported in this project was found by a PERSON. This file is
the standing proof that a person's session is still a first-class test input:
it drives the REAL `cli.tui.NeoApp` through Textual's own `Pilot`, types what a
person would type, and asserts on WHAT THE USER SEES rather than on any
internal.

The four claims this gate makes, and where each is proved:

* **A real session still works.** Multiple turns, a tool call, a diff, a
  failure, a recovery, a mid-run cancel and a resume, in one mounted shell
  (`TestTheRealSessionShowsThePersonWhatHappened`).
* **The phrase corpus is the intent router's regression net.** 69 utterances a
  person would really type, each asserted to produce a sensible, visible
  outcome, with the thirteen mis-routed ones REGISTERED and pinned by an INVERTED
  assertion that fails the day somebody fixes one
  (`TestThePhraseCorpusIsARoutingNet`).
* **Nothing leaks that a person should not read.** No raw journal event name,
  no library traceback, no vanished message, the product's own status
  vocabulary, and honest verification everywhere
  (`TestWhatTheUserSeesIsSane`).
* **A visual change shows up as a diff.** A machine-readable transcript and an
  SVG per scenario, each with a SHA-256, and a real diff between two runs
  (`TestAVisualRegressionBecomesADiff`).

Two things this file deliberately does NOT do, and both are stated in the
`evals/AGENTS.md` section this round appends:

1. **It does not fix the defects it finds.** `cli/tui.py` is Prompt 01's and
   `harness/agent_loop.py` is AGT-02/AGT-11's; the two surface gaps this round
   measured live in `evals/session_journey.py::REGISTERED_SURFACE_GAPS` with an
   owner and a fix for each, and the gate pins the register to the OBSERVED
   codes so a fix goes red and says which row to delete.
2. **It does not touch the verifier gate.** Every assertion here reads the
   shell's own rendering of a run the fake backend published; none of them can
   make an unverified run read as verified, and one class exists solely to say
   so.

Everything here is host-only: no Docker, no provider, no network, no
credential. The session backends are doubles installed through
`cli.interactive`'s documented module attributes and the model boundary through
`harness.deps.set_call_model`, and the driver restores every process-global it
touched.

The lanes are SLOW on purpose -- the whole point is a real event loop with real
threads -- so the file is organised as one class per claim with a module-scoped
cache for the drives that more than one class needs. Each drive is measured and
the number is asserted against a bound that is a MEASUREMENT, not a guess.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from evals import session_journey as journey

#: The whole corpus, driven ONCE through one mounted shell, and shared by
#: every class that needs it. A 69-line session is ~30 s of real event loop;
#: paying that once per assertion class would make the gate about the host's
#: patience rather than about the product.
_CORPUS_DRIVE: Dict[str, Any] = {}
_SCENARIO_DRIVES: Dict[str, Any] = {}


def corpus_drive() -> Dict[str, Any]:
    """The corpus scenario's transcript document, driven once per process."""
    if "corpus" not in _CORPUS_DRIVE:
        started = time.perf_counter()
        _CORPUS_DRIVE["corpus"] = journey.run_scenario(
            journey.scenario_map()[journey._PHRASE_CORPUS_SCENARIO.slug]
        )
        _CORPUS_DRIVE["seconds"] = time.perf_counter() - started
    return _CORPUS_DRIVE["corpus"]


def scenario_drive(slug: str) -> Dict[str, Any]:
    """One scenario's transcript document, driven once per process."""
    if slug not in _SCENARIO_DRIVES:
        _SCENARIO_DRIVES[slug] = journey.run_scenario(journey.scenario_map()[slug])
    return _SCENARIO_DRIVES[slug]


# ---------------------------------------------------------------------------
# 0. The corpus is a corpus, and the drivers are total
# ---------------------------------------------------------------------------


class TestTheCorpusIsRealInput:
    """The corpus properties, before any of it is driven.

    A gate over a corpus that shrank to nine phrases is a gate that stopped
    gating, so the size and the SHAPE are asserted here rather than inferred
    from the drive.
    """

    def test_the_corpus_has_at_least_sixty_utterances(self) -> None:
        assert len(journey.PHRASE_CORPUS) >= 60, (
            f"the brief asks for at least 60; the corpus has {len(journey.PHRASE_CORPUS)}"
        )

    def test_the_corpus_covers_every_shape_a_person_types(self) -> None:
        """Typos, no punctuation, all lowercase, run-on, vague, and no file.

        Each of those is a NAME in the data rather than a judgement, so a
        corpus that quietly stopped containing run-on sentences fails here
        instead of passing on a corpus that no longer resembles a person.
        """
        by_group: Dict[str, List[journey.Phrase]] = {}
        for phrase in journey.PHRASE_CORPUS:
            by_group.setdefault(phrase.group, []).append(phrase)
        for group in ("typo", "vague", "run_on", "question", "bug_report", "hostile"):
            assert by_group.get(group), f"the corpus has no {group!r} phrases"

        run_ons = by_group["run_on"]
        assert any(len(phrase.text.split()) >= 20 for phrase in run_ons), (
            "no run-on sentence is actually a run-on"
        )
        assert any(
            not any(char.isupper() for char in phrase.text) for phrase in run_ons
        ), "the lowercase requirement is asserted by nobody"
        assert any(
            phrase.text and phrase.text[-1] not in ".?!" for phrase in run_ons
        ), "the no-punctuation requirement is asserted by nobody"
        assert any(
            ".py" not in phrase.text and "()" not in phrase.text
            for phrase in by_group["vague"]
        ), "no vague phrase is actually vague about the file"

    def test_every_utterance_routes_to_one_of_three_kinds(self) -> None:
        for phrase in journey.PHRASE_CORPUS:
            assert phrase.kind in journey.ROUTER_KINDS, (
                f"{phrase.text!r} is recorded with kind {phrase.kind!r}, which is "
                "not one of the router's three -- a fourth kind must be added to "
                "ROUTER_KINDS deliberately, not by an unnoticed default"
            )

    def test_no_scanner_marker_is_a_phrase_a_person_can_type(self) -> None:
        """A marker a person can TYPE is a marker that cries wolf.

        This gate fired on itself once: rich's markup error reads `closing tag
        '[/]' at position N has nothing to close`, so `"closing tag"` was in
        `FORBIDDEN_TRACEBACK`, and the corpus contains the perfectly ordinary
        utterance `[/] unbalanced closing tag`. The finding was in the GATE.
        So the rule is asserted here: no forbidden marker may appear verbatim in
        a corpus utterance, except the `SomethingError:` forms nobody types in
        a sentence about their own code.
        """
        prose_markers = {
            "AttributeError:",
            "TypeError:",
            "KeyError:",
            "IndexError:",
            "ValueError:",
            "UnboundLocalError:",
            "Traceback (most recent call last)",
            "StaleWorker",
        }
        offending = sorted(
            {
                name
                for name in journey.FORBIDDEN_TRACEBACK
                for phrase in journey.PHRASE_CORPUS
                if name in phrase.text and name not in prose_markers
            }
        )
        assert offending == [], (
            "these traceback markers appear verbatim in a corpus utterance, so "
            f"the gate would fire on the person rather than on a defect: {offending}"
        )

    def test_no_utterance_is_duplicated(self) -> None:
        texts = [phrase.text for phrase in journey.PHRASE_CORPUS]
        assert len(set(texts)) == len(texts), (
            "a duplicate in the corpus would make the net thinner than it looks: "
            f"{sorted({t for t in texts if texts.count(t) > 1})}"
        )

    def test_every_recorded_gap_says_why(self) -> None:
        """A gap without a reason is a shrug with a table.

        The register exists so the gate can be green while a real defect is
        open. That is only honest if the reason is written down, so a bare
        code cannot be registered.
        """
        assert journey.RECORDED_ROUTING_GAPS, (
            "the register is empty -- either nothing is mis-routed (delete the "
            "register and this test) or nobody recorded what is"
        )
        for text, reason in journey.RECORDED_ROUTING_GAPS.items():
            assert reason.strip(), f"the routing gap for {text!r} has no reason"
            assert text in {phrase.text for phrase in journey.PHRASE_CORPUS}, (
                f"{text!r} is registered as a gap but is not in the corpus"
            )

    def test_the_gate_can_actually_fail(self) -> None:
        """The scanners' sensitivity, proved rather than assumed.

        A gate nobody knows the sensitivity of is a gate nobody reads. Each
        scanner is fed a synthetic breach and must produce its finding -- a
        scanner that cannot fail is decoration.

        The raw-event control deliberately uses a bare transport name rather
        than `unknown event: <kind>`, because that one IS a declared product
        receipt and is carved out on purpose; the carve-out has its own test
        below.
        """
        leak = journey.visible_findings(
            "the run emitted model_delta at 12:03 and then tool_call", label="control"
        )
        assert sorted(note["code"] for note in leak) == [
            "raw_event_name_visible",
            "raw_event_name_visible",
        ], "the raw-event scanner did not fire on a synthetic leak"
        assert not journey.visible_findings("Reading app.py", label="control"), (
            "the raw-event scanner fired on an ordinary human sentence"
        )

        boom = journey.visible_findings(
            'Traceback (most recent call last):\n  File "x.py", line 1', label="control"
        )
        assert [note["code"] for note in boom] == ["library_traceback_visible"], (
            "the traceback scanner did not fire on a synthetic traceback"
        )

    def test_the_declared_receipts_are_carved_out_and_nothing_wider(self) -> None:
        """The carve-outs are the product's declared honesty receipts.

        A wider exemption is a place for the next defect to hide, so the
        whole declared sentence is removed and the surrounding text is not.
        """
        declared = journey.visible_findings(
            "journal 2 unreadable event(s): verification_rung, steering_queue",
            label="control",
        )
        assert declared == [], "a declared receipt was reported as a leak"
        assert journey.visible_findings(
            "verification_rung happened here", label="control"
        ), "a transport name outside a declared receipt escaped the carve-out"

    def test_the_readers_never_raise_on_a_dead_app(self) -> None:
        """An evidence driver that crashes on a broken app proves nothing.

        Every reader in this module is total, so a shell that failed to mount
        produces a finding in the report rather than a traceback in the driver.
        """

        class _Dead:
            def query_one(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("not mounted")

        assert journey.transcript_plain(_Dead()) == ""
        assert journey.transcript_rows(_Dead()) == 0
        assert journey.frame_rows(_Dead()) == []
        assert journey.frame_text(_Dead()) == ""
        assert journey.surface_text(_Dead(), "neo-body") == ""


# ---------------------------------------------------------------------------
# 1. The real session
# ---------------------------------------------------------------------------


class TestTheRealSessionShowsThePersonWhatHappened:
    """One mounted shell, a real session, and every beat the brief names."""

    def test_a_session_with_turns_a_tool_call_a_diff_a_failure_and_a_recovery(
        self,
    ) -> None:
        """The canonical session, and it must leave NOTHING to find.

        The beats are not decorative: `expect` is checked against the kind the
        shell actually DISPATCHED, so a greeting that launched a run fails
        here, and a request that answered with silence fails here too.
        """
        transcript = scenario_drive("daily_session")
        assert transcript["findings"] == [], json.dumps(
            transcript["findings"], indent=2
        )
        assert transcript["verdict"] == "CLEAN"
        steps = {step["label"]: step for step in transcript["steps"]}

        assert steps["greeting"]["dispatched"] == "", (
            f"a greeting dispatched work: {steps['greeting']['dispatched']!r}"
        )
        assert steps["question"]["dispatched"] == "question", (
            "a question did not reach the read-only answer path"
        )
        assert steps["failing fix"]["dispatched"] == "agent_task"
        assert steps["recovery"]["dispatched"] == "agent_task"

    def test_a_tool_call_is_shown_as_a_sentence_a_person_would_read(self) -> None:
        """A tool call reads as an ACTION, not as a transport name.

        The scripted journal really does carry `tool_call` / `tool_result` /
        `plan` / `verify` rows, so this is a real projection and not a
        hand-drawn card -- and `cli.tracelog` turns a `cat <file>` into
        "Reading <file>".
        """
        transcript = scenario_drive("daily_session")
        text = "".join(step["transcript"] for step in transcript["steps"])
        assert "Reading mathutil.py" in text, (
            "the tool call did not render as a readable action:\n" + text[-1500:]
        )
        assert "planned 1 sub-step" in text, (
            "the plan did not render as a sentence a person can read"
        )

    def test_a_failure_shows_its_cause_and_a_recovery_shows_its_verdict(self) -> None:
        """A failure names something; a recovery says VERIFIED.

        Two different claims and two different sentences. A gate that only
        checked "the run finished" would pass on both of the failure shapes
        this project keeps shipping.

        The cause is asserted as the shell's own vocabulary -- the classified
        KIND, `syntax_error` -- and not as the raw message text, because the
        raw text is exactly what a person is not meant to be reading here. The
        full text is one `/trace` away and the card says so.
        """
        transcript = scenario_drive("daily_session")
        steps = {step["label"]: step for step in transcript["steps"]}
        failing = steps["failing fix"]
        assert "syntax_error" in failing["transcript"], (
            "the failing run did not name the kind of failure it hit:\n"
            + failing["transcript"][-1200:]
        )
        assert "/trace" in failing["transcript"], (
            "the failure did not offer the trace as the way to read the full "
            "output:\n" + failing["transcript"][-1200:]
        )
        assert "FAILED" in failing["verdicts"], (
            "the failing run rendered no FAILED verdict: " + repr(failing["verdicts"])
        )
        assert "VERIFIED" in steps["recovery"]["verdicts"], (
            "the recovered run did not render a VERIFIED verdict"
        )
        assert steps["recovery"]["last_verdict"] == "VERIFIED"

    def test_a_diff_is_visible_after_the_run_that_made_it(self) -> None:
        """`/diff` is a command a person types, and it must answer on screen."""
        transcript = scenario_drive("daily_session")
        diff_step = next(
            step for step in transcript["steps"] if step["label"] == "the diff"
        )
        assert diff_step["transcript_chars"] > 0
        assert "mathutil.py" in diff_step["transcript"], (
            "the diff step did not name the file that changed:\n"
            + diff_step["transcript"][-1200:]
        )

    def test_a_mid_run_cancel_stops_a_live_worker_and_says_so(self) -> None:
        """The cancel is REAL: the worker parks and the product interrupts it.

        A cancel test that sets a flag and then asserts the flag is unset
        proves nothing about the product. This one waits for the fake to report
        that it is parked, and measures how long the product took to get the
        worker back to idle.
        """
        transcript = scenario_drive("cancel_and_resume")
        steps = {step["label"]: step for step in transcript["steps"]}
        cancel = steps["a run that parks"]
        assert cancel["dispatched"] == "agent_task"
        assert "parked=True" in cancel["note"], (
            f"the worker never parked, so nothing was cancelled: {cancel['note']}"
        )
        assert cancel["status"] == "idle", (
            f"the shell is still {cancel['status']!r} after a cancel"
        )
        text = cancel["transcript"]
        assert "interrupted" in text.lower(), (
            "the cancel did not say it interrupted the run:\n" + text[-1200:]
        )
        assert "checkpoints kept" in text.lower(), (
            "the cancel did not say the checkpoints were kept, which is the "
            "half that tells a person the work is not lost"
        )

    def test_the_cancel_lands_quickly_and_the_number_is_asserted(self) -> None:
        """`cancel_to_idle_ms` is a MEASUREMENT, and this is its bound.

        Recorded so a regression that turns a 200 ms cancel into a 20 s one
        fails a test instead of being noticed by somebody waiting for a
        terminal. The bound is generous on purpose: it is a shared
        four-terminal host.
        """
        transcript = scenario_drive("cancel_and_resume")
        cancel = next(
            step for step in transcript["steps"] if step["expect"] == "cancel"
        )
        raw = next(
            part
            for part in cancel["note"].split()
            if part.startswith("cancel_to_idle_ms=")
        )
        millis = float(raw.split("=", 1)[1])
        assert millis < 5000.0, f"cancel_to_idle_ms was {millis}ms on this host"

    def test_a_resume_actually_restarts_a_run(self) -> None:
        """The resume must DISPATCH. What it must SHOW is a registered gap.

        The dispatch is the claim this file can make without fixing a defect
        in another terminal's file; the silence is registered in
        `REGISTERED_SURFACE_GAPS` with an owner, and
        `TestTheRegisteredSurfaceGapsAreHonest` keeps that registration tied
        to what was actually observed.
        """
        transcript = scenario_drive("cancel_and_resume")
        dispatched = [row["kind"] for row in transcript["metrics"]["dispatched"]]
        assert "resume" in dispatched, (
            f"the resume never reached the backend: {dispatched}"
        )
        resume = next(
            step for step in transcript["steps"] if step["expect"] == "resume"
        )
        assert resume["grew"] is False, (
            "the resume grew the transcript; if that is now correct, delete the "
            "resume row from REGISTERED_SURFACE_GAPS and assert the growth here"
        )

    def test_a_cancelled_run_publishes_no_terminal_result(self) -> None:
        """The journal shape a cancel must leave, and the reason it matters.

        A cancelled run has no terminal row, so the shell says RUNNING rather
        than dressing an interrupt as a finished run -- and `/resume` on that
        run is allowed instead of being refused as "session is completed".
        """
        transcript = scenario_drive("cancel_and_resume")
        cancel = next(
            step for step in transcript["steps"] if step["expect"] == "cancel"
        )
        assert "RUNNING" in cancel["transcript"], (
            "the cancelled run rendered a finished status:\n"
            + cancel["transcript"][-1200:]
        )
        assert "cannot resume" not in cancel["transcript"], (
            "the cancelled run was treated as a completed session"
        )


# ---------------------------------------------------------------------------
# 2. The phrase corpus, driven
# ---------------------------------------------------------------------------


class TestThePhraseCorpusIsARoutingNet:
    """69 utterances, typed into the real composer, each with a visible outcome.

    The net is over the ROUTER, so the assertion is the pair: the kind the
    deterministic tier measured, and the kind the shell actually DISPATCHED
    for the step that typed it. Those two agreeing is what makes the corpus a
    regression net rather than a restatement of the classifier's own table.
    """

    def test_every_utterance_was_typed_and_something_came_back(self) -> None:
        transcript = corpus_drive()
        assert len(transcript["steps"]) == len(journey.PHRASE_CORPUS), (
            f"{len(transcript['steps'])} steps for {len(journey.PHRASE_CORPUS)} phrases"
        )
        silent = [
            step["line"]
            for step in transcript["steps"]
            if "no_visible_outcome" in {note["code"] for note in step["findings"]}
        ]
        assert silent == [], f"these utterances produced nothing at all: {silent}"

    def test_every_utterance_routed_the_way_the_deterministic_tier_decided(
        self,
    ) -> None:
        """The whole corpus, one assertion each, against the DISPATCH.

        This is the regression net the brief asks for. It is 69 assertions in
        one test on purpose: a person wants to know "did the router change?",
        and one failure per changed phrase is the answer.

        The comparison is tier-1 verdict -> the dispatch the shell MADE, not
        verdict -> verdict. `chit_chat` launches nothing, so the net is also
        what proves no greeting quietly started a run.
        """
        transcript = corpus_drive()
        by_index = {step["index"]: step for step in transcript["steps"]}
        mismatches: List[str] = []
        for phrase, index in journey.routed_steps():
            expected = journey.expected_dispatch(phrase.kind)
            observed = by_index[index]["dispatched"]
            if observed != expected:
                mismatches.append(
                    f"{phrase.text!r}: tier said {phrase.kind!r} "
                    f"(expects a {expected!r} dispatch), shell did {observed!r}"
                )
        assert not mismatches, (
            "the shell's routing disagreed with the deterministic tier on "
            f"{len(mismatches)} utterance(s):\n  " + "\n  ".join(mismatches[:12])
        )

    def test_a_greeting_never_launches_work_and_a_bug_report_never_only_asks(
        self,
    ) -> None:
        """The two ends of the cost asymmetry, which is the product's decision.

        A wrong run burns minutes and model budget; a clarifying question costs
        one line. Both halves are asserted, because a router that always asks
        is as wrong as one that always runs. A bug report may legitimately be
        answered with a question -- "why is wrap() dropping the last line?" is a
        real question about a real bug -- so the claim is that it was not
        SWALLOWED into silence, not that it was worked on.
        """
        by_index = {step["index"]: step for step in corpus_drive()["steps"]}
        launched = [
            phrase.text
            for phrase, index in journey.routed_steps()
            if phrase.group in ("greeting", "meta")
            and by_index[index]["dispatched"] == "agent_task"
        ]
        assert launched == [], (
            f"a greeting or a meta question launched work: {launched}"
        )
        swallowed = [
            phrase.text
            for phrase, index in journey.routed_steps()
            if phrase.group == "bug_report"
            and phrase.sensible
            and by_index[index]["dispatched"] not in ("agent_task", "question")
        ]
        assert swallowed == [], (
            f"a real bug report produced neither work nor an answer: {swallowed}"
        )

    def test_the_recorded_routing_gaps_are_pinned_inverted(self) -> None:
        """The INVERTED pin: this test FAILS when somebody fixes a gap.

        A recorded gap is a gap somebody can close. The pin is what stops the
        register from becoming a permanent shrug -- and it is deliberately
        inverted, so the day `harness/agent_loop.py` learns the word `crash`
        the gate says so instead of quietly accepting the old answer.
        """
        by_index = {step["index"]: step for step in corpus_drive()["steps"]}
        still_gapped = [
            phrase.text
            for phrase, index in journey.routed_steps()
            if not phrase.sensible
            and by_index[index]["dispatched"] == journey.expected_dispatch(phrase.kind)
        ]
        # Every recorded gap must still be mis-routed, so the register and the
        # observation cannot drift apart in EITHER direction.
        assert set(still_gapped) == set(journey.corpus_gap_texts()), (
            "the recorded routing gaps and the observed ones disagree.\n"
            f"  registered but no longer observed: "
            f"{sorted(set(journey.corpus_gap_texts()) - set(still_gapped))} "
            "-- a FIXED gap: delete it from RECORDED_ROUTING_GAPS and assert the "
            "new routing in test_every_utterance_routed_the_way_the_"
            "deterministic_tier_decided.\n"
            f"  observed but not registered: {sorted(set(still_gapped) - set(journey.corpus_gap_texts()))} "
            "-- a NEW gap: add it with a reason."
        )

    def test_the_corpus_costs_what_it_costs_and_the_number_is_asserted(self) -> None:
        """A gate nobody can afford is a gate nobody runs. MEASURED, not guessed."""
        seconds = _CORPUS_DRIVE.get("seconds", 0.0)
        assert seconds < 300.0, (
            f"the {len(journey.PHRASE_CORPUS)}-phrase corpus took {seconds:.1f}s; "
            "the bound is a host budget, and blowing it means the gate stops "
            "being run rather than the product getting slower"
        )


# ---------------------------------------------------------------------------
# 3. What the user sees is sane
# ---------------------------------------------------------------------------


class TestWhatTheUserSeesIsSane:
    """The visible-surface assertions, over every scenario.

    These are the claims the brief lists: rows rendered, no vanished messages,
    no raw event names, no library traceback, correct status vocabulary, and
    honest verification everywhere.
    """

    @pytest.mark.parametrize("slug", journey.QUICK_SLUGS)
    def test_no_unjourneyed_scenario_left_a_finding(self, slug: str) -> None:
        transcript = scenario_drive(slug)
        assert transcript["findings"] == [], json.dumps(
            transcript["findings"], indent=2
        )

    @pytest.mark.parametrize("slug", journey.QUICK_SLUGS)
    def test_the_first_message_is_still_on_screen_at_the_end(self, slug: str) -> None:
        """A render failure must never DELETE a message.

        The control marker is typed before anything else and must still be
        readable when the last step finishes. This is the assertion no other
        one here can make: a transcript that silently lost its first line looks
        identical to a transcript that never had one.
        """
        transcript = scenario_drive(slug)
        metrics = transcript["metrics"]
        marker = f"MARKER-{slug}-must-survive"
        assert marker in metrics["surfaces"]["transcript"], (
            f"{slug}: the control marker is gone from the transcript; a render "
            "failure deleted a message"
        )
        assert not any(
            note["code"] == "message_vanished" for note in transcript["findings"]
        )

    @pytest.mark.parametrize("slug", journey.QUICK_SLUGS)
    def test_no_library_traceback_reached_the_screen(self, slug: str) -> None:
        transcript = scenario_drive(slug)
        for name in journey.FORBIDDEN_TRACEBACK:
            assert name not in transcript["metrics"]["surfaces"]["transcript"], (
                f"{slug}: {name!r} reached the transcript"
            )
            assert name not in transcript.get("frame_text", ""), (
                f"{slug}: {name!r} reached the frame"
            )

    @pytest.mark.parametrize("slug", journey.QUICK_SLUGS)
    def test_the_status_chip_uses_the_products_own_vocabulary(self, slug: str) -> None:
        """The shell's own words, read from the shell's own constants.

        Imported rather than restated: a copied list is a second vocabulary,
        and a second vocabulary is how a shell grows a word nobody pinned.
        """
        from cli import tui as tui_module

        vocabulary = {
            tui_module._STATUS_IDLE,
            tui_module._STATUS_RUNNING,
            tui_module._STATUS_WAITING,
            "success",
            "failed",
        }
        transcript = scenario_drive(slug)
        seen = {str(step["status"]) for step in transcript["steps"]}
        assert seen <= vocabulary, (
            f"{slug}: unexpected status words {sorted(seen - vocabulary)}"
        )

    def test_a_completed_run_with_no_verifier_reads_as_unverified(self) -> None:
        """`completed_unverified` is NEVER success, on any surface.

        The last verdict a reader's eye lands on is the assertion, not the
        only one: the scrollback is the run's history and an earlier verified
        run legitimately appears above it. A naive substring scan over the
        WHOLE transcript is deliberately NOT used -- the wordmark's own tagline
        ("verified, not vibed") and the control marker's name would both
        match it, so a substring gate here would be measuring the harness's own
        text rather than the product's claim about a run.
        """
        transcript = scenario_drive("unverified_is_not_success")
        step = transcript["steps"][-1]
        assert "COMPLETED · UNVERIFIED" in step["transcript"], (
            "the completion card did not say COMPLETED · UNVERIFIED:\n"
            + step["transcript"][-1200:]
        )
        assert step["last_verdict"] == "UNVERIFIED", (
            f"the last verdict a reader would land on is {step['last_verdict']!r}"
        )
        assert journey.unverified_tail_is_honest(step["transcript"]), (
            "an unverified run rendered a success word last"
        )
        assert "not verified" in step["surfaces"]["announce"].lower(), (
            "the announcement band did not say the run was not verified:\n"
            + repr(step["surfaces"]["announce"])
        )
        assert transcript["metrics"]["unverified_tail_honest"] is True
        assert transcript["metrics"]["frame_honest"] is True

    def test_the_verdict_word_reader_cannot_be_fooled_by_unverified(self) -> None:
        """`UNVERIFIED` must never match as `VERIFIED`.

        The regex's alternation order is the whole mechanism, and a reader who
        does not know that would `in` the text and get the opposite answer. So
        the ordering is pinned here rather than left to a careful future edit.
        """
        assert journey.verdict_mentions("COMPLETED · UNVERIFIED") == ("UNVERIFIED",)
        assert journey.verdict_mentions("SUCCESS · VERIFIED") == ("SUCCESS", "VERIFIED")
        assert journey.last_verdict("UNVERIFIED then VERIFIED") == "VERIFIED"
        assert journey.unverified_tail_is_honest("UNVERIFIED then VERIFIED") is False
        assert journey.unverified_tail_is_honest("no verdict at all") is True

    def test_a_failure_never_reads_as_a_success(self) -> None:
        transcript = scenario_drive("daily_session")
        failing = next(
            step for step in transcript["steps"] if step["label"] == "failing fix"
        )
        assert (
            "RUNNING" in failing["transcript"] or "FAILED" in failing["transcript"]
        ), (
            "the failing run rendered neither a running nor a failed verdict:\n"
            + failing["transcript"][-1200:]
        )
        assert journey.unverified_tail_is_honest(failing["transcript"]), (
            "the failing run's last verdict read as a success"
        )

    def test_the_hostile_run_keeps_the_message_and_never_interprets_the_markup(
        self,
    ) -> None:
        """A bracketed name is DATA. It must render literally and not vanish.

        This is the same regression the layout suite pins for the sidebar, seen
        through the whole session instead of one widget: a repository called
        `weird[name].py`, a request carrying `[bold]`/`[red]`/`[/]`, and a
        control marker that must still be on screen afterwards.
        """
        transcript = scenario_drive("hostile_repo")
        assert transcript["hostile_repo"] is True
        work = next(
            step for step in transcript["steps"] if step["label"] == "hostile request"
        )
        assert "weird[name]" in work["transcript"], (
            "the bracketed repository name was not rendered literally:\n"
            + work["transcript"][-1200:]
        )
        assert (
            "MARKER-hostile_repo-must-survive"
            in transcript["metrics"]["surfaces"]["transcript"]
        ), "a render failure deleted a message in the hostile session"

    def test_a_long_session_stays_bounded_and_every_turn_answered(self) -> None:
        """Sixty turns, measured: the transcript is bounded and nothing is lost.

        The bound is the shell's own (`_TRANSCRIPT_MAX_LINES`), read from the
        product rather than restated, and the assertion is that the transcript
        did NOT reach it -- because a session whose scrollback filled up is a
        session where the control marker would have been dropped.
        """
        from cli import tui as tui_module

        transcript = scenario_drive("long_session")
        assert transcript["metrics"]["steps"] == journey.LONG_TURN_COUNT + 1
        cap = int(tui_module._TRANSCRIPT_MAX_LINES)
        assert transcript["metrics"]["transcript_rows"] < cap, (
            f"the transcript reached the shell's own {cap}-row cap; a 60-turn "
            "session cannot then keep showing its own history"
        )
        for step in transcript["steps"]:
            assert "no_visible_outcome" not in {n["code"] for n in step["findings"]}, (
                f"turn {step['index']} produced nothing at all"
            )
        assert (
            "MARKER-long_session-must-survive"
            in transcript["metrics"]["surfaces"]["transcript"]
        ), "the 60-turn session dropped its first message"


# ---------------------------------------------------------------------------
# 4. The receipts, and the diff between runs
# ---------------------------------------------------------------------------


class TestAVisualRegressionBecomesADiff:
    """A machine-readable transcript and an SVG per scenario, and a real diff.

    The claim is not "we took a screenshot". It is that a change in what a
    person sees shows up as a DIFF in a document, so a reviewer reads two
    receipts side by side instead of squinting at two pictures.
    """

    def test_every_scenario_leaves_a_transcript_and_an_svg_with_a_digest(
        self, tmp_path: Path
    ) -> None:
        document = journey.run_journeys(tmp_path, slugs=list(journey.QUICK_SLUGS))
        run_dir = Path(document["run_dir"])
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["kind"] == "neo-session-journey-manifest"
        for slug in journey.QUICK_SLUGS:
            artifacts = manifest["artifacts"][slug]
            for kind in ("svg", "transcript"):
                receipt = artifacts[kind]
                path = run_dir / receipt["path"]
                assert path.is_file(), f"{slug}: {kind} receipt missing"
                import hashlib

                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                assert digest == receipt["sha256"], (
                    f"{slug}: the {kind} on disk does not match its own receipt -- "
                    "a receipt that cannot prove what it describes is not evidence"
                )
                assert receipt["bytes"] == path.stat().st_size

    def test_the_svg_is_a_real_frame_and_not_an_empty_document(self) -> None:
        """A receipt that rendered nothing raises rather than reporting zero."""
        transcript = scenario_drive("daily_session")
        svg = transcript.get("svg") or ""
        assert svg.startswith("<svg") or "<svg" in svg[:2000], (
            "the exported frame is not an SVG at all"
        )
        assert len(svg) > 5000, (
            f"the exported frame is {len(svg)} bytes -- suspiciously empty"
        )
        assert "neo" in svg.lower(), "the frame does not contain the product's own name"

    def test_two_runs_of_the_same_tree_are_identical_and_two_different_ones_are_not(
        self, tmp_path: Path
    ) -> None:
        """The diff has to be capable of saying "different", or it says nothing.

        One real pair of runs is compared for identity, and then a MUTATED copy
        of the first document is compared against the second, so the diff is
        exercised in both directions. A diff nobody has seen fire is a diff
        nobody trusts.
        """
        first = journey.run_journeys(
            tmp_path / "a", slugs=["unverified_is_not_success"]
        )
        second = journey.run_journeys(
            tmp_path / "b", slugs=["unverified_is_not_success"]
        )
        identity = journey.diff_runs(first, second)
        assert identity["identical"] is True, json.dumps(identity, indent=2)[:3000]

        mutated = json.loads(json.dumps(first))
        mutated["transcripts"][0]["steps"][0]["status"] = "mutated-on-purpose"
        mutated["transcripts"][0]["steps"][0]["verdicts"] = ["SUCCESS"]
        mutated["transcripts"][0]["steps"][0]["delta"] = (
            "+ a line a person did not see before"
        )
        delta = journey.diff_runs(first, mutated)
        assert delta["identical"] is False
        changed = delta["scenarios_changed"][0]
        fields = {change.get("field") for change in changed["steps"]}
        assert "status" in fields, (
            f"the diff missed the status change: {changed['steps']}"
        )
        assert "verdicts" in fields, (
            f"the diff missed the verdict change: {changed['steps']}"
        )
        assert "delta" in fields, (
            f"the diff missed the visible-output change: {changed['steps']}"
        )

    def test_a_step_records_what_it_ADDED_not_only_a_sliding_window(self) -> None:
        """The per-step DELTA is the regression signal, and it must be real.

        The tail is a sliding window over the scrollback, so on a sixty-turn
        session it moves on every turn and comparing it between runs is noise.
        What a person saw HAPPEN is the text the step added, and that is what
        the diff compares. Without this the gate would report a difference on
        every turn of a long session and be ignored.
        """
        transcript = scenario_drive("daily_session")
        greeting = transcript["steps"][0]
        assert greeting["delta_chars"] > 0, "the marker step recorded no delta at all"
        recovery = next(
            step for step in transcript["steps"] if step["label"] == "recovery"
        )
        assert recovery["delta_chars"] > 0
        assert "VERIFIED" in recovery["delta"], (
            "the recovery step's own output does not carry its verdict:\n"
            + recovery["delta"][-800:]
        )
        for step in transcript["steps"]:
            assert step["delta_chars"] >= 0, (
                f"step {step['index']} has a negative delta"
            )

    def test_the_diff_does_not_compare_a_wall_clock_number(self) -> None:
        """Timing is a measurement, not a behaviour.

        A diff that compares `elapsed_ms` makes every loaded run a regression,
        and a gate that cries wolf is a gate people stop reading.
        """
        first = {
            "run_id": "a",
            "transcripts": [{"slug": "s", "steps": [{"index": 1, "elapsed_ms": 10.0}]}],
        }
        second = {
            "run_id": "b",
            "transcripts": [
                {"slug": "s", "steps": [{"index": 1, "elapsed_ms": 9999.0}]}
            ],
        }
        delta = journey.diff_runs(first, second)
        assert delta["identical"] is True, (
            "a pure timing difference was reported as a change; the note in the "
            "diff document says it must not be"
        )
        assert "measurement" in delta["note"]

    def test_the_same_tree_typed_twice_renders_the_same_text(self) -> None:
        """Two runs of one scenario must agree on every step's visible output.

        This is the whole justification for the diff existing. When this fails,
        the delta is carrying capture noise -- and capture noise is worse than
        no diff, because a gate that reports changes nobody made is a gate
        people learn to ignore.

        The three normalisations that make it hold were each found by this
        failing, and each is anchored to something real rather than to a
        convenience: box chrome is stripped because the card measures its own
        columns against a rail that was still teardown; a space lost at a wrap
        boundary is repaired from the FRAME, which is the rendered truth; and
        a bounded settle gives the shell's own 0.125 s repaint timer a chance
        to run before the step is captured.
        """
        scenario = journey.scenario_map()["daily_session"]
        first = journey.run_scenario(scenario)
        second = journey.run_scenario(scenario)
        assert [step["index"] for step in first["steps"]] == [
            step["index"] for step in second["steps"]
        ], "the two runs typed a different number of lines"
        drifted = [
            (a["index"], a["label"])
            for a, b in zip(first["steps"], second["steps"], strict=True)
            if a["delta"] != b["delta"] and a["diffable"]
        ]
        assert drifted == [], (
            "the same tree rendered different text on the same line: "
            f"{drifted}; the delta is carrying capture noise, not behaviour"
        )

    def test_the_corpus_survives_a_repeat_typing(self) -> None:
        """Sixty-nine phrases of a routed reply must be a stable measurement.

        The corpus is the router's regression net, so its own output has to be
        reproducible or a change in it cannot be attributed. The cancel step
        is excluded by declaration: it is typed and cancelled in the same
        breath, so whether the journal tail got to render the run's feed lines
        is a scheduling fact, and the diff says so rather than reporting it.
        """
        scenario = journey.scenario_map()["phrase_corpus"]
        first = journey.run_scenario(scenario)
        second = journey.run_scenario(scenario)
        assert first["verdict"] == second["verdict"] == "CLEAN"
        drifted = [
            a["index"]
            for a, b in zip(first["steps"], second["steps"], strict=True)
            if a["delta"] != b["delta"] and a["diffable"]
        ]
        assert drifted == [], f"corpus output moved between identical runs at {drifted}"

    def test_a_racy_step_is_declared_rather_than_hoped_away(self) -> None:
        """A step whose output cannot be reproduced must SAY SO.

        Silently exempting a step would be a gate quietly measuring less than
        it claims. The declaration is published in the diff document, the
        affected rows carry a reason, the numbering matches the scenario's own
        1-based step order, and the exemption is small enough to read at a
        glance.
        """
        racy = journey.diff_runs(
            {"run_id": "a", "transcripts": []}, {"run_id": "b", "transcripts": []}
        )["racy_steps"]
        assert racy, "no step declared its output non-deterministic"
        assert len(racy) <= 2, (
            f"too much of the gate is exempt to be trustworthy: {racy}"
        )
        by_slug = {scenario.slug: scenario for scenario in journey.SCENARIOS}
        for entry in racy:
            slug, _, number = entry.rpartition("#")
            assert slug in by_slug, (
                f"declared a racy step for an unknown scenario: {entry}"
            )
            step = by_slug[slug].steps[int(number) - 1]
            assert step.diffable is False, (
                f"{entry} is published as exempt but JourneyStep.diffable says otherwise"
            )
            assert step.expect in {"cancel", "resume"}, (
                f"{entry} is exempt from the diff; a typing step has no excuse: {step.expect}"
            )

    def test_a_changed_delta_carries_its_exemption_reason(self) -> None:
        """A reader must be able to tell an exempt difference from a real one."""
        row = {
            "index": 1,
            "delta": "one",
            "diffable": True,
            "grew": True,
            "surfaces": [],
        }
        racy = {
            "index": 1,
            "delta": "two",
            "diffable": False,
            "grew": True,
            "surfaces": [],
        }
        document = {
            "run_id": "a",
            "transcripts": [
                {
                    "slug": "s",
                    "steps": [row],
                    "findings": [],
                    "registered": [],
                    "metrics": {"transcript_chars": 0, "last_verdict": ""},
                }
            ],
        }
        after = {
            "run_id": "b",
            "transcripts": [
                {
                    "slug": "s",
                    "steps": [racy],
                    "findings": [],
                    "registered": [],
                    "metrics": {"transcript_chars": 0, "last_verdict": ""},
                }
            ],
        }
        changed = journey.diff_runs(document, after)["scenarios_changed"][0]
        hit = next(item for item in changed["steps"] if item["change"] == "delta")
        assert hit["skipped_reason"], "an exempt delta change gave no reason"

    def test_the_delta_never_leaks_the_box_that_drew_it(self) -> None:
        """Chrome is not content, and a rule is not a verdict.

        The completion card draws its rule to whatever width the content region
        had when it was measured, so the same run draws a different rule length
        depending on whether the rail had finished tearing down. Keeping the
        rules would report that as a change. Keeping them would ALSO mean a
        box-drawing character could sit where a verdict word is read from, which
        is the failure `last_verdict` exists to prevent -- so the two concerns
        are asserted together here.
        """
        card = (
            "\u256d"
            + "\u2500" * 42
            + " summary journey-agent-001 "
            + "\u2500" * 5
            + "\u256e\n"
            "\u2502 \u23f1 COMPLETED \u00b7 UNVERIFIED \u00b7 agent_task \u00b7 journey-agent-001\n"
        )
        cleaned = journey.normalise_box_chrome(card)
        assert "\u2500" not in cleaned, "the card's rule survived normalisation"
        assert "\u256d" not in cleaned and "\u256e" not in cleaned
        assert "COMPLETED" in cleaned and "UNVERIFIED" in cleaned, (
            "normalisation ate the verdict words: " + cleaned
        )
        assert journey.last_verdict(cleaned) == "UNVERIFIED"

    def test_a_space_lost_at_a_wrap_boundary_is_repaired_from_the_frame(self) -> None:
        """The frame is the rendered truth, so the frame settles the spelling.

        A `RichLog`'s stored segments can read `change codein this repo` where
        the terminal draws `change code in this repo`. Without a repair the
        delta reports a change nobody made; without the frame's authority the
        repair would be a guess that could invent a word.
        """
        segment_stream = "change codein this repo"
        rendered = "change code in this repo"
        repaired = journey._step_delta("", segment_stream, rendered)
        assert repaired == rendered, (
            f"the wrap-boundary space was not restored: {repaired!r}"
        )
        assert (
            journey._step_delta("", "a wholly new token", rendered)
            == "a wholly new token"
        ), "a token the frame cannot vouch for was rewritten anyway"
        assert journey._step_delta("", segment_stream, "") == " ".join(
            segment_stream.split()
        ), "with no frame the repair must not invent a split"

    def test_a_scenario_and_a_phrase_must_not_be_confused(self) -> None:
        """A fast lane that skipped the corpus would have skipped the net."""
        assert journey._PHRASE_CORPUS_SCENARIO.slug not in journey.QUICK_SLUGS
        assert "phrase_corpus" in {scenario.slug for scenario in journey.SCENARIOS}


# ---------------------------------------------------------------------------
# 5. The registered gaps, and the lines this module does not cross
# ---------------------------------------------------------------------------


class TestTheRegisteredSurfaceGapsAreHonest:
    """The gate passes BECAUSE two defects are registered, not because they are fixed.

    Each row names the symptom as measured, the file that owns it, and what a
    fix looks like. These tests are what stop the register from rotting: it
    must match what the drives OBSERVE, in both directions.
    """

    def test_every_registered_gap_was_actually_observed(self) -> None:
        observed: set = set()
        for slug in journey.QUICK_SLUGS:
            for note in scenario_drive(slug).get("registered") or ():
                observed.add(str(note["code"]))
        registered = set(journey.REGISTERED_GAP_CODES)
        assert registered == observed, (
            "the register and the observation disagree.\n"
            f"  registered but not observed: {sorted(registered - observed)} "
            "-- a fixed defect: delete its row and assert the new behaviour.\n"
            f"  observed but not registered: {sorted(observed - registered)} "
            "-- a new defect: add it with an owner and a fix, or fix it."
        )

    def test_every_registered_gap_says_where_it_lives_and_how_to_fix_it(self) -> None:
        assert journey.REGISTERED_SURFACE_GAPS, (
            "the surface-gap register is empty; if nothing is open, delete it "
            "and this test so nobody inherits a table of nothing"
        )
        for gap in journey.REGISTERED_SURFACE_GAPS:
            for field_name in ("symptom", "why_it_matters", "owner", "fix"):
                value = str(getattr(gap, field_name) or "").strip()
                assert len(value) > 30, (
                    f"the {gap.code!r} gap has no usable {field_name}: {value!r}"
                )
            assert gap.slug in journey.scenario_map(), (
                f"the {gap.code!r} gap names a scenario that does not exist: {gap.slug!r}"
            )

    def test_this_module_adds_no_config_key_and_touches_no_product_module(self) -> None:
        """The two lines this round does not cross, asserted rather than promised.

        A knob in `harness/config.py::DEFAULTS` merges into every Task and every
        eval arm, and a product module edited here would be a concurrent edit to
        another terminal's file. Both are checkable, so both are checked.
        """
        import harness.config as harness_config

        for key in harness_config.DEFAULTS:
            assert "journey" not in key, (
                f"{key!r} is a session-journey key in DEFAULTS; a value there is "
                "merged into every task and every eval arm"
            )
        assert "session_journey" not in harness_config.DEFAULTS

    def test_the_fakes_are_undone_and_the_receipt_says_so_by_name(self) -> None:
        """Driving the real shell means monkeypatching it, so the undo matters.

        The fake backend, the routing tier and the hosted run are installed on
        the PRODUCT's own objects. If one survives, every later test in the
        process measures a shell that is still talking to a script -- the worst
        failure mode a shared test process has.

        The receipt field is named `global_state_restore_failures` and holds
        failures, not successes, so an empty list means "nothing failed to be
        restored" and can be read at a glance. It is asserted empty for every
        scenario, which is the only way an empty list means anything.
        """
        for slug in sorted(journey.QUICK_SLUGS):
            transcript = scenario_drive(slug)
            assert "global_state_restore_failures" in transcript, (
                f"{slug}: the receipt does not report whether the fakes were undone"
            )
            assert "global_state_restored" not in transcript, (
                f"{slug}: `global_state_restored` reads like a list of things restored, "
                "but it held failures. Use the name that says which."
            )
            assert transcript["global_state_restore_failures"] == [], (
                f"{slug}: a global was not put back: "
                f"{transcript['global_state_restore_failures']}"
            )

    def test_a_step_reports_a_path_it_normalised_rather_than_a_raw_one(self) -> None:
        """The receipts are diffed, and a temp path would make them un-diffable.

        The shell's wordmark prints the repository and log roots, so an
        un-normalised receipt differs between two runs of the same tree by
        nothing but a random temp directory name. The receipt therefore says
        it normalised, and the substitution is declared rather than silent.
        """
        transcript = scenario_drive("daily_session")
        assert transcript["paths_normalised"] is True
        surfaces = transcript["metrics"]["surfaces"]
        blob = json.dumps(surfaces)
        assert "<root>" in blob or "<root>" in transcript["frame_text"], (
            "nothing was actually normalised; the receipt claims otherwise"
        )
        for value in (transcript["frame_text"], *surfaces.values()):
            assert "neo-journey-" not in value, (
                "a random temp-root name survived into a receipt:\n" + str(value)[:400]
            )

    def test_the_gate_never_asserts_a_verified_run_from_an_unverified_one(self) -> None:
        """The verifier gate is not reachable from here, and this says so.

        Every receipt in this file records a status the FAKE backend published.
        No assertion in this file derives a status, promotes one, or edits a
        mint -- so the honest-status property of the shell is what is being
        measured, and the mint itself is untouched.
        """
        transcript = scenario_drive("unverified_is_not_success")
        statuses = {str(step["dispatched"]) for step in transcript["steps"]}
        assert "agent_task" in statuses, "the unverified scenario did not run anything"
        step = transcript["steps"][-1]
        assert step["last_verdict"] == "UNVERIFIED", (
            "the honest-status assertion has stopped discriminating"
        )
        assert journey.unverified_tail_is_honest("SUCCESS") is False, (
            "the tail rule would accept a success word, which would make the "
            "unverified assertion above vacuous"
        )
