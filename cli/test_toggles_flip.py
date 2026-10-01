"""T4.W1.3 - the tri-state toggle: one vocabulary, an advance that advances,
and a corrupt store that fails safe.

`tests/test_toggles.py` does not exist; the tri-state toggle was pinned only
by `tests/test_design_layout.py` (a vocabulary check) and by the TUI's own
round-trip test. Neither could catch the defect this file exists for:

    ToggleSettings.flip advanced ONCE and then stalled forever.

The cause was NOT the one the handoff recorded. It was not that `flip`
indexed `TRISTATE_VALUES` - it reads `spec.values`, which resolves to the
layout authority's vocabulary. The cause was one token: `flip` read
``spec.default`` instead of the LIVE value, so the proposed next value was
always the same one and `set` refused it as "already always" from the second
press onward. A test that flips once passes against the broken code, which
is why the handoff's proposed "flip 3 times" test matters: this file flips
TEN times and checks the CYCLE, because a stall is only visible after the
second advance.

Host-only: no Docker, no provider, no network, and every store is written
under ``tmp_path`` through an explicit ``home``, so a test run can never
touch a developer's real preferences.
"""

from __future__ import annotations

import ast
import itertools
import json
from pathlib import Path
from typing import List

import pytest

from cli import design, toggles

SIDEBAR = "sidebar"
CYCLE = ("auto", "show", "hide")


def _tui_action() -> ast.FunctionDef:
    """`cli/tui.py::VexApp.action_toggle_sidebar`, read as an AST node.

    Parsed rather than imported so the pin survives a module that cannot be
    imported in isolation, and so a reformat cannot empty the check the way
    a substring test can.
    """
    source = Path(toggles.__file__).with_name("tui.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "action_toggle_sidebar":
            return node
    raise AssertionError("cli/tui.py must keep action_toggle_sidebar")


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """An isolated Vex home, so no test reads or writes a real store."""
    target = tmp_path / "vex-home"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _advance(settings: toggles.ToggleSettings, times: int = 10) -> List[str]:
    seen = [str(settings.get(SIDEBAR))]
    for _ in range(times):
        changed, _note = settings.flip(SIDEBAR, persist=False)
        assert changed is True, (
            f"flip refused at {len(seen)}: a tri-state advance that refuses is "
            f"a stall, and a stall looks exactly like a working toggle that "
            f"happened to be at its maximum"
        )
        seen.append(str(settings.get(SIDEBAR)))
    return seen


class TestTheTriStateVocabularyIsOne:
    def test_the_toggle_registry_and_the_layout_speak_the_same_three_words(
        self,
    ) -> None:
        assert toggles.TRISTATE_VALUES == CYCLE
        assert tuple(toggles.TOGGLE_VALUES[SIDEBAR]) == CYCLE
        assert tuple(design.SIDEBAR_MODES) == CYCLE

    def test_every_word_round_trips_through_the_layout_normaliser(self) -> None:
        for word in CYCLE:
            assert design.normalize_sidebar_mode(word) == word

    def test_the_old_spellings_load_but_are_never_written(self) -> None:
        """A hand-edited store keeps working; nothing new is written that way.

        ``shown``/``hidden`` were the second vocabulary. They are now input
        aliases: accepted, canonicalised, never persisted. Deleting them
        outright would break a store somebody edited by hand, which is a
        data-loss bug traded for a tidier table.
        """
        assert toggles.TOGGLE_VALUE_ALIASES.get("shown") == "show"
        assert toggles.TOGGLE_VALUE_ALIASES.get("hidden") == "hide"
        for legacy in ("shown", "hidden"):
            assert legacy not in toggles.TRISTATE_VALUES
            assert toggles.TOGGLE_BY_NAME[SIDEBAR].coerce(legacy) in CYCLE

    def test_no_vocabulary_lives_outside_the_two_authorities(self) -> None:
        """A third spelling anywhere is a third answer to "what is hide?".

        Scans the module's own constants rather than restating them, so a
        new literal cannot be added without this going red.
        """
        assert set(toggles.TRISTATE_VALUES) <= set(design.SIDEBAR_MODES)
        for name, values in toggles.TOGGLE_VALUES.items():
            assert set(values) <= set(design.SIDEBAR_MODES), (
                f"{name} declares words the layout authority does not know"
            )


class TestTheAdvanceActuallyAdvances:
    def test_ten_flips_walk_the_whole_cycle_and_repeat(self) -> None:
        """THE TEST A ONE-FLIP TEST CANNOT BE.

        Measured on the pre-fix tree: flip 0 succeeded, then every flip
        returned ``(False, 'sidebar is already always')`` forever. A test
        that flips once passes against that.

        The expected sequence is DERIVED from the declared cycle rather than
        written out, so reordering the control does not make this test a
        transcription of the implementation - it stays a statement about the
        walk, not about the literal words.
        """
        settings = toggles.ToggleSettings()
        seen = _advance(settings, 10)
        start = CYCLE.index(seen[0])
        expected = [CYCLE[(start + step) % len(CYCLE)] for step in range(11)]

        assert seen == expected, (
            f"ten flips did not walk the cycle.\n  got      {seen}\n  expected {expected}"
        )
        assert len(set(seen)) == 3, "the walk never reached all three states"

    def test_a_whole_number_of_cycles_returns_to_the_start(self) -> None:
        """The periodicity claim, separate from the walk.

        Twelve flips is four whole cycles. Asserting this separately from the
        ten-flip walk is deliberate: 10 is not a multiple of three, so a
        ten-flip test cannot state periodicity, and a test that quietly
        claimed it would be asserting something the ten flips did not show.
        """
        settings = toggles.ToggleSettings()
        start = str(settings.get(SIDEBAR))
        seen = _advance(settings, 12)

        assert seen[12] == start
        assert seen[3] == start and seen[6] == start and seen[9] == start

    def test_no_advance_is_a_no_op(self) -> None:
        """Every flip must actually change the value.

        This is the control arm for the test above: a walk that returned
        ``True`` while the value never moved would satisfy "the flip
        succeeded" and answer nothing.
        """
        settings = toggles.ToggleSettings()
        seen = _advance(settings, 10)
        for before, after in itertools.pairwise(seen):
            assert before != after, f"flip reported success but {before!r} did not move"

    def test_the_advance_works_from_a_saved_non_default_value(self) -> None:
        """The pre-fix bug was reading the default instead of the LIVE value.

        Starting from a store that says ``hide``, the first advance must
        reach ``auto`` - the thing the broken version could not do from any
        starting point.
        """
        settings = toggles.ToggleSettings()
        settings.set(SIDEBAR, "hide", persist=False)
        assert str(settings.get(SIDEBAR)) == "hide"

        _changed, note = settings.flip(SIDEBAR, persist=False)

        assert str(settings.get(SIDEBAR)) == "auto", note

    def test_a_refused_value_does_not_poison_the_advance(self) -> None:
        """A garbage value in a store must not wedge the toggle.

        The store coerces on read, so this simulates the in-memory case a
        caller can produce by writing the object directly. The next flip
        must still move.
        """
        settings = toggles.ToggleSettings()
        settings.values[SIDEBAR] = "nonsense"

        changed, _note = settings.flip(SIDEBAR, persist=False)

        assert changed is True
        assert str(settings.get(SIDEBAR)) in CYCLE

    def test_a_boolean_toggle_still_toggles_rather_than_advancing(self) -> None:
        """The tri-state fix must not have changed the boolean path."""
        settings = toggles.ToggleSettings()
        start = settings.get("timestamps")
        settings.flip("timestamps", persist=False)
        assert settings.get("timestamps") is (not start)
        settings.flip("timestamps", persist=False)
        assert settings.get("timestamps") is start


class TestTheAdvanceRoundTripsThroughPersistence:
    def test_ten_flips_survive_a_write_and_a_reload(self, home: Path) -> None:
        """Persisting is the half that was never proven.

        An advance that works in memory and stalls on the second press
        after a reload is the same defect wearing a different hat.
        """
        sid = "sess-roundtrip"
        first = toggles.load_settings(session_id=sid, home=home)
        seen = _advance(first, 10)
        wrote, note = first.flush()
        assert wrote is True, f"the ten flips did not persist: {note}"

        reloaded = toggles.load_settings(session_id=sid, home=home)
        assert str(reloaded.get(SIDEBAR)) == seen[-1], (
            f"the reloaded value {reloaded.get(SIDEBAR)!r} is not the value "
            f"the ten flips ended on ({seen[-1]!r})"
        )
        assert reloaded.source(SIDEBAR) == "session"

    def test_the_reloaded_toggle_still_advances(self, home: Path) -> None:
        """The stall must not be waiting for the first reload.

        The pre-fix defect was invisible in-memory for one press and fatal
        after any. This is the case that proves the fix is in the LOGIC and
        not in a cache that happens to be warm.
        """
        sid = "sess-advance-after-reload"
        toggles.load_settings(session_id=sid, home=home).flip(SIDEBAR, persist=True)
        reloaded = toggles.load_settings(session_id=sid, home=home)
        start = str(reloaded.get(SIDEBAR))

        after = _advance(reloaded, 3)

        assert after[0] == start
        assert len(set(after)) == 3, f"the reloaded toggle did not cycle: {after}"

    def test_a_written_store_never_holds_a_word_the_reader_would_refuse(
        self, home: Path
    ) -> None:
        """Write filters through `coerce`, so the file cannot lie to the reader."""
        sid = "sess-write-filter"
        settings = toggles.load_settings(session_id=sid, home=home)
        settings.set(SIDEBAR, "shown", persist=False)
        settings.flush()

        stored = json.loads(
            Path(toggles.session_toggle_path(sid, home=home)).read_text(
                encoding="utf-8"
            )
        )
        assert stored["toggles"][SIDEBAR] == "show", (
            "an alias was written instead of the canonical word"
        )
        values, note = toggles.read_store(toggles.session_toggle_path(sid, home=home))
        assert note == "", f"the store this product just wrote is not readable: {note}"
        assert values[SIDEBAR] == "show"


class TestACorruptStoreFailsSafe:
    """Fail CLOSED, and never land on a half-advanced value.

    The failure this guards against is specific: a corrupt store that
    resolved to some intermediate tri-state would look exactly like a
    user's saved choice, and the shell would honour it. The answer must be
    the DEFAULT plus a note, or a refusal - never a plausible value.
    """

    def _assert_defaults(self, settings: toggles.ToggleSettings) -> None:
        assert str(settings.get(SIDEBAR)) == toggles.TOGGLE_DEFAULTS[SIDEBAR]
        assert settings.source(SIDEBAR) == "default", (
            "a corrupt store produced a value that reads as a user's choice"
        )

    def test_a_store_that_is_not_json_falls_back_to_the_defaults(
        self, home: Path
    ) -> None:
        path = toggles.session_toggle_path("sess-bad-json", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")

        settings = toggles.load_settings(session_id="sess-bad-json", home=home)

        self._assert_defaults(settings)
        assert any("defaults are in force" in note for note in settings.notes)

    def test_a_store_that_is_not_an_object_falls_back_to_the_defaults(
        self, home: Path
    ) -> None:
        path = toggles.session_toggle_path("sess-not-object", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")

        settings = toggles.load_settings(session_id="sess-not-object", home=home)

        self._assert_defaults(settings)

    def test_a_store_from_a_future_version_is_refused_whole(
        self, home: Path
    ) -> None:
        """A version this build does not understand is not partially read.

        Reading the keys it happens to recognise out of an unknown schema
        is how a preferences file written by a newer build silently
        reinterprets its meaning here.
        """
        path = toggles.session_toggle_path("sess-future", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 9999, "toggles": {SIDEBAR: "hide"}}),
            encoding="utf-8",
        )

        settings = toggles.load_settings(session_id="sess-future", home=home)

        self._assert_defaults(settings)
        assert any("unsupported store version" in note for note in settings.notes)

    def test_a_store_holding_an_unusable_value_falls_back_to_the_defaults(
        self, home: Path
    ) -> None:
        """``"sideways"`` is not a sidebar mode, and must not become one."""
        path = toggles.session_toggle_path("sess-bad-value", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"version": toggles.STORE_VERSION, "toggles": {SIDEBAR: "sideways"}}
            ),
            encoding="utf-8",
        )

        settings = toggles.load_settings(session_id="sess-bad-value", home=home)

        self._assert_defaults(settings)
        assert any("unusable value refused" in note for note in settings.notes)

    def test_a_string_false_is_not_a_boolean_false(self, home: Path) -> None:
        """A non-empty string is truthy, and a knob that read it as ON is the
        same defect class as a verifier reporting unverified work as verified."""
        path = toggles.session_toggle_path("sess-string-false", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": toggles.STORE_VERSION,
                    "toggles": {"timestamps": "false"},
                }
            ),
            encoding="utf-8",
        )

        settings = toggles.load_settings(session_id="sess-string-false", home=home)

        assert settings.get("timestamps") is toggles.TOGGLE_DEFAULTS["timestamps"]
        assert settings.source("timestamps") == "default"

    def test_a_corrupt_store_does_not_wedge_the_advance(self, home: Path) -> None:
        """Recovery means the key WORKS, not merely that it reads a default.

        A store that resolves to a default and then refuses every flip is
        the same stall wearing a persistence costume.
        """
        sid = "sess-corrupt-recover"
        path = toggles.session_toggle_path(sid, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")

        settings = toggles.load_settings(session_id=sid, home=home)

        assert _advance(settings, 3)[3] == "auto"

    def test_a_refused_write_is_reported_not_swallowed(self, home: Path) -> None:
        """A preference that looks saved and was not is the defect class."""
        settings = toggles.load_settings(session_id="sess-no-write", home=home)
        # No session id: `flush` cannot write a session store, and says so.
        naked = toggles.ToggleSettings(home=home)
        naked.set(SIDEBAR, "hide", persist=False)

        wrote, note = naked.flush()

        assert wrote is False
        assert "not persisted" in note
        assert str(naked.get(SIDEBAR)) == "hide", (
            "the in-memory value moved even though nothing was saved - which "
            "is correct, and is exactly why the note has to exist"
        )
        assert settings is not None


class TestTheTuiNoLongerWorksAroundTheToggle:
    def test_the_shell_delegates_the_advance_to_the_registry(self) -> None:
        """A workaround that outlives its cause is a second bug.

        `action_toggle_sidebar` used to compute the next mode from
        `design_mode_order()` and write it with `set()` on EVERY press,
        because `flip` was recorded as stalling. With `flip` fixed, the
        unconditional version keeps two advance rules in the product and a
        docstring standing as an anti-claim about code that now works.

        `set` is still reachable, and deliberately so - see the next test.
        What must not survive is an UNCONDITIONAL hand-rolled cycle.
        """
        import ast

        action = _tui_action()
        source = ast.unparse(action)

        assert ".flip(" in source, (
            "action_toggle_sidebar no longer calls ToggleSettings.flip at all; "
            "the hand-rolled cycle this replaced is a second advance rule"
        )
        assert "design_mode_order()" in source, (
            "the explicit-vs-unset branch is gone; an unchosen shell shows "
            "`show` over a registry that says `auto`, so advancing the "
            "registry blindly would make the FIRST press a no-op"
        )

    def test_the_hand_rolled_cycle_is_gated_on_the_user_having_chosen_nothing(
        self,
    ) -> None:
        """The gate is an explicit-vs-unset check, not a flag.

        `design.DEFAULT_SIDEBAR_MODE` is "show" and
        `toggles.TOGGLE_DEFAULTS["sidebar"]` is "auto", so at startup the
        shell shows one and the registry says another. The first press has
        to move what the user SEES. This reads the source to prove the
        hand-rolled branch is inside a conditional rather than the only
        path, because a test that only counted `set` calls would pass
        against the unconditional version too.
        """
        import ast

        action = _tui_action()
        gated = [
            node
            for node in ast.walk(action)
            if isinstance(node, ast.If)
            and "unchosen" in ast.unparse(node.test)
        ]
        assert gated, (
            "the hand-rolled cycle is no longer inside an `if unchosen:` - it "
            "is unconditional again, which is the workaround this file exists "
            "to keep deleted"
        )
        hand_rolled = ast.unparse(gated[0])
        assert ".set(" in hand_rolled and "design_mode_order()" in hand_rolled, (
            "the unchosen branch no longer writes the next visible mode"
        )
        # And the registry owns every OTHER press.
        else_branch = [node for node in gated[0].orelse]
        assert else_branch, "the unchosen branch has no else: every press hand-rolls"
        assert ".flip(" in ast.unparse(else_branch[0]), (
            "a press after the user has chosen a mode must go through flip"
        )

    def test_the_shell_never_invents_a_mode_the_store_disagrees_with(self) -> None:
        """When the registry refuses, the shell says so and stays put.

        Showing the next mode anyway would be the store and the screen
        disagreeing, which is the exact state the read-back half of this
        method exists to prevent.
        """
        import ast

        action = _tui_action()
        refused = [
            node
            for node in ast.walk(action)
            if isinstance(node, ast.If)
            and "resolved == current" in ast.unparse(node.test)
        ]
        assert refused, "the refused-registry branch is gone"
        branch = ast.unparse(refused[0])
        assert "resolved = current" in branch, (
            "a refused advance must keep the current mode, not substitute a "
            "mode the registry does not hold"
        )
