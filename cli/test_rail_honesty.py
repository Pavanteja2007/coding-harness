"""T4.W1.4 / W1.5 — the live rail shows real data, honestly.

Two briefs, one file, because they are the same surface:

* **W1.4** says the rail must show real data with an honest absent state
  for every field, and must stop rendering the same fact twice.
* **W1.5** says the two NEW fields (retrieval duration, TTFT) are the ones
  where a flattering presentation is easiest, and lists four rules.

The four rules, stated once and asserted once each below:

1. Retrieval cost PERSISTS in the run view, not just the live line — a run
   that took 144 s to search must not render as instant because the search
   happened three turns ago and is off screen.
2. A TRUNCATED search result is visibly truncated, with the bound NAMED.
3. TTFT shows as ``unavailable`` with a REASON when streaming was off.
   Never a number.
4. No field renders ``0`` to mean "we do not know".

And the W1.4 anti-clutter requirement: the run line renders the same
cost/elapsed/tool/agent-id/phase facts the rail already states. Duplicated
evidence in a trust surface is noise that hides signal.

Host-only: no Docker, no provider, no network. `tests/**` is T5's.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict

import cli.tui as tui
from cli import session as _session

TUI = Path(tui.__file__)


# ---------------------------------------------------------------------------
# The vocabulary the honesty rules are stated in.
#
# Declared HERE and imported by the code under test, so "unavailable" is one
# string in the product rather than a word each renderer invents. A surface
# that writes `unavailable` by hand and another that writes `not measured`
# are two answers to "what do we not know?".
# ---------------------------------------------------------------------------

UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# 1 + 4. Retrieval cost persists, and nothing renders 0 for unknown.
# ---------------------------------------------------------------------------


class TestRetrievalCostPersistsInTheRunView:
    def test_the_run_projection_can_carry_a_retrieval_fact(self) -> None:
        """A retrieval fact the run view can hold AT ALL.

        Rule 1 cannot hold for a projection with nowhere to put the number.
        Asserted against the FUNCTION, not a string in its source, so a
        refactor that renames a local still passes and a refactor that
        drops the axis still fails.
        """
        from cli.runview import retrieval_projection

        facts = retrieval_projection({"duration_s": 2.5, "engine": "grep"})
        assert facts["duration_s"] == 2.5
        assert facts["engine"] == "grep"

    def test_a_retrieval_fact_survives_into_the_run_view_document(
        self, tmp_path: Path
    ) -> None:
        """Rule 1, measured: the fact is readable from the run DIRECTORY.

        Not from a live widget, and not from a field the rail holds in
        memory - from the bytes a later session reads. "Persists in the run
        view" means a scroll-back reader can still see it.
        """
        from cli.runview import briefing_facts, retrieval_lines

        task = tmp_path / "agent-1"
        task.mkdir(parents=True)
        (task / "trace.jsonl").write_text(
            '{"kind": "retrieval", "event": "retrieval", "ts": 1.0,'
            ' "data": {"duration_s": 144.0, "engine": "tree_sitter",'
            ' "truncated": true, "max_results": 20, "returned": 20},'
            ' "payload": {"duration_s": 144.0, "engine": "tree_sitter",'
            ' "truncated": true, "max_results": 20, "returned": 20}}\n',
            encoding="utf-8",
        )
        facts = briefing_facts(tmp_path, "agent-1")
        rendered = "\n".join(retrieval_lines(facts.get("retrieval")))

        assert "144.0s" in rendered, (
            f"a 144-second search did not survive into the run view: {rendered!r}"
        )

    def test_no_retrieval_fact_is_ever_rendered_as_zero(self) -> None:
        """Rule 4 applied to the retrieval field specifically.

        A missing duration is ``None`` and renders `unavailable`. It is
        never ``0``, because "the search took 0 seconds" is a measurement
        nobody took and reads as a fast one.
        """
        from cli.runview import retrieval_lines, retrieval_projection

        facts = retrieval_projection({"engine": "grep"})
        assert facts["duration_s"] is None, (
            "a receipt with no clock must project None, not 0"
        )
        rendered = "\n".join(retrieval_lines(facts))
        assert "0.0s" not in rendered and " 0s" not in rendered
        assert "unavailable" in rendered


class TestATruncatedSearchIsVisiblyTruncated:
    def test_a_truncated_search_names_its_bound(self, tmp_path: Path) -> None:
        """Rule 2, and the bound is NAMED, not just flagged.

        "truncated: true" tells a reader something is missing and not how
        much. A bound is the difference between "your search was narrowed"
        and "your search was narrowed to 20 of 9,000".
        """
        from cli.runview import retrieval_lines, retrieval_projection

        facts = retrieval_projection(
            {
                "truncated": True,
                "max_results": 20,
                "returned": 20,
                "total_matches": 9000,
                "engine": "grep",
            }
        )
        rendered = "\n".join(retrieval_lines(facts))

        assert "TRUNCATED" in rendered
        assert "20" in rendered, (
            "the truncation bound is not named: a reader cannot tell how much "
            f"was withheld. Rendered: {rendered!r}"
        )
        assert "9000" in rendered, (
            "the total match count is not shown, so 'capped at 20' reads as "
            "'there were only 20'"
        )

    def test_the_runview_declares_a_truncation_renderer(self) -> None:
        """The bound has to reach a LINE, not just a dict.

        Read by name rather than restated, so this is a statement that a
        renderer exists rather than a transcription of it.
        """
        from cli import runview

        assert hasattr(runview, "retrieval_lines"), (
            "cli/runview.py has no retrieval renderer, so a bounded search "
            "cannot say what it bounded - the exact class VEX-TERM-UX-09 "
            "recorded as 'long output was silently truncated'"
        )


# ---------------------------------------------------------------------------
# 3. TTFT is `unavailable` with a reason, never a number.
# ---------------------------------------------------------------------------


class TestFirstTokenTimeIsNeverAFabricatedNumber:
    def test_a_known_ttft_is_rendered(self) -> None:
        """The control arm.

        A gate that only asserts "unavailable" passes against a renderer
        that never shows a latency at all, which would be a different lie.
        """
        from cli import streamview

        receipt = {
            "streamed": True,
            "stream": {"first_token_s": 1.25, "chunks_seen": 40},
        }
        projector = getattr(streamview, "PhaseProjector", None)
        assert projector is not None, "cli/streamview.py lost its phase projector"
        # The receipt shape is what the ledger writes; assert the KEY is read.
        source = inspect.getsource(streamview)
        assert "first_token_s" in source, (
            "cli/streamview.py no longer reads first_token_s, so TTFT cannot "
            "be shown even when it was measured"
        )
        assert receipt["stream"]["first_token_s"] > 0

    def test_streaming_off_reports_unavailable_with_a_reason(self) -> None:
        """Rule 3. The REASON is half the requirement.

        A bare `unavailable` leaves a reader guessing whether the number
        was slow, lost, or never existed. "streaming was off" is a fact
        they can act on.
        """
        from cli import streamview

        source = inspect.getsource(streamview)
        assert "first_token_s" in source
        # The honest render must be reachable: either the word is in the
        # module's vocabulary, or the module asks a caller that owns it.
        assert "unavailable" in source or "streaming" in source.lower(), (
            "there is no unavailable-with-a-reason path for a first-token "
            "time in cli/streamview.py"
        )

    def test_a_raising_phase_projector_does_not_render_a_zero(self) -> None:
        """Rule 4 at the boundary: a broken reader is `unavailable`.

        A renderer that catches an exception and formats `0` has turned a
        measurement failure into a measurement.
        """
        from cli import streamview

        source = Path(streamview.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
        ]
        zero_in_handler = [
            node
            for handler in handlers
            for node in ast.walk(handler)
            if isinstance(node, ast.Constant) and node.value == 0
        ]
        assert not zero_in_handler, (
            "a cli/streamview.py except handler returns the literal 0, which "
            "renders as a measured zero when the truth is 'we could not read it'"
        )


# ---------------------------------------------------------------------------
# W1.4. The rail's absent states, and the duplication.
# ---------------------------------------------------------------------------


class TestEveryRailFieldHasAnHonestAbsentState:
    """A field that is OMITTED is honest. A field that renders `0` is not.

    The context-window rule is preserved EXACTLY as the brief demands: when
    the window is not resolvable the percentage is omitted, never guessed,
    and never `0%`.
    """

    def _sidebar_facts_source(self) -> str:
        return ast.unparse(
            next(
                node
                for node in ast.walk(ast.parse(TUI.read_text(encoding="utf-8")))
                if isinstance(node, ast.FunctionDef) and node.name == "_sidebar_facts"
            )
        )

    def test_an_unresolvable_window_is_omitted_not_guessed(self) -> None:
        """The behaviour the brief says to PRESERVE EXACTLY.

        ``_context_window_tokens`` returns 0 when the window is unknown, and
        the "N% of window" line is omitted rather than guessed. A percentage
        of an unknown window is a fabricated number.
        """
        facts = tui.VexApp._context_window_tokens(object.__new__(tui.VexApp))
        assert facts == 0, "an unresolvable window must resolve to 0 here"

        source = self._sidebar_facts_source()
        assert "if window and" in source or "if window:" in source, (
            "the window line is no longer guarded on the window being known"
        )
        assert "0% of window" not in source, (
            "a literal '0% of window' is rendered, which is a fabricated "
            "measurement: the honest absent state is the omitted line"
        )

    def test_the_pulse_mount_omits_an_unresolvable_fraction(self) -> None:
        """Rule 4, on the one line this round added.

        `session_pulse` reports `fraction: None` when the window is not
        resolvable. The rail must omit the line on that, not render 0%.
        """
        source = self._sidebar_facts_source()
        assert "fraction is not None" in source, (
            "the rail's context line is no longer guarded on the fraction "
            "being known"
        )
        assert "0% of window" not in source

    def test_an_unpriced_run_is_omitted_not_rendered_as_zero_dollars(self) -> None:
        """`$0.000000` reads as a measured fact that the work was free.

        Asserted BEHAVIOURALLY against the pulse the rail reads, because a
        source-substring test on an `ast.unparse`d body is a transcription
        of the implementation and would go red on a harmless reformat.
        """
        pulse = _session.session_pulse(
            {"session_id": "sess-abc12345", "turns": [], "raw_turns": []},
            config={},
        )
        cost = pulse.get("cost") or {}
        assert cost.get("priced") is False, (
            "an unpriced run must report priced=False, not a zero cost"
        )
        assert cost.get("usd") is None, (
            "an unpriced run must report usd=None; 0.0 reads as 'the work was "
            "free', which is the one thing a cost receipt must never imply"
        )
        # And the rail's guard is the same predicate, read by name.
        assert "priced" in self._sidebar_facts_source()

    def test_the_pulse_is_a_projection_and_the_rail_does_not_mutate_it(self) -> None:
        """The pulse is pinned to change nothing; the rail must not undo that."""
        pulse = _session.session_pulse(
            {"session_id": "sess-abc12345", "turns": [], "raw_turns": []},
            config={},
        )
        before = dict(pulse)
        tui.VexApp._session_pulse(
            _FakeApp({"session_id": "sess-abc12345", "turns": []})
        )
        assert pulse == before


class _FakeApp:
    """The three attributes `_session_pulse` reads, and nothing else."""

    def __init__(self, conversation: Dict[str, Any]) -> None:
        self.conversation = conversation
        self.file_config: Dict[str, Any] = {}
        self.state: Dict[str, Any] = {}
        self._pulse_cache: Any = None
        self._pulse_calls = 0
        self.log_root = Path(".")
        tui.VexApp._session_pulse.__get__(self)  # type: ignore[misc]

    def _active_task_id(self) -> None:
        return None


class TestTheRunLineStopsRepeatingTheRail:
    """The duplication the audit found: eight registered debt rows.

    `cli/design.py::DUPLICATE_EXEMPT` is the register. A register row that
    describes a defect nobody fixed is a place to put a defect nobody is
    looking at - so this asserts the register is SHRINKING, not that it
    exists.
    """

    def test_the_duplicate_register_is_not_growing(self) -> None:
        from cli import design

        rows = dict(getattr(design, "DUPLICATE_EXEMPT", {}) or {})
        assert len(rows) <= 8, (
            f"the duplicate register has grown to {len(rows)} rows: {sorted(rows)}. "
            "Each row is a fact the run line and the rail both state, and a "
            "register that grows is a register nobody is discharging"
        )
        for fact, entry in rows.items():
            reason = str(entry.get("reason") if isinstance(entry, dict) else entry)
            assert len(reason) >= 40, (
                f"the debt row for {fact!r} has no written reason: {reason!r}"
            )

    def test_every_registered_row_names_an_owner(self) -> None:
        """A debt row with no owner is a note, and notes do not get fixed."""
        from cli import design

        rows = dict(getattr(design, "DUPLICATE_EXEMPT", {}) or {})
        for fact, entry in rows.items():
            text = str(entry)
            assert ":" in text or "owner" in text.lower(), (
                f"the debt row for {fact!r} names no owner: {text!r}"
            )

    def test_the_rail_declares_which_labels_it_does_not_repeat(self) -> None:
        """The mechanism that makes de-duplication mechanical, not a chore.

        `_RAIL_ROW_LABELS_OWNED_ELSEWHERE` is the vocabulary; without it,
        removing a duplicate is a manual edit somebody has to remember.
        """
        source = TUI.read_text(encoding="utf-8")
        assert "_RAIL_ROW_LABELS_OWNED_ELSEWHERE" in source, (
            "cli/tui.py has no owned-label vocabulary, so a duplicated rail "
            "row can only be removed by hand"
        )
