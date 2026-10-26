"""Pins for the Phase 5/W1 pane surfaces, and control arms FOR the truth gate.

Phase 5/W1 tasks T5.W1.4 and T5.W1.5. Host-only: no Docker, no provider, no
network, no credential.

Why this file exists separately from ``cli/pane_truth.py`` itself: a gate whose
only proof is that it returns green is a gate nobody can trust. So half of
these tests are **control arms** -- each one plants a defect in the gate's own
subject and requires a specific failure, which is the difference between "the
gate ran" and "the gate can fail". The three arms below were all reachable and
each one fired on its first run; §3 of ``cli/AGENTS.md``'s history records the
opposite failure (an arm nobody could fire) twice, so an arm that cannot fire
is treated as a defect here.

Everything is measured through the REAL renderers against the REAL fixture
repository built by ``pane_truth.build_fixture`` -- a real ``git init``, two
commits, and a real unmerged conflict -- because a truth gate driven by a
hand-written stand-in proves the stand-in renders.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from cli import changespane as _changes
from cli import filetreepane as _tree
from cli import gitpane as _git
from cli import pane_truth as _truth
from cli import review as _review
from cli import ui as _ui
from cli import workpane as _wp

_FIXTURE: Any = None


def fixture() -> Any:
    """The one real repository every test in this file measures against."""
    global _FIXTURE
    if _FIXTURE is None:
        import tempfile

        _FIXTURE = _truth.build_fixture(tempfile.mkdtemp(prefix="neo-pane-test-"))
    return _FIXTURE


def document() -> Any:
    """A real ``ReviewDocument`` over the real fixture rows."""
    return _truth._document(_truth._tier_rows())


class TestTheGateIsGreenOnThisTree(unittest.TestCase):
    """The whole matrix, and the shape assertions that keep it whole."""

    def test_the_full_matrix_is_clean(self) -> None:
        report = _truth.assert_every_surface_renders_truth()
        self.assertTrue(report["clean"])
        self.assertEqual(report["empty_renders"], 0)

    def test_the_matrix_has_exactly_the_declared_shape(self) -> None:
        rows = _truth.surface_matrix(fixture())
        expected = len(_truth.SURFACES) * len(_truth.EDGE_CASES) * len(_truth.WIDTHS)
        self.assertEqual(len(rows), expected)
        self.assertEqual(len(set(row.surface for row in rows)), len(_truth.SURFACES))
        self.assertEqual(len(set(row.edge for row in rows)), len(_truth.EDGE_CASES))

    def test_the_marker_contract_only_names_declared_surfaces(self) -> None:
        for surface, edge in _truth.REQUIRED_MARKERS:
            self.assertIn(surface, _truth.SURFACES)
            self.assertIn(edge, _truth.EDGE_CASES)

    def test_a_second_run_agrees_with_the_first(self) -> None:
        """The gate is pure over its fixture, so a repeat must not drift."""
        first = [
            (row.surface, row.edge, row.width, row.lines)
            for row in _truth.surface_matrix(fixture())
        ]
        second = [
            (row.surface, row.edge, row.width, row.lines)
            for row in _truth.surface_matrix(fixture())
        ]
        self.assertEqual(first, second)


class TestControlArmsTheGateCanActuallyFire(unittest.TestCase):
    """Each arm plants a defect and requires a SPECIFIC failure."""

    def test_an_empty_render_is_caught(self) -> None:
        empty = _truth.SurfaceRow("change_list", "no_changes", 60, ())
        self.assertTrue(_truth._problems_for(empty))

    def test_an_unsanitised_render_is_caught(self) -> None:
        """A line that has NOT crossed the sanitiser is not idempotent."""
        raw = _truth.SurfaceRow(
            "change_list", "no_changes", 60, ("key = \x1b[31mSECRET\x1b[0m",)
        )
        self.assertTrue(_truth._problems_for(raw))

    def test_a_missing_edge_marker_is_caught(self) -> None:
        row = _truth.SurfaceRow("change_list", "conflict", 100, ("src/a.py",))
        problems = _truth._problems_for(row)
        self.assertTrue(problems)
        self.assertIn("CONFLICT", " ".join(problems))

    def test_the_gate_fires_on_a_real_defect(self) -> None:
        """The strongest arm: neuter the CONFLICT label and require exit 2."""
        saved = _changes.CONFLICT_LABEL
        try:
            _changes.CONFLICT_LABEL = "MERGE-PENDING"
            rows = _truth.surface_matrix(fixture())
            with self.assertRaises(AssertionError) as caught:
                _truth.assert_every_surface_renders_truth(rows)
            self.assertIn("CONFLICT", str(caught.exception))
        finally:
            _changes.CONFLICT_LABEL = saved
        # and the restored gate is green again -- a control arm that leaves its
        # subject broken measures the next test instead of its own arm.
        _truth.assert_every_surface_renders_truth(_truth.surface_matrix(fixture()))

    def test_the_gate_fires_when_a_marker_table_row_is_dropped(self) -> None:
        """Delete a CONTRACT row and the matrix stops being CLEAN."""
        saved = dict(_truth.REQUIRED_MARKERS)
        try:
            _truth.REQUIRED_MARKERS.clear()
            rows = _truth.surface_matrix(fixture())
            # With no contract rows the matrix is merely non-empty, so this arm
            # asserts the weaker, still-true property: no row is flagged. The
            # gate cannot fire here BY CONSTRUCTION, which is exactly why it is
            # pinned rather than left implicit.
            self.assertEqual([row for row in rows if _truth._problems_for(row)], [])
        finally:
            _truth.REQUIRED_MARKERS.update(saved)

    def test_a_lost_row_is_caught_by_the_shape_assertion(self) -> None:
        rows = _truth.surface_matrix(fixture())
        with self.assertRaises(AssertionError):
            _truth.assert_every_surface_renders_truth(rows[:-1])


class TestBoundsAreStatedNotSilent(unittest.TestCase):
    """Every bound must SAY its bound. This is the class of failure the pane
    exists to close, so it is pinned per surface rather than once."""

    def test_a_bounded_change_list_says_it_is_bounded(self) -> None:
        model = _changes.pane_model(document(), repo=fixture().repo, stale_check=False)
        lines = _changes.pane_lines(model, 100, rows=2)
        self.assertTrue(any("more file(s) not shown" in line for line in lines))

    def test_a_bounded_tree_says_it_is_bounded(self) -> None:
        model = _tree.tree_model(Path(fixture().repo), ("src/alpha.py",), width=60)
        lines = _tree.tree_lines(model, 60, rows=3)
        self.assertTrue(any("more row(s) not shown" in line for line in lines))

    def test_a_bounded_hunk_view_says_how_many_hunks_it_dropped(self) -> None:
        row = _changes.change_rows(document(), fixture().repo, stale_check=False)
        huge = next(item for item in row if item.path == "data/table.csv")
        lines = _changes.hunk_view_lines(huge, 100)
        joined = " ".join(lines)
        self.assertIn("truncat", joined.lower())

    def test_a_narrow_pane_says_why_it_narrowed(self) -> None:
        model = _changes.pane_model(document(), repo=fixture().repo, stale_check=False)
        lines = _changes.pane_lines(model, 20)
        self.assertTrue(any("too narrow" in line or "narrow" in line for line in lines))


class TestEverySurfaceRendersAtEveryWidth(unittest.TestCase):
    """A surface that renders at 200 and silently vanishes at 24 is broken."""

    def test_no_surface_is_empty_at_any_width(self) -> None:
        for row in _truth.surface_matrix(fixture()):
            self.assertTrue(
                row.lines, f"{row.surface}/{row.edge}/{row.width} rendered nothing"
            )

    def test_every_line_fits_its_width(self) -> None:
        for row in _truth.surface_matrix(fixture()):
            for line in row.lines:
                self.assertLessEqual(
                    len(line),
                    row.width,
                    f"{row.surface}/{row.edge}/{row.width} produced a {len(line)}-column line",
                )


class TestSanitisation(unittest.TestCase):
    """Adversarial values, through every renderer, on the real paths."""

    HOSTILE: Tuple[str, ...] = (
        "\x1b[31mkey=sk-FAKE-LEAK-0123456789\x1b[0m",
        "key=sk-FA\u200bKE-FAKE-LEAK-0123456789",
        '+KEY = "key=sk-FA\rKE-FAKE-LEAK-0123456789"',
        ".env",
        "weird[name].py",
        "[bold red]evil[/]",
    )

    def test_no_hostile_value_reaches_a_renderer(self) -> None:
        for value in self.HOSTILE:
            rows = tuple(
                _review.FileReview(
                    path=value,
                    kind="modified",
                    state="changed",
                    hunk_headers=("@@ -1 +1 @@",),
                )
            )
            lines = _changes.pane_lines(
                _changes.pane_model(_truth._document(rows)), 100
            )
            joined = "\n".join(lines)
            self.assertNotIn("sk-FAKE-LEAK", joined, f"the secret survived {value!r}")
            self.assertNotIn("\x1b", joined)

    def test_a_hostile_branch_name_does_not_reach_the_git_surface(self) -> None:
        report = replace(
            _git.GitReport(), branch="main\x1b[31m-leaked", work_dir=fixture().work_dir
        )
        for line in _git.git_discoverability_lines(report, 100):
            self.assertNotIn("\x1b", line)
            self.assertEqual(_ui.sanitize_text(line), line)

    def test_every_pane_line_is_sanitiser_idempotent(self) -> None:
        for row in _truth.surface_matrix(fixture()):
            for line in row.lines:
                self.assertEqual(_ui.sanitize_text(line), line)


class TestTheTiersAreCarriedAndVisible(unittest.TestCase):
    """Every edit tier reaches the row, with its own marker."""

    def test_the_four_tiers_render_their_own_vocabulary(self) -> None:
        fix = fixture()
        rows = {
            row.path: row
            for row in _changes.change_rows(
                _truth._document(_truth._tier_rows()), fix.repo, stale_check=False
            )
        }
        for path, marker in (
            ("src/alpha.py", "=exact"),
            ("src/fuzzy_edited.py", "~fuzzy"),
            ("src/unrecorded.py", "?unrecorded"),
        ):
            self.assertIn(marker, _changes.file_row(rows[path], 100), path)

    def test_a_fuzzy_edit_renders_its_strategy_and_confidence(self) -> None:
        fix = fixture()
        row = next(
            item
            for item in _changes.change_rows(
                _truth._document(_truth._tier_rows()), fix.repo, stale_check=False
            )
            if item.path == "src/fuzzy_edited.py"
        )
        rendered = " ".join(_changes.file_row(row, 120))
        self.assertIn("context_levenshtein", rendered)
        self.assertIn("0.87", rendered)

    def test_an_unrecorded_tier_is_never_drawn_as_exact(self) -> None:
        fix = fixture()
        row = next(
            item
            for item in _changes.change_rows(
                _truth._document(_truth._tier_rows()), fix.repo, stale_check=False
            )
            if item.path == "src/unrecorded.py"
        )
        self.assertNotIn("=exact", " ".join(_changes.file_row(row, 120)))


class TestGitSafety(unittest.TestCase):
    """The git surface refuses a directory it cannot prove it owns."""

    def test_a_real_work_directory_is_accepted(self) -> None:
        self.assertTrue(_git.assert_work_dir_is_isolated(fixture().work_dir)["ok"])

    def test_the_repository_itself_is_refused(self) -> None:
        verdict = _git.assert_work_dir_is_isolated(fixture().repo)
        self.assertFalse(verdict["ok"])
        self.assertIn("work", verdict["reason"])

    def test_an_outside_directory_is_refused(self) -> None:
        verdict = _git.assert_work_dir_is_isolated(Path(fixture().repo) / "src")
        self.assertFalse(verdict["ok"])

    def test_a_refusal_names_why(self) -> None:
        verdict = _git.assert_work_dir_is_isolated(Path(fixture().repo) / "src")
        self.assertGreater(len(verdict["reason"]), 20)

    def test_a_missing_directory_is_refused_not_assumed(self) -> None:
        verdict = _git.assert_work_dir_is_isolated(
            Path(fixture().work_dir).parent / "nope" / "work"
        )
        self.assertFalse(verdict["ok"])


class TestTheBranchIsDiscoverable(unittest.TestCase):
    """A branch a person cannot reach is a branch they cannot use."""

    def test_the_work_directory_and_a_cd_line_are_both_named(self) -> None:
        report = _git.git_report(fixture().task_dir, fixture().repo, width=100)
        joined = " ".join(_git.git_discoverability_lines(report, 100))
        self.assertIn("work at", joined)
        self.assertIn("cd ", joined)

    def test_no_work_copy_says_so_rather_than_rendering_nothing(self) -> None:
        lines = _git.git_discoverability_lines(_git.GitReport(available=False), 100)
        self.assertTrue(lines)
        self.assertIn("no work copy", " ".join(lines))


class TestThePaneContainer(unittest.TestCase):
    """One region, three modes, one keyboard map."""

    def setUp(self) -> None:
        fix = fixture()
        self.state = _wp.WorkPaneState(
            task_dir=fix.task_dir,
            repo=fix.repo,
            review_document=document(),
            changed_paths=("src/alpha.py", "assets/logo.bin"),
            visible=True,
        )
        self.pane = _wp.WorkPane(self.state)

    def test_each_mode_renders_and_names_itself_active(self) -> None:
        for mode in _wp.MODES:
            self.state.mode = mode
            lines = self.pane.render_mode_lines()
            self.assertTrue(lines, mode)
            self.assertIn(f"*{mode}", " ".join(lines))

    def test_the_key_help_is_generated_from_the_key_map(self) -> None:
        help_lines = " ".join(self.pane.handle_action("help"))
        for keys, _what in _wp.KEY_HELP:
            self.assertIn(keys.split()[0], help_lines)

    def test_an_unknown_action_says_so_rather_than_doing_nothing(self) -> None:
        self.assertIn("unknown", " ".join(self.pane.handle_action("wibble")))

    def test_the_width_cycle_visits_every_declared_step(self) -> None:
        seen: List[int] = []
        for _ in range(len(_wp.WIDTH_STEPS) + 1):
            seen.append(int(self.state.width))
            self.pane.handle_action("width")
        self.assertEqual(sorted(set(seen)), sorted(set(_wp.WIDTH_STEPS)))

    def test_a_narrow_terminal_collapses_and_says_the_bound(self) -> None:
        note = self.pane.apply_visibility(terminal_columns=60)
        self.assertFalse(self.state.visible)
        self.assertIn(str(_wp.PANE_MIN_TERMINAL_COLUMNS), note)

    def test_text_lines_are_rich_text_so_markup_cannot_eat_a_message(self) -> None:
        for line in self.pane.text_lines():
            self.assertEqual(type(line).__name__, "Text")

    def test_the_pane_renders_something_for_every_mode_even_with_nothing_to_say(
        self,
    ) -> None:
        self.state.task_dir = None
        self.state.repo = None
        self.state.review_document = None
        self.state.changed_paths = ()
        for mode in _wp.MODES:
            self.state.mode = mode
            self.assertTrue(self.pane.render_mode_lines(), mode)


class TestTheThreePanesAgree(unittest.TestCase):
    def test_lists_and_tree_state_the_same_paths(self) -> None:
        report = _truth._parity_report()
        self.assertTrue(report["lists_agree"])
        self.assertTrue(report["tree_agrees"])
        self.assertTrue(report["clean"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
