"""VEX-PF-06 - the first run, the empty states, and help that teaches.

One class per required behaviour of the brief, one test per behaviour, named
after the behaviour rather than after the function that implements it. Every
test here is host-only: no Docker, no provider, no network, no credential.

The three claims this file is able to FAIL on, and why each of them was worth
a test rather than a comment:

* a task-phrased question that names no command reaches the right command
  FIRST (the measured pre-round result for "show me what changed" was
  `/help`, and for "stop the run" was `/detach`);
* every declared empty state produces a non-empty sentence AND a runnable
  action that the live registry actually declares - an empty state that
  teaches a command this build does not have is a dead end with better
  typography;
* the first screen carries all three required beats and all three affordances
  at every terminal width, and nothing it prints overflows the width.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List

import pytest

from cli import fuzzy
from cli import interactive as iv
from cli import onboarding as ob
from cli import tui_components as tc

#: The exact shape of the first-run budget, and the width the pre-round
#: suite pinned. Both are declared here so a change to either is a change to a
#: number somebody reads.
FIRST_RUN_WIDTHS = (40, 50, 60, 72, 78, 100, 120, 200)

#: The three commands a newcomer must be able to find on the FIRST screen,
#: without reading help. Exactly three, and that is the anti-clutter answer.
AFFORDANCE_COMMANDS = ("/diff", "/undo", "/cancel")


class _Say:
    """A `say`/`print` sink that records the raw markup it was handed."""

    def __init__(self) -> None:
        self.rows: List[str] = []

    def __call__(self, text: Any = "") -> None:
        self.rows.append(str(text))

    def print(self, text: Any = "") -> None:  # rich Console compatibility
        self.rows.append(str(text))

    @property
    def plain(self) -> str:
        from rich.markup import escape

        return "\n".join(escape(row) for row in self.rows)


def _strip_markup(row: str) -> str:
    """Drop rich/Textual style tags so a line can be asserted on as TEXT.

    Only used for assertions about wording and counts. It is not a safety
    measure - the safety property is proved by RENDERING through a real
    Console and reading back what survived, which is what the markup-safety
    class does.
    """
    return re.sub(r"\[[^\]]*\]", "", str(row))


def _devnull_console(width: int = 200):
    """A real rich Console whose output goes nowhere.

    Used to prove a rendered line does not RAISE in a real parser, and to
    measure what a TERMINAL would show. A substring assertion would pass while
    the message was being eaten; a render that raises is the failure mode this
    file exists to prevent.
    """
    from rich.console import Console

    return Console(width=width, file=open(os.devnull, "w", encoding="utf-8"))


def _render_widths(lines: List[str], width: int) -> dict:
    """Row count, widest row, and overflow count as a real Console renders them.

    Counting an escaped markup string measures the TAGS, not the terminal, and
    the first version of the overflow gate did exactly that and reported a
    defect that was not there.
    """
    from rich.console import Console

    with open(os.devnull, "w", encoding="utf-8") as sink:
        console = Console(
            width=width, record=True, file=sink, no_color=True, legacy_windows=False
        )
        for line in lines:
            console.print(line)
        rendered = console.export_text().splitlines()
    return {
        "rendered_rows": len(rendered),
        "widest_rendered": max((len(row) for row in rendered), default=0),
        "overflowing_rows": sum(1 for row in rendered if len(row) > width),
    }


# ---------------------------------------------------------------------------
# 1. THE FIRST RUN IS A GENUINE FIRST RUN
# ---------------------------------------------------------------------------


class TestTheFirstRunTeachesWhatToDo:
    def test_the_first_run_says_what_neo_is(self):
        """The screen answers "what IS this", not "there is a prompt"."""
        text = "\n".join(ob.first_run_lines("repo", "logs", width=78))
        assert "Neo" in text
        # The promise is about VERIFICATION, which is the product's claim.
        assert "verified when the tests actually pass" in text

    def test_the_first_run_carries_one_worked_example(self):
        """A first screen with nothing to try is a wall, however short it is.

        One example, in the two shapes a person actually types — and the
        difference between them, whether anything changes on disk, is the
        thing a newcomer cannot guess. So the example is ONE field with two
        rows, not two fields a reader has to compare.
        """
        examples = ob.FirstRun().examples()
        assert len(examples) == 2, "one example, in the two shapes a person uses"
        rows = [
            row
            for row in ob.first_run_lines("repo", "logs", width=120)
            if row.startswith("ask")
        ]
        assert len(rows) == 2, rows
        for label, body in examples:
            assert label == "ask"
            assert body.strip().startswith('"')
        text = "\n".join(rows)
        assert "changes nothing" in text
        assert "shows the diff" in text

    def test_the_first_run_names_exactly_one_next_action(self):
        """One next action. Two is a menu, and a menu is a question back."""
        assert ob.next_action(connected=False) == ob.NEXT_ACTION_WITHOUT_PROVIDER
        assert ob.next_action(connected=True) == ob.NEXT_ACTION_WITH_PROVIDER
        for connected in (True, False):
            rendered = ob.first_run_lines("r", "l", width=120, connected=connected)
            next_rows = [row for row in rendered if row.startswith("next")]
            assert len(next_rows) == 1, next_rows
        # The branch is chosen by the FACT (is a provider connected), never by
        # a preference, and the two branches are genuinely different.
        without = "\n".join(ob.first_run_lines("r", "l", width=120, connected=False))
        with_provider = "\n".join(
            ob.first_run_lines("r", "l", width=120, connected=True)
        )
        assert "/connect adds a provider" in without
        assert "/connect adds a provider" not in with_provider
        assert "/help" in with_provider

    def test_the_first_run_is_not_two_lines_and_a_prompt(self):
        """The brief's own words, as a bound rather than an adjective."""
        lines = ob.first_run_lines("C:/repo/x", "C:/logs", model="m", width=78)
        assert 6 <= len(lines) <= ob.MAX_FIRST_RUN_LINES
        # And the same bound holds at every width the pre-round suite pinned.
        for width in FIRST_RUN_WIDTHS:
            rendered = ob.first_run_lines(
                "C:/repo/x", "C:/logs", model="m", width=width
            )
            assert rendered, width
            for required in ("welcome", "what", "ask", "next", "keys"):
                assert any(row.startswith(required) for row in rendered), (
                    width,
                    required,
                )

    def test_no_line_of_the_first_run_overflows_the_width(self):
        """A sentence cut at the terminal edge is a sentence that is not true."""
        for width in FIRST_RUN_WIDTHS:
            for line in ob.first_run_lines(
                "C:/repo/x", "C:/logs", model="m", width=width
            ):
                assert len(line) <= width, (width, len(line), line)

    def test_the_rendered_first_run_does_not_overflow_at_any_width(self):
        """The PLAIN producer's bound and the RENDERED width are different
        measurements, and only the second is what a terminal shows."""
        for width in FIRST_RUN_WIDTHS:
            rendered = _render_widths(
                iv.render_first_run("C:/repo/x", "C:/logs", model="m", width=width),
                width,
            )
            assert rendered["overflowing_rows"] == 0, (width, rendered)

    def test_the_first_run_omits_an_unset_fact_instead_of_rendering_a_dash(self):
        """A row reading `model -` looks like a failed lookup, not "unset"."""
        text = "\n".join(ob.first_run_lines(None, None, width=100))
        assert "model" not in text
        assert "logs" not in text
        # `provider` is always present because "none yet" is itself actionable.
        assert "provider none yet" in text

    def test_the_first_run_is_total(self):
        """A caller that cannot resolve a fact omits it rather than erroring."""
        for args in ((None, None), ("", ""), (Path("/repo/x"), Path("/logs/y"))):
            lines = ob.first_run_lines(*args)
            assert lines
            assert "welcome" in "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. THE THREE AFFORDANCES ARE ON THE FIRST SCREEN
# ---------------------------------------------------------------------------


class TestTheThreeAffordancesAreDiscoverable:
    def test_undo_see_what_changed_and_stop_the_run_are_all_on_the_first_screen(self):
        """Discoverable from the first screen, not from reading help."""
        text = "\n".join(ob.first_run_lines("repo", "logs", width=120))
        for command in AFFORDANCE_COMMANDS:
            assert command in text, command

    def test_there_are_exactly_three_affordances(self):
        """Three clears the anti-clutter threshold; two would not render."""
        assert len(ob.AFFORDANCES) == 3
        assert tuple(a.command for a in ob.AFFORDANCES) == AFFORDANCE_COMMANDS

    def test_the_affordance_row_is_one_line_at_eighty_columns(self):
        """Measured, not asserted: the row fits the terminal people use."""
        assert len(ob.affordance_lines(width=78)) == 1
        assert len(ob.affordance_lines(width=120)) == 1
        assert len(ob.affordance_lines(width=200)) == 1

    def test_the_affordance_row_never_drops_an_affordance(self):
        """Narrow is a reason to wrap, never a reason to lose a control."""
        for width in FIRST_RUN_WIDTHS:
            text = "\n".join(ob.affordance_lines(width=width))
            for command in AFFORDANCE_COMMANDS:
                assert command in text, (width, command)
            for line in ob.affordance_lines(width=width):
                assert len(line) <= width, (width, line)

    def test_every_affordance_key_is_a_key_the_registry_declares(self):
        """An affordance advertising an unbound key is the same lie as help
        promising a command that does not exist."""
        from cli import commands as commands_mod

        declared: set[str] = set()
        for row in commands_mod.keyboard_shortcuts()["commands"]:
            declared.update(row["keys"])
        assert declared, "the registry declares no keys; this gate cannot run"
        keys = ob.resolve_keys()
        for affordance in ob.AFFORDANCES:
            if affordance.command in keys:
                assert keys[affordance.command].split(" / ")[0] in declared
        # Whatever the row actually PRINTS as a key, the registry declared it.
        printed = " ".join(ob.affordance_lines(width=200))
        for match in re.findall(r"\((ctrl\+[a-z0-9])\)", printed):
            assert match in declared, match

    def test_every_affordance_command_is_a_real_command(self):
        """A door with no room behind it is worse than no door."""
        names = set(ob.command_names())
        assert names, "the registry was unreachable; this gate cannot run"
        for affordance in ob.AFFORDANCES:
            assert affordance.command in names, affordance.command
            assert affordance.because.strip(), affordance.id


# ---------------------------------------------------------------------------
# 3. EVERY EMPTY STATE IS AN ACTIONABLE SENTENCE
# ---------------------------------------------------------------------------


class TestEmptyStatesAreNeverBlank:
    def test_the_seven_states_the_brief_names_are_all_declared(self):
        """Asserted as a SET, so a state cannot quietly go missing."""
        assert set(ob.REQUIRED_EMPTY_STATES) <= set(ob.EMPTY_STATES)

    @pytest.mark.parametrize("state_id", sorted(ob.EMPTY_STATES))
    def test_every_state_renders_one_sentence_and_a_next_step(self, state_id: str):
        """One actionable sentence, plus the door that fills the panel."""
        lines = ob.empty_state_lines(state_id, width=100)
        assert lines, state_id
        state = ob.empty_state(state_id)
        assert state.sentence.strip().endswith("."), state.sentence
        assert state.action.strip(), state_id
        assert state.what.strip(), state_id
        # The next step is NAMED, not implied: at least one rendered line
        # carries the runnable action.
        assert any(state.action in line for line in lines), (state_id, lines)
        # And the sentence is ACTIONABLE: it says what would fill the panel,
        # not only that the panel is empty.
        assert "—" in state.sentence or state.action in state.sentence, (
            state_id,
            state.sentence,
        )

    @pytest.mark.parametrize("state_id", sorted(ob.EMPTY_STATES))
    def test_every_state_keeps_the_historical_first_words(self, state_id: str):
        """The change is ADDITIVE. Five other suites in this tree assert on the
        historical lowercase wording of these sentences, and a surface whose
        empty state was rewritten in place would have broken all of them for
        no reason a user would notice."""
        historical = {
            "no_sessions": "no recorded sessions yet",
            "no_sessions_match": "no sessions match",
            "no_diff": "no diff from the last run",
            "no_runs": "no run in this session yet",
            "no_connectors": "no MCP servers/connectors configured",
        }
        expected = historical.get(state_id)
        if expected is None:
            return
        assert ob.empty_state(state_id).sentence.startswith(expected), state_id

    @pytest.mark.parametrize("state_id", sorted(ob.EMPTY_STATES))
    def test_every_state_action_is_a_command_this_build_has(self, state_id: str):
        """The gate that makes "no model" a door and not a dead end."""
        names = set(ob.command_names())
        assert names, "the registry was unreachable; this gate cannot run"
        state = ob.empty_state(state_id)
        assert state.action in names, (state_id, state.action)
        for extra in re.findall(r"(?<![/\w])/[a-z][\w-]*", state.also):
            assert extra in names, (state_id, extra)

    @pytest.mark.parametrize("state_id", sorted(ob.EMPTY_STATES))
    @pytest.mark.parametrize("width", (40, 60, 78, 120))
    def test_no_empty_state_line_overflows_the_width(self, state_id: str, width: int):
        for line in ob.empty_state_lines(state_id, width=width):
            assert len(line) <= width, (state_id, width, line)

    def test_an_undeclared_state_is_reported_rather_than_rendered_blank(self):
        """A missing entry in the vocabulary is a bug, and it says so."""
        state = ob.empty_state("a_state_nobody_declared")
        assert state.id == "unknown"
        assert state.sentence.strip()
        assert "/help" in state.action

    def test_the_repl_prints_the_sentence_instead_of_a_bare_refusal(self):
        """The wiring, not the table: `/mcp` with no connector configured."""
        from cli import connectors

        say = _Say()
        original = connectors.discover_mcp_servers
        connectors.discover_mcp_servers = lambda *a, **k: {}
        try:
            iv._render_mcp({}, say=say)
        finally:
            connectors.discover_mcp_servers = original
        text = say.plain
        # The historical wording survives (four other suites assert on it) and
        # the next step is NEW.
        assert "no MCP servers/connectors configured" in text
        assert "next: /mcp" in text
        assert "neo mcp add" in text

    def test_the_repl_cost_surface_names_the_run_it_needs(self):
        from pathlib import Path as _Path

        say = _Say()
        iv._render_cost({}, _Path("does-not-exist"), say=say)
        text = say.plain
        assert "no run in this session yet" in text
        assert "next: /status" in text

    def test_the_repl_sessions_surface_names_the_run_it_needs(self, tmp_path):
        say = _Say()
        # root_scoped keeps the global cross-repo index out of it; otherwise the
        # developer's real machine decides whether this assertion runs.
        iv._print_sessions(say, tmp_path / "logs", "", root_scoped=True)
        text = say.plain
        assert "no recorded sessions yet" in text
        assert "next: /sessions" in text

    def test_the_repl_diff_surface_names_what_would_fill_it(self, tmp_path, capsys):
        """The whole public dispatch path, captured the way a user sees it."""
        from rich.markup import escape

        iv._slash_command(
            "/diff", "/diff", {}, tmp_path / "logs", {"repo": str(tmp_path)}
        )
        text = escape(capsys.readouterr().out)
        assert "no diff from the last run" in text
        assert "next: /diff" in text


# ---------------------------------------------------------------------------
# 4. HELP IS SEARCHABLE BY TASK
# ---------------------------------------------------------------------------


#: The brief's requirement 5, as data: a query that names NO command must
#: still reach the right one, FIRST.
TASK_PHRASED_QUERIES = (
    ("show me what changed", "/diff"),
    ("what did you change", "/diff"),
    ("which files did you touch", "/files"),
    ("what happened to my code", "/diff"),
    ("undo that", "/undo"),
    ("put that back", "/undo"),
    ("revert the change", "/undo"),
    ("how do I get my files back", "/undo"),
    ("stop the run", "/cancel"),
    ("kill it", "/cancel"),
    ("it's stuck", "/cancel"),
    ("did the tests pass", "/status"),
    ("what is it doing right now", "/status"),
    ("show me the evidence", "/trace"),
    ("actually do this instead", "/steer"),
    ("how much did that cost", "/cost"),
    ("how many tokens", "/cost"),
    ("what did I do yesterday", "/sessions"),
    ("continue where I left off", "/resume"),
    ("add a provider", "/connect"),
    ("which model should I use", "/model"),
    ("something is broken", "/doctor"),
    ("what can I say", "/help"),
    ("connect my tools", "/mcp"),
)


def _mentions_any_command_word(query: str) -> bool:
    """True when `query` contains a command's name as a standalone word.

    Deliberately NOT a claim that the query "knows" the command — "which
    model should I use" is a task phrasing a person types without knowing
    anything about the registry. It is a measurement of how strong each
    demonstration case is, and the strong subset is gated separately.
    """
    folded = query.casefold()
    return any(
        re.search(rf"(?<![a-z]){re.escape(row.command.lstrip('/'))}(?![a-z])", folded)
        for row in ob.task_index()
    )


class TestHelpAnswersTheTaskNotTheCommandName:
    @pytest.mark.parametrize("query,expected", TASK_PHRASED_QUERIES)
    def test_a_task_phrased_query_reaches_its_command_first(self, query, expected):
        """The measured pre-round failure this exists to close."""
        matches = iv.help_search(query, limit=5)
        assert matches, query
        assert matches[0].name == expected, (query, [m.name for m in matches])

    @pytest.mark.parametrize("query,expected", TASK_PHRASED_QUERIES)
    def test_no_query_spells_a_command_as_a_command(self, query, expected):
        """Requirement 5 is about phrasings, not about slash syntax.

        Several of these DO contain the ordinary English word behind a command
        ("undo that", "which model should I use") — the brief names "undo
        that" itself. What a first-day user cannot do is type the command, so
        the claim under test is that no query needs one.
        """
        assert "/" not in query, query
        assert not re.search(r"^/[a-z]", query.strip()), query

    def test_most_task_phrased_queries_name_no_command_word_at_all(self):
        """The strong subset, and the reason this class exists.

        Twelve-plus of the phrasings contain no command word in any form, so
        they could not have been produced by somebody reading the registry.
        Each still reaches its command FIRST.
        """
        strong = [
            (query, expected)
            for query, expected in TASK_PHRASED_QUERIES
            if not _mentions_any_command_word(query)
        ]
        assert len(strong) >= 12, [q for q, _e in strong]
        for query, expected in strong:
            matches = iv.help_search(query, limit=5)
            assert matches and matches[0].name == expected, (
                query,
                [m.name for m in matches],
            )

    @pytest.mark.parametrize("query,expected", TASK_PHRASED_QUERIES)
    def test_the_row_carries_the_phrasing_that_reached_it(self, query, expected):
        """So `/help` can show "what did you change -> /diff", not a bare name."""
        matches = iv.help_search(query, limit=3)
        assert matches[0].name == expected
        assert matches[0].task, matches[0]
        rendered = iv.render_help(query, width=100)
        from rich.markup import escape

        assert escape(matches[0].task) in rendered

    def test_help_is_grouped_by_task_and_every_group_clears_the_threshold(self):
        """Grouped by TASK, and a group below the anti-clutter bar is dropped."""
        threshold = ob.anti_clutter_min_entries()
        groups = ob.task_groups()
        assert groups
        for name, rows in groups:
            assert len(rows) >= threshold, name
        names = [name for name, _rows in groups]
        assert "see what changed" in names
        assert "put it back" in names
        assert "stop the run" in names
        # Two groups that would not clear the threshold if they were written.
        for name, rows in groups:
            assert all(row.group == name for row in rows)

    def test_the_bare_help_opens_with_the_task_index(self):
        """A bare `/help` that starts with command names answers the wrong
        question; the task index is now first and the names follow."""
        rendered = iv.render_help(width=100)
        task_at = rendered.index("by what you want to do")
        names_at = rendered.index("every command, by name")
        assert task_at < names_at
        for phrase in ("show me what changed", "undo that", "stop the run"):
            from rich.markup import escape

            assert escape(phrase) in rendered

    def test_the_bare_help_still_names_every_registered_command(self):
        """The anti-drift property the pre-round gate owns: no command can fall
        behind help, and the task index cannot become the only index."""
        from cli.commands import COMMAND_SPECS

        rendered = iv.render_help(width=100)
        for spec in COMMAND_SPECS:
            assert spec.name in rendered, spec.name
        assert "/help <word>" in rendered

    def test_the_task_index_is_bounded(self):
        """One line per group, so the index is not the wall `/help` used to be."""
        lines = iv.render_task_help_index(width=100)
        assert len(lines) == 1 + len(ob.task_groups())

    def test_no_task_index_row_overflows_the_width_when_RENDERED(self):
        """Measured through a real Console, not by counting markup.

        The first version of this assertion counted the escaped string and
        reported a six-column overflow that did not exist - `[neo.muted]` is
        eleven characters of source and zero of terminal. A gate that reports
        a defect which is not there trains people to ignore it.
        """
        for width in (40, 56, 64, 72, 78, 96, 120, 200):
            rendered = _render_widths(iv.render_task_help_index(width=width), width)
            assert rendered["overflowing_rows"] == 0, (width, rendered)

    def test_a_dropped_phrase_is_counted_rather_than_silently_shortened(self):
        """A bounded list that does not say it is bounded reads as complete."""
        rows = [_strip_markup(row) for row in iv.render_task_help_index(width=64)]
        assert len(rows) == 1 + len(ob.task_groups())
        body = rows[1:]  # row 0 is the section heading
        dropped = [row for row in body if re.search(r"\+\d+", row)]
        # At 64 columns every group sheds phrasings, and every one that sheds
        # ends its phrase list with the count.
        assert len(dropped) == len(ob.task_groups()), rows
        for row in dropped:
            phrase = row.split(" -> ")[0].rstrip()
            assert re.search(r"\+\d+$", phrase), row
        # And the FIRST (canonical) phrase of every group survives, because a
        # person who types the one that vanished gets nothing back.
        for (group, group_rows), row in zip(ob.task_groups(), body, strict=True):
            assert group_rows[0].phrase in row, (group, row)

    def test_the_phrase_list_is_not_truncated_at_a_wide_terminal(self):
        body = [_strip_markup(row) for row in iv.render_task_help_index(width=200)][1:]
        for (group, group_rows), row in zip(ob.task_groups(), body, strict=True):
            for group_row in group_rows:
                assert group_row.phrase in row, (group, group_row.phrase)

    @pytest.mark.parametrize("width", (40, 56, 64, 78, 96, 120, 200))
    def test_the_omission_marker_states_the_number_actually_dropped(self, width):
        """A disclosure marker that over-counts is a disclosure marker that
        lies. The RESERVED width is the worst case; the PRINTED count is the
        truth, and reusing the reservation as the count claimed "+4" on a row
        that dropped two."""
        body = [_strip_markup(row) for row in iv.render_task_help_index(width=width)][
            1:
        ]
        for (group, group_rows), row in zip(ob.task_groups(), body, strict=True):
            match = re.search(r" \+(\d+)", row)
            shown = [r for r in group_rows if r.phrase in row]
            if match is None:
                assert len(shown) == len(group_rows), (group, row)
                continue
            assert int(match.group(1)) == len(group_rows) - len(shown), (group, row)

    def test_a_query_matching_nothing_still_says_nothing_matches(self):
        assert iv.help_search("") == []
        assert iv.help_search("zzz-not-a-command") == []
        assert "no command matches" in iv.render_help("zzz-not-a-command")

    def test_a_command_name_still_works(self):
        """The task index is additive; it is not the only way in."""
        assert iv.help_search("cost", limit=3)[0].name == "/cost"
        assert "ctrl+g" in iv.render_help("steer")


# ---------------------------------------------------------------------------
# 5. THE PHRASE MATCHER ITSELF
# ---------------------------------------------------------------------------


class TestThePhraseMatcher:
    def test_an_exact_phrase_outranks_a_phrase_that_merely_contains_it(self):
        """Tiers are far apart on purpose: the failure being removed is "the
        right answer was third", not "the right answer was missing"."""
        exact = fuzzy.phrase_score("undo that", "undo that")
        contained = fuzzy.phrase_score("undo that", "undo that and the redo step")
        assert exact is not None and contained is not None
        assert exact > contained
        assert fuzzy.phrase_score("undo that", "undo that") >= fuzzy.phrase_score(
            "undo that", "how to undo that safely"
        )

    def test_a_contiguous_run_outranks_a_scattered_one(self):
        """Signal words only: "that" and "the" are dropped before matching, so
        a test built from them would measure the noise filter, not the tiers."""
        contiguous = fuzzy.phrase_score("undo revert", "undo revert exactly")
        scattered = fuzzy.phrase_score("undo revert", "undo the whole plan and revert")
        assert contiguous is not None and scattered is not None
        assert contiguous > scattered

    def test_a_query_made_only_of_noise_words_still_matches(self):
        """`signal_phrase` falls back to the full token list rather than
        returning nothing, so "what is it" is a question and not silence."""
        assert fuzzy.phrase_score("what is it", "show me what changed") is not None

    def test_dropping_noise_words_from_the_query_never_loses_the_match(self):
        """The exact tier compares the RAW normalisation, so "show me what
        changed" outranks "what changed" against its own phrase - and the
        shorter form is still a match, which is the property that matters."""
        long_form = fuzzy.phrase_score("show me what changed", "show me what changed")
        short_form = fuzzy.phrase_score("what changed", "show me what changed")
        assert long_form is not None and short_form is not None
        assert long_form >= short_form

    def test_an_out_of_order_query_scores_below_the_in_order_one(self):
        """A person who typed the words in the wrong order has not asked that
        question, so it must not outrank the one who did."""
        in_order = fuzzy.phrase_score("stop the run", "stop the run")
        out_of_order = fuzzy.phrase_score("run stop", "stop the run")
        assert in_order is not None and out_of_order is not None
        assert out_of_order < in_order

    def test_one_word_of_five_cannot_outrank_three_of_three(self):
        partial = fuzzy.phrase_score("the run of the week stopped", "stop the run")
        exact = fuzzy.phrase_score("stop the run", "stop the run")
        assert partial is not None and exact is not None
        assert partial < exact

    def test_an_empty_query_matches_everything_at_score_zero(self):
        assert fuzzy.phrase_score("", "anything at all") == 0
        assert fuzzy.phrase_score("   ", "anything at all") == 0

    def test_the_matcher_is_total(self):
        for query, phrase in ((None, None), (1, 2), ([], {}), ("[", "]")):
            fuzzy.phrase_score(query, phrase)  # must not raise
        assert fuzzy.normalize_phrase(None) == ()
        assert fuzzy.phrase_score("x", "") is None

    def test_punctuation_and_case_are_blind(self):
        assert fuzzy.phrase_score("UNDO THAT", "undo that") == fuzzy.phrase_score(
            "undo-that", "Undo That!"
        )
        # And no bracket survives into a token, which is why these tokens are
        # safe to hand to any renderer.
        for token in fuzzy.normalize_phrase("weird[name].py don't stop"):
            assert re.fullmatch(r"[a-z0-9]+", token), token

    def test_the_word_matcher_is_unchanged(self):
        """The palette still uses `fuzzy_score`; this round added beside it."""
        assert fuzzy.fuzzy_score("un", "/undo") is not None
        assert fuzzy.fuzzy_score("zzz", "/undo") is None
        assert [item for item, _s in fuzzy.rank(["/undo", "/diff"], "")] == [
            "/undo",
            "/diff",
        ]


# ---------------------------------------------------------------------------
# 6. MARKUP SAFETY - a render failure must never delete a message
# ---------------------------------------------------------------------------


class TestNothingCrossesTheParserAsMarkup:
    HOSTILE = "weird[name].py"

    def test_the_first_run_escapes_a_hostile_repository_name(self):
        """A repository path may contain `[`; an unescaped one eats the line."""
        from rich.markup import escape

        rendered = iv.render_first_run(
            f"C:/repo/{self.HOSTILE}", "C:/logs", model="m", width=120
        )
        joined = "\n".join(rendered)
        assert escape(self.HOSTILE) in joined
        console = _devnull_console()
        for line in rendered:
            console.print(line)  # must not raise

    def test_the_hostile_name_is_still_VISIBLE_after_rendering(self):
        """The property that matters is that the message SURVIVED, not that a
        bracket is absent — rich escapes only the OPENING bracket, so a
        substring assertion passes while the text is being eaten.

        The sink is `interactive.render_first_run`, the ESCAPING exit, and the
        reason is measured rather than asserted: handing the plain producer's
        output straight to a markup-parsing Console renders `weird[name].py`
        as `weird.py`, which is exactly the "a render failure deleted the
        message" failure this whole file guards against. That is why the
        producers are plain and the exits are `escape_lines` / `text_lines`.
        """
        from rich.console import Console

        from cli import onboarding

        hostile = f"C:/repo/{self.HOSTILE}"
        escaped = iv.render_first_run(hostile, "C:/logs", model="m", width=200)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            console = Console(width=200, record=True, file=sink)
            for line in escaped:
                console.print(line)
        # `export_text` CLEARS the record buffer, so it is read once.
        captured = console.export_text()
        assert "weird" in captured
        assert "name].py" in captured
        # And the plain producer really does carry the raw brackets, which is
        # what makes the escaping step load-bearing rather than decorative.
        assert any(
            self.HOSTILE in line
            for line in onboarding.first_run_lines(hostile, "l", width=200)
        )

    def test_the_task_phrasings_and_sentences_carry_no_markup_delimiters(self):
        """No producer in `cli/onboarding.py` emits a bracket at all."""
        for row in ob.task_index():
            assert "[" not in row.phrase, row.phrase
        for state in ob.EMPTY_STATES.values():
            assert "[" not in state.sentence, state.id
            assert "[" not in state.action, state.id

    def test_the_empty_state_exit_is_rich_text_not_markup(self):
        """`rich.text.Text` has no markup interpretation at all."""
        rows = tc.empty_state_lines("no_diff")
        assert rows and all(not isinstance(row, str) for row in rows)
        for row in rows:
            assert "[neo." not in row.plain

    def test_a_hostile_provider_reaches_the_help_render_without_raising(self):
        console = _devnull_console()
        for line in iv.render_help("cost", width=100).splitlines():
            console.print(line)
        for line in iv.render_first_run(self.HOSTILE, "logs", width=100):
            console.print(line)

    def test_the_repl_empty_state_is_markup_escaped(self):
        say = _Say()
        iv.say_empty_state(say, "no_diff")
        assert say.rows
        for row in say.rows:
            assert row.startswith("[neo.") and row.endswith("[/]")
            assert "[neo." not in row[6:-3]

    def test_an_empty_state_with_a_hostile_fact_does_not_raise(self):
        """The state is a fixed sentence, so a hostile REPO name cannot reach
        it — and the renderer is total for the same reason."""
        say = _Say()
        for line in iv.say_empty_state(say, f"no_diff {self.HOSTILE}"):
            assert line
        console = _devnull_console()
        for row in say.rows:
            console.print(row)


# ---------------------------------------------------------------------------
# 7. THE VERIFIER GATE IS UNTOUCHED
# ---------------------------------------------------------------------------


class TestTheGateIsUntouched:
    def test_the_first_screen_makes_no_completion_claim_about_a_run(self):
        """`completed_unverified` is never success, and an onboarding surface
        is exactly where that sentence could be smuggled in.

        The check is on what the screen SAYS, not on what the module contains:
        the one legitimate use of the word is the honesty sentence, and it is
        conditional ("only ... when its tests actually pass"). A blanket
        substring ban would forbid the product's own differentiating claim.
        """
        rendered = "\n".join(ob.first_run_lines("r", "l", model="m", width=200))
        for line in rendered.splitlines():
            for word in ("success", "succeeded", "passed", "complete"):
                assert word not in line.casefold(), line
        # "verified" may appear ONLY inside the conditional honesty sentence.
        for match in re.finditer(r"[^.]*verified[^.]*", rendered, re.I):
            assert "only calls a run verified" in match.group(0), match.group(0)

    def test_no_empty_state_claims_a_run_reached_any_outcome(self):
        for state in ob.EMPTY_STATES.values():
            for field in (state.sentence, state.action, state.also, state.why):
                for word in ("success", "succeeded", "passed", "verified", "complete"):
                    assert word not in field.casefold(), (state.id, field)

    def test_no_onboarding_key_went_into_config_defaults(self):
        """A value in `DEFAULTS` merges into every task and every eval arm, and
        which panes a person wants is a layout preference, not a run fact."""
        from harness import config

        keys = getattr(config, "DEFAULTS", {})
        offenders = [
            key
            for key in keys
            if "onboard" in key
            or "first_run" in key
            or "affordance" in key
            or "empty_state" in key
        ]
        assert not offenders, offenders

    def test_the_module_declares_no_completion_status(self):
        """It must not import the completion vocabulary at all, so it cannot
        mint or re-word one."""
        source = Path(ob.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "RUN_STATUSES",
            "agent_contracts",
            "status_is_success",
            "completed_verified",
        ):
            assert forbidden not in source, forbidden
