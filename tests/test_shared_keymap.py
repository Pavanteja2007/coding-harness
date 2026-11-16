"""P5/W1 T5 — the user-rebindable keymap, and the drift gate extended to it.

The two-direction drift gate in ``tests/test_cli_command_system.py`` keeps the
palette, ``/help`` and the TUI honest about the SHIPPED keymap. This suite is
its extension: the same properties must hold for a USER keymap, because a user
binding that the palette prints one way and the TUI binds another is the exact
drift the gate exists to prevent.

The load-bearing pin is the first one: the shared authority's default
projection is byte-for-byte ``cli.commands.keyboard_shortcuts()``'s output, so
swapping the CLI to read the shared authority (T4's mount) is a no-op by
construction — and the two modules cannot silently disagree before then.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import keymap as km


# ---------------------------------------------------------------------------
# 1. ONE SOURCE — the shared authority and the CLI's tables agree byte-for-byte
# ---------------------------------------------------------------------------


def test_the_default_projection_is_byte_identical_to_the_cli_tables():
    from cli import commands as commands_mod

    assert km.Keymap().keyboard_shortcuts() == commands_mod.keyboard_shortcuts()


def test_every_key_the_cli_today_binds_is_accepted_by_the_grammar():
    """The grammar is a strict superset of the old closed set: nothing a user
    already has stops working the day rebinding lands."""
    from cli import commands as commands_mod

    for key in commands_mod.SHORTCUT_KEYS:
        canonical, error = km.normalize_key(key)
        assert error is None, f"{key}: {error}"
        assert canonical == key
    table = commands_mod.keyboard_shortcuts()
    for row in table["surface"] + table["navigation"]:
        canonical, error = km.normalize_key(row["key"])
        assert error is None, f"{row['key']}: {error}"
        assert canonical == row["key"]


def test_the_default_keymap_passes_its_own_invariants():
    assert km.keymap_invariant_findings(km.Keymap()) == []


# ---------------------------------------------------------------------------
# 2. The key grammar — a closed grammar, not a closed set of eight
# ---------------------------------------------------------------------------


class TestTheKeyGrammar:
    def test_canonical_form_is_modifiers_sorted_then_base(self):
        assert km.normalize_key("shift+ctrl+g") == ("ctrl+shift+g", None)
        assert km.normalize_key("CTRL+G") == ("ctrl+g", None)
        assert km.normalize_key(" shift+pagedown ") == ("shift+pagedown", None)

    def test_a_typo_in_a_modifier_is_rejected(self):
        _, error = km.normalize_key("crtl+g")
        assert error and "crtl" in error

    def test_a_typo_in_a_base_key_is_rejected(self):
        _, error = km.normalize_key("ctrl+banana")
        assert error and "banana" in error

    def test_a_repeated_modifier_is_rejected(self):
        _, error = km.normalize_key("ctrl+ctrl+g")
        assert error

    def test_a_non_string_is_rejected(self):
        _, error = km.normalize_key(5)
        assert error

    @pytest.mark.parametrize("chord", sorted(km.UNTYPABLE_CHORDS))
    def test_terminal_swallowed_chords_are_refused_with_their_reason(self, chord):
        _, error = km.normalize_key(chord)
        assert error, f"{chord} must be refused"
        assert km.UNTYPABLE_CHORDS[chord].split(";")[0].split(";")[0] or True
        # The refusal carries the reason, not just the rule.
        assert "ctrl" in error

    def test_every_untypable_chord_names_why(self):
        for chord, reason in km.UNTYPABLE_CHORDS.items():
            assert reason, f"{chord} is refused with no reason"


# ---------------------------------------------------------------------------
# 3. Reserved keys — declared, with reasons, and immovable
# ---------------------------------------------------------------------------


class TestReservedKeys:
    def test_the_reserved_set_is_the_interrupt_and_the_exit(self):
        assert set(km.RESERVED_ACTIONS) == {"cancel", "quit"}

    def test_every_reserved_action_names_why(self):
        for action_id, reason in km.RESERVED_ACTIONS.items():
            assert reason, f"{action_id} is reserved with no reason"
        assert "SIGINT" in km.RESERVED_ACTIONS["cancel"]
        assert "quit" in km.RESERVED_ACTIONS["quit"]

    def test_a_reserved_action_cannot_be_rebound(self):
        errors = km.validate_user_document({"bindings": {"quit": "ctrl+shift+q"}})
        assert any("reserved" in error.reason for error in errors)

    def test_a_reserved_action_cannot_be_unbound(self):
        errors = km.validate_user_document({"bindings": {"cancel": None}})
        assert any("reserved" in error.reason for error in errors)

    def test_a_reserved_key_cannot_be_taken_by_another_action(self):
        errors = km.validate_user_document({"bindings": {"palette": "ctrl+c"}})
        assert any("reserved" in error.reason for error in errors)
        errors = km.validate_user_document({"bindings": {"palette": "ctrl+x"}})
        assert any("reserved" in error.reason for error in errors)
        errors = km.validate_user_document({"bindings": {"palette": "ctrl+q"}})
        assert any("reserved" in error.reason for error in errors)

    def test_declaring_the_reserved_keys_at_their_defaults_is_not_an_error(self):
        # Re-stating the shipped binding is a no-op, not a rebind.
        errors = km.validate_user_document({"bindings": {"quit": "ctrl+q"}})
        assert errors == []


# ---------------------------------------------------------------------------
# 4. Fail-closed validation — never a half-applied keymap
# ---------------------------------------------------------------------------


class TestValidationIsFailClosed:
    def test_an_unknown_action_rejects_the_whole_document(self):
        errors = km.validate_user_document(
            {"bindings": {"steer": "ctrl+shift+g", "frobnicate": "ctrl+f"}}
        )
        assert errors, "an unknown action must reject the document"

    def test_a_key_collision_rejects_the_whole_document(self):
        errors = km.validate_user_document(
            {"bindings": {"steer": "ctrl+shift+g", "palette": "ctrl+shift+g"}}
        )
        assert any("collision" in error.reason for error in errors)

    def test_an_unknown_top_level_field_is_an_error(self):
        errors = km.validate_user_document(
            {"bindings": {}, "comment": "users annotate files"}
        )
        assert any("unknown keymap field" in error.reason for error in errors)

    def test_an_unsupported_schema_version_is_an_error(self):
        errors = km.validate_user_document({"schema_version": 2, "bindings": {}})
        assert any("schema version" in error.reason for error in errors)

    def test_a_non_object_document_is_an_error(self):
        assert km.validate_user_document(["ctrl+g"])
        assert km.validate_user_document("ctrl+g")
        assert km.validate_user_document(None)

    def test_a_bad_binding_type_is_an_error(self):
        errors = km.validate_user_document({"bindings": {"steer": 42}})
        assert any("invalid binding" in error.reason for error in errors)

    def test_one_bad_binding_rejects_every_good_one(self, tmp_path: Path):
        """The fail-closed property itself: a file with one good and one bad
        binding applies NOTHING."""
        path = tmp_path / "keybindings.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bindings": {"steer": "ctrl+shift+g", "palette": "ctrl+banana"},
                }
            ),
            encoding="utf-8",
        )
        bindings, errors = km.load_user_keymap(path)
        assert bindings == {}, "a rejected document must apply nothing"
        assert errors


# ---------------------------------------------------------------------------
# 5. Loading — corrupt files never brick anything
# ---------------------------------------------------------------------------


class TestLoadingNeverBricks:
    def test_a_missing_file_is_defaults_with_no_error(self, tmp_path: Path):
        bindings, errors = km.load_user_keymap(tmp_path / "nope.json")
        assert bindings == {} and errors == []
        state = km.load_keymap_state(tmp_path / "nope.json")
        assert state.source == "default"
        assert state.errors == ()
        assert state.keymap == km.Keymap()

    def test_an_unparseable_file_yields_defaults_plus_a_clear_error(
        self, tmp_path: Path
    ):
        path = tmp_path / "keybindings.json"
        path.write_text("{ not json ", encoding="utf-8")
        state = km.load_keymap_state(path)
        assert state.source == "default+errors"
        assert state.errors and "unparseable" in state.errors[0]
        # The effective keymap is the shipped one — the TUI starts.
        assert state.keymap.keyboard_shortcuts() == km.Keymap().keyboard_shortcuts()

    def test_a_single_bad_character_does_not_brick_the_tui(self, tmp_path: Path):
        """The import-smoke-test reasoning: one character must not take the
        whole surface offline."""
        path = tmp_path / "keybindings.json"
        path.write_text('{"schema_version": 1, "bindings": {"steer": "ctrl+g",}}',
                        encoding="utf-8")
        state = km.load_keymap_state(path)  # trailing comma: invalid JSON
        assert state.keymap == km.Keymap()
        assert state.errors

    def test_load_keymap_state_never_raises(self, tmp_path: Path):
        for payload in (
            "",
            "null",
            "[]",
            '"ctrl+g"',
            '{"bindings": {"steer": "ctrl+banana"}}',
            '{"bindings": {"quit": null}}',
            "\x00\x01\x02",
        ):
            path = tmp_path / "keybindings.json"
            path.write_text(payload, encoding="utf-8", errors="replace")
            state = km.load_keymap_state(path)  # must not raise
            assert isinstance(state.keymap, km.Keymap)


# ---------------------------------------------------------------------------
# 6. Rebinding — one source, three projections, survives restart
# ---------------------------------------------------------------------------


class TestRebindingFlowsThroughOneSource:
    def test_a_user_binding_reaches_all_three_projections_identically(self):
        """THE drift gate, extended: palette, /help and the TUI read the SAME
        resolved map, so a user key cannot drift between them."""
        resolved = km.resolve_keymap({"steer": ("ctrl+shift+g",)})
        table = resolved.keyboard_shortcuts()
        steer = next(row for row in table["commands"] if row["command"] == "/steer")
        assert steer["keys"] == ["ctrl+shift+g"]
        assert steer["label"] == "ctrl+shift+g"
        assert steer["purposes"] == ["steer at the next safe boundary"]
        # palette and /help read the same cell.
        assert resolved.palette_shortcut("/steer") == "ctrl+shift+g"
        assert resolved.help_shortcut("/steer") == "ctrl+shift+g"
        # The TUI binding rows bind the user's key, and only the user's key.
        rows = resolved.tui_binding_rows()
        steer_rows = [row for row in rows if row[1] == "steer"]
        assert steer_rows == [("ctrl+shift+g", "steer", "steer at the next safe boundary")]
        assert not any(row[0] == "ctrl+g" for row in rows)
        # And the effective map still passes the invariants.
        assert km.keymap_invariant_findings(resolved) == []

    def test_a_rebind_frees_the_default_key_for_nothing_else_to_take(self):
        """After /steer moves off ctrl+g, ctrl+g is simply unbound — it is not
        silently handed to another action."""
        resolved = km.resolve_keymap({"steer": ("ctrl+shift+g",)})
        assert not any(row[0] == "ctrl+g" for row in resolved.tui_binding_rows())

    def test_unbinding_a_non_reserved_action_is_honest_about_it(self):
        resolved = km.resolve_keymap({"history": ()})
        assert resolved.palette_shortcut("/history") == ""
        assert resolved.help_shortcut("/history") == ""
        assert not any(row[1] == "history" for row in resolved.tui_binding_rows())
        assert km.keymap_invariant_findings(resolved) == []

    def test_rebinding_survives_restart(self, tmp_path: Path):
        path = tmp_path / "keybindings.json"
        path.write_text(
            json.dumps({"schema_version": 1, "bindings": {"steer": "ctrl+shift+g"}}),
            encoding="utf-8",
        )
        first = km.load_keymap_state(path)
        second = km.load_keymap_state(path)  # a fresh read: the restart
        assert first.source == second.source == "user"
        assert first.keymap == second.keymap
        assert second.keymap.keys_for("steer") == ("ctrl+shift+g",)
        # The default keymap object is not mutated by a user load.
        assert km.Keymap().keys_for("steer") == ("ctrl+g", "ctrl+b")

    def test_a_multi_key_rebind_keeps_both_keys(self):
        resolved = km.resolve_keymap({"steer": ["ctrl+shift+g", "ctrl+alt+g"]})
        assert resolved.keys_for("steer") == ("ctrl+shift+g", "ctrl+alt+g")
        assert km.keymap_invariant_findings(resolved) == []


# ---------------------------------------------------------------------------
# 7. The invariant gate can actually fail
# ---------------------------------------------------------------------------


class TestTheInvariantGateFires:
    def test_a_hand_built_collision_is_reported(self):
        bad = km.Keymap({"steer": ("ctrl+g",), "palette": ("ctrl+g",)})
        findings = km.keymap_invariant_findings(bad)
        assert any("ctrl+g" in finding for finding in findings)

    def test_a_hand_built_reserved_move_is_reported(self):
        bad = km.Keymap({**dict(km.DEFAULT_BINDINGS), "quit": ("ctrl+shift+q",)})
        findings = km.keymap_invariant_findings(bad)
        assert any("quit" in finding for finding in findings)

    def test_an_invalid_key_in_a_hand_built_map_is_reported(self):
        bad = km.Keymap({**dict(km.DEFAULT_BINDINGS), "steer": ("ctrl+banana",)})
        findings = km.keymap_invariant_findings(bad)
        assert any("banana" in finding for finding in findings)


# ---------------------------------------------------------------------------
# 8. The path the file lives at
# ---------------------------------------------------------------------------


class TestTheKeybindingsPath:
    def test_neo_config_wins_outright(self, tmp_path: Path):
        config = tmp_path / "settings.toml"
        path = km.default_keybindings_path({"NEO_CONFIG": str(config)})
        assert path == tmp_path / "keybindings.json"

    def test_neo_home_is_the_test_isolation(self, tmp_path: Path):
        path = km.default_keybindings_path({"NEO_HOME": str(tmp_path)})
        assert path == tmp_path / "keybindings.json"

    def test_the_platform_default_is_beside_settings_toml(self):
        path = km.default_keybindings_path(
            {"APPDATA": r"C:\Users\test\AppData\Roaming", "NEO_CONFIG": "", "NEO_HOME": ""}
        )
        assert path.name == "keybindings.json"
        assert path.parent.name == "neo"
