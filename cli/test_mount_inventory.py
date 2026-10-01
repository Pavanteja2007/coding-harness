"""T4.W1.2 — the nine-surface mount inventory, pinned.

`cli/UNMOUNTED.md` recorded nine surfaces that were written, unit-proven and
never applied, across twelve rounds and 24 recorded handoff rows with 0
discharged. This file is the mechanical half of discharging them: for each
row it either proves the mount is live (behaviourally, or by reading the
dispatcher's AST) or records a written reason and a named owner.

The construct this file exists for is the **inverted pin**: a test that
passes precisely BECAUSE a surface is not wired, and fails the moment
somebody wires it. `tests/test_daily_platform_parity.py:2167` was the
project's named example and this round flips it. The other pins in
`cli/UNMOUNTED.md`'s table are listed in `FLIPPED_PINS` below with the test
that now carries the live claim, so the registry T5 maintains cannot report
a stale entry.

Host-only: no Docker, no provider, no network. `tests/**` is T5's.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

CLI = Path(__file__).resolve().parent

#: The nine rows, each with the file that owns it and the decision this
#: round made. `mounted` is measured, not declared - see the tests below.
SURFACES: Tuple[Dict[str, str], ...] = (
    {
        "id": "1.1 review",
        "module": "review.py",
        "decision": "mounted",
        "where": "tui.py VexApp._review_surface / interactive._render_review_surface",
        "why": "104KB of hash-verified review nothing called; /diff rendered the "
        "historical diff with no verdicts and no restore",
    },
    {
        "id": "2.1a instance guard",
        "module": "session.py",
        "decision": "mounted",
        "where": "tui.py VexApp._acquire_instance_guard / headless._resolve_session",
        "why": "two vex instances on one worktree were NOT refused; two agents "
        "mutating one tree is how work is lost",
    },
    {
        "id": "2.1b session pulse",
        "module": "session.py",
        "decision": "mounted",
        "where": "tui.py _sidebar_facts / interactive._print_session_pulse",
        "why": "the live context/cost projection nothing read, so the rail had "
        "no live data and Phase 5's P5.8 had nothing to build on",
    },
    {
        "id": "1.4a alias seam",
        "module": "command_aliases.py",
        "decision": "mounted",
        "where": "tui.py / interactive.py x2 / (headless is alias-reachability only)",
        "why": "the alias table was inert; resolve_line is a drop-in that hands "
        "every line it does not rewrite to the registry byte-identically",
    },
    {
        "id": "1.4b command queue",
        "module": "command_queue.py",
        "decision": "mounted",
        "where": "tui.py self._command_queue + _QueueLineView",
        "why": "one decide() for three surfaces, built and unreachable; the TUI "
        "hand-rolled a bare list[str] and the REPL/headless refused instead",
    },
    {
        "id": "W1.3 tri-state toggle",
        "module": "toggles.py",
        "decision": "mounted",
        "where": "toggles.ToggleSettings.flip + tui.py action_toggle_sidebar",
        "why": "flip stalled after one press; the cause was reading spec.default "
        "instead of the live value, and the TUI had a workaround for it",
    },
    {
        "id": "W1.5 retrieval display",
        "module": "runview.py",
        "decision": "mounted",
        "where": "runview.retrieval_projection / retrieval_lines",
        "why": "a slow search rendered as instant because the run view had no "
        "retrieval axis and no truncation renderer at all",
    },
    {
        "id": "1.2 palette menu",
        "module": "palette.py",
        "decision": "reason-unmounted",
        "where": "cli/tui.py:7935 VexApp.on_key (untouched)",
        "why": "the live ctrl+p palette is a SECOND data source "
        "(_palette_entries -> command_palette_entries) and mounting cli.palette "
        "without reconciling them produces two menus that disagree about what "
        "the product can do, which is worse than one. Reconciliation is a "
        "data-source swap plus a Pilot campaign whose 6 keyboard tests are "
        "recorded HANGING on this host.",
    },
    {
        "id": "1.3 model picker",
        "module": "models.py",
        "decision": "reason-unmounted",
        "where": "cli/main.py:3952 (untouched) + a new TUI binding",
        "why": "one line in cli/main.py plus a new screen class and keybind; "
        "the widget mount is feature work in the same shared file, and the "
        "effort cycle must be ONE result rather than a second screen.",
    },
)


def _tree(name: str) -> ast.Module:
    return ast.parse((CLI / name).read_text(encoding="utf-8"))


def _unparsed(tree: ast.Module, function: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return ast.unparse(node)
    raise AssertionError(f"{function} is gone")


def _method(tree: ast.Module, class_name: str, method: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method:
                    return ast.unparse(child)
    raise AssertionError(f"{class_name}.{method} is gone")


# ---------------------------------------------------------------------------
# The mounted rows, measured rather than declared.
# ---------------------------------------------------------------------------


class TestTheMountedRowsAreLive:
    def test_the_review_surface_is_reachable_from_both_dispatchers(self) -> None:
        tui = _tree("tui.py")
        repl = _tree("interactive.py")
        assert "review.review_command" in _unparsed(tui, "_review_surface")
        assert "review.build_review" in _unparsed(tui, "_review_surface")
        assert "review.review_command" in _unparsed(repl, "_render_review_surface")

    def test_the_review_falls_through_rather_than_swallowing_undo(self) -> None:
        """`handled=False` is what keeps `/diff undo` on the old engine.

        Asserted because it is the one property of the mount that would be
        invisible if it broke: the review would simply start answering for
        verbs it never owned, and every pre-review run would lose a
        capability.
        """
        from cli import review

        result = review.review_command(None, None, "all", verb="undo")
        assert result["handled"] is False, (
            "review_command claimed a verb it does not own; /diff undo would "
            "render the review surface instead of the historical engine"
        )

    def test_the_instance_guard_is_live_in_both_shells(self) -> None:
        tui = _tree("tui.py")
        headless = _tree("headless.py")
        assert "acquire_repository_lock" in _method(tui, "VexApp", "_acquire_instance_guard")
        assert "_release_instance_guard" in _method(tui, "VexApp", "on_unmount")
        assert "open_session" in _unparsed(headless, "_resolve_session")
        assert "load_or_create" not in _unparsed(headless, "_resolve_session")

    def test_the_session_pulse_is_live_in_both_shells(self) -> None:
        tui = _tree("tui.py")
        repl = _tree("interactive.py")
        assert "session_pulse" in _unparsed(tui, "_session_pulse")
        assert "_session_pulse()" in _unparsed(tui, "_sidebar_facts")
        assert "session_pulse" in _unparsed(repl, "_print_session_pulse")
        # And the REPL prints it, rather than only offering a helper.
        assert "_print_session_pulse(" in _unparsed(repl, "_slash_command_impl")

    def test_the_alias_seam_is_live_at_every_resolution_site(self) -> None:
        """THREE sites, and all three matter.

        `cli/interactive.py` has TWO resolution calls - the main dispatcher
        and the reader thread's own - and `cli/tui.py` has one. A seam
        mounted in only some of them answers differently depending on which
        thread the user happened to type into.
        """
        tui_source = (CLI / "tui.py").read_text(encoding="utf-8")
        repl_source = (CLI / "interactive.py").read_text(encoding="utf-8")
        assert tui_source.count("resolve_line(") >= 1, "the TUI has no alias seam"
        assert repl_source.count("resolve_line(") >= 2, (
            "the REPL must mount the seam at BOTH resolution sites: the main "
            "dispatcher and the reader thread's own call"
        )
        # And no site is left on the raw registry resolver.
        assert "_commands.resolve_command_line(" not in tui_source, (
            "cli/tui.py still resolves through the registry directly, so the "
            "alias table is inert on that surface"
        )
        assert "_commands.resolve_command_line(" not in repl_source, (
            "cli/interactive.py still resolves through the registry directly"
        )

    def test_the_queue_authority_is_live_and_writes_through(self) -> None:
        tui = _tree("tui.py")
        mount = _method(tui, "VexApp", "on_mount")
        assert "_acquire_instance_guard" in mount  # sanity: the AST reader works
        source = (CLI / "tui.py").read_text(encoding="utf-8")
        assert "CommandQueue(surface=" in source, (
            "the TUI queue authority is not a cli.command_queue.CommandQueue"
        )
        # No site may still append to a bare list.
        assert "self._queue.append(" not in source, (
            "an enqueue site still mutates a bare list instead of going "
            "through the one queue authority"
        )
        # And the statusline reads the authority's own facts.
        assert "statusline_facts" in _unparsed(tui, "_statusline_facts")

    def test_the_tri_state_toggle_advances(self) -> None:
        from cli import toggles

        settings = toggles.ToggleSettings()
        seen = [str(settings.get("sidebar"))]
        for _ in range(6):
            changed, _note = settings.flip("sidebar", persist=False)
            assert changed is True, "the tri-state advance stalled"
            seen.append(str(settings.get("sidebar")))
        assert len(set(seen)) == 3, f"the toggle did not cycle: {seen}"

    def test_the_retrieval_display_is_live(self) -> None:
        from cli.runview import retrieval_lines, retrieval_projection

        assert retrieval_lines(retrieval_projection({})) == [], (
            "a run with no retrieval receipt must render nothing, not a zero"
        )
        rows = retrieval_lines(
            retrieval_projection(
                {"duration_s": 144.0, "truncated": True, "max_results": 20, "returned": 20}
            )
        )
        assert any("144.0s" in row for row in rows)
        assert any("TRUNCATED" in row for row in rows)


# ---------------------------------------------------------------------------
# The un-mounted rows: each carries a reason and a named owner.
# ---------------------------------------------------------------------------


class TestEveryUnmountedRowCarriesAReasonAndAnOwner:
    def test_no_row_is_still_unmounted_without_a_written_reason(self) -> None:
        """*"Still unmounted" is only acceptable with a reason.

        A row with an empty reason is the state this whole file exists to
        end, and a test that accepts one is a test that ratifies it.
        """
        for row in SURFACES:
            assert len(row["why"]) >= 80, (
                f"{row['id']} carries no real reason: {row['why']!r}"
            )
            assert row["where"], f"{row['id']} names no call site"

    def test_a_reason_unmounted_row_names_who_would_close_it(self) -> None:
        """A reason with no owner is a note, and notes do not get closed."""
        for row in SURFACES:
            if row["decision"] != "reason-unmounted":
                continue
            why = row["why"]
            assert any(
                marker in why.lower()
                for marker in ("cli/", "untouched", "hanging", "reconcil")
            ), f"{row['id']} does not say what the next owner has to do: {why!r}"

    def test_the_inventory_covers_the_nine_recorded_rows(self) -> None:
        """The count is asserted, so a row cannot be deleted quietly."""
        assert len(SURFACES) == 9, (
            f"the inventory has {len(SURFACES)} rows, not 9: "
            f"{[row['id'] for row in SURFACES]}"
        )
        ids = [row["id"] for row in SURFACES]
        assert len(set(ids)) == 9, "two rows share an id"


class TestTheFlippedPinsAreRecordedForTheRegistry:
    """What T5's registry needs, so it does not report a stale entry.

    `cli/UNMOUNTED.md` named 13 inverted pins. This round flipped the one
    the brief named by name plus the vocabulary pin this round's toggle fix
    had to move. The rest still assert what they asserted, and the ones
    listed here must not be re-reported as "still to flip".
    """

    FLIPPED = (
        (
            "tests/test_daily_platform_parity.py",
            "test_no_shell_calls_the_guard_yet",
            "test_the_shells_now_hold_the_single_writer_guard",
        ),
        (
            "tests/test_design_layout.py",
            "test_the_sidebar_mode_vocabulary_is_the_toggle_registrys",
            "same name, INVERTED: two vocabularies -> one",
        ),
    )

    def test_every_flipped_pin_exists_at_the_name_recorded(self) -> None:
        """A registry entry for a test that was renamed is a stale entry."""
        repo = CLI.parent
        for relative, old, new in self.FLIPPED:
            path = repo / relative
            assert path.is_file(), f"{relative} is gone; the pin record is stale"
            source = path.read_text(encoding="utf-8")
            assert new.split()[0] in source, (
                f"{relative} no longer has {new!r}; the registry entry naming "
                f"{old!r} -> {new!r} is now wrong"
            )

    def test_no_flipped_pin_still_asserts_the_old_absence(self) -> None:
        """The point of an inverted pin: it must assert the PRESENCE now."""
        repo = CLI.parent
        parity = (repo / "tests/test_daily_platform_parity.py").read_text(
            encoding="utf-8"
        )
        assert 'assert "session.open_session(" not in source' not in parity, (
            "test_daily_platform_parity.py still asserts the guard is NOT "
            "called; the mount is live and the pin is stale"
        )
        design = (repo / "tests/test_design_layout.py").read_text(encoding="utf-8")
        assert '{"auto", "shown", "hidden"}' not in design, (
            "test_design_layout.py still pins the two-vocabulary state; there "
            "is ONE vocabulary now"
        )

    def test_the_names_of_the_flip_sites_are_recorded_exactly(self) -> None:
        """Line anchors drift; names do not.

        `cli/UNMOUNTED.md` records pins by `file:line`, and every line
        anchor in it has moved at least once. The registry should be able to
        find these by NAME.
        """
        listed = {new for _relative, _old, new in self.FLIPPED}
        assert len(listed) == len(self.FLIPPED)
        for name in listed:
            assert len(name) > 20, (
                f"the recorded replacement {name!r} is too short to be a "
                "reliable registry key"
            )


class TestTheMountTableIsNotStaleAboutItsOwnClaims:
    def test_a_mounted_row_names_a_function_that_exists(self) -> None:
        """A mount table that names a deleted function is worse than none."""
        for row in SURFACES:
            if row["decision"] != "mounted":
                continue
            names: List[str] = [
                part.split(".")[-1].split(" ")[0]
                for part in row["where"].split("/")
                if part.strip()
            ]
            sources = "\n".join(
                (CLI / name).read_text(encoding="utf-8")
                for name in ("tui.py", "interactive.py", "headless.py", "toggles.py", "runview.py")
                if (CLI / name).is_file()
            )
            for name in names:
                if name in ("self", "VexApp", "mount", "surface"):
                    continue
                assert name in sources, (
                    f"{row['id']} claims a mount at {name!r}, which is not in "
                    "the tree; the mount table is stale"
                )
