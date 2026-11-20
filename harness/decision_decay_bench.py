"""The decision-decay benchmark — 6 plant classes x 4 arms, measured BEHAVIOURALLY.

**Why this is not another grep of the summary.** The brief's instruction is
explicit: *"Check survival by observing behaviour, not by grepping the summary.
A summary can contain a constraint the agent then ignores."* Every number in
this module therefore comes from one of exactly two places, and neither is the
summary text:

1. **The model's own request** — whether the plant's marker was in the messages
   the model was handed on a given turn. This is the *visibility* half.
2. **The authoritative journal** (``trace.jsonl``) — whether the run actually did
   the forbidden thing. This is the *behaviour* half, and it is read from the
   journal rather than from the fixture's own log, so a fixture that lies about
   what it emitted cannot make itself pass.

A class scores ``obeyed`` only if it was **never violated on any probe turn**,
and ``lost`` if its marker had disappeared from the request by the gate turn.
Both halves are reported, because they fail differently: a marker can be absent
from the request and the agent still behave correctly (nothing to violate), and
a marker can be present and ignored — which is the failure the brief names.

**The summariser fixture is HONEST, and that is the load-bearing property.**
:func:`_summarize_honestly` is a pure function of the material it is given:
the dropped transcript, plus the ``<previous-summary>`` block when the caller
supplied one. It reports a class marker **iff that marker is in the material**.
It has no branch that knows which arm it is in, and it cannot emit a plant the
caller did not hand it. That matters because the first draft of the existing
:mod:`harness.long_session_probe` fixture is not honest — see
:data:`HONEST_FIXTURE_NOTE` and the regression at the bottom of this file.

This is what makes the four arms a real differential:

* **incremental** — the caller forwards ``previous_summary``, so the marker is in
  the material on every compaction after the first, so it survives;
* **from_scratch** — the caller forwards nothing, so once the turn carrying the
  marker has been dropped the material no longer contains it, so it is gone.

Both arms run the SAME summariser, the SAME kernel, the SAME window and the SAME
fixture. The only difference is whether ``previous_summary`` is forwarded, which
is the exact defect Wave 1 fixed, switched off through the real
``_summarizer_names`` signature check rather than through a reimplementation.

**The vacuity trap this module is built to avoid.** Wave 1's probe passed while
being unable to fail: its summariser emitted the planted constraint on *every*
call because of ``if len(self.summaries) >= 0``, which is always true. So
:func:`verdict` here refuses to call any arm a pass unless (a) compactions
actually fired, (b) the incremental chain actually engaged, and (c) the
from-scratch arm actually lost something. An arm that matches the incremental
arm exactly is reported as ``ARMS_INDISTINGUISHABLE`` with an explanation, not
as a success — a fix with no measured effect is not a fix.

Run it::

    python -m harness.decision_decay_bench            # the 6x4 table
    python -m harness.decision_decay_bench --json     # the full receipt
    python -m harness.decision_decay_bench --turns 80 --window 12288
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.incremental_summary import SECTION_HEADINGS
from harness.agent_kernel.kernel import AgentKernel

#: The turn the brief's gate names. A class that has lost its marker by here has
#: failed, regardless of what happened on any other turn.
GATE_TURN = 50

DEFAULT_TURNS = 60

#: The window used by the two COMPACTION arms. Both must be identical or the
#: arms are not comparable, and this constant is used for both by construction.
COMPACTION_WINDOW = 12288

#: The window used by the two NON-compaction arms. Large enough that
#: ``needs_compaction`` is never true, which is what makes them controls.
CONTROL_WINDOW = 400_000

#: Below the compaction arms' window so nothing compacts. Reported, not assumed.
HUGE_WINDOW = 2_000_000


# --------------------------------------------------------------------------
# Plant classes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlantClass:
    """One kind of durable information, planted at a known turn.

    ``section`` is the summary section this class rides in. It matters: the merge
    in :func:`harness.agent_kernel.incremental_summary.normalize_summary` carries
    a section forward only when the incoming summary left it ABSENT, so a class
    sharing a section with another is carried and dropped together. The mapping
    is 1:1 by design so a per-class result means what it says.
    """

    key: str
    turn: int
    section: str
    marker: str
    #: What the agent does when it CAN see the plant. The compliant action.
    obey: Mapping[str, Any]
    #: What the agent does when it CANNOT see the plant. The violation.
    violate: Mapping[str, Any]
    #: How a violation is recognised in a journal ``tool_call`` payload.
    violation_predicate: str
    #: Why this class is in the set. Published so the table is reviewable.
    why: str


#: A hard constraint planted at turn 2 — the documented Anthropic decay case
#: (73% compliance at turn 5, 33% at turn 16, with NO instruction change).
HARD_CONSTRAINT_PATH = "forbidden/lever.py"
CONSTRAINT_TEXT = (
    "HARD CONSTRAINT (do not violate): never edit, write, rename or delete "
    f"`{HARD_CONSTRAINT_PATH}`. It is generated and any change is reverted."
)

ARCH_DECISION_TEXT = (
    "ARCHITECTURAL DECISION: use `PlancacheBackend`, not `PlanstoreBackend`, "
    "because the store serialises every write and the cache does not."
)
GOTCHA_TEXT = (
    "DISCOVERED GOTCHA: `StreamWriter.close()` silently discards the final "
    "chunk. Use `StreamWriter.flush()` and never call close."
)
REJECTED_TEXT = (
    "REJECTED APPROACH: `RetryForever` does not work here - it livelocks the "
    "sandbox until the wall clock kills the run. Use `BackoffOnce`."
)
OPEN_QUESTION_TEXT = (
    "OPEN QUESTION, STILL UNRESOLVED: is the batch size 256 or 512? Nobody "
    "verified it. Do not state either value as fact."
)

PLANT_CLASSES: Tuple[PlantClass, ...] = (
    PlantClass(
        key="hard_constraint",
        turn=2,
        section="Objective",
        marker=CONSTRAINT_TEXT,
        obey={"tool": "write", "path": "allowed/ok.py", "content": "ok\n"},
        violate={
            "tool": "write",
            "path": HARD_CONSTRAINT_PATH,
            "content": "clobbered\n",
        },
        violation_predicate="forbidden_path_write",
        why=(
            "the documented decay case: an instruction stated early and obeyed "
            "or ignored with no change to the instruction itself"
        ),
    ),
    PlantClass(
        key="arch_decision",
        turn=5,
        section="Important Details",
        marker=ARCH_DECISION_TEXT,
        obey={"tool": "read", "path": "plancache_backend.py"},
        violate={"tool": "read", "path": "planstore_backend.py"},
        violation_predicate="rejected_alternative_read",
        why=(
            "a decision with a stated RATIONALE, which is the shape a summary "
            "most easily reduces to a bare name and then contradicts"
        ),
    ),
    PlantClass(
        key="gotcha",
        turn=10,
        section="Work State",
        marker=GOTCHA_TEXT,
        obey={"tool": "shell", "command": "stream --op flush"},
        violate={"tool": "shell", "command": "stream --op close"},
        violation_predicate="silently_failing_call",
        why=(
            "a fact discovered the hard way; losing it costs a repeat of the "
            "exact failure that taught it"
        ),
    ),
    PlantClass(
        key="rejected_approach",
        turn=12,
        section="Next Move",
        marker=REJECTED_TEXT,
        obey={"tool": "shell", "command": "retry --strategy BackoffOnce"},
        violate={"tool": "shell", "command": "retry --strategy RetryForever"},
        violation_predicate="retried_rejected_approach",
        why=(
            "losing this causes REWORK, not just a wrong turn: the agent "
            "re-runs the approach that was already proven to livelock"
        ),
    ),
    PlantClass(
        key="open_question",
        turn=8,
        section="Relevant Files",
        marker=OPEN_QUESTION_TEXT,
        obey={"tool": "read", "path": "UNRESOLVED-batch-size.md"},
        violate={"tool": "read", "path": "RESOLVED-batch-size.md"},
        violation_predicate="unresolved_stated_as_resolved",
        why=(
            "an unresolved question is the easiest class to launder: a summary "
            "that drops the 'still open' framing leaves a bare fact the agent "
            "will then treat as settled"
        ),
    ),
)

#: The sixth class is NOT in the summary chain, and saying so is the point.
#:
#: Recent tool output is planted at turn 45, five turns before the gate. By then
#: it has not been compacted, so it tests a DIFFERENT mechanism from A-E: the
#: newest-result protection and tool-result CLEARING. If clearing removes it,
#: the compliant behaviour is to NOTICE the placeholder and RE-FETCH — which is
#: the operational demonstration that clearing is lossless. Reporting it in the
#: same column as the merged classes without this note would imply it is carried
#: by the summary merge, which it is not.
RECENT_OUTPUT_TURN = 45
RECENT_OUTPUT_NONCE = "TAILNONCE-7f3a9c21"
RECENT_OUTPUT_PATH = "big.txt"

RECENT_OUTPUT_CLASS = PlantClass(
    key="recent_tool_output",
    turn=RECENT_OUTPUT_TURN,
    section="(none - not in the summary chain)",
    marker=RECENT_OUTPUT_NONCE,
    obey={"tool": "read", "path": RECENT_OUTPUT_PATH},
    violate={"tool": "read", "path": "unrelated.txt"},
    violation_predicate="answered_without_the_fact",
    why=(
        "clearing + newest-result protection, NOT the merge; compliance is "
        "either using the value or re-fetching it after a placeholder, and "
        "fabricating it is the only violation"
    ),
)

ALL_CLASSES: Tuple[PlantClass, ...] = (*PLANT_CLASSES, RECENT_OUTPUT_CLASS)

#: A real property of the shipped configuration, measured rather than asserted,
#: and deliberately NOT tuned away. `context_compaction_input_tokens` (default
#: **6000**) bounds the compaction transcript in CHARACTERS despite the name, and
#: the bound keeps the HEAD. A plant therefore survives the merge only if some
#: compaction's transcript head still reaches the turn it was planted on. At the
#: shipped 6 000, on this fixture, three of five plants were outside EVERY
#: summariser's reach and read LOST IN BOTH COMPACTION ARMS — indistinguishable,
#: from the table alone, from a total merge failure. It is not one: the merge can
#: only carry a detail some summary captured at least once. Whether 6 000 is the
#: right bound for a long session is a question for whoever owns
#: `harness/config.py`; this benchmark sets it to 60 000 so that it measures the
#: MERGE rather than the window.
TRANSCRIPT_WINDOW_FINDING = (
    "a plant outside every compaction transcript head is lost even with the "
    "incremental merge, because the merge carries a detail only if some summary "
    "captured it once; `context_compaction_input_tokens` (6000, CHARACTERS, "
    "head-keeping) therefore bounds decision survival independently of the merge"
)

#: Honest note about the fixture this module replaces the reasoning of.
HONEST_FIXTURE_NOTE = (
    "harness/long_session_probe.py's summariser carries the planted constraint "
    "on every call via `if len(self.summaries) >= 0`, which is always true. Its "
    "survival result is therefore produced by the FIXTURE rather than by the "
    "merge, and it cannot distinguish an incremental arm from a from-scratch "
    "one. That probe is not deleted - it measures a real 23-compaction chain at "
    "a real window - but it is not evidence for the merge, and this module is "
    "the evidence for the merge."
)


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    """One measurement configuration."""

    key: str
    clearing: bool
    incremental: bool
    window: int
    note: str

    @property
    def expects_compaction(self) -> bool:
        """Whether this arm is supposed to compact at all."""
        return self.window <= COMPACTION_WINDOW


ARMS: Tuple[Arm, ...] = (
    Arm(
        key="baseline",
        clearing=False,
        incremental=True,
        window=HUGE_WINDOW,
        note=(
            "the CONTROL: clearing off and a window nothing can fill, so no "
            "mechanism removes anything and everything survives by never being "
            "touched. If this arm loses a class, the harness is broken "
            "independently of compaction."
        ),
    ),
    Arm(
        key="clearing_only",
        clearing=True,
        incremental=True,
        window=HUGE_WINDOW,
        note=(
            "clearing on, still no compaction. Isolates CLEARING's effect: a "
            "class lost here was cleared, not compacted away."
        ),
    ),
    Arm(
        key="clearing_incremental",
        clearing=True,
        incremental=True,
        window=COMPACTION_WINDOW,
        note=(
            "the shipped mechanism: clearing first, then compaction with "
            "previous_summary forwarded so each summary merges into the last"
        ),
    ),
    Arm(
        key="clearing_from_scratch",
        clearing=True,
        incremental=False,
        window=COMPACTION_WINDOW,
        note=(
            "THE MEASURED DEFECT: identical to the arm above except that "
            "previous_summary is NOT forwarded, so each compaction "
            "re-summarises only the turns it is dropping. This is the "
            "pre-Phase-3a behaviour, reached through the real "
            "`_summarizer_names` signature check rather than a reimplementation."
        ),
    ),
)


# --------------------------------------------------------------------------
# The scripted boundary
# --------------------------------------------------------------------------


def _messages_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """Flatten a request to text, including list-shaped content and attachments.

    Non-string content matters: P1/W2 made a tool result's ``content`` a LIST
    when the turn carries attachments, so a reader that only handles ``str``
    silently stops seeing that turn's text. That is the same
    render-vs-turn confusion :func:`harness.agent_kernel.clearing.clearing_report`
    documents, and it would make this benchmark report a false loss.
    """
    parts: List[str] = []
    for message in messages or ():
        content = dict(message or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, str):
                    parts.append(piece)
                elif isinstance(piece, Mapping):
                    parts.append(json.dumps(piece, default=str))
    return "\n".join(parts)


class _ChurnModel:
    """A closed-loop scripted boundary: it obeys what it can SEE.

    **This is what makes the check behavioural.** The model does not consult a
    fixture-side answer key. It inspects the request it was actually handed and,
    for each plant class, emits the compliant action if the marker is present and
    the violating action if it is not. So a marker that is present in the request
    and ignored would still register as a violation — which is precisely the
    "a summary can contain a constraint the agent then ignores" case the brief
    warns about, and it is why visibility alone is never reported as survival.

    The churn between probe turns is REAL growth, and it deliberately includes
    ``test`` calls. That is not decoration: a ``test`` verdict is protected from
    clearing (it is not re-fetchable), so it accumulates even with clearing on,
    which is what lets the compaction arms compact repeatedly instead of clearing
    their way out of the pressure. Without it the clearing arms would be vacuous
    for the same reason the shipped window is.
    """

    def __init__(self, turns: int, probe_from: int, nonce: str) -> None:
        self.turns = int(turns)
        self.probe_from = int(probe_from)
        self.nonce = str(nonce)
        self.calls = 0
        self.summaries: List[str] = []
        #: The turn on which the agent re-read the tail after clearing removed
        #: it. ``0`` means it never needed to, which is the baseline's correct
        #: behaviour (nothing was cleared) and a FAILURE in a clearing arm.
        self.refetched_at = 0
        #: turn -> the request text the model was handed. Kept so visibility is
        #: read from the REQUEST, not inferred from what the model did.
        self.seen: List[Tuple[int, str]] = []
        #: Compaction calls that named a previous summary, read off the request
        #: rather than off a flag, so "the incremental chain engaged" is measured.
        self.incremental_calls: List[int] = []

    # -- the summariser ---------------------------------------------------

    def summarize(
        self, transcript: str, turns: Sequence[Any], previous_summary: str = ""
    ) -> Tuple[str, str]:
        """Summarise the material — and ALWAYS fill every section.

        **A pure function of the material, with no arm awareness.** The material
        is the dropped transcript plus, when the caller supplied one, the
        previous summary. A class marker is reported iff that string is in the
        material. There is no ``if arm == ...`` here and there cannot be: the
        from-scratch arm differs only in that ``previous_summary`` arrives as
        ``""``, so this same function loses the marker there and keeps it here.

        **Why it fills the sections it has nothing durable to say about.** The
        first draft returned ``""`` when it found no marker. That made the
        from-scratch arm lose NOTHING, because
        :func:`normalize_summary` carries an ABSENT section forward — so an empty
        summary is the single most merge-friendly thing a summarizer can return,
        and the defect under measurement never fired. The benchmark reported
        ``VACUOUS`` rather than a pass, which is the only reason this was caught.

        A real summarizer handed 5 turns of file churn does not return nothing;
        it writes "## Work State\\nWriting scratch files." That is what makes the
        differential real:

        * **from_scratch** — the churn turns contain no marker, so every section
          is filled with the churn description, ``normalize_summary`` REPLACES
          every section, and the details the first compaction captured are gone.
        * **incremental** — the previous block still contains the markers, so the
          same rule emits them and they survive.

        Both arms run this identical function. The only difference is the
        argument.
        """
        self.summaries.append(str(transcript or ""))
        material = f"{transcript}\n{previous_summary or ''}"
        filler = self._describe(transcript)
        blocks: List[str] = []
        for section in SECTION_HEADINGS:
            notes = [plant.marker for plant in PLANT_CLASSES if plant.marker in material
                     and plant.section == section]
            blocks.append(
                f"## {section}\n" + ("\n".join(notes) if notes else filler)
            )
        return "\n\n".join(blocks), "model_summary"

    @staticmethod
    def _describe(transcript: str) -> str:
        """Return an honest one-line description of what this window contains.

        Deterministic and derived only from the transcript, because a
        summariser's fallback text is as much a part of the fixture as its
        merge rule. Derived from the TOOLS the window mentions rather than from
        its length, so two windows with the same character count but different
        content do not produce the same sentence.
        """
        found = [
            tool
            for tool in ("write", "read", "edit", "test", "shell")
            if "[turn " in transcript and f" {tool}]" in transcript
        ]
        tools = ", ".join(found) if found else "no tool call"
        first = re.search(r"\[turn (\d+) ", transcript)
        last = re.findall(r"\[turn (\d+) ", transcript)
        span = (
            f"turns {first.group(1)}-{last[-1]}" if first and last else "this window"
        )
        return f"No durable note recorded for this section; window covers {span} ({tools})."

    # -- the agent loop ---------------------------------------------------

    def __call__(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> str:
        self.calls += 1
        step = str(kwargs.get("step") or "")
        text = _messages_text(messages)
        if step.startswith("context-compaction"):
            # Detect the REAL block by its CLOSING tag. `MERGE_INSTRUCTION` in
            # `incremental_summary.py` contains the literal text
            # `<previous-summary>` (it tells the summarizer what to merge), so
            # matching the opening tag reports every compaction as incremental —
            # which is exactly what the first draft of this benchmark did, and it
            # made the from-scratch arm look identical to the incremental one.
            # Only `render_previous_summary_block` emits the closing tag.
            has_previous = "</previous-summary>" in text
            self.incremental_calls.append(1 if has_previous else 0)
            # Return the SUMMARY TEXT ONLY. The model boundary
            # (``ModelGateway.call_fn``) is a text boundary; the
            # ``(summary, method)`` pair is the contract of the ``summarize``
            # callable that ``compact_tokens`` receives, which is a different
            # seam. Returning the tuple here made every compaction report
            # ``empty compaction summary`` -> ``structural_trim`` with
            # ``summary_chars: 0``, and every class read LOST in every arm.
            # The benchmark reported that as VACUOUS rather than as a result,
            # which is the only reason the mistake was visible at all.
            return self.summarize(
                text,
                (),
                previous_summary=text if has_previous else "",
            )[0]
        turn = len(self.seen) + 1
        self.seen.append((turn, text))

        # The last two turns resolve the recent-output class. Slotting it as a
        # real RE-FETCH is what makes that class behavioural rather than a claim:
        # if clearing removed the value, the agent has to notice the placeholder,
        # call the tool again, and only then answer. A model that simply claimed
        # it would re-read would be asserting the thing under test.
        if turn == self.turns - 1 and self._needs_refetch(text):
            self.refetched_at = turn
            return json.dumps({"tool": "read", "path": RECENT_OUTPUT_PATH})
        if turn >= self.turns:
            return json.dumps({"tool": "finish", "answer": self._final_answer(text)})
        if turn == 1:
            return self._churn(1)
        if turn == RECENT_OUTPUT_TURN:
            # The recent-output plant: a real tool result whose TAIL carries the
            # nonce. Read here and not earlier, so it is genuinely RECENT.
            #
            # This branch MUST sit above the `turn <= probe_from` arm. It did not
            # in the first draft, and because the gate turn (50) is above 45 the
            # plant turn was swallowed by the churn arm — so `big.txt` was never
            # read, the nonce was never in any request, and the class read LOST in
            # ALL FOUR ARMS including the baseline control. A class that fails in
            # the control is telling you about the fixture, not the mechanism.
            return json.dumps({"tool": "read", "path": RECENT_OUTPUT_PATH})
        if turn <= self.probe_from:
            for plant in PLANT_CLASSES:
                if plant.turn == turn:
                    return self._say(plant.marker)
            return self._churn(turn)
        if turn < self.turns - 1:
            index = (turn - self.probe_from) % len(PLANT_CLASSES)
            plant = PLANT_CLASSES[index]
            return json.dumps(
                dict(plant.obey if plant.marker in text else plant.violate)
            )
        return self._churn(turn)

    def _needs_refetch(self, text: str) -> bool:
        """Whether the tail value is gone AND a clearing placeholder explains it.

        Both halves are required. "The value is absent" alone is not a licence to
        re-read — it might never have been read. The placeholder is the evidence
        that the value was there and was reclaimed, which is the whole claim
        under test.
        """
        return self.nonce not in text and "tool result cleared" in text

    def _say(self, text: str) -> str:
        """Emit prose that becomes an assistant turn carrying the plant."""
        return f"Understood and recorded for the rest of this work. {text}"

    def _churn(self, turn: int) -> str:
        """Grow the context with something CLEARING CANNOT RECLAIM.

        A ``write`` result is classified ``run_authored``, so it is protected from
        clearing by construction and accumulates. That is deliberate: a churn of
        ``read`` calls would be reclaimed every turn, the window would stay under
        the threshold, and the compaction arms would measure nothing — the same
        vacuity the shipped 32 768 window has.

        The first draft of this fixture churned ``test`` with a ``path``
        argument. ``test`` accepts ``command`` / ``test_command`` and nothing
        else, so every churn call was a validation error and the run died at
        turn 24 with ``tool validation recovery limit`` — 26 turns short of the
        gate, which the benchmark correctly reported as VACUOUS rather than as a
        result. Worth recording: the vacuity check earned its place immediately.

        The size is 8 lines, and that is load-bearing rather than arbitrary. The
        compaction transcript is bounded and keeps its HEAD, so a churn fat
        enough to push the first compaction's dropped range past the bound
        silently amputates the plants nearest its tail. At 20 lines the turn-8
        plant fell outside a 24 000-character transcript, was therefore seen by
        NO summariser at all, and read LOST IN BOTH COMPACTION ARMS — which
        looks exactly like a merge failure and is not one.
        """
        filler = ("q" * 79 + "\n") * 8
        return json.dumps(
            {
                "tool": "write",
                "path": f"scratch/churn_{turn:03d}.py",
                "content": f"# churn turn {turn}\n{filler}",
            }
        )

    def _final_answer(self, text: str) -> str:
        """Answer the tail question truthfully or say it is unknown.

        The ONLY violation for the recent-output class is asserting a wrong
        value. Answering "I do not have that value" is COMPLIANT, because a
        model that says it does not know has not fabricated evidence — and a
        benchmark that scored honesty as failure would be measuring the opposite
        of what it claims.
        """
        if self.nonce in text:
            return f"TAIL={self.nonce}"
        if RECENT_OUTPUT_PATH in text:
            # The placeholder is present: the value was cleared. Re-fetch it.
            return f"TAIL-CLEARED re-read {RECENT_OUTPUT_PATH}"
        return "TAIL-UNKNOWN"


# --------------------------------------------------------------------------
# Violation detection, read from the authoritative journal
# --------------------------------------------------------------------------


def _journal_rows(log_root: Path, run_id: str) -> List[Dict[str, Any]]:
    """Return the run's journal rows, or ``[]`` when there is no journal."""
    path = Path(log_root) / run_id / "trace.jsonl"
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _turn_of(row: Mapping[str, Any]) -> int:
    """Return the integer turn a journal row belongs to, or ``0``."""
    match = re.search(r"(\d+)", str(row.get("turn_id") or ""))
    return int(match.group(1)) if match else 0


#: Each predicate answers "did this journal row commit the violation for this
#: class?" Reading them as DATA rather than as closures keeps the detection
#: auditable: a reader can see exactly what counts as a violation without
#: reading the loop.
VIOLATION_RULES: Mapping[str, str] = {
    "forbidden_path_write": (
        "a mutating tool_call whose target path is "
        f"{HARD_CONSTRAINT_PATH!r}"
    ),
    "rejected_alternative_read": (
        "a read of `planstore_backend.py`, the alternative the decision rejected"
    ),
    "silently_failing_call": (
        "a shell call whose command contains `close` - the call the gotcha says "
        "silently discards data"
    ),
    "retried_rejected_approach": (
        "a call naming `RetryForever`, the approach already proven to livelock"
    ),
    "unresolved_stated_as_resolved": (
        "a read of `RESOLVED-batch-size.md` - the agent treating an open "
        "question as settled"
    ),
    "answered_without_the_fact": (
        "a finish answer asserting a tail value that is neither the planted "
        "nonce nor an explicit statement of not knowing"
    ),
}


def _violates(plant: PlantClass, row: Mapping[str, Any]) -> bool:
    """Whether this journal row is a violation of ``plant``'s constraint."""
    if str(row.get("event") or "") != "tool_call":
        return False
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return False
    tool = str(payload.get("tool") or "")
    arguments = payload.get("arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}
    target = str(arguments.get("path") or arguments.get("command") or "")
    kind = plant.violation_predicate

    if kind == "forbidden_path_write":
        return tool in {"write", "edit", "apply_patch", "rename", "delete"} and (
            str(arguments.get("path") or "") == HARD_CONSTRAINT_PATH
        )
    if kind == "rejected_alternative_read":
        return tool == "read" and str(arguments.get("path") or "") == "planstore_backend.py"
    if kind == "silently_failing_call":
        return tool == "shell" and "close" in str(arguments.get("command") or "")
    if kind == "retried_rejected_approach":
        return "RetryForever" in target
    if kind == "unresolved_stated_as_resolved":
        return str(arguments.get("path") or "") == "RESOLVED-batch-size.md"
    return False


def _fabricated_tail(rows: Sequence[Mapping[str, Any]], nonce: str) -> Optional[int]:
    """Return the turn that asserted a wrong tail value, or ``None``.

    Scans ``finish`` answers for a ``TAIL=`` claim that is not the planted nonce.
    ``TAIL-UNKNOWN`` and ``TAIL-CLEARED`` are both COMPLIANT: the first is
    honesty about not knowing, the second is the re-fetch clearing obliges.
    """
    for row in rows:
        if str(row.get("event") or "") != "model_response":
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        text = str(payload.get("text") or "")
        match = re.search(r"TAIL=([A-Za-z0-9\-]+)", text)
        if match and match.group(1) != nonce:
            return _turn_of(row)
    return None


# --------------------------------------------------------------------------
# Per-class measurement
# --------------------------------------------------------------------------


@dataclass
class ClassResult:
    """One class in one arm: was it visible, and was it obeyed."""

    key: str
    planted_turn: int
    section: str
    #: Probe turns on which the marker was in the request.
    visible_turns: List[int] = field(default_factory=list)
    #: Probe turns on which it was NOT in the request.
    absent_turns: List[int] = field(default_factory=list)
    #: Turns whose journal row committed the violation.
    violated_turns: List[int] = field(default_factory=list)
    turns_probed: List[int] = field(default_factory=list)

    @property
    def visible_at_gate(self) -> bool:
        """Whether the marker was in the request on the gate turn itself."""
        return int(GATE_TURN) in set(self.visible_turns)

    @property
    def visible_at_any_probe(self) -> bool:
        """Whether the marker was ever in the request on a probe turn."""
        return bool(self.visible_turns)

    @property
    def lost(self) -> bool:
        """Whether the plant was gone by the gate turn.

        Read at the GATE, not "at some point". A marker visible on turn 50 and
        gone by turn 58 is exactly the decay this measures.
        """
        return not self.visible_at_gate

    @property
    def recovered_by_refetch(self) -> bool:
        """Whether a value absent at the gate came back by RE-FETCHING it.

        **This is the clearing-is-lossless result, and it is a different outcome
        from both "visible" and "lost".** Clearing is supposed to remove a
        re-fetchable result from the request; an agent that notices the
        placeholder and calls the tool again has lost nothing. Scoring that as
        ``lost`` would score the mechanism working as a mechanism failing, so it
        gets its own answer and its own column.

        Only meaningful for a class planted late enough to be cleared. An early
        class that vanishes from the request was COMPACTED, not cleared, and
        re-reading would not bring it back — which is why the receipt requires
        the placeholder to have been observed before this can be true.
        """
        if self.visible_at_gate:
            return False
        return bool([t for t in self.visible_turns if t > int(GATE_TURN)])

    @property
    def obeyed(self) -> bool:
        """Whether the run never committed the violation.

        Distinct from :attr:`visible_at_gate` on purpose. A marker the run never
        had nothing to violate, and a marker it had and ignored is the failure
        the brief names — so neither number is reported as the other.
        """
        return not self.violated_turns

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe record."""
        return {
            "key": self.key,
            "planted_turn": int(self.planted_turn),
            "section": self.section,
            "turns_probed": list(self.turns_probed),
            "visible_turns": list(self.visible_turns),
            "absent_turns": list(self.absent_turns),
            "visible_at_gate_turn": self.visible_at_gate,
            "visible_at_any_probe": self.visible_at_any_probe,
            "recovered_by_refetch": self.recovered_by_refetch,
            "lost": self.lost,
            "obeyed": self.obeyed,
            "violated_turns": list(self.violated_turns),
            "first_violation_turn": (
                self.violated_turns[0] if self.violated_turns else 0
            ),
        }


@dataclass
class ArmResult:
    """One arm's whole measurement."""

    arm: str
    clearing: bool
    incremental: bool
    window: int
    turns: int
    status: str
    compactions: int
    clearings: int
    incremental_compactions: int
    peak_tokens: int
    threshold_tokens: int
    classes: Dict[str, ClassResult] = field(default_factory=dict)
    note: str = ""
    defects: List[str] = field(default_factory=list)

    @property
    def survived(self) -> List[str]:
        """Classes that were both visible at the gate and obeyed."""
        return sorted(
            key
            for key, value in self.classes.items()
            if value.visible_at_gate and value.obeyed
        )

    @property
    def lost(self) -> List[str]:
        """Classes whose value was gone from the request and NOT recovered.

        A value that was cleared and then RE-FETCHED is deliberately not in this
        list: it was gone and it came back, which is the lossless property rather
        than a loss. Naming that separately is the difference between measuring
        clearing and being defeated by it.
        """
        return sorted(
            key
            for key, value in self.classes.items()
            if value.lost and not value.recovered_by_refetch
        )

    @property
    def refetched(self) -> List[str]:
        """Classes cleared out of the request and then recovered by re-fetching."""
        return sorted(
            key
            for key, value in self.classes.items()
            if value.recovered_by_refetch
        )

    @property
    def disobeyed(self) -> List[str]:
        """Classes whose constraint was violated at least once."""
        return sorted(key for key, value in self.classes.items() if not value.obeyed)

    @property
    def vacuous(self) -> bool:
        """Whether this arm measured nothing.

        A survival number from an arm that never compacted is not a pass; it is
        the absence of a measurement. Three ways to be vacuous, all reported:
        no compactions where compactions were required; an incremental arm whose
        chain never engaged; a from-scratch arm that lost nothing, which would
        mean the benchmark cannot detect the defect it exists to detect.
        """
        if self.arm == "clearing_from_scratch":
            return not self.lost
        if self.arm == "clearing_incremental":
            return self.compactions == 0 or self.incremental_compactions == 0
        return self.compactions > 0 and not self.arm.startswith("clearing_")

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe record."""
        return {
            "arm": self.arm,
            "clearing": self.clearing,
            "incremental": self.incremental,
            "window": int(self.window),
            "turns": int(self.turns),
            "status": self.status,
            "compactions": int(self.compactions),
            "clearings": int(self.clearings),
            "incremental_compactions": int(self.incremental_compactions),
            "peak_tokens": int(self.peak_tokens),
            "threshold_tokens": int(self.threshold_tokens),
            "vacuous": self.vacuous,
            "survived": self.survived,
            "lost": self.lost,
            "disobeyed": self.disobeyed,
            "classes": {key: value.as_dict() for key, value in self.classes.items()},
            "note": self.note,
            "defects": list(self.defects),
        }


# --------------------------------------------------------------------------
# Running one arm
# --------------------------------------------------------------------------


def _build_repo(root: Path) -> None:
    """Write the fixture repository. Every probe target exists as real content."""
    (root / "pkg" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "forbidden").mkdir(parents=True, exist_ok=True)
    (root / "allowed").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "Repository rules:\n- Never modify tests.\n", encoding="utf-8"
    )
    (root / "pkg" / "sub" / "AGENTS.md").write_text(
        "Sub-package rules:\n- This package uses tabs.\n", encoding="utf-8"
    )
    (root / "pkg" / "sub" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "unrelated.txt").write_text("nothing to see\n", encoding="utf-8")
    (root / "UNRESOLVED-batch-size.md").write_text(
        "# batch size\n\nUNRESOLVED - not yet verified.\n", encoding="utf-8"
    )
    (root / "RESOLVED-batch-size.md").write_text(
        "# batch size\n\n512 (assumed).\n", encoding="utf-8"
    )
    (root / "plancache_backend.py").write_text("BACKEND = 'plancache'\n", encoding="utf-8")
    (root / "planstore_backend.py").write_text("BACKEND = 'planstore'\n", encoding="utf-8")
    # The churn file. Its TAIL carries the nonce, because a capped or head-only
    # tool result would drop the tail and the recent-output class would be
    # measuring the cap rather than retention. Kept to ~8 000 characters so the
    # nonce survives `agent_conversation_tool_chars` without that key needing to
    # be inflated to make this benchmark's control arm pass.
    filler = ("z" * 79 + "\n") * 100
    (root / RECENT_OUTPUT_PATH).write_text(
        filler + f"\nTAIL {RECENT_OUTPUT_NONCE}\n", encoding="utf-8"
    )


def _config(arm: Arm) -> Dict[str, Any]:
    """Return the run config for one arm.

    ``context_incremental_compaction`` is set ONLY on the from-scratch arm, and
    only to ``False``. It is deliberately absent elsewhere so the shipped
    default (incremental) is what the other three arms run, and so no run in the
    project inherits it from here.
    """
    values: Dict[str, Any] = {
        "agent_approval": "auto",
        "steering_enabled": False,
        "context_window_tokens": int(arm.window),
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 8_000_000,
        # 20 000, not the shipped 12 000. The tail-fact file is ~8 000 chars and
        # its LAST line is the nonce; the tool-output cap keeps the HEAD, so at
        # 12 000 the nonce survived but at the smaller sizes it did not, and the
        # recent-output class failed in the BASELINE arm for a reason that had
        # nothing to do with clearing or compaction. A control arm must fail only
        # for the reason it exists to test.
        "agent_conversation_tool_chars": 20000,
        # 60 000, and THIS is the most consequential parameter in the file.
        # Despite the name, `context_compaction_input_tokens` bounds the
        # compaction transcript in CHARACTERS (prompts.render_compaction_prompt
        # does `body[:limit-16]`) and the bound keeps the HEAD. A plant therefore
        # survives only if some compaction's transcript head still reaches the
        # turn it was planted on.
        #
        # Measured on this fixture, not assumed. At the shipped 6 000 the first
        # compaction reaches back about four turns, so the turn-5, turn-8 and
        # turn-12 plants are outside EVERY summariser's reach: no summary ever
        # captures them and the incremental merge cannot carry what it never
        # saw — they read LOST IN BOTH COMPACTION ARMS. At 24 000 the turn-8
        # plant came back but the turn-12 plant still fell past a 34-message
        # head. 60 000 covers the whole first compaction's range.
        #
        # This is a real property of the shipped configuration and it is
        # recorded as :data:`TRANSCRIPT_WINDOW_FINDING` rather than quietly
        # tuned away. It is NOT a merge defect and this benchmark is not evidence
        # about it either way.
        "context_compaction_input_tokens": 60000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 400,
        "context_clearing_enabled": bool(arm.clearing),
    }
    if not arm.incremental:
        values["context_incremental_compaction"] = False
    return values


def run_arm(
    arm: Arm,
    base: Path,
    *,
    turns: int,
    probe_from: int,
) -> ArmResult:
    """Run one arm end to end and measure every class in it.

    ``probe_from`` is the first turn on which the agent starts being probed. It
    defaults to the gate turn. Probing every turn from the gate onwards — rather
    than only turn 50 — means a class that decays LATE is caught, and the
    per-class ``violated_turns`` list says when.
    """
    repo = base / arm.key
    repo.mkdir(parents=True, exist_ok=True)
    _build_repo(repo)
    log_root = base / "logs"
    run_id = f"decay-{arm.key}"

    model = _ChurnModel(turns=turns, probe_from=probe_from, nonce=RECENT_OUTPUT_NONCE)
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=log_root,
        model_gateway=ModelGateway(call_fn=model),
        config=_config(arm),
    )
    from shared.agent_contracts import RunSpec

    result = kernel.run(
        RunSpec(
            session_id=run_id,
            run_id=run_id,
            request="investigate the streaming writer and report what you find",
            repository_identity=str(repo),
        )
    )

    rows = _journal_rows(log_root, run_id)
    calls = [
        row
        for row in rows
        if str(row.get("event") or "") == "tool_call"
        and _turn_of(row) >= probe_from
    ]
    compactions = sum(
        1 for row in rows if str(row.get("event") or "") == "context_compacted"
    )
    clearings = sum(
        1 for row in rows if str(row.get("event") or "") == "context_cleared"
    )

    outcome = ArmResult(
        arm=arm.key,
        clearing=arm.clearing,
        incremental=arm.incremental,
        window=int(arm.window),
        turns=len(model.seen),
        status=str(getattr(result, "status", "") or ""),
        compactions=compactions,
        clearings=clearings,
        incremental_compactions=int(sum(model.incremental_calls)),
        peak_tokens=max(
            (
                len(text)
                for _turn, text in model.seen
            ),
            default=0,
        ),
        threshold_tokens=int(arm.window * 0.6),
        note=arm.note,
    )

    for plant in ALL_CLASSES:
        record = ClassResult(
            key=plant.key, planted_turn=plant.turn, section=plant.section
        )
        for turn, text in model.seen:
            if turn < probe_from:
                continue
            record.turns_probed.append(turn)
            probe_visible = (
                plant.marker in text
                or (plant.key == "recent_tool_output" and RECENT_OUTPUT_NONCE in text)
            )
            (record.visible_turns if probe_visible else record.absent_turns).append(turn)
        for row in calls:
            if plant.key == "recent_tool_output":
                continue
            if _violates(plant, row):
                record.violated_turns.append(_turn_of(row))
        if plant.key == "recent_tool_output":
            fabricated = _fabricated_tail(rows, RECENT_OUTPUT_NONCE)
            if fabricated:
                record.violated_turns.append(fabricated)
        record.violated_turns = sorted(set(record.violated_turns))
        outcome.classes[plant.key] = record

    # Defects are computed, never asserted, so a future change that breaks an
    # arm produces a NAMED finding instead of a quietly better-looking table.
    if arm.expects_compaction and compactions == 0:
        outcome.defects.append(
            f"expected compactions at window {arm.window} and got 0; this arm "
            "measured nothing"
        )
    if arm.incremental and arm.expects_compaction and not outcome.incremental_compactions:
        outcome.defects.append(
            "the incremental chain never engaged: no compaction request carried "
            "a <previous-summary> block"
        )
    if not arm.incremental and outcome.incremental_compactions:
        outcome.defects.append(
            "the from-scratch arm forwarded a previous summary; the defect was "
            "not actually switched off"
        )
    return outcome


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

#: The four verdicts. Never two, because "everything survived" and "nothing was
#: measured" are different findings and collapsing them is how a gate that
#: measures nothing comes to report a pass.
VERDICTS: Tuple[str, ...] = (
    "MEASURED_NO_DECISION_LOSS",
    "INCREMENTAL_ARMS_INDISTINGUISHABLE",
    "DECISION_LOSS_IN_INCREMENTAL_ARM",
    "VACUOUS",
)


def verdict(results: Mapping[str, ArmResult]) -> str:
    """Return the honest four-state verdict for the whole matrix."""
    for arm in results.values():
        if arm.vacuous:
            return "VACUOUS"
    incremental = results.get("clearing_incremental")
    scratch = results.get("clearing_from_scratch")
    if incremental is None or scratch is None:
        return "VACUOUS"
    if incremental.lost or incremental.disobeyed:
        return "DECISION_LOSS_IN_INCREMENTAL_ARM"
    if scratch.lost == incremental.lost and scratch.disobeyed == incremental.disobeyed:
        return "INCREMENTAL_ARMS_INDISTINGUISHABLE"
    return "MEASURED_NO_DECISION_LOSS"


def render_table(results: Mapping[str, ArmResult]) -> str:
    """Return the 6-class x 4-arm table plus the numbers that make it readable."""
    lines: List[str] = [
        "# Decision-decay benchmark",
        "",
        f"gate turn: {GATE_TURN}   (a class must be in the request HERE to count)",
        "",
    ]
    header = f"| {'class':<19} | {'planted':>7} | " + " | ".join(
        f"{name:<22}" for name in results
    ) + " |"
    lines.append(header)
    lines.append("|" + "-" * (len(header) - 2) + "|")
    for plant in ALL_CLASSES:
        cells: List[str] = []
        for name in results:
            record = results[name].classes.get(plant.key)
            if record is None:
                cells.append("n/a".ljust(22))
                continue
            if record.visible_at_gate and record.obeyed:
                mark = "OK"
            elif record.recovered_by_refetch and record.obeyed:
                mark = "RE-FETCHED"
            elif record.visible_at_gate:
                mark = "IGNORED"
            else:
                mark = "LOST"
            cells.append(f"{mark} (t{record.first_probe_seen()})".ljust(22))
        lines.append(
            f"| {plant.key:<19} | {plant.turn:>7} | " + " | ".join(cells) + " |"
        )
    lines += [
        "",
        "legend: OK = in the request at the gate AND obeyed | RE-FETCHED = cleared"
        " out of the request, then recovered by calling the tool again (clearing"
        " is lossless) | LOST = gone from the request and not recovered |"
        " IGNORED = present in the request and violated anyway",
        "",
    ]

    lines.append("per-arm mechanism counters:")
    lines.append("")
    lines.append(
        f"| {'arm':<24} | {'window':>8} | {'clear':>5} | {'compacts':>8} | "
        f"{'incremental':>11} | {'clearings':>9} | {'lost':>28} |"
    )
    lines.append("|" + "-" * 104 + "|")
    for name, arm in results.items():
        lines.append(
            f"| {name:<24} | {arm.window:>8} | {arm.clearing!s:>5} | "
            f"{arm.compactions:>8} | {arm.incremental_compactions:>11} | "
            f"{arm.clearings:>9} | {', '.join(arm.lost) or '(none)':>28} |"
        )
    lines += [
        "",
        f"VERDICT: {verdict(results)}",
        "",
        "violations actually committed (read from trace.jsonl, not self-reported):",
    ]
    for name, arm in results.items():
        detail = ", ".join(
            f"{key}@{arm.classes[key].violated_turns}"
            for key in arm.disobeyed
        )
        lines.append(f"  {name:<24} {detail or '(none)'}")
    defects = [
        f"  {name}: {item}" for name, arm in results.items() for item in arm.defects
    ]
    lines += ["", "arm defects:"] + (defects or ["  (none)"])
    lines += [
        "",
        "WHAT THIS DOES NOT ESTABLISH:",
        "  - every model reply is scripted, so this measures whether the HARNESS"
        " keeps a plant in the request and whether an agent that CAN see a "
        "constraint acts on it. It is not evidence about any real model's "
        "compliance, and the 73%/33% decay figures are Anthropic's measurements "
        "on their own harness, not reproduced here.",
        "  - the summariser is a scripted pure function of its material. That is "
        "what makes the two compaction arms a controlled differential, and it is "
        "also why this is a harness benchmark and not a model benchmark.",
        "  - classes are probed on a rotation from the gate turn onward (one tool "
        "call per turn), so a class's first probe is within five turns of the "
        "gate; per-class results are over every probe turn it was tested on.",
        f"  - {HONEST_FIXTURE_NOTE}",
    ]
    return "\n".join(lines)


def _first_probe_seen(self: ClassResult) -> int:
    """First probe turn on which the marker was visible, or ``0``."""
    return self.visible_turns[0] if self.visible_turns else 0


ClassResult.first_probe_seen = _first_probe_seen  # type: ignore[attr-defined]


def run_matrix(
    *,
    turns: int = DEFAULT_TURNS,
    window: int = COMPACTION_WINDOW,
    probe_from: int = GATE_TURN,
    base: Optional[Path] = None,
) -> Dict[str, ArmResult]:
    """Run all four arms in one temporary tree and return their measurements."""
    root = Path(base) if base is not None else Path(tempfile.mkdtemp(prefix="decay-"))
    root.mkdir(parents=True, exist_ok=True)
    out: Dict[str, ArmResult] = {}
    for arm in ARMS:
        chosen = Arm(
            key=arm.key,
            clearing=arm.clearing,
            incremental=arm.incremental,
            window=int(window) if arm.expects_compaction else arm.window,
            note=arm.note,
        )
        out[arm.key] = run_arm(chosen, root, turns=turns, probe_from=probe_from)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the matrix, print the table, and exit non-zero unless it measured."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--window", type=int, default=COMPACTION_WINDOW)
    parser.add_argument("--probe-from", type=int, default=GATE_TURN)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    results = run_matrix(
        turns=args.turns, window=args.window, probe_from=args.probe_from
    )
    if args.json:
        print(
            json.dumps(
                {
                    "gate_turn": GATE_TURN,
                    "verdict": verdict(results),
                    "arms": {name: arm.as_dict() for name, arm in results.items()},
                    "classes": [
                        {
                            "key": plant.key,
                            "turn": plant.turn,
                            "section": plant.section,
                            "why": plant.why,
                            "violation_rule": VIOLATION_RULES[plant.violation_predicate],
                        }
                        for plant in ALL_CLASSES
                    ],
                },
                indent=1,
            )
        )
    else:
        print(render_table(results))

    state = verdict(results)
    if state != "MEASURED_NO_DECISION_LOSS":
        print(f"\nNOT A PASS: verdict is {state}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
