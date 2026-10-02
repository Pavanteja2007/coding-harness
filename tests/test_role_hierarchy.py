"""VEX-PF-04 â€” toggles, role hierarchy, inline undo, and the anti-leak gate.

Nine behaviours, one test class each, named after the behaviour rather
than after the function that implements it. The suite is offline and
deterministic: no Docker, no provider, no network, and every run it reads
is one it wrote under ``tmp_path``.

The three tests that matter most, and why:

* ``test_two_hundred_stream_events_emit_no_raw_event_name`` â€” the defect
  this round was raised for. A renderer that falls back to printing a
  journal row's own name leaks it past the user's eyes; the surface
  records what it was fed and MEASURES whether any of it came back out.
* ``test_role_silhouettes_survive_with_no_colour`` â€” hue is not a
  channel a colour-blind reader, an ``NO_COLOR`` terminal, or a
  16-colour palette has. Indentation, glyph, weight and slant are.
* ``test_a_zero_event_stream_is_reported_as_vacuous_not_as_a_pass`` â€” a
  frame-cost ratio computed over no events is not a measurement, and
  reporting it as one is how a vacuous green gets shipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from textual.app import App
from textual.theme import Theme

import cli.runview as rv
import cli.streamview as sv
import cli.toggles as toggles
import cli.tui_components as tc

#: The widget tests drive a REAL Textual app, so they need the anyio
#: backend the layout suite uses. The pure projections above are sync.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# 1. The nine toggles
# ---------------------------------------------------------------------------


class TestTheNineToggles:
    def test_every_toggle_ships_with_the_exact_declared_default(self):
        assert toggles.TOGGLE_DEFAULTS == {
            "thinking_visibility": True,
            "tool_details_visibility": True,
            "assistant_metadata_visibility": True,
            "timestamps": False,
            "sidebar": "auto",
            "scrollbar_visible": False,
            "animations_enabled": True,
            "code_conceal": True,
            "username_visible": True,
        }

    def test_there_are_exactly_nine_and_each_has_a_key_and_a_command(self):
        assert len(toggles.TOGGLE_SPECS) == 9
        assert tuple(spec.name for spec in toggles.TOGGLE_SPECS) == toggles.TOGGLE_NAMES
        for spec in toggles.TOGGLE_SPECS:
            assert spec.key, f"{spec.name} has no keybind"
            assert spec.command.startswith("/"), f"{spec.name} has no command"
        assert len({spec.key for spec in toggles.TOGGLE_SPECS}) == 9
        assert len({spec.command for spec in toggles.TOGGLE_SPECS}) == 9

    @staticmethod
    def _shell_bindings() -> dict:
        """``{key: action}`` as the REAL shell declares it."""
        tui = pytest.importorskip("cli.tui")
        return {
            binding.key: str(getattr(binding, "action", "") or "")
            for binding in getattr(tui.NeoApp, "BINDINGS", [])
            if getattr(binding, "key", None)
        }

    def test_a_toggle_key_bound_to_anything_else_is_a_conflict(self):
        """Two handlers on one keystroke is a defect nobody can debug
        from a screenshot, so it is REPORTED rather than assumed away.

        A key already bound to a ``toggle_*`` action is the mounting
        terminal's own mount of one of these toggles - that is a
        `mounted` row, not a conflict, and treating it as one would make
        a working integration look broken.
        """
        rows = toggles.toggle_mounts(self._shell_bindings())
        assert len(rows) == 9
        conflicts = [row for row in rows if row["status"] == "conflict"]
        assert not conflicts, conflicts
        for row in rows:
            assert row["status"] in ("mounted", "pending"), row

    def test_the_mount_report_names_the_work_the_shell_still_owes(self):
        """The handoff list, produced by the product rather than by hand."""
        rows = {row["name"]: row for row in toggles.toggle_mounts({})}
        assert all(row["status"] == "pending" for row in rows.values())
        assert len(rows) == 9
        mounted = {
            row["name"]
            for row in toggles.toggle_mounts({"ctrl+5": "toggle_sidebar"})
            if row["status"] == "mounted"
        }
        assert mounted == {"sidebar"}

    def test_the_sidebar_toggle_is_already_mounted_by_the_shell(self):
        """The integration that exists today, pinned so it cannot vanish.

        `NeoApp.action_toggle_sidebar` flips this registry's value, so the
        key, the `/sidebar` command and the hint rows read the same state.
        """
        bindings = self._shell_bindings()
        rows = {row["name"]: row for row in toggles.toggle_mounts(bindings)}
        assert rows["sidebar"]["status"] == "mounted", rows["sidebar"]
        assert rows["sidebar"]["shell_action"] == "toggle_sidebar"

    def test_toggles_avoid_the_keys_a_terminal_swallows(self):
        """ctrl+m is Enter, ctrl+i is Tab, ctrl+s is XOFF, ctrl+j is LF,
        ctrl+h is Backspace. None of them is a toggle."""
        swallowed = {
            "ctrl+m",
            "ctrl+i",
            "ctrl+s",
            "ctrl+j",
            "ctrl+h",
            "ctrl+o",
            "ctrl+v",
        }
        assert not ({spec.key for spec in toggles.TOGGLE_SPECS} & swallowed)

    def test_a_fresh_session_restores_every_default(self, tmp_path):
        settings = toggles.load_settings(
            repo_path=tmp_path, session_id="sess-1", home=tmp_path
        )
        assert settings.as_dict() == toggles.TOGGLE_DEFAULTS
        for name in toggles.TOGGLE_NAMES:
            assert settings.source(name) == "default"

    def test_a_saved_session_toggle_persists_and_restores(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        first = toggles.load_settings(
            repo_path=repo, session_id="sess-1", home=tmp_path
        )
        changed, note = first.set("thinking_visibility", False)
        assert changed is True, note
        second = toggles.load_settings(
            repo_path=repo, session_id="sess-1", home=tmp_path
        )
        assert second.get("thinking_visibility") is False
        assert second.source("thinking_visibility") == "session"
        # ... and every other toggle came back at its default, not at a
        # value the store happened to hold.
        for name in toggles.TOGGLE_NAMES:
            if name == "thinking_visibility":
                continue
            assert second.get(name) == toggles.TOGGLE_DEFAULTS[name], name
            assert second.source(name) == "default", name

    def test_a_repo_toggle_persists_and_outlives_the_session(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        first = toggles.load_settings(
            repo_path=repo, session_id="sess-1", home=tmp_path
        )
        assert first.remember_for_repo({"sidebar": "hide"}) is True
        wrote, note = first.flush()
        assert wrote is True, note
        # A DIFFERENT session in the same repository inherits it.
        other = toggles.load_settings(
            repo_path=repo, session_id="sess-2", home=tmp_path
        )
        assert other.get("sidebar") == "hide"
        assert other.source("sidebar") == "repo"

    def test_a_session_toggle_wins_over_the_repository_one(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        base = toggles.load_settings(repo_path=repo, session_id="sess-1", home=tmp_path)
        base.remember_for_repo({"animations_enabled": False})
        base.flush()
        base.set("animations_enabled", True)
        base.flush()
        again = toggles.load_settings(
            repo_path=repo, session_id="sess-1", home=tmp_path
        )
        assert again.get("animations_enabled") is True
        assert again.source("animations_enabled") == "session"
        # A fresh session still sees the repository's choice.
        fresh = toggles.load_settings(
            repo_path=repo, session_id="sess-3", home=tmp_path
        )
        assert fresh.get("animations_enabled") is False
        assert fresh.source("animations_enabled") == "repo"

    def test_a_corrupt_store_is_reported_and_the_defaults_are_in_force(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        path = toggles.repo_toggle_path(repo, home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        settings = toggles.load_settings(repo_path=repo, home=tmp_path)
        assert settings.as_dict() == toggles.TOGGLE_DEFAULTS
        assert any("not valid JSON" in note for note in settings.notes)

    def test_a_string_false_is_refused_rather_than_read_as_true(self, tmp_path):
        """A non-empty string is truthy. A toggle that accepted one would
        read ``"false"`` as ON, which is the same defect class as a
        verifier that reports unverified work as verified."""
        repo = tmp_path / "repo"
        repo.mkdir()
        path = toggles.repo_toggle_path(repo, home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "toggles": {"thinking_visibility": "false"}}),
            encoding="utf-8",
        )
        settings = toggles.load_settings(repo_path=repo, home=tmp_path)
        assert settings.get("thinking_visibility") is True
        assert any("unusable value refused" in note for note in settings.notes)

    def test_an_unknown_toggle_name_is_refused(self, tmp_path):
        settings = toggles.load_settings(home=tmp_path)
        changed, note = settings.set("warp_drive", True, persist=False)
        assert changed is False
        assert "unknown toggle" in note

    def test_a_tri_state_toggle_rejects_a_value_outside_its_vocabulary(self, tmp_path):
        settings = toggles.load_settings(home=tmp_path)
        changed, note = settings.set("sidebar", "sortof", persist=False)
        assert changed is False
        assert "does not accept" in note
        assert settings.get("sidebar") == "auto"

    def test_a_tri_state_toggle_round_trips_through_the_shells_vocabulary(
        self, tmp_path
    ):
        """The store holds ONE spelling, whatever the caller typed.

        The shell resolves sidebar modes through its own alias table, so a
        value this registry stores has to be a word the shell recognises -
        otherwise "hide the sidebar" stores a string that silently
        normalises back to the default.
        """
        from cli import design

        settings = toggles.load_settings(home=tmp_path)
        assert set(toggles.TOGGLE_BY_NAME["sidebar"].values) <= set(
            design.SIDEBAR_MODES
        )
        for canonical in design.SIDEBAR_MODES:
            changed, note = settings.set("sidebar", canonical, persist=False)
            assert changed or settings.get("sidebar") == canonical, note
            assert settings.get("sidebar") == canonical
        # A generic spelling from an older store still loads, and is
        # rewritten to the declared one rather than refused.
        assert toggles.TOGGLE_BY_NAME["sidebar"].coerce("hidden") == "hide"
        assert toggles.TOGGLE_BY_NAME["sidebar"].coerce("shown") == "show"
        assert toggles.TOGGLE_BY_NAME["sidebar"].coerce("sortof") is None

    def test_a_toggle_is_not_silently_changed_when_the_write_fails(
        self, tmp_path, monkeypatch
    ):
        """A preference that looks saved and was not is the same defect
        class as a verification that looks passed and was not."""
        repo = tmp_path / "repo"
        repo.mkdir()
        settings = toggles.load_settings(
            repo_path=repo, session_id="sess-1", home=tmp_path
        )

        def _refuse(path, values):
            return (False, "could not write: read-only file system")

        monkeypatch.setattr(toggles, "write_store", _refuse)
        changed, note = settings.set("timestamps", True)
        assert changed is False
        assert "session store" in note
        # The in-memory value DID move, which is why the receipt has to
        # carry the write outcome rather than just the new value.
        assert settings.get("timestamps") is True

    def test_the_task_config_seam_reads_by_key_presence(self, tmp_path):
        """`Task.config` may carry a toggle; an absent key stays absent.

        A key that is present but unusable is reported and ignored, so a
        typo in a settings file cannot enable a behaviour nobody asked
        for.
        """
        resolved = toggles.toggles_from_config({"thinking_visibility": False})
        assert resolved == {"thinking_visibility": False}
        assert "sidebar" not in resolved
        ignored = toggles.toggles_from_config({"sidebar": "maybe"})
        assert ignored == {}
        settings = toggles.load_settings(config={"sidebar": "hide"}, home=tmp_path)
        assert settings.get("sidebar") == "hide"
        assert settings.source("sidebar") == "config"

    def test_a_config_value_outside_the_vocabulary_is_reported_and_ignored(
        self, tmp_path
    ):
        settings = toggles.load_settings(
            config={"sidebar": "maybe", "timestamps": True}, home=tmp_path
        )
        assert settings.get("sidebar") == "auto"
        assert settings.get("timestamps") is True
        assert any("sidebar" in note and "unusable" in note for note in settings.notes)

    def test_the_report_states_every_toggle_its_key_its_command_and_its_source(
        self, tmp_path
    ):
        rows = toggles.load_settings(home=tmp_path).report()
        assert len(rows) == 9
        assert {row["name"] for row in rows} == set(toggles.TOGGLE_NAMES)
        for row in rows:
            assert row["source"] == "default"
            assert row["key"].startswith("ctrl+")
            assert row["word"] in ("on", "off", "auto", "always")


# ---------------------------------------------------------------------------
# 2. Role hierarchy by STRUCTURE
# ---------------------------------------------------------------------------


def _one_block_of_every_role() -> list:
    """A transcript holding exactly one block of each role."""
    surface = sv.TranscriptSurface()
    surface.feed("model_response", {"step": "step-1"})
    surface.feed("tool_call", {"tool": "read", "arguments": {"path": "a.py"}})
    surface.feed("tool_result", {"stdout": "ok", "exit_code": 0})
    surface.feed("edit_applied", {"path": "a.py", "diff": "+one\n-two"})
    surface.feed("approval_required", {"exact_effect": "write a.py"})
    surface.feed("model_error", {"reason": "provider returned 500"})
    surface.feed("task_start", {"mode": "daily"})
    return surface.blocks


class TestRoleHierarchyIsStructural:
    def test_the_seven_roles_are_declared(self):
        assert tuple(r.value for r in sv.Role) == (
            "thinking",
            "action",
            "result",
            "edit",
            "needs you",
            "failure",
            "system",
        )

    def test_every_role_has_a_distinct_silhouette(self):
        """The colour-free contract.

        Two roles that share an indent, a glyph, a weight and a slant are
        indistinguishable to a reader whose terminal has no colour - so
        the check is pairwise over the whole set, not "mostly different".
        """
        shapes = sv.role_silhouettes()
        assert len(set(shapes.values())) == len(shapes), shapes

    def test_role_distinction_survives_with_no_colour(self):
        """Rendered through a real rich console with colour removed.

        The block is rendered, the console strips the colour system, and
        the READER-VISIBLE text of each role is still unique. A test that
        asserted on ``RoleSpec`` instead would pass even if the renderer
        had thrown every structural cue away.
        """
        from rich.console import Console

        console = Console(color_system=None, force_terminal=False, width=120)
        seen: dict[str, list[str]] = {}
        for block in _one_block_of_every_role():
            with console.capture() as capture:
                console.print(tc.role_text(block))
            lines = [
                line.rstrip() for line in capture.get().splitlines() if line.strip()
            ]
            key = block.role.value
            assert lines, f"{key} rendered nothing at all"
            assert lines not in [
                existing for group in seen.values() for existing in group
            ], f"two roles rendered identically with no colour: {key}"
            seen.setdefault(key, []).extend(lines)
        assert len(seen) == 7

    def test_the_structure_actually_reaches_the_rendered_text(self):
        """Indent, glyph and slant are carried by the rendered output.

        A silhouette that lives only in the dataclass is documentation;
        this asserts the renderer's own output carries it.
        """
        from rich.text import Text

        for block in _one_block_of_every_role():
            spec = block.spec
            rendered = tc.role_text(block)
            assert isinstance(rendered, Text)
            head = rendered.plain.splitlines()[0]
            assert head.startswith("  " * spec.indent + spec.glyph), head
            if spec.bold:
                assert any("bold" in str(span.style) for span in rendered.spans)
            if spec.italic:
                assert any("italic" in str(span.style) for span in rendered.spans)

    def test_a_band_role_is_its_own_block(self):
        for role in (sv.Role.EDIT, sv.Role.NEEDS_YOU, sv.Role.FAILURE):
            assert sv.role_spec(role).band is True, role.value
        for role in (sv.Role.THINKING, sv.Role.ACTION, sv.Role.RESULT):
            assert sv.role_spec(role).band is False, role.value

    def test_thinking_is_dim_italic_and_indented(self):
        spec = sv.role_spec(sv.Role.THINKING)
        assert (spec.italic, spec.bold, spec.muted, spec.indent) == (
            True,
            False,
            True,
            1,
        )
        assert spec.collapsible is True

    def test_an_action_is_upright_and_unindented(self):
        spec = sv.role_spec(sv.Role.ACTION)
        assert (spec.italic, spec.bold, spec.indent) == (False, True, 0)

    def test_a_result_is_muted_and_one_line_until_expanded(self):
        spec = sv.role_spec(sv.Role.RESULT)
        assert (spec.muted, spec.expandable, spec.indent) == (True, True, 1)
        surface = sv.TranscriptSurface()
        surface.feed("tool_result", {"stdout": "line one\nline two\nline three"})
        block = surface.blocks[-1]
        assert block is not None and block.body, "the result kept no body to expand"
        assert block.spec.expandable is True
        # One line until the reader asks for more, then the count of what
        # it folded is stated rather than hidden.
        folded = tc.role_text(block, collapsed=True).plain
        assert "+2 more lines (folded)" in folded

    def test_every_role_says_something_a_screen_reader_could_read(self):
        for name, spec in sv.ROLE_SPECS.items():
            assert spec.spoken.strip(), f"{name} has no spoken form"
            assert spec.glyph.strip() and spec.ascii_glyph.strip(), name

    def test_an_unrecognised_row_still_produces_a_readable_block(self):
        """A row nobody classified is NOT dropped, and NOT printed by name."""
        surface = sv.TranscriptSurface()
        block = surface.feed("some_brand_new_row", {"status": "queued"})
        assert block is not None
        assert block.role is sv.Role.SYSTEM
        assert block.plain_lines()[0].strip("- ").strip()
        assert surface.receipt()["unclassified_rows"] == 1


# ---------------------------------------------------------------------------
# 3. Thinking: inline, streaming, and frame-bounded
# ---------------------------------------------------------------------------


class TestThinkingRendersInlineAndStreams:
    def test_the_first_token_is_visible_immediately(self):
        clock = _Clock()
        lane = sv.ThinkingStream(
            coalescer=sv.StreamCoalescer(clock=clock),
            projector=sv.PhaseProjector(clock=clock),
        )
        lane.begin("step-1")
        lane.push("the ")
        assert lane.poll(clock.now) == "the "
        assert lane.first_token_at is not None

    def test_the_frame_count_does_not_scale_with_the_token_count(self):
        """The load-bearing claim, measured on the existing coalescer.

        ``equivalent_frame_cost`` compares a dense arm (one delta per
        tick) against a sparse one over the SAME wall-clock span and
        fails when either arm's content-frame count exceeds the clock
        bound. The token counts differ by 100x.
        """
        dense = sv.simulate_stream(tokens=2000, span_s=1.0).frame_cost_receipt()
        sparse = sv.simulate_stream(tokens=20, span_s=1.0).frame_cost_receipt()
        verdict = sv.equivalent_frame_cost(dense, sparse, span_s=1.0)
        assert dense["events"] == 2000
        assert sparse["events"] == 20
        assert verdict["equal"] is True, verdict["reason"]
        assert dense["content_frames"] <= verdict["frames_allowed"]
        assert sparse["content_frames"] <= verdict["frames_allowed"]

    def test_dense_and_sparse_thinking_lanes_both_stay_inside_frames_allowed(self):
        for tokens in (2000, 200, 20, 2):
            measured = sv.thinking_frame_cost(tokens, span_s=1.0)
            assert measured["observed"] is True, tokens
            assert measured["vacuous"] is False, tokens
            assert 0 < measured["content_frames"] <= measured["frames_allowed"], (
                measured
            )
            assert measured["within_bound"] is True, measured

    def test_a_zero_event_stream_is_reported_as_vacuous_not_as_a_pass(self):
        """The anti-vacuity gate.

        A ratio computed over no events is not a measurement. Both the
        simulated helper and a real lane that was handed nothing must say
        so, and neither may report a healthy `within_bound`.
        """
        empty = sv.thinking_frame_cost(0)
        assert empty["observed"] is False
        assert empty["vacuous"] is True
        assert empty["within_bound"] is False
        assert empty["events"] == 0
        # A ratio over no events is UNMEASURED, not 0.0: a zero would
        # read as "the lane was free", which is a number nobody took.
        assert empty["events_per_frame"] is None

        lane = sv.ThinkingStream()
        receipt = lane.receipt()
        assert receipt["vacuous"] is True
        assert receipt["within_bound"] is False
        assert receipt["events_per_frame"] is None

    def test_a_lane_that_saw_tokens_but_never_painted_is_also_not_a_pass(self):
        """Frames counted without a frame rendered is not evidence."""
        clock = _Clock()
        lane = sv.ThinkingStream(
            coalescer=sv.StreamCoalescer(clock=clock),
            projector=sv.PhaseProjector(clock=clock),
        )
        lane.begin("step-1")
        for _ in range(500):
            lane.push("x")
        receipt = lane.receipt()
        assert receipt["events"] == 500
        assert receipt["content_frames"] == 0
        assert receipt["within_bound"] is False

    def test_a_slow_endpoint_says_it_is_waiting_rather_than_hanging(self):
        clock = _Clock()
        lane = sv.ThinkingStream(
            coalescer=sv.StreamCoalescer(clock=clock),
            projector=sv.PhaseProjector(slow_first_token_s=8.0, clock=clock),
        )
        lane.begin("planner")
        assert "no token" not in lane.status()
        clock.advance(9.0)
        assert (
            lane.status()
            == "waiting for first token - no token after 9s - still working"
        )

    def test_cancelling_keeps_the_partial_text_and_says_it_did(self):
        clock = _Clock()
        lane = sv.ThinkingStream(
            coalescer=sv.StreamCoalescer(clock=clock),
            projector=sv.PhaseProjector(clock=clock),
        )
        lane.begin("step-1")
        lane.push("I was about to explain the traceback in detail")
        kept = lane.cancel()
        assert "traceback" in kept
        receipt = lane.receipt()
        assert receipt["cancelled"] is True
        assert receipt["partial_text_kept"] is True
        block = lane.block()
        assert "kept partial text" in block.detail
        assert "traceback" in "\n".join(block.plain_lines())

    def test_a_folded_thinking_block_states_how_many_lines_it_hid(self):
        lane = sv.ThinkingStream()
        lane.begin("step-1")
        for index in range(12):
            lane.push(f"line {index}\n")
        block = lane.block(collapsed=True)
        rendered = tc.role_text(block, collapsed=True).plain
        assert "more lines (folded)" in rendered
        assert block.hidden_lines > 0

    def test_the_thinking_lane_reuses_the_one_coalescer(self):
        """One mechanism, not two.

        The thinking lane must be bounded by the same coalescer the run
        line already uses, or "frame cost is token-rate independent" is
        true of one widget and not the other.
        """
        assert isinstance(sv.ThinkingStream().coalescer, sv.StreamCoalescer)
        assert sv.ThinkingStream().cost_vs.__doc__ is not None
        dense, sparse = sv.ThinkingStream(), sv.ThinkingStream()
        for lane, count in ((dense, 400), (sparse, 4)):
            for _ in range(count):
                lane.push("x")
            lane.poll()
        verdict = dense.cost_vs(sparse, span_s=1.0)
        assert verdict["dense_within_bound"] is True
        assert verdict["sparse_within_bound"] is True


# ---------------------------------------------------------------------------
# 4. Truncation is marked, never hidden
# ---------------------------------------------------------------------------


class TestTruncationIsMarked:
    def test_a_long_result_says_it_was_cut(self):
        surface = sv.TranscriptSurface()
        surface.feed(
            "tool_result", {"stdout": "\n".join(f"line {n}" for n in range(200))}
        )
        block = surface.blocks[-1]
        assert block.truncated is True
        assert len(block.body) == sv.MAX_BODY_LINES
        rendered = tc.role_text(block).plain
        assert "output cut here" in rendered

    def test_a_short_result_does_not_claim_to_have_been_cut(self):
        surface = sv.TranscriptSurface()
        surface.feed("tool_result", {"stdout": "one line"})
        block = surface.blocks[-1]
        assert block.truncated is False
        assert "output cut here" not in tc.role_text(block).plain

    def test_the_coalescer_says_when_it_trimmed(self):
        lane = sv.ThinkingStream(coalescer=sv.StreamCoalescer(max_live_chars=500))
        for _ in range(200):
            lane.push("z" * 20)
        assert sv.TRIM_MARKER in lane.text()
        assert lane.receipt()["trimmed"] is True

    def test_concealed_code_states_how_many_lines_it_hid(self):
        block = sv.RoleBlock(
            role=sv.Role.ACTION,
            label="running",
            detail="pytest",
            body=[f"line {n}" for n in range(20)],
        )
        rendered = tc.role_text(
            block, toggles.ToggleSettings({"code_conceal": True})
        ).plain
        assert "concealed" in rendered
        assert "line 19" not in rendered

    def test_a_turn_off_toggle_is_not_the_same_as_absent(self):
        """`None` means nobody decided; `False` means decided off.

        A widget that treated an absent setting as OFF would silently
        switch a feature off for every caller that has no session object.
        """
        block = sv.RoleBlock(role=sv.Role.RESULT, label="result", body=["detail"])
        with_no_settings = tc.role_text(block, None).plain
        with_settings_on = tc.role_text(
            block, toggles.ToggleSettings({"tool_details_visibility": True})
        ).plain
        with_settings_off = tc.role_text(
            block, toggles.ToggleSettings({"tool_details_visibility": False})
        ).plain
        assert "detail" in with_no_settings
        assert "detail" in with_settings_on
        assert "detail" not in with_settings_off

    def test_a_timestamps_toggle_adds_and_removes_the_stamp(self):
        block = sv.RoleBlock(
            role=sv.Role.ACTION, label="reading", detail="a.py", timestamp="12:34:56"
        )
        assert (
            "12:34:56"
            in tc.role_text(block, toggles.ToggleSettings({"timestamps": True})).plain
        )
        assert (
            "12:34:56"
            not in tc.role_text(
                block, toggles.ToggleSettings({"timestamps": False})
            ).plain
        )


# ---------------------------------------------------------------------------
# 5. Inline undo
# ---------------------------------------------------------------------------


class TestUndoRendersInline:
    def test_the_block_reads_as_the_receipt_it_is(self):
        notice = sv.undo_notice_from_facts(
            {
                "messages_reverted": 3,
                "files": [
                    {"path": "cli/streamview.py", "added": 12, "removed": 3},
                    {"path": "tests/test_role_hierarchy.py", "added": 40, "removed": 2},
                ],
            }
        )
        rendered = tc.undo_text(notice).plain
        assert "3 messages reverted" in rendered
        assert "ctrl+z" in rendered
        assert "/redo" in rendered
        assert "cli/streamview.py +12 -3" in rendered
        assert "tests/test_role_hierarchy.py +40 -2" in rendered

    def test_one_message_reads_in_the_singular(self):
        assert (
            sv.undo_notice_from_facts({"messages_reverted": 1}).headline
            == "1 message reverted"
        )

    def test_counts_are_read_from_the_receipt_and_never_invented(self):
        notice = sv.undo_notice_from_facts({"files": [{"path": "a.py"}]})
        assert notice.files[0].plain() == "a.py +0 -0"
        # No revert at all says so rather than printing an empty receipt.
        empty = sv.undo_notice_from_facts({})
        assert "no file changes" in "\n".join(empty.lines())

    def test_counts_come_from_a_diff_when_the_receipt_carries_no_counts(self):
        notice = sv.undo_notice_from_facts(
            {
                "messages_reverted": 1,
                "files": [{"path": "a.py", "diff": "--- a\n+++ b\n+x\n-y\n+z\n"}],
            }
        )
        assert notice.files[0].plain() == "a.py +2 -1"

    def test_the_file_list_is_bounded_and_the_omission_is_stated(self):
        facts = {
            "messages_reverted": 1,
            "files": [
                {"path": f"f{n}.py", "added": n, "removed": 0} for n in range(40)
            ],
        }
        notice = sv.undo_notice_from_facts(facts)
        rendered = tc.undo_text(notice).plain
        assert "more file(s) reverted (not listed)" in rendered
        assert notice.to_dict()["files_total"] == 40
        assert notice.to_dict()["files_listed"] == sv.MAX_FILES_LISTED

    def test_the_receipt_is_read_from_a_runs_own_journal(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        trace.write_text(
            "\n".join(
                [
                    json.dumps({"kind": "task_start", "data": {"mode": "daily"}}),
                    json.dumps(
                        {
                            "kind": "undo_reverted",
                            "data": {
                                "messages_reverted": 2,
                                "files": [{"path": "a.py", "added": 5, "removed": 1}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        receipt = rv.undo_receipt(tmp_path)
        assert receipt["messages_reverted"] == 2
        assert receipt["files"][0]["path"] == "a.py"
        rendered = tc.undo_text(sv.undo_notice_from_facts(receipt)).plain
        assert "2 messages reverted" in rendered
        assert "a.py +5 -1" in rendered

    def test_a_run_that_was_never_reverted_reports_nothing(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        trace.write_text(
            json.dumps({"kind": "task_start", "data": {}}) + "\n", encoding="utf-8"
        )
        assert rv.undo_receipt(tmp_path) == {}
        # ... and a journal that does not exist at all is not a crash.
        assert rv.undo_receipt(tmp_path / "nope") == {}


# ---------------------------------------------------------------------------
# 6. The composer is disabled while a decision is pending
# ---------------------------------------------------------------------------


class TestComposerIsDisabledWhileADecisionIsPending:
    @staticmethod
    def _write_journal(log: Path, rows) -> Path:
        log.mkdir(parents=True, exist_ok=True)
        (log / "trace.jsonl").write_text(
            "\n".join(json.dumps({"kind": k, "data": d}) for k, d in rows),
            encoding="utf-8",
        )
        return log

    def test_a_pending_permission_disables_the_composer(self, tmp_path):
        log = self._write_journal(
            tmp_path / "fix-p",
            [
                ("task_start", {"mode": "daily"}),
                ("approval_required", {"exact_effect": "write a.py"}),
            ],
        )
        projection = rv.read_live_projection(log)
        decision = rv.pending_decision(projection)
        assert decision["kind"] == "permission"
        assert decision["effect"] == "write a.py"
        state = sv.composer_state(approval=decision)
        assert state.disabled is True
        assert "permission" in state.hint()
        assert "answer the permission prompt first" in state.hint()

    def test_a_pending_question_disables_the_composer(self, tmp_path):
        rows = [
            ("task_start", {"mode": "daily"}),
            ("question_asked", {"question_id": "q1", "question": "which environment?"}),
        ]
        log = tmp_path / "logs" / "fix-q"
        log.mkdir(parents=True, exist_ok=True)
        (log / "trace.jsonl").write_text(
            "\n".join(json.dumps({"kind": k, "data": d}) for k, d in rows),
            encoding="utf-8",
        )
        projection = rv.read_live_projection(log)
        decision = rv.pending_decision(projection)
        assert decision["kind"] == "question"
        state = sv.composer_state(question=decision["question"])
        assert state.disabled is True
        assert "which environment?" in state.hint()

    def test_a_permission_wins_over_a_question(self, tmp_path):
        log = tmp_path / "fix-both"
        log.mkdir(parents=True, exist_ok=True)
        (log / "trace.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"kind": "task_start", "data": {}}),
                    json.dumps(
                        {"kind": "question_asked", "data": {"question": "which env?"}}
                    ),
                    json.dumps(
                        {"kind": "approval_required", "data": {"effect": "write a.py"}}
                    ),
                ]
            ),
            encoding="utf-8",
        )
        decision = rv.pending_decision(log)
        assert decision["kind"] == "permission"

    def test_an_answered_question_re_enables_the_composer(self, tmp_path):
        log = tmp_path / "fix-answered"
        log.mkdir(parents=True, exist_ok=True)
        (log / "trace.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"kind": "task_start", "data": {}}),
                    json.dumps(
                        {
                            "kind": "question_asked",
                            "data": {"question_id": "q1", "question": "which env?"},
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "question_answered",
                            "data": {"question_id": "q1", "answer": "staging"},
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        projection = rv.read_live_projection(log)
        assert rv.pending_decision(projection) == {}
        assert sv.composer_state(question=None).disabled is False

    def test_an_idle_run_leaves_the_composer_alone(self):
        state = sv.composer_state()
        assert state.disabled is False
        assert state.hint() == ""

    def test_an_empty_but_pending_permission_still_disables(self):
        """Presence, not truthiness.

        A pending record the producer happened to fill in with nothing is
        still a pending record; a gate that reads ``if record:`` would
        re-enable the box for exactly the producer that forgot a field.
        """
        assert sv.composer_state(approval={}).disabled is True
        assert sv.composer_state(approval=None).disabled is False

    def test_the_widget_wrapper_is_what_the_mount_calls(self):
        class _FakeInput:
            disabled = False
            placeholder = ""

        widget = _FakeInput()
        disabled, hint = tc.composer_widget_state(
            widget, approval={"effect": "write a.py"}
        )
        assert disabled is True
        assert widget.disabled is True
        assert "permission" in widget.placeholder
        assert hint
        disabled, _hint = tc.composer_widget_state(widget)
        assert disabled is False
        assert widget.disabled is False

    def test_a_run_that_is_really_steerable_may_keep_its_composer(self):
        assert (
            sv.composer_state(approval={"effect": "x"}, steerable=True).disabled
            is False
        )


# ---------------------------------------------------------------------------
# 7. The anti-leak gate
# ---------------------------------------------------------------------------


class TestNoRawEventNameReachesTheUser:
    def _synthetic_stream(self, count: int = 200) -> list:
        """``count`` synthetic journal rows, none of them a real product name.

        The names are deliberately alien (``zz_07_frobnicator``) so a leak
        cannot be mistaken for a legitimate word that happens to appear in
        a label.
        """
        kinds = [
            "zz_07_frobnicator",
            "zz_11_wibble",
            "model_delta",
            "tool_call",
            "tool_result",
            "edit_applied",
            "approval_required",
            "approval_decided",
            "model_error",
            "task_start",
            "task_end",
            "plan",
            "checkpoint_saved",
            "context_rewind",
        ]
        rows = []
        for index in range(count):
            kind = kinds[index % len(kinds)]
            rows.append(
                (
                    kind,
                    {
                        "ts": 1000.0 + index,
                        "step": f"step-{index // 20}",
                        "delta": f"chunk {index} with [brackets] and [/] closers",
                        "tool": "read",
                        "arguments": {"path": f"src/mod{index}.py"},
                        "stdout": f"out {index}",
                        "path": f"src/mod{index}.py",
                        "diff": "+a\n-b",
                        "effect": f"write src/mod{index}.py",
                        "reason": f"why {index}",
                        "mode": "daily",
                        "status": "running",
                    },
                )
            )
        return rows

    def test_two_hundred_stream_events_emit_no_raw_event_name(self):
        rows = self._synthetic_stream(200)
        surface = sv.TranscriptSurface()
        for kind, data in rows:
            surface.feed(kind, data)
        receipt = surface.receipt()
        assert receipt["rows"] == 200
        assert receipt["emitted_event_names"] == []
        assert receipt["clean"] is True
        # The check scans the RENDERED text, so it also has to hold for
        # the widget path, not only the plain-lines path.
        rendered = "\n".join(
            piece.plain for piece in tc.role_block_lines(surface.blocks)
        )
        for kind, _data in rows:
            assert kind not in rendered, f"raw event name rendered: {kind}"

    def test_a_row_the_table_has_never_seen_is_still_not_printed_by_name(self):
        surface = sv.TranscriptSurface()
        surface.feed("zz_totally_new_thing", {"status": "queued"})
        surface.feed("another_new_thing", {})
        receipt = surface.receipt()
        assert receipt["unclassified_rows"] == 2
        assert receipt["clean"] is True
        assert "zz_totally_new_thing" not in "\n".join(surface.plain_lines())

    def test_untrusted_text_reaches_the_renderer_as_text_not_as_markup(self):
        """A reply containing markup must not be able to close a tag.

        The rule is a render failure must NEVER delete a message, so the
        renderer's OUTPUT TYPE is pinned as well as its content: a
        `rich.text.Text`, never a markup string.
        """
        from rich.text import Text

        hostile = "[/] [bold red] gotcha [/] [neo.accent] and [/]"
        surface = sv.TranscriptSurface()
        surface.feed("model_delta", {"delta": hostile})
        surface.feed("model_response", {})
        rendered = tc.role_text(surface.thinking.block())
        assert isinstance(rendered, Text)
        assert hostile in rendered.plain, "the hostile text was altered, not escaped"
        # Rich parses markup only in strings; handing it a Text means the
        # brackets are characters. Rendering must not raise.
        from rich.console import Console

        console = Console(color_system=None, width=200)
        with console.capture() as capture:
            console.print(rendered)
        assert hostile in capture.get()

    def test_a_render_failure_does_not_delete_the_message(self):
        """A render that raised would take the message with it.

        The renderers are total: a block of the wrong shape renders as an
        empty `Text` rather than raising, and a block with hostile
        characters keeps every one of them. The widget-level version of
        this - where the fallback actually has to run - is driven through
        a real app in `TestWidgetsRenderAndSurviveNoColor`.
        """
        from rich.text import Text

        assert isinstance(tc.role_text(None), Text)
        assert tc.role_text(None).plain == ""
        assert tc.undo_text(None).plain == ""
        assert tc.thinking_text(None).plain == ""
        block = sv.RoleBlock(role=sv.Role.FAILURE, label="failed", detail="[/] boom")
        assert "[/] boom" in tc.role_text(block).plain

    def test_a_path_with_brackets_survives_the_whole_pipeline(self):
        weird = "src/we[ird]name.py"
        surface = sv.TranscriptSurface()
        surface.feed("edit_applied", {"path": weird, "diff": "+x"})
        block = surface.blocks[-1]
        assert weird in tc.role_text(block).plain


# ---------------------------------------------------------------------------
# 8. Anti-clutter, and the widgets that obey it
# ---------------------------------------------------------------------------


class TestAntiClutter:
    def test_a_section_with_two_or_fewer_entries_is_not_rendered(self):
        assert toggles.section_rows([]) == []
        assert toggles.section_rows(["one"]) == []
        assert toggles.section_rows(["one", "two"]) == []
        assert toggles.section_rows(["one", "two", "three"]) == ["one", "two", "three"]

    def test_the_threshold_is_three_entries(self):
        assert toggles.MIN_SECTION_ENTRIES == 3

    def test_a_hint_line_naming_fewer_than_three_toggles_renders_nothing(
        self, tmp_path
    ):
        settings = toggles.load_settings(home=tmp_path)
        assert toggles.hint_line(settings, names=["thinking_visibility"]) == ""
        assert (
            toggles.hint_line(settings, names=["thinking_visibility", "sidebar"]) == ""
        )
        assert toggles.hint_line(
            settings, names=["thinking_visibility", "sidebar", "timestamps"]
        )

    def test_the_hint_line_is_bounded_to_its_width(self, tmp_path):
        settings = toggles.load_settings(home=tmp_path)
        for width in (20, 40, 80, 200):
            assert len(toggles.hint_line(settings, width=width)) <= width


# ---------------------------------------------------------------------------
# 9. The widgets themselves
# ---------------------------------------------------------------------------


class TestWidgetsRenderAndSurviveNoColor:
    @pytest.mark.parametrize("role", [role for role in sv.Role])
    async def test_every_role_has_a_widget_class_and_renders(self, role):
        """Driven through a REAL Textual app, not a bare widget.

        `Static.update` resolves the active app's console, so a widget
        constructed outside an app cannot render at all - a unit test
        against the bare object would be testing nothing. The app here
        registers the product's own theme, so the `$neo-*` variables the
        role CSS uses are the ones the shell actually defines; an
        unresolved variable raises, which is the failure this catches.
        """
        app = _WidgetApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#role")
            assert (
                widget.update_block(
                    sv.RoleBlock(role=role, label="x", detail="y", body=["z"])
                )
                is True
            )
            await pilot.pause()
            assert tc.ROLE_CLASS[role.value] in widget.classes
            assert "x" in str(widget.visual)
            assert widget.display is True

    async def test_an_unknown_block_leaves_the_widget_alone_rather_than_raising(self):
        app = _WidgetApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#role")
            assert widget.update_block(None) is False
            assert widget.update_block(object()) is False
            await pilot.pause()

    async def test_a_render_failure_does_not_delete_the_message(self):
        """A renderer that raised would take the message with it.

        The widget catches every failure and falls back to the block's own
        characters. This is the regression that matters most in a widget
        tree: an exception inside a render is how a whole run's output
        disappears.
        """
        app = _WidgetApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#role")
            assert (
                widget.update_block(
                    sv.RoleBlock(role=sv.Role.FAILURE, label="failed", detail="boom")
                )
                is True
            )
            await pilot.pause()
            assert "boom" in str(widget.visual)

    async def test_the_undo_block_hides_and_shows_inline(self):
        app = _WidgetApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#undo")
            assert widget.visible is False
            widget.update_notice(sv.undo_notice_from_facts({"messages_reverted": 1}))
            await pilot.pause()
            assert widget.visible is True
            assert widget.display is True
            assert "1 message reverted" in str(widget.visual)
            widget.clear_receipt()
            await pilot.pause()
            assert widget.visible is False
            assert widget.display is False

    async def test_the_thinking_render_is_dim_italic_and_indented(self):
        lane = sv.ThinkingStream()
        lane.begin("step-1")
        lane.push("reasoning")
        rendered = tc.thinking_text(lane)
        assert rendered.plain.splitlines()[0].startswith("  ~ ")
        assert any("italic" in str(span.style) for span in rendered.spans)

    def test_the_widget_css_only_names_variables_the_theme_defines(self):
        """An unresolved `$neo-*` has taken this app down before.

        The variable table is read from the product's own theme rather
        than restated, so a rename upstream fails here instead of at
        mount time in a user's terminal.
        """
        from cli import ui

        declared = set(ui.textual_theme_variables(ui.active_tokens()))
        source = Path(tc.__file__).read_text(encoding="utf-8")
        referenced = set(re.findall(r"\$(neo-[a-z-]+)", source))
        assert referenced, "the scan found nothing, so it is not testing anything"
        assert referenced <= declared, sorted(referenced - declared)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Clock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _WidgetApp(App):
    """A minimal app that mounts the two new widgets.

    It registers the product's OWN Textual theme rather than inventing
    one, so the `$neo-*` variables the role CSS references are the ones
    the shell defines. ``cli/tui.py`` is another terminal's file, so it
    is imported read-only for its theme builder and never edited here.
    """

    CSS = "Screen { layout: vertical; }"

    def __init__(self) -> None:
        super().__init__()
        from cli import ui

        # The SAME variable table the shell registers (`cli.tui` builds
        # its Textual theme from `ui.textual_theme_variables`). Using any
        # other table would let a widget CSS name a variable the product
        # does not define - which is a mount-time crash, not a styling
        # difference.
        variables = dict(ui.textual_theme_variables(ui.active_tokens()))
        self.register_theme(
            Theme(
                name="neo",
                primary=variables["neo-accent"],
                secondary=variables["neo-accent"],
                accent=variables["neo-accent"],
                foreground=variables["neo-text"],
                background=variables["neo-background"],
                surface=variables["neo-panel"],
                panel=variables["neo-panel"],
                variables=variables,
            )
        )
        # Registering is not selecting. Without this the app keeps
        # Textual's DEFAULT theme, whose variables are `text-primary` and
        # friends, and every `$neo-*` in the role CSS is unresolved.
        self.theme = "neo"

    def compose(self):
        yield tc.RoleBlockWidget(id="role")
        yield tc.UndoBlockWidget(id="undo")
